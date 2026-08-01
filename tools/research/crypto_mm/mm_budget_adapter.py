#!/usr/bin/env python3
"""Pure integration boundary between ``mm_engine`` state and ``mm_budget``.

This module imports neither ``mm_engine`` nor an exchange client.  The engine
must fetch a complete balance/positions snapshot, capture its local risk
generation before and after those reads, and pass plain records here.

Intended engine mapping
-----------------------

* account identity: ``KALSHI_API_KEY_ID`` until a stable member/account ID is
  available.  A key rotation then deliberately blocks startup.
* REST balance: complete ``GET /portfolio/balance`` response.
* REST positions: all pages of
  ``GET /portfolio/positions?count_filter=position&subaccount=0``.
* local filled ledger: ``S.net_pos`` (``y``/``n`` quantities).
* resting: ``S.orders``; mapping key is ``(ticker, bid|ask_no)`` and value
  carries ``id``, risk ``px``, and remaining ``qty``.
* pending: ``S.pending_new`` with ``client_order_id``, ``px``, and ``qty``.
* cancel-unknown: ``S.unknown_orders``.  It reclassifies the still-reserved
  matching ``S.orders`` row and is never counted a second time.
* IOC unknown: ``S.flatten_pending`` needs future integration fields
  ``risk_px``, ``remaining_risk_qty``, and ``fee_reserve``.  Current engine
  records omit them, so this adapter fails closed instead of guessing.
* candidate: the final ``order_place`` arguments: ticker, engine side,
  risk price, quantity, and worst-case fee reserve.

The future engine patch must add ``S.risk_generation`` and increment it on
every fill, order reservation, acknowledgement, cancellation classification,
and reconciliation mutation.  A preflight is valid only when the generation
captured before and after the REST reads is identical.

Before a latch attempt the guard writes an identity-bound durable
``.trip-pending`` marker.  It reloads the budget on every assessment, requires
``cancel_all`` to return literal ``True``, and keeps retrying incomplete
latch/cancel/halt enforcement on later monitor ticks.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping, Optional, Sequence

import mm_budget as budget


class AdapterError(budget.BudgetError):
    """The engine/REST snapshot cannot prove a safe budget decision."""


class LedgerSchemaError(AdapterError):
    """A local risk reservation is absent or economically incomplete."""


class SnapshotRace(AdapterError):
    """Local order/fill state changed while REST truth was being fetched."""


def account_fingerprint(
    account_identity: str,
    *,
    subaccount: int = 0,
    exchange_index: int = 0,
) -> str:
    """Hash a non-secret stable account binding into the budget record key."""
    if (
        not isinstance(account_identity, str)
        or not account_identity
        or account_identity != account_identity.strip()
        or len(account_identity) > 512
        or "\x00" in account_identity
    ):
        raise AdapterError("account identity must be a trimmed non-empty string")
    if type(subaccount) is not int or not 0 <= subaccount <= 63:
        raise AdapterError("subaccount must be an integer in [0, 63]")
    if type(exchange_index) is not int or exchange_index < 0:
        raise AdapterError("exchange_index must be a non-negative integer")
    canonical = json.dumps(
        {
            "account_identity": account_identity,
            "exchange_index": exchange_index,
            "subaccount": subaccount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"crypto-mm-account-v1\0" + canonical).hexdigest()


def load_bound_budget(
    path: Path,
    *,
    account_identity: str,
    subaccount: int = 0,
    exchange_index: int = 0,
) -> budget.BudgetRecord:
    """Load only; missing records are never initialized or rebased."""
    fingerprint = account_fingerprint(
        account_identity,
        subaccount=subaccount,
        exchange_index=exchange_index,
    )
    return budget.load_budget(
        path,
        expected_account_fingerprint=fingerprint,
        expected_subaccount=subaccount,
        expected_exchange_index=exchange_index,
    )


def _engine_decimal(name: str, value: object) -> Decimal:
    """Conservative bridge for the engine's legacy float risk fields."""
    if isinstance(value, bool):
        raise LedgerSchemaError(f"{name} must be numeric")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise LedgerSchemaError(f"{name} must be a trimmed number")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise LedgerSchemaError(f"{name} is not numeric") from exc
    elif type(value) is int:
        parsed = Decimal(value)
    elif type(value) is float and math.isfinite(value):
        parsed = Decimal(str(value))
    else:
        raise LedgerSchemaError(f"{name} must be a finite number")
    if not parsed.is_finite() or parsed < 0:
        raise LedgerSchemaError(f"{name} must be finite and non-negative")
    return parsed


def _microcontracts_ceil(name: str, value: object) -> int:
    scaled = (
        _engine_decimal(name, value)
        * budget.MICRO_CONTRACTS_PER_CONTRACT
    ).to_integral_value(rounding=ROUND_CEILING)
    result = int(scaled)
    if result <= 0 or result > budget.MAX_I64:
        raise LedgerSchemaError(f"{name} is outside the supported range")
    return result


