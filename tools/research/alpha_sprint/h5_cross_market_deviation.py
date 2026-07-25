#!/usr/bin/env python3
"""Run the H5 cross-market deviation event study on Deep03 checkpoints.

Defensive lead/lag only (AS02-CROSS-MARKET-DEVIATION): when a leg of a
canonical multi-market event family reprices, how quickly do sibling legs
reprice — measured against a time-reversed control and an explicitly
UNAUTHENTICATED different-event-ticker proxy control.  No arbitrage, payout,
fee, latency, fill, or cash-PnL claim is produced.

Design constants are frozen in
``docs/research_reports/ALPHA_SPRINT_01_H5_PREREG_2026-07-25.md``; the
receipt binds the SHA-256 of that document, this module, and every input.

* TRAIN is exactly 2026-07-12 and 2026-07-15; VALIDATE is exactly
  2026-07-17 and is read only after the TRAIN model seal is written.
* Family identity comes ONLY from the sealed MARKET_GRAPH parquet
  (``mapping_status = 'CANONICAL_FAMILY_EVENT'`` joined on
  ``(date, market_ticker)``); ``event_proxy`` is never consulted.
* Quote validity, leader-event, burst de-dup, simultaneity set-aside and
  cluster-bootstrap uncertainty follow the preregistration exactly.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import random
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by the W09 wrapper
    raise SystemExit(
        "duckdb is required; on W09 use /opt/w09/venv/bin/python"
    ) from exc


TRAIN_DATES: Tuple[str, ...] = ("2026-07-12", "2026-07-15")
VALIDATE_DATES: Tuple[str, ...] = ("2026-07-17",)
ALL_DATES: Tuple[str, ...] = TRAIN_DATES + VALIDATE_DATES
HORIZONS_US: Tuple[int, ...] = (100_000, 1_000_000, 5_000_000)
PRIMARY_HORIZON_US = 100_000
SECONDARY_HORIZON_US = 1_000_000
MIN_TICK_E4 = 100.0
BURST_DEDUP_US = 1_000_000
MIN_TRAIN_LEADER_EVENTS = 20
MIN_FAMILY_LEGS = 2
BOOTSTRAP_RESAMPLES = 2_000
BOOTSTRAP_SEED = 202607_25
PROXY_SEED = 202607_11
MEMORY_LIMIT = "16GB"
THREADS = 2
EXPECTED_MARKET_GRAPH_SHA256 = (
    "78d2f23bb8ffb1b2c021e5e61535a0f54e2d42fda3354cf9cd6620d5483bd278"
)
CANONICAL_MAPPING_STATUS = "CANONICAL_FAMILY_EVENT"
PREREG_RELATIVE = Path(
    "docs/research_reports/ALPHA_SPRINT_01_H5_PREREG_2026-07-25.md"
)
RECEIPT_NAME = "H5_EVENT_STUDY_RECEIPT.json"
REPORT_NAME = "H5_EVENT_STUDY_REPORT.md"
MODEL_SEAL_NAME = "H5_TRAIN_MODEL_SEAL.json"
SCHEMA_VERSION = "alpha-sprint-h5-cross-market-deviation-v1"
MODEL_SCHEMA_VERSION = "alpha-sprint-h5-train-only-model-v1"
PARTITION_RE = re.compile(
    r"^date=(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})_bucket=(?P<bucket>[0-9]{2,3})$"
)

L2_REQUIRED_COLUMNS = {
    "date",
    "t_us",
    "market_ticker",
    "ws_sid",
    "ws_seq",
    "classification",
    "snapshot_epoch",
    "book_valid",
    "topology",
    "mid_e4",
}
GRAPH_REQUIRED_COLUMNS = {
    "date",
    "market_ticker",
    "event_ticker",
    "mapping_status",
    "l2_rows",
}


class H5StudyError(RuntimeError):
    """Fail-closed input, split, or output contract error."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def _module_sha256() -> str:
    return _sha256_path(Path(__file__).resolve())


