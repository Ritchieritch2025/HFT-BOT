"""Fail-closed A01 materialization from source-bound historical records.

This module deliberately stops before fee, latency, order-lifecycle or PnL
arithmetic.  It produces the four evidence collections consumed by the
PnL-spine runner:

* causally normalized A01 decision rows;
* exact public trade records;
* full-depth L2 snapshots selected by an explicit exit authority; and
* source-final settlement records.

The existing C1 campaign/fill/markout tables are not accepted as substitutes.
They contain scenario latency and gross marks, not the direct records required
here.  The extraction specification below identifies the validated Deep03
checkpoint stages and the still-missing sources without guessing values.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import ceil
import re
from typing import Any, Iterable, Mapping, Sequence

from .contracts import PRICE_SCALE_E4, canonical_sha256
from .experiments import (
    DecisionStatus,
    FrozenExperimentAdapters,
    NormalizedStateRow,
    RuntimeBindings,
)


SCHEMA_VERSION = "pnl-spine-a01-materialization-v1"
TRAIN_ARTIFACT_SCHEMA = "pnl-spine-a01-train-gates-v1"
EXTRACTION_SPEC_SCHEMA = "pnl-spine-a01-extraction-spec-v1"
SOURCE_COVERAGE_SCHEMA = "pnl-spine-a01-source-coverage-v1"

TRAIN_DATES = ("2026-07-12", "2026-07-15")
VALIDATION_DATES = ("2026-07-17",)
ALLOWED_STAGES = frozenset({"TRAIN", "VALIDATION"})
ALLOWED_CLASSIFICATIONS = frozenset(
    {"SNAPSHOT_APPLIED", "DELTA_APPLIED"}
)
REQUIRED_SOURCE_COLLECTIONS = (
    "book_records",
    "exit_requests",
    "metadata_records",
    "opportunity_records",
    "public_trade_records",
    "selection_records",
    "settlement_records",
)
ALLOWED_SETTLEMENT_STATUSES = frozenset(
    {
        "FINALIZED",
        "PROVISIONAL",
        "POSTPONED",
        "VOID",
        "CANCELED",
        "RETIRED",
        "UNKNOWN",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 60 * NS_PER_MINUTE
# recv_wall_ns and recv_mono_ns are sampled as one local receive event.  A
# larger cumulative offset drift cannot safely earn five-second dwell or
# 120-second warm-up, even if each individual step looks plausible.
MAX_RECEIVE_CLOCK_OFFSET_DRIFT_NS = NS_PER_MS


class A01MaterializerError(ValueError):
    """The materializer contract itself is malformed."""


@dataclass(frozen=True)
class A01Config:
    freeze_sha256: str
    book_ttl_ns: int
    minimum_raw_spread_ticks: int
    spread_dwell_ns: int
    warmup_ns: int
    cost_buffer_ticks: int
    quantity_e4: int
    quote_aggressiveness: str

    @classmethod
    def from_freeze(
        cls, freeze_document: Mapping[str, Any]
    ) -> "A01Config":
        # Reuse the pinned adapter validation so a locally edited freeze cannot
        # silently redefine the materializer.
        FrozenExperimentAdapters(freeze_document)
        revisions = freeze_document.get("revisions")
        if not isinstance(revisions, list):
            raise A01MaterializerError("freeze revisions are missing")
        matches = [
            row
            for row in revisions
            if isinstance(row, Mapping)
            and isinstance(row.get("source_card"), Mapping)
            and row["source_card"].get("experiment_id")
            == "A01-SPREAD-CAPTURE"
        ]
        if len(matches) != 1:
            raise A01MaterializerError(
                "freeze must contain one A01 revision"
            )
        parameters = matches[0].get("parameter_freeze")
        if not isinstance(parameters, Mapping):
            raise A01MaterializerError(
                "A01 parameter_freeze is missing"
            )

        def frozen(name: str) -> Any:
            row = parameters.get(name)
            if not isinstance(row, Mapping) or "frozen" not in row:
                raise A01MaterializerError(
                    f"A01 frozen parameter is missing: {name}"
                )
            return row["frozen"]

        quantity = frozen("quantity_contracts_per_active_side")
        if type(quantity) is not int or quantity != 1:
            raise A01MaterializerError(
                "A01 quantity must remain one contract per side"
            )
        freeze_sha = freeze_document.get("freeze_sha256")
        if not isinstance(freeze_sha, str):
            raise A01MaterializerError("freeze_sha256 is missing")
        return cls(
            freeze_sha256=freeze_sha,
            book_ttl_ns=int(frozen("book_ttl_ms")) * NS_PER_MS,
            minimum_raw_spread_ticks=int(
                frozen("minimum_raw_spread_ticks")
            ),
            spread_dwell_ns=int(frozen("spread_dwell_ms"))
            * NS_PER_MS,
            warmup_ns=int(frozen("warmup_ms")) * NS_PER_MS,
            cost_buffer_ticks=int(frozen("cost_buffer_ticks")),
            quantity_e4=quantity * 10_000,
            quote_aggressiveness=str(
                frozen("max_quote_aggressiveness")
            ),
        )


@dataclass(frozen=True)
class A01GateThreshold:
    stratum_key: str
    sample_count: int
    activity_p75: int
    toxicity_p90_e6: int
    adverse_width_p90_e4: int


@dataclass(frozen=True)
class A01TrainingArtifact:
    schema_version: str
    freeze_sha256: str
    train_dates_utc: tuple[str, ...]
    quantile_rule: str
    source_manifest_sha256: str
    thresholds: tuple[A01GateThreshold, ...]

    def __post_init__(self) -> None:
        if self.schema_version != TRAIN_ARTIFACT_SCHEMA:
            raise A01MaterializerError(
                "unknown A01 training artifact schema"
            )
        if self.train_dates_utc != TRAIN_DATES:
            raise A01MaterializerError(
                "A01 training artifact must use 07-12 and 07-15 only"
            )
        _require_sha("freeze_sha256", self.freeze_sha256)
        _require_sha(
            "source_manifest_sha256", self.source_manifest_sha256
        )
        if self.quantile_rule != "NEAREST_RANK_CEIL_V1":
            raise A01MaterializerError(
                "unknown A01 training quantile rule"
            )
        keys = [row.stratum_key for row in self.thresholds]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise A01MaterializerError(
                "A01 threshold strata must be unique and sorted"
            )
        for row in self.thresholds:
            _plain_int(
                "threshold.sample_count",
                row.sample_count,
                minimum=1,
            )
            _plain_int(
                "threshold.activity_p75",
                row.activity_p75,
                minimum=0,
            )
            _plain_int(
                "threshold.toxicity_p90_e6",
                row.toxicity_p90_e6,
                minimum=0,
            )
            _plain_int(
                "threshold.adverse_width_p90_e4",
                row.adverse_width_p90_e4,
                minimum=0,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "freeze_sha256": self.freeze_sha256,
            "train_dates_utc": list(self.train_dates_utc),
            "quantile_rule": self.quantile_rule,
            "source_manifest_sha256": self.source_manifest_sha256,
            "thresholds": [asdict(row) for row in self.thresholds],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class A01CollectionCoverage:
    collection: str
    manifest_sha256: str
    records_sha256: str
    record_count: int

    def __post_init__(self) -> None:
        if self.collection not in REQUIRED_SOURCE_COLLECTIONS:
            raise A01MaterializerError(
                f"unknown A01 source collection: {self.collection}"
            )
        _require_sha("manifest_sha256", self.manifest_sha256)
        _require_sha("records_sha256", self.records_sha256)
        _plain_int("record_count", self.record_count, minimum=0)


@dataclass(frozen=True)
class A01SourceCoverage:
    schema_version: str
    stage: str
    cohort_dates_utc: tuple[str, ...]
    opportunity_policy_sha256: str
    opportunity_schedule_sha256: str
    fee_facts_sha256: str
    measured_latency_receipt_sha256: str
    selection_policy_sha256: str
    root_map_sha256: str
    scheduled_start_source_sha256: str
    risk_policy_sha256: str
    terminal_contract_sha256: str
    strict_fill_evidence_sha256: str
    place_latency_ns: int
    cancel_latency_ns: int
    ioc_exit_latency_ns: int
    collections: tuple[A01CollectionCoverage, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_COVERAGE_SCHEMA:
            raise A01MaterializerError(
                "unknown A01 source coverage schema"
            )
        if self.stage not in ALLOWED_STAGES:
            raise A01MaterializerError(
                "A01 source coverage stage is invalid"
            )
        expected_dates = (
            TRAIN_DATES if self.stage == "TRAIN" else VALIDATION_DATES
        )
        if self.cohort_dates_utc != expected_dates:
            raise A01MaterializerError(
                "A01 source coverage has the wrong cohort"
            )
        _require_sha(
            "opportunity_policy_sha256",
            self.opportunity_policy_sha256,
        )
        _require_sha(
            "opportunity_schedule_sha256",
            self.opportunity_schedule_sha256,
        )
        _require_sha("fee_facts_sha256", self.fee_facts_sha256)
        _require_sha(
            "measured_latency_receipt_sha256",
            self.measured_latency_receipt_sha256,
        )
        _require_sha(
            "selection_policy_sha256",
            self.selection_policy_sha256,
        )
        for name in (
            "root_map_sha256",
            "scheduled_start_source_sha256",
            "risk_policy_sha256",
            "terminal_contract_sha256",
            "strict_fill_evidence_sha256",
        ):
            _require_sha(name, getattr(self, name))
        for name in (
            "place_latency_ns",
            "cancel_latency_ns",
            "ioc_exit_latency_ns",
        ):
            _plain_int(name, getattr(self, name), minimum=0)
        names = tuple(row.collection for row in self.collections)
        if names != REQUIRED_SOURCE_COLLECTIONS:
            raise A01MaterializerError(
                "A01 source coverage collections must be complete and sorted"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "cohort_dates_utc": list(self.cohort_dates_utc),
            "opportunity_policy_sha256": (
                self.opportunity_policy_sha256
            ),
            "opportunity_schedule_sha256": (
                self.opportunity_schedule_sha256
            ),
            "fee_facts_sha256": self.fee_facts_sha256,
            "measured_latency_receipt_sha256": (
                self.measured_latency_receipt_sha256
            ),
            "selection_policy_sha256": self.selection_policy_sha256,
            "root_map_sha256": self.root_map_sha256,
            "scheduled_start_source_sha256": (
                self.scheduled_start_source_sha256
            ),
            "risk_policy_sha256": self.risk_policy_sha256,
            "terminal_contract_sha256": (
                self.terminal_contract_sha256
            ),
            "strict_fill_evidence_sha256": (
                self.strict_fill_evidence_sha256
            ),
            "place_latency_ns": self.place_latency_ns,
            "cancel_latency_ns": self.cancel_latency_ns,
            "ioc_exit_latency_ns": self.ioc_exit_latency_ns,
            "collections": [asdict(row) for row in self.collections],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class A01TrainFitResult:
    artifact: A01TrainingArtifact | None
    blockers: tuple[dict[str, str], ...]

    @property
    def ready(self) -> bool:
        return self.artifact is not None and not self.blockers


@dataclass(frozen=True)
class A01MaterializationResult:
    stage: str
    cohort_dates_utc: tuple[str, ...]
    training_artifact_sha256: str | None
    source_coverage_sha256: str | None
    fee_facts_sha256: str | None
    measured_latency_receipt_sha256: str | None
    selection_policy_sha256: str | None
    opportunity_policy_sha256: str | None
    runtime_bindings_sha256: str | None
    rows: tuple[dict[str, Any], ...]
    public_trades: tuple[dict[str, Any], ...]
    exit_snapshots: tuple[dict[str, Any], ...]
    closures: tuple[dict[str, Any], ...]
    settlements: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, str], ...]
    extraction_spec: Mapping[str, Any]

    @property
    def ready(self) -> bool:
        return bool(self.rows) and not self.blockers

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "stage": self.stage,
            "cohort_dates_utc": list(self.cohort_dates_utc),
            "training_artifact_sha256": (
                self.training_artifact_sha256
            ),
            "source_coverage_sha256": self.source_coverage_sha256,
            "fee_facts_sha256": self.fee_facts_sha256,
            "measured_latency_receipt_sha256": (
                self.measured_latency_receipt_sha256
            ),
            "selection_policy_sha256": self.selection_policy_sha256,
            "opportunity_policy_sha256": (
                self.opportunity_policy_sha256
            ),
            "runtime_bindings_sha256": self.runtime_bindings_sha256,
            "rows": list(self.rows),
            "public_trades": list(self.public_trades),
            "exit_snapshots": list(self.exit_snapshots),
            "closures": list(self.closures),
            "settlements": list(self.settlements),
            "blockers": list(self.blockers),
            "extraction_spec": dict(self.extraction_spec),
            "ready": self.ready,
        }
        payload["payload_sha256"] = canonical_sha256(payload)
        return payload


def _require_sha(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise A01MaterializerError(
            f"{name} must be a lowercase SHA-256"
        )
    return value


def _plain_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise A01MaterializerError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise A01MaterializerError(
            f"{name} must be >= {minimum}"
        )
    if maximum is not None and value > maximum:
        raise A01MaterializerError(
            f"{name} must be <= {maximum}"
        )
    return value


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise A01MaterializerError(f"{name} must be nonempty text")
    return value


def _utc_date_from_ns(value: int) -> str:
    """Return the UTC calendar date without float rounding at midnight."""

    try:
        return datetime.fromtimestamp(
            value // NS_PER_SECOND,
            tz=timezone.utc,
        ).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise A01MaterializerError(
            "nanosecond timestamp is outside the UTC calendar range"
        ) from exc


def _utc_date_from_us(value: int) -> str:
    """Return the UTC calendar date without float rounding at midnight."""

    try:
        return datetime.fromtimestamp(
            value // 1_000_000,
            tz=timezone.utc,
        ).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise A01MaterializerError(
            "microsecond timestamp is outside the UTC calendar range"
        ) from exc


def _eligible_market(metadata: Mapping[str, Any]) -> bool:
    return (
        metadata.get("standard_binary_clob") is True
        and metadata.get("mve") is False
        and metadata.get("lifecycle_open") is True
        and metadata.get("paused") is False
    )


def _blocker(
    code: str,
    detail: str,
    *,
    record_id: str | None = None,
) -> dict[str, str]:
    result = {"code": code, "detail": detail}
    if record_id is not None:
        result["record_id"] = record_id
    return result


def _sorted_blockers(
    blockers: Iterable[Mapping[str, str]],
) -> tuple[dict[str, str], ...]:
    unique = {
        (
            str(row.get("code") or ""),
            str(row.get("detail") or ""),
            str(row.get("record_id") or ""),
        )
        for row in blockers
    }
    return tuple(
        {
            **{"code": code, "detail": detail},
            **({"record_id": record_id} if record_id else {}),
        }
        for code, detail, record_id in sorted(unique)
    )


def _nearest_rank(values: Sequence[int], numerator: int) -> int:
    if not values:
        raise A01MaterializerError(
            "cannot calculate a quantile without samples"
        )
    ordered = sorted(values)
    rank = ceil(numerator * len(ordered) / 100)
    return ordered[max(1, rank) - 1]


def a01_opportunity_schedule_sha256(
    opportunity_records: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the complete externally generated opportunity decision schedule."""

    opportunities: set[tuple[str, str, int]] = set()
    for index, raw in enumerate(opportunity_records):
        try:
            date = _text("date", raw.get("date"))
            root = _text("root_event_id", raw.get("root_event_id"))
            decision = _plain_int(
                "decision_ts_ns",
                raw.get("decision_ts_ns"),
                minimum=0,
            )
        except A01MaterializerError as exc:
            raise A01MaterializerError(
                f"opportunity[{index}] cannot enter schedule: {exc}"
            ) from exc
        opportunities.add((date, root, decision))
    return canonical_sha256(
        [
            {
                "date": date,
                "root_event_id": root,
                "decision_ts_ns": decision,
            }
            for date, root, decision in sorted(opportunities)
        ]
    )


