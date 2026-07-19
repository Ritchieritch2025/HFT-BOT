#!/usr/bin/env python3
"""Prepare one immutable, explicit-release Deep03 D3-W2A V3 run directory."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

from deep03_v3_common import (
    Deep03InputError,
    MODE,
    SCHEMA_PREPARE,
    atomic_write_json,
    build_input_manifest,
    input_projection_sha256,
    load_authority_context,
    refuse_credential_environment,
    sha256_file,
    source_hashes,
    utc_now,
    validate_explicit_releases,
    validate_run_id,
    write_authority_bundle,
)


def prepare_run(
    *,
    cache_root: Path,
    run_root: Path,
    run_id: str,
    release_ids: list[str],
    authority_path: Path,
    arm_path: Path,
    arm_claim_root: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    audit_path: Path,
    w0_release_path: Path,
    w1_release_path: Path,
    w1_complete_path: Path,
    expected_owner_uid: int = 0,
    authority_now: dt.datetime | None = None,
    claim_invocation_id: str | None = None,
    claim_proc_cgroup_path: Path | None = None,
) -> Path:
    refuse_credential_environment()
    validate_run_id(run_id)
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
    if release_ids != authority_context["binding"]["authorized_input_release_ids"]:
        raise Deep03InputError(
            "prepare release IDs differ from exact-release authority"
        )
    run_root = Path(run_root).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    destination = run_root / run_id
    if destination.exists():
        raise Deep03InputError(f"run directory already exists: {destination}")
    lock_path = run_root / f".{run_id}.prepare.lock"
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Deep03InputError(f"another prepare owns run_id {run_id}") from exc
    stage = run_root / f".{run_id}.preparing.{os.getpid()}"
    try:
        if stage.exists():
            raise Deep03InputError(f"prepare staging path already exists: {stage}")
        stage.mkdir(mode=0o750)
        try:
            if destination.exists():
                raise Deep03InputError(f"run directory already exists: {destination}")
            releases = validate_explicit_releases(Path(cache_root), release_ids)
            manifest = build_input_manifest(
                run_id=run_id,
                cache_root=Path(cache_root),
                release_records=releases,
                authority_context=authority_context,
            )
            write_authority_bundle(stage, authority_context)
            input_path = stage / "INPUT_MANIFEST.json"
            atomic_write_json(input_path, manifest, exclusive=True)
            receipt = {
                "schema_version": SCHEMA_PREPARE,
                "run_id": run_id,
                "prepared_at_utc": utc_now(),
                "state": "PREPARED_EXACT_V3_OPEN_DISCOVERY",
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "input_manifest_sha256": sha256_file(input_path),
                "input_projection_sha256": input_projection_sha256(manifest),
                "source_modules_sha256": source_hashes(),
                "authority_binding": authority_context["binding"],
                "authority_artifact_sha256s": authority_context[
                    "artifact_sha256s"
                ],
                "release_ids": list(release_ids),
                "release_count": len(releases),
                "rfq_objects": 0,
                "selection_mode": "EXPLICIT_RELEASE_IDS_ONLY",
                "network_reads": 0,
                "production_mutations": 0,
                "order_actions": 0,
            }
            atomic_write_json(stage / "PREPARE_RECEIPT.json", receipt, exclusive=True)
            os.replace(stage, destination)
            dirfd = os.open(run_root, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    finally:
        os.close(lock_fd)
        if lock_path.exists():
            lock_path.unlink()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--arm-file", required=True, type=Path)
    parser.add_argument("--arm-claim-root", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runtime-commit", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--w0-release", required=True, type=Path)
    parser.add_argument("--w1-release", required=True, type=Path)
    parser.add_argument("--w1-complete", required=True, type=Path)
    parser.add_argument(
        "--release",
        action="append",
        required=True,
        dest="releases",
        help="explicit V3 release_id; repeat for multiple dates (no latest selector)",
    )
    args = parser.parse_args(argv)
    try:
        destination = prepare_run(
            cache_root=args.cache,
            run_root=args.run_root,
            run_id=args.run_id,
            release_ids=args.releases,
            authority_path=args.authority,
            arm_path=args.arm_file,
            arm_claim_root=args.arm_claim_root,
            plan_path=args.plan,
            runtime_commit_path=args.runtime_commit,
            audit_path=args.audit,
            w0_release_path=args.w0_release,
            w1_release_path=args.w1_release,
            w1_complete_path=args.w1_complete,
        )
    except Deep03InputError as exc:
        print(f"D3_W2A_PREPARE_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"D3_W2A_PREPARE_PASS run_dir={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
