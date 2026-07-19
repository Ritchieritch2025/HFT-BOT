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
import fcntl
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
BOUNDED_EXECUTION_RECEIPT_SCHEMA = "deep03-bounded-execution-receipt-v1"
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
                "size": obj.get("size", obj.get("size_bytes")),
                "row_count": obj.get("row_count"),
            }
        )
    payload = {
        "release_ids": list(input_manifest.get("release_ids") or []),
        "release_dates": list(input_manifest.get("release_dates") or []),
        "evidence_tier": input_manifest.get("evidence_tier"),
        "release_evidence": sorted(
            [
                {
                    "release_id": release.get("release_id"),
                    "date": release.get("date"),
                    "evidence_tier": release.get("evidence_tier"),
                    "evidence_basis": release.get("evidence_basis"),
                }
                for release in (input_manifest.get("releases") or [])
                if isinstance(release, dict)
            ],
            key=lambda row: (str(row["release_id"]), str(row["date"])),
        ),
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
        self._lock_handle = (self.root / ".CHECKPOINT_WRITER.lock").open("a+b")
        try:
            fcntl.flock(
                self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            self._lock_handle.close()
            raise RuntimeError(
                f"bounded checkpoint root already has a writer: {self.root}"
            ) from exc
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._closed = True

    def __enter__(self) -> "BoundedCheckpointStore":
        if self._closed:
            raise RuntimeError("bounded checkpoint store is closed")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def _require_lock(self) -> None:
        if self._closed:
            raise RuntimeError("bounded checkpoint store is closed")

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
        self._require_lock()
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
        self._require_lock()
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
        with data_path.open("rb") as payload_handle:
            os.fsync(payload_handle.fileno())
        data_directory_fd = os.open(data_path.parent, os.O_RDONLY)
        try:
            os.fsync(data_directory_fd)
        finally:
            os.close(data_directory_fd)
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
        self._require_lock()
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
            generated_values: set[int] = set()
            for child in generated.iterdir() if generated.exists() else ():
                prefix = f"{partition_column}="
                if not child.is_dir() or not child.name.startswith(prefix):
                    raise RuntimeError(
                        f"bounded scatter emitted unexpected path: {child.name}"
                    )
                try:
                    generated_values.add(int(child.name[len(prefix) :]))
                except ValueError as exc:
                    raise RuntimeError(
                        f"bounded scatter emitted non-integer partition: {child.name}"
                    ) from exc
            unexpected_values = generated_values - set(normalized)
            if unexpected_values:
                raise RuntimeError(
                    "bounded scatter emitted out-of-domain partitions: "
                    + ",".join(str(value) for value in sorted(unexpected_values))
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
        self._require_lock()
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
    return sorted(
        rows,
        key=lambda row: (
            str(row["release_id"]),
            str(row["logical_key"]),
            str(row["source_version_id"]),
        ),
    )


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
          AND book_state='TWO_SIDED'
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


def _l1_interval_order_ambiguity_sql(relation: str = "l1_enriched") -> str:
    """QC the complete receive-order key used by interval ``lead()``."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", relation) is None:
        raise ValueError("invalid L1 interval-order QC relation")
    return f"""
      WITH keyed AS (
        SELECT date,market_ticker,t_us,
               coalesce(recv_wall_ns,0) AS recv_wall_key,
               coalesce(recv_mono_ns,0) AS recv_mono_key,
               coalesce(ws_sid,0) AS ws_sid_key,
               coalesce(ws_seq,0) AS ws_seq_key,
               count(*) AS raw_rows,
               count(DISTINCT struct_pack(
                 event_proxy:=event_proxy,sport:=sport,
                 book_state:=book_state,occurrence_us:=occurrence_us,
                 close_us:=close_us
               )) AS interval_variants
        FROM {relation}
        WHERE date IS NOT NULL AND market_ticker IS NOT NULL AND t_us IS NOT NULL
        GROUP BY date,market_ticker,t_us,recv_wall_key,recv_mono_key,
                 ws_sid_key,ws_seq_key
      )
      SELECT count(*) FILTER (WHERE interval_variants>1) AS ambiguous_keys,
             coalesce(sum(raw_rows) FILTER (WHERE interval_variants>1),0)
               AS ambiguous_rows,
             count(*) FILTER (WHERE raw_rows>1 AND interval_variants=1)
               AS safe_duplicate_keys,
             coalesce(sum(raw_rows-1) FILTER (
               WHERE raw_rows>1 AND interval_variants=1
             ),0) AS safe_duplicate_excess_rows
      FROM keyed
    """


def _assert_l1_interval_order_unambiguous(
    con, relation: str = "l1_enriched", *, marker: str = "global"
) -> dict[str, int]:
    row = con.execute(_l1_interval_order_ambiguity_sql(relation)).fetchone()
    profile = {
        "ambiguous_keys": int(row[0]),
        "ambiguous_rows": int(row[1]),
        "safe_duplicate_keys": int(row[2]),
        "safe_duplicate_excess_rows": int(row[3]),
    }
    print(
        "D3_W2A_QC l1_interval_full_order_tie "
        f"partition={marker} ambiguous_keys={profile['ambiguous_keys']} "
        f"ambiguous_rows={profile['ambiguous_rows']} "
        f"safe_duplicate_keys={profile['safe_duplicate_keys']} "
        "safe_duplicate_excess_rows="
        f"{profile['safe_duplicate_excess_rows']}",
        flush=True,
    )
    if profile["ambiguous_keys"]:
        raise RuntimeError(
            "L1 interval full receive-order ambiguity: "
            f"partition={marker} ambiguous_keys={profile['ambiguous_keys']} "
            f"ambiguous_rows={profile['ambiguous_rows']}"
        )
    return profile


def bounded_stage_abi(con, market_buckets: int) -> dict[str, Any]:
    """Return the physical-shard ABI that every stage version must bind."""
    if isinstance(market_buckets, bool) or not isinstance(market_buckets, int):
        raise ValueError("market_buckets must be an integer")
    if market_buckets < 1 or market_buckets > 256:
        raise ValueError("market_buckets must be in [1,256]")
    duckdb_version = str(con.execute("SELECT version()").fetchone()[0])
    payload = {
        "schema_version": "deep03-bounded-stage-abi-v1",
        "duckdb_version": duckdb_version,
        "methods_module_sha256": _sha256_path(Path(__file__).resolve()),
        "market_bucket_algorithm": "duckdb-hash-v1-modulo",
        "market_bucket_count": market_buckets,
        "trade_id_bucket_algorithm": "duckdb-hash-v1-modulo",
        "trade_id_bucket_count": TRADE_DEDUP_BUCKETS,
        "horizons_us": list(HORIZONS_US),
        "max_book_age_us": MAX_BOOK_AGE_US,
        "book_ttl_us": BOOK_TTL_US,
        "l1_interval_columns": list(L1_INTERVAL_COLUMNS),
        "trade_dedup_columns": list(TRADE_DEDUP_COLUMNS),
    }
    payload["abi_sha256"] = _sha256_bytes(_canonical_json_bytes(payload))
    return payload


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
        _assert_l1_interval_order_unambiguous(con)
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


class _BoundedContext:
    """Execution-local coordinator for sequential bounded stages."""

    def __init__(
        self,
        con,
        input_manifest: dict[str, Any],
        store: BoundedCheckpointStore,
        market_buckets: int,
    ):
        self.con = con
        self.input_manifest = input_manifest
        self.store = store
        self.market_buckets = market_buckets
        self.dates = _manifest_release_dates(input_manifest)
        self.abi = bounded_stage_abi(con, market_buckets)
        self.activity: dict[str, dict[str, int]] = {}
        self.dim_source_columns: set[str] = set()

    def version(self, stage: str, semantic_version: str) -> str:
        return (
            f"{stage}-{semantic_version}-abi-"
            f"{self.abi['abi_sha256'][:16]}"
        )

    def _record(self, stage: str, reused: bool) -> None:
        row = self.activity.setdefault(stage, {"reused": 0, "written": 0})
        row["reused" if reused else "written"] += 1

    def write(
        self,
        *,
        stage: str,
        semantic_version: str,
        key: str,
        sql: str,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt, reused = self.store.write_partition(
            self.con,
            stage=stage,
            stage_version=self.version(stage, semantic_version),
            partition_key=key,
            select_sql=sql,
            metrics=metrics,
        )
        self._record(stage, reused)
        return receipt

    def existing(
        self, *, stage: str, semantic_version: str, key: str
    ) -> dict[str, Any] | None:
        _data, receipt_path = self.store._paths(stage, key)
        if not receipt_path.exists():
            return None
        receipt = self.store.validate_partition(
            self.con,
            stage=stage,
            stage_version=self.version(stage, semantic_version),
            partition_key=key,
        )
        self._record(stage, True)
        return receipt

    def scatter(
        self,
        *,
        stage: str,
        semantic_version: str,
        partition_column: str,
        partition_keys: dict[int, str],
        sql: str,
    ) -> dict[str, tuple[dict[str, Any], bool]]:
        rows = self.store.write_partitioned_query(
            self.con,
            stage=stage,
            stage_version=self.version(stage, semantic_version),
            partition_column=partition_column,
            partition_keys=partition_keys,
            select_sql=sql,
        )
        for _key, (_receipt, reused) in rows.items():
            self._record(stage, reused)
        return rows

    def finalize(
        self, *, stage: str, semantic_version: str, keys: Iterable[str]
    ) -> dict[str, Any]:
        path = self.store.finalize_stage(
            self.con,
            stage=stage,
            stage_version=self.version(stage, semantic_version),
            partition_keys=keys,
        )
        return json.loads(path.read_text(encoding="ascii"))

    def data_path(self, stage: str, key: str) -> Path:
        return self.store._paths(stage, key)[0]

    def execution_receipt(self) -> dict[str, Any]:
        stages = []
        for stage in sorted(self.activity):
            manifest_path = self.store.root / stage / "MANIFEST.json"
            if not manifest_path.is_file():
                raise RuntimeError(f"bounded stage was not finalized: {stage}")
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            stages.append(
                {
                    "stage": stage,
                    "stage_version": manifest["stage_version"],
                    "manifest_sha256": _sha256_path(manifest_path),
                    "partition_count": manifest["partition_count"],
                    "row_count": manifest["row_count"],
                    "reused_partitions": self.activity[stage]["reused"],
                    "written_partitions": self.activity[stage]["written"],
                }
            )
        return {
            "schema_version": BOUNDED_EXECUTION_RECEIPT_SCHEMA,
            "state": "COMPLETE",
            "source_binding": self.store.source_binding,
            "stage_abi": self.abi,
            "market_bucket_count": self.market_buckets,
            "stages": stages,
        }


def _bounded_key(date: str, bucket: int, label: str = "bucket") -> str:
    return f"date={_canonical_date(date)}_{label}={bucket:02d}"


def _bounded_relation(paths: Iterable[Path]) -> str:
    values = sorted((Path(path) for path in paths), key=lambda path: str(path))
    if not values:
        raise ValueError("bounded checkpoint relation is empty")
    return (
        f"read_parquet({path_list(values)},union_by_name=true,"
        "hive_partitioning=false)"
    )


def _require_row_conservation(
    *, label: str, observed: int, expected: int, context: str
) -> dict[str, Any]:
    """Fail closed unless two exact stage row counts are identical."""
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (observed, expected)
    ):
        raise RuntimeError(
            f"bounded row conservation received invalid count: {label}: {context}"
        )
    if observed != expected:
        raise RuntimeError(
            "bounded row conservation failed: "
            f"{label}: {context}: observed={observed} expected={expected}"
        )
    return {
        "state": "PASS",
        "label": label,
        "context": context,
        "observed_rows": observed,
        "expected_rows": expected,
    }


def _manifest_object_row_count(
    objects: Iterable[dict[str, Any]],
) -> int | None:
    """Return the exact sum only when every bound object supplies a count."""
    counts: list[int] = []
    for obj in objects:
        value = obj.get("row_count")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts.append(value)
    return sum(counts) if counts else None


def _objects_for_date(
    input_manifest: dict[str, Any], *, date: str, channel: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for obj in input_manifest.get("objects") or []:
        if obj.get("date") != date:
            continue
        if channel is not None and obj.get("channel") != channel:
            continue
        if kind is not None and obj.get("kind") != kind:
            continue
        rows.append(obj)
    return rows


def _setup_bounded_capture_gaps(con, input_manifest: dict[str, Any]) -> int:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE capture_gaps("
        "date DATE,start_us BIGINT,end_us BIGINT)"
    )
    for obj in _kind_objects(input_manifest, "capture_gaps_projection"):
        con.execute(
            "INSERT INTO capture_gaps SELECT DATE %s,try_cast(start_us AS BIGINT),"
            "try_cast(end_us AS BIGINT) FROM read_csv(%s,header=true,all_varchar=true) "
            "WHERE try_cast(start_us AS BIGINT) IS NOT NULL "
            "AND try_cast(end_us AS BIGINT)>try_cast(start_us AS BIGINT)"
            % (quote(str(obj["date"])), quote(Path(obj["local_path"])))
        )
    return int(scalar(con, "SELECT count(*) FROM capture_gaps"))


def _stage_bounded_dim_markets(ctx: _BoundedContext) -> list[str]:
    stage = "dim_market_date"
    semantic = "v2-min-exact-date-map-source-schema"
    keys: list[str] = []
    for date in ctx.dates:
        key = f"date={date}"
        keys.append(key)
        existing = ctx.existing(stage=stage, semantic_version=semantic, key=key)
        if existing:
            source_columns = (existing.get("metrics") or {}).get("source_columns")
            if not isinstance(source_columns, list) or any(
                not isinstance(column, str) for column in source_columns
            ):
                raise RuntimeError(
                    f"bounded dim source schema missing from receipt: {key}"
                )
            ctx.dim_source_columns.update(source_columns)
            continue
        objects = [
            obj
            for obj in _objects_for_date(
                ctx.input_manifest, date=date, kind="dim_snapshot"
            )
            if str(obj.get("logical_key") or "").endswith("/markets.csv")
        ]
        if not objects:
            source_columns: list[str] = []
            sql = (
                "SELECT CAST(NULL AS DATE) AS date,"
                "CAST(NULL AS VARCHAR) AS market_ticker,"
                "CAST(NULL AS VARCHAR) AS event_ticker,"
                "CAST(NULL AS BIGINT) AS occurrence_us,"
                "CAST(NULL AS BIGINT) AS close_us WHERE false"
            )
        else:
            paths = [Path(obj["local_path"]) for obj in objects]
            ctx.con.execute(
                "CREATE OR REPLACE TEMP VIEW bounded_dim_source AS "
                + _relation_sql(paths)
            )
            columns = _columns(ctx.con, "bounded_dim_source")
            ctx.dim_source_columns.update(columns)
            source_columns = sorted(columns)
            projections = (
                _expr(columns, ("date",), "date", "DATE"),
                _expr(columns, ("ticker", "market_ticker"), "market_ticker", "VARCHAR"),
                _expr(columns, ("event_ticker",), "event_ticker", "VARCHAR"),
                _expr(columns, ("occurrence_datetime",), "occurrence_datetime", "TIMESTAMPTZ"),
                _expr(
                    columns,
                    ("close_time", "close", "expected_expiration_time"),
                    "close_time",
                    "TIMESTAMPTZ",
                ),
            )
            ctx.con.execute(
                "CREATE OR REPLACE TEMP VIEW bounded_dim_typed AS SELECT "
                + ",".join(projections)
                + " FROM bounded_dim_source"
            )
            misdated = int(
                scalar(
                    ctx.con,
                    "SELECT count(*) FROM bounded_dim_typed "
                    f"WHERE date IS NOT NULL AND date<>DATE {quote(date)}",
                )
            )
            if misdated:
                raise RuntimeError(
                    f"bounded dim date binding mismatch: date={date} rows={misdated}"
                )
            sql = """
              SELECT date,market_ticker,min(event_ticker) AS event_ticker,
                     min(epoch_us(occurrence_datetime)) AS occurrence_us,
                     min(epoch_us(close_time)) AS close_us
              FROM bounded_dim_typed
              WHERE date IS NOT NULL AND market_ticker IS NOT NULL
              GROUP BY date,market_ticker
            """
        ctx.write(
            stage=stage,
            semantic_version=semantic,
            key=key,
            sql=sql,
            metrics={"source_columns": source_columns},
        )
        ctx.con.execute("DROP VIEW IF EXISTS bounded_dim_typed")
        ctx.con.execute("DROP VIEW IF EXISTS bounded_dim_source")
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys


def _stage_bounded_l1(
    ctx: _BoundedContext,
) -> tuple[list[str], set[str]]:
    stage = "l1_physical"
    semantic = "v1-date-market-hash-enriched"
    conservation_stage = "l1_physical_conservation"
    conservation_semantic = "v1-exact-sports-source-rows"
    keys: list[str] = []
    conservation_keys: list[str] = []
    total_expected = 0
    total_observed = 0
    all_columns: set[str] = set()
    for date in ctx.dates:
        objects = [
            obj
            for obj in _fact_objects(ctx.input_manifest, "orderbooks_l1")
            if obj.get("date") == date
        ]
        if not objects:
            raise ValueError(f"bounded L1 source absent for exact date {date}")
        paths = [Path(obj["local_path"]) for obj in objects]
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_l1_source AS "
            + _relation_sql(paths)
        )
        manifest_count = _manifest_object_row_count(objects)
        if manifest_count is None:
            expected_rows = int(
                scalar(ctx.con, "SELECT count(*) FROM bounded_l1_source")
            )
            source_basis = "DIRECT_COUNTED_EXACT_SPORTS_SOURCE"
        else:
            expected_rows = manifest_count
            source_basis = "BOUND_OBJECT_ROW_COUNT_SUM"
        columns = _columns(ctx.con, "bounded_l1_source")
        all_columns.update(columns)
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("ws_sid",), "ws_sid", "BIGINT"),
            _expr(columns, ("ws_seq",), "ws_seq", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("yes_bid_e4",), "yes_bid_e4", "BIGINT"),
            _expr(columns, ("yes_ask_e4",), "yes_ask_e4", "BIGINT"),
        )
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_l1_norm AS SELECT "
            + ",".join(projections)
            + " FROM bounded_l1_source"
        )
        dim = ctx.data_path("dim_market_date", f"date={date}")
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_l1_enriched AS "
            "SELECT l.date,l.t_us,l.recv_wall_ns,l.recv_mono_ns,l.ws_sid,l.ws_seq,"
            "l.market_ticker,coalesce(l.fact_event_ticker,d.event_ticker,l.market_ticker) "
            "AS event_proxy,l.sport,l.yes_bid_e4,l.yes_ask_e4,d.occurrence_us,d.close_us,"
            "CASE WHEN l.yes_bid_e4>0 AND l.yes_bid_e4<10000 "
            "AND l.yes_ask_e4>0 AND l.yes_ask_e4<10000 "
            "AND l.yes_bid_e4<l.yes_ask_e4 THEN 'TWO_SIDED' "
            "WHEN (coalesce(l.yes_bid_e4>0 AND l.yes_bid_e4<10000,false) <> "
            "coalesce(l.yes_ask_e4>0 AND l.yes_ask_e4<10000,false)) "
            "THEN 'ONE_SIDED' ELSE 'INVALID_BOOK' END AS book_state "
            "FROM bounded_l1_norm l LEFT JOIN "
            f"read_parquet({quote(dim)},hive_partitioning=false) d USING(date,market_ticker)"
        )
        mapping = {
            bucket: _bounded_key(date, bucket)
            for bucket in range(ctx.market_buckets + 1)
        }
        keys.extend(mapping.values())
        bucket_sql = (
            f"CASE WHEN market_ticker IS NULL THEN {ctx.market_buckets} "
            f"ELSE cast(hash(market_ticker)%{ctx.market_buckets} AS INTEGER) END"
        )
        scatter = ctx.scatter(
            stage=stage,
            semantic_version=semantic,
            partition_column="market_bucket",
            partition_keys=mapping,
            sql=(
                "SELECT *,"
                + bucket_sql
                + " AS market_bucket FROM bounded_l1_enriched"
            ),
        )
        observed_rows = sum(
            int(receipt["data"]["row_count"])
            for receipt, _reused in scatter.values()
        )
        conservation = _require_row_conservation(
            label="l1_physical_vs_exact_sports_source",
            context=f"date={date}",
            observed=observed_rows,
            expected=expected_rows,
        )
        conservation["source_basis"] = source_basis
        conservation_key = f"date={date}"
        conservation_keys.append(conservation_key)
        ctx.write(
            stage=conservation_stage,
            semantic_version=conservation_semantic,
            key=conservation_key,
            sql=(
                f"SELECT DATE {quote(date)} AS date,{expected_rows}::BIGINT "
                f"AS expected_rows,{observed_rows}::BIGINT AS observed_rows,"
                f"{quote(source_basis)} AS source_basis,'PASS' AS state"
            ),
            metrics=conservation,
        )
        total_expected += expected_rows
        total_observed += observed_rows
        for name in ("bounded_l1_enriched", "bounded_l1_norm", "bounded_l1_source"):
            ctx.con.execute(f"DROP VIEW IF EXISTS {name}")
    l1_manifest = ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    _require_row_conservation(
        label="l1_physical_manifest_total_vs_exact_sports_source",
        context="all_exact_dates",
        observed=int(l1_manifest["row_count"]),
        expected=total_expected,
    )
    total_conservation = _require_row_conservation(
        label="l1_physical_total_vs_exact_sports_source",
        context="all_exact_dates",
        observed=total_observed,
        expected=total_expected,
    )
    conservation_keys.append("TOTAL")
    ctx.write(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        key="TOTAL",
        sql=(
            f"SELECT {total_expected}::BIGINT AS expected_rows,"
            f"{total_observed}::BIGINT AS observed_rows,'PASS' AS state"
        ),
        metrics=total_conservation,
    )
    ctx.finalize(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        keys=conservation_keys,
    )
    return keys, all_columns


def _stage_bounded_trade_sources(
    ctx: _BoundedContext,
) -> tuple[list[str], list[str], set[str]]:
    stage = "trades_by_id"
    semantic = "v1-date-full-trade-id-hash"
    count_stage = "trade_source_counts"
    count_semantic = "v1-date-source-and-null-id"
    conservation_stage = "trades_by_id_conservation"
    conservation_semantic = "v1-source-minus-null-id"
    keys: list[str] = []
    count_keys: list[str] = []
    conservation_keys: list[str] = []
    total_source_rows = 0
    total_null_trade_id_rows = 0
    total_observed_rows = 0
    all_columns: set[str] = set()
    for date in ctx.dates:
        objects = [
            obj
            for obj in _fact_objects(ctx.input_manifest, "trades")
            if obj.get("date") == date
        ]
        if not objects:
            raise ValueError(f"bounded trade source absent for exact date {date}")
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_trades_source AS "
            + _relation_sql([Path(obj["local_path"]) for obj in objects])
        )
        columns = _columns(ctx.con, "bounded_trades_source")
        all_columns.update(columns)
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("trade_id",), "trade_id", "VARCHAR"),
            _expr(columns, ("yes_price_e4",), "yes_price_e4", "BIGINT"),
            _expr(columns, ("no_price_e4",), "no_price_e4", "BIGINT"),
            _expr(columns, ("count_e4",), "count_e4", "BIGINT"),
            _expr(columns, ("taker_side",), "taker_side", "VARCHAR"),
        )
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_trades_norm AS SELECT "
            + ",".join(projections)
            + " FROM bounded_trades_source"
        )
        count_key = f"date={date}"
        count_keys.append(count_key)
        ctx.write(
            stage=count_stage,
            semantic_version=count_semantic,
            key=count_key,
            sql=(
                f"SELECT DATE {quote(date)} AS date,count(*) AS source_rows,"
                "count(*) FILTER (WHERE trade_id IS NULL) AS null_trade_id_rows "
                "FROM bounded_trades_norm"
            ),
        )
        source_rows, null_trade_id_rows = ctx.con.execute(
            "SELECT source_rows,null_trade_id_rows FROM "
            f"read_parquet({quote(ctx.data_path(count_stage, count_key))},"
            "hive_partitioning=false)"
        ).fetchone()
        source_rows = int(source_rows)
        null_trade_id_rows = int(null_trade_id_rows)
        manifest_count = _manifest_object_row_count(objects)
        if manifest_count is not None:
            _require_row_conservation(
                label="trades_source_vs_bound_object_row_count",
                context=f"date={date}",
                observed=source_rows,
                expected=manifest_count,
            )
        dim = ctx.data_path("dim_market_date", f"date={date}")
        ctx.con.execute(
            "CREATE OR REPLACE TEMP VIEW bounded_trades_enriched AS "
            "SELECT t.date,t.t_us,t.recv_wall_ns,t.recv_mono_ns,t.market_ticker,"
            "coalesce(t.fact_event_ticker,d.event_ticker,t.market_ticker) AS event_proxy,"
            "t.sport,t.trade_id,t.yes_price_e4,t.no_price_e4,t.count_e4,t.taker_side,"
            "d.occurrence_us FROM bounded_trades_norm t LEFT JOIN "
            f"read_parquet({quote(dim)},hive_partitioning=false) d USING(date,market_ticker)"
        )
        mapping = {
            bucket: _bounded_key(date, bucket, "id_bucket")
            for bucket in range(TRADE_DEDUP_BUCKETS)
        }
        keys.extend(mapping.values())
        scatter = ctx.scatter(
            stage=stage,
            semantic_version=semantic,
            partition_column="trade_id_bucket",
            partition_keys=mapping,
            sql=(
                "SELECT *,cast(hash(trade_id)%"
                f"{TRADE_DEDUP_BUCKETS} AS INTEGER) AS trade_id_bucket "
                "FROM bounded_trades_enriched WHERE trade_id IS NOT NULL"
            ),
        )
        observed_rows = sum(
            int(receipt["data"]["row_count"])
            for receipt, _reused in scatter.values()
        )
        expected_rows = source_rows - null_trade_id_rows
        conservation = _require_row_conservation(
            label="trades_by_id_vs_source_minus_null_trade_id",
            context=f"date={date}",
            observed=observed_rows,
            expected=expected_rows,
        )
        conservation_key = f"date={date}"
        conservation_keys.append(conservation_key)
        ctx.write(
            stage=conservation_stage,
            semantic_version=conservation_semantic,
            key=conservation_key,
            sql=(
                f"SELECT DATE {quote(date)} AS date,{source_rows}::BIGINT "
                f"AS source_rows,{null_trade_id_rows}::BIGINT AS null_trade_id_rows,"
                f"{expected_rows}::BIGINT AS expected_rows,"
                f"{observed_rows}::BIGINT AS observed_rows,'PASS' AS state"
            ),
            metrics=conservation,
        )
        total_source_rows += source_rows
        total_null_trade_id_rows += null_trade_id_rows
        total_observed_rows += observed_rows
        for name in (
            "bounded_trades_enriched",
            "bounded_trades_norm",
            "bounded_trades_source",
        ):
            ctx.con.execute(f"DROP VIEW IF EXISTS {name}")
    ctx.finalize(stage=count_stage, semantic_version=count_semantic, keys=count_keys)
    trade_manifest = ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    total_expected_rows = total_source_rows - total_null_trade_id_rows
    _require_row_conservation(
        label="trades_by_id_manifest_vs_source_minus_null_trade_id",
        context="all_exact_dates",
        observed=int(trade_manifest["row_count"]),
        expected=total_expected_rows,
    )
    total_conservation = _require_row_conservation(
        label="trades_by_id_partition_total_vs_source_minus_null_trade_id",
        context="all_exact_dates",
        observed=total_observed_rows,
        expected=total_expected_rows,
    )
    conservation_keys.append("TOTAL")
    ctx.write(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        key="TOTAL",
        sql=(
            f"SELECT {total_source_rows}::BIGINT AS source_rows,"
            f"{total_null_trade_id_rows}::BIGINT AS null_trade_id_rows,"
            f"{total_expected_rows}::BIGINT AS expected_rows,"
            f"{total_observed_rows}::BIGINT AS observed_rows,'PASS' AS state"
        ),
        metrics=total_conservation,
    )
    ctx.finalize(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        keys=conservation_keys,
    )
    return keys, count_keys, all_columns


def _stage_bounded_trade_dedup(
    ctx: _BoundedContext,
) -> tuple[list[str], dict[str, int]]:
    """Preserve global trade identity within complete full-ID hash buckets."""
    stage = "trades_dedup_id"
    semantic = "v1-global-economic-qc-winner"
    keys: list[str] = []
    totals = {
        "non_null_id_count": 0,
        "duplicate_id_count": 0,
        "conflicting_id_count": 0,
        "raw_rows": 0,
        "excess_repeat_rows": 0,
        "duplicate_raw_rows": 0,
        "eligible_duplicate_raw_rows": 0,
    }
    for bucket in range(TRADE_DEDUP_BUCKETS):
        key = f"id_bucket={bucket:02d}"
        keys.append(key)
        receipt = ctx.existing(stage=stage, semantic_version=semantic, key=key)
        if receipt is None:
            paths = [
                ctx.data_path(
                    "trades_by_id", _bounded_key(date, bucket, "id_bucket")
                )
                for date in ctx.dates
            ]
            ctx.con.execute(
                "CREATE OR REPLACE TEMP VIEW bounded_trade_id_rows AS SELECT * FROM "
                + _bounded_relation(paths)
            )
            ctx.con.execute(
                """
                CREATE OR REPLACE TEMP TABLE bounded_trade_id_qc AS
                SELECT trade_id,count(*) AS raw_rows,
                       count(DISTINCT hash(struct_pack(
                         date:=date,t_us:=t_us,market_ticker:=market_ticker,
                         yes_price_e4:=yes_price_e4,no_price_e4:=no_price_e4,
                         count_e4:=count_e4,taker_side:=lower(taker_side)
                       ))) AS economic_variants
                FROM bounded_trade_id_rows
                GROUP BY trade_id
                """
            )
            profile = ctx.con.execute(
                """
                SELECT count(*),count(*) FILTER (WHERE raw_rows>1),
                       count(*) FILTER (WHERE economic_variants>1),
                       coalesce(sum(raw_rows),0),coalesce(sum(raw_rows-1),0),
                       coalesce(sum(raw_rows) FILTER (WHERE raw_rows>1),0),
                       coalesce(sum(raw_rows) FILTER (
                         WHERE raw_rows>1 AND economic_variants=1
                       ),0)
                FROM bounded_trade_id_qc
                """
            ).fetchone()
            metrics = {
                name: int(value)
                for name, value in zip(totals, profile)
            }
            ambiguity = int(
                scalar(
                    ctx.con,
                    """
                    SELECT count(*) FROM (
                      SELECT t.trade_id,t.t_us,coalesce(t.recv_wall_ns,0),
                             coalesce(t.recv_mono_ns,0),t.market_ticker
                      FROM bounded_trade_id_rows t
                      JOIN bounded_trade_id_qc q USING(trade_id)
                      WHERE q.economic_variants=1
                      GROUP BY t.trade_id,t.t_us,coalesce(t.recv_wall_ns,0),
                               coalesce(t.recv_mono_ns,0),t.market_ticker
                      HAVING count(DISTINCT struct_pack(
                        date:=t.date,t_us:=t.t_us,market_ticker:=t.market_ticker,
                        event_proxy:=t.event_proxy,sport:=t.sport,
                        trade_id:=t.trade_id,yes_price_e4:=t.yes_price_e4,
                        count_e4:=t.count_e4,taker_side:=t.taker_side,
                        occurrence_us:=t.occurrence_us
                      ))>1
                    ) ambiguous
                    """,
                )
            )
            metrics["ambiguous_full_sort_keys"] = ambiguity
            if ambiguity:
                raise RuntimeError(
                    "bounded trade duplicate full sort-key ambiguity: "
                    f"bucket={bucket:02d} keys={ambiguity}"
                )
            ctx.con.execute(
                """
                CREATE OR REPLACE TEMP TABLE bounded_trades_dedup AS
                SELECT date,t_us,market_ticker,event_proxy,sport,trade_id,
                       yes_price_e4,count_e4,taker_side,occurrence_us
                FROM (
                  SELECT t.*,
                         row_number() OVER (
                           PARTITION BY t.trade_id
                           ORDER BY t.t_us,coalesce(t.recv_wall_ns,0),
                                    coalesce(t.recv_mono_ns,0),t.market_ticker
                         ) AS rn
                  FROM bounded_trade_id_rows t
                  JOIN bounded_trade_id_qc q USING(trade_id)
                  WHERE q.economic_variants=1
                    AND t.date IS NOT NULL AND t.t_us IS NOT NULL
                    AND t.market_ticker IS NOT NULL
                ) ranked WHERE rn=1
                """
            )
            receipt = ctx.write(
                stage=stage,
                semantic_version=semantic,
                key=key,
                sql="SELECT * FROM bounded_trades_dedup",
                metrics=metrics,
            )
            ctx.con.execute("DROP TABLE bounded_trades_dedup")
            ctx.con.execute("DROP TABLE bounded_trade_id_qc")
            ctx.con.execute("DROP VIEW bounded_trade_id_rows")
        metrics = receipt.get("metrics") or {}
        if int(metrics.get("ambiguous_full_sort_keys", 0)):
            raise RuntimeError(
                f"bounded reused trade ambiguity receipt is nonzero: {key}"
            )
        for name in totals:
            totals[name] += int(metrics.get(name, 0))
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys, totals


def _stage_bounded_trades_by_market(
    ctx: _BoundedContext, dedup_keys: list[str]
) -> list[str]:
    """Shuffle globally deduplicated rows once into complete market shards."""
    stage = "trades_market"
    semantic = "v1-date-market-hash-after-global-id"
    conservation_stage = "trades_market_conservation"
    conservation_semantic = "v1-vs-global-dedup"
    paths = [ctx.data_path("trades_dedup_id", key) for key in dedup_keys]
    relation = _bounded_relation(paths)
    allowed_dates = ",".join(f"DATE {quote(date)}" for date in ctx.dates)
    outside = int(
        scalar(
            ctx.con,
            f"SELECT count(*) FROM {relation} WHERE date NOT IN ({allowed_dates})",
        )
    )
    if outside:
        raise RuntimeError(
            f"bounded dedup output date outside exact releases: rows={outside}"
        )
    date_code = "CASE " + " ".join(
        f"WHEN date=DATE {quote(date)} THEN {index * ctx.market_buckets}"
        for index, date in enumerate(ctx.dates)
    ) + " ELSE -1000000 END"
    mapping: dict[int, str] = {}
    for index, date in enumerate(ctx.dates):
        for bucket in range(ctx.market_buckets):
            mapping[index * ctx.market_buckets + bucket] = _bounded_key(date, bucket)
    scatter = ctx.scatter(
        stage=stage,
        semantic_version=semantic,
        partition_column="market_partition_code",
        partition_keys=mapping,
        sql=(
            "SELECT *,(("
            + date_code
            + f")+cast(hash(market_ticker)%{ctx.market_buckets} AS INTEGER)) "
            "AS market_partition_code FROM "
            + relation
        ),
    )
    keys = list(mapping.values())
    partition_rows = sum(
        int(receipt["data"]["row_count"])
        for receipt, _reused in scatter.values()
    )
    dedup_rows = int(_bounded_manifest(ctx, "trades_dedup_id")["row_count"])
    _require_row_conservation(
        label="trades_market_partitions_vs_trades_dedup_id",
        context="all_exact_dates",
        observed=partition_rows,
        expected=dedup_rows,
    )
    market_manifest = ctx.finalize(
        stage=stage, semantic_version=semantic, keys=keys
    )
    conservation = _require_row_conservation(
        label="trades_market_manifest_vs_trades_dedup_id_manifest",
        context="all_exact_dates",
        observed=int(market_manifest["row_count"]),
        expected=dedup_rows,
    )
    ctx.write(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        key="TOTAL",
        sql=(
            f"SELECT {dedup_rows}::BIGINT AS dedup_rows,"
            f"{int(market_manifest['row_count'])}::BIGINT AS market_rows,"
            "'PASS' AS state"
        ),
        metrics=conservation,
    )
    ctx.finalize(
        stage=conservation_stage,
        semantic_version=conservation_semantic,
        keys=["TOTAL"],
    )
    return keys


def _stage_bounded_l1_intervals(ctx: _BoundedContext) -> list[str]:
    stage = "l1_intervals"
    semantic = "v1-exact-11-column-causal"
    keys: list[str] = []
    for date in ctx.dates:
        for bucket in range(ctx.market_buckets):
            key = _bounded_key(date, bucket)
            keys.append(key)
            receipt = ctx.existing(stage=stage, semantic_version=semantic, key=key)
            if receipt is None:
                l1_path = ctx.data_path("l1_physical", key)
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP VIEW l1_enriched AS SELECT * FROM "
                    f"read_parquet({quote(l1_path)},hive_partitioning=false)"
                )
                marker = key
                interval_qc = _assert_l1_interval_order_unambiguous(
                    ctx.con, marker=marker
                )
                asof_qc = _assert_l1_asof_timestamps_unambiguous(
                    ctx.con, marker=marker
                )
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP TABLE bounded_l1_intervals AS "
                    + _l1_interval_select_sql(date)
                )
                metric_row = ctx.con.execute(
                    """
                    SELECT count(*) AS interval_rows,
                           count(*) FILTER (WHERE duration_us>0)
                             AS positive_interval_rows,
                           count(*) FILTER (WHERE right_censored) AS right_censored,
                           count(*) FILTER (WHERE starts_in_gap) AS starts_in_gap,
                           count(*) FILTER (WHERE book_state='INVALID_BOOK') AS invalid_books,
                           count(*) FILTER (WHERE duration_us>0 AND book_state IN
                             ('ONE_SIDED','TWO_SIDED')) AS positive_valid_intervals,
                           coalesce(sum(duration_us) FILTER (
                             WHERE duration_us>0 AND book_state='ONE_SIDED'),0)
                             AS one_sided_exposure_us,
                           coalesce(sum(duration_us) FILTER (
                             WHERE duration_us>0 AND book_state IN
                               ('ONE_SIDED','TWO_SIDED')),0)
                             AS valid_exposure_us
                    FROM bounded_l1_intervals
                    """
                ).fetchone()
                metric_names = (
                    "interval_rows",
                    "positive_interval_rows",
                    "right_censored",
                    "starts_in_gap",
                    "invalid_books",
                    "positive_valid_intervals",
                    "one_sided_exposure_us",
                    "valid_exposure_us",
                )
                metrics = {
                    name: int(value)
                    for name, value in zip(metric_names, metric_row)
                }
                metrics["interval_order_qc"] = interval_qc
                metrics["asof_same_timestamp_qc"] = asof_qc
                receipt = ctx.write(
                    stage=stage,
                    semantic_version=semantic,
                    key=key,
                    sql="SELECT * FROM bounded_l1_intervals",
                    metrics=metrics,
                )
                ctx.con.execute("DROP TABLE bounded_l1_intervals")
                ctx.con.execute("DROP VIEW l1_enriched")
            metrics = receipt.get("metrics") or {}
            for qc_name in (
                "interval_order_qc",
                "asof_same_timestamp_qc",
            ):
                if int((metrics.get(qc_name) or {}).get("ambiguous_keys", -1)) != 0:
                    raise RuntimeError(
                        f"bounded reused L1 QC is absent or ambiguous: {key}:{qc_name}"
                    )
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys


def _exact_strata(con, relation: str, columns: tuple[str, ...]) -> list[tuple[Any, ...]]:
    select = ",".join(columns)
    return con.execute(
        f"SELECT DISTINCT {select} FROM {relation} ORDER BY {select}"
    ).fetchall()


def _sql_equals(column: str, value: Any) -> str:
    if value is None:
        return f"{column} IS NULL"
    if isinstance(value, dt.date):
        return f"{column}=DATE {quote(value.isoformat())}"
    if isinstance(value, str):
        return f"{column}={quote(value)}"
    if isinstance(value, bool):
        return f"{column}={'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{column}={value}"
    raise TypeError(f"unsupported exact reducer stratum value: {value!r}")


def _reduce_b02_intervals(con, paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Reduce exact legacy duration statistics from narrow durable rows."""
    relation = _bounded_relation(paths)
    prior_threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    con.execute("SET threads=1")
    try:
        rows = rows_as_dicts(
            con,
            f"""
            SELECT coalesce(sport,'_UNKNOWN') AS sport,phase,book_state,
                   count(*) AS intervals,count(DISTINCT market_ticker) AS markets,
                   count(DISTINCT event_proxy) AS root_event_proxies,
                   count(DISTINCT date) AS days,
                   sum(duration_us)/1000000.0 AS exposure_seconds,
                   quantile_cont(duration_us/1000000.0,0.5) AS p50_duration_seconds,
                   quantile_cont(duration_us/1000000.0,0.95) AS p95_duration_seconds
            FROM {relation}
            WHERE duration_us>0 AND book_state IN ('TWO_SIDED','ONE_SIDED')
            GROUP BY sport,phase,book_state ORDER BY sport,phase,book_state
            """,
        )
    finally:
        con.execute(f"SET threads={prior_threads}")
    for result in rows:
        result["query_id"] = "B02_STATE_EXPOSURE"
    return rows


def _stage_bounded_b01_observations(ctx: _BoundedContext) -> list[str]:
    stage = "b01_observations"
    semantic = "v1-exact-past-future-markout"
    keys: list[str] = []
    for date in ctx.dates:
        for bucket in range(ctx.market_buckets):
            key = _bounded_key(date, bucket)
            keys.append(key)
            receipt = ctx.existing(stage=stage, semantic_version=semantic, key=key)
            if receipt is None:
                l1_path = ctx.data_path("l1_physical", key)
                trade_path = ctx.data_path("trades_market", key)
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP VIEW l1_enriched AS SELECT * FROM "
                    f"read_parquet({quote(l1_path)},hive_partitioning=false)"
                )
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP VIEW trades_dedup AS SELECT * FROM "
                    f"read_parquet({quote(trade_path)},hive_partitioning=false)"
                )
                ctx.con.execute(
                    """
                    CREATE OR REPLACE TEMP VIEW trades_clean AS
                    SELECT *,CASE WHEN lower(taker_side)='yes' THEN 1
                                  WHEN lower(taker_side)='no' THEN -1 END AS taker_sign
                    FROM trades_dedup
                    WHERE yes_price_e4 BETWEEN 1 AND 9999
                      AND lower(taker_side) IN ('yes','no')
                    """
                )
                mid = _mid_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
                spread = _spread_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
                ctx.con.execute(
                    f"""
                    CREATE OR REPLACE TEMP TABLE bounded_b01_pre AS
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
                    ON t.date=b.date AND t.market_ticker=b.market_ticker
                       AND t.t_us>b.t_us
                    """
                )
                ctx.con.execute(
                    f"""
                    CREATE OR REPLACE TEMP TABLE bounded_b01_pre_fresh AS
                    SELECT * FROM bounded_b01_pre p
                    WHERE pre_book_t_us IS NOT NULL
                      AND t_us-pre_book_t_us BETWEEN 0 AND {MAX_BOOK_AGE_US}
                      AND NOT EXISTS (
                        SELECT 1 FROM capture_gaps g WHERE g.date=p.date
                          AND g.start_us<t_us AND g.end_us>pre_book_t_us
                      )
                    """
                )
                ctx.con.execute(
                    """
                    CREATE OR REPLACE TEMP TABLE bounded_b01_observations(
                      date DATE,event_proxy VARCHAR,market_ticker VARCHAR,
                      sport VARCHAR,phase VARCHAR,horizon_us BIGINT,
                      signed_markout_logodds DOUBLE,pre_mid_e4 DOUBLE,
                      spread_logodds DOUBLE
                    )
                    """
                )
                horizon_counts: dict[str, int] = {}
                for horizon in HORIZONS_US:
                    future_mid = _mid_logodds_sql("f.yes_bid_e4", "f.yes_ask_e4")
                    ctx.con.execute(
                        f"""
                        INSERT INTO bounded_b01_observations
                        WITH target AS (
                          SELECT *,t_us+{horizon} AS target_us
                          FROM bounded_b01_pre_fresh
                        ), joined AS (
                          SELECT p.*,f.t_us AS outcome_book_t_us,
                                 ({future_mid}) AS outcome_mid_logodds
                          FROM target p
                          ASOF LEFT JOIN (
                            SELECT * FROM l1_enriched
                            WHERE book_state='TWO_SIDED' AND t_us IS NOT NULL
                          ) f
                          ON p.date=f.date AND p.market_ticker=f.market_ticker
                             AND p.target_us>=f.t_us
                        )
                        SELECT date,event_proxy,market_ticker,sport,phase,
                               {horizon} AS horizon_us,
                               taker_sign*(outcome_mid_logodds-pre_mid_logodds),
                               pre_mid_e4,spread_logodds
                        FROM joined j
                        WHERE outcome_book_t_us>t_us
                          AND outcome_book_t_us<=target_us
                          AND NOT EXISTS (
                            SELECT 1 FROM capture_gaps g WHERE g.date=j.date
                              AND g.start_us<outcome_book_t_us AND g.end_us>t_us
                          )
                        """
                    )
                    horizon_counts[str(horizon)] = int(
                        scalar(
                            ctx.con,
                            "SELECT count(*) FROM bounded_b01_observations "
                            f"WHERE horizon_us={horizon}",
                        )
                    )
                metrics = {
                    "deduplicated_trade_rows": int(
                        scalar(ctx.con, "SELECT count(*) FROM trades_dedup")
                    ),
                    "clean_trade_rows": int(
                        scalar(ctx.con, "SELECT count(*) FROM trades_clean")
                    ),
                    "causal_past_book_rows": int(
                        scalar(
                            ctx.con,
                            "SELECT count(*) FROM bounded_b01_pre "
                            "WHERE pre_book_t_us IS NOT NULL",
                        )
                    ),
                    "fresh_past_book_rows": int(
                        scalar(ctx.con, "SELECT count(*) FROM bounded_b01_pre_fresh")
                    ),
                    "horizon_rows": horizon_counts,
                }
                receipt = ctx.write(
                    stage=stage,
                    semantic_version=semantic,
                    key=key,
                    sql="SELECT * FROM bounded_b01_observations",
                    metrics=metrics,
                )
                for name in (
                    "bounded_b01_observations",
                    "bounded_b01_pre_fresh",
                    "bounded_b01_pre",
                ):
                    ctx.con.execute(f"DROP TABLE {name}")
                for name in ("trades_clean", "trades_dedup", "l1_enriched"):
                    ctx.con.execute(f"DROP VIEW {name}")
            horizon_rows = (receipt.get("metrics") or {}).get("horizon_rows")
            if (
                not isinstance(horizon_rows, dict)
                or set(horizon_rows) != {str(horizon) for horizon in HORIZONS_US}
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in horizon_rows.values()
                )
            ):
                raise RuntimeError(f"bounded B01 metrics missing: {key}")
            _require_row_conservation(
                label="b01_partition_payload_vs_horizon_metrics",
                context=key,
                observed=int(receipt["data"]["row_count"]),
                expected=sum(horizon_rows.values()),
            )
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys


