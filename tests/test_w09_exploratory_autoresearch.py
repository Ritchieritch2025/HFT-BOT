#!/usr/bin/env python3
"""Offline contracts for W09 MODE 1 degraded-evidence autoresearch."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
W09 = ROOT / "deploy" / "w09"
TOOLS = ROOT / "tools"
RESEARCH_TOOLS = TOOLS / "research"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(RESEARCH_TOOLS))
sys.path.insert(0, str(W09))

import research_data  # noqa: E402
import research_reference  # noqa: E402
from deep03_v3_common import MODE, validate_explicit_releases  # noqa: E402
from test_research_reference_consumer import (  # noqa: E402
    _store_for,
    build_release,
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _degrade_manifest(manifest):
    manifest = copy.deepcopy(manifest)
    basis = copy.deepcopy(manifest["evidence"]["basis"])
    basis["downgrade_reasons"] = ["UNASSESSED_PENDING_PIPE_W03"]
    manifest["evidence"] = {
        "tier": "SEALED_DEGRADED_EVIDENCE",
        "basis": basis,
    }
    manifest["evidence_tier"] = "SEALED_DEGRADED_EVIDENCE"
    manifest["evidence_tier_basis"] = basis
    manifest["publication_state"]["evidence_tier"] = (
        "SEALED_DEGRADED_EVIDENCE"
    )
    manifest["publication_state"]["evidence_basis_sha256"] = (
        research_reference.canonical_sha256(basis)
    )
    state_sha = research_reference.canonical_sha256(
        manifest["publication_state"]
    )
    rid = "%s__v3ref__seal-%s__pub-%s" % (
        manifest["date"],
        manifest["source_seal"]["sha256"][:8],
        state_sha[:16],
    )
    manifest["release_id"] = rid
    manifest["publication_state_sha256"] = state_sha
    research_reference.validate_manifest(manifest, rid)
    return rid, manifest


def _degraded_release():
    _old_rid, manifest, source = build_release()
    rid, manifest = _degrade_manifest(manifest)
    return rid, manifest, source


def _materialize_degraded(tmp_path: Path) -> tuple[Path, str]:
    cache = tmp_path / "cache"
    old_rid, confirmation, source = build_release()
    store = _store_for((old_rid, confirmation, source))
    assert research_data.cmd_fetch(store, str(cache), old_rid, False) == 0
    rid, degraded = _degrade_manifest(confirmation)
    old_release = cache / "releases" / old_rid
    release = cache / "releases" / rid
    old_release.rename(release)
    manifest_raw = json.dumps(degraded, sort_keys=True, indent=2).encode() + b"\n"
    (release / "MANIFEST.json").write_bytes(manifest_raw)
    marker_path = release / ".VERIFIED.json"
    marker = json.loads(marker_path.read_text())
    marker.update({
        "release_id": rid,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "publication_state_sha256": degraded["publication_state_sha256"],
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "evidence_tier_basis": degraded["evidence_tier_basis"],
    })
    marker_path.write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n")
    assert research_data.cmd_view(
        str(cache), include_non_confirmation=True
    ) == 0
    return cache, rid


def test_exploratory_canary_accepts_degraded_but_strict_stays_red(tmp_path):
    exploratory = _load(
        "w09_exploratory_canary_test",
        W09 / "exploratory_v3_query_canary.py",
    )
    strict = _load("w09_strict_canary_guard", W09 / "v3_query_canary.py")
    cache, rid = _materialize_degraded(tmp_path)
    exploratory_receipt = tmp_path / "exploratory.json"
    strict_receipt = tmp_path / "strict.json"

    assert exploratory.main([
        "--cache", str(cache),
        "--release", rid,
        "--receipt", str(exploratory_receipt),
    ]) == 0
    receipt = json.loads(exploratory_receipt.read_text())
    assert receipt["mode"] == MODE
    assert receipt["state"] == exploratory.PASS_STATE
    assert receipt["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert receipt["strict_acceptance_claimed"] is False
    assert receipt["result_class"] == "EXPLORATORY_ONLY_NOT_STRICT_ACCEPTANCE"
    assert receipt["rfq"] == "OFF"

    assert strict.main([
        "--cache", str(cache),
        "--release", rid,
        "--receipt", str(strict_receipt),
    ]) == 2
    assert json.loads(strict_receipt.read_text())["state"] == strict.REFUSED_STATE


def test_deep03_gate_accepts_degraded_only_as_explicit_mode1(tmp_path):
    cache, rid = _materialize_degraded(tmp_path)
    records = validate_explicit_releases(cache, [rid])
    assert records[0]["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert records[0]["rfq_included"] is False


def test_selector_is_contiguous_rfq_free_and_evidence_bounded(tmp_path):
    selector = _load(
        "w09_exploratory_selector_test",
        W09 / "exploratory_release_selector.py",
    )
    rid, manifest, _source = _degraded_release()
    location = tmp_path / "reference_manifests" / rid
    location.mkdir(parents=True)
    (location / "MANIFEST.json").write_text(json.dumps(manifest))

    selected = selector.select(
        tmp_path,
        start_date="2026-07-13",
        end_date="2026-07-13",
    )
    assert selected["mode"] == MODE
    assert selected["release_ids"] == [rid]
    assert selected["rfq"] == "OFF"
    assert len(selected["selection_sha256"]) == 64

    with pytest.raises(selector.SelectionError, match="missing dates"):
        selector.select(
            tmp_path,
            start_date="2026-07-12",
            end_date="2026-07-13",
        )


def test_autoresearch_cycle_runs_once_then_is_an_idempotent_noop(tmp_path):
    automation = _load(
        "w09_exploratory_automation_test",
        W09 / "exploratory_autoresearch.py",
    )
    rid = "2026-07-13__v3ref__seal-aaaaaaaa__pub-bbbbbbbbbbbbbbbb"
    selection = {
        "schema_version": "w09-exploratory-v3-release-selection-v1",
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "start_date": "2026-07-13",
        "end_date": "2026-07-13",
        "require_contiguous": True,
        "rfq": "OFF",
        "release_ids": [rid],
        "releases": [{
            "release_id": rid,
            "date": "2026-07-13",
            "object_bytes": 0,
        }],
    }
    selection["selection_sha256"] = hashlib.sha256(
        json.dumps(
            selection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    commands: list[list[str]] = []

    def fake_runner(command, **_kwargs):
        command = list(command)
        commands.append(command)
        script = Path(command[1]).name if len(command) > 1 else ""
        if script == "exploratory_release_selector.py":
            output = json.dumps(selection)
        elif script == "exploratory_v3_query_canary.py":
            receipt = Path(command[command.index("--receipt") + 1])
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({
                "state": "W09_V3_EXPLORATORY_QUERY_CANARY_PASS",
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "release_id": rid,
                "rfq": "OFF",
            }))
            output = "canary pass"
        elif script == "deep03_v3_prepare.py":
            run_root = Path(command[command.index("--run-root") + 1])
            run_id = command[command.index("--run-id") + 1]
            (run_root / run_id).mkdir(parents=True)
            output = "prepare pass"
        elif script == "deep03_v3_runner.py":
            run_dir = Path(command[command.index("--run-dir") + 1])
            (run_dir / "RUN_COMPLETE.json").write_text(json.dumps({
                "state": "RUN_COMPLETE",
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "rfq_reads": 0,
                "release_ids": [rid],
            }))
            output = "run pass"
        else:
            output = "ok"
        return subprocess.CompletedProcess(command, 0, stdout=output)

    kwargs = {
        "cache": tmp_path / "cache",
        "state_root": tmp_path / "state",
        "run_root": tmp_path / "runs",
        "start_date": "2026-07-13",
        "end_date": "2026-07-13",
        "python": sys.executable,
        "tools_root": TOOLS,
        "w09_tools_root": W09,
        "hook": tmp_path / "absent-hook",
        "runner": fake_runner,
    }
    first = automation.run_cycle(**kwargs)
    assert first["state"] == "RESEARCH_COMPLETE"
    assert first["idempotent_noop"] is False
    assert any(Path(row[1]).name == "deep03_v3_runner.py" for row in commands)

    commands.clear()
    second = automation.run_cycle(**kwargs)
    assert second["state"] == "RESEARCH_COMPLETE"
    assert second["idempotent_noop"] is True
    assert not any(Path(row[1]).name == "deep03_v3_runner.py" for row in commands)


def test_deployment_payload_and_timer_are_pinned():
    manifest = W09 / "exploratory_autoresearch_payload.sha256"
    rows = []
    for line in manifest.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        rows.append(relative)
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
    assert set(rows) == {
        "deploy/w09/exploratory_v3_query_canary.py",
        "deploy/w09/exploratory_release_selector.py",
        "deploy/w09/exploratory_autoresearch.py",
        "deploy/w09/w09-exploratory-autoresearch.service",
        "deploy/w09/w09-exploratory-autoresearch.timer",
    }
    service = (W09 / "w09-exploratory-autoresearch.service").read_text()
    timer = (W09 / "w09-exploratory-autoresearch.timer").read_text()
    assert "--start-date 2026-07-10" in service
    assert "exploratory_autoresearch.sha256" in service
    assert "OnUnitInactiveSec=30min" in timer
    assert "Persistent=true" in timer
    assert "--with-rfq" not in service


def test_static_credentials_are_refused_without_printing_values(tmp_path, monkeypatch):
    automation = _load(
        "w09_exploratory_credential_refusal_test",
        W09 / "exploratory_autoresearch.py",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-print-this-value")
    with pytest.raises(automation.AutoResearchError) as caught:
        automation.run_cycle(
            cache=tmp_path / "cache",
            state_root=tmp_path / "state",
            run_root=tmp_path / "runs",
            start_date="2026-07-13",
            end_date="2026-07-13",
            python=sys.executable,
            tools_root=TOOLS,
            w09_tools_root=W09,
            hook=tmp_path / "hook",
        )
    assert "never-print-this-value" not in str(caught.value)
