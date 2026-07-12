#!/usr/bin/env python3
"""WP-03 Freshness Monitor — ONE number: seconds since the newest staging row.

The permanent alarm for the 2026-07 staleness incident class:
  * rotation-shard glob miss  — capture fine, staging silently 31 min behind
  * reader-lock crash-loop    — capture fine, ingest daemon dead, staging stalled
Both were (b)-fresh/(a)-stale, so TWO lags are measured and BOTH are printed:
  (a) staging lag = now − max(ts_utc) over the staging fact tables
      (orderbooks_l1, trades, orderbooks_full — the max over all of them;
      ts_utc is epoch MICROSECONDS per docs/warehouse_schema.md)
  (b) capture lag = now − mtime of the newest raw file under
      work/raw/date=<today>/ (yesterday's dir is also scanned so a check in
      the first seconds after UTC midnight does not false-alarm). The glob is
      firehose_*.ndjson*: rotation shards firehose_HH.ndjson.N count, while
      independent low-rate RFQ/L2 families can never mask a dead firehose.
Alert when EITHER lag exceeds the threshold: verdict STALE + exit 1.
Fresh: verdict FRESH + exit 0 (FRESH is the registry pass_token; the exit
code is authoritative). Anything unmeasurable — missing staging DB, empty
fact tables, no raw files, staging unreadable after the lock-retry window —
is STALE, loudly (GUARDRAILS S2 fail-closed: a metric we cannot read is
never reported green; D2: silence is the failure class this tool exists
to kill).

The live ingest daemon holds the DuckDB write lock in bursts, so the
read-only connect retries with patience (same lesson as ingest.py's
connect_with_retry, reader side). Read-only everywhere; this tool never
writes anything (safety class: pure).

Usage:
  python3 tools/freshness.py [--threshold 600] [--json]
  --staging/--raw-root override config; --now (epoch seconds) injects time
  for deterministic tests. --json emits one machine-readable object for
  WP-08's daily quality check.
stdlib + duckdb only.
"""
import argparse
import datetime
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

FACT_TABLES = ("orderbooks_l1", "trades", "orderbooks_full")
DEFAULT_THRESHOLD_S = 600.0


def connect_read_only_with_retry(duckdb, path, attempts=12, sleep_s=5.0):
    """Reader connect that survives writer-held locks (mirror of ingest.py's
    connect_with_retry: the daemon holds the lock only inside a processing
    window, so a patient reader always gets in between cycles). Returns a
    connection or None after the window is exhausted — the caller treats
    None as STALE, never as fresh."""
    for i in range(attempts):
        try:
            return duckdb.connect(path, read_only=True)
        except Exception:  # duckdb.IOException has no stable import path
            if i == 0:
                print("[freshness] staging locked by the writer; retrying up "
                      "to %ds" % int(attempts * sleep_s), file=sys.stderr)
            if i + 1 < attempts:
                time.sleep(sleep_s)
    return None


def staging_newest(staging_db, attempts, sleep_s):
    """(newest_us, table_name, error) — max(ts_utc) over all fact tables."""
    if not os.path.exists(staging_db):
        return None, None, "staging DB missing: %s" % staging_db
    import duckdb
    con = connect_read_only_with_retry(duckdb, staging_db, attempts, sleep_s)
    if con is None:
        return None, None, ("staging unreadable (lock held for the whole "
                            "%.0fs retry window): %s"
                            % (attempts * sleep_s, staging_db))
    newest, table = None, None
    try:
        for t in FACT_TABLES:
            try:
                mx = con.execute("SELECT max(ts_utc) FROM %s" % t).fetchone()[0]
            except Exception:
                continue  # table absent in a minimal staging — not an error
            if mx is not None and (newest is None or mx > newest):
                newest, table = mx, t
    finally:
        con.close()
    if newest is None:
        return None, None, "staging has no fact rows: %s" % staging_db
    return int(newest), table, None


