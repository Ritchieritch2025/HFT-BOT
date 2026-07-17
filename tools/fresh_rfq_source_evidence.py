#!/usr/bin/env python3
"""Pure exact-byte source evidence for the isolated fresh-RFQ lane.

The module performs no filesystem, network, AWS, process, deployment, or
publication I/O.  A caller supplies bytes returned by exact-VersionId GETs.
This code verifies every supplied byte count and SHA-256 before it emits a
body-free evidence object.

Receipt-container completeness is an explicit input contract: the caller
supplies the complete exact identity set for the 26 close-hour container
families.  Every identity must have one matching exact-byte object.  The
first 25 close-hour families must also be members of D's full_v2 seal; the
last family (D+1 hour 02, which carries segment D+1 hour 01) must not be
misrepresented as a D-seal member.

The physical D full_v2 seal uses ``receipt_cross_day_hours=2`` and therefore
does include D+1 hour 00/01 RFQ capture objects.  They remain WATERMARK data:
physical membership in D's cross-day seal never places them in D's 24-hour
analysis set and never claims that D+1 has a full-day seal.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any


SCHEMA = "fresh-rfq-source-evidence-v1"
SOURCE_BUCKET = "kalshi-vault-ritcardo"
RAW_PREFIX = "ec2/raw/"
SEAL_PREFIX = "ec2/warehouse/seals/"
MAX_JSON_LINE_BYTES = 16 << 20

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3])$")
UTC_SECONDS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$")
CAPTURE_REL_RE = re.compile(
    r"^date=(\d{4}-\d{2}-\d{2})/rfq_([01]\d|2[0-3])\.ndjson"
    r"(?:\.([1-9][0-9]*))?$")
CONTAINER_REL_RE = re.compile(
    r"^date=(\d{4}-\d{2}-\d{2})/rfq_receipts_"
    r"([01]\d|2[0-3])\.ndjson(?:\.([1-9][0-9]*))?$")

SEAL_FIELDS = {
    "version", "method", "go_no_go_eligible", "unverified", "code_commit",
    "status", "date", "sealed_at", "manifest_date_sha256", "archive_files",
    "archive_rows", "archive_file_stats", "capture_quality_status",
    "raw_retention_requirement", "receipt_cross_day_hours", "raw_files",
    "discovery_completeness",
}
RAW_FILE_FIELDS = {
    "file", "size", "inode", "mtime_ns", "ctime_ns", "checkpoint", "sha256",
}
EXACT_OBJECT_FIELDS = {"bucket", "key", "version_id", "size", "sha256", "body"}
IDENTITY_FIELDS = {"bucket", "key", "version_id", "size", "sha256"}
OUTER_FIELDS = {
    "recv_mono_ns", "recv_wall_ns", "source", "channel", "source_ticker",
    "marker", "raw",
}
SHARD_FIELDS = {
    "ordinal", "relpath", "bytes_before", "size", "parsed_bytes_at_close",
    "sha256",
}


class SourceEvidenceError(RuntimeError):
    """Fail-closed source-evidence violation with a stable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _DuplicateJsonKey(ValueError):
    pass


def _fail(code: str, detail: str) -> None:
    raise SourceEvidenceError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _strict_json_bytes(raw: bytes, label: str) -> Any:
    if not isinstance(raw, bytes):
        _fail("JSON_BYTES_REQUIRED", f"{label} must be bytes")
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("INVALID_JSON", f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text, object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateJsonKey as exc:
        _fail("DUPLICATE_JSON_KEY", f"{label} duplicates key {exc}")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        _fail("INVALID_JSON", f"{label}: {exc}")


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value or
            value != value.strip()):
        _fail("INVALID_TEXT", f"{label} must be canonical non-empty text")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase 64-hex")
    return value


