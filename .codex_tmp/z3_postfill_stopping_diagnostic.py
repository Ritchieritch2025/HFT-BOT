#!/usr/bin/env python3
"""Post-fill optimal-stopping headroom diagnostic (DISCOVERY only).

This imports the sealed P3 replay rather than reimplementing its admission,
allocation, queue, fee, or current-exit semantics.  One P3 state generates the
same first fills as the sealed run.  Each first fill then spawns an independent
shadow trajectory which keeps the original counterpart maker quote alive for
up to 60.06 seconds and records every trade/book event.

The counterfactual policies are fixed before data access:

* IOC_60MS: request cancel at the first fill; IOC after the 60ms ACK window.
* WAIT_W: keep maker live for W seconds, request cancel at W, IOC at W+60ms.
* CURRENT_DIST2_TTL60: request cancel on the first event where counterpart
  touch is >=2c above the quote, otherwise on the first event at/after 60s;
  IOC after the 60ms ACK window.
* CLAIRVOYANT_BEST_FIXED: per-episode best realized result among the policies
  above, ties resolved by shorter capital time and then policy name.

Maker fills during any cancel-ACK window count as maker pairs.  IOC P&L walks
the held-side displayed bids and includes the official rounded taker fee.  The
oracle is an opportunity-level upper bound: shadow episodes may overlap and it
does not model policy-dependent re-entry.  It is never a deployable candidate.

Only 2026-07-20..22 are opened.  2026-07-23 is discovery-contaminated and is
not read by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb


BASE_SCRIPT = os.environ.get(
    "Z3_PRICE_BASE_SCRIPT", "/tmp/z3_price_allocation_train.py"
)
BASE_SCRIPT_SHA256 = (
    "81c35b2982db9d892fb2c97d55f572681d5a60631b6367023e9330cdfc0feb5b"
)
TRAIN_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
LAT_US = 60_000
CLIP_E4 = 10_000
CLIP_CT = 1
DISTANCE_E4 = 200
MAX_WAIT_US = 60_000_000
REPORT_OUT = os.environ.get(
    "Z3_POSTFILL_REPORT", "/tmp/z3_postfill_stopping_report.json"
)
EPISODES_OUT = os.environ.get(
    "Z3_POSTFILL_EPISODES", "/tmp/z3_postfill_stopping_episodes.json.gz"
)
TRAJECTORY_PREFIX = os.environ.get(
    "Z3_POSTFILL_TRAJECTORY_PREFIX", "/tmp/z3_postfill_trajectory"
)
PREREG_PATH = os.environ.get(
    "Z3_POSTFILL_PREREG", "/tmp/z3_postfill_stopping_prereg.json"
)

FIXED_WAIT_REQUEST_S = {
    "IOC_60MS": 0.0,
    "WAIT_0P25": 0.25,
    "WAIT_0P5": 0.5,
    "WAIT_1": 1.0,
    "WAIT_2": 2.0,
    "WAIT_5": 5.0,
    "WAIT_10": 10.0,
    "WAIT_30": 30.0,
    "WAIT_60": 60.0,
}
MARKOUT_HORIZONS_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
CURRENT_POLICY = "CURRENT_DIST2_TTL60"
ORACLE_POLICY = "CLAIRVOYANT_BEST_FIXED"

PREREGISTRATION = {
    "schema": "z3-postfill-optimal-stopping-prereg-v1",
    "status": "DISCOVERY_HEADROOM_ONLY",
    "dates_opened": list(TRAIN_DATES),
    "date_2026_07_23": "DISCOVERY_CONTAMINATED_AND_NOT_READ",
    "base_script_sha256": BASE_SCRIPT_SHA256,
    "entry_and_fill_model": {
        "arm": "P3_SPEND1 imported from sealed base",
        "clip_contracts": 1,
        "pair_cost_max_c": 99,
        "maker_queue_ahead": "exact-level displayed quantity only",
        "maker_fill": "strict eligible taker volume > queue_ahead + clip",
        "cancel_ack_latency_ms": 60,
        "ioc": "walk held-side displayed bids; official rounded taker fee; remainder settles",
    },
    "fixed_policies": {
        **{
            name: (
                f"maker wait {wait:g}s; cancel request then IOC after 60ms "
                "unless maker pair fills strictly before ACK"
            )
            for name, wait in FIXED_WAIT_REQUEST_S.items()
        },
        CURRENT_POLICY: (
            "first event where counterpart touch-quote>=2c, fallback first "
            "event at/after 60s; cancel request then IOC after 60ms unless "
            "maker pair fills strictly before ACK"
        ),
        ORACLE_POLICY: (
            "per-episode max realized PnL among fixed waits plus current; "
            "tie shorter capital time then policy name; opportunity-level "
            "clairvoyant upper bound only"
        ),
    },
    "trajectory": (
        "one gzip JSONL row for every trade/book event through the later of "
        "policy resolution and the 60s markout for each independent shadow "
        "episode; fields include elapsed, counterpart quote/queue/touch, "
        "recent executable flow, fee-inclusive immediate IOC PnL, distance "
        "trigger, and maker-fill state"
    ),
    "diagnostic_markouts": (
        "for every first fill, fee-inclusive immediate IOC value is sampled "
        "as-of first fill and at 0.25/0.5/1/2/5/10/30/60s using only the "
        "latest book at or before each horizon; relative markout is horizon "
        "IOC value minus first-fill IOC value"
    ),
    "event_clock_and_no_lookahead": (
        "reuse sealed replay order: consume each trade before the enclosing "
        "book event, update strict maker queue on that trade, and apply a book "
        "message only after all prior/equal-time queued trades; every timed "
        "IOC/markout snapshot is taken before the first event at/after its "
        "scheduled time from the latest already-applied book, with an enforced "
        "book_asof_ts<=scheduled_ts invariant"
    ),
    "metrics": (
        "n, markets, EV, market-cluster bootstrap CI95, paired improvement "
        "versus current with cluster CI95, mean/median capital time"
    ),
    "baseline_gate": (
        "sealed P3 must reproduce cycles=878, pairs=433, markets=44, "
        "mean=-1.1702c; CURRENT results are sourced directly from those sealed "
        "cycles while shadow-current agreement is reported as an audit"
    ),
    "interpretation": (
        "oracle point EV<=0 kills mechanism; oracle >0 reports timing headroom "
        "only and cannot be called validation or a candidate"
    ),
}


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_base():
    if file_sha(BASE_SCRIPT) != BASE_SCRIPT_SHA256:
        raise RuntimeError("sealed base script SHA mismatch")
    spec = importlib.util.spec_from_file_location("sealed_z3_price_base", BASE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


B = load_base()
P3 = next(policy for policy in B.policies() if policy.name == "P3_SPEND1")


@dataclass
class Episode:
    episode_id: str
    market: str
    result: str
    admission: dict
    first_ts: int
    held_side: str
    entry_e4: int
    counterpart_side: str
    counterpart_lvl: int
    counterpart_ahead: int
    pair_gain_c: float
    maker_fill_ts: int | None = None
    distance_trigger_ts: int | None = None
    current_trigger_reason: str | None = None
    ioc_snapshots: dict = field(default_factory=dict)
    markout_snapshots: dict = field(default_factory=dict)
    resolved: bool = False
    markouts_emitted: bool = False


class JsonlGzipWriter:
    def __init__(self, path, date):
        self.path = path
        self.fh = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
        self.rows = 0
        self.emit(
            {
                "record_type": "META",
                "date": date,
                "schema": "z3-postfill-trajectory-v1",
                "script_sha256_filled_after_run_in_report": True,
            }
        )

    def emit(self, row):
        self.fh.write(json.dumps(row, separators=(",", ":"), allow_nan=False))
        self.fh.write("\n")
        self.rows += 1

    def close(self):
        self.fh.close()


class EpisodeTracker:
    def __init__(self, market_sim, writer):
        self.market_sim = market_sim
        self.writer = writer
        self.active = []
        self.results = []
        self.markouts = []
        self.episodes_started = 0

    def _ioc_pnl(self, episode):
        book = self.market_sim.books[episode.held_side]
        fills, filled = B.walk_book_sell(book, CLIP_E4)
        remaining = CLIP_E4 - filled
        total_c = sum(
            (price - episode.entry_e4) / 100.0 * (qty / 10000.0)
            for price, qty in fills
        )
        if filled:
            total_c -= B.taker_fee_c_per_order(fills)
        if remaining:
            won = (episode.result == "yes") == (episode.held_side == "y")
            settle = 10000 if won else 0
            total_c += (
                (settle - episode.entry_e4)
                / 100.0
                * (remaining / 10000.0)
            )
        return total_c / CLIP_CT

    def _recent_flow(self, side, level):
        total = 0
        for _ts, yes_px, qty, taker in self.market_sim.recent_trades:
            if side == "y":
                if taker == "no" and yes_px <= level:
                    total += qty
            elif taker == "yes" and 10000 - yes_px <= level:
                total += qty
        return total / 10000.0

    def _touch_distance(self, episode):
        touch = B.best(self.market_sim.books[episode.counterpart_side])
        if touch is None:
            return None, None
        return touch, touch - episode.counterpart_lvl

    def _evaluate_current_trigger(self, episode, ts):
        if episode.distance_trigger_ts is not None:
            return
        touch, distance = self._touch_distance(episode)
        if distance is not None and distance >= DISTANCE_E4:
            episode.distance_trigger_ts = ts
            episode.current_trigger_reason = "distance_2c"
            return
        if ts - episode.first_ts >= MAX_WAIT_US:
            # Match the event-driven sealed engine: TTL fires on the first
            # observed event at/after 60 seconds, not on an invented book tick.
            episode.distance_trigger_ts = ts
            episode.current_trigger_reason = "ttl_60s"

    @staticmethod
    def _horizon_key(horizon_s):
        return f"{horizon_s:g}s"

    def _ioc_snapshot_for_episode(
        self, episode, scheduled_ts, observed_event_ts
    ):
        # Keep snapshot construction in one place so every policy and markout
        # has the same no-future-book audit fields.
        book_asof_ts = self.market_sim.book_asof_ts
        if book_asof_ts is not None and book_asof_ts > scheduled_ts:
            raise RuntimeError(
                "future book used for IOC snapshot "
                f"book={book_asof_ts} scheduled={scheduled_ts}"
            )
        return {
            "scheduled_ts": int(scheduled_ts),
            "observed_event_ts": int(observed_event_ts),
            "book_asof_ts": (
                None if book_asof_ts is None else int(book_asof_ts)
            ),
            "pnl_c": self._ioc_pnl(episode),
        }

    def _capture_markouts_before_event(self, episode, ts):
        for horizon_s in MARKOUT_HORIZONS_S:
            key = self._horizon_key(horizon_s)
            due = episode.first_ts + int(round(horizon_s * 1e6))
            if key not in episode.markout_snapshots and due <= ts:
                episode.markout_snapshots[key] = self._ioc_snapshot_for_episode(
                    episode, due, ts
                )

    def start(self, state, ts):
        other = "n" if state.orphan_side == "y" else "y"
        quote = state.quotes[other]
        if quote is None:
            raise RuntimeError("first fill without counterpart shadow quote")
        admission = dict(state.admission or {})
        episode_id = (
            f"{self.market_sim.ticker}|{int(admission.get('ts', 0))}|{int(ts)}"
        )
        episode = Episode(
            episode_id=episode_id,
            market=self.market_sim.ticker,
            result=self.market_sim.result,
            admission=admission,
            first_ts=int(ts),
            held_side=state.orphan_side,
            entry_e4=int(state.orphan_entry),
            counterpart_side=other,
            counterpart_lvl=int(quote.lvl),
            counterpart_ahead=int(quote.ahead),
            pair_gain_c=(10000 - state.orphan_entry - quote.lvl) / 100.0,
        )
        if episode.pair_gain_c < 1.0 - 1e-9:
            raise RuntimeError("unsafe pair in post-fill episode")
        self.episodes_started += 1
        self.active.append(episode)
        episode.markout_snapshots["first_fill"] = (
            self._ioc_snapshot_for_episode(episode, episode.first_ts, ts)
        )
        self._evaluate_current_trigger(episode, ts)
        self.writer.emit(
            {
                "record_type": "EPISODE_START",
                "episode_id": episode_id,
                "market": episode.market,
                "admit_ts": admission.get("ts"),
                "first_ts": episode.first_ts,
                "held_side": episode.held_side,
                "entry_e4": episode.entry_e4,
                "counterpart_side": episode.counterpart_side,
                "counterpart_lvl": episode.counterpart_lvl,
                "counterpart_ahead": episode.counterpart_ahead,
                "pair_gain_c": episode.pair_gain_c,
                "admission_features": admission,
            }
        )

    def before_event(self, ts):
        for episode in self.active:
            self._capture_markouts_before_event(episode, ts)
            if not episode.resolved:
                self._evaluate_current_trigger(episode, ts)
            for name, request_s in FIXED_WAIT_REQUEST_S.items():
                due = episode.first_ts + int(round((request_s + 0.06) * 1e6))
                if (
                    not episode.resolved
                    and name not in episode.ioc_snapshots
                    and due <= ts
                ):
                    episode.ioc_snapshots[name] = (
                        self._ioc_snapshot_for_episode(episode, due, ts)
                    )
            if (
                not episode.resolved
                and episode.distance_trigger_ts is not None
            ):
                due = episode.distance_trigger_ts + LAT_US
                if CURRENT_POLICY not in episode.ioc_snapshots and due <= ts:
                    episode.ioc_snapshots[CURRENT_POLICY] = (
                        self._ioc_snapshot_for_episode(episode, due, ts)
                    )

    def apply_trade(self, ts, yes_px, qty, taker):
        for episode in self.active:
            if episode.resolved or episode.maker_fill_ts is not None:
                continue
            sell_taker = "no" if episode.counterpart_side == "y" else "yes"
            if taker != sell_taker:
                continue
            px_side = (
                yes_px
                if episode.counterpart_side == "y"
                else 10000 - yes_px
            )
            if px_side <= episode.counterpart_lvl:
                episode.counterpart_ahead -= qty
                if episode.counterpart_ahead < -CLIP_E4:
                    episode.maker_fill_ts = int(ts)

    def observe(self, ts, event_kind, event_fields):
        for episode in self.active:
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
                        episode.counterpart_side, episode.counterpart_lvl
                    ),
                    "held_side_flow60": self._recent_flow(
                        episode.held_side, episode.entry_e4
                    ),
                    "immediate_ioc_pnl_c_including_fee": self._ioc_pnl(episode),
                    "distance_trigger_ts": episode.distance_trigger_ts,
                    "current_trigger_reason": episode.current_trigger_reason,
                    "maker_pair_fill_ts": episode.maker_fill_ts,
                    "maker_pair_filled_this_event": episode.maker_fill_ts == int(ts),
                    "policy_results_resolved": episode.resolved,
                }
            )
        self._resolve_ready(ts)

    def after_book(self, ts, event_fields):
        for episode in self.active:
            if not episode.resolved:
                self._evaluate_current_trigger(episode, ts)
        self.before_event(ts)
        self.observe(ts, "book", event_fields)

    def _policy_result(self, episode, name, due):
        if episode.maker_fill_ts is not None and episode.maker_fill_ts < due:
            return {
                "policy": name,
                "pnl_c": episode.pair_gain_c,
                "capital_time_s": (
                    episode.maker_fill_ts - episode.first_ts
                ) / 1e6,
                "exit_kind": "maker_pair",
                "exit_ts": episode.maker_fill_ts,
            }
        snap = episode.ioc_snapshots.get(name)
        if snap is None:
            return None
        return {
            "policy": name,
            "pnl_c": snap["pnl_c"],
            "capital_time_s": (
                snap["scheduled_ts"] - episode.first_ts
            ) / 1e6,
            "exit_kind": "ioc",
            "exit_ts": snap["scheduled_ts"],
            "ioc_book_asof_ts": snap["book_asof_ts"],
            "ioc_observed_event_ts": snap["observed_event_ts"],
        }

    def _all_resolvable(self, episode):
        for name, request_s in FIXED_WAIT_REQUEST_S.items():
            due = episode.first_ts + int(round((request_s + 0.06) * 1e6))
            if self._policy_result(episode, name, due) is None:
                return False
        return self._current_policy_result(episode) is not None

    def _current_policy_result(self, episode):
        if episode.distance_trigger_ts is None:
            if episode.maker_fill_ts is None:
                return None
            return {
                "policy": CURRENT_POLICY,
                "pnl_c": episode.pair_gain_c,
                "capital_time_s": (
                    episode.maker_fill_ts - episode.first_ts
                ) / 1e6,
                "exit_kind": "maker_pair",
                "exit_ts": episode.maker_fill_ts,
            }
        current_due = episode.distance_trigger_ts + LAT_US
        return self._policy_result(episode, CURRENT_POLICY, current_due)

    def _resolve_episode(self, episode):
        rows = []
        for name, request_s in FIXED_WAIT_REQUEST_S.items():
            due = episode.first_ts + int(round((request_s + 0.06) * 1e6))
            rows.append(self._policy_result(episode, name, due))
        rows.append(self._current_policy_result(episode))
        if any(row is None for row in rows):
            raise RuntimeError("resolving episode with missing policy result")
        for row in rows:
            row.update(
                {
                    "episode_id": episode.episode_id,
                    "market": episode.market,
                    "admit_ts": episode.admission.get("ts"),
                    "first_ts": episode.first_ts,
                    "held_side": episode.held_side,
                    "entry_e4": episode.entry_e4,
                    "counterpart_lvl": episode.counterpart_lvl,
                    "pair_gain_c": episode.pair_gain_c,
                    "current_trigger_reason": episode.current_trigger_reason,
                }
            )
        oracle_source = min(
            rows,
            key=lambda row: (
                -row["pnl_c"],
                row["capital_time_s"],
                row["policy"],
            ),
        )
        oracle = dict(oracle_source)
        oracle["selected_source_policy"] = oracle_source["policy"]
        oracle["policy"] = ORACLE_POLICY
        rows.append(oracle)
        self.results.extend(rows)
        episode.resolved = True
        self.writer.emit(
            {
                "record_type": "POLICY_RESULTS",
                "episode_id": episode.episode_id,
                "market": episode.market,
                "maker_fill_ts": episode.maker_fill_ts,
                "distance_trigger_ts": episode.distance_trigger_ts,
                "current_trigger_reason": episode.current_trigger_reason,
                "policy_results": rows,
            }
        )

    def _all_markouts_captured(self, episode):
        return all(
            self._horizon_key(horizon_s) in episode.markout_snapshots
            for horizon_s in MARKOUT_HORIZONS_S
        )

    def _emit_markouts(self, episode):
        if episode.markouts_emitted:
            return
        if not self._all_markouts_captured(episode):
            raise RuntimeError("emitting incomplete markout record")
        record = {
            "episode_id": episode.episode_id,
            "market": episode.market,
            "admit_ts": episode.admission.get("ts"),
            "first_ts": episode.first_ts,
            "held_side": episode.held_side,
            "entry_e4": episode.entry_e4,
            "first_fill": episode.markout_snapshots["first_fill"],
            "horizons": {
                self._horizon_key(horizon_s): episode.markout_snapshots[
                    self._horizon_key(horizon_s)
                ]
                for horizon_s in MARKOUT_HORIZONS_S
            },
        }
        self.markouts.append(record)
        episode.markouts_emitted = True
        self.writer.emit(
            {
                "record_type": "EPISODE_END",
                **record,
                "maker_fill_ts": episode.maker_fill_ts,
                "policy_results_resolved": episode.resolved,
            }
        )

    def _resolve_ready(self, ts):
        for episode in self.active:
            if not episode.resolved and self._all_resolvable(episode):
                self._resolve_episode(episode)
            if episode.resolved and self._all_markouts_captured(episode):
                self._emit_markouts(episode)
        self.active = [
            episode
            for episode in self.active
            if not (episode.resolved and episode.markouts_emitted)
        ]

    def finish(self, last_ts):
        # Market streams extend beyond the longest horizon.  This fallback only
        # handles an unexpectedly sparse tail using the last observed book.
        for episode in self.active:
            for horizon_s in MARKOUT_HORIZONS_S:
                key = self._horizon_key(horizon_s)
                due = episode.first_ts + int(round(horizon_s * 1e6))
                if key not in episode.markout_snapshots:
                    episode.markout_snapshots[key] = (
                        self._ioc_snapshot_for_episode(
                            episode, due, int(last_ts)
                        )
                    )
            if episode.distance_trigger_ts is None:
                episode.distance_trigger_ts = max(
                    episode.first_ts + MAX_WAIT_US, int(last_ts)
                )
                episode.current_trigger_reason = "ttl_60s_sparse_tail"
            if not episode.resolved:
                for name, request_s in FIXED_WAIT_REQUEST_S.items():
                    due = episode.first_ts + int(
                        round((request_s + 0.06) * 1e6)
                    )
                    if (
                        name not in episode.ioc_snapshots
                        and not (
                            episode.maker_fill_ts is not None
                            and episode.maker_fill_ts < due
                        )
                    ):
                        episode.ioc_snapshots[name] = (
                            self._ioc_snapshot_for_episode(
                                episode, due, int(last_ts)
                            )
                        )
                if (
                    CURRENT_POLICY not in episode.ioc_snapshots
                    and episode.maker_fill_ts is None
                ):
                    due = episode.distance_trigger_ts + LAT_US
                    episode.ioc_snapshots[CURRENT_POLICY] = (
                        self._ioc_snapshot_for_episode(
                            episode, due, int(last_ts)
                        )
                    )
            if not episode.resolved:
                self._resolve_episode(episode)
            self._emit_markouts(episode)
        self.active = []


class TrackedMarket(B.Z3Market):
    def __init__(self, ticker, close_us, result, trades, policies, writer):
        super().__init__(ticker, close_us, result, trades, policies)
        self.book_asof_ts = None
        self.tracker = EpisodeTracker(self, writer)

    def _fill(self, policy, state, side, level, ts, was_cancel_pending):
        first_fill = state.orphan_side is None
        super()._fill(
            policy,
            state,
            side,
            level,
            ts,
            was_cancel_pending=was_cancel_pending,
        )
        if first_fill and state.orphan_side is not None:
            self.tracker.start(state, ts)

    def _consume_trades(self, upto):
        while (
            self.trade_i < len(self.trades)
            and self.trades[self.trade_i][0] <= upto
        ):
            ts, yes_px, qty, taker = self.trades[self.trade_i]
            self.trade_i += 1
            self.tracker.before_event(ts)
            self.tracker.apply_trade(ts, yes_px, qty, taker)
            for policy in self.policies:
                state = self.states[policy.name]
                self._advance(policy, state, ts)
                for side, sell_taker in (("y", "no"), ("n", "yes")):
                    quote = state.quotes[side]
                    if quote is None or taker != sell_taker:
                        continue
                    px_side = yes_px if side == "y" else 10000 - yes_px
                    if px_side <= quote.lvl:
                        quote.ahead -= qty
                        if quote.ahead < -CLIP_E4:
                            self._fill(
                                policy,
                                state,
                                side,
                                quote.lvl,
                                ts,
                                was_cancel_pending=quote.cx is not None,
                            )
            self.recent_trades.append((ts, yes_px, qty, taker))
            self._trim_flow(ts)
            self.flow_cache = {}
            self.tracker.observe(
                ts,
                "trade",
                {
                    "yes_price_e4": yes_px,
                    "count_e4": qty,
                    "taker_side": taker,
                },
            )

    def on_book(self, ts, mtype, side, px, delta, yes_levels, no_levels):
        if not self.close:
            return
        if self.first_event_ts is None:
            self.first_event_ts = ts
        self.last_ts = ts
        self._consume_trades(ts)
        for policy in self.policies:
            self._advance(policy, self.states[policy.name], ts)
        self.tracker.before_event(ts)
        if mtype == "snapshot":
            self.books = {"y": {}, "n": {}}
            for levels, key in ((yes_levels, "y"), (no_levels, "n")):
                for price, volume in levels or []:
                    if price is not None:
                        self.books[key][price] = volume or 0
            self.book_asof_ts = int(ts)
        else:
            if px is None or side not in ("yes", "no"):
                self.tracker.after_book(
                    ts,
                    {
                        "msg_type": mtype,
                        "side": side,
                        "price_e4": px,
                        "delta_e4": delta,
                        "ignored_by_base": True,
                    },
                )
                return
            key = "y" if side == "yes" else "n"
            self.books[key][px] = self.books[key].get(px, 0) + (delta or 0)
            self.book_asof_ts = int(ts)
        self.flow_cache = {}
        self.tracker.after_book(
            ts,
            {
                "msg_type": mtype,
                "side": side,
                "price_e4": px,
                "delta_e4": delta,
            },
        )
        eligible = self._z3_now(ts)
        for policy in self.policies:
            state = self.states[policy.name]
            self._advance(policy, state, ts)
            if state.orphan_side is not None:
                continue
            has_quote = (
                state.quotes["y"] is not None or state.quotes["n"] is not None
            )
            if has_quote:
                if eligible is None:
                    self._request_cancel(state, "y", ts, "zone_invalid")
                    self._request_cancel(state, "n", ts, "zone_invalid")
                continue
            if eligible is None:
                continue
            yb, nb, features = eligible
            allocation = self._allocate(policy, state, yb, nb, features)
            if allocation is not None:
                self._admit(policy, state, ts, features, allocation)

    def finalize_with_tracker(self):
        super().finalize()
        self.tracker.finish(self.last_ts or 0)


def run_day(args):
    date, trajectory_path = args
    meta = B.load_meta()
    l2_root = B.L2_GLOB.split("/**", 1)[0]
    tr_root = B.TR_GLOB.split("/**", 1)[0]
    l2_paths = list(
        Path(l2_root).glob(f"subcategory=*/date={date}/*.parquet")
    )
    tr_paths = list(
        Path(tr_root).glob(f"subcategory=*/date={date}/*.csv.gz")
    )
    l2_paths = [str(path) for path in l2_paths]
    tr_paths = [str(path) for path in tr_paths]
    if not l2_paths or not tr_paths:
        raise RuntimeError(f"missing date-scoped data for {date}")
    if any("2026-07-23" in path for path in l2_paths + tr_paths):
        raise RuntimeError("forbidden 2026-07-23 path")

    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='8GB'")
    trades_by_market = defaultdict(list)
    rows = connection.execute(
        """
        SELECT market_ticker, ts_utc, yes_price_e4, count_e4, taker_side
        FROM read_csv(?, header=true, union_by_name=true,
                      types={'taker_side':'VARCHAR'})
        WHERE series_ticker='KXBTC15M'
        ORDER BY market_ticker, ts_utc
        """,
        [tr_paths],
    ).fetchall()
    for ticker, ts, price, qty, taker in rows:
        if price is not None:
            trades_by_market[ticker].append(
                (int(ts), int(price), int(qty or 0), taker)
            )
    cursor = connection.execute(
        """
        SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR)
        FROM read_parquet(?, union_by_name=true)
        ORDER BY market_ticker, ts_utc, ws_seq
        """,
        [l2_paths],
    )
    writer = JsonlGzipWriter(trajectory_path, date)
    aggregate = {
        "cycles": [],
        "admissions": [],
        "bugs": [],
        "episodes": [],
        "markouts": [],
        "placed": 0,
        "fills": 0,
        "paired": 0,
        "flattened": 0,
        "carried": 0,
    }
    simulation = None

    def harvest(sim):
        if sim is None:
            return
        sim.finalize_with_tracker()
        state = sim.states[P3.name]
        aggregate["cycles"].extend(state.cycles)
        aggregate["admissions"].extend(state.admissions)
        aggregate["bugs"].extend(state.bugs)
        aggregate["episodes"].extend(sim.tracker.results)
        aggregate["markouts"].extend(sim.tracker.markouts)
        for key in ("placed", "fills", "paired", "flattened", "carried"):
            aggregate[key] += getattr(state, key)

    while True:
        batch = cursor.fetchmany(300_000)
        if not batch:
            break
        for ticker, ts, mtype, side, px, delta, yes_json, no_json in batch:
            ts = int(ts)
            if simulation is None or simulation.ticker != ticker:
                harvest(simulation)
                close_us, result = meta.get(ticker, (0, None))
                simulation = TrackedMarket(
                    ticker,
                    close_us,
                    result,
                    trades_by_market.get(ticker, []),
                    [P3],
                    writer,
                )
            yes_levels = no_levels = None
            if mtype == "snapshot":
                try:
                    yes_levels = json.loads(yes_json) if yes_json else []
                    no_levels = json.loads(no_json) if no_json else []
                except Exception:
                    yes_levels, no_levels = [], []
            simulation.on_book(
                ts,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
            )
    harvest(simulation)
    writer.close()
    connection.close()
    return date, trajectory_path, writer.rows, aggregate


def quantile(values, q):
    values = sorted(float(value) for value in values)
    position = (len(values) - 1) * q
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def cluster_ci(rows, key, label, reps=5000):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["market"]].append(float(row[key]))
    markets = sorted(grouped)
    seed = int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    boot = []
    for _ in range(reps):
        values = []
        for _j in markets:
            values.extend(grouped[rng.choice(markets)])
        boot.append(statistics.fmean(values))
    return [round(quantile(boot, 0.025), 4), round(quantile(boot, 0.975), 4)]


def policy_metrics(policy, rows, current_by_episode):
    improvements = []
    for row in rows:
        improvements.append(
            {
                "market": row["market"],
                "improvement_c": (
                    row["pnl_c"]
                    - current_by_episode[row["episode_id"]]["pnl_c"]
                ),
            }
        )
    capital = [row["capital_time_s"] for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "ev_c_per_first_fill": round(
            statistics.fmean(row["pnl_c"] for row in rows), 4
        ),
        "ci95_market_cluster": cluster_ci(rows, "pnl_c", f"ev:{policy}"),
        "improvement_vs_current_c": round(
            statistics.fmean(row["improvement_c"] for row in improvements), 4
        ),
        "improvement_ci95_market_cluster": cluster_ci(
            improvements,
            "improvement_c",
            f"delta:{policy}",
        ),
        "mean_capital_time_s": round(statistics.fmean(capital), 4),
        "median_capital_time_s": round(statistics.median(capital), 4),
        "maker_pairs": sum(row["exit_kind"] == "maker_pair" for row in rows),
        "ioc_exits": sum(row["exit_kind"] == "ioc" for row in rows),
        "settlement_exits": sum(
            row["exit_kind"] == "settlement" for row in rows
        ),
    }


def distribution_summary(rows, key, label):
    values = [float(row[key]) for row in rows]
    return {
        "mean": round(statistics.fmean(values), 4),
        "ci95_mean_market_cluster": cluster_ci(rows, key, label),
        "q10": round(quantile(values, 0.10), 4),
        "q25": round(quantile(values, 0.25), 4),
        "q50": round(quantile(values, 0.50), 4),
        "q75": round(quantile(values, 0.75), 4),
        "q90": round(quantile(values, 0.90), 4),
    }


def markout_metrics(markouts):
    first_rows = [
        {
            "market": row["market"],
            "ioc_value_c": row["first_fill"]["pnl_c"],
        }
        for row in markouts
    ]
    horizons = {}
    future_book_violations = []
    for horizon_s in MARKOUT_HORIZONS_S:
        key = EpisodeTracker._horizon_key(horizon_s)
        rows = []
        observation_lag = []
        book_staleness = []
        for episode in markouts:
            snap = episode["horizons"][key]
            first = episode["first_fill"]
            if (
                snap["book_asof_ts"] is not None
                and snap["book_asof_ts"] > snap["scheduled_ts"]
            ):
                future_book_violations.append(
                    {
                        "episode_id": episode["episode_id"],
                        "horizon": key,
                        "book_asof_ts": snap["book_asof_ts"],
                        "scheduled_ts": snap["scheduled_ts"],
                    }
                )
            rows.append(
                {
                    "market": episode["market"],
                    "ioc_value_c": snap["pnl_c"],
                    "relative_to_first_fill_c": (
                        snap["pnl_c"] - first["pnl_c"]
                    ),
                }
            )
            observation_lag.append(
                (snap["observed_event_ts"] - snap["scheduled_ts"]) / 1e6
            )
            if snap["book_asof_ts"] is not None:
                book_staleness.append(
                    (snap["scheduled_ts"] - snap["book_asof_ts"]) / 1e6
                )
        horizons[key] = {
            "n": len(rows),
            "markets": len({row["market"] for row in rows}),
            "fee_inclusive_immediate_ioc_value_c": distribution_summary(
                rows, "ioc_value_c", f"markout-ioc:{key}"
            ),
            "relative_to_first_fill_markout_c": distribution_summary(
                rows,
                "relative_to_first_fill_c",
                f"markout-relative:{key}",
            ),
            "sampling_audit": {
                "mean_observation_lag_s": round(
                    statistics.fmean(observation_lag), 6
                ),
                "max_observation_lag_s": round(max(observation_lag), 6),
                "mean_book_staleness_s": round(
                    statistics.fmean(book_staleness), 6
                ),
                "max_book_staleness_s": round(max(book_staleness), 6),
            },
        }
    if future_book_violations:
        raise RuntimeError(
            f"future book markout violations: {future_book_violations[:10]}"
        )
    return {
        "definition": (
            "IOC value uses the latest book already applied before the first "
            "event at/after each horizon; relative markout subtracts the "
            "fee-inclusive IOC value at the first fill"
        ),
        "first_fill_fee_inclusive_immediate_ioc_value_c": {
            "n": len(first_rows),
            "markets": len({row["market"] for row in first_rows}),
            **distribution_summary(
                first_rows, "ioc_value_c", "markout-ioc:first-fill"
            ),
        },
        "horizons": horizons,
        "future_book_violations": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", default=PREREG_PATH)
    parser.add_argument("--report", default=REPORT_OUT)
    parser.add_argument("--episodes", default=EPISODES_OUT)
    parser.add_argument("--trajectory-prefix", default=TRAJECTORY_PREFIX)
    args = parser.parse_args()

    script_sha = file_sha(__file__)
    prereg_bytes = Path(args.prereg).read_bytes()
    prereg_sha = hashlib.sha256(prereg_bytes).hexdigest()
    prereg = json.loads(prereg_bytes)
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("pre-registration script hash mismatch")
    if prereg.get("spec") != PREREGISTRATION:
        raise RuntimeError("pre-registration spec mismatch")
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print("RUN DISCOVERY 2026-07-20..22 ONLY", flush=True)

    tasks = [
        (
            date,
            f"{args.trajectory_prefix}_{date}.jsonl.gz",
        )
        for date in TRAIN_DATES
    ]
    with mp.Pool(processes=3) as pool:
        day_results = pool.map(run_day, tasks)

    merged = {
        "cycles": [],
        "admissions": [],
        "bugs": [],
        "episodes": [],
        "markouts": [],
        "placed": 0,
        "fills": 0,
        "paired": 0,
        "flattened": 0,
        "carried": 0,
    }
    trajectory_files = []
    for date, path, rows, aggregate in day_results:
        trajectory_files.append(
            {
                "date": date,
                "path": path,
                "rows": rows,
                "bytes": Path(path).stat().st_size,
                "sha256": file_sha(path),
            }
        )
        for key in ("cycles", "admissions", "bugs", "episodes", "markouts"):
            merged[key].extend(aggregate[key])
        for key in ("placed", "fills", "paired", "flattened", "carried"):
            merged[key] += aggregate[key]

    if merged["bugs"]:
        raise RuntimeError(f"base invariant bugs: {merged['bugs'][:10]}")
    cycles = merged["cycles"]
    baseline_mean = statistics.fmean(row["pnl_c"] for row in cycles)
    baseline_gate = {
        "cycles": len(cycles),
        "pairs": sum(row["paired"] for row in cycles),
        "markets": len({row["market"] for row in cycles}),
        "mean_c": round(baseline_mean, 4),
    }
    expected = {"cycles": 878, "pairs": 433, "markets": 44, "mean_c": -1.1702}
    if baseline_gate != expected:
        raise RuntimeError(
            f"sealed P3 baseline mismatch got={baseline_gate} expected={expected}"
        )

    shadow_by_policy = defaultdict(list)
    for row in merged["episodes"]:
        shadow_by_policy[row["policy"]].append(row)
    expected_policies = set(FIXED_WAIT_REQUEST_S) | {
        CURRENT_POLICY,
        ORACLE_POLICY,
    }
    if set(shadow_by_policy) != expected_policies:
        raise RuntimeError("missing/unexpected stopping policy results")
    if any(
        len(shadow_by_policy[name]) != len(cycles)
        for name in expected_policies
    ):
        raise RuntimeError("policy episode counts differ from base cycles")

    template_by_episode = {
        row["episode_id"]: row for row in shadow_by_policy["IOC_60MS"]
    }
    cycle_by_episode = {}
    for cycle in cycles:
        episode_id = (
            f"{cycle['market']}|{int(cycle['admit_ts'])}|"
            f"{int(cycle['first_ts'])}"
        )
        if episode_id in cycle_by_episode:
            raise RuntimeError(f"duplicate sealed cycle id {episode_id}")
        cycle_by_episode[episode_id] = cycle
    if set(cycle_by_episode) != set(template_by_episode):
        raise RuntimeError("sealed cycles and first-fill episodes do not align")

    actual_current_rows = []
    for episode_id, cycle in cycle_by_episode.items():
        template = template_by_episode[episode_id]
        if cycle["paired"]:
            exit_kind = "maker_pair"
        elif cycle["exit_reason"] == "carried_settlement":
            exit_kind = "settlement"
        else:
            exit_kind = "ioc"
        actual_current_rows.append(
            {
                **{
                    key: template[key]
                    for key in (
                        "episode_id",
                        "market",
                        "admit_ts",
                        "first_ts",
                        "held_side",
                        "entry_e4",
                        "counterpart_lvl",
                        "pair_gain_c",
                    )
                },
                "policy": CURRENT_POLICY,
                "pnl_c": float(cycle["pnl_c"]),
                "capital_time_s": float(cycle["capital_time_s"]),
                "exit_kind": exit_kind,
                "exit_ts": int(cycle["exit_ts"]),
                "current_trigger_reason": cycle["exit_reason"],
                "base_trigger_ts": cycle.get("trigger_ts"),
                "base_trigger_est_pnl_c": cycle.get("trigger_est_pnl_c"),
                "source": "sealed_P3_cycle",
            }
        )
    current_by_episode = {
        row["episode_id"]: row for row in actual_current_rows
    }

    shadow_current_by_episode = {
        row["episode_id"]: row
        for row in shadow_by_policy[CURRENT_POLICY]
    }
    shadow_deltas = []
    mismatch_examples = []
    for episode_id, actual in current_by_episode.items():
        shadow = shadow_current_by_episode[episode_id]
        delta = float(shadow["pnl_c"]) - float(actual["pnl_c"])
        shadow_deltas.append(abs(delta))
        if abs(delta) > 1e-9 and len(mismatch_examples) < 10:
            mismatch_examples.append(
                {
                    "episode_id": episode_id,
                    "sealed_current_c": actual["pnl_c"],
                    "original_quote_shadow_current_c": shadow["pnl_c"],
                    "delta_c": delta,
                }
            )
    shadow_current_audit = {
        "n": len(shadow_deltas),
        "exact_pnl_matches": sum(delta <= 1e-9 for delta in shadow_deltas),
        "mismatches": sum(delta > 1e-9 for delta in shadow_deltas),
        "mean_absolute_delta_c": round(
            statistics.fmean(shadow_deltas), 6
        ),
        "max_absolute_delta_c": round(max(shadow_deltas), 6),
        "examples": mismatch_examples,
        "interpretation": (
            "Canonical current-policy rows come directly from sealed P3 "
            "cycles. This shadow audit holds the original counterpart quote "
            "fixed, so differences can arise after base cancel/replacement."
        ),
    }

    by_policy = {
        name: list(shadow_by_policy[name])
        for name in FIXED_WAIT_REQUEST_S
    }
    by_policy[CURRENT_POLICY] = actual_current_rows
    oracle_rows = []
    for episode_id in sorted(current_by_episode):
        candidates = [
            next(
                row
                for row in by_policy[name]
                if row["episode_id"] == episode_id
            )
            for name in [*FIXED_WAIT_REQUEST_S, CURRENT_POLICY]
        ]
        source = min(
            candidates,
            key=lambda row: (
                -row["pnl_c"],
                row["capital_time_s"],
                row["policy"],
            ),
        )
        oracle = dict(source)
        oracle["selected_source_policy"] = source["policy"]
        oracle["policy"] = ORACLE_POLICY
        oracle_rows.append(oracle)
    by_policy[ORACLE_POLICY] = oracle_rows

    if len(merged["markouts"]) != len(cycles):
        raise RuntimeError("markout episode count differs from sealed cycles")
    if {
        row["episode_id"] for row in merged["markouts"]
    } != set(cycle_by_episode):
        raise RuntimeError("markout and sealed cycle episode IDs differ")
    markout_table = markout_metrics(merged["markouts"])

    policy_order = [
        "IOC_60MS",
        "WAIT_0P25",
        "WAIT_0P5",
        "WAIT_1",
        "WAIT_2",
        "WAIT_5",
        "WAIT_10",
        "WAIT_30",
        "WAIT_60",
        CURRENT_POLICY,
        ORACLE_POLICY,
    ]
    metrics = {
        name: policy_metrics(name, by_policy[name], current_by_episode)
        for name in policy_order
    }
    oracle_ev = metrics[ORACLE_POLICY]["ev_c_per_first_fill"]
    if oracle_ev <= 0:
        mechanism = "MECHANISM_DEAD_EVEN_CLAIRVOYANT_NONPOSITIVE"
    else:
        mechanism = "POSITIVE_CLAIRVOYANT_TIMING_HEADROOM_ONLY"

    with gzip.open(args.episodes, "wt", encoding="utf-8", compresslevel=6) as fh:
        canonical_rows = [
            row for name in policy_order for row in by_policy[name]
        ]
        artifact = {
            "schema": "z3-postfill-stopping-episode-results-v2",
            "status": "DISCOVERY_ONLY",
            "date_2026_07_23_read": False,
            "policy_rows": canonical_rows,
            "markout_rows": merged["markouts"],
            "shadow_current_audit": shadow_current_audit,
        }
        json.dump(artifact, fh, separators=(",", ":"), allow_nan=False)
    episodes_file = {
        "path": args.episodes,
        "bytes": Path(args.episodes).stat().st_size,
        "sha256": file_sha(args.episodes),
    }

    report = {
        "schema": "z3-postfill-optimal-stopping-headroom-v1",
        "status": "DISCOVERY_HEADROOM_ONLY",
        "not_validation": True,
        "not_candidate_selection": True,
        "deployable": False,
        "dates": {
            "opened": list(TRAIN_DATES),
            "2026-07-23_read": False,
            "2026-07-20_through_23_status": "DISCOVERY_CONTAMINATED",
            "real_final_test": "post-model-and-code-seal forward holdout",
        },
        "preregistration": PREREGISTRATION,
        "source": {
            "script_path": str(Path(__file__).resolve()),
            "script_sha256": script_sha,
            "preregistration_path": args.prereg,
            "preregistration_sha256": prereg_sha,
            "sealed_base_path": BASE_SCRIPT,
            "sealed_base_sha256": BASE_SCRIPT_SHA256,
        },
        "baseline_reproduction_gate": {
            "pass": True,
            "got": baseline_gate,
            "expected": expected,
            "canonical_current_source": "sealed_P3_cycles",
            "original_counterpart_shadow_current_audit": shadow_current_audit,
        },
        "conditional_episode_scope": {
            "first_fill_episodes": len(cycles),
            "markets": len({row["market"] for row in cycles}),
            "warning": (
                "shadow episodes are evaluated independently and can overlap; "
                "EV is per P3 first fill and does not include policy-dependent "
                "re-entry or portfolio capital contention"
            ),
        },
        "policy_order": policy_order,
        "metrics": metrics,
        "post_first_fill_ioc_markouts": markout_table,
        "trajectory_files": trajectory_files,
        "episode_results_file": episodes_file,
        "mechanism_assessment": {
            "classification": mechanism,
            "oracle_ev_c_per_first_fill": oracle_ev,
            "oracle_ci95_market_cluster": metrics[ORACLE_POLICY][
                "ci95_market_cluster"
            ],
            "headroom_vs_current_c": metrics[ORACLE_POLICY][
                "improvement_vs_current_c"
            ],
            "interpretation": (
                "Oracle is a clairvoyant upper bound only. Positive oracle EV "
                "means timing headroom exists; it is not a causal policy, "
                "candidate, validation result, or deployment authorization."
                if oracle_ev > 0
                else
                "Even the clairvoyant fixed-policy upper bound is nonpositive; "
                "the post-fill mechanism is dead under this opportunity set."
            ),
        },
        "run_completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    Path(args.report).write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print(json.dumps(metrics, indent=2), flush=True)
    print(json.dumps(report["mechanism_assessment"], indent=2), flush=True)
    print(f"REPORT {args.report}", flush=True)
    print(f"REPORT_SHA {file_sha(args.report)}", flush=True)
    print(f"EPISODES {args.episodes}", flush=True)
    print(f"EPISODES_SHA {episodes_file['sha256']}", flush=True)
    print("2026_07_23_READ false", flush=True)


if __name__ == "__main__":
    main()
