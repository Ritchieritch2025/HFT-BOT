#!/usr/bin/env python3
"""Bounded, sequence-valid L2/SNBD research for exact Deep03 V3 inputs.

This module intentionally stops at descriptive market-microstructure evidence.
It reconstructs only snapshot-anchored books, derives topology/depth/imbalance/
microprice states, and records retreat/depletion/refill lifecycles plus matched
control diagnostics.  It does not simulate fills or make a PnL claim.

Sequence integrity has two distinct authorities:

* the sealed full-stream ``l2_gaps.json`` receipt is the only packet-loss/gap
  authority (a per-market projection is not contiguous and must not be used to
  infer missing frames); and
* within a market, ``ws_sid`` identifies an epoch and ``ws_seq`` supplies the
  deterministic order.  A delta in a new epoch before a snapshot, a sequence
  regression, or malformed/negative depth invalidates that market until a
  later snapshot re-anchors it.
"""

from __future__ import annotations

import hashlib
import json
import math
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from deep03_v3_methods import (
    BoundedCheckpointStore,
    _canonical_json_bytes,
    _columns,
    _expr,
    _relation_sql,
    _require_row_conservation,
    bounded_source_binding,
    path_list,
    quote,
)


SCHEMA_VERSION = "deep03-v3-l2-snbd-v2"
L2_SCOPE_DATES = tuple(f"2026-07-{day:02d}" for day in range(10, 18))
L2_CAPTURE_DATES = tuple(f"2026-07-{day:02d}" for day in range(12, 18))
L2_ABSENT_DATES = ("2026-07-10", "2026-07-11")
L2_ANALYSIS_DATES = ("2026-07-12", "2026-07-15", "2026-07-17")
L2_KNOWN_EXCLUDED_DATES = {
    "2026-07-13": "FULL_STREAM_SEQUENCE_GAP",
    "2026-07-14": "SEVERE_PARSE_AND_FRAME_LOSS",
    "2026-07-16": "GAP_AND_EPOCH_MARKERS",
}
MIN_TOUCH_QTY_E4 = 10_000
MIN_DEPLETION_FRACTION = 0.50
REFILL_FRACTION = 0.80
EPISODE_HORIZON_NS = 1_000_000_000
QUIET_ANCHOR_LOOKBACK_NS = 1_000_000_000
MIN_TOP3_RETREAT_QTY_E4 = 10_000
MIN_TOP3_RETREAT_FRACTION = 0.50
CONTROL_SAMPLE_MODULUS = 16
MATCH_WINDOW_NS = 300_000_000_000
CONTROL_TIME_BUCKET_NS = 60_000_000_000
MAX_CONTROLS_PER_STRATUM_BUCKET = 8
MAX_MATCH_CANDIDATES_PER_EPISODE = 64
HAZARD_INTERVAL_NS = 100_000_000
REPLAY_FETCH_ROWS = 50_000


class L2ResearchError(RuntimeError):
    """Fail-closed exact-input or replay-integrity error."""


def _stable_id(prefix: str, *values: object) -> str:
    payload = "\x1f".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _logit_e4(price_e4: int) -> float:
    price = max(1, min(9999, int(price_e4)))
    return math.log(price / (10000.0 - price))


def _stable_control_sample(market: str, clock_ns: int) -> bool:
    return (zlib.crc32(market.encode("utf-8")) ^ int(clock_ns)) % (
        CONTROL_SAMPLE_MODULUS
    ) == 0


def parse_levels(value: object) -> dict[int, int]:
    """Strictly parse one snapshot side in fixed-point E4 units."""
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or not isinstance(value, (list, tuple)):
        raise ValueError("snapshot side is not a sequence")
    levels: dict[int, int] = {}
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("snapshot level is not [price,quantity]")
        price, quantity = int(item[0]), int(item[1])
        if not 1 <= price <= 9999 or quantity < 0:
            raise ValueError("snapshot level is outside E4 bounds")
        if price in levels:
            raise ValueError("snapshot contains a duplicate price")
        if quantity:
            levels[price] = quantity
    return levels


@dataclass
class L2Book:
    yes: dict[int, int] = field(default_factory=dict)
    no: dict[int, int] = field(default_factory=dict)
    valid: bool = False
    ws_sid: int | None = None
    last_ws_seq: int | None = None
    snapshot_epoch: int = 0

    def invalidate(self) -> None:
        self.yes.clear()
        self.no.clear()
        self.valid = False

    def snapshot(
        self, yes_levels: object, no_levels: object, *, ws_sid: int, ws_seq: int
    ) -> None:
        self.yes = parse_levels(yes_levels)
        self.no = parse_levels(no_levels)
        self.valid = True
        self.ws_sid = int(ws_sid)
        self.last_ws_seq = int(ws_seq)
        self.snapshot_epoch += 1

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
        return sum(
            quantity
            for price, quantity in self.side_book(side).items()
            if price >= raw_price
        )

    def depth_top3(self, side: str) -> int:
        return self._top_k_depth(self.side_book(side))

    @staticmethod
    def _top_k_depth(levels: Mapping[int, int], k: int = 3) -> int:
        return sum(quantity for _price, quantity in sorted(
            levels.items(), reverse=True
        )[:k])

    def state(self) -> dict[str, Any]:
        if not self.valid:
            return {
                "book_valid": False,
                "topology": "INVALID_EPOCH",
                "snapshot_epoch": self.snapshot_epoch,
            }
        bid, bid_quantity = self.best_raw("yes")
        no_bid, ask_quantity = self.best_raw("no")
        ask = None if no_bid is None else 10000 - no_bid
        bid_depth3 = self._top_k_depth(self.yes)
        ask_depth3 = self._top_k_depth(self.no)
        total_depth3 = bid_depth3 + ask_depth3
        result: dict[str, Any] = {
            "book_valid": True,
            "snapshot_epoch": self.snapshot_epoch,
            "bid_e4": bid,
            "bid_qty_e4": bid_quantity,
            "ask_e4": ask,
            "ask_qty_e4": ask_quantity,
            "bid_depth3_e4": bid_depth3,
            "ask_depth3_e4": ask_depth3,
            "bid_levels": len(self.yes),
            "ask_levels": len(self.no),
            "imbalance_depth3": (
                (bid_depth3 - ask_depth3) / total_depth3 if total_depth3 else None
            ),
        }
        if bid is None and ask is None:
            result["topology"] = "EMPTY"
        elif bid is None or ask is None:
            result["topology"] = "ONE_SIDED"
        elif not (0 < bid < ask < 10000):
            result["topology"] = "CROSSED_OR_LOCKED"
        else:
            result["topology"] = "TWO_SIDED"
            result["mid_e4"] = (bid + ask) / 2.0
            result["spread_e4"] = ask - bid
            result["mid_logodds"] = (_logit_e4(bid) + _logit_e4(ask)) / 2.0
            result["spread_logodds"] = _logit_e4(ask) - _logit_e4(bid)
            touch_total = bid_quantity + ask_quantity
            result["microprice_e4"] = (
                (ask * bid_quantity + bid * ask_quantity) / touch_total
                if touch_total
                else None
            )
        return result

    def apply_delta(
        self, *, side: str, price_e4: int, delta_e4: int,
        ws_sid: int, ws_seq: int,
    ) -> dict[str, Any]:
        """Apply one market-projected delta without inventing packet gaps."""
        if self.ws_sid is not None and int(ws_sid) != self.ws_sid:
            self.invalidate()
            self.ws_sid = int(ws_sid)
            self.last_ws_seq = int(ws_seq)
            return {
                "applied": False,
                "reason": "epoch_change_without_snapshot",
                "invalidated": True,
            }
        if self.last_ws_seq is not None and int(ws_seq) <= self.last_ws_seq:
            self.invalidate()
            return {
                "applied": False,
                "reason": "sequence_regression",
                "invalidated": True,
            }
        # A forward jump in a per-market projection is normal: other markets
        # share the sid stream.  The sealed full-stream receipt owns gap truth.
        self.ws_sid = int(ws_sid)
        self.last_ws_seq = int(ws_seq)
        if not self.valid:
            return {"applied": False, "reason": "delta_before_snapshot"}
        if side not in {"yes", "no"} or not 1 <= int(price_e4) <= 9999:
            self.invalidate()
            return {
                "applied": False,
                "reason": "malformed_delta",
                "invalidated": True,
            }
        if not int(delta_e4):
            self.invalidate()
            return {
                "applied": False,
                "reason": "zero_delta",
                "invalidated": True,
            }
        before = self.state()
        pre_side_depth3 = self.depth_top3(side)
        pre_touch_price, pre_touch_quantity = self.best_raw(side)
        levels = self.side_book(side)
        price = int(price_e4)
        current = levels.get(price, 0)
        nxt = current + int(delta_e4)
        if nxt < 0:
            self.invalidate()
            return {
                "applied": False,
                "reason": "negative_result",
                "invalidated": True,
            }
        if nxt:
            levels[price] = nxt
        else:
            levels.pop(price, None)
        after = self.state()
        post_side_depth3 = self.depth_top3(side)
        top3_removed = max(0, pre_side_depth3 - post_side_depth3)
        top3_retreat_fraction = (
            top3_removed / pre_side_depth3 if pre_side_depth3 else 0.0
        )
        top3_keys = (
            "topology", "bid_e4", "ask_e4", "bid_qty_e4", "ask_qty_e4",
            "bid_depth3_e4", "ask_depth3_e4", "spread_e4",
            "imbalance_depth3", "microprice_e4",
        )
        remaining = levels.get(pre_touch_price, 0) if pre_touch_price else 0
        removed = max(0, pre_touch_quantity - remaining)
        touch_depletion = bool(
            int(delta_e4) < 0
            and pre_touch_price is not None
            and price == pre_touch_price
            and pre_touch_quantity >= MIN_TOUCH_QTY_E4
            and removed >= MIN_TOUCH_QTY_E4
            and removed / pre_touch_quantity >= MIN_DEPLETION_FRACTION
        )
        return {
            "applied": True,
            "before": before,
            "after": after,
            "pre_touch_price_e4": pre_touch_price,
            "pre_touch_qty_e4": pre_touch_quantity,
            "removed_e4": removed,
            "depletion_fraction": (
                removed / pre_touch_quantity if pre_touch_quantity else 0.0
            ),
            "touch_depletion": touch_depletion,
            "top_changed": before != after,
            "top3_changed": any(
                before.get(key) != after.get(key) for key in top3_keys
            ),
            "pre_side_depth3_e4": pre_side_depth3,
            "post_side_depth3_e4": post_side_depth3,
            "top3_removed_e4": top3_removed,
            "top3_retreat_fraction": top3_retreat_fraction,
            "top3_retreat": bool(
                int(delta_e4) < 0
                and top3_removed >= MIN_TOP3_RETREAT_QTY_E4
                and top3_retreat_fraction >= MIN_TOP3_RETREAT_FRACTION
            ),
        }