def _micro_usd_ceil(name: str, value: object) -> int:
    scaled = (
        _engine_decimal(name, value) * budget.MICRO_USD_PER_USD
    ).to_integral_value(rounding=ROUND_CEILING)
    result = int(scaled)
    if result < 0 or result > budget.MAX_I64:
        raise LedgerSchemaError(f"{name} is outside the supported range")
    return result


def _order_cost_micro_usd(price: object, quantity: object) -> int:
    px = _engine_decimal("order risk price", price)
    qty = _engine_decimal("order quantity", quantity)
    if px <= 0 or px >= 1:
        raise LedgerSchemaError("order risk price must be strictly between 0 and 1")
    if qty <= 0:
        raise LedgerSchemaError("order quantity must be positive")
    scaled = (
        px * qty * budget.MICRO_USD_PER_USD
    ).to_integral_value(rounding=ROUND_CEILING)
    result = int(scaled)
    if result <= 0 or result > budget.MAX_I64:
        raise LedgerSchemaError("order cost is outside the supported range")
    return result


def _outcome(engine_side: object) -> str:
    if engine_side == "bid":
        return "yes"
    if engine_side == "ask_no":
        return "no"
    raise LedgerSchemaError(
        f"engine side must be 'bid' or 'ask_no', got {engine_side!r}"
    )


def _key(key: object, group: str) -> tuple[str, str]:
    if (
        not isinstance(key, tuple)
        or len(key) != 2
        or not isinstance(key[0], str)
    ):
        raise LedgerSchemaError(f"{group} key must be (ticker, engine_side)")
    _outcome(key[1])
    return key[0], key[1]


def _risk_from_local(
    *,
    reference: str,
    ticker: str,
    engine_side: str,
    price: object,
    quantity: object,
    fee_reserve: object = "0",
) -> budget.OrderWorstCase:
    quantity_micro = _microcontracts_ceil("order quantity", quantity)
    return budget.OrderWorstCase(
        reference=reference,
        market=ticker,
        outcome=_outcome(engine_side),
        quantity_microcontracts=quantity_micro,
        cost_micro_usd=_order_cost_micro_usd(price, quantity),
        fee_micro_usd=_micro_usd_ceil("fee reserve", fee_reserve),
    )


@dataclass(frozen=True)
class CandidateIntent:
    """Final economic fields checked immediately before an order POST."""

    reference: str
    ticker: str
    engine_side: str
    risk_px: object
    quantity: object
    fee_reserve: object = "0"

    def to_risk(self) -> budget.OrderWorstCase:
        return _risk_from_local(
            reference=self.reference,
            ticker=self.ticker,
            engine_side=self.engine_side,
            price=self.risk_px,
            quantity=self.quantity,
            fee_reserve=self.fee_reserve,
        )


@dataclass(frozen=True)
class LedgerRisks:
    resting: tuple[budget.OrderWorstCase, ...]
    pending: tuple[budget.OrderWorstCase, ...]
    unknown: tuple[budget.OrderWorstCase, ...]
    identities: frozenset[str]


@dataclass
class _RiskEntry:
    group: str
    risk: budget.OrderWorstCase
    aliases: set[str]


def _economic_identity(risk: budget.OrderWorstCase) -> tuple[object, ...]:
    return (
        risk.market,
        risk.outcome,
        risk.quantity_microcontracts,
        risk.cost_micro_usd,
        risk.fee_micro_usd,
    )


