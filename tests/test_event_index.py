"""W-E1: pure tests for tools/event_index.py (PLAN_EVENT_PACKAGING §3.2/§3.4).

Deterministic fixture catalog — no warehouse, no dim/latest. Exercises window
inference A–G, Q7 exclusions, catalog_incomplete, window_divergence, and the
padding property on observed units.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import event_index as ei  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures", "event_index_catalog")
POLICY = os.path.join(os.path.dirname(__file__), "..", "config", "event_packaging.yaml")
NOW = ei.parse_dt_us("2026-07-07 01:00:00")  # inside KX-SPORT-EV1 padded window


def _rows():
    return {r["unit_key"]: r for r in ei.build_index(FX, ei.load_policy(POLICY), NOW)}


def test_sports_event_cross_midnight_and_window():
    r = _rows()["KX-SPORT-EV1"]
    assert r["unit"] == "event"
    assert r["status"] == "active"
    assert r["crossed_day_boundary"] is True
    assert r["window_source"] == "scheduled_close"
    assert r["catalog_incomplete"] is False
    assert r["window_divergence"] is False
    assert set(r["markets"]) == {"KX-SPORT-A", "KX-SPORT-B"}
    # Sports pre_pad 2h, post_pad 1h
    assert r["win_start_us"] == ei.parse_dt_us("2026-07-06 16:00:00")
    assert r["win_end_us"] == ei.parse_dt_us("2026-07-07 03:00:00")


def test_crypto_market_unit():
    r = _rows()["KX-CRYPTO-1"]
    assert r["unit"] == "market"
    assert r["status"] == "partial"  # future sched/ticks vs now on 07-07
    assert r["window_source"] == "scheduled_close"  # sched_close > t_last on fixture
    assert r["markets"] == ["KX-CRYPTO-1"]


def test_q7_excluded_exotics_and_mve():
    rows = _rows()
    assert rows["KX-EXOTICS"]["status"] == "excluded"
    assert rows["KXMVECOMBO"]["status"] == "excluded"


def test_catalog_incomplete_missing_close():
    r = _rows()["KX-INCOMPLETE"]
    assert r["catalog_incomplete"] is True
    assert r["sched_close_us"] is None


def test_window_divergence_ot_delay():
    r = _rows()["KX-DIVERGE"]
    assert r["window_divergence"] is True
    assert r["window_source"] == "last_seen"
    assert r["t_last_seen_us"] == ei.parse_dt_us("2026-07-07 01:00:00")
    assert r["sched_close_us"] == ei.parse_dt_us("2026-07-06 22:00:00")


def test_lifecycle_settled_wins_window_end():
    r = _rows()["KX-LC"]
    assert r["window_source"] == "settled"
    assert r["t_settled_us"] == ei.parse_dt_us("2026-07-07 01:00:00")
    assert r["status"] == "sealed"


def test_scheduled_future_no_ticks():
    r = _rows()["KX-SCHEDULED"]
    assert r["status"] == "scheduled"
    assert r["t_first_seen_us"] is None
    assert r["win_start_us"] == ei.parse_dt_us("2026-07-10 08:00:00")  # Economics pre_pad 4h


def test_observed_padding_property():
    """win_start <= t_first and win_end >= t_last whenever observed non-empty."""
    for r in _rows().values():
        if r["status"] == "excluded":
            continue
        t0, t1 = r["t_first_seen_us"], r["t_last_seen_us"]
        if t0 is None:
            continue
        assert r["win_start_us"] <= t0
        assert r["win_end_us"] >= t1


def test_write_parquet_roundtrip():
    rows = ei.build_index(FX, ei.load_policy(POLICY), NOW)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "index.parquet")
        ei.write_parquet(rows, out)
        import duckdb
        con = duckdb.connect()
        n = con.execute("SELECT count(*) FROM '%s'" % out.replace("'", "''")).fetchone()[0]
        cols = [c[0] for c in con.execute("DESCRIBE SELECT * FROM '%s'" % out.replace("'", "''")).fetchall()]
        con.close()
    assert n == len(rows)
    assert cols == ei.COLS


def test_build_from_warehouse_real_dim(tmp_path):
    # W-E1 real-dim wiring: build the index from a warehouse + dim/latest, not a
    # synthetic catalog dir. Reuses the tested infer_index_row core.
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(__file__))
    from test_event_pack import _make_warehouse  # trades+l1: KX-SPORT-A/B + KX-OTHER-1
    root = str(tmp_path / "wh")
    _make_warehouse(root)
    dim = str(tmp_path / "markets.csv")
    with open(dim, "w") as f:
        f.write("ticker,event_ticker,open_time,close_time,status,mve_collection_ticker\n")
        f.write("KX-SPORT-A,KX-SPORT-EV1,2026-07-06 20:00:00,2026-07-07 03:00:00,settled,\n")
        f.write("KX-SPORT-B,KX-SPORT-EV1,2026-07-06 20:00:00,2026-07-07 03:00:00,settled,\n")
        f.write("KX-OTHER-1,KX-OTHER,2026-07-07 00:00:00,2026-07-07 02:00:00,settled,\n")
    policy_for = ei.load_policy(POLICY)
    rows = {r["unit_key"]: r for r in ei.build_index_from_warehouse(
        root, "2026-07-06", "2026-07-07", policy_for,
        ei.parse_dt_us("2026-07-09 00:00:00"), dim, archive_only=False)}
    ev = rows["KX-SPORT-EV1"]
    assert ev["unit"] == "event"
    assert ev["category"] == "Sports"                      # from observed rows
    assert ev["markets"] == ["KX-SPORT-A", "KX-SPORT-B"]    # observed markets
    assert ev["crossed_day_boundary"] is True              # trades span 07-06/07-07
    assert ev["sched_close_us"] == ei.parse_dt_us("2026-07-07 03:00:00")  # from dim
    assert ev["catalog_incomplete"] is False               # dim had open+close
    assert ev["win_start_us"] <= ev["t_first_seen_us"]     # padding property
    assert "KX-OTHER" in rows                               # separate event unit


def test_null_identity_rows_excluded(tmp_path):
    # audit Defect-1: a NULL event/market row must be dropped (not form a bogus
    # unit that crashes the None-vs-str sort).
    import duckdb
    import warehouse as wh
    root = str(tmp_path / "wh")
    os.makedirs(root, exist_ok=True)
    tddl = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
            'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
            '"group" VARCHAR, trade_id VARCHAR, yes_price_e4 INTEGER, '
            'no_price_e4 INTEGER, count_e4 BIGINT, taker_side VARCHAR')
    lddl = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
            'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
            '"group" VARCHAR, record_class VARCHAR, yes_bid_e4 INTEGER, '
            'yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT, '
            'price_e4 INTEGER, volume_e4 BIGINT, open_interest_e4 BIGINT, is_snapshot BOOLEAN')
    scon = duckdb.connect(os.path.join(root, "staging.duckdb"))
    scon.execute("CREATE TABLE trades (%s)" % tddl)
    scon.execute("CREATE TABLE orderbooks_l1 (%s)" % lddl)
    ts = ei.parse_dt_us("2026-07-06 12:00:00")
    scon.executemany("INSERT INTO trades VALUES (%s)" % ",".join("?" * 12), [
        (ts, "M1", "S", "EV-OK", "Sports", "_none", "g", "t1", 5000, 5000, 10000, "yes"),
        (ts, None, "S", None, "Sports", "_none", "g", "t2", 5000, 5000, 10000, "yes"),  # NULL id
    ])
    scon.close()
    wh._CON = None
    wh._ATTACHED.clear()
    rows = {r["unit_key"]: r for r in ei.build_index_from_warehouse(
        root, "2026-07-06", "2026-07-07", ei.load_policy(POLICY), NOW,
        str(tmp_path / "no_dim.csv"), archive_only=False)}
    assert "EV-OK" in rows      # valid unit built, no crash
    assert None not in rows     # NULL-identity row dropped


def test_empty_build_writes_empty_parquet(tmp_path):
    # audit Defect-2: an empty result still emits a readable empty-schema parquet.
    import duckdb
    out = str(tmp_path / "index.parquet")
    ei.write_parquet([], out)
    got = duckdb.sql("SELECT * FROM read_parquet('%s')" % out)
    assert got.fetchall() == [] and [c for c in got.columns] == ei.COLS
