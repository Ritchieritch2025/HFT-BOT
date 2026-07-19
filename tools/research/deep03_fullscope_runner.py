#!/usr/bin/env python3
"""Run the exact-V3 Deep03 base + market-graph + L2 full-scope unit.

This is deliberately a separate entry point from ``deep03_v3_runner.py``.
The existing D3-W2A runner therefore keeps its audited B01--B04 behavior,
while this runner requires additional, explicit graph and L2 authority before
it will touch the same exact-input checkpoint namespace.  RFQ is not admitted
by this unit and remains a separately authorized overlay.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import html
import json
import os
import platform
import shutil
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from deep03_fullscope_graph import (
    METHOD_ID as GRAPH_METHOD_ID,
    SCHEMA as GRAPH_SCHEMA,
    build_market_graph,
)
from deep03_v3_common import (
    Deep03InputError,
    LABELS,
    MODE,
    atomic_write_bytes,
    atomic_write_json,
    ensure_run_inputs_current,
    load_authority_context,
    refuse_credential_environment,
    sha256_file,
    source_hashes,
    utc_now,
)
from deep03_v3_l2 import (
    DWELL_SEMANTICS,
    L2_ABSENT_DATES,
    L2_ANALYSIS_DATES,
    L2_CAPTURE_DATES,
    L2_KNOWN_EXCLUDED_DATES,
    L2_SCOPE_DATES,
    PRIMARY_STALE_TTL_NS,
    STALE_TTL_REGISTRY_NS,
    execute_l2_snbd_bounded,
)
from deep03_v3_methods import (
    BOUNDED_EXECUTION_RECEIPT_SCHEMA,
    BoundedCheckpointStore,
    METHODS,
    bounded_source_binding,
    execute_all_bounded,
    path_list,
    rows_as_dicts,
)
from deep03_v3_runner import (
    CANONICAL_CHECKPOINT_ROOT,
    CHECKPOINT_EXPANSION_FACTOR,
    DEFAULT_CHECKPOINT_RESERVE_BYTES,
    MARKET_BUCKETS,
    _artifact_rows,
    _bar_chart,
    _checkpoint_namespace,
    _configure_duckdb,
    _estimability_receipt,
    _exclusion_receipt,
    _is_relative_to,
    _method_receipt,
    _quality_receipt,
    _render_report,
    _table,
    _verified_checkpoint_payload_bytes,
)


SCHEMA_RESULTS = "deep03-fullscope-base-l2-results-v1"
SCHEMA_EXECUTION = "deep03-fullscope-base-l2-execution-receipt-v1"
SCHEMA_COMPLETE = "deep03-fullscope-base-l2-run-complete-v1"
L2_EXECUTION_SCHEMA = "deep03-v3-l2-snbd-execution-v2"
L2_METHOD_ID = "D3-FULL-L2-SNBD-01"
FULLSCOPE_METHODS = (*METHODS, GRAPH_METHOD_ID, L2_METHOD_ID)
L2_CHECKPOINT_EXPANSION_FACTOR = 8
DEFAULT_L2_MARKET_BUCKETS = 16
FULLSCOPE_SOURCE_MODULES = (
    # deep03_v3_methods.py carries the checkpoint store, row-conservation and
    # bounded execution logic the L2 unit depends on; the independent audit
    # receipt must bind its exact SHA, not only the L2-specific modules.
    "deep03_v3_methods.py",
    "deep03_fullscope_graph.py",
    "deep03_v3_l2.py",
    "deep03_fullscope_runner.py",
)
FULLSCOPE_AUTHORITY_SCOPE = {
    GRAPH_METHOD_ID: "DESCRIPTIVE_ONLY_NO_PNL",
    L2_METHOD_ID: "DESCRIPTIVE_ONLY_NO_PNL",
}
REQUIRED_L2_STAGES = {
    "l2_availability",
    "l2_physical",
    "l2_replay",
    "l2_episodes",
    "l2_exact_atlas",
    "l2_matched_controls",
}
L2_AUDIT_SCHEMA = "deep03-fullscope-l2-independent-audit-v1"
L2_AUDIT_DECISION = "APPROVED_FOR_BASE_L2_INTEGRATION"
L2_AUDIT_BLOCKER_CLASSES = (
    "FUTURE_CONTROL",
    "POST_TREATMENT_COVARIATE",
    "ONE_SECOND_BOUNDARY",
    "DWELL_ACCOUNTING",
    "STALENESS_TTL",
    "SNAPSHOT_REGRESSION",
    "RESET_BEFORE_EXPIRY",
    "MANIFEST_ROW_AUTHORITY",
    "QUIET_CONTROL",
    "NEGATIVE_CONTROL",
    "HAZARD_SEMANTICS",
    "OOM_RESOURCE_BOUND",
    "REAL_QUALITY_RECEIPTS",
)
L2_AUDIT_FIELDS = {
    "schema_version",
    "state",
    "decision",
    "auditor_id",
    "auditor_independence_attested",
    "audited_runtime_commit",
    "audited_modules_sha256",
    "input_manifest_sha256",
    "input_release_ids",
    "l2_quality_objects",
    "real_quality_receipts_assessed",
    "blocker_classes_checked",
    "blockers",
    "approved_claim_tier",
    "rfq_scope",
    "audit_report_sha256",
}


def fullscope_source_hashes() -> dict[str, str]:
    """Return the narrow source pin plus every full-scope extension module."""
    hashes = source_hashes()
    here = Path(__file__).resolve().parent
    for name in FULLSCOPE_SOURCE_MODULES:
        path = here / name
        if not path.is_file() or path.is_symlink():
            raise Deep03InputError(f"pinned full-scope source module missing: {path}")
        hashes[f"tools/research/{name}"] = sha256_file(path)
    return dict(sorted(hashes.items()))


def _require_fullscope_authority(authority_context: Mapping[str, Any]) -> None:
    """Prevent an old B01--B04 ARM from implicitly authorizing graph/L2."""
    binding = authority_context.get("binding")
    scope = binding.get("authorized_method_scope") if isinstance(binding, dict) else None
    if not isinstance(scope, dict):
        raise Deep03InputError("full-scope authority has no method-scope mapping")
    missing = {
        method_id: expected
        for method_id, expected in FULLSCOPE_AUTHORITY_SCOPE.items()
        if scope.get(method_id) != expected
    }
    if missing:
        raise Deep03InputError(
            "full-scope graph/L2 authority is absent or mismatched: "
            + json.dumps(missing, sort_keys=True)
        )


def _validate_l2_market_buckets(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 256:
        raise Deep03InputError("L2 market bucket count must be an integer in [1,256]")
    return value


def _require_base_without_rfq(input_manifest: Mapping[str, Any]) -> None:
    if input_manifest.get("rfq_policy") != "FORBIDDEN_AND_ABSENT":
        raise Deep03InputError("full-scope base/L2 runner requires RFQ-forbidden input")
    contaminated = []
    for obj in input_manifest.get("objects") or []:
        kind = str(obj.get("kind") or "").lower()
        channel = str(obj.get("channel") or "").lower()
        logical_key = "/" + str(obj.get("logical_key") or "").lower().strip("/") + "/"
        if kind == "rfq" or channel.startswith("rfq") or "/rfq/" in logical_key:
            contaminated.append(str(obj.get("logical_key") or "<unnamed>"))
    if contaminated:
        raise Deep03InputError(
            "RFQ object reached the base/L2 runner: " + ",".join(contaminated[:8])
        )
    missing_counts = []
    declared_fact_rows = 0
    for obj in input_manifest.get("objects") or []:
        if str(obj.get("kind") or "").lower() != "facts":
            continue
        value = obj.get("row_count")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            missing_counts.append(str(obj.get("logical_key") or "<unnamed>"))
        else:
            declared_fact_rows += value
    if missing_counts:
        raise Deep03InputError(
            "every full-scope fact object requires an exact nonnegative row_count: "
            + ",".join(missing_counts[:8])
        )
    missing_total = input_manifest.get("fact_objects_without_row_count")
    if (
        isinstance(missing_total, bool)
        or not isinstance(missing_total, int)
        or missing_total != 0
    ):
        raise Deep03InputError(
            "full-scope manifest fact_objects_without_row_count must equal zero"
        )
    sealed_fact_rows = input_manifest.get("sealed_fact_rows")
    if (
        isinstance(sealed_fact_rows, bool)
        or not isinstance(sealed_fact_rows, int)
        or sealed_fact_rows != declared_fact_rows
    ):
        raise Deep03InputError(
            "full-scope sealed_fact_rows does not equal the exact fact-object row sum"
        )


def _expected_l2_quality_objects(input_manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for obj in input_manifest.get("objects") or []:
        if obj.get("kind") != "l2_quality_receipt":
            continue
        rows.append(
            {
                "date": obj.get("date"),
                "logical_key": obj.get("logical_key"),
                "source_version_id": obj.get("source_version_id"),
                "sha256": obj.get("sha256"),
            }
        )
    rows.sort(
        key=lambda row: (
            str(row["date"]),
            str(row["logical_key"]),
            str(row["source_version_id"]),
        )
    )
    if [row["date"] for row in rows] != list(L2_CAPTURE_DATES):
        raise Deep03InputError(
            "full-scope input must contain exactly one L2 quality object per captured date"
        )
    return rows


def _validate_l2_independent_audit(
    *,
    receipt_path: Path,
    report_path: Path,
    runtime_commit_path: Path,
    input_manifest: Mapping[str, Any],
    input_manifest_sha256: str,
    source_modules_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Require an external PASS that binds code and the real quality evidence.

    The current L2 implementation is intentionally non-runnable until a later
    independent audit has closed every known blocker.  This receipt is an
    additional safety gate; it does not replace the one-shot operator ARM.
    """
    receipt_path = Path(receipt_path)
    report_path = Path(report_path)
    runtime_commit_path = Path(runtime_commit_path)
    for path, label in (
        (receipt_path, "L2 independent audit receipt"),
        (report_path, "L2 independent audit report"),
        (runtime_commit_path, "runtime commit binding"),
    ):
        if not path.is_file() or path.is_symlink():
            raise Deep03InputError(f"{label} is missing or linked: {path}")
    try:
        receipt_raw = receipt_path.read_bytes()
        receipt = json.loads(receipt_raw)
        report_raw = report_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise Deep03InputError(f"L2 independent audit artifact unreadable: {exc}") from exc
    if not report_raw:
        raise Deep03InputError("L2 independent audit report is empty")
    report_sha256 = hashlib.sha256(report_raw).hexdigest()
    if not isinstance(receipt, dict) or set(receipt) != L2_AUDIT_FIELDS:
        raise Deep03InputError("L2 independent audit receipt field set is invalid")
    if receipt_raw != _canonical_json(receipt):
        raise Deep03InputError("L2 independent audit receipt is not canonical JSON")
    try:
        runtime_commit = runtime_commit_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise Deep03InputError(f"runtime commit binding unreadable: {exc}") from exc
    expected_modules = {
        f"tools/research/{name}": source_modules_sha256[f"tools/research/{name}"]
        for name in FULLSCOPE_SOURCE_MODULES
    }
    expected_quality = _expected_l2_quality_objects(input_manifest)
    fixed = {
        "schema_version": L2_AUDIT_SCHEMA,
        "state": "PASS",
        "decision": L2_AUDIT_DECISION,
        "auditor_independence_attested": True,
        "audited_runtime_commit": runtime_commit,
        "audited_modules_sha256": expected_modules,
        "input_manifest_sha256": input_manifest_sha256,
        "input_release_ids": list(input_manifest.get("release_ids") or []),
        "l2_quality_objects": expected_quality,
        "real_quality_receipts_assessed": True,
        "blocker_classes_checked": list(L2_AUDIT_BLOCKER_CLASSES),
        "blockers": [],
        "approved_claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
        "rfq_scope": "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED",
        "audit_report_sha256": report_sha256,
    }
    for field, value in fixed.items():
        if receipt.get(field) != value:
            raise Deep03InputError(f"L2 independent audit gate mismatch: {field}")
    auditor_id = receipt.get("auditor_id")
    if not isinstance(auditor_id, str) or not auditor_id.strip():
        raise Deep03InputError("L2 independent audit has no auditor identity")
    if len(runtime_commit) != 40 or any(ch not in "0123456789abcdef" for ch in runtime_commit):
        raise Deep03InputError("L2 independent audit runtime commit is invalid")
    return {
        "receipt": receipt,
        "receipt_bytes": receipt_raw,
        "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
        "report_bytes": report_raw,
        "report_sha256": report_sha256,
    }


