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
    bounded_source_binding,
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
    "control_candidate": "BOOLEAN",
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
    "topology": "VARCHAR",
    "spread_e4": "BIGINT",
    "imbalance_depth3": "DOUBLE",
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
        "schema_version": "deep03-v3-l2-snbd-stage-abi-v1",
        "module_sha256": _sha256_file(Path(__file__).resolve()),
        "market_partition_algorithm": "duckdb-hash-v1-modulo-plus-null-bucket",
        "market_bucket_count": market_buckets,
        "capture_dates": list(L2_CAPTURE_DATES),
        "explicit_absent_dates": list(L2_ABSENT_DATES),
        "replay_columns": list(REPLAY_COLUMNS),
        "episode_columns": list(EPISODE_COLUMNS),
        "episode_horizon_ns": EPISODE_HORIZON_NS,
        "min_touch_qty_e4": MIN_TOUCH_QTY_E4,
        "min_depletion_fraction": MIN_DEPLETION_FRACTION,
        "refill_fraction": REFILL_FRACTION,
        "per_market_forward_ws_seq_gap_inference_used": False,
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
    _insert_dict_rows(con, "l2_episode_build", EPISODE_COLUMNS, engine.finish())
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
    quality = {
        date: _load_quality_assessment(input_manifest, date)
        for date in L2_CAPTURE_DATES
    }
    blocked = {
        date: row["blockers"]
        for date, row in quality.items()
        if row["state"] != "PASS"
    }
    if blocked:
        raise L2ResearchError(
            "L2 full-stream sequence receipt refused captured date(s): "
            + json.dumps(blocked, sort_keys=True)
        )

    physical_stage = "l2_physical"
    replay_stage = "l2_replay"
    episode_stage = "l2_episodes"
    physical_version = _stage_version(abi, physical_stage, "exact-source-v1")
    replay_version = _stage_version(abi, replay_stage, "sequence-replay-v1")
    episode_version = _stage_version(abi, episode_stage, "lifecycle-v1")
    bucket_values = range(market_buckets + 1)  # final value is NULL-market bucket
    physical_keys: list[str] = []
    replay_keys: list[str] = []
    source_counts: dict[str, int] = {}
    activity = {
        physical_stage: {"written": 0, "reused": 0},
        replay_stage: {"written": 0, "reused": 0},
        episode_stage: {"written": 0, "reused": 0},
    }

    for date in L2_CAPTURE_DATES:
        objects = _l2_fact_objects(input_manifest, date)
        typed = _normalized_l2_sql(con, objects, date)
        source_count = int(con.execute(f"SELECT count(*) FROM ({typed})").fetchone()[0])
        source_counts[date] = source_count
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
    _require_row_conservation(
        label="l2_source_to_physical",
        observed=int(physical_manifest["row_count"]),
        expected=sum(source_counts.values()),
        context="all_captured_dates",
    )

    for key in sorted(physical_keys):
        replay_receipt, episode_receipt, reused = _replay_one_partition(
            con,
            store,
            source_path=store._paths(physical_stage, key)[0],
            replay_stage=replay_stage,
            replay_version=replay_version,
            episode_stage=episode_stage,
            episode_version=episode_version,
            key=key,
        )
        replay_keys.append(key)
        activity[replay_stage]["reused" if reused else "written"] += 1
        activity[episode_stage]["reused" if reused else "written"] += 1
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
        label="l2_physical_to_replay",
        observed=int(replay_manifest["row_count"]),
        expected=int(physical_manifest["row_count"]),
        context="all_captured_dates",
    )

    availability_stage = "l2_availability"
    availability_version = _stage_version(
        abi, availability_stage, "captured-vs-explicit-absent-v1"
    )
    con.execute("""
      CREATE OR REPLACE TEMP TABLE l2_availability_build(
        date VARCHAR,state VARCHAR,source_objects BIGINT,source_rows BIGINT,
        quality_state VARCHAR,quality_sha256 VARCHAR,absence_reason VARCHAR
      )
    """)
    availability_rows = []
    for date in L2_SCOPE_DATES:
        if date in L2_ABSENT_DATES:
            availability_rows.append((
                date, "ABSENT_NOT_CAPTURED", 0, 0, "NOT_APPLICABLE", None,
                "L2 capture was not legitimately available for this exact date",
            ))
        else:
            availability_rows.append((
                date, "CAPTURED_SEQUENCE_RECEIPT_PASS",
                len(_l2_fact_objects(input_manifest, date)), source_counts[date],
                quality[date]["state"], quality[date]["sha256"], None,
            ))
    con.executemany(
        "INSERT INTO l2_availability_build VALUES (?,?,?,?,?,?,?)",
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
            "explicit_absent_dates": list(L2_ABSENT_DATES),
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
        "schema_version": "deep03-v3-l2-snbd-execution-v1",
        "state": "COMPLETE",
        "claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
        "source_binding": store.source_binding,
        "stage_abi": abi,
        "quality": quality,
        "availability": {
            "captured_dates": list(L2_CAPTURE_DATES),
            "explicit_absent_dates": list(L2_ABSENT_DATES),
            "receipt": availability_receipt,
            "reused": availability_reused,
        },
        "row_conservation": replay_conservation,
        "episode_rows": int(episode_manifest["row_count"]),
        "activity": activity,
        "stages": manifests,
        "limitations": [
            "Per-market forward ws_seq jumps are not packet-loss evidence.",
            "L2 is absent, not zero, on 2026-07-10 and 2026-07-11.",
            "Displayed depth/refill is not own-order queue position or fill probability.",
            "No fee, latency, fill, or PnL claim is made.",
        ],
    }
