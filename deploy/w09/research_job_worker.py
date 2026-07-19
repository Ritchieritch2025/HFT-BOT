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
from pathlib import Path
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
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_CONTROL_BYTES = 8 * 1024 * 1024
TERMINAL_PLANNING_STATES = {"READY", "BLOCKED", "REFUSED"}


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
