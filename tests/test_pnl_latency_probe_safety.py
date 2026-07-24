#!/usr/bin/env python3
"""Offline safety tests for the C++ causal-latency probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / "pnl_latency_probe"


class PnlLatencyProbeSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        completed = subprocess.run(
            ["make", "build/pnl_latency_probe"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                "pnl_latency_probe build failed:\n"
                + completed.stdout
                + completed.stderr
            )

    def run_probe(
        self, *arguments: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(extra_env or {})
        return subprocess.run(
            [str(BINARY), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    def test_default_is_credential_free_socket_free_dry_run(self) -> None:
        completed = self.run_probe(
            extra_env={
                "KALSHI_API_KEY_ID": "",
                "KALSHI_PRIVATE_KEY_PATH": "",
            }
        )
        self.assertEqual(0, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertEqual("DRY_RUN", payload["default_mode"])
        self.assertFalse(payload["network_io"])
        self.assertFalse(payload["order_transmitted"])
        self.assertFalse(payload["three_path_latency_ready"])
        self.assertEqual(
            "FAIL_CLOSED_EXECUTOR_NOT_IMPLEMENTED",
            payload["ioc_exit_gate"],
        )

    def test_official_v2_fixed_point_fixtures_parse_exactly(self) -> None:
        completed = self.run_probe("--self-test-v2")
        self.assertEqual(0, completed.returncode)
        self.assertEqual(
            "V2 CONTRACT SELF-TEST PASS", completed.stdout.strip()
        )

    def test_ioc_execute_is_refused_before_credentials_or_network(self) -> None:
        completed = self.run_probe(
            "--execute-ioc-exit",
            extra_env={
                "KALSHI_API_KEY_ID": "must-not-be-read",
                "KALSHI_PRIVATE_KEY_PATH": "/does/not/exist",
                "KALSHI_ENV": "prod",
                "KALSHI_MODE": "live",
                "KALSHI_ALLOW_PROD": "1",
                "KALSHI_ALLOW_LIVE": "1",
            },
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("NOT IMPLEMENTED AND FAILS CLOSED", completed.stderr)
        self.assertNotIn("private key", completed.stderr.lower())

    def test_place_cancel_cannot_reach_runtime_without_all_pins(self) -> None:
        completed = self.run_probe(
            "--execute-place-cancel",
            "--ticker",
            "TEST-TICKER",
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("options incomplete", completed.stderr)

    def test_ioc_preflight_is_read_only_but_needs_read_credentials(self) -> None:
        completed = self.run_probe(
            "--ioc-exit-preflight",
            "--ticker",
            "TEST-TICKER",
            extra_env={
                "KALSHI_API_KEY_ID": "",
                "KALSHI_PRIVATE_KEY_PATH": "",
            },
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("read credentials", completed.stderr)

    def test_untrusted_control_files_refuse_before_credentials_or_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            authority_path = root / "authority.json"
            output_path = root / "trace.json"
            ledger_path = root / "consumed.json"
            config_path = root / "config.json"
            environment_path = root / "environment.json"
            host_path = root / "host.json"
            clock_path = root / "clock.json"
            for path in (
                authority_path,
                config_path,
                environment_path,
                host_path,
                clock_path,
            ):
                path.write_text("{}", encoding="utf-8")
            arguments = (
                "--execute-place-cancel",
                "--ticker",
                "TEST-TICKER",
                "--authority-file",
                str(authority_path),
                "--expected-authority-sha256",
                "1" * 64,
                "--producer-config-file",
                str(config_path),
                "--environment-receipt-file",
                str(environment_path),
                "--execution-host-receipt-file",
                str(host_path),
                "--clock-quality-receipt-file",
                str(clock_path),
                "--receipt-id",
                "single-use-test",
                "--out",
                str(output_path),
                "--consumption-ledger",
                str(ledger_path),
            )
            first = self.run_probe(
                *arguments,
                extra_env={
                    "KALSHI_API_KEY_ID": "must-not-be-read",
                    "KALSHI_PRIVATE_KEY_PATH": "/does/not/exist",
                    "KALSHI_ENV": "prod",
                    "KALSHI_MODE": "live",
                    "KALSHI_ALLOW_PROD": "1",
                    "KALSHI_ALLOW_LIVE": "1",
                },
            )
            self.assertEqual(2, first.returncode)
            self.assertIn("immutable code/config/environment/host/clock", first.stderr)
            self.assertNotIn("private key", first.stderr.lower())
            self.assertFalse(output_path.exists())
            self.assertFalse(ledger_path.exists())

    def test_cli_cannot_supply_self_reported_runtime_hashes(self) -> None:
        completed = self.run_probe(
            "--execute-place-cancel",
            "--producer-code-sha256",
            "1" * 64,
            extra_env={
                "KALSHI_API_KEY_ID": "must-not-be-read",
                "KALSHI_PRIVATE_KEY_PATH": "/does/not/exist",
            },
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("usage:", completed.stderr)

    def test_mutation_source_orders_safety_gates_before_credentials_and_post(
        self,
    ) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        execute_body = source[source.index("int execute_place_cancel") :]
        self.assertLess(
            execute_body.index("ledger_reservation.finish"),
            execute_body.index('std::getenv("KALSHI_API_KEY_ID")'),
        )
        self.assertLess(
            execute_body.index('std::getenv("KALSHI_API_KEY_ID")'),
            execute_body.index("lane.send(*place_request)"),
        )
        for required in (
            'readlink("/proc/self/exe"',
            "_NSGetExecutablePath",
            "hash_root_read_only_file",
            "read_root_control_file",
            "root_owned_components",
            "O_NOFOLLOW",
            "O_EXCL",
            "S_ISVTX",
            "kMaximumClockReceiptAgeMs",
            "kMaximumClockErrorNs",
            "validate_clock_quality_receipt",
            "facts->producer_code_sha256",
            "facts->producer_config_sha256",
            "facts->execution_host_fingerprint_sha256",
        ):
            self.assertIn(required, source)
        for forbidden in (
            'argument == "--producer-code-sha256"',
            'argument == "--producer-config-sha256"',
            'argument == "--environment-fingerprint-sha256"',
            'argument == "--execution-host-fingerprint-sha256"',
            'argument == "--clock-quality-receipt-sha256"',
            'argument == "--measured-on"',
        ):
            self.assertNotIn(forbidden, source)

    def test_ioc_exit_cannot_be_counted_as_complete_three_path(self) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"IOC_EXIT TRANSMISSION IS NOT IMPLEMENTED AND FAILS CLOSED',
            source,
        )
        self.assertIn(
            '"state\\":\\"PLACE_CANCEL_RECONCILED_IOC_EXIT_MISSING',
            source,
        )
        completed = self.run_probe(
            "--execute-ioc-exit",
            "--ticker",
            "TEST-TICKER",
            extra_env={
                "KALSHI_API_KEY_ID": "must-not-be-read",
                "KALSHI_PRIVATE_KEY_PATH": "/does/not/exist",
            },
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("FAILS CLOSED", completed.stderr)


if __name__ == "__main__":
    unittest.main()
