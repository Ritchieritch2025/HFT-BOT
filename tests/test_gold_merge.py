"""W2.3: merge iterator — global k-way merge of per-source typed-event lists
into one total order (PLAN_GOLD_DATA_CONTRACT §2.2 items 3-4). Contract
encoded here, written BEFORE tools/gold_merge.py exists (TDD):

  - total order on (ts_us, type_priority, source_file_order) with
    TRADE < BOOK_DELTA at equal ts_us — the same-µs delta is usually the
    decrement caused by the trade; the trade must see the PRE-trade book;
    trade-first extends to ALL book-state-bearing kinds at an equal µs
    (conservative: a trade is never credited with state that may postdate
    it); all non-trade kinds share one priority so within-source file
    order is never violated;
  - one forward cursor per source, look-ahead structurally impossible;
    a source that is not non-decreasing in (ts_us, type_priority) raises
    GoldMergeError ("Merge Order Violation") — fail-closed, never
    silently reordered (S2);
  - stream_seq: global, dense, 0-BASED (documented choice), strictly
    increasing in emit order;
  - book_seq: per-market, strictly increasing, gap-free (V3), minted
    exactly when the FSM mutates state (APPLIED or INVALIDATED — identical
    to W2.2's book_version); heartbeats/trades/refused deltas never mint;
  - trades reference only already-emitted book_seq: for every TRADE,
    ts(mint of its book_seq) <= ts_us(trade) AND merge-position(mint) <
    merge-position(trade) — structural V6 on the emitted stream.
"""
import json
import os

import pytest

from tools.gold_dtype import EVENT_TYPE, FLAGS
from tools.gold_fsm import APPLIED, INVALIDATED, NEUTRAL, REFUSED, GoldBookFSM
from tools.gold_load import BookMsg, Event, L1Top, Trade, load_full, load_trades
from tools.gold_merge import (
    STREAM_SEQ_BASE, GoldMergeError, MergedRecord, merge, type_priority,
    v3_violations, v6_violations,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
NOW_US = 1783382400000000       # freeze "now" for loader ts gates (fixtures)
T0 = 1783305736000000
S = 1_000_000                   # one second in µs
MT = "KXMERGE-UNIT"

SNAP, DELTA = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]
TRADE, L1, HB = EVENT_TYPE["TRADE"], EVENT_TYPE["L1_TICKER"], EVENT_TYPE["HEARTBEAT"]
MUTATING = (APPLIED, INVALIDATED)


def _snap(yes, no, mt=MT, ts=T0):
    return Event(SNAP, ts, mt, BookMsg(None, None, None, tuple(yes), tuple(no)))


def _delta(side, px, d, mt=MT, ts=T0):
    return Event(DELTA, ts, mt, BookMsg(side, px, d, None, None))


def _trade(tid, mt=MT, ts=T0, yp=4500, ct=30000, side="yes"):
    return Event(TRADE, ts, mt, Trade(tid, yp, 10000 - yp, ct, side))


def _l1(bid, bq, ask, aq, mt=MT, ts=T0, snap=False):
    return Event(L1, ts, mt, L1Top(bid, bq, ask, aq, snap))


def _hb(mt=MT, ts=T0):
    return Event(HB, ts, mt, None)


def _fixture_rows(name):
    rows = []
    with open(os.path.join(DEFECTS, "%s.ndjson" % name)) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


# ------------------------------------------------------------- total ordering

def test_total_order_ts_governs_across_sources():
    a = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
         _delta("yes", 4500, -10000, ts=T0 + 3 * S)]
    b = [_snap([(3000, 20000)], [(6500, 10000)], mt="KXMERGE-B", ts=T0 + S),
         _delta("yes", 3000, 5000, mt="KXMERGE-B", ts=T0 + 2 * S)]
    records, rep = merge([a, b])
    assert [r.ts_us for r in records] == [T0, T0 + S, T0 + 2 * S, T0 + 3 * S]
    assert [r.market_ticker for r in records] == [
        MT, "KXMERGE-B", "KXMERGE-B", MT]
    assert v3_violations(records) == []
    assert rep["records"] == 4 and rep["sources"] == 2


def test_stream_seq_dense_zero_based():
    assert STREAM_SEQ_BASE == 0    # documented choice: 0-based dense
    a = [_snap([(4500, 1)], [(5300, 1)], ts=T0), _hb(ts=T0 + S)]
    b = [_trade("t1", ts=T0 + 2 * S)]
    records, _ = merge([a, b])
    assert [r.stream_seq for r in records] == list(range(len(records)))


