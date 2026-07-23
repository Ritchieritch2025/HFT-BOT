"""Tests for exact-version input and receipt binding."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.provenance import (  # noqa: E402
    ExactReleaseBinding,
    ExactSourceObject,
    ProvenanceError,
    RunBinding,
    atomic_write_receipt,
    canonical_json_bytes,
    validate_local_payloads,
)


H = "a" * 64


def source(**changes: object) -> ExactSourceObject:
    values: dict[str, object] = {
        "logical_key": "orderbooks_full/date=2026-07-12/part.ndjson",
        "version_id": "v-123",
        "sha256": H,
        "size_bytes": 5,
        "channel": "L2",
        "date": "2026-07-12",
    }
    values.update(changes)
    return ExactSourceObject(**values)  # type: ignore[arg-type]


def release(**changes: object) -> ExactReleaseBinding:
    values: dict[str, object] = {
        "release_id": "2026-07-12__v3",
        "date": "2026-07-12",
        "manifest_sha256": "b" * 64,
        "manifest_version_id": "manifest-version-1",
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "objects": (source(),),
    }
    values.update(changes)
    return ExactReleaseBinding(**values)  # type: ignore[arg-type]


def test_source_requires_version_hash_and_safe_key():
    with pytest.raises(ProvenanceError, match="version_id"):
        source(version_id="")
    with pytest.raises(ProvenanceError, match="SHA"):
        source(sha256="unknown")
    with pytest.raises(ProvenanceError, match="unsafe"):
        source(logical_key="../escape")


def test_release_rejects_cross_date_and_duplicate_object():
    with pytest.raises(ProvenanceError, match="escaped"):
        release(objects=(source(date="2026-07-13"),))
    row = source()
    with pytest.raises(ProvenanceError, match="duplicate"):
        release(objects=(row, row))


def test_run_binding_hash_is_deterministic_and_release_order_is_frozen():
    values = {
        "schema_version": "pnl-spine-run-binding-v1",
        "run_id": "run-1",
        "code_sha256": "1" * 64,
        "frozen_experiment_sha256": "2" * 64,
        "fee_facts_sha256": "3" * 64,
        "latency_receipt_sha256": "4" * 64,
        "risk_policy_sha256": "5" * 64,
        "terminal_contract_sha256": "6" * 64,
        "releases": (release(),),
    }
    first = RunBinding(**values)
    second = RunBinding(**values)
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64


def test_local_payload_validation_checks_symlink_size_and_sha(tmp_path: Path):
    payload = b"hello"
    good = tmp_path / "materialized.ndjson"
    good.write_bytes(payload)
    bound = source(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    paths = {(bound.logical_key, bound.version_id): good.name}
    assert validate_local_payloads(
        tmp_path, [bound], local_paths=paths
    ) == {"objects_checked": 1, "bytes_checked": 5}
    good.write_bytes(b"HELLO")
    with pytest.raises(ProvenanceError, match="SHA"):
        validate_local_payloads(tmp_path, [bound], local_paths=paths)
    good.write_bytes(payload)
    link = tmp_path / "link.ndjson"
    link.symlink_to(good)
    with pytest.raises(ProvenanceError, match="symlink"):
        validate_local_payloads(
            tmp_path,
            [bound],
            local_paths={(bound.logical_key, bound.version_id): link.name},
        )


def test_atomic_receipt_is_canonical_and_hash_bound(tmp_path: Path):
    path = tmp_path / "RECEIPT.json"
    digest = atomic_write_receipt(path, {"z": 1, "a": [2, 3]})
    raw = path.read_bytes()
    assert raw == canonical_json_bytes({"a": [2, 3], "z": 1})
    assert digest == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw) == {"a": [2, 3], "z": 1}
