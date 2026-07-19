#!/usr/bin/env python3
"""Plan one immutable Research Inbox job without starting research.

The worker is suitable for ``w09-research-inbox-worker@.service``.  It reads
``/srv/w09-research/inbox/jobs/<job_id>`` by default, verifies that PLAN.md and
JOB_SPEC.json still describe the same exact bytes, snapshots the local V3 data
catalog, resolves exact releases, and atomically publishes planning artifacts.

This stage has no subprocess, shell, Markdown execution, network writer, or
research runner.  A READY result is only a structured request for a separately
installed authority/arm and the existing Deep03 systemd one-shot gate.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import threading
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from research.data_catalog import build_catalog, canonical_sha256  # noqa: E402
from research.data_resolver import resolve_data  # noqa: E402
from research.inbox import JOB_ID_RE, get_job, update_status  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402
from research.plugin_api import (  # noqa: E402
    REGISTRY_VERSION,
    PluginError,
    build_execution_request,
    load_registered_plugin,
    registration_for,
)


DEFAULT_INBOX_ROOT = Path("/srv/w09-research/inbox")
DEFAULT_CACHE_ROOT = Path("/srv/w09-research/cache")
PLANNING_SCHEMA = "research-job-planning-receipt-v1"
OUTPUT_SCHEMA = "research-job-output-receipt-v1"
RESULTS_SCHEMA = "research-results-v1"
REPORT_RECEIPT_SCHEMA = "research-report-receipt-v1"
DEEP03_RESULTS_SCHEMA = "deep03-d3-w2a-v3-results-v1"
DEEP03_COMPLETE_SCHEMA = "deep03-d3-w2a-v3-run-complete-v1"
DEEP03_ADAPTER_SCHEMA = "deep03-generic-job-output-adapter-v1"
PROVENANCE_SCHEMA = "research-authorized-output-provenance-v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_CONTROL_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_FILES = 256
MAX_REPORT_DEPTH = 8
TERMINAL_PLANNING_STATES = {"READY", "BLOCKED", "REFUSED"}
REPORT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEEP03_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
DEEP03_RECEIPTS = (
    "PREPARE_RECEIPT.json",
    "DATA_QUALITY_RECEIPT.json",
    "ESTIMABILITY_PREFLIGHT.json",
    "EXCLUSION_WATERFALL.json",
    "METHOD_EXECUTION_RECEIPT.json",
    "REPRODUCTION_RECEIPT.json",
)
DEEP03_AUTHORITY_ARTIFACTS = (
    "ADOPTED_PLAN.md",
    "AUDIT.md",
    "AUTHORITY.json",
    "BASE_COMMIT.txt",
    "D3_W0_RELEASE.json",
    "D3_W1_RELEASE.json",
    "EXECUTION_ARM.json",
    "W1_COMPLETE.json",
)

_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_AWS_ACCESS_KEY_RE = re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(rb"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/-]{12,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"(?i)(?:aws_secret_access_key|aws_session_token|secret_access_key|"
    rb"api[_-]?key|access[_-]?token|session[_-]?token|password|passwd)"
    rb"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+_.~=-]{16,})"
)
_OPENAI_KEY_RE = re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")


class ResearchJobWorkerError(RuntimeError):
    """A job could not safely enter or complete the planning stage."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ResearchJobWorkerError("%s is missing or unsafe" % label) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResearchJobWorkerError("%s is not a regular file" % label)
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise ResearchJobWorkerError("%s size is invalid" % label)
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise ResearchJobWorkerError("%s changed while being read" % label)
        return raw
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError) as exc:
        raise ResearchJobWorkerError("%s is not valid JSON" % label) from exc
    if not isinstance(value, dict):
        raise ResearchJobWorkerError("%s root is not an object" % label)
    return value


def _artifact_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResearchJobWorkerError("planning artifact is not canonical JSON") from exc


def _inbox_spec_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _credential_like(payload: bytes) -> bool:
    return any(
        pattern.search(payload)
        for pattern in (
            _PRIVATE_KEY_RE,
            _AWS_ACCESS_KEY_RE,
            _BEARER_RE,
            _SECRET_ASSIGNMENT_RE,
            _OPENAI_KEY_RE,
        )
    )


def _safe_report_path(report_root: Path, path: Path) -> PurePosixPath:
    if path.is_symlink() or not path.is_file():
        raise ResearchJobWorkerError("REPORT contains a link or special file")
    relative = path.relative_to(report_root)
    if (
        not relative.parts
        or len(relative.parts) + 1 > MAX_REPORT_DEPTH
        or any(REPORT_COMPONENT_RE.fullmatch(part) is None for part in relative.parts)
    ):
        raise ResearchJobWorkerError("REPORT contains an unsafe path")
    return PurePosixPath("REPORT", *relative.parts)


