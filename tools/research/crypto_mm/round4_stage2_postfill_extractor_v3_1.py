#!/usr/bin/env python3
"""V3.1 commit-authority seal for the frozen V3 source transform.

V3 remains useful as a deterministic source-preparation result, but its
public receipt is intentionally not an authority root: a caller can replace
typed rows and recompute all self-derived hashes.  V3.1 closes that boundary:

* dry-run receipts have a distinct non-authority schema and status;
* commit verification requires an externally sealed, typed configuration
  that pins the authority, V2 manifest, content root, and three daily builder
  receipts;
* the six exact parents are rebuilt from private controlled copies at commit
  time;
* a second ordered normalized-event spine is derived directly from those
  copies and compared with the candidate result; and
* only the exact, process-registered ``CommitVerifiedResultV31`` instance may
  emit an authoritative commit receipt.

This layer still does not install or authorize V4.2.  The durable eligibility
chain is an explicit upstream trust boundary, and all extraction, fitting,
candidate, shadow, and live permissions remain false.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
import tempfile

from tools.research.crypto_mm import round4_stage2_postfill_extractor as V1
from tools.research.crypto_mm import round4_stage2_postfill_extractor_v2 as V2
from tools.research.crypto_mm import round4_stage2_postfill_extractor_v3 as V3


SEALED_CONFIG_SCHEMA = "round4-stage2-v3.1-external-sealed-config-v1"
COMMIT_RESULT_SCHEMA = "round4-stage2-v3.1-commit-verified-result-v1"
COMMIT_RECEIPT_SCHEMA = "round4-stage2-v3.1-authoritative-commit-receipt-v1"
DRY_RUN_SCHEMA = "round4-stage2-v3.1-dry-run-receipt-v1"
COMMIT_STATUS = "COMMIT_VERIFIED_SOURCE_TRANSFORM_ONLY"
DRY_RUN_STATUS = "DRY_RUN_NON_AUTHORITY"
CONTRACT_ADAPTER_STATUS = "V4_2_PENDING"

_EXTERNAL_SEAL_TOKEN = object()
_COMMIT_FACTORY_TOKEN = object()
_SEALED_CONFIG_REGISTRY: dict[
    int,
    tuple["ExternalSealedCommitConfigV31", bytes],
] = {}
_COMMIT_REGISTRY: dict[
    int,
    tuple["CommitVerifiedResultV31", bytes],
] = {}


class Stage2CommitV31Error(RuntimeError):
    """A sealed-config, private-rebuild, or commit receipt invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise Stage2CommitV31Error(code, detail)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and V1.SHA256_RE.fullmatch(value) is not None
    )


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        _fail("INVALID_SHA256", label)
    return str(value)


@dataclass(frozen=True)
class DailyBuilderReceiptPinV31:
    source_date_utc: str
    eligible_day_authority_sha256: str
    upstream_builder_receipt_sha256: str

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "eligible_day_authority_sha256": (
                self.eligible_day_authority_sha256
            ),
            "upstream_builder_receipt_sha256": (
                self.upstream_builder_receipt_sha256
            ),
        }


@dataclass(frozen=True)
class ExternalSealedCommitConfigV31:
    schema: str
    status: str
    authority_payload_sha256: str
    bound_manifest_payload_sha256: str
    content_root: str
    daily_builder_receipt_pins: tuple[DailyBuilderReceiptPinV31, ...]
    daily_builder_receipt_pin_set_sha256: str
    external_seal_receipt_sha256: str
    config_payload_sha256: str
    v4_2_connection_authorized: bool = False
    _seal_token: object = field(
        default=None,
        repr=False,
        compare=False,
    )

    def receipt_without_payload_hash(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "authority_payload_sha256": self.authority_payload_sha256,
            "bound_manifest_payload_sha256": (
                self.bound_manifest_payload_sha256
            ),
            "content_root": self.content_root,
            "daily_builder_receipt_pins": [
                item.receipt() for item in self.daily_builder_receipt_pins
            ],
            "daily_builder_receipt_pin_set_sha256": (
                self.daily_builder_receipt_pin_set_sha256
            ),
            "external_seal_receipt_sha256": (
                self.external_seal_receipt_sha256
            ),
            "v4_2_connection_authorized": (
                self.v4_2_connection_authorized
            ),
        }

    def receipt(self) -> dict[str, object]:
        payload = self.receipt_without_payload_hash()
        payload["config_payload_sha256"] = self.config_payload_sha256
        return payload


