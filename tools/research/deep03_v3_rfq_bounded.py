#!/usr/bin/env python3
"""Bounded fresh-RFQ exploratory research for Deep03.

This module is deliberately downstream of the isolated ``W-RFQ-FRESH-01``
overlay.  It never discovers RFQ objects, resolves a latest S3 version, reads
the historical ``raw_rfq`` lineage, or writes AWS state.  A caller supplies a
locally cached fresh-overlay ``READY.json``, the exact fresh-epoch authority,
and a transport client for :class:`fresh_rfq_exact_reader.ExactReadSession`.

The exact object is opened once.  The canonical request-provenance builder
consumes it first; before the exact-reader context closes, a second bounded
local scan projects only narrow create/delete columns into full-RFQ-id hash
partitions.  No raw RFQ body survives the context.  Durable partitions use the
same fenced COMPLETE-receipt protocol as the bounded Deep03 L1/trades runtime.

D01--D07 are exploratory measurements.  D08 profitability/fill/PnL is always
blocked because passive communications frames do not observe quotes, fills,
fees, inventory, or settlement cash flows.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Iterator, Mapping
import uuid

import duckdb


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import fresh_rfq_exact_reader as exact_reader  # noqa: E402
import fresh_rfq_base_binding as base_binding  # noqa: E402
import fresh_rfq_market_mapping as market_mapping  # noqa: E402
import fresh_rfq_receipts as fresh_receipts  # noqa: E402
import fresh_rfq_request_provenance as request_provenance  # noqa: E402

from deep03_v3_methods import (  # noqa: E402
    BoundedCheckpointStore,
    path_list,
    quote,
)


SCHEMA = "deep03-fresh-rfq-bounded-report-v1"
OVERLAY_SCHEMA = "research-rfq-overlay-manifest-v4"
OVERLAY_LANE = "W-RFQ-FRESH-01"
OVERLAY_RECEIPT_SCHEMA = "fresh-rfq-overlay-receipt-v1"
OVERLAY_READY_SCHEMA = "fresh-rfq-overlay-cache-ready-v1"
SOURCE_EVENT_STAGE = "rfq_source_events"
SOURCE_META_STAGE = "rfq_source_meta"
MAPPING_STAGE = "rfq_mapping"
LIFECYCLE_STAGE = "rfq_lifecycle"
DEFAULT_HASH_BUCKETS = 32
MAX_ANALYSIS_DAYS = 31
MAX_OVERLAY_BYTES = 64 << 20
MAX_JSON_LINE_BYTES = request_provenance.MAX_JSON_LINE_BYTES
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
SAFE_STAGE_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
IMPACT_SCHEMA = "fresh-rfq-clob-impact-adapter-v2"
MAX_D07_COMPONENTS_PER_DAY = 2_000_000
MAX_D07_SOURCE_ROWS_PER_COMPONENT = 10_000_000
MAX_RFQ_CLOCK_ABS_SKEW_US = 5_000_000
EXACT_SOURCE_IDENTITY_FIELDS = {
    "logical_key", "bucket", "key", "version_id", "size", "sha256",
}
IMPACT_OBSERVATION_FIELDS = {
    "request_id", "component_index", "market_ticker", "status",
    "event_ts_us", "rfq_recv_wall_ns", "rfq_clock_skew_us",
    "pre_window_start_us", "pre_window_end_us",
    "post_window_start_us", "post_window_end_us",
    "pre_observation_ts_us", "post_observation_ts_us",
    "pre_mid_e6", "post_mid_e6", "pre_spread_e6", "post_spread_e6",
    "pre_depth_e2", "post_depth_e2", "l1_pre_rows", "l1_post_rows",
    "l2_pre_rows", "l2_post_rows", "gap_rows", "censor_reason",
}
IMPACT_STATUSES = {
    "OBSERVED", "UNMAPPED", "CENSORED_BOUNDARY", "CENSORED_GAP",
    "CENSORED_CLOCK",
}
SURVIVAL_HORIZONS_MS = (1_000, 5_000, 30_000, 60_000, 300_000)


class FreshRfqResearchError(RuntimeError):
    """Stable fail-closed error at the research boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise FreshRfqResearchError(code, detail)


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


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase SHA-256")
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _date(value: Any, label: str = "date") -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    if parsed.isoformat() != value:
        _fail("INVALID_DATE", f"{label} is not canonical")
    return value


