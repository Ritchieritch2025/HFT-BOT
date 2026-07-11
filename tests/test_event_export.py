"""W-E7: operator-facing CSV export (§3.6) — three-axis selector, per-event
folders, money-integrity (E4 byte-exact), and completeness manifest.
"""
import csv
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import event_export as ex  # noqa: E402
from test_event_pack import _make_warehouse, _ts, NOW  # noqa: E402


def _index(root):
    idx = os.path.join(root, "index.parquet")
    con = duckdb.connect()
    con.execute("CREATE TABLE idx (unit_key VARCHAR, event_ticker VARCHAR, "
                "series_ticker VARCHAR, markets VARCHAR[], category VARCHAR, "
                "win_start_us BIGINT, win_end_us BIGINT, window_source VARCHAR, "
                "crossed_day_boundary BOOLEAN, status VARCHAR)")
    con.executemany("INSERT INTO idx VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("KX-SPORT-EV1", "KX-SPORT-EV1", "KXNBA", ["KX-SPORT-A", "KX-SPORT-B"],
         "Sports", _ts("2026-07-06 20:00:00"), _ts("2026-07-07 03:00:00"),
         "scheduled_close", True, "sealed")])
    con.execute("COPY idx TO '%s' (FORMAT parquet)" % idx)
    return idx


def _setup(tmp_path):
    root = str(tmp_path / "wh")
    _make_warehouse(root)
    idx = _index(root)
    return root, idx


def _read_dest(dest, name):
    with open(os.path.join(dest, name)) as f:
        return list(csv.DictReader(f))


def test_event_export_money_integrity_and_layout(tmp_path):
    root, idx = _setup(tmp_path)
    rows, mfilter = ex.resolve_targets("event", "KX-SPORT-EV1", idx)
    out = ex.export_one(rows[0], mfilter, root, str(tmp_path / "packs"),
                        str(tmp_path / "exp"), NOW, gaps=[], gaps_available=True,
                        archive_only=False)
    assert out["status"] == "exported" and out["completeness"] == "pass"
    dest = out["dest"]
    # per-event folder layout (§3.6)
    for f in ("trades.csv", "orderbooks_l1.csv", "_event_summary.csv", "_manifest.json"):
        assert os.path.exists(os.path.join(dest, f)), f
    # MONEY-INTEGRITY: sub-penny E4 integer survives byte-exact through export.
    tr = _read_dest(dest, "trades.csv")
    t1 = next(r for r in tr if r["trade_id"] == "t1")
    assert t1["yes_price_e4"] == "90" and t1["count_e4"] == "12345"
    for r in tr:
        assert "." not in r["yes_price_e4"] and "." not in r["count_e4"]
    # no mixing events: only the two event markets, no foreign row
    assert {r["market_ticker"] for r in tr} == {"KX-SPORT-A", "KX-SPORT-B"}


def test_manifest_completeness_reflects_validator(tmp_path):
    root, idx = _setup(tmp_path)
    rows, mf = ex.resolve_targets("event", "KX-SPORT-EV1", idx)
    # clean -> pass
    out = ex.export_one(rows[0], mf, root, str(tmp_path / "p1"), str(tmp_path / "e1"),
                        NOW, gaps=[], gaps_available=True, archive_only=False)
    man = json.load(open(os.path.join(out["dest"], "_manifest.json")))
    assert man["completeness"] == "pass"
    # interior capture gap -> degraded (V-EP15 / AF-1)
    gap = [(_ts("2026-07-07 00:00:00"), _ts("2026-07-07 00:15:00"))]
    out2 = ex.export_one(rows[0], mf, root, str(tmp_path / "p2"), str(tmp_path / "e2"),
                         NOW, gaps=gap, gaps_available=True, archive_only=False)
    man2 = json.load(open(os.path.join(out2["dest"], "_manifest.json")))
    assert man2["completeness"] == "degraded"


def test_market_axis_filters_to_one_market(tmp_path):
    root, idx = _setup(tmp_path)
    rows, mfilter = ex.resolve_targets("market", "KX-SPORT-A", idx)
    assert mfilter == {"KX-SPORT-A"}
    out = ex.export_one(rows[0], mfilter, root, str(tmp_path / "packs"),
                        str(tmp_path / "exp"), NOW, gaps=[], gaps_available=True,
                        archive_only=False)
    tr = _read_dest(out["dest"], "trades.csv")
    assert {r["market_ticker"] for r in tr} == {"KX-SPORT-A"}   # single market only


def test_series_axis_resolves_events(tmp_path):
    root, idx = _setup(tmp_path)
    rows, mfilter = ex.resolve_targets("series", "KXNBA", idx)
    assert mfilter is None and len(rows) == 1 and rows[0][0] == "KX-SPORT-EV1"
