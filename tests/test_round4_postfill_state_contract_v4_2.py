from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from tools.research.crypto_mm import (
    round4_postfill_state_contract_v4_2 as V42,
)
from tools.research.crypto_mm import round4_table_builder_v4_2 as B42


ROOT = Path(__file__).resolve().parents[1]
DDL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract_v4_2.sql"
)
VALIDATOR_SQL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_v4_2_validator.sql"
)
CONTRACT = V42.CONTRACT_VERSION
DAY = "2026-07-20"
EPISODE = "ep"
TICKER = "KXBTC15M-26JUL20-TEST"
MARKET_ID = "market-ep"
MANIFEST_ID = "1" * 64
CONTENT_ROOT = "2" * 64


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )


def _projection(
    kind: str,
    key: str,
    payload: dict[str, object],
) -> dict[str, object]:
    text = _canonical(payload)
    return {
        "projection_row_id": f"{kind}::{key}",
        "contract_version": CONTRACT,
        "projection_kind": kind,
        "original_row_key": key,
        "postfill_episode_id": EPISODE,
        "source_date_utc": DAY,
        "market_ticker": TICKER,
        "market_id": MARKET_ID,
        "v41_payload_json": text,
        "payload_sha256": _sha_text(text),
    }


def _projection_spine(rows: list[dict[str, object]]) -> str:
    material = "|".join(
        (
            f"{row['projection_kind']}:"
            f"{row['original_row_key']}:"
            f"{row['payload_sha256']}"
        )
        for row in sorted(
            rows,
            key=lambda item: (
                str(item["projection_kind"]),
                str(item["original_row_key"]),
            ),
        )
    )
    return _sha_text(material)


