"""Focused tests for the local read-only Research Workbench."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("research_workbench", HERE / "workbench" / "app.py")
wb = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wb
SPEC.loader.exec_module(wb)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_registry_starts_every_atlas_hypothesis_unfrozen():
    registry = wb.load_hypotheses()
    expected = {
        "ATL-TTS-01",
        "ATL-BURST-01",
        "ATL-TOX-01",
        "ATL-RES-01",
        "ATL-FLOW-01",
        "ATL-WINDOW-01",
    }
    assert {item["hypothesis_id"] for item in registry["hypotheses"]} == expected
    assert {item["status"] for item in registry["hypotheses"]} == {"DRAFT_NEEDS_FREEZE"}


def test_build_aggregates_real_manifest_shape_and_only_writes_workbench(tmp_path):
    manifest = tmp_path / "work" / "warehouse" / "manifest.csv"
    _write(
        manifest,
        "date,table,category,subcategory,row_count,file_path,file_md5,created_ts\n"
        "2026-07-06,trades,Sports,Basketball,10,x,a,z\n"
        "2026-07-06,trades,Sports,Basketball,5,y,b,z\n"
        "2026-07-07,orderbooks_l1,Sports,Tennis,20,z,c,z\n"
        "2026-07-07,trades,Sports,Baseball,30,q,d,z\n"
        "2026-07-07,trades,Sports,Soccer,999,q,d,z\n"
        "2026-07-07,trades,Politics,Basketball,999,q,d,z\n",
    )
    snapshot = wb.build_workbench(
        repo_root=tmp_path,
        generated_at_utc="2026-07-11T00:00:00Z",
    )
    overview = snapshot["overview"]
    by_sport = {item["sport"]: item for item in overview["sports"]}
    assert by_sport["Basketball"]["total_rows"] == 15
    assert by_sport["Basketball"]["rows_by_table"] == {"trades": 15}
    assert by_sport["Tennis"]["by_date"] == [
        {"date": "2026-07-07", "table": "orderbooks_l1", "rows": 20}
    ]
    assert by_sport["Baseball"]["total_rows"] == 30
    assert by_sport["Soccer"]["total_rows"] == 999
    assert overview["dates"] == ["2026-07-06", "2026-07-07"]
    assert overview["source_manifest"]["sha256"]
    assert snapshot["liquidity"]["available"] is False
    written = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json"))
    assert written == [
        "sandbox/research/reports/workbench/experiments.json",
        "sandbox/research/reports/workbench/hypotheses.json",
        "sandbox/research/reports/workbench/liquidity.json",
        "sandbox/research/reports/workbench/main_events.json",
        "sandbox/research/reports/workbench/overview.json",
    ]


def test_liquidity_snapshot_uses_hourly_states_and_safe_deduplicated_trades(tmp_path):
    import duckdb

    l1_dir = (
        tmp_path
        / "work"
        / "warehouse"
        / "facts"
        / "orderbooks_l1"
        / "category=Sports"
        / "subcategory=Soccer"
        / "date=2026-07-06"
    )
    l1_dir.mkdir(parents=True)
    l1_path = l1_dir / "soccer.parquet"
    base_us = int(datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE l1 (
            ts_utc BIGINT, market_ticker VARCHAR, subcategory VARCHAR, date DATE,
            yes_bid_e4 INTEGER, yes_bid_qty_e4 BIGINT,
            yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT, is_snapshot BOOLEAN
        )
        """
    )
    connection.executemany(
        "INSERT INTO l1 VALUES (?, ?, 'Soccer', '2026-07-06', ?, ?, ?, ?, ?)",
        [
            (base_us, "A", 4000, 100000, 4200, 200000, True),
            (base_us + 1_000_000, "B", 3000, 50000, 3500, 30000, True),
            (base_us + 2_000_000, "C", 0, 0, 4000, 100000, True),
            (base_us + 3_000_000, "A", 4100, 100000, 4200, 100000, False),
        ],
    )
    connection.execute(f"COPY l1 TO '{l1_path}' (FORMAT PARQUET)")
    connection.close()

    trade_dir = (
        tmp_path
        / "work"
        / "warehouse"
        / "facts"
        / "trades"
        / "category=Sports"
        / "subcategory=Soccer"
        / "date=2026-07-06"
    )
    trade_dir.mkdir(parents=True)
    trade_path = trade_dir / "soccer.csv.gz"
    header = "ts_utc,market_ticker,series_ticker,event_ticker,category,subcategory,group,trade_id,yes_price_e4,no_price_e4,count_e4,taker_side\n"
    rows = [
        f"{base_us},A,S,E,Sports,Soccer,G,t1,4100,5900,20000,yes\n",
        f"{base_us},A,S,E,Sports,Soccer,G,t1,4100,5900,20000,yes\n",
        f"{base_us},B,S,E,Sports,Soccer,G,t2,3300,6700,10000,no\n",
        f"{base_us},C,S,E,Sports,Soccer,G,t3,4000,6000,10000,yes\n",
        f"{base_us},C,S,E,Sports,Soccer,G,t3,4000,6000,20000,yes\n",
    ]
    with gzip.open(trade_path, "wt", encoding="utf-8", newline="") as handle:
        handle.write(header)
        handle.writelines(rows)

    snapshot = wb.build_liquidity_snapshot(tmp_path, "2026-07-12T00:00:00Z")
    assert snapshot["available"] is True
    assert snapshot["sports"] == ["Soccer"]
    assert snapshot["analysis_unit"] == "market × UTC hour × archive date"
    hour = next(
        row
        for row in snapshot["slices"]
        if row["sport"] == "Soccer" and row["date"] == "2026-07-06" and row["hour_utc"] == 10
    )
    assert hour["availability"]["active_markets_p50"] == 3
    assert round(hour["availability"]["two_sided_share_p50"], 6) == 66.666667
    assert hour["metrics"]["spread_cents"]["n"] == 2
    assert hour["metrics"]["spread_cents"]["p50"] == 3.5
    assert hour["metrics"]["spread_cents"]["max"] == 5.0
    assert hour["metrics"]["touch_depth_contracts"]["p50"] == 6.5
    assert hour["metrics"]["l1_changes_per_hour"]["p50"] == 0
    assert hour["metrics"]["l1_changes_per_hour"]["max"] == 1
    assert hour["metrics"]["trade_count"]["p50"] == 1
    assert snapshot["trade_quality"] == {
        "raw_rows": 5,
        "safe_unique_trade_ids": 2,
        "conflicting_trade_ids_excluded": 1,
    }


