"""Pure tests for the cross-restart crypto-MM equity floor."""
from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
MM_DIR = REPO / "tools" / "research" / "crypto_mm"
sys.path.insert(0, str(MM_DIR))

import mm_budget as B  # noqa: E402


FP = "a" * 64


def micro(dollars: str) -> int:
    return B.fixed_dollars_to_micro_usd("test money", dollars)


def balance_record(
    balance_dollars: str,
    portfolio_value_cents: int = 0,
    *,
    updated_ts: int = 1_785_039_571,
) -> dict:
    balance_micro = micro(balance_dollars)
    return {
        "balance": balance_micro // B.MICRO_USD_PER_CENT,
        "balance_dollars": balance_dollars,
        "portfolio_value": portfolio_value_cents,
        "updated_ts": updated_ts,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": balance_dollars}
        ],
    }


class BalanceParsing(unittest.TestCase):
    def test_live_flat_shape_adds_balance_and_portfolio_exactly_once(self):
        snap = B.parse_balance_response(balance_record("19.8205", 0))
        self.assertEqual(snap.balance_micro_usd, 19_820_500)
        self.assertEqual(snap.portfolio_value_micro_usd, 0)
        self.assertEqual(snap.equity_micro_usd, 19_820_500)
        self.assertNotEqual(snap.equity_micro_usd, 0)           # PV-only bug
        self.assertNotEqual(snap.equity_micro_usd, 39_641_000)  # double-B bug

    def test_positions_value_is_added_in_integer_cents(self):
        record = balance_record("19.8205", 432)
        conservative = B.parse_balance_response(record)
        self.assertEqual(conservative.equity_micro_usd, 19_820_500)
        verified_positions_only = B.parse_balance_response(
            record, portfolio_value_is_positions_only=True
        )
        self.assertEqual(verified_positions_only.equity_micro_usd, 24_140_500)

    def test_precise_balance_must_floor_to_legacy_integer_cents(self):
        record = balance_record("19.8205")
        record["balance"] = 1983
        with self.assertRaisesRegex(B.BalanceSnapshotError, "disagree"):
            B.parse_balance_response(record)

    def test_breakdown_must_match_requested_exchange(self):
        record = balance_record("19.8205")
        record["balance_breakdown"][0]["balance"] = "19.8105"
        with self.assertRaisesRegex(B.BalanceSnapshotError, "breakdown"):
            B.parse_balance_response(record)

    def test_fixed_money_rejects_float_exponent_and_overprecision(self):
        for value in (19.8205, "1e1", "1.0000001", "-1.00", "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(B.BudgetError):
                    B.fixed_dollars_to_micro_usd("money", value)

    def test_updated_ts_is_freshness_not_a_unique_change_id(self):
        snap = B.parse_balance_response(
            balance_record("19.8205", updated_ts=1_785_039_433)
        )
        # Repeated timestamps inside the same second are valid.
        B.validate_snapshot_freshness(
            snap,
            request_started_s=1_785_039_433.2,
            response_finished_s=1_785_039_433.8,
            previous_updated_ts=1_785_039_433,
        )
        # updated_ts is last account mutation, so quiet age is not staleness.
        B.validate_snapshot_freshness(
            snap,
            request_started_s=1_785_039_440.0,
            response_finished_s=1_785_039_440.1,
        )
        with self.assertRaisesRegex(B.BalanceSnapshotError, "future"):
            B.validate_snapshot_freshness(
                snap,
                request_started_s=1_785_039_420.0,
                response_finished_s=1_785_039_420.1,
            )
        with self.assertRaisesRegex(B.BalanceSnapshotError, "regressed"):
            B.validate_snapshot_freshness(
                snap,
                request_started_s=1_785_039_433.2,
                response_finished_s=1_785_039_433.8,
                previous_updated_ts=1_785_039_434,
            )


class BudgetRecords(unittest.TestCase):
    def make_record(self, anchor: str = "20.157000") -> B.BudgetRecord:
        return B.new_budget_record(
            budget_id="btc15m-canary-1",
            account_fingerprint=FP,
            anchor_equity_micro_usd=micro(anchor),
            max_loss_micro_usd=micro("10.000000"),
            subaccount=0,
            exchange_index=0,
        )

    def test_record_floor_is_absolute_and_consistent(self):
        record = self.make_record()
        self.assertEqual(record.anchor_equity_micro_usd, 20_157_000)
        self.assertEqual(record.max_loss_micro_usd, 10_000_000)
        self.assertEqual(record.floor_equity_micro_usd, 10_157_000)

        bad = record.to_dict()
        bad["floor_equity_micro_usd"] += 1
        with self.assertRaisesRegex(B.BudgetRecordError, "floor"):
            B.validate_budget_record(bad)

    def test_record_rejects_account_generation_and_unknown_fields(self):
        record = self.make_record()
        with self.assertRaisesRegex(B.BudgetRecordError, "fingerprint mismatch"):
            B.validate_budget_record(
                record, expected_account_fingerprint="b" * 64
            )
        bad = record.to_dict()
        bad["generation"] = True
        with self.assertRaisesRegex(B.BudgetRecordError, "generation"):
            B.validate_budget_record(bad)
        bad = record.to_dict()
        bad["generation"] = 2
        with self.assertRaisesRegex(B.BudgetRecordError, "generation/state"):
            B.validate_budget_record(bad)
        bad = {
            **record.to_dict(),
            "latched": True,
            "latch_reason": "trip",
            "generation": 1,
        }
        with self.assertRaisesRegex(B.BudgetRecordError, "generation/state"):
            B.validate_budget_record(bad)
        bad = record.to_dict()
        bad["surprise"] = 1
        with self.assertRaisesRegex(B.BudgetRecordError, "unknown"):
            B.validate_budget_record(bad)

    def test_missing_record_never_auto_initializes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            with self.assertRaises(FileNotFoundError):
                B.load_budget(path, expected_account_fingerprint=FP)
            self.assertFalse(path.exists())

    def test_initialize_is_create_only_and_cannot_rebase(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            original = self.make_record()
            B.initialize_budget(path, original)
            raised_anchor = self.make_record(anchor="30.000000")
            with self.assertRaises(FileExistsError):
                B.initialize_budget(path, raised_anchor)
            loaded = B.load_budget(
                path,
                expected_account_fingerprint=FP,
                expected_subaccount=0,
                expected_exchange_index=0,
            )
            self.assertEqual(loaded, original)

    def test_latch_is_atomic_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            original = self.make_record()
            B.initialize_budget(path, original)
            latched = B.latch_budget(
                path, expected=original, reason="equity floor reached"
            )
            self.assertTrue(latched.latched)
            self.assertEqual(latched.generation, 2)
            self.assertEqual(
                latched.immutable_identity, original.immutable_identity
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                latched.to_dict(),
            )
            again = B.latch_budget(
                path, expected=original, reason="different retry reason"
            )
            self.assertEqual(again, latched)
            self.assertEqual(again.generation, 2)

    def test_latch_refuses_stale_or_changed_record(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            original = self.make_record()
            B.initialize_budget(path, original)
            tampered = original.to_dict()
            tampered["anchor_equity_micro_usd"] += 1
            tampered["floor_equity_micro_usd"] += 1
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                B.BudgetRecordError, "immutable budget identity"
            ):
                B.latch_budget(
                    path, expected=original, reason="must not overwrite"
                )


class SafeEquity(unittest.TestCase):
    def budget(self, floor: str = "10.000000") -> B.BudgetRecord:
        return B.new_budget_record(
            budget_id="risk-test",
            account_fingerprint=FP,
            anchor_equity_micro_usd=micro("20.000000"),
            max_loss_micro_usd=micro("20.000000") - micro(floor),
        )

    def test_safe_equity_counts_every_order_state_and_guarantee(self):
        snap = B.parse_balance_response(balance_record("20.000000", 200))
        resting = [
            B.OrderWorstCase.from_fixed(
                "resting",
                market="MKT",
                outcome="yes",
                quantity="1",
                cost="1.00",
                fee="0.10",
            )
        ]
        pending = [
            B.OrderWorstCase.from_fixed(
                "pending",
                market="MKT",
                outcome="yes",
                quantity="1",
                cost="2.00",
                fee="0.20",
            )
        ]
        unknown = [
            B.OrderWorstCase.from_fixed(
                "unknown",
                market="MKT",
                outcome="yes",
                quantity="1",
                cost="3.00",
                fee="0.30",
            )
        ]
        candidate = B.OrderWorstCase.from_fixed(
            "candidate",
            market="MKT",
            outcome="no",
            quantity="1",
            cost="4.00",
            fee="0.40",
        )
        out = B.compute_safe_equity(
            snap,
            other_guaranteed_payout_micro_usd=micro("1.00"),
            resting=resting,
            pending=pending,
            unknown=unknown,
            candidate=candidate,
        )
        self.assertEqual(out.mark_equity_micro_usd, micro("20.00"))
        self.assertEqual(out.mark_after_buffer_micro_usd, micro("19.99"))
        self.assertEqual(out.terminal_equity_micro_usd, micro("11.00"))
        self.assertEqual(out.safe_equity_micro_usd, micro("11.00"))
        self.assertEqual(out.order_count, 4)
        self.assertEqual(out.resting_cost_fee_micro_usd, micro("1.10"))
        self.assertEqual(out.pending_cost_fee_micro_usd, micro("2.20"))
        self.assertEqual(out.unknown_cost_fee_micro_usd, micro("3.30"))
        self.assertEqual(out.candidate_cost_fee_micro_usd, micro("4.40"))
        self.assertEqual(out.adverse_fill_loss_micro_usd, micro("10.00"))
        self.assertEqual(
            out.final_worst_settlement_micro_usd, -micro("9.00")
        )

    def test_safe_is_exact_minimum_of_mark_minus_one_cent_and_terminal(self):
        snap = B.parse_balance_response(balance_record("19.8205", 0))
        out = B.compute_safe_equity(snap)
        self.assertEqual(out.mark_after_buffer_micro_usd, micro("19.8105"))
        self.assertEqual(out.terminal_equity_micro_usd, micro("19.8205"))
        self.assertEqual(out.safe_equity_micro_usd, micro("19.8105"))

    def test_floor_boundary_buffer_and_latched_record(self):
        snap = B.parse_balance_response(balance_record("10.010000", 0))
        out = B.compute_safe_equity(snap)
        record = self.budget("10.000000")
        self.assertTrue(B.budget_allows(record, out))
        self.assertFalse(
            B.budget_allows(
                record,
                out,
                additional_buffer_micro_usd=B.MICRO_USD_PER_CENT,
            )
        )
        latched = B.validate_budget_record(
            {
                **record.to_dict(),
                "latched": True,
                "latch_reason": "trip",
                "generation": 2,
            }
        )
        self.assertFalse(B.budget_allows(latched, out))

    def test_duplicate_order_reference_fails_closed(self):
        snap = B.parse_balance_response(balance_record("20.00", 0))
        order = B.OrderWorstCase.from_fixed(
            "same",
            market="MKT",
            outcome="yes",
            quantity="1",
            cost="1",
        )
        with self.assertRaisesRegex(B.BudgetError, "unique"):
            B.compute_safe_equity(
                snap, resting=[order], unknown=[order]
            )

    def test_competing_hedges_cannot_double_claim_same_guarantee(self):
        snap = B.parse_balance_response(balance_record("15.837000", 432))
        position = B.MarketPosition.from_fixed_contracts(
            "MKT", yes="9"
        )
        hedge_a = B.OrderWorstCase.from_fixed(
            "resting-hedge",
            market="MKT",
            outcome="no",
            quantity="9",
            cost="4.860000",
            fee="0",
        )
        hedge_b = B.OrderWorstCase.from_fixed(
            "unknown-replacement",
            market="MKT",
            outcome="no",
            quantity="9",
            cost="4.860000",
            fee="0",
        )
        out = B.compute_safe_equity(
            snap,
            positions=[position],
            resting=[hedge_a],
            unknown=[hedge_b],
        )
        self.assertEqual(
            out.final_worst_settlement_micro_usd, -micro("0.72")
        )
        self.assertEqual(out.adverse_fill_loss_micro_usd, micro("0.72"))
        self.assertEqual(out.terminal_equity_micro_usd, micro("15.117"))

    def test_2026_07_26_pair_candidate_is_not_credited_before_fill(self):
        # Pre-canary equity was $20.1570.  After buying 9 YES @48c the
        # available balance was $15.8370.  Buying 9 NO @54c costs $4.86,
        # pays $0.1565 taker fee, and creates a guaranteed $9 pair payout.
        first_leg = B.parse_balance_response(
            balance_record("15.837000", 432)
        )
        position = B.MarketPosition.from_fixed_contracts(
            "KXBTC15M-INCIDENT", yes="9"
        )
        second_leg = B.OrderWorstCase.from_fixed(
            "external-flatten",
            market="KXBTC15M-INCIDENT",
            outcome="no",
            quantity="9",
            cost="4.860000",
            fee="0.156500",
        )
        out = B.compute_safe_equity(
            first_leg,
            positions=[position],
            candidate=second_leg,
        )
        # The NO candidate is not atomic with the existing YES position.  It
        # cannot be credited as a guaranteed pair before it actually fills.
        self.assertEqual(out.terminal_equity_micro_usd, micro("15.837000"))
        self.assertEqual(out.safe_equity_micro_usd, micro("15.827000"))
        self.assertEqual(
            micro("20.157000") - out.safe_equity_micro_usd,
            micro("4.330000"),
        )

    def test_non_atomic_complementary_orders_use_worst_fill_subset(self):
        snap = B.parse_balance_response(balance_record("10.400000", 0))
        yes = B.OrderWorstCase.from_fixed(
            "resting-yes",
            market="MKT",
            outcome="yes",
            quantity="1",
            cost="0.10",
        )
        no = B.OrderWorstCase.from_fixed(
            "candidate-no",
            market="MKT",
            outcome="no",
            quantity="1",
            cost="0.30",
        )
        out = B.compute_safe_equity(
            snap,
            resting=[yes],
            candidate=no,
        )
        # YES settles: only the losing NO fills (-30c).
        # NO settles: only the losing YES fills (-10c).  Worst is -30c.
        self.assertEqual(out.terminal_equity_micro_usd, micro("10.10"))
        self.assertEqual(out.safe_equity_micro_usd, micro("10.10"))
        self.assertEqual(out.adverse_fill_loss_micro_usd, micro("0.30"))
        self.assertEqual(
            out.final_worst_settlement_micro_usd, -micro("0.30")
        )

    def test_linear_formula_matches_exhaustive_fill_and_settlement_states(self):
        snap = B.parse_balance_response(balance_record("5.000000", 0))
        positions = [
            B.MarketPosition.from_fixed_contracts("M1", yes="2"),
            B.MarketPosition.from_fixed_contracts("M2", no="1"),
        ]
        orders = [
            B.OrderWorstCase.from_fixed(
                "m1-y", market="M1", outcome="yes", quantity="1",
                cost="0.40", fee="0.01",
            ),
            B.OrderWorstCase.from_fixed(
                "m1-n", market="M1", outcome="no", quantity="2",
                cost="1.10",
            ),
            B.OrderWorstCase.from_fixed(
                "m2-y", market="M2", outcome="yes", quantity="1",
                cost="0.20",
            ),
            B.OrderWorstCase.from_fixed(
                "m2-n", market="M2", outcome="no", quantity="1",
                cost="0.75", fee="0.02",
            ),
        ]
        other = micro("0.30")
        out = B.compute_safe_equity(
            snap,
            positions=positions,
            other_guaranteed_payout_micro_usd=other,
            resting=orders[:2],
            pending=orders[2:3],
            candidate=orders[3],
        )

        holdings = {
            position.market: {
                "yes": position.yes_microcontracts,
                "no": position.no_microcontracts,
            }
            for position in positions
        }
        exhaustive = []
        for states in itertools.product(("yes", "no"), repeat=2):
            settlement = dict(zip(("M1", "M2"), states))
            for fills in itertools.product((False, True), repeat=len(orders)):
                terminal = snap.balance_micro_usd + other
                terminal += sum(
                    sides[settlement[market]]
                    for market, sides in holdings.items()
                )
                for filled, order in zip(fills, orders):
                    if not filled:
                        continue
                    terminal -= (
                        order.cost_micro_usd + order.fee_micro_usd
                    )
                    if order.outcome == settlement[order.market]:
                        terminal += order.quantity_microcontracts
                exhaustive.append(terminal)
        self.assertEqual(out.terminal_equity_micro_usd, min(exhaustive))


if __name__ == "__main__":
    unittest.main(verbosity=2)
