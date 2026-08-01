#!/usr/bin/env python3
"""ALPHA-SPRINT-01 H6b politics executable-price-path study runner.

Implements the sealed execution file
``ALPHA-SPRINT-01 H6b Research Execution File (2026-07-24)`` which
supersedes the H6 calibration/extremization preregistration.

Modules:

* ``G0``     — estimability precheck (hard gate; runs on TRAIN only).
* ``H6B``    — politics executable-price-path study (B0/B1/B2).
* ``H6A_MV`` — calibration machinery validation.  Gated on settlement
  outcome availability; absent settlement data yields a first-class
  ``NOT_ESTIMABLE`` result, never a silent skip.
* ``SCAN``   — structural constraint census (complement, ladder,
  bracket, stale-side).  Not gated by G0.

Fail-closed behaviours required by section 14 of the execution file are
implemented as HALT receipts: the runner writes
``H6B_HALT_RECEIPT.json`` plus a markdown stub and exits with status 3
rather than degrading silently.

Read-only research.  No live orders.  No cash-PnL claim.  No midpoint
fills.  All EV figures are modeled, fee-inclusive, and carry the
1-tick adverse fill haircut on every entry and exit.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by the W09 wrapper
    raise SystemExit(
        "duckdb is required; on W09 use /opt/w09/venv/bin/python"
    ) from exc


SCHEMA_VERSION = "alpha-sprint-h6b-politics-path-study-v1"
EXECUTION_FILE = "ALPHA-SPRINT-01 H6b Research Execution File (2026-07-24)"

# ---------------------------------------------------------------------------
# Frozen design constants (section references are to the execution file).
# These are an evaluation grid, not a parameter space (§8.1).
# ---------------------------------------------------------------------------
# §3 of the execution file froze a 14-day window (7 TRAIN + 7 VALIDATE).
# Principal decision 2026-07-24: overridden — the runner now uses the
# entire most recent contiguous block ending at the latest available
# date, split TRAIN = first ceil(N/2) days, VALIDATE = remainder.  The
# deviation is recorded in the TRAIN seal and the receipt
# (spec_deviations); results from windows shorter than 14 days are not
# comparable to the sealed design and must be labeled as revised.
MIN_WINDOW_DAYS = 2                   # 1 TRAIN + 1 VALIDATE at minimum
SPEC_DEVIATIONS = (
    "window: sealed 14-day requirement replaced by "
    "use-all-contiguous-data-to-latest-date (principal decision 2026-07-24)",
)

# Audit-sync constraints (2026-07-25), emitted verbatim in every receipt.
CROSS_DECLARATIONS = (
    "H6/H6b multiple-comparison budgets are separately closed; no test "
    "here counts against H6's BH denominator and vice versa; neither "
    "program reads the other's VALIDATE split",
    "VPIN is an MM-T7 shared primitive: EXPLORATORY only in this study; "
    "entry into any main analysis requires its own preregistration; "
    "known-answer validation of VPIN uses synthetic data only",
    "stratification principle 2026-07-25: politics main conclusions "
    "reported per event-category stratum; mixed-pool-only significance "
    "is labeled ARTIFACT and cannot promote",
)
Q_BUCKET_EDGES = list(range(50, 95, 5))          # §7.1 exact 5-cent bands
PRIMARY_BANDS = (("55_75", 55, 75), ("75_90", 75, 90))
HORIZONS_US: Tuple[Tuple[str, int], ...] = (      # §7.2
    ("30m", 30 * 60 * 1_000_000),
    ("1h", 60 * 60 * 1_000_000),
    ("3h", 3 * 60 * 60 * 1_000_000),
    ("24h", 24 * 60 * 60 * 1_000_000),
    ("close", -1),
)
BRACKET_FAMILIES: Tuple[Tuple[str, int, int], ...] = (   # §8.1
    ("b5", 5, 5),
    ("b10", 10, 10),
)
PRIMARY_HORIZON = "1h"
PRIMARY_BRACKET = "b10"
PRIMARY_VARIANT = "B1"
DEFAULT_CLIP = 25                     # §8.2 frozen on TRAIN
CAPACITY_CLIPS = (1, 10, 25, 50, 100)
ADVERSE_FILL_TICKS = 1                # §8.3 per entry and per exit
LARGE_TRADE_LOOKBACK_US = 15 * 60 * 1_000_000    # §7.3 default L
LARGE_TRADE_PERCENTILE = 0.90
K_CLUSTER = 1                          # §6 primary endpoint
PLAUSIBLE_EDGE_CENTS = {"H6B": 3.0, "H6A_MV": 3.0, "SCAN": 2.0}   # §G0.4
POWER_MULTIPLIER = 2.8                 # §G0.3 (80% power, alpha=.05 two-sided)
C2_PERMUTATIONS = 1000                 # §5.2
C1_SHUFFLES = 100                      # §5.2 (count frozen here, pre-data)
BOOTSTRAP_REPLICATES = 10_000          # §10.2
BH_Q = 0.10                            # §10.3
PROMOTION_MIN_CLUSTERS = 100           # §11.3
STALE_MATERIAL_MOVE_TICKS = 2          # §9.3 stale-side material move
SCAN_SNAPSHOT_INTERVAL_US = 60 * 1_000_000  # SCAN census sampling cadence

# Frozen conservative fee fallback (§2 fee handling).  Source:
# tools/research/pnl_spine/fee_facts.py OFFICIAL July 7, 2026 quadratic
# rates.  Used only when no live schedule JSON is supplied; every
# downstream number is then flagged FEE_MODELED.
FALLBACK_FEE_SCHEDULE = {
    "source": "pnl_spine.fee_facts OFFICIAL 2026-07-07 quadratic",
    "version": "official-2026-07-07-quadratic-frozen-fallback",
    "taker_rate": 0.07,
    "maker_rate": 0.0175,
    "rounding": "ceil_to_cent_per_order",
}

# W09 checkpoint layout contract (same family as the H4 runner).
EXPECTED_L2_MARKET_BUCKETS = 16
EXPECTED_TRADE_MARKET_BUCKETS = 32
MEMORY_LIMIT = "16GB"
THREADS = 2
PARTITION_RE = re.compile(
    r"^date=(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})_bucket=(?P<bucket>[0-9]{2,3})$"
)

L2_REQUIRED_COLUMNS = {
    "date", "t_us", "market_ticker", "book_valid",
    "bid_e4", "bid_qty_e4", "ask_e4", "ask_qty_e4",
}
TRADE_REQUIRED_COLUMNS = {"date", "t_us", "market_ticker", "count_e4"}

RECEIPT_NAME = "H6B_RECEIPT.json"
REPORT_NAME = "H6B_REPORT.md"
SEAL_NAME = "H6B_TRAIN_SEAL.json"
HALT_RECEIPT_NAME = "H6B_HALT_RECEIPT.json"
OPPORTUNITY_TABLE_NAME = "H6B_OPPORTUNITIES.jsonl"

POLITICS_CATEGORY_ROOTS = ("politics", "politicians", "elections")


class H6bStudyError(RuntimeError):
    """Fail-closed input, split, or output contract error."""


class H6bHalt(RuntimeError):
    """Section 14 stop condition: halt and report, never degrade."""

    def __init__(self, reason_code: str, detail: Mapping[str, Any]):
        super().__init__(f"{reason_code}: {json.dumps(dict(detail), sort_keys=True)}")
        self.reason_code = reason_code
        self.detail = dict(detail)


# ---------------------------------------------------------------------------
# Canonical hashing / IO helpers (H4 conventions).
# ---------------------------------------------------------------------------

def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _module_sha256() -> str:
    return _sha256_path(Path(__file__).resolve())


def _parse_partition_key(key: str) -> Tuple[str, int]:
    match = PARTITION_RE.match(key)
    if not match:
        raise H6bStudyError(f"invalid partition key: {key}")
    return match.group("date"), int(match.group("bucket"))


def _log(message: str) -> None:
    print(f"[h6b] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Fee model (§8.3).  Taker both sides; quadratic in execution price.
# ---------------------------------------------------------------------------

def _load_fee_schedule(fee_schedule_path: Optional[Path]) -> Dict[str, Any]:
    if fee_schedule_path is None:
        schedule = dict(FALLBACK_FEE_SCHEDULE)
        schedule["fee_modeled"] = True
        return schedule
    if not fee_schedule_path.is_file():
        raise H6bHalt(
            "FEE_SCHEDULE_UNAVAILABLE",
            {"path": str(fee_schedule_path)},
        )
    raw = fee_schedule_path.read_bytes()
    schedule = json.loads(raw)
    for field in ("version", "taker_rate", "source"):
        if field not in schedule:
            raise H6bHalt(
                "FEE_SCHEDULE_UNAVAILABLE",
                {"path": str(fee_schedule_path), "missing_field": field},
            )
    schedule = dict(schedule)
    schedule["fee_modeled"] = False
    schedule["schedule_sha256"] = _sha256_bytes(raw)
    return schedule


def taker_fee_cents_per_contract(
    price_cents: float, clip: int, taker_rate: float
) -> float:
    """Per-contract fee in cents for a clip-sized taker order.

    fee_per_order_dollars = taker_rate * clip * p * (1-p), rounded up to
    the next cent per order, then amortized per contract.
    """
    p = price_cents / 100.0
    p = min(max(p, 0.0), 1.0)
    fee_order_dollars = taker_rate * clip * p * (1.0 - p)
    fee_order_cents = math.ceil(fee_order_dollars * 100.0 - 1e-9)
    return fee_order_cents / clip


# ---------------------------------------------------------------------------
# Universe / cluster map (§G0.1, §5.1).
# ---------------------------------------------------------------------------

def _load_universe(universe_path: Path) -> Dict[str, Any]:
    if not universe_path.is_file():
        raise H6bHalt(
            "CLUSTER_ASSIGNMENT_UNCONSTRUCTIBLE",
            {"reason": "universe json absent", "path": str(universe_path)},
        )
    raw = universe_path.read_bytes()
    doc = json.loads(raw)
    markets = doc.get("markets")
    if not isinstance(markets, Mapping) or not markets:
        raise H6bHalt(
            "CLUSTER_ASSIGNMENT_UNCONSTRUCTIBLE",
            {"reason": "universe json has no markets", "path": str(universe_path)},
        )
    politics: Dict[str, Dict[str, Any]] = {}
    for ticker, meta in markets.items():
        if not isinstance(meta, Mapping):
            raise H6bStudyError(f"universe market entry is not an object: {ticker}")
        category = str(meta.get("category", ""))
        cluster = meta.get("event_cluster")
        root = category.split("__", 1)[0].lower()
        if root in POLITICS_CATEGORY_ROOTS:
            if not isinstance(cluster, str) or not cluster:
                raise H6bHalt(
                    "CLUSTER_ASSIGNMENT_UNCONSTRUCTIBLE",
                    {"reason": "politics market without event_cluster",
                     "market_ticker": str(ticker)},
                )
            entry: Dict[str, Any] = {
                "category": category,
                "event_cluster": cluster,
            }
            ladder = meta.get("ladder")
            if isinstance(ladder, Mapping):
                entry["ladder_family"] = str(ladder.get("family", ""))
                entry["ladder_strike"] = ladder.get("strike")
                entry["ladder_exhaustive"] = bool(ladder.get("exhaustive", False))
            complement_of = meta.get("complement_of")
            if isinstance(complement_of, str) and complement_of:
                entry["complement_of"] = complement_of
            if meta.get("settled_in_window"):
                entry["settled_in_window"] = True
            politics[str(ticker)] = entry
    if not politics:
        raise H6bHalt(
            "CLUSTER_ASSIGNMENT_UNCONSTRUCTIBLE",
            {"reason": "universe json contains zero politics markets"},
        )
    return {
        "path": str(universe_path),
        "sha256": _sha256_bytes(raw),
        "politics_markets": politics,
        "politics_market_count": len(politics),
        "total_market_count": len(markets),
        "category_rule": (
            "category root (before '__', lowercased) in "
            + repr(list(POLITICS_CATEGORY_ROOTS))
        ),
    }


def _load_settlements(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Optional settlement outcomes: {ticker: {result: yes|no, ts_us: int}}."""
    if path is None:
        return None
    if not path.is_file():
        raise H6bStudyError(f"settlement json absent: {path}")
    raw = path.read_bytes()
    doc = json.loads(raw)
    outcomes = doc.get("outcomes")
    if not isinstance(outcomes, Mapping):
        raise H6bStudyError("settlement json missing 'outcomes' mapping")
    return {
        "path": str(path),
        "sha256": _sha256_bytes(raw),
        "outcomes": {str(k): dict(v) for k, v in outcomes.items()},
    }


