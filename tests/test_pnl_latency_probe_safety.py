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
            terminal_path = root / "terminal.json"
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
                "--terminal-consumption-receipt",
                str(terminal_path),
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
            self.assertFalse(terminal_path.exists())

    def test_authority_wall_and_monotonic_deadlines_fail_closed(self) -> None:
        completed = self.run_probe("--self-test-authority-deadline")
        self.assertEqual(0, completed.returncode)
        self.assertEqual(
            "AUTHORITY DEADLINE SELF-TEST PASS", completed.stdout.strip()
        )

    def test_proxy_and_ca_override_environment_is_scrubbed(self) -> None:
        completed = self.run_probe(
            "--self-test-network-environment",
            extra_env={
                "HTTP_PROXY": "http://attacker.invalid:8080",
                "https_proxy": "http://attacker.invalid:8081",
                "ALL_PROXY": "socks5://attacker.invalid:1080",
                "CURL_CA_BUNDLE": "/attacker/ca.pem",
                "SSL_CERT_FILE": "/attacker/cert.pem",
                "AWS_CA_BUNDLE": "/attacker/aws-ca.pem",
            },
        )
        self.assertEqual(0, completed.returncode)
        self.assertEqual(
            "NETWORK ENVIRONMENT SELF-TEST PASS", completed.stdout.strip()
        )

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
            execute_body.index("drop_execution_privileges"),
        )
        self.assertLess(
            execute_body.index("drop_execution_privileges"),
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
            "monotonic_deadline_ns",
            "authority_allows_new_risk",
            "cancel_risk_reduction_after_expiry",
            "BLOCKED_AMBIGUOUS_PLACE_OUTCOME_NO_RETRY",
            "scrub_ambient_network_environment",
            "network_boundary_still_valid",
            "default_curl_ca_path",
            "drop_execution_privileges",
            "::setgroups(0, nullptr)",
            "::setgid(",
            "::setuid(",
            "PR_SET_NO_NEW_PRIVS",
            "terminal_consumption_receipt",
            "transaction_id",
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

        place_section = execute_body[
            execute_body.index("CausalSample place;") :
            execute_body.index("CausalSample cancel;")
        ]
        self.assertLess(
            place_section.index("authority_allows_new_risk"),
            place_section.index("client.sign_request"),
        )
        self.assertLess(
            place_section.rindex("authority_allows_new_risk"),
            place_section.index("lane.send(*place_request)"),
        )
        self.assertLess(
            place_section.rindex("network_boundary_still_valid"),
            place_section.index("lane.send(*place_request)"),
        )
        order_parser = source[
            source.index("std::optional<OrderSnapshot> parse_order_snapshot") :
            source.index("std::optional<std::string> parse_order_id")
        ]
        self.assertNotIn('"status"', order_parser)
        self.assertNotIn('"order_status"', order_parser)

    def test_root_broker_files_are_not_execution_deletable(self) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        reservation = source[
            source.index("class OutputReservation") :
            source.index("struct PositionSnapshot")
        ]
        self.assertIn("create_flags, 0400", reservation)
        self.assertIn("info.st_uid != 0", reservation)
        execute = source[source.index("int execute_place_cancel") :]
        for reservation_name in (
            "ledger_reservation",
            "output_reservation",
            "terminal_reservation",
        ):
            self.assertLess(
                execute.index(reservation_name),
                execute.index("drop_execution_privileges"),
            )
        self.assertIn(
            "This root preamble is the single-use broker", execute
        )
        self.assertIn(
            "PLACE/CANCEL authority already consumed", execute
        )
        consumption = execute[
            execute.index("std::ostringstream consumption;") :
            execute.index("ledger_reservation.finish")
        ]
        self.assertLess(
            consumption.index("nonce_sha256"),
            consumption.index("producer_code_sha256"),
        )
        self.assertLess(
            consumption.index("producer_config_sha256"),
            consumption.index("receipt_id"),
        )
        self.assertLess(
            consumption.index("receipt_id"),
            consumption.index("schema_version"),
        )

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
