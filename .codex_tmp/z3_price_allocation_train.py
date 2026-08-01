#!/usr/bin/env python3
"""Pre-registered TRAIN-only Z3 quote-price allocation replay.

Exactly five arms are compared once on 2026-07-20..22.  Every arm shares the
already-frozen dist2+TTL60 orphan exit, clip=1, pair cost <=99c and 60ms cancel
latency.  July 20--23 are all discovery-contaminated by earlier work; this run
opens only July 20--22 and cannot make a validation claim.  A future,
post-seal forward holdout is required.

The allocation specification is embedded in ``PREREGISTRATION`` below and is
also sealed in a separate JSON receipt before the TRAIN command is launched.
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
OUT = os.environ.get(
    "Z3_PRICE_TRAIN_OUT", "/tmp/z3_price_allocation_train_report.json"
)
RAW_OUT = os.environ.get(
    "Z3_PRICE_TRAIN_RAW_OUT", "/tmp/z3_price_allocation_train_raw.json"
)
PREREG_PATH = os.environ.get(
    "Z3_PRICE_PREREG", "/tmp/z3_price_allocation_prereg.json"
)

PREREGISTRATION = {
    "schema": "z3-price-allocation-prereg-v1",
    "candidate_count": 5,
    "train_dates": list(TRAIN_DATES),
    "historical_split_status": {
        "discovery_contaminated": [
            "2026-07-20",
            "2026-07-21",
            "2026-07-22",
            "2026-07-23"
        ],
        "this_run_reads": list(TRAIN_DATES),
        "this_run_does_not_reuse_but_prior_work_opened": ["2026-07-23"],
        "final_validation": "post-model-and-code-seal forward holdout only"
    },
    "common": {
        "zone": "120<=TTE<300 and (mid<20c or mid>80c)",
        "clip_contracts": 1,
        "pair_cost_max_c": 99,
        "orphan_exit": "counterpart >=2c behind current touch, otherwise TTL60",
        "cancel_ack_latency_ms": 60,
        "fill_queue_ahead": "current displayed quantity at exact quote level only",
        "fill_test": "strict trade-through: cumulative eligible taker volume > ahead+clip",
        "tick_rule": "<10c or >90c: 0.1c; otherwise: 1c; apply sequentially from current price",
        "post_only": "candidate bid must be strictly below corresponding current ask",
    },
    "arms": {
        "P0_TOUCH": "quote both current touches; no 60s-support requirement",
        "P1_SHIFT1": (
            "require complete60s and finite two-sided touch ETA; improve slower "
            "touch leg one legal tick and retreat faster leg one legal tick"
        ),
        "P2_SHIFT2": (
            "same as P1, two sequential legal ticks on each leg"
        ),
        "P3_SPEND1": (
            "require P1 support; if pair slack funds a legal one-tick slower-leg "
            "improvement, keep fast leg unchanged; otherwise use P1_SHIFT1"
        ),
        "P4_ETA_ENUM1": (
            "enumerate each leg {-1,0,+1} legal tick; require post-only and "
            "sum<=99c; predicted ETA numerator=all displayed barrier depth at "
            "prices better than candidate + same-price ahead + clip; "
            "denominator=past60 executable taker flow to candidate/60; minimize "
            "max ETA, then pair sum, then abs(log ETAy-log ETAn), then "
            "lexicographic (yes_e4,no_e4)"
        ),
    },
    "support_rule": "P1-P4 SKIP without complete60s and finite positive two-sided ETA support",
    "slower_tie_rule": "YES is the slower leg when touch ETA is exactly tied",
    "selection_gate": (
        "cycles>=30, markets>=10, completion Wilson LCB95>break-even "
        "completion, market-cluster bootstrap CI95 lower>0"
    ),
}


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
    distance_stop_c: float = 2.0
    allocation: str = "touch"
    shift_ticks: int = 0


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
    eligible_decisions: int = 0
    skipped_support: int = 0
    skipped_post_only: int = 0
    skipped_pair_cost: int = 0
    allocation_counts: dict = field(default_factory=dict)
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
        self.flow_cache = {}
        self.first_event_ts = None
        self.last_ts = None

    def _trim_flow(self, ts):
        cutoff = ts - FLOW_WINDOW_US
        while self.recent_trades and self.recent_trades[0][0] < cutoff:
            self.recent_trades.popleft()

    def _flow_to(self, side, candidate_e4):
        key = (side, candidate_e4)
        if key in self.flow_cache:
            return self.flow_cache[key]
        flow_e4 = 0
        for _tts, yes_px, qty, taker in self.recent_trades:
            if side == "y":
                if taker == "no" and yes_px <= candidate_e4:
                    flow_e4 += qty
            elif taker == "yes" and 10000 - yes_px <= candidate_e4:
                flow_e4 += qty
        flow = flow_e4 / 10000.0
        self.flow_cache[key] = flow
        return flow

    def _eta_detail(self, side, candidate_e4):
        """Prediction only; barrier depth is never copied into fill ahead."""
        flow = self._flow_to(side, candidate_e4)
        book = self.books[side]
        barrier = sum(
            (volume or 0) for price, volume in book.items() if price > candidate_e4
        ) / 10000.0
        same = (book.get(candidate_e4, 0) or 0) / 10000.0
        numerator = barrier + same + CLIP_CT
        eta = math.inf if flow <= 0 else numerator / (flow / 60.0)
        return {
            "flow60": flow,
            "barrier_depth": barrier,
            "same_level_ahead": same,
            "eta_s": eta,
        }

    def _features(self, ts, yb, nb):
        self._trim_flow(ts)
        self.flow_cache = {}
        yes = self._eta_detail("y", yb)
        no = self._eta_detail("n", nb)
        mid_c = (yb + (10000 - nb)) / 200.0
        return {
            "history60": bool(
                self.first_event_ts is not None
                and ts - self.first_event_ts >= FLOW_WINDOW_US
            ),
            "flow_y60": yes["flow60"],
            "flow_n60": no["flow60"],
            "min_flow60": min(yes["flow60"], no["flow60"]),
            "depth_y": yes["same_level_ahead"],
            "depth_n": no["same_level_ahead"],
            "eta_y_s": yes["eta_s"],
            "eta_n_s": no["eta_s"],
            "max_eta_s": max(yes["eta_s"], no["eta_s"]),
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
        return yb, nb, self._features(ts, yb, nb)

    @staticmethod
    def _tick_e4(price_e4):
        return 10 if price_e4 < 1000 or price_e4 > 9000 else 100

    @classmethod
    def _move_ticks(cls, price_e4, direction, count):
        price = int(price_e4)
        for _ in range(count):
            price += direction * cls._tick_e4(price)
            if not (0 < price < 10000):
                return None
        return price

    @staticmethod
    def _touch_support(features):
        return bool(
            features["history60"]
            and math.isfinite(features["eta_y_s"])
            and math.isfinite(features["eta_n_s"])
            and features["eta_y_s"] > 0
            and features["eta_n_s"] > 0
        )

    @staticmethod
    def _candidate_valid(ypx, npx, yb, nb):
        if ypx is None or npx is None:
            return False, "post_only"
        yes_ask = 10000 - nb
        no_ask = 10000 - yb
        if not (0 < ypx < yes_ask and 0 < npx < no_ask):
            return False, "post_only"
        if ypx + npx > PAIR_LOCK_E4:
            return False, "pair_cost"
        return True, None

    def _allocation_details(self, ypx, npx):
        yes = self._eta_detail("y", ypx)
        no = self._eta_detail("n", npx)
        return {
            "allocated_yes_e4": ypx,
            "allocated_no_e4": npx,
            "allocated_pair_sum_c": (ypx + npx) / 100.0,
            "pred_eta_y_s": yes["eta_s"],
            "pred_eta_n_s": no["eta_s"],
            "pred_max_eta_s": max(yes["eta_s"], no["eta_s"]),
            "pred_flow_y60": yes["flow60"],
            "pred_flow_n60": no["flow60"],
            "pred_barrier_y": yes["barrier_depth"],
            "pred_barrier_n": no["barrier_depth"],
            "actual_ahead_y": yes["same_level_ahead"],
            "actual_ahead_n": no["same_level_ahead"],
        }

    def _shift_allocation(self, features, yb, nb, ticks):
        slow = "y" if features["eta_y_s"] >= features["eta_n_s"] else "n"
        if slow == "y":
            ypx = self._move_ticks(yb, +1, ticks)
            npx = self._move_ticks(nb, -1, ticks)
        else:
            ypx = self._move_ticks(yb, -1, ticks)
            npx = self._move_ticks(nb, +1, ticks)
        return slow, ypx, npx

    def _allocate(self, policy, st, yb, nb, features):
        st.eligible_decisions += 1
        if policy.allocation == "touch":
            ok, why = self._candidate_valid(yb, nb, yb, nb)
            if not ok:
                if why == "pair_cost":
                    st.skipped_pair_cost += 1
                else:
                    st.skipped_post_only += 1
                return None
            details = self._allocation_details(yb, nb)
            details["allocation_variant"] = "touch"
            return details

        if not self._touch_support(features):
            st.skipped_support += 1
            return None

        if policy.allocation in ("shift", "spend"):
            slow, ypx, npx = self._shift_allocation(
                features, yb, nb, policy.shift_ticks
            )
            variant = f"shift{policy.shift_ticks}_{slow}_slow"
            if policy.allocation == "spend":
                improved = self._move_ticks(
                    yb if slow == "y" else nb, +1, 1
                )
                spend_y = improved if slow == "y" else yb
                spend_n = improved if slow == "n" else nb
                spend_ok, _spend_why = self._candidate_valid(
                    spend_y, spend_n, yb, nb
                )
                if spend_ok:
                    ypx, npx = spend_y, spend_n
                    variant = f"spend1_{slow}_slow"
                else:
                    slow, ypx, npx = self._shift_allocation(
                        features, yb, nb, 1
                    )
                    variant = f"spend1_fallback_shift1_{slow}_slow"
            ok, why = self._candidate_valid(ypx, npx, yb, nb)
            if not ok:
                if why == "pair_cost":
                    st.skipped_pair_cost += 1
                else:
                    st.skipped_post_only += 1
                return None
            details = self._allocation_details(ypx, npx)
            details["allocation_variant"] = variant
            details["touch_slower_leg"] = slow
            return details

        if policy.allocation == "eta_enum":
            candidates = []
            for dy in (-1, 0, 1):
                ypx = yb if dy == 0 else self._move_ticks(yb, dy, 1)
                for dn in (-1, 0, 1):
                    npx = nb if dn == 0 else self._move_ticks(nb, dn, 1)
                    ok, _why = self._candidate_valid(ypx, npx, yb, nb)
                    if not ok:
                        continue
                    details = self._allocation_details(ypx, npx)
                    ey = details["pred_eta_y_s"]
                    en = details["pred_eta_n_s"]
                    if not (
                        math.isfinite(ey)
                        and math.isfinite(en)
                        and ey > 0
                        and en > 0
                    ):
                        continue
                    score = (
                        max(ey, en),
                        ypx + npx,
                        abs(math.log(ey) - math.log(en)),
                        ypx,
                        npx,
                    )
                    candidates.append((score, dy, dn, details))
            if not candidates:
                st.skipped_support += 1
                return None
            _score, dy, dn, details = min(candidates, key=lambda row: row[0])
            details["allocation_variant"] = f"enum_dy{dy:+d}_dn{dn:+d}"
            details["enum_score"] = list(_score)
            return details
        raise AssertionError(f"unknown allocation {policy.allocation}")

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

    def _admit(self, policy, st, ts, features, allocation):
        row = {
            "date": dt.datetime.fromtimestamp(
                ts / 1_000_000, dt.timezone.utc
            ).date().isoformat(),
            "market": self.ticker,
            "ts": ts,
            **features,
            **allocation,
        }
        st.admissions.append(row)
        st.admission = row
        variant = allocation["allocation_variant"]
        st.allocation_counts[variant] = st.allocation_counts.get(variant, 0) + 1
        self._place_quote(st, "y", allocation["allocated_yes_e4"], ts)
        self._place_quote(st, "n", allocation["allocated_no_e4"], ts)

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
                    "allocated_yes_e4",
                    "allocated_no_e4",
                    "allocated_pair_sum_c",
                    "pred_eta_y_s",
                    "pred_eta_n_s",
                    "pred_max_eta_s",
                    "pred_flow_y60",
                    "pred_flow_n60",
                    "pred_barrier_y",
                    "pred_barrier_n",
                    "actual_ahead_y",
                    "actual_ahead_n",
                    "allocation_variant",
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
            allocation = self._allocate(policy, st, yb, nb, features)
            if allocation is not None:
                self._admit(policy, st, ts, features, allocation)

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
    # Construct date-scoped globs so the reserved 07-23 files are not opened
    # (or even enumerated) by this TRAIN-only process.
    l2_root = L2_GLOB.split("/**", 1)[0]
    tr_root = TR_GLOB.split("/**", 1)[0]
    l2_paths = glob.glob(f"{l2_root}/subcategory=*/date={date}/*.parquet")
    tr_paths = glob.glob(f"{tr_root}/subcategory=*/date={date}/*.csv.gz")
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
            "eligible_decisions": 0,
            "skipped_support": 0,
            "skipped_post_only": 0,
            "skipped_pair_cost": 0,
            "allocation_counts": {},
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
                "eligible_decisions",
                "skipped_support",
                "skipped_post_only",
                "skipped_pair_cost",
            ):
                out[key] += getattr(st, key)
            for variant, count in st.allocation_counts.items():
                out["allocation_counts"][variant] = (
                    out["allocation_counts"].get(variant, 0) + count
                )
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
            "eligible_decisions": 0,
            "skipped_support": 0,
            "skipped_post_only": 0,
            "skipped_pair_cost": 0,
            "allocation_counts": {},
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
                "eligible_decisions",
                "skipped_support",
                "skipped_post_only",
                "skipped_pair_cost",
            ):
                merged[key] += part[key]
            for variant, count in part["allocation_counts"].items():
                merged["allocation_counts"][variant] = (
                    merged["allocation_counts"].get(variant, 0) + count
                )
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
        "eligible_decisions": raw["eligible_decisions"],
        "skipped_support": raw["skipped_support"],
        "skipped_post_only": raw["skipped_post_only"],
        "skipped_pair_cost": raw["skipped_pair_cost"],
        "allocation_counts": dict(sorted(raw["allocation_counts"].items())),
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


def policies():
    return [
        Policy(name="P0_TOUCH", allocation="touch"),
        Policy(name="P1_SHIFT1", allocation="shift", shift_ticks=1),
        Policy(name="P2_SHIFT2", allocation="shift", shift_ticks=2),
        Policy(name="P3_SPEND1", allocation="spend", shift_ticks=1),
        Policy(name="P4_ETA_ENUM1", allocation="eta_enum", shift_ticks=1),
    ]


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--raw-out", default=RAW_OUT)
    parser.add_argument("--prereg", default=PREREG_PATH)
    args = parser.parse_args()

    script_sha = hashlib.sha256(open(__file__, "rb").read()).hexdigest()
    prereg_bytes = open(args.prereg, "rb").read()
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()
    prereg = json.loads(prereg_bytes)
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("pre-registration script hash mismatch; run void")
    if prereg.get("spec") != PREREGISTRATION:
        raise RuntimeError("pre-registration spec mismatch; run void")

    arms = policies()
    if [p.name for p in arms] != list(PREREGISTRATION["arms"]):
        raise RuntimeError("pre-registration arm order mismatch; run void")
    print("TRAIN-only, exactly 5 pre-registered price-allocation arms", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    raw = run_train(arms)
    metrics = {p.name: metric_block(p.name, raw[p.name]) for p in arms}
    if any(raw[p.name]["bugs"] for p in arms):
        raise RuntimeError("invariant bug; report void")
    ranking = rank_names(metrics)
    qualified = [name for name in ranking if metrics[name]["strict_pass"]]
    discovery_candidate = qualified[0] if qualified else None
    print(
        json.dumps(
            [
                {
                    "rank": i + 1,
                    "name": n,
                    "n": metrics[n]["cycles"],
                    "markets": metrics[n]["markets"],
                    "paired": metrics[n]["paired"],
                    "orphans": metrics[n]["orphans"],
                    "completion": metrics[n]["completion"],
                    "net": metrics[n]["net_c_per_cycle"],
                    "ci": metrics[n]["ci95_market_boot"],
                    "lcb": metrics[n]["completion_wilson_lcb95"],
                    "q_star": metrics[n]["break_even_completion"],
                    "strict": metrics[n]["strict_pass"],
                }
                for i, n in enumerate(ranking)
            ],
            indent=1,
        ),
        flush=True,
    )

    report = {
        "schema": "z3-price-allocation-train-v1",
        "host": "ubuntu@3.130.232.109",
        "split": {
            "THIS_RUN_DISCOVERY": list(TRAIN_DATES),
            "ALL_DISCOVERY_CONTAMINATED": [
                "2026-07-20",
                "2026-07-21",
                "2026-07-22",
                "2026-07-23",
            ],
            "PRIOR_OPENED_NOT_REUSED_THIS_RUN": ["2026-07-23"],
            "FINAL_VALIDATION": "post-model-and-code-seal forward holdout only",
        },
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "preregistration": PREREGISTRATION,
        "policies": [asdict(p) for p in arms],
        "ranking": ranking,
        "metrics": metrics,
        "strict_qualified": qualified,
        "discovery_candidate": discovery_candidate,
        "decision": (
            "DISCOVERY_CANDIDATE_AWAIT_FORWARD_HOLDOUT"
            if discovery_candidate
            else "NO_CANDIDATE"
        ),
        "deployable": False,
        "required_next_evidence": (
            "model-and-code seal followed by genuinely forward holdout"
        ),
        "selection_rule": "max CI lower bound, then net, markets, cycles among strict passes",
        "source": {
            "experiment_sha256": script_sha,
            "base_replay_sha256": SOURCE_BASE_SHA,
            "preregistration_path": args.prereg,
            "preregistration_sha256": prereg_sha,
            "catalog": f"{SNAP_DIR}/shards/KXBTC15M.json",
            "l2_glob": L2_GLOB,
            "trades_glob": TR_GLOB,
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    raw_artifact = {
        "schema": "z3-price-allocation-train-raw-v1",
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "nonfinite_encoding": "null means positive infinity from zero executable flow",
        "arms": raw,
    }
    with open(args.out, "w") as fh:
        json.dump(json_safe(report), fh, indent=2, allow_nan=False)
    with open(args.raw_out, "w") as fh:
        json.dump(
            json_safe(raw_artifact),
            fh,
            separators=(",", ":"),
            allow_nan=False,
        )
    print(f"REPORT {args.out}", flush=True)
    print(f"RAW {args.raw_out}", flush=True)
    print(f"SHA {script_sha}", flush=True)
    print(f"DISCOVERY_CANDIDATE {discovery_candidate}", flush=True)
    print(
        "DECISION "
        + (
            "DISCOVERY_CANDIDATE_AWAIT_FORWARD_HOLDOUT"
            if discovery_candidate
            else "NO_CANDIDATE"
        ),
        flush=True,
    )
    print("2026_07_23_READ_THIS_RUN false", flush=True)
    print("HISTORICAL_VALIDATION_CLAIM false", flush=True)
    print("FINAL_VALIDATION forward_holdout_after_model_and_code_seal", flush=True)


if __name__ == "__main__":
    main()
