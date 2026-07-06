"""W2.2 book FSM (PLAN_GOLD_DATA_CONTRACT §2.2 item 2) — pure per-market book
state machine over the W2.1 typed events (tools/gold_load.Event). Zero I/O:
no files, no network, no clock — deterministic replay only. Semantics mirror
include/kalshi/orderbook.hpp (read-only reference):

  - a book has NO data until its first snapshot; deltas before it are refused;
  - snapshot => load levels (non-positive sizes dropped, mirroring
    load_snapshot) + valid + F_FROM_SNAPSHOT (cleared by the first applied
    delta: the state is then no longer verbatim snapshot content);
  - a delta driving any level negative means the book is ALREADY wrong:
    Invalid Book State, NO clamp; levels are cleared so nothing can ever
    serve stale data (the hpp keeps levels but refuses accessors — clearing
    gives the same observable zeros with a stronger no-resurrection bound);
  - once invalid every delta is refused without mutating; ONLY a later
    snapshot revalidates ("self-healing" = deterministic replay recovery,
    never silent repair, never production resync);
  - crossed (best yes bid >= best yes ask, yes-space) => F_CROSSED, flagged
    never repaired; heartbeats and trades NEVER mutate state or advance the
    book version (V9);
  - seq, both modes, no W5 dependency: events without seq (pre-W5) => file
    order governs and every report states "seq_unavailable"; events with seq
    (post-W5) => per-market gap (seq != last+1) => Invalid Book State +
    Sequence Gap, recovered only by a later snapshot. Every invalidation
    (gap OR negative delta) counts as Resync Required — the hpp reference
    requests a resync on corruption exactly as on a gap.

Yes-space: yes_levels are Yes bids as-is; no_levels are No bids, transformed
ask_px_e4 = 10000 - no_price_e4 (a No bid at q is a Yes ask at 1-q), asks
best-first ascending. BookState carries exactly the GoldRecord §2.1 book
fields: DEPTH best levels per side best-first, the tail beyond DEPTH
aggregated into *_rest_qty_e4 (never dropped silently), full per-side
*_nlevels, and flags from the frozen W1 FLAGS constants. Uncovered (L1-only)
markets serve top-of-book in slot 0 with F_BOOK_COVERED=0 and F_BOOK_VALID=1
(the top is real data; empty-side sentinels bid=0 / ask=10000 zero their
side). Covered markets keep F_BOOK_COVERED while invalid: coverage is a
subscription fact, validity a state fact. Invalid or unknown books serve
zeroed arrays + F_BOOK_VALID=0. stdlib only.
"""
import os
import sys
from collections import Counter, namedtuple

try:
    from tools.gold_dtype import DEPTH, EVENT_TYPE, FLAGS  # frozen W1 constants
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import DEPTH, EVENT_TYPE, FLAGS

# apply() outcomes — mirror kalshi::ApplyResult (Ok / NeedResync / Invalid)
# plus an explicit no-op for the never-mutating event kinds.
APPLIED = "applied"            # Ok: state mutated (or loaded)
NEUTRAL = "neutral"            # heartbeat/trade/L1-on-covered: zero effect
REFUSED = "refused_invalid"    # NeedResync: delta while invalid/pre-snapshot
INVALIDATED = "invalidated"    # Invalid: this event proved the book wrong

SEQ_UNAVAILABLE = "seq_unavailable"  # pre-W5: no seq columns exist anywhere

BookState = namedtuple("BookState",
                       "flags bid_px_e4 bid_qty_e4 ask_px_e4 ask_qty_e4 "
                       "bid_rest_qty_e4 ask_rest_qty_e4 bid_nlevels ask_nlevels")

_Z16 = (0,) * DEPTH


class GoldFSMError(Exception):
    """Contract violation surfaced to callers (e.g. unknown event kind)."""


def _zero_state(flags):
    return BookState(flags, _Z16, _Z16, _Z16, _Z16, 0, 0, 0, 0)


