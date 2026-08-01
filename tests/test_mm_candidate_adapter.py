from __future__ import annotations

import os

from tools.research.crypto_mm import mm_candidate_adapter as A


def test_candidates_are_importable_but_disabled_by_default(monkeypatch):
    for key in (
        "MM_LOG_ODDS_FEATURES",
        "MM_DYNAMIC_FOK_POLICY",
        "MM_STAGE2_SOURCE",
        "MM_V42_CONTRACT",
    ):
        monkeypatch.delenv(key, raising=False)
    status = A.status()
    assert set(status) == {"log_odds", "dynamic_fok", "stage2_v31", "v42"}
    assert all(not row["enabled"] for row in status.values())
    assert A.config_errors() == []


def test_execution_candidate_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("MM_DYNAMIC_FOK_POLICY", "1")
    errors = A.config_errors()
    assert errors and errors[0].startswith("dynamic_fok:FAIL_CLOSED")

