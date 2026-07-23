"""Effective-dated, fixed-point fee truth for the research PnL spine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .contracts import (
    FillRecord,
    LiquidityRole,
    MICRODOLLARS_PER_CENT,
    MICRODOLLARS_PER_CENTICENT,
    PRICE_SCALE_E4,
    Side,
    canonical_sha256,
    ceil_div,
    exact_trade_notional_e6,
    require_int,
    require_nonempty,
    require_sha256,
)


# multiplier_e4 × rate_e4 × quantity_e4 × p_e4 × (1-p)_e4
# produces a fee numerator.  Divide by this constant for unrounded MoneyE6.
QUADRATIC_FEE_E6_DENOMINATOR = 100_000_000_000_000


class FeeTruthUnavailable(RuntimeError):
    """Exact fee truth cannot be established; net PnL must not be emitted."""


class AmbiguousFeeRule(FeeTruthUnavailable):
    """More than one fee rule owns the same effective scope."""


class FeePrecision(str, Enum):
    """Venue cash precision used for balance-change rounding."""

    DIRECT_CENTICENT = "DIRECT_CENTICENT"
    NON_DIRECT_CENT = "NON_DIRECT_CENT"

    @property
    def target_e6(self) -> int:
        if self is FeePrecision.DIRECT_CENTICENT:
            return MICRODOLLARS_PER_CENTICENT
        return MICRODOLLARS_PER_CENT


@dataclass(frozen=True)
class FeeRule:
    """One effective-dated quadratic fee rate for one liquidity role.

    Scope precedence is exact market, then longest series prefix, then global.
    Rules at the same scope and role may not overlap in time.
    """

    rule_id: str
    liquidity_role: LiquidityRole
    rate_e4: int
    multiplier_e4: int
    precision: FeePrecision
    effective_from_ns: int
    effective_until_ns: int | None
    source_sha256: str
    market_ticker: str | None = None
    series_prefix: str | None = None

    def __post_init__(self) -> None:
        require_nonempty("rule_id", self.rule_id)
        if not isinstance(self.liquidity_role, LiquidityRole):
            raise ValueError("liquidity_role must be a LiquidityRole")
        require_int("rate_e4", self.rate_e4, minimum=0)
        require_int("multiplier_e4", self.multiplier_e4, minimum=0)
        if not isinstance(self.precision, FeePrecision):
            raise ValueError("precision must be a FeePrecision")
        require_int("effective_from_ns", self.effective_from_ns, minimum=0)
        if self.effective_until_ns is not None:
            require_int(
                "effective_until_ns",
                self.effective_until_ns,
                minimum=self.effective_from_ns + 1,
            )
        require_sha256("source_sha256", self.source_sha256)
        if self.market_ticker is not None:
            require_nonempty("market_ticker", self.market_ticker)
        if self.series_prefix is not None:
            require_nonempty("series_prefix", self.series_prefix)
        if self.market_ticker is not None and self.series_prefix is not None:
            raise ValueError(
                "fee rule must select exact market or series prefix, not both"
            )

    @property
    def selector(self) -> tuple[str, str]:
        if self.market_ticker is not None:
            return ("MARKET", self.market_ticker)
        if self.series_prefix is not None:
            return ("SERIES", self.series_prefix)
        return ("GLOBAL", "")

    @property
    def specificity(self) -> tuple[int, int]:
        if self.market_ticker is not None:
            return (2, len(self.market_ticker))
        if self.series_prefix is not None:
            return (1, len(self.series_prefix))
        return (0, 0)

    def matches(self, fill: FillRecord) -> bool:
        if fill.liquidity_role is not self.liquidity_role:
            return False
        if fill.executed_at_ns < self.effective_from_ns:
            return False
        if (
            self.effective_until_ns is not None
            and fill.executed_at_ns >= self.effective_until_ns
        ):
            return False
        if self.market_ticker is not None:
            return fill.market_ticker == self.market_ticker
        if self.series_prefix is not None:
            return fill.market_ticker.startswith(self.series_prefix)
        return True


@dataclass(frozen=True)
class FeeAssessment:
    fee_e6: int
    trade_fee_e6: int
    rounding_fee_e6: int
    rebate_e6: int
    rounding_accumulator_after_e6: int
    rounded_balance_change_e6: int | None
    source: str
    source_sha256: str
    rule_id: str | None
    raw_fee_numerator: int

    def __post_init__(self) -> None:
        require_int("fee_e6", self.fee_e6, minimum=0)
        require_int("trade_fee_e6", self.trade_fee_e6, minimum=0)
        require_int("rounding_fee_e6", self.rounding_fee_e6, minimum=0)
        require_int("rebate_e6", self.rebate_e6, minimum=0)
        require_int(
            "rounding_accumulator_after_e6",
            self.rounding_accumulator_after_e6,
            minimum=0,
        )
        if self.rounded_balance_change_e6 is not None:
            require_int(
                "rounded_balance_change_e6",
                self.rounded_balance_change_e6,
            )
        require_nonempty("source", self.source)
        require_sha256("source_sha256", self.source_sha256)
        require_int(
            "raw_fee_numerator",
            self.raw_fee_numerator,
            minimum=0,
        )
        if self.fee_e6 != (
            self.trade_fee_e6 + self.rounding_fee_e6 - self.rebate_e6
        ):
            raise ValueError("fee components do not equal net fee_e6")

    @property
    def is_actual_private_fee(self) -> bool:
        return self.source == "ACTUAL_PRIVATE_FILL"


@dataclass(frozen=True)
class BalanceRounding:
    revenue_e6: int
    trade_fee_e6: int
    exact_balance_change_e6: int
    rounded_balance_change_e6: int
    rounding_fee_e6: int
    rebate_e6: int
    net_fee_e6: int
    accumulator_after_e6: int


def apply_balance_rounding(
    *,
    revenue_e6: int,
    trade_fee_e6: int,
    precision: FeePrecision,
    accumulator_before_e6: int,
) -> BalanceRounding:
    """Apply the venue's floor, accumulator, and whole-cent rebate mechanics."""

    require_int("revenue_e6", revenue_e6)
    require_int("trade_fee_e6", trade_fee_e6, minimum=0)
    require_int(
        "accumulator_before_e6",
        accumulator_before_e6,
        minimum=0,
    )
    if not isinstance(precision, FeePrecision):
        raise ValueError("precision must be a FeePrecision")
    exact_balance_change_e6 = revenue_e6 - trade_fee_e6
    target_e6 = precision.target_e6
    rounded_balance_change_e6 = (
        exact_balance_change_e6 // target_e6
    ) * target_e6
    rounding_fee_e6 = (
        exact_balance_change_e6 - rounded_balance_change_e6
    )
    accumulated_e6 = accumulator_before_e6 + rounding_fee_e6
    available_rebate_e6 = (
        accumulated_e6 // MICRODOLLARS_PER_CENT
    ) * MICRODOLLARS_PER_CENT
    # The venue specifies a non-negative net fee.  A whole-cent rebate that
    # would make this fill negative is retained in the accumulator.
    rebate_capacity_e6 = (
        (trade_fee_e6 + rounding_fee_e6)
        // MICRODOLLARS_PER_CENT
    ) * MICRODOLLARS_PER_CENT
    rebate_e6 = min(available_rebate_e6, rebate_capacity_e6)
    accumulator_after_e6 = accumulated_e6 - rebate_e6
    net_fee_e6 = trade_fee_e6 + rounding_fee_e6 - rebate_e6
    return BalanceRounding(
        revenue_e6=revenue_e6,
        trade_fee_e6=trade_fee_e6,
        exact_balance_change_e6=exact_balance_change_e6,
        rounded_balance_change_e6=rounded_balance_change_e6,
        rounding_fee_e6=rounding_fee_e6,
        rebate_e6=rebate_e6,
        net_fee_e6=net_fee_e6,
        accumulator_after_e6=accumulator_after_e6,
    )


