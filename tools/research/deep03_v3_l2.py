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
from collections import defaultdict, deque
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
    path_list,
    quote,
)


SCHEMA_VERSION = "deep03-v3-l2-snbd-v1"
L2_SCOPE_DATES = tuple(f"2026-07-{day:02d}" for day in range(10, 18))
L2_CAPTURE_DATES = tuple(f"2026-07-{day:02d}" for day in range(12, 18))
L2_ABSENT_DATES = ("2026-07-10", "2026-07-11")
MIN_TOUCH_QTY_E4 = 10_000
MIN_DEPLETION_FRACTION = 0.50
REFILL_FRACTION = 0.80
EPISODE_HORIZON_NS = 1_000_000_000
CONTROL_SAMPLE_MODULUS = 16
MATCH_WINDOW_NS = 300_000_000_000
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
    "touch_depletion", "control_candidate",
)

EPISODE_COLUMNS = (
    "episode_id", "date", "market_ticker", "event_proxy", "sport", "family",
    "side", "depletion_ns", "observation_end_ns", "duration_us",
    "endpoint_reason", "event_observed", "refill_ns", "refill_fraction",
    "original_touch_price_e4", "pre_touch_qty_e4", "removed_e4",
    "depletion_fraction", "post_depletion_depth_e4", "snapshot_epoch",
    "topology", "spread_e4", "imbalance_depth3",
)


def _row_with_state(base: dict[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {column: (base.get(column) if column in base else state.get(column))
            for column in REPLAY_COLUMNS}


class L2ReplayEngine:
    """Replay one date+market-hash partition with bounded market state."""

    def __init__(self) -> None:
        self.books: dict[str, L2Book] = {}
        self.open_episodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.activity: dict[str, deque[int]] = defaultdict(deque)
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
            if clock_ns >= boundary:
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
            "event_proxy": str(event_proxy or market),
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
            "control_candidate": False,
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
            return _row_with_state(base, {}), []
        clock_ns = int(recv_wall_ns)
        completed = self._expire(market, clock_ns)
        self.last_clock[market] = max(clock_ns, self.last_clock.get(market, clock_ns))
        updates = self.activity[market]
        updates.append(clock_ns)
        while updates and updates[0] < clock_ns - EPISODE_HORIZON_NS:
            updates.popleft()
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
        base["top_changed"] = bool(result["top_changed"])
        base["touch_depletion"] = bool(result["touch_depletion"])
        state = result["after"]
        side = str(side_value).lower()
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
                "topology": state.get("topology"),
                "spread_e4": state.get("spread_e4"),
                "imbalance_depth3": state.get("imbalance_depth3"),
            }
            self.qc["touch_depletions"] += 1
        elif result["top_changed"] and _stable_control_sample(market, clock_ns):
            base["control_candidate"] = True
            self.qc["control_candidates"] += 1
        return _row_with_state(base, state), completed

    def finish(self) -> list[dict[str, Any]]:
        completed = []
        for key, episode in list(self.open_episodes.items()):
            market, _side = key
            last = self.last_clock.get(market, int(episode["depletion_ns"]))
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