def _reduce_b01_observations(con, paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Reduce exact legacy markout statistics from narrow durable rows."""
    relation = _bounded_relation(paths)
    prior_threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    con.execute("SET threads=1")
    try:
        rows = rows_as_dicts(
            con,
            f"""
            SELECT horizon_us,coalesce(sport,'_UNKNOWN') AS sport,phase,count(*) AS n,
                   avg(signed_markout_logodds) AS mean_signed_markout_logodds,
                   quantile_cont(signed_markout_logodds,0.5)
                     AS p50_signed_markout_logodds,
                   avg(pre_mid_e4) AS mean_pre_mid_e4,
                   avg(spread_logodds) AS mean_spread_logodds,
                   count(DISTINCT event_proxy) AS root_event_proxies,
                   count(DISTINCT market_ticker) AS markets,
                   count(DISTINCT date) AS days
            FROM {relation}
            GROUP BY horizon_us,sport,phase ORDER BY horizon_us,sport,phase
            """,
        )
    finally:
        con.execute(f"SET threads={prior_threads}")
    for result in rows:
        result["query_id"] = f"B01_H{int(result['horizon_us'])}"
    return rows


def _stage_bounded_b03_keys(ctx: _BoundedContext) -> list[str]:
    stage = "b03_event_market_keys"
    semantic = "v1-date-global-market-union"
    keys: list[str] = []
    for date in ctx.dates:
        key = f"date={date}"
        keys.append(key)
        paths = [
            ctx.data_path("l1_physical", _bounded_key(date, bucket))
            for bucket in range(ctx.market_buckets)
        ]
        ctx.write(
            stage=stage,
            semantic_version=semantic,
            key=key,
            sql=(
                "SELECT DISTINCT date,event_proxy,market_ticker FROM "
                + _bounded_relation(paths)
                + " WHERE t_us IS NOT NULL AND book_state='TWO_SIDED' "
                "AND event_proxy IS NOT NULL AND market_ticker IS NOT NULL"
            ),
        )
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys


def _stage_bounded_b04_observations(ctx: _BoundedContext) -> list[str]:
    stage = "b04_observations"
    semantic = "v1-active-market-minute-union"
    keys: list[str] = []
    for date in ctx.dates:
        for bucket in range(ctx.market_buckets):
            key = _bounded_key(date, bucket)
            keys.append(key)
            receipt = ctx.existing(stage=stage, semantic_version=semantic, key=key)
            if receipt is None:
                interval_path = ctx.data_path("l1_intervals", key)
                trade_path = ctx.data_path("trades_market", key)
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP VIEW l1_intervals AS SELECT * FROM "
                    f"read_parquet({quote(interval_path)},hive_partitioning=false)"
                )
                ctx.con.execute(
                    "CREATE OR REPLACE TEMP VIEW trades_dedup AS SELECT * FROM "
                    f"read_parquet({quote(trade_path)},hive_partitioning=false)"
                )
                ctx.con.execute(
                    """
                    CREATE OR REPLACE TEMP TABLE bounded_b04_active AS
                    WITH eligible AS (
                      SELECT date,market_ticker,event_proxy,
                             coalesce(sport,'_UNKNOWN') AS sport,phase,t_us,
                             interval_end_us,
                             cast(floor(t_us/60000000) AS BIGINT) AS minute_id
                      FROM l1_intervals
                      WHERE duration_us>0
                        AND book_state IN ('ONE_SIDED','TWO_SIDED')
                    ), expanded AS (
                      SELECT date,market_ticker,event_proxy,sport,phase,minute_id
                      FROM eligible
                      UNION ALL
                      SELECT date,market_ticker,event_proxy,sport,phase,minute_id+1
                      FROM eligible
                      WHERE interval_end_us>(minute_id+1)*60000000
                    )
                    SELECT DISTINCT date,market_ticker,event_proxy,sport,phase,minute_id
                    FROM expanded
                    """
                )
                ctx.con.execute(
                    """
                    CREATE OR REPLACE TEMP TABLE bounded_b04_trade_minutes AS
                    SELECT date,market_ticker,
                           cast(floor(t_us/60000000) AS BIGINT) AS minute_id,
                           count(*) AS trades,
                           coalesce(sum(count_e4),0) AS contracts_e4
                    FROM trades_dedup GROUP BY date,market_ticker,minute_id
                    """
                )
                ctx.con.execute(
                    """
                    CREATE OR REPLACE TEMP TABLE bounded_b04_observations AS
                    SELECT a.date,a.event_proxy,a.market_ticker,a.sport,a.phase,
                           a.minute_id,coalesce(t.trades,0) AS trades,
                           coalesce(t.contracts_e4,0) AS contracts_e4
                    FROM bounded_b04_active a
                    LEFT JOIN bounded_b04_trade_minutes t
                      USING(date,market_ticker,minute_id)
                    """
                )
                metrics = {
                    "active_market_minutes": int(
                        scalar(ctx.con, "SELECT count(*) FROM bounded_b04_active")
                    ),
                    "deduplicated_trade_rows": int(
                        scalar(ctx.con, "SELECT count(*) FROM trades_dedup")
                    ),
                    "matched_trades": int(
                        scalar(
                            ctx.con,
                            "SELECT coalesce(sum(trades),0) "
                            "FROM bounded_b04_observations",
                        )
                    ),
                    "eligible_intervals_spanning_gt2_minute_buckets": int(
                        scalar(
                            ctx.con,
                            "SELECT count(*) FROM l1_intervals "
                            "WHERE duration_us>0 AND book_state IN "
                            "('ONE_SIDED','TWO_SIDED') AND "
                            "(floor((interval_end_us-1)/60000000)-"
                            "floor(t_us/60000000)+1)>2",
                        )
                    ),
                    "max_eligible_interval_minute_buckets": int(
                        scalar(
                            ctx.con,
                            "SELECT coalesce(max(floor((interval_end_us-1)/60000000)-"
                            "floor(t_us/60000000)+1),0) FROM l1_intervals "
                            "WHERE duration_us>0 AND book_state IN "
                            "('ONE_SIDED','TWO_SIDED')",
                        )
                    ),
                }
                receipt = ctx.write(
                    stage=stage,
                    semantic_version=semantic,
                    key=key,
                    sql="SELECT * FROM bounded_b04_observations",
                    metrics=metrics,
                )
                for name in (
                    "bounded_b04_observations",
                    "bounded_b04_trade_minutes",
                    "bounded_b04_active",
                ):
                    ctx.con.execute(f"DROP TABLE {name}")
                for name in ("trades_dedup", "l1_intervals"):
                    ctx.con.execute(f"DROP VIEW {name}")
            active_market_minutes = (receipt.get("metrics") or {}).get(
                "active_market_minutes"
            )
            if (
                isinstance(active_market_minutes, bool)
                or not isinstance(active_market_minutes, int)
                or active_market_minutes < 0
            ):
                raise RuntimeError(f"bounded B04 metrics missing: {key}")
            _require_row_conservation(
                label="b04_partition_payload_vs_active_market_minutes",
                context=key,
                observed=int(receipt["data"]["row_count"]),
                expected=active_market_minutes,
            )
    ctx.finalize(stage=stage, semantic_version=semantic, keys=keys)
    return keys


def _reduce_b04_observations(con, paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Reduce active event/minute unions exactly from narrow durable rows."""
    relation = _bounded_relation(paths)
    prior_threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    con.execute("SET threads=1")
    try:
        rows = rows_as_dicts(
            con,
            f"""
            SELECT date,sport,phase,
                   cast((minute_id%1440)/60 AS BIGINT) AS utc_hour,
                   count(*) AS active_market_minutes,
                   count(DISTINCT coalesce(event_proxy,market_ticker)||':'||minute_id)
                     AS active_event_minutes,
                   count(DISTINCT market_ticker) AS markets,
                   count(DISTINCT event_proxy) AS root_event_proxies,
                   coalesce(sum(trades),0) AS trades,
                   coalesce(sum(contracts_e4),0) AS contracts_e4,
                   coalesce(sum(trades),0)::DOUBLE/count(*)
                     AS trades_per_active_market_minute
            FROM {relation}
            GROUP BY date,sport,phase,utc_hour
            ORDER BY date,sport,phase,utc_hour
            """,
        )
    finally:
        con.execute(f"SET threads={prior_threads}")
    for result in rows:
        result["query_id"] = "B04_EXPOSURE_ADJUSTED"
    return rows


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


def _bounded_manifest(ctx: _BoundedContext, stage: str) -> dict[str, Any]:
    path = ctx.store.root / stage / "MANIFEST.json"
    try:
        payload = path.read_bytes()
        manifest = json.loads(payload)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"bounded stage manifest unreadable: {stage}: {exc}") from exc
    if payload != _canonical_json_bytes(manifest):
        raise RuntimeError(f"bounded stage manifest is not canonical: {stage}")
    return manifest


