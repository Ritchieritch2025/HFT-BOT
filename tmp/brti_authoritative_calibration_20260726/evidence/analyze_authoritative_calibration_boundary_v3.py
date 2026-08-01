#!/usr/bin/env python3
"""Sealed V3 calibration from timestamp-boundary BRTI projection."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any


BASE_DOWNLOADER_SHA256 = (
    "31a1d08cfee96afa71d5201950c75642823ceacbd94420ec47092ba5b795b8f1"
)
BASE_HOURLY_ANALYZER_SHA256 = (
    "a2d2dc54c2ae7e4c3079dd0cc2aea93b364b08e70366cf5f0ec936714744d89b"
)
BASE_CALIBRATION_ANALYZER_SHA256 = (
    "4a127b6307dc8566b166ec58af7282fd4aec39d44f4615fe396c1d06336991bd"
)
BANNED_START_MS = 1_784_764_800_000


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
    downloader: Path,
    base_downloader: Path,
    base_hourly_analyzer: Path,
    base_calibration_analyzer: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "analyzer_sha256": sha256_path(script),
        "downloader_sha256": sha256_path(downloader),
        "base_downloader_sha256": sha256_path(base_downloader),
        "base_hourly_analyzer_sha256": sha256_path(
            base_hourly_analyzer
        ),
        "base_calibration_analyzer_sha256": sha256_path(
            base_calibration_analyzer
        ),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    expected_bases = {
        "base_downloader_sha256": BASE_DOWNLOADER_SHA256,
        "base_hourly_analyzer_sha256": BASE_HOURLY_ANALYZER_SHA256,
        "base_calibration_analyzer_sha256": (
            BASE_CALIBRATION_ANALYZER_SHA256
        ),
    }
    for key, expected in expected_bases.items():
        if observed[key] != expected:
            raise RuntimeError(f"embedded base hash mismatch: {key}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    official = specification.get("official_contract") or {}
    if (
        official.get("projection_rule")
        != "Select a historical row if and only if time_ms modulo 1000 equals zero. Never select every fifth row or use row position."
    ):
        raise RuntimeError("pre-registered projection rule mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def parse_hour_boundary(
    body: bytes,
    hour_start: str,
    hourly_base: Any,
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    envelope = json.loads(body)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("invalid/error Kalshi CF passthrough envelope")
    payload = data.get("payload")
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("raw historical payload is empty/non-list")
    start_ms = hourly_base.utc_ms(hour_start)
    end_ms = start_ms + 3_600_000
    previous_time = None
    projected: list[tuple[int, float]] = []
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(f"payload row {row_number} has invalid time")
        source_ms = int(raw_time)
        if not start_ms <= source_ms < end_ms:
            raise RuntimeError(f"payload row {row_number} outside hour")
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("payload reaches hard-banned start")
        if previous_time is not None and source_ms <= previous_time:
            raise RuntimeError("raw times duplicate/non-ascending")
        if source_ms % 200 != 0:
            raise RuntimeError("raw source time is off 200ms grid")
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"payload row {row_number} has invalid value"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"payload row {row_number} has nonpositive value"
            )
        if source_ms % 1000 == 0:
            projected.append((source_ms, value))
        previous_time = source_ms
    expected_times = [
        start_ms + second * 1000 for second in range(3_600)
    ]
    if [row[0] for row in projected] != expected_times:
        raise RuntimeError("one-second boundary projection is not exact")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return projected, {
        "hour_start_utc": hour_start,
        "row_count": len(payload),
        "projected_second_count": len(projected),
        "minimum_source_ms": projected[0][0],
        "maximum_source_ms": projected[-1][0],
        "payload_sha256": sha256_bytes(canonical),
        "raw_times_strictly_ascending_unique": True,
        "raw_times_on_200ms_grid": True,
        "projection_predicate": "time_ms % 1000 == 0",
        "projected_exact_second_grid": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--downloader", required=True)
    parser.add_argument("--base-downloader", required=True)
    parser.add_argument("--base-hourly-analyzer", required=True)
    parser.add_argument("--base-calibration-analyzer", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--acquisition-receipt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    downloader = Path(args.downloader).resolve()
    base_downloader = Path(args.base_downloader).resolve()
    base_hourly_analyzer_path = Path(
        args.base_hourly_analyzer
    ).resolve()
    base_calibration_analyzer_path = Path(
        args.base_calibration_analyzer
    ).resolve()
    source_dir = Path(args.source_dir).resolve()
    acquisition_receipt = Path(args.acquisition_receipt).resolve()
    output_path = Path(args.out).resolve()
    markdown_path = Path(args.md_out).resolve()

    seal_info = verify_seal(
        script,
        downloader,
        base_downloader,
        base_hourly_analyzer_path,
        base_calibration_analyzer_path,
        prereg,
        seal,
    )
    hourly_base = load_module(
        base_hourly_analyzer_path,
        "sealed_v2_hourly_analyzer_base",
    )
    calibration_base = load_module(
        base_calibration_analyzer_path,
        "sealed_calibration_analyzer_base",
    )
    hourly_base.parse_hour = lambda body, hour_start: parse_hour_boundary(
        body, hour_start, hourly_base
    )
    times, values, history_audit = hourly_base.load_hourly_history(
        source_dir,
        acquisition_receipt,
        seal_info,
        calibration_base,
    )
    history_audit.update({
        "raw_resolution": "PER_200MS_MAXIMUM",
        "model_projection": "time_ms % 1000 == 0",
        "projected_tick_count": len(times),
        "row_position_sampling_used": False,
    })
    metadata, metadata_audit = calibration_base.load_metadata()
    verified_metadata, settlement_audit = (
        calibration_base.settlement_crosscheck(
            metadata, times, values
        )
    )
    book_paths = calibration_base.exact_book_paths()
    book_samples, book_replay = calibration_base.load_book_samples(
        book_paths, verified_metadata
    )
    rows, excluded = calibration_base.build_calibration_rows(
        verified_metadata,
        times,
        values,
        book_samples,
    )
    metrics = calibration_base.calibration_report(rows)
    status = (
        "DISCOVERY_RESULT"
        if metrics["minimum_reportable_satisfied"]
        else "DESCRIPTIVE_ONLY_INSUFFICIENT_MARKETS_OR_OUTCOMES"
    )
    report = {
        "schema": "brti-authoritative-calibration-v3-boundary",
        "status": status,
        "claim": "DISCOVERY_ONLY_NOT_VALIDATION",
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "hard_banned_history_requested_or_opened": False,
        "seal": seal_info,
        "sources": {
            "authoritative_brti": history_audit,
            "market_metadata": metadata_audit,
            "orderbooks": calibration_base.file_manifest(
                path
                for date in calibration_base.ALLOWED_DATES
                for path in book_paths[date]
            ),
            "book_paths_by_date": book_paths,
            "book_replay": book_replay,
        },
        "settlement_crosscheck": settlement_audit,
        "calibration": metrics,
        "calibration_exclusions": excluded,
        "rows": rows,
        "blocked_secondary_tests": {
            "fair_mid_gap_to_future_mid": {
                "status": "BLOCKED_BRTI_RECEIVE_TIMESTAMP_ABSENT",
                "reason": (
                    "Historical CF rows have source time/value but not their "
                    "original Kalshi receipt clock; source time cannot be "
                    "mixed with book receive time for causal markouts."
                ),
            },
            "p3_first_fill_oof": {
                "status": "BLOCKED_EXISTING_P3_CLOCK_AND_SCHEMA",
                "reason": (
                    "Existing P3 episodes use the superseded ts_utc/ws_seq "
                    "replay and omit first_side/first_price."
                ),
            },
        },
        "interpretation_boundary": {
            "calibration": (
                "Brier, log loss, calibration intercept/slope, and the "
                "paired market-mid comparison test direction pricing."
            ),
            "execution_completion": (
                "Queue position, fill toxicity, opposite-leg completion, "
                "fees, forced exits, and realized PnL are not identified."
            ),
            "deployment_authorized": False,
        },
    }
    report = calibration_base.rounded(report)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        calibration_base.render_markdown(report), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
