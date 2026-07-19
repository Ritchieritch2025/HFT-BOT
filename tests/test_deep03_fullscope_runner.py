#!/usr/bin/env python3
"""Safety and bundle contracts for the base + L2 full-scope runner."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import deep03_fullscope_runner as runner  # noqa: E402
from deep03_v3_common import Deep03InputError  # noqa: E402


def _manifest(tmp_path: Path, run_id: str = "fullscope-fixture") -> dict:
    objects = []
    releases = []
    for index, date in enumerate(runner.L2_SCOPE_DATES):
        release_id = f"{date}__v3ref__fixture-{index}"
        releases.append(
            {
                "release_id": release_id,
                "date": date,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "tl1_status": "TL1",
                "evidence_basis": {"downgrade_reasons": ["fixture"]},
            }
        )
        for channel, size in (("orderbooks_l1", 10), ("trades", 20)):
            objects.append(
                {
                    "release_id": release_id,
                    "date": date,
                    "kind": "facts",
                    "channel": channel,
                    "logical_key": (
                        f"warehouse/facts/{channel}/category=Sports/"
                        f"date={date}/part.parquet"
                    ),
                    "local_path": str(tmp_path / f"{date}-{channel}.parquet"),
                    "source_version_id": f"version-{date}-{channel}",
                    "sha256": hashlib.sha256(f"{date}-{channel}".encode()).hexdigest(),
                    "size": size,
                    "row_count": 1,
                }
            )
        if date in runner.L2_CAPTURE_DATES:
            objects.append(
                {
                    "release_id": release_id,
                    "date": date,
                    "kind": "facts",
                    "channel": "orderbooks_full",
                    "logical_key": (
                        "warehouse/facts/orderbooks_full/category=Sports/"
                        f"date={date}/part.parquet"
                    ),
                    "local_path": str(tmp_path / f"{date}-l2.parquet"),
                    "source_version_id": f"version-{date}-l2",
                    "sha256": hashlib.sha256(f"{date}-l2".encode()).hexdigest(),
                    "size": 30,
                    "row_count": 1,
                }
            )
            quality = tmp_path / f"quality-{date}.json"
            quality.write_text(
                json.dumps(
                    {
                        "date": date,
                        "lines": 1,
                        "parse_errors": 0,
                        "seq_gap_events": 0,
                        "seq_missed_total": 0,
                        "sids_with_seq_gaps": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            objects.append(
                {
                    "release_id": release_id,
                    "date": date,
                    "kind": "l2_quality_receipt",
                    "channel": None,
                    "logical_key": f"control/quality/v1/date={date}/l2_gaps.json",
                    "local_path": str(quality),
                    "source_version_id": f"quality-version-{date}",
                    "sha256": hashlib.sha256(quality.read_bytes()).hexdigest(),
                    "size": quality.stat().st_size,
                    "row_count": None,
                }
            )
    return {
        "run_id": run_id,
        "release_ids": [row["release_id"] for row in releases],
        "release_dates": list(runner.L2_SCOPE_DATES),
        "rfq_policy": "FORBIDDEN_AND_ABSENT",
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "releases": releases,
        "objects": objects,
        "object_count": len(objects),
        "object_bytes": sum(row["size"] for row in objects),
        "sealed_fact_rows": sum(
            int(row["row_count"] or 0) for row in objects if row["kind"] == "facts"
        ),
        "fact_objects_without_row_count": 0,
    }


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


def _l2_result(binding: str) -> dict:
    stages = [
        {
            "stage": stage,
            "stage_version": f"{stage}-fixture-v1",
            "manifest_sha256": hashlib.sha256(stage.encode()).hexdigest(),
            "partition_count": 1,
            "row_count": 1,
        }
        for stage in sorted(runner.REQUIRED_L2_STAGES)
    ]
    return {
        "schema_version": runner.L2_EXECUTION_SCHEMA,
        "state": "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS",
        "claim_tier": "DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL",
        "source_binding": binding,
        "quality": {
            date: {
                "state": "PASS",
                "receipt_state": "PASS",
                "analysis_disposition": (
                    "INCLUDED_CLEAN_DATE"
                    if date in runner.L2_ANALYSIS_DATES
                    else "EXCLUDED_DATA_QUALITY"
                ),
                "usable_for_estimands": date in runner.L2_ANALYSIS_DATES,
                "logical_key": f"quality/{date}",
                "source_version_id": f"quality-version-{date}",
                "sha256": "a" * 64,
                "blockers": (
                    [] if date in runner.L2_ANALYSIS_DATES
                    else ["independent_quality_audit:fixture exclusion"]
                ),
            }
            for date in runner.L2_CAPTURE_DATES
        },
        "availability": {
            "captured_dates": list(runner.L2_CAPTURE_DATES),
            "explicit_absent_dates": list(runner.L2_ABSENT_DATES),
            "included_clean_dates": list(runner.L2_ANALYSIS_DATES),
            "excluded_data_quality_dates": sorted(runner.L2_KNOWN_EXCLUDED_DATES),
        },
        "row_conservation": {
            field: {
                "state": "PASS",
                "label": field,
                "context": "fixture",
                "observed_rows": 6,
                "expected_rows": 6,
            }
            for field in (
                "all_captured_physical_coverage",
                "included_clean_replay",
                "replay_classification",
                "included_plus_excluded_coverage",
            )
        },
        "episode_rows": 1,
        "atlas_rows": 1,
        "matched_control_rows": 0,
        "activity": {},
        "stages": stages,
        "limitations": ["fixture has no PnL authority"],
    }


def _report_tables(binding: str) -> dict:
    return {
        "schema_version": "deep03-fullscope-l2-report-tables-v1",
        "state": "COMPLETE",
        "claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
        "source_binding": binding,
        "coverage_by_date": [
            {
                "date": date,
                "market_days": 1,
                "l1_rows": 1,
                "trade_rows": 1,
                "l2_rows": 0 if date in runner.L2_ABSENT_DATES else 1,
                "l1_markets": 1,
                "trade_markets": 1,
                "l2_markets": 0 if date in runner.L2_ABSENT_DATES else 1,
            }
            for date in runner.L2_SCOPE_DATES
        ],
        "mapping_status": [{"mapping_status": "CANONICAL_FAMILY_EVENT", "market_days": 8}],
        "availability": [
            {
                "date": date,
                "state": (
                    "ABSENT_NOT_CAPTURED"
                    if date in runner.L2_ABSENT_DATES
                    else "CAPTURED_SEQUENCE_RECEIPT_PASS"
                ),
            }
            for date in runner.L2_SCOPE_DATES
        ],
        "quality": [{"date": date, "state": "PASS"} for date in runner.L2_CAPTURE_DATES],
        "atlas_summary": [{"record_kind": "STATE", "strata_rows": 1, "represented_rows": 1}],
        "stage_summary": [{"stage": stage, "row_count": 1} for stage in sorted(runner.REQUIRED_L2_STAGES)],
        "limitations": ["fixture has no PnL authority"],
    }


def _write_audit(
    tmp_path: Path,
    manifest: dict,
    prepare_sha: str,
    runtime_commit_path: Path,
    *,
    blockers: list[str] | None = None,
) -> tuple[Path, Path]:
    report = tmp_path / "l2-independent-audit.md"
    report.write_text("# Independent L2 audit\n\nFixture PASS.\n", encoding="utf-8")
    sources = runner.fullscope_source_hashes()
    receipt = {
        "schema_version": runner.L2_AUDIT_SCHEMA,
        "state": "PASS",
        "decision": runner.L2_AUDIT_DECISION,
        "auditor_id": "independent-fixture-auditor",
        "auditor_independence_attested": True,
        "audited_runtime_commit": runtime_commit_path.read_text().strip(),
        "audited_modules_sha256": {
            f"tools/research/{name}": sources[f"tools/research/{name}"]
            for name in runner.FULLSCOPE_SOURCE_MODULES
        },
        "input_manifest_sha256": prepare_sha,
        "input_release_ids": manifest["release_ids"],
        "l2_quality_objects": runner._expected_l2_quality_objects(manifest),
        "real_quality_receipts_assessed": True,
        "blocker_classes_checked": list(runner.L2_AUDIT_BLOCKER_CLASSES),
        "blockers": [] if blockers is None else blockers,
        "approved_claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
        "rfq_scope": "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED",
        "audit_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    }
    receipt_path = tmp_path / "l2-independent-audit.json"
    receipt_path.write_bytes(runner._canonical_json(receipt))
    return receipt_path, report


class _FakeStore:
    def __init__(self, root: Path, source_binding: str):
        self.root = Path(root)
        self.source_binding = source_binding

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _setup_success(tmp_path: Path, monkeypatch):
    manifest = _manifest(tmp_path)
    prepare_sha = "f" * 64
    runtime_commit = tmp_path / "runtime-commit.txt"
    runtime_commit.write_text("a" * 40 + "\n", encoding="ascii")
    audit_receipt, audit_report = _write_audit(
        tmp_path, manifest, prepare_sha, runtime_commit
    )
    authority = {
        "binding": {
            "authorized_method_scope": {
                **{method: "PARTIAL_DESCRIPTIVE_ONLY" for method in runner.METHODS},
                **runner.FULLSCOPE_AUTHORITY_SCOPE,
            }
        },
        "artifact_sha256s": {},
    }
    monkeypatch.setattr(runner, "load_authority_context", lambda **_kwargs: authority)
    monkeypatch.setattr(
        runner,
        "ensure_run_inputs_current",
        lambda _run_dir, _authority: (manifest, {"input_manifest_sha256": prepare_sha}),
    )
    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10**9, used=0, free=10**9),
    )

    def fake_bounded(_con, value, _namespace, *, market_buckets):
        binding = runner.bounded_source_binding(value)
        return (
            {"fixture": True},
            _closed_methods(),
            {
                "schema_version": runner.BOUNDED_EXECUTION_RECEIPT_SCHEMA,
                "state": "COMPLETE",
                "source_binding": binding,
                "stage_abi": {"market_bucket_count": market_buckets},
                "stages": [],
            },
        )

    def fake_graph(_con, value, *, output_dir):
        output_dir.mkdir(parents=True)
        graph_path = output_dir / "MARKET_GRAPH.parquet"
        graph_path.write_bytes(b"fixture-market-graph")
        digest = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        return {
            "schema_version": runner.GRAPH_SCHEMA,
            "method_id": runner.GRAPH_METHOD_ID,
            "status": "EXECUTED_DESCRIPTIVE_ONLY",
            "release_dates": value["release_dates"],
            "l2_present_dates": list(runner.L2_CAPTURE_DATES),
            "l2_absent_dates": list(runner.L2_ABSENT_DATES),
            "channel_by_date": _report_tables("unused")["coverage_by_date"],
            "mapping_status": _report_tables("unused")["mapping_status"],
            "claims": {"arbitrage_or_pnl": False},
            "artifacts": {
                "market_graph": {
                    "path": str(graph_path),
                    "bytes": graph_path.stat().st_size,
                    "sha256": digest,
                }
            },
        }

    monkeypatch.setattr(runner, "execute_all_bounded", fake_bounded)
    monkeypatch.setattr(runner, "build_market_graph", fake_graph)
    monkeypatch.setattr(runner, "BoundedCheckpointStore", _FakeStore)
    monkeypatch.setattr(
        runner,
        "execute_l2_snbd_bounded",
        lambda _con, value, store, *, market_buckets: _l2_result(store.source_binding),
    )
    monkeypatch.setattr(
        runner,
        "_l2_report_tables",
        lambda _con, store, _l2, _graph: _report_tables(store.source_binding),
    )
    run_dir = tmp_path / "runs" / manifest["run_id"]
    run_dir.mkdir(parents=True)
    (run_dir / "PREPARE_RECEIPT.json").write_text("{}\n", encoding="ascii")
    common = {
        "run_dir": run_dir,
        "authority_path": tmp_path / "authority.json",
        "arm_path": tmp_path / "arm.json",
        "arm_claim_root": tmp_path / "claims",
        "plan_path": tmp_path / "plan.md",
        "runtime_commit_path": runtime_commit,
        "audit_path": tmp_path / "narrow-audit.md",
        "w0_release_path": tmp_path / "w0.json",
        "w1_release_path": tmp_path / "w1.json",
        "w1_complete_path": tmp_path / "w1-complete.json",
        "l2_independent_audit_receipt_path": audit_receipt,
        "l2_independent_audit_report_path": audit_report,
        "checkpoint_root": tmp_path / "checkpoints",
        "checkpoint_reserve_bytes": 7,
        "memory_limit": "1GB",
        "threads": 1,
        "l2_market_buckets": 1,
        "required_checkpoint_parent": tmp_path / "checkpoints",
    }
    return manifest, run_dir, common


def test_disk_gate_includes_conservative_l2_budget(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    namespace = tmp_path / "checkpoints" / "source-a"
    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=0, free=2407),
    )
    receipt = runner._fullscope_disk_headroom_preflight(
        input_manifest=manifest,
        namespace=namespace,
        source_binding="a" * 64,
        reserve_bytes=7,
    )
    assert receipt["exact_l1_trade_source_bytes"] == 240
    assert receipt["exact_l2_source_bytes"] == 180
    assert receipt["predicted_checkpoint_bytes"] == 2400
    assert receipt["required_free_bytes"] == 2407

    monkeypatch.setattr(
        runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=7594, free=2406),
    )
    with pytest.raises(Deep03InputError, match="full-scope checkpoint disk gate failed"):
        runner._fullscope_disk_headroom_preflight(
            input_manifest=manifest,
            namespace=namespace,
            source_binding="a" * 64,
            reserve_bytes=7,
        )


@pytest.mark.parametrize("value", [False, 0, 257, 1.5])
def test_l2_bucket_bound_is_checked_before_execution(value):
    with pytest.raises(Deep03InputError, match="bucket count"):
        runner._validate_l2_market_buckets(value)


def test_old_b01_b04_authority_cannot_start_fullscope(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "PREPARE_RECEIPT.json").write_text("{}\n")
    monkeypatch.setattr(
        runner,
        "load_authority_context",
        lambda **_kwargs: {
            "binding": {"authorized_method_scope": {method: "x" for method in runner.METHODS}},
            "artifact_sha256s": {},
        },
    )
    called = False

    def inputs(*_args):
        nonlocal called
        called = True
        return manifest, {"input_manifest_sha256": "f" * 64}

    monkeypatch.setattr(runner, "ensure_run_inputs_current", inputs)
    with pytest.raises(Deep03InputError, match="graph/L2 authority"):
        runner.run_fullscope_discovery(
            run_dir=run_dir,
            authority_path=tmp_path / "authority",
            arm_path=tmp_path / "arm",
            arm_claim_root=tmp_path / "claims",
            plan_path=tmp_path / "plan",
            runtime_commit_path=tmp_path / "commit",
            audit_path=tmp_path / "audit",
            w0_release_path=tmp_path / "w0",
            w1_release_path=tmp_path / "w1",
            w1_complete_path=tmp_path / "w1c",
            l2_independent_audit_receipt_path=tmp_path / "l2-audit",
            l2_independent_audit_report_path=tmp_path / "l2-report",
            checkpoint_root=tmp_path / "checkpoints",
        )
    assert called is False


def test_independent_audit_blocker_refuses_before_any_compute(tmp_path, monkeypatch):
    manifest, run_dir, common = _setup_success(tmp_path, monkeypatch)
    receipt, report = _write_audit(
        tmp_path,
        manifest,
        "f" * 64,
        common["runtime_commit_path"],
        blockers=["FUTURE_CONTROL"],
    )
    common["l2_independent_audit_receipt_path"] = receipt
    common["l2_independent_audit_report_path"] = report
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("compute must not start")

    monkeypatch.setattr(runner, "execute_all_bounded", forbidden)
    with pytest.raises(Deep03InputError, match="blockers"):
        runner.run_fullscope_discovery(**common)
    assert calls == 0
    assert not (run_dir / ".scratch").exists()
    assert not (run_dir / "RUN_COMPLETE.json").exists()


def test_audit_quality_hash_drift_refuses_before_compute(tmp_path, monkeypatch):
    manifest, run_dir, common = _setup_success(tmp_path, monkeypatch)
    receipt_path = common["l2_independent_audit_receipt_path"]
    receipt = json.loads(receipt_path.read_text())
    receipt["l2_quality_objects"][0]["sha256"] = "0" * 64
    receipt_path.write_bytes(runner._canonical_json(receipt))
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(runner, "execute_all_bounded", forbidden)
    with pytest.raises(Deep03InputError, match="l2_quality_objects"):
        runner.run_fullscope_discovery(**common)
    assert called is False
    assert not (run_dir / "RUN_COMPLETE.json").exists()


def test_l2_validation_accepts_only_the_audited_clean_and_excluded_partition():
    binding = "b" * 64
    result = _l2_result(binding)
    runner._validate_l2_result(result, binding)

    excluded = "2026-07-13"
    result["quality"][excluded]["blockers"] = []
    with pytest.raises(Deep03InputError, match="neither clean nor excluded"):
        runner._validate_l2_result(result, binding)


def test_fullscope_success_emits_coverage_l2_receipts_and_final_complete(
    tmp_path, monkeypatch
):
    _manifest_value, run_dir, common = _setup_success(tmp_path, monkeypatch)
    writes: list[str] = []
    original = runner.atomic_write_json

    def recording(path, value, *, exclusive=False):
        writes.append(Path(path).name)
        return original(path, value, exclusive=exclusive)

    monkeypatch.setattr(runner, "atomic_write_json", recording)
    complete_path = runner.run_fullscope_discovery(**common)
    complete = json.loads(complete_path.read_text())
    assert complete["state"] == "RUN_COMPLETE"
    assert writes[-1] == "RUN_COMPLETE.json"
    assert complete["declared_methods"] == list(runner.FULLSCOPE_METHODS)
    assert complete["rfq_scope"] == "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED"
    assert complete["rfq_reads"] == 0
    assert set(runner.fullscope_source_hashes()).issubset(
        complete["source_modules_sha256"]
    )
    assert (run_dir / "TABLES" / "MARKET_GRAPH.parquet").is_file()
    assert (run_dir / "FULLSCOPE_L2_EXECUTION_RECEIPT.json").is_file()
    assert (run_dir / "L2_INDEPENDENT_AUDIT_RECEIPT.json").is_file()
    report = (run_dir / "REPORT" / "index.html").read_text()
    assert "Full-scope channel coverage" in report
    assert "L2 / SNBD sequence-valid analysis" in report
    assert "RFQ NOT INCLUDED IN THIS EXECUTION UNIT" in report
    assert not (run_dir / ".scratch").exists()


def test_atomic_graph_publish_never_overwrites(tmp_path):
    source = tmp_path / "source.parquet"
    target = tmp_path / "out" / "target.parquet"
    source.write_bytes(b"first")
    target.parent.mkdir()
    target.write_bytes(b"existing")
    with pytest.raises(Deep03InputError, match="already exists"):
        runner._atomic_publish_file(source, target)
    assert source.read_bytes() == b"first"
    assert target.read_bytes() == b"existing"


def test_fullscope_modules_are_sha_pinned_packaged_and_not_auto_started():
    manifest_path = ROOT / "deploy" / "w09" / "deep03_open_discovery_modules.sha256"
    rows = {}
    for line in manifest_path.read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        rows[relative] = digest
    for name in runner.FULLSCOPE_SOURCE_MODULES:
        relative = f"tools/research/{name}"
        assert rows[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    push = (ROOT / "deploy" / "w09" / "push_and_install.sh").read_text()
    install = (ROOT / "deploy" / "w09" / "install_on_host.sh").read_text()
    service = (ROOT / "deploy" / "w09" / "exploratory_autoresearch.py").read_text()
    for name in runner.FULLSCOPE_SOURCE_MODULES:
        assert name in push
        assert name in install
    assert "/usr/local/bin/deep03-v3-fullscope-run" in install
    assert "deep03_fullscope_runner.py" not in service
