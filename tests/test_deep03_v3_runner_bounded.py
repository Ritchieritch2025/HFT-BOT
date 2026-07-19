#!/usr/bin/env python3
"""Runner contracts for reusable bounded Deep03 checkpoints."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import deep03_v3_runner as runner  # noqa: E402
from deep03_v3_common import Deep03InputError  # noqa: E402


def _manifest(run_id: str, *, source_sha: str = "a" * 64) -> dict:
    release_id = "2026-07-17__v3ref__fixture"
    objects = [
        {
            "release_id": release_id,
            "date": "2026-07-17",
            "kind": "facts",
            "channel": channel,
            "logical_key": (
                "warehouse/facts/%s/category=Sports/subcategory=Tennis/"
                "date=2026-07-17/part.parquet" % channel
            ),
            "local_path": "/verified-cache/%s.parquet" % channel,
            "source_version_id": "exact-version-%s" % channel,
            "sha256": source_sha if channel == "orderbooks_l1" else "b" * 64,
            "size": size,
            "row_count": 1,
        }
        for channel, size in (("orderbooks_l1", 10), ("trades", 20))
    ]
    return {
        "run_id": run_id,
        "release_ids": [release_id],
        "release_dates": ["2026-07-17"],
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "releases": [
            {
                "release_id": release_id,
                "date": "2026-07-17",
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "tl1_status": "TL1",
                "evidence_basis": {"downgrade_reasons": ["fixture"]},
            }
        ],
        "objects": objects,
        "object_count": len(objects),
        "object_bytes": sum(row["size"] for row in objects),
        "sealed_fact_rows": 2,
        "fact_objects_without_row_count": 0,
    }


def test_checkpoint_namespace_excludes_run_and_authority_identity(tmp_path):
    checkpoint_root = tmp_path / "checkpoints"
    first, first_id, first_binding = runner._checkpoint_namespace(
        checkpoint_root=checkpoint_root,
        input_manifest=_manifest("fresh-arm-run-a"),
        run_dir=tmp_path / "runs" / "fresh-arm-run-a",
        required_parent=checkpoint_root,
    )
    second, second_id, second_binding = runner._checkpoint_namespace(
        checkpoint_root=checkpoint_root,
        input_manifest=_manifest("different-arm-run-b"),
        run_dir=tmp_path / "runs" / "different-arm-run-b",
        required_parent=checkpoint_root,
    )
    changed, _, _ = runner._checkpoint_namespace(
        checkpoint_root=checkpoint_root,
        input_manifest=_manifest("run-c", source_sha="c" * 64),
        run_dir=tmp_path / "runs" / "run-c",
        required_parent=checkpoint_root,
    )

    assert first == second
    assert first_id == second_id
    assert first_binding == second_binding
    assert changed != first
    assert "fresh-arm-run" not in first.as_posix()

    with pytest.raises(Deep03InputError, match="must be under"):
        runner._checkpoint_namespace(
            checkpoint_root=tmp_path / "outside",
            input_manifest=_manifest("run-d"),
            run_dir=tmp_path / "runs" / "run-d",
            required_parent=checkpoint_root,
        )


def test_disk_gate_uses_exact_l1_trades_four_x_plus_reserve(
    tmp_path, monkeypatch
):
    namespace = tmp_path / "checkpoints" / "source-a"
    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=0, free=127),
    )
    receipt = runner._disk_headroom_preflight(
        input_manifest=_manifest("run-a"),
        namespace=namespace,
        source_binding="a" * 64,
        reserve_bytes=7,
    )
    assert receipt["exact_l1_trade_source_bytes"] == 30
    assert receipt["checkpoint_expansion_factor"] == 4
    assert receipt["predicted_checkpoint_bytes"] == 120
    assert receipt["required_free_bytes"] == 127
    assert receipt["state"] == "PASS"

    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=874, free=126),
    )
    with pytest.raises(Deep03InputError, match="disk gate failed"):
        runner._disk_headroom_preflight(
            input_manifest=_manifest("run-b"),
            namespace=namespace,
            source_binding="a" * 64,
            reserve_bytes=7,
        )


def _closed_methods() -> list[dict]:
    return [
        {
            "method_id": method_id,
            "title": method_id,
            "status": "NOT_ESTIMABLE",
            "reason": "fixture",
            "queries": [],
            "summary": [],
            "waterfall": [],
            "uncertainty": {"state": "NOT_ESTIMABLE", "reason": "fixture"},
        }
        for method_id in runner.METHODS
    ]


def test_fresh_run_reuses_dataset_namespace_but_failed_run_is_not_retryable(
    tmp_path, monkeypatch
):
    checkpoint_root = tmp_path / "checkpoints"
    run_root = tmp_path / "runs"
    manifests: dict[str, dict] = {}
    authority_calls = 0
    bounded_calls: list[dict] = []

    def fake_authority(**_kwargs):
        nonlocal authority_calls
        authority_calls += 1
        return {"binding": {"fresh_authority_call": authority_calls}, "artifact_sha256s": {}}

    def fake_inputs(run_dir, _authority):
        return manifests[Path(run_dir).name], {"input_manifest_sha256": "f" * 64}

    def fake_bounded(_con, manifest, namespace, *, market_buckets):
        marker = namespace / "durable-stage.marker"
        reused = marker.exists()
        marker.write_text("same exact dataset\n", encoding="ascii")
        receipt = {
            "schema_version": "deep03-bounded-execution-receipt-v1",
            "state": "COMPLETE",
            "source_binding": runner.bounded_source_binding(manifest),
            "stage_abi": {"market_bucket_count": market_buckets},
            "stages": [
                {
                    "stage": "fixture",
                    "reused_partitions": 1 if reused else 0,
                    "written_partitions": 0 if reused else 1,
                }
            ],
        }
        bounded_calls.append({"namespace": namespace, "reused": reused})
        return {"fixture": True}, _closed_methods(), receipt

    monkeypatch.setattr(runner, "load_authority_context", fake_authority)
    monkeypatch.setattr(runner, "ensure_run_inputs_current", fake_inputs)
    monkeypatch.setattr(runner, "execute_all_bounded", fake_bounded)
    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=0, free=10_000),
    )

    common = {
        "authority_path": tmp_path / "AUTHORITY.json",
        "arm_path": tmp_path / "ARM.json",
        "arm_claim_root": tmp_path / "claims",
        "plan_path": tmp_path / "plan.md",
        "runtime_commit_path": tmp_path / "commit.txt",
        "audit_path": tmp_path / "audit.md",
        "w0_release_path": tmp_path / "w0.json",
        "w1_release_path": tmp_path / "w1.json",
        "w1_complete_path": tmp_path / "w1-complete.json",
        "checkpoint_root": checkpoint_root,
        "checkpoint_reserve_bytes": 7,
        "memory_limit": "1GB",
        "threads": 1,
        "required_checkpoint_parent": checkpoint_root,
    }

    for name in ("fresh-a", "fresh-b"):
        run_dir = run_root / name
        run_dir.mkdir(parents=True)
        (run_dir / "PREPARE_RECEIPT.json").write_text("{}\n", encoding="ascii")
        manifests[name] = _manifest(name)
        complete = runner.run_discovery(run_dir=run_dir, **common)
        assert json.loads(complete.read_text())["state"] == "RUN_COMPLETE"

    assert bounded_calls[0]["namespace"] == bounded_calls[1]["namespace"]
    assert bounded_calls[0]["reused"] is False
    assert bounded_calls[1]["reused"] is True
    copied = json.loads(
        (run_root / "fresh-b" / "CHECKPOINT_REUSE_RECEIPT.json").read_text()
    )
    assert copied["bounded_execution"]["stages"][0]["reused_partitions"] == 1
    reproduction = json.loads(
        (run_root / "fresh-b" / "REPRODUCTION_RECEIPT.json").read_text()
    )
    assert reproduction["checkpoint_reuse_receipt_sha256"]
    report = (run_root / "fresh-b" / "REPORT" / "index.html").read_text()
    assert "Execution queries / receipt references" in report
    assert "<h3>Exact queries</h3>" not in report

    failed = run_root / "failed-arm-run"
    failed.mkdir(parents=True)
    (failed / "PREPARE_RECEIPT.json").write_text("{}\n", encoding="ascii")
    (failed / "RUN_FAILED.json").write_text("{}\n", encoding="ascii")
    manifests[failed.name] = _manifest(failed.name)
    calls_before = len(bounded_calls)
    with pytest.raises(Deep03InputError, match="stale/partial artifacts"):
        runner.run_discovery(run_dir=failed, **common)
    assert len(bounded_calls) == calls_before
    # Authority is still checked on every attempt before any checkpoint reuse.
    assert authority_calls == 3
