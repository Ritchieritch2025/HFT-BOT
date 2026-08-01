"""Pure fixtures for the future mm_engine budget integration boundary."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MM_DIR = REPO / "tools" / "research" / "crypto_mm"
sys.path.insert(0, str(MM_DIR))

import mm_budget as B  # noqa: E402
import mm_budget_adapter as A  # noqa: E402


ACCOUNT = "kalshi-key-fixture"


def micro(dollars: str) -> int:
    return B.fixed_dollars_to_micro_usd("test dollars", dollars)


def balance(
    balance_dollars: str = "20.000000",
    portfolio_value: int = 0,
    *,
    updated_ts: int = 100,
) -> dict:
    balance_micro = micro(balance_dollars)
    return {
        "balance": balance_micro // B.MICRO_USD_PER_CENT,
        "balance_dollars": balance_dollars,
        "portfolio_value": portfolio_value,
        "updated_ts": updated_ts,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": balance_dollars}
        ],
    }


def position(ticker: str, signed: str) -> dict:
    return {
        "ticker": ticker,
        "position_fp": signed,
        "subaccount_number": 0,
        "exchange_index": 0,
        "market_exposure_dollars": "0.000000",
        "fees_paid_dollars": "0.000000",
    }


def snapshot(
    *,
    balance_response: dict | None = None,
    positions: list[dict] | None = None,
    generation_before: int = 7,
    generation_after: int = 7,
) -> A.SnapshotInputs:
    return A.SnapshotInputs(
        balance_response=balance_response or balance(),
        positions_response={"market_positions": positions or []},
        user_data_timestamp_response={
            "as_of_time": "1970-01-01T00:01:40.500000Z"
        },
        balance_request_started_s=100.2,
        balance_response_finished_s=100.8,
        user_data_timestamp_request_started_s=100.8,
        user_data_timestamp_response_finished_s=100.9,
        ledger_generation_before=generation_before,
        ledger_generation_after=generation_after,
        previous_balance_updated_ts=100,
        previous_user_data_as_of_s=100.0,
    )


def initialize(path: Path, *, floor: str = "10.000000") -> B.BudgetRecord:
    anchor = micro("20.000000")
    floor_micro = micro(floor)
    record = B.new_budget_record(
        budget_id="btc15m-budget-fixture",
        account_fingerprint=A.account_fingerprint(ACCOUNT),
        anchor_equity_micro_usd=anchor,
        max_loss_micro_usd=anchor - floor_micro,
    )
    B.initialize_budget(path, record)
    return record


def empty_ledger() -> dict:
    return {
        "local_net_pos": {},
        "orders": {},
        "pending_new": {},
        "unknown_orders": {},
        "flatten_pending": {},
    }


def callback(calls: list, kind: str, result: object = True):
    def invoke(reason: str):
        calls.append((kind, reason))
        return result

    return invoke


class AccountAndStartup(unittest.TestCase):
    def test_load_binds_account_and_missing_record_never_initializes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            with self.assertRaises(FileNotFoundError):
                A.load_bound_budget(path, account_identity=ACCOUNT)
            self.assertFalse(path.exists())

            original = initialize(path)
            self.assertEqual(
                A.load_bound_budget(path, account_identity=ACCOUNT),
                original,
            )
            with self.assertRaisesRegex(
                B.BudgetRecordError, "fingerprint mismatch"
            ):
                A.load_bound_budget(path, account_identity="different-key")

    def test_startup_loads_existing_anchor_without_rebase(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            original = initialize(path)
            before = path.read_bytes()
            guard = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls, "cancel"),
                halt=callback(calls, "halt"),
            )
            result = guard.startup_check(
                snapshot_inputs=snapshot(),
                **empty_ledger(),
            )
            self.assertTrue(result.allowed)
            self.assertEqual(guard.record, original)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(calls, [])

    def test_user_data_timestamp_is_zoned_monotonic_and_not_future(self):
        as_of = A.parse_user_data_timestamp(
            {"as_of_time": "1970-01-01T00:01:40.500000Z"}
        )
        self.assertEqual(as_of, 100.5)
        A.validate_user_data_timestamp(
            as_of,
            request_started_s=100.6,
            response_finished_s=100.8,
            previous_as_of_s=100.0,
        )
        with self.assertRaisesRegex(A.AdapterError, "future"):
            A.validate_user_data_timestamp(
                102.0,
                request_started_s=100.6,
                response_finished_s=100.8,
            )
        with self.assertRaisesRegex(A.AdapterError, "regressed"):
            A.validate_user_data_timestamp(
                as_of,
                request_started_s=100.6,
                response_finished_s=100.8,
                previous_as_of_s=100.6,
            )
        with self.assertRaisesRegex(A.AdapterError, "timezone"):
            A.parse_user_data_timestamp(
                {"as_of_time": "1970-01-01T00:01:40.500000"}
            )


class FieldMapping(unittest.TestCase):
    def test_resting_pending_and_cancel_unknown_are_counted_once(self):
        risks = A.build_ledger_risks(
            orders={
                ("MKT", "bid"): {
                    "id": "resting-y",
                    "px": 0.40,
                    "qty": 2.0,
                },
                ("MKT", "ask_no"): {
                    "id": "cancel-unknown-n",
                    "px": 0.60,
                    "qty": 2.0,
                },
            },
            pending_new={
                ("OTHER", "bid"): {
                    "client_order_id": "pending-y",
                    "px": 0.25,
                    "qty": 1.0,
                }
            },
            unknown_orders={
                "cancel-unknown-n": {
                    "ticker": "MKT",
                    "side": "ask_no",
                }
            },
            flatten_pending={},
        )
        self.assertEqual(
            [risk.reference for risk in risks.resting],
            ["order:resting-y"],
        )
        self.assertEqual(
            [risk.reference for risk in risks.pending],
            ["client:pending-y"],
        )
        self.assertEqual(
            [risk.reference for risk in risks.unknown],
            ["order:cancel-unknown-n"],
        )
        self.assertEqual(
            sum(
                len(group)
                for group in (risks.resting, risks.pending, risks.unknown)
            ),
            3,
        )

    def test_unbacked_unknown_and_current_ioc_shape_fail_closed(self):
        with self.assertRaisesRegex(A.LedgerSchemaError, "no retained"):
            A.build_ledger_risks(
                orders={},
                pending_new={},
                unknown_orders={"lost": {"ticker": "MKT", "side": "bid"}},
                flatten_pending={},
            )
        with self.assertRaisesRegex(
            A.LedgerSchemaError, "flatten_pending reservation missing"
        ):
            A.build_ledger_risks(
                orders={},
                pending_new={},
                unknown_orders={},
                flatten_pending={
                    ("MKT", "bid"): {
                        "client_order_id": "ioc-1",
                        "qty": 2.0,
                    }
                },
            )

    def test_future_ioc_reservation_maps_opposite_orphan_side(self):
        risks = A.build_ledger_risks(
            orders={},
            pending_new={},
            unknown_orders={},
            flatten_pending={
                ("MKT", "bid"): {
                    "client_order_id": "ioc-no",
                    "risk_px": "0.54",
                    "remaining_risk_qty": "2",
                    "fee_reserve": "0.04",
                }
            },
        )
        self.assertEqual(len(risks.unknown), 1)
        self.assertEqual(risks.unknown[0].outcome, "no")
        self.assertEqual(risks.unknown[0].cost_micro_usd, micro("1.08"))
        self.assertEqual(risks.unknown[0].fee_micro_usd, micro("0.04"))

    def test_same_raw_order_across_unknown_and_ioc_counts_once(self):
        risks = A.build_ledger_risks(
            orders={
                ("MKT", "ask_no"): {
                    "id": "same-id",
                    "px": "0.10",
                    "qty": "1",
                }
            },
            pending_new={},
            unknown_orders={
                "same-id": {"ticker": "MKT", "side": "ask_no"}
            },
            flatten_pending={
                ("MKT", "bid"): {
                    "order_id": "same-id",
                    "risk_px": "0.10",
                    "remaining_risk_qty": "1",
                    "fee_reserve": "0",
                }
            },
        )
        self.assertEqual(len(risks.unknown), 1)
        snap = B.parse_balance_response(balance("0.000000"))
        out = B.compute_safe_equity(
            snap,
            positions=[
                B.MarketPosition.from_fixed_contracts("MKT", yes="2")
            ],
            unknown=risks.unknown,
        )
        self.assertEqual(out.final_worst_settlement_micro_usd, micro("0"))
        self.assertEqual(out.terminal_equity_micro_usd, micro("0"))

        conflicting = {
            ("MKT", "bid"): {
                "order_id": "same-id",
                "risk_px": "0.10",
                "remaining_risk_qty": "2",
                "fee_reserve": "0",
            }
        }
        with self.assertRaisesRegex(
            A.LedgerSchemaError, "conflicting economic"
        ):
            A.build_ledger_risks(
                orders={
                    ("MKT", "ask_no"): {
                        "id": "same-id",
                        "px": "0.10",
                        "qty": "1",
                    }
                },
                pending_new={},
                unknown_orders={
                    "same-id": {"ticker": "MKT", "side": "ask_no"}
                },
                flatten_pending=conflicting,
            )

    def test_position_fp_sign_and_local_ledger_must_agree(self):
        positions = A.parse_positions_response(
            {"market_positions": [position("MKT", "-20.00")]}
        )
        self.assertEqual(positions[0].yes_microcontracts, 0)
        self.assertEqual(
            positions[0].no_microcontracts,
            B.fixed_contracts_to_microcontracts("quantity", "20"),
        )
        A.validate_local_positions(
            {"MKT": {"y": 0.0, "n": 20.0}},
            positions,
        )
        with self.assertRaisesRegex(A.AdapterError, "position mismatch"):
            A.validate_local_positions(
                {"MKT": {"y": 20.0, "n": 0.0}},
                positions,
            )


class PreflightAndMonitor(unittest.TestCase):
    def make_guard(self, path: Path, calls: list, floor: str = "10.000000"):
        initialize(path, floor=floor)
        return A.BudgetGuard(
            budget_path=path,
            account_identity=ACCOUNT,
            cancel_all=callback(calls, "cancel"),
            halt=callback(calls, "halt"),
        )

    def test_candidate_only_breach_is_denied_without_latching(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            guard = self.make_guard(path, calls)
            candidate = A.CandidateIntent(
                reference="candidate-large",
                ticker="MKT",
                engine_side="bid",
                risk_px="0.75",
                quantity="20",
            )
            result = guard.preflight(
                candidate=candidate,
                snapshot_inputs=snapshot(),
                **empty_ledger(),
            )
            self.assertTrue(
                result.baseline.safe_equity_micro_usd
                >= result.record.floor_equity_micro_usd
            )
            self.assertFalse(result.allowed)
            self.assertFalse(result.trip_required)
            self.assertFalse(B.load_budget(path).latched)
            self.assertEqual(calls, [])

    def test_candidate_identity_cannot_retry_an_existing_reservation(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            guard = self.make_guard(path, calls)
            with self.assertRaisesRegex(
                A.LedgerSchemaError, "already reserved"
            ):
                guard.preflight(
                    candidate=A.CandidateIntent(
                        reference="same-client-id",
                        ticker="MKT",
                        engine_side="bid",
                        risk_px="0.40",
                        quantity="1",
                    ),
                    snapshot_inputs=snapshot(),
                    local_net_pos={},
                    orders={},
                    pending_new={
                        ("MKT", "bid"): {
                            "client_order_id": "same-client-id",
                            "px": "0.40",
                            "qty": "1",
                        }
                    },
                    unknown_orders={},
                    flatten_pending={},
                )
            self.assertTrue(B.load_budget(path).latched)
            self.assertEqual([kind for kind, _ in calls], ["cancel", "halt"])

    def test_pair_closing_candidate_uses_joint_terminal_guarantee(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            guard = self.make_guard(path, calls)
            result = guard.preflight(
                candidate=A.CandidateIntent(
                    reference="candidate-no-hedge",
                    ticker="MKT",
                    engine_side="ask_no",
                    risk_px="0.54",
                    quantity="9",
                    fee_reserve="0.1565",
                ),
                snapshot_inputs=snapshot(
                    balance_response=balance("15.837000", 432),
                    positions=[position("MKT", "9.00")],
                ),
                local_net_pos={"MKT": {"y": 9.0, "n": 0.0}},
                orders={},
                pending_new={},
                unknown_orders={},
                flatten_pending={},
            )
            self.assertTrue(result.allowed)
            self.assertEqual(
                result.projected.terminal_equity_micro_usd,
                micro("15.837000"),
            )
            self.assertEqual(
                result.projected.final_worst_settlement_micro_usd,
                micro("0"),
            )
            self.assertEqual(calls, [])

    def test_generation_change_is_blind_and_trips(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            guard = self.make_guard(path, calls)
            with self.assertRaisesRegex(A.SnapshotRace, "changed"):
                guard.preflight(
                    candidate=A.CandidateIntent(
                        reference="candidate",
                        ticker="MKT",
                        engine_side="bid",
                        risk_px="0.40",
                        quantity="1",
                    ),
                    snapshot_inputs=snapshot(
                        generation_before=7,
                        generation_after=8,
                    ),
                    **empty_ledger(),
                )
            self.assertTrue(B.load_budget(path).latched)
            self.assertEqual([kind for kind, _ in calls], ["cancel", "halt"])

    def test_cancel_false_keeps_trip_pending_and_retries(self):
        calls = []
        cancel_results = iter((False, True))

        def cancel(reason):
            calls.append(("cancel", reason))
            return next(cancel_results)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            initialize(path)
            guard = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=cancel,
                halt=callback(calls, "halt"),
            )
            kwargs = {
                "snapshot_inputs": snapshot(
                    balance_response=balance("10.000000")
                ),
                **empty_ledger(),
            }
            guard.monitor_once(**kwargs)
            first = guard.trip_receipt
            self.assertIsNotNone(first)
            self.assertFalse(first.cancel_complete)
            self.assertFalse(first.complete)
            self.assertIn("returned False", first.cancel_error)

            guard.monitor_once(**kwargs)
            second = guard.trip_receipt
            self.assertTrue(second.cancel_complete)
            self.assertTrue(second.complete)
            self.assertEqual(
                [kind for kind, _ in calls].count("cancel"),
                2,
            )
            self.assertEqual(B.load_budget(path).generation, 2)

    def test_latch_write_failure_is_pending_halts_and_retries(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            initialize(path)
            guard = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls, "cancel"),
                halt=callback(calls, "halt"),
            )
            kwargs = {
                "snapshot_inputs": snapshot(
                    balance_response=balance("10.000000")
                ),
                **empty_ledger(),
            }
            real_latch = A.budget.latch_budget
            attempts = 0

            def flaky_latch(*args, **kw):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("transient fsync failure")
                return real_latch(*args, **kw)

            with mock.patch.object(
                A.budget, "latch_budget", side_effect=flaky_latch
            ):
                guard.monitor_once(**kwargs)
                first = guard.trip_receipt
                self.assertFalse(first.durable_latched)
                self.assertFalse(first.complete)
                self.assertIn("OSError", first.latch_error)
                self.assertTrue(first.halt_confirmed)
                self.assertFalse(B.load_budget(path).latched)
                self.assertTrue(
                    path.with_name(path.name + ".trip-pending").exists()
                )

                with self.assertRaisesRegex(
                    A.AdapterError, "trip-pending marker"
                ):
                    guard.monitor_once(**kwargs)
                second = guard.trip_receipt
                self.assertTrue(second.durable_latched)
                self.assertFalse(second.complete)
                self.assertTrue(B.load_budget(path).latched)
                self.assertEqual(attempts, 2)
                guard.monitor_once(**kwargs)
                self.assertTrue(guard.trip_receipt.complete)
            self.assertGreaterEqual(
                [kind for kind, _ in calls].count("halt"),
                1,
            )

    def test_trip_pending_marker_blocks_restart_after_latch_failure(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            initialize(path)
            first = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls, "cancel"),
                halt=callback(calls, "halt"),
            )
            unsafe = {
                "snapshot_inputs": snapshot(
                    balance_response=balance("10.000000")
                ),
                **empty_ledger(),
            }
            with mock.patch.object(
                A.budget,
                "latch_budget",
                side_effect=OSError("disk unavailable"),
            ):
                first.monitor_once(**unsafe)
            self.assertFalse(B.load_budget(path).latched)

            restart_calls = []
            restarted = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(restart_calls, "cancel"),
                halt=callback(restart_calls, "halt"),
            )
            with mock.patch.object(
                A.budget,
                "latch_budget",
                side_effect=OSError("still unavailable"),
            ):
                with self.assertRaisesRegex(
                    A.AdapterError, "trip-pending marker"
                ):
                    restarted.startup_check(
                        snapshot_inputs=snapshot(),
                        **empty_ledger(),
                    )
            self.assertEqual(
                [kind for kind, _ in restart_calls],
                ["cancel", "halt"],
            )
            self.assertFalse(restarted.trip_receipt.complete)

    def test_second_guard_reloads_latch_before_preflight(self):
        calls_a = []
        calls_b = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            initialize(path)
            guard_a = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls_a, "cancel"),
                halt=callback(calls_a, "halt"),
            )
            guard_b = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls_b, "cancel"),
                halt=callback(calls_b, "halt"),
            )
            guard_a.monitor_once(
                snapshot_inputs=snapshot(
                    balance_response=balance("10.000000")
                ),
                **empty_ledger(),
            )
            self.assertTrue(B.load_budget(path).latched)

            result = guard_b.preflight(
                candidate=A.CandidateIntent(
                    reference="small-candidate",
                    ticker="MKT",
                    engine_side="bid",
                    risk_px="0.40",
                    quantity="1",
                ),
                snapshot_inputs=snapshot(),
                **empty_ledger(),
            )
            self.assertFalse(result.allowed)
            self.assertTrue(result.trip_required)
            self.assertTrue(guard_b.record.latched)
            self.assertEqual(guard_b.record.generation, 2)
            self.assertEqual(
                [kind for kind, _ in calls_b],
                ["cancel", "halt"],
            )

    def test_monitor_latches_cancels_halts_once_and_restart_stays_blocked(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            original = initialize(path, floor="10.000000")
            guard = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(calls, "cancel"),
                halt=callback(calls, "halt"),
            )
            kwargs = {
                "snapshot_inputs": snapshot(
                    balance_response=balance("10.000000")
                ),
                **empty_ledger(),
            }
            first = guard.monitor_once(**kwargs)
            second = guard.monitor_once(**kwargs)
            self.assertFalse(first.allowed)
            self.assertFalse(second.allowed)
            self.assertEqual([kind for kind, _ in calls], ["cancel", "halt"])

            persisted = B.load_budget(path)
            self.assertTrue(persisted.latched)
            self.assertEqual(persisted.generation, 2)
            self.assertEqual(
                persisted.immutable_identity,
                original.immutable_identity,
            )

            restart_calls = []
            restarted = A.BudgetGuard(
                budget_path=path,
                account_identity=ACCOUNT,
                cancel_all=callback(restart_calls, "cancel"),
                halt=callback(restart_calls, "halt"),
            )
            result = restarted.startup_check(**kwargs)
            self.assertFalse(result.allowed)
            self.assertTrue(restarted.record.latched)
            self.assertEqual(
                [kind for kind, _ in restart_calls],
                ["cancel", "halt"],
            )
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                on_disk["anchor_equity_micro_usd"],
                original.anchor_equity_micro_usd,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_position_rows_may_omit_account_scope_fields():
    """Live wire shape (2026-07-27): /portfolio/positions rows carry no
    subaccount_number/exchange_index.  Absent is accepted (the request is
    key-scoped); a PRESENT mismatching value must still fail closed."""
    ok = A.parse_positions_response(
        {"market_positions": [{"ticker": "MKT", "position_fp": "-1.00"}]},
        expected_subaccount=0,
        expected_exchange_index=0,
    )
    assert len(ok) == 1
    assert ok[0].no_microcontracts > 0

    try:
        A.parse_positions_response(
            {"market_positions": [{"ticker": "MKT", "position_fp": "1.00",
                                   "subaccount_number": 7}]},
            expected_subaccount=0,
            expected_exchange_index=0,
        )
    except A.AdapterError:
        pass
    else:
        raise AssertionError("mismatching subaccount_number must fail closed")
