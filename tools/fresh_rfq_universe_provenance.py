#!/usr/bin/env python3
"""Exact-body L1/L2 market-universe provenance for the fresh RFQ lane.

This module is deliberately transport-neutral.  It rebuilds the exact v3
base binding, verifies Parquet content against every L1/L2 base identity, and
derives exact case-sensitive market universes in private local scratch.  The
legacy API accepts caller-supplied bytes; the bounded API accepts body-free
identities plus a context-managed one-object-at-a-time path reader.  Neither
API performs an AWS operation or claims an exact-VersionId GET occurred.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

import duckdb

import fresh_rfq_base_binding as base_binding


SCHEMA = "fresh-rfq-universe-provenance-v1"
STATE = "SUPPLIED_BYTES_MATCH_REBUILT_BASE_IDENTITIES"
FAMILIES = ("orderbooks_l1", "orderbooks_full")
MATCHING_POLICY = "EXACT_CASE_SENSITIVE_DISTINCT_NO_FALLBACK"
DATE_POLICY = "TS_UTC_IN_HALF_OPEN_ANALYSIS_DATE_NO_BORROW"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
IDENTITY_FIELDS = {
    "logical_key", "bucket", "key", "version_id", "size", "sha256",
}
EXACT_OBJECT_FIELDS = IDENTITY_FIELDS | {"body"}
OUTPUT_FIELDS = {
    "schema", "state", "verification_state", "analysis_date",
    "base_binding_sha256", "matching_policy", "date_policy",
    "extraction_contract", "extraction_contract_sha256", "families",
    "family_receipt_set_sha256", "source_object_count",
    "source_total_bytes", "all_base_family_objects_present",
    "all_input_bodies_omitted_from_output",
    "source_objects_exact_get_verified", "exact_get_attestation_state",
    "ephemeral_input_parquet_files_written",
    "ephemeral_input_parquet_bytes_written",
    "ephemeral_input_parquet_peak_file_bytes",
    "ephemeral_temp_deleted_before_return", "durable_data_objects_copied",
    "aws_read_performed_by_module", "aws_write_authorized",
    "research_eligible", "research_ready", "provenance_sha256",
}


class FreshRfqUniverseProvenanceError(ValueError):
    """Stable fail-closed error for exact-body universe derivation."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise FreshRfqUniverseProvenanceError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _canonical_exact_equal(left: Any, right: Any) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        _fail("INVALID_TEXT", f"{label} must be canonical non-empty text")
    return value


