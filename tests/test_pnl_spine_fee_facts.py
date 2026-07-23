#!/usr/bin/env python3
"""Adversarial tests for offline, integrity-bound PnL fee facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_TOOLS = ROOT / "tools" / "research"
if str(RESEARCH_TOOLS) not in sys.path:
    sys.path.insert(0, str(RESEARCH_TOOLS))

from pnl_spine.contracts import (  # noqa: E402
    FillPurpose,
    FillRecord,
    LiquidityRole,
    Outcome,
    Side,
    canonical_json_bytes,
    canonical_sha256,
    exact_trade_notional_e6,
)
from pnl_spine.fee_facts import (  # noqa: E402
    FeeFactsIntegrityError,
    FeeFactsUnavailable,
    load_fee_facts,
)
from pnl_spine.fees import (  # noqa: E402
    FeePrecision,
    apply_balance_rounding,
)


# Exact read-only receipts captured on 2026-07-23.
SERIES_HISTORY_SHA = (
    "2a6a459c2e2b0f5690ffc1ed4f2dca9144f525cfa0867c704dd3165e1567c460"
)
EVENT_HISTORY_SHA = (
    "4d755a11d76cd85bec7a4758a2d226815fd2a935a5563dbafae35ee9d373da45"
)
PRIVATE_SOURCE_RECEIPT_SHA = (
    "fc6fbbc94e80abd86ffc36a351883c8901db7a6e682b7b57c64a80622d17994d"
)
PRIVATE_AGGREGATE_SHA = (
    "4a9bae89e6600d9a9217e7cd9103c72dd4f1598f81328e93c30d5adabe829a72"
)
SHA_A = "a" * 64
SHA_B = "b" * 64


def base_payload(
    *,
    precision: str = "NON_DIRECT_CENT",
) -> dict[str, object]:
    aggregate = {
        "actual_fee_field": "fee_cost",
        "fill_count": 372,
        "maker_fill_count": 15,
        "source_private_fill_receipt_sha256": (
            PRIVATE_SOURCE_RECEIPT_SHA
        ),
        "taker_fill_count": 357,
        "total_actual_fee_cost_e6": 953_982_830,
    }
    assert canonical_sha256(aggregate) == PRIVATE_AGGREGATE_SHA
    return {
        "account": {
            "balance_precision": precision,
            "source_sha256": SHA_A,
        },
        "event_assertions": [
            {
                "assertion_id": "base-event-no-waiver",
                "effective_from_ns": 0,
                "effective_until_ns": 500,
                "event_ticker": "KXSPORTS-GAME",
                "fee_type_override": None,
                "multiplier_e4_override": None,
                "source_sha256": SHA_B,
                "waiver_state": "NO_WAIVER",
            },
            {
                "assertion_id": "event-override",
                "effective_from_ns": 0,
                "effective_until_ns": 500,
                "event_ticker": "KXSPORTS-OVERRIDE",
                "fee_type_override": "quadratic_with_maker_fees",
                "multiplier_e4_override": 20_000,
                "source_sha256": SHA_B,
                "waiver_state": "NO_WAIVER",
            },
            {
                "assertion_id": "waiver-active",
                "effective_from_ns": 0,
                "effective_until_ns": 100,
                "event_ticker": "KXSPORTS-WAIVED",
                "fee_type_override": None,
                "multiplier_e4_override": None,
                "source_sha256": SHA_B,
                "waiver_state": "WAIVED",
            },
            {
                "assertion_id": "waiver-expired",
                "effective_from_ns": 100,
                "effective_until_ns": 500,
                "event_ticker": "KXSPORTS-WAIVED",
                "fee_type_override": None,
                "multiplier_e4_override": None,
                "source_sha256": SHA_B,
                "waiver_state": "NO_WAIVER",
            },
            {
                "assertion_id": "default-event-no-waiver",
                "effective_from_ns": 0,
                "effective_until_ns": 500,
                "event_ticker": "KXDEFAULT-GAME",
                "fee_type_override": None,
                "multiplier_e4_override": None,
                "source_sha256": SHA_B,
                "waiver_state": "NO_WAIVER",
            },
        ],
        "formula": {
            "default_maker_multiplier_e4": 0,
            "default_taker_multiplier_e4": 10_000,
            "effective_from_ns": 0,
            "effective_until_ns": None,
            "maker_rate_e4": 175,
            "taker_rate_e4": 700,
        },
        "private_actual_fee_aggregate": aggregate,
        "schema_version": "pnl_fee_facts_v1",
        "series_changes": [
            {
                "change_id": "sports-maker-on",
                "effective_from_ns": 50,
                "fee_type": "quadratic_with_maker_fees",
                "multiplier_e4": 10_000,
                "series_ticker": "KXSPORTS",
            },
            {
                "change_id": "sports-maker-off",
                "effective_from_ns": 300,
                "fee_type": "quadratic",
                "multiplier_e4": 10_000,
                "series_ticker": "KXSPORTS",
            },
        ],
        "source_bindings": {
            "event_history_snapshot_sha256": EVENT_HISTORY_SHA,
            "fee_schedule_pdf_sha256": SHA_A,
            "private_actual_fee_aggregate_receipt_sha256": (
                PRIVATE_AGGREGATE_SHA
            ),
            "series_history_captured_at_ns": 500,
            "series_history_includes_historical": True,
            "series_history_snapshot_sha256": SERIES_HISTORY_SHA,
        },
    }


def fill(
    *,
    market_ticker: str,
    role: LiquidityRole,
    executed_at_ns: int,
    price_e4: int = 4_000,
    quantity_e4: int = 10_000,
    actual_fee_e6: int | None = None,
    actual_receipt_sha256: str | None = None,
) -> FillRecord:
    return FillRecord(
        fill_id=f"fill-{market_ticker}-{role.value}-{executed_at_ns}",
        order_id=f"order-{market_ticker}-{role.value}",
        path_id="path",
        market_ticker=market_ticker,
        outcome=Outcome.YES,
        side=Side.BUY,
        purpose=FillPurpose.ENTRY,
        liquidity_role=role,
        quantity_e4=quantity_e4,
        price_e4=price_e4,
        executed_at_ns=executed_at_ns,
        source_sha256=SHA_A,
        actual_private_fee_e6=actual_fee_e6,
        actual_private_fee_receipt_sha256=actual_receipt_sha256,
    )


class FactsFile:
    def __init__(self, payload: dict[str, object]) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "fee_facts.json"
        self.bytes = canonical_json_bytes(payload)
        self.path.write_bytes(self.bytes)
        self.sha256 = hashlib.sha256(self.bytes).hexdigest()

    def close(self) -> None:
        self._temp.cleanup()


class FeeFactsLoadingTests(unittest.TestCase):
    def test_loads_canonical_offline_facts_and_binds_real_receipt_shas(
        self,
    ) -> None:
        fixture = FactsFile(base_payload())
        self.addCleanup(fixture.close)
        facts = load_fee_facts(
            fixture.path,
            expected_facts_sha256=fixture.sha256,
            expected_series_history_snapshot_sha256=SERIES_HISTORY_SHA,
            expected_private_actual_fee_aggregate_receipt_sha256=(
                PRIVATE_AGGREGATE_SHA
            ),
        )
        self.assertEqual(fixture.sha256, facts.facts_sha256)
        self.assertEqual(
            SERIES_HISTORY_SHA,
            facts.sources.series_history_snapshot_sha256,
        )
        self.assertEqual(
            PRIVATE_AGGREGATE_SHA,
            facts.private_fee_aggregate.receipt_sha256,
        )
        self.assertEqual(372, facts.private_fee_aggregate.fill_count)
        self.assertEqual(
            953_982_830,
            facts.private_fee_aggregate.total_actual_fee_cost_e6,
        )

    def test_noncanonical_duplicate_float_and_sha_drift_fail_closed(
        self,
    ) -> None:
        fixture = FactsFile(base_payload())
        self.addCleanup(fixture.close)
        fixture.path.write_bytes(
            json.dumps(base_payload(), indent=2).encode("utf-8")
        )
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(fixture.path)

        fixture.path.write_bytes(
            b'{"x":1,"x":2}'
        )
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(fixture.path)

        payload = base_payload()
        payload["formula"]["taker_rate_e4"] = 700.0
        fixture.path.write_bytes(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(fixture.path)

        fixture.path.write_bytes(canonical_json_bytes(base_payload()))
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(
                fixture.path,
                expected_series_history_snapshot_sha256="c" * 64,
            )

    def test_unknown_account_or_event_waiver_truth_fails_closed(self) -> None:
        unknown_account = FactsFile(base_payload(precision="UNKNOWN"))
        self.addCleanup(unknown_account.close)
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(unknown_account.path)

        payload = base_payload()
        payload["event_assertions"][0]["waiver_state"] = "UNKNOWN"
        unknown_waiver = FactsFile(payload)
        self.addCleanup(unknown_waiver.close)
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(unknown_waiver.path)

    def test_private_aggregate_rejects_rows_or_bad_receipt_binding(
        self,
    ) -> None:
        payload = base_payload()
        payload["private_actual_fee_aggregate"]["fills"] = [
            {"sensitive": "must-not-be-here"}
        ]
        rows = FactsFile(payload)
        self.addCleanup(rows.close)
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(rows.path)

        payload = base_payload()
        payload["source_bindings"][
            "private_actual_fee_aggregate_receipt_sha256"
        ] = "d" * 64
        mismatch = FactsFile(payload)
        self.addCleanup(mismatch.close)
        with self.assertRaises(FeeFactsIntegrityError):
            load_fee_facts(mismatch.path)


class EffectiveDatedResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = FactsFile(base_payload())
        self.addCleanup(self.fixture.close)
        self.facts = load_fee_facts(self.fixture.path)

    def test_official_defaults_and_effective_dated_series_changes(
        self,
    ) -> None:
        default_taker = fill(
            market_ticker="KXDEFAULT-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=25,
        )
        default_maker = fill(
            market_ticker="KXDEFAULT-GAME-YES",
            role=LiquidityRole.MAKER,
            executed_at_ns=25,
        )
        self.assertEqual(
            16_800,
            self.facts.assess(
                default_taker,
                series_ticker="KXDEFAULT",
                event_ticker="KXDEFAULT-GAME",
            ).trade_fee_e6,
        )
        self.assertEqual(
            0,
            self.facts.assess(
                default_maker,
                series_ticker="KXDEFAULT",
                event_ticker="KXDEFAULT-GAME",
            ).trade_fee_e6,
        )

        before = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.MAKER,
            executed_at_ns=49,
        )
        maker_on = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.MAKER,
            executed_at_ns=50,
        )
        maker_off = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.MAKER,
            executed_at_ns=300,
        )
        assessments = [
            self.facts.assess(
                item,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-GAME",
            ).trade_fee_e6
            for item in (before, maker_on, maker_off)
        ]
        self.assertEqual([0, 4_200, 0], assessments)

    def test_event_override_and_waiver_take_precedence(self) -> None:
        override = fill(
            market_ticker="KXSPORTS-OVERRIDE-YES",
            role=LiquidityRole.MAKER,
            executed_at_ns=75,
        )
        waived = fill(
            market_ticker="KXSPORTS-WAIVED-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=99,
        )
        expired = fill(
            market_ticker="KXSPORTS-WAIVED-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=100,
        )
        self.assertEqual(
            8_400,
            self.facts.assess(
                override,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-OVERRIDE",
            ).trade_fee_e6,
        )
        self.assertEqual(
            0,
            self.facts.assess(
                waived,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-WAIVED",
            ).trade_fee_e6,
        )
        self.assertEqual(
            16_800,
            self.facts.assess(
                expired,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-WAIVED",
            ).trade_fee_e6,
        )

    def test_missing_event_context_future_snapshot_and_flat_fail_closed(
        self,
    ) -> None:
        unknown_event = fill(
            market_ticker="KXSPORTS-UNKNOWN-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=25,
        )
        with self.assertRaises(FeeFactsUnavailable):
            self.facts.assess(
                unknown_event,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-UNKNOWN",
            )

        too_new = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=501,
        )
        with self.assertRaises(FeeFactsUnavailable):
            self.facts.assess(
                too_new,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-GAME",
            )

        payload = base_payload()
        payload["series_changes"].append(
            {
                "change_id": "flat-unsupported",
                "effective_from_ns": 400,
                "fee_type": "flat",
                "multiplier_e4": 10_000,
                "series_ticker": "KXSPORTS",
            }
        )
        flat_fixture = FactsFile(payload)
        self.addCleanup(flat_fixture.close)
        flat_facts = load_fee_facts(flat_fixture.path)
        flat_fill = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=400,
        )
        with self.assertRaises(FeeFactsUnavailable):
            flat_facts.assess(
                flat_fill,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-GAME",
            )

    def test_actual_fee_must_match_bound_private_receipt(self) -> None:
        observed = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=75,
            actual_fee_e6=12_345,
            actual_receipt_sha256=PRIVATE_SOURCE_RECEIPT_SHA,
        )
        self.assertEqual(
            12_345,
            self.facts.assess(
                observed,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-GAME",
            ).fee_e6,
        )
        unbound = fill(
            market_ticker="KXSPORTS-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=75,
            actual_fee_e6=12_345,
            actual_receipt_sha256="e" * 64,
        )
        with self.assertRaises(FeeFactsUnavailable):
            self.facts.assess(
                unbound,
                series_ticker="KXSPORTS",
                event_ticker="KXSPORTS-GAME",
            )


class OfficialMoneyE6ExamplesTests(unittest.TestCase):
    def test_direct_and_non_direct_precision_are_distinct(self) -> None:
        non_direct_fixture = FactsFile(base_payload())
        direct_fixture = FactsFile(
            base_payload(precision="DIRECT_CENTICENT")
        )
        self.addCleanup(non_direct_fixture.close)
        self.addCleanup(direct_fixture.close)
        non_direct = load_fee_facts(non_direct_fixture.path)
        direct = load_fee_facts(direct_fixture.path)
        combined = fill(
            market_ticker="KXDEFAULT-GAME-YES",
            role=LiquidityRole.TAKER,
            executed_at_ns=25,
            price_e4=3_301,
            quantity_e4=300,
        )
        non_direct_fee = non_direct.assess(
            combined,
            series_ticker="KXDEFAULT",
            event_ticker="KXDEFAULT-GAME",
        )
        direct_fee = direct.assess(
            combined,
            series_ticker="KXDEFAULT",
            event_ticker="KXDEFAULT-GAME",
        )
        self.assertEqual(500, non_direct_fee.trade_fee_e6)
        self.assertEqual(10_097, non_direct_fee.fee_e6)
        self.assertEqual(597, direct_fee.fee_e6)

    def test_official_three_rounding_examples_are_exact_moneye6(self) -> None:
        # $0.055 × 1, using the official example's $0.0085 trade fee.
        self.assertEqual(55_000, exact_trade_notional_e6(550, 10_000))
        subpenny = apply_balance_rounding(
            revenue_e6=-55_000,
            trade_fee_e6=8_500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(
            (-70_000, 6_500, 15_000),
            (
                subpenny.rounded_balance_change_e6,
                subpenny.rounding_fee_e6,
                subpenny.net_fee_e6,
            ),
        )

        # $0.50 × 0.30, using the official example's $0.0041 trade fee.
        self.assertEqual(150_000, exact_trade_notional_e6(5_000, 3_000))
        fractional = apply_balance_rounding(
            revenue_e6=-150_000,
            trade_fee_e6=4_100,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(
            (-160_000, 5_900, 10_000),
            (
                fractional.rounded_balance_change_e6,
                fractional.rounding_fee_e6,
                fractional.net_fee_e6,
            ),
        )

        # $0.3301 × 0.03 preserves all six decimal places.
        self.assertEqual(9_903, exact_trade_notional_e6(3_301, 300))
        combined = apply_balance_rounding(
            revenue_e6=-9_903,
            trade_fee_e6=500,
            precision=FeePrecision.NON_DIRECT_CENT,
            accumulator_before_e6=0,
        )
        self.assertEqual(
            (-20_000, 9_597, 10_097),
            (
                combined.rounded_balance_change_e6,
                combined.rounding_fee_e6,
                combined.net_fee_e6,
            ),
        )


if __name__ == "__main__":
    unittest.main()
