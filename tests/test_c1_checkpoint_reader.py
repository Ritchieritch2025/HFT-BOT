#!/usr/bin/env python3
"""Adversarial tests for the read-only frozen C1 checkpoint reader."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import c1_checkpoint_reader as reader  # noqa: E402


SOURCE_BINDING = "a" * 64
STAGE = "synthetic_stage"
STAGE_VERSION = "synthetic-v1"


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha(path: Path) -> str:
    return sha(path.read_bytes())


def write_canonical(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


class Fixture:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "checkpoint"
        self.root.mkdir()
        self.config_path = tmp_path / "config.json"
        self.con = duckdb.connect(":memory:")
        self.keys = ["date=2026-07-12", "date=2026-07-15"]
        for index, key in enumerate(self.keys, start=1):
            data_path = self.root / STAGE / "data" / f"{key}.parquet"
            data_path.parent.mkdir(parents=True, exist_ok=True)
            escaped = str(data_path).replace("'", "''")
            self.con.execute(
                "COPY (SELECT * FROM (VALUES "
                f"({index}, 'v{index}'), ({index + 10}, 'w{index}')) "
                "AS t(id, value)) TO "
                f"'{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        self.rebuild_all()

    def receipt_path(self, key: str) -> Path:
        return self.root / STAGE / "receipts" / f"{key}.json"

    def data_path(self, key: str) -> Path:
        return self.root / STAGE / "data" / f"{key}.parquet"

    def manifest_path(self) -> Path:
        return self.root / STAGE / "MANIFEST.json"

    def make_receipt(self, key: str):
        data_path = self.data_path(key)
        escaped = str(data_path).replace("'", "''")
        schema = [
            {"name": row[0], "type": row[1]}
            for row in self.con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
            ).fetchall()
        ]
        row_count = int(
            self.con.execute(
                f"SELECT count(*) FROM read_parquet('{escaped}')"
            ).fetchone()[0]
        )
        return {
            "schema_version": reader.BOUNDED_CHECKPOINT_SCHEMA,
            "state": "COMPLETE",
            "stage": STAGE,
            "stage_version": STAGE_VERSION,
            "partition_key": key,
            "source_binding": SOURCE_BINDING,
            "data": {
                "path": self.data_path(key).relative_to(self.root).as_posix(),
                "sha256": file_sha(data_path),
                "size_bytes": data_path.stat().st_size,
                "row_count": row_count,
                "schema": schema,
            },
            "metrics": {},
        }

    def rebuild_all(self) -> None:
        for key in self.keys:
            write_canonical(self.receipt_path(key), self.make_receipt(key))
        self.rebuild_manifest()

    def rebuild_manifest(self, *, partitions=None, source_binding=SOURCE_BINDING):
        if partitions is None:
            partitions = []
            for key in self.keys:
                receipt_path = self.receipt_path(key)
                receipt = json.loads(receipt_path.read_text())
                partitions.append({
                    "partition_key": key,
                    "receipt_path": receipt_path.relative_to(self.root).as_posix(),
                    "receipt_sha256": file_sha(receipt_path),
                    "data_sha256": receipt["data"]["sha256"],
                    "row_count": receipt["data"]["row_count"],
                })
        manifest = {
            "schema_version": reader.BOUNDED_STAGE_MANIFEST_SCHEMA,
            "state": "COMPLETE",
            "stage": STAGE,
            "stage_version": STAGE_VERSION,
            "source_binding": source_binding,
            "partition_count": len(partitions),
            "row_count": sum(row["row_count"] for row in partitions),
            "partitions": partitions,
        }
        write_canonical(self.manifest_path(), manifest)
        self.freeze_manifest()
        return manifest

    def freeze_manifest(self):
        self.config_path.write_text(json.dumps({
            "source_binding": SOURCE_BINDING,
            "manifest_sha256": {STAGE: file_sha(self.manifest_path())},
        }))

    def validate(self, **kwargs):
        return reader.validate_stage(
            self.con,
            checkpoint_root=self.root,
            config_path=self.config_path,
            stage=STAGE,
            **kwargs,
        )


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.con.close()


def test_hash_pass_is_deterministic_read_only_and_selection_does_not_skip_validation(fixture):
    result = fixture.validate(selected_partition_keys=[fixture.keys[1]])
    assert result == fixture.validate(selected_partition_keys=[fixture.keys[1]])
    assert result["state"] == "COMPLETE"
    assert result["manifest_sha256"] == file_sha(fixture.manifest_path())
    assert result["validated_partition_count"] == 2
    assert result["validated_row_count"] == 4
    assert result["selected_partition_keys"] == [fixture.keys[1]]
    assert [row["partition_key"] for row in result["selected_partitions"]] == [
        fixture.keys[1]
    ]
    content = dict(result)
    digest = content.pop("validation_sha256")
    assert digest == sha(canonical(content))
    assert not (fixture.root / ".CHECKPOINT_WRITER.lock").exists()

    # The non-selected partition is still fully hashed and scanned.
    fixture.data_path(fixture.keys[0]).write_bytes(b"tampered")
    with pytest.raises(reader.CheckpointValidationError, match="payload size/SHA"):
        fixture.validate(selected_partition_keys=[fixture.keys[1]])


def test_tampered_manifest_fails_external_freeze(fixture):
    fixture.manifest_path().write_bytes(fixture.manifest_path().read_bytes() + b" ")
    with pytest.raises(reader.CheckpointValidationError, match="not canonical|manifest SHA"):
        fixture.validate()


def test_tampered_receipt_fails_manifest_binding(fixture):
    path = fixture.receipt_path(fixture.keys[0])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(reader.CheckpointValidationError, match="not canonical|receipt SHA"):
        fixture.validate()


def test_tampered_parquet_fails_payload_hash(fixture):
    fixture.data_path(fixture.keys[0]).write_bytes(b"not parquet")
    with pytest.raises(reader.CheckpointValidationError, match="payload size/SHA"):
        fixture.validate()


def test_receipt_path_traversal_is_rejected(fixture):
    manifest = json.loads(fixture.manifest_path().read_text())
    manifest["partitions"][0]["receipt_path"] = "../outside.json"
    write_canonical(fixture.manifest_path(), manifest)
    fixture.freeze_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="safe relative|escapes"):
        fixture.validate()


def test_data_path_traversal_is_rejected(fixture):
    key = fixture.keys[0]
    receipt = json.loads(fixture.receipt_path(key).read_text())
    receipt["data"]["path"] = "../outside.parquet"
    write_canonical(fixture.receipt_path(key), receipt)
    fixture.rebuild_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="safe relative|escapes"):
        fixture.validate()


def test_duplicate_partition_key_is_rejected(fixture):
    manifest = json.loads(fixture.manifest_path().read_text())
    duplicate = copy.deepcopy(manifest["partitions"][0])
    fixture.rebuild_manifest(partitions=[manifest["partitions"][0], duplicate])
    with pytest.raises(reader.CheckpointValidationError, match="not unique"):
        fixture.validate()


def test_manifest_partition_and_row_count_conservation_are_enforced(fixture):
    manifest = json.loads(fixture.manifest_path().read_text())
    manifest["row_count"] += 1
    write_canonical(fixture.manifest_path(), manifest)
    fixture.freeze_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="row_count conservation"):
        fixture.validate()


def test_wrong_parquet_schema_is_rejected_even_when_hashes_pass(fixture):
    key = fixture.keys[0]
    receipt = json.loads(fixture.receipt_path(key).read_text())
    receipt["data"]["schema"][0]["type"] = "VARCHAR"
    write_canonical(fixture.receipt_path(key), receipt)
    fixture.rebuild_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="Parquet schema mismatch"):
        fixture.validate()


def test_wrong_parquet_row_count_is_rejected_even_when_ledgers_conserve(fixture):
    key = fixture.keys[0]
    receipt = json.loads(fixture.receipt_path(key).read_text())
    receipt["data"]["row_count"] += 7
    write_canonical(fixture.receipt_path(key), receipt)
    fixture.rebuild_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="Parquet row_count mismatch"):
        fixture.validate()


def test_wrong_source_binding_is_rejected_at_manifest(fixture):
    fixture.rebuild_manifest(source_binding="b" * 64)
    with pytest.raises(reader.CheckpointValidationError, match="source_binding"):
        fixture.validate()


def test_wrong_receipt_fields_fail_closed(fixture):
    key = fixture.keys[0]
    receipt = json.loads(fixture.receipt_path(key).read_text())
    receipt["state"] = "PARTIAL"
    write_canonical(fixture.receipt_path(key), receipt)
    fixture.rebuild_manifest()
    with pytest.raises(reader.CheckpointValidationError, match="receipt field mismatch"):
        fixture.validate()


def test_unknown_and_duplicate_selected_keys_fail_closed(fixture):
    with pytest.raises(reader.CheckpointValidationError, match="absent"):
        fixture.validate(selected_partition_keys=["missing"])
    with pytest.raises(reader.CheckpointValidationError, match="duplicates"):
        fixture.validate(
            selected_partition_keys=[fixture.keys[0], fixture.keys[0]]
        )