@dataclass(frozen=True)
class OrderedNormalizedEventV31:
    parent_event_index: int
    source_date_utc: str
    table: str
    parent_logical_source_key: str
    parent_version_id: str
    parent_sha256: str
    parent_size: int
    content_root: str
    market_ticker: str
    recv_mono_ns: int
    recv_wall_ns: int
    kind: str
    stable_source_id: str
    source_ordinal: int
    raw_row_sha256: str
    normalized_source_rows_sha256: str
    normalized_payload_canonical_json: str
    atomic_envelope_sha256: str
    atomic_envelope_event_count: int
    atomic_no_precedence: bool = True

    def receipt(self) -> dict[str, object]:
        return {
            "parent_event_index": self.parent_event_index,
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "parent_logical_source_key": self.parent_logical_source_key,
            "parent_version_id": self.parent_version_id,
            "parent_sha256": self.parent_sha256,
            "parent_size": self.parent_size,
            "content_root": self.content_root,
            "market_ticker": self.market_ticker,
            "recv_mono_ns": self.recv_mono_ns,
            "recv_wall_ns": self.recv_wall_ns,
            "kind": self.kind,
            "stable_source_id": self.stable_source_id,
            "source_ordinal": self.source_ordinal,
            "raw_row_sha256": self.raw_row_sha256,
            "normalized_source_rows_sha256": (
                self.normalized_source_rows_sha256
            ),
            "normalized_payload_canonical_json": (
                self.normalized_payload_canonical_json
            ),
            "atomic_envelope_sha256": self.atomic_envelope_sha256,
            "atomic_envelope_event_count": (
                self.atomic_envelope_event_count
            ),
            "atomic_no_precedence": self.atomic_no_precedence,
        }

    def candidate_projection(self) -> dict[str, object]:
        return {
            "parent_event_index": self.parent_event_index,
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "parent_logical_source_key": self.parent_logical_source_key,
            "parent_version_id": self.parent_version_id,
            "parent_sha256": self.parent_sha256,
            "parent_size": self.parent_size,
            "content_root": self.content_root,
            "market_ticker": self.market_ticker,
            "recv_mono_ns": self.recv_mono_ns,
            "recv_wall_ns": self.recv_wall_ns,
            "kind": self.kind,
            "stable_source_id": self.stable_source_id,
            "source_ordinal": self.source_ordinal,
            "normalized_source_rows_sha256": (
                self.normalized_source_rows_sha256
            ),
            "atomic_envelope_sha256": self.atomic_envelope_sha256,
            "atomic_envelope_event_count": (
                self.atomic_envelope_event_count
            ),
            "atomic_no_precedence": self.atomic_no_precedence,
        }


@dataclass(frozen=True)
class ParentOrderedEventSpineV31:
    source_date_utc: str
    table: str
    parent_logical_source_key: str
    parent_version_id: str
    parent_sha256: str
    parent_size: int
    events: tuple[OrderedNormalizedEventV31, ...]
    event_count: int
    ordered_event_spine_sha256: str

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "parent_logical_source_key": self.parent_logical_source_key,
            "parent_version_id": self.parent_version_id,
            "parent_sha256": self.parent_sha256,
            "parent_size": self.parent_size,
            "event_count": self.event_count,
            "ordered_event_spine_sha256": (
                self.ordered_event_spine_sha256
            ),
            "events": [item.receipt() for item in self.events],
        }