def _fullscope_disk_headroom_preflight(
    *,
    input_manifest: dict[str, Any],
    namespace: Path,
    source_binding: str,
    reserve_bytes: int,
) -> dict[str, Any]:
    """Fail closed on a conservative L1/trade/L2 checkpoint envelope."""
    if (
        isinstance(reserve_bytes, bool)
        or not isinstance(reserve_bytes, int)
        or reserve_bytes < 0
    ):
        raise Deep03InputError("checkpoint reserve bytes must be a non-negative integer")
    channels = {
        "orderbooks_l1": {"objects": 0, "bytes": 0},
        "trades": {"objects": 0, "bytes": 0},
        "orderbooks_full": {"objects": 0, "bytes": 0},
    }
    for obj in input_manifest.get("objects") or []:
        channel = obj.get("channel")
        if channel == "orderbooks_l2":
            channel = "orderbooks_full"
        if (
            obj.get("kind") != "facts"
            or channel not in channels
            or "/category=Sports/" not in str(obj.get("logical_key") or "")
        ):
            continue
        size = obj.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise Deep03InputError(
                f"invalid exact full-scope source size for {obj.get('logical_key')}"
            )
        channels[str(channel)]["objects"] += 1
        channels[str(channel)]["bytes"] += size
    if not all(row["objects"] > 0 for row in channels.values()):
        raise Deep03InputError(
            "full-scope run requires Sports L1, trades, and captured L2 objects"
        )
    namespace.mkdir(parents=True, exist_ok=True, mode=0o750)
    verified_bytes = _verified_checkpoint_payload_bytes(namespace, source_binding)
    l1_trade_bytes = channels["orderbooks_l1"]["bytes"] + channels["trades"]["bytes"]
    l2_bytes = channels["orderbooks_full"]["bytes"]
    l1_trade_budget = l1_trade_bytes * CHECKPOINT_EXPANSION_FACTOR
    l2_budget = l2_bytes * L2_CHECKPOINT_EXPANSION_FACTOR
    predicted = l1_trade_budget + l2_budget
    # Existing payloads are verified for integrity but receive no capacity
    # credit.  This is intentionally conservative across stage-ABI changes.
    required = predicted + reserve_bytes
    available = shutil.disk_usage(namespace).free
    receipt = {
        "schema_version": "deep03-fullscope-disk-headroom-v1",
        "state": "PASS" if available >= required else "REFUSED",
        "source_binding": source_binding,
        "source_channels": channels,
        "exact_l1_trade_source_bytes": l1_trade_bytes,
        "exact_l2_source_bytes": l2_bytes,
        "verified_checkpoint_payload_bytes": verified_bytes,
        "verified_checkpoint_bytes_credited": 0,
        "l1_trade_checkpoint_expansion_factor": CHECKPOINT_EXPANSION_FACTOR,
        "l2_checkpoint_expansion_factor": L2_CHECKPOINT_EXPANSION_FACTOR,
        "checkpoint_budget_formula": (
            "exact_l1_trade_source_bytes*4+exact_l2_source_bytes*8+reserve_bytes"
        ),
        "l1_trade_checkpoint_budget_bytes": l1_trade_budget,
        "l2_checkpoint_budget_bytes": l2_budget,
        "predicted_checkpoint_bytes": predicted,
        "reserve_bytes": reserve_bytes,
        "required_free_bytes": required,
        "available_free_bytes": available,
    }
    if available < required:
        raise Deep03InputError(
            "full-scope checkpoint disk gate failed: "
            f"required={required} available={available} "
            f"l1_trade={l1_trade_bytes} l2={l2_bytes} reserve={reserve_bytes}"
        )
    return receipt