def _prereg_binding() -> Dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    path = root / PREREG_RELATIVE
    if path.is_symlink() or not path.is_file():
        raise H5StudyError(f"preregistration document is unavailable: {path}")
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="strict")
    # The graph hash below is the PREREGISTERED literal on purpose: tests
    # monkeypatch EXPECTED_MARKET_GRAPH_SHA256 for synthetic fixtures, and
    # that must never weaken the drift check against the frozen document.
    required_fragments = (
        "PREREGISTERED_BEFORE_IMPLEMENTATION_RUN",
        "BURST_DEDUP_US = 1_000_000",
        "MIN_TRAIN_LEADER_EVENTS = 20",
        "78d2f23bb8ffb1b2c021e5e61535a0f54e2d42fda3354cf9cd6620d5483bd278",
        "DIFFERENT_EVENT_TICKER_PROXY",
        "unrelated_root_control_authenticated: false",
    )
    for fragment in required_fragments:
        if fragment not in text:
            raise H5StudyError(
                f"preregistration drift: fragment missing: {fragment!r}"
            )
    return {"path": str(PREREG_RELATIVE), "sha256": _sha256_bytes(raw)}


def _parse_partition_key(key: str) -> Tuple[str, int]:
    match = PARTITION_RE.match(key)
    if match is None:
        raise H5StudyError(f"unexpected checkpoint partition key: {key}")
    return match.group("date"), int(match.group("bucket"))


def _schema_names(receipt: Mapping[str, Any]) -> set:
    schema = receipt.get("schema")
    if not isinstance(schema, list):
        return set()
    names = set()
    for row in schema:
        if isinstance(row, Mapping) and isinstance(row.get("name"), str):
            names.add(row["name"])
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
        raise H5StudyError(
            f"checkpoint stage manifest is unavailable: {manifest_path}"
        )
    raw_manifest = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw_manifest)
    except ValueError as exc:
        raise H5StudyError(
            f"checkpoint manifest is not JSON: {manifest_path}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("state") != "COMPLETE"
        or manifest.get("stage") != stage
        or not isinstance(manifest.get("source_binding"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["source_binding"])
    ):
        raise H5StudyError(f"checkpoint manifest binding/state mismatch: {stage}")
    partitions_raw = manifest.get("partitions")
    if not isinstance(partitions_raw, list) or not partitions_raw:
        raise H5StudyError(f"checkpoint stage has no partitions: {stage}")

    partitions: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for item in partitions_raw:
        if not isinstance(item, Mapping):
            raise H5StudyError(f"invalid checkpoint manifest partition: {stage}")
        key = item.get("partition_key")
        receipt_relative = item.get("receipt_path")
        if not isinstance(key, str) or not isinstance(receipt_relative, str):
            raise H5StudyError(f"checkpoint partition identity is absent: {stage}")
        date, bucket = _parse_partition_key(key)
        bucket_text = key.rsplit("=", 1)[-1]
        if len(bucket_text) != 3:
            raise H5StudyError(
                f"unexpected checkpoint bucket width: {stage}:{key}"
            )
        receipt_path = checkpoint_namespace / receipt_relative
        expected_receipt_path = (
            checkpoint_namespace / stage / "receipts" / f"{key}.json"
        )
        if (
            receipt_path.resolve() != expected_receipt_path.resolve()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise H5StudyError(f"checkpoint receipt path mismatch: {stage}:{key}")
        receipt_raw = receipt_path.read_bytes()
        if item.get("receipt_sha256") != _sha256_bytes(receipt_raw):
            raise H5StudyError(f"checkpoint receipt hash mismatch: {stage}:{key}")
        try:
            receipt = json.loads(receipt_raw)
        except ValueError as exc:
            raise H5StudyError(
                f"checkpoint receipt is not JSON: {stage}:{key}"
            ) from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("state") != "COMPLETE"
            or receipt.get("stage") != stage
            or receipt.get("partition_key") != key
            or receipt.get("source_binding") != manifest["source_binding"]
        ):
            raise H5StudyError(
                f"checkpoint receipt binding mismatch: {stage}:{key}"
            )
        data = receipt.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("path"), str):
            raise H5StudyError(f"checkpoint data receipt is absent: {stage}:{key}")
        data_path = checkpoint_namespace / data["path"]
        expected_data_path = (
            checkpoint_namespace / stage / "data" / f"{key}.parquet"
        )
        if (
            data_path.resolve() != expected_data_path.resolve()
            or data_path.is_symlink()
            or not data_path.is_file()
        ):
            raise H5StudyError(f"checkpoint payload path mismatch: {stage}:{key}")
        if data_path.stat().st_size != data.get("size_bytes"):
            raise H5StudyError(f"checkpoint payload size mismatch: {stage}:{key}")
        if item.get("data_sha256") != data.get("sha256"):
            raise H5StudyError(f"checkpoint payload receipt drift: {stage}:{key}")
        missing = required_columns - _schema_names(receipt)
        if missing:
            raise H5StudyError(
                f"checkpoint schema missing {sorted(missing)}: {stage}:{key}"
            )
        identity = (date, bucket)
        if identity in partitions:
            raise H5StudyError(f"duplicate checkpoint partition: {stage}:{key}")
        partitions[identity] = {
            "key": key,
            "date": date,
            "bucket": bucket,
            "path": data_path,
            "receipt_sha256": item["receipt_sha256"],
            "data_sha256": data["sha256"],
            "verify_payload_hash_when_opened": verify_payload_hashes,
            "row_count": int(data.get("row_count") or 0),
        }

    if len(partitions) != manifest.get("partition_count"):
        raise H5StudyError(f"checkpoint manifest partition count mismatch: {stage}")
    if sum(row["row_count"] for row in partitions.values()) != manifest.get(
        "row_count"
    ):
        raise H5StudyError(f"checkpoint manifest row count mismatch: {stage}")
    return {
        "stage": stage,
        "source_binding": manifest["source_binding"],
        "manifest_sha256": _sha256_bytes(raw_manifest),
        "partitions": partitions,
    }