@dataclass(frozen=True)
class CommitVerifiedResultV31:
    schema: str
    status: str
    sealed_config: ExternalSealedCommitConfigV31
    sealed_config_payload_sha256: str
    candidate_transformation_root_sha256: str
    rebuilt_transformation_root_sha256: str
    rebuilt_v3_state_spine_sha256: str
    parent_event_spines: tuple[ParentOrderedEventSpineV31, ...]
    ordered_event_count: int
    ordered_event_spine_set_sha256: str
    candidate_projection_sha256: str
    private_rebuild_projection_sha256: str
    commit_verified: bool = True
    contract_adapter_status: str = CONTRACT_ADAPTER_STATUS
    extraction_authorized: bool = False
    fit_authorized: bool = False
    candidate_selection_authorized: bool = False
    shadow_authorized: bool = False
    live_authorized: bool = False
    _factory_token: object = field(
        default=None,
        repr=False,
        compare=False,
    )


def _eligible_day_authority_sha256(day: V1.EligibleDay) -> str:
    return V1.canonical_sha256(
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
            "exact_version_objects": [
                item.receipt() for item in day.exact_version_objects
            ],
        }
    )


def _issue_externally_sealed_config_v31(
    *,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
    upstream_builder_receipt_pins: Mapping[str, str],
    external_seal_receipt_sha256: str,
) -> ExternalSealedCommitConfigV31:
    """Private admission hook for an upstream external-seal provider.

    There is deliberately no CLI path to this function.  Production wiring
    must keep the resulting typed object in-process after the upstream
    verifier has authenticated its own seal.
    """
    if type(authority) is not V1.SourcePreparationAuthority:
        _fail("AUTHORITY_TYPE_INVALID", repr(type(authority)))
    if type(input_manifest) is not V2.BoundInputManifestV2:
        _fail("TYPED_BOUND_MANIFEST_REQUIRED", repr(type(input_manifest)))
    if set(upstream_builder_receipt_pins) != set(V1.DISCOVERY_DATES):
        _fail(
            "DAILY_BUILDER_PIN_SET_INVALID",
            repr(sorted(upstream_builder_receipt_pins)),
        )
    _require_sha256(
        external_seal_receipt_sha256,
        "external seal receipt",
    )
    try:
        rebound = V2.build_bound_input_manifest_v2(
            authority=authority,
            content_root=content_root,
        )
    except V2.Stage2PreparationV2Error as exc:
        _fail(exc.code, exc.detail)
    if rebound != input_manifest:
        _fail(
            "BOUND_MANIFEST_REBIND_MISMATCH",
            input_manifest.payload_sha256,
        )
    days_by_date = {
        day.source_date_utc: day for day in authority.eligible_days
    }
    pins: list[DailyBuilderReceiptPinV31] = []
    for source_date in V1.DISCOVERY_DATES:
        upstream_sha = _require_sha256(
            upstream_builder_receipt_pins[source_date],
            f"{source_date}:upstream builder receipt",
        )
        pins.append(
            DailyBuilderReceiptPinV31(
                source_date_utc=source_date,
                eligible_day_authority_sha256=(
                    _eligible_day_authority_sha256(
                        days_by_date[source_date]
                    )
                ),
                upstream_builder_receipt_sha256=upstream_sha,
            )
        )
    pin_set_sha = V1.canonical_sha256(
        [item.receipt() for item in pins]
    )
    config = ExternalSealedCommitConfigV31(
        schema=SEALED_CONFIG_SCHEMA,
        status="EXTERNALLY_SEALED_COMMIT_CONFIG",
        authority_payload_sha256=V1.canonical_sha256(
            authority.receipt()
        ),
        bound_manifest_payload_sha256=input_manifest.payload_sha256,
        content_root=input_manifest.content_root,
        daily_builder_receipt_pins=tuple(pins),
        daily_builder_receipt_pin_set_sha256=pin_set_sha,
        external_seal_receipt_sha256=external_seal_receipt_sha256,
        config_payload_sha256="0" * 64,
        _seal_token=_EXTERNAL_SEAL_TOKEN,
    )
    sealed = replace(
        config,
        config_payload_sha256=V1.canonical_sha256(
            config.receipt_without_payload_hash()
        ),
    )
    _SEALED_CONFIG_REGISTRY[id(sealed)] = (
        sealed,
        V1._canonical_bytes(sealed.receipt()),
    )
    return sealed