def test_same_us_trade_sorts_before_delta_sees_pre_trade_book():
    """The §2.2 core rule: TRADE < BOOK_DELTA at equal ts_us."""
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
            _delta("yes", 4500, -30000, ts=T0 + S)]
    trades = [_trade("t1", ts=T0 + S, ct=30000)]
    records, _ = merge([full, trades])
    assert [r.kind for r in records] == [SNAP, TRADE, DELTA]
    tr, dl = records[1], records[2]
    assert tr.ts_us == dl.ts_us                       # genuinely the same µs
    assert type_priority(TRADE) < type_priority(DELTA)
    assert tr.book_seq == 1                           # references the snapshot
    assert tr.state.bid_qty_e4[0] == 100000           # PRE-trade book
    assert dl.book_seq == 2
    assert dl.state.bid_qty_e4[0] == 70000            # decrement lands after
    assert v3_violations(records) == [] and v6_violations(records) == []


def test_same_us_trade_sorts_before_snapshot_and_l1():
    """Trade-first extends to every book-state-bearing kind at an equal µs
    (conservative pre-mutation view; never credits post-trade state)."""
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0)]
    trades = [_trade("t1", ts=T0)]
    records, _ = merge([full, trades])
    assert [r.kind for r in records] == [TRADE, SNAP]
    assert records[0].book_seq == 0                   # no book emitted yet
    l1s = [_l1(4500, 100000, 4700, 50000, mt="KXMERGE-L1", ts=T0)]
    trades2 = [_trade("t2", mt="KXMERGE-L1", ts=T0)]
    records2, _ = merge([l1s, trades2])
    assert [r.kind for r in records2] == [TRADE, L1]
    assert records2[0].book_seq == 0


def test_equal_key_ties_fall_to_source_then_file_order():
    a = [_snap([(4500, 1)], [(5300, 1)], mt="KXMERGE-A", ts=T0),
         _delta("yes", 4500, 1, mt="KXMERGE-A", ts=T0)]   # same µs, file order
    b = [_snap([(3000, 1)], [(6500, 1)], mt="KXMERGE-B", ts=T0)]
    records, _ = merge([a, b])
    assert [(r.source_index, r.source_pos) for r in records] == [
        (0, 0), (0, 1), (1, 0)]                       # source 0 run first
    assert records[1].kind == DELTA                   # file order preserved


# ------------------------------------------------------------ book_seq minting

def test_book_seq_mints_on_snapshot_and_valid_delta_only():
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
            _delta("yes", 4500, -30000, ts=T0 + S),
            _delta("yes", 4400, 50000, ts=T0 + 2 * S)]
    records, rep = merge([full])
    assert [r.book_seq for r in records] == [1, 2, 3]    # dense from 1
    assert [r.fsm_result for r in records] == [APPLIED] * 3
    assert rep["book_seq_mints"] == 3
    assert v3_violations(records) == []


def test_trades_and_heartbeats_never_mint_book_seq():
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0)]
    trades = [_trade("t1", ts=T0 + S)]
    hbs = [_hb(ts=T0 + 2 * S)]
    records, rep = merge([full, trades, hbs])
    assert [r.book_seq for r in records] == [1, 1, 1]
    assert [r.fsm_result for r in records] == [APPLIED, NEUTRAL, NEUTRAL]
    assert records[2].state == records[0].state       # heartbeat diffs nothing
    assert rep["book_seq_mints"] == 1 and rep["trades_emitted"] == 1


def test_invalidation_mints_refused_delta_does_not():
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
            _delta("yes", 4500, -150000, ts=T0 + S),      # negative => INVALID
            _delta("yes", 4400, 10000, ts=T0 + 2 * S),    # refused, no mint
            _snap([(4600, 20000)], [(5100, 10000)], ts=T0 + 3 * S)]
    records, _ = merge([full])
    assert [r.fsm_result for r in records] == [
        APPLIED, INVALIDATED, REFUSED, APPLIED]
    assert [r.book_seq for r in records] == [1, 2, 2, 3]  # gap-free minting
    assert not records[1].state.flags & FLAGS["F_BOOK_VALID"]
    assert v3_violations(records) == []


