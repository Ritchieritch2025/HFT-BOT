#!/usr/bin/env python3
"""Focused offline tests for PIPE-W09 bring-up security contracts."""
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W09 = os.path.join(ROOT, "deploy", "w09")
CANONICAL = os.path.join(ROOT, "tools")


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
                return b"w09-research-runner\n"
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

    def test_wrong_role_inside_expected_profile_fails_closed(self):
        def fake_request(url, method="GET", headers=None, timeout=2.0):
            if url.endswith("/api/token"):
                return b"token"
            if url.endswith("/meta-data/iam/info"):
                return json.dumps({
                    "InstanceProfileArn":
                        "arn:aws:iam::123456789012:instance-profile/"
                        "w09-research-runner",
                }).encode()
            if url.endswith("/iam/security-credentials/"):
                return b"unexpected-broad-role"
            raise AssertionError("credential endpoint must not be reached")

        with mock.patch.object(self.module, "_request", fake_request):
            with self.assertRaises(SystemExit) as caught:
                self.module.IMDSv2Credentials().refresh()
        self.assertIn("attached role", str(caught.exception))

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

    def test_wrapper_forwards_consumer_arguments_without_rewriting(self):
        argv = [
            "research_data", "fetch", "--release",
            "2026-07-13__v3ref__seal-aaaaaaaa__pub-bbbbbbbbbbbbbbbb",
            "--with-rfq",
        ]
        with mock.patch.object(self.module, "refuse_static_credentials"), \
                mock.patch.object(self.module.rd, "main", return_value=19) \
                as consumer_main, \
                mock.patch.object(self.module.rd, "make_store"):
            self.assertEqual(self.module.main(argv), 19)
        consumer_main.assert_called_once_with(argv)


