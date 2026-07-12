#!/usr/bin/env python3
"""l2_gap_check — offline per-day L2 capture-quality record (PIPE-W06 §5).

Post-seal evidence for the targeted-L2 raw family (`l2_<HH>.ndjson` + rotation
shards). Counts, per UTC day, straight off the raw envelopes:

  * per-sid WS `seq` continuity (envelope `sid` + `source_sequence`, written
    by WsRecorder for every frame; docs/kalshi_ws_protocol.md I1: seq is
    per-sid monotonic, control `ok`/`unsubscribed` advance it too). Keyed by
    (hour file base, stream_epoch, sid) — each hourly l2_shadow segment is
    its own connection, so cross-hour "resets" are structure, not loss.
    seq==1 after an established stream is counted as a stream_restart
    (mid-hour ws_shadow relaunch appending to the same file — benign,
    counted separately, never folded into gaps).
  * WsRecorder marker records: gap / loss / epoch_change / resync_* — every
    kind seen is counted (a `loss` marker's source_sequence carries the
    number of frames dropped; summed into markers_lost_frames).
  * per-market `orderbook_snapshot` re-anchors: a snapshot for a market that
    already had one on the same (file base, epoch, sid) stream = the client
    re-anchored the book mid-stream (I3/I4 in-stream recovery).

Output: `work/event_packs/l2_gaps_<D>.json` (atomic tmp+replace) + one
quality_log line (same shape as daily_check's append_quality_log). Gaps are
COUNTED AND SURFACED, never hidden (D2) — a day with gaps still exits 0;
this is an evidence record, not a gate (same posture as capture_gaps).
A day with NO l2 files writes an honest `no_l2_files` record (the layer is
optional/disableable; absence is legitimate and must be visible, not
fabricated into a failure).

Field extraction is targeted slicing over the envelope prefix only (the line
is cut at `,"raw"` first, so payload bytes can never fake an envelope field);
full-JSON parsing of multi-GB raw days is deliberately avoided (same lesson
as capture_gaps). Read-only over work/raw (D1). stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

RAW_ROOT_DEFAULT = os.path.join("work", "raw")
OUT_DIR_DEFAULT = os.path.join("work", "event_packs")
QUALITY_LOG_DEFAULT = os.path.join("work", "quality_log.ndjson")

_SHARD_RE = re.compile(r"^(?P<base>l2_\d{2})\.ndjson(?:\.(?P<shard>\d+))?$")


def _l2_files_for_date(date_str, raw_root):
    """l2 family segments for a UTC date: base files + rotation shards, in
    (hour, shard-number) order so per-stream seq tracking sees each hourly
    stream contiguously (l2_13.ndjson.10 must sort after .2 — numeric)."""
    d = os.path.join(raw_root, "date=%s" % date_str)
    found = []
    for p in glob.glob(os.path.join(d, "l2_*.ndjson*")):
        m = _SHARD_RE.match(os.path.basename(p))
        if not m:
            continue
        found.append((m.group("base"), int(m.group("shard") or 0), p))
    return [(base, path) for base, _n, path in sorted(found)]


def _envelope(line):
    """The searchable envelope prefix. Everything after `,"raw"` is verbatim
    exchange payload whose quotes are escaped; cutting it off makes the
    targeted field slicing below immune to payload contents."""
    cut = line.find(',"raw"')
    return line[:cut] if cut >= 0 else line


def _field_int(env, key):
    tag = '"%s":' % key
    i = env.find(tag)
    if i < 0:
        return None
    j = i + len(tag)
    k = j
    if k < len(env) and env[k] == "-":
        k += 1
    while k < len(env) and env[k].isdigit():
        k += 1
    if k == j or (k == j + 1 and env[j] == "-"):
        return None
    try:
        return int(env[j:k])
    except ValueError:
        return None


def _field_str(env, key):
    tag = '"%s":"' % key
    i = env.find(tag)
    if i < 0:
        return None
    j = i + len(tag)
    k = env.find('"', j)
    if k < 0:
        return None
    return env[j:k]


def scan_date(date_str, raw_root):
    """Pure scan -> record dict (unit-tested; no writes)."""
    files = _l2_files_for_date(date_str, raw_root)
    record = {
        "date": date_str,
        "raw_root": raw_root,
        "files": [os.path.basename(p) for _b, p in files],
        "no_l2_files": not files,
        "lines": 0,
        "parse_errors": 0,
        "sids_total": 0,
        "sids_with_seq_gaps": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "seq_regressions": 0,
        "stream_restarts": 0,
        "recorder_markers": {},
        "markers_lost_frames": 0,
        "snapshot_re_anchors_total": 0,
        "per_market": {},
    }
    last_seq = {}         # (base, epoch, sid) -> last seq
    gap_sids = set()
    anchored = set()      # (base, epoch, sid, ticker) with a snapshot already
    per_market = record["per_market"]

    for base, path in files:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record["lines"] += 1
                env = _envelope(line)
                marker = _field_str(env, "marker")
                if marker is not None:
                    mk = record["recorder_markers"]
                    mk[marker] = mk.get(marker, 0) + 1
                    if marker == "loss":
                        lost = _field_int(env, "source_sequence")
                        record["markers_lost_frames"] += lost if lost else 0
                    continue
                channel = _field_str(env, "channel")
                ticker = _field_str(env, "source_ticker")
                sid = _field_int(env, "sid")
                seq = _field_int(env, "source_sequence")
                epoch = _field_int(env, "stream_epoch") or 0
                if channel is None and sid is None and seq is None:
                    record["parse_errors"] += 1
                    continue
                if ticker:
                    m = per_market.setdefault(
                        ticker, {"msgs": 0, "snapshots": 0, "re_anchors": 0})
                    m["msgs"] += 1
                    if channel == "orderbook_snapshot":
                        m["snapshots"] += 1
                        akey = (base, epoch, sid, ticker)
                        if akey in anchored:
                            m["re_anchors"] += 1
                            record["snapshot_re_anchors_total"] += 1
                        else:
                            anchored.add(akey)
                if sid is None or seq is None:
                    continue  # unsequenced channel (I9) — nothing to check
                key = (base, epoch, sid)
                prev = last_seq.get(key)
                if prev is None:
                    last_seq[key] = seq
                elif seq == prev + 1:
                    last_seq[key] = seq
                elif seq > prev + 1:
                    record["seq_gap_events"] += 1
                    record["seq_missed_total"] += seq - prev - 1
                    gap_sids.add(key)
                    last_seq[key] = seq
                elif seq == 1:
                    # fresh subscription on the same hourly file: a mid-hour
                    # ws_shadow relaunch appended a new stream (benign; counted,
                    # not hidden, and never inflated into a seq gap)
                    record["stream_restarts"] += 1
                    last_seq[key] = seq
                else:
                    record["seq_regressions"] += 1
                    last_seq[key] = seq
    record["sids_total"] = len(last_seq)
    record["sids_with_seq_gaps"] = len(gap_sids)
    return record


def write_record(record, out_path):
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def append_quality_log(path, record, evidence_path, now=None):
    """Same entry shape as tools/daily_check.py append_quality_log."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if record["no_l2_files"]:
        finding = "no l2 raw files for %s (layer off/absent — recorded honestly)" \
            % record["date"]
    else:
        finding = ("l2 %s: files=%d lines=%d seq_gaps=%d (missed=%d) "
                   "regressions=%d restarts=%d markers=%s lost_frames=%d "
                   "re_anchors=%d parse_errors=%d"
                   % (record["date"], len(record["files"]), record["lines"],
                      record["seq_gap_events"], record["seq_missed_total"],
                      record["seq_regressions"], record["stream_restarts"],
                      json.dumps(record["recorder_markers"], sort_keys=True),
                      record["markers_lost_frames"],
                      record["snapshot_re_anchors_total"],
                      record["parse_errors"]))
    entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "wp": "l2-gap-check",
             "window": record["date"], "finding": finding,
             "action": "recorded", "evidence": evidence_path}
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="UTC date YYYY-MM-DD to scan")
    ap.add_argument("--raw-root", default=RAW_ROOT_DEFAULT)
    ap.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    ap.add_argument("--quality-log", default=QUALITY_LOG_DEFAULT)
    args = ap.parse_args(argv)
    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        ap.error("--date must be YYYY-MM-DD")

    record = scan_date(args.date, args.raw_root)
    record["generated_at_utc"] = dt.datetime.now(dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path = os.path.join(args.out_dir, "l2_gaps_%s.json" % args.date)
    write_record(record, out_path)
    entry = append_quality_log(args.quality_log, record, out_path)
    print(entry["finding"])
    print("record -> %s" % out_path)
    # Evidence record, not a gate: gaps are surfaced above, exit stays 0 (D2:
    # counted-not-hidden; the research side decides which windows are usable).
    return 0


if __name__ == "__main__":
    sys.exit(main())
