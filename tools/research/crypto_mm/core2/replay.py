#!/usr/bin/env python3
"""Event-driven replay for quote_core_v2 on recorded KXBTC15M tape.

Honesty rules (the pessimistic-bound doctrine is the go/no-go):
- Maker fills ONLY when (a) a trade prints strictly through our price, or
  (b) prints AT our price exceed the displayed queue that was ahead of us
  when we joined.  Queue-ahead never shrinks on cancels (unobservable).
- Every action suffers LATENCY_S before it exists on the book.
- Taker cuts pay the fee and cross only displayed depth.
- Settlement = strike vs the exchange's own avg_60s_data at the close
  tick (phase 0), exactly the contract's settlement source.

Inputs per market window:
  book events  : warehouse orderbooks_full parquet (snapshot+delta)
  trades       : crypto_mm_shadow tape TRADE lines
  BRTI         : crypto_mm_shadow tape CF_RAW lines
"""
import bisect
import os
import json
import math
import re
from dataclasses import dataclass, field

from quote_core_v2 import Params, MarketState, compute, taker_fee_c

LATENCY_S = 0.25
DECIDE_EVERY_S = 1.0


def close_epoch(ticker):
    """KXBTC15M-26JUL282015-15 -> epoch close (ET tag + 4h = UTC)."""
    m = re.search(r"-26([A-Z]{3})(\d{2})(\d{2})(\d{2})-", ticker)
    mon = {"JUL": 7, "AUG": 8}[m.group(1)]
    day, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
    import datetime
    dt = datetime.datetime(2026, mon, day, hh, mm,
                           tzinfo=datetime.timezone.utc)
    return dt.timestamp() + 4 * 3600.0


def strike_of(ticker, strikes):
    """Strike table row from the odds feed is not in the tape; infer from
    the ticker suffix convention used by this series: the trailing -NN is
    a bucket id, not the strike.  The replayer receives strikes from the
    caller (extracted from the engine receipts) or estimates from BRTI at
    open (the 15M series strikes at the round index at window open)."""
    return strikes.get(ticker)


@dataclass
class SimOrder:
    side: str          # "bid" (buy YES) / "ask_no" (buy NO)
    px_c: float        # in own-side cents (YES cents for bid, NO cents for ask_no)
    qty: float
    ahead: float       # displayed depth ahead at join
    born: float        # sim time the order became live


@dataclass
class Lot:
    px_c: float
    ts: float
    qty: float


