#!/usr/bin/env python3
"""WP-09 Gate Calculator — contract tests for tools/gate_calc.py.

The three pre-committed gate numbers (signal count / day, lifespan p50,
theoretical net profit) must come from TESTED code, not gate-day scripts.
These fixtures ARE the contract (EXECUTION_PLAN WP-09):

  Signal (buy-all-legs bracket arb): a bracket = mutually-exclusive
  same-event markets. Buying 1 contract of EVERY leg at ask costs
  sum(asks) cents and pays exactly 100c at settlement (at most one leg
  settles YES; exhaustiveness caveat documented in gate_calc.py). Gross
  edge = 100 - sum(asks). A signal fires when gross edge >= min_edge_c
  (DEFAULT 2.0c -> sums <= 98c fire; sums in the 99-101c no-arb band are
  silent). One signal = one contiguous EPISODE (appears -> persists ->
  disappears), never one per tick.

  Full-quote guard: no evaluation until EVERY leg observed in the data
  has a valid ask (0 < ask_e4 < 10000) no staler than max_age_s
  (default 3600s = the heartbeat cadence, the warehouse's documented
  change-only LOCF reconstruction bound). Partial sums must NEVER fire.

  Lifespan: t_close - t_open seconds per episode; nearest-rank
  percentiles (gold-suite convention, via mm_research.pctl_nearest_rank).

  Net profit: gross - sum of per-leg taker fees from
  mm_research.trade_fee (READS config/kalshi_facts.yaml at call time; a
  second fee implementation is forbidden). gate_mode=True REFUSES
  (FeeNotVerifiedError) while fees.verified != true — mechanical,
  proven in BOTH directions below.

pytest-native; stdlib + pandas (research-side dependency, as WP-06).
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import gate_calc as gc  # noqa: E402
import mm_research as mr  # noqa: E402

US = 1_000_000
DAY0_US = 1_783_641_600_000_000  # 2026-07-10T00:00:00Z (same anchor as WP-06 suite)


def leg(ts_s, mt, ask_c, ev="EV-BRK"):
    """One L1 row shaped like the warehouse fact slice gate_calc consumes."""
    return {
        "ts_utc": DAY0_US + int(ts_s * US),
        "market_ticker": mt,
        "event_ticker": ev,
        "yes_ask_e4": None if ask_c is None else int(round(ask_c * 100)),
    }


def episodes(rows, **kw):
    return gc.bracket_signals(pd.DataFrame(rows), **kw)


# ------------------------------------------------------- signal detection

def test_signal_detection():
    """Plan fixture: 3-market bracket, asks sum 96c -> ONE BUY signal,
    gross edge 4c; asks summing inside the no-arb band -> NO signal."""
    # 30 + 33 + 33 = 96c -> edge 4c, one episode (opens once all legs quote)
    eps = episodes([leg(0, "A", 30), leg(1, "B", 33), leg(2, "C", 33)])
    assert len(eps) == 1
    e = eps[0]
    assert e["edge_open_c"] == pytest.approx(4.0)
    assert e["n_legs"] == 3
    assert e["censored"] is True  # still active at end of data

    # 33 + 33 + 33 = 99c -> edge 1c < default 2c threshold -> silence
    assert episodes([leg(0, "A", 33), leg(1, "B", 33), leg(2, "C", 33)]) == []
    # 34 + 34 + 33 = 101c -> negative edge -> silence
    assert episodes([leg(0, "A", 34), leg(1, "B", 34), leg(2, "C", 33)]) == []

    # threshold is parameterized, never hardcoded: min_edge_c=1.0 makes 99c fire
    eps = episodes([leg(0, "A", 33), leg(1, "B", 33), leg(2, "C", 33)],
                   min_edge_c=1.0)
    assert len(eps) == 1 and eps[0]["edge_open_c"] == pytest.approx(1.0)


def test_no_arb_fixture_yields_zero_signals():
    """Anti-fake-green: sums ~= 100c must yield ZERO signals."""
    assert episodes([leg(0, "A", 33), leg(1, "B", 33), leg(2, "C", 34)]) == []
    assert episodes([leg(0, "A", 33.5), leg(1, "B", 33.5), leg(2, "C", 33)]) == []
    # and a flat 100c-repeating timeline stays silent forever
    rows = [leg(0, "A", 50), leg(1, "B", 50)]
    rows += [leg(10 + i, "A", 50) for i in range(5)]
    assert episodes(rows) == []


def test_partial_quote_guard():
    """A leg with no ask yet BLOCKS evaluation: A+B=63c is NOT a 37c edge
    while leg C (present in the data later) has not quoted."""
    rows = [leg(0, "A", 30), leg(1, "B", 33), leg(50, "C", 40)]  # sum 103
    assert episodes(rows) == []
    # invalid asks (one-sided book, ask_e4=10000 or NULL) un-quote the leg
    rows = [leg(0, "A", 30), leg(1, "B", 33), leg(2, "C", 100)]
    assert episodes(rows) == []
    rows = [leg(0, "A", 30), leg(1, "B", 33), leg(2, "C", None)]
    assert episodes(rows) == []


# ---------------------------------------------------------------- lifespan

def test_lifespan():
    """Plan fixture timeline: signal appears, persists across book updates,
    disappears -> hand-computed persistence seconds.

    t=0..2   legs quote 34/33/33 (sum 100)          -> inactive
    t=10     A -> 30 (sum 96, edge 4)               -> episode 1 OPENS
    t=12,15  B, C re-quote unchanged (persists)     -> still ONE episode
    t=25     A -> 34 (sum 100)                      -> episode 1 CLOSES: 15.0s
    t=30     A -> 30 (sum 96)                       -> episode 2 OPENS
    t=40     A -> 35 (sum 101)                      -> episode 2 CLOSES: 10.0s
    """
    rows = [leg(0, "A", 34), leg(1, "B", 33), leg(2, "C", 33),
            leg(10, "A", 30), leg(12, "B", 33), leg(15, "C", 33),
            leg(25, "A", 34),
            leg(30, "A", 30), leg(40, "A", 35)]
    eps = episodes(rows)
    assert len(eps) == 2                    # persistence != re-signaling
    assert eps[0]["lifespan_s"] == pytest.approx(15.0)
    assert eps[0]["censored"] is False
    assert eps[0]["edge_open_c"] == pytest.approx(4.0)
    assert eps[1]["lifespan_s"] == pytest.approx(10.0)
    assert eps[1]["censored"] is False

    st = gc.lifespan_stats(eps)
    # nearest-rank over sorted [10, 15]: p50 = 1st = 10.0, p90 = 2nd = 15.0
    assert st["n"] == 2
    assert st["p50_s"] == pytest.approx(10.0)
    assert st["p90_s"] == pytest.approx(15.0)
    assert st["max_s"] == pytest.approx(15.0)


# -------------------------------------------------------------- net profit

def _episode_96c():
    """One signal episode: legs at 30/33/33c, gross edge 4c."""
    eps = episodes([leg(0, "A", 30), leg(1, "B", 33), leg(2, "C", 33)])
    assert len(eps) == 1
    return eps[0]


def test_net_profit_arithmetic_with_verified_fees(tmp_path):
    """gross - verified fee fn = expected net, hand-computed:
    taker 0.07: fee(0.30) = ceil_cc(0.07*0.30*0.70) = $0.0147
                fee(0.33) = ceil_cc(0.07*0.33*0.67 = 0.0154770) = $0.0155
    fees = 0.0147 + 0.0155 + 0.0155 = $0.0457 = 4.57c
    net  = 4.00c - 4.57c = -0.57c   (a 4c gross edge LOSES money at taker 7%)
    """
    facts = tmp_path / "facts_verified.yaml"
    facts.write_text(
        "fees:\n  verified: true\n  params:\n    taker_rate: 0.07\n")
    r = gc.signal_net_profit(_episode_96c(), gate_mode=True,
                             facts_path=str(facts))
    assert r["gross_c"] == pytest.approx(4.0)
    assert r["fees_c"] == pytest.approx(4.57)
    assert r["net_c"] == pytest.approx(-0.57)


def test_net_profit_gate_mode_refuses_unverified_fees(tmp_path):
    """Mechanical enforcement, BOTH directions (reusing mm_research's guard
    — no second fee implementation exists to drift):

    1. the REAL repo yaml is verified=false today -> gate mode MUST raise.
    2. an explicit verified=false yaml -> raises (flag read, not cached).
    3. verified=true yaml -> does NOT raise (the guard is the flag, nothing
       else) — refusal must not be over-eager or the gate can never run.
    """
    real = mr.load_fee_facts()
    assert real["verified"] is False, \
        "repo kalshi_facts.yaml flipped fees.verified — WP-07/WP-09 rerun due"
    with pytest.raises(mr.FeeNotVerifiedError):
        gc.signal_net_profit(_episode_96c(), gate_mode=True)

    off = tmp_path / "facts_false.yaml"
    off.write_text("fees:\n  verified: false\n  params:\n    taker_rate: 0.07\n")
    with pytest.raises(mr.FeeNotVerifiedError):
        gc.signal_net_profit(_episode_96c(), gate_mode=True,
                             facts_path=str(off))

    on = tmp_path / "facts_true.yaml"
    on.write_text("fees:\n  verified: true\n  params:\n    taker_rate: 0.07\n")
    r = gc.signal_net_profit(_episode_96c(), gate_mode=True,
                             facts_path=str(on))
    assert r["net_c"] == pytest.approx(-0.57)


def test_net_profit_preview_mode_computes_without_gate(tmp_path):
    """gate_mode=False is the NON-GATE PREVIEW path: computes with the
    recorded (unratified) rates so R can see the shape. Repo rate is 0.07
    today, so the numbers match the verified-yaml arithmetic exactly."""
    r = gc.signal_net_profit(_episode_96c(), gate_mode=False)
    assert r["gross_c"] == pytest.approx(4.0)
    assert r["net_c"] == pytest.approx(-0.57)
