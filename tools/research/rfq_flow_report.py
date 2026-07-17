#!/usr/bin/env python3
"""Build the first 48-hour passive RFQ flow atlas from sealed raw captures.

Input is the byte-exact WsRecorder family ``rfq_<HH>.ndjson*``.  The outer
record supplies TL1 receive clocks; its ``raw`` string is the verbatim Kalshi
communications frame.  Only broadcast ``rfq_created``/``rfq_deleted`` events
enter the atlas.  Any account-private quote events that happen to arrive are
counted as excluded frame types and are never interpreted as market-wide flow.

This is EXPLORATORY evidence, not a strategy/trading gate.  It deliberately
keeps ``contracts_fp`` (E2) and ``target_cost_dollars`` (E6) as separate exact
integer populations.  ``creator_id`` is joined from delete -> create by RFQ id because
the official AsyncAPI says it is currently empty in rfq_created messages.

Official schema pin:
  docs/vendor/kalshi/latest/asyncapi.yaml
  sha256 00858d5a892eb7066a8da247660622721e8ee80bfdd89b1b9a24278c15d7d421
Official mechanism: https://docs.kalshi.com/getting_started/rfqs
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import glob
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
SPEC_SHA256 = "00858d5a892eb7066a8da247660622721e8ee80bfdd89b1b9a24278c15d7d421"
RFQ_TYPES = {"rfq_created", "rfq_deleted"}
REQUIRED = {
    "rfq_created": ("id", "creator_id", "market_ticker", "created_ts"),
    "rfq_deleted": ("id", "creator_id", "market_ticker", "deleted_ts"),
}
HOUR_RE = re.compile(
    r"[/\\]date=(\d{4}-\d{2}-\d{2})[/\\]rfq_(\d{2})\.ndjson"
    r"(?:\.([1-9][0-9]*))?$")
HASH_CHUNK_BYTES = 1024 * 1024


class ReportError(RuntimeError):
    pass


def parse_iso(value: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an RFC3339 UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def fixed_exact(value, field: str, places: int) -> int | None:
    """Parse a fixed-point string into an integer scale with no float path."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be an official fixed-point string" % field)
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("%s must be a non-negative fixed-point decimal string" % field)
    try:
        scaled = Decimal(str(value)) * (Decimal(10) ** places)
    except InvalidOperation as exc:
        raise ValueError("%s is not decimal" % field) from exc
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError("%s has precision finer than E%d" % (field, places))
    return int(integral)


