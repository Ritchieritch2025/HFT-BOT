#!/usr/bin/env python3
"""W-C2: durable structured capture-gap record + live silence detector/alert.

The 24/7 firehose (ticker+trade, ALL markets) is sub-second-continuous when
healthy — measured max inter-record silence 0.33s (p99.99 ~0.17s) over a healthy
hour. A market-wide silence beyond --min-gap-secs means the capture feed was
DOWN for that window (a wedged socket, a crash, a respawn): data is missing.

This tool is the durable, structured source of truth for capture holes:

  * BUILD (--date / --since): scan raw-feed inter-record silence and write
    work/event_packs/capture_gaps.csv (start_us,end_us) — the authoritative
    source for event_validate's V-EP15 interior-gap check, replacing the coarse
    quality_log text parser. Idempotent per day.
  * LIVE (--live): check whether the feed is silent RIGHT NOW and write a
    dashboard-readable JSON alert (work/live/capture_alert.json). Exit code 2
    when a live gap is active, so a supervisor/cron can act on it.

Read-only over work/raw + work/metrics (D1: never touches raw). A detected hole
is always recorded, never silently dropped (D2). The window is [start_us,end_us]
= (last record before the silence, first record after it); an event overlapping
it is downgraded to `degraded`, never a silent `pass`.
"""
import argparse
import calendar
import csv
from array import array
import datetime as dt
import glob
import json
import os
import sys
import time

RAW_ROOT_DEFAULT = "work/raw"
RECORD_DEFAULT = "work/event_packs/capture_gaps.csv"
ALERT_DEFAULT = "work/live/capture_alert.json"
# Default hole threshold. Well above W-C1's ~20-25s forced-reconnect recovery
# envelope (so normal operation yields an EMPTY record = clean data) and far
# above the 0.33s healthy inter-record max, so anything recorded is a real
# capture failure. Tunable; lower it to also flag sub-minute recovery gaps.
MIN_GAP_SECS_DEFAULT = 60
# Live-alert silence threshold: shorter, to surface an in-progress outage fast.
ALERT_SECS_DEFAULT = 45


def _extract_recv_us(line):
    """recv_wall_ns (2nd field) -> microseconds, via a targeted slice (no full
    JSON parse: raw files are hundreds of MB). Returns None on an unparsable or
    truncated line so a corrupt tail is skipped, never crashes the scan (D3)."""
    i = line.find('"recv_wall_ns":')
    if i < 0:
        return None
    j = i + len('"recv_wall_ns":')
    k = line.find(",", j)
    if k < 0:
        k = line.find("}", j)
    if k < 0:
        return None
    try:
        return int(line[j:k]) // 1000
    except ValueError:
        return None


def find_gaps(times_us, min_gap_us):
    """Pure: given ASCENDING record timestamps (us), return [(start_us,end_us)]
    for every consecutive pair whose spacing exceeds min_gap_us. start = last
    record before the silence, end = first record after it."""
    gaps = []
    prev = None
    for t in times_us:
        if prev is not None and t - prev > min_gap_us:
            gaps.append((prev, t))
        prev = t
    return gaps


def _last_us(path):
    """Last parsable recv_us in a file (tail freshness)."""
    last = None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            u = _extract_recv_us(line)
            if u is not None:
                last = u
    return last


def _raw_files_for_date(date_str, raw_root):
    """All firehose segments for a UTC date (base + rotations). Order does not
    matter — scan_date globally sorts records — so no per-file peeking."""
    d = os.path.join(raw_root, f"date={date_str}")
    return sorted(glob.glob(os.path.join(d, "firehose_*.ndjson")) +
                  glob.glob(os.path.join(d, "firehose_*.ndjson.*")))


