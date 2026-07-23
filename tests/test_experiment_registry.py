#!/usr/bin/env python3
"""Offline contract tests for the monetizable experiment registry."""

from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import experiment_registry as registry_module  # noqa: E402


EXPECTED_FAMILY_COUNTS = {
    "A": 23,
    "B": 17,
    "C": 10,
    "D": 10,
    "E": 6,
    "F": 17,
    "G": 12,
    "P": 11,
}


def _rehash_registry(registry: dict) -> None:
    """Re-sign a tampered object so tests exercise semantic, not hash, gates."""

    for card in registry.get("experiments", []):
        card.pop("definition_sha256", None)
        card["definition_sha256"] = registry_module._sha256(card)
    registry.pop("registry_sha256", None)
    registry["registry_sha256"] = registry_module._sha256(registry)


class ExperimentRegistryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = registry_module.build_registry()
        cls.cards = cls.registry["experiments"]
        cls.by_id = {
            card["identity"]["experiment_id"]: card for card in cls.cards
        }

    def test_exact_atomic_ids_and_family_counts(self) -> None:
        ids = tuple(
            card["identity"]["experiment_id"] for card in self.cards
        )
        self.assertEqual(106, len(ids))
        self.assertEqual(registry_module.ATOMIC_IDS, ids)
        self.assertEqual(106, len(set(ids)))
        self.assertEqual(
            EXPECTED_FAMILY_COUNTS,
            dict(
                Counter(
                    card["identity"]["family_code"] for card in self.cards
                )
            ),
        )

    def test_every_action_card_has_execution_and_after_cost_pnl(self) -> None:
        action_cards = [
            card for card in self.cards if card["action"]["action_type"]
        ]
        self.assertGreater(len(action_cards), 0)
        for card in action_cards:
            card_id = card["identity"]["experiment_id"]
            with self.subTest(card_id=card_id):
                self.assertNotEqual(
                    "NONE", card["venue"]["execution_venue"]
                )
                self.assertIsNotNone(card["baseline"]["baseline_id"])
                self.assertEqual(
                    "PATHWISE_AFTER_COST", card["pnl"]["basis"]
                )
                self.assertTrue(card["pnl"]["formula"])
                self.assertTrue(card["pnl"]["incremental_formula"])
                self.assertEqual(
                    "eligible no-trigger/no-fill/no-accept rows contribute zero",
                    card["pnl"]["zero_fill_treatment"],
                )
                self.assertTrue(card["costs"])
                self.assertTrue(
                    all(cost.get("type") and cost.get("rule")
                        for cost in card["costs"])
                )
                self.assertTrue(
                    set(registry_module._cost_blockers(card["costs"]))
                    <= set(card["blockers"])
                )

    def test_no_action_card_is_never_a_strategy(self) -> None:
        no_action_cards = [
            card for card in self.cards if not card["action"]["action_type"]
        ]
        self.assertGreater(len(no_action_cards), 0)
        for card in no_action_cards:
            card_id = card["identity"]["experiment_id"]
            with self.subTest(card_id=card_id):
                classification = card["classification"]
                self.assertFalse(classification["strategy_eligible"])
                self.assertNotEqual(
                    "EXECUTABLE_STRATEGY",
                    classification["computed_class"],
                )
                self.assertEqual("NONE", card["venue"]["execution_venue"])
                self.assertEqual(
                    "NOT_DEFINED_NOT_A_STRATEGY", card["pnl"]["status"]
                )
                self.assertEqual([], card["costs"])

    def test_required_external_sports_data_fails_closed(self) -> None:
        required_cards = [
            card
            for card in self.cards
            if card["external_sports_data"]["dependency"] == "REQUIRED"
        ]
        self.assertGreater(len(required_cards), 0)
        for card in required_cards:
            card_id = card["identity"]["experiment_id"]
            external = card["external_sports_data"]
            with self.subTest(card_id=card_id):
                self.assertEqual("MISSING", external["availability"])
                self.assertEqual(
                    "BLOCK_NO_IMPUTE", external["missing_policy"]
                )
                self.assertIn(
                    "EXTERNAL_SPORTS_DATA_MISSING", card["blockers"]
                )
                self.assertFalse(card["classification"]["strategy_eligible"])
                if card["action"]["action_type"]:
                    self.assertIn(
                        "INSTRUMENT_MAPPING_UNKNOWN", card["blockers"]
                    )
                    self.assertIn(
                        "versioned provider-event-to-Kalshi "
                        "root/market/outcome mapping",
                        external["required_variables"],
                    )
                    self.assertTrue(
                        any(
                            cost["type"] == "DATA_API"
                            for cost in card["costs"]
                        )
                    )

    def test_mve_uses_its_own_private_execution_contract(self) -> None:
        catalog = self.by_id["D09-MVE-CATALOG-SCHEMA"]
        self.assertEqual("NONE", catalog["venue"]["execution_venue"])
        self.assertNotIn("VENUE_PERMISSION_MISSING", catalog["blockers"])

        direct = self.by_id["D10-MVE-DIRECT-TRADING"]
        self.assertEqual("MVE_DIRECT", direct["action"]["profile_id"])
        self.assertEqual("MVE_PRIVATE", direct["data"]["profile_id"])
        self.assertEqual(
            "KALSHI_MVE_PRIVATE_EXECUTION",
            direct["venue"]["execution_venue"],
        )
        self.assertIn("MVE_PRIVATE_EVENTS_MISSING", direct["blockers"])
        self.assertNotIn("RFQ_PUBLIC_DATA_UNRELEASED", direct["blockers"])
        self.assertIn(
            "MVE_package_and_hedge_cash_in", direct["pnl"]["formula"]
        )

    def test_private_inventory_signal_also_binds_public_market_state(self) -> None:
        card = self.by_id["A16-INVENTORY-RESERVATION-SKEW"]
        self.assertEqual("L1_PRIVATE", card["data"]["profile_id"])
        self.assertTrue(
            {
                "KALSHI_L1",
                "PUBLIC_TRADES",
                "CATALOG",
                "FROZEN_PAST_ONLY_FAIR_AND_VOLATILITY",
                "PRIVATE_ACCOUNT_ORDERS_POSITIONS_FILLS",
            }
            <= set(card["data"]["inputs"])
        )
        self.assertIn("PRIVATE_ACCOUNT_STATE_MISSING", card["blockers"])
        self.assertIn(
            "CONFIRMED_CHILD_STRATEGIES_MISSING", card["blockers"]
        )

    def test_commercial_roc_subtracts_full_operating_cost(self) -> None:
        card = self.by_id["P10-COMMERCIAL-ROC-COST"]
        self.assertEqual("COMMERCIAL_ROC", card["action"]["profile_id"])
        self.assertEqual(
            "UNCHANGED_CONFIRMED_CHILD_WITHOUT_COMMERCIAL_GATE",
            card["baseline"]["baseline_id"],
        )
        cost_types = {cost["type"] for cost in card["costs"]}
        self.assertTrue(
            {
                "DATA_PROVIDER_LICENSE_API",
                "CLOUD_COMPUTE_STORAGE_EGRESS",
                "OPS_LABOR_AND_INCIDENT",
                "COLLATERAL_AND_CAPITAL",
            }
            <= cost_types
        )
        self.assertIn("OPERATING_COST_UNKNOWN", card["blockers"])
        self.assertIn("CommercialROC", card["pnl"]["roc_definition"])
        self.assertIn("Fully allocated", card["pnl"]["cost_allocation_rule"])

    def test_child_dependent_profiles_cannot_float_without_a_child(self) -> None:
        cards = [
            card
            for card in self.cards
            if card["action"]["profile_id"]
            in registry_module.CHILD_DEPENDENT_ACTION_PROFILES
        ]
        self.assertGreater(len(cards), 0)
        for card in cards:
            with self.subTest(card_id=card["identity"]["experiment_id"]):
                self.assertIn(
                    "CONFIRMED_CHILD_STRATEGIES_MISSING",
                    card["blockers"],
                )

    def test_terminal_entry_and_existing_position_actions_are_separate(self) -> None:
        for card_id in (
            "B08-CONTINUOUS-PRICE-CALIBRATION",
            "E04-SETTLEMENT-CALIBRATION",
        ):
            with self.subTest(card_id=card_id):
                card = self.by_id[card_id]
                self.assertEqual("TERMINAL_ENTRY", card["action"]["profile_id"])
                self.assertEqual(
                    "EXPLICIT_BUY_YES_OR_BUY_NO_FROM_FLAT",
                    card["action"]["instrument_side"],
                )
                self.assertEqual(
                    "NO_TRADE_SAME_OPPORTUNITIES",
                    card["baseline"]["baseline_id"],
                )
        convergence = self.by_id["E01-SETTLEMENT-CONVERGENCE"]
        self.assertEqual("CONTROL_ONLY", convergence["action"]["profile_id"])
        self.assertEqual([], convergence["action"]["action_type"])
        self.assertEqual("NONE", convergence["pnl"]["basis"])
        self.assertIn(
            "later settlement label never triggers an order",
            convergence["signal"]["direction_semantics"],
        )
        cs_ask = self.by_id["B16-CS-ASK"]
        self.assertEqual("CS_ASK", cs_ask["action"]["profile_id"])
        self.assertEqual("POST_ONLY_LIMIT_ONE_FILL_MAX", cs_ask["action"]["order_style"])
        self.assertEqual(
            "actual authoritative settlement only for binding PnL",
            cs_ask["action"]["exit_or_settlement"],
        )
        self.assertIn("q - Y_high_outcome_terminal", cs_ask["pnl"]["formula"])
        for card_id in ("E06-HOLD-TO-TERMINAL-TAIL",):
            with self.subTest(card_id=card_id):
                card = self.by_id[card_id]
                self.assertEqual("TERMINAL_POLICY", card["action"]["profile_id"])
                self.assertEqual(
                    "EXACT_EXISTING_POSITION",
                    card["action"]["instrument_side"],
                )
        early_exit = self.by_id["E03-FREEZE-WINDOW"]
        self.assertEqual("EARLY_EXIT", early_exit["action"]["profile_id"])
        self.assertEqual(
            "WAIT_UNTIL_FREEZE_OR_TERMINAL",
            early_exit["baseline"]["baseline_id"],
        )

    def test_continuous_and_near_terminal_calibration_do_not_overlap(self) -> None:
        continuous = self.by_id["B08-CONTINUOUS-PRICE-CALIBRATION"]
        near_terminal = self.by_id["E04-SETTLEMENT-CALIBRATION"]
        self.assertIn(
            "outside E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW",
            continuous["signal"]["trigger"],
        )
        self.assertIn(
            "inside E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW",
            near_terminal["signal"]["trigger"],
        )
        self.assertIn(
            "abstains inside E04",
            continuous["experiment_design"]["overlap_and_precedence"],
        )
        self.assertIn(
            "counted once",
            near_terminal["experiment_design"]["overlap_and_precedence"],
        )

    def test_roll_and_hedge_compare_to_existing_exposure(self) -> None:
        roll = self.by_id["E02-EXPIRY-LIQUIDITY-MIGRATION"]
        self.assertEqual("EXPIRY_ROLL", roll["action"]["profile_id"])
        self.assertEqual(
            "HOLD_ORIGINAL_EXPOSURE", roll["baseline"]["baseline_id"]
        )
        self.assertIn(
            "NetPnL_hold_original_exposure",
            roll["pnl"]["incremental_formula"],
        )

        hedge = self.by_id["P05-BRACKET-HEDGE"]
        self.assertEqual("CHILD_HEDGE", hedge["action"]["profile_id"])
        self.assertEqual(
            "UNHEDGED_CONFIRMED_CHILD", hedge["baseline"]["baseline_id"]
        )
        self.assertIn(
            "NetPnL_unhedged_same_child",
            hedge["pnl"]["incremental_formula"],
        )

    def test_t90_rebuild_is_a_paired_increment(self) -> None:
        parent = self.by_id["A13-T90-RANGE"]
        rebuild = self.by_id["A14-T90-REBUILD"]
        self.assertEqual("T90_RANGE", parent["action"]["profile_id"])
        self.assertIn(
            "SAME_T90_RANGE_WITHOUT_RETREAT_KILL",
            parent["baseline"]["secondary_baselines"],
        )
        self.assertEqual("T90_REBUILD", rebuild["action"]["profile_id"])
        self.assertEqual("L1_L2", rebuild["data"]["profile_id"])
        self.assertEqual(
            "T90_PARENT_NEVER_REENTER", rebuild["baseline"]["baseline_id"]
        )
        self.assertIn(
            "NetPnL_same_root_T90_never_reenter",
            rebuild["pnl"]["incremental_formula"],
        )

    def test_authority_semantics_are_preserved(self) -> None:
        b07 = self.by_id["B07-MICROPRICE-PREDICTIVITY"]
        self.assertEqual("L1_L2", b07["data"]["profile_id"])

        b10 = self.by_id["B10-INPLAY-PROXY-PRICE-EROSION"]
        self.assertEqual("L1", b10["data"]["profile_id"])
        self.assertEqual(
            "OPTIONAL_REFINEMENT",
            b10["external_sports_data"]["dependency"],
        )
        self.assertIn("never call it score", b10["signal"]["direction_semantics"])

        f11 = self.by_id["F11-GOLF-THIN-LONG-LIFECYCLE"]
        self.assertEqual("L1", f11["data"]["profile_id"])
        self.assertEqual(
            "OPTIONAL_REFINEMENT",
            f11["external_sports_data"]["dependency"],
        )

        f15 = self.by_id["F15-BLINDNESS-EVPI"]
        self.assertEqual("OWNER_EVPI", f15["data"]["profile_id"])
        self.assertEqual(
            "NOT_REQUIRED", f15["external_sports_data"]["dependency"]
        )
        self.assertEqual("CONTROL_ONLY", f15["action"]["profile_id"])
        self.assertIn(
            "CONFIRMED_CHILD_STRATEGIES_MISSING", f15["blockers"]
        )
        self.assertEqual(
            "NO", f15["data"]["can_compute_now"]["signal_event"]
        )
        self.assertEqual(
            [],
            f15["data"]["study_dates"][
                "eligible_for_current_signal_description"
            ],
        )
        self.assertIn(
            "never a live signal", f15["signal"]["direction_semantics"]
        )

    def test_public_rfq_signals_remain_unsigned(self) -> None:
        for card_id in (
            "D01-RFQ-FLOW-CENSUS",
            "D04-RFQ-COMBO-DEMAND-LEG-PRESSURE",
            "D06-RFQ-DIRECTION-VOL-SIGNAL",
        ):
            with self.subTest(card_id=card_id):
                direction = self.by_id[card_id]["signal"][
                    "direction_semantics"
                ].upper()
                self.assertTrue(
                    "UNSIGNED" in direction or "NO_DIRECTION" in direction
                )
        for card_id in (
            "D02-RFQ-SIZE-INTENT",
            "D03-RFQ-LIFECYCLE-SURVIVAL",
            "D05-RFQ-REQUESTER-HASH",
            "D07-RFQ-TO-CLOB-IMPACT",
        ):
            with self.subTest(card_id=card_id):
                self.assertEqual(
                    "BOTH_RELATED_CLOB_SIDES_PUBLIC_UNSIGNED",
                    self.by_id[card_id]["action"]["instrument_side"],
                )

    def test_card_hash_tamper_is_rejected(self) -> None:
        tampered = copy.deepcopy(self.registry)
        tampered["experiments"][0]["signal"]["trigger"] += " TAMPERED"
        errors = registry_module.validate_registry(tampered)
        self.assertTrue(errors)
        self.assertTrue(
            any("definition SHA mismatch" in error for error in errors),
            errors,
        )

    def test_rehashed_semantic_attacks_are_rejected(self) -> None:
        action_card_index = next(
            index
            for index, card in enumerate(self.cards)
            if card["action"]["action_type"]
        )
        required_index = next(
            index
            for index, card in enumerate(self.cards)
            if card["external_sports_data"]["dependency"] == "REQUIRED"
            and card["action"]["action_type"]
        )
        rfq_index = registry_module.ATOMIC_IDS.index(
            "D02-RFQ-SIZE-INTENT"
        )
        unavailable_index = next(
            index
            for index, card in enumerate(self.cards)
            if card["data"]["can_compute_now"]["signal_event"] == "NO"
        )

        def set_live_authority(registry: dict) -> None:
            registry["scope"]["action_authority"] = "LIVE"

        def empty_action(registry: dict) -> None:
            registry["experiments"][action_card_index]["action"][
                "price_rule"
            ] = ""

        def empty_baseline(registry: dict) -> None:
            registry["experiments"][action_card_index]["baseline"][
                "policy"
            ] = ""

        def empty_pnl(registry: dict) -> None:
            registry["experiments"][action_card_index]["pnl"][
                "formula"
            ] = ""

        def unknown_cost_status(registry: dict) -> None:
            registry["experiments"][action_card_index]["costs"][0][
                "status"
            ] = "UNKNOWN"

        def allow_external_price_imputation(registry: dict) -> None:
            registry["experiments"][required_index][
                "external_sports_data"
            ]["proxy_policy"] = "ALLOW_PRICE_IMPUTATION"

        def inject_direction_into_public_rfq(registry: dict) -> None:
            registry["experiments"][rfq_index]["action"][
                "signal_to_action_mapping"
            ] += " BUY_YES"

        def add_bad_study_date(registry: dict) -> None:
            registry["experiments"][unavailable_index]["data"][
                "study_dates"
            ]["eligible_for_current_signal_description"] = ["2026-07-13"]

        def delete_evidence_binding(registry: dict) -> None:
            registry["source_bindings"].pop()

        def forge_summary(registry: dict) -> None:
            registry["summary"] = {
                key: 0 for key in registry["summary"]
            }

        attacks = {
            "live_authority": set_live_authority,
            "empty_action": empty_action,
            "empty_baseline": empty_baseline,
            "empty_pnl": empty_pnl,
            "unknown_cost_status": unknown_cost_status,
            "external_imputation": allow_external_price_imputation,
            "directional_public_rfq": inject_direction_into_public_rfq,
            "bad_study_date": add_bad_study_date,
            "deleted_evidence": delete_evidence_binding,
            "forged_summary": forge_summary,
        }
        for name, mutate in attacks.items():
            with self.subTest(attack=name):
                tampered = copy.deepcopy(self.registry)
                mutate(tampered)
                _rehash_registry(tampered)
                errors = registry_module.validate_registry(tampered)
                self.assertTrue(errors)
                self.assertFalse(
                    any("SHA mismatch" in error for error in errors),
                    errors,
                )

    def test_missing_card_is_rejected(self) -> None:
        incomplete = copy.deepcopy(self.registry)
        incomplete["experiments"].pop()
        errors = registry_module.validate_registry(incomplete)
        self.assertTrue(errors)
        self.assertTrue(
            any("expected 106 cards" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("card IDs/order differ" in error for error in errors),
            errors,
        )

    def test_missing_cost_cannot_be_unblocked(self) -> None:
        tampered = copy.deepcopy(self.registry)
        card = next(
            item
            for item in tampered["experiments"]
            if item["action"]["action_type"]
        )
        cost_blocker = registry_module._cost_blockers(card["costs"])[0]
        card["blockers"].remove(cost_blocker)
        errors = registry_module.validate_registry(tampered)
        self.assertTrue(
            any(
                "missing cost is not represented by a critical blocker" in error
                for error in errors
            ),
            errors,
        )

    def test_signal_unavailable_has_no_eligible_dates(self) -> None:
        unavailable = [
            card
            for card in self.cards
            if card["data"]["can_compute_now"]["signal_event"] != "YES"
        ]
        self.assertGreater(len(unavailable), 0)
        for card in unavailable:
            with self.subTest(card_id=card["identity"]["experiment_id"]):
                self.assertEqual(
                    [],
                    card["data"]["study_dates"][
                        "eligible_for_current_signal_description"
                    ],
                )

        tampered = copy.deepcopy(self.registry)
        card = next(
            item
            for item in tampered["experiments"]
            if item["data"]["can_compute_now"]["signal_event"] == "NO"
        )
        card["data"]["study_dates"][
            "eligible_for_current_signal_description"
        ] = ["2026-07-10"]
        errors = registry_module.validate_registry(tampered)
        self.assertTrue(
            any("dates marked eligible" in error for error in errors),
            errors,
        )

    def test_each_blocker_has_specific_remediation(self) -> None:
        for card in self.cards:
            card_id = card["identity"]["experiment_id"]
            gaps = card["data"]["critical_gaps"]
            with self.subTest(card_id=card_id):
                self.assertEqual(
                    card["blockers"],
                    [gap["blocker"] for gap in gaps],
                )
                self.assertTrue(
                    all(
                        gap["missing_evidence"]
                        and gap["acquisition_or_build_path"]
                        and gap["unknown_value_policy"] == "BLOCK_NOT_ZERO"
                        for gap in gaps
                    )
                )
        for card_id in (
            "A07D-ANCHOR-MANIPULABILITY",
            "A20-SYSTEM-LOAD-QUEUE-GROWTH",
            "F07-LOW-OCC-INDEPENDENT-FAIR",
        ):
            with self.subTest(card_id=card_id):
                card = self.by_id[card_id]
                self.assertEqual(
                    "NO", card["data"]["can_compute_now"]["signal_event"]
                )
                self.assertEqual(
                    [],
                    card["data"]["study_dates"][
                        "eligible_for_current_signal_description"
                    ],
                )

    def test_fresh_registry_passes_its_validator(self) -> None:
        self.assertEqual([], registry_module.validate_registry(self.registry))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            ExperimentRegistryContractTests
        )
    )
    if result.wasSuccessful():
        print("PASS: experiment registry offline contract tests")
    raise SystemExit(0 if result.wasSuccessful() else 1)
