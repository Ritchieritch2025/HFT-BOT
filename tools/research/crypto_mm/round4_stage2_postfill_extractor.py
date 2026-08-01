#!/usr/bin/env python3
"""Fail-closed Stage-2 causal-source preparation.

This module is deliberately *not* a V4.1 sample generator.  The independent
V4.1 audit returned REJECT/NO-SEAL, so no contract adapter is active.  The
code below prepares only the contract-independent foundation:

* exact-version, research-eligible input authorization;
* content-root and byte-hash binding;
* an exact 72-market discovery roster;
* receive-clock-only event normalization with atomic exact ties;
* fail-closed L2 left-truncation handling; and
* label-only terminal outcomes plus deterministic provenance receipts.

``authorize_extraction`` always fails until a separately audited V4.2 adapter
is installed.  Nothing here fits a model, selects a candidate, authorizes a
shadow/live strategy, accesses a private account, or submits an order.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo


DISCOVERY_DATES = (
    "2026-07-20",
    "2026-07-21",
    "2026-07-22",
)
FORBIDDEN_DATES = frozenset(("2026-07-23", "2026-07-26"))
SOURCE_PREPARATION_SCHEMA = "round4-stage2-causal-source-preparation-v1"
CONTRACT_ADAPTER_STATUS = "V4_2_PENDING"
EXTRACTION_AUTHORIZED = False
FIT_AUTHORIZED = False
CANDIDATE_SELECTION_AUTHORIZED = False
LIVE_AUTHORIZED = False

REJECTED_V41_CONTRACT_VERSION = "ROUND4_POSTFILL_PUBLIC_PROXY_V4_1"
REJECTED_V41_ARTIFACT_SHA256 = MappingProxyType(
    {
        "contract_python": (
            "2642797235851cfd172ac27a2ccf1b964d65e8b3e13cbc132761fbce5db165ab"
        ),
        "ddl": (
            "08c3510011f3ffc3c9e5b19ab4d06b9dd50b8ba38950801f4c3d23cd0709efe4"
        ),
        "validator": (
            "a148d75daaa69d2e79d351fe35efbcc675db3eb1fdd52aaddfc1a7e4a5ed5f1b"
        ),
        "preregistration": (
            "725ea9806f49de26ea3ef804e638a632d5c4e0051b98a5dc30b75bc84ac5fc8e"
        ),
    }
)
V41_REJECTION_REASON_CODES = (
    "VALIDATOR_HASH_NOT_PINNED",
    "ORPHAN_METADATA_SETTLEMENT",
    "BOOK_NOT_LATEST_ASOF",
    "SOURCE_PATH_NOT_CONTENT_ROOT_BOUND",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^KXBTC15M-[0-9]{2}[A-Z]{3}[0-9]{6}-15$")
E4 = Decimal("10000")
MIN_TRUE_FILL_E4 = 100
NEW_YORK = ZoneInfo("America/New_York")


class Stage2ExtractionError(RuntimeError):
    """A source, gate, clock, or causal-state invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise Stage2ExtractionError(code, detail)


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", label)
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("INVALID_TEXT", label)
    return value


def _require_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        _fail("INVALID_INTEGER", label)
    if minimum is not None and value < minimum:
        _fail("INVALID_INTEGER", f"{label} below {minimum}")
    return value


def _require_true(value: object, code: str, label: str) -> None:
    if value is not True:
        _fail(code, label)


def _load_json_bytes(payload: bytes, label: str) -> Mapping[str, object]:
    def unique_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("DUPLICATE_JSON_KEY", f"{label}:{key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique_pairs)
    except Stage2ExtractionError:
        raise
    except Exception as exc:
        _fail("INVALID_JSON", f"{label}: {exc}")
    if not isinstance(value, Mapping):
        _fail("INVALID_JSON", f"{label}: root must be an object")
    return value


def expected_hourly_roster(
    dates: Sequence[str] = DISCOVERY_DATES,
) -> tuple[str, ...]:
    """Return the sealed hourly ``-15`` KXBTC15M roster for UTC days."""
    if tuple(dates) != tuple(sorted(dates)):
        _fail("DATE_ORDER_INVALID", "roster dates must be sorted")
    result: list[str] = []
    for day in dates:
        if day in FORBIDDEN_DATES or day not in DISCOVERY_DATES:
            _fail("DATE_NOT_DISCOVERY", day)
        try:
            start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        except ValueError:
            _fail("DATE_NOT_DISCOVERY", day)
        for hour in range(24):
            contract_close_utc = start + timedelta(hours=hour, minutes=15)
            local_close = contract_close_utc.astimezone(NEW_YORK)
            identity = local_close.strftime("%y%b%d%H%M").upper()
            result.append(f"KXBTC15M-{identity}-15")
    if (
        len(result) != 24 * len(dates)
        or len(result) != len(set(result))
        or any(TICKER_RE.fullmatch(ticker) is None for ticker in result)
    ):
        _fail("ROSTER_CONSTRUCTION_FAILED", "hourly roster is not unique")
    return tuple(result)


def required_btc_fact_keys(day: str) -> dict[str, str]:
    if day not in DISCOVERY_DATES:
        _fail("DATE_NOT_DISCOVERY", day)
    partition = f"category=Crypto/subcategory=BTC/date={day}"
    return {
        "orderbooks_full": (
            "warehouse/facts/orderbooks_full/"
            f"{partition}/orderbooks_full__Crypto__BTC__{day}.parquet"
        ),
        "trades": (
            "warehouse/facts/trades/"
            f"{partition}/trades__Crypto__BTC__{day}.csv.gz"
        ),
    }


EXPECTED_ROSTER = expected_hourly_roster()
EXPECTED_ROSTER_SET = frozenset(EXPECTED_ROSTER)
MARKET_SOURCE_DAY = MappingProxyType(
    {
        ticker: day
        for day in DISCOVERY_DATES
        for ticker in expected_hourly_roster((day,))
    }
)


@dataclass(frozen=True)
class EligibleExactVersionObject:
    source_date_utc: str
    table: str
    logical_source_key: str
    bucket: str
    key: str
    version_id: str
    sha256: str
    size: int
    verification_state: str
    version_resolution: str

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "logical_source_key": self.logical_source_key,
            "bucket": self.bucket,
            "key": self.key,
            "VersionId": self.version_id,
            "sha256": self.sha256,
            "size": self.size,
            "verification_state": self.verification_state,
            "version_resolution": self.version_resolution,
        }


