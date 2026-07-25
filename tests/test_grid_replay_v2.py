# -*- coding: utf-8 -*-
"""RED-FIRST tests for grid_replay_v2 — inventory grid replay, KXBTC15M.

Written BEFORE the implementation (iron rule 2026-07-25). They pin the
contract that killed grid_replay v1 when it was silently dropped:

  * NEUTRAL arm == e4_front semantics EXACTLY: one-fill-per-side latch,
    join/front queue accounting, strict trade-through, cancel latency.
  * The disease detector: a refill arm WITHOUT a cap accumulates on
    repeated sweeps; the neutral arm cannot fill the same side twice.
  * Hard cap bounds per-market inventory no matter how many sweeps.
  * Skew only RETREATS the accumulating side (never improves a price —
    optimistic-direction shifts are forbidden by the fill model).
  * Min-requote threshold suppresses sub-threshold repricing intents.
  * The runner gate raises SystemExit on ANY field mismatch vs
    BASELINE_EXPECT (190 fills / 95 markets / +0.321c live inside it).
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.crypto_mm.grid_replay_v2 import (  # noqa: E402
    Arm,
    CLIP,
    MarketSim,
    entry_stats_block,
    gate_or_die,
    NEUTRAL_ARMS,
)
from tools.research.crypto_mm.e4_twoleg_closeout import (  # noqa: E402
    BASELINE_EXPECT,
)

US = 1_000_000
T0 = 1_000_000_000_000
CLOSE = T0 + 1_200 * US * 1_000  # close 20 minutes after T0 (inside 10-30m join)
LAT = 60_000

JOIN_LO = 600 * US * 1_000       # 10m
JOIN_HI = 1_800 * US * 1_000     # 30m
WITHDRAW = 300 * US * 1_000      # 5m


def neutral(mode="tail"):
    return Arm(name=f"neutral_{mode}", mode=mode)


def sim(arms, trades=(), close_us=CLOSE, res="yes"):
    return MarketSim("MKT", close_us, res, list(arms), LAT, list(trades),
                     JOIN_LO, JOIN_HI, WITHDRAW)


def snapshot(s, ts, y_levels, n_levels):
    s.on_book_event(ts, "snapshot", None, None, None,
                    list(y_levels), list(n_levels))


def delta(s, ts, side, px, dq):
    s.on_book_event(ts, "delta", side, px, dq, None, None)


def sweep(mt_trades, ts, px, qty, taker):
    mt_trades.append((ts, px, qty, taker))


# --------------------------------------------------- neutral == e4 semantics
def test_neutral_tail_joins_best_with_displayed_queue_ahead():
    s = sim([neutral("tail")])
    snapshot(s, T0, [(4000, 30_000)], [(5500, 10_000)])
    a = s.arm_state("neutral_tail")
    assert a.quotes["y"]["lvl"] == 4000
    assert a.quotes["y"]["ahead"] == 30_000
    assert a.quotes["n"]["lvl"] == 5500
    assert a.quotes["n"]["ahead"] == 10_000
    assert a.placed == 2


def test_neutral_front_improves_tenth_cent_ahead_zero():
    s = sim([neutral("front")])
    snapshot(s, T0, [(4000, 30_000)], [(5500, 10_000)])
    a = s.arm_state("neutral_front")
    assert a.quotes["y"]["lvl"] == 4010
    assert a.quotes["y"]["ahead"] == 0


def test_fill_requires_strict_trade_through_queue_plus_clip():
    tr = []
    sweep(tr, T0 + 1000, 4000, 30_000 + CLIP, "no")      # exactly queue+clip
    sweep(tr, T0 + 2000, 4000, 1, "no")                  # +1 => through
    s = sim([neutral("tail")], trades=tr)
    snapshot(s, T0, [(4000, 30_000)], [(9000, 1)])
    s.on_book_event(T0 + 3000, "delta", "yes", 4000, 0, None, None)
    a = s.arm_state("neutral_tail")
    assert a.fills == 1
    assert a.q == 5                                       # +5 contracts long


def test_neutral_latch_one_fill_per_side_survives_second_sweep():
    """THE regression that voided grid v1: base arm refilled forever."""
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 4000, big, "no")     # sweep 1: fill y
    sweep(tr, T0 + 5000, 4000, big, "no")     # sweep 2: must NOT fill again
    s = sim([neutral("tail")], trades=tr)
    snapshot(s, T0, [(4000, 1)], [(9000, 1)])
    delta(s, T0 + 2000, "yes", 4000, 0)       # book event between sweeps
    delta(s, T0 + 6000, "yes", 4000, 0)
    a = s.arm_state("neutral_tail")
    assert a.fills == 1
    assert a.quotes["y"] is None              # never re-quoted


def test_cancel_latency_quote_dies_only_after_cx():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3900, big, "no")     # best moves triggers reprice
    s = sim([neutral("tail")], trades=tr)
    snapshot(s, T0, [(4000, 1)], [(9000, 1)])
    # best y drops to 3900 -> reprice intent, cx = ts + LAT; old quote at
    # 4000 still fillable until cx
    delta(s, T0 + 500, "yes", 4000, -1)       # removes 4000 level entirely?
    a = s.arm_state("neutral_tail")
    # quote should be canceling, not gone
    assert a.quotes["y"] is not None and a.quotes["y"]["cx"] is not None


# ------------------------------------------------------------- refill + cap
def test_refill_arm_accumulates_without_cap_disease_detector():
    tr = []
    big = 10 * CLIP
    for k in range(4):
        sweep(tr, T0 + (k + 1) * 10_000 * US, 4000, big, "no")
    s = sim([Arm(name="refill", mode="tail", refill=True, cap=999)],
            trades=tr)
    snapshot(s, T0, [(4000, 1)], [(9000, 1)])
    for k in range(4):
        delta(s, T0 + (k + 1) * 10_000 * US + 5000, "yes", 4000, 0)
    a = s.arm_state("refill")
    assert a.fills == 4
    assert a.q == 20
    assert a.q_peak == 20


def test_hard_cap_blocks_accumulating_side():
    tr = []
    big = 10 * CLIP
    for k in range(4):
        sweep(tr, T0 + (k + 1) * 10_000 * US, 4000, big, "no")
    s = sim([Arm(name="cap10", mode="tail", refill=True, cap=10)],
            trades=tr)
    snapshot(s, T0, [(4000, 1)], [(9000, 1)])
    for k in range(4):
        delta(s, T0 + (k + 1) * 10_000 * US + 5000, "yes", 4000, 0)
    a = s.arm_state("cap10")
    assert a.fills == 2                        # 2 fills = 10 contracts = cap
    assert a.q == 10
    assert a.q_peak == 10


def test_cap_still_quotes_offsetting_side():
    tr = [(T0 + 10_000 * US, 4000, 10 * CLIP, "no"),
          (T0 + 20_000 * US, 4000, 10 * CLIP, "no")]
    s = sim([Arm(name="cap10", mode="tail", refill=True, cap=10)], trades=tr)
    snapshot(s, T0, [(4000, 1)], [(5500, 6_000)])
    delta(s, T0 + 15_000 * US, "yes", 4000, 0)   # re-arm between sweeps
    delta(s, T0 + 20_000 * US + 5000, "yes", 4000, 0)
    a = s.arm_state("cap10")
    assert a.q == 10
    assert a.quotes["y"] is None or a.quotes["y"].get("cx") is not None
    assert a.quotes["n"] is not None           # offsetting side stays


# ------------------------------------------------------------------- skew
def test_skew_retreats_accumulating_side_only_never_improves():
    tr = [(T0 + 10_000 * US, 4000, 10 * CLIP, "no")]
    s = sim([Arm(name="skew", mode="tail", refill=True, cap=999,
                 gamma_c=0.2)], trades=tr)
    snapshot(s, T0, [(4000, 8_000)], [(5500, 6_000)])
    delta(s, T0 + 10_000 * US + 5000, "yes", 4000, 8_000)
    a = s.arm_state("skew")
    # q=+5, gamma 0.2c/ct -> retreat 1c = 100 e4 on the y side
    assert a.quotes["y"]["lvl"] == 3900
    # retreated quote joins the tail of displayed size at that level
    assert a.quotes["y"]["ahead"] == s.books["y"].get(3900, 0)
    # offsetting side never improves beyond baseline join
    assert a.quotes["n"]["lvl"] == 5500


def test_skew_zero_inventory_is_baseline():
    s = sim([Arm(name="skew", mode="tail", refill=True, cap=999,
                 gamma_c=0.5)])
    snapshot(s, T0, [(4000, 8_000)], [(5500, 6_000)])
    a = s.arm_state("skew")
    assert a.quotes["y"]["lvl"] == 4000
    assert a.quotes["n"]["lvl"] == 5500


# --------------------------------------------------------- min-requote (F5)
def test_requote_threshold_suppresses_small_moves():
    s = sim([Arm(name="thr", mode="tail", requote_min_c=1.0),
             neutral("tail")])
    snapshot(s, T0, [(4000, 8_000)], [(9000, 1)])
    # best moves by 0.5c: below threshold -> thr arm holds, neutral cancels
    delta(s, T0 + 1000, "yes", 4050, 8_000)
    thr = s.arm_state("thr")
    ntr = s.arm_state("neutral_tail")
    assert thr.quotes["y"]["cx"] is None
    assert thr.requote_suppressed >= 1
    assert ntr.quotes["y"]["cx"] is not None
    # best moves 1c total from our level -> threshold met -> reprice
    delta(s, T0 + 2000, "yes", 4100, 8_000)
    assert s.arm_state("thr").quotes["y"]["cx"] is not None


# ---------------------------------------------------------- settle + stats
def test_settlement_pnl_and_entry_block_fields():
    tr = [(T0 + 10_000 * US, 4000, 10 * CLIP, "no")]
    s = sim([neutral("tail")], trades=tr, res="yes")
    snapshot(s, T0, [(4000, 1)], [(9000, 1)])
    delta(s, T0 + 10_000 * US + 5000, "yes", 4000, 0)
    s.finalize()
    a = s.arm_state("neutral_tail")
    assert a.pnl == [pytest.approx(60.0)]      # settle 100 - entry 40
    blk = entry_stats_block({"neutral_tail": a}, LAT)
    b = blk["neutral_tail"]
    assert b["quotes_placed"] == a.placed
    assert b["fills"] == 1
    assert b["markets_quoted"] == 1
    assert b["clusters_filled"] == 1
    assert b["mean_settle_pnl_c"] == pytest.approx(60.0)


def test_withdraw_window_cancels_and_blocks_entries():
    s = sim([neutral("tail")], close_us=T0 + WITHDRAW + 10_000 * US)
    # tte inside join window? tte = WITHDRAW + 10s < JOIN_LO -> no entry
    snapshot(s, T0, [(4000, 8_000)], [(5500, 6_000)])
    a = s.arm_state("neutral_tail")
    assert a.placed == 0


# ------------------------------------------------------------------ gate
def test_gate_or_die_passes_on_exact_match():
    fake = {k: dict(v) for k, v in
            BASELINE_EXPECT["main_VALIDATE"].items()}
    gate_or_die(fake, BASELINE_EXPECT["main_VALIDATE"], "main_VALIDATE")


def test_gate_or_die_exits_nonzero_on_any_field_drift():
    fake = {k: dict(v) for k, v in
            BASELINE_EXPECT["main_VALIDATE"].items()}
    fake["front_cancel_60ms"]["fills"] = 189          # off by one
    with pytest.raises(SystemExit) as e:
        gate_or_die(fake, BASELINE_EXPECT["main_VALIDATE"], "main_VALIDATE")
    assert e.value.code == 1


def test_neutral_arms_constant_matches_e4_modes():
    names = {(a.mode, a.refill, a.gamma_c, a.requote_min_c)
             for a in NEUTRAL_ARMS}
    assert names == {("tail", False, 0.0, 0.0), ("front", False, 0.0, 0.0)}
