#!/usr/bin/env python3
"""Generic Research Inbox report rendering contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.report_renderer import ReportError, render_report, write_report  # noqa: E402


def _results():
    return {
        "schema_version": "research-results-v1",
        "job_id": "RJOB-20260718T235901123456Z-aaaaaaaaaaaa",
        "title": "Queue <script>alert(1)</script>",
        "state": "COMPLETE",
        "execution_class": "READONLY_EXPLORATORY",
        "summary": ["Spread response was descriptive only."],
        "metrics": [{"name": "median markout", "value": 1.2, "unit": "cents"}],
        "tables": [{"title": "By spread", "columns": ["spread", "n"], "rows": [[1, 10]]}],
        "limitations": ["Prior exposed data"],
        "receipts": {"input_sha256": "a" * 64},
    }


def test_render_is_self_contained_and_escapes_content():
    page = render_report(_results())
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert "https://" not in page
    assert "Summary" in page and "Limitations" in page and "Receipts" in page


def test_write_report_binds_exact_results_and_refuses_overwrite(tmp_path):
    source = tmp_path / "RESULTS.json"
    raw = json.dumps(_results(), sort_keys=True).encode()
    source.write_bytes(raw)
    output = tmp_path / "REPORT"
    report = write_report(source, output)
    receipt = json.loads((output / "REPORT_RECEIPT.json").read_text())
    assert report.name == "index.html"
    assert receipt["results_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    with pytest.raises(ReportError, match="already exists"):
        write_report(source, output)


def test_invalid_schema_or_table_shape_is_refused():
    value = _results()
    value["schema_version"] = "unknown"
    with pytest.raises(ReportError, match="schema"):
        render_report(value)
    value = _results()
    value["tables"][0]["rows"] = [[1]]
    with pytest.raises(ReportError, match="row width"):
        render_report(value)