def _selected_partitions(
    stage: Mapping[str, Any], date: str
) -> List[Dict[str, Any]]:
    rows = [
        value
        for (partition_date, _bucket), value in stage["partitions"].items()
        if partition_date == date
    ]
    if not rows:
        raise H5StudyError(f"no l2_replay partitions for {date}")
    return sorted(rows, key=lambda row: row["bucket"])


def _verify_partition_payloads(rows: Iterable[Mapping[str, Any]]) -> None:
    for row in rows:
        if not row.get("verify_payload_hash_when_opened"):
            continue
        actual = _sha256_path(row["path"])
        if actual != row["data_sha256"]:
            raise H5StudyError(
                f"checkpoint payload bytes drifted: {row['key']}"
            )


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _path_list(paths: Sequence[Path]) -> str:
    return "[" + ", ".join(_quote(str(p)) for p in paths) + "]"


def _load_market_graph(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise H5StudyError(f"market graph parquet is unavailable: {path}")
    sha = _sha256_path(path)
    if sha != EXPECTED_MARKET_GRAPH_SHA256:
        raise H5StudyError(
            "market graph bytes do not match the sealed binding: "
            f"expected {EXPECTED_MARKET_GRAPH_SHA256} got {sha}"
        )
    return {"path": path, "sha256": sha}


def _families_for_date(
    con: Any, graph_path: Path, date: str
) -> Dict[str, List[str]]:
    sql = f"""
        SELECT event_ticker, market_ticker
        FROM read_parquet({_quote(str(graph_path))})
        WHERE CAST(date AS VARCHAR) = {_quote(date)}
          AND mapping_status = {_quote(CANONICAL_MAPPING_STATUS)}
          AND event_ticker IS NOT NULL
          AND TRY_CAST(l2_rows AS BIGINT) > 0
    """
    families: Dict[str, List[str]] = {}
    for event_ticker, market_ticker in con.execute(sql).fetchall():
        families.setdefault(str(event_ticker), []).append(str(market_ticker))
    return {
        family: sorted(set(legs))
        for family, legs in families.items()
        if len(set(legs)) >= MIN_FAMILY_LEGS
    }


def _change_pairs_sql(l2_paths: Sequence[Path], date: str) -> str:
    return f"""
        WITH valid AS (
            SELECT
                market_ticker,
                CAST(t_us AS BIGINT) AS t_us,
                CAST(ws_sid AS BIGINT) AS ws_sid,
                CAST(ws_seq AS BIGINT) AS ws_seq,
                CAST(snapshot_epoch AS BIGINT) AS snapshot_epoch,
                CAST(mid_e4 AS DOUBLE) AS mid_e4
            FROM read_parquet({_path_list(l2_paths)})
            WHERE CAST(date AS VARCHAR) = {_quote(date)}
              AND book_valid = TRUE
              AND classification = 'DELTA_APPLIED'
              AND topology = 'TWO_SIDED'
              AND mid_e4 IS NOT NULL
              AND t_us IS NOT NULL
              AND snapshot_epoch IS NOT NULL
        ),
        laggy AS (
            SELECT
                market_ticker,
                t_us,
                snapshot_epoch,
                mid_e4,
                lag(t_us) OVER w AS prev_t_us,
                lag(snapshot_epoch) OVER w AS prev_epoch,
                lag(mid_e4) OVER w AS prev_mid
            FROM valid
            WINDOW w AS (
                PARTITION BY market_ticker
                ORDER BY t_us, ws_sid, ws_seq
            )
        )
        SELECT market_ticker, prev_t_us, t_us
        FROM laggy
        WHERE prev_mid IS NOT NULL
          AND prev_epoch = snapshot_epoch
          AND abs(mid_e4 - prev_mid) >= {MIN_TICK_E4}
        ORDER BY market_ticker, t_us
    """


def _first_valid_sql(l2_paths: Sequence[Path], date: str) -> str:
    return f"""
        SELECT market_ticker, min(CAST(t_us AS BIGINT)) AS first_valid_t
        FROM read_parquet({_path_list(l2_paths)})
        WHERE CAST(date AS VARCHAR) = {_quote(date)}
          AND book_valid = TRUE
          AND classification = 'DELTA_APPLIED'
          AND topology = 'TWO_SIDED'
          AND mid_e4 IS NOT NULL
          AND t_us IS NOT NULL
          AND snapshot_epoch IS NOT NULL
        GROUP BY market_ticker
    """


def _accepted_leader_events(
    change_events: Mapping[str, List[int]],
    families: Mapping[str, List[str]],
) -> Tuple[Dict[str, List[Tuple[int, str]]], int, int]:
    """Family-ordered, burst-deduped, simultaneity-excluded leader events."""
    accepted: Dict[str, List[Tuple[int, str]]] = {}
    ambiguous = 0
    raw_total = 0
    for family, legs in families.items():
        merged: List[Tuple[int, str]] = []
        for leg in legs:
            merged.extend((t, leg) for t in change_events.get(leg, ()))
        merged.sort()
        raw_total += len(merged)
        times = [t for t, _leg in merged]
        keep: List[Tuple[int, str]] = []
        skip_times = {
            t
            for index, t in enumerate(times)
            if (index > 0 and times[index - 1] == t)
            or (index + 1 < len(times) and times[index + 1] == t)
        }
        ambiguous += sum(1 for t, _leg in merged if t in skip_times)
        last_accepted: Optional[int] = None
        for t, leg in merged:
            if t in skip_times:
                continue
            if last_accepted is not None and t - last_accepted < BURST_DEDUP_US:
                continue
            keep.append((t, leg))
            last_accepted = t
        if keep:
            accepted[family] = keep
    return accepted, ambiguous, raw_total


def _has_event_in_window(
    events: Sequence[int], start_exclusive: int, end_inclusive: int
) -> bool:
    index = bisect.bisect_right(events, start_exclusive)
    return index < len(events) and events[index] <= end_inclusive


def _has_event_in_pre_window(
    events: Sequence[int], start_inclusive: int, end_exclusive: int
) -> bool:
    index = bisect.bisect_left(events, start_inclusive)
    return index < len(events) and events[index] < end_exclusive


def _measure_date(
    con: Any,
    l2_paths: Sequence[Path],
    date: str,
    families: Mapping[str, List[str]],
) -> Dict[str, Any]:
    pair_rows = con.execute(_change_pairs_sql(l2_paths, date)).fetchall()
    forward: Dict[str, List[int]] = {}
    for market_ticker, _prev_t, t in pair_rows:
        forward.setdefault(str(market_ticker), []).append(int(t))
    first_valid = {
        str(market): int(t)
        for market, t in con.execute(_first_valid_sql(l2_paths, date)).fetchall()
    }
    leg_pool = sorted(
        leg for legs in families.values() for leg in legs if leg in first_valid
    )
    leg_family = {
        leg: family for family, legs in families.items() for leg in legs
    }

    accepted, ambiguous, raw_total = _accepted_leader_events(forward, families)

    rng = random.Random(f"{PROXY_SEED}:{date}")
    horizons: Dict[int, Dict[str, Dict[str, List[float]]]] = {
        horizon: {"real": {}, "reversed": {}, "proxy": {}}
        for horizon in HORIZONS_US
    }
    no_book_excluded = 0

    def _observe(
        arm: str,
        horizon: int,
        family: str,
        leader_events: Sequence[Tuple[int, str]],
        sibling_of: Any,
        *,
        pre_window: bool = False,
    ) -> None:
        trials = 0
        reactions = 0
        for t, leader_leg in leader_events:
            for sibling in sibling_of(family, leader_leg, t):
                trials += 1
                sibling_events = forward.get(sibling, ())
                if pre_window:
                    hit = _has_event_in_pre_window(
                        sibling_events, t - horizon, t
                    )
                else:
                    hit = _has_event_in_window(sibling_events, t, t + horizon)
                if hit:
                    reactions += 1
        if trials:
            bucket = horizons[horizon][arm]
            bucket.setdefault(family, []).append(reactions / trials)

    # Sibling resolution happens exactly once per accepted leader event so
    # the no_book_excluded diagnostic counts each exclusion exactly once.
    sibling_cache: Dict[Tuple[str, int, str], List[str]] = {}
    for family, leader_events in sorted(accepted.items()):
        for t, leader_leg in leader_events:
            picked = []
            for sibling in families[family]:
                if sibling == leader_leg:
                    continue
                first = first_valid.get(sibling)
                if first is None or first > t:
                    no_book_excluded += 1
                    continue
                picked.append(sibling)
            sibling_cache[(family, t, leader_leg)] = picked

    def _real_siblings(family: str, leader_leg: str, t: int) -> List[str]:
        return sibling_cache[(family, t, leader_leg)]

    proxy_pairings = 0
    for family, leader_events in sorted(accepted.items()):
        for horizon in HORIZONS_US:
            _observe("real", horizon, family, leader_events, _real_siblings)
            # Mirrored pre-event window: same leader events, same siblings,
            # window [t - H, t).  Causal lead-lag is pre-quiet.
            _observe(
                "reversed",
                horizon,
                family,
                leader_events,
                _real_siblings,
                pre_window=True,
            )
    for family, leader_events in sorted(accepted.items()):
        pseudo_map: Dict[Tuple[int, str], List[str]] = {}
        for t, leader_leg in leader_events:
            k = len(_real_siblings(family, leader_leg, t))
            candidates = [
                leg
                for leg in leg_pool
                if leg_family[leg] != family and first_valid[leg] <= t
            ]
            if k == 0 or len(candidates) < k:
                continue
            pseudo_map[(t, leader_leg)] = rng.sample(candidates, k)
            proxy_pairings += 1

        def _proxy_siblings(_family: str, leader_leg: str, t: int) -> List[str]:
            return pseudo_map.get((t, leader_leg), [])

        for horizon in HORIZONS_US:
            _observe("proxy", horizon, family, leader_events, _proxy_siblings)

    family_rates: Dict[int, Dict[str, Dict[str, float]]] = {}
    for horizon, arms in horizons.items():
        family_rates[horizon] = {
            arm: {
                family: sum(values) / len(values)
                for family, values in buckets.items()
            }
            for arm, buckets in arms.items()
        }
    return {
        "date": date,
        "families_in_scope": len(families),
        "legs_in_scope": sum(len(legs) for legs in families.values()),
        "raw_family_change_events": raw_total,
        "accepted_leader_events": {
            family: len(events) for family, events in accepted.items()
        },
        "ambiguous_simultaneous": ambiguous,
        "no_book_excluded": no_book_excluded,
        "proxy_pairings": proxy_pairings,
        "family_rates": family_rates,
    }


def _cluster_bootstrap_diff(
    real: Mapping[str, float],
    control: Mapping[str, float],
    seed_tag: str,
) -> Optional[Dict[str, Any]]:
    families = sorted(set(real) & set(control))
    if not families:
        return None
    diffs = [real[f] - control[f] for f in families]
    point = sum(diffs) / len(diffs)
    rng = random.Random(f"{BOOTSTRAP_SEED}:{seed_tag}")
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = [diffs[rng.randrange(len(diffs))] for _ in diffs]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.025 * (BOOTSTRAP_RESAMPLES - 1))]
    hi = means[int(0.975 * (BOOTSTRAP_RESAMPLES - 1))]
    return {
        "families": len(families),
        "mean_diff": point,
        "ci95": [lo, hi],
        "excludes_zero": bool(lo > 0.0 or hi < 0.0),
    }


