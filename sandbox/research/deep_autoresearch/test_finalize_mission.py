import importlib.util
import json
from pathlib import Path

import pytest


path = Path(__file__).with_name("finalize_mission.py")
spec = importlib.util.spec_from_file_location("finalize_mission", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def make_run(tmp_path):
    run = tmp_path / "20260715T112538Z__c21a79a8cff__deep01"
    (run / "REPORT/tables").mkdir(parents=True)
    (run / "REPORT/charts").mkdir(parents=True)
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    (run / "queries").mkdir(parents=True)
    (run / "REPORT/charts/chart.png").write_bytes(b"fixture-png")
    (run / "REPORT/tables/data_coverage_cube.csv").write_text(
        "date,channel,rows\n2026-07-12,orderbooks_l1,1\n", encoding="utf-8"
    )
    attestation = {
        "instance_id": "i-0e53d134dceffe166",
        "region": "us-east-2",
        "role": "w09-research-runner",
        "instance_profile": "w09-research-runner",
        "architecture": "aarch64",
        "duckdb": "1.4.5",
        "w09_run_inhibitor_present": True,
        "w09_run_inhibitor_is_ancestor": True,
        "static_credentials_present": False,
        "trading_credentials_present": False,
        "ambient_aws_or_kalshi_variables": [],
        "static_credential_paths_present": [],
        "installation_sha256": mod.EXPECTED_W09_INSTALLATION_SHA256,
        "s3_access": "READ_ONLY_RESEARCH_PREFIX",
    }
    write_json(run / "DATA_INTEGRITY/W09_ATTESTATION.json", attestation)
    frozen_finalizer = run / "queries/finalize_mission.py"
    frozen_finalizer.write_bytes(path.read_bytes())
    query_sums = run / "QUERY_SHA256SUMS.txt"
    query_sums.write_text(
        f"{mod.sha256(frozen_finalizer)}  queries/finalize_mission.py\n",
        encoding="utf-8",
    )
    write_json(run / "SOURCE_MANIFEST.json", [])
    write_json(run / "FEATURE_DICTIONARY.json", {"features": []})
    (run / "METHODS.md").write_text("# Frozen methods\n", encoding="utf-8")
    manifest = {
        "run_id": run.name,
        "mode": mod.EXPECTED_MODE,
        "explicit_degraded_admission": True,
        "repo_commit": "c21a79a8cfffc3e2140cc07d01eda06d53bd82c3",
        "mission": {"sha256": mod.EXPECTED_MISSION_SHA},
        "canonical_prompt": {"sha256": "5" * 64},
        "banners": {
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "experiment_split": mod.EXPECTED_SPLIT,
            "timestamp_discipline": mod.EXPECTED_TIMESTAMP,
            "authorization": mod.NO_LIVE_BANNER,
        },
        "gates": {
            "gate_a": {"status": "PASS_W05_EXPLORATORY_READY"},
            "gate_b": {
                "status": "PASS_W09_ATTESTED",
                "attestation_sha256": mod.sha256(
                    run / "DATA_INTEGRITY/W09_ATTESTATION.json"
                ),
            },
            "gate_c": {"status": "PASS_MODE1_ONLY", "mode2_authorized": False},
        },
        "selected_releases": [
            {
                "date": binding["date"],
                "release_id": release_id,
                "manifest_sha256": binding["manifest_sha256"],
                "object_version_set_sha256": binding["object_version_set_sha256"],
                "version_bindings_sha256": binding["version_bindings_sha256"],
                "seal_sha256": char * 64,
                "publication_state_sha256": char * 64,
                "evidence_tier": mod.EXPECTED_EVIDENCE,
                "tl1_status": mod.EXPECTED_TIMESTAMP,
                "include": mod.EXPECTED_SPLIT,
                "byte_count": 100 + index,
                "object_count": 10 + index,
            }
            for index, ((release_id, binding), char) in enumerate(
                zip(mod.EXPECTED_RELEASE_BINDINGS.items(), ("a", "b"))
            )
        ],
        "compute": {"instance_id": "i-w09", "instance_type": "r8g.2xlarge"},
        "repository": {
            "source_manifest_path": "SOURCE_MANIFEST.json",
            "source_manifest_sha256": mod.sha256(run / "SOURCE_MANIFEST.json"),
            "feature_definition_sha256": mod.sha256(run / "FEATURE_DICTIONARY.json"),
            "method_definition_sha256": mod.sha256(run / "METHODS.md"),
            "query_set_sha256": mod.sha256(query_sums),
        },
        "publication": {
            "canonical_local_archive": str(run),
            "s3_report_archive_status": "DEFERRED_AUTHORITY_CONFLICT",
        },
        "status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
    }
    write_json(run / "RUN_MANIFEST.json", manifest)
    cards = []
    for hypothesis_id in mod.HYPOTHESIS_IDS:
        card = {
            "hypothesis_id": hypothesis_id,
            "status": "CANDIDATE",
            "split": mod.EXPECTED_SPLIT,
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "sentence": f"Frozen sentence for {hypothesis_id}.",
            "mechanism": "Frozen mechanism.",
            "population": "Frozen population.",
            "feature": "Frozen causal feature.",
            "decision_clock": "TL1 receive clock.",
            "test_family": "Root-event block test.",
            "multiplicity_policy": "BH-FDR within family.",
            "negative_controls": ["future shift"],
            "required_engine_capabilities": ["causal as-of"],
            "fill_policy": "strict-through",
            "fee_policy": "exact known fees only",
            "rejection": "frozen kill rule",
            "reopen": "more sealed days",
            "economic_threshold": "frozen threshold",
        }
        if hypothesis_id in mod.RV_IDS:
            card["status"] = "DATA_STARVED"
            card["cycle1_result"] = {
                "hypothesis_status": "DATA_STARVED",
                "reason": "Authoritative family mapping is absent.",
            }
        cards.append(card)
    write_json(run / "HYPOTHESIS_LEDGER.json", {"hypotheses": cards})
    (run / "TRIAL_REGISTRY.jsonl").write_text(
        "".join(
            json.dumps({"trial_id": item, "status": "REGISTERED"}) + "\n"
            for item in mod.HYPOTHESIS_IDS
        ),
        encoding="utf-8",
    )
    write_json(
        run / mod.CORE_SUMMARY,
        {
            "run_id": run.name,
            "boundary": "No result is a promotion; RFQ full stage remains pending.",
            "atlas": {"status": "DIAGNOSTIC_ONLY"},
        },
    )
    write_json(
        run / mod.CORE_HYPOTHESIS_SUMMARY,
        {
            "schema_version": "sports-autoresearch-core-hypothesis-tests-v1",
            "run_id": run.name,
            "mode": mod.EXPECTED_MODE,
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "hypotheses": {
                hypothesis_id: {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_status": (
                        "DATA_STARVED"
                        if hypothesis_id == "C1-SPREAD-CAPTURE-01"
                        else "COLLECT_MORE"
                    ),
                    "status_reason": "Frozen economics unavailable."
                    if hypothesis_id == "C1-SPREAD-CAPTURE-01"
                    else "Only one evaluation day block.",
                }
                for hypothesis_id in mod.CORE_TEST_IDS
            },
        },
    )
    rfq = {
        "schema": "sports-autoresearch-rfq-full-stage-v1",
        "run_id": run.name,
        "mode": mod.EXPECTED_MODE,
        "evidence": mod.EXPECTED_EVIDENCE,
        "status": "DESCRIPTIVE_DISCOVERY_ONLY",
        "input": {
            "release_ids": list(mod.EXPECTED_RELEASE_BINDINGS),
            "objects": 2,
            "bytes": 200,
        },
        "counts": {
            "valid_requests": 8,
            "observed_first_valid_deletes": 5,
            "right_censored_creates": 3,
        },
        "coverage": {"delete_endpoint_share": 0.625},
        "clob": {
            "causal_eligible_endpoint_anchors": 4,
            "matched_pairs": 2,
        },
        "hard_truth": {"broadcast_contains_accepted_quote_or_fill": False},
    }
    write_json(run / mod.RFQ_SUMMARY, rfq)
    (run / "REPORT/tables/rfq_size_summary.csv").write_text(
        "contracts_n,target_n\n4,3\n", encoding="utf-8"
    )
    write_json(
        run / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json",
        {
            "schema_version": "sports-autoresearch-l2-hypothesis-stage-v1",
            "stage": "L2_HYPOTHESIS_TESTS",
            "status": "COLLECT_MORE",
            "run_id": run.name,
            "banner": {
                "data_evidence": mod.EXPECTED_EVIDENCE,
                "timestamp_discipline": mod.EXPECTED_TIMESTAMP,
                "experiment_split": mod.EXPECTED_SPLIT,
                "artifact_status": "DIAGNOSTIC_ONLY",
                "authorization": mod.NO_LIVE_BANNER,
            },
            "data_binding": {
                "release_ids": list(mod.EXPECTED_RELEASE_BINDINGS)
            },
            "receipt_quality": {},
            "replay_qc": {},
            "hypotheses": {
                hypothesis_id: {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_status": "COLLECT_MORE",
                    "status_reason": "Only two degraded days.",
                }
                for hypothesis_id in mod.L2_TEST_IDS
            },
        },
    )
    write_json(
        run / "DATA_COVERAGE.json",
        {
            "schema_version": "sports-autoresearch-coverage-v2",
            "coverage_status": "PARTIAL_CORE_FACT_CUBE_RFQ_FULL_STAGE_PENDING",
            "coverage_cube": "REPORT/tables/data_coverage_cube.csv",
            "coverage_cube_cells": 1,
            "channels": [
                {
                    "channel": "orderbooks_l1",
                    "included_rows": 1,
                    "n_markets": 1,
                    "n_games": 1,
                    "n_day_blocks": 1,
                }
            ],
            "exclusions": {"missing_receive_rows": 0},
            "limitations": [],
        },
    )
    usage_path = run / "RESOURCE_USAGE.json"
    write_json(
        usage_path,
        {
            "w09_driver_wall_seconds": 3600.0,
            "estimated_total_compute_cost_usd": 0.4713,
            "shutdown_confirmation": "PENDING_MISSION_END",
        },
    )
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["resource_usage"] = {
        "path": "RESOURCE_USAGE.json",
        "sha256": mod.sha256(usage_path),
        "estimated_total_compute_cost_usd": 0.4713,
    }
    write_json(run / "RUN_MANIFEST.json", manifest)
    return run