def _validate_sealed_config(
    config: ExternalSealedCommitConfigV31,
    *,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
) -> None:
    if type(config) is not ExternalSealedCommitConfigV31:
        _fail(
            "EXTERNAL_SEALED_CONFIG_REQUIRED",
            repr(type(config)),
        )
    registered = _SEALED_CONFIG_REGISTRY.get(id(config))
    if (
        config._seal_token is not _EXTERNAL_SEAL_TOKEN
        or registered is None
        or registered[0] is not config
        or registered[1] != V1._canonical_bytes(config.receipt())
    ):
        _fail(
            "REGISTERED_EXTERNAL_SEALED_CONFIG_REQUIRED",
            "config was not the exact externally admitted instance",
        )
    if (
        config.schema != SEALED_CONFIG_SCHEMA
        or config.status != "EXTERNALLY_SEALED_COMMIT_CONFIG"
        or config.v4_2_connection_authorized is not False
        or config.config_payload_sha256
        != V1.canonical_sha256(config.receipt_without_payload_hash())
    ):
        _fail("SEALED_CONFIG_INVALID", "header/payload")
    for value, label in (
        (config.authority_payload_sha256, "authority pin"),
        (config.bound_manifest_payload_sha256, "manifest pin"),
        (
            config.daily_builder_receipt_pin_set_sha256,
            "daily builder pin set",
        ),
        (config.external_seal_receipt_sha256, "external seal"),
    ):
        _require_sha256(value, label)
    if (
        config.authority_payload_sha256
        != V1.canonical_sha256(authority.receipt())
        or config.bound_manifest_payload_sha256
        != input_manifest.payload_sha256
        or config.content_root != input_manifest.content_root
        or str(Path(content_root).resolve()) != config.content_root
    ):
        _fail("SEALED_CONFIG_INPUT_MISMATCH", "authority/manifest/root")
    if (
        len(config.daily_builder_receipt_pins) != 3
        or tuple(
            item.source_date_utc
            for item in config.daily_builder_receipt_pins
        )
        != V1.DISCOVERY_DATES
        or config.daily_builder_receipt_pin_set_sha256
        != V1.canonical_sha256(
            [item.receipt() for item in config.daily_builder_receipt_pins]
        )
    ):
        _fail("DAILY_BUILDER_PIN_SET_INVALID", "expected exact three days")
    days_by_date = {
        day.source_date_utc: day for day in authority.eligible_days
    }
    for item in config.daily_builder_receipt_pins:
        if (
            type(item) is not DailyBuilderReceiptPinV31
            or item.eligible_day_authority_sha256
            != _eligible_day_authority_sha256(
                days_by_date[item.source_date_utc]
            )
        ):
            _fail(
                "DAILY_BUILDER_PIN_MISMATCH",
                item.source_date_utc,
            )
        _require_sha256(
            item.upstream_builder_receipt_sha256,
            f"{item.source_date_utc}:builder receipt",
        )


def _v31_transform_code_sha256() -> str:
    try:
        own_payload = Path(__file__).read_bytes()
    except OSError as exc:
        _fail("TRANSFORM_CODE_UNREADABLE", str(exc))
    _v3_components, v3_code_sha = V3._transform_code_identity()
    return V1.canonical_sha256(
        {
            "v3_transform_code_sha256": v3_code_sha,
            "v3_1_module_sha256": hashlib.sha256(own_payload).hexdigest(),
        }
    )