def _validate_graph_result(
    result: Mapping[str, Any], input_manifest: Mapping[str, Any], stage_dir: Path
) -> Path:
    expected = {
        "schema_version": GRAPH_SCHEMA,
        "method_id": GRAPH_METHOD_ID,
        "status": "EXECUTED_DESCRIPTIVE_ONLY",
    }
    for field, value in expected.items():
        if result.get(field) != value:
            raise Deep03InputError(f"market graph result mismatch: {field}")
    if result.get("release_dates") != list(input_manifest.get("release_dates") or []):
        raise Deep03InputError("market graph release-date binding mismatch")
    if result.get("l2_present_dates") != list(L2_CAPTURE_DATES):
        raise Deep03InputError("market graph L2-present coverage mismatch")
    if result.get("l2_absent_dates") != list(L2_ABSENT_DATES):
        raise Deep03InputError("market graph L2-absent coverage mismatch")
    claims = result.get("claims")
    if not isinstance(claims, dict) or claims.get("arbitrage_or_pnl") is not False:
        raise Deep03InputError("market graph attempted a PnL/arbitrage claim")
    artifacts = result.get("artifacts")
    graph_artifact = artifacts.get("market_graph") if isinstance(artifacts, dict) else None
    expected_path = stage_dir / "MARKET_GRAPH.parquet"
    if (
        not isinstance(graph_artifact, dict)
        or Path(str(graph_artifact.get("path") or "")).resolve() != expected_path.resolve()
        or not expected_path.is_file()
        or expected_path.is_symlink()
        or graph_artifact.get("bytes") != expected_path.stat().st_size
        or graph_artifact.get("sha256") != sha256_file(expected_path)
    ):
        raise Deep03InputError("market graph artifact binding mismatch")
    return expected_path


def _validate_l2_result(result: Mapping[str, Any], source_binding: str) -> None:
    expected = {
        "schema_version": L2_EXECUTION_SCHEMA,
        "state": "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS",
        "claim_tier": "DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL",
        "dwell_semantics": DWELL_SEMANTICS,
        "source_binding": source_binding,
    }
    for field, value in expected.items():
        if result.get(field) != value:
            raise Deep03InputError(f"L2 execution receipt mismatch: {field}")
    availability = result.get("availability")
    if not isinstance(availability, dict):
        raise Deep03InputError("L2 availability receipt is missing")
    if availability.get("captured_dates") != list(L2_CAPTURE_DATES):
        raise Deep03InputError("L2 captured-date coverage mismatch")
    if availability.get("explicit_absent_dates") != list(L2_ABSENT_DATES):
        raise Deep03InputError("L2 absent-date coverage mismatch")
    eligible_dates = availability.get("included_clean_dates")
    excluded_dates = availability.get("excluded_data_quality_dates")
    if not isinstance(eligible_dates, list) or not isinstance(excluded_dates, list):
        raise Deep03InputError(
            "L2 repaired receipt must declare eligible_dates and quality_excluded_dates"
        )
    if (
        eligible_dates != sorted(set(eligible_dates))
        or excluded_dates != sorted(set(excluded_dates))
        or eligible_dates != list(L2_ANALYSIS_DATES)
        or excluded_dates != sorted(L2_KNOWN_EXCLUDED_DATES)
        or set(eligible_dates) & set(excluded_dates)
        or set(eligible_dates) | set(excluded_dates) != set(L2_CAPTURE_DATES)
    ):
        raise Deep03InputError("L2 eligible/excluded date partition is invalid")
    conservation = result.get("row_conservation")
    conservation_fields = {
        "all_captured_physical_coverage",
        "included_clean_replay",
        "replay_classification",
        "included_plus_excluded_coverage",
    }
    if (
        not isinstance(conservation, dict)
        or set(conservation) != conservation_fields
        or any(
            not isinstance(conservation[field], dict)
            or conservation[field].get("state") != "PASS"
            or conservation[field].get("observed_rows")
            != conservation[field].get("expected_rows")
            for field in conservation_fields
        )
    ):
        raise Deep03InputError("L2 row-conservation equations are not all PASS")
    stages = result.get("stages")
    if not isinstance(stages, list) or {
        str(row.get("stage")) for row in stages if isinstance(row, dict)
    } != REQUIRED_L2_STAGES:
        raise Deep03InputError("L2 durable stage set is incomplete or unexpected")
    quality = result.get("quality")
    if not isinstance(quality, dict) or set(quality) != set(L2_CAPTURE_DATES):
        raise Deep03InputError("L2 quality date set is incomplete")
    pass_dates = []
    explicitly_excluded = []
    for date, row in sorted(quality.items()):
        if not isinstance(row, dict):
            raise Deep03InputError(f"L2 quality row is invalid: {date}")
        blockers = row.get("blockers")
        if (
            row.get("receipt_state") == "PASS"
            and row.get("analysis_disposition") == "INCLUDED_CLEAN_DATE"
            and row.get("usable_for_estimands") is True
            and blockers == []
        ):
            pass_dates.append(date)
        elif (
            row.get("analysis_disposition") == "EXCLUDED_DATA_QUALITY"
            and row.get("usable_for_estimands") is False
            and isinstance(blockers, list)
            and blockers
        ):
            explicitly_excluded.append(date)
        else:
            raise Deep03InputError(f"L2 quality row is neither clean nor excluded: {date}")
    if pass_dates != eligible_dates or explicitly_excluded != excluded_dates:
        raise Deep03InputError("L2 quality rows differ from eligible/excluded ledger")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _validated_stage_partitions(
    con,
    store: BoundedCheckpointStore,
    l2_result: Mapping[str, Any],
    stage: str,
) -> list[dict[str, Any]]:
    stage_rows = [row for row in l2_result["stages"] if row.get("stage") == stage]
    if len(stage_rows) != 1:
        raise Deep03InputError(f"L2 stage receipt is not unique: {stage}")
    stage_row = stage_rows[0]
    manifest_path = store.root / stage / "MANIFEST.json"
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or stage_row.get("manifest_sha256") != sha256_file(manifest_path)
    ):
        raise Deep03InputError(f"L2 stage manifest binding mismatch: {stage}")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise Deep03InputError(f"L2 stage manifest unreadable: {stage}: {exc}") from exc
    if raw != _canonical_json(manifest):
        raise Deep03InputError(f"L2 stage manifest is not canonical: {stage}")
    fixed = {
        "state": "COMPLETE",
        "stage": stage,
        "stage_version": stage_row.get("stage_version"),
        "source_binding": store.source_binding,
        "partition_count": stage_row.get("partition_count"),
        "row_count": stage_row.get("row_count"),
    }
    for field, value in fixed.items():
        if manifest.get(field) != value:
            raise Deep03InputError(f"L2 stage manifest field mismatch: {stage}/{field}")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or len(partitions) != manifest["partition_count"]:
        raise Deep03InputError(f"L2 stage partition ledger mismatch: {stage}")
    validated: list[dict[str, Any]] = []
    for row in partitions:
        if not isinstance(row, dict) or not isinstance(row.get("partition_key"), str):
            raise Deep03InputError(f"L2 stage partition row is invalid: {stage}")
        receipt = store.validate_partition(
            con,
            stage=stage,
            stage_version=str(manifest["stage_version"]),
            partition_key=row["partition_key"],
        )
        receipt_path = store._paths(stage, row["partition_key"])[1]
        if (
            row.get("receipt_path") != receipt_path.relative_to(store.root).as_posix()
            or row.get("receipt_sha256") != sha256_file(receipt_path)
            or row.get("data_sha256") != receipt["data"]["sha256"]
            or row.get("row_count") != receipt["data"]["row_count"]
        ):
            raise Deep03InputError(f"L2 stage partition binding mismatch: {stage}")
        payload = (store.root / receipt["data"]["path"]).resolve()
        if not _is_relative_to(payload, store.root) or payload.is_symlink():
            raise Deep03InputError(f"L2 stage payload escaped checkpoint root: {stage}")
        validated.append({
            "partition_key": row["partition_key"],
            "path": payload,
            "receipt": receipt,
        })
    if not validated:
        raise Deep03InputError(f"L2 stage has no durable payloads: {stage}")
    return validated


def _validated_stage_paths(
    con,
    store: BoundedCheckpointStore,
    l2_result: Mapping[str, Any],
    stage: str,
) -> list[Path]:
    return [
        row["path"]
        for row in _validated_stage_partitions(con, store, l2_result, stage)
    ]


