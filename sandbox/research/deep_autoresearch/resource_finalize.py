#!/usr/bin/env python3
"""Finalize W09 mission resource/cost accounting after sequential stages."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            target = Path(root) / name
            try:
                if not target.is_symlink():
                    total += target.stat().st_size
            except FileNotFoundError:
                pass
    return total


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--driver-start-epoch", type=float, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    usage_path = run_dir / "RESOURCE_USAGE.json"
    manifest_path = run_dir / "RUN_MANIFEST.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    elapsed = max(0.0, time.time() - args.driver_start_epoch)
    rate = 0.4713
    contract_path = Path("/etc/w09/cost-contract.json")
    contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.is_file() else None
    usage.update({
        "finalized_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "w09_driver_wall_seconds": round(elapsed, 3),
        "estimated_total_compute_cost_usd": round(elapsed / 3600.0 * rate, 6),
        "measurement_boundary": (
            "All gated analysis stages through resource finalization; lightweight final "
            "report generation, artifact sync, and shutdown observation occur afterward."
        ),
        "actual_billed_cost_usd": None,
        "actual_cost_note": "Cloud billing telemetry is not available on the read-only runner; estimate uses the approved cost contract.",
        "cost_rate_usd_per_hour": rate,
        "cost_contract": contract,
        "selected_release_cache_bytes": sum(
            int(item.get("byte_count", 0)) for item in manifest.get("selected_releases", [])
        ),
        "run_artifact_bytes_at_finalize": tree_bytes(run_dir),
        "s3_bytes_read_total": None,
        "s3_bytes_note": "research_data verify did not expose a transfer counter; analysis stages read only the pre-existing local verified cache.",
        "idle_shutdown_contract_seconds": 1800,
        "shutdown_confirmation": "PENDING_MISSION_END",
    })
    atomic_json(usage_path, usage)
    manifest["resource_usage"] = {
        "path": "RESOURCE_USAGE.json",
        "sha256": sha256(usage_path),
        "estimated_total_compute_cost_usd": usage["estimated_total_compute_cost_usd"],
    }
    atomic_json(manifest_path, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