def capture_newest(raw_root, now_s):
    """Newest firehose file for primary capture health.

    Other channel families have their own liveness contracts.  Looking at an
    arbitrary ``*.ndjson*`` file would let a healthy RFQ recorder make a dead
    production firehose look green (GUARDRAILS D2).
    """
    today = datetime.datetime.fromtimestamp(
        now_s, tz=datetime.timezone.utc).date()
    newest_path, newest_mtime = None, None
    for d in (today, today - datetime.timedelta(days=1)):
        day_dir = wc.raw_day_dir(raw_root, d.isoformat())
        for p in glob.glob(os.path.join(day_dir, "firehose_*.ndjson*")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue  # rotated away mid-scan
            if newest_mtime is None or m > newest_mtime:
                newest_path, newest_mtime = p, m
    if newest_path is None:
        return None, None, ("no firehose raw capture files under %s for %s "
                            "or the prior day" % (raw_root, today.isoformat()))
    return newest_path, newest_mtime, None


def iso_us(ts_us):
    return datetime.datetime.fromtimestamp(
        ts_us / 1e6, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def main(argv):
    ap = argparse.ArgumentParser(
        description="staging + capture freshness check (WP-03)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_S,
                    help="max allowed lag in seconds (default %.0f)"
                         % DEFAULT_THRESHOLD_S)
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output (WP-08 consumes this)")
    ap.add_argument("--staging", default=None,
                    help="staging duckdb path (default from config)")
    ap.add_argument("--raw-root", default=None,
                    help="raw capture root (default from config)")
    ap.add_argument("--now", type=float, default=None,
                    help="epoch seconds to measure against (tests only)")
    ap.add_argument("--lock-attempts", type=int, default=12,
                    help="read-only connect attempts while the writer holds "
                         "the lock (x --lock-sleep seconds each)")
    ap.add_argument("--lock-sleep", type=float, default=5.0)
    args = ap.parse_args(argv[1:])

    cfg = wc.load_config()
    staging_db = args.staging or cfg["staging_db"]
    raw_root = args.raw_root or cfg["raw_root"]
    now_s = args.now if args.now is not None else time.time()

    newest_us, table, stag_err = staging_newest(
        staging_db, args.lock_attempts, args.lock_sleep)
    cap_path, cap_mtime, cap_err = capture_newest(raw_root, now_s)

    staging_lag = (now_s - newest_us / 1e6) if newest_us is not None else None
    capture_lag = (now_s - cap_mtime) if cap_mtime is not None else None

    reasons = []
    if staging_lag is None:
        reasons.append("staging lag unmeasurable — %s" % stag_err)
    elif staging_lag > args.threshold:
        reasons.append("staging lag %.1fs > threshold %.0fs (ingest behind "
                       "or dead; capture may still be fine)"
                       % (staging_lag, args.threshold))
    if capture_lag is None:
        reasons.append("capture lag unmeasurable — %s" % cap_err)
    elif capture_lag > args.threshold:
        reasons.append("capture lag %.1fs > threshold %.0fs (capture died)"
                       % (capture_lag, args.threshold))
    verdict = "STALE" if reasons else "FRESH"

    if args.json:
        print(json.dumps({
            "verdict": verdict,
            "threshold_s": args.threshold,
            "now_utc": iso_us(int(now_s * 1e6)),
            "staging_lag_s": round(staging_lag, 3) if staging_lag is not None else None,
            "staging_newest_us": newest_us,
            "staging_newest_ts_utc": iso_us(newest_us) if newest_us is not None else None,
            "staging_newest_table": table,
            "staging_db": staging_db,
            "capture_lag_s": round(capture_lag, 3) if capture_lag is not None else None,
            "capture_newest_file": cap_path,
            "stale_reasons": reasons,
        }, indent=1))
    else:
        if staging_lag is not None:
            print("staging lag: %.1fs  (newest ts_utc %s via %s in %s)"
                  % (staging_lag, iso_us(newest_us), table, staging_db))
        else:
            print("staging lag: UNMEASURABLE — %s" % stag_err)
        if capture_lag is not None:
            print("capture lag: %.1fs  (newest raw file %s)"
                  % (capture_lag, cap_path))
        else:
            print("capture lag: UNMEASURABLE — %s" % cap_err)
        if verdict == "FRESH":
            print("VERDICT: FRESH (all lags <= threshold %.0fs)" % args.threshold)
        else:
            print("VERDICT: STALE")
            for r in reasons:
                print("  - %s" % r)
    return 0 if verdict == "FRESH" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
