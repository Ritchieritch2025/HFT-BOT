#!/usr/bin/env python3
"""Read-only, fail-closed validation for frozen C1 checkpoint stages.

The Deep03 checkpoint writer owns an exclusive lock and is deliberately not
used here.  This reader treats ``config/c1_real_fill_v1.json`` as the external
trust root: each stage manifest must have the exact SHA-256 frozen there.  It
then follows every manifest -> receipt -> Parquet binding and independently
recomputes the payload hash, size, schema, and row count.

``selected_partition_keys`` only controls which already-validated payloads are
returned to a caller.  It never narrows integrity validation: every manifest
row, receipt, and Parquet payload in the immutable stage is checked first.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


BOUNDED_CHECKPOINT_SCHEMA = "deep03-bounded-checkpoint-v1"
BOUNDED_STAGE_MANIFEST_SCHEMA = "deep03-bounded-stage-manifest-v1"
VALIDATION_RECEIPT_SCHEMA = "c1-checkpoint-validation-receipt-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHECKPOINT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.=-]{0,199}$")
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "c1_real_fill_v1.json"
)


class CheckpointValidationError(RuntimeError):
    """A frozen checkpoint binding or payload failed validation."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CheckpointValidationError(
            f"value is not canonical ASCII JSON: {exc}"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CheckpointValidationError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _require_plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CheckpointValidationError(
            f"{label} must be a plain integer >= {minimum}"
        )
    return value


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise CheckpointValidationError(f"{label} must be a lowercase SHA-256")
    return value


def _read_json(path: Path, label: str, *, canonical: bool) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise CheckpointValidationError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CheckpointValidationError(f"{label} must be a JSON object")
    if canonical and raw != _canonical_json_bytes(value):
        raise CheckpointValidationError(f"{label} is not canonical JSON")
    return value, raw


