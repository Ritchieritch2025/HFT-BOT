#!/usr/bin/env python3
"""Offline tests for tools/feed_readiness.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import feed_readiness as fr  # noqa: E402

NOW = 1_783_400_000_000


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("")


def _env(key_path):
    return {
        "KALSHI_ENV": "prod",
        "KALSHI_API_KEY_ID": "secret-key-id-that-must-not-print",
        "KALSHI_PRIVATE_KEY_PATH": key_path,
    }


def _write_metrics(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _feed(ts=NOW, **kw):
    row = {
        "type": "feed",
        "source": "kalshi_ws",
        "synthetic": False,
        "ts_ms": ts,
        "connected": True,
        "valid": True,
    }
    row.update(kw)
    return row


def _market(ts=NOW, **kw):
    row = {
        "type": "market_data",
        "source": "kalshi_ws",
        "synthetic": False,
        "ts_ms": ts,
        "market_ticker": "MKT-A",
    }
    row.update(kw)
    return row


class TestFeedReadiness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feed-ready-")
        self.root = os.path.join(self.tmp, "repo")
        self.metrics = os.path.join(self.root, "work", "metrics.ndjson")
        self.key = os.path.join(self.tmp, "key.pem")
        _touch(os.path.join(self.root, "build", "ws_shadow"))
        _touch(os.path.join(self.root, "tools", "exchange_check.sh"))
        _touch(self.key)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_credentials_is_not_ready(self):
        s = fr.collect_status(self.root, self.metrics, env={}, now=NOW)
        self.assertEqual(s["status"], "missing_prerequisites")
        self.assertFalse(s["env"]["api_key_id_present"])
        ok, lines = fr.report_lines(s)
        self.assertFalse(ok)
        self.assertTrue(any("KALSHI_API_KEY_ID is missing" in ln for ln in lines))

    def test_ready_when_local_prereqs_exist_even_before_feed_active(self):
        s = fr.collect_status(self.root, self.metrics, env=_env(self.key), now=NOW)
        self.assertEqual(s["status"], "ready")
        self.assertFalse(s["metrics"]["active"])
        self.assertTrue(fr.report_lines(s)[0])

    def test_active_when_fresh_real_feed_and_market_rows_exist(self):
        _write_metrics(self.metrics, [_feed(), _market()])
        s = fr.collect_status(self.root, self.metrics, env=_env(self.key), now=NOW + 100)
        self.assertEqual(s["status"], "active")
        self.assertTrue(s["metrics"]["active"])
        self.assertEqual(s["metrics"]["real_feed_rows"], 1)
        self.assertEqual(s["metrics"]["real_market_rows"], 1)

    def test_synthetic_rows_do_not_make_feed_active(self):
        _write_metrics(self.metrics, [
            dict(_feed(), synthetic=True),
            dict(_market(), synthetic=True),
        ])
        s = fr.collect_status(self.root, self.metrics, env=_env(self.key), now=NOW + 100)
        self.assertEqual(s["status"], "ready")
        self.assertFalse(s["metrics"]["active"])

    def test_stale_rows_do_not_make_feed_active(self):
        _write_metrics(self.metrics, [_feed(ts=NOW - 10_000), _market(ts=NOW - 10_000)])
        s = fr.collect_status(self.root, self.metrics, env=_env(self.key), now=NOW)
        self.assertEqual(s["status"], "ready")
        self.assertFalse(s["metrics"]["active"])

    def test_json_output_redacts_key_id(self):
        env = os.environ.copy()
        env.update(_env(self.key))
        # This path invokes the real CLI clock, unlike the unit cases above
        # that inject the frozen NOW fixture.  Keep the fixture fresh at
        # execution time so the test does not start failing merely because
        # the calendar advanced after it was authored.
        current = fr.now_ms()
        _write_metrics(
            self.metrics, [_feed(ts=current), _market(ts=current)])
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "feed_readiness.py"),
             "--metrics", self.metrics, "--json"],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('"api_key_id_present": true', proc.stdout)
        self.assertNotIn("secret-key-id-that-must-not-print", proc.stdout)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    ran = result.testsRun
    bad = len(result.failures) + len(result.errors)
    for _ in range(ran - bad):
        print("PASS: feed_readiness test")
    for fail in result.failures + result.errors:
        print("FAIL: %s" % (fail[0],))
    print("ALL PASS" if bad == 0 else "FAILURES")
    sys.exit(1 if bad else 0)
