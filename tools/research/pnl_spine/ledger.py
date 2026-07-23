"""Append-only pathwise cash ledger with fail-closed PnL finalization."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    CashFlow,
    CashFlowKind,
    ClosureState,
    FillPurpose,
    FillRecord,
    PathSpec,
    Side,
    canonical_sha256,
    exact_trade_notional_e6,
    require_int,
    require_nonempty,
    require_sha256,
)
from .fees import FeeAssessment, FeeSchedule, FeeTruthUnavailable
from .terminal import SettlementRecord, classify_settlement


class LedgerInvariantError(RuntimeError):
    """A cash, position, ordering, or identity invariant was violated."""


class IncompletePnL(LedgerInvariantError):
    """The path is still open or censored and cannot publish net PnL."""


def _result_payload(
    *,
    path_id: str,
    closure_state: ClosureState,
    gross_pnl_e6: int,
    fee_cost_e6: int,
    variable_cost_e6: int,
    net_pnl_e6: int,
    cashflow_count: int,
    ledger_sha256: str,
) -> dict[str, object]:
    return {
        "path_id": path_id,
        "closure_state": closure_state,
        "gross_pnl_e6": gross_pnl_e6,
        "fee_cost_e6": fee_cost_e6,
        "variable_cost_e6": variable_cost_e6,
        "net_pnl_e6": net_pnl_e6,
        "cashflow_count": cashflow_count,
        "ledger_sha256": ledger_sha256,
    }


@dataclass(frozen=True)
class PnLResult:
    path_id: str
    closure_state: ClosureState
    gross_pnl_e6: int
    fee_cost_e6: int
    variable_cost_e6: int
    net_pnl_e6: int
    cashflow_count: int
    ledger_sha256: str
    result_sha256: str

    def __post_init__(self) -> None:
        require_nonempty("path_id", self.path_id)
        if self.closure_state not in {
            ClosureState.CLOSED_NO_POSITION,
            ClosureState.CLOSED_BY_EXIT,
            ClosureState.CLOSED_BY_SETTLEMENT,
        }:
            raise IncompletePnL("PnLResult requires a complete closure state")
        for name in (
            "gross_pnl_e6",
            "fee_cost_e6",
            "variable_cost_e6",
            "net_pnl_e6",
        ):
            require_int(name, getattr(self, name))
        require_int("cashflow_count", self.cashflow_count, minimum=1)
        require_sha256("ledger_sha256", self.ledger_sha256)
        require_sha256("result_sha256", self.result_sha256)
        if self.net_pnl_e6 != (
            self.gross_pnl_e6
            - self.fee_cost_e6
            - self.variable_cost_e6
        ):
            raise LedgerInvariantError("published PnL cash identity failed")
        expected = canonical_sha256(
            _result_payload(
                path_id=self.path_id,
                closure_state=self.closure_state,
                gross_pnl_e6=self.gross_pnl_e6,
                fee_cost_e6=self.fee_cost_e6,
                variable_cost_e6=self.variable_cost_e6,
                net_pnl_e6=self.net_pnl_e6,
                cashflow_count=self.cashflow_count,
                ledger_sha256=self.ledger_sha256,
            )
        )
        if self.result_sha256 != expected:
            raise LedgerInvariantError("PnL result SHA-256 mismatch")


class PnLLedger:
    """One experiment/baseline path with immutable identity and append-only cash."""

    def __init__(self, path: PathSpec, fee_schedule: FeeSchedule) -> None:
        self._path = path
        self._fee_schedule = fee_schedule
        self._cashflows: list[CashFlow] = []
        self._fee_assessments: list[FeeAssessment] = []
        self._terminal_observations: list[SettlementRecord] = []
        self._event_ids: set[str] = set()
        self._fill_ids: set[str] = set()
        self._position_e4 = 0
        self._closure_state = ClosureState.OPEN
        self._last_observed_at_ns = -1
        self._last_settlement_revision = -1
        self._order_fee_mode: dict[str, str] = {}
        self._order_rounding_accumulator_e6: dict[str, int] = {}
        self._sealed = False
        self._result: PnLResult | None = None

    @property
    def path(self) -> PathSpec:
        return self._path

    @property
    def cashflows(self) -> tuple[CashFlow, ...]:
        return tuple(self._cashflows)

    @property
    def fee_assessments(self) -> tuple[FeeAssessment, ...]:
        return tuple(self._fee_assessments)

    @property
    def terminal_observations(self) -> tuple[SettlementRecord, ...]:
        return tuple(self._terminal_observations)

    @property
    def position_e4(self) -> int:
        return self._position_e4

    @property
    def closure_state(self) -> ClosureState:
        return self._closure_state

    @property
    def deterministic_sha256(self) -> str:
        return canonical_sha256(
            {
                "path": self._path,
                "fee_schedule_sha256": (
                    self._fee_schedule.deterministic_sha256
                ),
                "cashflows": self.cashflows,
                "fee_assessments": self.fee_assessments,
                "terminal_observations": self.terminal_observations,
                "closure_state": self._closure_state,
                "position_e4": self._position_e4,
            }
        )

    def _require_mutable(self) -> None:
        if self._sealed:
            raise LedgerInvariantError("ledger is finalized and immutable")

    def _validate_observed_at(self, occurred_at_ns: int) -> None:
        require_int("occurred_at_ns", occurred_at_ns, minimum=0)
        if occurred_at_ns < self._last_observed_at_ns:
            raise LedgerInvariantError(
                "ledger observations must be append-time monotone"
            )

    def _append_cashflows(self, cashflows: tuple[CashFlow, ...]) -> None:
        local_ids: set[str] = set()
        for offset, cashflow in enumerate(cashflows):
            if cashflow.path_id != self._path.path_id:
                raise LedgerInvariantError("cashflow path mismatch")
            if cashflow.sequence != len(self._cashflows) + offset:
                raise LedgerInvariantError("cashflow sequence is not contiguous")
            if (
                cashflow.event_id in self._event_ids
                or cashflow.event_id in local_ids
            ):
                raise LedgerInvariantError(
                    f"duplicate cashflow event {cashflow.event_id}"
                )
            self._validate_observed_at(cashflow.occurred_at_ns)
            local_ids.add(cashflow.event_id)
        self._cashflows.extend(cashflows)
        self._event_ids.update(local_ids)
        self._last_observed_at_ns = max(
            self._last_observed_at_ns,
            *(cashflow.occurred_at_ns for cashflow in cashflows),
        )

    def _assess_fee(self, fill: FillRecord) -> FeeAssessment:
        mode = (
            "ACTUAL"
            if fill.actual_private_fee_e6 is not None
            else "SCHEDULE"
        )
        prior_mode = self._order_fee_mode.get(fill.order_id)
        if prior_mode is not None and prior_mode != mode:
            raise FeeTruthUnavailable(
                f"order {fill.order_id} mixes actual and scheduled fee truth"
            )
        accumulator_before = self._order_rounding_accumulator_e6.get(
            fill.order_id,
            0,
        )
        assessment = self._fee_schedule.assess(
            fill,
            rounding_accumulator_before_e6=accumulator_before,
        )
        if mode == "SCHEDULE":
            if assessment.rule_id is None:
                raise FeeTruthUnavailable("scheduled fee lacks rule binding")
        return assessment

    def record_fill(self, fill: FillRecord) -> FeeAssessment:
        self._require_mutable()
        if self._closure_state in {
            ClosureState.CLOSED_NO_POSITION,
            ClosureState.CLOSED_BY_EXIT,
            ClosureState.CLOSED_BY_SETTLEMENT,
        }:
            raise LedgerInvariantError("cannot fill a closed path")
        if (
            self._closure_state is ClosureState.CENSORED
            and fill.purpose is FillPurpose.ENTRY
        ):
            raise LedgerInvariantError(
                "cannot increase risk after a censored terminal observation"
            )
        if fill.fill_id in self._fill_ids:
            raise LedgerInvariantError(f"duplicate fill {fill.fill_id}")
        if fill.path_id != self._path.path_id:
            raise LedgerInvariantError("fill path mismatch")
        if fill.market_ticker != self._path.market_ticker:
            raise LedgerInvariantError("fill market mismatch")
        if fill.outcome is not self._path.outcome:
            raise LedgerInvariantError("fill outcome mismatch")
        self._validate_observed_at(fill.executed_at_ns)

        delta_e4 = (
            fill.quantity_e4
            if fill.side is Side.BUY
            else -fill.quantity_e4
        )
        position_after_e4 = self._position_e4 + delta_e4
        if fill.purpose is FillPurpose.ENTRY:
            if (
                self._position_e4 != 0
                and self._position_e4 * delta_e4 < 0
            ):
                raise LedgerInvariantError(
                    "ENTRY cannot offset an existing position"
                )
            if abs(position_after_e4) <= abs(self._position_e4):
                raise LedgerInvariantError(
                    "ENTRY must strictly increase absolute exposure"
                )
        else:
            if self._position_e4 == 0:
                raise LedgerInvariantError("EXIT requires an open position")
            if self._position_e4 * delta_e4 >= 0:
                raise LedgerInvariantError(
                    "EXIT must oppose the open position"
                )
            if abs(delta_e4) > abs(self._position_e4):
                raise LedgerInvariantError("EXIT cannot cross through flat")

        notional_e6 = exact_trade_notional_e6(
            fill.price_e4,
            fill.quantity_e4,
        )
        principal_e6 = (
            -notional_e6 if fill.side is Side.BUY else notional_e6
        )
        assessment = self._assess_fee(fill)
        sequence = len(self._cashflows)
        principal = CashFlow(
            event_id=f"{fill.fill_id}:principal",
            path_id=self._path.path_id,
            sequence=sequence,
            occurred_at_ns=fill.executed_at_ns,
            kind=CashFlowKind.TRADE_PRINCIPAL,
            amount_e6=principal_e6,
            position_delta_e4=delta_e4,
            source_sha256=fill.source_sha256,
            order_id=fill.order_id,
            fill_id=fill.fill_id,
        )
        fee = CashFlow(
            event_id=f"{fill.fill_id}:fee",
            path_id=self._path.path_id,
            sequence=sequence + 1,
            occurred_at_ns=fill.executed_at_ns,
            kind=CashFlowKind.FEE,
            amount_e6=-assessment.fee_e6,
            position_delta_e4=0,
            source_sha256=assessment.source_sha256,
            order_id=fill.order_id,
            fill_id=fill.fill_id,
            note=assessment.source,
        )
        self._append_cashflows((principal, fee))

        mode = (
            "ACTUAL"
            if assessment.is_actual_private_fee
            else "SCHEDULE"
        )
        self._order_fee_mode[fill.order_id] = mode
        if mode == "SCHEDULE":
            self._order_rounding_accumulator_e6[fill.order_id] = (
                assessment.rounding_accumulator_after_e6
            )
        self._fee_assessments.append(assessment)
        self._fill_ids.add(fill.fill_id)
        self._position_e4 = position_after_e4
        self._last_observed_at_ns = fill.executed_at_ns
        if self._position_e4 == 0:
            self._closure_state = ClosureState.CLOSED_BY_EXIT
        return assessment

    def record_variable_cost(
        self,
        *,
        cost_id: str,
        amount_e6: int,
        occurred_at_ns: int,
        source_sha256: str,
        note: str = "",
    ) -> None:
        self._require_mutable()
        require_nonempty("cost_id", cost_id)
        require_int("amount_e6", amount_e6, minimum=0)
        require_sha256("source_sha256", source_sha256)
        if not isinstance(note, str):
            raise LedgerInvariantError("cost note must be a string")
        self._validate_observed_at(occurred_at_ns)
        cashflow = CashFlow(
            event_id=f"cost:{cost_id}",
            path_id=self._path.path_id,
            sequence=len(self._cashflows),
            occurred_at_ns=occurred_at_ns,
            kind=CashFlowKind.VARIABLE_COST,
            amount_e6=-amount_e6,
            position_delta_e4=0,
            source_sha256=source_sha256,
            note=note,
        )
        self._append_cashflows((cashflow,))

    def observe_settlement(self, record: SettlementRecord) -> ClosureState:
        self._require_mutable()
        if self._closure_state in {
            ClosureState.CLOSED_NO_POSITION,
            ClosureState.CLOSED_BY_EXIT,
            ClosureState.CLOSED_BY_SETTLEMENT,
        }:
            raise LedgerInvariantError(
                "cannot settle an already closed path"
            )
        if record.market_ticker != self._path.market_ticker:
            raise LedgerInvariantError("settlement market mismatch")
        self._validate_observed_at(record.observed_at_ns)
        if record.revision <= self._last_settlement_revision:
            raise LedgerInvariantError(
                "settlement revision must strictly increase"
            )
        decision = classify_settlement(record)
        if not decision.closes_position:
            self._terminal_observations.append(record)
            self._last_settlement_revision = record.revision
            self._last_observed_at_ns = record.observed_at_ns
            self._closure_state = ClosureState.CENSORED
            return self._closure_state

        numerator = self._position_e4 * record.settlement_value_e4
        if numerator % 100:
            raise LedgerInvariantError(
                "settlement payout is not exactly representable in MoneyE6"
            )
        settlement_cash_e6 = numerator // 100
        cashflow = CashFlow(
            event_id=f"{record.settlement_id}:settlement",
            path_id=self._path.path_id,
            sequence=len(self._cashflows),
            occurred_at_ns=record.observed_at_ns,
            kind=CashFlowKind.SETTLEMENT,
            amount_e6=settlement_cash_e6,
            position_delta_e4=-self._position_e4,
            source_sha256=record.source_sha256,
            note=record.status.value,
        )
        self._append_cashflows((cashflow,))
        self._terminal_observations.append(record)
        self._last_settlement_revision = record.revision
        self._position_e4 = 0
        self._closure_state = ClosureState.CLOSED_BY_SETTLEMENT
        return self._closure_state

    def close_no_position(
        self,
        *,
        closure_id: str,
        occurred_at_ns: int,
        source_sha256: str,
    ) -> None:
        """Explicitly close a no-trigger/no-fill path at zero PnL."""

        self._require_mutable()
        require_nonempty("closure_id", closure_id)
        require_sha256("source_sha256", source_sha256)
        if self._closure_state is not ClosureState.OPEN:
            raise LedgerInvariantError(
                "only an untouched open path can close with no position"
            )
        if self._position_e4 != 0 or self._fill_ids:
            raise LedgerInvariantError("no-position close has trading activity")
        self._validate_observed_at(occurred_at_ns)
        cashflow = CashFlow(
            event_id=f"no-position:{closure_id}",
            path_id=self._path.path_id,
            sequence=len(self._cashflows),
            occurred_at_ns=occurred_at_ns,
            kind=CashFlowKind.NO_POSITION_CLOSE,
            amount_e6=0,
            position_delta_e4=0,
            source_sha256=source_sha256,
        )
        self._append_cashflows((cashflow,))
        self._closure_state = ClosureState.CLOSED_NO_POSITION

    def finalize(self) -> PnLResult:
        if self._result is not None:
            return self._result
        if self._closure_state in {
            ClosureState.OPEN,
            ClosureState.CENSORED,
        }:
            raise IncompletePnL(
                f"path cannot finalize in {self._closure_state.value}"
            )
        if self._position_e4 != 0:
            raise LedgerInvariantError("closed path retains a position")
        if not self._cashflows:
            raise LedgerInvariantError("closed path has no closure receipt")
        position_sum = sum(
            cashflow.position_delta_e4
            for cashflow in self._cashflows
        )
        if position_sum != 0:
            raise LedgerInvariantError("position cashflow identity failed")

        gross_pnl_e6 = sum(
            cashflow.amount_e6
            for cashflow in self._cashflows
            if cashflow.kind in {
                CashFlowKind.TRADE_PRINCIPAL,
                CashFlowKind.SETTLEMENT,
            }
        )
        fee_cash_e6 = sum(
            cashflow.amount_e6
            for cashflow in self._cashflows
            if cashflow.kind is CashFlowKind.FEE
        )
        cost_cash_e6 = sum(
            cashflow.amount_e6
            for cashflow in self._cashflows
            if cashflow.kind is CashFlowKind.VARIABLE_COST
        )
        fee_cost_e6 = -fee_cash_e6
        variable_cost_e6 = -cost_cash_e6
        net_pnl_e6 = sum(
            cashflow.amount_e6 for cashflow in self._cashflows
        )
        if net_pnl_e6 != (
            gross_pnl_e6 - fee_cost_e6 - variable_cost_e6
        ):
            raise LedgerInvariantError("cashflow PnL identity failed")

        ledger_sha256 = self.deterministic_sha256
        payload = _result_payload(
            path_id=self._path.path_id,
            closure_state=self._closure_state,
            gross_pnl_e6=gross_pnl_e6,
            fee_cost_e6=fee_cost_e6,
            variable_cost_e6=variable_cost_e6,
            net_pnl_e6=net_pnl_e6,
            cashflow_count=len(self._cashflows),
            ledger_sha256=ledger_sha256,
        )
        self._result = PnLResult(
            path_id=self._path.path_id,
            closure_state=self._closure_state,
            gross_pnl_e6=gross_pnl_e6,
            fee_cost_e6=fee_cost_e6,
            variable_cost_e6=variable_cost_e6,
            net_pnl_e6=net_pnl_e6,
            cashflow_count=len(self._cashflows),
            ledger_sha256=ledger_sha256,
            result_sha256=canonical_sha256(payload),
        )
        self._sealed = True
        return self._result
