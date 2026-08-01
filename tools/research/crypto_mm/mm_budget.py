#!/usr/bin/env python3
"""Pure, fail-closed equity-budget primitives for the crypto MM.

This module deliberately has no exchange client and no dependency on
``mm_engine``.  Callers provide already-fetched API records and complete sets
of resting, pending, unknown, and candidate-order risks.

Money is represented as integer micro-dollars throughout:

    1 USD  = 1_000_000 micro-USD
    1 cent =    10_000 micro-USD

Kalshi's current ``GET /portfolio/balance`` contract exposes available cash as
``balance_dollars``.  Official references conflict on whether integer-cent
``portfolio_value`` is positions-only or total portfolio value, so the
fail-closed default never adds it to cash.  The legacy integer ``balance`` is
retained only as a schema cross-check because it loses sub-cent precision.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Optional, Sequence


MICRO_USD_PER_USD = 1_000_000
MICRO_USD_PER_CENT = 10_000
MICRO_CONTRACTS_PER_CONTRACT = 1_000_000
MARK_UNCERTAINTY_MICRO_USD = MICRO_USD_PER_CENT
BUDGET_SCHEMA = "crypto-mm-budget-v1"
MAX_I64 = 2**63 - 1

_FIXED_DOLLARS_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
_FIXED_CONTRACTS_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$"
)
_MARKET_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_BUDGET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

BUDGET_FIELDS = {
    "schema_version",
    "budget_id",
    "account_fingerprint",
    "subaccount",
    "exchange_index",
    "anchor_equity_micro_usd",
    "max_loss_micro_usd",
    "floor_equity_micro_usd",
    "latched",
    "latch_reason",
    "generation",
}


class BudgetError(ValueError):
    """Base class for fail-closed budget validation errors."""


class BalanceSnapshotError(BudgetError):
    """The exchange balance response is absent, stale, or inconsistent."""


class BudgetRecordError(BudgetError):
    """The durable budget record is malformed or has changed unexpectedly."""


class StaleBudgetRecord(BudgetRecordError):
    """A writer attempted to latch an obsolete budget generation."""


def _plain_int(
    name: str,
    value: object,
    *,
    minimum: int = 0,
    maximum: int = MAX_I64,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BudgetError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def fixed_dollars_to_micro_usd(name: str, value: object) -> int:
    """Parse an official fixed-point dollar string without binary floats."""
    if not isinstance(value, str) or not _FIXED_DOLLARS_RE.fullmatch(value):
        raise BudgetError(
            f"{name} must be a non-negative fixed-point dollar string "
            "with at most 6 decimals"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:  # defensive; the regex should preclude it
        raise BudgetError(f"{name} is not a valid decimal") from exc
    scaled = amount * MICRO_USD_PER_USD
    if scaled != scaled.to_integral_value():
        raise BudgetError(f"{name} exceeds micro-dollar precision")
    result = int(scaled)
    if result < 0 or result > MAX_I64:
        raise BudgetError(f"{name} is outside the supported money range")
    return result


def micro_usd_to_fixed_dollars(value: int) -> str:
    """Canonical six-decimal formatting for receipts and tests."""
    value = _plain_int("micro_usd", value)
    return f"{Decimal(value) / MICRO_USD_PER_USD:.6f}"


def fixed_contracts_to_microcontracts(name: str, value: object) -> int:
    """Parse a non-negative fixed-point contract count exactly."""
    if not isinstance(value, str) or not _FIXED_CONTRACTS_RE.fullmatch(value):
        raise BudgetError(
            f"{name} must be a non-negative fixed-point contract string "
            "with at most 6 decimals"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise BudgetError(f"{name} is not a valid contract count") from exc
    scaled = amount * MICRO_CONTRACTS_PER_CONTRACT
    if scaled != scaled.to_integral_value():
        raise BudgetError(f"{name} exceeds micro-contract precision")
    result = int(scaled)
    if result < 0 or result > MAX_I64:
        raise BudgetError(f"{name} is outside the supported quantity range")
    return result


@dataclass(frozen=True)
class BalanceSnapshot:
    """Strictly parsed, single-response exchange balance snapshot."""

    balance_micro_usd: int
    portfolio_value_micro_usd: int
    equity_micro_usd: int
    legacy_balance_cents: int
    portfolio_value_cents: int
    updated_ts: int
    exchange_index: int


def parse_balance_response(
    response: object,
    *,
    expected_exchange_index: int = 0,
    portfolio_value_is_positions_only: bool = False,
) -> BalanceSnapshot:
    """Parse ``GET /portfolio/balance`` and prove its redundant fields agree.

    The published meaning of ``portfolio_value`` conflicts across official
    references (positions-only versus total portfolio value).  Therefore the
    fail-closed default does *not* add it to cash.  A caller may opt into
    ``balance + portfolio_value`` only after an external live contract probe
    has established positions-only semantics for the bound account surface.
    """
    expected_exchange_index = _plain_int(
        "expected_exchange_index", expected_exchange_index
    )
    if type(portfolio_value_is_positions_only) is not bool:
        raise BalanceSnapshotError(
            "portfolio_value_is_positions_only must be boolean"
        )
    if not isinstance(response, Mapping):
        raise BalanceSnapshotError("balance response must be an object")
    try:
        balance_micro = fixed_dollars_to_micro_usd(
            "balance_dollars", response["balance_dollars"]
        )
        legacy_balance = _plain_int("balance", response["balance"])
        portfolio_cents = _plain_int(
            "portfolio_value", response["portfolio_value"]
        )
        updated_ts = _plain_int("updated_ts", response["updated_ts"])
    except KeyError as exc:
        raise BalanceSnapshotError(f"missing balance field {exc.args[0]!r}") from exc
    except BudgetError as exc:
        raise BalanceSnapshotError(str(exc)) from exc

    # Current production emits the integer-cent field as the floor of the
    # precise direct-member balance (e.g. $19.8205 -> 1982 cents).
    if balance_micro // MICRO_USD_PER_CENT != legacy_balance:
        raise BalanceSnapshotError(
            "balance and balance_dollars disagree "
            f"({legacy_balance}c vs {balance_micro} micro-USD)"
        )

    breakdown = response.get("balance_breakdown")
    if breakdown is not None:
        if not isinstance(breakdown, list):
            raise BalanceSnapshotError("balance_breakdown must be an array")
        matches = []
        for item in breakdown:
            if not isinstance(item, Mapping):
                raise BalanceSnapshotError(
                    "balance_breakdown entries must be objects"
                )
            try:
                exchange_index = _plain_int(
                    "balance_breakdown.exchange_index", item["exchange_index"]
                )
            except KeyError as exc:
                raise BalanceSnapshotError(
                    "balance_breakdown entry missing exchange_index"
                ) from exc
            except BudgetError as exc:
                raise BalanceSnapshotError(str(exc)) from exc
            if exchange_index == expected_exchange_index:
                try:
                    matches.append(
                        fixed_dollars_to_micro_usd(
                            "balance_breakdown.balance", item["balance"]
                        )
                    )
                except KeyError as exc:
                    raise BalanceSnapshotError(
                        "balance_breakdown entry missing balance"
                    ) from exc
                except BudgetError as exc:
                    raise BalanceSnapshotError(str(exc)) from exc
        if len(matches) != 1:
            raise BalanceSnapshotError(
                "balance_breakdown must contain exactly one expected "
                f"exchange_index={expected_exchange_index} row"
            )
        if matches[0] != balance_micro:
            raise BalanceSnapshotError(
                "balance_breakdown does not match balance_dollars"
            )

    portfolio_micro = portfolio_cents * MICRO_USD_PER_CENT
    equity_micro = (
        balance_micro + portfolio_micro
        if portfolio_value_is_positions_only else balance_micro
    )
    if equity_micro > MAX_I64:
        raise BalanceSnapshotError("equity exceeds the supported money range")
    return BalanceSnapshot(
        balance_micro_usd=balance_micro,
        portfolio_value_micro_usd=portfolio_micro,
        equity_micro_usd=equity_micro,
        legacy_balance_cents=legacy_balance,
        portfolio_value_cents=portfolio_cents,
        updated_ts=updated_ts,
        exchange_index=expected_exchange_index,
    )


def validate_snapshot_freshness(
    snapshot: BalanceSnapshot,
    *,
    request_started_s: float,
    response_finished_s: float,
    previous_updated_ts: Optional[int] = None,
    past_slack_s: float = 2.0,
    future_slack_s: float = 1.0,
) -> None:
    """Validate response timing and monotonic account-update metadata.

    ``updated_ts`` is the time the *account last changed*, not the as-of time
    of this GET response.  A quiet account may therefore legitimately carry
    an arbitrarily old value.  HTTP success and the response's redundant
    balance fields establish this round's snapshot; ``updated_ts`` is used
    only to reject a future timestamp or a regression relative to a prior
    successful response.
    """
    values = (
        request_started_s,
        response_finished_s,
        past_slack_s,
        future_slack_s,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        raise BalanceSnapshotError("freshness inputs must be finite numbers")
    if response_finished_s < request_started_s:
        raise BalanceSnapshotError("response finished before request started")
    if past_slack_s < 0 or future_slack_s < 0:
        raise BalanceSnapshotError("freshness slack cannot be negative")
    if snapshot.updated_ts > response_finished_s + future_slack_s:
        raise BalanceSnapshotError("balance snapshot timestamp is in the future")
    if previous_updated_ts is not None:
        try:
            previous = _plain_int("previous_updated_ts", previous_updated_ts)
        except BudgetError as exc:
            raise BalanceSnapshotError(str(exc)) from exc
        if snapshot.updated_ts < previous:
            raise BalanceSnapshotError("balance snapshot timestamp regressed")


@dataclass(frozen=True)
class BudgetRecord:
    """Absolute, cross-restart drawdown floor.

    There is intentionally no method that changes the anchor, max loss, floor,
    account, or budget ID.  A new baseline requires a separately initialized
    record under a new operator-approved budget ID.
    """

    schema_version: str
    budget_id: str
    account_fingerprint: str
    subaccount: int
    exchange_index: int
    anchor_equity_micro_usd: int
    max_loss_micro_usd: int
    floor_equity_micro_usd: int
    latched: bool
    latch_reason: str
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "budget_id": self.budget_id,
            "account_fingerprint": self.account_fingerprint,
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
            "anchor_equity_micro_usd": self.anchor_equity_micro_usd,
            "max_loss_micro_usd": self.max_loss_micro_usd,
            "floor_equity_micro_usd": self.floor_equity_micro_usd,
            "latched": self.latched,
            "latch_reason": self.latch_reason,
            "generation": self.generation,
        }

    @property
    def immutable_identity(self) -> tuple[object, ...]:
        return (
            self.schema_version,
            self.budget_id,
            self.account_fingerprint,
            self.subaccount,
            self.exchange_index,
            self.anchor_equity_micro_usd,
            self.max_loss_micro_usd,
            self.floor_equity_micro_usd,
        )


def validate_budget_record(
    value: object,
    *,
    expected_account_fingerprint: Optional[str] = None,
    expected_subaccount: Optional[int] = None,
    expected_exchange_index: Optional[int] = None,
) -> BudgetRecord:
    if isinstance(value, BudgetRecord):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise BudgetRecordError("budget record must be an object")
    unknown = set(value) - BUDGET_FIELDS
    missing = BUDGET_FIELDS - set(value)
    if unknown:
        raise BudgetRecordError(
            "unknown budget fields: " + ",".join(sorted(unknown))
        )
    if missing:
        raise BudgetRecordError(
            "missing budget fields: " + ",".join(sorted(missing))
        )
    if value["schema_version"] != BUDGET_SCHEMA:
        raise BudgetRecordError(f"schema_version must be {BUDGET_SCHEMA}")

    budget_id = value["budget_id"]
    if not isinstance(budget_id, str) or not _BUDGET_ID_RE.fullmatch(budget_id):
        raise BudgetRecordError("budget_id is invalid")
    fingerprint = value["account_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
    ):
        raise BudgetRecordError(
            "account_fingerprint must be 64 lowercase hex characters"
        )
    try:
        subaccount = _plain_int("subaccount", value["subaccount"], maximum=63)
        exchange_index = _plain_int("exchange_index", value["exchange_index"])
        anchor = _plain_int(
            "anchor_equity_micro_usd",
            value["anchor_equity_micro_usd"],
            minimum=1,
        )
        max_loss = _plain_int(
            "max_loss_micro_usd", value["max_loss_micro_usd"], minimum=1
        )
        floor = _plain_int(
            "floor_equity_micro_usd", value["floor_equity_micro_usd"]
        )
        generation = _plain_int(
            "generation", value["generation"], minimum=1
        )
    except BudgetError as exc:
        raise BudgetRecordError(str(exc)) from exc
    if max_loss > anchor or anchor - max_loss != floor:
        raise BudgetRecordError(
            "floor must equal anchor_equity_micro_usd - max_loss_micro_usd"
        )
    if type(value["latched"]) is not bool:
        raise BudgetRecordError("latched must be boolean")
    latched = value["latched"]
    reason = value["latch_reason"]
    if (
        not isinstance(reason, str)
        or len(reason) > 200
        or "\x00" in reason
        or reason != reason.strip()
    ):
        raise BudgetRecordError(
            "latch_reason must be a trimmed string of at most 200 characters"
        )
    if latched and not reason:
        raise BudgetRecordError("a latched budget requires latch_reason")
    if not latched and reason:
        raise BudgetRecordError("an unlatched budget cannot have latch_reason")
    expected_generation = 2 if latched else 1
    if generation != expected_generation:
        raise BudgetRecordError(
            "generation/state mismatch: unlatched must be generation 1 "
            "and latched must be generation 2"
        )

    if (
        expected_account_fingerprint is not None
        and fingerprint != expected_account_fingerprint
    ):
        raise BudgetRecordError("budget account_fingerprint mismatch")
    if expected_subaccount is not None:
        try:
            expected_subaccount = _plain_int(
                "expected_subaccount", expected_subaccount, maximum=63
            )
        except BudgetError as exc:
            raise BudgetRecordError(str(exc)) from exc
        if subaccount != expected_subaccount:
            raise BudgetRecordError("budget subaccount mismatch")
    if expected_exchange_index is not None:
        try:
            expected_exchange_index = _plain_int(
                "expected_exchange_index", expected_exchange_index
            )
        except BudgetError as exc:
            raise BudgetRecordError(str(exc)) from exc
        if exchange_index != expected_exchange_index:
            raise BudgetRecordError("budget exchange_index mismatch")

    return BudgetRecord(
        schema_version=BUDGET_SCHEMA,
        budget_id=budget_id,
        account_fingerprint=fingerprint,
        subaccount=subaccount,
        exchange_index=exchange_index,
        anchor_equity_micro_usd=anchor,
        max_loss_micro_usd=max_loss,
        floor_equity_micro_usd=floor,
        latched=latched,
        latch_reason=reason,
        generation=generation,
    )


def new_budget_record(
    *,
    budget_id: str,
    account_fingerprint: str,
    anchor_equity_micro_usd: int,
    max_loss_micro_usd: int,
    subaccount: int = 0,
    exchange_index: int = 0,
) -> BudgetRecord:
    try:
        floor = anchor_equity_micro_usd - max_loss_micro_usd
    except TypeError as exc:
        raise BudgetRecordError("anchor and max loss must be integers") from exc
    return validate_budget_record(
        {
            "schema_version": BUDGET_SCHEMA,
            "budget_id": budget_id,
            "account_fingerprint": account_fingerprint,
            "subaccount": subaccount,
            "exchange_index": exchange_index,
            "anchor_equity_micro_usd": anchor_equity_micro_usd,
            "max_loss_micro_usd": max_loss_micro_usd,
            "floor_equity_micro_usd": floor,
            "latched": False,
            "latch_reason": "",
            "generation": 1,
        }
    )


def _encoded_record(record: BudgetRecord) -> bytes:
    return (
        json.dumps(
            record.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Atomic replacement still holds on filesystems without directory fsync.
        pass


def initialize_budget(path: Path, record: BudgetRecord) -> None:
    """Atomically create a generation-1 budget; never overwrite/rebase."""
    path = Path(path)
    record = validate_budget_record(record)
    if record.generation != 1 or record.latched:
        raise BudgetRecordError(
            "a new budget must be unlatched generation 1"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.init.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            os.chmod(tmp_name, 0o600)
            handle.write(_encoded_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) is an atomic create-if-absent operation on one filesystem.
        os.link(tmp_name, str(path))
        _fsync_directory(path.parent)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def load_budget(
    path: Path,
    *,
    expected_account_fingerprint: Optional[str] = None,
    expected_subaccount: Optional[int] = None,
    expected_exchange_index: Optional[int] = None,
) -> BudgetRecord:
    """Load an existing budget.  Missing/corrupt never initializes a new one."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BudgetRecordError(f"cannot read budget record: {exc}") from exc
    return validate_budget_record(
        value,
        expected_account_fingerprint=expected_account_fingerprint,
        expected_subaccount=expected_subaccount,
        expected_exchange_index=expected_exchange_index,
    )


