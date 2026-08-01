#!/usr/bin/env python3
"""Synthetic regressions for queue strictness and post-allocation ETA skip."""
from __future__ import annotations

import importlib.util
import json
import os
import sys


ROOT = "/Users/ritcardo/HFT BOT"
os.environ["ROUND3_SAMPLED_PARENT"] = (
    f"{ROOT}/.codex_tmp/z3_postfill_stopping_receive_clock_sampled.py"
)
os.environ["ROUND3_FULL_CONSUMER"] = (
    f"{ROOT}/.codex_tmp/z3_postfill_stopping_receive_clock.py"
)
os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    f"{ROOT}/.codex_tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = (
    f"{ROOT}/.codex_tmp/z3_price_allocation_train.py"
)
os.environ["ROUND3_CAUSAL_CONTRACT"] = (
    f"{ROOT}/tmp/crypto_mm_canary_20260726/round3/"
    "causal_replay_contract.py"
)

SCRIPT = (
    f"{ROOT}/.codex_tmp/"
    "z3_postfill_stopping_receive_clock_sampled_a1skip.py"
)
spec = importlib.util.spec_from_file_location("a1skip_synthetic", SCRIPT)
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)


class NullWriter:
    def emit(self, _row):
        return None


class NullTracker:
    def __init__(self):
        self.results = []
        self.markouts = []
        self.episodes_started = 0

    def before_event(self, _ts):
        return None

    def apply_trade(self, _ts, _yes_px, _qty, _taker):
        return None

    def observe(self, _ts, _kind, _fields):
        return None

    def after_book(self, _ts, _fields):
        return None

    def start(self, _state, _ts):
        self.episodes_started += 1

    def finish(self, _last_ts):
        return None


def market():
    sim = M.EtaGuardedSampledMarket(
        "SYNTH", 1_000_000_000, "yes", [M.F.P3], NullWriter()
    )
    sim.tracker = NullTracker()
    return sim


def trade(sim, qty):
    sim.on_trade_causal(
        70_000_000,
        3000,
        qty,
        "no",
        {
            "trade_id": f"q-{qty}",
            "yes_price_e4": 3000,
            "count_e4": qty,
            "taker_side": "no",
            "recv_wall_ns": 70_000_000_000,
            "recv_mono_ns": 1,
            "local_recv_ts_us": 70_000_000,
            "causal_channel_priority": 1,
        },
    )


# One eligible trade decrements queue exactly once.
once = market()
once_state = once.states[M.F.P3.name]
once_state.quotes["y"] = M.F.B.Quote(
    lvl=3000, ahead=20_000, placed_ts=1
)
trade(once, 10_000)
assert once_state.quotes["y"].ahead == 10_000
assert once_state.fills == 0

# Strict threshold: total eligible qty == ahead + clip does not fill.
equal = market()
equal_state = equal.states[M.F.P3.name]
equal_state.quotes["y"] = M.F.B.Quote(
    lvl=3000, ahead=20_000, placed_ts=1
)
trade(equal, 20_000 + M.F.D.CLIP_E4)
assert equal_state.quotes["y"] is not None
assert equal_state.quotes["y"].ahead == -M.F.D.CLIP_E4
assert equal_state.fills == 0
assert equal_state.orphan_side is None

# One unit beyond ahead + clip fills.
beyond = market()
beyond_state = beyond.states[M.F.P3.name]
beyond_state.quotes["y"] = M.F.B.Quote(
    lvl=3000, ahead=20_000, placed_ts=1
)
trade(beyond, 20_000 + M.F.D.CLIP_E4 + 1)
assert beyond_state.quotes["y"] is None
assert beyond_state.fills == 1
assert beyond_state.orphan_side == "y"

# Real allocator fallback shape: touch has support on both sides, but moving
# YES from 9000 to 8900 leaves the allocated level with legal zero flow.
guarded = market()
ts = 70_000_000
guarded.first_event_ts = 0
guarded.books = {
    "y": {9000: 10_000},
    "n": {960: 10_000},
}
guarded.recent_trades.append(
    (ts - 1_000_000, 9000, 1_000_000, "no")
)
guarded.recent_trades.append(
    (ts - 500_000, 9040, 100_000, "yes")
)
features = guarded._features(ts, 9000, 960)
assert features["eta_y_s"] > 0
assert features["eta_n_s"] > features["eta_y_s"]
guarded_state = guarded.states[M.F.P3.name]
skips_before = guarded_state.skipped_support
allocation = guarded._allocate(
    M.F.P3, guarded_state, 9000, 960, features
)
assert allocation is None
assert guarded_state.skipped_support == skips_before + 1
assert guarded.audit["post_allocation_eta_skips"] == 1
assert guarded.audit["post_allocation_eta_skip_y"] == 1
assert guarded.audit["post_allocation_zero_flow_y"] == 1
assert guarded.audit["post_allocation_eta_skip_n"] == 0
assert guarded.audit["A1_failures"] == 0

print(
    json.dumps(
        {
            "schema": "z3-a1skip-synthetic-regression-v1",
            "queue_decrement_once": True,
            "strict_equal_no_fill": True,
            "strict_beyond_fill": True,
            "fallback_zero_flow_skipped": True,
            "skipped_support_delta": 1,
        },
        sort_keys=True,
    )
)
