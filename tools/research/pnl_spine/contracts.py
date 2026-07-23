"""Immutable contracts and fixed-point primitives for the research PnL spine.

The spine deliberately has no floating-point boundary.  Prices and quantities
are represented in ``e4`` units and money is represented in ``MoneyE6``:

* ``10_000 price_e4`` is one dollar of payout.
* ``10_000 quantity_e4`` is one contract.
* ``1_000_000 amount_e6`` is one dollar.

MoneyE6 preserves the official combined subpenny/fractional example
``$0.3301 × 0.03 = $0.009903``.  Venue rounding is performed only by the fee
module at its explicitly documented centicent or cent boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping


PRICE_SCALE_E4 = 10_000
QUANTITY_SCALE_E4 = 10_000
MONEY_SCALE_E6 = 1_000_000
MICRODOLLARS_PER_CENTICENT = 100
MICRODOLLARS_PER_CENT = 10_000
CENTICENTS_PER_DOLLAR = 10_000  # dimensional compatibility constant

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A spine input violates an immutable contract."""


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class LiquidityRole(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class FillPurpose(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class CashFlowKind(str, Enum):
    TRADE_PRINCIPAL = "TRADE_PRINCIPAL"
    FEE = "FEE"
    VARIABLE_COST = "VARIABLE_COST"
    SETTLEMENT = "SETTLEMENT"
    NO_POSITION_CLOSE = "NO_POSITION_CLOSE"


class ClosureState(str, Enum):
    OPEN = "OPEN"
    CENSORED = "CENSORED"
    CLOSED_NO_POSITION = "CLOSED_NO_POSITION"
    CLOSED_BY_EXIT = "CLOSED_BY_EXIT"
    CLOSED_BY_SETTLEMENT = "CLOSED_BY_SETTLEMENT"


def require_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return an integer after rejecting bools, floats, and out-of-range data."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer fixed-point value")
    if minimum is not None and value < minimum:
        raise ContractError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{name} must be <= {maximum}")
    return value


def require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")
    return value


def require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def ceil_div(numerator: int, denominator: int) -> int:
    require_int("numerator", numerator, minimum=0)
    require_int("denominator", denominator, minimum=1)
    return (numerator + denominator - 1) // denominator


def exact_trade_notional_e6(price_e4: int, quantity_e4: int) -> int:
    """Convert price × quantity to MoneyE6 without hidden rounding."""

    require_int("price_e4", price_e4, minimum=0, maximum=PRICE_SCALE_E4)
    require_int("quantity_e4", quantity_e4, minimum=1)
    numerator = price_e4 * quantity_e4
    # (price_e4 / 1e4) * (quantity_e4 / 1e4) dollars * 1e6.
    if numerator % 100:
        raise ContractError(
            "price_e4 * quantity_e4 is not exactly representable in MoneyE6"
        )
    return numerator // 100


def exact_trade_notional_cc(price_e4: int, quantity_e4: int) -> int:
    """Compatibility conversion for callers that explicitly require cc.

    The shared core never uses this lossy boundary.  It fails unless the
    MoneyE6 notional happens to be an exact number of centicents.
    """

    amount_e6 = exact_trade_notional_e6(price_e4, quantity_e4)
    if amount_e6 % MICRODOLLARS_PER_CENTICENT:
        raise ContractError("trade notional is not an exact centicent amount")
    return amount_e6 // MICRODOLLARS_PER_CENTICENT


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ContractError("canonical mappings require string keys")
        return {
            key: _canonical(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ContractError(
        f"unsupported canonical value {type(value).__name__}; floats are forbidden"
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class PathSpec:
    """Immutable identity binding for one strategy or baseline PnL path."""

    path_id: str
    experiment_id: str
    baseline_id: str
    definition_sha256: str
    release_sha256: str
    market_ticker: str
    event_ticker: str
    factor_key: str
    outcome: Outcome

    def __post_init__(self) -> None:
        for name in (
            "path_id",
            "experiment_id",
            "baseline_id",
            "market_ticker",
            "event_ticker",
            "factor_key",
        ):
            require_nonempty(name, getattr(self, name))
        require_sha256("definition_sha256", self.definition_sha256)
        require_sha256("release_sha256", self.release_sha256)
        if not isinstance(self.outcome, Outcome):
            raise ContractError("outcome must be an Outcome")


@dataclass(frozen=True)
class FillRecord:
    """A strictly simulated or privately observed immutable fill."""

    fill_id: str
    order_id: str
    path_id: str
    market_ticker: str
    outcome: Outcome
    side: Side
    purpose: FillPurpose
    liquidity_role: LiquidityRole
    quantity_e4: int
    price_e4: int
    executed_at_ns: int
    source_sha256: str
    actual_private_fee_e6: int | None = None
    actual_private_fee_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "path_id", "market_ticker"):
            require_nonempty(name, getattr(self, name))
        if not isinstance(self.outcome, Outcome):
            raise ContractError("outcome must be an Outcome")
        if not isinstance(self.side, Side):
            raise ContractError("side must be a Side")
        if not isinstance(self.purpose, FillPurpose):
            raise ContractError("purpose must be a FillPurpose")
        if not isinstance(self.liquidity_role, LiquidityRole):
            raise ContractError("liquidity_role must be a LiquidityRole")
        require_int("quantity_e4", self.quantity_e4, minimum=1)
        require_int(
            "price_e4",
            self.price_e4,
            minimum=0,
            maximum=PRICE_SCALE_E4,
        )
        require_int("executed_at_ns", self.executed_at_ns, minimum=0)
        require_sha256("source_sha256", self.source_sha256)
        if self.actual_private_fee_e6 is None:
            if self.actual_private_fee_receipt_sha256 is not None:
                raise ContractError(
                    "private fee receipt cannot exist without an actual private fee"
                )
        else:
            require_int(
                "actual_private_fee_e6",
                self.actual_private_fee_e6,
                minimum=0,
            )
            require_sha256(
                "actual_private_fee_receipt_sha256",
                self.actual_private_fee_receipt_sha256,
            )


@dataclass(frozen=True)
class CashFlow:
    """One append-only signed cash movement.

    Positive ``amount_e6`` is cash received; negative is cash paid.
    """

    event_id: str
    path_id: str
    sequence: int
    occurred_at_ns: int
    kind: CashFlowKind
    amount_e6: int
    position_delta_e4: int
    source_sha256: str
    order_id: str | None = None
    fill_id: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        require_nonempty("event_id", self.event_id)
        require_nonempty("path_id", self.path_id)
        require_int("sequence", self.sequence, minimum=0)
        require_int("occurred_at_ns", self.occurred_at_ns, minimum=0)
        if not isinstance(self.kind, CashFlowKind):
            raise ContractError("kind must be a CashFlowKind")
        require_int("amount_e6", self.amount_e6)
        require_int("position_delta_e4", self.position_delta_e4)
        require_sha256("source_sha256", self.source_sha256)
        if self.order_id is not None:
            require_nonempty("order_id", self.order_id)
        if self.fill_id is not None:
            require_nonempty("fill_id", self.fill_id)
        if not isinstance(self.note, str):
            raise ContractError("note must be a string")
