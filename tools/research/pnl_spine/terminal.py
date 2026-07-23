"""Fail-closed terminal and settlement classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import (
    ClosureState,
    PRICE_SCALE_E4,
    require_int,
    require_nonempty,
    require_sha256,
)


class SettlementStatus(str, Enum):
    FINALIZED = "FINALIZED"
    PROVISIONAL = "PROVISIONAL"
    POSTPONED = "POSTPONED"
    VOID = "VOID"
    CANCELED = "CANCELED"
    RETIRED = "RETIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SettlementRecord:
    """A source-bound terminal observation.

    A non-final record may never carry a payout used by the PnL spine.
    The research boundary accepts only the normalized official
    ``FINALIZED`` state as settlement authority.  VOID/CANCELED/RETIRED are
    source lifecycle reasons, not payout rules; a trusted adapter must map a
    genuinely paid terminal record to ``FINALIZED`` while preserving the raw
    reason in its source evidence.
    """

    settlement_id: str
    market_ticker: str
    status: SettlementStatus
    finalized: bool
    settlement_value_e4: int | None
    observed_at_ns: int
    revision: int
    source_sha256: str

    def __post_init__(self) -> None:
        require_nonempty("settlement_id", self.settlement_id)
        require_nonempty("market_ticker", self.market_ticker)
        if not isinstance(self.status, SettlementStatus):
            raise ValueError("status must be a SettlementStatus")
        if not isinstance(self.finalized, bool):
            raise ValueError("finalized must be bool")
        require_int("observed_at_ns", self.observed_at_ns, minimum=0)
        require_int("revision", self.revision, minimum=0)
        require_sha256("source_sha256", self.source_sha256)
        if self.finalized != (self.status is SettlementStatus.FINALIZED):
            raise ValueError(
                "finalized must be true exactly for FINALIZED status"
            )
        if self.finalized:
            require_int(
                "settlement_value_e4",
                self.settlement_value_e4,
                minimum=0,
                maximum=PRICE_SCALE_E4,
            )
        elif self.settlement_value_e4 is not None:
            raise ValueError(
                "non-final settlement must not expose a payout to PnL"
            )


@dataclass(frozen=True)
class TerminalDecision:
    state: ClosureState
    reason: str
    record: SettlementRecord

    @property
    def closes_position(self) -> bool:
        return self.state is ClosureState.CLOSED_BY_SETTLEMENT


def classify_settlement(record: SettlementRecord) -> TerminalDecision:
    if record.finalized and record.status is SettlementStatus.FINALIZED:
        return TerminalDecision(
            state=ClosureState.CLOSED_BY_SETTLEMENT,
            reason="SOURCE_FINALIZED_WITH_EXACT_PAYOUT",
            record=record,
        )
    return TerminalDecision(
        state=ClosureState.CENSORED,
        reason=f"NON_FINAL_OR_UNKNOWN_{record.status.value}",
        record=record,
    )