def _l2_report_tables(
    con,
    store: BoundedCheckpointStore,
    l2_result: Mapping[str, Any],
    graph_result: Mapping[str, Any],
) -> dict[str, Any]:
    availability_paths = _validated_stage_paths(
        con, store, l2_result, "l2_availability"
    )
    atlas_paths = _validated_stage_paths(con, store, l2_result, "l2_exact_atlas")
    match_partitions = _validated_stage_partitions(
        con, store, l2_result, "l2_matched_controls"
    )
    availability = rows_as_dicts(
        con,
        f"SELECT * FROM read_parquet({path_list(availability_paths)},"
        "hive_partitioning=false) ORDER BY date",
    )
    if [row.get("date") for row in availability] != list(L2_SCOPE_DATES):
        raise Deep03InputError("L2 availability table does not cover the exact scope")
    eligible = set(l2_result["availability"]["included_clean_dates"])
    excluded = set(l2_result["availability"]["excluded_data_quality_dates"])
    expected_states = {
        date: (
            "ABSENT_NOT_CAPTURED"
            if date in L2_ABSENT_DATES
            else "CAPTURED_CLEAN_INCLUDED"
            if date in eligible
            else "EXCLUDED_DATA_QUALITY"
        )
        for date in L2_SCOPE_DATES
    }
    if any(row.get("state") != expected_states[row["date"]] for row in availability):
        raise Deep03InputError("L2 durable availability states differ from quality ledger")
    atlas = rows_as_dicts(
        con,
        "SELECT record_kind,count(*)::BIGINT AS strata_rows,"
        "coalesce(sum(n_rows),0)::BIGINT AS represented_rows "
        f"FROM read_parquet({path_list(atlas_paths)},hive_partitioning=false) "
        "GROUP BY record_kind ORDER BY record_kind",
    )
    if not atlas:
        raise Deep03InputError("L2 atlas summary is empty")
    atlas_relation = (
        f"read_parquet({path_list(atlas_paths)},hive_partitioning=false)"
    )
    refill_hazard = rows_as_dicts(
        con,
        "SELECT date,horizon_start_us,horizon_end_us,"
        "sum(at_risk_n)::BIGINT AS at_risk_n,"
        "sum(events_n)::BIGINT AS events_n,"
        "(sum(events_n)::DOUBLE/nullif(sum(at_risk_n),0)) AS pooled_interval_hazard,"
        "min(survival_to_end)::DOUBLE AS min_stratum_survival,"
        "max(survival_to_end)::DOUBLE AS max_stratum_survival,"
        "count(*)::BIGINT AS strata "
        f"FROM {atlas_relation} WHERE record_kind='REFILL_HAZARD' "
        "GROUP BY date,horizon_start_us,horizon_end_us "
        "ORDER BY date,horizon_start_us",
    )
    state_dwell = rows_as_dicts(
        con,
        "SELECT date,staleness_ttl_ns,endpoint_reason,"
        "sum(n_rows)::BIGINT AS n_rows,"
        "coalesce(sum(total_dwell_us),0)::DOUBLE AS total_dwell_us "
        f"FROM {atlas_relation} WHERE record_kind='STATE' "
        "GROUP BY date,staleness_ttl_ns,endpoint_reason "
        "ORDER BY date,staleness_ttl_ns,endpoint_reason",
    )
    observed_ttls = {row.get("staleness_ttl_ns") for row in state_dwell}
    if state_dwell and observed_ttls != set(STALE_TTL_REGISTRY_NS):
        raise Deep03InputError(
            "L2 state dwell table does not cover the finite staleness TTL registry"
        )
    match_coverage: list[dict[str, Any]] = []
    match_balance: list[dict[str, Any]] = []
    match_concentration: list[dict[str, Any]] = []
    for partition in sorted(
        match_partitions, key=lambda row: str(row["partition_key"])
    ):
        key = str(partition["partition_key"])
        if not key.startswith("date="):
            raise Deep03InputError(f"L2 match partition key is not dated: {key}")
        date = key[len("date="):]
        metrics = partition["receipt"].get("metrics")
        if not isinstance(metrics, dict):
            raise Deep03InputError(f"L2 match receipt metrics missing: {key}")
        coverage = metrics.get("episode_coverage")
        balance = metrics.get("balance")
        concentration = metrics.get("concentration")
        if (
            not isinstance(coverage, dict)
            or not isinstance(balance, dict)
            or not isinstance(concentration, dict)
        ):
            raise Deep03InputError(
                f"L2 match receipt lacks coverage/balance/concentration: {key}"
            )
        match_coverage.append({"date": date, **coverage})
        match_balance.append({"date": date, **balance})
        for identity in ("control_market", "control_event"):
            identity_row = concentration.get(identity)
            if not isinstance(identity_row, dict):
                raise Deep03InputError(
                    f"L2 match concentration identity missing: {key}: {identity}"
                )
            match_concentration.append(
                {"date": date, "identity": identity, **identity_row}
            )
    stage_rows = [
        {
            "stage": row["stage"],
            "partition_count": row["partition_count"],
            "row_count": row["row_count"],
            "manifest_sha256": row["manifest_sha256"],
        }
        for row in sorted(l2_result["stages"], key=lambda value: value["stage"])
    ]
    quality_rows = [
        {
            "date": date,
            "receipt_state": row.get("receipt_state"),
            "analysis_disposition": row.get("analysis_disposition"),
            "usable_for_estimands": row.get("usable_for_estimands"),
            "logical_key": row.get("logical_key"),
            "source_version_id": row.get("source_version_id"),
            "sha256": row.get("sha256"),
            "source_rows": row.get("source_rows"),
            "blockers": "; ".join(row.get("blockers") or []) or "none",
        }
        for date, row in sorted(l2_result["quality"].items())
    ]
    coverage_by_date = []
    for row in graph_result.get("channel_by_date") or []:
        projected = dict(row)
        date = str(projected.get("date") or "")
        projected["l2_analysis_state"] = expected_states.get(date, "OUTSIDE_SCOPE")
        coverage_by_date.append(projected)
    return {
        "schema_version": "deep03-fullscope-l2-report-tables-v2",
        "state": "COMPLETE",
        "claim_tier": "DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL",
        "source_binding": store.source_binding,
        "dwell_semantics": DWELL_SEMANTICS,
        "staleness_ttl_registry_ns": list(STALE_TTL_REGISTRY_NS),
        "primary_staleness_ttl_ns": PRIMARY_STALE_TTL_NS,
        "coverage_by_date": coverage_by_date,
        "mapping_status": list(graph_result.get("mapping_status") or []),
        "availability": availability,
        "quality": quality_rows,
        "atlas_summary": atlas,
        "refill_hazard": refill_hazard,
        "state_dwell": state_dwell,
        "match_coverage": match_coverage,
        "match_balance": match_balance,
        "match_concentration": match_concentration,
        "stage_summary": stage_rows,
        "limitations": list(l2_result.get("limitations") or []),
    }


