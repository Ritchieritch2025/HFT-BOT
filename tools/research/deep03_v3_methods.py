#!/usr/bin/env python3
"""Corrected D3-W2A descriptive methods for exact V3 Sports facts.

The four methods are intentionally narrow:

* D3-B01-MARKOUT uses only receive-clock-past books and strictly future books;
* D3-B02-ONESIDE ends state at update/TTL/gap/terminal and right-censors tails;
* D3-B03-XMKT refuses a residual without exhaustive payouts, fees and fills;
* D3-B04-RHYTHM uses active market-minute/event-minute denominators.

No method computes strategy PnL or emits a candidate verdict.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


METHODS = (
    "D3-B01-MARKOUT",
    "D3-B02-ONESIDE",
    "D3-B03-XMKT",
    "D3-B04-RHYTHM",
)
HORIZONS_US = (1_000_000, 5_000_000, 30_000_000)
MAX_BOOK_AGE_US = 5_000_000
BOOK_TTL_US = 60_000_000
L1_INTERVAL_COLUMNS = (
    "date",
    "t_us",
    "market_ticker",
    "event_proxy",
    "sport",
    "book_state",
    "right_censored",
    "starts_in_gap",
    "interval_end_us",
    "duration_us",
    "phase",
)
TRADE_DEDUP_BUCKETS = 32
TRADE_DUPLICATE_FASTPATH_MAX_ROWS = 1_000_000
TRADE_DEDUP_COLUMNS = (
    "date",
    "t_us",
    "market_ticker",
    "event_proxy",
    "sport",
    "trade_id",
    "yes_price_e4",
    "count_e4",
    "taker_side",
    "occurrence_us",
)
BOUNDED_CHECKPOINT_SCHEMA = "deep03-bounded-checkpoint-v1"
BOUNDED_STAGE_MANIFEST_SCHEMA = "deep03-bounded-stage-manifest-v1"
BOUNDED_CHECKPOINT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.=-]{0,199}$")


def _canonical_json_bytes(value: Any) -> bytes:
    """Return the single byte representation used by bounded receipts."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    """Durably replace a metadata file; incomplete bytes are never visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def bounded_source_binding(input_manifest: dict[str, Any]) -> str:
    """Bind checkpoints to exact releases and exact immutable source objects.

    Local cache paths are deliberately excluded: moving a byte-identical,
    exact-version cache does not change the research input.  Every immutable
    identity field that can change the facts remains included.
    """
    objects = []
    for obj in input_manifest.get("objects") or []:
        objects.append(
            {
                "release_id": obj.get("release_id"),
                "date": obj.get("date"),
                "kind": obj.get("kind"),
                "channel": obj.get("channel"),
                "logical_key": obj.get("logical_key"),
                "source_version_id": obj.get("source_version_id"),
                "sha256": obj.get("sha256"),
                "row_count": obj.get("row_count"),
            }
        )
    payload = {
        "release_ids": list(input_manifest.get("release_ids") or []),
        "release_dates": list(input_manifest.get("release_dates") or []),
        "objects": sorted(
            objects,
            key=lambda row: (
                str(row["release_id"]),
                str(row["logical_key"]),
                str(row["source_version_id"]),
            ),
        ),
    }
    return _sha256_bytes(_canonical_json_bytes(payload))


