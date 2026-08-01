#!/usr/bin/env python3
"""ROUND4 causal table-builder scaffold.

This module is deliberately narrower than a replay or a strategy:

* discovery source paths are allowlisted before any reader is called;
* BOOK/TRADE rows are merged only on the local receipt clock;
* L2 is rebuilt from full snapshots and exact signed deltas, fail-closed;
* ``entry_episode``, ``entry_risk_interval`` and ``first_fill_state`` are
  materialized with primary-key, non-overlap and terminal-cause conservation;
* all post-fill writes remain blocked by ``ACTION_SET_PENDING``.

It contains no model, candidate ranking, network client or live-trading path.
The current implementation is a pure synthetic/offline scaffold.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re


EXPERIMENT_ID = "KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1"
CANDIDATE_STATUS = "ACTION_SET_PENDING"
DEPLOYABLE = False
LIVE_AUTHORIZED = False

DISCOVERY_DATES = frozenset(
    ("2026-07-20", "2026-07-21", "2026-07-22")
)
FORBIDDEN_DATES = frozenset(("2026-07-23", "2026-07-26"))

BOOK_PRIORITY = 0
TRADE_PRIORITY = 1
TRADE_SEQ_SENTINEL = (1 << 63) - 1
ENTRY_TIME_BINS_MS = (
    Decimal("0"),
    Decimal("1000"),
    Decimal("2000"),
    Decimal("5000"),
    Decimal("15000"),
    Decimal("30000"),
    Decimal("60000"),
    Decimal("120000"),
    Decimal("300000"),
)

CORE_TABLES = (
    "entry_episode",
    "entry_risk_interval",
    "first_fill_state",
    "postfill_zero_time_atom",
)
POSTFILL_TABLES = (
    "postfill_decision",
    "postfill_action",
    "postfill_risk_interval",
    "postfill_action_outcome",
)
DATE_TOKEN_RE = re.compile(
    r"(?<![0-9])([0-9]{4})[-_/]?([0-1][0-9])[-_/]?([0-3][0-9])(?![0-9])"
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DDL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_two_stage_tables.sql"
)


class Round4ContractError(RuntimeError):
    """A row cannot participate in the fail-closed ROUND4 scaffold."""


class ForbiddenSourceError(Round4ContractError):
    """A source path failed before-open date isolation."""


class ActionSetPendingError(Round4ContractError):
    """Post-fill action materialization is not yet authorized."""


def _iso_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ForbiddenSourceError(f"invalid source date: {text!r}") from exc


def _path_date_tokens(text: str) -> frozenset[str]:
    tokens = set()
    for match in DATE_TOKEN_RE.finditer(text):
        candidate = "-".join(match.groups())
        try:
            tokens.add(date.fromisoformat(candidate).isoformat())
        except ValueError as exc:
            raise ForbiddenSourceError(
                f"invalid date-like token in source path: {candidate}"
            ) from exc
    return frozenset(tokens)


def assert_source_path_allowed(
    path: os.PathLike[str] | str,
    source_date_utc: object,
    *,
    data_role: str = "DISCOVERY",
) -> Path:
    """Validate a source path without stat, resolve, open or glob.

    ROUND4 is currently discovery-only.  A future forward reader must be
    implemented only after an action-set seal; it must not weaken this gate.
    """
    day = _iso_date(source_date_utc)
    text = os.fspath(path)
    if not text:
        raise ForbiddenSourceError("empty source path")
    if day in FORBIDDEN_DATES:
        raise ForbiddenSourceError(f"forbidden source date: {day}")
    if data_role != "DISCOVERY":
        raise ActionSetPendingError(
            "forward source access is disabled while ACTION_SET_PENDING"
        )
    if day not in DISCOVERY_DATES:
        raise ForbiddenSourceError(
            f"date outside ROUND4 discovery allowlist: {day}"
        )
    path_dates = _path_date_tokens(text)
    forbidden = path_dates & FORBIDDEN_DATES
    if forbidden:
        raise ForbiddenSourceError(
            "forbidden-date token(s) in source path: "
            f"{sorted(forbidden)}"
        )
    if day not in path_dates:
        raise ForbiddenSourceError(
            f"source path is not positively bound to {day}: {text}"
        )
    extras = path_dates - {day}
    if extras:
        raise ForbiddenSourceError(
            "source path carries additional date token(s): "
            f"{sorted(extras)}"
        )
    return Path(text)


def preflight_source_paths(
    paths: Iterable[os.PathLike[str] | str],
    source_date_utc: object,
    *,
    data_role: str = "DISCOVERY",
) -> tuple[Path, ...]:
    """Gate the complete batch before returning any path to a reader."""
    candidates = tuple(paths)
    if not candidates:
        raise ForbiddenSourceError("empty source path batch")
    return tuple(
        assert_source_path_allowed(
            path,
            source_date_utc,
            data_role=data_role,
        )
        for path in candidates
    )


def read_preflighted_sources(
    paths: Iterable[os.PathLike[str] | str],
    source_date_utc: object,
    *,
    reader: Callable[[Path], object] | None = None,
    data_role: str = "DISCOVERY",
) -> tuple[object, ...]:
    """Read only after every path in the batch passes the date gate."""
    allowed = preflight_source_paths(
        paths,
        source_date_utc,
        data_role=data_role,
    )
    read_one = reader or (lambda path: path.read_bytes())
    return tuple(read_one(path) for path in allowed)


def _plain_int(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise Round4ContractError(f"{label}: expected plain integer")
    if positive and value <= 0:
        raise Round4ContractError(f"{label}: expected positive integer")
    return value


def _exact_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise Round4ContractError(
            f"{label}: float/bool is forbidden for exact quantity"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Round4ContractError(
            f"{label}: invalid exact decimal {value!r}"
        ) from exc
    if not result.is_finite():
        raise Round4ContractError(f"{label}: non-finite decimal")
    return result


def assert_receipt_clock(event: Mapping[str, object]) -> None:
    wall = _plain_int(
        event.get("recv_wall_ns"),
        "recv_wall_ns",
        positive=True,
    )
    _plain_int(
        event.get("recv_mono_ns"),
        "recv_mono_ns",
        positive=True,
    )
    local_us = _plain_int(
        event.get("local_recv_ts_us"),
        "local_recv_ts_us",
        positive=True,
    )
    expected = wall // 1_000
    if local_us != expected:
        raise Round4ContractError(
            "receipt-clock identity failed: "
            f"local_recv_ts_us={local_us} expected={expected}"
        )


def normalize_receipt_event(
    event: Mapping[str, object],
) -> dict[str, object]:
    row = dict(event)
    assert_receipt_clock(row)
    channel = row.get("channel")
    if channel not in ("BOOK", "TRADE"):
        raise Round4ContractError(f"unknown channel: {channel!r}")
    market = row.get("market_ticker")
    stable_id = row.get("stable_source_id")
    if not isinstance(market, str) or not market:
        raise Round4ContractError("missing market_ticker")
    if not isinstance(stable_id, str) or not stable_id:
        raise Round4ContractError("missing stable_source_id")
    if channel == "BOOK":
        if row.get("kind") not in ("snapshot", "delta"):
            raise Round4ContractError(
                f"unknown BOOK kind: {row.get('kind')!r}"
            )
        sid = _plain_int(row.get("ws_sid"), "ws_sid")
        seq = _plain_int(row.get("ws_seq"), "ws_seq")
        if sid < 0 or seq < 0:
            raise Round4ContractError("negative ws_sid/ws_seq")
    elif row.get("kind") != "trade":
        raise Round4ContractError(
            f"unknown TRADE kind: {row.get('kind')!r}"
        )
    return row


def receipt_merge_key(
    event: Mapping[str, object],
) -> tuple[int, int, int, int, str]:
    row = normalize_receipt_event(event)
    channel = str(row["channel"])
    priority = BOOK_PRIORITY if channel == "BOOK" else TRADE_PRIORITY
    seq = (
        int(row["ws_seq"])
        if channel == "BOOK"
        else TRADE_SEQ_SENTINEL
    )
    return (
        int(row["recv_wall_ns"]),
        int(row["recv_mono_ns"]),
        priority,
        seq,
        str(row["stable_source_id"]),
    )


def merge_receipt_events(
    events: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Return one deterministic BOOK-before-TRADE receipt-clock order."""
    normalized = tuple(normalize_receipt_event(event) for event in events)
    keyed = [(receipt_merge_key(row), row) for row in normalized]
    keys = [key for key, _row in keyed]
    if len(keys) != len(set(keys)):
        raise Round4ContractError("duplicate receipt merge key")
    stable_ids = [str(row["stable_source_id"]) for row in normalized]
    if len(stable_ids) != len(set(stable_ids)):
        raise Round4ContractError("duplicate stable_source_id")
    return tuple(row for _key, row in sorted(keyed, key=lambda item: item[0]))


