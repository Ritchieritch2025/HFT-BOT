import importlib.util
import json
from pathlib import Path

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools" / "research" / "deep03_fullscope_graph.py"


def _load():
    spec = importlib.util.spec_from_file_location("deep03_fullscope_graph", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _write_fact(path: Path, rows: list[tuple[str, str, int]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE x(date DATE,market_ticker VARCHAR,value BIGINT)")
    con.executemany("INSERT INTO x VALUES (?,?,?)", rows)
    escaped = str(path).replace("'", "''")
    con.execute(f"COPY x TO '{escaped}' (FORMAT PARQUET)")
    con.close()
    return len(rows)


def _obj(path: Path, *, date: str, logical: str, kind: str, channel=None, rows=None):
    raw = path.read_bytes()
    import hashlib

    return {
        "release_id": f"release-{date}",
        "date": date,
        "logical_key": logical,
        "local_path": str(path),
        "source_version_id": "v-" + path.name,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "kind": kind,
        "channel": channel,
        "row_count": rows,
    }


def _fixture(tmp_path: Path):
    objects = []
    for date in ("2026-07-10", "2026-07-12"):
        root = tmp_path / date
        series = root / "series.csv"
        events = root / "events.csv"
        markets = root / "markets.csv"
        _write_csv(
            series,
            "ticker,title,category,fee_type,fee_multiplier",
            ["S1,League,Sports,quadratic,1.0"],
        )
        _write_csv(
            events,
            "event_ticker,series_ticker,title,category,mutually_exclusive,last_updated_ts",
            ["E1,S1,Alpha vs Beta,Sports,true,2026-07-10T00:00:00Z"],
        )
        _write_csv(
            markets,
            "ticker,event_ticker,title,market_type,event_structure,occurrence_datetime,close_time,updated_time",
            ["M1,E1,Winner,binary,game,2026-07-12T12:00:00Z,2026-07-12T13:00:00Z,2026-07-10T00:00:00Z"],
        )
        for name, path in (("series", series), ("events", events), ("markets", markets)):
            objects.append(
                _obj(
                    path,
                    date=date,
                    logical=f"warehouse/dim/snapshots/date={date}/{name}.csv",
                    kind="dim_snapshot",
                )
            )
    l1_10 = tmp_path / "facts" / "l1_10.parquet"
    l1_12 = tmp_path / "facts" / "l1_12.parquet"
    trades = tmp_path / "facts" / "trades.parquet"
    l2 = tmp_path / "facts" / "l2.parquet"
    counts = {
        "l1_10": _write_fact(l1_10, [("2026-07-10", "M1", 1)]),
        "l1_12": _write_fact(l1_12, [("2026-07-12", "M1", 2)]),
        "trades": _write_fact(trades, [("2026-07-12", "M1", 1)]),
        "orderbooks_full": _write_fact(
            l2,
            [("2026-07-12", "M1", 1), ("2026-07-12", "M1", 2)],
        ),
    }
    for channel, date, path, count_key in (
        ("orderbooks_l1", "2026-07-10", l1_10, "l1_10"),
        ("orderbooks_l1", "2026-07-12", l1_12, "l1_12"),
        ("trades", "2026-07-12", trades, "trades"),
        ("orderbooks_full", "2026-07-12", l2, "orderbooks_full"),
    ):
        objects.append(
            _obj(
                path,
                date=date,
                logical=f"warehouse/facts/{channel}/category=Sports/date={date}/{path.name}",
                kind="facts",
                channel=channel,
                rows=counts[count_key],
            )
        )
    catalog = tmp_path / "catalog.parquet"
    _write_fact(catalog, [("2026-07-12", "M1", 1)])
    objects.append(
        _obj(
            catalog,
            date="2026-07-12",
            logical="warehouse/catalog/markets/part-00000.parquet",
            kind="catalog",
            channel="catalog",
        )
    )
    return {
        "release_dates": ["2026-07-10", "2026-07-12"],
        "objects": objects,
    }


def test_graph_scans_all_fact_rows_and_exposes_absent_l2_date(tmp_path):
    module = _load()
    manifest = _fixture(tmp_path)
    con = duckdb.connect()
    result = module.build_market_graph(con, manifest, output_dir=tmp_path / "out")
    assert result["status"] == "EXECUTED_DESCRIPTIVE_ONLY"
    assert result["support_conservation"]["source_rows"] == {
        "orderbooks_full": 2,
        "orderbooks_l1": 2,
        "trades": 1,
    }
    assert result["l2_present_dates"] == ["2026-07-12"]
    assert result["l2_absent_dates"] == ["2026-07-10"]
    rows = con.execute(
        "SELECT cast(date AS VARCHAR),l1_rows,trade_rows,l2_rows,mapping_status "
        "FROM fullscope_graph ORDER BY date"
    ).fetchall()
    assert rows == [
        ("2026-07-10", 1, 0, 0, "CANONICAL_FAMILY_EVENT"),
        ("2026-07-12", 1, 1, 2, "CANONICAL_FAMILY_EVENT"),
    ]
    receipt = json.loads((tmp_path / "out" / "MARKET_GRAPH_RECEIPT.json").read_text())
    assert receipt["claims"]["arbitrage_or_pnl"] is False
    assert (tmp_path / "out" / "MARKET_GRAPH.parquet").is_file()


def test_graph_fails_closed_on_declared_fact_row_drift(tmp_path):
    module = _load()
    manifest = _fixture(tmp_path)
    fact = next(row for row in manifest["objects"] if row["kind"] == "facts")
    fact["row_count"] += 1
    with pytest.raises(module.MarketGraphError, match="row conservation failed"):
        module.build_market_graph(duckdb.connect(), manifest)


@pytest.mark.parametrize("declared", [None, True, -1, "2", 2.0])
def test_standalone_graph_requires_exact_fact_row_count(tmp_path, declared):
    module = _load()
    manifest = _fixture(tmp_path)
    fact = next(row for row in manifest["objects"] if row["kind"] == "facts")
    fact["row_count"] = declared
    with pytest.raises(module.MarketGraphError, match="row_count is mandatory"):
        module.build_market_graph(duckdb.connect(), manifest)


def test_graph_keeps_dimension_conflict_visible(tmp_path):
    module = _load()
    manifest = _fixture(tmp_path)
    market_obj = next(
        row
        for row in manifest["objects"]
        if row["kind"] == "dim_snapshot" and row["logical_key"].endswith("markets.csv")
    )
    path = Path(market_obj["local_path"])
    path.write_text(
        path.read_text()
        + "M1,E1,Conflicting title,binary,game,2026-07-12T12:00:00Z,2026-07-12T13:00:00Z,2026-07-10T00:00:00Z\n",
        encoding="utf-8",
    )
    con = duckdb.connect()
    result = module.build_market_graph(con, manifest)
    assert any(row["mapping_status"] == "AMBIGUOUS_MARKET" for row in result["mapping_status"])
    assert con.execute(
        "SELECT mapping_status FROM fullscope_graph WHERE date=DATE '2026-07-10'"
    ).fetchone()[0] == "AMBIGUOUS_MARKET"
