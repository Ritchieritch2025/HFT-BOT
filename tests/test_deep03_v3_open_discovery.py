#!/usr/bin/env python3
"""Contracts for the exact-V3 Deep03 OPEN_DISCOVERY / D3-W2A runner."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "research"))
sys.path.insert(0, str(ROOT / "deploy" / "w09"))

import deep03_one_shot_arm as one_shot  # noqa: E402
import research_data as rd  # noqa: E402
import deep03_authority_gate  # noqa: E402
from deep03_v3_common import (  # noqa: E402
    Deep03InputError,
    validate_explicit_releases,
)
from deep03_v3_methods import (  # noqa: E402
    L1_INTERVAL_COLUMNS,
    _l1_interval_select_sql,
    _manifest_release_dates,
    _materialize_l1_intervals,
    execute_all,
)
from deep03_v3_prepare import prepare_run  # noqa: E402
from deep03_v3_runner import run_discovery  # noqa: E402
from test_research_reference_consumer import (  # noqa: E402
    _store_for,
    build_release,
)
from test_w09_exploratory_autoresearch import (  # noqa: E402
    _claimed_authority_files,
    _materialize_degraded,
)


DEEP03_MODULES = {
    "tools/research/deep03_v3_common.py",
    "tools/research/deep03_v3_w1_preflight.py",
    "tools/research/deep03_v3_prepare.py",
    "tools/research/deep03_v3_methods.py",
    "tools/research/deep03_v3_runner.py",
}


def _materialize(tmp_path: Path, *, with_rfq: bool = False) -> tuple[Path, str]:
    cache = tmp_path / "cache"
    rid, manifest, source = build_release(with_rfq=with_rfq)
    assert rd.cmd_fetch(_store_for((rid, manifest, source)), str(cache), rid, False) == 0
    return cache, rid


def test_input_gate_requires_explicit_v3_and_rejects_latest_or_v2(tmp_path):
    with pytest.raises(Deep03InputError, match="explicit --release"):
        validate_explicit_releases(tmp_path, [])
    with pytest.raises(Deep03InputError, match="latest/newest/auto"):
        validate_explicit_releases(tmp_path, ["latest"])

    rid = "2026-07-17__seal-aaaaaaaa__pub-bbbbbbbbbbbbbbbb"
    release = tmp_path / "releases" / rid
    release.mkdir(parents=True)
    (release / "MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": "research-release-manifest-v2",
                "release_id": rid,
                "version_binding": {"mode": "VERSION_BOUND"},
            }
        )
    )
    (release / ".VERIFIED.json").write_text(json.dumps({"release_id": rid}))
    with pytest.raises(Deep03InputError, match="strict V3 manifest gate"):
        validate_explicit_releases(tmp_path, [rid])


def test_input_gate_rejects_rfq_and_marker_manifest_drift(tmp_path):
    rfq_cache, rfq_rid = _materialize(tmp_path / "rfq", with_rfq=True)
    with pytest.raises(
        Deep03InputError,
        match="VERIFIED marker does not bind|RFQ|exact Deep03 semantic lock",
    ):
        validate_explicit_releases(rfq_cache, [rfq_rid])

    cache, rid = _materialize_degraded(tmp_path / "drift")
    marker_path = cache / "releases" / rid / ".VERIFIED.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(Deep03InputError, match="does not bind V3 manifest"):
        validate_explicit_releases(cache, [rid])


def test_input_gate_refuses_a_different_evidence_tier(tmp_path):
    cache, rid = _materialize(tmp_path)
    with pytest.raises(Deep03InputError, match="exact Deep03 semantic lock"):
        validate_explicit_releases(cache, [rid])


def test_tiny_exact_v3_end_to_end_writes_self_contained_atomic_report(
    tmp_path, monkeypatch
):
    # This runner unit intentionally uses one tiny synthetic release.  The
    # production gate's separate contract tests retain the exact eight-release
    # W1/W2A requirement.
    monkeypatch.setattr(deep03_authority_gate, "W1_EXACT_RELEASE_COUNT", 1)
    cache, rid = _materialize_degraded(tmp_path)
    records = validate_explicit_releases(cache, [rid])
    expected_object_count = len(records[0]["objects"])
    expected_object_bytes = sum(
        int(item["size"]) for item in records[0]["objects"]
    )
    monkeypatch.setattr(
        deep03_authority_gate, "EXPECTED_OBJECT_COUNT", expected_object_count
    )
    monkeypatch.setattr(
        deep03_authority_gate, "EXPECTED_OBJECT_BYTES", expected_object_bytes
    )
    (
        _gate,
        authority,
        arm,
        plan,
        runtime,
        audit,
        w0,
        w1,
        w1_complete,
        claim_root,
        invocation_id,
        proc_cgroup,
    ) = _claimed_authority_files(
        tmp_path / "authority",
        [rid],
        expected_object_count=expected_object_count,
        expected_object_bytes=expected_object_bytes,
    )
    authority_kwargs = {
        "authority_path": authority,
        "arm_path": arm,
        "arm_claim_root": claim_root,
        "plan_path": plan,
        "runtime_commit_path": runtime,
        "audit_path": audit,
        "w0_release_path": w0,
        "w1_release_path": w1,
        "w1_complete_path": w1_complete,
        "expected_owner_uid": os.getuid(),
        "claim_invocation_id": invocation_id,
        "claim_proc_cgroup_path": proc_cgroup,
    }
    run_dir = prepare_run(
        cache_root=cache,
        run_root=tmp_path / "runs",
        run_id="tiny-v3-open-discovery",
        release_ids=[rid],
        **authority_kwargs,
    )
    complete_path = run_discovery(
        run_dir=run_dir, memory_limit="1GB", threads=1, **authority_kwargs
    )
    complete = json.loads(complete_path.read_text())
    method = json.loads((run_dir / "METHOD_EXECUTION_RECEIPT.json").read_text())
    report = (run_dir / "REPORT" / "index.html").read_text()

    assert complete["state"] == "RUN_COMPLETE"
    assert complete["mode"] == "MODE 1 / EXPLORATORY_AUTORESEARCH"
    assert complete["strict_acceptance_claimed"] is False
    assert complete["release_ids"] == [rid]
    assert complete["all_declared_methods_closed"] is True
    manifest = json.loads((run_dir / "INPUT_MANIFEST.json").read_text())
    assert complete["authority_binding"] == manifest["authority_binding"]
    assert complete["authority_artifact_sha256s"] == manifest[
        "authority_artifact_sha256s"
    ]
    for name in (
        "ADOPTED_PLAN.md",
        "ARM_CLAIM.json",
        "AUDIT.md",
        "AUTHORITY.json",
        "BASE_COMMIT.txt",
        "D3_W0_RELEASE.json",
        "D3_W1_RELEASE.json",
        "EXECUTION_ARM.json",
        "W1_COMPLETE.json",
    ):
        assert hashlib.sha256((run_dir / name).read_bytes()).hexdigest() == manifest[
            "authority_artifact_sha256s"
        ][name]
    assert method["declared_methods"] == [
        "D3-B01-MARKOUT",
        "D3-B02-ONESIDE",
        "D3-B03-XMKT",
        "D3-B04-RHYTHM",
    ]
    assert all(
        row["method_status"] in {"EXECUTED", "NOT_ESTIMABLE"}
        for row in method["methods"]
    )
    assert "SEALED_PENDING_QUALITY_ASSESSMENT" in report
    assert "EXPLORATORY_ONLY" in report
    assert "MODE 1 / EXPLORATORY_AUTORESEARCH" in report
    assert "NOT STRICT ACCEPTANCE" in report
    assert "<svg" in report or "Chart unavailable" in report
    assert "http://" not in report and "https://" not in report

    sums = (run_dir / "ARTIFACT_SHA256SUMS").read_text().splitlines()
    assert sums
    for line in sums:
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((run_dir / relative).read_bytes()).hexdigest() == digest
    assert complete_path.stat().st_mtime_ns >= max(
        path.stat().st_mtime_ns
        for path in run_dir.rglob("*")
        if path.is_file() and path != complete_path
    )
    with pytest.raises(Deep03InputError, match="RUN_COMPLETE is immutable"):
        run_discovery(
            run_dir=run_dir,
            memory_limit="1GB",
            threads=1,
            **authority_kwargs,
        )


def test_prepare_and_runner_cannot_bypass_or_drift_exact_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(deep03_authority_gate, "W1_EXACT_RELEASE_COUNT", 1)
    cache, rid = _materialize_degraded(tmp_path)
    records = validate_explicit_releases(cache, [rid])
    expected_object_count = len(records[0]["objects"])
    expected_object_bytes = sum(
        int(item["size"]) for item in records[0]["objects"]
    )
    monkeypatch.setattr(
        deep03_authority_gate, "EXPECTED_OBJECT_COUNT", expected_object_count
    )
    monkeypatch.setattr(
        deep03_authority_gate, "EXPECTED_OBJECT_BYTES", expected_object_bytes
    )
    (
        _gate,
        authority,
        arm,
        plan,
        runtime,
        audit,
        w0,
        w1,
        w1_complete,
        claim_root,
        invocation_id,
        proc_cgroup,
    ) = _claimed_authority_files(
        tmp_path / "authority",
        [rid],
        expected_object_count=expected_object_count,
        expected_object_bytes=expected_object_bytes,
    )
    authority_kwargs = {
        "authority_path": authority,
        "arm_path": arm,
        "arm_claim_root": claim_root,
        "plan_path": plan,
        "runtime_commit_path": runtime,
        "audit_path": audit,
        "w0_release_path": w0,
        "w1_release_path": w1,
        "w1_complete_path": w1_complete,
        "expected_owner_uid": os.getuid(),
        "claim_invocation_id": invocation_id,
        "claim_proc_cgroup_path": proc_cgroup,
    }
    empty_claim_root = tmp_path / "unclaimed"
    empty_claim_root.mkdir(mode=0o750)
    with pytest.raises(Deep03InputError, match="one-shot.*never claimed"):
        prepare_run(
            cache_root=cache,
            run_root=tmp_path / "unclaimed-runs",
            run_id="direct-prepare-without-claim-refused",
            release_ids=[rid],
            **{**authority_kwargs, "arm_claim_root": empty_claim_root},
        )
    authority.chmod(0o644)
    with pytest.raises(Deep03InputError, match="exact authority refused.*writable"):
        prepare_run(
            cache_root=cache,
            run_root=tmp_path / "refused-runs",
            run_id="authority-bypass-refused",
            release_ids=[rid],
            **authority_kwargs,
        )
    authority.chmod(0o444)
    run_dir = prepare_run(
        cache_root=cache,
        run_root=tmp_path / "runs",
        run_id="authority-drift-refused",
        release_ids=[rid],
        **authority_kwargs,
    )
    copied = run_dir / "AUTHORITY.json"
    copied.write_bytes(copied.read_bytes() + b"\n")
    with pytest.raises(
        Deep03InputError, match="run authority artifact differs from exact authority"
    ):
        run_discovery(
            run_dir=run_dir,
            memory_limit="1GB",
            threads=1,
            **authority_kwargs,
        )

    consumed_run = prepare_run(
        cache_root=cache,
        run_root=tmp_path / "consumed-runs",
        run_id="runner-consumed-claim-refused",
        release_ids=[rid],
        **authority_kwargs,
    )
    identity = one_shot.load_arm_identity(
        authority_path=authority,
        arm_path=arm,
        expected_owner_uid=os.getuid(),
    )
    one_shot.consume_one_shot(
        claim_root=claim_root,
        identity=identity,
        invocation_id=invocation_id,
        service_result="failure",
        exit_code="exited",
        exit_status="2",
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
    )
    with pytest.raises(Deep03InputError, match="one-shot.*field mismatch: state"):
        run_discovery(
            run_dir=consumed_run,
            memory_limit="1GB",
            threads=1,
            **authority_kwargs,
        )
    with pytest.raises(Deep03InputError, match="one-shot.*field mismatch: state"):
        prepare_run(
            cache_root=cache,
            run_root=tmp_path / "replay-runs",
            run_id="same-arm-second-prepare-refused",
            release_ids=[rid],
            **authority_kwargs,
        )


def _synthetic_method_input(tmp_path: Path) -> dict:
    l1 = (
        tmp_path
        / "facts/orderbooks_l1/category=Sports/subcategory=Tennis/"
        "date=2026-07-17/part.parquet"
    )
    trades = (
        tmp_path
        / "facts/trades/category=Sports/subcategory=Tennis/"
        "date=2026-07-17/part.parquet"
    )
    markets = tmp_path / "dim/snapshots/date=2026-07-17/markets.csv"
    for path in (l1, trades, markets):
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        '''CREATE TABLE l1(
          local_recv_ts_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
          ws_sid BIGINT,ws_seq BIGINT,market_ticker VARCHAR,event_ticker VARCHAR,
          series_ticker VARCHAR,subcategory VARCHAR,"group" VARCHAR,
          yes_bid_e4 BIGINT,yes_ask_e4 BIGINT,yes_bid_qty_e4 BIGINT,
          yes_ask_qty_e4 BIGINT)'''
    )
    t0 = 1_784_246_400_000_000
    l1_rows = [
        (t0, 1, 1, 1, 1, "M1", "E1", "S", "Tennis", "ATP", 4000, 4200, 10000, 10000),
        (t0 + 1_000_000, 2, 2, 1, 2, "M1", "E1", "S", "Tennis", "ATP", 4100, None, 10000, None),
        (t0 + 2_000_000, 3, 3, 1, 3, "M1", "E1", "S", "Tennis", "ATP", 4200, 4400, 10000, 10000),
        (t0 + 4_000_000, 4, 4, 1, 4, "M1", "E1", "S", "Tennis", "ATP", 4500, 4700, 10000, 10000),
        (t0, 1, 1, 1, 1, "M2", "E1", "S", "Tennis", "ATP", 5000, 5200, 10000, 10000),
        (t0 + 4_000_000, 4, 4, 1, 4, "M2", "E1", "S", "Tennis", "ATP", 5100, 5300, 10000, 10000),
    ]
    con.executemany("INSERT INTO l1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", l1_rows)
    con.execute(f"COPY l1 TO '{str(l1).replace(chr(39), chr(39)*2)}' (FORMAT PARQUET)")
    con.execute(
        '''CREATE TABLE trades(
          local_recv_ts_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
          market_ticker VARCHAR,event_ticker VARCHAR,series_ticker VARCHAR,
          subcategory VARCHAR,"group" VARCHAR,trade_id VARCHAR,yes_price_e4 BIGINT,
          no_price_e4 BIGINT,count_e4 BIGINT,taker_side VARCHAR)'''
    )
    con.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            t0 + 500_000,
            10,
            10,
            "M1",
            "E1",
            "S",
            "Tennis",
            "ATP",
            "trade-1",
            4200,
            5800,
            10000,
            "yes",
        ],
    )
    con.execute(
        f"COPY trades TO '{str(trades).replace(chr(39), chr(39)*2)}' (FORMAT PARQUET)"
    )
    con.close()
    markets.write_text(
        "ticker,event_ticker,occurrence_datetime,close_time\n"
        "M1,E1,2026-07-17T12:00:00Z,2026-07-17T20:00:00Z\n"
        "M2,E1,2026-07-17T12:00:00Z,2026-07-17T20:00:00Z\n"
    )

    objects = []
    for channel, path in (("orderbooks_l1", l1), ("trades", trades)):
        local = "facts/" + str(path).split("/facts/", 1)[1]
        objects.append(
            {
                "kind": "facts",
                "channel": channel,
                "date": "2026-07-17",
                "logical_key": "warehouse/" + local,
                "local_path": str(path),
                "release_id": "fixture-v3",
                "source_version_id": f"version-{channel}",
                "sha256": "a" * 64,
                "row_count": len(l1_rows) if channel == "orderbooks_l1" else 1,
            }
        )
    objects.append(
        {
            "kind": "dim_snapshot",
            "channel": None,
            "date": "2026-07-17",
            "logical_key": "warehouse/dim/snapshots/date=2026-07-17/markets.csv",
            "local_path": str(markets),
            "release_id": "fixture-v3",
            "source_version_id": "version-markets",
            "sha256": "b" * 64,
            "row_count": None,
        }
    )
    return {
        "release_ids": ["fixture-v3"],
        "release_dates": ["2026-07-17"],
        "releases": [{"release_id": "fixture-v3", "date": "2026-07-17"}],
        "objects": objects,
    }


def test_corrected_methods_execute_or_close_not_estimable_with_evidence(
    tmp_path, capsys
):
    con = duckdb.connect()
    try:
        _capabilities, methods = execute_all(con, _synthetic_method_input(tmp_path))
        assert con.execute(
            "SELECT count(*) FROM b01_pre WHERE pre_book_t_us>=t_us"
        ).fetchone()[0] == 0
        for horizon in (1_000_000, 5_000_000, 30_000_000):
            assert con.execute(
                f"SELECT count(*) FROM b01_h{horizon} "
                "WHERE outcome_book_t_us<=t_us OR outcome_book_t_us>target_us"
            ).fetchone()[0] == 0
        assert con.execute(
            "SELECT count(*) FROM l1_intervals WHERE right_censored AND duration_us<>0"
        ).fetchone()[0] == 0
        assert {
            row[0] for row in con.execute("DESCRIBE l1_intervals").fetchall()
        } == {
            "date",
            "t_us",
            "market_ticker",
            "event_proxy",
            "sport",
            "book_state",
            "right_censored",
            "starts_in_gap",
            "interval_end_us",
            "duration_us",
            "phase",
        }
    finally:
        con.close()
    assert capsys.readouterr().out.splitlines() == [
        "D3_W2A_STAGE l1_intervals date=2026-07-17 state=START",
        "D3_W2A_STAGE l1_intervals date=2026-07-17 state=COMPLETE",
        "D3_W2A_STAGE trade_id_qc state=START",
        "D3_W2A_STAGE trade_id_qc state=COMPLETE",
        "D3_W2A_STAGE trades_dedup state=START",
        "D3_W2A_STAGE trades_dedup state=COMPLETE",
    ]
    status = {method["method_id"]: method["status"] for method in methods}
    assert status == {
        "D3-B01-MARKOUT": "EXECUTED",
        "D3-B02-ONESIDE": "EXECUTED",
        "D3-B03-XMKT": "NOT_ESTIMABLE",
        "D3-B04-RHYTHM": "EXECUTED",
    }
    b01 = next(method for method in methods if method["method_id"] == "D3-B01-MARKOUT")
    assert all(row["n"] > 0 for row in b01["summary"])
    assert all("query_id" in row for row in b01["summary"])
    b02 = next(method for method in methods if method["method_id"] == "D3-B02-ONESIDE")
    assert b02["incidence"]["one_sided_exposure_us"] == 1_000_000
    assert any(row["book_state"] == "ONE_SIDED" for row in b02["summary"])
    b03 = next(method for method in methods if method["method_id"] == "D3-B03-XMKT")
    assert b03["estimability_evidence"]["linked_event_proxy_candidates"] == 1
    assert "PAYOUT_EXHAUSTIVENESS_NOT_AUTHENTICATED" in b03["estimability_evidence"]["blocking_reasons"]
    b04 = next(method for method in methods if method["method_id"] == "D3-B04-RHYTHM")
    assert b04["summary"][0]["active_market_minutes"] == 2
    assert b04["summary"][0]["trades_per_active_market_minute"] == 0.5


def test_chunked_l1_intervals_equal_legacy_single_query_for_ties_gaps_and_censor():
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TEMP TABLE capture_gaps("
            "date DATE,start_us BIGINT,end_us BIGINT)"
        )
        con.execute(
            "INSERT INTO capture_gaps VALUES "
            "(DATE '2026-07-17',250,275),(DATE '2026-07-18',1150,1175)"
        )
        con.execute(
            "CREATE TEMP TABLE l1_enriched("
            "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
            "sport VARCHAR,book_state VARCHAR,occurrence_us BIGINT,"
            "close_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,"
            "ws_sid BIGINT,ws_seq BIGINT)"
        )
        con.executemany(
            "INSERT INTO l1_enriched VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                # Same receive timestamp, resolved only by the retained causal
                # tie-breaks: ONE_SIDED leads TWO_SIDED by recv_wall_ns.
                ("2026-07-17", 100, "M1", "E1", "Tennis", "TWO_SIDED", 150, 500, 20, 2, 1, 2),
                ("2026-07-17", 100, "M1", "E1", "Tennis", "ONE_SIDED", 150, 500, 10, 1, 1, 1),
                # This interval is cut at the next capture gap (250), while
                # the following state begins inside that gap and gets zero.
                ("2026-07-17", 200, "M1", "E1", "Tennis", "TWO_SIDED", 150, 500, 30, 3, 1, 3),
                ("2026-07-17", 260, "M1", "E1", "Tennis", "ONE_SIDED", 150, 500, 40, 4, 1, 4),
                ("2026-07-17", 400, "M1", "E1", "Tennis", "INVALID_BOOK", 150, 500, 50, 5, 1, 5),
                # The second date proves lead() never crosses a date boundary.
                ("2026-07-18", 1000, "M1", "E2", "Soccer", "TWO_SIDED", 1100, 1400, 10, 1, 2, 1),
                ("2026-07-18", 1200, "M1", "E2", "Soccer", "ONE_SIDED", 1100, 1400, 20, 2, 2, 2),
            ],
        )
        con.execute(
            "CREATE TEMP TABLE l1_intervals_legacy AS "
            + _l1_interval_select_sql(None)
        )
        _materialize_l1_intervals(con, ("2026-07-18", "2026-07-17"))

        assert con.execute(
            "SELECT count(*) FROM ("
            "SELECT * FROM l1_intervals_legacy EXCEPT ALL "
            "SELECT * FROM l1_intervals)"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT count(*) FROM ("
            "SELECT * FROM l1_intervals EXCEPT ALL "
            "SELECT * FROM l1_intervals_legacy)"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT book_state,duration_us FROM l1_intervals "
            "WHERE date=DATE '2026-07-17' AND t_us=100 "
            "ORDER BY book_state"
        ).fetchall() == [("ONE_SIDED", 0), ("TWO_SIDED", 100)]
        assert con.execute(
            "SELECT interval_end_us,duration_us FROM l1_intervals "
            "WHERE date=DATE '2026-07-17' AND t_us=200"
        ).fetchone() == (250, 50)
        assert con.execute(
            "SELECT starts_in_gap,duration_us FROM l1_intervals "
            "WHERE date=DATE '2026-07-17' AND t_us=260"
        ).fetchone() == (True, 0)
        assert con.execute(
            "SELECT count(*) FROM l1_intervals "
            "WHERE right_censored AND duration_us=0"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT DISTINCT phase FROM l1_intervals "
            "WHERE date=DATE '2026-07-17' ORDER BY phase"
        ).fetchall() == [
            ("POST_SCHEDULED_START_PROXY",),
            ("PRE_SCHEDULED_START",),
        ]
        assert [
            (row[0], row[1])
            for row in con.execute("DESCRIBE l1_intervals").fetchall()
        ] == [
            ("date", "DATE"),
            ("t_us", "BIGINT"),
            ("market_ticker", "VARCHAR"),
            ("event_proxy", "VARCHAR"),
            ("sport", "VARCHAR"),
            ("book_state", "VARCHAR"),
            ("right_censored", "BOOLEAN"),
            ("starts_in_gap", "BOOLEAN"),
            ("interval_end_us", "BIGINT"),
            ("duration_us", "BIGINT"),
            ("phase", "VARCHAR"),
        ]
        assert tuple(row[0] for row in con.execute(
            "DESCRIBE l1_intervals"
        ).fetchall()) == L1_INTERVAL_COLUMNS
    finally:
        con.close()


def test_l1_interval_materializer_uses_only_sorted_exact_manifest_date_inserts(
    capsys,
):
    class RecordingConnection:
        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(" ".join(sql.split()))
            return self

    con = RecordingConnection()
    _materialize_l1_intervals(con, ("2026-07-18", "2026-07-17"))
    assert capsys.readouterr().out.splitlines() == [
        "D3_W2A_STAGE l1_intervals date=2026-07-17 state=START",
        "D3_W2A_STAGE l1_intervals date=2026-07-17 state=COMPLETE",
        "D3_W2A_STAGE l1_intervals date=2026-07-18 state=START",
        "D3_W2A_STAGE l1_intervals date=2026-07-18 state=COMPLETE",
    ]
    assert len(con.statements) == 3
    assert con.statements[0].startswith(
        "CREATE OR REPLACE TEMP TABLE l1_intervals(date DATE,t_us BIGINT"
    )
    inserts = con.statements[1:]
    assert [
        "DATE '2026-07-17'" in inserts[0],
        "DATE '2026-07-18'" in inserts[1],
    ] == [True, True]
    for statement in inserts:
        assert statement.startswith(
            "INSERT INTO l1_intervals(date,t_us,market_ticker,event_proxy,"
        )
        assert statement.count("DATE '") == 1
        assert "FROM l1_enriched l WHERE l.date=DATE '" in statement
        assert "PARTITION BY date,market_ticker" in statement
        assert (
            "ORDER BY t_us,coalesce(recv_wall_ns,0), "
            "coalesce(recv_mono_ns,0),coalesce(ws_sid,0), "
            "coalesce(ws_seq,0)"
        ) in statement
    assert not any("preserve_insertion_order" in statement for statement in inserts)


def test_l1_interval_dates_are_manifest_bound_and_reject_code_shaped_input():
    manifest = {
        "release_dates": ["2026-07-18", "2026-07-17"],
        "releases": [
            {"date": "2026-07-17"},
            {"date": "2026-07-18"},
        ],
        "objects": [
            {
                "kind": "facts",
                "channel": "orderbooks_l1",
                "date": "2026-07-17",
                "logical_key": "warehouse/facts/category=Sports/date=2026-07-17/a.parquet",
            },
            {
                "kind": "facts",
                "channel": "orderbooks_l1",
                "date": "2026-07-18",
                "logical_key": "warehouse/facts/category=Sports/date=2026-07-18/a.parquet",
            },
        ],
    }
    assert _manifest_release_dates(manifest) == ("2026-07-17", "2026-07-18")

    manifest["release_dates"][1] = "2026-07-17'; DROP TABLE l1_enriched;--"
    with pytest.raises(ValueError, match="invalid Deep03 release date"):
        _manifest_release_dates(manifest)


def test_w09_payload_sha_pins_and_installs_every_deep03_module():
    manifest_path = ROOT / "deploy/w09/deep03_open_discovery_modules.sha256"
    rows = {}
    for line in manifest_path.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        rows[relative] = digest
    assert set(rows) == DEEP03_MODULES
    for relative, expected in rows.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected

    push = (ROOT / "deploy/w09/push_and_install.sh").read_text()
    install = (ROOT / "deploy/w09/install_on_host.sh").read_text()
    for relative in DEEP03_MODULES:
        name = Path(relative).name
        assert f'cp "$SOURCE_REPO/tools/research/$module"' in push
        assert f'install -m 0644 "$PAYLOAD_ROOT/tools/research/$module"' in install
        assert name in (ROOT / relative).name
    assert "deep03_open_discovery_modules.sha256" in push
    assert "deep03_open_discovery_modules.sha256" in install
    assert "/usr/local/bin/deep03-v3-prepare" in install
    assert "/usr/local/bin/deep03-v3-w1-preflight" in install
    assert "/usr/local/bin/deep03-v3-run" in install
