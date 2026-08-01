#!/usr/bin/env python3
"""Logging-only revision of the sealed receive-clock post-fill replay.

All episodes still run through the full strategy, queue, policy, IOC, and
markout state machines.  To remove the demonstrated logging bottleneck, only
an outcome-blind deterministic 1/16 sample of episode identities emits
per-event trajectory JSON.  Full policy rows, markouts, and aggregate metrics
remain in memory and in the episode/report artifacts for every episode.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import sys


FULL_CONSUMER = os.environ.get(
    "ROUND3_FULL_CONSUMER",
    "/tmp/z3_postfill_stopping_receive_clock.py",
)
FULL_CONSUMER_SHA256 = (
    "ee2e49cb011557e207bd0774f05573ca0f0b35563341d6647bf7547a7bbe1a13"
)
TRAJECTORY_SAMPLE_MODULUS = 16
TRAJECTORY_SAMPLE_BUCKET = 0


def file_sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_full_consumer():
    if file_sha(FULL_CONSUMER) != FULL_CONSUMER_SHA256:
        raise RuntimeError("sealed full-trajectory consumer SHA mismatch")
    spec = importlib.util.spec_from_file_location(
        "sealed_receive_clock_full_consumer", FULL_CONSUMER
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


F = load_full_consumer()


def trajectory_selected(episode_id):
    digest = hashlib.sha256(str(episode_id).encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % TRAJECTORY_SAMPLE_MODULUS
    return bucket == TRAJECTORY_SAMPLE_BUCKET


class SamplingWriter:
    """Filter only the diagnostic stream; never strategy state or results."""

    def __init__(self, delegate):
        self.delegate = delegate

    def emit(self, row):
        episode_id = row.get("episode_id")
        if episode_id is None or trajectory_selected(episode_id):
            self.delegate.emit(row)


class SampledEpisodeTracker(F.D.EpisodeTracker):
    def __init__(self, market_sim, writer):
        if not isinstance(writer, SamplingWriter):
            writer = SamplingWriter(writer)
        super().__init__(market_sim, writer)

    def observe(self, ts, event_kind, event_fields):
        for episode in self.active:
            if not trajectory_selected(episode.episode_id):
                continue
            touch, distance = self._touch_distance(episode)
            self.writer.emit(
                {
                    "record_type": "EVENT",
                    "episode_id": episode.episode_id,
                    "market": episode.market,
                    "ts": int(ts),
                    "elapsed_s": (int(ts) - episode.first_ts) / 1e6,
                    "event_kind": event_kind,
                    "event": event_fields,
                    "counterpart_side": episode.counterpart_side,
                    "counterpart_lvl": episode.counterpart_lvl,
                    "counterpart_queue_ahead": episode.counterpart_ahead,
                    "counterpart_touch": touch,
                    "counterpart_distance_from_touch": distance,
                    "counterpart_flow60": self._recent_flow(
                        episode.counterpart_side,
                        episode.counterpart_lvl,
                    ),
                    "held_side_flow60": self._recent_flow(
                        episode.held_side,
                        episode.entry_e4,
                    ),
                    "immediate_ioc_pnl_c_including_fee": self._ioc_pnl(
                        episode
                    ),
                    "distance_trigger_ts": episode.distance_trigger_ts,
                    "current_trigger_reason": episode.current_trigger_reason,
                    "maker_pair_fill_ts": episode.maker_fill_ts,
                    "maker_pair_filled_this_event": (
                        episode.maker_fill_ts == int(ts)
                    ),
                    "policy_results_resolved": episode.resolved,
                }
            )
        self._resolve_ready(ts)


FullCausalTrackedMarket = F.CausalTrackedMarket


class SampledCausalTrackedMarket(FullCausalTrackedMarket):
    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, policies, writer)
        self.tracker = SampledEpisodeTracker(self, writer)


PREREGISTRATION = copy.deepcopy(F.PREREGISTRATION)
PREREGISTRATION.update(
    {
        "schema": (
            "z3-postfill-optimal-stopping-prereg-v3-sampled-trajectory"
        ),
        "trajectory": (
            "full policy/queue/IOC/markout state is evaluated for every "
            "episode; per-event gzip JSON is emitted only when "
            "uint64_be(SHA256(episode_id)[0:8]) mod 16 == 0, with "
            "episode_id=market|admit_ts|first_ts. Selection is fixed before "
            "outcomes and independent of result/PnL. Start, event, policy, "
            "and end rows use the same filter. All episode policy outcomes, "
            "8-horizon markouts, and aggregate metrics remain full-population."
        ),
        "logging_only_revision": {
            "only_change_from_v2": (
                "skip construction/emission of per-event diagnostic rows for "
                "15/16 deterministic outcome-blind episode buckets"
            ),
            "full_receive_clock_consumer_sha256": FULL_CONSUMER_SHA256,
            "sample_hash": "SHA256",
            "sample_identity": "market|admit_ts|first_ts",
            "sample_uint": "first 8 digest bytes, unsigned big-endian",
            "sample_modulus": TRAJECTORY_SAMPLE_MODULUS,
            "included_bucket": TRAJECTORY_SAMPLE_BUCKET,
            "main_state_semantics_changed": False,
            "full_episode_policy_rows": True,
            "full_episode_markouts": True,
            "full_aggregate_metrics": True,
        },
        "predecessor_attempts": [
            {
                "script_sha256": (
                    "efa402bfa2a45cb6624bae483a4de59b1c176400168ef87c7ab247d737c9e381"
                ),
                "status": "terminated_after_contamination_confirmed",
                "results_eligible": False,
            },
            {
                "script_sha256": FULL_CONSUMER_SHA256,
                "status": "terminated_logging_bottleneck",
                "results_eligible": False,
            },
        ],
    }
)

# The frozen module owns run_day/main; redirect only its market constructor,
# script identity, and pre-registration globals before invoking it.
F.CausalTrackedMarket = SampledCausalTrackedMarket
F.PREREGISTRATION = PREREGISTRATION
F.__file__ = __file__


if __name__ == "__main__":
    F.main()