@dataclass(frozen=True)
class _RawEventBuild:
    parent: V2.BoundExactParentV2
    raw_row_sha256: str
    event: V1.SourceEvent
    normalized_payload_canonical_json: str


def _private_parent_ordered_event_spines(
    *,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
) -> tuple[tuple[ParentOrderedEventSpineV31, ...], str, int]:
    try:
        rebound = V2.build_bound_input_manifest_v2(
            authority=authority,
            content_root=content_root,
        )
    except V2.Stage2PreparationV2Error as exc:
        _fail(exc.code, exc.detail)
    if rebound != input_manifest:
        _fail(
            "BOUND_MANIFEST_REBIND_MISMATCH",
            input_manifest.payload_sha256,
        )
    code_sha = _v31_transform_code_sha256()
    root_lexical = V2._lexical_absolute(content_root)
    root_resolved = Path(rebound.content_root)
    rows_by_parent: dict[
        tuple[str, str],
        tuple[dict[str, object], ...],
    ] = {}
    with tempfile.TemporaryDirectory(
        prefix="round4-stage2-v31-private-",
    ) as snapshot_directory:
        for index, parent in enumerate(rebound.parents):
            suffix = (
                ".parquet"
                if parent.table == "orderbooks_full"
                else ".csv.gz"
            )
            _read_receipt, selected_rows = V3._read_parent_to_eof(
                parent=parent,
                root_lexical=root_lexical,
                root_resolved=root_resolved,
                snapshot_path=(
                    Path(snapshot_directory) / f"parent-{index}{suffix}"
                ),
                transform_code_sha256=code_sha,
            )
            rows_by_parent[
                (parent.source_date_utc, parent.table)
            ] = selected_rows

    parent_by_key = {
        (parent.source_date_utc, parent.table): parent
        for parent in rebound.parents
    }
    built_events: list[_RawEventBuild] = []
    for table in ("orderbooks_full", "trades"):
        raw_with_parent: list[
            tuple[V2.BoundExactParentV2, dict[str, object]]
        ] = []
        for source_date in V1.DISCOVERY_DATES:
            parent = parent_by_key[(source_date, table)]
            raw_with_parent.extend(
                (parent, dict(raw))
                for raw in rows_by_parent[(source_date, table)]
            )
        raw_with_parent.sort(
            key=lambda item: V1.canonical_sha256(item[1])
        )
        for ordinal, (parent, raw) in enumerate(raw_with_parent):
            if table == "orderbooks_full":
                event = V1.normalize_l2_row(
                    raw,
                    source_ordinal=ordinal,
                    roster=V1.EXPECTED_ROSTER_SET,
                )
            else:
                event = V1.normalize_trade_row(
                    raw,
                    source_ordinal=ordinal,
                    roster=V1.EXPECTED_ROSTER_SET,
                )
            built_events.append(
                _RawEventBuild(
                    parent=parent,
                    raw_row_sha256=V1.canonical_sha256(raw),
                    event=event,
                    normalized_payload_canonical_json=(
                        V1._canonical_bytes(event.payload).decode("utf-8")
                    ),
                )
            )

    build_by_event_id = {
        id(item.event): item for item in built_events
    }
    envelopes = V1.group_atomic_envelopes(
        item.event for item in built_events
    )
    collected: dict[
        tuple[str, str],
        list[OrderedNormalizedEventV31],
    ] = {
        key: [] for key in parent_by_key
    }
    for envelope in envelopes:
        for event in envelope.events:
            built = build_by_event_id[id(event)]
            parent = built.parent
            key = (parent.source_date_utc, parent.table)
            collected[key].append(
                OrderedNormalizedEventV31(
                    parent_event_index=len(collected[key]),
                    source_date_utc=parent.source_date_utc,
                    table=parent.table,
                    parent_logical_source_key=(
                        parent.logical_source_key
                    ),
                    parent_version_id=parent.version_id,
                    parent_sha256=parent.sha256,
                    parent_size=parent.size,
                    content_root=rebound.content_root,
                    market_ticker=event.market_ticker,
                    recv_mono_ns=event.recv_mono_ns,
                    recv_wall_ns=event.recv_wall_ns,
                    kind=event.kind,
                    stable_source_id=event.stable_source_id,
                    source_ordinal=event.source_ordinal,
                    raw_row_sha256=built.raw_row_sha256,
                    normalized_source_rows_sha256=(
                        event.source_rows_sha256
                    ),
                    normalized_payload_canonical_json=(
                        built.normalized_payload_canonical_json
                    ),
                    atomic_envelope_sha256=(
                        envelope.source_rows_sha256
                    ),
                    atomic_envelope_event_count=len(envelope.events),
                )
            )

    parent_spines: list[ParentOrderedEventSpineV31] = []
    for parent in rebound.parents:
        events = tuple(
            collected[(parent.source_date_utc, parent.table)]
        )
        if not events:
            _fail(
                "PRIVATE_PARENT_EVENT_SPINE_EMPTY",
                parent.logical_source_key,
            )
        spine_sha = V1.canonical_sha256(
            [item.receipt() for item in events]
        )
        parent_spines.append(
            ParentOrderedEventSpineV31(
                source_date_utc=parent.source_date_utc,
                table=parent.table,
                parent_logical_source_key=parent.logical_source_key,
                parent_version_id=parent.version_id,
                parent_sha256=parent.sha256,
                parent_size=parent.size,
                events=events,
                event_count=len(events),
                ordered_event_spine_sha256=spine_sha,
            )
        )
    spines = tuple(parent_spines)
    event_count = sum(item.event_count for item in spines)
    spine_set_sha = V1.canonical_sha256(
        [item.receipt() for item in spines]
    )
    return spines, spine_set_sha, event_count