def _phase(scheduled_start_ns: int, decision_ns: int) -> str:
    tts = scheduled_start_ns - decision_ns
    if tts < 0:
        return "POST_START"
    if tts < 15 * NS_PER_MINUTE:
        return "PREMATCH_0_TO_15M"
    if tts <= 6 * NS_PER_HOUR:
        return "PREMATCH_15M_TO_6H"
    if tts <= 24 * NS_PER_HOUR:
        return "PREMATCH_6H_TO_24H"
    return "PREMATCH_GT_24H"


def _stratum(
    *,
    sport: str,
    phase: str,
    liquidity_stratum: str,
) -> str:
    return "|".join((sport, phase, liquidity_stratum))


def _metadata_index(
    metadata_records: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    list[dict[str, str]],
]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    blockers: list[dict[str, str]] = []
    required = {
        "date",
        "market_ticker",
        "root_event_id",
        "sport",
        "tick_size_e4",
        "scheduled_start_ts_ns",
        "scheduled_start_asof_ns",
        "identity_asof_ns",
        "tick_size_asof_ns",
        "lifecycle_asof_ns",
        "market_structure_asof_ns",
        "lifecycle_open",
        "paused",
        "standard_binary_clob",
        "mve",
        "source_sha256",
    }
    for index, raw in enumerate(metadata_records):
        record_id = f"metadata[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_METADATA_FIELD_MISSING",
                    ",".join(missing),
                    record_id=record_id,
                )
            )
            continue
        try:
            date = _text("date", raw["date"])
            market = _text("market_ticker", raw["market_ticker"])
            root = _text("root_event_id", raw["root_event_id"])
            sport = _text("sport", raw["sport"])
            tick = _plain_int(
                "tick_size_e4",
                raw["tick_size_e4"],
                minimum=1,
                maximum=PRICE_SCALE_E4,
            )
            start = _plain_int(
                "scheduled_start_ts_ns",
                raw["scheduled_start_ts_ns"],
                minimum=0,
            )
            start_asof = _plain_int(
                "scheduled_start_asof_ns",
                raw["scheduled_start_asof_ns"],
                minimum=0,
            )
            identity_asof = _plain_int(
                "identity_asof_ns",
                raw["identity_asof_ns"],
                minimum=0,
            )
            tick_asof = _plain_int(
                "tick_size_asof_ns",
                raw["tick_size_asof_ns"],
                minimum=0,
            )
            lifecycle_asof = _plain_int(
                "lifecycle_asof_ns",
                raw["lifecycle_asof_ns"],
                minimum=0,
            )
            structure_asof = _plain_int(
                "market_structure_asof_ns",
                raw["market_structure_asof_ns"],
                minimum=0,
            )
            _require_sha("source_sha256", raw["source_sha256"])
            for name in (
                "lifecycle_open",
                "paused",
                "standard_binary_clob",
                "mve",
            ):
                if type(raw[name]) is not bool:
                    raise A01MaterializerError(
                        f"{name} must be bool"
                    )
            if PRICE_SCALE_E4 % tick:
                raise A01MaterializerError(
                    "tick_size_e4 must divide 10000"
                )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"metadata date escapes cohort: {date}"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_METADATA_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        key = (date, market)
        normalized = dict(raw)
        normalized.update(
            {
                "date": date,
                "market_ticker": market,
                "root_event_id": root,
                "sport": sport,
                "tick_size_e4": tick,
                "scheduled_start_ts_ns": start,
                "scheduled_start_asof_ns": start_asof,
                "identity_asof_ns": identity_asof,
                "tick_size_asof_ns": tick_asof,
                "lifecycle_asof_ns": lifecycle_asof,
                "market_structure_asof_ns": structure_asof,
            }
        )
        if key in result and result[key] != normalized:
            blockers.append(
                _blocker(
                    "BLOCK_A01_METADATA_AUTHORITY_CONFLICT",
                    f"{date}/{market}",
                    record_id=record_id,
                )
            )
            continue
        result[key] = normalized
    return result, blockers