def _bounded_metrics(
    ctx: _BoundedContext, stage: str, keys: Iterable[str]
) -> list[dict[str, Any]]:
    return [
        ctx.store._read_receipt(ctx.store._paths(stage, key)[1]).get("metrics") or {}
        for key in keys
    ]


def _sum_metric(metrics: Iterable[dict[str, Any]], name: str) -> int:
    return sum(int(row.get(name, 0)) for row in metrics)


def _bounded_capabilities(
    ctx: _BoundedContext,
    *,
    l1_columns: set[str],
    trade_columns: set[str],
    capture_gaps: int,
    trade_count_keys: list[str],
    trade_qc_profile: dict[str, int],
) -> dict[str, Any]:
    count_paths = [
        ctx.data_path("trade_source_counts", key) for key in trade_count_keys
    ]
    count_relation = _bounded_relation(count_paths)
    source_trade_rows, null_trade_id_rows = ctx.con.execute(
        f"SELECT coalesce(sum(source_rows),0),"
        f"coalesce(sum(null_trade_id_rows),0) FROM {count_relation}"
    ).fetchone()
    l1_manifest = _bounded_manifest(ctx, "l1_physical")
    trade_manifest = _bounded_manifest(ctx, "trades_dedup_id")
    return {
        "l1": {"present": bool(l1_columns), "columns": sorted(l1_columns)},
        "trades": {
            "present": bool(trade_columns),
            "columns": sorted(trade_columns),
        },
        "dim_markets": {
            "present": bool(
                [
                    obj
                    for obj in _kind_objects(ctx.input_manifest, "dim_snapshot")
                    if str(obj.get("logical_key") or "").endswith("/markets.csv")
                ]
            ),
            "columns": sorted(ctx.dim_source_columns),
        },
        "capture_gaps": capture_gaps,
        "counts": {
            "source_l1_rows": int(l1_manifest["row_count"]),
            "source_trade_rows": int(source_trade_rows),
            "null_trade_id_rows": int(null_trade_id_rows),
            "deduplicated_trade_rows": int(trade_manifest["row_count"]),
            "trade_id_qc": trade_qc_profile,
        },
        "bounded_execution": {
            "source_binding": ctx.store.source_binding,
            "market_bucket_count": ctx.market_buckets,
            "stage_abi": ctx.abi,
        },
    }


