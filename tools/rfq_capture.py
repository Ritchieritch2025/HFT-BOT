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
import signal
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


class Refused(RuntimeError):
    """A fail-closed startup refusal (configuration/safety, not a retry)."""


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
    evidence = {
        "recorder_rows": 0, "subscribed_communications": 0,
        "subscription_ack_wall_ns": None, "rfq_created": 0, "rfq_deleted": 0,
        "markers": {}, "partition_mismatches": 0, "max_stream_epoch": 0,
        "subscription_invalidations": 0, "subscription_proven_at_end": None,
        "findings": [],
    }
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            data = fh.read()
    except OSError:
        evidence["findings"] = ["RFQ raw file missing/unreadable after child run"]
        return evidence
    for raw_line in data.splitlines():
        try:
            outer = json.loads(raw_line)
            wall = outer.get("recv_wall_ns")
            if type(wall) is not int or wall <= 0:
                raise ValueError("bad wall clock")
        except (ValueError, TypeError):
            evidence["findings"].append("malformed recorder row")
            continue
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
            continue
        try:
            frame = json.loads(outer["raw"])
        except (ValueError, KeyError, TypeError):
            evidence["findings"].append("malformed inner WS frame")
            continue
        typ = frame.get("type")
        if typ == "subscribed":
            msg = frame.get("msg")
            if (isinstance(msg, dict) and msg.get("channel") == "communications" and
                    type(msg.get("sid")) is int and msg["sid"] > 0):
                evidence["subscribed_communications"] += 1
                evidence["subscription_proven_at_end"] = True
                if evidence["subscription_ack_wall_ns"] is None:
                    evidence["subscription_ack_wall_ns"] = wall
        elif typ in ("error", "unsubscribed"):
            evidence["subscription_invalidations"] += 1
            evidence["subscription_proven_at_end"] = False
            evidence["findings"].append(
                "unexpected communications control frame: %s" % typ)
        elif typ in ("rfq_created", "rfq_deleted"):
            evidence[typ] += 1
    if evidence["partition_mismatches"]:
        evidence["findings"].append(
            "recv-hour/path partition mismatch=%d" % evidence["partition_mismatches"])
    if evidence["markers"].get("loss", 0) or evidence["markers"].get("gap", 0):
        evidence["findings"].append("raw recorder loss/gap marker present")
    evidence["findings"] = sorted(set(evidence["findings"]))
    return evidence


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


def subscription_startup_timed_out(child_age_s: float, proven: bool,
                                   grace_s: float) -> bool:
    return child_age_s > grace_s and not proven


def wait_for_partition_close(old_path: Path, new_path: Path, new_hour: str,
                             child: subprocess.Popen, *, timeout_s: float,
                             stable_s: float) -> tuple[bool, int]:
    """Wait for writer rollover plus old-file size stability.

    ``hour_open`` is emitted by the single recorder writer after its current
    queue drain.  The extra stability interval catches a delayed pre-boundary
    row that is routed back by its receive timestamp.  If anything appends
    even later, the receipt SHA will no longer match the final day seal and the
    48-hour report still fails closed.
    """
    deadline = time.monotonic() + timeout_s
    saw_new_marker = False
    stable_since = None
    last_size = _file_size(old_path)
    while time.monotonic() < deadline and child.poll() is None:
        new_evidence = capture_evidence_since(new_path, 0, new_hour)
        if new_evidence["markers"].get("hour_open", 0):
            saw_new_marker = True
        size = _file_size(old_path)
        if saw_new_marker and size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return True, int((time.monotonic() - stable_since) * 1000)
        else:
            stable_since = None
        last_size = size
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

    template = capture_template(raw_root)
    try:
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
        return 0

    try:
        lock_handle = _acquire_lock(lock)
    except Refused as exc:
        _alert(alert, str(exc))
        print("rfq_capture REFUSED: %s" % exc, file=sys.stderr)
        return 78

    stopping = False

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
            capture_offset = _file_size(cap)
            child_started_wall_ns = time.time_ns()
            child = subprocess.Popen([str(binary)], cwd=str(ROOT), env=env)
            segment_started_wall_ns = child_started_wall_ns
            segment_reconnects_start = 0
            segment_disconnects_start = 0
            segment_errors_start = 0
            subscription_proven = False
            runtime_findings: list[str] = []
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

            def finalize_current(reason: str, *, boundary_closed: bool,
                                 close_stability_ms: int,
                                 child_rc: int | None) -> tuple[str, int, int, int, int, bool]:
                nonlocal subscription_proven
                expected_start_ns = int(segment_start.timestamp() * 1e9)
                expected_end_ns = int(deadline.timestamp() * 1e9)
                lo_ms, hi_ms = expected_start_ns // 1_000_000, expected_end_ns // 1_000_000
                raw_evidence = capture_evidence_since(cap, capture_offset, segment_hour)
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
                findings.extend(raw_evidence["findings"])
                findings.extend(metric_evidence["findings"])
                if raw_evidence["markers"].get("hour_open", 0) == 0:
                    findings.append("missing durable hour_open marker")
                if capture_offset != 0:
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
                findings = sorted(set(findings))

                expected_partial = (
                    reason == "boundary" and start_lag_ms > 5_000 and
                    all(x.startswith("partial hour: capture started") or
                        x == "feed heartbeat does not cover hour start"
                        for x in findings)
                )
                if reason == "disable":
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

                capture_bytes = _file_size(cap)
                capture_sha = None
                if cap.exists():
                    capture_sha = hashlib.sha256(cap.read_bytes()).hexdigest()
                receipt = {
                    "type": "rfq_segment_receipt",
                    "schema": "rfq-segment-receipt-v2",
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
                    "capture_bytes_before": capture_offset,
                    "capture_bytes_at_close": capture_bytes,
                    "capture_sha256_at_close": capture_sha,
                    "raw_evidence": raw_evidence,
                    "metrics_evidence": metric_evidence,
                    "findings": findings,
                    "generated_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
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
                    capture_offset = 0
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
                    raw_health = capture_evidence_since(
                        cap, capture_offset, segment_hour)
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
