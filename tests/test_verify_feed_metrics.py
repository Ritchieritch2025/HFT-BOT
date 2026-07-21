#!/usr/bin/env python3
"""Offline tests for tools/verify_feed_metrics.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import verify_feed_metrics as v  # noqa: E402

NOW = 1_783_300_000_000
CAPTURE = "work/exchange_check_capture.ndjson"


def feed(**kw):
    row = {
        "type": "feed",
        "ts_ms": NOW,
        "synthetic": False,
        "source": "kalshi_ws",
        "connected": True,
        "valid": True,
        "freshness_ms": 10,
        "messages": 3,
        "recorder_dropped": 0,
        "telemetry_dropped": 0,
        "capture": CAPTURE,
    }
    row.update(kw)
    return row


def md(ticker="MKT-A", channel="orderbook_snapshot", **kw):
    row = {
        "type": "market_data",
        "ts_ms": NOW,
        "synthetic": False,
        "source": "kalshi_ws",
        "channel": channel,
        "market_ticker": ticker,
        "valid": True,
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4500",
    }
    row.update(kw)
    return row


class TestVerifyFeedMetrics(unittest.TestCase):
    def test_clean_feed_metrics_pass(self):
        ok, lines = v.verify_metrics(
            [feed(), md(), md(channel="trade")],
            capture=CAPTURE, tickers=["MKT-A"], max_age_ms=1000, now_ms=NOW + 50)
        self.assertTrue(ok, "\n".join(lines))

    def test_final_disconnect_is_ok_when_session_had_healthy_heartbeat(self):
        ok, lines = v.verify_metrics(
            [feed(), feed(ts_ms=NOW + 1, connected=False, valid=False), md()],
            capture=CAPTURE)
        self.assertTrue(ok, "\n".join(lines))

    def test_missing_feed_rows_fails(self):
        ok, lines = v.verify_metrics([md()])
        self.assertFalse(ok)
        self.assertTrue(any("feed rows" in ln and ln.startswith("FAIL") for ln in lines))

    def test_missing_market_data_rows_fails(self):
        ok, lines = v.verify_metrics([feed()])
        self.assertFalse(ok)
        self.assertTrue(any("market_data" in ln and ln.startswith("FAIL") for ln in lines))

    def test_synthetic_rows_do_not_count(self):
        ok, lines = v.verify_metrics([feed(synthetic=True), md(synthetic=True)])
        self.assertFalse(ok)
        self.assertTrue(any(ln.startswith("FAIL") for ln in lines))

    def test_capture_mismatch_fails(self):
        ok, lines = v.verify_metrics([feed(capture="other.ndjson"), md()], capture=CAPTURE)
        self.assertFalse(ok)
        self.assertTrue(any("capture" in ln and ln.startswith("FAIL") for ln in lines))

    def test_expected_ticker_must_appear(self):
        ok, lines = v.verify_metrics([feed(), md("MKT-A")], tickers=["MKT-B"])
        self.assertFalse(ok)
        self.assertTrue(any("MKT-B" in ln and ln.startswith("FAIL") for ln in lines))

    def test_stale_metrics_fail_when_max_age_is_requested(self):
        ok, lines = v.verify_metrics(
            [feed(ts_ms=NOW - 10_000), md(ts_ms=NOW - 10_000)],
            max_age_ms=1000, now_ms=NOW)
        self.assertFalse(ok)
        self.assertTrue(any("fresh enough" in ln and ln.startswith("FAIL") for ln in lines))

    def test_cli_end_to_end(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as fh:
            for row in [feed(), md()]:
                fh.write(json.dumps(row) + "\n")
            path = fh.name
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "verify_feed_metrics.py"),
                 path, "--capture", CAPTURE, "--tickers", "MKT-A"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("ALL PASS", proc.stdout)
        finally:
            os.unlink(path)

    def test_malformed_json_line_fails_cli(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as fh:
            fh.write(json.dumps(feed()) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps(md()) + "\n")
            path = fh.name
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "verify_feed_metrics.py"),
                 path],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("VERIFY FEED METRICS FAIL", proc.stdout)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    ran = result.testsRun
    bad = len(result.failures) + len(result.errors)
    for _ in range(ran - bad):
        print("PASS: verify_feed_metrics test")
    for fail in result.failures + result.errors:
        print("FAIL: %s" % (fail[0],))
    print("ALL PASS" if bad == 0 else "FAILURES")
    sys.exit(1 if bad else 0)
