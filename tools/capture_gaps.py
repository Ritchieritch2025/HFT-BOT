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


# Plausibility window for recv_wall_ns (D3: validate external input at the
# boundary). Microseconds since epoch for 2020-01-01 .. 2100-01-01. A corrupt
# line whose digit-run parses to an absurd value is DROPPED (counted unparsed),
# never staged and never crashing the scan (a garbage huge int also overflows
# the int64 timestamp array).
_MIN_PLAUSIBLE_US = 1_577_836_800_000_000   # 2020-01-01Z
_MAX_PLAUSIBLE_US = 4_102_444_800_000_000   # 2100-01-01Z


def _extract_recv_us(line):
    """recv_wall_ns (2nd field) -> microseconds, via a targeted slice (no full
    JSON parse: raw files are hundreds of MB). Returns None on an unparsable,
    truncated, or IMPLAUSIBLE line so a corrupt record is skipped and counted,
    never crashes the scan (D3)."""
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
        us = int(line[j:k]) // 1000
    except ValueError:
        return None
    return us if _MIN_PLAUSIBLE_US <= us <= _MAX_PLAUSIBLE_US else None


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


def _last_us(path, window=65536):
    """Last parsable recv_us in a file (tail freshness). Tail-seek: read only the
    final `window` bytes so the 60s live poll is O(1), not O(file) — the current
    firehose segment is up to ~512 MiB and a full scan every minute is ~GBs of
    wasted reads. A truncated last line yields None (skipped); the previous good
    record wins. Records are small (hundreds of bytes) so a 64 KiB tail holds
    many lines. Small files are read whole."""
    last = None
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        if size > window:
            fh.seek(size - window)
            fh.readline()  # discard the partial line the seek landed inside
        else:
            fh.seek(0)
        for raw in fh:
            u = _extract_recv_us(raw.decode("utf-8", "replace"))
            if u is not None:
                last = u
    return last


def _raw_files_for_date(date_str, raw_root):
    """All firehose segments for a UTC date (base + rotations). Order does not
    matter — scan_date globally sorts records — so no per-file peeking."""
    d = os.path.join(raw_root, f"date={date_str}")
    return sorted(glob.glob(os.path.join(d, "firehose_*.ndjson")) +
                  glob.glob(os.path.join(d, "firehose_*.ndjson.*")))


def scan_date(date_str, raw_root, min_gap_us, now_us=None):
    """Detect capture gaps for a UTC date. Returns (gaps, stats).

    Global sort (not sequential-per-file streaming) is required: a within-hour
    respawn/rotation can leave two segments whose time ranges OVERLAP, and
    streaming them back-to-back would invent a false backward 'gap'. int64-us
    array keeps a full day memory-cheap.

    Gaps detected (each a real hole in coverage; D2 — never miss one):
      * INTERIOR: silence between two consecutive records.
      * LEADING: day_start -> first record (feed down at the start of the day).
      * TRAILING: last record -> min(day_end, now) (feed died and stayed down;
        capped at `now` so a still-in-progress day isn't flagged as trailing).
    These edges close the day-boundary blind spot: a hole crossing midnight is
    recorded as a trailing gap of day D plus a leading gap of day D+1.

    Fail-closed on missing data (never certify a day clean without evidence):
      * files present but ZERO parseable records (e.g. a recv_wall_ns format
        change) -> the whole elapsed span is a gap; stats['unreadable']=True.
      * NO raw files at all (pruned past retention, or never captured) ->
        stats['has_files']=False; NO gap fabricated and the caller MUST NOT
        overwrite this day's existing record (raw outlives-nothing; the record
        must outlive the 3-day raw)."""
    now_us = now_us if now_us is not None else int(time.time() * 1_000_000)
    day_start, day_end = _day_bounds_us(date_str)
    eff_end = min(day_end, now_us)
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
    stats = {"files": len(files), "records": len(ts), "unparsed": bad,
             "has_files": len(files) > 0, "unreadable": False}

    if not ts:
        if files and eff_end - day_start > min_gap_us:
            stats["unreadable"] = True          # raw present but unparseable (B2)
            return [(day_start, eff_end)], stats
        return [], stats                         # no raw -> preserve record (B3)

    gaps = list(find_gaps(ts, min_gap_us))       # interior
    if ts[0] - day_start > min_gap_us:           # leading-edge silence
        gaps.append((day_start, ts[0]))
    if eff_end - ts[-1] > min_gap_us:            # trailing-edge silence
        gaps.append((ts[-1], eff_end))
    gaps.sort()
    return gaps, stats