def test_finalizer_is_terminal_idempotent_and_self_contained(tmp_path):
    run = make_run(tmp_path)
    mod.finalize(run)

    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "COMPLETE"
    assert manifest["terminal_condition"]["id"] == 4
    summary = json.loads(
        (run / "REPORT/tables/FINAL_MISSION_SUMMARY.json").read_text(encoding="utf-8")
    )
    assert summary["hypotheses_tested"] == 10
    assert summary["promotion_ready"] == 0
    assert summary["formal_verdict_pass"] == 0
    assert summary["shortlisted_strategies"] == []
    assert summary["status_counts"]["COLLECT_MORE"] == 7
    assert summary["status_counts"]["DATA_STARVED"] == 3
    full = (run / "REPORT/FULL_REPORT.md").read_text(encoding="utf-8")
    assert full.count("\n## ") == 25
    page = (run / "REPORT/index.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in page
    assert "<style>" in page and "<script>" in page
    rfq = json.loads((run / mod.RFQ_CONCLUSIONS).read_text(encoding="utf-8"))
    assert {
        item["hypothesis_status"] for item in rfq["hypotheses"].values()
    } == {"COLLECT_MORE"}
    registry_before = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    sums_before = (run / "ARTIFACT_SHA256SUMS").read_bytes()

    # Re-running completion must not append a second final result or change
    # deterministic artifacts.
    mod.finalize(run)
    assert (run / "TRIAL_REGISTRY.jsonl").read_bytes() == registry_before
    assert (run / "ARTIFACT_SHA256SUMS").read_bytes() == sums_before
    mod.verify_artifacts(run)


def test_completed_archive_rerun_refuses_to_reseal_tampering(tmp_path):
    run = make_run(tmp_path)
    mod.finalize(run)
    registry_path = run / "TRIAL_REGISTRY.jsonl"
    records = registry_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(records[-1])
    last["status_reason"] = "post-completion mutation"
    records[-1] = json.dumps(last, sort_keys=True)
    registry_path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(mod.MissionFinalizationError, match="artifact hash mismatch"):
        mod.finalize(run)


def test_finalizer_requires_resource_finalize_hash_binding(tmp_path):
    run = make_run(tmp_path)
    usage_path = run / "RESOURCE_USAGE.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    usage["estimated_total_compute_cost_usd"] = 999.0
    write_json(usage_path, usage)

    with pytest.raises(mod.MissionFinalizationError, match="not bound"):
        mod.finalize(run)


@pytest.mark.parametrize("mutation", ["summary", "resource", "release"])
def test_verify_recomputes_manifest_internal_bindings(tmp_path, mutation):
    run = make_run(tmp_path)
    mod.finalize(run)
    manifest_path = run / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "summary":
        manifest["finalization"]["summary_sha256"] = "0" * 64
    elif mutation == "resource":
        manifest["resource_usage"]["sha256"] = "0" * 64
    else:
        manifest["selected_releases"][0]["manifest_sha256"] = "0" * 64
    write_json(manifest_path, manifest)

    with pytest.raises(mod.MissionFinalizationError, match="binding mismatch"):
        mod.verify_artifacts(run)


def test_finalizer_rejects_forbidden_l2_status_and_rfq_zero_is_data_starved(tmp_path):
    run = make_run(tmp_path)
    rfq_path = run / mod.RFQ_SUMMARY
    rfq = json.loads(rfq_path.read_text(encoding="utf-8"))
    rfq["clob"]["matched_pairs"] = 0
    write_json(rfq_path, rfq)
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    conclusions = mod.build_rfq_conclusions(
        run, manifest, rfq, completed_at_utc="2026-07-15T12:00:00Z"
    )
    assert conclusions["hypotheses"]["C1-RFQ-CLOB-01"]["hypothesis_status"] == "DATA_STARVED"
    assert conclusions["hypotheses"]["C1-ANOM-RFQ-SIZE-TAIL-01"]["hypothesis_status"] == "COLLECT_MORE"

    frozen = run / "queries/finalize_mission.py"
    original = frozen.read_bytes()
    frozen.write_bytes(original + b"\n# post-freeze mutation\n")
    with pytest.raises(mod.MissionFinalizationError, match="frozen query hash mismatch"):
        mod.validate_frozen_query_source(run, manifest)
    frozen.write_bytes(original)

    l2_path = run / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json"
    l2 = json.loads(l2_path.read_text(encoding="utf-8"))
    l2["hypotheses"]["C1-HFOLLOW-RETREAT-01"]["hypothesis_status"] = "PROMOTION_READY"
    write_json(l2_path, l2)
    with pytest.raises(mod.MissionFinalizationError, match="forbidden Mode-1 status"):
        mod.finalize(run)