class BoundedCheckpointStore:
    """Fail-closed durable Parquet checkpoints for bounded Deep03 stages.

    A partition becomes reusable only after both its Parquet payload and its
    atomic COMPLETE receipt exist and independently verify.  A data file left
    without a receipt is an unpublished crash remnant and is recomputed.  A
    receipt whose bytes, schema, count, stage version, or source binding drift
    is evidence corruption and is never silently repaired.
    """

    def __init__(self, root: Path, source_binding: str):
        self.root = Path(root).resolve()
        if not re.fullmatch(r"[0-9a-f]{64}", source_binding):
            raise ValueError("bounded checkpoint source binding must be SHA-256")
        self.source_binding = source_binding
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".partial").mkdir(exist_ok=True)

    @staticmethod
    def _key(value: str, label: str) -> str:
        if not isinstance(value, str) or BOUNDED_CHECKPOINT_KEY_RE.fullmatch(value) is None:
            raise ValueError(f"invalid bounded checkpoint {label}: {value!r}")
        return value

    def _paths(self, stage: str, partition_key: str) -> tuple[Path, Path]:
        stage = self._key(stage, "stage")
        partition_key = self._key(partition_key, "partition key")
        base = self.root / stage
        return (
            base / "data" / f"{partition_key}.parquet",
            base / "receipts" / f"{partition_key}.json",
        )

    def _read_receipt(self, receipt_path: Path) -> dict[str, Any]:
        try:
            raw = receipt_path.read_bytes()
            receipt = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"bounded checkpoint receipt unreadable: {receipt_path}: {exc}"
            ) from exc
        if raw != _canonical_json_bytes(receipt):
            raise RuntimeError(
                f"bounded checkpoint receipt is not canonical: {receipt_path}"
            )
        return receipt

    def validate_partition(
        self,
        con,
        *,
        stage: str,
        stage_version: str,
        partition_key: str,
    ) -> dict[str, Any]:
        data_path, receipt_path = self._paths(stage, partition_key)
        if not receipt_path.is_file():
            raise FileNotFoundError(receipt_path)
        receipt = self._read_receipt(receipt_path)
        expected = {
            "schema_version": BOUNDED_CHECKPOINT_SCHEMA,
            "state": "COMPLETE",
            "stage": stage,
            "stage_version": stage_version,
            "partition_key": partition_key,
            "source_binding": self.source_binding,
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise RuntimeError(
                    "bounded checkpoint receipt binding mismatch: "
                    f"{partition_key}: {field}"
                )
        data = receipt.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"bounded checkpoint data receipt missing: {partition_key}")
        expected_relative = data_path.relative_to(self.root).as_posix()
        if data.get("path") != expected_relative or not data_path.is_file():
            raise RuntimeError(f"bounded checkpoint payload missing: {partition_key}")
        size = data_path.stat().st_size
        if data.get("size_bytes") != size or data.get("sha256") != _sha256_path(data_path):
            raise RuntimeError(f"bounded checkpoint payload hash mismatch: {partition_key}")
        relation = f"read_parquet({quote(data_path)},hive_partitioning=false)"
        schema = [
            {"name": row[0], "type": row[1]}
            for row in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
        ]
        if data.get("schema") != schema:
            raise RuntimeError(f"bounded checkpoint payload schema mismatch: {partition_key}")
        row_count = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
        if data.get("row_count") != row_count:
            raise RuntimeError(f"bounded checkpoint payload row-count mismatch: {partition_key}")
        return receipt

    def write_partition(
        self,
        con,
        *,
        stage: str,
        stage_version: str,
        partition_key: str,
        select_sql: str,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Write or verify one partition; return ``(receipt, reused)``."""
        if not isinstance(stage_version, str) or not stage_version:
            raise ValueError("bounded checkpoint stage version is empty")
        data_path, receipt_path = self._paths(stage, partition_key)
        if receipt_path.exists():
            return (
                self.validate_partition(
                    con,
                    stage=stage,
                    stage_version=stage_version,
                    partition_key=partition_key,
                ),
                True,
            )
        data_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        # A crash may have promoted data just before its receipt.  It was
        # never COMPLETE, so deleting and deterministically recomputing it is
        # the only permitted recovery.
        if data_path.exists():
            data_path.unlink()
        temporary = self.root / ".partial" / f"{stage}-{partition_key}-{uuid.uuid4().hex}.parquet"
        try:
            con.execute(
                f"COPY ({select_sql}) TO {quote(temporary)} "
                "(FORMAT PARQUET,COMPRESSION ZSTD)"
            )
            return (
                self._publish_temporary(
                    con,
                    stage=stage,
                    stage_version=stage_version,
                    partition_key=partition_key,
                    temporary=temporary,
                    metrics=metrics,
                ),
                False,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    def _publish_temporary(
        self,
        con,
        *,
        stage: str,
        stage_version: str,
        partition_key: str,
        temporary: Path,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Promote an already-written Parquet file and publish its receipt."""
        data_path, receipt_path = self._paths(stage, partition_key)
        if receipt_path.exists():
            raise RuntimeError(
                f"bounded checkpoint receipt appeared during publish: {partition_key}"
            )
        data_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        relation = f"read_parquet({quote(temporary)},hive_partitioning=false)"
        schema = [
            {"name": row[0], "type": row[1]}
            for row in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
        ]
        row_count = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
        payload_sha256 = _sha256_path(temporary)
        size_bytes = temporary.stat().st_size
        if data_path.exists():
            data_path.unlink()
        os.replace(temporary, data_path)
        receipt = {
            "schema_version": BOUNDED_CHECKPOINT_SCHEMA,
            "state": "COMPLETE",
            "stage": stage,
            "stage_version": stage_version,
            "partition_key": partition_key,
            "source_binding": self.source_binding,
            "data": {
                "path": data_path.relative_to(self.root).as_posix(),
                "sha256": payload_sha256,
                "size_bytes": size_bytes,
                "row_count": row_count,
                "schema": schema,
            },
            "metrics": metrics or {},
        }
        _atomic_replace_bytes(receipt_path, _canonical_json_bytes(receipt))
        return self.validate_partition(
            con,
            stage=stage,
            stage_version=stage_version,
            partition_key=partition_key,
        )

    def write_partitioned_query(
        self,
        con,
        *,
        stage: str,
        stage_version: str,
        partition_column: str,
        partition_keys: dict[int, str],
        select_sql: str,
    ) -> dict[str, tuple[dict[str, Any], bool]]:
        """Scan one source query once and checkpoint every physical shard.

        ``select_sql`` must return the integer ``partition_column`` plus the
        narrow payload columns.  DuckDB's partitioned COPY performs the
        scatter in one query.  The partition column is encoded by the
        directory and intentionally excluded from each Parquet payload.
        """
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", partition_column) is None:
            raise ValueError("invalid bounded partition column")
        if not partition_keys or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in partition_keys
        ):
            raise ValueError("bounded physical partition values must be integers")
        normalized = {
            value: self._key(key, "partition key")
            for value, key in partition_keys.items()
        }
        if len(normalized) != len(set(normalized.values())):
            raise ValueError("bounded physical partition keys are duplicated")

        result: dict[str, tuple[dict[str, Any], bool]] = {}
        missing: dict[int, str] = {}
        for value, key in sorted(normalized.items()):
            _data_path, receipt_path = self._paths(stage, key)
            if receipt_path.exists():
                result[key] = (
                    self.validate_partition(
                        con,
                        stage=stage,
                        stage_version=stage_version,
                        partition_key=key,
                    ),
                    True,
                )
            else:
                missing[value] = key
        if not missing:
            return result

        generated = self.root / ".partial" / (
            f"scatter-{stage}-{uuid.uuid4().hex}"
        )
        try:
            con.execute(
                f"COPY ({select_sql}) TO {quote(generated)} "
                f"(FORMAT PARQUET,COMPRESSION ZSTD,PARTITION_BY ({partition_column}))"
            )
            for value, key in sorted(missing.items()):
                partition_dir = generated / f"{partition_column}={value}"
                files = sorted(partition_dir.glob("*.parquet"))
                if len(files) == 1:
                    temporary = files[0]
                else:
                    temporary = self.root / ".partial" / (
                        f"consolidate-{stage}-{key}-{uuid.uuid4().hex}.parquet"
                    )
                    if files:
                        con.execute(
                            f"COPY (SELECT * FROM read_parquet({path_list(files)})) "
                            f"TO {quote(temporary)} (FORMAT PARQUET,COMPRESSION ZSTD)"
                        )
                    else:
                        # The expected partition is empty.  LIMIT 0 preserves
                        # the exact payload schema without rescanning facts.
                        con.execute(
                            f"COPY (SELECT * EXCLUDE ({partition_column}) "
                            f"FROM ({select_sql}) WHERE false) TO {quote(temporary)} "
                            "(FORMAT PARQUET,COMPRESSION ZSTD)"
                        )
                receipt = self._publish_temporary(
                    con,
                    stage=stage,
                    stage_version=stage_version,
                    partition_key=key,
                    temporary=temporary,
                )
                result[key] = (receipt, False)
                if temporary.exists():
                    temporary.unlink()
        finally:
            if generated.exists():
                shutil.rmtree(generated)
        return result

    def finalize_stage(
        self,
        con,
        *,
        stage: str,
        stage_version: str,
        partition_keys: Iterable[str],
    ) -> Path:
        """Publish a canonical complete-set manifest for exactly these keys."""
        stage = self._key(stage, "stage")
        keys = sorted(self._key(value, "partition key") for value in partition_keys)
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("bounded stage partition set is empty or duplicated")
        receipt_dir = self.root / stage / "receipts"
        actual = sorted(path.stem for path in receipt_dir.glob("*.json"))
        if actual != keys:
            raise RuntimeError(
                f"bounded stage receipt set mismatch: {stage}: expected={keys} actual={actual}"
            )
        partitions = []
        for key in keys:
            receipt = self.validate_partition(
                con,
                stage=stage,
                stage_version=stage_version,
                partition_key=key,
            )
            receipt_path = self._paths(stage, key)[1]
            partitions.append(
                {
                    "partition_key": key,
                    "receipt_path": receipt_path.relative_to(self.root).as_posix(),
                    "receipt_sha256": _sha256_path(receipt_path),
                    "data_sha256": receipt["data"]["sha256"],
                    "row_count": receipt["data"]["row_count"],
                }
            )
        manifest = {
            "schema_version": BOUNDED_STAGE_MANIFEST_SCHEMA,
            "state": "COMPLETE",
            "stage": stage,
            "stage_version": stage_version,
            "source_binding": self.source_binding,
            "partition_count": len(partitions),
            "row_count": sum(row["row_count"] for row in partitions),
            "partitions": partitions,
        }
        manifest_path = self.root / stage / "MANIFEST.json"
        payload = _canonical_json_bytes(manifest)
        if manifest_path.exists() and manifest_path.read_bytes() != payload:
            raise RuntimeError(f"bounded immutable stage manifest drift: {stage}")
        _atomic_replace_bytes(manifest_path, payload)
        _atomic_replace_bytes(
            self.root / stage / "MANIFEST.sha256",
            (_sha256_bytes(payload) + "  MANIFEST.json\n").encode("ascii"),
        )
        return manifest_path


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def rows_as_dicts(con, sql: str) -> list[dict[str, Any]]:
    cursor = con.execute(sql)
    names = [item[0] for item in cursor.description]
    return [
        {name: _json_value(value) for name, value in zip(names, row)}
        for row in cursor.fetchall()
    ]


def scalar(con, sql: str, default: Any = 0) -> Any:
    row = con.execute(sql).fetchone()
    return default if not row or row[0] is None else _json_value(row[0])


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def path_list(paths: Iterable[Path]) -> str:
    values = list(paths)
    if not values:
        raise ValueError("empty source path list")
    return "[" + ",".join(quote(path) for path in values) + "]"


