#!/usr/bin/env python3
"""Local-only research catalog and exact-release resolver contracts."""

from __future__ import annotations

import copy
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import research_data  # noqa: E402
from research.data_catalog import build_catalog  # noqa: E402
from research.data_resolver import resolve_data  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402
from test_research_reference_consumer import _store_for, build_release  # noqa: E402


def _cache(tmp_path: Path, *, with_rfq: bool = False) -> tuple[Path, str]:
    cache = tmp_path / "cache"
    release_id, manifest, source = build_release(with_rfq=with_rfq)
    assert research_data.cmd_fetch(
        _store_for((release_id, manifest, source)),
        str(cache),
        release_id,
        with_rfq,
    ) == 0
    return cache, release_id


def _spec(**data_overrides):
    data = {
        "required": ["L1", "L2", "TRADES", "MARKET_GRAPH"],
        "optional": [],
        "forbidden": ["RFQ"],
    }
    data.update(data_overrides)
    lines = [
        "---",
        "plugin: deep03",
        "data:",
        "  required: [%s]" % ", ".join(data["required"]),
        "  optional: [%s]" % ", ".join(data["optional"]),
        "  forbidden: [%s]" % ", ".join(data["forbidden"]),
        "---",
        "# Catalog experiment",
        "",
    ]
    return compile_plan("\n".join(lines))


def test_catalog_scans_only_verified_content_addressed_v3(tmp_path):
    cache, release_id = _cache(tmp_path)
    before = sorted(path.relative_to(cache).as_posix() for path in cache.rglob("*"))
    first = build_catalog(cache)
    second = build_catalog(cache)
    after = sorted(path.relative_to(cache).as_posix() for path in cache.rglob("*"))

    assert first == second
    assert before == after
    assert first["state"] == "READY"
    assert first["release_count"] == 1
    row = first["releases"][0]
    assert row["release_id"] == release_id
    assert {"L1", "L2", "TRADES", "MARKET_GRAPH"}.issubset(row["data_families"])
    assert row["channels"] == ["orderbooks_full", "orderbooks_l1", "trades"]
    assert row["object_count"] > 0 and row["object_bytes"] > 0
    assert row["rfq"] == {
        "manifest_included": False,
        "materialized": False,
        "status": "ABSENT_FROM_RELEASE",
    }
    assert first["network_reads"] == 0
    assert first["copied_bytes"] == 0


def test_resolver_selects_exact_release_and_emits_ready_preflight(tmp_path):
    cache, release_id = _cache(tmp_path)
    catalog = build_catalog(cache)
    selection, preflight = resolve_data(_spec(), catalog)

    assert selection["state"] == "SELECTED"
    assert selection["release_ids"] == [release_id]
    assert selection["releases"][0]["manifest_version_id"].startswith("manifest-version-")
    assert selection["zero_copy"] is True
    assert preflight["state"] == "READY"
    assert preflight["release_ids"] == [release_id]
    assert preflight["object_bytes"] == catalog["releases"][0]["object_bytes"]
    assert preflight["network_reads"] == 0
    assert preflight["copied_bytes"] == 0
    assert resolve_data(_spec(), catalog) == (selection, preflight)


def test_missing_required_family_blocks_without_partial_selection(tmp_path):
    cache, _release_id = _cache(tmp_path)
    selection, preflight = resolve_data(
        _spec(required=["L1", "L2", "TRADES", "SETTLEMENT_TICKS"]),
        build_catalog(cache),
    )
    assert selection["state"] == "BLOCKED"
    assert selection["release_ids"] == []
    assert selection["reasons"] == [{
        "code": "REQUIRED_FAMILY_MISSING",
        "date": "2026-07-13",
        "families": ["SETTLEMENT_TICKS"],
    }]
    assert preflight["state"] == "BLOCKED"
    assert preflight["research_execution_started"] is False


def test_forbidden_materialized_family_refuses(tmp_path):
    cache, _release_id = _cache(tmp_path, with_rfq=True)
    catalog = build_catalog(cache)
    assert "RFQ" in catalog["releases"][0]["data_families"]
    selection, preflight = resolve_data(_spec(), catalog)
    assert selection["state"] == "REFUSED"
    assert selection["release_ids"] == []
    assert selection["reasons"][0]["code"] == "FORBIDDEN_FAMILY_PRESENT"
    assert selection["reasons"][0]["families"] == ["RFQ"]
    assert preflight["state"] == "REFUSED"


def test_explicit_contiguous_window_blocks_on_missing_date(tmp_path):
    cache, _release_id = _cache(tmp_path)
    spec = _spec()
    spec["date_window"] = {"start": "2026-07-13", "end": "2026-07-14"}
    selection, _preflight = resolve_data(spec, build_catalog(cache))
    assert selection["state"] == "BLOCKED"
    assert selection["reasons"] == [{"code": "DATE_MISSING", "date": "2026-07-14"}]


def test_catalog_digest_tampering_is_rejected(tmp_path):
    cache, _release_id = _cache(tmp_path)
    catalog = build_catalog(cache)
    changed = copy.deepcopy(catalog)
    changed["releases"][0]["object_bytes"] += 1
    try:
        resolve_data(_spec(), changed)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampered catalog was accepted")
