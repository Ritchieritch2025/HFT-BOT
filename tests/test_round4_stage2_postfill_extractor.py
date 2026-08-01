"""Synthetic fail-closed tests for the ROUND4 Stage-2 source extractor."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from tools.research.crypto_mm.round4_stage2_postfill_extractor import (
    DISCOVERY_DATES,
    CausalBookReconstructor,
    Stage2ExtractionError,
    authorize_extraction,
    build_bound_input_manifest,
    build_extraction_receipt,
    expected_hourly_roster,
    group_atomic_envelopes,
    normalize_l2_row,
    normalize_terminal_labels,
    normalize_trade_row,
    prepare_source_authority,
    read_l2_parquet_rows,
    read_trade_csv_rows,
    verify_day_eligibility,
    verify_local_exact_version,
    visible_fok_levels,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _audit_receipt(*, decision: str = "REJECT") -> dict[str, object]:
    return {
        "schema": "round4-postfill-v4-1-independent-audit-v1",
        "decision": decision,
        "independent": True,
        "contract_version": "ROUND4_POSTFILL_PUBLIC_PROXY_V4_1",
        "seal_authorized": False,
        "reason_codes": [
            "VALIDATOR_HASH_NOT_PINNED",
            "ORPHAN_METADATA_SETTLEMENT",
            "BOOK_NOT_LATEST_ASOF",
            "SOURCE_PATH_NOT_CONTENT_ROOT_BOUND",
        ],
    }


def _eligible_object(
    day: str,
    table: str,
    logical_source_key: str,
    payload: bytes,
) -> dict[str, object]:
    return {
        "VersionId": f"version-{day}-{table}",
        "bucket": "kalshi-vault-ritcardo",
        "date": day,
        "durability_scope": True,
        "durability_verified": True,
        "eligibility_tag_state": "TAGGED_VERIFIED",
        "exposure_policy": "RESEARCH_ELIGIBLE",
        "family": "facts",
        "key": f"ec2/{logical_source_key}",
        "logical_source_key": logical_source_key,
        "mutable_source": False,
        "research_candidate": True,
        "research_eligible": True,
        "sha256": _sha(payload),
        "size": len(payload),
        "table": table,
        "verification_state": "EXACT_VERSION_FULL_SHA256",
        "version_resolution": "SEALED_CURRENT_EXACT",
    }


def _day_receipts(
    day: str,
    objects: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object], bytes]:
    receipt_set = hashlib.sha256(f"set:{day}".encode()).hexdigest()
    durable_set = hashlib.sha256(f"durable:{day}".encode()).hexdigest()
    audit_sha = hashlib.sha256(f"audit:{day}".encode()).hexdigest()
    tagged = {
        "schema_version": "canonical-object-receipt-v1",
        "date": day,
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authoritative": True,
        "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
        "receipt_set_sha256": receipt_set,
        "byte_attestation_receipt_set_sha256": durable_set,
        "durability_set_sha256": hashlib.sha256(
            f"object-durability:{day}".encode()
        ).hexdigest(),
        "eligibility_single_writer_audit_sha256": audit_sha,
        "objects": objects,
    }
    tagged_bytes = _canonical(tagged)
    index = {
        "schema_version": "canonical-durable-receipt-index-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
        "date": day,
        "complete": True,
        "completed": True,
        "receipt_set_sha256": receipt_set,
        "byte_attestation_receipt_set_sha256": durable_set,
        "eligibility_single_writer_audit_sha256": audit_sha,
        "receipt_payload_sha256": _sha(tagged_bytes),
        "receipt_payload_size": len(tagged_bytes),
        "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
        "receipt_object": {
            "VersionId": f"tagged-version-{day}",
            "verification_state": "EXACT_VERSION_FULL_SHA256",
            "sha256": _sha(tagged_bytes),
            "size": len(tagged_bytes),
        },
    }
    status = {
        "schema_version": "research-v3-daily-status-v2",
        "state": "V3_REFERENCE_PUBLISHED",
        "date": day,
        "durable_receipt_set_sha256": durable_set,
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "VersionId": f"manifest-version-{day}",
            "verification_state": "EXACT_VERSION_FULL_SHA256",
            "sha256": hashlib.sha256(f"manifest:{day}".encode()).hexdigest(),
            "size": 123,
        },
        "tagged_index": (
            f"/receipt/date={day}/TAGGED-DURABLE-{receipt_set}.json"
        ),
    }
    return status, index, tagged_bytes


def _required_keys(day: str) -> dict[str, str]:
    stem = f"category=Crypto/subcategory=BTC/date={day}"
    return {
        "orderbooks_full": (
            "warehouse/facts/orderbooks_full/"
            f"{stem}/orderbooks_full__Crypto__BTC__{day}.parquet"
        ),
        "trades": (
            "warehouse/facts/trades/"
            f"{stem}/trades__Crypto__BTC__{day}.csv.gz"
        ),
    }


def _authorized_days() -> dict[str, object]:
    result = {}
    for day in DISCOVERY_DATES:
        required = _required_keys(day)
        objects = [
            _eligible_object(day, table, key, f"{day}:{table}".encode())
            for table, key in required.items()
        ]
        status, index, tagged_bytes = _day_receipts(day, objects)
        result[day] = verify_day_eligibility(
            day=day,
            status=status,
            durable_index=index,
            tagged_receipt_bytes=tagged_bytes,
            required_logical_sources=required,
        )
    return result


def _l2(
    ticker: str,
    *,
    mono: int,
    wall: int,
    msg_type: str,
    side: str | None = None,
    price: int | None = None,
    delta: int | None = None,
    yes_levels: object | None = None,
    no_levels: object | None = None,
    seq: int = 1,
    ts_utc: int = 999,
) -> dict[str, object]:
    return {
        "ts_utc": ts_utc,
        "market_ticker": ticker,
        "msg_type": msg_type,
        "side": side,
        "price_e4": price,
        "delta_e4": delta,
        "yes_levels": (
            json.dumps(yes_levels) if yes_levels is not None else None
        ),
        "no_levels": (
            json.dumps(no_levels) if no_levels is not None else None
        ),
        "ws_sid": 1,
        "ws_seq": seq,
        "recv_wall_ns": wall,
        "recv_mono_ns": mono,
    }


def _trade(
    ticker: str,
    *,
    mono: int,
    wall: int,
    trade_id: str,
    taker_side: object = "yes",
    yes_price_e4: int = 3_100,
    count_e4: int = 10_000,
    ts_utc: int = 999,
) -> dict[str, object]:
    return {
        "ts_utc": ts_utc,
        "market_ticker": ticker,
        "trade_id": trade_id,
        "yes_price_e4": yes_price_e4,
        "no_price_e4": 10_000 - yes_price_e4,
        "count_e4": count_e4,
        "taker_side": taker_side,
        "recv_wall_ns": wall,
        "recv_mono_ns": mono,
    }


def test_hourly_roster_is_exactly_72_and_uses_utc_to_new_york_identity():
    roster = expected_hourly_roster()
    assert len(roster) == 72
    assert len(set(roster)) == 72
    assert roster[0] == "KXBTC15M-26JUL192015-15"
    assert roster[23] == "KXBTC15M-26JUL201915-15"
    assert roster[24] == "KXBTC15M-26JUL202015-15"
    assert roster[-1] == "KXBTC15M-26JUL221915-15"
    assert all(ticker.endswith("15-15") for ticker in roster)


def test_source_preparation_requires_all_three_published_days_but_cannot_extract():
    days = _authorized_days()
    authority = prepare_source_authority(eligible_days=days)
    assert authority.source_preparation_allowed is True
    assert authority.extraction_allowed is False
    assert authority.contract_adapter_status == "V4_2_PENDING"
    assert authority.source_dates == DISCOVERY_DATES
    assert len(authority.exact_version_objects) == 6

    with pytest.raises(Stage2ExtractionError, match="CONTRACT_ADAPTER_PENDING"):
        authorize_extraction(
            audit_receipt=_audit_receipt(decision="REJECT"),
            eligible_days=days,
        )
    with pytest.raises(Stage2ExtractionError, match="DAY_SET_MISMATCH"):
        prepare_source_authority(
            eligible_days={
                day: value
                for day, value in days.items()
                if day != "2026-07-22"
            },
        )


def test_eligibility_fails_closed_on_unpublished_or_non_exact_object():
    day = DISCOVERY_DATES[0]
    required = _required_keys(day)
    objects = [
        _eligible_object(day, table, key, f"{day}:{table}".encode())
        for table, key in required.items()
    ]
    status, index, tagged_bytes = _day_receipts(day, objects)
    status["state"] = "PREPARING"
    with pytest.raises(Stage2ExtractionError, match="DAY_NOT_PUBLISHED"):
        verify_day_eligibility(
            day=day,
            status=status,
            durable_index=index,
            tagged_receipt_bytes=tagged_bytes,
            required_logical_sources=required,
        )

    status["state"] = "V3_REFERENCE_PUBLISHED"
    tagged = json.loads(tagged_bytes)
    tagged["objects"][0]["research_eligible"] = False
    changed = _canonical(tagged)
    index["receipt_payload_sha256"] = _sha(changed)
    index["receipt_payload_size"] = len(changed)
    index["receipt_object"]["sha256"] = _sha(changed)
    index["receipt_object"]["size"] = len(changed)
    with pytest.raises(Stage2ExtractionError, match="SOURCE_NOT_ELIGIBLE"):
        verify_day_eligibility(
            day=day,
            status=status,
            durable_index=index,
            tagged_receipt_bytes=changed,
            required_logical_sources=required,
        )


def test_local_source_must_match_exact_version_bytes(tmp_path: Path):
    payload = b"sealed exact bytes"
    logical = _required_keys(DISCOVERY_DATES[0])["trades"]
    path = tmp_path / logical
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    obj = _eligible_object(
        DISCOVERY_DATES[0],
        "trades",
        logical,
        payload,
    )
    verified = verify_local_exact_version(
        path,
        obj,
        content_root=tmp_path,
    )
    assert verified["sha256"] == _sha(payload)
    with pytest.raises(Stage2ExtractionError, match="CONTENT_ROOT_MISMATCH"):
        verify_local_exact_version(
            path,
            obj,
            content_root=tmp_path / "wrong-root",
        )
    path.write_bytes(payload + b"drift")
    with pytest.raises(Stage2ExtractionError, match="LOCAL_SOURCE_DRIFT"):
        verify_local_exact_version(
            path,
            obj,
            content_root=tmp_path,
        )


def test_trade_csv_forces_taker_side_varchar(tmp_path: Path):
    ticker = expected_hourly_roster()[0]
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "market_ticker,trade_id,yes_price_e4,no_price_e4,count_e4,"
        "taker_side,recv_wall_ns,recv_mono_ns,ts_utc\n"
        f"{ticker},a,3100,6900,10000,yes,2000,1000,9\n"
        f"{ticker},b,3000,7000,20000,no,2100,1100,8\n"
    )
    rows = read_trade_csv_rows(csv_path, {ticker})
    assert [row["taker_side"] for row in rows] == ["yes", "no"]
    assert all(type(row["taker_side"]) is str for row in rows)

    bad = _trade(
        ticker,
        mono=1,
        wall=2,
        trade_id="bad",
        taker_side=True,
    )
    with pytest.raises(Stage2ExtractionError, match="TAKER_SIDE_NOT_VARCHAR"):
        normalize_trade_row(bad, source_ordinal=0, roster={ticker})


def test_l2_parquet_reader_selects_only_roster_and_receive_clocks(
    tmp_path: Path,
):
    duckdb = pytest.importorskip("duckdb")
    ticker = expected_hourly_roster()[0]
    path = tmp_path / "l2.parquet"
    connection = duckdb.connect(":memory:")
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
    connection.executemany(
        "INSERT INTO l2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                ticker,
                "delta",
                "yes",
                3_000,
                10_000,
                None,
                None,
                1,
                2,
                2_000,
                1_000,
                999,
            ),
            (
                "KXOTHER-OUTSIDE",
                "delta",
                "yes",
                3_000,
                10_000,
                None,
                None,
                1,
                1,
                1_000,
                900,
                1,
            ),
        ],
    )
    connection.execute(
        "COPY l2 TO ? (FORMAT PARQUET)",
        [str(path)],
    )
    connection.close()

    rows = read_l2_parquet_rows(path, {ticker})
    assert len(rows) == 1
    assert rows[0]["market_ticker"] == ticker
    assert rows[0]["recv_mono_ns"] == 1_000
    assert "ts_utc" not in rows[0]


def test_recv_mono_orders_events_ts_utc_is_never_a_clock():
    ticker = expected_hourly_roster()[0]
    later_exchange_ts = normalize_trade_row(
        _trade(
            ticker,
            mono=100,
            wall=1_000,
            trade_id="first-local",
            ts_utc=999_999,
        ),
        source_ordinal=0,
        roster={ticker},
    )
    earlier_exchange_ts = normalize_trade_row(
        _trade(
            ticker,
            mono=200,
            wall=1_100,
            trade_id="second-local",
            ts_utc=1,
        ),
        source_ordinal=1,
        roster={ticker},
    )
    envelopes = group_atomic_envelopes(
        [earlier_exchange_ts, later_exchange_ts]
    )
    assert [row.recv_mono_ns for row in envelopes] == [100, 200]
    assert all("ts_utc" not in event.payload for row in envelopes for event in row.events)


def test_exact_receive_tie_is_atomic_and_input_order_invariant():
    ticker = expected_hourly_roster()[0]
    rows = [
        normalize_l2_row(
            _l2(
                ticker,
                mono=100,
                wall=1_000,
                msg_type="delta",
                side="yes",
                price=3_000,
                delta=10_000,
                seq=2,
            ),
            source_ordinal=1,
            roster={ticker},
        ),
        normalize_trade_row(
            _trade(
                ticker,
                mono=100,
                wall=1_000,
                trade_id="tie-trade",
            ),
            source_ordinal=2,
            roster={ticker},
        ),
    ]
    forward = group_atomic_envelopes(rows)
    reverse = group_atomic_envelopes(reversed(rows))
    assert len(forward) == 1
    assert forward[0].atomic_no_precedence is True
    assert forward[0].source_rows_sha256 == reverse[0].source_rows_sha256
    assert {event.kind for event in forward[0].events} == {
        "BOOK_DELTA",
        "TRADE",
    }

    conflict = dict(rows[0].payload)
    conflict.update(
        {
            "market_ticker": ticker,
            "msg_type": "delta",
            "side": "no",
            "price_e4": 6_900,
            "delta_e4": 10_000,
            "ws_sid": 1,
            "ws_seq": 3,
            "recv_mono_ns": 100,
            "recv_wall_ns": 1_001,
        }
    )
    conflict_event = normalize_l2_row(
        conflict,
        source_ordinal=3,
        roster={ticker},
    )
    with pytest.raises(Stage2ExtractionError, match="CLOCK_PAIR_CONFLICT"):
        group_atomic_envelopes([rows[0], conflict_event])


def test_left_truncation_ignores_delta_until_first_valid_snapshot():
    ticker = expected_hourly_roster()[0]
    raw = [
        _l2(
            ticker,
            mono=100,
            wall=1_000,
            msg_type="delta",
            side="yes",
            price=3_000,
            delta=50_000,
            seq=1,
        ),
        _l2(
            ticker,
            mono=200,
            wall=1_100,
            msg_type="snapshot",
            yes_levels=[[3_000, 10_000], [2_900, 20_000]],
            no_levels=[[6_900, 30_000]],
            seq=2,
        ),
        _l2(
            ticker,
            mono=300,
            wall=1_200,
            msg_type="delta",
            side="yes",
            price=3_000,
            delta=10_000,
            seq=3,
        ),
    ]
    events = [
        normalize_l2_row(row, source_ordinal=index, roster={ticker})
        for index, row in enumerate(raw)
    ]
    replay = CausalBookReconstructor({ticker})
    envelopes = group_atomic_envelopes(events)

    replay.consume_atomic(envelopes[0])
    assert replay.is_usable(ticker) is False
    assert replay.coverage(ticker)["ignored_pre_snapshot_deltas"] == 1

    replay.consume_atomic(envelopes[1])
    assert replay.is_usable(ticker) is True
    assert replay.coverage(ticker)["first_valid_snapshot_recv_mono_ns"] == 200
    assert replay.book(ticker)["yes"][3_000] == 10_000

    replay.consume_atomic(envelopes[2])
    assert replay.book(ticker)["yes"][3_000] == 20_000


def test_fok_levels_use_only_observed_causal_prices_without_grid_synthesis():
    ticker = expected_hourly_roster()[0]
    snapshot = normalize_l2_row(
        _l2(
            ticker,
            mono=200,
            wall=1_100,
            msg_type="snapshot",
            yes_levels=[[3_100, 10_000], [3_000, 20_000]],
            no_levels=[[6_600, 30_000], [6_700, 40_000]],
            seq=2,
        ),
        source_ordinal=0,
        roster={ticker},
    )
    replay = CausalBookReconstructor({ticker})
    replay.consume_atomic(group_atomic_envelopes([snapshot])[0])

    held_yes = visible_fok_levels(
        replay,
        ticker=ticker,
        flatten_book_side="ASK",
        limit_price_e4=3_000,
    )
    assert held_yes == (
        {"price_e4": 3_100, "qty_fp": Decimal("1")},
        {"price_e4": 3_000, "qty_fp": Decimal("2")},
    )
    assert all(level["price_e4"] != 3_050 for level in held_yes)

    held_no = visible_fok_levels(
        replay,
        ticker=ticker,
        flatten_book_side="BID",
        limit_price_e4=3_400,
    )
    assert held_no == (
        {"price_e4": 3_300, "qty_fp": Decimal("4")},
        {"price_e4": 3_400, "qty_fp": Decimal("3")},
    )


def test_terminal_result_is_separate_label_and_never_feature_payload():
    ticker = expected_hourly_roster()[0]
    labels = normalize_terminal_labels(
        [
            {
                "market_ticker": ticker,
                "market_id": "market-id",
                "market_status": "FINALIZED",
                "official_market_result": "YES",
                "settlement_recv_wall_ns": 20_000,
                "settlement_recv_mono_ns": 10_000,
                "settlement_stable_source_id": "terminal-id",
                "source_rows_sha256": "a" * 64,
            }
        ],
        roster={ticker},
    )
    assert labels[ticker]["official_market_result"] == "YES"
    assert labels[ticker]["role"] == "LABEL_ONLY"
    assert labels[ticker]["feature_eligible"] is False

    event = normalize_trade_row(
        _trade(
            ticker,
            mono=100,
            wall=1_000,
            trade_id="causal",
        ),
        source_ordinal=0,
        roster={ticker},
    )
    assert "official_market_result" not in event.payload
    assert "market_status" not in event.payload


def test_receipt_binds_roster_provenance_completeness_and_is_deterministic(
    tmp_path: Path,
):
    authority = prepare_source_authority(
        eligible_days=_authorized_days(),
    )
    for obj in authority.exact_version_objects:
        source = tmp_path / obj.logical_source_key
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"{obj.source_date_utc}:{obj.table}".encode())
    input_manifest = build_bound_input_manifest(
        authority=authority,
        content_root=tmp_path,
    )
    assert input_manifest["content_root_bound"] is True
    assert len(input_manifest["files"]) == 6
    roster = expected_hourly_roster()
    coverage = {
        ticker: {
            "first_valid_snapshot_recv_mono_ns": index + 1,
            "first_valid_snapshot_recv_wall_ns": index + 101,
            "ignored_pre_snapshot_deltas": 0,
            "trade_rows": 1,
            "l2_rows": 1,
        }
        for index, ticker in enumerate(roster)
    }
    receipt_a = build_extraction_receipt(
        authority=authority,
        input_manifest=input_manifest,
        roster=roster,
        coverage_by_market=coverage,
        causal_rows_sha256="b" * 64,
        terminal_labels_sha256="c" * 64,
        causal_row_count=144,
        terminal_label_count=72,
    )
    receipt_b = build_extraction_receipt(
        authority=authority,
        input_manifest=input_manifest,
        roster=list(reversed(roster)),
        coverage_by_market=dict(reversed(list(coverage.items()))),
        causal_rows_sha256="b" * 64,
        terminal_labels_sha256="c" * 64,
        causal_row_count=144,
        terminal_label_count=72,
    )
    assert receipt_a == receipt_b
    assert receipt_a["market_count"] == 72
    assert receipt_a["markets_per_day"] == {
        "2026-07-20": 24,
        "2026-07-21": 24,
        "2026-07-22": 24,
    }
    assert receipt_a["all_markets_have_valid_snapshot"] is True
    assert receipt_a["terminal_result_feature_use"] is False
    assert receipt_a["contract_adapter_status"] == "V4_2_PENDING"
    assert receipt_a["extraction_authorized"] is False
    assert len(receipt_a["input_exact_version_objects"]) == 6
    assert len(receipt_a["payload_sha256"]) == 64

    incomplete = dict(coverage)
    incomplete.pop(roster[-1])
    with pytest.raises(Stage2ExtractionError, match="ROSTER_INCOMPLETE"):
        build_extraction_receipt(
            authority=authority,
            input_manifest=input_manifest,
            roster=roster,
            coverage_by_market=incomplete,
            causal_rows_sha256="b" * 64,
            terminal_labels_sha256="c" * 64,
            causal_row_count=143,
            terminal_label_count=72,
        )
