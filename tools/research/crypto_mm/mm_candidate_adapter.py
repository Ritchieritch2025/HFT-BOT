#!/usr/bin/env python3
"""Fail-closed bridge for offline crypto-MM research components.

The research modules deliberately have no live permissions.  This adapter is
the only place the execution engine knows about them: imports are optional,
feature flags default off, and enabling a component requires its immutable
permission constants to remain false (the engine can only consume metadata,
never call a research action directly).
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any


def _flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class CandidateComponent:
    name: str
    enabled: bool
    imported: bool
    allowed: bool
    status: str
    error: str | None = None
    module: Any = None


def _load(name: str, module_name: str, enabled: bool) -> CandidateComponent:
    try:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as first:
            # Source-tree tests import the package; deployed bundles import
            # sibling modules after mm_engine prepends its directory.
            try:
                module = importlib.import_module(
                    f"tools.research.crypto_mm.{module_name}")
            except ModuleNotFoundError:
                raise first
    except Exception as exc:  # optional research bundle may be absent in deploy
        return CandidateComponent(name, enabled, False, False,
                                  "IMPORT_FAILED", f"{type(exc).__name__}: {exc}")
    if name == "log_odds":
        allowed = True  # features are pure and have no execution side effects
    elif name == "dynamic_fok":
        # A policy with all execution permissions false is safe to inspect,
        # but it is not an executable engine component.
        allowed = False
    elif name == "stage2_v31":
        allowed = False
    elif name == "v42":
        allowed = False
    else:
        allowed = False
    status = "ENABLED_METADATA_ONLY" if enabled and allowed else ("DISABLED" if not enabled else "FAIL_CLOSED")
    return CandidateComponent(name, enabled, True, allowed, status, None, module)


def components() -> dict[str, CandidateComponent]:
    """Load all candidates and return immutable execution metadata."""
    return {
        "log_odds": _load("log_odds", "log_odds_features", _flag("MM_LOG_ODDS_FEATURES")),
        "dynamic_fok": _load("dynamic_fok", "round4_dynamic_keep_fok_policy_v2_1", _flag("MM_DYNAMIC_FOK_POLICY")),
        "stage2_v31": _load("stage2_v31", "round4_stage2_postfill_extractor_v3_1", _flag("MM_STAGE2_SOURCE")),
        "v42": _load("v42", "round4_postfill_state_contract_v4_2", _flag("MM_V42_CONTRACT")),
    }


def config_errors() -> list[str]:
    """Return errors only for explicitly enabled candidates."""
    errors: list[str] = []
    for key, item in components().items():
        if item.enabled and (not item.imported or not item.allowed):
            errors.append(f"{key}:{item.status}:{item.error or 'permission contract'}")
    return errors


def status() -> dict[str, object]:
    return {
        key: {"enabled": value.enabled, "imported": value.imported,
              "allowed": value.allowed, "status": value.status,
              "error": value.error}
        for key, value in components().items()
    }
