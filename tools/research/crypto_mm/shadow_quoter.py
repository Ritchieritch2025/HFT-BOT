#!/usr/bin/env python3
"""Shadow quoter — decisive anchor-freshness experiment (no live orders).

Consumes two live Kalshi websockets:
  1. cfbenchmarks_value  (BRTI)            -> anchor + lock-in ticks
  2. market data (orderbook_delta/trade)   -> KXBTC15M books + tape

Every second it computes kernel fair value (rti_pricing) per open
KXBTC15M market, decides which side it WOULD quote (edge >= theta,
10m <= tte < 30m, hard withdraw at tte 5m), and logs:
  - every shadow quote decision (place/cancel) with book state;
  - every would-be fill under the audit tail-queue rule (queue_ahead
    from displayed size at join, consumed by observed taker prints
    through our level);
  - settlement marking from the official market lifecycle.

Output: NDJSON receipts (one line per event) under --out-dir, one file
per UTC hour, fsync'd — same discipline as the firehose. The analysis
side replays these receipts to place our realized edge on the
(-1c .. +6c) anchor-freshness bracket measured 2026-07-25.

NO ORDERS ARE SENT. This process has no trading permissions and only
requires a read/market-data API key.

Deployment (build line): /opt venv needs `websockets` + `cryptography`.
Auth: Kalshi WS handshake headers KALSHI-ACCESS-KEY / -SIGNATURE /
-TIMESTAMP (RSA-PSS over "{ts}GET/trade-api/ws/v2"), key paths via env
KALSHI_KEY_ID / KALSHI_PRIV_KEY_PATH.  Run under systemd like other
capture units; this script is crash-only (state rebuilt from snapshots).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rti_pricing as rp  # noqa: E402

try:
    import websockets
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError as exc:  # pragma: no cover
    raise SystemExit("needs `websockets` + `cryptography` in the venv") from exc

MD_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
# Live-probed 2026-07-25 (build line): the /cfbenchmarks_value path 404s;
# the channel rides the standard v2 socket and subscribe REQUIRES index_ids.
CF_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
SERIES = "KXBTC15M"
THETA_C = 2.0
JOIN_LO_S, JOIN_HI_S, WITHDRAW_S = 600, 1800, 300
CLIP = 5
SIGMA_WINDOW = 300          # trailing RTI ticks for sigma (~5 min)


def _sign(priv_key, ts_ms: str, path: str) -> str:
    msg = f"{ts_ms}GET{path}".encode()
    sig = priv_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def auth_headers(path: str) -> dict:
    key_id = os.environ["KALSHI_KEY_ID"]
    key_path = os.environ["KALSHI_PRIV_KEY_PATH"]
    priv = serialization.load_pem_private_key(
        Path(key_path).read_bytes(), password=None)
    ts_ms = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": _sign(priv, ts_ms, path),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
    }


class HourlyLog:
    def __init__(self, out_dir: Path, prefix: str):
        self.dir = out_dir
        self.prefix = prefix
        self.hour = None
        self.fh = None
        out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, obj: dict) -> None:
        obj["recv_wall_ns"] = time.time_ns()
        obj["recv_mono_ns"] = time.monotonic_ns()
        hour = time.strftime("%Y%m%dT%H", time.gmtime())
        if hour != self.hour:
            if self.fh:
                self.fh.flush(); os.fsync(self.fh.fileno()); self.fh.close()
            self.hour = hour
            self.fh = open(self.dir / f"{self.prefix}_{hour}.ndjson", "a")
        self.fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.fh.flush()


class Shadow:
    """Kernel state: RTI window, per-market book, shadow quotes."""

    def __init__(self, log: HourlyLog):
        self.log = log
        self.rti = collections.deque(maxlen=SIGMA_WINDOW)
        self.lock_ticks = {}      # mt -> (sum, n) inside final minute
        self.books = {}           # mt -> {"y": {px: qty}, "n": {...}}
        self.meta = {}            # mt -> (close_ts_s, strike)
        self.quotes = {}          # mt -> {"y": qd|None, "n": qd|None}

    # ---- feed handlers -------------------------------------------------
    def on_rti(self, value: float, src_ts_ms: int) -> None:
        self.rti.append(value)
        now = time.time()
        for mt, (close_s, strike) in list(self.meta.items()):
            if 0 < close_s - now <= 60:
                s, n = self.lock_ticks.get(mt, (0.0, 0))
                self.lock_ticks[mt] = (s + value, n + 1)
        self.evaluate()

    def on_book(self, mt: str, side: str, price: int, delta: int,
                snapshot=None) -> None:
        bk = self.books.setdefault(mt, {"y": {}, "n": {}})
        if snapshot is not None:
            bk["y"], bk["n"] = snapshot
        else:
            key = "y" if side == "yes" else "n"
            bk[key][price] = bk[key].get(price, 0) + delta

    def on_trade(self, mt: str, yes_price: int, count: int, taker: str) -> None:
        """Tail-queue fill check for resting shadow quotes."""
        qs = self.quotes.get(mt)
        if not qs:
            return
        for sk, sell_taker in (("y", "no"), ("n", "yes")):
            qd = qs.get(sk)
            if qd is None or taker != sell_taker:
                continue
            px_side = yes_price if sk == "y" else 10000 - yes_price
            if px_side <= qd["lvl"]:
                qd["ahead"] -= count
                if qd["ahead"] < -CLIP:
                    self.log.write({"ev": "SHADOW_FILL", "mt": mt,
                                    "side": sk, "lvl": qd["lvl"],
                                    "joined_at": qd["t"]})
                    qs[sk] = None

    # ---- kernel + policy ------------------------------------------------
    def evaluate(self) -> None:
        if len(self.rti) < 31:
            return
        ticks = list(self.rti)
        sigma = rp.sigma_from_ticks(ticks)
        rti = ticks[-1]
        now = time.time()
        for mt, (close_s, strike) in list(self.meta.items()):
            tte = close_s - now
            if tte <= 0:
                self.meta.pop(mt, None)
                continue
            ls, ln = self.lock_ticks.get(mt, (0.0, 0))
            p = rp.p_settle_above(rti, strike, sigma or 0.0, tte,
                                  locked_sum=ls, locked_n=ln)
            bk = self.books.get(mt)
            if p is None or not bk:
                continue
            yb = max((k for k, v in bk["y"].items() if v > 0), default=None)
            nb = max((k for k, v in bk["n"].items() if v > 0), default=None)
            if yb is None or nb is None:
                continue
            edge_y, edge_n = rp.maker_edges(p, yb / 100.0, (10000 - nb) / 100.0)
            qs = self.quotes.setdefault(mt, {"y": None, "n": None})
            for sk, edge, lvl, depth in (
                ("y", edge_y, yb, bk["y"].get(yb, 0)),
                ("n", edge_n, nb, bk["n"].get(nb, 0)),
            ):
                want = (edge >= THETA_C and JOIN_LO_S <= tte < JOIN_HI_S)
                if tte <= WITHDRAW_S:
                    want = False
                qd = qs[sk]
                if qd is None and want:
                    qs[sk] = {"lvl": lvl, "ahead": depth, "t": now}
                    self.log.write({"ev": "SHADOW_PLACE", "mt": mt,
                                    "side": sk, "lvl": lvl, "ahead": depth,
                                    "p": round(p, 4), "edge_c": round(edge, 2),
                                    "rti": rti, "tte_s": int(tte)})
                elif qd is not None and (not want or qd["lvl"] != lvl):
                    self.log.write({"ev": "SHADOW_CANCEL", "mt": mt,
                                    "side": sk, "lvl": qd["lvl"],
                                    "reason": "edge_gone" if not want else "reprice"})
                    qs[sk] = None


async def cf_task(shadow: Shadow, log: HourlyLog):
    while True:
        try:
            async with websockets.connect(
                    CF_URL, additional_headers=auth_headers("/trade-api/ws/v2"),
                    ping_interval=10) as ws:
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["cfbenchmarks_value"],
                               "index_ids": ["BRTI"]}}))
                async for raw in ws:
                    log.write({"ev": "CF_RAW", "raw": raw[:2000]})
                    m = json.loads(raw)
                    if m.get("type") == "cfbenchmarks_value":
                        data = json.loads(m["msg"]["data"])
                        value = float(data.get("value") or data.get("price"))
                        shadow.on_rti(value, m["msg"]["received_at"])
        except Exception as exc:
            log.write({"ev": "CF_WS_ERROR", "err": str(exc)[:300]})
            await asyncio.sleep(2)


REST_BASE = "https://external-api.kalshi.com/trade-api/v2"
DISCOVER_EVERY_S = 60


def _px_e4(dollars: str) -> int:
    return int(round(float(dollars) * 10_000))


def _discover_open_markets() -> dict:
    """REST: open SERIES markets -> {ticker: (close_epoch_s, strike)}.

    Live-verified contract 2026-07-25: orderbook_delta REJECTS series-level
    subscription, so the md socket must name market tickers explicitly and
    re-subscribe as 15-minute windows rotate.  status=open is the accepted
    server-side filter (maps to status 'active').
    """
    import datetime as dt
    import urllib.parse
    import urllib.request

    url = "%s/markets?%s" % (REST_BASE, urllib.parse.urlencode(
        {"series_ticker": SERIES, "status": "open", "limit": 1000}))
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.load(resp)
    found = {}
    for m in payload.get("markets", []):
        ticker = m.get("ticker", "")
        close_raw = m.get("close_time") or m.get("expected_expiration_time")
        strike = m.get("floor_strike")
        if strike is None:
            strike = m.get("cap_strike")
        if not ticker or not close_raw or strike is None:
            continue
        close_s = int(dt.datetime.fromisoformat(
            close_raw.replace("Z", "+00:00")).timestamp())
        found[ticker] = (close_s, float(strike))
    return found


async def md_task(shadow: Shadow, log: HourlyLog):
    loop = asyncio.get_running_loop()
    while True:
        try:
            open_markets = await loop.run_in_executor(
                None, _discover_open_markets)
            shadow.meta.update(open_markets)
            tickers = sorted(open_markets)
            if not tickers:
                log.write({"ev": "MD_NO_OPEN_MARKETS"})
                await asyncio.sleep(5)
                continue
            async with websockets.connect(
                    MD_URL, additional_headers=auth_headers("/trade-api/ws/v2"),
                    ping_interval=10) as ws:
                # Live schema (probed 2026-07-25): trade ignores series
                # filters and floods every series -> filter by ticker prefix;
                # book prices arrive as dollar STRINGS (tapered_deci_cent).
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta", "trade"],
                               "market_tickers": tickers}}))
                log.write({"ev": "MD_SUBSCRIBED", "tickers": tickers})
                next_discover = time.time() + DISCOVER_EVERY_S
                while True:
                    timeout = max(1.0, next_discover - time.time())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        m = json.loads(raw)
                        t = m.get("type")
                        msg = m.get("msg", {})
                        mt = msg.get("market_ticker", "")
                        if t == "orderbook_snapshot":
                            shadow.on_book(mt, "", 0, 0, snapshot=(
                                {_px_e4(p): float(q) for p, q in
                                 (msg.get("yes_dollars_fp") or [])},
                                {_px_e4(p): float(q) for p, q in
                                 (msg.get("no_dollars_fp") or [])}))
                        elif t == "orderbook_delta":
                            shadow.on_book(
                                mt, msg["side"],
                                _px_e4(msg["price_dollars"]),
                                float(msg["delta_fp"]))
                        elif t == "trade":
                            if not mt.startswith(SERIES + "-"):
                                continue
                            yes_e4 = _px_e4(msg["yes_price_dollars"])
                            count = float(msg["count_fp"])
                            shadow.on_trade(mt, yes_e4, count,
                                            msg["taker_side"])
                            log.write({"ev": "TRADE", "mt": mt,
                                       "yes_price_e4": yes_e4,
                                       "count": count,
                                       "taker_side": msg.get("taker_side"),
                                       "ts_ms": msg.get("ts_ms")})
                    if time.time() >= next_discover:
                        current = await loop.run_in_executor(
                            None, _discover_open_markets)
                        shadow.meta.update(current)
                        if set(current) != set(tickers):
                            log.write({"ev": "MD_ROTATION",
                                       "new": sorted(set(current)
                                                     - set(tickers)),
                                       "gone": sorted(set(tickers)
                                                      - set(current))})
                            break  # reconnect with the fresh window set
                        next_discover = time.time() + DISCOVER_EVERY_S
        except Exception as exc:
            log.write({"ev": "MD_WS_ERROR", "err": str(exc)[:300]})
            await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    log = HourlyLog(args.out_dir, "shadow_btc15m")
    shadow = Shadow(log)
    log.write({"ev": "START", "series": SERIES, "theta_c": THETA_C,
               "clip": CLIP, "join_s": [JOIN_LO_S, JOIN_HI_S],
               "withdraw_s": WITHDRAW_S, "no_orders": True})
    await asyncio.gather(cf_task(shadow, log), md_task(shadow, log))

if __name__ == "__main__":
    asyncio.run(main())