@dataclass(frozen=True)
class EligibleDay:
    source_date_utc: str
    daily_status_sha256: str
    durable_index_sha256: str
    tagged_receipt_sha256: str
    receipt_set_sha256: str
    durable_receipt_set_sha256: str
    durability_set_sha256: str
    eligibility_single_writer_audit_sha256: str
    manifest_version_id: str
    manifest_sha256: str
    exact_version_objects: tuple[EligibleExactVersionObject, ...]


@dataclass(frozen=True)
class SourcePreparationAuthority:
    source_dates: tuple[str, ...]
    eligible_days: tuple[EligibleDay, ...]
    exact_version_objects: tuple[EligibleExactVersionObject, ...]
    source_preparation_allowed: bool = True
    extraction_allowed: bool = EXTRACTION_AUTHORIZED
    contract_adapter_status: str = CONTRACT_ADAPTER_STATUS

    def receipt(self) -> dict[str, object]:
        return {
            "source_dates": list(self.source_dates),
            "source_preparation_allowed": self.source_preparation_allowed,
            "extraction_allowed": self.extraction_allowed,
            "contract_adapter_status": self.contract_adapter_status,
            "days": [
                {
                    "source_date_utc": day.source_date_utc,
                    "daily_status_sha256": day.daily_status_sha256,
                    "durable_index_sha256": day.durable_index_sha256,
                    "tagged_receipt_sha256": day.tagged_receipt_sha256,
                    "receipt_set_sha256": day.receipt_set_sha256,
                    "durable_receipt_set_sha256": (
                        day.durable_receipt_set_sha256
                    ),
                    "durability_set_sha256": day.durability_set_sha256,
                    "eligibility_single_writer_audit_sha256": (
                        day.eligibility_single_writer_audit_sha256
                    ),
                    "manifest_version_id": day.manifest_version_id,
                    "manifest_sha256": day.manifest_sha256,
                }
                for day in self.eligible_days
            ],
            "exact_version_objects": [
                obj.receipt() for obj in self.exact_version_objects
            ],
        }


def _eligible_object_from_receipt(
    raw: Mapping[str, object],
    *,
    day: str,
    table: str,
    logical_source_key: str,
) -> EligibleExactVersionObject:
    if (
        raw.get("date") != day
        or raw.get("family") != "facts"
        or raw.get("table") != table
        or raw.get("logical_source_key") != logical_source_key
    ):
        _fail(
            "SOURCE_IDENTITY_MISMATCH",
            f"{day}:{table}:{logical_source_key}",
        )
    for field in (
        "durability_scope",
        "durability_verified",
        "research_candidate",
        "research_eligible",
    ):
        _require_true(
            raw.get(field),
            "SOURCE_NOT_ELIGIBLE",
            f"{logical_source_key}:{field}",
        )
    if (
        raw.get("eligibility_tag_state") != "TAGGED_VERIFIED"
        or raw.get("exposure_policy") != "RESEARCH_ELIGIBLE"
        or raw.get("mutable_source") is not False
    ):
        _fail("SOURCE_NOT_ELIGIBLE", logical_source_key)
    verification_state = _require_text(
        raw.get("verification_state"),
        f"{logical_source_key}:verification_state",
    )
    version_resolution = _require_text(
        raw.get("version_resolution"),
        f"{logical_source_key}:version_resolution",
    )
    if (
        verification_state != "EXACT_VERSION_FULL_SHA256"
        or version_resolution != "SEALED_CURRENT_EXACT"
    ):
        _fail("SOURCE_NOT_EXACT_VERSION", logical_source_key)
    key = _require_text(raw.get("key"), f"{logical_source_key}:key")
    if key != f"ec2/{logical_source_key}":
        _fail("SOURCE_KEY_MISMATCH", logical_source_key)
    return EligibleExactVersionObject(
        source_date_utc=day,
        table=table,
        logical_source_key=logical_source_key,
        bucket=_require_text(raw.get("bucket"), "bucket"),
        key=key,
        version_id=_require_text(raw.get("VersionId"), "VersionId"),
        sha256=_require_sha(raw.get("sha256"), f"{logical_source_key}:sha"),
        size=_require_int(
            raw.get("size"),
            f"{logical_source_key}:size",
            minimum=1,
        ),
        verification_state=verification_state,
        version_resolution=version_resolution,
    )


