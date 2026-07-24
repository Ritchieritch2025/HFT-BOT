from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.research.pnl_spine.terminal_lineage_bridge import (
    COVERAGE_POLICY,
    EXPLICIT_PER_SHARD,
    SINGLE_VERSION,
    SPEC_SCHEMA,
    TEMPORALITY,
    TerminalLineageBridgeError,
    _read_pinned_file,
    build_terminal_root_bundle,
    canonical_json_bytes,
    canonical_sha256,
)


H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64


def _write_json(path: Path, value: object) -> str:
    raw = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o444)
    return hashlib.sha256(raw).hexdigest()


def _adapter_config() -> dict[str, Any]:
    return {
        "schema_version": "test-adapter-config-v1",
        "authentication": "FORBIDDEN",
        "proxy": "FORBIDDEN",
        "redirect": "FORBIDDEN",
        "terminal_rule": "FINALIZED_YES_NO_EXACT_PAYOUT_ONLY",
        "metadata_temporality": TEMPORALITY,
        "scheduled_start_mapping": "FORBIDDEN",
    }


def _metadata(
    ticker: str,
    *,
    raw_sha: str,
    wall_before: int,
    wall_after: int,
    mono_before: int,
    mono_after: int,
) -> dict[str, Any]:
    return {
        "market_ticker": ticker,
        "source_tier": "current",
        "request_url": (
            "https://api.elections.kalshi.com/trade-api/v2/markets/"
            f"{ticker}"
        ),
        "raw_response_sha256": raw_sha,
        "observed_wall_ns_before": wall_before,
        "observed_wall_ns_after": wall_after,
        "observed_monotonic_ns_before": mono_before,
        "observed_monotonic_ns_after": mono_after,
        "market_status": "finalized",
        "official_result": "yes",
        "yes_settlement_value_e4": 10_000,
        "settlement_ts": "2026-07-23T00:00:00Z",
        "price_level_structure": "linear_cent",
        "price_ranges": [
            {"start": "0.0000", "end": "1.0000", "step": "0.0100"}
        ],
        "tick_size_e4": 100,
        "open_time": "2026-07-01T00:00:00Z",
        "close_time": "2026-07-22T00:00:00Z",
        "expected_expiration_time": "2026-07-22T00:00:00Z",
        "occurrence_datetime": "2026-07-21T00:00:00Z",
        "metadata_asof_semantics": TEMPORALITY,
        "historical_asof_utc": None,
        "scheduled_start_ts_ns": None,
        "scheduled_start_field_mapping": (
            "BLOCKED_NO_AUTHORIZED_SCHEDULED_START_MAPPING"
        ),
        "point_in_time_lifecycle_interval": None,
        "point_in_time_tick_interval": None,
    }


