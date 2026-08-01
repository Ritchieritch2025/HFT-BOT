#!/usr/bin/env python3
"""Sealed DISCOVERY-only full-grid price-allocation replay, round 2.

This experiment reuses the event/fill/cancel simulator from the already sealed
``z3_price_allocation_train.py`` exactly once for 2026-07-20..22.  It changes
only quote-price allocation, legal-grid handling, and reporting.  July 23 is
discovery-contaminated but is not opened by this run.  No result is deployable
without a later, genuinely forward holdout.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
from dataclasses import asdict

import numpy as np


TRAIN_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
BASE_SHA256 = (
    "81c35b2982db9d892fb2c97d55f572681d5a60631b6367023e9330cdfc0feb5b"
)
BASE_PATH = os.environ.get(
    "Z3_ROUND2_BASE", "/tmp/z3_price_allocation_train.py"
)
OUT = os.environ.get(
    "Z3_ROUND2_OUT", "/tmp/z3_price_allocation_round2_report.json"
)
RAW_OUT = os.environ.get(
    "Z3_ROUND2_RAW_OUT", "/tmp/z3_price_allocation_round2_raw.json"
)
PREREG_PATH = os.environ.get(
    "Z3_ROUND2_PREREG", "/tmp/z3_price_allocation_round2_prereg.json"
)

PAIR_LOCK_E4 = 9_900
HORIZON_S = 60.0
FROZEN_ORPHAN_LOSS_C = 3.5
LEGAL_PRICES_E4 = tuple(
    list(range(10, 1_000, 10))
    + list(range(1_000, 9_001, 100))
    + list(range(9_010, 10_000, 10))
)
LEGAL_PRICE_SET = frozenset(LEGAL_PRICES_E4)

PREREGISTRATION = {
    "schema": "z3-price-allocation-round2-prereg-v1",
    "candidate_count": 4,
    "train_dates": list(TRAIN_DATES),
    "historical_split_status": {
        "all_discovery_contaminated": [
            "2026-07-20",
            "2026-07-21",
            "2026-07-22",
            "2026-07-23",
        ],
        "this_run_reads": list(TRAIN_DATES),
        "this_run_never_opens": ["2026-07-23"],
        "claim": "DISCOVERY_ONLY",
        "final_validation": "post-model-and-code-seal forward holdout only",
    },
    "sealed_base": {
        "script": "z3_price_allocation_train.py",
        "sha256": BASE_SHA256,
        "reuse": (
            "one shared three-day event replay; allocation subclass only; "
            "engine code is not imported or modified"
        ),
    },
    "common": {
        "zone": "120<=TTE<300 and (mid<20c or mid>80c)",
        "support": (
            "complete trailing 60s history and finite positive two-sided "
            "touch ETA; every arm skips otherwise"
        ),
        "clip_contracts": 1,
        "pair_cost_max_c": 99,
        "post_only": (
            "each candidate bid is a real legal-grid price strictly below "
            "its corresponding current ask"
        ),
        "legal_grid": (
            "0.1c increments on [0.1c,9.9c], 1c increments on [10c,90c], "
            "0.1c increments on [90.1c,99.9c]; boundary predecessor/successor "
            "is 9.9c<->10c and 90c<->90.1c"
        ),
        "orphan_exit": "counterpart >=2c behind current touch, otherwise TTL60",
        "cancel_ack_latency_ms": 60,
        "fill_queue_ahead": "displayed quantity at exact quote level only",
        "fill_test": (
            "strict trade-through: cumulative eligible taker volume "
            "> exact-level ahead + clip"
        ),
        "eta": (
            "(all displayed barrier depth at better prices + same-level "
            "ahead + clip)/(past60 executable taker flow to candidate/60)"
        ),
        "q_model": "q_side=1-exp(-60/ETA_side); q_both=q_yes*q_no",
        "proxy_gain": "G=100-p_yes-p_no cents",
        "proxy_loss_c": FROZEN_ORPHAN_LOSS_C,
        "ev_proxy": "q_both*G-(1-q_both)*3.5c",
    },
    "arms": {
        "A0_COMMON_TOUCH": (
            "after common support, quote both current touches if legal, "
            "post-only, and pair sum<=99c"
        ),
        "A1_SPEND_ALL_SLOW": (
            "keep the faster touch leg unchanged; assign the largest feasible "
            "legal-grid price to the slower touch-ETA leg, spending all usable "
            "pair slack subject to post-only and sum<=99c; YES is slower on tie"
        ),
        "A2_FULL_MINIMAX_ETA": (
            "enumerate the complete Cartesian product of legal post-only "
            "prices with pair sum<=99c and finite positive ETAs; minimize max "
            "ETA; ties choose higher q_both, lower pair sum, lower absolute "
            "log-ETA imbalance, then lower YES price, then lower NO price"
        ),
        "A3_FULL_EV_GATE": (
            "enumerate the same complete feasible legal grid; maximize frozen "
            "EVproxy; ties choose higher q_both, lower max ETA, lower pair "
            "sum, lower absolute log-ETA imbalance, then lower YES price, "
            "then lower NO price; admit only when selected EVproxy>0 strictly"
        ),
    },
    "selection_gate": (
        "cycles>=30 AND markets>=10 AND empirical pair-completion Wilson "
        "LCB95>empirical break-even q* AND market-cluster bootstrap mean-PnL "
        "CI95 lower>0 AND zero invariant bugs"
    ),
    "ranking": (
        "strict passes first by market-cluster CI lower, realized mean PnL, "
        "markets, cycles"
    ),
    "interpretation": (
        "a strict positive is only a DISCOVERY candidate; deployable=false; "
        "forward holdout remains mandatory"
    ),
}


def sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sealed_base():
    actual = sha256_path(BASE_PATH)
    if actual != BASE_SHA256:
        raise RuntimeError(
            f"sealed base hash mismatch: expected {BASE_SHA256}, got {actual}"
        )
    spec = importlib.util.spec_from_file_location("z3_round2_sealed_base", BASE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sealed base replay")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def legal_floor(price_e4):
    idx = bisect.bisect_right(LEGAL_PRICES_E4, int(price_e4)) - 1
    return None if idx < 0 else LEGAL_PRICES_E4[idx]


def legal_predecessor(price_e4):
    idx = bisect.bisect_left(LEGAL_PRICES_E4, int(price_e4)) - 1
    return None if idx < 0 else LEGAL_PRICES_E4[idx]


def legal_move(price_e4, direction, count=1):
    if price_e4 not in LEGAL_PRICE_SET or direction not in (-1, 1):
        return None
    idx = bisect.bisect_left(LEGAL_PRICES_E4, price_e4)
    target = idx + direction * int(count)
    if target < 0 or target >= len(LEGAL_PRICES_E4):
        return None
    return LEGAL_PRICES_E4[target]


def quantile_linear(values, q):
    vals = sorted(float(value) for value in values)
    if not vals:
        return None
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
    rad = z * math.sqrt(
        p * (1.0 - p) / n + z * z / (4.0 * n * n)
    )
    return (center - rad) / den


def cluster_boot_ci(cycles, seed, reps=5_000):
    by_market = {}
    for row in cycles:
        by_market.setdefault(row["market"], []).append(float(row["pnl_c"]))
    markets = sorted(by_market)
    if not markets:
        return [None, None]
    rng = random.Random(seed)
    boots = []
    for _ in range(reps):
        values = []
        for _market in markets:
            values.extend(by_market[rng.choice(markets)])
        boots.append(statistics.fmean(values))
    return [
        round(quantile_linear(boots, 0.025), 4),
        round(quantile_linear(boots, 0.975), 4),
    ]


def make_grid_market(base):
    class FullGridMarket(base.Z3Market):
        """Allocation-only subclass; the base simulator owns all event replay."""

        @staticmethod
        def _tick_e4(price_e4):
            if price_e4 not in LEGAL_PRICE_SET:
                return None
            nxt = legal_move(price_e4, 1)
            return None if nxt is None else nxt - price_e4

        @classmethod
        def _move_ticks(cls, price_e4, direction, count):
            return legal_move(int(price_e4), int(direction), int(count))

        @staticmethod
        def _candidate_valid(ypx, npx, yb, nb):
            if (
                ypx not in LEGAL_PRICE_SET
                or npx not in LEGAL_PRICE_SET
            ):
                return False, "legal_grid"
            yes_ask = 10_000 - nb
            no_ask = 10_000 - yb
            if not (ypx < yes_ask and npx < no_ask):
                return False, "post_only"
            if ypx + npx > PAIR_LOCK_E4:
                return False, "pair_cost"
            return True, None

        def _exit_quote_target(self, st):
            held = st.orphan_side
            exit_side = "n" if held == "y" else "y"
            ceiling = PAIR_LOCK_E4 - st.orphan_entry
            held_best = base.best(self.books[held])
            if held_best is None:
                post_only_cap = LEGAL_PRICES_E4[-1]
            else:
                post_only_cap = legal_predecessor(10_000 - held_best)
                if post_only_cap is None:
                    raise RuntimeError("no legal post-only exit quote")
            cap = min(
                ceiling,
                post_only_cap,
            )
            target = legal_floor(cap)
            if target is None:
                raise RuntimeError("no legal risk-reducing exit quote")
            return exit_side, target

        def _side_table(self, side, ask_e4):
            prices = np.asarray(
                [p for p in LEGAL_PRICES_E4 if p < ask_e4],
                dtype=np.int64,
            )
            if not prices.size:
                return None
            book = self.books[side]
            book_prices = np.asarray(list(book), dtype=np.int64)
            book_volumes = np.asarray(
                [int(book[p] or 0) for p in book], dtype=np.float64
            )
            if book_prices.size:
                barrier = (
                    (
                        book_prices[np.newaxis, :]
                        > prices[:, np.newaxis]
                    )
                    * book_volumes[np.newaxis, :]
                ).sum(axis=1) / 10_000.0
            else:
                barrier = np.zeros(prices.size, dtype=np.float64)
            same = np.asarray(
                [float(book.get(int(p), 0) or 0) / 10_000.0 for p in prices],
                dtype=np.float64,
            )

            executable_prices = []
            executable_qty = []
            for _ts, yes_px, qty, taker in self.recent_trades:
                if side == "y" and taker == "no":
                    executable_prices.append(int(yes_px))
                    executable_qty.append(int(qty or 0))
                elif side == "n" and taker == "yes":
                    executable_prices.append(10_000 - int(yes_px))
                    executable_qty.append(int(qty or 0))
            if executable_prices:
                trade_prices = np.asarray(executable_prices, dtype=np.int64)
                trade_qty = np.asarray(executable_qty, dtype=np.float64)
                flow = (
                    (
                        trade_prices[np.newaxis, :]
                        <= prices[:, np.newaxis]
                    )
                    * trade_qty[np.newaxis, :]
                ).sum(axis=1) / 10_000.0
            else:
                flow = np.zeros(prices.size, dtype=np.float64)
            numerator = barrier + same + 1.0
            eta = np.full(prices.size, np.inf, dtype=np.float64)
            positive = flow > 0
            eta[positive] = numerator[positive] / (flow[positive] / HORIZON_S)
            q = np.zeros(prices.size, dtype=np.float64)
            finite = np.isfinite(eta) & (eta > 0)
            q[finite] = -np.expm1(-HORIZON_S / eta[finite])
            return {
                "prices": prices,
                "flow": flow,
                "barrier": barrier,
                "same": same,
                "eta": eta,
                "q": q,
                "finite": finite,
            }

        def _tables(self, yb, nb):
            key = (
                self.last_ts,
                int(yb),
                int(nb),
                len(self.recent_trades),
                sum(int(row[2] or 0) for row in self.recent_trades),
            )
            cached = getattr(self, "_round2_table_cache", None)
            if cached is not None and cached[0] == key:
                return cached[1], cached[2]
            yes = self._side_table("y", 10_000 - nb)
            no = self._side_table("n", 10_000 - yb)
            self._round2_table_cache = (key, yes, no)
            return yes, no

        @staticmethod
        def _row_index(table, price):
            idx = int(np.searchsorted(table["prices"], int(price)))
            if idx >= len(table["prices"]) or int(table["prices"][idx]) != int(price):
                return None
            return idx

        @staticmethod
        def _mask_keep(mask, values, mode):
            if not np.any(mask):
                return mask
            selected = values[mask]
            target = selected.min() if mode == "min" else selected.max()
            return mask & (values == target)

        def _details(self, yes, no, yi, ni, variant):
            ypx = int(yes["prices"][yi])
            npx = int(no["prices"][ni])
            eta_y = float(yes["eta"][yi])
            eta_n = float(no["eta"][ni])
            q_y = float(yes["q"][yi])
            q_n = float(no["q"][ni])
            q_both = q_y * q_n
            gain_c = 100.0 - (ypx + npx) / 100.0
            proxy_q_star = (
                FROZEN_ORPHAN_LOSS_C
                / (gain_c + FROZEN_ORPHAN_LOSS_C)
            )
            ev_proxy = (
                q_both * gain_c
                - (1.0 - q_both) * FROZEN_ORPHAN_LOSS_C
            )
            return {
                "allocated_yes_e4": ypx,
                "allocated_no_e4": npx,
                "allocated_pair_sum_c": (ypx + npx) / 100.0,
                "pred_eta_y_s": eta_y,
                "pred_eta_n_s": eta_n,
                "pred_max_eta_s": max(eta_y, eta_n),
                "pred_flow_y60": float(yes["flow"][yi]),
                "pred_flow_n60": float(no["flow"][ni]),
                "pred_barrier_y": float(yes["barrier"][yi]),
                "pred_barrier_n": float(no["barrier"][ni]),
                "actual_ahead_y": float(yes["same"][yi]),
                "actual_ahead_n": float(no["same"][ni]),
                "pred_q_y_60": q_y,
                "pred_q_n_60": q_n,
                "pred_q_both_60": q_both,
                "proxy_gain_c": gain_c,
                "proxy_q_star": proxy_q_star,
                "selected_ev_proxy_c": ev_proxy,
                "allocation_variant": variant,
            }

        def _full_matrices(self, yb, nb):
            yes, no = self._tables(yb, nb)
            if yes is None or no is None:
                return None
            yp = yes["prices"][:, np.newaxis]
            np_ = no["prices"][np.newaxis, :]
            finite = yes["finite"][:, np.newaxis] & no["finite"][np.newaxis, :]
            valid = finite & ((yp + np_) <= PAIR_LOCK_E4)
            if not np.any(valid):
                return None
            eta_y = yes["eta"][:, np.newaxis]
            eta_n = no["eta"][np.newaxis, :]
            q_both = yes["q"][:, np.newaxis] * no["q"][np.newaxis, :]
            max_eta = np.maximum(eta_y, eta_n)
            pair_sum = yp + np_
            imbalance = np.abs(np.log(eta_y) - np.log(eta_n))
            gain_c = 100.0 - pair_sum / 100.0
            ev = (
                q_both * gain_c
                - (1.0 - q_both) * FROZEN_ORPHAN_LOSS_C
            )
            return {
                "yes": yes,
                "no": no,
                "valid": valid,
                "q_both": q_both,
                "max_eta": max_eta,
                "pair_sum": pair_sum,
                "imbalance": imbalance,
                "ev": ev,
                "yp": np.broadcast_to(yp, valid.shape),
                "np": np.broadcast_to(np_, valid.shape),
            }

        def _choose_matrix(self, matrices, objective):
            mask = matrices["valid"].copy()
            if objective == "minimax":
                order = (
                    ("max_eta", "min"),
                    ("q_both", "max"),
                    ("pair_sum", "min"),
                    ("imbalance", "min"),
                    ("yp", "min"),
                    ("np", "min"),
                )
            elif objective == "ev":
                order = (
                    ("ev", "max"),
                    ("q_both", "max"),
                    ("max_eta", "min"),
                    ("pair_sum", "min"),
                    ("imbalance", "min"),
                    ("yp", "min"),
                    ("np", "min"),
                )
            else:
                raise AssertionError(objective)
            for name, mode in order:
                mask = self._mask_keep(mask, matrices[name], mode)
            coordinates = np.argwhere(mask)
            if len(coordinates) != 1:
                raise RuntimeError(
                    f"non-unique {objective} full-grid selection: "
                    f"{len(coordinates)}"
                )
            return int(coordinates[0][0]), int(coordinates[0][1])

        def _allocate(self, policy, st, yb, nb, features):
            st.eligible_decisions += 1
            if not self._touch_support(features):
                st.skipped_support += 1
                return None
            yes, no = self._tables(yb, nb)
            if yes is None or no is None:
                st.skipped_post_only += 1
                return None
            yi_touch = self._row_index(yes, yb)
            ni_touch = self._row_index(no, nb)
            if yi_touch is None or ni_touch is None:
                st.skipped_post_only += 1
                return None

            if policy.allocation == "common_touch":
                ok, why = self._candidate_valid(yb, nb, yb, nb)
                if not ok:
                    if why == "pair_cost":
                        st.skipped_pair_cost += 1
                    else:
                        st.skipped_post_only += 1
                    return None
                return self._details(
                    yes, no, yi_touch, ni_touch, "common_touch"
                )

            if policy.allocation == "spend_all_slow":
                slow = (
                    "y"
                    if features["eta_y_s"] >= features["eta_n_s"]
                    else "n"
                )
                fast_px = nb if slow == "y" else yb
                slow_touch = yb if slow == "y" else nb
                slow_table = yes if slow == "y" else no
                cap = min(
                    PAIR_LOCK_E4 - fast_px,
                    int(slow_table["prices"][-1]),
                )
                candidate = legal_floor(cap)
                if candidate is None or candidate < slow_touch:
                    st.skipped_pair_cost += 1
                    return None
                slow_i = self._row_index(slow_table, candidate)
                if slow_i is None:
                    st.skipped_post_only += 1
                    return None
                yi = slow_i if slow == "y" else yi_touch
                ni = slow_i if slow == "n" else ni_touch
                ok, why = self._candidate_valid(
                    int(yes["prices"][yi]),
                    int(no["prices"][ni]),
                    yb,
                    nb,
                )
                if not ok:
                    if why == "pair_cost":
                        st.skipped_pair_cost += 1
                    else:
                        st.skipped_post_only += 1
                    return None
                details = self._details(
                    yes, no, yi, ni, f"spend_all_{slow}_slow"
                )
                details["touch_slower_leg"] = slow
                return details

            matrices = self._full_matrices(yb, nb)
            if matrices is None:
                st.skipped_support += 1
                return None
            if policy.allocation == "full_minimax_eta":
                yi, ni = self._choose_matrix(matrices, "minimax")
                return self._details(
                    matrices["yes"],
                    matrices["no"],
                    yi,
                    ni,
                    "full_minimax_eta",
                )
            if policy.allocation == "full_ev_gate":
                yi, ni = self._choose_matrix(matrices, "ev")
                details = self._details(
                    matrices["yes"],
                    matrices["no"],
                    yi,
                    ni,
                    "full_ev_gate",
                )
                if not details["selected_ev_proxy_c"] > 0.0:
                    marker = "ev_gate_reject"
                    st.allocation_counts[marker] = (
                        st.allocation_counts.get(marker, 0) + 1
                    )
                    return None
                return details
            raise AssertionError(f"unknown allocation {policy.allocation}")

        def _record_cycle(self, st, ts, pnl_c, paired, exit_reason):
            super()._record_cycle(st, ts, pnl_c, paired, exit_reason)
            row = st.cycles[-1]["admission_features"]
            admission = st.admission or {}
            for key in (
                "pred_q_y_60",
                "pred_q_n_60",
                "pred_q_both_60",
                "proxy_gain_c",
                "proxy_q_star",
                "selected_ev_proxy_c",
                "touch_slower_leg",
            ):
                row[key] = admission.get(key)

    FullGridMarket.__name__ = "FullGridMarket"
    FullGridMarket.__qualname__ = "FullGridMarket"
    return FullGridMarket


def policies(base):
    return [
        base.Policy(name="A0_COMMON_TOUCH", allocation="common_touch"),
        base.Policy(name="A1_SPEND_ALL_SLOW", allocation="spend_all_slow"),
        base.Policy(name="A2_FULL_MINIMAX_ETA", allocation="full_minimax_eta"),
        base.Policy(name="A3_FULL_EV_GATE", allocation="full_ev_gate"),
    ]


def finite_summary(values):
    vals = [float(value) for value in values if value is not None and math.isfinite(value)]
    if not vals:
        return {
            "mean": None,
            "median": None,
            "p10": None,
            "p90": None,
            "min": None,
            "max": None,
        }
    return {
        "mean": round(statistics.fmean(vals), 6),
        "median": round(statistics.median(vals), 6),
        "p10": round(quantile_linear(vals, 0.10), 6),
        "p90": round(quantile_linear(vals, 0.90), 6),
        "min": round(min(vals), 6),
        "max": round(max(vals), 6),
    }


def metric_block(name, raw):
    cycles = raw["cycles"]
    paired_rows = [row for row in cycles if row["paired"]]
    orphan_rows = [row for row in cycles if not row["paired"]]
    n = len(cycles)
    paired = len(paired_rows)
    markets = len({row["market"] for row in cycles})
    completion = None if not n else paired / n
    pair_gain = (
        statistics.fmean(row["pnl_c"] for row in paired_rows)
        if paired_rows else None
    )
    orphan_loss = (
        statistics.fmean(row["pnl_c"] for row in orphan_rows)
        if orphan_rows else None
    )
    realized_ev = (
        statistics.fmean(row["pnl_c"] for row in cycles)
        if cycles else None
    )
    q_star = None
    if (
        pair_gain is not None
        and orphan_loss is not None
        and pair_gain > 0 > orphan_loss
    ):
        q_star = abs(orphan_loss) / (pair_gain + abs(orphan_loss))
    formula_ev = None
    if (
        completion is not None
        and pair_gain is not None
        and orphan_loss is not None
    ):
        formula_ev = (
            completion * pair_gain
            + (1.0 - completion) * orphan_loss
        )
    lcb = wilson_lcb(paired, n)
    ci = cluster_boot_ci(
        cycles,
        int(hashlib.sha256(name.encode()).hexdigest()[:12], 16),
    )
    admissions = raw["admissions"]
    proxy_ev = [
        row.get("selected_ev_proxy_c") for row in admissions
    ]
    proxy_q_star = [row.get("proxy_q_star") for row in admissions]
    proxy_q_both = [row.get("pred_q_both_60") for row in admissions]
    block = {
        "cycles": n,
        "markets": markets,
        "paired": paired,
        "orphans": len(orphan_rows),
        "completion": None if completion is None else round(completion, 6),
        "completion_wilson_lcb95": None if lcb is None else round(lcb, 6),
        "empirical_break_even_q_star": (
            None if q_star is None else round(q_star, 6)
        ),
        "pair_gain_c": None if pair_gain is None else round(pair_gain, 6),
        "orphan_loss_c": (
            None if orphan_loss is None else round(orphan_loss, 6)
        ),
        "realized_ev_c_per_cycle": (
            None if realized_ev is None else round(realized_ev, 6)
        ),
        "empirical_formula_ev_c": (
            None if formula_ev is None else round(formula_ev, 6)
        ),
        "ci95_market_cluster_c": ci,
        "selected_ev_proxy_c": finite_summary(proxy_ev),
        "selected_proxy_q_star": finite_summary(proxy_q_star),
        "selected_q_both_60": finite_summary(proxy_q_both),
        "admissions": len(admissions),
        "placed": raw["placed"],
        "fills": raw["fills"],
        "flattened": raw["flattened"],
        "carried": raw["carried"],
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
        and realized_ev is not None
        and realized_ev > 0
        and not raw["bugs"]
    )
    return block


def rank_names(metrics):
    def key(name):
        metric = metrics[name]
        lo = metric["ci95_market_cluster_c"][0]
        ev = metric["realized_ev_c_per_cycle"]
        return (
            bool(metric["strict_pass"]),
            -math.inf if lo is None else lo,
            -math.inf if ev is None else ev,
            metric["markets"],
            metric["cycles"],
        )

    return sorted(metrics, key=key, reverse=True)


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def self_test(base):
    assert legal_move(990, 1) == 1_000
    assert legal_move(1_000, -1) == 990
    assert legal_move(9_000, 1) == 9_010
    assert legal_move(9_010, -1) == 9_000
    assert legal_floor(995) == 990
    assert legal_floor(9_005) == 9_000
    klass = make_grid_market(base)
    policies_ = policies(base)
    sim = klass("SYNTH", 300_000_000, "yes", [], policies_)
    sim.last_ts = 1
    sim.first_event_ts = -60_000_000
    sim.books = {
        "y": {990: 10_000, 1_000: 10_000},
        "n": {8_900: 10_000},
    }
    sim.recent_trades.extend([
        (-1, 900, 100_000, "no"),
        (-1, 1_100, 100_000, "yes"),
    ])
    features = sim._features(1, 990, 8_900)
    assert sim._touch_support(features)
    yes, no = sim._tables(990, 8_900)
    assert sim._row_index(yes, 1_000) is not None
    assert sim._row_index(no, 9_010) is None  # no ask is 90.1c, strict post-only
    print("SELF_TEST_OK legal_grid_and_full_matrix", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--raw-out", default=RAW_OUT)
    parser.add_argument("--prereg", default=PREREG_PATH)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    script_sha = sha256_path(__file__)
    prereg_bytes = open(args.prereg, "rb").read()
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()
    prereg = json.loads(prereg_bytes)
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("pre-registration script hash mismatch; run void")
    if prereg.get("spec") != PREREGISTRATION:
        raise RuntimeError("pre-registration spec mismatch; run void")

    base = load_sealed_base()
    if args.self_test:
        self_test(base)
        return
    if mp.get_start_method(allow_none=True) not in (None, "fork"):
        raise RuntimeError("sealed replay requires Linux fork multiprocessing")

    base.TRAIN_DATES = TRAIN_DATES
    base.Z3Market = make_grid_market(base)
    arms = policies(base)
    if [policy.name for policy in arms] != list(PREREGISTRATION["arms"]):
        raise RuntimeError("pre-registration arm order mismatch; run void")

    print(
        "DISCOVERY-only, exactly 4 pre-registered full-grid allocation arms",
        flush=True,
    )
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"BASE_SHA {BASE_SHA256}", flush=True)
    print("THIS_RUN_DATES 2026-07-20,2026-07-21,2026-07-22", flush=True)
    print("2026_07_23_READ_THIS_RUN false", flush=True)

    raw = base.run_train(arms)
    metrics = {
        policy.name: metric_block(policy.name, raw[policy.name])
        for policy in arms
    }
    if any(raw[policy.name]["bugs"] for policy in arms):
        raise RuntimeError("invariant bug; report void")
    ranking = rank_names(metrics)
    qualified = [name for name in ranking if metrics[name]["strict_pass"]]
    discovery_candidate = qualified[0] if qualified else None
    decision = (
        "DISCOVERY_CANDIDATE_AWAIT_FORWARD_HOLDOUT"
        if discovery_candidate else "NO_CANDIDATE"
    )
    print(
        json.dumps(
            [
                {
                    "rank": rank,
                    "name": name,
                    "cycles": metrics[name]["cycles"],
                    "markets": metrics[name]["markets"],
                    "completion": metrics[name]["completion"],
                    "lcb": metrics[name]["completion_wilson_lcb95"],
                    "q_star": metrics[name]["empirical_break_even_q_star"],
                    "realized_ev": metrics[name]["realized_ev_c_per_cycle"],
                    "ci": metrics[name]["ci95_market_cluster_c"],
                    "proxy_ev": metrics[name]["selected_ev_proxy_c"]["mean"],
                    "strict": metrics[name]["strict_pass"],
                }
                for rank, name in enumerate(ranking, 1)
            ],
            indent=1,
        ),
        flush=True,
    )

    report = {
        "schema": "z3-price-allocation-round2-report-v1",
        "host": "ubuntu@3.130.232.109",
        "split": {
            "THIS_RUN_DISCOVERY": list(TRAIN_DATES),
            "ALL_DISCOVERY_CONTAMINATED": [
                "2026-07-20",
                "2026-07-21",
                "2026-07-22",
                "2026-07-23",
            ],
            "THIS_RUN_NEVER_OPENS": ["2026-07-23"],
            "FINAL_VALIDATION": "forward holdout after model and code seal",
        },
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "preregistration": PREREGISTRATION,
        "policies": [asdict(policy) for policy in arms],
        "ranking": ranking,
        "metrics": metrics,
        "strict_qualified": qualified,
        "discovery_candidate": discovery_candidate,
        "decision": decision,
        "deployable": False,
        "required_next_evidence": (
            "model-and-code seal followed by genuinely forward holdout"
        ),
        "source": {
            "experiment_sha256": script_sha,
            "sealed_base_sha256": BASE_SHA256,
            "preregistration_path": args.prereg,
            "preregistration_sha256": prereg_sha,
            "catalog": f"{base.SNAP_DIR}/shards/KXBTC15M.json",
            "l2_glob": base.L2_GLOB,
            "trades_glob": base.TR_GLOB,
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    raw_artifact = {
        "schema": "z3-price-allocation-round2-raw-v1",
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "nonfinite_encoding": "null means positive infinity from zero flow",
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
    print(f"DISCOVERY_CANDIDATE {discovery_candidate}", flush=True)
    print(f"DECISION {decision}", flush=True)
    print("DEPLOYABLE false", flush=True)
    print("HISTORICAL_VALIDATION_CLAIM false", flush=True)
    print("FINAL_VALIDATION forward_holdout_after_model_and_code_seal", flush=True)


if __name__ == "__main__":
    main()