def _atomic_replace_budget(path: Path, record: BudgetRecord) -> None:
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.latch.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            os.chmod(tmp_name, 0o600)
            handle.write(_encoded_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        tmp_name = None
        _fsync_directory(path.parent)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


@contextmanager
def _record_lock(path: Path) -> Iterable[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def latch_budget(
    path: Path,
    *,
    expected: BudgetRecord,
    reason: str,
) -> BudgetRecord:
    """Atomically and irreversibly latch one known budget generation.

    The expected record closes the read/modify/write race and proves that none
    of the immutable baseline fields changed on disk.  Repeating a latch is
    idempotent; there is intentionally no unlatch or rebase operation.
    """
    path = Path(path)
    expected = validate_budget_record(expected)
    if not isinstance(reason, str):
        raise BudgetRecordError("latch reason must be a string")
    reason = reason.strip()
    if not reason or len(reason) > 200 or "\x00" in reason:
        raise BudgetRecordError(
            "latch reason must contain 1-200 safe characters"
        )
    with _record_lock(path):
        current = load_budget(
            path,
            expected_account_fingerprint=expected.account_fingerprint,
            expected_subaccount=expected.subaccount,
            expected_exchange_index=expected.exchange_index,
        )
        if current.immutable_identity != expected.immutable_identity:
            raise BudgetRecordError(
                "immutable budget identity changed; refusing to latch"
            )
        if current.latched:
            return current
        if current != expected:
            raise StaleBudgetRecord(
                f"budget generation changed ({expected.generation} -> "
                f"{current.generation})"
            )
        if current.generation >= MAX_I64:
            raise BudgetRecordError("budget generation exhausted")
        updated = replace(
            current,
            latched=True,
            latch_reason=reason,
            generation=current.generation + 1,
        )
        updated = validate_budget_record(updated)
        _atomic_replace_budget(path, updated)
        return updated


def _validate_market(market: object) -> str:
    if not isinstance(market, str) or not _MARKET_RE.fullmatch(market):
        raise BudgetError("market must be a short ticker-like string")
    return market


@dataclass(frozen=True)
class MarketPosition:
    """Gross outcome holdings used for joint terminal-settlement math."""

    market: str
    yes_microcontracts: int = 0
    no_microcontracts: int = 0

    def __post_init__(self) -> None:
        _validate_market(self.market)
        _plain_int("YES position", self.yes_microcontracts)
        _plain_int("NO position", self.no_microcontracts)

    @classmethod
    def from_fixed_contracts(
        cls,
        market: str,
        *,
        yes: str = "0",
        no: str = "0",
    ) -> "MarketPosition":
        return cls(
            market=market,
            yes_microcontracts=fixed_contracts_to_microcontracts(
                "YES position", yes
            ),
            no_microcontracts=fixed_contracts_to_microcontracts(
                "NO position", no
            ),
        )


@dataclass(frozen=True)
class OrderWorstCase:
    """Maximum debit if one buy order fills in the joint portfolio state.

    Guarantee is deliberately not caller supplied.  It is derived after all
    resting, pending, unknown, and candidate buys fill by aggregating
    quantities per market and applying ``min(total YES, total NO)`` once.
    This prevents two competing hedge orders from each claiming the same
    guaranteed dollar.
    """

    reference: str
    market: str
    outcome: str
    quantity_microcontracts: int
    cost_micro_usd: int
    fee_micro_usd: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, str)
            or not self.reference.strip()
            or self.reference != self.reference.strip()
            or len(self.reference) > 128
        ):
            raise BudgetError("order risk reference must be a short string")
        _validate_market(self.market)
        if self.outcome not in ("yes", "no"):
            raise BudgetError("order outcome must be 'yes' or 'no'")
        _plain_int(
            "order quantity",
            self.quantity_microcontracts,
            minimum=1,
        )
        _plain_int("order cost", self.cost_micro_usd)
        _plain_int("order fee", self.fee_micro_usd)

    @classmethod
    def from_fixed(
        cls,
        reference: str,
        *,
        market: str,
        outcome: str,
        quantity: str,
        cost: str,
        fee: str = "0",
    ) -> "OrderWorstCase":
        return cls(
            reference=reference,
            market=market,
            outcome=outcome,
            quantity_microcontracts=fixed_contracts_to_microcontracts(
                "order quantity", quantity
            ),
            cost_micro_usd=fixed_dollars_to_micro_usd("order cost", cost),
            fee_micro_usd=fixed_dollars_to_micro_usd("order fee", fee),
        )


