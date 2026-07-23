#!/usr/bin/env python3
"""Fail-closed contract tests for the PnL-spine experiment freeze."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "frozen"
    / "PNL_SPINE_EXPERIMENTS_V1.json"
)
REGISTRY_PATH = (
    ROOT / "Deepresearch V3" / "registry" / "EXPERIMENT_REGISTRY_V1.json"
)

EXPECTED_REGISTRY_SHA = (
    "6253beeb1934c1310611cb470a560b0c3dee8487922b1fa7f8d4718493ceea16"
)
EXPECTED_CARD_SHAS = {
    "A01-SPREAD-CAPTURE": (
        "d0a426775e71969141ec96db9fac7a5d38e53fb4d294d1d6027dd37bfc4cb9d4"
    ),
    "A11-ONE-SIDED-PROVISION": (
        "4258bbe91636b0e1798a733e9c58d958bf51ae5548156b5813d47f374ebf6d2c"
    ),
    "B09-LISTING-TO-START-DRIFT": (
        "d2c4c9765c141713bf1d10d1147dc7edd8a35cd2a70a587a7620223081c28023"
    ),
}
EXPECTED_ENGINEERING_DATES = [
    "2026-07-12",
    "2026-07-15",
    "2026-07-17",
]
EXPECTED_SOURCE_PLAN_SHA = (
    "ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36"
)


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _without_key(value: dict, key: str) -> dict:
    result = copy.deepcopy(value)
    result.pop(key, None)
    return result


class PnlSpineExperimentFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.revisions = {
            item["source_card"]["experiment_id"]: item
            for item in cls.freeze["revisions"]
        }
        cls.registry_cards = {
            item["identity"]["experiment_id"]: item
            for item in cls.registry["experiments"]
        }

    def assertExactKeys(
        self, value: dict, expected: set[str], path: str
    ) -> None:
        self.assertEqual(expected, set(value), f"unknown/missing keys at {path}")

    def test_top_level_and_shared_objects_reject_unknown_keys(self) -> None:
        self.assertExactKeys(
            self.freeze,
            {
                "schema_version",
                "freeze_id",
                "freeze_sha256",
                "registry_binding",
                "claim_contract",
                "cohorts",
                "execution_contract",
                "required_bindings",
                "revisions",
            },
            "$",
        )
        self.assertExactKeys(
            self.freeze["registry_binding"],
            {"path", "registry_id", "registry_sha256"},
            "$.registry_binding",
        )
        self.assertExactKeys(
            self.freeze["claim_contract"],
            {
                "claim_tier",
                "execution_authority",
                "live_order_authority",
                "promotion_allowed",
                "pnl_basis",
                "pnl_formula",
                "incremental_formula",
                "aggregation_unit",
                "zero_treatment",
                "markout_policy",
                "capacity_policy",
            },
            "$.claim_contract",
        )
        self.assertExactKeys(
            self.freeze["cohorts"],
            {"engineering_acceptance", "untouched_confirmation"},
            "$.cohorts",
        )
        self.assertExactKeys(
            self.freeze["cohorts"]["engineering_acceptance"],
            {
                "cohort_id",
                "dates_utc",
                "selection_status",
                "purpose",
                "claim_tier",
                "inference_allowed",
                "promotion_allowed",
            },
            "$.cohorts.engineering_acceptance",
        )
        self.assertExactKeys(
            self.freeze["cohorts"]["untouched_confirmation"],
            {
                "cohort_id",
                "dates_utc",
                "status",
                "allocation_receipt_sha256",
                "claim_tier",
                "read_allowed",
                "promotion_allowed",
            },
            "$.cohorts.untouched_confirmation",
        )
        self.assertExactKeys(
            self.freeze["required_bindings"],
            {
                "fees",
                "latency",
                "root_and_start",
                "risk",
                "terminal",
                "execution",
            },
            "$.required_bindings",
        )
        self.assertExactKeys(
            self.freeze["execution_contract"],
            {
                "stage",
                "fill_authority",
                "at_price_fill",
                "same_timestamp_ordering",
                "public_volume_allocation",
                "private_event_dedupe",
                "cancel_before_replace",
                "partial_cancel_reconcile",
                "passive_order_tif",
                "passive_post_only",
                "forced_exit_tif",
                "forced_exit_post_only",
                "forced_exit_reduce_only",
                "exit_price",
                "midpoint_exit_allowed",
                "price_quantity_precision",
                "cash_precision",
                "size_per_side_contracts",
                "max_abs_root_inventory_contracts",
                "max_active_markets_per_root",
                "collateral_reserve_mode",
                "conservation_invariants",
            },
            "$.execution_contract",
        )
        for category, bindings in self.freeze["required_bindings"].items():
            self.assertTrue(bindings, category)
            for index, binding in enumerate(bindings):
                self.assertExactKeys(
                    binding,
                    {"field", "value", "status", "unknown_value_policy"},
                    f"$.required_bindings.{category}[{index}]",
                )

    def test_revision_shapes_and_card_specific_shapes_reject_unknown_keys(
        self,
    ) -> None:
        expected_revision_keys = {
            "revision_id",
            "revision_version",
            "source_card",
                "state",
                "claim_tier",
                "required_bindings_ref",
                "card_required_bindings",
                "engineering_cohort_ref",
            "confirmation_cohort_ref",
            "baseline",
            "parameter_freeze",
            "validation_contract",
            "revision_definition_sha256",
        }
        expected_baseline_keys = {
            "baseline_id",
            "baseline_type",
            "policy",
            "comparability",
            "secondary_baselines",
            "excluded_alternatives",
            "status",
        }
        expected_validation_keys = {
            "engineering_acceptance_purpose",
            "primary_estimand",
            "required_invariants",
            "failure_conditions",
            "promotion_rule",
        }
        expected_source_keys = {
            "registry_id",
            "registry_sha256",
            "experiment_id",
            "card_definition_sha256",
        }
        for card_id, revision in self.revisions.items():
            with self.subTest(card_id=card_id):
                self.assertExactKeys(
                    revision, expected_revision_keys, f"$.revisions[{card_id}]"
                )
                self.assertExactKeys(
                    revision["source_card"],
                    expected_source_keys,
                    f"$.revisions[{card_id}].source_card",
                )
                self.assertExactKeys(
                    revision["baseline"],
                    expected_baseline_keys,
                    f"$.revisions[{card_id}].baseline",
                )
                self.assertExactKeys(
                    revision["validation_contract"],
                    expected_validation_keys,
                    f"$.revisions[{card_id}].validation_contract",
                )
                for index, binding in enumerate(
                    revision["card_required_bindings"]
                ):
                    self.assertExactKeys(
                        binding,
                        {
                            "field",
                            "value",
                            "status",
                            "unknown_value_policy",
                        },
                        (
                            f"$.revisions[{card_id}]"
                            f".card_required_bindings[{index}]"
                        ),
                    )

        provenance_keys = {"source", "proposed", "frozen"}
        a01 = self.revisions["A01-SPREAD-CAPTURE"]["parameter_freeze"]
        a11 = self.revisions["A11-ONE-SIDED-PROVISION"]["parameter_freeze"]
        self.assertExactKeys(
            a01,
            {
                "source_plan",
                "source_plan_sha256",
                "policy_lineage",
                "book_ttl_ms",
                "minimum_raw_spread_ticks",
                "spread_dwell_ms",
                "max_quote_aggressiveness",
                "cost_buffer_ticks",
                "toxicity_gate",
                "adverse_width_bound",
                "fair_model",
                "inventory_skew_gamma_ticks_per_contract",
                "requote_policy",
                "warmup_ms",
                "activity_gate",
                "normal_quote_age_ms",
                "normal_requote_cooldown_ms",
                "stop_new_risk_tts_ms",
                "force_unwind_tts_ms",
                "quantity_contracts_per_active_side",
                "fill_response",
            },
            "$.revisions[A01].parameter_freeze",
        )
        self.assertExactKeys(
            a11,
            {
                "source_plan",
                "source_plan_sha256",
                "policy_lineage",
                "tts_window_ms",
                "normalized_price_e4",
                "one_sided_persistence_ms",
                "surviving_side_book_age_max_ms",
                "minimum_updates_trailing_60s",
                "minimum_trades_trailing_5m",
                "reference_mid_onset_age_max_ms",
                "surviving_side_max_move_ticks",
                "missing_side_offset_k_ticks",
                "quantity_contracts",
                "max_fills_per_root",
                "replenish",
                "quote_age_ms",
                "max_hold_ms",
                "quote_rule",
                "cancel_rule",
                "timeout_exit",
                "residual_valuation",
            },
            "$.revisions[A11].parameter_freeze",
        )
        for card_id, params in (("A01", a01), ("A11", a11)):
            for name, value in params.items():
                if name in {
                    "source_plan",
                    "source_plan_sha256",
                    "policy_lineage",
                }:
                    continue
                self.assertExactKeys(
                    value,
                    provenance_keys,
                    f"$.revisions[{card_id}].parameter_freeze.{name}",
                )

        b09 = self.revisions["B09-LISTING-TO-START-DRIFT"][
            "parameter_freeze"
        ]
        self.assertExactKeys(
            b09,
            {
                "source",
                "candidate_taxonomy",
                "training_validation_protocol",
                "unresolved_training_outputs",
                "pretraining_required_bindings",
            },
            "$.revisions[B09].parameter_freeze",
        )
        self.assertExactKeys(
            b09["candidate_taxonomy"],
            {
                "listing_age",
                "scheduled_phase",
                "interval_semantics",
                "unknown_bucket_policy",
            },
            "$.revisions[B09].parameter_freeze.candidate_taxonomy",
        )
        self.assertExactKeys(
            b09["training_validation_protocol"],
            {
                "protocol_id",
                "status",
                "allocation_basis",
                "allocation_constraints",
                "train_only_steps",
                "train_artifact_required_fields",
                "train_artifact_sha256",
                "validation_lock",
                "retraining_policy",
            },
            (
                "$.revisions[B09].parameter_freeze"
                ".training_validation_protocol"
            ),
        )
        self.assertExactKeys(
            b09["unresolved_training_outputs"],
            {"admitted_cells", "direction_by_cell"},
            "$.revisions[B09].unresolved_training_outputs",
        )
        for name, value in b09["unresolved_training_outputs"].items():
            self.assertExactKeys(
                value,
                {
                    "source",
                    "proposed",
                    "frozen",
                    "status",
                    "unknown_value_policy",
                },
                f"$.revisions[B09].unresolved_training_outputs.{name}",
            )
        for index, binding in enumerate(
            b09["pretraining_required_bindings"]
        ):
            self.assertExactKeys(
                binding,
                {"field", "value", "status", "unknown_value_policy"},
                f"$.revisions[B09].pretraining_required_bindings[{index}]",
            )

    def test_registry_card_and_revision_definition_shas(self) -> None:
        registry_without_sha = _without_key(
            self.registry, "registry_sha256"
        )
        self.assertEqual(
            EXPECTED_REGISTRY_SHA, _sha256(registry_without_sha)
        )
        self.assertEqual(
            EXPECTED_REGISTRY_SHA,
            self.freeze["registry_binding"]["registry_sha256"],
        )
        for card_id, expected_sha in EXPECTED_CARD_SHAS.items():
            with self.subTest(card_id=card_id):
                registry_card = self.registry_cards[card_id]
                actual_card_sha = _sha256(
                    _without_key(registry_card, "definition_sha256")
                )
                self.assertEqual(expected_sha, actual_card_sha)
                revision = self.revisions[card_id]
                self.assertEqual(
                    expected_sha,
                    revision["source_card"]["card_definition_sha256"],
                )
                self.assertEqual(
                    EXPECTED_REGISTRY_SHA,
                    revision["source_card"]["registry_sha256"],
                )
                self.assertEqual(
                    revision["revision_definition_sha256"],
                    _sha256(
                        _without_key(
                            revision, "revision_definition_sha256"
                        )
                    ),
                )
        self.assertEqual(
            self.freeze["freeze_sha256"],
            _sha256(_without_key(self.freeze, "freeze_sha256")),
        )

    def test_split_is_engineering_only_and_confirmation_is_unallocated(
        self,
    ) -> None:
        engineering = self.freeze["cohorts"]["engineering_acceptance"]
        self.assertEqual(EXPECTED_ENGINEERING_DATES, engineering["dates_utc"])
        self.assertEqual("ENGINEERING_ONLY", engineering["claim_tier"])
        self.assertFalse(engineering["inference_allowed"])
        self.assertFalse(engineering["promotion_allowed"])

        confirmation = self.freeze["cohorts"]["untouched_confirmation"]
        self.assertEqual("NOT_ALLOCATED", confirmation["status"])
        self.assertEqual([], confirmation["dates_utc"])
        self.assertIsNone(confirmation["cohort_id"])
        self.assertIsNone(confirmation["allocation_receipt_sha256"])
        self.assertFalse(confirmation["read_allowed"])
        self.assertFalse(confirmation["promotion_allowed"])

        self.assertEqual(
            "ENGINEERING_ONLY",
            self.freeze["claim_contract"]["claim_tier"],
        )
        self.assertFalse(
            self.freeze["claim_contract"]["promotion_allowed"]
        )
        for revision in self.freeze["revisions"]:
            self.assertEqual("ENGINEERING_ONLY", revision["claim_tier"])
            self.assertEqual(
                "#/cohorts/engineering_acceptance",
                revision["engineering_cohort_ref"],
            )
            self.assertEqual(
                "#/cohorts/untouched_confirmation",
                revision["confirmation_cohort_ref"],
            )

    def test_every_unknown_binding_fails_closed_and_is_not_zero(self) -> None:
        all_bindings = [
            binding
            for bindings in self.freeze["required_bindings"].values()
            for binding in bindings
        ]
        all_bindings.extend(
            binding
            for revision in self.freeze["revisions"]
            for binding in revision["card_required_bindings"]
        )
        b09 = self.revisions["B09-LISTING-TO-START-DRIFT"][
            "parameter_freeze"
        ]
        all_bindings.extend(b09["pretraining_required_bindings"])
        self.assertGreater(len(all_bindings), 0)
        for binding in all_bindings:
            with self.subTest(field=binding["field"]):
                self.assertIsNone(binding["value"])
                self.assertEqual("NOT_BOUND", binding["status"])
                self.assertEqual(
                    "BLOCK_NOT_ZERO", binding["unknown_value_policy"]
                )

    def test_baseline_contract_is_frozen_and_a11_does_not_mix_pm_parent(
        self,
    ) -> None:
        for card_id, revision in self.revisions.items():
            with self.subTest(card_id=card_id):
                baseline = revision["baseline"]
                self.assertEqual(
                    "NO_TRADE_SAME_OPPORTUNITIES",
                    baseline["baseline_id"],
                )
                self.assertEqual("NO_TRADE", baseline["baseline_type"])
                self.assertEqual("FROZEN", baseline["status"])
                source_baseline = self.registry_cards[card_id]["baseline"]
                self.assertEqual(
                    source_baseline["baseline_id"],
                    baseline["baseline_id"],
                )
        a11 = self.revisions["A11-ONE-SIDED-PROVISION"]["baseline"]
        self.assertEqual(
            ["PM_PARENT_PAIRED_ABLATION"], a11["excluded_alternatives"]
        )
        self.assertEqual([], a11["secondary_baselines"])

    def test_a01_freezes_each_conservative_pm_ts_initial_value(self) -> None:
        params = self.revisions["A01-SPREAD-CAPTURE"][
            "parameter_freeze"
        ]
        self.assertEqual(
            EXPECTED_SOURCE_PLAN_SHA, params["source_plan_sha256"]
        )
        expected = {
            "book_ttl_ms": 250,
            "minimum_raw_spread_ticks": 4,
            "spread_dwell_ms": 5000,
            "max_quote_aggressiveness": "BEHIND_1",
            "cost_buffer_ticks": 2,
            "toxicity_gate": "TRAIN_P90",
            "adverse_width_bound": "CONDITIONAL_TRAIN_P90",
            "fair_model": "LOGODDS_MIDPOINT",
            "inventory_skew_gamma_ticks_per_contract": 0,
            "requote_policy": "R0_SAFETY_ONLY",
        }
        for name, frozen in expected.items():
            with self.subTest(parameter=name):
                self.assertEqual(frozen, params[name]["frozen"])
                self.assertEqual(
                    params[name]["proposed"][0], params[name]["frozen"]
                )
                self.assertTrue(params[name]["source"])

    def test_a11_is_exact_os0_with_no_parent_gate(self) -> None:
        revision = self.revisions["A11-ONE-SIDED-PROVISION"]
        params = revision["parameter_freeze"]
        self.assertEqual(
            EXPECTED_SOURCE_PLAN_SHA, params["source_plan_sha256"]
        )
        self.assertEqual("DR3-PM-OS-01::OS0", params["policy_lineage"])
        self.assertEqual(
            30000, params["one_sided_persistence_ms"]["frozen"]
        )
        self.assertEqual(2, params["missing_side_offset_k_ticks"]["frozen"])
        self.assertEqual(120000, params["max_hold_ms"]["frozen"])
        invariants = revision["validation_contract"]["required_invariants"]
        self.assertIn(
            "PM_PARENT_PAIRED_GATE_IS_NOT_USED_BY_THIS_REVISION",
            invariants,
        )

    def test_b09_cannot_invent_direction_or_cells_before_training(self) -> None:
        revision = self.revisions["B09-LISTING-TO-START-DRIFT"]
        self.assertEqual("BLOCKED_PARAMETER_TRAINING", revision["state"])
        params = revision["parameter_freeze"]
        outputs = params["unresolved_training_outputs"]
        self.assertIsNone(outputs["admitted_cells"]["proposed"])
        self.assertIsNone(outputs["admitted_cells"]["frozen"])
        self.assertIsNone(outputs["direction_by_cell"]["proposed"])
        self.assertIsNone(outputs["direction_by_cell"]["frozen"])
        self.assertEqual(
            "FROZEN_PROTOCOL_AWAITING_TRAIN_ARTIFACT",
            params["training_validation_protocol"]["status"],
        )
        self.assertIsNone(
            params["training_validation_protocol"][
                "train_artifact_sha256"
            ]
        )
        self.assertIn(
            "ENGINEERING_ACCEPTANCE_DATES_CANNOT_SELECT_CELLS_OR_DIRECTIONS",
            params["training_validation_protocol"][
                "allocation_constraints"
            ],
        )

    def test_strict_fill_and_complete_exit_contract_is_fail_closed(
        self,
    ) -> None:
        execution = self.freeze["execution_contract"]
        self.assertFalse(execution["at_price_fill"])
        self.assertEqual(
            "ADVERSE_EVENT_FIRST", execution["same_timestamp_ordering"]
        )
        self.assertEqual(
            "ALLOCATE_EACH_PUBLIC_PRINT_ONCE_GLOBALLY_ACROSS_ALL_ORDERS",
            execution["public_volume_allocation"],
        )
        self.assertEqual(
            "EFFECTIVE_TIME_EXACT_L2_WALK", execution["exit_price"]
        )
        self.assertFalse(execution["midpoint_exit_allowed"])
        self.assertTrue(execution["forced_exit_reduce_only"])
        self.assertIn(
            "UNKNOWN_OR_UNRECONCILED_STATE_BLOCKS_RUN_COMPLETION",
            execution["conservation_invariants"],
        )


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            PnlSpineExperimentFreezeTests
        )
    )
    if result.wasSuccessful():
        print("PASS: PnL-spine experiment freeze offline contract tests")
    raise SystemExit(0 if result.wasSuccessful() else 1)
