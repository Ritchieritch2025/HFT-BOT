"""Deterministic fill authority for the shared research PnL spine.

Only two execution authorities exist here:

``PUBLIC_STRICT_THROUGH``
    A passive order may fill only from a later, matching-direction public
    trade strictly through its price.  One public trade id is allocated to at
    most one virtual order, globally, and its quantity is never exceeded.

``EXACT_L2_IOC``
    A marketable limit order walks one exact, receive-clock L2 snapshot in
    executable price order.  Any unfilled suffix is cancelled.

There is deliberately no at-touch fill, midpoint fill, inferred cancellation,
or floating-point arithmetic in this module.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence


PRICE_SCALE_E4 = 10_000
ONE_CONTRACT_E4 = 10_000
MIN_PRICE_E4 = 1
MAX_PRICE_E4 = 9_999


class FillError(ValueError):
    """The fill request or market evidence violated a fail-closed contract."""


class OutcomeSide(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class LiquidityRole(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


def _plain_int(name: str, value: object, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise FillError(f"{name} must be a plain integer")
    if minimum is not None and value < minimum:
        raise FillError(f"{name} must be >= {minimum}")
    return value


def _price(name: str, value: object) -> int:
    price = _plain_int(name, value)
    if not MIN_PRICE_E4 <= price <= MAX_PRICE_E4:
        raise FillError(f"{name} must be in [{MIN_PRICE_E4}, {MAX_PRICE_E4}]")
    return price


def _nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise FillError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class PassiveOrder:
    order_id: str
    experiment_id: str
    root_id: str
    market_ticker: str
    side: OutcomeSide
    price_e4: int
    quantity_e4: int
    activation_us: int
    cancel_effective_us: int

    def __post_init__(self) -> None:
        _nonempty("order_id", self.order_id)
        _nonempty("experiment_id", self.experiment_id)
        _nonempty("root_id", self.root_id)
        _nonempty("market_ticker", self.market_ticker)
        if not isinstance(self.side, OutcomeSide):
            raise FillError("side must be an OutcomeSide")
        _price("price_e4", self.price_e4)
        _plain_int("quantity_e4", self.quantity_e4, minimum=1)
        _plain_int("activation_us", self.activation_us, minimum=0)
        _plain_int("cancel_effective_us", self.cancel_effective_us, minimum=0)
        if self.cancel_effective_us < self.activation_us:
            raise FillError("cancel_effective_us precedes activation_us")

    @property
    def touch_yes_price_e4(self) -> int:
        return (
            self.price_e4
            if self.side is OutcomeSide.YES
            else PRICE_SCALE_E4 - self.price_e4
        )


@dataclass(frozen=True)
class PublicTrade:
    trade_id: str
    market_ticker: str
    timestamp_us: int
    yes_price_e4: int
    quantity_e4: int
    taker_side: OutcomeSide

    def __post_init__(self) -> None:
        _nonempty("trade_id", self.trade_id)
        _nonempty("market_ticker", self.market_ticker)
        _plain_int("timestamp_us", self.timestamp_us, minimum=0)
        _price("yes_price_e4", self.yes_price_e4)
        _plain_int("quantity_e4", self.quantity_e4, minimum=1)
        if not isinstance(self.taker_side, OutcomeSide):
            raise FillError("taker_side must be an OutcomeSide")


@dataclass(frozen=True)
class L2Level:
    yes_price_e4: int
    quantity_e4: int

    def __post_init__(self) -> None:
        _price("yes_price_e4", self.yes_price_e4)
        _plain_int("quantity_e4", self.quantity_e4, minimum=1)


@dataclass(frozen=True)
class L2Snapshot:
    snapshot_id: str
    market_ticker: str
    receive_timestamp_us: int
    yes_bids: tuple[L2Level, ...]
    yes_asks: tuple[L2Level, ...]
    book_valid: bool = True
    gap_free: bool = True

    def __post_init__(self) -> None:
        _nonempty("snapshot_id", self.snapshot_id)
        _nonempty("market_ticker", self.market_ticker)
        _plain_int("receive_timestamp_us", self.receive_timestamp_us, minimum=0)
        if type(self.book_valid) is not bool or type(self.gap_free) is not bool:
            raise FillError("book validity flags must be bool")
        if tuple(sorted(self.yes_bids, key=lambda x: -x.yes_price_e4)) != self.yes_bids:
            raise FillError("yes_bids must be strictly executable-price ordered")
        if tuple(sorted(self.yes_asks, key=lambda x: x.yes_price_e4)) != self.yes_asks:
            raise FillError("yes_asks must be strictly executable-price ordered")
        bid_prices = [level.yes_price_e4 for level in self.yes_bids]
        ask_prices = [level.yes_price_e4 for level in self.yes_asks]
        if len(bid_prices) != len(set(bid_prices)):
            raise FillError("yes_bids contains duplicate price levels")
        if len(ask_prices) != len(set(ask_prices)):
            raise FillError("yes_asks contains duplicate price levels")
        if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
            raise FillError("L2 snapshot is locked or crossed")


@dataclass(frozen=True)
class MarketableOrder:
    order_id: str
    experiment_id: str
    root_id: str
    market_ticker: str
    side: OutcomeSide
    action: OrderAction
    limit_price_e4: int
    quantity_e4: int
    effective_timestamp_us: int

    def __post_init__(self) -> None:
        _nonempty("order_id", self.order_id)
        _nonempty("experiment_id", self.experiment_id)
        _nonempty("root_id", self.root_id)
        _nonempty("market_ticker", self.market_ticker)
        if not isinstance(self.side, OutcomeSide):
            raise FillError("side must be an OutcomeSide")
        if not isinstance(self.action, OrderAction):
            raise FillError("action must be an OrderAction")
        _price("limit_price_e4", self.limit_price_e4)
        _plain_int("quantity_e4", self.quantity_e4, minimum=1)
        _plain_int(
            "effective_timestamp_us", self.effective_timestamp_us, minimum=0
        )


@dataclass(frozen=True)
class FillSlice:
    fill_id: str
    order_id: str
    experiment_id: str
    root_id: str
    market_ticker: str
    side: OutcomeSide
    action: OrderAction
    liquidity_role: LiquidityRole
    price_e4: int
    quantity_e4: int
    timestamp_us: int
    source_id: str
    reason: str

    def __post_init__(self) -> None:
        _nonempty("fill_id", self.fill_id)
        _nonempty("source_id", self.source_id)
        _price("price_e4", self.price_e4)
        _plain_int("quantity_e4", self.quantity_e4, minimum=1)
        _plain_int("timestamp_us", self.timestamp_us, minimum=0)


@dataclass(frozen=True)
class FillBatch:
    fills: tuple[FillSlice, ...]
    ordered_e4: int
    filled_e4: int
    unfilled_e4: int
    source_quantity_e4: int
    consumed_source_quantity_e4: int
    duplicate_source_allocations: int

    def __post_init__(self) -> None:
        for field_name in (
            "ordered_e4",
            "filled_e4",
            "unfilled_e4",
            "source_quantity_e4",
            "consumed_source_quantity_e4",
            "duplicate_source_allocations",
        ):
            _plain_int(field_name, getattr(self, field_name), minimum=0)
        if self.filled_e4 + self.unfilled_e4 != self.ordered_e4:
            raise FillError("order quantity conservation failed")
        if self.consumed_source_quantity_e4 > self.source_quantity_e4:
            raise FillError("fills exceed authoritative source quantity")
        if sum(row.quantity_e4 for row in self.fills) != self.filled_e4:
            raise FillError("fill-slice quantity does not match batch")


def _strict_match(order: PassiveOrder, trade: PublicTrade) -> bool:
    """Whether a public print proves a passive fill strictly through price."""
    if order.side is OutcomeSide.YES:
        return (
            trade.taker_side is OutcomeSide.NO
            and trade.yes_price_e4 < order.touch_yes_price_e4
        )
    return (
        trade.taker_side is OutcomeSide.YES
        and trade.yes_price_e4 > order.touch_yes_price_e4
    )


def allocate_passive_strict_fills(
    orders: Iterable[PassiveOrder], trades: Iterable[PublicTrade]
) -> FillBatch:
    """Allocate public prints once to passive orders in canonical time order.

    The allocation is intentionally conservative: once an eligible public
    trade id is assigned to the earliest active order, unused quantity from
    that same public print is not recycled into another counterfactual order.
    This prevents one observed aggressor print from manufacturing several
    independent virtual fills.
    """
    order_rows = tuple(orders)
    trade_rows = tuple(trades)
    if any(not isinstance(row, PassiveOrder) for row in order_rows):
        raise FillError("orders must contain PassiveOrder rows only")
    if any(not isinstance(row, PublicTrade) for row in trade_rows):
        raise FillError("trades must contain PublicTrade rows only")
    order_ids = [row.order_id for row in order_rows]
    trade_ids = [row.trade_id for row in trade_rows]
    if len(order_ids) != len(set(order_ids)):
        raise FillError("duplicate order_id")
    if len(trade_ids) != len(set(trade_ids)):
        raise FillError("duplicate trade_id")

    canonical_trades = tuple(
        sorted(
            trade_rows,
            key=lambda row: (
                row.market_ticker,
                row.timestamp_us,
                row.trade_id,
            ),
        )
    )
    # Reject rather than silently repair a noncanonical tape.
    if canonical_trades != trade_rows:
        raise FillError("public trade tape is not canonically ordered")

    ordered = sorted(
        order_rows,
        key=lambda row: (
            row.activation_us,
            row.order_id,
        ),
    )
    remaining = {row.order_id: row.quantity_e4 for row in ordered}
    by_market: dict[str, list[PublicTrade]] = defaultdict(list)
    for trade in trade_rows:
        by_market[trade.market_ticker].append(trade)

    allocated: set[str] = set()
    fills: list[FillSlice] = []
    consumed: Counter[str] = Counter()
    for order in ordered:
        for trade in by_market.get(order.market_ticker, ()):
            if remaining[order.order_id] == 0:
                break
            if trade.trade_id in allocated:
                continue
            # Same-time ambiguity is adverse: no fill at activation, but a
            # print at cancel-effective is counted before cancellation wins.
            if not (
                order.activation_us < trade.timestamp_us
                <= order.cancel_effective_us
            ):
                continue
            if not _strict_match(order, trade):
                continue
            allocated.add(trade.trade_id)
            quantity = min(remaining[order.order_id], trade.quantity_e4)
            remaining[order.order_id] -= quantity
            consumed[trade.trade_id] += quantity
            fill_number = len(fills)
            fills.append(
                FillSlice(
                    fill_id=f"{order.order_id}|STRICT|{trade.trade_id}|{fill_number}",
                    order_id=order.order_id,
                    experiment_id=order.experiment_id,
                    root_id=order.root_id,
                    market_ticker=order.market_ticker,
                    side=order.side,
                    action=OrderAction.BUY,
                    liquidity_role=LiquidityRole.MAKER,
                    price_e4=order.price_e4,
                    quantity_e4=quantity,
                    timestamp_us=trade.timestamp_us,
                    source_id=trade.trade_id,
                    reason="PUBLIC_STRICT_THROUGH",
                )
            )

    source_by_id = {row.trade_id: row.quantity_e4 for row in trade_rows}
    for source_id, quantity in consumed.items():
        if quantity > source_by_id[source_id]:
            raise FillError("allocated fill exceeds public trade quantity")
    source_counts = Counter(row.source_id for row in fills)
    duplicates = sum(max(0, count - 1) for count in source_counts.values())
    if duplicates:
        raise FillError("one public trade was allocated to multiple fill slices")

    ordered_e4 = sum(row.quantity_e4 for row in order_rows)
    filled_e4 = sum(row.quantity_e4 for row in fills)
    return FillBatch(
        fills=tuple(fills),
        ordered_e4=ordered_e4,
        filled_e4=filled_e4,
        unfilled_e4=ordered_e4 - filled_e4,
        source_quantity_e4=sum(source_by_id.values()),
        consumed_source_quantity_e4=sum(consumed.values()),
        duplicate_source_allocations=0,
    )


def _outcome_price(side: OutcomeSide, yes_price_e4: int) -> int:
    return (
        yes_price_e4
        if side is OutcomeSide.YES
        else PRICE_SCALE_E4 - yes_price_e4
    )


def _marketable_levels(
    order: MarketableOrder, snapshot: L2Snapshot
) -> Sequence[L2Level]:
    # BUY YES and SELL NO consume YES asks.  BUY NO and SELL YES consume YES
    # bids.  Ordering in outcome-price terms is preserved by the stored book
    # ordering and the YES/NO complement.
    consume_asks = (
        order.action is OrderAction.BUY and order.side is OutcomeSide.YES
    ) or (
        order.action is OrderAction.SELL and order.side is OutcomeSide.NO
    )
    return snapshot.yes_asks if consume_asks else snapshot.yes_bids


def _within_limit(
    order: MarketableOrder, outcome_price_e4: int
) -> bool:
    if order.action is OrderAction.BUY:
        return outcome_price_e4 <= order.limit_price_e4
    return outcome_price_e4 >= order.limit_price_e4


def walk_exact_l2_ioc(
    order: MarketableOrder,
    snapshot: L2Snapshot,
    *,
    maximum_snapshot_age_us: int,
) -> FillBatch:
    """Execute a marketable limit against one exact receive-clock snapshot."""
    if not isinstance(order, MarketableOrder):
        raise FillError("order must be a MarketableOrder")
    if not isinstance(snapshot, L2Snapshot):
        raise FillError("snapshot must be an L2Snapshot")
    _plain_int("maximum_snapshot_age_us", maximum_snapshot_age_us, minimum=0)
    if snapshot.market_ticker != order.market_ticker:
        raise FillError("snapshot market does not match order")
    if not snapshot.book_valid or not snapshot.gap_free:
        raise FillError("snapshot is invalid or crosses a capture gap")
    if snapshot.receive_timestamp_us > order.effective_timestamp_us:
        raise FillError("future L2 snapshot cannot execute an earlier order")
    if (
        order.effective_timestamp_us - snapshot.receive_timestamp_us
        > maximum_snapshot_age_us
    ):
        raise FillError("L2 snapshot is stale at order-effective time")

    remaining = order.quantity_e4
    fills: list[FillSlice] = []
    source_total = 0
    consumed = 0
    for level_index, level in enumerate(_marketable_levels(order, snapshot)):
        outcome_price = _outcome_price(order.side, level.yes_price_e4)
        if not _within_limit(order, outcome_price):
            break
        source_total += level.quantity_e4
        if remaining == 0:
            continue
        quantity = min(remaining, level.quantity_e4)
        remaining -= quantity
        consumed += quantity
        fills.append(
            FillSlice(
                fill_id=f"{order.order_id}|IOC|{snapshot.snapshot_id}|{level_index}",
                order_id=order.order_id,
                experiment_id=order.experiment_id,
                root_id=order.root_id,
                market_ticker=order.market_ticker,
                side=order.side,
                action=order.action,
                liquidity_role=LiquidityRole.TAKER,
                price_e4=outcome_price,
                quantity_e4=quantity,
                timestamp_us=order.effective_timestamp_us,
                source_id=f"{snapshot.snapshot_id}|level={level_index}",
                reason="EXACT_L2_IOC",
            )
        )

    filled = order.quantity_e4 - remaining
    return FillBatch(
        fills=tuple(fills),
        ordered_e4=order.quantity_e4,
        filled_e4=filled,
        unfilled_e4=remaining,
        source_quantity_e4=source_total,
        consumed_source_quantity_e4=consumed,
        duplicate_source_allocations=0,
    )


def assert_portfolio_fill_conservation(
    batches: Iterable[FillBatch],
    *,
    authoritative_source_quantity: Mapping[str, int] | None = None,
) -> None:
    """Recheck portfolio-wide identities across experiment runner batches."""
    rows = tuple(batches)
    fill_ids: set[str] = set()
    source_use: Counter[str] = Counter()
    for batch in rows:
        if not isinstance(batch, FillBatch):
            raise FillError("batches must contain FillBatch values")
        for fill in batch.fills:
            if fill.fill_id in fill_ids:
                raise FillError("duplicate fill_id across batches")
            fill_ids.add(fill.fill_id)
            source_use[fill.source_id] += fill.quantity_e4
    if authoritative_source_quantity is None:
        return
    for source_id, quantity in authoritative_source_quantity.items():
        _plain_int(f"source[{source_id}]", quantity, minimum=0)
    for source_id, used in source_use.items():
        if source_id not in authoritative_source_quantity:
            raise FillError("fill references an unregistered authoritative source")
        if used > authoritative_source_quantity[source_id]:
            raise FillError("portfolio fills exceed authoritative source quantity")
