#!/usr/bin/env python3
"""Focused offline tests for PIPE-W09 bring-up security contracts."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W09 = os.path.join(ROOT, "deploy", "w09")
CANONICAL = "/Users/ritcardo/HFT-BOT-pipeline-recovery/tools"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestInstanceProfileCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, CANONICAL)
        cls.module = load_module(
            "w09_role_cli_test",
            os.path.join(W09, "research_data_instance_profile.py"),
        )

    @classmethod
    def tearDownClass(cls):
        try:
            sys.path.remove(CANONICAL)
        except ValueError:
            pass

    def test_imdsv2_token_role_and_temporary_credential_contract(self):
        calls = []

        def fake_request(url, method="GET", headers=None, timeout=2.0):
            calls.append((url, method, dict(headers or {})))
            if url.endswith("/api/token"):
                return b"imdsv2-token"
            if url.endswith("/meta-data/iam/info"):
                return json.dumps({
                    "InstanceProfileArn":
                        "arn:aws:iam::123456789012:instance-profile/"
                        "w09-research-runner",
                }).encode()
            if url.endswith("/iam/security-credentials/"):
                return b"contained-role-name\n"
            return json.dumps({
                "Code": "Success",
                "AccessKeyId": "ASIATEST",
                "SecretAccessKey": "secret",
                "Token": "session-token",
                "Expiration": "2099-01-01T00:00:00Z",
            }).encode()

        with mock.patch.object(self.module, "_request", fake_request):
            creds = self.module.IMDSv2Credentials()
            self.assertEqual(
                creds.current(), ("ASIATEST", "secret", "session-token")
            )
        self.assertEqual(calls[0][1], "PUT")
        self.assertEqual(
            calls[0][2]["X-aws-ec2-metadata-token-ttl-seconds"], "21600"
        )
        self.assertTrue(all(
            call[2].get("X-aws-ec2-metadata-token") == "imdsv2-token"
            for call in calls[1:]
        ))

    def test_wrong_instance_profile_fails_closed(self):
        def fake_request(url, method="GET", headers=None, timeout=2.0):
            if url.endswith("/api/token"):
                return b"token"
            if url.endswith("/meta-data/iam/info"):
                return json.dumps({
                    "InstanceProfileArn":
                        "arn:aws:iam::123456789012:instance-profile/wrong",
                }).encode()
            return b"some-role"

        with mock.patch.object(self.module, "_request", fake_request):
            with self.assertRaises(SystemExit) as caught:
                self.module.IMDSv2Credentials().refresh()
        self.assertIn("identity", str(caught.exception))

    def test_static_key_mode_is_refused_without_reading_values(self):
        with mock.patch.dict(
                os.environ,
                {"AWS_ACCESS_KEY_ID": "must-not-print-this"}, clear=True), \
                mock.patch.object(self.module.os.path, "exists",
                                  return_value=False):
            with self.assertRaises(SystemExit) as caught:
                self.module.refuse_static_credentials()
        self.assertNotIn("must-not-print-this", str(caught.exception))

    def test_session_token_is_part_of_sigv4_signed_headers(self):
        class Credentials:
            def current(self):
                return "ASIATEST", "secret", "session-token"

        captured = {}

        class Response:
            def read(self, *_args):
                return b""

        def fake_urlopen(request, timeout=120):
            captured["request"] = request
            return Response()

        store = self.module.InstanceProfileS3Store(
            "bucket", "research", "us-east-2", Credentials()
        )
        with mock.patch.object(self.module.urllib.request, "urlopen",
                               fake_urlopen):
            store._signed_request("research/releases/x/MANIFEST.json")
        headers = {key.lower(): value for key, value in
                   captured["request"].header_items()}
        self.assertEqual(headers["x-amz-security-token"], "session-token")
        self.assertIn("x-amz-security-token", headers["authorization"])

    def test_local_fixture_root_does_not_recurse_after_monkeypatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.module.make_instance_profile_store(tmp)
            self.assertEqual(store.describe(), tmp)


class TestReleaseSelection(unittest.TestCase):
    def test_newest_date_then_publication_time_not_hash_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rid, date, generated in (
                ("2026-07-12__seal-aaaaaaaa__pub-ffffffffffffffff",
                 "2026-07-12", "2026-07-13T04:00:00Z"),
                ("2026-07-13__seal-bbbbbbbb__pub-0000000000000000",
                 "2026-07-13", "2026-07-14T03:00:00Z"),
                ("2026-07-13__seal-cccccccc__pub-1111111111111111",
                 "2026-07-13", "2026-07-14T05:00:00Z"),
            ):
                path = os.path.join(tmp, "releases", rid)
                os.makedirs(path)
                with open(os.path.join(path, "MANIFEST.json"), "w") as f:
                    json.dump({
                        "schema_version": "research-release-manifest-v2",
                        "release_id": rid,
                        "date": date,
                        "generated_at_utc": generated,
                        "version_binding": {"mode": "VERSION_BOUND"},
                        "objects": [{"size": 7}],
                    }, f)
            result = subprocess.run([
                sys.executable, os.path.join(W09, "select_newest_release.py"),
                "--cache", tmp, "--json",
            ], check=True, capture_output=True, text=True)
            selected = json.loads(result.stdout)
            self.assertEqual(
                selected["release_id"],
                "2026-07-13__seal-cccccccc__pub-1111111111111111",
            )


class TestIdleGuard(unittest.TestCase):
    def setUp(self):
        self.module = load_module(
            "w09_idle_test_%s" % id(self),
            os.path.join(W09, "w09_idle_check.py"),
        )

    def test_sensor_failure_means_busy_not_shutdown(self):
        with mock.patch.object(self.module.os.path, "exists",
                               return_value=False), \
                mock.patch.object(self.module, "run_sensor",
                                  side_effect=RuntimeError("sensor")):
            self.assertEqual(self.module.busy_reason(), "sensor-error")

    def test_real_idle_baseline_accepts_procps_empty_selection(self):
        """procps rc=1/no-output means the research user owns no work."""
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),  # ss
            subprocess.CompletedProcess([], 0, "", ""),  # loginctl
            subprocess.CompletedProcess([], 1, "", ""),  # ps: no match
            subprocess.CompletedProcess([], 0, "", ""),  # inhibitors
        ]
        with mock.patch.object(self.module.os.path, "exists",
                               return_value=False), \
                mock.patch.object(self.module.subprocess, "run",
                                  side_effect=responses):
            self.assertIsNone(self.module.busy_reason())

    def test_procps_rc1_with_error_still_fails_closed_and_names_sensor(self):
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess(
                [], 1, "", "permission denied: must-not-log-this"
            ),
        ]
        with mock.patch.object(self.module.os.path, "exists",
                               return_value=False), \
                mock.patch.object(self.module.subprocess, "run",
                                  side_effect=responses):
            self.assertEqual(self.module.busy_reason(), "sensor-error")
        self.assertEqual(self.module.LAST_SENSOR_ERROR["sensor"], "ps")
        self.assertEqual(self.module.LAST_SENSOR_ERROR["returncode"], 1)
        self.assertEqual(
            self.module.LAST_SENSOR_ERROR["failure_kind"],
            "nonzero-exit",
        )
        self.assertNotIn("stderr", self.module.LAST_SENSOR_ERROR)

    def test_sensor_error_event_is_structured_and_redacted(self):
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess(
                [], 1, "", "permission denied: must-not-log-this"
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self.module.RUN_DIR = os.path.join(tmp, "run")
            self.module.STATE_DIR = os.path.join(tmp, "state")
            self.module.IDLE_STATE = os.path.join(
                self.module.RUN_DIR, "idle_since.json")
            self.module.LOCK_PATH = os.path.join(
                self.module.RUN_DIR, "check.lock")
            self.module.EVENT_LOG = os.path.join(
                self.module.STATE_DIR, "events.jsonl")
            with mock.patch.object(self.module.os.path, "exists",
                                   return_value=False), \
                    mock.patch.object(self.module.subprocess, "run",
                                      side_effect=responses), \
                    mock.patch.object(self.module, "boot_id",
                                      return_value="boot-a"):
                self.assertEqual(self.module.main(), 0)
            with open(self.module.EVENT_LOG, encoding="utf-8") as handle:
                raw_event = handle.read()
            self.assertNotIn("must-not-log-this", raw_event)
            event = json.loads(raw_event)
            self.assertEqual(event["reason"], "sensor-error")
            self.assertEqual(event["sensor_error"], {
                "sensor": "ps",
                "failure_kind": "nonzero-exit",
                "returncode": 1,
            })

    def test_unwrapped_ubuntu_process_means_busy(self):
        def sensor(args, allowed_empty_returncodes=()):
            if args[0] == "/usr/bin/ps":
                return " 123 python3 python3 long_research.py\n"
            return ""
        with mock.patch.object(self.module.os.path, "exists",
                               return_value=False), \
                mock.patch.object(self.module, "run_sensor", sensor):
            self.assertEqual(self.module.busy_reason(), "user-workload")

    def test_full_1800_seconds_required_then_poweroff(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.module.RUN_DIR = os.path.join(tmp, "run")
            self.module.STATE_DIR = os.path.join(tmp, "state")
            self.module.IDLE_STATE = os.path.join(
                self.module.RUN_DIR, "idle_since.json")
            self.module.LOCK_PATH = os.path.join(
                self.module.RUN_DIR, "check.lock")
            self.module.EVENT_LOG = os.path.join(
                self.module.STATE_DIR, "events.jsonl")
            poweroff = mock.Mock(return_value=mock.Mock(returncode=0))
            with mock.patch.object(self.module, "busy_reason",
                                   return_value=None), \
                    mock.patch.object(self.module, "boot_id",
                                      return_value="boot-a"), \
                    mock.patch.object(self.module, "uptime_seconds",
                                      return_value=100.0):
                self.assertEqual(self.module.main(), 0)
            with mock.patch.object(self.module, "busy_reason",
                                   return_value=None), \
                    mock.patch.object(self.module, "boot_id",
                                      return_value="boot-a"), \
                    mock.patch.object(self.module, "uptime_seconds",
                                      side_effect=[1900.0, 1901.0]), \
                    mock.patch.object(self.module.time, "sleep"), \
                    mock.patch.object(self.module.subprocess, "run", poweroff):
                self.assertEqual(self.module.main(), 0)
            command = poweroff.call_args.args[0]
            self.assertEqual(command, ["/usr/bin/systemctl", "poweroff"])
            with open(self.module.EVENT_LOG) as handle:
                events = [json.loads(line) for line in handle]
            self.assertEqual(events[-1]["decision"], "poweroff-requested")
            self.assertGreaterEqual(events[-1]["idle_for_sec"], 1800)

    def test_stale_boot_idle_state_restarts_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.module.RUN_DIR = os.path.join(tmp, "run")
            self.module.STATE_DIR = os.path.join(tmp, "state")
            self.module.IDLE_STATE = os.path.join(
                self.module.RUN_DIR, "idle_since.json")
            self.module.LOCK_PATH = os.path.join(
                self.module.RUN_DIR, "check.lock")
            self.module.EVENT_LOG = os.path.join(
                self.module.STATE_DIR, "events.jsonl")
            os.makedirs(self.module.RUN_DIR)
            with open(self.module.IDLE_STATE, "w", encoding="utf-8") as handle:
                json.dump({"boot_id": "old-boot", "uptime": 1.0}, handle)
            poweroff = mock.Mock(return_value=mock.Mock(returncode=0))
            with mock.patch.object(self.module, "busy_reason",
                                   return_value=None), \
                    mock.patch.object(self.module, "boot_id",
                                      return_value="new-boot"), \
                    mock.patch.object(self.module, "uptime_seconds",
                                      return_value=4000.0), \
                    mock.patch.object(self.module.subprocess, "run", poweroff):
                self.assertEqual(self.module.main(), 0)
            poweroff.assert_not_called()
            with open(self.module.IDLE_STATE, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state, {"boot_id": "new-boot", "uptime": 4000.0})

    def test_sensor_failure_on_final_recheck_cancels_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.module.RUN_DIR = os.path.join(tmp, "run")
            self.module.STATE_DIR = os.path.join(tmp, "state")
            self.module.IDLE_STATE = os.path.join(
                self.module.RUN_DIR, "idle_since.json")
            self.module.LOCK_PATH = os.path.join(
                self.module.RUN_DIR, "check.lock")
            self.module.EVENT_LOG = os.path.join(
                self.module.STATE_DIR, "events.jsonl")
            os.makedirs(self.module.RUN_DIR)
            with open(self.module.IDLE_STATE, "w", encoding="utf-8") as handle:
                json.dump({"boot_id": "boot-a", "uptime": 100.0}, handle)
            checks = iter((None, None, "sensor-error"))

            def reason():
                value = next(checks)
                if value == "sensor-error":
                    self.module.LAST_SENSOR_ERROR = {
                        "sensor": "ps",
                        "failure_kind": "nonzero-exit",
                        "returncode": 1,
                    }
                return value

            poweroff = mock.Mock(return_value=mock.Mock(returncode=0))
            with mock.patch.object(self.module, "busy_reason", side_effect=reason), \
                    mock.patch.object(self.module, "boot_id",
                                      return_value="boot-a"), \
                    mock.patch.object(self.module, "uptime_seconds",
                                      return_value=2000.0), \
                    mock.patch.object(self.module.time, "sleep"), \
                    mock.patch.object(self.module.subprocess, "run", poweroff):
                self.assertEqual(self.module.main(), 0)
            poweroff.assert_not_called()
            self.assertFalse(os.path.exists(self.module.IDLE_STATE))
            with open(self.module.EVENT_LOG, encoding="utf-8") as handle:
                event = json.loads(handle.readlines()[-1])
            self.assertEqual(event["decision"], "shutdown-cancelled")
            self.assertEqual(event["reason"], "sensor-error")
            self.assertEqual(event["sensor_error"]["sensor"], "ps")


class TestIdleProof(unittest.TestCase):
    def test_failed_poweroff_can_never_be_confirmed(self):
        proof = load_module(
            "w09_idle_proof_test",
            os.path.join(W09, "w09_idle_proof.py"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            proof.EVENT_LOG = os.path.join(tmp, "events.jsonl")
            proof.CONTROL_PLANE_PROOF = os.path.join(tmp, "control.json")
            proof.BOOT_ID_PATH = os.path.join(tmp, "boot_id")
            request = {
                "decision": "poweroff-requested", "boot_id": "boot-a",
                "utc": "2026-07-14T20:00:00+00:00", "idle_for_sec": 1801,
                "reason": "no-ssh-no-inhibitor",
            }
            failure = {
                "decision": "poweroff-failed", "boot_id": "boot-a",
                "utc": "2026-07-14T20:00:01+00:00", "returncode": 1,
            }
            with open(proof.EVENT_LOG, "w") as handle:
                handle.write(json.dumps(request) + "\n")
                handle.write(json.dumps(failure) + "\n")
            with open(proof.BOOT_ID_PATH, "w") as handle:
                handle.write("boot-b\n")
            with open(proof.CONTROL_PLANE_PROOF, "w") as handle:
                json.dump({
                    "operator_observed_state": "stopped",
                    "poweroff_boot_id": "boot-a",
                    "request_utc": request["utc"],
                }, handle)
            with self.assertRaises(SystemExit) as caught:
                proof.main()
            self.assertIn("returned failure", str(caught.exception))

    def test_external_stopped_proof_must_bind_request(self):
        proof = load_module(
            "w09_idle_proof_binding_test",
            os.path.join(W09, "w09_idle_proof.py"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            proof.EVENT_LOG = os.path.join(tmp, "events.jsonl")
            proof.CONTROL_PLANE_PROOF = os.path.join(tmp, "control.json")
            proof.BOOT_ID_PATH = os.path.join(tmp, "boot_id")
            request = {
                "decision": "poweroff-requested", "boot_id": "boot-a",
                "utc": "2026-07-14T20:00:00+00:00", "idle_for_sec": 1801,
                "reason": "no-ssh-no-inhibitor",
            }
            with open(proof.EVENT_LOG, "w") as handle:
                handle.write(json.dumps(request) + "\n")
            with open(proof.BOOT_ID_PATH, "w") as handle:
                handle.write("boot-b\n")
            with open(proof.CONTROL_PLANE_PROOF, "w") as handle:
                json.dump({
                    "operator_observed_state": "stopped",
                    "poweroff_boot_id": "wrong-boot",
                    "request_utc": request["utc"],
                }, handle)
            with self.assertRaises(SystemExit) as caught:
                proof.main()
            self.assertIn("does not bind", str(caught.exception))


class TestShellSyntax(unittest.TestCase):
    def test_bash_scripts_parse(self):
        for name in ("install_on_host.sh", "push_and_install.sh",
                     "acceptance_on_host.sh", "run_acceptance.sh"):
            subprocess.run(["bash", "-n", os.path.join(W09, name)],
                           check=True)


if __name__ == "__main__":
    unittest.main()
