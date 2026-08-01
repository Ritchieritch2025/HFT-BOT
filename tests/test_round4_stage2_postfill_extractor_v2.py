"""Fail-closed tests for the independent Stage-2 source foundation V2."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path

import pytest

from tools.research.crypto_mm.round4_stage2_postfill_extractor import (
    DISCOVERY_DATES,
    EligibleDay,
    EligibleExactVersionObject,
    SourcePreparationAuthority,
    expected_hourly_roster,
    required_btc_fact_keys,
)
from tools.research.crypto_mm.round4_stage2_postfill_extractor_v2 import (
    CausalPreparationResult,
    Stage2PreparationV2Error,
    build_bound_input_manifest_v2,
    build_source_preparation_receipt_v2,
    prepare_causal_result,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _authority(root: Path) -> SourcePreparationAuthority:
    days = []
    objects = []
    for day in DISCOVERY_DATES:
        day_objects = []
        for table, logical_key in sorted(required_btc_fact_keys(day).items()):
            payload = f"{day}:{table}:exact-parent".encode()
            path = root / logical_key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            obj = EligibleExactVersionObject(
                source_date_utc=day,
                table=table,
                logical_source_key=logical_key,
                bucket="bucket",
                key=f"ec2/{logical_key}",
                version_id=f"version-{day}-{table}",
                sha256=_sha(payload),
                size=len(payload),
                verification_state="EXACT_VERSION_FULL_SHA256",
                version_resolution="SEALED_CURRENT_EXACT",
            )
            day_objects.append(obj)
            objects.append(obj)
        days.append(
            EligibleDay(
                source_date_utc=day,
                daily_status_sha256=_sha(f"status:{day}".encode()),
                durable_index_sha256=_sha(f"index:{day}".encode()),
                tagged_receipt_sha256=_sha(f"tagged:{day}".encode()),
                receipt_set_sha256=_sha(f"receipt:{day}".encode()),
                durable_receipt_set_sha256=_sha(f"durable:{day}".encode()),
                durability_set_sha256=_sha(f"objects:{day}".encode()),
                eligibility_single_writer_audit_sha256=_sha(
                    f"audit:{day}".encode()
                ),
                manifest_version_id=f"manifest-version-{day}",
                manifest_sha256=_sha(f"manifest:{day}".encode()),
                exact_version_objects=tuple(
                    sorted(day_objects, key=lambda item: item.table)
                ),
            )
        )
    return SourcePreparationAuthority(
        source_dates=DISCOVERY_DATES,
        eligible_days=tuple(days),
        exact_version_objects=tuple(
            sorted(
                objects,
                key=lambda item: (
                    item.source_date_utc,
                    item.table,
                ),
            )
        ),
    )


def _replace_object(
    authority: SourcePreparationAuthority,
    index: int,
    **updates: object,
) -> SourcePreparationAuthority:
    objects = list(authority.exact_version_objects)
    objects[index] = replace(objects[index], **updates)
    return replace(authority, exact_version_objects=tuple(objects))


def _snapshot(
    ticker: str,
    *,
    mono: int,
    wall: int,
    sid: int,
    yes_levels: list[list[int]] | None = None,
    no_levels: list[list[int]] | None = None,
) -> dict[str, object]:
    return {
        "market_ticker": ticker,
        "msg_type": "snapshot",
        "side": None,
        "price_e4": None,
        "delta_e4": None,
        "yes_levels": json.dumps(yes_levels or [[3_000, 10_000]]),
        "no_levels": json.dumps(no_levels or [[6_900, 20_000]]),
        "ws_sid": sid,
        "ws_seq": 1,
        "recv_wall_ns": wall,
        "recv_mono_ns": mono,
        "ts_utc": 1,
    }


def _delta(
    ticker: str,
    *,
    mono: int,
    wall: int,
    sid: int,
) -> dict[str, object]:
    return {
        "market_ticker": ticker,
        "msg_type": "delta",
        "side": "yes",
        "price_e4": 2_900,
        "delta_e4": 10_000,
        "yes_levels": None,
        "no_levels": None,
        "ws_sid": sid,
        "ws_seq": 0,
        "recv_wall_ns": wall,
        "recv_mono_ns": mono,
        "ts_utc": 999_999,
    }


def _complete_l2_rows() -> list[dict[str, object]]:
    rows = []
    roster = expected_hourly_roster()
    for index, ticker in enumerate(roster):
        mono = 1_000_000 + index * 100
        wall = 10_000_000 + index * 100
        if index == 0:
            rows.append(
                _delta(
                    ticker,
                    mono=mono - 10,
                    wall=wall - 10,
                    sid=index + 1,
                )
            )
        rows.append(
            _snapshot(
                ticker,
                mono=mono,
                wall=wall,
                sid=index + 1,
            )
        )
    return rows


def test_v2_manifest_binds_six_exact_regular_files_under_nonsymlink_root(
    tmp_path: Path,
):
    root = tmp_path / "content"
    root.mkdir()
    authority = _authority(root)
    manifest = build_bound_input_manifest_v2(
        authority=authority,
        content_root=root,
    )
    assert manifest.content_root == str(root.resolve())
    assert manifest.file_count == 6
    assert len(manifest.parents) == 6
    assert all(parent.regular_file for parent in manifest.parents)
    assert all(parent.symlink_free for parent in manifest.parents)
    assert len(manifest.payload_sha256) == 64


@pytest.mark.parametrize(
    "malicious",
    (
        "/absolute/trades.csv.gz",
        "../escape.csv.gz",
        "warehouse/../escape.csv.gz",
        "warehouse//facts/trades.csv.gz",
        "warehouse/./facts/trades.csv.gz",
        "warehouse/facts/",
    ),
)
def test_v2_rejects_absolute_traversal_and_noncanonical_logical_keys(
    tmp_path: Path,
    malicious: str,
):
    root = tmp_path / "content"
    root.mkdir()
    authority = _authority(root)
    bad = _replace_object(
        authority,
        0,
        logical_source_key=malicious,
    )
    with pytest.raises(
        Stage2PreparationV2Error,
        match="LOGICAL_SOURCE_KEY_INVALID",
    ):
        build_bound_input_manifest_v2(
            authority=bad,
            content_root=root,
        )


def test_v2_rejects_link_inside_root_pointing_outside(
    tmp_path: Path,
):
    root = tmp_path / "content"
    root.mkdir()
    authority = _authority(root)
    victim = authority.exact_version_objects[0]
    victim_path = root / victim.logical_source_key
    outside = tmp_path / "outside.bin"
    outside.write_bytes(victim_path.read_bytes())
    victim_path.unlink()
    victim_path.symlink_to(outside)

    with pytest.raises(Stage2PreparationV2Error, match="SYMLINK_FORBIDDEN"):
        build_bound_input_manifest_v2(
            authority=authority,
            content_root=root,
        )


def test_v2_rejects_symlink_ancestor_even_when_target_remains_inside(
    tmp_path: Path,
):
    root = tmp_path / "content"
    root.mkdir()
    authority = _authority(root)
    original = root / "warehouse"
    moved = root / "warehouse-real"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)

    with pytest.raises(Stage2PreparationV2Error, match="SYMLINK_FORBIDDEN"):
        build_bound_input_manifest_v2(
            authority=authority,
            content_root=root,
        )


def test_v2_rejects_content_root_that_is_itself_a_symlink(
    tmp_path: Path,
):
    real_root = tmp_path / "content-real"
    real_root.mkdir()
    authority = _authority(real_root)
    linked_root = tmp_path / "content-link"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(Stage2PreparationV2Error, match="SYMLINK_FORBIDDEN"):
        build_bound_input_manifest_v2(
            authority=authority,
            content_root=linked_root,
        )


def test_pipeline_returns_frozen_typed_result_and_recomputes_complete_roster():
    result = prepare_causal_result(
        raw_l2_rows=_complete_l2_rows(),
        raw_trade_rows=(),
    )
    assert type(result) is CausalPreparationResult
    assert result.causal_row_count == 73
    assert len(result.market_completeness) == 72
    assert result.market_completeness[0].ignored_pre_snapshot_deltas == 1
    assert 2_900 in result.market_completeness[0].observed_yes_price_e4
    assert len(result.causal_spine_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        result.causal_row_count = 1  # type: ignore[misc]

    receipt = build_source_preparation_receipt_v2(result)
    assert receipt["market_count"] == 72
    assert receipt["markets_per_day"] == {
        "2026-07-20": 24,
        "2026-07-21": 24,
        "2026-07-22": 24,
    }
    assert receipt["causal_row_count"] == 73
    assert receipt["coverage_computed_from_typed_rows"] is True
    assert receipt["real_warehouse_coverage_claimed"] is False
    assert receipt["contract_adapter_status"] == "V4_2_PENDING"
    assert receipt["extraction_authorized"] is False
    assert receipt["terminal_label_firewall"] == {
        "terminal_result_role": "LABEL_ONLY",
        "terminal_result_feature_use": False,
        "terminal_join_requires_sealed_decision_action_spines": True,
    }


def test_receipt_rejects_plain_mapping_and_empty_rows_with_handfilled_coverage():
    good = prepare_causal_result(
        raw_l2_rows=_complete_l2_rows(),
        raw_trade_rows=(),
    )
    fake_mapping = {
        "causal_rows": [],
        "market_completeness": good.market_completeness,
        "causal_row_count": good.causal_row_count,
        "causal_spine_sha256": good.causal_spine_sha256,
    }
    with pytest.raises(Stage2PreparationV2Error, match="TYPED_RESULT_REQUIRED"):
        build_source_preparation_receipt_v2(fake_mapping)  # type: ignore[arg-type]

    empty_rows = replace(
        good,
        causal_rows=(),
    )
    with pytest.raises(
        Stage2PreparationV2Error,
        match="CAUSAL_RESULT_INCONSISTENT",
    ):
        build_source_preparation_receipt_v2(empty_rows)


def test_receipt_recomputes_spine_and_rejects_forged_count_or_hash():
    good = prepare_causal_result(
        raw_l2_rows=_complete_l2_rows(),
        raw_trade_rows=(),
    )
    with pytest.raises(
        Stage2PreparationV2Error,
        match="CAUSAL_RESULT_INCONSISTENT",
    ):
        build_source_preparation_receipt_v2(
            replace(good, causal_row_count=9_999)
        )
    with pytest.raises(
        Stage2PreparationV2Error,
        match="CAUSAL_RESULT_INCONSISTENT",
    ):
        build_source_preparation_receipt_v2(
            replace(good, causal_spine_sha256="f" * 64)
        )


def test_receipt_rejects_typed_result_not_emitted_by_pipeline():
    good = prepare_causal_result(
        raw_l2_rows=_complete_l2_rows(),
        raw_trade_rows=(),
    )
    constructed = replace(good, _factory_token=None)
    with pytest.raises(Stage2PreparationV2Error, match="TYPED_RESULT_REQUIRED"):
        build_source_preparation_receipt_v2(constructed)


def test_receipt_refuses_71_market_pipeline_without_caller_coverage_escape():
    rows = _complete_l2_rows()
    missing = expected_hourly_roster()[-1]
    rows = [row for row in rows if row["market_ticker"] != missing]
    result = prepare_causal_result(
        raw_l2_rows=rows,
        raw_trade_rows=(),
    )
    with pytest.raises(
        Stage2PreparationV2Error,
        match="ROSTER_INCOMPLETE",
    ):
        build_source_preparation_receipt_v2(result)


def test_atomic_envelope_spine_is_input_order_invariant():
    forward = prepare_causal_result(
        raw_l2_rows=_complete_l2_rows(),
        raw_trade_rows=(),
    )
    reverse = prepare_causal_result(
        raw_l2_rows=list(reversed(_complete_l2_rows())),
        raw_trade_rows=(),
    )
    assert forward.causal_spine_sha256 == reverse.causal_spine_sha256
    assert forward.causal_rows == reverse.causal_rows