def scan_date(date_str, raw_root, min_gap_us):
    """Collect ALL record timestamps for a date, GLOBALLY SORT, then detect
    gaps. Global sort (not sequential-per-file streaming) is required: a
    within-hour respawn/rotation can leave two segments whose time ranges
    OVERLAP, and streaming them back-to-back would invent a false backward
    'gap'. Timestamps are int64 microseconds in an array (8 bytes each) so a
    full day stays memory-cheap. Returns (gaps, stats)."""
    files = _raw_files_for_date(date_str, raw_root)
    times = array("q")
    bad = 0
    for f in files:
        with open(f, "r", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                u = _extract_recv_us(line)
                if u is None:
                    bad += 1
                    continue
                times.append(u)
    ts = sorted(times)
    gaps = find_gaps(ts, min_gap_us)
    return gaps, {"files": len(files), "records": len(ts), "unparsed": bad}


def _day_bounds_us(date_str):
    base = dt.datetime.strptime(date_str, "%Y-%m-%d")
    start = calendar.timegm(base.timetuple()) * 1_000_000
    return start, start + 86_400 * 1_000_000


def write_record(record_path, date_str, gaps):
    """Merge `gaps` for `date_str` into the durable record, idempotently: drop
    any existing rows whose start_us falls in this day, add the fresh ones, keep
    the rest, sorted. Absence of the file = first write."""
    os.makedirs(os.path.dirname(record_path) or ".", exist_ok=True)
    day_start, day_end = _day_bounds_us(date_str)
    kept = []
    if os.path.exists(record_path):
        with open(record_path) as f:
            for r in csv.DictReader(f):
                try:
                    s = int(r["start_us"])
                    e = int(r["end_us"])
                except (KeyError, ValueError):
                    continue
                if not (day_start <= s < day_end):
                    kept.append((s, e))
    rows = sorted(set(kept) | set((int(s), int(e)) for s, e in gaps))
    tmp = record_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_us", "end_us"])
        w.writerows(rows)
    os.replace(tmp, record_path)
    return rows


def _newest_raw_file(raw_root):
    """The most recently written firehose segment across all date dirs."""
    files = glob.glob(os.path.join(raw_root, "date=*", "firehose_*.ndjson")) + \
        glob.glob(os.path.join(raw_root, "date=*", "firehose_*.ndjson.*"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def live_check(raw_root, alert_path, alert_secs, now_us=None):
    """Is capture silent RIGHT NOW? Writes a dashboard-readable alert file and
    returns the status dict. Fail-closed: no raw file at all => `gap` (unknown
    liveness is treated as down, not silently ok)."""
    now_us = now_us if now_us is not None else int(time.time() * 1_000_000)
    newest = _newest_raw_file(raw_root)
    last_us = _last_us(newest) if newest else None
    if last_us is None:
        silent_us = None
        status = "gap"
    else:
        silent_us = now_us - last_us
        status = "gap" if silent_us > alert_secs * 1_000_000 else "ok"
    alert = {
        "status": status,
        "ts_ms": now_us // 1000,
        "last_record_us": last_us,
        "silent_secs": round(silent_us / 1_000_000, 1) if silent_us is not None else None,
        "since_utc": (dt.datetime.utcfromtimestamp(last_us / 1e6).isoformat() + "Z"
                      if last_us is not None else None),
        "alert_secs": alert_secs,
    }
    os.makedirs(os.path.dirname(alert_path) or ".", exist_ok=True)
    tmp = alert_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(alert, f)
    os.replace(tmp, alert_path)
    return alert


def _dates_since(n_days):
    today = dt.datetime.utcnow().date()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]


def main(argv):
    ap = argparse.ArgumentParser(description="Capture-gap record builder + live detector (W-C2)")
    ap.add_argument("--date", help="UTC date YYYY-MM-DD to scan and record")
    ap.add_argument("--since", type=int, metavar="N", help="scan the last N UTC days")
    ap.add_argument("--live", action="store_true", help="check current feed silence + write alert")
    ap.add_argument("--min-gap-secs", type=float, default=MIN_GAP_SECS_DEFAULT)
    ap.add_argument("--alert-secs", type=float, default=ALERT_SECS_DEFAULT)
    ap.add_argument("--raw-root", default=RAW_ROOT_DEFAULT)
    ap.add_argument("--record", default=RECORD_DEFAULT)
    ap.add_argument("--alert", default=ALERT_DEFAULT)
    args = ap.parse_args(argv)

    if args.live:
        a = live_check(args.raw_root, args.alert, args.alert_secs)
        print(json.dumps(a))
        return 2 if a["status"] == "gap" else 0

    dates = []
    if args.date:
        dates = [args.date]
    elif args.since:
        dates = _dates_since(args.since)
    else:
        ap.error("one of --date, --since, or --live is required")

    min_gap_us = int(args.min_gap_secs * 1_000_000)
    total = 0
    for d in dates:
        gaps, stats = scan_date(d, args.raw_root, min_gap_us)
        write_record(args.record, d, gaps)
        total += len(gaps)
        for s, e in gaps:
            dur = (e - s) / 1_000_000
            print(f"{d} GAP {dt.datetime.utcfromtimestamp(s/1e6).isoformat()}Z "
                  f"-> {dt.datetime.utcfromtimestamp(e/1e6).isoformat()}Z ({dur:.0f}s)")
        print(f"{d}: {len(gaps)} gap(s) >{args.min_gap_secs:g}s "
              f"[{stats['records']} records, {stats['files']} files, {stats['unparsed']} unparsed]")
    print(f"total {total} gap(s) written to {args.record}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
