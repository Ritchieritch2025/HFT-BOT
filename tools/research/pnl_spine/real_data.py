"""Hash-pinned, read-only adapters for the existing real research artifacts.

This module is intentionally narrower than a strategy backtest.  It answers
one question before the PnL runner is allowed to do any economic arithmetic:
which fields can be carried, without inference, from the existing C1 and
DeepResearch V3 artifacts into the frozen experiment contracts?

The current C1 artifacts are valuable strict-fill and gross-markout
engineering evidence.  They are *not* net PnL:

* their latency labels are scenarios, not measured production latency;
* ``MARKOUTS`` contains top-of-book gross marks, not exact L2 exits;
* no settlement truth is present;
* the C1 fee receipt explicitly failed closed.

All source files are supplied explicitly, must be regular files reached
without any symlink component, and are read only after their SHA-256 has been
verified.  This module performs no network, AWS, credential, or write action.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence

from .contracts import canonical_sha256
from .experiments import AdapterContractError, NormalizedStateRow
from .provenance import atomic_write_receipt


SCHEMA_VERSION = "pnl-spine-real-data-audit-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 64 * 1024 * 1024

# Exact local artifacts inspected on 2026-07-23.  Paths are deliberately not
# embedded: callers must name every file they intend to read.
KNOWN_20260722_PINS = {
    "analysis_ledger": (
        "3c33faa8122d859e9ca7259a5e4b032a55ae5aa37a9d95b354875dca813831ef"
    ),
    "campaigns": (
        "aa5ac77c9300d589fed68ac925a99103ae5cda92b3aa789a75b739777f187150"
    ),
    "fill_slices": (
        "4756e58b8b6eed04230d41ffe49218848c8975fe4acf3cc8a9c72fd40691c504"
    ),
    "markouts": (
        "b3ee7fb40ca2739f548ba592ca007f101fc4dd9d7894f0297be3b7c5af68f8a4"
    ),
    "deep03_data_quality": (
        "9e37aa6e85e45e61b50b51c8c28dbc306e9f407517630fdc0d89fba618de0459"
    ),
    "deep03_l2_audit": (
        "677d1f4db04dc8d36bf1607ec06c69ed1dd96c07e80ded2e808834550cdc4690"
    ),
}


class RealDataError(ValueError):
    """A real-data source is unsafe, unbound, malformed, or contradictory."""


@dataclass(frozen=True)
class RealInputPaths:
    """Every read path is explicit; no directory discovery is performed."""

    analysis_ledger: Path
    campaigns: Path
    fill_slices: Path
    markouts: Path
    deep03_data_quality: Path
    deep03_l2_audit: Path


@dataclass(frozen=True)
class RealInputPins:
    analysis_ledger: str
    campaigns: str
    fill_slices: str
    markouts: str
    deep03_data_quality: str
    deep03_l2_audit: str

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
                raise RealDataError(f"{name} must be a lowercase SHA-256")

    @classmethod
    def known_20260722(cls) -> "RealInputPins":
        return cls(**KNOWN_20260722_PINS)


@dataclass(frozen=True)
class SourceInventoryRow:
    source_id: str
    path: str
    sha256: str
    size_bytes: int
    row_count: int | None
    dates_utc: tuple[str, ...]
    classification: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperimentDataStatus:
    experiment_id: str
    state: str
    source_candidate_rows: int
    source_candidate_opportunities: int
    normalized_state_rows: int
    strict_fill_rows: int
    gross_markout_rows: int
    exact_exit_rows: int
    settled_rows: int
    net_pnl_rows: int
    blocker_codes: tuple[str, ...]
    allowed_claims: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "source_candidate_rows",
            "source_candidate_opportunities",
            "normalized_state_rows",
            "strict_fill_rows",
            "gross_markout_rows",
            "exact_exit_rows",
            "settled_rows",
            "net_pnl_rows",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise RealDataError(f"{name} must be a nonnegative integer")
        if self.net_pnl_rows:
            raise RealDataError(
                "the inspected C1/Deep03 artifacts cannot contain net PnL rows"
            )
        if self.exact_exit_rows or self.settled_rows:
            raise RealDataError(
                "the inspected artifacts do not bind exact exits or settlements"
            )


@dataclass(frozen=True)
class RealDataAudit:
    schema_version: str
    source_inventory: tuple[SourceInventoryRow, ...]
    artifact_dates_utc: tuple[str, ...]
    l2_receipt_dates_utc: tuple[str, ...]
    deep03_claim_tier: str
    normalized_rows: tuple[NormalizedStateRow, ...]
    experiments: tuple[ExperimentDataStatus, ...]
    classification: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RealDataError("unknown real-data audit schema")
        if self.classification != "ENGINEERING_INPUT_ONLY_NOT_NET_PNL":
            raise RealDataError("real C1 artifacts may not be promoted to net PnL")
        if any(row.net_pnl_rows for row in self.experiments):
            raise RealDataError("net PnL rows are forbidden in this adapter")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_inventory": [
                asdict(row) for row in self.source_inventory
            ],
            "artifact_dates_utc": list(self.artifact_dates_utc),
            "l2_receipt_dates_utc": list(self.l2_receipt_dates_utc),
            "deep03_claim_tier": self.deep03_claim_tier,
            "normalized_rows": [
                asdict(row) for row in self.normalized_rows
            ],
            "experiments": [asdict(row) for row in self.experiments],
            "classification": self.classification,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_explicit_file(path: Path, expected_sha256: str) -> Path:
    """Validate an absolute regular file without following any symlink."""

    if not isinstance(path, Path):
        raise RealDataError("all source paths must be pathlib.Path values")
    if not path.is_absolute():
        raise RealDataError(f"source path must be absolute: {path}")
    if not isinstance(expected_sha256, str) or (
        SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise RealDataError("expected source SHA-256 is malformed")

    # lstat each existing component before resolve; resolve alone would hide a
    # symlink and turn the safety check into a TOCTOU-friendly path alias.
    parts = path.parts
    cursor = Path(parts[0])
    for part in parts[1:]:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise RealDataError(f"source path is unavailable: {path}") from exc
        if stat.S_ISLNK(mode):
            raise RealDataError(f"source path contains a symlink: {path}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RealDataError(f"source path is not a regular file: {path}")
    if _sha256_file(path) != expected_sha256:
        raise RealDataError(f"source SHA-256 mismatch: {path}")
    return path


def _read_json(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise RealDataError(f"JSON source exceeds {MAX_JSON_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RealDataError(f"invalid JSON source: {path}") from exc
    if not isinstance(value, Mapping):
        raise RealDataError(f"JSON source must contain an object: {path}")
    return value


def _duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RealDataError("duckdb is required to read pinned Parquet") from exc
    return duckdb


def _columns(connection: Any, path: Path) -> tuple[str, ...]:
    rows = connection.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _require_columns(
    source_id: str, columns: Sequence[str], required: Iterable[str]
) -> None:
    missing = sorted(set(required) - set(columns))
    if missing:
        raise RealDataError(
            f"{source_id} is missing columns: {','.join(missing)}"
        )


def _scalar_int(connection: Any, query: str, path: Path) -> int:
    value = connection.execute(query, [str(path)]).fetchone()[0]
    if type(value) is not int or value < 0:
        raise RealDataError("Parquet aggregate is not a nonnegative integer")
    return value


def _dates(connection: Any, path: Path) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT CAST(date AS VARCHAR) AS d "
        "FROM read_parquet(?) ORDER BY d",
        [str(path)],
    ).fetchall()
    dates = tuple(str(row[0]) for row in rows)
    if any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row) is None for row in dates):
        raise RealDataError("Parquet contains a malformed UTC date")
    return dates


def _ledger_bindings(
    ledger: Mapping[str, Any],
) -> dict[str, tuple[str, int]]:
    if ledger.get("schema_version") != "c1-analysis-artifact-ledger-v1":
        raise RealDataError("unexpected C1 artifact-ledger schema")
    artifacts = ledger.get("artifacts")
    if not isinstance(artifacts, list):
        raise RealDataError("C1 artifact ledger has no artifacts array")
    result: dict[str, tuple[str, int]] = {}
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise RealDataError("C1 artifact-ledger row is malformed")
        relative = raw.get("path")
        sha = raw.get("sha256")
        size = raw.get("bytes")
        if not isinstance(relative, str) or relative in result:
            raise RealDataError("C1 artifact-ledger path is missing or duplicate")
        if not isinstance(sha, str) or SHA256_RE.fullmatch(sha) is None:
            raise RealDataError("C1 artifact-ledger SHA is malformed")
        if type(size) is not int or size < 0:
            raise RealDataError("C1 artifact-ledger size is malformed")
        result[relative] = (sha, size)
    return result


def _validate_ledger_table(
    *,
    path: Path,
    expected_sha256: str,
    relative: str,
    ledger_bindings: Mapping[str, tuple[str, int]],
) -> None:
    binding = ledger_bindings.get(relative)
    if binding is None:
        raise RealDataError(f"C1 ledger does not bind {relative}")
    if binding != (expected_sha256, path.stat().st_size):
        raise RealDataError(f"C1 ledger contradicts local {relative}")


def _validate_deep03(
    data_quality: Mapping[str, Any],
    l2_audit: Mapping[str, Any],
) -> tuple[tuple[str, ...], str]:
    if data_quality.get("schema_version") != (
        "deep03-d3-w2a-data-quality-receipt-v1"
    ):
        raise RealDataError("unexpected Deep03 data-quality schema")
    if l2_audit.get("schema_version") != (
        "deep03-fullscope-l2-independent-audit-v2"
    ):
        raise RealDataError("unexpected Deep03 L2-audit schema")
    if (
        l2_audit.get("state") != "PASS"
        or l2_audit.get("decision") != "APPROVED_FOR_BASE_L2_INTEGRATION"
        or l2_audit.get("real_quality_receipts_assessed") is not True
        or l2_audit.get("blockers") != []
    ):
        raise RealDataError("Deep03 L2 independent audit is not a clean PASS")
    claim_tier = l2_audit.get("approved_claim_tier")
    if claim_tier != "DESCRIPTIVE_ONLY_NO_PNL":
        raise RealDataError("Deep03 L2 audit claim tier is not no-PnL")

    releases = data_quality.get("releases")
    audit_release_ids = l2_audit.get("input_release_ids")
    if not isinstance(releases, list) or not isinstance(
        audit_release_ids, list
    ):
        raise RealDataError("Deep03 release binding is malformed")
    release_ids: list[str] = []
    quality_by_date: dict[str, tuple[str, str]] = {}
    receipt_dates: list[str] = []
    for raw in releases:
        if not isinstance(raw, Mapping):
            raise RealDataError("Deep03 data-quality release is malformed")
        date = raw.get("date")
        release_id = raw.get("release_id")
        if not isinstance(date, str) or not isinstance(release_id, str):
            raise RealDataError("Deep03 release identity is malformed")
        release_ids.append(release_id)
        summary = raw.get("l2_quality_summary")
        if isinstance(summary, Mapping) and summary:
            quality_objects = raw.get("quality_objects")
            if not isinstance(quality_objects, list):
                raise RealDataError("Deep03 quality_objects is malformed")
            matches = [
                row
                for row in quality_objects
                if isinstance(row, Mapping)
                and row.get("logical_key")
                == f"control/quality/v1/date={date}/l2_gaps.json"
            ]
            if len(matches) != 1:
                raise RealDataError(
                    f"Deep03 release has no unique L2 receipt: {date}"
                )
            receipt = matches[0]
            sha = receipt.get("sha256")
            version = receipt.get("source_version_id")
            if not isinstance(sha, str) or not isinstance(version, str):
                raise RealDataError("Deep03 L2 receipt identity is malformed")
            quality_by_date[date] = (sha, version)
            receipt_dates.append(date)
    if release_ids != list(audit_release_ids):
        raise RealDataError("Deep03 audit release list contradicts DQ receipt")

    audit_quality = l2_audit.get("l2_quality_objects")
    if not isinstance(audit_quality, list):
        raise RealDataError("Deep03 audit L2 quality list is malformed")
    audit_by_date: dict[str, tuple[str, str]] = {}
    for raw in audit_quality:
        if not isinstance(raw, Mapping):
            raise RealDataError("Deep03 audit L2 quality row is malformed")
        date = raw.get("date")
        sha = raw.get("sha256")
        version = raw.get("source_version_id")
        if not all(isinstance(value, str) for value in (date, sha, version)):
            raise RealDataError("Deep03 audit quality identity is malformed")
        if date in audit_by_date:
            raise RealDataError("Deep03 audit has a duplicate L2 quality date")
        audit_by_date[str(date)] = (str(sha), str(version))
    if audit_by_date != quality_by_date:
        raise RealDataError(
            "Deep03 independent audit does not bind every DQ L2 receipt"
        )
    return tuple(sorted(receipt_dates)), str(claim_tier)


_A11_DIRECT_REQUIRED = frozenset(
    {
        "campaign_id",
        "root_event_id",
        "market_ticker",
        "sport",
        "decision_ts_ns",
        "features_asof_ns",
        "book_observed_at_ns",
        "tick_size_e4",
        "scheduled_start_ts_ns",
        "scheduled_start_asof_ns",
        "best_yes_bid_e4",
        "best_yes_ask_e4",
        "state_started_at_ns",
        "reference_mid_onset_e4",
        "reference_mid_onset_observed_at_ns",
        "surviving_side_onset_e4",
        "update_count_60s",
        "trade_count_300s",
        "activity_burst",
        "book_valid",
        "lifecycle_open",
        "gap",
        "paused",
        "locked",
        "crossed",
    }
)


def load_a11_normalized_rows(
    campaigns_path: Path,
    expected_sha256: str,
) -> tuple[tuple[NormalizedStateRow, ...], tuple[str, ...]]:
    """Directly map an enriched campaign table, or return missing-field blocks.

    No field is inferred.  In particular ``event_proxy`` is never upgraded to
    ``root_event_id``, and C1 scenario clocks are never called real latency.
    """

    path = _safe_explicit_file(campaigns_path, expected_sha256)
    connection = _duckdb().connect(database=":memory:")
    try:
        columns = _columns(connection, path)
        missing = sorted(_A11_DIRECT_REQUIRED - set(columns))
        if missing:
            return (), tuple(
                f"BLOCK_A11_MISSING_DIRECT_FIELD:{name}" for name in missing
            )
        selected = sorted(_A11_DIRECT_REQUIRED)
        quoted = ",".join(f'"{name}"' for name in selected)
        raw_rows = connection.execute(
            f"SELECT {quoted} FROM read_parquet(?) "
            "ORDER BY decision_ts_ns,campaign_id",
            [str(path)],
        ).fetchall()
    finally:
        connection.close()

    result: list[NormalizedStateRow] = []
    for values in raw_rows:
        raw = dict(zip(selected, values))
        campaign_id = raw.pop("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise RealDataError("campaign_id cannot bind a normalized row_id")
        try:
            result.append(
                NormalizedStateRow(row_id=campaign_id, **raw)
            )
        except (AdapterContractError, TypeError, ValueError) as exc:
            raise RealDataError(
                f"campaign {campaign_id!r} violates NormalizedStateRow"
            ) from exc
    if len({row.row_id for row in result}) != len(result):
        raise RealDataError("duplicate normalized A11 row_id")
    return tuple(result), ()


def _status(
    *,
    experiment_id: str,
    source_candidate_rows: int,
    source_candidate_opportunities: int,
    normalized_state_rows: int,
    strict_fill_rows: int,
    gross_markout_rows: int,
    blockers: Iterable[str],
    allowed_claims: Iterable[str],
) -> ExperimentDataStatus:
    blocker_codes = tuple(sorted(set(blockers)))
    state = "INPUT_ROWS_READY" if not blocker_codes else "BLOCKED_SOURCE_FIELDS"
    return ExperimentDataStatus(
        experiment_id=experiment_id,
        state=state,
        source_candidate_rows=source_candidate_rows,
        source_candidate_opportunities=source_candidate_opportunities,
        normalized_state_rows=normalized_state_rows,
        strict_fill_rows=strict_fill_rows,
        gross_markout_rows=gross_markout_rows,
        exact_exit_rows=0,
        settled_rows=0,
        net_pnl_rows=0,
        blocker_codes=blocker_codes,
        allowed_claims=tuple(sorted(set(allowed_claims))),
    )


def audit_real_data(
    paths: RealInputPaths,
    pins: RealInputPins,
) -> RealDataAudit:
    """Audit the exact C1/Deep03 inputs and expose only honest row readiness."""

    checked = {
        name: _safe_explicit_file(getattr(paths, name), getattr(pins, name))
        for name in asdict(paths)
    }
    ledger = _read_json(checked["analysis_ledger"])
    data_quality = _read_json(checked["deep03_data_quality"])
    l2_audit = _read_json(checked["deep03_l2_audit"])
    ledger_bindings = _ledger_bindings(ledger)
    for name, relative in (
        ("campaigns", "TABLES/CAMPAIGNS.parquet"),
        ("fill_slices", "TABLES/FILL_SLICES.parquet"),
        ("markouts", "TABLES/MARKOUTS.parquet"),
    ):
        _validate_ledger_table(
            path=checked[name],
            expected_sha256=getattr(pins, name),
            relative=relative,
            ledger_bindings=ledger_bindings,
        )
    l2_receipt_dates, claim_tier = _validate_deep03(
        data_quality, l2_audit
    )

    connection = _duckdb().connect(database=":memory:")
    try:
        campaign_columns = _columns(connection, checked["campaigns"])
        fill_columns = _columns(connection, checked["fill_slices"])
        markout_columns = _columns(connection, checked["markouts"])
        _require_columns(
            "CAMPAIGNS",
            campaign_columns,
            {
                "campaign_id",
                "episode_id",
                "date",
                "market_ticker",
                "event_proxy",
                "sport",
                "side",
                "quote_price_e4",
                "depletion_ns",
                "latency_id",
                "activation_ns",
                "cancel_effective_ns",
                "state_ns",
            },
        )
        _require_columns(
            "FILL_SLICES",
            fill_columns,
            {
                "fill_slice_id",
                "campaign_id",
                "date",
                "sport",
                "track",
                "fill_count_e4",
            },
        )
        _require_columns(
            "MARKOUTS",
            markout_columns,
            {
                "fill_slice_id",
                "date",
                "sport",
                "track",
                "markout_status",
                "observed_count_e4",
                "censored_count_e4",
                "gross_e4",
            },
        )
        campaign_count = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?)",
            checked["campaigns"],
        )
        fill_count = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?)",
            checked["fill_slices"],
        )
        markout_count = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?)",
            checked["markouts"],
        )
        campaign_dates = _dates(connection, checked["campaigns"])
        fill_dates = _dates(connection, checked["fill_slices"])
        markout_dates = _dates(connection, checked["markouts"])
        if campaign_dates != fill_dates or campaign_dates != markout_dates:
            raise RealDataError("C1 table date sets contradict each other")
        if not set(campaign_dates).issubset(l2_receipt_dates):
            raise RealDataError("C1 artifacts escape audited L2 receipt dates")

        # Counts describe candidate evidence, not trigger truth.
        a11_candidate_rows, a11_opportunities = connection.execute(
            "SELECT count(*)::BIGINT,"
            "count(DISTINCT episode_id)::BIGINT "
            "FROM read_parquet(?) "
            "WHERE sport IN ('Tennis','Basketball')",
            [str(checked["campaigns"])],
        ).fetchone()
        a11_strict_fills = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?) "
            "WHERE sport IN ('Tennis','Basketball') "
            "AND track='STRICT_THROUGH'",
            checked["fill_slices"],
        )
        a11_gross_marks = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?) "
            "WHERE sport IN ('Tennis','Basketball') "
            "AND track='STRICT_THROUGH'",
            checked["markouts"],
        )
        all_strict_fills = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?) "
            "WHERE track='STRICT_THROUGH'",
            checked["fill_slices"],
        )
        all_strict_marks = _scalar_int(
            connection,
            "SELECT count(*)::BIGINT FROM read_parquet(?) "
            "WHERE track='STRICT_THROUGH'",
            checked["markouts"],
        )
    finally:
        connection.close()

    normalized_rows, direct_blocks = load_a11_normalized_rows(
        checked["campaigns"], pins.campaigns
    )
    common_claims = (
        "SOURCE_ROW_INVENTORY",
        "HASH_PINNED_ENGINEERING_EVIDENCE",
        "GROSS_MARKOUT_DIAGNOSTICS_NOT_NET_PNL",
    )
    common_blocks = {
        "BLOCK_C1_SCENARIO_LATENCY_IS_NOT_MEASURED_LATENCY",
        "BLOCK_C1_MARKOUT_IS_NOT_EXACT_L2_EXIT",
        "BLOCK_MISSING_SETTLEMENT_TRUTH",
        "BLOCK_C1_FEE_RECEIPT_FAILS_CLOSED",
    }
    a01 = _status(
        experiment_id="A01-SPREAD-CAPTURE",
        # Raw rows are inventoried for coverage.  normalized_state_rows stays
        # zero because C1 depletion is not an A01 spread-dwell opportunity.
        source_candidate_rows=campaign_count,
        source_candidate_opportunities=0,
        normalized_state_rows=0,
        strict_fill_rows=all_strict_fills,
        gross_markout_rows=all_strict_marks,
        blockers=common_blocks
        | {
            "BLOCK_A01_C1_DEPLETION_ROWS_ARE_NOT_SPREAD_DWELL_ROWS",
            "BLOCK_A01_MISSING_EXACT_BID_ASK_DWELL_WARMUP",
            "BLOCK_A01_MISSING_TRAIN_GATE_ARTIFACTS",
            "BLOCK_MISSING_SCHEDULED_START_BINDING",
        },
        allowed_claims=common_claims,
    )
    a11_blocks = set(common_blocks) | set(direct_blocks)
    if direct_blocks:
        a11_blocks |= {
            "BLOCK_A11_DEPLETION_IS_NOT_30S_ONE_SIDED_PERSISTENCE",
            "BLOCK_A11_EVENT_PROXY_IS_NOT_ROOT_EVENT_ID",
            "BLOCK_A11_MISSING_ONSET_REFERENCE_AND_SURVIVOR_STATE",
            "BLOCK_MISSING_SCHEDULED_START_BINDING",
        }
    a11 = _status(
        experiment_id="A11-ONE-SIDED-PROVISION",
        source_candidate_rows=int(a11_candidate_rows),
        source_candidate_opportunities=int(a11_opportunities),
        normalized_state_rows=len(normalized_rows),
        strict_fill_rows=a11_strict_fills,
        gross_markout_rows=a11_gross_marks,
        blockers=a11_blocks,
        allowed_claims=common_claims
        + ("STRICT_THROUGH_PUBLIC_FILL_ENGINEERING_EVIDENCE",),
    )
    b09 = _status(
        experiment_id="B09-LISTING-TO-START-DRIFT",
        source_candidate_rows=0,
        source_candidate_opportunities=0,
        normalized_state_rows=0,
        strict_fill_rows=0,
        gross_markout_rows=0,
        blockers=common_blocks
        | {
            "BLOCK_B09_TRAIN_ARTIFACT_UNBOUND",
            "BLOCK_B09_DIRECTION_CELL_HORIZON_SHA_UNBOUND",
            "BLOCK_B09_NO_LISTING_AGE_OR_SCHEDULED_PHASE_ROWS",
            "BLOCK_MISSING_SCHEDULED_START_BINDING",
        },
        allowed_claims=("SOURCE_ROW_INVENTORY",),
    )

    inventory = (
        SourceInventoryRow(
            source_id="C1_ANALYSIS_ARTIFACT_LEDGER",
            path=str(checked["analysis_ledger"]),
            sha256=pins.analysis_ledger,
            size_bytes=checked["analysis_ledger"].stat().st_size,
            row_count=None,
            dates_utc=campaign_dates,
            classification="PROVENANCE_LEDGER",
        ),
        SourceInventoryRow(
            source_id="C1_CAMPAIGNS",
            path=str(checked["campaigns"]),
            sha256=pins.campaigns,
            size_bytes=checked["campaigns"].stat().st_size,
            row_count=campaign_count,
            dates_utc=campaign_dates,
            classification="SCENARIO_CAMPAIGNS_ENGINEERING_ONLY",
            columns=campaign_columns,
        ),
        SourceInventoryRow(
            source_id="C1_FILL_SLICES",
            path=str(checked["fill_slices"]),
            sha256=pins.fill_slices,
            size_bytes=checked["fill_slices"].stat().st_size,
            row_count=fill_count,
            dates_utc=fill_dates,
            classification="PUBLIC_FILL_ENGINEERING_ONLY",
            columns=fill_columns,
        ),
        SourceInventoryRow(
            source_id="C1_MARKOUTS",
            path=str(checked["markouts"]),
            sha256=pins.markouts,
            size_bytes=checked["markouts"].stat().st_size,
            row_count=markout_count,
            dates_utc=markout_dates,
            classification="GROSS_MARKOUT_NOT_NET_PNL",
            columns=markout_columns,
        ),
        SourceInventoryRow(
            source_id="DEEP03_DATA_QUALITY",
            path=str(checked["deep03_data_quality"]),
            sha256=pins.deep03_data_quality,
            size_bytes=checked["deep03_data_quality"].stat().st_size,
            row_count=None,
            dates_utc=l2_receipt_dates,
            classification="QUALITY_RECEIPT_NO_PNL",
        ),
        SourceInventoryRow(
            source_id="DEEP03_L2_INDEPENDENT_AUDIT",
            path=str(checked["deep03_l2_audit"]),
            sha256=pins.deep03_l2_audit,
            size_bytes=checked["deep03_l2_audit"].stat().st_size,
            row_count=None,
            dates_utc=l2_receipt_dates,
            classification="DESCRIPTIVE_AUDIT_NO_PNL",
        ),
    )
    return RealDataAudit(
        schema_version=SCHEMA_VERSION,
        source_inventory=inventory,
        artifact_dates_utc=campaign_dates,
        l2_receipt_dates_utc=l2_receipt_dates,
        deep03_claim_tier=claim_tier,
        normalized_rows=normalized_rows,
        experiments=(a01, a11, b09),
        classification="ENGINEERING_INPUT_ONLY_NOT_NET_PNL",
    )


def evidence_payload(audit: RealDataAudit) -> dict[str, Any]:
    """Return a self-identifying receipt without changing the audit hash."""

    payload = audit.to_dict()
    payload["audit_sha256"] = audit.sha256
    payload["experiment_status_sha256"] = {
        row.experiment_id: canonical_sha256(asdict(row))
        for row in audit.experiments
    }
    payload["source_binding_sha256"] = canonical_sha256(
        [
            {
                "source_id": row.source_id,
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
            }
            for row in audit.source_inventory
        ]
    )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit six explicit, hash-pinned C1/Deep03 files and write a "
            "canonical machine-readable engineering-evidence receipt."
        )
    )
    parser.add_argument("--analysis-ledger", required=True, type=Path)
    parser.add_argument("--campaigns", required=True, type=Path)
    parser.add_argument("--fill-slices", required=True, type=Path)
    parser.add_argument("--markouts", required=True, type=Path)
    parser.add_argument("--deep03-data-quality", required=True, type=Path)
    parser.add_argument("--deep03-l2-audit", required=True, type=Path)
    parser.add_argument(
        "--known-20260722-pins",
        "--known-pins",
        action="store_true",
        dest="known_pins",
        help="use the inspected 2026-07-22 artifact SHA set",
    )
    parser.add_argument("--analysis-ledger-sha256")
    parser.add_argument("--campaigns-sha256")
    parser.add_argument("--fill-slices-sha256")
    parser.add_argument("--markouts-sha256")
    parser.add_argument("--deep03-data-quality-sha256")
    parser.add_argument("--deep03-l2-audit-sha256")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _pins_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> RealInputPins:
    explicit = {
        "analysis_ledger": args.analysis_ledger_sha256,
        "campaigns": args.campaigns_sha256,
        "fill_slices": args.fill_slices_sha256,
        "markouts": args.markouts_sha256,
        "deep03_data_quality": args.deep03_data_quality_sha256,
        "deep03_l2_audit": args.deep03_l2_audit_sha256,
    }
    supplied = [name for name, value in explicit.items() if value is not None]
    if args.known_pins:
        if supplied:
            parser.error(
                "--known-20260722-pins cannot be mixed with explicit SHA pins"
            )
        return RealInputPins.known_20260722()
    if len(supplied) != len(explicit):
        missing = sorted(set(explicit) - set(supplied))
        parser.error(
            "use --known-20260722-pins or provide every explicit SHA pin; "
            f"missing: {','.join(missing)}"
        )
    try:
        return RealInputPins(**explicit)
    except RealDataError as exc:
        parser.error(str(exc))
        raise AssertionError("argparse.error must terminate") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    pins = _pins_from_args(parser, args)
    paths = RealInputPaths(
        analysis_ledger=args.analysis_ledger,
        campaigns=args.campaigns,
        fill_slices=args.fill_slices,
        markouts=args.markouts,
        deep03_data_quality=args.deep03_data_quality,
        deep03_l2_audit=args.deep03_l2_audit,
    )
    try:
        result = audit_real_data(paths, pins)
        receipt_sha256 = atomic_write_receipt(
            args.output, evidence_payload(result)
        )
    except RealDataError as exc:
        parser.error(str(exc))
        raise AssertionError("argparse.error must terminate") from exc
    print(f"audit_sha256={result.sha256}")
    print(f"receipt_sha256={receipt_sha256}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
