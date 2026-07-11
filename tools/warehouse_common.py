#!/usr/bin/env python3
"""Shared warehouse config + path helpers (stdlib only).

Reads config/warehouse.yaml (flat `key: value` lines). Every path is resolved
relative to the repo root so tools work from any cwd. Partition-path values are
sanitized identically by the exporter and the loader (`load()`), so archive
files can be targeted by path alone even for categories with spaces
("Climate and Weather" -> "Climate_and_Weather"); the real value is always
preserved as a column inside each file.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "warehouse.yaml")

DEFAULTS = {
    "raw_root": "work/raw",
    "staging_db": "work/warehouse/staging.duckdb",
    "warehouse_root": "work/warehouse",
    "archive_root": "work/warehouse/facts",
    "raw_retention_days": "2",
    "staging_retain_days": "2",
    "heartbeat_active_hours": "24",
    "ingest_loop_seconds": "60",
}

_INT_KEYS = {"raw_retention_days", "staging_retain_days",
             "heartbeat_active_hours", "ingest_loop_seconds"}
_PATH_KEYS = {"raw_root", "staging_db", "warehouse_root", "archive_root"}


def load_config(path=CONFIG_PATH):
    """Flat YAML subset: `key: value`, `#` comments. Env var of the same name
    (upper-cased, e.g. ARCHIVE_ROOT) overrides the file."""
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            s = line.split("#", 1)[0].strip()
            if not s or ":" not in s:
                continue
            k, v = s.split(":", 1)
            cfg[k.strip()] = v.strip()
    for k in list(cfg):
        env = os.environ.get(k.upper())
        if env:
            cfg[k] = env
    out = {}
    for k, v in cfg.items():
        if k in _INT_KEYS:
            out[k] = int(v)
        elif k in _PATH_KEYS:
            out[k] = v if os.path.isabs(v) else os.path.join(ROOT, v)
        else:
            out[k] = v
    return out


def sanitize(value):
    """Partition-path form of a category/subcategory value. None -> _unclassified."""
    if value is None or value == "":
        return "_unclassified"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_") or "_unclassified"


def partition_dir(archive_root, table, category, subcategory, date):
    return os.path.join(archive_root, table,
                        "category=%s" % sanitize(category),
                        "subcategory=%s" % sanitize(subcategory),
                        "date=%s" % date)


def partition_file(table, category, subcategory, date, ext):
    return "%s__%s__%s__%s.%s" % (table, sanitize(category), sanitize(subcategory), date, ext)


def raw_day_dir(raw_root, date):
    return os.path.join(raw_root, "date=%s" % date)


def hour_us(ts_us):
    return ts_us // 3_600_000_000


def day_of_us(ts_us):
    import datetime
    return datetime.datetime.fromtimestamp(
        ts_us / 1_000_000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def day_start_us(date_str):
    import datetime
    d = datetime.date.fromisoformat(date_str)
    return int(datetime.datetime(d.year, d.month, d.day,
                   tzinfo=datetime.timezone.utc).timestamp() * 1_000_000)


def seal_path(warehouse_root, date):
    """Authoritative completed-day attestation written only after full proof."""
    return os.path.join(warehouse_root, "seals", "date=%s.json" % date)


def manifest_date_sha256(manifest_path, date):
    """Stable digest of every manifest field for one UTC date."""
    import csv
    import hashlib
    import json
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError("manifest missing: %s" % manifest_path)
    with open(manifest_path, newline="") as f:
        rows = [dict(r) for r in csv.DictReader(f) if r.get("date") == date]
    rows.sort(key=lambda r: (r.get("table", ""), r.get("category", ""),
                             r.get("subcategory", ""), r.get("file_path", "")))
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), rows


def late_raw_dependency_files(warehouse_root, raw_root, exchange_date):
    """Raw files that later invalidated/rebuilt an already sealed exchange day.

    The append-only invalidation ledger makes receipt-time files outside the
    normal D+1 00/01 watermark part of every future seal for D.  Dependencies
    are stored relative to raw_root so a restored warehouse remains portable.
    """
    import json
    ledger = os.path.join(warehouse_root, "seal_invalidations.ndjson")
    if not os.path.isfile(ledger):
        return []
    out = set()
    with open(ledger, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            try:
                row = json.loads(line)
            except ValueError as e:
                raise RuntimeError("invalid late-source ledger line %d: %s" %
                                   (lineno, e))
            if (row.get("event") not in
                    ("SEALED_DAY_INVALIDATED_BY_LATE_FACT",
                     "LATE_RAW_DEPENDENCY_OBSERVED") or
                    row.get("exchange_date") != exchange_date):
                continue
            rel = row.get("source_raw_rel")
            if not rel:
                raise RuntimeError(
                    "sealed day has non-reproducible late fact dependency: %s"
                    % row.get("source_file"))
            path = os.path.abspath(os.path.join(raw_root, rel))
            root = os.path.abspath(raw_root)
            if os.path.commonpath([root, path]) != root:
                raise RuntimeError("late raw dependency escapes raw_root: %s" % rel)
            if not os.path.isfile(path):
                raise RuntimeError("late raw dependency missing: %s" % path)
            out.add(path)
    return sorted(out)


def seal_raw_files(raw_root, exchange_date, cross_day_hours=2,
                   warehouse_root=None):
    """Closed receipt-time inputs needed to seal one exchange-timestamp day.

    Facts partition on exchange ``ts_utc`` while raw partitions on receipt day.
    Include all exchange-date raw plus the next receipt day's first N closed
    hours so a just-after-midnight receipt carrying a just-before-midnight
    exchange timestamp cannot be omitted.
    """
    import datetime
    import glob
    d = datetime.date.fromisoformat(exchange_date)
    next_date = (d + datetime.timedelta(days=1)).isoformat()
    files = set(glob.glob(os.path.join(raw_day_dir(raw_root, exchange_date),
                                      "*.ndjson*")))
    hour_re = re.compile(r"_(\d{2})\.ndjson(?:\.\d+)?$")
    seen_firehose = set()
    for path in glob.glob(os.path.join(raw_day_dir(raw_root, next_date), "*.ndjson*")):
        m = hour_re.search(os.path.basename(path))
        if m and int(m.group(1)) < cross_day_hours:
            files.add(path)
            if os.path.basename(path).startswith("firehose_"):
                seen_firehose.add(int(m.group(1)))
    missing = sorted(set(range(cross_day_hours)) - seen_firehose)
    if missing:
        raise RuntimeError("missing closed next-day firehose hour(s) for %s: %s"
                           % (exchange_date, missing))
    if warehouse_root is not None:
        files.update(late_raw_dependency_files(
            warehouse_root, raw_root, exchange_date))
    return sorted(files)
