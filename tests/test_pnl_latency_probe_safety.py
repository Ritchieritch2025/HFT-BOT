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
            "FAIL_CLOSED_AMBIGUOUS_POST_RECOVERY_UNPROVEN",
            payload["place_cancel_gate"],
        )
        self.assertEqual(
            "FAIL_CLOSED_PENDING_INDEPENDENT_AUDIT_FEE_BINDING_AND_"
            "ACCOUNT_LOCK_DEPLOYMENT",
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
        self.assertIn(
            "IMPLEMENTED BUT LIVE TRANSMISSION REMAINS DISABLED",
            completed.stderr,
        )
        self.assertNotIn("private key", completed.stderr.lower())

    def test_place_cancel_live_path_is_disabled_before_all_inputs(self) -> None:
        completed = self.run_probe(
            "--execute-place-cancel",
            "--ticker",
            "TEST-TICKER",
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "LIVE TRANSMISSION IS DISABLED AND FAILS CLOSED",
            completed.stderr,
        )

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

    def test_disabled_live_path_touches_no_control_credentials_or_output(
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
            self.assertIn("LIVE TRANSMISSION IS DISABLED", first.stderr)
            self.assertNotIn("private key", first.stderr.lower())
            self.assertFalse(output_path.exists())
            self.assertFalse(ledger_path.exists())
            self.assertFalse(terminal_path.exists())

    def test_ambiguous_post_attack_matrix_is_fail_closed(self) -> None:
        completed = self.run_probe(
            "--self-test-ambiguous-post-recovery"
        )
        self.assertEqual(0, completed.returncode)
        self.assertEqual(
            "AMBIGUOUS POST RECOVERY SELF-TEST PASS",
            completed.stdout.strip(),
        )
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        for attack_case in (
            "order_exists_cleanly_canceled",
            "order_not_found",
            "query_timeout",
            "cancel_timeout",
            "partial_fill",
        ):
            self.assertIn(attack_case, source)
        self.assertIn(
            "constexpr bool kAmbiguousPlaceRecoveryProven = false",
            source,
        )

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

    def test_dormant_mutation_scaffold_retains_prior_safety_ordering(
        self,
    ) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        execute_body = source[source.index("int execute_place_cancel") :]
        hard_gate = execute_body.index(
            "if constexpr (!kAmbiguousPlaceRecoveryProven)"
        )
        for prohibited_before_gate in (
            "require_common_options",
            "collect_runtime_facts",
            'std::getenv("KALSHI_API_KEY_ID")',
            "ledger_reservation",
            "lane.send(*place_request)",
        ):
            self.assertLess(
                hard_gate, execute_body.index(prohibited_before_gate)
            )
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

    def test_dormant_root_broker_files_remain_execution_non_deletable(
        self,
    ) -> None:
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

    def test_ioc_exit_implementation_remains_closed_until_fresh_audit(
        self,
    ) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "constexpr bool kIocExitExecutorIndependentlyAudited = false",
            source,
        )
        self.assertIn(
            "constexpr bool kIocPretradeFeeScheduleBound = false",
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
        self.assertIn("PENDING INDEPENDENT AUDIT", completed.stderr)

    def test_ioc_exit_state_machine_adversarial_matrix_passes(self) -> None:
        completed = self.run_probe("--self-test-ioc-exit-executor")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "IOC EXIT EXECUTOR CONTRACT SELF-TEST PASS",
            completed.stdout.strip(),
        )

    def test_ioc_exit_live_gate_precedes_every_side_effect(self) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        execute = source[source.index("int execute_ioc_exit") :]
        gate = execute.index(
            "if constexpr (!kIocExitExecutorIndependentlyAudited ||"
        )
        for later in (
            "require_common_options",
            "collect_runtime_facts",
            "parse_ioc_authority",
            "account_lock",
            "ledger_reservation",
            "drop_execution_privileges",
            'std::getenv("KALSHI_API_KEY_ID")',
            "pnl_ioc::run",
        ):
            self.assertLess(gate, execute.index(later), later)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out.json"
            consumed = root / "consumed.json"
            terminal = root / "terminal.json"
            completed = self.run_probe(
                "--execute-ioc-exit",
                "--ticker",
                "TEST-TICKER",
                "--authority-file",
                str(root / "missing-authority.json"),
                "--expected-authority-sha256",
                "1" * 64,
                "--producer-config-file",
                str(root / "missing-config.json"),
                "--environment-receipt-file",
                str(root / "missing-environment.json"),
                "--execution-host-receipt-file",
                str(root / "missing-host.json"),
                "--clock-quality-receipt-file",
                str(root / "missing-clock.json"),
                "--receipt-id",
                "ioc-gate-test",
                "--out",
                str(out),
                "--consumption-ledger",
                str(consumed),
                "--terminal-consumption-receipt",
                str(terminal),
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
            self.assertFalse(out.exists())
            self.assertFalse(consumed.exists())
            self.assertFalse(terminal.exists())

    def test_ioc_executor_has_one_post_and_known_id_only_recovery(self) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        header = (ROOT / "apps" / "pnl_ioc_exit_state.hpp").read_text(
            encoding="utf-8"
        )
        execute = source[
            source.index("int execute_ioc_exit") :
            source.index("int execute_place_cancel")
        ]
        transport = source[
            source.index("class KalshiIocTransport") :
            source.index("bool best_effort_cancel")
        ]
        self.assertEqual(
            1, transport.count("lane_.send(*request)"),
            "IOC transport must expose exactly one mutation send",
        )
        self.assertIn(
            'Method::Get, std::string(kGetOrderPath) + "/"',
            transport,
        )
        self.assertNotIn("/portfolio/orders?", transport)
        self.assertNotIn("client_order_id=", transport)
        self.assertIn(
            "kMaximumKnownOrderReadAttempts = 3", header
        )
        self.assertIn(
            "there is deliberately no list-orders", header.lower()
        )
        self.assertIn(
            "credential_broker_must_refuse_while_present", execute
        )
        self.assertIn(
            "ROOT_ONLY_AFTER_TERMINAL_RECEIPT_", execute
        )
        self.assertIn(
            '<< "RECONCILIATION', execute
        )
        self.assertIn(
            '"fragment_only_not_three_path_ready\\":true', execute
        )

    def test_ioc_body_uses_exact_official_v2_safety_fields(self) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        body = source[
            source.index("std::string ioc_order_body") :
            source.index("std::string sample_json")
        ]
        for exact_field in (
            '\\"time_in_force\\":\\"immediate_or_cancel\\"',
            '\\"reduce_only\\":true',
            '\\"post_only\\":false',
            '\\"self_trade_prevention_type\\":\\"taker_at_cross\\"',
            '\\"cancel_order_on_pause\\":true',
            '\\"exchange_index\\":-1',
        ):
            self.assertIn(exact_field, body)
        self.assertIn("fixed_count_e2(plan.quantity_e4)", body)
        self.assertIn('\\"subaccount\\":', body)
        self.assertIn("std::to_string(plan.subaccount)", body)
        self.assertNotIn("expiration_time", body)

    def test_ioc_repair_01_protocol_authority_and_cash_truth_are_bound(
        self,
    ) -> None:
        source = (ROOT / "apps" / "pnl_latency_probe.cpp").read_text(
            encoding="utf-8"
        )
        header = (ROOT / "apps" / "pnl_ioc_exit_state.hpp").read_text(
            encoding="utf-8"
        )
        for authority_field in (
            '"expected_position_before_e4"',
            '"book_side"',
            '"outcome_side"',
            '"price_limit_semantics"',
            '"subaccount"',
            "MAXIMUM_BUY_YES_PRICE_CAP",
            "MINIMUM_SELL_YES_PRICE_FLOOR",
        ):
            self.assertIn(authority_field, source)
        self.assertIn("plan.quantity_e4 % 100 != 0", header)
        self.assertIn("plan.subaccount != 0", header)
        self.assertIn(
            "average_within_official_rounding_interval", header
        )
        self.assertIn("json_has_recursive_unique_keys", source)
        self.assertIn("json_has_negative_zero_number", source)
        self.assertIn("canonical_count_e4", source)
        for cash_field in (
            '\\"taker_fill_cost_e6\\"',
            '\\"maker_fill_cost_e6\\"',
            '\\"total_fill_cost_e6\\"',
            '\\"taker_fee_e6\\"',
            '\\"maker_fee_e6\\"',
            '\\"total_fee_e6\\"',
            '\\"cash_truth_receipt_binding_sha256\\"',
            '\\"create_response_sha256\\"',
            '\\"get_order_response_sha256\\"',
        ):
            self.assertIn(cash_field, source)


if __name__ == "__main__":
    unittest.main()