# ---------------------------------------------------------------------------
# Checkpoint loading (adapted from the H4 runner; same integrity checks).
# ---------------------------------------------------------------------------

def _schema_names(receipt: Mapping[str, Any]) -> set:
    schema = receipt.get("schema")
    names = set()
    if isinstance(schema, list):
        for item in schema:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                names.add(item["name"])
            elif isinstance(item, str):
                names.add(item)
    return names


def _load_stage(
    checkpoint_namespace: Path,
    stage: str,
    required_columns: set,
    verify_payload_hashes: bool,
) -> Dict[str, Any]:
    manifest_path = checkpoint_namespace / stage / "MANIFEST.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or not checkpoint_namespace.is_dir()
    ):
        raise H6bStudyError(
            f"checkpoint stage manifest is unavailable: {manifest_path}"
        )
    raw_manifest = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw_manifest)
    except ValueError as exc:
        raise H6bStudyError(
            f"checkpoint manifest is not JSON: {manifest_path}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("state") != "COMPLETE"
        or manifest.get("stage") != stage
        or not isinstance(manifest.get("source_binding"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["source_binding"])
    ):
        raise H6bStudyError(f"checkpoint manifest binding/state mismatch: {stage}")
    partitions_raw = manifest.get("partitions")
    if not isinstance(partitions_raw, list) or not partitions_raw:
        raise H6bStudyError(f"checkpoint stage has no partitions: {stage}")

    partitions: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for item in partitions_raw:
        if not isinstance(item, Mapping):
            raise H6bStudyError(f"invalid checkpoint manifest partition: {stage}")
        key = item.get("partition_key")
        receipt_relative = item.get("receipt_path")
        if not isinstance(key, str) or not isinstance(receipt_relative, str):
            raise H6bStudyError(f"checkpoint partition identity is absent: {stage}")
        date, bucket = _parse_partition_key(key)
        receipt_path = checkpoint_namespace / receipt_relative
        expected_receipt_path = (
            checkpoint_namespace / stage / "receipts" / f"{key}.json"
        )
        if (
            receipt_path.resolve() != expected_receipt_path.resolve()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise H6bStudyError(f"checkpoint receipt path mismatch: {stage}:{key}")
        receipt_raw = receipt_path.read_bytes()
        if item.get("receipt_sha256") != _sha256_bytes(receipt_raw):
            raise H6bStudyError(f"checkpoint receipt hash mismatch: {stage}:{key}")
        try:
            receipt = json.loads(receipt_raw)
        except ValueError as exc:
            raise H6bStudyError(
                f"checkpoint receipt is not JSON: {stage}:{key}"
            ) from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("state") != "COMPLETE"
            or receipt.get("stage") != stage
            or receipt.get("partition_key") != key
            or receipt.get("source_binding") != manifest["source_binding"]
        ):
            raise H6bStudyError(f"checkpoint receipt binding mismatch: {stage}:{key}")
        data = receipt.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("path"), str):
            raise H6bStudyError(f"checkpoint data receipt is absent: {stage}:{key}")
        data_path = checkpoint_namespace / data["path"]
        expected_data_path = (
            checkpoint_namespace / stage / "data" / f"{key}.parquet"
        )
        if (
            data_path.resolve() != expected_data_path.resolve()
            or data_path.is_symlink()
            or not data_path.is_file()
        ):
            raise H6bStudyError(f"checkpoint payload path mismatch: {stage}:{key}")
        if data_path.stat().st_size != data.get("size_bytes"):
            raise H6bStudyError(f"checkpoint payload size mismatch: {stage}:{key}")
        if item.get("data_sha256") != data.get("sha256"):
            raise H6bStudyError(f"checkpoint payload receipt drift: {stage}:{key}")
        if verify_payload_hashes and _sha256_path(data_path) != data.get("sha256"):
            raise H6bStudyError(f"checkpoint payload hash mismatch: {stage}:{key}")
        missing = required_columns - _schema_names(receipt)
        if missing:
            raise H6bStudyError(
                f"checkpoint schema missing {sorted(missing)}: {stage}:{key}"
            )
        identity = (date, bucket)
        if identity in partitions:
            raise H6bStudyError(f"duplicate checkpoint partition: {stage}:{key}")
        partitions[identity] = {
            "key": key,
            "date": date,
            "bucket": bucket,
            "path": data_path,
            "data_sha256": data["sha256"],
            "row_count": int(data.get("row_count") or 0),
            "schema_names": sorted(_schema_names(receipt)),
        }
    if len(partitions) != manifest.get("partition_count"):
        raise H6bStudyError(f"checkpoint manifest partition count mismatch: {stage}")
    return {
        "stage": stage,
        "stage_version": manifest.get("stage_version"),
        "source_binding": manifest["source_binding"],
        "manifest_sha256": _sha256_bytes(raw_manifest),
        "partitions": partitions,
    }


def _stage_dates(stage: Mapping[str, Any]) -> List[str]:
    return sorted({date for (date, _bucket) in stage["partitions"]})


def _selected_partitions(stage: Mapping[str, Any], date: str) -> List[Dict[str, Any]]:
    rows = [
        value
        for (partition_date, _bucket), value in stage["partitions"].items()
        if partition_date == date
    ]
    return sorted(rows, key=lambda row: row["bucket"])


# ---------------------------------------------------------------------------
# Window discovery (§3, §14 stop condition #1).
# ---------------------------------------------------------------------------

def _contiguous_runs(dates: Sequence[str]) -> List[List[str]]:
    runs: List[List[str]] = []
    for date in sorted(dates):
        day = dt.date.fromisoformat(date)
        if runs and dt.date.fromisoformat(runs[-1][-1]) + dt.timedelta(days=1) == day:
            runs[-1].append(date)
        else:
            runs.append([date])
    return runs


