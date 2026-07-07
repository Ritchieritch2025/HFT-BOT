#!/usr/bin/env python3
"""WP-06 Research Notebook — contract tests for tools/mm_research.py.

Metric definitions are PINNED by EXECUTION_PLAN WP-06's hand-computed
fixtures. The adopted interpretation (documented in tools/mm_research.py and
queued for R's confirmation in docs/BACKLOG.md):

  moves    = consecutive NONZERO mid changes within a market-day
  K        = sum of squared moves  (realized quadratic variation)
  z        = net displacement, last mid - first mid
  wiggle   = (K - z^2) / 2   (harvestable mean-reversion, unit-maker bound)
  reversal_rate = sign flips between consecutive moves / (n_moves - 1)

Pinned fixture check: mids [10, 12, 10, 12] (cents)
  moves = +2, -2, +2  ->  K = 4+4+4 = 12 ; z = 12-10 = 2
  wiggle = (12 - 4)/2 = 4 ; reversal_rate = 2 flips / 2 pairs = 1.0
which matches the plan's "K=12, z=2, wiggle=(12-4)/2=4" EXACTLY.

A2 (binding): every metric is emitted in BOTH price space (cents) and
log-odds space (logit of mid probability, clipped to [0.01, 0.99] — same
transform as tools/mm_calibrate.py).

Heartbeats: staging L1 heartbeat rows are is_snapshot=true AND price_e4 NULL
(scheduled heartbeats carry book state but NULL price/volume/oi —
docs/warehouse_schema.md). They are excluded from ALL metrics BEFORE
computation. First-observation snapshots (is_snapshot=true, price present)
are real observations and are KEPT — the exclusion is the AND, not
is_snapshot alone.

Intervals: inter-update intervals per market on the heartbeat-excluded rows;
percentiles are nearest-rank (same convention as the gold V5 suite).

Fee guard: the fee fn reads config/kalshi_facts.yaml fees.* at call time —
never hardcoded rates. Any gate_mode=True call while fees.verified is false
MUST raise FeeNotVerifiedError (GUARDRAILS S2 fail-closed; the yaml is
currently verified=false per the WP-05 audit).

pytest-native; stdlib + pandas (already a research-side dependency).
"""
import math
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mm_research as mr  # noqa: E402

# ------------------------------------------------------------------ helpers

US = 1_000_000  # micros per second
DAY0_US = 1_783_641_600_000_000  # 2026-07-10T00:00:00Z (any fixed UTC midnight)


def l1_row(ts_s, bid_c, ask_c, snapshot=False, price_c=50.0, mt="MKT-A"):
    """One staging-shaped L1 row; prices given in cents, stored as E4."""
    return {
        "ts_utc": DAY0_US + int(ts_s * US),
        "market_ticker": mt,
        "category": "Sports",
        "subcategory": "Baseball",
        "group": "MLB",
        "yes_bid_e4": int(bid_c * 100),
        "yes_ask_e4": int(ask_c * 100),
        "price_e4": None if price_c is None else int(price_c * 100),
        "is_snapshot": snapshot,
    }


def mid_rows(mids_c, t0=0.0, dt=1.0, mt="MKT-A"):
    """Rows whose (bid+ask)/2 mid equals each requested cent value."""
    return [l1_row(t0 + i * dt, m - 1, m + 1, snapshot=(i == 0), mt=mt)
            for i, m in enumerate(mids_c)]


# ---------------------------------------------------- pinned plan fixtures

def test_wiggle():
    """EXECUTION_PLAN WP-06 fixture: mids [10,12,10,12] -> K=12, z=2,
    wiggle=(12-4)/2=4."""
    m = mr.wiggle_metrics([10, 12, 10, 12])
    assert m["K"] == 12
    assert m["z"] == 2
    assert m["wiggle"] == 4


def test_reversal_rate():
    """EXECUTION_PLAN WP-06 fixture: same series -> 1.0 (every move flips
    sign)."""
    m = mr.wiggle_metrics([10, 12, 10, 12])
    assert m["reversal_rate"] == 1.0


def test_wiggle_degenerate_series():
    """<2 nonzero moves: metrics defined but never NaN/crash."""
    flat = mr.wiggle_metrics([50, 50, 50])
    assert flat["K"] == 0 and flat["z"] == 0 and flat["wiggle"] == 0
    assert flat["reversal_rate"] is None  # no move pairs to flip
    one = mr.wiggle_metrics([50, 60])
    assert one["K"] == 100 and one["z"] == 10
    assert one["wiggle"] == 0  # (100 - 100)/2: pure drift harvests nothing
    assert one["reversal_rate"] is None


def test_heartbeats_excluded():
    """Heartbeat rows (is_snapshot=true AND price_e4 NULL) are excluded from
    ALL metrics; first-observation snapshots (price present) are kept."""
    rows = mid_rows([10, 12, 10, 12])          # 0s,1s,2s,3s; row 0 = snapshot
    rows.append(l1_row(3600.0, 11, 13, snapshot=True, price_c=None))  # heartbeat
    df = pd.DataFrame(rows)

    out = mr.market_day_metrics(df)
    assert len(out) == 1
    r = out[0]
    # the first-obs snapshot (mid 10) MUST be in: K=12 needs all four mids
    assert r["n_updates"] == 4                  # heartbeat not counted
    assert r["K_px"] == 12 and r["z_px"] == 2 and r["wiggle_px"] == 4
    assert r["reversal_rate"] == 1.0
    # intervals [1,1,1]s — a leaked heartbeat would make p90 = 3597s
    assert r["interval_p50_s"] == 1.0
    assert r["interval_p90_s"] == 1.0


