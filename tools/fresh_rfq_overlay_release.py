#!/usr/bin/env python3
"""Build an independent body-free RFQ overlay bound to a completed base.

This module cannot alter the base durable receipt or base MANIFEST.  It writes
only an RFQ overlay cache.  A later tagger/publisher step can fail or retry in
that cache while the already completed L1/L2/orderbook base stays successful.
No AWS transport is implemented here.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fresh_rfq_daily_eligibility as gate  # noqa: E402
import fresh_rfq_receipts as fresh  # noqa: E402
import fresh_rfq_request_provenance as request_provenance  # noqa: E402
import fresh_rfq_universe_provenance as universe_provenance  # noqa: E402
import precommit_fresh_rfq_authority as precommit  # noqa: E402


SCHEMA = "fresh-rfq-overlay-receipt-v1"
STATE = "RFQ_OVERLAY_LOCALLY_READY"
STATUS_SCHEMA = "fresh-rfq-overlay-status-v1"
READY_NAME = "READY.json"
RECEIPT_NAME = "OVERLAY-RECEIPT.json"
MANIFEST_NAME = "MANIFEST.json"
DEFAULT_OUTPUT_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/fresh_rfq_overlays")
MAX_BYTES = 64 << 20
MAX_CHECKPOINT_BYTES = 512 << 20
MAX_ANALYSIS_RFQ_OBJECTS = 4096
MAX_BASE_ORDERBOOK_OBJECTS = 4096
MAX_SINGLE_EXACT_OBJECT_BYTES = 16 << 30
MAX_EXACT_INPUT_BYTES_PER_DATE = 256 << 30
MIN_SCRATCH_RESERVE_BYTES = 64 << 20
CHECKPOINT_SCHEMA = "fresh-rfq-overlay-heavy-checkpoint-v1"


class OverlayError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _read(path: pathlib.Path, *, json_value: bool = False,
          limit: int = MAX_BYTES):
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            raise OverlayError(f"unsafe input: {path}")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        raw = bytes(chunks)
        after = os.fstat(fd)
        if (len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            raise OverlayError(f"input changed: {path}")
    finally:
        os.close(fd)
    if not json_value:
        return raw
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OverlayError(f"invalid JSON {path}: {exc}") from exc


def _write(path: pathlib.Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OverlayError(f"write failed: {path}")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


class _BodyPathExactReader:
    """Compatibility reader for local body-path fixtures/configuration.

    It stores identities and paths only.  The bounded request/universe modules
    open and verify one body while its context is active; no all-day byte map
    is ever constructed.
    """

    def __init__(self, value: Any):
        if not isinstance(value, dict):
            raise OverlayError("mapping inputs must be a JSON object")
        self.pre_event_window_ms = value.get("pre_event_window_ms")
        self.post_event_window_ms = value.get("post_event_window_ms")
        self._paths: dict[tuple[str, str, str], pathlib.Path] = {}
        allowed = {
            "exact_analysis_rfq_objects", "orderbooks_l1_objects",
            "orderbooks_full_objects", "pre_event_window_ms",
            "post_event_window_ms"}
        if set(value) != allowed:
            raise OverlayError("mapping input fields differ")
        for family in ("exact_analysis_rfq_objects", "orderbooks_l1_objects",
                       "orderbooks_full_objects"):
            rows = value.get(family)
            if not isinstance(rows, list):
                raise OverlayError(f"mapping input {family} must be a list")
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or "body_path" not in row:
                    raise OverlayError(f"{family}[{index}] lacks body_path")
                exact = (row.get("bucket"), row.get("key"),
                         row.get("version_id"))
                path = pathlib.Path(row["body_path"]).absolute()
                prior = self._paths.get(exact)
                if prior is not None and prior != path:
                    raise OverlayError("one exact identity has conflicting paths")
                self._paths[exact] = path

    @contextlib.contextmanager
    def open_exact(self, identity: dict[str, Any]):
        exact = (identity.get("bucket"), identity.get("key"),
                 identity.get("version_id"))
        path = self._paths.get(exact)
        if path is None:
            raise OverlayError(f"mapping body path is absent: {exact!r}")
        try:
            observed = path.lstat()
        except OSError as exc:
            raise OverlayError(f"mapping body path invalid: {exc}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise OverlayError(f"mapping body path is unsafe: {path}")
        yield type("OpenedExact", (), {"path": path})()


def _body_path_reader(value: Any) -> _BodyPathExactReader:
    """Return an identity-only, one-object-at-a-time compatibility reader."""
    if not isinstance(value, dict):
        raise OverlayError("mapping inputs must be a JSON object")
    return _BodyPathExactReader(value)


def _validate_base_terminal(value: Any, raw: bytes, date: str) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or value.get("schema_version") != "research-v3-daily-status-v2"
            or value.get("state") != "V3_REFERENCE_PUBLISHED"
            or value.get("date") != date
            or value.get("rfq") != "OFF"
            or value.get("manifest_commit_state") not in {
                "REFERENCE_MANIFEST_COMMITTED",
                "REFERENCE_MANIFEST_ALREADY_COMMITTED",
            }
            or not isinstance(value.get("manifest_object"), dict)
            or set(value["manifest_object"]) != {
                "bucket", "key", "VersionId", "size", "sha256",
                "verification_state",
            }
            or value["manifest_object"].get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        raise OverlayError("base terminal is not a completed RFQ-free release")
    result = copy.deepcopy(value)
    result["terminal_file_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def _module_file_sha256(module: Any) -> str:
    path = pathlib.Path(module.__file__).resolve(strict=True)
    raw = _read(path, limit=8 << 20)
    return hashlib.sha256(raw).hexdigest()


def _bounded_exact_inputs(*, analysis: list[dict[str, Any]],
                          l1: list[dict[str, Any]],
                          l2: list[dict[str, Any]],
                          scratch_root: pathlib.Path) -> None:
    groups = (
        ("analysis RFQ", analysis, MAX_ANALYSIS_RFQ_OBJECTS),
        ("orderbooks_l1", l1, MAX_BASE_ORDERBOOK_OBJECTS),
        ("orderbooks_full", l2, MAX_BASE_ORDERBOOK_OBJECTS),
    )
    total = 0
    largest = 0
    physical = set()
    for label, rows, maximum in groups:
        if not isinstance(rows, list) or not rows or len(rows) > maximum:
            raise OverlayError(f"{label} object-count bound exceeded")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise OverlayError(f"{label}[{index}] identity is invalid")
            size = row.get("size")
            if type(size) is not int or size < 0 \
                    or size > MAX_SINGLE_EXACT_OBJECT_BYTES:
                raise OverlayError(f"{label}[{index}] size bound exceeded")
            exact = (row.get("bucket"), row.get("key"), row.get("version_id"))
            if exact in physical:
                raise OverlayError("exact input identity is duplicated")
            physical.add(exact)
            total += size
            largest = max(largest, size)
    if total > MAX_EXACT_INPUT_BYTES_PER_DATE:
        raise OverlayError("date exact-input byte bound exceeded")
    scratch = pathlib.Path(scratch_root).absolute()
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The universe reader briefly holds the transport cache and one private
    # DuckDB stage for the same largest object.  Refuse before opening a body
    # unless that strict two-copy ephemeral peak plus reserve fits.
    required = largest * 2 + MIN_SCRATCH_RESERVE_BYTES
    if shutil.disk_usage(scratch).free < required:
        raise OverlayError(
            f"scratch capacity below exact bound: need={required}")


def _checkpoint_input_sha(stage: str, payload: dict[str, Any]) -> str:
    contract = {
        "stage": stage,
        "payload": payload,
        "request_module_sha256": _module_file_sha256(request_provenance),
        "universe_module_sha256": _module_file_sha256(universe_provenance),
        "receipts_module_sha256": _module_file_sha256(fresh),
    }
    return canonical_sha256(contract)


def _validate_request_checkpoint(value: Any, expected: dict[str, Any]) -> None:
    if (not isinstance(value, dict)
            or set(value) != request_provenance.OUTPUT_FIELDS):
        raise OverlayError("request checkpoint fields differ")
    unsigned = copy.deepcopy(value)
    supplied = unsigned.pop("receipt_sha256", None)
    if supplied != request_provenance.canonical_sha256(unsigned):
        raise OverlayError("request checkpoint digest differs")
    bindings = {
        "analysis_date": expected["analysis_date"],
        "authority_sha256": expected["authority_sha256"],
        "source_evidence_sha256": expected["source_evidence_sha256"],
        "time_contract_sha256": expected["time_contract_sha256"],
        "analysis_rfq_objects": expected["analysis_rfq_objects"],
        "analysis_rfq_object_set_sha256": canonical_sha256(
            expected["analysis_rfq_objects"]),
    }
    if any(canonical_bytes(value.get(key)) != canonical_bytes(wanted)
           for key, wanted in bindings.items()):
        raise OverlayError("request checkpoint input binding differs")
    for field in (
            "all_input_objects_matched", "all_object_bytes_parsed",
            "all_physical_rows_classified", "all_unique_created_projected",
            "input_bodies_omitted"):
        if value.get(field) is not True:
            raise OverlayError(f"request checkpoint lacks {field}")


def _validate_universe_checkpoint(value: Any, expected: dict[str, Any]) -> None:
    if (not isinstance(value, dict)
            or set(value) != universe_provenance.OUTPUT_FIELDS):
        raise OverlayError("universe checkpoint fields differ")
    unsigned = copy.deepcopy(value)
    supplied = unsigned.pop("provenance_sha256", None)
    if supplied != universe_provenance.canonical_sha256(unsigned):
        raise OverlayError("universe checkpoint digest differs")
    if (value.get("analysis_date") != expected["analysis_date"]
            or value.get("base_binding_sha256") !=
            expected["base_binding_sha256"]):
        raise OverlayError("universe checkpoint input binding differs")
    for family in ("orderbooks_l1", "orderbooks_full"):
        observed = value.get("families", {}).get(family, {})
        wanted = expected[family]
        if (observed.get("source_object_count") != len(wanted)
                or observed.get("source_object_set_sha256") !=
                canonical_sha256(wanted)):
            raise OverlayError(f"universe checkpoint {family} set differs")
    if (value.get("all_base_family_objects_present") is not True
            or value.get("all_input_bodies_omitted_from_output") is not True
            or value.get("ephemeral_temp_deleted_before_return") is not True):
        raise OverlayError("universe checkpoint is incomplete")


def _load_or_build_checkpoint(
        *, root: pathlib.Path, stage: str, input_sha256: str,
        expected: dict[str, Any], validator: Callable[[Any, dict[str, Any]], None],
        builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    root = pathlib.Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{stage}-{input_sha256}.json"
    if path.exists():
        envelope = _read(
            path, json_value=True, limit=MAX_CHECKPOINT_BYTES)
        if (not isinstance(envelope, dict)
                or set(envelope) != {
                    "schema_version", "stage", "stage_input_sha256",
                    "output", "output_sha256"}
                or envelope.get("schema_version") != CHECKPOINT_SCHEMA
                or envelope.get("stage") != stage
                or envelope.get("stage_input_sha256") != input_sha256
                or envelope.get("output_sha256") !=
                canonical_sha256(envelope.get("output"))):
            raise OverlayError(f"{stage} checkpoint envelope differs")
        validator(envelope["output"], expected)
        return copy.deepcopy(envelope["output"])
    output = builder()
    validator(output, expected)
    envelope = {
        "schema_version": CHECKPOINT_SCHEMA,
        "stage": stage,
        "stage_input_sha256": input_sha256,
        "output": output,
        "output_sha256": canonical_sha256(output),
    }
    raw = canonical_bytes(envelope) + b"\n"
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise OverlayError(f"{stage} checkpoint exceeds hard byte bound")
    _write(path, raw)
    return copy.deepcopy(output)


def _commit_overlay_cache(*, date: str, eligibility: dict[str, Any],
                          base_terminal: dict[str, Any],
                          base_identity: dict[str, Any],
                          manifest: dict[str, Any],
                          output_root: pathlib.Path) -> pathlib.Path:
    if manifest["eligible_date"] != date:
        raise OverlayError("overlay date differs from eligibility")
    eligible_projection = sorted([{
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for row in eligibility["eligible_objects"]], key=lambda row: row["key"])
    if manifest["analysis_rfq_objects"] != eligible_projection:
        raise OverlayError("overlay exact RFQ set differs from eligibility")
    terminal_manifest = base_terminal["manifest_object"]
    expected_terminal = {
        "bucket": base_identity["bucket"], "key": base_identity["key"],
        "version_id": base_identity["version_id"], "size": base_identity["size"],
        "sha256": base_identity["sha256"],
    }
    observed_terminal = {
        "bucket": terminal_manifest.get("bucket"),
        "key": terminal_manifest.get("key"),
        "version_id": terminal_manifest.get("VersionId"),
        "size": terminal_manifest.get("size"),
        "sha256": terminal_manifest.get("sha256"),
    }
    if observed_terminal != expected_terminal:
        raise OverlayError("base terminal and exact base MANIFEST differ")
    receipt = {
        "schema_version": SCHEMA,
        "state": STATE,
        "date": date,
        "base_state": "V3_REFERENCE_PUBLISHED",
        "base_rfq": "OFF",
        "base_terminal_file_sha256": base_terminal["terminal_file_sha256"],
        "base_manifest_exact_identity": base_identity,
        "base_binding_sha256": manifest["base_binding_sha256"],
        "eligibility_sha256": eligibility["eligibility_sha256"],
        "authority_sha256": eligibility["authority_sha256"],
        "source_evidence_sha256": eligibility["source_evidence_sha256"],
        "analysis_rfq_objects": manifest["analysis_rfq_objects"],
        "analysis_rfq_object_set_sha256": manifest[
            "analysis_rfq_object_set_sha256"],
        "overlay_manifest_sha256": manifest["manifest_sha256"],
        "required_tags": {
            "research-eligible": "true", "research-channel": "rfq"},
        "tag_state": "NOT_ATTEMPTED",
        "publication_state": "NOT_ATTEMPTED",
        "base_mutations": 0,
        "data_objects_copied": 0,
        "aws_writes": 0,
    }
    receipt["overlay_receipt_sha256"] = canonical_sha256(receipt)
    ready = {
        "schema_version": "fresh-rfq-overlay-cache-ready-v1",
        "state": "RFQ_OVERLAY_CACHE_READY",
        "date": date,
        "overlay_receipt_sha256": receipt["overlay_receipt_sha256"],
        "overlay_manifest_sha256": manifest["manifest_sha256"],
        "base_terminal_file_sha256": base_terminal["terminal_file_sha256"],
        "base_mutations": 0,
    }
    ready["ready_sha256"] = canonical_sha256(ready)
    root = pathlib.Path(output_root).absolute()
    target = root / f"date={date}" / ("base=" + base_identity["sha256"])
    if target.exists():
        existing = _read(target / READY_NAME, json_value=True)
        if existing != ready:
            raise OverlayError("immutable overlay cache conflicts")
        return target / READY_NAME
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    pending = pathlib.Path(tempfile.mkdtemp(
        prefix=".overlay-pending-", dir=target.parent))
    try:
        _write(pending / MANIFEST_NAME, canonical_bytes(manifest) + b"\n")
        _write(pending / RECEIPT_NAME, canonical_bytes(receipt) + b"\n")
        _write(pending / READY_NAME, canonical_bytes(ready) + b"\n")
        os.rename(pending, target)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return target / READY_NAME


def create_overlay_cache(*, date: str, eligibility_path: pathlib.Path,
                         base_terminal_path: pathlib.Path,
                         base_manifest_path: pathlib.Path,
                         base_manifest_identity_path: pathlib.Path,
                         mapping_inputs_path: pathlib.Path,
                         output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT
                         ) -> pathlib.Path:
    base_identity = _read(base_manifest_identity_path, json_value=True)
    reader = _body_path_reader(_read(mapping_inputs_path, json_value=True))
    return create_overlay_cache_from_reader(
        date=date, eligibility_path=eligibility_path,
        base_terminal_path=base_terminal_path,
        base_manifest_path=base_manifest_path,
        base_manifest_exact_identity=base_identity,
        open_exact=reader.open_exact,
        checkpoint_root=pathlib.Path(output_root) / ".heavy-checkpoints",
        output_root=output_root,
        pre_event_window_ms=reader.pre_event_window_ms,
        post_event_window_ms=reader.post_event_window_ms)


def create_overlay_cache_from_reader(
        *, date: str, eligibility_path: pathlib.Path,
        base_terminal_path: pathlib.Path, base_manifest_path: pathlib.Path,
        base_manifest_exact_identity: dict[str, Any],
        open_exact: Callable[[dict[str, Any]], Any],
        checkpoint_root: pathlib.Path,
        output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT,
        pre_event_window_ms: int = 300_000,
        post_event_window_ms: int = 300_000) -> pathlib.Path:
    """Build a full overlay while opening at most one exact body at a time.

    Completed request and L1/L2-universe derivations are content-addressed
    checkpoints.  A retry after a later-stage failure reuses those body-free
    receipts and the reader's exact-object disk cache instead of downloading
    or retaining an all-day body map in memory.
    """
    if not callable(open_exact):
        raise OverlayError("open_exact must be callable")
    eligibility = gate.load_package(eligibility_path, expected_date=date)
    package = pathlib.Path(eligibility_path).absolute().parent
    envelope = _read(package / gate.AUTHORITY_NAME, json_value=True)
    authority = precommit.validate_precommit_envelope(envelope)["authority"]
    source_evidence = _read(package / gate.SOURCE_NAME, json_value=True)
    hour_set = _read(package / gate.HOURS_NAME, json_value=True)
    base_terminal_raw = _read(base_terminal_path)
    try:
        base_terminal_value = json.loads(base_terminal_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OverlayError(f"invalid base terminal: {exc}") from exc
    base_terminal = _validate_base_terminal(
        base_terminal_value, base_terminal_raw, date)
    base_manifest_raw = _read(base_manifest_path)
    base_identity = copy.deepcopy(base_manifest_exact_identity)
    base = fresh._build_base_binding(
        manifest_bytes=base_manifest_raw,
        manifest_exact_identity=base_identity, date=date)
    analysis, watermark, analysis_objects, watermark_objects = \
        fresh._overlay_components(
            authority, base, hour_set["receipts"], source_evidence)
    normalized_source = fresh._normalize_source_evidence(
        source_evidence, authority, base, analysis, watermark,
        analysis_objects, watermark_objects)
    time_contract = fresh._derive_time_contract(base)
    rfq_exact = [{"bucket": fresh.SOURCE_BUCKET, **row}
                 for row in analysis_objects]
    l1_exact = copy.deepcopy(base["families"]["orderbooks_l1"]["objects"])
    l2_exact = copy.deepcopy(base["families"]["orderbooks_full"]["objects"])
    _bounded_exact_inputs(
        analysis=rfq_exact, l1=l1_exact, l2=l2_exact,
        scratch_root=checkpoint_root)
    checkpoint_dir = (pathlib.Path(checkpoint_root).absolute()
                      / f"date={date}" / f"base={base_identity['sha256']}")

    request_expected = {
        "analysis_date": date,
        "authority_sha256": authority["authority_sha256"],
        "source_evidence_sha256": normalized_source["evidence_sha256"],
        "time_contract_sha256": fresh.canonical_sha256(time_contract),
        "analysis_rfq_objects": analysis_objects,
    }
    request_input_sha = _checkpoint_input_sha("request-provenance", {
        **request_expected, "exact_analysis_rfq_objects": rfq_exact})
    request_receipt = _load_or_build_checkpoint(
        root=checkpoint_dir, stage="request-provenance",
        input_sha256=request_input_sha, expected=request_expected,
        validator=_validate_request_checkpoint,
        builder=lambda: request_provenance.build_request_provenance_from_reader(
            analysis_date=date,
            authority_sha256=authority["authority_sha256"],
            source_evidence_sha256=normalized_source["evidence_sha256"],
            time_contract_sha256=fresh.canonical_sha256(time_contract),
            analysis_rfq_objects=analysis_objects,
            exact_analysis_rfq_objects=rfq_exact,
            open_exact=open_exact))

    universe_expected = {
        "analysis_date": date,
        "base_binding_sha256": base["binding_sha256"],
        "orderbooks_l1": l1_exact,
        "orderbooks_full": l2_exact,
    }
    universe_input_sha = _checkpoint_input_sha("universe-provenance", {
        **universe_expected,
        "manifest_exact_identity": base_identity,
        "manifest_sha256": hashlib.sha256(base_manifest_raw).hexdigest(),
    })
    universe_receipt = _load_or_build_checkpoint(
        root=checkpoint_dir, stage="universe-provenance",
        input_sha256=universe_input_sha, expected=universe_expected,
        validator=_validate_universe_checkpoint,
        builder=lambda: universe_provenance.
        build_universe_provenance_from_reader(
            manifest_bytes=base_manifest_raw,
            manifest_exact_identity=base_identity, date=date,
            orderbooks_l1_objects=l1_exact,
            orderbooks_full_objects=l2_exact,
            open_exact=open_exact))
    manifest = fresh.build_overlay_manifest_from_provenance(
        authority=authority, base_manifest_bytes=base_manifest_raw,
        base_manifest_exact_identity=base_identity,
        hour_receipts=hour_set["receipts"],
        source_evidence=source_evidence,
        request_provenance=request_receipt,
        universe_provenance=universe_receipt,
        pre_event_window_ms=pre_event_window_ms,
        post_event_window_ms=post_event_window_ms)
    return _commit_overlay_cache(
        date=date, eligibility=eligibility, base_terminal=base_terminal,
        base_identity=base_identity, manifest=manifest,
        output_root=output_root)


def attempt_overlay_tagging(*, base_terminal_path: pathlib.Path,
                            overlay_receipt_path: pathlib.Path,
                            tagger: Callable[[dict[str, Any]], dict[str, Any]],
                            status_path: pathlib.Path) -> dict[str, Any]:
    """Record tagger outcome only in overlay state; base is read twice."""
    before = _read(base_terminal_path)
    base = json.loads(before.decode("utf-8"))
    receipt = _read(overlay_receipt_path, json_value=True)
    date = receipt.get("date")
    _validate_base_terminal(base, before, date)
    try:
        proof = tagger(copy.deepcopy(receipt))
        if (not isinstance(proof, dict)
                or proof.get("state") != "EXACT_VERSION_TAG_READBACK_VERIFIED"
                or proof.get("rfq") != "FRESH_SEALED"):
            raise OverlayError("tagger proof is not strict dual-tag PASS")
        state = "RFQ_OVERLAY_TAGGED_READY"
        code = None
    except Exception as exc:
        proof = None
        state = "RFQ_OVERLAY_OFF"
        code = f"TAGGER_FAILED:{type(exc).__name__}"
    after = _read(base_terminal_path)
    if after != before:
        raise OverlayError("base terminal changed during overlay attempt")
    status = {
        "schema_version": STATUS_SCHEMA,
        "state": state,
        "date": date,
        "base_state": "V3_REFERENCE_PUBLISHED",
        "base_rfq": "OFF",
        "base_terminal_file_sha256": hashlib.sha256(before).hexdigest(),
        "overlay_receipt_sha256": receipt["overlay_receipt_sha256"],
        "tagger_proof": proof,
        "reason_code": code,
        "base_mutations": 0,
    }
    status["status_sha256"] = canonical_sha256(status)
    pathlib.Path(status_path).parent.mkdir(parents=True, exist_ok=True)
    _write(pathlib.Path(status_path), canonical_bytes(status) + b"\n")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True)
    parser.add_argument("--eligible", required=True, type=pathlib.Path)
    parser.add_argument("--base-terminal", required=True, type=pathlib.Path)
    parser.add_argument("--base-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--base-manifest-identity", required=True,
                        type=pathlib.Path)
    parser.add_argument("--mapping-inputs", required=True, type=pathlib.Path)
    parser.add_argument("--output-root", type=pathlib.Path,
                        default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)
    try:
        ready = create_overlay_cache(
            date=args.date, eligibility_path=args.eligible,
            base_terminal_path=args.base_terminal,
            base_manifest_path=args.base_manifest,
            base_manifest_identity_path=args.base_manifest_identity,
            mapping_inputs_path=args.mapping_inputs,
            output_root=args.output_root)
        print(json.dumps({
            "state": "RFQ_OVERLAY_CACHE_READY", "date": args.date,
            "ready": str(ready), "base_mutations": 0, "aws_writes": 0,
        }, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"RFQ_OVERLAY_OFF {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
