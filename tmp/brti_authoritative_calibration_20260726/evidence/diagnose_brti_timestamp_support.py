#!/usr/bin/env python3
"""Sealed timestamp-only BRTI support diagnostic.

The raw response is never JSON-deserialized. Only exact integer values of the
JSON key ``time`` are extracted from bytes. BRTI ``value`` fields are neither
accessed nor converted.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
HORIZONS_S = (120, 180, 240)
SERIES = "KXBTC15M"
INFORMATION_LAG_MS = 2_000
MAX_CUTOFF_SOURCE_GAP_MS = 1_500
BANNED_START_MS = 1_784_764_800_000
EXPECTED_ACQUISITION_SHA256 = (
    "a0098b608caf0eb5d9c16f77ffcac29e3936c37ccd311f606c5eabb0a36b2682"
)
EXPECTED_ACQUISITION_SEAL_SHA256 = (
    "624dce500f0dbbce04e6596a71751b8eb34a4a58835ea6f07e2b6b9904976a80"
)
TIME_FIELD = re.compile(rb'"time"\s*:\s*(\d+)')
META_PATH = Path(
    "/home/ubuntu/h6b_inputs/catalog_normal/"
    "snapshot=20260724T213017Z/shards/KXBTC15M.json"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_ms(text: str) -> int:
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp: {text}")
    return round(parsed.timestamp() * 1000)


def utc_date(timestamp_ms: int) -> str:
    return dt.datetime.fromtimestamp(
        timestamp_ms / 1000, tz=dt.timezone.utc
    ).date().isoformat()


def verify_seal(
    script: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "script_sha256": sha256_path(script),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    if (
        specification.get("source", {}).get(
            "v3_acquisition_receipt_sha256"
        )
        != EXPECTED_ACQUISITION_SHA256
    ):
        raise RuntimeError("acquisition receipt binding mismatch")
    if (
        specification.get("universe", {}).get(
            "selected_close_dates_utc"
        )
        != list(ALLOWED_DATES)
    ):
        raise RuntimeError("selected close-date mismatch")
    if (
        specification.get("universe", {}).get("fixed_horizons_s")
        != list(HORIZONS_S)
    ):
        raise RuntimeError("horizon mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def extract_times_only(body: bytes) -> list[int]:
    return [int(match.group(1)) for match in TIME_FIELD.finditer(body)]


def load_projected_timestamps(
    source_dir: Path,
    receipt_path: Path,
) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    receipt_bytes = receipt_path.read_bytes()
    if sha256_bytes(receipt_bytes) != EXPECTED_ACQUISITION_SHA256:
        raise RuntimeError("V3 acquisition receipt hash mismatch")
    receipt = json.loads(receipt_bytes)
    if receipt.get("status") != "NO_DECISION_BOUNDARY_ACQUISITION_FAILED":
        raise RuntimeError("unexpected V3 acquisition status")
    if int(receipt.get("raw_persisted_hour_count", -1)) != 72:
        raise RuntimeError("V3 did not persist exactly 72 raw envelopes")
    if (
        receipt.get("seal", {}).get("seal_sha256")
        != EXPECTED_ACQUISITION_SEAL_SHA256
    ):
        raise RuntimeError("V3 acquisition seal mismatch")
    hour_rows = receipt.get("hours") or []
    if len(hour_rows) != 72:
        raise RuntimeError("V3 receipt does not contain 72 hours")
    raw_manifests = []
    hour_support = []
    all_projected_times: list[int] = []
    for logical in sorted(
        hour_rows, key=lambda row: row["hour_start_utc"]
    ):
        hour_start = str(logical["hour_start_utc"])
        if hour_start >= "2026-07-23T00:00:00.000Z":
            raise RuntimeError("hard-banned hour in receipt")
        if logical.get("persisted") is not True:
            raise RuntimeError(f"raw envelope not persisted: {hour_start}")
        hour_label = hour_start[:13].replace("T", "_")
        path = source_dir / f"brti_history_{hour_label}.json.gz"
        compressed = path.read_bytes()
        if sha256_bytes(compressed) != logical.get("stored_sha256"):
            raise RuntimeError(f"gzip hash mismatch for {hour_start}")
        body = gzip.decompress(compressed)
        attempts = logical.get("attempts") or []
        if not attempts or attempts[-1].get("http_status") != 200:
            raise RuntimeError(f"no successful body hash for {hour_start}")
        if sha256_bytes(body) != attempts[-1].get("body_sha256"):
            raise RuntimeError(f"body hash mismatch for {hour_start}")
        times = extract_times_only(body)
        if len(times) != int(logical.get("payload_count", -1)):
            raise RuntimeError(
                f"exact time-field count mismatch for {hour_start}"
            )
        start_ms = utc_ms(hour_start)
        end_ms = start_ms + 3_600_000
        previous = None
        for source_ms in times:
            if not start_ms <= source_ms < end_ms:
                raise RuntimeError(f"time outside hour: {hour_start}")
            if source_ms >= BANNED_START_MS:
                raise RuntimeError("time reaches hard-banned start")
            if previous is not None and source_ms <= previous:
                raise RuntimeError(
                    f"nonascending/duplicate time: {hour_start}"
                )
            if source_ms % 200 != 0:
                raise RuntimeError(f"time off 200ms grid: {hour_start}")
            previous = source_ms
        projected = [
            source_ms for source_ms in times if source_ms % 1000 == 0
        ]
        expected = [
            start_ms + second * 1000 for second in range(3_600)
        ]
        missing = sorted(set(expected) - set(projected))
        extra = sorted(set(projected) - set(expected))
        duplicates = len(projected) - len(set(projected))
        all_projected_times.extend(projected)
        raw_manifests.append({
            "path": str(path),
            "bytes": len(compressed),
            "sha256": sha256_bytes(compressed),
            "body_sha256": sha256_bytes(body),
            "raw_time_count": len(times),
        })
        hour_support.append({
            "hour_start_utc": hour_start,
            "raw_time_count": len(times),
            "projected_second_count": len(projected),
            "missing_second_count": len(missing),
            "missing_second_timestamps_ms": missing,
            "extra_second_count": len(extra),
            "duplicate_second_count": duplicates,
            "complete_second_support": (
                not missing and not extra and duplicates == 0
            ),
        })
    if len(all_projected_times) != len(set(all_projected_times)):
        raise RuntimeError("cross-hour duplicate projected timestamp")
    return sorted(all_projected_times), raw_manifests, hour_support


def load_market_closes() -> tuple[dict[str, int], dict[str, Any]]:
    metadata_bytes = META_PATH.read_bytes()
    source = json.loads(metadata_bytes)
    markets = source.get("markets") if isinstance(source, dict) else None
    if not isinstance(markets, dict):
        raise RuntimeError("metadata has no markets mapping")
    selected = {}
    counts = Counter()
    for ticker, market in markets.items():
        counts["catalog_market_n"] += 1
        if not str(ticker).startswith(f"{SERIES}-"):
            continue
        counts["series_market_n"] += 1
        try:
            close_ms = utc_ms(market["close_time"])
        except (KeyError, TypeError, ValueError):
            counts["missing_close_time"] += 1
            continue
        if utc_date(close_ms) not in ALLOWED_DATES:
            counts["outside_selected_close_dates"] += 1
            continue
        selected[str(ticker)] = close_ms
        counts["selected_market_n"] += 1
    return selected, {
        "path": str(META_PATH),
        "bytes": len(metadata_bytes),
        "sha256": sha256_bytes(metadata_bytes),
        "counts": dict(sorted(counts.items())),
    }


def missing_integral_seconds(
    projected_set: set[int],
    start_ms: int,
    end_ms_inclusive: int,
) -> list[int]:
    return [
        timestamp_ms
        for timestamp_ms in range(
            start_ms, end_ms_inclusive + 1, 1_000
        )
        if timestamp_ms not in projected_set
    ]


def build_support_manifest(
    projected_times: list[int],
    market_closes: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    projected_set = set(projected_times)
    counts = Counter()
    horizon_counts = {
        str(horizon): Counter() for horizon in HORIZONS_S
    }
    markets = {}
    for ticker, close_ms in sorted(market_closes.items()):
        settlement_missing = missing_integral_seconds(
            projected_set, close_ms - 60_000, close_ms - 1_000
        )
        settlement_supported = not settlement_missing
        counts[
            "settlement_supported_market_n"
            if settlement_supported
            else "settlement_unsupported_market_n"
        ] += 1
        checkpoints = {}
        for horizon_s in HORIZONS_S:
            cutoff_ms = (
                close_ms - horizon_s * 1000 - INFORMATION_LAG_MS
            )
            index = bisect.bisect_right(
                projected_times, cutoff_ms
            ) - 1
            selected_source_ms = (
                projected_times[index] if index >= 0 else None
            )
            source_gap_ms = (
                cutoff_ms - selected_source_ms
                if selected_source_ms is not None else None
            )
            current_supported = (
                selected_source_ms is not None
                and source_gap_ms is not None
                and 0 <= source_gap_ms <= MAX_CUTOFF_SOURCE_GAP_MS
            )
            if current_supported:
                trailing_60_missing = missing_integral_seconds(
                    projected_set,
                    selected_source_ms - 60_000,
                    selected_source_ms,
                )
                trailing_300_missing = missing_integral_seconds(
                    projected_set,
                    selected_source_ms - 300_000,
                    selected_source_ms,
                )
            else:
                trailing_60_missing = []
                trailing_300_missing = []
            trailing_60_supported = (
                current_supported and not trailing_60_missing
            )
            trailing_300_supported = (
                current_supported and not trailing_300_missing
            )
            kernel_supported = (
                current_supported
                and trailing_60_supported
                and trailing_300_supported
            )
            analysis_supported = (
                kernel_supported and settlement_supported
            )
            reasons = []
            if not current_supported:
                reasons.append("CURRENT_SOURCE_UNSUPPORTED")
            if current_supported and not trailing_60_supported:
                reasons.append("TRAILING_60S_MISSING")
            if current_supported and not trailing_300_supported:
                reasons.append("TRAILING_300S_MISSING")
            if not settlement_supported:
                reasons.append("SETTLEMENT_WINDOW_MISSING")
            if not reasons:
                reasons.append("SUPPORTED")
            horizon_counter = horizon_counts[str(horizon_s)]
            horizon_counter[
                "analysis_supported"
                if analysis_supported
                else "analysis_unsupported"
            ] += 1
            if not kernel_supported:
                horizon_counter["kernel_unsupported"] += 1
            if not settlement_supported:
                horizon_counter["settlement_unsupported"] += 1
            checkpoints[str(horizon_s)] = {
                "horizon_s": horizon_s,
                "information_cutoff_ms": cutoff_ms,
                "selected_source_ms": selected_source_ms,
                "cutoff_source_gap_ms": source_gap_ms,
                "current_source_supported": current_supported,
                "trailing_60s_supported": trailing_60_supported,
                "trailing_60s_missing_count": len(
                    trailing_60_missing
                ),
                "trailing_60s_missing_timestamps_ms": (
                    trailing_60_missing
                ),
                "trailing_300s_supported": trailing_300_supported,
                "trailing_300s_missing_count": len(
                    trailing_300_missing
                ),
                "trailing_300s_missing_timestamps_ms": (
                    trailing_300_missing
                ),
                "kernel_checkpoint_supported": kernel_supported,
                "settlement_window_supported": settlement_supported,
                "analysis_checkpoint_supported": analysis_supported,
                "reasons": reasons,
            }
        all_supported = all(
            row["analysis_checkpoint_supported"]
            for row in checkpoints.values()
        )
        counts[
            "all_horizons_supported_market_n"
            if all_supported
            else "one_or_more_horizons_unsupported_market_n"
        ] += 1
        markets[ticker] = {
            "close_ms": close_ms,
            "settlement_window_supported": settlement_supported,
            "settlement_missing_count": len(settlement_missing),
            "settlement_missing_timestamps_ms": settlement_missing,
            "all_horizons_supported": all_supported,
            "checkpoints": checkpoints,
        }
    manifest = {
        "schema": "brti-timestamp-support-manifest-v1",
        "contains_brti_values": False,
        "contains_prices_probabilities_outcomes_strikes_or_pnl": False,
        "projected_timestamp_count": len(projected_times),
        "market_count": len(markets),
        "horizons_s": list(HORIZONS_S),
        "rules": {
            "projection": "time_ms % 1000 == 0",
            "information_lag_ms": INFORMATION_LAG_MS,
            "maximum_cutoff_source_gap_ms": (
                MAX_CUTOFF_SOURCE_GAP_MS
            ),
            "trailing_windows_require_every_second": [60, 300],
            "settlement_window_requires_every_second": True,
            "interpolation_or_neighbor_substitution": False,
        },
        "counts": dict(sorted(counts.items())),
        "counts_by_horizon_s": {
            horizon: dict(sorted(counter.items()))
            for horizon, counter in sorted(horizon_counts.items())
        },
        "markets": markets,
    }
    summary = {
        "market_counts": dict(sorted(counts.items())),
        "checkpoint_counts_by_horizon_s": {
            horizon: dict(sorted(counter.items()))
            for horizon, counter in sorted(horizon_counts.items())
        },
    }
    return manifest, summary


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["support_summary"]
    lines = [
        "# BRTI timestamp-only support diagnostic",
        "",
        "Status: **TIMESTAMP_SUPPORT_MAPPED**.",
        "",
        "No BRTI value was deserialized, accessed, converted, derived, or "
        "written. Only integer `time` fields and support booleans were used.",
        "",
        f"- Projected one-second timestamps: "
        f"`{report['projected_timestamp_count']}`",
        f"- Missing one-second timestamps: "
        f"`{report['missing_projected_second_count']}`",
        f"- Markets: `{report['market_count']}`",
        f"- All-horizon supported markets: "
        f"`{summary['market_counts'].get('all_horizons_supported_market_n', 0)}`",
        f"- Markets with one or more unsupported horizons: "
        f"`{summary['market_counts'].get('one_or_more_horizons_unsupported_market_n', 0)}`",
        "",
        "## Checkpoints",
        "",
    ]
    for horizon, counts in sorted(
        summary["checkpoint_counts_by_horizon_s"].items(),
        key=lambda item: int(item[0]),
    ):
        lines.append(
            f"- {horizon}s: supported "
            f"`{counts.get('analysis_supported', 0)}`, unsupported "
            f"`{counts.get('analysis_unsupported', 0)}`."
        )
    lines.extend([
        "",
        f"Support manifest SHA-256: "
        f"`{report['support_manifest']['sha256']}`.",
        "",
        "This receipt does not run or authorize model calibration. Any later "
        "analyzer must bind this manifest hash and exclude every unsupported "
        "market-checkpoint without interpolation.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--acquisition-receipt", required=True)
    parser.add_argument("--support-out", required=True)
    parser.add_argument("--receipt-out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    source_dir = Path(args.source_dir).resolve()
    acquisition_receipt = Path(args.acquisition_receipt).resolve()
    support_path = Path(args.support_out).resolve()
    receipt_path = Path(args.receipt_out).resolve()
    markdown_path = Path(args.md_out).resolve()

    seal_info = verify_seal(script, prereg, seal)
    projected_times, raw_manifests, hour_support = (
        load_projected_timestamps(source_dir, acquisition_receipt)
    )
    market_closes, metadata_audit = load_market_closes()
    support_manifest, support_summary = build_support_manifest(
        projected_times, market_closes
    )
    support_path.write_text(
        json.dumps(support_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    expected_projected_count = 72 * 3_600
    report = {
        "schema": "brti-timestamp-support-receipt-v1",
        "status": "TIMESTAMP_SUPPORT_MAPPED",
        "read_only": True,
        "model_analysis_performed": False,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "brti_values_deserialized_accessed_converted_or_output": False,
        "hard_banned_history_opened": False,
        "seal": seal_info,
        "source_acquisition_receipt": {
            "path": str(acquisition_receipt),
            "sha256": sha256_path(acquisition_receipt),
        },
        "raw_envelopes": raw_manifests,
        "hour_support": hour_support,
        "projected_timestamp_count": len(projected_times),
        "missing_projected_second_count": (
            expected_projected_count - len(projected_times)
        ),
        "market_count": len(market_closes),
        "metadata": metadata_audit,
        "support_summary": support_summary,
        "support_manifest": {
            "path": str(support_path),
            "bytes": support_path.stat().st_size,
            "sha256": sha256_path(support_path),
        },
        "next_step": (
            "SEAL_VALUE_ANALYZER_BOUND_TO_SUPPORT_MANIFEST_SHA; "
            "EXCLUDE_UNSUPPORTED_MARKET_CHECKPOINTS; NO_INTERPOLATION"
        ),
    }
    receipt_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
