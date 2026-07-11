#!/usr/bin/env python3
"""Conservative market-making backtest (MM_ROADMAP Phase 1).

Simulates a symmetric join-the-touch maker on one or more markets/days using
warehouse L1 + trades. Honest by construction — it reports a PESSIMISTIC and
an OPTIMISTIC fill bound instead of one flattering number:

  strategy  : when spread >= min_spread, quote buy@best_bid and sell@best_ask
              (join the touch, size = --size). Requote on every L1 change.
              Stop quoting a side when |inventory| >= --max-inv on that side.
  fills     : a trade with taker_side=no  lifts resting YES bids;
              a trade with taker_side=yes lifts resting YES asks.
      optimistic  = trade AT my price fills me (front of queue)
      pessimistic = only a trade STRICTLY THROUGH my price fills me
                    (back of queue; guaranteed cleared)
  fees      : NOT MODELED. Maker fees can be series-specific; the current facts
              file is not ratified for a gate. Settlement is also not modeled;
              open inventory is marked to the day's last mid.
  output    : per-market fills / gross spread capture / end inventory /
              marked PnL for both bounds.

Read-only research tool. No network, no orders.

Usage:
  python3 tools/mm_backtest.py --date 2026-07-06 --markets T1,T2
  python3 tools/mm_backtest.py --date 2026-07-06 --from-scan 10   # top-N from mm_scan
"""
import argparse
import csv
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
from warehouse import load  # noqa: E402


class Sim:
    def __init__(self, size, max_inv, min_spread_e4):
        self.size = size
        self.max_inv = max_inv
        self.min_spread = min_spread_e4
        self.bid = self.ask = None          # my quotes (E4) or None
        self.inv = 0.0                      # signed YES contracts
        self.cash = 0.0                     # dollars
        self.fills = 0
        self.captured = 0.0                 # gross spread capture estimate

    def requote(self, best_bid, best_ask):
        wide = (best_bid and best_ask and best_ask - best_bid >= self.min_spread
                and best_bid > 0 and best_ask < 10000)
        self.bid = best_bid if (wide and self.inv < self.max_inv) else None
        self.ask = best_ask if (wide and self.inv > -self.max_inv) else None

    def on_trade(self, price_e4, taker_side, pessimistic):
        if taker_side == "no" and self.bid is not None:      # sellers hit bids
            if price_e4 < self.bid or (not pessimistic and price_e4 == self.bid):
                self._fill(+1, self.bid)
        elif taker_side == "yes" and self.ask is not None:   # buyers lift asks
            if price_e4 > self.ask or (not pessimistic and price_e4 == self.ask):
                self._fill(-1, self.ask)

    def _fill(self, sign, px_e4):
        qty = self.size
        self.inv += sign * qty
        self.cash -= sign * qty * px_e4 / 10000.0
        self.fills += 1
        # enforce the inventory cap between book updates too — several trades
        # can hit a standing quote before the next requote re-evaluates it
        if self.inv >= self.max_inv:
            self.bid = None
        if self.inv <= -self.max_inv:
            self.ask = None

    def pnl(self, last_mid_e4):
        return self.cash + self.inv * (last_mid_e4 or 0) / 10000.0