def _safe_key(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.startswith(("/", "\\")) or "\\" in value:
        _fail("UNSAFE_KEY", f"{label} is not a portable relative key")
    if any(part in ("", ".", "..") for part in value.split("/")):
        _fail("UNSAFE_KEY", f"{label} escapes containment")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase SHA-256")
    return value


def _version(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.lower() == "null":
        _fail("VERSION_REQUIRED", f"{label} requires a non-null VersionId")
    return value


def _identity(value: Any, label: str) -> dict[str, Any]:
    value = _exact_keys(value, IDENTITY_FIELDS, label)
    logical_key = _safe_key(value["logical_key"], f"{label}.logical_key")
    key = _safe_key(value["key"], f"{label}.key")
    if not logical_key.endswith(".parquet") or not key.endswith(".parquet"):
        _fail("PARQUET_REQUIRED", f"{label} must bind an exact Parquet object")
    size = value["size"]
    if type(size) is not int or size < 0:
        _fail("INVALID_SIZE", f"{label}.size must be a non-negative integer")
    return {
        "logical_key": logical_key,
        "bucket": _text(value["bucket"], f"{label}.bucket"),
        "key": key,
        "version_id": _version(value["version_id"], f"{label}.version_id"),
        "size": size,
        "sha256": _sha256(value["sha256"], f"{label}.sha256"),
    }


def _exact_object(value: Any, label: str) -> tuple[dict[str, Any], bytes]:
    value = _exact_keys(value, EXACT_OBJECT_FIELDS, label)
    identity = _identity(
        {field: value[field] for field in IDENTITY_FIELDS}, label
    )
    body = value["body"]
    if type(body) is not bytes:
        _fail("BODY_BYTES_REQUIRED", f"{label}.body must be exact bytes")
    if len(body) != identity["size"]:
        _fail("BODY_SIZE_MISMATCH", f"{label}.body size differs from identity")
    if hashlib.sha256(body).hexdigest() != identity["sha256"]:
        _fail("BODY_SHA_MISMATCH", f"{label}.body SHA-256 differs from identity")
    return identity, body


def _day_bounds_us(date_text: str) -> tuple[int, int]:
    try:
        day = dt.date.fromisoformat(date_text)
    except (TypeError, ValueError) as exc:
        _fail("INVALID_DATE", str(exc))
    epoch = dt.date(1970, 1, 1)
    lo = (day - epoch).days * 86_400_000_000
    return lo, lo + 86_400_000_000


def _validate_family_identity_set(
    identities: list[dict[str, Any]],
    family: str,
    expected_family: dict[str, Any],
    other_expected: dict[str, Any],
) -> list[dict[str, Any]]:
    expected = [
        _identity(row, f"base_binding.{family}[{index}]")
        for index, row in enumerate(expected_family["objects"])
    ]
    if (
        expected_family["object_count"] != len(expected)
        or expected_family["set_sha256"] != canonical_sha256(expected)
    ):
        _fail("BASE_FAMILY_INVALID", f"{family} family receipt is inconsistent")
    other_fingerprints = {
        canonical_bytes(_identity(row, f"base_binding.other[{index}]"))
        for index, row in enumerate(other_expected["objects"])
    }
    logicals: set[str] = set()
    physical: set[tuple[str, str, str]] = set()
    for identity in identities:
        if identity["logical_key"] in logicals:
            _fail("DUPLICATE_OBJECT", f"{family} duplicates a logical key")
        exact = (identity["bucket"], identity["key"], identity["version_id"])
        if exact in physical:
            _fail("DUPLICATE_OBJECT", f"{family} duplicates an exact version")
        logicals.add(identity["logical_key"])
        physical.add(exact)
        if canonical_bytes(identity) in other_fingerprints:
            _fail("CROSS_FAMILY_OBJECT", f"{family} contains the other family")
    normalized = sorted(identities, key=lambda row: row["logical_key"])
    observed = copy.deepcopy(normalized)
    expected.sort(key=lambda row: row["logical_key"])
    if not _canonical_exact_equal(observed, expected):
        _fail(
            "BASE_FAMILY_SET_MISMATCH",
            f"{family} exact object set differs from rebuilt base binding",
        )
    return normalized


def _normalize_family_inputs(
    values: Any,
    family: str,
    expected_family: dict[str, Any],
    other_expected: dict[str, Any],
) -> list[tuple[dict[str, Any], bytes]]:
    if not isinstance(values, list):
        _fail("OBJECT_LIST", f"{family} objects must be a list")
    pairs = [
        _exact_object(value, f"{family}_objects[{index}]")
        for index, value in enumerate(values)
    ]
    normalized_identities = _validate_family_identity_set(
        [identity for identity, _body in pairs],
        family,
        expected_family,
        other_expected,
    )
    body_by_identity = {
        canonical_bytes(identity): body for identity, body in pairs
    }
    return [
        (identity, body_by_identity[canonical_bytes(identity)])
        for identity in normalized_identities
    ]


def _normalize_family_reader_inputs(
    values: Any,
    family: str,
    expected_family: dict[str, Any],
    other_expected: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate a body-free family before the reader is ever invoked."""
    if not isinstance(values, list):
        _fail("OBJECT_LIST", f"{family} objects must be a list")
    identities = [
        _identity(value, f"{family}_objects[{index}]")
        for index, value in enumerate(values)
    ]
    return _validate_family_identity_set(
        identities, family, expected_family, other_expected
    )


def _reader_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Project the six-field base identity onto the exact-reader boundary."""
    return {
        key: identity[key]
        for key in ("bucket", "key", "version_id", "size", "sha256")
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_signature(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _write_all(descriptor: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError as exc:
            _fail("TEMP_IO_FAILED", str(exc))
        if written <= 0:
            _fail("TEMP_IO_FAILED", "short write while staging reader object")
        view = view[written:]


def _stage_reader_file(
    source: Path, stage: Path, identity: dict[str, Any],
) -> tuple[os.stat_result, int]:
    """Copy one source fd into the private stage DuckDB will reopen.

    DuckDB must never reopen the reader-owned pathname.  The source is opened
    once with ``O_NOFOLLOW`` and streamed into one ``O_EXCL`` 0600 inode under
    the module's 0700 root while size and SHA-256 are recomputed.  Pre/post
    stats bind DuckDB's path reads to that same verified stage inode.
    """
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    read_flags |= getattr(os, "O_CLOEXEC", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    write_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    digest = hashlib.sha256()
    total = 0
    source_descriptor: int | None = None
    stage_descriptor: int | None = None
    try:
        source_descriptor = os.open(source, read_flags)
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            _fail("READER_PATH_INVALID", "reader path is not a regular file")
        stage_descriptor = os.open(stage, write_flags, 0o600)
        os.fchmod(stage_descriptor, 0o600)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > identity["size"]:
                _fail(
                    "READER_SIZE_MISMATCH",
                    f"{identity['logical_key']} reader size differs",
                )
            digest.update(chunk)
            _write_all(stage_descriptor, chunk)
        os.fsync(stage_descriptor)
        source_after = os.fstat(source_descriptor)
        staged = os.fstat(stage_descriptor)
    except FreshRfqUniverseProvenanceError:
        raise
    except OSError as exc:
        _fail("READER_IO_FAILED", str(exc))
    finally:
        if stage_descriptor is not None:
            os.close(stage_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
    if _stat_signature(source_before) != _stat_signature(source_after):
        _fail(
            "READER_FILE_CHANGED",
            f"reader file changed while staging: {identity['logical_key']}",
        )
    if total != identity["size"] or staged.st_size != identity["size"]:
        _fail(
            "READER_SIZE_MISMATCH",
            f"{identity['logical_key']} reader size differs",
        )
    if digest.hexdigest() != identity["sha256"]:
        _fail(
            "READER_SHA_MISMATCH",
            f"{identity['logical_key']} reader SHA differs",
        )
    if (
        not stat.S_ISREG(staged.st_mode)
        or stat.S_IMODE(staged.st_mode) != 0o600
        or staged.st_nlink != 1
    ):
        _fail("TEMP_WRITE_MISMATCH", "private reader stage shape differs")
    return staged, total


def _reader_path(value: Any) -> Path:
    raw = getattr(value, "path", None)
    if not isinstance(raw, (str, os.PathLike)):
        _fail("READER_PATH_INVALID", "open_exact must yield an object with .path")
    raw_path = Path(raw)
    try:
        raw_stat = raw_path.lstat()
        if stat.S_ISLNK(raw_stat.st_mode) or not stat.S_ISREG(raw_stat.st_mode):
            _fail("READER_PATH_INVALID", "reader path must be a regular non-symlink")
        path = raw_path.resolve(strict=True)
        resolved_stat = path.lstat()
    except FreshRfqUniverseProvenanceError:
        raise
    except (OSError, RuntimeError) as exc:
        _fail("READER_PATH_INVALID", str(exc))
    if not stat.S_ISREG(resolved_stat.st_mode):
        _fail("READER_PATH_INVALID", "resolved reader path is not a regular file")
    return path


def _write_private_file(path: Path, body: bytes, expected_sha: str) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        observed = path.lstat()
    except OSError as exc:
        _fail("TEMP_IO_FAILED", str(exc))
    if not stat.S_ISREG(observed.st_mode) or observed.st_size != len(body):
        _fail("TEMP_WRITE_MISMATCH", "private scratch file shape differs")
    if _sha256_file(path) != expected_sha:
        _fail("TEMP_WRITE_MISMATCH", "private scratch file SHA-256 differs")
    return observed


def _restricted_connection(root: Path) -> duckdb.DuckDBPyConnection:
    spill = root / "spill"
    spill.mkdir(mode=0o700, exist_ok=True)
    try:
        connection = duckdb.connect(
            ":memory:",
            config={
                "threads": "1",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "allow_community_extensions": "false",
                "allow_unsigned_extensions": "false",
                "preserve_insertion_order": "false",
            },
        )
        connection.execute("SET temp_directory = ?", [str(spill)])
        connection.execute("SET allowed_directories = ?", [[str(root)]])
        connection.execute("SET default_collation = 'binary'")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
        return connection
    except (duckdb.Error, OSError) as exc:
        _fail("DUCKDB_SETUP_FAILED", str(exc))


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        _fail("INVALID_MARKET_TICKER", f"{label} is not an exact identifier")
    return value


def _extract_object(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    identity: dict[str, Any],
    analysis_date: str,
    day_lo: int,
    day_hi: int,
) -> tuple[dict[str, Any], list[str]]:
    read = "read_parquet(?, hive_partitioning=false, union_by_name=false)"
    try:
        described = connection.execute(
            f"DESCRIBE SELECT ts_utc, market_ticker FROM {read}", [str(path)]
        ).fetchall()
    except duckdb.Error as exc:
        _fail("PARQUET_READ_FAILED", f"{identity['logical_key']}: {exc}")
    required = [(row[0], row[1].upper()) for row in described]
    if required != [("ts_utc", "BIGINT"), ("market_ticker", "VARCHAR")]:
        _fail(
            "PARQUET_SCHEMA_INVALID",
            f"{identity['logical_key']} required schema is {required!r}",
        )
    try:
        summary = connection.execute(
            f"""
            WITH source AS (
              SELECT ts_utc, market_ticker FROM {read}
            )
            SELECT
              count(*),
              count(*) FILTER (WHERE ts_utc IS NULL),
              count(*) FILTER (WHERE ts_utc < ? OR ts_utc >= ?),
              count(*) FILTER (WHERE market_ticker IS NULL),
              min(ts_utc), max(ts_utc)
            FROM source
            """,
            [str(path), day_lo, day_hi],
        ).fetchone()
        ticker_rows = connection.execute(
            f"SELECT DISTINCT market_ticker FROM {read}", [str(path)]
        ).fetchall()
    except duckdb.Error as exc:
        _fail("PARQUET_READ_FAILED", f"{identity['logical_key']}: {exc}")
    assert summary is not None
    row_count, null_ts, cross_date, null_ticker, min_ts, max_ts = summary
    if null_ts:
        _fail("NULL_TS_UTC", f"{identity['logical_key']} contains null ts_utc")
    if cross_date:
        _fail("CROSS_DATE_ROWS", f"{identity['logical_key']} leaves date D")
    if null_ticker:
        _fail(
            "NULL_MARKET_TICKER",
            f"{identity['logical_key']} contains null market_ticker",
        )
    tickers = [
        _identifier(row[0], f"{identity['logical_key']}.market_ticker")
        for row in ticker_rows
    ]
    if len(tickers) != len(set(tickers)):
        _fail("DISTINCT_CONTRACT_FAILED", identity["logical_key"])
    tickers.sort()
    universe_rows = [
        {"analysis_date": analysis_date, "market_ticker": ticker}
        for ticker in tickers
    ]
    receipt = {
        **identity,
        "required_schema_sha256": canonical_sha256(required),
        "row_count": int(row_count),
        "min_ts_utc": min_ts,
        "max_ts_utc": max_ts,
        "null_ts_utc_row_count": 0,
        "cross_date_row_count": 0,
        "null_market_ticker_row_count": 0,
        "distinct_market_ticker_count": len(tickers),
        "market_ticker_set_sha256": canonical_sha256(universe_rows),
        "body_size_verified": True,
        "body_sha256_verified": True,
    }
    return receipt, tickers


def _family_receipt(
    *, family: str, identities: list[dict[str, Any]],
    object_receipts: list[dict[str, Any]], all_tickers: set[str],
    object_distinct_sum: int, base: dict[str, Any], analysis_date: str,
) -> dict[str, Any]:
    universe = [
        {"analysis_date": analysis_date, "market_ticker": ticker}
        for ticker in sorted(all_tickers)
    ]
    return {
        "family": family,
        "base_family_set_sha256": base["families"][family]["set_sha256"],
        "source_object_count": len(identities),
        "source_object_set_sha256": canonical_sha256(identities),
        "source_total_bytes": sum(row["size"] for row in identities),
        "body_verified_object_count": len(identities),
        "parquet_row_count": sum(row["row_count"] for row in object_receipts),
        "object_distinct_market_ticker_sum": object_distinct_sum,
        "cross_object_market_ticker_overlap_count": (
            object_distinct_sum - len(universe)
        ),
        "market_universe": universe,
        "market_universe_count": len(universe),
        "market_universe_sha256": canonical_sha256(universe),
        "object_receipts": object_receipts,
        "object_receipt_set_sha256": canonical_sha256(object_receipts),
        "null_ts_utc_row_count": 0,
        "cross_date_row_count": 0,
        "null_market_ticker_row_count": 0,
    }


def _extract_families(
    normalized: dict[str, list[tuple[dict[str, Any], bytes]]],
    base: dict[str, Any],
    analysis_date: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    day_lo, day_hi = _day_bounds_us(analysis_date)
    temp_path: str | None = None
    family_receipts: dict[str, Any] = {}
    stats = {"files": 0, "bytes": 0, "peak": 0}
    try:
        with tempfile.TemporaryDirectory(prefix="fresh-rfq-universe-") as root_text:
            temp_path = root_text
            root = Path(root_text)
            os.chmod(root, 0o700)
            connection = _restricted_connection(root)
            try:
                global_ordinal = 0
                for family in FAMILIES:
                    object_receipts = []
                    all_tickers: set[str] = set()
                    object_distinct_sum = 0
                    for identity, body in normalized[family]:
                        path = root / f"object-{global_ordinal:08d}.parquet"
                        global_ordinal += 1
                        before = _write_private_file(
                            path, body, identity["sha256"]
                        )
                        stats["files"] += 1
                        stats["bytes"] += len(body)
                        stats["peak"] = max(stats["peak"], len(body))
                        try:
                            receipt, tickers = _extract_object(
                                connection,
                                path,
                                identity,
                                analysis_date,
                                day_lo,
                                day_hi,
                            )
                            after = path.lstat()
                            signature_before = (
                                before.st_ino,
                                before.st_size,
                                before.st_mtime_ns,
                                before.st_ctime_ns,
                            )
                            signature_after = (
                                after.st_ino,
                                after.st_size,
                                after.st_mtime_ns,
                                after.st_ctime_ns,
                            )
                            if signature_after != signature_before:
                                _fail(
                                    "TEMP_FILE_CHANGED",
                                    f"scratch changed: {identity['logical_key']}",
                                )
                            object_receipts.append(receipt)
                            object_distinct_sum += len(tickers)
                            all_tickers.update(tickers)
                        finally:
                            try:
                                path.unlink(missing_ok=True)
                            except OSError as exc:
                                _fail("TEMP_CLEANUP_FAILED", str(exc))
                    identities = [item[0] for item in normalized[family]]
                    family_receipts[family] = _family_receipt(
                        family=family,
                        identities=identities,
                        object_receipts=object_receipts,
                        all_tickers=all_tickers,
                        object_distinct_sum=object_distinct_sum,
                        base=base,
                        analysis_date=analysis_date,
                    )
            finally:
                connection.close()
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            _fail("TEMP_CLEANUP_FAILED", "private scratch remains after extraction")
    return family_receipts, stats


def _extract_families_from_reader(
    normalized: dict[str, list[dict[str, Any]]],
    base: dict[str, Any],
    analysis_date: str,
    open_exact: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Stage and consume one reader-owned exact object at a time."""
    if not callable(open_exact):
        _fail("READER_INVALID", "open_exact must be callable")
    day_lo, day_hi = _day_bounds_us(analysis_date)
    temp_path: str | None = None
    family_receipts: dict[str, Any] = {}
    stats = {"files": 0, "bytes": 0, "peak": 0}
    try:
        with tempfile.TemporaryDirectory(
            prefix="fresh-rfq-universe-reader-"
        ) as root_text:
            temp_path = root_text
            root = Path(root_text)
            os.chmod(root, 0o700)
            global_ordinal = 0
            for family in FAMILIES:
                object_receipts = []
                all_tickers: set[str] = set()
                object_distinct_sum = 0
                for identity in normalized[family]:
                    object_completed_and_verified = False
                    try:
                        manager = open_exact(_reader_identity(identity))
                    except Exception as exc:
                        _fail("READER_FAILED", str(exc))
                    try:
                        with manager as opened:
                            stage_path = root / (
                                f"reader-object-{global_ordinal:08d}.parquet"
                            )
                            global_ordinal += 1
                            try:
                                source_path = _reader_path(opened)
                                staged, observed_size = _stage_reader_file(
                                    source_path, stage_path, identity
                                )
                                try:
                                    before_query = stage_path.lstat()
                                except OSError as exc:
                                    _fail("TEMP_IO_FAILED", str(exc))
                                if _stat_signature(before_query) != \
                                        _stat_signature(staged):
                                    _fail(
                                        "TEMP_FILE_CHANGED",
                                        f"reader stage changed before query: "
                                        f"{identity['logical_key']}",
                                    )

                                connection = _restricted_connection(root)
                                try:
                                    receipt, tickers = _extract_object(
                                        connection,
                                        stage_path,
                                        identity,
                                        analysis_date,
                                        day_lo,
                                        day_hi,
                                    )
                                finally:
                                    connection.close()

                                try:
                                    after_query = stage_path.lstat()
                                except OSError as exc:
                                    _fail("TEMP_IO_FAILED", str(exc))
                                if _stat_signature(after_query) != \
                                        _stat_signature(staged):
                                    _fail(
                                        "TEMP_FILE_CHANGED",
                                        f"reader stage changed during query: "
                                        f"{identity['logical_key']}",
                                    )
                                object_receipts.append(receipt)
                                object_distinct_sum += len(tickers)
                                all_tickers.update(tickers)
                                stats["files"] += 1
                                stats["bytes"] += observed_size
                                stats["peak"] = max(
                                    stats["peak"], observed_size
                                )
                                object_completed_and_verified = True
                            finally:
                                try:
                                    stage_path.unlink(missing_ok=True)
                                except OSError as exc:
                                    _fail("TEMP_CLEANUP_FAILED", str(exc))
                    except FreshRfqUniverseProvenanceError:
                        raise
                    except Exception as exc:
                        _fail("READER_FAILED", str(exc))
                    # A callback context manager is not trusted to propagate
                    # exceptions: __exit__ may return True and suppress a
                    # hash/size/Parquet failure raised inside its block.  This
                    # postcondition is outside that manager and therefore
                    # cannot be suppressed by it.
                    if not object_completed_and_verified:
                        _fail(
                            "READER_VERIFICATION_INCOMPLETE",
                            f"reader object was not fully verified: "
                            f"{identity['logical_key']}",
                        )
                family_receipts[family] = _family_receipt(
                    family=family,
                    identities=normalized[family],
                    object_receipts=object_receipts,
                    all_tickers=all_tickers,
                    object_distinct_sum=object_distinct_sum,
                    base=base,
                    analysis_date=analysis_date,
                )
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            _fail("TEMP_CLEANUP_FAILED", "private reader scratch remains")
    return family_receipts, stats


def _provenance_result(
    base: dict[str, Any], families: dict[str, Any], stats: dict[str, int],
) -> dict[str, Any]:
    extraction_contract = {
        "engine": "duckdb",
        "engine_version": duckdb.__version__,
        "families": list(FAMILIES),
        "required_columns": {
            "ts_utc": "BIGINT",
            "market_ticker": "VARCHAR",
        },
        "date_interval": "[D_00_UTC,D_PLUS_1_00_UTC)",
        "hive_partitioning": False,
        "union_by_name": False,
        "external_access": False,
        "one_object_at_a_time": True,
        "duckdb_spill_scope": "PRIVATE_EPHEMERAL_SCRATCH",
    }
    family_digest_rows = [
        {
            "family": family,
            "receipt_sha256": canonical_sha256(families[family]),
        }
        for family in FAMILIES
    ]
    result = {
        "schema": SCHEMA,
        "state": STATE,
        "verification_state": "BASE_BOUND_PARQUET_BODIES_LOCALLY_VERIFIED",
        "analysis_date": base["date"],
        "base_binding_sha256": base["binding_sha256"],
        "matching_policy": MATCHING_POLICY,
        "date_policy": DATE_POLICY,
        "extraction_contract": extraction_contract,
        "extraction_contract_sha256": canonical_sha256(extraction_contract),
        "families": families,
        "family_receipt_set_sha256": canonical_sha256(family_digest_rows),
        "source_object_count": stats["files"],
        "source_total_bytes": stats["bytes"],
        "all_base_family_objects_present": True,
        "all_input_bodies_omitted_from_output": True,
        "source_objects_exact_get_verified": False,
        "exact_get_attestation_state": "NOT_ATTESTED_BY_LOCAL_BODY_VERIFIER",
        "ephemeral_input_parquet_files_written": stats["files"],
        "ephemeral_input_parquet_bytes_written": stats["bytes"],
        "ephemeral_input_parquet_peak_file_bytes": stats["peak"],
        "ephemeral_temp_deleted_before_return": True,
        "durable_data_objects_copied": 0,
        "aws_read_performed_by_module": False,
        "aws_write_authorized": False,
        "research_eligible": False,
        "research_ready": False,
    }
    result["provenance_sha256"] = canonical_sha256(result)
    return result


def _base_binding(
    manifest_bytes: Any, manifest_exact_identity: Any, date: Any,
) -> dict[str, Any]:
    try:
        return base_binding.build_base_binding(
            manifest_bytes=manifest_bytes,
            manifest_exact_identity=manifest_exact_identity,
            date=date,
        )
    except base_binding.FreshRfqBaseBindingError as exc:
        _fail("BASE_BINDING_INVALID", str(exc))


def _derive_universe_provenance(
    *,
    manifest_bytes: Any,
    manifest_exact_identity: Any,
    date: Any,
    orderbooks_l1_objects: Any,
    orderbooks_full_objects: Any,
) -> dict[str, Any]:
    base = _base_binding(manifest_bytes, manifest_exact_identity, date)
    normalized = {
        "orderbooks_l1": _normalize_family_inputs(
            orderbooks_l1_objects,
            "orderbooks_l1",
            base["families"]["orderbooks_l1"],
            base["families"]["orderbooks_full"],
        ),
        "orderbooks_full": _normalize_family_inputs(
            orderbooks_full_objects,
            "orderbooks_full",
            base["families"]["orderbooks_full"],
            base["families"]["orderbooks_l1"],
        ),
    }
    families, stats = _extract_families(normalized, base, base["date"])
    return _provenance_result(base, families, stats)


def _derive_universe_provenance_from_reader(
    *,
    manifest_bytes: Any,
    manifest_exact_identity: Any,
    date: Any,
    orderbooks_l1_objects: Any,
    orderbooks_full_objects: Any,
    open_exact: Any,
) -> dict[str, Any]:
    base = _base_binding(manifest_bytes, manifest_exact_identity, date)
    # Normalize both exact identity sets before permitting the first callback.
    normalized = {
        "orderbooks_l1": _normalize_family_reader_inputs(
            orderbooks_l1_objects,
            "orderbooks_l1",
            base["families"]["orderbooks_l1"],
            base["families"]["orderbooks_full"],
        ),
        "orderbooks_full": _normalize_family_reader_inputs(
            orderbooks_full_objects,
            "orderbooks_full",
            base["families"]["orderbooks_full"],
            base["families"]["orderbooks_l1"],
        ),
    }
    families, stats = _extract_families_from_reader(
        normalized, base, base["date"], open_exact
    )
    return _provenance_result(base, families, stats)


def build_universe_provenance(
    *,
    manifest_bytes: bytes,
    manifest_exact_identity: dict[str, Any],
    date: str,
    orderbooks_l1_objects: list[dict[str, Any]],
    orderbooks_full_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one body-free L1/L2 universe receipt from exact input bytes."""
    return _derive_universe_provenance(
        manifest_bytes=manifest_bytes,
        manifest_exact_identity=manifest_exact_identity,
        date=date,
        orderbooks_l1_objects=orderbooks_l1_objects,
        orderbooks_full_objects=orderbooks_full_objects,
    )


def build_universe_provenance_from_reader(
    *,
    manifest_bytes: bytes,
    manifest_exact_identity: dict[str, Any],
    date: str,
    orderbooks_l1_objects: list[dict[str, Any]],
    orderbooks_full_objects: list[dict[str, Any]],
    open_exact: Any,
) -> dict[str, Any]:
    """Build the same receipt from body-free identities and a bounded reader.

    ``open_exact(identity)`` must return a context manager whose yielded value
    has a ``.path`` attribute.  The path is consumed only while that context is
    active.  This transport-neutral function verifies the complete identity
    sets before the first callback and never claims that the callback used S3.
    """
    return _derive_universe_provenance_from_reader(
        manifest_bytes=manifest_bytes,
        manifest_exact_identity=manifest_exact_identity,
        date=date,
        orderbooks_l1_objects=orderbooks_l1_objects,
        orderbooks_full_objects=orderbooks_full_objects,
        open_exact=open_exact,
    )


def validate_universe_provenance(
    value: Any,
    *,
    manifest_bytes: bytes,
    manifest_exact_identity: dict[str, Any],
    date: str,
    orderbooks_l1_objects: list[dict[str, Any]],
    orderbooks_full_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild and canonical-type compare a receipt to all exact inputs."""
    value = _exact_keys(value, OUTPUT_FIELDS, "universe provenance")
    supplied_sha = _sha256(value["provenance_sha256"], "provenance_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("provenance_sha256")
    if supplied_sha != canonical_sha256(unsigned):
        _fail("PROVENANCE_DIGEST_MISMATCH", "provenance_sha256 mismatch")
    expected = _derive_universe_provenance(
        manifest_bytes=manifest_bytes,
        manifest_exact_identity=manifest_exact_identity,
        date=date,
        orderbooks_l1_objects=orderbooks_l1_objects,
        orderbooks_full_objects=orderbooks_full_objects,
    )
    if not _canonical_exact_equal(value, expected):
        _fail(
            "PROVENANCE_REBUILD_MISMATCH",
            "receipt differs from the exact-body rebuild",
        )
    return copy.deepcopy(value)


def validate_universe_provenance_from_reader(
    value: Any,
    *,
    manifest_bytes: bytes,
    manifest_exact_identity: dict[str, Any],
    date: str,
    orderbooks_l1_objects: list[dict[str, Any]],
    orderbooks_full_objects: list[dict[str, Any]],
    open_exact: Any,
) -> dict[str, Any]:
    """Rebuild a reader-derived receipt with bounded exact-object access."""
    value = _exact_keys(value, OUTPUT_FIELDS, "universe provenance")
    supplied_sha = _sha256(value["provenance_sha256"], "provenance_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("provenance_sha256")
    if supplied_sha != canonical_sha256(unsigned):
        _fail("PROVENANCE_DIGEST_MISMATCH", "provenance_sha256 mismatch")
    expected = _derive_universe_provenance_from_reader(
        manifest_bytes=manifest_bytes,
        manifest_exact_identity=manifest_exact_identity,
        date=date,
        orderbooks_l1_objects=orderbooks_l1_objects,
        orderbooks_full_objects=orderbooks_full_objects,
        open_exact=open_exact,
    )
    if not _canonical_exact_equal(value, expected):
        _fail(
            "PROVENANCE_REBUILD_MISMATCH",
            "receipt differs from the bounded-reader rebuild",
        )
    return copy.deepcopy(value)
