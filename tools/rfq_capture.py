#!/usr/bin/env python3
"""Dedicated passive Kalshi communications-channel capture supervisor.

This process owns a SECOND authenticated WebSocket connection and a distinct
raw-log family::

    work/raw/date=YYYY-MM-DD/rfq_HH.ndjson

It reuses ``build/ws_shadow`` for authentication, reconnects, receive-time
stamping and byte-exact recording.  The child is forced into ``data_collect``
mode, subscribes only to ``communications``, has REST cross-checks disabled,
and is independently refused by ws_shadow if order placement ever becomes
possible.  This module contains no RFQ/quote/order REST client.

Production is deliberately not started by installing this file.  The separate
``kalshi-rfq-capture.service`` must be enabled during an operator maintenance
window.  ``work/live/rfq_disable`` is the one-touch disable flag; when it
appears, only the RFQ child is terminated.  The firehose service/PIDs are never
read, signalled or restarted here.

Official contract pinned during implementation:
  docs/vendor/kalshi/latest/asyncapi.yaml
  sha256 00858d5a892eb7066a8da247660622721e8ee80bfdd89b1b9a24278c15d7d421
Live reference: https://docs.kalshi.com/websockets/communications
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_ROOT = ROOT / "work" / "raw"
DEFAULT_LIVE_ROOT = ROOT / "work" / "live"
DEFAULT_DISABLE = DEFAULT_LIVE_ROOT / "rfq_disable"
DEFAULT_LOCK = DEFAULT_LIVE_ROOT / "rfq_capture.lock"
DEFAULT_ALERT = DEFAULT_LIVE_ROOT / "rfq_alert.json"
DEFAULT_STATE = DEFAULT_LIVE_ROOT / "rfq_state.json"
DEFAULT_LEDGER = DEFAULT_LIVE_ROOT / "rfq_segments.ndjson"
DEFAULT_METRICS = DEFAULT_LIVE_ROOT / "rfq_metrics.ndjson"
DEFAULT_BINARY = ROOT / "build" / "ws_shadow"
CAPTURE_TEMPLATE = "date={UTC_DATE}/rfq_{UTC_HOUR}.ndjson"
DEFAULT_CHILD_LIFETIME_SECONDS = 30 * 24 * 60 * 60
OFFICIAL_ASYNCAPI_SHA256 = (
    "00858d5a892eb7066a8da247660622721e8ee80bfdd89b1b9a24278c15d7d421"
)
HASH_CHUNK_BYTES = 1024 * 1024
FRESH_AUTHORITY_MAX_BYTES = 1024 * 1024
FRESH_AUTHORITY_READ_BYTES = 1024 * 1024
FRESH_PRECOMMIT_CTIME_TOLERANCE_SECONDS = 5.0


class Refused(RuntimeError):
    """A fail-closed startup refusal (configuration/safety, not a retry)."""


def _fresh_receipts_module():
    """Lazy import: the unbound diagnostic collector needs no fresh module."""
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import fresh_rfq_receipts  # pylint: disable=import-outside-toplevel
    return fresh_rfq_receipts


def _fresh_precommit_module():
    """Lazy import of the canonical precommit envelope contract."""
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import precommit_fresh_rfq_authority  # pylint: disable=import-outside-toplevel
    return precommit_fresh_rfq_authority


def _json_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: %s" % key)
        value[key] = item
    return value


def probe_clean_git_head(root: Path = ROOT) -> str:
    """Return a clean checkout HEAD using fixed argv and no shell."""
    root = Path(root).resolve()
    safe_directory = "safe.directory=%s" % root
    git_env = {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent",
    }
    base = [
        "/usr/bin/git", "-c", safe_directory,
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "core.untrackedCache=false", "-C", str(root),
    ]
    status = subprocess.run(
        base + ["status", "--porcelain=v1", "--untracked-files=all"],
        shell=False, check=False, text=True, capture_output=True, timeout=10,
        env=git_env)
    if status.returncode != 0:
        raise Refused("cannot verify clean capture worktree: %s" %
                      status.stderr.strip())
    if status.stdout:
        raise Refused("capture worktree is not clean")
    head = subprocess.run(
        base + ["rev-parse", "--verify", "HEAD"],
        shell=False, check=False, text=True, capture_output=True, timeout=10,
        env=git_env)
    value = head.stdout.strip()
    if head.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise Refused("cannot resolve exact capture worktree HEAD")
    return value


def _open_directory_nofollow(path: Path) -> int:
    """Open every absolute directory component without following symlinks."""
    if not path.is_absolute() or not hasattr(os, "O_NOFOLLOW") or \
            not hasattr(os, "O_DIRECTORY"):
        raise Refused("safe authority directory traversal is unavailable")
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
             getattr(os, "O_CLOEXEC", 0))
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _utc_seconds(value: str, label: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError) as exc:
        raise Refused("invalid %s in fresh authority envelope" % label) from exc


def load_fresh_lane_authority(
    path_value: str | os.PathLike, *, expected_owner_uid: int,
    expected_generation: str | None = None,
    runtime_commit: str | None = None,
    git_probe=probe_clean_git_head,
    now: dt.datetime | None = None,
) -> dict:
    """Read one canonical pre-T0 envelope through a pinned parent/openat FD."""
    if type(expected_owner_uid) is not int or expected_owner_uid < 0:
        raise Refused("fresh authority expected owner UID is invalid")
    path = Path(os.path.abspath(os.fspath(Path(path_value).expanduser())))
    parent_fd = -1
    try:
        parent_fd = _open_directory_nofollow(path.parent)
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_uid != expected_owner_uid or
                stat.S_IMODE(parent_info.st_mode) & 0o022):
            raise Refused("fresh authority parent owner/mode is unsafe")
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise Refused("fresh lane authority symlink is forbidden: %s" % path)
        if not stat.S_ISREG(before.st_mode):
            raise Refused("fresh lane authority is not a regular file: %s" % path)
        if (before.st_uid != expected_owner_uid or before.st_nlink != 1 or
                stat.S_IMODE(before.st_mode) != 0o444):
            raise Refused("fresh lane authority owner/mode/link count is unsafe")
        if before.st_size <= 0 or before.st_size > FRESH_AUTHORITY_MAX_BYTES:
            raise Refused("fresh lane authority size is outside 1..%d bytes" %
                          FRESH_AUTHORITY_MAX_BYTES)
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        digest = hashlib.sha256()
        payload = bytearray()
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                 opened.st_mtime_ns, opened.st_ctime_ns, opened.st_uid,
                 opened.st_nlink) !=
                    (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                     before.st_mtime_ns, before.st_ctime_ns, before.st_uid,
                     before.st_nlink)):
                raise Refused("fresh lane authority identity changed during open")
            while True:
                chunk = os.read(fd, FRESH_AUTHORITY_READ_BYTES)
                if not chunk:
                    break
                payload.extend(chunk)
                digest.update(chunk)
                if len(payload) > FRESH_AUTHORITY_MAX_BYTES:
                    raise Refused("fresh lane authority exceeded size limit")
            after = os.fstat(fd)
        finally:
            os.close(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if ((after.st_dev, after.st_ino, after.st_mode, after.st_size,
             after.st_mtime_ns, after.st_ctime_ns, after.st_uid, after.st_nlink) !=
                (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                 before.st_mtime_ns, before.st_ctime_ns, before.st_uid,
                 before.st_nlink) or
                (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino) or
                len(payload) != before.st_size):
            raise Refused("fresh lane authority changed while reading")
        decoded = json.loads(payload.decode("utf-8"),
                             object_pairs_hook=_json_without_duplicate_keys)
        precommit = _fresh_precommit_module()
        envelope = precommit.validate_precommit_envelope(decoded)
        canonical = _fresh_receipts_module().canonical_bytes(envelope) + b"\n"
        if bytes(payload) != canonical:
            raise Refused(
                "fresh lane authority must be canonical JSON plus one newline")
        authority = envelope["authority"]
        if (expected_generation is not None and
                (not isinstance(expected_generation, str) or
                 _fresh_receipts_module().GENERATION_RE.fullmatch(
                     expected_generation) is None or
                 authority["generation"] != expected_generation)):
            raise Refused(
                "fresh authority generation differs from service generation")
        precommitted_at = _utc_seconds(
            envelope["precommitted_at_utc"], "precommitted_at_utc")
        strict_t0 = _utc_seconds(envelope["strict_t0_utc"], "strict_t0_utc")
        ctime = dt.datetime.fromtimestamp(
            opened.st_ctime_ns / 1_000_000_000, tz=dt.timezone.utc)
        minimum_lead = envelope["minimum_lead_seconds"]
        observed_now = (dt.datetime.now(dt.timezone.utc) if now is None else now)
        if not isinstance(observed_now, dt.datetime) or observed_now.tzinfo is None:
            raise Refused("fresh authority validation clock is invalid")
        observed_now = observed_now.astimezone(dt.timezone.utc)
        if (abs((ctime - precommitted_at).total_seconds()) >
                FRESH_PRECOMMIT_CTIME_TOLERANCE_SECONDS or
                (strict_t0 - precommitted_at).total_seconds() < minimum_lead or
                (strict_t0 - ctime).total_seconds() < minimum_lead or
                precommitted_at > observed_now or ctime > observed_now):
            raise Refused("fresh authority filesystem/precommit T0 timing is invalid")
        if runtime_commit is None:
            runtime_commit = git_probe(ROOT)
        if (not isinstance(runtime_commit, str) or
                re.fullmatch(r"[0-9a-f]{40}", runtime_commit) is None or
                authority["deployment_commit"] != runtime_commit):
            raise Refused("fresh authority deployment commit differs from clean runtime HEAD")
    except Refused:
        raise
    except Exception as exc:
        raise Refused("fresh lane authority validation failed: %s" % exc) from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    return {
        "path": str(path),
        "expected_owner_uid": expected_owner_uid,
        "expected_generation": expected_generation,
        "runtime_commit": runtime_commit,
        "git_probe": git_probe,
        "identity": {
            "device": before.st_dev, "inode": before.st_ino,
            "mode": before.st_mode, "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns,
            "uid": before.st_uid, "nlink": before.st_nlink,
        },
        "file_sha256": digest.hexdigest(),
        "envelope": envelope,
        "authority": authority,
    }


def revalidate_fresh_lane_authority(initial: dict) -> dict:
    """Require the launch-time authority identity and digest to remain fixed."""
    git_probe = initial.get("git_probe", probe_clean_git_head)
    try:
        current_runtime_commit = git_probe(ROOT)
    except Exception as exc:
        raise Refused("capture worktree changed after authority load: %s" % exc) from exc
    if current_runtime_commit != initial.get("runtime_commit"):
        raise Refused("capture worktree HEAD changed after authority load")
    current = load_fresh_lane_authority(
        initial["path"], expected_owner_uid=initial["expected_owner_uid"],
        expected_generation=initial.get("expected_generation"),
        runtime_commit=current_runtime_commit, git_probe=git_probe)
    if (current["identity"] != initial.get("identity") or
            current["file_sha256"] != initial.get("file_sha256") or
            current["authority"].get("authority_sha256") !=
            initial.get("authority", {}).get("authority_sha256") or
            current["authority"] != initial.get("authority")):
        raise Refused("fresh lane authority identity/digest changed after launch")
    return current


def fresh_lane_finalize_guard(binding: dict | None) -> tuple[bool, list[str]]:
    """Convert a changed bound authority into receipt-blocking evidence."""
    if binding is None:
        return True, []
    try:
        revalidate_fresh_lane_authority(binding)
    except Refused as exc:
        return False, ["fresh lane authority invalidated: %s" % exc]
    return True, []


def fresh_lane_receipt_fields(binding: dict | None, segment_hour: str, *,
                              supervisor_pid: int, child_pid: int,
                              child_generation: int,
                              child_subscription_ack_wall_ns: int | None = None,
                              child_subscription_ack_identity_sha256: str | None = None,
                              child_subscription_ack_count: int = 0,
                              authority_valid: bool = True) -> dict:
    """Build provenance fields without ever claiming research eligibility."""
    fields = {
        "fresh_lane_state": "UNBOUND_DIAGNOSTIC",
        "supervisor_pid": supervisor_pid,
        "child_pid": child_pid,
        "child_generation": child_generation,
        "child_subscription_ack_wall_ns": child_subscription_ack_wall_ns,
        "child_subscription_ack_identity_sha256":
            child_subscription_ack_identity_sha256,
        "child_subscription_ack_count": child_subscription_ack_count,
    }
    if binding is None:
        return fields
    authority = binding["authority"]
    segment_start = dt.datetime.strptime(segment_hour, "%Y-%m-%dT%H").replace(
        tzinfo=dt.timezone.utc)
    strict_t0 = dt.datetime.strptime(
        authority["strict_t0_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    if not authority_valid:
        state = "BOUND_AUTHORITY_INVALID"
    elif segment_start < strict_t0:
        state = "BOUND_PRE_T0_DIAGNOSTIC"
    else:
        state = "BOUND_AUTHORITY"
    fields.update({
        "fresh_lane_state": state,
        "lane_id": authority["lane_id"],
        "authority_sha256": authority["authority_sha256"],
        "deployment_commit": authority["deployment_commit"],
        "generation": authority["generation"],
    })
    return fields


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _alert(path: Path, reason: str, *, child_rc: int | None = None) -> None:
    payload = {
        "type": "rfq_capture_alert",
        "status": "ALERT",
        "observed_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
    }
    if child_rc is not None:
        payload["child_rc"] = child_rc
    _atomic_json(path, payload)


def _write_state(path: Path, status: str, **details) -> None:
    payload = {
        "type": "rfq_capture_state",
        "status": status,
        "observed_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload.update(details)
    _atomic_json(path, payload)


def metrics_loss_since(path: Path, byte_offset: int, *,
                       start_ms: int | None = None,
                       end_ms: int | None = None) -> list[str]:
    """Return explicit recorder/overflow loss evidence appended by one child.

    ws_shadow's historical process exit predicate does not include recorder
    drops/WS buffer overflow.  The isolated wrapper therefore treats its feed
    metrics as a second, independent completeness gate.
    """
    findings: list[str] = []
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            data = fh.read()
    except OSError:
        return ["RFQ metrics missing/unreadable after child run"]
    for raw in data.splitlines():
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if row.get("type") != "feed":
            continue
        ts = row.get("ts_ms")
        if start_ms is not None and (type(ts) is not int or ts < start_ms):
            continue
        if end_ms is not None and (type(ts) is not int or ts >= end_ms):
            continue
        dropped = int(row.get("recorder_dropped", 0) or 0)
        write_failures = int(row.get("recorder_write_failures", 0) or 0)
        if dropped:
            findings.append("recorder_dropped=%d" % dropped)
        if write_failures:
            findings.append("recorder_write_failures=%d" % write_failures)
        if row.get("connected") is True and row.get("valid") is False and not dropped:
            findings.append("connected feed invalid (possible WS buffer overflow)")
    return sorted(set(findings))


def metrics_evidence_since(path: Path, byte_offset: int, *,
                           start_ms: int | None = None,
                           end_ms: int | None = None) -> dict:
    evidence = {
        "feed_rows": 0, "connected_valid_rows": 0,
        "min_reconnects": None, "max_reconnects": 0,
        "min_disconnects": None, "max_disconnects": 0,
        "min_errors": None, "max_errors": 0,
        "max_recorder_dropped": 0, "max_recorder_write_failures": 0,
        "first_ts_ms": None, "last_ts_ms": None, "findings": [],
        "end_offset": byte_offset, "next_window_offset": None,
    }
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            while True:
                line_start = fh.tell()
                raw = fh.readline()
                if not raw:
                    evidence["end_offset"] = fh.tell()
                    break
                if not raw.endswith(b"\n"):
                    # Active writer may be between fwrite calls. Re-read this
                    # incomplete row on the next incremental pass.
                    evidence["end_offset"] = line_start
                    break
                evidence["end_offset"] = fh.tell()
                try:
                    row = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                ts = row.get("ts_ms")
                if (end_ms is not None and type(ts) is int and ts >= end_ms and
                        evidence["next_window_offset"] is None):
                    evidence["next_window_offset"] = line_start
                if row.get("type") != "feed":
                    continue
                if start_ms is not None and (type(ts) is not int or ts < start_ms):
                    continue
                if end_ms is not None and (type(ts) is not int or ts >= end_ms):
                    continue
                evidence["feed_rows"] += 1
                if type(ts) is int:
                    if evidence["first_ts_ms"] is None:
                        evidence["first_ts_ms"] = ts
                    evidence["last_ts_ms"] = ts
                if row.get("connected") is True and row.get("valid") is True:
                    evidence["connected_valid_rows"] += 1
                reconnects = int(row.get("reconnects", 0) or 0)
                if evidence["min_reconnects"] is None:
                    evidence["min_reconnects"] = reconnects
                evidence["min_reconnects"] = min(
                    evidence["min_reconnects"], reconnects)
                evidence["max_reconnects"] = max(
                    evidence["max_reconnects"], reconnects)
                disconnects = int(row.get("disconnects", 0) or 0)
                errors = int(row.get("errors", 0) or 0)
                if evidence["min_disconnects"] is None:
                    evidence["min_disconnects"] = disconnects
                if evidence["min_errors"] is None:
                    evidence["min_errors"] = errors
                evidence["min_disconnects"] = min(
                    evidence["min_disconnects"], disconnects)
                evidence["max_disconnects"] = max(
                    evidence["max_disconnects"], disconnects)
                evidence["min_errors"] = min(evidence["min_errors"], errors)
                evidence["max_errors"] = max(evidence["max_errors"], errors)
                dropped = int(row.get("recorder_dropped", 0) or 0)
                write_failures = int(row.get("recorder_write_failures", 0) or 0)
                evidence["max_recorder_dropped"] = max(
                    evidence["max_recorder_dropped"], dropped)
                evidence["max_recorder_write_failures"] = max(
                    evidence["max_recorder_write_failures"], write_failures)
                if dropped:
                    evidence["findings"].append("recorder_dropped=%d" % dropped)
                if write_failures:
                    evidence["findings"].append(
                        "recorder_write_failures=%d" % write_failures)
                if (row.get("connected") is True and row.get("valid") is False and
                        not dropped and not write_failures):
                    evidence["findings"].append(
                        "connected feed invalid (possible WS buffer overflow)")
    except OSError:
        evidence["findings"] = ["RFQ metrics missing/unreadable after child run"]
        return evidence
    if evidence["next_window_offset"] is None:
        evidence["next_window_offset"] = evidence["end_offset"]
    if evidence["feed_rows"] == 0:
        evidence["findings"].append("no RFQ feed metrics rows")
    if evidence["connected_valid_rows"] == 0:
        evidence["findings"].append("no connected+valid RFQ feed heartbeat")
    evidence["findings"] = sorted(set(evidence["findings"]))
    return evidence


def capture_evidence_since(path: Path, byte_offset: int, expected_hour: str) -> dict:
    """Backward-compatible whole-hour reader, now spanning every shard."""
    return capture_evidence_since_shards(
        Path(path), {str(Path(path)): byte_offset}, expected_hour,
        require_base=True)


def _append_ndjson(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        data = memoryview(
            (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode())
        while data:
            wrote = os.write(fd, data)
            if wrote <= 0:
                raise OSError("short write while persisting RFQ receipt")
            data = data[wrote:]
        os.fsync(fd)
    finally:
        os.close(fd)


def persist_segment_receipt(receipt_raw_path: Path, ledger_path: Path, receipt: dict) -> None:
    """Append the same receipt to seal-covered raw metadata and live ledger."""
    now_wall = time.time_ns()
    outer = {
        "recv_mono_ns": time.monotonic_ns(),
        "recv_wall_ns": now_wall,
        "source": "Kalshi",
        "channel": "rfq_segment_receipt",
        "source_ticker": "",
        "marker": "segment_receipt",
        "raw": json.dumps(receipt, sort_keys=True, separators=(",", ":")),
    }
    _append_ndjson(receipt_raw_path, outer)
    _append_ndjson(ledger_path, receipt)


def hour_deadline(now: dt.datetime) -> dt.datetime:
    now = now.astimezone(dt.timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)


def child_environment(parent: dict[str, str], capture: Path, metrics: Path,
                      seconds: int, *, validate_credentials: bool = True
                      ) -> dict[str, str]:
    """Return the least-privilege ws_shadow environment or raise Refused."""
    inherited_mode = parent.get("KALSHI_MODE", "").strip().lower()
    if inherited_mode == "live":
        raise Refused("inherited KALSHI_MODE=live")
    if parent.get("KALSHI_ENV") != "prod":
        raise Refused("KALSHI_ENV must be exactly prod")
    if parent.get("KALSHI_ALLOW_PROD") != "1":
        raise Refused("KALSHI_ALLOW_PROD must be exactly 1")
    if parent.get("KALSHI_API_KEY_SCOPE") != "read":
        raise Refused("dedicated RFQ key must declare KALSHI_API_KEY_SCOPE=read")
    if validate_credentials:
        if not parent.get("KALSHI_API_KEY_ID"):
            raise Refused("KALSHI_API_KEY_ID is missing")
        key = parent.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not key or not Path(key).is_file():
            raise Refused("KALSHI_PRIVATE_KEY_PATH is missing or unreadable")

    env = dict(parent)
    # Explicitly remove every permission-expanding flag known to this repo.
    for key in ("KALSHI_ALLOW_LIVE", "KALSHI_WS_TICKERS",
                "KALSHI_HOST_UNSAFE_OVERRIDE", "KALSHI_BASE_URL",
                "KALSHI_WS_URL", "KALSHI_WS_SIGN_PATH"):
        env.pop(key, None)
    env["KALSHI_MODE"] = "data_collect"
    env["KALSHI_BASE_URL"] = "https://external-api.kalshi.com"
    env["KALSHI_WS_URL"] = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    env["KALSHI_WS_SIGN_PATH"] = "/trade-api/ws/v2"
    # ws_shadow calls this flag "firehose"; here it only means a global
    # subscription with no market_tickers.  Kalshi documents that market
    # specification is ignored for the communications channel.
    env["KALSHI_WS_FIREHOSE"] = "1"
    env["KALSHI_WS_CHANNELS"] = "communications"
    env["KALSHI_WS_OMIT_USE_YES_PRICE"] = "1"
    env["KALSHI_SHADOW_SECONDS"] = str(seconds)
    env["KALSHI_SHADOW_CAPTURE"] = str(capture)
    env["KALSHI_SHADOW_METRICS"] = str(metrics)
    env["KALSHI_SHADOW_XCHECK"] = "0"
    return env


def segment(now: dt.datetime, raw_root: Path) -> tuple[Path, int]:
    """Current hourly raw path and bounded seconds remaining in the UTC hour."""
    now = now.astimezone(dt.timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    # Unlike the legacy supervisor's minimum-30s convenience, never let a
    # late service start deliberately write past the UTC hour boundary.
    seconds = max(1, int(math.ceil((next_hour - now).total_seconds())))
    path = raw_root / ("date=" + now.strftime("%Y-%m-%d")) / (
        "rfq_" + now.strftime("%H") + ".ndjson"
    )
    return path, seconds


def capture_template(raw_root: Path) -> Path:
    """Receive-clock template consumed by RawLogWriter inside ws_shadow."""
    return raw_root / "date={UTC_DATE}" / "rfq_{UTC_HOUR}.ndjson"


def _hour_start(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _empty_capture_evidence() -> dict:
    return {
        "recorder_rows": 0, "subscribed_communications": 0,
        "subscription_ack_wall_ns": None,
        "subscription_ack_identity_sha256": None,
        "rfq_created": 0, "rfq_deleted": 0,
        "markers": {}, "partition_mismatches": 0, "max_stream_epoch": 0,
        "subscription_invalidations": 0, "subscription_proven_at_end": None,
        "findings": [], "start_offsets": {}, "end_offsets": {}, "shards": [],
    }


def discover_hour_shards(base_path: Path, *, require_base: bool = False
                         ) -> tuple[list[Path], list[str]]:
    """Return ``base,.1,.2,...`` without following hostile filesystem links.

    RawLogWriter resumes the highest *contiguous* shard.  Treating a gapped,
    aliased, or non-regular shard set as valid would let the supervisor attest
    a different byte stream from the writer, so discovery is deliberately
    fail-closed.
    """
    base_path = Path(base_path)
    findings: list[str] = []
    indexed: dict[int, Path] = {}
    try:
        entries = list(base_path.parent.iterdir())
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        return [], ["RFQ shard directory unreadable: %s" % exc]
    prefix = base_path.name + "."
    for path in entries:
        if path.name == base_path.name:
            ordinal = 0
        elif path.name.startswith(prefix):
            suffix = path.name[len(prefix):]
            if not re.fullmatch(r"[1-9][0-9]*", suffix):
                # Only names that look like rotation candidates are relevant;
                # unrelated sidecars do not poison the hour.
                if suffix.isdigit():
                    findings.append("non-canonical RFQ shard suffix: %s" % path)
                continue
            ordinal = int(suffix)
        else:
            continue
        if ordinal in indexed:
            findings.append("duplicate RFQ shard ordinal %d" % ordinal)
            continue
        indexed[ordinal] = path
    if not indexed:
        if require_base:
            findings.append("RFQ raw file missing/unreadable after child run")
        return [], sorted(set(findings))
    if 0 not in indexed:
        findings.append("RFQ shard set is missing base object")
    missing: list[str] = []
    expected = 0
    for actual in sorted(indexed):
        if actual > expected:
            missing.append(str(expected) if actual == expected + 1 else
                           "%d-%d" % (expected, actual - 1))
        expected = actual + 1
    if missing:
        findings.append("non-contiguous RFQ shard set missing ordinals=%s" %
                        ",".join(missing))
    seen_inodes: set[tuple[int, int]] = set()
    ordered: list[Path] = []
    for ordinal in sorted(indexed):
        path = indexed[ordinal]
        try:
            info = path.lstat()
        except OSError as exc:
            findings.append("RFQ shard unreadable %s: %s" % (path, exc))
            continue
        if stat.S_ISLNK(info.st_mode):
            findings.append("RFQ shard symlink forbidden: %s" % path)
            continue
        if not stat.S_ISREG(info.st_mode):
            findings.append("RFQ shard is not a regular file: %s" % path)
            continue
        inode = (info.st_dev, info.st_ino)
        if inode in seen_inodes:
            findings.append("RFQ shard inode alias forbidden: %s" % path)
            continue
        seen_inodes.add(inode)
        ordered.append(path)
    return ordered, sorted(set(findings))


def hour_shard_snapshot(base_path: Path, *, require_base: bool = False) -> dict:
    paths, findings = discover_hour_shards(base_path, require_base=require_base)
    shards = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            findings.append("RFQ shard unreadable %s: %s" % (path, exc))
            continue
        suffix = path.name[len(base_path.name):]
        ordinal = 0 if not suffix else int(suffix[1:])
        shards.append({
            "ordinal": ordinal, "path": str(path), "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "device": info.st_dev,
            "inode": info.st_ino,
        })
    return {"shards": shards, "findings": sorted(set(findings))}


def _snapshot_offsets(snapshot: dict) -> dict[str, int]:
    return {str(row["path"]): int(row["size"])
            for row in snapshot.get("shards", [])}


def _snapshot_signature(snapshot: dict) -> tuple:
    if snapshot.get("findings"):
        return ("INVALID", tuple(snapshot["findings"]))
    return tuple((row["ordinal"], row["path"], row["device"], row["inode"],
                  row["size"], row["mtime_ns"], row["ctime_ns"])
                 for row in snapshot.get("shards", []))


def final_attestation_snapshot_findings(before: dict, after: dict) -> list[str]:
    """Reject every shard-set mutation across final parse/hash attestation."""
    findings = list(before.get("findings", [])) + list(after.get("findings", []))
    if _snapshot_signature(before) != _snapshot_signature(after):
        findings.append("RFQ shard set changed during final attestation/rescan")
    return sorted(set(findings))


def _merge_capture_evidence(total: dict, chunk: dict) -> dict:
    for key in ("recorder_rows", "subscribed_communications", "rfq_created",
                "rfq_deleted", "partition_mismatches",
                "subscription_invalidations"):
        total[key] += int(chunk.get(key, 0))
    total["max_stream_epoch"] = max(
        int(total.get("max_stream_epoch", 0)),
        int(chunk.get("max_stream_epoch", 0)))
    if (total.get("subscription_ack_wall_ns") is None and
            chunk.get("subscription_ack_wall_ns") is not None):
        total["subscription_ack_wall_ns"] = chunk["subscription_ack_wall_ns"]
        total["subscription_ack_identity_sha256"] = \
            chunk.get("subscription_ack_identity_sha256")
    for key, count in chunk.get("markers", {}).items():
        total["markers"][key] = total["markers"].get(key, 0) + int(count)
    if chunk.get("subscription_proven_at_end") is not None:
        total["subscription_proven_at_end"] = bool(
            chunk["subscription_proven_at_end"])
    total["findings"] = sorted(set(total.get("findings", [])) |
                                set(chunk.get("findings", [])))
    total["end_offsets"].update(chunk.get("end_offsets", {}))
    # Preserve every shard observed during the segment.  The newest chunk can
    # omit a shard only when the file disappeared, which is itself a blocking
    # integrity finding and must not erase the earlier evidence inventory.
    total["shards"] = list(dict.fromkeys(
        list(total.get("shards", [])) + list(chunk.get("shards", []))))
    return total


def _consume_capture_line(evidence: dict, raw_line: bytes,
                          expected_hour: str) -> None:
    try:
        outer = json.loads(raw_line)
        wall = outer.get("recv_wall_ns")
        if type(wall) is not int or wall <= 0:
            raise ValueError("bad wall clock")
    except (ValueError, TypeError):
        evidence["findings"].append("malformed recorder row")
        return
    evidence["recorder_rows"] += 1
    epoch = outer.get("stream_epoch")
    if type(epoch) is int:
        evidence["max_stream_epoch"] = max(evidence["max_stream_epoch"], epoch)
    actual_hour = dt.datetime.fromtimestamp(
        wall / 1e9, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H")
    if actual_hour != expected_hour:
        evidence["partition_mismatches"] += 1
    marker = outer.get("marker")
    if marker:
        key = str(marker)
        evidence["markers"][key] = evidence["markers"].get(key, 0) + 1
        if key in ("transport_close", "transport_error"):
            evidence["subscription_invalidations"] += 1
            evidence["subscription_proven_at_end"] = False
            evidence["findings"].append(
                "transport boundary marker present: %s" % key)
        return
    try:
        frame = json.loads(outer["raw"])
    except (ValueError, KeyError, TypeError):
        evidence["findings"].append("malformed inner WS frame")
        return
    typ = frame.get("type")
    if typ == "subscribed":
        msg = frame.get("msg")
        if (isinstance(msg, dict) and msg.get("channel") == "communications" and
                type(msg.get("sid")) is int and msg["sid"] > 0):
            evidence["subscribed_communications"] += 1
            evidence["subscription_proven_at_end"] = True
            if evidence["subscription_ack_wall_ns"] is None:
                evidence["subscription_ack_wall_ns"] = wall
                evidence["subscription_ack_identity_sha256"] = \
                    hashlib.sha256(raw_line).hexdigest()
    elif typ in ("error", "unsubscribed"):
        evidence["subscription_invalidations"] += 1
        evidence["subscription_proven_at_end"] = False
        evidence["findings"].append(
            "unexpected communications control frame: %s" % typ)
    elif typ in ("rfq_created", "rfq_deleted"):
        evidence[typ] += 1


def capture_evidence_since_shards(base_path: Path, offsets: dict[str, int],
                                   expected_hour: str, *,
                                   require_base: bool = True) -> dict:
    """Incrementally parse every contiguous rotation shard for one UTC hour."""
    evidence = _empty_capture_evidence()
    evidence["start_offsets"] = dict(offsets)
    # Retain cursors for previously observed shards even if one disappears.
    # This makes disappearance sticky on every later sample instead of
    # forgetting the missing object after a single pass.
    evidence["end_offsets"] = dict(offsets)
    paths, findings = discover_hour_shards(base_path, require_base=require_base)
    evidence["findings"].extend(findings)
    current_paths = {str(path) for path in paths}
    vanished = sorted(set(offsets) - current_paths)
    if vanished:
        evidence["findings"].append(
            "RFQ shard disappeared after observation: %s" % ",".join(vanished))
    for path in paths:
        key = str(path)
        start = int(offsets.get(key, 0))
        evidence["shards"].append(key)
        try:
            before = path.lstat()
            if start < 0 or start > before.st_size:
                evidence["findings"].append(
                    "RFQ shard truncated below cursor: %s" % path)
                start = 0
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("not a regular file")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OSError("identity changed during open")
            with os.fdopen(fd, "rb", closefd=True) as fh:
                fh.seek(start)
                end = start
                while True:
                    line_start = fh.tell()
                    raw_line = fh.readline()
                    if not raw_line:
                        end = fh.tell()
                        break
                    if not raw_line.endswith(b"\n"):
                        # The writer may be between fwrite calls. Re-read the
                        # incomplete row on the next incremental pass.
                        end = line_start
                        break
                    end = fh.tell()
                    _consume_capture_line(evidence, raw_line, expected_hour)
            evidence["end_offsets"][key] = end
        except OSError as exc:
            evidence["findings"].append(
                "RFQ shard unreadable %s: %s" % (path, exc))
            evidence["end_offsets"][key] = start
    if evidence["partition_mismatches"]:
        evidence["findings"].append(
            "recv-hour/path partition mismatch=%d" %
            evidence["partition_mismatches"])
    if evidence["markers"].get("loss", 0) or evidence["markers"].get("gap", 0):
        evidence["findings"].append("raw recorder loss/gap marker present")
    evidence["findings"] = sorted(set(evidence["findings"]))
    return evidence


def _stream_file_attestation(path: Path, *, chunk_bytes: int = HASH_CHUNK_BYTES
                             ) -> tuple[int, str]:
    """Hash a fixed regular-file identity with bounded memory and no symlinks."""
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError("not a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("identity changed during open")
        while True:
            chunk = os.read(fd, chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) or
            total != before.st_size):
        raise OSError("file changed while hashing")
    return total, digest.hexdigest()


def attest_hour_shards(base_path: Path, raw_root: Path,
                       offsets_before: dict[str, int]) -> tuple[list[dict], list[str]]:
    paths, findings = discover_hour_shards(base_path, require_base=True)
    objects: list[dict] = []
    for path in paths:
        try:
            relpath = path.relative_to(raw_root).as_posix()
            size, sha256 = _stream_file_attestation(path)
            suffix = path.name[len(base_path.name):]
            ordinal = 0 if not suffix else int(suffix[1:])
            before = int(offsets_before.get(str(path), 0))
            if before < 0 or before > size:
                raise OSError("invalid pre-capture byte cursor")
            objects.append({
                "ordinal": ordinal, "relpath": relpath,
                "bytes_before": before, "size": size, "sha256": sha256,
            })
        except (OSError, ValueError) as exc:
            findings.append("RFQ shard attestation failed %s: %s" % (path, exc))
    return objects, sorted(set(findings))


def bind_complete_line_eof(shards: list[dict], final_offsets: dict[str, int],
                           raw_root: Path) -> tuple[list[dict], list[str]]:
    """Bind portable parsed EOF offsets to the exact attested shard set."""
    bound: list[dict] = []
    findings: list[str] = []
    expected_paths: set[str] = set()
    for shard in shards:
        member = dict(shard)
        relpath = member.get("relpath")
        if not isinstance(relpath, str):
            findings.append("RFQ attested shard has invalid relpath")
            member["parsed_bytes_at_close"] = None
            bound.append(member)
            continue
        absolute = str(raw_root / relpath)
        expected_paths.add(absolute)
        parsed = final_offsets.get(absolute)
        member["parsed_bytes_at_close"] = parsed
        if type(parsed) is not int:
            findings.append("RFQ shard missing final complete-line cursor: %s" %
                            relpath)
        elif parsed != member.get("size"):
            findings.append(
                "RFQ shard has unparsed/torn bytes at close: %s parsed=%d size=%s" %
                (relpath, parsed, member.get("size")))
        bound.append(member)
    for path in sorted(set(final_offsets) - expected_paths):
        findings.append(
            "RFQ final cursor is absent from attested shard set: %s" % path)
    return bound, sorted(set(findings))


def _canonical_capture_shards(shards: list[dict]) -> list[dict]:
    """Validate and canonicalize a complete portable RFQ shard identity."""
    if not isinstance(shards, list) or not shards:
        raise ValueError("capture shard set must be a non-empty list")
    canonical: list[dict] = []
    seen_ordinals: set[int] = set()
    seen_relpaths: set[str] = set()
    pattern = re.compile(
        r"^date=(\d{4}-\d{2}-\d{2})/rfq_(?:[01]\d|2[0-3])\.ndjson"
        r"(?:\.([1-9][0-9]*))?$")
    for row in shards:
        if not isinstance(row, dict):
            raise ValueError("capture shard member must be an object")
        ordinal = row.get("ordinal")
        relpath = row.get("relpath")
        bytes_before = row.get("bytes_before")
        size = row.get("size")
        parsed = row.get("parsed_bytes_at_close")
        sha256 = row.get("sha256")
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("capture shard ordinal is invalid")
        if ordinal in seen_ordinals:
            raise ValueError("duplicate capture shard ordinal")
        if not isinstance(relpath, str) or "\\" in relpath:
            raise ValueError("capture shard relpath is unsafe")
        matched = pattern.fullmatch(relpath)
        if not matched:
            raise ValueError("capture shard relpath is unsafe")
        try:
            dt.date.fromisoformat(matched.group(1))
        except ValueError as exc:
            raise ValueError("capture shard relpath date is invalid") from exc
        rel_ordinal = 0 if matched.group(2) is None else int(matched.group(2))
        if rel_ordinal != ordinal or relpath in seen_relpaths:
            raise ValueError("capture shard relpath/ordinal is duplicated or mismatched")
        if (type(bytes_before) is not int or bytes_before < 0 or
                type(size) is not int or size < 0 or bytes_before > size or
                type(parsed) is not int or parsed != size):
            raise ValueError("capture shard byte bounds are invalid")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("capture shard SHA-256 is invalid")
        seen_ordinals.add(ordinal)
        seen_relpaths.add(relpath)
        canonical.append({
            "ordinal": ordinal, "relpath": relpath,
            "bytes_before": bytes_before, "size": size,
            "parsed_bytes_at_close": parsed, "sha256": sha256,
        })
    canonical.sort(key=lambda row: (row["ordinal"], row["relpath"]))
    if [row["ordinal"] for row in canonical] != list(range(len(canonical))):
        raise ValueError("capture shard ordinals are not contiguous from zero")
    return canonical


def capture_shard_set_sha256(shards: list[dict]) -> str:
    canonical = _canonical_capture_shards(shards)
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def subscription_startup_timed_out(child_age_s: float, proven: bool,
                                   grace_s: float) -> bool:
    return child_age_s > grace_s and not proven


def wait_for_partition_close(old_path: Path, new_path: Path, new_hour: str,
                             child: subprocess.Popen, *, timeout_s: float,
                             stable_s: float) -> tuple[bool, int]:
    """Wait for writer rollover plus whole old-shard-set stability.

    ``hour_open`` is emitted by the single recorder writer after its current
    queue drain.  The extra stability interval catches a delayed pre-boundary
    row that is routed back by its receive timestamp.  If anything appends
    even later, the receipt SHA will no longer match the final day seal and the
    48-hour report still fails closed.
    """
    deadline = time.monotonic() + timeout_s
    saw_new_marker = False
    stable_since = None
    last_signature = _snapshot_signature(
        hour_shard_snapshot(old_path, require_base=True))
    new_offsets: dict[str, int] = {}
    while time.monotonic() < deadline and child.poll() is None:
        new_evidence = capture_evidence_since_shards(
            new_path, new_offsets, new_hour, require_base=False)
        new_offsets = dict(new_evidence.get("end_offsets", new_offsets))
        if new_evidence["markers"].get("hour_open", 0):
            saw_new_marker = True
        snapshot = hour_shard_snapshot(old_path, require_base=True)
        signature = _snapshot_signature(snapshot)
        valid = bool(snapshot.get("shards")) and not snapshot.get("findings")
        if saw_new_marker and valid and signature == last_signature:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return True, int((time.monotonic() - stable_since) * 1000)
        else:
            stable_since = None
        last_signature = signature
        time.sleep(0.05)
    stable_ms = 0 if stable_since is None else int(
        (time.monotonic() - stable_since) * 1000)
    return False, stable_ms


def _terminate_child(child: subprocess.Popen, timeout_s: float = 20.0) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _acquire_lock(lock_path: Path):
    """Kernel-owned advisory lock; a crash/OOM cannot leave a stale lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("a+", encoding="ascii")
    except OSError as exc:
        raise Refused("cannot open RFQ supervisor lock %s" % lock_path) from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise Refused("another RFQ capture supervisor holds %s" % lock_path) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()) + "\n")
    handle.flush()
    return handle


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def run(args: argparse.Namespace) -> int:
    raw_root = Path(args.raw_root).resolve()
    live_root = Path(args.live_root).resolve()
    disable = Path(args.disable_flag).resolve()
    lock = Path(args.lock_dir).resolve()
    alert = Path(args.alert_path).resolve()
    state_path = Path(args.state_path).resolve()
    ledger_path = Path(args.ledger_path).resolve()
    metrics = Path(args.metrics_path).resolve()
    # Production is permanently pinned to the audited read-only harness. There
    # is intentionally no CLI/env override that could receive prod credentials.
    binary = DEFAULT_BINARY.resolve()
    fresh_authority_binding = None

    template = capture_template(raw_root)
    try:
        if getattr(args, "fresh_lane_authority", None):
            fresh_authority_binding = load_fresh_lane_authority(
                args.fresh_lane_authority,
                expected_owner_uid=args.fresh_lane_authority_owner_uid,
                expected_generation=args.fresh_lane_expected_generation)
        if args.child_lifetime_seconds < 3_600:
            raise Refused("persistent child lifetime must be at least one hour")
        if args.close_stability_seconds < 1.0:
            raise Refused("hour-close stability proof must be at least 1 second")
        if args.boundary_timeout_seconds < args.close_stability_seconds + 1.0:
            raise Refused("boundary timeout is too short for the stability proof")
        if not 5.0 <= args.subscription_grace_seconds <= 120.0:
            raise Refused("subscription ACK grace must be between 5 and 120 seconds")
        env = child_environment(os.environ, template, metrics,
                                args.child_lifetime_seconds,
                                validate_credentials=not args.dry_run)
        if not args.dry_run and not binary.is_file():
            raise Refused("collector binary missing: %s" % binary)
    except Refused as exc:
        if not args.dry_run:
            _alert(alert, str(exc))
        print("rfq_capture REFUSED: %s" % exc, file=sys.stderr)
        return 78

    if args.dry_run:
        print("RFQ CAPTURE DRY RUN — no socket opened")
        for key in (
            "KALSHI_ENV", "KALSHI_ALLOW_PROD", "KALSHI_API_KEY_SCOPE", "KALSHI_MODE",
            "KALSHI_BASE_URL", "KALSHI_WS_URL", "KALSHI_WS_SIGN_PATH",
            "KALSHI_WS_FIREHOSE", "KALSHI_WS_CHANNELS",
            "KALSHI_WS_OMIT_USE_YES_PRICE",
            "KALSHI_SHADOW_SECONDS", "KALSHI_SHADOW_CAPTURE",
            "KALSHI_SHADOW_METRICS", "KALSHI_SHADOW_XCHECK",
        ):
            print("%s=%s" % (key, env.get(key, "")))
        print("KALSHI_WS_TICKERS=<absent>")
        print("binary=%s" % binary)
        print("fresh_lane_state=%s" % (
            "BOUND_AUTHORITY" if fresh_authority_binding is not None else
            "UNBOUND_DIAGNOSTIC"))
        if fresh_authority_binding is not None:
            print("fresh_lane_authority=%s" % fresh_authority_binding["path"])
            print("authority_sha256=%s" %
                  fresh_authority_binding["authority"]["authority_sha256"])
        return 0

    try:
        lock_handle = _acquire_lock(lock)
    except Refused as exc:
        _alert(alert, str(exc))
        print("rfq_capture REFUSED: %s" % exc, file=sys.stderr)
        return 78

    stopping = False
    supervisor_pid = os.getpid()
    child_generation_counter = 0
    fresh_authority_compromised = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        while not stopping:
            if disable.exists():
                _write_state(state_path, "DISABLED", disable_flag=str(disable))
                print("[rfq_capture] DISABLED by %s" % disable, flush=True)
                for _ in range(max(1, int(args.disabled_poll_seconds * 2))):
                    if stopping or not disable.exists():
                        break
                    time.sleep(0.5)
                continue

            launch_authority_valid, launch_authority_findings = \
                fresh_lane_finalize_guard(fresh_authority_binding)
            if not launch_authority_valid:
                _alert(alert, "; ".join(launch_authority_findings))
                print("[rfq_capture] REFUSED: %s" %
                      "; ".join(launch_authority_findings),
                      file=sys.stderr, flush=True)
                return 78
            now = utc_now()
            cap, _ = segment(now, raw_root)
            raw_root.mkdir(parents=True, exist_ok=True)
            live_root.mkdir(parents=True, exist_ok=True)
            try:
                env = child_environment(
                    os.environ, template, metrics, args.child_lifetime_seconds)
            except Refused as exc:
                _alert(alert, str(exc))
                print("[rfq_capture] REFUSED: %s" % exc, file=sys.stderr, flush=True)
                return 78

            print("[rfq_capture] start persistent communications capture=%s lifetime=%ds"
                  % (template, args.child_lifetime_seconds), flush=True)
            # Same bounded metrics policy as the firehose, but a distinct file.
            # Rotation happens between children, never under an active writer.
            subprocess.run(
                ["bash", str(ROOT / "tools" / "rotate_metrics.sh"), str(metrics)],
                cwd=str(ROOT), env=env, check=False,
            )
            child_metrics_offset = _file_size(metrics)
            segment_metrics_offset = child_metrics_offset
            health_metrics_offset = child_metrics_offset
            launch_now = utc_now()
            segment_start = _hour_start(launch_now)
            deadline = segment_start + dt.timedelta(hours=1)
            segment_hour = segment_start.strftime("%Y-%m-%dT%H")
            cap, _ = segment(launch_now, raw_root)
            # Snapshot before Popen: the child can write hour_open + subscribe
            # ACK immediately, and those bytes must belong to this segment's
            # evidence rather than being skipped by a post-launch stat race.
            capture_before_snapshot = hour_shard_snapshot(cap)
            capture_offsets_before = _snapshot_offsets(capture_before_snapshot)
            capture_cursor = dict(capture_offsets_before)
            segment_raw_evidence = _empty_capture_evidence()
            child_started_wall_ns = time.time_ns()
            child_generation_counter += 1
            active_child_generation = child_generation_counter
            child = subprocess.Popen([str(binary)], cwd=str(ROOT), env=env)
            child_subscription_ack_wall_ns = None
            child_subscription_ack_identity_sha256 = None
            child_subscription_ack_count = 0
            segment_started_wall_ns = child_started_wall_ns
            segment_reconnects_start = 0
            segment_disconnects_start = 0
            segment_errors_start = 0
            subscription_proven = False
            runtime_findings: list[str] = list(
                capture_before_snapshot.get("findings", []))
            next_live_check = time.monotonic() + 10.0
            last_connected_valid_seen = time.monotonic()
            health_disconnects_seen = 0
            health_errors_seen = 0
            current_active = True
            exit_reason: str | None = None
            fatal_writer = False
            _write_state(state_path, "RUNNING_UNPROVEN", segment_hour=segment_hour,
                         capture_template=str(template), child_pid=child.pid,
                         child_started_wall_ns=child_started_wall_ns)

            def absorb_child_subscription_ack(evidence: dict) -> None:
                nonlocal child_subscription_ack_wall_ns
                nonlocal child_subscription_ack_identity_sha256
                nonlocal child_subscription_ack_count
                observed = int(evidence.get("subscribed_communications", 0))
                if observed <= 0:
                    return
                child_subscription_ack_count += observed
                if child_subscription_ack_wall_ns is None:
                    child_subscription_ack_wall_ns = evidence.get(
                        "subscription_ack_wall_ns")
                    child_subscription_ack_identity_sha256 = evidence.get(
                        "subscription_ack_identity_sha256")
                if child_subscription_ack_count != 1:
                    runtime_findings.append(
                        "persistent child has multiple communications ACKs")

            def finalize_current(reason: str, *, boundary_closed: bool,
                                 close_stability_ms: int,
                                 child_rc: int | None) -> tuple[str, int, int, int, int, bool]:
                nonlocal subscription_proven, capture_cursor, segment_raw_evidence
                nonlocal fresh_authority_compromised
                authority_valid, authority_findings = \
                    fresh_lane_finalize_guard(fresh_authority_binding)
                if not authority_valid:
                    fresh_authority_compromised = True
                expected_start_ns = int(segment_start.timestamp() * 1e9)
                expected_end_ns = int(deadline.timestamp() * 1e9)
                lo_ms, hi_ms = expected_start_ns // 1_000_000, expected_end_ns // 1_000_000
                raw_chunk = capture_evidence_since_shards(
                    cap, capture_cursor, segment_hour, require_base=True)
                capture_cursor = dict(raw_chunk.get("end_offsets", capture_cursor))
                absorb_child_subscription_ack(raw_chunk)
                # Finalization builds a receipt snapshot without mutating the
                # live accumulated evidence object retained by the health loop.
                raw_evidence = _empty_capture_evidence()
                _merge_capture_evidence(raw_evidence, segment_raw_evidence)
                _merge_capture_evidence(raw_evidence, raw_chunk)
                metric_evidence = metrics_evidence_since(
                    metrics, segment_metrics_offset, start_ms=lo_ms, end_ms=hi_ms)
                if raw_evidence["subscription_proven_at_end"] is not None:
                    subscription_proven = bool(
                        raw_evidence["subscription_proven_at_end"])
                reconnects_end = metric_evidence["max_reconnects"]
                disconnects_end = metric_evidence["max_disconnects"]
                errors_end = metric_evidence["max_errors"]
                reconnect_changed = reconnects_end > segment_reconnects_start
                disconnect_changed = disconnects_end > segment_disconnects_start
                errors_changed = errors_end > segment_errors_start
                connection_tainted = (
                    reconnect_changed or disconnect_changed or errors_changed or
                    raw_evidence["subscription_invalidations"] > 0)
                if connection_tainted:
                    # The old subscription is no longer proof for the next
                    # interval. A new authenticated ACK can re-establish the
                    # connection state, but this hour remains unprovable
                    # because RFQ broadcasts expose no replay/sequence gap.
                    subscription_proven = bool(
                        raw_evidence["subscription_proven_at_end"])

                findings = list(runtime_findings)
                findings.extend(authority_findings)
                findings.extend(raw_evidence["findings"])
                findings.extend(metric_evidence["findings"])
                if raw_evidence["markers"].get("hour_open", 0) == 0:
                    findings.append("missing durable hour_open marker")
                if any(int(value) != 0
                       for value in capture_offsets_before.values()):
                    findings.append(
                        "pre-existing RFQ bytes at segment start (multiple capture sessions)")
                if not subscription_proven:
                    findings.append("no authenticated communications subscription proof")
                if reason == "boundary" and not boundary_closed:
                    findings.append("hour-close writer barrier/stability not proven")
                if metric_evidence["feed_rows"] and (
                        metric_evidence["connected_valid_rows"] * 10 <
                        metric_evidence["feed_rows"] * 8):
                    findings.append("connected+valid heartbeat coverage below 80%")
                if reason == "boundary":
                    first = metric_evidence["first_ts_ms"]
                    last = metric_evidence["last_ts_ms"]
                    if first is None or first > lo_ms + 5_000:
                        findings.append("feed heartbeat does not cover hour start")
                    if last is None or last < hi_ms - 5_000:
                        findings.append("feed heartbeat does not cover hour end")
                start_lag_ms = max(
                    0, (segment_started_wall_ns - expected_start_ns) // 1_000_000)
                ended_wall_ns = time.time_ns()
                end_early_ms = max(
                    0, (expected_end_ns - ended_wall_ns) // 1_000_000)
                if start_lag_ms > 5_000:
                    findings.append(
                        "partial hour: capture started %dms after boundary" % start_lag_ms)
                if reason != "boundary" and end_early_ms > 2_000:
                    findings.append(
                        "partial hour: capture ended %dms before boundary" % end_early_ms)
                if connection_tainted:
                    findings.append(
                        "EVENT_COMPLETENESS_UNPROVEN: reconnect %d->%d, "
                        "disconnect %d->%d, errors %d->%d, raw invalidations=%d "
                        "(RFQ has no seq/replay proof)" %
                        (segment_reconnects_start, reconnects_end,
                         segment_disconnects_start, disconnects_end,
                         segment_errors_start, errors_end,
                         raw_evidence["subscription_invalidations"]))
                if child_rc not in (None, 0) and reason not in ("disable", "stop"):
                    findings.append("ws_shadow child rc=%d" % child_rc)
                attestation_snapshot_before = hour_shard_snapshot(
                    cap, require_base=True)
                capture_shards, attestation_findings = attest_hour_shards(
                    cap, raw_root, capture_offsets_before)
                findings.extend(attestation_findings)
                capture_shards, eof_findings = bind_complete_line_eof(
                    capture_shards, capture_cursor, raw_root)
                findings.extend(eof_findings)
                try:
                    capture_shard_set_sha = capture_shard_set_sha256(
                        capture_shards)
                except ValueError as exc:
                    capture_shard_set_sha = None
                    findings.append("RFQ shard-set identity invalid: %s" % exc)
                attestation_snapshot_after = hour_shard_snapshot(
                    cap, require_base=True)
                findings.extend(final_attestation_snapshot_findings(
                    attestation_snapshot_before, attestation_snapshot_after))
                final_authority_valid, final_authority_findings = \
                    fresh_lane_finalize_guard(fresh_authority_binding)
                authority_valid = authority_valid and final_authority_valid
                if not final_authority_valid:
                    fresh_authority_compromised = True
                findings.extend(final_authority_findings)
                findings = sorted(set(findings))

                expected_partial = (
                    reason == "boundary" and start_lag_ms > 5_000 and
                    all(x.startswith("partial hour: capture started") or
                        x == "feed heartbeat does not cover hour start"
                        for x in findings)
                )
                if not authority_valid:
                    status = "FAIL"
                elif reason == "disable":
                    status = "DISABLED_PARTIAL"
                elif reason == "stop":
                    status = "STOPPED_PARTIAL"
                elif expected_partial:
                    status = "PARTIAL_START"
                elif connection_tainted:
                    status = "EVENT_COMPLETENESS_UNPROVEN"
                elif findings:
                    status = "FAIL"
                else:
                    status = "PASS"

                capture_bytes_before = sum(
                    int(row.get("bytes_before", 0)) for row in capture_shards)
                capture_bytes = sum(int(row.get("size", 0)) for row in capture_shards)
                capture_sha = (capture_shards[0]["sha256"]
                               if len(capture_shards) == 1 else None)
                receipt = {
                    "type": "rfq_segment_receipt",
                    "schema": "rfq-segment-receipt-v3",
                    "segment_hour": segment_hour,
                    "expected_start_wall_ns": expected_start_ns,
                    "expected_end_wall_ns": expected_end_ns,
                    "child_started_wall_ns": child_started_wall_ns,
                    "segment_started_wall_ns": segment_started_wall_ns,
                    "receipt_observed_wall_ns": ended_wall_ns,
                    "start_lag_ms": start_lag_ms,
                    "end_early_ms": end_early_ms,
                    "end_reason": reason,
                    "boundary_closed": boundary_closed,
                    "close_stability_ms": close_stability_ms,
                    "child_rc": child_rc,
                    "status": status,
                    "subscription_proven": subscription_proven,
                    "capture_relpath": str(cap.relative_to(raw_root)),
                    "capture_origin_path": str(cap),
                    "capture_bytes_before": capture_bytes_before,
                    "capture_bytes_at_close": capture_bytes,
                    "capture_sha256_at_close": capture_sha,
                    "capture_shards": capture_shards,
                    "capture_shard_count": len(capture_shards),
                    "capture_shard_set_sha256": capture_shard_set_sha,
                    "raw_evidence": raw_evidence,
                    "metrics_evidence": metric_evidence,
                    "findings": findings,
                    "generated_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                receipt.update(fresh_lane_receipt_fields(
                    fresh_authority_binding, segment_hour,
                    supervisor_pid=supervisor_pid, child_pid=child.pid,
                    child_generation=active_child_generation,
                    child_subscription_ack_wall_ns=
                        child_subscription_ack_wall_ns,
                    child_subscription_ack_identity_sha256=
                        child_subscription_ack_identity_sha256,
                    child_subscription_ack_count=child_subscription_ack_count,
                    authority_valid=authority_valid))
                receipt_now = utc_now()
                receipt_raw = raw_root / (
                    "date=" + receipt_now.strftime("%Y-%m-%d")) / (
                    "rfq_receipts_" + receipt_now.strftime("%H") + ".ndjson")
                persist_segment_receipt(receipt_raw, ledger_path, receipt)
                _write_state(state_path, status, segment_hour=segment_hour,
                             child_pid=child.pid, receipt_status=status,
                             findings=findings)
                if status not in ("PASS", "PARTIAL_START", "DISABLED_PARTIAL",
                                     "STOPPED_PARTIAL"):
                    _alert(alert, "%s: %s" %
                           (status, "; ".join(findings) or status),
                           child_rc=child_rc)
                    print("[rfq_capture] %s: %s" %
                          (status, "; ".join(findings) or status),
                          file=sys.stderr, flush=True)
                return (status, reconnects_end, disconnects_end, errors_end,
                        int(metric_evidence["next_window_offset"]),
                        connection_tainted)

            while current_active:
                if stopping:
                    exit_reason = "stop"
                    _terminate_child(child)
                    break
                if disable.exists():
                    exit_reason = "disable"
                    _terminate_child(child)
                    break
                rc_now = child.poll()
                if rc_now is not None:
                    exit_reason = "child_exit"
                    break

                now = utc_now()
                if now >= deadline:
                    new_cap, _ = segment(deadline, raw_root)
                    new_hour = deadline.strftime("%Y-%m-%dT%H")
                    closed, stability_ms = wait_for_partition_close(
                        cap, new_cap, new_hour, child,
                        timeout_s=args.boundary_timeout_seconds,
                        stable_s=args.close_stability_seconds)
                    if child.poll() is not None:
                        finalize_current("child_exit", boundary_closed=False,
                                         close_stability_ms=stability_ms,
                                         child_rc=child.poll())
                        current_active = False
                        exit_reason = "child_exit"
                        break
                    (segment_status, segment_reconnects_start,
                     segment_disconnects_start, segment_errors_start,
                     segment_metrics_offset, connection_tainted) = finalize_current(
                        "boundary", boundary_closed=closed,
                        close_stability_ms=stability_ms, child_rc=None)
                    if fresh_authority_compromised:
                        exit_reason = "fresh_authority_invalid"
                        _terminate_child(child)
                        current_active = False
                        break
                    if connection_tainted or not subscription_proven:
                        # Reset the dedicated socket immediately. The closed
                        # hour is already ineligible; a new process gives the
                        # next candidate full hour fresh zero-based counters
                        # and a new authenticated ACK.
                        exit_reason = "connection_tainted"
                        _terminate_child(child)
                        current_active = False
                        break
                    segment_start = deadline
                    deadline = segment_start + dt.timedelta(hours=1)
                    segment_hour = segment_start.strftime("%Y-%m-%dT%H")
                    cap = new_cap
                    capture_offsets_before = {}
                    capture_cursor = {}
                    segment_raw_evidence = _empty_capture_evidence()
                    segment_started_wall_ns = int(segment_start.timestamp() * 1e9)
                    runtime_findings = []
                    _write_state(state_path, "RUNNING", segment_hour=segment_hour,
                                 capture=str(cap), child_pid=child.pid,
                                 subscription_proven=subscription_proven)
                    continue

                if time.monotonic() >= next_live_check:
                    recent = metrics_evidence_since(
                        metrics, health_metrics_offset)
                    health_metrics_offset = int(recent["end_offset"])
                    if recent["connected_valid_rows"]:
                        last_connected_valid_seen = time.monotonic()
                    immediate = [x for x in recent["findings"]
                                 if ("dropped=" in x or "buffer overflow" in x or
                                     "recorder_write_failures=" in x)]
                    runtime_findings.extend(immediate)
                    if any("recorder_write_failures=" in x for x in immediate):
                        fatal_writer = True
                        exit_reason = "writer_failure"
                        _terminate_child(child)
                        break
                    if (recent["max_disconnects"] > health_disconnects_seen or
                            recent["max_errors"] > health_errors_seen):
                        runtime_findings.append(
                            "transport disconnect/error observed in live health sample")
                        exit_reason = "transport_failure"
                        _terminate_child(child)
                        break
                    health_disconnects_seen = recent["max_disconnects"]
                    health_errors_seen = recent["max_errors"]
                    child_age_s = (time.time_ns() - child_started_wall_ns) / 1e9
                    raw_health = capture_evidence_since_shards(
                        cap, capture_cursor, segment_hour, require_base=True)
                    capture_cursor = dict(
                        raw_health.get("end_offsets", capture_cursor))
                    absorb_child_subscription_ack(raw_health)
                    _merge_capture_evidence(segment_raw_evidence, raw_health)
                    if raw_health["subscription_proven_at_end"] is not None:
                        was_proven = subscription_proven
                        subscription_proven = bool(
                            raw_health["subscription_proven_at_end"])
                        if subscription_proven and not was_proven:
                            _write_state(
                                state_path, "RUNNING", segment_hour=segment_hour,
                                capture=str(cap), child_pid=child.pid,
                                subscription_proven=True)
                    if raw_health["subscription_invalidations"]:
                        runtime_findings.extend(raw_health["findings"])
                        exit_reason = "subscription_invalidated"
                        _terminate_child(child)
                        break
                    if subscription_startup_timed_out(
                            child_age_s, subscription_proven,
                            args.subscription_grace_seconds):
                        runtime_findings.append(
                            "authenticated communications subscribe ACK missing "
                            "after %.0fs" % args.subscription_grace_seconds)
                        exit_reason = "subscription_timeout"
                        _terminate_child(child)
                        break
                    if any("recorder_dropped=" in x or "buffer overflow" in x
                           for x in immediate):
                        # Recorder counters are cumulative for a process. The
                        # affected hour is already unprovable; restart this
                        # isolated child so later full hours can become clean.
                        exit_reason = "capture_loss"
                        _terminate_child(child)
                        break
                    if (child_age_s > 45 and
                            time.monotonic() - last_connected_valid_seen > 30):
                        runtime_findings.append(
                            "no connected+valid heartbeat in trailing 30s")
                        exit_reason = "health_failure"
                        _terminate_child(child)
                        break
                    next_live_check = time.monotonic() + 10.0
                time.sleep(args.child_poll_seconds)

            rc = child.wait()
            if current_active:
                reason = exit_reason or "child_exit"
                finalize_current(reason, boundary_closed=True,
                                 close_stability_ms=0, child_rc=rc)
            if fresh_authority_compromised:
                return 78
            if stopping:
                break
            if disable.exists():
                _write_state(state_path, "DISABLED", disable_flag=str(disable))
                print("[rfq_capture] child stopped by disable flag", flush=True)
                continue
            if fatal_writer:
                return 1  # systemd backoff/restart; never claim a healthy sink
            time.sleep(args.retry_seconds)
    finally:
        _release_lock(lock_handle)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="print exact child env; no socket")
    p.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    p.add_argument("--live-root", default=str(DEFAULT_LIVE_ROOT))
    p.add_argument("--disable-flag", default=str(DEFAULT_DISABLE))
    p.add_argument("--lock-dir", default=str(DEFAULT_LOCK))
    p.add_argument("--alert-path", default=str(DEFAULT_ALERT))
    p.add_argument("--state-path", default=str(DEFAULT_STATE))
    p.add_argument("--ledger-path", default=str(DEFAULT_LEDGER))
    p.add_argument("--metrics-path", default=str(DEFAULT_METRICS))
    p.add_argument(
        "--fresh-lane-authority",
        help="read-only local fresh RFQ epoch authority; omitted is diagnostic only")
    p.add_argument(
        "--fresh-lane-authority-owner-uid", type=int, default=0,
        help="required owner UID for the protected authority file and parent")
    p.add_argument(
        "--fresh-lane-expected-generation",
        help="generation ID that must exactly match the authority envelope")
    p.add_argument("--child-poll-seconds", type=float, default=0.1)
    p.add_argument("--disabled-poll-seconds", type=float, default=5.0)
    p.add_argument("--retry-seconds", type=float, default=15.0)
    p.add_argument("--child-lifetime-seconds", type=int,
                   default=DEFAULT_CHILD_LIFETIME_SECONDS,
                   help="persistent child lifetime; default 30 days")
    p.add_argument("--boundary-timeout-seconds", type=float, default=10.0)
    p.add_argument("--close-stability-seconds", type=float, default=2.0)
    p.add_argument("--subscription-grace-seconds", type=float, default=30.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
