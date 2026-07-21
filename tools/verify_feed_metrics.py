#!/usr/bin/env python3
"""Verify that ws_shadow wrote real market-feed telemetry for the dashboard.

This checks the NDJSON metrics file consumed by dashboard_server.py. It is
intentionally separate from verify_ws_capture.py:

  - verify_ws_capture.py proves the raw recorder capture is structurally clean.
  - verify_feed_metrics.py proves the operator-facing feed panel has fresh,
    non-synthetic kalshi_ws feed and market_data rows.

Usage:
  verify_feed_metrics.py METRICS.ndjson [--source kalshi_ws]
      [--capture CAPTURE.ndjson] [--tickers T1,T2] [--max-age-ms N]

stdlib only. Prints PASS:/FAIL: lines and a final ALL PASS or VERIFY FEED
METRICS FAIL so it fits the repo's test/tool conventions.
"""
import argparse
import json
import os
import sys
import time


def load_ndjson(path):
    """Return (records, parse_errors). Blank lines are skipped."""
    records, errors = [], []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError as e:
                errors.append("line %d: %s" % (lineno, e))
    return records, errors


def _norm_path(path):
    return os.path.normpath(str(path)).replace("\\", "/")


def _capture_matches(row_capture, expected_capture):
    if not expected_capture:
        return True
    if not row_capture:
        return False
    return _norm_path(row_capture) == _norm_path(expected_capture)


def _is_real_source(record, source):
    return (
        isinstance(record, dict)
        and record.get("source") == source
        and record.get("synthetic") is False
    )


def verify_metrics(records, source="kalshi_ws", capture=None, tickers=None,
                   max_age_ms=None, now_ms=None):
    """Validate parsed dashboard metrics rows.

    Args:
      records: list of dicts parsed from NDJSON.
      source: expected feed source, normally kalshi_ws.
      capture: optional capture path that the feed rows must reference.
      tickers: optional list of expected market tickers.
      max_age_ms: optional freshness bound for the newest real feed row.
      now_ms: test override for current wall-clock milliseconds.

    Returns (ok: bool, lines: list[str]).
    """
    lines = []
    ok = True
    tickers = [t for t in (tickers or []) if t]

    def check(cond, msg):
        nonlocal ok
        lines.append(("PASS: " if cond else "FAIL: ") + msg)
        if not cond:
            ok = False

    real_rows = [r for r in records if _is_real_source(r, source)]
    feed_rows = [r for r in real_rows if r.get("type") == "feed"]
    data_rows = [r for r in real_rows if r.get("type") == "market_data"]

    check(bool(feed_rows),
          "metrics contains non-synthetic %s feed rows (%d)" %
          (source, len(feed_rows)))
    check(bool(data_rows),
          "metrics contains non-synthetic %s market_data rows (%d)" %
          (source, len(data_rows)))
    if not feed_rows or not data_rows:
        return ok, lines

    healthy = [
        r for r in feed_rows
        if r.get("connected") is True and r.get("valid") is True and
        int(r.get("messages", 0) or 0) > 0
    ]
    check(bool(healthy),
          "at least one feed heartbeat is connected, valid, and has messages")

    dropped = [
        r for r in feed_rows
        if int(r.get("recorder_dropped", 0) or 0) > 0 or
        int(r.get("telemetry_dropped", 0) or 0) > 0
    ]
    check(not dropped, "no recorder/telemetry drops reported in feed rows")

    if capture is not None:
        matched = [r for r in feed_rows if _capture_matches(r.get("capture"), capture)]
        check(bool(matched), "feed rows reference capture %s" % capture)

    seen_tickers = sorted({
        str(r.get("market_ticker")) for r in data_rows if r.get("market_ticker")
    })
    check(bool(seen_tickers),
          "market_data rows include market_ticker values (%d unique)" %
          len(seen_tickers))
    for ticker in tickers:
        check(ticker in seen_tickers,
              "expected ticker %s appears in market_data rows" % ticker)

    channels = {}
    for r in data_rows:
        ch = str(r.get("channel") or "?")
        channels[ch] = channels.get(ch, 0) + 1
    lines.append("PASS: market_data channels " + ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(channels.items())))

    ts_values = [
        int(r.get("ts_ms")) for r in feed_rows + data_rows
        if isinstance(r.get("ts_ms"), int)
    ]
    if max_age_ms is not None:
        check(bool(ts_values), "rows include ts_ms for freshness check")
        if ts_values:
            now = int(time.time() * 1000) if now_ms is None else int(now_ms)
            age = max(0, now - max(ts_values))
            check(age <= int(max_age_ms),
                  "latest %s row is fresh enough (%dms <= %dms)" %
                  (source, age, int(max_age_ms)))
    elif ts_values:
        lines.append("PASS: newest %s row ts_ms=%d" % (source, max(ts_values)))

    return ok, lines


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("metrics", help="dashboard metrics NDJSON path")
    ap.add_argument("--source", default="kalshi_ws")
    ap.add_argument("--capture")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--max-age-ms", type=int)
    args = ap.parse_args(argv[1:])

    try:
        records, errors = load_ndjson(args.metrics)
    except OSError as e:
        print("FAIL: cannot read %s: %s" % (args.metrics, e))
        print("VERIFY FEED METRICS FAIL")
        return 1

    tickers = [t for t in args.tickers.split(",") if t]
    ok, lines = verify_metrics(
        records, source=args.source, capture=args.capture,
        tickers=tickers, max_age_ms=args.max_age_ms)
    for e in errors:
        print("FAIL: malformed NDJSON %s" % e)
        ok = False
    for line in lines:
        print(line)
    print("ALL PASS" if ok else "VERIFY FEED METRICS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