def _side_arrays(levels):
    """Best-first (px, qty) list -> (px16, qty16, rest_qty, nlevels). The tail
    beyond DEPTH is aggregated, never dropped silently (§2.1)."""
    px, qty = list(_Z16), list(_Z16)
    for i, (p, q) in enumerate(levels[:DEPTH]):
        px[i], qty[i] = p, q
    rest = sum(q for _, q in levels[DEPTH:])
    return tuple(px), tuple(qty), rest, len(levels)


class MarketBookFSM(object):
    """One market's book state. Pure; counters is a shared Counter owned by
    GoldBookFSM so per-reason accounting lands in one operator report."""

    def __init__(self, market_ticker, counters=None):
        self.market_ticker = market_ticker
        self.counters = counters if counters is not None else Counter()
        self.covered = False        # has ever seen a full-depth snapshot
        self.valid = False          # no data until the first snapshot
        self.from_snapshot = False
        self.yes = {}               # Yes-bid price_e4 -> qty_e4
        self.no = {}                # No-bid  price_e4 -> qty_e4
        self.l1 = None              # last L1Top (uncovered markets only)
        self.book_version = 0       # advanced ONLY by state-mutating events
        self.last_seq = None        # None until a seq is observed (pre-W5)

    def _invalidate(self, reason):
        self.valid = False
        self.from_snapshot = False
        self.yes.clear()            # zeroed, never stale — nothing to resurrect
        self.no.clear()
        self.counters[reason] += 1
        self.counters["Resync Required"] += 1   # only a later snapshot recovers
        self.book_version += 1
        return INVALIDATED

    def on_snapshot(self, msg, seq=None):
        """load_snapshot: (re)establish state, clear invalid, set seq baseline."""
        self.yes = {p: q for p, q in msg.yes_levels if q > 0}
        self.no = {p: q for p, q in msg.no_levels if q > 0}
        self.covered = True
        self.valid = True
        self.from_snapshot = True
        self.last_seq = seq
        self.book_version += 1
        return APPLIED

    def on_delta(self, msg, seq=None):
        """apply_delta: gap => invalid; refuse while invalid; negative level
        => invalid (NO clamp); zero level erased; otherwise mutate."""
        if not self.valid:          # already invalid: refuse EVERYTHING until a
            self.counters["deltas_refused_while_invalid"] += 1
            return REFUSED          # snapshot resets state AND the seq baseline
        if seq is not None and self.last_seq is not None and seq != self.last_seq + 1:
            return self._invalidate("Sequence Gap")
        book = self.yes if msg.side == "yes" else self.no
        nxt = book.get(msg.price_e4, 0) + msg.delta_e4
        if nxt < 0:                 # the book is ALREADY wrong — never clamp
            return self._invalidate("negative_delta")
        if nxt == 0:
            book.pop(msg.price_e4, None)
        else:
            book[msg.price_e4] = nxt
        if seq is not None:
            self.last_seq = seq
        self.from_snapshot = False
        self.book_version += 1
        return APPLIED

    def on_l1(self, top):
        """Top-of-book for uncovered (L1-only) markets; NEUTRAL once the
        market has full-depth coverage (L1 then feeds V5 cross-checks only)."""
        if self.covered:
            return NEUTRAL
        self.l1 = top
        self.book_version += 1
        return APPLIED

    def state(self):
        """GoldRecord §2.1 book fields as-of now. Invalid/unknown => zeroed."""
        if self.covered:
            if not self.valid:
                return _zero_state(FLAGS["F_BOOK_COVERED"])
            bids = sorted(self.yes.items(), key=lambda kv: -kv[0])
            asks = sorted((10000 - p, q) for p, q in self.no.items())
            bp, bq, brest, bn = _side_arrays(bids)
            ap, aq, arest, an = _side_arrays(asks)
            flags = FLAGS["F_BOOK_VALID"] | FLAGS["F_BOOK_COVERED"]
            if self.from_snapshot:
                flags |= FLAGS["F_FROM_SNAPSHOT"]
            if bids and asks and bids[0][0] >= asks[0][0]:
                flags |= FLAGS["F_CROSSED"]        # flagged, never repaired
            return BookState(flags, bp, bq, ap, aq, brest, arest, bn, an)
        if self.l1 is None:
            return _zero_state(0)                  # never seen data
        t, px, qty = self.l1, list(_Z16), list(_Z16)
        bn = 0 if t.yes_bid_e4 == 0 else 1         # empty-side sentinels
        an = 0 if t.yes_ask_e4 == 10000 else 1
        if bn:
            px[0], qty[0] = t.yes_bid_e4, t.yes_bid_qty_e4
        apx, aqty = list(_Z16), list(_Z16)
        if an:
            apx[0], aqty[0] = t.yes_ask_e4, t.yes_ask_qty_e4
        flags = FLAGS["F_BOOK_VALID"]              # F_BOOK_COVERED stays 0
        if t.is_snapshot:
            flags |= FLAGS["F_FROM_SNAPSHOT"]
        if bn and an and t.yes_bid_e4 >= t.yes_ask_e4:
            flags |= FLAGS["F_CROSSED"]
        return BookState(flags, tuple(px), tuple(qty), tuple(apx), tuple(aqty),
                         0, 0, bn, an)