class ReceiptBook:
    """Full-snapshot/signed-delta L2 state for one market."""

    def __init__(self, market_ticker: str):
        self.market_ticker = str(market_ticker)
        self.books: dict[str, dict[int, Decimal]] = {
            "yes": {},
            "no": {},
        }
        self.anchored = False
        self.ws_sid: int | None = None
        self.ws_seq: int | None = None
        self.last_key: tuple[int, int, int, int, str] | None = None
        self.snapshot_count = 0
        self.delta_count = 0
        self.zero_delete_count = 0
        self.reanchor_count = 0

    def _levels(
        self,
        levels: object,
        side: str,
    ) -> dict[int, Decimal]:
        if not isinstance(levels, (list, tuple)):
            raise Round4ContractError(
                f"{self.market_ticker}/{side}: malformed snapshot"
            )
        result: dict[int, Decimal] = {}
        for item in levels:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise Round4ContractError(
                    f"{self.market_ticker}/{side}: malformed level"
                )
            price = _plain_int(item[0], f"{side} snapshot price")
            quantity = _exact_decimal(
                item[1],
                f"{side} snapshot quantity",
            )
            if not 0 < price < 10_000 or quantity <= 0:
                raise Round4ContractError(
                    f"{self.market_ticker}/{side}: invalid level "
                    f"{price}/{quantity}"
                )
            if price in result:
                raise Round4ContractError(
                    f"{self.market_ticker}/{side}: duplicate price {price}"
                )
            result[price] = quantity
        return result

    def apply(self, event: Mapping[str, object]) -> None:
        row = normalize_receipt_event(event)
        if row["channel"] != "BOOK":
            raise Round4ContractError("ReceiptBook accepts BOOK rows only")
        if row["market_ticker"] != self.market_ticker:
            raise Round4ContractError("book event market mismatch")
        key = receipt_merge_key(row)
        if self.last_key is not None and key <= self.last_key:
            raise Round4ContractError(
                f"{self.market_ticker}: non-increasing receipt key"
            )
        sid = int(row["ws_sid"])
        seq = int(row["ws_seq"])
        if row["kind"] == "snapshot":
            if (
                self.anchored
                and sid == self.ws_sid
                and self.ws_seq is not None
                and seq <= self.ws_seq
            ):
                raise Round4ContractError(
                    f"{self.market_ticker}: snapshot seq regression"
                )
            self.books = {
                "yes": self._levels(row.get("yes_levels"), "yes"),
                "no": self._levels(row.get("no_levels"), "no"),
            }
            if self.anchored:
                self.reanchor_count += 1
            self.anchored = True
            self.ws_sid = sid
            self.ws_seq = seq
            self.snapshot_count += 1
        else:
            if (
                not self.anchored
                or sid != self.ws_sid
                or self.ws_seq is None
                or seq <= self.ws_seq
            ):
                raise Round4ContractError(
                    f"{self.market_ticker}: delta lacks a same-sid, "
                    "strictly-earlier snapshot/sequence anchor"
                )
            side = row.get("side")
            if side not in ("yes", "no"):
                raise Round4ContractError(
                    f"{self.market_ticker}: invalid delta side {side!r}"
                )
            price = _plain_int(row.get("price_e4"), "delta price_e4")
            delta_value = (
                row["delta_fp"]
                if "delta_fp" in row
                else row.get("delta_e4")
            )
            change = _exact_decimal(delta_value, "signed delta")
            if not 0 < price < 10_000 or change == 0:
                raise Round4ContractError(
                    f"{self.market_ticker}: invalid delta {price}/{change}"
                )
            before = self.books[str(side)].get(price, Decimal("0"))
            after = before + change
            if after < 0:
                raise Round4ContractError(
                    f"{self.market_ticker}: negative signed depth "
                    f"side={side} price={price} before={before} "
                    f"delta={change} after={after}"
                )
            if after == 0:
                self.books[str(side)].pop(price, None)
                self.zero_delete_count += 1
            else:
                self.books[str(side)][price] = after
            self.ws_seq = seq
            self.delta_count += 1
        self.last_key = key

    def causal_snapshot(self) -> dict[str, object]:
        if not self.anchored:
            raise Round4ContractError(
                f"{self.market_ticker}: snapshot requested before anchor"
            )
        return {
            "market_ticker": self.market_ticker,
            "book_ws_sid": self.ws_sid,
            "book_ws_seq": self.ws_seq,
            "yes_levels": tuple(
                sorted(self.books["yes"].items(), reverse=True)
            ),
            "no_levels": tuple(
                sorted(self.books["no"].items(), reverse=True)
            ),
            "last_receipt_key": self.last_key,
        }


