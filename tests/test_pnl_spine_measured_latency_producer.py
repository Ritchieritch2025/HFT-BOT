#!/usr/bin/env python3
"""Adversarial tests for the strict causal-latency producer."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from typing import Any, Callable


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


RAW_ORDER_ID = "raw-order-id-must-never-leak"
CLOCK_ID = "STD_STEADY_CLOCK:probe-process"


def _sha(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _path_sha(path: Path) -> str:
    return _sha(str(path))


def _transaction_id(authority_sha: str, nonce: str, receipt_id: str) -> str:
    return _sha(authority_sha + nonce + receipt_id)


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


def sample(path: str, start: int, engine_base_ms: int) -> dict[str, object]:
    semantics = {
        "PLACE": "NEW_ORDER_PLACE",
        "CANCEL": "RESTING_ORDER_CANCEL",
        "IOC_EXIT": "IOC_POSITION_REDUCING_EXIT",
    }
    return {
        "path": path,
        "action_semantics": semantics[path],
        "clock_quality_receipt_sha256": "0" * 64,
        "decision_ns": start,
        "sent_ns": start + 10,
        "acknowledged_ns": start + 20,
        "effective_ns": start + 30,
        "order_ref_sha256": _sha(
            RAW_ORDER_ID + ("PLACE" if path == "CANCEL" else path)
        ),
        "request_sha256": _sha("request-" + path),
        "response_sha256": _sha("response-" + path),
        "source_event_sha256": _sha("event-" + path),
        "http_status": 201 if path != "CANCEL" else 200,
        "live_authority_sha256": "0" * 64,
        "environment_fingerprint_sha256": "0" * 64,
        "execution_host_fingerprint_sha256": "0" * 64,
        "matching_engine_ts_ms": engine_base_ms + start,
        "average_fee_paid_e6": 12_300 if path == "IOC_EXIT" else None,
        "average_fill_price_e4": 5_500 if path == "IOC_EXIT" else None,
        "book_side": "BID",
        "request_reduce_only": path == "IOC_EXIT",
        "requested_price_e4": 5_500 if path == "IOC_EXIT" else 100,
        "subaccount": 0,
        "ticker_sha256": "0" * 64,
        "time_in_force": (
            "IMMEDIATE_OR_CANCEL"
            if path == "IOC_EXIT"
            else "GOOD_TILL_CANCELED"
        ),
        "fill_reconciliation": reconciliation(path),
    }


def valid_trace(now_ms: int) -> dict[str, object]:
    engine_base_ms = now_ms - 2_000
    return {
        "schema_version": "pnl-spine-private-causal-latency-trace-v1",
        "receipt_id": "three-path-real-order-test",
        "measurement_mode": "REAL_ORDER_MEASURED",
        "clock_id": CLOCK_ID,
        "measured_on": "w09-research",
        "created_at_ns": 10_000,
        "created_at_wall_utc_ms": now_ms - 1_000,
        "clock_quality_receipt_sha256": "0" * 64,
        "environment_fingerprint_sha256": "0" * 64,
        "execution_host_fingerprint_sha256": "0" * 64,
        "producer_code_sha256": "0" * 64,
        "producer_config_sha256": "0" * 64,
        "samples": [
            sample("PLACE", 100, engine_base_ms),
            sample("CANCEL", 200, engine_base_ms),
            sample("IOC_EXIT", 300, engine_base_ms),
        ],
    }


class EvidenceBundle:
    """Create one fully cross-bound synthetic evidence graph, without I/O."""

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        same_nonce: bool = False,
        clock_captured_at_ms: int | None = None,
        raw_trace: bytes | None = None,
    ) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.now_ms = int(time.time_ns() // 1_000_000)
        self.receipt_id = "three-path-real-order-test"
        self.ticker = "TEST-TICKER"
        self.hostname = "w09-research"
        self.instance_id = "i-0e53d134dceffe166"
        self.machine_sha = _sha("machine-id")
        self.execution_uid = 1001
        self.execution_gid = 1001

        self.trace_path = self.root / "private.json"
        self.source_path = self.root / "execution-source.json"
        self.binary_path = self.root / "pnl_latency_probe"
        self.config_path = self.root / "producer-config.json"
        self.environment_path = self.root / "environment.json"
        self.host_path = self.root / "host.json"
        self.clock_path = self.root / "clock.json"
        self.ca_path = self.root / "ca-bundle.pem"
        self.place_authority_path = self.root / "place-authority.json"
        self.place_consumption_path = self.root / "place-consumption.json"
        self.place_terminal_path = self.root / "place-terminal.json"
        self.ioc_authority_path = self.root / "ioc-authority.json"
        self.ioc_consumption_path = self.root / "ioc-consumption.json"
        self.ioc_terminal_path = self.root / "ioc-terminal.json"
        self.promotion_path = self.root / "promotion.json"

        self.binary_path.write_bytes(b"pinned-producer-binary-v2")
        self.ca_path.write_bytes(b"pinned-test-ca-bundle")
        self.code_sha = _sha(self.binary_path.read_bytes())
        self.ca_sha = _sha(self.ca_path.read_bytes())
        self.config = {
            "ca_bundle_path": str(self.ca_path),
            "ca_bundle_sha256": self.ca_sha,
            "execution_gid": self.execution_gid,
            "execution_uid": self.execution_uid,
            "max_cash_loss_e6": 10_000,
            "max_fee_e6": 10_000,
            "place_post_only": True,
            "place_side": "BID",
            "place_time_in_force": "GOOD_TILL_CANCELED",
            "price_cap_e4": 100,
            "quantity_e4": 10_000,
            "schema_version": "pnl-spine-latency-probe-config-v2",
        }
        self._write_json(self.config_path, self.config)
        self.config_sha = _sha(self.config_path.read_bytes())
        self.environment = {
            "ca_bundle_sha256": self.ca_sha,
            "hostname": self.hostname,
            "instance_id": self.instance_id,
            "kalshi_environment": "prod",
            "kalshi_mode": "live",
            "machine_id_sha256": self.machine_sha,
            "producer_code_sha256": self.code_sha,
            "producer_config_sha256": self.config_sha,
            "schema_version": "pnl-spine-execution-environment-receipt-v1",
        }
        self._write_json(self.environment_path, self.environment)
        self.environment_sha = _sha(self.environment_path.read_bytes())
        self.host = {
            "captured_at_wall_utc_ms": self.now_ms - 20_000,
            "environment_fingerprint_sha256": self.environment_sha,
            "hostname": self.hostname,
            "instance_id": self.instance_id,
            "machine_id_sha256": self.machine_sha,
            "schema_version": "pnl-spine-execution-host-receipt-v1",
        }
        self._write_json(self.host_path, self.host)
        self.host_sha = _sha(self.host_path.read_bytes())
        self.clock = {
            "captured_at_wall_utc_ms": (
                self.now_ms - 10_000
                if clock_captured_at_ms is None
                else clock_captured_at_ms
            ),
            "clock_id": CLOCK_ID,
            "hostname": self.hostname,
            "instance_id": self.instance_id,
            "machine_id_sha256": self.machine_sha,
            "max_error_ns": 1_000_000,
            "schema_version": "pnl-spine-clock-quality-receipt-v1",
            "source": "chrony-tracking-root-receipt",
            "synchronized": True,
        }
        self._write_json(self.clock_path, self.clock)
        self.clock_sha = _sha(self.clock_path.read_bytes())

        issued_s = self.now_ms // 1_000 - 60
        expires_s = self.now_ms // 1_000 + 300
        place_nonce = _sha("place-nonce")
        ioc_nonce = place_nonce if same_nonce else _sha("ioc-nonce")
        common = {
            "ca_bundle_sha256": self.ca_sha,
            "clock_quality_receipt_sha256": self.clock_sha,
            "environment_fingerprint_sha256": self.environment_sha,
            "execution_host_fingerprint_sha256": self.host_sha,
            "expires_at_unix_s": expires_s,
            "issued_at_unix_s": issued_s,
            "max_attempts": 1,
            "measured_on": self.hostname,
            "producer_code_sha256": self.code_sha,
            "producer_config_sha256": self.config_sha,
            "receipt_id": self.receipt_id,
            "single_use": True,
            "ticker": self.ticker,
            "trace_output_path_sha256": _path_sha(self.source_path),
        }
        self.place_authority = {
            **common,
            "allow_ioc_exit": False,
            "allow_place_cancel": True,
            "consumption_ledger_path_sha256": _path_sha(
                self.place_consumption_path
            ),
            "max_cancel_orders": 1,
            "max_cash_loss_e6": 10_000,
            "max_fee_e6": 10_000,
            "max_place_orders": 1,
            "nonce_sha256": place_nonce,
            "price_cap_e4": 100,
            "quantity_e4": 10_000,
            "schema_version": "pnl-spine-latency-probe-authority-v2",
            "terminal_consumption_receipt_path_sha256": _path_sha(
                self.place_terminal_path
            ),
        }
        self.ioc_authority = {
            **common,
            "allow_ioc_exit": True,
            "book_side": "bid",
            "consumption_ledger_path_sha256": _path_sha(
                self.ioc_consumption_path
            ),
            "expected_position_before_e4": -20_000,
            "max_cash_loss_e6": 1_000_000,
            "max_fee_e6": 100_000,
            "max_ioc_exit_orders": 1,
            "nonce_sha256": ioc_nonce,
            "outcome_side": "yes",
            "price_limit_e4": 5_500,
            "price_limit_semantics": "MAXIMUM_BUY_YES_PRICE_CAP",
            "quantity_e4": 20_000,
            "schema_version": "pnl-spine-ioc-exit-authority-v2",
            "subaccount": 0,
            "terminal_consumption_receipt_path_sha256": _path_sha(
                self.ioc_terminal_path
            ),
        }
        self._write_json(self.place_authority_path, self.place_authority)
        self._write_json(self.ioc_authority_path, self.ioc_authority)
        self.place_authority_sha = _sha(
            self.place_authority_path.read_bytes()
        )
        self.ioc_authority_sha = _sha(self.ioc_authority_path.read_bytes())

        trace = deepcopy(payload) if payload is not None else valid_trace(
            self.now_ms
        )
        self._bind_trace_context(trace)
        trace_bytes = (
            canonical_json_bytes(trace)
            if raw_trace is None
            else raw_trace
        )
        self.trace_path.write_bytes(trace_bytes)
        self.trace_sha = _sha(trace_bytes)
        self._build_consumption_and_promotion()

    def _bind_trace_context(self, trace: dict[str, object]) -> None:
        trace["receipt_id"] = self.receipt_id
        trace["clock_id"] = CLOCK_ID
        trace["measured_on"] = self.hostname
        trace["clock_quality_receipt_sha256"] = self.clock_sha
        trace["environment_fingerprint_sha256"] = self.environment_sha
        trace["execution_host_fingerprint_sha256"] = self.host_sha
        trace["producer_code_sha256"] = self.code_sha
        trace["producer_config_sha256"] = self.config_sha
        samples = trace.get("samples")
        if not isinstance(samples, list):
            return
        for item in samples:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            item["clock_quality_receipt_sha256"] = self.clock_sha
            item["environment_fingerprint_sha256"] = self.environment_sha
            item["execution_host_fingerprint_sha256"] = self.host_sha
            item["ticker_sha256"] = _sha(self.ticker)
            item["live_authority_sha256"] = (
                self.ioc_authority_sha
                if path == "IOC_EXIT"
                else self.place_authority_sha
            )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_bytes(canonical_json_bytes(value))

    def _build_consumption_and_promotion(self) -> None:
        consumed_ms = self.now_ms - 2_500
        finished_ms = self.now_ms - 800
        self.place_transaction_id = _transaction_id(
            self.place_authority_sha,
            self.place_authority["nonce_sha256"],
            self.receipt_id,
        )
        self.ioc_transaction_id = _transaction_id(
            self.ioc_authority_sha,
            self.ioc_authority["nonce_sha256"],
            self.receipt_id,
        )
        common_consumption = {
            "ca_bundle_sha256": self.ca_sha,
            "clock_quality_receipt_sha256": self.clock_sha,
            "consumed_at_wall_utc_ms": consumed_ms,
            "environment_fingerprint_sha256": self.environment_sha,
            "execution_host_fingerprint_sha256": self.host_sha,
            "max_attempts": 1,
            "producer_code_sha256": self.code_sha,
            "producer_config_sha256": self.config_sha,
            "receipt_id": self.receipt_id,
            "trace_output_path_sha256": _path_sha(self.source_path),
        }
        self.place_consumption = {
            **common_consumption,
            "authority_sha256": self.place_authority_sha,
            "max_cancel_delete_attempts": 1,
            "max_place_post_attempts": 1,
            "nonce_sha256": self.place_authority["nonce_sha256"],
            "schema_version": (
                "pnl-spine-latency-authority-consumption-v2"
            ),
            "terminal_consumption_receipt_path_sha256": _path_sha(
                self.place_terminal_path
            ),
            "transaction_id": self.place_transaction_id,
        }
        self.ioc_consumption = {
            **common_consumption,
            "authority_sha256": self.ioc_authority_sha,
            "expected_position_before_e4": (
                self.ioc_authority["expected_position_before_e4"]
            ),
            "max_ioc_exit_post_attempts": 1,
            "nonce_sha256": self.ioc_authority["nonce_sha256"],
            "schema_version": (
                "pnl-spine-ioc-exit-authority-consumption-v2"
            ),
            "subaccount": self.ioc_authority["subaccount"],
            "terminal_consumption_receipt_path_sha256": _path_sha(
                self.ioc_terminal_path
            ),
            "transaction_id": self.ioc_transaction_id,
        }
        self.place_terminal = {
            "authority_sha256": self.place_authority_sha,
            "cancel_delete_attempts": 1,
            "cancel_risk_reduction_after_expiry": False,
            "client_order_id_sha256": _sha("place-client-order-id"),
            "finished_at_wall_utc_ms": finished_ms,
            "place_post_attempts": 1,
            "schema_version": "pnl-spine-latency-authority-terminal-v2",
            "terminal_state": "PLACE_CANCEL_RECONCILED",
            "trace_source_sha256": self.trace_sha,
            "transaction_id": self.place_transaction_id,
        }
        self.ioc_terminal = {
            "authority_sha256": self.ioc_authority_sha,
            "client_order_id_sha256": _sha("ioc-client-order-id"),
            "finished_at_wall_utc_ms": finished_ms,
            "ioc_exit_post_attempts": 1,
            "schema_version": "pnl-spine-ioc-exit-authority-terminal-v2",
            "terminal_state": "IOC_EXIT_RECONCILED",
            "trace_source_sha256": self.trace_sha,
            "transaction_id": self.ioc_transaction_id,
        }
        self.promotion = {
            "producer_code_sha256": self.code_sha,
            "promoted_at_wall_utc_ms": self.now_ms - 500,
            "promoted_mode_octal": "0400",
            "promoted_owner_uid": 0,
            "promoted_path": str(self.trace_path),
            "promoted_raw_sha256": self.trace_sha,
            "promoted_size_bytes": self.trace_path.stat().st_size,
            "promoter_uid": 0,
            "schema_version": "pnl-spine-root-promotion-receipt-v1",
            "source_device": 1,
            "source_inode": 1,
            "source_mode_octal": "0400",
            "source_path": str(self.source_path),
            "source_raw_sha256": self.trace_sha,
            "source_size_bytes": self.trace_path.stat().st_size,
            "source_uid": 0,
        }
        for path, value in (
            (self.place_consumption_path, self.place_consumption),
            (self.ioc_consumption_path, self.ioc_consumption),
            (self.place_terminal_path, self.place_terminal),
            (self.ioc_terminal_path, self.ioc_terminal),
            (self.promotion_path, self.promotion),
        ):
            self._write_json(path, value)

    def load(
        self, *, require_root_read_only: bool = False
    ) -> tuple[dict[str, Any], str]:
        return load_private_trace(
            self.trace_path,
            producer_binary_path=self.binary_path,
            producer_config_path=self.config_path,
            environment_receipt_path=self.environment_path,
            execution_host_receipt_path=self.host_path,
            clock_quality_receipt_path=self.clock_path,
            ca_bundle_path=self.ca_path,
            place_authority_path=self.place_authority_path,
            place_consumption_path=self.place_consumption_path,
            place_terminal_path=self.place_terminal_path,
            ioc_authority_path=self.ioc_authority_path,
            ioc_consumption_path=self.ioc_consumption_path,
            ioc_terminal_path=self.ioc_terminal_path,
            promotion_receipt_path=self.promotion_path,
            require_root_read_only=require_root_read_only,
            now_wall_utc_ms=None if require_root_read_only else self.now_ms,
        )

    def rewrite_trace(
        self, mutation: Callable[[dict[str, Any]], None]
    ) -> None:
        trace = json.loads(self.trace_path.read_text(encoding="utf-8"))
        mutation(trace)
        self._write_json(self.trace_path, trace)
        self.trace_sha = _sha(self.trace_path.read_bytes())
        size = self.trace_path.stat().st_size
        for terminal_path in (
            self.place_terminal_path,
            self.ioc_terminal_path,
        ):
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            terminal["trace_source_sha256"] = self.trace_sha
            self._write_json(terminal_path, terminal)
        promotion = json.loads(self.promotion_path.read_text(encoding="utf-8"))
        promotion["promoted_raw_sha256"] = self.trace_sha
        promotion["source_raw_sha256"] = self.trace_sha
        promotion["promoted_size_bytes"] = size
        promotion["source_size_bytes"] = size
        self._write_json(self.promotion_path, promotion)

    def mutate_artifact(
        self, path: Path, mutation: Callable[[dict[str, Any]], None]
    ) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        mutation(value)
        self._write_json(path, value)

    def rewrite_ioc_authority(
        self,
        mutation: Callable[[dict[str, Any]], None],
        *,
        trace_mutation: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        authority = json.loads(
            self.ioc_authority_path.read_text(encoding="utf-8")
        )
        mutation(authority)
        self._write_json(self.ioc_authority_path, authority)
        self.ioc_authority = authority
        self.ioc_authority_sha = _sha(
            self.ioc_authority_path.read_bytes()
        )
        self.ioc_transaction_id = _transaction_id(
            self.ioc_authority_sha,
            authority["nonce_sha256"],
            self.receipt_id,
        )

        def bind_trace(trace: dict[str, Any]) -> None:
            for item in trace["samples"]:
                if item["path"] == "IOC_EXIT":
                    item["live_authority_sha256"] = (
                        self.ioc_authority_sha
                    )
            if trace_mutation is not None:
                trace_mutation(trace)

        self.rewrite_trace(bind_trace)
        consumption = json.loads(
            self.ioc_consumption_path.read_text(encoding="utf-8")
        )
        consumption.update(
            authority_sha256=self.ioc_authority_sha,
            expected_position_before_e4=authority[
                "expected_position_before_e4"
            ],
            nonce_sha256=authority["nonce_sha256"],
            subaccount=authority["subaccount"],
            transaction_id=self.ioc_transaction_id,
        )
        self._write_json(self.ioc_consumption_path, consumption)
        terminal = json.loads(
            self.ioc_terminal_path.read_text(encoding="utf-8")
        )
        terminal.update(
            authority_sha256=self.ioc_authority_sha,
            trace_source_sha256=self.trace_sha,
            transaction_id=self.ioc_transaction_id,
        )
        self._write_json(self.ioc_terminal_path, terminal)

    def close(self) -> None:
        self.temp.cleanup()


def load(payload: dict[str, object]) -> tuple[dict[str, Any], str]:
    fixture = EvidenceBundle(payload)
    try:
        return fixture.load()
    finally:
        fixture.close()


class MeasuredLatencyProducerTests(unittest.TestCase):
    def test_complete_trace_produces_runner_and_audit_ready_receipts(
        self,
    ) -> None:
        fixture = EvidenceBundle()
        self.addCleanup(fixture.close)
        private_trace, source_sha = fixture.load()
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
        fixture = EvidenceBundle()
        self.addCleanup(fixture.close)
        private_trace, source_sha = fixture.load()
        _, inventory, expected_audit = build_receipts(
            private_trace, source_sha256=source_sha
        )
        inventory_path = fixture.root / "inventory-input.json"
        inventory_path.write_bytes(canonical_json_bytes(inventory))
        inventory_sha = _sha(inventory_path.read_bytes())
        typed = load_latency_evidence_inventory(
            inventory_path, expected_inventory_sha256=inventory_sha
        )
        self.assertEqual(3, len(typed.trace_candidates))
        self.assertEqual(
            expected_audit["source_inventory_sha256"], inventory_sha
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
            self.assertEqual(_sha(first.read_bytes()), hashes["first"])
            real_parent = root / "real"
            real_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(MeasuredLatencyError, "symlinks"):
                write_canonical_bundle(
                    {
                        "linked": (
                            linked_parent / "receipt.json",
                            {"value": 3},
                        )
                    }
                )

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

    def test_core_sample_semantics_remain_fail_closed(self) -> None:
        attacks: tuple[
            tuple[str, Callable[[dict[str, Any]], None], str], ...
        ] = (
            (
                "missing request",
                lambda trace: trace["samples"][0].pop("request_sha256"),
                "missing=",
            ),
            (
                "noncausal",
                lambda trace: trace["samples"][0].update(
                    acknowledged_ns=99
                ),
                "not causal",
            ),
            (
                "wrong action",
                lambda trace: trace["samples"][0].update(
                    action_semantics="IOC_POSITION_REDUCING_EXIT"
                ),
                "does not match PLACE",
            ),
            (
                "partial place",
                lambda trace: trace["samples"][0][
                    "fill_reconciliation"
                ].update(
                    filled_quantity_e4=1,
                    remaining_quantity_e4=9_999,
                    position_after_e4=1,
                ),
                "partially filled",
            ),
            (
                "cancel remainder",
                lambda trace: trace["samples"][1][
                    "fill_reconciliation"
                ].update(
                    canceled_quantity_e4=9_000,
                    remaining_quantity_e4=1_000,
                ),
                "full unfilled",
            ),
            (
                "cancel position drift",
                lambda trace: trace["samples"][1][
                    "fill_reconciliation"
                ].update(position_after_e4=-10_000),
                "changed position",
            ),
        )
        for label, attack, message in attacks:
            with self.subTest(label=label):
                fixture = EvidenceBundle()
                self.addCleanup(fixture.close)
                fixture.rewrite_trace(attack)
                with self.assertRaisesRegex(MeasuredLatencyError, message):
                    fixture.load()

    def test_fee_engine_and_ioc_exit_semantics_are_strict(self) -> None:
        attacks: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda trace: trace["samples"][0].update(
                matching_engine_ts_ms=0
            ),
            lambda trace: trace["samples"][0].update(
                average_fee_paid_e6=1
            ),
            lambda trace: trace["samples"][2].update(
                average_fee_paid_e6=None
            ),
            lambda trace: trace["samples"][2].update(book_side="ASK"),
            lambda trace: trace["samples"][2][
                "fill_reconciliation"
            ].update(reduce_only=False),
            lambda trace: trace["samples"][2][
                "fill_reconciliation"
            ].update(
                filled_quantity_e4=10_000,
                canceled_quantity_e4=10_000,
            ),
            lambda trace: trace["samples"][2][
                "fill_reconciliation"
            ].update(position_after_e4=10_000),
        )
        for attack in attacks:
            fixture = EvidenceBundle()
            self.addCleanup(fixture.close)
            fixture.rewrite_trace(attack)
            with self.assertRaises(MeasuredLatencyError):
                fixture.load()

    def test_lifecycle_splicing_and_global_reuse_are_rejected(self) -> None:
        attacks: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda trace: trace["samples"][1].update(
                order_ref_sha256="8" * 64
            ),
            lambda trace: trace["samples"][1].update(
                decision_ns=120,
                sent_ns=121,
                acknowledged_ns=122,
                effective_ns=123,
            ),
            lambda trace: trace["samples"][1].update(
                ticker_sha256="8" * 64
            ),
            lambda trace: trace["samples"][2].update(
                order_ref_sha256=trace["samples"][0]["order_ref_sha256"]
            ),
        )
        for attack in attacks:
            fixture = EvidenceBundle()
            self.addCleanup(fixture.close)
            fixture.rewrite_trace(attack)
            with self.assertRaises(MeasuredLatencyError):
                fixture.load()

    def test_authority_replay_and_duplicate_ioc_reproducers_are_rejected(
        self,
    ) -> None:
        fixture = EvidenceBundle()
        self.addCleanup(fixture.close)

        def replay_place_cancel(trace: dict[str, Any]) -> None:
            place = deepcopy(trace["samples"][0])
            cancel = deepcopy(trace["samples"][1])
            order_ref = _sha("second-place-cancel-order")
            for suffix, item in (("place", place), ("cancel", cancel)):
                item["order_ref_sha256"] = order_ref
                item["request_sha256"] = _sha("replay-request-" + suffix)
                item["response_sha256"] = _sha("replay-response-" + suffix)
                item["source_event_sha256"] = _sha("replay-event-" + suffix)
            trace["samples"].extend((place, cancel))

        fixture.rewrite_trace(replay_place_cancel)
        with self.assertRaisesRegex(MeasuredLatencyError, "exactly one"):
            fixture.load()

        second = EvidenceBundle()
        self.addCleanup(second.close)

        def duplicate_ioc(trace: dict[str, Any]) -> None:
            ioc = deepcopy(trace["samples"][2])
            ioc["request_sha256"] = _sha("duplicate-ioc-request")
            ioc["response_sha256"] = _sha("duplicate-ioc-response")
            ioc["source_event_sha256"] = _sha("duplicate-ioc-event")
            trace["samples"].append(ioc)

        second.rewrite_trace(duplicate_ioc)
        with self.assertRaisesRegex(MeasuredLatencyError, "exactly one"):
            second.load()

    def test_missing_or_tampered_consumption_and_terminal_refuse_ready(
        self,
    ) -> None:
        missing = EvidenceBundle()
        self.addCleanup(missing.close)
        missing.place_consumption_path.unlink()
        with self.assertRaises(MeasuredLatencyError):
            missing.load()

        bad_consumption = EvidenceBundle()
        self.addCleanup(bad_consumption.close)
        bad_consumption.mutate_artifact(
            bad_consumption.place_consumption_path,
            lambda value: value.update(max_place_post_attempts=2),
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "maxima"):
            bad_consumption.load()

        bad_terminal = EvidenceBundle()
        self.addCleanup(bad_terminal.close)
        bad_terminal.mutate_artifact(
            bad_terminal.ioc_terminal_path,
            lambda value: value.update(ioc_exit_post_attempts=2),
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "mutation count"):
            bad_terminal.load()

        late_consumption = EvidenceBundle()
        self.addCleanup(late_consumption.close)
        late_consumption.mutate_artifact(
            late_consumption.place_consumption_path,
            lambda value: value.update(
                consumed_at_wall_utc_ms=late_consumption.now_ms - 850
            ),
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "consumed transaction"
        ):
            late_consumption.load()

        reused_client_id = EvidenceBundle()
        self.addCleanup(reused_client_id.close)
        place_terminal = json.loads(
            reused_client_id.place_terminal_path.read_text(encoding="utf-8")
        )
        reused_client_id.mutate_artifact(
            reused_client_id.ioc_terminal_path,
            lambda value: value.update(
                client_order_id_sha256=place_terminal[
                    "client_order_id_sha256"
                ]
            ),
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "must be distinct"):
            reused_client_id.load()

    def test_authority_documents_are_loaded_not_self_reported(self) -> None:
        bad = EvidenceBundle()
        self.addCleanup(bad.close)
        bad.mutate_artifact(
            bad.place_authority_path,
            lambda value: value.update(max_attempts=2),
        )
        with self.assertRaises(MeasuredLatencyError):
            bad.load()

        same_nonce = EvidenceBundle(same_nonce=True)
        self.addCleanup(same_nonce.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "nonces"):
            same_nonce.load()

        source = (
            ROOT
            / "tools"
            / "research"
            / "pnl_spine"
            / "measured_latency_producer.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "--expected-source-sha256",
            "--expected-producer-code-sha256",
            "--expected-clock-quality-receipt-sha256",
            "--expected-place-cancel-live-authority-sha256",
        ):
            self.assertNotIn(forbidden, source)

    def test_ioc_authority_direction_floor_cap_and_subaccount_are_bound(
        self,
    ) -> None:
        reversed_position = EvidenceBundle()
        self.addCleanup(reversed_position.close)
        reversed_position.rewrite_ioc_authority(
            lambda value: value.update(
                expected_position_before_e4=20_000,
                book_side="ask",
                outcome_side="no",
                price_limit_semantics="MINIMUM_SELL_YES_PRICE_FLOOR",
            )
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "differs from authority"
        ):
            reversed_position.load()

        floor_violation = EvidenceBundle()
        self.addCleanup(floor_violation.close)
        floor_violation.rewrite_ioc_authority(
            lambda value: value.update(
                expected_position_before_e4=20_000,
                book_side="ask",
                outcome_side="no",
                price_limit_e4=5_500,
                price_limit_semantics="MINIMUM_SELL_YES_PRICE_FLOOR",
            ),
            trace_mutation=lambda trace: trace["samples"][2].update(
                book_side="ASK",
                requested_price_e4=5_499,
                fill_reconciliation={
                    **trace["samples"][2]["fill_reconciliation"],
                    "position_before_e4": 20_000,
                },
            ),
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "differs from authority"
        ):
            floor_violation.load()

        nonprimary = EvidenceBundle()
        self.addCleanup(nonprimary.close)
        nonprimary.rewrite_ioc_authority(
            lambda value: value.update(subaccount=1),
            trace_mutation=lambda trace: trace["samples"][2].update(
                subaccount=1
            ),
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "authority action differs"
        ):
            nonprimary.load()

        unbounded_fee = EvidenceBundle()
        self.addCleanup(unbounded_fee.close)
        unbounded_fee.rewrite_ioc_authority(
            lambda value: value.update(
                max_cash_loss_e6=10_000,
                max_fee_e6=10_001,
            )
        )
        with self.assertRaisesRegex(
            MeasuredLatencyError, "authority action differs"
        ):
            unbounded_fee.load()

    def test_code_context_clock_and_promotion_are_independently_verified(
        self,
    ) -> None:
        binary = EvidenceBundle()
        self.addCleanup(binary.close)
        binary.binary_path.write_bytes(b"substituted binary")
        with self.assertRaises(MeasuredLatencyError):
            binary.load()

        clock = EvidenceBundle(
            clock_captured_at_ms=int(time.time_ns() // 1_000_000) + 60_000
        )
        self.addCleanup(clock.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "future-dated"):
            clock.load()

        promotion = EvidenceBundle()
        self.addCleanup(promotion.close)
        promotion.mutate_artifact(
            promotion.promotion_path,
            lambda value: value.update(source_inode=0),
        )
        with self.assertRaises(MeasuredLatencyError):
            promotion.load()

    def test_future_trace_and_matching_engine_evidence_is_rejected(self) -> None:
        trace = EvidenceBundle()
        self.addCleanup(trace.close)
        trace.rewrite_trace(
            lambda value: value.update(
                created_at_wall_utc_ms=trace.now_ms + 60_000
            )
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "trusted clock"):
            trace.load()

        engine = EvidenceBundle()
        self.addCleanup(engine.close)
        engine.rewrite_trace(
            lambda value: value["samples"][0].update(
                matching_engine_ts_ms=engine.now_ms + 60_000
            )
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "future-dated"):
            engine.load()

    def test_trace_mutations_cannot_be_hidden_by_refreshing_sha_receipts(
        self,
    ) -> None:
        fixture = EvidenceBundle()
        self.addCleanup(fixture.close)
        fixture.rewrite_trace(
            lambda trace: trace["samples"][0].update(
                live_authority_sha256="f" * 64
            )
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "raw authority"):
            fixture.load()

    def test_noncanonical_duplicate_float_unknown_and_symlink_refused(
        self,
    ) -> None:
        base = valid_trace(int(time.time_ns() // 1_000_000))
        pretty = EvidenceBundle(base, raw_trace=json.dumps(base, indent=2).encode())
        self.addCleanup(pretty.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "not exact canonical"):
            pretty.load()

        duplicate = EvidenceBundle(
            base,
            raw_trace=b'{"schema_version":"a","schema_version":"b"}',
        )
        self.addCleanup(duplicate.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "duplicate JSON key"):
            duplicate.load()

        float_fixture = EvidenceBundle()
        self.addCleanup(float_fixture.close)
        raw = float_fixture.trace_path.read_bytes().replace(
            b'"created_at_ns":10000', b'"created_at_ns":10000.0'
        )
        float_fixture.trace_path.write_bytes(raw)
        with self.assertRaisesRegex(MeasuredLatencyError, "floating-point"):
            float_fixture.load()

        unknown = EvidenceBundle()
        self.addCleanup(unknown.close)
        unknown.rewrite_trace(
            lambda trace: trace["samples"][0].update(surprise=1)
        )
        with self.assertRaisesRegex(MeasuredLatencyError, "extra="):
            unknown.load()

        symlink = EvidenceBundle()
        self.addCleanup(symlink.close)
        link = symlink.root / "linked-private.json"
        link.symlink_to(symlink.trace_path)
        symlink.trace_path = link
        with self.assertRaisesRegex(MeasuredLatencyError, "symlink"):
            symlink.load()

    def test_production_loader_rejects_unpromoted_private_trace(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root-owned production boundary needs non-root test")
        fixture = EvidenceBundle()
        self.addCleanup(fixture.close)
        with self.assertRaisesRegex(MeasuredLatencyError, "root-owned"):
            fixture.load(require_root_read_only=True)


if __name__ == "__main__":
    unittest.main()
