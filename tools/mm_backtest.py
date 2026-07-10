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

DECISION CLOCK (W-TL1 — explicit, never implicit):
  --clock recv (DEFAULT)  decisions keyed off local_recv_ts_us — the time WE
      actually saw each message. The only clock a tradable strategy can be
      validated on. Rows missing local_recv_ts_us are counted and the run
      FAILS CLOSED (exit 1) unless --allow-missing-recv explicitly drops them;
      silently substituting exchange time is forbidden (look-ahead bias).
  --clock exchange        DIAGNOSTIC ONLY: decisions keyed off the exchange
      timestamp — before the message physically reached us. Exists solely to
      DEMONSTRATE look-ahead bias (compare against clock=recv). Its numbers
      must NEVER feed a go/no-go decision (GUARDRAILS Q2).
  Scheduled heartbeat rows (is_snapshot, NULL price, all-NULL ladder) are
  state continuation stamped by OUR hour scheduler: they replay at ts_utc
  under both clocks, are counted separately, and are never "missing recv".

ACTIVE-QUOTE LATENCY MODEL (W-TL1 — minimal, mechanism over precision):
  a quote generated on a book update at t is NOT instantly live:
      decision_ts_us                = t + processing_chain_p99_us + signing_p99_us
      order_effective_not_before_us = decision_ts_us + signed_post_rtt_p99_us
  trades earlier than the quote's effective time cannot fill it. On requote
  the previous quote is dropped immediately (simplification: stale-quote
  fills — favorable AND adverse — are both excluded; documented, minimal).
  The three p99 parameters come from config/backtest_latency.yaml and are
  CONSERVATIVE PLACEHOLDERS until the separate measurement task lands.

Read-only research tool. No network, no orders.

Usage:
  python3 tools/mm_backtest.py --date 2026-07-06 --markets T1,T2
  python3 tools/mm_backtest.py --date 2026-07-06 --from-scan 10   # top-N from mm_scan
  python3 tools/mm_backtest.py --date D --markets T1 --clock exchange  # bias demo
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

LATENCY_CONFIG = os.path.join(wc.ROOT, "config", "backtest_latency.yaml")
LATENCY_KEYS = ("processing_chain_p99_us", "signing_p99_us", "signed_post_rtt_p99_us")
# PLACEHOLDER defaults (mirror config/backtest_latency.yaml — conservative
# guesses, NOT measurements; the measurement campaign is a separate task).
LATENCY_DEFAULTS = {"processing_chain_p99_us": 2000,
                    "signing_p99_us": 5000,
                    "signed_post_rtt_p99_us": 60000}


def load_latency_config(path=LATENCY_CONFIG):
    """Flat `key: value` yaml subset (same dialect as config/warehouse.yaml);
    env var of the UPPER-CASED name overrides the file."""
    cfg = dict(LATENCY_DEFAULTS)
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            s = line.split("#", 1)[0].strip()
            if not s or ":" not in s:
                continue
            k, v = s.split(":", 1)
            if k.strip() in LATENCY_KEYS:
                cfg[k.strip()] = int(v.strip())
    for k in LATENCY_KEYS:
        env = os.environ.get(k.upper())
        if env:
            cfg[k] = int(env)
    return cfg


class Sim:
    def __init__(self, size, max_inv, min_spread_e4, latency_us=0):
        self.size = size
        self.max_inv = max_inv
        self.min_spread = min_spread_e4
        self.latency_us = latency_us       # decision chain + signing + RTT
        self.bid = self.ask = None         # (price_e4, effective_not_before_us)
        self.inv = 0.0                     # signed YES contracts
        self.cash = 0.0                    # dollars
        self.fills = 0

    def requote(self, best_bid, best_ask, now_us):
        """New quotes on a book update seen at now_us (decision clock) become
        effective only after the latency chain; the old quote is dropped."""
        wide = (best_bid and best_ask and best_ask - best_bid >= self.min_spread
                and best_bid > 0 and best_ask < 10000)
        active_us = now_us + self.latency_us
        self.bid = (best_bid, active_us) if (wide and self.inv < self.max_inv) else None
        self.ask = (best_ask, active_us) if (wide and self.inv > -self.max_inv) else None

    def on_trade(self, ts_us, price_e4, taker_side, pessimistic):
        # a trade earlier than the quote's effective time cannot fill it
        if taker_side == "no" and self.bid is not None:      # sellers hit bids
            px, active = self.bid
            if ts_us >= active and (price_e4 < px or
                                    (not pessimistic and price_e4 == px)):
                self._fill(+1, px)
        elif taker_side == "yes" and self.ask is not None:   # buyers lift asks
            px, active = self.ask
            if ts_us >= active and (price_e4 > px or
                                    (not pessimistic and price_e4 == px)):
                self._fill(-1, px)

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