@dataclass(frozen=True)
class EquityComputation:
    mark_equity_micro_usd: int
    mark_after_buffer_micro_usd: int
    terminal_equity_micro_usd: int
    safe_equity_micro_usd: int
    existing_guaranteed_payout_micro_usd: int
    resting_cost_fee_micro_usd: int
    pending_cost_fee_micro_usd: int
    unknown_cost_fee_micro_usd: int
    candidate_cost_fee_micro_usd: int
    adverse_fill_loss_micro_usd: int
    final_worst_settlement_micro_usd: int
    order_count: int


def _validated_orders(
    orders: Sequence[OrderWorstCase],
    *,
    group_name: str,
) -> tuple[OrderWorstCase, ...]:
    if isinstance(orders, (str, bytes)) or not isinstance(orders, Sequence):
        raise BudgetError(f"{group_name} orders must be a sequence")
    result = tuple(orders)
    for order in result:
        if not isinstance(order, OrderWorstCase):
            raise BudgetError(
                f"{group_name} orders must contain OrderWorstCase values"
            )
        # A forged object could bypass normal construction via object.__new__.
        order.__post_init__()
    return result


def _validated_positions(
    positions: Sequence[MarketPosition],
) -> tuple[MarketPosition, ...]:
    if isinstance(positions, (str, bytes)) or not isinstance(
        positions, Sequence
    ):
        raise BudgetError("positions must be a sequence")
    result = tuple(positions)
    markets = set()
    for position in result:
        if not isinstance(position, MarketPosition):
            raise BudgetError("positions must contain MarketPosition values")
        position.__post_init__()
        if position.market in markets:
            raise BudgetError("positions must contain one row per market")
        markets.add(position.market)
    return result


