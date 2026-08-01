#!/usr/bin/env python3
"""ROUND3 causal, fail-closed DISCOVERY replay of four allocation arms.

The economic arms and fill/cancel simulator are imported from sealed ROUND2.
ROUND3 changes the data reconstruction only where ROUND2 was proved invalid:
books and trades share one local receive clock, snapshots and signed deltas are
guarded by the shared causal contract, corrupt market-days are rolled back, and
all allocation ETAs must be finite and positive.  Full-grid selection is
mathematically identical but uses a precomputed flat feasible-pair index and
prefix sums.  Nothing in this file places an exchange order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict

import duckdb
import numpy as np


TRAIN_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FORBIDDEN_DATE = "2026-07-23"
ROUND2_PATH = os.environ.get(
    "Z3_ROUND3_ROUND2",
    "/tmp/z3_price_allocation_round2.py",
)
ROUND2_SHA256 = (
    "3b5ff6d4fddf617e6cb4bcf69e5449fc537a43f4d28cec9b61b1d938482358ee"
)
SEALED_BASE_SHA256 = (
    "81c35b2982db9d892fb2c97d55f572681d5a60631b6367023e9330cdfc0feb5b"
)
CONTRACT_PATH = os.environ.get(
    "Z3_ROUND3_CONTRACT",
    os.path.join(os.path.dirname(__file__), "causal_replay_contract.py"),
)
CONTRACT_SHA256 = (
    "4b7a3379a69aba3ea95361294db4ffea775791f56942f5c10818b14b18c04afd"
)
OUT = os.environ.get(
    "Z3_ROUND3_OUT",
    "/tmp/z3_price_allocation_round3_report.json",
)
RAW_OUT = os.environ.get(
    "Z3_ROUND3_RAW_OUT",
    "/tmp/z3_price_allocation_round3_raw.json",
)
PREREG_PATH = os.environ.get(
    "Z3_ROUND3_PREREG",
    "/tmp/z3_price_allocation_round3_prereg.json",
)
EQUIV_OUT = os.environ.get(
    "Z3_ROUND3_EQUIV_OUT",
    "/tmp/z3_price_allocation_round3_equivalence.json",
)
EQUIV_DATE = "2026-07-20"
EQUIV_TICKER = "KXBTC15M-26JUL192315-15"
PAIR_LOCK_E4 = 9_900
HORIZON_S = 60.0
FROZEN_ORPHAN_LOSS_C = 3.5
LEGAL_PRICES_E4 = tuple(
    list(range(10, 1_000, 10))
    + list(range(1_000, 9_001, 100))
    + list(range(9_010, 10_000, 10))
)
LEGAL_PRICE_SET = frozenset(LEGAL_PRICES_E4)
LEGAL_ARRAY = np.asarray(LEGAL_PRICES_E4, dtype=np.int64)
_PAIR_Y_IDX, _PAIR_N_IDX = np.nonzero(
    LEGAL_ARRAY[:, np.newaxis] + LEGAL_ARRAY[np.newaxis, :] <= PAIR_LOCK_E4
)
PAIR_Y_IDX = _PAIR_Y_IDX.astype(np.int32)
PAIR_N_IDX = _PAIR_N_IDX.astype(np.int32)
PAIR_SUM_E4 = (
    LEGAL_ARRAY[PAIR_Y_IDX] + LEGAL_ARRAY[PAIR_N_IDX]
).astype(np.int64)

_BASE = None
_MARKET_CLASS = None
_ROUND2 = None
_CONTRACT = None


PREREGISTRATION = {
    "schema": "z3-price-allocation-round3-prereg-v1",
    "status": "DISCOVERY_ONLY_CAUSAL_RECONSTRUCTION",
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
        "this_run_never_opens": [FORBIDDEN_DATE],
        "claim": "DISCOVERY_ONLY",
        "final_validation": "post-model-and-code-seal forward holdout only",
    },
    "sealed_sources": {
        "round2_script_sha256": ROUND2_SHA256,
        "base_replay_sha256": SEALED_BASE_SHA256,
        "causal_contract_sha256": CONTRACT_SHA256,
    },
    "causal_reconstruction": {
        "required_fields_both_channels": [
            "recv_wall_ns",
            "recv_mono_ns",
            "local_recv_ts_us",
        ],
        "clock_identity": "local_recv_ts_us == recv_wall_ns//1000",
        "decision_flow_tte_clock": "local_recv_ts_us only",
        "forbidden_replay_clocks": ["ts_utc", "exchange_ts_us"],
        "combined_market_key": (
            "(recv_wall_ns,recv_mono_ns,channel_priority,"
            "ws_seq_or_sentinel,stable_id)"
        ),
        "same_envelope_priority": "BOOK=0 before TRADE=1",
        "book_order": "recv_wall_ns,recv_mono_ns,ws_seq",
        "trade_order": "recv_wall_ns,recv_mono_ns,trade_id",
        "snapshot": "full re-anchor including ws_sid/ws_seq",
        "delta": (
            "requires prior snapshot, same ws_sid and strictly increasing "
            "ws_seq; raw daily receipt, not per-market +1, is gap authority"
        ),
        "signed_level_rule": (
            "next=prior+delta; next<0 invalidates and rolls back the complete "
            "market-day; next=0 deletes; never clamp"
        ),
        "failure_scope": (
            "daily raw receipt failure aborts the date; any market clock, "
            "sequence, snapshot, delta, trade, or reconstruction failure "
            "rolls back that complete market-day for all arms and is reported"
        ),
        "raw_l2_receipt_gate": {
            "schema": "l2-gap-receipt-v1",
            "required_zero": [
                "sids_with_seq_gaps",
                "seq_gap_events",
                "seq_missed_total",
                "seq_regressions",
                "stream_restarts",
                "markers_lost_frames",
                "parse_errors",
            ],
            "allowed_recorder_marker_keys": ["transport_close"],
            "snapshot_reanchors": "audited but allowed",
        },
    },
    "common": {
        "zone": "120<=TTE<300 and (mid<20c or mid>80c)",
        "support": (
            "complete trailing causal 60s history and finite positive "
            "two-sided touch ETA; every arm skips otherwise"
        ),
        "clip_contracts": 1,
        "pair_cost_max_c": 99,
        "post_only": (
            "each candidate bid is on the real legal grid and strictly below "
            "its corresponding current ask"
        ),
        "legal_grid": (
            "0.1c on [0.1c,9.9c], 1c on [10c,90c], 0.1c on "
            "[90.1c,99.9c], including 9.9<->10 and 90<->90.1 boundaries"
        ),
        "orphan_exit": "counterpart >=2c behind current touch, otherwise TTL60",
        "cancel_ack_latency_ms": 60,
        "fill_queue_ahead": "displayed quantity at exact quote level only",
        "fill_test": (
            "strict trade-through: eligible causal trade volume "
            "> exact-level ahead + clip"
        ),
        "eta": (
            "(positive displayed barrier depth at better prices + same-level "
            "ahead + clip)/(past causal 60s executable taker flow/60)"
        ),
        "q_model": "q_side=1-exp(-60/ETA_side); q_both=q_yes*q_no",
        "proxy_gain": "G=100-p_yes-p_no cents",
        "proxy_loss_c": FROZEN_ORPHAN_LOSS_C,
        "ev_proxy": "q_both*G-(1-q_both)*3.5c",
    },
    "arms": {
        "A0_COMMON_TOUCH": (
            "quote both touches after common support if legal/post-only/sum<=99c"
        ),
        "A1_SPEND_ALL_SLOW": (
            "keep fast touch; spend all feasible pair slack on slow leg; "
            "after allocation require both candidate ETAs finite and >0"
        ),
        "A2_FULL_MINIMAX_ETA": (
            "complete legal feasible Cartesian product; minimize max ETA; "
            "ties higher q_both, lower pair sum, lower ETA imbalance, "
            "lower YES then NO price"
        ),
        "A3_FULL_EV_GATE": (
            "same complete product; maximize frozen EVproxy; ties higher "
            "q_both, lower max ETA, lower pair sum, lower ETA imbalance, "
            "lower YES then NO; enter only EVproxy>0"
        ),
    },
    "equivalent_acceleration": {
        "side_tables": (
            "sorted integer book/trade prefix sums replace outer-product sums"
        ),
        "pair_grid": (
            "precomputed flat indices for every legal pair with sum<=99c; "
            "no feasible action is removed"
        ),
        "required_tests": (
            "synthetic legal-boundary and causal vectors plus actual old "
            "discovery-market pointwise optimized-vs-naive enumeration for "
            "both A2 and A3"
        ),
    },
    "selection_gate": (
        "cycles>=30 AND markets>=10 AND empirical completion Wilson "
        "LCB95>empirical break-even q* AND market-cluster bootstrap mean-PnL "
        "CI95 lower>0 AND zero strategy bugs AND all daily raw receipt gates "
        "pass; only fully clean market-days enter metrics"
    ),
    "interpretation": (
        "strict positive is only DISCOVERY candidate; deployable=false; "
        "forward holdout mandatory"
    ),
}


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_pinned_module(path, expected_sha, name):
    actual = sha256_path(path)
    if actual != expected_sha:
        raise RuntimeError(
            f"{name} SHA mismatch expected={expected_sha} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def initialize_modules():
    global _BASE, _MARKET_CLASS, _ROUND2, _CONTRACT
    _CONTRACT = load_pinned_module(
        CONTRACT_PATH,
        CONTRACT_SHA256,
        "z3_round3_causal_contract",
    )
    _ROUND2 = load_pinned_module(
        ROUND2_PATH,
        ROUND2_SHA256,
        "z3_round3_sealed_round2",
    )
    _BASE = _ROUND2.load_sealed_base()
    _MARKET_CLASS = make_causal_fast_market(_ROUND2, _BASE)


def make_causal_fast_market(round2, base):
    parent = round2.make_grid_market(base)

    class CausalFastMarket(parent):
        """Sealed economics with causal trades and equivalent fast allocation."""

        def __init__(self, ticker, close_us, result, policies):
            super().__init__(ticker, close_us, result, [], policies)
            self._book_version = 0
            self._trade_version = 0
            self._round3_table_cache = None
            self._equivalence_budget = {"minimax": 0, "ev": 0}
            self.equivalence_counts = {"minimax": 0, "ev": 0}

        def set_equivalence_budget(self, each):
            self._equivalence_budget = {
                "minimax": int(each),
                "ev": int(each),
            }

        def _trim_flow(self, ts):
            before = len(self.recent_trades)
            super()._trim_flow(ts)
            if len(self.recent_trades) != before:
                self._trade_version += 1

        def on_trade_causal(self, ts, yes_px, qty, taker):
            ts = int(ts)
            qty = int(qty)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
            for policy in self.policies:
                st = self.states[policy.name]
                self._advance(policy, st, ts)
                for side, sell_taker in (("y", "no"), ("n", "yes")):
                    quote = st.quotes[side]
                    if quote is None or taker != sell_taker:
                        continue
                    side_px = int(yes_px) if side == "y" else 10_000 - int(yes_px)
                    if side_px <= quote.lvl:
                        quote.ahead -= qty
                        if quote.ahead < -base.CLIP_E4:
                            self._fill(
                                policy,
                                st,
                                side,
                                quote.lvl,
                                ts,
                                was_cancel_pending=quote.cx is not None,
                            )
            self.recent_trades.append((ts, int(yes_px), qty, taker))
            self._trade_version += 1
            self._trim_flow(ts)

        def on_book_causal(
            self,
            ts,
            msg_type,
            side,
            price,
            delta,
            yes_levels,
            no_levels,
        ):
            if not self.close:
                return
            ts = int(ts)
            if self.first_event_ts is None:
                self.first_event_ts = ts
            self.last_ts = ts
            for policy in self.policies:
                self._advance(policy, self.states[policy.name], ts)
            self._book_version += 1
            if msg_type == "snapshot":
                self.books = {"y": {}, "n": {}}
                for levels, key in (
                    (yes_levels, "y"),
                    (no_levels, "n"),
                ):
                    for level, quantity in levels or []:
                        self.books[key][int(level)] = int(quantity)
            else:
                key = "y" if side == "yes" else "n"
                level = int(price)
                after = self.books[key].get(level, 0) + int(delta)
                if after == 0:
                    self.books[key].pop(level, None)
                else:
                    self.books[key][level] = after
            eligible = self._z3_now(ts)
            for policy in self.policies:
                st = self.states[policy.name]
                self._advance(policy, st, ts)
                if st.orphan_side is not None:
                    continue
                has_quote = (
                    st.quotes["y"] is not None
                    or st.quotes["n"] is not None
                )
                if has_quote:
                    if eligible is None:
                        self._request_cancel(
                            st, "y", ts, "zone_invalid"
                        )
                        self._request_cancel(
                            st, "n", ts, "zone_invalid"
                        )
                    continue
                if eligible is None:
                    continue
                yb, nb, features = eligible
                allocation = self._allocate(
                    policy, st, yb, nb, features
                )
                if allocation is not None:
                    self._admit(
                        policy, st, ts, features, allocation
                    )

        def _side_table(self, side, ask_e4):
            stop = int(np.searchsorted(LEGAL_ARRAY, int(ask_e4), side="left"))
            prices = LEGAL_ARRAY[:stop]
            if prices.size == 0:
                return None
            book = self.books[side]
            if any(
                type(price) is not int
                or type(quantity) is not int
                or quantity <= 0
                for price, quantity in book.items()
            ):
                raise RuntimeError("non-positive/non-integer book reached ETA")
            if book:
                ordered = sorted(book.items())
                book_prices = np.fromiter(
                    (row[0] for row in ordered),
                    dtype=np.int64,
                    count=len(ordered),
                )
                book_qty = np.fromiter(
                    (row[1] for row in ordered),
                    dtype=np.int64,
                    count=len(ordered),
                )
                prefix = np.concatenate(
                    (
                        np.zeros(1, dtype=np.int64),
                        np.cumsum(book_qty, dtype=np.int64),
                    )
                )
                positions = np.searchsorted(
                    book_prices, prices, side="right"
                )
                barrier_e4 = prefix[-1] - prefix[positions]
            else:
                barrier_e4 = np.zeros(prices.size, dtype=np.int64)
            same_e4 = np.fromiter(
                (int(book.get(int(price), 0)) for price in prices),
                dtype=np.int64,
                count=prices.size,
            )

            executable = []
            for _ts, yes_px, quantity, taker in self.recent_trades:
                if side == "y" and taker == "no":
                    executable.append((int(yes_px), int(quantity)))
                elif side == "n" and taker == "yes":
                    executable.append((10_000 - int(yes_px), int(quantity)))
            if executable:
                executable.sort()
                trade_prices = np.fromiter(
                    (row[0] for row in executable),
                    dtype=np.int64,
                    count=len(executable),
                )
                trade_qty = np.fromiter(
                    (row[1] for row in executable),
                    dtype=np.int64,
                    count=len(executable),
                )
                flow_prefix = np.concatenate(
                    (
                        np.zeros(1, dtype=np.int64),
                        np.cumsum(trade_qty, dtype=np.int64),
                    )
                )
                trade_positions = np.searchsorted(
                    trade_prices, prices, side="right"
                )
                flow_e4 = flow_prefix[trade_positions]
            else:
                flow_e4 = np.zeros(prices.size, dtype=np.int64)
            barrier = barrier_e4.astype(np.float64) / 10_000.0
            same = same_e4.astype(np.float64) / 10_000.0
            flow = flow_e4.astype(np.float64) / 10_000.0
            numerator = barrier + same + 1.0
            eta = np.full(prices.size, np.inf, dtype=np.float64)
            positive = flow_e4 > 0
            eta[positive] = numerator[positive] / (
                flow[positive] / HORIZON_S
            )
            finite = np.isfinite(eta) & (eta > 0)
            q = np.zeros(prices.size, dtype=np.float64)
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
                self._book_version,
                self._trade_version,
                len(self.recent_trades),
                int(yb),
                int(nb),
            )
            cached = self._round3_table_cache
            if cached is not None and cached[0] == key:
                return cached[1], cached[2]
            yes = self._side_table("y", 10_000 - int(nb))
            no = self._side_table("n", 10_000 - int(yb))
            self._round3_table_cache = (key, yes, no)
            return yes, no

        def _features(self, ts, yb, nb):
            self._trim_flow(ts)
            self.flow_cache = {}
            yes, no = self._tables(yb, nb)

            def touch(table, price):
                if table is None:
                    return {
                        "flow": 0.0,
                        "same": 0.0,
                        "eta": math.inf,
                    }
                index = self._row_index(table, price)
                if index is None:
                    return {
                        "flow": 0.0,
                        "same": 0.0,
                        "eta": math.inf,
                    }
                return {
                    "flow": float(table["flow"][index]),
                    "same": float(table["same"][index]),
                    "eta": float(table["eta"][index]),
                }

            yrow = touch(yes, yb)
            nrow = touch(no, nb)
            mid_c = (int(yb) + (10_000 - int(nb))) / 200.0
            return {
                "history60": bool(
                    self.first_event_ts is not None
                    and int(ts) - self.first_event_ts >= base.FLOW_WINDOW_US
                ),
                "flow_y60": yrow["flow"],
                "flow_n60": nrow["flow"],
                "min_flow60": min(yrow["flow"], nrow["flow"]),
                "depth_y": yrow["same"],
                "depth_n": nrow["same"],
                "eta_y_s": yrow["eta"],
                "eta_n_s": nrow["eta"],
                "max_eta_s": max(yrow["eta"], nrow["eta"]),
                "mid_c": mid_c,
                "yes_bid_c": int(yb) / 100.0,
                "no_bid_c": int(nb) / 100.0,
                "tte_s": (self.close - int(ts)) / 1_000_000.0,
            }

        def _full_matrices(self, yb, nb):
            yes, no = self._tables(yb, nb)
            if yes is None or no is None:
                return None
            candidate = (
                (PAIR_Y_IDX < len(yes["prices"]))
                & (PAIR_N_IDX < len(no["prices"]))
            )
            yi = PAIR_Y_IDX[candidate].astype(np.int64, copy=False)
            ni = PAIR_N_IDX[candidate].astype(np.int64, copy=False)
            finite = yes["finite"][yi] & no["finite"][ni]
            yi = yi[finite]
            ni = ni[finite]
            if yi.size == 0:
                return None
            eta_y = yes["eta"][yi]
            eta_n = no["eta"][ni]
            q_both = yes["q"][yi] * no["q"][ni]
            pair_sum = (
                yes["prices"][yi] + no["prices"][ni]
            ).astype(np.int64)
            max_eta = np.maximum(eta_y, eta_n)
            imbalance = np.abs(np.log(eta_y) - np.log(eta_n))
            gain_c = 100.0 - pair_sum.astype(np.float64) / 100.0
            ev = (
                q_both * gain_c
                - (1.0 - q_both) * FROZEN_ORPHAN_LOSS_C
            )
            return {
                "yes": yes,
                "no": no,
                "valid": np.ones(yi.size, dtype=bool),
                "yi": yi,
                "ni": ni,
                "q_both": q_both,
                "max_eta": max_eta,
                "pair_sum": pair_sum,
                "imbalance": imbalance,
                "ev": ev,
                "yp": yes["prices"][yi],
                "np": no["prices"][ni],
            }

        @staticmethod
        def _naive_choice(yes, no, objective):
            best_score = None
            best_pair = None
            for yi, ypx in enumerate(yes["prices"]):
                if not yes["finite"][yi]:
                    continue
                eta_y = float(yes["eta"][yi])
                q_y = float(yes["q"][yi])
                for ni, npx in enumerate(no["prices"]):
                    if int(ypx) + int(npx) > PAIR_LOCK_E4:
                        continue
                    if not no["finite"][ni]:
                        continue
                    eta_n = float(no["eta"][ni])
                    q_both = q_y * float(no["q"][ni])
                    max_eta = max(eta_y, eta_n)
                    pair_sum = int(ypx) + int(npx)
                    imbalance = abs(math.log(eta_y) - math.log(eta_n))
                    gain_c = 100.0 - pair_sum / 100.0
                    ev = (
                        q_both * gain_c
                        - (1.0 - q_both) * FROZEN_ORPHAN_LOSS_C
                    )
                    if objective == "minimax":
                        score = (
                            max_eta,
                            -q_both,
                            pair_sum,
                            imbalance,
                            int(ypx),
                            int(npx),
                        )
                    elif objective == "ev":
                        score = (
                            -ev,
                            -q_both,
                            max_eta,
                            pair_sum,
                            imbalance,
                            int(ypx),
                            int(npx),
                        )
                    else:
                        raise AssertionError(objective)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_pair = (yi, ni)
            return best_pair

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
            selected = np.flatnonzero(mask)
            if selected.size != 1:
                raise RuntimeError(
                    f"non-unique {objective} flat-grid selection "
                    f"{selected.size}"
                )
            position = int(selected[0])
            choice = (
                int(matrices["yi"][position]),
                int(matrices["ni"][position]),
            )
            if self._equivalence_budget[objective] > 0:
                naive = self._naive_choice(
                    matrices["yes"], matrices["no"], objective
                )
                if naive != choice:
                    raise RuntimeError(
                        f"{objective} optimized/naive mismatch "
                        f"optimized={choice} naive={naive}"
                    )
                self._equivalence_budget[objective] -= 1
                self.equivalence_counts[objective] += 1
            return choice

        def _allocate(self, policy, st, yb, nb, features):
            details = super()._allocate(policy, st, yb, nb, features)
            if details is None:
                return None
            eta_y = details.get("pred_eta_y_s")
            eta_n = details.get("pred_eta_n_s")
            valid_eta = (
                eta_y is not None
                and eta_n is not None
                and math.isfinite(eta_y)
                and math.isfinite(eta_n)
                and eta_y > 0
                and eta_n > 0
            )
            if not valid_eta:
                if policy.allocation == "spend_all_slow":
                    st.skipped_support += 1
                    return None
                raise RuntimeError(
                    f"{policy.name}: invalid selected candidate ETA"
                )
            if (
                details["pred_barrier_y"] < 0
                or details["pred_barrier_n"] < 0
                or details["actual_ahead_y"] < 0
                or details["actual_ahead_n"] < 0
            ):
                raise RuntimeError(f"{policy.name}: negative selected depth")
            return details

    CausalFastMarket.__name__ = "CausalFastMarket"
    CausalFastMarket.__qualname__ = "CausalFastMarket"
    return CausalFastMarket


def policies():
    return [
        _BASE.Policy(name="A0_COMMON_TOUCH", allocation="common_touch"),
        _BASE.Policy(name="A1_SPEND_ALL_SLOW", allocation="spend_all_slow"),
        _BASE.Policy(name="A2_FULL_MINIMAX_ETA", allocation="full_minimax_eta"),
        _BASE.Policy(name="A3_FULL_EV_GATE", allocation="full_ev_gate"),
    ]


def empty_arm():
    return {
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


def merge_arm(target, source):
    cycles = source.cycles if not isinstance(source, dict) else source["cycles"]
    admissions = (
        source.admissions
        if not isinstance(source, dict)
        else source["admissions"]
    )
    target["cycles"].extend(cycles)
    target["admissions"].extend(admissions)
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
        target[key] += getattr(source, key) if not isinstance(source, dict) else source[key]
    counts = (
        source.allocation_counts
        if not isinstance(source, dict)
        else source["allocation_counts"]
    )
    for variant, count in counts.items():
        target["allocation_counts"][variant] = (
            target["allocation_counts"].get(variant, 0) + count
        )
    bugs = source.bugs if not isinstance(source, dict) else source["bugs"]
    target["bugs"].extend(bugs)


def exact_date_paths(root_glob, date, extension):
    root = root_glob.split("/**", 1)[0]
    paths = sorted(
        glob.glob(
            f"{root}/subcategory=*/date={date}/*.{extension}"
        )
    )
    if not paths:
        raise RuntimeError(f"no {extension} source paths for {date}")
    for path in paths:
        _CONTRACT.assert_allowed_date_path(path, date)
    return paths


def load_causal_trades(connection, paths, date, market_filter=None):
    where_filter = "" if market_filter is None else " AND market_ticker=?"
    parameters = [paths, "KXBTC15M"]
    if market_filter is not None:
        parameters.append(market_filter)
    query = f"""
        SELECT market_ticker, trade_id, yes_price_e4, count_e4, taker_side,
               recv_wall_ns, recv_mono_ns, local_recv_ts_us
        FROM read_csv(?, header=true, union_by_name=true,
                      types={{'taker_side':'VARCHAR'}})
        WHERE series_ticker=? {where_filter}
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, trade_id
    """
    cursor = connection.execute(query, parameters)
    by_market = defaultdict(list)
    invalid = defaultdict(list)
    previous_key = {}
    row_count = 0
    markets = set()
    while True:
        rows = cursor.fetchmany(250_000)
        if not rows:
            break
        for (
            ticker,
            trade_id,
            yes_price,
            quantity,
            taker,
            wall,
            mono,
            local_us,
        ) in rows:
            row_count += 1
            markets.add(ticker)
            label = f"{date}/{ticker}/trade/{trade_id}"
            try:
                ts = _CONTRACT.assert_receive_clock(
                    wall, mono, local_us, label
                )
                if (
                    not isinstance(trade_id, str)
                    or not trade_id
                    or type(yes_price) is not int
                    or not 0 < yes_price < 10_000
                    or yes_price not in LEGAL_PRICE_SET
                    or type(quantity) is not int
                    or quantity <= 0
                    or taker not in ("yes", "no")
                ):
                    raise RuntimeError("malformed causal trade")
                if (
                    dt.datetime.fromtimestamp(
                        ts / 1_000_000, dt.timezone.utc
                    ).date().isoformat()
                    != date
                ):
                    raise RuntimeError("trade receive date outside partition")
                key = _CONTRACT.trade_key(wall, mono, trade_id)
                if ticker in previous_key and key <= previous_key[ticker]:
                    raise RuntimeError("non-increasing/duplicate trade key")
                previous_key[ticker] = key
                by_market[ticker].append(
                    (
                        int(wall),
                        int(mono),
                        int(ts),
                        int(yes_price),
                        int(quantity),
                        taker,
                    )
                )
            except Exception as exc:
                if len(invalid[ticker]) < 5:
                    invalid[ticker].append(f"{label}: {exc}")
    return by_market, invalid, {
        "rows": row_count,
        "markets": len(markets),
        "invalid_markets": len(invalid),
    }


def run_day_clean(args):
    date, policy_rows, market_filter, equivalence_each = args
    quality_receipt = _CONTRACT.load_gap_receipt(date)
    policy_objects = [_BASE.Policy(**row) for row in policy_rows]
    l2_paths = exact_date_paths(_BASE.L2_GLOB, date, "parquet")
    trade_paths = exact_date_paths(_BASE.TR_GLOB, date, "csv.gz")
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='8GB'")
    trades, invalid_trade_markets, trade_stats = load_causal_trades(
        connection, trade_paths, date, market_filter
    )
    meta = _BASE.load_meta()
    merged = {policy.name: empty_arm() for policy in policy_objects}
    source = {
        "date": date,
        "raw_l2_receipt": quality_receipt,
        "l2_paths": [
            {"path": path, "bytes": os.path.getsize(path)}
            for path in l2_paths
        ],
        "trade_paths": [
            {"path": path, "bytes": os.path.getsize(path)}
            for path in trade_paths
        ],
        "trade_stats": trade_stats,
        "book_rows": 0,
        "book_markets": 0,
        "snapshot_events": 0,
        "delta_events": 0,
        "snapshot_reanchors": 0,
        "stream_resets": 0,
        "zero_level_deletes": 0,
        "clean_market_days": 0,
        "clean_strategy_market_days": 0,
        "no_settlement_meta_market_days": 0,
        "invalid_market_days": 0,
        "invalid_market_samples": [],
        "rolled_back": {
            policy.name: {"cycles": 0, "admissions": 0}
            for policy in policy_objects
        },
        "equivalence_counts": {"minimax": 0, "ev": 0},
        "sample_truncated_after_equivalence": False,
    }

    where_filter = "" if market_filter is None else " AND market_ticker=?"
    parameters = [l2_paths, "KXBTC15M"]
    if market_filter is not None:
        parameters.append(market_filter)
    cursor = connection.execute(
        f"""
        SELECT market_ticker, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR),
               ws_sid, ws_seq, recv_wall_ns, recv_mono_ns,
               local_recv_ts_us
        FROM read_parquet(?, union_by_name=true)
        WHERE series_ticker=? {where_filter}
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, ws_seq
        """,
        parameters,
    )

    current_ticker = None
    sim = None
    guard = None
    violation = None
    trade_rows = []
    trade_index = 0

    def process_trade(row):
        nonlocal sim
        if sim is None or violation is not None:
            return
        _wall, _mono, ts, price, quantity, taker = row
        sim.on_trade_causal(ts, price, quantity, taker)

    def finish_market():
        nonlocal sim, guard, violation, trade_index
        if current_ticker is None:
            return
        if violation is None:
            while trade_index < len(trade_rows):
                process_trade(trade_rows[trade_index])
                trade_index += 1
        if sim is not None:
            sim.finalize()
            for key in ("minimax", "ev"):
                source["equivalence_counts"][key] += (
                    sim.equivalence_counts[key]
                )
        if guard is not None:
            for key in (
                "snapshots",
                "snapshot_reanchors",
                "stream_resets",
                "deltas",
                "zero_level_deletes",
            ):
                output_key = {
                    "snapshots": "snapshot_events",
                    "deltas": "delta_events",
                }.get(key, key)
                source[output_key] += guard.audit[key]
        if violation is not None:
            source["invalid_market_days"] += 1
            if len(source["invalid_market_samples"]) < 30:
                source["invalid_market_samples"].append(
                    {
                        "market": current_ticker,
                        "reasons": violation[:5],
                    }
                )
            if sim is not None:
                for policy in policy_objects:
                    st = sim.states[policy.name]
                    source["rolled_back"][policy.name]["cycles"] += len(
                        st.cycles
                    )
                    source["rolled_back"][policy.name]["admissions"] += len(
                        st.admissions
                    )
            return
        source["clean_market_days"] += 1
        if sim is None:
            source["no_settlement_meta_market_days"] += 1
            return
        source["clean_strategy_market_days"] += 1
        for policy in policy_objects:
            merge_arm(merged[policy.name], sim.states[policy.name])

    stop_after_equivalence = False
    while True:
        rows = cursor.fetchmany(250_000)
        if not rows:
            break
        for row in rows:
            (
                ticker,
                msg_type,
                side,
                price,
                delta,
                yes_raw,
                no_raw,
                ws_sid,
                ws_seq,
                wall,
                mono,
                local_us,
            ) = row
            source["book_rows"] += 1
            if ticker != current_ticker:
                finish_market()
                current_ticker = ticker
                source["book_markets"] += 1
                guard = _CONTRACT.BookReconstructor(ticker)
                violation = list(invalid_trade_markets.get(ticker, [])) or None
                close_us, result = meta.get(ticker, (0, None))
                sim = (
                    _MARKET_CLASS(
                        ticker,
                        close_us,
                        result,
                        policy_objects,
                    )
                    if close_us and result in ("yes", "no")
                    else None
                )
                if sim is not None and equivalence_each:
                    sim.set_equivalence_budget(equivalence_each)
                trade_rows = trades.get(ticker, [])
                trade_index = 0
            book_pair = (int(wall), int(mono))
            while (
                trade_index < len(trade_rows)
                and (trade_rows[trade_index][0], trade_rows[trade_index][1])
                < book_pair
            ):
                process_trade(trade_rows[trade_index])
                trade_index += 1
            if violation is not None:
                continue
            try:
                ts = _CONTRACT.assert_receive_clock(
                    wall,
                    mono,
                    local_us,
                    f"{date}/{ticker}/book/{ws_seq}",
                )
                if (
                    dt.datetime.fromtimestamp(
                        ts / 1_000_000, dt.timezone.utc
                    ).date().isoformat()
                    != date
                ):
                    raise RuntimeError("book receive date outside partition")
                stable_id = f"{msg_type}:{ws_seq}"
                if msg_type == "snapshot":
                    yes_levels = json.loads(yes_raw or "[]")
                    no_levels = json.loads(no_raw or "[]")
                    guard.snapshot(
                        yes_levels,
                        no_levels,
                        int(ws_sid),
                        int(ws_seq),
                        int(wall),
                        int(mono),
                        int(local_us),
                        stable_id,
                    )
                    if any(
                        int(level[0]) not in LEGAL_PRICE_SET
                        for levels in (yes_levels, no_levels)
                        for level in levels
                    ):
                        raise RuntimeError("snapshot price off legal grid")
                elif msg_type == "delta":
                    if type(price) is not int or price not in LEGAL_PRICE_SET:
                        raise RuntimeError("delta price off legal grid")
                    guard.delta(
                        side,
                        price,
                        delta,
                        int(ws_sid),
                        int(ws_seq),
                        int(wall),
                        int(mono),
                        int(local_us),
                        stable_id,
                    )
                    yes_levels = no_levels = None
                else:
                    raise RuntimeError(f"unknown book msg_type {msg_type!r}")
            except Exception as exc:
                violation = [
                    f"{date}/{ticker}/book/{ws_seq}: {type(exc).__name__}: {exc}"
                ]
                continue
            if sim is not None:
                sim.on_book_causal(
                    ts,
                    msg_type,
                    side,
                    price,
                    delta,
                    yes_levels,
                    no_levels,
                )
                normalized = {
                    "yes": {
                        int(level): int(quantity)
                        for level, quantity in sim.books["y"].items()
                        if quantity > 0
                    },
                    "no": {
                        int(level): int(quantity)
                        for level, quantity in sim.books["n"].items()
                        if quantity > 0
                    },
                }
                if normalized != guard.books:
                    raise RuntimeError(
                        "strategy/source book reconstruction mismatch"
                    )
                if (
                    equivalence_each
                    and sim.equivalence_counts["minimax"]
                    >= equivalence_each
                    and sim.equivalence_counts["ev"] >= equivalence_each
                ):
                    stop_after_equivalence = True
                    source["sample_truncated_after_equivalence"] = True
                    break
        if stop_after_equivalence:
            break
    finish_market()
    connection.close()
    source["all_included_market_days_clean"] = True
    source["daily_receipt_gate_pass"] = True
    source["equivalence_complete"] = bool(
        not equivalence_each
        or (
            source["equivalence_counts"]["minimax"] >= equivalence_each
            and source["equivalence_counts"]["ev"] >= equivalence_each
        )
    )
    return date, merged, source


def run_all_days(arms):
    policy_rows = [asdict(policy) for policy in arms]
    with mp.Pool(processes=3) as pool:
        results = pool.map(
            run_day_clean,
            [
                (date, policy_rows, None, 0)
                for date in TRAIN_DATES
            ],
        )
    raw = {policy.name: empty_arm() for policy in arms}
    source_days = []
    for _date, day, source in results:
        source_days.append(source)
        for policy in arms:
            merge_arm(raw[policy.name], day[policy.name])
    return raw, sorted(source_days, key=lambda row: row["date"])


def synthetic_equivalence_test():
    contract_result = _CONTRACT.synthetic_contract_test()
    assert _ROUND2.legal_move(990, 1) == 1_000
    assert _ROUND2.legal_move(1_000, -1) == 990
    assert _ROUND2.legal_move(9_000, 1) == 9_010
    assert _ROUND2.legal_move(9_010, -1) == 9_000
    rng = random.Random(90210)
    comparisons = {"minimax": 0, "ev": 0}
    arm_rows = [asdict(policy) for policy in policies()]
    for case in range(12):
        sim = _MARKET_CLASS(
            f"SYNTH-{case}",
            1_000_000_000,
            "yes",
            [_BASE.Policy(**row) for row in arm_rows],
        )
        sim.first_event_ts = -100_000_000
        sim.last_ts = 1
        yb = rng.choice(LEGAL_PRICES_E4[40:150])
        nb_cap = _ROUND2.legal_floor(min(9_800 - yb, 8_500))
        nb = rng.choice(
            [price for price in LEGAL_PRICES_E4 if 500 <= price <= nb_cap]
        )
        sim.books = {
            "y": {
                price: rng.randint(1, 500) * 10_000
                for price in rng.sample(
                    [p for p in LEGAL_PRICES_E4 if p <= yb], 18
                )
            },
            "n": {
                price: rng.randint(1, 500) * 10_000
                for price in rng.sample(
                    [p for p in LEGAL_PRICES_E4 if p <= nb], 18
                )
            },
        }
        sim.books["y"][yb] = rng.randint(1, 500) * 10_000
        sim.books["n"][nb] = rng.randint(1, 500) * 10_000
        for index in range(80):
            sim.recent_trades.append(
                (
                    -60_000_000 + index * 700_000,
                    rng.choice(LEGAL_PRICES_E4[5:-5]),
                    rng.randint(1, 100) * 10_000,
                    "yes" if index % 2 else "no",
                )
            )
        sim._book_version += 1
        sim._trade_version += 1
        matrices = sim._full_matrices(yb, nb)
        if matrices is None:
            raise AssertionError("synthetic case has no feasible matrix")
        for objective in ("minimax", "ev"):
            optimized = sim._choose_matrix(matrices, objective)
            naive = sim._naive_choice(
                matrices["yes"], matrices["no"], objective
            )
            if optimized != naive:
                raise AssertionError(
                    f"synthetic {objective} mismatch {optimized}/{naive}"
                )
            comparisons[objective] += 1
    return {
        "status": "ROUND3_SYNTHETIC_EQUIVALENCE_OK",
        "causal_contract": contract_result,
        "legal_boundary_tests": 4,
        "full_grid_cases": 12,
        "comparisons": comparisons,
        "precomputed_feasible_pairs": int(PAIR_Y_IDX.size),
        "complete_cartesian_pairs": int(LEGAL_ARRAY.size ** 2),
    }


def actual_sample_equivalence(arms, out_path):
    date, _day, source = run_day_clean(
        (
            EQUIV_DATE,
            [asdict(policy) for policy in arms],
            EQUIV_TICKER,
            8,
        )
    )
    if not source["equivalence_complete"]:
        raise RuntimeError(
            f"actual equivalence budget incomplete {source['equivalence_counts']}"
        )
    receipt = {
        "schema": "z3-round3-actual-equivalence-v1",
        "claim": "DISCOVERY_QA_ONLY",
        "date": date,
        "market": EQUIV_TICKER,
        "forbidden_date_opened": False,
        "optimized_vs_naive_pointwise": source["equivalence_counts"],
        "complete_legal_action_space": True,
        "source_quality": source,
    }
    with open(out_path, "w") as handle:
        json.dump(receipt, handle, indent=2)
    return receipt


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--raw-out", default=RAW_OUT)
    parser.add_argument("--prereg", default=PREREG_PATH)
    parser.add_argument("--equiv-out", default=EQUIV_OUT)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--actual-equivalence", action="store_true")
    args = parser.parse_args()

    script_sha = sha256_path(__file__)
    prereg_bytes = open(args.prereg, "rb").read()
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()
    prereg = json.loads(prereg_bytes)
    spec_sha = hashlib.sha256(
        json.dumps(
            PREREGISTRATION,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("pre-registration script hash mismatch; run void")
    if prereg.get("embedded_spec_sha256") != spec_sha:
        raise RuntimeError("pre-registration spec hash mismatch; run void")
    if (
        prereg.get("schema") != "z3-price-allocation-round3-prereg-receipt-v1"
        or prereg.get("claim") != "DISCOVERY_ONLY"
        or prereg.get("dates") != list(TRAIN_DATES)
        or prereg.get("forbidden_dates") != [FORBIDDEN_DATE]
        or prereg.get("arms") != list(PREREGISTRATION["arms"])
    ):
        raise RuntimeError("pre-registration receipt fields mismatch; run void")
    initialize_modules()
    tool_evidence = _CONTRACT.verify_pinned_contract_tools()
    arms = policies()
    if [policy.name for policy in arms] != list(PREREGISTRATION["arms"]):
        raise RuntimeError("pre-registration arm order mismatch")
    synthetic = synthetic_equivalence_test()
    if args.self_test:
        print(json.dumps(synthetic, indent=2))
        return
    if args.actual_equivalence:
        receipt = actual_sample_equivalence(arms, args.equiv_out)
        print(json.dumps(receipt, indent=2))
        print(f"EQUIVALENCE_RECEIPT {args.equiv_out}")
        return
    if mp.get_start_method(allow_none=True) not in (None, "fork"):
        raise RuntimeError("ROUND3 requires Linux fork multiprocessing")

    print("ROUND3 DISCOVERY-only causal clean replay", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"ROUND2_SHA {ROUND2_SHA256}", flush=True)
    print(f"BASE_SHA {SEALED_BASE_SHA256}", flush=True)
    print(f"CONTRACT_SHA {CONTRACT_SHA256}", flush=True)
    print("THIS_RUN_DATES 2026-07-20,2026-07-21,2026-07-22", flush=True)
    print("2026_07_23_READ_THIS_RUN false", flush=True)

    raw, source_days = run_all_days(arms)
    metrics = {
        policy.name: _ROUND2.metric_block(policy.name, raw[policy.name])
        for policy in arms
    }
    all_receipts_green = all(
        day["daily_receipt_gate_pass"] for day in source_days
    )
    for metric in metrics.values():
        metric["source_clean_market_days"] = sum(
            day["clean_strategy_market_days"] for day in source_days
        )
        metric["source_invalid_market_days_excluded"] = sum(
            day["invalid_market_days"] for day in source_days
        )
        metric["strict_pass"] = bool(
            metric["strict_pass"] and all_receipts_green
        )
    if any(raw[policy.name]["bugs"] for policy in arms):
        raise RuntimeError("strategy invariant bug; report void")
    ranking = _ROUND2.rank_names(metrics)
    qualified = [name for name in ranking if metrics[name]["strict_pass"]]
    candidate = qualified[0] if qualified else None
    decision = (
        "DISCOVERY_CANDIDATE_AWAIT_FORWARD_HOLDOUT"
        if candidate
        else "NO_CANDIDATE"
    )
    compact = [
        {
            "rank": index,
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
        for index, name in enumerate(ranking, 1)
    ]
    print(json.dumps(compact, indent=1), flush=True)

    report = {
        "schema": "z3-price-allocation-round3-report-v1",
        "host": "ubuntu@3.130.232.109",
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "deployable": False,
        "decision": decision,
        "discovery_candidate": candidate,
        "strict_qualified": qualified,
        "required_next_evidence": (
            "model-and-code seal followed by genuinely forward holdout"
        ),
        "split": {
            "THIS_RUN_DISCOVERY": list(TRAIN_DATES),
            "ALL_DISCOVERY_CONTAMINATED": [
                *TRAIN_DATES,
                FORBIDDEN_DATE,
            ],
            "THIS_RUN_NEVER_OPENS": [FORBIDDEN_DATE],
            "FINAL_VALIDATION": "forward holdout after model and code seal",
        },
        "preregistration": PREREGISTRATION,
        "policies": [asdict(policy) for policy in arms],
        "ranking": ranking,
        "metrics": metrics,
        "source_quality": {
            "all_daily_receipts_green": all_receipts_green,
            "days": source_days,
            "tool_contract_evidence": tool_evidence,
        },
        "equivalence_qa": synthetic,
        "source": {
            "experiment_sha256": script_sha,
            "preregistration_path": args.prereg,
            "preregistration_sha256": prereg_sha,
            "round2_sha256": ROUND2_SHA256,
            "sealed_base_sha256": SEALED_BASE_SHA256,
            "causal_contract_sha256": CONTRACT_SHA256,
            "catalog": f"{_BASE.SNAP_DIR}/shards/KXBTC15M.json",
            "l2_glob": _BASE.L2_GLOB,
            "trades_glob": _BASE.TR_GLOB,
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    raw_artifact = {
        "schema": "z3-price-allocation-round3-raw-v1",
        "historical_validation_claim": False,
        "this_run_read_2026_07_23": False,
        "deployable": False,
        "nonfinite_encoding": "null means positive infinity from zero flow",
        "source_quality": report["source_quality"],
        "arms": raw,
    }
    with open(args.out, "w") as handle:
        json.dump(json_safe(report), handle, indent=2, allow_nan=False)
    with open(args.raw_out, "w") as handle:
        json.dump(
            json_safe(raw_artifact),
            handle,
            separators=(",", ":"),
            allow_nan=False,
        )
    print(f"REPORT {args.out}", flush=True)
    print(f"RAW {args.raw_out}", flush=True)
    print(f"DISCOVERY_CANDIDATE {candidate}", flush=True)
    print(f"DECISION {decision}", flush=True)
    print("DEPLOYABLE false", flush=True)
    print("HISTORICAL_VALIDATION_CLAIM false", flush=True)
    print("FINAL_VALIDATION forward_holdout_after_model_and_code_seal", flush=True)


if __name__ == "__main__":
    main()