def run_market(l1_rows, tr_rows, size, max_inv, min_spread_e4, latency_us=0):
    """l1_rows: (t_event_us, bid_e4, ask_e4); tr_rows: (t_event_us, px_e4, side).
    t_event is already in the CHOSEN decision clock (see event_times)."""
    events = ([(r[0], "book", r) for r in l1_rows] +
              [(r[0], "trade", r) for r in tr_rows])
    events.sort(key=lambda e: (e[0], e[1] == "trade"))  # book update before trade at same ts
    out = {}
    for mode in ("pessimistic", "optimistic"):
        sim = Sim(size, max_inv, min_spread_e4, latency_us)
        last_mid = None
        for ts, kind, r in events:
            if kind == "book":
                _, bid, ask = r
                bid, ask = int(bid), int(ask)
                if bid and ask and ask > bid:
                    last_mid = (bid + ask) // 2
                sim.requote(bid or None, ask or None, ts)
            else:
                _, px, side = r
                sim.on_trade(ts, int(px), side, mode == "pessimistic")
        out[mode] = {"fills": sim.fills, "inventory": sim.inv,
                     "pnl": round(sim.pnl(last_mid), 2)}
    return out


def event_times(df, clock, is_l1):
    """Assign each row its decision-clock event time. Returns (df with column
    `t_event`, n_heartbeat, n_missing). Heartbeats (L1 only: is_snapshot, NULL
    price, all-NULL ladder) replay at ts_utc — they are OUR scheduler's rows,
    never exchange messages, and never count as missing."""
    import pandas as pd
    hb = None
    if is_l1:
        hb = (df["is_snapshot"].fillna(False).astype(bool)
              & df["price_e4"].isna() & df["exchange_ts_us"].isna()
              & df["local_recv_ts_us"].isna())
    else:
        hb = pd.Series(False, index=df.index)
    if clock == "recv":
        t = df["local_recv_ts_us"].copy()
    else:  # exchange — DIAGNOSTIC ONLY; legacy rows' ts_utc IS exchange time
        t = df["exchange_ts_us"].fillna(df["ts_utc"])
    t = t.where(~hb, df["ts_utc"])         # heartbeats replay at hour start
    missing = t.isna() & ~hb
    df = df.assign(t_event=t)
    return df, int(hb.sum()), int(missing.sum())


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
    ap.add_argument("--clock", choices=("recv", "exchange"), default="recv",
                    help="decision clock. recv (default) = local_recv_ts_us, the "
                         "tradable clock. exchange = DIAGNOSTIC ONLY (look-ahead "
                         "bias demo); never valid for go/no-go (Q2)")
    ap.add_argument("--allow-missing-recv", action="store_true",
                    help="clock=recv only: DROP rows missing local_recv_ts_us "
                         "(counted + printed) instead of failing closed. Never "
                         "falls back to exchange time")
    for k in LATENCY_KEYS:
        ap.add_argument("--%s" % k.replace("_", "-"), type=int, default=None,
                        help="override config/backtest_latency.yaml (PLACEHOLDER "
                             "conservative default until measured)")
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

    lat_cfg = load_latency_config()
    for k in LATENCY_KEYS:
        v = getattr(args, k)
        if v is not None:
            lat_cfg[k] = v
    latency_us = sum(lat_cfg[k] for k in LATENCY_KEYS)

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
              columns=["ts_utc", "market_ticker", "yes_bid_e4", "yes_ask_e4",
                       "is_snapshot", "price_e4", "exchange_ts_us",
                       "local_recv_ts_us"]).df()

    _grades = __import__("warehouse").last_seal_grades()
    _legacy = sorted(d for d, g in _grades.items() if g.get("method") == "legacy_v0")
    if _legacy:
        print("BANNER: range includes legacy_v0-sealed day(s) %s — evidence grade "
              "is archive-self-consistency only; PERMANENTLY ineligible for "
              "go/no-go verdicts (operator ruling 2026-07-11)" % ",".join(_legacy))
    tr = load("trades", **load_kwargs,
              columns=["ts_utc", "market_ticker", "yes_price_e4", "taker_side",
                       "exchange_ts_us", "local_recv_ts_us"]).df()
    tr = tr.dropna(subset=["yes_price_e4"])
    tr["yes_price_e4"] = tr["yes_price_e4"].astype(int)
    l1 = l1.fillna({"yes_bid_e4": 0, "yes_ask_e4": 0}).astype(
        {"yes_bid_e4": int, "yes_ask_e4": int})

    l1, hb_l1, miss_l1 = event_times(l1, args.clock, is_l1=True)
    tr, _, miss_tr = event_times(tr, args.clock, is_l1=False)
    if args.clock == "recv":
        n_l1, n_tr = len(l1), len(tr)
        print("recv-clock coverage: L1 missing %d/%d (%.1f%%), trades missing "
              "%d/%d (%.1f%%); heartbeats (replayed at hour start): %d"
              % (miss_l1, n_l1, 100.0 * miss_l1 / n_l1 if n_l1 else 0.0,
                 miss_tr, n_tr, 100.0 * miss_tr / n_tr if n_tr else 0.0, hb_l1))
        if (miss_l1 or miss_tr) and not args.allow_missing_recv:
            print("FAIL-CLOSED: %d row(s) lack local_recv_ts_us — pre-TL1 data "
                  "cannot be replayed on the recv clock. Re-run with "
                  "--allow-missing-recv to DROP them (never silently swapped "
                  "to exchange time)." % (miss_l1 + miss_tr), file=sys.stderr)
            return 3
        l1 = l1[l1.t_event.notna()]
        tr = tr[tr.t_event.notna()]
    else:
        print("*" * 78)
        print("* clock=exchange is DIAGNOSTIC ONLY: it keys decisions off the")
        print("* exchange timestamp — before messages physically reached us.")
        print("* Use it solely to demonstrate look-ahead bias vs --clock recv.")
        print("* Results are INVALID for go/no-go (GUARDRAILS Q2).")
        print("*" * 78)
    l1 = l1.astype({"t_event": "int64"})
    tr = tr.astype({"t_event": "int64"})

    min_spread_e4 = int(round(args.min_spread * 10000))
    print("NON-GATE: source=%s; capture quality UNASSESSED_PENDING_PIPE_W03; "
          "maker fees/queue/settlement are not validated."
          % ("ARCHIVE-SEALED" if archive_only else "LIVE/MIXED"))
    print("maker backtest %s..%s  size=%g max_inv=%g min_spread=$%.2f" %
          (date, end, args.size, args.max_inv, args.min_spread))
    print("clock=%s | latency chain = %d us (processing %d + signing %d + "
          "signed POST RTT %d; PLACEHOLDERS until measured)"
          % (args.clock, latency_us, lat_cfg["processing_chain_p99_us"],
             lat_cfg["signing_p99_us"], lat_cfg["signed_post_rtt_p99_us"]))
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
            list(zip(l1m.t_event, l1m.yes_bid_e4, l1m.yes_ask_e4)),
            list(zip(trm.t_event, trm.yes_price_e4, trm.taker_side)),
            args.size, args.max_inv, min_spread_e4, latency_us)
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
    if args.clock == "exchange":
        print("REMINDER: clock=exchange numbers are look-ahead-biased by "
              "construction — diagnostic only, never go/no-go.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