def _version(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.lower() == "null":
        _fail("VERSION_REQUIRED", f"{label} must be a non-null exact VersionId")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("INVALID_INTEGER", f"{label} must be an integer >= {minimum}")
    return value


def _safe_key(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.startswith("/") or "\\" in value:
        _fail("UNSAFE_KEY", f"{label} is not a portable relative key")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("UNSAFE_KEY", f"{label} escapes containment")
    return "/".join(parts)


def _date(value: Any, label: str) -> dt.date:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    if parsed.isoformat() != value:
        _fail("INVALID_DATE", f"{label} is not canonical")
    return parsed


def _hour(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or HOUR_RE.fullmatch(value) is None:
        _fail("INVALID_HOUR", f"{label} must be YYYY-MM-DDTHH")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H").replace(
            tzinfo=dt.timezone.utc)
    except ValueError as exc:
        _fail("INVALID_HOUR", f"{label}: {exc}")
    if parsed.strftime("%Y-%m-%dT%H") != value:
        _fail("INVALID_HOUR", f"{label} is not canonical")
    return parsed


def _normalize_expected_hours(value: Any) -> tuple[list[str], dt.datetime]:
    if not isinstance(value, list) or len(value) != 26:
        _fail("EXPECTED_HOURS", "exactly 26 expected hours are required")
    parsed = [_hour(row, f"expected_hours[{index}]")
              for index, row in enumerate(value)]
    start = parsed[0]
    if start.hour != 0:
        _fail("EXPECTED_HOURS", "analysis D must begin at UTC hour 00")
    expected = [start + dt.timedelta(hours=index) for index in range(26)]
    if parsed != expected:
        _fail("EXPECTED_HOURS", "expected hours must be D 00..23 plus D+1 00/01")
    return [row.strftime("%Y-%m-%dT%H") for row in parsed], start


def _normalize_identity(value: Any, label: str) -> dict[str, Any]:
    value = _exact_keys(value, IDENTITY_FIELDS, label)
    bucket = _text(value["bucket"], f"{label}.bucket")
    if bucket != SOURCE_BUCKET:
        _fail("BUCKET_MISMATCH", f"{label}.bucket is outside the fixed source")
    return {
        "bucket": bucket,
        "key": _safe_key(value["key"], f"{label}.key"),
        "version_id": _version(value["version_id"], f"{label}.version_id"),
        "size": _integer(value["size"], f"{label}.size"),
        "sha256": _sha(value["sha256"], f"{label}.sha256"),
    }


def _normalize_exact_object(value: Any, label: str) -> tuple[dict[str, Any], bytes]:
    value = _exact_keys(value, EXACT_OBJECT_FIELDS, label)
    identity = _normalize_identity(
        {field: value[field] for field in IDENTITY_FIELDS}, label)
    body = value["body"]
    if type(body) is not bytes:
        _fail("BODY_BYTES_REQUIRED", f"{label}.body must be exact bytes")
    if len(body) != identity["size"]:
        _fail("OBJECT_SIZE_MISMATCH", f"{label}.body size differs from identity")
    digest = hashlib.sha256(body).hexdigest()
    if digest != identity["sha256"]:
        _fail("OBJECT_SHA_MISMATCH", f"{label}.body SHA-256 differs from identity")
    return identity, body


def _parse_rel_identity(
    identity: dict[str, Any], pattern: re.Pattern[str], label: str,
) -> tuple[str, str, int]:
    if not identity["key"].startswith(RAW_PREFIX):
        _fail("SOURCE_KIND_MIX", f"{label} is outside {RAW_PREFIX}")
    relpath = identity["key"][len(RAW_PREFIX):]
    match = pattern.fullmatch(relpath)
    if match is None:
        _fail("SOURCE_KIND_MIX", f"{label} has the wrong RFQ source family")
    _date(match.group(1), f"{label}.date")
    ordinal = 0 if match.group(3) is None else int(match.group(3))
    return relpath, f"{match.group(1)}T{match.group(2)}", ordinal


def _natural_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (row["source_hour"], row["ordinal"]))


def _require_contiguous_shards(
    rows: list[dict[str, Any]], expected_hours: set[str], label: str,
) -> None:
    groups: dict[str, list[int]] = {}
    for row in rows:
        groups.setdefault(row["source_hour"], []).append(row["ordinal"])
    if set(groups) != expected_hours:
        missing = sorted(expected_hours - set(groups))
        extra = sorted(set(groups) - expected_hours)
        _fail("SHARD_SET", f"{label} hours differ: missing={missing} extra={extra}")
    for hour, ordinals in groups.items():
        ordered = sorted(ordinals)
        if ordered != list(range(len(ordered))):
            _fail("SHARD_SET", f"{label} {hour} ordinals are not contiguous from zero")


def _normalize_raw_file(value: Any, index: int) -> dict[str, Any]:
    label = f"seal.raw_files[{index}]"
    value = _exact_keys(value, RAW_FILE_FIELDS, label)
    result = {
        "file": _safe_key(value["file"], f"{label}.file"),
        "size": _integer(value["size"], f"{label}.size"),
        "inode": _integer(value["inode"], f"{label}.inode", minimum=1),
        "mtime_ns": _integer(value["mtime_ns"], f"{label}.mtime_ns"),
        "ctime_ns": _integer(value["ctime_ns"], f"{label}.ctime_ns"),
        "checkpoint": _integer(value["checkpoint"], f"{label}.checkpoint"),
        "sha256": _sha(value["sha256"], f"{label}.sha256"),
    }
    if result["checkpoint"] > result["size"]:
        _fail("SEAL_RAW_FILE", f"{label}.checkpoint exceeds size")
    return result


def _parse_seal(
    seal_object: Any, analysis_start: dt.datetime, expected_hours: list[str],
) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]
]:
    identity, body = _normalize_exact_object(seal_object, "seal_object")
    seal = _strict_json_bytes(body, "full_v2 seal")
    _exact_keys(seal, SEAL_FIELDS, "full_v2 seal")
    date_text = analysis_start.strftime("%Y-%m-%d")
    expected_key = f"{SEAL_PREFIX}date={date_text}.json"
    if identity["key"] != expected_key:
        _fail("SEAL_IDENTITY", "seal key does not bind the analysis date")
    if (seal.get("version") != 2 or seal.get("method") != "full_v2" or
            seal.get("status") != "SEALED" or seal.get("date") != date_text or
            seal.get("go_no_go_eligible") is not True or
            seal.get("unverified") != [] or
            seal.get("receipt_cross_day_hours") != 2):
        _fail("SEAL_NOT_FULL_V2", "seal is not eligible full_v2 for analysis D")
    if (not isinstance(seal.get("code_commit"), str) or
            COMMIT_RE.fullmatch(seal["code_commit"]) is None):
        _fail("SEAL_SCHEMA", "seal code_commit must be exact 40-hex")
    if (not isinstance(seal.get("sealed_at"), str) or
            UTC_SECONDS_RE.fullmatch(seal["sealed_at"]) is None):
        _fail("SEAL_SCHEMA", "seal sealed_at must be canonical UTC seconds")
    try:
        dt.datetime.strptime(seal["sealed_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        _fail("SEAL_SCHEMA", f"seal sealed_at is not a real timestamp: {exc}")
    _sha(seal.get("manifest_date_sha256"), "seal.manifest_date_sha256")
    _integer(seal.get("archive_files"), "seal.archive_files")
    _integer(seal.get("archive_rows"), "seal.archive_rows")
    if not isinstance(seal.get("archive_file_stats"), list):
        _fail("SEAL_SCHEMA", "seal.archive_file_stats must be a list")
    if not isinstance(seal.get("capture_quality_status"), str):
        _fail("SEAL_SCHEMA", "seal.capture_quality_status must be text")
    if seal.get("raw_retention_requirement") != "LOCAL_OR_VAULT_VERIFIED_RECEIPT":
        _fail("SEAL_SCHEMA", "seal raw-retention contract changed")
    if not isinstance(seal.get("discovery_completeness"), dict):
        _fail("SEAL_SCHEMA", "seal.discovery_completeness must be an object")
    if not isinstance(seal.get("raw_files"), list):
        _fail("SEAL_SCHEMA", "seal.raw_files must be a list")

    raw_rows = [_normalize_raw_file(row, index)
                for index, row in enumerate(seal["raw_files"])]
    if len({row["file"] for row in raw_rows}) != len(raw_rows):
        _fail("DUPLICATE_SEAL_FILE", "seal.raw_files contains duplicate file paths")

    captures: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    for row in raw_rows:
        capture = CAPTURE_REL_RE.fullmatch(row["file"])
        container = CONTAINER_REL_RE.fullmatch(row["file"])
        if capture is not None:
            source_hour = f"{capture.group(1)}T{capture.group(2)}"
            ordinal = 0 if capture.group(3) is None else int(capture.group(3))
            if row["checkpoint"] != row["size"]:
                _fail("SEAL_RAW_FILE", f"capture {row['file']} is not byte-complete")
            captures.append({
                "file": row["file"], "size": row["size"],
                "sha256": row["sha256"], "checkpoint": row["checkpoint"],
                "source_hour": source_hour, "ordinal": ordinal,
                "role": "ANALYSIS" if source_hour in set(expected_hours[:24])
                else "WATERMARK",
                "seal_membership_scope": "D_ANALYSIS_MEMBER"
                if source_hour in set(expected_hours[:24])
                else "D_FULL_V2_CROSS_DAY_MEMBER",
            })
        elif container is not None:
            source_hour = f"{container.group(1)}T{container.group(2)}"
            ordinal = 0 if container.group(3) is None else int(container.group(3))
            if row["checkpoint"] != row["size"]:
                _fail("SEAL_RAW_FILE", f"container {row['file']} is not byte-complete")
            containers.append({
                "file": row["file"], "size": row["size"],
                "sha256": row["sha256"], "checkpoint": row["checkpoint"],
                "source_hour": source_hour, "ordinal": ordinal,
            })
        elif row["file"].split("/")[-1].startswith("rfq_"):
            _fail("RFQ_KIND_MIX", f"unrecognized RFQ family in seal: {row['file']}")

    expected_set = set(expected_hours)
    _require_contiguous_shards(captures, expected_set, "sealed RFQ capture")
    captures = _natural_rows(captures)

    next_date = (analysis_start.date() + dt.timedelta(days=1)).isoformat()
    allowed_container_hours = {
        *(f"{date_text}T{hour:02d}" for hour in range(24)),
        f"{next_date}T00", f"{next_date}T01",
    }
    bad_container_hours = sorted(
        {row["source_hour"] for row in containers} - allowed_container_hours)
    if bad_container_hours:
        _fail("SEAL_CONTAINER_SCOPE",
              f"D seal contains out-of-scope receipt containers {bad_container_hours}")
    # Validate natural shard structure for each container family that the seal
    # actually names.  A family need not exist when it carried no row.
    by_container_hour: dict[str, list[dict[str, Any]]] = {}
    for row in containers:
        by_container_hour.setdefault(row["source_hour"], []).append(row)
    for hour, rows in by_container_hour.items():
        ordinals = sorted(row["ordinal"] for row in rows)
        if ordinals != list(range(len(ordinals))):
            _fail("SHARD_SET", f"sealed receipt container {hour} has shard gaps")
    containers = _natural_rows(containers)

    seal_projection = {
        **identity,
        "date": date_text,
        "version": 2,
        "method": "full_v2",
        "status": "SEALED",
        "code_commit": seal["code_commit"],
        "manifest_date_sha256": seal["manifest_date_sha256"],
    }
    return identity, captures, containers, seal_projection


def _normalize_capture_objects(
    values: Any, sealed_rows: list[dict[str, Any]], expected_hours: list[str],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if not isinstance(values, list):
        _fail("OBJECT_LIST", "capture_objects must be a list")
    normalized: list[dict[str, Any]] = []
    bodies: dict[str, bytes] = {}
    for index, value in enumerate(values):
        identity, body = _normalize_exact_object(value, f"capture_objects[{index}]")
        relpath, source_hour, ordinal = _parse_rel_identity(
            identity, CAPTURE_REL_RE, f"capture_objects[{index}]")
        if identity["key"] in bodies:
            _fail("DUPLICATE_OBJECT", f"duplicate capture key {identity['key']}")
        bodies[identity["key"]] = body
        normalized.append({
            **identity, "relpath": relpath, "source_hour": source_hour,
            "ordinal": ordinal,
            "role": "ANALYSIS" if source_hour in set(expected_hours[:24])
            else "WATERMARK",
            "seal_member": True,
            "seal_membership_scope": "D_ANALYSIS_MEMBER"
            if source_hour in set(expected_hours[:24])
            else "D_FULL_V2_CROSS_DAY_MEMBER",
        })
    normalized = _natural_rows(normalized)
    _require_contiguous_shards(normalized, set(expected_hours), "exact capture")

    sealed_by_key = {f"{RAW_PREFIX}{row['file']}": row for row in sealed_rows}
    if set(bodies) != set(sealed_by_key):
        _fail("CAPTURE_SET_MISMATCH",
              "exact capture object keys do not equal the seal RFQ capture set")
    for row in normalized:
        sealed = sealed_by_key[row["key"]]
        if (row["size"], row["sha256"], row["source_hour"], row["ordinal"]) != (
                sealed["size"], sealed["sha256"], sealed["source_hour"],
                sealed["ordinal"]):
            _fail("CAPTURE_SET_MISMATCH",
                  f"capture exact identity differs from seal: {row['key']}")
    return normalized, bodies


def _normalize_complete_containers(
    identities_value: Any, object_values: Any, sealed_rows: list[dict[str, Any]],
    expected_hours: list[str],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if not isinstance(identities_value, list):
        _fail("OBJECT_LIST", "complete_container_identities must be a list")
    identities: list[dict[str, Any]] = []
    for index, value in enumerate(identities_value):
        identity = _normalize_identity(value, f"complete_container_identities[{index}]")
        relpath, source_hour, ordinal = _parse_rel_identity(
            identity, CONTAINER_REL_RE,
            f"complete_container_identities[{index}]")
        identities.append({
            **identity, "relpath": relpath, "source_hour": source_hour,
            "ordinal": ordinal,
        })
    if len({row["key"] for row in identities}) != len(identities):
        _fail("DUPLICATE_OBJECT", "complete container identities duplicate a key")

    expected_close_hours = [
        (_hour(hour, "expected hour") + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        for hour in expected_hours
    ]
    _require_contiguous_shards(
        identities, set(expected_close_hours), "complete receipt container")
    identities = _natural_rows(identities)

    sealed_by_key = {f"{RAW_PREFIX}{row['file']}": row for row in sealed_rows}
    final_close = expected_close_hours[-1]
    relevant_sealed = {
        key: row for key, row in sealed_by_key.items()
        if row["source_hour"] in set(expected_close_hours[:-1])
    }
    supplied_pre_final = {
        row["key"]: row for row in identities if row["source_hour"] != final_close
    }
    if set(supplied_pre_final) != set(relevant_sealed):
        _fail("CONTAINER_SEAL_SET_MISMATCH",
              "first 25 close-hour container identities do not equal D seal members")
    for key, row in supplied_pre_final.items():
        sealed = relevant_sealed[key]
        if (row["size"], row["sha256"], row["ordinal"]) != (
                sealed["size"], sealed["sha256"], sealed["ordinal"]):
            _fail("CONTAINER_SEAL_SET_MISMATCH",
                  f"container exact identity differs from D seal: {key}")
    final_rows = [row for row in identities if row["source_hour"] == final_close]
    if not final_rows:
        _fail("CONTAINER_SET_MISMATCH", "D+1 hour 02 close container is required")
    if any(row["key"] in sealed_by_key for row in final_rows):
        _fail("CONTAINER_SEAL_SCOPE",
              "D+1 hour 02 container must not be claimed by D seal")

    if not isinstance(object_values, list):
        _fail("OBJECT_LIST", "receipt_container_objects must be a list")
    objects: dict[str, tuple[dict[str, Any], bytes]] = {}
    for index, value in enumerate(object_values):
        identity, body = _normalize_exact_object(
            value, f"receipt_container_objects[{index}]")
        _parse_rel_identity(
            identity, CONTAINER_REL_RE, f"receipt_container_objects[{index}]")
        if identity["key"] in objects:
            _fail("DUPLICATE_OBJECT", f"duplicate container key {identity['key']}")
        objects[identity["key"]] = (identity, body)
    identity_by_key = {row["key"]: row for row in identities}
    if set(objects) != set(identity_by_key):
        _fail("CONTAINER_SET_MISMATCH",
              "container exact-byte objects do not equal the complete identity set")
    bodies: dict[str, bytes] = {}
    output: list[dict[str, Any]] = []
    for row in identities:
        observed, body = objects[row["key"]]
        projected = {field: row[field] for field in IDENTITY_FIELDS}
        if observed != projected:
            _fail("CONTAINER_SET_MISMATCH",
                  f"container object differs from complete identity: {row['key']}")
        if not body:
            _fail("EMPTY_CONTAINER", f"receipt container is empty: {row['key']}")
        bodies[row["key"]] = body
        output.append({
            **row,
            "seal_member": row["source_hour"] != final_close,
        })
    return output, bodies


def _normalize_receipt_capture_shards(
    receipt: dict[str, Any], hour: str,
    capture_rows: list[dict[str, Any]],
) -> str:
    shards = receipt.get("capture_shards")
    if not isinstance(shards, list) or not shards:
        _fail("SEGMENT_CAPTURE_SET", f"{hour} capture_shards must be non-empty")
    normalized = []
    date_text, hour_number = hour.split("T")
    base_rel = f"date={date_text}/rfq_{hour_number}.ndjson"
    for index, row in enumerate(shards):
        row = _exact_keys(row, SHARD_FIELDS, f"{hour}.capture_shards[{index}]")
        relpath = row.get("relpath")
        expected_rel = base_rel if index == 0 else f"{base_rel}.{index}"
        size = _integer(row.get("size"), f"{hour}.capture_shards[{index}].size",
                        minimum=1)
        normalized_row = {
            "ordinal": index,
            "relpath": relpath,
            "bytes_before": 0,
            "size": size,
            "parsed_bytes_at_close": size,
            "sha256": _sha(row.get("sha256"),
                            f"{hour}.capture_shards[{index}].sha256"),
        }
        if (row.get("ordinal") != index or relpath != expected_rel or
                row.get("bytes_before") != 0 or
                row.get("parsed_bytes_at_close") != size):
            _fail("SEGMENT_CAPTURE_SET", f"{hour} capture shard contract changed")
        normalized.append(normalized_row)
    total = sum(row["size"] for row in normalized)
    compatibility_sha = normalized[0]["sha256"] if len(normalized) == 1 else None
    if (receipt.get("capture_relpath") != base_rel or
            receipt.get("capture_bytes_before") != 0 or
            receipt.get("capture_bytes_at_close") != total or
            receipt.get("capture_shard_count") != len(normalized) or
            receipt.get("capture_shard_set_sha256") != canonical_sha256(normalized) or
            receipt.get("capture_sha256_at_close") != compatibility_sha):
        _fail("SEGMENT_CAPTURE_SET", f"{hour} shard aggregate binding mismatch")

    expected_rows = [row for row in capture_rows if row["source_hour"] == hour]
    expected_projection = [
        {
            "ordinal": row["ordinal"], "relpath": row["relpath"],
            "size": row["size"], "sha256": row["sha256"],
        }
        for row in expected_rows
    ]
    actual_projection = [
        {
            "ordinal": row["ordinal"], "relpath": row["relpath"],
            "size": row["size"], "sha256": row["sha256"],
        }
        for row in normalized
    ]
    if actual_projection != expected_projection:
        _fail("SEGMENT_CAPTURE_SET",
              f"{hour} receipt shards differ from exact sealed capture objects")
    exact_identities = [
        {field: row[field] for field in IDENTITY_FIELDS}
        for row in expected_rows
    ]
    return canonical_sha256(exact_identities)


def _parse_container_lines(
    container: dict[str, Any], body: bytes,
    capture_rows: list[dict[str, Any]], expected_set: set[str],
) -> list[dict[str, Any]]:
    proofs = []
    offset = 0
    line_number = 0
    expected_segment = (
        _hour(container["source_hour"], "container source_hour") -
        dt.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H")
    while offset < len(body):
        newline = body.find(b"\n", offset)
        if newline < 0:
            _fail("TORN_CONTAINER",
                  f"{container['key']} has a non-newline-terminated final row")
        if newline + 1 - offset > MAX_JSON_LINE_BYTES:
            _fail("CONTAINER_LINE_TOO_LARGE",
                  f"{container['key']} line {line_number + 1} exceeds the limit")
        physical = body[offset:newline + 1]
        payload = physical[:-1]
        line_number += 1
        if not payload or payload.endswith(b"\r"):
            _fail("MALFORMED_CONTAINER_LINE",
                  f"{container['key']} line {line_number} is blank or non-LF")
        outer = _strict_json_bytes(
            payload, f"{container['key']} outer line {line_number}")
        _exact_keys(outer, OUTER_FIELDS,
                    f"{container['key']} outer line {line_number}")
        if (outer.get("source") != "Kalshi" or
                outer.get("channel") != "rfq_segment_receipt" or
                outer.get("source_ticker") != "" or
                outer.get("marker") != "segment_receipt" or
                type(outer.get("recv_mono_ns")) is not int or
                outer["recv_mono_ns"] <= 0 or
                type(outer.get("recv_wall_ns")) is not int or
                outer["recv_wall_ns"] <= 0 or
                not isinstance(outer.get("raw"), str)):
            _fail("OUTER_BINDING",
                  f"{container['key']} line {line_number} is not a receipt wrapper")
        try:
            observed_container_hour = dt.datetime.fromtimestamp(
                outer["recv_wall_ns"] // 1_000_000_000,
                tz=dt.timezone.utc).strftime("%Y-%m-%dT%H")
        except (OverflowError, OSError, ValueError) as exc:
            _fail("OUTER_BINDING",
                  f"{container['key']} line {line_number} has invalid recv time: {exc}")
        if observed_container_hour != container["source_hour"]:
            _fail("OUTER_BINDING",
                  f"{container['key']} line {line_number} recv hour/key mismatch")
        receipt = _strict_json_bytes(
            outer["raw"].encode("utf-8"),
            f"{container['key']} inner line {line_number}")
        if not isinstance(receipt, dict):
            _fail("SEGMENT_SCHEMA", "inner receipt must be an object")
        if (receipt.get("schema") != "rfq-segment-receipt-v3" or
                receipt.get("type") != "rfq_segment_receipt"):
            _fail("SEGMENT_SCHEMA",
                  f"{container['key']} line {line_number} is not segment v3")
        segment_hour = receipt.get("segment_hour")
        _hour(segment_hour, "segment receipt hour")
        if segment_hour != expected_segment or segment_hour not in expected_set:
            _fail("POST_RECEIPT_EXTRA",
                  f"{container['key']} contains out-of-scope segment {segment_hour}")
        capture_set_sha = _normalize_receipt_capture_shards(
            receipt, segment_hour, capture_rows)
        # Offsets and lengths address the physical container bytes.  The outer
        # digest deliberately includes the terminating LF so an appended,
        # removed, or changed delimiter cannot retain the same line proof.
        proofs.append({
            "segment_hour": segment_hour,
            "role": "ANALYSIS" if segment_hour in {
                row["source_hour"] for row in capture_rows if row["role"] == "ANALYSIS"
            } else "WATERMARK",
            "status": receipt.get("status"),
            "findings": receipt.get("findings"),
            "fresh_lane_state": receipt.get("fresh_lane_state"),
            "authority_sha256": receipt.get("authority_sha256"),
            "generation": receipt.get("generation"),
            "container_bucket": container["bucket"],
            "container_key": container["key"],
            "container_version_id": container["version_id"],
            "container_size": container["size"],
            "container_sha256": container["sha256"],
            "container_seal_member": container["seal_member"],
            "line_number": line_number,
            "byte_offset": offset,
            "byte_length": len(physical),
            "outer_row_sha256": hashlib.sha256(physical).hexdigest(),
            "raw_segment_canonical_sha256": canonical_sha256(receipt),
            "capture_exact_object_set_sha256": capture_set_sha,
        })
        offset = newline + 1
    return proofs


def build_source_evidence(
    *,
    seal_object: dict[str, Any],
    capture_objects: list[dict[str, Any]],
    receipt_container_objects: list[dict[str, Any]],
    complete_container_identities: list[dict[str, Any]],
    expected_hours: list[str],
    authority_sha256: str,
    generation: str,
) -> dict[str, Any]:
    """Build body-free exact source evidence for D 24h + D+1 2h.

    ``complete_container_identities`` is the caller's complete set evidence for
    the 26 close-hour container families.  This function refuses cherry-pick:
    the supplied exact-byte container objects must equal that identity set.
    """
    expected_hours, analysis_start = _normalize_expected_hours(expected_hours)
    authority_sha256 = _sha(authority_sha256, "authority_sha256")
    if not isinstance(generation, str) or GENERATION_RE.fullmatch(generation) is None:
        _fail("INVALID_GENERATION", "generation is not canonical")

    _, sealed_capture_rows, sealed_container_rows, seal_projection = _parse_seal(
        seal_object, analysis_start, expected_hours)
    captures, _capture_bodies = _normalize_capture_objects(
        capture_objects, sealed_capture_rows, expected_hours)
    containers, container_bodies = _normalize_complete_containers(
        complete_container_identities, receipt_container_objects,
        sealed_container_rows, expected_hours)

    proofs: list[dict[str, Any]] = []
    expected_set = set(expected_hours)
    for container in containers:
        proofs.extend(_parse_container_lines(
            container, container_bodies[container["key"]], captures, expected_set))

    by_hour: dict[str, list[dict[str, Any]]] = {hour: [] for hour in expected_hours}
    for proof in proofs:
        by_hour[proof["segment_hour"]].append(proof)
    ordered_proofs = []
    for hour in expected_hours:
        rows = by_hour[hour]
        if not rows:
            _fail("RECEIPT_MISSING", f"no receipt line proves {hour}")
        if len(rows) != 1:
            _fail("RECEIPT_AMBIGUOUS",
                  f"{hour} has {len(rows)} receipt lines; exactly one is required")
        proof = rows[0]
        if (proof["authority_sha256"] != authority_sha256 or
                proof["generation"] != generation or
                proof["fresh_lane_state"] != "BOUND_AUTHORITY"):
            _fail("RECEIPT_AUTHORITY", f"{hour} authority/generation binding differs")
        if proof["status"] != "PASS" or proof["findings"] != []:
            _fail("RECEIPT_NOT_PASS", f"{hour} is not strict PASS")
        ordered_proofs.append(proof)

    analysis_captures = [row for row in captures if row["role"] == "ANALYSIS"]
    watermark_captures = [row for row in captures if row["role"] == "WATERMARK"]
    analysis_proofs = ordered_proofs[:24]
    watermark_proofs = ordered_proofs[24:]
    result = {
        "schema": SCHEMA,
        "state": "ALL_INPUT_EXACT_VERSION_BYTES_VERIFIED",
        "source_bucket": SOURCE_BUCKET,
        "analysis_date": analysis_start.strftime("%Y-%m-%d"),
        "expected_hours": expected_hours,
        "authority_sha256": authority_sha256,
        "generation": generation,
        "seal": seal_projection,
        "seal_binding_sha256": canonical_sha256(seal_projection),
        "seal_rfq_capture_members": sealed_capture_rows,
        "seal_rfq_capture_member_set_sha256": canonical_sha256(sealed_capture_rows),
        "seal_rfq_receipt_container_members": sealed_container_rows,
        "seal_rfq_receipt_container_member_set_sha256": canonical_sha256(
            sealed_container_rows),
        "analysis_capture_objects": analysis_captures,
        "analysis_capture_object_set_sha256": canonical_sha256(analysis_captures),
        "watermark_capture_objects": watermark_captures,
        "watermark_capture_object_set_sha256": canonical_sha256(watermark_captures),
        "receipt_containers": containers,
        "receipt_container_object_set_sha256": canonical_sha256(containers),
        "container_completeness_state":
            "CALLER_COMPLETE_IDENTITY_SET_EXACT_BYTES_VERIFIED",
        "analysis_receipt_proofs": analysis_proofs,
        "analysis_receipt_proof_set_sha256": canonical_sha256(analysis_proofs),
        "watermark_receipt_proofs": watermark_proofs,
        "watermark_receipt_proof_set_sha256": canonical_sha256(watermark_proofs),
        "all_input_bodies_omitted_from_output": True,
    }
    result["evidence_sha256"] = canonical_sha256(result)
    return result
