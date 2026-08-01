"""Synthetic tests for the real three-day Stage-2 authority CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.research.crypto_mm import round4_stage2_postfill_extractor as V1
from tools.research.crypto_mm import round4_stage2_postfill_extractor_v2 as V2
from tools.research.crypto_mm.round4_stage2_real_authority import (
    BOUND_MANIFEST_FILENAME,
    SOURCE_AUTHORITY_FILENAME,
    RealAuthorityError,
    build_real_source_receipts,
    materialize_real_source_receipts,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    return path


def _eligible_object(
    *,
    day: str,
    table: str,
    logical_key: str,
    payload: bytes,
) -> dict[str, object]:
    return {
        "VersionId": f"source-version-{day}-{table}",
        "bucket": "kalshi-vault-ritcardo",
        "date": day,
        "durability_scope": True,
        "durability_verified": True,
        "eligibility_tag_state": "TAGGED_VERIFIED",
        "exposure_policy": "RESEARCH_ELIGIBLE",
        "family": "facts",
        "key": f"ec2/{logical_key}",
        "logical_source_key": logical_key,
        "mutable_source": False,
        "research_candidate": True,
        "research_eligible": True,
        "sha256": _sha(payload),
        "size": len(payload),
        "table": table,
        "verification_state": "EXACT_VERSION_FULL_SHA256",
        "version_resolution": "SEALED_CURRENT_EXACT",
    }


def _receipt_binding(
    *,
    day: str,
    digest: str,
    raw: bytes,
    version: str,
) -> dict[str, object]:
    return {
        "bucket": "kalshi-vault-ritcardo",
        "key": (
            "ec2/control/canonical-receipts/v1/"
            f"date={day}/receipt-{digest}.json"
        ),
        "VersionId": version,
        "size": len(raw),
        "sha256": _sha(raw),
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    }


def _build_fixture(tmp_path: Path) -> dict[str, object]:
    content_root = tmp_path / "work"
    live_root = content_root / "live"
    output_dir = tmp_path / "output"
    live_root.mkdir(parents=True)
    output_dir.mkdir()
    by_day: dict[str, dict[str, Path | str]] = {}
    for day in V1.DISCOVERY_DATES:
        objects: list[dict[str, object]] = []
        for table, logical_key in V1.required_btc_fact_keys(day).items():
            payload = f"{day}:{table}:exact-parent\n".encode()
            parent = content_root / logical_key
            parent.parent.mkdir(parents=True, exist_ok=True)
            parent.write_bytes(payload)
            objects.append(
                _eligible_object(
                    day=day,
                    table=table,
                    logical_key=logical_key,
                    payload=payload,
                )
            )

        durable_digest = _sha(f"durable:{day}".encode())
        durable_dir = (
            live_root
            / "canonical_receipts"
            / "durable"
            / f"date={day}"
        )
        durable_receipt = {
            "schema_version": "canonical-object-receipt-v1",
            "state": "DURABLE_RECEIPT_VERIFIED",
            "authority": "CANONICAL_CONTROL_PLANE",
            "authoritative": True,
            "s3_published": True,
            "date": day,
            "receipt_set_sha256": durable_digest,
            "seal": {"date": day, "status": "SEALED", "version": 2},
            "objects": objects,
        }
        durable_receipt_path = _write_json(
            durable_dir / f"receipt-{durable_digest}.json",
            durable_receipt,
        )
        durable_raw = durable_receipt_path.read_bytes()
        durable_index = {
            "schema_version": "canonical-durable-receipt-index-v1",
            "state": "DURABLE_RECEIPT_VERIFIED",
            "date": day,
            "receipt_set_sha256": durable_digest,
            "complete": True,
            "prune_eligible": False,
            "receipt_object": _receipt_binding(
                day=day,
                digest=durable_digest,
                raw=durable_raw,
                version=f"durable-receipt-version-{day}",
            ),
        }
        durable_index_path = _write_json(
            durable_dir / f"DURABLE-{durable_digest}.json",
            durable_index,
        )

        tagged_digest = _sha(f"tagged:{day}".encode())
        audit_digest = _sha(f"audit:{day}".encode())
        durable_set = _sha(f"object-durability:{day}".encode())
        tagged_dir = (
            live_root
            / "canonical_receipts"
            / "tagged"
            / f"date={day}"
        )
        tagged_receipt = {
            "schema_version": "canonical-object-receipt-v1",
            "state": "DURABLE_RECEIPT_VERIFIED",
            "authority": "CANONICAL_CONTROL_PLANE",
            "authoritative": True,
            "s3_published": True,
            "date": day,
            "prune_eligible": False,
            "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
            "receipt_set_sha256": tagged_digest,
            "byte_attestation_receipt_set_sha256": durable_digest,
            "durability_set_sha256": durable_set,
            "eligibility_single_writer_audit_sha256": audit_digest,
            "objects": objects,
        }
        tagged_receipt_path = _write_json(
            tagged_dir / f"receipt-{tagged_digest}.json",
            tagged_receipt,
        )
        tagged_raw = tagged_receipt_path.read_bytes()
        tagged_index = {
            "schema_version": "canonical-durable-receipt-index-v1",
            "state": "DURABLE_RECEIPT_VERIFIED",
            "date": day,
            "receipt_set_sha256": tagged_digest,
            "complete": True,
            "completed": True,
            "prune_eligible": False,
            "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
            "byte_attestation_receipt_set_sha256": durable_digest,
            "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
            "eligibility_single_writer_audit_sha256": audit_digest,
            "receipt_payload_size": len(tagged_raw),
            "receipt_payload_sha256": _sha(tagged_raw),
            "receipt_object": _receipt_binding(
                day=day,
                digest=tagged_digest,
                raw=tagged_raw,
                version=f"tagged-receipt-version-{day}",
            ),
        }
        tagged_index_path = _write_json(
            tagged_dir / f"TAGGED-DURABLE-{tagged_digest}.json",
            tagged_index,
        )

        status = {
            "schema_version": "research-v3-daily-status-v2",
            "state": "V3_REFERENCE_PUBLISHED",
            "date": day,
            "durable_receipt_set_sha256": durable_digest,
            "tagged_index": str(tagged_index_path),
            "destination": "s3://fixture/research",
            "live_dir": str(live_root),
            "release_id": f"{day}__v3ref__seal-12345678__pub-0123456789abcdef",
            "prepared_plan_sha256": _sha(f"plan:{day}".encode()),
            "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
            "manifest_object": {
                "bucket": "kalshi-vault-ritcardo",
                "key": f"research/releases/{day}/MANIFEST.json",
                "VersionId": f"manifest-version-{day}",
                "size": 123,
                "sha256": _sha(f"manifest:{day}".encode()),
                "verification_state": "EXACT_VERSION_FULL_SHA256",
            },
            "rfq": "OFF",
            "completed_at_utc": f"{day}T23:59:59Z",
        }
        status_path = _write_json(
            live_root
            / "research_v3_daily"
            / f"date={day}"
            / f"receipt={durable_digest}"
            / "STATUS.json",
            status,
        )
        by_day[day] = {
            "status": status_path,
            "durable_index": durable_index_path,
            "durable_receipt": durable_receipt_path,
            "tagged_index": tagged_index_path,
            "tagged_receipt": tagged_receipt_path,
            "durable_digest": durable_digest,
            "tagged_digest": tagged_digest,
        }
    return {
        "content_root": content_root,
        "live_root": live_root,
        "output_dir": output_dir,
        "by_day": by_day,
    }


def test_materializes_exact_v1_v2_receipts_and_raw_hash_summary(
    tmp_path: Path,
):
    tree = _build_fixture(tmp_path)
    summary = materialize_real_source_receipts(
        live_root=tree["live_root"],
        content_root=tree["content_root"],
        output_dir=tree["output_dir"],
    )
    authority_path = Path(
        summary["authority_receipt"]["path"]  # type: ignore[index]
    )
    manifest_path = Path(
        summary["bound_manifest_receipt"]["path"]  # type: ignore[index]
    )
    assert authority_path.name == SOURCE_AUTHORITY_FILENAME
    assert manifest_path.name == BOUND_MANIFEST_FILENAME
    assert authority_path.read_bytes().endswith(b"\n")
    assert manifest_path.read_bytes().endswith(b"\n")
    assert summary["authority_receipt"]["raw_byte_sha256"] == _sha(  # type: ignore[index]
        authority_path.read_bytes()
    )
    assert summary["bound_manifest_receipt"]["raw_byte_sha256"] == _sha(  # type: ignore[index]
        manifest_path.read_bytes()
    )
    authority = json.loads(authority_path.read_bytes())
    manifest = json.loads(manifest_path.read_bytes())
    assert authority["source_dates"] == list(V1.DISCOVERY_DATES)
    assert len(authority["exact_version_objects"]) == 6
    assert manifest["file_count"] == 6
    assert manifest["authority_sha256"] == V1.canonical_sha256(authority)
    assert len(summary["chain"]) == 3
    assert summary["forbidden_dates_excluded"] == [
        "2026-07-23",
        "2026-07-26",
    ]

    before = {
        authority_path: authority_path.read_bytes(),
        manifest_path: manifest_path.read_bytes(),
    }
    with pytest.raises(RealAuthorityError, match="OUTPUT_EXISTS"):
        materialize_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
            output_dir=tree["output_dir"],
        )
    assert {path: path.read_bytes() for path in before} == before


def test_cli_prints_paths_raw_hashes_and_chain_summary(tmp_path: Path):
    tree = _build_fixture(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.research.crypto_mm.round4_stage2_real_authority",
            "--live-root",
            str(tree["live_root"]),
            "--content-root",
            str(tree["content_root"]),
            "--output-dir",
            str(tree["output_dir"]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["status"] == "SOURCE_PREPARATION_RECEIPTS_WRITTEN"
    assert summary["bound_manifest_receipt"]["file_count"] == 6
    assert len(summary["chain"]) == 3


@pytest.mark.parametrize("layer", ("durable", "tagged"))
def test_rejects_receipt_payload_tamper(tmp_path: Path, layer: str):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    path = Path(tree["by_day"][day][f"{layer}_receipt"])  # type: ignore[index]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(RealAuthorityError, match="RECEIPT_BYTE_MISMATCH"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


@pytest.mark.parametrize("field", ("size", "sha256"))
def test_rejects_index_sha_or_size_mismatch(
    tmp_path: Path,
    field: str,
):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    index_path = Path(tree["by_day"][day]["tagged_index"])  # type: ignore[index]
    index = json.loads(index_path.read_bytes())
    if field == "size":
        index["receipt_object"]["size"] += 1
    else:
        index["receipt_object"]["sha256"] = "f" * 64
    _write_json(index_path, index)
    with pytest.raises(RealAuthorityError, match="RECEIPT_BYTE_MISMATCH"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


def test_rejects_duplicate_published_status(tmp_path: Path):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    original = Path(tree["by_day"][day]["status"])  # type: ignore[index]
    duplicate_digest = "f" * 64
    duplicate = (
        Path(tree["live_root"])
        / "research_v3_daily"
        / f"date={day}"
        / f"receipt={duplicate_digest}"
        / "STATUS.json"
    )
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())
    with pytest.raises(
        RealAuthorityError,
        match="STATUS_CARDINALITY_MISMATCH",
    ):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


def test_rejects_symlink_anywhere_in_selected_chain(tmp_path: Path):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    tagged_index = Path(tree["by_day"][day]["tagged_index"])  # type: ignore[index]
    real_copy = tmp_path / "tagged-index-copy.json"
    real_copy.write_bytes(tagged_index.read_bytes())
    tagged_index.unlink()
    tagged_index.symlink_to(real_copy)
    with pytest.raises(RealAuthorityError, match="SYMLINK_FORBIDDEN"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "research-v3-daily-status-v1"),
        ("state", "BLOCKED"),
    ),
)
def test_rejects_wrong_status(
    tmp_path: Path,
    field: str,
    value: str,
):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    status_path = Path(tree["by_day"][day]["status"])  # type: ignore[index]
    status = json.loads(status_path.read_bytes())
    status[field] = value
    _write_json(status_path, status)
    with pytest.raises(RealAuthorityError, match="STATUS_NOT_PUBLISHED"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


def test_rejects_tagged_index_path_traversal(tmp_path: Path):
    tree = _build_fixture(tmp_path)
    day = V1.DISCOVERY_DATES[0]
    status_path = Path(tree["by_day"][day]["status"])  # type: ignore[index]
    tagged_index = Path(tree["by_day"][day]["tagged_index"])  # type: ignore[index]
    status = json.loads(status_path.read_bytes())
    status["tagged_index"] = str(
        tagged_index.parent / ".." / f"date={day}" / tagged_index.name
    )
    _write_json(status_path, status)
    with pytest.raises(RealAuthorityError, match="PATH_TRAVERSAL"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


def test_rejects_source_parent_drift(tmp_path: Path):
    tree = _build_fixture(tmp_path)
    logical_key = next(
        iter(V1.required_btc_fact_keys(V1.DISCOVERY_DATES[0]).values())
    )
    parent = Path(tree["content_root"]) / logical_key
    parent.write_bytes(parent.read_bytes() + b"tamper")
    with pytest.raises(V2.Stage2PreparationV2Error, match="LOCAL_SOURCE_DRIFT"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
        )


@pytest.mark.parametrize("forbidden", ("2026-07-23", "2026-07-26"))
def test_forbidden_dates_cannot_enter_builder(
    tmp_path: Path,
    forbidden: str,
):
    tree = _build_fixture(tmp_path)
    with pytest.raises(RealAuthorityError, match="FORBIDDEN_DATE"):
        build_real_source_receipts(
            live_root=tree["live_root"],
            content_root=tree["content_root"],
            dates=(*V1.DISCOVERY_DATES, forbidden),
        )
