#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mm_dashboard — READ-ONLY terminal panel for the BTC15M MM engine.

Runs directly in an SSH terminal (plain ANSI, zero third-party deps),
refreshes every second.  Four blocks + a rolling decision ticker:

  1 status bar   mode | uptime | RTI age (>5s red) | connection | HALT
  2 P&L          realized today (exchange settlements) | floating |
                 net c/contract | fills | paired rate | windows
  3 positions    per market: net (yellow > cap/2, red > cap) | cost/$cap
                 | resting orders (side/px/qty/age) | tte
  4 latency      order ACK p50/p99 | cancel ACK p50/p99 | rate blocks |
                 reject rate | recon divergence | last recon

Data sources: the engine's NDJSON receipts/heartbeat (MM_OUT) and
read-only exchange GETs.  STRICTLY no order placement, no cancels, no
control writes.  A source older than its freshness budget renders as
"数据陈旧 N 秒" — stale numbers are never shown as live.
"""
from __future__ import annotations

import base64
import collections
import glob
import json
import math
import os
import sys
import time
import datetime as dt
import urllib.request

OUT_DIR = os.environ.get("MM_OUT", "/home/ubuntu/h6b_inputs/mm_engine")
REST = "https://api.elections.kalshi.com/trade-api/v2"
RTI_RED_S = 5.0
HEART_MAX_S = 25.0     # HEALTH heartbeat is 10s; 2 misses = stale

R = "\x1b[31;1m"; Y = "\x1b[33;1m"; G = "\x1b[32m"; DIM = "\x1b[2m"
B = "\x1b[1m"; N = "\x1b[0m"


# ------------------------------------------------------------ pure logic
def pct(sorted_xs, q):
    """Nearest-rank percentile of an ascending list; None when empty."""
    if not sorted_xs:
        return None
    k = max(1, math.ceil(q / 100.0 * len(sorted_xs)))
    return sorted_xs[min(k, len(sorted_xs)) - 1]


def paired_rate(fills):
    """2*min(yes,no) contracts per market / total contracts."""
    by = {}
    for f in fills or []:
        d = by.setdefault(f.get("ticker"), {"y": 0, "n": 0})
        side = "y" if f.get("side") in ("yes", "bid") else "n"
        d[side] += int(f.get("count", 0) or 0)
    total = sum(d["y"] + d["n"] for d in by.values())
    if not total:
        return None
    paired = sum(2 * min(d["y"], d["n"]) for d in by.values())
    return paired / total


def realized_today(settlements, day_utc):
    """(usd, windows, contracts) from EXCHANGE settlement records whose
    settled_time falls on day_utc (YYYY-MM-DD).  The only P&L source."""
    usd, windows, contracts = 0.0, 0, 0
    for s in settlements or []:
        ts = str(s.get("settled_time") or "")
        if not ts.startswith(day_utc):
            continue
        rev = float(s.get("revenue") or 0)
        cost = (float(s.get("yes_total_cost") or 0)
                + float(s.get("no_total_cost") or 0))
        usd += (rev - cost) / 100.0
        windows += 1
        contracts += int(s.get("yes_count") or 0) + int(s.get("no_count") or 0)
    return usd, windows, contracts


def floating_pnl(positions, marks):
    """Mark open positions to yes-mid (cents).  None if any mark missing
    — a partly-marked number is a lie, show nothing instead."""
    total_c = 0.0
    for p in positions or []:
        pos = int(p.get("position") or 0)
        if pos == 0:
            continue
        mark = marks.get(p.get("ticker"))
        if mark is None:
            return None
        value_c = pos * mark if pos > 0 else (-pos) * (100.0 - mark)
        total_c += value_c - float(p.get("market_exposure") or 0)
    return total_c / 100.0


def net_severity(net, max_net):
    a = abs(int(net))
    if a > max_net:
        return "crit"
    if a and a >= max_net / 2.0:
        return "warn"
    return "ok"


class Source:
    """Freshness tracker for one data feed."""

    def __init__(self, name, max_age_s):
        self.name = name
        self.max_age_s = max_age_s
        self.last_ok = None

    def ok(self, now=None):
        self.last_ok = time.monotonic() if now is None else now

    def age(self, now=None):
        if self.last_ok is None:
            return None
        return (time.monotonic() if now is None else now) - self.last_ok

    def stale(self, now=None):
        a = self.age(now)
        return a is None or a > self.max_age_s


def stale_or(source, now=None, value_lines=()):
    """The staleness rule: NEVER show old values as live."""
    if not source.stale(now):
        return list(value_lines)
    a = source.age(now)
    tag = f"{int(a)}s" if a is not None else "从未成功"
    return [f"{R}⚠ {source.name} 数据陈旧 {tag} — 值已隐藏{N}"]


class NdjsonTail:
    """Incremental reader over MM_OUT/mm_*.ndjson (hourly files)."""

    def __init__(self, dirpath):
        self.dir = dirpath
        self.offsets = {}

    def poll(self):
        events = []
        for fn in sorted(glob.glob(os.path.join(self.dir, "mm_*.ndjson"))):
            off = self.offsets.get(fn, 0)
            try:
                with open(fn, "rb") as h:
                    h.seek(off)
                    chunk = h.read()
            except OSError:
                continue
            if not chunk:
                continue
            cut = chunk.rfind(b"\n")
            if cut < 0:
                continue                      # no complete line yet
            self.offsets[fn] = off + cut + 1
            for line in chunk[:cut].split(b"\n"):
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        return events


class EngineState:
    """Reduce the receipt stream into what the panel shows."""

    def __init__(self):
        self.start_ns = None
        self.mode = "?"
        self.health = {}
        self.health_ns = None
        self.order_ms = collections.deque(maxlen=500)
        self.cancel_ms = collections.deque(maxlen=500)
        self.acks = 0
        self.rejs = 0
        self.rate_skips = 0
        self.fills = 0
        self.last_recon_ok_ns = None
        self.recon_fetch_fails = 0
        self.halt_reason = ""
        self.decisions = collections.deque(maxlen=5)

    def reject_rate(self):
        n = self.acks + self.rejs
        return (self.rejs / n) if n else None

    def _decide(self, ns, kind, text):
        self.decisions.append((ns, kind, text))

    def feed(self, events):
        for e in events:
            ev = e.get("ev")
            ns = e.get("wall_ns")
            if ev == "START":
                self.start_ns = ns
                self.mode = e.get("mode", "?")
            elif ev == "HEALTH":
                self.health = e
                self.health_ns = ns
                self.mode = e.get("mode", self.mode)
            elif ev in ("ORDER_ACK", "INTENT_PLACE"):
                if ev == "ORDER_ACK":
                    self.acks += 1
                    if e.get("ms") is not None:
                        self.order_ms.append(float(e["ms"]))
                edge = e.get("edge_c")
                why = f" edge={edge:+.1f}c" if edge is not None else ""
                self._decide(ns, "挂", f"{e.get('mt')} {e.get('side')} "
                                       f"@{e.get('px')}{why}")
            elif ev == "ORDER_REJ":
                self.rejs += 1
                self._decide(ns, "拒", f"{e.get('mt')} {e.get('side')} "
                                       f"@{e.get('px')} code={e.get('code')}")
            elif ev in ("CANCEL_ACK", "INTENT_CANCEL"):
                if ev == "CANCEL_ACK" and e.get("ms") is not None:
                    self.cancel_ms.append(float(e["ms"]))
                why = e.get("reason") or ""
                self._decide(ns, "撤", f"{e.get('mt')} {e.get('side')}"
                                       f" {why}".rstrip())
            elif ev == "FILL":
                self.fills += 1
                raw = e.get("raw") or {}
                self._decide(ns, "成交", f"{raw.get('ticker')} "
                                         f"{raw.get('side')} x{raw.get('count')}")
            elif ev == "RATE_SKIP":
                self.rate_skips += 1
            elif ev == "RECON_OK":
                self.last_recon_ok_ns = ns
            elif ev == "RECON_FETCH_FAIL":
                self.recon_fetch_fails += 1
            elif ev in ("RECON_HALT", "ECON_HALT", "KILL", "HALT"):
                self.halt_reason = f"{ev}: {e.get('reason', '')}"


# ------------------------------------------------------- exchange client
class Exchange:
    """Read-only GETs.  No credentials -> panel runs file-only."""

    def __init__(self):
        self.enabled = False
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = (os.environ.get("KALSHI_PRIV_KEY_PATH")
                    or os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
        if not key_id or not key_path or not os.path.exists(key_path):
            return
        try:
            from cryptography.hazmat.primitives import (hashes,
                                                        serialization)
            from cryptography.hazmat.primitives.asymmetric import padding
            self._hashes, self._padding = hashes, padding
            with open(key_path, "rb") as h:
                self._priv = serialization.load_pem_private_key(
                    h.read(), password=None)
            self._key_id = key_id
            self.enabled = True
        except Exception:
            self.enabled = False

    def get(self, path):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET/trade-api/v2{path.split('?')[0]}".encode()
        sig = self._priv.sign(
            msg,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH),
            self._hashes.SHA256())
        req = urllib.request.Request(REST + path, method="GET", headers={
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.load(r)
        except Exception:
            return -1, {}


# ------------------------------------------------------------- rendering
def _fmt_ms(v):
    return f"{v:.0f}ms" if v is not None else "—"


def _uptime(start_ns, now_ns):
    if not start_ns:
        return "—"
    s = int((now_ns - start_ns) / 1e9)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def _hms(ns):
    if not ns:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.gmtime(ns / 1e9))


def render(st, ex_state, width=100):
    now_ns = time.time_ns()
    lines = []

    # -- block 1: status bar
    heart_age = ((now_ns - st.health_ns) / 1e9) if st.health_ns else None
    heart_ok = heart_age is not None and heart_age <= HEART_MAX_S
    h = st.health if heart_ok else {}
    rti_ages = (h.get("rti_age") or {})
    rti_age = max(rti_ages.values()) if rti_ages else None
    rti_s = ("—" if rti_age is None else
             (f"{R}{rti_age:.1f}s{N}" if rti_age > RTI_RED_S
              else f"{G}{rti_age:.1f}s{N}"))
    conn = (f"{G}心跳 {heart_age:.0f}s{N}" if heart_ok else
            f"{R}引擎心跳陈旧 {heart_age:.0f}s{N}" if heart_age is not None
            else f"{R}无引擎心跳{N}")
    halted = bool(h.get("halted")) or bool(st.halt_reason)
    halt_s = (f"{R}HALT {st.halt_reason or '(engine flag)'}{N}"
              if halted else f"{G}正常{N}")
    mode_s = (f"{Y}{st.mode}{N}" if st.mode != "live" else f"{B}LIVE{N}")
    lines.append(f"{B}[{mode_s}]{N} 运行 {_uptime(st.start_ns, now_ns)}"
                 f" | RTI龄 {rti_s} | {conn} | {halt_s}")
    lines.append("─" * width)

    # -- block 2: P&L (exchange truth)
    lines.append(f"{B}【PnL(交易所结算口径)】{N}")
    if not ex_state["enabled"]:
        lines.append(f"{DIM}无交易所凭证 — PnL 块停用{N}")
    else:
        pl = []
        usd, windows, contracts = ex_state["realized"]
        per_ct = (usd * 100.0 / contracts) if contracts else None
        flt = ex_state["floating"]
        pr = ex_state["paired"]
        pl.append(
            f"今日已实现 {'':1}{(R if usd < 0 else G)}${usd:+.2f}{N}"
            f" | 浮动 {('—' if flt is None else f'${flt:+.2f}')}"
            f" | 每张净益 {('—' if per_ct is None else f'{per_ct:+.2f}¢')}"
            f" | 成交 {ex_state['fills_n']} 笔"
            f" | 配对率 {('—' if pr is None else f'{pr * 100:.0f}%')}"
            f" | 今日窗口 {windows}")
        lines += stale_or(ex_state["src_pnl"], value_lines=pl)
    lines.append("─" * width)

    # -- block 3: positions & orders
    lines.append(f"{B}【持仓与订单】{N}")
    if not ex_state["enabled"]:
        lines.append(f"{DIM}无交易所凭证 — 持仓块停用{N}")
    else:
        rows = []
        max_net = int(h.get("max_net") or 6)
        cap = float(h.get("max_open_cost") or 0)
        for mt in sorted(set(list(ex_state["pos_by_mt"]) +
                             list(ex_state["ord_by_mt"]))):
            p = ex_state["pos_by_mt"].get(mt, {})
            net = int(p.get("position") or 0)
            cost = float(p.get("market_exposure") or 0) / 100.0
            sev = net_severity(net, max_net)
            col = R if sev == "crit" else (Y if sev == "warn" else "")
            ods = []
            for o in ex_state["ord_by_mt"].get(mt, []):
                age = o.get("_age_s")
                ods.append(f"{o.get('side')}@{o.get('yes_price')}c"
                           f"x{o.get('remaining_count')}"
                           f" {int(age)}s" if age is not None else "")
            tte = ex_state["tte_by_mt"].get(mt)
            tte_s = "—" if tte is None else (
                f"{int(tte // 60)}m{int(tte % 60):02d}s" if tte > 0 else "已收")
            row = (f"{mt:<28} 净 {net:+d}张"
                   f" | ${cost:.2f}/${cap:.0f}"
                   f" | 单 {('; '.join(x for x in ods if x) or '无')}"
                   f" | 距结算 {tte_s}")
            rows.append(f"{col}{row}{N}" if col else row)
        if not rows:
            rows = [f"{DIM}零持仓、零在场订单{N}"]
        lines += stale_or(ex_state["src_pos"], value_lines=rows)
    lines.append("─" * width)

    # -- block 4: latency & health
    lines.append(f"{B}【延迟与健康】{N}")
    o = sorted(st.order_ms)
    c = sorted(st.cancel_ms)
    rr = st.reject_rate()
    recon_age = ((now_ns - st.last_recon_ok_ns) / 1e9
                 if st.last_recon_ok_ns else None)
    recon_s = ("—" if recon_age is None else
               (f"{R}{recon_age:.0f}s前{N}" if recon_age > 15
                else f"{G}{recon_age:.0f}s前{N}"))
    lines.append(
        f"下单ACK p50 {_fmt_ms(pct(o, 50))} p99 {_fmt_ms(pct(o, 99))}"
        f" | 撤单ACK p50 {_fmt_ms(pct(c, 50))} p99 {_fmt_ms(pct(c, 99))}"
        f" | 限速拦截 {st.rate_skips}"
        f" | 重挂抑制 {h.get('requote_suppressed', '—')}"
        f" | 拒单率 {('—' if rr is None else f'{rr * 100:.1f}%')}")
    lines.append(
        f"对账: 最近成功 {recon_s} | 拉取失败 {st.recon_fetch_fails}"
        f" | recon_fails(心跳) {h.get('recon_fails', '—')}"
        f" | 已实现(引擎) {h.get('realized', '—')}")
    lines.append("─" * width)

    # -- ticker
    lines.append(f"{B}【最近决策】{N}")
    if st.decisions:
        for ns, kind, text in list(st.decisions):
            k = {"成交": G + "成交" + N, "拒": R + "拒" + N}.get(kind, kind)
            lines.append(f"{DIM}{_hms(ns)}{N} {k} {text}")
    else:
        lines.append(f"{DIM}(暂无){N}")
    return lines


# ------------------------------------------------------------------ main
def main():
    ex = Exchange()
    tail = NdjsonTail(OUT_DIR)
    st = EngineState()
    src_pnl = Source("PnL/交易所", max_age_s=45.0)
    src_pos = Source("持仓/交易所", max_age_s=20.0)
    ex_state = {"enabled": ex.enabled, "realized": (0.0, 0, 0),
                "floating": None, "paired": None, "fills_n": 0,
                "pos_by_mt": {}, "ord_by_mt": {}, "tte_by_mt": {},
                "src_pnl": src_pnl, "src_pos": src_pos}
    last_fast = last_slow = 0.0
    sys.stdout.write("\x1b[2J\x1b[?25l")
    try:
        while True:
            st.feed(tail.poll())
            now = time.monotonic()
            if ex.enabled and now - last_fast >= 5.0:
                last_fast = now
                code, d = ex.get(
                    "/portfolio/positions?settlement_status=unsettled&limit=200")
                code2, d2 = ex.get("/portfolio/orders?status=resting&limit=200")
                if code == 200 and code2 == 200:
                    pos = [p for p in (d.get("market_positions") or [])
                           if int(p.get("position") or 0) != 0]
                    ex_state["pos_by_mt"] = {p["ticker"]: p for p in pos}
                    by = {}
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    for o in (d2.get("orders") or []):
                        try:
                            created = dt.datetime.fromisoformat(
                                str(o.get("created_time")).replace(
                                    "Z", "+00:00"))
                            o["_age_s"] = (now_utc - created).total_seconds()
                        except (TypeError, ValueError):
                            o["_age_s"] = None
                        by.setdefault(o.get("ticker"), []).append(o)
                    ex_state["ord_by_mt"] = by
                    tickers = sorted(set(list(ex_state["pos_by_mt"]) +
                                         list(by)))
                    marks, tte = {}, {}
                    if tickers:
                        code3, d3 = ex.get(
                            "/markets?tickers=" + ",".join(tickers[:40]))
                        if code3 == 200:
                            for mk in (d3.get("markets") or []):
                                yb = mk.get("yes_bid")
                                ya = mk.get("yes_ask")
                                if yb is not None and ya is not None:
                                    marks[mk["ticker"]] = (float(yb)
                                                           + float(ya)) / 2.0
                                try:
                                    cs = dt.datetime.fromisoformat(
                                        str(mk.get("close_time")).replace(
                                            "Z", "+00:00"))
                                    tte[mk["ticker"]] = (
                                        cs - now_utc).total_seconds()
                                except (TypeError, ValueError):
                                    pass
                    ex_state["tte_by_mt"] = tte
                    ex_state["floating"] = floating_pnl(pos, marks)
                    src_pos.ok()
            if ex.enabled and now - last_slow >= 15.0:
                last_slow = now
                day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                c1, s1 = ex.get("/portfolio/settlements?limit=200")
                c2, s2 = ex.get("/portfolio/fills?limit=200")
                if c1 == 200 and c2 == 200:
                    ex_state["realized"] = realized_today(
                        s1.get("settlements"), day)
                    fills = [f for f in (s2.get("fills") or [])
                             if str(f.get("created_time", "")).startswith(day)]
                    ex_state["fills_n"] = len(fills)
                    ex_state["paired"] = paired_rate(fills)
                    src_pnl.ok()
            lines = render(st, ex_state)
            out = ["\x1b[H"]
            for ln in lines:
                out.append(ln + "\x1b[K\n")
            out.append("\x1b[J")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
