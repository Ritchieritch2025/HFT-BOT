#!/usr/bin/env python3
import importlib.util
import os
import sys
from collections import deque


os.environ["ROUND3_FULL_CONSUMER"] = (
    ".codex_tmp/z3_postfill_stopping_receive_clock.py"
)
os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    ".codex_tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = (
    "tmp/crypto_mm_canary_20260726/z3_price_allocation_train.py"
)
os.environ["ROUND3_CAUSAL_CONTRACT"] = (
    "tmp/crypto_mm_canary_20260726/round3/causal_replay_contract.py"
)
spec = importlib.util.spec_from_file_location(
    "sampled",
    ".codex_tmp/z3_postfill_stopping_receive_clock_sampled.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Writer:
    def __init__(self):
        self.rows = []

    def emit(self, row):
        self.rows.append(row)


class Market:
    def __init__(self, ticker):
        self.ticker = ticker
        self.result = "yes"
        self.books = {
            "y": {2700: 10_000},
            "n": {7100: 20_000},
        }
        self.book_asof_ts = 900_000
        self.recent_trades = deque()


def state(base):
    value = module.F.B.ArmState()
    value.orphan_side = "y"
    value.orphan_entry = 2800
    value.first_ts = base
    value.admission = {"ts": base - 200_000}
    value.quotes["n"] = module.F.B.Quote(
        lvl=7100,
        ahead=0,
        placed_ts=base - 200_000,
    )
    return value


def run(tracker_cls, ticker):
    writer = Writer()
    market = Market(ticker)
    tracker = tracker_cls(market, writer)
    base = 1_000_000
    tracker.start(state(base), base)
    tracker.before_event(base + 60_000)
    tracker.observe(base + 60_000, "book", {})
    tracker.before_event(base + 100_000)
    tracker.apply_trade(base + 100_000, 2900, 10_001, "yes")
    tracker.observe(base + 100_000, "trade", {})
    for horizon_s in module.F.D.MARKOUT_HORIZONS_S:
        ts = base + int(horizon_s * 1e6)
        market.book_asof_ts = ts - 1
        tracker.before_event(ts)
        tracker.observe(ts, "book", {})
    return tracker, writer


unselected_ticker = next(
    f"SYNTH-{index}"
    for index in range(10_000)
    if not module.trajectory_selected(
        f"SYNTH-{index}|800000|1000000"
    )
)
full, full_writer = run(module.F.D.EpisodeTracker, unselected_ticker)
lean, lean_writer = run(module.SampledEpisodeTracker, unselected_ticker)
assert full.results == lean.results
assert full.markouts == lean.markouts
assert full.episodes_started == lean.episodes_started == 1
assert len(full_writer.rows) > 0
assert lean_writer.rows == []

selected_ticker = next(
    f"SYNTH-{index}"
    for index in range(10_000)
    if module.trajectory_selected(
        f"SYNTH-{index}|800000|1000000"
    )
)
full_selected, full_selected_writer = run(
    module.F.D.EpisodeTracker, selected_ticker
)
lean_selected, lean_selected_writer = run(
    module.SampledEpisodeTracker, selected_ticker
)
assert full_selected.results == lean_selected.results
assert full_selected.markouts == lean_selected.markouts
assert full_selected_writer.rows == lean_selected_writer.rows

print("SAMPLED_TRAJECTORY_SYNTHETIC_EQUIVALENCE_OK")