def _projection_from_private_spines(
    spines: tuple[ParentOrderedEventSpineV31, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "parent": {
                "source_date_utc": spine.source_date_utc,
                "table": spine.table,
                "logical_source_key": spine.parent_logical_source_key,
                "VersionId": spine.parent_version_id,
                "sha256": spine.parent_sha256,
                "size": spine.parent_size,
            },
            "events": [
                item.candidate_projection() for item in spine.events
            ],
        }
        for spine in spines
    )


def _projection_from_v3_result(
    result: V3.AuthoritativeSourceResultV3,
) -> tuple[dict[str, object], ...]:
    parent_order = [
        (
            parent.source_date_utc,
            parent.table,
            parent.logical_source_key,
            parent.version_id,
            parent.bound_sha256,
            parent.bound_size,
        )
        for parent in result.parent_reads
    ]
    events_by_parent: dict[
        tuple[str, str],
        list[dict[str, object]],
    ] = {
        (parent.source_date_utc, parent.table): []
        for parent in result.parent_reads
    }
    for row in result.state_rows:
        for event in row.source_events:
            table = (
                "trades"
                if event.kind == "TRADE"
                else "orderbooks_full"
            )
            key = (row.source_date_utc, table)
            events_by_parent[key].append(
                {
                    "parent_event_index": len(events_by_parent[key]),
                    "source_date_utc": row.source_date_utc,
                    "table": table,
                    "parent_logical_source_key": (
                        event.parent_logical_source_key
                    ),
                    "parent_version_id": event.parent_version_id,
                    "parent_sha256": event.parent_sha256,
                    "parent_size": event.parent_size,
                    "content_root": event.content_root,
                    "market_ticker": row.market_ticker,
                    "recv_mono_ns": row.recv_mono_ns,
                    "recv_wall_ns": row.recv_wall_ns,
                    "kind": event.kind,
                    "stable_source_id": event.stable_source_id,
                    "source_ordinal": event.source_ordinal,
                    "normalized_source_rows_sha256": (
                        event.source_rows_sha256
                    ),
                    "atomic_envelope_sha256": (
                        row.atomic_source_rows_sha256
                    ),
                    "atomic_envelope_event_count": row.event_count,
                    "atomic_no_precedence": (
                        row.atomic_no_precedence
                    ),
                }
            )
    return tuple(
        {
            "parent": {
                "source_date_utc": source_date,
                "table": table,
                "logical_source_key": logical_key,
                "VersionId": version_id,
                "sha256": sha256,
                "size": size,
            },
            "events": events_by_parent[(source_date, table)],
        }
        for (
            source_date,
            table,
            logical_key,
            version_id,
            sha256,
            size,
        ) in parent_order
    )


