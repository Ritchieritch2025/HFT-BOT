"""Tests for the six-parent authoritative Stage-2 source transform V3."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import gzip
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys

import duckdb
import pytest

from tools.research.crypto_mm.round4_stage2_postfill_extractor import (
    DISCOVERY_DATES,
    EligibleDay,
    EligibleExactVersionObject,
    SourcePreparationAuthority,
    MARKET_SOURCE_DAY,
    expected_hourly_roster,
    required_btc_fact_keys,
)
from tools.research.crypto_mm import (
    round4_stage2_postfill_extractor as V1_MODULE,
)
from tools.research.crypto_mm.round4_stage2_postfill_extractor_v2 import (
    BoundInputManifestV2,
    build_bound_input_manifest_v2,
)
from tools.research.crypto_mm.round4_stage2_postfill_extractor_v3 import (
    AuthoritativeSourceResultV3,
    Stage2AuthoritativeV3Error,
    build_authoritative_source_receipt_v3,
    build_authoritative_source_result_v3,
    dry_run_authoritative_source_v3,
    latest_asof_state_v3,
    reverify_authoritative_source_result_v3,
    visible_fok_levels_v3,
)


TRADE_HEADER = (
    "market_ticker,trade_id,yes_price_e4,no_price_e4,count_e4,"
    "taker_side,recv_wall_ns,recv_mono_ns,ts_utc\n"
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _l2_rows_for_day(
    day: str,
    *,
    late_snapshot: bool,
) -> list[tuple[object, ...]]:
    roster = expected_hourly_roster()
    rows: list[tuple[object, ...]] = []
    for global_index, ticker in enumerate(roster):
        if MARKET_SOURCE_DAY[ticker] != day:
            continue
        mono = 1_000_000 + global_index * 1_000
        wall = 10_000_000 + global_index * 1_000
        if late_snapshot and global_index == 0:
            rows.append(
                (
                    ticker,
                    "delta",
                    "yes",
                    2_900,
                    10_000,
                    None,
                    None,
                    global_index + 1,
                    0,
                    wall - 10,
                    mono - 10,
                    999_999,
                )
            )
        rows.append(
            (
                ticker,
                "snapshot",
                None,
                None,
                None,
                json.dumps([[3_000, 10_000]]),
                json.dumps([[6_900, 20_000]]),
                global_index + 1,
                1,
                wall,
                mono,
                1,
            )
        )
        if global_index % 24 == 0:
            rows.append(
                (
                    ticker,
                    "delta",
                    "yes",
                    2_900,
                    10_000,
                    None,
                    None,
                    global_index + 1,
                    2,
                    wall + 200,
                    mono + 200,
                    2,
                )
            )
    return rows


def _write_l2_parent(
    path: Path,
    *,
    day: str,
    empty: bool,
    late_snapshot: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE l2 (
                market_ticker VARCHAR,
                msg_type VARCHAR,
                side VARCHAR,
                price_e4 INTEGER,
                delta_e4 BIGINT,
                yes_levels VARCHAR,
                no_levels VARCHAR,
                ws_sid BIGINT,
                ws_seq BIGINT,
                recv_wall_ns BIGINT,
                recv_mono_ns BIGINT,
                ts_utc BIGINT
            )
            """
        )
        rows = (
            []
            if empty
            else _l2_rows_for_day(
                day,
                late_snapshot=late_snapshot,
            )
        )
        if rows:
            connection.executemany(
                "INSERT INTO l2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        connection.execute("COPY l2 TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        connection.close()


def _write_trade_parent(
    path: Path,
    *,
    day: str,
    empty: bool,
    taker_side: str = "yes",
    atomic_snapshot_tie: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ticker = next(
        item
        for item in expected_hourly_roster()
        if MARKET_SOURCE_DAY[item] == day
    )
    global_index = expected_hourly_roster().index(ticker)
    offset = 0 if atomic_snapshot_tie else 100
    mono = 1_000_000 + global_index * 1_000 + offset
    wall = 10_000_000 + global_index * 1_000 + offset
    body = TRADE_HEADER
    if not empty:
        body += (
            f"{ticker},trade-{day},3000,7000,10000,"
            f"{taker_side},{wall},{mono},999999\n"
        )
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        handle.write(body)


def _write_six_parents(
    root: Path,
    *,
    empty_table: str | None = None,
    late_snapshot: bool = False,
    taker_side: str = "yes",
    atomic_snapshot_tie: bool = False,
) -> None:
    for day in DISCOVERY_DATES:
        keys = required_btc_fact_keys(day)
        _write_l2_parent(
            root / keys["orderbooks_full"],
            day=day,
            empty=empty_table == "orderbooks_full" and day == DISCOVERY_DATES[0],
            late_snapshot=late_snapshot and day == DISCOVERY_DATES[0],
        )
        _write_trade_parent(
            root / keys["trades"],
            day=day,
            empty=empty_table == "trades" and day == DISCOVERY_DATES[0],
            taker_side=taker_side,
            atomic_snapshot_tie=atomic_snapshot_tie,
        )


def _authority_from_files(
    root: Path,
    *,
    version_suffix: str = "a",
) -> SourcePreparationAuthority:
    days: list[EligibleDay] = []
    all_objects: list[EligibleExactVersionObject] = []
    for day in DISCOVERY_DATES:
        day_objects: list[EligibleExactVersionObject] = []
        for table, logical_key in sorted(required_btc_fact_keys(day).items()):
            payload = (root / logical_key).read_bytes()
            item = EligibleExactVersionObject(
                source_date_utc=day,
                table=table,
                logical_source_key=logical_key,
                bucket="bucket",
                key=f"ec2/{logical_key}",
                version_id=f"version-{version_suffix}-{day}-{table}",
                sha256=_sha(payload),
                size=len(payload),
                verification_state="EXACT_VERSION_FULL_SHA256",
                version_resolution="SEALED_CURRENT_EXACT",
            )
            day_objects.append(item)
            all_objects.append(item)
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
                all_objects,
                key=lambda item: (
                    item.source_date_utc,
                    item.table,
                    item.logical_source_key,
                ),
            )
        ),
    )


