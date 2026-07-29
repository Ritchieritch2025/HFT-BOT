#!/usr/bin/env python3
"""Drive quote_core_v2 through recorded KXBTC15M days.

Usage:
  run_replay.py --dates 2026-07-26,2026-07-27 [--params params.json]
                [--out results.json]

Data:
  book   : warehouse orderbooks_full Crypto/BTC parquet (duckdb)
  trades : crypto_mm_shadow tape TRADE lines
  BRTI   : crypto_mm_shadow tape CF_RAW lines (also yields strikes:
           the series settles/strikes on the exchange's own avg_60s at
           the quarter-hour marks)
"""
import argparse
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay import MarketSim, close_epoch, DECIDE_EVERY_S  # noqa: E402
from quote_core_v2 import Params  # noqa: E402

BOOK_GLOB = ("/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/"
             "category=Crypto/subcategory=BTC/date={d}/*.parquet")
TAPE_GLOB = ("/home/ubuntu/hft-bot/work/live/crypto_mm_shadow/"
             "shadow_btc15m_{dt}*.ndjson")


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def load_tape(dates):
    """(rti_ts, rti_val) sorted; anchors {epoch_of_quarter: avg60};
    trades {ticker: [(ts, taker_side, count)]}"""
    rti, anchors, trades = [], {}, {}
    files = []
    for d in dates:
        files += glob.glob(TAPE_GLOB.format(dt=d.replace("-", "")))
    for fn in sorted(set(files)):
        for line in open(fn, errors="ignore"):
            if '"CF_RAW"' in line:
                try:
                    raw = json.loads(json.loads(line)["raw"])
                    msg = raw["msg"]
                    data = json.loads(msg["data"])
                    t_ms = data["time"]
                    v = float(data["value"])
                    rti.append((t_ms / 1000.0, v))
                    if t_ms % 900_000 == 0:
                        a = msg.get("avg_60s_data") or {}
                        if a.get("value") is not None:
                            anchors[t_ms // 1000] = float(a["value"])
                except Exception:
                    continue
            elif '"TRADE"' in line:
                try:
                    d_ = json.loads(line)
                    trades.setdefault(d_["mt"], []).append(
                        (d_["recv_wall_ns"] / 1e9, d_["taker_side"],
                         int(d_["yes_price_e4"]), float(d_["count"])))
                except Exception:
                    continue
    rti.sort()
    return rti, anchors, trades


def book_tickers(dates, con):
    files = [p for d in dates for p in glob.glob(BOOK_GLOB.format(d=d))]
    tickers = set()
    for p in files:
        for (t,) in con.execute(
                "select distinct market_ticker from read_parquet("
                f"'{p}') where market_ticker like 'KXBTC15M%'").fetchall():
            tickers.add(t)
    return sorted(tickers), files


def book_events_for(ticker, files, con):
    rows = []
    for p in files:
        q = ("select recv_wall_ns, msg_type, side, price_e4, delta_e4, "
             "yes_levels, no_levels "
             f"from read_parquet('{p}') where market_ticker = '{ticker}' "
             "order by recv_wall_ns")
        rows += con.execute(q).fetchall()
    rows.sort(key=lambda r: r[0])
    return [(r[0] / 1e9,) + tuple(r[1:]) for r in rows]


class FairModel:
    """Self-contained normal model on raw BRTI ticks."""
    def __init__(self, rti):
        self.ts = [t for t, _ in rti]
        self.vs = [v for _, v in rti]

    def at(self, now, strike, tte_s):
        import bisect
        i = bisect.bisect_right(self.ts, now)
        if i < 40 or strike is None:
            return None, 0.0
        w = self.vs[max(0, i - 300):i]
        diffs = [w[j + 1] - w[j] for j in range(len(w) - 1)]
        m = sum(diffs) / len(diffs)
        var = sum((x - m) ** 2 for x in diffs) / max(1, len(diffs) - 1)
        sig1 = math.sqrt(max(var, 1e-8))
        rti_now = self.vs[i - 1]
        sig_t = max(sig1 * math.sqrt(max(tte_s, 1.0)), 1e-6)
        z = (rti_now - strike) / sig_t
        fair = 100.0 * phi(z)
        dpds = 100.0 * math.exp(-z * z / 2.0) / math.sqrt(2 * math.pi) / sig_t
        sigma_c = min(dpds * sig1 * math.sqrt(30.0), 25.0)
        return fair, sigma_c


STRIKES = "/home/ubuntu/core2/strikes.json"


def run(dates, params, verbose=False, limit=0):
    import duckdb
    con = duckdb.connect()
    rti, anchors, trades = load_tape(dates)
    tickers, files = book_tickers(dates, con)
    if limit:
        tickers = tickers[:limit]
    try:
        strikes = json.load(open(STRIKES))
    except Exception:
        strikes = {}
    fm = FairModel(rti)
    results = []
    curve = []
    equity_c = 0.0
    for ticker in tickers:
        meta = strikes.get(ticker) or {}
        strike = meta.get("strike")
        if meta.get("close_time"):
            import datetime
            close_s = datetime.datetime.fromisoformat(
                meta["close_time"].replace("Z", "+00:00")).timestamp()
        else:
            close_s = close_epoch(ticker)
        open_s = close_s - 900.0
        if strike is None:
            continue
        ev_book = [e for e in book_events_for(ticker, files, con)
                   if e[0] <= close_s]
        ev_tr = [e for e in trades.get(ticker, []) if e[0] <= close_s]
        if not ev_book:
            continue
        sim = MarketSim(ticker, strike, close_s, params)
        sim.rti = [(t, v) for t, v in rti
                   if open_s - 5 <= t <= close_s + 5]
        bi = ti = 0
        next_decide = ev_book[0][0]
        while bi < len(ev_book) or ti < len(ev_tr):
            bt = ev_book[bi][0] if bi < len(ev_book) else float("inf")
            tt = ev_tr[ti][0] if ti < len(ev_tr) else float("inf")
            now = min(bt, tt)
            while next_decide <= now:
                tte = close_s - next_decide
                if tte > 0:
                    fair, sc = fm.at(next_decide, strike, tte)
                    if fair is not None:
                        sim.now = next_decide
                        sim.decide(next_decide, fair, sc)
                next_decide += DECIDE_EVERY_S
            if bt <= tt:
                _, mt_, side, pe4, de4, yl, nl = ev_book[bi]
                sim.on_book(mt_, side, pe4, de4, yl, nl)
                bi += 1
            else:
                ts_, tside, ype4, cnt = ev_tr[ti]
                sim.on_trade(ts_, tside, ype4, cnt)
                ti += 1
        sim.settle(official_result=meta.get("result"))
        r = sim.result()
        equity_c += r["pnl_c"]
        curve.append((close_s, round(equity_c / 100.0, 4)))
        results.append(r)
        if verbose:
            print(r)
    total = sum(r["pnl_c"] for r in results)
    summary = dict(
        markets=len(results),
        pnl_usd=round(total / 100.0, 2),
        fills=sum(r["fills"] for r in results),
        locks=sum(r["locks"] for r in results),
        locked_usd=round(sum(r["locked_c"] for r in results) / 100.0, 2),
        taker_cuts=sum(r["taker_cuts"] for r in results),
        cut_pnl_usd=round(sum(r["cut_pnl_c"] for r in results) / 100.0, 2),
        settle_pnl_usd=round(
            sum(r["settle_pnl_c"] for r in results) / 100.0, 2),
        fees_usd=round(sum(r["fees_c"] for r in results) / 100.0, 2),
        losers=len([r for r in results if r["pnl_c"] < 0]),
        worst=sorted(results, key=lambda r: r["pnl_c"])[:3],
        max_drawdown_usd=max_drawdown(curve),
    )
    return results, curve, summary


def max_drawdown(curve):
    peak, dd = -1e18, 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return round(dd, 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True)
    ap.add_argument("--params", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    p = Params()
    if a.params:
        for k, v in json.load(open(a.params)).items():
            setattr(p, k, v)
    res, curve, summary = run(a.dates.split(","), p, verbose=a.v,
                              limit=a.limit)
    print(json.dumps(summary, indent=1))
    if a.out:
        json.dump(dict(results=res, curve=curve, summary=summary,
                       params=vars(p)), open(a.out, "w"))