def reconstruct_receipt_stream(
    events: Iterable[Mapping[str, object]],
) -> tuple[
    tuple[dict[str, object], ...],
    dict[str, ReceiptBook],
    tuple[dict[str, object], ...],
]:
    """Merge receipts, rebuild L2 and snapshot state after every event."""
    merged = merge_receipt_events(events)
    states: dict[str, ReceiptBook] = {}
    timeline = []
    for row in merged:
        market = str(row["market_ticker"])
        state = states.setdefault(market, ReceiptBook(market))
        if row["channel"] == "BOOK":
            state.apply(row)
        elif not state.anchored:
            raise Round4ContractError(
                f"{market}: trade receipt arrived before a causal snapshot"
            )
        timeline.append(
            {
                "stable_source_id": row["stable_source_id"],
                "channel": row["channel"],
                "kind": row["kind"],
                "recv_wall_ns": row["recv_wall_ns"],
                "book": state.causal_snapshot(),
            }
        )
    return merged, states, tuple(timeline)


def _required(
    row: Mapping[str, object],
    fields: Sequence[str],
    label: str,
) -> None:
    missing = [field for field in fields if row.get(field) is None]
    if missing:
        raise Round4ContractError(f"{label}: missing fields {missing}")


def _cause_vector(cause: str) -> tuple[int, int, int, int]:
    values = {
        "YES_FIRST": (1, 0, 0, 0),
        "NO_FIRST": (0, 1, 0, 0),
        "ADMIN_CENSOR_NO_FIRST_FILL": (0, 0, 1, 0),
    }
    try:
        return values[cause]
    except KeyError as exc:
        raise Round4ContractError(
            f"terminal cause has no risk-vector: {cause}"
        ) from exc


