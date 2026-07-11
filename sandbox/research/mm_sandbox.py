#!/usr/bin/env python3
"""Executable market-making research sandbox.

This is the fast, reversible bridge between the existing Gold replay tape and
the production pricing math.  It is intentionally inside ``sandbox/``:

* no credentials, network, orders, or production writes;
* production code never imports this module;
* integer E4 quantities/prices and an E8 cash ledger;
* deterministic stream order (the Gold stream is never re-sorted);
* submit-pending -> resting -> cancel-pending -> terminal order lifecycle;
* strict-through (binding lower bound), queue and optimistic fill tracks;
* static touch, dynamic fair and dynamic flow-guard policies on one tape.

Current Gold tapes use ``ts_us`` and are useful for machinery diagnostics, but
the July-06 Gold build does not carry a certified receive clock or source WS
sequence.  Therefore Gold economic results are labelled DIAGNOSTIC_ONLY.  The
fresh targeted L2 receive-clock tape is the first input eligible for an edge
claim; the simulator itself is ready for that adapter.

Examples::

  python3 sandbox/research/mm_sandbox.py --demo
  python3 sandbox/research/mm_sandbox.py --gold-root work/gold \
      --date 2026-07-06 \
      --market KXMLBSPREAD-26JUL052130BOSLAA-BOS5 \
      --out sandbox/research/reports/mlb_smoke.json
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import math
import os
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from gold_dtype import EVENT_TYPE, FLAGS, fnv1a64  # noqa: E402
from gold_io import GoldDayReader  # noqa: E402
from pricing import fair, lo, quote as quote_math  # noqa: E402


E4 = 10_000
E8 = 100_000_000
TICK_E4 = 100
MARKOUT_HORIZONS_US = (0, 100_000, 1_000_000, 5_000_000, 30_000_000)


def _dollars(e8: int) -> float:
    return round(e8 / E8, 8)


def _contracts(qty_e4: int) -> float:
    return round(qty_e4 / E4, 4)


@dataclass(frozen=True)
class BookState:
    valid: bool
    bid_px_e4: tuple[int, ...] = ()
    bid_qty_e4: tuple[int, ...] = ()
    ask_px_e4: tuple[int, ...] = ()
    ask_qty_e4: tuple[int, ...] = ()

    @property
    def best_bid(self) -> int | None:
        return self.bid_px_e4[0] if self.valid and self.bid_px_e4 else None

    @property
    def best_ask(self) -> int | None:
        return self.ask_px_e4[0] if self.valid and self.ask_px_e4 else None

    @property
    def midpoint_e4(self) -> int | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) // 2

    def qty_ahead(self, side: str, price_e4: int) -> int:
        px = self.bid_px_e4 if side == "bid" else self.ask_px_e4
        qty = self.bid_qty_e4 if side == "bid" else self.ask_qty_e4
        for p, q in zip(px, qty):
            if p == price_e4:
                return max(0, int(q))
        return 0


@dataclass(frozen=True)
class TapeEvent:
    ts_us: int
    stream_seq: int
    event_type: int
    book: BookState
    trade_price_e4: int = 0
    trade_qty_e4: int = 0
    taker_side: str = ""
    trade_id: str = ""

    @property
    def is_trade(self) -> bool:
        return self.event_type == EVENT_TYPE["TRADE"]


@dataclass(frozen=True)
class DesiredQuote:
    bid_e4: int | None
    ask_e4: int | None
    qty_e4: int
    reason: str


@dataclass
class RestingOrder:
    side: str
    price_e4: int
    remaining_e4: int
    active_at_us: int
    queue_ahead_e4: int = 0
    first_qty_e4: int = 0
    had_fill: bool = False


@dataclass
class SideMachine:
    desired: tuple[int, int] | None = None
    pending: RestingOrder | None = None
    active: RestingOrder | None = None
    cancel_at_us: int | None = None


@dataclass(frozen=True)
class Fill:
    ts_us: int
    stream_seq: int
    side: str
    price_e4: int
    qty_e4: int
    stale: bool
    entry_edge_e4: int | None
    trade_id: str


class FillSimulator:
    """One-order-per-side lifecycle with conservative sequential replace.

    A changed quote first waits for cancel effectiveness, then waits for the
    replacement placement latency.  The old order remains fillable while its
    cancellation is pending.  This deliberately sacrifices queue opportunity
    rather than creating impossible overlapping quotes.
    """

    def __init__(self, *, fill_rule: str, place_latency_us: int,
                 cancel_latency_us: int, maker_fee_e4: int = 0):
        if fill_rule not in ("strict", "queue", "optimistic"):
            raise ValueError("fill_rule must be strict|queue|optimistic")
        self.fill_rule = fill_rule
        self.place_latency_us = int(place_latency_us)
        self.cancel_latency_us = int(cancel_latency_us)
        self.maker_fee_e4 = int(maker_fee_e4)
        self.sides = {"bid": SideMachine(), "ask": SideMachine()}
        self.inventory_e4 = 0
        self.cash_e8 = 0
        self.fees_e8 = 0
        self.max_abs_inventory_e4 = 0
        self.fills: list[Fill] = []
        self.placements = 0
        self.cancels = 0
        self.stale_fills = 0
        self.duplicate_trades = 0
        self._seen_trades: set[str] = set()
        self._book = BookState(False)

    @staticmethod
    def _same(order: RestingOrder | None,
              desired: tuple[int, int] | None) -> bool:
        # Do not replenish a partially-filled order merely because its
        # remaining quantity is below the original desired size.
        return bool(order and desired and order.price_e4 == desired[0])

    def update_book(self, book: BookState) -> None:
        self._book = book

    def _schedule_place(self, side: str, state: SideMachine, at_us: int) -> None:
        if state.desired is None or state.pending is not None or state.active is not None:
            return
        px, qty = state.desired
        state.pending = RestingOrder(side, px, qty,
                                     at_us + self.place_latency_us,
                                     first_qty_e4=qty)
        self.placements += 1

    def set_desired(self, quote: DesiredQuote, now_us: int) -> None:
        self.advance_through(now_us)
        desired_by_side = {
            "bid": (quote.bid_e4, quote.qty_e4) if quote.bid_e4 is not None else None,
            "ask": (quote.ask_e4, quote.qty_e4) if quote.ask_e4 is not None else None,
        }
        for side, desired in desired_by_side.items():
            state = self.sides[side]
            state.desired = desired
            if state.active is not None:
                if not self._same(state.active, desired) and state.cancel_at_us is None:
                    state.cancel_at_us = now_us + self.cancel_latency_us
                    self.cancels += 1
            elif state.pending is None:
                self._schedule_place(side, state, now_us)
            # If a different placement is already in flight, let it activate
            # and then cancel it.  Pretending an already-sent create vanished
            # would hide stale-order exposure.
        # Zero-latency is a useful deterministic test/configuration and should
        # mean effective now, not "effective on the next unrelated event".
        self.advance_through(now_us)

    def _next_action(self, state: SideMachine) -> tuple[int, str] | None:
        candidates = []
        if state.pending is not None:
            candidates.append((state.pending.active_at_us, "activate"))
        if state.active is not None and state.cancel_at_us is not None:
            candidates.append((state.cancel_at_us, "cancel"))
        return min(candidates) if candidates else None

    def _advance(self, until_us: int, inclusive: bool) -> None:
        for side, state in self.sides.items():
            while True:
                nxt = self._next_action(state)
                if nxt is None:
                    break
                at_us, action = nxt
                if at_us > until_us or (at_us == until_us and not inclusive):
                    break
                if action == "activate":
                    order = state.pending
                    state.pending = None
                    state.active = order
                    order.queue_ahead_e4 = self._book.qty_ahead(side, order.price_e4)
                    if not self._same(order, state.desired):
                        state.cancel_at_us = at_us + self.cancel_latency_us
                        self.cancels += 1
                else:
                    state.active = None
                    state.cancel_at_us = None
                    self._schedule_place(side, state, at_us)

    def advance_before(self, ts_us: int) -> None:
        self._advance(ts_us, inclusive=False)

    def advance_through(self, ts_us: int) -> None:
        self._advance(ts_us, inclusive=True)

    def _eligible_qty(self, order: RestingOrder, event: TapeEvent) -> int:
        px = event.trade_price_e4
        at_price = px == order.price_e4
        through = (px < order.price_e4 if order.side == "bid"
                   else px > order.price_e4)
        if through:
            # A print through our price implies every better-priced order was
            # cleared, so the full remaining order is the certified lower-bound
            # fill even when the through-print itself is small.
            return order.remaining_e4
        if not at_price or self.fill_rule == "strict":
            return 0
        available = max(0, event.trade_qty_e4)
        if self.fill_rule == "queue":
            consumed = min(order.queue_ahead_e4, available)
            order.queue_ahead_e4 -= consumed
            available -= consumed
        return min(order.remaining_e4, available)

    def on_trade(self, event: TapeEvent) -> list[Fill]:
        if not event.is_trade:
            return []
        trade_key = event.trade_id or "seq:%d" % event.stream_seq
        if trade_key in self._seen_trades:
            self.duplicate_trades += 1
            return []
        self._seen_trades.add(trade_key)
        side = "ask" if event.taker_side == "yes" else \
               "bid" if event.taker_side == "no" else None
        if side is None:
            return []
        state = self.sides[side]
        order = state.active
        if order is None:
            return []
        qty = self._eligible_qty(order, event)
        if qty <= 0:
            return []
        stale = not self._same(order, state.desired) or state.cancel_at_us is not None
        midpoint = event.book.midpoint_e4
        entry_edge = None
        if midpoint is not None:
            entry_edge = (midpoint - order.price_e4 if side == "bid"
                          else order.price_e4 - midpoint)
        fill = Fill(event.ts_us, event.stream_seq,
                    "buy" if side == "bid" else "sell",
                    order.price_e4, qty, stale, entry_edge, trade_key)
        self.fills.append(fill)
        if stale:
            self.stale_fills += 1
        sign = 1 if side == "bid" else -1
        self.inventory_e4 += sign * qty
        self.cash_e8 -= sign * order.price_e4 * qty
        self.fees_e8 += self.maker_fee_e4 * qty
        self.max_abs_inventory_e4 = max(self.max_abs_inventory_e4,
                                        abs(self.inventory_e4))
        order.remaining_e4 -= qty
        order.had_fill = True
        if order.remaining_e4 <= 0:
            state.active = None
            state.cancel_at_us = None
        return [fill]

    def wealth_e8(self, mark_e4: int | None) -> int:
        return self.cash_e8 + self.inventory_e4 * int(mark_e4 or 0) - self.fees_e8


class BasePolicy:
    name = "base"

    def observe_book(self, event: TapeEvent) -> None:
        pass

    def observe_trade(self, event: TapeEvent) -> None:
        pass

    def desired(self, event: TapeEvent, inventory_e4: int) -> DesiredQuote:
        raise NotImplementedError


class StaticTouchPolicy(BasePolicy):
    name = "static_touch"

    def __init__(self, *, qty_e4: int, min_spread_e4: int, max_inv_e4: int):
        self.qty_e4 = qty_e4
        self.min_spread_e4 = min_spread_e4
        self.max_inv_e4 = max_inv_e4

    def desired(self, event: TapeEvent, inventory_e4: int) -> DesiredQuote:
        b, a = event.book.best_bid, event.book.best_ask
        if b is None or a is None or a - b < self.min_spread_e4:
            return DesiredQuote(None, None, self.qty_e4, "book_not_eligible")
        bid = b if inventory_e4 < self.max_inv_e4 else None
        ask = a if inventory_e4 > -self.max_inv_e4 else None
        return DesiredQuote(bid, ask, self.qty_e4, "join_touch")


class DynamicPolicy(BasePolicy):
    def __init__(self, *, name: str, qty_e4: int, min_spread_e4: int,
                 max_inv_e4: int, flow_window_us: int,
                 flow_guard_threshold: float | None,
                 flow_guard_min_trades: int = 3,
                 drift_coeff: float = fair.DRIFT_COEFF_LO,
                 drift_cap: float = fair.DRIFT_CAP_LO,
                 delta_base: float = quote_math.DELTA_BASE_LO,
                 tox_width_lo: float = 0.0,
                 horizon_s: float = 3600.0,
                 close_ts_us: int | None = None,
                 enable_breaker: bool = False):
        self.name = name
        self.qty_e4 = qty_e4
        self.min_spread_e4 = min_spread_e4
        self.max_inv_e4 = max_inv_e4
        self.flow_window_us = flow_window_us
        self.flow_guard_threshold = flow_guard_threshold
        self.flow_guard_min_trades = flow_guard_min_trades
        self.drift_coeff = drift_coeff
        self.drift_cap = drift_cap
        self.delta_base = delta_base
        self.tox_width_lo = tox_width_lo
        self.horizon_s = horizon_s
        self.close_ts_us = close_ts_us
        self.enable_breaker = enable_breaker
        self._flow: collections.deque[tuple[int, int]] = collections.deque()
        self._books: collections.deque[tuple[int, float]] = collections.deque()

    def _prune(self, now_us: int) -> None:
        cutoff = now_us - self.flow_window_us
        while self._flow and self._flow[0][0] < cutoff:
            self._flow.popleft()
        book_cutoff = now_us - 5_000_000
        while self._books and self._books[0][0] < book_cutoff:
            self._books.popleft()

    def observe_book(self, event: TapeEvent) -> None:
        mid = event.book.midpoint_e4
        if mid is not None:
            self._books.append((event.ts_us, lo.lo_of_e4(mid)))
        self._prune(event.ts_us)

    def observe_trade(self, event: TapeEvent) -> None:
        sign = 1 if event.taker_side == "yes" else -1 if event.taker_side == "no" else 0
        if sign and event.trade_qty_e4 > 0:
            self._flow.append((event.ts_us, sign * event.trade_qty_e4))
        self._prune(event.ts_us)

    def flow_imbalance(self, now_us: int) -> float:
        self._prune(now_us)
        gross = sum(abs(q) for _, q in self._flow)
        return (sum(q for _, q in self._flow) / gross) if gross else 0.0

    def _vol_lo(self) -> float:
        vals = [x for _, x in self._books]
        if len(vals) < 3:
            return 0.0
        diffs = [b - a for a, b in zip(vals, vals[1:])]
        return statistics.pstdev(diffs) if len(diffs) > 1 else abs(diffs[0])

    def _breaker(self) -> bool:
        if not self.enable_breaker or len(self._books) < 2:
            return False
        t0, p0 = self._books[0]
        t1, p1 = self._books[-1]
        dt_s = (t1 - t0) / 1_000_000
        return quote_math.jump_breaker(p0, p1, dt_s,
                                       book_updates=len(self._books),
                                       window_s=max(dt_s, 1e-6))

    def desired(self, event: TapeEvent, inventory_e4: int) -> DesiredQuote:
        book = event.book
        b, a = book.best_bid, book.best_ask
        if b is None or a is None or a - b < self.min_spread_e4:
            return DesiredQuote(None, None, self.qty_e4, "book_not_eligible")
        bq, aq = book.bid_qty_e4[0], book.ask_qty_e4[0]
        flow = self.flow_imbalance(event.ts_us)
        fair_lo = fair.fair_lo(b, a, bq, aq, taker_imbalance=flow,
                                drift_coeff=self.drift_coeff,
                                drift_cap=self.drift_cap)
        if fair_lo is None:
            return DesiredQuote(None, None, self.qty_e4, "fair_unavailable")
        if self.close_ts_us is None:
            t_remaining_s = self.horizon_s  # no fake close-time inference
        else:
            t_remaining_s = max(0.0, (self.close_ts_us - event.ts_us) / 1_000_000)
        out = quote_math.quote(
            fair_lo, inventory_e4 / E4, t_remaining_s, self.horizon_s,
            self.max_inv_e4 / E4, vol_lo=self._vol_lo(),
            tox_lo=abs(flow) * self.tox_width_lo,
            breaker_tripped=self._breaker(), delta_base=self.delta_base)
        if not out["quoting"]:
            return DesiredQuote(None, None, self.qty_e4, out["reason"])
        bid, ask = out["bid_e4"], out["ask_e4"]
        # Post-only clamp.  Improving inside the spread is allowed; crossing
        # the opposite touch is not.
        if bid is not None:
            bid = min(bid, a - TICK_E4)
        if ask is not None:
            ask = max(ask, b + TICK_E4)
        if (self.flow_guard_threshold is not None
                and len(self._flow) >= self.flow_guard_min_trades
                and abs(flow) >= self.flow_guard_threshold):
            if flow > 0:
                ask = None  # buy pressure: do not sell into the jump
                reason = "buy_flow_guard"
            else:
                bid = None  # sell pressure: do not buy into the drop
                reason = "sell_flow_guard"
        else:
            reason = out["reason"]
        return DesiredQuote(bid, ask, self.qty_e4, reason)


@dataclass
class RunResult:
    policy: str
    fill_rule: str
    fills: int
    contracts: float
    gross_pnl_dollars: float
    fees_dollars: float
    net_pnl_dollars: float
    end_inventory: float
    max_abs_inventory: float
    stale_fills: int
    placements: int
    cancels: int
    duplicate_trades_ignored: int
    quote_decisions: int
    markouts_cents: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    fill_ledger: list[dict] = field(default_factory=list)


def _markouts(fills: list[Fill], books: list[tuple[int, BookState]]) -> dict:
    times = [t for t, _ in books]
    out = {}
    for horizon in MARKOUT_HORIZONS_US:
        mid_num = exe_num = qty_den = 0
        n = 0
        for fill in fills:
            i = bisect.bisect_left(times, fill.ts_us + horizon)
            if i >= len(books):
                continue
            book = books[i][1]
            mid = book.midpoint_e4
            if mid is None or book.best_bid is None or book.best_ask is None:
                continue
            sign = 1 if fill.side == "buy" else -1
            mid_edge = sign * (mid - fill.price_e4)
            executable = (book.best_bid - fill.price_e4 if sign > 0
                          else fill.price_e4 - book.best_ask)
            mid_num += mid_edge * fill.qty_e4
            exe_num += executable * fill.qty_e4
            qty_den += fill.qty_e4
            n += 1
        label = "%dms" % (horizon // 1000)
        out[label] = {
            "n_fills": n,
            "mid": round(mid_num / qty_den / 100, 6) if qty_den else None,
            "executable": round(exe_num / qty_den / 100, 6) if qty_den else None,
        }
    return out


def run_tape(events: Iterable[TapeEvent], policy: BasePolicy, *, fill_rule: str,
             place_latency_us: int, cancel_latency_us: int,
             maker_fee_e4: int = 0) -> RunResult:
    events = list(events)
    if not events:
        raise ValueError("empty tape")
    prev = None
    for e in events:
        key = (e.ts_us, e.stream_seq)
        if prev is not None and key <= prev:
            raise ValueError("tape must already be strictly ordered by (ts_us, stream_seq)")
        prev = key
    sim = FillSimulator(fill_rule=fill_rule,
                        place_latency_us=place_latency_us,
                        cancel_latency_us=cancel_latency_us,
                        maker_fee_e4=maker_fee_e4)
    current_book = BookState(False)
    books: list[tuple[int, BookState]] = []
    last_valid_mid = None
    decisions = 0
    for event in events:
        # Scheduled actions exactly at an external-event timestamp take effect
        # AFTER that event: conservative, no fill on a just-effective quote.
        sim.advance_before(event.ts_us)
        current_book = event.book
        sim.update_book(current_book)
        if current_book.midpoint_e4 is not None:
            # A Gold TRADE record carries the untouched pre-trade book.  Keep
            # that causal state too; markout(0) must not jump ahead to a
            # same-microsecond book mutation that follows the trade.
            books.append((event.ts_us, current_book))
            last_valid_mid = current_book.midpoint_e4
        if event.is_trade:
            sim.on_trade(event)
            policy.observe_trade(event)
        else:
            policy.observe_book(event)
        # A trade/fill is itself a decision trigger; use the latest causal book
        # state carried by the record, then apply placement latency.
        desired = policy.desired(event, sim.inventory_e4)
        decisions += 1
        sim.set_desired(desired, event.ts_us)
        sim.advance_through(event.ts_us)
    last_mid = last_valid_mid
    gross_e8 = sim.cash_e8 + sim.inventory_e4 * int(last_mid or 0)
    net_e8 = sim.wealth_e8(last_mid)
    return RunResult(
        policy=policy.name,
        fill_rule=fill_rule,
        fills=len(sim.fills),
        contracts=_contracts(sum(f.qty_e4 for f in sim.fills)),
        gross_pnl_dollars=_dollars(gross_e8),
        fees_dollars=_dollars(sim.fees_e8),
        net_pnl_dollars=_dollars(net_e8),
        end_inventory=_contracts(sim.inventory_e4),
        max_abs_inventory=_contracts(sim.max_abs_inventory_e4),
        stale_fills=sim.stale_fills,
        placements=sim.placements,
        cancels=sim.cancels,
        duplicate_trades_ignored=sim.duplicate_trades,
        quote_decisions=decisions,
        markouts_cents=_markouts(sim.fills, books),
        fill_ledger=[{
            **asdict(f),
            "qty_contracts": _contracts(f.qty_e4),
            "price_dollars": f.price_e4 / E4,
        } for f in sim.fills],
    )


def _book(bid: int, ask: int, bid_qty: int = 100_000,
          ask_qty: int = 100_000, valid: bool = True) -> BookState:
    if not valid:
        return BookState(False)
    return BookState(True, (bid,), (bid_qty,), (ask,), (ask_qty,))


def demo_tape() -> list[TapeEvent]:
    """A buy-pressure tape where the static ask is picked off before a jump."""
    book_event = EVENT_TYPE["BOOK_DELTA"]
    trade_event = EVENT_TYPE["TRADE"]
    one_second = 1_000_000
    one_contract = 10_000
    return [
        # Two causal prints arrive during a book-unavailable warmup.  They
        # create the flow signal but cannot generate or fill a quote.
        TapeEvent(50_000, 0, trade_event, BookState(False),
                  5000, one_contract, "yes", "flow-1"),
        TapeEvent(100_000, 1, trade_event, BookState(False),
                  5000, one_contract, "yes", "flow-2"),
        # First valid book: static joins both sides; dynamic guard sees the
        # already-observed pressure and suppresses the toxic ask.
        TapeEvent(150_000, 2, book_event, _book(4000, 5000)),
        # Toxic print strictly through the old/static ask.
        TapeEvent(300_000, 3, trade_event, _book(4000, 5000),
                  5400, one_contract, "yes", "toxic"),
        TapeEvent(500_000, 4, book_event, _book(5800, 6400)),
        TapeEvent(one_second, 5, book_event, _book(6000, 6600)),
    ]


def _state_from_gold(rec) -> BookState:
    flags = int(rec["flags"])
    valid = bool(flags & FLAGS["F_BOOK_VALID"]) and not bool(flags & FLAGS["F_CROSSED"])
    if not valid:
        return BookState(False)
    bn, an = int(rec["bid_nlevels"]), int(rec["ask_nlevels"])
    bp = tuple(int(x) for x in rec["bid_px_e4"][:min(16, bn)] if int(x) > 0)
    bq = tuple(int(x) for x in rec["bid_qty_e4"][:len(bp)])
    ap = tuple(int(x) for x in rec["ask_px_e4"][:min(16, an)] if int(x) > 0)
    aq = tuple(int(x) for x in rec["ask_qty_e4"][:len(ap)])
    return BookState(bool(bp and ap), bp, bq, ap, aq)


def load_gold_tape(root: str, date: str, market: str,
                   allow_unsafe: bool = False) -> tuple[list[TapeEvent], dict]:
    reader = GoldDayReader(root, date)
    mid = reader.market_id(market)
    if mid is None:
        raise ValueError("market not present in Gold day: %s" % market)
    unsafe = reader.manifest.get("safety_verdicts", {}).get(
        "unsafe_for_microstructure", [])
    unsafe_keys = {x.get("key") for x in unsafe if isinstance(x, dict)}
    if market in unsafe_keys and not allow_unsafe:
        raise ValueError("Gold manifest marks market unsafe_for_microstructure; "
                         "pass --allow-unsafe-gold for diagnostics only")
    events = []
    rows = reader.records[reader.records["market_id"] == mid]
    for rec in rows:
        kind = int(rec["event_type"])
        side = ("yes" if int(rec["taker_side"]) == 1
                else "no" if int(rec["taker_side"]) == 2 else "")
        trade_id = ""
        if kind == EVENT_TYPE["TRADE"]:
            trade_id = reader.trade_uuid(int(rec["stream_seq"])) or \
                       "hash:%d" % int(rec["trade_id_hash"])
            if int(rec["trade_id_hash"]) != fnv1a64(trade_id):
                raise ValueError("trade sidecar/hash mismatch at stream_seq %d"
                                 % int(rec["stream_seq"]))
        events.append(TapeEvent(
            int(rec["ts_us"]), int(rec["stream_seq"]), kind,
            _state_from_gold(rec), int(rec["trade_yes_price_e4"]),
            int(rec["trade_qty_e4"]), side, trade_id))
    meta = {
        "source": "gold",
        "date": date,
        "market": market,
        "records": len(events),
        "clock_status": "DIAGNOSTIC_ONLY_NOT_RECEIVE_CLOCK_CERTIFIED",
        "sequence_status": "DIAGNOSTIC_ONLY_SOURCE_WS_SEQ_NOT_CERTIFIED",
    }
    return events, meta


def policy_factories(args):
    common = dict(qty_e4=int(round(args.size * E4)),
                  min_spread_e4=int(round(args.min_spread_cents * 100)),
                  max_inv_e4=int(round(args.max_inventory * E4)))
    return {
        "static_touch": lambda: StaticTouchPolicy(**common),
        "dynamic_fair": lambda: DynamicPolicy(
            name="dynamic_fair", **common,
            flow_window_us=int(args.flow_window_ms * 1000),
            flow_guard_threshold=None, drift_coeff=args.drift_coeff,
            delta_base=args.delta_base, tox_width_lo=args.tox_width_lo,
            enable_breaker=args.enable_breaker),
        "dynamic_guard": lambda: DynamicPolicy(
            name="dynamic_guard", **common,
            flow_window_us=int(args.flow_window_ms * 1000),
            flow_guard_threshold=args.flow_guard_threshold,
            flow_guard_min_trades=args.flow_guard_min_trades,
            drift_coeff=args.drift_coeff, delta_base=args.delta_base,
            tox_width_lo=args.tox_width_lo,
            enable_breaker=args.enable_breaker),
    }


def _safe_out(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    root = (ROOT / "sandbox" / "research" / "reports").resolve()
    if p != root and root not in p.parents:
        raise ValueError("--out must stay under sandbox/research/reports/")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demo", action="store_true")
    src.add_argument("--date")
    ap.add_argument("--gold-root", default="work/gold")
    ap.add_argument("--market")
    ap.add_argument("--allow-unsafe-gold", action="store_true")
    ap.add_argument("--policies", default="static_touch,dynamic_fair,dynamic_guard")
    ap.add_argument("--fill-rules", default="strict,queue,optimistic")
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--max-inventory", type=float, default=10.0)
    ap.add_argument("--min-spread-cents", type=float, default=2.0)
    ap.add_argument("--place-latency-us", type=int, default=67_000)
    ap.add_argument("--cancel-latency-us", type=int, default=67_000)
    ap.add_argument("--maker-fee-cents", type=float, default=0.0,
                    help="explicit maker fee per filled contract; exploratory input")
    ap.add_argument("--flow-window-ms", type=float, default=2000.0)
    ap.add_argument("--flow-guard-threshold", type=float, default=0.8)
    ap.add_argument("--flow-guard-min-trades", type=int, default=3)
    ap.add_argument("--drift-coeff", type=float, default=fair.DRIFT_COEFF_LO)
    ap.add_argument("--delta-base", type=float, default=quote_math.DELTA_BASE_LO)
    ap.add_argument("--tox-width-lo", type=float, default=0.0)
    ap.add_argument("--enable-breaker", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    if args.demo:
        events = demo_tape()
        meta = {"source": "synthetic_demo", "records": len(events),
                "clock_status": "SYNTHETIC_CAUSAL_CLOCK",
                "sequence_status": "SYNTHETIC_DENSE_SEQUENCE"}
        # The two-print demo is deliberately small enough to trip the guard.
        if args.flow_guard_min_trades == 3:
            args.flow_guard_min_trades = 2
    else:
        if not args.market:
            ap.error("--date requires --market")
        events, meta = load_gold_tape(args.gold_root, args.date, args.market,
                                     args.allow_unsafe_gold)

    factories = policy_factories(args)
    wanted_policies = [x.strip() for x in args.policies.split(",") if x.strip()]
    wanted_rules = [x.strip() for x in args.fill_rules.split(",") if x.strip()]
    unknown = [x for x in wanted_policies if x not in factories]
    if unknown:
        ap.error("unknown policies: %s" % ",".join(unknown))
    results = []
    maker_fee_e4 = int(round(args.maker_fee_cents * 100))
    for name in wanted_policies:
        for rule in wanted_rules:
            results.append(run_tape(
                events, factories[name](), fill_rule=rule,
                place_latency_us=args.place_latency_us,
                cancel_latency_us=args.cancel_latency_us,
                maker_fee_e4=maker_fee_e4))
    payload = {
        "schema_version": 1,
        "status": "EXPLORATORY_NOT_GO_NO_GO",
        "source": meta,
        "assumptions": {
            "place_latency_us": args.place_latency_us,
            "cancel_latency_us": args.cancel_latency_us,
            "maker_fee_cents_per_contract": args.maker_fee_cents,
            "strict_through_is_binding_lower_bound": True,
            "gold_clock_and_sequence_are_not_edge_certified": meta["source"] == "gold",
        },
        "results": [asdict(r) for r in results],
    }
    print("MM SANDBOX — %s | records=%d" % (payload["status"], len(events)))
    print("%-16s %-10s %5s %9s %10s %8s %8s %7s" %
          ("policy", "fill", "fills", "contracts", "net_pnl", "end_inv",
           "stale", "orders"))
    for r in results:
        print("%-16s %-10s %5d %9.2f %10.4f %8.2f %8d %7d" %
              (r.policy, r.fill_rule, r.fills, r.contracts,
               r.net_pnl_dollars, r.end_inventory, r.stale_fills, r.placements))
    if args.out:
        out = _safe_out(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print("wrote %s" % out)
    print("Economic results remain exploratory until receive-clock + sequence-valid L2 input.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