class GoldBookFSM(object):
    """All markets: dispatches loader Events to per-market FSMs and keeps the
    operator-facing accounting (§3 vocabulary). seq is passed per event by the
    caller (post-W5); None means no seq column exists (pre-W5, file order)."""

    def __init__(self):
        self.books = {}
        self.counters = Counter()
        self.seq_seen = False

    def market(self, market_ticker):
        b = self.books.get(market_ticker)
        if b is None:
            b = self.books[market_ticker] = MarketBookFSM(market_ticker,
                                                          self.counters)
        return b

    def on_event(self, event, seq=None):
        if seq is not None:
            self.seq_seen = True
        k = event.kind
        if k == EVENT_TYPE["HEARTBEAT"]:           # V9: mutates NOTHING —
            self.counters["heartbeats_seen"] += 1  # not even a book entry
            r = NEUTRAL
        elif k == EVENT_TYPE["TRADE"]:             # trades never move books
            r = NEUTRAL
        elif k == EVENT_TYPE["BOOK_SNAPSHOT"]:
            r = self.market(event.market_ticker).on_snapshot(event.payload, seq)
        elif k == EVENT_TYPE["BOOK_DELTA"]:
            r = self.market(event.market_ticker).on_delta(event.payload, seq)
        elif k == EVENT_TYPE["L1_TICKER"]:
            r = self.market(event.market_ticker).on_l1(event.payload)
        else:
            raise GoldFSMError("unknown event kind %r" % (k,))
        self.counters[r] += 1
        return r

    def state(self, market_ticker):
        b = self.books.get(market_ticker)
        return b.state() if b is not None else _zero_state(0)

    def version(self, market_ticker):
        b = self.books.get(market_ticker)
        return b.book_version if b is not None else 0

    def report(self):
        """Operator vocabulary verbatim (§3). Pre-W5 every report states
        seq_unavailable; nothing here fails a build — W2.5 owns verdicts."""
        c = self.counters
        return {
            "seq_mode": "per_market_seq" if self.seq_seen else SEQ_UNAVAILABLE,
            "markets": len(self.books),
            "covered_markets": sum(1 for b in self.books.values() if b.covered),
            "Invalid Book State": sum(1 for b in self.books.values()
                                      if b.covered and not b.valid),
            "Sequence Gap": c["Sequence Gap"],
            "Resync Required": c["Resync Required"],
            "negative_delta_invalidations": c["negative_delta"],
            "deltas_refused_while_invalid": c["deltas_refused_while_invalid"],
            "heartbeats_seen": c["heartbeats_seen"],
            "events_applied": c[APPLIED],
            "events_neutral": c[NEUTRAL],
            "crossed_books_now": sum(
                1 for b in self.books.values()
                if b.state().flags & FLAGS["F_CROSSED"]),
        }