def _ready_dataset(
    tmp_path: Path,
    *,
    empty_table: str | None = None,
    late_snapshot: bool = False,
    atomic_snapshot_tie: bool = False,
) -> tuple[Path, SourcePreparationAuthority, BoundInputManifestV2]:
    root = tmp_path / "content"
    root.mkdir()
    _write_six_parents(
        root,
        empty_table=empty_table,
        late_snapshot=late_snapshot,
        atomic_snapshot_tie=atomic_snapshot_tie,
    )
    authority = _authority_from_files(root)
    manifest = build_bound_input_manifest_v2(
        authority=authority,
        content_root=root,
    )
    return root, authority, manifest


def test_v3_public_builder_accepts_no_raw_rows_coverage_count_or_hash():
    parameters = inspect.signature(
        build_authoritative_source_result_v3
    ).parameters
    assert tuple(parameters) == (
        "authority",
        "input_manifest",
        "content_root",
    )


def test_v3_reads_six_parents_to_eof_and_emits_recomputable_state(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    assert type(result) is AuthoritativeSourceResultV3
    assert len(result.parent_reads) == 6
    assert all(item.byte_eof_reached for item in result.parent_reads)
    assert all(
        item.parser_input_from_controlled_snapshot
        for item in result.parent_reads
    )
    assert all(
        item.parser_snapshot_postparse_verified
        for item in result.parent_reads
    )
    assert all(
        item.parser_input_sha256 == item.bound_sha256
        for item in result.parent_reads
    )
    assert all(
        item.roster_selected_stream_exhausted
        for item in result.parent_reads
    )
    assert all(item.byte_eof_pass_count == 2 for item in result.parent_reads)
    assert all(
        item.byte_count_per_eof_pass == item.bound_size
        for item in result.parent_reads
    )
    assert all(item.parent_total_row_count > 0 for item in result.parent_reads)
    assert all(
        item.roster_selected_row_count > 0
        for item in result.parent_reads
    )
    assert len({item.transform_code_sha256 for item in result.parent_reads}) == 1
    assert result.state_row_count == 78
    assert len(result.market_coverage) == 72

    first_ticker = expected_hourly_roster()[0]
    initial = latest_asof_state_v3(
        result,
        market_ticker=first_ticker,
        decision_recv_mono_ns=1_000_001,
        decision_recv_wall_ns=10_000_001,
    )
    assert [(item.price_e4, item.qty_e4) for item in initial.yes_levels] == [
        (3_000, 10_000)
    ]
    assert [(item.price_e4, item.qty_e4) for item in initial.no_levels] == [
        (6_900, 20_000)
    ]
    first_source = result.state_rows[0].source_events[0]
    assert first_source.parent_version_id
    assert first_source.parent_sha256
    assert first_source.parent_size > 0
    assert first_source.content_root == str(root.resolve())
    assert [
        (item.price_e4, item.qty_e4)
        for item in visible_fok_levels_v3(
            initial,
            flatten_book_side="ASK",
            limit_price_e4=3_000,
        )
    ] == [(3_000, 10_000)]
    assert [
        (item.price_e4, item.qty_e4)
        for item in visible_fok_levels_v3(
            initial,
            flatten_book_side="BID",
            limit_price_e4=3_100,
        )
    ] == [(3_100, 20_000)]

    strict_before_trade = latest_asof_state_v3(
        result,
        market_ticker=first_ticker,
        decision_recv_mono_ns=1_000_100,
        decision_recv_wall_ns=10_000_100,
    )
    assert strict_before_trade.source_state_index == initial.source_state_index
    asof = latest_asof_state_v3(
        result,
        market_ticker=first_ticker,
        decision_recv_mono_ns=1_000_101,
        decision_recv_wall_ns=10_000_101,
    )
    assert asof.market_ticker == first_ticker
    assert "TRADE" in asof.event_kinds
    after_delta = latest_asof_state_v3(
        result,
        market_ticker=first_ticker,
        decision_recv_mono_ns=1_000_201,
        decision_recv_wall_ns=10_000_201,
    )
    assert [
        (item.price_e4, item.qty_e4)
        for item in visible_fok_levels_v3(
            after_delta,
            flatten_book_side="ASK",
            limit_price_e4=2_900,
        )
    ] == [(3_000, 10_000), (2_900, 10_000)]
    snapshot_payload_count = sum(
        event.kind == "BOOK_SNAPSHOT"
        for row in result.state_rows
        for event in row.book_events
    )
    assert snapshot_payload_count == 72
    with pytest.raises(FrozenInstanceError):
        result.state_row_count = 1  # type: ignore[misc]

    receipt = build_authoritative_source_receipt_v3(result)
    assert receipt["authoritative_source_transform"] is True
    assert receipt["exact_parent_eof_coverage"] is True
    assert receipt["market_count"] == 72
    assert receipt["complete_normalized_replay_spine_exported"] is True
    assert len(receipt["normalized_replay_rows"]) == 78
    assert receipt["terminal_l2_market_count"] == 72
    assert len(receipt["terminal_l2_states"]) == 72
    assert receipt["markets_per_day"] == {
        "2026-07-20": 24,
        "2026-07-21": 24,
        "2026-07-22": 24,
    }
    assert receipt["contract_adapter_status"] == "V4_2_PENDING"
    assert receipt["extraction_authorized"] is False
    assert receipt["shadow_authorized"] is False
    assert receipt["live_authorized"] is False
    commit_receipt = reverify_authoritative_source_result_v3(
        prior_result=result,
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    assert commit_receipt["commit_time_reverified"] is True


def test_v3_duckdb_parses_only_private_controlled_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    observed_paths: list[Path] = []
    original_l2 = V1_MODULE.read_l2_parquet_rows
    original_trades = V1_MODULE.read_trade_csv_rows

    def guarded_l2(path: Path | str, roster: object):
        observed_paths.append(Path(path))
        assert not Path(path).is_relative_to(root)
        return original_l2(path, roster)  # type: ignore[arg-type]

    def guarded_trades(path: Path | str, roster: object):
        observed_paths.append(Path(path))
        assert not Path(path).is_relative_to(root)
        return original_trades(path, roster)  # type: ignore[arg-type]

    monkeypatch.setattr(V1_MODULE, "read_l2_parquet_rows", guarded_l2)
    monkeypatch.setattr(V1_MODULE, "read_trade_csv_rows", guarded_trades)
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    assert result.state_row_count == 78
    assert len(observed_paths) == 6
    assert all(not path.exists() for path in observed_paths)


@pytest.mark.parametrize("empty_table", ("trades", "orderbooks_full"))
def test_v3_rejects_zero_row_parent(
    tmp_path: Path,
    empty_table: str,
):
    root, authority, manifest = _ready_dataset(
        tmp_path,
        empty_table=empty_table,
    )
    with pytest.raises(Stage2AuthoritativeV3Error, match="PARENT_ZERO_ROWS"):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )


def test_v3_rejects_nonempty_parent_with_zero_roster_selected_rows(
    tmp_path: Path,
):
    root, _, _ = _ready_dataset(tmp_path)
    victim = root / required_btc_fact_keys(DISCOVERY_DATES[0])["trades"]
    with gzip.open(victim, "wt", encoding="utf-8", newline="") as handle:
        handle.write(TRADE_HEADER)
        handle.write(
            "KXOTHER-OUTSIDE,outside,3000,7000,10000,"
            "yes,10000100,1000100,999999\n"
        )
    authority = _authority_from_files(root, version_suffix="outside")
    manifest = build_bound_input_manifest_v2(
        authority=authority,
        content_root=root,
    )
    with pytest.raises(Stage2AuthoritativeV3Error, match="PARENT_ZERO_ROWS"):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )


