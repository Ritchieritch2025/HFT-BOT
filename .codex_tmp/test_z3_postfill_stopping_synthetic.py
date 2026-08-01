#!/usr/bin/env python3
import importlib.util
import os
import sys
from collections import deque


os.environ["Z3_PRICE_BASE_SCRIPT"] = (
    "tmp/crypto_mm_canary_20260726/z3_price_allocation_train.py"
)
spec = importlib.util.spec_from_file_location(
    "postfill", ".codex_tmp/z3_postfill_stopping_diagnostic.py"
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
    def __init__(self):
        self.ticker = "SYNTH"
        self.result = "yes"
        self.books = {
            "y": {2700: 10_000},
            "n": {7100: 20_000},
        }
        self.book_asof_ts = 900_000
        self.recent_trades = deque()


def state():
    value = module.B.ArmState()
    value.orphan_side = "y"
    value.orphan_entry = 2800
    value.first_ts = 1_000_000
    value.admission = {"ts": 800_000}
    value.quotes["n"] = module.B.Quote(
        lvl=7100, ahead=0, placed_ts=800_000
    )
    return value


# Maker fills at 100ms: IOC60 exits; all longer fixed waits pair.  The episode
# remains live for markout capture even after policy results resolve.
market = Market()
writer = Writer()
tracker = module.EpisodeTracker(market, writer)
tracker.start(state(), 1_000_000)
tracker.before_event(1_060_000)
tracker.observe(1_060_000, "book", {})
tracker.before_event(1_100_000)
tracker.apply_trade(1_100_000, 2900, 10_001, "yes")
tracker.observe(1_100_000, "trade", {})
assert tracker.active and tracker.active[0].resolved
for horizon_s in module.MARKOUT_HORIZONS_S:
    ts = 1_000_000 + int(horizon_s * 1e6)
    market.book_asof_ts = ts - 1
    tracker.before_event(ts)
    tracker.observe(ts, "book", {})
assert not tracker.active
by_policy = {row["policy"]: row for row in tracker.results}
assert by_policy["IOC_60MS"]["exit_kind"] == "ioc"
assert by_policy["WAIT_0P25"]["exit_kind"] == "maker_pair"
assert len(tracker.markouts) == 1
assert set(tracker.markouts[0]["horizons"]) == {
    module.EpisodeTracker._horizon_key(value)
    for value in module.MARKOUT_HORIZONS_S
}


# Strict race: a maker fill at the exact IOC due loses to IOC.
market2 = Market()
writer2 = Writer()
tracker2 = module.EpisodeTracker(market2, writer2)
tracker2.start(state(), 1_000_000)
tracker2.before_event(1_060_000)
tracker2.apply_trade(1_060_000, 2900, 10_001, "yes")
tracker2.observe(1_060_000, "trade", {})
for horizon_s in module.MARKOUT_HORIZONS_S:
    ts = 1_000_000 + int(horizon_s * 1e6)
    market2.book_asof_ts = ts
    tracker2.before_event(ts)
    tracker2.observe(ts, "book", {})
assert {
    row["policy"]: row for row in tracker2.results
}["IOC_60MS"]["exit_kind"] == "ioc"


# No future book is accepted for a scheduled snapshot.
market3 = Market()
writer3 = Writer()
tracker3 = module.EpisodeTracker(market3, writer3)
tracker3.start(state(), 1_000_000)
market3.book_asof_ts = 1_300_001
try:
    tracker3.before_event(1_300_001)
except RuntimeError as exc:
    assert "future book" in str(exc)
else:
    raise AssertionError("future-book guard did not fire")

print("SYNTHETIC_SELFTEST_OK")
