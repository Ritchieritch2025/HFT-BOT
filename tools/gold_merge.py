"""W2.3 merge iterator (PLAN_GOLD_DATA_CONTRACT §2.2 items 3-4) — global
k-way merge of per-source typed-event lists (tools/gold_load.Event) into one
total order, minting stream_seq and book_seq. Pure logic: zero I/O, no clock,
no network — synthetic or loader-produced lists in, merged records out.

Total order key: (ts_us, type_priority, source_file_order), where
source_file_order is (source_index, position_in_source) — a "source" is any
file-ordered event list (one channel, or one channel x market slice; the
caller composes; per-market relative order is what the FSM replay needs).

  - type_priority: TRADE = 0, every other kind = 1. §2.2 mandates
    TRADE < BOOK_DELTA at an equal ts_us: the same-µs delta is usually the
    decrement caused by the trade, and the trade must see the PRE-trade
    book. The rule extends to ALL book-state-bearing kinds (snapshot, L1)
    — at an equal microsecond a trade always sees the pre-mutation book,
    the conservative direction for V6 (a trade is never credited with
    state that may postdate it). All non-trade kinds share ONE priority on
    purpose: distinct priorities would reorder same-µs events inside a
    single source, violating file order — the FSM's replay contract.
  - one forward cursor per source (a heap of source heads); look-ahead is
    structurally impossible — no timestamp indexing, no random access,
    each source element is seen exactly once, when its cursor reaches it.
    Precondition, fail-closed (S2): every source must be non-decreasing in
    (ts_us, type_priority). Loader lists satisfy it per channel x market
    (trades are stable-sorted by W2.1; book/L1 rows follow file order per
    market). A violating source raises GoldMergeError with the operator
    vocabulary "Merge Order Violation" — never silently reordered
    (W2.1-reported ts regressions therefore fail the merge; disposition
    policy belongs to W2.5/W2.6, see docs/BACKLOG.md).
  - stream_seq: global, dense, 0-BASED (STREAM_SEQ_BASE — documented
    choice), strictly increasing in emit order; every merged event,
    including heartbeats and refused deltas, gets exactly one.
  - book_seq: per-market, strictly increasing, gap-free (V3): minted as
    last+1 exactly when the FSM mutates state — result APPLIED (snapshot
    load, valid delta, L1-on-uncovered) or INVALIDATED (the transition to
    Invalid Book State is itself a state mutation). Identical by
    construction to W2.2's book_version, which increments on exactly those
    outcomes (test-enforced against the FSM as an oracle). Heartbeats,
    trades, refused deltas and L1-on-covered NEVER mint (V9). The first
    mint per market is 1; book_seq 0 means "no book state has ever been
    emitted for this market".
  - trades reference only already-emitted book_seq: a TRADE record carries
    its market's current book_seq, minted at an earlier merge position
    with ts_us <= the trade's (V6) — checked structurally on the emitted
    stream by v6_violations(); v3_violations() checks the V3 half. Both
    checkers read ONLY emitted records, so W2.5 can reuse them verbatim.

stdlib only.
"""
import heapq
import os
import sys
from collections import namedtuple

try:
    from tools.gold_dtype import EVENT_TYPE          # frozen W1 constants
    from tools.gold_fsm import APPLIED, INVALIDATED, GoldBookFSM
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import EVENT_TYPE
    from gold_fsm import APPLIED, INVALIDATED, GoldBookFSM

STREAM_SEQ_BASE = 0          # documented choice: stream_seq is 0-based dense
_TRADE = EVENT_TYPE["TRADE"]
_MUTATING = (APPLIED, INVALIDATED)   # exactly the outcomes that mint book_seq

# One GoldRecord-shaped view per event: identity/order fields, the minted
# seqs, the FSM outcome, and the BookState as-of this record (post-apply for
# book events; the untouched pre-trade book for trades — §2.2 item 4).
MergedRecord = namedtuple("MergedRecord",
                          "stream_seq ts_us kind market_ticker book_seq "
                          "fsm_result state event source_index source_pos")


class GoldMergeError(Exception):
    """Contract violation surfaced to callers — fail closed, never repaired."""


def type_priority(kind):
    """TRADE first at an equal microsecond; all other kinds share one
    priority so within-source file order is never reordered."""
    return 0 if kind == _TRADE else 1


