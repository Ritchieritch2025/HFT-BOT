"""Tests for the V3.1 commit-authority sealing boundary."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from tests.test_round4_stage2_postfill_extractor_v3 import (
    _ready_dataset,
    _sha,
)
from tools.research.crypto_mm import (
    round4_stage2_postfill_extractor as V1,
)
from tools.research.crypto_mm import (
    round4_stage2_postfill_extractor_v3 as V3,
)
from tools.research.crypto_mm.round4_stage2_postfill_extractor_v3_1 import (
    CommitVerifiedResultV31,
    Stage2CommitV31Error,
    _issue_externally_sealed_config_v31,
    build_authoritative_commit_receipt_v31,
    build_dry_run_receipt_v31,
    commit_verify_source_v31,
)


def _sealed_config(authority, manifest, root: Path):
    return _issue_externally_sealed_config_v31(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
        upstream_builder_receipt_pins={
            day: _sha(f"builder-receipt:{day}".encode())
            for day in V1.DISCOVERY_DATES
        },
        external_seal_receipt_sha256=_sha(b"external-config-seal"),
    )


def _recompute_common_v3_roots(
    result: V3.AuthoritativeSourceResultV3,
    *,
    rows: tuple[V3.AuthoritativeStateRowV3, ...],
    parents: tuple[V3.ParentEOFReadV3, ...] | None = None,
) -> V3.AuthoritativeSourceResultV3:
    event_spines = V3._event_spines_by_parent(rows)
    source_parents = parents or result.parent_reads
    rewritten_parents = tuple(
        replace(
            parent,
            normalized_event_spine_sha256=V1.canonical_sha256(
                list(
                    event_spines[
                        (parent.source_date_utc, parent.table)
                    ]
                )
            ),
        )
        for parent in source_parents
    )
    parent_spine = V1.canonical_sha256(
        [item.receipt() for item in rewritten_parents]
    )
    state_spine = V1.canonical_sha256(
        [item.receipt() for item in rows]
    )
    coverage = V3._coverage_from_state_rows(rows)
    coverage_sha = V1.canonical_sha256(
        [item.receipt() for item in coverage]
    )
    _terminal_rows, terminal_sha = V3._terminal_l2_receipt_rows(rows)
    source_event_count = sum(item.event_count for item in rows)
    root = V3._transformation_root(
        content_root=result.content_root,
        authority_sha256=result.authority_sha256,
        bound_manifest_sha256=result.bound_manifest_sha256,
        transform_code_sha256=result.transform_code_sha256,
        parent_spine_sha256=parent_spine,
        state_spine_sha256=state_spine,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_sha,
        source_event_count=source_event_count,
    )
    return replace(
        result,
        parent_reads=rewritten_parents,
        parent_spine_sha256=parent_spine,
        state_rows=rows,
        state_row_count=len(rows),
        source_event_count=source_event_count,
        state_spine_sha256=state_spine,
        market_coverage=coverage,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_sha,
        transformation_root_sha256=root,
    )


def _forge_cross_ticker_event_ref_swap(
    result: V3.AuthoritativeSourceResultV3,
) -> V3.AuthoritativeSourceResultV3:
    rows = list(result.state_rows)
    candidates = [
        index
        for index, row in enumerate(rows)
        if row.source_date_utc == V1.DISCOVERY_DATES[0]
        and row.event_kinds == ("BOOK_SNAPSHOT",)
    ]
    left, right = candidates[:2]
    left_refs = rows[left].source_events
    right_refs = rows[right].source_events
    rows[left] = replace(rows[left], source_events=right_refs)
    rows[right] = replace(rows[right], source_events=left_refs)
    return _recompute_common_v3_roots(result, rows=tuple(rows))


def _forge_deleted_delta_and_compressed_ordinals(
    result: V3.AuthoritativeSourceResultV3,
) -> V3.AuthoritativeSourceResultV3:
    removed_index = next(
        index
        for index, row in enumerate(result.state_rows)
        if row.event_kinds == ("BOOK_DELTA",)
    )
    removed = result.state_rows[removed_index]
    removed_ordinal = removed.source_events[0].source_ordinal
    removed_parent_key = (
        removed.source_date_utc,
        "orderbooks_full",
    )
    rows: list[V3.AuthoritativeStateRowV3] = []
    for row in (
        *result.state_rows[:removed_index],
        *result.state_rows[removed_index + 1 :],
    ):
        refs = tuple(
            replace(
                event,
                source_ordinal=event.source_ordinal - 1,
            )
            if event.kind != "TRADE"
            and event.source_ordinal > removed_ordinal
            else event
            for event in row.source_events
        )
        rows.append(
            replace(
                row,
                state_index=len(rows),
                source_events=refs,
            )
        )
    parents = tuple(
        replace(
            parent,
            roster_selected_row_count=(
                parent.roster_selected_row_count - 1
            ),
            nonselected_row_count=parent.nonselected_row_count + 1,
        )
        if (parent.source_date_utc, parent.table) == removed_parent_key
        else parent
        for parent in result.parent_reads
    )
    return _recompute_common_v3_roots(
        result,
        rows=tuple(rows),
        parents=parents,
    )


def test_v31_commit_result_is_only_authoritative_receipt_input(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    sealed = _sealed_config(authority, manifest, root)
    committed = commit_verify_source_v31(
        candidate_result=candidate,
        authority=authority,
        input_manifest=manifest,
        content_root=root,
        sealed_config=sealed,
    )
    assert type(committed) is CommitVerifiedResultV31
    assert committed.commit_verified is True
    assert len(committed.parent_event_spines) == 6
    with pytest.raises(FrozenInstanceError):
        committed.commit_verified = False  # type: ignore[misc]

    receipt = build_authoritative_commit_receipt_v31(committed)
    assert receipt["status"] == "COMMIT_VERIFIED_SOURCE_TRANSFORM_ONLY"
    assert receipt["authoritative_commit_receipt"] is True
    assert receipt["daily_builder_receipt_pin_count"] == 3
    assert [
        item["source_date_utc"]
        for item in receipt["sealed_config"][
            "daily_builder_receipt_pins"
        ]
    ] == list(V1.DISCOVERY_DATES)
    assert receipt["contract_adapter_status"] == "V4_2_PENDING"
    assert receipt["extraction_authorized"] is False
    assert receipt["shadow_authorized"] is False
    assert receipt["live_authorized"] is False
    first_event = receipt["parent_event_spines"][0]["events"][0]
    for field in (
        "market_ticker",
        "recv_mono_ns",
        "recv_wall_ns",
        "kind",
        "stable_source_id",
        "source_ordinal",
        "raw_row_sha256",
        "normalized_payload_canonical_json",
        "atomic_envelope_sha256",
    ):
        assert field in first_event
    assert _sha(
        first_event["normalized_payload_canonical_json"].encode()
    ) == first_event["normalized_source_rows_sha256"]

    with pytest.raises(
        Stage2CommitV31Error,
        match="COMMIT_VERIFIED_RESULT_REQUIRED",
    ):
        build_authoritative_commit_receipt_v31(  # type: ignore[arg-type]
            candidate
        )
    with pytest.raises(
        Stage2CommitV31Error,
        match="REGISTERED_COMMIT_RESULT_REQUIRED",
    ):
        build_authoritative_commit_receipt_v31(
            replace(committed, commit_verified=False)
        )


def test_v31_dry_run_is_explicitly_non_authoritative(tmp_path: Path):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    dry_run = build_dry_run_receipt_v31(candidate)
    assert dry_run["status"] == "DRY_RUN_NON_AUTHORITY"
    assert dry_run["authoritative_commit_receipt"] is False
    assert dry_run["commit_eligible"] is False
    with pytest.raises(
        Stage2CommitV31Error,
        match="COMMIT_VERIFIED_RESULT_REQUIRED",
    ):
        build_authoritative_commit_receipt_v31(  # type: ignore[arg-type]
            dry_run
        )


def test_v31_rejects_cross_ticker_event_ref_swap_even_with_recomputed_roots(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    forged = _forge_cross_ticker_event_ref_swap(candidate)
    # Demonstrates the V3 self-derived public receipt gap being sealed.
    assert V3.build_authoritative_source_receipt_v3(forged)[
        "payload_sha256"
    ]
    with pytest.raises(
        Stage2CommitV31Error,
        match="CANDIDATE_REBUILD_MISMATCH",
    ):
        commit_verify_source_v31(
            candidate_result=forged,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            sealed_config=_sealed_config(authority, manifest, root),
        )


def test_v31_rejects_deleted_delta_and_compressed_ordinal_with_new_roots(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    forged = _forge_deleted_delta_and_compressed_ordinals(candidate)
    assert forged.source_event_count == candidate.source_event_count - 1
    assert V3.build_authoritative_source_receipt_v3(forged)[
        "payload_sha256"
    ]
    with pytest.raises(
        Stage2CommitV31Error,
        match="CANDIDATE_REBUILD_MISMATCH",
    ):
        commit_verify_source_v31(
            candidate_result=forged,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            sealed_config=_sealed_config(authority, manifest, root),
        )


def test_v31_rejects_unsealed_or_mismatched_external_config(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    sealed = _sealed_config(authority, manifest, root)
    with pytest.raises(
        Stage2CommitV31Error,
        match="REGISTERED_EXTERNAL_SEALED_CONFIG_REQUIRED",
    ):
        commit_verify_source_v31(
            candidate_result=candidate,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            sealed_config=replace(sealed, _seal_token=None),
        )
    wrong_pin = replace(
        sealed,
        authority_payload_sha256="f" * 64,
    )
    wrong_pin = replace(
        wrong_pin,
        config_payload_sha256=V1.canonical_sha256(
            wrong_pin.receipt_without_payload_hash()
        ),
    )
    with pytest.raises(
        Stage2CommitV31Error,
        match="REGISTERED_EXTERNAL_SEALED_CONFIG_REQUIRED",
    ):
        commit_verify_source_v31(
            candidate_result=candidate,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            sealed_config=wrong_pin,
        )
    with pytest.raises(
        Stage2CommitV31Error,
        match="DAILY_BUILDER_PIN_SET_INVALID",
    ):
        _issue_externally_sealed_config_v31(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            upstream_builder_receipt_pins={
                V1.DISCOVERY_DATES[0]: _sha(b"only-one")
            },
            external_seal_receipt_sha256=_sha(b"external"),
        )


def test_v31_rejects_rehashed_post_issuance_builder_pin_tampering(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    candidate = V3.build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    sealed = _sealed_config(authority, manifest, root)
    forged_pins = (
        replace(
            sealed.daily_builder_receipt_pins[0],
            upstream_builder_receipt_sha256="f" * 64,
        ),
        *sealed.daily_builder_receipt_pins[1:],
    )
    forged = replace(
        sealed,
        daily_builder_receipt_pins=forged_pins,
        daily_builder_receipt_pin_set_sha256=V1.canonical_sha256(
            [item.receipt() for item in forged_pins]
        ),
        external_seal_receipt_sha256="e" * 64,
    )
    forged = replace(
        forged,
        config_payload_sha256=V1.canonical_sha256(
            forged.receipt_without_payload_hash()
        ),
    )
    with pytest.raises(
        Stage2CommitV31Error,
        match="REGISTERED_EXTERNAL_SEALED_CONFIG_REQUIRED",
    ):
        commit_verify_source_v31(
            candidate_result=candidate,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
            sealed_config=forged,
        )