def _collect_output_artifacts(
    output_root: Path, job_id: str, *, published: bool
) -> dict[PurePosixPath, bytes]:
    """Read one bounded result/report tree without following links."""
    output_root = Path(output_root)
    if output_root.is_symlink() or not output_root.is_dir():
        raise ResearchJobWorkerError("output source is missing or unsafe")
    entries = list(output_root.iterdir())
    expected = {"RESULTS.json", "REPORT"}
    if published:
        expected.add("OUTPUT_RECEIPT.json")
    if {path.name for path in entries} != expected:
        raise ResearchJobWorkerError(
            "output source must contain only RESULTS.json and REPORT"
            + (" plus OUTPUT_RECEIPT.json" if published else "")
        )

    results_raw = _read_regular(
        output_root / "RESULTS.json",
        label="RESULTS.json",
        max_bytes=MAX_OUTPUT_FILE_BYTES,
    )
    results = _json(results_raw, "RESULTS.json")
    if results.get("schema_version") != RESULTS_SCHEMA or results.get("job_id") != job_id:
        raise ResearchJobWorkerError("RESULTS.json does not bind this job")

    report_root = output_root / "REPORT"
    if report_root.is_symlink() or not report_root.is_dir():
        raise ResearchJobWorkerError("REPORT is missing or unsafe")
    artifacts: dict[PurePosixPath, bytes] = {
        PurePosixPath("RESULTS.json"): results_raw,
    }
    for path in sorted(report_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = _safe_report_path(report_root, path)
        artifacts[relative] = _read_regular(
            path, label=str(relative), max_bytes=MAX_OUTPUT_FILE_BYTES
        )
    index_path = PurePosixPath("REPORT/index.html")
    report_receipt_path = PurePosixPath("REPORT/REPORT_RECEIPT.json")
    if index_path not in artifacts or report_receipt_path not in artifacts:
        raise ResearchJobWorkerError("output source lacks its report contract")
    # STATUS.json and OUTPUT_RECEIPT.json consume the other two export slots.
    if len(artifacts) + 2 > MAX_OUTPUT_FILES:
        raise ResearchJobWorkerError("output source contains too many files")
    if sum(len(payload) for payload in artifacts.values()) > MAX_OUTPUT_BYTES:
        raise ResearchJobWorkerError("output source exceeds 64 MiB")
    for path, payload in artifacts.items():
        if path.suffix.lower() in {".json", ".html", ".css", ".csv", ".txt", ".md", ".svg"}:
            if _credential_like(payload):
                raise ResearchJobWorkerError("output source contains credential-like content")

    report_receipt = _json(artifacts[report_receipt_path], "REPORT_RECEIPT.json")
    if (
        report_receipt.get("schema_version") != REPORT_RECEIPT_SCHEMA
        or report_receipt.get("job_id") != job_id
        or report_receipt.get("results_sha256") != _sha(results_raw)
        or report_receipt.get("report_sha256") != _sha(artifacts[index_path])
    ):
        raise ResearchJobWorkerError("report receipt does not bind results/report bytes")
    return artifacts


def _output_receipt(
    job_id: str, artifacts: dict[PurePosixPath, bytes]
) -> dict[str, Any]:
    rows = [
        {"path": str(path), "bytes": len(payload), "sha256": _sha(payload)}
        for path, payload in sorted(artifacts.items(), key=lambda item: str(item[0]))
    ]
    receipt: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA,
        "job_id": job_id,
        "output_directory": "OUTPUT",
        "artifact_count": len(rows),
        "artifact_bytes": sum(row["bytes"] for row in rows),
        "artifacts": rows,
        "data_files_exported": 0,
        "source_contract": "DEDICATED_JOB_OUTPUT_ONLY",
    }
    receipt["output_sha256"] = _sha(_artifact_bytes(receipt))
    return receipt


def _validate_published_output(job_dir: Path, job_id: str) -> dict[str, Any]:
    output = job_dir / "OUTPUT"
    artifacts = _collect_output_artifacts(output, job_id, published=True)
    raw = _read_regular(
        output / "OUTPUT_RECEIPT.json",
        label="OUTPUT_RECEIPT.json",
        max_bytes=MAX_CONTROL_BYTES,
    )
    receipt = _json(raw, "OUTPUT_RECEIPT.json")
    expected = _output_receipt(job_id, artifacts)
    if receipt != expected or raw != _artifact_bytes(receipt):
        raise ResearchJobWorkerError("published output receipt or artifact hashes differ")
    return receipt