def test_main_event_tape_separates_real_prints_from_aggregate_l1_adds(tmp_path):
    import duckdb

    l1_dir = (
        tmp_path
        / "work"
        / "warehouse"
        / "facts"
        / "orderbooks_l1"
        / "category=Sports"
        / "subcategory=Soccer"
        / "date=2026-07-06"
    )
    l1_dir.mkdir(parents=True)
    l1_path = l1_dir / "soccer.parquet"
    base_us = int(datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)
    event = "KXSOCCER-26JUL06TEST"
    market = f"{event}-YES"
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE l1 (
            ts_utc BIGINT, market_ticker VARCHAR, event_ticker VARCHAR,
            subcategory VARCHAR, "group" VARCHAR, date DATE,
            yes_bid_e4 INTEGER, yes_bid_qty_e4 BIGINT,
            yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT,
            price_e4 INTEGER, is_snapshot BOOLEAN
        )
        """
    )
    connection.executemany(
        "INSERT INTO l1 VALUES (?, ?, ?, 'Soccer', 'Winner', '2026-07-06', ?, ?, ?, ?, ?, ?)",
        [
            (base_us, market, event, 4000, 100000, 4200, 200000, 4100, True),
            (base_us + 1_000_000, market, event, 4000, 300000, 4200, 200000, 4100, False),
            (base_us + 2_000_000, market, event, 4000, 300000, 4200, 500000, 4100, False),
        ],
    )
    connection.execute(f"COPY l1 TO '{l1_path}' (FORMAT PARQUET)")
    connection.close()

    trade_dir = (
        tmp_path
        / "work"
        / "warehouse"
        / "facts"
        / "trades"
        / "category=Sports"
        / "subcategory=Soccer"
        / "date=2026-07-06"
    )
    trade_dir.mkdir(parents=True)
    trade_path = trade_dir / "soccer.csv.gz"
    header = "ts_utc,market_ticker,series_ticker,event_ticker,category,subcategory,group,trade_id,yes_price_e4,no_price_e4,count_e4,taker_side\n"
    rows = [
        f"{base_us},{market},S,{event},Sports,Soccer,Winner,t1,4100,5900,10000,yes\n",
        f"{base_us},{market},S,{event},Sports,Soccer,Winner,t1,4100,5900,10000,yes\n",
        f"{base_us + 1_000_000},{market},S,{event},Sports,Soccer,Winner,t2,4200,5800,1000000,no\n",
        f"{base_us + 2_000_000},{market},S,{event},Sports,Soccer,Winner,t3,4200,5800,20000,yes\n",
        f"{base_us + 2_000_000},{market},S,{event},Sports,Soccer,Winner,t3,4200,5800,30000,yes\n",
    ]
    with gzip.open(trade_path, "wt", encoding="utf-8", newline="") as handle:
        handle.write(header)
        handle.writelines(rows)

    snapshot = wb.build_main_events_snapshot(tmp_path, "2026-07-12T00:00:00Z")
    assert snapshot["available"] is True
    assert snapshot["sports"] == ["Soccer"]
    assert snapshot["trade_quality"] == {
        "raw_rows": 5,
        "safe_unique_trade_ids": 2,
        "conflicting_trade_ids_excluded": 1,
    }
    episode = snapshot["episodes"][0]
    assert episode["episode_key"] == "26JUL06TEST"
    selected = episode["markets"][0]
    assert selected["market_ticker"] == market
    candidates = {item["type"]: item for item in selected["candidates"]}
    assert set(candidates) == {"trade", "bid_add", "ask_add"}
    assert candidates["trade"]["reference"] == "t2"
    assert candidates["trade"]["contracts"] == 100.0
    assert "AGGREGATE_TOUCH_PROXY" not in candidates["trade"]["quality_flags"]
    assert candidates["bid_add"]["contracts"] == 20.0
    assert candidates["ask_add"]["contracts"] == 30.0
    assert "AGGREGATE_TOUCH_PROXY" in candidates["bid_add"]["quality_flags"]
    assert snapshot["score_alignment"]["captured_now"] is False
    assert snapshot["score_alignment"]["possible_prospectively"] is True
    assert episode["score_events"] == []


def test_unknown_and_missing_hypotheses_are_visible_as_invalid(tmp_path):
    experiments = tmp_path / "sandbox" / "research" / "reports" / "experiments"
    base = {
        "schema_version": "research-experiment-v1",
        "experiment_id": "e1",
        "title": "test",
        "result_status": "NO_RESULTS",
    }
    _write(experiments / "valid.json", json.dumps({**base, "hypothesis_id": "ATL-TTS-01"}))
    _write(experiments / "unknown.json", json.dumps({**base, "experiment_id": "e2", "hypothesis_id": "ATL-NOPE-01"}))
    _write(experiments / "missing.json", json.dumps({**base, "experiment_id": "e3"}))
    result = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "DRAFT_NEEDS_FREEZE"},
        "2026-07-11T00:00:00Z",
    )
    statuses = {item["artifact"]: item["status"] for item in result["experiments"]}
    assert statuses == {"missing.json": "INVALID", "unknown.json": "INVALID", "valid.json": "VALID"}
    assert result["invalid_count"] == 2
    assert {item["artifact"] for item in result["validation_errors"]} == {
        "missing.json",
        "unknown.json",
    }


def test_non_loopback_hosts_are_rejected():
    assert wb.is_loopback_host("127.0.0.1")
    assert wb.is_loopback_host("::1")
    assert wb.is_loopback_host("localhost")
    assert not wb.is_loopback_host("0.0.0.0")
    assert not wb.is_loopback_host("example.com")


def test_frontend_uses_only_local_api_data_without_embedded_results():
    html = (HERE / "workbench" / "index.html").read_text(encoding="utf-8")
    assert all(route in html for route in (
        "/api/overview", "/api/liquidity", "/api/main-events",
        "/api/hypotheses", "/api/experiments"
    ))
    assert "/assets/echarts.min.js" in html
    assert all(control in html for control in (
        "liquidity-sport", "liquidity-date", "liquidity-metric",
        "liquidity-time-chart", "liquidity-ecdf-chart",
        "liquidity-heatmap-chart", "liquidity-availability-chart",
    ))
    assert all(control in html for control in (
        "whale-sport", "whale-event", "whale-market", "whale-threshold",
        "whale-timeline-chart", "whale-trade-ecdf", "whale-touch-ecdf",
        "whale-table", "score-status",
    ))
    assert all(label in html for label in ("p50", "p99", "max", "market-hours"))
    assert "https://" not in html
    assert "http://" not in html
    assert "2,250,301" not in html
    assert "6,566,589" not in html
    assert "9,599,100" not in html


def test_optional_charts_and_tables_pass_through_and_must_be_lists(tmp_path):
    experiments = tmp_path / "experiments"
    chart = {
        "title": "Spread",
        "kind": "line",
        "x_label": "Minutes",
        "y_label": "Cents",
        "series": [{"name": "Basketball", "points": [{"x": 60, "y": 4.2}]}],
    }
    table = {"title": "Coverage", "columns": ["date", "rows"], "rows": [["2026-07-06", 10]]}
    base = {
        "schema_version": "research-experiment-v1",
        "experiment_id": "visual-e1",
        "hypothesis_id": "ATL-TTS-01",
        "title": "Visual result",
        "result_status": "EXPLORATORY",
    }
    _write(experiments / "valid.json", json.dumps({**base, "charts": [chart], "tables": [table]}))
    _write(
        experiments / "invalid.json",
        json.dumps({**base, "experiment_id": "visual-e2", "charts": {}, "tables": "not-a-list"}),
    )
    result = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "FROZEN_EXPLORATORY"},
        "2026-07-11T00:00:00Z",
    )
    entries = {item["artifact"]: item for item in result["experiments"]}
    assert entries["valid.json"]["status"] == "VALID"
    assert entries["valid.json"]["charts"] == [chart]
    assert entries["valid.json"]["tables"] == [table]
    assert entries["invalid.json"]["status"] == "INVALID"
    assert entries["invalid.json"]["charts"] == []
    assert entries["invalid.json"]["tables"] == []
    assert entries["invalid.json"]["validation_errors"] == [
        "charts must be a list when present",
        "tables must be a list when present",
    ]


def test_registry_status_can_advance_and_results_require_frozen_hypothesis(tmp_path):
    registry = wb.load_hypotheses()
    registry["hypotheses"][0]["status"] = "FROZEN_TRAIN"
    registry_path = tmp_path / "hypotheses.json"
    _write(registry_path, json.dumps(registry))
    assert wb.load_hypotheses(registry_path)["hypotheses"][0]["status"] == "FROZEN_TRAIN"

    experiments = tmp_path / "experiments"
    base = {
        "schema_version": "research-experiment-v1",
        "hypothesis_id": "ATL-TTS-01",
        "title": "Lifecycle test",
    }
    _write(
        experiments / "draft-result.json",
        json.dumps({**base, "experiment_id": "e1", "result_status": "EXPLORATORY"}),
    )
    _write(
        experiments / "draft-plan.json",
        json.dumps({**base, "experiment_id": "e2", "result_status": "NO_RESULTS"}),
    )
    draft = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "DRAFT_NEEDS_FREEZE"},
        "2026-07-11T00:00:00Z",
    )
    draft_entries = {item["artifact"]: item for item in draft["experiments"]}
    assert draft_entries["draft-result.json"]["status"] == "INVALID"
    assert draft_entries["draft-plan.json"]["status"] == "VALID"
    assert "require a hypothesis status beginning FROZEN_" in draft_entries[
        "draft-result.json"
    ]["validation_errors"][0]

    frozen = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "FROZEN_TRAIN"},
        "2026-07-11T00:00:00Z",
    )
    assert {item["status"] for item in frozen["experiments"]} == {"VALID"}
