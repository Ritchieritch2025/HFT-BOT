#!/usr/bin/env python3
"""Manifest-bound, read-only DuckDB canary for a verified W09 v3 release.

This is deliberately narrower than a research workload.  It proves that the
exact canonical versions admitted by ``research_reference.validate_manifest``
are materialized in the verified cache and can be opened by DuckDB.  It never
discovers files with a glob and never reads RFQ.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat as stat_module
import sys
from typing import Any

import duckdb
import research_reference


RECEIPT_SCHEMA = "w09-v3-duckdb-query-canary-v1"
PASS_STATE = "W09_V3_DUCKDB_QUERY_CANARY_PASS"
REFUSED_STATE = "W09_V3_DUCKDB_QUERY_CANARY_REFUSED"
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
MAX_CONTROL_BYTES = 4 * 1024 * 1024


class CanaryError(RuntimeError):
    """A fail-closed local acceptance failure."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanaryError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CanaryError("non-finite JSON number: %s" % value)


def _read_json(path: Path, limit: int, label: str) -> tuple[dict[str, Any], bytes]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat_module.S_ISREG(opened.st_mode):
            os.close(fd)
            raise CanaryError("%s is not a regular file" % label)
        if opened.st_size <= 0 or opened.st_size > limit:
            os.close(fd)
            raise CanaryError("%s size is outside the accepted bound" % label)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            raw = handle.read(limit + 1)
        if len(raw) != opened.st_size:
            raise CanaryError("%s changed while it was read" % label)
        value = json.loads(
            raw,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_invalid_constant,
        )
    except CanaryError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise CanaryError("%s is unreadable or invalid: %s" % (label, exc)) from exc
    if not isinstance(value, dict):
        raise CanaryError("%s root is not an object" % label)
    return value, raw


