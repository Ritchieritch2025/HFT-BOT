#!/usr/bin/env python3
"""Bounded-memory checkpoint and reducer contracts for Deep03 D3-W2A."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

from deep03_v3_methods import (  # noqa: E402
    BOUNDED_CHECKPOINT_SCHEMA,
    BOUNDED_STAGE_MANIFEST_SCHEMA,
    BoundedCheckpointStore,
    _assert_l1_interval_order_unambiguous,
    _assert_l1_asof_timestamps_unambiguous,
    bounded_stage_abi,
    bounded_source_binding,
)


def _manifest(*, source_sha: str = "a" * 64, local_root: str = "/cache-a") -> dict:
    return {
        "release_ids": ["2026-07-17__v3ref__fixture"],
        "release_dates": ["2026-07-17"],
        "objects": [
            {
                "release_id": "2026-07-17__v3ref__fixture",
                "date": "2026-07-17",
                "kind": "facts",
                "channel": "orderbooks_l1",
                "logical_key": "warehouse/facts/orderbooks_l1/date=2026-07-17/a.parquet",
                "local_path": f"{local_root}/a.parquet",
                "source_version_id": "exact-version-a",
                "sha256": source_sha,
                "row_count": 3,
            }
        ],
    }


def test_source_binding_is_path_independent_and_exact_object_sensitive():
    first = bounded_source_binding(_manifest(local_root="/first/cache"))
    moved = bounded_source_binding(_manifest(local_root="/mounted/elsewhere"))
    changed = bounded_source_binding(_manifest(source_sha="b" * 64))
    assert first == moved
    assert changed != first
    assert len(first) == 64


def test_checkpoint_publish_reuse_and_canonical_manifest(tmp_path):
    con = duckdb.connect()
    try:
        store = BoundedCheckpointStore(
            tmp_path / "checkpoints", bounded_source_binding(_manifest())
        )
        receipt, reused = store.write_partition(
            con,
            stage="l1_intervals",
            stage_version="interval-v1",
            partition_key="date=2026-07-17_bucket=03",
            select_sql="SELECT * FROM (VALUES (1,'M1'),(2,'M2')) t(t_us,market_ticker)",
            metrics={"positive_rows": 2},
        )
        assert reused is False
        assert receipt["schema_version"] == BOUNDED_CHECKPOINT_SCHEMA
        assert receipt["state"] == "COMPLETE"
        assert receipt["data"]["row_count"] == 2

        same, reused = store.write_partition(
            con,
            stage="l1_intervals",
            stage_version="interval-v1",
            partition_key="date=2026-07-17_bucket=03",
            # Reuse must not execute or replace a complete payload.
            select_sql="SELECT error('must not execute on resume')",
        )
        assert reused is True
        assert same == receipt

        manifest_path = store.finalize_stage(
            con,
            stage="l1_intervals",
            stage_version="interval-v1",
            partition_keys=["date=2026-07-17_bucket=03"],
        )
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        assert manifest["schema_version"] == BOUNDED_STAGE_MANIFEST_SCHEMA
        assert manifest["partition_count"] == 1
        assert manifest["row_count"] == 2
        assert manifest_bytes == (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        assert (manifest_path.parent / "MANIFEST.sha256").read_text() == (
            hashlib.sha256(manifest_bytes).hexdigest() + "  MANIFEST.json\n"
        )
        # Finalization is deterministic and idempotent.
        assert store.finalize_stage(
            con,
            stage="l1_intervals",
            stage_version="interval-v1",
            partition_keys=["date=2026-07-17_bucket=03"],
        ).read_bytes() == manifest_bytes
    finally:
        con.close()


def test_checkpoint_rejects_corruption_binding_drift_and_extra_receipts(tmp_path):
    con = duckdb.connect()
    try:
        root = tmp_path / "checkpoints"
        store = BoundedCheckpointStore(root, bounded_source_binding(_manifest()))
        store.write_partition(
            con,
            stage="observations",
            stage_version="obs-v1",
            partition_key="p00",
            select_sql="SELECT 1::BIGINT AS value",
        )
        data = root / "observations/data/p00.parquet"
        with data.open("ab") as handle:
            handle.write(b"corruption")
        with pytest.raises(RuntimeError, match="payload hash mismatch"):
            store.validate_partition(
                con,
                stage="observations",
                stage_version="obs-v1",
                partition_key="p00",
            )

        store.close()
        other = BoundedCheckpointStore(root, bounded_source_binding(_manifest(source_sha="b" * 64)))
        with pytest.raises(RuntimeError, match="source_binding"):
            other.validate_partition(
                con,
                stage="observations",
                stage_version="obs-v1",
                partition_key="p00",
            )
        other.close()
    finally:
        con.close()


def test_unpublished_crash_file_is_recomputed_and_completed_partitions_resume(tmp_path):
    con = duckdb.connect()
    try:
        root = tmp_path / "checkpoints"
        binding = bounded_source_binding(_manifest())
        first_store = BoundedCheckpointStore(root, binding)
        first_store.write_partition(
            con,
            stage="physical_shards",
            stage_version="shard-v1",
            partition_key="p00",
            select_sql="SELECT 10 AS value",
        )
        first_store.close()
        # Simulate a process dying after data promotion and before its receipt.
        orphan = root / "physical_shards/data/p01.parquet"
        con.execute(f"COPY (SELECT 999 AS value) TO '{orphan}' (FORMAT PARQUET)")

        resumed = BoundedCheckpointStore(root, binding)
        first, was_reused = resumed.write_partition(
            con,
            stage="physical_shards",
            stage_version="shard-v1",
            partition_key="p00",
            select_sql="SELECT error('completed partition was rerun')",
        )
        assert was_reused and first["data"]["row_count"] == 1
        second, was_reused = resumed.write_partition(
            con,
            stage="physical_shards",
            stage_version="shard-v1",
            partition_key="p01",
            select_sql="SELECT 20 AS value",
        )
        assert not was_reused
        assert con.execute(
            f"SELECT value FROM read_parquet('{orphan}')"
        ).fetchone() == (20,)
        assert second["data"]["row_count"] == 1
        resumed.finalize_stage(
            con,
            stage="physical_shards",
            stage_version="shard-v1",
            partition_keys=["p00", "p01"],
        )
    finally:
        con.close()


def test_stage_manifest_refuses_unexpected_complete_partition(tmp_path):
    con = duckdb.connect()
    try:
        store = BoundedCheckpointStore(
            tmp_path / "checkpoints", bounded_source_binding(_manifest())
        )
        for key in ("p00", "p01"):
            store.write_partition(
                con,
                stage="exact_reducer_input",
                stage_version="reduce-v1",
                partition_key=key,
                select_sql=f"SELECT '{key}' AS value",
            )
        with pytest.raises(RuntimeError, match="receipt set mismatch"):
            store.finalize_stage(
                con,
                stage="exact_reducer_input",
                stage_version="reduce-v1",
                partition_keys=["p00"],
            )
    finally:
        con.close()


def _install_l1_asof_rows(con, rows):
    con.execute(
        """
        CREATE TABLE l1_enriched(
          date DATE,market_ticker VARCHAR,t_us BIGINT,
          recv_wall_ns BIGINT,recv_mono_ns BIGINT,ws_sid BIGINT,ws_seq BIGINT,
          book_state VARCHAR,yes_bid_e4 BIGINT,yes_ask_e4 BIGINT
        )
        """
    )
    con.executemany("INSERT INTO l1_enriched VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def test_l1_asof_qc_accepts_exact_economic_duplicates(capsys):
    con = duckdb.connect()
    try:
        _install_l1_asof_rows(
            con,
            [
                ("2026-07-17", "M1", 100, 1, 5, 1, 1, "TWO_SIDED", 4000, 4200),
                # Receive ordering differs, but B01-observable economics are exact.
                ("2026-07-17", "M1", 100, 2, 6, 1, 2, "TWO_SIDED", 4000, 4200),
                ("2026-07-17", "M1", 200, 3, 7, 1, 3, "ONE_SIDED", 4100, None),
            ],
        )
        assert _assert_l1_asof_timestamps_unambiguous(
            con, marker="date=2026-07-17_bucket=00"
        ) == {
            "ambiguous_keys": 0,
            "ambiguous_rows": 0,
            "safe_duplicate_keys": 1,
            "safe_duplicate_excess_rows": 1,
        }
    finally:
        con.close()
    assert capsys.readouterr().out.strip() == (
        "D3_W2A_QC l1_asof_same_timestamp "
        "partition=date=2026-07-17_bucket=00 ambiguous_keys=0 "
        "ambiguous_rows=0 safe_duplicate_keys=1 safe_duplicate_excess_rows=1"
    )


def test_l1_asof_qc_fails_closed_on_different_quote_at_same_key(capsys):
    con = duckdb.connect()
    try:
        _install_l1_asof_rows(
            con,
            [
                ("2026-07-17", "M1", 100, 1, 1, 1, 1, "TWO_SIDED", 4000, 4200),
                ("2026-07-17", "M1", 100, 2, 2, 1, 2, "TWO_SIDED", 4100, 4300),
            ],
        )
        with pytest.raises(RuntimeError, match="same-timestamp L1 ASOF ambiguity"):
            _assert_l1_asof_timestamps_unambiguous(con)
    finally:
        con.close()
    assert "ambiguous_keys=1 ambiguous_rows=2" in capsys.readouterr().out


def test_b01_asof_qc_ignores_non_two_sided_rows_at_same_timestamp(capsys):
    con = duckdb.connect()
    try:
        _install_l1_asof_rows(
            con,
            [
                ("2026-07-17", "M1", 100, 1, 1, 1, 1, "TWO_SIDED", 4000, 4200),
                ("2026-07-17", "M1", 100, 2, 2, 1, 2, "ONE_SIDED", 4100, None),
            ],
        )
        profile = _assert_l1_asof_timestamps_unambiguous(con)
        assert profile["ambiguous_keys"] == 0
    finally:
        con.close()


def test_l1_interval_qc_fails_on_different_outputs_at_full_receive_order_tie(capsys):
    con = duckdb.connect()
    try:
        con.execute(
            """
            CREATE TABLE l1_enriched(
              date DATE,market_ticker VARCHAR,t_us BIGINT,
              recv_wall_ns BIGINT,recv_mono_ns BIGINT,ws_sid BIGINT,ws_seq BIGINT,
              event_proxy VARCHAR,sport VARCHAR,book_state VARCHAR,
              occurrence_us BIGINT,close_us BIGINT
            )
            """
        )
        con.executemany(
            "INSERT INTO l1_enriched VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-07-17", "M1", 100, 1, 1, 1, 1, "E1", "Tennis", "TWO_SIDED", 500, 1000),
                ("2026-07-17", "M1", 100, 1, 1, 1, 1, "E1", "Tennis", "ONE_SIDED", 500, 1000),
            ],
        )
        with pytest.raises(RuntimeError, match="full receive-order ambiguity"):
            _assert_l1_interval_order_unambiguous(con)
    finally:
        con.close()
    assert "ambiguous_keys=1 ambiguous_rows=2" in capsys.readouterr().out


def test_one_pass_physical_scatter_writes_empty_and_nonempty_shards(tmp_path):
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE source_rows(value BIGINT)")
        con.executemany("INSERT INTO source_rows VALUES (?)", [(0,), (2,), (3,)])
        store = BoundedCheckpointStore(
            tmp_path / "checkpoints", bounded_source_binding(_manifest())
        )
        results = store.write_partitioned_query(
            con,
            stage="l1_physical",
            stage_version="l1-physical-v1",
            partition_column="market_bucket",
            partition_keys={0: "date=2026-07-17_bucket=00", 1: "date=2026-07-17_bucket=01", 2: "date=2026-07-17_bucket=02"},
            select_sql=(
                "SELECT value,value%2 AS market_bucket FROM source_rows"
            ),
        )
        assert {key: value[0]["data"]["row_count"] for key, value in results.items()} == {
            "date=2026-07-17_bucket=00": 2,
            "date=2026-07-17_bucket=01": 1,
            "date=2026-07-17_bucket=02": 0,
        }
        for key, (receipt, reused) in results.items():
            assert reused is False
            assert [row["name"] for row in receipt["data"]["schema"]] == ["value"]
            assert "market_bucket" not in receipt["data"]["path"]

        resumed = store.write_partitioned_query(
            con,
            stage="l1_physical",
            stage_version="l1-physical-v1",
            partition_column="market_bucket",
            partition_keys={0: "date=2026-07-17_bucket=00", 1: "date=2026-07-17_bucket=01", 2: "date=2026-07-17_bucket=02"},
            select_sql="SELECT error('scatter must not rerun'),0 AS market_bucket",
        )
        assert all(reused for _receipt, reused in resumed.values())
        store.finalize_stage(
            con,
            stage="l1_physical",
            stage_version="l1-physical-v1",
            partition_keys=results,
        )
    finally:
        con.close()


def test_checkpoint_root_enforces_one_writer_fence_and_stage_abi(tmp_path):
    con = duckdb.connect()
    try:
        root = tmp_path / "checkpoints"
        binding = bounded_source_binding(_manifest())
        first = BoundedCheckpointStore(root, binding)
        with pytest.raises(RuntimeError, match="already has a writer"):
            BoundedCheckpointStore(root, binding)
        abi = bounded_stage_abi(con, 32)
        assert abi["market_bucket_count"] == 32
        assert abi["trade_id_bucket_count"] == 32
        assert len(abi["abi_sha256"]) == 64
        first.close()
        with BoundedCheckpointStore(root, binding) as resumed:
            assert resumed.root == root.resolve()
        with pytest.raises(RuntimeError, match="closed"):
            resumed.finalize_stage(
                con,
                stage="closed",
                stage_version="v1",
                partition_keys=["p00"],
            )
    finally:
        con.close()
