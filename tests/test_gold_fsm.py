"""W2.2: book FSM — pure per-market state machine over W2.1 typed events
(PLAN_GOLD_DATA_CONTRACT §2.2 item 2, semantics reference
include/kalshi/orderbook.hpp). Contract encoded here, written BEFORE
tools/gold_fsm.py exists (TDD):

  - snapshot => load levels + valid + F_FROM_SNAPSHOT; deltas before the
    first snapshot are refused (a book has no data until its snapshot);
  - delta driving any level negative => Invalid Book State, NO clamp;
  - invalid stays invalid (deltas refused, state zeroed, never resurrected)
    until a LATER snapshot revalidates; no later snapshot => permanently
    F_BOOK_VALID=0;
  - heartbeats and trades NEVER mutate state or the book version (V9);
  - crossed book (best yes bid >= best yes ask, yes-space) => F_CROSSED,
    flagged never repaired;
  - yes-space transform: ask_px_e4 = 10000 - no_price_e4 (No bids), asks
    best-first ascending;
  - GoldRecord book arrays: DEPTH best levels/side best-first, tail
    aggregated into *_rest_qty_e4 (never dropped silently), full nlevels;
  - uncovered (L1-only) markets: top-of-book in slot 0, F_BOOK_COVERED=0;
  - both seq modes with no W5 dependency: no seq => file order + report
    states seq_unavailable; seq present => gap => Invalid + Resync Required.
"""
import json
import os

from tools.gold_dtype import DEPTH, EVENT_TYPE, FLAGS
from tools.gold_load import BookMsg, Event, L1Top, Trade, load_full
from tools.gold_fsm import (
    APPLIED, INVALIDATED, NEUTRAL, REFUSED, SEQ_UNAVAILABLE, GoldBookFSM,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
NOW_US = 1783382400000000  # freeze "now" for the loader ts gate
TS = 1783305736000000
MT = "KXFSM-UNIT"

VALID = FLAGS["F_BOOK_VALID"]
COVERED = FLAGS["F_BOOK_COVERED"]
CROSSED = FLAGS["F_CROSSED"]
FROM_SNAP = FLAGS["F_FROM_SNAPSHOT"]
Z16 = (0,) * DEPTH


def _snap(yes, no, mt=MT, ts=TS):
    return Event(EVENT_TYPE["BOOK_SNAPSHOT"], ts, mt,
                 BookMsg(None, None, None, tuple(yes), tuple(no)))


def _delta(side, px, d, mt=MT, ts=TS):
    return Event(EVENT_TYPE["BOOK_DELTA"], ts, mt, BookMsg(side, px, d, None, None))


def _hb(mt=MT, ts=TS):
    return Event(EVENT_TYPE["HEARTBEAT"], ts, mt, None)


def _trade(mt=MT, ts=TS):
    return Event(EVENT_TYPE["TRADE"], ts, mt,
                 Trade("t-1", 4500, 5500, 10000, "yes"))


def _l1(bid, bq, ask, aq, snap=False, mt=MT, ts=TS):
    return Event(EVENT_TYPE["L1_TICKER"], ts, mt, L1Top(bid, bq, ask, aq, snap))


def _zeroed(st):
    return (st.bid_px_e4 == Z16 and st.ask_px_e4 == Z16 and
            st.bid_qty_e4 == Z16 and st.ask_qty_e4 == Z16 and
            st.bid_rest_qty_e4 == 0 and st.ask_rest_qty_e4 == 0 and
            st.bid_nlevels == 0 and st.ask_nlevels == 0)


def _defect_events(name):
    """FSM defect fixtures are loader-clean by construction; the defect is a
    stream-semantics defect the FSM must catch, not a row-shape defect."""
    rows = []
    with open(os.path.join(DEFECTS, "%s.ndjson" % name)) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    events, rep = load_full(rows, now_us=NOW_US)
    assert rep.malformed_record_count == 0, dict(rep.reasons)
    return events, rows


# ------------------------------------------------------------- snapshot basics

def test_snapshot_loads_levels_valid_from_snapshot():
    fsm = GoldBookFSM()
    assert fsm.on_event(_snap([(4500, 100000), (4400, 50000)],
                              [(5300, 70000)])) == APPLIED
    st = fsm.state(MT)
    assert st.flags == VALID | COVERED | FROM_SNAP
    assert st.bid_px_e4[:2] == (4500, 4400)          # best-first (highest bid)
    assert st.bid_qty_e4[:2] == (100000, 50000)
    assert st.ask_px_e4[0] == 4700                   # 10000 - 5300
    assert st.ask_qty_e4[0] == 70000
    assert st.bid_px_e4[2:] == Z16[2:] and st.ask_px_e4[1:] == Z16[1:]
    assert (st.bid_nlevels, st.ask_nlevels) == (2, 1)
    assert st.bid_rest_qty_e4 == 0 and st.ask_rest_qty_e4 == 0


def test_yes_space_transform_and_ask_ordering():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([], [(9700, 10000), (5200, 20000), (8000, 30000)]))
    st = fsm.state(MT)
    # No bids 9700/8000/5200 => Yes asks 300/2000/4800, best (lowest) first
    assert st.ask_px_e4[:3] == (300, 2000, 4800)
    assert st.ask_qty_e4[:3] == (10000, 30000, 20000)
    assert st.bid_nlevels == 0 and st.ask_nlevels == 3
    assert not st.flags & CROSSED                    # one-sided is never crossed