class TestReaderPayload(unittest.TestCase):
    MANIFEST = os.path.join(W09, "research_reader_modules.sha256")
    QUERY_CANARY = os.path.join(W09, "v3_query_canary.py")
    QUERY_CANARY_MANIFEST = os.path.join(W09, "v3_query_canary.sha256")
    MODULES = {
        "tools/research_data.py",
        "tools/research_reference.py",
        "tools/warehouse_common.py",
    }

    def _manifest_rows(self):
        rows = {}
        with open(self.MANIFEST, encoding="ascii") as handle:
            for line in handle:
                digest, relative = line.rstrip("\n").split("  ", 1)
                rows[relative] = digest
        return rows

    def test_pinned_manifest_is_complete_and_matches_source_bytes(self):
        rows = self._manifest_rows()
        self.assertEqual(set(rows), self.MODULES)
        for relative, expected in rows.items():
            with open(os.path.join(ROOT, relative), "rb") as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(),
                                 expected, relative)

    def test_payload_module_set_imports_from_an_isolated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = os.path.join(tmp, "tools")
            os.makedirs(tools_dir)
            for relative in self._manifest_rows():
                shutil.copy2(os.path.join(ROOT, relative), tools_dir)
            result = subprocess.run([
                sys.executable, "-c",
                "import research_data as rd, research_reference as rr; "
                "assert rd.ref is rr; print(rr.SCHEMA)",
            ], env={**os.environ, "PYTHONPATH": tools_dir},
                check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(),
                             "research-release-manifest-v3-reference")

    def test_query_canary_payload_is_exact_sha256_pinned(self):
        with open(self.QUERY_CANARY_MANIFEST, encoding="ascii") as handle:
            rows = [line.rstrip("\n").split("  ", 1)
                    for line in handle if line.strip()]
        with open(self.QUERY_CANARY, "rb") as handle:
            expected = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(rows, [[
            expected, "deploy/w09/v3_query_canary.py",
        ]])

    def test_push_and_install_cover_every_module_with_read_only_mode(self):
        with open(os.path.join(W09, "push_and_install.sh"),
                  encoding="utf-8") as handle:
            push = handle.read()
        with open(os.path.join(W09, "install_on_host.sh"),
                  encoding="utf-8") as handle:
            install = handle.read()
        for relative in self.MODULES:
            name = os.path.basename(relative)
            self.assertIn(
                'cp "$SOURCE_REPO/tools/%s" "$tmp/tools/"' % name,
                push,
            )
            self.assertIn(
                'install -m 0644 "$PAYLOAD_ROOT/tools/%s"' % name,
                install,
            )
        self.assertIn("sha256sum -c", install)
        self.assertIn("shasum -a 256 -c", push)
        self.assertIn("v3_query_canary.py", push)
        self.assertIn("v3_query_canary.sha256", push)
        self.assertIn("v3_query_canary.py", install)
        self.assertIn("v3_query_canary.sha256", install)
        self.assertIn("/etc/w09/v3_query_canary.sha256", install)

    def test_shell_wrappers_preserve_all_argv(self):
        with open(os.path.join(W09, "w09-run"),
                  encoding="utf-8") as handle:
            inhibitor = handle.read()
        with open(os.path.join(W09, "w09-inhibit-run"),
                  encoding="utf-8") as handle:
            privileged_helper = handle.read()
        with open(os.path.join(W09, "w09-inhibit-run.sudoers"),
                  encoding="utf-8") as handle:
            sudoers = handle.read()
        with open(os.path.join(W09, "install_on_host.sh"),
                  encoding="utf-8") as handle:
            install = handle.read()
        self.assertIn('-- "$@"', inhibitor)
        self.assertIn(
            'sudo -n /usr/local/libexec/w09-inhibit-run "$@"',
            inhibitor)
        self.assertIn('SUDO_USER:-}" != "ubuntu"', privileged_helper)
        self.assertIn("--reuid=ubuntu", privileged_helper)
        self.assertIn("--regid=ubuntu", privileged_helper)
        self.assertIn("--reset-env", privileged_helper)
        self.assertIn('-- "$@"', privileged_helper)
        self.assertEqual(
            sudoers.strip(),
            "ubuntu ALL=(root) NOPASSWD: "
            "/usr/local/libexec/w09-inhibit-run *")
        self.assertIn("visudo -cf /etc/sudoers.d/w09-inhibit-run", install)
        self.assertIn('--cache /srv/w09-research/cache "$@"', install)