def test_per_market_book_seq_isolation():
    full = [_snap([(4500, 1)], [(5300, 1)], mt="KXMERGE-A", ts=T0),
            _snap([(3000, 1)], [(6500, 1)], mt="KXMERGE-B", ts=T0 + S),
            _delta("yes", 4500, 1, mt="KXMERGE-A", ts=T0 + 2 * S),
            _delta("yes", 3000, 1, mt="KXMERGE-B", ts=T0 + 3 * S),
            _delta("yes", 3000, 1, mt="KXMERGE-B", ts=T0 + 4 * S)]
    records, _ = merge([full])
    seqs = {}
    for r in records:
        seqs.setdefault(r.market_ticker, []).append(r.book_seq)
    assert seqs == {"KXMERGE-A": [1, 2], "KXMERGE-B": [1, 2, 3]}
    assert v3_violations(records) == []


# ------------------------------------------ trades reference emitted state (V6)

def test_trade_references_already_emitted_book_seq():
    full = [_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
            _delta("yes", 4500, -30000, ts=T0 + S),
            _delta("yes", 4400, 50000, ts=T0 + 3 * S)]
    trades = [_trade("t1", ts=T0 + 2 * S)]
    records, _ = merge([full, trades])
    tr = next(r for r in records if r.kind == TRADE)
    assert tr.book_seq == 2                            # the delta before it
    mint = next(r for r in records if r.book_seq == 2 and r.fsm_result in MUTATING)
    assert mint.ts_us <= tr.ts_us and mint.stream_seq < tr.stream_seq
    assert v6_violations(records) == []


def test_trade_before_any_book_state_gets_book_seq_zero():
    records, _ = merge([[_trade("t1", ts=T0)]])
    assert records[0].book_seq == 0
    assert records[0].state.flags == 0                 # zeroed unknown state
    assert v6_violations(records) == [] and v3_violations(records) == []


# ----------------------------------------------- fail-closed order precondition

def test_source_ts_regression_raises_merge_order_violation():
    bad = [_delta("yes", 4500, 1, ts=T0 + S), _snap([(4500, 1)], [(5300, 1)], ts=T0)]
    with pytest.raises(GoldMergeError, match="Merge Order Violation"):
        merge([bad])


def test_source_same_us_delta_then_trade_raises_not_reorders():
    """A mixed source with delta-then-trade at one µs cannot honor both file
    order and TRADE<BOOK_DELTA — fail closed, never silently reorder."""
    bad = [_snap([(4500, 1)], [(5300, 1)], ts=T0),
           _delta("yes", 4500, 1, ts=T0 + S), _trade("t1", ts=T0 + S)]
    with pytest.raises(GoldMergeError, match="Merge Order Violation"):
        merge([bad])


def test_seqs_shape_mismatch_raises():
    full = [_snap([(4500, 1)], [(5300, 1)], ts=T0)]
    with pytest.raises(GoldMergeError, match="seqs"):
        merge([full], seqs=[[1, 2]])
    with pytest.raises(GoldMergeError, match="seqs"):
        merge([full], seqs=[])


# ------------------------------------------------------------- seq passthrough

def test_post_w5_seq_gap_invalidates_and_minting_stays_gap_free():
    full = [_snap([(4000, 50000)], [(5500, 30000)], ts=T0),
            _delta("yes", 4000, 10000, ts=T0 + S),
            _delta("yes", 4000, 10000, ts=T0 + 2 * S)]   # seq gap below
    records, rep = merge([full], seqs=[[100, 101, 103]])
    assert [r.fsm_result for r in records] == [APPLIED, APPLIED, INVALIDATED]
    assert [r.book_seq for r in records] == [1, 2, 3]    # invalidation mints
    assert rep["fsm"]["Sequence Gap"] == 1
    assert rep["fsm"]["seq_mode"] != "seq_unavailable"
    assert v3_violations(records) == []


def test_pre_w5_no_seqs_reported_unavailable():
    records, rep = merge([[_snap([(4500, 1)], [(5300, 1)], ts=T0)]])
    assert rep["fsm"]["seq_mode"] == "seq_unavailable"


# --------------------------------------------------------------- empty / report

def test_empty_merge_is_valid_and_reported():
    records, rep = merge([])
    assert records == [] and rep["records"] == 0 and rep["sources"] == 0
    records, rep = merge([[], []])
    assert records == [] and rep["sources"] == 2


def test_report_fields():
    full = [_snap([(4500, 1)], [(5300, 1)], ts=T0)]
    _, rep = merge([full, [_trade("t1", ts=T0 + S)]])
    for key in ("sources", "records", "stream_seq_base", "trades_emitted",
                "book_seq_mints", "markets_minted", "fsm"):
        assert key in rep, key
    assert rep["stream_seq_base"] == 0 and rep["markets_minted"] == 1


