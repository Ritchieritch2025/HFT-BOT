#!/usr/bin/env python3
"""Publish the audited C1 real-fill result as charts, HTML, and PDF.

The publisher is deliberately economics-conservative: it accepts only the
canonical aggregate schema emitted by ``c1_real_fill_runner.py`` and never
renames a gross markout as net profit.  Chart source CSVs are emitted next to
the images so every visual number can be independently checked.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import math
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:
    from tools.research.c1_real_fill_runner import _pilot_verdict
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from c1_real_fill_runner import _pilot_verdict


SCHEMA_VERSION = "c1-real-fill-aggregates-v1"
TRACK_ORDER = (
    "STRICT_THROUGH",
    "QUEUE_PESSIMISTIC",
    "OPTIMISTIC_AT_TOUCH",
)
TRACK_LABELS = {
    "STRICT_THROUGH": "Strict-through lower bound",
    "QUEUE_PESSIMISTIC": "Queue-pessimistic diagnostic",
    "OPTIMISTIC_AT_TOUCH": "Optimistic at-touch upper bound",
}
TRACK_COLORS = {
    "STRICT_THROUGH": "#16324F",
    "QUEUE_PESSIMISTIC": "#2A9D8F",
    "OPTIMISTIC_AT_TOUCH": "#E9C46A",
}
DATE_LABEL = {
    "2026-07-12": "Jul 12",
    "2026-07-15": "Jul 15",
    "2026-07-17": "Jul 17",
}
ELIGIBLE_DATES = tuple(DATE_LABEL)
LATENCY_ORDER = ("FAST", "PRIMARY", "STRESS")
CANCEL_TIMERS = (200_000, 300_000)
MARKOUT_HORIZONS = (100_000, 200_000, 500_000, 1_000_000)

FILL_RATE_FIELDS = {
    "date", "latency_id", "cancel_timer_us", "track",
    "campaigns_eligible", "filled_orders", "fill_slices", "filled_count_e4",
    "campaigns_accepted", "offered_count_e4", "fill_rate_order_num",
    "fill_rate_order_den", "fill_rate_qty_num_e4", "fill_rate_qty_den_e4",
    "fill_rate_order", "fill_rate_quantity", "denominator_scope",
    "queue_band_cohort_id", "queue_band_fill_rate_order_num",
    "queue_band_fill_rate_order_den", "queue_band_fill_rate_qty_num_e4",
    "queue_band_fill_rate_qty_den_e4",
}
MARKOUT_FIELDS = {
    "date", "latency_id", "cancel_timer_us", "track", "horizon_us",
    "total_slices", "observed_slices", "censored_slices",
    "total_filled_count_e4", "observed_count_e4", "censored_count_e4",
    "weighted_gross_sum_e8", "weighted_mid_twice_sum_e8",
    "weighted_censored_entry_price_sum_e8",
    "weighted_gross_lower_sum_e8", "weighted_gross_upper_sum_e8",
    "coverage_count_num_e4", "coverage_count_den_e4",
    "coverage_fraction_diagnostic", "mean_gross_e4", "mean_mid_e4",
}
ATTRIBUTION_FIELDS = {
    "date", "latency_id", "cancel_timer_us", "track", "refill_group",
    "horizon_us", "total_slices", "observed_slices", "censored_slices",
    "total_filled_count_e4", "observed_count_e4", "censored_count_e4",
    "weighted_gross_sum_e8", "weighted_gross_lower_sum_e8",
    "weighted_gross_upper_sum_e8", "weighted_censored_entry_price_sum_e8",
    "coverage_count_num_e4",
    "coverage_count_den_e4", "mean_gross_e4",
}


class C1ReportError(RuntimeError):
    """The aggregate bundle cannot support an honest report."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise C1ReportError(f"cannot load required JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise C1ReportError(f"expected object in {path}")
    return value


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_canonical_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("ascii")
    with path.open("xb") as handle:
        handle.write(payload)


def _validate_analysis_ledger(
    input_dir: Path, complete: Mapping[str, Any]
) -> Path:
    """Verify every analysis artifact byte and the minimum complete run set."""
    ledger_path = input_dir / "ANALYSIS_ARTIFACT_SHA256.json"
    expected_sha = complete.get("analysis_artifact_ledger_sha256")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
        or not ledger_path.is_file()
        or ledger_path.is_symlink()
        or _sha256_path(ledger_path) != expected_sha
    ):
        raise C1ReportError("analysis artifact ledger is missing or not completion-bound")
    ledger = _load_json(ledger_path)
    if set(ledger) != {"schema_version", "artifacts"} or ledger.get(
        "schema_version"
    ) != "c1-analysis-artifact-ledger-v1" or not isinstance(
        ledger.get("artifacts"), list
    ):
        raise C1ReportError("analysis artifact ledger schema differs")
    input_root = input_dir.resolve(strict=True)
    actual_paths: set[str] = set()
    for index, row in enumerate(ledger["artifacts"]):
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256", "bytes"}:
            raise C1ReportError(f"invalid analysis ledger artifact[{index}]")
        logical = row["path"]
        digest = row["sha256"]
        size = row["bytes"]
        if not isinstance(logical, str):
            raise C1ReportError("analysis ledger path is not text")
        relative = PurePosixPath(logical)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != logical
            or logical in actual_paths
            or logical in {"ANALYSIS_ARTIFACT_SHA256.json", "C1_RUN_COMPLETE.json"}
        ):
            raise C1ReportError("analysis ledger contains an unsafe or duplicate path")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size) is not int
            or size <= 0
        ):
            raise C1ReportError("analysis ledger hash/size is malformed")
        candidate = input_dir
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise C1ReportError("analysis artifact path traverses a symlink")
        try:
            candidate.resolve(strict=True).relative_to(input_root)
        except (OSError, ValueError) as exc:
            raise C1ReportError("analysis artifact escapes the input directory") from exc
        if (
            not candidate.is_file()
            or candidate.stat().st_size != size
            or _sha256_path(candidate) != digest
        ):
            raise C1ReportError(f"analysis artifact byte mismatch: {logical}")
        actual_paths.add(logical)

    required = {
        "C1_AGGREGATES.json", "INPUT_RECEIPT.json", "SPEC_RECEIPT.json",
        "CLOCK_RECEIPT.json", "QUEUE_RECEIPT.json", "FEE_RECEIPT.json",
        "EXCLUSION_WATERFALL.json", "RESOURCE_RECEIPT.json",
        "REPRODUCTION_RECEIPT.json", "TABLES/CAMPAIGNS.parquet",
        "TABLES/FILL_SLICES.parquet", "TABLES/MARKOUTS.parquet",
        "TABLES/FILL_RATES.csv", "TABLES/MARKOUTS.csv",
        "TABLES/ATTRIBUTION.csv", "TABLES/CONCENTRATION.csv",
        "TABLES/SUPPORT.csv", "TABLES/EXCLUSIONS.csv",
    }
    for date in ELIGIBLE_DATES:
        for bucket in range(16):
            prefix = f"PARTITIONS/date={date}_bucket={bucket:02d}"
            required.update({
                f"{prefix}/CAMPAIGNS.parquet",
                f"{prefix}/FILL_SLICES.parquet",
                f"{prefix}/MARKOUTS.parquet",
                f"{prefix}/EXCLUSIONS.csv",
                f"{prefix}/PARTITION_RECEIPT.json",
            })
    missing = required - actual_paths
    if missing:
        raise C1ReportError(
            "analysis ledger omits required results: " + ",".join(sorted(missing)[:5])
        )
    return ledger_path


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise C1ReportError(f"{label} must be an integer")
    return value