def test_unknown_market_serves_zeroed_state():
    st = GoldBookFSM().state("KXNEVER-SEEN")
    assert st.flags == 0 and _zeroed(st)


# --------------------------------------------------------------- delta algebra

def test_valid_deltas_mutate_add_update_remove():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    assert fsm.on_event(_delta("yes", 4400, 50000)) == APPLIED    # add level
    assert fsm.on_event(_delta("yes", 4500, -40000)) == APPLIED   # decrement
    assert fsm.on_event(_delta("no", 5300, -70000)) == APPLIED    # remove level
    st = fsm.state(MT)
    assert st.flags == VALID | COVERED           # FROM_SNAPSHOT cleared by delta
    assert st.bid_px_e4[:2] == (4500, 4400)
    assert st.bid_qty_e4[:2] == (60000, 50000)
    assert st.ask_nlevels == 0 and st.ask_px_e4 == Z16   # 0-qty level erased


def test_delta_before_snapshot_refused():
    fsm = GoldBookFSM()
    assert fsm.on_event(_delta("yes", 4500, 10000)) == REFUSED
    st = fsm.state(MT)
    assert st.flags == 0 and _zeroed(st)         # no data until first snapshot
    assert fsm.report()["deltas_refused_while_invalid"] == 1


# ------------------------------------------- negative delta: INVALID, no clamp

def test_negative_delta_invalidates_no_clamp():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5200, 80000)]))
    assert fsm.on_event(_delta("yes", 4500, -150000)) == INVALIDATED
    st = fsm.state(MT)
    assert not st.flags & VALID
    assert st.flags & COVERED                    # coverage is a subscription fact
    assert _zeroed(st)                           # never stale, never clamped-to-0
    # a clamping implementation would now hold a "valid" resurrected level:
    assert fsm.on_event(_delta("yes", 4500, 50000)) == REFUSED
    st = fsm.state(MT)
    assert not st.flags & VALID and _zeroed(st)


def test_invalid_until_later_snapshot_revalidates():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5200, 80000)]))
    fsm.on_event(_delta("yes", 4500, -150000))
    for _ in range(3):                            # stays invalid under pressure
        assert fsm.on_event(_delta("yes", 4600, 10000)) == REFUSED
        assert not fsm.state(MT).flags & VALID
    assert fsm.on_event(_snap([(4600, 70000)], [(5100, 60000)])) == APPLIED
    st = fsm.state(MT)
    assert st.flags == VALID | COVERED | FROM_SNAP
    assert st.bid_px_e4[0] == 4600 and st.bid_qty_e4[0] == 70000
    assert st.bid_nlevels == 1                    # old 4500 level did NOT survive
    assert st.ask_px_e4[0] == 4900
    assert fsm.on_event(_delta("yes", 4600, -20000)) == APPLIED
    assert fsm.state(MT).bid_qty_e4[0] == 50000