def merge(sources, seqs=None):
    """K-way merge per-source Event lists into (records, report).

    sources: list of Event lists, each non-decreasing in
    (ts_us, type_priority). seqs: None (pre-W5: no seq columns exist) or a
    list mirroring sources' shape with a per-event seq-or-None, handed to
    the W2.2 FSM for per-market gap detection (post-W5 hook)."""
    if seqs is not None and (
            len(seqs) != len(sources) or
            any(len(q) != len(s) for q, s in zip(seqs, sources))):
        raise GoldMergeError("seqs must mirror sources' shape exactly")
    fsm = GoldBookFSM()
    book_seq = {}                # market_ticker -> last minted book_seq
    records, heap = [], []
    trades = mints = 0

    def push(i, pos, prev_key):  # advance source i's single forward cursor
        src = sources[i]
        if pos >= len(src):
            return
        ev = src[pos]
        key = (ev.ts_us, type_priority(ev.kind))
        if prev_key is not None and key < prev_key:
            raise GoldMergeError(
                "Merge Order Violation: source %d event %d (ts_us=%d, "
                "type_priority=%d) sorts before its predecessor (ts_us=%d, "
                "type_priority=%d); sources must be non-decreasing in "
                "(ts_us, type_priority) — never silently reordered"
                % (i, pos, key[0], key[1], prev_key[0], prev_key[1]))
        heapq.heappush(heap, (key[0], key[1], i, pos, ev))

    for i in range(len(sources)):
        push(i, 0, None)
    while heap:
        ts, prio, i, pos, ev = heapq.heappop(heap)
        res = fsm.on_event(ev, seq=None if seqs is None else seqs[i][pos])
        mt = ev.market_ticker
        if res in _MUTATING:     # state mutated: mint the next book version
            book_seq[mt] = book_seq.get(mt, 0) + 1
            mints += 1
        if ev.kind == _TRADE:
            trades += 1
        records.append(MergedRecord(
            STREAM_SEQ_BASE + len(records), ev.ts_us, ev.kind, mt,
            book_seq.get(mt, 0), res, fsm.state(mt), ev, i, pos))
        push(i, pos + 1, (ts, prio))
    report = {"sources": len(sources),
              "records": len(records),
              "stream_seq_base": STREAM_SEQ_BASE,
              "trades_emitted": trades,
              "book_seq_mints": mints,
              "markets_minted": len(book_seq),
              "fsm": fsm.report()}
    return records, report


def v3_violations(records):
    """Structural V3 on an emitted stream: stream_seq dense from
    STREAM_SEQ_BASE; (ts_us, stream_seq) non-decreasing; per-market book_seq
    minted +1 gap-free by exactly the mutating outcomes and frozen by all
    others. Returns violation strings; empty list = pass."""
    out, cur, prev_ts = [], {}, None
    for i, r in enumerate(records):
        if r.stream_seq != STREAM_SEQ_BASE + i:
            out.append("stream_seq not dense at index %d: got %d want %d"
                       % (i, r.stream_seq, STREAM_SEQ_BASE + i))
        if prev_ts is not None and r.ts_us < prev_ts:
            out.append("ts_us decreased at stream_seq %d: %d < %d"
                       % (r.stream_seq, r.ts_us, prev_ts))
        prev_ts = r.ts_us
        last = cur.get(r.market_ticker, 0)
        want = last + 1 if r.fsm_result in _MUTATING else last
        if r.book_seq != want:
            out.append("book_seq gap/dup at stream_seq %d (%s, %s): got %d "
                       "want %d" % (r.stream_seq, r.market_ticker,
                                    r.fsm_result, r.book_seq, want))
        cur[r.market_ticker] = r.book_seq
    return out


def v6_violations(records):
    """Structural V6 (no look-ahead) on an emitted stream: every TRADE's
    book_seq is the market's LAST minted book version, minted at an earlier
    merge position with ts(mint) <= ts(trade); book_seq 0 only before any
    mint. Returns violation strings; empty list = pass."""
    out, mint = [], {}           # market_ticker -> minting record
    for r in records:
        if r.fsm_result in _MUTATING:
            mint[r.market_ticker] = r
        if r.kind != _TRADE:
            continue
        m = mint.get(r.market_ticker)
        if r.book_seq == 0:
            if m is not None:
                out.append("trade at stream_seq %d has book_seq 0 but %s "
                           "state was already emitted (book_seq %d)"
                           % (r.stream_seq, r.market_ticker, m.book_seq))
            continue
        if m is None or m.book_seq != r.book_seq:
            out.append("trade at stream_seq %d references book_seq %d which "
                       "is not the last emitted for %s (%s)"
                       % (r.stream_seq, r.book_seq, r.market_ticker,
                          "none minted" if m is None else "last=%d" % m.book_seq))
            continue
        if m.ts_us > r.ts_us:
            out.append("look-ahead: trade at stream_seq %d (ts_us=%d) "
                       "references book_seq %d minted at ts_us=%d"
                       % (r.stream_seq, r.ts_us, r.book_seq, m.ts_us))
        if m.stream_seq >= r.stream_seq:
            out.append("look-ahead: trade at stream_seq %d references "
                       "book_seq %d minted at merge position %d"
                       % (r.stream_seq, r.book_seq, m.stream_seq))
    return out