def verify_day_eligibility(
    *,
    day: str,
    status: Mapping[str, object],
    durable_index: Mapping[str, object],
    tagged_receipt_bytes: bytes,
    required_logical_sources: Mapping[str, str],
) -> EligibleDay:
    """Bind a published daily status to its exact tagged fact objects."""
    if day not in DISCOVERY_DATES or day in FORBIDDEN_DATES:
        _fail("DATE_NOT_DISCOVERY", day)
    if (
        status.get("schema_version") != "research-v3-daily-status-v2"
        or status.get("state") != "V3_REFERENCE_PUBLISHED"
        or status.get("manifest_commit_state")
        != "REFERENCE_MANIFEST_COMMITTED"
        or status.get("date") != day
    ):
        _fail("DAY_NOT_PUBLISHED", day)
    if (
        durable_index.get("schema_version")
        != "canonical-durable-receipt-index-v1"
        or durable_index.get("state") != "DURABLE_RECEIPT_VERIFIED"
        or durable_index.get("receipt_phase")
        != "TAGGED_ELIGIBILITY_VERIFIED"
        or durable_index.get("date") != day
        or durable_index.get("complete") is not True
        or durable_index.get("completed") is not True
    ):
        _fail("DURABLE_INDEX_NOT_FINAL", day)

    tagged_sha = hashlib.sha256(tagged_receipt_bytes).hexdigest()
    tagged_size = len(tagged_receipt_bytes)
    index_tagged_sha = _require_sha(
        durable_index.get("receipt_payload_sha256"),
        f"{day}:receipt_payload_sha256",
    )
    index_tagged_size = _require_int(
        durable_index.get("receipt_payload_size"),
        f"{day}:receipt_payload_size",
        minimum=1,
    )
    receipt_object = durable_index.get("receipt_object")
    if not isinstance(receipt_object, Mapping):
        _fail("DURABLE_INDEX_NOT_FINAL", f"{day}:receipt_object")
    if (
        tagged_sha != index_tagged_sha
        or tagged_size != index_tagged_size
        or receipt_object.get("sha256") != tagged_sha
        or receipt_object.get("size") != tagged_size
        or receipt_object.get("verification_state")
        != "EXACT_VERSION_FULL_SHA256"
    ):
        _fail("TAGGED_RECEIPT_BYTE_MISMATCH", day)
    _require_text(receipt_object.get("VersionId"), "receipt VersionId")

    tagged = _load_json_bytes(tagged_receipt_bytes, f"tagged:{day}")
    receipt_set = _require_sha(
        tagged.get("receipt_set_sha256"),
        f"{day}:receipt_set_sha256",
    )
    durable_set = _require_sha(
        durable_index.get("byte_attestation_receipt_set_sha256"),
        f"{day}:durable set",
    )
    audit_sha = _require_sha(
        durable_index.get("eligibility_single_writer_audit_sha256"),
        f"{day}:eligibility audit",
    )
    if (
        tagged.get("schema_version") != "canonical-object-receipt-v1"
        or tagged.get("date") != day
        or tagged.get("state") != "DURABLE_RECEIPT_VERIFIED"
        or tagged.get("authoritative") is not True
        or tagged.get("receipt_phase") != "TAGGED_ELIGIBILITY_VERIFIED"
        or tagged.get("receipt_set_sha256") != receipt_set
        or tagged.get("byte_attestation_receipt_set_sha256") != durable_set
        or tagged.get("eligibility_single_writer_audit_sha256") != audit_sha
        or durable_index.get("receipt_set_sha256") != receipt_set
        or status.get("durable_receipt_set_sha256") != durable_set
        or durable_index.get("receipt_object_eligibility_tag_state")
        != "TAGGED_VERIFIED"
    ):
        _fail("ELIGIBILITY_CHAIN_MISMATCH", day)
    object_durability_set = _require_sha(
        tagged.get("durability_set_sha256"),
        f"{day}:durability_set_sha256",
    )
    tagged_index_path = _require_text(
        status.get("tagged_index"),
        f"{day}:tagged_index",
    )
    if Path(tagged_index_path).name != f"TAGGED-DURABLE-{receipt_set}.json":
        _fail("ELIGIBILITY_CHAIN_MISMATCH", f"{day}:tagged_index")

    manifest = status.get("manifest_object")
    if not isinstance(manifest, Mapping):
        _fail("MANIFEST_NOT_EXACT_VERSION", day)
    if (
        manifest.get("verification_state") != "EXACT_VERSION_FULL_SHA256"
        or type(manifest.get("size")) is not int
        or int(manifest["size"]) <= 0
    ):
        _fail("MANIFEST_NOT_EXACT_VERSION", day)
    manifest_version = _require_text(
        manifest.get("VersionId"),
        f"{day}:manifest VersionId",
    )
    manifest_sha = _require_sha(
        manifest.get("sha256"),
        f"{day}:manifest sha",
    )

    expected_sources = required_btc_fact_keys(day)
    if dict(required_logical_sources) != expected_sources:
        _fail(
            "REQUIRED_SOURCE_SET_INVALID",
            f"{day}: expected exact Crypto/BTC fact keys",
        )
    objects = tagged.get("objects")
    if not isinstance(objects, list):
        _fail("TAGGED_RECEIPT_INVALID", f"{day}:objects")
    resolved: list[EligibleExactVersionObject] = []
    for table, logical_key in sorted(required_logical_sources.items()):
        matches = [
            item
            for item in objects
            if isinstance(item, Mapping)
            and item.get("logical_source_key") == logical_key
        ]
        if len(matches) != 1:
            _fail(
                "SOURCE_CARDINALITY_MISMATCH",
                f"{day}:{table}:found={len(matches)}",
            )
        resolved.append(
            _eligible_object_from_receipt(
                matches[0],
                day=day,
                table=table,
                logical_source_key=logical_key,
            )
        )
    return EligibleDay(
        source_date_utc=day,
        daily_status_sha256=canonical_sha256(status),
        durable_index_sha256=canonical_sha256(durable_index),
        tagged_receipt_sha256=tagged_sha,
        receipt_set_sha256=receipt_set,
        durable_receipt_set_sha256=durable_set,
        durability_set_sha256=object_durability_set,
        eligibility_single_writer_audit_sha256=audit_sha,
        manifest_version_id=manifest_version,
        manifest_sha256=manifest_sha,
        exact_version_objects=tuple(
            sorted(resolved, key=lambda item: item.table)
        ),
    )


def prepare_source_authority(
    *,
    eligible_days: Mapping[str, EligibleDay],
) -> SourcePreparationAuthority:
    """Authorize source preparation only; no Stage-2 contract extraction."""
    if tuple(sorted(eligible_days)) != DISCOVERY_DATES:
        _fail(
            "DAY_SET_MISMATCH",
            f"expected={DISCOVERY_DATES} actual={tuple(sorted(eligible_days))}",
        )
    days: list[EligibleDay] = []
    objects: list[EligibleExactVersionObject] = []
    seen: set[str] = set()
    for day in DISCOVERY_DATES:
        receipt = eligible_days[day]
        if not isinstance(receipt, EligibleDay):
            _fail("DAY_RECEIPT_TYPE_INVALID", day)
        if receipt.source_date_utc != day:
            _fail("DAY_RECEIPT_IDENTITY_MISMATCH", day)
        if {item.table for item in receipt.exact_version_objects} != {
            "trades",
            "orderbooks_full",
        }:
            _fail("REQUIRED_SOURCE_SET_INVALID", day)
        for item in receipt.exact_version_objects:
            if item.logical_source_key in seen:
                _fail("DUPLICATE_LOGICAL_SOURCE", item.logical_source_key)
            seen.add(item.logical_source_key)
            objects.append(item)
        days.append(receipt)
    return SourcePreparationAuthority(
        source_dates=DISCOVERY_DATES,
        eligible_days=tuple(days),
        exact_version_objects=tuple(
            sorted(
                objects,
                key=lambda item: (
                    item.source_date_utc,
                    item.table,
                    item.logical_source_key,
                ),
            )
        ),
    )