def _relation_sql(paths: list[Path]) -> str:
    parquet = [path for path in paths if path.name.endswith(".parquet")]
    csvs = [path for path in paths if not path.name.endswith(".parquet")]
    parts: list[str] = []
    if parquet:
        parts.append(
            "SELECT * FROM read_parquet(%s,union_by_name=true,"
            "hive_partitioning=true)" % path_list(parquet)
        )
    if csvs:
        parts.append(
            "SELECT * FROM read_csv(%s,header=true,union_by_name=true,"
            "hive_partitioning=true,all_varchar=true)" % path_list(csvs)
        )
    if not parts:
        raise ValueError("no readable fact files")
    return " UNION ALL BY NAME ".join(parts)


def _columns(con, relation: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            f"DESCRIBE SELECT * FROM {relation}"
        ).fetchall()
    }


def _expr(
    columns: set[str], candidates: tuple[str, ...], alias: str, sql_type: str
) -> str:
    available = [f'"{name}"' for name in candidates if name in columns]
    if not available:
        base = "NULL"
    elif len(available) == 1:
        base = available[0]
    else:
        base = "coalesce(" + ",".join(available) + ")"
    return f'try_cast({base} AS {sql_type}) AS "{alias}"'


def _fact_objects(input_manifest: dict[str, Any], channel: str) -> list[dict[str, Any]]:
    return [
        obj
        for obj in input_manifest.get("objects") or []
        if obj.get("kind") == "facts"
        and obj.get("channel") == channel
        and "/category=Sports/" in str(obj.get("logical_key") or "")
    ]


def _kind_objects(input_manifest: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        obj
        for obj in input_manifest.get("objects") or []
        if obj.get("kind") == kind
    ]


def _object_provenance(
    input_manifest: dict[str, Any], *, channels: set[str], kinds: set[str] | None = None
) -> list[dict[str, Any]]:
    kinds = kinds or set()
    rows = []
    for obj in input_manifest.get("objects") or []:
        if obj.get("channel") not in channels and obj.get("kind") not in kinds:
            continue
        rows.append(
            {
                "release_id": obj["release_id"],
                "logical_key": obj["logical_key"],
                "source_version_id": obj["source_version_id"],
                "sha256": obj["sha256"],
                "row_count": obj.get("row_count"),
            }
        )
    return rows


def _phase_sql(alias: str) -> str:
    return (
        f"CASE WHEN {alias}.occurrence_us IS NULL THEN 'UNKNOWN_PHASE' "
        f"WHEN {alias}.t_us < {alias}.occurrence_us THEN 'PRE_SCHEDULED_START' "
        "ELSE 'POST_SCHEDULED_START_PROXY' END"
    )


def _mid_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,cast({column} AS DOUBLE)))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"(({logit(bid)})+({logit(ask)}))/2.0"


def _spread_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,cast({column} AS DOUBLE)))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"({logit(ask)})-({logit(bid)})"


