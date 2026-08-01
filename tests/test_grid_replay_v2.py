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


# ------------------------------------------------------ no-lookahead guards
def test_maker_grace_does_not_flatten_before_grace_window():
    arm = Arm(name="grace", mode="tail", refill=True, pair=True,
               maker_grace_s=30.0, flatten_age_s=90.0)
    tr = [(T0 + 1_000, 4000, 10 * CLIP, "no")]
    s = sim([arm], trades=tr)
    snapshot(s, T0, [(4000, 10_000)], [(9000, 10_000)])
    # Fill the YES leg; the opposite leg is intentionally absent.
    delta(s, T0 + 2_000, "yes", 4000, 0)
    a = s.arm_state("grace")
    assert len(a.unpaired["y"]) == 1
    # At age 90s the grace arm must still wait (no future tape is consulted).
    s.on_book_event(T0 + 90_000 * US, "delta", "yes", 4000, 0, None, None)
    assert len(a.unpaired["y"]) == 1


def test_opposite_depth_gate_uses_only_current_book():
    arm = Arm(name="depth", mode="tail", refill=True, pair=True,
              min_opp_depth_ct=5)
    s = sim([arm])
    # YES entry requires visible NO depth.  One contract is insufficient.
    snapshot(s, T0, [(4000, 10_000)], [(9000, 4 * 10_000)])
    a = s.arm_state("depth")
    assert a.quotes["y"] is None
    # Add enough currently visible depth; the next event may admit a quote.
    delta(s, T0 + 1_000, "no", 9000, 2 * 10_000)
    assert a.quotes["y"] is not None


def test_neutral_arms_constant_matches_e4_modes():
    names = {(a.mode, a.refill, a.gamma_c, a.requote_min_c)
             for a in NEUTRAL_ARMS}
    assert names == {("tail", False, 0.0, 0.0), ("front", False, 0.0, 0.0)}


# ------------------------------------------------------------ pairing arm
# Operator doctrine 2026-07-26 ("反复出入场吃差价才是做市"): both sides
# always quoted; after one side fills, PUSH the opposite side so the
# pair costs <= pair_lock_c (Kalshi nets the position -> capital
# released, >=1c locked); unpaired lots die by taker flatten at age
# flatten_age_s or at the T-5m withdraw, never carried into the death
# zone.  This is NOT the killed e4_twoleg family (fixed offset from own
# entry, hold-side exit): the exit here is an ENTRY on the other side
# priced off the pair-cost ceiling.

def pair_arm(**kw):
    d = dict(name="pair", mode="tail", refill=True, pair=True)
    d.update(kw)
    return Arm(**d)


def test_pair_opposite_fills_lock_pnl_and_release_inventory():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")     # sell-taker fills our y@30.00
    sweep(tr, T0 + 2000, 4000, big, "yes")    # buy-taker: n side 6000<=6500
    s = sim([pair_arm()], trades=tr)
    snapshot(s, T0, [(3000, 1)], [(6500, 1)])
    delta(s, T0 + 3000, "yes", 3000, 0)       # consume both trades
    a = s.arm_state("pair")
    assert a.fills == 2
    assert a.paired_ct == 10                  # 2 lots x CLIP_CT netted
    assert a.pnl == [pytest.approx(2.5), pytest.approx(2.5)]  # 100-30-65
    assert not a.unpaired["y"] and not a.unpaired["n"]
    assert a.q == 0
    s.finalize()
    assert a.carried_ct == 0


def test_pair_push_reprices_opposite_to_lock_ceiling():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")     # y lot @3000, unpaired
    s = sim([pair_arm()], trades=tr)
    snapshot(s, T0, [(3000, 1)], [(6500, 1)])
    delta(s, T0 + 2000, "yes", 3000, 0)       # fill applies; reprice intent
    a = s.arm_state("pair")
    assert a.unpaired["y"] == [(3000, T0 + 1000)]
    assert a.quotes["n"] is not None and a.quotes["n"]["cx"] is not None
    delta(s, T0 + 2000 + LAT + 1, "yes", 3000, 0)   # cancel done, re-place
    # ceiling = 9900-3000 = 6900; n ask = 10000-3000-100 = 6900 too
    assert a.quotes["n"]["lvl"] == 6900
    assert a.quotes["n"]["ahead"] == 0        # improving past 6500 best
    # unpaired cap (1 lot): the y side must NOT be re-quoted
    assert a.quotes["y"] is None


