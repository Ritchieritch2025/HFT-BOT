#!/usr/bin/env python3
"""Build an offline, per-day L2 capture-quality receipt.

The scanner treats the recorder envelope and its escaped Kalshi payload as two
independent copies of sequence identity.  A row may affect continuity only when
both copies are valid and agree.  This matters because two writers interleaving
bytes can leave a syntactically valid integer (and even valid JSON) that is not
a real sequence number.

``scan_date`` is read-only.  The CLI writes the normal event-pack receipt and
quality-log entry; callers doing a strict read-only audit should call the pure
function directly.
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
_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1
_CRITICAL_ENVELOPE_KEYS = (
    b'"channel":',
    b'"source_ticker":',
    b'"source_sequence":',
    b'"sid":',
    b'"stream_epoch":',
    b'"marker":',
)


def _l2_files_for_date(date_str, raw_root):
    """Return L2 base files and rotation shards in numeric shard order."""
    day_dir = os.path.join(raw_root, "date=%s" % date_str)
    found = []
    for path in glob.glob(os.path.join(day_dir, "l2_*.ndjson*")):
        match = _SHARD_RE.match(os.path.basename(path))
        if not match:
            continue
        found.append((match.group("base"), int(match.group("shard") or 0), path))
    return [(base, path) for base, _shard, path in sorted(found)]


def _is_uint(value, maximum):
    return isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= maximum


def _corruption(reason, sequence_related=False):
    return {"kind": "corrupt", "reason": reason,
            "sequence_related": sequence_related}


def _decode_row(line):
    """Decode and cross-check one raw-log row.

    Full JSON validation is intentional.  Merely bounding the envelope integer
    is insufficient: a decimal splice can remain below uint64.  Critical key
    uniqueness is checked separately because json.loads otherwise accepts a
    duplicate key and silently keeps its final value.
    """
    try:
        outer = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _corruption("outer_json")
    if not isinstance(outer, dict):
        return _corruption("outer_not_object")

    marker = outer.get("marker")
    if marker is not None:
        if not isinstance(marker, str) or not marker:
            return _corruption("invalid_marker")
        lost = outer.get("source_sequence")
        if lost is not None and not _is_uint(lost, _UINT64_MAX):
            return _corruption("invalid_marker_sequence", True)
        return {"kind": "marker", "marker": marker, "lost": lost}

    # RawLogWriter emits every envelope field before the single `,"raw"`
    # member.  Payload quotes are escaped, so they cannot satisfy these byte
    # patterns.  Reject duplicates before json.loads' last-key-wins semantics
    # can hide them.
    separator = b',"raw"'
    if line.count(separator) != 1:
        return _corruption("raw_member_count")
    envelope = line.split(separator, 1)[0]
    if any(envelope.count(key) > 1 for key in _CRITICAL_ENVELOPE_KEYS):
        return _corruption("duplicate_envelope_key", True)

    channel = outer.get("channel")
    ticker = outer.get("source_ticker")
    if not isinstance(channel, str) or not isinstance(ticker, str):
        return _corruption("invalid_envelope")
    for clock_key in ("recv_mono_ns", "recv_wall_ns"):
        if not _is_uint(outer.get(clock_key), _UINT64_MAX):
            return _corruption("invalid_%s" % clock_key)

    raw = outer.get("raw")
    if not isinstance(raw, str):
        return _corruption("raw_not_string")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _corruption("raw_json")
    if not isinstance(payload, dict):
        return _corruption("raw_not_object")

    raw_type = payload.get("type")
    if raw_type != channel:
        return _corruption("channel_mismatch")

    sid = outer.get("sid")
    seq = outer.get("source_sequence")
    epoch = outer.get("stream_epoch", 0)
    raw_sid = payload.get("sid")
    raw_seq = payload.get("seq")
    values = ((sid, _UINT32_MAX), (raw_sid, _UINT32_MAX),
              (seq, _UINT64_MAX), (raw_seq, _UINT64_MAX))
    if any(value is not None and not _is_uint(value, maximum)
           for value, maximum in values):
        return _corruption("invalid_sequence_value", True)
    if not _is_uint(epoch, _UINT64_MAX):
        return _corruption("invalid_stream_epoch", True)
    if (sid is None) != (seq is None) or (raw_sid is None) != (raw_seq is None):
        return _corruption("incomplete_sequence_identity", True)
    if sid != raw_sid or seq != raw_seq:
        return _corruption("sequence_identity_mismatch", True)

    raw_message = payload.get("msg")
    if isinstance(raw_message, dict) and "market_ticker" in raw_message:
        raw_ticker = raw_message["market_ticker"]
        if not isinstance(raw_ticker, str) or raw_ticker != ticker:
            return _corruption("ticker_mismatch")

    return {"kind": "message", "channel": channel, "ticker": ticker,
            "sid": sid, "seq": seq, "epoch": epoch}


def _empty_record(date_str, raw_root, files):
    return {
        "schema_version": "l2-gap-receipt-v2",
        "date": date_str,
        "raw_root": raw_root,
        "files": [os.path.basename(path) for _base, path in files],
        "file_inventory": [
            {"file": "date=%s/%s" % (date_str, os.path.basename(path)),
             "bytes": os.stat(path).st_size}
            for _base, path in files
        ],
        "no_l2_files": not files,
        "lines": 0,
        "parse_errors": 0,
        "corrupt_rows": 0,
        "corrupt_sequence_rows": 0,
        "corrupt_row_reasons": {},
        "continuity_resets_due_to_corruption": 0,
        "seq_continuity_complete": True,
        "seq_counts_are_lower_bound": False,
        "trusted_file_bases": [],
        "untrusted_file_bases": [],
        "sids_total": 0,
        "sids_with_seq_gaps": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "seq_regressions": 0,
        "stream_restarts": 0,
        "seq_gap_events_discarded_untrusted": 0,
        "seq_missed_total_discarded_untrusted": 0,
        "seq_regressions_discarded_untrusted": 0,
        "stream_restarts_discarded_untrusted": 0,
        "recorder_markers": {},
        "markers_lost_frames": 0,
        "snapshot_re_anchors_total": 0,
        "per_market": {},
    }


def _reset_base_state(base, last_seq, anchored):
    """Break continuity at an unknowable row instead of inventing a gap."""
    seq_keys = [key for key in last_seq if key[0] == base]
    anchor_keys = [key for key in anchored if key[0] == base]
    for key in seq_keys:
        del last_seq[key]
    for key in anchor_keys:
        anchored.remove(key)
    return bool(seq_keys or anchor_keys)


def scan_date(date_str, raw_root):
    """Pure read-only scan returning a receipt dictionary."""
    files = _l2_files_for_date(date_str, raw_root)
    record = _empty_record(date_str, raw_root, files)
    last_seq = {}
    seen_streams = set()
    anchored = set()
    per_market = record["per_market"]
    base_stats = {}
    untrusted_bases = set()

    for base, path in files:
        with open(path, "rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record["lines"] += 1
                row = _decode_row(line)
                if row["kind"] == "corrupt":
                    untrusted_bases.add(base)
                    record["parse_errors"] += 1
                    record["corrupt_rows"] += 1
                    if row["sequence_related"]:
                        record["corrupt_sequence_rows"] += 1
                    reasons = record["corrupt_row_reasons"]
                    reason = row["reason"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    if _reset_base_state(base, last_seq, anchored):
                        record["continuity_resets_due_to_corruption"] += 1
                    continue

                if row["kind"] == "marker":
                    markers = record["recorder_markers"]
                    marker = row["marker"]
                    markers[marker] = markers.get(marker, 0) + 1
                    if marker == "loss" and row["lost"] is not None:
                        record["markers_lost_frames"] += row["lost"]
                    continue

                ticker = row["ticker"]
                if ticker:
                    market = per_market.setdefault(
                        ticker, {"msgs": 0, "snapshots": 0, "re_anchors": 0})
                    market["msgs"] += 1
                    if row["channel"] == "orderbook_snapshot":
                        market["snapshots"] += 1
                        anchor_key = (base, row["epoch"], row["sid"], ticker)
                        if anchor_key in anchored:
                            market["re_anchors"] += 1
                            record["snapshot_re_anchors_total"] += 1
                        else:
                            anchored.add(anchor_key)

                sid = row["sid"]
                seq = row["seq"]
                if sid is None:
                    continue
                key = (base, row["epoch"], sid)
                seen_streams.add(key)
                stats = base_stats.setdefault(base, {
                    "gap_events": 0, "missed": 0, "regressions": 0,
                    "restarts": 0, "gap_sids": set(),
                })
                previous = last_seq.get(key)
                if previous is None:
                    last_seq[key] = seq
                elif seq == previous + 1:
                    last_seq[key] = seq
                elif seq > previous + 1:
                    stats["gap_events"] += 1
                    stats["missed"] += seq - previous - 1
                    stats["gap_sids"].add(key)
                    last_seq[key] = seq
                elif seq == 1:
                    stats["restarts"] += 1
                    last_seq[key] = seq
                else:
                    stats["regressions"] += 1
                    last_seq[key] = seq

    record["sids_total"] = len(seen_streams)
    trusted_bases = set(base_stats) - untrusted_bases
    trusted_gap_sids = set()
    for base, stats in base_stats.items():
        if base in untrusted_bases:
            record["seq_gap_events_discarded_untrusted"] += stats["gap_events"]
            record["seq_missed_total_discarded_untrusted"] += stats["missed"]
            record["seq_regressions_discarded_untrusted"] += stats["regressions"]
            record["stream_restarts_discarded_untrusted"] += stats["restarts"]
            continue
        record["seq_gap_events"] += stats["gap_events"]
        record["seq_missed_total"] += stats["missed"]
        record["seq_regressions"] += stats["regressions"]
        record["stream_restarts"] += stats["restarts"]
        trusted_gap_sids.update(stats["gap_sids"])
    record["trusted_file_bases"] = sorted(trusted_bases)
    record["untrusted_file_bases"] = sorted(untrusted_bases)
    record["sids_with_seq_gaps"] = len(trusted_gap_sids)
    if record["corrupt_rows"]:
        record["seq_continuity_complete"] = False
        record["seq_counts_are_lower_bound"] = True
    return record


def write_record(record, out_path):
    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = out_path + ".tmp.%d" % os.getpid()
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, out_path)


def append_quality_log(path, record, evidence_path, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if record["no_l2_files"]:
        finding = "no l2 raw files for %s (layer off/absent — recorded honestly)" \
            % record["date"]
    else:
        finding = ("l2 %s: files=%d lines=%d seq_gaps=%d (missed=%d; "
                   "lower_bound=%s) regressions=%d restarts=%d markers=%s "
                   "lost_frames=%d re_anchors=%d corrupt_rows=%d "
                   "corrupt_sequence_rows=%d"
                   % (record["date"], len(record["files"]), record["lines"],
                      record["seq_gap_events"], record["seq_missed_total"],
                      str(record["seq_counts_are_lower_bound"]).lower(),
                      record["seq_regressions"], record["stream_restarts"],
                      json.dumps(record["recorder_markers"], sort_keys=True),
                      record["markers_lost_frames"],
                      record["snapshot_re_anchors_total"],
                      record["corrupt_rows"],
                      record["corrupt_sequence_rows"]))
    entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "wp": "l2-gap-check", "window": record["date"],
             "finding": finding, "action": "recorded",
             "evidence": evidence_path}
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    parser.add_argument("--raw-root", default=RAW_ROOT_DEFAULT)
    parser.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    parser.add_argument("--quality-log", default=QUALITY_LOG_DEFAULT)
    args = parser.parse_args(argv)
    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")

    record = scan_date(args.date, args.raw_root)
    record["generated_at_utc"] = dt.datetime.now(dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path = os.path.join(args.out_dir, "l2_gaps_%s.json" % args.date)
    write_record(record, out_path)
    entry = append_quality_log(args.quality_log, record, out_path)
    print(entry["finding"])
    print("record -> %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