def fixed_text(value: int | None, places: int) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    scale = 10 ** places
    return "%s%d.%0*d" % (sign, value // scale, places, value % scale)


def nearest_rank(values: list[int], pct: int) -> int | None:
    if not values:
        return None
    xs = sorted(values)
    rank = max(1, (pct * len(xs) + 99) // 100)
    return xs[min(len(xs), rank) - 1]


def bps(num: int, den: int) -> int | None:
    if den <= 0:
        return None
    return (num * 10_000 + den // 2) // den


def hour_key_from_path(path: str) -> str | None:
    m = HOUR_RE.search(path)
    return (m.group(1) + "T" + m.group(2)) if m else None


def shard_ordinal_from_path(path: str) -> int | None:
    m = HOUR_RE.search(path)
    if not m:
        return None
    return int(m.group(3) or 0)


def _path_sort_key(path: str) -> tuple:
    hour = hour_key_from_path(path)
    ordinal = shard_ordinal_from_path(path)
    if hour is not None and ordinal is not None:
        return (0, hour, ordinal, path)
    return (1, path, 0, path)


def _stream_file_identity(path: Path, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> dict:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError("input must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("input identity changed during open")
        while True:
            chunk = os.read(fd, chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) or
            total != before.st_size):
        raise OSError("input changed while hashing")
    return {"bytes": total, "sha256": digest.hexdigest(),
            "device": before.st_dev, "inode": before.st_ino}


def _canonical_capture_shards(shards: list[dict]) -> list[dict]:
    """Validate and canonicalize a complete portable RFQ shard identity."""
    if not isinstance(shards, list) or not shards:
        raise ValueError("capture shard set must be a non-empty list")
    canonical: list[dict] = []
    seen_ordinals: set[int] = set()
    seen_relpaths: set[str] = set()
    pattern = re.compile(
        r"^date=(\d{4}-\d{2}-\d{2})/rfq_(?:[01]\d|2[0-3])\.ndjson"
        r"(?:\.([1-9][0-9]*))?$")
    for row in shards:
        if not isinstance(row, dict):
            raise ValueError("capture shard member must be an object")
        ordinal = row.get("ordinal")
        relpath = row.get("relpath")
        bytes_before = row.get("bytes_before")
        size = row.get("size")
        parsed = row.get("parsed_bytes_at_close")
        sha256 = row.get("sha256")
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("capture shard ordinal is invalid")
        if ordinal in seen_ordinals:
            raise ValueError("duplicate capture shard ordinal")
        if not isinstance(relpath, str) or "\\" in relpath:
            raise ValueError("capture shard relpath is unsafe")
        matched = pattern.fullmatch(relpath)
        if not matched:
            raise ValueError("capture shard relpath is unsafe")
        try:
            dt.date.fromisoformat(matched.group(1))
        except ValueError as exc:
            raise ValueError("capture shard relpath date is invalid") from exc
        rel_ordinal = 0 if matched.group(2) is None else int(matched.group(2))
        if rel_ordinal != ordinal or relpath in seen_relpaths:
            raise ValueError("capture shard relpath/ordinal is duplicated or mismatched")
        if (type(bytes_before) is not int or bytes_before < 0 or
                type(size) is not int or size < 0 or bytes_before > size or
                type(parsed) is not int or parsed != size):
            raise ValueError("capture shard byte bounds are invalid")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("capture shard SHA-256 is invalid")
        seen_ordinals.add(ordinal)
        seen_relpaths.add(relpath)
        canonical.append({
            "ordinal": ordinal, "relpath": relpath,
            "bytes_before": bytes_before, "size": size,
            "parsed_bytes_at_close": parsed, "sha256": sha256,
        })
    canonical.sort(key=lambda row: (row["ordinal"], row["relpath"]))
    if [row["ordinal"] for row in canonical] != list(range(len(canonical))):
        raise ValueError("capture shard ordinals are not contiguous from zero")
    return canonical


def capture_shard_set_sha256(shards: list[dict]) -> str:
    canonical = _canonical_capture_shards(shards)
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def discover(raw_root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(raw_root, "date=*", "rfq_*.ndjson*")),
                  key=_path_sort_key)


def parse_paths(paths: Iterable[str]) -> dict:
    """Parse recorder files; retain validated events and explicit DQ counts."""
    events: list[dict] = []
    dq_records: list[dict] = []
    excluded_records: list[dict] = []
    marker_records: list[dict] = []
    segment_receipts: list[dict] = []
    observed_hours: set[str] = set()
    subscribed_hours: set[str] = set()
    file_hours: set[str] = set()
    input_files: list[dict] = []
    capture_shard_sets: dict[str, list[dict]] = {}
    recorder_rows = 0
    min_recv_ns = None
    max_recv_ns = None

    for path in sorted(paths, key=_path_sort_key):
        path_hour = hour_key_from_path(path)
        ordinal = shard_ordinal_from_path(path)
        try:
            identity = _stream_file_identity(Path(path))
            item = {
                "path": path,
                "hour": path_hour,
                "bytes": identity["bytes"],
                "sha256": identity["sha256"],
                "device": identity["device"],
                "inode": identity["inode"],
            }
            if path_hour is not None and ordinal is not None:
                item["ordinal"] = ordinal
                file_hours.add(path_hour)
                capture_shard_sets.setdefault(path_hour, []).append(item)
            input_files.append(item)
        except OSError as exc:
            dq_records.append({"hour": path_hour, "key": "unsafe_or_unreadable_file",
                               "detail": str(exc)})
            continue
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fh = os.fdopen(os.open(path, flags), "r", encoding="utf-8")
        except OSError as exc:
            dq_records.append({"hour": path_hour, "key": "unreadable_file"})
            continue
        with fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    outer = json.loads(line)
                    recv_ns = outer.get("recv_wall_ns")
                    if type(recv_ns) is not int or recv_ns <= 0:
                        raise ValueError("invalid recv_wall_ns")
                    row_hour = dt.datetime.fromtimestamp(
                        recv_ns / 1e9, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H")
                    if path_hour and row_hour != path_hour:
                        dq_records.append({"hour": path_hour,
                                           "key": "partition_hour_mismatch"})
                    if outer.get("marker"):
                        marker = str(outer["marker"])
                        marker_records.append({"hour": row_hour, "key": marker})
                        if marker == "segment_receipt":
                            try:
                                receipt = json.loads(outer.get("raw", ""))
                                if (not isinstance(receipt, dict) or
                                        receipt.get("type") != "rfq_segment_receipt"):
                                    raise ValueError("bad receipt")
                                receipt = dict(receipt)
                                receipt["_source_file"] = path
                                segment_receipts.append(receipt)
                            except (ValueError, TypeError):
                                dq_records.append({"hour": row_hour,
                                                   "key": "malformed_segment_receipt"})
                        recorder_rows += 1
                        min_recv_ns = recv_ns if min_recv_ns is None else min(min_recv_ns, recv_ns)
                        max_recv_ns = recv_ns if max_recv_ns is None else max(max_recv_ns, recv_ns)
                        observed_hours.add(row_hour)
                        continue
                    raw = outer["raw"]
                    if not isinstance(raw, str):
                        raise ValueError("raw is not a string")
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        raise ValueError("inner frame is not an object")
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    dq_records.append({"hour": path_hour,
                                       "key": "malformed_recorder_row"})
                    continue
                recorder_rows += 1
                min_recv_ns = recv_ns if min_recv_ns is None else min(min_recv_ns, recv_ns)
                max_recv_ns = recv_ns if max_recv_ns is None else max(max_recv_ns, recv_ns)
                observed_hours.add(row_hour)

                typ = frame.get("type")
                if typ == "subscribed":
                    ack = frame.get("msg")
                    if (isinstance(ack, dict) and ack.get("channel") == "communications" and
                            type(ack.get("sid")) is int and ack["sid"] > 0):
                        subscribed_hours.add(row_hour)
                    else:
                        dq_records.append({"hour": row_hour,
                                           "key": "invalid_subscribed_ack"})
                elif typ in ("error", "unsubscribed"):
                    dq_records.append({"hour": row_hour,
                                       "key": "unexpected_communications_" + typ})
                if typ not in RFQ_TYPES:
                    excluded_records.append({"hour": row_hour,
                                             "key": str(typ or "_missing")})
                    continue
                if type(frame.get("sid")) is not int or frame["sid"] <= 0:
                    dq_records.append({"hour": row_hour, "key": "invalid_or_missing_sid"})
                    continue
                msg = frame.get("msg")
                if not isinstance(msg, dict):
                    dq_records.append({"hour": row_hour, "key": "missing_msg"})
                    continue
                missing = [f for f in REQUIRED[typ] if f not in msg or msg[f] is None]
                if missing:
                    dq_records.append({"hour": row_hour, "key": "missing_required_field"})
                    continue
                if not all(isinstance(msg.get(k), str) for k in REQUIRED[typ]):
                    dq_records.append({"hour": row_hour,
                                       "key": "invalid_required_field_type"})
                    continue
                if not msg["id"] or not msg["market_ticker"]:
                    dq_records.append({"hour": row_hour,
                                       "key": "empty_required_identifier"})
                    continue
                for opt in ("event_ticker", "mve_collection_ticker"):
                    if msg.get(opt) is not None and not isinstance(msg[opt], str):
                        dq_records.append({"hour": row_hour,
                                           "key": "invalid_optional_field_type"})
                        break
                else:
                    pass
                if any(msg.get(opt) is not None and not isinstance(msg[opt], str)
                       for opt in ("event_ticker", "mve_collection_ticker")):
                    continue
                ts_field = "created_ts" if typ == "rfq_created" else "deleted_ts"
                try:
                    exchange_dt = parse_iso(msg[ts_field])
                    contracts_e2 = fixed_exact(msg.get("contracts_fp"),
                                               "contracts_fp", 2)
                    target_e6 = fixed_exact(msg.get("target_cost_dollars"),
                                            "target_cost_dollars", 6)
                except (ValueError, OverflowError):
                    dq_records.append({"hour": row_hour,
                                       "key": "invalid_fixedpoint_or_timestamp"})
                    continue
                legs = msg.get("mve_selected_legs")
                if legs is not None and not isinstance(legs, list):
                    dq_records.append({"hour": row_hour,
                                       "key": "invalid_mve_selected_legs"})
                    continue
                legs_ok = True
                for leg in legs or []:
                    if not isinstance(leg, dict):
                        legs_ok = False
                        break
                    for key in ("event_ticker", "market_ticker", "side"):
                        if leg.get(key) is not None and not isinstance(leg[key], str):
                            legs_ok = False
                    try:
                        fixed_exact(leg.get("yes_settlement_value_dollars"),
                                    "yes_settlement_value_dollars", 6)
                    except ValueError:
                        legs_ok = False
                if not legs_ok:
                    dq_records.append({"hour": row_hour,
                                       "key": "invalid_mve_selected_legs"})
                    continue
                events.append({
                    "type": typ,
                    "rfq_id": msg["id"],
                    "creator_id": msg.get("creator_id", ""),
                    "market_ticker": msg["market_ticker"],
                    "event_ticker": msg.get("event_ticker"),
                    "contracts_e2": contracts_e2,
                    "target_cost_e6": target_e6,
                    "exchange_ts": exchange_dt,
                    "exchange_ts_text": msg[ts_field],
                    "recv_wall_ns": recv_ns,
                    "recv_date": dt.datetime.fromtimestamp(
                        recv_ns / 1e9, tz=dt.timezone.utc).date().isoformat(),
                    "mve_collection_ticker": msg.get("mve_collection_ticker"),
                    "mve_selected_legs": legs,
                    "source_file": path,
                    "source_line": line_no,
                })
    for hour, rows in capture_shard_sets.items():
        rows.sort(key=lambda row: int(row["ordinal"]))
        ordinals = [int(row["ordinal"]) for row in rows]
        if ordinals != list(range(len(ordinals))):
            dq_records.append({"hour": hour,
                               "key": "non_contiguous_capture_shards"})
        inodes = [(int(row["device"]), int(row["inode"])) for row in rows]
        if len(set(inodes)) != len(inodes):
            dq_records.append({"hour": hour, "key": "capture_shard_inode_alias"})
    return {
        "events": events,
        "dq_records": dq_records,
        "excluded_records": excluded_records,
        "marker_records": marker_records,
        "segment_receipts": segment_receipts,
        "observed_hours": observed_hours,
        "subscribed_hours": subscribed_hours,
        "file_hours": file_hours,
        "input_files": input_files,
        "capture_shard_sets": capture_shard_sets,
        "recorder_rows": recorder_rows,
        "min_recv_ns": min_recv_ns,
        "max_recv_ns": max_recv_ns,
    }


def series_from_market(ticker: str) -> str:
    return ticker.split("-", 1)[0] if ticker else ""


def load_classification(warehouse_root: str | None) -> dict[str, str]:
    if not warehouse_root:
        return {}
    path = Path(warehouse_root) / "catalog" / "series_classified" / "part-00000.parquet"
    if not path.exists():
        return {}
    import duckdb
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT series_ticker, category FROM read_parquet(?)", [str(path)]
        ).fetchall()
    finally:
        con.close()
    return {str(series): str(category) for series, category in rows if series and category}


def _fixed_summary(values: list[int], places: int) -> dict:
    suffix = "_e%d" % places
    summary = {
        "n": len(values),
        "scale_places": places,
        "p10" + suffix: nearest_rank(values, 10),
        "p50" + suffix: nearest_rank(values, 50),
        "p90" + suffix: nearest_rank(values, 90),
        "p99" + suffix: nearest_rank(values, 99),
        "min" + suffix: min(values) if values else None,
        "max" + suffix: max(values) if values else None,
    }
    if not values:
        summary["histogram"] = []
        return summary
    lo, hi = min(values), max(values)
    if lo == hi:
        summary["histogram"] = [{
            "lo" + suffix: lo, "hi" + suffix: hi, "n": len(values)}]
        return summary
    width = max(1, (hi - lo + 9) // 10)
    counts = [0] * 10
    for value in values:
        counts[min(9, (value - lo) // width)] += 1
    bins = []
    for i, count in enumerate(counts):
        if count == 0:
            continue
        bin_lo = lo + i * width
        bin_hi = hi if i == 9 else min(hi, bin_lo + width - 1)
        bins.append({"lo" + suffix: bin_lo, "hi" + suffix: bin_hi, "n": count})
    summary["histogram"] = bins
    return summary


def _numeric_summary(values: list[int], unit: str) -> dict:
    return {
        "n": len(values),
        "p10_" + unit: nearest_rank(values, 10),
        "p50_" + unit: nearest_rank(values, 50),
        "p90_" + unit: nearest_rank(values, 90),
        "p99_" + unit: nearest_rank(values, 99),
        "min_" + unit: min(values) if values else None,
        "max_" + unit: max(values) if values else None,
    }


def _expected_hours(start: dt.datetime, hours: int) -> list[str]:
    start = start.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(hours)]


def _aggregate_records(records: list[dict], expected: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in records:
        if row.get("hour") not in expected:
            continue
        key = str(row.get("key") or "_unknown")
        out[key] = out.get(key, 0) + 1
    return out


def _valid_capture_relpath(value, segment_hour: str,
                           expected_ordinal: int | None = None) -> bool:
    if not isinstance(value, str):
        return False
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2:
        return False
    matched_hour = hour_key_from_path("/" + rel.as_posix())
    ordinal = shard_ordinal_from_path("/" + rel.as_posix())
    return (matched_hour == segment_hour and ordinal is not None and
            (expected_ordinal is None or ordinal == expected_ordinal))


def _v3_evidence_contract_errors(row: dict, segment_hour: str) -> list[str]:
    """Mechanically validate the health evidence required for v3 eligibility."""
    errors: list[str] = []
    if row.get("findings") != []:
        errors.append("v3 receipt findings are missing/non-empty")
    try:
        start = dt.datetime.strptime(segment_hour, "%Y-%m-%dT%H").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return ["v3 segment_hour is not an exact UTC hour"]
    if start.strftime("%Y-%m-%dT%H") != segment_hour:
        return ["v3 segment_hour is not an exact UTC hour"]
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int((start + dt.timedelta(hours=1)).timestamp() * 1_000_000_000)
    if (type(row.get("expected_start_wall_ns")) is not int or
            row.get("expected_start_wall_ns") != start_ns):
        errors.append("v3 expected_start_wall_ns does not match segment_hour")
    if (type(row.get("expected_end_wall_ns")) is not int or
            row.get("expected_end_wall_ns") != end_ns):
        errors.append("v3 expected_end_wall_ns does not match segment_hour")
    if "child_rc" not in row or row.get("child_rc") is not None:
        errors.append("v3 PASS boundary receipt child_rc must be null")

    raw = row.get("raw_evidence")
    if not isinstance(raw, dict):
        errors.append("v3 raw_evidence is missing/invalid")
    else:
        if type(raw.get("recorder_rows")) is not int or raw["recorder_rows"] < 1:
            errors.append("v3 raw recorder_rows is missing/invalid")
        markers = raw.get("markers")
        if (not isinstance(markers, dict) or
                type(markers.get("hour_open")) is not int or
                markers.get("hour_open") != 1):
            errors.append("v3 raw hour_open count is not exactly one")
        elif any(type(markers.get(key, 0)) is not int or
                 markers.get(key, 0) != 0 for key in
                 ("loss", "gap", "transport_close", "transport_error")):
            errors.append("v3 raw evidence contains a blocking stream marker")
        if (type(raw.get("partition_mismatches")) is not int or
                raw.get("partition_mismatches") != 0):
            errors.append("v3 raw partition_mismatches is not zero")
        if (type(raw.get("subscription_invalidations")) is not int or
                raw.get("subscription_invalidations") != 0):
            errors.append("v3 raw subscription_invalidations is not zero")
        if raw.get("findings") != []:
            errors.append("v3 raw findings are missing/non-empty")

    metrics = row.get("metrics_evidence")
    if not isinstance(metrics, dict):
        errors.append("v3 metrics_evidence is missing/invalid")
    else:
        if metrics.get("findings") != []:
            errors.append("v3 metrics findings are missing/non-empty")
        zero_fields = (
            "min_reconnects", "max_reconnects",
            "min_disconnects", "max_disconnects",
            "min_errors", "max_errors",
            "max_recorder_dropped", "max_recorder_write_failures",
        )
        for field in zero_fields:
            if type(metrics.get(field)) is not int or metrics[field] != 0:
                errors.append("v3 metrics %s is missing/non-zero" % field)
        feed_rows = metrics.get("feed_rows")
        connected = metrics.get("connected_valid_rows")
        if type(feed_rows) is not int or feed_rows < 1:
            errors.append("v3 metrics feed_rows is missing/invalid")
        if (type(connected) is not int or connected < 1 or
                type(feed_rows) is not int or connected > feed_rows or
                connected * 10 < feed_rows * 8):
            errors.append("v3 connected-valid heartbeat coverage is unhealthy")
        first_ms = metrics.get("first_ts_ms")
        last_ms = metrics.get("last_ts_ms")
        lo_ms, hi_ms = start_ns // 1_000_000, end_ns // 1_000_000
        if (type(first_ms) is not int or first_ms < lo_ms or
                first_ms > lo_ms + 5_000):
            errors.append("v3 metrics do not cover the hour start")
        if (type(last_ms) is not int or last_ms < hi_ms - 5_000 or
                last_ms >= hi_ms):
            errors.append("v3 metrics do not cover the hour end")
    return errors


def receipt_contract_errors(row: dict) -> list[str]:
    """Validate receipt shape; v2 remains valid only for one legacy object."""
    errors: list[str] = []
    stability = row.get("close_stability_ms")
    if row.get("status") != "PASS":
        errors.append("status is not PASS")
    if row.get("findings"):
        errors.append("receipt findings are non-empty")
    if row.get("subscription_proven") is not True:
        errors.append("subscription is not proven")
    if row.get("boundary_closed") is not True or row.get("end_reason") != "boundary":
        errors.append("hour boundary is not closed")
    if type(stability) is not int or stability < 1_000:
        errors.append("close stability is below 1000ms")
    schema = row.get("schema")
    segment_hour = row.get("segment_hour")
    if not isinstance(segment_hour, str):
        errors.append("segment_hour is missing")
        return errors
    if schema == "rfq-segment-receipt-v2":
        if row.get("capture_bytes_before") != 0:
            errors.append("v2 capture has pre-existing bytes")
        if not _valid_capture_relpath(row.get("capture_relpath"), segment_hour, 0):
            errors.append("v2 capture_relpath is invalid")
        if (type(row.get("capture_bytes_at_close")) is not int or
                row["capture_bytes_at_close"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}",
                                 str(row.get("capture_sha256_at_close") or ""))):
            errors.append("v2 capture identity is invalid")
        return errors
    if schema != "rfq-segment-receipt-v3":
        errors.append("unsupported receipt schema")
        return errors
    errors.extend(_v3_evidence_contract_errors(row, segment_hour))
    shards = row.get("capture_shards")
    if not isinstance(shards, list) or not shards:
        errors.append("v3 capture_shards is empty/invalid")
        return errors
    if (type(row.get("capture_shard_count")) is not int or
            row.get("capture_shard_count") != len(shards)):
        errors.append("v3 capture_shard_count mismatch")
    seen_relpaths: set[str] = set()
    shard_members_valid = True
    for expected_ordinal, shard in enumerate(shards):
        if not isinstance(shard, dict):
            errors.append("v3 shard member is not an object")
            shard_members_valid = False
            continue
        if shard.get("ordinal") != expected_ordinal:
            errors.append("v3 shard ordinals are not contiguous")
        relpath = shard.get("relpath")
        if not _valid_capture_relpath(relpath, segment_hour, expected_ordinal):
            errors.append("v3 shard relpath/hour/ordinal mismatch")
        if relpath in seen_relpaths:
            errors.append("v3 duplicate shard relpath")
        if isinstance(relpath, str):
            seen_relpaths.add(relpath)
        if shard.get("bytes_before") != 0:
            errors.append("v3 shard has pre-existing bytes")
        if type(shard.get("size")) is not int or shard["size"] < 0:
            errors.append("v3 shard size is invalid")
        if (type(shard.get("parsed_bytes_at_close")) is not int or
                shard.get("parsed_bytes_at_close") != shard.get("size")):
            errors.append("v3 shard is not parsed through exact EOF")
        if not re.fullmatch(r"[0-9a-f]{64}", str(shard.get("sha256") or "")):
            errors.append("v3 shard SHA-256 is invalid")
    if shard_members_valid:
        try:
            shard_set_sha256 = capture_shard_set_sha256(shards)
        except ValueError:
            errors.append("v3 capture_shards canonical set is invalid")
        else:
            if row.get("capture_shard_set_sha256") != shard_set_sha256:
                errors.append("v3 capture_shard_set_sha256 mismatch")
    return sorted(set(errors))


def load_seal_index(seals_root: str, raw_root: str, dates: set[str]) -> tuple[dict, list[str]]:
    """Load full_v2 day-seal raw proofs as absolute-path -> proof."""
    index: dict[str, dict] = {}
    errors: list[str] = []
    for date in sorted(dates):
        path = Path(seals_root) / ("date=" + date + ".json")
        try:
            seal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append("missing/unreadable day seal %s" % path)
            continue
        if seal.get("status") != "SEALED" or seal.get("method") != "full_v2":
            errors.append("non-full_v2 seal %s" % path)
            continue
        for proof in seal.get("raw_files", []):
            rel = proof.get("file")
            if isinstance(rel, str):
                index[str((Path(raw_root) / rel).resolve())] = proof
    return index, errors


def build_report(parsed: dict, classification: dict[str, str], *,
                 start: dt.datetime | None = None, hours: int = 48,
                 seal_index: dict[str, dict] | None = None,
                 seal_errors: list[str] | None = None) -> dict:
    events = parsed["events"]
    if start is None:
        if parsed["min_recv_ns"] is None:
            raise ReportError("no readable recorder rows")
        start = dt.datetime.fromtimestamp(
            parsed["min_recv_ns"] / 1e9, tz=dt.timezone.utc
        ).replace(minute=0, second=0, microsecond=0)
    if start != start.replace(minute=0, second=0, microsecond=0):
        raise ReportError("T0 must be aligned to an exact UTC-hour boundary")
    end = start + dt.timedelta(hours=hours)
    lo_ns, hi_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)
    expected = _expected_hours(start, hours)
    expected_set = set(expected)
    events = [e for e in events if lo_ns <= e["recv_wall_ns"] < hi_ns]
    # Dedupe only AFTER the fixed window is selected; older history can never
    # suppress an in-window observation.
    deduped: list[dict] = []
    seen: set[tuple] = set()
    duplicate_events = 0
    for event in sorted(events, key=lambda e: e["recv_wall_ns"]):
        identity = (event["type"], event["rfq_id"], event["exchange_ts_text"],
                    event["market_ticker"])
        if identity in seen:
            duplicate_events += 1
            continue
        seen.add(identity)
        deduped.append(event)
    events = deduped
    dq_window = _aggregate_records(parsed["dq_records"], expected_set)
    if duplicate_events:
        dq_window["duplicate_event"] = duplicate_events
    excluded_window = _aggregate_records(parsed["excluded_records"], expected_set)
    markers_window = _aggregate_records(parsed["marker_records"], expected_set)
    file_hours = set(expected) & parsed["file_hours"]
    subscribed_hours = set(expected) & parsed["subscribed_hours"]
    hour_open_hours = {
        row.get("hour") for row in parsed["marker_records"]
        if row.get("key") == "hour_open" and row.get("hour") in expected_set
    }
    observed = sorted(set(expected) & parsed["observed_hours"])
    missing_files = sorted(set(expected) - file_hours)
    missing_hour_open = sorted(set(expected) - hour_open_hours)
    # A persistent WebSocket authenticates once and normally has no new
    # subscribe ACK at hourly file boundaries. Per-hour proof comes from the
    # durable receipt's subscription_proven state, not a forced reconnect.
    ack_diagnostic_missing = sorted(set(expected) - subscribed_hours)
    loss_markers = sum(markers_window.get(k, 0) for k in (
        "loss", "gap", "transport_close", "transport_error"))
    receipts = [r for r in parsed["segment_receipts"]
                if r.get("segment_hour") in expected_set]
    receipt_by_hour: dict[str, list[dict]] = {}
    for receipt in receipts:
        receipt_by_hour.setdefault(receipt["segment_hour"], []).append(receipt)
    missing_receipts = sorted(h for h in expected if h not in receipt_by_hour)

    def healthy_receipt(rows: list[dict]) -> bool:
        if len(rows) != 1:
            return False
        return not receipt_contract_errors(rows[0])

    unhealthy_receipt_hours = sorted(
        h for h, rows in receipt_by_hour.items()
        if not healthy_receipt(rows))
    missing = sorted(set(missing_files) | set(missing_hour_open) |
                     set(missing_receipts) | set(unhealthy_receipt_hours))

    required_files = [f for f in parsed["input_files"] if f.get("hour") in expected_set]
    receipt_sources = {r.get("_source_file") for r in receipts if r.get("_source_file")}
    required_files.extend(f for f in parsed["input_files"] if f.get("path") in receipt_sources)
    required_files = list({f["path"]: f for f in required_files}.values())
    seal_index = seal_index or {}
    seal_failures = list(seal_errors or [])
    for item in required_files:
        proof = seal_index.get(str(Path(item["path"]).resolve()))
        if not proof:
            seal_failures.append("not present in day seal: %s" % item["path"])
        elif int(proof.get("size", -1)) != item["bytes"] or proof.get("sha256") != item["sha256"]:
            seal_failures.append("day-seal size/SHA mismatch: %s" % item["path"])
    capture_shard_sets = parsed.get("capture_shard_sets", {})
    for receipt in receipts:
        if receipt.get("status") != "PASS":
            continue
        segment_hour = receipt.get("segment_hour")
        actual_rows = sorted(capture_shard_sets.get(segment_hour, []),
                             key=lambda row: int(row.get("ordinal", -1)))
        actual_relpaths = ["/".join(Path(row["path"]).parts[-2:])
                           for row in actual_rows]
        if receipt.get("schema") == "rfq-segment-receipt-v2":
            capture_relpath = receipt.get("capture_relpath")
            if len(actual_rows) != 1 or actual_relpaths != [capture_relpath]:
                seal_failures.append(
                    "v2 PASS receipt does not bind complete shard set: %s" %
                    (segment_hour or "<missing>"))
                continue
            item = actual_rows[0]
            if (receipt.get("capture_bytes_at_close") != item["bytes"] or
                    receipt.get("capture_sha256_at_close") != item["sha256"]):
                seal_failures.append(
                    "post-receipt append or receipt SHA mismatch: %s" %
                    capture_relpath)
            continue
        if receipt.get("schema") != "rfq-segment-receipt-v3":
            continue
        claimed = receipt.get("capture_shards") or []
        claimed_relpaths = [row.get("relpath") for row in claimed
                            if isinstance(row, dict)]
        if claimed_relpaths != actual_relpaths:
            seal_failures.append(
                "v3 PASS receipt does not bind complete shard set: %s" %
                (segment_hour or "<missing>"))
            continue
        for claim, item in zip(claimed, actual_rows):
            if (claim.get("size") != item["bytes"] or
                    claim.get("sha256") != item["sha256"]):
                seal_failures.append(
                    "post-receipt append or receipt SHA mismatch: %s" %
                    claim.get("relpath"))

    created_by_id: dict[str, dict] = {}
    deletes_by_id: dict[str, list[dict]] = {}
    id_collisions = 0
    join_mismatches = 0
    unmatched_deletes = 0
    for event in sorted(events, key=lambda e: e["recv_wall_ns"]):
        if event["type"] == "rfq_created":
            if event["rfq_id"] in created_by_id:
                id_collisions += 1
                continue
            created_by_id[event["rfq_id"]] = dict(event)
        else:
            deletes_by_id.setdefault(event["rfq_id"], []).append(event)

    lifetimes_ms: list[int] = []
    known_requesters = 0
    requester_counts: dict[str, int] = {}
    for rfq_id, create in created_by_id.items():
        deletes = deletes_by_id.get(rfq_id, [])
        consistent = []
        for candidate in deletes:
            mismatch = (
                candidate["market_ticker"] != create["market_ticker"] or
                candidate["exchange_ts"] < create["exchange_ts"] or
                (candidate["contracts_e2"] is not None and create["contracts_e2"] is not None and
                 candidate["contracts_e2"] != create["contracts_e2"]) or
                (candidate["target_cost_e6"] is not None and create["target_cost_e6"] is not None and
                 candidate["target_cost_e6"] != create["target_cost_e6"])
            )
            if mismatch:
                join_mismatches += 1
            else:
                consistent.append(candidate)
        delete = min(consistent, key=lambda e: e["recv_wall_ns"]) if consistent else None
        creator = (delete or create).get("creator_id", "")
        if creator:
            known_requesters += 1
            requester_counts[creator] = requester_counts.get(creator, 0) + 1
        create["joined_creator_id"] = creator or None
        create["deleted"] = bool(delete)
        if delete:
            delta_ms = int((delete["exchange_ts"] - create["exchange_ts"]).total_seconds() * 1000)
            if delta_ms >= 0:
                lifetimes_ms.append(delta_ms)
        if create["contracts_e2"] is None and create["target_cost_e6"] is None:
            create["size_mode"] = "missing"
        elif create["contracts_e2"] is not None and create["target_cost_e6"] is not None:
            # A create request selects one sizing input, but broadcast/read
            # representations may report the exchange-derived counterpart too
            # (the official examples show both). Keep both exact populations;
            # do not invent a DQ violation.
            create["size_mode"] = "both_reported"
        elif create["contracts_e2"] is not None:
            create["size_mode"] = "contracts"
        else:
            create["size_mode"] = "target_cost"
        create["combo"] = bool(create.get("mve_collection_ticker") or
                               create.get("mve_selected_legs"))
        create["category"] = classification.get(
            series_from_market(create["market_ticker"]), "_unclassified")
    for rfq_id, deletes in deletes_by_id.items():
        if rfq_id not in created_by_id:
            unmatched_deletes += len(deletes)

    creates = list(created_by_id.values())
    requests_by_day_category: dict[str, dict[str, int]] = {}
    for row in creates:
        day = row["recv_date"]
        by_cat = requests_by_day_category.setdefault(day, {})
        by_cat[row["category"]] = by_cat.get(row["category"], 0) + 1

    contracts = [r["contracts_e2"] for r in creates if r["contracts_e2"] is not None]
    targets = [r["target_cost_e6"] for r in creates if r["target_cost_e6"] is not None]
    combo_n = sum(1 for r in creates if r["combo"])
    deleted_n = sum(1 for r in creates if r["deleted"])
    category_known_n = sum(1 for r in creates if r["category"] != "_unclassified")
    size_missing = sum(1 for r in creates if r["size_mode"] == "missing")
    size_both = sum(1 for r in creates if r["size_mode"] == "both_reported")
    ranked_requesters = sorted(requester_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    total_known = sum(requester_counts.values())
    top1 = ranked_requesters[0][1] if ranked_requesters else 0
    top5 = sum(v for _, v in ranked_requesters[:5])
    hhi_num = sum(v * v for v in requester_counts.values())
    if join_mismatches:
        dq_window["create_delete_join_mismatch"] = join_mismatches
    if id_collisions:
        dq_window["rfq_id_collision"] = id_collisions
    blocking_dq = sum(dq_window.values())

    report = {
        "schema": "rfq-flow-report-v1",
        "tier": "EXPLORATORY",
        "trading_gate": False,
        "official_asyncapi_sha256": SPEC_SHA256,
        "window": {
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc_exclusive": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "required_hours": hours,
            "raw_observed_hours": len(observed),
            "hour_files_present": len(file_hours),
            "hour_open_markers": len(hour_open_hours),
            "connection_ack_hours": len(subscribed_hours),
            "subscribed_hours": len(subscribed_hours),  # diagnostic alias
            "observed_hours": len((set(file_hours) & set(hour_open_hours)) -
                                  set(unhealthy_receipt_hours)),
            "missing_file_hours": missing_files,
            "missing_hour_open_hours": missing_hour_open,
            "hours_without_new_subscription_ack": ack_diagnostic_missing,
            "missing_receipt_hours": missing_receipts,
            "unhealthy_receipt_hours": unhealthy_receipt_hours,
            "missing_hours": missing,
            "loss_or_gap_markers": loss_markers,
            "blocking_stream_markers": loss_markers,
            "seal_failures": seal_failures,
            "complete": (not missing and loss_markers == 0 and
                         blocking_dq == 0 and not seal_failures),
        },
        "hypotheses": [
            {"id": "RFQ-FLOW-01", "question": "How does request flow vary by day/category?",
             "status": "EXPLORATORY_NO_INFERENCE"},
            {"id": "RFQ-SIZE-01", "question": "What are the separate contract and target-cost size distributions?",
             "status": "EXPLORATORY_NO_INFERENCE"},
            {"id": "RFQ-COMBO-01", "question": "What share of broadcast requests are combo RFQs?",
             "status": "EXPLORATORY_NO_INFERENCE"},
            {"id": "RFQ-REQUESTER-01", "question": "How concentrated is flow among known static requester IDs?",
             "status": "EXPLORATORY_NO_INFERENCE"},
        ],
        "counts": {
            "recorder_rows": parsed["recorder_rows"],
            "rfq_events_in_window": len(events),
            "unique_requests": len(creates),
            "created_deleted_matches": deleted_n,
            "unmatched_deletes": unmatched_deletes,
            "rfq_id_collisions": id_collisions,
        },
        "requests_by_day_category": requests_by_day_category,
        "contracts_fp": _fixed_summary(contracts, 2),
        "target_cost_dollars": _fixed_summary(targets, 6),
        "size_missing_n": size_missing,
        "size_both_reported_n": size_both,
        "combo": {
            "combo_n": combo_n,
            "single_or_unclassified_n": len(creates) - combo_n,
            "combo_share_bps": bps(combo_n, len(creates)),
        },
        "hvm": {
            "exact_share": None,
            "known_hvm_lower_bound_n": combo_n,
            "known_hvm_lower_bound_share_bps": bps(combo_n, len(creates)),
            "reason": "Official docs guarantee every combo is HVM, but communications payloads expose no general is_hvm field; non-combo HVM status is UNKNOWN.",
        },
        "requesters": {
            "population": "known_deleted_subset",
            "censoring_warning": "Requester concentration is conditional on RFQs whose public creator_id became observable via a matched delete inside the window; short-lived requests are overrepresented.",
            "known_id_requests": known_requesters,
            "known_id_coverage_bps": bps(known_requesters, len(creates)),
            "distinct_known_ids": len(requester_counts),
            "top1_share_bps": bps(top1, total_known),
            "top5_share_bps": bps(top5, total_known),
            "hhi_bps": bps(hhi_num, total_known * total_known),
            "full_cohort_top1_lower_bound_bps": bps(top1, len(creates)),
            "full_cohort_top1_upper_bound_bps": bps(
                top1 + (len(creates) - known_requesters), len(creates)),
            "ranked": [
                {"requester_hash": hashlib.sha256(k.encode()).hexdigest()[:12], "requests": v}
                for k, v in ranked_requesters
            ],
        },
        "lifecycle": {
            "deleted_match_coverage_bps": bps(deleted_n, len(creates)),
            "lifetime_ms": _numeric_summary(lifetimes_ms, "ms"),
        },
        "data_quality": {
            "parse_and_schema_counts": dq_window,
            "excluded_frame_types": excluded_window,
            "recorder_markers": markers_window,
            "category_join_coverage_bps": bps(category_known_n, len(creates)),
            "creator_id_note": "rfq_created.creator_id is currently empty by official contract; concentration uses delete-joined IDs only and excludes empty IDs.",
        },
        "segment_receipts": receipts,
        "input_files": required_files,
    }
    return report


def _svg_bars(items: list[tuple[str, int]], title: str, *, suffix: str = "",
              value_formatter=None) -> str:
    items = items[:20]
    width, row_h, left = 760, 28, 210
    height = max(90, 52 + row_h * max(1, len(items)))
    maximum = max([v for _, v in items] + [1])
    rows = []
    for i, (label, value) in enumerate(items):
        y = 38 + i * row_h
        bar = int((width - left - 40) * value / maximum)
        shown = value_formatter(value) if value_formatter else (str(value) + suffix)
        rows.append(
            '<text x="4" y="%d" class="lbl">%s</text>'
            '<rect x="%d" y="%d" width="%d" height="16" rx="3"/>'
            '<text x="%d" y="%d" class="val">%s%s</text>'
            % (y + 13, html.escape(label[:34]), left, y, bar, left + bar + 7,
               y + 13, html.escape(shown), "")
        )
    if not items:
        rows.append('<text x="10" y="55" class="lbl">No observations</text>')
    return ('<section><h2>%s</h2><svg viewBox="0 0 %d %d" role="img">%s</svg></section>'
            % (html.escape(title), width, height, "".join(rows)))


def _histogram_items(summary: dict, places: int, *, prefix: str = "") -> list[tuple[str, int]]:
    suffix = "_e%d" % places
    out = []
    for row in summary.get("histogram", []):
        lo, hi = row["lo" + suffix], row["hi" + suffix]
        label = fixed_text(lo, places)
        if hi != lo:
            label += "–" + fixed_text(hi, places)
        out.append((prefix + label, row["n"]))
    return out


def render_html(report: dict) -> str:
    req_items = []
    for day, cats in sorted(report["requests_by_day_category"].items()):
        req_items.extend((day + " / " + cat, n) for cat, n in sorted(cats.items()))
    combo = report["combo"]
    rq = report["requesters"]
    window = report["window"]
    hourly = [("observed", window["observed_hours"]),
              ("missing", len(window["missing_hours"]))]
    requester_items = [(r["requester_hash"], r["requests"]) for r in rq["ranked"][:20]]
    charts = [
        _svg_bars(req_items, "1. Requests/day by category"),
        _svg_bars(_histogram_items(report["contracts_fp"], 2),
                  "2. contracts_fp histogram (n=%d, absent=%d)" %
                  (report["contracts_fp"]["n"],
                   report["counts"]["unique_requests"] - report["contracts_fp"]["n"])),
        _svg_bars(_histogram_items(report["target_cost_dollars"], 6, prefix="$"),
                  "3. target_cost_dollars histogram (n=%d, absent=%d)" %
                  (report["target_cost_dollars"]["n"],
                   report["counts"]["unique_requests"] - report["target_cost_dollars"]["n"])),
        _svg_bars([("combo", combo["combo_n"]),
                   ("single / unknown-HVM", combo["single_or_unclassified_n"])],
                  "4. Combo vs single"),
        _svg_bars(requester_items, "5. Requester concentration — known-deleted subset only (hashed IDs)"),
        _svg_bars([("known HVM lower bound (combo)", combo["combo_n"]),
                   ("HVM status unknown", combo["single_or_unclassified_n"])],
                  "6. HVM share: verifiable lower bound only"),
        _svg_bars([("created→deleted matched", report["counts"]["created_deleted_matches"]),
                   ("right-censored/unmatched", report["counts"]["unique_requests"] -
                    report["counts"]["created_deleted_matches"])],
                  "7. Created→deleted coverage"),
        _svg_bars(hourly, "8. Hourly capture coverage"),
    ]
    dq_rows = "".join("<tr><td>%s</td><td>%s</td></tr>" %
                      (html.escape(k), v) for k, v in sorted(
                          report["data_quality"]["parse_and_schema_counts"].items())) or \
              "<tr><td>parse/schema errors</td><td>0</td></tr>"
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>RFQ Flow Atlas</title>
<style>
body{font:14px system-ui;background:#0d1117;color:#e6edf3;margin:24px;max-width:1100px}
.banner{padding:14px;background:#5a1d1d;border:2px solid #ff7b72;border-radius:8px;font-weight:700}
.note{padding:12px;background:#161b22;border-left:4px solid #d29922;margin:16px 0}
section{background:#161b22;margin:18px 0;padding:14px;border-radius:8px}h1,h2{margin-top:0}
svg{width:100%%;background:#0d1117;border-radius:6px}rect{fill:#58a6ff}.lbl{fill:#c9d1d9;font-size:12px}.val{fill:#f0f6fc;font-size:12px}
table{border-collapse:collapse;width:100%%}td,th{padding:7px;border-bottom:1px solid #30363d;text-align:left}code{color:#79c0ff}
</style></head><body>
<div class="banner">EXPLORATORY — NOT A TRADING GATE — NO PROFITABILITY CLAIM</div>
<h1>Kalshi RFQ Flow Atlas — first 48-hour report</h1>
<p>Window: <code>%(start)s</code> → <code>%(end)s</code>. Unique requests: <b>%(n)s</b>.</p>
<div class="note"><b>HVM limitation:</b> %(hvm)s</div>
<div class="note"><b>Requester censoring:</b> %(rqnote)s</div>
%(charts)s
<section><h2>Data-quality ledger</h2><table><tr><th>Condition</th><th>Count</th></tr>%(dq)s</table>
<p>Missing hours: <code>%(missing)s</code></p>
<p>Known requester-ID coverage: %(rqcov)s bps; category join coverage: %(catcov)s bps; missing both size fields: %(sizebad)s.</p></section>
</body></html>""" % {
        "start": window["start_utc"], "end": window["end_utc_exclusive"],
        "n": report["counts"]["unique_requests"], "hvm": html.escape(report["hvm"]["reason"]),
        "rqnote": html.escape(rq["censoring_warning"]),
        "charts": "".join(charts), "dq": dq_rows,
        "missing": html.escape(", ".join(window["missing_hours"]) or "none"),
        "rqcov": rq["known_id_coverage_bps"],
        "catcov": report["data_quality"]["category_join_coverage_bps"],
        "sizebad": report["size_missing_n"],
    }


def write_report(report: dict, out_dir: str) -> tuple[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "rfq_flow_report.json"
    html_path = out / "index.html"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return str(json_path), str(html_path)


def parse_start(value: str | None) -> dt.datetime | None:
    return parse_iso(value) if value else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--raw-root", default=str(ROOT / "work" / "raw"))
    p.add_argument("--warehouse-root", default=str(ROOT / "work" / "warehouse"))
    p.add_argument("--seals-root", default=str(ROOT / "work" / "warehouse" / "seals"))
    p.add_argument("--start", help="fixed UTC window start (RFC3339); default earliest capture hour")
    p.add_argument("--hours", type=int, default=48)
    p.add_argument("--out", default=str(ROOT / "work" / "research" / "rfq_flow_48h"))
    p.add_argument("--allow-incomplete", action="store_true",
                   help="render diagnostic despite missing hourly coverage")
    args = p.parse_args(argv)
    if args.hours <= 0:
        p.error("--hours must be positive")
    paths = discover(args.raw_root)
    if not paths:
        print("RFQ FLOW REPORT REFUSED: no rfq_*.ndjson* files under %s" % args.raw_root,
              file=sys.stderr)
        return 2
    parsed = parse_paths(paths)
    start = parse_start(args.start)
    if start is None and not args.allow_incomplete:
        print("RFQ FLOW REPORT REFUSED: --start is required for a complete report; pin T0 to the first full UTC-hour boundary",
              file=sys.stderr)
        return 2
    if start is not None:
        end = start + dt.timedelta(hours=args.hours)
        if end > dt.datetime.now(dt.timezone.utc) and not args.allow_incomplete:
            print("RFQ FLOW REPORT REFUSED: requested window ends in the future", file=sys.stderr)
            return 2
        dates = {(start + dt.timedelta(hours=i)).date().isoformat()
                 for i in range(args.hours)}
        seal_index, seal_errors = load_seal_index(args.seals_root, args.raw_root, dates)
    else:
        seal_index, seal_errors = {}, ["explicit T0 absent; seals not evaluated"]
    try:
        report = build_report(parsed, load_classification(args.warehouse_root),
                              start=start, hours=args.hours, seal_index=seal_index,
                              seal_errors=seal_errors)
    except (ReportError, ValueError) as exc:
        print("RFQ FLOW REPORT REFUSED: %s" % exc, file=sys.stderr)
        return 2
    if not report["window"]["complete"] and not args.allow_incomplete:
        print("RFQ FLOW REPORT REFUSED: fixed window is incomplete (%d/%d receipt-backed hourly segments; missing %s)"
              % (report["window"]["observed_hours"], report["window"]["required_hours"],
                 ",".join(report["window"]["missing_hours"][:8])), file=sys.stderr)
        return 2
    json_path, html_path = write_report(report, args.out)
    print("RFQ FLOW REPORT %s: %s | %s" %
          ("PASS" if report["window"]["complete"] else "INCOMPLETE_DIAGNOSTIC",
           json_path, html_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