def _intervals_overlap(left: FeeRule, right: FeeRule) -> bool:
    left_end = left.effective_until_ns
    right_end = right.effective_until_ns
    return (
        (right_end is None or left.effective_from_ns < right_end)
        and (left_end is None or right.effective_from_ns < left_end)
    )


class FeeSchedule:
    """Immutable rule set with fail-closed fee resolution."""

    def __init__(self, rules: Iterable[FeeRule]) -> None:
        self._rules = tuple(
            sorted(
                rules,
                key=lambda rule: (
                    rule.selector,
                    rule.liquidity_role.value,
                    rule.effective_from_ns,
                    rule.rule_id,
                ),
            )
        )
        seen_ids: set[str] = set()
        for index, rule in enumerate(self._rules):
            if rule.rule_id in seen_ids:
                raise AmbiguousFeeRule(f"duplicate fee rule id {rule.rule_id}")
            seen_ids.add(rule.rule_id)
            for other in self._rules[index + 1 :]:
                if (
                    rule.selector == other.selector
                    and rule.liquidity_role is other.liquidity_role
                    and _intervals_overlap(rule, other)
                ):
                    raise AmbiguousFeeRule(
                        f"overlapping fee rules {rule.rule_id} and "
                        f"{other.rule_id}"
                    )

    @property
    def rules(self) -> tuple[FeeRule, ...]:
        return self._rules

    @property
    def deterministic_sha256(self) -> str:
        return canonical_sha256(self._rules)

    def resolve_rule(self, fill: FillRecord) -> FeeRule:
        candidates = [rule for rule in self._rules if rule.matches(fill)]
        if not candidates:
            raise FeeTruthUnavailable(
                f"no {fill.liquidity_role.value} fee rule for "
                f"{fill.market_ticker} at {fill.executed_at_ns}"
            )
        best_specificity = max(rule.specificity for rule in candidates)
        best = [
            rule for rule in candidates
            if rule.specificity == best_specificity
        ]
        if len(best) != 1:
            raise AmbiguousFeeRule(
                f"ambiguous fee truth for fill {fill.fill_id}: "
                + ",".join(rule.rule_id for rule in best)
            )
        return best[0]

    def assess(
        self,
        fill: FillRecord,
        *,
        rounding_accumulator_before_e6: int = 0,
    ) -> FeeAssessment:
        """Assess one fill.

        A private fill fee, including a valid zero, always wins over schedule
        estimation.  Otherwise each trade fee is independently rounded up to
        one centicent.  Non-direct cash changes are then rounded toward
        negative infinity to a whole cent; that rounding fee accumulates per
        order and each accumulated whole cent is returned as a rebate.
        """

        require_int(
            "rounding_accumulator_before_e6",
            rounding_accumulator_before_e6,
            minimum=0,
        )
        if fill.actual_private_fee_e6 is not None:
            return FeeAssessment(
                fee_e6=fill.actual_private_fee_e6,
                trade_fee_e6=fill.actual_private_fee_e6,
                rounding_fee_e6=0,
                rebate_e6=0,
                rounding_accumulator_after_e6=(
                    rounding_accumulator_before_e6
                ),
                rounded_balance_change_e6=None,
                source="ACTUAL_PRIVATE_FILL",
                source_sha256=fill.actual_private_fee_receipt_sha256,
                rule_id=None,
                raw_fee_numerator=0,
            )

        rule = self.resolve_rule(fill)
        p_e4 = fill.price_e4
        raw_fee_numerator = (
            rule.multiplier_e4
            * rule.rate_e4
            * fill.quantity_e4
            * p_e4
            * (PRICE_SCALE_E4 - p_e4)
        )
        trade_fee_e6 = (
            ceil_div(
                raw_fee_numerator,
                (
                    QUADRATIC_FEE_E6_DENOMINATOR
                    * MICRODOLLARS_PER_CENTICENT
                ),
            )
            * MICRODOLLARS_PER_CENTICENT
        )
        principal_e6 = exact_trade_notional_e6(
            fill.price_e4,
            fill.quantity_e4,
        )
        if fill.side is Side.BUY:
            principal_e6 = -principal_e6
        rounding = apply_balance_rounding(
            revenue_e6=principal_e6,
            trade_fee_e6=trade_fee_e6,
            precision=rule.precision,
            accumulator_before_e6=rounding_accumulator_before_e6,
        )
        return FeeAssessment(
            fee_e6=rounding.net_fee_e6,
            trade_fee_e6=trade_fee_e6,
            rounding_fee_e6=rounding.rounding_fee_e6,
            rebate_e6=rounding.rebate_e6,
            rounding_accumulator_after_e6=(
                rounding.accumulator_after_e6
            ),
            rounded_balance_change_e6=(
                rounding.rounded_balance_change_e6
            ),
            source="EFFECTIVE_DATED_SCHEDULE",
            source_sha256=rule.source_sha256,
            rule_id=rule.rule_id,
            raw_fee_numerator=raw_fee_numerator,
        )
