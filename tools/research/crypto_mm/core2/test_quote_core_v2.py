"""Golden cases for quote_core_v2 — one per operator spec bullet."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quote_core_v2 import Params, MarketState, compute  # noqa: E402


def st(**kw):
    base = dict(yes_bid_c=48.0, yes_ask_c=52.0, bid_depth=500.0,
                ask_depth=500.0, flow_buy=5.0, flow_sell=5.0,
                fair_c=50.0, sigma_c=0.3, q=0.0, tte_s=600.0,
                unpaired_age_s=0.0)
    base.update(kw)
    return MarketState(**base)


def test_balanced_book_quotes_both_sides_equal():
    q = compute(st())
    assert q.bid_sz > 0 and q.ask_sz > 0
    assert q.bid_sz == q.ask_sz
    assert q.bid_px_c is not None and q.ask_px_c is not None


def test_toxic_sell_flow_shrinks_bid_to_zero():
    calm = compute(st())
    hot = compute(st(flow_sell=400.0))
    assert hot.bid_sz < calm.bid_sz
    assert hot.bid_sz == 0.0, "heavy adverse flow must empty the side"
    assert hot.ask_sz >= calm.ask_sz * 0.9  # other side keeps working


def test_inventory_skew_shrinks_the_growing_side():
    p = Params(q_max=6.0)
    long_q = compute(st(q=5.0), p)
    flat = compute(st(q=0.0), p)
    assert long_q.bid_sz < flat.bid_sz
    assert long_q.ask_sz >= flat.ask_sz  # reducing side never shrunk by inv


def test_size_tapers_to_zero_near_settlement():
    assert compute(st(tte_s=60.0)).bid_sz == 0.0
    assert compute(st(tte_s=60.0)).ask_sz == 0.0


def test_entry_cutoff_blocks_new_legs_but_not_reduction():
    p = Params()
    q0 = compute(st(tte_s=200.0), p)          # flat: both sides are entries
    assert q0.bid_sz == 0.0 and q0.ask_sz == 0.0
    q1 = compute(st(tte_s=200.0, q=3.0, basis_c=48.0), p)  # ask_no reduces
    assert q1.bid_sz == 0.0
    assert q1.ask_sz > 0.0, "reduction stays open inside the cutoff"


def test_ladder_refuses_completion_beyond_max_loss():
    # long YES @ 60, market crashed: NO join costs 55 -> completing pays
    # 115 for the pair.  Even at full age the ladder tolerates only
    # maxloss; the quote must NOT be placed (knives IOC instead).
    q = compute(st(yes_bid_c=44.0, yes_ask_c=45.0, q=3.0, basis_c=60.0,
                   unpaired_age_s=60.0, fair_c=44.5))
    assert q.ask_sz == 0.0, "no completion quote at a catastrophic price"


def test_ladder_accepts_bounded_loss_late():
    # long YES @ 50, NO join costs 51.5 -> lock -1.5c; young age refuses,
    # old age (near bail) accepts within maxloss=2.
    young = compute(st(yes_bid_c=48.0, yes_ask_c=48.5, q=3.0, basis_c=50.0,
                       unpaired_age_s=5.0, fair_c=48.2))
    old = compute(st(yes_bid_c=48.0, yes_ask_c=48.5, q=3.0, basis_c=50.0,
                     unpaired_age_s=110.0, fair_c=48.2))
    assert young.ask_sz == 0.0
    assert old.ask_sz > 0.0


def test_no_net_edge_after_costs_means_no_quote():
    # fair well below the bid: standing at the bid is negative edge
    q = compute(st(fair_c=40.0))
    assert q.bid_sz == 0.0
    # and the NO side of the same picture is rich in edge
    assert q.ask_sz > 0.0


def test_wide_spread_improves_one_tick_when_paid():
    p = Params()
    q = compute(st(yes_bid_c=40.0, yes_ask_c=60.0, fair_c=50.0), p)
    assert q.bid_px_c == 41.0, "improve into a wide spread"
    assert q.ask_px_c == 41.0  # NO side: join/improve symmetric


def test_bail_signal_fires_on_aged_inventory():
    q = compute(st(q=3.0, unpaired_age_s=130.0))
    assert q.take_side == "ask_no"
    assert q.diag["take_reason"] == "bail"


def test_lock_take_signal_between_ages():
    q = compute(st(q=3.0, unpaired_age_s=40.0))
    assert q.diag.get("take_reason") == "lock_take_if_profitable"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