def _read_canonical_json(path: Path, label: str) -> dict[str, Any]:
    path = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail("OVERLAY_READ_FAILED", f"{label}: {exc}")
    try:
        before = os.fstat(descriptor)
        if before.st_size <= 0 or before.st_size > MAX_OVERLAY_BYTES:
            _fail("OVERLAY_SIZE_INVALID", f"{label} size is outside bounds")
        chunks = bytearray()
        while len(chunks) <= MAX_OVERLAY_BYTES:
            chunk = os.read(
                descriptor, min(1 << 20, MAX_OVERLAY_BYTES + 1 - len(chunks))
            )
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(chunks) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns)
    ):
        _fail("OVERLAY_CHANGED", f"{label} changed while it was read")
    raw = bytes(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        _fail("OVERLAY_JSON_INVALID", f"{label}: {exc}")
    if not isinstance(value, dict) or raw not in {
        canonical_bytes(value), canonical_bytes(value) + b"\n"
    }:
        _fail("OVERLAY_NONCANONICAL", f"{label} is not canonical JSON")
    return value


def _verify_self_digest(value: dict[str, Any], field: str, label: str) -> None:
    supplied = _sha(value.get(field), f"{label}.{field}")
    unsigned = copy.deepcopy(value)
    unsigned.pop(field, None)
    if supplied != canonical_sha256(unsigned):
        _fail("DIGEST_MISMATCH", f"{label}.{field} mismatch")


def _validate_embedded_base(
    *,
    ready_doc: dict[str, Any],
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    date_text: str,
) -> dict[str, Any]:
    """Bind every local overlay alias to the embedded exact-base receipt.

    This does not claim that the consumer has re-read the exact manifest
    bytes.  It makes the local READY/receipt/manifest chain internally exact;
    :func:`_rebuild_exact_base` is the separate byte-level gate required
    before D07 may emit an observed result.
    """
    if ready_doc["base_terminal_file_sha256"] != receipt[
        "base_terminal_file_sha256"
    ]:
        _fail(
            "OVERLAY_BASE_TERMINAL_MISMATCH",
            "READY and overlay receipt bind different base terminal files",
        )
    base = manifest.get("base_binding")
    if not isinstance(base, dict):
        _fail("OVERLAY_BASE_BINDING", "embedded base binding is absent")
    _verify_self_digest(base, "binding_sha256", "embedded base binding")
    fixed = {
        "date": date_text,
        "binding_sha256": manifest.get("base_binding_sha256"),
        "manifest_exact_identity": receipt.get("base_manifest_exact_identity"),
        "rfq_objects_in_base": 0,
        "data_objects_copied": 0,
        "aws_write_authorized": False,
    }
    for field, expected in fixed.items():
        if base.get(field) != expected:
            _fail(
                "OVERLAY_BASE_BINDING",
                f"embedded base binding differs at {field}",
            )
    if receipt.get("base_binding_sha256") != base["binding_sha256"]:
        _fail("OVERLAY_BASE_BINDING", "receipt/base binding digest differs")

    universe = manifest.get("universe_provenance")
    if not isinstance(universe, dict) or universe.get(
        "base_binding_sha256"
    ) != base["binding_sha256"]:
        _fail(
            "OVERLAY_BASE_BINDING",
            "universe provenance does not bind the exact base",
        )
    try:
        provenance_families = universe["families"]
        base_families = base["families"]
        for family_name in ("orderbooks_l1", "orderbooks_full"):
            family = provenance_families[family_name]
            expected_family = base_families[family_name]
            identities = expected_family["objects"]
            if (
                expected_family["object_count"] != len(identities)
                or expected_family["set_sha256"] != canonical_sha256(identities)
                or family["source_object_count"] != len(identities)
                or family["source_object_set_sha256"]
                != expected_family["set_sha256"]
                or family["base_family_set_sha256"]
                != expected_family["set_sha256"]
                or family["body_verified_object_count"] != len(identities)
            ):
                _fail(
                    "OVERLAY_BASE_BINDING",
                    f"{family_name} provenance/base family differs",
                )
            receipts = family["object_receipts"]
            if not isinstance(receipts, list) or len(receipts) != len(identities):
                _fail(
                    "OVERLAY_BASE_BINDING",
                    f"{family_name} exact object receipts are incomplete",
                )
            projected = []
            for index, row in enumerate(receipts):
                if not isinstance(row, dict):
                    _fail(
                        "OVERLAY_BASE_BINDING",
                        f"{family_name} object receipt {index} is invalid",
                    )
                identity = {
                    field: row.get(field) for field in EXACT_SOURCE_IDENTITY_FIELDS
                }
                if set(identity) != EXACT_SOURCE_IDENTITY_FIELDS or (
                    row.get("body_size_verified") is not True
                    or row.get("body_sha256_verified") is not True
                ):
                    _fail(
                        "OVERLAY_BASE_BINDING",
                        f"{family_name} object receipt {index} lacks byte proof",
                    )
                projected.append(identity)
            projected.sort(key=lambda row: row["logical_key"])
            if canonical_bytes(projected) != canonical_bytes(identities):
                _fail(
                    "OVERLAY_BASE_BINDING",
                    f"{family_name} receipt identities differ from exact base",
                )
    except (KeyError, TypeError) as exc:
        _fail("OVERLAY_BASE_BINDING", f"base family evidence absent: {exc}")
    return copy.deepcopy(base)


def _rebuild_exact_base(
    descriptor: dict[str, Any], manifest_bytes: bytes,
) -> dict[str, Any]:
    """Rebuild the embedded base binding from the exact V3 manifest bytes."""
    if type(manifest_bytes) is not bytes:
        _fail(
            "D07_BASE_EXACT_BYTES_REQUIRED",
            f"{descriptor['date']} base manifest must be exact bytes",
        )
    try:
        rebuilt = base_binding.build_base_binding(
            manifest_bytes=manifest_bytes,
            manifest_exact_identity=descriptor["base_manifest_exact_identity"],
            date=descriptor["date"],
        )
    except base_binding.FreshRfqBaseBindingError as exc:
        _fail("D07_BASE_EXACT_REBUILD", str(exc))
    if canonical_bytes(rebuilt) != canonical_bytes(descriptor["base_binding"]):
        _fail(
            "D07_BASE_EXACT_REBUILD",
            f"{descriptor['date']} exact manifest rebuild differs from overlay",
        )
    return rebuilt


def _base_source_attestation(
    descriptor: dict[str, Any], family_name: str,
) -> dict[str, Any]:
    """Project the exact, body-verified L1/L2 source proof into D07."""
    base_family = descriptor["base_binding"]["families"][family_name]
    provenance_family = descriptor["manifest"]["universe_provenance"][
        "families"
    ][family_name]
    receipts = provenance_family["object_receipts"]
    result = {
        "schema": "fresh-rfq-d07-exact-source-attestation-v1",
        "analysis_date": descriptor["date"],
        "family": family_name,
        "base_binding_sha256": descriptor["base_binding_sha256"],
        "exact_objects": copy.deepcopy(base_family["objects"]),
        "exact_object_count": base_family["object_count"],
        "exact_object_set_sha256": base_family["set_sha256"],
        "body_verified_object_count": provenance_family[
            "body_verified_object_count"
        ],
        "source_row_count": sum(int(row["row_count"]) for row in receipts),
        "min_ts_us": min(int(row["min_ts_utc"]) for row in receipts),
        "max_ts_us": max(int(row["max_ts_utc"]) for row in receipts),
        "body_size_and_sha_verified": all(
            row.get("body_size_verified") is True
            and row.get("body_sha256_verified") is True
            for row in receipts
        ),
        "universe_family_receipt_sha256": canonical_sha256(
            provenance_family
        ),
    }
    result["attestation_sha256"] = canonical_sha256(result)
    return result


def build_l2_quality_gate(
    *, analysis_date: str, exact_identity: dict[str, Any], body: bytes,
) -> dict[str, Any]:
    """Build a D07 L2 quality gate from exact, caller-read receipt bytes.

    The body is verified against a non-null VersionId identity inside this
    function and is never retained in the result.  Any sequence loss, parser
    error, recorder gap/loss/epoch marker, absent file, or empty stream blocks
    the entire date rather than silently salvaging impact observations.
    """
    date_text = _date(analysis_date, "L2 quality date")
    identity_fields = {"bucket", "key", "version_id", "size", "sha256"}
    identity = _exact_keys(
        exact_identity, identity_fields, "L2 quality exact identity",
    )
    if (
        not isinstance(identity["bucket"], str)
        or not identity["bucket"]
        or not isinstance(identity["key"], str)
        or not identity["key"]
        or not isinstance(identity["version_id"], str)
        or not identity["version_id"]
        or identity["version_id"].lower() == "null"
        or type(identity["size"]) is not int
        or identity["size"] <= 0
    ):
        _fail("D07_L2_QUALITY_IDENTITY", "quality exact identity is invalid")
    _sha(identity["sha256"], "L2 quality identity.sha256")
    if type(body) is not bytes:
        _fail("D07_L2_QUALITY_BYTES", "quality receipt body must be bytes")
    if len(body) != identity["size"]:
        _fail("D07_L2_QUALITY_BYTES", "quality receipt size differs")
    if hashlib.sha256(body).hexdigest() != identity["sha256"]:
        _fail("D07_L2_QUALITY_BYTES", "quality receipt SHA-256 differs")
    try:
        receipt = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        _fail("D07_L2_QUALITY_JSON", str(exc))
    if not isinstance(receipt, dict) or receipt.get("date") != date_text:
        _fail("D07_L2_QUALITY_DATE", "quality receipt date differs")
    blockers: list[str] = []
    for key in (
        "parse_errors", "seq_gap_events", "seq_missed_total",
        "seq_regressions", "markers_lost_frames",
    ):
        value = receipt.get(key, 0)
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            blockers.append(f"{key}=INVALID")
        else:
            if count:
                blockers.append(f"{key}={count}")
    markers = receipt.get("recorder_markers") or {}
    if not isinstance(markers, Mapping):
        blockers.append("recorder_markers=INVALID")
        markers = {}
    for key, value in sorted(markers.items(), key=lambda row: str(row[0])):
        if str(key).lower() in {"gap", "loss", "epoch_change"}:
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                count = 1
            if count:
                blockers.append(f"recorder_marker:{key}={count}")
    if receipt.get("no_l2_files") is True:
        blockers.append("no_l2_files=true")
    try:
        lines = int(receipt.get("lines") or 0)
    except (TypeError, ValueError):
        lines = 0
    if lines <= 0:
        blockers.append("lines<=0")
    result = {
        "schema": "fresh-rfq-d07-l2-quality-gate-v1",
        "analysis_date": date_text,
        "state": "PASS" if not blockers else "REFUSED",
        "exact_identity": copy.deepcopy(identity),
        "exact_body_verified": True,
        "lines": lines,
        "blockers": blockers,
        "salvage_allowed": False,
    }
    result["gate_sha256"] = canonical_sha256(result)
    return result


def _validate_overlay_hours(
    manifest: dict[str, Any], authority: dict[str, Any], date_text: str,
) -> list[dict[str, Any]]:
    analysis = manifest.get("analysis_hours")
    watermark = manifest.get("watermark_hours")
    if not isinstance(analysis, list) or not isinstance(watermark, list):
        _fail("OVERLAY_HOURS_INVALID", "overlay hour ledgers must be lists")
    if len(analysis) != 24 or len(watermark) != 2:
        _fail("OVERLAY_HOURS_INVALID", "exact D 00..23 plus D+1 00/01 required")
    source_evidence = manifest.get("source_evidence")
    validated = []
    for index, row in enumerate(analysis + watermark):
        try:
            checked = fresh_receipts.validate_hour_receipt(
                row, authority, source_evidence=source_evidence,
            )
        except fresh_receipts.FreshRfqError as exc:
            _fail("FRESH_HOUR_INVALID", f"{exc.code}: {exc.detail}")
        validated.append(checked)
    start = dt.datetime.combine(
        dt.date.fromisoformat(date_text), dt.time(), tzinfo=dt.timezone.utc,
    )
    expected = [
        (start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
        for index in range(26)
    ]
    if [row["segment_hour"] for row in validated] != expected:
        _fail("OVERLAY_HOURS_INVALID", "overlay hour sequence is not contiguous")
    if any(
        row["resolution_state"] != "RESOLVED_EXACT"
        or row["complete"] is not True
        or row["subscription_invalidations"] != 0
        for row in validated
    ):
        _fail("SOURCE_GAP_GATE", "fresh RFQ hour evidence is not exact PASS")
    return validated


def load_overlay_descriptor(
    ready_path: str | os.PathLike[str], fresh_authority: dict[str, Any],
) -> dict[str, Any]:
    """Load and bind one immutable local fresh-overlay cache.

    The full authority validator proves the old 284-object deny set, strict T0,
    read-only lane, generation, and ``repair_state=FORBIDDEN``.  Every embedded
    hour is then revalidated against that authority and source-evidence body.
    """
    try:
        authority = fresh_receipts.validate_fresh_epoch_authority(
            fresh_authority
        )
    except fresh_receipts.FreshRfqError as exc:
        _fail("FRESH_AUTHORITY_INVALID", f"{exc.code}: {exc.detail}")
    ready = Path(ready_path).absolute()
    if ready.is_dir():
        ready = ready / "READY.json"
    root = ready.parent
    ready_doc = _read_canonical_json(ready, "READY.json")
    receipt = _read_canonical_json(root / "OVERLAY-RECEIPT.json", "overlay receipt")
    manifest = _read_canonical_json(root / "MANIFEST.json", "overlay manifest")

    ready_fields = {
        "schema_version", "state", "date", "overlay_receipt_sha256",
        "overlay_manifest_sha256", "base_terminal_file_sha256",
        "base_mutations", "ready_sha256",
    }
    _exact_keys(ready_doc, ready_fields, "overlay READY")
    if (
        ready_doc["schema_version"] != OVERLAY_READY_SCHEMA
        or ready_doc["state"] != "RFQ_OVERLAY_CACHE_READY"
        or ready_doc["base_mutations"] != 0
    ):
        _fail("OVERLAY_READY_INVALID", "overlay READY fixed contract changed")
    _verify_self_digest(ready_doc, "ready_sha256", "overlay READY")

    receipt_fields = {
        "schema_version", "state", "date", "base_state", "base_rfq",
        "base_terminal_file_sha256", "base_manifest_exact_identity",
        "base_binding_sha256", "eligibility_sha256", "authority_sha256",
        "source_evidence_sha256", "analysis_rfq_objects",
        "analysis_rfq_object_set_sha256", "overlay_manifest_sha256",
        "required_tags", "tag_state", "publication_state", "base_mutations",
        "data_objects_copied", "aws_writes", "overlay_receipt_sha256",
    }
    _exact_keys(receipt, receipt_fields, "overlay receipt")
    if (
        receipt["schema_version"] != OVERLAY_RECEIPT_SCHEMA
        or receipt["state"] != "RFQ_OVERLAY_LOCALLY_READY"
        or receipt["base_state"] != "V3_REFERENCE_PUBLISHED"
        or receipt["base_rfq"] != "OFF"
        or receipt["base_mutations"] != 0
        or receipt["data_objects_copied"] != 0
        or receipt["aws_writes"] != 0
        or receipt["required_tags"] != {
            "research-eligible": "true", "research-channel": "rfq"
        }
    ):
        _fail("OVERLAY_RECEIPT_INVALID", "overlay receipt fixed contract changed")
    _verify_self_digest(receipt, "overlay_receipt_sha256", "overlay receipt")

    if manifest.get("schema") != OVERLAY_SCHEMA or manifest.get("lane_id") != OVERLAY_LANE:
        _fail("OVERLAY_MANIFEST_INVALID", "wrong overlay schema/lane")
    if (
        manifest.get("state") != "LOCALLY_VERIFIED_UNPUBLISHED"
        or manifest.get("data_objects_copied") != 0
        or manifest.get("aws_write_authorized") is not False
        or manifest.get("research_eligible") is not False
        or manifest.get("research_ready") is not False
    ):
        _fail("OVERLAY_MANIFEST_INVALID", "overlay elevated a local-only claim")
    _verify_self_digest(manifest, "manifest_sha256", "overlay manifest")

    date_text = _date(ready_doc["date"], "overlay date")
    aliases = {
        "date": receipt["date"],
        "eligible_date": manifest.get("eligible_date"),
        "ready_receipt": ready_doc["overlay_receipt_sha256"],
        "receipt_sha": receipt["overlay_receipt_sha256"],
        "ready_manifest": ready_doc["overlay_manifest_sha256"],
        "receipt_manifest": receipt["overlay_manifest_sha256"],
        "manifest_sha": manifest["manifest_sha256"],
        "authority_receipt": receipt["authority_sha256"],
        "authority_manifest": manifest.get("authority_sha256"),
        "authority": authority["authority_sha256"],
    }
    if aliases["date"] != date_text or aliases["eligible_date"] != date_text:
        _fail("OVERLAY_BINDING", "overlay date aliases differ")
    if len({aliases["ready_receipt"], aliases["receipt_sha"]}) != 1:
        _fail("OVERLAY_BINDING", "READY/receipt digest binding differs")
    if len({aliases["ready_manifest"], aliases["receipt_manifest"], aliases["manifest_sha"]}) != 1:
        _fail("OVERLAY_BINDING", "manifest digest aliases differ")
    if len({aliases["authority_receipt"], aliases["authority_manifest"], aliases["authority"]}) != 1:
        _fail("OVERLAY_BINDING", "fresh authority digest aliases differ")

    validated_hours = _validate_overlay_hours(manifest, authority, date_text)
    analysis_objects = sorted(
        ({
            "key": obj["key"], "version_id": obj["version_id"],
            "size": obj["size"], "sha256": obj["sha256"],
        } for hour in validated_hours[:24] for obj in hour["rfq_objects"]),
        key=lambda row: row["key"],
    )
    if len({row["key"] for row in analysis_objects}) != len(analysis_objects):
        _fail("OVERLAY_BINDING", "analysis object keys are duplicated")
    object_sha = canonical_sha256(analysis_objects)
    if (
        receipt["analysis_rfq_objects"] != analysis_objects
        or manifest.get("analysis_rfq_objects") != analysis_objects
        or receipt["analysis_rfq_object_set_sha256"] != object_sha
        or manifest.get("analysis_rfq_object_set_sha256") != object_sha
    ):
        _fail("OVERLAY_BINDING", "exact analysis object set differs from hours")
    source_evidence = manifest.get("source_evidence")
    if (
        not isinstance(source_evidence, dict)
        or source_evidence.get("generation") != authority["generation"]
        or source_evidence.get("analysis_date") != date_text
        or source_evidence.get("authority_sha256") != authority["authority_sha256"]
        or source_evidence.get("evidence_sha256") != receipt["source_evidence_sha256"]
        or source_evidence.get("evidence_sha256") != manifest.get("source_evidence_sha256")
    ):
        _fail("OVERLAY_BINDING", "source evidence differs from fresh authority/date")
    if manifest.get("time_contract_sha256") != canonical_sha256(manifest.get("time_contract")):
        _fail("CLOCK_GATE", "time contract digest mismatch")
    if manifest.get("base_binding_sha256") != receipt["base_binding_sha256"]:
        _fail("OVERLAY_BINDING", "base-binding digest aliases differ")

    embedded_request = manifest.get("request_provenance")
    embedded_mapping = manifest.get("market_mapping")
    embedded_universe = manifest.get("universe_provenance")
    if not all(isinstance(row, dict) for row in (
        embedded_request, embedded_mapping, embedded_universe,
    )):
        _fail("OVERLAY_PROVENANCE", "embedded provenance is absent")
    if (
        embedded_request.get("receipt_sha256")
        != manifest.get("request_provenance_sha256")
        or embedded_mapping.get("mapping_sha256")
        != manifest.get("market_mapping_sha256")
        or embedded_universe.get("provenance_sha256")
        != manifest.get("universe_provenance_sha256")
    ):
        _fail("OVERLAY_PROVENANCE", "embedded provenance digest aliases differ")
    _verify_self_digest(embedded_request, "receipt_sha256", "request provenance")
    _verify_self_digest(embedded_mapping, "mapping_sha256", "market mapping")
    _verify_self_digest(
        embedded_universe, "provenance_sha256", "universe provenance",
    )
    embedded_base = _validate_embedded_base(
        ready_doc=ready_doc,
        receipt=receipt,
        manifest=manifest,
        date_text=date_text,
    )
    try:
        families = embedded_universe["families"]
        for family_name in ("orderbooks_l1", "orderbooks_full"):
            family = families[family_name]
            if family["market_universe_sha256"] != canonical_sha256(
                family["market_universe"]
            ):
                _fail(
                    "OVERLAY_PROVENANCE",
                    f"{family_name} market-universe digest differs",
                )
    except (KeyError, TypeError) as exc:
        _fail("OVERLAY_PROVENANCE", f"universe family binding absent: {exc}")
    request_bindings = {
        "analysis_date": date_text,
        "authority_sha256": authority["authority_sha256"],
        "source_evidence_sha256": source_evidence["evidence_sha256"],
        "time_contract_sha256": manifest["time_contract_sha256"],
        "analysis_rfq_object_set_sha256": object_sha,
    }
    if any(embedded_request.get(key) != expected for key, expected in request_bindings.items()):
        _fail("OVERLAY_PROVENANCE", "request provenance input binding differs")
    mapping_bindings = {
        "analysis_date": date_text,
        "base_binding_sha256": manifest["base_binding_sha256"],
        "analysis_rfq_object_set_sha256": object_sha,
        "time_contract_sha256": manifest["time_contract_sha256"],
    }
    if any(embedded_mapping.get(key) != expected for key, expected in mapping_bindings.items()):
        _fail("OVERLAY_PROVENANCE", "market mapping input binding differs")

    return {
        "date": date_text,
        "ready_path": str(ready),
        "ready_sha256": ready_doc["ready_sha256"],
        "overlay_receipt_sha256": receipt["overlay_receipt_sha256"],
        "overlay_manifest_sha256": manifest["manifest_sha256"],
        "authority_sha256": authority["authority_sha256"],
        "generation": authority["generation"],
        "source_evidence_sha256": source_evidence["evidence_sha256"],
        "time_contract_sha256": manifest["time_contract_sha256"],
        "base_binding_sha256": manifest["base_binding_sha256"],
        "base_terminal_file_sha256": receipt["base_terminal_file_sha256"],
        "base_manifest_exact_identity": copy.deepcopy(
            receipt["base_manifest_exact_identity"]
        ),
        "base_binding": embedded_base,
        "source_gate_sha256": canonical_sha256({
            "analysis_hour_receipt_set_sha256": manifest[
                "analysis_hour_receipt_set_sha256"
            ],
            "time_contract_sha256": manifest["time_contract_sha256"],
            "authority_sha256": authority["authority_sha256"],
        }),
        "analysis_rfq_objects": analysis_objects,
        "exact_objects": [
            {"bucket": exact_reader.SOURCE_BUCKET, **copy.deepcopy(row)}
            for row in analysis_objects
        ],
        "manifest": manifest,
    }


def _parse_utc_us(value: Any, label: str) -> int:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        _fail("EVENT_TIMESTAMP_INVALID", f"{label} must be canonical UTC Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        _fail("EVENT_TIMESTAMP_INVALID", f"{label}: {exc}")
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = parsed - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000
            + delta.microseconds)


def _fixed_exact(value: Any, label: str, places: int) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"\d+(?:\.\d+)?", value) is None:
        _fail("EVENT_FIXED_POINT_INVALID", f"{label} is not fixed-point text")
    try:
        scaled = Decimal(value) * (Decimal(10) ** places)
    except InvalidOperation as exc:
        _fail("EVENT_FIXED_POINT_INVALID", f"{label}: {exc}")
    if scaled != scaled.to_integral_value():
        _fail("EVENT_FIXED_POINT_INVALID", f"{label} is finer than E{places}")
    result = int(scaled)
    if not -(1 << 63) <= result < (1 << 63):
        _fail("EVENT_FIXED_POINT_INVALID", f"{label} exceeds int64")
    return result


def _creator_hash(value: Any, label: str) -> str | None:
    if not isinstance(value, str) or "\x00" in value or value != value.strip():
        _fail("EVENT_CREATOR_INVALID", f"{label} must be canonical text")
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _event_bucket(rfq_id: str, buckets: int) -> int:
    digest = hashlib.sha256(rfq_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % buckets


def _parse_event(
    frame: dict[str, Any], outer: dict[str, Any], *, analysis_date: str,
    source_key: str, source_version_id: str, line_number: int,
) -> dict[str, Any]:
    event_type = frame.get("type")
    if event_type not in {"rfq_created", "rfq_deleted"}:
        _fail("EVENT_TYPE_INVALID", f"unsupported RFQ event {event_type!r}")
    sid = frame.get("sid")
    if type(sid) is not int or sid <= 0:
        _fail("EVENT_SCHEMA_INVALID", "RFQ event sid must be positive")
    msg = frame.get("msg")
    if not isinstance(msg, dict):
        _fail("EVENT_SCHEMA_INVALID", "RFQ event msg must be an object")
    timestamp_field = "created_ts" if event_type == "rfq_created" else "deleted_ts"
    for field in ("id", "creator_id", "market_ticker", timestamp_field):
        if not isinstance(msg.get(field), str):
            _fail("EVENT_SCHEMA_INVALID", f"RFQ msg.{field} is required text")
    try:
        rfq_id = request_provenance._identifier(msg["id"], "msg.id")
        market_ticker = request_provenance._identifier(
            msg["market_ticker"], "msg.market_ticker",
        )
    except request_provenance.FreshRfqRequestProvenanceError as exc:
        _fail("EVENT_SCHEMA_INVALID", f"{exc.code}: {exc.detail}")
    exchange_ts_us = _parse_utc_us(msg[timestamp_field], f"msg.{timestamp_field}")
    if event_type == "rfq_created" and not msg[timestamp_field].startswith(analysis_date + "T"):
        _fail("EVENT_DATE_MISMATCH", "create timestamp is outside overlay date")
    contracts_e2 = _fixed_exact(msg.get("contracts_fp"), "contracts_fp", 2)
    target_cost_e6 = _fixed_exact(
        msg.get("target_cost_dollars"), "target_cost_dollars", 6,
    )
    creator_hash = _creator_hash(msg["creator_id"], "creator_id")

    collection = msg.get("mve_collection_ticker")
    legs: list[dict[str, Any]] = []
    if event_type == "rfq_created":
        try:
            projected = request_provenance._project_rfq_created(
                frame, analysis_date, f"{source_key} line {line_number}",
            )
        except request_provenance.FreshRfqRequestProvenanceError as exc:
            _fail("EVENT_SCHEMA_INVALID", f"{exc.code}: {exc.detail}")
        raw_legs = msg.get("mve_selected_legs") or []
        for index, raw_leg in enumerate(raw_legs):
            try:
                ticker = request_provenance._identifier(
                    raw_leg.get("market_ticker"),
                    f"mve_selected_legs[{index}].market_ticker",
                )
            except request_provenance.FreshRfqRequestProvenanceError as exc:
                _fail("EVENT_SCHEMA_INVALID", f"{exc.code}: {exc.detail}")
            side = raw_leg.get("side")
            if side is not None and not isinstance(side, str):
                _fail("EVENT_SCHEMA_INVALID", f"combo leg {index} side is not text")
            normalized_side = side.lower() if isinstance(side, str) else None
            if normalized_side not in {None, "yes", "no"}:
                normalized_side = "unknown"
            legs.append({
                "market_ticker": ticker,
                "side": normalized_side,
                "yes_settlement_value_e6": _fixed_exact(
                    raw_leg.get("yes_settlement_value_dollars"),
                    f"mve_selected_legs[{index}].yes_settlement_value_dollars",
                    6,
                ),
            })
        collection = projected["mve_collection_ticker"]
        if sorted(row["market_ticker"] for row in legs) != sorted(
            row["market_ticker"] for row in projected["mve_selected_legs"]
        ):
            _fail("EVENT_SCHEMA_INVALID", "combo leg projection differs")
    elif collection is not None:
        _fail("EVENT_SCHEMA_INVALID", "rfq_deleted unexpectedly carries combo collection")

    legs.sort(key=lambda row: (row["market_ticker"], str(row["side"])))
    yes_legs = sum(row["side"] == "yes" for row in legs)
    no_legs = sum(row["side"] == "no" for row in legs)
    unknown_legs = len(legs) - yes_legs - no_legs
    raw_sha = hashlib.sha256(outer["raw"].encode("utf-8")).hexdigest()
    economic = {
        "event_type": event_type,
        "rfq_id": rfq_id,
        "market_ticker": market_ticker,
        "exchange_ts_us": exchange_ts_us,
        "contracts_e2": contracts_e2,
        "target_cost_e6": target_cost_e6,
        "collection": collection,
        "legs": legs,
        "creator_hash": creator_hash,
    }
    return {
        "analysis_date": analysis_date,
        "event_type": "CREATE" if event_type == "rfq_created" else "DELETE",
        "rfq_id": rfq_id,
        "rfq_id_sha256": hashlib.sha256(rfq_id.encode("utf-8")).hexdigest(),
        "exchange_ts_us": exchange_ts_us,
        "recv_wall_ns": outer["recv_wall_ns"],
        "clock_skew_ms": outer["recv_wall_ns"] // 1_000_000 - exchange_ts_us // 1_000,
        "market_ticker": market_ticker,
        "creator_hash": creator_hash,
        "contracts_e2": contracts_e2,
        "target_cost_e6": target_cost_e6,
        "mve_collection_ticker": collection,
        "legs_json": canonical_bytes(legs).decode("ascii"),
        "leg_count": len(legs),
        "yes_leg_count": yes_legs,
        "no_leg_count": no_legs,
        "unknown_side_leg_count": unknown_legs,
        "raw_sha256": raw_sha,
        "economic_sha256": canonical_sha256(economic),
        "source_key": source_key,
        "source_version_id": source_version_id,
        "source_line": line_number,
    }


class _HashObserver:
    """Stream one date into narrow full-ID hash buckets."""

    def __init__(self, root: Path, analysis_date: str, buckets: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.analysis_date = analysis_date
        self.buckets = buckets
        self.paths = [self.root / f"bucket-{value:02d}.ndjson" for value in range(buckets)]
        self.handles = [path.open("xb") for path in self.paths]
        self.closed = False
        self.physical_lines = 0
        self.marker_rows = 0
        self.created_rows = 0
        self.deleted_rows = 0
        self.excluded_counts: dict[str, int] = {}
        self.bucket_counts = [0 for _ in range(buckets)]

    def close(self) -> None:
        if self.closed:
            return
        for handle in self.handles:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        self.closed = True

    def consume(self, identity: Mapping[str, Any], path: Path) -> None:
        if self.closed:
            _fail("OBSERVER_STATE", "observer is already closed")
        expected_hour = identity["key"].split("/rfq_", 1)[1][:2]
        hour_text = f"{self.analysis_date}T{expected_hour}"
        with Path(path).open("rb") as stream:
            line_number = 0
            while True:
                physical = stream.readline(MAX_JSON_LINE_BYTES + 1)
                if not physical:
                    break
                line_number += 1
                self.physical_lines += 1
                if len(physical) > MAX_JSON_LINE_BYTES or not physical.endswith(b"\n"):
                    _fail("OBSERVER_LINE_INVALID", f"{identity['key']} line {line_number}")
                payload = physical[:-1]
                try:
                    outer = request_provenance._normalize_outer(
                        request_provenance._strict_json_bytes(
                            payload, f"{identity['key']} line {line_number}",
                        ),
                        hour_text,
                        f"{identity['key']} line {line_number}",
                    )
                except request_provenance.FreshRfqRequestProvenanceError as exc:
                    _fail("OBSERVER_PARSE_INVALID", f"{exc.code}: {exc.detail}")
                if "marker" in outer:
                    self.marker_rows += 1
                    continue
                try:
                    frame = request_provenance._parse_inner_frame(
                        outer, f"{identity['key']} line {line_number}",
                    )
                except request_provenance.FreshRfqRequestProvenanceError as exc:
                    _fail("OBSERVER_PARSE_INVALID", f"{exc.code}: {exc.detail}")
                frame_type = frame["type"]
                if frame_type not in {"rfq_created", "rfq_deleted"}:
                    self.excluded_counts[frame_type] = self.excluded_counts.get(frame_type, 0) + 1
                    continue
                event = _parse_event(
                    frame, outer, analysis_date=self.analysis_date,
                    source_key=identity["key"],
                    source_version_id=identity["version_id"],
                    line_number=line_number,
                )
                bucket = _event_bucket(event["rfq_id"], self.buckets)
                self.handles[bucket].write(canonical_bytes(event) + b"\n")
                self.bucket_counts[bucket] += 1
                if event["event_type"] == "CREATE":
                    self.created_rows += 1
                else:
                    self.deleted_rows += 1

    def result(self) -> dict[str, Any]:
        self.close()
        result = {
            "schema": "deep03-fresh-rfq-observer-v1",
            "analysis_date": self.analysis_date,
            "physical_line_count": self.physical_lines,
            "marker_row_count": self.marker_rows,
            "rfq_created_occurrence_count": self.created_rows,
            "rfq_deleted_occurrence_count": self.deleted_rows,
            "excluded_frame_type_counts": [
                {"frame_type": key, "count": self.excluded_counts[key]}
                for key in sorted(self.excluded_counts)
            ],
            "bucket_row_counts": [
                {"bucket": index, "row_count": count}
                for index, count in enumerate(self.bucket_counts)
            ],
        }
        result["observer_sha256"] = canonical_sha256(result)
        return result


@contextmanager
def _observed_open_exact(
    session: exact_reader.ExactReadSession,
    observer: _HashObserver,
    identity: dict[str, Any],
) -> Iterator[exact_reader.VerifiedExactObject]:
    """Attach the narrow observer before ExactReadSession removes its file."""
    with session.open_exact(identity) as opened:
        yield opened
        observer.consume(opened.identity, opened.path)


EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("analysis_date", "VARCHAR"), ("event_type", "VARCHAR"),
    ("rfq_id", "VARCHAR"), ("rfq_id_sha256", "VARCHAR"),
    ("exchange_ts_us", "BIGINT"), ("recv_wall_ns", "BIGINT"),
    ("clock_skew_ms", "BIGINT"), ("market_ticker", "VARCHAR"),
    ("creator_hash", "VARCHAR"), ("contracts_e2", "BIGINT"),
    ("target_cost_e6", "BIGINT"), ("mve_collection_ticker", "VARCHAR"),
    ("legs_json", "VARCHAR"), ("leg_count", "INTEGER"),
    ("yes_leg_count", "INTEGER"), ("no_leg_count", "INTEGER"),
    ("unknown_side_leg_count", "INTEGER"), ("raw_sha256", "VARCHAR"),
    ("economic_sha256", "VARCHAR"), ("source_key", "VARCHAR"),
    ("source_version_id", "VARCHAR"), ("source_line", "BIGINT"),
)
MAPPING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("analysis_date", "VARCHAR"), ("request_id", "VARCHAR"),
    ("created_ts", "VARCHAR"), ("request_kind", "VARCHAR"),
    ("component_index", "INTEGER"), ("source_field", "VARCHAR"),
    ("market_ticker", "VARCHAR"), ("l1_present", "BOOLEAN"),
    ("l2_present", "BOOLEAN"), ("mapping_state", "VARCHAR"),
    ("event_window_within_base_date", "BOOLEAN"),
    ("mapping_sha256", "VARCHAR"), ("source_gate_sha256", "VARCHAR"),
)


def _module_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _stage_version(label: str) -> str:
    if SAFE_STAGE_RE.fullmatch(label) is None:
        _fail("STAGE_VERSION_INVALID", label)
    return f"{label}-{_module_sha256()[:16]}"


def _source_binding(descriptors: list[dict[str, Any]], buckets: int) -> str:
    return canonical_sha256({
        "schema": "deep03-fresh-rfq-source-binding-v1",
        "hash_algorithm": "SHA256_FULL_RFQ_ID_FIRST_U64_BE_MOD_N",
        "hash_buckets": buckets,
        "days": [{
            key: row[key]
            for key in (
                "date", "ready_sha256", "overlay_receipt_sha256",
                "overlay_manifest_sha256", "authority_sha256", "generation",
                "source_evidence_sha256", "time_contract_sha256",
                "base_binding_sha256", "source_gate_sha256",
            )
        } | {
            "analysis_rfq_object_set_sha256": canonical_sha256(
                row["analysis_rfq_objects"]
            ),
        } for row in descriptors],
    })


def _empty_select(columns: tuple[tuple[str, str], ...]) -> str:
    return "SELECT " + ",".join(
        f"CAST(NULL AS {kind}) AS {name}" for name, kind in columns
    ) + " WHERE false"


def _json_select(path: Path, columns: tuple[tuple[str, str], ...]) -> str:
    if path.stat().st_size == 0:
        return _empty_select(columns)
    projection = ",".join(
        f"CAST({name} AS {kind}) AS {name}" for name, kind in columns
    )
    return (
        f"SELECT {projection} FROM read_json_auto({quote(path)},"
        "format='newline_delimited',maximum_object_size=16777216)"
    )


def _checkpoint_path(
    store: BoundedCheckpointStore, stage: str, partition_key: str,
) -> Path:
    return store.root / stage / "data" / f"{partition_key}.parquet"


def _partition_key(date_text: str, bucket: int) -> str:
    return f"d{date_text.replace('-', '')}_h{bucket:02d}"


def _date_meta_key(date_text: str) -> str:
    return f"d{date_text.replace('-', '')}"


def _mapping_inputs(descriptor: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    manifest = descriptor["manifest"]
    try:
        families = manifest["universe_provenance"]["families"]
        l1 = families["orderbooks_l1"]["market_universe"]
        l2 = families["orderbooks_full"]["market_universe"]
        embedded = manifest["market_mapping"]
        pre = embedded["pre_event_window_ms"]
        post = embedded["post_event_window_ms"]
    except (KeyError, TypeError) as exc:
        _fail("OVERLAY_PROVENANCE", f"mapping input absent: {exc}")
    if not isinstance(l1, list) or not isinstance(l2, list):
        _fail("OVERLAY_PROVENANCE", "market universes must be lists")
    return copy.deepcopy(l1), copy.deepcopy(l2), pre, post


def _rebuild_mapping(
    descriptor: dict[str, Any], provenance: dict[str, Any],
) -> dict[str, Any]:
    l1, l2, pre, post = _mapping_inputs(descriptor)
    try:
        rebuilt = market_mapping.build_market_mapping(
            analysis_date=descriptor["date"],
            base_binding_sha256=descriptor["base_binding_sha256"],
            analysis_rfq_object_set_sha256=canonical_sha256(
                descriptor["analysis_rfq_objects"]
            ),
            time_contract_sha256=descriptor["time_contract_sha256"],
            rfq_requests=provenance["rfq_requests"],
            l1_market_universe=l1,
            l2_market_universe=l2,
            pre_event_window_ms=pre,
            post_event_window_ms=post,
        )
    except market_mapping.FreshRfqMarketMappingError as exc:
        _fail("MARKET_MAPPING_INVALID", f"{exc.code}: {exc.detail}")
    embedded = descriptor["manifest"]["market_mapping"]
    if canonical_bytes(rebuilt) != canonical_bytes(embedded):
        _fail("MARKET_MAPPING_MISMATCH", "exact rebuild differs from overlay")
    if rebuilt["rfq_input_sha256"] != provenance["rfq_input_sha256"]:
        _fail("MARKET_MAPPING_MISMATCH", "request input digest differs")
    return rebuilt


def _observer_conservation(
    observer: dict[str, Any], provenance: dict[str, Any],
) -> None:
    expected_irrelevant = {
        row["frame_type"]: row["count"]
        for row in provenance["irrelevant_type_counts"]
    }
    observed_irrelevant = {
        row["frame_type"]: row["count"]
        for row in observer["excluded_frame_type_counts"]
    }
    observed_irrelevant["rfq_deleted"] = observer[
        "rfq_deleted_occurrence_count"
    ]
    gates = {
        "physical lines": (
            observer["physical_line_count"], provenance["physical_line_count"]
        ),
        "marker rows": (
            observer["marker_row_count"], provenance["marker_row_count"]
        ),
        "create occurrences": (
            observer["rfq_created_occurrence_count"],
            provenance["rfq_created_occurrence_count"],
        ),
        "irrelevant frame rows": (
            sum(observed_irrelevant.values()),
            provenance["irrelevant_frame_row_count"],
        ),
    }
    for label, (observed, expected) in gates.items():
        if observed != expected:
            _fail("CONSERVATION_FAILED", f"{label}: {observed} != {expected}")
    if observed_irrelevant != expected_irrelevant:
        _fail("CONSERVATION_FAILED", "irrelevant frame type counts differ")
    if observer["physical_line_count"] != (
        observer["marker_row_count"]
        + observer["rfq_created_occurrence_count"]
        + observer["rfq_deleted_occurrence_count"]
        + sum(row["count"] for row in observer["excluded_frame_type_counts"])
    ):
        _fail("CONSERVATION_FAILED", "observer classifications do not conserve")


def _write_mapping_files(
    root: Path, descriptor: dict[str, Any], mapping: dict[str, Any], buckets: int,
) -> tuple[list[Path], list[int]]:
    paths = [root / f"mapping-{value:02d}.ndjson" for value in range(buckets)]
    handles = [path.open("xb") for path in paths]
    counts = [0 for _ in range(buckets)]
    try:
        for row in mapping["mapping_rows"]:
            bucket = _event_bucket(row["request_id"], buckets)
            payload = {
                "analysis_date": descriptor["date"],
                **copy.deepcopy(row),
                "mapping_sha256": mapping["mapping_sha256"],
                "source_gate_sha256": descriptor["source_gate_sha256"],
            }
            handles[bucket].write(canonical_bytes(payload) + b"\n")
            counts[bucket] += 1
        for handle in handles:
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        for handle in handles:
            handle.close()
    if sum(counts) != mapping["mapping_input_ticker_count"]:
        _fail("CONSERVATION_FAILED", "mapping bucket rows do not conserve")
    return paths, counts


def _publish_meta(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptor: dict[str, Any],
    provenance: dict[str, Any],
    mapping: dict[str, Any],
    observer: dict[str, Any],
    attestation: dict[str, Any],
    stage_version: str,
) -> dict[str, Any]:
    value = {
        "schema": "deep03-fresh-rfq-source-meta-v1",
        "date": descriptor["date"],
        "overlay_manifest_sha256": descriptor["overlay_manifest_sha256"],
        "source_gate_sha256": descriptor["source_gate_sha256"],
        "provenance": provenance,
        "mapping": mapping,
        "observer": observer,
        "exact_reader_attestation": attestation,
    }
    value["meta_sha256"] = canonical_sha256(value)
    payload = canonical_bytes(value).decode("ascii")
    key = _date_meta_key(descriptor["date"])
    receipt, _reused = store.write_partition(
        con,
        stage=SOURCE_META_STAGE,
        stage_version=stage_version,
        partition_key=key,
        select_sql=(
            f"SELECT {quote(descriptor['date'])}::VARCHAR AS analysis_date,"
            f"{quote(payload)}::VARCHAR AS meta_json,"
            f"{quote(value['meta_sha256'])}::VARCHAR AS meta_sha256"
        ),
        metrics={
            "analysis_rfq_object_count": len(descriptor["analysis_rfq_objects"]),
            "analysis_rfq_total_bytes": sum(
                row["size"] for row in descriptor["analysis_rfq_objects"]
            ),
            "physical_line_count": observer["physical_line_count"],
            "source_event_row_count": (
                observer["rfq_created_occurrence_count"]
                + observer["rfq_deleted_occurrence_count"]
            ),
        },
    )
    return receipt


def _load_meta(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptor: dict[str, Any],
    stage_version: str,
) -> dict[str, Any]:
    key = _date_meta_key(descriptor["date"])
    store.validate_partition(
        con, stage=SOURCE_META_STAGE, stage_version=stage_version,
        partition_key=key,
    )
    path = _checkpoint_path(store, SOURCE_META_STAGE, key)
    row = con.execute(
        f"SELECT analysis_date,meta_json,meta_sha256 FROM read_parquet({quote(path)})"
    ).fetchone()
    if row is None or row[0] != descriptor["date"]:
        _fail("CHECKPOINT_META_INVALID", f"meta date differs for {descriptor['date']}")
    try:
        value = json.loads(row[1])
    except (TypeError, ValueError) as exc:
        _fail("CHECKPOINT_META_INVALID", str(exc))
    if row[2] != value.get("meta_sha256"):
        _fail("CHECKPOINT_META_INVALID", "meta digest alias differs")
    _verify_self_digest(value, "meta_sha256", "checkpoint meta")
    if (
        value.get("date") != descriptor["date"]
        or value.get("overlay_manifest_sha256")
        != descriptor["overlay_manifest_sha256"]
        or value.get("source_gate_sha256") != descriptor["source_gate_sha256"]
    ):
        _fail("CHECKPOINT_META_INVALID", "meta input binding differs")
    _observer_conservation(value["observer"], value["provenance"])
    rebuilt = _rebuild_mapping(descriptor, value["provenance"])
    if canonical_bytes(rebuilt) != canonical_bytes(value["mapping"]):
        _fail("CHECKPOINT_META_INVALID", "stored mapping differs from rebuild")
    return value


def _date_is_reusable(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptor: dict[str, Any],
    buckets: int,
    versions: dict[str, str],
) -> bool:
    keys = [_partition_key(descriptor["date"], bucket) for bucket in range(buckets)]
    meta_key = _date_meta_key(descriptor["date"])
    candidates = [
        (SOURCE_META_STAGE, versions[SOURCE_META_STAGE], meta_key),
        *((SOURCE_EVENT_STAGE, versions[SOURCE_EVENT_STAGE], key) for key in keys),
        *((MAPPING_STAGE, versions[MAPPING_STAGE], key) for key in keys),
    ]
    if any(not store._paths(stage, key)[1].exists() for stage, _version, key in candidates):
        return False
    for stage, version, key in candidates:
        store.validate_partition(
            con, stage=stage, stage_version=version, partition_key=key,
        )
    return True


def _materialize_day(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptor: dict[str, Any],
    *, client_factory: Callable[[dict[str, Any]], Any],
    transport_kind: str,
    buckets: int,
    versions: dict[str, str],
    temp_parent: Path | None,
) -> dict[str, Any]:
    if _date_is_reusable(con, store, descriptor, buckets, versions):
        return _load_meta(
            con, store, descriptor, versions[SOURCE_META_STAGE],
        )
    client = client_factory(copy.deepcopy(descriptor))
    if client is None:
        _fail("EXACT_CLIENT_REQUIRED", f"no exact client for {descriptor['date']}")
    scratch_parent = store.root / ".partial"
    observer_root = scratch_parent / f"rfq-observe-{uuid.uuid4().hex}"
    observer = _HashObserver(observer_root, descriptor["date"], buckets)
    session = exact_reader.ExactReadSession(
        descriptor["exact_objects"], client,
        transport_kind=transport_kind,
        temp_parent=temp_parent,
    )
    try:
        with session:
            try:
                provenance = request_provenance.build_request_provenance_from_reader(
                    analysis_date=descriptor["date"],
                    authority_sha256=descriptor["authority_sha256"],
                    source_evidence_sha256=descriptor["source_evidence_sha256"],
                    time_contract_sha256=descriptor["time_contract_sha256"],
                    analysis_rfq_objects=descriptor["analysis_rfq_objects"],
                    exact_analysis_rfq_objects=descriptor["exact_objects"],
                    open_exact=lambda identity: _observed_open_exact(
                        session, observer, identity,
                    ),
                )
            except request_provenance.FreshRfqRequestProvenanceError as exc:
                _fail("REQUEST_PROVENANCE_INVALID", f"{exc.code}: {exc.detail}")
        attestation = session.attestation
        observed = observer.result()
        _observer_conservation(observed, provenance)
        embedded = descriptor["manifest"]["request_provenance"]
        if canonical_bytes(provenance) != canonical_bytes(embedded):
            _fail(
                "REQUEST_PROVENANCE_MISMATCH",
                "exact-reader rebuild differs from fresh overlay",
            )
        mapping = _rebuild_mapping(descriptor, provenance)
        mapping_paths, mapping_counts = _write_mapping_files(
            observer_root, descriptor, mapping, buckets,
        )
        for bucket in range(buckets):
            key = _partition_key(descriptor["date"], bucket)
            event_receipt, _ = store.write_partition(
                con,
                stage=SOURCE_EVENT_STAGE,
                stage_version=versions[SOURCE_EVENT_STAGE],
                partition_key=key,
                select_sql=_json_select(observer.paths[bucket], EVENT_COLUMNS),
                metrics={"observer_row_count": observed["bucket_row_counts"][bucket]["row_count"]},
            )
            if event_receipt["data"]["row_count"] != observed["bucket_row_counts"][bucket]["row_count"]:
                _fail("CONSERVATION_FAILED", f"event checkpoint bucket {bucket}")
            mapping_receipt, _ = store.write_partition(
                con,
                stage=MAPPING_STAGE,
                stage_version=versions[MAPPING_STAGE],
                partition_key=key,
                select_sql=_json_select(mapping_paths[bucket], MAPPING_COLUMNS),
                metrics={"mapping_row_count": mapping_counts[bucket]},
            )
            if mapping_receipt["data"]["row_count"] != mapping_counts[bucket]:
                _fail("CONSERVATION_FAILED", f"mapping checkpoint bucket {bucket}")
        _publish_meta(
            con, store, descriptor, provenance, mapping, observed, attestation,
            versions[SOURCE_META_STAGE],
        )
        return {
            "schema": "deep03-fresh-rfq-source-meta-v1",
            "date": descriptor["date"],
            "overlay_manifest_sha256": descriptor["overlay_manifest_sha256"],
            "source_gate_sha256": descriptor["source_gate_sha256"],
            "provenance": provenance,
            "mapping": mapping,
            "observer": observed,
            "exact_reader_attestation": attestation,
        }
    finally:
        observer.close()
        shutil.rmtree(observer_root, ignore_errors=True)


def _lifecycle_sql(event_paths: list[Path], analysis_end_us: int) -> str:
    relation = f"read_parquet({path_list(event_paths)},union_by_name=true,hive_partitioning=false)"
    return f"""
WITH source AS (
  SELECT * FROM {relation}
),
create_stats AS (
  SELECT rfq_id,
         count(*)::BIGINT AS create_occurrence_count,
         count(DISTINCT raw_sha256)::BIGINT AS create_distinct_raw_count,
         count(DISTINCT economic_sha256)::BIGINT AS create_economic_variant_count
  FROM source WHERE event_type='CREATE' GROUP BY rfq_id
),
valid_create_ids AS (
  SELECT rfq_id FROM create_stats WHERE create_economic_variant_count=1
),
creates AS (
  SELECT s.*
  FROM source s JOIN valid_create_ids v USING (rfq_id)
  WHERE s.event_type='CREATE'
  QUALIFY row_number() OVER (
    PARTITION BY s.rfq_id
    ORDER BY s.recv_wall_ns,s.source_key,s.source_version_id,s.source_line
  )=1
),
delete_stats AS (
  SELECT rfq_id, count(*)::BIGINT AS delete_occurrence_count,
         count(DISTINCT raw_sha256)::BIGINT AS delete_distinct_raw_count
  FROM source WHERE event_type='DELETE' GROUP BY rfq_id
),
deletes AS (
  SELECT * FROM source WHERE event_type='DELETE'
  QUALIFY row_number() OVER (
    PARTITION BY rfq_id,raw_sha256
    ORDER BY recv_wall_ns,source_key,source_version_id,source_line
  )=1
),
delete_candidates AS (
  SELECT c.rfq_id,
         d.exchange_ts_us AS deleted_ts_us,
         d.recv_wall_ns AS delete_recv_wall_ns,
         d.creator_hash AS delete_creator_hash,
         d.source_key AS delete_source_key,
         d.source_line AS delete_source_line,
         CASE WHEN d.market_ticker=c.market_ticker
                    AND d.exchange_ts_us>=c.exchange_ts_us
                    AND d.exchange_ts_us<{analysis_end_us}
                    AND (d.contracts_e2 IS NULL OR c.contracts_e2 IS NULL
                         OR d.contracts_e2=c.contracts_e2)
                    AND (d.target_cost_e6 IS NULL OR c.target_cost_e6 IS NULL
                         OR d.target_cost_e6=c.target_cost_e6)
              THEN true ELSE false END AS consistent
  FROM creates c JOIN deletes d USING (rfq_id)
),
chosen_delete AS (
  SELECT * FROM delete_candidates WHERE consistent
  QUALIFY row_number() OVER (
    PARTITION BY rfq_id
    ORDER BY deleted_ts_us,delete_recv_wall_ns,delete_source_key,delete_source_line
  )=1
),
delete_quality AS (
  SELECT rfq_id,
         count(*) FILTER (WHERE NOT consistent)::BIGINT AS inconsistent_delete_count,
         count(*) FILTER (WHERE consistent)::BIGINT AS consistent_delete_count
  FROM delete_candidates GROUP BY rfq_id
)
SELECT
  c.analysis_date,
  c.rfq_id,
  c.rfq_id_sha256,
  c.exchange_ts_us AS created_ts_us,
  c.recv_wall_ns AS create_recv_wall_ns,
  c.clock_skew_ms AS create_clock_skew_ms,
  c.market_ticker,
  c.contracts_e2,
  c.target_cost_e6,
  c.mve_collection_ticker,
  c.legs_json,
  c.leg_count,
  c.yes_leg_count,
  c.no_leg_count,
  c.unknown_side_leg_count,
  coalesce(cd.delete_creator_hash,c.creator_hash) AS requester_hash,
  (cd.rfq_id IS NOT NULL) AS deletion_observed,
  cd.deleted_ts_us,
  CASE WHEN cd.rfq_id IS NULL THEN NULL
       ELSE ((cd.deleted_ts_us-c.exchange_ts_us)/1000)::BIGINT END AS lifetime_ms,
  CASE WHEN cd.rfq_id IS NULL
       THEN greatest(0,({analysis_end_us}-c.exchange_ts_us)/1000)::BIGINT
       ELSE ((cd.deleted_ts_us-c.exchange_ts_us)/1000)::BIGINT END AS observed_duration_ms,
  ({analysis_end_us})::BIGINT AS censor_end_us,
  (cs.create_occurrence_count-1)::BIGINT AS exact_create_duplicate_count,
  coalesce(ds.delete_occurrence_count-ds.delete_distinct_raw_count,0)::BIGINT
    AS exact_delete_duplicate_count,
  coalesce(dq.inconsistent_delete_count,0)::BIGINT AS inconsistent_delete_count,
  coalesce(dq.consistent_delete_count,0)::BIGINT AS consistent_delete_count
FROM creates c
JOIN create_stats cs USING (rfq_id)
LEFT JOIN delete_stats ds USING (rfq_id)
LEFT JOIN delete_quality dq USING (rfq_id)
LEFT JOIN chosen_delete cd USING (rfq_id)
"""


def _materialize_lifecycle(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptors: list[dict[str, Any]],
    buckets: int,
    stage_version: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    last = dt.date.fromisoformat(descriptors[-1]["date"]) + dt.timedelta(days=1)
    analysis_end_us = int(dt.datetime.combine(
        last, dt.time(), tzinfo=dt.timezone.utc,
    ).timestamp() * 1_000_000)
    receipts = []
    totals = {
        "create_occurrences": 0, "delete_occurrences": 0,
        "unique_create_ids": 0, "conflicting_create_ids": 0,
        "orphan_delete_ids": 0, "lifecycle_rows": 0,
    }
    for bucket in range(buckets):
        event_paths = [
            _checkpoint_path(
                store, SOURCE_EVENT_STAGE, _partition_key(row["date"], bucket)
            )
            for row in descriptors
        ]
        relation = f"read_parquet({path_list(event_paths)},union_by_name=true,hive_partitioning=false)"
        metrics_row = con.execute(f"""
          WITH source AS (SELECT * FROM {relation}),
          creates AS (
            SELECT rfq_id,count(*) n,count(DISTINCT economic_sha256) variants
            FROM source WHERE event_type='CREATE' GROUP BY rfq_id
          ),
          deletes AS (
            SELECT DISTINCT rfq_id FROM source WHERE event_type='DELETE'
          )
          SELECT
            count(*) FILTER (WHERE event_type='CREATE'),
            count(*) FILTER (WHERE event_type='DELETE'),
            (SELECT count(*) FROM creates),
            (SELECT count(*) FROM creates WHERE variants>1),
            (SELECT count(*) FROM deletes d LEFT JOIN creates c USING (rfq_id)
              WHERE c.rfq_id IS NULL)
          FROM source
        """).fetchone()
        metrics = {
            "create_occurrences": int(metrics_row[0]),
            "delete_occurrences": int(metrics_row[1]),
            "unique_create_ids": int(metrics_row[2]),
            "conflicting_create_ids": int(metrics_row[3]),
            "orphan_delete_ids": int(metrics_row[4]),
        }
        key = f"h{bucket:02d}"
        receipt, _ = store.write_partition(
            con,
            stage=LIFECYCLE_STAGE,
            stage_version=stage_version,
            partition_key=key,
            select_sql=_lifecycle_sql(event_paths, analysis_end_us),
            metrics=metrics,
        )
        expected_rows = metrics["unique_create_ids"] - metrics["conflicting_create_ids"]
        if receipt["data"]["row_count"] != expected_rows:
            _fail("CONSERVATION_FAILED", f"lifecycle bucket {bucket}")
        metrics["lifecycle_rows"] = receipt["data"]["row_count"]
        for name in totals:
            totals[name] += metrics[name]
        receipts.append(receipt)
    if totals["unique_create_ids"] != (
        totals["lifecycle_rows"] + totals["conflicting_create_ids"]
    ):
        _fail("CONSERVATION_FAILED", "global lifecycle rows do not conserve")
    return receipts, totals


def _bps(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    return (numerator * 10_000 + denominator // 2) // denominator


def _numeric_summary_sql(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    column: str,
    *, where: str = "true",
    unit: str,
) -> dict[str, Any]:
    row = con.execute(f"""
      SELECT count({column})::BIGINT,
             min({column})::BIGINT,
             quantile_disc({column},0.10)::BIGINT,
             quantile_disc({column},0.50)::BIGINT,
             quantile_disc({column},0.90)::BIGINT,
             quantile_disc({column},0.95)::BIGINT,
             quantile_disc({column},0.99)::BIGINT,
             max({column})::BIGINT
      FROM {relation} WHERE ({where}) AND {column} IS NOT NULL
    """).fetchone()
    return {
        "unit": unit,
        "quantile_method": "NEAREST_RANK_QUANTILE_DISC",
        "n": int(row[0]),
        "min": None if row[1] is None else int(row[1]),
        "p10": None if row[2] is None else int(row[2]),
        "p50": None if row[3] is None else int(row[3]),
        "p90": None if row[4] is None else int(row[4]),
        "p95": None if row[5] is None else int(row[5]),
        "p99": None if row[6] is None else int(row[6]),
        "max": None if row[7] is None else int(row[7]),
    }


def _python_numeric_summary(values: list[int], unit: str) -> dict[str, Any]:
    ordered = sorted(values)

    def nearest(percent: int) -> int | None:
        if not ordered:
            return None
        rank = max(1, (percent * len(ordered) + 99) // 100)
        return ordered[min(rank, len(ordered)) - 1]

    return {
        "unit": unit, "quantile_method": "NEAREST_RANK",
        "n": len(ordered), "min": ordered[0] if ordered else None,
        "p10": nearest(10), "p50": nearest(50), "p90": nearest(90),
        "p95": nearest(95), "p99": nearest(99),
        "max": ordered[-1] if ordered else None,
    }


def _validate_impact_adapter(
    value: Any,
    descriptor: dict[str, Any],
    mapping: dict[str, Any],
    *,
    exact_base: dict[str, Any],
    l2_quality_gate: dict[str, Any],
    rfq_events: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "schema", "state", "analysis_date", "mapping_sha256",
        "source_gate_sha256", "base_binding_sha256", "base_release_id",
        "base_manifest_exact_identity", "base_exact_object_set_sha256",
        "source_attestations", "source_attestation_set_sha256",
        "l2_quality_gate_sha256", "window_contract",
        "observation_count", "observations", "observations_sha256",
        "adapter_sha256",
    }
    value = _exact_keys(value, fields, "CLOB impact adapter")
    expected_sources = {
        family: _base_source_attestation(descriptor, family)
        for family in ("orderbooks_l1", "orderbooks_full")
    }
    expected_window = {
        "pre_event_window_ms": mapping["pre_event_window_ms"],
        "post_event_window_ms": mapping["post_event_window_ms"],
        "pre_interval": "[EVENT_MINUS_PRE,EVENT]",
        "post_interval": "(EVENT,EVENT_PLUS_POST]",
        "event_clock": "RFQ_EXCHANGE_CREATED_TS_US",
        "receive_clock": "RFQ_ENVELOPE_RECV_WALL_NS",
        "max_abs_exchange_receive_skew_us": MAX_RFQ_CLOCK_ABS_SKEW_US,
        "cross_date_borrow": False,
    }
    if (
        value["schema"] != IMPACT_SCHEMA
        or value["state"] != "COMPLETE"
        or value["analysis_date"] != descriptor["date"]
        or value["mapping_sha256"] != mapping["mapping_sha256"]
        or value["source_gate_sha256"] != descriptor["source_gate_sha256"]
        or value["base_binding_sha256"] != exact_base["binding_sha256"]
        or value["base_release_id"] != exact_base["release_id"]
        or value["base_manifest_exact_identity"]
        != exact_base["manifest_exact_identity"]
        or value["base_exact_object_set_sha256"]
        != exact_base["base_exact_set_sha256"]
        or canonical_bytes(value["source_attestations"])
        != canonical_bytes(expected_sources)
        or value["source_attestation_set_sha256"]
        != canonical_sha256(expected_sources)
        or value["l2_quality_gate_sha256"]
        != l2_quality_gate["gate_sha256"]
        or canonical_bytes(value["window_contract"])
        != canonical_bytes(expected_window)
    ):
        _fail("IMPACT_ADAPTER_BINDING", "adapter fixed binding differs")
    _verify_self_digest(value, "adapter_sha256", "CLOB impact adapter")
    observations = value["observations"]
    if not isinstance(observations, list):
        _fail("IMPACT_ADAPTER_SCHEMA", "observations must be a list")
    if (
        value["observation_count"] != len(observations)
        or value["observations_sha256"] != canonical_sha256(observations)
    ):
        _fail("IMPACT_ADAPTER_DIGEST", "observation count/digest differs")
    if len(observations) > MAX_D07_COMPONENTS_PER_DAY:
        _fail("IMPACT_ADAPTER_RESOURCE", "D07 component count exceeds hard bound")
    expected = {
        (row["request_id"], row["component_index"]): row
        for row in mapping["mapping_rows"]
    }
    if len(expected) != len(mapping["mapping_rows"]):
        _fail("IMPACT_ADAPTER_MAPPING", "mapping component keys are duplicated")
    l1_source_rows = expected_sources["orderbooks_l1"]["source_row_count"]
    l2_source_rows = expected_sources["orderbooks_full"]["source_row_count"]
    observed: dict[tuple[str, int], dict[str, Any]] = {}
    for index, raw in enumerate(observations):
        row = _exact_keys(
            raw, IMPACT_OBSERVATION_FIELDS, f"observations[{index}]",
        )
        key = (row["request_id"], row["component_index"])
        if key in observed:
            _fail("IMPACT_ADAPTER_DUPLICATE", f"duplicate observation {key}")
        expected_row = expected.get(key)
        if expected_row is None or row["market_ticker"] != expected_row["market_ticker"]:
            _fail("IMPACT_ADAPTER_MAPPING", f"observation is not exact mapping {key}")
        status = row["status"]
        if status not in IMPACT_STATUSES:
            _fail("IMPACT_ADAPTER_STATUS", f"invalid status for {key}")
        rfq_event = rfq_events.get(row["request_id"])
        if rfq_event is None:
            _fail("IMPACT_ADAPTER_RFQ_EVENT", f"missing exact RFQ event for {key}")
        expected_event_us = _parse_utc_us(
            expected_row["created_ts"], f"mapping event {key}",
        )
        expected_recv_ns = rfq_event["create_recv_wall_ns"]
        expected_clock_skew_us = expected_recv_ns // 1_000 - expected_event_us
        pre_us = mapping["pre_event_window_ms"] * 1_000
        post_us = mapping["post_event_window_ms"] * 1_000
        expected_clock_fields = {
            "event_ts_us": expected_event_us,
            "rfq_recv_wall_ns": expected_recv_ns,
            "rfq_clock_skew_us": expected_clock_skew_us,
            "pre_window_start_us": expected_event_us - pre_us,
            "pre_window_end_us": expected_event_us,
            "post_window_start_us": expected_event_us,
            "post_window_end_us": expected_event_us + post_us,
        }
        if any(row[field] != expected for field, expected in expected_clock_fields.items()):
            _fail("IMPACT_ADAPTER_CLOCK", f"event/window clock differs for {key}")
        if expected_row["mapping_state"] != "MAPPED_L1_L2":
            required_status = "UNMAPPED"
            required_reason = expected_row["mapping_state"]
        elif expected_row["event_window_within_base_date"] is not True:
            required_status = "CENSORED_BOUNDARY"
            required_reason = "EVENT_WINDOW_OUTSIDE_BASE_DATE"
        elif abs(expected_clock_skew_us) > MAX_RFQ_CLOCK_ABS_SKEW_US:
            required_status = "CENSORED_CLOCK"
            required_reason = (
                "FUTURE_EXCHANGE_CLOCK"
                if expected_clock_skew_us < 0
                else "STALE_EXCHANGE_CLOCK"
            )
        else:
            required_status = None
            required_reason = None
        if required_status is not None and status != required_status:
            _fail("IMPACT_ADAPTER_STATUS", f"{key} must be {required_status}")
        if required_status is None and status not in {
            "OBSERVED", "CENSORED_GAP", "CENSORED_CLOCK",
        }:
            _fail("IMPACT_ADAPTER_STATUS", f"mapped {key} has invalid censor state")
        if status == "CENSORED_CLOCK" and required_status is None:
            _fail(
                "IMPACT_ADAPTER_CLOCK",
                f"{key} cannot self-declare clock censoring inside tolerance",
            )
        expected_reason = {
            "OBSERVED": None,
            "CENSORED_GAP": "L2_SEQUENCE_GAP",
        }.get(status, required_reason)
        if row["censor_reason"] != expected_reason:
            _fail("IMPACT_ADAPTER_CENSOR", f"censor reason differs for {key}")
        numeric_fields = (
            "pre_mid_e6", "post_mid_e6", "pre_spread_e6", "post_spread_e6",
            "pre_depth_e2", "post_depth_e2",
        )
        observation_clock_fields = (
            "pre_observation_ts_us", "post_observation_ts_us",
        )
        count_fields = (
            "l1_pre_rows", "l1_post_rows", "l2_pre_rows", "l2_post_rows",
            "gap_rows",
        )
        for field in numeric_fields:
            if row[field] is not None and type(row[field]) is not int:
                _fail("IMPACT_ADAPTER_SCHEMA", f"{key}.{field} must be integer/null")
        for field in observation_clock_fields:
            if row[field] is not None and type(row[field]) is not int:
                _fail("IMPACT_ADAPTER_SCHEMA", f"{key}.{field} must be integer/null")
        for field in count_fields:
            if (
                type(row[field]) is not int
                or row[field] < 0
                or row[field] > MAX_D07_SOURCE_ROWS_PER_COMPONENT
            ):
                _fail("IMPACT_ADAPTER_SCHEMA", f"{key}.{field} must be nonnegative")
        if (
            row["l1_pre_rows"] + row["l1_post_rows"] > l1_source_rows
            or row["l2_pre_rows"] + row["l2_post_rows"] > l2_source_rows
        ):
            _fail("IMPACT_ADAPTER_RESOURCE", f"{key} row counts exceed exact source")
        if status == "OBSERVED":
            if (
                any(row[field] is None for field in numeric_fields)
                or any(row[field] is None for field in observation_clock_fields)
                or min(
                    row["l1_pre_rows"], row["l1_post_rows"],
                    row["l2_pre_rows"], row["l2_post_rows"],
                ) <= 0
                or row["gap_rows"] != 0
                or not row["pre_window_start_us"]
                <= row["pre_observation_ts_us"]
                <= row["event_ts_us"]
                or not row["event_ts_us"]
                < row["post_observation_ts_us"]
                <= row["post_window_end_us"]
                or not 0 <= row["pre_mid_e6"] <= 1_000_000
                or not 0 <= row["post_mid_e6"] <= 1_000_000
                or min(row["pre_spread_e6"], row["post_spread_e6"],
                       row["pre_depth_e2"], row["post_depth_e2"]) < 0
            ):
                _fail("IMPACT_ADAPTER_OBSERVATION", f"invalid observed row {key}")
        else:
            if any(
                row[field] is not None
                for field in numeric_fields + observation_clock_fields
            ):
                _fail("IMPACT_ADAPTER_CENSOR", f"censored {key} carries values")
            if status == "CENSORED_GAP" and row["gap_rows"] <= 0:
                _fail("IMPACT_ADAPTER_CENSOR", f"gap-censored {key} lacks gap")
            if status != "CENSORED_GAP" and any(row[field] for field in count_fields):
                _fail(
                    "IMPACT_ADAPTER_CENSOR",
                    f"non-gap censored {key} carries source row counts",
                )
        observed[key] = copy.deepcopy(row)
    if set(observed) != set(expected):
        _fail("IMPACT_ADAPTER_COVERAGE", "adapter does not enumerate exact mapping set")
    return copy.deepcopy(value)


def _d07_result(
    descriptors: list[dict[str, Any]],
    metas: list[dict[str, Any]],
    impact_adapters: Mapping[str, dict[str, Any]] | None,
    *,
    exact_bases: Mapping[str, dict[str, Any]] | None,
    l2_quality_gates: Mapping[str, dict[str, Any]] | None,
    rfq_events: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    contract = {
        "schema": IMPACT_SCHEMA,
        "exact_component_key": ["request_id", "component_index", "market_ticker"],
        "required_states": sorted(IMPACT_STATUSES),
        "price_scale": "E6_PROBABILITY_DOLLARS",
        "depth_scale": "E2_CONTRACTS",
        "all_mapping_components_required": True,
        "gap_and_clock_censoring_required": True,
        "exact_base_manifest_rebuild_required": True,
        "exact_l1_l2_source_attestation_required": True,
        "l2_quality_pass_required": True,
        "pre_event_state_must_be_past_or_event": True,
        "post_event_state_must_be_strictly_future": True,
        "max_components_per_day": MAX_D07_COMPONENTS_PER_DAY,
        "max_source_rows_per_component": MAX_D07_SOURCE_ROWS_PER_COMPONENT,
        "no_cross_date_borrow": True,
    }
    if impact_adapters is None:
        return {
            "id": "D07", "status": "BLOCKED_ADAPTER_NOT_SUPPLIED",
            "contract": contract,
            "eligible_mapping_components": sum(
                row["mapping"]["mapping_input_ticker_count"] for row in metas
            ),
            "observed_components": 0,
            "claim": "NO_RFQ_TO_CLOB_IMPACT_RESULT",
        }
    expected_dates = {row["date"] for row in descriptors}
    if set(impact_adapters) != expected_dates:
        return {
            "id": "D07", "status": "BLOCKED_INCOMPLETE_ADAPTER_SET",
            "contract": contract,
            "expected_dates": sorted(expected_dates),
            "supplied_dates": sorted(impact_adapters),
            "observed_components": 0,
            "claim": "NO_RFQ_TO_CLOB_IMPACT_RESULT",
        }
    if exact_bases is None or set(exact_bases) != expected_dates:
        return {
            "id": "D07", "status": "BLOCKED_EXACT_BASE_REBUILD_NOT_SUPPLIED",
            "contract": contract, "observed_components": 0,
            "claim": "NO_RFQ_TO_CLOB_IMPACT_RESULT",
        }
    if l2_quality_gates is None or set(l2_quality_gates) != expected_dates:
        return {
            "id": "D07", "status": "BLOCKED_EXACT_L2_QUALITY_NOT_SUPPLIED",
            "contract": contract, "observed_components": 0,
            "claim": "NO_RFQ_TO_CLOB_IMPACT_RESULT",
        }
    refused_quality = {
        date: gate.get("blockers")
        for date, gate in l2_quality_gates.items()
        if gate.get("state") != "PASS"
    }
    if refused_quality:
        return {
            "id": "D07", "status": "BLOCKED_L2_QUALITY_REFUSED",
            "contract": contract, "quality_blockers": refused_quality,
            "observed_components": 0,
            "claim": "NO_RFQ_TO_CLOB_IMPACT_RESULT",
        }
    statuses: dict[str, int] = {}
    impacts: list[int] = []
    spread_changes: list[int] = []
    depth_changes: list[int] = []
    direction_aligned_changes: list[int] = []
    direction_statuses = {
        "combo_leg_side_observed": 0,
        "combo_leg_side_unknown": 0,
        "single_direction_not_inferred": 0,
    }
    adapter_shas = []
    for descriptor, meta in zip(descriptors, metas):
        adapter = _validate_impact_adapter(
            impact_adapters[descriptor["date"]], descriptor, meta["mapping"],
            exact_base=exact_bases[descriptor["date"]],
            l2_quality_gate=l2_quality_gates[descriptor["date"]],
            rfq_events=rfq_events,
        )
        adapter_shas.append(adapter["adapter_sha256"])
        for row in adapter["observations"]:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["status"] == "OBSERVED":
                impacts.append(row["post_mid_e6"] - row["pre_mid_e6"])
                spread_changes.append(
                    row["post_spread_e6"] - row["pre_spread_e6"]
                )
                depth_changes.append(
                    row["post_depth_e2"] - row["pre_depth_e2"]
                )
                rfq_event = rfq_events[row["request_id"]]
                legs = json.loads(rfq_event["legs_json"])
                if not legs:
                    direction_statuses["single_direction_not_inferred"] += 1
                else:
                    component = row["component_index"]
                    if component >= len(legs) or (
                        legs[component]["market_ticker"] != row["market_ticker"]
                    ):
                        _fail(
                            "IMPACT_ADAPTER_DIRECTION",
                            f"combo leg ordering differs for {row['request_id']}",
                        )
                    side = legs[component].get("side")
                    if side in {"yes", "no"}:
                        direction_statuses["combo_leg_side_observed"] += 1
                        change = row["post_mid_e6"] - row["pre_mid_e6"]
                        direction_aligned_changes.append(
                            change if side == "yes" else -change
                        )
                    else:
                        direction_statuses["combo_leg_side_unknown"] += 1
    return {
        "id": "D07", "status": "EXPLORATORY_OBSERVED"
        if impacts else "BLOCKED_NO_OBSERVED_COMPONENTS",
        "contract": contract,
        "adapter_set_sha256": canonical_sha256(adapter_shas),
        "component_status_counts": [
            {"status": key, "count": statuses[key]} for key in sorted(statuses)
        ],
        "unaligned_market_response": {
            "population": "ALL_OBSERVED_MAPPED_COMPONENTS_NO_DIRECTION_INFERENCE",
            "mid_change_e6": _python_numeric_summary(impacts, "E6_DOLLARS"),
            "spread_change_e6": _python_numeric_summary(
                spread_changes, "E6_DOLLARS",
            ),
            "depth_change_e2": _python_numeric_summary(
                depth_changes, "E2_CONTRACTS",
            ),
        },
        "direction_aligned_combo_leg_response": {
            "population": "OBSERVED_COMBO_COMPONENTS_WITH_EXPLICIT_YES_OR_NO_SIDE",
            "yes_positive_mid_change_e6": _python_numeric_summary(
                direction_aligned_changes, "E6_DOLLARS",
            ),
            "coverage_counts": direction_statuses,
            "single_direction_inferred": False,
        },
        "causal_claim": False,
        "fill_or_pnl_claim": False,
    }


def _build_report(
    con: duckdb.DuckDBPyConnection,
    store: BoundedCheckpointStore,
    descriptors: list[dict[str, Any]],
    metas: list[dict[str, Any]],
    lifecycle_totals: dict[str, int],
    buckets: int,
    impact_adapters: Mapping[str, dict[str, Any]] | None,
    exact_bases: Mapping[str, dict[str, Any]] | None,
    l2_quality_gates: Mapping[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    lifecycle_paths = [
        _checkpoint_path(store, LIFECYCLE_STAGE, f"h{bucket:02d}")
        for bucket in range(buckets)
    ]
    mapping_paths = [
        _checkpoint_path(store, MAPPING_STAGE, _partition_key(day["date"], bucket))
        for day in descriptors for bucket in range(buckets)
    ]
    life = f"read_parquet({path_list(lifecycle_paths)},union_by_name=true,hive_partitioning=false)"
    mapped = f"read_parquet({path_list(mapping_paths)},union_by_name=true,hive_partitioning=false)"
    rfq_events = {
        str(row[0]): {
            "created_ts_us": int(row[1]),
            "create_recv_wall_ns": int(row[2]),
            "create_clock_skew_ms": int(row[3]),
            "legs_json": str(row[4]),
            "leg_count": int(row[5]),
        }
        for row in con.execute(f"""
          SELECT rfq_id,created_ts_us,create_recv_wall_ns,create_clock_skew_ms,
                 legs_json,leg_count
          FROM {life}
        """).fetchall()
    }

    per_hour = [
        {"utc_hour": str(row[0]), "requests": int(row[1])}
        for row in con.execute(f"""
          SELECT strftime(to_timestamp(created_ts_us/1000000.0),'%Y-%m-%dT%H') utc_hour,
                 count(*)::BIGINT
          FROM {life} GROUP BY utc_hour ORDER BY utc_hour
        """).fetchall()
    ]
    source_create = sum(row["observer"]["rfq_created_occurrence_count"] for row in metas)
    source_delete = sum(row["observer"]["rfq_deleted_occurrence_count"] for row in metas)
    if source_create != lifecycle_totals["create_occurrences"]:
        _fail("CONSERVATION_FAILED", "source/lifecycle create occurrences differ")
    if source_delete != lifecycle_totals["delete_occurrences"]:
        _fail("CONSERVATION_FAILED", "source/lifecycle delete occurrences differ")

    mapping_counts = [
        {"mapping_state": str(row[0]), "components": int(row[1])}
        for row in con.execute(f"""
          SELECT mapping_state,count(*)::BIGINT FROM {mapped}
          GROUP BY mapping_state ORDER BY mapping_state
        """).fetchall()
    ]
    boundary_excluded = int(con.execute(f"""
      SELECT count(*) FROM {mapped}
      WHERE event_window_within_base_date=false
    """).fetchone()[0])

    size_modes = [
        {"mode": str(row[0]), "requests": int(row[1])}
        for row in con.execute(f"""
          SELECT CASE
            WHEN contracts_e2 IS NULL AND target_cost_e6 IS NULL THEN 'MISSING'
            WHEN contracts_e2 IS NOT NULL AND target_cost_e6 IS NOT NULL THEN 'BOTH_REPORTED'
            WHEN contracts_e2 IS NOT NULL THEN 'CONTRACTS'
            ELSE 'TARGET_COST' END size_mode,
            count(*)::BIGINT
          FROM {life} GROUP BY size_mode ORDER BY size_mode
        """).fetchall()
    ]
    deleted_count, censored_count = [int(value) for value in con.execute(f"""
      SELECT count(*) FILTER (WHERE deletion_observed),
             count(*) FILTER (WHERE NOT deletion_observed)
      FROM {life}
    """).fetchone()]
    survival = []
    for horizon in SURVIVAL_HORIZONS_MS:
        row = con.execute(f"""
          WITH bins AS (
            SELECT observed_duration_ms t,
                   count(*) FILTER (WHERE deletion_observed)::DOUBLE deaths,
                   count(*) FILTER (WHERE NOT deletion_observed)::DOUBLE censored
            FROM {life} GROUP BY observed_duration_ms
          ), risks AS (
            SELECT t,deaths,censored,
                   (sum(deaths+censored) OVER ()
                    - coalesce(sum(deaths+censored) OVER (
                        ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                      ),0))::DOUBLE at_risk
            FROM bins
          )
          SELECT count(*) FILTER (WHERE t<={horizon} AND deaths>0),
                 coalesce(sum(deaths) FILTER (WHERE t<={horizon}),0),
                 coalesce(sum(censored) FILTER (WHERE t<={horizon}),0),
                 CASE WHEN bool_or(t<={horizon} AND deaths>=at_risk AND deaths>0)
                      THEN 0.0
                      ELSE exp(coalesce(sum(
                        CASE WHEN t<={horizon} AND deaths>0
                             THEN ln(1.0-deaths/at_risk) ELSE 0 END
                      ),0)) END
          FROM risks
        """).fetchone()
        survival.append({
            "horizon_ms": horizon,
            "event_time_count": int(row[0]),
            "deletions_through_horizon": int(row[1]),
            "censored_through_horizon": int(row[2]),
            "kaplan_meier_survival_bps": int(round(float(row[3]) * 10_000)),
        })

    combo_row = con.execute(f"""
      SELECT count(*) FILTER (WHERE leg_count>0 OR mve_collection_ticker IS NOT NULL),
             count(*),sum(leg_count),sum(yes_leg_count),sum(no_leg_count),
             sum(unknown_side_leg_count)
      FROM {life}
    """).fetchone()
    combo_count, request_count, total_legs, yes_legs, no_legs, unknown_legs = [
        int(value or 0) for value in combo_row
    ]
    requester_row = con.execute(f"""
      WITH counts AS (
        SELECT requester_hash,count(*)::BIGINT n FROM {life}
        WHERE requester_hash IS NOT NULL GROUP BY requester_hash
      )
      SELECT (SELECT count(*) FROM {life} WHERE requester_hash IS NOT NULL),
             count(*),coalesce(max(n),0),
             coalesce(sum(n*n),0),coalesce(sum(n),0)
      FROM counts
    """).fetchone()
    known_requests, distinct_requesters, top1, hhi_numerator, known_total = [
        int(value or 0) for value in requester_row
    ]
    ranked_requesters = [
        {"requester_hash": str(row[0]), "requests": int(row[1])}
        for row in con.execute(f"""
          SELECT requester_hash,count(*)::BIGINT n FROM {life}
          WHERE requester_hash IS NOT NULL GROUP BY requester_hash
          ORDER BY n DESC,requester_hash LIMIT 20
        """).fetchall()
    ]
    top5 = sum(row["requests"] for row in ranked_requesters[:5])
    direction_requests = int(con.execute(f"""
      SELECT count(*) FROM {life} WHERE yes_leg_count+no_leg_count>0
    """).fetchone()[0])

    excluded_frames: dict[str, int] = {}
    for meta in metas:
        for row in meta["observer"]["excluded_frame_type_counts"]:
            excluded_frames[row["frame_type"]] = (
                excluded_frames.get(row["frame_type"], 0) + row["count"]
            )
    mapping_component_total = sum(row["components"] for row in mapping_counts)
    embedded_mapping_total = sum(
        meta["mapping"]["mapping_input_ticker_count"] for meta in metas
    )
    if mapping_component_total != embedded_mapping_total:
        _fail("CONSERVATION_FAILED", "mapping manifest/checkpoint totals differ")

    report = {
        "schema": SCHEMA,
        "tier": "EXPLORATORY_AUTORESEARCH",
        "research_lane": "FRESH_RFQ_ONLY",
        "old_lineage_state": "DATA_INTEGRITY_BLOCKED_NOT_READ",
        "rfq_scope": "D01_D07",
        "source_binding_sha256": store.source_binding,
        "method_module_sha256": _module_sha256(),
        "analysis_dates": [row["date"] for row in descriptors],
        "inputs": {
            "fresh_authority_sha256": descriptors[0]["authority_sha256"],
            "generation": descriptors[0]["generation"],
            "overlay_manifest_sha256s": [
                row["overlay_manifest_sha256"] for row in descriptors
            ],
            "exact_object_count": sum(
                len(row["analysis_rfq_objects"]) for row in descriptors
            ),
            "exact_object_bytes": sum(
                item["size"] for row in descriptors
                for item in row["analysis_rfq_objects"]
            ),
            "exact_reader_attestation_sha256s": [
                row["exact_reader_attestation"]["attestation_sha256"]
                for row in metas
            ],
            "source_gap_gate": "PASS_24_EXACT_HOURS_PER_DATE",
            "clock_gate": "PASS_FRESH_HOUR_RECEIPTS_AND_TIME_CONTRACT",
            "hash_partition": {
                "algorithm": "SHA256_FULL_RFQ_ID_FIRST_U64_BE_MOD_N",
                "buckets": buckets,
            },
        },
        "conservation": {
            "source_create_occurrences": source_create,
            "source_delete_occurrences": source_delete,
            "global_unique_create_ids": lifecycle_totals["unique_create_ids"],
            "conflicting_create_ids_excluded": lifecycle_totals[
                "conflicting_create_ids"
            ],
            "lifecycle_rows": lifecycle_totals["lifecycle_rows"],
            "identity_equation_pass": lifecycle_totals["unique_create_ids"] == (
                lifecycle_totals["conflicting_create_ids"]
                + lifecycle_totals["lifecycle_rows"]
            ),
            "orphan_delete_ids_excluded": lifecycle_totals["orphan_delete_ids"],
            "mapping_components": mapping_component_total,
            "all_exact_objects_parsed": all(
                row["provenance"]["all_object_bytes_parsed"] is True
                and row["provenance"]["all_physical_rows_classified"] is True
                for row in metas
            ),
        },
        "mapping_quality": {
            "matching_policy": market_mapping.MATCHING_POLICY,
            "cross_date_policy": market_mapping.CROSS_DATE_POLICY,
            "state_counts": mapping_counts,
            "event_window_excluded_components": boundary_excluded,
            "unmapped_or_partial_components": sum(
                row["components"] for row in mapping_counts
                if row["mapping_state"] != "MAPPED_L1_L2"
            ),
        },
        "d01_flow_census": {
            "id": "D01", "status": "EXPLORATORY_COMPLETE",
            "unique_requests": request_count,
            "create_occurrences_before_exact_dedup": source_create,
            "delete_occurrences_before_exact_dedup": source_delete,
            "per_utc_hour": per_hour,
            "excluded_non_rfq_broadcast_frames": [
                {"frame_type": key, "count": excluded_frames[key]}
                for key in sorted(excluded_frames)
            ],
        },
        "d02_size_intent": {
            "id": "D02", "status": "EXPLORATORY_COMPLETE",
            "contracts_fp": _numeric_summary_sql(
                con, life, "contracts_e2", unit="E2_CONTRACTS",
            ),
            "target_cost_dollars": _numeric_summary_sql(
                con, life, "target_cost_e6", unit="E6_DOLLARS",
            ),
            "size_mode_counts": size_modes,
            "intent_observability": (
                "SINGLE_HAS_NO_SIDE; COMBO_LEG_SIDE_ONLY_WHEN_PRESENT"
            ),
            "populations_combined": False,
        },
        "d03_lifecycle": {
            "id": "D03", "status": "EXPLORATORY_COMPLETE_WITH_RIGHT_CENSORING",
            "deletion_observed": deleted_count,
            "right_censored": censored_count,
            "deletion_match_coverage_bps": _bps(deleted_count, request_count),
            "observed_lifetime_ms": _numeric_summary_sql(
                con, life, "lifetime_ms", where="deletion_observed",
                unit="MILLISECONDS",
            ),
            "kaplan_meier": survival,
            "censoring_rule": "UNMATCHED_AT_END_OF_LAST_CONTIGUOUS_UTC_DATE",
        },
        "d04_combo_leg_pressure": {
            "id": "D04", "status": "EXPLORATORY_COMPLETE",
            "combo_requests": combo_count,
            "single_requests": request_count - combo_count,
            "combo_share_bps": _bps(combo_count, request_count),
            "leg_count": _numeric_summary_sql(
                con, life, "leg_count", where="leg_count>0", unit="LEGS",
            ),
            "total_legs": total_legs,
            "yes_side_legs": yes_legs,
            "no_side_legs": no_legs,
            "unknown_side_legs": unknown_legs,
            "signed_leg_pressure_proxy": yes_legs - no_legs,
            "claim": "REQUEST_LEG_COMPOSITION_ONLY_NOT_TRADING_DIRECTION",
        },
        "d05_requester_concentration": {
            "id": "D05", "status": "EXPLORATORY_CONDITIONAL_ON_OBSERVED_ID",
            "known_requester_requests": known_requests,
            "known_id_coverage_bps": _bps(known_requests, request_count),
            "distinct_requester_hashes": distinct_requesters,
            "known_subset_top1_share_bps": _bps(top1, known_total),
            "known_subset_top5_share_bps": _bps(top5, known_total),
            "known_subset_hhi_bps": _bps(
                hhi_numerator, known_total * known_total,
            ),
            "full_cohort_top1_lower_bound_bps": _bps(top1, request_count),
            "full_cohort_top1_upper_bound_bps": _bps(
                top1 + (request_count - known_requests), request_count,
            ),
            "top_requester_hashes": ranked_requesters,
            "censoring_warning": (
                "creator_id is normally learned only from a consistent delete; "
                "concentration is conditional on that observed subset"
            ),
        },
        "d06_direction_volume_proxy": {
            "id": "D06",
            "status": "EXPLORATORY_PARTIALLY_OBSERVABLE"
            if direction_requests else "BLOCKED_NO_OBSERVABLE_DIRECTION",
            "requests_with_observed_combo_leg_side": direction_requests,
            "coverage_bps": _bps(direction_requests, request_count),
            "yes_leg_count": yes_legs,
            "no_leg_count": no_legs,
            "signed_direction_proxy": yes_legs - no_legs,
            "contracts_volume_proxy_on_observable_subset": _numeric_summary_sql(
                con, life, "contracts_e2",
                where="yes_leg_count+no_leg_count>0", unit="E2_CONTRACTS",
            ),
            "target_cost_proxy_on_observable_subset": _numeric_summary_sql(
                con, life, "target_cost_e6",
                where="yes_leg_count+no_leg_count>0", unit="E6_DOLLARS",
            ),
            "single_request_direction_inferred": False,
            "contracts_and_target_cost_combined": False,
        },
        "d07_rfq_to_clob_impact": _d07_result(
            descriptors, metas, impact_adapters,
            exact_bases=exact_bases,
            l2_quality_gates=l2_quality_gates,
            rfq_events=rfq_events,
        ),
        "d08_profitability": {
            "id": "D08",
            "status": "BLOCKED_NO_QUOTE_FILL_FEE_INVENTORY_SETTLEMENT_OBSERVABILITY",
            "pnl_claim": False,
            "fill_claim": False,
            "monetizable_strategy_claim": False,
            "reason": (
                "passive RFQ create/delete plus L1/L2 impact cannot establish "
                "quote acceptance, fill probability, fees, inventory, or realized PnL"
            ),
        },
        "research_claims": {
            "descriptive_only": True,
            "causal": False,
            "profitable": False,
            "production_trading_gate": False,
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = canonical_bytes(report) + b"\n"
    try:
        descriptor = os.open(
            pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o640,
        )
        try:
            view = memoryview(payload)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    _fail("REPORT_WRITE_FAILED", str(path))
                view = view[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if pending.exists():
            pending.unlink()


def run_bounded_fresh_rfq(
    *,
    overlay_ready_paths: list[str | os.PathLike[str]],
    fresh_authority: dict[str, Any],
    client_factory: Callable[[dict[str, Any]], Any],
    checkpoint_root: str | os.PathLike[str],
    transport_kind: str = "INJECTED_EXACT_VERSION_CLIENT",
    hash_buckets: int = DEFAULT_HASH_BUCKETS,
    exact_temp_parent: str | os.PathLike[str] | None = None,
    impact_adapters: Mapping[str, dict[str, Any]] | None = None,
    base_manifest_bytes_by_date: Mapping[str, bytes] | None = None,
    l2_quality_receipts_by_date: Mapping[str, dict[str, Any]] | None = None,
    report_path: str | os.PathLike[str] | None = None,
    expected_eligible_dates: list[str] | None = None,
) -> dict[str, Any]:
    """Run bounded D01--D07 over one or more contiguous fresh-overlay days.

    All overlay and authority inputs are validated before ``client_factory`` is
    called.  The function performs local checkpoint/report writes only.  It
    contains no AWS client, publication, tag, or deployment implementation.
    """
    if not isinstance(overlay_ready_paths, list) or not overlay_ready_paths:
        _fail("OVERLAY_SET_INVALID", "at least one overlay READY is required")
    if len(overlay_ready_paths) > MAX_ANALYSIS_DAYS:
        _fail(
            "OVERLAY_SET_INVALID",
            f"one bounded run supports at most {MAX_ANALYSIS_DAYS} days",
        )
    if type(hash_buckets) is not int or not 1 <= hash_buckets <= 256:
        _fail("HASH_BUCKETS_INVALID", "hash_buckets must be 1..256")
    if not callable(client_factory):
        _fail("EXACT_CLIENT_REQUIRED", "client_factory must be callable")
    descriptors = [
        load_overlay_descriptor(path, fresh_authority)
        for path in overlay_ready_paths
    ]
    descriptors.sort(key=lambda row: row["date"])
    observed_date_texts = [row["date"] for row in descriptors]
    if expected_eligible_dates is not None:
        if not isinstance(expected_eligible_dates, list) or not expected_eligible_dates:
            _fail(
                "ELIGIBLE_DATE_MISMATCH",
                "expected_eligible_dates must be a non-empty exact date list",
            )
        expected_date_texts = [
            _date(value, f"expected_eligible_dates[{index}]")
            for index, value in enumerate(expected_eligible_dates)
        ]
        if expected_date_texts != observed_date_texts:
            _fail(
                "ELIGIBLE_DATE_MISMATCH",
                f"expected={expected_date_texts} observed={observed_date_texts}",
            )
    dates = [dt.date.fromisoformat(row["date"]) for row in descriptors]
    if len(set(dates)) != len(dates):
        _fail("OVERLAY_SET_INVALID", "overlay dates are duplicated")
    if dates != [dates[0] + dt.timedelta(days=index) for index in range(len(dates))]:
        _fail("SOURCE_GAP_GATE", "analysis dates must be contiguous")
    if len({row["authority_sha256"] for row in descriptors}) != 1:
        _fail("OVERLAY_SET_INVALID", "overlay set spans multiple authorities")
    if len({row["generation"] for row in descriptors}) != 1:
        _fail("OVERLAY_SET_INVALID", "overlay set spans multiple generations")

    date_set = set(observed_date_texts)
    exact_bases: dict[str, dict[str, Any]] | None = None
    if base_manifest_bytes_by_date is not None:
        if (
            not isinstance(base_manifest_bytes_by_date, Mapping)
            or set(base_manifest_bytes_by_date) != date_set
        ):
            _fail(
                "D07_BASE_EXACT_SET",
                "exact base manifest byte set must equal overlay dates",
            )
        exact_bases = {
            descriptor["date"]: _rebuild_exact_base(
                descriptor, base_manifest_bytes_by_date[descriptor["date"]],
            )
            for descriptor in descriptors
        }

    l2_quality_gates: dict[str, dict[str, Any]] | None = None
    if l2_quality_receipts_by_date is not None:
        if (
            not isinstance(l2_quality_receipts_by_date, Mapping)
            or set(l2_quality_receipts_by_date) != date_set
        ):
            _fail(
                "D07_L2_QUALITY_SET",
                "exact L2 quality receipt set must equal overlay dates",
            )
        l2_quality_gates = {}
        for date_text in observed_date_texts:
            entry = _exact_keys(
                l2_quality_receipts_by_date[date_text],
                {"exact_identity", "body"},
                f"L2 quality receipt {date_text}",
            )
            l2_quality_gates[date_text] = build_l2_quality_gate(
                analysis_date=date_text,
                exact_identity=entry["exact_identity"],
                body=entry["body"],
            )

    source_binding = _source_binding(descriptors, hash_buckets)
    versions = {
        SOURCE_EVENT_STAGE: _stage_version("fresh-rfq-source-events-v1"),
        SOURCE_META_STAGE: _stage_version("fresh-rfq-source-meta-v1"),
        MAPPING_STAGE: _stage_version("fresh-rfq-mapping-v1"),
        LIFECYCLE_STAGE: _stage_version("fresh-rfq-lifecycle-v1"),
    }
    con = duckdb.connect()
    try:
        con.execute("SET threads=2")
        con.execute("SET memory_limit='16GB'")
        con.execute("SET preserve_insertion_order=false")
        with BoundedCheckpointStore(Path(checkpoint_root), source_binding) as store:
            metas = [
                _materialize_day(
                    con, store, descriptor,
                    client_factory=client_factory,
                    transport_kind=transport_kind,
                    buckets=hash_buckets,
                    versions=versions,
                    temp_parent=None if exact_temp_parent is None
                    else Path(exact_temp_parent),
                )
                for descriptor in descriptors
            ]
            event_keys = [
                _partition_key(row["date"], bucket)
                for row in descriptors for bucket in range(hash_buckets)
            ]
            meta_keys = [_date_meta_key(row["date"]) for row in descriptors]
            store.finalize_stage(
                con, stage=SOURCE_EVENT_STAGE,
                stage_version=versions[SOURCE_EVENT_STAGE],
                partition_keys=event_keys,
            )
            store.finalize_stage(
                con, stage=MAPPING_STAGE,
                stage_version=versions[MAPPING_STAGE],
                partition_keys=event_keys,
            )
            store.finalize_stage(
                con, stage=SOURCE_META_STAGE,
                stage_version=versions[SOURCE_META_STAGE],
                partition_keys=meta_keys,
            )
            _receipts, lifecycle_totals = _materialize_lifecycle(
                con, store, descriptors, hash_buckets,
                versions[LIFECYCLE_STAGE],
            )
            store.finalize_stage(
                con, stage=LIFECYCLE_STAGE,
                stage_version=versions[LIFECYCLE_STAGE],
                partition_keys=[f"h{bucket:02d}" for bucket in range(hash_buckets)],
            )
            report = _build_report(
                con, store, descriptors, metas, lifecycle_totals,
                hash_buckets, impact_adapters, exact_bases, l2_quality_gates,
            )
        if report_path is not None:
            _write_report(Path(report_path), report)
        return report
    finally:
        con.close()


__all__ = [
    "FreshRfqResearchError", "IMPACT_SCHEMA", "SCHEMA",
    "build_l2_quality_gate", "canonical_bytes", "canonical_sha256",
    "load_overlay_descriptor",
    "run_bounded_fresh_rfq",
]