def _planning_provenance_bindings(inputs: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_existing_bundle(inputs)
    if validated.get("state") != "READY":
        raise ResearchJobWorkerError("output requires a READY immutable planning bundle")
    planning = inputs["job_dir"] / "PLANNING"
    receipt_raw = _read_regular(
        planning / "PLANNING_RECEIPT.json",
        label="PLANNING_RECEIPT.json",
        max_bytes=MAX_CONTROL_BYTES,
    )
    execution_raw = _read_regular(
        planning / "EXECUTION_REQUEST.json",
        label="EXECUTION_REQUEST.json",
        max_bytes=MAX_CONTROL_BYTES,
    )
    receipt = _json(receipt_raw, "PLANNING_RECEIPT.json")
    execution = _json(execution_raw, "EXECUTION_REQUEST.json")
    return {
        "job_id": inputs["job"]["job_id"],
        "plugin_id": inputs["spec"]["plugin_id"],
        "plan_sha256": inputs["plan_sha256"],
        "job_spec_sha256": inputs["job_spec_sha256"],
        "catalog_sha256": execution["catalog_sha256"],
        "selection_sha256": execution["selection_sha256"],
        "preflight_sha256": execution["preflight_sha256"],
        "plugin_source_sha256": execution["plugin_source_sha256"],
        "planning_receipt_sha256": _sha(receipt_raw),
        "execution_request_artifact_sha256": _sha(execution_raw),
        "execution_request_digest": execution["execution_request_sha256"],
        "release_ids": execution["plugin_payload"]["input_binding"]["release_ids"],
        "authority_schema": execution["plugin_payload"]["downstream_gate_contract"][
            "authority_schema"
        ],
        "arm_schema": execution["plugin_payload"]["downstream_gate_contract"][
            "arm_schema"
        ],
        "planning_receipt": receipt,
        "execution_request": execution,
    }


def _validate_execution_provenance(
    inputs: dict[str, Any], provenance: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise ResearchJobWorkerError("authorized output provenance is missing")
    bindings = _planning_provenance_bindings(inputs)
    expected = {
        key: bindings[key]
        for key in (
            "job_id",
            "plugin_id",
            "plan_sha256",
            "job_spec_sha256",
            "catalog_sha256",
            "selection_sha256",
            "preflight_sha256",
            "plugin_source_sha256",
            "planning_receipt_sha256",
            "execution_request_artifact_sha256",
            "execution_request_digest",
            "authority_schema",
            "arm_schema",
        )
    }
    expected["schema_version"] = PROVENANCE_SCHEMA
    for field, value in expected.items():
        if provenance.get(field) != value:
            raise ResearchJobWorkerError(
                "authorized output provenance differs on %s" % field
            )
    for field in (
        "authority_sha256",
        "arm_sha256",
        "adopted_plan_sha256",
        "source_run_complete_sha256",
        "source_results_sha256",
        "source_report_sha256",
    ):
        value = provenance.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ResearchJobWorkerError("authorized output provenance lacks %s" % field)
    if provenance["adopted_plan_sha256"] != inputs["plan_sha256"]:
        raise ResearchJobWorkerError("authorized adopted plan differs from exact job plan")
    run_id = provenance.get("source_run_id")
    if not isinstance(run_id, str) or DEEP03_RUN_ID_RE.fullmatch(run_id) is None:
        raise ResearchJobWorkerError("authorized output provenance run ID is invalid")
    digest = provenance.get("provenance_sha256")
    unsigned = {key: value for key, value in provenance.items() if key != "provenance_sha256"}
    if not isinstance(digest, str) or digest != _sha(_artifact_bytes(unsigned)):
        raise ResearchJobWorkerError("authorized output provenance digest mismatch")
    return bindings


def publish_job_output(
    *,
    inbox_root: Path = DEFAULT_INBOX_ROOT,
    job_id: str,
    source_root: Path,
    execution_provenance: dict[str, Any],
) -> dict[str, Any]:
    """Atomically publish already-authorized results under this job's ``OUTPUT``.

    This is intentionally not exposed by the planning-worker CLI.  A separate
    authority/arm-bound research runner must first advance the job to RUNNING
    and produce the generic result/report source.  This helper only validates,
    bounds and seals those output bytes; it cannot start research.
    """
    inputs = _job_inputs(Path(inbox_root), job_id)
    with _job_lock(inputs["job_dir"]):
        inputs = _job_inputs(Path(inbox_root), job_id)
        status = inputs["job"]["status"]
        if (
            status.get("state") != "RUNNING"
            or status.get("research_execution_started") is not True
        ):
            raise ResearchJobWorkerError(
                "output publication requires an authority-started RUNNING job"
            )
        _validate_execution_provenance(inputs, execution_provenance)
        artifacts = _collect_output_artifacts(Path(source_root), job_id, published=False)
        generic_results = _json(artifacts[PurePosixPath("RESULTS.json")], "RESULTS.json")
        if generic_results.get("execution_provenance") != execution_provenance:
            raise ResearchJobWorkerError(
                "RESULTS.json does not contain the validated execution provenance"
            )
        receipt = _output_receipt(job_id, artifacts)
        final = inputs["job_dir"] / "OUTPUT"
        if final.exists() or final.is_symlink():
            existing = _validate_published_output(inputs["job_dir"], job_id)
            if existing.get("output_sha256") != receipt["output_sha256"]:
                raise ResearchJobWorkerError("published output conflicts with retry bytes")
            return {
                "schema_version": OUTPUT_SCHEMA,
                "state": "OUTPUT_PUBLISHED",
                "job_id": job_id,
                "output_sha256": receipt["output_sha256"],
                "output_receipt_sha256": _sha(_artifact_bytes(existing)),
                "artifact_count": existing["artifact_count"],
                "artifact_bytes": existing["artifact_bytes"],
                "research_started_by_publisher": False,
                "idempotent_replay": True,
            }

        stage = inputs["job_dir"] / (
            ".OUTPUT.preparing.%d.%d" % (os.getpid(), threading.get_ident())
        )
        try:
            stage.mkdir(mode=0o750)
            for relative, payload in sorted(
                artifacts.items(), key=lambda item: str(item[0])
            ):
                destination = stage.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                _write_file(destination, payload)
            receipt_raw = _artifact_bytes(receipt)
            _write_file(stage / "OUTPUT_RECEIPT.json", receipt_raw)
            directories = sorted(
                (path for path in stage.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                directory.chmod(0o550)
            stage_fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            os.rename(stage, final)
            final.chmod(0o550)
            parent_fd = os.open(inputs["job_dir"], os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
        validated = _validate_published_output(inputs["job_dir"], job_id)
        return {
            "schema_version": OUTPUT_SCHEMA,
            "state": "OUTPUT_PUBLISHED",
            "job_id": job_id,
            "output_sha256": validated["output_sha256"],
            "output_receipt_sha256": _sha(_artifact_bytes(validated)),
            "artifact_count": validated["artifact_count"],
            "artifact_bytes": validated["artifact_bytes"],
            "research_started_by_publisher": False,
            "idempotent_replay": False,
        }


def _deep03_artifact_map(complete: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = complete.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise ResearchJobWorkerError("Deep03 RUN_COMPLETE artifact map is missing")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}:
            raise ResearchJobWorkerError("Deep03 artifact map entry is invalid")
        path = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or path in result
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise ResearchJobWorkerError("Deep03 artifact map entry is unsafe")
        result[path] = row
    return result


def _deep03_file(
    run_dir: Path,
    relative: str,
    artifact_map: dict[str, dict[str, Any]],
    *,
    limit: int = MAX_OUTPUT_FILE_BYTES,
) -> bytes:
    row = artifact_map.get(relative)
    if row is None:
        raise ResearchJobWorkerError("Deep03 artifact map lacks %s" % relative)
    raw = _read_regular(run_dir / relative, label="Deep03 %s" % relative, max_bytes=limit)
    if len(raw) != row["size"] or _sha(raw) != row["sha256"]:
        raise ResearchJobWorkerError("Deep03 artifact differs from RUN_COMPLETE: %s" % relative)
    return raw


def _deep03_adapter_inputs(
    inputs: dict[str, Any], run_dir: Path
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    bindings = _planning_provenance_bindings(inputs)
    execution = bindings["execution_request"]
    payload = execution.get("plugin_payload")
    if (
        inputs["spec"].get("plugin_id") != "deep03"
        or not isinstance(payload, dict)
        or payload.get("schema_version") != "deep03-structured-execution-request-v1"
        or payload.get("state") != "AWAITING_EXISTING_AUTHORITY_AND_ONESHOT_GATE"
        or payload.get("mode") != "MODE 1 / EXPLORATORY_AUTORESEARCH"
        or payload.get("phase") != "OPEN_DISCOVERY"
        or payload.get("work_package") != "D3-W2A"
    ):
        raise ResearchJobWorkerError("job is not a bound Deep03 execution request")

    run_dir = Path(run_dir)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ResearchJobWorkerError("Deep03 run directory is missing or unsafe")
    complete_raw = _read_regular(
        run_dir / "RUN_COMPLETE.json",
        label="Deep03 RUN_COMPLETE.json",
        max_bytes=MAX_CONTROL_BYTES,
    )
    complete = _json(complete_raw, "Deep03 RUN_COMPLETE.json")
    run_id = complete.get("run_id")
    releases = payload["input_binding"].get("release_ids")
    if (
        complete.get("schema_version") != DEEP03_COMPLETE_SCHEMA
        or complete.get("state") != "RUN_COMPLETE"
        or complete.get("exit_status") != 0
        or not isinstance(run_id, str)
        or DEEP03_RUN_ID_RE.fullmatch(run_id) is None
        or run_dir.name != run_id
        or complete.get("mode") != payload["mode"]
        or complete.get("research_stage") != payload["phase"]
        or complete.get("work_package") != payload["work_package"]
        or complete.get("release_ids") != releases
        or complete.get("strict_acceptance_claimed") is not False
        or complete.get("candidate_or_profit_claim") is not False
        or any(complete.get(field) != 0 for field in ("rfq_reads", "network_reads", "production_mutations", "order_actions"))
    ):
        raise ResearchJobWorkerError("Deep03 RUN_COMPLETE differs from the planned safe run")
    if complete_raw != _artifact_bytes(complete):
        raise ResearchJobWorkerError("Deep03 RUN_COMPLETE is not canonical JSON")

    artifact_map = _deep03_artifact_map(complete)
    deep_results_raw = _deep03_file(run_dir, "RESULTS.json", artifact_map)
    report_raw = _deep03_file(run_dir, "REPORT/index.html", artifact_map)
    input_raw = _deep03_file(run_dir, "INPUT_MANIFEST.json", artifact_map)
    deep_results = _json(deep_results_raw, "Deep03 RESULTS.json")
    input_manifest = _json(input_raw, "Deep03 INPUT_MANIFEST.json")
    authority_binding = complete.get("authority_binding")
    authority_hashes = complete.get("authority_artifact_sha256s")
    if (
        deep_results.get("schema_version") != DEEP03_RESULTS_SCHEMA
        or deep_results.get("run_id") != run_id
        or deep_results.get("mode") != payload["mode"]
        or deep_results.get("research_stage") != payload["phase"]
        or deep_results.get("work_package") != payload["work_package"]
        or deep_results.get("release_ids") != releases
        or deep_results.get("strict_acceptance_claimed") is not False
        or deep_results.get("candidate_or_profit_claim") is not False
        or input_manifest.get("run_id") != run_id
        or input_manifest.get("release_ids") != releases
        or input_manifest.get("authority_binding") != authority_binding
        or input_manifest.get("authority_artifact_sha256s") != authority_hashes
        or complete.get("results_sha256") != _sha(deep_results_raw)
        or complete.get("report_sha256") != _sha(report_raw)
        or complete.get("input_manifest_sha256") != _sha(input_raw)
    ):
        raise ResearchJobWorkerError("Deep03 result/input/report provenance mismatch")
    if not isinstance(authority_binding, dict) or not isinstance(authority_hashes, dict):
        raise ResearchJobWorkerError("Deep03 authority provenance is missing")
    if (
        authority_binding.get("authorized_input_release_ids") != releases
        or authority_binding.get("authorized_phase_id") != payload["phase"]
        or authority_binding.get("authorized_work_package_id") != payload["work_package"]
        or authority_binding.get("adopted_plan_sha256") != inputs["plan_sha256"]
        or set(authority_hashes) != set(DEEP03_AUTHORITY_ARTIFACTS)
    ):
        raise ResearchJobWorkerError("Deep03 authority does not bind this exact job")
    for name in DEEP03_AUTHORITY_ARTIFACTS:
        raw = _read_regular(
            run_dir / name,
            label="Deep03 authority artifact %s" % name,
            max_bytes=MAX_CONTROL_BYTES,
        )
        if _sha(raw) != authority_hashes.get(name):
            raise ResearchJobWorkerError("Deep03 authority artifact hash mismatch: %s" % name)
    authority_binding_raw = _deep03_file(run_dir, "AUTHORITY_BINDING.json", artifact_map)
    authority_binding_file = _json(authority_binding_raw, "Deep03 AUTHORITY_BINDING.json")
    if authority_binding_file != {
        "schema_version": "deep03-d3-w2a-authority-binding-v1",
        "binding": authority_binding,
        "artifact_sha256s": authority_hashes,
    }:
        raise ResearchJobWorkerError("Deep03 AUTHORITY_BINDING differs from RUN_COMPLETE")
    authority = _json(
        _read_regular(
            run_dir / "AUTHORITY.json",
            label="Deep03 AUTHORITY.json",
            max_bytes=MAX_CONTROL_BYTES,
        ),
        "Deep03 AUTHORITY.json",
    )
    arm = _json(
        _read_regular(
            run_dir / "EXECUTION_ARM.json",
            label="Deep03 EXECUTION_ARM.json",
            max_bytes=MAX_CONTROL_BYTES,
        ),
        "Deep03 EXECUTION_ARM.json",
    )
    authority_sha = authority_hashes["AUTHORITY.json"]
    arm_sha = authority_hashes["EXECUTION_ARM.json"]
    if (
        authority.get("schema_version") != bindings["authority_schema"]
        or arm.get("schema_version") != bindings["arm_schema"]
        or authority_binding.get("authority_sha256") != authority_sha
        or authority_binding.get("arm_sha256") != arm_sha
        or arm.get("authority_sha256") != authority_sha
        or _sha(
            _read_regular(
                run_dir / "ADOPTED_PLAN.md",
                label="Deep03 ADOPTED_PLAN.md",
                max_bytes=MAX_PLAN_BYTES,
            )
        )
        != inputs["plan_sha256"]
    ):
        raise ResearchJobWorkerError("Deep03 authority/arm/adopted-plan binding mismatch")

    sums_raw = _read_regular(
        run_dir / "ARTIFACT_SHA256SUMS",
        label="Deep03 ARTIFACT_SHA256SUMS",
        max_bytes=MAX_OUTPUT_FILE_BYTES,
    )
    if complete.get("artifact_sha256sums_sha256") != _sha(sums_raw):
        raise ResearchJobWorkerError("Deep03 artifact checksum ledger hash mismatch")
    try:
        sum_lines = sums_raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise ResearchJobWorkerError("Deep03 artifact checksum ledger is not ASCII") from exc
    sums: dict[str, str] = {}
    for line in sum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise ResearchJobWorkerError("Deep03 artifact checksum ledger is invalid")
        if parts[1] in sums:
            raise ResearchJobWorkerError("Deep03 artifact checksum ledger has duplicates")
        sums[parts[1]] = parts[0]
    for name in ("RESULTS.json", "REPORT/index.html", "INPUT_MANIFEST.json", *DEEP03_RECEIPTS):
        if sums.get(name) != artifact_map.get(name, {}).get("sha256"):
            raise ResearchJobWorkerError("Deep03 checksum ledger differs on %s" % name)

    preserved: dict[str, bytes] = {}
    for name in DEEP03_RECEIPTS:
        preserved[name] = _deep03_file(run_dir, name, artifact_map)
    preserved["RUN_COMPLETE.json"] = complete_raw
    preserved["ARTIFACT_SHA256SUMS.txt"] = sums_raw
    provenance: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA,
        "job_id": inputs["job"]["job_id"],
        "plugin_id": "deep03",
        "plan_sha256": inputs["plan_sha256"],
        "job_spec_sha256": inputs["job_spec_sha256"],
        "catalog_sha256": bindings["catalog_sha256"],
        "selection_sha256": bindings["selection_sha256"],
        "preflight_sha256": bindings["preflight_sha256"],
        "plugin_source_sha256": bindings["plugin_source_sha256"],
        "planning_receipt_sha256": bindings["planning_receipt_sha256"],
        "execution_request_artifact_sha256": bindings[
            "execution_request_artifact_sha256"
        ],
        "execution_request_digest": bindings["execution_request_digest"],
        "authority_schema": bindings["authority_schema"],
        "arm_schema": bindings["arm_schema"],
        "authority_sha256": authority_sha,
        "arm_sha256": arm_sha,
        "adopted_plan_sha256": inputs["plan_sha256"],
        "source_run_id": run_id,
        "source_run_complete_sha256": _sha(complete_raw),
        "source_results_sha256": _sha(deep_results_raw),
        "source_report_sha256": _sha(report_raw),
    }
    provenance["provenance_sha256"] = _sha(_artifact_bytes(provenance))
    return provenance, {
        "deep_results": deep_results_raw,
        "report": report_raw,
        **preserved,
    }, bindings


def finalize_deep03_job_output(
    *, inbox_root: Path = DEFAULT_INBOX_ROOT, job_id: str, run_dir: Path
) -> dict[str, Any]:
    """Adapt one authority-complete Deep03 run, publish it, then mark COMPLETE."""
    inputs = _job_inputs(Path(inbox_root), job_id)
    status = inputs["job"]["status"]
    if status.get("state") == "COMPLETE":
        receipt = _validate_published_output(inputs["job_dir"], job_id)
        return {
            "schema_version": DEEP03_ADAPTER_SCHEMA,
            "state": "COMPLETE",
            "job_id": job_id,
            "output_sha256": receipt["output_sha256"],
            "idempotent_replay": True,
        }
    if status.get("state") != "RUNNING" or status.get("research_execution_started") is not True:
        raise ResearchJobWorkerError("Deep03 finalizer requires a gate-started RUNNING job")
    provenance, source, _bindings = _deep03_adapter_inputs(inputs, Path(run_dir))
    deep_results = _json(source["deep_results"], "Deep03 RESULTS.json")
    generic_results = {
        "schema_version": RESULTS_SCHEMA,
        "job_id": job_id,
        "title": inputs["spec"].get("title") or "Deep03 research result",
        "state": "COMPLETE",
        "execution_class": "READONLY_EXPLORATORY",
        "summary": [
            "Authority-bound Deep03 OPEN_DISCOVERY run completed.",
            "The original Deep03 result object and report are preserved below.",
        ],
        "metrics": [],
        "tables": [],
        "limitations": [
            "Exploratory only; not strict acceptance and no trading authority.",
        ],
        "receipts": {
            "provenance_sha256": provenance["provenance_sha256"],
            "deep03_run_complete_sha256": provenance["source_run_complete_sha256"],
            "deep03_results_sha256": provenance["source_results_sha256"],
            "deep03_report_sha256": provenance["source_report_sha256"],
        },
        "execution_provenance": provenance,
        "source_schema_version": DEEP03_RESULTS_SCHEMA,
        "source_results": deep_results,
    }
    results_raw = _artifact_bytes(generic_results)
    report_receipt = {
        "schema_version": REPORT_RECEIPT_SCHEMA,
        "job_id": job_id,
        "results_sha256": _sha(results_raw),
        "report_sha256": _sha(source["report"]),
    }
    stage = inputs["job_dir"] / (
        ".DEEP03-ADAPTER.preparing.%d.%d" % (os.getpid(), threading.get_ident())
    )
    try:
        stage.mkdir(mode=0o750)
        (stage / "REPORT" / "source_receipts").mkdir(parents=True, mode=0o750)
        _write_file(stage / "RESULTS.json", results_raw)
        _write_file(stage / "REPORT" / "index.html", source["report"])
        _write_file(
            stage / "REPORT" / "REPORT_RECEIPT.json", _artifact_bytes(report_receipt)
        )
        for name in (*DEEP03_RECEIPTS, "RUN_COMPLETE.json", "ARTIFACT_SHA256SUMS.txt"):
            _write_file(stage / "REPORT" / "source_receipts" / name, source[name])
        published = publish_job_output(
            inbox_root=Path(inbox_root),
            job_id=job_id,
            source_root=stage,
            execution_provenance=provenance,
        )
    finally:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
    with _job_lock(inputs["job_dir"]):
        _validate_published_output(inputs["job_dir"], job_id)
        current = get_job(Path(inbox_root), job_id)["status"]["state"]
        if current == "RUNNING":
            update_status(
                Path(inbox_root),
                job_id,
                state="COMPLETE",
                message="Authority-bound Deep03 output was sealed and returned.",
                details={
                    "output_sha256": published["output_sha256"],
                    "provenance_sha256": provenance["provenance_sha256"],
                    "source_run_complete_sha256": provenance[
                        "source_run_complete_sha256"
                    ],
                },
            )
        elif current != "COMPLETE":
            raise ResearchJobWorkerError("job status changed during Deep03 finalization")
    return {
        "schema_version": DEEP03_ADAPTER_SCHEMA,
        "state": "COMPLETE",
        "job_id": job_id,
        "output_sha256": published["output_sha256"],
        "provenance_sha256": provenance["provenance_sha256"],
        "source_run_complete_sha256": provenance["source_run_complete_sha256"],
        "idempotent_replay": published["idempotent_replay"],
        "research_started_by_finalizer": False,
    }


def _job_inputs(inbox_root: Path, job_id: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise ResearchJobWorkerError("invalid job id")
    root = Path(inbox_root).expanduser().resolve()
    job = get_job(root, job_id)
    job_dir = root / "jobs" / job_id
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise ResearchJobWorkerError("job directory is missing or unsafe")
    plan_raw = _read_regular(job_dir / "PLAN.md", label="PLAN.md", max_bytes=MAX_PLAN_BYTES)
    spec_raw = _read_regular(
        job_dir / "JOB_SPEC.json", label="JOB_SPEC.json", max_bytes=MAX_CONTROL_BYTES
    )
    request_raw = _read_regular(
        job_dir / "REQUEST.json", label="REQUEST.json", max_bytes=MAX_CONTROL_BYTES
    )
    spec = _json(spec_raw, "JOB_SPEC.json")
    request = _json(request_raw, "REQUEST.json")
    try:
        plan_text = plan_raw.decode("utf-8")
    except UnicodeError as exc:
        raise ResearchJobWorkerError("PLAN.md is not UTF-8") from exc

    if spec_raw != _inbox_spec_bytes(spec):
        raise ResearchJobWorkerError("JOB_SPEC.json is not the immutable canonical artifact")
    plan_sha = _sha(plan_raw)
    expected = {
        "job_id": job_id,
        "plan_sha256": plan_sha,
        "plan_bytes": len(plan_raw),
        "execution_target": "W09_ISOLATED_RESEARCH",
        "data_access": "ZERO_COPY_EXACT_VERSION_READONLY",
        "arbitrary_plan_code_execution": False,
    }
    for field, value in expected.items():
        if request.get(field) != value:
            raise ResearchJobWorkerError("REQUEST.json differs on %s" % field)
    if not isinstance(request.get("automatic_execution_requested"), bool):
        raise ResearchJobWorkerError("REQUEST.json automatic execution flag is invalid")
    if spec.get("plan_sha256") != plan_sha or spec.get("plan_bytes") != len(plan_raw):
        raise ResearchJobWorkerError("JOB_SPEC.json does not bind exact PLAN.md bytes")
    try:
        recompiled = compile_plan(plan_text, request.get("source_filename", "PLAN.md"))
    except Exception as exc:
        raise ResearchJobWorkerError("PLAN.md no longer compiles under the safe contract") from exc
    if recompiled != spec:
        raise ResearchJobWorkerError("JOB_SPEC.json differs from exact PLAN.md compilation")
    if job["request"] != request or job["spec"] != spec:
        raise ResearchJobWorkerError("job controls changed during worker read")
    return {
        "root": root,
        "job_dir": job_dir,
        "job": job,
        "plan_sha256": plan_sha,
        "job_spec_sha256": _sha(spec_raw),
        "spec": spec,
        "request": request,
    }


@contextmanager
def _job_lock(job_dir: Path) -> Iterator[None]:
    path = job_dir / ".PLANNING.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o640)
    except OSError as exc:
        raise ResearchJobWorkerError("planning lock is unsafe") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_file(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o440)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _publish_bundle(job_dir: Path, artifacts: dict[str, dict[str, Any]]) -> Path:
    final = job_dir / "PLANNING"
    if final.exists() or final.is_symlink():
        raise ResearchJobWorkerError("planning artifact directory already exists")
    stage = job_dir / (
        ".PLANNING.preparing.%d.%d" % (os.getpid(), threading.get_ident())
    )
    try:
        stage.mkdir(mode=0o750)
    except OSError as exc:
        raise ResearchJobWorkerError("cannot create planning staging directory") from exc
    try:
        for name, value in artifacts.items():
            _write_file(stage / name, _artifact_bytes(value))
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(stage, final)
        os.chmod(final, 0o550)
        parent_fd = os.open(job_dir, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return final


def _internal_digest(value: dict[str, Any], field: str, label: str) -> None:
    digest = value.get(field)
    unsigned = {key: item for key, item in value.items() if key != field}
    if not isinstance(digest, str) or digest != canonical_sha256(unsigned):
        raise ResearchJobWorkerError("%s internal digest mismatch" % label)


def _validate_existing_bundle(inputs: dict[str, Any]) -> dict[str, Any]:
    planning = inputs["job_dir"] / "PLANNING"
    if planning.is_symlink() or not planning.is_dir():
        raise ResearchJobWorkerError("planning artifact directory is unsafe")
    receipt_raw = _read_regular(
        planning / "PLANNING_RECEIPT.json",
        label="PLANNING_RECEIPT.json",
        max_bytes=MAX_CONTROL_BYTES,
    )
    receipt = _json(receipt_raw, "PLANNING_RECEIPT.json")
    expected = {
        "schema_version": PLANNING_SCHEMA,
        "job_id": inputs["job"]["job_id"],
        "plan_sha256": inputs["plan_sha256"],
        "job_spec_sha256": inputs["job_spec_sha256"],
        "research_execution_started": False,
        "planning_only": True,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ResearchJobWorkerError("planning receipt differs on %s" % field)
    if receipt.get("state") not in TERMINAL_PLANNING_STATES:
        raise ResearchJobWorkerError("planning receipt state is invalid")
    artifact_sha256s = receipt.get("artifact_sha256s")
    if not isinstance(artifact_sha256s, dict):
        raise ResearchJobWorkerError("planning receipt artifact map is invalid")
    expected_names = {
        "DATA_CATALOG.json",
        "DATA_SELECTION.json",
        "PREFLIGHT.json",
    }
    if receipt["state"] == "READY":
        expected_names.add("EXECUTION_REQUEST.json")
    if set(artifact_sha256s) != expected_names:
        raise ResearchJobWorkerError("planning receipt artifact set is invalid")
    entries = list(planning.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ResearchJobWorkerError("planning directory contains an unsafe entry")
    actual_names = {path.name for path in entries}
    if actual_names != expected_names | {"PLANNING_RECEIPT.json"}:
        raise ResearchJobWorkerError("planning directory contains unexpected artifacts")

    values: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_names):
        raw = _read_regular(planning / name, label=name, max_bytes=MAX_CONTROL_BYTES)
        if artifact_sha256s.get(name) != _sha(raw):
            raise ResearchJobWorkerError("planning artifact SHA-256 mismatch: %s" % name)
        values[name] = _json(raw, name)
    _internal_digest(values["DATA_CATALOG.json"], "catalog_sha256", "catalog")
    _internal_digest(values["DATA_SELECTION.json"], "selection_sha256", "selection")
    _internal_digest(values["PREFLIGHT.json"], "preflight_sha256", "preflight")
    if values["DATA_SELECTION.json"].get("plan_sha256") != inputs["plan_sha256"]:
        raise ResearchJobWorkerError("selection does not bind exact plan")
    if values["PREFLIGHT.json"].get("selection_sha256") != values[
        "DATA_SELECTION.json"
    ].get("selection_sha256"):
        raise ResearchJobWorkerError("preflight does not bind exact selection")
    if values["DATA_SELECTION.json"].get("catalog_sha256") != values[
        "DATA_CATALOG.json"
    ].get("catalog_sha256"):
        raise ResearchJobWorkerError("selection does not bind exact catalog")
    if values["PREFLIGHT.json"].get("catalog_sha256") != values[
        "DATA_CATALOG.json"
    ].get("catalog_sha256"):
        raise ResearchJobWorkerError("preflight does not bind exact catalog")
    expected_selection_state = "SELECTED" if receipt["state"] == "READY" else receipt["state"]
    if values["DATA_SELECTION.json"].get("state") != expected_selection_state:
        raise ResearchJobWorkerError("selection state differs from planning receipt")
    expected_preflight_state = "READY" if receipt["state"] == "READY" else receipt["state"]
    if values["PREFLIGHT.json"].get("state") != expected_preflight_state:
        raise ResearchJobWorkerError("preflight state differs from planning receipt")
    if "EXECUTION_REQUEST.json" in values:
        _internal_digest(
            values["EXECUTION_REQUEST.json"],
            "execution_request_sha256",
            "execution request",
        )
        if values["EXECUTION_REQUEST.json"].get("research_execution_started") is not False:
            raise ResearchJobWorkerError("execution request claims research started")
        request = values["EXECUTION_REQUEST.json"]
        request_bindings = {
            "job_id": inputs["job"]["job_id"],
            "plan_sha256": inputs["plan_sha256"],
            "job_spec_sha256": inputs["job_spec_sha256"],
            "catalog_sha256": values["DATA_CATALOG.json"]["catalog_sha256"],
            "selection_sha256": values["DATA_SELECTION.json"]["selection_sha256"],
            "preflight_sha256": values["PREFLIGHT.json"]["preflight_sha256"],
            "plugin_source_sha256": receipt.get("plugin_source_sha256"),
            "planning_only": True,
            "downstream_authority_required": True,
            "downstream_one_shot_gate_required": True,
            "arbitrary_plan_code_execution": False,
        }
        for field, expected_value in request_bindings.items():
            if request.get(field) != expected_value:
                raise ResearchJobWorkerError(
                    "execution request differs on %s" % field
                )
    return {
        "job_id": inputs["job"]["job_id"],
        "state": receipt["state"],
        "planning_path": str(planning),
        "planning_receipt_sha256": _sha(receipt_raw),
        "research_execution_started": False,
        "idempotent_replay": True,
    }


def _transition_to_preflight(inputs: dict[str, Any]) -> None:
    state = get_job(inputs["root"], inputs["job"]["job_id"])["status"]["state"]
    if state == "QUEUED":
        update_status(
            inputs["root"],
            inputs["job"]["job_id"],
            state="PREFLIGHT",
            message="Building immutable local-only data planning artifacts.",
            details={"planning_only": True, "research_execution_started": False},
        )
    elif state != "PREFLIGHT":
        raise ResearchJobWorkerError("job cannot enter planning from state %s" % state)


def _finish_status(inputs: dict[str, Any], result_state: str, receipt: dict[str, Any]) -> None:
    current = get_job(inputs["root"], inputs["job"]["job_id"])["status"]["state"]
    if current == result_state:
        return
    if current == "QUEUED":
        _transition_to_preflight(inputs)
        current = "PREFLIGHT"
    if current != "PREFLIGHT":
        raise ResearchJobWorkerError(
            "job status is inconsistent with immutable planning artifacts"
        )
    messages = {
        "READY": "Planning complete; existing authority and one-shot gate are still required.",
        "BLOCKED": "Planning stopped because required data is unavailable.",
        "REFUSED": "Planning refused an unsafe or forbidden input scope.",
    }
    update_status(
        inputs["root"],
        inputs["job"]["job_id"],
        state=result_state,
        message=messages[result_state],
        details={
            "planning_only": True,
            "research_execution_started": False,
            "selection_sha256": receipt["selection_sha256"],
            "preflight_sha256": receipt["preflight_sha256"],
            "execution_request_created": receipt["execution_request_created"],
            "downstream_authority_required": result_state == "READY",
            "downstream_one_shot_gate_required": result_state == "READY",
        },
    )


def _terminal_error(inputs: dict[str, Any], state: str, message: str) -> None:
    current = get_job(inputs["root"], inputs["job"]["job_id"])["status"]["state"]
    if current == "QUEUED":
        _transition_to_preflight(inputs)
        current = "PREFLIGHT"
    if current == "PREFLIGHT":
        update_status(
            inputs["root"],
            inputs["job"]["job_id"],
            state=state,
            message=message,
            details={"planning_only": True, "research_execution_started": False},
        )


def plan_job(
    *,
    inbox_root: Path = DEFAULT_INBOX_ROOT,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    job_id: str,
) -> dict[str, Any]:
    """Run the deterministic planning stage for one inbox job."""
    inputs = _job_inputs(Path(inbox_root), job_id)
    spec = inputs["spec"]
    plugin_id = spec.get("plugin_id")
    if spec.get("state") == "NEEDS_METHOD" or registration_for(plugin_id) is None:
        if inputs["job"]["status"].get("state") != "NEEDS_METHOD":
            raise ResearchJobWorkerError("unregistered plugin job has an invalid state")
        return {
            "job_id": job_id,
            "state": "NEEDS_METHOD",
            "planning_path": None,
            "research_execution_started": False,
            "next_action": "REGISTER_HASH_PINNED_METHOD_PLUGIN",
        }
    if spec.get("state") != "READY":
        raise ResearchJobWorkerError("registered plugin job is not READY for preflight")
    if spec.get("plugin_registry_version") != REGISTRY_VERSION:
        raise ResearchJobWorkerError("job plugin registry version is unsupported")

    with _job_lock(inputs["job_dir"]):
        # Re-read after obtaining the cross-process lock.  This also catches a
        # control-file mutation that raced the first validation.
        inputs = _job_inputs(Path(inbox_root), job_id)
        planning = inputs["job_dir"] / "PLANNING"
        if planning.exists() or planning.is_symlink():
            result = _validate_existing_bundle(inputs)
            receipt = _json(
                _read_regular(
                    planning / "PLANNING_RECEIPT.json",
                    label="PLANNING_RECEIPT.json",
                    max_bytes=MAX_CONTROL_BYTES,
                ),
                "PLANNING_RECEIPT.json",
            )
            _finish_status(inputs, result["state"], receipt)
            return result

        _transition_to_preflight(inputs)
        try:
            loaded = load_registered_plugin(plugin_id)
            catalog = build_catalog(Path(cache_root))
            selection, preflight = resolve_data(spec, catalog)
            artifacts: dict[str, dict[str, Any]] = {
                "DATA_CATALOG.json": catalog,
                "DATA_SELECTION.json": selection,
                "PREFLIGHT.json": preflight,
            }
            if preflight.get("state") == "READY":
                execution_request = build_execution_request(
                    loaded,
                    context={
                        "job_id": job_id,
                        "plan_sha256": inputs["plan_sha256"],
                        "job_spec_sha256": inputs["job_spec_sha256"],
                        "catalog_sha256": catalog["catalog_sha256"],
                        "data_selection": selection,
                        "preflight": preflight,
                        "job_spec": spec,
                        "automatic_execution_requested": inputs["request"][
                            "automatic_execution_requested"
                        ],
                    },
                )
                artifacts["EXECUTION_REQUEST.json"] = execution_request
                result_state = "READY"
            elif preflight.get("state") == "REFUSED":
                result_state = "REFUSED"
            else:
                result_state = "BLOCKED"

            artifact_sha256s = {
                name: _sha(_artifact_bytes(value)) for name, value in artifacts.items()
            }
            receipt = {
                "schema_version": PLANNING_SCHEMA,
                "state": result_state,
                "job_id": job_id,
                "plan_sha256": inputs["plan_sha256"],
                "job_spec_sha256": inputs["job_spec_sha256"],
                "plugin_registry_version": REGISTRY_VERSION,
                "plugin_id": loaded.registration.plugin_id,
                "plugin_version": loaded.registration.plugin_version,
                "plugin_source_sha256": loaded.registration.source_sha256,
                "catalog_sha256": catalog["catalog_sha256"],
                "selection_sha256": selection["selection_sha256"],
                "preflight_sha256": preflight["preflight_sha256"],
                "execution_request_created": "EXECUTION_REQUEST.json" in artifacts,
                "artifact_sha256s": artifact_sha256s,
                "planning_only": True,
                "research_execution_started": False,
                "network_writes": 0,
                "s3_writes": 0,
                "production_mutations": 0,
                "arbitrary_plan_code_execution": False,
            }
            artifacts["PLANNING_RECEIPT.json"] = receipt
            planning = _publish_bundle(inputs["job_dir"], artifacts)
            _finish_status(inputs, result_state, receipt)
            receipt_raw = _read_regular(
                planning / "PLANNING_RECEIPT.json",
                label="PLANNING_RECEIPT.json",
                max_bytes=MAX_CONTROL_BYTES,
            )
            return {
                "job_id": job_id,
                "state": result_state,
                "planning_path": str(planning),
                "planning_receipt_sha256": _sha(receipt_raw),
                "research_execution_started": False,
                "idempotent_replay": False,
            }
        except PluginError as exc:
            _terminal_error(inputs, "REFUSED", "Hash-pinned method plugin refused planning.")
            raise ResearchJobWorkerError("method plugin refused planning") from exc
        except ResearchJobWorkerError:
            _terminal_error(inputs, "FAILED", "Planning failed closed.")
            raise
        except Exception as exc:
            _terminal_error(inputs, "FAILED", "Planning failed closed.")
            raise ResearchJobWorkerError("planning stage failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--inbox-root", type=Path, default=DEFAULT_INBOX_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    args = parser.parse_args(argv)
    try:
        result = plan_job(
            inbox_root=args.inbox_root,
            cache_root=args.cache_root,
            job_id=args.job_id,
        )
    except ResearchJobWorkerError as exc:
        print("RESEARCH_JOB_PLANNING_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
