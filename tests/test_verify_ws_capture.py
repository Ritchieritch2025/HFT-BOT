#!/usr/bin/env python3
"""Offline tests for tools/verify_ws_capture.py (PLAN_LIVE_VALIDATION P2).

Fixture-driven, stdlib only, no network: builds synthetic capture records and
asserts the validator's PASS/FAIL verdict for clean streams, a missing snapshot,
an unexplained vs. gap-accounted seq hole, duplicate seqs, and loss/epoch markers.

Run: python3 tests/test_verify_ws_capture.py
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import verify_ws_capture as v  # noqa: E402

WALL0 = 1_700_000_000_000_000_000  # arbitrary fixed base ns (deterministic)


def frame(ticker, sid, seq, channel="orderbook_delta", dt_ns=1_000_000):
    return {
        "recv_wall_ns": WALL0 + seq * dt_ns,
        "source": "kalshi",
        "channel": channel,
        "source_ticker": ticker,
        "sid": sid,
        "source_sequence": seq,
        "raw": "{}",
    }


def marker(kind, sid=None, seq=None):
    m = {"recv_wall_ns": WALL0, "source": "kalshi", "marker": kind}
    if sid is not None:
        m["sid"] = sid
    if seq is not None:
        m["source_sequence"] = seq
    return m


def clean_stream(ticker="KXTEST-A", sid=1, n=10):
    """Snapshot then n consecutive deltas on one sid."""
    recs = [frame(ticker, sid, 1, channel="orderbook_snapshot")]
    recs += [frame(ticker, sid, s) for s in range(2, n + 1)]
    return recs


class TestVerifyCapture(unittest.TestCase):
    def test_clean_single_ticker_passes(self):
        ok, lines = v.verify_capture(clean_stream())
        self.assertTrue(ok, "\n".join(lines))

    def test_clean_two_tickers_passes(self):
        recs = clean_stream("KXTEST-A", sid=1) + clean_stream("KXTEST-B", sid=2)
        ok, lines = v.verify_capture(recs, tickers=["KXTEST-A", "KXTEST-B"])
        self.assertTrue(ok, "\n".join(lines))

    def test_missing_snapshot_fails(self):
        # Only deltas, no orderbook_snapshot for the ticker.
        recs = [frame("KXTEST-A", 1, s) for s in range(1, 6)]
        ok, lines = v.verify_capture(recs, tickers=["KXTEST-A"])
        self.assertFalse(ok)
        self.assertTrue(any("orderbook_snapshot" in ln and ln.startswith("FAIL") for ln in lines))

    def test_unexplained_seq_hole_fails(self):
        recs = clean_stream(n=5)
        recs.append(frame("KXTEST-A", 1, 8))  # jump 5 -> 8, no gap marker
        ok, lines = v.verify_capture(recs)
        self.assertFalse(ok)
        self.assertTrue(any("gap marker" in ln and ln.startswith("FAIL") for ln in lines))

    def test_gap_accounted_hole_passes(self):
        recs = clean_stream(n=5)
        recs.append(frame("KXTEST-A", 1, 8))  # jump 5 -> 8
        recs.append(marker("gap", sid=1))     # ...explained by a gap marker
        ok, lines = v.verify_capture(recs)
        self.assertTrue(ok, "\n".join(lines))

    def test_duplicate_seq_fails(self):
        recs = clean_stream(n=5)
        recs.append(frame("KXTEST-A", 1, 5))  # duplicate seq 5
        ok, lines = v.verify_capture(recs)
        self.assertFalse(ok)
        self.assertTrue(any("duplicate" in ln and ln.startswith("FAIL") for ln in lines))

    def test_loss_marker_fails_and_is_counted(self):
        recs = clean_stream()
        recs.append(marker("loss", seq=3))
        recs.append(marker("loss", seq=2))
        ok, lines = v.verify_capture(recs)
        self.assertFalse(ok)
        loss_lines = [ln for ln in lines if "loss markers" in ln]
        self.assertTrue(loss_lines and loss_lines[0].startswith("FAIL"))
        self.assertIn("count=2", loss_lines[0])

    def test_epoch_change_marker_fails(self):
        recs = clean_stream()
        recs.append(marker("epoch_change"))
        ok, lines = v.verify_capture(recs)
        self.assertFalse(ok)
        self.assertTrue(any("epoch_change" in ln and ln.startswith("FAIL") for ln in lines))

    def test_min_span_seconds_enforced(self):
        # clean_stream spans ~9ms; requiring 30s must fail.
        ok, lines = v.verify_capture(clean_stream(), min_span_seconds=30.0)
        self.assertFalse(ok)
        self.assertTrue(any("wall time" in ln and ln.startswith("FAIL") for ln in lines))

    def test_unexpected_ticker_fails(self):
        recs = clean_stream("KXTEST-A", sid=1) + clean_stream("KXSURPRISE", sid=2)
        ok, lines = v.verify_capture(recs, tickers=["KXTEST-A"])
        self.assertFalse(ok)
        self.assertTrue(any("unexpected ticker" in ln and ln.startswith("FAIL") for ln in lines))

    def test_cli_end_to_end(self):
        # Exercise main() through the real CLI on a clean fixture file.
        import json
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as fh:
            for r in clean_stream():
                fh.write(json.dumps(r) + "\n")
            path = fh.name
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "verify_ws_capture.py"), path],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("ALL PASS", proc.stdout)
        finally:
            os.unlink(path)

    def test_malformed_json_line_fails_cli(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as fh:
            fh.write('{"channel":"orderbook_snapshot"}\n')
            fh.write("not json at all\n")
            path = fh.name
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "verify_ws_capture.py"), path],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("VERIFY FAIL", proc.stdout)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    ran = result.testsRun
    bad = len(result.failures) + len(result.errors)
    for _ in range(ran - bad):
        print("PASS: verify_ws_capture test")
    for fail in result.failures + result.errors:
        print("FAIL: %s" % (fail[0],))
    print("ALL PASS" if bad == 0 else "FAILURES")
    sys.exit(1 if bad else 0)