def build_ledger_risks(
    *,
    orders: Mapping[object, object],
    pending_new: Mapping[object, object],
    unknown_orders: Mapping[object, object],
    flatten_pending: Mapping[object, object],
) -> LedgerRisks:
    """Translate current ``S`` order maps without dropping or double-counting."""
    for name, value in (
        ("orders", orders),
        ("pending_new", pending_new),
        ("unknown_orders", unknown_orders),
        ("flatten_pending", flatten_pending),
    ):
        if not isinstance(value, Mapping):
            raise LedgerSchemaError(f"{name} must be a mapping")

    entries: dict[int, _RiskEntry] = {}
    alias_registry: dict[str, int] = {}
    next_entry_id = 0

    def register(
        *,
        risk: budget.OrderWorstCase,
        group: str,
        aliases: Sequence[str],
        allow_exact_merge: bool,
    ) -> _RiskEntry:
        nonlocal next_entry_id
        normalized = set()
        for identity in aliases:
            if (
                not isinstance(identity, str)
                or not identity
                or identity != identity.strip()
            ):
                raise LedgerSchemaError("order identity must be a string")
            normalized.add(identity)
        if not normalized:
            raise LedgerSchemaError("order reservation has no identity")
        collisions = {
            alias_registry[identity]
            for identity in normalized
            if identity in alias_registry
        }
        if not collisions:
            entry_id = next_entry_id
            next_entry_id += 1
            entry = _RiskEntry(group=group, risk=risk, aliases=normalized)
            entries[entry_id] = entry
            for identity in normalized:
                alias_registry[identity] = entry_id
            return entry
        if len(collisions) != 1 or not allow_exact_merge:
            raise LedgerSchemaError(
                "order identity collides across risk reservations"
            )
        entry_id = next(iter(collisions))
        entry = entries[entry_id]
        if _economic_identity(entry.risk) != _economic_identity(risk):
            raise LedgerSchemaError(
                "same order identity has conflicting economic reservation"
            )
        entry.group = "unknown"
        entry.aliases.update(normalized)
        for identity in normalized:
            existing = alias_registry.get(identity)
            if existing is not None and existing != entry_id:
                raise LedgerSchemaError(
                    "order aliases connect two different reservations"
                )
            alias_registry[identity] = entry_id
        return entry

    for raw_key, raw_order in orders.items():
        ticker, side = _key(raw_key, "orders")
        if not isinstance(raw_order, Mapping):
            raise LedgerSchemaError("orders values must be objects")
        order_id = raw_order.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise LedgerSchemaError("resting order is missing id")
        try:
            risk = _risk_from_local(
                reference=f"order:{order_id}",
                ticker=ticker,
                engine_side=side,
                price=raw_order["px"],
                quantity=raw_order["qty"],
            )
        except KeyError as exc:
            raise LedgerSchemaError(
                f"resting order {order_id} missing {exc.args[0]}"
            ) from exc
        register(
            risk=risk,
            group="resting",
            aliases=(order_id,),
            allow_exact_merge=False,
        )

    for raw_key, raw_order in pending_new.items():
        ticker, side = _key(raw_key, "pending_new")
        if not isinstance(raw_order, Mapping):
            raise LedgerSchemaError("pending_new values must be objects")
        client_id = raw_order.get("client_order_id")
        if not isinstance(client_id, str) or not client_id:
            raise LedgerSchemaError("pending order is missing client_order_id")
        reference = f"client:{client_id}"
        try:
            risk = _risk_from_local(
                reference=reference,
                ticker=ticker,
                engine_side=side,
                price=raw_order["px"],
                quantity=raw_order["qty"],
            )
        except KeyError as exc:
            raise LedgerSchemaError(
                f"pending order {client_id} missing {exc.args[0]}"
            ) from exc
        register(
            risk=risk,
            group="pending",
            aliases=(client_id,),
            allow_exact_merge=True,
        )

    for raw_order_id, raw_unknown in unknown_orders.items():
        if not isinstance(raw_order_id, str) or not raw_order_id:
            raise LedgerSchemaError("unknown order id must be a string")
        if not isinstance(raw_unknown, Mapping):
            raise LedgerSchemaError("unknown_orders values must be objects")
        existing_id = alias_registry.get(raw_order_id)
        if existing_id is not None:
            entry = entries[existing_id]
            if (
                "ticker" in raw_unknown
                and raw_unknown["ticker"] != entry.risk.market
            ):
                raise LedgerSchemaError(
                    "unknown order ticker conflicts with retained reservation"
                )
            if (
                "side" in raw_unknown
                and _outcome(raw_unknown["side"]) != entry.risk.outcome
            ):
                raise LedgerSchemaError(
                    "unknown order side conflicts with retained reservation"
                )
            if (
                "qty" in raw_unknown
                and _microcontracts_ceil(
                    "unknown order quantity", raw_unknown["qty"]
                ) != entry.risk.quantity_microcontracts
            ):
                raise LedgerSchemaError(
                    "unknown order quantity conflicts with retained reservation"
                )
            if (
                "fee_reserve" in raw_unknown
                and _micro_usd_ceil(
                    "unknown order fee", raw_unknown["fee_reserve"]
                ) != entry.risk.fee_micro_usd
            ):
                raise LedgerSchemaError(
                    "unknown order fee conflicts with retained reservation"
                )
            if "risk_px" in raw_unknown:
                comparison_qty = raw_unknown.get(
                    "qty",
                    Decimal(entry.risk.quantity_microcontracts)
                    / budget.MICRO_CONTRACTS_PER_CONTRACT,
                )
                if (
                    _order_cost_micro_usd(
                        raw_unknown["risk_px"], comparison_qty
                    ) != entry.risk.cost_micro_usd
                ):
                    raise LedgerSchemaError(
                        "unknown order price conflicts with retained reservation"
                    )
            economic_fields = (
                "ticker", "side", "risk_px", "qty", "fee_reserve"
            )
            if all(name in raw_unknown for name in economic_fields):
                compared = _risk_from_local(
                    reference=entry.risk.reference,
                    ticker=raw_unknown["ticker"],
                    engine_side=raw_unknown["side"],
                    price=raw_unknown["risk_px"],
                    quantity=raw_unknown["qty"],
                    fee_reserve=raw_unknown["fee_reserve"],
                )
                register(
                    risk=compared,
                    group="unknown",
                    aliases=(raw_order_id,),
                    allow_exact_merge=True,
                )
            else:
                entry.group = "unknown"
            continue
        required = ("ticker", "side", "risk_px", "qty", "fee_reserve")
        missing = [name for name in required if name not in raw_unknown]
        if missing:
            raise LedgerSchemaError(
                f"unknown order {raw_order_id} has no retained reservation "
                f"and is missing {','.join(missing)}"
            )
        risk = _risk_from_local(
            reference=f"order:{raw_order_id}",
            ticker=raw_unknown["ticker"],
            engine_side=raw_unknown["side"],
            price=raw_unknown["risk_px"],
            quantity=raw_unknown["qty"],
            fee_reserve=raw_unknown["fee_reserve"],
        )
        register(
            risk=risk,
            group="unknown",
            aliases=(raw_order_id,),
            allow_exact_merge=True,
        )

    # An unresolved IOC can already have partially applied fills.  Requiring
    # the remaining economic reservation avoids counting either zero or the
    # original quantity by guesswork.
    for raw_key, raw_order in flatten_pending.items():
        ticker, orphan_side = _key(raw_key, "flatten_pending")
        if not isinstance(raw_order, Mapping):
            raise LedgerSchemaError("flatten_pending values must be objects")
        required = ("risk_px", "remaining_risk_qty", "fee_reserve")
        missing = [name for name in required if name not in raw_order]
        if missing:
            raise LedgerSchemaError(
                "flatten_pending reservation missing " + ",".join(missing)
            )
        buy_side = "ask_no" if orphan_side == "bid" else "bid"
        order_id = raw_order.get("order_id")
        client_id = raw_order.get("client_order_id")
        identities = tuple(
            identity
            for identity in (order_id, client_id)
            if isinstance(identity, str) and identity
        )
        if not identities:
            raise LedgerSchemaError("flatten_pending is missing order identity")
        primary = order_id if isinstance(order_id, str) and order_id else client_id
        risk = _risk_from_local(
            reference=(
                f"order:{primary}" if primary == order_id
                else f"client:{primary}"
            ),
            ticker=ticker,
            engine_side=buy_side,
            price=raw_order["risk_px"],
            quantity=raw_order["remaining_risk_qty"],
            fee_reserve=raw_order["fee_reserve"],
        )
        register(
            risk=risk,
            group="unknown",
            aliases=identities,
            allow_exact_merge=True,
        )

    return LedgerRisks(
        resting=tuple(
            entry.risk for entry in entries.values()
            if entry.group == "resting"
        ),
        pending=tuple(
            entry.risk for entry in entries.values()
            if entry.group == "pending"
        ),
        unknown=tuple(
            entry.risk for entry in entries.values()
            if entry.group == "unknown"
        ),
        identities=frozenset(alias_registry),
    )