def _l1_asof_ambiguity_sql(relation: str = "l1_enriched") -> str:
    """Return the fail-closed QC for B01's receive-time ASOF key.

    DuckDB ASOF chooses one row when multiple right-hand rows share the same
    ordering key.  The legacy B01 estimand orders that side only by ``t_us``;
    receive-wall/mono/sequence fields therefore cannot be used as an
    undisclosed winner correction.  Exact duplicate economic states are safe,
    while different usable quotes at one key are ambiguous and must stop the
    run before any markout is claimed.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", relation) is None:
        raise ValueError("invalid L1 ASOF QC relation")
    return f"""
      WITH keyed AS (
        SELECT date,market_ticker,t_us,count(*) AS raw_rows,
               count(DISTINCT struct_pack(
                 book_state:=book_state,
                 yes_bid_e4:=yes_bid_e4,
                 yes_ask_e4:=yes_ask_e4
               )) AS economic_variants
        FROM {relation}
        WHERE date IS NOT NULL AND market_ticker IS NOT NULL AND t_us IS NOT NULL
        GROUP BY date,market_ticker,t_us
      )
      SELECT count(*) FILTER (WHERE economic_variants>1) AS ambiguous_keys,
             coalesce(sum(raw_rows) FILTER (WHERE economic_variants>1),0)
               AS ambiguous_rows,
             count(*) FILTER (WHERE raw_rows>1 AND economic_variants=1)
               AS safe_duplicate_keys,
             coalesce(sum(raw_rows-1) FILTER (
               WHERE raw_rows>1 AND economic_variants=1
             ),0) AS safe_duplicate_excess_rows
      FROM keyed
    """


def _assert_l1_asof_timestamps_unambiguous(
    con, relation: str = "l1_enriched", *, marker: str = "global"
) -> dict[str, int]:
    """Record and enforce deterministic B01 ASOF economics at equal ``t_us``."""
    row = con.execute(_l1_asof_ambiguity_sql(relation)).fetchone()
    profile = {
        "ambiguous_keys": int(row[0]),
        "ambiguous_rows": int(row[1]),
        "safe_duplicate_keys": int(row[2]),
        "safe_duplicate_excess_rows": int(row[3]),
    }
    print(
        "D3_W2A_QC l1_asof_same_timestamp "
        f"partition={marker} "
        f"ambiguous_keys={profile['ambiguous_keys']} "
        f"ambiguous_rows={profile['ambiguous_rows']} "
        f"safe_duplicate_keys={profile['safe_duplicate_keys']} "
        "safe_duplicate_excess_rows="
        f"{profile['safe_duplicate_excess_rows']}",
        flush=True,
    )
    if profile["ambiguous_keys"]:
        raise RuntimeError(
            "B01 same-timestamp L1 ASOF ambiguity: "
            f"partition={marker} "
            f"ambiguous_keys={profile['ambiguous_keys']} "
            f"ambiguous_rows={profile['ambiguous_rows']}"
        )
    return profile


def _canonical_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Deep03 release date must be a canonical ISO string")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid Deep03 release date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"non-canonical Deep03 release date: {value!r}")
    return value


def _manifest_release_dates(input_manifest: dict[str, Any]) -> tuple[str, ...]:
    """Return the manifest-bound dates in deterministic chronological order."""
    raw_dates = input_manifest.get("release_dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise ValueError("INPUT_MANIFEST release_dates is missing or empty")
    dates = tuple(_canonical_date(value) for value in raw_dates)
    if len(dates) != len(set(dates)):
        raise ValueError("INPUT_MANIFEST release_dates contains a duplicate date")

    releases = input_manifest.get("releases")
    if not isinstance(releases, list) or len(releases) != len(dates):
        raise ValueError("INPUT_MANIFEST release summaries do not bind release_dates")
    summary_dates = tuple(
        _canonical_date(release.get("date"))
        for release in releases
        if isinstance(release, dict)
    )
    if len(summary_dates) != len(dates) or set(summary_dates) != set(dates):
        raise ValueError("INPUT_MANIFEST release summary dates differ from release_dates")

    l1_dates = {
        _canonical_date(obj.get("date"))
        for obj in _fact_objects(input_manifest, "orderbooks_l1")
    }
    if not l1_dates.issubset(set(dates)):
        raise ValueError("L1 object date falls outside INPUT_MANIFEST release_dates")
    return tuple(sorted(dates))


def _l1_interval_select_sql(exact_date: str | None) -> str:
    """Build the unchanged interval estimand, optionally bounded to one date."""
    date_filter = ""
    if exact_date is not None:
        date_filter = f"l.date=DATE {quote(_canonical_date(exact_date))} AND "
    return f"""
        WITH ordered AS (
          -- Keep the exact causal ordering keys in the window, but do not
          -- carry the full L1 row through its blocking sort.  The exact-date
          -- predicate makes each production sort one manifest day wide.
          SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
                 occurrence_us,close_us,
                 lead(t_us) OVER (
                   PARTITION BY date,market_ticker
                   ORDER BY t_us,coalesce(recv_wall_ns,0),
                            coalesce(recv_mono_ns,0),coalesce(ws_sid,0),
                            coalesce(ws_seq,0)
                 ) AS next_t_us
          FROM l1_enriched l
          WHERE {date_filter}date IS NOT NULL AND market_ticker IS NOT NULL
                AND t_us IS NOT NULL
        ), bounded AS (
          SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
                 occurrence_us,next_t_us IS NULL AS right_censored,
                 CASE WHEN next_t_us IS NULL THEN t_us ELSE
                   least(next_t_us,t_us+{BOOK_TTL_US},
                     CASE WHEN close_us>t_us THEN close_us
                          ELSE t_us+{BOOK_TTL_US} END)
                 END AS base_end_us
          FROM ordered
        ), gap_bound AS (
          SELECT b.date,b.t_us,b.market_ticker,b.event_proxy,b.sport,
                 b.book_state,b.occurrence_us,b.right_censored,b.base_end_us,
                 (SELECT min(g.start_us) FROM capture_gaps g
                  WHERE g.date=b.date AND g.start_us>b.t_us
                    AND g.start_us<b.base_end_us) AS next_gap_start_us,
                 EXISTS(SELECT 1 FROM capture_gaps g
                        WHERE g.date=b.date AND g.start_us<=b.t_us
                          AND g.end_us>b.t_us) AS starts_in_gap
          FROM bounded b
        )
        SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
               right_censored,starts_in_gap,
               least(base_end_us,coalesce(next_gap_start_us,base_end_us))
                 AS interval_end_us,
               CASE WHEN right_censored OR starts_in_gap THEN 0
                    ELSE greatest(0,least(base_end_us,
                         coalesce(next_gap_start_us,base_end_us))-t_us)
               END AS duration_us,
               {_phase_sql('gap_bound')} AS phase
        FROM gap_bound
    """


def _materialize_l1_intervals(con, dates: tuple[str, ...]) -> None:
    """Materialize fixed-width L1 intervals with one bounded sort per date."""
    bounded_dates = tuple(sorted(_canonical_date(date) for date in dates))
    if len(bounded_dates) != len(set(bounded_dates)):
        raise ValueError("L1 interval materialization received duplicate dates")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE l1_intervals("
        "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,book_state VARCHAR,right_censored BOOLEAN,"
        "starts_in_gap BOOLEAN,interval_end_us BIGINT,duration_us BIGINT,"
        "phase VARCHAR)"
    )
    columns = ",".join(L1_INTERVAL_COLUMNS)
    for date in bounded_dates:
        print(
            f"D3_W2A_STAGE l1_intervals date={date} state=START",
            flush=True,
        )
        con.execute(
            f"INSERT INTO l1_intervals({columns}) "
            + _l1_interval_select_sql(date)
        )
        print(
            f"D3_W2A_STAGE l1_intervals date={date} state=COMPLETE",
            flush=True,
        )


def _trade_duplicate_bucket_select_sql(bucket: int) -> str:
    """Return one exact trade-id bucket from the narrow duplicate candidates."""
    if isinstance(bucket, bool) or not isinstance(bucket, int):
        raise ValueError("trade dedup bucket must be an integer")
    if bucket < 0 or bucket >= TRADE_DEDUP_BUCKETS:
        raise ValueError("trade dedup bucket is outside the fixed 32 buckets")
    return f"""
        SELECT date,t_us,market_ticker,event_proxy,sport,trade_id,
               yes_price_e4,count_e4,taker_side,occurrence_us
        FROM (
          SELECT c.*,
                 row_number() OVER (
                   PARTITION BY c.trade_id
                   ORDER BY c.t_us,coalesce(c.recv_wall_ns,0),
                            coalesce(c.recv_mono_ns,0),c.market_ticker
                 ) AS rn
          FROM trade_duplicate_candidates c
          WHERE c.source_bucket={bucket} AND c.qc_bucket={bucket}
        ) ranked
        WHERE rn=1
    """


def _trade_duplicate_candidate_select_sql(bucket: int | None = None) -> str:
    """Return the narrow duplicate candidate projection, optionally bucketed."""
    if bucket is None:
        bucket_join = ""
    else:
        if isinstance(bucket, bool) or not isinstance(bucket, int):
            raise ValueError("trade dedup bucket must be an integer")
        if bucket < 0 or bucket >= TRADE_DEDUP_BUCKETS:
            raise ValueError("trade dedup bucket is outside the fixed 32 buckets")
        bucket_join = (
            f" AND q.qc_bucket={bucket}"
            f" AND hash(t.trade_id)%{TRADE_DEDUP_BUCKETS}={bucket}"
        )
    return f"""
        SELECT t.date,t.t_us,t.market_ticker,
               coalesce(t.fact_event_ticker,d.event_ticker,t.market_ticker)
                 AS event_proxy,
               t.sport,t.trade_id,t.yes_price_e4,t.count_e4,t.taker_side,
               d.occurrence_us,t.recv_wall_ns,t.recv_mono_ns,
               hash(t.trade_id)%{TRADE_DEDUP_BUCKETS} AS source_bucket,
               q.qc_bucket
        FROM trades_norm t JOIN trade_duplicate_ids q
          ON t.trade_id=q.trade_id{bucket_join}
        LEFT JOIN dim_market d
          ON t.date=d.date AND t.market_ticker=d.market_ticker
        WHERE q.economic_variants=1
          AND hash(t.trade_id)%{TRADE_DEDUP_BUCKETS}=q.qc_bucket
          AND t.date IS NOT NULL AND t.t_us IS NOT NULL
          AND t.market_ticker IS NOT NULL
    """


def _assert_trade_duplicate_sort_key_unambiguous(
    con, candidate_set: str
) -> None:
    """Fail closed when an exact winner sort key maps to different outputs."""
    ambiguous_sort_key_count = int(
        con.execute(
            """
            SELECT count(*)
            FROM (
              SELECT trade_id,t_us,coalesce(recv_wall_ns,0),
                     coalesce(recv_mono_ns,0),market_ticker
              FROM trade_duplicate_candidates
              GROUP BY trade_id,t_us,coalesce(recv_wall_ns,0),
                       coalesce(recv_mono_ns,0),market_ticker
              HAVING count(DISTINCT struct_pack(
                       date:=date,t_us:=t_us,market_ticker:=market_ticker,
                       event_proxy:=event_proxy,sport:=sport,
                       trade_id:=trade_id,yes_price_e4:=yes_price_e4,
                       count_e4:=count_e4,taker_side:=taker_side,
                       occurrence_us:=occurrence_us
                     )) > 1
            ) ambiguous_sort_keys
            """
        ).fetchone()[0]
    )
    print(
        "D3_W2A_QC trade_duplicate_sort_key_ambiguity "
        f"candidate_set={candidate_set} "
        f"ambiguous_sort_key_count={ambiguous_sort_key_count}",
        flush=True,
    )
    if ambiguous_sort_key_count:
        raise RuntimeError(
            "trade duplicate candidate sort-key ambiguity: "
            f"candidate_set={candidate_set} "
            f"ambiguous_sort_key_count={ambiguous_sort_key_count}"
        )


def _materialize_trade_duplicate_candidates(
    con, bucket: int | None = None
) -> None:
    """Create and validate one bounded candidate set."""
    if bucket is None:
        candidate_set = "global"
        marker = ""
    else:
        candidate_set = f"bucket-{bucket:02d}-of-{TRADE_DEDUP_BUCKETS}"
        marker = f" bucket={bucket:02d}/{TRADE_DEDUP_BUCKETS}"
    print(
        f"D3_W2A_STAGE trade_duplicate_candidates{marker} state=START",
        flush=True,
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE trade_duplicate_candidates AS "
        + _trade_duplicate_candidate_select_sql(bucket)
    )
    print(
        f"D3_W2A_STAGE trade_duplicate_candidates{marker} state=COMPLETE",
        flush=True,
    )
    _assert_trade_duplicate_sort_key_unambiguous(con, candidate_set)


def _materialize_trades_dedup(con) -> None:
    """Stream unique IDs and bucket only true duplicate trade IDs."""
    print("D3_W2A_STAGE trades_dedup state=START", flush=True)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE trades_dedup("
        "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,trade_id VARCHAR,yes_price_e4 BIGINT,count_e4 BIGINT,"
        "taker_side VARCHAR,occurrence_us BIGINT)"
    )
    columns = ",".join(TRADE_DEDUP_COLUMNS)

    print("D3_W2A_STAGE trade_duplicate_ids state=START", flush=True)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE trade_duplicate_ids AS
        SELECT trade_id,raw_rows,economic_variants,
               hash(trade_id)%{TRADE_DEDUP_BUCKETS} AS qc_bucket
        FROM trade_id_qc
        WHERE raw_rows>1
        """
    )
    print("D3_W2A_STAGE trade_duplicate_ids state=COMPLETE", flush=True)
    profile = con.execute(
        """
        SELECT count(*) AS non_null_id_count,
               count(*) FILTER (WHERE raw_rows>1) AS duplicate_id_count,
               count(*) FILTER (WHERE economic_variants>1)
                 AS conflicting_id_count,
               coalesce(sum(raw_rows),0) AS raw_rows,
               coalesce(sum(raw_rows-1),0) AS excess_repeat_rows,
               coalesce(sum(raw_rows) FILTER (WHERE raw_rows>1),0)
                 AS duplicate_raw_rows,
               coalesce(sum(raw_rows) FILTER (
                 WHERE raw_rows>1 AND economic_variants=1
               ),0) AS eligible_duplicate_raw_rows
        FROM trade_id_qc
        """
    ).fetchone()
    print(
        "D3_W2A_QC_PROFILE "
        f"non_null_id_count={int(profile[0])} "
        f"duplicate_id_count={int(profile[1])} "
        f"conflicting_id_count={int(profile[2])} "
        f"raw_rows={int(profile[3])} "
        f"excess_repeat_rows={int(profile[4])} "
        f"duplicate_raw_rows={int(profile[5])} "
        f"eligible_duplicate_raw_rows={int(profile[6])}",
        flush=True,
    )
    # Nothing after this point consumes the global QC table.  Release it
    # before the unique stream and duplicate window allocate their pages.
    con.execute("DROP TABLE trade_id_qc")
    print("D3_W2A_STAGE trade_id_qc state=DROPPED", flush=True)

    # Every non-null ID absent from trade_duplicate_ids has exactly one raw
    # row in the already-completed global QC table and therefore exactly one
    # economic variant.  It needs no blocking row_number operator.
    print("D3_W2A_STAGE trades_dedup_unique state=START", flush=True)
    con.execute(
        f"""
        INSERT INTO trades_dedup({columns})
        SELECT t.date,t.t_us,t.market_ticker,
               coalesce(t.fact_event_ticker,d.event_ticker,t.market_ticker),
               t.sport,t.trade_id,t.yes_price_e4,t.count_e4,t.taker_side,
               d.occurrence_us
        FROM trades_norm t
        LEFT JOIN dim_market d
          ON t.date=d.date AND t.market_ticker=d.market_ticker
        WHERE t.trade_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM trade_duplicate_ids q
            WHERE q.trade_id=t.trade_id
          )
          AND t.date IS NOT NULL AND t.t_us IS NOT NULL
          AND t.market_ticker IS NOT NULL
        """
    )
    print("D3_W2A_STAGE trades_dedup_unique state=COMPLETE", flush=True)

    eligible_duplicate_raw_rows = int(profile[6])
    if int(profile[1]) == 0 or eligible_duplicate_raw_rows == 0:
        con.execute("DROP TABLE trade_duplicate_ids")
        print("D3_W2A_STAGE trades_dedup state=COMPLETE", flush=True)
        return

    # Small duplicate sets retain the single narrow candidate CTAS.  Larger
    # sets rescan the source once per complete trade-id hash bucket so that no
    # all-duplicate candidate table is ever resident at once.
    use_global_candidate = (
        eligible_duplicate_raw_rows <= TRADE_DUPLICATE_FASTPATH_MAX_ROWS
    )
    if use_global_candidate:
        _materialize_trade_duplicate_candidates(con)

    for bucket in range(TRADE_DEDUP_BUCKETS):
        label = f"{bucket:02d}/{TRADE_DEDUP_BUCKETS}"
        print(
            f"D3_W2A_STAGE trades_dedup bucket={label} state=START",
            flush=True,
        )
        if not use_global_candidate:
            _materialize_trade_duplicate_candidates(con, bucket)
        con.execute(
            f"INSERT INTO trades_dedup({columns}) "
            + _trade_duplicate_bucket_select_sql(bucket)
        )
        if not use_global_candidate:
            con.execute("DROP TABLE trade_duplicate_candidates")
        print(
            f"D3_W2A_STAGE trades_dedup bucket={label} state=COMPLETE",
            flush=True,
        )
    if use_global_candidate:
        con.execute("DROP TABLE trade_duplicate_candidates")
    con.execute("DROP TABLE trade_duplicate_ids")
    print("D3_W2A_STAGE trades_dedup state=COMPLETE", flush=True)


