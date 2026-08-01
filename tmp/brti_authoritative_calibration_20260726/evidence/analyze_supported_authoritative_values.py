#!/usr/bin/env python3
"""Sealed value analysis bound to the timestamp support manifest."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any
import gzip


EXPECTED_ACQUISITION_SHA256 = (
    "a0098b608caf0eb5d9c16f77ffcac29e3936c37ccd311f606c5eabb0a36b2682"
)
EXPECTED_SUPPORT_RECEIPT_SHA256 = (
    "06e94c82b06eeb76523aaa8008ebac74f5105c7427665515294171cb3b42d2bb"
)
EXPECTED_SUPPORT_MANIFEST_SHA256 = (
    "2d104cc1715348a2cdea18af176792da970f0ba6e6fe16172085422294bb9d41"
)
EXPECTED_BASE_ANALYZER_SHA256 = (
    "4a127b6307dc8566b166ec58af7282fd4aec39d44f4615fe396c1d06336991bd"
)
EXPECTED_SUPPORTED_CHECKPOINTS = 854
EXPECTED_PRICEABLE_SUPPORTED_CHECKPOINTS = 848
METADATA_STRIKE_MISSING_MARKETS = {
    "KXBTC15M-26JUL221430-30",
    "KXBTC15M-26JUL221445-45",
}
ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
BANNED_START_MS = 1_784_764_800_000
GAP_TEMPLATE = "/home/ubuntu/hft-bot/work/event_packs/l2_gaps_{date}.json"
ZERO_GAP_FIELDS = (
    "sids_with_seq_gaps",
    "seq_gap_events",
    "seq_missed_total",
    "seq_regressions",
    "stream_restarts",
    "markers_lost_frames",
    "parse_errors",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load sealed module {name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def verify_seal(
    script: Path,
    base_analyzer: Path,
    prereg: Path,
    seal: Path,
    acquisition_receipt: Path,
    support_receipt: Path,
    support_manifest: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "script_sha256": sha256_path(script),
        "base_analyzer_sha256": sha256_path(base_analyzer),
        "preregistration_sha256": sha256_path(prereg),
        "acquisition_receipt_sha256": sha256_path(
            acquisition_receipt
        ),
        "support_receipt_sha256": sha256_path(support_receipt),
        "support_manifest_sha256": sha256_path(support_manifest),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    expected = {
        "base_analyzer_sha256": EXPECTED_BASE_ANALYZER_SHA256,
        "acquisition_receipt_sha256": EXPECTED_ACQUISITION_SHA256,
        "support_receipt_sha256": EXPECTED_SUPPORT_RECEIPT_SHA256,
        "support_manifest_sha256": EXPECTED_SUPPORT_MANIFEST_SHA256,
    }
    for key, value in expected.items():
        if observed[key] != value:
            raise RuntimeError(f"embedded evidence hash mismatch: {key}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    sources = specification.get("sources") or {}
    source_bindings = {
        "v3_acquisition_receipt_sha256": (
            EXPECTED_ACQUISITION_SHA256
        ),
        "timestamp_support_receipt_sha256": (
            EXPECTED_SUPPORT_RECEIPT_SHA256
        ),
        "timestamp_support_manifest_sha256": (
            EXPECTED_SUPPORT_MANIFEST_SHA256
        ),
        "base_calibration_analyzer_sha256": (
            EXPECTED_BASE_ANALYZER_SHA256
        ),
    }
    for key, value in source_bindings.items():
        if sources.get(key) != value:
            raise RuntimeError(f"pre-registration source mismatch: {key}")
    if (
        specification.get("support_gate", {}).get(
            "supported_checkpoint_count_before_value_read"
        )
        != EXPECTED_SUPPORTED_CHECKPOINTS
    ):
        raise RuntimeError("pre-registered supported-count mismatch")
    if (
        specification.get("support_gate", {}).get(
            "priceable_supported_checkpoint_count_before_value_read"
        )
        != EXPECTED_PRICEABLE_SUPPORTED_CHECKPOINTS
    ):
        raise RuntimeError("pre-registered priceable-count mismatch")
    if set(
        specification.get("support_gate", {}).get(
            "metadata_strike_missing_markets"
        )
        or []
    ) != METADATA_STRIKE_MISSING_MARKETS:
        raise RuntimeError("pre-registered missing-strike set mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def load_support_evidence(
    support_receipt_path: Path,
    support_manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = json.loads(support_receipt_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        support_manifest_path.read_text(encoding="utf-8")
    )
    if receipt.get("status") != "TIMESTAMP_SUPPORT_MAPPED":
        raise RuntimeError("timestamp support receipt is not complete")
    if (
        receipt.get(
            "brti_values_deserialized_accessed_converted_or_output"
        )
        is not False
    ):
        raise RuntimeError("timestamp support was not value-blind")
    if (
        receipt.get("support_manifest", {}).get("sha256")
        != EXPECTED_SUPPORT_MANIFEST_SHA256
    ):
        raise RuntimeError("support receipt/manifest hash mismatch")
    if manifest.get("contains_brti_values") is not False:
        raise RuntimeError("support manifest contains values")
    if int(manifest.get("market_count", -1)) != 288:
        raise RuntimeError("support manifest market count mismatch")
    supported = sum(
        bool(checkpoint["analysis_checkpoint_supported"])
        for market in manifest["markets"].values()
        for checkpoint in market["checkpoints"].values()
    )
    if supported != EXPECTED_SUPPORTED_CHECKPOINTS:
        raise RuntimeError(
            f"support manifest has {supported} supported checkpoints"
        )
    return manifest, {
        "support_receipt": {
            "path": str(support_receipt_path),
            "bytes": support_receipt_path.stat().st_size,
            "sha256": sha256_path(support_receipt_path),
        },
        "support_manifest": {
            "path": str(support_manifest_path),
            "bytes": support_manifest_path.stat().st_size,
            "sha256": sha256_path(support_manifest_path),
        },
        "pre_value_supported_checkpoint_count": supported,
        "pre_value_unsupported_checkpoint_count": 864 - supported,
        "support_rules_mutated_after_value_read": False,
        "strike_or_timestamp_imputation_used": False,
    }


def load_book_gap_gate() -> dict[str, Any]:
    rows = []
    passed = True
    for date in ALLOWED_DATES:
        path = Path(GAP_TEMPLATE.format(date=date))
        raw = path.read_bytes()
        receipt = json.loads(raw)
        observed = {}
        for field in ZERO_GAP_FIELDS:
            value = receipt.get(field)
            try:
                observed[field] = int(value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"gap receipt {date} missing integer {field}"
                ) from exc
            if observed[field] != 0:
                passed = False
        rows.append({
            "date": date,
            "path": str(path),
            "bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "required_zero_fields": observed,
        })
    if not passed:
        raise RuntimeError("shared book capture gap gate failed")
    return {
        "passed": True,
        "required_zero_fields": list(ZERO_GAP_FIELDS),
        "receipts": rows,
    }


def load_projected_values(
    source_dir: Path,
    acquisition_receipt_path: Path,
) -> tuple[list[int], list[float], dict[str, Any]]:
    receipt = json.loads(
        acquisition_receipt_path.read_text(encoding="utf-8")
    )
    if receipt.get("status") != "NO_DECISION_BOUNDARY_ACQUISITION_FAILED":
        raise RuntimeError("unexpected acquisition receipt status")
    if int(receipt.get("raw_persisted_hour_count", -1)) != 72:
        raise RuntimeError("not all raw hourly envelopes were persisted")
    hour_rows = sorted(
        receipt.get("hours") or [],
        key=lambda row: row["hour_start_utc"],
    )
    if len(hour_rows) != 72:
        raise RuntimeError("acquisition receipt lacks 72 hours")
    projected_by_time: dict[int, float] = {}
    manifests = []
    raw_counts = Counter()
    for logical in hour_rows:
        hour_start = str(logical["hour_start_utc"])
        if hour_start >= "2026-07-23T00:00:00.000Z":
            raise RuntimeError("hard-banned hour in acquisition")
        if logical.get("persisted") is not True:
            raise RuntimeError(f"raw envelope absent: {hour_start}")
        hour_label = hour_start[:13].replace("T", "_")
        path = source_dir / f"brti_history_{hour_label}.json.gz"
        compressed = path.read_bytes()
        if sha256_bytes(compressed) != logical.get("stored_sha256"):
            raise RuntimeError(f"gzip hash mismatch for {hour_start}")
        body = gzip.decompress(compressed)
        attempts = logical.get("attempts") or []
        if not attempts or attempts[-1].get("http_status") != 200:
            raise RuntimeError(f"no successful body for {hour_start}")
        if sha256_bytes(body) != attempts[-1].get("body_sha256"):
            raise RuntimeError(f"body hash mismatch for {hour_start}")
        envelope = json.loads(body)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(f"invalid CF envelope for {hour_start}")
        payload = data.get("payload")
        if not isinstance(payload, list):
            raise RuntimeError(f"invalid CF payload for {hour_start}")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if sha256_bytes(canonical) != logical.get("payload_sha256"):
            raise RuntimeError(f"payload hash mismatch for {hour_start}")
        previous_time = None
        hour_projected = 0
        for row_number, row in enumerate(payload):
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"non-object payload row {hour_start}:{row_number}"
                )
            raw_time = row.get("time")
            if isinstance(raw_time, bool) or not isinstance(raw_time, int):
                raise RuntimeError(
                    f"invalid time {hour_start}:{row_number}"
                )
            source_ms = int(raw_time)
            if source_ms >= BANNED_START_MS:
                raise RuntimeError("value source reaches banned start")
            if previous_time is not None and source_ms <= previous_time:
                raise RuntimeError("raw source time duplicate/regression")
            if source_ms % 200 != 0:
                raise RuntimeError("raw source time off 200ms grid")
            previous_time = source_ms
            if source_ms % 1000 != 0:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid projected value {hour_start}:{row_number}"
                ) from exc
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError("nonpositive/nonfinite projected value")
            if source_ms in projected_by_time:
                raise RuntimeError("duplicate projected source time")
            projected_by_time[source_ms] = value
            hour_projected += 1
        raw_counts[str(len(payload))] += 1
        manifests.append({
            "hour_start_utc": hour_start,
            "path": str(path),
            "bytes": len(compressed),
            "sha256": sha256_bytes(compressed),
            "body_sha256": sha256_bytes(body),
            "raw_row_count": len(payload),
            "projected_second_count": hour_projected,
        })
    times = sorted(projected_by_time)
    values = [projected_by_time[source_ms] for source_ms in times]
    if len(times) != 259_051:
        raise RuntimeError(
            f"projected time count changed: {len(times)}"
        )
    return times, values, {
        "raw_hour_count": len(manifests),
        "projected_tick_count": len(times),
        "missing_from_full_three_day_second_grid": (
            72 * 3_600 - len(times)
        ),
        "raw_row_count_frequency": dict(sorted(raw_counts.items())),
        "raw_envelopes": manifests,
        "projection_predicate": "time_ms % 1000 == 0",
        "individual_values_in_report": False,
    }


def support_filtered_metadata(
    metadata: dict[str, dict[str, Any]],
    support_manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    support_markets = support_manifest.get("markets") or {}
    missing_in_support = set(metadata) - set(support_markets)
    missing_in_metadata = set(support_markets) - set(metadata)
    if (
        missing_in_support
        or missing_in_metadata != METADATA_STRIKE_MISSING_MARKETS
    ):
        raise RuntimeError(
            "metadata/support universe mismatch: "
            f"support_missing={sorted(missing_in_support)[:3]} "
            f"metadata_missing={sorted(missing_in_metadata)[:3]}"
        )
    included = {
        ticker: meta
        for ticker, meta in metadata.items()
        if support_markets[ticker]["settlement_window_supported"]
    }
    excluded = sorted(set(metadata) - set(included))
    metadata_missing_supported_checkpoints = sum(
        bool(checkpoint["analysis_checkpoint_supported"])
        for ticker in METADATA_STRIKE_MISSING_MARKETS
        for checkpoint in support_markets[ticker]["checkpoints"].values()
    )
    priceable_supported_checkpoints = sum(
        bool(checkpoint["analysis_checkpoint_supported"])
        for ticker in metadata
        for checkpoint in support_markets[ticker]["checkpoints"].values()
    )
    if metadata_missing_supported_checkpoints != 6:
        raise RuntimeError("missing-strike checkpoint count changed")
    if (
        priceable_supported_checkpoints
        != EXPECTED_PRICEABLE_SUPPORTED_CHECKPOINTS
    ):
        raise RuntimeError("priceable supported checkpoint count changed")
    return included, {
        "timestamp_support_market_count": len(support_markets),
        "metadata_market_count": len(metadata),
        "metadata_strike_missing_excluded_market_count": len(
            METADATA_STRIKE_MISSING_MARKETS
        ),
        "metadata_strike_missing_markets": sorted(
            METADATA_STRIKE_MISSING_MARKETS
        ),
        "metadata_strike_missing_checkpoint_exclusions": (
            metadata_missing_supported_checkpoints
        ),
        "priceable_supported_checkpoint_count_before_value_read": (
            priceable_supported_checkpoints
        ),
        "settlement_supported_before_value_read": len(included),
        "settlement_unsupported_excluded_before_value_read": len(
            excluded
        ),
        "settlement_unsupported_markets": excluded,
        "strike_or_timestamp_imputation_used": False,
    }


def build_supported_rows(
    base: Any,
    metadata: dict[str, dict[str, Any]],
    support_manifest: dict[str, Any],
    times: list[int],
    values: list[float],
    book_samples: dict[str, dict[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    excluded = Counter()
    excluded_details = []
    support_markets = support_manifest["markets"]
    expected_supported_after_settlement_crosscheck = sum(
        bool(
            support_markets[ticker]["checkpoints"][str(horizon)][
                "analysis_checkpoint_supported"
            ]
        )
        for ticker in metadata
        for horizon in base.HORIZONS_S
    )
    for ticker, meta in sorted(metadata.items()):
        market_support = support_markets[ticker]
        for horizon_s in base.HORIZONS_S:
            checkpoint = market_support["checkpoints"][str(horizon_s)]
            if not checkpoint["analysis_checkpoint_supported"]:
                excluded["timestamp_support_gate"] += 1
                excluded_details.append({
                    "market": ticker,
                    "horizon_s": horizon_s,
                    "reasons": checkpoint["reasons"],
                })
                continue
            target_ms = meta["close_ms"] - horizon_s * 1000
            kernel = base.kernel_at(
                times,
                values,
                target_ms,
                meta["close_ms"],
                meta["strike"],
            )
            if kernel is None:
                raise RuntimeError(
                    "support manifest promised kernel availability: "
                    f"{ticker} {horizon_s}"
                )
            book = book_samples.get(ticker, {}).get(horizon_s)
            if book is None:
                excluded["market_mid_unavailable_after_book_gate"] += 1
            rows.append({
                "market": ticker,
                "close_date": meta["close_date"],
                "horizon_s": horizon_s,
                "outcome": meta["outcome"],
                "kernel_p": kernel["probability"],
                "market_p": (
                    book["probability"] if book is not None else None
                ),
                "kernel_sigma60": kernel["sigma60"],
                "kernel_sigma300": kernel["sigma300"],
                "kernel_sigma": kernel["sigma"],
                "kernel_model_tte_s": kernel["model_tte_s"],
                "kernel_information_age_s": (
                    kernel["information_age_s"]
                ),
                "market_mid_age_s": (
                    book["age_s"] if book is not None else None
                ),
            })
    if len(rows) != expected_supported_after_settlement_crosscheck:
        raise RuntimeError(
            f"post-settlement supported row count {len(rows)} "
            f"!= expected {expected_supported_after_settlement_crosscheck}"
        )
    return rows, {
        "counts": dict(sorted(excluded.items())),
        "timestamp_support_excluded_details": excluded_details,
        "included_kernel_checkpoint_count": len(rows),
        "supported_after_settlement_crosscheck": (
            expected_supported_after_settlement_crosscheck
        ),
        "paired_book_checkpoint_count": sum(
            row["market_p"] is not None for row in rows
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    calibration = report["calibration"]
    pooled = calibration["all_horizons"]
    paired = pooled["paired_difference"]
    lines = [
        "# Supported authoritative BRTI calibration",
        "",
        f"Status: **{report['status']}** (discovery only).",
        "",
        f"- Pre-value supported checkpoints: "
        f"`{report['support_gate']['pre_value_supported_checkpoint_count']}`",
        f"- Kernel checkpoints analyzed: "
        f"`{report['analysis_population']['included_kernel_checkpoint_count']}`",
        f"- Paired causal-book checkpoints: "
        f"`{report['analysis_population']['paired_book_checkpoint_count']}`",
        f"- Unique calibration markets: `{calibration['unique_markets']}`",
        f"- Kernel Brier: `{pooled['kernel'].get('brier')}`",
        f"- Market-mid Brier: `{pooled['market'].get('brier')}`",
        f"- Kernel minus market Brier: "
        f"`{paired.get('kernel_minus_market_brier')}`",
        f"- Kernel log loss: `{pooled['kernel'].get('logloss')}`",
        f"- Market-mid log loss: `{pooled['market'].get('logloss')}`",
        f"- Kernel minus market log loss: "
        f"`{paired.get('kernel_minus_market_logloss')}`",
        "",
        "Negative paired differences favor the kernel; positive differences "
        "favor the market mid. Per-horizon results and market-cluster 95% "
        "confidence intervals are in the JSON.",
        "",
        "## 1/2/5/10-second lead",
        "",
        "**Blocked and reported separately.** Historical CF rows do not "
        "contain their original Kalshi receipt timestamp, so BRTI source "
        "time cannot be mixed with book receive time for causal markouts.",
        "",
        "This audit does not identify queue fills, opposite-leg completion, "
        "forced exits, or realizable market-making PnL, and does not authorize "
        "deployment.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--base-analyzer", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--acquisition-receipt", required=True)
    parser.add_argument("--support-receipt", required=True)
    parser.add_argument("--support-manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    base_analyzer_path = Path(args.base_analyzer).resolve()
    source_dir = Path(args.source_dir).resolve()
    acquisition_receipt = Path(args.acquisition_receipt).resolve()
    support_receipt_path = Path(args.support_receipt).resolve()
    support_manifest_path = Path(args.support_manifest).resolve()
    output_path = Path(args.out).resolve()
    markdown_path = Path(args.md_out).resolve()

    seal_info = verify_seal(
        script,
        base_analyzer_path,
        prereg,
        seal,
        acquisition_receipt,
        support_receipt_path,
        support_manifest_path,
    )
    support_manifest, support_audit = load_support_evidence(
        support_receipt_path, support_manifest_path
    )
    capture_gap_gate = load_book_gap_gate()
    base = load_module(
        base_analyzer_path, "sealed_supported_value_base"
    )
    metadata, metadata_audit = base.load_metadata()
    settlement_supported_metadata, support_metadata_audit = (
        support_filtered_metadata(metadata, support_manifest)
    )

    times, values, value_source_audit = load_projected_values(
        source_dir, acquisition_receipt
    )
    verified_metadata, settlement_audit = base.settlement_crosscheck(
        settlement_supported_metadata, times, values
    )
    book_paths = base.exact_book_paths()
    book_samples, book_replay = base.load_book_samples(
        book_paths, verified_metadata
    )
    rows, population_audit = build_supported_rows(
        base,
        verified_metadata,
        support_manifest,
        times,
        values,
        book_samples,
    )
    metrics = base.calibration_report(rows)
    status = (
        "DISCOVERY_RESULT"
        if metrics["minimum_reportable_satisfied"]
        else "NO_DECISION_INSUFFICIENT_MARKETS_OR_OUTCOMES"
    )
    report = {
        "schema": "brti-supported-authoritative-value-analysis-v1",
        "status": status,
        "claim": "DISCOVERY_ONLY_NOT_VALIDATION",
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "hard_banned_history_requested_or_opened": False,
        "seal": seal_info,
        "support_gate": support_audit,
        "capture_gap_gate": capture_gap_gate,
        "sources": {
            "authoritative_brti": value_source_audit,
            "market_metadata": metadata_audit,
            "support_metadata_alignment": support_metadata_audit,
            "orderbooks": base.file_manifest(
                path
                for date in base.ALLOWED_DATES
                for path in book_paths[date]
            ),
            "book_paths_by_date": book_paths,
            "book_replay": book_replay,
        },
        "settlement_crosscheck": settlement_audit,
        "analysis_population": population_audit,
        "calibration": metrics,
        "rows": rows,
        "separate_lead_report": {
            "horizons_s": [1, 2, 5, 10],
            "status": "BLOCKED_BRTI_RECEIVE_TIMESTAMP_ABSENT",
            "coefficients_or_markouts_computed": False,
            "reason": (
                "Historical CF rows have source time/value but not their "
                "original Kalshi receipt time. A causal lead test cannot mix "
                "BRTI source time with book receive time."
            ),
        },
        "execution_boundary": {
            "queue_fill_identified": False,
            "opposite_leg_completion_identified": False,
            "episode_pnl_identified": False,
            "strike_or_timestamp_imputation_used": False,
            "deployment_authorized": False,
        },
    }
    report = base.rounded(report)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