def test_pair_push_never_exceeds_ceiling_when_best_is_above_it():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")
    s = sim([pair_arm()], trades=tr)
    snapshot(s, T0, [(3000, 1)], [(9000, 4_000), (6900, 7_000)])
    delta(s, T0 + 2000, "yes", 3000, 0)
    a = s.arm_state("pair")
    delta(s, T0 + 2000 + LAT + 1, "yes", 3000, 0)
    # natural join would be 9000 -> pair cost 120c; ceiling wins
    assert a.quotes["n"]["lvl"] == 6900
    assert a.quotes["n"]["ahead"] == 7_000    # tail of displayed at 6900


def test_pair_fifo_pairs_oldest_lot_first():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")
    sweep(tr, T0 + 3000, 3100, big, "no")
    sweep(tr, T0 + 5000, 4000, big, "yes")    # n fill 6500 pairs vs 3000 lot
    s = sim([pair_arm(unpaired_max_lots=2)], trades=tr)
    snapshot(s, T0, [(3100, 1), (3000, 1)], [(6500, 1)])
    delta(s, T0 + 2000, "yes", 3000, 0)       # fill 1 (y joins best 3100...)
    a = s.arm_state("pair")
    delta(s, T0 + 2000 + LAT + 1, "yes", 3000, 0)
    delta(s, T0 + 6000, "yes", 3000, 0)
    assert a.paired_ct == 10
    assert len(a.unpaired["y"]) == 1          # newest lot remains
    assert not a.unpaired["n"]


def test_pair_age_flatten_pays_taker_fee():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")     # y lot @3000
    s = sim([pair_arm()], trades=tr)
    snapshot(s, T0, [(3000, 1)], [(9000, 1)])
    delta(s, T0 + 2000, "yes", 3000, -1)      # consume fill; empty 3000
    a = s.arm_state("pair")
    assert a.unpaired["y"] == [(3000, T0 + 1000)]
    # Replay timestamps are nanoseconds: 90 seconds = 90*US*1000.
    ts_flat = T0 + 1000 + 90 * US * 1000 + 1
    delta(s, ts_flat, "yes", 2500, 50_000)    # displayed bid appears
    # 5 ct sold @25.00 vs 30.00 entry: -5c/ct - fee(7% quad, ceil to cent
    # order-total 7c => 1.4c/ct) = -6.4c/ct
    assert not a.unpaired["y"]
    assert a.flattened_ct == 5
    assert a.pnl[-1] == pytest.approx(-6.4)
    assert a.q == 0


def test_pair_withdraw_window_flattens_regardless_of_age():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")
    s = sim([pair_arm(flatten_age_s=1e9)], trades=tr)   # age rule off
    snapshot(s, T0, [(3000, 1)], [(9000, 1)])
    delta(s, T0 + 2000, "yes", 3000, -1)
    a = s.arm_state("pair")
    assert a.unpaired["y"]
    ts_wd = CLOSE - WITHDRAW + 1              # inside the T-5m death zone
    delta(s, ts_wd, "yes", 2500, 50_000)
    assert not a.unpaired["y"]
    assert a.flattened_ct == 5


def test_pair_finalize_settles_remaining_unpaired():
    tr = []
    big = 10 * CLIP
    sweep(tr, T0 + 1000, 3000, big, "no")
    s = sim([pair_arm()], trades=tr, res="yes")
    snapshot(s, T0, [(3000, 1)], [(9000, 1)])
    delta(s, T0 + 2000, "yes", 3000, 0)
    s.finalize()
    a = s.arm_state("pair")
    assert a.carried_ct == 5
    assert a.pnl[-1] == pytest.approx(70.0)   # 100 - 30, settled yes
    assert not a.unpaired["y"]


def test_pair_defaults_leave_neutral_arms_untouched():
    for a in NEUTRAL_ARMS:
        assert a.pair is False