def _render_fullscope_report(
    input_manifest: dict[str, Any],
    quality: dict[str, Any],
    methods: list[dict[str, Any]],
    graph_result: dict[str, Any],
    l2_result: dict[str, Any],
    l2_tables: dict[str, Any],
) -> str:
    base = _render_report(input_manifest, quality, methods)
    source = (
        "exact INPUT_MANIFEST + FULLSCOPE_MARKET_GRAPH_RESULT.json + "
        "FULLSCOPE_L2_EXECUTION_RECEIPT.json"
    )
    coverage = l2_tables["coverage_by_date"]
    eligible_dates = l2_result["availability"]["included_clean_dates"]
    excluded_dates = l2_result["availability"]["excluded_data_quality_dates"]
    graph_chart = _bar_chart(coverage, "date", "l2_rows")
    limitation_rows = [
        {"limitation": value} for value in l2_tables.get("limitations") or []
    ]
    hazard_rows = l2_tables["refill_hazard"]
    hazard_chart_rows = [
        {
            "bucket": (
                f"{row.get('date')} {row.get('horizon_start_us')}-"
                f"{row.get('horizon_end_us')}us"
            ),
            "pooled_interval_hazard": row.get("pooled_interval_hazard"),
        }
        for row in hazard_rows
    ]
    hazard_chart = _bar_chart(
        hazard_chart_rows, "bucket", "pooled_interval_hazard"
    )
    ttl_registry_label = ", ".join(
        f"{ttl / 1_000_000:g}ms" for ttl in l2_tables["staleness_ttl_registry_ns"]
    )
    primary_ttl_label = (
        f"{l2_tables['primary_staleness_ttl_ns'] / 1_000_000:g}ms"
    )
    extension = f"""
    <section class="meta" id="FULLSCOPE-COVERAGE"><h2>Full-scope channel coverage</h2>
    <p><strong>Base cohort only:</strong> every admitted L1/trade row plus every
    legitimately captured, sequence-valid L2 row. RFQ is not read by this runner.</p>
    {_table(coverage, source)}<h3>L2 rows by exact date</h3>{graph_chart}
    <h3>Market graph mapping ledger</h3>{_table(l2_tables['mapping_status'], source)}</section>
    <section class="meta" id="FULLSCOPE-L2"><h2>L2 / SNBD sequence-valid analysis</h2>
    <p><span class="status tested">TESTED · DESCRIPTIVE ONLY</span></p>
    <p>Sequence-quality eligible dates: {html.escape(', '.join(eligible_dates))}.
    Quality-blocked and excluded from L2 estimands: {html.escape(', '.join(excluded_dates) or 'none')}.
    Explicitly absent, not zero-filled: {html.escape(', '.join(L2_ABSENT_DATES))}.</p>
    <h3>Availability</h3>{_table(l2_tables['availability'], source)}
    <h3>Full-stream sequence-quality evidence</h3>{_table(l2_tables['quality'], source)}
    <h3>Topology / retreat / episode / refill-hazard atlas</h3>{_table(l2_tables['atlas_summary'], source)}
    <h3>Refill hazard by 100ms horizon interval (discrete risk set, pooled across strata)</h3>
    {_table(hazard_rows, source)}{hazard_chart}
    <h3>TTL-capped state dwell (registry {html.escape(ttl_registry_label)}; primary {html.escape(primary_ttl_label)})</h3>
    <p>Dwell is <strong>TTL-capped update-to-update dwell</strong>, never full
    market uptime: pause/close/terminal lifecycle is not a proven L2 boundary
    in these inputs, and an interval whose next update exceeds its TTL is
    RIGHT_CENSORED_STALE_TTL.</p>
    {_table(l2_tables['state_dwell'], source)}
    <h3>Matched-control coverage</h3>{_table(l2_tables['match_coverage'], source)}
    <h3>Matched-control covariate balance</h3>{_table(l2_tables['match_balance'], source)}
    <h3>Matched-control concentration</h3>{_table(l2_tables['match_concentration'], source)}
    <h3>Durable exact-input stages</h3>{_table(l2_tables['stage_summary'], source)}
    <h3>Limits</h3>{_table(limitation_rows, source)}
    <p>No queue position, fill probability, fee-after PnL, causal effect,
    confirmation, promotion, or live-trading claim is made.</p></section>
    """
    marker = '<section class="meta"><h2>Decision</h2>'
    if marker not in base:
        raise Deep03InputError("base HTML report decision anchor is missing")
    result = base.replace(marker, extension + marker, 1)
    result = result.replace(
        "<h1>Deep03 D3-W2A V3 Open Discovery</h1>",
        "<h1>Deep03 Full-Scope Base + L2 Open Discovery</h1>"
        '<div class="banner">RFQ NOT INCLUDED IN THIS EXECUTION UNIT</div>',
        1,
    )
    if "Full-scope channel coverage" not in result or "L2 / SNBD" not in result:
        raise Deep03InputError("full-scope report extension was not rendered")
    return result


