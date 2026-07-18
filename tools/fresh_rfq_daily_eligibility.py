#!/usr/bin/env python3
"""Create and verify the local, fail-closed fresh-RFQ daily admission gate.

This module is deliberately local-only.  It performs no AWS, network, process,
service, tag, publication, or raw-data write.  A successful package proves that
one complete UTC analysis day (24 hours plus the two D+1 watermark hours) is
bound to the precommitted fresh-lane authority, one persistent capture session,
strict PASS hour receipts, exact source evidence, and one full_v2 day seal.

``ELIGIBLE.json`` is the commit marker.  It is written only by atomically
renaming a fully populated ``date=D`` directory.  Consumers revalidate the
four immutable inputs and rebuild the marker before enabling RFQ; a marker is
never trusted by filename or by a self-declared state alone.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fresh_rfq_receipts as fresh  # noqa: E402
import precommit_fresh_rfq_authority as precommit  # noqa: E402


SCHEMA = "fresh-rfq-daily-eligibility-v1"
STATE = "FRESH_RFQ_DAILY_ELIGIBLE"
HOUR_SET_SCHEMA = "fresh-rfq-hour-receipt-set-v1"
LANE_ID = fresh.LANE_ID
ELIGIBLE_NAME = "ELIGIBLE.json"
AUTHORITY_NAME = "AUTHORITY-ENVELOPE.json"
SOURCE_NAME = "SOURCE-EVIDENCE.json"
HOURS_NAME = "HOUR-RECEIPTS.json"
SESSION_NAME = "SESSION-LEDGER.ndjson"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXACT_FIELDS = ("bucket", "key", "version_id", "size", "sha256")
INPUT_NAMES = (AUTHORITY_NAME, SOURCE_NAME, HOURS_NAME, SESSION_NAME)


class EligibilityError(RuntimeError):
    """Stable local admission failure."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise EligibilityError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs_no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail("DUPLICATE_JSON_KEY", repr(key))
        out[key] = value
    return out


def _read_regular(path: pathlib.Path, limit: int, label: str) -> bytes:
    path = pathlib.Path(path).absolute()
    if not hasattr(os, "O_NOFOLLOW"):
        _fail("LOCAL_SAFETY_UNAVAILABLE", "O_NOFOLLOW is required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise EligibilityError("INPUT_INVALID", f"{label}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            _fail("INPUT_INVALID", f"{label} size/type is invalid")
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        fingerprint = lambda row: (row.st_dev, row.st_ino, row.st_size,
                                   row.st_mtime_ns, row.st_ctime_ns)
        if (total != before.st_size or total > limit
                or fingerprint(before) != fingerprint(after)
                or stat.S_ISLNK(named.st_mode)
                or (named.st_dev, named.st_ino) !=
                (after.st_dev, after.st_ino)):
            _fail("INPUT_CHANGED", label)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite {value}")),
        )
    except EligibilityError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise EligibilityError("JSON_INVALID", f"{label}: {exc}") from exc


def _read_json(path: pathlib.Path, label: str) -> tuple[Any, bytes]:
    raw = _read_regular(path, MAX_JSON_BYTES, label)
    return _decode_json(raw, label), raw


def _date(value: Any) -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("DATE_INVALID", repr(value))
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise EligibilityError("DATE_INVALID", str(exc)) from exc
    if parsed.isoformat() != value:
        _fail("DATE_INVALID", value)
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail("SHA256_INVALID", label)
    return value


def _exact_identity(row: Any, label: str) -> dict[str, Any]:
    if not isinstance(row, dict) or not set(EXACT_FIELDS).issubset(row):
        _fail("EXACT_IDENTITY_INVALID", label)
    out = {field: row[field] for field in EXACT_FIELDS}
    if (not isinstance(out["bucket"], str) or not out["bucket"]
            or out["bucket"] != fresh.SOURCE_BUCKET
            or not isinstance(out["key"], str)
            or not out["key"].startswith(fresh.RAW_KEY_PREFIX + "/")
            or not isinstance(out["version_id"], str)
            or not out["version_id"] or out["version_id"].lower() == "null"
            or type(out["size"]) is not int or out["size"] <= 0):
        _fail("EXACT_IDENTITY_INVALID", label)
    _sha(out["sha256"], f"{label}.sha256")
    return out


