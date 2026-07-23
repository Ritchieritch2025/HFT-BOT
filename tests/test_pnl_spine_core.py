#!/usr/bin/env python3
"""Adversarial contract tests for the shared fixed-point PnL spine core."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_TOOLS = ROOT / "tools" / "research"
if str(RESEARCH_TOOLS) not in sys.path:
    sys.path.insert(0, str(RESEARCH_TOOLS))

from pnl_spine import (  # noqa: E402
    AmbiguousFeeRule,
    ClosureState,
    ContractError,
    FeePrecision,
    FeeRule,
    FeeSchedule,
    FeeTruthUnavailable,
    FillPurpose,
    FillRecord,
    IncompletePnL,
    LatencyPath,
    LatencyReceipt,
    LatencySample,
    LatencyTruthUnavailable,
    LedgerInvariantError,
    LiquidityRole,
    MeasuredLatencyProfile,
    Outcome,
    PathSpec,
    PnLLedger,
    RiskInvariantError,
    RiskLedger,
    RiskLimitExceeded,
    RiskLimits,
    RiskRequest,
    SettlementRecord,
    SettlementStatus,
    Side,
    apply_balance_rounding,
    canonical_sha256,
    classify_settlement,
    exact_trade_notional_e6,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def path_spec(path_id: str = "path-1") -> PathSpec:
    return PathSpec(
        path_id=path_id,
        experiment_id="A01-SPREAD-CAPTURE",
        baseline_id="NO_TRADE",
        definition_sha256=SHA_A,
        release_sha256=SHA_B,
        market_ticker="KXSPORTS-GAME-YES",
        event_ticker="KXSPORTS-GAME",
        factor_key="SPORTS:GAME",
        outcome=Outcome.YES,
    )


def fill(
    *,
    fill_id: str,
    order_id: str,
    side: Side,
    purpose: FillPurpose,
    role: LiquidityRole,
    price_e4: int,
    executed_at_ns: int,
    actual_fee_e6: int | None = None,
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        path_id="path-1",
        market_ticker="KXSPORTS-GAME-YES",
        outcome=Outcome.YES,
        side=side,
        purpose=purpose,
        liquidity_role=role,
        quantity_e4=10_000,
        price_e4=price_e4,
        executed_at_ns=executed_at_ns,
        source_sha256=SHA_C,
        actual_private_fee_e6=actual_fee_e6,
        actual_private_fee_receipt_sha256=(
            SHA_D if actual_fee_e6 is not None else None
        ),
    )


def direct_rule(
    *,
    rule_id: str,
    role: LiquidityRole,
    rate_e4: int,
    multiplier_e4: int = 10_000,
    effective_from_ns: int = 0,
    effective_until_ns: int | None = None,
    series_prefix: str | None = "KXSPORTS",
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        liquidity_role=role,
        rate_e4=rate_e4,
        multiplier_e4=multiplier_e4,
        precision=FeePrecision.DIRECT_CENTICENT,
        effective_from_ns=effective_from_ns,
        effective_until_ns=effective_until_ns,
        source_sha256=SHA_A,
        series_prefix=series_prefix,
    )


class FixedPointContractTests(unittest.TestCase):
    def test_float_and_inexact_money_fail_closed(self) -> None:
        with self.assertRaises(ContractError):
            fill(
                fill_id="bad",
                order_id="o",
                side=Side.BUY,
                purpose=FillPurpose.ENTRY,
                role=LiquidityRole.MAKER,
                price_e4=0.4,  # type: ignore[arg-type]
                executed_at_ns=1,
            )
        with self.assertRaises(ContractError):
            exact_trade_notional_e6(1, 1)
        self.assertEqual(9_903, exact_trade_notional_e6(3_301, 300))
        with self.assertRaises(ContractError):
            canonical_sha256({"forbidden_float": 0.1})

    def test_private_fee_requires_integrity_receipt(self) -> None:
        with self.assertRaises(ContractError):
            FillRecord(
                fill_id="f",
                order_id="o",
                path_id="path-1",
                market_ticker="KXSPORTS-GAME-YES",
                outcome=Outcome.YES,
                side=Side.BUY,
                purpose=FillPurpose.ENTRY,
                liquidity_role=LiquidityRole.MAKER,
                quantity_e4=10_000,
                price_e4=4_000,
                executed_at_ns=1,
                source_sha256=SHA_A,
                actual_private_fee_e6=0,
            )


class FeeTruthTests(unittest.TestCase):
    def test_effective_role_and_scope_precedence(self) -> None:
        schedule = FeeSchedule(
            (
                direct_rule(
                    rule_id="global-maker-zero",
                    role=LiquidityRole.MAKER,
                    rate_e4=175,
                    multiplier_e4=0,
                    series_prefix=None,
                ),
                direct_rule(
                    rule_id="sports-maker-old",
                    role=LiquidityRole.MAKER,
                    rate_e4=175,
                    effective_until_ns=100,
                ),
                direct_rule(
                    rule_id="sports-maker-new",
                    role=LiquidityRole.MAKER,
                    rate_e4=200,
                    effective_from_ns=100,
                ),
                direct_rule(
                    rule_id="global-taker",
                    role=LiquidityRole.TAKER,
                    rate_e4=700,
                    series_prefix=None,
                ),
            )
        )
        maker_old = fill(
            fill_id="m1",
            order_id="m1",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=99,
        )
        maker_new = replace(
            maker_old,
            fill_id="m2",
            order_id="m2",
            executed_at_ns=100,
        )
        taker = replace(
            maker_old,
            fill_id="t1",
            order_id="t1",
            liquidity_role=LiquidityRole.TAKER,
        )
        self.assertEqual("sports-maker-old", schedule.resolve_rule(maker_old).rule_id)
        self.assertEqual("sports-maker-new", schedule.resolve_rule(maker_new).rule_id)
        self.assertEqual("global-taker", schedule.resolve_rule(taker).rule_id)
        self.assertEqual(4_200, schedule.assess(maker_old).fee_e6)
        self.assertEqual(4_800, schedule.assess(maker_new).fee_e6)
        self.assertEqual(16_800, schedule.assess(taker).fee_e6)

    def test_actual_private_fee_wins_even_without_schedule(self) -> None:
        actual = fill(
            fill_id="actual",
            order_id="actual-order",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=10,
            actual_fee_e6=700,
        )
        assessment = FeeSchedule(()).assess(actual)
        self.assertTrue(assessment.is_actual_private_fee)
        self.assertEqual(700, assessment.fee_e6)
        self.assertEqual(SHA_D, assessment.source_sha256)

    def test_missing_and_ambiguous_fee_truth_fail_closed(self) -> None:
        unknown = fill(
            fill_id="unknown",
            order_id="unknown",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=10,
        )
        with self.assertRaises(FeeTruthUnavailable):
            FeeSchedule(()).assess(unknown)
        first = direct_rule(
            rule_id="one",
            role=LiquidityRole.MAKER,
            rate_e4=175,
        )
        second = replace(first, rule_id="two")
        with self.assertRaises(AmbiguousFeeRule):
            FeeSchedule((first, second))

    def test_official_centicent_and_non_direct_order_accumulator(self) -> None:
        rule = FeeRule(
            rule_id="sports-maker-nondirect",
            liquidity_role=LiquidityRole.MAKER,
            rate_e4=175,
            multiplier_e4=10_000,
            precision=FeePrecision.NON_DIRECT_CENT,
            effective_from_ns=0,
            effective_until_ns=None,
            source_sha256=SHA_A,
            series_prefix="KXSPORTS",
        )
        schedule = FeeSchedule((rule,))
        first_fill = fill(
            fill_id="nd1",
            order_id="same-order",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=1,
        )
        first = schedule.assess(first_fill)
        second = schedule.assess(
            replace(first_fill, fill_id="nd2", executed_at_ns=2),
            rounding_accumulator_before_e6=(
                first.rounding_accumulator_after_e6
            ),
        )
        self.assertEqual(
            (4_200, 5_800, 0, 10_000, 5_800),
            (
                first.trade_fee_e6,
                first.rounding_fee_e6,
                first.rebate_e6,
                first.fee_e6,
                first.rounding_accumulator_after_e6,
            ),
        )
        self.assertEqual(
            (4_200, 5_800, 10_000, 0, 1_600),
            (
                second.trade_fee_e6,
                second.rounding_fee_e6,
                second.rebate_e6,
                second.fee_e6,
                second.rounding_accumulator_after_e6,
            ),
        )

    def test_official_rounding_worked_examples_in_moneye6(self) -> None:
        # $0.055 × 1, with the documented $0.0085 trade fee.
        self.assertEqual(55_000, exact_trade_notional_e6(550, 10_000))
        subpenny_1 = apply_balance_rounding(
            revenue_e6=-55_000,
            trade_fee_e6=8_500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(-70_000, subpenny_1.rounded_balance_change_e6)
        self.assertEqual(6_500, subpenny_1.rounding_fee_e6)
        self.assertEqual(15_000, subpenny_1.net_fee_e6)
        subpenny_2 = apply_balance_rounding(
            revenue_e6=-55_000,
            trade_fee_e6=8_500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=subpenny_1.accumulator_after_e6,
        )
        self.assertEqual(10_000, subpenny_2.rebate_e6)
        self.assertEqual(5_000, subpenny_2.net_fee_e6)

        # $0.50 × 0.30, with the documented $0.0041 trade fee.
        self.assertEqual(150_000, exact_trade_notional_e6(5_000, 3_000))
        fractional_1 = apply_balance_rounding(
            revenue_e6=-150_000,
            trade_fee_e6=4_100,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(-160_000, fractional_1.rounded_balance_change_e6)
        self.assertEqual(5_900, fractional_1.rounding_fee_e6)
        self.assertEqual(10_000, fractional_1.net_fee_e6)
        fractional_2 = apply_balance_rounding(
            revenue_e6=-150_000,
            trade_fee_e6=4_100,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=fractional_1.accumulator_after_e6,
        )
        self.assertEqual(10_000, fractional_2.rebate_e6)
        self.assertEqual(0, fractional_2.net_fee_e6)

        # $0.3301 × 0.03 preserves all six decimal places.
        self.assertEqual(9_903, exact_trade_notional_e6(3_301, 300))
        combined_1 = apply_balance_rounding(
            revenue_e6=-9_903,
            trade_fee_e6=500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(-20_000, combined_1.rounded_balance_change_e6)
        self.assertEqual(9_597, combined_1.rounding_fee_e6)
        self.assertEqual(10_097, combined_1.net_fee_e6)
        combined_2 = apply_balance_rounding(
            revenue_e6=-9_903,
            trade_fee_e6=500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=combined_1.accumulator_after_e6,
        )
        self.assertEqual(10_000, combined_2.rebate_e6)
        self.assertEqual(97, combined_2.net_fee_e6)
        combined_3 = apply_balance_rounding(
            revenue_e6=-9_903,
            trade_fee_e6=500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=combined_2.accumulator_after_e6,
        )
        self.assertEqual(10_000, combined_3.rebate_e6)
        self.assertEqual(97, combined_3.net_fee_e6)

    def test_order_accumulator_carries_from_taker_to_maker(self) -> None:
        maker = FeeRule(
            rule_id="maker",
            liquidity_role=LiquidityRole.MAKER,
            rate_e4=175,
            multiplier_e4=10_000,
            precision=FeePrecision.NON_DIRECT_CENT,
            effective_from_ns=0,
            effective_until_ns=None,
            source_sha256=SHA_A,
            series_prefix="KXSPORTS",
        )
        taker = replace(
            maker,
            rule_id="taker",
            liquidity_role=LiquidityRole.TAKER,
            rate_e4=700,
        )
        ledger = PnLLedger(path_spec(), FeeSchedule((maker, taker)))
        first = ledger.record_fill(
            fill(
                fill_id="same-order-taker",
                order_id="hybrid-order",
                side=Side.BUY,
                purpose=FillPurpose.ENTRY,
                role=LiquidityRole.TAKER,
                price_e4=4_000,
                executed_at_ns=1,
            )
        )
        second = ledger.record_fill(
            fill(
                fill_id="same-order-maker",
                order_id="hybrid-order",
                side=Side.BUY,
                purpose=FillPurpose.ENTRY,
                role=LiquidityRole.MAKER,
                price_e4=4_000,
                executed_at_ns=2,
            )
        )
        self.assertEqual(3_200, first.rounding_accumulator_after_e6)
        self.assertEqual(9_000, second.rounding_accumulator_after_e6)
        self.assertEqual(0, second.rebate_e6)


class LatencyReceiptTests(unittest.TestCase):
    @staticmethod
    def sample(path: LatencyPath, offset: int) -> LatencySample:
        return LatencySample(
            sample_id=f"{path.value.lower()}-{offset}",
            order_id=f"order-{offset}",
            path=path,
            decision_ns=offset,
            sent_ns=offset + 10,
            acknowledged_ns=offset + 20,
            effective_ns=offset + 30,
        )

    def test_complete_integrity_bound_receipt_is_measured(self) -> None:
        receipt = LatencyReceipt.create(
            receipt_id="latency-1",
            clock_id="w09-monotonic-boot-1",
            measured_on="W09",
            created_at_ns=1_000,
            source_sha256=SHA_A,
            samples=(
                self.sample(LatencyPath.EXIT, 200),
                self.sample(LatencyPath.PLACE, 0),
                self.sample(LatencyPath.CANCEL, 100),
            ),
        )
        profile = MeasuredLatencyProfile.from_receipt(receipt)
        self.assertEqual(3, len(profile.summaries))
        self.assertEqual(receipt.payload_sha256, profile.receipt_sha256)
        self.assertTrue(all(item.p99_ns == 30 for item in profile.summaries))

    def test_missing_exit_cannot_be_marked_measured(self) -> None:
        receipt = LatencyReceipt.create(
            receipt_id="latency-partial",
            clock_id="clock",
            measured_on="W09",
            created_at_ns=1_000,
            source_sha256=SHA_A,
            samples=(
                self.sample(LatencyPath.PLACE, 0),
                self.sample(LatencyPath.CANCEL, 100),
            ),
        )
        with self.assertRaises(LatencyTruthUnavailable):
            MeasuredLatencyProfile.from_receipt(receipt)

    def test_tamper_and_noncausal_samples_are_rejected(self) -> None:
        receipt = LatencyReceipt.create(
            receipt_id="latency-1",
            clock_id="clock",
            measured_on="W09",
            created_at_ns=1_000,
            source_sha256=SHA_A,
            samples=(self.sample(LatencyPath.PLACE, 0),),
        )
        with self.assertRaises(LatencyTruthUnavailable):
            replace(receipt, payload_sha256=SHA_B)
        with self.assertRaises(LatencyTruthUnavailable):
            LatencySample(
                sample_id="bad",
                order_id="bad",
                path=LatencyPath.EXIT,
                decision_ns=10,
                sent_ns=9,
                acknowledged_ns=11,
                effective_ns=12,
            )


class LedgerTests(unittest.TestCase):
    @staticmethod
    def build_exit_ledger() -> tuple[PnLLedger, object]:
        schedule = FeeSchedule(
            (
                direct_rule(
                    rule_id="taker",
                    role=LiquidityRole.TAKER,
                    rate_e4=700,
                ),
            )
        )
        ledger = PnLLedger(path_spec(), schedule)
        entry = fill(
            fill_id="entry",
            order_id="entry-order",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=10,
            actual_fee_e6=5_000,
        )
        exit_fill = fill(
            fill_id="exit",
            order_id="exit-order",
            side=Side.SELL,
            purpose=FillPurpose.EXIT,
            role=LiquidityRole.TAKER,
            price_e4=5_500,
            executed_at_ns=20,
        )
        ledger.record_fill(entry)
        ledger.record_fill(exit_fill)
        ledger.record_variable_cost(
            cost_id="egress",
            amount_e6=2_500,
            occurred_at_ns=30,
            source_sha256=SHA_B,
        )
        return ledger, ledger.finalize()

    def test_complete_exit_cash_identity_and_deterministic_sha(self) -> None:
        first_ledger, first = self.build_exit_ledger()
        second_ledger, second = self.build_exit_ledger()
        self.assertEqual(ClosureState.CLOSED_BY_EXIT, first.closure_state)
        self.assertEqual(150_000, first.gross_pnl_e6)
        self.assertEqual(22_400, first.fee_cost_e6)
        self.assertEqual(2_500, first.variable_cost_e6)
        self.assertEqual(125_100, first.net_pnl_e6)
        self.assertEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(
            first_ledger.deterministic_sha256,
            second_ledger.deterministic_sha256,
        )
        self.assertTrue(
            first_ledger.fee_assessments[0].is_actual_private_fee
        )
        with self.assertRaises(LedgerInvariantError):
            first_ledger.record_variable_cost(
                cost_id="late",
                amount_e6=100,
                occurred_at_ns=31,
                source_sha256=SHA_A,
            )

    def test_missing_fee_is_atomic_and_fails_closed(self) -> None:
        ledger = PnLLedger(path_spec(), FeeSchedule(()))
        unknown_fee_fill = fill(
            fill_id="missing-fee",
            order_id="missing-fee",
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            role=LiquidityRole.MAKER,
            price_e4=4_000,
            executed_at_ns=10,
        )
        with self.assertRaises(FeeTruthUnavailable):
            ledger.record_fill(unknown_fee_fill)
        self.assertEqual((), ledger.cashflows)
        self.assertEqual(0, ledger.position_e4)

    def test_unknown_terminal_is_censored_until_finalized_revision(self) -> None:
        schedule = FeeSchedule(
            (
                direct_rule(
                    rule_id="maker",
                    role=LiquidityRole.MAKER,
                    rate_e4=175,
                ),
            )
        )
        ledger = PnLLedger(path_spec(), schedule)
        ledger.record_fill(
            fill(
                fill_id="entry",
                order_id="entry",
                side=Side.BUY,
                purpose=FillPurpose.ENTRY,
                role=LiquidityRole.MAKER,
                price_e4=6_000,
                executed_at_ns=10,
            )
        )
        unknown = SettlementRecord(
            settlement_id="settlement-unknown",
            market_ticker="KXSPORTS-GAME-YES",
            status=SettlementStatus.UNKNOWN,
            finalized=False,
            settlement_value_e4=None,
            observed_at_ns=20,
            revision=0,
            source_sha256=SHA_B,
        )
        self.assertEqual(
            ClosureState.CENSORED,
            ledger.observe_settlement(unknown),
        )
        with self.assertRaises(IncompletePnL):
            ledger.finalize()
        final = SettlementRecord(
            settlement_id="settlement-final",
            market_ticker="KXSPORTS-GAME-YES",
            status=SettlementStatus.FINALIZED,
            finalized=True,
            settlement_value_e4=10_000,
            observed_at_ns=30,
            revision=1,
            source_sha256=SHA_C,
        )
        self.assertEqual(
            ClosureState.CLOSED_BY_SETTLEMENT,
            ledger.observe_settlement(final),
        )
        result = ledger.finalize()
        self.assertEqual(400_000, result.gross_pnl_e6)
        self.assertEqual(4_200, result.fee_cost_e6)
        self.assertEqual(395_800, result.net_pnl_e6)

    def test_nonfinal_status_cannot_self_declare_finalized_payout(self) -> None:
        for status in (
            SettlementStatus.PROVISIONAL,
            SettlementStatus.POSTPONED,
            SettlementStatus.VOID,
            SettlementStatus.CANCELED,
            SettlementStatus.RETIRED,
            SettlementStatus.UNKNOWN,
        ):
            with self.subTest(status=status.value):
                with self.assertRaisesRegex(
                    ValueError,
                    "exactly for FINALIZED status",
                ):
                    SettlementRecord(
                        settlement_id=f"caller-finalized-{status.value}",
                        market_ticker="KXSPORTS-GAME-YES",
                        status=status,
                        finalized=True,
                        settlement_value_e4=10_000,
                        observed_at_ns=30,
                        revision=1,
                        source_sha256=SHA_C,
                    )

    def test_explicit_no_fill_path_retains_zero(self) -> None:
        ledger = PnLLedger(path_spec(), FeeSchedule(()))
        ledger.close_no_position(
            closure_id="window-complete",
            occurred_at_ns=100,
            source_sha256=SHA_A,
        )
        result = ledger.finalize()
        self.assertEqual(ClosureState.CLOSED_NO_POSITION, result.closure_state)
        self.assertEqual(0, result.net_pnl_e6)
        self.assertEqual(1, result.cashflow_count)


class RiskLedgerTests(unittest.TestCase):
    @staticmethod
    def limits(max_total_e6: int = 1_000_000) -> RiskLimits:
        return RiskLimits(
            max_market_e6=1_000_000,
            max_event_e6=1_000_000,
            max_factor_e6=1_000_000,
            max_total_e6=max_total_e6,
            max_daily_loss_e6=500_000,
        )

    @staticmethod
    def request(reservation_id: str = "r1") -> RiskRequest:
        return RiskRequest(
            reservation_id=reservation_id,
            path_id="path-1",
            market_ticker="KXSPORTS-GAME-YES",
            event_ticker="KXSPORTS-GAME",
            factor_key="SPORTS:GAME",
            quantity_e4=20_000,
            worst_case_loss_e6=100_000,
            requested_at_ns=1,
            source_sha256=SHA_A,
        )

    def test_risk_stays_through_fill_and_releases_only_on_exit(self) -> None:
        ledger = RiskLedger(self.limits())
        ledger.reserve(self.request(), event_id="reserve")
        ledger.record_fill(
            "r1",
            quantity_e4=20_000,
            event_id="fill",
            occurred_at_ns=2,
            source_sha256=SHA_A,
        )
        self.assertEqual(100_000, ledger.total_exposure_e6)
        ledger.record_exit(
            "r1",
            quantity_e4=10_000,
            event_id="exit-1",
            occurred_at_ns=3,
            source_sha256=SHA_B,
        )
        self.assertEqual(50_000, ledger.total_exposure_e6)
        ledger.record_exit(
            "r1",
            quantity_e4=10_000,
            event_id="exit-2",
            occurred_at_ns=4,
            source_sha256=SHA_B,
        )
        self.assertEqual(0, ledger.total_exposure_e6)

    def test_censored_settlement_cannot_release_position_risk(self) -> None:
        ledger = RiskLedger(self.limits())
        ledger.reserve(self.request(), event_id="reserve")
        ledger.record_fill(
            "r1",
            quantity_e4=10_000,
            event_id="fill",
            occurred_at_ns=2,
            source_sha256=SHA_A,
        )
        ledger.record_cancel(
            "r1",
            quantity_e4=10_000,
            event_id="cancel",
            occurred_at_ns=3,
            source_sha256=SHA_A,
        )
        self.assertEqual(50_000, ledger.total_exposure_e6)
        unknown = SettlementRecord(
            settlement_id="unknown",
            market_ticker="KXSPORTS-GAME-YES",
            status=SettlementStatus.UNKNOWN,
            finalized=False,
            settlement_value_e4=None,
            observed_at_ns=4,
            revision=0,
            source_sha256=SHA_B,
        )
        with self.assertRaises(RiskInvariantError):
            ledger.record_settlement(
                "r1",
                decision=classify_settlement(unknown),
                event_id="settle-unknown",
            )
        self.assertEqual(50_000, ledger.total_exposure_e6)
        final = replace(
            unknown,
            settlement_id="final",
            status=SettlementStatus.FINALIZED,
            finalized=True,
            settlement_value_e4=10_000,
            observed_at_ns=5,
            revision=1,
        )
        ledger.record_settlement(
            "r1",
            decision=classify_settlement(final),
            event_id="settle-final",
        )
        self.assertEqual(0, ledger.total_exposure_e6)

    def test_prospective_limit_blocks_without_mutation(self) -> None:
        ledger = RiskLedger(self.limits(max_total_e6=99_900))
        before = ledger.deterministic_sha256
        with self.assertRaises(RiskLimitExceeded):
            ledger.reserve(self.request(), event_id="reserve")
        self.assertEqual(0, ledger.total_exposure_e6)
        self.assertEqual(before, ledger.deterministic_sha256)

    def test_daily_loss_gate_resets_by_utc_date(self) -> None:
        day_ns = 86_400 * 1_000_000_000
        ledger = RiskLedger(self.limits())
        ledger.record_realized_pnl(
            path_id="closed-loss",
            pnl_e6=-500_000,
            event_id="loss-day-one",
            occurred_at_ns=day_ns + 1,
            source_sha256=SHA_A,
        )
        same_day = replace(
            self.request("same-day"),
            requested_at_ns=day_ns + 2,
        )
        with self.assertRaisesRegex(
            RiskLimitExceeded,
            "daily realized loss gate is closed",
        ):
            ledger.reserve(same_day, event_id="reserve-same-day")

        next_day = replace(
            self.request("next-day"),
            requested_at_ns=2 * day_ns,
        )
        ledger.reserve(next_day, event_id="reserve-next-day")
        self.assertEqual(100_000, ledger.total_exposure_e6)
        self.assertEqual(
            {"1970-01-02": -500_000},
            ledger.realized_pnl_by_utc_date_e6,
        )

    def test_open_exposure_survives_utc_day_rollover(self) -> None:
        day_ns = 86_400 * 1_000_000_000
        ledger = RiskLedger(self.limits(max_total_e6=150_000))
        first = replace(
            self.request("cross-midnight"),
            requested_at_ns=day_ns - 1,
        )
        ledger.reserve(first, event_id="reserve-before-midnight")
        next_day = replace(
            self.request("new-day"),
            requested_at_ns=day_ns,
        )
        with self.assertRaisesRegex(
            RiskLimitExceeded,
            "risk limits exceeded: total",
        ):
            ledger.reserve(next_day, event_id="reserve-after-midnight")
        self.assertEqual(100_000, ledger.total_exposure_e6)

    def test_daily_loss_breach_latches_after_later_profit_recovery(self) -> None:
        day_ns = 86_400 * 1_000_000_000
        ledger = RiskLedger(self.limits())
        ledger.reserve(
            replace(
                self.request("loss-path"),
                requested_at_ns=day_ns + 1,
            ),
            event_id="reserve-loss-path",
        )
        ledger.reserve(
            replace(
                self.request("profit-path"),
                requested_at_ns=day_ns + 2,
            ),
            event_id="reserve-profit-path",
        )
        ledger.record_realized_pnl(
            path_id="loss-path",
            pnl_e6=-600_000,
            event_id="realize-loss",
            occurred_at_ns=day_ns + 3,
            source_sha256=SHA_A,
        )
        ledger.record_realized_pnl(
            path_id="profit-path",
            pnl_e6=200_000,
            event_id="realize-profit",
            occurred_at_ns=day_ns + 4,
            source_sha256=SHA_B,
        )
        self.assertEqual(
            {"1970-01-02": -400_000},
            ledger.realized_pnl_by_utc_date_e6,
        )
        self.assertEqual(
            frozenset({"1970-01-02"}),
            ledger.daily_loss_breached_utc_dates,
        )
        with self.assertRaisesRegex(
            RiskLimitExceeded,
            "daily realized loss gate is closed",
        ):
            ledger.reserve(
                replace(
                    self.request("later-path"),
                    requested_at_ns=day_ns + 5,
                ),
                event_id="reserve-after-recovery",
            )


if __name__ == "__main__":
    unittest.main()