def _commit_receipt_payload(
    result: CommitVerifiedResultV31,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": COMMIT_RECEIPT_SCHEMA,
        "status": COMMIT_STATUS,
        "authoritative_commit_receipt": True,
        "dry_run": False,
        "sealed_config": result.sealed_config.receipt(),
        "sealed_config_payload_sha256": (
            result.sealed_config_payload_sha256
        ),
        "external_seal_trust_boundary": "UPSTREAM_PROVIDER",
        "durable_eligibility_chain_revalidated_in_v3_1": False,
        "daily_builder_receipt_pin_count": len(
            result.sealed_config.daily_builder_receipt_pins
        ),
        "v4_2_requires_exact_daily_builder_receipt_pins": True,
        "candidate_transformation_root_sha256": (
            result.candidate_transformation_root_sha256
        ),
        "rebuilt_transformation_root_sha256": (
            result.rebuilt_transformation_root_sha256
        ),
        "rebuilt_v3_state_spine_sha256": (
            result.rebuilt_v3_state_spine_sha256
        ),
        "ordered_event_count": result.ordered_event_count,
        "ordered_event_spine_set_sha256": (
            result.ordered_event_spine_set_sha256
        ),
        "candidate_projection_sha256": (
            result.candidate_projection_sha256
        ),
        "private_rebuild_projection_sha256": (
            result.private_rebuild_projection_sha256
        ),
        "candidate_private_projection_equal": True,
        "parent_event_spines": [
            item.receipt() for item in result.parent_event_spines
        ],
        "commit_verified": True,
        "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        "v4_2_contract_coverage_claimed": False,
        "extraction_authorized": False,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "shadow_authorized": False,
        "live_authorized": False,
    }
    payload["payload_sha256"] = V1.canonical_sha256(payload)
    return payload


