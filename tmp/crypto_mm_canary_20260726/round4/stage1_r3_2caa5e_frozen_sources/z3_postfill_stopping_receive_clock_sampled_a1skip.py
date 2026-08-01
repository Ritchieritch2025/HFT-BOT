#!/usr/bin/env python3
"""Fail-closed post-allocation ETA revision of the sampled causal replay.

The economic change is deliberately narrow: after the frozen allocator moves
away from touch, recompute/inspect both allocated-level ETAs and skip the
admission when either is missing, non-finite, or non-positive.  The existing
admission assertion remains as a fail-closed invariant.

The runner also reports completed dates as they return and propagates a worker
exception immediately.  It restores input-date order before aggregation, so
this observability-only change does not alter event or result semantics.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import math
import os
import sys


SAMPLED_PARENT = os.environ.get(
    "ROUND3_SAMPLED_PARENT",
    "/tmp/z3_postfill_stopping_receive_clock_sampled.py",
)
SAMPLED_PARENT_SHA256 = (
    "566fde5ee3bd854330c9de3586add8503c06511d334fbee66220b339a06ad14f"
)


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_sampled_parent():
    if file_sha(SAMPLED_PARENT) != SAMPLED_PARENT_SHA256:
        raise RuntimeError("sealed sampled parent SHA mismatch")
    spec = importlib.util.spec_from_file_location(
        "sealed_sampled_receive_clock_parent", SAMPLED_PARENT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


S = load_sampled_parent()
F = S.F


def invalid_eta_sides(allocation):
    invalid = {}
    for side in ("y", "n"):
        eta = allocation.get(f"pred_eta_{side}_s")
        if eta is None:
            invalid[side] = "missing"
        elif not math.isfinite(float(eta)):
            invalid[side] = "nonfinite"
        elif float(eta) <= 0:
            invalid[side] = "nonpositive"
    return invalid


class EtaGuardedSampledMarket(S.SampledCausalTrackedMarket):
    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, policies, writer)
        self.audit.update(
            {
                "post_allocation_eta_skips": 0,
                "post_allocation_eta_skip_y": 0,
                "post_allocation_eta_skip_n": 0,
                "post_allocation_zero_flow_y": 0,
                "post_allocation_zero_flow_n": 0,
            }
        )

    def _allocate(self, policy, state, yb, nb, features):
        allocation = super()._allocate(
            policy, state, yb, nb, features
        )
        if allocation is None:
            return None
        invalid = invalid_eta_sides(allocation)
        if not invalid:
            return allocation
        state.skipped_support += 1
        self.audit["post_allocation_eta_skips"] += 1
        for side in invalid:
            self.audit[f"post_allocation_eta_skip_{side}"] += 1
            flow = allocation.get(f"pred_flow_{side}60")
            if flow is not None and float(flow) == 0:
                self.audit[f"post_allocation_zero_flow_{side}"] += 1
        return None


OriginalPool = F.mp.Pool


class FailFastOrderedPool:
    """Pool adapter: fail on first completed error, preserve map order."""

    def __init__(self, *args, **kwargs):
        self.delegate = OriginalPool(*args, **kwargs)

    def __enter__(self):
        self.delegate.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.delegate.__exit__(exc_type, exc, tb)

    def map(self, func, iterable):
        items = list(iterable)
        by_date = {}
        try:
            for result in self.delegate.imap_unordered(
                func, items, chunksize=1
            ):
                date = result[0]
                print(f"DAY_COMPLETE {date}", flush=True)
                by_date[date] = result
        except BaseException:
            self.delegate.terminate()
            print("DAY_FAIL_FAST", flush=True)
            raise
        return [by_date[item[0]] for item in items]


PREREGISTRATION = copy.deepcopy(S.PREREGISTRATION)
PREREGISTRATION.update(
    {
        "schema": (
            "z3-postfill-optimal-stopping-prereg-v4-"
            "sampled-trajectory-a1-skip"
        ),
        "post_allocation_eta_revision": {
            "only_economic_change_from_sampled_v3": (
                "after the frozen allocator returns a candidate, require "
                "pred_eta_y_s and pred_eta_n_s to be present, finite, and "
                "strictly positive; otherwise increment skipped_support and "
                "do not admit"
            ),
            "admission_A1_assertion_retained": True,
            "invalid_eta_is_not_clamped": True,
            "zero_flow_is_not_imputed": True,
            "triggering_evidence": {
                "date": "2026-07-20",
                "market": "KXBTC15M-26JUL200915-15",
                "local_recv_ts_us": 1784553054242655,
                "allocation_variant": (
                    "spend1_fallback_shift1_n_slow"
                ),
                "allocated_yes_e4": 8900,
                "allocated_no_e4": 970,
                "pred_flow_y60": 0.0,
                "pred_eta_y_s": "Infinity",
                "classification": (
                    "legal zero-flow allocated level; upstream candidate "
                    "must be skipped"
                ),
            },
        },
        "runner_observability_revision": {
            "economic_or_event_change": False,
            "worker_results_consumed": "completion order",
            "worker_error": "terminate siblings and propagate immediately",
            "aggregation_order": (
                "restored to preregistered TRAIN_DATES input order"
            ),
        },
        "predecessor_attempts": [
            *S.PREREGISTRATION.get("predecessor_attempts", []),
            {
                "script_sha256": SAMPLED_PARENT_SHA256,
                "status": "VOID_A1_FINITE_POSITIVE_GATE_FAILURE",
                "results_eligible": False,
                "failure_market": "KXBTC15M-26JUL200915-15",
                "failure_ts": 1784553054242655,
            },
        ],
    }
)

# The sealed parent owns run_day/main.  Replace only the market constructor,
# run-result collection adapter, identity, and pre-registration globals.
F.CausalTrackedMarket = EtaGuardedSampledMarket
F.mp.Pool = FailFastOrderedPool
F.PREREGISTRATION = PREREGISTRATION
F.__file__ = __file__


if __name__ == "__main__":
    F.main()