def assess_l2_quality_receipt(receipt: Mapping[str, object]) -> dict[str, Any]:
    """Assess the sealed complete-stream receipt; any gap blocks the date."""
    markers = receipt.get("recorder_markers") or {}
    if not isinstance(markers, Mapping):
        markers = {}
    blockers: list[str] = []
    for key in (
        "parse_errors",
        "seq_gap_events",
        "seq_missed_total",
        "seq_regressions",
        "markers_lost_frames",
    ):
        value = receipt.get(key, 0)
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            blockers.append(f"{key}=INVALID")
        else:
            if count:
                blockers.append(f"{key}={count}")
    for key, value in sorted(markers.items(), key=lambda item: str(item[0])):
        if str(key).lower() in {"gap", "loss", "epoch_change"}:
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                count = 1
            if count:
                blockers.append(f"recorder_marker:{key}={count}")
    if bool(receipt.get("no_l2_files")):
        blockers.append("no_l2_files=true")
    try:
        lines = int(receipt.get("lines") or 0)
    except (TypeError, ValueError):
        lines = 0
    if lines <= 0:
        blockers.append("lines<=0")
    return {
        "date": receipt.get("date"),
        "state": "PASS" if not blockers else "REFUSED",
        "usable_for_replay": not blockers,
        "blockers": blockers,
        "lines": lines,
        "sequence_authority": "sealed full-stream l2_gaps.json",
        "per_market_forward_ws_seq_gap_inference_used": False,
    }


REPLAY_COLUMNS = (
    "date", "t_us", "recv_wall_ns", "recv_mono_ns", "market_ticker",
    "event_proxy", "sport", "family", "ws_sid", "ws_seq", "msg_type",
    "side", "price_e4", "delta_e4", "classification", "snapshot_epoch",
    "book_valid", "topology", "bid_e4", "bid_qty_e4", "ask_e4",
    "ask_qty_e4", "bid_depth3_e4", "ask_depth3_e4", "bid_levels",
    "ask_levels", "mid_e4", "microprice_e4", "spread_e4",
    "mid_logodds", "spread_logodds", "imbalance_depth3", "top_changed",
    "touch_depletion", "top3_retreat", "pre_topology", "pre_spread_e4",
    "pre_imbalance_depth3", "pre_side_depth3_e4",
    "pre_opposite_depth3_e4", "post_side_depth3_e4", "top3_removed_e4",
    "top3_retreat_fraction",
    "control_candidate", "control_quiet_lookback_ns",
)

EPISODE_COLUMNS = (
    "episode_id", "date", "market_ticker", "event_proxy", "sport", "family",
    "side", "depletion_ns", "observation_end_ns", "duration_us",
    "endpoint_reason", "event_observed", "refill_ns", "refill_fraction",
    "original_touch_price_e4", "pre_touch_qty_e4", "removed_e4",
    "depletion_fraction", "post_depletion_depth_e4", "snapshot_epoch",
    "covariate_timing", "covariate_clock_ns", "pre_topology",
    "pre_spread_e4", "pre_imbalance_depth3", "pre_side_depth3_e4",
    "pre_opposite_depth3_e4", "top3_removed_e4",
    "top3_retreat_fraction", "top3_retreat",
)