def _fit_admission(train_results: Sequence[Mapping[str, Any]]) -> List[str]:
    per_date_counts = [result["accepted_leader_events"] for result in train_results]
    admitted = None
    for counts in per_date_counts:
        eligible = {
            family
            for family, count in counts.items()
            if count >= MIN_TRAIN_LEADER_EVENTS
        }
        admitted = eligible if admitted is None else (admitted & eligible)
    return sorted(admitted or ())


def _restrict_rates(
    result: Mapping[str, Any], admitted: Sequence[str]
) -> Dict[str, Any]:
    admitted_set = set(admitted)
    restricted: Dict[str, Any] = {}
    for horizon, arms in result["family_rates"].items():
        restricted[horizon] = {
            arm: {
                family: rate
                for family, rate in rates.items()
                if family in admitted_set
            }
            for arm, rates in arms.items()
        }
    return restricted


def _decide(validate_summary: Mapping[int, Mapping[str, Any]]) -> Dict[str, Any]:
    primary = validate_summary.get(PRIMARY_HORIZON_US, {})
    secondary = validate_summary.get(SECONDARY_HORIZON_US, {})

    def _passes(block: Mapping[str, Any], strict: bool) -> bool:
        for control in ("vs_reversed", "vs_proxy"):
            stats = block.get(control)
            if not stats:
                return False
            if stats["mean_diff"] <= 0.0:
                return False
            if strict and not stats["excludes_zero"]:
                return False
            if not strict and stats["ci95"][1] < 0.0:
                return False
        return True

    survives = _passes(primary, strict=True) and _passes(secondary, strict=False)
    return {
        "survives_defensive_signal": bool(survives),
        "rule": (
            "primary 100ms: real-minus-control mean > 0 with cluster-bootstrap "
            "95% CI excluding zero for BOTH controls; secondary 1s: "
            "directionally positive and its CI must not exclude zero from "
            "the negative side"
        ),
    }