def setup_database(con, input_manifest: dict[str, Any]) -> dict[str, Any]:
    """Create normalized receive-clock views and return explicit capabilities."""
    capabilities: dict[str, Any] = {
        "l1": {"present": False, "columns": []},
        "trades": {"present": False, "columns": []},
        "dim_markets": {"present": False, "columns": []},
        "capture_gaps": 0,
    }

    con.execute(
        "CREATE OR REPLACE TEMP TABLE capture_gaps("
        "date DATE,start_us BIGINT,end_us BIGINT)"
    )
    for obj in _kind_objects(input_manifest, "capture_gaps_projection"):
        path = Path(obj["local_path"])
        date = str(obj["date"])
        con.execute(
            "INSERT INTO capture_gaps SELECT DATE %s,try_cast(start_us AS BIGINT),"
            "try_cast(end_us AS BIGINT) FROM read_csv(%s,header=true,all_varchar=true) "
            "WHERE try_cast(start_us AS BIGINT) IS NOT NULL "
            "AND try_cast(end_us AS BIGINT)>try_cast(start_us AS BIGINT)"
            % (quote(date), quote(path))
        )
    capabilities["capture_gaps"] = int(
        scalar(con, "SELECT count(*) FROM capture_gaps")
    )

    dim_objects = [
        obj
        for obj in _kind_objects(input_manifest, "dim_snapshot")
        if str(obj.get("logical_key") or "").endswith("/markets.csv")
    ]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE dim_market("
        "date DATE,market_ticker VARCHAR,event_ticker VARCHAR,"
        "occurrence_us BIGINT,close_us BIGINT)"
    )
    if dim_objects:
        dim_paths = [Path(obj["local_path"]) for obj in dim_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dim_market_source AS "
            + _relation_sql(dim_paths)
        )
        columns = _columns(con, "dim_market_source")
        capabilities["dim_markets"] = {
            "present": True,
            "columns": sorted(columns),
        }
        ticker = _expr(columns, ("ticker", "market_ticker"), "market_ticker", "VARCHAR")
        event = _expr(columns, ("event_ticker",), "event_ticker", "VARCHAR")
        date = _expr(columns, ("date",), "date", "DATE")
        occurrence = _expr(
            columns,
            ("occurrence_datetime",),
            "occurrence_datetime",
            "TIMESTAMPTZ",
        )
        close = _expr(
            columns,
            ("close_time", "close", "expected_expiration_time"),
            "close_time",
            "TIMESTAMPTZ",
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dim_market_typed AS SELECT "
            + ",".join((date, ticker, event, occurrence, close))
            + " FROM dim_market_source"
        )
        con.execute(
            "INSERT INTO dim_market "
            "SELECT date,market_ticker,min(event_ticker),"
            "min(epoch_us(occurrence_datetime)),min(epoch_us(close_time)) "
            "FROM dim_market_typed WHERE date IS NOT NULL "
            "AND market_ticker IS NOT NULL GROUP BY date,market_ticker"
        )

    l1_objects = _fact_objects(input_manifest, "orderbooks_l1")
    if l1_objects:
        release_dates = _manifest_release_dates(input_manifest)
        l1_paths = [Path(obj["local_path"]) for obj in l1_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_source AS " + _relation_sql(l1_paths)
        )
        columns = _columns(con, "l1_source")
        capabilities["l1"] = {"present": True, "columns": sorted(columns)}
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("ws_sid",), "ws_sid", "BIGINT"),
            _expr(columns, ("ws_seq",), "ws_seq", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("series_ticker",), "series_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("group", "league"), "league", "VARCHAR"),
            _expr(columns, ("yes_bid_e4",), "yes_bid_e4", "BIGINT"),
            _expr(columns, ("yes_ask_e4",), "yes_ask_e4", "BIGINT"),
            _expr(columns, ("yes_bid_qty_e4",), "yes_bid_qty_e4", "BIGINT"),
            _expr(columns, ("yes_ask_qty_e4",), "yes_ask_qty_e4", "BIGINT"),
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_norm AS SELECT "
            + ",".join(projections)
            + " FROM l1_source"
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_enriched AS "
            "SELECT l.*,coalesce(l.fact_event_ticker,d.event_ticker,l.market_ticker) "
            "AS event_proxy,d.occurrence_us,d.close_us,"
            "CASE WHEN l.yes_bid_e4>0 AND l.yes_bid_e4<10000 "
            "AND l.yes_ask_e4>0 AND l.yes_ask_e4<10000 "
            "AND l.yes_bid_e4<l.yes_ask_e4 THEN 'TWO_SIDED' "
            "WHEN (coalesce(l.yes_bid_e4>0 AND l.yes_bid_e4<10000,false) <> "
            "coalesce(l.yes_ask_e4>0 AND l.yes_ask_e4<10000,false)) "
            "THEN 'ONE_SIDED' "
            "ELSE 'INVALID_BOOK' END AS book_state "
            "FROM l1_norm l LEFT JOIN dim_market d USING(date,market_ticker)"
        )
        _materialize_l1_intervals(con, release_dates)

    trade_objects = _fact_objects(input_manifest, "trades")
    if trade_objects:
        trade_paths = [Path(obj["local_path"]) for obj in trade_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW trades_source AS "
            + _relation_sql(trade_paths)
        )
        columns = _columns(con, "trades_source")
        capabilities["trades"] = {"present": True, "columns": sorted(columns)}
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("series_ticker",), "series_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("group", "league"), "league", "VARCHAR"),
            _expr(columns, ("trade_id",), "trade_id", "VARCHAR"),
            _expr(columns, ("yes_price_e4",), "yes_price_e4", "BIGINT"),
            _expr(columns, ("no_price_e4",), "no_price_e4", "BIGINT"),
            _expr(columns, ("count_e4",), "count_e4", "BIGINT"),
            _expr(columns, ("taker_side",), "taker_side", "VARCHAR"),
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW trades_norm AS SELECT "
            + ",".join(projections)
            + " FROM trades_source"
        )
        print("D3_W2A_STAGE trade_id_qc state=START", flush=True)
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE trade_id_qc AS
            SELECT trade_id,count(*) AS raw_rows,
                   count(DISTINCT hash(struct_pack(
                     date:=date,t_us:=t_us,market_ticker:=market_ticker,
                     yes_price_e4:=yes_price_e4,no_price_e4:=no_price_e4,
                     count_e4:=count_e4,taker_side:=lower(taker_side)
                   ))) AS economic_variants
            FROM trades_norm WHERE trade_id IS NOT NULL GROUP BY trade_id
            """
        )
        print("D3_W2A_STAGE trade_id_qc state=COMPLETE", flush=True)
        _materialize_trades_dedup(con)
        con.execute(
            """
            CREATE OR REPLACE TEMP VIEW trades_clean AS
            SELECT *,CASE WHEN lower(taker_side)='yes' THEN 1
                          WHEN lower(taker_side)='no' THEN -1 END AS taker_sign
            FROM trades_dedup
            WHERE yes_price_e4 BETWEEN 1 AND 9999
              AND lower(taker_side) IN ('yes','no')
            """
        )
    return capabilities


def _base_result(
    method_id: str,
    title: str,
    input_manifest: dict[str, Any],
    channels: set[str],
) -> dict[str, Any]:
    return {
        "method_id": method_id,
        "title": title,
        "research_stage": "OPEN_DISCOVERY",
        "evidence_label": "EXPLORATORY_ONLY",
        "economic_claim": "FORBIDDEN",
        "uncertainty": {
            "state": "NOT_ESTIMATED_DESCRIPTIVE_OPEN_DISCOVERY",
            "reason": (
                "D3-W2A corrects descriptive estimands only; no confidence "
                "interval, multiplicity-adjusted test, or policy verdict is implied"
            ),
        },
        "status": "NOT_ESTIMABLE",
        "reason": None,
        "summary": [],
        "waterfall": [],
        "queries": [],
        "provenance": {
            "release_ids": list(input_manifest.get("release_ids") or []),
            "exact_objects": _object_provenance(
                input_manifest,
                channels=channels,
                kinds={"dim_snapshot", "capture_gaps_projection"},
            ),
        },
    }


def run_b01(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B01-MARKOUT",
        "Receive-clock trade-side forward markout surface",
        input_manifest,
        {"orderbooks_l1", "trades"},
    )
    l1_columns = set(capabilities["l1"]["columns"])
    trade_columns = set(capabilities["trades"]["columns"])
    required_l1 = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    required_trades = {
        "local_recv_ts_us",
        "market_ticker",
        "trade_id",
        "yes_price_e4",
        "taker_side",
    }
    missing = sorted((required_l1 - l1_columns) | (required_trades - trade_columns))
    if missing:
        result["reason"] = "required receive-clock/price fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    result["asof_same_timestamp_qc"] = _assert_l1_asof_timestamps_unambiguous(con)

    mid = _mid_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    spread = _spread_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    pre_sql = f"""
      CREATE OR REPLACE TEMP TABLE b01_pre AS
      SELECT t.*,b.t_us AS pre_book_t_us,b.yes_bid_e4,b.yes_ask_e4,
             ({mid}) AS pre_mid_logodds,({spread}) AS spread_logodds,
             10000.0/(1.0+exp(-({mid}))) AS pre_mid_e4,
             CASE WHEN t.occurrence_us IS NULL THEN 'UNKNOWN_PHASE'
                  WHEN t.t_us<t.occurrence_us THEN 'PRE_SCHEDULED_START'
                  ELSE 'POST_SCHEDULED_START_PROXY' END AS phase
      FROM trades_clean t
      ASOF LEFT JOIN (
        SELECT * FROM l1_enriched WHERE book_state='TWO_SIDED'
          AND t_us IS NOT NULL
      ) b
      ON t.date=b.date AND t.market_ticker=b.market_ticker AND t.t_us>b.t_us
    """
    con.execute(pre_sql)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE b01_pre_fresh AS
        SELECT * FROM b01_pre p
        WHERE pre_book_t_us IS NOT NULL
          AND t_us-pre_book_t_us BETWEEN 0 AND {MAX_BOOK_AGE_US}
          AND NOT EXISTS (
            SELECT 1 FROM capture_gaps g WHERE g.date=p.date
              AND g.start_us<t_us AND g.end_us>pre_book_t_us
          )
        """
    )
    result["queries"].append({"query_id": "B01_PREBOOK", "sql": pre_sql.strip()})
    source = int(scalar(con, "SELECT count(*) FROM trades_norm"))
    clean = int(scalar(con, "SELECT count(*) FROM trades_clean"))
    causal = int(scalar(con, "SELECT count(*) FROM b01_pre WHERE pre_book_t_us IS NOT NULL"))
    fresh = int(scalar(con, "SELECT count(*) FROM b01_pre_fresh"))
    result["waterfall"] = [
        {"step": "source_trade_rows", "count": source, "excluded": 0},
        {"step": "identity_clock_side_price_valid", "count": clean, "excluded": source - clean},
        {"step": "strictly_past_book", "count": causal, "excluded": clean - causal},
        {"step": "book_age_and_gap_clean", "count": fresh, "excluded": causal - fresh},
    ]

    summaries: list[dict[str, Any]] = []
    measurable_max = 0
    for horizon in HORIZONS_US:
        table = f"b01_h{horizon}"
        future_mid = _mid_logodds_sql("f.yes_bid_e4", "f.yes_ask_e4")
        query = f"""
          CREATE OR REPLACE TEMP TABLE {table} AS
          WITH target AS (
            SELECT *,t_us+{horizon} AS target_us FROM b01_pre_fresh
          ), joined AS (
            SELECT p.*,f.t_us AS outcome_book_t_us,
                   ({future_mid}) AS outcome_mid_logodds
            FROM target p
            ASOF LEFT JOIN (
              SELECT * FROM l1_enriched WHERE book_state='TWO_SIDED'
                AND t_us IS NOT NULL
            ) f
            ON p.date=f.date AND p.market_ticker=f.market_ticker
               AND p.target_us>=f.t_us
          )
          SELECT *,taker_sign*(outcome_mid_logodds-pre_mid_logodds)
                     AS signed_markout_logodds
          FROM joined j
          WHERE outcome_book_t_us>t_us AND outcome_book_t_us<=target_us
            AND NOT EXISTS (
              SELECT 1 FROM capture_gaps g WHERE g.date=j.date
                AND g.start_us<outcome_book_t_us AND g.end_us>t_us
            )
        """
        con.execute(query)
        result["queries"].append(
            {"query_id": f"B01_H{horizon}", "sql": query.strip()}
        )
        n = int(scalar(con, f"SELECT count(*) FROM {table}"))
        measurable_max = max(measurable_max, n)
        rows = rows_as_dicts(
            con,
            f"""
            SELECT {horizon} AS horizon_us,coalesce(sport,'_UNKNOWN') AS sport,
                   phase,count(*) AS n,
                   avg(signed_markout_logodds) AS mean_signed_markout_logodds,
                   quantile_cont(signed_markout_logodds,0.5) AS p50_signed_markout_logodds,
                   avg(pre_mid_e4) AS mean_pre_mid_e4,
                   avg(spread_logodds) AS mean_spread_logodds,
                   count(DISTINCT event_proxy) AS root_event_proxies,
                   count(DISTINCT market_ticker) AS markets,
                   count(DISTINCT date) AS days
            FROM {table}
            GROUP BY sport,phase ORDER BY sport,phase
            """,
        )
        for row in rows:
            row["query_id"] = f"B01_H{horizon}"
        summaries.extend(rows)
        result["waterfall"].append(
            {
                "step": f"strict_future_book_within_{horizon}us",
                "count": n,
                "excluded": fresh - n,
            }
        )
    result["summary"] = summaries
    if measurable_max == 0:
        result["reason"] = "no trade had both a fresh past book and a strictly future receive-clock book"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "descriptive markout surface executed; positive signed values mean "
            "continuation in taker direction and are not NetPnL"
        )
    return result