def _run_b01_bounded(
    ctx: _BoundedContext,
    capabilities: dict[str, Any],
    interval_keys: list[str],
) -> dict[str, Any]:
    result = _base_result(
        "D3-B01-MARKOUT",
        "Receive-clock trade-side forward markout surface",
        ctx.input_manifest,
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
    keys = _stage_bounded_b01_observations(ctx)
    paths = [ctx.data_path("b01_observations", key) for key in keys]
    result["summary"] = _reduce_b01_observations(ctx.con, paths)
    metrics = _bounded_metrics(ctx, "b01_observations", keys)
    interval_metrics = _bounded_metrics(ctx, "l1_intervals", interval_keys)
    result["asof_same_timestamp_qc"] = {
        name: sum(
            int((row.get("asof_same_timestamp_qc") or {}).get(name, 0))
            for row in interval_metrics
        )
        for name in (
            "ambiguous_keys",
            "ambiguous_rows",
            "safe_duplicate_keys",
            "safe_duplicate_excess_rows",
        )
    }
    source = int(capabilities["counts"]["source_trade_rows"])
    clean = _sum_metric(metrics, "clean_trade_rows")
    causal = _sum_metric(metrics, "causal_past_book_rows")
    fresh = _sum_metric(metrics, "fresh_past_book_rows")
    result["waterfall"] = [
        {"step": "source_trade_rows", "count": source, "excluded": 0},
        {"step": "identity_clock_side_price_valid", "count": clean, "excluded": source - clean},
        {"step": "strictly_past_book", "count": causal, "excluded": clean - causal},
        {"step": "book_age_and_gap_clean", "count": fresh, "excluded": causal - fresh},
    ]
    result["queries"].append(
        {
            "query_id": "B01_PREBOOK",
            "sql": "exact date+market-hash strictly-past ASOF with age and capture-gap gate",
        }
    )
    measurable_max = 0
    for horizon in HORIZONS_US:
        n = sum(int((row.get("horizon_rows") or {}).get(str(horizon), 0)) for row in metrics)
        measurable_max = max(measurable_max, n)
        result["waterfall"].append(
            {"step": f"strict_future_book_within_{horizon}us", "count": n, "excluded": fresh - n}
        )
        result["queries"].append(
            {"query_id": f"B01_H{horizon}", "sql": "exact date+market-hash ASOF observations; single-thread exact GROUP BY reducer"}
        )
    if measurable_max:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "descriptive markout surface executed; positive signed values mean "
            "continuation in taker direction and are not NetPnL"
        )
    else:
        result["reason"] = "no trade had both a fresh past book and a strictly future receive-clock book"
    return result