_SIGNED_FIXED_RE = re.compile(
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$"
)


def _signed_microcontracts(name: str, value: object) -> int:
    if not isinstance(value, str) or not _SIGNED_FIXED_RE.fullmatch(value):
        raise AdapterError(
            f"{name} must be a signed fixed-point contract string"
        )
    try:
        scaled = Decimal(value) * budget.MICRO_CONTRACTS_PER_CONTRACT
    except InvalidOperation as exc:
        raise AdapterError(f"{name} is not a valid contract count") from exc
    if scaled != scaled.to_integral_value():
        raise AdapterError(f"{name} exceeds micro-contract precision")
    result = int(scaled)
    if abs(result) > budget.MAX_I64:
        raise AdapterError(f"{name} is outside the supported range")
    return result


def parse_positions_response(
    response: object,
    *,
    expected_subaccount: int = 0,
    expected_exchange_index: int = 0,
) -> tuple[budget.MarketPosition, ...]:
    """Parse a complete REST positions collection into gross outcome rows."""
    if type(expected_subaccount) is not int or not 0 <= expected_subaccount <= 63:
        raise AdapterError("expected_subaccount is invalid")
    if type(expected_exchange_index) is not int or expected_exchange_index < 0:
        raise AdapterError("expected_exchange_index is invalid")
    if not isinstance(response, Mapping):
        raise AdapterError("positions response must be an object")
    rows = response.get("market_positions")
    if not isinstance(rows, list):
        raise AdapterError("positions response requires market_positions array")
    if response.get("cursor") or response.get("next_cursor"):
        raise AdapterError("positions response is paginated/incomplete")
    positions = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise AdapterError("position rows must be objects")
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise AdapterError("position row is missing ticker")
        if ticker in seen:
            raise AdapterError("duplicate position ticker")
        # Wire contract (measured against the live API 2026-07-27, first
        # real position): /portfolio/positions rows OMIT subaccount_number
        # and exchange_index entirely.  The request is already scoped to the
        # authenticated key, and the account-level binding is enforced by
        # account_fingerprint plus the balance response's exchange_index, so
        # an absent field is the documented shape -- not an identity gap.
        # A field that IS present must still match: a future API that starts
        # returning another subaccount's rows must fail closed.
        row_subaccount = row.get("subaccount_number")
        if (row_subaccount is not None
                and row_subaccount != expected_subaccount):
            raise AdapterError("position row subaccount_number mismatch")
        row_exchange_index = row.get("exchange_index")
        if (row_exchange_index is not None
                and row_exchange_index != expected_exchange_index):
            raise AdapterError("position row exchange_index mismatch")
        if "position_fp" not in row:
            raise AdapterError("position row is missing position_fp")
        signed = _signed_microcontracts("position_fp", row["position_fp"])
        positions.append(
            budget.MarketPosition(
                market=ticker,
                yes_microcontracts=max(signed, 0),
                no_microcontracts=max(-signed, 0),
            )
        )
        seen.add(ticker)
    return tuple(positions)