def _atomic_publish_file(source: Path, target: Path) -> None:
    """Publish one same-filesystem file without overwriting an existing path."""
    source = Path(source)
    target = Path(target)
    if not source.is_file() or source.is_symlink():
        raise Deep03InputError(f"publish source is not a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise Deep03InputError(f"artifact target already exists: {target}")
    try:
        os.link(source, target)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise Deep03InputError("artifact staging and run directory differ by filesystem") from exc
        raise
    try:
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    source.unlink()


def run_fullscope_discovery(
    *,
    run_dir: Path,
    authority_path: Path,
    arm_path: Path,
    arm_claim_root: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    audit_path: Path,
    w0_release_path: Path,
    w1_release_path: Path,
    w1_complete_path: Path,
    l2_independent_audit_receipt_path: Path,
    l2_independent_audit_report_path: Path,
    checkpoint_root: Path,
    checkpoint_reserve_bytes: int = DEFAULT_CHECKPOINT_RESERVE_BYTES,
    memory_limit: str = "16GB",
    threads: int = 2,
    l2_market_buckets: int = DEFAULT_L2_MARKET_BUCKETS,
    expected_owner_uid: int = 0,
    authority_now: dt.datetime | None = None,
    claim_invocation_id: str | None = None,
    claim_proc_cgroup_path: Path | None = None,
    required_checkpoint_parent: Path = CANONICAL_CHECKPOINT_ROOT,
) -> Path:
    refuse_credential_environment()
    _validate_l2_market_buckets(l2_market_buckets)
    initial_sources = fullscope_source_hashes()
    authority_context = load_authority_context(
        authority_path=authority_path,
        arm_path=arm_path,
        arm_claim_root=arm_claim_root,
        plan_path=plan_path,
        runtime_commit_path=runtime_commit_path,
        audit_path=audit_path,
        w0_release_path=w0_release_path,
        w1_release_path=w1_release_path,
        w1_complete_path=w1_complete_path,
        expected_owner_uid=expected_owner_uid,
        now=authority_now,
        claim_invocation_id=claim_invocation_id,
        claim_proc_cgroup_path=claim_proc_cgroup_path,
    )
    _require_fullscope_authority(authority_context)
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise Deep03InputError(f"prepared run directory missing: {run_dir}")
    if (run_dir / "RUN_COMPLETE.json").exists():
        raise Deep03InputError("RUN_COMPLETE is immutable; use a new run_id")
    allowed = {
        "ADOPTED_PLAN.md",
        "ARM_CLAIM.json",
        "AUDIT.md",
        "AUTHORITY.json",
        "AUTHORITY_BINDING.json",
        "BASE_COMMIT.txt",
        "D3_W0_RELEASE.json",
        "D3_W1_RELEASE.json",
        "EXECUTION_ARM.json",
        "INPUT_MANIFEST.json",
        "PREPARE_RECEIPT.json",
        "W1_COMPLETE.json",
    }
    unexpected = sorted(path.name for path in run_dir.iterdir() if path.name not in allowed)
    if unexpected:
        raise Deep03InputError(
            "prepared run contains stale/partial artifacts; use a new run_id: "
            + ",".join(unexpected)
        )
    input_manifest, prepare = ensure_run_inputs_current(run_dir, authority_context)
    _require_base_without_rfq(input_manifest)
    if list(input_manifest.get("release_dates") or []) != list(L2_SCOPE_DATES):
        raise Deep03InputError(
            "full-scope base/L2 run must bind exactly 2026-07-10 through 2026-07-17"
        )
    l2_audit = _validate_l2_independent_audit(
        receipt_path=l2_independent_audit_receipt_path,
        report_path=l2_independent_audit_report_path,
        runtime_commit_path=runtime_commit_path,
        input_manifest=input_manifest,
        input_manifest_sha256=prepare["input_manifest_sha256"],
        source_modules_sha256=initial_sources,
    )
    checkpoint_namespace, checkpoint_namespace_id, source_binding = _checkpoint_namespace(
        checkpoint_root=checkpoint_root,
        input_manifest=input_manifest,
        run_dir=run_dir,
        required_parent=required_checkpoint_parent,
    )
    disk_preflight = _fullscope_disk_headroom_preflight(
        input_manifest=input_manifest,
        namespace=checkpoint_namespace,
        source_binding=source_binding,
        reserve_bytes=checkpoint_reserve_bytes,
    )
    lock_handle = (run_dir / "PREPARE_RECEIPT.json").open("rb")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Deep03InputError("another runner holds this prepared run") from exc
        started = utc_now()
        scratch = run_dir / ".scratch"
        graph_stage = scratch / "fullscope-graph"
        import duckdb

        con = duckdb.connect()
        try:
            _configure_duckdb(con, scratch, memory_limit, threads)
            capabilities, methods, bounded_receipt = execute_all_bounded(
                con,
                input_manifest,
                checkpoint_namespace,
                market_buckets=MARKET_BUCKETS,
            )
            if (
                not isinstance(bounded_receipt, dict)
                or bounded_receipt.get("schema_version") != BOUNDED_EXECUTION_RECEIPT_SCHEMA
                or bounded_receipt.get("state") != "COMPLETE"
                or bounded_receipt.get("source_binding") != source_binding
            ):
                raise Deep03InputError("bounded B01--B04 checkpoint receipt mismatch")
            graph_result = build_market_graph(
                con, input_manifest, output_dir=graph_stage
            )
            graph_source = _validate_graph_result(
                graph_result, input_manifest, graph_stage
            )
            with BoundedCheckpointStore(checkpoint_namespace, source_binding) as store:
                l2_result = execute_l2_snbd_bounded(
                    con,
                    input_manifest,
                    store,
                    market_buckets=l2_market_buckets,
                )
                _validate_l2_result(l2_result, source_binding)
                l2_tables = _l2_report_tables(
                    con, store, l2_result, graph_result
                )
        finally:
            con.close()

        quality = _quality_receipt(input_manifest)
        estimability = _estimability_receipt(input_manifest, capabilities, methods)
        exclusions = _exclusion_receipt(input_manifest, methods)
        method_receipt = _method_receipt(input_manifest, methods)
        report = _render_fullscope_report(
            input_manifest,
            quality,
            methods,
            graph_result,
            l2_result,
            l2_tables,
        )
        if fullscope_source_hashes() != initial_sources:
            raise Deep03InputError("full-scope source modules changed during execution")

        graph_target = run_dir / "TABLES" / "MARKET_GRAPH.parquet"
        _atomic_publish_file(graph_source, graph_target)
        normalized_graph = dict(graph_result)
        normalized_graph["artifacts"] = {
            "market_graph": {
                "path": graph_target.relative_to(run_dir).as_posix(),
                "bytes": graph_target.stat().st_size,
                "sha256": sha256_file(graph_target),
            }
        }
        checkpoint_receipt = {
            "schema_version": "deep03-fullscope-checkpoint-reuse-receipt-v1",
            "state": "COMPLETE",
            "run_id": input_manifest["run_id"],
            "source_binding": source_binding,
            "checkpoint_namespace_id": checkpoint_namespace_id,
            "disk_headroom_preflight": disk_preflight,
            "bounded_execution": bounded_receipt,
            "l2_execution": l2_result,
        }
        execution_receipt = {
            "schema_version": SCHEMA_EXECUTION,
            "state": "COMPLETE",
            "run_id": input_manifest["run_id"],
            "source_binding": source_binding,
            "declared_methods": list(FULLSCOPE_METHODS),
            "completed_methods": list(FULLSCOPE_METHODS),
            "base_method_receipt_state": method_receipt["all_declared_methods_closed"],
            "market_graph_state": normalized_graph["status"],
            "l2_state": l2_result["state"],
            "rfq_scope": "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED",
            "candidate_or_profit_claim": False,
            "l2_independent_audit_receipt_sha256": l2_audit["receipt_sha256"],
            "l2_independent_audit_report_sha256": l2_audit["report_sha256"],
            "source_modules_sha256": initial_sources,
        }
        results = {
            "schema_version": SCHEMA_RESULTS,
            "run_id": input_manifest["run_id"],
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "research_stage": "OPEN_DISCOVERY",
            "work_package": "DEEP03-FULL-BASE-L2-01",
            "evidence_labels": list(LABELS),
            "release_ids": input_manifest["release_ids"],
            "conclusion_status": "DESCRIPTIVE_FULLSCOPE_BASE_L2_COMPLETE",
            "candidate_or_profit_claim": False,
            "rfq_scope": "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED",
            "base_methods": methods,
            "market_graph": normalized_graph,
            "l2_execution": l2_result,
            "l2_report_tables": l2_tables,
        }

        atomic_write_json(run_dir / "DATA_QUALITY_RECEIPT.json", quality, exclusive=True)
        atomic_write_json(run_dir / "ESTIMABILITY_PREFLIGHT.json", estimability, exclusive=True)
        atomic_write_json(run_dir / "EXCLUSION_WATERFALL.json", exclusions, exclusive=True)
        atomic_write_json(run_dir / "METHOD_EXECUTION_RECEIPT.json", method_receipt, exclusive=True)
        atomic_write_json(
            run_dir / "CHECKPOINT_REUSE_RECEIPT.json", checkpoint_receipt, exclusive=True
        )
        atomic_write_json(
            run_dir / "FULLSCOPE_EXECUTION_RECEIPT.json", execution_receipt, exclusive=True
        )
        atomic_write_json(
            run_dir / "FULLSCOPE_MARKET_GRAPH_RESULT.json",
            normalized_graph,
            exclusive=True,
        )
        atomic_write_json(
            run_dir / "FULLSCOPE_L2_EXECUTION_RECEIPT.json",
            l2_result,
            exclusive=True,
        )
        atomic_write_json(
            run_dir / "FULLSCOPE_L2_REPORT_TABLES.json", l2_tables, exclusive=True
        )
        atomic_write_bytes(
            run_dir / "L2_INDEPENDENT_AUDIT_RECEIPT.json",
            l2_audit["receipt_bytes"],
            exclusive=True,
        )
        atomic_write_bytes(
            run_dir / "L2_INDEPENDENT_AUDIT_REPORT.md",
            l2_audit["report_bytes"],
            exclusive=True,
        )
        atomic_write_json(run_dir / "RESULTS.json", results, exclusive=True)
        atomic_write_bytes(
            run_dir / "REPORT" / "index.html", report.encode("utf-8"), exclusive=True
        )
        reproduction = {
            "schema_version": "deep03-fullscope-base-l2-reproduction-receipt-v1",
            "run_id": input_manifest["run_id"],
            "run_command_shape": (
                "w09-run deep03-v3-fullscope-run --run-dir RUN_DIR "
                "--checkpoint-root /srv/w09-research/checkpoints "
                "--checkpoint-reserve-bytes RESERVE_BYTES --l2-market-buckets N "
                "--authority AUTHORITY --arm-file ARM --arm-claim-root CLAIM_ROOT "
                "--plan PLAN --runtime-commit COMMIT --audit AUDIT "
                "--w0-release W0 --w1-release W1 --w1-complete W1_COMPLETE "
                "--l2-independent-audit-receipt L2_AUDIT_JSON "
                "--l2-independent-audit-report L2_AUDIT_MD"
            ),
            "source_modules_sha256": initial_sources,
            "duckdb_version": duckdb.__version__,
            "memory_limit": memory_limit,
            "threads": threads,
            "l2_market_buckets": l2_market_buckets,
            "l2_independent_audit_receipt_sha256": l2_audit["receipt_sha256"],
            "l2_independent_audit_report_sha256": l2_audit["report_sha256"],
            "checkpoint_namespace_id": checkpoint_namespace_id,
            "checkpoint_source_binding": source_binding,
            "checkpoint_reserve_bytes": checkpoint_reserve_bytes,
            "disk_headroom_preflight": disk_preflight,
            "network_reads": 0,
            "rfq_reads": 0,
            "order_actions": 0,
        }
        atomic_write_json(
            run_dir / "REPRODUCTION_RECEIPT.json", reproduction, exclusive=True
        )
        if scratch.exists():
            shutil.rmtree(scratch)
        if fullscope_source_hashes() != initial_sources:
            raise Deep03InputError("full-scope source modules changed before completion")
        artifacts = _artifact_rows(run_dir)
        sums = "".join(f"{row['sha256']}  {row['path']}\n" for row in artifacts)
        atomic_write_bytes(
            run_dir / "ARTIFACT_SHA256SUMS", sums.encode("ascii"), exclusive=True
        )
        complete = {
            "schema_version": SCHEMA_COMPLETE,
            "run_id": input_manifest["run_id"],
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "research_stage": "OPEN_DISCOVERY",
            "work_package": "DEEP03-FULL-BASE-L2-01",
            "evidence_labels": list(LABELS),
            "release_ids": input_manifest["release_ids"],
            "started_at_utc": started,
            "completed_at_utc": utc_now(),
            "exit_status": 0,
            "state": "RUN_COMPLETE",
            "declared_methods": list(FULLSCOPE_METHODS),
            "completed_methods": list(FULLSCOPE_METHODS),
            "input_manifest_sha256": prepare["input_manifest_sha256"],
            "results_sha256": sha256_file(run_dir / "RESULTS.json"),
            "fullscope_execution_receipt_sha256": sha256_file(
                run_dir / "FULLSCOPE_EXECUTION_RECEIPT.json"
            ),
            "market_graph_result_sha256": sha256_file(
                run_dir / "FULLSCOPE_MARKET_GRAPH_RESULT.json"
            ),
            "l2_execution_receipt_sha256": sha256_file(
                run_dir / "FULLSCOPE_L2_EXECUTION_RECEIPT.json"
            ),
            "l2_independent_audit_receipt_sha256": sha256_file(
                run_dir / "L2_INDEPENDENT_AUDIT_RECEIPT.json"
            ),
            "l2_independent_audit_report_sha256": sha256_file(
                run_dir / "L2_INDEPENDENT_AUDIT_REPORT.md"
            ),
            "report_sha256": sha256_file(run_dir / "REPORT" / "index.html"),
            "checkpoint_reuse_receipt_sha256": sha256_file(
                run_dir / "CHECKPOINT_REUSE_RECEIPT.json"
            ),
            "checkpoint_namespace_id": checkpoint_namespace_id,
            "checkpoint_source_binding": source_binding,
            "artifact_sha256sums_sha256": sha256_file(
                run_dir / "ARTIFACT_SHA256SUMS"
            ),
            "artifacts": artifacts,
            "source_modules_sha256": initial_sources,
            "host": {
                "hostname": socket.gethostname(),
                "system": platform.system(),
                "machine": platform.machine(),
            },
            "rfq_scope": "NOT_INCLUDED_SEPARATE_OVERLAY_REQUIRED",
            "rfq_reads": 0,
            "network_reads": 0,
            "production_mutations": 0,
            "order_actions": 0,
            "candidate_or_profit_claim": False,
            "authority_binding": authority_context["binding"],
            "authority_artifact_sha256s": authority_context["artifact_sha256s"],
        }
        # Intentionally the final filesystem write in a successful bundle.
        atomic_write_json(run_dir / "RUN_COMPLETE.json", complete, exclusive=True)
        return run_dir / "RUN_COMPLETE.json"
    finally:
        lock_handle.close()


# --------------------------------------------------------------------------
# Fresh RFQ overlay unit — a SEPARATE cohort, never joined with the base.
#
# Cohort separation contract (operator scope decision, handoff
# DEEP03_FULLSCOPE_L2_FRESH_RFQ_HANDOFF_2026-07-19):
#   - base cohort: exact V3 releases 2026-07-10..17 (L1/trades/graph/L2),
#     RFQ forbidden and absent (_require_base_without_rfq refuses any RFQ
#     object in the historical INPUT_MANIFEST);
#   - fresh RFQ cohort: post-T0 2026-07-20+ overlay generation only, with
#     its own source binding, row-conservation ledger and claims;
#   - the two cohorts are never merged into one manifest, binding or claim.
#
# D07 external anchor provenance (MANDATORY blocker I4b in
# docs/plan_audits/AUDIT_DEEP03_RFQ_QUALITY_D07_ATTESTATION_2026-07-19.md):
# the anchor may enter this runtime ONLY by reading the pinned
# root-installed 0444 independent-audit receipt below.  No function in this
# runner accepts a caller-constructed anchor object.
# --------------------------------------------------------------------------

FRESH_RFQ_COHORT_SCHEMA = "deep03-fullscope-fresh-rfq-cohort-receipt-v1"
BASE_COHORT_ID = "HISTORICAL_BASE_L1_TRADES_L2_2026-07-10_2026-07-17"
FRESH_RFQ_COHORT_ID = "FRESH_RFQ_POST_T0_2026-07-20"
FRESH_RFQ_GENERATION = "fresh-rfq-20260720-01"
FRESH_RFQ_STRICT_T0_UTC = "2026-07-20T00:00:00Z"
D07_EXTERNAL_ANCHOR_INSTALL_PATH = Path(
    "/etc/w09/deep03/d07-external-anchor.json"
)
D07_EXTERNAL_ANCHOR_MAX_BYTES = 1024 * 1024


def _rfq_module():
    """Import the audited RFQ overlay module lazily.

    The base/L2 unit above must stay importable and byte-identical in
    behavior even where the fresh-RFQ dependencies are not installed.
    """
    import deep03_v3_rfq_bounded

    return deep03_v3_rfq_bounded


def load_d07_external_anchor(
    anchor_path: Path | str, *, expected_owner_uid: int = 0
) -> dict[str, Any]:
    """Load the D07 external anchor from the pinned root-installed receipt.

    Fail-closed, typed provenance checks (auditor-recommended hardening for
    blocker I4b): the path must be exactly the pinned /etc/w09/deep03/
    install path; the file must be a regular non-symlink file
    (``O_NOFOLLOW``), owned by ``expected_owner_uid`` (root in production),
    with mode exactly 0444 (not writable by anyone), within the size bound;
    and its bytes must parse to the audited external-anchor schema
    (validated by the audited module itself).  This function is the ONLY
    code path in this runner that can produce an anchor object.
    """
    rfq = _rfq_module()
    anchor_path = Path(anchor_path)
    if anchor_path != D07_EXTERNAL_ANCHOR_INSTALL_PATH:
        raise rfq.FreshRfqResearchError(
            "D07_ANCHOR_PATH",
            "external anchor must be read from the pinned root-installed "
            f"path {D07_EXTERNAL_ANCHOR_INSTALL_PATH}, not {anchor_path}",
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(anchor_path, flags)
    except FileNotFoundError as exc:
        raise rfq.FreshRfqResearchError(
            "D07_ANCHOR_NOT_INSTALLED",
            f"pinned external anchor receipt is not installed: {anchor_path}",
        ) from exc
    except OSError as exc:
        raise rfq.FreshRfqResearchError(
            "D07_ANCHOR_PROVENANCE",
            f"pinned external anchor receipt is unreadable or linked: {exc}",
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise rfq.FreshRfqResearchError(
                "D07_ANCHOR_PROVENANCE",
                "pinned external anchor receipt is not a regular file",
            )
        if metadata.st_uid != expected_owner_uid:
            raise rfq.FreshRfqResearchError(
                "D07_ANCHOR_OWNER",
                "pinned external anchor receipt has owner uid "
                f"{metadata.st_uid}, required uid {expected_owner_uid}",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o444:
            raise rfq.FreshRfqResearchError(
                "D07_ANCHOR_MODE",
                "pinned external anchor receipt mode is "
                f"0{stat.S_IMODE(metadata.st_mode):o}, required exactly 0444",
            )
        if not 0 < metadata.st_size <= D07_EXTERNAL_ANCHOR_MAX_BYTES:
            raise rfq.FreshRfqResearchError(
                "D07_ANCHOR_PROVENANCE",
                "pinned external anchor receipt size is outside the bound",
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(D07_EXTERNAL_ANCHOR_MAX_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise rfq.FreshRfqResearchError(
                "D07_ANCHOR_PROVENANCE",
                "pinned external anchor receipt changed while being read",
            )
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise rfq.FreshRfqResearchError(
            "D07_ANCHOR_INVALID",
            f"pinned external anchor receipt is not valid JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise rfq.FreshRfqResearchError(
            "D07_ANCHOR_INVALID",
            "pinned external anchor receipt root is not an object",
        )
    # The audited module's own schema validator (state, authority, digest
    # lists, self-digest) is the final and only schema authority.
    return rfq._validate_d07_external_anchor(payload)


def discover_fresh_rfq_overlay_ready_paths(overlay_root: Path | str) -> list[Path]:
    """Enumerate candidate overlay READY receipts under one local root."""
    overlay_root = Path(overlay_root)
    if not overlay_root.is_dir() or overlay_root.is_symlink():
        return []
    found: list[Path] = []
    for child in sorted(overlay_root.iterdir()):
        ready = child / "READY.json"
        if (
            child.is_dir()
            and not child.is_symlink()
            and ready.is_file()
            and not ready.is_symlink()
        ):
            found.append(ready)
    return found


def _fresh_rfq_receipt_base() -> dict[str, Any]:
    return {
        "schema_version": FRESH_RFQ_COHORT_SCHEMA,
        "cohort": FRESH_RFQ_COHORT_ID,
        "base_cohort": BASE_COHORT_ID,
        "cohort_join": "FORBIDDEN_SEPARATE_SOURCE_BINDINGS_AND_LEDGERS",
        "historical_input_manifest_rfq_entries": 0,
        "generation": FRESH_RFQ_GENERATION,
        "strict_t0_utc": FRESH_RFQ_STRICT_T0_UTC,
        "old_damaged_rfq": "DATA_INTEGRITY_BLOCKED_NO_REPAIR_NO_ANALYSIS",
        "candidate_or_profit_claim": False,
        "created_at_utc": utc_now(),
    }


def run_fresh_rfq_overlay(
    *,
    overlay_root: Path,
    fresh_authority_path: Path,
    client_factory: Callable[[dict[str, Any]], Any],
    checkpoint_root: Path,
    receipt_path: Path,
    report_path: Path | None = None,
    anchor_path: Path | str = D07_EXTERNAL_ANCHOR_INSTALL_PATH,
    anchor_owner_uid: int = 0,
    hash_buckets: int | None = None,
    exact_temp_parent: Path | None = None,
    impact_adapters: Mapping[str, dict[str, Any]] | None = None,
    base_manifest_bytes_by_date: Mapping[str, bytes] | None = None,
    l2_quality_receipts_by_date: Mapping[str, dict[str, Any]] | None = None,
    d07_producer_receipt: dict[str, Any] | None = None,
    expected_eligible_dates: list[str] | None = None,
) -> dict[str, Any]:
    """Run the separate fresh-RFQ cohort, or emit an explicit non-result.

    Until the post-T0 eligibility evidence exists (validated fresh epoch
    authority for the pinned generation plus at least one overlay READY
    receipt), this writes an explicit ``WAITING``/``BLOCKED`` receipt and
    returns — it never fabricates an empty analysis result.  This unit
    never opens the historical base INPUT_MANIFEST; the base runner
    independently refuses any RFQ object (``_require_base_without_rfq``).

    There is deliberately NO parameter through which a caller can supply a
    D07 external anchor object; the anchor is derived exclusively from the
    pinned root-installed receipt via ``load_d07_external_anchor``.
    """
    rfq = _rfq_module()
    receipt_path = Path(receipt_path)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise Deep03InputError(
            f"fresh RFQ cohort receipt already exists: {receipt_path}"
        )
    fresh_authority_path = Path(fresh_authority_path)
    if not fresh_authority_path.is_file() or fresh_authority_path.is_symlink():
        raise Deep03InputError(
            f"fresh RFQ epoch authority is missing or linked: {fresh_authority_path}"
        )
    try:
        authority_doc = json.loads(
            fresh_authority_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise Deep03InputError(
            f"fresh RFQ epoch authority is unreadable: {exc}"
        ) from exc

    def _blocked(state: str, blocker: str) -> dict[str, Any]:
        receipt = {
            **_fresh_rfq_receipt_base(),
            "state": state,
            "result_kind": "EXPLICIT_NON_RESULT_STATE",
            "fabricated_rows": 0,
            "eligible_overlay_count": 0,
            "blocker": blocker,
        }
        atomic_write_json(receipt_path, receipt, exclusive=True)
        return receipt

    try:
        authority = rfq.fresh_receipts.validate_fresh_epoch_authority(
            authority_doc
        )
    except rfq.fresh_receipts.FreshRfqError as exc:
        return _blocked(
            "BLOCKED_FRESH_AUTHORITY", f"{exc.code}: {exc.detail}"
        )
    if (
        authority["generation"] != FRESH_RFQ_GENERATION
        or authority["strict_t0_utc"] != FRESH_RFQ_STRICT_T0_UTC
    ):
        return _blocked(
            "BLOCKED_COHORT_PIN",
            "fresh authority generation/T0 differ from the pinned cohort: "
            f"generation={authority['generation']} "
            f"strict_t0_utc={authority['strict_t0_utc']}",
        )
    ready_paths = discover_fresh_rfq_overlay_ready_paths(overlay_root)
    if not ready_paths:
        receipt = {
            **_fresh_rfq_receipt_base(),
            "state": "WAITING_FRESH_RFQ_ELIGIBILITY",
            "result_kind": "EXPLICIT_NON_RESULT_STATE",
            "fabricated_rows": 0,
            "eligible_overlay_count": 0,
            "overlay_root": str(Path(overlay_root)),
            "reason": (
                "no post-T0 overlay READY evidence exists yet; the first "
                "complete eligible fresh RFQ date is possible only after "
                "the frozen 24h+2h evidence delay past "
                + FRESH_RFQ_STRICT_T0_UTC
            ),
        }
        atomic_write_json(receipt_path, receipt, exclusive=True)
        return receipt

    anchor: dict[str, Any] | None = None
    try:
        anchor = load_d07_external_anchor(
            anchor_path, expected_owner_uid=anchor_owner_uid
        )
        anchor_state = "LOADED_FROM_PINNED_ROOT_RECEIPT"
    except rfq.FreshRfqResearchError as exc:
        if exc.code != "D07_ANCHOR_NOT_INSTALLED":
            raise
        anchor_state = "NOT_INSTALLED_D07_BLOCKED_UNANCHORED"

    report = rfq.run_bounded_fresh_rfq(
        overlay_ready_paths=[str(path) for path in ready_paths],
        fresh_authority=authority_doc,
        client_factory=client_factory,
        checkpoint_root=Path(checkpoint_root),
        hash_buckets=(
            rfq.DEFAULT_HASH_BUCKETS if hash_buckets is None else hash_buckets
        ),
        exact_temp_parent=exact_temp_parent,
        impact_adapters=impact_adapters,
        base_manifest_bytes_by_date=base_manifest_bytes_by_date,
        l2_quality_receipts_by_date=l2_quality_receipts_by_date,
        d07_producer_receipt=d07_producer_receipt,
        d07_external_anchor=anchor,
        report_path=report_path,
        expected_eligible_dates=expected_eligible_dates,
    )
    receipt = {
        **_fresh_rfq_receipt_base(),
        "state": "FRESH_RFQ_OVERLAY_COMPLETE",
        "result_kind": "SEPARATE_COHORT_ANALYSIS_RESULT",
        "eligible_overlay_count": len(ready_paths),
        "analysis_dates": list(report["analysis_dates"]),
        "source_binding_sha256": report["source_binding_sha256"],
        "conservation": report["conservation"],
        "d07_external_anchor_state": anchor_state,
        "d07_external_anchor_sha256": (
            None if anchor is None else anchor["anchor_sha256"]
        ),
        "d07_external_anchor_authority": (
            None if anchor is None else anchor["audit_authority"]
        ),
        "claims": {
            "cohort_scope": "FRESH_RFQ_ONLY",
            "base_cohort_claims": "NONE_BASE_COHORT_UNTOUCHED",
            "candidate_or_profit_claim": False,
        },
    }
    atomic_write_json(receipt_path, receipt, exclusive=True)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--arm-file", required=True, type=Path)
    parser.add_argument("--arm-claim-root", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runtime-commit", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--w0-release", required=True, type=Path)
    parser.add_argument("--w1-release", required=True, type=Path)
    parser.add_argument("--w1-complete", required=True, type=Path)
    parser.add_argument("--l2-independent-audit-receipt", required=True, type=Path)
    parser.add_argument("--l2-independent-audit-report", required=True, type=Path)
    parser.add_argument("--checkpoint-root", required=True, type=Path)
    parser.add_argument(
        "--checkpoint-reserve-bytes",
        default=DEFAULT_CHECKPOINT_RESERVE_BYTES,
        type=int,
    )
    parser.add_argument("--memory-limit", default="16GB")
    parser.add_argument("--threads", default=2, type=int)
    parser.add_argument(
        "--l2-market-buckets", default=DEFAULT_L2_MARKET_BUCKETS, type=int
    )
    args = parser.parse_args(argv)
    try:
        complete = run_fullscope_discovery(
            run_dir=args.run_dir,
            authority_path=args.authority,
            arm_path=args.arm_file,
            arm_claim_root=args.arm_claim_root,
            plan_path=args.plan,
            runtime_commit_path=args.runtime_commit,
            audit_path=args.audit,
            w0_release_path=args.w0_release,
            w1_release_path=args.w1_release,
            w1_complete_path=args.w1_complete,
            l2_independent_audit_receipt_path=args.l2_independent_audit_receipt,
            l2_independent_audit_report_path=args.l2_independent_audit_report,
            checkpoint_root=args.checkpoint_root,
            checkpoint_reserve_bytes=args.checkpoint_reserve_bytes,
            memory_limit=args.memory_limit,
            threads=args.threads,
            l2_market_buckets=args.l2_market_buckets,
        )
    except Exception as exc:
        try:
            if args.run_dir.is_dir() and not (args.run_dir / "RUN_COMPLETE.json").exists():
                atomic_write_json(
                    args.run_dir / "RUN_FAILED.json",
                    {
                        "schema_version": "deep03-fullscope-base-l2-run-failed-v1",
                        "failed_at_utc": utc_now(),
                        "state": "EXECUTION_FAILED",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "run_complete_written": False,
                    },
                    exclusive=True,
                )
        except Exception:
            pass
        print(f"DEEP03_FULLSCOPE_RUN_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"DEEP03_FULLSCOPE_RUN_PASS receipt={complete}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