def _make_shard(
    root: Path,
    *,
    shard_id: str,
    tickers: list[str],
    code_sha: str,
    sequence: int,
) -> dict[str, Any]:
    shard = root / shard_id
    shard.mkdir(parents=True)
    config = _adapter_config()
    config_sha = canonical_sha256(config)
    authority = {
        "schema_version": "a01-official-market-authority-v1",
        "authority_id": f"authority-{shard_id}",
        "purpose": "READ_ONLY_OFFICIAL_MARKET_TERMINAL_METADATA",
        "issued_at_utc": "2026-07-23T00:00:00Z",
        "expires_at_utc": "2026-07-24T00:00:00Z",
        "tickers": tickers,
        "adapter_code_sha256": code_sha,
        "adapter_config_sha256": config_sha,
    }
    authority_path = shard / "AUTHORITY.json"
    authority_sha = _write_json(authority_path, authority)

    captures: list[dict[str, Any]] = []
    selected_responses: list[dict[str, Any]] = []
    terminal_records: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    raw_evidence: list[dict[str, Any]] = []
    for index, ticker in enumerate(tickers):
        raw_name = f"{index:04d}.{index:04d}.current.response.json"
        raw_path = shard / raw_name
        raw = canonical_json_bytes(
            {
                "market": {
                    "ticker": ticker,
                    "status": "finalized",
                    "result": "yes",
                }
            }
        )
        raw_path.write_bytes(raw)
        raw_path.chmod(0o444)
        raw_sha = hashlib.sha256(raw).hexdigest()
        wall_before = 1_700_000_000_000_000_000 + sequence * 1_000 + index * 10
        wall_after = wall_before + 5
        mono_before = 10_000 + sequence * 1_000 + index * 10
        mono_after = mono_before + 5
        attempt = {
            "ticker": ticker,
            "source_tier": "current",
            "endpoint_attempt_index": 0,
            "batch_attempt_index": index,
            "request_url": (
                "https://api.elections.kalshi.com/trade-api/v2/markets/"
                f"{ticker}"
            ),
            "http_status": 200,
            "required_delay_from_prior_attempt_ns": (
                0 if index == 0 else 250_000_000
            ),
            "retry_after_seconds": None,
            "fetch_wall_ns_before": wall_before,
            "fetch_wall_ns_after": wall_after,
            "fetch_monotonic_ns_before": mono_before,
            "fetch_monotonic_ns_after": mono_after,
            "raw_relative_path": raw_name,
            "raw_size": len(raw),
            "raw_sha256": raw_sha,
            "adapter_code_sha256": code_sha,
            "adapter_config_sha256": config_sha,
        }
        captures.append(
            {
                "ticker": ticker,
                "attempts": [attempt],
                "selected_attempt_index": 0,
            }
        )
        selected_responses.append(
            {
                key: attempt[key]
                for key in (
                    "ticker",
                    "source_tier",
                    "request_url",
                    "http_status",
                    "raw_size",
                    "raw_sha256",
                )
            }
        )
        terminal_records.append(
            {
                "settlement_id": (
                    f"kalshi-official:{ticker}:2026-07-23T00:00:00Z:"
                    f"{raw_sha[:16]}"
                ),
                "market_ticker": ticker,
                "status": "FINALIZED",
                "finalized": True,
                "yes_settlement_value_e4": 10_000,
                "observed_at_ns": wall_after,
                "revision": 0,
                "source_sha256": raw_sha,
            }
        )
        metadata_rows.append(
            _metadata(
                ticker,
                raw_sha=raw_sha,
                wall_before=wall_before,
                wall_after=wall_after,
                mono_before=mono_before,
                mono_after=mono_after,
            )
        )
        raw_evidence.append(
            {**attempt, "raw_reverified_sha256": raw_sha}
        )

    capture = {
        "schema_version": "a01-official-market-capture-receipt-v1",
        "authority_raw_sha256": authority_sha,
        "authority_id": authority["authority_id"],
        "authority_issued_at_utc": authority["issued_at_utc"],
        "authority_expires_at_utc": authority["expires_at_utc"],
        "authority_issued_wall_ns": 1,
        "authority_expires_wall_ns": 2,
        "adapter_code_sha256": code_sha,
        "adapter_config_sha256": config_sha,
        "adapter_config": config,
        "request_authentication": "NONE",
        "proxy_policy": "DISABLED",
        "redirect_policy": "REJECT",
        "temporality": TEMPORALITY,
        "captures": captures,
    }
    capture_path = shard / "CAPTURE_RECEIPT.json"
    capture_sha = _write_json(capture_path, capture)
    pins = {
        "schema_version": "a01-official-market-raw-pins-v1",
        "authority_raw_sha256": authority_sha,
        "capture_receipt_raw_sha256": capture_sha,
        "adapter_code_sha256": code_sha,
        "adapter_config_sha256": config_sha,
        "selected_responses": selected_responses,
    }
    pins_path = shard / "RAW_PINS.json"
    pins_sha = _write_json(pins_path, pins)
    normalized = {
        "schema_version": "a01-official-market-terminal-metadata-v1",
        "authority_raw_sha256": authority_sha,
        "capture_receipt_raw_sha256": capture_sha,
        "raw_pins_raw_sha256": pins_sha,
        "adapter_code_sha256": code_sha,
        "adapter_config_sha256": config_sha,
        "terminal_records": terminal_records,
        "metadata_evidence": metadata_rows,
        "raw_response_evidence": raw_evidence,
        "resolved_capabilities": [
            "OFFICIAL_FINALIZED_YES_NO_RESULT",
            "EXACT_YES_PAYOUT_E4",
            "OFFICIAL_SETTLEMENT_TIMESTAMP",
            "TERMINAL_OBSERVATION_CLOCK_BRACKET",
            "TERMINAL_OBSERVED_TICK_TABLE",
            "TERMINAL_OBSERVED_LIFECYCLE_TIMESTAMPS",
        ],
        "remaining_blockers": [
            "BLOCK_A01_HISTORICAL_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
            "BLOCK_A01_SCHEDULED_START_AUTHORITY_MISSING",
            "BLOCK_A01_EXCHANGE_SETTLEMENT_REVISION_SEQUENCE_UNAVAILABLE",
        ],
        "prohibitions": [
            (
                "occurrence_datetime, expected_expiration_time and "
                "close_time are not scheduled_start"
            ),
            (
                "fetch observation time is not a historical metadata "
                "as-of timestamp"
            ),
        ],
    }
    normalized_path = shard / "NORMALIZED.json"
    normalized_sha = _write_json(normalized_path, normalized)
    return {
        "shard_id": shard_id,
        "authority_path": str(authority_path),
        "authority_raw_sha256": authority_sha,
        "capture_receipt_path": str(capture_path),
        "capture_receipt_raw_sha256": capture_sha,
        "raw_pins_path": str(pins_path),
        "raw_pins_raw_sha256": pins_sha,
        "normalized_output_path": str(normalized_path),
        "normalized_output_raw_sha256": normalized_sha,
        "adapter_code_sha256": code_sha,
        "adapter_config_sha256": config_sha,
    }