def test_no_later_snapshot_permanent_invalid():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4200, 30000)], [(5400, 20000)]))
    fsm.on_event(_delta("yes", 4200, -40000))
    for i in range(10):
        fsm.on_event(_delta("yes", 4000 + i, 10000))
        fsm.on_event(_hb())
        st = fsm.state(MT)
        assert not st.flags & VALID and _zeroed(st)
    assert fsm.report()["Invalid Book State"] == 1


# ------------------------------------------------- heartbeat / trade neutrality

def test_heartbeat_neutrality_v9():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    before, ver = fsm.state(MT), fsm.version(MT)
    assert fsm.on_event(_hb()) == NEUTRAL
    assert fsm.state(MT) == before               # diffs no state
    assert fsm.version(MT) == ver                # advances no book version
    assert fsm.report()["heartbeats_seen"] == 1


def test_heartbeat_never_revalidates_invalid_book():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    fsm.on_event(_delta("yes", 4500, -200000))
    before, ver = fsm.state(MT), fsm.version(MT)
    assert fsm.on_event(_hb()) == NEUTRAL
    assert fsm.state(MT) == before and fsm.version(MT) == ver
    assert not fsm.state(MT).flags & VALID


def test_trade_events_never_mutate_book():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    before, ver = fsm.state(MT), fsm.version(MT)
    assert fsm.on_event(_trade()) == NEUTRAL
    assert fsm.state(MT) == before and fsm.version(MT) == ver


# --------------------------------------------------------------- crossed books

def test_crossed_book_flagged_not_repaired():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(6000, 10000)], [(4600, 20000)]))  # ask 5400 < bid 6000
    st = fsm.state(MT)
    assert st.flags & CROSSED and st.flags & VALID
    assert st.bid_px_e4[0] == 6000 and st.ask_px_e4[0] == 5400  # kept as-is


def test_touching_book_bid_equals_ask_is_crossed():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(5000, 10000)], [(5000, 10000)]))  # ask 5000 == bid 5000
    assert fsm.state(MT).flags & CROSSED


def test_uncrossed_book_not_flagged():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 10000)], [(5300, 10000)]))  # ask 4700 > bid 4500
    assert not fsm.state(MT).flags & CROSSED


# ------------------------------------------------- depth truncation, rest aggs

def test_depth_tail_aggregated_never_dropped_silently():
    yes = [(p, p * 10) for p in range(100, 2100, 100)]      # 20 bid levels
    no = [(p, p) for p in range(100, 1900, 100)]            # 18 no-bid levels
    fsm = GoldBookFSM()
    fsm.on_event(_snap(yes, no))
    st = fsm.state(MT)
    assert st.bid_px_e4 == tuple(range(2000, 400, -100))    # 16 best, best-first
    assert st.bid_qty_e4 == tuple(p * 10 for p in range(2000, 400, -100))
    assert st.bid_rest_qty_e4 == (400 + 300 + 200 + 100) * 10
    assert st.bid_nlevels == 20                              # full count kept
    assert st.ask_px_e4 == tuple(range(8200, 9800, 100))    # 16 best asks
    assert st.ask_qty_e4 == tuple(10000 - a for a in range(8200, 9800, 100))
    assert st.ask_rest_qty_e4 == 200 + 100                  # no-bids 200 + 100
    assert st.ask_nlevels == 18


# ---------------------------------------------------- L1-only (uncovered) books

