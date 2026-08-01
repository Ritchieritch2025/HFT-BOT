"""Unit tests for the exit-leg state machine in e4_twoleg_closeout.

The entry engine is verbatim e4_front.py and is gated at run time by exact
reproduction of BASELINE_EXPECT; these tests cover the NEW layer only:
passive-exit fills, ladder repricing, T-3m taker flatten, carry paths,
fee math, and the contract-conservation invariant.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.crypto_mm.e4_twoleg_closeout import (  # noqa: E402
    CLIP,
    FLATTEN_TTE_US,
    cluster_boot_ci,
    exit_consume_trade,
    exit_on_event,
    finalize_pos,
    flatten_taker,
    pos_new,
    taker_fee_c_per_order,
    target_ask_e4,
    walk_book_sell,
)

LAT = 60_000
T0 = 1_000_000_000_000  # fill timestamp (us)
CLOSE = T0 + 900_000_000  # 15 minutes later


def make_pos(side="y", entry=4000, offset=100, opp_book=None):
    return pos_new(side, "MKT", entry, T0, offset, opp_book or {}, LAT)


# ------------------------------------------------------------- pure math
def test_taker_fee_rounds_order_total_up_to_cent():
    # 5 contracts at 50c: 0.07 * 5 * 0.25 = $0.0875 -> $0.09 -> 9 cents
    assert taker_fee_c_per_order([(5000, 50_000)]) == 9.0


def test_target_ask_ladder_steps_and_clamp():
    assert target_ask_e4(4000, 100, 0) == 4100
    assert target_ask_e4(4000, 100, 29_999_999) == 4100
    assert target_ask_e4(4000, 100, 30_000_000) == 4000
    assert target_ask_e4(4000, 100, 60_000_000) == 3900
    assert target_ask_e4(4000, 100, 120_000_000) == 3800
    assert target_ask_e4(9850, 300, 0) == 9900          # cap 99c
    assert target_ask_e4(150, 100, 120_000_000) == 100  # floor 1c


def test_walk_book_sell_best_first_partial():
    fills, filled = walk_book_sell({4400: 20_000, 4500: 20_000, 4300: 0}, CLIP)
    assert fills == [(4500, 20_000), (4400, 20_000)]
    assert filled == 40_000


# ------------------------------------------------------- passive exit fills
def test_passive_exit_fill_after_queue_consumed():
    pos = make_pos(opp_book={5900: 20_000})  # ask 41c == no-bid 59c
    assert pos["lvl_opp"] == 5900 and pos["ahead"] == 20_000
    t = T0 + LAT  # first active timestamp
    exit_consume_trade(pos, t, 4100, 20_000, "yes", CLOSE)
    assert pos["state"] == "RESTING"          # queue eaten, not yet through
    exit_consume_trade(pos, t + 1, 4100, CLIP + 1, "yes", CLOSE)
    assert pos["state"] == "CLOSED_PASSIVE"
    assert pos["pnl_c"] == pytest.approx(1.0)  # sold 41c vs entry 40c
    assert pos["closed_e4"] == CLIP
    assert pos["exit_ts"] == t + 1


def test_exit_ignores_wrong_taker_pre_activation_and_post_t3m():
    pos = make_pos(opp_book={})
    big = 10 * CLIP
    exit_consume_trade(pos, T0 + 1, 4100, big, "yes", CLOSE)   # before +60ms
    assert pos["state"] == "RESTING" and pos["ahead"] == 0
    exit_consume_trade(pos, T0 + LAT, 4100, big, "no", CLOSE)  # wrong taker
    assert pos["state"] == "RESTING"
    late = CLOSE - FLATTEN_TTE_US
    exit_consume_trade(pos, late, 4100, big, "yes", CLOSE)     # past T-3m
    assert pos["state"] == "RESTING"


def test_deeper_trade_through_fills_but_shallower_does_not():
    pos = make_pos(opp_book={})
    t = T0 + LAT
    exit_consume_trade(pos, t, 4099, 10 * CLIP, "yes", CLOSE)  # below our ask
    assert pos["state"] == "RESTING"
    exit_consume_trade(pos, t, 4200, CLIP + 1, "yes", CLOSE)   # through us
    assert pos["state"] == "CLOSED_PASSIVE"


def test_n_side_symmetry():
    # long NO at 60c, ask 61c -> rests on YES book at 39c, taker 'no' fills
    pos = make_pos(side="n", entry=6000, offset=100, opp_book={3900: 0})
    assert pos["lvl_opp"] == 3900
    t = T0 + LAT
    exit_consume_trade(pos, t, 3900, CLIP + 1, "no", CLOSE)
    assert pos["state"] == "CLOSED_PASSIVE"
    assert pos["pnl_c"] == pytest.approx(1.0)


# ------------------------------------------------------------ ladder logic
def test_ladder_reprice_cancel_then_rejoin_tail():
    pos = make_pos(opp_book={5900: 0})
    ts = T0 + 30_000_000  # 30s age -> target drops 41c -> 40c
    exit_on_event(pos, ts, CLOSE, {6000: 7_000}, {}, LAT, "yes")
    assert pos["state"] == "CANCELING" and pos["cx"] == ts + LAT
    # old quote still live until cx: can still fill at the OLD ask
    exit_consume_trade(pos, ts + 1, 4100, CLIP + 1, "yes", CLOSE)
    assert pos["state"] == "CLOSED_PASSIVE" and pos["pnl_c"] == pytest.approx(1.0)

    pos2 = make_pos(opp_book={5900: 0})
    exit_on_event(pos2, ts, CLOSE, {6000: 7_000}, {}, LAT, "yes")
    # after cx the dead quote cannot fill
    exit_consume_trade(pos2, ts + LAT, 4100, 10 * CLIP, "yes", CLOSE)
    assert pos2["state"] == "CANCELING"
    # next book event re-places at new level, tail of displayed qty
    exit_on_event(pos2, ts + LAT + 1, CLOSE, {6000: 7_000}, {}, LAT, "yes")
    assert pos2["state"] == "RESTING"
    assert pos2["A"] == 4000 and pos2["lvl_opp"] == 6000
    assert pos2["ahead"] == 7_000
    assert pos2["active_from"] == ts + LAT + 1


# ------------------------------------------------------- T-3m force flatten
def test_flatten_walks_book_pays_fee_carries_remainder():
    pos = make_pos()  # entry 40c long YES
    ts = CLOSE - FLATTEN_TTE_US
    exit_on_event(pos, ts, CLOSE, {}, {4500: 20_000, 4400: 20_000}, LAT, "no")
    assert pos["state"] == "FORCED_FLAT"
    assert pos["forced_e4"] == 40_000 and pos["carried_e4"] == 10_000
    # levels: +5c*2ct +4c*2ct = 18c; fee ceil(6.9146c)=7c; carried 1ct loses 40c
    assert pos["pnl_c"] == pytest.approx((18.0 - 7.0 - 40.0) / 5.0)
    assert pos["exit_kind"] == "taker_flatten"


def test_flatten_empty_book_carries_all():
    pos = make_pos()
    ts = CLOSE - FLATTEN_TTE_US
    exit_on_event(pos, ts, CLOSE, {}, {4500: 0}, LAT, "yes")
    assert pos["state"] == "CARRIED" and pos["carried_e4"] == CLIP
    assert pos["pnl_c"] == pytest.approx(60.0)  # settle 100c vs entry 40c
    assert pos["exit_kind"] == "carried_no_bid"


def test_finalize_open_position_carries_at_settlement():
    pos = make_pos()
    finalize_pos(pos, "no")
    assert pos["state"] == "CARRIED" and pos["carried_e4"] == CLIP
    assert pos["pnl_c"] == pytest.approx(-40.0)
    assert pos["exit_kind"] == "carried_data_end"


def test_terminal_states_conserve_contracts():
    for pos in (make_pos(), make_pos(side="n", entry=6000)):
        finalize_pos(pos, "yes")
        assert pos["closed_e4"] + pos["forced_e4"] + pos["carried_e4"] == CLIP
        finalize_pos(pos, "no")  # idempotent on terminal
        assert pos["closed_e4"] + pos["forced_e4"] + pos["carried_e4"] == CLIP


def test_flatten_taker_full_depth_no_carry():
    pos = make_pos()
    flatten_taker(pos, CLOSE - FLATTEN_TTE_US, {4000: CLIP}, "yes")
    assert pos["state"] == "FORCED_FLAT"
    assert pos["forced_e4"] == CLIP and pos["carried_e4"] == 0
    # scratch at 40c: gross 0, fee 0.07*5*0.4*0.6=$0.084 -> 9c order -> 1.8c/ct
    assert pos["pnl_c"] == pytest.approx(-1.8)


# ---------------------------------------------------------------- misc
def test_cluster_boot_ci_degenerate_single_market():
    ci = cluster_boot_ci([("M1", 2.0), ("M1", 2.0)])
    assert ci == [2.0, 2.0]
    assert cluster_boot_ci([]) is None