def _parse_levels(
    name: str,
    value: object,
    *,
    descending: bool,
) -> tuple[dict[str, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise A01MaterializerError(f"{name} must be a level array")
    levels: list[tuple[int, int]] = []
    seen: set[int] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise A01MaterializerError(
                f"{name}[{index}] must be an object"
            )
        price = _plain_int(
            f"{name}[{index}].yes_price_e4",
            raw.get("yes_price_e4"),
            minimum=1,
            maximum=PRICE_SCALE_E4 - 1,
        )
        quantity = _plain_int(
            f"{name}[{index}].quantity_e4",
            raw.get("quantity_e4"),
            minimum=1,
        )
        if price in seen:
            raise A01MaterializerError(
                f"{name} contains duplicate price {price}"
            )
        seen.add(price)
        levels.append((price, quantity))
    levels.sort(key=lambda row: row[0], reverse=descending)
    return tuple(
        {"yes_price_e4": price, "quantity_e4": quantity}
        for price, quantity in levels
    )


def _prepare_books(
    book_records: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
    metadata: Mapping[tuple[str, str], Mapping[str, Any]],
    config: A01Config,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    required = {
        "record_id",
        "date",
        "market_ticker",
        "receive_timestamp_ns",
        "receive_timestamp_us",
        "receive_monotonic_ns",
        "stream_epoch",
        "ws_sid",
        "ws_seq",
        "sid_gap_generation",
        "market_revalidated_gap_generation",
        "sid_sequence_valid",
        "classification",
        "snapshot_epoch",
        "book_valid",
        "topology",
        "best_yes_bid_e4",
        "best_yes_ask_e4",
        "yes_bids",
        "yes_asks",
        "gap",
        "reset",
        "toxicity_score_e6",
        "toxicity_asof_ns",
        "adverse_width_e4",
        "adverse_width_asof_ns",
        "liquidity_stratum",
        "liquidity_stratum_asof_ns",
        "depth_complete",
        "replay_receipt_sha256",
        "source_sha256",
    }
    parsed: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(book_records):
        fallback_id = f"book[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_L2_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback_id,
                )
            )
            continue
        record_id = str(raw.get("record_id") or fallback_id)
        try:
            record_id = _text("record_id", raw["record_id"])
            date = _text("date", raw["date"])
            market = _text("market_ticker", raw["market_ticker"])
            wall_ns = _plain_int(
                "receive_timestamp_ns",
                raw["receive_timestamp_ns"],
                minimum=0,
            )
            receive_us = _plain_int(
                "receive_timestamp_us",
                raw["receive_timestamp_us"],
                minimum=0,
            )
            mono_ns = _plain_int(
                "receive_monotonic_ns",
                raw["receive_monotonic_ns"],
                minimum=0,
            )
            stream_epoch = _plain_int(
                "stream_epoch", raw["stream_epoch"], minimum=1
            )
            sid = _plain_int("ws_sid", raw["ws_sid"], minimum=0)
            seq = _plain_int("ws_seq", raw["ws_seq"], minimum=0)
            sid_gap_generation = _plain_int(
                "sid_gap_generation",
                raw["sid_gap_generation"],
                minimum=0,
            )
            market_revalidated_generation = _plain_int(
                "market_revalidated_gap_generation",
                raw["market_revalidated_gap_generation"],
                minimum=0,
            )
            classification = _text(
                "classification", raw["classification"]
            )
            epoch = _plain_int(
                "snapshot_epoch", raw["snapshot_epoch"], minimum=1
            )
            bid = _plain_int(
                "best_yes_bid_e4",
                raw["best_yes_bid_e4"],
                minimum=1,
                maximum=PRICE_SCALE_E4 - 1,
            )
            ask = _plain_int(
                "best_yes_ask_e4",
                raw["best_yes_ask_e4"],
                minimum=1,
                maximum=PRICE_SCALE_E4 - 1,
            )
            toxicity = _plain_int(
                "toxicity_score_e6",
                raw["toxicity_score_e6"],
                minimum=0,
            )
            toxicity_asof = _plain_int(
                "toxicity_asof_ns",
                raw["toxicity_asof_ns"],
                minimum=0,
            )
            adverse = _plain_int(
                "adverse_width_e4",
                raw["adverse_width_e4"],
                minimum=0,
            )
            adverse_asof = _plain_int(
                "adverse_width_asof_ns",
                raw["adverse_width_asof_ns"],
                minimum=0,
            )
            liquidity = _text(
                "liquidity_stratum", raw["liquidity_stratum"]
            )
            liquidity_asof = _plain_int(
                "liquidity_stratum_asof_ns",
                raw["liquidity_stratum_asof_ns"],
                minimum=0,
            )
            source_sha = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            replay_receipt_sha = _require_sha(
                "replay_receipt_sha256",
                raw["replay_receipt_sha256"],
            )
            for name in (
                "book_valid",
                "gap",
                "reset",
                "sid_sequence_valid",
            ):
                if type(raw[name]) is not bool:
                    raise A01MaterializerError(
                        f"{name} must be bool"
                    )
            if raw["depth_complete"] is not True:
                raise A01MaterializerError(
                    "full-depth L2 replay is not attested complete"
                )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"L2 date escapes cohort: {date}"
                )
            if _utc_date_from_ns(wall_ns) != date:
                raise A01MaterializerError(
                    "L2 date contradicts receive_timestamp_ns UTC date"
                )
            if (date, market) not in metadata:
                raise A01MaterializerError(
                    f"no direct market metadata for {date}/{market}"
                )
            if (
                toxicity_asof > wall_ns
                or adverse_asof > wall_ns
                or liquidity_asof > wall_ns
            ):
                raise A01MaterializerError(
                    "TRAIN feature clock is future relative to receive time"
                )
            if wall_ns // 1_000 != receive_us:
                raise A01MaterializerError(
                    "receive ns/us clocks do not describe one instant"
                )
            if bid >= ask:
                raise A01MaterializerError(
                    "best bid/ask must be positive and uncrossed"
                )
            tick = int(metadata[(date, market)]["tick_size_e4"])
            if bid % tick or ask % tick:
                raise A01MaterializerError(
                    "best bid/ask are not tick aligned"
                )
            bids = _parse_levels(
                "yes_bids", raw["yes_bids"], descending=True
            )
            asks = _parse_levels(
                "yes_asks", raw["yes_asks"], descending=False
            )
            if any(
                level["yes_price_e4"] % tick
                for level in (*bids, *asks)
            ):
                raise A01MaterializerError(
                    "full L2 levels are not tick aligned"
                )
            if not bids or bids[0]["yes_price_e4"] != bid:
                raise A01MaterializerError(
                    "full bid levels contradict best_yes_bid_e4"
                )
            if not asks or asks[0]["yes_price_e4"] != ask:
                raise A01MaterializerError(
                    "full ask levels contradict best_yes_ask_e4"
                )
            if record_id in seen_ids:
                raise A01MaterializerError(
                    f"duplicate L2 record_id {record_id}"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_L2_RECORD_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen_ids.add(record_id)
        parsed.append(
            {
                **dict(raw),
                "record_id": record_id,
                "date": date,
                "market_ticker": market,
                "receive_timestamp_ns": wall_ns,
                "receive_timestamp_us": receive_us,
                "receive_monotonic_ns": mono_ns,
                "stream_epoch": stream_epoch,
                "ws_sid": sid,
                "ws_seq": seq,
                "sid_gap_generation": sid_gap_generation,
                "market_revalidated_gap_generation": (
                    market_revalidated_generation
                ),
                "classification": classification,
                "snapshot_epoch": epoch,
                "best_yes_bid_e4": bid,
                "best_yes_ask_e4": ask,
                "yes_bids": bids,
                "yes_asks": asks,
                "toxicity_score_e6": toxicity,
                "toxicity_asof_ns": toxicity_asof,
                "adverse_width_e4": adverse,
                "adverse_width_asof_ns": adverse_asof,
                "liquidity_stratum": liquidity,
                "liquidity_stratum_asof_ns": liquidity_asof,
                "depth_complete": True,
                "replay_receipt_sha256": replay_receipt_sha,
                "source_sha256": source_sha,
            }
        )

    sid_invalid_records: set[str] = set()
    sid_previous: dict[tuple[str, int, int], dict[str, Any]] = {}
    sid_order = sorted(
        parsed,
        key=lambda row: (
            row["date"],
            row["receive_timestamp_ns"],
            row["receive_monotonic_ns"],
            row["stream_epoch"],
            row["ws_sid"],
            row["ws_seq"],
            row["record_id"],
        ),
    )
    for row in sid_order:
        sid_key = (
            row["date"],
            row["stream_epoch"],
            row["ws_sid"],
        )
        prior_sid = sid_previous.get(sid_key)
        invalid_detail: str | None = None
        if row["sid_sequence_valid"] is not True:
            invalid_detail = "adapter marks sid sequence invalid"
        elif prior_sid is not None:
            if row["ws_seq"] <= prior_sid["ws_seq"]:
                invalid_detail = "sid-global ws_seq is not increasing"
            elif (
                row["sid_gap_generation"]
                < prior_sid["sid_gap_generation"]
            ):
                invalid_detail = "sid gap generation regressed"
            elif (
                row["sid_gap_generation"]
                > prior_sid["sid_gap_generation"]
                and row["gap"] is not True
            ):
                invalid_detail = (
                    "sid gap generation advanced without a gap marker"
                )
            elif (
                row["gap"] is True
                and row["sid_gap_generation"]
                <= prior_sid["sid_gap_generation"]
            ):
                invalid_detail = (
                    "sid gap marker did not advance global generation"
                )
        if invalid_detail is not None:
            sid_invalid_records.add(row["record_id"])
            blockers.append(
                _blocker(
                    "BLOCK_A01_SID_SEQUENCE_INVALID",
                    (
                        f"{row['date']}/epoch={row['stream_epoch']}/"
                        f"sid={row['ws_sid']}:{invalid_detail}"
                    ),
                    record_id=row["record_id"],
                )
            )
        sid_previous[sid_key] = row

    parsed.sort(
        key=lambda row: (
            row["date"],
            row["market_ticker"],
            row["receive_timestamp_ns"],
            row["receive_monotonic_ns"],
            row["ws_sid"],
            row["ws_seq"],
            row["record_id"],
        )
    )
    causal_counts: dict[tuple[Any, ...], int] = {}
    for row in parsed:
        causal_key = (
            row["date"],
            row["market_ticker"],
            row["receive_timestamp_ns"],
            row["receive_monotonic_ns"],
        )
        causal_counts[causal_key] = causal_counts.get(causal_key, 0) + 1
    previous: dict[tuple[str, str], dict[str, Any]] = {}
    windows: dict[tuple[str, str], list[int]] = {}
    for row in parsed:
        causal_key = (
            row["date"],
            row["market_ticker"],
            row["receive_timestamp_ns"],
            row["receive_monotonic_ns"],
        )
        receive_order_ambiguous = causal_counts[causal_key] > 1
        if receive_order_ambiguous:
            blockers.append(
                _blocker(
                    "BLOCK_A01_RECEIVE_ORDER_AMBIGUOUS",
                    (
                        f"{row['date']}/{row['market_ticker']}/"
                        f"{causal_key[2:]}"
                    ),
                    record_id=row["record_id"],
                )
            )
        market_key = (row["date"], row["market_ticker"])
        prior = previous.get(market_key)
        is_snapshot = (
            row["classification"] == "SNAPSHOT_APPLIED"
            and row["reset"] is True
        )
        valid = (
            row["classification"] in ALLOWED_CLASSIFICATIONS
            and row["book_valid"] is True
            and row["topology"] == "TWO_SIDED"
            and row["gap"] is False
            and row["sid_sequence_valid"] is True
            and row["record_id"] not in sid_invalid_records
            and row["market_revalidated_gap_generation"]
            == row["sid_gap_generation"]
            and not receive_order_ambiguous
        )
        continuity_break = prior is None
        if prior is not None:
            wall_delta = (
                row["receive_timestamp_ns"]
                - prior["receive_timestamp_ns"]
            )
            if wall_delta < 0:
                valid = False
            if wall_delta > config.book_ttl_ns:
                continuity_break = True
            if (
                row["stream_epoch"] != prior["stream_epoch"]
                or row["ws_sid"] != prior["ws_sid"]
            ):
                continuity_break = True
                if not is_snapshot:
                    valid = False
            elif row["ws_seq"] <= prior["ws_seq"]:
                valid = False
                continuity_break = True
            if (
                row["stream_epoch"] == prior["stream_epoch"]
                and row["ws_sid"] == prior["ws_sid"]
            ):
                monotonic_delta = (
                    row["receive_monotonic_ns"]
                    - prior["receive_monotonic_ns"]
                )
                if monotonic_delta <= 0:
                    valid = False
                    continuity_break = True
                if monotonic_delta > config.book_ttl_ns:
                    continuity_break = True
                if (
                    wall_delta >= 0
                    and monotonic_delta > 0
                    and abs(wall_delta - monotonic_delta)
                    > config.book_ttl_ns
                ):
                    blockers.append(
                        _blocker(
                            "BLOCK_A01_RECEIVE_CLOCK_DIVERGENCE",
                            (
                                f"{row['date']}/{row['market_ticker']}:"
                                f"wall_delta={wall_delta},"
                                f"mono_delta={monotonic_delta}"
                            ),
                            record_id=row["record_id"],
                        )
                    )
                    valid = False
                    continuity_break = True
                if (
                    not is_snapshot
                    and prior.get("_continuity_valid")
                    and wall_delta >= 0
                    and monotonic_delta > 0
                ):
                    anchor_offset = int(
                        prior.get(
                            "_clock_offset_anchor_ns",
                            (
                                prior["receive_timestamp_ns"]
                                - prior["receive_monotonic_ns"]
                            ),
                        )
                    )
                    current_offset = (
                        row["receive_timestamp_ns"]
                        - row["receive_monotonic_ns"]
                    )
                    cumulative_drift = abs(
                        current_offset - anchor_offset
                    )
                    if (
                        cumulative_drift
                        > MAX_RECEIVE_CLOCK_OFFSET_DRIFT_NS
                    ):
                        blockers.append(
                            _blocker(
                                "BLOCK_A01_RECEIVE_CLOCK_DIVERGENCE",
                                (
                                    f"{row['date']}/"
                                    f"{row['market_ticker']}:"
                                    "cumulative_offset_drift_ns="
                                    f"{cumulative_drift}"
                                ),
                                record_id=row["record_id"],
                            )
                        )
                        valid = False
                        continuity_break = True
            if row["snapshot_epoch"] != prior["snapshot_epoch"]:
                continuity_break = True
                if not is_snapshot:
                    valid = False
        if is_snapshot:
            continuity_break = True
            if (
                row["market_revalidated_gap_generation"]
                != row["sid_gap_generation"]
            ):
                valid = False
            if (
                prior is not None
                and row["snapshot_epoch"]
                <= prior.get("_authorized_epoch", 0)
            ):
                valid = False
        elif row["classification"] == "SNAPSHOT_APPLIED":
            valid = False
        elif (
            row["classification"] == "DELTA_APPLIED"
            and (
                prior is None
                or not prior.get("_continuity_valid")
                or row["snapshot_epoch"] != prior["snapshot_epoch"]
                or (
                    row["market_revalidated_gap_generation"]
                    != prior[
                        "market_revalidated_gap_generation"
                    ]
                )
                or row["reset"] is True
            )
        ):
            valid = False
        if not valid:
            continuity_break = True

        tick = int(metadata[market_key]["tick_size_e4"])
        spread = (
            row["best_yes_ask_e4"] - row["best_yes_bid_e4"]
        )
        spread_qualified = (
            valid
            and spread % tick == 0
            and spread // tick >= config.minimum_raw_spread_ticks
        )
        if (
            continuity_break
            or prior is None
            or not prior.get("_continuity_valid")
        ):
            coverage_start = row["receive_timestamp_ns"]
            clock_offset_anchor = (
                row["receive_timestamp_ns"]
                - row["receive_monotonic_ns"]
            )
        else:
            coverage_start = prior["_coverage_start_ns"]
            clock_offset_anchor = prior["_clock_offset_anchor_ns"]
        if (
            continuity_break
            or prior is None
            or not prior.get("_spread_qualified")
            or not spread_qualified
        ):
            spread_start = (
                row["receive_timestamp_ns"]
                if spread_qualified
                else None
            )
        else:
            spread_start = prior["_spread_start_ns"]

        times = windows.setdefault(market_key, [])
        if continuity_break:
            times.clear()
        cutoff = row["receive_timestamp_ns"] - 60 * NS_PER_SECOND
        left = bisect_right(times, cutoff)
        if left:
            del times[:left]
        if valid:
            times.append(row["receive_timestamp_ns"])
        row["_update_count_60s"] = len(times)
        row["_continuity_valid"] = valid
        row["_authorized_epoch"] = (
            row["snapshot_epoch"]
            if is_snapshot and valid
            else (
                prior.get("_authorized_epoch", 0)
                if prior is not None
                else 0
            )
        )
        row["_coverage_start_ns"] = coverage_start
        row["_clock_offset_anchor_ns"] = clock_offset_anchor
        row["_spread_qualified"] = spread_qualified
        row["_spread_start_ns"] = spread_start
        previous[market_key] = row
    return parsed, blockers


def fit_a01_training_artifact(
    *,
    freeze_document: Mapping[str, Any],
    book_records: Sequence[Mapping[str, Any]],
    metadata_records: Sequence[Mapping[str, Any]],
    source_manifest_sha256: str,
) -> A01TrainFitResult:
    """Fit TRAIN-only p75/p90 gates without opening validation records."""

    config = A01Config.from_freeze(freeze_document)
    _require_sha("source_manifest_sha256", source_manifest_sha256)
    raw_dates = {
        str(row.get("date"))
        for row in book_records
        if row.get("date") is not None
    }
    blockers: list[dict[str, str]] = []
    if "2026-07-17" in raw_dates:
        blockers.append(
            _blocker(
                "BLOCK_A01_VALIDATION_EXPOSED_DURING_TRAIN",
                "07-17 records may not enter TRAIN threshold fitting",
            )
        )
    unexpected = sorted(raw_dates - set(TRAIN_DATES))
    if unexpected:
        blockers.append(
            _blocker(
                "BLOCK_A01_TRAIN_DATE_ESCAPE",
                ",".join(unexpected),
            )
        )
    missing_dates = sorted(set(TRAIN_DATES) - raw_dates)
    if missing_dates:
        blockers.append(
            _blocker(
                "BLOCK_A01_TRAIN_DATE_MISSING",
                ",".join(missing_dates),
            )
        )
    metadata, metadata_blocks = _metadata_index(
        metadata_records,
        allowed_dates=frozenset(TRAIN_DATES),
    )
    blockers.extend(metadata_blocks)
    metadata_dates = {
        str(row.get("date"))
        for row in metadata_records
        if row.get("date") is not None
    }
    missing_metadata_dates = sorted(
        set(TRAIN_DATES) - metadata_dates
    )
    if missing_metadata_dates:
        blockers.append(
            _blocker(
                "BLOCK_A01_TRAIN_METADATA_DATE_MISSING",
                ",".join(missing_metadata_dates),
            )
        )
    books, book_blocks = _prepare_books(
        book_records,
        allowed_dates=frozenset(TRAIN_DATES),
        metadata=metadata,
        config=config,
    )
    blockers.extend(book_blocks)

    values: dict[str, dict[str, list[int]]] = {}
    for row in books:
        if not row.get("_continuity_valid"):
            continue
        meta = metadata[(row["date"], row["market_ticker"])]
        start = int(meta["scheduled_start_ts_ns"])
        asof = int(meta["scheduled_start_asof_ns"])
        metadata_asof = max(
            asof,
            int(meta["identity_asof_ns"]),
            int(meta["tick_size_asof_ns"]),
            int(meta["lifecycle_asof_ns"]),
            int(meta["market_structure_asof_ns"]),
        )
        if metadata_asof > row["receive_timestamp_ns"]:
            blockers.append(
                _blocker(
                    "BLOCK_A01_METADATA_LOOKAHEAD",
                    f"{row['date']}/{row['market_ticker']}",
                    record_id=row["record_id"],
                )
            )
            continue
        if not _eligible_market(meta):
            continue
        stratum = _stratum(
            sport=str(meta["sport"]),
            phase=_phase(start, row["receive_timestamp_ns"]),
            liquidity_stratum=row["liquidity_stratum"],
        )
        bucket = values.setdefault(
            stratum,
            {"activity": [], "toxicity": [], "adverse": []},
        )
        bucket["activity"].append(row["_update_count_60s"])
        bucket["toxicity"].append(row["toxicity_score_e6"])
        bucket["adverse"].append(row["adverse_width_e4"])

    thresholds = tuple(
        A01GateThreshold(
            stratum_key=key,
            sample_count=len(bucket["activity"]),
            activity_p75=_nearest_rank(bucket["activity"], 75),
            toxicity_p90_e6=_nearest_rank(bucket["toxicity"], 90),
            adverse_width_p90_e4=_nearest_rank(
                bucket["adverse"], 90
            ),
        )
        for key, bucket in sorted(values.items())
        if bucket["activity"]
    )
    if not thresholds:
        blockers.append(
            _blocker(
                "BLOCK_A01_TRAIN_GATE_SUPPORT_EMPTY",
                "no causal valid TRAIN strata were materialized",
            )
        )
    final_blocks = _sorted_blockers(blockers)
    if final_blocks:
        return A01TrainFitResult(
            artifact=None,
            blockers=final_blocks,
        )
    return A01TrainFitResult(
        artifact=A01TrainingArtifact(
            schema_version=TRAIN_ARTIFACT_SCHEMA,
            freeze_sha256=config.freeze_sha256,
            train_dates_utc=TRAIN_DATES,
            quantile_rule="NEAREST_RANK_CEIL_V1",
            source_manifest_sha256=source_manifest_sha256,
            thresholds=thresholds,
        ),
        blockers=(),
    )


def _opportunity_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
    metadata: Mapping[tuple[str, str], Mapping[str, Any]],
    opportunity_policy_sha256: str | None,
) -> tuple[
    dict[tuple[str, str, int], dict[str, Any]],
    list[dict[str, str]],
]:
    required = {
        "opportunity_id",
        "date",
        "root_event_id",
        "decision_ts_ns",
        "asof_ns",
        "opportunity_policy_sha256",
        "source_sha256",
    }
    parsed: dict[tuple[str, str, int], dict[str, Any]] = {}
    blockers: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(records):
        fallback = f"opportunity[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_OPPORTUNITY_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback,
                )
            )
            continue
        record_id = str(raw.get("opportunity_id") or fallback)
        try:
            record_id = _text(
                "opportunity_id", raw["opportunity_id"]
            )
            date = _text("date", raw["date"])
            root = _text("root_event_id", raw["root_event_id"])
            decision = _plain_int(
                "decision_ts_ns",
                raw["decision_ts_ns"],
                minimum=0,
            )
            asof = _plain_int(
                "asof_ns", raw["asof_ns"], minimum=0
            )
            policy = _require_sha(
                "opportunity_policy_sha256",
                raw["opportunity_policy_sha256"],
            )
            source = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"opportunity date escapes cohort: {date}"
                )
            if _utc_date_from_ns(decision) != date:
                raise A01MaterializerError(
                    "opportunity date contradicts decision UTC date"
                )
            if asof > decision:
                raise A01MaterializerError(
                    "opportunity schedule is future at decision"
                )
            if policy != opportunity_policy_sha256:
                raise A01MaterializerError(
                    "opportunity uses an unpinned enumeration policy"
                )
            root_metadata = [
                row
                for (meta_date, _market), row in metadata.items()
                if meta_date == date and row["root_event_id"] == root
            ]
            if not root_metadata:
                raise A01MaterializerError(
                    f"opportunity root has no direct metadata: {date}/{root}"
                )
            future_members = sorted(
                str(row["market_ticker"])
                for row in root_metadata
                if max(
                    int(row["scheduled_start_asof_ns"]),
                    int(row["identity_asof_ns"]),
                    int(row["tick_size_asof_ns"]),
                    int(row["lifecycle_asof_ns"]),
                    int(row["market_structure_asof_ns"]),
                )
                > decision
            )
            if future_members:
                raise A01MaterializerError(
                    "candidate universe has future metadata:"
                    + ",".join(future_members)
                )
            key = (date, root, decision)
            if record_id in seen_ids:
                raise A01MaterializerError(
                    f"duplicate opportunity_id {record_id}"
                )
            if key in parsed:
                raise A01MaterializerError(
                    "duplicate opportunity root decision"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_OPPORTUNITY_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen_ids.add(record_id)
        parsed[key] = {
            "opportunity_id": record_id,
            "date": date,
            "root_event_id": root,
            "decision_ts_ns": decision,
            "asof_ns": asof,
            "opportunity_policy_sha256": policy,
            "source_sha256": source,
        }
    return parsed, blockers


def _selection_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
    metadata: Mapping[tuple[str, str], Mapping[str, Any]],
    fee_facts_sha256: str | None,
    measured_latency_receipt_sha256: str | None,
    selection_policy_sha256: str | None,
    opportunities: Mapping[
        tuple[str, str, int], Mapping[str, Any]
    ],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    required = {
        "selection_id",
        "date",
        "root_event_id",
        "market_ticker",
        "decision_ts_ns",
        "risk_adjusted_score_e6",
        "worst_state_loss_e6",
        "net_capture_margin_ticks",
        "asof_ns",
        "fee_facts_sha256",
        "measured_latency_receipt_sha256",
        "selection_policy_sha256",
        "source_sha256",
    }
    parsed: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    authority_keys: set[tuple[str, str, int, str]] = set()
    for index, raw in enumerate(records):
        fallback = f"selection[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SELECTION_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback,
                )
            )
            continue
        record_id = str(raw.get("selection_id") or fallback)
        try:
            record_id = _text("selection_id", raw["selection_id"])
            date = _text("date", raw["date"])
            root = _text("root_event_id", raw["root_event_id"])
            market = _text("market_ticker", raw["market_ticker"])
            decision = _plain_int(
                "decision_ts_ns", raw["decision_ts_ns"], minimum=0
            )
            score = _plain_int(
                "risk_adjusted_score_e6",
                raw["risk_adjusted_score_e6"],
            )
            loss = _plain_int(
                "worst_state_loss_e6",
                raw["worst_state_loss_e6"],
                minimum=0,
            )
            margin = _plain_int(
                "net_capture_margin_ticks",
                raw["net_capture_margin_ticks"],
            )
            asof = _plain_int("asof_ns", raw["asof_ns"], minimum=0)
            source = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            fee_facts = _require_sha(
                "fee_facts_sha256", raw["fee_facts_sha256"]
            )
            measured_latency = _require_sha(
                "measured_latency_receipt_sha256",
                raw["measured_latency_receipt_sha256"],
            )
            selection_policy = _require_sha(
                "selection_policy_sha256",
                raw["selection_policy_sha256"],
            )
            if fee_facts != fee_facts_sha256:
                raise A01MaterializerError(
                    "selection margin uses unpinned fee facts"
                )
            if measured_latency != measured_latency_receipt_sha256:
                raise A01MaterializerError(
                    "selection margin uses unpinned measured latency"
                )
            if selection_policy != selection_policy_sha256:
                raise A01MaterializerError(
                    "selection score uses an unpinned policy"
                )
            if asof > decision:
                raise A01MaterializerError(
                    "selection score is future relative to decision"
                )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"selection date escapes cohort: {date}"
                )
            if _utc_date_from_ns(decision) != date:
                raise A01MaterializerError(
                    "selection date contradicts decision_ts_ns UTC date"
                )
            meta = metadata.get((date, market))
            if meta is None:
                raise A01MaterializerError(
                    f"selection has no metadata: {date}/{market}"
                )
            if not _eligible_market(meta):
                raise A01MaterializerError(
                    "selection market is not an open standard binary CLOB"
                )
            if meta["root_event_id"] != root:
                raise A01MaterializerError(
                    "selection root contradicts direct metadata"
                )
            if max(
                int(meta["scheduled_start_asof_ns"]),
                int(meta["identity_asof_ns"]),
                int(meta["tick_size_asof_ns"]),
                int(meta["lifecycle_asof_ns"]),
                int(meta["market_structure_asof_ns"]),
            ) > decision:
                raise A01MaterializerError(
                    "candidate-universe metadata is future at selection"
                )
            if record_id in seen_ids:
                raise A01MaterializerError(
                    f"duplicate selection_id {record_id}"
                )
            authority_key = (date, root, decision, market)
            if authority_key in authority_keys:
                raise A01MaterializerError(
                    "duplicate market score at one root decision"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SELECTION_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen_ids.add(record_id)
        authority_keys.add(authority_key)
        parsed.append(
            {
                **dict(raw),
                "selection_id": record_id,
                "date": date,
                "root_event_id": root,
                "market_ticker": market,
                "decision_ts_ns": decision,
                "risk_adjusted_score_e6": score,
                "worst_state_loss_e6": loss,
                "net_capture_margin_ticks": margin,
                "asof_ns": asof,
                "fee_facts_sha256": fee_facts,
                "measured_latency_receipt_sha256": measured_latency,
                "selection_policy_sha256": selection_policy,
                "source_sha256": source,
            }
        )
    # One market per exact root decision.  Score desc, loss asc, market ID asc.
    winners: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in parsed:
        groups.setdefault(
            (
                row["date"],
                row["root_event_id"],
                row["decision_ts_ns"],
            ),
            [],
        ).append(row)
    for date, root, decision in sorted(set(groups) - set(opportunities)):
        blockers.append(
            _blocker(
                "BLOCK_A01_SELECTION_OUTSIDE_OPPORTUNITY_SCHEDULE",
                f"{date}/{root}/{decision}",
            )
        )
    for key in sorted(opportunities):
        date, root, decision = key
        root_metadata = [
            row
            for (meta_date, _market), row in metadata.items()
            if meta_date == date and row["root_event_id"] == root
        ]
        expected_markets = {
            market
            for (meta_date, market), row in metadata.items()
            if (
                meta_date == date
                and row["root_event_id"] == root
                and _eligible_market(row)
            )
        }
        supplied_rows = groups.get(key, [])
        supplied_markets = {
            row["market_ticker"] for row in supplied_rows
        }
        missing_markets = sorted(expected_markets - supplied_markets)
        if missing_markets:
            blockers.append(
                _blocker(
                    "BLOCK_A01_MARKET_SELECTION_INCOMPLETE",
                    f"{date}/{root}:{','.join(missing_markets)}",
                )
            )
        unexpected_markets = sorted(
            supplied_markets - expected_markets
        )
        if unexpected_markets:
            blockers.append(
                _blocker(
                    "BLOCK_A01_MARKET_SELECTION_UNEXPECTED",
                    f"{date}/{root}:{','.join(unexpected_markets)}",
                )
            )
        if not expected_markets:
            continue
        if not supplied_rows:
            blockers.append(
                _blocker(
                    "BLOCK_A01_ROOT_SELECTION_MISSING",
                    f"{date}/{root}/{decision}",
                )
            )
            continue
        ordered = sorted(
            supplied_rows,
            key=lambda row: (
                -row["risk_adjusted_score_e6"],
                row["worst_state_loss_e6"],
                row["market_ticker"],
                row["selection_id"],
            ),
        )
        winners.append(ordered[0])
    return winners, blockers


def _book_at_or_before(
    books_by_market: Mapping[
        tuple[str, str], tuple[list[int], list[dict[str, Any]]]
    ],
    *,
    date: str,
    market: str,
    at_ns: int,
) -> dict[str, Any] | None:
    row = books_by_market.get((date, market))
    if row is None:
        return None
    times, books = row
    index = bisect_right(times, at_ns) - 1
    if index < 0:
        return None
    return books[index]


def _trade_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
    markets: frozenset[tuple[str, str]],
    metadata: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, str]]]:
    required = {
        "trade_id",
        "date",
        "market_ticker",
        "exchange_timestamp_us",
        "receive_timestamp_us",
        "receive_wall_ns",
        "receive_monotonic_ns",
        "clock_domain",
        "yes_price_e4",
        "quantity_e4",
        "taker_side",
        "source_sha256",
    }
    parsed: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        fallback = f"trade[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRADE_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback,
                )
            )
            continue
        record_id = str(raw.get("trade_id") or fallback)
        try:
            trade_id = _text("trade_id", raw["trade_id"])
            date = _text("date", raw["date"])
            market = _text("market_ticker", raw["market_ticker"])
            exchange_timestamp = _plain_int(
                "exchange_timestamp_us",
                raw["exchange_timestamp_us"],
                minimum=0,
            )
            receive_timestamp = _plain_int(
                "receive_timestamp_us",
                raw["receive_timestamp_us"],
                minimum=0,
            )
            receive_wall_ns = _plain_int(
                "receive_wall_ns",
                raw["receive_wall_ns"],
                minimum=0,
            )
            receive_monotonic_ns = _plain_int(
                "receive_monotonic_ns",
                raw["receive_monotonic_ns"],
                minimum=0,
            )
            clock_domain = _text("clock_domain", raw["clock_domain"])
            price = _plain_int(
                "yes_price_e4",
                raw["yes_price_e4"],
                minimum=1,
                maximum=PRICE_SCALE_E4 - 1,
            )
            quantity = _plain_int(
                "quantity_e4", raw["quantity_e4"], minimum=1
            )
            taker = _text("taker_side", raw["taker_side"]).upper()
            source = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"trade date escapes cohort: {date}"
                )
            if clock_domain != "LOCAL_RECEIVE_WALL":
                raise A01MaterializerError(
                    "trade fill clock must be LOCAL_RECEIVE_WALL"
                )
            if receive_timestamp != receive_wall_ns // 1_000:
                raise A01MaterializerError(
                    "receive_timestamp_us must equal receive_wall_ns//1000"
                )
            if _utc_date_from_us(receive_timestamp) != date:
                raise A01MaterializerError(
                    "trade date contradicts local receive UTC date"
                )
            if taker not in {"YES", "NO"}:
                raise A01MaterializerError(
                    "taker_side must be YES or NO"
                )
            meta = metadata.get((date, market))
            if meta is None:
                raise A01MaterializerError(
                    f"trade has no direct market metadata: {date}/{market}"
                )
            if price % int(meta["tick_size_e4"]):
                raise A01MaterializerError(
                    "public trade price is not tick aligned"
                )
            if trade_id in seen:
                raise A01MaterializerError(
                    f"duplicate trade_id {trade_id}"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRADE_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen.add(trade_id)
        if (date, market) not in markets:
            continue
        parsed.append(
            {
                "trade_id": trade_id,
                "market_ticker": market,
                # The strict fill engine's ``timestamp_us`` is explicitly
                # local receive-wall time.  Exchange time is source evidence,
                # never the causal activation/cancel comparison clock.
                "timestamp_us": receive_timestamp,
                "yes_price_e4": price,
                "quantity_e4": quantity,
                "taker_side": taker,
                "source_sha256": source,
            }
        )
    parsed.sort(
        key=lambda row: (
            row["market_ticker"],
            row["timestamp_us"],
            row["trade_id"],
        )
    )
    return tuple(parsed), blockers


def _exit_rows(
    requests: Sequence[Mapping[str, Any]],
    *,
    allowed_dates: frozenset[str],
    books_by_market: Mapping[
        tuple[str, str], tuple[list[int], list[dict[str, Any]]]
    ],
    intent_context: Mapping[str, Mapping[str, Any]],
    settlement_ids_by_market: Mapping[str, str],
    measured_latency_receipt_sha256: str | None,
    cancel_latency_ns: int | None,
    ioc_exit_latency_ns: int | None,
    config: A01Config,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    list[dict[str, str]],
]:
    required = {
        "exit_id",
        "row_id",
        "intent_id",
        "date",
        "market_ticker",
        "exit_decision_ts_ns",
        "effective_ts_ns",
        "measured_ioc_exit_latency_ns",
        "exit_limit_price_e4",
        "maximum_snapshot_age_us",
        "measured_latency_receipt_sha256",
        "source_sha256",
    }
    snapshots_by_book: dict[str, dict[str, Any]] = {}
    closures: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_intents: set[str] = set()
    micro_to_book: dict[tuple[str, int], str] = {}
    for index, raw in enumerate(requests):
        fallback = f"exit[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_EXIT_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback,
                )
            )
            continue
        record_id = str(raw.get("exit_id") or fallback)
        try:
            exit_id = _text("exit_id", raw["exit_id"])
            row_id = _text("row_id", raw["row_id"])
            intent_id = _text("intent_id", raw["intent_id"])
            date = _text("date", raw["date"])
            market = _text("market_ticker", raw["market_ticker"])
            decision = _plain_int(
                "exit_decision_ts_ns",
                raw["exit_decision_ts_ns"],
                minimum=0,
            )
            effective = _plain_int(
                "effective_ts_ns",
                raw["effective_ts_ns"],
                minimum=decision,
            )
            measured_ioc_latency = _plain_int(
                "measured_ioc_exit_latency_ns",
                raw["measured_ioc_exit_latency_ns"],
                minimum=0,
            )
            exit_limit = _plain_int(
                "exit_limit_price_e4",
                raw["exit_limit_price_e4"],
                minimum=1,
                maximum=PRICE_SCALE_E4 - 1,
            )
            max_age_us = _plain_int(
                "maximum_snapshot_age_us",
                raw["maximum_snapshot_age_us"],
                minimum=0,
            )
            exit_authority_source = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            exit_latency_source = _require_sha(
                "measured_latency_receipt_sha256",
                raw["measured_latency_receipt_sha256"],
            )
            if (
                exit_latency_source
                != measured_latency_receipt_sha256
            ):
                raise A01MaterializerError(
                    "exit effective time uses unpinned measured latency"
                )
            if measured_ioc_latency != ioc_exit_latency_ns:
                raise A01MaterializerError(
                    "exit latency value differs from pinned IOC_EXIT latency"
                )
            if effective != decision + measured_ioc_latency:
                raise A01MaterializerError(
                    "effective exit time is not decision plus IOC_EXIT latency"
                )
            if date not in allowed_dates:
                raise A01MaterializerError(
                    f"exit date escapes cohort: {date}"
                )
            if _utc_date_from_ns(decision) != date:
                raise A01MaterializerError(
                    "exit date contradicts exit_decision_ts_ns UTC date"
                )
            context = intent_context.get(intent_id)
            if context is None:
                raise A01MaterializerError(
                    "exit authority references no triggered A01 intent"
                )
            if context["row_id"] != row_id:
                raise A01MaterializerError(
                    "exit authority row_id contradicts intent"
                )
            if (
                context["date"] != date
                or context["market_ticker"] != market
            ):
                raise A01MaterializerError(
                    "exit authority market/date contradicts intent"
                )
            if decision < int(context["entry_decision_ts_ns"]):
                raise A01MaterializerError(
                    "exit decision precedes its entry decision"
                )
            earliest_exit_decision = (
                int(context["entry_decision_ts_ns"])
                + int(context["expire_after_ms"]) * NS_PER_MS
                + int(cancel_latency_ns or 0)
            )
            if decision < earliest_exit_decision:
                raise A01MaterializerError(
                    "exit decision precedes quote expiry plus CANCEL latency"
                )
            if exit_limit % int(context["tick_size_e4"]):
                raise A01MaterializerError(
                    "exit limit is not aligned to direct tick size"
                )
            if max_age_us * 1_000 > config.book_ttl_ns:
                raise A01MaterializerError(
                    "exit snapshot max age exceeds frozen 250ms TTL"
                )
            if exit_id in seen_ids:
                raise A01MaterializerError(
                    f"duplicate exit_id {exit_id}"
                )
            if intent_id in seen_intents:
                raise A01MaterializerError(
                    "multiple exit authorities for one A01 intent"
                )
            book = _book_at_or_before(
                books_by_market,
                date=date,
                market=market,
                at_ns=effective,
            )
            if book is None:
                raise A01MaterializerError(
                    "no receive-time L2 at or before effective exit"
                )
            if not book.get("_continuity_valid"):
                raise A01MaterializerError(
                    "latest exit L2 is gap/reset/epoch invalid"
                )
            if effective - book["receive_timestamp_ns"] > (
                max_age_us * 1_000
            ):
                raise A01MaterializerError(
                    "latest exit L2 exceeds exact max age"
                )
            micro_key = (market, int(book["receive_timestamp_us"]))
            prior_book_id = micro_to_book.get(micro_key)
            if (
                prior_book_id is not None
                and prior_book_id != book["record_id"]
            ):
                raise A01MaterializerError(
                    "runner microsecond exit snapshot identity is ambiguous"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_EXIT_SNAPSHOT_UNAVAILABLE",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen_ids.add(exit_id)
        seen_intents.add(intent_id)
        micro_to_book[micro_key] = book["record_id"]
        snapshot_id = "a01-exit-" + canonical_sha256(
            {
                "book_record_id": book["record_id"],
                "book_source_sha256": book["source_sha256"],
            }
        )
        snapshots_by_book.setdefault(
            book["record_id"],
            {
                "snapshot_id": snapshot_id,
                "market_ticker": market,
                "receive_timestamp_us": book["receive_timestamp_us"],
                "yes_bids": list(book["yes_bids"]),
                "yes_asks": list(book["yes_asks"]),
                "book_valid": True,
                "gap_free": True,
                "source_sha256": book["source_sha256"],
            },
        )
        closures.append(
            {
                "intent_id": intent_id,
                "exit_snapshot_id": snapshot_id,
                "exit_decision_ts_ns": decision,
                "exit_limit_price_e4": exit_limit,
                "maximum_snapshot_age_us": max_age_us,
                "settlement_id": settlement_ids_by_market.get(market),
            }
        )
    for intent_id in sorted(set(intent_context) - seen_intents):
        blockers.append(
            _blocker(
                "BLOCK_A01_EXIT_DECISION_AUTHORITY_MISSING",
                intent_id,
            )
        )
    snapshots = list(snapshots_by_book.values())
    snapshots.sort(
        key=lambda row: (
            row["market_ticker"],
            row["receive_timestamp_us"],
            row["snapshot_id"],
        )
    )
    closures.sort(key=lambda row: row["intent_id"])
    return tuple(snapshots), tuple(closures), blockers


def _settlement_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    selected_market_names: frozenset[str],
    entry_decision_ns: Mapping[str, int],
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, str]]]:
    required = {
        "settlement_id",
        "market_ticker",
        "status",
        "finalized",
        "yes_settlement_value_e4",
        "observed_at_ns",
        "revision",
        "source_sha256",
    }
    parsed_by_market: dict[str, list[dict[str, Any]]] = {}
    blockers: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        fallback = f"settlement[{index}]"
        missing = sorted(required - set(raw))
        if missing:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_FIELD_MISSING",
                    ",".join(missing),
                    record_id=fallback,
                )
            )
            continue
        record_id = str(raw.get("settlement_id") or fallback)
        try:
            settlement_id = _text(
                "settlement_id", raw["settlement_id"]
            )
            market = _text("market_ticker", raw["market_ticker"])
            status = _text("status", raw["status"])
            if status not in ALLOWED_SETTLEMENT_STATUSES:
                raise A01MaterializerError(
                    f"unknown settlement status: {status}"
                )
            finalized = raw["finalized"]
            if type(finalized) is not bool:
                raise A01MaterializerError("finalized must be bool")
            value = raw["yes_settlement_value_e4"]
            if finalized:
                value = _plain_int(
                    "yes_settlement_value_e4",
                    value,
                    minimum=0,
                    maximum=PRICE_SCALE_E4,
                )
            elif value is not None:
                raise A01MaterializerError(
                    "nonfinal settlement cannot expose payout"
                )
            observed = _plain_int(
                "observed_at_ns", raw["observed_at_ns"], minimum=0
            )
            revision = _plain_int(
                "revision", raw["revision"], minimum=0
            )
            source = _require_sha(
                "source_sha256", raw["source_sha256"]
            )
            if finalized != (status == "FINALIZED"):
                raise A01MaterializerError(
                    "only FINALIZED may carry final payout authority"
                )
            if settlement_id in seen:
                raise A01MaterializerError(
                    f"duplicate settlement_id {settlement_id}"
                )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_INVALID",
                    str(exc),
                    record_id=record_id,
                )
            )
            continue
        seen.add(settlement_id)
        if market not in selected_market_names:
            continue
        parsed_by_market.setdefault(market, []).append(
            {
                "settlement_id": settlement_id,
                "market_ticker": market,
                "status": status,
                "finalized": finalized,
                "yes_settlement_value_e4": value,
                "observed_at_ns": observed,
                "revision": revision,
                "source_sha256": source,
            }
        )
    final_rows: list[dict[str, Any]] = []
    for market in sorted(selected_market_names):
        history = parsed_by_market.get(market, [])
        if not history:
            blockers.append(
                _blocker(
                    "BLOCK_A01_FINAL_SETTLEMENT_MISSING",
                    market,
                )
            )
            continue
        by_revision: dict[int, list[dict[str, Any]]] = {}
        for row in history:
            by_revision.setdefault(row["revision"], []).append(row)
        duplicate_revisions = sorted(
            revision
            for revision, rows in by_revision.items()
            if len(rows) != 1
        )
        if duplicate_revisions:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_REVISION_CONFLICT",
                    (
                        f"{market}:"
                        + ",".join(map(str, duplicate_revisions))
                    ),
                )
            )
            continue
        ordered = sorted(history, key=lambda row: row["revision"])
        if any(
            later["observed_at_ns"] <= earlier["observed_at_ns"]
            for earlier, later in zip(ordered, ordered[1:])
        ):
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_REVISION_TIME_CONFLICT",
                    market,
                )
            )
            continue
        finalized_history = [
            row for row in ordered if row["finalized"]
        ]
        latest = ordered[-1]
        if len(finalized_history) != 1 or latest["finalized"] is not True:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_LATEST_NOT_UNIQUE_FINAL",
                    market,
                )
            )
            continue
        if latest["observed_at_ns"] <= entry_decision_ns[market]:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SETTLEMENT_PRECEDES_ENTRY",
                    market,
                )
            )
            continue
        final_rows.append(latest)
    final_rows.sort(
        key=lambda row: (
            row["market_ticker"],
            row["observed_at_ns"],
            row["settlement_id"],
        )
    )
    return tuple(final_rows), blockers


