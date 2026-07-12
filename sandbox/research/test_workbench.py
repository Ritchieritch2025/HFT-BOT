"""Focused tests for the local read-only Research Workbench."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("research_workbench", HERE / "workbench" / "app.py")
wb = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wb
SPEC.loader.exec_module(wb)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_registry_starts_every_atlas_hypothesis_unfrozen():
    registry = wb.load_hypotheses()
    expected = {
        "ATL-TTS-01",
        "ATL-BURST-01",
        "ATL-TOX-01",
        "ATL-RES-01",
        "ATL-FLOW-01",
        "ATL-WINDOW-01",
    }
    assert {item["hypothesis_id"] for item in registry["hypotheses"]} == expected
    assert {item["status"] for item in registry["hypotheses"]} == {"DRAFT_NEEDS_FREEZE"}


def test_build_aggregates_real_manifest_shape_and_only_writes_workbench(tmp_path):
    manifest = tmp_path / "work" / "warehouse" / "manifest.csv"
    _write(
        manifest,
        "date,table,category,subcategory,row_count,file_path,file_md5,created_ts\n"
        "2026-07-06,trades,Sports,Basketball,10,x,a,z\n"
        "2026-07-06,trades,Sports,Basketball,5,y,b,z\n"
        "2026-07-07,orderbooks_l1,Sports,Tennis,20,z,c,z\n"
        "2026-07-07,trades,Sports,Baseball,30,q,d,z\n"
        "2026-07-07,trades,Sports,Soccer,999,q,d,z\n"
        "2026-07-07,trades,Politics,Basketball,999,q,d,z\n",
    )
    snapshot = wb.build_workbench(
        repo_root=tmp_path,
        generated_at_utc="2026-07-11T00:00:00Z",
    )
    overview = snapshot["overview"]
    by_sport = {item["sport"]: item for item in overview["sports"]}
    assert by_sport["Basketball"]["total_rows"] == 15
    assert by_sport["Basketball"]["rows_by_table"] == {"trades": 15}
    assert by_sport["Tennis"]["by_date"] == [
        {"date": "2026-07-07", "table": "orderbooks_l1", "rows": 20}
    ]
    assert by_sport["Baseball"]["total_rows"] == 30
    assert overview["dates"] == ["2026-07-06", "2026-07-07"]
    assert overview["source_manifest"]["sha256"]
    written = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json"))
    assert written == [
        "sandbox/research/reports/workbench/experiments.json",
        "sandbox/research/reports/workbench/hypotheses.json",
        "sandbox/research/reports/workbench/overview.json",
    ]


def test_unknown_and_missing_hypotheses_are_visible_as_invalid(tmp_path):
    experiments = tmp_path / "sandbox" / "research" / "reports" / "experiments"
    base = {
        "schema_version": "research-experiment-v1",
        "experiment_id": "e1",
        "title": "test",
        "result_status": "NO_RESULTS",
    }
    _write(experiments / "valid.json", json.dumps({**base, "hypothesis_id": "ATL-TTS-01"}))
    _write(experiments / "unknown.json", json.dumps({**base, "experiment_id": "e2", "hypothesis_id": "ATL-NOPE-01"}))
    _write(experiments / "missing.json", json.dumps({**base, "experiment_id": "e3"}))
    result = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "DRAFT_NEEDS_FREEZE"},
        "2026-07-11T00:00:00Z",
    )
    statuses = {item["artifact"]: item["status"] for item in result["experiments"]}
    assert statuses == {"missing.json": "INVALID", "unknown.json": "INVALID", "valid.json": "VALID"}
    assert result["invalid_count"] == 2
    assert {item["artifact"] for item in result["validation_errors"]} == {
        "missing.json",
        "unknown.json",
    }


def test_non_loopback_hosts_are_rejected():
    assert wb.is_loopback_host("127.0.0.1")
    assert wb.is_loopback_host("::1")
    assert wb.is_loopback_host("localhost")
    assert not wb.is_loopback_host("0.0.0.0")
    assert not wb.is_loopback_host("example.com")


def test_frontend_uses_only_local_api_data_without_embedded_results():
    html = (HERE / "workbench" / "index.html").read_text(encoding="utf-8")
    assert all(route in html for route in (
        "/api/overview", "/api/hypotheses", "/api/experiments"
    ))
    assert "https://" not in html
    assert "http://" not in html
    assert "2,250,301" not in html
    assert "6,566,589" not in html
    assert "9,599,100" not in html


def test_optional_charts_and_tables_pass_through_and_must_be_lists(tmp_path):
    experiments = tmp_path / "experiments"
    chart = {
        "title": "Spread",
        "kind": "line",
        "x_label": "Minutes",
        "y_label": "Cents",
        "series": [{"name": "Basketball", "points": [{"x": 60, "y": 4.2}]}],
    }
    table = {"title": "Coverage", "columns": ["date", "rows"], "rows": [["2026-07-06", 10]]}
    base = {
        "schema_version": "research-experiment-v1",
        "experiment_id": "visual-e1",
        "hypothesis_id": "ATL-TTS-01",
        "title": "Visual result",
        "result_status": "EXPLORATORY",
    }
    _write(experiments / "valid.json", json.dumps({**base, "charts": [chart], "tables": [table]}))
    _write(
        experiments / "invalid.json",
        json.dumps({**base, "experiment_id": "visual-e2", "charts": {}, "tables": "not-a-list"}),
    )
    result = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "FROZEN_EXPLORATORY"},
        "2026-07-11T00:00:00Z",
    )
    entries = {item["artifact"]: item for item in result["experiments"]}
    assert entries["valid.json"]["status"] == "VALID"
    assert entries["valid.json"]["charts"] == [chart]
    assert entries["valid.json"]["tables"] == [table]
    assert entries["invalid.json"]["status"] == "INVALID"
    assert entries["invalid.json"]["charts"] == []
    assert entries["invalid.json"]["tables"] == []
    assert entries["invalid.json"]["validation_errors"] == [
        "charts must be a list when present",
        "tables must be a list when present",
    ]


def test_registry_status_can_advance_and_results_require_frozen_hypothesis(tmp_path):
    registry = wb.load_hypotheses()
    registry["hypotheses"][0]["status"] = "FROZEN_TRAIN"
    registry_path = tmp_path / "hypotheses.json"
    _write(registry_path, json.dumps(registry))
    assert wb.load_hypotheses(registry_path)["hypotheses"][0]["status"] == "FROZEN_TRAIN"

    experiments = tmp_path / "experiments"
    base = {
        "schema_version": "research-experiment-v1",
        "hypothesis_id": "ATL-TTS-01",
        "title": "Lifecycle test",
    }
    _write(
        experiments / "draft-result.json",
        json.dumps({**base, "experiment_id": "e1", "result_status": "EXPLORATORY"}),
    )
    _write(
        experiments / "draft-plan.json",
        json.dumps({**base, "experiment_id": "e2", "result_status": "NO_RESULTS"}),
    )
    draft = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "DRAFT_NEEDS_FREEZE"},
        "2026-07-11T00:00:00Z",
    )
    draft_entries = {item["artifact"]: item for item in draft["experiments"]}
    assert draft_entries["draft-result.json"]["status"] == "INVALID"
    assert draft_entries["draft-plan.json"]["status"] == "VALID"
    assert "require a hypothesis status beginning FROZEN_" in draft_entries[
        "draft-result.json"
    ]["validation_errors"][0]

    frozen = wb.discover_experiments(
        experiments,
        {"ATL-TTS-01": "FROZEN_TRAIN"},
        "2026-07-11T00:00:00Z",
    )
    assert {item["status"] for item in frozen["experiments"]} == {"VALID"}
