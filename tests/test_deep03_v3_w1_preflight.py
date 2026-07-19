#!/usr/bin/env python3
"""Contracts for the standalone D3-W1 exploratory pre-authority bundle."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "research"))

from deep03_v3_common import (  # noqa: E402
    Deep03InputError,
    MODE,
    validate_explicit_releases,
)
import deep03_v3_w1_preflight as w1_preflight  # noqa: E402
from deep03_v3_w1_preflight import build_preflight  # noqa: E402
from test_w09_exploratory_autoresearch import _materialize_degraded  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, monkeypatch) -> dict:
    cache, rid = _materialize_degraded(tmp_path)
    release = cache / "releases" / rid
    manifest = json.loads((release / "MANIFEST.json").read_text())
    marker = json.loads((release / ".VERIFIED.json").read_text())
    records = validate_explicit_releases(cache, [rid])
    objects = records[0]["objects"]
    expected_object_count = len(objects)
    expected_object_bytes = sum(int(item["size"]) for item in objects)
    monkeypatch.setattr(w1_preflight, "EXPECTED_OBJECT_COUNT", expected_object_count)
    monkeypatch.setattr(w1_preflight, "EXPECTED_OBJECT_BYTES", expected_object_bytes)

    plan = tmp_path / "plan.md"
    audit = tmp_path / "audit.md"
    plan.write_text("# exact plan\n")
    audit.write_text("# independent audit\n")
    plan_sha = _sha(plan)
    audit_sha = _sha(audit)
    w0_id = "D3-W0-2026-07-18.05"
    w1_id = "D3-W1-2026-07-18.05"
    w0 = tmp_path / "W0.json"
    w0.write_text(json.dumps({
        "schema_version": "deep03-w0-release-v1",
        "state": "RELEASE_CANDIDATE",
        "release_id": w0_id,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "expected_evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "expected_object_count": expected_object_count,
        "expected_object_bytes": expected_object_bytes,
        "research_execution_authority": False,
    }, sort_keys=True) + "\n")
    w0_sha = _sha(w0)
    w1 = tmp_path / "W1.json"
    w1.write_text(json.dumps({
        "schema_version": "deep03-w1-release-v1",
        "state": "RELEASE_CANDIDATE",
        "release_id": w1_id,
        "w0_release_id": w0_id,
        "w0_release_sha256": w0_sha,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "mode": MODE,
        "expected_evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "expected_object_count": expected_object_count,
        "expected_object_bytes": expected_object_bytes,
        "authorized_input_release_ids": [rid],
        "authorized_input_dates": [manifest["date"]],
        "rfq_included": False,
        "strict_acceptance_claimed": False,
        "holdout_opened": False,
        "research_execution_authority": False,
    }, sort_keys=True) + "\n")
    canary = tmp_path / "canary.json"
    canary.write_text(json.dumps({
        "state": "W09_V3_EXPLORATORY_QUERY_CANARY_PASS",
        "mode": MODE,
        "release_id": rid,
        "date": manifest["date"],
        "evidence_tier": marker["evidence_tier"],
        "strict_acceptance_claimed": False,
        "rfq": "OFF",
        "table_count": 3,
    }, sort_keys=True) + "\n")
    return {
        "cache_root": cache,
        "output_dir": tmp_path / "w1-bundle",
        "plan_path": plan,
        "plan_sha256": plan_sha,
        "audit_path": audit,
        "audit_sha256": audit_sha,
        "w0_release_path": w0,
        "w0_release_sha256": w0_sha,
        "w0_release_id": w0_id,
        "w1_release_path": w1,
        "w1_release_sha256": _sha(w1),
        "w1_release_id": w1_id,
        "release_ids": [rid],
        "canary_paths": [canary],
    }


def test_w1_preflight_binds_exact_inputs_and_closes_no_holdout(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    completion_path = build_preflight(**args)
    root = completion_path.parent
    completion = json.loads(completion_path.read_text())
    inputs = json.loads((root / "INPUT_MANIFEST.json").read_text())
    dq = json.loads((root / "DATA_QUALITY_RECEIPT.json").read_text())
    split = json.loads((root / "SPLIT_MANIFEST_OPEN_DISCOVERY.json").read_text())
    prior = [
        json.loads(line)
        for line in (root / "PRIOR_EXPOSURE_LEDGER.jsonl").read_text().splitlines()
    ]

    assert completion["state"] == "W1_COMPLETE_EXPLORATORY_PRECHECK"
    assert inputs["schema_version"] == "deep03-w1-v3-input-manifest-v1"
    assert "authority_binding" not in inputs
    assert completion["research_execution_started"] is False
    assert completion["holdout_opened"] is False
    assert completion["strict_acceptance_claimed"] is False
    assert completion["rfq"] == "OFF_AND_ABSENT"
    assert dq["state"] == "W1_DQ_PASS_FOR_EXPLORATORY_ONLY"
    assert dq["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert dq["evidence_tier_counts"] == {"SEALED_DEGRADED_EVIDENCE": 1}
    assert inputs["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert completion["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert completion["embedded_data_quality_receipt"] == dq
    assert completion["artifacts_sha256"]["DATA_QUALITY_RECEIPT.json"] == _sha(
        root / "DATA_QUALITY_RECEIPT.json"
    )
    assert split["validation_release_ids"] == []
    assert split["confirmation_release_ids"] == []
    assert all(row["exposure_class"] == "PRIOR_EXPOSED" for row in prior)
    for name, digest in completion["artifacts_sha256"].items():
        assert _sha(root / name) == digest
    with pytest.raises(Deep03InputError, match="already exists"):
        build_preflight(**args)


def test_w1_preflight_rejects_canary_or_release_scope_drift(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    canary_path = args["canary_paths"][0]
    canary = json.loads(canary_path.read_text())
    canary["rfq"] = "ON"
    canary_path.write_text(json.dumps(canary) + "\n")
    with pytest.raises(Deep03InputError, match="canary contract mismatch"):
        build_preflight(**args)

    args = _fixture(tmp_path / "scope", monkeypatch)
    w1_path = args["w1_release_path"]
    w1 = json.loads(w1_path.read_text())
    w1["holdout_opened"] = True
    w1_path.write_text(json.dumps(w1, sort_keys=True) + "\n")
    args["w1_release_sha256"] = _sha(w1_path)
    with pytest.raises(Deep03InputError, match="scope/binding mismatch"):
        build_preflight(**args)


def test_w1_preflight_rejects_plan_or_audit_sha_drift(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    bad = copy.deepcopy(args)
    bad["plan_sha256"] = "0" * 64
    with pytest.raises(Deep03InputError, match="adopted plan SHA-256 mismatch"):
        build_preflight(**bad)


def test_w1_preflight_rejects_release_semantic_drift(tmp_path, monkeypatch):
    args = _fixture(tmp_path, monkeypatch)
    w0_path = args["w0_release_path"]
    w0 = json.loads(w0_path.read_text())
    w0["expected_object_count"] += 1
    w0_path.write_text(json.dumps(w0, sort_keys=True) + "\n")
    args["w0_release_sha256"] = _sha(w0_path)
    with pytest.raises(Deep03InputError, match="W0 release semantic lock mismatch"):
        build_preflight(**args)