def a01_extraction_spec() -> dict[str, Any]:
    """Machine-readable source locations and forbidden substitutions."""

    return {
        "schema_version": EXTRACTION_SPEC_SCHEMA,
        "split": {
            "train_dates_utc": list(TRAIN_DATES),
            "validation_dates_utc": list(VALIDATION_DATES),
            "validation_read_rule": (
                "07-17 MAY OPEN ONLY AFTER TRAIN ARTIFACT SHA IS FROZEN"
            ),
        },
        "existing_receipts": {
            "deep03_input_receipt": (
                "/Users/ritcardo/HFT-BOT-c1-real-fill-01/tmp/"
                "c1_real_fill_run/"
                "c1-real-fill-20260722-214710-8a62e78/work/results/"
                "INPUT_RECEIPT.json"
            ),
            "deep03_l2_audit": (
                "Deepresearch V3/run_2026-07-22_D3-W2A/"
                "L2_INDEPENDENT_AUDIT_RECEIPT.json"
            ),
            "exact_release_pins": (
                "Deepresearch V3/pnl_spine/inputs/"
                "EXACT_V3_RELEASES_20260710_17.json"
            ),
        },
        "w09_observed_inventory": {
            "checkpoint_root_reported_prefix": (
                "/srv/w09-research/checkpoints/source-c080..."
            ),
            "l2_replay": {
                "parquet_files": 51,
                "rows": 85_912_536,
                "scope": "top/depth3 only; forbidden for full IOC walk",
            },
            "l2_physical": {
                "parquet_files": 102,
                "columns": [
                    "date",
                    "t_us",
                    "recv_wall_ns",
                    "recv_mono_ns",
                    "market_ticker",
                    "event_proxy",
                    "sport",
                    "family",
                    "msg_type",
                    "side",
                    "price_e4",
                    "delta_e4",
                    "yes_levels",
                    "no_levels",
                    "ws_sid",
                    "ws_seq",
                ],
                "status": (
                    "raw ingredients exist, but audited full-depth replay "
                    "adapter and replay receipt do not"
                ),
            },
            "dim_market_date": {
                "columns": [
                    "date",
                    "market_ticker",
                    "event_ticker",
                    "occurrence_us",
                    "close_us",
                ],
                "missing_direct_authorities": [
                    "scheduled_start_ts_ns_and_asof",
                    "tick_size_e4_and_asof",
                    "lifecycle_open_paused_and_asof",
                    "standard_binary_mve_and_asof",
                ],
            },
            "trades_market": {"parquet_files": 256},
            "settlement_candidate": {
                "candidate_date": "2026-07-20",
                "version_id_reported_prefix": "Y323...",
                "sha256_reported_prefix": "54053...",
                "validation_2026_07_17_l2_market_overlap": 0,
                "status": "FORBIDDEN_AS_07_17_TERMINAL_AUTHORITY",
            },
        },
        "record_sources": {
            "rows": {
                "l2_stage": "l2_replay",
                "l2_manifest_sha256": (
                    "3cf24b5a28d27badd2fcd1638ac14e2fe27f2558a9a90"
                    "e377b7eedf6a84a48f2"
                ),
                "metadata_stage": "dim_market_date",
                "metadata_manifest_sha256": (
                    "7af45436bf2bc41b6f94ccc6c11f44f70ce5d4f18fe62"
                    "fb6a7666010f7def6dc"
                ),
                "l2_required_columns": [
                    "date",
                    "recv_wall_ns",
                    "recv_mono_ns",
                    "stream_epoch",
                    "t_us",
                    "market_ticker",
                    "ws_sid",
                    "ws_seq",
                    "sid_gap_generation",
                    "market_revalidated_gap_generation",
                    "sid_sequence_valid",
                    "classification",
                    "snapshot_epoch",
                    "book_valid",
                    "topology",
                    "bid_e4",
                    "ask_e4",
                ],
                "metadata_required_direct_fields": [
                    "event_ticker",
                    "tick_size_e4",
                    "identity_asof_ns",
                    "tick_size_asof_ns",
                    "scheduled_start_ts_ns",
                    "scheduled_start_asof_ns",
                    "lifecycle_open",
                    "paused",
                    "lifecycle_asof_ns",
                    "standard_binary_clob",
                    "mve",
                    "market_structure_asof_ns",
                ],
                "derived_train_features_required": [
                    "liquidity_stratum",
                    "toxicity_score_e6_and_asof_ns",
                    "adverse_width_e4_and_asof_ns",
                    "risk_adjusted_score_e6",
                    "worst_state_loss_e6",
                    "net_capture_margin_ticks",
                ],
            },
            "public_trades": {
                "stage": "trades_market",
                "manifest_sha256": (
                    "5d963ea39487c0991f3f759fa2d6872e0d50deadd5fbacc"
                    "41e73e8a2a62f01ee"
                ),
                "required_columns": [
                    "date",
                    "exchange_ts_us",
                    "local_recv_ts_us",
                    "recv_wall_ns",
                    "recv_mono_ns",
                    "market_ticker",
                    "trade_id",
                    "yes_price_e4",
                    "count_e4",
                    "taker_side",
                    "exact_source_object_sha256",
                ],
                "adapter_contract": {
                    "runner_timestamp_us": "local_recv_ts_us",
                    "required_equality": (
                        "local_recv_ts_us == recv_wall_ns // 1000"
                    ),
                    "clock_domain": "LOCAL_RECEIVE_WALL",
                    "exchange_ts_us_role": (
                        "preserved source event metadata; never used for "
                        "activation/cancel comparisons"
                    ),
                },
            },
            "exit_snapshots": {
                "source": (
                    "exact raw L2 snapshot+delta objects from pinned V3 "
                    "release, replayed in receive order"
                ),
                "required": (
                    "full YES bid and reconstructed YES ask ladders at an "
                    "explicit measured-latency effective exit time"
                ),
                "forbidden_substitution": (
                    "l2_replay top/depth3 columns are not a full IOC walk"
                ),
            },
            "settlements": {
                "source": (
                    "exact versioned market terminal records from a trusted "
                    "terminal adapter"
                ),
                "required": (
                    "FINALIZED status, exact payout, revision, receive clock, "
                    "source object SHA"
                ),
                "current_stage": None,
                "minimum_adapter_input": [
                    "exact_request_url",
                    "market_ticker",
                    "raw_response_bytes_sha256",
                    "fetch_wall_ns_before_after",
                    "fetch_monotonic_ns_before_after",
                    "status",
                    "result",
                    "settlement_value_dollars",
                    "settlement_ts",
                    "occurrence_datetime",
                    "price_ranges",
                ],
                "minimum_adapter_output": [
                    "settlement_id",
                    "market_ticker",
                    "status",
                    "finalized",
                    "yes_settlement_value_e4",
                    "observed_at_ns",
                    "revision",
                    "source_sha256",
                ],
                "network_contract": (
                    "official allowlisted endpoint only; no proxy, no "
                    "redirect, raw response create-once"
                ),
                "scheduled_start_prohibition": (
                    "occurrence_datetime is not scheduled-start authority"
                ),
            },
        },
        "current_real_data_blockers": [
            "BLOCK_A01_CHECKPOINT_PARQUETS_NOT_IN_LOCAL_RESULT_BUNDLE",
            "BLOCK_A01_PARQUET_ADAPTER_NOT_IMPLEMENTED",
            "BLOCK_A01_SOURCE_COVERAGE_MISSING",
            "BLOCK_A01_FULL_DEPTH_REPLAY_RECEIPT_MISSING",
            "BLOCK_A01_FULL_DEPTH_EXIT_SOURCE_NOT_MATERIALIZED",
            "BLOCK_A01_TRAIN_FEATURES_NOT_MATERIALIZED",
            "BLOCK_A01_TRAIN_ARTIFACT_EXTERNAL_PIN_MISSING",
            "BLOCK_A01_SELECTION_SCORE_AUTHORITY_MISSING",
            "BLOCK_A01_OPPORTUNITY_POLICY_EXTERNAL_PIN_MISSING",
            "BLOCK_A01_SCHEDULED_START_AUTHORITY_MISSING",
            "BLOCK_A01_TICK_TABLE_AUTHORITY_MISSING",
            "BLOCK_A01_LIFECYCLE_AUTHORITY_MISSING",
            "BLOCK_A01_MEASURED_LATENCY_AUTHORITY_MISSING",
            "BLOCK_A01_FEE_FACTS_AUTHORITY_MISSING",
            "BLOCK_A01_EXIT_DECISION_AUTHORITY_MISSING",
            "BLOCK_A01_SETTLEMENT_SOURCE_MISSING",
            "BLOCK_A01_SETTLEMENT_MARKET_COVERAGE_ZERO",
            "BLOCK_A01_LATENCY_FEE_DERIVATION_NOT_BOUND",
            "BLOCK_A01_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
            "BLOCK_A01_TRADE_CLOCK_EPOCH_AND_NS_ENGINE_MISSING",
            "BLOCK_A01_OPPORTUNITY_DENOMINATOR_LEDGER_MISSING",
        ],
        "forbidden_substitutions": [
            "C1 CAMPAIGNS is depletion evidence, not A01 spread dwell",
            "C1 latency_id/place_us/cancel_us are scenarios, not measured latency",
            "C1 MARKOUTS is gross diagnostic data, not an exact IOC exit",
            "event_proxy is not promoted to root_event_id",
            "close_time is not silently promoted to scheduled start",
            (
                "occurrence_datetime is not promoted to scheduled start "
                "without separately audited exact equivalence"
            ),
            (
                "2026-07-20 settlements with zero 2026-07-17 L2 market "
                "overlap are not terminal authority"
            ),
            "missing fee, latency, exit or settlement is never zero-filled",
        ],
    }