def authorize_extraction(
    *,
    audit_receipt: Mapping[str, object],
    eligible_days: Mapping[str, EligibleDay],
) -> SourcePreparationAuthority:
    """Fail closed until a V4.2 contract adapter is audited and installed."""
    prepare_source_authority(eligible_days=eligible_days)
    version = audit_receipt.get("contract_version")
    if version == REJECTED_V41_CONTRACT_VERSION:
        detail = (
            "V4.1 is REJECT/NO-SEAL; "
            f"reasons={','.join(V41_REJECTION_REASON_CODES)}"
        )
    else:
        detail = "no independently accepted V4.2 adapter is installed"
    _fail("CONTRACT_ADAPTER_PENDING", detail)


def _object_value(
    obj: EligibleExactVersionObject | Mapping[str, object],
    field: str,
) -> object:
    if isinstance(obj, EligibleExactVersionObject):
        return {
            "logical_source_key": obj.logical_source_key,
            "sha256": obj.sha256,
            "size": obj.size,
            "VersionId": obj.version_id,
            "table": obj.table,
            "source_date_utc": obj.source_date_utc,
        }[field]
    return obj.get(field)


def verify_local_exact_version(
    path: Path | str,
    obj: EligibleExactVersionObject | Mapping[str, object],
    *,
    content_root: Path | str,
) -> dict[str, object]:
    """Verify an on-disk source against a content-root-bound exact receipt."""
    source_path = Path(path).resolve()
    root = Path(content_root).resolve()
    logical_key = _require_text(
        _object_value(obj, "logical_source_key"),
        "logical_source_key",
    )
    expected_path = (root / logical_key).resolve()
    if source_path != expected_path:
        _fail(
            "CONTENT_ROOT_MISMATCH",
            f"expected={expected_path} actual={source_path}",
        )
    try:
        stat = source_path.stat()
    except OSError as exc:
        _fail("LOCAL_SOURCE_UNREADABLE", f"{source_path}: {exc}")
    expected_size = _require_int(
        _object_value(obj, "size"),
        f"{logical_key}:size",
        minimum=1,
    )
    expected_sha = _require_sha(
        _object_value(obj, "sha256"),
        f"{logical_key}:sha256",
    )
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        _fail("LOCAL_SOURCE_UNREADABLE", f"{source_path}: {exc}")
    actual_sha = digest.hexdigest()
    if stat.st_size != expected_size or actual_sha != expected_sha:
        _fail(
            "LOCAL_SOURCE_DRIFT",
            (
                f"{logical_key}: expected=({expected_size},{expected_sha}) "
                f"actual=({stat.st_size},{actual_sha})"
            ),
        )
    return {
        "content_root": str(root),
        "path": str(source_path),
        "logical_source_key": logical_key,
        "VersionId": _require_text(
            _object_value(obj, "VersionId"),
            f"{logical_key}:VersionId",
        ),
        "sha256": actual_sha,
        "size": stat.st_size,
    }


def build_bound_input_manifest(
    *,
    authority: SourcePreparationAuthority,
    content_root: Path | str,
) -> dict[str, object]:
    """Hash all six local inputs and bind them to one explicit content root."""
    if not isinstance(authority, SourcePreparationAuthority):
        _fail("AUTHORITY_TYPE_INVALID", repr(type(authority)))
    root = Path(content_root).resolve()
    files = [
        verify_local_exact_version(
            root / obj.logical_source_key,
            obj,
            content_root=root,
        )
        for obj in authority.exact_version_objects
    ]
    payload: dict[str, object] = {
        "schema": "round4-stage2-bound-input-manifest-v1",
        "status": "EXACT_VERSION_LOCAL_BYTES_VERIFIED",
        "content_root": str(root),
        "content_root_bound": True,
        "source_dates": list(authority.source_dates),
        "file_count": len(files),
        "files": sorted(
            files,
            key=lambda row: str(row["logical_source_key"]),
        ),
        "authority_sha256": canonical_sha256(authority.receipt()),
    }
    if len(files) != 6:
        _fail("BOUND_INPUT_COUNT_MISMATCH", str(len(files)))
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def is_legal_observed_price(price_e4: int) -> bool:
    # The foundation preserves the price points actually observed in the
    # historical L2.  It does not infer a historical grid from today's market
    # metadata.  Candidate whole-cent filtering happens separately.
    return 1 <= price_e4 <= 9_999


@dataclass(frozen=True)
class SourceEvent:
    kind: str
    market_ticker: str
    recv_mono_ns: int
    recv_wall_ns: int
    source_ordinal: int
    stable_source_id: str
    payload: Mapping[str, object]
    source_rows_sha256: str


@dataclass(frozen=True)
class AtomicEnvelope:
    market_ticker: str
    recv_mono_ns: int
    recv_wall_ns: int
    events: tuple[SourceEvent, ...]
    source_rows_sha256: str
    atomic_no_precedence: bool = True


def _validate_ticker(ticker: object, roster: set[str] | frozenset[str]) -> str:
    text = _require_text(ticker, "market_ticker")
    if text not in roster:
        _fail("MARKET_OUTSIDE_ROSTER", text)
    return text


