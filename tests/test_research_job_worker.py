#!/usr/bin/env python3
"""Offline contracts for the general Research Inbox planning worker."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.data_catalog import canonical_sha256  # noqa: E402
from research.inbox import create_job, get_job  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402
from research.plugin_api import (  # noqa: E402
    PLUGIN_REGISTRY,
    PluginContractError,
    PluginIntegrityError,
    build_execution_request,
    load_registered_plugin,
)


def _worker_module():
    source = ROOT / "deploy" / "w09" / "research_job_worker.py"
    spec = importlib.util.spec_from_file_location("research_job_worker_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(*, required: list[str] | None = None) -> str:
    required = required or ["L1", "L2", "TRADES", "MARKET_GRAPH"]
    return "\n".join(
        [
            "---",
            "plugin: deep03",
            "date_window:",
            "  start: 2026-07-13",
            "  end: 2026-07-13",
            "data:",
            "  required: [%s]" % ", ".join(required),
            "  forbidden: [RFQ]",
            "budget:",
            "  max_runtime_seconds: 3600",
            "  max_spend_usd: 2",
            "---",
            "# Spread response experiment",
            "",
            "The prose may mention `rm -rf /`; Markdown remains inert data.",
            "",
        ]
    )


def _job(inbox: Path, text: str | None = None):
    text = text or _plan()
    return create_job(
        inbox,
        filename="PLAN.md",
        plan_text=text,
        job_spec=compile_plan(text, "PLAN.md"),
    )


def _catalog(*, families: list[str] | None = None) -> dict:
    families = families or ["L1", "L2", "TRADES", "MARKET_GRAPH"]
    release_id = "2026-07-13__v3ref__seal-1234abcd__pub-1234567890abcdef"
    payload = {
        "schema_version": "research-data-catalog-v1",
        "state": "READY",
        "release_count": 1,
        "dates": ["2026-07-13"],
        "releases": [
            {
                "release_id": release_id,
                "date": "2026-07-13",
                "published_at_utc": "2026-07-14T00:10:00Z",
                "storage_mode": "REFERENCE_V3",
                "version_binding_mode": "CANONICAL_REFERENCE",
                "manifest_sha256": "1" * 64,
                "manifest_version_id": "exact-manifest-version",
                "reference_set_sha256": "2" * 64,
                "object_semantics_sha256": "3" * 64,
                "publication_state_sha256": "4" * 64,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "tl1_status": "UNASSESSED_PENDING_PIPE_W03",
                "data_families": families,
                "channels": ["orderbooks_full", "orderbooks_l1", "trades"],
                "manifest_data_families": families,
                "manifest_channels": ["orderbooks_full", "orderbooks_l1", "trades"],
                "object_count": 17,
                "object_bytes": 123456,
                "manifest_object_count": 17,
                "manifest_object_bytes": 123456,
                "rfq": {
                    "manifest_included": False,
                    "materialized": False,
                    "status": "ABSENT_FROM_RELEASE",
                },
            }
        ],
        "rejected_releases": [],
        "network_reads": 0,
        "copied_bytes": 0,
        "zero_copy": True,
    }
    payload["catalog_sha256"] = canonical_sha256(payload)
    return payload


def test_worker_plans_exact_data_and_never_executes_research(tmp_path, monkeypatch):
    worker = _worker_module()
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    monkeypatch.setattr(worker, "build_catalog", lambda _cache: _catalog())

    result = worker.plan_job(
        inbox_root=inbox,
        cache_root=tmp_path / "cache",
        job_id=job["job_id"],
    )

    assert result["state"] == "READY"
    assert result["research_execution_started"] is False
    assert get_job(inbox, job["job_id"])["status"]["state"] == "READY"
    planning = inbox / "jobs" / job["job_id"] / "PLANNING"
    assert sorted(path.name for path in planning.iterdir()) == [
        "DATA_CATALOG.json",
        "DATA_SELECTION.json",
        "EXECUTION_REQUEST.json",
        "PLANNING_RECEIPT.json",
        "PREFLIGHT.json",
    ]
    selection = json.loads((planning / "DATA_SELECTION.json").read_text())
    request = json.loads((planning / "EXECUTION_REQUEST.json").read_text())
    assert selection["release_ids"] == [
        "2026-07-13__v3ref__seal-1234abcd__pub-1234567890abcdef"
    ]
    assert request["state"] == "PLANNED_AWAITING_AUTHORITY"
    assert request["planning_only"] is True
    assert request["research_execution_started"] is False
    assert request["downstream_authority_required"] is True
    assert request["downstream_one_shot_gate_required"] is True
    assert request["plugin_payload"]["downstream_gate_contract"]["service_type"] == (
        "SYSTEMD_ONESHOT"
    )
    assert request["plugin_payload"]["capabilities"]["direct_execution"] is False
    serialized = json.dumps(request, sort_keys=True).lower()
    assert "rm -rf" not in serialized
    assert '"command"' not in serialized
    assert '"script"' not in serialized


def test_worker_replay_is_idempotent_and_does_not_rescan_catalog(tmp_path, monkeypatch):
    worker = _worker_module()
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    monkeypatch.setattr(worker, "build_catalog", lambda _cache: _catalog())
    first = worker.plan_job(
        inbox_root=inbox, cache_root=tmp_path / "cache", job_id=job["job_id"]
    )
    receipt = Path(first["planning_path"]) / "PLANNING_RECEIPT.json"
    before = receipt.read_bytes()
    monkeypatch.setattr(
        worker,
        "build_catalog",
        lambda _cache: (_ for _ in ()).throw(AssertionError("catalog rescanned")),
    )

    second = worker.plan_job(
        inbox_root=inbox, cache_root=tmp_path / "cache", job_id=job["job_id"]
    )

    assert second["state"] == "READY"
    assert second["idempotent_replay"] is True
    assert second["planning_receipt_sha256"] == first["planning_receipt_sha256"]
    assert receipt.read_bytes() == before


def test_unknown_method_remains_needs_method_without_catalog_or_artifacts(
    tmp_path, monkeypatch
):
    worker = _worker_module()
    text = "# A new experiment with no registered adapter\n"
    inbox = tmp_path / "inbox"
    job = _job(inbox, text)
    monkeypatch.setattr(
        worker,
        "build_catalog",
        lambda _cache: (_ for _ in ()).throw(AssertionError("catalog should not run")),
    )

    result = worker.plan_job(
        inbox_root=inbox, cache_root=tmp_path / "cache", job_id=job["job_id"]
    )

    assert result["state"] == "NEEDS_METHOD"
    assert result["next_action"] == "REGISTER_HASH_PINNED_METHOD_PLUGIN"
    assert get_job(inbox, job["job_id"])["status"]["state"] == "NEEDS_METHOD"
    assert not (inbox / "jobs" / job["job_id"] / "PLANNING").exists()


def test_missing_required_data_atomically_blocks_without_execution_request(
    tmp_path, monkeypatch
):
    worker = _worker_module()
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    monkeypatch.setattr(worker, "build_catalog", lambda _cache: _catalog(families=["L1"]))

    result = worker.plan_job(
        inbox_root=inbox, cache_root=tmp_path / "cache", job_id=job["job_id"]
    )

    assert result["state"] == "BLOCKED"
    planning = Path(result["planning_path"])
    assert (planning / "DATA_SELECTION.json").is_file()
    assert (planning / "PREFLIGHT.json").is_file()
    assert not (planning / "EXECUTION_REQUEST.json").exists()
    assert get_job(inbox, job["job_id"])["status"]["state"] == "BLOCKED"


def test_job_spec_drift_is_refused_before_plugin_or_catalog(tmp_path, monkeypatch):
    worker = _worker_module()
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    spec_path = inbox / "jobs" / job["job_id"] / "JOB_SPEC.json"
    changed = json.loads(spec_path.read_text())
    changed["title"] = "mutated after enqueue"
    spec_path.write_text(
        json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    monkeypatch.setattr(
        worker,
        "build_catalog",
        lambda _cache: (_ for _ in ()).throw(AssertionError("catalog should not run")),
    )

    with pytest.raises(worker.ResearchJobWorkerError, match="differs from exact PLAN"):
        worker.plan_job(
            inbox_root=inbox, cache_root=tmp_path / "cache", job_id=job["job_id"]
        )


def test_plugin_source_is_hash_pinned_and_executable_fields_are_rejected():
    registration = PLUGIN_REGISTRY["deep03"]
    bad_registry = {"deep03": replace(registration, source_sha256="0" * 64)}
    with pytest.raises(PluginIntegrityError, match="SHA-256 mismatch"):
        load_registered_plugin("deep03", registry=bad_registry)

    loaded = load_registered_plugin("deep03")
    bad_loaded = replace(
        loaded,
        adapter=lambda _context: {
            "schema_version": registration.payload_schema,
            "plugin_id": "deep03",
            "plugin_version": "1",
            "command": "python arbitrary.py",
        },
    )
    context = {
        "job_id": "RJOB-20260718T235901123456Z-aaaaaaaaaaaa",
        "plan_sha256": "a" * 64,
        "job_spec_sha256": "b" * 64,
        "catalog_sha256": "c" * 64,
        "data_selection": {"state": "SELECTED", "selection_sha256": "d" * 64},
        "preflight": {"state": "READY", "preflight_sha256": "e" * 64},
        "job_spec": {"plugin_id": "deep03"},
        "automatic_execution_requested": True,
    }
    with pytest.raises(PluginContractError, match="executable field"):
        build_execution_request(bad_loaded, context=context)