def materialize_a01(
    *,
    stage: str,
    freeze_document: Mapping[str, Any],
    training_artifact: A01TrainingArtifact | None,
    expected_training_artifact_sha256: str | None = None,
    source_coverage: A01SourceCoverage | None = None,
    expected_source_coverage_sha256: str | None = None,
    expected_opportunity_policy_sha256: str | None = None,
    expected_fee_facts_sha256: str | None = None,
    expected_measured_latency_receipt_sha256: str | None = None,
    expected_selection_policy_sha256: str | None = None,
    book_records: Sequence[Mapping[str, Any]],
    metadata_records: Sequence[Mapping[str, Any]],
    opportunity_records: Sequence[Mapping[str, Any]],
    selection_records: Sequence[Mapping[str, Any]],
    public_trade_records: Sequence[Mapping[str, Any]],
    exit_requests: Sequence[Mapping[str, Any]],
    settlement_records: Sequence[Mapping[str, Any]],
) -> A01MaterializationResult:
    """Materialize one frozen cohort without manufacturing missing evidence."""

    if stage not in ALLOWED_STAGES:
        raise A01MaterializerError("stage must be TRAIN or VALIDATION")
    config = A01Config.from_freeze(freeze_document)
    cohort = TRAIN_DATES if stage == "TRAIN" else VALIDATION_DATES
    allowed_dates = frozenset(cohort)
    blockers: list[dict[str, str]] = []
    source_coverage_sha: str | None = None
    fee_facts_sha: str | None = None
    measured_latency_sha: str | None = None
    selection_policy_sha: str | None = None
    opportunity_policy_sha: str | None = None
    if source_coverage is None:
        blockers.append(
            _blocker(
                "BLOCK_A01_SOURCE_COVERAGE_MISSING",
                (
                    "an empty collection cannot be distinguished from "
                    "an incomplete extraction"
                ),
            )
        )
    else:
        source_coverage_sha = source_coverage.sha256
        fee_facts_sha = source_coverage.fee_facts_sha256
        measured_latency_sha = (
            source_coverage.measured_latency_receipt_sha256
        )
        selection_policy_sha = (
            source_coverage.selection_policy_sha256
        )
        opportunity_policy_sha = (
            source_coverage.opportunity_policy_sha256
        )
        if source_coverage.stage != stage:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SOURCE_COVERAGE_STAGE_MISMATCH",
                    (
                        f"coverage={source_coverage.stage},"
                        f"materialization={stage}"
                    ),
                )
            )
        if source_coverage.cohort_dates_utc != cohort:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SOURCE_COVERAGE_COHORT_MISMATCH",
                    "source coverage does not bind the active cohort",
                )
            )
        actual_collections = {
            "book_records": book_records,
            "exit_requests": exit_requests,
            "metadata_records": metadata_records,
            "opportunity_records": opportunity_records,
            "public_trade_records": public_trade_records,
            "selection_records": selection_records,
            "settlement_records": settlement_records,
        }
        coverage_by_name = {
            row.collection: row
            for row in source_coverage.collections
        }
        for collection in source_coverage.collections:
            actual_records = actual_collections[collection.collection]
            actual_count = len(actual_records)
            if collection.record_count != actual_count:
                blockers.append(
                    _blocker(
                        "BLOCK_A01_SOURCE_COVERAGE_COUNT_MISMATCH",
                        (
                            f"{collection.collection}:"
                            f"receipt={collection.record_count},"
                            f"actual={actual_count}"
                        ),
                    )
                )
            actual_records_sha = canonical_sha256(
                [dict(row) for row in actual_records]
            )
            if collection.records_sha256 != actual_records_sha:
                blockers.append(
                    _blocker(
                        "BLOCK_A01_SOURCE_COVERAGE_CONTENT_MISMATCH",
                        collection.collection,
                    )
                )
        if expected_opportunity_policy_sha256 is None:
            blockers.append(
                _blocker(
                    "BLOCK_A01_OPPORTUNITY_POLICY_EXTERNAL_PIN_MISSING",
                    "opportunity enumeration cannot self-authorize",
                )
            )
        else:
            try:
                expected_opportunity_policy_sha256 = _require_sha(
                    "expected_opportunity_policy_sha256",
                    expected_opportunity_policy_sha256,
                )
            except A01MaterializerError as exc:
                blockers.append(
                    _blocker(
                        "BLOCK_A01_OPPORTUNITY_POLICY_EXTERNAL_PIN_INVALID",
                        str(exc),
                    )
                )
            else:
                if (
                    source_coverage.opportunity_policy_sha256
                    != expected_opportunity_policy_sha256
                ):
                    blockers.append(
                        _blocker(
                            "BLOCK_A01_OPPORTUNITY_POLICY_EXTERNAL_PIN_MISMATCH",
                            "coverage uses an unapproved opportunity policy",
                        )
                    )
        external_binding_pins = (
            (
                "FEE_FACTS",
                source_coverage.fee_facts_sha256,
                expected_fee_facts_sha256,
            ),
            (
                "MEASURED_LATENCY",
                source_coverage.measured_latency_receipt_sha256,
                expected_measured_latency_receipt_sha256,
            ),
            (
                "SELECTION_POLICY",
                source_coverage.selection_policy_sha256,
                expected_selection_policy_sha256,
            ),
        )
        for label, actual, expected in external_binding_pins:
            if expected is None:
                blockers.append(
                    _blocker(
                        f"BLOCK_A01_{label}_EXTERNAL_PIN_MISSING",
                        f"{label.lower()} cannot self-authorize",
                    )
                )
                continue
            try:
                checked_expected = _require_sha(
                    f"expected_{label.lower()}_sha256",
                    expected,
                )
            except A01MaterializerError as exc:
                blockers.append(
                    _blocker(
                        f"BLOCK_A01_{label}_EXTERNAL_PIN_INVALID",
                        str(exc),
                    )
                )
                continue
            if checked_expected != actual:
                blockers.append(
                    _blocker(
                        f"BLOCK_A01_{label}_EXTERNAL_PIN_MISMATCH",
                        f"coverage uses unapproved {label.lower()}",
                    )
                )
        try:
            schedule_sha = a01_opportunity_schedule_sha256(
                opportunity_records
            )
        except A01MaterializerError as exc:
            blockers.append(
                _blocker(
                    "BLOCK_A01_OPPORTUNITY_SCHEDULE_INVALID",
                    str(exc),
                )
            )
        else:
            if (
                schedule_sha
                != source_coverage.opportunity_schedule_sha256
            ):
                blockers.append(
                    _blocker(
                        "BLOCK_A01_OPPORTUNITY_SCHEDULE_INCOMPLETE",
                        "selection decision set differs from authority",
                    )
                )
        replay_receipt_pin = coverage_by_name[
            "book_records"
        ].manifest_sha256
        if any(
            row.get("replay_receipt_sha256") != replay_receipt_pin
            for row in book_records
        ):
            blockers.append(
                _blocker(
                    "BLOCK_A01_FULL_DEPTH_REPLAY_RECEIPT_MISMATCH",
                    (
                        "every full-depth row must bind the externally "
                        "pinned replay receipt"
                    ),
                )
            )
        if expected_source_coverage_sha256 is None:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SOURCE_COVERAGE_EXTERNAL_PIN_MISSING",
                    "source coverage cannot be self-authorizing",
                )
            )
        else:
            try:
                expected_source_coverage_sha256 = _require_sha(
                    "expected_source_coverage_sha256",
                    expected_source_coverage_sha256,
                )
            except A01MaterializerError as exc:
                blockers.append(
                    _blocker(
                        "BLOCK_A01_SOURCE_COVERAGE_EXTERNAL_PIN_INVALID",
                        str(exc),
                    )
                )
            else:
                if (
                    source_coverage_sha
                    != expected_source_coverage_sha256
                ):
                    blockers.append(
                        _blocker(
                            "BLOCK_A01_SOURCE_COVERAGE_EXTERNAL_PIN_MISMATCH",
                            "active coverage differs from authority pin",
                        )
                    )
    if training_artifact is None:
        blockers.append(
            _blocker(
                "BLOCK_A01_TRAIN_ARTIFACT_MISSING",
                "p75/p90 gates cannot be inferred or zero-filled",
            )
        )
        threshold_index: dict[str, A01GateThreshold] = {}
        artifact_sha: str | None = None
    else:
        artifact_sha = training_artifact.sha256
        threshold_index = {
            row.stratum_key: row
            for row in training_artifact.thresholds
        }
        if expected_training_artifact_sha256 is None:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRAIN_ARTIFACT_EXTERNAL_PIN_MISSING",
                    (
                        "validation may not trust a self-declared TRAIN "
                        "artifact"
                    ),
                )
            )
        else:
            try:
                expected_training_artifact_sha256 = _require_sha(
                    "expected_training_artifact_sha256",
                    expected_training_artifact_sha256,
                )
            except A01MaterializerError as exc:
                blockers.append(
                    _blocker(
                        "BLOCK_A01_TRAIN_ARTIFACT_EXTERNAL_PIN_INVALID",
                        str(exc),
                    )
                )
            else:
                if artifact_sha != expected_training_artifact_sha256:
                    blockers.append(
                        _blocker(
                            "BLOCK_A01_TRAIN_ARTIFACT_EXTERNAL_PIN_MISMATCH",
                            "active TRAIN artifact differs from authority pin",
                        )
                    )
        if training_artifact.freeze_sha256 != config.freeze_sha256:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRAIN_ARTIFACT_FREEZE_MISMATCH",
                    "artifact is not bound to the active freeze",
                )
            )
        if training_artifact.train_dates_utc != TRAIN_DATES:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRAIN_ARTIFACT_SPLIT_MISMATCH",
                    "artifact is not 07-12/15 TRAIN-only",
                )
            )

    runtime_bindings: RuntimeBindings | None = None
    runtime_bindings_sha: str | None = None
    if source_coverage is not None and artifact_sha is not None:
        runtime_bindings = RuntimeBindings(
            fee_facts_sha256=source_coverage.fee_facts_sha256,
            latency_receipt_sha256=(
                source_coverage.measured_latency_receipt_sha256
            ),
            root_map_sha256=source_coverage.root_map_sha256,
            scheduled_start_source_sha256=(
                source_coverage.scheduled_start_source_sha256
            ),
            risk_policy_sha256=source_coverage.risk_policy_sha256,
            terminal_contract_sha256=(
                source_coverage.terminal_contract_sha256
            ),
            strict_fill_evidence_sha256=(
                source_coverage.strict_fill_evidence_sha256
            ),
            card_parameter_artifact_sha256=artifact_sha,
        )
        runtime_bindings_sha = runtime_bindings.sha256

    observed_dates = {
        str(row.get("date"))
        for collection in (
            book_records,
            metadata_records,
            selection_records,
            public_trade_records,
            exit_requests,
        )
        for row in collection
        if row.get("date") is not None
    }
    escaped = sorted(observed_dates - allowed_dates)
    if escaped:
        blockers.append(
            _blocker(
                "BLOCK_A01_COHORT_DATE_ESCAPE",
                f"{stage}:{','.join(escaped)}",
            )
        )
    missing = sorted(allowed_dates - observed_dates)
    if missing:
        blockers.append(
            _blocker(
                "BLOCK_A01_COHORT_DATE_MISSING",
                f"{stage}:{','.join(missing)}",
            )
        )
    required_cohort_sources = (
        ("L2", book_records),
        ("METADATA", metadata_records),
        ("OPPORTUNITY", opportunity_records),
        ("SELECTION", selection_records),
    )
    for source_name, records in required_cohort_sources:
        source_dates = {
            str(row.get("date"))
            for row in records
            if row.get("date") is not None
        }
        source_missing = sorted(allowed_dates - source_dates)
        if source_missing:
            blockers.append(
                _blocker(
                    f"BLOCK_A01_{source_name}_COHORT_DATE_MISSING",
                    f"{stage}:{','.join(source_missing)}",
                )
            )

    metadata, metadata_blocks = _metadata_index(
        metadata_records,
        allowed_dates=allowed_dates,
    )
    blockers.extend(metadata_blocks)
    books, book_blocks = _prepare_books(
        book_records,
        allowed_dates=allowed_dates,
        metadata=metadata,
        config=config,
    )
    blockers.extend(book_blocks)
    opportunities, opportunity_blocks = _opportunity_rows(
        opportunity_records,
        allowed_dates=allowed_dates,
        metadata=metadata,
        opportunity_policy_sha256=opportunity_policy_sha,
    )
    blockers.extend(opportunity_blocks)
    selections, selection_blocks = _selection_rows(
        selection_records,
        allowed_dates=allowed_dates,
        metadata=metadata,
        fee_facts_sha256=fee_facts_sha,
        measured_latency_receipt_sha256=measured_latency_sha,
        selection_policy_sha256=selection_policy_sha,
        opportunities=opportunities,
    )
    blockers.extend(selection_blocks)
    if not selections:
        blockers.append(
            _blocker(
                "BLOCK_A01_SELECTION_SCORE_AUTHORITY_MISSING",
                "no source-bound deterministic root/market score exists",
            )
        )

    books_by_market: dict[
        tuple[str, str], tuple[list[int], list[dict[str, Any]]]
    ] = {}
    for row in books:
        key = (row["date"], row["market_ticker"])
        if key not in books_by_market:
            books_by_market[key] = ([], [])
        books_by_market[key][0].append(row["receive_timestamp_ns"])
        books_by_market[key][1].append(row)

    rows: list[dict[str, Any]] = []
    row_dates: dict[str, str] = {}
    selected_markets: set[tuple[str, str]] = set()
    for selection in selections:
        date = selection["date"]
        market = selection["market_ticker"]
        decision = selection["decision_ts_ns"]
        meta = metadata[(date, market)]
        book = _book_at_or_before(
            books_by_market,
            date=date,
            market=market,
            at_ns=decision,
        )
        if book is None:
            blockers.append(
                _blocker(
                    "BLOCK_A01_DECISION_BOOK_MISSING",
                    f"{date}/{market}/{decision}",
                    record_id=selection["selection_id"],
                )
            )
            continue
        if decision - book["receive_timestamp_ns"] > config.book_ttl_ns:
            blockers.append(
                _blocker(
                    "BLOCK_A01_DECISION_BOOK_STALE_250MS",
                    f"{date}/{market}/{decision}",
                    record_id=selection["selection_id"],
                )
            )
            continue
        if not book.get("_continuity_valid"):
            blockers.append(
                _blocker(
                    "BLOCK_A01_DECISION_BOOK_GAP_RESET_EPOCH",
                    f"{date}/{market}/{decision}",
                    record_id=selection["selection_id"],
                )
            )
            continue
        start_asof = int(meta["scheduled_start_asof_ns"])
        if start_asof > decision:
            blockers.append(
                _blocker(
                    "BLOCK_A01_SCHEDULE_LOOKAHEAD",
                    f"{date}/{market}/{decision}",
                    record_id=selection["selection_id"],
                )
            )
            continue
        phase = _phase(
            int(meta["scheduled_start_ts_ns"]),
            decision,
        )
        stratum_key = _stratum(
            sport=str(meta["sport"]),
            phase=phase,
            liquidity_stratum=book["liquidity_stratum"],
        )
        threshold = threshold_index.get(stratum_key)
        if threshold is None:
            blockers.append(
                _blocker(
                    "BLOCK_A01_TRAIN_STRATUM_UNSEEN",
                    stratum_key,
                    record_id=selection["selection_id"],
                )
            )
            continue
        features_asof = max(
            book["toxicity_asof_ns"],
            book["adverse_width_asof_ns"],
            book["liquidity_stratum_asof_ns"],
            selection["asof_ns"],
            start_asof,
            int(meta["identity_asof_ns"]),
            int(meta["tick_size_asof_ns"]),
            int(meta["lifecycle_asof_ns"]),
            int(meta["market_structure_asof_ns"]),
            book["receive_timestamp_ns"],
        )
        if features_asof > decision:
            blockers.append(
                _blocker(
                    "BLOCK_A01_FEATURE_LOOKAHEAD",
                    f"{date}/{market}/{decision}",
                    record_id=selection["selection_id"],
                )
            )
            continue
        row_id = "a01-" + canonical_sha256(
            {
                "date": date,
                "root_event_id": meta["root_event_id"],
                "market_ticker": market,
                "decision_ts_ns": decision,
                "selection_id": selection["selection_id"],
                "book_record_id": book["record_id"],
            }
        )
        normalized = NormalizedStateRow(
            row_id=row_id,
            root_event_id=str(meta["root_event_id"]),
            market_ticker=market,
            sport=str(meta["sport"]),
            decision_ts_ns=decision,
            features_asof_ns=features_asof,
            book_observed_at_ns=book["receive_timestamp_ns"],
            tick_size_e4=int(meta["tick_size_e4"]),
            scheduled_start_ts_ns=int(
                meta["scheduled_start_ts_ns"]
            ),
            scheduled_start_asof_ns=start_asof,
            best_yes_bid_e4=book["best_yes_bid_e4"],
            best_yes_ask_e4=book["best_yes_ask_e4"],
            state_started_at_ns=book["_spread_start_ns"],
            warmup_started_at_ns=book["_coverage_start_ns"],
            net_capture_margin_ticks=selection[
                "net_capture_margin_ticks"
            ],
            update_count_60s=book["_update_count_60s"],
            scheduled_phase=phase,
            activity_gate_passed=(
                book["_update_count_60s"] >= threshold.activity_p75
            ),
            toxicity_gate_passed=(
                book["toxicity_score_e6"]
                <= threshold.toxicity_p90_e6
            ),
            adverse_width_gate_passed=(
                book["adverse_width_e4"]
                <= threshold.adverse_width_p90_e4
            ),
            book_valid=bool(book["book_valid"]),
            lifecycle_open=bool(meta["lifecycle_open"]),
            gap=bool(book["gap"]),
            paused=bool(meta["paused"]),
            locked=book["topology"] == "CROSSED_OR_LOCKED",
            crossed=book["topology"] == "CROSSED_OR_LOCKED",
        )
        rows.append(asdict(normalized))
        row_dates[row_id] = date
        selected_markets.add((date, market))
    rows.sort(
        key=lambda row: (
            row["decision_ts_ns"],
            row["root_event_id"],
            row["market_ticker"],
            row["row_id"],
        )
    )

    for date, market in sorted(selected_markets):
        meta = metadata[(date, market)]
        if (
            meta["standard_binary_clob"] is not True
            or meta["mve"] is not False
        ):
            blockers.append(
                _blocker(
                    "BLOCK_A01_NONSTANDARD_OR_MVE_MARKET",
                    f"{date}/{market}",
                )
            )
        if meta["lifecycle_open"] is not True:
            blockers.append(
                _blocker(
                    "BLOCK_A01_LIFECYCLE_NOT_OPEN",
                    f"{date}/{market}",
                )
            )

    trades, trade_blocks = _trade_rows(
        public_trade_records,
        allowed_dates=allowed_dates,
        markets=frozenset(selected_markets),
        metadata=metadata,
    )
    blockers.extend(trade_blocks)
    settlements, settlement_blocks = _settlement_rows(
        settlement_records,
        selected_market_names=frozenset(
            market for _date, market in selected_markets
        ),
        entry_decision_ns={
            market: max(
                int(selection["decision_ts_ns"])
                for selection in selections
                if selection["market_ticker"] == market
            )
            for _date, market in selected_markets
        },
    )
    blockers.extend(settlement_blocks)
    settlement_ids_by_market = {
        row["market_ticker"]: row["settlement_id"]
        for row in settlements
    }
    intent_context: dict[str, dict[str, Any]] = {}
    if runtime_bindings is not None:
        adapter = FrozenExperimentAdapters(freeze_document)
        for row in rows:
            normalized = NormalizedStateRow(**row)
            decision_result = adapter.evaluate_a01(
                normalized,
                runtime_bindings,
            )
            if decision_result.status is not DecisionStatus.ORDER_INTENTS:
                continue
            for intent in decision_result.intents:
                intent_context[intent.intent_id] = {
                    "row_id": row["row_id"],
                    "date": row_dates[row["row_id"]],
                    "market_ticker": row["market_ticker"],
                    "entry_decision_ts_ns": row["decision_ts_ns"],
                    "tick_size_e4": row["tick_size_e4"],
                    "expire_after_ms": intent.expire_after_ms,
                }
    exits, closures, exit_blocks = _exit_rows(
        exit_requests,
        allowed_dates=allowed_dates,
        books_by_market=books_by_market,
        intent_context=intent_context,
        settlement_ids_by_market=settlement_ids_by_market,
        measured_latency_receipt_sha256=measured_latency_sha,
        cancel_latency_ns=(
            source_coverage.cancel_latency_ns
            if source_coverage is not None
            else None
        ),
        ioc_exit_latency_ns=(
            source_coverage.ioc_exit_latency_ns
            if source_coverage is not None
            else None
        ),
        config=config,
    )
    blockers.extend(exit_blocks)

    return A01MaterializationResult(
        stage=stage,
        cohort_dates_utc=cohort,
        training_artifact_sha256=artifact_sha,
        source_coverage_sha256=source_coverage_sha,
        fee_facts_sha256=fee_facts_sha,
        measured_latency_receipt_sha256=measured_latency_sha,
        selection_policy_sha256=selection_policy_sha,
        opportunity_policy_sha256=opportunity_policy_sha,
        runtime_bindings_sha256=runtime_bindings_sha,
        rows=tuple(rows),
        public_trades=trades,
        exit_snapshots=exits,
        closures=closures,
        settlements=settlements,
        blockers=_sorted_blockers(blockers),
        extraction_spec=a01_extraction_spec(),
    )