def _case(tmp_path: Path) -> dict[str, Any]:
    first = _make_shard(
        tmp_path,
        shard_id="batch-00",
        tickers=["KX-A", "KX-B"],
        code_sha=H1,
        sequence=0,
    )
    second = _make_shard(
        tmp_path,
        shard_id="batch-01",
        tickers=["KX-C"],
        code_sha=H1,
        sequence=1,
    )
    return {
        "schema_version": SPEC_SCHEMA,
        "temporality": TEMPORALITY,
        "coverage_policy": COVERAGE_POLICY,
        "required_tickers": ["KX-A", "KX-B", "KX-C"],
        "adapter_version_policy": {
            "mode": SINGLE_VERSION,
            "approved_shards": [],
        },
        "shards": [first, second],
        "eligibility_exclusions": [],
    }


def _add_pga_exclusion(
    spec: dict[str, Any],
    root: Path,
    *,
    ticker: str = "KX-PGA",
) -> None:
    shard = spec["shards"][0]
    authority = {
        "schema_version": "a01-official-market-authority-v1",
        "authority_id": "authority-pga",
        "purpose": "READ_ONLY_OFFICIAL_MARKET_TERMINAL_METADATA",
        "issued_at_utc": "2026-07-23T00:00:00Z",
        "expires_at_utc": "2026-07-24T00:00:00Z",
        "tickers": [ticker],
        "adapter_code_sha256": shard["adapter_code_sha256"],
        "adapter_config_sha256": shard["adapter_config_sha256"],
    }
    authority_path = root / "pga" / "AUTHORITY.json"
    authority_sha = _write_json(authority_path, authority)
    raw_path = root / "pga" / "0000.0000.current.response.json"
    raw = json.dumps(
        {
            "market": {
                "ticker": ticker,
                "status": "finalized",
                "result": "no",
                "settlement_value_dollars": "0.0000",
                "price_level_structure": "tapered_deci_cent",
                "floor_strike": 67.5,
            }
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw_path.write_bytes(raw)
    raw_path.chmod(0o444)
    spec["required_tickers"] = sorted(
        [*spec["required_tickers"], ticker]
    )
    spec["eligibility_exclusions"] = [
        {
            "ticker": ticker,
            "reason_code": "NON_STANDARD_PRICE_LEVEL_STRUCTURE",
            "authority_path": str(authority_path),
            "authority_raw_sha256": authority_sha,
            "raw_response_path": str(raw_path),
            "raw_response_raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_response_size_bytes": len(raw),
            "adapter_code_sha256": shard["adapter_code_sha256"],
            "adapter_config_sha256": shard[
                "adapter_config_sha256"
            ],
            "source_tier": "current",
            "http_status": 200,
            "observed_price_level_structure": "tapered_deci_cent",
        }
    ]


def _rewrite(path: str, value: object) -> str:
    target = Path(path)
    target.chmod(0o644)
    return _write_json(target, value)


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def test_builds_exact_observation_only_terminal_root(tmp_path: Path) -> None:
    spec = _case(tmp_path)
    bundle = build_terminal_root_bundle(
        spec, require_root_read_only=False
    )

    assert sorted(bundle.terminal_records_by_ticker) == [
        "KX-A",
        "KX-B",
        "KX-C",
    ]
    receipt = bundle.receipt
    assert receipt["required_tickers"] == ["KX-A", "KX-B", "KX-C"]
    assert (
        receipt["historical_point_in_time_metadata_satisfied"] is False
    )
    assert receipt["scheduled_start_authority_satisfied"] is False
    assert receipt["historical_lifecycle_intervals_satisfied"] is False
    assert receipt["s3_writes"] == 0
    payload = dict(receipt)
    assert payload.pop("payload_sha256") == canonical_sha256(payload)


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_exact_ticker_coverage_rejects_missing_extra_and_duplicate(
    tmp_path: Path,
    mutation: str,
) -> None:
    spec = _case(tmp_path)
    if mutation == "missing":
        spec["required_tickers"].remove("KX-C")
    elif mutation == "extra":
        spec["required_tickers"].append("KX-D")
    else:
        spec["required_tickers"].insert(1, "KX-A")
    with pytest.raises(TerminalLineageBridgeError):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_tampered_raw_response_fails_closed(tmp_path: Path) -> None:
    spec = _case(tmp_path)
    capture = _load(spec["shards"][0]["capture_receipt_path"])
    raw_name = capture["captures"][0]["attempts"][0]["raw_relative_path"]
    raw_path = (
        Path(spec["shards"][0]["capture_receipt_path"]).parent / raw_name
    )
    raw_path.chmod(0o644)
    raw_path.write_bytes(b"tampered")

    with pytest.raises(
        TerminalLineageBridgeError, match="raw SHA-256 mismatch"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_mixed_adapter_versions_need_exact_per_shard_approval(
    tmp_path: Path,
) -> None:
    spec = _case(tmp_path)
    replacement = _make_shard(
        tmp_path / "replacement",
        shard_id="batch-01",
        tickers=["KX-C"],
        code_sha=H2,
        sequence=2,
    )
    spec["shards"][1] = replacement
    with pytest.raises(
        TerminalLineageBridgeError, match="mixed adapter versions"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )

    spec["adapter_version_policy"] = {
        "mode": EXPLICIT_PER_SHARD,
        "approved_shards": [
            {
                "shard_id": shard["shard_id"],
                "adapter_code_sha256": shard["adapter_code_sha256"],
                "adapter_config_sha256": shard[
                    "adapter_config_sha256"
                ],
            }
            for shard in spec["shards"]
        ],
    }
    bundle = build_terminal_root_bundle(
        spec, require_root_read_only=False
    )
    assert bundle.receipt["adapter_version_policy"]["mode"] == (
        EXPLICIT_PER_SHARD
    )


def test_explicit_mixed_version_approval_cannot_be_forged_or_omitted(
    tmp_path: Path,
) -> None:
    spec = _case(tmp_path)
    spec["adapter_version_policy"] = {
        "mode": EXPLICIT_PER_SHARD,
        "approved_shards": [
            {
                "shard_id": "batch-00",
                "adapter_code_sha256": H0,
                "adapter_config_sha256": spec["shards"][0][
                    "adapter_config_sha256"
                ],
            },
            {
                "shard_id": "batch-01",
                "adapter_code_sha256": H1,
                "adapter_config_sha256": spec["shards"][1][
                    "adapter_config_sha256"
                ],
            },
        ],
    }
    with pytest.raises(
        TerminalLineageBridgeError, match="do not exactly match"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_zero_coverage_fails_closed(tmp_path: Path) -> None:
    spec = _case(tmp_path)
    spec["required_tickers"] = []
    spec["shards"] = []
    with pytest.raises(
        TerminalLineageBridgeError, match="zero coverage"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_ineligible_terminal_is_explicit_denominator_not_economic_row(
    tmp_path: Path,
) -> None:
    spec = _case(tmp_path)
    _add_pga_exclusion(spec, tmp_path)

    bundle = build_terminal_root_bundle(
        spec, require_root_read_only=False
    )

    assert bundle.receipt["required_tickers"] == [
        "KX-A",
        "KX-B",
        "KX-C",
        "KX-PGA",
    ]
    assert bundle.receipt["eligible_tickers"] == [
        "KX-A",
        "KX-B",
        "KX-C",
    ]
    assert "KX-PGA" not in bundle.terminal_records_by_ticker
    exclusion = bundle.receipt["eligibility_exclusions"][0]
    assert exclusion["ticker"] == "KX-PGA"
    assert exclusion["reason_code"] == (
        "NON_STANDARD_PRICE_LEVEL_STRUCTURE"
    )
    assert exclusion["economic_record_admitted"] is False
    assert exclusion["terminal_fact"]["yes_settlement_value_e4"] == 0


def test_required_ineligible_ticker_without_root_exclusion_is_missing(
    tmp_path: Path,
) -> None:
    spec = _case(tmp_path)
    spec["required_tickers"].append("KX-PGA")
    with pytest.raises(
        TerminalLineageBridgeError, match="terminal coverage is not exact"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_historical_gate_claims_are_rejected(tmp_path: Path) -> None:
    spec = _case(tmp_path)
    shard = spec["shards"][0]
    output = _load(shard["normalized_output_path"])
    output["metadata_evidence"][0]["scheduled_start_ts_ns"] = 123
    shard["normalized_output_raw_sha256"] = _rewrite(
        shard["normalized_output_path"], output
    )
    with pytest.raises(
        TerminalLineageBridgeError,
        match="cannot supply scheduled_start",
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )


def test_wrong_owner_mode_and_symlink_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "control.json"
    raw = b"{}\n"
    target.write_bytes(raw)
    target.chmod(0o444)
    digest = hashlib.sha256(raw).hexdigest()

    real_fstat = os.fstat

    def nonroot_fstat(fd: int) -> SimpleNamespace:
        value = real_fstat(fd)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_size=value.st_size,
            st_uid=1234,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_mtime_ns=value.st_mtime_ns,
        )

    monkeypatch.setattr(
        "tools.research.pnl_spine.terminal_lineage_bridge.os.fstat",
        nonroot_fstat,
    )
    with pytest.raises(TerminalLineageBridgeError, match="root-owned"):
        _read_pinned_file(
            str(target),
            digest,
            label="control",
            maximum_bytes=1024,
            require_root_read_only=True,
        )
    monkeypatch.undo()

    real_fstat_again = os.fstat

    def writable_root_fstat(fd: int) -> SimpleNamespace:
        value = real_fstat_again(fd)
        return SimpleNamespace(
            st_mode=value.st_mode | 0o200,
            st_size=value.st_size,
            st_uid=0,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_mtime_ns=value.st_mtime_ns,
        )

    monkeypatch.setattr(
        "tools.research.pnl_spine.terminal_lineage_bridge.os.fstat",
        writable_root_fstat,
    )
    with pytest.raises(TerminalLineageBridgeError, match="no write bits"):
        _read_pinned_file(
            str(target),
            digest,
            label="control",
            maximum_bytes=1024,
            require_root_read_only=True,
        )
    monkeypatch.undo()

    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(TerminalLineageBridgeError, match="symlink"):
        _read_pinned_file(
            str(linked),
            digest,
            label="control",
            maximum_bytes=1024,
            require_root_read_only=False,
        )


def test_normalized_output_tamper_is_pinned(tmp_path: Path) -> None:
    spec = _case(tmp_path)
    path = Path(spec["shards"][0]["normalized_output_path"])
    path.chmod(0o644)
    value = _load(str(path))
    value["terminal_records"][0]["yes_settlement_value_e4"] = 0
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    with pytest.raises(
        TerminalLineageBridgeError, match="raw SHA-256 mismatch"
    ):
        build_terminal_root_bundle(
            spec, require_root_read_only=False
        )