def run_b02(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B02-ONESIDE",
        "Censor-correct one-sided state duration and incidence",
        input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    required = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    missing = sorted(required - columns)
    if missing:
        result["reason"] = "required L1 state fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    query = """
      SELECT coalesce(sport,'_UNKNOWN') AS sport,phase,book_state,
             count(*) AS intervals,count(DISTINCT market_ticker) AS markets,
             count(DISTINCT event_proxy) AS root_event_proxies,
             count(DISTINCT date) AS days,
             sum(duration_us)/1000000.0 AS exposure_seconds,
             quantile_cont(duration_us/1000000.0,0.5) AS p50_duration_seconds,
             quantile_cont(duration_us/1000000.0,0.95) AS p95_duration_seconds
      FROM l1_intervals
      WHERE duration_us>0 AND book_state IN ('TWO_SIDED','ONE_SIDED')
      GROUP BY sport,phase,book_state ORDER BY sport,phase,book_state
    """
    result["queries"].append({"query_id": "B02_STATE_EXPOSURE", "sql": query.strip()})
    rows = rows_as_dicts(con, query)
    for row in rows:
        row["query_id"] = "B02_STATE_EXPOSURE"
    source = int(scalar(con, "SELECT count(*) FROM l1_norm"))
    clocked = int(scalar(con, "SELECT count(*) FROM l1_intervals"))
    right = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE right_censored"))
    gap = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE starts_in_gap"))
    valid = int(
        scalar(
            con,
            "SELECT count(*) FROM l1_intervals WHERE duration_us>0 "
            "AND book_state IN ('TWO_SIDED','ONE_SIDED')",
        )
    )
    invalid = int(
        scalar(con, "SELECT count(*) FROM l1_intervals WHERE book_state='INVALID_BOOK'")
    )
    one_us = int(
        scalar(
            con,
            "SELECT coalesce(sum(duration_us),0) FROM l1_intervals "
            "WHERE duration_us>0 AND book_state='ONE_SIDED'",
        )
    )
    total_us = int(
        scalar(
            con,
            "SELECT coalesce(sum(duration_us),0) FROM l1_intervals "
            "WHERE duration_us>0 AND book_state IN ('ONE_SIDED','TWO_SIDED')",
        )
    )
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source, "excluded": 0},
        {"step": "receive_clock_market_rows", "count": clocked, "excluded": source - clocked},
        {"step": "invalid_book_state", "count": invalid, "excluded": invalid},
        {"step": "right_censored_last_rows", "count": right, "excluded": right},
        {"step": "starts_inside_capture_gap", "count": gap, "excluded": gap},
        {"step": "positive_valid_state_intervals", "count": valid, "excluded": max(0, clocked - valid)},
    ]
    result["summary"] = rows
    result["incidence"] = {
        "one_sided_exposure_us": one_us,
        "valid_state_exposure_us": total_us,
        "one_sided_exposure_share": (one_us / total_us if total_us else None),
        "query_id": "B02_STATE_EXPOSURE",
    }
    if total_us <= 0:
        result["reason"] = "no positive uncensored L1 state exposure after TTL/gap/terminal bounds"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "state exposure executed with terminal tail right-censored; zero "
            "one-sided exposure is a valid descriptive finding"
        )
    return result


def run_b03(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B03-XMKT",
        "Quote-age aligned linked-market residual",
        input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    if {"local_recv_ts_us", "market_ticker"} <= columns:
        query = """
          SELECT count(*) FROM (
            SELECT date,event_proxy,count(DISTINCT market_ticker) AS markets
            FROM l1_enriched
            WHERE t_us IS NOT NULL AND book_state='TWO_SIDED'
              AND event_proxy IS NOT NULL
            GROUP BY date,event_proxy HAVING count(DISTINCT market_ticker)>=2
          )
        """
        candidates = int(scalar(con, query))
        result["queries"].append(
            {"query_id": "B03_LINKED_EVENT_PREFLIGHT", "sql": query.strip()}
        )
    else:
        candidates = 0
    result["estimability_evidence"] = {
        "linked_event_proxy_candidates": candidates,
        "payout_exhaustive_roots": 0,
        "all_leg_fee_facts_complete": 0,
        "strict_multileg_fill_feasible_roots": 0,
        "blocking_reasons": [
            "PAYOUT_EXHAUSTIVENESS_NOT_AUTHENTICATED",
            "LEG_SPECIFIC_FEE_FACTS_NOT_BOUND_TO_INPUT",
            "STRICT_MULTI_LEG_FILL_MODEL_ABSENT",
        ],
    }
    result["waterfall"] = [
        {"step": "linked_event_proxy_candidates", "count": candidates, "excluded": 0},
        {"step": "payout_exhaustiveness_authenticated", "count": 0, "excluded": candidates},
        {"step": "fee_and_fill_feasible", "count": 0, "excluded": candidates},
    ]
    result["reason"] = (
        "NOT_ESTIMABLE: event_ticker proximity does not prove exhaustive payouts; "
        "without leg-specific fees and strict multi-leg fills, a residual would be "
        "a synthetic-arbitrage claim"
    )
    return result


def run_b04(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B04-RHYTHM",
        "Exposure-adjusted activity rhythm",
        input_manifest,
        {"orderbooks_l1", "trades"},
    )
    l1_columns = set(capabilities["l1"]["columns"])
    trade_columns = set(capabilities["trades"]["columns"])
    required_l1 = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    required_trade = {"local_recv_ts_us", "market_ticker", "trade_id"}
    missing = sorted((required_l1 - l1_columns) | (required_trade - trade_columns))
    if missing:
        result["reason"] = "active-minute/trade receive-clock fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    active_query = """
      CREATE OR REPLACE TEMP TABLE b04_active_minutes AS
      WITH eligible AS (
        SELECT date,market_ticker,event_proxy,coalesce(sport,'_UNKNOWN') AS sport,
               phase,t_us,interval_end_us,
               cast(floor(t_us/60000000) AS BIGINT) AS minute_id
        FROM l1_intervals
        WHERE duration_us>0 AND book_state IN ('ONE_SIDED','TWO_SIDED')
      ), expanded AS (
        SELECT date,market_ticker,event_proxy,sport,phase,minute_id FROM eligible
        UNION ALL
        SELECT date,market_ticker,event_proxy,sport,phase,minute_id+1
        FROM eligible WHERE interval_end_us>(minute_id+1)*60000000
      )
      SELECT DISTINCT date,market_ticker,event_proxy,sport,phase,minute_id
      FROM expanded
    """
    con.execute(active_query)
    trade_query = """
      CREATE OR REPLACE TEMP TABLE b04_trade_minutes AS
      SELECT date,market_ticker,cast(floor(t_us/60000000) AS BIGINT) AS minute_id,
             count(*) AS trades,coalesce(sum(count_e4),0) AS contracts_e4
      FROM trades_dedup GROUP BY date,market_ticker,minute_id
    """
    con.execute(trade_query)
    summary_query = """
      SELECT a.date,a.sport,a.phase,
             cast((a.minute_id % 1440)/60 AS BIGINT) AS utc_hour,
             count(*) AS active_market_minutes,
             count(DISTINCT coalesce(a.event_proxy,a.market_ticker)||':'||a.minute_id)
               AS active_event_minutes,
             count(DISTINCT a.market_ticker) AS markets,
             count(DISTINCT a.event_proxy) AS root_event_proxies,
             coalesce(sum(t.trades),0) AS trades,
             coalesce(sum(t.contracts_e4),0) AS contracts_e4,
             coalesce(sum(t.trades),0)::DOUBLE/count(*) AS trades_per_active_market_minute
      FROM b04_active_minutes a
      LEFT JOIN b04_trade_minutes t USING(date,market_ticker,minute_id)
      GROUP BY a.date,a.sport,a.phase,utc_hour
      ORDER BY a.date,a.sport,a.phase,utc_hour
    """
    result["queries"].extend(
        [
            {"query_id": "B04_ACTIVE_MINUTES", "sql": active_query.strip()},
            {"query_id": "B04_TRADE_MINUTES", "sql": trade_query.strip()},
            {"query_id": "B04_EXPOSURE_ADJUSTED", "sql": summary_query.strip()},
        ]
    )
    rows = rows_as_dicts(con, summary_query)
    for row in rows:
        row["query_id"] = "B04_EXPOSURE_ADJUSTED"
    source_l1 = int(scalar(con, "SELECT count(*) FROM l1_norm"))
    interval_rows = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE duration_us>0"))
    active = int(scalar(con, "SELECT count(*) FROM b04_active_minutes"))
    source_trades = int(scalar(con, "SELECT count(*) FROM trades_norm"))
    clean_trades = int(scalar(con, "SELECT count(*) FROM trades_dedup"))
    matched_trades = int(
        scalar(
            con,
            "SELECT coalesce(sum(t.trades),0) FROM b04_active_minutes a "
            "JOIN b04_trade_minutes t USING(date,market_ticker,minute_id)",
        )
    )
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source_l1, "excluded": 0},
        {"step": "positive_gap_clean_state_intervals", "count": interval_rows, "excluded": max(0, source_l1 - interval_rows)},
        {"step": "distinct_active_market_minutes", "count": active, "excluded": 0},
        {"step": "source_trade_rows", "count": source_trades, "excluded": 0},
        {"step": "identity_clock_deduplicated_trades", "count": clean_trades, "excluded": source_trades - clean_trades},
        {"step": "trades_matched_to_active_market_minute", "count": matched_trades, "excluded": max(0, clean_trades - matched_trades)},
    ]
    result["summary"] = rows
    if active <= 0:
        result["reason"] = "no active market-minute exposure after causal interval bounds"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "UTC rhythm executed with active market-minute/event-minute "
            "denominators; raw clock-hour volume is not used as capacity"
        )
    return result


def execute_all(con, input_manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    capabilities = setup_database(con, input_manifest)
    results = [
        run_b01(con, input_manifest, capabilities),
        run_b02(con, input_manifest, capabilities),
        run_b03(con, input_manifest, capabilities),
        run_b04(con, input_manifest, capabilities),
    ]
    if [result["method_id"] for result in results] != list(METHODS):
        raise RuntimeError("declared/executed method order drift")
    return capabilities, results