def _run_b02_bounded(
    ctx: _BoundedContext,
    capabilities: dict[str, Any],
    interval_keys: list[str],
) -> dict[str, Any]:
    result = _base_result(
        "D3-B02-ONESIDE",
        "Censor-correct one-sided state duration and incidence",
        ctx.input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    required = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    missing = sorted(required - columns)
    if missing:
        result["reason"] = "required L1 state fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result
    paths = [ctx.data_path("l1_intervals", key) for key in interval_keys]
    result["summary"] = _reduce_b02_intervals(ctx.con, paths)
    result["queries"].append(
        {"query_id": "B02_STATE_EXPOSURE", "sql": "verified 11-column interval Parquet; single-thread exact GROUP BY/quantile reducer"}
    )
    metrics = _bounded_metrics(ctx, "l1_intervals", interval_keys)
    source = int(capabilities["counts"]["source_l1_rows"])
    clocked = _sum_metric(metrics, "interval_rows")
    valid = _sum_metric(metrics, "positive_valid_intervals")
    one_us = _sum_metric(metrics, "one_sided_exposure_us")
    total_us = _sum_metric(metrics, "valid_exposure_us")
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source, "excluded": 0},
        {"step": "receive_clock_market_rows", "count": clocked, "excluded": source - clocked},
        {"step": "invalid_book_state", "count": _sum_metric(metrics, "invalid_books"), "excluded": _sum_metric(metrics, "invalid_books")},
        {"step": "right_censored_last_rows", "count": _sum_metric(metrics, "right_censored"), "excluded": _sum_metric(metrics, "right_censored")},
        {"step": "starts_inside_capture_gap", "count": _sum_metric(metrics, "starts_in_gap"), "excluded": _sum_metric(metrics, "starts_in_gap")},
        {"step": "positive_valid_state_intervals", "count": valid, "excluded": max(0, clocked - valid)},
    ]
    result["incidence"] = {
        "one_sided_exposure_us": one_us,
        "valid_state_exposure_us": total_us,
        "one_sided_exposure_share": one_us / total_us if total_us else None,
        "query_id": "B02_STATE_EXPOSURE",
    }
    if total_us > 0:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "state exposure executed with terminal tail right-censored; zero "
            "one-sided exposure is a valid descriptive finding"
        )
    else:
        result["reason"] = "no positive uncensored L1 state exposure after TTL/gap/terminal bounds"
    return result