def run_event_study(
    checkpoint_namespace: Path,
    market_graph_path: Path,
    output_dir: Path,
    *,
    verify_payload_hashes: bool = False,
) -> Dict[str, Any]:
    checkpoint_namespace = Path(checkpoint_namespace).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prereg = _prereg_binding()
    graph = _load_market_graph(Path(market_graph_path).resolve())
    stage = _load_stage(
        checkpoint_namespace,
        "l2_replay",
        L2_REQUIRED_COLUMNS,
        verify_payload_hashes,
    )

    con = duckdb.connect()
    con.execute(f"SET memory_limit={_quote(MEMORY_LIMIT)}")
    con.execute(f"SET threads={THREADS}")

    date_results: Dict[str, Dict[str, Any]] = {}
    for date in ALL_DATES:
        partitions = _selected_partitions(stage, date)
        _verify_partition_payloads(partitions)
        families = _families_for_date(con, graph["path"], date)
        if not families:
            raise H5StudyError(f"no canonical multi-leg families on {date}")
        if date in VALIDATE_DATES and MODEL_SEAL_NAME not in {
            p.name for p in output_dir.iterdir()
        }:
            raise H5StudyError(
                "VALIDATE ordering violation: model seal must exist first"
            )
        date_results[date] = _measure_date(
            con, [p["path"] for p in partitions], date, families
        )
        if date == TRAIN_DATES[-1]:
            admitted = _fit_admission(
                [date_results[d] for d in TRAIN_DATES]
            )
            if not admitted:
                raise H5StudyError(
                    "no family satisfies TRAIN-only admission; refusing to "
                    "read VALIDATE"
                )
            model = {
                "schema_version": MODEL_SCHEMA_VERSION,
                "train_dates": list(TRAIN_DATES),
                "admitted_families": admitted,
                "constants": {
                    "min_tick_e4": MIN_TICK_E4,
                    "burst_dedup_us": BURST_DEDUP_US,
                    "min_train_leader_events": MIN_TRAIN_LEADER_EVENTS,
                    "min_family_legs": MIN_FAMILY_LEGS,
                    "horizons_us": list(HORIZONS_US),
                    "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
                    "bootstrap_seed": BOOTSTRAP_SEED,
                    "proxy_seed": PROXY_SEED,
                },
                "preregistration": prereg,
                "market_graph_sha256": graph["sha256"],
                "source_binding": stage["source_binding"],
            }
            model_bytes = _canonical_bytes(model)
            model["model_sha256"] = _sha256_bytes(model_bytes)
            _atomic_write(
                output_dir / MODEL_SEAL_NAME,
                json.dumps(model, indent=1, sort_keys=True).encode("ascii"),
            )

    seal_raw = (output_dir / MODEL_SEAL_NAME).read_bytes()
    seal = json.loads(seal_raw)
    admitted = seal["admitted_families"]

    summaries: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for date, result in date_results.items():
        restricted = _restrict_rates(result, admitted)
        date_summary: Dict[int, Dict[str, Any]] = {}
        for horizon in HORIZONS_US:
            arms = restricted[horizon]
            date_summary[horizon] = {
                "families_with_real_trials": len(arms["real"]),
                "mean_real_rate": (
                    sum(arms["real"].values()) / len(arms["real"])
                    if arms["real"]
                    else None
                ),
                "vs_reversed": _cluster_bootstrap_diff(
                    arms["real"], arms["reversed"], f"{date}:{horizon}:rev"
                ),
                "vs_proxy": _cluster_bootstrap_diff(
                    arms["real"], arms["proxy"], f"{date}:{horizon}:proxy"
                ),
            }
        summaries[date] = date_summary

    decision = _decide(summaries[VALIDATE_DATES[0]])

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "module_sha256": _module_sha256(),
        "preregistration": prereg,
        "market_graph": {
            "path": str(graph["path"]),
            "sha256": graph["sha256"],
        },
        "checkpoint": {
            "namespace": str(checkpoint_namespace),
            "source_binding": stage["source_binding"],
            "l2_manifest_sha256": stage["manifest_sha256"],
        },
        "unrelated_root_control_authenticated": False,
        "unrelated_root_control_label": "DIFFERENT_EVENT_TICKER_PROXY",
        "train_dates": list(TRAIN_DATES),
        "validate_dates": list(VALIDATE_DATES),
        "admitted_families": admitted,
        "model": {"path": MODEL_SEAL_NAME, "model_sha256": seal["model_sha256"]},
        "diagnostics": {
            date: {
                key: value
                for key, value in result.items()
                if key != "family_rates"
            }
            for date, result in date_results.items()
        },
        "summaries": {
            date: {str(h): block for h, block in summary.items()}
            for date, summary in summaries.items()
        },
        "decision": decision,
        "claims": {
            "cash_pnl": False,
            "arbitrage": False,
            "payout_exhaustiveness": False,
            "fees_fills_latency": False,
            "defensive_lead_lag_only": True,
        },
    }
    report = _render_markdown(receipt)
    _atomic_write(output_dir / REPORT_NAME, report.encode("utf-8"))
    receipt["report"] = {
        "path": REPORT_NAME,
        "sha256": _sha256_bytes(report.encode("utf-8")),
    }
    receipt_bytes = json.dumps(receipt, indent=1, sort_keys=True).encode("ascii")
    _atomic_write(output_dir / RECEIPT_NAME, receipt_bytes)
    return receipt