def test_l1_only_market_top_of_book_slot0_uncovered():
    fsm = GoldBookFSM()
    assert fsm.on_event(_l1(4500, 100000, 4700, 50000)) == APPLIED
    st = fsm.state(MT)
    assert st.flags == VALID                       # F_BOOK_COVERED absent
    assert st.bid_px_e4[0] == 4500 and st.bid_qty_e4[0] == 100000
    assert st.ask_px_e4[0] == 4700 and st.ask_qty_e4[0] == 50000
    assert (st.bid_nlevels, st.ask_nlevels) == (1, 1)
    assert st.bid_px_e4[1:] == Z16[1:] and st.ask_px_e4[1:] == Z16[1:]


def test_l1_empty_side_sentinels_zero_that_side():
    fsm = GoldBookFSM()
    fsm.on_event(_l1(0, 0, 4700, 50000))           # no bid sentinel
    st = fsm.state(MT)
    assert st.bid_nlevels == 0 and st.bid_px_e4[0] == 0
    assert st.ask_px_e4[0] == 4700
    fsm.on_event(_l1(4500, 100000, 10000, 0))      # no ask sentinel
    st = fsm.state(MT)
    assert st.ask_nlevels == 0 and st.ask_px_e4[0] == 0
    assert st.bid_px_e4[0] == 4500
    assert not st.flags & CROSSED


def test_l1_snapshot_flag_and_crossed():
    fsm = GoldBookFSM()
    fsm.on_event(_l1(4500, 100000, 4700, 50000, snap=True))
    assert fsm.state(MT).flags == VALID | FROM_SNAP
    fsm.on_event(_l1(5000, 100000, 4900, 50000))   # bid >= ask in yes-space
    assert fsm.state(MT).flags == VALID | CROSSED


def test_l1_neutral_on_covered_market():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    before, ver = fsm.state(MT), fsm.version(MT)
    assert fsm.on_event(_l1(9000, 1, 9100, 1)) == NEUTRAL
    assert fsm.state(MT) == before and fsm.version(MT) == ver


def test_l1_then_snapshot_promotes_to_covered_depth():
    fsm = GoldBookFSM()
    fsm.on_event(_l1(4500, 100000, 4700, 50000))
    fsm.on_event(_snap([(4400, 30000)], [(5400, 20000)]))
    st = fsm.state(MT)
    assert st.flags & COVERED and st.bid_px_e4[0] == 4400


# ---------------------------------------------------------- sequencing modes

def test_pre_w5_no_seq_file_order_governs_and_reported():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)]))
    fsm.on_event(_delta("yes", 4500, -50000))
    assert fsm.state(MT).flags & VALID             # no false gap invalidation
    assert fsm.report()["seq_mode"] == SEQ_UNAVAILABLE


def test_post_w5_seq_gap_invalid_resync_required_then_snapshot_recovers():
    fsm = GoldBookFSM()
    assert fsm.on_event(_snap([(4000, 50000)], [(5500, 30000)]), seq=100) == APPLIED
    assert fsm.on_event(_delta("yes", 4000, 10000), seq=101) == APPLIED
    assert fsm.on_event(_delta("yes", 4000, 10000), seq=103) == INVALIDATED  # gap
    st = fsm.state(MT)
    assert not st.flags & VALID and _zeroed(st)
    rep = fsm.report()
    assert rep["Sequence Gap"] == 1 and rep["Resync Required"] == 1
    assert rep["seq_mode"] != SEQ_UNAVAILABLE
    assert fsm.on_event(_delta("yes", 4000, 10000), seq=104) == REFUSED
    assert fsm.on_event(_snap([(4100, 20000)], [(5600, 10000)]), seq=200) == APPLIED
    assert fsm.on_event(_delta("yes", 4100, 5000), seq=201) == APPLIED
    assert fsm.state(MT).bid_qty_e4[0] == 25000


def test_per_market_isolation():
    fsm = GoldBookFSM()
    fsm.on_event(_snap([(4500, 100000)], [(5300, 70000)], mt="KXFSM-A"))
    fsm.on_event(_snap([(3000, 20000)], [(6500, 10000)], mt="KXFSM-B"))
    fsm.on_event(_delta("yes", 4500, -900000, mt="KXFSM-A"))   # kills A only
    assert not fsm.state("KXFSM-A").flags & VALID
    stb = fsm.state("KXFSM-B")
    assert stb.flags & VALID and stb.bid_px_e4[0] == 3000
    assert fsm.report()["Invalid Book State"] == 1


