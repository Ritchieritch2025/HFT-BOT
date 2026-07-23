"""Append-only research risk ledger.

Reserved worst-case loss remains charged while quantity is either an open
order or a filled position.  It is released only by an explicit cancel of
unfilled quantity, an executed exit, or a source-finalized settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .contracts import (
    canonical_sha256,
    ceil_div,
    require_int,
    require_nonempty,
    require_sha256,
)
from .terminal import TerminalDecision


class RiskInvariantError(RuntimeError):
    """A risk lifecycle or conservation invariant was violated."""


class RiskLimitExceeded(RiskInvariantError):
    """A prospective reservation exceeds a configured cap."""


def _utc_date_from_ns(value: int) -> str:
    require_int("occurred_at_ns", value, minimum=0)
    try:
        return datetime.fromtimestamp(
            value // 1_000_000_000,
            tz=timezone.utc,
        ).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise RiskInvariantError(
            "risk event timestamp is outside UTC date range"
        ) from exc


class RiskEventKind(str, Enum):
    RESERVE = "RESERVE"
    FILL = "FILL"
    CANCEL = "CANCEL"
    EXIT = "EXIT"
    SETTLEMENT = "SETTLEMENT"
    REALIZED_PNL = "REALIZED_PNL"


@dataclass(frozen=True)
class RiskLimits:
    max_market_e6: int
    max_event_e6: int
    max_factor_e6: int
    max_total_e6: int
    max_daily_loss_e6: int

    def __post_init__(self) -> None:
        for name in (
            "max_market_e6",
            "max_event_e6",
            "max_factor_e6",
            "max_total_e6",
            "max_daily_loss_e6",
        ):
            require_int(name, getattr(self, name), minimum=0)


@dataclass(frozen=True)
class RiskRequest:
    reservation_id: str
    path_id: str
    market_ticker: str
    event_ticker: str
    factor_key: str
    quantity_e4: int
    worst_case_loss_e6: int
    requested_at_ns: int
    source_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "path_id",
            "market_ticker",
            "event_ticker",
            "factor_key",
        ):
            require_nonempty(name, getattr(self, name))
        require_int("quantity_e4", self.quantity_e4, minimum=1)
        require_int(
            "worst_case_loss_e6",
            self.worst_case_loss_e6,
            minimum=0,
        )
        require_int("requested_at_ns", self.requested_at_ns, minimum=0)
        require_sha256("source_sha256", self.source_sha256)


@dataclass(frozen=True)
class RiskEvent:
    sequence: int
    event_id: str
    kind: RiskEventKind
    reservation_id: str | None
    path_id: str
    occurred_at_ns: int
    open_delta_e4: int
    position_delta_e4: int
    realized_pnl_e6: int
    held_risk_after_e6: int
    source_sha256: str

    def __post_init__(self) -> None:
        require_int("sequence", self.sequence, minimum=0)
        require_nonempty("event_id", self.event_id)
        if not isinstance(self.kind, RiskEventKind):
            raise RiskInvariantError("kind must be a RiskEventKind")
        if self.reservation_id is not None:
            require_nonempty("reservation_id", self.reservation_id)
        require_nonempty("path_id", self.path_id)
        require_int("occurred_at_ns", self.occurred_at_ns, minimum=0)
        require_int("open_delta_e4", self.open_delta_e4)
        require_int("position_delta_e4", self.position_delta_e4)
        require_int("realized_pnl_e6", self.realized_pnl_e6)
        require_int("held_risk_after_e6", self.held_risk_after_e6, minimum=0)
        require_sha256("source_sha256", self.source_sha256)


@dataclass(frozen=True)
class RiskReservationSnapshot:
    reservation_id: str
    path_id: str
    market_ticker: str
    event_ticker: str
    factor_key: str
    quantity_e4: int
    open_quantity_e4: int
    position_quantity_e4: int
    canceled_quantity_e4: int
    exited_quantity_e4: int
    settled_quantity_e4: int
    worst_case_loss_e6: int
    held_risk_e6: int


@dataclass
class _ReservationState:
    request: RiskRequest
    open_quantity_e4: int
    position_quantity_e4: int = 0
    canceled_quantity_e4: int = 0
    exited_quantity_e4: int = 0
    settled_quantity_e4: int = 0

    @property
    def held_quantity_e4(self) -> int:
        return self.open_quantity_e4 + self.position_quantity_e4

    @property
    def held_risk_e6(self) -> int:
        if self.held_quantity_e4 == 0:
            return 0
        return ceil_div(
            self.request.worst_case_loss_e6 * self.held_quantity_e4,
            self.request.quantity_e4,
        )

    def assert_conservation(self) -> None:
        total = (
            self.open_quantity_e4
            + self.position_quantity_e4
            + self.canceled_quantity_e4
            + self.exited_quantity_e4
            + self.settled_quantity_e4
        )
        if total != self.request.quantity_e4:
            raise RiskInvariantError(
                f"reservation {self.request.reservation_id} quantity "
                f"conservation failed"
            )

    def snapshot(self) -> RiskReservationSnapshot:
        return RiskReservationSnapshot(
            reservation_id=self.request.reservation_id,
            path_id=self.request.path_id,
            market_ticker=self.request.market_ticker,
            event_ticker=self.request.event_ticker,
            factor_key=self.request.factor_key,
            quantity_e4=self.request.quantity_e4,
            open_quantity_e4=self.open_quantity_e4,
            position_quantity_e4=self.position_quantity_e4,
            canceled_quantity_e4=self.canceled_quantity_e4,
            exited_quantity_e4=self.exited_quantity_e4,
            settled_quantity_e4=self.settled_quantity_e4,
            worst_case_loss_e6=self.request.worst_case_loss_e6,
            held_risk_e6=self.held_risk_e6,
        )


class RiskLedger:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits
        self._states: dict[str, _ReservationState] = {}
        self._events: list[RiskEvent] = []
        self._event_ids: set[str] = set()
        self._last_occurred_at_ns = -1
        self._realized_pnl_by_utc_date_e6: dict[str, int] = {}
        self._daily_loss_breached_utc_dates: set[str] = set()

    @property
    def events(self) -> tuple[RiskEvent, ...]:
        return tuple(self._events)

    @property
    def daily_realized_pnl_e6(self) -> int:
        return sum(self._realized_pnl_by_utc_date_e6.values())

    @property
    def realized_pnl_by_utc_date_e6(self) -> dict[str, int]:
        return dict(self._realized_pnl_by_utc_date_e6)

    @property
    def daily_loss_breached_utc_dates(self) -> frozenset[str]:
        return frozenset(self._daily_loss_breached_utc_dates)

    @property
    def deterministic_sha256(self) -> str:
        return canonical_sha256(
            {
                "limits": self._limits,
                "events": self.events,
                "realized_pnl_by_utc_date_e6": (
                    self._realized_pnl_by_utc_date_e6
                ),
                "daily_loss_breached_utc_dates": sorted(
                    self._daily_loss_breached_utc_dates
                ),
            }
        )

    def reservation(self, reservation_id: str) -> RiskReservationSnapshot:
        try:
            return self._states[reservation_id].snapshot()
        except KeyError as exc:
            raise RiskInvariantError(
                f"unknown reservation {reservation_id}"
            ) from exc

    def _current_exposure(
        self,
        *,
        market_ticker: str | None = None,
        event_ticker: str | None = None,
        factor_key: str | None = None,
    ) -> int:
        total = 0
        for state in self._states.values():
            request = state.request
            if (
                market_ticker is not None
                and request.market_ticker != market_ticker
            ):
                continue
            if event_ticker is not None and request.event_ticker != event_ticker:
                continue
            if factor_key is not None and request.factor_key != factor_key:
                continue
            total += state.held_risk_e6
        return total

    @property
    def total_exposure_e6(self) -> int:
        return self._current_exposure()

    def _validate_event(
        self,
        *,
        event_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        require_nonempty("event_id", event_id)
        require_int("occurred_at_ns", occurred_at_ns, minimum=0)
        require_sha256("source_sha256", source_sha256)
        if event_id in self._event_ids:
            raise RiskInvariantError(f"duplicate risk event {event_id}")
        if occurred_at_ns < self._last_occurred_at_ns:
            raise RiskInvariantError("risk events must be append-time monotone")

    def _append(
        self,
        *,
        event_id: str,
        kind: RiskEventKind,
        state: _ReservationState | None,
        path_id: str,
        occurred_at_ns: int,
        open_delta_e4: int,
        position_delta_e4: int,
        realized_pnl_e6: int,
        source_sha256: str,
    ) -> None:
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=occurred_at_ns,
            source_sha256=source_sha256,
        )
        event = RiskEvent(
            sequence=len(self._events),
            event_id=event_id,
            kind=kind,
            reservation_id=(
                state.request.reservation_id if state is not None else None
            ),
            path_id=path_id,
            occurred_at_ns=occurred_at_ns,
            open_delta_e4=open_delta_e4,
            position_delta_e4=position_delta_e4,
            realized_pnl_e6=realized_pnl_e6,
            held_risk_after_e6=(
                state.held_risk_e6 if state is not None else 0
            ),
            source_sha256=source_sha256,
        )
        self._events.append(event)
        self._event_ids.add(event_id)
        self._last_occurred_at_ns = occurred_at_ns

    def reserve(self, request: RiskRequest, *, event_id: str) -> None:
        if request.reservation_id in self._states:
            raise RiskInvariantError(
                f"duplicate reservation {request.reservation_id}"
            )
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=request.requested_at_ns,
            source_sha256=request.source_sha256,
        )
        request_date = _utc_date_from_ns(request.requested_at_ns)
        if request_date in self._daily_loss_breached_utc_dates:
            raise RiskLimitExceeded("daily realized loss gate is closed")
        prospective = {
            "market": self._current_exposure(
                market_ticker=request.market_ticker
            ) + request.worst_case_loss_e6,
            "event": self._current_exposure(
                event_ticker=request.event_ticker
            ) + request.worst_case_loss_e6,
            "factor": self._current_exposure(
                factor_key=request.factor_key
            ) + request.worst_case_loss_e6,
            "total": self.total_exposure_e6 + request.worst_case_loss_e6,
        }
        limits = {
            "market": self._limits.max_market_e6,
            "event": self._limits.max_event_e6,
            "factor": self._limits.max_factor_e6,
            "total": self._limits.max_total_e6,
        }
        exceeded = [
            name for name in prospective
            if prospective[name] > limits[name]
        ]
        if exceeded:
            raise RiskLimitExceeded(
                "risk limits exceeded: " + ",".join(exceeded)
            )
        state = _ReservationState(
            request=request,
            open_quantity_e4=request.quantity_e4,
        )
        state.assert_conservation()
        self._states[request.reservation_id] = state
        self._append(
            event_id=event_id,
            kind=RiskEventKind.RESERVE,
            state=state,
            path_id=request.path_id,
            occurred_at_ns=request.requested_at_ns,
            open_delta_e4=request.quantity_e4,
            position_delta_e4=0,
            realized_pnl_e6=0,
            source_sha256=request.source_sha256,
        )

    def record_fill(
        self,
        reservation_id: str,
        *,
        quantity_e4: int,
        event_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        state = self._state_for(reservation_id)
        require_int("quantity_e4", quantity_e4, minimum=1)
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=occurred_at_ns,
            source_sha256=source_sha256,
        )
        if quantity_e4 > state.open_quantity_e4:
            raise RiskInvariantError("fill exceeds open reserved quantity")
        state.open_quantity_e4 -= quantity_e4
        state.position_quantity_e4 += quantity_e4
        state.assert_conservation()
        self._append(
            event_id=event_id,
            kind=RiskEventKind.FILL,
            state=state,
            path_id=state.request.path_id,
            occurred_at_ns=occurred_at_ns,
            open_delta_e4=-quantity_e4,
            position_delta_e4=quantity_e4,
            realized_pnl_e6=0,
            source_sha256=source_sha256,
        )

    def record_cancel(
        self,
        reservation_id: str,
        *,
        quantity_e4: int,
        event_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        state = self._state_for(reservation_id)
        require_int("quantity_e4", quantity_e4, minimum=1)
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=occurred_at_ns,
            source_sha256=source_sha256,
        )
        if quantity_e4 > state.open_quantity_e4:
            raise RiskInvariantError("cancel exceeds open reserved quantity")
        state.open_quantity_e4 -= quantity_e4
        state.canceled_quantity_e4 += quantity_e4
        state.assert_conservation()
        self._append(
            event_id=event_id,
            kind=RiskEventKind.CANCEL,
            state=state,
            path_id=state.request.path_id,
            occurred_at_ns=occurred_at_ns,
            open_delta_e4=-quantity_e4,
            position_delta_e4=0,
            realized_pnl_e6=0,
            source_sha256=source_sha256,
        )

    def record_exit(
        self,
        reservation_id: str,
        *,
        quantity_e4: int,
        event_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        state = self._state_for(reservation_id)
        require_int("quantity_e4", quantity_e4, minimum=1)
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=occurred_at_ns,
            source_sha256=source_sha256,
        )
        if quantity_e4 > state.position_quantity_e4:
            raise RiskInvariantError("exit exceeds filled position quantity")
        state.position_quantity_e4 -= quantity_e4
        state.exited_quantity_e4 += quantity_e4
        state.assert_conservation()
        self._append(
            event_id=event_id,
            kind=RiskEventKind.EXIT,
            state=state,
            path_id=state.request.path_id,
            occurred_at_ns=occurred_at_ns,
            open_delta_e4=0,
            position_delta_e4=-quantity_e4,
            realized_pnl_e6=0,
            source_sha256=source_sha256,
        )

    def record_settlement(
        self,
        reservation_id: str,
        *,
        decision: TerminalDecision,
        event_id: str,
    ) -> None:
        state = self._state_for(reservation_id)
        if not decision.closes_position:
            raise RiskInvariantError(
                "non-final or unknown settlement cannot release risk"
            )
        record = decision.record
        if record.market_ticker != state.request.market_ticker:
            raise RiskInvariantError("settlement market mismatch")
        if state.open_quantity_e4:
            raise RiskInvariantError(
                "open order quantity must be canceled before settlement"
            )
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=record.observed_at_ns,
            source_sha256=record.source_sha256,
        )
        quantity_e4 = state.position_quantity_e4
        if quantity_e4 <= 0:
            raise RiskInvariantError("settlement has no open position")
        state.position_quantity_e4 = 0
        state.settled_quantity_e4 += quantity_e4
        state.assert_conservation()
        self._append(
            event_id=event_id,
            kind=RiskEventKind.SETTLEMENT,
            state=state,
            path_id=state.request.path_id,
            occurred_at_ns=record.observed_at_ns,
            open_delta_e4=0,
            position_delta_e4=-quantity_e4,
            realized_pnl_e6=0,
            source_sha256=record.source_sha256,
        )

    def record_realized_pnl(
        self,
        *,
        path_id: str,
        pnl_e6: int,
        event_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        require_nonempty("path_id", path_id)
        require_int("pnl_e6", pnl_e6)
        self._validate_event(
            event_id=event_id,
            occurred_at_ns=occurred_at_ns,
            source_sha256=source_sha256,
        )
        event_date = _utc_date_from_ns(occurred_at_ns)
        self._realized_pnl_by_utc_date_e6[event_date] = (
            self._realized_pnl_by_utc_date_e6.get(event_date, 0)
            + pnl_e6
        )
        daily_pnl_e6 = self._realized_pnl_by_utc_date_e6[event_date]
        if (
            daily_pnl_e6 < 0
            and -daily_pnl_e6 >= self._limits.max_daily_loss_e6
        ):
            self._daily_loss_breached_utc_dates.add(event_date)
        self._append(
            event_id=event_id,
            kind=RiskEventKind.REALIZED_PNL,
            state=None,
            path_id=path_id,
            occurred_at_ns=occurred_at_ns,
            open_delta_e4=0,
            position_delta_e4=0,
            realized_pnl_e6=pnl_e6,
            source_sha256=source_sha256,
        )

    def _state_for(self, reservation_id: str) -> _ReservationState:
        try:
            return self._states[reservation_id]
        except KeyError as exc:
            raise RiskInvariantError(
                f"unknown reservation {reservation_id}"
            ) from exc