def compute_safe_equity(
    snapshot: BalanceSnapshot,
    *,
    positions: Sequence[MarketPosition] = (),
    other_guaranteed_payout_micro_usd: int = 0,
    resting: Sequence[OrderWorstCase] = (),
    pending: Sequence[OrderWorstCase] = (),
    unknown: Sequence[OrderWorstCase] = (),
    candidate: Optional[OrderWorstCase] = None,
    mark_buffer_micro_usd: int = MARK_UNCERTAINTY_MICRO_USD,
) -> EquityComputation:
    """Compute ``min(mark - 1c, terminal worst fill-subset case)``.

    Order fills are non-atomic.  For each market and each possible settlement
    state, an adversary may independently choose whether every resting,
    pending, unknown, or candidate order fills.  One order contributes

    ``min(0, state_payoff_if_filled - cost - fee)``

    because a profitable fill cannot be relied upon while a losing fill must
    be reserved.  We then take the worse of that market's YES and NO states.
    This O(N) construction cannot let two complementary orders manufacture a
    guarantee by assuming they fill together.

    ``snapshot.balance_micro_usd`` is available cash.  The 2026-07-26
    account-bound probe proved a new resting bid does not reduce that field,
    so every possible resting fill still needs its full cost/fee reservation.
    The default mark branch is cash-only; ``portfolio_value`` is not added
    unless its conflicting contract is separately proven by the caller.
    """
    if not isinstance(snapshot, BalanceSnapshot):
        raise BudgetError("snapshot must be BalanceSnapshot")
    other_guaranteed = _plain_int(
        "other_guaranteed_payout_micro_usd",
        other_guaranteed_payout_micro_usd,
    )
    mark_buffer = _plain_int(
        "mark_buffer_micro_usd", mark_buffer_micro_usd
    )
    resting_ = _validated_orders(resting, group_name="resting")
    pending_ = _validated_orders(pending, group_name="pending")
    unknown_ = _validated_orders(unknown, group_name="unknown")
    candidate_ = () if candidate is None else (candidate,)
    candidate_ = _validated_orders(candidate_, group_name="candidate")
    positions_ = _validated_positions(positions)

    groups = (resting_, pending_, unknown_, candidate_)
    references = [order.reference for group in groups for order in group]
    if len(references) != len(set(references)):
        raise BudgetError("order risk references must be unique")

    def cost_fee(group: Sequence[OrderWorstCase]) -> int:
        return sum(
            order.cost_micro_usd + order.fee_micro_usd for order in group
        )

    resting_cost_fee = cost_fee(resting_)
    pending_cost_fee = cost_fee(pending_)
    unknown_cost_fee = cost_fee(unknown_)
    candidate_cost_fee = cost_fee(candidate_)
    holdings: dict[str, list[int]] = {
        position.market: [
            position.yes_microcontracts,
            position.no_microcontracts,
        ]
        for position in positions_
    }
    existing_position_guarantee = sum(
        min(yes_qty, no_qty) for yes_qty, no_qty in holdings.values()
    )
    orders_by_market: dict[str, list[OrderWorstCase]] = {}
    for group in groups:
        for order in group:
            orders_by_market.setdefault(order.market, []).append(order)

    existing_guaranteed = (
        other_guaranteed + existing_position_guarantee
    )
    market_worst = 0
    for market in set(holdings) | set(orders_by_market):
        yes_qty, no_qty = holdings.get(market, [0, 0])
        # One micro-contract pays one micro-dollar in its winning state.
        yes_state = yes_qty
        no_state = no_qty
        for order in orders_by_market.get(market, ()):
            debit = order.cost_micro_usd + order.fee_micro_usd
            yes_payoff = (
                order.quantity_microcontracts
                if order.outcome == "yes" else 0
            )
            no_payoff = (
                order.quantity_microcontracts
                if order.outcome == "no" else 0
            )
            yes_state += min(0, yes_payoff - debit)
            no_state += min(0, no_payoff - debit)
        market_worst += min(yes_state, no_state)

    final_worst_settlement = other_guaranteed + market_worst
    adverse_fill_loss = existing_guaranteed - final_worst_settlement
    if adverse_fill_loss < 0:  # optional fills can never improve the worst case
        raise BudgetError("internal error: adverse fill loss became negative")
    terminal = snapshot.balance_micro_usd + final_worst_settlement
    mark_after_buffer = snapshot.equity_micro_usd - mark_buffer
    safe = min(mark_after_buffer, terminal)
    return EquityComputation(
        mark_equity_micro_usd=snapshot.equity_micro_usd,
        mark_after_buffer_micro_usd=mark_after_buffer,
        terminal_equity_micro_usd=terminal,
        safe_equity_micro_usd=safe,
        existing_guaranteed_payout_micro_usd=existing_guaranteed,
        resting_cost_fee_micro_usd=resting_cost_fee,
        pending_cost_fee_micro_usd=pending_cost_fee,
        unknown_cost_fee_micro_usd=unknown_cost_fee,
        candidate_cost_fee_micro_usd=candidate_cost_fee,
        adverse_fill_loss_micro_usd=adverse_fill_loss,
        final_worst_settlement_micro_usd=final_worst_settlement,
        order_count=len(references),
    )


def budget_allows(
    record: BudgetRecord,
    computation: EquityComputation,
    *,
    additional_buffer_micro_usd: int = 0,
) -> bool:
    """True only for an unlatched record with provable floor headroom."""
    record = validate_budget_record(record)
    if not isinstance(computation, EquityComputation):
        raise BudgetError("computation must be EquityComputation")
    buffer_ = _plain_int(
        "additional_buffer_micro_usd", additional_buffer_micro_usd
    )
    return (
        not record.latched
        and computation.safe_equity_micro_usd
        >= record.floor_equity_micro_usd + buffer_
    )
