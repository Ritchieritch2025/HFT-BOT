#!/usr/bin/env python3
"""Snapshot-aware L2 tests for SPORTS-AUTORESEARCH-01 Cycle 1.

This stage is deliberately narrow.  It consumes only exact manifest-bound,
locally VERIFIED W05 releases, replays the sealed Sports L2 facts on the TL1
receive clock, and tests the two preregistered L2 cards.  It never interprets
per-market projections of ``ws_seq`` as packet-loss evidence: the sealed
``quality/l2_gaps.json`` receipt, computed on complete subscription streams,
is the sole sequence/capture-quality authority.

The current frozen split has one feature-training day (2026-07-12) and one
evaluation day (2026-07-13).  Consequently every otherwise executable result
is COLLECT_MORE and every calendar-day bootstrap is explicitly degenerate.
No result from this module can be a verdict, promotion, or live authorization.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import sys
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

import run_cycle1 as core
from stats import block_bootstrap_mean, percentile, summary


SCHEMA_VERSION = "sports-autoresearch-l2-hypothesis-stage-v1"
BANNER = "EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION"
EVIDENCE_TIER = "SEALED_DEGRADED_EVIDENCE"
TIMESTAMP_TIER = "TL1"
TRAIN_DATE = "2026-07-12"
EVAL_DATE = "2026-07-13"
SEED = 20260715
MIN_BOOTSTRAP_REPLICATES = 1000
MIN_RELEVANT_GAMES = 200
MIN_DAY_BLOCKS = 20
BOOK_AGE_CAP_NS = 5_000_000_000
COORDINATION_WINDOW_NS = 100_000_000
HFOLLOW_HORIZONS_US = (100_000, 1_000_000, 3_000_000, 10_000_000)
REFILL_WINDOWS_US = (100_000, 1_000_000)
REFILL_HORIZONS_US = (100_000, 1_000_000, 5_000_000, 30_000_000)
MIN_TOUCH_QTY_E4 = 10_000
MIN_DEPLETION_FRACTION = 0.50
REFILL_FRACTION = 0.80
CONTROL_SAMPLE_MODULUS = 16
MAX_SPARSE_EVENTS_PER_CLASS = 2_000_000
MAX_STATE_SPOOL_BYTES = 20 * 1024**3
MAX_CANONICAL_TABLE_BYTES = 4 * 1024**3
EXPECTED_RELEASE_IDS = tuple(core.RELEASE_IDS)
EXPECTED_MISSION_SHA = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
EXPECTED_MANIFEST_SHA256 = {
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03":
        "6fedbd5d2b0d811b5189a953331bd236aeea3fb5843a5a549d1100d5ee290505",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5":
        "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd",
}
STAGE_NAME = "L2_HYPOTHESIS_TESTS"


class L2StageError(RuntimeError):
    pass


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def atomic_copy_csv(con, query: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".tmp" + path.suffix)
    con.execute(
        f"COPY ({query}) TO {core.quote(str(temporary))} "
        "(HEADER, DELIMITER ',', QUOTE '\"', ESCAPE '\"')"
    )
    if temporary.stat().st_size > MAX_CANONICAL_TABLE_BYTES:
        temporary.unlink()
        raise L2StageError(
            f"canonical table exceeds {MAX_CANONICAL_TABLE_BYTES} bytes: {path.name}"
        )
    os.replace(temporary, path)


def logit_e4(price_e4: int) -> float:
    value = max(1, min(9999, int(price_e4)))
    return math.log(value / (10000.0 - value))


def expit(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def spread_bin(value: float | None) -> str:
    if value is None:
        return "INVALID"
    if value < 0.10:
        return "S00_010"
    if value < 0.25:
        return "S010_025"
    if value < 0.50:
        return "S025_050"
    return "S050_PLUS"


def price_bin(mid_logodds: float | None) -> str:
    if mid_logodds is None:
        return "INVALID"
    price = expit(mid_logodds)
    if price < 0.10:
        return "P00_010"
    if price < 0.25:
        return "P010_025"
    if price < 0.75:
        return "P025_075"
    if price < 0.90:
        return "P075_090"
    return "P090_100"


def activity_bin(updates_1s: int) -> str:
    if updates_1s <= 2:
        return "A00_02"
    if updates_1s <= 10:
        return "A03_10"
    if updates_1s <= 50:
        return "A11_50"
    return "A51_PLUS"


def size_bin(removed_e4: int) -> str:
    contracts = removed_e4 / 10_000.0
    if contracts < 5:
        return "Q01_05"
    if contracts < 25:
        return "Q05_25"
    if contracts < 100:
        return "Q025_100"
    return "Q100_PLUS"


def stable_control_sample(market: str, clock_ns: int) -> bool:
    token = zlib.crc32(market.encode("utf-8")) ^ int(clock_ns)
    return token % CONTROL_SAMPLE_MODULUS == 0


def parse_levels(value: object) -> dict[int, int]:
    """Strictly parse one snapshot side; duplicate prices are rejected."""
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        raise ValueError("snapshot side is null")
    if not isinstance(value, (list, tuple)):
        raise ValueError("snapshot side is not a sequence")
    levels: dict[int, int] = {}
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("snapshot level is not [price,qty]")
        price, qty = int(item[0]), int(item[1])
        if not 1 <= price <= 9999 or qty < 0:
            raise ValueError("snapshot level is outside fixed-point bounds")
        if price in levels:
            raise ValueError("duplicate snapshot price")
        if qty:
            levels[price] = qty
    return levels


@dataclass
class Book:
    yes: dict[int, int] = field(default_factory=dict)
    no: dict[int, int] = field(default_factory=dict)
    valid: bool = False
    snapshot_epoch: int = 0

    def snapshot(self, yes_levels: object, no_levels: object) -> None:
        self.yes = parse_levels(yes_levels)
        self.no = parse_levels(no_levels)
        self.valid = True
        self.snapshot_epoch += 1

    def invalidate(self) -> None:
        self.yes.clear()
        self.no.clear()
        self.valid = False

    def side_book(self, side: str) -> dict[int, int]:
        if side == "yes":
            return self.yes
        if side == "no":
            return self.no
        raise ValueError("side must be yes or no")

    def best_raw(self, side: str) -> tuple[int | None, int]:
        levels = self.side_book(side)
        if not self.valid or not levels:
            return None, 0
        price = max(levels)
        return price, levels[price]

    def depth_at_or_better(self, side: str, raw_price: int) -> int:
        return sum(qty for price, qty in self.side_book(side).items() if price >= raw_price)

    def top(self) -> dict:
        if not self.valid:
            return {"valid_two_sided": False}
        bid, bid_qty = self.best_raw("yes")
        no_bid, ask_qty = self.best_raw("no")
        ask = None if no_bid is None else 10000 - no_bid
        if bid is None or ask is None or not (0 < bid < ask < 10000):
            return {
                "valid_two_sided": False,
                "bid_e4": bid,
                "bid_qty_e4": bid_qty,
                "ask_e4": ask,
                "ask_qty_e4": ask_qty,
            }
        bid_lo, ask_lo = logit_e4(bid), logit_e4(ask)
        return {
            "valid_two_sided": True,
            "bid_e4": bid,
            "bid_qty_e4": bid_qty,
            "ask_e4": ask,
            "ask_qty_e4": ask_qty,
            "spread_logodds": ask_lo - bid_lo,
            "mid_logodds": (ask_lo + bid_lo) / 2.0,
        }

    def delta(self, side: str, price_e4: int, delta_e4: int) -> dict:
        """Apply a typed delta; negative resulting depth invalidates the book."""
        if not self.valid:
            return {"applied": False, "reason": "delta_before_snapshot"}
        if side not in ("yes", "no") or not 1 <= int(price_e4) <= 9999 or not delta_e4:
            self.invalidate()
            return {"applied": False, "reason": "malformed_delta", "invalidated": True}
        before_top = self.top()
        pre_price, pre_qty = self.best_raw(side)
        levels = self.side_book(side)
        current = levels.get(int(price_e4), 0)
        nxt = current + int(delta_e4)
        if nxt < 0:
            self.invalidate()
            return {"applied": False, "reason": "negative_result", "invalidated": True}
        if nxt:
            levels[int(price_e4)] = nxt
        else:
            levels.pop(int(price_e4), None)
        after_top = self.top()
        post_at_original = levels.get(pre_price, 0) if pre_price is not None else 0
        removed = max(0, pre_qty - post_at_original)
        touch_depletion = bool(
            int(delta_e4) < 0
            and pre_price is not None
            and int(price_e4) == pre_price
            and pre_qty >= MIN_TOUCH_QTY_E4
            and removed >= MIN_TOUCH_QTY_E4
            and removed / pre_qty >= MIN_DEPLETION_FRACTION
        )
        top_changed = before_top != after_top
        return {
            "applied": True,
            "before_top": before_top,
            "after_top": after_top,
            "pre_touch_price": pre_price,
            "pre_touch_qty_e4": pre_qty,
            "removed_e4": removed,
            "depletion_fraction": removed / pre_qty if pre_qty else 0.0,
            "touch_depletion": touch_depletion,
            "top_changed": top_changed,
        }


def receipt_assessment(receipt: Mapping[str, object]) -> dict:
    """Conservative whole-day decision from the authoritative sealed receipt."""
    markers = receipt.get("recorder_markers") or {}
    if not isinstance(markers, Mapping):
        markers = {}
    blocking_markers = {
        str(key): int(value or 0)
        for key, value in markers.items()
        if str(key).lower() in {"gap", "loss", "epoch_change"} and int(value or 0) > 0
    }
    blockers = []
    for key in (
        "parse_errors",
        "seq_gap_events",
        "seq_missed_total",
        "seq_regressions",
        "markers_lost_frames",
    ):
        if int(receipt.get(key) or 0) > 0:
            blockers.append(f"{key}={int(receipt.get(key) or 0)}")
    if bool(receipt.get("no_l2_files")):
        blockers.append("no_l2_files=true")
    if int(receipt.get("lines") or 0) <= 0:
        blockers.append("lines<=0")
    if blocking_markers:
        blockers.append("blocking_recorder_markers=" + json.dumps(blocking_markers, sort_keys=True))
    return {
        "date": receipt.get("date"),
        "usable_for_continuous_replay": not blockers,
        "blockers": blockers,
        "stream_restarts": int(receipt.get("stream_restarts") or 0),
        "snapshot_re_anchors_total": int(receipt.get("snapshot_re_anchors_total") or 0),
        "sequence_authority": "sealed quality/l2_gaps.json over complete per-sid raw streams",
        "per_market_ws_seq_gap_inference_used": False,
    }


def preregistered_design(replicates: int) -> dict:
    return {
        "schema_version": "sports-autoresearch-l2-preregistered-design-v1",
        "written_before_outcome_query": True,
        "banner": BANNER,
        "split": {"feature_training_date": TRAIN_DATE, "evaluation_date": EVAL_DATE},
        "common": {
            "decision_clock": "recv_wall_ns; recv_mono_ns is deterministic receive-order tie-break",
            "root_mapping": "PROVISIONAL_HEURISTIC_MATCHUP_TIME and t_us>=dim_effective_us",
            "flow_quality": "whole-day usability comes only from sealed quality/l2_gaps.json; per-market ws_seq gaps forbidden",
            "minimum_games": MIN_RELEVANT_GAMES,
            "minimum_day_blocks": MIN_DAY_BLOCKS,
            "bootstrap_replicates": replicates,
            "bootstrap_seed": SEED,
            "current_split_constraint": "one evaluation day makes calendar-day bootstrap degenerate; no rejection/promotion/verdict",
        },
        "C1-HFOLLOW-RETREAT-01": {
            "single_market_retreat": "negative touch delta removes >=1 contract and >=50% of pre-delta touch quantity",
            "treatment": "first causal event where trailing 100ms includes retreat in >= frozen q90/minimum-two distinct sibling markets of the same root and literal book side",
            "threshold_training": "ceil(q90 distinct-market causal retreat score on 2026-07-12), minimum 2",
            "control": "deterministically sampled top-changing non-retreat delta with no same-root/side retreat +/-100ms; exact sport/series-family/hour/side/price-band/spread/activity strata; nearest time; different root; no replacement",
            "horizons_us": list(HFOLLOW_HORIZONS_US),
            "outcomes": ["treatment-minus-control adverse signed log-odds move", "treatment-minus-control log-odds spread change"],
            "expected_direction": "positive for both outcomes",
            "negative_controls": ["future-shift both matched anchors by -10s", "deterministic root-label rotation", "lookahead component sentinel"],
            "data_starved": "no clean train/eval receipt, no trained threshold, or no matchable eval treatment/control",
            "collect_more": "any nonzero study with <200 games or <20 evaluation day blocks",
            "rejection": "only with >=200 games and >=20 days, multiplicity-adjusted absence beyond the frozen economic threshold, stable negative controls; impossible on this split",
        },
        "C1-DEPLETION-REFILL-01": {
            "depletion": "negative delta at current touch removes >=1 contract and >=50% of touch quantity",
            "refill": "same-side displayed depth at original touch or better adds back >=80% of removed quantity before boundary",
            "snapshot_rule": "snapshot closes an open episode as right-censored and reanchors the book; snapshot is never a refill",
            "treatment": "refill observed within fixed 100ms or 1s; decision clock is the causal refill receive row",
            "control": "no refill through the fixed window with observation intact; decision clock is depletion+window; exact sport/series-family/hour/side/depletion-size/spread/activity strata; nearest time; different root; no replacement",
            "horizons_us": list(REFILL_HORIZONS_US),
            "outcomes": ["treatment-minus-control adverse signed log-odds move", "treatment-minus-control spread recovery relative to pre-depletion spread"],
            "expected_direction": "negative adverse-move effect and positive spread-recovery effect",
            "negative_controls": ["future-refill decision sentinel", "snapshot-reset-as-refill sentinel", "far-from-touch sentinel", "side-flip algebra"],
            "data_starved": "no clean eval receipt, no qualifying depletion/refill population, or no matched pairs",
            "collect_more": "any nonzero study with <200 games or <20 evaluation day blocks",
            "rejection": "only with >=200 games and >=20 days after multiplicity and controls; impossible on this split",
        },
    }


class StateSpool:
    HEADER = [
        "state_id", "date", "clock_ns", "market_ticker", "root_event_id",
        "sport", "family", "bid_e4", "bid_qty_e4", "ask_e4", "ask_qty_e4",
        "spread_logodds", "mid_logodds", "snapshot_epoch",
    ]

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=self.HEADER)
        self.writer.writeheader()
        self.rows = 0

    def add(self, row: Mapping[str, object]) -> None:
        self.writer.writerow({key: row.get(key) for key in self.HEADER})
        self.rows += 1
        if self.rows % 100_000 == 0:
            self.handle.flush()
            if self.handle.tell() > MAX_STATE_SPOOL_BYTES:
                raise L2StageError(
                    f"L2 state spool exceeds fail-closed cap {MAX_STATE_SPOOL_BYTES} bytes"
                )

    def close(self) -> None:
        if self.handle.closed:
            return
        self.handle.flush()
        too_large = self.handle.tell() > MAX_STATE_SPOOL_BYTES
        os.fsync(self.handle.fileno())
        self.handle.close()
        if too_large:
            raise L2StageError(
                f"L2 state spool exceeds fail-closed cap {MAX_STATE_SPOOL_BYTES} bytes"
            )


def event_base(
    *, date: str, clock_ns: int, t_us: int, market: str, meta: Mapping[str, object],
    side: str, top: Mapping[str, object], activity_1s: int,
) -> dict:
    return {
        "date": date,
        "clock_ns": int(clock_ns),
        "t_us": int(t_us),
        "market_ticker": market,
        "root_event_id": str(meta["root_event_id"]),
        "sport": str(meta.get("sport") or "_UNKNOWN"),
        "family": str(meta.get("family") or "_UNKNOWN"),
        "hour_utc": int((clock_ns // 3_600_000_000_000) % 24),
        "side": side,
        "direction_sign": -1 if side == "yes" else 1,
        "spread_logodds": top.get("spread_logodds"),
        "mid_logodds": top.get("mid_logodds"),
        "spread_bin": spread_bin(top.get("spread_logodds")),
        "price_bin": price_bin(top.get("mid_logodds")),
        "activity_1s": int(activity_1s),
        "activity_bin": activity_bin(activity_1s),
    }


def finalize_episode(
    episode: dict, end_ns: int, reason: str, completed: list[dict],
) -> None:
    if len(completed) >= MAX_SPARSE_EVENTS_PER_CLASS:
        raise L2StageError(
            "depletion lifecycle population exceeded the fail-closed sparse-event cap"
        )
    episode["observation_end_ns"] = max(episode["depletion_ns"], int(end_ns))
    episode["endpoint_reason"] = reason
    episode["duration_us"] = (
        min(episode["observation_end_ns"], episode["depletion_ns"] + 1_000_000_000)
        - episode["depletion_ns"]
    ) // 1000
    episode["event_observed"] = episode.get("refill_ns") is not None
    completed.append(episode)


def replay_rows(
    rows: Iterable[Sequence[object]],
    *,
    state_sink: StateSpool | None = None,
) -> dict:
    """Replay rows in receive order.

    Row order/shape is the production query contract documented in ``run``.
    The function is pure apart from the optional state spool, so small fixtures
    exercise exactly the same snapshot/depletion semantics as W09.
    """
    books: dict[str, Book] = {}
    activity: dict[str, deque[int]] = defaultdict(deque)
    open_episodes: dict[tuple[str, str], dict] = {}
    retreats: list[dict] = []
    control_windows: list[dict] = []
    completed: list[dict] = []
    last_day_ns: dict[str, int] = {}
    state_id = 0
    retreat_id = 0
    control_id = 0
    depletion_id = 0
    current_date: str | None = None
    qc = defaultdict(int)

    def expire_market(market: str, clock_ns: int) -> None:
        for side in ("yes", "no"):
            key = (market, side)
            episode = open_episodes.get(key)
            if episode and clock_ns >= episode["depletion_ns"] + 1_000_000_000:
                finalize_episode(
                    episode, episode["depletion_ns"] + 1_000_000_000,
                    "right_censored_1s_horizon", completed,
                )
                del open_episodes[key]

    def close_day(day: str | None) -> None:
        if day is None:
            return
        day_end = last_day_ns.get(day, 0)
        for key, episode in list(open_episodes.items()):
            if episode["date"] != day:
                continue
            boundary = min(day_end, episode["depletion_ns"] + 1_000_000_000)
            reason = (
                "right_censored_1s_horizon"
                if day_end >= episode["depletion_ns"] + 1_000_000_000
                else "right_censored_day_end"
            )
            finalize_episode(episode, boundary, reason, completed)
            del open_episodes[key]

    for raw in rows:
        (
            date_value, t_us, recv_wall_ns, recv_mono_ns, market, msg_type,
            side, price_e4, delta_e4, yes_levels, no_levels, ws_sid, ws_seq,
            root_event_id, sport, family, dim_effective_us,
        ) = raw
        date = str(date_value)
        if current_date is not None and date != current_date:
            close_day(current_date)
            books.clear()
            activity.clear()
        current_date = date
        qc["rows_seen"] += 1
        if recv_wall_ns is None or recv_mono_ns is None or t_us is None:
            qc["missing_tl1_clock_rows"] += 1
            continue
        clock_ns = int(recv_wall_ns)
        last_day_ns[date] = max(last_day_ns.get(date, 0), clock_ns)
        market = str(market)
        book = books.setdefault(market, Book())
        expire_market(market, clock_ns)
        updates = activity[market]
        updates.append(clock_ns)
        while updates and updates[0] < clock_ns - 1_000_000_000:
            updates.popleft()
        n_activity = len(updates)
        meta = {
            "root_event_id": root_event_id,
            "sport": sport,
            "family": family,
            "dim_effective_us": dim_effective_us,
        }
        causal_mapping = bool(
            root_event_id
            and dim_effective_us is not None
            and int(t_us) >= int(dim_effective_us)
        )
        msg_type = str(msg_type or "").lower()
        if msg_type == "snapshot":
            for episode_side in ("yes", "no"):
                key = (market, episode_side)
                if key in open_episodes:
                    finalize_episode(
                        open_episodes.pop(key), clock_ns,
                        "right_censored_snapshot_boundary", completed,
                    )
                    qc["snapshot_censored_episodes"] += 1
            try:
                book.snapshot(yes_levels, no_levels)
            except (TypeError, ValueError, json.JSONDecodeError):
                book.invalidate()
                qc["invalid_snapshot_rows"] += 1
                continue
            qc["snapshots_applied"] += 1
            top = book.top()
            if causal_mapping and top.get("valid_two_sided") and state_sink:
                state_id += 1
                state_sink.add({
                    "state_id": state_id, "date": date, "clock_ns": clock_ns,
                    "market_ticker": market, "root_event_id": root_event_id,
                    "sport": sport or "_UNKNOWN", "family": family or "_UNKNOWN",
                    "bid_e4": top["bid_e4"], "bid_qty_e4": top["bid_qty_e4"],
                    "ask_e4": top["ask_e4"], "ask_qty_e4": top["ask_qty_e4"],
                    "spread_logodds": top["spread_logodds"],
                    "mid_logodds": top["mid_logodds"],
                    "snapshot_epoch": book.snapshot_epoch,
                })
            continue
        if msg_type != "delta":
            qc["invalid_msg_type_rows"] += 1
            continue
        if ws_seq is None:
            qc["rows_with_null_ws_seq"] += 1
        result = book.delta(str(side or "").lower(), int(price_e4 or 0), int(delta_e4 or 0))
        if not result.get("applied"):
            qc[result.get("reason", "delta_rejected")] += 1
            if result.get("invalidated"):
                for episode_side in ("yes", "no"):
                    key = (market, episode_side)
                    if key in open_episodes:
                        finalize_episode(
                            open_episodes.pop(key), clock_ns,
                            "right_censored_invalid_book", completed,
                        )
            continue
        qc["deltas_applied"] += 1
        top = result["after_top"]
        if not causal_mapping or not top.get("valid_two_sided"):
            continue
        if result.get("top_changed") and state_sink:
            state_id += 1
            state_sink.add({
                "state_id": state_id, "date": date, "clock_ns": clock_ns,
                "market_ticker": market, "root_event_id": root_event_id,
                "sport": sport or "_UNKNOWN", "family": family or "_UNKNOWN",
                "bid_e4": top["bid_e4"], "bid_qty_e4": top["bid_qty_e4"],
                "ask_e4": top["ask_e4"], "ask_qty_e4": top["ask_qty_e4"],
                "spread_logodds": top["spread_logodds"],
                "mid_logodds": top["mid_logodds"],
                "snapshot_epoch": book.snapshot_epoch,
            })

        event_side = str(side).lower()
        existing = open_episodes.get((market, event_side))
        if existing:
            recovered = (
                book.depth_at_or_better(event_side, existing["original_touch_price_e4"])
                - existing["post_depletion_depth_e4"]
            )
            fraction = max(0.0, recovered / existing["removed_e4"])
            if fraction >= REFILL_FRACTION:
                existing["refill_ns"] = clock_ns
                existing["refill_fraction"] = fraction
                existing["refill_snapshot_epoch"] = book.snapshot_epoch
                existing["refill_state_id"] = (
                    state_id if result.get("top_changed") and state_sink else None
                )
                finalize_episode(existing, clock_ns, "refill_observed", completed)
                del open_episodes[(market, event_side)]
                qc["refills_observed"] += 1

        base = event_base(
            date=date, clock_ns=clock_ns, t_us=int(t_us), market=market,
            meta=meta, side=event_side, top=top, activity_1s=n_activity,
        )
        base["decision_state_id"] = (
            state_id if result.get("top_changed") and state_sink else None
        )
        if result.get("touch_depletion"):
            if int(price_e4) != int(result["pre_touch_price"]):
                qc["far_from_touch_qualifying_depletion_rows"] += 1
            retreat_id += 1
            retreat = {
                **base,
                "event_id": f"HF-R-{retreat_id:012d}",
                "removed_e4": int(result["removed_e4"]),
                "depletion_fraction": float(result["depletion_fraction"]),
                "snapshot_epoch": book.snapshot_epoch,
            }
            retreats.append(retreat)
            key = (market, event_side)
            if key in open_episodes:
                finalize_episode(
                    open_episodes.pop(key), clock_ns,
                    "right_censored_repeated_depletion", completed,
                )
            depletion_id += 1
            original = int(result["pre_touch_price"])
            open_episodes[key] = {
                **base,
                "depletion_id": f"DEP-{depletion_id:012d}",
                "depletion_ns": clock_ns,
                "original_touch_price_e4": original,
                "pre_touch_qty_e4": int(result["pre_touch_qty_e4"]),
                "removed_e4": int(result["removed_e4"]),
                "depletion_fraction": float(result["depletion_fraction"]),
                "post_depletion_depth_e4": book.depth_at_or_better(event_side, original),
                "pre_depletion_spread_logodds": result["before_top"].get("spread_logodds"),
                "snapshot_epoch": book.snapshot_epoch,
                "refill_ns": None,
                "refill_fraction": None,
            }
            qc["qualifying_touch_depletions"] += 1
        elif result.get("top_changed") and stable_control_sample(market, clock_ns):
            control_id += 1
            control_windows.append({
                **base,
                "event_id": f"HF-C-{control_id:012d}",
                "snapshot_epoch": book.snapshot_epoch,
            })

        if (
            len(retreats) > MAX_SPARSE_EVENTS_PER_CLASS
            or len(control_windows) > MAX_SPARSE_EVENTS_PER_CLASS
            or len(completed) > MAX_SPARSE_EVENTS_PER_CLASS
        ):
            raise L2StageError(
                "sparse L2 event population exceeded the preregistered in-memory cap; "
                "refusing silent truncation"
            )

    close_day(current_date)
    qc["top_state_rows"] = state_sink.rows if state_sink else 0
    qc["retreat_events"] = len(retreats)
    qc["sampled_nonretreat_controls"] = len(control_windows)
    qc["depletion_episodes"] = len(completed)
    qc["per_market_ws_seq_gap_inference_used"] = False
    qc["sequence_quality_source"] = "sealed quality/l2_gaps.json only"
    return {
        "retreats": retreats,
        "control_windows": control_windows,
        "depletion_episodes": completed,
        "qc": dict(qc),
    }


def annotate_retreats(retreats: list[dict]) -> tuple[list[dict], int | None]:
    ordered = sorted(retreats, key=lambda r: (r["date"], r["clock_ns"], r["event_id"]))
    queues: dict[tuple[str, str, str], deque[dict]] = defaultdict(deque)
    by_key_times: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for event in ordered:
        key = (event["date"], event["root_event_id"], event["side"])
        queue = queues[key]
        while queue and queue[0]["clock_ns"] < event["clock_ns"] - COORDINATION_WINDOW_NS:
            queue.popleft()
        queue.append(event)
        markets = {item["market_ticker"] for item in queue}
        event["causal_coordination_score"] = len(markets)
        event["causal_component_max_ns"] = max(item["clock_ns"] for item in queue)
        by_key_times[key].append(event["clock_ns"])
    training_scores = [
        int(event["causal_coordination_score"])
        for event in ordered if event["date"] == TRAIN_DATE
    ]
    threshold_value = percentile(training_scores, 0.90)
    threshold = max(2, int(math.ceil(threshold_value))) if threshold_value is not None else None
    return ordered, threshold


def has_nearby_retreat(
    index: Mapping[tuple[str, str, str], Sequence[int]], event: Mapping[str, object],
) -> bool:
    key = (str(event["date"]), str(event["root_event_id"]), str(event["side"]))
    times = index.get(key, ())
    left = bisect.bisect_left(times, int(event["clock_ns"]) - COORDINATION_WINDOW_NS)
    return left < len(times) and times[left] <= int(event["clock_ns"]) + COORDINATION_WINDOW_NS


def hfollow_populations(
    retreats: list[dict], controls: list[dict], threshold: int | None,
) -> tuple[list[dict], list[dict], dict]:
    if threshold is None:
        return [], [], {"trained_threshold": None, "lookahead_sentinel_rows": 0}
    index: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for event in retreats:
        index[(event["date"], event["root_event_id"], event["side"])].append(event["clock_ns"])
    for values in index.values():
        values.sort()
    treatments = []
    last_anchor: dict[tuple[str, str, str], int] = {}
    for event in retreats:
        if event["date"] != EVAL_DATE or event["causal_coordination_score"] < threshold:
            continue
        key = (event["date"], event["root_event_id"], event["side"])
        if event["clock_ns"] - last_anchor.get(key, -10**30) <= COORDINATION_WINDOW_NS:
            continue
        last_anchor[key] = event["clock_ns"]
        treatments.append(event)
    eligible_controls = [
        event for event in controls
        if event["date"] == EVAL_DATE and not has_nearby_retreat(index, event)
    ]
    return treatments, eligible_controls, {
        "trained_threshold": threshold,
        "training_retreat_events": sum(r["date"] == TRAIN_DATE for r in retreats),
        "evaluation_retreat_events": sum(r["date"] == EVAL_DATE for r in retreats),
        "lookahead_sentinel_rows": sum(
            int(r["causal_component_max_ns"]) > int(r["clock_ns"]) for r in retreats
        ),
    }


HFOLLOW_STRATA = (
    "date", "sport", "family", "hour_utc", "side", "price_bin", "spread_bin",
    "activity_bin",
)
DEPLETION_STRATA = (
    "date", "sport", "family", "hour_utc", "side", "depletion_size_bin",
    "spread_bin", "activity_bin",
)


def deterministic_match(
    treatments: Sequence[dict], controls: Sequence[dict], strata_fields: Sequence[str],
    *, treatment_clock: str = "clock_ns", control_clock: str = "clock_ns",
) -> tuple[list[dict], dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for control in controls:
        grouped[tuple(control.get(field) for field in strata_fields)].append(control)
    for values in grouped.values():
        values.sort(key=lambda r: (int(r[control_clock]), str(r.get("event_id") or r.get("depletion_id"))))
    used: set[str] = set()
    pairs = []
    unmatched = defaultdict(int)
    for treatment in sorted(
        treatments, key=lambda r: (str(r["date"]), int(r[treatment_clock]), str(r.get("event_id") or r.get("depletion_id"))),
    ):
        key = tuple(treatment.get(field) for field in strata_fields)
        candidates = grouped.get(key, [])
        if not candidates:
            unmatched["no_exact_stratum"] += 1
            continue
        clocks = [int(item[control_clock]) for item in candidates]
        center = bisect.bisect_left(clocks, int(treatment[treatment_clock]))
        selected = None
        left, right = center - 1, center
        while left >= 0 or right < len(candidates):
            if left < 0:
                i, right = right, right + 1
            elif right >= len(candidates):
                i, left = left, left - 1
            else:
                left_distance = abs(clocks[left] - int(treatment[treatment_clock]))
                right_distance = abs(clocks[right] - int(treatment[treatment_clock]))
                if left_distance <= right_distance:
                    i, left = left, left - 1
                else:
                    i, right = right, right + 1
            candidate = candidates[i]
            identity = str(candidate.get("event_id") or candidate.get("depletion_id"))
            if identity in used or candidate["root_event_id"] == treatment["root_event_id"]:
                continue
            selected = candidate
            used.add(identity)
            break
        if selected is None:
            unmatched["no_unused_different_root"] += 1
            continue
        pairs.append({"treatment": treatment, "control": selected})
    return pairs, {
        "treatments": len(treatments),
        "controls": len(controls),
        "matched_pairs": len(pairs),
        "unmatched": dict(unmatched),
        "control_reuse": 0,
        "different_root_required": True,
        "strata": list(strata_fields),
    }


def depletion_populations(episodes: Sequence[dict], window_us: int) -> tuple[list[dict], list[dict]]:
    window_ns = int(window_us) * 1000
    treatments, controls = [], []
    for source in episodes:
        if source["date"] != EVAL_DATE:
            continue
        row = dict(source)
        row["depletion_size_bin"] = size_bin(int(row["removed_e4"]))
        row["event_id"] = row["depletion_id"] + f"-W{window_us}"
        deadline = int(row["depletion_ns"]) + window_ns
        refill_ns = row.get("refill_ns")
        if refill_ns is not None and int(refill_ns) <= deadline:
            row["decision_ns"] = int(refill_ns)
            row["clock_ns"] = int(refill_ns)
            row["decision_state_id"] = row.get("refill_state_id")
            treatments.append(row)
        elif int(row.get("observation_end_ns") or 0) >= deadline:
            row["decision_ns"] = deadline
            row["clock_ns"] = deadline
            row["decision_state_id"] = None
            controls.append(row)
    return treatments, controls


def rotated_root_coordination_count(retreats: Sequence[dict], threshold: int | None) -> int | None:
    if threshold is None:
        return None
    roots = sorted({r["root_event_id"] for r in retreats if r["date"] == EVAL_DATE})
    if len(roots) < 2:
        return 0
    rotated = {root: roots[(i + 1) % len(roots)] for i, root in enumerate(roots)}
    by_market = {}
    for event in sorted(retreats, key=lambda r: (r["market_ticker"], r["root_event_id"])):
        by_market.setdefault(event["market_ticker"], rotated[event["root_event_id"]])
    queues: dict[tuple[str, str, str], deque[dict]] = defaultdict(deque)
    count = 0
    last: dict[tuple[str, str, str], int] = {}
    for event in sorted(retreats, key=lambda r: (r["date"], r["clock_ns"], r["event_id"])):
        if event["date"] != EVAL_DATE:
            continue
        root = by_market[event["market_ticker"]]
        key = (event["date"], root, event["side"])
        queue = queues[key]
        while queue and queue[0]["clock_ns"] < event["clock_ns"] - COORDINATION_WINDOW_NS:
            queue.popleft()
        queue.append(event)
        if len({item["market_ticker"] for item in queue}) >= threshold:
            if event["clock_ns"] - last.get(key, -10**30) > COORDINATION_WINDOW_NS:
                count += 1
                last[key] = event["clock_ns"]
    return count


def load_state_table(con, spool: StateSpool) -> None:
    spool.close()
    con.execute("""
      CREATE OR REPLACE TABLE l2_top_states(
        state_id BIGINT,date DATE,clock_ns BIGINT,market_ticker VARCHAR,
        root_event_id VARCHAR,sport VARCHAR,family VARCHAR,bid_e4 INTEGER,
        bid_qty_e4 BIGINT,ask_e4 INTEGER,ask_qty_e4 BIGINT,
        spread_logodds DOUBLE,mid_logodds DOUBLE,snapshot_epoch BIGINT
      )
    """)
    con.execute(
        f"COPY l2_top_states FROM {core.quote(str(spool.path))} "
        "(HEADER TRUE, DELIMITER ',', NULL '')"
    )
    con.execute("""
      CREATE OR REPLACE TABLE l2_top_states_final AS
      SELECT * EXCLUDE(rn) FROM (
        SELECT *,row_number() OVER (
          PARTITION BY date,market_ticker,clock_ns ORDER BY state_id DESC
        ) AS rn FROM l2_top_states
      ) WHERE rn=1
    """)


def build_candidate_legs(
    hfollow_pairs: Sequence[dict], depletion_pairs: Mapping[int, Sequence[dict]],
) -> list[tuple]:
    rows = []
    pair_counter = 0

    def append_pair(analysis: str, window_us: int, pair: Mapping[str, dict], shift_ns: int = 0) -> None:
        nonlocal pair_counter
        pair_counter += 1
        pair_id = f"PAIR-{pair_counter:012d}"
        for role in ("treatment", "control"):
            event = pair[role]
            rows.append((
                pair_id, analysis, int(window_us), role, str(event["event_id"]),
                event["date"], int(event["clock_ns"]) + int(shift_ns),
                event["market_ticker"], event["root_event_id"], event["sport"],
                event["family"], int(event["direction_sign"]),
                event.get("pre_depletion_spread_logodds"),
                None if shift_ns else event.get("decision_state_id"),
            ))

    for pair in hfollow_pairs:
        append_pair("HFOLLOW", 0, pair)
        append_pair("HFOLLOW_FUTURE_SHIFT_CONTROL", 0, pair, -10_000_000_000)
    for window_us, pairs in sorted(depletion_pairs.items()):
        for pair in pairs:
            append_pair("DEPLETION_REFILL", int(window_us), pair)
    return rows


def compute_outcomes(con, candidate_rows: Sequence[tuple]) -> None:
    con.execute("""
      CREATE OR REPLACE TABLE l2_candidate_legs(
        pair_id VARCHAR,analysis VARCHAR,window_us BIGINT,role VARCHAR,
        event_id VARCHAR,date DATE,clock_ns BIGINT,market_ticker VARCHAR,
        root_event_id VARCHAR,sport VARCHAR,family VARCHAR,direction_sign INTEGER,
        pre_depletion_spread_logodds DOUBLE,decision_state_id BIGINT
      )
    """)
    if candidate_rows:
        con.executemany(
            "INSERT INTO l2_candidate_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            candidate_rows,
        )
    con.execute("""
      CREATE OR REPLACE TABLE l2_horizon_grid(
        analysis VARCHAR,window_us BIGINT,horizon_us BIGINT
      )
    """)
    horizon_rows = [
        (analysis, window, horizon)
        for analysis, windows, horizons in (
            ("HFOLLOW", (0,), HFOLLOW_HORIZONS_US),
            ("HFOLLOW_FUTURE_SHIFT_CONTROL", (0,), HFOLLOW_HORIZONS_US),
            ("DEPLETION_REFILL", REFILL_WINDOWS_US, REFILL_HORIZONS_US),
        )
        for window in windows for horizon in horizons
    ]
    con.executemany("INSERT INTO l2_horizon_grid VALUES (?,?,?)", horizon_rows)
    con.execute(f"""
      CREATE OR REPLACE TABLE l2_candidate_asof AS
      SELECT c.*,s.state_id AS asof_state_id,s.clock_ns AS asof_state_ns,
             s.mid_logodds AS asof_mid_logodds,
             s.spread_logodds AS asof_spread_logodds
      FROM (SELECT * FROM l2_candidate_legs ORDER BY date,market_ticker,clock_ns) c
      ASOF LEFT JOIN (
        SELECT * FROM l2_top_states_final ORDER BY date,market_ticker,clock_ns
      ) s
        ON c.date=s.date AND c.market_ticker=s.market_ticker
       AND c.clock_ns>=s.clock_ns
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l2_candidate_current AS
      SELECT c.*,
             coalesce(exact.clock_ns,c.asof_state_ns) AS current_state_ns,
             coalesce(exact.mid_logodds,c.asof_mid_logodds) AS current_mid_logodds,
             coalesce(exact.spread_logodds,c.asof_spread_logodds) AS current_spread_logodds
      FROM l2_candidate_asof c
      LEFT JOIN l2_top_states exact ON exact.state_id=c.decision_state_id
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE l2_outcome_legs AS
      WITH targets AS (
        SELECT c.*,h.horizon_us,c.clock_ns+h.horizon_us*1000 AS target_ns
        FROM l2_candidate_current c
        JOIN l2_horizon_grid h USING(analysis,window_us)
        WHERE c.current_state_ns IS NOT NULL
          AND c.clock_ns-c.current_state_ns BETWEEN 0 AND {BOOK_AGE_CAP_NS}
      )
      SELECT t.*,s.clock_ns AS future_state_ns,s.mid_logodds AS future_mid_logodds,
             s.spread_logodds AS future_spread_logodds,
             t.direction_sign*(s.mid_logodds-t.current_mid_logodds)
               AS adverse_move_logodds,
             s.spread_logodds-t.current_spread_logodds
               AS spread_change_logodds,
             t.pre_depletion_spread_logodds-s.spread_logodds
               AS spread_recovery_logodds
      FROM (SELECT * FROM targets ORDER BY date,market_ticker,target_ns) t
      ASOF LEFT JOIN (
        SELECT * FROM l2_top_states_final ORDER BY date,market_ticker,clock_ns
      ) s
        ON t.date=s.date AND t.market_ticker=s.market_ticker
       AND t.target_ns>=s.clock_ns
      WHERE s.clock_ns>=t.clock_ns
        AND t.target_ns-s.clock_ns BETWEEN 0 AND {BOOK_AGE_CAP_NS}
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l2_pair_effects AS
      SELECT t.pair_id,t.analysis,t.window_us,t.horizon_us,t.date,
             t.root_event_id AS treatment_root_event_id,
             c.root_event_id AS control_root_event_id,
             t.market_ticker AS treatment_market_ticker,
             c.market_ticker AS control_market_ticker,
             t.adverse_move_logodds-c.adverse_move_logodds
               AS adverse_effect_logodds,
             t.spread_change_logodds-c.spread_change_logodds
               AS spread_change_effect_logodds,
             t.spread_recovery_logodds-c.spread_recovery_logodds
               AS spread_recovery_effect_logodds,
             t.current_state_ns,t.future_state_ns,c.current_state_ns AS control_current_state_ns,
             c.future_state_ns AS control_future_state_ns
      FROM l2_outcome_legs t JOIN l2_outcome_legs c
        ON t.pair_id=c.pair_id AND t.analysis=c.analysis
       AND t.window_us=c.window_us AND t.horizon_us=c.horizon_us
      WHERE t.role='treatment' AND c.role='control'
      ORDER BY t.analysis,t.window_us,t.horizon_us,t.date,t.pair_id
    """)


def horizon_summaries(con) -> list[dict]:
    cursor = con.execute("""
      SELECT analysis,window_us,horizon_us,count(*) AS n_pairs,
             count(DISTINCT treatment_root_event_id) AS n_treatment_games,
             count(DISTINCT control_root_event_id) AS n_control_games,
             count(DISTINCT date) AS n_day_blocks,
             avg(adverse_effect_logodds) AS mean_adverse_effect_logodds,
             median(adverse_effect_logodds) AS p50_adverse_effect_logodds,
             quantile_cont(adverse_effect_logodds,.99) AS p99_adverse_effect_logodds,
             max(adverse_effect_logodds) AS max_adverse_effect_logodds,
             avg(spread_change_effect_logodds) AS mean_spread_change_effect_logodds,
             median(spread_change_effect_logodds) AS p50_spread_change_effect_logodds,
             quantile_cont(spread_change_effect_logodds,.99) AS p99_spread_change_effect_logodds,
             max(spread_change_effect_logodds) AS max_spread_change_effect_logodds,
             avg(spread_recovery_effect_logodds) AS mean_spread_recovery_effect_logodds,
             median(spread_recovery_effect_logodds) AS p50_spread_recovery_effect_logodds,
             quantile_cont(spread_recovery_effect_logodds,.99) AS p99_spread_recovery_effect_logodds,
             max(spread_recovery_effect_logodds) AS max_spread_recovery_effect_logodds
      FROM l2_pair_effects
      GROUP BY analysis,window_us,horizon_us
      ORDER BY analysis,window_us,horizon_us
    """)
    names = [item[0] for item in cursor.description]
    values = [dict(zip(names, row)) for row in cursor.fetchall()]
    unions = {
        (row["analysis"], int(row["window_us"]), int(row["horizon_us"])):
            int(row["n_games_union"])
        for row in core.rows_as_dicts(con, """
          WITH roots AS (
            SELECT analysis,window_us,horizon_us,treatment_root_event_id AS root_event_id
            FROM l2_pair_effects
            UNION
            SELECT analysis,window_us,horizon_us,control_root_event_id
            FROM l2_pair_effects
          )
          SELECT analysis,window_us,horizon_us,count(DISTINCT root_event_id) AS n_games_union
          FROM roots GROUP BY analysis,window_us,horizon_us
        """)
    }
    for row in values:
        row["n_games_union"] = unions.get(
            (row["analysis"], int(row["window_us"]), int(row["horizon_us"])), 0
        )
    return values


def bootstrap_effects(con, replicates: int) -> dict:
    rows = core.rows_as_dicts(con, """
      SELECT analysis,window_us,horizon_us,date,treatment_root_event_id,
             avg(adverse_effect_logodds) AS adverse,
             avg(spread_change_effect_logodds) AS spread_change,
             avg(spread_recovery_effect_logodds) AS spread_recovery
      FROM l2_pair_effects
      GROUP BY analysis,window_us,horizon_us,date,treatment_root_event_id
      ORDER BY analysis,window_us,horizon_us,date,treatment_root_event_id
    """)
    grouped: dict[tuple, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for row in rows:
        key = (row["analysis"], int(row["window_us"]), int(row["horizon_us"]))
        day, root = str(row["date"]), str(row["treatment_root_event_id"])
        for metric in ("adverse", "spread_change", "spread_recovery"):
            value = row.get(metric)
            if value is not None and math.isfinite(float(value)):
                grouped[(key, metric)][day][root] = float(value)
    output = {}
    for (key, metric), by_day in sorted(grouped.items(), key=str):
        analysis, window_us, horizon_us = key
        result = block_bootstrap_mean(
            by_day, replicates=replicates,
            seed=SEED + int(window_us) + int(horizon_us) + len(metric),
        )
        result["degenerate_one_day_block"] = result["n_days"] <= 1
        result["interpretation"] = (
            "DIAGNOSTIC_ONLY: one evaluation day is resampled as itself; the interval "
            "cannot support rejection, promotion, or a verdict."
            if result["n_days"] <= 1 else "Exploratory calendar-day block interval."
        )
        output[f"{analysis}|{window_us}|{horizon_us}|{metric}"] = result
    return output


def hypothesis_status(n_pairs: int, n_games: int, n_days: int, leakage: bool) -> tuple[str, str]:
    if leakage:
        return "INVALIDATED_BY_LEAKAGE", "A causal/lookahead sentinel was nonzero."
    if n_pairs <= 0 or n_games <= 0:
        return "DATA_STARVED", "No eligible matched treatment/control outcome survived the frozen data and quality gates."
    return (
        "COLLECT_MORE",
        f"Only {n_days} evaluation day block(s) and {n_games} treatment game(s); "
        f"frozen minima are {MIN_DAY_BLOCKS} days and {MIN_RELEVANT_GAMES} games.",
    )


def make_charts(run_dir: Path, horizon_rows: Sequence[dict], episodes: Sequence[dict]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_dir = run_dir / "REPORT/charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []

    hf = [r for r in horizon_rows if r["analysis"] == "HFOLLOW"]
    fig, ax = plt.subplots(figsize=(10, 6))
    if hf:
        x = [r["horizon_us"] / 1e6 for r in hf]
        ax.plot(x, [r["mean_adverse_effect_logodds"] for r in hf], marker="o", label="adverse move")
        ax.plot(x, [r["mean_spread_change_effect_logodds"] for r in hf], marker="s", label="spread change")
        ax.axhline(0, color="black", linewidth=.8)
        ax.set_xscale("log")
        ax.legend()
    else:
        ax.text(.5, .5, "DATA_STARVED — no matched H-FOLLOW outcomes", ha="center", va="center")
    ax.set_xlabel("Outcome horizon (seconds)")
    ax.set_ylabel("Treatment − matched control (log-odds)")
    ax.set_title("C1-HFOLLOW-RETREAT-01 matched effects\n" + BANNER)
    ax.grid(alpha=.25)
    fig.tight_layout()
    path = chart_dir / "C1-HFOLLOW-RETREAT-01__l2_effects.png"
    temporary = path.with_name(path.stem + ".tmp.png")
    fig.savefig(temporary, dpi=150)
    plt.close(fig)
    os.replace(temporary, path)
    artifacts.append(str(path.relative_to(run_dir)))

    durations = sorted(
        (min(int(e.get("duration_us") or 0), 1_000_000), bool(e.get("event_observed")))
        for e in episodes if e["date"] == EVAL_DATE
    )
    at_risk, survival = len(durations), 1.0
    curve_x, curve_y = [0.0], [1.0]
    for duration in sorted({d for d, _ in durations}):
        failures = sum(1 for d, observed in durations if d == duration and observed)
        censored = sum(1 for d, observed in durations if d == duration and not observed)
        if at_risk and failures:
            survival *= 1.0 - failures / at_risk
            curve_x.append(duration / 1e6)
            curve_y.append(survival)
        at_risk -= failures + censored
    fig, ax = plt.subplots(figsize=(10, 6))
    if durations:
        ax.step(curve_x, curve_y, where="post")
    else:
        ax.text(.5, .5, "DATA_STARVED — no eligible depletion episodes", ha="center", va="center")
    ax.set_xlim(0, 1.0)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Time since depletion (seconds; right-censored at snapshot/invalid/day/1s)")
    ax.set_ylabel("Estimated probability not yet refilled")
    ax.set_title("C1-DEPLETION-REFILL-01 refill survival\n" + BANNER)
    ax.grid(alpha=.25)
    fig.tight_layout()
    path = chart_dir / "C1-DEPLETION-REFILL-01__refill_survival.png"
    temporary = path.with_name(path.stem + ".tmp.png")
    fig.savefig(temporary, dpi=150)
    plt.close(fig)
    os.replace(temporary, path)
    artifacts.append(str(path.relative_to(run_dir)))
    return artifacts


def append_registry(run_dir: Path, summary_path: Path, summary_value: Mapping[str, object]) -> None:
    registry = run_dir / "TRIAL_REGISTRY.jsonl"
    if not registry.is_file():
        raise L2StageError("TRIAL_REGISTRY.jsonl missing")
    result_sha = sha256(summary_path)
    with registry.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        existing: dict[str, dict] = {}
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("stage") == STAGE_NAME:
                existing[str(row.get("trial_id"))] = row
        new_rows = []
        for hypothesis_id, result in summary_value["hypotheses"].items():
            prior = existing.get(hypothesis_id)
            if prior:
                if prior.get("result_sha256") != result_sha:
                    raise L2StageError(f"idempotency conflict in trial registry: {hypothesis_id}")
                continue
            new_rows.append({
                "recorded_at_utc": summary_value["completed_at_utc"],
                "trial_id": hypothesis_id,
                "stage": STAGE_NAME,
                "record_type": "RESULT_STAGE_APPEND",
                "result_opened": True,
                "hypothesis_status": result["hypothesis_status"],
                "artifact_status": "DIAGNOSTIC_ONLY",
                "execution_status": result["execution_status"],
                "split": "EXPLORATORY_ONLY",
                "evidence_tier": EVIDENCE_TIER,
                "timestamp_tier": TIMESTAMP_TIER,
                "release_ids": list(EXPECTED_RELEASE_IDS),
                "result_artifact": str(summary_path.relative_to(run_dir)),
                "result_sha256": result_sha,
            })
        handle.seek(0, os.SEEK_END)
        for row in new_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_run(run_dir: Path, inputs: Mapping[str, object]) -> dict:
    manifest_path = run_dir / "RUN_MANIFEST.json"
    if not manifest_path.is_file():
        raise L2StageError("RUN_MANIFEST.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_dir.name:
        raise L2StageError("run id/path mismatch")
    if manifest.get("mode") != "EXPLORATORY_AUTORESEARCH":
        raise L2StageError("stage requires MODE 1")
    if manifest.get("status") != "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING":
        raise L2StageError("L2 stage requires the completed Cycle-1 core status")
    if not manifest.get("analysis_started"):
        raise L2StageError("analysis gate has not opened")
    if (manifest.get("mission") or {}).get("sha256") != EXPECTED_MISSION_SHA:
        raise L2StageError("operator-pinned mission SHA mismatch")
    mission_archive = run_dir / "MISSION.md"
    if not mission_archive.is_file() or sha256(mission_archive) != EXPECTED_MISSION_SHA:
        raise L2StageError("archived mission content hash mismatch")
    gates = manifest.get("gates") or {}
    if not str((gates.get("gate_a") or {}).get("status", "")).startswith("PASS_"):
        raise L2StageError("Gate A is not PASS")
    if not str((gates.get("gate_b") or {}).get("status", "")).startswith("PASS_"):
        raise L2StageError("Gate B is not PASS")
    gate_c = gates.get("gate_c") or {}
    if gate_c.get("status") != "PASS_MODE1_ONLY" or gate_c.get("mode2_authorized") is not False:
        raise L2StageError("Gate C is not the frozen MODE-1-only authority")
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    if not attestation_path.is_file():
        raise L2StageError("W09 attestation missing")
    if sha256(attestation_path) != (gates.get("gate_b") or {}).get("attestation_sha256"):
        raise L2StageError("W09 attestation hash mismatch")
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    if (
        attestation.get("instance_id") != core.EXPECTED_INSTANCE
        or attestation.get("region") != core.EXPECTED_REGION
        or attestation.get("role") != core.EXPECTED_ROLE
        or attestation.get("instance_profile") != core.EXPECTED_ROLE
        or attestation.get("architecture") != "aarch64"
        or attestation.get("duckdb") != "1.4.5"
        or attestation.get("static_credentials_present") is not False
        or attestation.get("trading_credentials_present") is not False
        or attestation.get("ambient_aws_or_kalshi_variables") != []
        or attestation.get("static_credential_paths_present") != []
        or attestation.get("installation_sha256") != core.EXPECTED_W09_INSTALLATION_SHA256
        or attestation.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"
        or attestation.get("w09_run_inhibitor_present") is not True
        or attestation.get("w09_run_inhibitor_is_ancestor") is not True
    ):
        raise L2StageError("W09 identity or DuckDB pin mismatch")
    selected = {item["release_id"]: item for item in manifest.get("selected_releases", [])}
    if set(selected) != set(EXPECTED_RELEASE_IDS):
        raise L2StageError("selected release set mismatch")
    for release in inputs["releases"]:
        frozen = selected[release["release_id"]]
        local_manifest = release["base"] / "MANIFEST.json"
        exact_sha = EXPECTED_MANIFEST_SHA256[release["release_id"]]
        if (
            sha256(local_manifest) != exact_sha
            or frozen.get("manifest_sha256") != exact_sha
            or frozen.get("evidence_tier") != EVIDENCE_TIER
            or frozen.get("tl1_status") != TIMESTAMP_TIER
            or frozen.get("include") != "EXPLORATORY_ONLY"
        ):
            raise L2StageError(f"frozen manifest mismatch: {release['release_id']}")
    query_sums = run_dir / "QUERY_SHA256SUMS.txt"
    if not query_sums.is_file():
        raise L2StageError("QUERY_SHA256SUMS.txt missing")
    expected_query_sha = None
    for line in query_sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith("queries/l2_hypothesis_stage.py"):
            expected_query_sha = parts[0]
    if expected_query_sha is None or expected_query_sha != sha256(Path(__file__).resolve()):
        raise L2StageError("L2 query source is absent from or differs from frozen query set")
    return manifest


def run(args: argparse.Namespace) -> dict:
    run_dir = args.run_dir.resolve()
    summary_path = run_dir / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json"
    if args.bootstrap_replicates < MIN_BOOTSTRAP_REPLICATES:
        raise L2StageError(f"bootstrap replicates must be >= {MIN_BOOTSTRAP_REPLICATES}")

    inputs = core.discover_release_inputs(args.cache_root.resolve())
    manifest = validate_run(run_dir, inputs)
    if summary_path.is_file():
        existing = json.loads(summary_path.read_text(encoding="utf-8"))
        if existing.get("schema_version") != SCHEMA_VERSION:
            raise L2StageError("existing L2 summary has incompatible schema")
        append_registry(run_dir, summary_path, existing)
        return existing
    design = preregistered_design(args.bootstrap_replicates)
    design_path = run_dir / "REPORT/tables/l2_preregistered_design.json"
    atomic_json(design_path, design)
    consumed_channels = {
        "l1", "l2", "dim_markets", "dim_events", "dim_series", "l2_gap_receipts"
    }
    consumed_bindings = sorted(
        (
            {key: item.get(key) for key in (
                "release_id", "key", "version_id", "sha256", "bytes", "channel"
            )}
            for item in inputs["object_bindings"] if item["channel"] in consumed_channels
        ),
        key=lambda item: (item["release_id"], item["key"]),
    )
    binding_path = run_dir / "DATA_INTEGRITY/L2_STAGE_OBJECT_BINDINGS.json"
    atomic_json(binding_path, {
        "schema_version": "sports-autoresearch-l2-stage-object-bindings-v1",
        "selection": "exact MANIFEST objects only",
        "objects": consumed_bindings,
    })

    receipts = {}
    receipt_bindings = []
    for path in inputs["l2_gap_receipts"]:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assessment = receipt_assessment(receipt)
        date = str(receipt.get("date"))
        if date in receipts:
            raise L2StageError(f"duplicate L2 receipt date: {date}")
        receipts[date] = assessment
        expected = next(
            item for item in inputs["object_bindings"]
            if item["channel"] == "l2_gap_receipts" and path == args.cache_root.resolve() / "releases" / item["release_id"] / item["key"]
        )
        actual_sha = sha256(path)
        if expected.get("sha256") and actual_sha != expected["sha256"]:
            raise L2StageError(f"sealed L2 receipt content hash mismatch: {date}")
        receipt_bindings.append({
            "date": date, "release_id": expected["release_id"],
            "key": expected["key"], "version_id": expected.get("version_id"),
            "sha256": actual_sha, "assessment": assessment,
        })
    clean_dates = sorted(date for date, item in receipts.items() if item["usable_for_continuous_replay"])

    import duckdb
    if duckdb.__version__ != "1.4.5":
        raise L2StageError("DuckDB runtime is not pinned 1.4.5")
    db_path = run_dir / "cache/l2_hypothesis.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    spool = StateSpool(run_dir / "cache/l2_top_states.spool.csv")
    try:
        core.configure(con, run_dir, args.memory_limit, args.threads, args.max_temp_directory_size)
        l1 = core.path_list(inputs["l1"])
        con.execute(f"""
          CREATE OR REPLACE VIEW l1_all AS
          SELECT date,local_recv_ts_us AS t_us,exchange_ts_us,recv_wall_ns,recv_mono_ns,
                 market_ticker,event_ticker,series_ticker,subcategory AS sport,
                 "group" AS league,yes_bid_e4,yes_bid_qty_e4,yes_ask_e4,
                 yes_ask_qty_e4,is_snapshot
          FROM read_parquet({l1},union_by_name=true,hive_partitioning=true)
          WHERE local_recv_ts_us IS NOT NULL
        """)
        core.materialize_dimensions(con, inputs)
        l2 = core.path_list(inputs["l2"])
        def batches() -> Iterator[Sequence[object]]:
            # One receive date per query bounds each external sort and enables
            # Hive partition pruning.  Only a 100k-row cursor batch crosses
            # into Python; the 28m/49m-row fact set is never materialized there.
            for clean_date in clean_dates:
                source_query = f"""
                  SELECT cast(l.date AS VARCHAR),l.local_recv_ts_us,l.recv_wall_ns,
                         l.recv_mono_ns,l.market_ticker,l.msg_type,l.side,l.price_e4,
                         l.delta_e4,l.yes_levels,l.no_levels,l.ws_sid,l.ws_seq,
                         u.root_event_id,u.sport,u.series_ticker,u.dim_effective_us
                  FROM read_parquet({l2},union_by_name=true,hive_partitioning=true) l
                  JOIN universe u ON u.date=l.date AND u.market_ticker=l.market_ticker
                  WHERE l.date=CAST({core.quote(clean_date)} AS DATE)
                    AND u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
                  ORDER BY l.recv_wall_ns NULLS LAST,l.recv_mono_ns NULLS LAST,
                           l.market_ticker,l.ws_sid NULLS LAST,l.ws_seq NULLS LAST,
                           CASE WHEN l.msg_type='snapshot' THEN 0 ELSE 1 END,
                           l.price_e4 NULLS FIRST,l.delta_e4 NULLS FIRST
                """
                cursor = con.execute(source_query)
                while True:
                    batch = cursor.fetchmany(100_000)
                    if not batch:
                        break
                    yield from batch

        replay = replay_rows(batches(), state_sink=spool)
        load_state_table(con, spool)
        annotated, threshold = annotate_retreats(replay["retreats"])
        treatments, controls, hf_diag = hfollow_populations(
            annotated, replay["control_windows"], threshold,
        )
        hfollow_pairs, hfollow_match = deterministic_match(
            treatments, controls, HFOLLOW_STRATA,
        )

        depletion_pairs = {}
        depletion_match = {}
        for window_us in REFILL_WINDOWS_US:
            dep_treatments, dep_controls = depletion_populations(
                replay["depletion_episodes"], window_us,
            )
            pairs, match = deterministic_match(
                dep_treatments, dep_controls, DEPLETION_STRATA,
            )
            depletion_pairs[int(window_us)] = pairs
            depletion_match[str(window_us)] = match

        candidates = build_candidate_legs(hfollow_pairs, depletion_pairs)
        compute_outcomes(con, candidates)
        asof_sentinels = {
            "current_state_after_decision_rows": core.scalar(
                con, "SELECT count(*) FROM l2_candidate_current WHERE current_state_ns>clock_ns"
            ),
            "future_state_after_target_rows": core.scalar(
                con, "SELECT count(*) FROM l2_outcome_legs WHERE future_state_ns>target_ns"
            ),
            "future_state_before_decision_rows": core.scalar(
                con, "SELECT count(*) FROM l2_outcome_legs WHERE future_state_ns<clock_ns"
            ),
        }
        horizon_rows = horizon_summaries(con)
        bootstraps = bootstrap_effects(con, args.bootstrap_replicates)

        tables = run_dir / "REPORT/tables"
        atomic_copy_csv(
            con,
            "SELECT * FROM l2_pair_effects ORDER BY analysis,window_us,horizon_us,date,pair_id",
            tables / "l2_pair_effects.csv",
        )
        atomic_copy_csv(
            con,
            "SELECT * FROM l2_candidate_legs ORDER BY analysis,window_us,pair_id,role",
            tables / "l2_matched_candidates.csv",
        )
        # The complete top-state stream remains a bounded, reproducible DuckDB
        # cache.  Canonical output contains only states actually used by a
        # candidate/outcome, avoiding a multi-GB duplicate of immutable facts.
        atomic_copy_csv(
            con,
            "SELECT * FROM l2_outcome_legs ORDER BY analysis,window_us,pair_id,role,horizon_us",
            tables / "l2_candidate_state_context.csv",
        )

        episode_fields = sorted({key for row in replay["depletion_episodes"] for key in row})
        episode_path = tables / "l2_depletion_lifecycle.csv"
        temporary = episode_path.with_name(episode_path.stem + ".tmp.csv")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=episode_fields)
            writer.writeheader()
            for row_number, row in enumerate(sorted(
                replay["depletion_episodes"],
                key=lambda r: (r["date"], r["depletion_ns"], r["depletion_id"]),
            ), 1):
                writer.writerow({key: row.get(key) for key in episode_fields})
                if row_number % 10_000 == 0 and handle.tell() > MAX_CANONICAL_TABLE_BYTES:
                    raise L2StageError(
                        "depletion lifecycle canonical table exceeded fail-closed cap"
                    )
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > MAX_CANONICAL_TABLE_BYTES:
            temporary.unlink()
            raise L2StageError("depletion lifecycle canonical table exceeds output cap")
        os.replace(temporary, episode_path)

        hf_primary = [r for r in horizon_rows if r["analysis"] == "HFOLLOW"]
        dep_primary = [r for r in horizon_rows if r["analysis"] == "DEPLETION_REFILL"]
        hf_games = max((int(r["n_treatment_games"]) for r in hf_primary), default=0)
        hf_days = max((int(r["n_day_blocks"]) for r in hf_primary), default=0)
        hf_outcome_pairs = max((int(r["n_pairs"]) for r in hf_primary), default=0)
        dep_games = max((int(r["n_treatment_games"]) for r in dep_primary), default=0)
        dep_days = max((int(r["n_day_blocks"]) for r in dep_primary), default=0)
        dep_outcome_pairs = max((int(r["n_pairs"]) for r in dep_primary), default=0)
        hf_leakage = (
            int(hf_diag["lookahead_sentinel_rows"]) > 0
            or any(int(value) > 0 for value in asof_sentinels.values())
        )
        future_refill_sentinel = sum(
            int(pair["treatment"]["clock_ns"]) < int(pair["treatment"].get("refill_ns") or 0)
            for pairs in depletion_pairs.values() for pair in pairs
        )
        snapshot_as_refill = sum(
            e.get("endpoint_reason") == "right_censored_snapshot_boundary" and e.get("event_observed")
            for e in replay["depletion_episodes"]
        )
        dep_leakage = (
            future_refill_sentinel > 0
            or snapshot_as_refill > 0
            or any(int(value) > 0 for value in asof_sentinels.values())
        )
        hf_status, hf_reason = hypothesis_status(hf_outcome_pairs, hf_games, hf_days, hf_leakage)
        dep_status, dep_reason = hypothesis_status(dep_outcome_pairs, dep_games, dep_days, dep_leakage)
        evaluation_quarantined = not receipts.get(EVAL_DATE, {}).get(
            "usable_for_continuous_replay", False
        )
        if evaluation_quarantined and not (hf_leakage or dep_leakage):
            blocker_text = ", ".join(receipts.get(EVAL_DATE, {}).get("blockers", []))
            hf_status = dep_status = "DATA_STARVED"
            hf_reason = dep_reason = (
                f"Zero clean evaluation day: {EVAL_DATE} is quarantined by its sealed "
                f"full-stream L2 receipt ({blocker_text}). No evaluation fact outcome was opened."
            )
        charts = make_charts(run_dir, horizon_rows, replay["depletion_episodes"])
        stage_status = (
            "INVALIDATED_BY_LEAKAGE"
            if "INVALIDATED_BY_LEAKAGE" in (hf_status, dep_status)
            else "DATA_STARVED"
            if hf_status == dep_status == "DATA_STARVED"
            else "COLLECT_MORE"
        )
        train_retreats = [row for row in annotated if row["date"] == TRAIN_DATE]
        train_episodes = [
            row for row in replay["depletion_episodes"] if row["date"] == TRAIN_DATE
        ]
        hfollow_training_counts = {
            "n_retreat_rows": len(train_retreats),
            "n_markets": len({row["market_ticker"] for row in train_retreats}),
            "n_games": len({row["root_event_id"] for row in train_retreats}),
            "n_day_blocks": len({row["date"] for row in train_retreats}),
            "removed_contracts_distribution": summary(
                row["removed_e4"] / 10_000.0 for row in train_retreats
            ),
        }
        depletion_training_counts = {
            "n_episode_rows": len(train_episodes),
            "n_markets": len({row["market_ticker"] for row in train_episodes}),
            "n_games": len({row["root_event_id"] for row in train_episodes}),
            "n_day_blocks": len({row["date"] for row in train_episodes}),
            "duration_us_distribution": summary(row.get("duration_us") for row in train_episodes),
            "removed_contracts_distribution": summary(
                row["removed_e4"] / 10_000.0 for row in train_episodes
            ),
        }

        summary_value = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "status": stage_status,
            "run_id": manifest["run_id"],
            "completed_at_utc": utc_now(),
            "banner": {
                "data_evidence": EVIDENCE_TIER,
                "timestamp_discipline": TIMESTAMP_TIER,
                "experiment_split": "EXPLORATORY_ONLY",
                "artifact_status": "DIAGNOSTIC_ONLY",
                "authorization": "NOT A LIVE-TRADING AUTHORIZATION",
            },
            "data_binding": {
                "release_ids": list(EXPECTED_RELEASE_IDS),
                "train_date": TRAIN_DATE,
                "evaluation_date": EVAL_DATE,
                "clean_replay_dates": clean_dates,
                "clean_evaluation_day_count": int(EVAL_DATE in clean_dates),
                "evaluation_fact_outcome_opened": EVAL_DATE in clean_dates,
                "receipt_bindings": receipt_bindings,
                "query_sha256": sha256(Path(__file__).resolve()),
                "preregistered_design": str(design_path.relative_to(run_dir)),
                "preregistered_design_sha256": sha256(design_path),
                "consumed_object_bindings": str(binding_path.relative_to(run_dir)),
                "consumed_object_bindings_sha256": sha256(binding_path),
                "consumed_object_count": len(consumed_bindings),
            },
            "receipt_quality": receipts,
            "replay_qc": replay["qc"],
            "resource_contract": {
                "duckdb_memory_limit": args.memory_limit,
                "duckdb_threads": args.threads,
                "duckdb_max_temp_directory_size": args.max_temp_directory_size,
                "receive_date_partitioned_scan": True,
                "python_fetch_batch_rows": 100_000,
                "state_spool_max_bytes": MAX_STATE_SPOOL_BYTES,
                "sparse_events_max_per_class": MAX_SPARSE_EVENTS_PER_CLASS,
                "canonical_table_max_bytes_each": MAX_CANONICAL_TABLE_BYTES,
                "silent_truncation_allowed": False,
            },
            "hypotheses": {
                "C1-HFOLLOW-RETREAT-01": {
                    "hypothesis_id": "C1-HFOLLOW-RETREAT-01",
                    "hypothesis_status": hf_status,
                    "artifact_status": "DIAGNOSTIC_ONLY",
                    "execution_status": (
                        "DATA_SCREENED_EVALUATION_QUARANTINED" if evaluation_quarantined
                        else "COMPLETED_MATCHED_DIAGNOSTIC" if hf_outcome_pairs
                        else "DATA_SCREENED_NO_MATCHED_OUTCOME"
                    ),
                    "status_reason": hf_reason,
                    "sample_counts": {
                        **hfollow_match,
                        "training_tier1": hfollow_training_counts,
                        "outcome_pairs_max_horizon_cell": hf_outcome_pairs,
                        "n_treatment_games": hf_games,
                        "n_day_blocks": hf_days,
                        "n_clean_evaluation_day_blocks": int(EVAL_DATE in clean_dates),
                        "n_markets": len({r["market_ticker"] for r in treatments}),
                        "required_additional_games_at_least": max(0, MIN_RELEVANT_GAMES - hf_games),
                        "required_additional_day_blocks_at_least": max(0, MIN_DAY_BLOCKS - hf_days),
                    },
                    "trained_coordination_threshold_distinct_markets": threshold,
                    "multiplicity_status": (
                        "NOT_COMPUTABLE_ZERO_CLEAN_EVALUATION_DAY"
                        if evaluation_quarantined else "FIXED_HORIZON_FAMILY_DIAGNOSTIC_ONLY"
                    ),
                    "horizon_results": hf_primary,
                    "bootstrap": {
                        key: value for key, value in bootstraps.items()
                        if key.startswith("HFOLLOW|")
                    },
                    "negative_controls": {
                        "lookahead_component_sentinel_rows": hf_diag["lookahead_sentinel_rows"],
                        "strict_asof_sentinels": asof_sentinels,
                        "future_shift_horizon_results": [
                            r for r in horizon_rows if r["analysis"] == "HFOLLOW_FUTURE_SHIFT_CONTROL"
                        ],
                        "rotated_root_coordination_count": rotated_root_coordination_count(annotated, threshold),
                        "negative_control_interpretation": "Controls are leakage diagnostics only; the one-day split cannot validate their null distribution.",
                        "execution_status": (
                            "NOT_OPENED_EVALUATION_QUARANTINED"
                            if evaluation_quarantined else "EXECUTED_DIAGNOSTIC_ONLY"
                        ),
                    },
                    "limitations": [
                        "Evaluation has exactly one calendar-day block; 1000+ day-block draws are identical at the block level.",
                        "Root IDs are causal-time-gated provisional matchup/time heuristics, not canonical event IDs.",
                        "L2 is a targeted subset; results do not generalize to all Sports markets.",
                        "No participant/professional identity is observed or claimed.",
                        "This is a book-response test, not fill/PnL economics.",
                    ],
                },
                "C1-DEPLETION-REFILL-01": {
                    "hypothesis_id": "C1-DEPLETION-REFILL-01",
                    "hypothesis_status": dep_status,
                    "artifact_status": "DIAGNOSTIC_ONLY",
                    "execution_status": (
                        "DATA_SCREENED_EVALUATION_QUARANTINED" if evaluation_quarantined
                        else "COMPLETED_MATCHED_DIAGNOSTIC" if dep_outcome_pairs
                        else "DATA_SCREENED_NO_MATCHED_OUTCOME"
                    ),
                    "status_reason": dep_reason,
                    "sample_counts": {
                        "episodes": len(replay["depletion_episodes"]),
                        "training_tier1": depletion_training_counts,
                        "matching_by_refill_window_us": depletion_match,
                        "outcome_pairs_max_horizon_cell": dep_outcome_pairs,
                        "n_treatment_games": dep_games,
                        "n_day_blocks": dep_days,
                        "n_clean_evaluation_day_blocks": int(EVAL_DATE in clean_dates),
                        "n_markets": len({e["market_ticker"] for e in replay["depletion_episodes"] if e["date"] == EVAL_DATE}),
                        "required_additional_games_at_least": max(0, MIN_RELEVANT_GAMES - dep_games),
                        "required_additional_day_blocks_at_least": max(0, MIN_DAY_BLOCKS - dep_days),
                    },
                    "horizon_results": dep_primary,
                    "multiplicity_status": (
                        "NOT_COMPUTABLE_ZERO_CLEAN_EVALUATION_DAY"
                        if evaluation_quarantined else "FIXED_HORIZON_FAMILY_DIAGNOSTIC_ONLY"
                    ),
                    "bootstrap": {
                        key: value for key, value in bootstraps.items()
                        if key.startswith("DEPLETION_REFILL|")
                    },
                    "negative_controls": {
                        "future_refill_decision_sentinel_rows": future_refill_sentinel,
                        "snapshot_boundary_counted_as_refill_rows": snapshot_as_refill,
                        "far_from_touch_qualifying_depletion_rows": replay["qc"].get(
                            "far_from_touch_qualifying_depletion_rows", 0
                        ),
                        "strict_asof_sentinels": asof_sentinels,
                        "side_flip_identity": "opposite-side adverse markout is the exact negative by construction; no sign-selected trial was opened",
                        "execution_status": (
                            "NOT_OPENED_EVALUATION_QUARANTINED"
                            if evaluation_quarantined else "EXECUTED_DIAGNOSTIC_ONLY"
                        ),
                    },
                    "censoring": {
                        reason: sum(e.get("endpoint_reason") == reason for e in replay["depletion_episodes"])
                        for reason in sorted({str(e.get("endpoint_reason")) for e in replay["depletion_episodes"]})
                    },
                    "limitations": [
                        "Evaluation has exactly one calendar-day block; no inferential rejection is permitted.",
                        "Snapshot reset, invalid book, repeated depletion, day end and the 1s analysis horizon right-censor episodes.",
                        "Refill is displayed-depth resilience, not an own-order fill probability.",
                        "No fee, latency, queue or net-PnL claim is made.",
                    ],
                },
            },
            "artifacts": {
                "design": str(design_path.relative_to(run_dir)),
                "pair_effects": "REPORT/tables/l2_pair_effects.csv",
                "matched_candidates": "REPORT/tables/l2_matched_candidates.csv",
                "depletion_lifecycle": "REPORT/tables/l2_depletion_lifecycle.csv",
                "candidate_state_context": "REPORT/tables/l2_candidate_state_context.csv",
                "bounded_replay_cache": "cache/l2_hypothesis.duckdb",
                "charts": charts,
            },
            "limitations": [
                "Both releases are SEALED_DEGRADED_EVIDENCE and prior-exposed.",
                "Only two dates exist (one train, one evaluation); this stage cannot reject or promote either hypothesis.",
                "No Mode 2, validation, historical confirmation, verdict, promotion, live-ready or live-trading claim is authorized.",
                "Per-market ws_seq jumps were never used to infer packet loss.",
            ],
        }
        atomic_json(summary_path, summary_value)
        md_path = run_dir / "REPORT/L2_HYPOTHESIS_STAGE.md"
        atomic_text(md_path, "\n".join([
            "# L2 hypothesis stage", "", f"> **{BANNER}**", "",
            f"- C1-HFOLLOW-RETREAT-01: `{hf_status}` — {hf_reason}",
            f"- C1-DEPLETION-REFILL-01: `{dep_status}` — {dep_reason}", "",
            "The replay is snapshot-aware. Open depletion episodes are right-censored at a",
            "snapshot boundary; snapshots never count as refill. Sequence quality comes only",
            "from the sealed full-stream `quality/l2_gaps.json` receipts. Per-market `ws_seq`",
            "projections were not used to infer loss.", "",
            "The evaluation split contains one day. Its calendar-day bootstrap is degenerate",
            "even with at least 1,000 deterministic replicates, so neither absence nor a positive",
            "effect can support rejection, promotion, a verdict, or live authorization.", "",
        ]))
        summary_value["artifacts"]["report"] = str(md_path.relative_to(run_dir))
        atomic_json(summary_path, summary_value)
        append_registry(run_dir, summary_path, summary_value)
        return summary_value
    finally:
        spool.close()
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--max-temp-directory-size", default="120GB")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    args = parser.parse_args(argv)
    result = run(args)
    statuses = {
        key: value["hypothesis_status"] for key, value in result["hypotheses"].items()
    }
    print("L2_HYPOTHESIS_STAGE_COMPLETE", json.dumps(statuses, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