def commit_verify_source_v31(
    *,
    candidate_result: V3.AuthoritativeSourceResultV3,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
    sealed_config: ExternalSealedCommitConfigV31,
) -> CommitVerifiedResultV31:
    """Rebuild six parents and register one exact commit-verified result."""
    if type(candidate_result) is not V3.AuthoritativeSourceResultV3:
        _fail(
            "TYPED_V3_RESULT_REQUIRED",
            repr(type(candidate_result)),
        )
    _validate_sealed_config(
        sealed_config,
        authority=authority,
        input_manifest=input_manifest,
        content_root=content_root,
    )
    try:
        rebuilt = V3.build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=input_manifest,
            content_root=content_root,
        )
    except V3.Stage2AuthoritativeV3Error as exc:
        _fail(exc.code, exc.detail)

    spines, spine_set_sha, event_count = (
        _private_parent_ordered_event_spines(
            authority=authority,
            input_manifest=input_manifest,
            content_root=content_root,
        )
    )
    private_projection = _projection_from_private_spines(spines)
    rebuilt_projection = _projection_from_v3_result(rebuilt)
    candidate_projection = _projection_from_v3_result(candidate_result)
    private_projection_sha = V1.canonical_sha256(private_projection)
    rebuilt_projection_sha = V1.canonical_sha256(rebuilt_projection)
    candidate_projection_sha = V1.canonical_sha256(
        candidate_projection
    )
    if (
        private_projection != rebuilt_projection
        or private_projection_sha != rebuilt_projection_sha
        or event_count != rebuilt.source_event_count
    ):
        _fail(
            "PRIVATE_REBUILD_EVENT_SPINE_MISMATCH",
            (
                f"private={private_projection_sha} "
                f"rebuilt={rebuilt_projection_sha}"
            ),
        )
    if (
        candidate_result != rebuilt
        or candidate_projection != private_projection
        or candidate_projection_sha != private_projection_sha
    ):
        _fail(
            "CANDIDATE_REBUILD_MISMATCH",
            (
                f"candidate="
                f"{candidate_result.transformation_root_sha256} "
                f"rebuilt={rebuilt.transformation_root_sha256} "
                f"candidate_projection={candidate_projection_sha} "
                f"private_projection={private_projection_sha}"
            ),
        )

    committed = CommitVerifiedResultV31(
        schema=COMMIT_RESULT_SCHEMA,
        status=COMMIT_STATUS,
        sealed_config=sealed_config,
        sealed_config_payload_sha256=(
            sealed_config.config_payload_sha256
        ),
        candidate_transformation_root_sha256=(
            candidate_result.transformation_root_sha256
        ),
        rebuilt_transformation_root_sha256=(
            rebuilt.transformation_root_sha256
        ),
        rebuilt_v3_state_spine_sha256=rebuilt.state_spine_sha256,
        parent_event_spines=spines,
        ordered_event_count=event_count,
        ordered_event_spine_set_sha256=spine_set_sha,
        candidate_projection_sha256=candidate_projection_sha,
        private_rebuild_projection_sha256=private_projection_sha,
        _factory_token=_COMMIT_FACTORY_TOKEN,
    )
    receipt = _commit_receipt_payload(committed)
    receipt_bytes = V1._canonical_bytes(receipt)
    _COMMIT_REGISTRY[id(committed)] = (committed, receipt_bytes)
    return committed


def build_authoritative_commit_receipt_v31(
    result: CommitVerifiedResultV31,
) -> dict[str, object]:
    """Return only the receipt registered by the direct commit-time rebuild."""
    if type(result) is not CommitVerifiedResultV31:
        _fail(
            "COMMIT_VERIFIED_RESULT_REQUIRED",
            repr(type(result)),
        )
    registered = _COMMIT_REGISTRY.get(id(result))
    if (
        result._factory_token is not _COMMIT_FACTORY_TOKEN
        or registered is None
        or registered[0] is not result
    ):
        _fail(
            "REGISTERED_COMMIT_RESULT_REQUIRED",
            "result was not directly returned by commit verification",
        )
    receipt_bytes = registered[1]
    loaded = V1._load_json_bytes(
        receipt_bytes,
        "v3.1-authoritative-commit-receipt",
    )
    return dict(loaded)


def build_dry_run_receipt_v31(
    candidate_result: V3.AuthoritativeSourceResultV3,
) -> dict[str, object]:
    """Build a visibly non-authoritative diagnostic receipt."""
    if type(candidate_result) is not V3.AuthoritativeSourceResultV3:
        _fail("TYPED_V3_RESULT_REQUIRED", repr(type(candidate_result)))
    payload: dict[str, object] = {
        "schema": DRY_RUN_SCHEMA,
        "status": DRY_RUN_STATUS,
        "authoritative_commit_receipt": False,
        "commit_eligible": False,
        "candidate_transformation_root_sha256": (
            candidate_result.transformation_root_sha256
        ),
        "candidate_state_spine_sha256": (
            candidate_result.state_spine_sha256
        ),
        "external_sealed_config_consumed": False,
        "private_six_parent_commit_rebuild_performed": False,
        "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        "extraction_authorized": False,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "shadow_authorized": False,
        "live_authorized": False,
    }
    payload["payload_sha256"] = V1.canonical_sha256(payload)
    return payload
