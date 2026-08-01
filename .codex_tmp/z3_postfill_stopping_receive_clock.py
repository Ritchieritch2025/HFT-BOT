#!/usr/bin/env python3
"""ROUND3 post-fill diagnostic on one causal receive clock.

This is a data-reconstruction-only revision of the frozen post-fill policy
experiment.  Policy definitions, queue threshold, fees, waits, oracle, and
markout horizons are imported unchanged.  Books and trades are instead merged
on their recorder receive envelope.  Any missing/mismatched clock, invalid L2
receipt, sequence regression, delta before snapshot, or negative reconstructed
level fails the run; quantities are never silently clamped.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import duckdb


LEGACY_DIAGNOSTIC = os.environ.get(
    "Z3_POSTFILL_LEGACY_DIAGNOSTIC",
    "/tmp/z3_postfill_stopping_diagnostic.py",
)
LEGACY_DIAGNOSTIC_SHA256 = (
    "efa402bfa2a45cb6624bae483a4de59b1c176400168ef87c7ab247d737c9e381"
)
CAUSAL_CONTRACT = os.environ.get(
    "ROUND3_CAUSAL_CONTRACT",
    "/tmp/causal_replay_contract.py",
)
CAUSAL_CONTRACT_SHA256 = (
    "4b7a3379a69aba3ea95361294db4ffea775791f56942f5c10818b14b18c04afd"
)
REPORT_OUT = os.environ.get(
    "Z3_POSTFILL_R3_REPORT",
    "/tmp/z3_postfill_stopping_receive_clock_report.json",
)
EPISODES_OUT = os.environ.get(
    "Z3_POSTFILL_R3_EPISODES",
    "/tmp/z3_postfill_stopping_receive_clock_episodes.json.gz",
)
TRAJECTORY_PREFIX = os.environ.get(
    "Z3_POSTFILL_R3_TRAJECTORY_PREFIX",
    "/tmp/z3_postfill_receive_clock_trajectory",
)
PREREG_PATH = os.environ.get(
    "Z3_POSTFILL_R3_PREREG",
    "/tmp/z3_postfill_stopping_receive_clock_prereg.json",
)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_legacy():
    if file_sha(LEGACY_DIAGNOSTIC) != LEGACY_DIAGNOSTIC_SHA256:
        raise RuntimeError("frozen post-fill diagnostic SHA mismatch")
    spec = importlib.util.spec_from_file_location(
        "frozen_postfill_diagnostic", LEGACY_DIAGNOSTIC
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


D = load_legacy()
B = D.B
P3 = D.P3
TRAIN_DATES = D.TRAIN_DATES


def load_contract():
    if file_sha(CAUSAL_CONTRACT) != CAUSAL_CONTRACT_SHA256:
        raise RuntimeError("shared ROUND3 causal contract SHA mismatch")
    spec = importlib.util.spec_from_file_location(
        "round3_causal_replay_contract", CAUSAL_CONTRACT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


C = load_contract()
if tuple(TRAIN_DATES) != tuple(C.ALLOWED_DATES):
    raise RuntimeError("policy dates differ from causal contract allowlist")

PREREGISTRATION = copy.deepcopy(D.PREREGISTRATION)
PREREGISTRATION.update(
    {
        "schema": "z3-postfill-optimal-stopping-prereg-v2-receive-clock",
        "status": "DISCOVERY_HEADROOM_ONLY_CAUSAL_RECONSTRUCTION",
        "data_reconstruction_revision": {
            "only_change_from_v1": (
                "replace legacy (ts_utc,ws_seq) book replay and mixed trade "
                "clock with one recorder receive-clock merge; policies and "
                "all economic definitions are unchanged"
            ),
            "required_clock_fields": [
                "recv_wall_ns",
                "recv_mono_ns",
                "local_recv_ts_us",
            ],
            "clock_identity": "local_recv_ts_us == recv_wall_ns//1000",
            "decision_flow_tte_clock": "local_recv_ts_us only",
            "forbidden_clocks": [
                "ts_utc",
                "exchange_ts_us",
            ],
            "combined_market_key": (
                "(recv_wall_ns,recv_mono_ns,channel_priority,"
                "ws_seq_or_sentinel,stable_id)"
            ),
            "same_envelope_priority": "BOOK=0 before TRADE=1",
            "book_tie": "ws_seq then deterministic row stable_id",
            "trade_tie": "trade_id",
            "snapshot": "re-anchor full book and (ws_sid,ws_seq)",
            "delta": (
                "requires prior snapshot, unchanged ws_sid, and strictly "
                "increasing ws_seq; +1 is not required"
            ),
            "signed_level_rule": (
                "next=prior+delta; next<0 fails the run; next=0 deletes; "
                "never clamp"
            ),
            "A1_gate": "allocated two-sided ETA must both be finite and >0",
        },
        "raw_l2_receipt_gate": {
            "schema": "l2-gap-receipt-v1",
            "shared_contract_sha256": CAUSAL_CONTRACT_SHA256,
            "tool_sha256": C.GAP_TOOL_SHA256,
            "ingest_sha256": C.INGEST_SHA256,
            "receipt_sha256": C.GAP_RECEIPT_SHA256,
            "required_zero": [
                "sids_with_seq_gaps",
                "seq_gap_events",
                "seq_missed_total",
                "seq_regressions",
                "stream_restarts",
                "markers_lost_frames",
                "parse_errors",
            ],
            "no_l2_files": False,
            "allowed_recorder_marker_keys": ["transport_close"],
            "snapshot_re_anchors": "audited but allowed nonzero",
        },
        "event_clock_and_no_lookahead": (
            "merge book and trade facts by the shared causal receive key; "
            "BOOK precedes TRADE only when recv_wall_ns and recv_mono_ns are "
            "identical. Every timed IOC/markout snapshot is taken before the "
            "first causal event at/after its scheduled local-receive time from "
            "the latest already-applied book, with enforced "
            "book_asof_ts<=scheduled_ts"
        ),
        "baseline_gate": (
            "legacy 878/433/44/-1.1702c is contamination evidence only and "
            "is not an expected result after causal reconstruction; require "
            "all clock/L2/level/A1 gates pass, unique aligned first-fill "
            "episodes, and report the new causal counts without tuning"
        ),
        "predecessor_attempt": {
            "script_sha256": LEGACY_DIAGNOSTIC_SHA256,
            "status": "terminated_after_contamination_confirmed",
            "results_eligible": False,
        },
    }
)


causal_key = C.causal_key
assert_receive_clock = C.assert_receive_clock
load_gap_receipt = C.load_gap_receipt
TRADE_SEQ_SENTINEL = C.TRADE_SEQ_SENTINEL


class CausalTrackedMarket(D.TrackedMarket):
    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, [], policies, writer)
        self.reconstructor = C.BookReconstructor(ticker)
        self.audit = {
            "trade_events": 0,
            "negative_level_failures": 0,
            "clock_null_failures": 0,
            "clock_identity_failures": 0,
            "sequence_failures": 0,
            "A1_failures": 0,
        }

    def _admit(self, policy, state, ts, features, allocation):
        eta_y = allocation.get("pred_eta_y_s")
        eta_n = allocation.get("pred_eta_n_s")
        if not (
            eta_y is not None
            and eta_n is not None
            and math.isfinite(float(eta_y))
            and math.isfinite(float(eta_n))
            and float(eta_y) > 0
            and float(eta_n) > 0
        ):
            self.audit["A1_failures"] += 1
            raise RuntimeError(
                f"{self.ticker}/{ts}: A1 finite-positive ETA gate failed"
            )
        super()._admit(policy, state, ts, features, allocation)

    def on_trade_causal(self, ts, yes_px, qty, taker, event_fields):
        if not self.close:
            return
        self.last_ts = int(ts)
        self.audit["trade_events"] += 1
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
                    if quote.ahead < -D.CLIP_E4:
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
        self.tracker.observe(ts, "trade", event_fields)

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
        if not self.close:
            return
        if ws_sid is None or ws_seq is None:
            self.audit["sequence_failures"] += 1
            raise RuntimeError(f"{self.ticker}/{ts}: missing sid/seq")
        if self.first_event_ts is None:
            self.first_event_ts = int(ts)
        self.last_ts = int(ts)
        for policy in self.policies:
            self._advance(policy, self.states[policy.name], ts)
        self.tracker.before_event(ts)
        recv_wall_ns = event_fields["recv_wall_ns"]
        recv_mono_ns = event_fields["recv_mono_ns"]
        local_recv_ts_us = event_fields["local_recv_ts_us"]
        stable_id = event_fields["book_stable_id"]
        if mtype == "snapshot":
            self.reconstructor.snapshot(
                yes_levels,
                no_levels,
                ws_sid,
                ws_seq,
                recv_wall_ns,
                recv_mono_ns,
                local_recv_ts_us,
                stable_id,
            )
        elif mtype == "delta":
            self.reconstructor.delta(
                side,
                px,
                delta,
                ws_sid,
                ws_seq,
                recv_wall_ns,
                recv_mono_ns,
                local_recv_ts_us,
                stable_id,
            )
        else:
            self.audit["sequence_failures"] += 1
            raise RuntimeError(
                f"{self.ticker}/{ts}: unexpected book msg_type={mtype!r}"
            )
        self.books = {
            "y": self.reconstructor.books["yes"],
            "n": self.reconstructor.books["no"],
        }
        self.book_asof_ts = int(ts)
        self.flow_cache = {}
        self.tracker.after_book(ts, event_fields)
        eligible = self._z3_now(ts)
        for policy in self.policies:
            state = self.states[policy.name]
            self._advance(policy, state, ts)
            if state.orphan_side is not None:
                continue
            has_quote = (
                state.quotes["y"] is not None
                or state.quotes["n"] is not None
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


def run_day(args):
    date, trajectory_path = args
    gap_receipt = load_gap_receipt(date)
    meta = B.load_meta()
    l2_root = B.L2_GLOB.split("/**", 1)[0]
    tr_root = B.TR_GLOB.split("/**", 1)[0]
    l2_paths = [
        str(path)
        for path in Path(l2_root).glob(
            f"subcategory=*/date={date}/*.parquet"
        )
    ]
    tr_paths = [
        str(path)
        for path in Path(tr_root).glob(
            f"subcategory=*/date={date}/*.csv.gz"
        )
    ]
    if not l2_paths or not tr_paths:
        raise RuntimeError(f"{date}: missing date-scoped input")
    for path in l2_paths + tr_paths:
        C.assert_allowed_date_path(path, date)

    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='8GB'")
    trade_rows = connection.execute(
        """
        SELECT market_ticker, recv_wall_ns, recv_mono_ns,
               local_recv_ts_us, trade_id, yes_price_e4, count_e4,
               taker_side
        FROM read_csv(?, header=true, union_by_name=true,
                      types={'taker_side':'VARCHAR'})
        WHERE series_ticker='KXBTC15M'
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, trade_id
        """,
        [tr_paths],
    ).fetchall()
    trades_by_market = defaultdict(list)
    trade_clock_rows_checked = 0
    for (
        ticker,
        recv_wall_ns,
        recv_mono_ns,
        local_recv_ts_us,
        trade_id,
        yes_px,
        qty,
        taker,
    ) in trade_rows:
        event_us = assert_receive_clock(
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
            f"{date}/{ticker}/trade/{trade_id}",
        )
        if (
            not ticker
            or trade_id is None
            or yes_px is None
            or qty is None
            or int(qty) <= 0
            or taker not in ("yes", "no")
        ):
            raise RuntimeError(f"{date}/{ticker}: malformed trade fact")
        key = C.trade_key(recv_wall_ns, recv_mono_ns, trade_id)
        rows = trades_by_market[ticker]
        if rows and key <= rows[-1]["key"]:
            raise RuntimeError(f"{date}/{ticker}: trade key not strict")
        rows.append(
            {
                "key": key,
                "event_us": event_us,
                "recv_wall_ns": int(recv_wall_ns),
                "recv_mono_ns": int(recv_mono_ns),
                "trade_id": str(trade_id),
                "yes_px": int(yes_px),
                "qty": int(qty),
                "taker": taker,
            }
        )
        trade_clock_rows_checked += 1

    cursor = connection.execute(
        """
        SELECT market_ticker, recv_wall_ns, recv_mono_ns,
               local_recv_ts_us, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR),
               ws_sid, ws_seq,
               concat_ws('|',
                   coalesce(CAST(ws_sid AS VARCHAR), '<NULL>'),
                   coalesce(CAST(ws_seq AS VARCHAR), '<NULL>'),
                   coalesce(msg_type, '<NULL>'),
                   coalesce(side, '<NULL>'),
                   coalesce(CAST(price_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(delta_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(yes_levels AS VARCHAR), '<NULL>'),
                   coalesce(CAST(no_levels AS VARCHAR), '<NULL>')
               ) AS book_stable_id
        FROM read_parquet(?, union_by_name=true)
        WHERE series_ticker='KXBTC15M'
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, ws_seq,
                 book_stable_id
        """,
        [l2_paths],
    )
    writer = D.JsonlGzipWriter(trajectory_path, date)
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
        "data_audit": {
            "date": date,
            "gap_receipt": gap_receipt,
            "trade_clock_rows_checked": trade_clock_rows_checked,
            "book_clock_rows_checked": 0,
            "combined_events_processed": 0,
            "trade_only_markets": [],
            "market_audit": {},
        },
    }
    simulation = None
    current_ticker = None
    trade_index = 0
    last_combined_key = None
    book_markets = set()

    def process_trade(sim, trade):
        nonlocal last_combined_key
        if last_combined_key is not None and trade["key"] <= last_combined_key:
            raise RuntimeError(
                f"{date}/{sim.ticker}: combined event key not strict"
            )
        last_combined_key = trade["key"]
        sim.on_trade_causal(
            trade["event_us"],
            trade["yes_px"],
            trade["qty"],
            trade["taker"],
            {
                "trade_id": trade["trade_id"],
                "yes_price_e4": trade["yes_px"],
                "count_e4": trade["qty"],
                "taker_side": trade["taker"],
                "recv_wall_ns": trade["recv_wall_ns"],
                "recv_mono_ns": trade["recv_mono_ns"],
                "local_recv_ts_us": trade["event_us"],
                "causal_channel_priority": 1,
            },
        )
        aggregate["data_audit"]["combined_events_processed"] += 1

    def drain_trades_before(sim, upper_key=None):
        nonlocal trade_index
        trades = trades_by_market.get(sim.ticker, [])
        while trade_index < len(trades):
            trade = trades[trade_index]
            if upper_key is not None and not (trade["key"] < upper_key):
                break
            process_trade(sim, trade)
            trade_index += 1

    def harvest(sim):
        if sim is None:
            return
        drain_trades_before(sim)
        sim.finalize_with_tracker()
        state = sim.states[P3.name]
        aggregate["cycles"].extend(state.cycles)
        aggregate["admissions"].extend(state.admissions)
        aggregate["bugs"].extend(state.bugs)
        aggregate["episodes"].extend(sim.tracker.results)
        aggregate["markouts"].extend(sim.tracker.markouts)
        aggregate["data_audit"]["market_audit"][sim.ticker] = {
            **sim.reconstructor.audit,
            **sim.audit,
        }
        for key in ("placed", "fills", "paired", "flattened", "carried"):
            aggregate[key] += getattr(state, key)

    while True:
        batch = cursor.fetchmany(300_000)
        if not batch:
            break
        for (
            ticker,
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
            mtype,
            side,
            px,
            delta,
            yes_json,
            no_json,
            ws_sid,
            ws_seq,
            book_stable_id,
        ) in batch:
            event_us = assert_receive_clock(
                recv_wall_ns,
                recv_mono_ns,
                local_recv_ts_us,
                f"{date}/{ticker}/book/{ws_sid}/{ws_seq}",
            )
            aggregate["data_audit"]["book_clock_rows_checked"] += 1
            if ws_sid is None or ws_seq is None:
                raise RuntimeError(
                    f"{date}/{ticker}: missing book ws_sid/ws_seq"
                )
            if ticker != current_ticker:
                harvest(simulation)
                current_ticker = ticker
                trade_index = 0
                last_combined_key = None
                if ticker not in meta:
                    raise RuntimeError(
                        f"{date}/{ticker}: missing resolved market metadata"
                    )
                close_us, result = meta[ticker]
                simulation = CausalTrackedMarket(
                    ticker, close_us, result, [P3], writer
                )
            book_markets.add(ticker)
            book_key = C.book_key(
                recv_wall_ns,
                recv_mono_ns,
                ws_seq if ws_seq is not None else -1,
                book_stable_id,
            )
            drain_trades_before(simulation, book_key)
            if (
                last_combined_key is not None
                and book_key <= last_combined_key
            ):
                raise RuntimeError(
                    f"{date}/{ticker}: combined book key not strict"
                )
            last_combined_key = book_key
            yes_levels = no_levels = None
            if mtype == "snapshot":
                try:
                    yes_levels = json.loads(yes_json) if yes_json else []
                    no_levels = json.loads(no_json) if no_json else []
                except Exception as exc:
                    raise RuntimeError(
                        f"{date}/{ticker}: snapshot JSON parse failed"
                    ) from exc
            simulation.on_book_causal(
                event_us,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
                ws_sid,
                ws_seq,
                {
                    "msg_type": mtype,
                    "side": side,
                    "price_e4": px,
                    "delta_e4": delta,
                    "ws_sid": ws_sid,
                    "ws_seq": ws_seq,
                    "recv_wall_ns": int(recv_wall_ns),
                    "recv_mono_ns": int(recv_mono_ns),
                    "local_recv_ts_us": event_us,
                    "causal_channel_priority": 0,
                    "book_stable_id": book_stable_id,
                },
            )
            aggregate["data_audit"]["combined_events_processed"] += 1
    harvest(simulation)
    aggregate["data_audit"]["trade_only_markets"] = sorted(
        set(trades_by_market) - book_markets
    )
    writer.close()
    connection.close()
    return date, trajectory_path, writer.rows, aggregate


def canonical_policy_rows(cycles, shadow_rows):
    shadow_by_policy = defaultdict(list)
    for row in shadow_rows:
        shadow_by_policy[row["policy"]].append(row)
    expected_policies = set(D.FIXED_WAIT_REQUEST_S) | {
        D.CURRENT_POLICY,
        D.ORACLE_POLICY,
    }
    if set(shadow_by_policy) != expected_policies:
        raise RuntimeError("missing/unexpected shadow policy rows")
    if any(
        len(shadow_by_policy[name]) != len(cycles)
        for name in expected_policies
    ):
        raise RuntimeError("shadow policy count differs from causal cycles")

    templates = {
        row["episode_id"]: row
        for row in shadow_by_policy["IOC_60MS"]
    }
    cycle_by_episode = {}
    for cycle in cycles:
        episode_id = (
            f"{cycle['market']}|{int(cycle['admit_ts'])}|"
            f"{int(cycle['first_ts'])}"
        )
        if episode_id in cycle_by_episode:
            raise RuntimeError(f"duplicate causal cycle {episode_id}")
        cycle_by_episode[episode_id] = cycle
    if set(cycle_by_episode) != set(templates):
        raise RuntimeError("causal cycles and shadow episodes do not align")

    actual_current = []
    for episode_id, cycle in cycle_by_episode.items():
        template = templates[episode_id]
        if cycle["paired"]:
            exit_kind = "maker_pair"
        elif cycle["exit_reason"] == "carried_settlement":
            exit_kind = "settlement"
        else:
            exit_kind = "ioc"
        actual_current.append(
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
                "policy": D.CURRENT_POLICY,
                "pnl_c": float(cycle["pnl_c"]),
                "capital_time_s": float(cycle["capital_time_s"]),
                "exit_kind": exit_kind,
                "exit_ts": int(cycle["exit_ts"]),
                "current_trigger_reason": cycle["exit_reason"],
                "base_trigger_ts": cycle.get("trigger_ts"),
                "base_trigger_est_pnl_c": cycle.get(
                    "trigger_est_pnl_c"
                ),
                "source": "causal_receive_clock_P3_cycle",
            }
        )
    current_by_episode = {
        row["episode_id"]: row for row in actual_current
    }

    shadow_current = {
        row["episode_id"]: row
        for row in shadow_by_policy[D.CURRENT_POLICY]
    }
    absolute_deltas = []
    mismatch_examples = []
    for episode_id, actual in current_by_episode.items():
        shadow = shadow_current[episode_id]
        delta = float(shadow["pnl_c"]) - float(actual["pnl_c"])
        absolute_deltas.append(abs(delta))
        if abs(delta) > 1e-9 and len(mismatch_examples) < 10:
            mismatch_examples.append(
                {
                    "episode_id": episode_id,
                    "causal_current_c": actual["pnl_c"],
                    "original_quote_shadow_current_c": shadow["pnl_c"],
                    "delta_c": delta,
                }
            )
    shadow_audit = {
        "n": len(absolute_deltas),
        "exact_pnl_matches": sum(
            value <= 1e-9 for value in absolute_deltas
        ),
        "mismatches": sum(value > 1e-9 for value in absolute_deltas),
        "mean_absolute_delta_c": round(
            statistics.fmean(absolute_deltas), 6
        ),
        "max_absolute_delta_c": round(max(absolute_deltas), 6),
        "examples": mismatch_examples,
        "interpretation": (
            "Canonical current rows are the causal P3 cycles. The independent "
            "shadow keeps the original counterpart quote, so later base "
            "cancel/replacement can differ."
        ),
    }

    by_policy = {
        name: list(shadow_by_policy[name])
        for name in D.FIXED_WAIT_REQUEST_S
    }
    by_policy[D.CURRENT_POLICY] = actual_current
    indexed = {
        name: {row["episode_id"]: row for row in rows}
        for name, rows in by_policy.items()
    }
    oracle = []
    for episode_id in sorted(current_by_episode):
        candidates = [
            indexed[name][episode_id]
            for name in [*D.FIXED_WAIT_REQUEST_S, D.CURRENT_POLICY]
        ]
        source = min(
            candidates,
            key=lambda row: (
                -row["pnl_c"],
                row["capital_time_s"],
                row["policy"],
            ),
        )
        row = dict(source)
        row["selected_source_policy"] = source["policy"]
        row["policy"] = D.ORACLE_POLICY
        oracle.append(row)
    by_policy[D.ORACLE_POLICY] = oracle
    return by_policy, current_by_episode, cycle_by_episode, shadow_audit


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
    contract_tool_evidence = C.verify_pinned_contract_tools()
    contract_synthetic = C.synthetic_contract_test()
    print(f"SCRIPT_SHA {script_sha}", flush=True)
    print(f"PREREG_SHA {prereg_sha}", flush=True)
    print("RUN CAUSAL RECEIVE CLOCK DISCOVERY 2026-07-20..22", flush=True)

    tasks = [
        (date, f"{args.trajectory_prefix}_{date}.jsonl.gz")
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
    data_audits = []
    trajectory_files = []
    for date, path, rows, aggregate in day_results:
        data_audits.append(aggregate["data_audit"])
        trajectory_files.append(
            {
                "date": date,
                "path": path,
                "rows": rows,
                "bytes": Path(path).stat().st_size,
                "sha256": file_sha(path),
            }
        )
        for key in (
            "cycles",
            "admissions",
            "bugs",
            "episodes",
            "markouts",
        ):
            merged[key].extend(aggregate[key])
        for key in ("placed", "fills", "paired", "flattened", "carried"):
            merged[key] += aggregate[key]
    if merged["bugs"]:
        raise RuntimeError(f"P3 invariant bugs: {merged['bugs'][:10]}")
    cycles = merged["cycles"]
    if not cycles:
        raise RuntimeError("causal replay produced no completed cycles")

    aggregate_quality = defaultdict(int)
    for day in data_audits:
        for market in day["market_audit"].values():
            for key, value in market.items():
                aggregate_quality[key] += int(value)
    failure_fields = (
        "negative_level_failures",
        "clock_null_failures",
        "clock_identity_failures",
        "sequence_failures",
        "A1_failures",
    )
    failures = {
        key: aggregate_quality[key]
        for key in failure_fields
        if aggregate_quality[key] != 0
    }
    if failures:
        raise RuntimeError(f"causal reconstruction gate failed {failures}")

    by_policy, current_by_episode, cycle_by_episode, shadow_audit = (
        canonical_policy_rows(cycles, merged["episodes"])
    )
    if len(merged["markouts"]) != len(cycles):
        raise RuntimeError("markout count differs from causal cycles")
    if {
        row["episode_id"] for row in merged["markouts"]
    } != set(cycle_by_episode):
        raise RuntimeError("markouts and causal cycles do not align")
    markouts = D.markout_metrics(merged["markouts"])

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
        D.CURRENT_POLICY,
        D.ORACLE_POLICY,
    ]
    metrics = {
        name: D.policy_metrics(
            name, by_policy[name], current_by_episode
        )
        for name in policy_order
    }
    oracle_ev = metrics[D.ORACLE_POLICY]["ev_c_per_first_fill"]
    mechanism = (
        "MECHANISM_DEAD_EVEN_CLAIRVOYANT_NONPOSITIVE"
        if oracle_ev <= 0
        else "POSITIVE_CLAIRVOYANT_TIMING_HEADROOM_ONLY"
    )
    baseline = {
        "cycles": len(cycles),
        "pairs": sum(row["paired"] for row in cycles),
        "markets": len({row["market"] for row in cycles}),
        "mean_c": round(
            statistics.fmean(row["pnl_c"] for row in cycles), 4
        ),
    }

    canonical_rows = [
        row for name in policy_order for row in by_policy[name]
    ]
    with gzip.open(
        args.episodes, "wt", encoding="utf-8", compresslevel=6
    ) as fh:
        json.dump(
            {
                "schema": (
                    "z3-postfill-stopping-receive-clock-episodes-v1"
                ),
                "status": "DISCOVERY_ONLY",
                "date_2026_07_23_read": False,
                "policy_rows": canonical_rows,
                "markout_rows": merged["markouts"],
                "shadow_current_audit": shadow_audit,
            },
            fh,
            separators=(",", ":"),
            allow_nan=False,
        )
    episodes_file = {
        "path": args.episodes,
        "bytes": Path(args.episodes).stat().st_size,
        "sha256": file_sha(args.episodes),
    }

    report = {
        "schema": "z3-postfill-optimal-stopping-receive-clock-v1",
        "status": "DISCOVERY_HEADROOM_ONLY_CAUSAL_RECONSTRUCTION",
        "not_validation": True,
        "not_candidate_selection": True,
        "deployable": False,
        "date_2026_07_23_read": False,
        "dates_opened": list(TRAIN_DATES),
        "source": {
            "script_path": str(Path(__file__).resolve()),
            "script_sha256": script_sha,
            "preregistration_path": args.prereg,
            "preregistration_sha256": prereg_sha,
            "frozen_policy_diagnostic_path": LEGACY_DIAGNOSTIC,
            "frozen_policy_diagnostic_sha256": (
                LEGACY_DIAGNOSTIC_SHA256
            ),
            "shared_causal_contract_path": CAUSAL_CONTRACT,
            "shared_causal_contract_sha256": CAUSAL_CONTRACT_SHA256,
            "sealed_base_path": D.BASE_SCRIPT,
            "sealed_base_sha256": D.BASE_SCRIPT_SHA256,
            "l2_gap_tool_sha256": C.GAP_TOOL_SHA256,
            "ingest_contract_sha256": C.INGEST_SHA256,
        },
        "preregistration": PREREGISTRATION,
        "causal_reconstruction_gate": {
            "pass": True,
            "clock": "local_recv_ts_us only",
            "combined_key": PREREGISTRATION[
                "data_reconstruction_revision"
            ]["combined_market_key"],
            "shared_contract_synthetic": contract_synthetic,
            "pinned_tool_evidence": contract_tool_evidence,
            "aggregate_market_audit": dict(aggregate_quality),
            "per_day": data_audits,
        },
        "causal_P3_baseline": baseline,
        "legacy_contaminated_baseline_for_comparison_only": {
            "cycles": 878,
            "pairs": 433,
            "markets": 44,
            "mean_c": -1.1702,
        },
        "predecessor_attempt": PREREGISTRATION["predecessor_attempt"],
        "conditional_episode_scope": {
            "first_fill_episodes": len(cycles),
            "markets": len({row["market"] for row in cycles}),
            "warning": (
                "independent post-fill episodes can overlap; per-first-fill "
                "EV excludes policy-dependent re-entry and capital contention"
            ),
        },
        "shadow_current_audit": shadow_audit,
        "policy_order": policy_order,
        "metrics": metrics,
        "post_first_fill_ioc_markouts": markouts,
        "trajectory_files": trajectory_files,
        "episode_results_file": episodes_file,
        "mechanism_assessment": {
            "classification": mechanism,
            "oracle_ev_c_per_first_fill": oracle_ev,
            "oracle_ci95_market_cluster": metrics[D.ORACLE_POLICY][
                "ci95_market_cluster"
            ],
            "headroom_vs_current_c": metrics[D.ORACLE_POLICY][
                "improvement_vs_current_c"
            ],
            "interpretation": (
                "Positive oracle means timing headroom only; it is not a "
                "causal policy, candidate, validation, or deployment approval."
                if oracle_ev > 0
                else
                "The clairvoyant fixed-policy upper bound is nonpositive, so "
                "this post-fill mechanism is dead on the discovery set."
            ),
        },
        "run_completed_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
    }
    Path(args.report).write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print(json.dumps(metrics, indent=2), flush=True)
    print(json.dumps(report["mechanism_assessment"], indent=2), flush=True)
    print(f"REPORT {args.report}", flush=True)
    print(f"REPORT_SHA {file_sha(args.report)}", flush=True)
    print(f"EPISODES_SHA {episodes_file['sha256']}", flush=True)
    print("2026_07_23_READ false", flush=True)


if __name__ == "__main__":
    main()
