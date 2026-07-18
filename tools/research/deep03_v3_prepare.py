#!/usr/bin/env python3
"""Prepare one immutable, explicit-release Deep03 D3-W2A V3 run directory."""

from __future__ import annotations

import argparse
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
    refuse_credential_environment,
    sha256_file,
    source_hashes,
    utc_now,
    validate_explicit_releases,
    validate_run_id,
)


def prepare_run(
    *, cache_root: Path, run_root: Path, run_id: str, release_ids: list[str]
) -> Path:
    refuse_credential_environment()
    validate_run_id(run_id)
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
                run_id=run_id, cache_root=Path(cache_root), release_records=releases
            )
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
        )
    except Deep03InputError as exc:
        print(f"D3_W2A_PREPARE_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"D3_W2A_PREPARE_PASS run_dir={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