def _ensure_regular_no_symlink(path: Path, root: Path, label: str) -> None:
    """Reject a symlink in any component below the already-resolved root."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CheckpointValidationError(f"{label} escapes checkpoint root") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CheckpointValidationError(f"{label} contains a symlink")
    if not path.is_file():
        raise CheckpointValidationError(f"{label} is not a regular file")


def _safe_relative_file(root: Path, value: object, label: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CheckpointValidationError(f"{label} must be a non-empty POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise CheckpointValidationError(f"{label} is not a safe relative path")
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CheckpointValidationError(f"{label} escapes checkpoint root") from exc
    _ensure_regular_no_symlink(candidate, root, label)
    return value, candidate


def _duckdb_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _load_config(config_path: Path) -> tuple[dict[str, Any], str]:
    config, raw = _read_json(config_path, "C1 frozen config", canonical=False)
    _require_sha256(config.get("source_binding"), "config source_binding")
    manifest_hashes = config.get("manifest_sha256")
    if not isinstance(manifest_hashes, dict) or not manifest_hashes:
        raise CheckpointValidationError(
            "config manifest_sha256 must be a non-empty object"
        )
    for stage, digest in manifest_hashes.items():
        if not isinstance(stage, str) or CHECKPOINT_KEY_RE.fullmatch(stage) is None:
            raise CheckpointValidationError("config contains an invalid stage key")
        _require_sha256(digest, f"config manifest_sha256[{stage!r}]")
    return config, _sha256_bytes(raw)


def _validate_receipt_metadata(
    *,
    root: Path,
    stage: str,
    stage_version: str,
    source_binding: str,
    ledger_row: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, Path, Path]:
    key = ledger_row.get("partition_key")
    if not isinstance(key, str) or CHECKPOINT_KEY_RE.fullmatch(key) is None:
        raise CheckpointValidationError(
            f"stage {stage} has an invalid partition key"
        )
    receipt_relative, receipt_path = _safe_relative_file(
        root, ledger_row.get("receipt_path"), f"receipt path for {stage}/{key}"
    )
    receipt, receipt_raw = _read_json(
        receipt_path, f"receipt for {stage}/{key}", canonical=True
    )
    receipt_sha = _sha256_bytes(receipt_raw)
    expected_receipt_sha = _require_sha256(
        ledger_row.get("receipt_sha256"),
        f"manifest receipt_sha256 for {stage}/{key}",
    )
    if receipt_sha != expected_receipt_sha:
        raise CheckpointValidationError(
            f"receipt SHA-256 mismatch for {stage}/{key}"
        )

    expected = {
        "schema_version": BOUNDED_CHECKPOINT_SCHEMA,
        "state": "COMPLETE",
        "stage": stage,
        "stage_version": stage_version,
        "partition_key": key,
        "source_binding": source_binding,
    }
    for field, expected_value in expected.items():
        if receipt.get(field) != expected_value:
            raise CheckpointValidationError(
                f"receipt field mismatch for {stage}/{key}: {field}"
            )
    if set(receipt) != {
        "schema_version",
        "state",
        "stage",
        "stage_version",
        "partition_key",
        "source_binding",
        "data",
        "metrics",
    }:
        raise CheckpointValidationError(
            f"receipt field set mismatch for {stage}/{key}"
        )
    if not isinstance(receipt.get("metrics"), dict):
        raise CheckpointValidationError(
            f"receipt metrics must be an object for {stage}/{key}"
        )
    data = receipt.get("data")
    if not isinstance(data, dict) or set(data) != {
        "path",
        "sha256",
        "size_bytes",
        "row_count",
        "schema",
    }:
        raise CheckpointValidationError(
            f"receipt data field set mismatch for {stage}/{key}"
        )
    data_relative, data_path = _safe_relative_file(
        root, data.get("path"), f"data path for {stage}/{key}"
    )
    data_sha = _require_sha256(data.get("sha256"), f"data SHA for {stage}/{key}")
    _require_plain_int(data.get("size_bytes"), f"data size for {stage}/{key}")
    data_row_count = _require_plain_int(
        data.get("row_count"), f"data row_count for {stage}/{key}"
    )
    schema = data.get("schema")
    if not isinstance(schema, list) or any(
        not isinstance(row, dict)
        or set(row) != {"name", "type"}
        or not isinstance(row["name"], str)
        or not row["name"]
        or not isinstance(row["type"], str)
        or not row["type"]
        for row in schema
    ):
        raise CheckpointValidationError(
            f"data schema ledger is invalid for {stage}/{key}"
        )
    if ledger_row.get("data_sha256") != data_sha:
        raise CheckpointValidationError(
            f"manifest/data SHA binding mismatch for {stage}/{key}"
        )
    if ledger_row.get("row_count") != data_row_count:
        raise CheckpointValidationError(
            f"manifest/data row_count binding mismatch for {stage}/{key}"
        )

    # Keep normalized relative paths for the deterministic receipt without
    # leaking host-specific checkpoint roots.
    receipt["_validated_receipt_path"] = receipt_relative
    receipt["_validated_data_path"] = data_relative
    return receipt, receipt_raw, receipt_path, data_path


def _validate_parquet_payload(
    con: Any,
    *,
    stage: str,
    key: str,
    receipt: Mapping[str, Any],
    data_path: Path,
) -> dict[str, Any]:
    data = receipt["data"]
    before_stat = data_path.stat()
    before_sha = _sha256_path(data_path)
    if before_stat.st_size != data["size_bytes"] or before_sha != data["sha256"]:
        raise CheckpointValidationError(
            f"payload size/SHA-256 mismatch for {stage}/{key}"
        )
    relation = (
        f"read_parquet({_duckdb_path(data_path)},hive_partitioning=false)"
    )
    try:
        described = con.execute(
            f"DESCRIBE SELECT * FROM {relation}"
        ).fetchall()
        schema = [{"name": row[0], "type": row[1]} for row in described]
        row_count = int(
            con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
        )
    except Exception as exc:
        raise CheckpointValidationError(
            f"DuckDB could not validate Parquet for {stage}/{key}: {exc}"
        ) from exc
    if schema != data["schema"]:
        raise CheckpointValidationError(
            f"Parquet schema mismatch for {stage}/{key}"
        )
    if row_count != data["row_count"]:
        raise CheckpointValidationError(
            f"Parquet row_count mismatch for {stage}/{key}"
        )

    # Close the hash/scan TOCTOU window: a concurrent replacement must never
    # yield a successful validation receipt.
    after_stat = data_path.stat()
    after_sha = _sha256_path(data_path)
    if (
        before_stat.st_dev != after_stat.st_dev
        or before_stat.st_ino != after_stat.st_ino
        or before_stat.st_size != after_stat.st_size
        or before_stat.st_mtime_ns != after_stat.st_mtime_ns
        or before_sha != after_sha
    ):
        raise CheckpointValidationError(
            f"payload changed during validation for {stage}/{key}"
        )
    return {
        "partition_key": key,
        "receipt_path": receipt["_validated_receipt_path"],
        "receipt_sha256": _sha256_bytes(
            _canonical_json_bytes(
                {
                    name: value
                    for name, value in receipt.items()
                    if not name.startswith("_validated_")
                }
            )
        ),
        "data_path": receipt["_validated_data_path"],
        "data_sha256": data["sha256"],
        "size_bytes": data["size_bytes"],
        "row_count": row_count,
        "schema": schema,
    }


def validate_stage(
    con: Any,
    *,
    checkpoint_root: str | Path,
    stage: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    selected_partition_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate one externally frozen checkpoint stage without any writes.

    All stage payloads are validated.  When ``selected_partition_keys`` is
    supplied, only those validated entries appear in ``selected_partitions``;
    the complete ledger remains in ``partitions`` and the integrity work is
    unchanged.
    """
    if not isinstance(stage, str) or CHECKPOINT_KEY_RE.fullmatch(stage) is None:
        raise CheckpointValidationError("stage is not a valid checkpoint key")
    try:
        root = Path(checkpoint_root).resolve(strict=True)
    except OSError as exc:
        raise CheckpointValidationError(
            f"checkpoint root is unavailable: {exc}"
        ) from exc
    if not root.is_dir():
        raise CheckpointValidationError("checkpoint root is not a directory")

    config, config_sha = _load_config(Path(config_path))
    expected_manifest_sha = config["manifest_sha256"].get(stage)
    if expected_manifest_sha is None:
        raise CheckpointValidationError(
            f"stage is not frozen in config manifest_sha256: {stage}"
        )
    source_binding = config["source_binding"]

    manifest_path = root / stage / "MANIFEST.json"
    _ensure_regular_no_symlink(manifest_path, root, f"manifest for {stage}")
    manifest, manifest_raw = _read_json(
        manifest_path, f"manifest for {stage}", canonical=True
    )
    manifest_sha = _sha256_bytes(manifest_raw)
    if manifest_sha != expected_manifest_sha:
        raise CheckpointValidationError(
            f"frozen manifest SHA-256 mismatch for stage {stage}"
        )
    expected_manifest = {
        "schema_version": BOUNDED_STAGE_MANIFEST_SCHEMA,
        "state": "COMPLETE",
        "stage": stage,
        "source_binding": source_binding,
    }
    for field, expected_value in expected_manifest.items():
        if manifest.get(field) != expected_value:
            raise CheckpointValidationError(
                f"manifest field mismatch for {stage}: {field}"
            )
    if set(manifest) != {
        "schema_version",
        "state",
        "stage",
        "stage_version",
        "source_binding",
        "partition_count",
        "row_count",
        "partitions",
    }:
        raise CheckpointValidationError(f"manifest field set mismatch for {stage}")
    stage_version = manifest.get("stage_version")
    if not isinstance(stage_version, str) or not stage_version:
        raise CheckpointValidationError(
            f"manifest stage_version is invalid for {stage}"
        )
    partition_count = _require_plain_int(
        manifest.get("partition_count"),
        f"manifest partition_count for {stage}",
        minimum=1,
    )
    row_count = _require_plain_int(
        manifest.get("row_count"), f"manifest row_count for {stage}"
    )
    rows = manifest.get("partitions")
    if not isinstance(rows, list) or len(rows) != partition_count:
        raise CheckpointValidationError(
            f"manifest partition_count conservation failed for {stage}"
        )
    keys: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "partition_key",
            "receipt_path",
            "receipt_sha256",
            "data_sha256",
            "row_count",
        }:
            raise CheckpointValidationError(
                f"manifest partition row is invalid for {stage}"
            )
        key = row.get("partition_key")
        if not isinstance(key, str) or CHECKPOINT_KEY_RE.fullmatch(key) is None:
            raise CheckpointValidationError(
                f"manifest partition key is invalid for {stage}"
            )
        keys.append(key)
        _require_plain_int(
            row.get("row_count"), f"manifest partition row_count for {stage}/{key}"
        )
    if len(keys) != len(set(keys)):
        raise CheckpointValidationError(
            f"manifest partition keys are not unique for {stage}"
        )
    if sum(row["row_count"] for row in rows) != row_count:
        raise CheckpointValidationError(
            f"manifest row_count conservation failed for {stage}"
        )

    if selected_partition_keys is None:
        selected = set(keys)
    else:
        supplied = list(selected_partition_keys)
        if any(not isinstance(key, str) for key in supplied):
            raise CheckpointValidationError(
                "selected_partition_keys must contain strings only"
            )
        if len(supplied) != len(set(supplied)):
            raise CheckpointValidationError(
                "selected_partition_keys contains duplicates"
            )
        selected = set(supplied)
        missing = sorted(selected.difference(keys))
        if missing:
            raise CheckpointValidationError(
                f"selected partition keys are absent from {stage}: {missing}"
            )

    validated: list[dict[str, Any]] = []
    for row in rows:
        receipt, _raw, _receipt_path, data_path = _validate_receipt_metadata(
            root=root,
            stage=stage,
            stage_version=stage_version,
            source_binding=source_binding,
            ledger_row=row,
        )
        validated.append(
            _validate_parquet_payload(
                con,
                stage=stage,
                key=row["partition_key"],
                receipt=receipt,
                data_path=data_path,
            )
        )
    validated.sort(key=lambda row: row["partition_key"])
    selected_rows = [
        row for row in validated if row["partition_key"] in selected
    ]
    receipt: dict[str, Any] = {
        "schema_version": VALIDATION_RECEIPT_SCHEMA,
        "state": "COMPLETE",
        "config_sha256": config_sha,
        "stage": stage,
        "stage_version": stage_version,
        "source_binding": source_binding,
        "manifest_path": f"{stage}/MANIFEST.json",
        "manifest_sha256": manifest_sha,
        "partition_count": partition_count,
        "row_count": row_count,
        "validated_partition_count": len(validated),
        "validated_row_count": sum(row["row_count"] for row in validated),
        "selected_partition_keys": sorted(selected),
        "selected_partition_count": len(selected_rows),
        "partitions": validated,
        "selected_partitions": selected_rows,
    }
    receipt["validation_sha256"] = _sha256_bytes(_canonical_json_bytes(receipt))
    return receipt


__all__ = [
    "CheckpointValidationError",
    "DEFAULT_CONFIG_PATH",
    "VALIDATION_RECEIPT_SCHEMA",
    "validate_stage",
]
