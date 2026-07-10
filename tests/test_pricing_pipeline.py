"""W-P4 acceptance (PLAN_PRICING_MODEL §3): end-to-end fair→quote golden
scenarios.

Five committed scenario tapes under tests/fixtures/pricing_scenarios/, each a
FULLY hand-computed expected quote sequence — the executable spec the Phase-2
C++ port must reproduce number-for-number. Emitted E4 quotes are asserted
EXACTLY; derived lo intermediates (delta/cap/correction) to a tight tolerance.

This W is a CONSUMER: it drives the FROZEN W-P1/W-P2/W-P3 modules and never
edits tools/pricing/*. The red-proof seeds a TRANSIENT in-test mutation
(price-space micro-price instead of lo-space, via monkeypatch — never a
committed edit) and asserts the suite goes RED, so the golden tests can
genuinely fail (D2 mutation-mindset).
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from pricing import fair, quote as q, lo  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "pricing_scenarios")


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def _run_book_step(s, horizon_s, cap_max):
    """fair→quote for one order-book tape step, using the frozen modules."""
    f = fair.fair_lo(s["bid_e4"], s["ask_e4"], s["bid_qty"], s["ask_qty"],
                     taker_imbalance=s.get("taker_imbalance", 0.0))
    return q.quote(f, s["inventory"], s["t_remaining_s"], horizon_s, cap_max,
                   breaker_tripped=s.get("breaker_tripped", False))


def _assert_quote(got, expect, ctx):
    assert got["quoting"] == expect["quoting"], ctx + " quoting"
    assert got["bid_e4"] == expect["bid_e4"], ctx + " bid_e4"
    assert got["ask_e4"] == expect["ask_e4"], ctx + " ask_e4"
    assert got["reason"] == expect["reason"], ctx + " reason"
    if "delta_lo" in expect:
        assert got["delta_lo"] == pytest.approx(expect["delta_lo"], abs=1e-6), \
            ctx + " delta_lo"
    if "cap" in expect:
        assert got["cap"] == pytest.approx(expect["cap"], abs=1e-6), ctx + " cap"


# ── the five golden scenarios, each asserted EXACTLY ────────────────────

@pytest.mark.parametrize("fixture", [
    "calm_two_sided.json", "buy_pressure_trend.json", "imbalanced_wide_book.json",
    "pre_settlement_winddown.json"])
def test_book_scenarios_match_golden(fixture):
    sc = _load(fixture)
    assert sc["kind"] == "book"
    for i, s in enumerate(sc["steps"]):
        got = _run_book_step(s, sc["horizon_s"], sc["cap_max"])
        _assert_quote(got, s["expect"], "%s step %d" % (sc["name"], i))


def test_buy_pressure_is_monotone_up():
    sc = _load("buy_pressure_trend.json")
    bids = [_run_book_step(s, sc["horizon_s"], sc["cap_max"])["bid_e4"]
            for s in sc["steps"]]
    asks = [_run_book_step(s, sc["horizon_s"], sc["cap_max"])["ask_e4"]
            for s in sc["steps"]]
    assert bids == sorted(bids) and asks == sorted(asks)   # rise with pressure


def test_winddown_delta_widens_cap_shrinks_then_stops():
    sc = _load("pre_settlement_winddown.json")
    outs = [_run_book_step(s, sc["horizon_s"], sc["cap_max"]) for s in sc["steps"]]
    # the quoting steps (exclude the final Q6 hard stop, whose δ is None):
    deltas = [o["delta_lo"] for o in outs if o["delta_lo"] is not None]
    caps = [o["cap"] for o in outs if o["quoting"]]
    assert deltas == sorted(deltas)                        # δ widens toward close
    assert caps == sorted(caps, reverse=True)              # cap shrinks to ~0
    assert outs[-1]["quoting"] is False                    # hard stop inside Q6


def test_bracket_dislocation_matches_golden():
    sc = _load("bracket_dislocation.json")
    assert sc["kind"] == "bracket"
    legs = [(l["prob"], l["depth"]) for l in sc["legs"]]
    corr = fair.bracket_corrections(legs)
    assert sum(corr) == pytest.approx(sum(p for p, _ in legs) - 1.0, abs=1e-12)
    for i, (leg, c) in enumerate(zip(sc["legs"], corr)):
        raw = lo.logit(leg["prob"])
        cf = fair.apply_bracket_to_fair_lo(raw, c)
        got = q.quote(cf, sc["inventory"], sc["t_remaining_s"], sc["horizon_s"],
                      sc["cap_max"])
        ctx = "bracket leg %d" % i
        assert cf == pytest.approx(leg["expect"]["corr_lo"], abs=1e-6), ctx + " corr_lo"
        assert cf < raw, ctx + " corrected DOWN (sum>1)"
        assert got["bid_e4"] == leg["expect"]["bid_e4"], ctx + " bid"
        assert got["ask_e4"] == leg["expect"]["ask_e4"], ctx + " ask"
    # the thin leg (depth 10) moved most
    assert abs(corr[2]) > abs(corr[0])


def test_jump_event_matches_golden():
    sc = _load("jump_event.json")
    assert sc["kind"] == "jump"
    lb = lo.logit(sc["velocity_prob_from"])
    la = lo.logit(sc["velocity_prob_to"])
    assert q.jump_breaker(lb, la, sc["dt_s"]) is sc["expect_velocity_trips"]
    # a huge trade volume with a still book must NOT trip (watches quotes)
    assert q.jump_breaker(lb, lb, sc["dt_s"], trade_volume=sc["trade_only_volume"],
                          window_s=sc["dt_s"]) is sc["expect_trade_only_trips"]
    b = sc["book"]
    f = fair.fair_lo(b["bid_e4"], b["ask_e4"], b["bid_qty"], b["ask_qty"])
    calm = q.quote(f, sc["inventory"], sc["t_remaining_s"], sc["horizon_s"],
                   sc["cap_max"], breaker_tripped=False)
    jump = q.quote(f, sc["inventory"], sc["t_remaining_s"], sc["horizon_s"],
                   sc["cap_max"], breaker_tripped=True)
    _assert_quote(calm, sc["expect_calm_quote"], "jump calm")
    _assert_quote(jump, sc["expect_jump_quote"], "jump tripped")


# ── red-proof: a price-space micro-price makes the golden suite FAIL (D2) ─

def test_redproof_price_space_micro_diverges(monkeypatch):
    """Transient mutation (never committed): replace the lo-space micro-price
    with a PRICE-space one (arithmetic size-weighted price → lo at the end).
    On the wide imbalanced book (Jensen gap) the golden quote must diverge —
    proving the golden test genuinely exercises the lo-space pipeline and can
    fail."""
    def price_space_micro(bid_e4, ask_e4, bid_qty, ask_qty):
        if not fair._valid_book(bid_e4, ask_e4, bid_qty, ask_qty):
            return None
        px_e4 = (bid_e4 * ask_qty + ask_e4 * bid_qty) / (bid_qty + ask_qty)
        return lo.lo_of_e4(int(round(px_e4)))          # the DEFECT
    monkeypatch.setattr(fair, "micro_price_lo", price_space_micro)

    sc = _load("imbalanced_wide_book.json")
    s = sc["steps"][0]
    got = _run_book_step(s, sc["horizon_s"], sc["cap_max"])
    # under the defect the emitted quote is NOT the golden one
    assert (got["bid_e4"], got["ask_e4"]) != \
        (s["expect"]["bid_e4"], s["expect"]["ask_e4"])
    # and the golden assertion would raise
    with pytest.raises(AssertionError):
        _assert_quote(got, s["expect"], "redproof")


def test_pricing_modules_frozen_not_edited_by_this_w():
    """W-P4 is a consumer: it must not have edited the frozen impl modules.
    (A committed edit to tools/pricing/* would be a Forbidden-writes breach;
    this asserts the public callables this W depends on still exist.)"""
    for fn in ("micro_price_lo", "fair_lo", "bracket_corrections",
               "apply_bracket_to_fair_lo"):
        assert callable(getattr(fair, fn))
    for fn in ("quote", "reservation_lo", "half_width_lo", "cap_t",
               "jump_breaker"):
        assert callable(getattr(q, fn))
