#!/usr/bin/env python3
"""Safe Markdown-to-job-spec compiler contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.plan_contract import PlanContractError, compile_plan  # noqa: E402


def test_plain_markdown_is_accepted_without_being_executed():
    text = "# Novel experiment\n\n```python\nprint('data, not code')\n```\n"
    spec = compile_plan(text, "../../idea.md")
    assert spec["state"] == "NEEDS_METHOD"
    assert spec["plugin_id"] is None
    assert spec["source_filename"] == "idea.md"
    assert spec["plan_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert spec["arbitrary_plan_code_execution"] is False
    assert spec["safety"]["orders"] is False


def test_registered_plugin_frontmatter_becomes_ready_zero_copy():
    text = """---
title: Queue decay by spread
plugin: deep03
data:
  required: [L1, L2, TRADES, MARKET_GRAPH]
  forbidden: [RFQ]
date_window:
  start: 2026-07-10
  end: 2026-07-17
budget:
  max_runtime_seconds: 3600
  max_spend_usd: 1.0
---
# Details
Run descriptive diagnostics.
"""
    spec = compile_plan(text)
    assert spec["state"] == "READY"
    assert spec["plugin_id"] == "deep03"
    assert spec["data_requirements"]["zero_copy"] is True
    assert spec["data_requirements"]["forbidden"] == ["RFQ"]
    assert spec["date_window"] == {"start": "2026-07-10", "end": "2026-07-17"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# secret\nAKIAABCDEFGHIJKLMNOP\n",
        "# secret\n-----BEGIN PRIVATE KEY-----\n",
        "# bad\n\x00\n",
    ],
)
def test_refuses_empty_secret_or_binary_plan(text):
    with pytest.raises(PlanContractError):
        compile_plan(text)


def test_unknown_plugin_never_becomes_runnable():
    spec = compile_plan("---\nplugin: arbitrary-python\n---\n# Run me\n")
    assert spec["state"] == "NEEDS_METHOD"
    assert spec["requested_plugin_id"] == "arbitrary-python"
    assert spec["plugin_id"] is None


def test_conflicting_data_and_unbounded_budget_are_refused():
    with pytest.raises(PlanContractError, match="required/optional/forbidden"):
        compile_plan("---\nplugin: deep03\ndata:\n  required: [L1]\n  forbidden: [L1]\n---\n# x\n")
    with pytest.raises(PlanContractError, match="max_spend_usd"):
        compile_plan("---\nbudget:\n  max_spend_usd: 100\n---\n# x\n")
