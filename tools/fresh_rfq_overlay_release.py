#!/usr/bin/env python3
"""Build an independent body-free RFQ overlay bound to a completed base.

This module cannot alter the base durable receipt or base MANIFEST.  It writes
only an RFQ overlay cache.  A later tagger/publisher step can fail or retry in
that cache while the already completed L1/L2/orderbook base stays successful.
No AWS transport is implemented here.
"""

from __future__ import annotations

import argparse
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
MAX_BODY_BYTES = 4 << 30


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


def _materialize_exact_bodies(value: Any) -> dict[str, Any]:
    """Replace JSON ``body_path`` bindings with verified exact bytes."""
    if not isinstance(value, dict):
        raise OverlayError("mapping inputs must be a JSON object")
    result = copy.deepcopy(value)
    for family in ("exact_analysis_rfq_objects", "orderbooks_l1_objects",
                   "orderbooks_full_objects"):
        rows = result.get(family)
        if not isinstance(rows, list):
            raise OverlayError(f"mapping input {family} must be a list")
        materialized = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or "body_path" not in row:
                raise OverlayError(f"{family}[{index}] lacks body_path")
            normalized = dict(row)
            path = pathlib.Path(normalized.pop("body_path"))
            raw = _read(path, limit=MAX_BODY_BYTES)
            if len(raw) > MAX_BODY_BYTES:
                raise OverlayError(f"mapping body exceeds limit: {path}")
            if (normalized.get("size") != len(raw)
                    or normalized.get("sha256")
                    != hashlib.sha256(raw).hexdigest()):
                raise OverlayError(f"mapping body identity mismatch: {path}")
            normalized["body"] = raw
            materialized.append(normalized)
        result[family] = materialized
    return result


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


def create_overlay_cache(*, date: str, eligibility_path: pathlib.Path,
                         base_terminal_path: pathlib.Path,
                         base_manifest_path: pathlib.Path,
                         base_manifest_identity_path: pathlib.Path,
                         mapping_inputs_path: pathlib.Path,
                         output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT
                         ) -> pathlib.Path:
    eligibility = gate.load_package(eligibility_path, expected_date=date)
    package = pathlib.Path(eligibility_path).absolute().parent
    envelope = _read(package / gate.AUTHORITY_NAME, json_value=True)
    authority = precommit.validate_precommit_envelope(envelope)["authority"]
    source_evidence = _read(package / gate.SOURCE_NAME, json_value=True)
    hour_set = _read(package / gate.HOURS_NAME, json_value=True)
    base_terminal_raw = _read(base_terminal_path)
    base_terminal = _validate_base_terminal(
        json.loads(base_terminal_raw.decode("utf-8")), base_terminal_raw, date)
    base_manifest_raw = _read(base_manifest_path)
    base_identity = _read(base_manifest_identity_path, json_value=True)
    mapping_inputs = _materialize_exact_bodies(
        _read(mapping_inputs_path, json_value=True))
    manifest = fresh.build_overlay_manifest(
        authority=authority, base_manifest_bytes=base_manifest_raw,
        base_manifest_exact_identity=base_identity,
        hour_receipts=hour_set["receipts"],
        source_evidence=source_evidence,
        mapping_provenance_inputs=mapping_inputs)
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
    target = root / f"date={date}" / (
        "base=" + base_identity["sha256"])
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