def _atomic_json(path: Path, payload: dict[str, Any]) -> str:
    """Replace one receipt atomically and durably; never follow its leaf."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink():
        raise CanaryError("receipt path must not be a symlink")
    raw = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    temporary = parent / (
        ".%s.tmp-%d-%s" % (path.name, os.getpid(), secrets.token_hex(8))
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o640)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(raw).hexdigest()


def _release_dir(cache: Path, release_id: str) -> Path:
    if not RELEASE_RE.fullmatch(release_id) or release_id in {".", ".."}:
        raise CanaryError("release id is not a safe cache component")
    root = cache.absolute()
    release = root / "releases" / release_id
    if os.path.commonpath((str(root), str(release))) != str(root):
        raise CanaryError("release path escapes the cache")
    if not release.is_dir() or release.is_symlink():
        raise CanaryError("verified release directory is missing or linked")
    return release


def _manifest_leaf(release: Path, local_key: str) -> Path:
    if (not local_key or local_key.startswith(("/", "\\"))
            or "\\" in local_key):
        raise CanaryError("manifest local_key is not relative")
    parts = local_key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CanaryError("manifest local_key contains traversal")
    candidate = release.joinpath(*parts)
    release_real = os.path.realpath(release)
    parent_real = os.path.realpath(candidate.parent)
    if (parent_real != release_real
            and not parent_real.startswith(release_real + os.sep)):
        raise CanaryError("manifest local_key parent escapes the release")
    return candidate


def _content_path(cache: Path, digest: str) -> Path:
    if not SHA256_RE.fullmatch(digest):
        raise CanaryError("manifest source SHA-256 is invalid")
    return cache.absolute() / "objects" / "sha256" / digest[:2] / digest


def _source_binding(cache: Path, release: Path, item: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = _manifest_leaf(release, item["local_key"])
    expected = _content_path(cache, item["sha256"])
    if not path.is_symlink():
        raise CanaryError("facts local_key is not a verified content symlink")
    if os.path.realpath(path) != os.path.realpath(expected):
        raise CanaryError("facts local_key is not bound to its content hash")
    if not expected.is_file() or expected.is_symlink():
        raise CanaryError("verified content object is missing or linked")
    stat = expected.stat()
    if stat.st_size != item["size"]:
        raise CanaryError("verified content size differs from manifest")
    source = {
        "logical_key": item["logical_key"],
        "local_key": item["local_key"],
        "source_bucket": item["source_bucket"],
        "source_key": item["source_key"],
        "source_version_id": item["source_version_id"],
        "size": item["size"],
        "sha256": item["sha256"],
        "kind": item["kind"],
        "channel": item["channel"],
    }
    source["binding_sha256"] = _canonical_sha256(source)
    return path, source


def _stable_value(value: Any) -> Any:
    """Type-aware representation used only to hash a sampled row."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanaryError("DuckDB sample contains a non-finite float")
        return {"type": "float", "value": repr(value)}
    if isinstance(value, decimal.Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stable_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    return {"type": type(value).__name__, "value": str(value)}


def _query_kind(path: Path) -> tuple[str, str]:
    name = path.name.lower()
    if name.endswith(".parquet"):
        reader = "read_parquet(?)"
    elif name.endswith((".csv", ".tsv", ".csv.gz", ".tsv.gz")):
        reader = "read_csv_auto(?)"
    else:
        raise CanaryError("unsupported facts file type: %s" % path.suffix.lower())
    return (
        "DESCRIBE SELECT * FROM %s" % reader,
        "SELECT * FROM %s LIMIT 1" % reader,
    )


def _run_table_query(
        connection: duckdb.DuckDBPyConnection,
        cache: Path,
        release: Path,
        descriptor: dict[str, Any],
        table: str,
) -> dict[str, Any]:
    candidates = sorted(
        (item for item in descriptor["objects"]
         if item["kind"] == "facts" and item["channel"] == table),
        key=lambda item: item["logical_key"],
    )
    if not candidates:
        raise CanaryError("manifest table has no facts object: %s" % table)
    path, source = _source_binding(cache, release, candidates[0])
    describe_sql, sample_sql = _query_kind(path)
    source_stat_before = path.stat()
    try:
        schema_rows = connection.execute(describe_sql, [str(path)]).fetchall()
        sample_rows = connection.execute(sample_sql, [str(path)]).fetchall()
    except duckdb.Error as exc:
        raise CanaryError("DuckDB query failed for %s: %s" % (table, exc)) from exc
    source_stat_after = path.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(source_stat_before, field) != getattr(source_stat_after, field)
           for field in stable_fields):
        raise CanaryError("facts source changed while DuckDB was reading it")
    if len(sample_rows) != 1:
        raise CanaryError("LIMIT 1 did not return exactly one row for %s" % table)

    schema = [{
        "column_name": row[0],
        "column_type": row[1],
        "null": row[2],
        "key": row[3],
        "default": row[4],
        "extra": row[5],
    } for row in schema_rows]
    columns = [row["column_name"] for row in schema]
    frozen_columns = descriptor["tables"][table].get("columns")
    if columns != frozen_columns:
        raise CanaryError(
            "DuckDB schema differs from manifest for %s" % table
        )
    normalized_row = [_stable_value(value) for value in sample_rows[0]]
    describe_binding = {
        "sql": describe_sql,
        "parameter": source["local_key"],
        "source_binding_sha256": source["binding_sha256"],
    }
    sample_binding = {
        "sql": sample_sql,
        "parameter": source["local_key"],
        "source_binding_sha256": source["binding_sha256"],
    }
    return {
        "table": table,
        "source": source,
        "manifest_table_schema_sha256": _canonical_sha256(
            descriptor["tables"][table]
        ),
        "describe": {
            "query_sha256": _canonical_sha256(describe_binding),
            "schema": schema,
            "schema_sha256": _canonical_sha256(schema),
        },
        "sample": {
            "query_sha256": _canonical_sha256(sample_binding),
            "rows_returned": 1,
            "row_sha256": _canonical_sha256(normalized_row),
            "value_types": [type(value).__name__ for value in sample_rows[0]],
        },
        "source_stat_stable": True,
    }