class MarketSim:
    def __init__(self, ticker, strike, close_s, params):
        self.t = ticker
        self.strike = strike
        self.close_s = close_s
        self.p = params
        self.yes = {}            # price_e4 -> qty (YES buy side levels)
        self.no = {}             # price_e4 -> qty (NO buy side levels)
        self.orders = {}         # side -> SimOrder
        self.pending = []        # (effective_ts, action, payload)
        self.lots = {"bid": [], "ask_no": []}   # unpaired inventory
        self.cash_c = 0.0        # realized cents (negative = spent)
        self.fees_c = 0.0
        self.fills = 0
        self.locks = 0
        self.locked_c = 0.0
        self.taker_cuts = 0
        self.trades_window = []  # (ts, taker_side, count)
        self.rti = []            # (ts, value)
        self.settle_avg = None
        self.last_decide = 0.0
        self.events = 0
        self.dbg = {"decides": 0, "no_book": 0, "want_bid": 0,
                    "want_ask": 0, "placed": 0, "trades": 0}

    # ---------- book ----------
    def on_book(self, msg_type, side, price_e4, delta_e4, yes_levels,
                no_levels):
        if msg_type == "snapshot":
            self.yes.clear(); self.no.clear()
            for txt, book in ((yes_levels, self.yes), (no_levels, self.no)):
                if not txt:
                    continue
                try:
                    levels = json.loads(txt)
                except Exception:
                    continue
                for px, qty_e4 in levels:
                    q = qty_e4 / 1e4
                    if q > 0:
                        book[int(px)] = q
        elif msg_type == "delta":
            book = self.yes if side == "yes" else self.no
            px = int(price_e4)
            q = book.get(px, 0.0) + float(delta_e4) / 1e4
            if q <= 1e-9:
                book.pop(px, None)
            else:
                book[px] = q

    def best(self):
        yb = max(self.yes) if self.yes else None
        nb = max(self.no) if self.no else None
        return yb, nb

    # ---------- trades ----------
    def on_trade(self, ts, taker_side, yes_price_e4, count):
        self.now = ts
        self.trades_window.append((ts, taker_side, count))
        px_yes_c = yes_price_e4 / 100.0
        # maker fills: taker "yes" lifts YES asks == hits NO-buy side
        # (our ask_no order at no_px fills when taker buys YES at
        #  yes_px >= 100 - no_px); taker "no" hits YES bids.
        od = self.orders.get("bid")
        if od and od.born > ts:
            od = None                       # not on the book yet (latency)
        if od and taker_side == "no":
            if px_yes_c < od.px_c - 1e-9:
                self._fill("bid", od, od.qty)
            elif abs(px_yes_c - od.px_c) <= 1e-9:
                self._queue_fill(od, count, "bid")
        od = self.orders.get("ask_no")
        if od and od.born > ts:
            od = None
        if od and taker_side == "yes":
            no_px_trade = 100.0 - px_yes_c
            if no_px_trade < od.px_c - 1e-9:
                self._fill("ask_no", od, od.qty)
            elif abs(no_px_trade - od.px_c) <= 1e-9:
                self._queue_fill(od, count, "ask_no")

    def _queue_fill(self, od, printed, side):
        if od.ahead > 0:
            take = min(od.ahead, printed)
            od.ahead -= take
            printed -= take
        if printed > 1e-9 and od.ahead <= 1e-9:
            self._fill(side, od, min(od.qty, printed))

    def _fill(self, side, od, qty):
        if qty <= 1e-9:
            return
        self.fills += 1
        self.cash_c -= od.px_c * qty
        self._book_lot(side, od.px_c, qty)
        od.qty -= qty
        if od.qty <= 1e-9:
            self.orders.pop(side, None)

    def _book_lot(self, side, px_c, qty):
        opp = "ask_no" if side == "bid" else "bid"
        rem = qty
        while rem > 1e-9 and self.lots[opp]:
            lot = self.lots[opp][0]
            take = min(rem, lot.qty)
            lock = 100.0 - lot.px_c - px_c
            self.locked_c += lock * take
            self.locks += 1
            # a completed pair pays 100c at settlement: credit now
            self.cash_c += 100.0 * take
            lot.qty -= take
            rem -= take
            if lot.qty <= 1e-9:
                self.lots[opp].pop(0)
        if rem > 1e-9:
            self.lots[side].append(Lot(px_c, self.now, rem))

    # ---------- inventory helpers ----------
    def net_q(self):
        y = sum(l.qty for l in self.lots["bid"])
        n = sum(l.qty for l in self.lots["ask_no"])
        return y - n

    def oldest_age(self):
        ages = [self.now - l.ts
                for s in ("bid", "ask_no") for l in self.lots[s]]
        return max(ages) if ages else 0.0

    # ---------- decision ----------
    def decide(self, now, fair_c, sigma_c):
        self.now = now
        self.dbg["decides"] += 1
        yb, nb = self.best()
        if yb is None or nb is None:
            self.dbg["no_book"] += 1
            return
        yes_bid_c = yb / 100.0
        yes_ask_c = 100.0 - nb / 100.0
        if yes_ask_c - yes_bid_c <= 0:
            self.dbg["no_book"] += 1
            return
        cutoff = now - self.p.flow_win_s
        while self.trades_window and self.trades_window[0][0] < cutoff:
            self.trades_window.pop(0)
        buy = sum(c for t, s, c in self.trades_window if s == "yes")
        sell = sum(c for t, s, c in self.trades_window if s == "no")
        q_now = self.net_q()
        basis = None
        if q_now > 1e-9 and self.lots["bid"]:
            basis = self.lots["bid"][0].px_c
        elif q_now < -1e-9 and self.lots["ask_no"]:
            basis = self.lots["ask_no"][0].px_c
        st = MarketState(
            yes_bid_c=yes_bid_c, yes_ask_c=yes_ask_c,
            bid_depth=self.yes.get(yb, 0.0), ask_depth=self.no.get(nb, 0.0),
            flow_buy=buy, flow_sell=sell,
            fair_c=fair_c, sigma_c=sigma_c,
            q=q_now, tte_s=self.close_s - now,
            unpaired_age_s=self.oldest_age(),
            basis_c=basis,
        )
        qs = compute(st, self.p)
        if qs.bid_sz > 0:
            self.dbg["want_bid"] += 1
        if qs.ask_sz > 0:
            self.dbg["want_ask"] += 1
        if os.environ.get("CORE2_PROBE") and self.dbg["decides"] % 60 == 0:
            print("PROBE", self.t[-7:], "tte", round(st.tte_s),
                  "bk", st.yes_bid_c, st.yes_ask_c,
                  "depth", round(st.bid_depth), round(st.ask_depth),
                  "flow", round(st.flow_buy, 1), round(st.flow_sell, 1),
                  "fair", None if st.fair_c is None else round(st.fair_c, 1),
                  "sc", round(st.sigma_c, 2), "q", st.q, qs.diag)
        # ---- knives first ----
        if qs.take_side:
            self._maybe_cut(qs)
        # ---- maker quotes: cancel/replace toward desired ----
        for side, px, sz in (("bid", qs.bid_px_c, qs.bid_sz),
                             ("ask_no", qs.ask_px_c, qs.ask_sz)):
            cur = self.orders.get(side)
            if sz <= 0 or px is None:
                if cur:
                    self.orders.pop(side, None)
                continue
            if cur and abs(cur.px_c - px) <= 1e-9:
                continue                     # keep queue position
            book = self.yes if side == "bid" else self.no
            lvl = int(round(px * 100))
            ahead = book.get(lvl, 0.0)
            self.orders[side] = SimOrder(side, px, sz, ahead,
                                         now + LATENCY_S)

    def _maybe_cut(self, qs):
        q = self.net_q()
        side_held = "bid" if q > 0 else "ask_no"
        lots = self.lots[side_held]
        if not lots:
            return
        lot = lots[0]
        yb, nb = self.best()
        if yb is None or nb is None:
            return
        proceeds_c = (yb / 100.0) if q > 0 else (nb / 100.0)
        depth = self.yes.get(yb, 0.0) if q > 0 else self.no.get(nb, 0.0)
        fee = taker_fee_c(proceeds_c, self.p)
        net = proceeds_c - lot.px_c - fee
        reason = qs.diag.get("take_reason")
        if reason == "lock_take_if_profitable" and net < self.p.lock_take_min_c:
            return
        ct = min(lot.qty, depth)
        if ct <= 1e-9:
            return
        self.taker_cuts += 1
        self.fees_c += fee * ct
        self.cash_c += proceeds_c * ct - fee * ct
        lot.qty -= ct
        if lot.qty <= 1e-9:
            lots.pop(0)

    # ---------- settlement ----------
    def settle(self, official_result=None):
        if official_result in ("yes", "no"):
            result_yes = official_result == "yes"
        else:
            if self.settle_avg is None and self.rti:
                tail = [v for t, v in self.rti if t >= self.close_s - 60.0]
                self.settle_avg = (sum(tail) / len(tail)) if tail \
                    else self.rti[-1][1]
            result_yes = (self.settle_avg is not None
                          and self.strike is not None
                          and self.settle_avg > self.strike)
        for l in self.lots["bid"]:
            self.cash_c += 100.0 * l.qty if result_yes else 0.0
        for l in self.lots["ask_no"]:
            self.cash_c += 0.0 if result_yes else 100.0 * l.qty
        self.lots = {"bid": [], "ask_no": []}
        return result_yes

    def result(self):
        return dict(ticker=self.t, pnl_c=round(self.cash_c - self.fees_c, 2),
                    fills=self.fills, locks=self.locks,
                    locked_c=round(self.locked_c, 2),
                    taker_cuts=self.taker_cuts,
                    fees_c=round(self.fees_c, 2), dbg=dict(self.dbg))