def test_v3_rejects_parent_truncated_after_manifest(tmp_path: Path):
    root, authority, manifest = _ready_dataset(tmp_path)
    prior = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    victim = root / required_btc_fact_keys(DISCOVERY_DATES[0])["trades"]
    payload = victim.read_bytes()
    victim.write_bytes(payload[:-8])
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="PARENT_REBIND_FAILED",
    ):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="PARENT_REBIND_FAILED",
    ):
        reverify_authoritative_source_result_v3(
            prior_result=prior,
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )


def test_v3_rejects_valid_exact_parent_with_logically_truncated_roster(
    tmp_path: Path,
):
    root, _, _ = _ready_dataset(tmp_path)
    day = DISCOVERY_DATES[0]
    victim = root / required_btc_fact_keys(day)["orderbooks_full"]
    replacement = victim.with_name("replacement.parquet")
    missing_ticker = [
        ticker
        for ticker in expected_hourly_roster()
        if MARKET_SOURCE_DAY[ticker] == day
    ][-1]
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE truncated AS
            SELECT *
            FROM read_parquet(?)
            WHERE market_ticker <> ?
            """,
            [str(victim), missing_ticker],
        )
        connection.execute(
            "COPY truncated TO ? (FORMAT PARQUET)",
            [str(replacement)],
        )
    finally:
        connection.close()
    replacement.replace(victim)
    authority = _authority_from_files(root, version_suffix="logical-trunc")
    manifest = build_bound_input_manifest_v2(
        authority=authority,
        content_root=root,
    )
    with pytest.raises(Stage2AuthoritativeV3Error, match="ROSTER_INCOMPLETE"):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )


def test_v3_rejects_any_event_before_first_valid_snapshot(tmp_path: Path):
    root, authority, manifest = _ready_dataset(
        tmp_path,
        late_snapshot=True,
    )
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="LATE_FIRST_SNAPSHOT",
    ):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=root,
        )


def test_v3_rejects_same_paths_with_swapped_exact_parent_version(
    tmp_path: Path,
):
    root, authority_a, manifest_a = _ready_dataset(tmp_path)
    authority_b = _authority_from_files(root, version_suffix="b")
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="BOUND_MANIFEST_REBIND_MISMATCH",
    ):
        build_authoritative_source_result_v3(
            authority=authority_b,
            input_manifest=manifest_a,
            content_root=root,
        )
    # The original consistent triple remains usable.
    assert build_authoritative_source_result_v3(
        authority=authority_a,
        input_manifest=manifest_a,
        content_root=root,
    ).state_row_count == 78


def test_v3_preserves_exact_trade_book_tie_as_one_atomic_envelope(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(
        tmp_path,
        atomic_snapshot_tie=True,
    )
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    first = result.state_rows[0]
    assert first.atomic_no_precedence is True
    assert first.event_count == 2
    assert first.event_kinds == ("BOOK_SNAPSHOT", "TRADE")


def test_v3_rejects_same_paths_with_swapped_content(tmp_path: Path):
    root, _, manifest_a = _ready_dataset(tmp_path)
    victim = root / required_btc_fact_keys(DISCOVERY_DATES[0])["trades"]
    _write_trade_parent(
        victim,
        day=DISCOVERY_DATES[0],
        empty=False,
        taker_side="no",
    )
    authority_b = _authority_from_files(root, version_suffix="content-b")
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="BOUND_MANIFEST_REBIND_MISMATCH",
    ):
        build_authoritative_source_result_v3(
            authority=authority_b,
            input_manifest=manifest_a,
            content_root=root,
        )


def test_v3_requires_typed_manifest_and_exact_bound_content_root(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="TYPED_BOUND_MANIFEST_REQUIRED",
    ):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest.receipt(),  # type: ignore[arg-type]
            content_root=root,
        )

    copied_root = tmp_path / "same-bytes-different-root"
    shutil.copytree(root, copied_root)
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="BOUND_MANIFEST_REBIND_MISMATCH",
    ):
        build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=manifest,
            content_root=copied_root,
        )


def test_v3_receipt_rejects_mapping_forged_counts_coverage_and_book(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="TYPED_RESULT_REQUIRED",
    ):
        build_authoritative_source_receipt_v3(  # type: ignore[arg-type]
            {"state_rows": result.state_rows}
        )
    for forged in (
        replace(result, state_row_count=0),
        replace(result, state_spine_sha256="f" * 64),
        replace(result, market_coverage=()),
    ):
        with pytest.raises(
            Stage2AuthoritativeV3Error,
            match="AUTHORITATIVE_RESULT_INCONSISTENT",
        ):
            build_authoritative_source_receipt_v3(forged)

    first = result.state_rows[0]
    forged_first = replace(first, book_events=())
    forged_rows = (forged_first, *result.state_rows[1:])
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="AUTHORITATIVE_RESULT_INCONSISTENT",
    ):
        build_authoritative_source_receipt_v3(
            replace(result, state_rows=forged_rows)
        )
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="AUTHORITATIVE_RESULT_INCONSISTENT",
    ):
        latest_asof_state_v3(
            replace(result, state_rows=result.state_rows[:-1]),
            market_ticker=expected_hourly_roster()[0],
            decision_recv_mono_ns=1_000_101,
            decision_recv_wall_ns=10_000_101,
        )


def test_v3_latest_asof_rejects_decision_before_initial_snapshot(
    tmp_path: Path,
):
    root, authority, manifest = _ready_dataset(tmp_path)
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=root,
    )
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="NO_LATEST_ASOF_STATE",
    ):
        latest_asof_state_v3(
            result,
            market_ticker=expected_hourly_roster()[0],
            decision_recv_mono_ns=999_999,
            decision_recv_wall_ns=9_999_999,
        )


def test_v3_six_file_dry_run_from_v1_v2_json_receipts(tmp_path: Path):
    root, authority, manifest = _ready_dataset(tmp_path)
    authority_path = tmp_path / "authority.json"
    manifest_path = tmp_path / "bound-manifest.json"
    authority_path.write_text(
        json.dumps(authority.receipt(), sort_keys=True),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(manifest.receipt(), sort_keys=True),
        encoding="utf-8",
    )
    authority_receipt_sha = _sha(authority_path.read_bytes())
    manifest_receipt_sha = _sha(manifest_path.read_bytes())
    receipt = dry_run_authoritative_source_v3(
        authority_receipt_path=authority_path,
        authority_receipt_sha256=authority_receipt_sha,
        bound_manifest_receipt_path=manifest_path,
        bound_manifest_receipt_sha256=manifest_receipt_sha,
        content_root=root,
    )
    assert receipt["parent_count"] == 6
    assert receipt["state_row_count"] == 78
    assert receipt["v4_2_contract_coverage_claimed"] is False
    with pytest.raises(
        Stage2AuthoritativeV3Error,
        match="RECEIPT_PIN_MISMATCH",
    ):
        dry_run_authoritative_source_v3(
            authority_receipt_path=authority_path,
            authority_receipt_sha256="f" * 64,
            bound_manifest_receipt_path=manifest_path,
            bound_manifest_receipt_sha256=manifest_receipt_sha,
            content_root=root,
        )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            (
                "tools.research.crypto_mm."
                "round4_stage2_postfill_extractor_v3"
            ),
            "--authority-receipt",
            str(authority_path),
            "--authority-receipt-sha256",
            authority_receipt_sha,
            "--bound-manifest-receipt",
            str(manifest_path),
            "--bound-manifest-receipt-sha256",
            manifest_receipt_sha,
            "--content-root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    cli_receipt = json.loads(completed.stdout)
    assert cli_receipt["parent_count"] == 6
    assert cli_receipt["state_row_count"] == 78