def test_report_operator_vocabulary():
    rep = GoldBookFSM().report()
    for key in ("Invalid Book State", "Resync Required", "Sequence Gap",
                "seq_mode", "markets", "deltas_refused_while_invalid",
                "heartbeats_seen"):
        assert key in rep, key
    assert rep["seq_mode"] == SEQ_UNAVAILABLE      # pre-W5 default, stated


# ------------------------------------- seeded FSM defect fixtures (must catch)

def test_fixture_clamp_bug_sequence_hides_negative_unless_latched():
    """RED fixture: snapshot 100000 -> delta -150000 -> delta +50000. A
    clamping mutant would serve a 'valid' 50000 level; the latch must not."""
    events, _ = _defect_events("fsm_clamp_bug")
    fsm = GoldBookFSM()
    assert [fsm.on_event(e) for e in events] == [APPLIED, INVALIDATED, REFUSED]
    st = fsm.state("KXFSM-CLAMP")
    assert not st.flags & VALID and _zeroed(st)
    assert st.bid_qty_e4[0] == 0                   # NOT 50000: clamp would lie


def test_fixture_stale_state_after_invalid_never_resurrects():
    """RED fixture: delta stream continues after invalidation with adds that
    would rebuild a plausible book; state must stay invalid/zeroed."""
    events, _ = _defect_events("fsm_stale_after_invalid")
    fsm = GoldBookFSM()
    assert fsm.on_event(events[0]) == APPLIED
    assert fsm.on_event(events[1]) == INVALIDATED
    for ev in events[2:]:
        assert fsm.on_event(ev) == REFUSED
        st = fsm.state("KXFSM-STALE")
        assert not st.flags & VALID and _zeroed(st)


def test_fixture_negative_delta_no_side():
    events, _ = _defect_events("fsm_negative_delta")
    fsm = GoldBookFSM()
    assert [fsm.on_event(e) for e in events] == [APPLIED, INVALIDATED]
    st = fsm.state("KXFSM-NEG")
    assert not st.flags & VALID and _zeroed(st)


def test_fixture_crossed_book_flagged():
    events, _ = _defect_events("fsm_crossed_book")
    fsm = GoldBookFSM()
    fsm.on_event(events[0])
    st = fsm.state("KXFSM-CROSS")
    assert st.flags & CROSSED and st.flags & VALID
    assert st.bid_px_e4[0] == 6000 and st.ask_px_e4[0] == 5400


def test_fixture_seq_gap_invalid_until_snapshot():
    events, rows = _defect_events("fsm_seq_gap")
    fsm = GoldBookFSM()
    results = [fsm.on_event(e, seq=r["seq"]) for e, r in zip(events, rows)]
    assert results == [APPLIED, APPLIED, INVALIDATED, REFUSED, APPLIED, APPLIED]
    rep = fsm.report()
    assert rep["Sequence Gap"] == 1 and rep["Resync Required"] == 1
    st = fsm.state("KXFSM-GAP")
    assert st.flags & VALID and st.bid_px_e4[0] == 4100
    assert st.bid_qty_e4[0] == 25000


def test_fixture_missing_snapshot_after_invalidation_permanent():
    events, _ = _defect_events("fsm_missing_snapshot_after_invalid")
    fsm = GoldBookFSM()
    assert fsm.on_event(events[0]) == APPLIED
    assert fsm.on_event(events[1]) == INVALIDATED
    for ev in events[2:]:
        assert fsm.on_event(ev) == REFUSED
        st = fsm.state("KXFSM-NOSNAP")
        assert not st.flags & VALID and _zeroed(st)
    rep = fsm.report()
    assert rep["Invalid Book State"] == 1 and rep["Resync Required"] == 1
