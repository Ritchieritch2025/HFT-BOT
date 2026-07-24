#!/usr/bin/env python3
"""Adversarial tests for the strict causal-latency producer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_TOOLS = ROOT / "tools" / "research"
if str(RESEARCH_TOOLS) not in sys.path:
    sys.path.insert(0, str(RESEARCH_TOOLS))

from pnl_spine.contracts import canonical_json_bytes  # noqa: E402
from pnl_spine.latency_evidence import (  # noqa: E402
    LATENCY_AGGREGATE_READY,
    load_latency_evidence_inventory,
)
from pnl_spine.measured_latency_producer import (  # noqa: E402
    MeasuredLatencyError,
    build_receipts,
    load_private_trace,
    write_canonical_bundle,
)
from pnl_spine.preflight import _validate_latency  # noqa: E402


ENV_SHA = "1" * 64
CODE_SHA = "2" * 64
CONFIG_SHA = "3" * 64
AUTH_SHA = "4" * 64
IOC_AUTH_SHA = "8" * 64
CLOCK_SHA = "5" * 64
HOST_SHA = "6" * 64
TICKER_SHA = "7" * 64
RAW_ORDER_ID = "raw-order-id-must-never-leak"


def reconciliation(path: str) -> dict[str, object]:
    if path == "PLACE":
        return {
            "requested_quantity_e4": 10_000,
            "filled_quantity_e4": 0,
            "canceled_quantity_e4": 0,
            "remaining_quantity_e4": 10_000,
            "position_before_e4": 0,
            "position_after_e4": 0,
            "reconciled": True,
            "reduce_only": False,
            "order_status": "RESTING",
        }
    if path == "CANCEL":
        return {
            "requested_quantity_e4": 10_000,
            "filled_quantity_e4": 0,
            "canceled_quantity_e4": 10_000,
            "remaining_quantity_e4": 0,
            "position_before_e4": 0,
            "position_after_e4": 0,
            "reconciled": True,
            "reduce_only": False,
            "order_status": "CANCELED",
        }
    return {
        "requested_quantity_e4": 20_000,
        "filled_quantity_e4": 20_000,
        "canceled_quantity_e4": 0,
        "remaining_quantity_e4": 0,
        "position_before_e4": -20_000,
        "position_after_e4": 0,
        "reconciled": True,
        "reduce_only": True,
        "order_status": "EXECUTED",
    }


def sample(path: str, start: int) -> dict[str, object]:
    semantics = {
        "PLACE": "NEW_ORDER_PLACE",
        "CANCEL": "RESTING_ORDER_CANCEL",
        "IOC_EXIT": "IOC_POSITION_REDUCING_EXIT",
    }
    return {
        "path": path,
        "action_semantics": semantics[path],
        "clock_quality_receipt_sha256": CLOCK_SHA,
        "decision_ns": start,
        "sent_ns": start + 10,
        "acknowledged_ns": start + 20,
        "effective_ns": start + 30,
        "order_ref_sha256": hashlib.sha256(
            (
                RAW_ORDER_ID
                + ("PLACE" if path == "CANCEL" else path)
            ).encode()
        ).hexdigest(),
        "request_sha256": hashlib.sha256(
            ("request-" + path).encode()
        ).hexdigest(),
        "response_sha256": hashlib.sha256(
            ("response-" + path).encode()
        ).hexdigest(),
        "source_event_sha256": hashlib.sha256(
            ("event-" + path).encode()
        ).hexdigest(),
        "http_status": 201 if path != "CANCEL" else 200,
        "live_authority_sha256": (
            IOC_AUTH_SHA if path == "IOC_EXIT" else AUTH_SHA
        ),
        "environment_fingerprint_sha256": ENV_SHA,
        "execution_host_fingerprint_sha256": HOST_SHA,
        "matching_engine_ts_ms": 1_800_000_000_000 + start,
        "average_fee_paid_e6": 12_300 if path == "IOC_EXIT" else None,
        "average_fill_price_e4": 5_500 if path == "IOC_EXIT" else None,
        "book_side": "BID",
        "request_reduce_only": path == "IOC_EXIT",
        "requested_price_e4": 5_500 if path == "IOC_EXIT" else 100,
        "subaccount": 0,
        "ticker_sha256": TICKER_SHA,
        "time_in_force": (
            "IMMEDIATE_OR_CANCEL"
            if path == "IOC_EXIT"
            else "GOOD_TILL_CANCELED"
        ),
        "fill_reconciliation": reconciliation(path),
    }


def valid_trace() -> dict[str, object]:
    return {
        "schema_version": "pnl-spine-private-causal-latency-trace-v1",
        "receipt_id": "three-path-real-order-test",
        "measurement_mode": "REAL_ORDER_MEASURED",
        "clock_id": "CLOCK_MONOTONIC_RAW:prod-exec-1",
        "measured_on": "prod-exec-1",
        "created_at_ns": 10_000,
        "created_at_wall_utc_ms": 1_800_000_000_999,
        "clock_quality_receipt_sha256": CLOCK_SHA,
        "environment_fingerprint_sha256": ENV_SHA,
        "execution_host_fingerprint_sha256": HOST_SHA,
        "producer_code_sha256": CODE_SHA,
        "producer_config_sha256": CONFIG_SHA,
        "samples": [
            sample("PLACE", 100),
            sample("CANCEL", 200),
            sample("IOC_EXIT", 300),
        ],
    }


class TraceFixture:
    def __init__(self, payload: object, *, raw: bytes | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / "private.json"
        self.raw = canonical_json_bytes(payload) if raw is None else raw
        self.path.write_bytes(self.raw)
        self.sha256 = hashlib.sha256(self.raw).hexdigest()

    def close(self) -> None:
        self.temp.cleanup()


def load(payload: object) -> tuple[dict[str, object], str]:
    fixture = TraceFixture(payload)
    try:
        return load_private_trace(
            fixture.path,
            expected_source_sha256=fixture.sha256,
            expected_environment_fingerprint_sha256=ENV_SHA,
            expected_producer_code_sha256=CODE_SHA,
            expected_producer_config_sha256=CONFIG_SHA,
            expected_place_cancel_live_authority_sha256=AUTH_SHA,
            expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
            expected_clock_quality_receipt_sha256=CLOCK_SHA,
            expected_execution_host_fingerprint_sha256=HOST_SHA,
        )
    finally:
        fixture.close()


class MeasuredLatencyProducerTests(unittest.TestCase):
    def test_complete_trace_produces_runner_and_audit_ready_receipts(
        self,
    ) -> None:
        private_trace, source_sha = load(valid_trace())
        measured, inventory, aggregate = build_receipts(
            private_trace, source_sha256=source_sha
        )
        blockers, facts = _validate_latency(measured)
        self.assertEqual([], blockers)
        self.assertEqual(
            {"PLACE": 1, "CANCEL": 1, "IOC_EXIT": 1},
            facts["latency_sample_counts"],
        )
        self.assertEqual(LATENCY_AGGREGATE_READY, aggregate["state"])
        public_bytes = canonical_json_bytes(
            {"measured": measured, "inventory": inventory, "audit": aggregate}
        )
        self.assertNotIn(RAW_ORDER_ID.encode(), public_bytes)
        self.assertNotIn(b"request-PLACE", public_bytes)

    def test_emitted_inventory_is_accepted_by_existing_loader(self) -> None:
        private_trace, source_sha = load(valid_trace())
        _, inventory, expected_audit = build_receipts(
            private_trace, source_sha256=source_sha
        )
        fixture = TraceFixture(inventory)
        self.addCleanup(fixture.close)
        typed = load_latency_evidence_inventory(
            fixture.path, expected_inventory_sha256=fixture.sha256
        )
        self.assertEqual(3, len(typed.trace_candidates))
        self.assertEqual(
            expected_audit["source_inventory_sha256"], fixture.sha256
        )

    def test_bundle_reserves_every_path_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            existing = root / "inventory.json"
            existing.write_text("operator-owned", encoding="utf-8")
            measured = root / "measured.json"
            aggregate = root / "aggregate.json"
            with self.assertRaises(FileExistsError):
                write_canonical_bundle(
                    {
                        "aggregate": (aggregate, {"value": 1}),
                        "inventory": (existing, {"value": 2}),
                        "measured": (measured, {"value": 3}),
                    }
                )
            self.assertFalse(aggregate.exists())
            self.assertFalse(measured.exists())
            self.assertEqual(
                "operator-owned", existing.read_text(encoding="utf-8")
            )

    def test_bundle_is_read_only_and_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "first.json"
            second = root / "second.json"
            hashes = write_canonical_bundle(
                {
                    "first": (first, {"value": 1}),
                    "second": (second, {"value": 2}),
                }
            )
            self.assertEqual(0o444, first.stat().st_mode & 0o777)
            self.assertEqual(0o444, second.stat().st_mode & 0o777)
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashes["first"],
            )

            real_parent = root / "real"
            real_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(
                MeasuredLatencyError, "symlinks"
            ):
                write_canonical_bundle(
                    {
                        "linked": (
                            linked_parent / "receipt.json",
                            {"value": 3},
                        )
                    }
                )
            self.assertFalse((real_parent / "receipt.json").exists())

    def test_production_publication_requires_root_boundary(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("non-root boundary test requires non-root runner")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "receipt.json"
            with self.assertRaisesRegex(
                MeasuredLatencyError, "must run as root"
            ):
                write_canonical_bundle(
                    {"receipt": (output, {"value": 1})},
                    require_root_parent=True,
                )
            self.assertFalse(output.exists())

    def test_missing_real_field_is_rejected(self) -> None:
        trace = valid_trace()
        del trace["samples"][0]["request_sha256"]
        with self.assertRaisesRegex(MeasuredLatencyError, "missing="):
            load(trace)

    def test_noncausal_timestamps_are_rejected(self) -> None:
        trace = valid_trace()
        trace["samples"][0]["acknowledged_ns"] = 99
        with self.assertRaisesRegex(MeasuredLatencyError, "not causal"):
            load(trace)

    def test_path_action_mismatch_is_rejected(self) -> None:
        trace = valid_trace()
        trace["samples"][0]["action_semantics"] = (
            "IOC_POSITION_REDUCING_EXIT"
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "does not match PLACE"
        ):
            load(trace)

    def test_place_partial_fill_cannot_be_called_success(self) -> None:
        trace = valid_trace()
        reconciliation = trace["samples"][0]["fill_reconciliation"]
        reconciliation["filled_quantity_e4"] = 1
        reconciliation["remaining_quantity_e4"] = 9_999
        reconciliation["position_after_e4"] = 1
        with self.assertRaisesRegex(MeasuredLatencyError, "partially filled"):
            load(trace)

    def test_cancel_with_remaining_quantity_is_rejected(self) -> None:
        trace = valid_trace()
        reconciliation = trace["samples"][1]["fill_reconciliation"]
        reconciliation["canceled_quantity_e4"] = 9_000
        reconciliation["remaining_quantity_e4"] = 1_000
        with self.assertRaisesRegex(MeasuredLatencyError, "full unfilled"):
            load(trace)

    def test_position_drift_on_cancel_is_rejected(self) -> None:
        trace = valid_trace()
        trace["samples"][1]["fill_reconciliation"][
            "position_after_e4"
        ] = -10_000
        with self.assertRaisesRegex(MeasuredLatencyError, "changed position"):
            load(trace)

    def test_matching_engine_timestamp_and_fee_semantics_are_strict(
        self,
    ) -> None:
        bad_timestamp = valid_trace()
        bad_timestamp["samples"][0]["matching_engine_ts_ms"] = 0
        with self.assertRaisesRegex(
            MeasuredLatencyError, "matching_engine_ts_ms"
        ):
            load(bad_timestamp)

        fee_on_zero_fill = valid_trace()
        fee_on_zero_fill["samples"][0]["average_fee_paid_e6"] = 1
        with self.assertRaisesRegex(
            MeasuredLatencyError, "must be null with zero fills"
        ):
            load(fee_on_zero_fill)

        missing_ioc_fee = valid_trace()
        missing_ioc_fee["samples"][2]["average_fee_paid_e6"] = None
        with self.assertRaisesRegex(
            MeasuredLatencyError, "average_fee_paid_e6"
        ):
            load(missing_ioc_fee)

    def test_unrelated_place_cancel_samples_cannot_be_spliced(self) -> None:
        different_order = valid_trace()
        different_order["samples"][1]["order_ref_sha256"] = "8" * 64
        with self.assertRaisesRegex(
            MeasuredLatencyError, "same order_ref lifecycle"
        ):
            load(different_order)

        cancel_before_place_effective = valid_trace()
        cancel = cancel_before_place_effective["samples"][1]
        cancel["decision_ns"] = 120
        cancel["sent_ns"] = 121
        cancel["acknowledged_ns"] = 122
        cancel["effective_ns"] = 123
        with self.assertRaisesRegex(
            MeasuredLatencyError, "precedes PLACE effective"
        ):
            load(cancel_before_place_effective)

        mismatched_ticker = valid_trace()
        mismatched_ticker["samples"][1]["ticker_sha256"] = "8" * 64
        with self.assertRaisesRegex(
            MeasuredLatencyError, "ticker_sha256 differs"
        ):
            load(mismatched_ticker)

        reused_ioc_order = valid_trace()
        reused_ioc_order["samples"][2]["order_ref_sha256"] = (
            reused_ioc_order["samples"][0]["order_ref_sha256"]
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "separate order lifecycle"
        ):
            load(reused_ioc_order)

    def test_ioc_book_side_must_reduce_signed_position(self) -> None:
        wrong_negative_exit = valid_trace()
        wrong_negative_exit["samples"][2]["book_side"] = "ASK"
        with self.assertRaisesRegex(
            MeasuredLatencyError, "do not reduce"
        ):
            load(wrong_negative_exit)

        wrong_positive_exit = valid_trace()
        wrong_positive_exit["samples"][2]["fill_reconciliation"][
            "position_before_e4"
        ] = 20_000
        with self.assertRaisesRegex(
            MeasuredLatencyError, "do not reduce"
        ):
            load(wrong_positive_exit)

    def test_wall_clock_receipt_must_follow_engine_timestamps(self) -> None:
        trace = valid_trace()
        trace["created_at_wall_utc_ms"] = 1
        with self.assertRaisesRegex(
            MeasuredLatencyError, "precedes matching-engine"
        ):
            load(trace)

    def test_each_sample_must_match_current_live_authority_pin(self) -> None:
        trace = valid_trace()
        trace["samples"][1]["live_authority_sha256"] = "8" * 64
        with self.assertRaisesRegex(
            MeasuredLatencyError, "live_authority_sha256"
        ):
            load(trace)

        wrong_host = valid_trace()
        wrong_host["samples"][2][
            "execution_host_fingerprint_sha256"
        ] = "8" * 64
        with self.assertRaisesRegex(
            MeasuredLatencyError, "differs from trace execution context"
        ):
            load(wrong_host)

    def test_ioc_must_be_reduce_only_full_fill_and_flatten(self) -> None:
        mutations = (
            ("reduce_only", False),
            ("filled_quantity_e4", 10_000),
            ("remaining_quantity_e4", 10_000),
            ("position_after_e4", 10_000),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                trace = valid_trace()
                reconciliation = trace["samples"][2]["fill_reconciliation"]
                reconciliation[field] = value
                if field == "filled_quantity_e4":
                    reconciliation["canceled_quantity_e4"] = 10_000
                if field == "position_after_e4":
                    reconciliation["filled_quantity_e4"] = 10_000
                    reconciliation["canceled_quantity_e4"] = 10_000
                with self.assertRaises(MeasuredLatencyError):
                    load(trace)

    def test_external_source_and_build_pins_are_mandatory(self) -> None:
        trace = valid_trace()
        fixture = TraceFixture(trace)
        self.addCleanup(fixture.close)
        arguments = {
            "expected_source_sha256": fixture.sha256,
            "expected_environment_fingerprint_sha256": ENV_SHA,
            "expected_producer_code_sha256": CODE_SHA,
            "expected_producer_config_sha256": CONFIG_SHA,
            "expected_place_cancel_live_authority_sha256": AUTH_SHA,
            "expected_ioc_exit_live_authority_sha256": IOC_AUTH_SHA,
            "expected_clock_quality_receipt_sha256": CLOCK_SHA,
            "expected_execution_host_fingerprint_sha256": HOST_SHA,
        }
        for field in tuple(arguments):
            with self.subTest(field=field):
                bad = dict(arguments)
                bad[field] = "f" * 64
                with self.assertRaisesRegex(
                    MeasuredLatencyError, "external pin"
                ):
                    load_private_trace(fixture.path, **bad)

    def test_noncanonical_duplicate_float_unknown_and_symlink_refused(
        self,
    ) -> None:
        trace = valid_trace()
        pretty = json.dumps(trace, indent=2).encode()
        pretty_fixture = TraceFixture(trace, raw=pretty)
        self.addCleanup(pretty_fixture.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "not exact canonical"):
            load_private_trace(
                pretty_fixture.path,
                expected_source_sha256=pretty_fixture.sha256,
                expected_environment_fingerprint_sha256=ENV_SHA,
                expected_producer_code_sha256=CODE_SHA,
                expected_producer_config_sha256=CONFIG_SHA,
                expected_place_cancel_live_authority_sha256=AUTH_SHA,
                expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
                expected_clock_quality_receipt_sha256=CLOCK_SHA,
                expected_execution_host_fingerprint_sha256=HOST_SHA,
            )

        duplicate = b'{"schema_version":"a","schema_version":"b"}'
        duplicate_fixture = TraceFixture(trace, raw=duplicate)
        self.addCleanup(duplicate_fixture.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "duplicate JSON key"):
            load_private_trace(
                duplicate_fixture.path,
                expected_source_sha256=duplicate_fixture.sha256,
                expected_environment_fingerprint_sha256=ENV_SHA,
                expected_producer_code_sha256=CODE_SHA,
                expected_producer_config_sha256=CONFIG_SHA,
                expected_place_cancel_live_authority_sha256=AUTH_SHA,
                expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
                expected_clock_quality_receipt_sha256=CLOCK_SHA,
                expected_execution_host_fingerprint_sha256=HOST_SHA,
            )

        float_trace = valid_trace()
        raw_float = canonical_json_bytes(float_trace).replace(
            b'"created_at_ns":10000', b'"created_at_ns":10000.0'
        )
        float_fixture = TraceFixture(float_trace, raw=raw_float)
        self.addCleanup(float_fixture.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "floating-point"):
            load_private_trace(
                float_fixture.path,
                expected_source_sha256=float_fixture.sha256,
                expected_environment_fingerprint_sha256=ENV_SHA,
                expected_producer_code_sha256=CODE_SHA,
                expected_producer_config_sha256=CONFIG_SHA,
                expected_place_cancel_live_authority_sha256=AUTH_SHA,
                expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
                expected_clock_quality_receipt_sha256=CLOCK_SHA,
                expected_execution_host_fingerprint_sha256=HOST_SHA,
            )

        unknown_trace = valid_trace()
        unknown_trace["samples"][0]["surprise"] = 1
        with self.assertRaisesRegex(MeasuredLatencyError, "extra="):
            load(unknown_trace)

        symlink_fixture = TraceFixture(trace)
        self.addCleanup(symlink_fixture.close)
        link = symlink_fixture.path.with_name("link.json")
        link.symlink_to(symlink_fixture.path)
        with self.assertRaisesRegex(MeasuredLatencyError, "symlink"):
            load_private_trace(
                link,
                expected_source_sha256=symlink_fixture.sha256,
                expected_environment_fingerprint_sha256=ENV_SHA,
                expected_producer_code_sha256=CODE_SHA,
                expected_producer_config_sha256=CONFIG_SHA,
                expected_place_cancel_live_authority_sha256=AUTH_SHA,
                expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
                expected_clock_quality_receipt_sha256=CLOCK_SHA,
                expected_execution_host_fingerprint_sha256=HOST_SHA,
            )

    def test_production_loader_rejects_unpromoted_private_trace(self) -> None:
        fixture = TraceFixture(valid_trace())
        self.addCleanup(fixture.close)
        with self.assertRaisesRegex(
            MeasuredLatencyError, "root-owned"
        ):
            load_private_trace(
                fixture.path,
                expected_source_sha256=fixture.sha256,
                expected_environment_fingerprint_sha256=ENV_SHA,
                expected_producer_code_sha256=CODE_SHA,
                expected_producer_config_sha256=CONFIG_SHA,
                expected_place_cancel_live_authority_sha256=AUTH_SHA,
                expected_ioc_exit_live_authority_sha256=IOC_AUTH_SHA,
                expected_clock_quality_receipt_sha256=CLOCK_SHA,
                expected_execution_host_fingerprint_sha256=HOST_SHA,
                require_root_read_only=True,
            )


if __name__ == "__main__":
    unittest.main()