def _row_with_state(base: dict[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {column: (base.get(column) if column in base else state.get(column))
            for column in REPLAY_COLUMNS}


class L2ReplayEngine:
    """Replay one date+market-hash partition with bounded market state."""

    def __init__(self) -> None:
        self.books: dict[str, L2Book] = {}
        self.open_episodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_top3_change_ns: dict[str, int] = {}
        self.last_clock: dict[str, int] = {}
        self.qc: dict[str, int] = defaultdict(int)

    def _finalize(
        self, episode: dict[str, Any], end_ns: int, reason: str
    ) -> dict[str, Any]:
        end = max(int(episode["depletion_ns"]), int(end_ns))
        result = dict(episode)
        result["observation_end_ns"] = end
        result["duration_us"] = (end - int(episode["depletion_ns"])) // 1000
        result["endpoint_reason"] = reason
        result["event_observed"] = result.get("refill_ns") is not None
        return {column: result.get(column) for column in EPISODE_COLUMNS}

    def _close_market_episodes(
        self, market: str, clock_ns: int, reason: str
    ) -> list[dict[str, Any]]:
        completed = []
        for side in ("yes", "no"):
            key = (market, side)
            episode = self.open_episodes.pop(key, None)
            if episode is not None:
                completed.append(self._finalize(episode, clock_ns, reason))
        return completed

    def _expire(self, market: str, clock_ns: int) -> list[dict[str, Any]]:
        completed = []
        for side in ("yes", "no"):
            key = (market, side)
            episode = self.open_episodes.get(key)
            if episode is None:
                continue
            boundary = int(episode["depletion_ns"]) + EPISODE_HORIZON_NS
            # Closed endpoint: a refill stamped exactly at one second is an
            # observed event.  Only a later row proves the endpoint passed.
            if clock_ns > boundary:
                completed.append(self._finalize(
                    self.open_episodes.pop(key), boundary,
                    "right_censored_1s_horizon",
                ))
        return completed

    def process(self, raw: Sequence[object]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        (
            date_value, t_us, recv_wall_ns, recv_mono_ns, market_value,
            event_proxy, sport, family, msg_type_value, side_value, price_e4,
            delta_e4, yes_levels, no_levels, ws_sid, ws_seq,
        ) = raw
        self.qc["source_rows"] += 1
        date = str(date_value)
        market = str(market_value or "")
        base: dict[str, Any] = {
            "date": date,
            "t_us": t_us,
            "recv_wall_ns": recv_wall_ns,
            "recv_mono_ns": recv_mono_ns,
            "market_ticker": market,
            # Missing event identity stays missing.  Falling back to market
            # ticker would fabricate "different event" balance in matching.
            "event_proxy": str(event_proxy or ""),
            "sport": str(sport or "_UNKNOWN"),
            "family": str(family or "_UNKNOWN"),
            "ws_sid": ws_sid,
            "ws_seq": ws_seq,
            "msg_type": str(msg_type_value or "").lower(),
            "side": str(side_value or "").lower() or None,
            "price_e4": price_e4,
            "delta_e4": delta_e4,
            "top_changed": False,
            "touch_depletion": False,
            "top3_retreat": False,
            "control_candidate": False,
            "control_quiet_lookback_ns": None,
        }
        if (
            not market
            or t_us is None
            or recv_wall_ns is None
            or recv_mono_ns is None
            or ws_sid is None
            or ws_seq is None
        ):
            base["classification"] = "REJECTED_MISSING_REPLAY_KEY"
            self.qc["rejected_missing_replay_key"] += 1
            completed: list[dict[str, Any]] = []
            book = self.books.get(market) if market else None
            if book is not None:
                book.invalidate()
                boundary = (
                    int(recv_wall_ns)
                    if recv_wall_ns is not None
                    else self.last_clock.get(market, 0)
                )
                completed.extend(self._close_market_episodes(
                    market, boundary, "right_censored_missing_replay_key"
                ))
            return _row_with_state(base, book.state() if book else {}), completed
        clock_ns = int(recv_wall_ns)
        completed = self._expire(market, clock_ns)
        self.last_clock[market] = max(clock_ns, self.last_clock.get(market, clock_ns))
        book = self.books.setdefault(market, L2Book())
        msg_type = base["msg_type"]
        if msg_type == "snapshot":
            completed.extend(self._close_market_episodes(
                market, clock_ns, "right_censored_snapshot_boundary"
            ))
            try:
                book.snapshot(
                    yes_levels, no_levels, ws_sid=int(ws_sid), ws_seq=int(ws_seq)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                book.invalidate()
                base["classification"] = "REJECTED_INVALID_SNAPSHOT"
                self.qc["rejected_invalid_snapshot"] += 1
                return _row_with_state(base, book.state()), completed
            base["classification"] = "SNAPSHOT_APPLIED"
            base["top_changed"] = True
            self.last_top3_change_ns[market] = clock_ns
            self.qc["snapshots_applied"] += 1
            return _row_with_state(base, book.state()), completed
        if msg_type != "delta":
            base["classification"] = "REJECTED_INVALID_MESSAGE_TYPE"
            self.qc["rejected_invalid_message_type"] += 1
            return _row_with_state(base, book.state()), completed
        try:
            result = book.apply_delta(
                side=str(side_value or "").lower(),
                price_e4=int(price_e4 or 0),
                delta_e4=int(delta_e4 or 0),
                ws_sid=int(ws_sid),
                ws_seq=int(ws_seq),
            )
        except (TypeError, ValueError):
            book.invalidate()
            result = {
                "applied": False,
                "reason": "malformed_delta",
                "invalidated": True,
            }
        if not result["applied"]:
            reason = str(result["reason"])
            base["classification"] = "REJECTED_" + reason.upper()
            self.qc["rejected_" + reason] += 1
            if result.get("invalidated"):
                completed.extend(self._close_market_episodes(
                    market, clock_ns, "right_censored_invalid_epoch"
                ))
            return _row_with_state(base, book.state()), completed

        self.qc["deltas_applied"] += 1
        base["classification"] = "DELTA_APPLIED"
        state = result["after"]
        pre_state = result["before"]
        side = str(side_value).lower()
        base["top_changed"] = bool(result["top_changed"])
        base["touch_depletion"] = bool(result["touch_depletion"])
        base["top3_retreat"] = bool(result["top3_retreat"])
        base["pre_topology"] = pre_state.get("topology")
        base["pre_spread_e4"] = pre_state.get("spread_e4")
        base["pre_imbalance_depth3"] = pre_state.get("imbalance_depth3")
        base["pre_side_depth3_e4"] = int(result["pre_side_depth3_e4"])
        base["pre_opposite_depth3_e4"] = (
            pre_state.get("ask_depth3_e4")
            if side == "yes" else pre_state.get("bid_depth3_e4")
        )
        base["post_side_depth3_e4"] = int(result["post_side_depth3_e4"])
        base["top3_removed_e4"] = int(result["top3_removed_e4"])
        base["top3_retreat_fraction"] = float(result["top3_retreat_fraction"])
        key = (market, side)
        existing = self.open_episodes.get(key)
        if existing is not None:
            recovered = (
                book.depth_at_or_better(side, existing["original_touch_price_e4"])
                - existing["post_depletion_depth_e4"]
            )
            refill_fraction = max(0.0, recovered / existing["removed_e4"])
            if refill_fraction >= REFILL_FRACTION:
                existing["refill_ns"] = clock_ns
                existing["refill_fraction"] = refill_fraction
                completed.append(self._finalize(
                    self.open_episodes.pop(key), clock_ns, "refill_observed"
                ))
                self.qc["refills_observed"] += 1

        if result["touch_depletion"]:
            previous = self.open_episodes.pop(key, None)
            if previous is not None:
                completed.append(self._finalize(
                    previous, clock_ns, "right_censored_repeated_depletion"
                ))
            episode_id = _stable_id(
                "DEP", date, market, side, clock_ns, ws_sid, ws_seq
            )
            self.open_episodes[key] = {
                "episode_id": episode_id,
                "date": date,
                "market_ticker": market,
                "event_proxy": base["event_proxy"],
                "sport": base["sport"],
                "family": base["family"],
                "side": side,
                "depletion_ns": clock_ns,
                "refill_ns": None,
                "refill_fraction": None,
                "original_touch_price_e4": int(result["pre_touch_price_e4"]),
                "pre_touch_qty_e4": int(result["pre_touch_qty_e4"]),
                "removed_e4": int(result["removed_e4"]),
                "depletion_fraction": float(result["depletion_fraction"]),
                "post_depletion_depth_e4": book.depth_at_or_better(
                    side, int(result["pre_touch_price_e4"])
                ),
                "snapshot_epoch": book.snapshot_epoch,
                "covariate_timing": "PRE_DEPLETION_STATE",
                "covariate_clock_ns": clock_ns,
                "pre_topology": pre_state.get("topology"),
                "pre_spread_e4": pre_state.get("spread_e4"),
                "pre_imbalance_depth3": pre_state.get("imbalance_depth3"),
                "pre_side_depth3_e4": (
                    pre_state.get("bid_depth3_e4")
                    if side == "yes" else pre_state.get("ask_depth3_e4")
                ),
                "pre_opposite_depth3_e4": (
                    pre_state.get("ask_depth3_e4")
                    if side == "yes" else pre_state.get("bid_depth3_e4")
                ),
                "top3_removed_e4": int(result["top3_removed_e4"]),
                "top3_retreat_fraction": float(result["top3_retreat_fraction"]),
                "top3_retreat": bool(result["top3_retreat"]),
            }
            self.qc["touch_depletions"] += 1
        else:
            last_change = self.last_top3_change_ns.get(market)
            quiet_lookback = (
                clock_ns - last_change if last_change is not None else None
            )
            if (
                not result["top3_changed"]
                and state.get("topology") == "TWO_SIDED"
                and key not in self.open_episodes
                and quiet_lookback is not None
                and quiet_lookback >= QUIET_ANCHOR_LOOKBACK_NS
                and _stable_control_sample(market, clock_ns)
            ):
                base["control_candidate"] = True
                base["control_quiet_lookback_ns"] = quiet_lookback
                self.qc["quiet_control_candidates"] += 1
        if result["top3_changed"]:
            self.last_top3_change_ns[market] = clock_ns
        return _row_with_state(base, state), completed

    def finish(self, observation_end_ns: int | None = None) -> list[dict[str, Any]]:
        """Close remaining episodes at a proven full-stream observation end.

        Production supplies the exact date-wide last Sports L2 receive clock.
        That mirrors the inherited clean-stream semantics: while the sealed
        subscription stream remains live, absence of a delta for one market is
        evidence that its displayed depth did not change.  Fixture callers may
        omit it and receive the more conservative per-market boundary.
        """
        completed = []
        for key, episode in list(self.open_episodes.items()):
            market, _side = key
            last = self.last_clock.get(market, int(episode["depletion_ns"]))
            if observation_end_ns is not None:
                last = max(last, int(observation_end_ns))
            horizon = int(episode["depletion_ns"]) + EPISODE_HORIZON_NS
            reason = (
                "right_censored_1s_horizon"
                if last >= horizon
                else "right_censored_date_end"
            )
            completed.append(self._finalize(
                episode, min(last, horizon), reason
            ))
            del self.open_episodes[key]
        return completed


def replay_rows(rows: Iterable[Sequence[object]]) -> dict[str, Any]:
    """Pure fixture-facing wrapper around the production replay engine."""
    engine = L2ReplayEngine()
    replay: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    for row in rows:
        replay_row, finished = engine.process(row)
        replay.append(replay_row)
        episodes.extend(finished)
    episodes.extend(engine.finish())
    engine.qc["replay_rows"] = len(replay)
    engine.qc["episodes"] = len(episodes)
    _require_row_conservation(
        label="l2_source_to_replay",
        observed=len(replay),
        expected=int(engine.qc["source_rows"]),
        context="fixture",
    )
    return {"replay_rows": replay, "episodes": episodes, "qc": dict(engine.qc)}


REPLAY_TYPES = {
    "date": "VARCHAR",
    "t_us": "BIGINT",
    "recv_wall_ns": "BIGINT",
    "recv_mono_ns": "BIGINT",
    "market_ticker": "VARCHAR",
    "event_proxy": "VARCHAR",
    "sport": "VARCHAR",
    "family": "VARCHAR",
    "ws_sid": "BIGINT",
    "ws_seq": "BIGINT",
    "msg_type": "VARCHAR",
    "side": "VARCHAR",
    "price_e4": "BIGINT",
    "delta_e4": "BIGINT",
    "classification": "VARCHAR",
    "snapshot_epoch": "BIGINT",
    "book_valid": "BOOLEAN",
    "topology": "VARCHAR",
    "bid_e4": "BIGINT",
    "bid_qty_e4": "BIGINT",
    "ask_e4": "BIGINT",
    "ask_qty_e4": "BIGINT",
    "bid_depth3_e4": "BIGINT",
    "ask_depth3_e4": "BIGINT",
    "bid_levels": "BIGINT",
    "ask_levels": "BIGINT",
    "mid_e4": "DOUBLE",
    "microprice_e4": "DOUBLE",
    "spread_e4": "BIGINT",
    "mid_logodds": "DOUBLE",
    "spread_logodds": "DOUBLE",
    "imbalance_depth3": "DOUBLE",
    "top_changed": "BOOLEAN",
    "touch_depletion": "BOOLEAN",
    "top3_retreat": "BOOLEAN",
    "pre_topology": "VARCHAR",
    "pre_spread_e4": "BIGINT",
    "pre_imbalance_depth3": "DOUBLE",
    "pre_side_depth3_e4": "BIGINT",
    "pre_opposite_depth3_e4": "BIGINT",
    "post_side_depth3_e4": "BIGINT",
    "top3_removed_e4": "BIGINT",
    "top3_retreat_fraction": "DOUBLE",
    "control_candidate": "BOOLEAN",
    "control_quiet_lookback_ns": "BIGINT",
}

EPISODE_TYPES = {
    "episode_id": "VARCHAR",
    "date": "VARCHAR",
    "market_ticker": "VARCHAR",
    "event_proxy": "VARCHAR",
    "sport": "VARCHAR",
    "family": "VARCHAR",
    "side": "VARCHAR",
    "depletion_ns": "BIGINT",
    "observation_end_ns": "BIGINT",
    "duration_us": "BIGINT",
    "endpoint_reason": "VARCHAR",
    "event_observed": "BOOLEAN",
    "refill_ns": "BIGINT",
    "refill_fraction": "DOUBLE",
    "original_touch_price_e4": "BIGINT",
    "pre_touch_qty_e4": "BIGINT",
    "removed_e4": "BIGINT",
    "depletion_fraction": "DOUBLE",
    "post_depletion_depth_e4": "BIGINT",
    "snapshot_epoch": "BIGINT",
    "covariate_timing": "VARCHAR",
    "covariate_clock_ns": "BIGINT",
    "pre_topology": "VARCHAR",
    "pre_spread_e4": "BIGINT",
    "pre_imbalance_depth3": "DOUBLE",
    "pre_side_depth3_e4": "BIGINT",
    "pre_opposite_depth3_e4": "BIGINT",
    "top3_removed_e4": "BIGINT",
    "top3_retreat_fraction": "DOUBLE",
    "top3_retreat": "BOOLEAN",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _l2_fact_objects(input_manifest: Mapping[str, Any], date: str) -> list[dict[str, Any]]:
    rows = []
    for obj in input_manifest.get("objects") or []:
        if obj.get("kind") != "facts" or obj.get("date") != date:
            continue
        if obj.get("channel") not in {"orderbooks_full", "orderbooks_l2"}:
            continue
        if "/category=Sports/" not in str(obj.get("logical_key") or ""):
            continue
        rows.append(dict(obj))
    return sorted(rows, key=lambda row: str(row.get("logical_key")))


def _quality_objects(input_manifest: Mapping[str, Any], date: str) -> list[dict[str, Any]]:
    return [
        dict(obj)
        for obj in input_manifest.get("objects") or []
        if obj.get("kind") == "l2_quality_receipt" and obj.get("date") == date
    ]


def _load_quality_assessment(
    input_manifest: Mapping[str, Any], date: str
) -> dict[str, Any]:
    objects = _quality_objects(input_manifest, date)
    if len(objects) != 1:
        raise L2ResearchError(
            f"exactly one L2 quality receipt is required for {date}; found {len(objects)}"
        )
    obj = objects[0]
    path = Path(str(obj.get("local_path") or ""))
    if not path.is_file():
        raise L2ResearchError(f"L2 quality receipt is missing: {date}")
    expected_sha = obj.get("sha256")
    if expected_sha and _sha256_file(path) != expected_sha:
        raise L2ResearchError(f"L2 quality receipt hash mismatch: {date}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise L2ResearchError(f"L2 quality receipt unreadable: {date}: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("date") != date:
        raise L2ResearchError(f"L2 quality receipt date binding mismatch: {date}")
    assessment = assess_l2_quality_receipt(receipt)
    assessment["logical_key"] = obj.get("logical_key")
    assessment["source_version_id"] = obj.get("source_version_id")
    assessment["sha256"] = expected_sha or _sha256_file(path)
    return assessment


def _l2_abi(market_buckets: int) -> dict[str, Any]:
    if (
        isinstance(market_buckets, bool)
        or not isinstance(market_buckets, int)
        or market_buckets < 1
        or market_buckets > 256
    ):
        raise ValueError("L2 market_buckets must be an integer in [1,256]")
    payload = {
        "schema_version": "deep03-v3-l2-snbd-stage-abi-v2",
        "module_sha256": _sha256_file(Path(__file__).resolve()),
        "market_partition_algorithm": "duckdb-hash-v1-modulo-plus-null-bucket",
        "market_bucket_count": market_buckets,
        "capture_dates": list(L2_CAPTURE_DATES),
        "analysis_dates": list(L2_ANALYSIS_DATES),
        "known_excluded_dates": dict(L2_KNOWN_EXCLUDED_DATES),
        "explicit_absent_dates": list(L2_ABSENT_DATES),
        "replay_columns": list(REPLAY_COLUMNS),
        "episode_columns": list(EPISODE_COLUMNS),
        "episode_horizon_ns": EPISODE_HORIZON_NS,
        "endpoint_rule": "refill_ns<=depletion_ns+episode_horizon_ns",
        "min_touch_qty_e4": MIN_TOUCH_QTY_E4,
        "min_depletion_fraction": MIN_DEPLETION_FRACTION,
        "refill_fraction": REFILL_FRACTION,
        "top3_retreat_definition": {
            "baseline": "same_side_displayed_depth_best_three_prices_pre_delta",
            "min_removed_e4": MIN_TOP3_RETREAT_QTY_E4,
            "min_fraction": MIN_TOP3_RETREAT_FRACTION,
        },
        "quiet_anchor_lookback_ns": QUIET_ANCHOR_LOOKBACK_NS,
        "matching": {
            "direction": "past_only",
            "window_ns": MATCH_WINDOW_NS,
            "time_bucket_ns": CONTROL_TIME_BUCKET_NS,
            "max_controls_per_exact_stratum_bucket": MAX_CONTROLS_PER_STRATUM_BUCKET,
            "max_candidates_per_episode": MAX_MATCH_CANDIDATES_PER_EPISODE,
        },
        "refill_survival": {
            "estimator": "discrete_risk_set_hazard_product_limit_survival",
            "interval_ns": HAZARD_INTERVAL_NS,
            "raw_event_fraction_called_hazard": False,
        },
        "per_market_forward_ws_seq_gap_inference_used": False,
        "universe_scope": "TARGETED_WATCHLIST_NOT_FULL_MARKET_UNIVERSE",
    }
    payload["abi_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return payload


def _stage_version(abi: Mapping[str, Any], stage: str, semantic: str) -> str:
    return f"{stage}-{semantic}-abi-{str(abi['abi_sha256'])[:16]}"


def _partition_key(date: str, bucket: int) -> str:
    return f"date={date}_bucket={bucket:03d}"


def _create_build_table(con, name: str, types: Mapping[str, str]) -> None:
    con.execute(f"DROP TABLE IF EXISTS {name}")
    con.execute(
        f"CREATE TEMP TABLE {name}("
        + ",".join(f'"{column}" {sql_type}' for column, sql_type in types.items())
        + ")"
    )


def _insert_dict_rows(
    con, table: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> None:
    if not rows:
        return
    placeholders = ",".join("?" for _ in columns)
    names = ",".join(f'"{column}"' for column in columns)
    con.executemany(
        f"INSERT INTO {table}({names}) VALUES ({placeholders})",
        [[row.get(column) for column in columns] for row in rows],
    )


def _validate_scope(input_manifest: Mapping[str, Any]) -> None:
    dates = input_manifest.get("release_dates")
    if list(dates or []) != list(L2_SCOPE_DATES):
        raise L2ResearchError(
            "L2 full-scope input must bind exactly 2026-07-10 through 2026-07-17"
        )
    for date in L2_ABSENT_DATES:
        if _l2_fact_objects(input_manifest, date):
            raise L2ResearchError(
                f"date declared ABSENT contains L2 fact objects: {date}"
            )
    for date in L2_CAPTURE_DATES:
        if not _l2_fact_objects(input_manifest, date):
            raise L2ResearchError(f"captured L2 date has no exact fact object: {date}")


def _normalized_l2_sql(con, objects: Sequence[Mapping[str, Any]], date: str) -> str:
    paths = [Path(str(obj["local_path"])) for obj in objects]
    if any(not path.is_file() for path in paths):
        raise L2ResearchError(f"exact L2 source object is unavailable: {date}")
    relation = _relation_sql(paths)
    columns = _columns(con, f"({relation})")
    projections = (
        _expr(columns, ("date",), "date", "DATE"),
        _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
        _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
        _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
        _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
        _expr(columns, ("event_ticker",), "event_proxy", "VARCHAR"),
        _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
        _expr(columns, ("series_ticker",), "family", "VARCHAR"),
        _expr(columns, ("msg_type",), "msg_type", "VARCHAR"),
        _expr(columns, ("side",), "side", "VARCHAR"),
        _expr(columns, ("price_e4",), "price_e4", "BIGINT"),
        _expr(columns, ("delta_e4",), "delta_e4", "BIGINT"),
        _expr(columns, ("yes_levels",), "yes_levels", "VARCHAR"),
        _expr(columns, ("no_levels",), "no_levels", "VARCHAR"),
        _expr(columns, ("ws_sid",), "ws_sid", "BIGINT"),
        _expr(columns, ("ws_seq",), "ws_seq", "BIGINT"),
    )
    typed = "SELECT " + ",".join(projections) + f" FROM ({relation}) source"
    wrong_date = int(con.execute(
        f"SELECT count(*) FROM ({typed}) WHERE date IS NULL OR date<>?::DATE", [date]
    ).fetchone()[0])
    if wrong_date:
        raise L2ResearchError(
            f"L2 source date partition mismatch: {date}: rows={wrong_date}"
        )
    return typed


def _assert_partition_order_unambiguous(con, relation: str, marker: str) -> None:
    row = con.execute(f"""
      WITH keyed AS (
        SELECT market_ticker,recv_wall_ns,recv_mono_ns,ws_sid,ws_seq,
               count(*) AS raw_rows,
               count(DISTINCT hash(struct_pack(
                 msg_type:=msg_type,side:=side,price_e4:=price_e4,
                 delta_e4:=delta_e4,yes_levels:=yes_levels,no_levels:=no_levels
               ))) AS variants
        FROM {relation}
        WHERE market_ticker IS NOT NULL AND recv_wall_ns IS NOT NULL
          AND recv_mono_ns IS NOT NULL AND ws_sid IS NOT NULL AND ws_seq IS NOT NULL
        GROUP BY market_ticker,recv_wall_ns,recv_mono_ns,ws_sid,ws_seq
      )
      SELECT count(*) FILTER (WHERE raw_rows>1),
             count(*) FILTER (WHERE variants>1) FROM keyed
    """).fetchone()
    if int(row[0]) or int(row[1]):
        raise L2ResearchError(
            "L2 receive-order key is duplicated or ambiguous: "
            f"{marker}: duplicate_keys={int(row[0])} ambiguous_keys={int(row[1])}"
        )


def _replay_one_partition(
    con,
    store: BoundedCheckpointStore,
    *,
    source_path: Path,
    replay_stage: str,
    replay_version: str,
    episode_stage: str,
    episode_version: str,
    key: str,
    observation_end_ns: int | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    replay_receipt_path = store._paths(replay_stage, key)[1]
    episode_receipt_path = store._paths(episode_stage, key)[1]
    if replay_receipt_path.exists() and episode_receipt_path.exists():
        replay_receipt = store.validate_partition(
            con, stage=replay_stage, stage_version=replay_version,
            partition_key=key,
        )
        episode_receipt = store.validate_partition(
            con, stage=episode_stage, stage_version=episode_version,
            partition_key=key,
        )
        return replay_receipt, episode_receipt, True

    relation = f"read_parquet({quote(source_path)},hive_partitioning=false)"
    _assert_partition_order_unambiguous(con, relation, key)
    _create_build_table(con, "l2_replay_build", REPLAY_TYPES)
    _create_build_table(con, "l2_episode_build", EPISODE_TYPES)
    cursor = con.cursor()
    cursor.execute(f"""
      SELECT date,t_us,recv_wall_ns,recv_mono_ns,market_ticker,
             event_proxy,sport,family,msg_type,side,price_e4,delta_e4,
             yes_levels,no_levels,ws_sid,ws_seq
      FROM {relation}
      ORDER BY market_ticker,recv_wall_ns,recv_mono_ns,ws_sid,ws_seq,
               msg_type,side,price_e4,delta_e4
    """)
    engine = L2ReplayEngine()
    while True:
        batch = cursor.fetchmany(REPLAY_FETCH_ROWS)
        if not batch:
            break
        replay_batch: list[dict[str, Any]] = []
        episode_batch: list[dict[str, Any]] = []
        for raw in batch:
            replay_row, episodes = engine.process(raw)
            replay_batch.append(replay_row)
            episode_batch.extend(episodes)
        _insert_dict_rows(con, "l2_replay_build", REPLAY_COLUMNS, replay_batch)
        _insert_dict_rows(con, "l2_episode_build", EPISODE_COLUMNS, episode_batch)
    cursor.close()
    _insert_dict_rows(
        con, "l2_episode_build", EPISODE_COLUMNS,
        engine.finish(observation_end_ns=observation_end_ns),
    )
    source_rows = int(engine.qc["source_rows"])
    replay_rows_count = int(con.execute(
        "SELECT count(*) FROM l2_replay_build"
    ).fetchone()[0])
    conservation = _require_row_conservation(
        label="l2_source_to_replay", observed=replay_rows_count,
        expected=source_rows, context=key,
    )
    null_classifications = int(con.execute(
        "SELECT count(*) FROM l2_replay_build WHERE classification IS NULL"
    ).fetchone()[0])
    if null_classifications:
        raise L2ResearchError(
            f"L2 replay left rows unclassified: {key}: {null_classifications}"
        )
    replay_receipt, replay_reused = store.write_partition(
        con,
        stage=replay_stage,
        stage_version=replay_version,
        partition_key=key,
        select_sql=(
            "SELECT * FROM l2_replay_build ORDER BY market_ticker,recv_wall_ns,"
            "recv_mono_ns,ws_sid,ws_seq"
        ),
        metrics={
            "source_rows": source_rows,
            "row_conservation": conservation,
            "qc": dict(engine.qc),
        },
    )
    episode_rows = int(con.execute(
        "SELECT count(*) FROM l2_episode_build"
    ).fetchone()[0])
    episode_receipt, episode_reused = store.write_partition(
        con,
        stage=episode_stage,
        stage_version=episode_version,
        partition_key=key,
        select_sql=(
            "SELECT * FROM l2_episode_build ORDER BY date,market_ticker,"
            "depletion_ns,episode_id"
        ),
        metrics={"episode_rows": episode_rows},
    )
    return replay_receipt, episode_receipt, replay_reused and episode_reused


def _bin_sql(column: str, kind: str) -> str:
    if kind == "spread":
        return (
            f"CASE WHEN {column} IS NULL THEN 'INVALID' "
            f"WHEN {column}<=100 THEN 'SPREAD_000_100' "
            f"WHEN {column}<=500 THEN 'SPREAD_101_500' "
            "ELSE 'SPREAD_501_PLUS' END"
        )
    if kind == "depth":
        return (
            f"CASE WHEN {column} IS NULL THEN 'DEPTH_UNKNOWN' "
            f"WHEN {column}<=20000 THEN 'DEPTH_000_020K' "
            f"WHEN {column}<=100000 THEN 'DEPTH_020K_100K' "
            "ELSE 'DEPTH_100K_PLUS' END"
        )
    if kind == "imbalance":
        return (
            f"CASE WHEN {column} IS NULL THEN 'IMBALANCE_UNKNOWN' "
            f"WHEN abs({column})<0.20 THEN 'IMBALANCE_BALANCED' "
            f"WHEN {column}<0 THEN 'IMBALANCE_ASK_HEAVY' "
            "ELSE 'IMBALANCE_BID_HEAVY' END"
        )
    raise ValueError(f"unknown L2 bin kind: {kind}")


def _atlas_sql(replay_relation: str, episode_relation: str) -> str:
    spread_bin = _bin_sql("spread_e4", "spread")
    depth_bin = _bin_sql("bid_depth3_e4+ask_depth3_e4", "depth")
    imbalance_bin = _bin_sql("imbalance_depth3", "imbalance")
    retreat_spread_bin = _bin_sql("pre_spread_e4", "spread")
    retreat_depth_bin = _bin_sql("pre_side_depth3_e4", "depth")
    retreat_imbalance_bin = _bin_sql("pre_imbalance_depth3", "imbalance")
    episode_spread_bin = _bin_sql("pre_spread_e4", "spread")
    episode_depth_bin = _bin_sql("pre_side_depth3_e4", "depth")
    episode_imbalance_bin = _bin_sql("pre_imbalance_depth3", "imbalance")
    interval_us = HAZARD_INTERVAL_NS // 1000
    interval_count = EPISODE_HORIZON_NS // HAZARD_INTERVAL_NS
    return f"""
      WITH date_bounds AS (
        SELECT date,max(recv_wall_ns)::BIGINT AS observation_end_ns
        FROM {replay_relation} GROUP BY date
      ), ordered AS (
        SELECT r.*,b.observation_end_ns,
               lead(recv_wall_ns) OVER market_order AS next_clock_ns,
               lead(book_valid) OVER market_order AS next_valid,
               lead(snapshot_epoch) OVER market_order AS next_snapshot_epoch,
               lead(msg_type) OVER market_order AS next_msg_type,
               lag(topology) OVER market_order AS previous_topology,
               lag(book_valid) OVER market_order AS previous_valid,
               lag(snapshot_epoch) OVER market_order AS previous_snapshot_epoch
        FROM {replay_relation} r JOIN date_bounds b USING(date)
        WINDOW market_order AS (
          PARTITION BY date,market_ticker
          ORDER BY recv_wall_ns,recv_mono_ns,ws_sid,ws_seq
        )
      ), state_rows AS (
        SELECT *,{spread_bin} AS spread_bin,
               {depth_bin} AS depth_bin,
               {imbalance_bin} AS imbalance_bin,
               CASE WHEN next_clock_ns IS NOT NULL AND next_clock_ns>=recv_wall_ns
                    THEN (next_clock_ns-recv_wall_ns)/1000
                    WHEN observation_end_ns>=recv_wall_ns
                    THEN (observation_end_ns-recv_wall_ns)/1000 END AS dwell_us,
               CASE
                 WHEN next_clock_ns IS NULL THEN 'RIGHT_CENSORED_CAPTURE_END'
                 WHEN next_valid AND next_snapshot_epoch=snapshot_epoch
                   THEN 'OBSERVED_NEXT_VALID_STATE'
                 WHEN next_msg_type='snapshot'
                   THEN 'RIGHT_CENSORED_SNAPSHOT_RESET'
                 ELSE 'RIGHT_CENSORED_INVALID_OR_REJECTED'
               END AS dwell_end_reason
        FROM ordered WHERE book_valid
      ), state_atlas AS (
        SELECT 'STATE' AS record_kind,date,sport,family,
               NULL::VARCHAR AS side,topology,
               NULL::VARCHAR AS from_topology,NULL::VARCHAR AS to_topology,
               dwell_end_reason AS endpoint_reason,
               spread_bin,depth_bin,imbalance_bin,
               count(*)::BIGINT AS n_rows,
               count(DISTINCT market_ticker)::BIGINT AS n_markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS n_events,
               cast(coalesce(sum(dwell_us),0) AS DOUBLE) AS total_dwell_us,
               quantile_cont(dwell_us,0.5)::DOUBLE AS median_dwell_us,
               quantile_cont(dwell_us,0.95)::DOUBLE AS p95_dwell_us,
               NULL::DOUBLE AS observed_event_fraction,
               NULL::BIGINT AS horizon_start_us,NULL::BIGINT AS horizon_end_us,
               NULL::BIGINT AS at_risk_n,NULL::BIGINT AS events_n,
               NULL::DOUBLE AS interval_hazard,NULL::DOUBLE AS survival_to_end,
               NULL::DOUBLE AS median_duration_us,
               NULL::DOUBLE AS p95_duration_us
        FROM state_rows
        GROUP BY date,sport,family,topology,dwell_end_reason,
                 spread_bin,depth_bin,imbalance_bin
      ), transition_atlas AS (
        SELECT 'TRANSITION' AS record_kind,date,sport,family,
               NULL::VARCHAR AS side,NULL::VARCHAR AS topology,
               previous_topology AS from_topology,topology AS to_topology,
               'OBSERVED_WITHIN_VALID_EPOCH' AS endpoint_reason,
               spread_bin,depth_bin,imbalance_bin,
               count(*)::BIGINT AS n_rows,
               count(DISTINCT market_ticker)::BIGINT AS n_markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS n_events,
               NULL::DOUBLE AS total_dwell_us,NULL::DOUBLE AS median_dwell_us,
               NULL::DOUBLE AS p95_dwell_us,
               NULL::DOUBLE AS observed_event_fraction,
               NULL::BIGINT AS horizon_start_us,NULL::BIGINT AS horizon_end_us,
               NULL::BIGINT AS at_risk_n,NULL::BIGINT AS events_n,
               NULL::DOUBLE AS interval_hazard,NULL::DOUBLE AS survival_to_end,
               NULL::DOUBLE AS median_duration_us,
               NULL::DOUBLE AS p95_duration_us
        FROM state_rows
        WHERE previous_valid AND previous_snapshot_epoch=snapshot_epoch
          AND previous_topology IS NOT NULL
        GROUP BY date,sport,family,previous_topology,topology,
                 spread_bin,depth_bin,imbalance_bin
      ), retreat_atlas AS (
        SELECT 'RETREAT_TOP3_BASELINE' AS record_kind,date,sport,family,
               side,pre_topology AS topology,
               NULL::VARCHAR AS from_topology,NULL::VARCHAR AS to_topology,
               'MATERIAL_TOP3_DEPTH_REDUCTION' AS endpoint_reason,
               {retreat_spread_bin} AS spread_bin,
               {retreat_depth_bin} AS depth_bin,
               {retreat_imbalance_bin} AS imbalance_bin,
               count(*)::BIGINT AS n_rows,
               count(DISTINCT market_ticker)::BIGINT AS n_markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS n_events,
               NULL::DOUBLE AS total_dwell_us,NULL::DOUBLE AS median_dwell_us,
               NULL::DOUBLE AS p95_dwell_us,
               NULL::DOUBLE AS observed_event_fraction,
               NULL::BIGINT AS horizon_start_us,NULL::BIGINT AS horizon_end_us,
               NULL::BIGINT AS at_risk_n,NULL::BIGINT AS events_n,
               NULL::DOUBLE AS interval_hazard,NULL::DOUBLE AS survival_to_end,
               NULL::DOUBLE AS median_duration_us,
               NULL::DOUBLE AS p95_duration_us
        FROM state_rows WHERE top3_retreat
        GROUP BY ALL
      ), episode_atlas AS (
        SELECT 'EPISODE' AS record_kind,date,sport,family,side,
               pre_topology AS topology,
               NULL::VARCHAR AS from_topology,NULL::VARCHAR AS to_topology,
               endpoint_reason,{episode_spread_bin} AS spread_bin,
               {episode_depth_bin} AS depth_bin,
               {episode_imbalance_bin} AS imbalance_bin,
               count(*)::BIGINT AS n_rows,
               count(DISTINCT market_ticker)::BIGINT AS n_markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS n_events,
               NULL::DOUBLE AS total_dwell_us,NULL::DOUBLE AS median_dwell_us,
               NULL::DOUBLE AS p95_dwell_us,
               avg(CASE WHEN event_observed THEN 1.0 ELSE 0.0 END)::DOUBLE
                 AS observed_event_fraction,
               NULL::BIGINT AS horizon_start_us,NULL::BIGINT AS horizon_end_us,
               NULL::BIGINT AS at_risk_n,NULL::BIGINT AS events_n,
               NULL::DOUBLE AS interval_hazard,NULL::DOUBLE AS survival_to_end,
               quantile_cont(duration_us,0.5)::DOUBLE AS median_duration_us,
               quantile_cont(duration_us,0.95)::DOUBLE AS p95_duration_us
        FROM {episode_relation}
        GROUP BY ALL
      ), hazard_risk_rows AS (
        SELECT e.*,b.bucket_index::BIGINT AS bucket_index,
               (b.bucket_index*{interval_us})::BIGINT AS horizon_start_us,
               ((b.bucket_index+1)*{interval_us})::BIGINT AS horizon_end_us
        FROM {episode_relation} e
        CROSS JOIN range(0,{interval_count}) b(bucket_index)
        WHERE (b.bucket_index=0 AND e.duration_us>=0)
           OR (b.bucket_index>0 AND e.duration_us>b.bucket_index*{interval_us})
      ), hazard_counts AS (
        SELECT date,sport,family,side,pre_topology,
               {episode_spread_bin} AS spread_bin,
               {episode_depth_bin} AS depth_bin,
               {episode_imbalance_bin} AS imbalance_bin,
               horizon_start_us,horizon_end_us,count(*)::BIGINT AS at_risk_n,
               count(*) FILTER (
                 WHERE event_observed
                   AND duration_us<=horizon_end_us
                   AND (bucket_index=0 OR duration_us>horizon_start_us)
               )::BIGINT AS events_n,
               count(DISTINCT market_ticker)::BIGINT AS n_markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS n_events
        FROM hazard_risk_rows
        GROUP BY ALL
      ), hazard_rates AS (
        SELECT *,events_n::DOUBLE/nullif(at_risk_n,0) AS interval_hazard
        FROM hazard_counts
      ), refill_hazard AS (
        SELECT 'REFILL_HAZARD' AS record_kind,date,sport,family,side,
               pre_topology AS topology,
               NULL::VARCHAR AS from_topology,NULL::VARCHAR AS to_topology,
               NULL::VARCHAR AS endpoint_reason,
               spread_bin,depth_bin,imbalance_bin,at_risk_n AS n_rows,
               n_markets,n_events,
               NULL::DOUBLE AS total_dwell_us,NULL::DOUBLE AS median_dwell_us,
               NULL::DOUBLE AS p95_dwell_us,
               NULL::DOUBLE AS observed_event_fraction,
               horizon_start_us,horizon_end_us,at_risk_n,events_n,
               interval_hazard,
               product(1.0-interval_hazard) OVER (
                 PARTITION BY date,sport,family,side,pre_topology,
                              spread_bin,depth_bin,imbalance_bin
                 ORDER BY horizon_start_us ROWS UNBOUNDED PRECEDING
               )::DOUBLE AS survival_to_end,
               NULL::DOUBLE AS median_duration_us,
               NULL::DOUBLE AS p95_duration_us
        FROM hazard_rates
      )
      SELECT * FROM state_atlas
      UNION ALL BY NAME SELECT * FROM transition_atlas
      UNION ALL BY NAME SELECT * FROM retreat_atlas
      UNION ALL BY NAME SELECT * FROM episode_atlas
      UNION ALL BY NAME SELECT * FROM refill_hazard
    """


def _matches_sql(
    replay_relation: str,
    episode_relation: str,
    *,
    anchor_shift_ns: int = 0,
) -> str:
    """Return bounded past-only, quiet-anchor matching SQL.

    The join cardinality is bounded before episode/control pairing: within
    every exact stratum and 60-second clock bucket, at most eight controls are
    admitted.  A five-minute lookback spans at most six buckets, hence at most
    48 joined candidates per episode (and the explicit 64-row cap is a second
    fail-closed ceiling).  No date-wide Cartesian relation is constructed.
    """
    episode_spread = _bin_sql("e.pre_spread_e4", "spread")
    episode_depth = _bin_sql("e.pre_side_depth3_e4", "depth")
    episode_imbalance = _bin_sql("e.pre_imbalance_depth3", "imbalance")
    control_spread = _bin_sql("c.spread_e4", "spread")
    control_depth = _bin_sql(
        "CASE WHEN c.side='yes' THEN c.bid_depth3_e4 ELSE c.ask_depth3_e4 END",
        "depth",
    )
    control_imbalance = _bin_sql("c.imbalance_depth3", "imbalance")
    bucket_span = math.ceil(MATCH_WINDOW_NS / CONTROL_TIME_BUCKET_NS)
    return f"""
      WITH episodes AS (
        SELECT e.*,{episode_spread} AS spread_bin,
               {episode_depth} AS depth_bin,
               {episode_imbalance} AS imbalance_bin,
               (e.depletion_ns+({int(anchor_shift_ns)}))::BIGINT AS anchor_ns,
               ((e.depletion_ns+({int(anchor_shift_ns)})) //
                     {CONTROL_TIME_BUCKET_NS})::BIGINT AS anchor_bucket
        FROM {episode_relation} e
        WHERE e.covariate_timing='PRE_DEPLETION_STATE'
      ), controls_binned AS (
        SELECT c.*,{control_spread} AS spread_bin,
               {control_depth} AS depth_bin,
               {control_imbalance} AS imbalance_bin,
               CASE WHEN c.side='yes' THEN c.bid_depth3_e4
                    ELSE c.ask_depth3_e4 END::BIGINT AS control_side_depth3_e4,
               (c.recv_wall_ns // {CONTROL_TIME_BUCKET_NS})::BIGINT
                 AS control_bucket,
               concat(c.market_ticker,'|',cast(c.recv_wall_ns AS VARCHAR),'|',
                      cast(c.ws_sid AS VARCHAR),'|',cast(c.ws_seq AS VARCHAR))
                 AS control_id,
               row_number() OVER (
                 PARTITION BY c.date,c.sport,c.family,c.side,c.topology,
                              {control_spread},{control_depth},{control_imbalance},
                              (c.recv_wall_ns // {CONTROL_TIME_BUCKET_NS})
                 ORDER BY c.recv_wall_ns DESC,c.market_ticker,c.ws_sid,c.ws_seq
               ) AS stratum_bucket_rank
        FROM {replay_relation} c
        WHERE c.control_candidate AND c.book_valid AND c.topology='TWO_SIDED'
          AND c.control_quiet_lookback_ns>={QUIET_ANCHOR_LOOKBACK_NS}
      ), controls AS (
        SELECT * FROM controls_binned
        WHERE stratum_bucket_rank<={MAX_CONTROLS_PER_STRATUM_BUCKET}
      ), eligible_ranked AS (
        SELECT e.episode_id,e.date,e.market_ticker AS treatment_market,
               e.event_proxy AS treatment_event,e.depletion_ns,e.anchor_ns,
               c.control_id,c.market_ticker AS control_market,
               c.event_proxy AS control_event,c.recv_wall_ns AS control_ns,
               c.ws_sid AS control_ws_sid,c.ws_seq AS control_ws_seq,
               c.control_quiet_lookback_ns,c.stratum_bucket_rank,
               e.sport,e.family,e.side,e.pre_topology AS topology,
               e.spread_bin,e.depth_bin,e.imbalance_bin,
               (e.anchor_ns-c.recv_wall_ns)::BIGINT AS control_lag_ns,
               e.pre_imbalance_depth3::DOUBLE AS treatment_imbalance,
               c.imbalance_depth3::DOUBLE AS control_imbalance,
               e.pre_spread_e4::BIGINT AS treatment_spread_e4,
               c.spread_e4::BIGINT AS control_spread_e4,
               e.pre_side_depth3_e4::BIGINT AS treatment_depth3_e4,
               c.control_side_depth3_e4::BIGINT AS control_depth3_e4,
               abs(c.imbalance_depth3-e.pre_imbalance_depth3)::DOUBLE
                 AS imbalance_distance,
               abs(c.spread_e4-e.pre_spread_e4)::BIGINT AS spread_distance,
               abs(c.control_side_depth3_e4-e.pre_side_depth3_e4)::BIGINT
                 AS depth_distance,
               row_number() OVER (
                 PARTITION BY e.episode_id
                 ORDER BY e.anchor_ns-c.recv_wall_ns,c.market_ticker,
                          c.recv_wall_ns,c.ws_sid,c.ws_seq
               ) AS candidate_rank,
               count(*) OVER (PARTITION BY e.episode_id)::BIGINT AS candidate_pool_n
        FROM episodes e JOIN controls c
          ON c.date=e.date AND c.sport=e.sport AND c.family=e.family
         AND c.side=e.side AND c.topology=e.pre_topology
         AND c.spread_bin=e.spread_bin AND c.depth_bin=e.depth_bin
         AND c.imbalance_bin=e.imbalance_bin
         AND c.event_proxy<>'' AND e.event_proxy<>''
         AND c.event_proxy<>e.event_proxy
         AND c.control_bucket BETWEEN e.anchor_bucket-{bucket_span}
                                  AND e.anchor_bucket
         AND c.recv_wall_ns<e.anchor_ns
         AND c.recv_wall_ns>=e.anchor_ns-{MATCH_WINDOW_NS}
      ), eligible AS (
        SELECT * FROM eligible_ranked
        WHERE candidate_rank<={MAX_MATCH_CANDIDATES_PER_EPISODE}
      ), first_choice AS (
        SELECT * EXCLUDE (candidate_rank),
               row_number() OVER (
                 PARTITION BY control_id
                 ORDER BY control_lag_ns,episode_id
               ) AS control_choice
        FROM eligible WHERE candidate_rank=1
      )
      SELECT concat(episode_id,'|',control_id) AS match_id,
             episode_id,date,treatment_market,treatment_event,depletion_ns,
             anchor_ns,control_id,control_market,control_event,control_ns,
             control_ws_sid,control_ws_seq,sport,family,side,topology,
             spread_bin,depth_bin,imbalance_bin,control_lag_ns,
             control_quiet_lookback_ns,stratum_bucket_rank,candidate_pool_n,
             treatment_imbalance,control_imbalance,
             treatment_spread_e4,control_spread_e4,
             treatment_depth3_e4,control_depth3_e4,
             imbalance_distance,spread_distance,depth_distance,
             true AS past_only,true AS quiet_anchor,
             'PAST_ONLY_QUIET_NEAREST_GLOBAL_ARBITRATION' AS matching_method
      FROM first_choice WHERE control_choice=1
      ORDER BY date,episode_id,control_id
    """


def _write_date_reducers(
    con,
    store: BoundedCheckpointStore,
    *,
    abi: Mapping[str, Any],
    date: str,
    replay_paths: Sequence[Path],
    episode_paths: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    atlas_stage = "l2_exact_atlas"
    match_stage = "l2_matched_controls"
    atlas_version = _stage_version(abi, atlas_stage, "risk-set-survival-v2")
    match_version = _stage_version(abi, match_stage, "past-quiet-bounded-v2")
    key = f"date={date}"
    atlas_receipt_path = store._paths(atlas_stage, key)[1]
    match_receipt_path = store._paths(match_stage, key)[1]
    if atlas_receipt_path.exists() and match_receipt_path.exists():
        return (
            store.validate_partition(
                con, stage=atlas_stage, stage_version=atlas_version,
                partition_key=key,
            ),
            store.validate_partition(
                con, stage=match_stage, stage_version=match_version,
                partition_key=key,
            ),
            {"atlas_reused": True, "match_reused": True},
        )
    replay_relation = (
        f"read_parquet({path_list(replay_paths)},union_by_name=true,"
        "hive_partitioning=false)"
    )
    episode_relation = (
        f"read_parquet({path_list(episode_paths)},union_by_name=true,"
        "hive_partitioning=false)"
    )
    state_rows = int(con.execute(
        f"SELECT count(*) FROM {replay_relation} WHERE book_valid"
    ).fetchone()[0])
    episode_rows = int(con.execute(
        f"SELECT count(*) FROM {episode_relation}"
    ).fetchone()[0])
    con.execute(
        "CREATE OR REPLACE TEMP TABLE l2_atlas_build AS "
        + _atlas_sql(replay_relation, episode_relation)
    )
    state_endpoints = {
        str(reason): int(count)
        for reason, count in con.execute("""
          SELECT endpoint_reason,coalesce(sum(n_rows),0)::BIGINT
          FROM l2_atlas_build WHERE record_kind='STATE'
          GROUP BY endpoint_reason ORDER BY endpoint_reason
        """).fetchall()
    }
    hazard_violations = int(con.execute("""
      SELECT count(*) FROM l2_atlas_build
      WHERE record_kind='REFILL_HAZARD' AND (
        at_risk_n<=0 OR events_n<0 OR events_n>at_risk_n
        OR interval_hazard<0 OR interval_hazard>1
        OR survival_to_end<0 OR survival_to_end>1
      )
    """).fetchone()[0])
    survival_increases = int(con.execute("""
      SELECT count(*) FROM (
        SELECT survival_to_end,
               lag(survival_to_end) OVER (
                 PARTITION BY date,sport,family,side,topology,
                              spread_bin,depth_bin,imbalance_bin
                 ORDER BY horizon_start_us
               ) AS prior_survival
        FROM l2_atlas_build WHERE record_kind='REFILL_HAZARD'
      ) ordered
      WHERE prior_survival IS NOT NULL
        AND survival_to_end>prior_survival+1e-12
    """).fetchone()[0])
    if hazard_violations or survival_increases:
        raise L2ResearchError(
            f"L2 risk-set survival invariant failed: {date}: "
            f"hazard={hazard_violations} monotonic={survival_increases}"
        )
    atlas_receipt, atlas_reused = store.write_partition(
        con,
        stage=atlas_stage,
        stage_version=atlas_version,
        partition_key=key,
        select_sql=(
            "SELECT * FROM l2_atlas_build ORDER BY record_kind,date,sport,"
            "family,side,topology,horizon_start_us,endpoint_reason"
        ),
        metrics={
            "eligible_state_rows": state_rows,
            "episode_rows": episode_rows,
            "quantiles": "duckdb_exact_quantile_cont",
            "cross_market_bucket_reduction": True,
            "state_dwell_endpoint_counts": state_endpoints,
            "last_valid_state_extends_to_proven_date_observation_end": True,
            "invalid_and_snapshot_resets_are_right_censoring": True,
            "refill_estimator": "100ms_discrete_risk_set_hazard_product_limit_survival",
            "raw_refill_fraction_labeled_as_hazard": False,
            "hazard_invariant_violations": hazard_violations,
            "survival_monotonicity_violations": survival_increases,
        },
    )
    atlas_path = store._paths(atlas_stage, key)[0]
    reduced_state_rows = int(con.execute(
        f"SELECT coalesce(sum(n_rows),0) FROM read_parquet({quote(atlas_path)}) "
        "WHERE record_kind='STATE'"
    ).fetchone()[0])
    reduced_episode_rows = int(con.execute(
        f"SELECT coalesce(sum(n_rows),0) FROM read_parquet({quote(atlas_path)}) "
        "WHERE record_kind='EPISODE'"
    ).fetchone()[0])
    _require_row_conservation(
        label="l2_state_atlas", observed=reduced_state_rows,
        expected=state_rows, context=date,
    )
    _require_row_conservation(
        label="l2_episode_atlas", observed=reduced_episode_rows,
        expected=episode_rows, context=date,
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE l2_match_build AS "
        + _matches_sql(replay_relation, episode_relation)
    )
    shift_ns = -2 * MATCH_WINDOW_NS
    con.execute(
        "CREATE OR REPLACE TEMP TABLE l2_shift_match_build AS "
        + _matches_sql(
            replay_relation, episode_relation, anchor_shift_ns=shift_ns
        )
    )
    theoretical_candidate_bound = (
        math.ceil(MATCH_WINDOW_NS / CONTROL_TIME_BUCKET_NS) + 1
    ) * MAX_CONTROLS_PER_STRATUM_BUCKET
    matching_invariants = con.execute(f"""
      SELECT
        count(*) FILTER (WHERE control_ns>=anchor_ns),
        count(*) FILTER (
          WHERE control_quiet_lookback_ns<{QUIET_ANCHOR_LOOKBACK_NS}
        ),
        count(*) FILTER (
          WHERE stratum_bucket_rank>{MAX_CONTROLS_PER_STRATUM_BUCKET}
        ),
        count(*) FILTER (
          WHERE candidate_pool_n>{theoretical_candidate_bound}
             OR candidate_pool_n>{MAX_MATCH_CANDIDATES_PER_EPISODE}
        ),
        count(*)::BIGINT,count(DISTINCT episode_id)::BIGINT,
        count(DISTINCT control_id)::BIGINT
      FROM l2_match_build
    """).fetchone()
    invariant_names = (
        "future_rows", "nonquiet_rows", "control_bucket_cap_rows",
        "episode_candidate_cap_rows", "rows", "episodes", "controls",
    )
    invariant_receipt = {
        name: int(value) for name, value in zip(invariant_names, matching_invariants)
    }
    if any(invariant_receipt[name] for name in invariant_names[:4]):
        raise L2ResearchError(
            f"L2 matched-control temporal/bound invariant failed: {date}: "
            + json.dumps(invariant_receipt, sort_keys=True)
        )
    if not (
        invariant_receipt["rows"] == invariant_receipt["episodes"]
        == invariant_receipt["controls"]
    ):
        raise L2ResearchError(f"L2 matched-control uniqueness failed: {date}")

    def standardized_balance(treatment: str, control: str) -> float | None:
        values = con.execute(f"""
          SELECT avg({treatment}),avg({control}),
                 var_pop({treatment}),var_pop({control})
          FROM l2_match_build
          WHERE {treatment} IS NOT NULL AND {control} IS NOT NULL
        """).fetchone()
        if values[0] is None or values[1] is None:
            return None
        pooled = math.sqrt(max(0.0, (float(values[2] or 0) + float(values[3] or 0)) / 2))
        if pooled == 0:
            return 0.0 if float(values[0]) == float(values[1]) else None
        return (float(values[0]) - float(values[1])) / pooled

    distance_row = con.execute("""
      SELECT avg(imbalance_distance),quantile_cont(imbalance_distance,0.95),
             avg(spread_distance),quantile_cont(spread_distance,0.95),
             avg(depth_distance),quantile_cont(depth_distance,0.95),
             quantile_cont(control_lag_ns,0.5),
             quantile_cont(control_lag_ns,0.95)
      FROM l2_match_build
    """).fetchone()
    distance_names = (
        "mean_abs_imbalance", "p95_abs_imbalance", "mean_abs_spread_e4",
        "p95_abs_spread_e4", "mean_abs_depth3_e4", "p95_abs_depth3_e4",
        "median_control_lag_ns", "p95_control_lag_ns",
    )
    balance_receipt: dict[str, Any] = {
        name: (None if value is None else float(value))
        for name, value in zip(distance_names, distance_row)
    }
    balance_receipt["smd_imbalance"] = standardized_balance(
        "treatment_imbalance", "control_imbalance"
    )
    balance_receipt["smd_spread_e4"] = standardized_balance(
        "treatment_spread_e4", "control_spread_e4"
    )
    balance_receipt["smd_depth3_e4"] = standardized_balance(
        "treatment_depth3_e4", "control_depth3_e4"
    )

    def concentration(column: str) -> dict[str, Any]:
        row = con.execute(f"""
          WITH counts AS (
            SELECT {column} AS identity,count(*)::DOUBLE AS n
            FROM l2_match_build GROUP BY {column}
          ), totals AS (SELECT coalesce(sum(n),0)::DOUBLE AS total FROM counts)
          SELECT count(*)::BIGINT,
                 CASE WHEN total>0 THEN max(n)/total END,
                 CASE WHEN total>0 THEN sum((n/total)*(n/total)) END
          FROM counts CROSS JOIN totals GROUP BY total
        """).fetchone()
        if row is None:
            return {"unique": 0, "max_share": None, "hhi": None}
        return {
            "unique": int(row[0]),
            "max_share": None if row[1] is None else float(row[1]),
            "hhi": None if row[2] is None else float(row[2]),
        }

    shifted = con.execute("""
      SELECT count(*)::BIGINT,
             count(*) FILTER (WHERE control_ns>=anchor_ns)::BIGINT,
             count(*) FILTER (WHERE NOT past_only OR NOT quiet_anchor)::BIGINT
      FROM l2_shift_match_build
    """).fetchone()
    negative_controls = {
        "future_leakage": {
            "state": "PASS" if invariant_receipt["future_rows"] == 0 else "FAIL",
            "violations": invariant_receipt["future_rows"],
            "rule": "control_ns < treatment_anchor_ns",
        },
        "reset_proximity": {
            "state": "PASS" if invariant_receipt["nonquiet_rows"] == 0 else "FAIL",
            "violations": invariant_receipt["nonquiet_rows"],
            "rule": f"quiet_lookback_ns >= {QUIET_ANCHOR_LOOKBACK_NS}",
        },
        "past_shift_placebo": {
            "state": "DIAGNOSTIC_ONLY_NO_OUTCOME_ESTIMATE",
            "anchor_shift_ns": shift_ns,
            "matched_rows": int(shifted[0]),
            "future_violations": int(shifted[1]),
            "past_or_quiet_flag_violations": int(shifted[2]),
        },
    }
    match_receipt, match_reused = store.write_partition(
        con,
        stage=match_stage,
        stage_version=match_version,
        partition_key=key,
        select_sql="SELECT * FROM l2_match_build ORDER BY date,episode_id,control_id",
        metrics={
            "matching_method": "PAST_ONLY_QUIET_NEAREST_GLOBAL_ARBITRATION",
            "match_window_ns": MATCH_WINDOW_NS,
            "quiet_anchor_lookback_ns": QUIET_ANCHOR_LOOKBACK_NS,
            "control_time_bucket_ns": CONTROL_TIME_BUCKET_NS,
            "max_controls_per_exact_stratum_bucket": MAX_CONTROLS_PER_STRATUM_BUCKET,
            "theoretical_candidates_per_episode_bound": theoretical_candidate_bound,
            "hard_candidates_per_episode_cap": MAX_MATCH_CANDIDATES_PER_EPISODE,
            "date_wide_cartesian_join_used": False,
            "same_exact_strata": [
                "date", "sport", "family", "side", "topology",
                "spread_bin", "depth_bin", "imbalance_bin",
            ],
            "different_event_required": True,
            "past_only_required": True,
            "without_control_replacement": True,
            "invariants": invariant_receipt,
            "negative_controls": negative_controls,
            "balance": balance_receipt,
            "concentration": {
                "control_market": concentration("control_market"),
                "control_event": concentration("control_event"),
            },
            "outcome_or_pnl_columns": False,
        },
    )
    match_path = store._paths(match_stage, key)[0]
    uniqueness = con.execute(f"""
      SELECT count(*) AS rows,count(DISTINCT episode_id) AS episodes,
             count(DISTINCT control_id) AS controls
      FROM read_parquet({quote(match_path)})
    """).fetchone()
    if int(uniqueness[0]) != int(uniqueness[1]) or int(uniqueness[0]) != int(uniqueness[2]):
        raise L2ResearchError(f"L2 matched-control uniqueness failed: {date}")
    return atlas_receipt, match_receipt, {
        "atlas_reused": atlas_reused,
        "match_reused": match_reused,
    }


def execute_l2_snbd_bounded(
    con,
    input_manifest: dict[str, Any],
    store: BoundedCheckpointStore,
    *,
    market_buckets: int = 16,
) -> dict[str, Any]:
    """Execute exact-input L2 replay and publish durable partition receipts.

    The caller owns the checkpoint-store writer lock.  All captured days are
    physically partitioned by exact date and market hash; both absent days are
    represented in the availability ledger and never silently treated as zero.
    """
    _validate_scope(input_manifest)
    expected_binding = bounded_source_binding(input_manifest)
    if store.source_binding != expected_binding:
        raise L2ResearchError("L2 checkpoint store source binding mismatch")
    abi = _l2_abi(market_buckets)
    quality: dict[str, dict[str, Any]] = {}
    for date in L2_CAPTURE_DATES:
        assessment = _load_quality_assessment(input_manifest, date)
        blockers = list(assessment["blockers"])
        known_exclusion = L2_KNOWN_EXCLUDED_DATES.get(date)
        if known_exclusion and known_exclusion not in blockers:
            # These dispositions come from the immutable independent audit of
            # the exact capture, not from an inferred per-market ws_seq gap.
            blockers.append(f"independent_quality_audit:{known_exclusion}")
        included = (
            date in L2_ANALYSIS_DATES
            and assessment["state"] == "PASS"
            and not known_exclusion
        )
        assessment["receipt_state"] = assessment["state"]
        assessment["blockers"] = blockers
        assessment["analysis_disposition"] = (
            "INCLUDED_CLEAN_DATE" if included else "EXCLUDED_DATA_QUALITY"
        )
        assessment["usable_for_estimands"] = included
        assessment["known_quality_disposition"] = known_exclusion
        quality[date] = assessment
    analysis_dates = tuple(
        date for date in L2_CAPTURE_DATES
        if quality[date]["usable_for_estimands"]
    )

    physical_stage = "l2_physical"
    replay_stage = "l2_replay"
    episode_stage = "l2_episodes"
    physical_version = _stage_version(
        abi, physical_stage, "all-captured-coverage-v2"
    )
    replay_version = _stage_version(abi, replay_stage, "sequence-replay-v2")
    episode_version = _stage_version(abi, episode_stage, "causal-lifecycle-v2")
    bucket_values = range(market_buckets + 1)  # final value is NULL-market bucket
    physical_keys: list[str] = []
    replay_keys: list[str] = []
    source_counts: dict[str, int] = {}
    source_observation_ends: dict[str, int | None] = {}
    activity = {
        physical_stage: {"written": 0, "reused": 0},
        replay_stage: {"written": 0, "reused": 0},
        episode_stage: {"written": 0, "reused": 0},
    }
    date_replay_qc: dict[str, dict[str, int]] = {
        date: defaultdict(int) for date in analysis_dates
    }

    for date in L2_CAPTURE_DATES:
        objects = _l2_fact_objects(input_manifest, date)
        typed = _normalized_l2_sql(con, objects, date)
        source_count = int(con.execute(f"SELECT count(*) FROM ({typed})").fetchone()[0])
        if source_count <= 0:
            quality[date]["usable_for_estimands"] = False
            quality[date]["analysis_disposition"] = "EXCLUDED_DATA_QUALITY"
            quality[date]["blockers"].append("empty_sports_l2_capture")
        source_counts[date] = source_count
        quality[date]["source_objects"] = len(objects)
        quality[date]["source_rows"] = source_count
        observed_end = con.execute(
            f"SELECT max(recv_wall_ns) FROM ({typed})"
        ).fetchone()[0]
        source_observation_ends[date] = (
            int(observed_end) if observed_end is not None else None
        )
        declared_counts = [obj.get("row_count") for obj in objects]
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in declared_counts
        ):
            _require_row_conservation(
                label="l2_manifest_to_source",
                observed=source_count,
                expected=sum(int(value) for value in declared_counts),
                context=date,
            )
        partition_keys = {
            bucket: _partition_key(date, bucket) for bucket in bucket_values
        }
        scatter_sql = f"""
          SELECT CASE WHEN market_ticker IS NULL THEN {market_buckets}
                      ELSE cast(mod(hash(market_ticker),{market_buckets}) AS INTEGER)
                 END AS partition_value,
                 *
          FROM ({typed})
        """
        scatter = store.write_partitioned_query(
            con,
            stage=physical_stage,
            stage_version=physical_version,
            partition_column="partition_value",
            partition_keys=partition_keys,
            select_sql=scatter_sql,
        )
        for key, (_receipt, reused) in scatter.items():
            physical_keys.append(key)
            activity[physical_stage]["reused" if reused else "written"] += 1

    physical_manifest_path = store.finalize_stage(
        con, stage=physical_stage, stage_version=physical_version,
        partition_keys=physical_keys,
    )
    physical_manifest = json.loads(physical_manifest_path.read_text(encoding="ascii"))
    physical_conservation = _require_row_conservation(
        label="l2_all_captured_source_to_physical_coverage",
        observed=int(physical_manifest["row_count"]),
        expected=sum(source_counts.values()),
        context="all_captured_dates",
    )

    for key in sorted(physical_keys):
        partition_date = key[len("date=") : len("date=") + 10]
        if not quality[partition_date]["usable_for_estimands"]:
            continue
        replay_receipt, episode_receipt, reused = _replay_one_partition(
            con,
            store,
            source_path=store._paths(physical_stage, key)[0],
            replay_stage=replay_stage,
            replay_version=replay_version,
            episode_stage=episode_stage,
            episode_version=episode_version,
            key=key,
            observation_end_ns=source_observation_ends[partition_date],
        )
        replay_keys.append(key)
        activity[replay_stage]["reused" if reused else "written"] += 1
        activity[episode_stage]["reused" if reused else "written"] += 1
        for metric, value in (replay_receipt.get("metrics", {}).get("qc") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                date_replay_qc[partition_date][metric] += value
        if replay_receipt["data"]["row_count"] < 0 or episode_receipt["data"]["row_count"] < 0:
            raise L2ResearchError(f"negative durable row count: {key}")

    replay_manifest_path = store.finalize_stage(
        con, stage=replay_stage, stage_version=replay_version,
        partition_keys=replay_keys,
    )
    episode_manifest_path = store.finalize_stage(
        con, stage=episode_stage, stage_version=episode_version,
        partition_keys=replay_keys,
    )
    replay_manifest = json.loads(replay_manifest_path.read_text(encoding="ascii"))
    episode_manifest = json.loads(episode_manifest_path.read_text(encoding="ascii"))
    replay_conservation = _require_row_conservation(
        label="l2_included_clean_source_to_replay",
        observed=int(replay_manifest["row_count"]),
        expected=sum(
            source_counts[date]
            for date in L2_CAPTURE_DATES
            if quality[date]["usable_for_estimands"]
        ),
        context="included_clean_dates_only",
    )
    replay_qc: dict[str, int] = defaultdict(int)
    for metrics in date_replay_qc.values():
        for metric, value in metrics.items():
            replay_qc[metric] += value
    rejected_rows = sum(
        value for metric, value in replay_qc.items()
        if metric.startswith("rejected_")
    )
    classified_rows = (
        replay_qc.get("snapshots_applied", 0)
        + replay_qc.get("deltas_applied", 0)
        + rejected_rows
    )
    included_source_rows = sum(
        source_counts[date] for date in L2_CAPTURE_DATES
        if quality[date]["usable_for_estimands"]
    )
    excluded_quality_rows = sum(
        source_counts[date] for date in L2_CAPTURE_DATES
        if not quality[date]["usable_for_estimands"]
    )
    classification_conservation = _require_row_conservation(
        label="l2_replay_classification",
        observed=classified_rows,
        expected=included_source_rows,
        context="included_clean_dates_only",
    )
    coverage_conservation = _require_row_conservation(
        label="l2_coverage_included_plus_excluded",
        observed=included_source_rows + excluded_quality_rows,
        expected=sum(source_counts.values()),
        context="all_captured_dates",
    )

    atlas_stage = "l2_exact_atlas"
    match_stage = "l2_matched_controls"
    atlas_version = _stage_version(abi, atlas_stage, "risk-set-survival-v2")
    match_version = _stage_version(abi, match_stage, "past-quiet-bounded-v2")
    reducer_keys: list[str] = []
    activity[atlas_stage] = {"written": 0, "reused": 0}
    activity[match_stage] = {"written": 0, "reused": 0}
    for date in analysis_dates:
        if not quality[date]["usable_for_estimands"]:
            continue
        date_keys = sorted(
            key for key in replay_keys if key.startswith(f"date={date}_")
        )
        atlas_receipt, match_receipt, reuse = _write_date_reducers(
            con,
            store,
            abi=abi,
            date=date,
            replay_paths=[store._paths(replay_stage, key)[0] for key in date_keys],
            episode_paths=[store._paths(episode_stage, key)[0] for key in date_keys],
        )
        reducer_key = f"date={date}"
        reducer_keys.append(reducer_key)
        activity[atlas_stage][
            "reused" if reuse["atlas_reused"] else "written"
        ] += 1
        activity[match_stage][
            "reused" if reuse["match_reused"] else "written"
        ] += 1
        if atlas_receipt["data"]["row_count"] < 0 or match_receipt["data"]["row_count"] < 0:
            raise L2ResearchError(f"negative L2 reducer row count: {date}")
    atlas_manifest_path = store.finalize_stage(
        con, stage=atlas_stage, stage_version=atlas_version,
        partition_keys=reducer_keys,
    )
    match_manifest_path = store.finalize_stage(
        con, stage=match_stage, stage_version=match_version,
        partition_keys=reducer_keys,
    )
    atlas_manifest = json.loads(atlas_manifest_path.read_text(encoding="ascii"))
    match_manifest = json.loads(match_manifest_path.read_text(encoding="ascii"))

    availability_stage = "l2_availability"
    availability_version = _stage_version(
        abi, availability_stage, "coverage-and-quality-disposition-v2"
    )
    con.execute("""
      CREATE OR REPLACE TEMP TABLE l2_availability_build(
        date VARCHAR,state VARCHAR,source_objects BIGINT,source_rows BIGINT,
        analysis_rows BIGINT,eligible_for_estimands BOOLEAN,
        quality_state VARCHAR,quality_sha256 VARCHAR,quality_blockers_json VARCHAR,
        absence_reason VARCHAR,universe_scope VARCHAR
      )
    """)
    availability_rows = []
    for date in L2_SCOPE_DATES:
        if date in L2_ABSENT_DATES:
            availability_rows.append((
                date, "ABSENT_NOT_CAPTURED", 0, 0, 0, False,
                "NOT_APPLICABLE", None, "[]",
                "L2 capture was not legitimately available for this exact date",
                "TARGETED_WATCHLIST_NOT_FULL_MARKET_UNIVERSE",
            ))
        else:
            included = bool(quality[date]["usable_for_estimands"])
            availability_rows.append((
                date, (
                    "CAPTURED_CLEAN_INCLUDED"
                    if included else "EXCLUDED_DATA_QUALITY"
                ),
                len(_l2_fact_objects(input_manifest, date)), source_counts[date],
                source_counts[date] if included else 0, included,
                quality[date]["receipt_state"], quality[date]["sha256"],
                json.dumps(quality[date]["blockers"], sort_keys=True),
                None,
                "TARGETED_WATCHLIST_NOT_FULL_MARKET_UNIVERSE",
            ))
    con.executemany(
        "INSERT INTO l2_availability_build VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        availability_rows,
    )
    availability_receipt, availability_reused = store.write_partition(
        con,
        stage=availability_stage,
        stage_version=availability_version,
        partition_key="scope",
        select_sql="SELECT * FROM l2_availability_build ORDER BY date",
        metrics={
            "captured_dates": list(L2_CAPTURE_DATES),
            "included_clean_dates": [
                date for date in L2_CAPTURE_DATES
                if quality[date]["usable_for_estimands"]
            ],
            "excluded_data_quality_dates": [
                date for date in L2_CAPTURE_DATES
                if not quality[date]["usable_for_estimands"]
            ],
            "explicit_absent_dates": list(L2_ABSENT_DATES),
            "all_captured_rows_in_coverage_ledger": sum(source_counts.values()),
            "included_analysis_rows": included_source_rows,
            "excluded_data_quality_rows": excluded_quality_rows,
            "universe_scope": "TARGETED_WATCHLIST_NOT_FULL_MARKET_UNIVERSE",
        },
    )
    availability_manifest_path = store.finalize_stage(
        con, stage=availability_stage, stage_version=availability_version,
        partition_keys=["scope"],
    )
    manifests = []
    for stage, version, path in (
        (availability_stage, availability_version, availability_manifest_path),
        (physical_stage, physical_version, physical_manifest_path),
        (replay_stage, replay_version, replay_manifest_path),
        (episode_stage, episode_version, episode_manifest_path),
        (atlas_stage, atlas_version, atlas_manifest_path),
        (match_stage, match_version, match_manifest_path),
    ):
        manifest = json.loads(path.read_text(encoding="ascii"))
        manifests.append({
            "stage": stage,
            "stage_version": version,
            "manifest_sha256": _sha256_file(path),
            "partition_count": manifest["partition_count"],
            "row_count": manifest["row_count"],
        })
    return {
        "schema_version": "deep03-v3-l2-snbd-execution-v2",
        "state": "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS",
        "claim_tier": "DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL",
        "source_binding": store.source_binding,
        "stage_abi": abi,
        "quality": quality,
        "availability": {
            "captured_dates": list(L2_CAPTURE_DATES),
            "included_clean_dates": [
                date for date in L2_CAPTURE_DATES
                if quality[date]["usable_for_estimands"]
            ],
            "excluded_data_quality_dates": [
                date for date in L2_CAPTURE_DATES
                if not quality[date]["usable_for_estimands"]
            ],
            "explicit_absent_dates": list(L2_ABSENT_DATES),
            "receipt": availability_receipt,
            "reused": availability_reused,
        },
        "row_accounting": {
            "all_captured_source_rows": sum(source_counts.values()),
            "physical_coverage_rows": int(physical_manifest["row_count"]),
            "included_clean_source_rows": included_source_rows,
            "excluded_data_quality_rows": excluded_quality_rows,
            "replay_rows": int(replay_manifest["row_count"]),
            "classified_rows": classified_rows,
            "rejected_rows_within_clean_dates": rejected_rows,
            "applied_snapshot_rows": replay_qc.get("snapshots_applied", 0),
            "applied_delta_rows": replay_qc.get("deltas_applied", 0),
            "rejection_class_counts": {
                key: value for key, value in sorted(replay_qc.items())
                if key.startswith("rejected_")
            },
            "per_clean_date_qc": {
                date: dict(sorted(metrics.items()))
                for date, metrics in sorted(date_replay_qc.items())
            },
        },
        "row_conservation": {
            "all_captured_physical_coverage": physical_conservation,
            "included_clean_replay": replay_conservation,
            "replay_classification": classification_conservation,
            "included_plus_excluded_coverage": coverage_conservation,
        },
        "episode_rows": int(episode_manifest["row_count"]),
        "atlas_rows": int(atlas_manifest["row_count"]),
        "matched_control_rows": int(match_manifest["row_count"]),
        "activity": activity,
        "stages": manifests,
        "universe_scope": {
            "state": "TARGETED_WATCHLIST_NOT_FULL_MARKET_UNIVERSE",
            "all_legitimately_captured_rows_within_watchlist_covered": True,
            "full_market_coverage_claim": False,
        },
        "limitations": [
            "Per-market forward ws_seq jumps are not packet-loss evidence.",
            "L2 is absent, not zero, on 2026-07-10 and 2026-07-11.",
            "2026-07-13, 2026-07-14, and 2026-07-16 are coverage-only and excluded from every estimand because of full-stream data quality.",
            "The captured L2 universe is a targeted watchlist, not every market on the exchange.",
            "Displayed depth/refill is not own-order queue position or fill probability.",
            "No fee, latency, fill, or PnL claim is made.",
        ],
    }
