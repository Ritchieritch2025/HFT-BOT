# -*- coding: utf-8 -*-
"""RED-FIRST tests for mm_dashboard — read-only terminal panel.

Pure-logic layer only (percentiles, paired rate, realized-today from
settlements, floating marks, color thresholds, staleness rule, NDJSON
incremental tail, event reduction).  Rendering I/O is a thin shell on
top of these and is not unit-tested.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research" / "crypto_mm"))

import mm_dashboard as D  # noqa: E402


# ------------------------------------------------------------- percentiles
def test_pct_nearest_rank():
    xs = list(range(1, 101))          # 1..100
    assert D.pct(xs, 50) == 50
    assert D.pct(xs, 99) == 99
    assert D.pct([7.0], 99) == 7.0
    assert D.pct([], 50) is None


# ---------------------------------------------------- real-schema records
# VERBATIM field shapes captured live 2026-07-26 01:2x-01:30Z (incident
# #3 postmortem).  The dashboard must read THESE, not the field names we
# guessed: *_fp and *_dollars, values as STRINGS.
REAL_FILL = {"action": "sell", "book_side": "ask", "count_fp": "2.00",
             "created_time": "2026-07-26T01:21:34.292512Z",
             "fee_cost": "0.000000", "is_taker": False,
             "market_ticker": "KXBTC15M-26JUL252130-30",
             "no_price_dollars": "0.7500", "outcome_side": "no",
             "side": "no", "ticker": "KXBTC15M-26JUL252130-30",
             "trade_id": "t-1", "yes_price_dollars": "0.2500"}
REAL_POSITION = {"ticker": "KXBTC15M-26JUL252130-30",
                 "position_fp": "-20.00",
                 "market_exposure_dollars": "14.680000",
                 "fees_paid_dollars": "0.000000",
                 "realized_pnl_dollars": "0.000000",
                 "total_traded_dollars": "14.680000"}
REAL_SETTLEMENT = {"event_ticker": "KXBTC15M-26JUL252130",
                   "fee_cost": "0.000000", "market_result": "no",
                   "no_count_fp": "20.00",
                   "no_total_cost_dollars": "14.680000",
                   "revenue": 2000,
                   "settled_time": "2026-07-26T01:30:06.038277Z",
                   "ticker": "KXBTC15M-26JUL252130-30", "value": 0,
                   "yes_count_fp": "0.00",
                   "yes_total_cost_dollars": "0.000000"}


def test_paired_rate_reads_real_fill_schema():
    a = dict(REAL_FILL)
    b = dict(REAL_FILL, side="yes", count_fp="2.00", trade_id="t-2")
    # 2 no + 2 yes on the same market -> fully paired
    assert D.paired_rate([a, b]) == pytest.approx(1.0)


def test_realized_today_reads_real_settlement_schema():
    usd, windows, contracts = D.realized_today(
        [REAL_SETTLEMENT], day_utc="2026-07-26")
    assert usd == pytest.approx(20.00 - 14.68)
    assert windows == 1
    assert contracts == 20


def test_floating_pnl_reads_real_position_schema():
    marks = {"KXBTC15M-26JUL252130-30": 25.0}   # yes mid 25c -> NO worth 75c
    # 20 NO valued 20*75=1500c vs exposure 1468c -> +$0.32
    out = D.floating_pnl([REAL_POSITION], marks)
    assert out == pytest.approx(0.32)


# ------------------------------------------------------------- paired rate
def test_paired_rate_two_sided_market():
    fills = [
        {"ticker": "A", "side": "yes", "count": 2},
        {"ticker": "A", "side": "no", "count": 2},
        {"ticker": "B", "side": "yes", "count": 4},
    ]
    # A pairs 2y+2n -> 4 paired; total 8 contracts -> 0.5
    assert D.paired_rate(fills) == pytest.approx(0.5)
    assert D.paired_rate([]) is None


# --------------------------------------------------------- realized today
def test_realized_today_filters_utc_day_and_sums():
    settlements = [
        {"ticker": "A", "revenue": 400, "yes_total_cost": 232,
         "no_total_cost": 0, "yes_count": 4, "no_count": 0,
         "settled_time": "2026-07-25T22:15:00Z"},
        {"ticker": "B", "revenue": 0, "yes_total_cost": 0,
         "no_total_cost": 120, "yes_count": 0, "no_count": 2,
         "settled_time": "2026-07-25T01:00:00Z"},
        {"ticker": "OLD", "revenue": 900, "yes_total_cost": 0,
         "no_total_cost": 100, "yes_count": 1, "no_count": 1,
         "settled_time": "2026-07-24T23:59:00Z"},        # yesterday
    ]
    usd, windows, contracts = D.realized_today(
        settlements, day_utc="2026-07-25")
    assert usd == pytest.approx((400 - 232 - 120) / 100.0)
    assert windows == 2
    assert contracts == 6


# ------------------------------------------------------------ floating pnl
def test_floating_pnl_marks_long_yes_and_long_no():
    positions = [
        {"ticker": "A", "position": 4, "market_exposure": 180},   # cost $1.80
        {"ticker": "B", "position": -2, "market_exposure": 120},  # long 2 NO
    ]
    marks = {"A": 50.0, "B": 40.0}    # yes mid in cents
    # A: value 4*50=200c -> +20c; B: value 2*(100-40)=120c -> 0c
    assert D.floating_pnl(positions, marks) == pytest.approx(0.20)
    # missing mark -> that position contributes None -> overall None
    assert D.floating_pnl(positions, {"A": 50.0}) is None


# ---------------------------------------------------------------- coloring
def test_net_severity_thresholds():
    assert D.net_severity(0, 6) == "ok"
    assert D.net_severity(3, 6) == "warn"      # > half cap
    assert D.net_severity(-4, 6) == "warn"
    assert D.net_severity(7, 6) == "crit"      # > cap
    assert D.net_severity(6, 6) == "warn"      # at cap: warn, not crit


# --------------------------------------------------------------- staleness
def test_source_reports_stale_after_threshold():
    s = D.Source("exch", max_age_s=15.0)
    s.ok(now=100.0)
    assert not s.stale(now=110.0)
    assert s.stale(now=116.0)
    assert s.age(now=116.0) == pytest.approx(16.0)
    # never succeeded -> stale from the start, age unknown
    virgin = D.Source("x", max_age_s=5.0)
    assert virgin.stale(now=0.0)
    assert virgin.age(now=0.0) is None


# ------------------------------------------------------------- ndjson tail
def test_ndjson_tail_reads_incrementally(tmp_path):
    f = tmp_path / "mm_20260726T01.ndjson"
    f.write_text(json.dumps({"ev": "START", "wall_ns": 1}) + "\n")
    t = D.NdjsonTail(str(tmp_path))
    evs = t.poll()
    assert [e["ev"] for e in evs] == ["START"]
    assert t.poll() == []                       # nothing new
    with f.open("a") as h:
        h.write(json.dumps({"ev": "HEALTH", "wall_ns": 2}) + "\n")
        h.write("{broken json\n")
        h.write(json.dumps({"ev": "FILL", "wall_ns": 3}) + "\n")
    evs = t.poll()
    assert [e["ev"] for e in evs] == ["HEALTH", "FILL"]  # bad line skipped


def test_ndjson_tail_picks_up_new_hour_file(tmp_path):
    a = tmp_path / "mm_20260726T01.ndjson"
    a.write_text(json.dumps({"ev": "START", "wall_ns": 1}) + "\n")
    t = D.NdjsonTail(str(tmp_path))
    t.poll()
    b = tmp_path / "mm_20260726T02.ndjson"
    b.write_text(json.dumps({"ev": "HEALTH", "wall_ns": 2}) + "\n")
    assert [e["ev"] for e in t.poll()] == ["HEALTH"]


# ---------------------------------------------------------- event reducer
def _ns(s):
    return int(s * 1e9)


def test_reducer_tracks_health_latency_counters_and_decisions():
    st = D.EngineState()
    st.feed([
        {"ev": "START", "wall_ns": _ns(1000), "mode": "shadow"},
        {"ev": "HEALTH", "wall_ns": _ns(1010), "halted": False,
         "rti_age": {"KXBTC15M": 1.2}, "requote_suppressed": 3},
        {"ev": "ORDER_ACK", "wall_ns": _ns(1011), "mt": "M", "side": "bid",
         "px": 0.45, "ms": 40.0},
        {"ev": "ORDER_ACK", "wall_ns": _ns(1012), "mt": "M", "side": "bid",
         "px": 0.46, "ms": 60.0},
        {"ev": "ORDER_REJ", "wall_ns": _ns(1013), "mt": "M", "side": "bid",
         "px": 0.47, "code": 400},
        {"ev": "CANCEL_ACK", "wall_ns": _ns(1014), "mt": "M", "side": "bid",
         "ms": 30.0, "reason": "reprice"},
        {"ev": "RATE_SKIP", "wall_ns": _ns(1015), "mt": "M", "side": "bid"},
        {"ev": "RECON_OK", "wall_ns": _ns(1016), "markets": 1},
        {"ev": "FILL", "wall_ns": _ns(1017),
         "raw": {"ticker": "M", "side": "yes", "count": 2}},
    ])
    assert st.start_ns == _ns(1000)
    assert st.health["requote_suppressed"] == 3
    assert D.pct(sorted(st.order_ms), 50) == 40.0
    assert st.acks == 2 and st.rejs == 1
    assert st.rate_skips == 1
    assert st.last_recon_ok_ns == _ns(1016)
    assert len(st.decisions) == 5              # ACK,ACK,REJ,CANCEL,FILL
    assert st.decisions[-1][1] == "成交"
    assert st.reject_rate() == pytest.approx(1 / 3)


def test_reducer_captures_halt_reason():
    st = D.EngineState()
    st.feed([{"ev": "ECON_HALT", "wall_ns": _ns(2000),
              "reason": "2 consecutive zero-revenue settlements"}])
    assert st.halt_reason.startswith("ECON")
    st2 = D.EngineState()
    st2.feed([{"ev": "RECON_HALT", "wall_ns": _ns(2000),
               "reason": "divergence", "diffs": [{"mt": "M"}]}])
    assert "divergence" in st2.halt_reason


# ------------------------------------------------------------ stale render
def test_stale_block_replaces_values_with_age_banner():
    s = D.Source("exch", max_age_s=15.0)
    s.ok(now=100.0)
    out = D.stale_or(s, now=200.0, value_lines=["余额 $10"])
    assert out != ["余额 $10"]
    assert any("数据陈旧" in ln and "100" in ln for ln in out)
    fresh = D.stale_or(s, now=105.0, value_lines=["余额 $10"])
    assert fresh == ["余额 $10"]