def _ratio(num: object, den: object) -> float:
    numerator = _plain_int(num, "ratio numerator")
    denominator = _plain_int(den, "ratio denominator")
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise C1ReportError("invalid non-negative bounded ratio")
    return numerator / denominator if denominator else math.nan


def _exact_fields(row: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(row) != expected:
        raise C1ReportError(
            f"{label} fields differ; missing={sorted(expected-set(row))} "
            f"extra={sorted(set(row)-expected)}"
        )


def _optional_ratio(value: object, numerator: int, denominator: int, label: str) -> None:
    expected = numerator / denominator if denominator else None
    if expected is None:
        if value is not None:
            raise C1ReportError(f"{label} must be null when denominator is zero")
    elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isclose(
        float(value), expected, rel_tol=0.0, abs_tol=1e-12
    ):
        raise C1ReportError(f"{label} differs from its exact ratio")


def validate_aggregates(data: Mapping[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise C1ReportError(
            f"aggregate schema must be {SCHEMA_VERSION!r}, got "
            f"{data.get('schema_version')!r}"
        )
    if data.get("claim_tier") != "EXPLORATORY_NON_GATE":
        raise C1ReportError("claim_tier must remain EXPLORATORY_NON_GATE")
    if data.get("fee_state") != "NOT_ESTIMABLE_FAIL_CLOSED":
        raise C1ReportError("fee_state must fail closed")
    summary = data.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "eligible_dates", "work_partitions", "headline_track", "headline_latency",
        "gross_markout_only", "markout_quantity_policy",
        "censored_price_bound_e4", "net_pnl_estimated", "live_order_writes",
    }:
        raise C1ReportError("summary fields differ from the completed C1 run ABI")
    if (
        summary.get("eligible_dates") != list(ELIGIBLE_DATES)
        or summary.get("work_partitions") != 48
        or summary.get("headline_track") != "STRICT_THROUGH"
        or summary.get("headline_latency") != "PRIMARY"
        or summary.get("gross_markout_only") is not True
        or summary.get("markout_quantity_policy")
        != "PARTIAL_SAME_OUTCOME_TOP_DEPTH"
        or summary.get("censored_price_bound_e4") != [0, 10_000]
        or summary.get("net_pnl_estimated") is not False
        or summary.get("live_order_writes") != 0
    ):
        raise C1ReportError("summary attempts to widen or alter the frozen claim")
    for name in (
        "fill_rates", "markouts", "attribution", "concentration", "exclusions"
    ):
        if not isinstance(data.get(name), list):
            raise C1ReportError(f"{name} must be an array")
    expected_fill_keys = {
        (date, latency, timer, track)
        for date in ELIGIBLE_DATES for latency in LATENCY_ORDER
        for timer in CANCEL_TIMERS for track in TRACK_ORDER
    }
    fill_keys: set[tuple[object, ...]] = set()
    fill_by_key: dict[tuple[object, ...], Mapping[str, Any]] = {}
    for index, row in enumerate(data["fill_rates"]):
        if not isinstance(row, Mapping):
            raise C1ReportError(f"invalid fill_rates[{index}]")
        _exact_fields(row, FILL_RATE_FIELDS, f"fill_rates[{index}]")
        key = (row["date"], row["latency_id"], row["cancel_timer_us"], row["track"])
        if key in fill_keys:
            raise C1ReportError(f"duplicate fill-rate cell: {key}")
        fill_keys.add(key)
        fill_by_key[key] = row
        integer_fields = (
            "campaigns_eligible", "filled_orders", "fill_slices",
            "filled_count_e4", "campaigns_accepted", "offered_count_e4",
            "fill_rate_order_num", "fill_rate_order_den",
            "fill_rate_qty_num_e4", "fill_rate_qty_den_e4",
            "queue_band_fill_rate_order_num", "queue_band_fill_rate_order_den",
            "queue_band_fill_rate_qty_num_e4", "queue_band_fill_rate_qty_den_e4",
        )
        values = {field: _plain_int(row[field], field) for field in integer_fields}
        if min(values.values()) < 0:
            raise C1ReportError("fill-rate integer ledger contains a negative value")
        if (
            values["campaigns_eligible"] > values["campaigns_accepted"]
            or values["filled_orders"] > values["campaigns_eligible"]
            or values["fill_slices"] < values["filled_orders"]
            or (values["filled_orders"] == 0) != (values["fill_slices"] == 0)
            or (values["filled_orders"] == 0) != (values["filled_count_e4"] == 0)
            or values["offered_count_e4"] != values["campaigns_eligible"] * 10_000
            or values["filled_count_e4"] > values["offered_count_e4"]
            or values["fill_rate_order_num"] != values["filled_orders"]
            or values["fill_rate_order_den"] != values["campaigns_eligible"]
            or values["fill_rate_qty_num_e4"] != values["filled_count_e4"]
            or values["fill_rate_qty_den_e4"] != values["offered_count_e4"]
            or values["queue_band_fill_rate_qty_den_e4"]
            != values["queue_band_fill_rate_order_den"] * 10_000
        ):
            raise C1ReportError("fill-rate duplicated fields do not conserve")
        order_ratio = _ratio(row["fill_rate_order_num"], row["fill_rate_order_den"])
        qty_ratio = _ratio(row["fill_rate_qty_num_e4"], row["fill_rate_qty_den_e4"])
        _optional_ratio(row["fill_rate_order"], int(row["fill_rate_order_num"]), int(row["fill_rate_order_den"]), "fill_rate_order")
        _optional_ratio(row["fill_rate_quantity"], int(row["fill_rate_qty_num_e4"]), int(row["fill_rate_qty_den_e4"]), "fill_rate_quantity")
        if math.isnan(order_ratio) != (row["fill_rate_order"] is None) or math.isnan(qty_ratio) != (row["fill_rate_quantity"] is None):
            raise C1ReportError("fill-rate nullability differs from denominator")
        _ratio(row["queue_band_fill_rate_order_num"], row["queue_band_fill_rate_order_den"])
        _ratio(row["queue_band_fill_rate_qty_num_e4"], row["queue_band_fill_rate_qty_den_e4"])
        if row["queue_band_cohort_id"] != "QUEUE_RECONSTRUCTABLE_ACTIVATIONS":
            raise C1ReportError("queue-band cohort is not the frozen common cohort")
        expected_scope = (
            "QUEUE_RECONSTRUCTABLE_ACTIVATIONS"
            if row["track"] == "QUEUE_PESSIMISTIC" else "ALL_ACTIVATION_ELIGIBLE"
        )
        if row["denominator_scope"] != expected_scope:
            raise C1ReportError("fill-rate denominator scope is mislabeled")
    if fill_keys != expected_fill_keys:
        raise C1ReportError("fill-rate grid is not exact 3x3x2x3")
    for date in ELIGIBLE_DATES:
        for latency in LATENCY_ORDER:
            for timer in CANCEL_TIMERS:
                rows = [
                    fill_by_key[(date, latency, timer, track)]
                    for track in TRACK_ORDER
                ]
                order_denominators = {
                    int(row["queue_band_fill_rate_order_den"]) for row in rows
                }
                quantity_denominators = {
                    int(row["queue_band_fill_rate_qty_den_e4"]) for row in rows
                }
                order_numerators = [
                    int(row["queue_band_fill_rate_order_num"]) for row in rows
                ]
                quantity_numerators = [
                    int(row["queue_band_fill_rate_qty_num_e4"]) for row in rows
                ]
                if (
                    len(order_denominators) != 1
                    or len(quantity_denominators) != 1
                    or order_numerators != sorted(order_numerators)
                    or quantity_numerators != sorted(quantity_numerators)
                ):
                    raise C1ReportError(
                        "queue uncertainty band is not a common monotone cohort"
                    )

    expected_markout_keys = {
        (date, latency, timer, track, horizon)
        for date in ELIGIBLE_DATES for latency in LATENCY_ORDER
        for timer in CANCEL_TIMERS for track in TRACK_ORDER
        for horizon in MARKOUT_HORIZONS
    }
    markout_keys: set[tuple[object, ...]] = set()
    markout_by_key: dict[tuple[object, ...], Mapping[str, Any]] = {}
    for index, row in enumerate(data["markouts"]):
        if not isinstance(row, Mapping):
            raise C1ReportError(f"invalid markouts[{index}]")
        _exact_fields(row, MARKOUT_FIELDS, f"markouts[{index}]")
        key = (row["date"], row["latency_id"], row["cancel_timer_us"], row["track"], row["horizon_us"])
        if key in markout_keys:
            raise C1ReportError(f"duplicate markout cell: {key}")
        markout_keys.add(key)
        markout_by_key[key] = row
        total_slices = _plain_int(row["total_slices"], "total_slices")
        observed_slices = _plain_int(row["observed_slices"], "observed_slices")
        censored_slices = _plain_int(row["censored_slices"], "censored_slices")
        total = _plain_int(row["total_filled_count_e4"], "total_filled_count_e4")
        observed = _plain_int(row["observed_count_e4"], "observed_count_e4")
        censored = _plain_int(row["censored_count_e4"], "censored_count_e4")
        if (
            min(total_slices, observed_slices, censored_slices, total, observed, censored) < 0
            or total != observed + censored
            or (total == 0) != (total_slices == 0)
            or (observed == 0) != (observed_slices == 0)
            or (censored == 0) != (censored_slices == 0)
            or max(observed_slices, censored_slices) > total_slices
            or total_slices > observed_slices + censored_slices
        ):
            raise C1ReportError("markout observed/censored quantity does not conserve")
        fill = fill_by_key[key[:4]]
        if (
            total_slices != int(fill["fill_slices"])
            or total != int(fill["filled_count_e4"])
        ):
            raise C1ReportError("markout grid does not conserve the fill-rate ledger")
        if row["coverage_count_num_e4"] != observed or row["coverage_count_den_e4"] != total:
            raise C1ReportError("markout coverage fields differ from quantity ledger")
        _optional_ratio(row["coverage_fraction_diagnostic"], observed, total, "coverage_fraction_diagnostic")
        point = _plain_int(row["weighted_gross_sum_e8"], "weighted_gross_sum_e8")
        mid = _plain_int(row["weighted_mid_twice_sum_e8"], "weighted_mid_twice_sum_e8")
        entry = _plain_int(
            row["weighted_censored_entry_price_sum_e8"],
            "weighted_censored_entry_price_sum_e8",
        )
        lower = _plain_int(row["weighted_gross_lower_sum_e8"], "weighted_gross_lower_sum_e8")
        upper = _plain_int(row["weighted_gross_upper_sum_e8"], "weighted_gross_upper_sum_e8")
        if (
            not -10_000 * observed <= point <= 10_000 * observed
            or not -20_000 * observed <= mid <= 20_000 * observed
            or not 0 <= entry <= 10_000 * censored
            or lower != point - entry
            or upper != point + 10_000 * censored - entry
            or (observed == 0 and (point != 0 or mid != 0))
        ):
            raise C1ReportError("markout legal-price bounds are not mechanically exact")
        _optional_ratio(row["mean_gross_e4"], point, observed, "mean_gross_e4")
        expected_mid = mid / (2 * observed) if observed else None
        if expected_mid is None:
            if row["mean_mid_e4"] is not None:
                raise C1ReportError("mean_mid_e4 must be null without observed quantity")
        elif not isinstance(row["mean_mid_e4"], (int, float)) or not math.isclose(float(row["mean_mid_e4"]), expected_mid, rel_tol=0.0, abs_tol=1e-12):
            raise C1ReportError("mean_mid_e4 differs from weighted midpoint")
    if markout_keys != expected_markout_keys:
        raise C1ReportError("markout grid is not exact 3x3x2x3x4")

    attribution_keys: set[tuple[object, ...]] = set()
    attribution_sums: dict[tuple[object, ...], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for index, row in enumerate(data["attribution"]):
        if not isinstance(row, Mapping):
            raise C1ReportError(f"invalid attribution[{index}]")
        _exact_fields(row, ATTRIBUTION_FIELDS, f"attribution[{index}]")
        key = tuple(row[name] for name in ("date", "latency_id", "cancel_timer_us", "track", "refill_group", "horizon_us"))
        if key in attribution_keys:
            raise C1ReportError(f"duplicate attribution cell: {key}")
        attribution_keys.add(key)
        base_key = (
            row["date"], row["latency_id"], row["cancel_timer_us"],
            row["track"], row["horizon_us"],
        )
        if (
            base_key not in expected_markout_keys
            or row["refill_group"] not in {"REFILL_BY_TIMER", "NON_REFILL_OR_CENSORED"}
        ):
            raise C1ReportError("attribution cell is outside the frozen markout grid")
        total_slices = _plain_int(row["total_slices"], "attribution total_slices")
        observed_slices = _plain_int(row["observed_slices"], "attribution observed_slices")
        censored_slices = _plain_int(row["censored_slices"], "attribution censored_slices")
        total = _plain_int(row["total_filled_count_e4"], "attribution total")
        observed = _plain_int(row["observed_count_e4"], "attribution observed")
        censored = _plain_int(row["censored_count_e4"], "attribution censored")
        if (
            min(total_slices, observed_slices, censored_slices, total, observed, censored) < 0
            or total != observed + censored
            or (total == 0) != (total_slices == 0)
            or (observed == 0) != (observed_slices == 0)
            or (censored == 0) != (censored_slices == 0)
            or max(observed_slices, censored_slices) > total_slices
            or total_slices > observed_slices + censored_slices
        ):
            raise C1ReportError("attribution quantity does not conserve")
        if row["coverage_count_num_e4"] != observed or row["coverage_count_den_e4"] != total:
            raise C1ReportError("attribution coverage differs from quantities")
        point = _plain_int(row["weighted_gross_sum_e8"], "attribution point")
        entry = _plain_int(
            row["weighted_censored_entry_price_sum_e8"], "attribution entry"
        )
        lower = _plain_int(row["weighted_gross_lower_sum_e8"], "attribution lower")
        upper = _plain_int(row["weighted_gross_upper_sum_e8"], "attribution upper")
        if (
            not -10_000 * observed <= point <= 10_000 * observed
            or not 0 <= entry <= 10_000 * censored
            or lower != point - entry
            or upper != point + 10_000 * censored - entry
            or (observed == 0 and point != 0)
        ):
            raise C1ReportError("attribution legal-price bounds are not mechanically exact")
        _optional_ratio(row["mean_gross_e4"], point, observed, "attribution mean")
        for field in (
            "total_slices", "observed_slices", "censored_slices",
            "total_filled_count_e4", "observed_count_e4", "censored_count_e4",
            "weighted_gross_sum_e8", "weighted_censored_entry_price_sum_e8",
            "weighted_gross_lower_sum_e8", "weighted_gross_upper_sum_e8",
        ):
            attribution_sums[base_key][field] += int(row[field])
    for key, markout in markout_by_key.items():
        summed = attribution_sums[key]
        for field in (
            "total_slices", "observed_slices", "censored_slices",
            "total_filled_count_e4", "observed_count_e4", "censored_count_e4",
            "weighted_gross_sum_e8", "weighted_censored_entry_price_sum_e8",
            "weighted_gross_lower_sum_e8", "weighted_gross_upper_sum_e8",
        ):
            if summed[field] != int(markout[field]):
                raise C1ReportError("attribution does not sum back to markouts")

    concentration_dates: set[str] = set()
    for index, row in enumerate(data["concentration"]):
        if not isinstance(row, Mapping) or set(row) != {
            "date", "events", "markets", "max_market_share", "hhi"
        }:
            raise C1ReportError(f"invalid concentration[{index}]")
        date = str(row["date"])
        if date in concentration_dates:
            raise C1ReportError(f"duplicate concentration date: {date}")
        concentration_dates.add(date)
        events = _plain_int(row["events"], "concentration events")
        markets = _plain_int(row["markets"], "concentration markets")
        if min(events, markets) < 0:
            raise C1ReportError("concentration counts must be non-negative")
        for field in ("max_market_share", "hhi"):
            value = row[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
                raise C1ReportError(f"concentration {field} is outside [0,1]")
    if concentration_dates != set(ELIGIBLE_DATES):
        raise C1ReportError("concentration does not cover exactly three clean dates")

    for index, row in enumerate(data["exclusions"]):
        if not isinstance(row, Mapping) or not {"date", "reason", "count"}.issubset(row):
            raise C1ReportError(f"invalid exclusions[{index}]")
        if set(row) - {"date", "reason", "count", "ledger_status"}:
            raise C1ReportError(f"exclusions[{index}] contains an unknown field")
        if not isinstance(row["date"], str) or not isinstance(row["reason"], str) or not row["reason"]:
            raise C1ReportError("exclusion date/reason is malformed")
        if _plain_int(row["count"], "exclusion count") < 0:
            raise C1ReportError("exclusion count must be non-negative")

    queue = data.get("queue_invariants")
    if not isinstance(queue, Mapping):
        raise C1ReportError("queue invariants are absent")
    if (
        queue.get("duplicate_trade_allocation_count") != 0
        or queue.get("public_volume_exceeded_count") != 0
        or queue.get("negative_l2_delta_creates_fill") is not False
        or queue.get("strict_at_touch_creates_fill") is not False
        or queue.get("queue_band_comparison_cohort") != "QUEUE_RECONSTRUCTABLE_ACTIVATIONS"
    ):
        raise C1ReportError("queue/allocation invariants are not clean")
    global_invariants = queue.get("global_merged_output_invariants")
    if not isinstance(global_invariants, Mapping) or set(global_invariants) != {
        "duplicate_fill_slice_ids", "cross_partition_trade_allocation_failures",
        "cross_partition_public_volume_failures", "markout_fill_linkage_failures",
        "markout_identity_failures",
        "markout_quantity_conservation_failures",
    } or any(
        type(value) is not int or value != 0 for value in global_invariants.values()
    ):
        raise C1ReportError("global 48-partition invariants are not clean")

    clock = data.get("clock_qc")
    if not isinstance(clock, Mapping) or (
        clock.get("decision_clock") != "recv_wall_ns"
        or clock.get("trade_resolution") != "microsecond_ambiguity_against_strategy"
        or clock.get("past_only_asof") is not True
        or clock.get("state_ttl_us") != 250_000
    ):
        raise C1ReportError("clock receipt differs from the frozen semantics")
    if not isinstance(data.get("provenance"), Mapping):
        raise C1ReportError("provenance is absent")

    verdict = data.get("verdict")
    if not isinstance(verdict, Mapping) or set(verdict) != {
        "code", "precedence", "reason", "binding_track", "binding_latency",
        "decision_rule", "fees_estimated", "positive_promotion", "live_ready",
    }:
        raise C1ReportError("verdict fields differ from the frozen non-gate ABI")
    if (
        verdict.get("code") not in {
            "KILL_C1_ENTRY", "RETAIN_FOR_20_DAY_VALIDATION",
            "INDETERMINATE_MORE_CLEAN_DAYS",
        }
        or verdict.get("fees_estimated") is not False
        or verdict.get("positive_promotion") is not False
        or verdict.get("live_ready") is not False
    ):
        raise C1ReportError("verdict attempts to authorize or estimate unsupported claims")
    recomputed = _pilot_verdict(data["fill_rates"], data["markouts"], ELIGIBLE_DATES)
    if dict(verdict) != recomputed:
        raise C1ReportError("published verdict differs from deterministic recomputation")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({str(key) for row in rows for key in row})
    if not fields:
        fields = ["state"]
        rows = [{"state": "NO_ROWS"}]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _save_figure(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _empty_axis(ax: plt.Axes, title: str) -> None:
    ax.text(
        0.5, 0.5, "No eligible observations", ha="center", va="center",
        fontsize=13, color="#5B6573", transform=ax.transAxes,
    )
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_axis_off()


def _primary(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("latency_id") == "PRIMARY"]


def chart_fill_rates(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["fill_rates"])
        if row.get("cancel_timer_us") == 200_000
    ]
    source = []
    for row in rows:
        source.append({
            "date": row["date"],
            "track": row["track"],
            "filled_orders": row["fill_rate_order_num"],
            "eligible_orders": row["fill_rate_order_den"],
            "fill_rate": _ratio(
                row["fill_rate_order_num"], row["fill_rate_order_den"]
            ),
        })
    csv_path = out / "chart_sources" / "fill_rates_primary_200ms.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "01_fill_rates_primary.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    dates = sorted({str(row["date"]) for row in source})
    if not dates:
        _empty_axis(ax, "Observed virtual-order fill rate")
    else:
        width = 0.24
        x = list(range(len(dates)))
        for offset, track in enumerate(TRACK_ORDER):
            lookup = {
                str(row["date"]): float(row["fill_rate"])
                for row in source if row["track"] == track
            }
            values = [100 * lookup.get(date, 0.0) for date in dates]
            positions = [value + (offset - 1) * width for value in x]
            ax.bar(
                positions, values, width=width, label=TRACK_LABELS[track],
                color=TRACK_COLORS[track],
            )
        ax.set_xticks(x, [DATE_LABEL.get(date, date) for date in dates])
        ax.set_ylabel("Orders with any fill (%)")
        ax.set_title(
            "Observed virtual-order fill rate — 50ms placement, 200ms timer",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, ncol=1)
        ax.grid(axis="y", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_markouts(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["markouts"])
        if row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
    ]
    source = []
    for row in rows:
        total = _plain_int(row["total_filled_count_e4"], "total_filled_count_e4")
        observed = _plain_int(row["observed_count_e4"], "observed_count_e4")
        weighted = _plain_int(row["weighted_gross_sum_e8"], "weighted_gross_sum_e8")
        lower = _plain_int(row["weighted_gross_lower_sum_e8"], "weighted_gross_lower_sum_e8")
        upper = _plain_int(row["weighted_gross_upper_sum_e8"], "weighted_gross_upper_sum_e8")
        source.append({
            "date": row["date"],
            "horizon_us": row["horizon_us"],
            "observed_slices": row["observed_slices"],
            "censored_slices": row["censored_slices"],
            "total_filled_count_e4": total,
            "observed_count_e4": observed,
            "censored_count_e4": row["censored_count_e4"],
            "coverage_fraction": observed / total if total else None,
            "observed_contribution_e4": weighted / total if total else None,
            "gross_lower_mean_e4": lower / total if total else None,
            "gross_upper_mean_e4": upper / total if total else None,
        })
    csv_path = out / "chart_sources" / "strict_markouts_primary_200ms.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "02_strict_markouts.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    dates = sorted({str(row["date"]) for row in source})
    if not dates:
        _empty_axis(ax, "Strict-through executable gross markout")
    else:
        for date in dates:
            values = sorted(
                (row for row in source if row["date"] == date),
                key=lambda row: int(row["horizon_us"]),
            )
            x = [int(row["horizon_us"]) / 1000 for row in values]
            y = [
                (float(row["observed_contribution_e4"]) / 100 if row["observed_contribution_e4"] is not None else math.nan)
                for row in values
            ]
            lower = [
                (float(row["gross_lower_mean_e4"]) / 100 if row["gross_lower_mean_e4"] is not None else math.nan)
                for row in values
            ]
            upper = [
                (float(row["gross_upper_mean_e4"]) / 100 if row["gross_upper_mean_e4"] is not None else math.nan)
                for row in values
            ]
            ax.errorbar(
                x, y,
                yerr=[
                    [max(0.0, point - lo) for point, lo in zip(y, lower)],
                    [max(0.0, hi - point) for point, hi in zip(y, upper)],
                ],
                marker="o", linewidth=2, capsize=3,
                label=DATE_LABEL.get(date, date),
            )
        ax.axhline(0, color="#A23E48", linewidth=1.2)
        ax.set_xscale("log")
        ax.set_xticks([100, 200, 500, 1000], ["100", "200", "500", "1000"])
        ax.set_xlabel("Milliseconds after fill")
        ax.set_ylabel("Gross markout bound (¢ / filled contract)")
        ax.set_title(
            "Strict-through: legal-price bounds for depth-censored gross markout",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, ncol=3)
        ax.grid(alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_attribution(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["attribution"])
        if row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
        and row.get("horizon_us") in (200_000, 1_000_000)
    ]
    source = []
    for row in rows:
        total = _plain_int(row["total_filled_count_e4"], "total_filled_count_e4")
        observed = _plain_int(row["observed_count_e4"], "observed_count_e4")
        weighted = _plain_int(row["weighted_gross_sum_e8"], "weighted_gross_sum_e8")
        source.append({
            "date": row["date"],
            "horizon_us": row["horizon_us"],
            "refill_group": row["refill_group"],
            "total_filled_count_e4": total,
            "observed_count_e4": observed,
            "censored_count_e4": row["censored_count_e4"],
            "weighted_gross_sum_e8": weighted,
            "weighted_gross_lower_sum_e8": row["weighted_gross_lower_sum_e8"],
            "weighted_gross_upper_sum_e8": row["weighted_gross_upper_sum_e8"],
        })
    csv_path = out / "chart_sources" / "strict_refill_attribution.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "03_refill_attribution.png"
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.0), sharey=True)
    for ax, horizon in zip(axes, (200_000, 1_000_000)):
        subset = [row for row in source if row["horizon_us"] == horizon]
        groups = ("REFILL_BY_TIMER", "NON_REFILL_OR_CENSORED")
        values = []
        low_values = []
        high_values = []
        for group in groups:
            group_rows = [row for row in subset if row["refill_group"] == group]
            den = sum(int(row["total_filled_count_e4"]) for row in group_rows)
            point = sum(int(row["weighted_gross_sum_e8"]) for row in group_rows)
            lower = sum(int(row["weighted_gross_lower_sum_e8"]) for row in group_rows)
            upper = sum(int(row["weighted_gross_upper_sum_e8"]) for row in group_rows)
            values.append(point / den / 100 if den else math.nan)
            low_values.append(lower / den / 100 if den else math.nan)
            high_values.append(upper / den / 100 if den else math.nan)
        if not subset:
            _empty_axis(ax, f"{horizon // 1000}ms")
            continue
        bars = ax.bar(
            ["Refilled", "No refill / censored"], values,
            color=["#2A9D8F", "#E76F51"],
        )
        centers = [bar.get_x() + bar.get_width() / 2 for bar in bars]
        ax.errorbar(
            centers, values,
            yerr=[
                [max(0.0, value - low) for value, low in zip(values, low_values)],
                [max(0.0, high - value) for value, high in zip(values, high_values)],
            ],
            fmt="none", ecolor="#16324F", capsize=4, linewidth=1.2,
        )
        ax.bar_label(bars, fmt="%.3f¢", padding=3, fontsize=9)
        ax.axhline(0, color="#A23E48", linewidth=1)
        ax.set_title(f"{horizon // 1000}ms after fill", loc="left", fontweight="bold")
        ax.grid(axis="y", alpha=0.2)
    axes[0].set_ylabel("Gross bound / filled contract (¢)")
    fig.suptitle(
        "Adverse-selection attribution — descriptive, not a net-PnL ledger",
        x=0.04, ha="left", fontweight="bold",
    )
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_queue_band(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["fill_rates"])
        if row.get("cancel_timer_us") in (200_000, 300_000)
    ]
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["cancel_timer_us"]), str(row["track"]))].append(row)
    source = []
    for (timer, track), parts in sorted(grouped.items()):
        num = sum(int(row["queue_band_fill_rate_order_num"]) for row in parts)
        den = sum(int(row["queue_band_fill_rate_order_den"]) for row in parts)
        source.append({
            "cancel_timer_us": timer,
            "track": track,
            "filled_orders": num,
            "eligible_orders": den,
            "fill_rate": num / den if den else None,
        })
    csv_path = out / "chart_sources" / "queue_band_primary.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "04_queue_band.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    if not source:
        _empty_axis(ax, "Queue uncertainty band")
    else:
        x = [0, 1]
        timers = [200_000, 300_000]
        for track in TRACK_ORDER:
            lookup = {
                int(row["cancel_timer_us"]): row["fill_rate"]
                for row in source if row["track"] == track
            }
            y = [
                100 * float(lookup[timer]) if lookup.get(timer) is not None else math.nan
                for timer in timers
            ]
            ax.plot(
                x, y, marker="o", linewidth=2.4, color=TRACK_COLORS[track],
                label=TRACK_LABELS[track],
            )
        ax.set_xticks(x, ["Cancel at 200ms", "Cancel at 300ms"])
        ax.set_ylabel("Orders with any fill (%)")
        ax.set_title(
            "Queue-model band — the strict line is the binding lower bound",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.5))
        ax.grid(axis="y", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_exclusions(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    source = []
    for row in data["exclusions"]:
        if not isinstance(row, Mapping):
            raise C1ReportError("invalid exclusion row")
        source.append({
            "date": str(row.get("date", "_UNKNOWN")),
            "reason": str(row.get("reason", "UNKNOWN")),
            "count": _plain_int(row.get("count", 0), "exclusion count"),
        })
    source.sort(key=lambda row: (str(row["date"]), str(row["reason"])))
    csv_path = out / "chart_sources" / "exclusion_waterfall.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "05_exclusions.png"
    grouped_counts: dict[str, int] = defaultdict(int)
    for row in source:
        grouped_counts[str(row["reason"])] += int(row["count"])
    shown = [
        {"reason": reason, "count": count}
        for reason, count in sorted(grouped_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]
    fig, ax = plt.subplots(figsize=(10.5, 6.0))
    if not shown:
        _empty_axis(ax, "Fail-closed exclusion waterfall")
    else:
        labels = [str(row["reason"]).replace("_", " ").title() for row in shown][::-1]
        values = [int(row["count"]) for row in shown][::-1]
        ax.barh(labels, values, color="#6C8EAD")
        ax.set_xlabel("Campaign / observation count")
        ax.set_title("Fail-closed exclusion waterfall", loc="left", fontweight="bold")
        ax.grid(axis="x", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def generate_charts(data: Mapping[str, Any], out: Path) -> list[Path]:
    (out / "charts").mkdir(parents=True, exist_ok=True)
    (out / "chart_sources").mkdir(parents=True, exist_ok=True)
    functions = (
        chart_fill_rates,
        chart_markouts,
        chart_attribution,
        chart_queue_band,
        chart_exclusions,
    )
    return [function(data, out)[0] for function in functions]


def _image_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _fmt_int(value: object) -> str:
    return f"{_plain_int(value, 'display integer'):,}"


def _primary_strict_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        row for row in data["fill_rates"]
        if row.get("latency_id") == "PRIMARY"
        and row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
    ]


def build_html(data: Mapping[str, Any], charts: Sequence[Path], path: Path) -> None:
    verdict = data["verdict"]
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    strict_rows = _primary_strict_rows(data)
    eligible = sum(int(row["fill_rate_order_den"]) for row in strict_rows)
    filled = sum(int(row["fill_rate_order_num"]) for row in strict_rows)
    fill_rate = 100 * filled / eligible if eligible else math.nan
    image_sections = []
    captions = (
        "Three fill tracks by clean date",
        "Strict-through gross markout with censoring bounds",
        "Refill versus non-refill partial-identification bounds",
        "Queue-model uncertainty band",
        "Fail-closed exclusion waterfall",
    )
    for chart, caption in zip(charts, captions):
        image_sections.append(
            f'<section class="chart"><h3>{html.escape(caption)}</h3>'
            f'<img src="{_image_data_uri(chart)}" alt="{html.escape(caption)}"></section>'
        )
    concentration_rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(key, '')))}</td>"
            for key in ("date", "events", "markets", "max_market_share", "hhi")
        ) + "</tr>"
        for row in data["concentration"]
    )
    generated = str(summary.get("generated_at_utc", data.get("generated_at_utc", "BOUND_TO_RUN")))
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>C1 Real-Fill Kill Test</title><style>
:root{{--ink:#17202a;--muted:#5b6573;--navy:#16324f;--teal:#2a9d8f;--sand:#f4f1ea;--red:#a23e48}}
*{{box-sizing:border-box}} body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--ink);background:#edf1f4}}
main{{max-width:1120px;margin:24px auto;background:white;padding:48px 56px;box-shadow:0 6px 32px #10203020}}
.eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--teal);font-weight:700}}
h1{{font-size:38px;line-height:1.1;margin:8px 0 10px}} h2{{margin-top:40px;border-bottom:2px solid #e7ecef;padding-bottom:8px}} h3{{font-size:18px}}
.warning{{background:#fff1f0;border-left:5px solid var(--red);padding:18px 20px;margin:22px 0}}
.verdict{{background:var(--navy);color:white;padding:24px;margin:22px 0}} .verdict strong{{font-size:24px;display:block;margin-bottom:8px}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}} .metric{{background:var(--sand);padding:16px}} .metric b{{font-size:24px;display:block}}
.chart{{margin:30px 0;border-top:1px solid #e7ecef;padding-top:18px}} .chart img{{width:100%;height:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{border-bottom:1px solid #dde3e8;padding:9px;text-align:right}} th:first-child,td:first-child{{text-align:left}}
code{{overflow-wrap:anywhere}} .muted{{color:var(--muted)}} ul{{line-height:1.55}} footer{{margin-top:42px;color:var(--muted);font-size:12px}}
@media(max-width:760px){{main{{margin:0;padding:24px}}.metrics{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<div class="eyebrow">C1 · Deepresearch V3 follow-through · {html.escape(generated)}</div>
<h1>Real-Fill Kill Test<br><span class="muted">深度回补做市的真实成交下界</span></h1>
<p>本报告检验“显示深度回补”能否转化为一张真实可成交的被动单。输入只来自 2026-07-12、07-15、07-17 三个通过 L2 质量门的精确版本。</p>
<div class="warning"><b>EXPLORATORY_NON_GATE</b><br>所有金额均为手续费前的 gross markout，不是净利润。只有成交后盘口中可见的同侧退出深度被计为 executable；其余数量保留完整合法价格上下界。历史动态费率、账户舍入、实测下单/撤单延迟和终值账本尚未闭合。</div>
<div class="verdict"><strong>{html.escape(str(verdict.get('code')))}</strong>{html.escape(str(verdict.get('reason', '')))}</div>
<div class="metrics">
<div class="metric"><span>Clean dates</span><b>3</b></div>
<div class="metric"><span>Primary strict eligible</span><b>{eligible:,}</b></div>
<div class="metric"><span>Primary strict filled</span><b>{filled:,}</b></div>
<div class="metric"><span>Observed fill rate</span><b>{fill_rate:.3f}%</b></div>
</div>
<h2>Executive reading</h2>
<ul><li>Headline uses only <b>strict-through</b>: an at-price print never counts as our fill.</li><li>Future markout state must be strictly after the fill; visible exit quantity caps the executable component.</li><li>Censored quantity is not dropped: legal-price lower/upper bounds determine the verdict, with no fitted coverage threshold.</li><li>Queue-pessimistic and optimistic-at-touch are diagnostics, not promotion evidence.</li><li>No p-value is reported: three clean dates are shown independently, not treated as hundreds of thousands of independent trials.</li><li>A retain verdict can only request a 20-day validation; it cannot authorize live trading.</li></ul>
<h2>Results</h2>{''.join(image_sections)}
<h2>Support and concentration</h2>
<table><thead><tr><th>Date</th><th>Events</th><th>Markets</th><th>Max market share</th><th>HHI</th></tr></thead><tbody>{concentration_rows}</tbody></table>
<h2>What remains blocked</h2><ul>
<li><b>Fee-after net PnL:</b> {html.escape(str(data.get('fee_state')))}.</li>
<li><b>Latency:</b> 5/50/500ms are preregistered diagnostics, not measured production distributions.</li>
<li><b>Queue truth:</b> public L2 cannot identify whether a negative delta canceled the front or back of the queue.</li>
<li><b>Capacity:</b> displayed top depth is applied per fill slice only; this report makes no simultaneous portfolio-capacity claim.</li>
<li><b>Validation:</b> only three clean dates; final train/validation gate needs at least 20.</li></ul>
<h2>Reproducibility</h2><pre>{html.escape(json.dumps(data.get('provenance', {}), ensure_ascii=False, indent=2, sort_keys=True))}</pre>
<footer>Experiment {html.escape(str(data.get('experiment_id')))} · live-order writes: 0 · positive_promotion: false</footer>
</main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def _register_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("C1Unicode", str(candidate)))
            return "C1Unicode"
        except Exception:
            continue
    return "Helvetica"


def build_pdf(data: Mapping[str, Any], charts: Sequence[Path], path: Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
            Spacer, Table, TableStyle,
        )
    except ImportError as exc:
        raise C1ReportError("ReportLab is required to publish the PDF") from exc

    font = _register_pdf_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="C1Title", parent=styles["Title"], fontName=font, fontSize=26,
        leading=31, textColor=colors.HexColor("#16324F"), alignment=TA_LEFT,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="C1H1", parent=styles["Heading1"], fontName=font, fontSize=17,
        leading=21, textColor=colors.HexColor("#16324F"), spaceBefore=14,
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        name="C1Body", parent=styles["BodyText"], fontName=font, fontSize=9.5,
        leading=14, spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="C1Small", parent=styles["BodyText"], fontName=font, fontSize=7.5,
        leading=10, textColor=colors.HexColor("#5B6573"),
    ))
    styles.add(ParagraphStyle(
        name="C1Warning", parent=styles["BodyText"], fontName=font, fontSize=10,
        leading=14, textColor=colors.HexColor("#7B1E24"), borderColor=colors.HexColor("#A23E48"),
        borderWidth=1, borderPadding=9, backColor=colors.HexColor("#FFF1F0"),
        spaceBefore=6, spaceAfter=12,
    ))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path), pagesize=letter, leftMargin=0.58 * inch,
        rightMargin=0.58 * inch, topMargin=0.55 * inch, bottomMargin=0.72 * inch,
        title="C1 Real-Fill Kill Test", author="HFT BOT Research",
    )
    story: list[Any] = []
    verdict = data["verdict"]
    strict = _primary_strict_rows(data)
    eligible = sum(int(row["fill_rate_order_den"]) for row in strict)
    filled = sum(int(row["fill_rate_order_num"]) for row in strict)
    rate = 100 * filled / eligible if eligible else math.nan
    story.extend([
        Paragraph("C1 REAL-FILL KILL TEST", styles["C1Title"]),
        Paragraph("深度回补做市：从显示回补到真实成交下界", styles["C1H1"]),
        Paragraph(
            "EXPLORATORY_NON_GATE — 所有金额均为手续费前的 gross markout；只有成交后"
            "可见退出深度属于 executable，剩余数量保留合法价格上下界。本报告不是净利润，"
            "也不授权任何实盘订单。", styles["C1Warning"],
        ),
        Paragraph(f"<b>Verdict: {html.escape(str(verdict.get('code')))}</b>", styles["C1H1"]),
        Paragraph(html.escape(str(verdict.get("reason", ""))), styles["C1Body"]),
    ])
    metrics = [
        ["Clean dates", "Primary eligible", "Strict filled", "Strict rate"],
        ["3", f"{eligible:,}", f"{filled:,}", f"{rate:.3f}%"],
    ]
    table = Table(metrics, colWidths=[1.7 * inch] * 4)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F4F1EA")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 10)])
    story.append(Paragraph(
        "主结果只使用 strict-through：等价成交不算填单。Queue-pessimistic 与 "
        "optimistic-at-touch 只展示模型带宽。未来盘口必须严格晚于成交，且可执行量受"
        "顶档深度限制；censor 数量通过合法价格上下界进入决策。三天逐日报告，不进行 "
        "t 检验或显著性声明。",
        styles["C1Body"],
    ))
    captions = (
        "1. 三轨成交率",
        "2. 严格穿价成交后的 gross markout 上下界",
        "3. 回补 / 不回补的逆向选择上下界",
        "4. 队列模型带宽",
        "5. Fail-closed 排除瀑布",
    )
    for index, (chart, caption) in enumerate(zip(charts, captions)):
        if index in (1, 3):
            story.append(PageBreak())
        image = Image(str(chart), width=7.1 * inch, height=3.38 * inch)
        story.append(KeepTogether([
            Paragraph(caption, styles["C1H1"]), image,
            Paragraph(
                "Source CSV 与本图同目录；gross ≠ net。空缺、非成交后、跨 epoch、过期"
                "状态及退出深度不足均进入 censor 数量和合法价格界。",
                styles["C1Small"],
            ),
        ]))
    story.append(PageBreak())
    story.extend([
        Paragraph("为什么现在仍不能说“赚钱”", styles["C1H1"]),
        Paragraph(
            "精确版本未闭合逐成交历史 fee change、全部 event override、账户舍入精度与"
            "订单级 accumulator；5/50/500ms 也是预注册压力值而不是生产实测。故 fee-after "
            "net PnL、容量、日利润和 G3 晋级全部 fail closed。", styles["C1Body"],
        ),
        Paragraph("下一决策", styles["C1H1"]),
        Paragraph(
            "若 verdict 保留候选：补齐费用证据与生产 place/cancel latency，继续累计至少 20 "
            "个干净 L2 日后进行按日 train/validation。若 verdict 淘汰：保留深度不回补作为"
            "撤单/退出信号，不再把 C1 当进攻性做市策略。", styles["C1Body"],
        ),
        Paragraph("Provenance", styles["C1H1"]),
        Paragraph(
            html.escape(json.dumps(data.get("provenance", {}), ensure_ascii=False, sort_keys=True)),
            styles["C1Small"],
        ),
    ])

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#5B6573"))
        canvas.drawString(0.58 * inch, 0.34 * inch, str(data.get("experiment_id")))
        canvas.drawRightString(7.92 * inch, 0.34 * inch, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def publish(input_dir: Path, output_dir: Path, pdf_path: Path) -> dict[str, Any]:
    data = _load_json(input_dir / "C1_AGGREGATES.json")
    validate_aggregates(data)
    complete_path = input_dir / "C1_RUN_COMPLETE.json"
    if not complete_path.is_file():
        raise C1ReportError("analysis completion receipt is missing")
    complete = _load_json(complete_path)
    if (
        set(complete) != {
            "schema_version", "state", "experiment_id", "claim_tier", "verdict",
            "partition_receipts", "analysis_artifact_ledger_sha256",
            "live_order_writes", "report_publication_pending",
        }
        or complete_path.is_symlink()
        or complete.get("schema_version") != "c1-real-fill-run-complete-v1"
        or complete.get("state") != "COMPLETE"
        or complete.get("experiment_id") != data.get("experiment_id")
        or complete.get("claim_tier") != "EXPLORATORY_NON_GATE"
        or complete.get("verdict") != data.get("verdict")
        or complete.get("partition_receipts") != 48
        or complete.get("live_order_writes") != 0
        or complete.get("report_publication_pending") is not True
    ):
        raise C1ReportError("analysis completion receipt does not bind this aggregate")
    analysis_ledger_path = _validate_analysis_ledger(input_dir, complete)
    if output_dir.exists() or pdf_path.exists():
        raise C1ReportError("publication destinations must not already exist")
    output_dir.mkdir(parents=True)
    charts = generate_charts(data, output_dir)
    html_path = output_dir / "index.html"
    build_html(data, charts, html_path)
    build_pdf(data, charts, pdf_path)
    chart_sources = sorted((output_dir / "chart_sources").glob("*.csv"))
    if len(charts) != 5 or len(chart_sources) != 5:
        raise C1ReportError("publication must contain exactly five charts and source tables")
    aggregate_path = input_dir / "C1_AGGREGATES.json"
    artifact_paths: list[tuple[str, Path]] = [
        ("analysis/C1_AGGREGATES.json", aggregate_path),
        ("analysis/ANALYSIS_ARTIFACT_SHA256.json", analysis_ledger_path),
        ("analysis/C1_RUN_COMPLETE.json", complete_path),
        ("report/index.html", html_path),
        (f"pdf/{pdf_path.name}", pdf_path),
    ]
    artifact_paths.extend(
        (f"report/charts/{path.name}", path) for path in sorted(charts)
    )
    artifact_paths.extend(
        (f"report/chart_sources/{path.name}", path) for path in chart_sources
    )
    ledger = {
        "schema_version": "c1-report-artifact-ledger-v1",
        "experiment_id": data["experiment_id"],
        "artifacts": [
            {
                "path": logical,
                "sha256": _sha256_path(path),
                "bytes": path.stat().st_size,
            }
            for logical, path in artifact_paths
        ],
    }
    ledger_path = output_dir / "REPORT_ARTIFACT_SHA256.json"
    _write_canonical_json(ledger_path, ledger)
    publication_complete = {
        "schema_version": "c1-publication-complete-v1",
        "state": "COMPLETE",
        "experiment_id": data["experiment_id"],
        "claim_tier": "EXPLORATORY_NON_GATE",
        "verdict": data["verdict"],
        "analysis_complete_sha256": _sha256_path(complete_path),
        "analysis_artifact_ledger_sha256": _sha256_path(analysis_ledger_path),
        "aggregate_sha256": _sha256_path(aggregate_path),
        "report_artifact_ledger_sha256": _sha256_path(ledger_path),
        "chart_count": 5,
        "chart_source_count": 5,
        "positive_promotion": False,
        "live_order_writes": 0,
    }
    publication_complete_path = output_dir / "C1_PUBLICATION_COMPLETE.json"
    _write_canonical_json(publication_complete_path, publication_complete)
    return {
        "html": str(html_path),
        "pdf": str(pdf_path),
        "charts": [str(path) for path in charts],
        "chart_sources": [str(path) for path in chart_sources],
        "artifact_ledger": str(ledger_path),
        "publication_complete": str(publication_complete_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pdf-path", type=Path, required=True)
    args = parser.parse_args(argv)
    result = publish(args.input_dir, args.output_dir, args.pdf_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
