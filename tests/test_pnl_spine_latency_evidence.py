#!/usr/bin/env python3
"""Adversarial tests for real-order latency evidence gating."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_TOOLS = ROOT / "tools" / "research"
if str(RESEARCH_TOOLS) not in sys.path:
    sys.path.insert(0, str(RESEARCH_TOOLS))

from pnl_spine.contracts import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
)
from pnl_spine.latency_evidence import (  # noqa: E402
    LATENCY_AGGREGATE_READY,
    LATENCY_BLOCKED,
    LatencyEvidenceError,
    build_latency_audit_receipt,
    load_latency_evidence_inventory,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
PRIVATE_ORDER_MARKER = "private-order-should-never-be-emitted"


def candidate(
    path: str,
    start_ns: int,
    total_ns: int,
) -> dict[str, object]:
    semantics = {
        "PLACE": "NEW_ORDER_PLACE",
        "CANCEL": "RESTING_ORDER_CANCEL",
        "IOC_EXIT": "IOC_POSITION_REDUCING_EXIT",
    }
    return {
        "acknowledged_ns": start_ns + total_ns - 1,
        "action_semantics": semantics[path],
        "clock_id": "CLOCK_MONOTONIC_RAW:prod-exec-1",
        "decision_ns": start_ns,
        "effective_ns": start_ns + total_ns,
        "measurement_mode": "REAL_ORDER_MEASURED",
        "order_ref_sha256": hashlib.sha256(
            (PRIVATE_ORDER_MARKER + path + str(start_ns)).encode()
        ).hexdigest(),
        "path": path,
        "real_order_source_sha256": SHA_D,
        "sent_ns": start_ns + 1,
        "source_event_sha256": SHA_E,
    }


def base_payload() -> dict[str, object]:
    return {
        "audit_id": "latency-audit-test",
        "audited_at_ns": 1_000_000,
        "clock_id": "CLOCK_MONOTONIC_RAW:prod-exec-1",
        "environment_fingerprint_sha256": SHA_A,
        "measured_on": "prod-exec-1",
        "rejected_artifacts": [
            {
                "artifact_sha256": SHA_B,
                "artifact_type": "AUTHENTICATED_READ_ONLY_GET_RTT",
                "observed_fields": [
                    "http_status",
                    "rtt_ms",
                ],
                "record_count": 1600,
                "rejection_reasons": [
                    "NO_CAUSAL_ORDER_EVENTS",
                    "NOT_PATH_CLASSIFIED",
                    "NOT_REAL_ORDER",
                ],
            },
            {
                "artifact_sha256": SHA_C,
                "artifact_type": "ORDER_ACTIVITY_SNAPSHOT",
                "observed_fields": [
                    "created_time",
                    "last_update_time",
                    "status",
                ],
                "record_count": 13,
                "rejection_reasons": [
                    "NO_ACK_TIMESTAMP",
                    "NO_DECISION_TIMESTAMP",
                    "NO_EFFECTIVE_TIMESTAMP",
                    "NO_SEND_TIMESTAMP",
                ],
            },
        ],
        "schema_version": "pnl-spine-latency-evidence-inventory-v1",
        "trace_candidates": [],
    }


class InventoryFixture:
    def __init__(self, payload: dict[str, object]) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "inventory.json"
        self.bytes = canonical_json_bytes(payload)
        self.path.write_bytes(self.bytes)
        self.sha256 = hashlib.sha256(self.bytes).hexdigest()

    def close(self) -> None:
        self._temp.cleanup()


class LatencyBlockedReceiptTests(unittest.TestCase):
    def test_tracked_real_audit_receipt_is_reproducible_and_blocked(
        self,
    ) -> None:
        receipt_dir = ROOT / "Deepresearch V3" / "registry" / "receipts"
        inventory_path = (
            receipt_dir
            / "PNL_SPINE_LATENCY_EVIDENCE_INVENTORY_20260723.json"
        )
        receipt_path = (
            receipt_dir / "PNL_SPINE_LATENCY_BLOCKED_20260723.json"
        )
        inventory = load_latency_evidence_inventory(
            inventory_path,
            expected_inventory_sha256=(
                "0b9cce7976490361920df87f95393ab50fa27fc393105c19c3b68ab2ded8e8e4"
            ),
        )
        generated = build_latency_audit_receipt(inventory)
        tracked = json.loads(receipt_path.read_bytes())
        self.assertEqual(tracked, generated)
        self.assertEqual(LATENCY_BLOCKED, generated["state"])
        self.assertEqual(
            (
                "32d4dbef255558519ab0b6ce8bfd33fe"
                "193b60c045b072f028e08c9bb4c495f3"
            ),
            generated["payload_sha256"],
        )

    def test_get_ping_cpu_and_order_snapshots_cannot_be_laundered(
        self,
    ) -> None:
        fixture = InventoryFixture(base_payload())
        self.addCleanup(fixture.close)
        inventory = load_latency_evidence_inventory(
            fixture.path,
            expected_inventory_sha256=fixture.sha256,
        )
        receipt = build_latency_audit_receipt(inventory)
        self.assertEqual(LATENCY_BLOCKED, receipt["state"])
        self.assertEqual(
            [
                "LATENCY_CANCEL_REAL_CAUSAL_TRACE_MISSING",
                "LATENCY_IOC_EXIT_REAL_CAUSAL_TRACE_MISSING",
                "LATENCY_PLACE_REAL_CAUSAL_TRACE_MISSING",
            ],
            sorted(blocker["code"] for blocker in receipt["blockers"]),
        )
        self.assertEqual(
            [0, 0, 0],
            [
                summary["valid_causal_sample_count"]
                for summary in receipt["path_summaries"]
            ],
        )
        policy = receipt["policy"]
        self.assertFalse(
            policy["ping_or_http_rtt_accepted_as_order_latency"]
        )
        self.assertFalse(
            policy["order_snapshot_accepted_as_causal_trace"]
        )
        payload_sha = receipt.pop("payload_sha256")
        self.assertEqual(canonical_sha256(receipt), payload_sha)

    def test_partial_paths_remain_blocked_and_name_only_missing_path(
        self,
    ) -> None:
        payload = base_payload()
        payload["trace_candidates"] = [
            candidate("PLACE", 100, 20),
            candidate("CANCEL", 200, 30),
        ]
        fixture = InventoryFixture(payload)
        self.addCleanup(fixture.close)
        receipt = build_latency_audit_receipt(
            load_latency_evidence_inventory(fixture.path)
        )
        self.assertEqual(LATENCY_BLOCKED, receipt["state"])
        self.assertEqual(
            ["LATENCY_IOC_EXIT_REAL_CAUSAL_TRACE_MISSING"],
            [blocker["code"] for blocker in receipt["blockers"]],
        )

    def test_bad_semantics_noncausal_or_missing_source_never_counts(
        self,
    ) -> None:
        bad_semantics = candidate("IOC_EXIT", 100, 20)
        bad_semantics["action_semantics"] = "NEW_ORDER_PLACE"
        noncausal = candidate("PLACE", 200, 20)
        noncausal["effective_ns"] = 199
        no_real_source = candidate("CANCEL", 300, 20)
        no_real_source["real_order_source_sha256"] = None
        payload = base_payload()
        payload["trace_candidates"] = [
            bad_semantics,
            noncausal,
            no_real_source,
        ]
        fixture = InventoryFixture(payload)
        self.addCleanup(fixture.close)
        receipt = build_latency_audit_receipt(
            load_latency_evidence_inventory(fixture.path)
        )
        self.assertEqual(LATENCY_BLOCKED, receipt["state"])
        summaries = {
            summary["path"]: summary
            for summary in receipt["path_summaries"]
        }
        self.assertEqual(
            {"NON_CAUSAL_TIMESTAMPS": 1},
            summaries["PLACE"]["invalid_reason_counts"],
        )
        self.assertEqual(
            {"REAL_ORDER_SOURCE_SHA_MISSING": 1},
            summaries["CANCEL"]["invalid_reason_counts"],
        )
        self.assertEqual(
            {"WRONG_ACTION_SEMANTICS": 1},
            summaries["IOC_EXIT"]["invalid_reason_counts"],
        )


class ReadyAggregatePrivacyTests(unittest.TestCase):
    def test_complete_real_three_path_evidence_emits_aggregates_only(
        self,
    ) -> None:
        payload = base_payload()
        payload["trace_candidates"] = [
            candidate("PLACE", 100, 10),
            candidate("PLACE", 200, 30),
            candidate("CANCEL", 300, 40),
            candidate("IOC_EXIT", 400, 50),
        ]
        fixture = InventoryFixture(payload)
        self.addCleanup(fixture.close)
        receipt = build_latency_audit_receipt(
            load_latency_evidence_inventory(fixture.path)
        )
        self.assertEqual(LATENCY_AGGREGATE_READY, receipt["state"])
        self.assertEqual([], receipt["blockers"])
        summaries = {
            summary["path"]: summary
            for summary in receipt["path_summaries"]
        }
        self.assertEqual(2, summaries["PLACE"]["valid_causal_sample_count"])
        self.assertEqual(10, summaries["PLACE"]["p50_ns"])
        self.assertEqual(30, summaries["PLACE"]["p95_ns"])
        self.assertEqual(30, summaries["PLACE"]["p99_ns"])
        encoded = canonical_json_bytes(receipt)
        self.assertNotIn(PRIVATE_ORDER_MARKER.encode(), encoded)
        self.assertNotIn(b"order_ref_sha256", encoded)
        self.assertNotIn(b"decision_ns", encoded)
        self.assertNotIn(b"effective_ns", encoded)
        self.assertFalse(
            receipt["policy"]["raw_order_or_trace_identifiers_emitted"]
        )

    def test_cli_prints_only_canonical_receipt_and_blocked_exit_code(
        self,
    ) -> None:
        fixture = InventoryFixture(base_payload())
        self.addCleanup(fixture.close)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pnl_spine.latency_evidence",
                str(fixture.path),
                "--expected-inventory-sha256",
                fixture.sha256,
            ],
            cwd=RESEARCH_TOOLS,
            check=False,
            capture_output=True,
        )
        self.assertEqual(3, result.returncode)
        receipt = json.loads(result.stdout)
        self.assertEqual(LATENCY_BLOCKED, receipt["state"])
        self.assertEqual(
            canonical_json_bytes(receipt) + b"\n",
            result.stdout,
        )
        self.assertEqual(b"", result.stderr)


class CanonicalInventoryTests(unittest.TestCase):
    def test_noncanonical_duplicate_float_symlink_and_sha_drift_refused(
        self,
    ) -> None:
        fixture = InventoryFixture(base_payload())
        self.addCleanup(fixture.close)
        fixture.path.write_bytes(
            json.dumps(base_payload(), indent=2).encode()
        )
        with self.assertRaises(LatencyEvidenceError):
            load_latency_evidence_inventory(fixture.path)

        fixture.path.write_bytes(b'{"x":1,"x":2}')
        with self.assertRaises(LatencyEvidenceError):
            load_latency_evidence_inventory(fixture.path)

        payload = base_payload()
        payload["audited_at_ns"] = 1.5
        fixture.path.write_bytes(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        with self.assertRaises(LatencyEvidenceError):
            load_latency_evidence_inventory(fixture.path)

        fixture.path.write_bytes(canonical_json_bytes(base_payload()))
        with self.assertRaises(LatencyEvidenceError):
            load_latency_evidence_inventory(
                fixture.path,
                expected_inventory_sha256="f" * 64,
            )

        symlink_path = fixture.path.parent / "symlink.json"
        symlink_path.symlink_to(fixture.path)
        with self.assertRaises(LatencyEvidenceError):
            load_latency_evidence_inventory(symlink_path)


if __name__ == "__main__":
    unittest.main()
