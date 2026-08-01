#!/usr/bin/env python3
"""brti_replicator.py — synthetic BRTI from constituent-exchange public feeds.

Implements the CME CF Real Time Index methodology (see
docs/research_reports/BRTI_REPLICATION_SOURCES_20260727.md for sources and
open parameter questions). Subscribes to free public L2 feeds of BRTI
constituent exchanges, maintains books, and emits a synthetic index once per
second as NDJSON. Research tool: read-only market data, no keys, no orders.

Usage (EC2):  pip install websockets
    python3 brti_replicator.py --out /home/ubuntu/research_fast_anchor/
Env:
    BRTI_LAMBDA      float, default 10.3
    BRTI_LAMBDA_MODE fixed | scaled   (scaled: lam = BRTI_LAMBDA / v_T)
    BRTI_SPREAD_D    float, default 0.005
Known v0 gaps (quantified by the validator, not hand-waved):
    - LMAX / Bullish / Crypto.com / itBit not yet included (no free feed or
      adapter stub pending) -> persistent residual vs official BRTI.
    - Bitstamp feed is top-100 levels only.
"""
import argparse
import asyncio
import json
import math
import os
import statistics
import time

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets --break-system-packages")

SPREAD_D = float(os.environ.get("BRTI_SPREAD_D", "0.005"))
LAMBDA0 = float(os.environ.get("BRTI_LAMBDA", "10.3"))
LAMBDA_MODE = os.environ.get("BRTI_LAMBDA_MODE", "fixed")
STALE_S = 30.0          # methodology: books older than 30s are excluded
DEV_MAX = 0.25          # methodology: mid deviating >25% from median excluded

BOOKS = {}              # name -> {"bids": {p:sz}, "asks": {p:sz}, "ts": float}


def book(name):
    return BOOKS.setdefault(name, {"bids": {}, "asks": {}, "ts": 0.0})


# ---------------------------------------------------------------- adapters
async def coinbase():
    uri = "wss://ws-feed.exchange.coinbase.com"
    sub = {"type": "subscribe", "product_ids": ["BTC-USD"],
           "channels": ["level2_batch"]}
    b = book("coinbase")
    async for ws in websockets.connect(uri, ping_interval=20, max_size=2**24):
        try:
            await ws.send(json.dumps(sub))
            async for raw in ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "snapshot":
                    b["bids"] = {float(p): float(s) for p, s in m["bids"]}
                    b["asks"] = {float(p): float(s) for p, s in m["asks"]}
                elif t == "l2update":
                    for side, p, s in m["changes"]:
                        d = b["bids"] if side == "buy" else b["asks"]
                        p, s = float(p), float(s)
                        if s == 0:
                            d.pop(p, None)
                        else:
                            d[p] = s
                else:
                    continue
                b["ts"] = time.time()
        except Exception:
            b["bids"], b["asks"] = {}, {}
            await asyncio.sleep(2)


async def kraken():
    uri = "wss://ws.kraken.com"
    sub = {"event": "subscribe", "pair": ["XBT/USD"],
           "subscription": {"name": "book", "depth": 500}}
    b = book("kraken")
    async for ws in websockets.connect(uri, ping_interval=20, max_size=2**24):
        try:
            await ws.send(json.dumps(sub))
            async for raw in ws:
                m = json.loads(raw)
                if not isinstance(m, list) or len(m) < 4:
                    continue
                payload = m[1] if isinstance(m[1], dict) else {}
                for key, side in (("bs", "bids"), ("as", "asks"),
                                  ("b", "bids"), ("a", "asks")):
                    for lvl in payload.get(key, []):
                        p, s = float(lvl[0]), float(lvl[1])
                        if s == 0:
                            b[side].pop(p, None)
                        else:
                            b[side][p] = s
                b["ts"] = time.time()
        except Exception:
            b["bids"], b["asks"] = {}, {}
            await asyncio.sleep(2)


async def bitstamp():
    uri = "wss://ws.bitstamp.net"
    sub = {"event": "bts:subscribe",
           "data": {"channel": "order_book_btcusd"}}   # top-100 snapshots
    b = book("bitstamp")
    async for ws in websockets.connect(uri, ping_interval=20, max_size=2**24):
        try:
            await ws.send(json.dumps(sub))
            async for raw in ws:
                m = json.loads(raw)
                d = m.get("data") or {}
                if "bids" in d:
                    b["bids"] = {float(p): float(s) for p, s in d["bids"]}
                    b["asks"] = {float(p): float(s) for p, s in d["asks"]}
                    b["ts"] = time.time()
        except Exception:
            b["bids"], b["asks"] = {}, {}
            await asyncio.sleep(2)


