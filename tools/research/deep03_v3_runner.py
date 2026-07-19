#!/usr/bin/env python3
"""Run the smallest honest Deep03 OPEN_DISCOVERY / D3-W2A V3 cycle."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import platform
import re
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

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
from deep03_v3_methods import METHODS, execute_all


SCHEMA_RESULTS = "deep03-d3-w2a-v3-results-v1"
SCHEMA_COMPLETE = "deep03-d3-w2a-v3-run-complete-v1"
SIZE_RE = re.compile(r"^[1-9][0-9]*(?:KB|MB|GB|TB)$", re.IGNORECASE)


def _configure_duckdb(con, scratch: Path, memory_limit: str, threads: int) -> None:
    if SIZE_RE.fullmatch(memory_limit) is None:
        raise Deep03InputError("memory_limit must look like 8GB or 512MB")
    if threads < 1 or threads > 128:
        raise Deep03InputError("threads must be in [1,128]")
    scratch.mkdir(mode=0o750)
    escaped = str(scratch).replace("'", "''")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={threads}")
    con.execute(f"SET temp_directory='{escaped}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET TimeZone='UTC'")


def _quality_receipt(input_manifest: dict[str, Any]) -> dict[str, Any]:
    releases = []
    downgrade_count = 0
    for release in input_manifest["releases"]:
        basis = release.get("evidence_basis") or {}
        reasons = list(basis.get("downgrade_reasons") or [])
        downgrade_count += len(reasons)
        quality_objects = []
        capture_summary: dict[str, Any] = {}
        l2_summary: dict[str, Any] = {}
        for obj in input_manifest["objects"]:
            if obj["release_id"] != release["release_id"] or obj["kind"] not in {
                "capture_gap_receipt",
                "capture_gaps_projection",
                "l2_quality_receipt",
            }:
                continue
            quality_objects.append(
                {
                    "logical_key": obj["logical_key"],
                    "source_version_id": obj["source_version_id"],
                    "sha256": obj["sha256"],
                }
            )
            if obj["kind"] not in {"capture_gap_receipt", "l2_quality_receipt"}:
                continue
            try:
                payload = json.loads(Path(obj["local_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise Deep03InputError(
                    f"verified quality object cannot be decoded: {obj['logical_key']}: {exc}"
                ) from exc
            if obj["kind"] == "capture_gap_receipt":
                capture_summary = {
                    "n_files": payload.get("n_files"),
                    "records": payload.get("records"),
                    "unparsed": payload.get("unparsed"),
                    "unreadable": payload.get("unreadable"),
                    "gap_intervals": len(payload.get("gaps") or []),
                    "source_version_id": obj["source_version_id"],
                }
            else:
                l2_summary = {
                    "lines": payload.get("lines"),
                    "parse_errors": payload.get("parse_errors"),
                    "seq_gap_events": payload.get("seq_gap_events"),
                    "seq_missed_total": payload.get("seq_missed_total"),
                    "sids_with_seq_gaps": payload.get("sids_with_seq_gaps"),
                    "source_version_id": obj["source_version_id"],
                }
        releases.append(
            {
                "release_id": release["release_id"],
                "date": release["date"],
                "evidence_tier": release["evidence_tier"],
                "tl1_status": release["tl1_status"],
                "evidence_basis": basis,
                "downgrade_reasons": reasons,
                "quality_objects": quality_objects,
                "capture_gap_summary": capture_summary,
                "l2_quality_summary": l2_summary,
            }
        )
    return {
        "schema_version": "deep03-d3-w2a-data-quality-receipt-v1",
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "research_stage": "OPEN_DISCOVERY",
        "evidence_labels": list(LABELS),
        "state": (
            "EXACT_V3_WITH_DISCLOSED_DOWNGRADES"
            if downgrade_count
            else "EXACT_V3_QUALITY_EVIDENCE_PRESENT"
        ),
        "gate_scope": "DESCRIPTIVE_OPEN_DISCOVERY_ONLY",
        "confirmation_or_promotion_eligible": False,
        "release_count": len(releases),
        "downgrade_reason_count": downgrade_count,
        "rfq_objects": 0,
        "releases": releases,
    }


def _estimability_receipt(
    input_manifest: dict[str, Any],
    capabilities: dict[str, Any],
    methods: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    by_id = {method["method_id"]: method for method in methods}
    requirements = {
        "D3-B01-MARKOUT": ["L1_RECEIVE_CLOCK", "TRADE_RECEIVE_CLOCK", "TRADE_SIDE"],
        "D3-B02-ONESIDE": ["L1_RECEIVE_CLOCK", "BOOK_SIDES", "CENSOR_BOUNDARY"],
        "D3-B03-XMKT": ["LINKED_MARKETS", "PAYOUT_EXHAUSTIVENESS", "LEG_FEES", "STRICT_FILLS"],
        "D3-B04-RHYTHM": ["L1_RECEIVE_CLOCK", "TRADE_RECEIVE_CLOCK", "ACTIVE_MINUTE_DENOMINATOR"],
    }
    for method_id in METHODS:
        method = by_id[method_id]
        rows.append(
            {
                "method_id": method_id,
                "required_inputs": requirements[method_id],
                "estimability_status": method["status"],
                "reason": method["reason"],
                "eligible_output_rows": len(method.get("summary") or []),
                "reopen_trigger": (
                    "supply the exact missing fields/authority named in reason, "
                    "then prepare a new explicit-release run_id"
                    if method["status"] == "NOT_ESTIMABLE"
                    else None
                ),
            }
        )
    return {
        "schema_version": "deep03-d3-w2a-estimability-preflight-v1",
        "run_id": input_manifest["run_id"],
        "release_ids": input_manifest["release_ids"],
        "capabilities": capabilities,
        "methods": rows,
    }


def _method_receipt(input_manifest: dict[str, Any], methods: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for method in methods:
        rows.append(
            {
                "method_id": method["method_id"],
                "attempt_status": "COMPLETE",
                "method_status": method["status"],
                "reason": method["reason"],
                "query_ids": [query["query_id"] for query in method["queries"]],
                "result_rows": len(method.get("summary") or []),
                "input_release_ids": input_manifest["release_ids"],
            }
        )
    return {
        "schema_version": "deep03-d3-w2a-method-execution-receipt-v1",
        "run_id": input_manifest["run_id"],
        "declared_methods": list(METHODS),
        "attempted_methods": list(METHODS),
        "executed_methods": [
            method["method_id"] for method in methods if method["status"] == "EXECUTED"
        ],
        "not_estimable_methods": [
            method["method_id"]
            for method in methods
            if method["status"] == "NOT_ESTIMABLE"
        ],
        "all_declared_methods_closed": len(rows) == len(METHODS),
        "methods": rows,
    }


def _exclusion_receipt(input_manifest: dict[str, Any], methods: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "deep03-d3-w2a-exclusion-waterfall-v1",
        "run_id": input_manifest["run_id"],
        "rule": "every bounded, censored, invalid, missing or unsupported row remains visible",
        "methods": [
            {
                "method_id": method["method_id"],
                "status": method["status"],
                "waterfall": method["waterfall"],
            }
            for method in methods
        ],
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def _table(rows: list[dict[str, Any]], source: str) -> str:
    if not rows:
        return '<p class="empty">No estimable result rows.</p>'
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    head = "".join(f"<th>{html.escape(key)}</th>" for key in columns)
    body = []
    for row in rows:
        body.append(
            "<tr>"
            + "".join(
                f'<td title="{html.escape(source)}">{html.escape(_fmt(row.get(key)))}</td>'
                for key in columns
            )
            + "</tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + f'<p class="source">Source: {html.escape(source)}</p>'
    )


def _bar_chart(rows: list[dict[str, Any]], label_key: str, value_key: str) -> str:
    points = []
    for row in rows[:24]:
        value = row.get(value_key)
        if isinstance(value, (int, float)) and math_isfinite(float(value)):
            points.append((str(row.get(label_key, "")), float(value)))
    if not points:
        return '<div class="chart-empty">Chart unavailable: no finite estimable values.</div>'
    max_abs = max(abs(value) for _, value in points) or 1.0
    width = 860
    row_h = 26
    height = 38 + len(points) * row_h
    center = 430
    bars = [
        f'<line x1="{center}" y1="20" x2="{center}" y2="{height-8}" stroke="#889"/>'
    ]
    for index, (label, value) in enumerate(points):
        y = 28 + index * row_h
        scaled = 360 * abs(value) / max_abs
        x = center if value >= 0 else center - scaled
        color = "#2a9d8f" if value >= 0 else "#e76f51"
        bars.append(
            f'<text x="4" y="{y+12}" font-size="11">{html.escape(label[:45])}</text>'
            f'<rect x="{x:.2f}" y="{y}" width="{scaled:.2f}" height="16" fill="{color}"/>'
            f'<text x="{center+365}" y="{y+12}" font-size="11">{value:.6g}</text>'
        )
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="inline bar chart">{"".join(bars)}</svg>'
    )


def math_isfinite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _render_report(
    input_manifest: dict[str, Any],
    quality: dict[str, Any],
    methods: list[dict[str, Any]],
) -> str:
    source = (
        "INPUT_MANIFEST.json · releases="
        + ",".join(input_manifest["release_ids"])
        + " · exact VersionId objects="
        + str(input_manifest["object_count"])
    )
    index_rows = []
    sections = []
    chart_specs = {
        "D3-B01-MARKOUT": ("horizon_us", "mean_signed_markout_logodds"),
        "D3-B02-ONESIDE": ("book_state", "exposure_seconds"),
        "D3-B04-RHYTHM": ("utc_hour", "trades_per_active_market_minute"),
    }
    for method in methods:
        display_status = "TESTED" if method["status"] == "EXECUTED" else method["status"]
        index_rows.append(
            f'<tr class="method-row" data-status="{html.escape(display_status)}">'
            f'<td><a href="#{html.escape(method["method_id"])}">{html.escape(method["method_id"])}</a></td>'
            f'<td>{html.escape(method["title"])}</td><td><span class="status {display_status.lower()}">'
            f'{html.escape(display_status)}</span></td><td>{html.escape(str(method["reason"]))}</td></tr>'
        )
        provenance = (
            source
            + " · queries="
            + ",".join(query["query_id"] for query in method["queries"])
        )
        chart = ""
        if method["method_id"] in chart_specs:
            label, value = chart_specs[method["method_id"]]
            chart = _bar_chart(method.get("summary") or [], label, value)
        query_blocks = "".join(
            f'<details><summary>{html.escape(query["query_id"])}</summary>'
            f'<pre>{html.escape(query["sql"])}</pre></details>'
            for query in method["queries"]
        )
        sections.append(
            f'<section class="method method-row" data-status="{html.escape(display_status)}" '
            f'id="{html.escape(method["method_id"])}"><h2>{html.escape(method["method_id"])} · '
            f'{html.escape(method["title"])}</h2><p><span class="status {display_status.lower()}">'
            f'{html.escape(display_status)}</span></p><p>{html.escape(str(method["reason"]))}</p>'
            f'<h3>Chart</h3>{chart}<h3>Result table</h3>{_table(method.get("summary") or [], provenance)}'
            f'<h3>Inclusion → exclusion waterfall</h3>{_table(method["waterfall"], provenance)}'
            f'<h3>Exact queries</h3>{query_blocks}<h3>Falsification / limits</h3>'
            f'<p>Uncertainty: {html.escape(method["uncertainty"]["state"])} — '
            f'{html.escape(method["uncertainty"]["reason"])}</p>'
            '<p>No fee-after PnL, fill, causal attribution, confirmation, promotion, or live claim is made. '
            'A missing dependency produces NOT_ESTIMABLE rather than a synthetic result.</p></section>'
        )
    quality_rows = [
        {
            "release_id": release["release_id"],
            "date": release["date"],
            "evidence_tier": release["evidence_tier"],
            "tl1_status": release["tl1_status"],
            "downgrade_reasons": "; ".join(release["downgrade_reasons"]) or "none",
            "capture_gap_intervals": release["capture_gap_summary"].get("gap_intervals"),
            "capture_unparsed": release["capture_gap_summary"].get("unparsed"),
            "l2_parse_errors": release["l2_quality_summary"].get("parse_errors"),
            "l2_seq_gap_events": release["l2_quality_summary"].get("seq_gap_events"),
            "quality_objects": len(release["quality_objects"]),
        }
        for release in quality["releases"]
    ]
    object_rows = [
        {
            "release_id": obj["release_id"],
            "logical_key": obj["logical_key"],
            "source_version_id": obj["source_version_id"],
            "sha256": obj["sha256"],
            "size": obj["size"],
            "row_count": obj.get("row_count"),
            "row_count_state": obj.get("row_count_state"),
        }
        for obj in input_manifest["objects"]
    ]
    css = """
      :root{color-scheme:light;font-family:Inter,system-ui,sans-serif;color:#18202a;background:#f4f6f8}
      body{max-width:1180px;margin:auto;padding:24px}.banner{padding:12px;margin:8px 0;border:2px solid #b23a48;background:#fff0f1;font-weight:800}
      .meta,.method{background:#fff;border:1px solid #ccd3da;border-radius:10px;padding:18px;margin:18px 0;box-shadow:0 2px 8px #0000000d}
      table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #dce1e6;padding:7px;text-align:left;vertical-align:top}th{background:#eef2f5;position:sticky;top:0}
      .scroll{overflow:auto;max-height:520px}.status{display:inline-block;padding:3px 8px;border-radius:999px;font-weight:700;background:#dde6ed}.tested{background:#d8f3dc}.not_estimable{background:#ffe5b4}
      .source{font-family:ui-monospace,monospace;font-size:11px;color:#53606c}.chart{width:100%;max-height:560px;background:#fbfcfd;border:1px solid #e1e5e9}.chart-empty,.empty{padding:12px;background:#fff6dc;color:#6b4e00}
      pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101820;color:#e8f1f8;padding:12px;border-radius:6px}button{margin:4px;padding:6px 10px}.hidden{display:none}a{color:#145da0}
    """
    script = """
      function filterStatus(s){document.querySelectorAll('.method-row').forEach(function(e){e.classList.toggle('hidden',s!=='ALL'&&e.dataset.status!==s);});}
    """
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Deep03 D3-W2A V3 Open Discovery</title><style>{css}</style></head><body>
    <div class="banner">MODE 1 / EXPLORATORY_AUTORESEARCH</div><div class="banner">SEALED_PENDING_QUALITY_ASSESSMENT</div><div class="banner">EXPLORATORY_ONLY · NOT STRICT ACCEPTANCE · NO STRATEGY PNL · NO ORDER AUTHORITY</div>
    <h1>Deep03 D3-W2A V3 Open Discovery</h1><div class="meta"><p>Run <code>{html.escape(input_manifest['run_id'])}</code></p>
    <p>Exact explicit releases: {html.escape(', '.join(input_manifest['release_ids']))}</p>
    <p>Objects: {input_manifest['object_count']} · bytes: {input_manifest['object_bytes']} · sealed fact rows reported by source manifests: {input_manifest['sealed_fact_rows']} · fact objects without a source row count: {input_manifest['fact_objects_without_row_count']}</p>
    <p class="source">Source: {html.escape(source)}</p></div>
    <section class="meta"><h2>Data quality and coverage</h2>{_table(quality_rows, source)}
    <details><summary>Exact release / VersionId / object ledger</summary>{_table(object_rows, 'INPUT_MANIFEST.json exact object ledger')}</details></section>
    <section class="meta"><h2>Hypothesis / method index</h2><p><button onclick="filterStatus('ALL')">All</button><button onclick="filterStatus('TESTED')">TESTED</button><button onclick="filterStatus('NOT_ESTIMABLE')">NOT_ESTIMABLE</button></p>
    <table><thead><tr><th>Method</th><th>Question</th><th>Status</th><th>Reason</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table></section>
    {''.join(sections)}
    <section class="meta"><h2>Decision</h2><p><strong>DESCRIPTIVE_OPEN_DISCOVERY_COMPLETE.</strong> These results can propose follow-up hypotheses only. They cannot freeze, promote, shadow, trade, or claim profitability.</p></section>
    <script>{script}</script></body></html>"""


def _artifact_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir).as_posix()
        if rel in {"ARTIFACT_SHA256SUMS", "RUN_COMPLETE.json", "RUN_FAILED.json"}:
            continue
        if rel.startswith(".scratch/"):
            continue
        rows.append({"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size})
    return rows


def run_discovery(
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
    memory_limit: str = "8GB",
    threads: int = 4,
    expected_owner_uid: int = 0,
    authority_now: dt.datetime | None = None,
    claim_invocation_id: str | None = None,
    claim_proc_cgroup_path: Path | None = None,
) -> Path:
    refuse_credential_environment()
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
    unexpected = sorted(
        path.name for path in run_dir.iterdir() if path.name not in allowed
    )
    if unexpected:
        raise Deep03InputError(
            "prepared run contains stale/partial artifacts; use a new run_id: "
            + ",".join(unexpected)
        )
    input_manifest, prepare = ensure_run_inputs_current(
        run_dir, authority_context
    )
    lock_handle = (run_dir / "PREPARE_RECEIPT.json").open("rb")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Deep03InputError("another runner holds this prepared run") from exc
        started = utc_now()
        scratch = run_dir / ".scratch"
        import duckdb

        con = duckdb.connect()
        try:
            _configure_duckdb(con, scratch, memory_limit, threads)
            capabilities, methods = execute_all(con, input_manifest)
        finally:
            con.close()
        quality = _quality_receipt(input_manifest)
        estimability = _estimability_receipt(input_manifest, capabilities, methods)
        exclusions = _exclusion_receipt(input_manifest, methods)
        method_receipt = _method_receipt(input_manifest, methods)
        results = {
            "schema_version": SCHEMA_RESULTS,
            "run_id": input_manifest["run_id"],
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "research_stage": "OPEN_DISCOVERY",
            "work_package": "D3-W2A",
            "evidence_labels": list(LABELS),
            "release_ids": input_manifest["release_ids"],
            "conclusion_status": "DESCRIPTIVE_OPEN_DISCOVERY_COMPLETE",
            "candidate_or_profit_claim": False,
            "methods": methods,
        }
        atomic_write_json(run_dir / "DATA_QUALITY_RECEIPT.json", quality, exclusive=True)
        atomic_write_json(run_dir / "ESTIMABILITY_PREFLIGHT.json", estimability, exclusive=True)
        atomic_write_json(run_dir / "EXCLUSION_WATERFALL.json", exclusions, exclusive=True)
        atomic_write_json(run_dir / "METHOD_EXECUTION_RECEIPT.json", method_receipt, exclusive=True)
        atomic_write_json(run_dir / "RESULTS.json", results, exclusive=True)
        report = _render_report(input_manifest, quality, methods).encode("utf-8")
        atomic_write_bytes(run_dir / "REPORT" / "index.html", report, exclusive=True)
        reproduction = {
            "schema_version": "deep03-d3-w2a-reproduction-receipt-v1",
            "run_id": input_manifest["run_id"],
            "prepare_command_shape": (
                "deep03-v3-prepare --cache CACHE --run-root RUN_ROOT "
                "--run-id RUN_ID --authority AUTHORITY --arm-file ARM "
                "--arm-claim-root CLAIM_ROOT "
                "--plan PLAN --runtime-commit COMMIT --audit AUDIT "
                "--w0-release W0 --w1-release W1 --w1-complete W1_COMPLETE "
                "--release EXPLICIT_RELEASE_ID [...]"
            ),
            "run_command_shape": (
                "w09-run deep03-v3-run --run-dir RUN_DIR "
                "--authority AUTHORITY --arm-file ARM --plan PLAN "
                "--arm-claim-root CLAIM_ROOT "
                "--runtime-commit COMMIT --audit AUDIT --w0-release W0 "
                "--w1-release W1 --w1-complete W1_COMPLETE"
            ),
            "source_modules_sha256": source_hashes(),
            "duckdb_version": duckdb.__version__,
            "memory_limit": memory_limit,
            "threads": threads,
            "network_reads": 0,
            "rfq_reads": 0,
            "order_actions": 0,
        }
        atomic_write_json(run_dir / "REPRODUCTION_RECEIPT.json", reproduction, exclusive=True)
        if scratch.exists():
            shutil.rmtree(scratch)
        artifacts = _artifact_rows(run_dir)
        sums = "".join(f"{row['sha256']}  {row['path']}\n" for row in artifacts)
        atomic_write_bytes(run_dir / "ARTIFACT_SHA256SUMS", sums.encode("ascii"), exclusive=True)
        sums_sha = sha256_file(run_dir / "ARTIFACT_SHA256SUMS")
        complete = {
            "schema_version": SCHEMA_COMPLETE,
            "run_id": input_manifest["run_id"],
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "research_stage": "OPEN_DISCOVERY",
            "work_package": "D3-W2A",
            "evidence_labels": list(LABELS),
            "release_ids": input_manifest["release_ids"],
            "started_at_utc": started,
            "completed_at_utc": utc_now(),
            "exit_status": 0,
            "state": "RUN_COMPLETE",
            "declared_methods": list(METHODS),
            "executed_methods": method_receipt["executed_methods"],
            "not_estimable_methods": method_receipt["not_estimable_methods"],
            "all_declared_methods_closed": True,
            "input_manifest_sha256": prepare["input_manifest_sha256"],
            "results_sha256": sha256_file(run_dir / "RESULTS.json"),
            "method_receipt_sha256": sha256_file(
                run_dir / "METHOD_EXECUTION_RECEIPT.json"
            ),
            "report_sha256": sha256_file(run_dir / "REPORT" / "index.html"),
            "artifact_sha256sums_sha256": sums_sha,
            "artifacts": artifacts,
            "source_modules_sha256": source_hashes(),
            "host": {
                "hostname": socket.gethostname(),
                "system": platform.system(),
                "machine": platform.machine(),
            },
            "rfq_reads": 0,
            "network_reads": 0,
            "production_mutations": 0,
            "order_actions": 0,
            "candidate_or_profit_claim": False,
            "authority_binding": authority_context["binding"],
            "authority_artifact_sha256s": authority_context[
                "artifact_sha256s"
            ],
        }
        # This is intentionally the final filesystem write inside the bundle.
        atomic_write_json(run_dir / "RUN_COMPLETE.json", complete, exclusive=True)
        return run_dir / "RUN_COMPLETE.json"
    finally:
        lock_handle.close()


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--threads", default=4, type=int)
    args = parser.parse_args(argv)
    try:
        complete = run_discovery(
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
            memory_limit=args.memory_limit,
            threads=args.threads,
        )
    except Exception as exc:
        # A failed attempt gets an explicit non-completion receipt.  It is not
        # retryable in place; the operator prepares a fresh run_id.
        try:
            if args.run_dir.is_dir() and not (args.run_dir / "RUN_COMPLETE.json").exists():
                atomic_write_json(
                    args.run_dir / "RUN_FAILED.json",
                    {
                        "schema_version": "deep03-d3-w2a-run-failed-v1",
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
        print(f"D3_W2A_RUN_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"D3_W2A_RUN_PASS receipt={complete}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
