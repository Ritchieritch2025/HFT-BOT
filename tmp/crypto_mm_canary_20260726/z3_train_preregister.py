#!/usr/bin/env python3
"""Bounded, TRAIN-only Z3 pair-policy search.

This is an experiment artifact, not production code.  It deliberately has no
automatic VALIDATE path.  Phase A freezes one of ten pre-registered orphan-exit
policies.  Phase B combines that frozen exit with six pre-registered admission
rules.  All inputs are decision-time observable; all thresholds are derived
from TRAIN admissions only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

import duckdb

CLIP_E4 = 10_000
CLIP_CT = 1
LAT_US = 60_000
PAIR_LOCK_E4 = 9_900
TAKER_FEE_RATE = 0.07
Z3_LO_US = 120_000_000
Z3_HI_US = 300_000_000
FLOW_WINDOW_US = 60_000_000
TRAIN_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
SOURCE_BASE_SHA = (
    "57bdd55ae86589863645d3e50f32cb0f2d5bb161ad78ed3b639d3ef442440e10"
)

SNAP_DIR = os.environ.get(
    "E4_SNAP_DIR",
    "/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z",
)
L2_GLOB = os.environ.get(
    "E4_L2_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/"
    "orderbooks_full/category=Crypto/**/*.parquet",
)
TR_GLOB = os.environ.get(
    "E4_TR_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/"
    "trades/category=Crypto/**/*.csv.gz",
)
OUT = os.environ.get("Z3_TRAIN_OUT", "/tmp/z3_train_preregister_report.json")
RAW_OUT = os.environ.get("Z3_TRAIN_RAW_OUT", "/tmp/z3_train_preregister_raw.json")


def walk_book_sell(book, qty_e4):
    """Consume displayed bids best-first; do not mutate the historical book."""
    fills = []
    remaining = qty_e4
    for price in sorted(
        (p for p, volume in book.items() if (volume or 0) > 0), reverse=True
    ):
        take = min(remaining, book[price])
        fills.append((price, take))
        remaining -= take
        if remaining <= 0:
            break
    return fills, qty_e4 - remaining


def taker_fee_c_per_order(fills):
    """Official 7% quadratic taker fee, total rounded up to one cent."""
    dollars = sum(
        TAKER_FEE_RATE
        * (qty / 10000.0)
        * (price / 10000.0)
        * (1.0 - price / 10000.0)
        for price, qty in fills
    )
    return float(math.ceil(dollars * 100.0))


@dataclass(frozen=True)
class Policy:
    name: str
    ttl_s: float = 60.0
    distance_stop_c: float | None = None
    cost_stop_c: float | None = None
    gate_kind: str = "none"
    eta_max_s: float | None = None
    min_flow60: float | None = None
    phase: str = "A_exit"


@dataclass
class Quote:
    lvl: int
    ahead: int
    placed_ts: int
    cx: int | None = None
    cx_reason: str | None = None


@dataclass
class ArmState:
    quotes: dict = field(default_factory=lambda: {"y": None, "n": None})
    orphan_side: str | None = None
    orphan_entry: int | None = None
    first_ts: int | None = None
    admission: dict | None = None
    ioc_due: int | None = None
    exit_reason: str | None = None
    exit_trigger_ts: int | None = None
    exit_trigger_est_c: float | None = None
    cycles: list = field(default_factory=list)
    admissions: list = field(default_factory=list)
    placed: int = 0
    fills: int = 0
    paired: int = 0
    flattened: int = 0
    carried: int = 0
    zone_cancel_orders: int = 0
    stop_cancel_orders: int = 0
    fills_during_cancel: int = 0
    exit_replacements: int = 0
    bugs: list = field(default_factory=list)


def best(book):
    return max((p for p, v in book.items() if (v or 0) > 0), default=None)


def quantile_linear(values, q):
    vals = sorted(float(x) for x in values)
    if not vals:
        raise ValueError("empty quantile input")
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def wilson_lcb(k, n, z=1.959963984540054):
    if n <= 0:
        return None
    p = k / n
    den = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    rad = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (center - rad) / den


def cluster_boot_ci(cycles, seed, reps=5000):
    by_market = defaultdict(list)
    for row in cycles:
        by_market[row["market"]].append(float(row["pnl_c"]))
    markets = sorted(by_market)
    if not markets:
        return [None, None]
    rng = random.Random(seed)
    boots = []
    for _ in range(reps):
        vals = []
        for _j in markets:
            vals.extend(by_market[rng.choice(markets)])
        boots.append(statistics.fmean(vals))
    boots.sort()
    return [
        round(quantile_linear(boots, 0.025), 4),
        round(quantile_linear(boots, 0.975), 4),
    ]


def load_meta():
    src = json.load(open(f"{SNAP_DIR}/shards/KXBTC15M.json"))
    meta = {}
    for ticker, market in src["markets"].items():
        close_raw = market.get("close_time") or market.get("expected_expiration_time")
        result = str(market.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        try:
            close_us = int(
                dt.datetime.fromisoformat(str(close_raw).replace("Z", "+00:00"))
                .timestamp()
                * 1_000_000
            )
        except Exception:
            continue
        meta[ticker] = (close_us, result)
    return meta


class Z3Market:
    def __init__(self, ticker, close_us, result, trades, policies):
        self.ticker = ticker
        self.close = close_us
        self.result = result
        self.trades = trades
        self.trade_i = 0
        self.books = {"y": {}, "n": {}}
        self.policies = policies
        self.states = {p.name: ArmState() for p in policies}
        self.recent_trades = deque()
        self.first_event_ts = None
        self.last_ts = None

    def _trim_flow(self, ts):
        cutoff = ts - FLOW_WINDOW_US
        while self.recent_trades and self.recent_trades[0][0] < cutoff:
            self.recent_trades.popleft()

    def _features(self, ts, yb, nb):
        self._trim_flow(ts)
        flow_y_e4 = 0
        flow_n_e4 = 0
        for _tts, yes_px, qty, taker in self.recent_trades:
            if taker == "no" and yes_px <= yb:
                flow_y_e4 += qty
            elif taker == "yes" and 10000 - yes_px <= nb:
                flow_n_e4 += qty
        flow_y = flow_y_e4 / 10000.0
        flow_n = flow_n_e4 / 10000.0
        depth_y = (self.books["y"].get(yb, 0) or 0) / 10000.0
        depth_n = (self.books["n"].get(nb, 0) or 0) / 10000.0
        eta_y = math.inf if flow_y <= 0 else (depth_y + CLIP_CT) / (flow_y / 60.0)
        eta_n = math.inf if flow_n <= 0 else (depth_n + CLIP_CT) / (flow_n / 60.0)
        mid_c = (yb + (10000 - nb)) / 200.0
        return {
            "history60": bool(
                self.first_event_ts is not None
                and ts - self.first_event_ts >= FLOW_WINDOW_US
            ),
            "flow_y60": flow_y,
            "flow_n60": flow_n,
            "min_flow60": min(flow_y, flow_n),
            "depth_y": depth_y,
            "depth_n": depth_n,
            "eta_y_s": eta_y,
            "eta_n_s": eta_n,
            "max_eta_s": max(eta_y, eta_n),
            "mid_c": mid_c,
            "yes_bid_c": yb / 100.0,
            "no_bid_c": nb / 100.0,
            "tte_s": (self.close - ts) / 1_000_000.0,
        }

    def _z3_now(self, ts):
        tte = self.close - ts
        yb = best(self.books["y"])
        nb = best(self.books["n"])
        if yb is None or nb is None:
            return None
        mid_c = (yb + (10000 - nb)) / 200.0
        if not (Z3_LO_US <= tte < Z3_HI_US):
            return None
        if not (mid_c < 20.0 or mid_c > 80.0):
            return None
        if yb + nb > PAIR_LOCK_E4:
            return None
        return yb, nb, self._features(ts, yb, nb)

    @staticmethod
    def _gate(policy, features):
        if policy.gate_kind == "none":
            return True
        if not features["history60"] or not math.isfinite(features["max_eta_s"]):
            return False
        if policy.eta_max_s is not None and features["max_eta_s"] > policy.eta_max_s:
            return False
        if (
            policy.min_flow60 is not None
            and features["min_flow60"] < policy.min_flow60
        ):
            return False
        return True

    def _place_quote(self, st, side, lvl, ts, replacement=False):
        book = self.books[side]
        st.quotes[side] = Quote(
            lvl=int(lvl),
            ahead=int(book.get(int(lvl), 0) or 0),
            placed_ts=ts,
        )
        st.placed += 1
        if replacement:
            st.exit_replacements += 1

    def _admit(self, policy, st, ts, yb, nb, features):
        row = {
            "date": dt.datetime.fromtimestamp(
                ts / 1_000_000, dt.timezone.utc
            ).date().isoformat(),
            "market": self.ticker,
            "ts": ts,
            **features,
        }
        st.admissions.append(row)
        st.admission = row
        self._place_quote(st, "y", yb, ts)
        self._place_quote(st, "n", nb, ts)

    def _request_cancel(self, st, side, ts, reason):
        quote = st.quotes[side]
        if quote is None or quote.cx is not None:
            return
        quote.cx = ts + LAT_US
        quote.cx_reason = reason
        if reason == "zone_invalid":
            st.zone_cancel_orders += 1
        else:
            st.stop_cancel_orders += 1

    def _current_ioc_estimate(self, st):
        if st.orphan_side is None or st.orphan_entry is None:
            return None
        own = self.books[st.orphan_side]
        fills, filled = walk_book_sell(own, CLIP_E4)
        rem = CLIP_E4 - filled
        total_c = sum(
            (p - st.orphan_entry) / 100.0 * (q / 10000.0) for p, q in fills
        )
        if filled:
            total_c -= taker_fee_c_per_order(fills)
        if rem:
            won = (self.result == "yes") == (st.orphan_side == "y")
            total_c += (
                ((10000 if won else 0) - st.orphan_entry)
                / 100.0
                * (rem / 10000.0)
            )
        return total_c / CLIP_CT

    def _trigger_exit(self, st, ts, reason):
        if st.orphan_side is None or st.ioc_due is not None:
            return
        st.exit_reason = reason
        st.exit_trigger_ts = ts
        st.exit_trigger_est_c = self._current_ioc_estimate(st)
        exit_side = "n" if st.orphan_side == "y" else "y"
        quote = st.quotes[exit_side]
        st.ioc_due = ts + LAT_US
        if quote is not None:
            self._request_cancel(st, exit_side, ts, reason)

    def _exit_quote_target(self, st):
        held = st.orphan_side
        exit_side = "n" if held == "y" else "y"
        ceiling = PAIR_LOCK_E4 - st.orphan_entry
        held_best = best(self.books[held])
        post_only = ceiling if held_best is None else (10000 - held_best) - 100
        return exit_side, max(100, min(ceiling, post_only, 9900))

    def _record_cycle(self, st, ts, pnl_c, paired, exit_reason):
        adm = st.admission or {}
        age_s = None if st.first_ts is None else (ts - st.first_ts) / 1_000_000.0
        row = {
            "market": self.ticker,
            "date": adm.get("date"),
            "admit_ts": adm.get("ts"),
            "first_ts": st.first_ts,
            "exit_ts": ts,
            "capital_time_s": age_s,
            "paired": bool(paired),
            "pnl_c": float(pnl_c),
            "entry_c": None
            if st.orphan_entry is None
            else st.orphan_entry / 100.0,
            "exit_reason": exit_reason,
            "trigger_ts": st.exit_trigger_ts,
            "trigger_est_pnl_c": st.exit_trigger_est_c,
            "admission_features": {
                k: adm.get(k)
                for k in (
                    "history60",
                    "flow_y60",
                    "flow_n60",
                    "min_flow60",
                    "depth_y",
                    "depth_n",
                    "eta_y_s",
                    "eta_n_s",
                    "max_eta_s",
                    "mid_c",
                    "yes_bid_c",
                    "no_bid_c",
                    "tte_s",
                )
            },
        }
        st.cycles.append(row)

    def _clear_orphan(self, st):
        st.orphan_side = None
        st.orphan_entry = None
        st.first_ts = None
        st.admission = None
        st.ioc_due = None
        st.exit_reason = None
        st.exit_trigger_ts = None
        st.exit_trigger_est_c = None

    def _ioc_flatten(self, st, ts):
        if st.orphan_side is None:
            st.ioc_due = None
            return
        pnl_c = self._current_ioc_estimate(st)
        self._record_cycle(
            st,
            ts,
            pnl_c,
            paired=False,
            exit_reason=st.exit_reason or "ioc",
        )
        st.flattened += 1
        st.quotes = {"y": None, "n": None}
        self._clear_orphan(st)

    def _check_exit_trigger(self, policy, st, ts):
        if st.orphan_side is None or st.ioc_due is not None:
            return
        age_s = (ts - st.first_ts) / 1_000_000.0
        if age_s >= policy.ttl_s:
            self._trigger_exit(st, ts, f"ttl_{policy.ttl_s:g}s")
            return
        exit_side = "n" if st.orphan_side == "y" else "y"
        if policy.distance_stop_c is not None:
            q = st.quotes[exit_side]
            touch = best(self.books[exit_side])
            if (
                q is not None
                and touch is not None
                and touch - q.lvl >= int(round(policy.distance_stop_c * 100))
            ):
                self._trigger_exit(
                    st, ts, f"distance_{policy.distance_stop_c:g}c"
                )
                return
        if policy.cost_stop_c is not None:
            held_bid = best(self.books[st.orphan_side])
            if held_bid is not None:
                pair_cost_e4 = st.orphan_entry + (10000 - held_bid)
                if pair_cost_e4 >= int(round(policy.cost_stop_c * 100)):
                    self._trigger_exit(st, ts, f"cost_{policy.cost_stop_c:g}c")

    def _advance(self, policy, st, ts):
        self._check_exit_trigger(policy, st, ts)
        acked_reasons = []
        for side in ("y", "n"):
            q = st.quotes[side]
            if q is not None and q.cx is not None and ts >= q.cx:
                acked_reasons.append(q.cx_reason)
                st.quotes[side] = None
        if st.orphan_side is not None and st.ioc_due is not None and ts >= st.ioc_due:
            self._ioc_flatten(st, st.ioc_due)
            return
        if (
            st.orphan_side is not None
            and st.ioc_due is None
            and st.quotes["n" if st.orphan_side == "y" else "y"] is None
        ):
            side, lvl = self._exit_quote_target(st)
            self._place_quote(st, side, lvl, ts, replacement=True)

    def _fill(self, policy, st, side, lvl, ts, was_cancel_pending):
        st.quotes[side] = None
        st.fills += 1
        if was_cancel_pending:
            st.fills_during_cancel += 1
        if st.orphan_side is None:
            other = "n" if side == "y" else "y"
            st.orphan_side = side
            st.orphan_entry = lvl
            st.first_ts = ts
            oq = st.quotes[other]
            if oq is None:
                target_side, target = self._exit_quote_target(st)
                self._place_quote(st, target_side, target, ts, replacement=True)
            else:
                pair_sum = lvl + oq.lvl
                if pair_sum > PAIR_LOCK_E4:
                    st.bugs.append(
                        f"unsafe counterpart {pair_sum} at {self.ticker}/{ts}"
                    )
            self._check_exit_trigger(policy, st, ts)
            return
        if side == st.orphan_side:
            st.bugs.append(f"same-side second fill at {self.ticker}/{ts}")
            return
        locked_c = (10000 - st.orphan_entry - lvl) / 100.0
        if locked_c < 1.0 - 1e-9:
            st.bugs.append(f"unsafe pair {locked_c}c at {self.ticker}/{ts}")
        self._record_cycle(st, ts, locked_c, paired=True, exit_reason="maker_pair")
        st.paired += 1
        self._clear_orphan(st)

    def _consume_trades(self, upto):
        while self.trade_i < len(self.trades) and self.trades[self.trade_i][0] <= upto:
            ts, yes_px, qty, taker = self.trades[self.trade_i]
            self.trade_i += 1
            for policy in self.policies:
                st = self.states[policy.name]
                self._advance(policy, st, ts)
                for side, sell_taker in (("y", "no"), ("n", "yes")):
                    q = st.quotes[side]
                    if q is None or taker != sell_taker:
                        continue
                    px_side = yes_px if side == "y" else 10000 - yes_px
                    if px_side <= q.lvl:
                        q.ahead -= qty
                        if q.ahead < -CLIP_E4:
                            self._fill(
                                policy,
                                st,
                                side,
                                q.lvl,
                                ts,
                                was_cancel_pending=q.cx is not None,
                            )
            self.recent_trades.append((ts, yes_px, qty, taker))
            self._trim_flow(ts)

    def on_book(self, ts, mtype, side, px, delta, yes_levels, no_levels):
        if not self.close:
            return
        if self.first_event_ts is None:
            self.first_event_ts = ts
        self.last_ts = ts
        self._consume_trades(ts)
        for policy in self.policies:
            self._advance(policy, self.states[policy.name], ts)
        if mtype == "snapshot":
            self.books = {"y": {}, "n": {}}
            for levels, key in ((yes_levels, "y"), (no_levels, "n")):
                for p, v in levels or []:
                    if p is not None:
                        self.books[key][p] = v or 0
        else:
            if px is None or side not in ("yes", "no"):
                return
            key = "y" if side == "yes" else "n"
            self.books[key][px] = self.books[key].get(px, 0) + (delta or 0)
        eligible = self._z3_now(ts)
        for policy in self.policies:
            st = self.states[policy.name]
            self._advance(policy, st, ts)
            if st.orphan_side is not None:
                continue
            has_quote = st.quotes["y"] is not None or st.quotes["n"] is not None
            if has_quote:
                if eligible is None:
                    self._request_cancel(st, "y", ts, "zone_invalid")
                    self._request_cancel(st, "n", ts, "zone_invalid")
                continue
            if eligible is None:
                continue
            yb, nb, features = eligible
            if self._gate(policy, features):
                self._admit(policy, st, ts, yb, nb, features)

    def finalize(self):
        if self.last_ts is None:
            return
        for policy in self.policies:
            st = self.states[policy.name]
            st.quotes = {"y": None, "n": None}
            if st.orphan_side is not None:
                won = (self.result == "yes") == (st.orphan_side == "y")
                pnl_c = (
                    (10000 if won else 0) - st.orphan_entry
                ) / 100.0
                self._record_cycle(
                    st,
                    self.last_ts,
                    pnl_c,
                    paired=False,
                    exit_reason="carried_settlement",
                )
                st.carried += 1
                self._clear_orphan(st)


def run_day(args):
    date, policies_raw = args
    policies = [Policy(**row) for row in policies_raw]
    meta = load_meta()
    l2_paths = [p for p in glob.glob(L2_GLOB, recursive=True) if f"date={date}" in p]
    tr_paths = [p for p in glob.glob(TR_GLOB, recursive=True) if f"date={date}" in p]
    if not l2_paths:
        raise RuntimeError(f"no L2 paths for {date}")
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    trades_by_market = defaultdict(list)
    if tr_paths:
        rows = con.execute(
            """
            SELECT market_ticker, ts_utc, yes_price_e4, count_e4, taker_side
            FROM read_csv(?, header=true, union_by_name=true,
                          types={'taker_side':'VARCHAR'})
            WHERE series_ticker='KXBTC15M'
            ORDER BY market_ticker, ts_utc
            """,
            [tr_paths],
        ).fetchall()
        for ticker, ts, px, qty, taker in rows:
            if px is not None:
                trades_by_market[ticker].append(
                    (int(ts), int(px), int(qty or 0), taker)
                )
    cur = con.execute(
        """
        SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR)
        FROM read_parquet(?, union_by_name=true)
        ORDER BY market_ticker, ts_utc, ws_seq
        """,
        [l2_paths],
    )
    merged = {
        p.name: {
            "cycles": [],
            "admissions": [],
            "placed": 0,
            "fills": 0,
            "paired": 0,
            "flattened": 0,
            "carried": 0,
            "zone_cancel_orders": 0,
            "stop_cancel_orders": 0,
            "fills_during_cancel": 0,
            "exit_replacements": 0,
            "bugs": [],
        }
        for p in policies
    }
    sim = None

    def harvest(market_sim):
        if market_sim is None:
            return
        market_sim.finalize()
        for name, st in market_sim.states.items():
            out = merged[name]
            out["cycles"].extend(st.cycles)
            out["admissions"].extend(st.admissions)
            for key in (
                "placed",
                "fills",
                "paired",
                "flattened",
                "carried",
                "zone_cancel_orders",
                "stop_cancel_orders",
                "fills_during_cancel",
                "exit_replacements",
            ):
                out[key] += getattr(st, key)
            out["bugs"].extend(st.bugs)

    while True:
        rows = cur.fetchmany(300_000)
        if not rows:
            break
        for ticker, ts, mtype, side, px, delta, yl, nl in rows:
            ts = int(ts)
            if sim is None or sim.ticker != ticker:
                harvest(sim)
                close_us, result = meta.get(ticker, (0, None))
                sim = Z3Market(
                    ticker,
                    close_us,
                    result,
                    trades_by_market.get(ticker, []),
                    policies,
                )
            yes_levels = no_levels = None
            if mtype == "snapshot":
                try:
                    yes_levels = json.loads(yl) if yl else []
                    no_levels = json.loads(nl) if nl else []
                except Exception:
                    yes_levels, no_levels = [], []
            sim.on_book(ts, mtype, side, px, delta, yes_levels, no_levels)
    harvest(sim)
    con.close()
    return date, merged


def run_train(policies):
    raw = {p.name: None for p in policies}
    with mp.Pool(processes=3) as pool:
        day_results = pool.map(
            run_day,
            [(date, [asdict(p) for p in policies]) for date in TRAIN_DATES],
        )
    for policy in policies:
        merged = {
            "cycles": [],
            "admissions": [],
            "placed": 0,
            "fills": 0,
            "paired": 0,
            "flattened": 0,
            "carried": 0,
            "zone_cancel_orders": 0,
            "stop_cancel_orders": 0,
            "fills_during_cancel": 0,
            "exit_replacements": 0,
            "bugs": [],
        }
        for _date, day in day_results:
            part = day[policy.name]
            merged["cycles"].extend(part["cycles"])
            merged["admissions"].extend(part["admissions"])
            for key in (
                "placed",
                "fills",
                "paired",
                "flattened",
                "carried",
                "zone_cancel_orders",
                "stop_cancel_orders",
                "fills_during_cancel",
                "exit_replacements",
            ):
                merged[key] += part[key]
            merged["bugs"].extend(part["bugs"])
        raw[policy.name] = merged
    return raw


def metric_block(name, raw):
    cycles = raw["cycles"]
    paired_rows = [r for r in cycles if r["paired"]]
    orphan_rows = [r for r in cycles if not r["paired"]]
    n = len(cycles)
    paired = len(paired_rows)
    markets = len({r["market"] for r in cycles})
    pair_gain = (
        statistics.fmean(r["pnl_c"] for r in paired_rows) if paired_rows else None
    )
    orphan_loss = (
        statistics.fmean(r["pnl_c"] for r in orphan_rows) if orphan_rows else None
    )
    net = statistics.fmean(r["pnl_c"] for r in cycles) if cycles else None
    q_star = None
    if pair_gain is not None and orphan_loss is not None and pair_gain > 0 > orphan_loss:
        q_star = abs(orphan_loss) / (pair_gain + abs(orphan_loss))
    lcb = wilson_lcb(paired, n)
    ci = cluster_boot_ci(
        cycles,
        int(hashlib.sha256(name.encode()).hexdigest()[:12], 16),
    )
    block = {
        "cycles": n,
        "markets": markets,
        "paired": paired,
        "orphans": len(orphan_rows),
        "completion": None if not n else round(paired / n, 4),
        "completion_wilson_lcb95": None if lcb is None else round(lcb, 4),
        "break_even_completion": None if q_star is None else round(q_star, 4),
        "pair_gain_c": None if pair_gain is None else round(pair_gain, 4),
        "orphan_loss_c": None if orphan_loss is None else round(orphan_loss, 4),
        "net_c_per_cycle": None if net is None else round(net, 4),
        "ci95_market_boot": ci,
        "mean_capital_time_s": None
        if not cycles
        else round(
            statistics.fmean(
                r["capital_time_s"]
                for r in cycles
                if r["capital_time_s"] is not None
            ),
            4,
        ),
        "admissions": len(raw["admissions"]),
        "placed": raw["placed"],
        "fills": raw["fills"],
        "flattened": raw["flattened"],
        "carried": raw["carried"],
        "zone_cancel_orders": raw["zone_cancel_orders"],
        "stop_cancel_orders": raw["stop_cancel_orders"],
        "fills_during_cancel": raw["fills_during_cancel"],
        "exit_replacements": raw["exit_replacements"],
        "bugs": raw["bugs"][:20],
    }
    block["strict_pass"] = bool(
        n >= 30
        and markets >= 10
        and q_star is not None
        and lcb is not None
        and lcb > q_star
        and ci[0] is not None
        and ci[0] > 0
        and not raw["bugs"]
    )
    return block


def rank_names(metrics):
    def key(name):
        m = metrics[name]
        lo = m["ci95_market_boot"][0]
        net = m["net_c_per_cycle"]
        return (
            -math.inf if lo is None else lo,
            -math.inf if net is None else net,
            m["markets"],
            m["cycles"],
        )

    return sorted(metrics, key=key, reverse=True)


def phase_a_policies():
    return [
        Policy(name="ttl5", ttl_s=5),
        Policy(name="ttl15", ttl_s=15),
        Policy(name="ttl30", ttl_s=30),
        Policy(name="ttl60", ttl_s=60),
        Policy(name="dist1_ttl60", ttl_s=60, distance_stop_c=1),
        Policy(name="dist2_ttl60", ttl_s=60, distance_stop_c=2),
        Policy(name="dist3_ttl60", ttl_s=60, distance_stop_c=3),
        Policy(name="cost101_ttl60", ttl_s=60, cost_stop_c=101),
        Policy(name="cost102_ttl60", ttl_s=60, cost_stop_c=102),
        Policy(name="cost103_ttl60", ttl_s=60, cost_stop_c=103),
    ]


def phase_b_policies(frozen_exit, thresholds):
    common = {
        "ttl_s": frozen_exit.ttl_s,
        "distance_stop_c": frozen_exit.distance_stop_c,
        "cost_stop_c": frozen_exit.cost_stop_c,
        "phase": "B_admission",
    }
    return [
        Policy(name="nogate", gate_kind="none", **common),
        Policy(
            name="eta_q20",
            gate_kind="eta",
            eta_max_s=thresholds["eta_q20_s"],
            **common,
        ),
        Policy(
            name="eta_q40",
            gate_kind="eta",
            eta_max_s=thresholds["eta_q40_s"],
            **common,
        ),
        Policy(
            name="eta_q60",
            gate_kind="eta",
            eta_max_s=thresholds["eta_q60_s"],
            **common,
        ),
        Policy(
            name="eta_q40_flow_q20",
            gate_kind="eta_flow",
            eta_max_s=thresholds["eta_q40_s"],
            min_flow60=thresholds["flow_q20"],
            **common,
        ),
        Policy(
            name="eta_q60_flow_q40",
            gate_kind="eta_flow",
            eta_max_s=thresholds["eta_q60_s"],
            min_flow60=thresholds["flow_q40"],
            **common,
        ),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--raw-out", default=RAW_OUT)
    args = parser.parse_args()

    phase_a = phase_a_policies()
    print("PHASE A TRAIN-only, 10 preregistered exit policies", flush=True)
    raw_a = run_train(phase_a)
    metrics_a = {p.name: metric_block(p.name, raw_a[p.name]) for p in phase_a}
    if any(raw_a[p.name]["bugs"] for p in phase_a):
        raise RuntimeError("invariant bug in phase A; report void")
    ranking_a = rank_names(metrics_a)
    frozen_exit_name = ranking_a[0]
    frozen_exit = next(p for p in phase_a if p.name == frozen_exit_name)
    print(
        json.dumps(
            [
                {
                    "rank": i + 1,
                    "name": n,
                    "n": metrics_a[n]["cycles"],
                    "markets": metrics_a[n]["markets"],
                    "net": metrics_a[n]["net_c_per_cycle"],
                    "ci": metrics_a[n]["ci95_market_boot"],
                }
                for i, n in enumerate(ranking_a)
            ],
            indent=1,
        ),
        flush=True,
    )
    print(f"FROZEN EXIT: {frozen_exit_name}", flush=True)

    baseline_admissions = raw_a["ttl60"]["admissions"]
    eligible = [
        row
        for row in baseline_admissions
        if row["history60"] and math.isfinite(row["max_eta_s"])
    ]
    eta = [row["max_eta_s"] for row in eligible]
    flow = [row["min_flow60"] for row in eligible]
    thresholds = {
        "source_policy": "ttl60",
        "source_admissions": len(baseline_admissions),
        "eligible_history60_finite_eta": len(eligible),
        "eta_q20_s": quantile_linear(eta, 0.20),
        "eta_q40_s": quantile_linear(eta, 0.40),
        "eta_q60_s": quantile_linear(eta, 0.60),
        "flow_q20": quantile_linear(flow, 0.20),
        "flow_q40": quantile_linear(flow, 0.40),
    }
    print("TRAIN thresholds:", json.dumps(thresholds, indent=1), flush=True)

    phase_b = phase_b_policies(frozen_exit, thresholds)
    print("PHASE B TRAIN-only, 6 preregistered admission policies", flush=True)
    raw_b = run_train(phase_b)
    metrics_b = {p.name: metric_block(p.name, raw_b[p.name]) for p in phase_b}
    if any(raw_b[p.name]["bugs"] for p in phase_b):
        raise RuntimeError("invariant bug in phase B; report void")
    ranking_b = rank_names(metrics_b)
    strict = [name for name in ranking_b if metrics_b[name]["strict_pass"]]
    frozen_final = strict[0] if strict else None
    print(
        json.dumps(
            [
                {
                    "rank": i + 1,
                    "name": n,
                    "n": metrics_b[n]["cycles"],
                    "markets": metrics_b[n]["markets"],
                    "net": metrics_b[n]["net_c_per_cycle"],
                    "ci": metrics_b[n]["ci95_market_boot"],
                    "lcb": metrics_b[n]["completion_wilson_lcb95"],
                    "q_star": metrics_b[n]["break_even_completion"],
                    "strict": metrics_b[n]["strict_pass"],
                }
                for i, n in enumerate(ranking_b)
            ],
            indent=1,
        ),
        flush=True,
    )

    script_sha = hashlib.sha256(open(__file__, "rb").read()).hexdigest()
    report = {
        "schema": "z3-preregistered-train-v1",
        "host": "ubuntu@3.130.232.109",
        "split": {"TRAIN": list(TRAIN_DATES), "VALIDATE": ["2026-07-23"]},
        "validate_touched": False,
        "scope": {
            "zone": "Z3 only: 120<=TTE<300 and (mid<20 or mid>80)",
            "clip_contracts": CLIP_CT,
            "pair_lock_max_c": 99,
            "latency_ms": LAT_US / 1000,
            "features": [
                "past-60s executable taker flow at current YES/NO touch",
                "current displayed queue depth at both touches",
                "queue-clear ETA=max((depth+1)/(flow/60))",
                "current price-derived midpoint and TTE for Z3",
            ],
            "candidate_count": 16,
            "search_shape": "10 exit policies, freeze one; 6 admissions under frozen exit",
        },
        "phase_A_exit": {
            "policies": [asdict(p) for p in phase_a],
            "ranking": ranking_a,
            "metrics": metrics_a,
            "frozen_unique_exit": frozen_exit_name,
            "selection_rule": "max CI lower bound, then net, markets, cycles",
            "ev_wait_reference": "ttl60",
        },
        "train_thresholds": thresholds,
        "phase_B_admission": {
            "policies": [asdict(p) for p in phase_b],
            "ranking": ranking_b,
            "metrics": metrics_b,
            "strict_rule": (
                "cycles>=30, markets>=10, completion Wilson LCB95 > "
                "break-even completion, market-cluster bootstrap CI95 lower>0"
            ),
            "frozen_unique_final": frozen_final,
        },
        "source": {
            "experiment_sha256": script_sha,
            "base_replay_sha256": SOURCE_BASE_SHA,
            "catalog": f"{SNAP_DIR}/shards/KXBTC15M.json",
            "l2_glob": L2_GLOB,
            "trades_glob": TR_GLOB,
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    raw = {
        "schema": "z3-preregistered-train-raw-v1",
        "validate_touched": False,
        "phase_A": raw_a,
        "phase_B": raw_b,
    }
    with open(args.raw_out, "w") as fh:
        json.dump(raw, fh, separators=(",", ":"), allow_nan=False)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, allow_nan=False)
    print(f"REPORT {args.out}", flush=True)
    print(f"RAW {args.raw_out}", flush=True)
    print(f"SHA {script_sha}", flush=True)
    print(f"FINAL_FROZEN {frozen_final}", flush=True)
    print("VALIDATE_TOUCHED false", flush=True)


if __name__ == "__main__":
    main()