def validate_local_positions(
    local_net_pos: Mapping[object, object],
    exchange_positions: Sequence[budget.MarketPosition],
    recent_fill_markets: Mapping[str, float] | None = None,
    grace_s: float = 10.0,
    now_mono: float | None = None,
    open_markets: object = None,
) -> None:
    """Require ``S.net_pos`` signed quantities to equal REST positions.

    ``recent_fill_markets`` (ticker -> monotonic ts of our latest local
    fill) covers the THIRD read-replica race observed live 2026-07-27:
    the fills stream told us about an execution before the positions
    endpoint reflected it (local=1, exchange=0 one second after a fill).
    A mismatch on a market we filled within ``grace_s`` is propagation
    lag, not divergence -- anything else still trips.
    """
    if not isinstance(local_net_pos, Mapping):
        raise AdapterError("local net_pos must be a mapping")
    local: dict[str, int] = {}
    for ticker, raw in local_net_pos.items():
        if not isinstance(ticker, str) or not ticker:
            raise AdapterError("local net_pos ticker is invalid")
        if not isinstance(raw, Mapping):
            raise AdapterError("local net_pos values must be objects")
        if "y" not in raw or "n" not in raw:
            raise AdapterError("local net_pos row requires y and n")
        yes_qty = _microcontracts_ceil("local YES quantity", raw["y"]) if (
            _engine_decimal("local YES quantity", raw["y"]) > 0
        ) else 0
        no_qty = _microcontracts_ceil("local NO quantity", raw["n"]) if (
            _engine_decimal("local NO quantity", raw["n"]) > 0
        ) else 0
        local[ticker] = yes_qty - no_qty
    exchange = {
        position.market: (
            position.yes_microcontracts - position.no_microcontracts
        )
        for position in exchange_positions
    }
    all_markets = set(local) | set(exchange)
    # Float-dust tolerance (race #12, 2026-07-28T01:00): fractional fills
    # (count_fp 1.64 etc.) accumulate binary-float crumbs and the local
    # ceil rounding then differs from the exchange by ONE micro-contract
    # (local=1640001 vs exchange=1640000).  1000 micro = 0.001 contract =
    # at most 0.1 cent of notional -- pure representation noise, no
    # economic content.  Real divergences are 6 orders of magnitude bigger.
    EPS_MICRO = 1000
    mismatches = [
        (ticker, local.get(ticker, 0), exchange.get(ticker, 0))
        for ticker in sorted(all_markets)
        if abs(local.get(ticker, 0) - exchange.get(ticker, 0)) > EPS_MICRO
    ]
    if mismatches and open_markets is not None:
        # Race #5 (settlement clearing, observed live 2026-07-27T22:00:21):
        # the exchange zeroes a position AT settlement, seconds before the
        # engine's settlement flow reconciles the local ledger.  A market
        # no longer in the OPEN set is in its settlement window -- its
        # terminal accounting arrives via the settlements pipeline and the
        # balance snapshot, so it leaves the live comparison.  Markets that
        # ARE open keep the strict check.
        mismatches = [m for m in mismatches if m[0] in open_markets]
    if mismatches:
        if recent_fill_markets:
            import time as _time
            now_m = _time.monotonic() if now_mono is None else now_mono
            mismatches = [
                m for m in mismatches
                if now_m - recent_fill_markets.get(m[0], -1e9) > grace_s
            ]
        if mismatches:
            ticker, local_qty, exchange_qty = mismatches[0]
            raise AdapterError(
                "local/REST position mismatch "
                f"{ticker}: local={local_qty}, exchange={exchange_qty}"
            )


def parse_user_data_timestamp(response: object) -> float:
    """Strictly parse ``GET /exchange/user_data_timestamp`` metadata."""
    if not isinstance(response, Mapping):
        raise AdapterError("user data timestamp response must be an object")
    raw = response.get("as_of_time")
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise AdapterError("user data timestamp requires as_of_time string")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdapterError("user data as_of_time is unparseable") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdapterError("user data as_of_time requires a timezone")
    result = parsed.timestamp()
    if not math.isfinite(result) or result < 0:
        raise AdapterError("user data as_of_time is outside range")
    return result