# ---------------------------------------------- structural V6/V3, larger stream

def _synthetic_day():
    """Multi-market, multi-source stream exercising every merge behavior:
    same-µs trade/delta races, invalidation + recovery, uncovered trades,
    L1-only market, heartbeats."""
    full_a = [_snap([(4500, 100000), (4400, 50000)], [(5300, 70000)], mt="KXMERGE-A", ts=T0),
              _delta("yes", 4500, -30000, mt="KXMERGE-A", ts=T0 + S),
              _delta("no", 5300, -70000, mt="KXMERGE-A", ts=T0 + 2 * S),
              _delta("yes", 4400, -90000, mt="KXMERGE-A", ts=T0 + 3 * S),  # INVALID
              _delta("yes", 4400, 10000, mt="KXMERGE-A", ts=T0 + 4 * S),   # refused
              _snap([(4600, 20000)], [(5100, 10000)], mt="KXMERGE-A", ts=T0 + 5 * S),
              _delta("yes", 4600, -5000, mt="KXMERGE-A", ts=T0 + 6 * S)]
    full_b = [_snap([(3000, 20000)], [(6500, 10000)], mt="KXMERGE-B", ts=T0 + S // 2),
              _delta("yes", 3000, 5000, mt="KXMERGE-B", ts=T0 + 2 * S + 1),
              _delta("yes", 3100, 7000, mt="KXMERGE-B", ts=T0 + 5 * S + 1)]
    trades = [_trade("t-d0", mt="KXMERGE-D", ts=T0),                # uncovered
              _trade("t-a1", mt="KXMERGE-A", ts=T0 + S),            # same µs race
              _trade("t-b1", mt="KXMERGE-B", ts=T0 + 2 * S + 1),    # same µs race
              _trade("t-a2", mt="KXMERGE-A", ts=T0 + 4 * S),        # invalid book
              _trade("t-a3", mt="KXMERGE-A", ts=T0 + 6 * S)]
    l1_c = [_l1(4500, 100000, 4700, 50000, mt="KXMERGE-C", ts=T0 + S, snap=True),
            _l1(4600, 90000, 4800, 40000, mt="KXMERGE-C", ts=T0 + 3 * S)]
    hbs = [_hb(mt="KXMERGE-A", ts=T0 + 2 * S), _hb(mt="KXMERGE-A", ts=T0 + 5 * S)]
    return [full_a, full_b, trades, l1_c, hbs]


def test_structural_v6_and_v3_on_synthetic_day():
    records, rep = merge(_synthetic_day())
    assert v3_violations(records) == []
    assert v6_violations(records) == []
    # independent in-test re-derivation of V6 (not just the module checker):
    mint = {}
    trades_checked = 0
    for r in records:
        if r.fsm_result in MUTATING:
            mint[r.market_ticker] = r
        if r.kind == TRADE and r.book_seq:
            m = mint[r.market_ticker]
            assert m.book_seq == r.book_seq            # only already-emitted
            assert m.ts_us <= r.ts_us                  # ts(book_seq) <= ts(trade)
            assert m.stream_seq < r.stream_seq         # merge-pos(book) < merge-pos(trade)
            trades_checked += 1
    assert trades_checked == 4                          # t-a1 t-b1 t-a2 t-a3
    # same-µs races really saw the PRE-trade book:
    ta1 = next(r for r in records if r.kind == TRADE and r.event.payload.trade_id == "t-a1")
    assert ta1.state.bid_qty_e4[0] == 100000            # pre-decrement
    assert rep["trades_emitted"] == 5 and rep["markets_minted"] == 3


def test_merge_matches_fsm_oracle_replay():
    """Replaying the emitted order through a fresh W2.2 FSM reproduces every
    record's fsm_result, book_seq (== book_version) and state exactly."""
    records, _ = merge(_synthetic_day())
    fsm = GoldBookFSM()
    for r in records:
        assert fsm.on_event(r.event) == r.fsm_result
        assert fsm.version(r.market_ticker) == r.book_seq
        assert fsm.state(r.market_ticker) == r.state


# ------------------------------------- the checkers themselves can turn red

def test_v3_checker_catches_gap_dup_and_disorder():
    records, _ = merge([[_snap([(4500, 1)], [(5300, 1)], ts=T0),
                         _delta("yes", 4500, 1, ts=T0 + S)]])
    gap = [records[0], records[1]._replace(book_seq=3)]          # 1 -> 3 gap
    assert v3_violations(gap)
    dup = [records[0], records[1]._replace(book_seq=1)]          # no mint
    assert v3_violations(dup)
    sparse = [records[0], records[1]._replace(stream_seq=5)]     # not dense
    assert v3_violations(sparse)
    back = [records[0]._replace(ts_us=T0 + 2 * S), records[1]]   # ts regression
    assert v3_violations(back)


def test_v6_checker_catches_lookahead():
    records, _ = merge([[_snap([(4500, 100000)], [(5300, 70000)], ts=T0),
                         _delta("yes", 4500, -30000, ts=T0 + 2 * S)],
                        [_trade("t1", ts=T0 + S)]])
    snap_r, tr, dl = records
    assert (tr.kind, tr.book_seq) == (TRADE, 1)        # well-formed baseline
    # trade referencing a book_seq only minted LATER in the stream:
    doctored = [snap_r, tr._replace(book_seq=2), dl]
    assert v6_violations(doctored)
    # trade referencing a book_seq never minted at all:
    ghost = [snap_r, tr._replace(book_seq=7), dl]
    assert v6_violations(ghost)
    # mint positioned before the trade but with ts AFTER the trade
    # (ts(book_seq) <= ts(trade) must fail):
    backdated = [snap_r, dl._replace(stream_seq=1),
                 tr._replace(stream_seq=2, book_seq=2)]
    assert v6_violations(backdated)


# --------------------------------------- seeded merge defect fixtures (RED set)

def test_fixture_same_us_delta_before_trade_ordering_bug():
    """RED fixture: trade and its decrement delta share one µs. A mutant that
    sorts BOOK_DELTA before TRADE at equal ts_us serves the trade the
    POST-trade book (70000) and the post-trade book_seq — must go red."""
    full, rf = load_full(_fixture_rows("merge_same_us_full"), now_us=NOW_US)
    trades, rt = load_trades(_fixture_rows("merge_same_us_trades"), now_us=NOW_US)
    assert rf.malformed_record_count == 0 and rt.malformed_record_count == 0
    records, _ = merge([full, trades])
    assert [r.kind for r in records] == [SNAP, TRADE, DELTA]
    tr, dl = records[1], records[2]
    assert tr.ts_us == dl.ts_us
    assert tr.book_seq == 1 and dl.book_seq == 2
    assert tr.state.bid_qty_e4[0] == 100000            # PRE-trade book
    assert dl.state.bid_qty_e4[0] == 70000
    assert v6_violations(records) == [] and v3_violations(records) == []


def test_fixture_book_seq_gap_minting_dense_per_market():
    """RED fixture: two markets, applied/invalidated/refused mix + trades. A
    mutant that skips (+2) or duplicates (no-mint) book_seq minting breaks
    the dense per-market sequences and v3 — must go red."""
    full, rf = load_full(_fixture_rows("merge_book_seq_gap_full"), now_us=NOW_US)
    trades, rt = load_trades(_fixture_rows("merge_book_seq_gap_trades"), now_us=NOW_US)
    assert rf.malformed_record_count == 0 and rt.malformed_record_count == 0
    records, rep = merge([full, trades])
    minted = {}
    for r in records:
        if r.fsm_result in MUTATING:
            minted.setdefault(r.market_ticker, []).append(r.book_seq)
    assert minted == {"KXMERGE-GAPA": [1, 2, 3, 4],
                      "KXMERGE-GAPB": [1, 2, 3]}       # dense, gap-free (V3)
    results = {}
    for r in records:
        results.setdefault(r.market_ticker, []).append(r.fsm_result)
    # trade at the refused delta's µs sorts first (TRADE priority):
    assert results["KXMERGE-GAPB"] == [APPLIED, INVALIDATED, NEUTRAL, REFUSED, APPLIED]
    tr_a = next(r for r in records if r.kind == TRADE and r.market_ticker == "KXMERGE-GAPA")
    tr_b = next(r for r in records if r.kind == TRADE and r.market_ticker == "KXMERGE-GAPB")
    assert tr_a.book_seq == 1                          # same-µs race: pre-delta
    assert tr_b.book_seq == 2                          # invalidated state, emitted
    assert rep["book_seq_mints"] == 7
    assert v3_violations(records) == [] and v6_violations(records) == []
