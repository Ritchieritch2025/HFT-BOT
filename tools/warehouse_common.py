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