class TestReleaseSelection(unittest.TestCase):
    def test_strict_v3_gate_rejects_v2_only_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "2026-07-15__seal-eeeeeeee__pub-3333333333333333"
            path = os.path.join(tmp, "releases", rid)
            os.makedirs(path)
            with open(os.path.join(path, "MANIFEST.json"), "w") as handle:
                json.dump({
                    "schema_version": "research-release-manifest-v2",
                    "release_id": rid,
                    "date": "2026-07-15",
                    "generated_at_utc": "2026-07-16T05:00:00Z",
                    "version_binding": {"mode": "VERSION_BOUND"},
                    "objects": [{"size": 17}],
                }, handle)
            result = subprocess.run([
                sys.executable, os.path.join(W09, "select_newest_release.py"),
                "--cache", tmp, "--require-v3-reference",
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("strict gate requires", result.stderr)

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
            v3_rid = (
                "2026-07-14__v3ref__seal-dddddddd__pub-2222222222222222"
            )
            # Fresh inventory caches v3 neutrally; it is not materialized into
            # releases/ until exact-version fetch succeeds.
            v3_path = os.path.join(tmp, "reference_manifests", v3_rid)
            os.makedirs(v3_path)
            with open(os.path.join(v3_path, "MANIFEST.json"), "w") as f:
                json.dump({
                    "schema": "research-release-manifest-v3-reference",
                    "schema_version": 3,
                    "storage_mode": "CANONICAL_REFERENCE",
                    "publication_status": "PUBLISHED",
                    "release_id": v3_rid,
                    "date": "2026-07-14",
                    "published_at_utc": "2026-07-15T03:00:00Z",
                    "version_binding": {"mode": "CANONICAL_REFERENCE"},
                    "evidence": {"tier": "SEALED_CONFIRMATION"},
                    "objects": [{"size": 11}],
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
            self.assertEqual(selected["storage_mode"], "COPIED_V2")
            result = subprocess.run([
                sys.executable, os.path.join(W09, "select_newest_release.py"),
                "--cache", tmp, "--include-v3-reference", "--json",
            ], check=True, capture_output=True, text=True)
            selected = json.loads(result.stdout)
            self.assertEqual(selected["release_id"], v3_rid)
            self.assertEqual(selected["storage_mode"], "REFERENCE_V3")

            # Strict acceptance must not let a newer copied-v2 release outrank
            # the published v3 canonical reference.
            newer_v2_rid = (
                "2026-07-15__seal-eeeeeeee__pub-3333333333333333"
            )
            newer_v2_path = os.path.join(tmp, "releases", newer_v2_rid)
            os.makedirs(newer_v2_path)
            with open(os.path.join(newer_v2_path, "MANIFEST.json"), "w") as f:
                json.dump({
                    "schema_version": "research-release-manifest-v2",
                    "release_id": newer_v2_rid,
                    "date": "2026-07-15",
                    "generated_at_utc": "2026-07-16T05:00:00Z",
                    "version_binding": {"mode": "VERSION_BOUND"},
                    "objects": [{"size": 17}],
                }, f)
            result = subprocess.run([
                sys.executable, os.path.join(W09, "select_newest_release.py"),
                "--cache", tmp, "--require-v3-reference", "--json",
            ], check=True, capture_output=True, text=True)
            selected = json.loads(result.stdout)
            self.assertEqual(selected["release_id"], v3_rid)
            self.assertEqual(selected["storage_mode"], "REFERENCE_V3")


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
            mock.Mock(returncode=0, stdout="", stderr=""),  # ss
            mock.Mock(returncode=0, stdout="", stderr=""),  # loginctl
            mock.Mock(returncode=1, stdout="", stderr=""),  # ps: no match
            mock.Mock(returncode=0, stdout="", stderr=""),  # inhibitors
        ]
        with mock.patch.object(self.module.os.path, "exists",
                               return_value=False), \
                mock.patch.object(self.module.subprocess, "run",
                                  side_effect=responses):
            self.assertIsNone(self.module.busy_reason())

    def test_procps_rc1_with_error_still_fails_closed_and_names_sensor(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="permission denied"),
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
    def test_acceptance_is_v3_only_and_keeps_rfq_off(self):
        with open(os.path.join(W09, "acceptance_on_host.sh"),
                  encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("cache-v3-canary", script)
        self.assertGreaterEqual(script.count("--require-v3-reference"), 2)
        self.assertNotIn("--include-v3-reference", script)
        self.assertNotIn("--with-rfq", script)
        self.assertIn('marker.get("storage_mode") != "REFERENCE_V3"', script)
        self.assertIn(
            'marker.get("version_binding_mode") != "CANONICAL_REFERENCE"',
            script)
        self.assertIn('marker.get("rfq_included") is not False', script)
        self.assertIn("/etc/w09/v3_query_canary.sha256", script)
        self.assertIn("v3_query_canary.py", script)
        self.assertIn("W09_V3_DUCKDB_QUERY_CANARY_PASS", script)
        self.assertLess(script.index('verify --release "$RID"'),
                        script.index("v3_query_canary.py"))

    def test_bash_scripts_parse(self):
        for name in ("install_on_host.sh", "push_and_install.sh",
                     "acceptance_on_host.sh", "run_acceptance.sh"):
            subprocess.run(["bash", "-n", os.path.join(W09, name)],
                           check=True)


if __name__ == "__main__":
    unittest.main()