def _discover_window(
    l2_stage: Mapping[str, Any], trade_stage: Mapping[str, Any]
) -> Dict[str, Any]:
    """Use the entire contiguous block ending at the latest joint date.

    TRAIN = first ceil(N/2) days, VALIDATE = remainder.  Halts only if
    fewer than MIN_WINDOW_DAYS contiguous days exist (a TRAIN/VALIDATE
    split is then impossible, not merely underpowered — underpowering
    is G0's job to report, not this function's).
    """
    l2_dates = set(_stage_dates(l2_stage))
    trade_dates = set(_stage_dates(trade_stage))
    both = sorted(l2_dates & trade_dates)
    runs = _contiguous_runs(both)
    coverage = {
        "l2_dates": sorted(l2_dates),
        "trade_dates": sorted(trade_dates),
        "joint_dates": both,
        "contiguous_runs": [[run[0], run[-1], len(run)] for run in runs],
    }
    if not runs or len(runs[-1]) < MIN_WINDOW_DAYS:
        longest = max((len(run) for run in runs), default=0)
        raise H6bHalt(
            "WINDOW_TOO_SHORT",
            {
                "required_contiguous_days": MIN_WINDOW_DAYS,
                "longest_contiguous_days": longest,
                "latest_block_days": len(runs[-1]) if runs else 0,
                "coverage": coverage,
            },
        )
    window = runs[-1]                     # block ending at the latest date
    train_days = math.ceil(len(window) / 2)
    return {
        "train_dates": window[:train_days],
        "validate_dates": window[train_days:],
        "window_days": len(window),
        "meets_sealed_14_day_design": len(window) >= 14,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------------
# Quote granularity probe (§2).
# ---------------------------------------------------------------------------

def _probe_granularity(
    con: Any, l2_rows: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Distinguish event-level from snapshot capture on one partition."""
    probe = l2_rows[0]
    sql = f"""
        WITH g AS (
            SELECT market_ticker, t_us - LAG(t_us) OVER (
                PARTITION BY market_ticker ORDER BY t_us
            ) AS gap
            FROM read_parquet({_quote(probe['path'])})
        )
        SELECT
            COUNT(*) AS n,
            approx_quantile(gap, 0.5) AS gap_p50_us,
            approx_quantile(gap, 0.05) AS gap_p05_us
        FROM g WHERE gap IS NOT NULL AND gap > 0
    """
    row = con.execute(sql).fetchone()
    n, p50, p05 = row
    if not n:
        return {"quote_granularity": "unknown", "snapshot_interval_s": None}
    # Snapshot capture shows a hard floor at a fixed interval; event
    # capture shows sub-second p05 gaps.
    if p05 is not None and p05 >= 1_000_000:
        return {
            "quote_granularity": "snapshot",
            "snapshot_interval_s": round(p50 / 1_000_000.0, 3) if p50 else None,
            "path_bounded": True,
        }
    return {
        "quote_granularity": "event",
        "snapshot_interval_s": None,
        "path_bounded": False,
    }


# ---------------------------------------------------------------------------
# Politics stream extraction.
# ---------------------------------------------------------------------------

def _register_universe_table(con: Any, politics: Mapping[str, Mapping[str, Any]]) -> None:
    con.execute("DROP TABLE IF EXISTS politics_universe")
    con.execute(
        "CREATE TABLE politics_universe "
        "(market_ticker VARCHAR PRIMARY KEY, event_cluster VARCHAR)"
    )
    con.executemany(
        "INSERT INTO politics_universe VALUES (?, ?)",
        [(ticker, meta["event_cluster"]) for ticker, meta in politics.items()],
    )


def _extract_quotes_for_date(
    con: Any,
    l2_rows: Sequence[Mapping[str, Any]],
    date: str,
) -> Dict[str, Dict[str, list]]:
    """Return per-market ordered quote arrays for politics markets."""
    paths = "[" + ",".join(_quote(row["path"]) for row in l2_rows) + "]"
    sql = f"""
        SELECT l.market_ticker, u.event_cluster, l.t_us,
               l.bid_e4, l.ask_e4, l.bid_qty_e4, l.ask_qty_e4
        FROM read_parquet({paths}) l
        JOIN politics_universe u USING (market_ticker)
        WHERE l.date = {_quote(date)}
          AND l.book_valid
          AND l.bid_e4 IS NOT NULL AND l.ask_e4 IS NOT NULL
          AND l.bid_e4 > 0 AND l.ask_e4 < 10000
          AND l.ask_e4 > l.bid_e4
        ORDER BY l.market_ticker, l.t_us
    """
    streams: Dict[str, Dict[str, list]] = {}
    cursor = con.execute(sql)
    while True:
        batch = cursor.fetchmany(200_000)
        if not batch:
            break
        for ticker, cluster, t_us, bid_e4, ask_e4, bid_qty_e4, ask_qty_e4 in batch:
            entry = streams.get(ticker)
            if entry is None:
                entry = {
                    "cluster": cluster,
                    "t_us": [],
                    "yes_bid_c": [],
                    "yes_ask_c": [],
                    "bid_qty": [],
                    "ask_qty": [],
                }
                streams[ticker] = entry
            entry["t_us"].append(int(t_us))
            entry["yes_bid_c"].append(bid_e4 / 100.0)
            entry["yes_ask_c"].append(ask_e4 / 100.0)
            entry["bid_qty"].append((bid_qty_e4 or 0) / 10_000.0)
            entry["ask_qty"].append((ask_qty_e4 or 0) / 10_000.0)
    return streams


def _extract_trades_for_date(
    con: Any,
    trade_rows: Sequence[Mapping[str, Any]],
    date: str,
) -> Dict[str, Dict[str, list]]:
    paths = "[" + ",".join(_quote(row["path"]) for row in trade_rows) + "]"
    sql = f"""
        SELECT t.market_ticker, t.t_us, t.count_e4
        FROM read_parquet({paths}) t
        JOIN politics_universe u USING (market_ticker)
        WHERE t.date = {_quote(date)}
        ORDER BY t.market_ticker, t.t_us
    """
    trades: Dict[str, Dict[str, list]] = {}
    for ticker, t_us, count_e4 in con.execute(sql).fetchall():
        entry = trades.setdefault(ticker, {"t_us": [], "size": []})
        entry["t_us"].append(int(t_us))
        entry["size"].append((count_e4 or 0) / 10_000.0)
    return trades


# ---------------------------------------------------------------------------
# Feature: large_trade_flag (§7.3, strictly [t-L, t)).
# ---------------------------------------------------------------------------

def _round_up_clean(value: float) -> int:
    """Round a size up to a clean exchange boundary."""
    boundaries = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000,
                  2000, 2500, 5000, 10000]
    for boundary in boundaries:
        if value <= boundary:
            return boundary
    return int(math.ceil(value / 10000.0)) * 10000


def _fit_s_large(train_trades_by_date: Mapping[str, Mapping[str, Mapping[str, list]]]) -> Dict[str, Any]:
    sizes: List[float] = []
    for date_map in train_trades_by_date.values():
        for entry in date_map.values():
            sizes.extend(entry["size"])
    if not sizes:
        raise H6bHalt(
            "LOOKAHEAD_ASSERTION_UNVERIFIABLE",
            {"reason": "no politics trades on TRAIN; S_large cannot be fit"},
        )
    sizes.sort()
    index = min(len(sizes) - 1, int(math.ceil(LARGE_TRADE_PERCENTILE * len(sizes))) - 1)
    raw_p90 = sizes[max(index, 0)]
    s_large = _round_up_clean(raw_p90)
    return {
        "train_trade_count": len(sizes),
        "raw_p90_contracts": raw_p90,
        "s_large_contracts": s_large,
        "lookback_us": LARGE_TRADE_LOOKBACK_US,
    }


def _large_flag(
    trades: Optional[Mapping[str, list]],
    t_us: int,
    s_large: float,
    lookback_us: int,
) -> Tuple[int, int]:
    """Return (flag, max_trade_t_considered).

    Window is [t-L, t): trades at exactly t are excluded (§7.3).
    """
    if not trades:
        return 0, -1
    times = trades["t_us"]
    sizes = trades["size"]
    hi = bisect.bisect_left(times, t_us)          # strictly before t
    lo = bisect.bisect_left(times, t_us - lookback_us)
    max_t = times[hi - 1] if hi > lo else -1
    for i in range(lo, hi):
        if sizes[i] >= s_large:
            return 1, max_t
    return 0, max_t


# ---------------------------------------------------------------------------
# Path engine (§6, §8): per-cluster sequential, K_cluster = 1.
# ---------------------------------------------------------------------------

def _q_bucket(q: float) -> Optional[str]:
    if q < 50 or q >= 90:
        return "exploratory"
    for low in range(50, 90, 5):
        if low <= q < low + 5:
            return f"{low}_{low + 5}"
    return None


def _build_cluster_streams(
    quotes: Mapping[str, Mapping[str, list]],
) -> Dict[str, List[Tuple[int, str, int]]]:
    """cluster -> time-ordered [(t_us, ticker, row_index)]."""
    clusters: Dict[str, List[Tuple[int, str, int]]] = {}
    for ticker, stream in quotes.items():
        cluster = stream["cluster"]
        rows = clusters.setdefault(cluster, [])
        for index, t in enumerate(stream["t_us"]):
            rows.append((t, ticker, index))
    for rows in clusters.values():
        rows.sort()
    return clusters


def run_cluster_pass(
    quotes: Mapping[str, Mapping[str, list]],
    cluster_rows: Sequence[Tuple[int, str, int]],
    trades: Mapping[str, Mapping[str, list]],
    *,
    variant: str,
    horizon_us: int,
    bracket_up: int,
    bracket_down: int,
    clip: int,
    s_large: float,
    taker_rate: float,
    flag_override: Optional[Mapping[Tuple[str, int], int]] = None,
    entry_time_override: Optional[Sequence[Tuple[int, str, int]]] = None,
    settlements_truth: Optional[Mapping[str, Tuple[str, int]]] = None,
) -> Dict[str, Any]:
    """Run one sequential arm over a single event cluster.

    Returns opportunity rows plus tallies.  ``variant`` semantics:
      B0 — enter favorite at every eligible state;
      B1 — enter favorite only when large_trade_flag == 1;
      B2 — enter anti-favorite only when large_trade_flag == 1.
    """
    opportunities: List[Dict[str, Any]] = []
    depth_rejects = 0
    lookahead_violation = False
    open_until = -1                       # K_cluster = 1 (§6)

    entry_iter = entry_time_override if entry_time_override is not None else cluster_rows
    for t_us, ticker, row_index in entry_iter:
        if t_us < open_until:
            continue
        stream = quotes[ticker]
        yes_bid = stream["yes_bid_c"][row_index]
        yes_ask = stream["yes_ask_c"][row_index]
        no_ask = 100.0 - yes_bid
        no_bid = 100.0 - yes_ask
        if yes_ask >= no_ask:
            fav_side, fav_ask, fav_bid = "yes", yes_ask, yes_bid
            fav_ask_depth = stream["ask_qty"][row_index]
            anti_side, anti_ask = "no", no_ask
            anti_ask_depth = stream["bid_qty"][row_index]
        else:
            fav_side, fav_ask, fav_bid = "no", no_ask, no_bid
            fav_ask_depth = stream["bid_qty"][row_index]
            anti_side, anti_ask = "yes", yes_ask
            anti_ask_depth = stream["ask_qty"][row_index]
        q = fav_ask
        bucket = _q_bucket(q)
        if bucket is None:
            continue

        if variant in ("B1", "B2"):
            if flag_override is not None:
                flag = flag_override.get((ticker, row_index), 0)
                max_trade_t = -1
            else:
                flag, max_trade_t = _large_flag(
                    trades.get(ticker), t_us, s_large, LARGE_TRADE_LOOKBACK_US
                )
                if max_trade_t >= t_us:
                    lookahead_violation = True
            if flag != 1:
                continue
        else:
            flag, max_trade_t = _large_flag(
                trades.get(ticker), t_us, s_large, LARGE_TRADE_LOOKBACK_US
            )
            if max_trade_t >= t_us:
                lookahead_violation = True

        if variant == "B2":
            side, entry_ask = anti_side, anti_ask
            entry_depth = anti_ask_depth
        else:
            side, entry_ask = fav_side, fav_ask
            entry_depth = fav_ask_depth

        if entry_depth < clip:
            depth_rejects += 1
            continue

        entry_price = entry_ask + ADVERSE_FILL_TICKS
        entry_fee = taker_fee_cents_per_contract(entry_price, clip, taker_rate)

        # Walk forward on this market's own quote stream.
        times = stream["t_us"]
        deadline = t_us + horizon_us if horizon_us > 0 else None
        exit_reason = "UNRESOLVED_CLOSE"
        exit_bid = None
        exit_t = times[-1]
        idx = row_index + 1
        n_rows = len(times)
        while idx < n_rows:
            t_now = times[idx]
            if stream["yes_ask_c"][idx] > stream["yes_bid_c"][idx]:
                if side == "yes":
                    bid_now = stream["yes_bid_c"][idx]
                else:
                    bid_now = 100.0 - stream["yes_ask_c"][idx]
                if deadline is not None and t_now >= deadline:
                    exit_reason, exit_bid, exit_t = "TIME", bid_now, t_now
                    break
                # Stop wins ties (§8.1).
                if bid_now <= entry_ask - bracket_down:
                    exit_reason, exit_bid, exit_t = "STOP", bid_now, t_now
                    break
                if bid_now >= entry_ask + bracket_up:
                    exit_reason, exit_bid, exit_t = "TARGET", bid_now, t_now
                    break
                exit_bid = bid_now
                exit_t = t_now
            idx += 1
        settled = (
            settlements_truth.get(ticker) if settlements_truth else None
        )
        if exit_reason == "UNRESOLVED_CLOSE" and settled is not None:
            result_side, settle_ts = settled
            if deadline is None or settle_ts <= deadline:
                # §7.2: resolved inside the window marks to settlement
                # value.  Redemption at settlement pays no trading fee
                # and takes no haircut.
                exit_reason = "SETTLEMENT"
                exit_bid = None
                exit_t = settle_ts
                settlement_value = 100.0 if result_side == side else 0.0
                net = (settlement_value - entry_price) - entry_fee
                opportunities.append(
                    {
                        "market_ticker": ticker,
                        "row_index": row_index,
                        "t_entry_us": t_us,
                        "t_exit_us": exit_t,
                        "q": q,
                        "q_bucket": bucket,
                        "side": side,
                        "side_class": (
                            "favorite" if variant != "B2" else "anti_favorite"
                        ),
                        "flag": flag,
                        "exit_reason": exit_reason,
                        "net_pnl_c": net,
                    }
                )
                open_until = exit_t
                continue
        if exit_reason == "UNRESOLVED_CLOSE" and exit_bid is None:
            # No subsequent two-sided state: insufficient data (§6).
            continue

        exit_price = exit_bid - ADVERSE_FILL_TICKS
        exit_fee = taker_fee_cents_per_contract(max(exit_price, 0.0), clip, taker_rate)
        net = (exit_price - entry_price) - entry_fee - exit_fee
        opportunities.append(
            {
                "market_ticker": ticker,
                "row_index": row_index,
                "t_entry_us": t_us,
                "t_exit_us": exit_t,
                "q": q,
                "q_bucket": bucket,
                "side": side,
                "side_class": "favorite" if variant != "B2" else "anti_favorite",
                "flag": flag,
                "exit_reason": exit_reason,
                "net_pnl_c": net,
            }
        )
        open_until = exit_t
    return {
        "opportunities": opportunities,
        "depth_rejects": depth_rejects,
        "lookahead_violation": lookahead_violation,
    }


# ---------------------------------------------------------------------------
# Statistics (§10).
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _cluster_means(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[float]]:
    by_cluster: Dict[str, List[float]] = {}
    for row in rows:
        by_cluster.setdefault(row["cluster"], []).append(row["net_pnl_c"])
    return by_cluster


def cluster_bootstrap_ci(
    rows: Sequence[Mapping[str, Any]],
    rng: random.Random,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> Dict[str, Any]:
    """Block bootstrap resampling whole event clusters (§10.2)."""
    by_cluster = _cluster_means(rows)
    clusters = list(by_cluster.values())
    n_clusters = len(clusters)
    if n_clusters < 2:
        return {"ci_low": None, "ci_high": None, "n_clusters": n_clusters}
    stats: List[float] = []
    for _ in range(replicates):
        total = 0.0
        count = 0
        for _ in range(n_clusters):
            values = clusters[rng.randrange(n_clusters)]
            total += sum(values)
            count += len(values)
        if count:
            stats.append(total / count)
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[min(len(stats) - 1, int(0.975 * len(stats)))]
    return {"ci_low": lo, "ci_high": hi, "n_clusters": n_clusters}


def permutation_percentile(observed: float, null_stats: Sequence[float]) -> Optional[float]:
    if not null_stats:
        return None
    below = sum(1 for value in null_stats if value <= observed)
    return below / len(null_stats)


def benjamini_hochberg(pvalues: Sequence[Tuple[str, float]], q: float) -> Dict[str, Any]:
    ranked = sorted(pvalues, key=lambda item: item[1])
    m = len(ranked)
    passing: set = set()
    max_k = 0
    for k, (_name, p) in enumerate(ranked, start=1):
        if p <= q * k / m:
            max_k = k
    for k, (name, _p) in enumerate(ranked, start=1):
        if k <= max_k:
            passing.add(name)
    return {
        "total_tests": m,
        "passing": sorted(passing),
        "q": q,
    }


# ---------------------------------------------------------------------------
# SCAN (§9.3): census, no p-values, no calibration model.
# ---------------------------------------------------------------------------

def _sampled_quote_indexes(times: Sequence[int]) -> List[int]:
    """Indexes of the last quote at each SCAN sampling boundary."""
    if not times:
        return []
    indexes: List[int] = []
    boundary = times[0]
    last_index = 0
    for i, t in enumerate(times):
        while t >= boundary + SCAN_SNAPSHOT_INTERVAL_US:
            indexes.append(last_index)
            boundary += SCAN_SNAPSHOT_INTERVAL_US
        last_index = i
    indexes.append(last_index)
    return indexes


def run_scan(
    quotes_by_date: Mapping[str, Mapping[str, Mapping[str, list]]],
    universe: Mapping[str, Any],
    clip: int,
    taker_rate: float,
    t_stale_us: Optional[int],
) -> Dict[str, Any]:
    """Structural constraint census (§9.3).

    Within one Kalshi market YES and NO share a single book, so the
    binary-pair complement check is only meaningful across *distinct*
    markets declared complementary in the universe metadata
    (``complement_of``).  Ladder checks likewise require ``ladder``
    metadata.  Absent metadata is reported as NOT_CONSTRUCTIBLE, never
    silently skipped.
    """
    complement_violations: List[Dict[str, Any]] = []
    ladder_inversions: List[Dict[str, Any]] = []
    stale_events: List[Dict[str, Any]] = []
    markets_meta = universe["politics_markets"]
    complement_pairs = sorted(
        {
            tuple(sorted((ticker, str(meta["complement_of"]))))
            for ticker, meta in markets_meta.items()
            if meta.get("complement_of")
        }
    )
    ladder_families: Dict[str, List[Tuple[float, str]]] = {}
    for ticker, meta in markets_meta.items():
        family = meta.get("ladder_family")
        strike = meta.get("ladder_strike")
        if family and strike is not None:
            ladder_families.setdefault(str(family), []).append(
                (float(strike), ticker)
            )

    for date, quotes in sorted(quotes_by_date.items()):
        # --- Complement violations across declared pairs. -------------
        for ticker_a, ticker_b in complement_pairs:
            stream_a = quotes.get(ticker_a)
            stream_b = quotes.get(ticker_b)
            if not stream_a or not stream_b:
                continue
            idx_b = 0
            times_b = stream_b["t_us"]
            for i in _sampled_quote_indexes(stream_a["t_us"]):
                t = stream_a["t_us"][i]
                while idx_b + 1 < len(times_b) and times_b[idx_b + 1] <= t:
                    idx_b += 1
                if times_b[idx_b] > t:
                    continue
                # Lock: SELL YES on both legs.  mutually_exclusive
                # guarantees at most one leg pays 100, so collecting
                # bid_A + bid_B > 100 + costs is riskless.  The ask-side
                # lock (buy both < 100) additionally requires the pair
                # to be EXHAUSTIVE, which mutual exclusivity does not
                # imply — pricing both asks below 100 is legitimate when
                # neither outcome may occur.  Never use the ask-side
                # test on non-exhaustive pairs.
                proceeds_a = stream_a["yes_bid_c"][i] - ADVERSE_FILL_TICKS
                proceeds_b = (
                    stream_b["yes_bid_c"][idx_b] - ADVERSE_FILL_TICKS
                )
                fees = (
                    taker_fee_cents_per_contract(
                        max(proceeds_a, 0.0), clip, taker_rate
                    )
                    + taker_fee_cents_per_contract(
                        max(proceeds_b, 0.0), clip, taker_rate
                    )
                )
                capture = (proceeds_a + proceeds_b) - 100.0 - fees
                gross = (
                    stream_a["yes_bid_c"][i] + stream_b["yes_bid_c"][idx_b]
                ) - 100.0
                if gross > 0:
                    size = min(
                        stream_a["bid_qty"][i], stream_b["bid_qty"][idx_b]
                    )
                    complement_violations.append(
                        {
                            "date": date,
                            "pair": [ticker_a, ticker_b],
                            "t_us": t,
                            "gross_capture_c": gross,
                            "net_capture_c_after_haircut": capture,
                            "size_at_level": size,
                            "capturable": capture > 0 and size >= clip,
                        }
                    )
        # --- Ladder monotonicity at sampled states. --------------------
        for family, members in ladder_families.items():
            members_sorted = sorted(members)
            present = [
                (strike, ticker)
                for strike, ticker in members_sorted
                if ticker in quotes
            ]
            if len(present) < 2:
                continue
            base_times = quotes[present[0][1]]["t_us"]
            for i in _sampled_quote_indexes(base_times):
                t = base_times[i]
                asks: List[Tuple[float, float, str]] = []
                usable = True
                for strike, ticker in present:
                    stream = quotes[ticker]
                    j = bisect.bisect_right(stream["t_us"], t) - 1
                    if j < 0:
                        usable = False
                        break
                    asks.append((strike, stream["yes_ask_c"][j], ticker))
                if not usable:
                    continue
                for (s1, a1, m1), (s2, a2, m2) in zip(asks, asks[1:]):
                    # "Above X" ladders: implied P must be monotone
                    # non-increasing in strike.
                    if a2 > a1 + 1e-9:
                        ladder_inversions.append(
                            {
                                "date": date,
                                "family": family,
                                "t_us": t,
                                "low_strike": s1,
                                "high_strike": s2,
                                "low_ask_c": a1,
                                "high_ask_c": a2,
                                "markets": [m1, m2],
                            }
                        )
        # --- Stale-side detection. -------------------------------------
        for ticker, stream in quotes.items():
            times = stream["t_us"]
            last_bid_change_t = times[0] if times else 0
            last_ask_change_t = times[0] if times else 0
            prev_bid = None
            prev_ask = None
            for i, t in enumerate(times):
                yes_bid = stream["yes_bid_c"][i]
                yes_ask = stream["yes_ask_c"][i]
                if t_stale_us is not None and prev_bid is not None:
                    if yes_bid != prev_bid:
                        last_bid_change_t = t
                    if yes_ask != prev_ask:
                        last_ask_change_t = t
                    bid_stale = t - last_bid_change_t
                    ask_stale = t - last_ask_change_t
                    moved = abs(yes_ask - prev_ask) >= STALE_MATERIAL_MOVE_TICKS
                    if moved and bid_stale >= t_stale_us:
                        stale_events.append(
                            {
                                "date": date,
                                "market_ticker": ticker,
                                "t_us": t,
                                "stale_side": "bid",
                                "stale_us": bid_stale,
                            }
                        )
                    moved_bid = abs(yes_bid - prev_bid) >= STALE_MATERIAL_MOVE_TICKS
                    if moved_bid and ask_stale >= t_stale_us:
                        stale_events.append(
                            {
                                "date": date,
                                "market_ticker": ticker,
                                "t_us": t,
                                "stale_side": "ask",
                                "stale_us": ask_stale,
                            }
                        )
                prev_bid = yes_bid
                prev_ask = yes_ask
    capturable = [v for v in complement_violations if v["capturable"]]
    return {
        "complement_pairs_declared": len(complement_pairs),
        "complement_checks": (
            "NOT_CONSTRUCTIBLE: universe json declares no complement_of pairs"
            if not complement_pairs
            else "declared pairs scanned at sampled states"
        ),
        "complement_violation_count": len(complement_violations),
        "complement_capturable_count": len(capturable),
        "complement_not_capturable_count": (
            len(complement_violations) - len(capturable)
        ),
        "complement_violations_top": sorted(
            complement_violations,
            key=lambda v: -v["net_capture_c_after_haircut"],
        )[:50],
        "ladder_families_declared": len(ladder_families),
        "ladder_checks": (
            "NOT_CONSTRUCTIBLE: universe json carries no ladder metadata"
            if not ladder_families
            else "ladder families scanned at sampled states"
        ),
        "ladder_inversion_count": len(ladder_inversions),
        "ladder_inversions_top": ladder_inversions[:50],
        "stale_side_event_count": len(stale_events),
        "stale_side_events_top": stale_events[:50],
        "t_stale_us": t_stale_us,
        "scan_sample_interval_us": SCAN_SNAPSHOT_INTERVAL_US,
    }


def _fit_t_stale(quotes_by_date: Mapping[str, Mapping[str, Mapping[str, list]]]) -> Optional[int]:
    """T_stale = p95 of same-side inter-update gaps on TRAIN (frozen)."""
    gaps: List[int] = []
    for quotes in quotes_by_date.values():
        for stream in quotes.values():
            times = stream["t_us"]
            prev_bid = None
            last_change = None
            for i, t in enumerate(times):
                bid = stream["yes_bid_c"][i]
                if prev_bid is not None and bid != prev_bid and last_change is not None:
                    gaps.append(t - last_change)
                if prev_bid is None or bid != prev_bid:
                    last_change = t
                prev_bid = bid
    if len(gaps) < 100:
        return None
    gaps.sort()
    return gaps[min(len(gaps) - 1, int(0.95 * len(gaps)))]


# ---------------------------------------------------------------------------
# Cell aggregation.
# ---------------------------------------------------------------------------

def _band_of(q_bucket: str) -> Optional[str]:
    if q_bucket == "exploratory":
        return None
    low = int(q_bucket.split("_")[0])
    for name, lo, hi in PRIMARY_BANDS:
        if lo <= low < hi:
            return name
    return None


def _cell_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    values = [row["net_pnl_c"] for row in rows]
    clusters = {row["cluster"] for row in rows}
    resolved = [r for r in rows if r["exit_reason"] != "UNRESOLVED_CLOSE"]
    return {
        "n_opportunities": len(rows),
        "n_clusters": len(clusters),
        "mean_net_pnl_c": _mean(values),
        "unresolved_close_count": len(rows) - len(resolved),
        "positive_share": (
            sum(1 for v in values if v > 0) / len(values) if values else None
        ),
    }


# ---------------------------------------------------------------------------
# Main study.
# ---------------------------------------------------------------------------

def _emit_halt(output_dir: Path, halt: H6bHalt, context: Mapping[str, Any]) -> None:
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "execution_file": EXECUTION_FILE,
        "state": "HALTED",
        "halt_reason_code": halt.reason_code,
        "halt_detail": _jsonable(halt.detail),
        "context": _jsonable(context),
        "runner_module_sha256": _module_sha256(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / HALT_RECEIPT_NAME, _canonical_bytes(receipt))
    lines = [
        "# H6b HALT — stop condition fired",
        "",
        f"- Reason: `{halt.reason_code}`",
        f"- Detail: `{json.dumps(_jsonable(halt.detail), sort_keys=True)}`",
        "",
        "Per §14 of the execution file the runner halts and reports",
        "rather than degrading silently.  A NOT ESTIMABLE / halted outcome",
        "is a first-class, reportable result.",
    ]
    _atomic_write(output_dir / REPORT_NAME, ("\n".join(lines) + "\n").encode())


def run_study(
    checkpoint_namespace: Path,
    output_dir: Path,
    universe_path: Path,
    *,
    fee_schedule_path: Optional[Path] = None,
    settlement_path: Optional[Path] = None,
    verify_payload_hashes: bool = False,
    stage_only_contract: bool = False,
    clip: int = DEFAULT_CLIP,
) -> Dict[str, Any]:
    checkpoint_namespace = Path(checkpoint_namespace).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise H6bStudyError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    context: Dict[str, Any] = {"checkpoint_namespace": str(checkpoint_namespace)}
    try:
        fee_schedule = _load_fee_schedule(fee_schedule_path)
        universe = _load_universe(universe_path)
        settlements = _load_settlements(settlement_path)

        # Settlement-truth guard (operator directive 2026-07-25): a market
        # the catalog knows settled inside the study window must carry an
        # outcome record.  Letting it fall through to UNRESOLVED_CLOSE
        # would fake completeness.  Fail closed instead.
        claimed_settled = [
            ticker
            for ticker, meta in universe["politics_markets"].items()
            if meta.get("settled_in_window")
        ]
        outcome_map = (settlements or {}).get("outcomes", {})
        truth_missing = [t for t in claimed_settled if t not in outcome_map]
        if truth_missing:
            raise H6bHalt(
                "SETTLEMENT_TRUTH_INCOMPLETE",
                {
                    "settled_in_window_markets": len(claimed_settled),
                    "missing_outcomes": len(truth_missing),
                    "missing_sample": truth_missing[:50],
                    "note": (
                        "universe claims these markets settled inside the "
                        "window but the settlement input lacks outcomes; "
                        "refusing to mislabel them UNRESOLVED_CLOSE"
                    ),
                },
            )
        settlements_truth: Optional[Dict[str, Tuple[str, int]]] = None
        if settlements is not None:
            settlements_truth = {}
            for ticker, rec in outcome_map.items():
                result = str(rec.get("result", "")).lower()
                if result not in ("yes", "no"):
                    continue
                ts_us = rec.get("settlement_ts_us")
                if ts_us is None:
                    raw_ts = rec.get("settlement_ts") or rec.get("close_time")
                    if not raw_ts:
                        continue
                    try:
                        parsed = dt.datetime.fromisoformat(
                            str(raw_ts).replace("Z", "+00:00")
                        )
                        ts_us = int(parsed.timestamp() * 1_000_000)
                    except ValueError:
                        continue
                settlements_truth[ticker] = (result, int(ts_us))

        l2_stage = _load_stage(
            checkpoint_namespace, "l2_replay",
            L2_REQUIRED_COLUMNS, verify_payload_hashes,
        )
        trade_stage = _load_stage(
            checkpoint_namespace, "trades_market",
            TRADE_REQUIRED_COLUMNS, verify_payload_hashes,
        )
        if l2_stage["source_binding"] != trade_stage["source_binding"]:
            raise H6bStudyError("L2 and trade checkpoint source bindings differ")
        context["source_binding"] = l2_stage["source_binding"]

        window = _discover_window(l2_stage, trade_stage)
        train_dates = window["train_dates"]
        validate_dates = window["validate_dates"]

        scratch = output_dir / ".scratch"
        duckdb_temp = scratch / "duckdb-temp"
        duckdb_temp.mkdir(parents=True)
        con = duckdb.connect(str(scratch / "h6b.duckdb"))
        con.execute(f"SET memory_limit={_quote(MEMORY_LIMIT)}")
        con.execute(f"SET threads={THREADS}")
        con.execute("SET preserve_insertion_order=false")
        con.execute(f"SET temp_directory={_quote(duckdb_temp)}")
        _register_universe_table(con, universe["politics_markets"])

        sample_l2 = _selected_partitions(l2_stage, train_dates[0])
        if not sample_l2:
            raise H6bStudyError(f"no L2 partitions for {train_dates[0]}")
        granularity = _probe_granularity(con, sample_l2)

        # Depth retention is observable from the schema: the W09
        # l2_replay stage retains top-of-book only, so clip-size
        # eligibility uses displayed top size and never walks deeper
        # levels.  This is conservative, not optimistic.
        schema_names = set(sample_l2[0]["schema_names"])
        depth_levels_retained = 1
        data_contract = {
            "checkpoint_id": l2_stage["source_binding"],
            "checkpoint_retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "l2_stage_version": l2_stage["stage_version"],
            "trade_stage_version": trade_stage["stage_version"],
            "date_coverage": window["coverage"],
            "window_days": window["window_days"],
            "meets_sealed_14_day_design": window["meets_sealed_14_day_design"],
            "spec_deviations": list(SPEC_DEVIATIONS),
            "quote_granularity": granularity["quote_granularity"],
            "snapshot_interval_s": granularity["snapshot_interval_s"],
            "path_bounded": granularity.get("path_bounded", False),
            "depth_levels_retained": depth_levels_retained,
            "depth_model": (
                "top-of-book displayed size only; opportunity ineligible "
                "(DEPTH_REJECT) unless displayed size >= clip"
            ),
            "trade_tape": {
                "per_trade_size": True,
                "aggressor_side_column_present": (
                    "taker_side"
                    in set(
                        _selected_partitions(trade_stage, train_dates[0])[0][
                            "schema_names"
                        ]
                    )
                ),
            },
            "settlement_outcome_availability": settlements is not None,
            "fee_schedule": {
                key: fee_schedule[key]
                for key in ("source", "version", "fee_modeled")
            },
            "universe": {
                "path": universe["path"],
                "sha256": universe["sha256"],
                "politics_market_count": universe["politics_market_count"],
                "category_rule": universe["category_rule"],
            },
        }
        _log(f"data contract asserted; window TRAIN={train_dates} "
             f"VALIDATE={validate_dates}")

        if stage_only_contract:
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "execution_file": EXECUTION_FILE,
                "state": "CONTRACT_ONLY",
                "data_contract": data_contract,
                "train_range": [train_dates[0], train_dates[-1]],
                "validate_range": [validate_dates[0], validate_dates[-1]],
                "window_days": window["window_days"],
                "meets_sealed_14_day_design": (
                    window["meets_sealed_14_day_design"]
                ),
                "spec_deviations": list(SPEC_DEVIATIONS),
                "runner_module_sha256": _module_sha256(),
            }
            _atomic_write(output_dir / RECEIPT_NAME, _canonical_bytes(_jsonable(receipt)))
            return receipt

        # ------------------------------------------------------------------
        # TRAIN materialization (VALIDATE partitions are not opened yet).
        # ------------------------------------------------------------------
        train_quotes: Dict[str, Dict[str, Dict[str, list]]] = {}
        train_trades: Dict[str, Dict[str, Dict[str, list]]] = {}
        for date in train_dates:
            l2_rows = _selected_partitions(l2_stage, date)
            trade_rows = _selected_partitions(trade_stage, date)
            train_quotes[date] = _extract_quotes_for_date(con, l2_rows, date)
            train_trades[date] = _extract_trades_for_date(con, trade_rows, date)
            _log(f"TRAIN {date}: {len(train_quotes[date])} politics markets with "
                 f"two-sided quotes")

        s_large_fit = _fit_s_large(train_trades)
        t_stale_us = _fit_t_stale(train_quotes)

        # G0 (§4) — on TRAIN only, before any PnL at the primary lattice.
        def collect_counts(quotes_by_date):
            markets = set()
            clusters = set()
            for quotes in quotes_by_date.values():
                for ticker, stream in quotes.items():
                    markets.add(ticker)
                    clusters.add(stream["cluster"])
            return markets, clusters

        train_markets, train_clusters = collect_counts(train_quotes)

        # sigma_path: empirical std of B0 net path PnL at the primary
        # horizon/bracket on TRAIN (§G0.3).
        primary_horizon_us = dict((n, u) for n, u in HORIZONS_US)[PRIMARY_HORIZON]
        primary_bracket = next(
            b for b in BRACKET_FAMILIES if b[0] == PRIMARY_BRACKET
        )
        taker_rate = float(fee_schedule["taker_rate"])

        def run_all_clusters(
            quotes_by_date, trades_by_date, variant, horizon_us,
            bracket, clip_size, split_name, flag_overrides=None,
        ):
            rows: List[Dict[str, Any]] = []
            depth_rejects = 0
            for date in sorted(quotes_by_date):
                quotes = quotes_by_date[date]
                trades = trades_by_date.get(date, {})
                cluster_streams = _build_cluster_streams(quotes)
                for cluster, stream_rows in cluster_streams.items():
                    result = run_cluster_pass(
                        quotes, stream_rows, trades,
                        variant=variant,
                        horizon_us=horizon_us,
                        bracket_up=bracket[1],
                        bracket_down=bracket[2],
                        clip=clip_size,
                        s_large=s_large_fit["s_large_contracts"],
                        taker_rate=taker_rate,
                        flag_override=(
                            flag_overrides.get((date, cluster))
                            if flag_overrides else None
                        ),
                        settlements_truth=settlements_truth,
                    )
                    if result["lookahead_violation"]:
                        raise H6bHalt(
                            "LOOKAHEAD_ASSERTION_UNVERIFIABLE",
                            {"date": date, "cluster": cluster},
                        )
                    for opp in result["opportunities"]:
                        opp["cluster"] = cluster
                        opp["date"] = date
                        opp["split"] = split_name
                        opp["variant"] = variant
                        rows.append(opp)
                    depth_rejects += result["depth_rejects"]
            return rows, depth_rejects

        _log("G0: running TRAIN B0 primary-cell pass for sigma_path")
        g0_rows, g0_depth_rejects = run_all_clusters(
            train_quotes, train_trades, "B0",
            primary_horizon_us, primary_bracket, clip, "TRAIN",
        )
        g0_values = [row["net_pnl_c"] for row in g0_rows]
        if len(g0_values) >= 2:
            mu = _mean(g0_values)
            sigma_path = math.sqrt(
                sum((v - mu) ** 2 for v in g0_values) / (len(g0_values) - 1)
            )
        else:
            sigma_path = None

        def mde_at(n_clusters: int, sigma: float) -> Optional[float]:
            if not n_clusters or sigma is None:
                return None
            return POWER_MULTIPLIER * sigma / math.sqrt(n_clusters)

        observed_clusters = len({row["cluster"] for row in g0_rows})
        h6b_mde = mde_at(observed_clusters, sigma_path)
        h6b_estimable = (
            h6b_mde is not None and h6b_mde <= PLAUSIBLE_EDGE_CENTS["H6B"]
        )
        required_clusters = (
            math.ceil((POWER_MULTIPLIER * sigma_path / PLAUSIBLE_EDGE_CENTS["H6B"]) ** 2)
            if sigma_path
            else None
        )
        g0_report = {
            "h6b": {
                "opportunity_count": len(g0_rows),
                "unique_market_count": len(train_markets),
                "unique_cluster_count": len(train_clusters),
                "clusters_with_opportunities": observed_clusters,
                "sigma_path_c": sigma_path,
                "mde_c_at_observed_clusters": h6b_mde,
                "plausible_edge_c": PLAUSIBLE_EDGE_CENTS["H6B"],
                "required_clusters_at_plausible_edge": required_clusters,
                "gate": "PASS" if h6b_estimable else "NOT_ESTIMABLE",
                "depth_reject_count_train_primary": g0_depth_rejects,
            },
            "h6a_mv": {
                "gate": (
                    "NOT_ESTIMABLE"
                    if settlements is None
                    else "PASS_PENDING_UNIVERSE_SCREEN"
                ),
                "reason": (
                    "settlement outcomes unavailable in data contract"
                    if settlements is None
                    else "settlement data supplied; screen §5.3 required"
                ),
            },
            "scan": {"gate": "NOT_GATED_BY_G0"},
        }

        # ------------------------------------------------------------------
        # Seal TRAIN parameters before any VALIDATE payload is read (§3).
        # ------------------------------------------------------------------
        seal_payload = {
            "schema_version": SCHEMA_VERSION,
            "execution_file": EXECUTION_FILE,
            "train_dates": train_dates,
            "validate_dates": validate_dates,
            "window_days": window["window_days"],
            "meets_sealed_14_day_design": window["meets_sealed_14_day_design"],
            "spec_deviations": list(SPEC_DEVIATIONS),
            "cross_declarations": list(CROSS_DECLARATIONS),
            "clip": clip,
            "adverse_fill_ticks": ADVERSE_FILL_TICKS,
            "s_large": s_large_fit,
            "t_stale_us": t_stale_us,
            "k_cluster": K_CLUSTER,
            "fee_schedule": {
                key: fee_schedule.get(key)
                for key in ("source", "version", "taker_rate", "fee_modeled")
            },
            "universe_sha256": universe["sha256"],
            "q_bucket_edges": Q_BUCKET_EDGES,
            "horizons": [name for name, _ in HORIZONS_US],
            "bracket_families": [name for name, _u, _d in BRACKET_FAMILIES],
            "primary_endpoint": {
                "variant": PRIMARY_VARIANT,
                "side": "favorite",
                "horizon": PRIMARY_HORIZON,
                "bracket": PRIMARY_BRACKET,
                "k_cluster": K_CLUSTER,
                "bands": [name for name, _lo, _hi in PRIMARY_BANDS],
            },
            "g0": g0_report,
            "sigma_path_c": sigma_path,
        }
        seal_path = output_dir / SEAL_NAME
        seal_bytes = _canonical_bytes(_jsonable(seal_payload))
        _atomic_write(seal_path, seal_bytes)
        train_seal_hash = _sha256_path(seal_path)
        if train_seal_hash != _sha256_bytes(seal_bytes):
            raise H6bHalt("TRAIN_SEAL_HASH_MISMATCH", {"path": str(seal_path)})
        _log(f"TRAIN seal written: {train_seal_hash}")
        rng = random.Random(int(train_seal_hash[:16], 16))

        if not h6b_estimable:
            # §G0.4: the module stops; no PnL is computed "for
            # information".  SCAN is not gated by G0 and still runs.
            _log(
                "G0: H6b gate NOT_ESTIMABLE — PnL lattice will be skipped, "
                "SCAN census still runs (§0: G0 does not gate SCAN)"
            )

        # ------------------------------------------------------------------
        # VALIDATE materialization (seal verified immediately before).
        # ------------------------------------------------------------------
        if _sha256_path(seal_path) != train_seal_hash:
            raise H6bHalt("TRAIN_SEAL_HASH_MISMATCH", {"path": str(seal_path)})
        validate_quotes: Dict[str, Dict[str, Dict[str, list]]] = {}
        validate_trades: Dict[str, Dict[str, Dict[str, list]]] = {}
        for date in validate_dates:
            l2_rows = _selected_partitions(l2_stage, date)
            trade_rows = _selected_partitions(trade_stage, date)
            validate_quotes[date] = _extract_quotes_for_date(con, l2_rows, date)
            validate_trades[date] = _extract_trades_for_date(con, trade_rows, date)
            _log(f"VALIDATE {date}: {len(validate_quotes[date])} politics markets")

        # ------------------------------------------------------------------
        # Full lattice (only when H6b is estimable).
        # ------------------------------------------------------------------
        cells: Dict[str, Any] = {}
        depth_reject_total = 0
        secondary_tests: List[Tuple[str, float]] = []
        primary_results: Dict[str, Any] = {}
        opportunity_log_path = output_dir / OPPORTUNITY_TABLE_NAME
        opportunity_log = opportunity_log_path.open("w", encoding="utf-8")
        c1_summary: Optional[Dict[str, Any]] = None
        c2_summary: Optional[Dict[str, Any]] = None
        capacity_curve: List[Dict[str, Any]] = []

        if h6b_estimable:
            splits = (
                ("TRAIN", train_quotes, train_trades),
                ("VALIDATE", validate_quotes, validate_trades),
            )
            for variant in ("B0", "B1", "B2"):
                for horizon_name, horizon_us in HORIZONS_US:
                    for bracket in BRACKET_FAMILIES:
                        for split_name, quotes_by_date, trades_by_date in splits:
                            rows, depth_rejects = run_all_clusters(
                                quotes_by_date, trades_by_date, variant,
                                horizon_us, bracket, clip, split_name,
                            )
                            depth_reject_total += depth_rejects
                            for row in rows:
                                opportunity_log.write(
                                    json.dumps(_jsonable(row), sort_keys=True) + "\n"
                                )
                            by_bucket: Dict[str, List[Mapping[str, Any]]] = {}
                            for row in rows:
                                by_bucket.setdefault(row["q_bucket"], []).append(row)
                            for bucket, bucket_rows in by_bucket.items():
                                cell_key = (
                                    f"{variant}|{split_name}|{horizon_name}|"
                                    f"{bracket[0]}|{bucket}"
                                )
                                cells[cell_key] = _cell_summary(bucket_rows)
                            # Primary endpoint cells (§10.1).
                            if (
                                variant == PRIMARY_VARIANT
                                and horizon_name == PRIMARY_HORIZON
                                and bracket[0] == PRIMARY_BRACKET
                            ):
                                for band_name, lo, hi in PRIMARY_BANDS:
                                    band_rows = [
                                        row for row in rows
                                        if row["q_bucket"] != "exploratory"
                                        and lo <= int(row["q_bucket"].split("_")[0]) < hi
                                    ]
                                    summary = _cell_summary(band_rows)
                                    summary["bootstrap"] = cluster_bootstrap_ci(
                                        band_rows, rng
                                    )
                                    primary_results[f"{split_name}|{band_name}"] = summary
                                    primary_results[
                                        f"{split_name}|{band_name}|rows"
                                    ] = band_rows

            _log("running C2 label permutations on the primary cells")
            c2_stats: Dict[str, List[float]] = {}
            for band_name, lo, hi in PRIMARY_BANDS:
                observed_rows = primary_results.get(
                    f"VALIDATE|{band_name}|rows", []
                )
                observed_mean = _mean([r["net_pnl_c"] for r in observed_rows])
                # Build the eligible B0 VALIDATE opportunity set for this
                # band as the permutation frame.
                b0_rows, _dr = run_all_clusters(
                    validate_quotes, validate_trades, "B0",
                    primary_horizon_us, primary_bracket, clip, "VALIDATE",
                )
                frame = [
                    row for row in b0_rows
                    if row["q_bucket"] != "exploratory"
                    and lo <= int(row["q_bucket"].split("_")[0]) < hi
                ]
                flags = [row["flag"] for row in frame]
                values = [row["net_pnl_c"] for row in frame]
                strata: Dict[str, List[int]] = {}
                for index, row in enumerate(frame):
                    strata.setdefault(row["cluster"], []).append(index)
                nulls: List[float] = []
                for _ in range(C2_PERMUTATIONS):
                    permuted = list(flags)
                    for indexes in strata.values():
                        shuffled = [flags[i] for i in indexes]
                        rng.shuffle(shuffled)
                        for slot, i in enumerate(indexes):
                            permuted[i] = shuffled[slot]
                    flagged = [
                        values[i] for i in range(len(values)) if permuted[i] == 1
                    ]
                    if flagged:
                        nulls.append(sum(flagged) / len(flagged))
                c2_stats[band_name] = nulls
                if observed_mean is not None and nulls:
                    pct = permutation_percentile(observed_mean, nulls)
                    primary_results[f"VALIDATE|{band_name}"]["c2_percentile"] = pct
            c2_summary = {
                "permutations": C2_PERMUTATIONS,
                "frame": (
                    "B0 VALIDATE opportunity grid; flags permuted within "
                    "event-cluster strata (§5.2); observed statistic is the "
                    "sequential B1 arm mean"
                ),
                "null_summary": {
                    band: {
                        "n": len(stats),
                        "mean": _mean(stats),
                        "p95": (
                            sorted(stats)[int(0.95 * len(stats))]
                            if stats else None
                        ),
                    }
                    for band, stats in c2_stats.items()
                },
            }

            _log("running C1 timestamp shuffles on the primary cells")
            c1_means: List[float] = []
            for _ in range(C1_SHUFFLES):
                shuffled_rows: List[float] = []
                for date in sorted(validate_quotes):
                    quotes = validate_quotes[date]
                    trades = validate_trades.get(date, {})
                    cluster_streams = _build_cluster_streams(quotes)
                    for cluster, stream_rows in cluster_streams.items():
                        entry_override = list(stream_rows)
                        rng.shuffle(entry_override)
                        entry_override = sorted(
                            entry_override[: max(1, len(entry_override) // 4)]
                        )
                        result = run_cluster_pass(
                            quotes, stream_rows, trades,
                            variant="B1",
                            horizon_us=primary_horizon_us,
                            bracket_up=primary_bracket[1],
                            bracket_down=primary_bracket[2],
                            clip=clip,
                            s_large=s_large_fit["s_large_contracts"],
                            taker_rate=taker_rate,
                            entry_time_override=entry_override,
                        )
                        shuffled_rows.extend(
                            opp["net_pnl_c"] for opp in result["opportunities"]
                        )
                mean_value = _mean(shuffled_rows)
                if mean_value is not None:
                    c1_means.append(mean_value)
            c1_summary = {
                "shuffles": C1_SHUFFLES,
                "mean_of_means_c": _mean(c1_means),
                "note": (
                    "entry timestamps subsampled+shuffled within cluster, "
                    "price path fixed; surviving EV = pipeline artifact"
                ),
            }

            _log("running capacity curve at the primary endpoint")
            for capacity_clip in CAPACITY_CLIPS:
                rows, depth_rejects = run_all_clusters(
                    validate_quotes, validate_trades, PRIMARY_VARIANT,
                    primary_horizon_us, primary_bracket, capacity_clip,
                    "VALIDATE",
                )
                entry: Dict[str, Any] = {"clip": capacity_clip,
                                         "depth_rejects": depth_rejects}
                for band_name, lo, hi in PRIMARY_BANDS:
                    band_rows = [
                        row for row in rows
                        if row["q_bucket"] != "exploratory"
                        and lo <= int(row["q_bucket"].split("_")[0]) < hi
                    ]
                    entry[band_name] = _cell_summary(band_rows)
                capacity_curve.append(entry)

            # Secondary multiplicity accounting (§10.3): every non-primary
            # cell mean tested against the cluster bootstrap null.
            for cell_key, summary in cells.items():
                if summary["n_opportunities"] < 2 or summary["n_clusters"] < 2:
                    continue
                variant, split_name, horizon_name, bracket_name, bucket = (
                    cell_key.split("|")
                )
                is_primary = (
                    variant == PRIMARY_VARIANT
                    and split_name == "VALIDATE"
                    and horizon_name == PRIMARY_HORIZON
                    and bracket_name == PRIMARY_BRACKET
                    and _band_of(bucket) is not None
                )
                if is_primary:
                    continue
                mean_value = summary["mean_net_pnl_c"]
                if mean_value is None:
                    continue
                # Normal-approximation p against zero using the cell's
                # cluster count (raw; BH applied across all).
                p_proxy = 1.0  # conservative placeholder when no spread
                if sigma_path and summary["n_clusters"] >= 2:
                    z = abs(mean_value) / (
                        sigma_path / math.sqrt(summary["n_clusters"])
                    )
                    p_proxy = math.erfc(z / math.sqrt(2.0))
                secondary_tests.append((cell_key, p_proxy))

        opportunity_log.close()
        bh = benjamini_hochberg(secondary_tests, BH_Q) if secondary_tests else {
            "total_tests": 0, "passing": [], "q": BH_Q,
        }

        # ------------------------------------------------------------------
        # SCAN (not gated by G0): TRAIN + VALIDATE census.
        # ------------------------------------------------------------------
        _log("running SCAN census")
        all_quotes: Dict[str, Mapping[str, Mapping[str, list]]] = {}
        all_quotes.update(train_quotes)
        all_quotes.update(validate_quotes)
        scan_result = run_scan(
            all_quotes, universe, clip, taker_rate, t_stale_us
        )

        # ------------------------------------------------------------------
        # Shortlist (§11) and receipt (§13).
        # ------------------------------------------------------------------
        def primary_number(split: str, band: str) -> Optional[float]:
            cell = primary_results.get(f"{split}|{band}")
            return cell["mean_net_pnl_c"] if cell else None

        shortlist: List[Dict[str, Any]] = []
        for band_name, _lo, _hi in PRIMARY_BANDS:
            cell = primary_results.get(f"VALIDATE|{band_name}")
            if not h6b_estimable:
                shortlist.append({
                    "template": f"H6b B1 favorite {PRIMARY_HORIZON} "
                                f"{PRIMARY_BRACKET} q{band_name}",
                    "label": "NOT_ESTIMABLE",
                    "required_clusters": required_clusters,
                    "observed_clusters": observed_clusters,
                })
                continue
            if not cell or cell["mean_net_pnl_c"] is None:
                shortlist.append({
                    "template": f"H6b B1 favorite {PRIMARY_HORIZON} "
                                f"{PRIMARY_BRACKET} q{band_name}",
                    "label": "DROP",
                    "reason": "no eligible VALIDATE opportunities",
                })
                continue
            positive = cell["mean_net_pnl_c"] > 0
            enough_clusters = cell["n_clusters"] >= PROMOTION_MIN_CLUSTERS
            c2_ok = (cell.get("c2_percentile") or 0) >= 0.95
            c1_ok = (
                c1_summary is not None
                and (c1_summary["mean_of_means_c"] or 0) <= 0
            )
            label = "DROP"
            if positive and enough_clusters and c2_ok and c1_ok:
                label = "WATCH"  # PROMOTE additionally needs §11 items 4-12
            shortlist.append({
                "template": f"H6b B1 favorite {PRIMARY_HORIZON} "
                            f"{PRIMARY_BRACKET} q{band_name}",
                "label": label,
                "validate_mean_net_pnl_c": cell["mean_net_pnl_c"],
                "n_clusters": cell["n_clusters"],
                "c2_percentile": cell.get("c2_percentile"),
                "gates": {
                    "positive_ev": positive,
                    "clusters_ge_100": enough_clusters,
                    "c1_no_ev": c1_ok,
                    "c2_p95": c2_ok,
                },
            })
        shortlist.append({
            "template": "H6a-MV calibration machinery validation",
            "label": g0_report["h6a_mv"]["gate"],
            "reason": g0_report["h6a_mv"]["reason"],
        })
        shortlist.append({
            "template": "SCAN structural census",
            "label": (
                "WATCH" if scan_result["complement_capturable_count"] > 0
                else "DROP"
            ),
            "capturable_complement_violations": (
                scan_result["complement_capturable_count"]
            ),
        })

        # Strip row-level payloads out of the receipt.
        primary_public = {
            key: value for key, value in primary_results.items()
            if not key.endswith("|rows")
        }

        receipt = {
            "schema_version": SCHEMA_VERSION,
            "execution_file": EXECUTION_FILE,
            "state": "COMPLETE",
            "run_id": None,  # assigned by the W09 wrapper
            "runner_version": SCHEMA_VERSION,
            "runner_module_sha256": _module_sha256(),
            "checkpoint_id": l2_stage["source_binding"],
            "checkpoint_retrieved_at": data_contract["checkpoint_retrieved_at"],
            "fee_schedule_version": fee_schedule.get("version"),
            "fee_modeled": bool(fee_schedule.get("fee_modeled")),
            "quote_granularity": data_contract["quote_granularity"],
            "snapshot_interval_s": data_contract["snapshot_interval_s"],
            "train_range": [train_dates[0], train_dates[-1]],
            "validate_range": [validate_dates[0], validate_dates[-1]],
            "window_days": window["window_days"],
            "meets_sealed_14_day_design": window["meets_sealed_14_day_design"],
            "spec_deviations": list(SPEC_DEVIATIONS),
            "cross_declarations": list(CROSS_DECLARATIONS),
            "train_seal_hash": train_seal_hash,
            "modules_run": (
                ["G0", "H6B", "SCAN"] if h6b_estimable else ["G0", "SCAN"]
            ),
            "g0_result_per_module": {
                "H6B": g0_report["h6b"]["gate"],
                "H6A_MV": g0_report["h6a_mv"]["gate"],
                "SCAN": "NOT_GATED",
            },
            "cluster_count_per_universe": {
                "politics_train": len(train_clusters),
            },
            "clip_size": clip,
            "adverse_fill_ticks": ADVERSE_FILL_TICKS,
            "primary_endpoint_definition": seal_payload["primary_endpoint"],
            "primary_endpoint_value_55_75": primary_number("VALIDATE", "55_75"),
            "primary_endpoint_value_75_90": primary_number("VALIDATE", "75_90"),
            "secondary_test_count": bh["total_tests"],
            "bh_q": BH_Q,
            "bh_passing": bh["passing"],
            "permutation_replicates": C2_PERMUTATIONS,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "depth_reject_count": depth_reject_total,
            "path_bounded_count": (
                len(cells) if data_contract["path_bounded"] else 0
            ),
            "data_contract": data_contract,
            "g0": g0_report,
            "primary_results": primary_public,
            "cells": cells,
            "c1": c1_summary,
            "c2": c2_summary,
            "capacity_curve": capacity_curve,
            "scan": scan_result,
            "shortlist": shortlist,
        }
        receipt_bytes = _canonical_bytes(_jsonable(receipt))
        _atomic_write(output_dir / RECEIPT_NAME, receipt_bytes)
        _atomic_write(
            output_dir / REPORT_NAME, _render_markdown(receipt).encode("utf-8")
        )
        _log("study complete")
        return receipt

    except H6bHalt as halt:
        _emit_halt(output_dir, halt, context)
        raise


def _render_markdown(receipt: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# ALPHA-SPRINT-01 H6b politics path study")
    lines.append("")
    lines.append("Read-only research. No live orders. No cash-PnL claim. "
                 "All EV modeled, fee-inclusive, 1-tick adverse haircut both ways.")
    lines.append("")
    lines.append("## Data contract")
    contract = receipt["data_contract"]
    lines.append(f"- checkpoint: `{receipt['checkpoint_id']}`")
    lines.append(f"- quote granularity: {contract['quote_granularity']}"
                 f" (path_bounded={contract['path_bounded']})")
    lines.append(f"- depth levels retained: {contract['depth_levels_retained']}"
                 f" — {contract['depth_model']}")
    lines.append(f"- settlement outcomes available: "
                 f"{contract['settlement_outcome_availability']}")
    lines.append(f"- fee schedule: {contract['fee_schedule']['version']}"
                 f" (FEE_MODELED={contract['fee_schedule']['fee_modeled']})")
    lines.append(f"- TRAIN {receipt['train_range']}  VALIDATE "
                 f"{receipt['validate_range']} "
                 f"({receipt['window_days']} contiguous days)")
    if not receipt["meets_sealed_14_day_design"]:
        lines.append(
            "- **SPEC DEVIATION**: window is shorter than the sealed "
            "14-day design; this is a revised test under the principal's "
            "2026-07-24 use-all-available-data decision, not the sealed "
            "H6b design. Results are not comparable to a 7+7 run."
        )
    lines.append(f"- train_seal_hash: `{receipt['train_seal_hash']}`")
    lines.append("")
    lines.append("## G0 estimability")
    lines.append("```json")
    lines.append(json.dumps(_jsonable(receipt["g0"]), indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Primary endpoint (sealed §10.1)")
    lines.append("Variant B1, favorite side, horizon 1h, bracket +10/−10, "
                 "K_cluster=1, clip "
                 f"{receipt['clip_size']}.")
    for band in ("55_75", "75_90"):
        value = receipt.get(f"primary_endpoint_value_{band}")
        cell = receipt["primary_results"].get(f"VALIDATE|{band}", {})
        lines.append(
            f"- q [{band.replace('_', ',')}) VALIDATE mean net PnL: "
            f"{value if value is not None else 'n/a'} c/contract; "
            f"clusters={cell.get('n_clusters')}; "
            f"C2 pct={cell.get('c2_percentile')}"
        )
    lines.append("")
    lines.append("## Controls")
    lines.append("```json")
    lines.append(json.dumps(_jsonable({"C1": receipt["c1"], "C2": receipt["c2"]}),
                            indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Capacity curve (primary endpoint)")
    lines.append("```json")
    lines.append(json.dumps(_jsonable(receipt["capacity_curve"]),
                            indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## SCAN census")
    scan = receipt["scan"]
    lines.append(f"- complement violations: {scan['complement_violation_count']}"
                 f" (capturable after haircut: "
                 f"{scan['complement_capturable_count']})")
    lines.append(f"- stale-side events: {scan['stale_side_event_count']}")
    lines.append(f"- ladder checks: {scan['ladder_checks']}")
    lines.append("")
    lines.append("## Shortlist")
    lines.append("```json")
    lines.append(json.dumps(_jsonable(receipt["shortlist"]), indent=2,
                            sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append(f"Secondary tests computed: {receipt['secondary_test_count']} "
                 f"(BH q={receipt['bh_q']}; passing: {receipt['bh_passing']})")
    lines.append("")
    lines.append("Full per-cell table in the receipt JSON; per-opportunity "
                 f"rows in `{OPPORTUNITY_TABLE_NAME}`.")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-namespace", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--universe-json", required=True, type=Path,
                        help="market_ticker -> {category, event_cluster, ladder?}")
    parser.add_argument("--fee-schedule-json", type=Path, default=None,
                        help="live fee schedule; absent => frozen fallback, "
                             "all numbers FEE_MODELED")
    parser.add_argument("--settlement-json", type=Path, default=None)
    parser.add_argument("--verify-payload-hashes", action="store_true")
    parser.add_argument("--stage", choices=("contract", "all"), default="all",
                        help="'contract' asserts the data contract + window "
                             "and stops (cheap estimability probe)")
    parser.add_argument("--clip", type=int, default=DEFAULT_CLIP)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_study(
            args.checkpoint_namespace,
            args.output_dir,
            args.universe_json,
            fee_schedule_path=args.fee_schedule_json,
            settlement_path=args.settlement_json,
            verify_payload_hashes=args.verify_payload_hashes,
            stage_only_contract=(args.stage == "contract"),
            clip=args.clip,
        )
    except H6bHalt as halt:
        _log(f"HALT: {halt.reason_code}")
        return 3
    except H6bStudyError as error:
        _log(f"CONTRACT FAILURE: {error}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