def validate_user_data_timestamp(
    as_of_s: float,
    *,
    request_started_s: float,
    response_finished_s: float,
    previous_as_of_s: Optional[float] = None,
    future_slack_s: float = 1.0,
) -> None:
    """Reject malformed, future, or regressing validation metadata.

    The endpoint is documented as an approximate validation time.  No
    maximum-age threshold is imposed yet: its deadline must be calibrated
    and proven nonblocking under REST latency/429s before live promotion.
    """
    values = (
        as_of_s,
        request_started_s,
        response_finished_s,
        future_slack_s,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        raise AdapterError("user data timestamp timing must be finite")
    if request_started_s > response_finished_s:
        raise AdapterError("user data timestamp response precedes request")
    if future_slack_s < 0:
        raise AdapterError("user data timestamp future slack is invalid")
    if as_of_s > response_finished_s + future_slack_s:
        raise AdapterError("user data as_of_time is in the future")
    if previous_as_of_s is not None:
        if (
            isinstance(previous_as_of_s, bool)
            or not isinstance(previous_as_of_s, (int, float))
            or not math.isfinite(float(previous_as_of_s))
        ):
            raise AdapterError("previous user data as_of_time is invalid")
        if as_of_s < previous_as_of_s:
            raise AdapterError("user data as_of_time regressed")


@dataclass(frozen=True)
class SnapshotInputs:
    balance_response: object
    positions_response: object
    user_data_timestamp_response: object
    balance_request_started_s: float
    balance_response_finished_s: float
    user_data_timestamp_request_started_s: float
    user_data_timestamp_response_finished_s: float
    ledger_generation_before: int
    ledger_generation_after: int
    previous_balance_updated_ts: Optional[int] = None
    previous_user_data_as_of_s: Optional[float] = None


@dataclass(frozen=True)
class Assessment:
    record: budget.BudgetRecord
    snapshot: budget.BalanceSnapshot
    user_data_as_of_s: float
    positions: tuple[budget.MarketPosition, ...]
    risks: LedgerRisks
    baseline: budget.EquityComputation
    projected: budget.EquityComputation
    candidate: Optional[budget.OrderWorstCase]
    allowed: bool
    trip_required: bool
    reason: str


def assess(
    *,
    record: budget.BudgetRecord,
    snapshot_inputs: SnapshotInputs,
    local_net_pos: Mapping[object, object],
    orders: Mapping[object, object],
    pending_new: Mapping[object, object],
    unknown_orders: Mapping[object, object],
    flatten_pending: Mapping[object, object],
    candidate: Optional[CandidateIntent] = None,
    additional_buffer_micro_usd: int = 0,
    recent_fill_markets: Optional[Mapping[str, float]] = None,
    open_markets: object = None,
) -> Assessment:
    """Build baseline and candidate E_safe from one generation-stable view."""
    before = snapshot_inputs.ledger_generation_before
    after = snapshot_inputs.ledger_generation_after
    if type(before) is not int or type(after) is not int or before < 0 or after < 0:
        raise SnapshotRace("ledger generations must be non-negative integers")
    if before != after:
        raise SnapshotRace(
            f"ledger changed during snapshot ({before} -> {after})"
        )
    record = budget.validate_budget_record(record)
    snapshot = budget.parse_balance_response(
        snapshot_inputs.balance_response,
        expected_exchange_index=record.exchange_index,
    )
    budget.validate_snapshot_freshness(
        snapshot,
        request_started_s=snapshot_inputs.balance_request_started_s,
        response_finished_s=snapshot_inputs.balance_response_finished_s,
        previous_updated_ts=snapshot_inputs.previous_balance_updated_ts,
    )
    user_data_as_of_s = parse_user_data_timestamp(
        snapshot_inputs.user_data_timestamp_response
    )
    validate_user_data_timestamp(
        user_data_as_of_s,
        request_started_s=(
            snapshot_inputs.user_data_timestamp_request_started_s
        ),
        response_finished_s=(
            snapshot_inputs.user_data_timestamp_response_finished_s
        ),
        previous_as_of_s=snapshot_inputs.previous_user_data_as_of_s,
    )
    positions = parse_positions_response(
        snapshot_inputs.positions_response,
        expected_subaccount=record.subaccount,
        expected_exchange_index=record.exchange_index,
    )
    validate_local_positions(local_net_pos, positions,
                             recent_fill_markets=recent_fill_markets,
                             open_markets=open_markets)
    risks = build_ledger_risks(
        orders=orders,
        pending_new=pending_new,
        unknown_orders=unknown_orders,
        flatten_pending=flatten_pending,
    )
    if candidate is not None and candidate.reference in risks.identities:
        raise LedgerSchemaError(
            "candidate identity is already reserved in the risk ledger"
        )
    candidate_risk = None if candidate is None else candidate.to_risk()
    baseline = budget.compute_safe_equity(
        snapshot,
        positions=positions,
        resting=risks.resting,
        pending=risks.pending,
        unknown=risks.unknown,
    )
    projected = budget.compute_safe_equity(
        snapshot,
        positions=positions,
        resting=risks.resting,
        pending=risks.pending,
        unknown=risks.unknown,
        candidate=candidate_risk,
    )
    baseline_ok = budget.budget_allows(
        record,
        baseline,
        additional_buffer_micro_usd=additional_buffer_micro_usd,
    )
    projected_ok = budget.budget_allows(
        record,
        projected,
        additional_buffer_micro_usd=additional_buffer_micro_usd,
    )
    if record.latched:
        reason = "budget is durably latched"
    elif not baseline_ok:
        reason = "current E_safe is below the absolute floor"
    elif not projected_ok:
        reason = "candidate would push E_safe below the absolute floor"
    else:
        reason = "safe"
    return Assessment(
        record=record,
        snapshot=snapshot,
        user_data_as_of_s=user_data_as_of_s,
        positions=positions,
        risks=risks,
        baseline=baseline,
        projected=projected,
        candidate=candidate_risk,
        allowed=baseline_ok and projected_ok,
        trip_required=record.latched or not baseline_ok,
        reason=reason,
    )


@dataclass(frozen=True)
class TripReceipt:
    reason: str
    record: budget.BudgetRecord
    durable_latched: bool
    cancel_complete: bool
    halt_confirmed: bool
    complete: bool
    marker_error: Optional[str]
    latch_error: Optional[str]
    cancel_error: Optional[str]
    halt_error: Optional[str]


class BudgetGuard:
    """Startup/preflight/1 Hz single-step guard with injected side effects."""

    def __init__(
        self,
        *,
        budget_path: Path,
        account_identity: str,
        cancel_all: Callable[[str], object],
        halt: Callable[[str], object],
        subaccount: int = 0,
        exchange_index: int = 0,
        additional_buffer_micro_usd: int = 0,
    ) -> None:
        self.budget_path = Path(budget_path)
        self.account_identity = account_identity
        self.subaccount = subaccount
        self.exchange_index = exchange_index
        self.record = load_bound_budget(
            self.budget_path,
            account_identity=account_identity,
            subaccount=subaccount,
            exchange_index=exchange_index,
        )
        if not callable(cancel_all) or not callable(halt):
            raise AdapterError("cancel_all and halt must be callable")
        if (
            type(additional_buffer_micro_usd) is not int
            or additional_buffer_micro_usd < 0
        ):
            raise AdapterError("additional buffer must be non-negative integer")
        self.cancel_all = cancel_all
        self.halt = halt
        self.additional_buffer_micro_usd = additional_buffer_micro_usd
        self._immutable_identity = self.record.immutable_identity
        self._trip_marker_path = self.budget_path.with_name(
            self.budget_path.name + ".trip-pending"
        )
        self._cancel_confirmed = False
        self._halt_confirmed = False
        self._trip_receipt: Optional[TripReceipt] = None

    def _identity_digest(self) -> str:
        encoded = json.dumps(
            list(self._immutable_identity),
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _read_trip_marker(self) -> Optional[dict[str, object]]:
        try:
            raw = self._trip_marker_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise AdapterError(f"cannot read trip marker: {exc}") from exc
        try:
            marker = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterError("trip marker is corrupt") from exc
        if not isinstance(marker, dict):
            raise AdapterError("trip marker must be an object")
        expected_fields = {
            "schema_version",
            "budget_id",
            "identity_sha256",
            "reason",
        }
        if set(marker) != expected_fields:
            raise AdapterError("trip marker fields are invalid")
        if marker["schema_version"] != "crypto-mm-trip-pending-v1":
            raise AdapterError("trip marker schema is invalid")
        if marker["budget_id"] != self.record.budget_id:
            raise AdapterError("trip marker budget_id mismatch")
        if marker["identity_sha256"] != self._identity_digest():
            raise AdapterError("trip marker immutable identity mismatch")
        if not isinstance(marker["reason"], str) or not marker["reason"]:
            raise AdapterError("trip marker reason is invalid")
        return marker

    def _ensure_trip_marker(self, reason: str) -> None:
        existing = self._read_trip_marker()
        if existing is not None:
            return
        payload = (
            json.dumps(
                {
                    "schema_version": "crypto-mm-trip-pending-v1",
                    "budget_id": self.record.budget_id,
                    "identity_sha256": self._identity_digest(),
                    "reason": reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(self._trip_marker_path.parent),
                prefix=f".{self._trip_marker_path.name}.",
                delete=False,
            ) as handle:
                tmp_name = handle.name
                os.chmod(tmp_name, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp_name, str(self._trip_marker_path))
            except FileExistsError:
                self._read_trip_marker()
            directory_fd = os.open(
                str(self._trip_marker_path.parent), os.O_RDONLY
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    def _reload_record(
        self,
        *,
        allow_trip_pending: bool = False,
    ) -> budget.BudgetRecord:
        current = load_bound_budget(
            self.budget_path,
            account_identity=self.account_identity,
            subaccount=self.subaccount,
            exchange_index=self.exchange_index,
        )
        if current.immutable_identity != self._immutable_identity:
            raise AdapterError("durable budget immutable identity changed")
        if current.generation < self.record.generation:
            raise AdapterError("durable budget generation regressed")
        if self.record.latched and not current.latched:
            raise AdapterError("durable budget latch regressed")
        self.record = current
        if not current.latched and not allow_trip_pending:
            marker = self._read_trip_marker()
            if marker is not None:
                raise AdapterError("durable trip-pending marker is active")
        return current

    def _trip(
        self,
        reason: str,
        *,
        reservations_clear: bool,
    ) -> TripReceipt:
        safe_reason = " ".join(str(reason).split())[:200] or "budget trip"
        marker_error = None
        try:
            self._ensure_trip_marker(safe_reason)
        except Exception as exc:
            marker_error = f"{type(exc).__name__}: {exc}"
        latch_error = None
        durable_latched = False
        try:
            durable_latched = self._reload_record(
                allow_trip_pending=True
            ).latched
        except Exception as exc:
            latch_error = f"{type(exc).__name__}: {exc}"
        if latch_error is None and not durable_latched:
            try:
                self.record = budget.latch_budget(
                    self.budget_path,
                    expected=self.record,
                    reason=safe_reason,
                )
                durable_latched = self.record.latched
            except Exception as exc:  # cancel/halt even if disk is unhealthy
                latch_error = f"{type(exc).__name__}: {exc}"

        cancel_error = None
        if not self._cancel_confirmed or not reservations_clear:
            try:
                cancel_result = self.cancel_all("BUDGET_LATCH")
                if cancel_result is True:
                    self._cancel_confirmed = True
                else:
                    self._cancel_confirmed = False
                    cancel_error = (
                        "cancel_all returned False"
                        if cancel_result is False
                        else "cancel_all did not return True"
                    )
            except Exception as exc:
                self._cancel_confirmed = False
                cancel_error = f"{type(exc).__name__}: {exc}"
        cancel_complete = self._cancel_confirmed and reservations_clear

        halt_error = None
        if (
            not self._halt_confirmed
            or not durable_latched
            or not cancel_complete
        ):
            try:
                halt_result = self.halt(safe_reason)
                if halt_result is False:
                    self._halt_confirmed = False
                    halt_error = "halt returned False"
                else:
                    self._halt_confirmed = True
            except Exception as exc:
                self._halt_confirmed = False
                halt_error = f"{type(exc).__name__}: {exc}"

        complete = (
            durable_latched
            and cancel_complete
            and self._halt_confirmed
        )
        self._trip_receipt = TripReceipt(
            reason=safe_reason,
            record=self.record,
            durable_latched=durable_latched,
            cancel_complete=cancel_complete,
            halt_confirmed=self._halt_confirmed,
            complete=complete,
            marker_error=marker_error,
            latch_error=latch_error,
            cancel_error=cancel_error,
            halt_error=halt_error,
        )
        return self._trip_receipt

    def _assess(self, **kwargs: Any) -> Assessment:
        self._reload_record()
        return assess(
            record=self.record,
            additional_buffer_micro_usd=self.additional_buffer_micro_usd,
            **kwargs,
        )

    def trip_blind(
        self,
        *,
        stage: str,
        error: object,
        reservations_clear: bool = False,
    ) -> TripReceipt:
        """Public fail-closed hook for errors while acquiring engine inputs.

        ``startup_check``/``preflight``/``monitor_once`` own validation once
        a ``SnapshotInputs`` object exists.  Pagination, HTTP, or exchange
        open-order reconciliation can fail before that object can be built;
        the engine calls this hook so those acquisition failures receive the
        same durable latch/cancel/halt sequence instead of escaping the guard.
        """
        normalized_stage = "_".join(str(stage).upper().split())
        if not normalized_stage or len(normalized_stage) > 40:
            normalized_stage = "SNAPSHOT"
        return self._trip(
            f"BUDGET_{normalized_stage}_BLIND "
            f"{type(error).__name__}: {error}",
            reservations_clear=reservations_clear,
        )

    @staticmethod
    def _reservations_clear(result: Assessment) -> bool:
        return not (
            result.risks.resting
            or result.risks.pending
            or result.risks.unknown
        )

    def startup_check(self, **kwargs: Any) -> Assessment:
        """Ignition gate; a latched or blind restart cannot quote."""
        try:
            result = self._assess(**kwargs)
        except Exception as exc:
            self._trip(
                f"BUDGET_STARTUP_BLIND {type(exc).__name__}: {exc}",
                reservations_clear=False,
            )
            raise
        if result.trip_required:
            self._trip(
                f"BUDGET_STARTUP_BLOCK {result.reason}",
                reservations_clear=self._reservations_clear(result),
            )
        return result

    def preflight(
        self,
        *,
        candidate: CandidateIntent,
        **kwargs: Any,
    ) -> Assessment:
        """Final decision immediately before POST.

        A candidate-only breach is rejected without latching so a genuinely
        risk-reducing alternative can still be considered.  An already-unsafe
        baseline or any blind input latches, cancels, and halts.
        """
        try:
            result = self._assess(candidate=candidate, **kwargs)
        except Exception as exc:
            self._trip(
                f"BUDGET_PREFLIGHT_BLIND {type(exc).__name__}: {exc}",
                reservations_clear=False,
            )
            raise
        if result.trip_required:
            self._trip(
                f"BUDGET_PREFLIGHT_TRIP {result.reason}",
                reservations_clear=self._reservations_clear(result),
            )
        return result

    def monitor_once(self, **kwargs: Any) -> Assessment:
        """One independent monitor iteration; call on a 1 Hz engine task."""
        try:
            result = self._assess(**kwargs)
        except Exception as exc:
            self._trip(
                f"BUDGET_MONITOR_BLIND {type(exc).__name__}: {exc}",
                reservations_clear=False,
            )
            raise
        if result.trip_required:
            self._trip(
                f"BUDGET_MONITOR_TRIP {result.reason}",
                reservations_clear=self._reservations_clear(result),
            )
        return result

    @property
    def trip_receipt(self) -> Optional[TripReceipt]:
        return self._trip_receipt
