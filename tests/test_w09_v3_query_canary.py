#!/usr/bin/env python3
"""Offline positive/negative tests for the manifest-bound W09 query canary."""

import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CANARY_PATH = ROOT / "deploy" / "w09" / "v3_query_canary.py"
sys.path.insert(0, str(ROOT / "tools"))

import research_data  # noqa: E402

from test_research_reference_consumer import build_release, _store_for  # noqa: E402


def _load_canary():
    spec = importlib.util.spec_from_file_location(
        "w09_v3_query_canary_test", CANARY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _materialize(cache: Path, *, with_rfq: bool = False) -> str:
    rid, manifest, source = build_release(with_rfq=with_rfq)
    store = _store_for((rid, manifest, source))
    assert research_data.cmd_fetch(store, str(cache), rid, False) == 0
    return rid


def test_tiny_v3_release_runs_real_duckdb_queries_and_writes_bound_receipt(
        tmp_path):
    canary = _load_canary()
    cache = tmp_path / "cache"
    release_id = _materialize(cache)
    receipt_path = tmp_path / "receipts" / "query-canary.json"

    assert canary.main([
        "--cache", str(cache),
        "--release", release_id,
        "--receipt", str(receipt_path),
    ]) == 0

    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema_version"] == canary.RECEIPT_SCHEMA
    assert receipt["state"] == canary.PASS_STATE
    assert receipt["storage_mode"] == "REFERENCE_V3"
    assert receipt["version_binding_mode"] == "CANONICAL_REFERENCE"
    assert receipt["evidence_tier"] == "SEALED_CONFIRMATION"
    assert receipt["rfq"] == "OFF"
    assert receipt["view_provenance"] == {
        "sha256": receipt["view_provenance"]["sha256"],
        "date": "2026-07-13",
        "release_id": release_id,
    }
    assert receipt["table_count"] == 3
    assert {row["table"] for row in receipt["tables"]} == {
        "orderbooks_l1", "orderbooks_full", "trades",
    }
    for row in receipt["tables"]:
        assert row["source"]["source_version_id"].startswith("version-")
        assert len(row["source"]["sha256"]) == 64
        assert len(row["source"]["binding_sha256"]) == 64
        assert len(row["describe"]["query_sha256"]) == 64
        assert len(row["describe"]["schema_sha256"]) == 64
        assert row["sample"]["rows_returned"] == 1
        assert len(row["sample"]["row_sha256"]) == 64
        assert row["source_stat_stable"] is True
    # Sample values are hashed, not copied into the operational receipt.
    assert "TICKER" not in receipt_path.read_text()
    assert not list(receipt_path.parent.glob(".*.tmp-*"))


def test_wrong_view_date_to_release_binding_refuses_and_replaces_green_receipt(
        tmp_path):
    canary = _load_canary()
    cache = tmp_path / "cache"
    release_id = _materialize(cache)
    receipt_path = tmp_path / "query-canary.json"
    receipt_path.write_text(json.dumps({
        "schema_version": canary.RECEIPT_SCHEMA,
        "state": canary.PASS_STATE,
    }))
    provenance_path = cache / "view" / ".view_provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["verified_releases"]["2026-07-13"] = "wrong-release"
    provenance_path.write_text(json.dumps(provenance))

    assert canary.main([
        "--cache", str(cache),
        "--release", release_id,
        "--receipt", str(receipt_path),
    ]) == 2
    refusal = json.loads(receipt_path.read_text())
    assert refusal["state"] == canary.REFUSED_STATE
    assert "view provenance" in refusal["error"]["message"]


def test_rfq_bearing_v3_release_is_rejected_even_when_rfq_was_not_fetched(
        tmp_path):
    canary = _load_canary()
    cache = tmp_path / "cache"
    release_id = _materialize(cache, with_rfq=True)
    receipt_path = tmp_path / "query-canary.json"

    assert canary.main([
        "--cache", str(cache),
        "--release", release_id,
        "--receipt", str(receipt_path),
    ]) == 2
    refusal = json.loads(receipt_path.read_text())
    assert refusal["state"] == canary.REFUSED_STATE
    assert "RFQ must be OFF" in refusal["error"]["message"]


def test_source_local_key_must_remain_bound_to_manifest_content_hash(tmp_path):
    canary = _load_canary()
    cache = tmp_path / "cache"
    release_id = _materialize(cache)
    release = cache / "releases" / release_id
    manifest = json.loads((release / "MANIFEST.json").read_text())
    fact = sorted(
        (row for row in canary.research_reference.validate_manifest(
            manifest, release_id)["objects"] if row["kind"] == "facts"),
        key=lambda row: row["logical_key"],
    )[0]
    local = release.joinpath(*fact["local_key"].split("/"))
    local.unlink()
    local.symlink_to(release / "MANIFEST.json")
    receipt_path = tmp_path / "query-canary.json"

    assert canary.main([
        "--cache", str(cache),
        "--release", release_id,
        "--receipt", str(receipt_path),
    ]) == 2
    refusal = json.loads(receipt_path.read_text())
    assert "content hash" in refusal["error"]["message"]