class Round4TableBuilder:
    """In-memory builder for core entry, first-fill and atom tables."""

    def __init__(self) -> None:
        self.candidate_status = CANDIDATE_STATUS
        self.deployable = DEPLOYABLE
        self.live_authorized = LIVE_AUTHORIZED
        self.entry_episodes: dict[
            tuple[str, str], dict[str, object]
        ] = {}
        self.entry_risk_intervals: dict[
            tuple[str, str, int], dict[str, object]
        ] = {}
        self.first_fill_states: dict[str, dict[str, object]] = {}
        self._first_fill_by_entry: dict[tuple[str, str], str] = {}
        self.zero_time_atoms: dict[str, dict[str, object]] = {}
        self._zero_atom_by_entry: dict[tuple[str, str], str] = {}

    def add_entry_episode(
        self,
        episode: Mapping[str, object],
    ) -> tuple[str, str]:
        row = dict(episode)
        _required(
            row,
            (
                "experiment_id",
                "data_role",
                "source_date_utc",
                "market_ticker",
                "market_cluster_id",
                "cycle_id",
                "entry_episode_id",
                "entry_action_id",
                "action_set_version",
                "source_rows_sha256",
                "decision_recv_wall_ns",
                "decision_recv_mono_ns",
                "feature_asof_wall_ns",
                "book_ws_sid",
                "book_ws_seq",
                "action_kind",
                "legal_grid",
                "yes_post_only",
                "no_post_only",
                "ack_state",
                "tte_ms",
                "spread_e4",
                "terminal_cause",
            ),
            "entry_episode",
        )
        if row["experiment_id"] != EXPERIMENT_ID:
            raise Round4ContractError("entry_episode experiment mismatch")
        if row["data_role"] != "DISCOVERY":
            raise ActionSetPendingError(
                "FORWARD rows disabled while ACTION_SET_PENDING"
            )
        source_day = _iso_date(row["source_date_utc"])
        if source_day not in DISCOVERY_DATES:
            raise ForbiddenSourceError(
                f"entry_episode date not allowed: {source_day}"
            )
        row["source_date_utc"] = source_day
        decision = _plain_int(
            row["decision_recv_wall_ns"],
            "decision_recv_wall_ns",
            positive=True,
        )
        _plain_int(
            row["decision_recv_mono_ns"],
            "decision_recv_mono_ns",
            positive=True,
        )
        feature_asof = _plain_int(
            row["feature_asof_wall_ns"],
            "feature_asof_wall_ns",
            positive=True,
        )
        if feature_asof > decision:
            raise Round4ContractError("entry feature lookahead")
        if not isinstance(row["book_ws_sid"], str) or not row["book_ws_sid"]:
            raise Round4ContractError("missing book_ws_sid")
        if _plain_int(row["book_ws_seq"], "book_ws_seq") < 0:
            raise Round4ContractError("negative book_ws_seq")
        if row["action_kind"] not in (
            "ENTRY_SKIP",
            "ENTRY_PAIR_POST_ONLY",
        ):
            raise Round4ContractError("unknown entry action kind")
        if row["terminal_cause"] == "DATA_INVALID":
            raise Round4ContractError(
                "DATA_INVALID cannot enter analytical tables; rollback the "
                "entire market-day and every action upstream"
            )
        if row["terminal_cause"] not in (
            "YES_FIRST",
            "NO_FIRST",
            "ADMIN_CENSOR_NO_FIRST_FILL",
            "ACK_FAILED",
            "ENTRY_SKIP",
        ):
            raise Round4ContractError("unknown entry terminal cause")
        if row["ack_state"] not in (
            "NOT_SENT",
            "BOTH_ACKED",
            "ONE_FAILED",
            "UNKNOWN",
        ):
            raise Round4ContractError("unknown entry ACK state")
        active = row.get("entry_active_wall_ns")
        terminal = row.get("terminal_wall_ns")
        if terminal is not None:
            terminal = _plain_int(
                terminal,
                "terminal_wall_ns",
                positive=True,
            )
            if terminal < decision:
                raise Round4ContractError("terminal precedes decision")
        if row["action_kind"] == "ENTRY_PAIR_POST_ONLY":
            _required(
                row,
                (
                    "yes_price_e4",
                    "no_price_e4",
                    "clip_fp",
                    "pair_cost_e4",
                    "locked_pair_ceiling_e4",
                ),
                "ENTRY_PAIR_POST_ONLY",
            )
            yes_price = _plain_int(row["yes_price_e4"], "yes_price_e4")
            no_price = _plain_int(row["no_price_e4"], "no_price_e4")
            clip = _exact_decimal(row["clip_fp"], "clip_fp")
            ceiling = _plain_int(
                row["locked_pair_ceiling_e4"],
                "locked_pair_ceiling_e4",
            )
            pair_cost = _plain_int(row["pair_cost_e4"], "pair_cost_e4")
            if not (
                0 < yes_price < 10_000
                and 0 < no_price < 10_000
                and clip > 0
                and pair_cost == yes_price + no_price
                and pair_cost <= ceiling
                and row["legal_grid"] is True
                and row["yes_post_only"] is True
                and row["no_post_only"] is True
            ):
                raise Round4ContractError("invalid paired entry contract")
            if row["ack_state"] == "BOTH_ACKED":
                if row["terminal_cause"] not in (
                    "YES_FIRST",
                    "NO_FIRST",
                    "ADMIN_CENSOR_NO_FIRST_FILL",
                ):
                    raise Round4ContractError(
                        "BOTH_ACKED pair has a pre-risk terminal cause"
                    )
                if active is None or terminal is None:
                    raise Round4ContractError(
                        "active paired entry needs active/terminal clocks"
                    )
                active = _plain_int(
                    active,
                    "entry_active_wall_ns",
                    positive=True,
                )
                if active < decision or terminal < active:
                    raise Round4ContractError(
                        "invalid active entry time ordering"
                    )
            else:
                if (
                    row["terminal_cause"] != "ACK_FAILED"
                    or terminal is None
                    or active is not None
                    or row.get("first_fill_wall_ns") is not None
                ):
                    raise Round4ContractError(
                        "non-ACKed pair must terminate ACK_FAILED "
                        "before entering risk"
                    )
            if row["terminal_cause"] in ("YES_FIRST", "NO_FIRST"):
                if row.get("first_fill_wall_ns") != terminal:
                    raise Round4ContractError(
                        "first-fill terminal clock does not conserve"
                    )
            elif row.get("first_fill_wall_ns") is not None:
                raise Round4ContractError(
                    "non-fill terminal cannot carry first_fill_wall_ns"
                )
        elif row["terminal_cause"] != "ENTRY_SKIP":
            raise Round4ContractError(
                "ENTRY_SKIP action must terminate ENTRY_SKIP"
            )
        key = (
            str(row["entry_episode_id"]),
            str(row["entry_action_id"]),
        )
        if key in self.entry_episodes:
            raise Round4ContractError(
                f"duplicate entry_episode primary key: {key}"
            )
        if active is not None and terminal is not None:
            for other in self.entry_episodes.values():
                other_active = other.get("entry_active_wall_ns")
                other_terminal = other.get("terminal_wall_ns")
                same_market = (
                    other["experiment_id"] == row["experiment_id"]
                    and other["source_date_utc"] == row["source_date_utc"]
                    and other["market_ticker"] == row["market_ticker"]
                )
                different_cycle = other["cycle_id"] != row["cycle_id"]
                if (
                    same_market
                    and different_cycle
                    and other_active is not None
                    and other_terminal is not None
                    and int(active) < int(other_terminal)
                    and int(other_active) < int(terminal)
                ):
                    raise Round4ContractError(
                        "overlapping active cycles in one market"
                    )
        self.entry_episodes[key] = row
        return key

    def add_entry_risk_interval(
        self,
        interval: Mapping[str, object],
    ) -> tuple[str, str, int]:
        row = dict(interval)
        _required(
            row,
            (
                "entry_episode_id",
                "entry_action_id",
                "interval_index",
                "interval_start_wall_ns",
                "interval_stop_wall_ns",
                "feature_asof_wall_ns",
                "elapsed_start_ms",
                "elapsed_stop_ms",
                "at_risk",
                "event_yes_first",
                "event_no_first",
                "admin_censor",
                "data_invalid",
            ),
            "entry_risk_interval",
        )
        entry_key = (
            str(row["entry_episode_id"]),
            str(row["entry_action_id"]),
        )
        if entry_key not in self.entry_episodes:
            raise Round4ContractError("risk interval has no entry parent")
        index = _plain_int(row["interval_index"], "interval_index")
        if index < 0:
            raise Round4ContractError("negative interval_index")
        key = entry_key + (index,)
        if key in self.entry_risk_intervals:
            raise Round4ContractError(
                f"duplicate entry_risk_interval primary key: {key}"
            )
        start = _plain_int(
            row["interval_start_wall_ns"],
            "interval_start_wall_ns",
            positive=True,
        )
        stop = _plain_int(
            row["interval_stop_wall_ns"],
            "interval_stop_wall_ns",
            positive=True,
        )
        asof = _plain_int(
            row["feature_asof_wall_ns"],
            "feature_asof_wall_ns",
            positive=True,
        )
        elapsed_start = _exact_decimal(
            row["elapsed_start_ms"],
            "elapsed_start_ms",
        )
        elapsed_stop = _exact_decimal(
            row["elapsed_stop_ms"],
            "elapsed_stop_ms",
        )
        if stop <= start or asof > start:
            raise Round4ContractError("invalid/no-lookahead risk clocks")
        if elapsed_start < 0 or elapsed_stop <= elapsed_start:
            raise Round4ContractError("invalid elapsed risk interval")
        flags = (
            row["event_yes_first"],
            row["event_no_first"],
            row["admin_censor"],
            row["data_invalid"],
        )
        if any(type(value) is not int or value not in (0, 1) for value in flags):
            raise Round4ContractError("risk flags must be integer 0/1")
        if sum(flags) > 1:
            raise Round4ContractError("competing terminal causes overlap")
        if row["data_invalid"] != 0:
            raise Round4ContractError(
                "DATA_INVALID cannot enter a risk interval; rollback the "
                "entire market-day and every action upstream"
            )
        if row["at_risk"] is not True:
            raise Round4ContractError("materialized interval is not at risk")
        self.entry_risk_intervals[key] = row
        return key

    def materialize_entry_risk_intervals(
        self,
        entry_key: tuple[str, str],
    ) -> tuple[dict[str, object], ...]:
        if entry_key not in self.entry_episodes:
            raise Round4ContractError("unknown entry episode")
        episode = self.entry_episodes[entry_key]
        if episode["ack_state"] != "BOTH_ACKED":
            raise Round4ContractError(
                "risk starts only after both entry orders ACK"
            )
        active = int(episode["entry_active_wall_ns"])
        terminal = int(episode["terminal_wall_ns"])
        duration_ns = terminal - active
        if duration_ns <= 0:
            raise Round4ContractError(
                "zero-duration first event needs an explicit tie receipt row"
            )
        duration_ms = Decimal(duration_ns) / Decimal("1000000")
        if duration_ms > ENTRY_TIME_BINS_MS[-1]:
            raise Round4ContractError("entry episode exceeds sealed 300s horizon")
        feature_names = (
            "yes_same_price_ahead_fp",
            "no_same_price_ahead_fp",
            "yes_better_depth_fp",
            "no_better_depth_fp",
            "yes_flow_10s_fp",
            "no_flow_10s_fp",
            "yes_flow_60s_fp",
            "no_flow_60s_fp",
            "touch_imbalance",
            "spread_e4",
            "mid_move_1s_e4",
            "mid_move_10s_e4",
            "tte_ms",
        )
        rows = []
        for index, (left, right) in enumerate(
            zip(ENTRY_TIME_BINS_MS, ENTRY_TIME_BINS_MS[1:])
        ):
            if left >= duration_ms:
                break
            stop_elapsed = min(right, duration_ms)
            start_ns = active + int(left * Decimal("1000000"))
            stop_ns = active + int(
                stop_elapsed * Decimal("1000000")
            )
            is_terminal = stop_elapsed == duration_ms
            flags = (
                _cause_vector(str(episode["terminal_cause"]))
                if is_terminal
                else (0, 0, 0, 0)
            )
            row: dict[str, object] = {
                "entry_episode_id": entry_key[0],
                "entry_action_id": entry_key[1],
                "interval_index": index,
                "interval_start_wall_ns": start_ns,
                "interval_stop_wall_ns": stop_ns,
                "feature_asof_wall_ns": episode[
                    "feature_asof_wall_ns"
                ],
                "elapsed_start_ms": left,
                "elapsed_stop_ms": stop_elapsed,
                "at_risk": True,
                "event_yes_first": flags[0],
                "event_no_first": flags[1],
                "admin_censor": flags[2],
                "data_invalid": flags[3],
                "yes_order_age_ms": int(left),
                "no_order_age_ms": int(left),
            }
            for feature in feature_names:
                if feature in episode:
                    row[feature] = episode[feature]
            self.add_entry_risk_interval(row)
            rows.append(row)
        return tuple(rows)

    def add_first_fill_state(
        self,
        first_fill: Mapping[str, object],
    ) -> str:
        row = dict(first_fill)
        _required(
            row,
            (
                "postfill_episode_id",
                "entry_episode_id",
                "entry_action_id",
                "source_rows_sha256",
                "first_fill_side",
                "first_fill_price_e4",
                "first_fill_qty_fp",
                "first_fill_fee_usd",
                "first_fill_recv_wall_ns",
                "first_fill_recv_mono_ns",
                "feature_asof_wall_ns",
                "first_fill_elapsed_ms",
                "complement_order_id",
                "complement_side",
                "complement_price_e4",
                "complement_remaining_qty_fp",
                "complement_order_age_ms",
                "spread_e4",
                "pair_gain_if_complement_usd",
                "buy_complement_exit_pnl_usd",
                "sell_first_exit_pnl_usd",
                "buy_complement_executable_qty_fp",
                "sell_first_executable_qty_fp",
                "tte_ms",
                "cancel_state",
            ),
            "first_fill_state",
        )
        entry_key = (
            str(row["entry_episode_id"]),
            str(row["entry_action_id"]),
        )
        episode = self.entry_episodes.get(entry_key)
        if episode is None:
            raise Round4ContractError("first fill has no entry parent")
        postfill_id = str(row["postfill_episode_id"])
        if (
            postfill_id in self.first_fill_states
            or entry_key in self._first_fill_by_entry
        ):
            raise Round4ContractError(
                "duplicate first_fill_state primary/entry key"
            )
        side = row["first_fill_side"]
        complement = row["complement_side"]
        expected_side = {
            "YES_FIRST": "YES",
            "NO_FIRST": "NO",
        }.get(str(episode["terminal_cause"]))
        if (
            side not in ("YES", "NO")
            or complement not in ("YES", "NO")
            or side == complement
            or side != expected_side
        ):
            raise Round4ContractError("first-fill side/cause mismatch")
        fill_wall = _plain_int(
            row["first_fill_recv_wall_ns"],
            "first_fill_recv_wall_ns",
            positive=True,
        )
        _plain_int(
            row["first_fill_recv_mono_ns"],
            "first_fill_recv_mono_ns",
            positive=True,
        )
        feature_asof = _plain_int(
            row["feature_asof_wall_ns"],
            "feature_asof_wall_ns",
            positive=True,
        )
        if (
            fill_wall != episode.get("first_fill_wall_ns")
            or fill_wall != episode.get("terminal_wall_ns")
            or feature_asof > fill_wall
        ):
            raise Round4ContractError(
                "first-fill clock/linkage conservation failed"
            )
        expected_elapsed = (
            Decimal(fill_wall - int(episode["entry_active_wall_ns"]))
            / Decimal("1000000")
        )
        if (
            _exact_decimal(
                row["first_fill_elapsed_ms"],
                "first_fill_elapsed_ms",
            )
            != expected_elapsed
        ):
            raise Round4ContractError(
                "first-fill elapsed clock does not conserve"
            )
        if row["source_rows_sha256"] != episode["source_rows_sha256"]:
            raise Round4ContractError("first-fill source lineage mismatch")
        first_qty = _exact_decimal(
            row["first_fill_qty_fp"],
            "first_fill_qty_fp",
        )
        remaining = _exact_decimal(
            row["complement_remaining_qty_fp"],
            "complement_remaining_qty_fp",
        )
        clip = _exact_decimal(episode["clip_fp"], "entry clip_fp")
        if not (Decimal("0") < first_qty <= clip):
            raise Round4ContractError("first-fill quantity exceeds entry clip")
        if not (Decimal("0") <= remaining <= clip):
            raise Round4ContractError(
                "complement remaining quantity exceeds entry clip"
            )
        if _exact_decimal(row["first_fill_fee_usd"], "first fill fee") < 0:
            raise Round4ContractError("negative first-fill fee")
        for field in (
            "first_fill_price_e4",
            "complement_price_e4",
        ):
            price = _plain_int(row[field], field)
            if not 0 < price < 10_000:
                raise Round4ContractError(f"invalid {field}")
        self.first_fill_states[postfill_id] = row
        self._first_fill_by_entry[entry_key] = postfill_id
        return postfill_id

    def add_zero_time_atom(
        self,
        atom: Mapping[str, object],
    ) -> str:
        """Materialize a same-envelope complement completion at elapsed zero."""
        row = dict(atom)
        _required(
            row,
            (
                "postfill_episode_id",
                "entry_episode_id",
                "entry_action_id",
                "source_rows_sha256",
                "first_fill_side",
                "complement_side",
                "atom_recv_wall_ns",
                "atom_recv_mono_ns",
                "receipt_envelope_id",
                "first_fill_stable_source_id",
                "complement_fill_stable_source_id",
                "complement_fill_price_e4",
                "complement_fill_qty_fp",
                "complement_fill_fee_usd",
                "reconciliation_ok",
            ),
            "postfill_zero_time_atom",
        )
        postfill_id = str(row["postfill_episode_id"])
        entry_key = (
            str(row["entry_episode_id"]),
            str(row["entry_action_id"]),
        )
        state = self.first_fill_states.get(postfill_id)
        if state is None:
            raise Round4ContractError(
                "zero-time atom has no first_fill_state parent"
            )
        if (
            entry_key
            != (
                str(state["entry_episode_id"]),
                str(state["entry_action_id"]),
            )
            or entry_key in self._zero_atom_by_entry
            or postfill_id in self.zero_time_atoms
        ):
            raise Round4ContractError(
                "zero-time atom primary/entry linkage is not unique"
            )
        if (
            row["first_fill_side"] != state["first_fill_side"]
            or row["complement_side"] != state["complement_side"]
            or row["first_fill_side"] == row["complement_side"]
        ):
            raise Round4ContractError("zero-time atom side mismatch")
        if row["source_rows_sha256"] != state["source_rows_sha256"]:
            raise Round4ContractError("zero-time atom lineage mismatch")
        if (
            _plain_int(
                row["atom_recv_wall_ns"],
                "atom_recv_wall_ns",
                positive=True,
            )
            != int(state["first_fill_recv_wall_ns"])
            or _plain_int(
                row["atom_recv_mono_ns"],
                "atom_recv_mono_ns",
                positive=True,
            )
            != int(state["first_fill_recv_mono_ns"])
        ):
            raise Round4ContractError(
                "zero-time atom must share the first-fill receipt envelope"
            )
        envelope = row["receipt_envelope_id"]
        first_id = row["first_fill_stable_source_id"]
        complement_id = row["complement_fill_stable_source_id"]
        if (
            not isinstance(envelope, str)
            or not envelope
            or not isinstance(first_id, str)
            or not isinstance(complement_id, str)
            or not first_id
            or not complement_id
            or first_id >= complement_id
        ):
            raise Round4ContractError(
                "zero-time atom stable ordering/envelope is invalid"
            )
        price = _plain_int(
            row["complement_fill_price_e4"],
            "complement_fill_price_e4",
        )
        quantity = _exact_decimal(
            row["complement_fill_qty_fp"],
            "complement_fill_qty_fp",
        )
        first_quantity = _exact_decimal(
            state["first_fill_qty_fp"],
            "first_fill_qty_fp",
        )
        remaining = _exact_decimal(
            state["complement_remaining_qty_fp"],
            "complement_remaining_qty_fp",
        )
        fee = _exact_decimal(
            row["complement_fill_fee_usd"],
            "complement_fill_fee_usd",
        )
        if (
            not 0 < price < 10_000
            or quantity != first_quantity
            or remaining != 0
            or fee < 0
            or row["reconciliation_ok"] is not True
        ):
            raise Round4ContractError(
                "zero-time atom quantity/price/ledger does not conserve"
            )
        self.zero_time_atoms[postfill_id] = row
        self._zero_atom_by_entry[entry_key] = postfill_id
        return postfill_id

    def postfill_interface(self) -> dict[str, object]:
        return {
            "candidate_status": self.candidate_status,
            "tables": POSTFILL_TABLES,
            "writes_enabled": False,
            "fit_enabled": False,
            "candidate_selection_enabled": False,
        }

    def add_postfill_row(
        self,
        table: str,
        _row: Mapping[str, object],
    ) -> None:
        if table not in POSTFILL_TABLES:
            raise Round4ContractError(f"unknown postfill table: {table}")
        raise ActionSetPendingError(
            f"{table} is blocked until clean postfill discovery "
            "and ACTION_SET_SEAL"
        )

    def validate_core(self) -> dict[str, object]:
        intervals_by_entry: dict[
            tuple[str, str], list[dict[str, object]]
        ] = {}
        for key, row in self.entry_risk_intervals.items():
            intervals_by_entry.setdefault(key[:2], []).append(row)
        first_causes = {"YES_FIRST", "NO_FIRST"}
        for entry_key, episode in self.entry_episodes.items():
            cause = str(episode["terminal_cause"])
            rows = sorted(
                intervals_by_entry.get(entry_key, []),
                key=lambda row: int(row["interval_index"]),
            )
            risk_expected = episode["ack_state"] == "BOTH_ACKED"
            if not risk_expected:
                if rows:
                    raise Round4ContractError(
                        "pre-risk episode has risk intervals"
                    )
                continue
            if not rows:
                raise Round4ContractError(
                    "active episode has no risk intervals"
                )
            expected_indices = list(range(len(rows)))
            if [int(row["interval_index"]) for row in rows] != expected_indices:
                raise Round4ContractError("risk interval index gap")
            active = int(episode["entry_active_wall_ns"])
            terminal = int(episode["terminal_wall_ns"])
            if (
                int(rows[0]["interval_start_wall_ns"]) != active
                or int(rows[-1]["interval_stop_wall_ns"]) != terminal
                or _exact_decimal(
                    rows[0]["elapsed_start_ms"],
                    "elapsed_start_ms",
                )
                != 0
            ):
                raise Round4ContractError(
                    "risk intervals do not cover entry episode"
                )
            for index, row in enumerate(rows):
                elapsed_start = _exact_decimal(
                    row["elapsed_start_ms"],
                    "elapsed_start_ms",
                )
                elapsed_stop = _exact_decimal(
                    row["elapsed_stop_ms"],
                    "elapsed_stop_ms",
                )
                expected_start = (
                    Decimal(active)
                    + elapsed_start * Decimal("1000000")
                )
                expected_stop = (
                    Decimal(active)
                    + elapsed_stop * Decimal("1000000")
                )
                if (
                    expected_start
                    != Decimal(int(row["interval_start_wall_ns"]))
                    or expected_stop
                    != Decimal(int(row["interval_stop_wall_ns"]))
                ):
                    raise Round4ContractError(
                        "absolute/elapsed risk clocks do not conserve"
                    )
                row_flags = sum(
                    int(row[field])
                    for field in (
                        "event_yes_first",
                        "event_no_first",
                        "admin_censor",
                        "data_invalid",
                    )
                )
                if index < len(rows) - 1 and row_flags != 0:
                    raise Round4ContractError(
                        "terminal cause appears before the final interval"
                    )
            for left, right in zip(rows, rows[1:]):
                if (
                    int(left["interval_stop_wall_ns"])
                    != int(right["interval_start_wall_ns"])
                    or _exact_decimal(
                        left["elapsed_stop_ms"],
                        "elapsed_stop_ms",
                    )
                    != _exact_decimal(
                        right["elapsed_start_ms"],
                        "elapsed_start_ms",
                    )
                ):
                    raise Round4ContractError(
                        "risk intervals overlap or leave a gap"
                    )
            observed = tuple(
                sum(int(row[field]) for row in rows)
                for field in (
                    "event_yes_first",
                    "event_no_first",
                    "admin_censor",
                    "data_invalid",
                )
            )
            if observed != _cause_vector(cause):
                raise Round4ContractError(
                    "risk terminal-cause conservation failed"
                )
            state_exists = entry_key in self._first_fill_by_entry
            if (cause in first_causes) != state_exists:
                raise Round4ContractError(
                    "first-fill state existence does not conserve"
                )
        return {
            "experiment_id": EXPERIMENT_ID,
            "candidate_status": self.candidate_status,
            "deployable": self.deployable,
            "live_authorized": self.live_authorized,
            "entry_episode_rows": len(self.entry_episodes),
            "entry_risk_interval_rows": len(
                self.entry_risk_intervals
            ),
            "first_fill_state_rows": len(self.first_fill_states),
            "postfill_zero_time_atom_rows": len(self.zero_time_atoms),
            "postfill": self.postfill_interface(),
            "status": "ROUND4_CORE_SCAFFOLD_VALID",
        }

    def insert_core_tables(self, connection: object) -> dict[str, object]:
        """Validate, then insert the authorized core tables transactionally."""
        receipt = self.validate_core()
        rows_by_table = {
            "entry_episode": tuple(self.entry_episodes.values()),
            "entry_risk_interval": tuple(
                self.entry_risk_intervals.values()
            ),
            "first_fill_state": tuple(self.first_fill_states.values()),
            "postfill_zero_time_atom": tuple(
                self.zero_time_atoms.values()
            ),
        }
        connection.execute("BEGIN TRANSACTION")
        try:
            for table in CORE_TABLES:
                table_columns = {
                    str(info[1])
                    for info in connection.execute(
                        f"PRAGMA table_info('{table}')"
                    ).fetchall()
                }
                for row in rows_by_table[table]:
                    columns = tuple(row.keys())
                    unknown = set(columns) - table_columns
                    if unknown:
                        raise Round4ContractError(
                            f"{table}: fields outside sealed DDL: "
                            f"{sorted(unknown)}"
                        )
                    placeholders = ", ".join("?" for _ in columns)
                    names = ", ".join(columns)
                    connection.execute(
                        f"INSERT INTO {table} ({names}) "
                        f"VALUES ({placeholders})",
                        [row[name] for name in columns],
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return receipt


def create_round4_schema(
    connection: object,
    ddl_path: os.PathLike[str] | str = DEFAULT_DDL_PATH,
) -> Path:
    """Create the sealed empty schema; this reads code, not source data."""
    path = Path(ddl_path)
    connection.execute(path.read_text())
    return path