def _run_b03_bounded(
    ctx: _BoundedContext, capabilities: dict[str, Any]
) -> dict[str, Any]:
    result = _base_result(
        "D3-B03-XMKT",
        "Quote-age aligned linked-market residual",
        ctx.input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    candidates = 0
    if {"local_recv_ts_us", "market_ticker"} <= columns:
        keys = _stage_bounded_b03_keys(ctx)
        for key in keys:
            relation = _bounded_relation([ctx.data_path("b03_event_market_keys", key)])
            candidates += int(
                scalar(
                    ctx.con,
                    f"SELECT count(*) FROM (SELECT date,event_proxy FROM {relation} "
                    "GROUP BY date,event_proxy HAVING count(DISTINCT market_ticker)>=2)",
                )
            )
        result["queries"].append(
            {"query_id": "B03_LINKED_EVENT_PREFLIGHT", "sql": "exact per-date union of market keys across every market hash bucket"}
        )
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


def _run_b04_bounded(
    ctx: _BoundedContext,
    capabilities: dict[str, Any],
    interval_keys: list[str],
) -> dict[str, Any]:
    result = _base_result(
        "D3-B04-RHYTHM",
        "Exposure-adjusted activity rhythm",
        ctx.input_manifest,
        {"orderbooks_l1", "trades"},
    )
    l1_columns = set(capabilities["l1"]["columns"])
    trade_columns = set(capabilities["trades"]["columns"])
    missing = sorted(
        ({"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"} - l1_columns)
        | ({"local_recv_ts_us", "market_ticker", "trade_id"} - trade_columns)
    )
    if missing:
        result["reason"] = "active-minute/trade receive-clock fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result
    keys = _stage_bounded_b04_observations(ctx)
    result["summary"] = _reduce_b04_observations(
        ctx.con, [ctx.data_path("b04_observations", key) for key in keys]
    )
    result["queries"].extend(
        [
            {"query_id": "B04_ACTIVE_MINUTES", "sql": "exact inherited start-minute plus one-next-minute expansion per verified interval shard"},
            {"query_id": "B04_TRADE_MINUTES", "sql": "globally deduplicated trade minute counts per complete market shard"},
            {"query_id": "B04_EXPOSURE_ADJUSTED", "sql": "single-thread global DISTINCT/GROUP BY reducer over narrow observations"},
        ]
    )
    metrics = _bounded_metrics(ctx, "b04_observations", keys)
    interval_metrics = _bounded_metrics(ctx, "l1_intervals", interval_keys)
    source_l1 = int(capabilities["counts"]["source_l1_rows"])
    positive = _sum_metric(interval_metrics, "positive_interval_rows")
    active = _sum_metric(metrics, "active_market_minutes")
    source_trades = int(capabilities["counts"]["source_trade_rows"])
    clean_trades = int(capabilities["counts"]["deduplicated_trade_rows"])
    matched = _sum_metric(metrics, "matched_trades")
    span_gt2 = _sum_metric(metrics, "eligible_intervals_spanning_gt2_minute_buckets")
    max_span = max(
        (int(row.get("max_eligible_interval_minute_buckets", 0)) for row in metrics),
        default=0,
    )
    result["minute_expansion_diagnostic"] = {
        "eligible_intervals_spanning_gt2_minute_buckets": span_gt2,
        "max_eligible_interval_minute_buckets": max_span,
        "inherited_expansion_semantics": "START_MINUTE_PLUS_AT_MOST_ONE_NEXT_MINUTE",
        "full_active_minute_exposure_claimed": False if span_gt2 else True,
    }
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source_l1, "excluded": 0},
        {"step": "positive_gap_clean_state_intervals", "count": positive, "excluded": max(0, source_l1 - positive)},
        {"step": "distinct_active_market_minutes", "count": active, "excluded": 0},
        {"step": "source_trade_rows", "count": source_trades, "excluded": 0},
        {"step": "identity_clock_deduplicated_trades", "count": clean_trades, "excluded": source_trades - clean_trades},
        {"step": "trades_matched_to_active_market_minute", "count": matched, "excluded": max(0, clean_trades - matched)},
    ]
    if active > 0:
        result["status"] = "EXECUTED"
        if span_gt2:
            result["reason"] = (
                "UTC rhythm executed with inherited start-minute plus at-most-one-next-minute "
                "expansion; full active-minute exposure is not claimed because the span "
                "diagnostic is nonzero"
            )
        else:
            result["reason"] = (
                "UTC rhythm executed with active market-minute/event-minute "
                "denominators; raw clock-hour volume is not used as capacity"
            )
    else:
        result["reason"] = "no active market-minute exposure after causal interval bounds"
    return result


