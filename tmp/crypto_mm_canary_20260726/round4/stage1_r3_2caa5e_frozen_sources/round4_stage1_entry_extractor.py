#!/usr/bin/env python3
"""Full-roster causal Stage-1 entry extraction for ROUND4.

This discovery-only extractor instruments the sealed receive-clock A1-skip
replay at the entry decision, ACK/risk-start, strict trade-through fill and
two-sided cancel-ACK boundaries.  Unlike the existing post-fill artifact, it
keeps the complete denominator: ENTRY_SKIP, ACK_FAILED, YES_FIRST, NO_FIRST
and ADMIN_CENSOR_NO_FIRST_FILL.

The historical engine is a shadow simulator: successful pair placement is an
immediate synthetic BOTH_ACKED transition and contains no private wire ACK
facts.  Therefore historical ACK_FAILED is explicitly supported by the table
contract but is expected to be zero and is not estimable from this run.

This file does not fit a model, select an action, create an action-set seal,
authorize deployment, or trade.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import copy
import datetime as dt
from decimal import Decimal
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
STAGE1_CONTRACT_PATH = os.environ.get(
    "ROUND4_STAGE1_CONTRACT",
    "/home/ubuntu/hft-bot/tools/research/crypto_mm/"
    "round4_stage1_entry_contract.py",
)
STAGE1_CONTRACT_SHA256 = (
    "7b038b8f97831ddda6ffcfce5580ffbd71cd9a8bdf095cf2bb4c41ca7c03989a"
)
TABLE_BUILDER_PATH = os.environ.get(
    "ROUND4_TABLE_BUILDER",
    "/home/ubuntu/hft-bot/tools/research/crypto_mm/"
    "round4_table_builder.py",
)
TABLE_BUILDER_SHA256 = (
    "e4200628e26e46fa6dbd8637f610907805ad1d31e3d28fb08ef14276da212b32"
)
ROUND4_DDL_PATH = os.environ.get(
    "ROUND4_DDL",
    "/tmp/round4_two_stage_tables.sql",
)
ROUND4_DDL_SHA256 = (
    "ede23569a00e9948cd3772cc2213a07f8e5c3b9849b9dd0f46e8e6adff3eaed3"
)
STAGE1_DDL_PATH = os.environ.get(
    "ROUND4_STAGE1_DDL",
    "/tmp/round4_stage1_entry_supplemental.sql",
)
STAGE1_DDL_SHA256 = (
    "13402e5c8e94574365eee8e6a56a6276c5922a921cf1978ea939863cbe07b654"
)
ROUND4_DESIGN_PATH = os.environ.get(
    "ROUND4_DESIGN_PREREG",
    "/tmp/round4_two_stage_hazard_prereg.json",
)
ROUND4_DESIGN_SHA256 = (
    "c3c01c61971e5649f9d5f5a5fcf64b66881f64233452c70a4639a51f5c9e05ef"
)

DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FORBIDDEN_DATES = ("2026-07-23", "2026-07-26")
EXPECTED_FIRST_FILLS = 1_202
EXPECTED_FIRST_FILL_MARKETS = 58
ENTRY_BOUNDARIES_MS = (
    Decimal("0"),
    Decimal("1000"),
    Decimal("2000"),
    Decimal("5000"),
    Decimal("15000"),
    Decimal("30000"),
    Decimal("60000"),
    Decimal("120000"),
    Decimal("300000"),
)
E4 = Decimal("10000")
CLIP_E4 = 10_000
PAIR_LOCK_E4 = 9_900
LATENCY_NS = 60_000_000
TRADE_PRIORITY = 1


PREREGISTRATION = {
    "schema": "z3-round4-stage1-entry-full-roster-prereg-v3",
    "status": "DISCOVERY_EXTRACTION_ONLY",
    "candidate_status": "ACTION_SET_PENDING",
    "candidate": False,
    "validation": False,
    "deployable": False,
    "live_authorized": False,
    "dates": list(DATES),
    "forbidden_dates": list(FORBIDDEN_DATES),
    "revision": {
        "id": "R3_PRE_RUN_CAUSAL_GAP_SENTINEL",
        "superseded_r2": {
            "script_sha256": (
                "d0f719a6c42f444dbb47501f59c85f61f3c6ef697ca90ec5e799b1d41146c067"
            ),
            "prereg_sha256": (
                "ef17552358c29490f99be72b69b7a5ca1e2a07fd16ae2b470d4e1e5e89b538f7"
            ),
            "revision_receipt_sha256": (
                "82d14c448811b3fd3626d691eadcba101f4fdbeed6780f0dc24a22c4830dcd99"
            ),
            "manifest_sha256": (
                "49a96183519b2c796ed8f31c6bb3b78cfb3d71a97c45acf552cc56f11f95e60c"
            ),
            "status": "SUPERSEDED_BEFORE_RUN",
            "remote_upload_started": False,
            "remote_run_started": False,
            "results_eligible": False,
            "partial_artifacts_written": False,
            "reason": (
                "missing BOOK receipts were skipped instead of recorded as "
                "causal NULL sentinels, so a lag lookup could cross the gap"
            ),
        },
        "earlier_c796_attempt": {
            "script_sha256": (
                "c796907e56960756031c989eb363e1f0c4884e0fb459d09847cd02f09aa39a10"
            ),
            "prereg_sha256": (
                "f9e3163123c4d1cfc1bf4b2965eacf3f7fb1cc215e78494124cb354685eb2bea"
            ),
            "status": "VOID_CODE_CONTRACT_OVERSTRICT",
            "results_eligible": False,
            "partial_artifacts_written": False,
            "first_error_market": "KXBTC15M-26JUL212015-15",
            "first_error": "passive mid recorder rejected missing public touch",
            "remote_log_sha256": (
                "639e10e31fc194da935545807a692e3a5a771950ca1117f75d3cc4e1a8962e64"
            ),
        },
        "economic_or_action_change": False,
        "correction": (
            "every one-sided/empty BOOK receipt appends an explicit NULL "
            "midpoint sentinel; lag as-of lookup returns the last receipt "
            "state at the target time and therefore cannot cross a gap"
        ),
        "missing_indicator": (
            "spread_e4 IS NULL; no imputation, stale midpoint carry, or "
            "extractor-selected model feature is allowed"
        ),
    },
    "source_replay": {
        "sealed_a1_skip_sha256": SEALED_A1_SHA256,
        "sealed_first_fill_receipt_sha256": SEALED_EPISODES_SHA256,
        "reused_semantics": [
            "local receipt clock and deterministic BOOK-before-TRADE merge",
            "A1 post-allocation finite-positive ETA guard",
            "P3_SPEND1 legal post-only allocation",
            "strict trade-through cumulative volume > queue ahead + clip",
            "60ms simulated cancel-ACK window",
        ],
        "not_reused_as_denominator": (
            "the 1202 sealed post-fill episodes contain only first fills and "
            "must never be used to infer NO_FIRST_FILL"
        ),
    },
    "decision_roster": {
        "unit": (
            "each frozen-engine eligible entry decision while flat and "
            "without a live quote"
        ),
        "partition": [
            "ENTRY_SKIP",
            "ENTRY_PAIR_POST_ONLY",
        ],
        "entry_skip": (
            "allocation/support/post-only/pair-cost/A1 rejection at that "
            "decision receipt; instantaneous, not admitted, zero capital"
        ),
        "pair_post_only": (
            "frozen P3 allocation with two synthetic shadow ACKs at the "
            "decision receipt"
        ),
        "nonoverlap": "at most one active cycle per market/policy",
    },
    "ack_boundary": {
        "historical_observation": "SHADOW_IMMEDIATE_BOTH_ACKED",
        "wire_ack_available": False,
        "ack_failed_historical_expected": 0,
        "ack_failed_contract": (
            "explicit pre-risk terminal retained for synthetic tests and "
            "future capture; it may carry rollback reservation capital but "
            "must have no Stage-1 risk interval"
        ),
        "inference_limit": (
            "this replay cannot estimate real ACK failure probability"
        ),
    },
    "stage1_risk": {
        "starts": "only after BOTH_ACKED",
        "causes": ["YES_FIRST", "NO_FIRST"],
        "administrative_terminal": "ADMIN_CENSOR_NO_FIRST_FILL",
        "time_bins_ms": [format(value, "f") for value in ENTRY_BOUNDARIES_MS],
        "dynamic_state": (
            "last fully applied receipt at each fixed interval start; the "
            "crossing event is not applied"
        ),
        "no_first_fill_source": (
            "directly reconstructed from every BOTH_ACKED active entry that "
            "reaches two-sided cancel ACK or source end before a first fill"
        ),
        "zero_duration": (
            "fail closed; never fabricate an epsilon Stage-1 interval"
        ),
        "missing_public_touch": {
            "passive_recorder": (
                "increment passive_mid_skipped_missing_touch and append an "
                "explicit NULL midpoint sentinel for that BOOK receipt"
            ),
            "active_grid": (
                "retain queue/depth/flow; set touch_imbalance, spread_e4, "
                "mid_move_1s_e4 and mid_move_10s_e4 to NULL"
            ),
            "lag_asof": (
                "return the midpoint or NULL of the last BOOK receipt at or "
                "before the target time; a missing sentinel is never crossed"
            ),
            "indicator": "spread_e4 IS NULL",
            "stale_midpoint_carry_forbidden": True,
            "imputation_forbidden": True,
            "model_missing_indicators": (
                "deferred to fold-safe model fitting; not created here"
            ),
        },
    },
    "capital": {
        "pre_first_fill_locked_usd": (
            "(yes_price_e4+no_price_e4)/10000 * clip"
        ),
        "capital_dollar_seconds": (
            "integral of explicit reservation segments over receipt time"
        ),
        "no_first_fill_included": True,
        "entry_skip_zero": True,
        "ack_failed_rollback_segment_supported": True,
    },
    "clock_and_data": {
        "engine_clock": "local_recv_ts_us=recv_wall_ns//1000",
        "merge_key": (
            "(recv_wall_ns,recv_mono_ns,channel_priority,"
            "ws_seq_or_sentinel,stable_source_id)"
        ),
        "source_preflight": "sealed replay checks date-scoped paths before open",
        "data_invalid": (
            "discard the entire market-day/all actions and fail the worker"
        ),
        "partial_artifact_on_failure": False,
    },
    "whole_policy_scope": {
        "roster_dependency": (
            "the entry roster inherits the frozen P3 current post-fill "
            "exit/re-entry state machine"
        ),
        "counterfactual_warning": (
            "a new KEEP/IOC policy changes capital release and subsequent "
            "entry opportunities; independent first-fill trajectories cannot "
            "be appended to this roster to claim whole-policy EV"
        ),
        "required_final_step": (
            "integrated non-overlapping entry+post-fill replay with the "
            "sealed candidate policy, including all no-fill cycles and "
            "capital-time"
        ),
        "whole_policy_economics_ready": False,
    },
    "contracts": {
        "round4_design_sha256": ROUND4_DESIGN_SHA256,
        "round4_base_ddl_sha256": ROUND4_DDL_SHA256,
        "stage1_contract_sha256": STAGE1_CONTRACT_SHA256,
        "round4_table_builder_sha256": TABLE_BUILDER_SHA256,
        "stage1_supplemental_ddl_sha256": STAGE1_DDL_SHA256,
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
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def sha_value(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
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


def public_touch_snapshot(books):
    """Return a two-sided public-touch snapshot, or ``None`` if unavailable."""
    touches = {}
    for side in ("y", "n"):
        side_book = books.get(side, {})
        price = max(
            (
                int(level)
                for level, quantity in side_book.items()
                if int(quantity or 0) > 0
            ),
            default=None,
        )
        if price is None:
            return None
        touches[side] = (
            price,
            int(side_book[price] or 0),
        )
    yb, yq = touches["y"]
    nb, nq = touches["n"]
    yes_ask = 10_000 - nb
    spread = yes_ask - yb
    if spread < 0:
        raise RuntimeError("crossed causal public book")
    denominator = yq + nq
    imbalance = (
        Decimal(yq - nq) / Decimal(denominator)
        if denominator
        else Decimal("0")
    )
    return {
        "yes_bid_e4": yb,
        "no_bid_e4": nb,
        "yes_ask_e4": yes_ask,
        "spread_e4": spread,
        "mid_x2_e4": yb + yes_ask,
        "touch_imbalance": imbalance,
    }


def append_causal_mid_receipt(history, fields, snapshot):
    """Append one BOOK receipt, including an explicit missing-touch sentinel.

    A missing public touch is a real causal state, not an absence of data in
    this history.  Recording ``None`` prevents later lag lookups from walking
    backwards through a one-sided/empty-book gap to a stale midpoint.
    """
    wall = int(fields["recv_wall_ns"])
    identity = str(fields["book_stable_id"])
    if history and history[-1][2] == identity:
        if int(history[-1][0]) != wall:
            raise RuntimeError("duplicate book identity changed receipt time")
        return False
    mid_x2 = (
        None
        if snapshot is None
        else int(snapshot["mid_x2_e4"])
    )
    history.append((wall, mid_x2, identity))
    cutoff = wall - 11_000_000_000
    while history and int(history[0][0]) < cutoff:
        history.popleft()
    return True


def causal_mid_x2_asof(history, wall_ns):
    """Return the last BOOK-receipt midpoint at target time, including NULL."""
    target = int(wall_ns)
    for wall, mid_x2, _identity in reversed(history):
        if int(wall) <= target:
            return None if mid_x2 is None else int(mid_x2)
    return None


def touch_dependent_risk_features(
    snapshot,
    *,
    mid_1s_x2=None,
    mid_10s_x2=None,
):
    """Materialize the all-NULL missing-touch bundle without imputation."""
    if snapshot is None:
        return {
            "touch_imbalance": None,
            "spread_e4": None,
            "mid_move_1s_e4": None,
            "mid_move_10s_e4": None,
        }

    def half_move(old_x2, label):
        if old_x2 is None:
            return None
        delta = int(snapshot["mid_x2_e4"]) - int(old_x2)
        if delta % 2:
            raise RuntimeError(
                f"{label}: half-e4 midpoint move is outside DDL"
            )
        return delta // 2

    return {
        "touch_imbalance": snapshot["touch_imbalance"],
        "spread_e4": int(snapshot["spread_e4"]),
        "mid_move_1s_e4": half_move(mid_1s_x2, "mid_move_1s"),
        "mid_move_10s_e4": half_move(mid_10s_x2, "mid_move_10s"),
    }


def load_module(path, expected_sha, name, label):
    require_sha(path, expected_sha, label)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_expected_first_fill_ids():
    require_sha(
        SEALED_EPISODES_PATH,
        SEALED_EPISODES_SHA256,
        "sealed first-fill receipt",
    )
    with gzip.open(SEALED_EPISODES_PATH, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if (
        payload.get("date_2026_07_23_read") is not False
        or payload.get("status") != "DISCOVERY_ONLY"
    ):
        raise RuntimeError("sealed first-fill receipt scope mismatch")
    ids = {
        str(row["episode_id"])
        for row in payload["policy_rows"]
        if row["policy"] == "IOC_60MS"
    }
    if len(ids) != EXPECTED_FIRST_FILLS:
        raise RuntimeError("sealed first-fill identity count mismatch")
    return ids


class NullStage1Writer:
    """Suppress trajectory JSON while returning staged market payloads."""

    def __init__(self, path, date):
        self.path = path
        self.date = date
        self.market_payloads = []

    @property
    def rows(self):
        return self.market_payloads

    def emit(self, _row):
        return None

    def add_market_payload(self, payload):
        self.market_payloads.append(payload)

    def close(self):
        return None


def build_engine_hooks(A, PC):
    """Return the sealed runner and an entry-instrumented market class."""
    R = A.F

    class EntryRosterMarket(A.EtaGuardedSampledMarket):
        def __init__(self, ticker, close_us, result, policies, writer):
            super().__init__(ticker, close_us, result, policies, writer)
            self.stage1_writer = writer
            self.current_event_fields = None
            self.last_applied_fields = None
            self.mid_history = deque()
            self.active_entry = {}
            self.entry_rows = []
            self.risk_rows = []
            self.capital_rows = []
            self.outcome_rows = []
            self.stage1_audit = Counter()
            self._payload_emitted = False

        def _book_state(self, *, require_two_sided=True):
            snapshot = public_touch_snapshot(self.books)
            if snapshot is None and require_two_sided:
                raise RuntimeError(
                    f"{self.ticker}: entry decision missing causal touch"
                )
            return snapshot

        def _record_mid(self, fields):
            if (
                fields is None
                or int(fields.get("causal_channel_priority", -1)) != 0
            ):
                return
            book = self._book_state(require_two_sided=False)
            appended = append_causal_mid_receipt(
                self.mid_history,
                fields,
                book,
            )
            if appended and book is None:
                self.stage1_audit[
                    "passive_mid_skipped_missing_touch"
                ] += 1

        def _mid_x2_asof(self, wall_ns):
            return causal_mid_x2_asof(self.mid_history, wall_ns)

        def _flow(self, side, level_e4, scheduled_us, window_s):
            lower = int(scheduled_us) - int(window_s * 1_000_000)
            total = 0
            for trade_us, yes_price, qty, taker in self.recent_trades:
                if not lower < int(trade_us) <= int(scheduled_us):
                    continue
                if side == "y":
                    eligible = taker == "no" and int(yes_price) <= level_e4
                else:
                    eligible = (
                        taker == "yes"
                        and 10_000 - int(yes_price) <= level_e4
                    )
                if eligible:
                    total += int(qty)
            return Decimal(total) / E4

        def _state_for_active(
            self,
            policy,
            state,
            active,
            elapsed_ms,
            scheduled_wall_ns,
        ):
            yes_quote = state.quotes["y"]
            no_quote = state.quotes["n"]
            if yes_quote is None or no_quote is None:
                raise RuntimeError(
                    f"{active['entry_episode_id']}: incomplete entry quote "
                    "before Stage-1 terminal"
                )
            scheduled_us = int(scheduled_wall_ns) // 1_000
            feature_fields = (
                active["decision_fields"]
                if elapsed_ms == 0
                else self.last_applied_fields
            )
            if feature_fields is None:
                raise RuntimeError("Stage-1 feature state has no receipt")
            feature_wall = int(feature_fields["recv_wall_ns"])
            if feature_wall > int(scheduled_wall_ns):
                raise RuntimeError("Stage-1 feature lookahead")
            book = self._book_state(require_two_sided=False)
            if book is None:
                self.stage1_audit["risk_state_missing_touch"] += 1
            touch_features = touch_dependent_risk_features(
                book,
                mid_1s_x2=(
                    None
                    if book is None
                    else self._mid_x2_asof(
                        int(scheduled_wall_ns) - 1_000_000_000
                    )
                ),
                mid_10s_x2=(
                    None
                    if book is None
                    else self._mid_x2_asof(
                        int(scheduled_wall_ns) - 10_000_000_000
                    )
                ),
            )
            result = {
                "feature_asof_wall_ns": feature_wall,
                "yes_same_price_ahead_fp": (
                    Decimal(int(yes_quote.ahead)) / E4
                ),
                "no_same_price_ahead_fp": (
                    Decimal(int(no_quote.ahead)) / E4
                ),
                "yes_better_depth_fp": (
                    Decimal(
                        sum(
                            int(quantity or 0)
                            for price, quantity in self.books["y"].items()
                            if int(price) > int(yes_quote.lvl)
                        )
                    )
                    / E4
                ),
                "no_better_depth_fp": (
                    Decimal(
                        sum(
                            int(quantity or 0)
                            for price, quantity in self.books["n"].items()
                            if int(price) > int(no_quote.lvl)
                        )
                    )
                    / E4
                ),
                "yes_flow_10s_fp": self._flow(
                    "y", int(yes_quote.lvl), scheduled_us, 10
                ),
                "no_flow_10s_fp": self._flow(
                    "n", int(no_quote.lvl), scheduled_us, 10
                ),
                "yes_flow_60s_fp": self._flow(
                    "y", int(yes_quote.lvl), scheduled_us, 60
                ),
                "no_flow_60s_fp": self._flow(
                    "n", int(no_quote.lvl), scheduled_us, 60
                ),
                **touch_features,
                "tte_ms": max(
                    0,
                    int((self.close - scheduled_us) // 1_000),
                ),
            }
            return result

        def _flat_features(self, yb, nb, scheduled_wall_ns):
            book = self._book_state()
            scheduled_us = int(scheduled_wall_ns) // 1_000
            touch_features = touch_dependent_risk_features(
                book,
                mid_1s_x2=self._mid_x2_asof(
                    int(scheduled_wall_ns) - 1_000_000_000
                ),
                mid_10s_x2=self._mid_x2_asof(
                    int(scheduled_wall_ns) - 10_000_000_000
                ),
            )
            return {
                "yes_same_price_ahead_fp": (
                    Decimal(int(self.books["y"].get(int(yb), 0) or 0))
                    / E4
                ),
                "no_same_price_ahead_fp": (
                    Decimal(int(self.books["n"].get(int(nb), 0) or 0))
                    / E4
                ),
                "yes_better_depth_fp": Decimal("0"),
                "no_better_depth_fp": Decimal("0"),
                "yes_queue_position_fp": None,
                "no_queue_position_fp": None,
                "yes_flow_1s_fp": self._flow("y", int(yb), scheduled_us, 1),
                "no_flow_1s_fp": self._flow("n", int(nb), scheduled_us, 1),
                "yes_flow_5s_fp": self._flow("y", int(yb), scheduled_us, 5),
                "no_flow_5s_fp": self._flow("n", int(nb), scheduled_us, 5),
                "yes_flow_10s_fp": self._flow(
                    "y", int(yb), scheduled_us, 10
                ),
                "no_flow_10s_fp": self._flow(
                    "n", int(nb), scheduled_us, 10
                ),
                "yes_flow_60s_fp": self._flow(
                    "y", int(yb), scheduled_us, 60
                ),
                "no_flow_60s_fp": self._flow(
                    "n", int(nb), scheduled_us, 60
                ),
                **touch_features,
            }

        def _identity(self, action_kind):
            fields = self.current_event_fields
            if (
                fields is None
                or int(fields.get("causal_channel_priority", -1)) != 0
            ):
                raise RuntimeError("entry decision lacks causal BOOK receipt")
            identity = {
                "market": self.ticker,
                "recv_wall_ns": int(fields["recv_wall_ns"]),
                "recv_mono_ns": int(fields["recv_mono_ns"]),
                "ws_sid": int(fields["ws_sid"]),
                "ws_seq": int(fields["ws_seq"]),
                "stable_source_id": str(fields["book_stable_id"]),
                "action_kind": action_kind,
            }
            digest = sha_value(identity)[:24]
            episode_id = f"stage1|{self.ticker}|{digest}"
            return episode_id, identity

        def _base_entry_row(
            self,
            episode_id,
            identity,
            action_kind,
            action_id,
            tte_ms,
            features,
        ):
            fields = self.current_event_fields
            wall = int(fields["recv_wall_ns"])
            return {
                "experiment_id": PC.EXPERIMENT_ID,
                "data_role": "DISCOVERY",
                "source_date_utc": dt.datetime.fromtimestamp(
                    int(fields["local_recv_ts_us"]) / 1_000_000,
                    dt.timezone.utc,
                ).date().isoformat(),
                "market_ticker": self.ticker,
                "market_cluster_id": self.ticker,
                "cycle_id": episode_id,
                "entry_episode_id": episode_id,
                "entry_action_id": action_id,
                "action_set_version": "DIAGNOSTIC_SOURCE_POLICY_V0",
                "source_rows_sha256": sha_value(identity),
                "decision_recv_wall_ns": wall,
                "decision_recv_mono_ns": int(fields["recv_mono_ns"]),
                "feature_asof_wall_ns": wall,
                "entry_active_wall_ns": None,
                "terminal_wall_ns": wall,
                "book_ws_sid": str(fields["ws_sid"]),
                "book_ws_seq": int(fields["ws_seq"]),
                "action_kind": action_kind,
                "yes_price_e4": None,
                "no_price_e4": None,
                "clip_fp": None,
                "pair_cost_e4": None,
                "locked_pair_ceiling_e4": None,
                "legal_grid": False,
                "yes_post_only": False,
                "no_post_only": False,
                "ack_state": "NOT_SENT",
                "tte_ms": int(tte_ms),
                **features,
                "terminal_cause": "ENTRY_SKIP",
                "first_fill_wall_ns": None,
                "censor_reason": None,
            }

        def _skip_reason(self, before):
            if (
                int(self.audit.get("post_allocation_eta_skips", 0))
                > before["post_allocation_eta_skips"]
            ):
                return "A1_POST_ALLOCATION_ETA_INVALID"
            state = self.states[self.policies[0].name]
            for name, reason in (
                ("skipped_support", "INSUFFICIENT_CAUSAL_SUPPORT"),
                ("skipped_post_only", "POST_ONLY_ILLEGAL"),
                ("skipped_pair_cost", "PAIR_COST_ILLEGAL"),
            ):
                if int(getattr(state, name)) > before[name]:
                    return reason
            return "ALLOCATION_REJECTED_FAIL_CLOSED"

        def _record_skip(self, yb, nb, features, before):
            self._record_mid(self.current_event_fields)
            episode_id, identity = self._identity("ENTRY_SKIP")
            wall = int(self.current_event_fields["recv_wall_ns"])
            row = self._base_entry_row(
                episode_id,
                identity,
                "ENTRY_SKIP",
                "ENTRY_SKIP_P3_A1_GUARD",
                max(0, int(features["tte_s"] * 1_000)),
                self._flat_features(yb, nb, wall),
            )
            reason = self._skip_reason(before)
            row["censor_reason"] = reason
            self.entry_rows.append(row)
            self.outcome_rows.append(
                {
                    "entry_episode_id": episode_id,
                    "entry_action_id": row["entry_action_id"],
                    "source_date_utc": row["source_date_utc"],
                    "market_ticker": self.ticker,
                    "admitted": False,
                    "terminal_type": "ENTRY_SKIP",
                    "first_fill_episode_id": None,
                    "strict_trade_through_verified": None,
                    "ack_observation_kind": "NOT_SENT",
                    "entry_quote_seconds": Decimal("0"),
                    "capital_dollar_seconds": Decimal("0"),
                    "peak_episode_capital_usd": Decimal("0"),
                    "reconciliation_ok": True,
                }
            )
            self.stage1_audit["eligible_entry_decisions"] += 1
            self.stage1_audit["entry_skip_decisions"] += 1
            self.stage1_audit[f"skip_{reason}"] += 1

        def _allocate(self, policy, state, yb, nb, features):
            before = {
                "post_allocation_eta_skips": int(
                    self.audit.get("post_allocation_eta_skips", 0)
                ),
                "skipped_support": int(state.skipped_support),
                "skipped_post_only": int(state.skipped_post_only),
                "skipped_pair_cost": int(state.skipped_pair_cost),
            }
            allocation = super()._allocate(
                policy, state, yb, nb, features
            )
            if allocation is None:
                self._record_skip(yb, nb, features, before)
            return allocation

        def _admit(self, policy, state, ts, features, allocation):
            if policy.name in self.active_entry:
                raise RuntimeError("overlapping Stage-1 active entry")
            super()._admit(policy, state, ts, features, allocation)
            self._record_mid(self.current_event_fields)
            episode_id, identity = self._identity(
                "ENTRY_PAIR_POST_ONLY"
            )
            wall = int(self.current_event_fields["recv_wall_ns"])
            mono = int(self.current_event_fields["recv_mono_ns"])
            dynamic = self._state_for_active(
                policy,
                state,
                {
                    "entry_episode_id": episode_id,
                    "decision_fields": dict(self.current_event_fields),
                },
                Decimal("0"),
                wall,
            )
            entry_features = {
                **dynamic,
                "yes_queue_position_fp": (
                    Decimal(
                        max(
                            0,
                            int(state.quotes["y"].ahead) + CLIP_E4,
                        )
                    )
                    / E4
                ),
                "no_queue_position_fp": (
                    Decimal(
                        max(
                            0,
                            int(state.quotes["n"].ahead) + CLIP_E4,
                        )
                    )
                    / E4
                ),
                "yes_flow_1s_fp": self._flow(
                    "y",
                    int(allocation["allocated_yes_e4"]),
                    int(ts),
                    1,
                ),
                "no_flow_1s_fp": self._flow(
                    "n",
                    int(allocation["allocated_no_e4"]),
                    int(ts),
                    1,
                ),
                "yes_flow_5s_fp": self._flow(
                    "y",
                    int(allocation["allocated_yes_e4"]),
                    int(ts),
                    5,
                ),
                "no_flow_5s_fp": self._flow(
                    "n",
                    int(allocation["allocated_no_e4"]),
                    int(ts),
                    5,
                ),
            }
            row = self._base_entry_row(
                episode_id,
                {
                    **identity,
                    "allocation": allocation,
                    "features": features,
                },
                "ENTRY_PAIR_POST_ONLY",
                "P3_SPEND1_A1_SKIP",
                max(0, int(features["tte_s"] * 1_000)),
                entry_features,
            )
            yes_price = int(allocation["allocated_yes_e4"])
            no_price = int(allocation["allocated_no_e4"])
            row.update(
                {
                    "source_rows_sha256": sha_value(
                        {
                            **identity,
                            "allocation": allocation,
                            "features": features,
                        }
                    ),
                    "entry_active_wall_ns": wall,
                    "terminal_wall_ns": None,
                    "yes_price_e4": yes_price,
                    "no_price_e4": no_price,
                    "clip_fp": Decimal("1"),
                    "pair_cost_e4": yes_price + no_price,
                    "locked_pair_ceiling_e4": PAIR_LOCK_E4,
                    "legal_grid": True,
                    "yes_post_only": True,
                    "no_post_only": True,
                    "ack_state": "BOTH_ACKED",
                    "terminal_cause": None,
                    "censor_reason": None,
                }
            )
            active = {
                "entry_episode_id": episode_id,
                "entry_action_id": row["entry_action_id"],
                "row": row,
                "decision_fields": dict(self.current_event_fields),
                "active_wall_ns": wall,
                "active_mono_ns": mono,
                "admit_us": int(ts),
                "states": {Decimal("0"): dynamic},
                "cancel_due": {},
                "strict_margin_e4": None,
            }
            self.active_entry[policy.name] = active
            self.stage1_audit["eligible_entry_decisions"] += 1
            self.stage1_audit["pair_post_only_decisions"] += 1
            self.stage1_audit["both_acked"] += 1

        def _pending_terminal_clock(self, active):
            if len(active["cancel_due"]) != 2:
                return None
            rows = list(active["cancel_due"].values())
            terminal = max(rows, key=lambda item: int(item["due_wall_ns"]))
            return (
                int(terminal["due_wall_ns"]),
                int(terminal["due_mono_ns"]),
                str(terminal["reason"]),
            )

        def _capture_due_before(self, event_fields):
            event_wall = int(event_fields["recv_wall_ns"])
            event_mono = int(event_fields["recv_mono_ns"])
            for policy in self.policies:
                active = self.active_entry.get(policy.name)
                if active is None:
                    continue
                state = self.states[policy.name]
                pending = self._pending_terminal_clock(active)
                limit_wall = event_wall
                limit_mono = event_mono
                if pending is not None:
                    limit_wall = min(limit_wall, pending[0])
                    limit_mono = min(limit_mono, pending[1])
                for elapsed in ENTRY_BOUNDARIES_MS[1:]:
                    if elapsed in active["states"]:
                        continue
                    horizon_ns = int(elapsed * Decimal("1000000"))
                    scheduled_wall = active["active_wall_ns"] + horizon_ns
                    scheduled_mono = active["active_mono_ns"] + horizon_ns
                    if (
                        scheduled_wall <= limit_wall
                        and scheduled_mono <= limit_mono
                        and (
                            pending is None
                            or scheduled_wall < pending[0]
                        )
                    ):
                        active["states"][elapsed] = (
                            self._state_for_active(
                                policy,
                                state,
                                active,
                                elapsed,
                                scheduled_wall,
                            )
                        )

        def _request_cancel(self, state, side, ts, reason):
            policy = next(
                (
                    policy
                    for policy in self.policies
                    if self.states[policy.name] is state
                ),
                None,
            )
            quote = state.quotes[side]
            new_request = quote is not None and quote.cx is None
            super()._request_cancel(state, side, ts, reason)
            if (
                policy is None
                or not new_request
                or state.orphan_side is not None
            ):
                return
            active = self.active_entry.get(policy.name)
            if active is None:
                return
            fields = self.current_event_fields
            if fields is None:
                raise RuntimeError("cancel request lacks causal receipt")
            due_us = int(state.quotes[side].cx)
            delta_ns = (
                due_us - int(fields["local_recv_ts_us"])
            ) * 1_000
            active["cancel_due"][side] = {
                "due_us": due_us,
                "due_wall_ns": int(fields["recv_wall_ns"]) + delta_ns,
                "due_mono_ns": int(fields["recv_mono_ns"]) + delta_ns,
                "reason": reason,
            }

        def _finalize_entry(
            self,
            policy,
            state,
            cause,
            terminal_wall_ns,
            terminal_mono_ns,
            reason,
            *,
            first_fill_id=None,
            strict_margin_e4=None,
        ):
            active = self.active_entry.get(policy.name)
            if active is None:
                raise RuntimeError("Stage-1 terminal has no active entry")
            if int(terminal_wall_ns) <= int(active["active_wall_ns"]):
                raise RuntimeError(
                    "zero-duration Stage-1 event; epsilon is forbidden"
                )
            row = copy.deepcopy(active["row"])
            row["terminal_wall_ns"] = int(terminal_wall_ns)
            row["terminal_cause"] = cause
            row["first_fill_wall_ns"] = (
                int(terminal_wall_ns)
                if cause in ("YES_FIRST", "NO_FIRST")
                else None
            )
            row["censor_reason"] = (
                None
                if cause in ("YES_FIRST", "NO_FIRST")
                else str(reason)
            )
            risk = PC.build_stage1_risk_intervals(
                row,
                active["states"],
            )
            locked = (
                Decimal(int(row["pair_cost_e4"]))
                / E4
                * Decimal(row["clip_fp"])
            )
            capital = PC.build_capital_interval(row, 0, locked)
            quote_seconds = (
                Decimal(
                    int(terminal_wall_ns) - int(active["active_wall_ns"])
                )
                / Decimal("1000000000")
            )
            outcome = {
                "entry_episode_id": row["entry_episode_id"],
                "entry_action_id": row["entry_action_id"],
                "source_date_utc": row["source_date_utc"],
                "market_ticker": self.ticker,
                "admitted": True,
                "terminal_type": cause,
                "first_fill_episode_id": first_fill_id,
                "strict_trade_through_verified": (
                    True
                    if cause in ("YES_FIRST", "NO_FIRST")
                    else None
                ),
                "ack_observation_kind": "SHADOW_IMMEDIATE_BOTH_ACKED",
                "entry_quote_seconds": quote_seconds,
                "capital_dollar_seconds": capital[
                    "capital_dollar_seconds"
                ],
                "peak_episode_capital_usd": locked,
                "reconciliation_ok": True,
            }
            self.entry_rows.append(row)
            self.risk_rows.extend(risk)
            self.capital_rows.append(capital)
            self.outcome_rows.append(outcome)
            if cause in ("YES_FIRST", "NO_FIRST"):
                if strict_margin_e4 is None or strict_margin_e4 <= 0:
                    raise RuntimeError("strict trade-through margin missing")
                self.stage1_audit["first_fill"] += 1
                self.stage1_audit[
                    f"{cause.lower()}_entries"
                ] += 1
                self.stage1_audit["strict_margin_e4_sum"] += int(
                    strict_margin_e4
                )
            else:
                self.stage1_audit["no_first_fill"] += 1
                self.stage1_audit[
                    f"no_first_{reason}"
                ] += 1
            del self.active_entry[policy.name]

        def _advance(self, policy, state, ts):
            active_before = self.active_entry.get(policy.name)
            super()._advance(policy, state, ts)
            active = self.active_entry.get(policy.name)
            if (
                active_before is not None
                and active is not None
                and state.orphan_side is None
                and state.quotes["y"] is None
                and state.quotes["n"] is None
            ):
                pending = self._pending_terminal_clock(active)
                if pending is None:
                    raise RuntimeError(
                        "two-sided quote disappeared without cancel-ACK clocks"
                    )
                self._finalize_entry(
                    policy,
                    state,
                    "ADMIN_CENSOR_NO_FIRST_FILL",
                    pending[0],
                    pending[1],
                    f"{pending[2]}_CANCEL_ACK",
                )

        def _fill(
            self,
            policy,
            state,
            side,
            level,
            ts,
            was_cancel_pending,
        ):
            first_fill = state.orphan_side is None
            if first_fill:
                active = self.active_entry.get(policy.name)
                fields = self.current_event_fields
                quote = state.quotes[side]
                if (
                    active is None
                    or fields is None
                    or int(fields.get("causal_channel_priority", -1))
                    != TRADE_PRIORITY
                    or quote is None
                ):
                    raise RuntimeError(
                        "first fill lacks active causal entry/trade receipt"
                    )
                strict_margin = -int(quote.ahead) - CLIP_E4
                if strict_margin <= 0:
                    raise RuntimeError(
                        "first fill violates strict trade-through"
                    )
                first_id = (
                    f"{self.ticker}|{active['admit_us']}|{int(ts)}"
                )
                self._finalize_entry(
                    policy,
                    state,
                    "YES_FIRST" if side == "y" else "NO_FIRST",
                    int(fields["recv_wall_ns"]),
                    int(fields["recv_mono_ns"]),
                    "STRICT_TRADE_THROUGH",
                    first_fill_id=first_id,
                    strict_margin_e4=strict_margin,
                )
            super()._fill(
                policy,
                state,
                side,
                level,
                ts,
                was_cancel_pending=was_cancel_pending,
            )

        def on_trade_causal(
            self,
            ts,
            yes_px,
            qty,
            taker,
            event_fields,
        ):
            self._capture_due_before(event_fields)
            self.current_event_fields = dict(event_fields)
            super().on_trade_causal(
                ts,
                yes_px,
                qty,
                taker,
                event_fields,
            )
            self.last_applied_fields = dict(event_fields)
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
            self._capture_due_before(event_fields)
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
            self._record_mid(event_fields)
            self.last_applied_fields = dict(event_fields)
            self.current_event_fields = None

        def _finalize_source_end_entries(self):
            if self.last_applied_fields is None:
                if self.active_entry:
                    raise RuntimeError("active entry without source receipt")
                return
            terminal_wall = int(
                self.last_applied_fields["recv_wall_ns"]
            )
            terminal_mono = int(
                self.last_applied_fields["recv_mono_ns"]
            )
            for policy in self.policies:
                if policy.name not in self.active_entry:
                    continue
                state = self.states[policy.name]
                if state.orphan_side is not None:
                    raise RuntimeError(
                        "Stage-1 active entry survived after first fill"
                    )
                self._finalize_entry(
                    policy,
                    state,
                    "ADMIN_CENSOR_NO_FIRST_FILL",
                    terminal_wall,
                    terminal_mono,
                    "SOURCE_END",
                )

        def finalize_with_tracker(self):
            self._finalize_source_end_entries()
            super().finalize_with_tracker()
            if self._payload_emitted:
                raise RuntimeError("duplicate Stage-1 market harvest")
            state = self.states[self.policies[0].name]
            if len(state.admissions) != int(
                self.stage1_audit["pair_post_only_decisions"]
            ):
                raise RuntimeError(
                    "frozen admissions and Stage-1 pair roster disagree"
                )
            if self.active_entry:
                raise RuntimeError("unharvested Stage-1 active entry")
            first_ids = {
                row["first_fill_episode_id"]
                for row in self.outcome_rows
                if row["first_fill_episode_id"] is not None
            }
            tracker_ids = {
                row["episode_id"] for row in self.tracker.results
            }
            if first_ids != tracker_ids:
                raise RuntimeError(
                    "Stage-1 first fills disagree with frozen tracker"
                )
            self.stage1_writer.add_market_payload(
                {
                    "market_ticker": self.ticker,
                    "entry_episodes": self.entry_rows,
                    "entry_risk_intervals": self.risk_rows,
                    "entry_capital_intervals": self.capital_rows,
                    "entry_stage1_outcomes": self.outcome_rows,
                    "source_audit": dict(self.stage1_audit),
                    "frozen_engine": {
                        "eligible_decisions": int(state.eligible_decisions),
                        "admissions": len(state.admissions),
                        "skipped_support": int(state.skipped_support),
                        "skipped_post_only": int(state.skipped_post_only),
                        "skipped_pair_cost": int(state.skipped_pair_cost),
                    },
                }
            )
            self._payload_emitted = True

    return R, EntryRosterMarket


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
                    _jsonable(value),
                    text,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )


def synthetic_preflight(PC):
    assert set(DATES).isdisjoint(FORBIDDEN_DATES)
    assert ENTRY_BOUNDARIES_MS[0] == 0
    assert ENTRY_BOUNDARIES_MS[-1] == 300_000
    assert PC.CANDIDATE_STATUS == "ACTION_SET_PENDING"
    missing = touch_dependent_risk_features(None)
    assert all(value is None for value in missing.values())
    assert public_touch_snapshot({"y": {3000: 1}, "n": {}}) is None
    history = deque()
    append_causal_mid_receipt(
        history,
        {"recv_wall_ns": 0, "book_stable_id": "valid"},
        {"mid_x2_e4": 6_000},
    )
    append_causal_mid_receipt(
        history,
        {"recv_wall_ns": 1_000_000_000, "book_stable_id": "missing"},
        None,
    )
    append_causal_mid_receipt(
        history,
        {"recv_wall_ns": 2_000_000_000, "book_stable_id": "recovered"},
        {"mid_x2_e4": 6_200},
    )
    assert causal_mid_x2_asof(history, 1_500_000_000) is None
    assert causal_mid_x2_asof(history, 2_000_000_000) == 6_200
    return {
        "status": "ROUND4_STAGE1_R3_SYNTHETIC_PREFLIGHT_OK",
        "forbidden_dates_disjoint": True,
        "epsilon_interval_forbidden": True,
        "ack_failed_interface_retained": True,
        "passive_empty_touch_sentinel": True,
        "lag_lookup_cannot_cross_missing_sentinel": True,
        "active_empty_touch_nullable_bundle": True,
        "candidate": False,
        "deployable": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument(
        "--report-out",
        default="/tmp/z3_round4_stage1_entry_report.json",
    )
    parser.add_argument(
        "--rows-out",
        default="/tmp/z3_round4_stage1_entry_rows.json.gz",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    script_sha = file_sha(__file__)
    with open(args.prereg, "rb") as handle:
        prereg_bytes = handle.read()
    prereg = json.loads(prereg_bytes)
    expected_prereg = {
        "schema": "z3-round4-stage1-entry-full-roster-receipt-v1",
        "experiment_sha256": script_sha,
        "embedded_spec_sha256": sha_value(PREREGISTRATION),
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate_status": "ACTION_SET_PENDING",
        "candidate": False,
        "deployable": False,
        "live_authorized": False,
    }
    if prereg != expected_prereg:
        raise RuntimeError("Stage-1 prereg receipt mismatch")
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()

    require_sha(
        TABLE_BUILDER_PATH,
        TABLE_BUILDER_SHA256,
        "ROUND4 table builder",
    )
    PC = load_module(
        STAGE1_CONTRACT_PATH,
        STAGE1_CONTRACT_SHA256,
        "round4_stage1_entry_contract_pinned",
        "Stage-1 row contract",
    )
    synthetic = synthetic_preflight(PC)
    if args.self_test:
        print(json.dumps(synthetic, indent=2))
        return

    require_sha(ROUND4_DDL_PATH, ROUND4_DDL_SHA256, "ROUND4 base DDL")
    require_sha(
        STAGE1_DDL_PATH,
        STAGE1_DDL_SHA256,
        "Stage-1 supplemental DDL",
    )
    require_sha(
        ROUND4_DESIGN_PATH,
        ROUND4_DESIGN_SHA256,
        "ROUND4 design prereg",
    )
    expected_first_ids = load_expected_first_fill_ids()
    A = load_module(
        SEALED_A1_PATH,
        SEALED_A1_SHA256,
        "sealed_a1_stage1_source",
        "sealed A1-skip replay",
    )
    R, EntryRosterMarket = build_engine_hooks(A, PC)
    R.CausalTrackedMarket = EntryRosterMarket
    R.D.JsonlGzipWriter = NullStage1Writer
    tasks = [
        (date, f"/tmp/stage1_null_{date}.jsonl.gz")
        for date in DATES
    ]
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print("RUN STAGE1 FULL ROSTER 2026-07-20..22", flush=True)
    with R.mp.Pool(processes=3) as pool:
        day_results = pool.map(R.run_day, tasks)

    entry_episodes = []
    risk_intervals = []
    capital_intervals = []
    outcomes = []
    source_counts = Counter()
    causal_quality = Counter()
    data_audits = []
    per_date = {}
    first_fill_markets = set()
    for date, _path, market_payloads, aggregate in day_results:
        if date not in DATES or not isinstance(market_payloads, list):
            raise RuntimeError("Stage-1 worker return contract mismatch")
        date_counts = Counter()
        for payload in market_payloads:
            entries = payload["entry_episodes"]
            risks = payload["entry_risk_intervals"]
            capitals = payload["entry_capital_intervals"]
            market_outcomes = payload["entry_stage1_outcomes"]
            entry_episodes.extend(entries)
            risk_intervals.extend(risks)
            capital_intervals.extend(capitals)
            outcomes.extend(market_outcomes)
            source_counts.update(payload["source_audit"])
            date_counts["entry_decisions"] += len(entries)
            date_counts["risk_intervals"] += len(risks)
            for outcome in market_outcomes:
                terminal = str(outcome["terminal_type"])
                date_counts[terminal] += 1
                if terminal in ("YES_FIRST", "NO_FIRST"):
                    first_fill_markets.add(payload["market_ticker"])
            frozen = payload["frozen_engine"]
            if (
                int(frozen["eligible_decisions"])
                != len(entries)
                or int(frozen["admissions"])
                != sum(
                    row["action_kind"] == "ENTRY_PAIR_POST_ONLY"
                    for row in entries
                )
            ):
                raise RuntimeError(
                    "frozen engine decision counters disagree with roster"
                )
        for market_audit in aggregate["data_audit"][
            "market_audit"
        ].values():
            causal_quality.update(
                {
                    key: int(value)
                    for key, value in market_audit.items()
                }
            )
        data_audits.append(aggregate["data_audit"])
        per_date[date] = dict(date_counts)

    failure_fields = (
        "negative_level_failures",
        "clock_null_failures",
        "clock_identity_failures",
        "sequence_failures",
        "A1_failures",
    )
    failures = {
        name: causal_quality[name]
        for name in failure_fields
        if causal_quality[name] != 0
    }
    if failures:
        raise RuntimeError(f"causal source gate failed: {failures}")

    source_audit = {
        "eligible_entry_decisions": int(
            source_counts["eligible_entry_decisions"]
        ),
        "pair_post_only_decisions": int(
            source_counts["pair_post_only_decisions"]
        ),
        "entry_skip_decisions": int(
            source_counts["entry_skip_decisions"]
        ),
        "both_acked": int(source_counts["both_acked"]),
        "ack_failed": 0,
        "data_invalid_market_days": 0,
    }
    batch = PC.validate_and_serialize_stage1_rows(
        entry_episodes,
        risk_intervals,
        capital_intervals,
        outcomes,
        expected_first_fill_ids=expected_first_ids,
        source_audit=source_audit,
    )
    if (
        batch.receipt["first_fill_entries"] != EXPECTED_FIRST_FILLS
        or len(first_fill_markets) != EXPECTED_FIRST_FILL_MARKETS
    ):
        raise RuntimeError("sealed first-fill count/market linkage mismatch")
    if batch.receipt["historical_ack_failure_identified"]:
        raise RuntimeError("shadow replay falsely identified wire ACK failure")

    rows_payload = {
        "schema": "z3-round4-stage1-entry-full-roster-rows-v3",
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate_status": "ACTION_SET_PENDING",
        "candidate": False,
        "deployable": False,
        "live_authorized": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "tables": batch.as_ddl_rows(),
        "contract_receipt": batch.receipt,
    }
    deterministic_gzip_json(args.rows_out, rows_payload)
    rows_sha = file_sha(args.rows_out)

    admitted = int(batch.receipt["admitted_entries"])
    no_fill = int(batch.receipt["no_first_fill_entries"])
    report = {
        "schema": "z3-round4-stage1-entry-full-roster-report-v3",
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate_status": "ACTION_SET_PENDING",
        "candidate": False,
        "validation": False,
        "deployable": False,
        "live_authorized": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "source": {
            "script_sha256": script_sha,
            "preregistration_sha256": prereg_sha,
            "sealed_a1_skip_sha256": SEALED_A1_SHA256,
            "sealed_first_fill_receipt_sha256": (
                SEALED_EPISODES_SHA256
            ),
            "stage1_contract_sha256": STAGE1_CONTRACT_SHA256,
            "round4_table_builder_sha256": TABLE_BUILDER_SHA256,
            "round4_base_ddl_sha256": ROUND4_DDL_SHA256,
            "stage1_supplemental_ddl_sha256": STAGE1_DDL_SHA256,
            "round4_design_sha256": ROUND4_DESIGN_SHA256,
        },
        "preregistration": PREREGISTRATION,
        "synthetic_preflight": synthetic,
        "contract_receipt": batch.receipt,
        "coverage": {
            "per_date": per_date,
            "first_fill_markets": len(first_fill_markets),
            "no_first_fill_rate_given_both_acked": (
                no_fill / admitted if admitted else None
            ),
            "ack_failed_historical_rows": 0,
            "ack_failed_historical_identified": False,
            "sealed_first_fill_identity_exact": True,
            "no_first_fill_derived_from_first_fill_artifact": False,
            "trajectory_sampling_used": False,
            "passive_mid_skipped_missing_touch": int(
                source_counts["passive_mid_skipped_missing_touch"]
            ),
            "risk_state_missing_touch": int(
                source_counts["risk_state_missing_touch"]
            ),
        },
        "capital": {
            "pre_first_fill_capital_dollar_seconds": batch.receipt[
                "capital_dollar_seconds"
            ],
            "includes_no_first_fill": True,
            "entry_skip_capital_zero": True,
            "post_first_fill_capital_included": False,
        },
        "causal_gate": {
            "pass": True,
            "failure_fields": {
                name: causal_quality[name]
                for name in failure_fields
            },
            "aggregate_market_audit": dict(causal_quality),
            "per_day": data_audits,
        },
        "whole_policy_scope": {
            "whole_policy_economics_ready": False,
            "reason": (
                "entry/re-entry timing is inherited from frozen P3 current "
                "post-fill exits; a different KEEP/IOC policy changes capital "
                "release and future admission opportunities"
            ),
            "required_next": (
                "integrated non-overlapping candidate-policy replay over the "
                "full eligible-decision roster, with no-fill and capital-time"
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
            _jsonable(report),
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
                "status": "ROUND4_STAGE1_FULL_ROSTER_OK",
                "contract_receipt": batch.receipt,
                "coverage": report["coverage"],
                "whole_policy_scope": report["whole_policy_scope"],
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
