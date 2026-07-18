#!/usr/bin/env python3
"""Build an immutable, pre-authority D3-W1 OPEN_DISCOVERY evidence bundle.

The tool has no network client and performs no research calculation.  It
revalidates already materialized exact-VersionId V3 releases and their already
completed exploratory DuckDB canaries, then records the all-prior-exposed,
no-holdout split that a later exact D3-W2A authority must bind.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from deep03_v3_common import (
    Deep03InputError,
    MODE,
    atomic_write_bytes,
    atomic_write_json,
    build_input_manifest,
    canonical_json_bytes,
    input_projection_sha256,
    load_json,
    refuse_credential_environment,
    sha256_file,
    utc_now,
    validate_explicit_releases,
)


SCHEMA_W0 = "deep03-w0-release-v1"
SCHEMA_W1 = "deep03-w1-release-v1"
SCHEMA_DQ = "deep03-w1-data-quality-receipt-v1"
SCHEMA_PRIOR = "deep03-w1-prior-exposure-ledger-v1"
SCHEMA_SPLIT = "deep03-w1-open-discovery-split-v1"
SCHEMA_COMPLETE = "deep03-w1-exploratory-precheck-completion-v1"
CANARY_STATE = "W09_V3_EXPLORATORY_QUERY_CANARY_PASS"


def _exact_file(path: Path, expected_sha256: str, label: str) -> bytes:
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise Deep03InputError(f"{label} expected SHA-256 is invalid")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise Deep03InputError(f"{label} is unreadable: {path}: {exc}") from exc
    if not raw or sha256_file(Path(path)) != expected_sha256:
        raise Deep03InputError(f"{label} SHA-256 mismatch")
    return raw


def _release_doc(
    path: Path,
    *,
    expected_sha256: str,
    schema: str,
    release_id: str,
    label: str,
) -> dict[str, Any]:
    _exact_file(path, expected_sha256, label)
    value = load_json(path, label)
    if value.get("schema_version") != schema or value.get("release_id") != release_id:
        raise Deep03InputError(f"{label} identity mismatch")
    if value.get("state") != "RELEASE_CANDIDATE":
        raise Deep03InputError(f"{label} must be a RELEASE_CANDIDATE")
    if value.get("research_execution_authority") is not False:
        raise Deep03InputError(f"{label} must grant no research authority")
    return value


def _canary_records(
    canary_paths: list[Path], release_records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    expected = {row["release_id"]: row for row in release_records}
    if len(canary_paths) != len(expected):
        raise Deep03InputError("one exploratory canary is required per release")
    records: dict[str, dict[str, Any]] = {}
    for path in canary_paths:
        receipt = load_json(path, "exploratory canary")
        release_id = receipt.get("release_id")
        if not isinstance(release_id, str) or release_id not in expected:
            raise Deep03InputError("canary names an unauthorized release")
        if release_id in records:
            raise Deep03InputError("duplicate canary release")
        release = expected[release_id]
        fixed = {
            "state": CANARY_STATE,
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "rfq": "OFF",
            "evidence_tier": release["evidence_tier"],
            "date": release["date"],
        }
        for field, value in fixed.items():
            if receipt.get(field) != value:
                raise Deep03InputError(
                    f"canary contract mismatch: {release_id}/{field}"
                )
        table_count = receipt.get("table_count")
        if not isinstance(table_count, int) or isinstance(table_count, bool) or table_count < 1:
            raise Deep03InputError(f"canary has no queryable facts table: {release_id}")
        records[release_id] = {
            "release_id": release_id,
            "date": release["date"],
            "path": str(Path(path).resolve()),
            "sha256": sha256_file(path),
            "state": receipt["state"],
            "evidence_tier": receipt["evidence_tier"],
            "table_count": table_count,
            "strict_acceptance_claimed": False,
            "rfq": "OFF",
        }
    return [records[row["release_id"]] for row in release_records]


def build_preflight(
    *,
    cache_root: Path,
    output_dir: Path,
    plan_path: Path,
    plan_sha256: str,
    audit_path: Path,
    audit_sha256: str,
    w0_release_path: Path,
    w0_release_sha256: str,
    w0_release_id: str,
    w1_release_path: Path,
    w1_release_sha256: str,
    w1_release_id: str,
    release_ids: list[str],
    canary_paths: list[Path],
) -> Path:
    refuse_credential_environment()
    _exact_file(plan_path, plan_sha256, "adopted plan")
    _exact_file(audit_path, audit_sha256, "independent audit")
    w0 = _release_doc(
        w0_release_path,
        expected_sha256=w0_release_sha256,
        schema=SCHEMA_W0,
        release_id=w0_release_id,
        label="D3-W0 release",
    )
    w1 = _release_doc(
        w1_release_path,
        expected_sha256=w1_release_sha256,
        schema=SCHEMA_W1,
        release_id=w1_release_id,
        label="D3-W1 release",
    )
    if w0.get("adopted_plan_sha256") != plan_sha256 or w0.get("audit_sha256") != audit_sha256:
        raise Deep03InputError("D3-W0 release does not bind plan/audit")
    if (
        w1.get("w0_release_id") != w0_release_id
        or w1.get("w0_release_sha256") != w0_release_sha256
        or w1.get("adopted_plan_sha256") != plan_sha256
        or w1.get("audit_sha256") != audit_sha256
        or w1.get("mode") != MODE
        or w1.get("authorized_input_release_ids") != release_ids
        or w1.get("rfq_included") is not False
        or w1.get("strict_acceptance_claimed") is not False
        or w1.get("holdout_opened") is not False
    ):
        raise Deep03InputError("D3-W1 release scope/binding mismatch")

    releases = validate_explicit_releases(cache_root, release_ids)
    dates = [row["date"] for row in releases]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise Deep03InputError("W1 releases must be date-sorted and unique")
    if dates != w1.get("authorized_input_dates"):
        raise Deep03InputError("W1 release date set differs from exact manifests")
    canaries = _canary_records(canary_paths, releases)
    input_manifest = build_input_manifest(
        run_id=w1_release_id,
        cache_root=cache_root,
        release_records=releases,
    )

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise Deep03InputError(f"W1 output already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = output_dir.parent / f".{output_dir.name}.preparing.{os.getpid()}"
    if stage.exists():
        raise Deep03InputError(f"W1 staging path already exists: {stage}")
    stage.mkdir(mode=0o750)
    try:
        input_path = stage / "INPUT_MANIFEST.json"
        atomic_write_json(input_path, input_manifest, exclusive=True)
        tier_counts: dict[str, int] = {}
        for row in releases:
            tier = row["evidence_tier"]
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        dq = {
            "schema_version": SCHEMA_DQ,
            "state": "W1_DQ_PASS_FOR_EXPLORATORY_ONLY",
            "generated_at_utc": utc_now(),
            "mode": MODE,
            "strict_acceptance_claimed": False,
            "strict_canary_expected_state": "REFUSED_UNLESS_SEALED_CONFIRMATION",
            "release_ids": release_ids,
            "release_dates": dates,
            "release_count": len(releases),
            "object_count": input_manifest["object_count"],
            "object_bytes": input_manifest["object_bytes"],
            "version_binding_mode": "CANONICAL_REFERENCE",
            "exact_version_local_verification": "PASS",
            "evidence_tier_counts": tier_counts,
            "all_inputs_prior_exposed": True,
            "holdout_opened": False,
            "rfq": "OFF_AND_ABSENT",
            "canaries": canaries,
            "limitations": [
                "EXPLORATORY_ONLY",
                "PRIOR_EXPOSED",
                "NO_STRICT_ACCEPTANCE",
                "NO_CONFIRMATION_OR_CANDIDATE_CLAIM",
            ],
        }
        dq_path = stage / "DATA_QUALITY_RECEIPT.json"
        atomic_write_json(dq_path, dq, exclusive=True)

        prior_path = stage / "PRIOR_EXPOSURE_LEDGER.jsonl"
        prior_rows = [
            {
                "schema_version": SCHEMA_PRIOR,
                "release_id": row["release_id"],
                "date": row["date"],
                "exposure_class": "PRIOR_EXPOSED",
                "allowed_stage": "OPEN_DISCOVERY",
                "validation_eligible": False,
                "confirmation_eligible": False,
                "evidence_tier": row["evidence_tier"],
                "rfq": "OFF",
            }
            for row in releases
        ]
        atomic_write_bytes(
            prior_path,
            b"".join(canonical_json_bytes(row) for row in prior_rows),
            exclusive=True,
        )
        split = {
            "schema_version": SCHEMA_SPLIT,
            "state": "NO_HOLDOUT_ALL_PRIOR_EXPOSED",
            "mode": MODE,
            "open_discovery_release_ids": release_ids,
            "open_discovery_dates": dates,
            "train_release_ids": [],
            "validation_release_ids": [],
            "confirmation_release_ids": [],
            "holdout_opened": False,
            "split_salt": None,
            "strict_acceptance_claimed": False,
        }
        split_path = stage / "SPLIT_MANIFEST_OPEN_DISCOVERY.json"
        atomic_write_json(split_path, split, exclusive=True)

        artifacts = {
            "INPUT_MANIFEST.json": sha256_file(input_path),
            "DATA_QUALITY_RECEIPT.json": sha256_file(dq_path),
            "PRIOR_EXPOSURE_LEDGER.jsonl": sha256_file(prior_path),
            "SPLIT_MANIFEST_OPEN_DISCOVERY.json": sha256_file(split_path),
        }
        complete = {
            "schema_version": SCHEMA_COMPLETE,
            "state": "W1_COMPLETE_EXPLORATORY_PRECHECK",
            "completed_at_utc": utc_now(),
            "mode": MODE,
            "w0_release_id": w0_release_id,
            "w0_release_sha256": w0_release_sha256,
            "w1_release_id": w1_release_id,
            "w1_release_sha256": w1_release_sha256,
            "adopted_plan_sha256": plan_sha256,
            "audit_sha256": audit_sha256,
            "authorized_input_release_ids": release_ids,
            "input_projection_sha256": input_projection_sha256(input_manifest),
            "artifacts_sha256": artifacts,
            "release_count": len(releases),
            "object_count": input_manifest["object_count"],
            "object_bytes": input_manifest["object_bytes"],
            "all_inputs_prior_exposed": True,
            "holdout_opened": False,
            "strict_acceptance_claimed": False,
            "research_execution_started": False,
            "rfq": "OFF_AND_ABSENT",
            "network_reads_during_preflight": 0,
            "s3_writes": 0,
        }
        atomic_write_json(stage / "W1_COMPLETE.json", complete, exclusive=True)
        os.replace(stage, output_dir)
        dirfd = os.open(output_dir.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return output_dir / "W1_COMPLETE.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--audit-sha256", required=True)
    parser.add_argument("--w0-release", required=True, type=Path)
    parser.add_argument("--w0-release-sha256", required=True)
    parser.add_argument("--w0-release-id", required=True)
    parser.add_argument("--w1-release", required=True, type=Path)
    parser.add_argument("--w1-release-sha256", required=True)
    parser.add_argument("--w1-release-id", required=True)
    parser.add_argument("--release", action="append", required=True, dest="releases")
    parser.add_argument("--canary", action="append", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        complete = build_preflight(
            cache_root=args.cache,
            output_dir=args.output_dir,
            plan_path=args.plan,
            plan_sha256=args.plan_sha256,
            audit_path=args.audit,
            audit_sha256=args.audit_sha256,
            w0_release_path=args.w0_release,
            w0_release_sha256=args.w0_release_sha256,
            w0_release_id=args.w0_release_id,
            w1_release_path=args.w1_release,
            w1_release_sha256=args.w1_release_sha256,
            w1_release_id=args.w1_release_id,
            release_ids=args.releases,
            canary_paths=args.canary,
        )
    except (Deep03InputError, OSError, ValueError) as exc:
        print(f"D3_W1_PREFLIGHT_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"D3_W1_PREFLIGHT_PASS completion={complete}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
