#!/usr/bin/env python3
"""Ops-console backend tests (PLAN_PROD_V1 P2): output parser, safety policy,
registry round-trip. stdlib unittest, no network.

Run: python3 tests/test_console.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import run_tests  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(method, url, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class TestOutputParser(unittest.TestCase):
    def test_counts_pass_fail(self):
        out = "PASS: a\nPASS: b\nFAIL: c\nALL PASS\n"
        p, f, ok = run_tests.parse_output(out, "ALL PASS")
        self.assertEqual((p, f), (2, 1))
        self.assertTrue(ok)

    def test_failures_sentinel_fails(self):
        out = "PASS: a\nFAIL: b\nFAILURES\n"
        p, f, ok = run_tests.parse_output(out, "ALL PASS")
        self.assertEqual((p, f), (1, 1))
        self.assertFalse(ok, "FAILURES sentinel must mark the suite failed")

    def test_missing_pass_token_fails(self):
        _, _, ok = run_tests.parse_output("PASS: a\n(no final token)\n", "ALL PASS")
        self.assertFalse(ok)

    def test_custom_pass_token(self):
        _, _, ok = run_tests.parse_output("...\nWS SMOKE PASS\n", "WS SMOKE PASS")
        self.assertTrue(ok)

    def test_no_token_required(self):
        # Benches have no pass_token -> token check is vacuously true.
        _, _, ok = run_tests.parse_output("500000 msgs 320 ns/msg\n", None)
        self.assertTrue(ok)

    def test_truncated_log(self):
        _, _, ok = run_tests.parse_output("PASS: a\nPASS", "ALL PASS")  # cut mid-line
        self.assertFalse(ok)


class TestSafetyPolicy(unittest.TestCase):
    def test_pure_offline_runnable(self):
        self.assertTrue(run_tests.may_run({"safety": "pure"})[0])
        self.assertTrue(run_tests.may_run({"safety": "offline"})[0])

    def test_network_read_needs_flag(self):
        self.assertFalse(run_tests.may_run({"safety": "network_read"}, allow_network=False)[0])
        self.assertTrue(run_tests.may_run({"safety": "network_read"}, allow_network=True)[0])

    def test_live_order_never_runnable(self):
        self.assertFalse(run_tests.may_run({"safety": "live_order"}, allow_network=False)[0])
        self.assertFalse(run_tests.may_run({"safety": "live_order"}, allow_network=True)[0],
                         "live_order must be refused EVEN with --allow-network")

    def test_unknown_safety_refused(self):
        self.assertFalse(run_tests.may_run({"safety": "whatever"})[0])


class TestRegistryRoundTrip(unittest.TestCase):
    def test_registry_loads_and_is_classified(self):
        tools = run_tests.load_registry()
        self.assertGreater(len(tools), 0)
        for t in tools:
            self.assertIn("name", t)
            self.assertIn(t["safety"], {"pure", "offline", "network_read", "live_order"})

    def test_every_live_order_is_refused(self):
        for t in run_tests.load_registry():
            if t["safety"] == "live_order":
                self.assertFalse(run_tests.may_run(t, allow_network=True)[0],
                                 "%s (live_order) must never run" % t["name"])

    def test_runnable_set_excludes_dangerous(self):
        runnable = run_tests.runnable_test_set(run_tests.load_registry())
        for t in runnable:
            self.assertIn(t["safety"], {"pure", "offline"})


class TestDashboardServerPolicy(unittest.TestCase):
    """Start the real server and prove safety is enforced SERVER-SIDE, not just
    in the UI (guardrail 2)."""
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        # Start WITHOUT --allow-network so network_read is also refused.
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "dashboard_server.py"),
             "--port", str(cls.port), "--host", "127.0.0.1",
             "--results", os.path.join(ROOT, "work", "test_results.ndjson")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        base = "http://127.0.0.1:%d" % cls.port
        cls.base = base
        for _ in range(50):
            try:
                if _req("GET", base + "/healthz")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=3)
        except Exception:
            cls.proc.kill()

    def test_live_order_post_returns_403(self):
        code, body = _req("POST", self.base + "/api/run", {"name": "fill_test"})
        self.assertEqual(code, 403, "fill_test (live_order) must be refused server-side")
        self.assertEqual(body.get("error"), "refused")

    def test_tradingd_post_returns_403(self):
        code, _ = _req("POST", self.base + "/api/run", {"name": "tradingd"})
        self.assertEqual(code, 403, "tradingd (live_order) must be refused")

    def test_network_read_refused_without_flag(self):
        code, _ = _req("POST", self.base + "/api/run", {"name": "account_info"})
        self.assertEqual(code, 403, "network_read refused without --allow-network")

    def test_unknown_tool_404(self):
        code, _ = _req("POST", self.base + "/api/run", {"name": "nope"})
        self.assertEqual(code, 404)

    def test_api_tools_lists_runnable_flags(self):
        code, body = _req("GET", self.base + "/api/tools")
        self.assertEqual(code, 200)
        self.assertFalse(body["allow_network"])
        flags = {t["name"]: t["runnable"] for t in body["tools"]}
        self.assertFalse(flags["fill_test"], "live_order not runnable")
        self.assertTrue(flags["test_ring"], "pure test runnable")

    def test_pure_tool_runs_and_passes(self):
        code, body = _req("POST", self.base + "/api/run", {"name": "test_fixedpoint"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "pass")


if __name__ == "__main__":
    # Emit the house PASS:/ALL PASS convention so run_pipeline can parse it.
    result = unittest.main(exit=False, verbosity=0).result
    ran = result.testsRun
    bad = len(result.failures) + len(result.errors)
    for _ in range(ran - bad):
        print("PASS: console test")
    for fail in result.failures + result.errors:
        print("FAIL: %s" % (fail[0],))
    print("ALL PASS" if bad == 0 else "FAILURES")
    sys.exit(1 if bad else 0)