def test_interval_distribution():
    """Fixture timestamps 0,1,2,3,4,10 s -> intervals [1,1,1,1,6];
    nearest-rank p50 = 3rd of 5 sorted = 1.0, p90 = 5th of 5 = 6.0."""
    ts_us = [DAY0_US + s * US for s in (0, 1, 2, 3, 4, 10)]
    st = mr.interval_stats(ts_us)
    assert st["n"] == 5
    assert st["p50_s"] == 1.0
    assert st["p90_s"] == 6.0


def test_fee_placeholder_guard(tmp_path):
    """Gate-mode fee calls MUST raise while fees.verified is false; the flag
    is READ from the yaml, never hardcoded."""
    # 1. reality: the repo yaml is verified=false right now (WP-05 audit,
    #    fail-closed) — gate mode must refuse on the real config.
    facts = mr.load_fee_facts()
    assert facts["verified"] is False, \
        "repo kalshi_facts.yaml flipped fees.verified — revisit WP-07"
    with pytest.raises(mr.FeeNotVerifiedError):
        mr.trade_fee(0.50, 1, gate_mode=True)

    # 2. research mode still computes (flagged, not blocked):
    #    0.07 * 1 * 0.50 * 0.50 = 0.0175 exactly (already centicent-aligned)
    assert mr.trade_fee(0.50, 1) == pytest.approx(0.0175)
    #    ceil-to-centicent: 0.07 * 2 * 0.37 * 0.63 = 0.032634 -> 0.0327
    assert mr.trade_fee(0.37, 2) == pytest.approx(0.0327)

    # 3. behavior FOLLOWS the config (mutate a temp yaml, never hardcoded):
    unverified = tmp_path / "facts_false.yaml"
    unverified.write_text(
        "fees:\n  verified: false\n  params:\n    taker_rate: 0.07\n")
    with pytest.raises(mr.FeeNotVerifiedError):
        mr.trade_fee(0.50, 1, gate_mode=True, facts_path=str(unverified))

    verified = tmp_path / "facts_true.yaml"
    verified.write_text(
        "fees:\n  verified: true\n  params:\n    taker_rate: 0.05\n")
    # verified=true unlocks gate mode AND the rate comes from the file:
    # 0.05 * 1 * 0.5 * 0.5 = 0.0125
    assert mr.trade_fee(0.50, 1, gate_mode=True,
                        facts_path=str(verified)) == pytest.approx(0.0125)


# --------------------------------------------------------- A2: both spaces

def test_logit_transform():
    """logit of mid probability, clipped to [0.01, 0.99] (mm_calibrate's
    transform)."""
    assert mr.to_logit(50.0) == pytest.approx(0.0)
    assert mr.to_logit(10.0) == pytest.approx(math.log(0.1 / 0.9))
    # clip: 0.5c -> p=0.005 -> 0.01 ; 99.9c -> 0.999 -> 0.99
    assert mr.to_logit(0.5) == pytest.approx(math.log(0.01 / 0.99))
    assert mr.to_logit(99.9) == pytest.approx(math.log(0.99 / 0.01))


def test_metrics_emitted_in_both_spaces():
    """A2: same fixture, hand-computed log-odds values alongside price-space.
    p = 0.10 <-> 0.12 ; d = logit(0.12) - logit(0.10) = 0.204794...
    K_lo = 3*d^2 ; z_lo = d ; wiggle_lo = (3d^2 - d^2)/2 = d^2."""
    d = math.log(0.12 / 0.88) - math.log(0.10 / 0.90)  # hand-derived move
    df = pd.DataFrame(mid_rows([10, 12, 10, 12]))
    r = mr.market_day_metrics(df)[0]
    assert r["K_px"] == 12                       # price space (cents^2)
    assert r["K_lo"] == pytest.approx(3 * d * d)  # log-odds space
    assert r["z_lo"] == pytest.approx(d)
    assert r["wiggle_lo"] == pytest.approx(d * d)
    # reversal rate is space-invariant (logit is monotonic)
    assert r["reversal_rate"] == 1.0


def test_market_day_split_and_labels():
    """Metrics are per market-DAY with hierarchy labels carried through."""
    rows = mid_rows([10, 12, 10, 12], mt="MKT-A")
    rows += mid_rows([40, 45], t0=86_400.0, mt="MKT-A")   # next UTC day
    rows += mid_rows([60, 55, 60], mt="MKT-B")
    df = pd.DataFrame(rows)
    out = {(r["market_ticker"], r["day"]): r for r in mr.market_day_metrics(df)}
    assert len(out) == 3
    a0 = out[("MKT-A", "2026-07-10")]
    a1 = out[("MKT-A", "2026-07-11")]
    b0 = out[("MKT-B", "2026-07-10")]
    assert a0["K_px"] == 12 and a1["K_px"] == 25 and b0["K_px"] == 50
    assert a0["category"] == "Sports" and a0["group"] == "MLB"