def _integrity_rows() -> dict[str, list[dict[str, object]]]:
    outcome_id = (
        "ep::V41::GRID::0::FLATTEN_FOK::OUTCOME::FOK_FULL"
    )
    slice_0 = f"{outcome_id}::SLICE::0"
    slice_1 = f"{outcome_id}::SLICE::1"
    projections = [
        _projection(
            "CAUSAL_STATE",
            "ep::V41::GRID::0",
            {"decision_index": 0},
        ),
        _projection(
            "ACTION",
            "ep::V41::GRID::0::KEEP",
            {"decision_index": 0, "action_kind": "KEEP"},
        ),
        _projection(
            "ACTION",
            "ep::V41::GRID::0::FLATTEN_FOK",
            {"decision_index": 0, "action_kind": "FLATTEN_FOK"},
        ),
        _projection(
            "KEEP_TRANSITION",
            "ep::V41::GRID::0::KEEP::TRANSITION",
            {"decision_index": 0},
        ),
        _projection(
            "FLATTEN_FOK_OUTCOME",
            outcome_id,
            {
                "decision_index": 0,
                "postfill_fok_outcome_id": outcome_id,
                "terminal_type": "FOK_FULL",
                "fok_fill_qty_fp": "2",
                "fok_processed_wall_ns": 200,
                "fok_processed_mono_ns": 200,
                "pre_effective_book_stable_source_id": "book-state-0",
                "pre_effective_book_source_rows_sha256": "5" * 64,
            },
        ),
        _projection(
            "FOK_SLICE",
            slice_0,
            {
                "postfill_fok_outcome_id": outcome_id,
                "decision_index": 0,
                "slice_index": 0,
                "slice_qty_fp": "1",
            },
        ),
        _projection(
            "FOK_SLICE",
            slice_1,
            {
                "postfill_fok_outcome_id": outcome_id,
                "decision_index": 0,
                "slice_index": 1,
                "slice_qty_fp": "1",
            },
        ),
        _projection(
            "PUBLIC_PROXY_EVIDENCE",
            "ep-evidence",
            {"evidence_id": "ep-evidence"},
        ),
    ]
    trade_payload = {
        "public_trade_row_id": "trade-row-0",
        "trade_id": "trade-0",
        "stable_source_id": "trade-0",
    }
    source_payload = {
        "postfill_episode_id": EPISODE,
        "market_ticker": TICKER,
        "market_id": MARKET_ID,
        "trade": trade_payload,
    }
    source_payload_json = _canonical(source_payload)
    source_payload_sha = _sha_text(source_payload_json)
    source_record_id = "source-trade-0"
    book_levels = [{"price_e4": 3_000, "qty_fp": "2"}]
    book_payload = {
        "postfill_episode_id": EPISODE,
        "decision_index": 0,
        "book_side": "ASK",
        "atomic_group_id": "book-envelope-0",
        "atomic_member_sequence": 0,
        "atomic_group_terminal": True,
        "book": {
            "market_ticker": TICKER,
            "market_id": MARKET_ID,
            "recv_wall_ns": 100,
            "recv_mono_ns": 100,
            "ingest_sequence": 0,
            "stable_source_id": "book-state-0",
            "levels": book_levels,
            "source_rows_sha256": "5" * 64,
        },
    }
    book_payload_json = _canonical(book_payload)
    book_payload_sha = _sha_text(book_payload_json)
    book_record_id = "source-book-0"
    book_row: dict[str, object] = {
        "source_record_id": book_record_id,
        "contract_version": CONTRACT,
        "source_manifest_id": MANIFEST_ID,
        "source_version": V42.EXACT_SOURCE_VERSION,
        "postfill_episode_id": EPISODE,
        "decision_index": 0,
        "market_ticker": TICKER,
        "market_id": MARKET_ID,
        "book_side": "ASK",
        "recv_wall_ns": 100,
        "recv_mono_ns": 100,
        "ingest_sequence": 0,
        "stable_source_id": "book-state-0",
        "atomic_group_id": "book-envelope-0",
        "atomic_member_sequence": 0,
        "atomic_group_terminal": True,
        "levels_json": _canonical(book_levels),
        "v41_book_source_rows_sha256": "5" * 64,
        "source_payload_sha256": book_payload_sha,
        "content_root_sha256": CONTENT_ROOT,
    }
    book_row["row_payload_sha256"] = V42.book_snapshot_row_sha256(
        book_row
    )
    trade_row: dict[str, object] = {
        "public_trade_row_id": "trade-row-0",
        "contract_version": CONTRACT,
        "source_record_id": source_record_id,
        "source_manifest_id": MANIFEST_ID,
        "source_version": V42.EXACT_SOURCE_VERSION,
        "postfill_episode_id": EPISODE,
        "market_ticker": TICKER,
        "market_id": MARKET_ID,
        "trade_id": "trade-0",
        "stable_source_id": "trade-0",
        "recv_wall_ns": 10,
        "recv_mono_ns": 10,
        "ingest_sequence": 0,
        "trade_payload_json": _canonical(trade_payload),
        "source_payload_sha256": source_payload_sha,
        "content_root_sha256": CONTENT_ROOT,
    }
    trade_row["row_payload_sha256"] = V42.trade_row_sha256(trade_row)
    trade_spine = _sha_text(
        f"{CONTENT_ROOT}:{source_record_id}:{source_payload_sha}"
    )
    book_spine = _sha_text(
        f"{CONTENT_ROOT}:{book_record_id}:{book_payload_sha}"
    )
    trade_row["trade_row_spine_sha256"] = trade_spine
    metadata_json = _canonical({"kind": "metadata", "episode": EPISODE})
    settlement_json = _canonical(
        {"kind": "settlement", "episode": EPISODE}
    )
    fee_json = _canonical({"kind": "fee"})
    seal_payload = {
        "contract_version": CONTRACT,
        "action_family_version": V42.ACTION_FAMILY_VERSION,
        "frozen_v41_contract_sha256": V42.FROZEN_V41_CONTRACT_SHA256,
        "exact_source_format": V42.EXACT_SOURCE_FORMAT,
        "exact_source_version": V42.EXACT_SOURCE_VERSION,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "live_authorized": False,
    }
    return {
        "postfill_v42_contract_seal": [
            {
                "contract_seal_id": (
                    "ROUND4_POSTFILL_V42_DATA_INTEGRITY_SEAL"
                ),
                **seal_payload,
                "seal_payload_sha256": V42.payload_sha256(seal_payload),
            }
        ],
        "postfill_v42_source_manifest": [
            {
                "source_manifest_id": MANIFEST_ID,
                "contract_version": CONTRACT,
                "source_format": V42.EXACT_SOURCE_FORMAT,
                "source_version": V42.EXACT_SOURCE_VERSION,
                "source_date_utc": DAY,
                "logical_source_path": f"/sealed/{DAY}/public.jsonl",
                "physical_source_path": f"/sealed/{DAY}/public.jsonl",
                "content_sha256": CONTENT_ROOT,
                "content_size_bytes": 1,
                "complete": True,
            }
        ],
        "postfill_v42_fee_schedule_receipt": [
            {
                "fee_schedule_receipt_id": "fee-receipt",
                "contract_version": CONTRACT,
                "v41_payload_json": fee_json,
                "payload_sha256": _sha_text(fee_json),
            }
        ],
        "postfill_v42_episode": [
            {
                "postfill_episode_id": EPISODE,
                "contract_version": CONTRACT,
                "action_family_version": V42.ACTION_FAMILY_VERSION,
                "experiment_id": (
                    "KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1"
                ),
                "source_date_utc": DAY,
                "market_ticker": TICKER,
                "market_id": MARKET_ID,
                "market_metadata_receipt_id": "metadata-receipt",
                "settlement_receipt_id": "settlement-receipt",
                "fee_schedule_receipt_id": "fee-receipt",
                "v41_batch_sha256": "3" * 64,
                "content_roots_sha256": _sha_text(CONTENT_ROOT),
                "projection_row_count": len(projections),
                "projection_spine_sha256": _projection_spine(projections),
                "zero_time_atom": False,
                "fit_authorized": False,
                "candidate_selection_authorized": False,
                "live_authorized": False,
            }
        ],
        "postfill_v42_episode_source_binding": [
            {
                "episode_source_binding_id": "4" * 64,
                "contract_version": CONTRACT,
                "postfill_episode_id": EPISODE,
                "source_manifest_id": MANIFEST_ID,
                "source_version": V42.EXACT_SOURCE_VERSION,
                "content_root_sha256": CONTENT_ROOT,
            }
        ],
        "postfill_v42_market_metadata_receipt": [
            {
                "market_metadata_receipt_id": "metadata-receipt",
                "contract_version": CONTRACT,
                "postfill_episode_id": EPISODE,
                "source_date_utc": DAY,
                "market_ticker": TICKER,
                "market_id": MARKET_ID,
                "payload_json": metadata_json,
                "payload_sha256": _sha_text(metadata_json),
            }
        ],
        "postfill_v42_settlement_receipt": [
            {
                "settlement_receipt_id": "settlement-receipt",
                "contract_version": CONTRACT,
                "postfill_episode_id": EPISODE,
                "source_date_utc": DAY,
                "market_ticker": TICKER,
                "market_id": MARKET_ID,
                "market_status": "FINALIZED",
                "payload_json": settlement_json,
                "payload_sha256": _sha_text(settlement_json),
            }
        ],
        "postfill_v42_projection_row": projections,
        "postfill_v42_source_record": [
            {
                "source_record_id": book_record_id,
                "contract_version": CONTRACT,
                "source_manifest_id": MANIFEST_ID,
                "source_version": V42.EXACT_SOURCE_VERSION,
                "source_date_utc": DAY,
                "postfill_episode_id": EPISODE,
                "record_type": "BOOK_SNAPSHOT",
                "record_index": 0,
                "payload_json": book_payload_json,
                "payload_sha256": book_payload_sha,
                "content_root_sha256": CONTENT_ROOT,
            },
            {
                "source_record_id": source_record_id,
                "contract_version": CONTRACT,
                "source_manifest_id": MANIFEST_ID,
                "source_version": V42.EXACT_SOURCE_VERSION,
                "source_date_utc": DAY,
                "postfill_episode_id": EPISODE,
                "record_type": "TRADE",
                "record_index": 1,
                "payload_json": source_payload_json,
                "payload_sha256": source_payload_sha,
                "content_root_sha256": CONTENT_ROOT,
            }
        ],
        "postfill_v42_book_snapshot": [book_row],
        "postfill_v42_fok_book_binding": [
            {
                "postfill_fok_outcome_id": outcome_id,
                "contract_version": CONTRACT,
                "postfill_episode_id": EPISODE,
                "decision_index": 0,
                "terminal_type": "FOK_FULL",
                "source_record_id": book_record_id,
                "source_manifest_id": MANIFEST_ID,
                "source_version": V42.EXACT_SOURCE_VERSION,
                "content_root_sha256": CONTENT_ROOT,
                "source_payload_sha256": book_payload_sha,
                "effective_wall_ns": 200,
                "effective_mono_ns": 200,
                "selected_recv_wall_ns": 100,
                "selected_recv_mono_ns": 100,
                "selected_ingest_sequence": 0,
                "selected_stable_source_id": "book-state-0",
                "selected_atomic_group_id": "book-envelope-0",
                "selected_atomic_member_sequence": 0,
                "selected_v41_book_source_rows_sha256": "5" * 64,
            }
        ],
        "postfill_v42_trade_row": [trade_row],
        "postfill_v42_book_spine": [
            {
                "postfill_episode_id": EPISODE,
                "contract_version": CONTRACT,
                "row_count": 1,
                "content_roots_sha256": _sha_text(CONTENT_ROOT),
                "row_spine_sha256": book_spine,
            }
        ],
        "postfill_v42_trade_spine": [
            {
                "postfill_episode_id": EPISODE,
                "contract_version": CONTRACT,
                "row_count": 1,
                "content_roots_sha256": _sha_text(CONTENT_ROOT),
                "row_spine_sha256": trade_spine,
            }
        ],
    }


