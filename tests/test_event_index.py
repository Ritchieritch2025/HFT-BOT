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