def _normalize_exact_rows(rows: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        _fail("EXACT_SET_INVALID", label)
    normalized = [_exact_identity(row, f"{label}[{index}]")
                  for index, row in enumerate(rows)]
    normalized.sort(key=lambda row: (row["key"], row["version_id"]))
    physical = [(row["bucket"], row["key"], row["version_id"])
                for row in normalized]
    if len(set(physical)) != len(physical):
        _fail("EXACT_SET_DUPLICATE", label)
    return normalized


def _expected_hours(date: str) -> list[str]:
    start = dt.datetime.combine(
        dt.date.fromisoformat(date), dt.time(), tzinfo=dt.timezone.utc)
    return [(start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
            for index in range(26)]


def _parse_ledger(raw: bytes) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        value = _decode_json(line, f"session ledger line {number}")
        if not isinstance(value, dict):
            _fail("SESSION_LEDGER_INVALID", f"line {number} is not an object")
        rows.append(value)
    if not rows:
        _fail("SESSION_LEDGER_INVALID", "ledger is empty")
    return rows


def _hour_set(value: Any, date: str) -> list[dict[str, Any]]:
    fields = {"schema", "date", "receipts"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("schema") != HOUR_SET_SCHEMA
            or value.get("date") != date
            or not isinstance(value.get("receipts"), list)):
        _fail("HOUR_SET_INVALID", "hour receipt set contract differs")
    return value["receipts"]


def _source_sets(source: dict[str, Any]) -> tuple[list[dict[str, Any]],
                                                   list[dict[str, Any]],
                                                   list[dict[str, Any]]]:
    analysis = _normalize_exact_rows(
        source.get("analysis_capture_objects"), "analysis captures")
    watermark = _normalize_exact_rows(
        source.get("watermark_capture_objects"), "watermark captures")
    containers = _normalize_exact_rows(
        source.get("receipt_containers"), "receipt containers")
    return analysis, watermark, containers


def build_eligibility(*, date: str, authority_envelope: Any,
                      source_evidence: Any, hour_receipt_set: Any,
                      session_ledger_rows: list[dict[str, Any]],
                      input_sha256: dict[str, str], generated_at_utc: str,
                      alert_absent: bool) -> dict[str, Any]:
    """Validate all local evidence and return one body-free admission marker."""
    date = _date(date)
    if not alert_absent:
        _fail("CAPTURE_ALERT_PRESENT", date)
    try:
        envelope = precommit.validate_precommit_envelope(authority_envelope)
        authority = envelope["authority"]
    except (precommit.PrecommitError, fresh.FreshRfqError) as exc:
        raise EligibilityError("AUTHORITY_INVALID", str(exc)) from exc
    expected_hours = _expected_hours(date)
    t0 = dt.datetime.strptime(
        authority["strict_t0_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    day_start = dt.datetime.strptime(date, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc)
    if day_start < t0:
        _fail("BEFORE_STRICT_T0", "analysis day begins before strict T0")
    if (not isinstance(source_evidence, dict)
            or source_evidence.get("analysis_date") != date
            or source_evidence.get("expected_hours") != expected_hours
            or source_evidence.get("authority_sha256")
            != authority["authority_sha256"]
            or source_evidence.get("generation") != authority["generation"]):
        _fail("SOURCE_EVIDENCE_INVALID", "date/window/authority differs")

    receipts = _hour_set(hour_receipt_set, date)
    if len(receipts) != 26:
        _fail("HOUR_SET_INVALID", "exactly 24+2 receipts are required")
    validated = []
    try:
        for receipt in receipts:
            validated.append(fresh.validate_hour_receipt(
                receipt, authority, source_evidence=source_evidence))
    except fresh.FreshRfqError as exc:
        raise EligibilityError("HOUR_RECEIPT_INVALID", str(exc)) from exc
    if [row["segment_hour"] for row in validated] != expected_hours:
        _fail("HOUR_SET_INVALID", "receipts are not exact ordered D24+D1/2")

    matching = {}
    for row in session_ledger_rows:
        if (row.get("type") != "rfq_segment_receipt"
                or row.get("schema") != "rfq-segment-receipt-v3"
                or row.get("authority_sha256") != authority["authority_sha256"]
                or row.get("generation") != authority["generation"]
                or row.get("segment_hour") not in expected_hours):
            continue
        hour = row["segment_hour"]
        if hour in matching:
            _fail("MULTIPLE_SESSION_ROWS", f"duplicate ledger hour {hour}")
        try:
            matching[hour] = fresh.validate_v3_segment_receipt(row, authority)
        except fresh.FreshRfqError as exc:
            raise EligibilityError("SESSION_LEDGER_INVALID", str(exc)) from exc
    if set(matching) != set(expected_hours):
        missing = sorted(set(expected_hours) - set(matching))
        _fail("SESSION_LEDGER_INCOMPLETE", repr(missing))
    ordered_segments = [matching[hour] for hour in expected_hours]
    embedded = [row["segment_receipt"] for row in validated]
    if canonical_bytes(ordered_segments) != canonical_bytes(embedded):
        _fail("SESSION_LEDGER_MISMATCH", "ledger and hour receipts differ")
    session_fields = (
        "supervisor_pid", "child_pid", "child_generation",
        "child_subscription_ack_wall_ns",
        "child_subscription_ack_identity_sha256",
    )
    session_ids = {tuple(row[field] for field in session_fields)
                   for row in ordered_segments}
    if len(session_ids) != 1:
        _fail("MULTIPLE_CAPTURE_SESSIONS", "24+2 receipts mix sessions")

    analysis, watermark, containers = _source_sets(source_evidence)
    deny = {(row["size"], row["sha256"])
            for row in authority["deny_identities"]}
    all_evidence = sorted(
        analysis + watermark + containers,
        key=lambda row: (row["key"], row["version_id"]),
    )
    overlaps = [row for row in all_evidence
                if (row["size"], row["sha256"]) in deny]
    if overlaps:
        _fail("OLD_LINEAGE_OVERLAP", overlaps[0]["key"])
    seal = source_evidence.get("seal")
    if (not isinstance(seal, dict) or seal.get("date") != date
            or seal.get("version") != 2 or seal.get("method") != "full_v2"
            or seal.get("status") != "SEALED"
            or seal.get("bucket") != fresh.SOURCE_BUCKET
            or seal.get("key") != f"ec2/warehouse/seals/date={date}.json"):
        _fail("SOURCE_SEAL_INVALID", date)
    source_sha = _sha(source_evidence.get("evidence_sha256"),
                      "source evidence SHA")
    if source_sha != fresh.canonical_sha256({
            key: value for key, value in source_evidence.items()
            if key != "evidence_sha256"}):
        _fail("SOURCE_EVIDENCE_INVALID", "self digest mismatch")
    if any(name not in input_sha256 for name in INPUT_NAMES):
        _fail("INPUT_BINDING_INVALID", "input hash set is incomplete")
    bindings = {name: _sha(input_sha256[name], name)
                for name in INPUT_NAMES}
    try:
        generated = dt.datetime.strptime(
            generated_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
    except (TypeError, ValueError) as exc:
        raise EligibilityError("GENERATED_TIME_INVALID", str(exc)) from exc
    if generated < day_start + dt.timedelta(days=1, hours=2):
        _fail("WATERMARK_NOT_CLOSED", generated_at_utc)

    session_id = next(iter(session_ids))
    payload = {
        "schema_version": SCHEMA,
        "state": STATE,
        "research_ready": True,
        "date": date,
        "lane_id": LANE_ID,
        "generation": authority["generation"],
        "strict_t0_utc": authority["strict_t0_utc"],
        "authority_sha256": authority["authority_sha256"],
        "authority_envelope_sha256": envelope["envelope_sha256"],
        "source_evidence_sha256": source_sha,
        "source_seal": {
            "bucket": seal["bucket"], "key": seal["key"],
            "version_id": seal["version_id"], "size": seal["size"],
            "sha256": seal["sha256"], "verification_state": "PASS",
        },
        "analysis_hour_count": 24,
        "watermark_hour_count": 2,
        "expected_hours": expected_hours,
        "session": {
            **dict(zip(session_fields, session_id)),
            "segment_receipt_set_sha256": canonical_sha256(ordered_segments),
            "first_hour": expected_hours[0],
            "last_hour": expected_hours[-1],
        },
        "eligible_objects": analysis,
        "eligible_object_set_sha256": canonical_sha256(analysis),
        "watermark_objects": watermark,
        "watermark_object_set_sha256": canonical_sha256(watermark),
        "receipt_container_objects": containers,
        "receipt_container_object_set_sha256": canonical_sha256(containers),
        "whole_evidence_object_set_sha256": canonical_sha256(all_evidence),
        "old_lineage_state": "DATA_INTEGRITY_BLOCKED",
        "repair_state": "FORBIDDEN",
        "old_284_object_count": authority["old_284_object_count"],
        "old_284_object_set_sha256":
            authority["old_284_object_set_sha256"],
        "old_lineage_overlap_count": 0,
        "alert_state": "ABSENT",
        "input_sha256": bindings,
        "generated_at_utc": generated_at_utc,
        "aws_reads": 0,
        "aws_writes": 0,
        "data_objects_copied": 0,
    }
    payload["eligibility_sha256"] = canonical_sha256(payload)
    return payload


def _canonical_json_file(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def _canonical_ledger(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


def _write_bytes(path: pathlib.Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail("OUTPUT_WRITE_FAILED", str(path))
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def create_package(*, date: str, authority_path: pathlib.Path,
                   source_path: pathlib.Path, hour_receipts_path: pathlib.Path,
                   session_ledger_path: pathlib.Path,
                   alert_path: pathlib.Path | None,
                   output_root: pathlib.Path,
                   generated_at_utc: str | None = None) -> pathlib.Path:
    """Create one immutable date directory; ``ELIGIBLE.json`` is last."""
    date = _date(date)
    if alert_path is not None and os.path.lexists(alert_path):
        _fail("CAPTURE_ALERT_PRESENT", str(alert_path))
    root = pathlib.Path(output_root).absolute()
    target = root / f"date={date}"
    if target.exists():
        # The date directory is a commit-once authority.  Retries validate and
        # reuse its complete fixed package instead of comparing it with newly
        # timestamped mutable producer inputs.
        load_package(target / ELIGIBLE_NAME, expected_date=date)
        return target / ELIGIBLE_NAME
    authority_value, _authority_raw = _read_json(authority_path, "authority")
    source_value, _source_raw = _read_json(source_path, "source evidence")
    hour_value, _hour_raw = _read_json(hour_receipts_path, "hour receipts")
    ledger_raw = _read_regular(
        session_ledger_path, MAX_LEDGER_BYTES, "session ledger")
    ledger_rows = _parse_ledger(ledger_raw)
    expected = set(_expected_hours(date))
    authority = precommit.validate_precommit_envelope(
        authority_value)["authority"]
    selected = [row for row in ledger_rows
                if row.get("type") == "rfq_segment_receipt"
                and row.get("schema") == "rfq-segment-receipt-v3"
                and row.get("authority_sha256") == authority["authority_sha256"]
                and row.get("generation") == authority["generation"]
                and row.get("segment_hour") in expected]
    normalized_inputs = {
        AUTHORITY_NAME: _canonical_json_file(authority_value),
        SOURCE_NAME: _canonical_json_file(source_value),
        HOURS_NAME: _canonical_json_file(hour_value),
        SESSION_NAME: _canonical_ledger(selected),
    }
    input_sha = {name: hashlib.sha256(raw).hexdigest()
                 for name, raw in normalized_inputs.items()}
    generated_at_utc = generated_at_utc or dt.datetime.now(
        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker = build_eligibility(
        date=date, authority_envelope=authority_value,
        source_evidence=source_value, hour_receipt_set=hour_value,
        session_ledger_rows=selected, input_sha256=input_sha,
        generated_at_utc=generated_at_utc, alert_absent=True)

    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    pending = pathlib.Path(tempfile.mkdtemp(
        prefix=f".date={date}.pending-", dir=root))
    try:
        os.chmod(pending, 0o750)
        for name in INPUT_NAMES:
            _write_bytes(pending / name, normalized_inputs[name])
        _write_bytes(pending / ELIGIBLE_NAME, _canonical_json_file(marker))
        directory_fd = os.open(pending, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(pending, target)
        root_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return target / ELIGIBLE_NAME


def load_package(path: pathlib.Path, *, expected_date: str | None = None
                 ) -> dict[str, Any]:
    """Re-read every fixed input and reproduce the admission marker."""
    path = pathlib.Path(path).absolute()
    if path.name != ELIGIBLE_NAME:
        _fail("PACKAGE_PATH_INVALID", str(path))
    marker, _marker_raw = _read_json(path, "eligibility marker")
    if not isinstance(marker, dict):
        _fail("ELIGIBILITY_INVALID", "marker root is not an object")
    date = _date(marker.get("date"))
    if expected_date is not None and date != _date(expected_date):
        _fail("DATE_MISMATCH", f"{date} != {expected_date}")
    if path.parent.name != f"date={date}":
        _fail("PACKAGE_PATH_INVALID", str(path.parent))
    values = {}
    hashes = {}
    for name in INPUT_NAMES:
        raw = _read_regular(path.parent / name,
                            MAX_LEDGER_BYTES if name == SESSION_NAME
                            else MAX_JSON_BYTES, name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        values[name] = (_parse_ledger(raw) if name == SESSION_NAME
                        else _decode_json(raw, name))
    rebuilt = build_eligibility(
        date=date,
        authority_envelope=values[AUTHORITY_NAME],
        source_evidence=values[SOURCE_NAME],
        hour_receipt_set=values[HOURS_NAME],
        session_ledger_rows=values[SESSION_NAME],
        input_sha256=hashes,
        generated_at_utc=marker.get("generated_at_utc"),
        alert_absent=marker.get("alert_state") == "ABSENT",
    )
    if marker != rebuilt:
        _fail("ELIGIBILITY_INVALID", "marker differs from rebuilt evidence")
    _sha(marker.get("eligibility_sha256"), "eligibility_sha256")
    return copy.deepcopy(marker)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--date", required=True)
    build.add_argument("--authority-envelope", required=True, type=pathlib.Path)
    build.add_argument("--source-evidence", required=True, type=pathlib.Path)
    build.add_argument("--hour-receipts", required=True, type=pathlib.Path)
    build.add_argument("--session-ledger", required=True, type=pathlib.Path)
    build.add_argument("--alert-file", type=pathlib.Path)
    build.add_argument("--output-root", required=True, type=pathlib.Path)
    check = sub.add_parser("check")
    check.add_argument("--eligible", required=True, type=pathlib.Path)
    check.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            path = create_package(
                date=args.date, authority_path=args.authority_envelope,
                source_path=args.source_evidence,
                hour_receipts_path=args.hour_receipts,
                session_ledger_path=args.session_ledger,
                alert_path=args.alert_file, output_root=args.output_root)
            value = load_package(path, expected_date=args.date)
            print(json.dumps({
                "state": value["state"], "date": value["date"],
                "eligible": str(path),
                "eligibility_sha256": value["eligibility_sha256"],
                "eligible_objects": len(value["eligible_objects"]),
                "aws_writes": 0,
            }, sort_keys=True))
        else:
            value = load_package(args.eligible, expected_date=args.date)
            print(json.dumps({
                "state": value["state"], "date": value["date"],
                "eligibility_sha256": value["eligibility_sha256"],
                "research_ready": True, "aws_writes": 0,
            }, sort_keys=True))
        return 0
    except (EligibilityError, precommit.PrecommitError,
            fresh.FreshRfqError) as exc:
        print(f"FRESH_RFQ_DAILY_BLOCKED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