async def gemini():
    uri = "wss://api.gemini.com/v2/marketdata"
    sub = {"type": "subscribe",
           "subscriptions": [{"name": "l2", "symbols": ["BTCUSD"]}]}
    b = book("gemini")
    async for ws in websockets.connect(uri, ping_interval=20, max_size=2**24):
        try:
            await ws.send(json.dumps(sub))
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") != "l2_updates":
                    continue
                for side, p, s in m.get("changes", []):
                    d = b["bids"] if side == "buy" else b["asks"]
                    p, s = float(p), float(s)
                    if s == 0:
                        d.pop(p, None)
                    else:
                        d[p] = s
                b["ts"] = time.time()
        except Exception:
            b["bids"], b["asks"] = {}, {}
            await asyncio.sleep(2)


ADAPTERS = [coinbase, kraken, bitstamp, gemini]
# v0.1 stubs: cryptocom, paxos_itbit, bullish  (see sources doc)


# ---------------------------------------------------------- index formula
def dynamic_cap(bids, asks):
    """C_T = trimmed mean + 5 * winsorized sigma of order sizes near best.
    Best-effort reading of the current methodology (<=50 per side, 1% trim);
    validator sensitivity-checks this block."""
    smp = ([s for _, s in bids[:50]] + [s for _, s in asks[:50]])
    if len(smp) < 10:
        return float("inf")
    smp = sorted(smp)
    k = max(1, int(round(0.01 * len(smp))))
    core = smp[k:-k] if len(smp) > 2 * k else smp
    mean = statistics.fmean(core)
    lo, hi = core[0], core[-1]
    wins = [min(max(x, lo), hi) for x in smp]
    sd = statistics.pstdev(wins)
    return mean + 5.0 * sd


def synthetic_brti(now):
    active, mids, excluded = [], {}, {}
    for name, b in BOOKS.items():
        if now - b["ts"] > STALE_S or not b["bids"] or not b["asks"]:
            excluded[name] = "stale_or_empty"
            continue
        mid = (max(b["bids"]) + min(b["asks"])) / 2.0
        mids[name] = mid
        active.append(name)
    if not active:
        return None
    med = statistics.median(mids.values())
    for name in list(active):
        if abs(mids[name] - med) / med > DEV_MAX:
            excluded[name] = "deviation"
            active.remove(name)
    if not active:
        return None

    bids, asks = [], []
    for name in active:
        bids += BOOKS[name]["bids"].items()
        asks += BOOKS[name]["asks"].items()
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    cap = dynamic_cap(bids, asks)
    bids = [(p, min(s, cap)) for p, s in bids]
    asks = [(p, min(s, cap)) for p, s in asks]

    # marginal price curves at integer BTC volumes
    def marginal(levels, vmax=2000):
        out, cum, v = [], 0.0, 1
        for p, s in levels:
            cum += s
            while v <= cum and v <= vmax:
                out.append(p)
                v += 1
            if v > vmax:
                break
        return out
    A, B = marginal(asks), marginal(bids)
    n = min(len(A), len(B))
    if n == 0:
        return None
    v_T = 0
    mid_curve = []
    for i in range(n):
        m = (A[i] + B[i]) / 2.0
        mid_curve.append(m)
        if (A[i] / m - 1.0) <= SPREAD_D:
            v_T = i + 1
        else:
            break
    v_T = max(v_T, 1)
    lam = LAMBDA0 / v_T if LAMBDA_MODE == "scaled" else LAMBDA0
    w = [lam * math.exp(-lam * v) for v in range(1, v_T + 1)]
    nf = sum(w)
    val = sum(mid_curve[i] * w[i] for i in range(v_T)) / nf
    return {"ts": round(now, 3), "synthetic_brti": round(val, 2),
            "v_T": v_T, "cap": round(cap, 2) if cap != float("inf") else None,
            "lam": round(lam, 4), "n_books": len(active),
            "active": sorted(active), "excluded": excluded,
            "mids": {k: round(v, 2) for k, v in mids.items()}}


async def emitter(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    f, hour = None, None
    while True:
        await asyncio.sleep(max(0.0, 1.0 - (time.time() % 1.0)))
        now = time.time()
        rec = synthetic_brti(now)
        h = time.strftime("%Y%m%dT%H", time.gmtime(now))
        if h != hour:
            if f:
                f.close()
            f = open(os.path.join(out_dir, f"synthetic_brti_{h}.ndjson"), "a")
            hour = h
        if rec:
            f.write(json.dumps(rec) + "\n")
            f.flush()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    await asyncio.gather(emitter(a.out), *[fn() for fn in ADAPTERS])


if __name__ == "__main__":
    asyncio.run(main())