def _normalize_snapshot_levels(
    raw: object,
    *,
    label: str,
) -> tuple[tuple[int, int], ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:
            _fail("INVALID_BOOK_SNAPSHOT", f"{label}: {exc}")
    if not isinstance(raw, list):
        _fail("INVALID_BOOK_SNAPSHOT", f"{label}: expected list")
    result: list[tuple[int, int]] = []
    seen: set[int] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            _fail("INVALID_BOOK_SNAPSHOT", f"{label}[{index}]")
        price = _require_int(item[0], f"{label}[{index}].price")
        qty = _require_int(
            item[1],
            f"{label}[{index}].qty",
            minimum=1,
        )
        if (
            not is_legal_observed_price(price)
            or price in seen
        ):
            _fail("INVALID_BOOK_SNAPSHOT", f"{label}[{index}]")
        seen.add(price)
        result.append((price, qty))
    return tuple(sorted(result))


def normalize_trade_row(
    raw: Mapping[str, object],
    *,
    source_ordinal: int,
    roster: set[str] | frozenset[str],
) -> SourceEvent:
    ticker = _validate_ticker(raw.get("market_ticker"), roster)
    ordinal = _require_int(source_ordinal, "source_ordinal", minimum=0)
    taker_side = raw.get("taker_side")
    if type(taker_side) is not str:
        _fail("TAKER_SIDE_NOT_VARCHAR", f"{ticker}:{taker_side!r}")
    if taker_side not in ("yes", "no"):
        _fail("TAKER_SIDE_INVALID", f"{ticker}:{taker_side!r}")
    yes_price = _require_int(raw.get("yes_price_e4"), "yes_price_e4")
    no_price = _require_int(raw.get("no_price_e4"), "no_price_e4")
    count = _require_int(raw.get("count_e4"), "count_e4", minimum=1)
    if (
        not is_legal_observed_price(yes_price)
        or not is_legal_observed_price(no_price)
        or yes_price + no_price != 10_000
    ):
        _fail("TRADE_ROW_INVALID", ticker)
    mono = _require_int(
        raw.get("recv_mono_ns"),
        "recv_mono_ns",
        minimum=1,
    )
    wall = _require_int(
        raw.get("recv_wall_ns"),
        "recv_wall_ns",
        minimum=1,
    )
    trade_id = _require_text(raw.get("trade_id"), "trade_id")
    payload: dict[str, object] = {
        "market_ticker": ticker,
        "trade_id": trade_id,
        "yes_price_e4": yes_price,
        "no_price_e4": no_price,
        "count_e4": count,
        "count_fp": Decimal(count) / E4,
        "taker_side": taker_side,
        "recv_mono_ns": mono,
        "recv_wall_ns": wall,
        "source_ordinal": ordinal,
    }
    digest = canonical_sha256(payload)
    return SourceEvent(
        kind="TRADE",
        market_ticker=ticker,
        recv_mono_ns=mono,
        recv_wall_ns=wall,
        source_ordinal=ordinal,
        stable_source_id=f"trade:{trade_id}",
        payload=MappingProxyType(payload),
        source_rows_sha256=digest,
    )


def normalize_l2_row(
    raw: Mapping[str, object],
    *,
    source_ordinal: int,
    roster: set[str] | frozenset[str],
) -> SourceEvent:
    ticker = _validate_ticker(raw.get("market_ticker"), roster)
    ordinal = _require_int(source_ordinal, "source_ordinal", minimum=0)
    mono = _require_int(
        raw.get("recv_mono_ns"),
        "recv_mono_ns",
        minimum=1,
    )
    wall = _require_int(
        raw.get("recv_wall_ns"),
        "recv_wall_ns",
        minimum=1,
    )
    sid = _require_int(raw.get("ws_sid"), "ws_sid", minimum=0)
    seq = _require_int(raw.get("ws_seq"), "ws_seq", minimum=0)
    msg_type = raw.get("msg_type")
    payload: dict[str, object] = {
        "market_ticker": ticker,
        "msg_type": msg_type,
        "recv_mono_ns": mono,
        "recv_wall_ns": wall,
        "ws_sid": sid,
        "ws_seq": seq,
        "source_ordinal": ordinal,
    }
    if msg_type == "snapshot":
        payload["yes_levels"] = _normalize_snapshot_levels(
            raw.get("yes_levels"),
            label="yes_levels",
        )
        payload["no_levels"] = _normalize_snapshot_levels(
            raw.get("no_levels"),
            label="no_levels",
        )
        kind = "BOOK_SNAPSHOT"
    elif msg_type == "delta":
        side = raw.get("side")
        if side not in ("yes", "no"):
            _fail("BOOK_DELTA_INVALID", f"{ticker}:side")
        price = _require_int(raw.get("price_e4"), "price_e4")
        delta = _require_int(raw.get("delta_e4"), "delta_e4")
        if (
            not is_legal_observed_price(price)
            or delta == 0
        ):
            _fail("BOOK_DELTA_INVALID", ticker)
        payload.update(
            {
                "side": side,
                "price_e4": price,
                "delta_e4": delta,
            }
        )
        kind = "BOOK_DELTA"
    else:
        _fail("BOOK_MESSAGE_TYPE_INVALID", f"{ticker}:{msg_type!r}")
    digest = canonical_sha256(payload)
    return SourceEvent(
        kind=kind,
        market_ticker=ticker,
        recv_mono_ns=mono,
        recv_wall_ns=wall,
        source_ordinal=ordinal,
        stable_source_id=f"book:{sid}:{seq}",
        payload=MappingProxyType(payload),
        source_rows_sha256=digest,
    )


def read_trade_csv_rows(
    path: Path | str,
    roster: set[str] | frozenset[str],
) -> tuple[dict[str, object], ...]:
    """Read trades with an explicit VARCHAR override for ``taker_side``."""
    try:
        import duckdb
    except ImportError as exc:
        _fail("DUCKDB_UNAVAILABLE", str(exc))
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("CREATE TEMP TABLE stage2_roster(ticker VARCHAR)")
        connection.executemany(
            "INSERT INTO stage2_roster VALUES (?)",
            [(ticker,) for ticker in sorted(roster)],
        )
        # Do not select ts_utc/exchange_ts_us.  Receive clocks are the only
        # causal clocks admitted to the source-preparation boundary.
        cursor = connection.execute(
            """
            SELECT
                t.market_ticker,
                t.trade_id,
                t.yes_price_e4,
                t.no_price_e4,
                t.count_e4,
                CAST(t.taker_side AS VARCHAR) AS taker_side,
                t.recv_wall_ns,
                t.recv_mono_ns
            FROM read_csv(
                ?,
                header = true,
                types = {'taker_side': 'VARCHAR'}
            ) AS t
            JOIN stage2_roster AS r
              ON r.ticker = t.market_ticker
            ORDER BY
                t.recv_mono_ns,
                t.recv_wall_ns,
                t.trade_id
            """,
            [str(Path(path))],
        )
        names = [item[0] for item in cursor.description]
        return tuple(dict(zip(names, row)) for row in cursor.fetchall())
    except Stage2ExtractionError:
        raise
    except Exception as exc:
        _fail("TRADE_SOURCE_READ_FAILED", str(exc))
    finally:
        connection.close()


def read_l2_parquet_rows(
    path: Path | str,
    roster: set[str] | frozenset[str],
) -> tuple[dict[str, object], ...]:
    """Read only roster L2 fields, ordered strictly by receive clocks."""
    try:
        import duckdb
    except ImportError as exc:
        _fail("DUCKDB_UNAVAILABLE", str(exc))
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("CREATE TEMP TABLE stage2_roster(ticker VARCHAR)")
        connection.executemany(
            "INSERT INTO stage2_roster VALUES (?)",
            [(ticker,) for ticker in sorted(roster)],
        )
        # ts_utc/exchange_ts_us/local_recv_ts_us are intentionally absent.
        cursor = connection.execute(
            """
            SELECT
                b.market_ticker,
                b.msg_type,
                b.side,
                b.price_e4,
                b.delta_e4,
                b.yes_levels,
                b.no_levels,
                b.ws_sid,
                b.ws_seq,
                b.recv_wall_ns,
                b.recv_mono_ns
            FROM read_parquet(?) AS b
            JOIN stage2_roster AS r
              ON r.ticker = b.market_ticker
            ORDER BY
                b.recv_mono_ns,
                b.recv_wall_ns,
                b.ws_sid,
                b.ws_seq
            """,
            [str(Path(path))],
        )
        names = [item[0] for item in cursor.description]
        return tuple(dict(zip(names, row)) for row in cursor.fetchall())
    except Stage2ExtractionError:
        raise
    except Exception as exc:
        _fail("L2_SOURCE_READ_FAILED", str(exc))
    finally:
        connection.close()


def group_atomic_envelopes(
    events: Iterable[SourceEvent],
) -> tuple[AtomicEnvelope, ...]:
    """Group exact receive-clock ties without assigning channel precedence."""
    rows = tuple(events)
    walls_by_market_mono: dict[tuple[str, int], set[int]] = {}
    grouped: dict[tuple[int, int, str], list[SourceEvent]] = {}
    for event in rows:
        if not isinstance(event, SourceEvent):
            _fail("EVENT_TYPE_INVALID", repr(type(event)))
        walls_by_market_mono.setdefault(
            (event.market_ticker, event.recv_mono_ns),
            set(),
        ).add(event.recv_wall_ns)
        grouped.setdefault(
            (
                event.recv_mono_ns,
                event.recv_wall_ns,
                event.market_ticker,
            ),
            [],
        ).append(event)
    conflicts = [
        key for key, walls in walls_by_market_mono.items() if len(walls) != 1
    ]
    if conflicts:
        _fail("CLOCK_PAIR_CONFLICT", repr(sorted(conflicts)[:3]))

    envelopes: list[AtomicEnvelope] = []
    last_clock: dict[str, tuple[int, int]] = {}
    for (mono, wall, ticker), members in sorted(grouped.items()):
        prior = last_clock.get(ticker)
        if prior is not None and (mono <= prior[0] or wall <= prior[1]):
            _fail(
                "RECEIVE_CLOCK_DIVERGENCE",
                f"{ticker}: prior={prior} current={(mono, wall)}",
            )
        last_clock[ticker] = (mono, wall)
        ordered = tuple(
            sorted(
                members,
                key=lambda item: (
                    item.kind,
                    item.stable_source_id,
                    item.source_ordinal,
                    item.source_rows_sha256,
                ),
            )
        )
        identities = [
            (item.kind, item.stable_source_id) for item in ordered
        ]
        if len(identities) != len(set(identities)):
            _fail("DUPLICATE_SOURCE_EVENT", f"{ticker}:{mono}:{wall}")
        digest = canonical_sha256(
            {
                "market_ticker": ticker,
                "recv_mono_ns": mono,
                "recv_wall_ns": wall,
                "atomic_no_precedence": True,
                "events": [
                    {
                        "kind": item.kind,
                        "stable_source_id": item.stable_source_id,
                        "source_rows_sha256": item.source_rows_sha256,
                        "payload": item.payload,
                    }
                    for item in ordered
                ],
            }
        )
        envelopes.append(
            AtomicEnvelope(
                market_ticker=ticker,
                recv_mono_ns=mono,
                recv_wall_ns=wall,
                events=ordered,
                source_rows_sha256=digest,
            )
        )
    return tuple(envelopes)


class CausalBookReconstructor:
    """Streaming public-book state with explicit left-truncation sentinels."""

    def __init__(self, roster: set[str] | frozenset[str]) -> None:
        if not roster:
            _fail("ROSTER_EMPTY", "book reconstructor")
        self._roster = frozenset(roster)
        self._books: dict[str, dict[str, dict[int, int]]] = {
            ticker: {"yes": {}, "no": {}} for ticker in self._roster
        }
        self._usable = {ticker: False for ticker in self._roster}
        self._latest: dict[str, AtomicEnvelope | None] = {
            ticker: None for ticker in self._roster
        }
        self._coverage: dict[str, dict[str, object]] = {
            ticker: {
                "first_valid_snapshot_recv_mono_ns": None,
                "first_valid_snapshot_recv_wall_ns": None,
                "ignored_pre_snapshot_deltas": 0,
                "invalid_or_empty_snapshots": 0,
                "causal_gap_count": 0,
                "trade_rows": 0,
                "l2_rows": 0,
                "observed_yes_price_count": 0,
                "observed_no_price_count": 0,
            }
            for ticker in self._roster
        }
        self._observed: dict[str, dict[str, set[int]]] = {
            ticker: {"yes": set(), "no": set()} for ticker in self._roster
        }

    def is_usable(self, ticker: str) -> bool:
        if ticker not in self._roster:
            _fail("MARKET_OUTSIDE_ROSTER", ticker)
        return self._usable[ticker]

    def coverage(self, ticker: str) -> dict[str, object]:
        if ticker not in self._roster:
            _fail("MARKET_OUTSIDE_ROSTER", ticker)
        row = dict(self._coverage[ticker])
        row["observed_yes_price_count"] = len(
            self._observed[ticker]["yes"]
        )
        row["observed_no_price_count"] = len(
            self._observed[ticker]["no"]
        )
        return row

    def book(self, ticker: str) -> dict[str, dict[int, int]]:
        if not self.is_usable(ticker):
            _fail("BOOK_UNUSABLE", ticker)
        return {
            side: dict(levels)
            for side, levels in self._books[ticker].items()
        }

    def latest_envelope(self, ticker: str) -> AtomicEnvelope:
        if not self.is_usable(ticker):
            _fail("BOOK_UNUSABLE", ticker)
        envelope = self._latest[ticker]
        if envelope is None:
            _fail("BOOK_UNUSABLE", ticker)
        return envelope

    def consume_atomic(self, envelope: AtomicEnvelope) -> None:
        ticker = envelope.market_ticker
        if ticker not in self._roster:
            _fail("MARKET_OUTSIDE_ROSTER", ticker)
        coverage = self._coverage[ticker]
        trade_events = [
            item for item in envelope.events if item.kind == "TRADE"
        ]
        book_events = [
            item
            for item in envelope.events
            if item.kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
        ]
        coverage["trade_rows"] = int(coverage["trade_rows"]) + len(
            trade_events
        )
        coverage["l2_rows"] = int(coverage["l2_rows"]) + len(book_events)
        if not book_events:
            return

        snapshots = [
            item for item in book_events if item.kind == "BOOK_SNAPSHOT"
        ]
        deltas = [
            item for item in book_events if item.kind == "BOOK_DELTA"
        ]
        if len(snapshots) > 1 or (snapshots and deltas):
            _fail(
                "AMBIGUOUS_ATOMIC_BOOK_TIE",
                f"{ticker}:{envelope.recv_mono_ns}",
            )
        if snapshots:
            payload = snapshots[0].payload
            yes = dict(payload["yes_levels"])
            no = dict(payload["no_levels"])
            if not yes and not no:
                self._books[ticker] = {"yes": {}, "no": {}}
                self._usable[ticker] = False
                self._latest[ticker] = None
                coverage["invalid_or_empty_snapshots"] = (
                    int(coverage["invalid_or_empty_snapshots"]) + 1
                )
                coverage["causal_gap_count"] = (
                    int(coverage["causal_gap_count"]) + 1
                )
                return
            if yes and no and max(yes) + max(no) >= 10_000:
                _fail(
                    "CROSSED_BOOK_SNAPSHOT",
                    f"{ticker}:{envelope.recv_mono_ns}",
                )
            self._books[ticker] = {"yes": yes, "no": no}
            self._usable[ticker] = True
            self._latest[ticker] = envelope
            self._observed[ticker]["yes"].update(yes)
            self._observed[ticker]["no"].update(no)
            if coverage["first_valid_snapshot_recv_mono_ns"] is None:
                coverage["first_valid_snapshot_recv_mono_ns"] = (
                    envelope.recv_mono_ns
                )
                coverage["first_valid_snapshot_recv_wall_ns"] = (
                    envelope.recv_wall_ns
                )
            return

        if not self._usable[ticker]:
            coverage["ignored_pre_snapshot_deltas"] = (
                int(coverage["ignored_pre_snapshot_deltas"]) + len(deltas)
            )
            return
        aggregated: dict[tuple[str, int], int] = {}
        for event in deltas:
            side = str(event.payload["side"])
            price = int(event.payload["price_e4"])
            delta = int(event.payload["delta_e4"])
            aggregated[(side, price)] = (
                aggregated.get((side, price), 0) + delta
            )
            self._observed[ticker][side].add(price)
        for (side, price), delta in sorted(aggregated.items()):
            prior = self._books[ticker][side].get(price, 0)
            updated = prior + delta
            if updated < 0:
                _fail(
                    "NEGATIVE_BOOK_LEVEL",
                    f"{ticker}:{side}:{price}:{updated}",
                )
            if updated == 0:
                self._books[ticker][side].pop(price, None)
            else:
                self._books[ticker][side][price] = updated
        if not self._books[ticker]["yes"] and not self._books[ticker]["no"]:
            self._usable[ticker] = False
            self._latest[ticker] = None
            coverage["causal_gap_count"] = (
                int(coverage["causal_gap_count"]) + 1
            )
        else:
            self._latest[ticker] = envelope


def visible_fok_levels(
    replay: CausalBookReconstructor,
    *,
    ticker: str,
    flatten_book_side: str,
    limit_price_e4: int,
) -> tuple[dict[str, object], ...]:
    """Return only observed, whole-cent, causally current public levels."""
    if not is_legal_observed_price(limit_price_e4):
        _fail("FOK_LIMIT_OFF_GRID", str(limit_price_e4))
    if limit_price_e4 % 100 != 0:
        _fail("FOK_LIMIT_SENSITIVITY_ONLY", str(limit_price_e4))
    book = replay.book(ticker)
    levels: list[tuple[int, int]]
    if flatten_book_side == "ASK":
        levels = [
            (price, qty)
            for price, qty in book["yes"].items()
            if price >= limit_price_e4 and price % 100 == 0
        ]
        levels.sort(reverse=True)
    elif flatten_book_side == "BID":
        levels = [
            (10_000 - no_price, qty)
            for no_price, qty in book["no"].items()
            if 10_000 - no_price <= limit_price_e4
            and (10_000 - no_price) % 100 == 0
        ]
        levels.sort()
    else:
        _fail("FOK_BOOK_SIDE_INVALID", flatten_book_side)
    return tuple(
        {
            "price_e4": price,
            "qty_fp": Decimal(qty) / E4,
        }
        for price, qty in levels
    )


def normalize_terminal_labels(
    rows: Iterable[Mapping[str, object]],
    *,
    roster: set[str] | frozenset[str],
) -> dict[str, dict[str, object]]:
    """Normalize terminal facts into a physically separate label table."""
    labels: dict[str, dict[str, object]] = {}
    for raw in rows:
        ticker = _validate_ticker(raw.get("market_ticker"), roster)
        if ticker in labels:
            _fail("DUPLICATE_TERMINAL_LABEL", ticker)
        result = raw.get("official_market_result")
        if raw.get("market_status") != "FINALIZED" or result not in (
            "YES",
            "NO",
        ):
            _fail("TERMINAL_NOT_FINALIZED", ticker)
        labels[ticker] = {
            "market_ticker": ticker,
            "market_id": _require_text(raw.get("market_id"), "market_id"),
            "market_status": "FINALIZED",
            "official_market_result": result,
            "settlement_recv_wall_ns": _require_int(
                raw.get("settlement_recv_wall_ns"),
                "settlement_recv_wall_ns",
                minimum=1,
            ),
            "settlement_recv_mono_ns": _require_int(
                raw.get("settlement_recv_mono_ns"),
                "settlement_recv_mono_ns",
                minimum=1,
            ),
            "settlement_stable_source_id": _require_text(
                raw.get("settlement_stable_source_id"),
                "settlement_stable_source_id",
            ),
            "source_rows_sha256": _require_sha(
                raw.get("source_rows_sha256"),
                "terminal source_rows_sha256",
            ),
            "role": "LABEL_ONLY",
            "feature_eligible": False,
        }
    return {ticker: labels[ticker] for ticker in sorted(labels)}


def _validate_complete_roster(
    roster: Sequence[str],
    coverage_by_market: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    normalized = tuple(sorted(roster))
    if (
        len(normalized) != 72
        or len(set(normalized)) != 72
        or frozenset(normalized) != EXPECTED_ROSTER_SET
        or set(coverage_by_market) != EXPECTED_ROSTER_SET
    ):
        _fail(
            "ROSTER_INCOMPLETE",
            (
                f"roster={len(normalized)} "
                f"coverage={len(coverage_by_market)} expected=72"
            ),
        )
    for ticker in normalized:
        coverage = coverage_by_market[ticker]
        if (
            type(coverage.get("first_valid_snapshot_recv_mono_ns")) is not int
            or type(coverage.get("first_valid_snapshot_recv_wall_ns"))
            is not int
        ):
            _fail("ROSTER_INCOMPLETE", f"{ticker}:no valid snapshot")
    return normalized


def build_extraction_receipt(
    *,
    authority: SourcePreparationAuthority,
    input_manifest: Mapping[str, object],
    roster: Sequence[str],
    coverage_by_market: Mapping[str, Mapping[str, object]],
    causal_rows_sha256: str,
    terminal_labels_sha256: str,
    causal_row_count: int,
    terminal_label_count: int,
) -> dict[str, object]:
    """Build a deterministic source-preparation receipt, never a V4.1 seal."""
    if not isinstance(authority, SourcePreparationAuthority):
        _fail("AUTHORITY_TYPE_INVALID", repr(type(authority)))
    if authority.extraction_allowed:
        _fail("UNEXPECTED_EXTRACTION_AUTHORITY", "adapter must be pending")
    if (
        input_manifest.get("schema")
        != "round4-stage2-bound-input-manifest-v1"
        or input_manifest.get("status")
        != "EXACT_VERSION_LOCAL_BYTES_VERIFIED"
        or input_manifest.get("content_root_bound") is not True
        or input_manifest.get("file_count") != 6
        or input_manifest.get("authority_sha256")
        != canonical_sha256(authority.receipt())
    ):
        _fail("BOUND_INPUT_MANIFEST_INVALID", "manifest/authority mismatch")
    supplied_manifest_sha = _require_sha(
        input_manifest.get("payload_sha256"),
        "input manifest payload_sha256",
    )
    manifest_without_hash = dict(input_manifest)
    del manifest_without_hash["payload_sha256"]
    if canonical_sha256(manifest_without_hash) != supplied_manifest_sha:
        _fail("BOUND_INPUT_MANIFEST_INVALID", "payload hash mismatch")
    manifest_files = input_manifest.get("files")
    if not isinstance(manifest_files, list):
        _fail("BOUND_INPUT_MANIFEST_INVALID", "files missing")
    expected_file_bindings = {
        (
            item.logical_source_key,
            item.version_id,
            item.sha256,
            item.size,
        )
        for item in authority.exact_version_objects
    }
    actual_file_bindings = {
        (
            item.get("logical_source_key"),
            item.get("VersionId"),
            item.get("sha256"),
            item.get("size"),
        )
        for item in manifest_files
        if isinstance(item, Mapping)
    }
    if actual_file_bindings != expected_file_bindings:
        _fail("BOUND_INPUT_MANIFEST_INVALID", "file bindings mismatch")
    normalized_roster = _validate_complete_roster(
        roster,
        coverage_by_market,
    )
    causal_sha = _require_sha(causal_rows_sha256, "causal rows sha")
    labels_sha = _require_sha(
        terminal_labels_sha256,
        "terminal labels sha",
    )
    causal_count = _require_int(
        causal_row_count,
        "causal_row_count",
        minimum=0,
    )
    label_count = _require_int(
        terminal_label_count,
        "terminal_label_count",
        minimum=0,
    )
    if label_count != 72:
        _fail("TERMINAL_LABEL_ROSTER_INCOMPLETE", str(label_count))
    coverage = {
        ticker: {
            str(key): value
            for key, value in sorted(coverage_by_market[ticker].items())
        }
        for ticker in normalized_roster
    }
    markets_per_day = {
        day: sum(
            MARKET_SOURCE_DAY[ticker] == day
            for ticker in normalized_roster
        )
        for day in DISCOVERY_DATES
    }
    payload: dict[str, Any] = {
        "schema": SOURCE_PREPARATION_SCHEMA,
        "status": "SOURCE_PREPARATION_ONLY",
        "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        "extraction_authorized": False,
        "fit_authorized": FIT_AUTHORIZED,
        "candidate_selection_authorized": CANDIDATE_SELECTION_AUTHORIZED,
        "live_authorized": LIVE_AUTHORIZED,
        "source_dates": list(DISCOVERY_DATES),
        "forbidden_dates_read": False,
        "market_count": 72,
        "markets_per_day": markets_per_day,
        "market_roster": list(normalized_roster),
        "market_roster_sha256": canonical_sha256(normalized_roster),
        "coverage_by_market": coverage,
        "all_markets_have_valid_snapshot": True,
        "clock_contract": {
            "primary": "recv_mono_ns",
            "auxiliary": "recv_wall_ns",
            "exchange_ts_used_for_causal_order": False,
            "exact_clock_ties": "ATOMIC_NO_PRECEDENCE",
        },
        "left_truncation_contract": (
            "all rows before each market's first valid snapshot are "
            "feature-ineligible"
        ),
        "observed_price_contract": (
            "only causally visible source levels; no synthetic grid levels"
        ),
        "terminal_result_feature_use": False,
        "terminal_result_role": "LABEL_ONLY",
        "causal_row_count": causal_count,
        "causal_rows_sha256": causal_sha,
        "terminal_label_count": label_count,
        "terminal_labels_sha256": labels_sha,
        "input_authority": authority.receipt(),
        "bound_input_manifest": dict(input_manifest),
        "input_exact_version_objects": [
            item.receipt() for item in authority.exact_version_objects
        ],
        "rejected_v4_1": {
            "contract_version": REJECTED_V41_CONTRACT_VERSION,
            "artifact_sha256": dict(REJECTED_V41_ARTIFACT_SHA256),
            "decision": "REJECT_NO_SEAL",
            "reason_codes": list(V41_REJECTION_REASON_CODES),
        },
        "required_next": (
            "install and independently accept a V4.2 adapter before "
            "building contract rows"
        ),
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return _jsonable(payload)  # type: ignore[return-value]
