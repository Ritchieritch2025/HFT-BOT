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
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    L2_ABSENT_DATES,
    L2_CAPTURE_DATES,
    L2_SCOPE_DATES,
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
L2_EXECUTION_SCHEMA = "deep03-v3-l2-snbd-execution-v1"
L2_METHOD_ID = "D3-FULL-L2-SNBD-01"
FULLSCOPE_METHODS = (*METHODS, GRAPH_METHOD_ID, L2_METHOD_ID)
L2_CHECKPOINT_EXPANSION_FACTOR = 8
DEFAULT_L2_MARKET_BUCKETS = 16
FULLSCOPE_SOURCE_MODULES = (
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
    except (OSError, ValueError) as exc:
        raise Deep03InputError(f"L2 independent audit receipt unreadable: {exc}") from exc
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
        "audit_report_sha256": sha256_file(report_path),
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
        "report_bytes": report_path.read_bytes(),
        "report_sha256": sha256_file(report_path),
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
        "state": "COMPLETE",
        "claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
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
    if (result.get("row_conservation") or {}).get("state") != "PASS":
        raise Deep03InputError("L2 row conservation is not PASS")
    stages = result.get("stages")
    if not isinstance(stages, list) or {
        str(row.get("stage")) for row in stages if isinstance(row, dict)
    } != REQUIRED_L2_STAGES:
        raise Deep03InputError("L2 durable stage set is incomplete or unexpected")
    quality = result.get("quality")
    if not isinstance(quality, dict) or set(quality) != set(L2_CAPTURE_DATES):
        raise Deep03InputError("L2 quality date set is incomplete")
    if any(not isinstance(row, dict) or row.get("state") != "PASS" for row in quality.values()):
        raise Deep03InputError("L2 sequence-quality gate is not PASS")


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


def _validated_stage_paths(
    con,
    store: BoundedCheckpointStore,
    l2_result: Mapping[str, Any],
    stage: str,
) -> list[Path]:
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
    paths: list[Path] = []
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
        paths.append(payload)
    if not paths:
        raise Deep03InputError(f"L2 stage has no durable payloads: {stage}")
    return paths


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
    availability = rows_as_dicts(
        con,
        f"SELECT * FROM read_parquet({path_list(availability_paths)},"
        "hive_partitioning=false) ORDER BY date",
    )
    if [row.get("date") for row in availability] != list(L2_SCOPE_DATES):
        raise Deep03InputError("L2 availability table does not cover the exact scope")
    atlas = rows_as_dicts(
        con,
        "SELECT record_kind,count(*)::BIGINT AS strata_rows,"
        "coalesce(sum(n_rows),0)::BIGINT AS represented_rows "
        f"FROM read_parquet({path_list(atlas_paths)},hive_partitioning=false) "
        "GROUP BY record_kind ORDER BY record_kind",
    )
    if not atlas:
        raise Deep03InputError("L2 atlas summary is empty")
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
            "state": row.get("state"),
            "logical_key": row.get("logical_key"),
            "source_version_id": row.get("source_version_id"),
            "sha256": row.get("sha256"),
            "blockers": "; ".join(row.get("blockers") or []) or "none",
        }
        for date, row in sorted(l2_result["quality"].items())
    ]
    return {
        "schema_version": "deep03-fullscope-l2-report-tables-v1",
        "state": "COMPLETE",
        "claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
        "source_binding": store.source_binding,
        "coverage_by_date": list(graph_result.get("channel_by_date") or []),
        "mapping_status": list(graph_result.get("mapping_status") or []),
        "availability": availability,
        "quality": quality_rows,
        "atlas_summary": atlas,
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
    graph_chart = _bar_chart(coverage, "date", "l2_rows")
    limitation_rows = [
        {"limitation": value} for value in l2_tables.get("limitations") or []
    ]
    extension = f"""
    <section class="meta" id="FULLSCOPE-COVERAGE"><h2>Full-scope channel coverage</h2>
    <p><strong>Base cohort only:</strong> every admitted L1/trade row plus every
    legitimately captured, sequence-valid L2 row. RFQ is not read by this runner.</p>
    {_table(coverage, source)}<h3>L2 rows by exact date</h3>{graph_chart}
    <h3>Market graph mapping ledger</h3>{_table(l2_tables['mapping_status'], source)}</section>
    <section class="meta" id="FULLSCOPE-L2"><h2>L2 / SNBD sequence-valid analysis</h2>
    <p><span class="status tested">TESTED · DESCRIPTIVE ONLY</span></p>
    <p>Captured dates: {html.escape(', '.join(L2_CAPTURE_DATES))}. Explicitly absent,
    not zero-filled: {html.escape(', '.join(L2_ABSENT_DATES))}.</p>
    <h3>Availability</h3>{_table(l2_tables['availability'], source)}
    <h3>Full-stream sequence-quality evidence</h3>{_table(l2_tables['quality'], source)}
    <h3>Topology / retreat / episode / refill-hazard atlas</h3>{_table(l2_tables['atlas_summary'], source)}
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