def _day_bounds_us(date_str):
    base = dt.datetime.strptime(date_str, "%Y-%m-%d")
    start = calendar.timegm(base.timetuple()) * 1_000_000
    return start, start + 86_400 * 1_000_000


def write_record(record_path, date_str, gaps, replace_day=True):
    """Merge `gaps` for `date_str` into the durable record. With replace_day
    (the day was actually SCANNED with data) the day's existing rows are
    replaced by the fresh scan. With replace_day=False (the raw was pruned/absent
    — scan produced no evidence) the day's existing rows are PRESERVED: a
    re-run over aged-out raw must never wipe a previously-recorded real gap (B3).
    Other days are always kept. Idempotent; deduped; atomic replace."""
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
                in_day = day_start <= s < day_end
                if in_day and replace_day:
                    continue  # superseded by the fresh scan of this day
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


def write_scan_receipt(receipt_dir, date_str, raw_root, gaps, stats):
    """PIPE-W05 P0-3: AFFIRMATIVE per-date scan receipt. Binds the date, the
    EXACT raw inventory scanned (relative file names + byte sizes) and the
    result. A done-marker or a bare CSV is never proof of scan coverage —
    downstream consumers must match this inventory against the day seal.
    Written only when raw files were actually scanned (absence of raw proves
    nothing and earns no receipt)."""
    files = [{"file": os.path.join("date=%s" % date_str,
                                   os.path.basename(p)).replace(os.sep, "/"),
              "bytes": os.stat(p).st_size}
             for p in _raw_files_for_date(date_str, raw_root)]
    payload = {
        "schema_version": "capture-gap-scan-receipt-v1",
        "date": date_str,
        "raw_root": raw_root,
        "files": files,
        "n_files": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "records": stats["records"],
        "unparsed": stats["unparsed"],
        "unreadable": stats["unreadable"],
        "gaps": [{"start_us": s, "end_us": e} for s, e in gaps],
        "generated_at_utc":
            dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(receipt_dir,
                        "capture_gap_receipt_%s.json" % date_str)
    os.makedirs(receipt_dir or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def main(argv):
    ap = argparse.ArgumentParser(description="Capture-gap record builder + live detector (W-C2)")
    ap.add_argument("--date", help="UTC date YYYY-MM-DD to scan and record")
    ap.add_argument("--since", type=int, metavar="N", help="scan the last N UTC days")
    ap.add_argument("--live", action="store_true", help="check current feed silence + write alert")
    ap.add_argument("--min-gap-secs", type=float, default=MIN_GAP_SECS_DEFAULT)
    ap.add_argument("--alert-secs", type=float, default=ALERT_SECS_DEFAULT)
    ap.add_argument("--raw-root", default=RAW_ROOT_DEFAULT)
    ap.add_argument("--record", default=RECORD_DEFAULT)
    ap.add_argument("--receipt-dir", default=None,
                    help="where per-date scan receipts land (default: the "
                         "record file's directory)")
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
    rc = 0
    for d in dates:
        gaps, stats = scan_date(d, args.raw_root, min_gap_us)
        if not stats["has_files"]:
            # Raw pruned/absent: preserve any existing record for this day, never
            # wipe it (B3). The record must outlive the 3-day raw retention.
            print(f"{d}: no raw files (pruned or absent) — existing record preserved (not rescanned)")
            continue
        write_record(args.record, d, gaps, replace_day=True)
        write_scan_receipt(args.receipt_dir
                           or (os.path.dirname(args.record) or "."),
                           d, args.raw_root, gaps, stats)
        total += len(gaps)
        for s, e in gaps:
            dur = (e - s) / 1_000_000
            print(f"{d} GAP {dt.datetime.utcfromtimestamp(s/1e6).isoformat()}Z "
                  f"-> {dt.datetime.utcfromtimestamp(e/1e6).isoformat()}Z ({dur:.0f}s)")
        if stats["unreadable"]:
            print(f"{d}: RAW PRESENT BUT UNREADABLE — {stats['unparsed']} unparsed lines, 0 "
                  f"records; recorded a full-day gap (fail-closed). Fix the parser + rescan.")
            rc = 3
        print(f"{d}: {len(gaps)} gap(s) >{args.min_gap_secs:g}s "
              f"[{stats['records']} records, {stats['files']} files, {stats['unparsed']} unparsed]")
    print(f"total {total} gap(s) written to {args.record}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