def execute_all_bounded(
    con,
    input_manifest: dict[str, Any],
    checkpoint_root: Path,
    *,
    market_buckets: int = 32,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Execute B01-B04 sequentially from durable bounded-memory partitions."""
    binding = bounded_source_binding(input_manifest)
    with BoundedCheckpointStore(Path(checkpoint_root), binding) as store:
        ctx = _BoundedContext(con, input_manifest, store, market_buckets)
        capture_gaps = _setup_bounded_capture_gaps(con, input_manifest)
        _stage_bounded_dim_markets(ctx)
        _l1_keys, l1_columns = _stage_bounded_l1(ctx)
        _trade_keys, trade_count_keys, trade_columns = _stage_bounded_trade_sources(ctx)
        dedup_keys, trade_qc_profile = _stage_bounded_trade_dedup(ctx)
        _stage_bounded_trades_by_market(ctx, dedup_keys)
        interval_keys = _stage_bounded_l1_intervals(ctx)
        capabilities = _bounded_capabilities(
            ctx,
            l1_columns=l1_columns,
            trade_columns=trade_columns,
            capture_gaps=capture_gaps,
            trade_count_keys=trade_count_keys,
            trade_qc_profile=trade_qc_profile,
        )
        results = [
            _run_b01_bounded(ctx, capabilities, interval_keys),
            _run_b02_bounded(ctx, capabilities, interval_keys),
            _run_b03_bounded(ctx, capabilities),
            _run_b04_bounded(ctx, capabilities, interval_keys),
        ]
        if [result["method_id"] for result in results] != list(METHODS):
            raise RuntimeError("declared/executed bounded method order drift")
        receipt = ctx.execution_receipt()
        public_capabilities = {
            key: capabilities[key]
            for key in ("l1", "trades", "dim_markets", "capture_gaps")
        }
    return public_capabilities, results, receipt


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