def _connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    B42.create_postfill_v42_schema(connection, DDL)
    return connection


def _disable_pending_source_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        V42,
        "reverify_source_bound_rows",
        lambda **_kwargs: None,
    )


def _reject(
    rows: dict[str, list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    validator: Path = VALIDATOR_SQL,
) -> None:
    _disable_pending_source_only(monkeypatch)
    connection = _connection()
    with pytest.raises(
        (
            B42.Round4V42CommitError,
            duckdb.ConstraintException,
            duckdb.BinderException,
        )
    ):
        B42.insert_postfill_v42_tables(
            connection,
            rows,
            source_reverification=(),
            validator_sql_path=validator,
        )
    for table in V42.POSTFILL_V42_TABLES:
        assert connection.execute(
            f"SELECT count(*) FROM {table}"
        ).fetchone() == (0,)


def test_v42_source_adapter_is_hard_closed_until_authoritative_v3() -> None:
    assert V42.SOURCE_ADAPTER_AUTHORIZED is False
    assert V42.SOURCE_ADAPTER_STATUS == (
        "STAGE2_V3_AUTHORITATIVE_EOF_ADAPTER_PENDING"
    )
    with pytest.raises(
        V42.PostfillV42ContractError,
        match="SOURCE_ADAPTER_V3_PENDING",
    ):
        V42.validate_and_serialize_postfill_rows(
            logical_source_paths={DAY: [f"/sealed/{DAY}/public.jsonl"]},
            exact_source_manifests=[],
        )


def test_v42_commit_is_hard_closed_until_source_v3() -> None:
    connection = _connection()
    with pytest.raises(
        V42.PostfillV42ContractError,
        match="SOURCE_ADAPTER_V3_PENDING",
    ):
        B42.insert_postfill_v42_tables(
            connection,
            _integrity_rows(),
            source_reverification=(),
            validator_sql_path=VALIDATOR_SQL,
        )
    for table in V42.POSTFILL_V42_TABLES:
        assert connection.execute(
            f"SELECT count(*) FROM {table}"
        ).fetchone() == (0,)


def test_v42_rejects_stale_only_synthetic_source_remanifest() -> None:
    with pytest.raises(
        V42.PostfillV42ContractError,
        match="six exact-version parent bytes",
    ):
        V42.build_exact_source_bytes(
            v41_batch=object(),
            flatten_fok_outcomes=[{"stale_only": True}],
            source_date_utc=DAY,
        )


def test_v42_rejects_same_logical_path_with_new_parent_root_version() -> None:
    with pytest.raises(
        V42.PostfillV42ContractError,
        match="caller-authored complete files/manifests are forbidden",
    ):
        V42.exact_source_manifest(
            logical_source_path=f"/sealed/{DAY}/public.jsonl",
            physical_source_path=f"/other-root/{DAY}/public.jsonl",
            source_date_utc=DAY,
            source_version="replacement-version",
            content=b"replacement",
        )


def test_v42_normative_validator_exact_sha_and_valid_integrity_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hashlib.sha256(VALIDATOR_SQL.read_bytes()).hexdigest() == (
        B42.NORMATIVE_VALIDATOR_SHA256
    )
    _disable_pending_source_only(monkeypatch)
    connection = _connection()
    receipt = B42.insert_postfill_v42_tables(
        connection,
        _integrity_rows(),
        source_reverification=(),
        validator_sql_path=VALIDATOR_SQL,
    )
    assert receipt["status"] == "POSTFILL_V42_SQL_GATE_VALID"
    assert receipt["fit_authorized"] is False
    assert receipt["candidate_selection_authorized"] is False
    assert receipt["live_authorized"] is False


@pytest.mark.parametrize(
    "replacement",
    (
        b"",
        b"\n",
        (
            b"CREATE OR REPLACE TEMP VIEW "
            b"postfill_v42_validation_violations AS "
            b"SELECT NULL::VARCHAR AS violation_code, "
            b"NULL::VARCHAR AS row_key, NULL::VARCHAR AS detail "
            b"WHERE FALSE;"
        ),
    ),
)
def test_v42_commit_rejects_replaced_or_empty_validator(
    tmp_path: Path,
    replacement: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "validator.sql"
    fake.write_bytes(replacement)
    _reject(_integrity_rows(), monkeypatch, validator=fake)


class _MissingViolationsViewConnection:
    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        exact_sql: str,
    ) -> None:
        self.connection = connection
        self.exact_sql = exact_sql

    def execute(self, query, parameters=None):
        if query == self.exact_sql:
            return self.connection.execute(
                "CREATE OR REPLACE TEMP VIEW "
                "postfill_v42_validator_attestation AS "
                "SELECT "
                f"'{CONTRACT}'::VARCHAR AS contract_version, "
                "validator_sha256::VARCHAR AS validator_sha256 "
                "FROM postfill_v42_validator_runtime_input"
            )
        if parameters is None:
            return self.connection.execute(query)
        return self.connection.execute(query, parameters)


def test_v42_commit_rejects_missing_violations_view_even_with_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_pending_source_only(monkeypatch)
    raw = _connection()
    connection = _MissingViolationsViewConnection(
        raw,
        VALIDATOR_SQL.read_text(),
    )
    with pytest.raises(
        B42.Round4V42CommitError,
        match="violations view is missing",
    ):
        B42.insert_postfill_v42_tables(
            connection,
            _integrity_rows(),
            source_reverification=(),
            validator_sql_path=VALIDATOR_SQL,
        )
    assert raw.execute(
        "SELECT count(*) FROM postfill_v42_episode"
    ).fetchone() == (0,)


def test_v42_rejects_orphan_metadata_and_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for table, receipt_field in (
        ("postfill_v42_market_metadata_receipt", "metadata-orphan"),
        ("postfill_v42_settlement_receipt", "settlement-orphan"),
    ):
        rows = _integrity_rows()
        orphan = deepcopy(rows[table][0])
        id_field = (
            "market_metadata_receipt_id"
            if table.endswith("metadata_receipt")
            else "settlement_receipt_id"
        )
        orphan[id_field] = receipt_field
        orphan["postfill_episode_id"] = "missing-episode"
        rows[table].append(orphan)
        _reject(rows, monkeypatch)


def test_v42_rejects_duplicate_or_multiply_referenced_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _integrity_rows()
    duplicate = deepcopy(rows["postfill_v42_episode"][0])
    duplicate["postfill_episode_id"] = "second-episode"
    duplicate["market_ticker"] = "KXBTC15M-26JUL20-SECOND"
    duplicate["market_id"] = "market-second"
    rows["postfill_v42_episode"].append(duplicate)
    _reject(rows, monkeypatch)


def test_v42_rejects_receipt_episode_market_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _integrity_rows()
    rows["postfill_v42_settlement_receipt"][0][
        "market_ticker"
    ] = "KXBTC15M-26JUL20-WRONG"
    _reject(rows, monkeypatch)


def test_v42_rejects_owned_projection_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _integrity_rows()
    orphan = deepcopy(rows["postfill_v42_projection_row"][0])
    orphan["projection_row_id"] = "orphan-projection"
    orphan["original_row_key"] = "orphan"
    orphan["postfill_episode_id"] = "missing-episode"
    rows["postfill_v42_projection_row"].append(orphan)
    _reject(rows, monkeypatch)


def test_v42_rejects_precise_deleted_fok_slice_even_if_view_would_be_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _integrity_rows()
    target = (
        "ep::V41::GRID::0::FLATTEN_FOK::OUTCOME::"
        "FOK_FULL::SLICE::1"
    )
    rows["postfill_v42_projection_row"] = [
        row
        for row in rows["postfill_v42_projection_row"]
        if row["original_row_key"] != target
    ]
    episode = rows["postfill_v42_episode"][0]
    episode["projection_row_count"] = len(
        rows["postfill_v42_projection_row"]
    )
    episode["projection_spine_sha256"] = _projection_spine(
        rows["postfill_v42_projection_row"]
    )
    _reject(rows, monkeypatch)


def test_v42_rejects_book_trade_spine_content_root_local_rehash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _integrity_rows()
    rows["postfill_v42_trade_spine"][0][
        "content_roots_sha256"
    ] = "f" * 64
    rows["postfill_v42_trade_spine"][0][
        "row_spine_sha256"
    ] = "e" * 64
    rows["postfill_v42_trade_row"][0][
        "trade_row_spine_sha256"
    ] = "e" * 64
    _reject(rows, monkeypatch)


def test_v42_all_authorization_flags_are_false() -> None:
    assert V42.FIT_AUTHORIZED is False
    assert V42.CANDIDATE_SELECTION_AUTHORIZED is False
    assert V42.LIVE_AUTHORIZED is False
    assert V42.DEPLOYABLE is False
