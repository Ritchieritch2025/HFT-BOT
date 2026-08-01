#!/usr/bin/env python3
"""Admission-only all-date preflight for the ETA-guarded causal replay."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict


os.environ["ROUND3_SAMPLED_PARENT"] = (
    "/tmp/z3_postfill_stopping_receive_clock_sampled.py"
)
os.environ["ROUND3_FULL_CONSUMER"] = (
    "/tmp/z3_postfill_stopping_receive_clock.py"
)
os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    "/tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = "/tmp/z3_price_allocation_train.py"
os.environ["ROUND3_CAUSAL_CONTRACT"] = "/tmp/causal_replay_contract.py"

SCRIPT = (
    "/tmp/z3_postfill_stopping_receive_clock_sampled_a1skip.preseal.py"
)
spec = importlib.util.spec_from_file_location("a1skip_preflight", SCRIPT)
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)
F = M.F
C = F.C


class NullWriter:
    def __init__(self, _path, _date):
        self.rows = 0

    def emit(self, _row):
        return None

    def close(self):
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


class AdmissionPreflightMarket(M.EtaGuardedSampledMarket):
    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, policies, writer)
        self.tracker = NullTracker()

    def finalize_with_tracker(self):
        state = self.states[F.P3.name]
        self.audit["state_eligible_decisions"] = (
            state.eligible_decisions
        )
        self.audit["state_skipped_support"] = state.skipped_support
        return super().finalize_with_tracker()


def day_summary(result):
    date, _path, _rows, aggregate = result
    summed = defaultdict(int)
    market_audit = aggregate["data_audit"]["market_audit"]
    for audit in market_audit.values():
        for key, value in audit.items():
            if isinstance(value, int):
                summed[key] += value
    return {
        "date": date,
        "markets": len(market_audit),
        "admissions": len(aggregate["admissions"]),
        "cycles": len(aggregate["cycles"]),
        "A1_failures": summed["A1_failures"],
        "post_allocation_eta_skips": summed[
            "post_allocation_eta_skips"
        ],
        "post_allocation_eta_skip_y": summed[
            "post_allocation_eta_skip_y"
        ],
        "post_allocation_eta_skip_n": summed[
            "post_allocation_eta_skip_n"
        ],
        "post_allocation_zero_flow_y": summed[
            "post_allocation_zero_flow_y"
        ],
        "post_allocation_zero_flow_n": summed[
            "post_allocation_zero_flow_n"
        ],
        "state_eligible_decisions": summed[
            "state_eligible_decisions"
        ],
        "state_skipped_support": summed["state_skipped_support"],
        "negative_level_failures": summed["negative_level_failures"],
        "clock_null_failures": summed["clock_null_failures"],
        "clock_identity_failures": summed["clock_identity_failures"],
        "sequence_failures": summed["sequence_failures"],
        "book_events": summed["book_events"],
        "trade_events": summed["trade_events"],
    }


def main():
    if tuple(F.TRAIN_DATES) != tuple(C.ALLOWED_DATES):
        raise RuntimeError("date allowlist mismatch")
    if "2026-07-23" in F.TRAIN_DATES:
        raise RuntimeError("contaminated date in preflight")
    C.verify_pinned_contract_tools()
    F.CausalTrackedMarket = AdmissionPreflightMarket
    F.D.JsonlGzipWriter = NullWriter
    tasks = [
        (date, f"/tmp/preflight-null-{date}")
        for date in F.TRAIN_DATES
    ]
    results = []
    with M.OriginalPool(processes=3) as pool:
        try:
            for result in pool.imap_unordered(
                F.run_day, tasks, chunksize=1
            ):
                summary = day_summary(result)
                print(
                    "PREFLIGHT_DAY "
                    + json.dumps(summary, sort_keys=True),
                    flush=True,
                )
                results.append(result)
        except BaseException:
            pool.terminate()
            print("PREFLIGHT_FAIL_FAST", flush=True)
            raise
    summaries = sorted(
        (day_summary(result) for result in results),
        key=lambda row: row["date"],
    )
    totals = {
        key: sum(row[key] for row in summaries)
        for key in summaries[0]
        if key not in ("date",)
    }
    failure_keys = (
        "A1_failures",
        "negative_level_failures",
        "clock_null_failures",
        "clock_identity_failures",
        "sequence_failures",
    )
    assert all(totals[key] == 0 for key in failure_keys)
    receipt = {
        "schema": "z3-a1skip-admission-preflight-v1",
        "dates_opened": list(F.TRAIN_DATES),
        "date_2026_07_23_read": False,
        "per_day": summaries,
        "totals": totals,
        "gate_pass": True,
    }
    print(
        "PREFLIGHT_RECEIPT "
        + json.dumps(receipt, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
