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


def _req(method, url, obj=None, timeout=10):
    data = json.dumps(obj).encode() if obj is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


def _get_text(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


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

    def test_market_feed_tape_ui_is_present_and_real_only(self):
        code, html = _get_text(self.base + "/")
        self.assertEqual(code, 200)
        for marker in (
            "Market Feed Readiness",
            "Market Feed Tape",
            'id="md-max-age"',
            'id="md-max-spread"',
            'id="md-min-size"',
            'id="md-row-limit"',
            'id="md-hot-only"',
            'id="market-tbody"',
            "if(o.synthetic) return false;",
            "yes_bid_dollars",
            "yes_ask_dollars",
            "/api/feed_readiness",
        ):
            self.assertIn(marker, html)

    def test_feed_readiness_api_returns_json_without_secrets(self):
        code, body = _req("GET", self.base + "/api/feed_readiness")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("type"), "feed_readiness")
        self.assertIn(body.get("status"), ("active", "ready", "missing_prerequisites"))
        self.assertIn("checks", body)
        self.assertIn("api_key_id_present", body.get("env", {}))
        self.assertNotIn(os.environ.get("KALSHI_API_KEY_ID", "unlikely-secret"), json.dumps(body))

    def test_pure_tool_runs_and_passes(self):
        code, body = _req("POST", self.base + "/api/run", {"name": "test_fixedpoint"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "pass")

    def test_broken_tool_returns_json_not_crash(self):
        # A tool whose binary is missing/unrunnable must yield a clean JSON
        # error record, never a dropped connection (server-side robustness).
        code, body = _req("POST", self.base + "/api/run", {"name": "test_ring"})
        self.assertEqual(code, 200)
        self.assertIn(body["status"], ("pass", "fail", "error"))
        if body["status"] == "error":
            self.assertTrue(body.get("reason"), "error status must carry a reason")

    # ---- lifecycle readiness source (/api/init compatibility + /api/lifecycle) ----
    _init_cache = None

    def _init(self):
        if TestDashboardServerPolicy._init_cache is None:
            TestDashboardServerPolicy._init_cache = _req(
                "POST", self.base + "/api/init", {}, timeout=120)
        return TestDashboardServerPolicy._init_cache

    def test_api_init_returns_valid_json(self):
        code, body = self._init()
        self.assertEqual(code, 200)
        self.assertEqual(body.get("type"), "lifecycle_status")
        self.assertIn(body.get("status"), ("pass", "fail", "skipped", "not_started", "blocked"))
        stages = body.get("stages")
        self.assertIsInstance(stages, list)
        self.assertEqual([s.get("label") for s in stages], [
            "Kalshi API Updates",
            "API Spec Alignment",
            "Connection / Exchange Evaluation",
            "Core Tests",
            "Data Pipeline",
            "Strategy Shadow",
            "Live Execution Gate",
        ])
        for s in stages:
            self.assertIn("id", s)
            self.assertIn("label", s)
            self.assertIn(s.get("status"), ("pass", "fail", "skipped", "not_started", "blocked"))
            self.assertIn("summary", s)

    def test_api_init_never_contains_live_order(self):
        _, body = self._init()
        live = {t["name"] for t in run_tests.load_registry()
                if t.get("safety") == "live_order"}
        for stage in body["stages"]:
            for check in stage.get("checks", []):
                self.assertNotIn(check.get("name"), live,
                                 "%s (live_order) must never appear in lifecycle" %
                                 check.get("name"))

    def test_api_init_network_steps_skipped_without_flag(self):
        # Server was started WITHOUT --allow-network: Exchange/API steps must
        # be skipped (not run, not failed).
        _, body = self._init()
        self.assertFalse(body["allow_network"])
        by_stage = {s["id"]: s for s in body["stages"]}
        self.assertEqual(by_stage["connection_exchange"]["status"], "skipped")
        by_check = {c["name"]: c for c in by_stage["connection_exchange"]["checks"]}
        for name in ("preflight", "account_info"):
            self.assertIn(name, by_check, "network step %s missing from lifecycle" % name)
            self.assertEqual(by_check[name]["status"], "skipped",
                             "%s must be skipped without --allow-network" % name)

    def test_lifecycle_get_and_run_return_json(self):
        code, body = _req("GET", self.base + "/api/lifecycle")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("type"), "lifecycle_status")
        code, body = _req("POST", self.base + "/api/lifecycle/run",
                          {"run_core_tests": False}, timeout=120)
        self.assertEqual(code, 200)
        self.assertEqual(body.get("type"), "lifecycle_status")
        self.assertIn(body.get("status"), ("pass", "fail", "skipped", "not_started", "blocked"))


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
