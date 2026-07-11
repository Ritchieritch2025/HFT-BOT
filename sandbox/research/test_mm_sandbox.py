"""Fast executable proofs for the free-fire market-making sandbox."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location("mm_sandbox", HERE / "mm_sandbox.py")
mm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mm
SPEC.loader.exec_module(mm)


def ev(ts, seq, kind, book, px=0, qty=0, side="", tid=""):
    return mm.TapeEvent(ts, seq, kind, book, px, qty, side, tid)


def desired(bid, ask, qty=10_000, reason="test"):
    return mm.DesiredQuote(bid, ask, qty, reason)


def test_quote_cannot_fill_before_effective_time():
    sim = mm.FillSimulator(fill_rule="strict", place_latency_us=100,
                           cancel_latency_us=50)
    book = mm._book(4000, 5000)
    sim.update_book(book)
    sim.set_desired(desired(4000, None), 0)
    early = ev(99, 1, mm.EVENT_TYPE["TRADE"], book, 3900, 10_000, "no", "early")
    sim.advance_before(early.ts_us)
    assert sim.on_trade(early) == []
    sim.advance_through(100)
    late = ev(101, 2, mm.EVENT_TYPE["TRADE"], book, 3900, 10_000, "no", "late")
    assert len(sim.on_trade(late)) == 1


def test_cancel_pending_order_remains_fillable_and_is_labeled_stale():
    sim = mm.FillSimulator(fill_rule="strict", place_latency_us=10,
                           cancel_latency_us=100)
    book = mm._book(4000, 5000)
    sim.update_book(book)
    sim.set_desired(desired(4000, None), 0)
    sim.advance_through(10)
    sim.set_desired(desired(3000, None), 20)  # old 4000 bid cancels at 120
    trade = ev(50, 1, mm.EVENT_TYPE["TRADE"], book, 3900, 10_000, "no", "stale")
    fills = sim.on_trade(trade)
    assert len(fills) == 1 and fills[0].price_e4 == 4000
    assert fills[0].stale is True and sim.stale_fills == 1


def test_queue_ahead_and_public_size_bound_partial_fill():
    sim = mm.FillSimulator(fill_rule="queue", place_latency_us=0,
                           cancel_latency_us=0)
    # Five contracts ahead at our bid; our order is three contracts.
    book = mm._book(4000, 5000, bid_qty=50_000)
    sim.update_book(book)
    sim.set_desired(desired(4000, None, qty=30_000), 0)
    first = ev(1, 1, mm.EVENT_TYPE["TRADE"], book, 4000, 40_000, "no", "q1")
    assert sim.on_trade(first) == []
    second = ev(2, 2, mm.EVENT_TYPE["TRADE"], book, 4000, 20_000, "no", "q2")
    fills = sim.on_trade(second)
    assert len(fills) == 1
    assert fills[0].qty_e4 == 10_000       # 1 contract, bounded by print size
    assert sim.sides["bid"].active.remaining_e4 == 20_000


def test_strict_through_ignores_at_price_and_fills_after_cross():
    sim = mm.FillSimulator(fill_rule="strict", place_latency_us=0,
                           cancel_latency_us=0)
    book = mm._book(4000, 5000)
    sim.update_book(book)
    sim.set_desired(desired(4000, None), 0)
    at = ev(1, 1, mm.EVENT_TYPE["TRADE"], book, 4000, 999_000, "no", "at")
    assert sim.on_trade(at) == []
    through = ev(2, 2, mm.EVENT_TYPE["TRADE"], book, 3900, 1, "no", "through")
    assert sim.on_trade(through)[0].qty_e4 == 10_000


def test_duplicate_trade_never_double_fills():
    sim = mm.FillSimulator(fill_rule="strict", place_latency_us=0,
                           cancel_latency_us=0)
    book = mm._book(4000, 5000)
    sim.update_book(book)
    sim.set_desired(desired(4000, None, 20_000), 0)
    trade = ev(1, 1, mm.EVENT_TYPE["TRADE"], book, 3900, 1, "no", "dup")
    assert len(sim.on_trade(trade)) == 1
    assert sim.on_trade(copy.copy(trade)) == []
    assert sim.duplicate_trades == 1


def test_invalid_book_pulls_desired_but_cancel_pending_exposure_survives():
    class PullOnInvalid(mm.StaticTouchPolicy):
        pass

    book = mm._book(4000, 5000)
    bad = mm.BookState(False)
    tape = [
        ev(0, 0, mm.EVENT_TYPE["BOOK_DELTA"], book),
        ev(20, 1, mm.EVENT_TYPE["BOOK_DELTA"], bad),
        # Placement active at 10; cancellation effective at 120, so this fill
        # must survive the gap/invalid-state pull.
        ev(50, 2, mm.EVENT_TYPE["TRADE"], bad, 3900, 1, "no", "gap-fill"),
        ev(200, 3, mm.EVENT_TYPE["BOOK_DELTA"], book),
    ]
    policy = PullOnInvalid(qty_e4=10_000, min_spread_e4=100,
                           max_inv_e4=100_000)
    result = mm.run_tape(tape, policy, fill_rule="strict",
                         place_latency_us=10, cancel_latency_us=100)
    assert result.fills == 1 and result.stale_fills == 1


def test_replay_is_deterministic_and_integer_wealth_identity_holds():
    tape = mm.demo_tape()
    kwargs = dict(fill_rule="strict", place_latency_us=20_000,
                  cancel_latency_us=20_000, maker_fee_e4=7)
    p1 = mm.StaticTouchPolicy(qty_e4=10_000, min_spread_e4=100,
                              max_inv_e4=100_000)
    p2 = mm.StaticTouchPolicy(qty_e4=10_000, min_spread_e4=100,
                              max_inv_e4=100_000)
    a, b = mm.run_tape(tape, p1, **kwargs), mm.run_tape(tape, p2, **kwargs)
    assert a == b
    # Net = gross - fees exactly; values were derived from integer E8 ledger.
    assert round(a.gross_pnl_dollars - a.fees_dollars, 8) == a.net_pnl_dollars


def test_dynamic_flow_guard_avoids_demo_toxic_short():
    tape = mm.demo_tape()
    static = mm.StaticTouchPolicy(qty_e4=10_000, min_spread_e4=100,
                                  max_inv_e4=100_000)
    dynamic = mm.DynamicPolicy(
        name="dynamic_guard", qty_e4=10_000, min_spread_e4=100,
        max_inv_e4=100_000, flow_window_us=2_000_000,
        flow_guard_threshold=0.8, flow_guard_min_trades=2)
    cfg = dict(fill_rule="strict", place_latency_us=20_000,
               cancel_latency_us=20_000)
    s = mm.run_tape(tape, static, **cfg)
    d = mm.run_tape(tape, dynamic, **cfg)
    assert s.fills == 1 and s.end_inventory == -1.0
    assert d.fills == 0 and d.end_inventory == 0.0


def test_out_of_order_tape_refuses_instead_of_resorting():
    book = mm._book(4000, 5000)
    tape = [ev(10, 2, mm.EVENT_TYPE["BOOK_DELTA"], book),
            ev(10, 1, mm.EVENT_TYPE["TRADE"], book, 3900, 1, "no", "late")]
    policy = mm.StaticTouchPolicy(qty_e4=10_000, min_spread_e4=100,
                                  max_inv_e4=100_000)
    try:
        mm.run_tape(tape, policy, fill_rule="strict",
                    place_latency_us=0, cancel_latency_us=0)
    except ValueError as exc:
        assert "strictly ordered" in str(exc)
    else:
        raise AssertionError("out-of-order tape was silently accepted")


def test_gold_adapter_reads_verified_fixture_and_labels_it_diagnostic():
    root = ROOT / "tests" / "fixtures" / "gold_defects" / "v6_trade_lookahead"
    tape, meta = mm.load_gold_tape(str(root), "2026-07-06", "KXGV-A",
                                  allow_unsafe=True)
    assert len(tape) == 6
    assert all(a.stream_seq < b.stream_seq for a, b in zip(tape, tape[1:]))
    assert meta["clock_status"].startswith("DIAGNOSTIC_ONLY")
    assert meta["sequence_status"].startswith("DIAGNOSTIC_ONLY")