def _render_markdown(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# H5 Cross-Market Deviation — Defensive Lead/Lag Result",
        "",
        f"Schema: `{receipt['schema_version']}`",
        "Cash-PnL / arbitrage / fee / fill / latency claims: **none made**.",
        "Unrelated-root control: `DIFFERENT_EVENT_TICKER_PROXY` "
        "(`unrelated_root_control_authenticated=false`).",
        "",
        f"Preregistration: `{receipt['preregistration']['path']}` "
        f"(`{receipt['preregistration']['sha256'][:16]}…`)",
        f"Market graph: `{receipt['market_graph']['sha256'][:16]}…`; "
        f"checkpoint binding `{receipt['checkpoint']['source_binding'][:16]}…`",
        "",
        f"Admitted families (TRAIN-only): "
        f"{len(receipt['admitted_families'])}",
        "",
        "| Date | Horizon | Fam(real) | Mean real rate | Δ vs reversed "
        "[95% CI] | Δ vs proxy [95% CI] |",
        "|---|---|---:|---:|---|---|",
    ]

    def _fmt(stats: Optional[Mapping[str, Any]]) -> str:
        if not stats:
            return "n/a"
        lo, hi = stats["ci95"]
        star = "*" if stats["excludes_zero"] else ""
        return f"{stats['mean_diff']:+.4f} [{lo:+.4f}, {hi:+.4f}]{star}"

    for date, summary in receipt["summaries"].items():
        for horizon, block in summary.items():
            mean_rate = block["mean_real_rate"]
            lines.append(
                f"| {date} | {int(horizon) / 1000:g}ms | "
                f"{block['families_with_real_trials']} | "
                f"{'n/a' if mean_rate is None else f'{mean_rate:.4f}'} | "
                f"{_fmt(block['vs_reversed'])} | {_fmt(block['vs_proxy'])} |"
            )
    decision = receipt["decision"]
    lines += [
        "",
        f"## Decision: "
        f"{'SURVIVES (defensive signal)' if decision['survives_defensive_signal'] else 'KILLED (defensive signal)'}",
        "",
        decision["rule"],
        "",
        "Survival only admits H5 to the shared exact-fee PnL-spine pipeline; "
        "it is not a trading claim.",
    ]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the offline H5 cross-market deviation event study"
    )
    parser.add_argument("--checkpoint-namespace", type=Path, required=True)
    parser.add_argument(
        "--market-graph",
        type=Path,
        required=True,
        help="sealed MARKET_GRAPH.parquet (byte hash must match the prereg)",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify-payload-hashes", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_event_study(
            args.checkpoint_namespace,
            args.market_graph,
            args.output_dir,
            verify_payload_hashes=args.verify_payload_hashes,
        )
    except (H5StudyError, OSError, duckdb.Error) as exc:
        print(f"H5_EVENT_STUDY_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        "H5_EVENT_STUDY_COMPLETE "
        f"receipt={Path(args.output_dir).resolve() / RECEIPT_NAME} "
        f"survives={receipt['decision']['survives_defensive_signal']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
