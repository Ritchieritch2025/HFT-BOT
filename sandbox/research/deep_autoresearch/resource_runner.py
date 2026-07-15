#!/usr/bin/env python3
"""Run one W09 research stage while recording bounded resource evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def tree_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
    return total


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def process_tree_rss_kib(pid: int) -> int:
    """Best-effort instantaneous Linux RSS for a process and its descendants."""
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            for line in Path(f"/proc/{current}/status").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
            children = Path(f"/proc/{current}/task/{current}/children")
            if children.is_file():
                pending.extend(int(value) for value in children.read_text().split())
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return total


def execute(run_dir: Path, label: str, command: list[str], poll_seconds: float) -> int:
    if not command:
        raise ValueError("missing command after --")
    run_dir = run_dir.resolve()
    temp_dir = run_dir / "tmp"
    db_dir = run_dir / "cache"
    started_at = utc_now()
    started = time.monotonic()
    disk_before = shutil.disk_usage(run_dir)
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_temp_bytes = tree_bytes(temp_dir)
    peak_stage_cache_bytes = tree_bytes(db_dir)
    minimum_disk_free_bytes = disk_before.free
    peak_process_tree_rss_kib = 0
    samples = 0
    process = subprocess.Popen(command)
    while True:
        peak_temp_bytes = max(peak_temp_bytes, tree_bytes(temp_dir))
        peak_stage_cache_bytes = max(peak_stage_cache_bytes, tree_bytes(db_dir))
        minimum_disk_free_bytes = min(minimum_disk_free_bytes, shutil.disk_usage(run_dir).free)
        peak_process_tree_rss_kib = max(
            peak_process_tree_rss_kib, process_tree_rss_kib(process.pid)
        )
        samples += 1
        try:
            return_code = process.wait(timeout=poll_seconds)
            break
        except subprocess.TimeoutExpired:
            pass
    peak_temp_bytes = max(peak_temp_bytes, tree_bytes(temp_dir))
    peak_stage_cache_bytes = max(peak_stage_cache_bytes, tree_bytes(db_dir))
    disk_after = shutil.disk_usage(run_dir)
    minimum_disk_free_bytes = min(minimum_disk_free_bytes, disk_after.free)
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_seconds = time.monotonic() - started
    hourly_cost = 0.4713
    result = {
        "schema_version": "w09-stage-resource-v1",
        "label": label,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "command": command,
        "return_code": return_code,
        "wall_seconds": round(wall_seconds, 3),
        "cpu_user_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 3),
        "cpu_system_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 3),
        "cpu_hours": round(
            (usage_after.ru_utime - usage_before.ru_utime
             + usage_after.ru_stime - usage_before.ru_stime) / 3600.0, 6
        ),
        "peak_process_tree_rss_kib_polled": peak_process_tree_rss_kib,
        "cumulative_children_max_rss_kib": int(usage_after.ru_maxrss),
        "rss_note": "Process-tree RSS is sampled and may miss sub-poll peaks; cumulative_children_max_rss_kib is an upper-bound cross-stage diagnostic, not stage-specific.",
        "peak_temp_bytes_polled": peak_temp_bytes,
        "peak_stage_cache_bytes_polled": peak_stage_cache_bytes,
        "disk_free_before_bytes": disk_before.free,
        "minimum_disk_free_bytes_polled": minimum_disk_free_bytes,
        "disk_free_after_bytes": disk_after.free,
        "poll_seconds": poll_seconds,
        "poll_samples": samples,
        "estimated_compute_cost_usd": round(wall_seconds / 3600.0 * hourly_cost, 6),
        "cost_rate_usd_per_hour": hourly_cost,
        "s3_bytes_read_by_analysis": 0,
        "s3_note": "Analysis reads the already verified local immutable cache; gate verification is tracked separately and exposes no per-command S3 byte counter.",
    }
    stage_path = run_dir / "logs" / "resources" / f"{label}.json"
    atomic_json(stage_path, result)
    usage_path = run_dir / "RESOURCE_USAGE.json"
    if usage_path.is_file():
        aggregate = json.loads(usage_path.read_text(encoding="utf-8"))
    else:
        aggregate = {"schema_version": "sports-autoresearch-resource-v1", "stages": []}
    aggregate["stages"] = [row for row in aggregate.get("stages", [])
                           if row.get("label") != label] + [result]
    aggregate["stages"].sort(key=lambda row: row["label"])
    aggregate["stage_wall_seconds"] = round(sum(row["wall_seconds"] for row in aggregate["stages"]), 3)
    aggregate["cpu_hours"] = round(sum(row["cpu_hours"] for row in aggregate["stages"]), 6)
    aggregate["estimated_stage_compute_cost_usd"] = round(
        sum(row["estimated_compute_cost_usd"] for row in aggregate["stages"]), 6
    )
    aggregate["failed_or_retried_tasks"] = [
        {"label": row["label"], "return_code": row["return_code"]}
        for row in aggregate["stages"] if row["return_code"] != 0
    ]
    atomic_json(usage_path, aggregate)
    return return_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.poll_seconds <= 0:
        parser.error("poll seconds must be positive")
    try:
        return execute(args.run_dir, args.label, command, args.poll_seconds)
    except (OSError, ValueError) as exc:
        print(f"RESOURCE_RUNNER_BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