def build_receipt(cache: Path, release_id: str) -> dict[str, Any]:
    try:
        cache = cache.resolve(strict=True)
    except OSError as exc:
        raise CanaryError("verified cache is missing or inaccessible") from exc
    if not cache.is_dir():
        raise CanaryError("verified cache is not a directory")
    release = _release_dir(cache, release_id)
    manifest, manifest_raw = _read_json(
        release / "MANIFEST.json", MAX_MANIFEST_BYTES, "v3 manifest"
    )
    try:
        descriptor = research_reference.validate_manifest(manifest, release_id)
    except research_reference.ReferenceManifestError as exc:
        raise CanaryError("manifest contract rejected: %s" % exc) from exc
    marker, marker_raw = _read_json(
        release / ".VERIFIED.json", MAX_CONTROL_BYTES, "verification marker"
    )
    provenance, provenance_raw = _read_json(
        cache / "view" / ".view_provenance.json",
        MAX_CONTROL_BYTES,
        "view provenance",
    )

    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if descriptor.get("storage_mode") != "CANONICAL_REFERENCE":
        raise CanaryError("manifest is not CANONICAL_REFERENCE")
    if descriptor.get("evidence_tier") != "SEALED_CONFIRMATION":
        raise CanaryError("manifest has not earned SEALED_CONFIRMATION")
    if descriptor.get("rfq_included") is not False:
        raise CanaryError("RFQ must be OFF for the standard W09 canary")
    expected_marker = {
        "schema": "research-reference-verified-v1",
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "release_id": release_id,
        "date": descriptor["date"],
        "manifest_sha256": manifest_sha256,
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
        "evidence_tier": "SEALED_CONFIRMATION",
        "rfq_included": False,
        "rfq_status": "ABSENT_FROM_RELEASE",
        "canonical_receipt_verified": True,
    }
    for field, expected in expected_marker.items():
        if marker.get(field) != expected:
            raise CanaryError("verification marker binding mismatch: %s" % field)
    manifest_version_id = marker.get("manifest_version_id")
    if (not isinstance(manifest_version_id, str)
            or not manifest_version_id
            or len(manifest_version_id) > 1024
            or any(ord(character) < 0x20 for character in manifest_version_id)):
        raise CanaryError("verification marker has no manifest VersionId")
    verified_releases = provenance.get("verified_releases")
    if (not isinstance(verified_releases, dict)
            or verified_releases.get(descriptor["date"]) != release_id):
        raise CanaryError("view provenance does not bind date to release id")
    quarantined = provenance.get("quarantined_legacy_overrides")
    if not isinstance(quarantined, dict) or descriptor["date"] in quarantined:
        raise CanaryError("view provenance marks the release as quarantined")

    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET threads=1")
        tables = [
            _run_table_query(connection, cache, release, descriptor, table)
            for table in sorted(descriptor["tables"])
        ]
    finally:
        connection.close()
    if not tables:
        raise CanaryError("validated manifest contains no facts tables")

    sources = [{
        "table": row["table"],
        "binding_sha256": row["source"]["binding_sha256"],
        "sha256": row["source"]["sha256"],
        "source_version_id": row["source"]["source_version_id"],
    } for row in tables]
    queries = [{
        "table": row["table"],
        "describe_query_sha256": row["describe"]["query_sha256"],
        "sample_query_sha256": row["sample"]["query_sha256"],
    } for row in tables]
    manifest_binding = {
        "sha256": manifest_sha256,
        "version_id": manifest_version_id,
        "version_binding_sha256": _canonical_sha256({
            "release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "manifest_version_id": manifest_version_id,
        }),
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
    }
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "state": PASS_STATE,
        "generated_at_utc": _utc_now(),
        "release_id": release_id,
        "date": descriptor["date"],
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "evidence_tier": "SEALED_CONFIRMATION",
        "rfq": "OFF",
        "manifest": manifest_binding,
        "verification_marker_sha256": hashlib.sha256(marker_raw).hexdigest(),
        "view_provenance": {
            "sha256": hashlib.sha256(provenance_raw).hexdigest(),
            "date": descriptor["date"],
            "release_id": release_id,
        },
        "source_set_sha256": _canonical_sha256(sources),
        "query_set_sha256": _canonical_sha256(queries),
        "table_count": len(tables),
        "tables": tables,
    }
    receipt["receipt_payload_sha256"] = _canonical_sha256(receipt)
    return receipt


def _refusal(error: BaseException, release_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "state": REFUSED_STATE,
        "generated_at_utc": _utc_now(),
        "release_id": release_id,
        "error": {
            "class": type(error).__name__,
            "message": str(error),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    receipt_path = Path(args.receipt).absolute()
    try:
        receipt = build_receipt(Path(args.cache), args.release)
        file_sha = _atomic_json(receipt_path, receipt)
    except (CanaryError, OSError, duckdb.Error) as exc:
        try:
            _atomic_json(receipt_path, _refusal(exc, args.release))
        except (CanaryError, OSError):
            pass
        print("W09_V3_QUERY_CANARY_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(
        "%s release=%s tables=%d receipt=%s file_sha256=%s"
        % (PASS_STATE, args.release, receipt["table_count"],
           receipt_path, file_sha)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
