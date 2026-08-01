#!/usr/bin/env python3
"""Full-coverage causal post-fill state extraction for ROUND4.

This is a discovery-only extractor, not a strategy candidate or validator.  It
reuses the sealed receive-clock/A1-skip replay for first fills, but replaces
the sampled diagnostic writer with compact state rows for every first-fill
episode.  KEEP state and deterministic IOC route economics are extracted at
0/.25/.5/1/2/5/10/30/60 seconds.  REPRICE is deliberately disabled.

Fee precision boundary
----------------------
The L2 walk identifies price-level slices, not the exchange's private
same-price true-fill partition.  Kalshi rounds each true fill.  Consequently
``ceil_0.0001(sum(raw level fees))`` is a lower bound on true direct net fee
and the corresponding PnL is an optimistic maximum-PnL sensitivity.  Nothing
in this artifact is labeled account-exact or realized direct-fee PnL.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import os
import sys
from collections import Counter
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


SEALED_A1_PATH = os.environ.get(
    "Z3_A1_SKIP_REPLAY",
    "/tmp/z3_postfill_stopping_receive_clock_sampled_a1skip.py",
)
SEALED_A1_SHA256 = (
    "b667c811707b7203bc532b5d51accc1ff2e082d2fff284e76b7377e8945d1b09"
)
SEALED_EPISODES_PATH = os.environ.get(
    "Z3_A1_SKIP_EPISODES",
    "/tmp/z3_postfill_receive_clock_sampled_a1skip_episodes_"
    "20260726T082350Z.json.gz",
)
SEALED_EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
ROUND4_DDL_PATH = os.environ.get(
    "ROUND4_DDL",
    "/tmp/round4_two_stage_tables.sql",
)
ROUND4_DDL_SHA256 = (
    "ede23569a00e9948cd3772cc2213a07f8e5c3b9849b9dd0f46e8e6adff3eaed3"
)
ROUND4_DESIGN_PATH = os.environ.get(
    "ROUND4_DESIGN_PREREG",
    "/tmp/round4_two_stage_hazard_prereg.json",
)
ROUND4_DESIGN_SHA256 = (
    "c3c01c61971e5649f9d5f5a5fcf64b66881f64233452c70a4639a51f5c9e05ef"
)
FEE_BOUNDARY_PATH = os.environ.get(
    "ROUND4_FEE_BOUNDARY",
    "/tmp/postfill_fee_precision_boundary_addendum_v2.json",
)
FEE_BOUNDARY_SHA256 = (
    "e08322198b2fa0ccc5bcce27047f67ba364b6255a48cfa6456460de3ac45b638"
)
FULL_CONTRACT_PATH = os.environ.get(
    "ROUND4_FULL_CONTRACT",
    "/home/ubuntu/hft-bot/tools/research/crypto_mm/"
    "round4_postfill_state_contract.py",
)
FULL_CONTRACT_SHA256 = (
    "ce92a3183610412de5a6cd46740bfa5eaefaf7544808aa0742371b21d2bdaeeb"
)
FULL_CONTRACT_MANIFEST_PATH = os.environ.get(
    "ROUND4_FULL_CONTRACT_MANIFEST",
    "/tmp/FULL_COVERAGE_STAGE2_R2_SHA256SUMS",
)
FULL_CONTRACT_MANIFEST_SHA256 = (
    "65a0797a2c62962ec9ff7ec0f00b851e6bfee2531e7a714640cf2dffa356dbf8"
)
FULL_CONTRACT_DDL_PATH = os.environ.get(
    "ROUND4_FULL_CONTRACT_DDL",
    "/tmp/round4_postfill_state_contract.sql",
)
FULL_CONTRACT_DDL_SHA256 = (
    "fc686fa60739d6d6d1ddf3ec23f9c82c2de1a6e087f12ac6e64eca61b4354e79"
)

DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FORBIDDEN_DATES = ("2026-07-23", "2026-07-26")
HORIZONS_S = (0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
FEE_CLASSIFICATION = (
    "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
)
EXPECTED_FIRST_FILLS = 1202
EXPECTED_MARKETS = 58
CLIP_E4 = 10_000
E4 = Decimal("10000")
FEE_RATE = Decimal("0.07")
CENTICENT_USD = Decimal("0.0001")
WHOLE_CENT_USD = Decimal("0.01")


PREREGISTRATION = {
    "schema": "z3-round4-full-coverage-compact-extractor-prereg-v1",
    "status": "DISCOVERY_EXTRACTION_ONLY",
    "candidate": False,
    "validation": False,
    "deployable": False,
    "live_authorized": False,
    "dates": list(DATES),
    "forbidden_dates": list(FORBIDDEN_DATES),
    "source_replay": {
        "path": SEALED_A1_PATH,
        "sha256": SEALED_A1_SHA256,
        "first_fill_identity_receipt_sha256": SEALED_EPISODES_SHA256,
        "reuse": (
            "same receive-clock merge, A1 allocation guard, strict queue "
            "threshold, and full first-fill population"
        ),
    },
    "coverage": {
        "trajectory_sampling": False,
        "expected_first_fills": EXPECTED_FIRST_FILLS,
        "expected_markets": EXPECTED_MARKETS,
        "required_partition": (
            "each sealed first fill is exactly one of same-envelope "
            "zero-time atom or a continuous episode with a t=0 state"
        ),
        "decision_horizons_s": list(HORIZONS_S),
        "later_horizon_rows": (
            "only while the original complement KEEP order remains at risk"
        ),
    },
    "clock": {
        "engine_clock": "local_recv_ts_us=recv_wall_ns//1000",
        "merge_key": (
            "(recv_wall_ns,recv_mono_ns,channel_priority,"
            "ws_seq_or_sentinel,stable_source_id)"
        ),
        "same_envelope": "(recv_wall_ns,recv_mono_ns)",
        "same_envelope_order": "full merge key; BOOK before TRADE",
        "decision_wall_ns": "first_fill_recv_wall_ns+horizon_ns",
        "decision_mono_ns": "first_fill_recv_mono_ns+horizon_ns",
        "observed_trigger": (
            "first receipt crossing scheduled wall; recorded separately and "
            "never substituted for decision_wall_ns"
        ),
        "state_timing": (
            "crossing event is not applied; feature/book/flow as-of clocks "
            "must be <= scheduled decision clock"
        ),
        "t0": (
            "capture immediately after the first-fill trade; remove it if a "
            "later full-merge-key trade in the same receipt envelope completes "
            "the complement, then materialize only the zero-time atom"
        ),
    },
    "features": [
        "complement raw/nonnegative queue ahead and queue position",
        "complement order age, external/effective touch distance and better depth",
        "complement executable flow 1s/5s/10s/60s",
        "flow acceleration 1v5 and 10v60",
        "YES and complement-oriented touch imbalance",
        "spread, midpoint, causal mid moves 1s/10s/since entry/since fill",
        "pair cost/gain, TTE, first side/price/quantity/elapsed",
        "BUY_COMPLEMENT and SELL_FIRST exact L2 gross/quantity/residual",
        "both routes' executable quantity and residual inventory are sealed "
        "in every paired causal state",
        "aggregate-once minimum-fee and maximum-PnL sensitivity",
    ],
    "actions": {
        "KEEP": "preserve original complement price, queue age and priority",
        "IOC": (
            "exact L2 gross for both visible routes; emit exactly one IOC "
            "action row. Only residual=0 routes are eligible; choose maximum "
            "fee_high PnL, with an exact tie fixed to BUY_COMPLEMENT"
        ),
        "ioc_cancel_ack_latency_ms": (
            "60 exact; inherited from the sealed causal IOC policy"
        ),
        "pair_cost_ceiling_e4": 9900,
        "REPRICE": (
            "DISABLED_NO_CAUSAL_CANCEL_REPLACE_QUEUE_RESET_EVIDENCE"
        ),
        "partial_ioc": (
            "if both routes are incomplete, IOC legal=false with residual "
            "held, selected route/limit null, fixed skip reason, and KEEP is "
            "the only legal action"
        ),
    },
    "fee_semantics": {
        "classification": FEE_CLASSIFICATION,
        "private_fill_exact": False,
        "l2_gross_exact": True,
        "fee_low": (
            "ceil_$0.0001(sum over L2 price-level raw quadratic fee); "
            "partition-independent minimum fee / maximum PnL"
        ),
        "fee_base": (
            "sum over L2 price levels of "
            "ceil_$0.0001(raw quadratic fee at that level); L2-slice "
            "sensitivity, still not private-fill exact"
        ),
        "fee_high": (
            "ceil_$0.01(sum over L2 price-level raw quadratic fee); "
            "conservative whole-cent fee band used for route selection"
        ),
        "required_pnl_order": "fee_low PnL >= fee_base PnL >= fee_high PnL",
        "residual_settlement_imputation": False,
        "official_url": (
            "https://docs.kalshi.com/getting_started/fee_rounding"
        ),
        "precision_boundary_sha256": FEE_BOUNDARY_SHA256,
    },
    "round4_contract": {
        "ddl_sha256": ROUND4_DDL_SHA256,
        "design_prereg_sha256": ROUND4_DESIGN_SHA256,
        "full_coverage_contract_sha256": FULL_CONTRACT_SHA256,
        "full_coverage_manifest_sha256": (
            FULL_CONTRACT_MANIFEST_SHA256
        ),
        "supplemental_ddl_sha256": FULL_CONTRACT_DDL_SHA256,
        "required_entry_points": [
            "preflight_canary_postfill_paths",
            "validate_and_serialize_postfill_rows",
        ],
        "output_kind": "canonical contract DDL batch + receipt; no parallel schema",
        "contract_version": "ROUND4_POSTFILL_FULL_COVERAGE_V2",
    },
    "failure_policy": {
        "source_paths_preflight_before_open": True,
        "forbidden_date_token_fails_before_open": True,
        "market_day_rows_staged_until clean harvest": True,
        "data_invalid": (
            "discard all staged rows for that market-day and fail the worker; "
            "final all-date artifacts are written only after every worker passes"
        ),
        "no_partial_artifact_on_failure": True,
    },
}


def file_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha(path, expected, label):
    actual = file_sha(path)
    if actual != expected:
        raise RuntimeError(
            f"{label} SHA mismatch expected={expected} actual={actual}"
        )
    return actual


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def sha_value(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def decimal_text(value, places="0.00000001"):
    return format(Decimal(value).quantize(Decimal(places)), "f")


def load_sealed_a1():
    require_sha(SEALED_A1_PATH, SEALED_A1_SHA256, "sealed A1 replay")
    spec = importlib.util.spec_from_file_location(
        "sealed_a1_full_coverage_source",
        SEALED_A1_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def walk_sell(book, requested_e4):
    remaining = int(requested_e4)
    fills = []
    for price in sorted(book, reverse=True):
        available = int(book[price] or 0)
        if available <= 0:
            continue
        take = min(remaining, available)
        if take:
            fills.append((int(price), int(take)))
            remaining -= take
        if remaining == 0:
            break
    return fills, int(requested_e4) - remaining, remaining


def _fee_bands(fills):
    level_raw = [
        (
            FEE_RATE
            * (Decimal(quantity) / E4)
            * (Decimal(price) / E4)
            * (Decimal("1") - Decimal(price) / E4)
        )
        for price, quantity in fills
    ]
    raw_total = sum(level_raw, Decimal("0"))
    low = (
        raw_total.quantize(CENTICENT_USD, rounding=ROUND_CEILING)
        if fills
        else Decimal("0")
    )
    base = sum(
        (
            value.quantize(
                CENTICENT_USD,
                rounding=ROUND_CEILING,
            )
            for value in level_raw
        ),
        Decimal("0"),
    )
    high = (
        raw_total.quantize(
            WHOLE_CENT_USD,
            rounding=ROUND_CEILING,
        )
        if fills
        else Decimal("0")
    )
    if not low <= base <= high:
        raise RuntimeError("fee bands are not monotone")
    return {
        "raw_total_usd": raw_total,
        "fee_low_usd": low,
        "fee_base_usd": base,
        "fee_high_usd": high,
    }


def _route_economics(
    book,
    entry_e4,
    route,
    requested_e4=CLIP_E4,
):
    if route not in ("BUY_COMPLEMENT", "SELL_FIRST_LEG"):
        raise ValueError(f"unknown IOC route {route!r}")
    visible_prices = [
        int(price)
        for price, quantity in book.items()
        if int(quantity or 0) > 0
    ]
    if not visible_prices:
        raise RuntimeError("IOC route has no visible held-side bid")
    fills, filled_e4, residual_e4 = walk_sell(book, requested_e4)
    gross = sum(
        (
            (Decimal(price) - Decimal(entry_e4))
            / E4
            * (Decimal(quantity) / E4)
        )
        for price, quantity in fills
    )
    bands = _fee_bands(fills)
    complete = residual_e4 == 0
    worst_bid = min(
        (price for price, _qty in fills),
        default=max(visible_prices),
    )
    limit_e4 = (
        10_000 - int(worst_bid)
        if route == "BUY_COMPLEMENT"
        else int(worst_bid)
    )
    return {
        "route": route,
        "fills": tuple(fills),
        "requested_qty_fp": Decimal(requested_e4) / E4,
        "executable_qty_fp": Decimal(filled_e4) / E4,
        "residual_inventory_fp": Decimal(residual_e4) / E4,
        "complete": complete,
        "ioc_limit_price_e4": limit_e4,
        "gross_pnl_usd": gross,
        "pnl_fee_low_usd": gross - bands["fee_low_usd"],
        "pnl_fee_base_usd": gross - bands["fee_base_usd"],
        "pnl_fee_high_usd": gross - bands["fee_high_usd"],
        **bands,
    }


def visible_ioc_routes(book, entry_e4, requested_e4=CLIP_E4):
    return {
        route: _route_economics(
            book,
            entry_e4,
            route,
            requested_e4,
        )
        for route in ("BUY_COMPLEMENT", "SELL_FIRST_LEG")
    }


def select_ioc_route(routes):
    eligible = [
        row for row in routes.values() if row["complete"]
    ]
    if not eligible:
        return {
            "route": None,
            "ioc_limit_price_e4": None,
            "legal_action": False,
            "skip_reason": (
                "IOC_ALL_ROUTES_INCOMPLETE_RESIDUAL_HELD"
            ),
        }
    selected = min(
        eligible,
        key=lambda row: (
            -row["pnl_fee_high_usd"],
            0 if row["route"] == "BUY_COMPLEMENT" else 1,
        ),
    )
    return {
        "route": selected["route"],
        "ioc_limit_price_e4": selected["ioc_limit_price_e4"],
        "legal_action": True,
        "skip_reason": None,
    }


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


class NullWriter:
    """Preserve replay interfaces without sampled/full trajectory output."""

    def __init__(self, path, date):
        self.path = path
        self.date = date
        self.rows = 0

    def emit(self, _row):
        return None

    def close(self):
        return None


def build_engine_hooks(A, PC):
    R = A.F
    D = R.D
    B = R.B
    C = R.C

    class CompactTracker(D.EpisodeTracker):
        def __init__(self, market_sim, writer):
            super().__init__(market_sim, writer)
            self.compact = {}
            self.last_applied_event = None
            self.flow_trades = []
            self.mid_history = []
            self.audit = Counter()

        @staticmethod
        def _event_key(fields):
            if fields.get("causal_channel_priority") == 0:
                return C.book_key(
                    fields["recv_wall_ns"],
                    fields["recv_mono_ns"],
                    fields["ws_seq"],
                    fields["book_stable_id"],
                )
            return C.trade_key(
                fields["recv_wall_ns"],
                fields["recv_mono_ns"],
                fields["trade_id"],
            )

        @staticmethod
        def _key_json(key):
            return [
                int(key[0]),
                int(key[1]),
                int(key[2]),
                int(key[3]),
                str(key[4]),
            ]

        @staticmethod
        def _stable_id(fields):
            if int(fields["causal_channel_priority"]) == 0:
                return str(fields["book_stable_id"])
            return str(fields["trade_id"])

        @staticmethod
        def _half_delta_int(new_x2, old_x2, label):
            delta = int(new_x2) - int(old_x2)
            if delta % 2:
                raise RuntimeError(
                    f"{label}: half-e4 midpoint move is outside "
                    "the integer ROUND4 contract"
                )
            return delta // 2

        def _book_state(self):
            yb = B.best(self.market_sim.books["y"])
            nb = B.best(self.market_sim.books["n"])
            if yb is None or nb is None:
                raise RuntimeError(
                    f"{self.market_sim.ticker}: empty touch in compact state"
                )
            yes_ask = 10_000 - int(nb)
            spread = yes_ask - int(yb)
            if spread < 0:
                raise RuntimeError(
                    f"{self.market_sim.ticker}: crossed reconstructed book"
                )
            mid_x2 = int(yb) + yes_ask
            yq = int(self.market_sim.books["y"][yb])
            nq = int(self.market_sim.books["n"][nb])
            denominator = yq + nq
            imbalance = (
                Decimal(yq - nq) / Decimal(denominator)
                if denominator
                else Decimal("0")
            )
            return {
                "yes_bid_e4": int(yb),
                "no_bid_e4": int(nb),
                "yes_ask_e4": yes_ask,
                "spread_e4": spread,
                "mid_x2_e4": mid_x2,
                "mid_e4": Decimal(mid_x2) / Decimal("2"),
                "yes_touch_qty_fp": Decimal(yq) / E4,
                "no_touch_qty_fp": Decimal(nq) / E4,
                "yes_touch_imbalance": imbalance,
            }

        def _record_book_mid(self):
            state = self._book_state()
            fields = self.market_sim.current_event_fields
            wall = int(fields["recv_wall_ns"])
            mid_x2 = int(state["mid_x2_e4"])
            if not self.mid_history or self.mid_history[-1][1] != mid_x2:
                self.mid_history.append((wall, mid_x2))

        def _mid_x2_asof(self, wall_ns):
            for wall, mid_x2 in reversed(self.mid_history):
                if wall <= wall_ns:
                    return mid_x2
            return None

        def _append_flow_trade(self):
            fields = self.market_sim.current_event_fields
            key = self._event_key(fields)
            self.flow_trades.append(
                {
                    "key": key,
                    "wall_ns": int(fields["recv_wall_ns"]),
                    "yes_price_e4": int(fields["yes_price_e4"]),
                    "qty_e4": int(fields["count_e4"]),
                    "taker_side": fields["taker_side"],
                }
            )
            cutoff = int(fields["recv_wall_ns"]) - 61_000_000_000
            if self.flow_trades and self.flow_trades[0]["wall_ns"] < cutoff:
                first_keep = 0
                while (
                    first_keep < len(self.flow_trades)
                    and self.flow_trades[first_keep]["wall_ns"] < cutoff
                ):
                    first_keep += 1
                self.flow_trades = self.flow_trades[first_keep:]

        def _flow(self, side, level_e4, scheduled_wall_ns, window_s):
            lower = scheduled_wall_ns - int(window_s * 1_000_000_000)
            total = 0
            for trade in self.flow_trades:
                if not lower < trade["wall_ns"] <= scheduled_wall_ns:
                    continue
                if side == "y":
                    eligible = (
                        trade["taker_side"] == "no"
                        and trade["yes_price_e4"] <= level_e4
                    )
                else:
                    eligible = (
                        trade["taker_side"] == "yes"
                        and 10_000 - trade["yes_price_e4"] <= level_e4
                    )
                if eligible:
                    total += trade["qty_e4"]
            return Decimal(total) / E4

        def start(self, state, ts):
            super().start(state, ts)
            episode = self.active[-1]
            fields = self.market_sim.current_event_fields
            if (
                fields is None
                or fields.get("causal_channel_priority") != 1
                or int(fields["local_recv_ts_us"]) != int(ts)
            ):
                raise RuntimeError("first fill lacks its causal trade receipt")
            first_key = self._event_key(fields)
            quote = state.quotes[episode.counterpart_side]
            if quote is None:
                raise RuntimeError("first fill lost original complement quote")
            book = self._book_state()
            admission_mid_x2 = int(
                round(float(episode.admission["mid_c"]) * 200.0)
            )
            source_identity = {
                "episode_id": episode.episode_id,
                "first_fill_merge_key": self._key_json(first_key),
                "book_merge_key": self._key_json(
                    self.market_sim.reconstructor.last_key
                ),
                "admission": episode.admission,
            }
            self.compact[episode.episode_id] = {
                "episode": episode,
                "first_key": first_key,
                "first_fields": dict(fields),
                "quote_placed_us": int(quote.placed_ts),
                "first_mid_x2_e4": int(book["mid_x2_e4"]),
                "admission_mid_x2_e4": admission_mid_x2,
                "first_tte_ms": max(
                    0,
                    int(
                        (
                            self.market_sim.close
                            - int(fields["local_recv_ts_us"])
                        )
                        // 1000
                    ),
                ),
                "complement_order_age_at_first_fill_ms": max(
                    0,
                    int(
                        (
                            int(fields["local_recv_ts_us"])
                            - int(quote.placed_ts)
                        )
                        // 1000
                    ),
                ),
                "complement_order_id": (
                    "shadow-complement-"
                    + hashlib.sha256(
                        episode.episode_id.encode()
                    ).hexdigest()[:24]
                ),
                "source_rows_sha256": sha_value(source_identity),
                "decisions": {},
                "observed_trigger_audit": {},
                "route_audit": {},
                "zero_time_atom": None,
                "maker_fill_receipt": None,
            }

        def _capture_state(
            self,
            episode,
            horizon_s,
            observed_trigger_fields,
            feature_fields,
        ):
            meta = self.compact[episode.episode_id]
            decision_index = HORIZONS_S.index(horizon_s)
            elapsed_ms = PC.DECISION_GRID_MS[decision_index]
            horizon_ns = elapsed_ms * 1_000_000
            scheduled_wall = (
                int(meta["first_fields"]["recv_wall_ns"]) + horizon_ns
            )
            scheduled_mono = (
                int(meta["first_fields"]["recv_mono_ns"]) + horizon_ns
            )
            observed_wall = int(observed_trigger_fields["recv_wall_ns"])
            observed_mono = int(observed_trigger_fields["recv_mono_ns"])
            feature_wall = int(feature_fields["recv_wall_ns"])
            feature_mono = int(feature_fields["recv_mono_ns"])
            if (
                observed_wall < scheduled_wall
                or feature_wall > scheduled_wall
                or feature_mono > scheduled_mono
            ):
                raise RuntimeError(
                    f"{episode.episode_id}: compact feature lookahead"
                )
            book_key = self.market_sim.reconstructor.last_key
            if book_key is None:
                raise RuntimeError("compact state has no book anchor")
            book_wall = int(book_key[0])
            book_mono = int(book_key[1])
            if book_wall > scheduled_wall or book_mono > scheduled_mono:
                raise RuntimeError(
                    f"{episode.episode_id}: future book in compact state"
                )
            state = self._book_state()
            touch = B.best(
                self.market_sim.books[episode.counterpart_side]
            )
            if touch is None:
                raise RuntimeError("missing complement external touch")
            touch = int(touch)
            effective_touch = max(touch, int(episode.counterpart_lvl))
            better_e4 = sum(
                int(quantity or 0)
                for price, quantity in self.market_sim.books[
                    episode.counterpart_side
                ].items()
                if int(price) > int(episode.counterpart_lvl)
            )
            raw_ahead_e4 = int(episode.counterpart_ahead)
            nonnegative_ahead_e4 = max(0, raw_ahead_e4)
            queue_position_e4 = max(
                0,
                raw_ahead_e4 + CLIP_E4,
            )
            flows = {
                window: self._flow(
                    episode.counterpart_side,
                    int(episode.counterpart_lvl),
                    scheduled_wall,
                    window,
                )
                for window in (1, 5, 10, 60)
            }
            mid1 = self._mid_x2_asof(
                scheduled_wall - 1_000_000_000
            )
            mid10 = self._mid_x2_asof(
                scheduled_wall - 10_000_000_000
            )
            current_mid_x2 = int(state["mid_x2_e4"])
            complement_sign = (
                Decimal("-1")
                if episode.counterpart_side == "n"
                else Decimal("1")
            )
            if mid1 is None or mid10 is None:
                raise RuntimeError(
                    f"{episode.episode_id}: missing causal midpoint history"
                )
            ioc_routes = visible_ioc_routes(
                self.market_sim.books[episode.held_side],
                episode.entry_e4,
                CLIP_E4,
            )
            selected_ioc = select_ioc_route(ioc_routes)
            pair_gain = (
                Decimal(
                    10_000
                    - int(episode.entry_e4)
                    - int(episode.counterpart_lvl)
                )
                / E4
            )
            feature_key = self._event_key(feature_fields)
            source_max_id = self._stable_id(feature_fields)
            causal_source_hash = sha_value(
                _jsonable({
                    "episode_source_rows_sha256": meta[
                        "source_rows_sha256"
                    ],
                    "decision_index": decision_index,
                    "source_max_merge_key": self._key_json(feature_key),
                    "book_asof_merge_key": self._key_json(book_key),
                    "flows": flows,
                    "book_state": state,
                })
            )
            common = {
                "postfill_episode_id": episode.episode_id,
                "source_date_utc": dt.datetime.fromtimestamp(
                    int(meta["first_fields"]["local_recv_ts_us"]) / 1e6,
                    tz=dt.timezone.utc,
                ).date().isoformat(),
                "decision_index": decision_index,
                "decision_elapsed_ms": elapsed_ms,
                "action_family_version": PC.ACTION_FAMILY_VERSION,
                "causal_source_rows_sha256": causal_source_hash,
                "source_max_stable_id": source_max_id,
                "decision_recv_wall_ns": scheduled_wall,
                "decision_recv_mono_ns": scheduled_mono,
                "feature_asof_wall_ns": feature_wall,
                "source_max_recv_wall_ns": feature_wall,
                "source_max_recv_mono_ns": feature_mono,
                "first_fill_side": (
                    "YES" if episode.held_side == "y" else "NO"
                ),
                "first_fill_price_e4": int(episode.entry_e4),
                "first_fill_qty_fp": Decimal("1"),
                "first_fill_elapsed_ms": (
                    Decimal(
                        int(meta["first_fields"]["local_recv_ts_us"])
                        - int(episode.admission["ts"])
                    )
                    / Decimal("1000")
                ),
                "remaining_inventory_fp": Decimal("1"),
                "complement_order_id": meta["complement_order_id"],
                "complement_side": (
                    "YES" if episode.counterpart_side == "y" else "NO"
                ),
                "complement_price_e4": int(episode.counterpart_lvl),
                "complement_order_age_ms": (
                    meta["complement_order_age_at_first_fill_ms"]
                    + elapsed_ms
                ),
                "complement_queue_position_fp": (
                    Decimal(queue_position_e4) / E4
                ),
                "complement_same_price_ahead_fp": (
                    Decimal(nonnegative_ahead_e4) / E4
                ),
                "complement_better_depth_fp": Decimal(better_e4) / E4,
                "complement_touch_distance_e4": (
                    effective_touch - int(episode.counterpart_lvl)
                ),
                "complement_flow_1s_fp": flows[1],
                "complement_flow_5s_fp": flows[5],
                "complement_flow_10s_fp": flows[10],
                "complement_flow_60s_fp": flows[60],
                "complement_flow_acceleration_fp": (
                    flows[10] - flows[60] / Decimal("6")
                ),
                "touch_imbalance": (
                    state["yes_touch_imbalance"] * complement_sign
                ),
                "spread_e4": state["spread_e4"],
                "mid_move_1s_e4": self._half_delta_int(
                    current_mid_x2,
                    mid1,
                    "mid_move_1s_e4",
                ),
                "mid_move_10s_e4": self._half_delta_int(
                    current_mid_x2,
                    mid10,
                    "mid_move_10s_e4",
                ),
                "mid_move_since_entry_e4": self._half_delta_int(
                    current_mid_x2,
                    meta["admission_mid_x2_e4"],
                    "mid_move_since_entry_e4",
                ),
                "mid_move_since_fill_e4": self._half_delta_int(
                    current_mid_x2,
                    meta["first_mid_x2_e4"],
                    "mid_move_since_fill_e4",
                ),
                "buy_complement_pnl_fee_low_usd": ioc_routes[
                    "BUY_COMPLEMENT"
                ]["pnl_fee_low_usd"],
                "buy_complement_pnl_fee_base_usd": ioc_routes[
                    "BUY_COMPLEMENT"
                ]["pnl_fee_base_usd"],
                "buy_complement_pnl_fee_high_usd": ioc_routes[
                    "BUY_COMPLEMENT"
                ]["pnl_fee_high_usd"],
                "buy_complement_executable_qty_fp": ioc_routes[
                    "BUY_COMPLEMENT"
                ]["executable_qty_fp"],
                "buy_complement_residual_inventory_fp": ioc_routes[
                    "BUY_COMPLEMENT"
                ]["residual_inventory_fp"],
                "sell_first_pnl_fee_low_usd": ioc_routes[
                    "SELL_FIRST_LEG"
                ]["pnl_fee_low_usd"],
                "sell_first_pnl_fee_base_usd": ioc_routes[
                    "SELL_FIRST_LEG"
                ]["pnl_fee_base_usd"],
                "sell_first_pnl_fee_high_usd": ioc_routes[
                    "SELL_FIRST_LEG"
                ]["pnl_fee_high_usd"],
                "sell_first_executable_qty_fp": ioc_routes[
                    "SELL_FIRST_LEG"
                ]["executable_qty_fp"],
                "sell_first_residual_inventory_fp": ioc_routes[
                    "SELL_FIRST_LEG"
                ]["residual_inventory_fp"],
                "pair_gain_if_complement_usd": pair_gain,
                "tte_ms": meta["first_tte_ms"] - elapsed_ms,
                "cancel_state": "NONE",
                "market_day_gate_pass": True,
                "reconciliation_ok": True,
                "data_invalid": False,
            }
            if common["tte_ms"] < 0:
                raise RuntimeError(
                    f"{episode.episode_id}: decision beyond hard boundary"
                )
            keep = {
                **common,
                "action_kind": "KEEP",
                "requested_qty_fp": Decimal("1"),
                "pair_cost_ceiling_e4": 9900,
                "cancel_ack_latency_ms": Decimal("0"),
                "legal_action": True,
                "skip_reason": None,
                "ioc_route": None,
                "ioc_limit_price_e4": None,
            }
            ioc = {
                **common,
                "action_kind": "IOC",
                "requested_qty_fp": Decimal("1"),
                "pair_cost_ceiling_e4": 9900,
                "cancel_ack_latency_ms": Decimal("60"),
                "legal_action": selected_ioc["legal_action"],
                "skip_reason": selected_ioc["skip_reason"],
                "ioc_route": selected_ioc["route"],
                "ioc_limit_price_e4": selected_ioc[
                    "ioc_limit_price_e4"
                ],
            }
            meta["decisions"][horizon_s] = (keep, ioc)
            meta["observed_trigger_audit"][horizon_s] = {
                "postfill_episode_id": episode.episode_id,
                "decision_index": decision_index,
                "scheduled_wall_ns": scheduled_wall,
                "scheduled_mono_ns": scheduled_mono,
                "observed_trigger_wall_ns": observed_wall,
                "observed_trigger_mono_ns": observed_mono,
                "observed_trigger_merge_key": self._key_json(
                    self._event_key(observed_trigger_fields)
                ),
                "feature_asof_merge_key": self._key_json(feature_key),
                "book_asof_merge_key": self._key_json(book_key),
            }
            meta["route_audit"][horizon_s] = {
                route: {
                    "executable_qty_fp": economics[
                        "executable_qty_fp"
                    ],
                    "residual_inventory_fp": economics[
                        "residual_inventory_fp"
                    ],
                    "pnl_fee_high_usd": economics[
                        "pnl_fee_high_usd"
                    ],
                }
                for route, economics in ioc_routes.items()
            }
            self.audit[f"decision_h{horizon_s:g}"] += 1

        def before_event(self, ts):
            fields = self.market_sim.current_event_fields
            if fields is not None:
                observed_wall = int(fields["recv_wall_ns"])
                for episode in self.active:
                    meta = self.compact.get(episode.episode_id)
                    if (
                        meta is None
                        or meta["zero_time_atom"] is not None
                        or episode.maker_fill_ts is not None
                    ):
                        continue
                    for horizon_s in HORIZONS_S[1:]:
                        if horizon_s in meta["decisions"]:
                            continue
                        scheduled_wall = (
                            int(meta["first_fields"]["recv_wall_ns"])
                            + int(round(horizon_s * 1_000_000_000))
                        )
                        if observed_wall >= scheduled_wall:
                            feature_fields = self.last_applied_event
                            if feature_fields is None:
                                raise RuntimeError(
                                    "no causal feature receipt before timer"
                                )
                            self._capture_state(
                                episode,
                                horizon_s,
                                fields,
                                feature_fields,
                            )
            super().before_event(ts)

        def apply_trade(self, ts, yes_px, qty, taker):
            before = {
                episode.episode_id: episode.maker_fill_ts
                for episode in self.active
            }
            super().apply_trade(ts, yes_px, qty, taker)
            fields = self.market_sim.current_event_fields
            current_key = self._event_key(fields)
            for episode in self.active:
                if (
                    before.get(episode.episode_id) is None
                    and episode.maker_fill_ts is not None
                ):
                    meta = self.compact[episode.episode_id]
                    receipt = {
                        "recv_wall_ns": int(fields["recv_wall_ns"]),
                        "recv_mono_ns": int(fields["recv_mono_ns"]),
                        "local_recv_ts_us": int(
                            fields["local_recv_ts_us"]
                        ),
                        "stable_source_id": str(fields["trade_id"]),
                        "merge_key": self._key_json(current_key),
                    }
                    meta["maker_fill_receipt"] = receipt
                    terminal_elapsed_ms = (
                        Decimal(
                            int(episode.maker_fill_ts)
                            - int(episode.first_ts)
                        )
                        / Decimal("1000")
                    )
                    for captured_horizon in tuple(meta["decisions"]):
                        captured_index = HORIZONS_S.index(
                            captured_horizon
                        )
                        captured_ms = Decimal(
                            PC.DECISION_GRID_MS[captured_index]
                        )
                        if captured_ms >= terminal_elapsed_ms:
                            del meta["decisions"][captured_horizon]
                            meta["observed_trigger_audit"].pop(
                                captured_horizon,
                                None,
                            )
                            meta["route_audit"].pop(
                                captured_horizon,
                                None,
                            )
                            self.audit[
                                "terminal_preempted_grid_rows"
                            ] += 1
                    first_envelope = (
                        int(meta["first_fields"]["recv_wall_ns"]),
                        int(meta["first_fields"]["recv_mono_ns"]),
                    )
                    current_envelope = (
                        int(fields["recv_wall_ns"]),
                        int(fields["recv_mono_ns"]),
                    )
                    if current_envelope == first_envelope:
                        if not meta["first_key"] < current_key:
                            raise RuntimeError(
                                "same-envelope atom merge order invalid"
                            )
                        meta["decisions"].clear()
                        meta["zero_time_atom"] = {
                            "postfill_episode_id": episode.episode_id,
                            "market_ticker": episode.market,
                            "source_rows_sha256": meta[
                                "source_rows_sha256"
                            ],
                            "first_fill_side": (
                                "YES"
                                if episode.held_side == "y"
                                else "NO"
                            ),
                            "complement_side": (
                                "YES"
                                if episode.counterpart_side == "y"
                                else "NO"
                            ),
                            "atom_recv_wall_ns": current_envelope[0],
                            "atom_recv_mono_ns": current_envelope[1],
                            "receipt_envelope_id": (
                                f"{current_envelope[0]}|"
                                f"{current_envelope[1]}"
                            ),
                            "first_fill_stable_source_id": str(
                                meta["first_fields"]["trade_id"]
                            ),
                            "complement_fill_stable_source_id": str(
                                fields["trade_id"]
                            ),
                            "first_fill_merge_key": self._key_json(
                                meta["first_key"]
                            ),
                            "complement_fill_merge_key": self._key_json(
                                current_key
                            ),
                            "complement_fill_price_e4": int(
                                episode.counterpart_lvl
                            ),
                            "complement_fill_qty_fp": Decimal("1"),
                            "complement_fill_fee_usd": Decimal("0"),
                            "reconciliation_ok": True,
                            "continuous_hazard_input_forbidden": True,
                            "postfill_action_forbidden": True,
                        }
                        meta["observed_trigger_audit"].clear()
                        meta["route_audit"].clear()
                        self.audit["same_envelope_atoms"] += 1

        def observe(self, ts, event_kind, event_fields):
            fields = self.market_sim.current_event_fields
            if event_kind == "trade":
                self._append_flow_trade()
            elif event_kind == "book":
                self._record_book_mid()
            current_key = self._event_key(fields)
            for episode in self.active:
                meta = self.compact.get(episode.episode_id)
                if (
                    meta is not None
                    and meta["zero_time_atom"] is None
                    and 0.0 not in meta["decisions"]
                    and current_key == meta["first_key"]
                ):
                    self._capture_state(
                        episode,
                        0.0,
                        fields,
                        fields,
                    )
            super().observe(ts, event_kind, event_fields)
            self.last_applied_event = dict(fields)

        def _emit_markouts(self, episode):
            before = len(self.markouts)
            super()._emit_markouts(episode)
            if len(self.markouts) != before + 1:
                raise RuntimeError("compact markout linkage failed")
            meta = self.compact[episode.episode_id]
            atom = meta["zero_time_atom"]
            if atom is None and 0.0 not in meta["decisions"]:
                raise RuntimeError("continuous episode lacks t=0 state")
            if atom is not None and meta["decisions"]:
                raise RuntimeError("zero-time atom leaked into decisions")
            first_fields = meta["first_fields"]
            maker_receipt = meta["maker_fill_receipt"]
            if atom is not None:
                terminal_type = "ZERO_TIME_ATOM"
                terminal_elapsed_ms = Decimal("0")
            elif (
                maker_receipt is not None
                and (
                    int(episode.maker_fill_ts)
                    - int(episode.first_ts)
                )
                <= 60_000_000
            ):
                terminal_type = "COMPLEMENT_FILL"
                terminal_elapsed_ms = (
                    Decimal(
                        int(episode.maker_fill_ts)
                        - int(episode.first_ts)
                    )
                    / Decimal("1000")
                )
            else:
                terminal_type = "ADMIN_CENSOR"
                terminal_elapsed_ms = Decimal("60000.001")
            terminal_ns_decimal = (
                terminal_elapsed_ms * Decimal("1000000")
            )
            if terminal_ns_decimal != terminal_ns_decimal.to_integral_value():
                raise RuntimeError("terminal clock is not exact nanoseconds")
            terminal_ns = int(terminal_ns_decimal)
            if terminal_elapsed_ms > Decimal(meta["first_tte_ms"]):
                raise RuntimeError(
                    f"{episode.episode_id}: terminal exceeds first-fill TTE"
                )
            manifest = {
                "experiment_id": PC.EXPERIMENT_ID,
                "data_role": "DISCOVERY",
                "source_date_utc": dt.datetime.fromtimestamp(
                    int(first_fields["local_recv_ts_us"]) / 1e6,
                    tz=dt.timezone.utc,
                ).date().isoformat(),
                "market_ticker": episode.market,
                "postfill_episode_id": episode.episode_id,
                "entry_episode_id": episode.episode_id,
                "entry_action_id": "P3_SPEND1_A1_SKIP",
                "source_rows_sha256": meta["source_rows_sha256"],
                "first_fill_side": (
                    "YES" if episode.held_side == "y" else "NO"
                ),
                "first_fill_price_e4": int(episode.entry_e4),
                "first_fill_qty_fp": Decimal("1"),
                "first_fill_fee_usd": Decimal("0"),
                "first_fill_recv_wall_ns": int(
                    first_fields["recv_wall_ns"]
                ),
                "first_fill_recv_mono_ns": int(
                    first_fields["recv_mono_ns"]
                ),
                "first_fill_elapsed_ms": (
                    Decimal(
                        int(episode.first_ts)
                        - int(episode.admission["ts"])
                    )
                    / Decimal("1000")
                ),
                "first_fill_tte_ms": meta["first_tte_ms"],
                "complement_order_id": meta["complement_order_id"],
                "complement_order_age_at_first_fill_ms": meta[
                    "complement_order_age_at_first_fill_ms"
                ],
                "terminal_elapsed_ms": terminal_elapsed_ms,
                "terminal_wall_ns": (
                    int(first_fields["recv_wall_ns"]) + terminal_ns
                ),
                "terminal_mono_ns": (
                    int(first_fields["recv_mono_ns"]) + terminal_ns
                ),
                "terminal_type": terminal_type,
                "zero_time_atom": atom is not None,
                "market_day_gate_pass": True,
                "reconciliation_ok": True,
                "data_invalid": False,
            }
            if atom is not None:
                manifest.update(
                    {
                        key: atom[key]
                        for key in (
                            "receipt_envelope_id",
                            "first_fill_stable_source_id",
                            "complement_fill_stable_source_id",
                            "complement_side",
                            "complement_fill_price_e4",
                            "complement_fill_qty_fp",
                            "complement_fill_fee_usd",
                        )
                    }
                )
            required_grid = PC.required_decision_grid(
                terminal_elapsed_ms,
                zero_time_atom=atom is not None,
            )
            actual_indices = [
                HORIZONS_S.index(horizon)
                for horizon in HORIZONS_S
                if horizon in meta["decisions"]
            ]
            if actual_indices != [
                index for index, _elapsed in required_grid
            ]:
                raise RuntimeError(
                    f"{episode.episode_id}: compact grid capture mismatch "
                    f"required={required_grid} actual={actual_indices}"
                )
            self.markouts[-1]["round4_compact"] = {
                "episode_manifest": manifest,
                "compact_rows": [
                    action_row
                    for horizon in HORIZONS_S
                    if horizon in meta["decisions"]
                    for action_row in meta["decisions"][horizon]
                ],
                "observed_trigger_audit": [
                    meta["observed_trigger_audit"][horizon]
                    for horizon in HORIZONS_S
                    if horizon in meta["observed_trigger_audit"]
                ],
                "route_audit": [
                    {
                        "postfill_episode_id": episode.episode_id,
                        "decision_index": HORIZONS_S.index(horizon),
                        **meta["route_audit"][horizon],
                    }
                    for horizon in HORIZONS_S
                    if horizon in meta["route_audit"]
                ],
                "terminal_observation_audit": (
                    None
                    if maker_receipt is None
                    else {
                        "postfill_episode_id": episode.episode_id,
                        "terminal_type": terminal_type,
                        "observed_receipt": maker_receipt,
                        "engine_terminal_elapsed_ms": (
                            Decimal(
                                int(episode.maker_fill_ts)
                                - int(episode.first_ts)
                            )
                            / Decimal("1000")
                        ),
                    }
                ),
            }

    class CompactMarket(A.EtaGuardedSampledMarket):
        def __init__(self, ticker, close_us, result, policies, writer):
            super().__init__(
                ticker,
                close_us,
                result,
                policies,
                writer,
            )
            self.current_event_fields = None
            self.tracker = CompactTracker(self, writer)

        def on_trade_causal(
            self,
            ts,
            yes_px,
            qty,
            taker,
            event_fields,
        ):
            self.current_event_fields = dict(event_fields)
            super().on_trade_causal(
                ts,
                yes_px,
                qty,
                taker,
                event_fields,
            )
            self.current_event_fields = None

        def on_book_causal(
            self,
            ts,
            mtype,
            side,
            px,
            delta,
            yes_levels,
            no_levels,
            ws_sid,
            ws_seq,
            event_fields,
        ):
            self.current_event_fields = dict(event_fields)
            super().on_book_causal(
                ts,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
                ws_sid,
                ws_seq,
                event_fields,
            )
            self.current_event_fields = None

    return R, CompactMarket


def load_full_contract():
    require_sha(
        FULL_CONTRACT_PATH,
        FULL_CONTRACT_SHA256,
        "FULL-COVERAGE Stage-2 contract",
    )
    spec = importlib.util.spec_from_file_location(
        "round4_postfill_state_contract_sealed",
        FULL_CONTRACT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def logical_source_paths():
    l2_glob = os.environ.get(
        "E4_L2_GLOB",
        "/home/ubuntu/hft-bot/work/warehouse/facts/"
        "orderbooks_full/category=Crypto/**/*.parquet",
    )
    trade_glob = os.environ.get(
        "E4_TR_GLOB",
        "/home/ubuntu/hft-bot/work/warehouse/facts/"
        "trades/category=Crypto/**/*.csv.gz",
    )
    l2_root = l2_glob.split("/**", 1)[0]
    trade_root = trade_glob.split("/**", 1)[0]
    return {
        day: (
            f"{l2_root}/subcategory=*/date={day}/*.parquet",
            f"{trade_root}/subcategory=*/date={day}/*.csv.gz",
        )
        for day in DATES
    }


def load_expected_episode_ids_by_day():
    require_sha(
        SEALED_EPISODES_PATH,
        SEALED_EPISODES_SHA256,
        "sealed episode identity receipt",
    )
    with gzip.open(
        SEALED_EPISODES_PATH,
        "rt",
        encoding="utf-8",
    ) as handle:
        sealed = json.load(handle)
    if sealed.get("date_2026_07_23_read") is not False:
        raise RuntimeError("sealed episode receipt read forbidden date")
    rows = [
        row
        for row in sealed["policy_rows"]
        if row["policy"] == "IOC_60MS"
    ]
    ids = {row["episode_id"] for row in rows}
    if len(rows) != EXPECTED_FIRST_FILLS or len(ids) != EXPECTED_FIRST_FILLS:
        raise RuntimeError("sealed episode identity count mismatch")
    grouped = {day: [] for day in DATES}
    for row in rows:
        day = dt.datetime.fromtimestamp(
            int(row["first_ts"]) / 1e6,
            tz=dt.timezone.utc,
        ).date().isoformat()
        if day not in grouped:
            raise RuntimeError(
                f"sealed episode outside discovery dates: {day}"
            )
        grouped[day].append(str(row["episode_id"]))
    if any(not grouped[day] for day in DATES):
        raise RuntimeError("empty sealed market-day episode roster")
    return {
        day: tuple(sorted(grouped[day]))
        for day in DATES
    }


def deterministic_gzip_json(path, value):
    with open(path, "wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=6,
            mtime=0,
        ) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8") as text:
                json.dump(
                    value,
                    text,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )


def synthetic_test(PC):
    full = visible_ioc_routes({3000: 10_000}, 3200)
    assert full["BUY_COMPLEMENT"]["complete"] is True
    assert full["BUY_COMPLEMENT"]["gross_pnl_usd"] == Decimal("-0.02")
    assert (
        full["BUY_COMPLEMENT"]["pnl_fee_low_usd"]
        >= full["BUY_COMPLEMENT"]["pnl_fee_base_usd"]
        >= full["BUY_COMPLEMENT"]["pnl_fee_high_usd"]
    )
    partial = visible_ioc_routes({3000: 2_500}, 3200)
    assert partial["BUY_COMPLEMENT"]["complete"] is False
    assert partial["BUY_COMPLEMENT"]["residual_inventory_fp"] == Decimal(
        "0.75"
    )
    assert select_ioc_route(partial)["legal_action"] is False

    def route(route, complete, high):
        return {
            "route": route,
            "complete": complete,
            "pnl_fee_high_usd": Decimal(high),
            "ioc_limit_price_e4": (
                7000 if route == "BUY_COMPLEMENT" else 3000
            ),
        }

    # Higher-PnL partial liquidity is never allowed to beat a complete route.
    high_partial = {
        "BUY_COMPLEMENT": route(
            "BUY_COMPLEMENT",
            False,
            "0.50",
        ),
        "SELL_FIRST_LEG": route(
            "SELL_FIRST_LEG",
            True,
            "-0.10",
        ),
    }
    assert select_ioc_route(high_partial)["route"] == "SELL_FIRST_LEG"
    # Among complete routes, selection is by conservative fee-high PnL.
    distinct = {
        "BUY_COMPLEMENT": route(
            "BUY_COMPLEMENT",
            True,
            "-0.03",
        ),
        "SELL_FIRST_LEG": route(
            "SELL_FIRST_LEG",
            True,
            "-0.01",
        ),
    }
    assert select_ioc_route(distinct)["route"] == "SELL_FIRST_LEG"
    # Exact tie is deterministic and sealed to BUY_COMPLEMENT.
    tie = {
        "BUY_COMPLEMENT": route(
            "BUY_COMPLEMENT",
            True,
            "-0.01",
        ),
        "SELL_FIRST_LEG": route(
            "SELL_FIRST_LEG",
            True,
            "-0.01",
        ),
    }
    assert select_ioc_route(tie)["route"] == "BUY_COMPLEMENT"
    both_partial = {
        "BUY_COMPLEMENT": route(
            "BUY_COMPLEMENT",
            False,
            "0.20",
        ),
        "SELL_FIRST_LEG": route(
            "SELL_FIRST_LEG",
            False,
            "0.30",
        ),
    }
    rejected = select_ioc_route(both_partial)
    assert rejected["legal_action"] is False
    assert rejected["skip_reason"] == (
        "IOC_ALL_ROUTES_INCOMPLETE_RESIDUAL_HELD"
    )
    first_key = (100, 10, 1, (1 << 63) - 1, "a")
    complement_key = (100, 10, 1, (1 << 63) - 1, "b")
    assert first_key < complement_key
    assert PC.required_decision_grid(
        Decimal("500"),
        zero_time_atom=False,
    ) == ((0, 0), (1, 250))
    assert PC.required_decision_grid(
        Decimal("60000.001"),
        zero_time_atom=False,
    )[-1] == (8, 60000)
    return {
        "status": "FULL_COVERAGE_COMPACT_SYNTHETIC_OK",
        "fee_band_monotonic": True,
        "partial_high_pnl_route_ineligible": True,
        "complete_high_fee_pnl_route_selected": True,
        "exact_tie_buy_complement": True,
        "both_partial_illegal_residual_held": True,
        "same_envelope_full_key_order": True,
        "terminal_preempts_equal_grid": True,
        "survivor_includes_60s_grid": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument(
        "--report-out",
        default="/tmp/z3_round4_full_coverage_compact_report.json",
    )
    parser.add_argument(
        "--rows-out",
        default="/tmp/z3_round4_full_coverage_compact_rows.json.gz",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    script_sha = file_sha(__file__)
    with open(args.prereg, "rb") as handle:
        prereg_bytes = handle.read()
    prereg = json.loads(prereg_bytes)
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("extractor prereg script SHA mismatch")
    if prereg.get("spec") != PREREGISTRATION:
        raise RuntimeError("extractor prereg spec mismatch")
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()
    require_sha(ROUND4_DDL_PATH, ROUND4_DDL_SHA256, "ROUND4 DDL")
    require_sha(
        ROUND4_DESIGN_PATH,
        ROUND4_DESIGN_SHA256,
        "ROUND4 design prereg",
    )
    require_sha(
        FEE_BOUNDARY_PATH,
        FEE_BOUNDARY_SHA256,
        "fee precision boundary",
    )
    require_sha(
        FULL_CONTRACT_MANIFEST_PATH,
        FULL_CONTRACT_MANIFEST_SHA256,
        "FULL-COVERAGE contract manifest",
    )
    require_sha(
        FULL_CONTRACT_DDL_PATH,
        FULL_CONTRACT_DDL_SHA256,
        "FULL-COVERAGE supplemental DDL",
    )
    PC = load_full_contract()
    synthetic = synthetic_test(PC)
    if args.self_test:
        print(json.dumps(synthetic, indent=2))
        return

    expected_by_day = load_expected_episode_ids_by_day()
    guarded_paths = logical_source_paths()
    normalized_expected, normalized_guarded = (
        PC.preflight_canary_postfill_paths(
            guarded_paths,
            expected_by_day,
        )
    )
    expected_ids = {
        episode_id
        for ids in normalized_expected.values()
        for episode_id in ids
    }
    A = load_sealed_a1()
    R, CompactMarket = build_engine_hooks(A, PC)
    R.CausalTrackedMarket = CompactMarket
    R.D.JsonlGzipWriter = NullWriter
    tasks = [
        (date, f"/tmp/compact_null_{date}.jsonl.gz")
        for date in DATES
    ]
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print("RUN FULL-COVERAGE COMPACT 2026-07-20..22", flush=True)
    with R.mp.Pool(processes=3) as pool:
        day_results = pool.map(R.run_day, tasks)

    episode_manifest = []
    compact_rows = []
    trigger_audits = []
    route_audits = []
    terminal_audits = []
    data_audits = []
    quality = Counter()
    per_date = {}
    for date, _path, writer_rows, aggregate in day_results:
        if writer_rows != 0:
            raise RuntimeError("trajectory writer unexpectedly emitted")
        data_audits.append(aggregate["data_audit"])
        date_episodes = date_atoms = date_states = 0
        for market_audit in aggregate["data_audit"][
            "market_audit"
        ].values():
            quality.update(
                {key: int(value) for key, value in market_audit.items()}
            )
        for markout in aggregate["markouts"]:
            compact = markout.get("round4_compact")
            if compact is None:
                raise RuntimeError("markout lacks compact extraction")
            manifest = compact["episode_manifest"]
            if manifest["source_date_utc"] != date:
                raise RuntimeError("worker date/episode date mismatch")
            episode_manifest.append(manifest)
            compact_rows.extend(compact["compact_rows"])
            trigger_audits.extend(compact["observed_trigger_audit"])
            route_audits.extend(compact["route_audit"])
            terminal_audit = compact["terminal_observation_audit"]
            if terminal_audit is not None:
                terminal_audits.append(terminal_audit)
            date_episodes += 1
            date_atoms += int(manifest["zero_time_atom"])
            date_states += len(compact["compact_rows"]) // 2
        per_date[date] = {
            "episode_manifest_rows": date_episodes,
            "zero_time_atom_rows": date_atoms,
            "causal_state_rows": date_states,
            "causal_action_rows": date_states * 2,
            "markets": len(aggregate["data_audit"]["market_audit"]),
            "trajectory_rows_emitted": 0,
        }
    failure_fields = (
        "negative_level_failures",
        "clock_null_failures",
        "clock_identity_failures",
        "sequence_failures",
        "A1_failures",
    )
    failures = {
        key: quality[key]
        for key in failure_fields
        if quality[key] != 0
    }
    if failures:
        raise RuntimeError(f"causal source gate failed {failures}")
    batch = PC.validate_and_serialize_postfill_rows(
        logical_source_paths=guarded_paths,
        expected_episode_ids_by_day=expected_by_day,
        episode_manifest=episode_manifest,
        compact_rows=compact_rows,
    )
    contract_receipt = batch.receipt()
    if contract_receipt["episode_count"] != EXPECTED_FIRST_FILLS:
        raise RuntimeError("contract episode coverage mismatch")
    if set(batch.source_dates) != set(DATES):
        raise RuntimeError("contract source-date coverage mismatch")
    if tuple(
        sorted(
            os.fspath(path)
            for paths in normalized_guarded.values()
            for path in paths
        )
    ) != batch.guarded_source_paths:
        raise RuntimeError("contract guarded-path receipt drift")

    decision_horizon_counts = Counter(
        str(row["elapsed_since_first_fill_ms"])
        for row in batch.postfill_compact_causal_state
    )
    terminal_counts = Counter(
        row["terminal_type"] for row in episode_manifest
    )
    action_counts = Counter(
        row["action_kind"]
        for row in batch.postfill_compact_causal_action
    )
    ioc_route_counts = Counter(
        (
            row["ioc_route"]
            if row["legal_action"]
            else "ILLEGAL_INCOMPLETE"
        )
        for row in batch.postfill_compact_causal_action
        if row["action_kind"] == "IOC"
    )
    route_completeness = Counter()
    for audit in route_audits:
        for route in ("BUY_COMPLEMENT", "SELL_FIRST_LEG"):
            residual = audit[route]["residual_inventory_fp"]
            route_completeness[
                f"{route}|{'COMPLETE' if residual == 0 else 'PARTIAL'}"
            ] += 1
    trigger_delays = sorted(
        int(row["observed_trigger_wall_ns"])
        - int(row["scheduled_wall_ns"])
        for row in trigger_audits
    )
    if any(delay < 0 for delay in trigger_delays):
        raise RuntimeError("negative observed trigger delay")
    source_id_sha = sha_value(sorted(expected_ids))
    rows_payload = {
        "schema": "z3-round4-full-coverage-contract-batch-v1",
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate": False,
        "deployable": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "contract_receipt": contract_receipt,
        "tables": _jsonable(batch.as_ddl_rows()),
    }
    deterministic_gzip_json(args.rows_out, rows_payload)
    rows_sha = file_sha(args.rows_out)
    report = {
        "schema": "z3-round4-full-coverage-compact-report-v1",
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate": False,
        "validation": False,
        "deployable": False,
        "live_authorized": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "source": {
            "script_sha256": script_sha,
            "preregistration_sha256": prereg_sha,
            "sealed_a1_replay_sha256": SEALED_A1_SHA256,
            "sealed_episode_receipt_sha256": SEALED_EPISODES_SHA256,
            "round4_ddl_sha256": ROUND4_DDL_SHA256,
            "round4_design_prereg_sha256": ROUND4_DESIGN_SHA256,
            "fee_precision_boundary_sha256": FEE_BOUNDARY_SHA256,
            "full_coverage_contract_sha256": FULL_CONTRACT_SHA256,
            "full_coverage_contract_manifest_sha256": (
                FULL_CONTRACT_MANIFEST_SHA256
            ),
            "full_coverage_supplemental_ddl_sha256": (
                FULL_CONTRACT_DDL_SHA256
            ),
        },
        "preregistration": PREREGISTRATION,
        "synthetic_preflight": synthetic,
        "coverage": {
            "episode_manifest_rows": len(episode_manifest),
            "sealed_episode_identity_sha256": source_id_sha,
            "markets": len(
                {row["market_ticker"] for row in episode_manifest}
            ),
            "zero_time_atom_rows": len(
                batch.postfill_zero_time_atom
            ),
            "causal_state_rows": len(
                batch.postfill_compact_causal_state
            ),
            "causal_action_rows": len(
                batch.postfill_compact_causal_action
            ),
            "decision_horizon_counts": dict(
                sorted(
                    decision_horizon_counts.items(),
                    key=lambda item: int(item[0]),
                )
            ),
            "keep_terminal_counts": dict(terminal_counts),
            "action_counts": dict(action_counts),
            "ioc_route_counts": dict(ioc_route_counts),
            "per_date": per_date,
            "trajectory_sampling_used": False,
            "trajectory_rows_emitted": 0,
        },
        "causal_gate": {
            "pass": True,
            "failure_fields": {
                key: quality[key] for key in failure_fields
            },
            "aggregate_market_audit": dict(quality),
            "per_day": data_audits,
        },
        "contract_receipt": contract_receipt,
        "extraction_audit": {
            "observed_trigger_rows": len(trigger_audits),
            "observed_trigger_receipt_sha256": sha_value(
                _jsonable(
                    sorted(
                        trigger_audits,
                        key=lambda row: (
                            row["postfill_episode_id"],
                            row["decision_index"],
                        ),
                    )
                )
            ),
            "observed_trigger_delay_ns": {
                "min": trigger_delays[0] if trigger_delays else None,
                "p50_order_statistic": (
                    trigger_delays[len(trigger_delays) // 2]
                    if trigger_delays
                    else None
                ),
                "max": trigger_delays[-1] if trigger_delays else None,
            },
            "route_completeness_counts": dict(route_completeness),
            "route_receipt_sha256": sha_value(
                _jsonable(
                    sorted(
                        route_audits,
                        key=lambda row: (
                            row["postfill_episode_id"],
                            row["decision_index"],
                        ),
                    )
                )
            ),
            "terminal_observation_rows": len(terminal_audits),
            "terminal_observation_receipt_sha256": sha_value(
                _jsonable(
                    sorted(
                        terminal_audits,
                        key=lambda row: row["postfill_episode_id"],
                    )
                )
            ),
        },
        "fee_precision": {
            "classification": FEE_CLASSIFICATION,
            "private_fill_exact": False,
            "l2_gross_exact": True,
            "residual_settlement_imputation_used": False,
        },
        "actions": {
            "extracted": ["KEEP", "IOC"],
            "reprice_rows": 0,
            "reprice_disabled_reason": (
                "NO_CAUSAL_CANCEL_REPLACE_QUEUE_RESET_EVIDENCE"
            ),
        },
        "rows_file": {
            "path": args.rows_out,
            "sha256": rows_sha,
            "bytes": Path(args.rows_out).stat().st_size,
        },
        "run_completed_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
    }
    report_bytes = (
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    with open(args.report_out, "wb") as handle:
        handle.write(report_bytes)
    print(
        json.dumps(
            {
                "status": "FULL_COVERAGE_COMPACT_OK",
                "coverage": report["coverage"],
                "contract_receipt": contract_receipt,
                "fee_precision": report["fee_precision"],
                "report_out": args.report_out,
                "report_sha256": hashlib.sha256(
                    report_bytes
                ).hexdigest(),
                "rows_out": args.rows_out,
                "rows_sha256": rows_sha,
                "date_2026_07_23_read": False,
                "date_2026_07_26_read": False,
            },
            indent=2,
            allow_nan=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