def run_market(l1_rows, tr_rows, size, max_inv, min_spread_e4):
    events = ([(r[0], "book", r) for r in l1_rows] +
              [(r[0], "trade", r) for r in tr_rows])
    events.sort(key=lambda e: (e[0], e[1] == "trade"))  # book update before trade at same ts
    out = {}
    for mode in ("pessimistic", "optimistic"):
        sim = Sim(size, max_inv, min_spread_e4)
        last_mid = None
        for _ts, kind, r in events:
            if kind == "book":
                _, bid, ask = r
                bid, ask = int(bid), int(ask)
                if bid and ask and ask > bid:
                    last_mid = (bid + ask) // 2
                sim.requote(bid or None, ask or None)
            else:
                _, px, side = r
                sim.on_trade(int(px), side, mode == "pessimistic")
        out[mode] = {"fills": sim.fills, "inventory": sim.inv,
                     "pnl": round(sim.pnl(last_mid), 2)}
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None)
    ap.add_argument("--end", default=None, help="inclusive end date (default = --date)")
    ap.add_argument("--markets", default=None, help="comma-separated tickers")
    ap.add_argument("--from-scan", type=int, default=0,
                    help="take top-N markets from work/mm/candidates_<date>.csv")
    ap.add_argument("--size", type=float, default=5.0, help="contracts per quote")
    ap.add_argument("--max-inv", type=float, default=25.0)
    ap.add_argument("--min-spread", type=float, default=0.02, help="dollars")
    ap.add_argument("--archive-only", action="store_true",
                    help="never attach live staging (automatic for past ranges)")
    args = ap.parse_args(argv[1:])
    date = args.date or (datetime.datetime.now(datetime.timezone.utc).date()
                         - datetime.timedelta(days=1)).isoformat()
    end = args.end or date
    try:
        end_date = datetime.date.fromisoformat(end)
    except ValueError:
        ap.error("--end/--date must be YYYY-MM-DD")
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    if end_date >= today_utc:
        ap.error("research tools NEVER attach live staging (PIPE-R001, operator "
                 "ruling 2026-07-11); requested range reaches %s but only sealed "
                 "past UTC days are readable — pass a completed day" % end_date)
    archive_only = True  # unconditional; live staging is not reachable from here
    load_kwargs = {"start": date, "end": end, "archive_only": archive_only}

    markets = []
    if args.markets:
        markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    elif args.from_scan:
        path = os.path.join(wc.ROOT, "work", "mm", "candidates_%s.csv" % date)
        if not os.path.exists(path):
            print("no scan file %s — run tools/mm_scan.py first" % path, file=sys.stderr)
            return 1
        with open(path) as f:
            markets = [r["market_ticker"] for r in csv.DictReader(f)][:args.from_scan]
    if not markets:
        print("pass --markets or --from-scan N", file=sys.stderr)
        return 2

    l1 = load("orderbooks_l1", **load_kwargs,
              columns=["ts_utc", "market_ticker", "yes_bid_e4", "yes_ask_e4"]).df()

    _grades = __import__("warehouse").last_seal_grades()
    _legacy = sorted(d for d, g in _grades.items() if g.get("method") == "legacy_v0")
    if _legacy:
        print("BANNER: range includes legacy_v0-sealed day(s) %s — evidence grade "
              "is archive-self-consistency only; PERMANENTLY ineligible for "
              "go/no-go verdicts (operator ruling 2026-07-11)" % ",".join(_legacy))
    tr = load("trades", **load_kwargs,
              columns=["ts_utc", "market_ticker", "yes_price_e4", "taker_side"]).df()
    tr = tr.dropna(subset=["yes_price_e4"])
    tr["yes_price_e4"] = tr["yes_price_e4"].astype(int)
    l1 = l1.fillna({"yes_bid_e4": 0, "yes_ask_e4": 0}).astype(
        {"yes_bid_e4": int, "yes_ask_e4": int})

    min_spread_e4 = int(round(args.min_spread * 10000))
    print("NON-GATE: source=%s; capture quality UNASSESSED_PENDING_PIPE_W03; "
          "maker fees/queue/settlement are not validated."
          % ("ARCHIVE-SEALED" if archive_only else "LIVE/MIXED"))
    print("maker backtest %s..%s  size=%g max_inv=%g min_spread=$%.2f" %
          (date, end, args.size, args.max_inv, args.min_spread))
    print("%-44s %5s %8s %6s | %5s %8s %6s" %
          ("market", "fillP", "pnlP", "invP", "fillO", "pnlO", "invO"))
    tot = defaultdict(float)
    for mt in markets:
        l1m = l1[l1.market_ticker == mt]
        trm = tr[tr.market_ticker == mt]
        if l1m.empty:
            print("%-44s (no L1 data)" % mt[:44])
            continue
        res = run_market(
            list(zip(l1m.ts_utc, l1m.yes_bid_e4, l1m.yes_ask_e4)),
            list(zip(trm.ts_utc, trm.yes_price_e4, trm.taker_side)),
            args.size, args.max_inv, min_spread_e4)
        p, o = res["pessimistic"], res["optimistic"]
        print("%-44s %5d %8.2f %6.0f | %5d %8.2f %6.0f" %
              (mt[:44], p["fills"], p["pnl"], p["inventory"],
               o["fills"], o["pnl"], o["inventory"]))
        tot["pnlP"] += p["pnl"]; tot["pnlO"] += o["pnl"]
        tot["fillP"] += p["fills"]; tot["fillO"] += o["fills"]
    print("-" * 96)
    print("TOTAL pessimistic: fills=%d pnl=$%.2f | optimistic: fills=%d pnl=$%.2f" %
          (tot["fillP"], tot["pnlP"], tot["fillO"], tot["pnlO"]))
    print("(strict-through is a diagnostic fill lower bound; this output is NOT "
          "eligible for go/no-go)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
