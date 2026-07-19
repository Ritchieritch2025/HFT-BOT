#!/usr/bin/env python3
"""Produce the strict fresh-RFQ daily package from capture/S3 evidence.

The runner is an independent, read-only S3 consumer.  It never tags, uploads,
publishes, starts W09, or mutates the base L1/L2/orderbook release.  It resolves
the exact current S3 VersionId for the full_v2 seal and every RFQ capture and
receipt container, streams those exact versions through the source-evidence
validator, rebuilds all 26 hour receipts from the persistent session ledger,
observes the mandatory capture-health input, then atomically commits
``ELIGIBLE.json`` locally.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fresh_rfq_daily_eligibility as gate  # noqa: E402
import fresh_rfq_close_inventory as close_inventory  # noqa: E402
import fresh_rfq_receipts as fresh  # noqa: E402
import fresh_rfq_source_evidence as source  # noqa: E402


BUCKET = "kalshi-vault-ritcardo"
AUTHORITY = pathlib.Path(
    "/var/lib/kalshi-rfq-fresh/control/fresh-rfq-20260720-01/"
    "authority-envelope.json")
RAW_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/raw")
SEAL_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/warehouse/seals")
SESSION_LEDGER = pathlib.Path("/home/ubuntu/hft-bot/work/live/rfq_segments.ndjson")
ALERT_PATH = pathlib.Path(gate.PRODUCTION_ALERT_PATH)
OUTPUT_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/live/fresh_rfq_research")
SCRATCH_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/live/fresh_rfq_producer")
AWS_CLI = pathlib.Path("/snap/aws-cli/current/bin/aws")
CAPTURE_RE = re.compile(
    r"^date=(\d{4}-\d{2}-\d{2})/rfq_([01]\d|2[0-3])\.ndjson"
    r"(?:\.([1-9][0-9]*))?$")
CONTAINER_RE = re.compile(
    r"^date=(\d{4}-\d{2}-\d{2})/rfq_receipts_"
    r"([01]\d|2[0-3])\.ndjson(?:\.([1-9][0-9]*))?$")
MAX_LEDGER_LINE_BYTES = 4 << 20


class RunnerError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise RunnerError(code, detail)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False).encode("utf-8")


def _read_regular(path: pathlib.Path, limit: int) -> bytes:
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("LOCAL_INPUT_INVALID", f"{path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            _fail("LOCAL_INPUT_INVALID", str(path))
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(fd, min(1 << 20, limit + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or len(raw) > limit
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            _fail("LOCAL_INPUT_CHANGED", str(path))
        return bytes(raw)
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> Any:
    seen_error = None

    def pairs(rows):
        nonlocal seen_error
        out = {}
        for key, value in rows:
            if key in out:
                seen_error = f"duplicate key {key!r}"
            out[key] = value
        return out

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               ValueError(f"non-finite {value}")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunnerError("JSON_INVALID", f"{label}: {exc}") from exc
    if seen_error:
        _fail("JSON_INVALID", f"{label}: {seen_error}")
    return value


class AwsReadOnly:
    """Small allowlisted AWS CLI transport; there is no write operation."""

    def __init__(self, executable: pathlib.Path = AWS_CLI):
        executable = pathlib.Path(executable).absolute()
        if executable != AWS_CLI:
            _fail("AWS_BINARY_INVALID", str(executable))
        self.executable = str(executable)

    def _run(self, args: list[str]) -> dict[str, Any]:
        if args[:2] not in (["s3api", "head-object"],
                            ["s3api", "get-object"],
                            ["s3api", "list-object-versions"]):
            _fail("AWS_OPERATION_FORBIDDEN", repr(args[:2]))
        env = dict(os.environ)
        env["AWS_PAGER"] = ""
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                timeout=3600, env=env, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("AWS_READ_FAILED", str(exc)) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            _fail("AWS_READ_FAILED", detail or f"rc={result.returncode}")
        try:
            return json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RunnerError("AWS_RESPONSE_INVALID", str(exc)) from exc

    def head(self, key: str, version_id: str | None = None) -> dict[str, Any]:
        args = ["s3api", "head-object", "--bucket", BUCKET, "--key", key]
        if version_id is not None:
            args += ["--version-id", version_id]
        args += ["--output", "json"]
        return self._run(args)

    def get(self, identity: dict[str, Any], destination: pathlib.Path) -> None:
        self._run([
            "s3api", "get-object", "--bucket", identity["bucket"],
            "--key", identity["key"], "--version-id", identity["version_id"],
            str(destination), "--output", "json",
        ])

    def list_version_pages(self, prefix: str) -> list[dict[str, Any]]:
        pages = []
        key_marker = None
        version_marker = None
        seen = set()
        while True:
            marker = (key_marker, version_marker)
            if marker in seen or len(pages) >= 10000:
                _fail("VERSION_INVENTORY_PAGINATION_LOOP", prefix)
            seen.add(marker)
            args = [
                "s3api", "list-object-versions", "--bucket", BUCKET,
                "--prefix", prefix, "--no-paginate", "--output", "json",
            ]
            if key_marker is not None:
                args += ["--key-marker", key_marker]
            if version_marker is not None:
                args += ["--version-id-marker", version_marker]
            result = self._run(args)
            pages.append(result)
            if result.get("IsTruncated") is not True:
                return pages
            key_marker = result.get("NextKeyMarker")
            version_marker = result.get("NextVersionIdMarker")
            if not isinstance(key_marker, str) or not key_marker:
                _fail("VERSION_INVENTORY_INCOMPLETE", prefix)


class _Opened:
    def __init__(self, path: pathlib.Path):
        self.path = path


class ExactS3Reader:
    def __init__(self, transport: AwsReadOnly, scratch_root: pathlib.Path,
                 *, persistent_cache: bool = True):
        self.transport = transport
        self.scratch_root = pathlib.Path(scratch_root).absolute()
        self.persistent_cache = persistent_cache

    def _cache_path(self, identity: dict[str, Any]) -> pathlib.Path:
        digest = hashlib.sha256(_canonical({
            key: identity[key]
            for key in ("bucket", "key", "version_id", "size", "sha256")
        })).hexdigest()
        return self.scratch_root / "exact-object-cache" / digest

    @staticmethod
    def _verify_cache(path: pathlib.Path, identity: dict[str, Any]) -> None:
        size, sha = _hash_regular(path)
        if size != identity["size"] or sha != identity["sha256"]:
            _fail("EXACT_CACHE_IDENTITY_MISMATCH", identity["key"])

    @contextlib.contextmanager
    def open_exact(self, identity: dict[str, Any]) -> Iterator[_Opened]:
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.persistent_cache:
            cache = self._cache_path(identity)
            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if cache.exists():
                self._verify_cache(cache, identity)
                yield _Opened(cache)
                return
            with tempfile.TemporaryDirectory(
                    prefix="exact-pending-", dir=cache.parent) as directory:
                path = pathlib.Path(directory) / "object"
                self.transport.get(identity, path)
                self._verify_cache(path, identity)
                os.chmod(path, 0o600)
                try:
                    os.rename(path, cache)
                except FileExistsError:
                    self._verify_cache(cache, identity)
                parent_fd = os.open(cache.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            yield _Opened(cache)
            return
        with tempfile.TemporaryDirectory(
                prefix="exact-", dir=self.scratch_root) as directory:
            path = pathlib.Path(directory) / "object"
            self.transport.get(identity, path)
            yield _Opened(path)


def _hash_regular(path: pathlib.Path) -> tuple[int, str]:
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("LOCAL_INPUT_INVALID", f"{path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            _fail("LOCAL_INPUT_INVALID", str(path))
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (total != before.st_size
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            _fail("LOCAL_INPUT_CHANGED", str(path))
        return total, digest.hexdigest()
    finally:
        os.close(fd)


def _resolve_local(path: pathlib.Path, key: str, transport: AwsReadOnly,
                   *, version_id: str | None = None) -> dict[str, Any]:
    size, sha = _hash_regular(path)
    head = transport.head(key, version_id)
    resolved_version = version_id or head.get("VersionId")
    if (not isinstance(resolved_version, str) or not resolved_version
            or resolved_version.lower() == "null"):
        _fail("EXACT_VERSION_REQUIRED", key)
    if head.get("ContentLength") != size:
        _fail("S3_LOCAL_SIZE_MISMATCH", key)
    return {
        "bucket": BUCKET, "key": key, "version_id": resolved_version,
        "size": size, "sha256": sha,
    }


def _expected_hours(date: str) -> list[str]:
    start = dt.datetime.strptime(date, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc)
    return [(start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
            for index in range(26)]


def _latest_family_inventory(
        transport: AwsReadOnly, prefix: str, pattern: re.Pattern[str]
        ) -> tuple[dict[str, str], list[dict[str, Any]]]:
    raw_pages = transport.list_version_pages(prefix)
    projected = []
    latest = {}
    for index, result in enumerate(raw_pages):
        request_key = None if index == 0 else raw_pages[index - 1].get(
            "NextKeyMarker")
        request_version = None if index == 0 else raw_pages[index - 1].get(
            "NextVersionIdMarker")
        versions = [{
            "key": row.get("Key"), "version_id": row.get("VersionId"),
            "is_latest": row.get("IsLatest"), "size": row.get("Size"),
        } for row in result.get("Versions", [])]
        deletes = [{
            "key": row.get("Key"), "version_id": row.get("VersionId"),
            "is_latest": row.get("IsLatest"),
        } for row in result.get("DeleteMarkers", [])]
        projected.append({
            "request_key_marker": request_key,
            "request_version_id_marker": request_version,
            "response_key_marker": result.get("KeyMarker") or None,
            "response_version_id_marker": result.get("VersionIdMarker") or None,
            "is_truncated": result.get("IsTruncated") is True,
            "next_key_marker": result.get("NextKeyMarker") or None,
            "next_version_id_marker": result.get("NextVersionIdMarker") or None,
            "versions": versions, "delete_markers": deletes,
        })
        for row in versions:
            key = row["key"]
            if (row["is_latest"] is not True or not isinstance(key, str)
                    or pattern.fullmatch(key.removeprefix("ec2/raw/")) is None):
                continue
            version = row["version_id"]
            if (not isinstance(version, str) or not version
                    or version.lower() == "null"):
                _fail("EXACT_VERSION_REQUIRED", key)
            if key in latest:
                _fail("VERSION_INVENTORY_AMBIGUOUS", key)
            latest[key] = version
    return latest, projected


def _select_segment_lines(lines: Iterator[tuple[int, bytes]], *,
                          expected_hours: list[str],
                          authority: dict[str, Any]) -> list[dict[str, Any]]:
    by_hour = {}
    for number, physical in lines:
        if not physical.strip():
            continue
        row = _json(physical, f"session ledger line {number}")
        if (not isinstance(row, dict)
                or row.get("schema") != "rfq-segment-receipt-v3"
                or row.get("type") != "rfq_segment_receipt"
                or row.get("authority_sha256") != authority["authority_sha256"]
                or row.get("generation") != authority["generation"]
                or row.get("segment_hour") not in expected_hours):
            continue
        hour = row["segment_hour"]
        if hour in by_hour:
            _fail("SESSION_LEDGER_DUPLICATE", hour)
        by_hour[hour] = fresh.validate_v3_segment_receipt(row, authority)
    if set(by_hour) != set(expected_hours):
        _fail("SESSION_LEDGER_INCOMPLETE", repr(sorted(
            set(expected_hours) - set(by_hour))))
    return [by_hour[hour] for hour in expected_hours]


def _select_segments(raw: bytes, *, expected_hours: list[str],
                     authority: dict[str, Any]) -> list[dict[str, Any]]:
    return _select_segment_lines(
        iter(enumerate(raw.splitlines(), 1)),
        expected_hours=expected_hours, authority=authority)


def _select_segments_path(path: pathlib.Path, *, expected_hours: list[str],
                          authority: dict[str, Any]) -> list[dict[str, Any]]:
    """Stream a growing ledger and retain only this date's 26 small rows.

    There is deliberately no whole-ledger byte ceiling: a healthy append-only
    production ledger must not become unreadable merely because it outlives a
    64 MiB process limit.  Every individual receipt line remains hard bounded,
    and a concurrent append/change fails closed so the next timer retry can
    take a stable snapshot.
    """
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("LOCAL_INPUT_INVALID", f"{path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            _fail("LOCAL_INPUT_INVALID", str(path))

        def lines() -> Iterator[tuple[int, bytes]]:
            with os.fdopen(os.dup(fd), "rb", buffering=0) as handle:
                number = 0
                while True:
                    physical = handle.readline(MAX_LEDGER_LINE_BYTES + 1)
                    if not physical:
                        break
                    number += 1
                    if len(physical) > MAX_LEDGER_LINE_BYTES:
                        _fail("SESSION_LEDGER_LINE_TOO_LARGE", str(number))
                    if not physical.endswith(b"\n"):
                        _fail("SESSION_LEDGER_PARTIAL_LINE", str(number))
                    yield number, physical[:-1]

        selected = _select_segment_lines(
            lines(), expected_hours=expected_hours, authority=authority)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        signature = lambda row: (
            row.st_dev, row.st_ino, row.st_size,
            row.st_mtime_ns, row.st_ctime_ns)
        if (signature(before) != signature(after)
                or stat.S_ISLNK(named.st_mode)
                or (named.st_dev, named.st_ino) !=
                (after.st_dev, after.st_ino)):
            _fail("LOCAL_INPUT_CHANGED", str(path))
        return selected
    finally:
        os.close(fd)


def _require_single_session(segments: list[dict[str, Any]]) -> None:
    fields = (
        "supervisor_pid", "child_pid", "child_generation",
        "child_subscription_ack_wall_ns",
        "child_subscription_ack_identity_sha256",
    )
    sessions = {tuple(row[field] for field in fields) for row in segments}
    if len(sessions) != 1:
        _fail("MULTIPLE_CAPTURE_SESSIONS", "24+2 ledger rows mix sessions")


def build_hour_set(*, date: str, authority: dict[str, Any],
                   source_evidence: dict[str, Any],
                   segments: list[dict[str, Any]]) -> dict[str, Any]:
    captures = (source_evidence["analysis_capture_objects"]
                + source_evidence["watermark_capture_objects"])
    receipts = []
    for segment in segments:
        hour = segment["segment_hour"]
        exact = [{
            "key": row["key"], "version_id": row["version_id"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in captures if row["source_hour"] == hour]
        receipts.append(fresh.build_hour_receipt(
            authority=authority, segment_receipt=segment,
            exact_inventory=exact, raw_key_prefix=authority["raw_key_prefix"],
            source_evidence=source_evidence,
            resolver=lambda row: copy.deepcopy(row)))
    return {"schema": gate.HOUR_SET_SCHEMA, "date": date,
            "receipts": receipts}


def _write_private(path: pathlib.Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail("LOCAL_WRITE_FAILED", str(path))
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_local_regular(path: pathlib.Path, label: str) -> None:
    try:
        observed = pathlib.Path(path).lstat()
    except OSError as exc:
        raise RunnerError("LOCAL_INPUT_INVALID", f"{label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode) \
            or observed.st_size <= 0:
        _fail("LOCAL_INPUT_INVALID", label)


def produce(date: str, *, transport: AwsReadOnly,
            authority_path: pathlib.Path = AUTHORITY,
            raw_root: pathlib.Path = RAW_ROOT,
            seal_root: pathlib.Path = SEAL_ROOT,
            session_ledger: pathlib.Path = SESSION_LEDGER,
            alert_path: pathlib.Path = ALERT_PATH,
            output_root: pathlib.Path = OUTPUT_ROOT,
            scratch_root: pathlib.Path = SCRATCH_ROOT,
            generated_at_utc: str | None = None) -> pathlib.Path:
    """Build one date package.  Existing immutable packages are revalidated."""
    existing = pathlib.Path(output_root) / f"date={date}" / gate.ELIGIBLE_NAME
    if existing.exists():
        gate.load_package(existing, expected_date=date)
        return existing
    try:
        day_start = dt.datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise RunnerError("DATE_INVALID", str(exc)) from exc
    generated_at_utc = generated_at_utc or dt.datetime.now(
        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        generated_time = dt.datetime.strptime(
            generated_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise RunnerError("GENERATED_TIME_INVALID", str(exc)) from exc
    final_close = day_start + dt.timedelta(days=1, hours=2)
    if generated_time < final_close:
        _fail("WATERMARK_NOT_CLOSED", generated_at_utc)

    authority_raw = _read_regular(authority_path, gate.MAX_JSON_BYTES)
    if hashlib.sha256(authority_raw).hexdigest() != \
            gate.PRODUCTION_ENVELOPE_FILE_SHA256:
        _fail("AUTHORITY_FILE_MISMATCH", str(authority_path))
    envelope = _json(authority_raw, "authority envelope")
    validated = gate.precommit.validate_precommit_envelope(envelope)
    authority = validated["authority"]
    gate._require_production_authority(validated, authority)
    expected_hours = _expected_hours(date)
    expected_close_hours = {
        (dt.datetime.strptime(hour, "%Y-%m-%dT%H").replace(
            tzinfo=dt.timezone.utc) + dt.timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H")
        for hour in expected_hours
    }

    # Mandatory cheap gates come before hashing any raw body or issuing an
    # exact-version GET.  The append-only session ledger is streamed, not read
    # into a process-sized buffer; only this date's canonical 26 rows remain.
    segments = _select_segments_path(
        session_ledger, expected_hours=expected_hours, authority=authority)
    _require_single_session(segments)
    selected_ledger_raw = b"".join(
        _canonical(row) + b"\n" for row in segments)
    health = gate.build_health_receipt(
        date=date, authority_envelope=envelope,
        session_ledger_sha256=hashlib.sha256(selected_ledger_raw).hexdigest(),
        session_ledger_row_count=len(segments),
        checked_at_utc=generated_at_utc, alert_path=alert_path)

    seal_path = pathlib.Path(seal_root) / f"date={date}.json"
    seal_body = _read_regular(seal_path, source.MAX_SEAL_BYTES)
    seal_value = _json(seal_body, "full_v2 seal")
    raw_files = seal_value.get("raw_files")
    if not isinstance(raw_files, list):
        _fail("SEAL_INVALID", "raw_files missing")
    capture_paths: list[tuple[pathlib.Path, str]] = []
    container_paths: list[tuple[pathlib.Path, str]] = []
    for row in raw_files:
        if not isinstance(row, dict) or not isinstance(row.get("file"), str):
            _fail("SEAL_INVALID", "raw file row invalid")
        relpath = row["file"]
        destination = pathlib.Path(raw_root) / relpath
        capture_match = CAPTURE_RE.fullmatch(relpath)
        container_match = CONTAINER_RE.fullmatch(relpath)
        if (capture_match is not None
                and f"{capture_match.group(1)}T{capture_match.group(2)}"
                in set(expected_hours)):
            _require_local_regular(destination, relpath)
            capture_paths.append((destination, "ec2/raw/" + relpath))
        elif (container_match is not None
              and f"{container_match.group(1)}T{container_match.group(2)}"
              in expected_close_hours):
            _require_local_regular(destination, relpath)
            container_paths.append((destination, "ec2/raw/" + relpath))

    capture_hours = {
        f"{match.group(1)}T{match.group(2)}"
        for _path, key in capture_paths
        if (match := CAPTURE_RE.fullmatch(key.removeprefix("ec2/raw/")))
    }
    if capture_hours != set(expected_hours):
        _fail(
            "LOCAL_24_PLUS_2_INCOMPLETE",
            repr(sorted(set(expected_hours) - capture_hours)),
        )

    final_rel_prefix = final_close.strftime("date=%Y-%m-%d/rfq_receipts_%H.ndjson")
    final_key_prefix = "ec2/raw/" + final_rel_prefix
    versions, inventory_pages = _latest_family_inventory(
        transport, final_key_prefix, CONTAINER_RE)
    local_final = sorted(
        path for path in pathlib.Path(raw_root, final_rel_prefix).parent.glob(
            pathlib.Path(final_rel_prefix).name + "*")
        if CONTAINER_RE.fullmatch(str(path.relative_to(raw_root))) is not None)
    local_keys = {"ec2/raw/" + str(path.relative_to(raw_root)): path
                  for path in local_final}
    if not local_keys or set(local_keys) != set(versions):
        _fail("FINAL_CONTAINER_INVENTORY_MISMATCH", final_key_prefix)

    # The complete cheap gate has now passed.  Only below this line may the
    # producer hash large local bodies or exact-GET source objects.
    seal_identity = _resolve_local(
        seal_path, f"ec2/warehouse/seals/date={date}.json", transport)
    captures = [
        _resolve_local(path, key, transport)
        for path, key in capture_paths
    ]
    containers = [
        _resolve_local(path, key, transport)
        for path, key in container_paths
    ]
    for key, path in sorted(local_keys.items()):
        containers.append(_resolve_local(
            path, key, transport, version_id=versions[key]))
    final_identities = sorted([
        row for row in containers if row["key"].startswith(final_key_prefix)
    ], key=lambda row: (row["key"], row["version_id"]))
    snapshot = close_inventory.build_close_inventory_snapshot(
        analysis_date=date, pages=inventory_pages,
        exact_identities=final_identities)
    inventory_observed_at = generated_at_utc
    close_receipt = {
        "schema_version": gate.CLOSE_ATTESTATION_SCHEMA,
        "state": gate.CLOSE_ATTESTATION_STATE,
        "analysis_date": date,
        "bucket": BUCKET,
        "prefix": final_key_prefix,
        "aws_cli": str(AWS_CLI),
        "list_request_count": len(inventory_pages),
        "observed_at_utc": inventory_observed_at,
        "snapshot": snapshot,
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "latest_exact_identities": final_identities,
    }
    close_receipt["attestation_sha256"] = gate.canonical_sha256(close_receipt)

    reader = ExactS3Reader(transport, scratch_root)
    evidence = source.build_source_evidence_from_reader(
        seal_object=seal_identity, capture_objects=captures,
        receipt_container_objects=containers,
        complete_container_identities=containers,
        expected_hours=expected_hours,
        authority_sha256=authority["authority_sha256"],
        generation=authority["generation"], open_exact=reader.open_exact)
    hour_set = build_hour_set(
        date=date, authority=authority, source_evidence=evidence,
        segments=segments)

    pathlib.Path(scratch_root).mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
            prefix=f"date={date}-", dir=scratch_root) as directory:
        root = pathlib.Path(directory)
        source_path = root / "source.json"
        hours_path = root / "hours.json"
        ledger_path = root / "ledger.ndjson"
        health_path = root / "health.json"
        close_path = root / "close-inventory.json"
        _write_private(source_path, _canonical(evidence) + b"\n")
        _write_private(hours_path, _canonical(hour_set) + b"\n")
        _write_private(ledger_path, selected_ledger_raw)
        _write_private(health_path, _canonical(health) + b"\n")
        _write_private(close_path, _canonical(close_receipt) + b"\n")
        return gate.create_package(
            date=date, authority_path=authority_path,
            source_path=source_path, hour_receipts_path=hours_path,
            session_ledger_path=ledger_path,
            health_receipt_path=health_path, output_root=output_root,
            close_inventory_path=close_path,
            generated_at_utc=generated_at_utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date")
    parser.add_argument("--scan-days", type=int, default=14)
    parser.add_argument("--aws-cli", type=pathlib.Path, default=AWS_CLI)
    args = parser.parse_args(argv)
    if args.scan_days < 1 or args.scan_days > 90:
        print("FRESH_RFQ_DAILY_PRODUCTS_BLOCKED scan-days outside 1..90",
              file=sys.stderr)
        return 2
    if args.date:
        dates = [args.date]
    else:
        yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
        first = dt.date.fromisoformat(gate.PRODUCTION_STRICT_T0_UTC[:10])
        dates = [
            (yesterday - dt.timedelta(days=offset)).isoformat()
            for offset in range(args.scan_days)
            if yesterday - dt.timedelta(days=offset) >= first
        ]
        dates.sort()
    ready = []
    blocked = []
    transport = AwsReadOnly(args.aws_cli)
    for date in dates:
        try:
            path = produce(date, transport=transport)
            marker = gate.load_package(path, expected_date=date)
            ready.append({
                "date": date, "eligible": str(path),
                "eligibility_sha256": marker["eligibility_sha256"],
                "eligible_objects": len(marker["eligible_objects"]),
            })
        except Exception as exc:
            blocked.append({
                "date": date, "code": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)[-500:],
            })
    result = {
        "state": ("FRESH_RFQ_DAILY_PRODUCTS_READY" if not blocked
                  else "FRESH_RFQ_DAILY_PRODUCTS_PARTIAL" if ready
                  else "FRESH_RFQ_DAILY_PRODUCTS_BLOCKED"),
        "dates_scanned": dates,
        "ready": ready,
        "blocked": blocked,
        "aws_writes": 0,
        "base_release_mutations": 0,
    }
    print(json.dumps(result, sort_keys=True),
          file=sys.stdout if not blocked else sys.stderr)
    return 0 if not blocked else 2


if __name__ == "__main__":
    raise SystemExit(main())
