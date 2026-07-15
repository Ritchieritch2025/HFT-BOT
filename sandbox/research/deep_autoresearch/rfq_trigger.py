#!/usr/bin/env python3
"""Reproduce prior Cycle-0 RFQ anomaly triggers before dependent tests."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


BANNER = "EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION"
RELEASE_IDS = (
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5",
)


def discover_manifest_bound_paths(cache_root: Path) -> tuple[list[Path], list[dict]]:
    """Select exact manifest RFQ objects and count byte-identical overlaps once."""
    paths = []
    overlaps = []
    seen: dict[str, dict] = {}
    for release_id in RELEASE_IDS:
        base = cache_root / "releases" / release_id
        manifest_path = base / "MANIFEST.json"
        verified_path = base / ".VERIFIED.json"
        if not manifest_path.is_file() or not verified_path.is_file():
            raise ValueError(f"release not locally verified: {release_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verified = json.loads(verified_path.read_text(encoding="utf-8"))
        if (manifest.get("release_id") != release_id or
                verified.get("release_id") != release_id or
                verified.get("version_binding_mode") != "VERSION_BOUND"):
            raise ValueError(f"release identity/binding mismatch: {release_id}")
        for obj in sorted(manifest["objects"], key=lambda item: item["key"]):
            key = obj["key"]
            if not key.startswith("raw_rfq/") or ".ndjson" not in Path(key).name:
                continue
            path = base / key
            if not path.is_file():
                raise ValueError(f"manifest RFQ object missing: {release_id}:{key}")
            if key in seen:
                if seen[key].get("sha256") != obj.get("sha256"):
                    raise ValueError(f"overlapping RFQ key changed bytes: {key}")
                overlaps.append({"key": key, "kept_release_id": seen[key]["release_id"],
                                 "skipped_release_id": release_id,
                                 "sha256": obj.get("sha256")})
                continue
            seen[key] = {"release_id": release_id, "sha256": obj.get("sha256")}
            paths.append(path)
    return paths, overlaps


def fixed(value, places: int):
    if value in (None, ""):
        return None
    try:
        scaled = Decimal(str(value)) * (Decimal(10) ** places)
    except (InvalidOperation, ValueError):
        return None
    integral = scaled.to_integral_value()
    return int(integral) if scaled == integral else None


def kaplan_meier(durations: list[float], observed: list[bool]) -> list[tuple[float, float, int, int]]:
    """Return (time, survival, deaths, censored) at each distinct time."""
    if len(durations) != len(observed):
        raise ValueError("duration/observed length mismatch")
    rows = sorted((float(t), bool(event)) for t, event in zip(durations, observed)
                  if t >= 0)
    at_risk = len(rows)
    survival = 1.0
    out = []
    i = 0
    while i < len(rows):
        time_value = rows[i][0]
        deaths = censored = 0
        while i < len(rows) and rows[i][0] == time_value:
            if rows[i][1]:
                deaths += 1
            else:
                censored += 1
            i += 1
        if at_risk and deaths:
            survival *= 1.0 - deaths / at_risk
        out.append((time_value, survival, deaths, censored))
        at_risk -= deaths + censored
    return out


def downsample_ccdf(values: list[int], points: int = 2000) -> tuple[list[float], list[float]]:
    xs = sorted(float(v) for v in values if v and v > 0)
    if not xs:
        return [], []
    unique = []
    first_indices = []
    for index, value in enumerate(xs):
        if not unique or value != unique[-1]:
            unique.append(value)
            first_indices.append(index)
    if len(unique) <= points:
        indices = list(range(len(unique)))
    else:
        indices = sorted({round(i * (len(unique) - 1) / (points - 1)) for i in range(points)})
    return ([unique[i] for i in indices],
            [(len(xs) - first_indices[i]) / len(xs) for i in indices])


def scan(paths: list[Path], max_rows: int) -> dict:
    rows = malformed = created_n = deleted_n = 0
    target_cost = []
    contracts = []
    lifecycle_events: list[tuple[int, str, str]] = []
    completed: list[float] = []
    first_wall = last_wall = None
    previous_wall = None
    input_wall_regressions = 0
    files_touched = []
    for path in paths:
        path_used = False
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if rows >= max_rows:
                    break
                if not line.strip():
                    continue
                rows += 1
                path_used = True
                try:
                    outer = json.loads(line)
                    wall = outer.get("recv_wall_ns")
                    raw = outer.get("raw")
                    if type(wall) is not int or not isinstance(raw, str):
                        continue
                    first_wall = wall if first_wall is None else min(first_wall, wall)
                    last_wall = wall if last_wall is None else max(last_wall, wall)
                    if previous_wall is not None and wall < previous_wall:
                        input_wall_regressions += 1
                    previous_wall = wall
                    frame = json.loads(raw)
                    typ = frame.get("type")
                    msg = frame.get("msg")
                    if typ not in ("rfq_created", "rfq_deleted") or not isinstance(msg, dict):
                        continue
                    rid = msg.get("id")
                    if typ == "rfq_created":
                        created_n += 1
                        if isinstance(rid, str) and rid:
                            lifecycle_events.append((wall, "create", rid))
                        value = fixed(msg.get("target_cost_dollars"), 6)
                        if value is not None and value >= 0:
                            target_cost.append(value)
                        value = fixed(msg.get("contracts_fp"), 2)
                        if value is not None and value >= 0:
                            contracts.append(value)
                    else:
                        deleted_n += 1
                        if isinstance(rid, str) and rid:
                            lifecycle_events.append((wall, "delete", rid))
                except (ValueError, TypeError, json.JSONDecodeError):
                    malformed += 1
            if rows >= max_rows:
                if path_used:
                    files_touched.append(str(path))
                break
        if path_used and (not files_touched or files_touched[-1] != str(path)):
            files_touched.append(str(path))
    creates: dict[str, int] = {}
    unmatched_deletes = repeated_creates = 0
    for wall, typ, rid in sorted(lifecycle_events, key=lambda item: (item[0], item[1], item[2])):
        if typ == "create":
            if rid in creates:
                repeated_creates += 1
            else:
                creates[rid] = wall
        elif rid in creates:
            completed.append((wall - creates.pop(rid)) / 1e9)
        else:
            unmatched_deletes += 1
    scan_end = last_wall or 0
    censored = [(scan_end - wall) / 1e9 for wall in creates.values() if scan_end >= wall]
    return {
        "rows": rows, "malformed": malformed, "created": created_n, "deleted": deleted_n,
        "target_cost_e6": target_cost, "contracts_e2": contracts,
        "completed_lifetimes_s": completed, "censored_lifetimes_s": censored,
        "first_recv_wall_ns": first_wall, "last_recv_wall_ns": last_wall,
        "files_touched": files_touched,
        "input_wall_order_regressions": input_wall_regressions,
        "lifecycle_sorted_by_receive_wall_before_join": True,
        "unmatched_deletes": unmatched_deletes,
        "repeated_creates_while_open": repeated_creates,
        "sampling_design": "LEXICOGRAPHIC_MANIFEST_PREFIX_SAMPLE",
        "sampling_limit_rows": max_rows,
    }


def render(result: dict, run_dir: Path, overlaps: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_dir = run_dir / "REPORT/charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 6))
    for key, label in (("target_cost_e6", "target_cost_e6"), ("contracts_e2", "contracts_e2")):
        x, y = downsample_ccdf(result[key])
        if x:
            ax.plot(x, y, label=f"{label} (n={len(result[key]):,})")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("Stored fixed-point magnitude")
    ax.set_ylabel("Empirical P(X ≥ x)")
    ax.set_title("RFQ size-tail trigger — 2m-row manifest-prefix reproduction\n" + BANNER)
    ax.legend(); ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(chart_dir / "C1-ANOM-RFQ-SIZE-TAIL-01__trigger_ccdf.png", dpi=150)
    plt.close(fig)
    durations = result["completed_lifetimes_s"] + result["censored_lifetimes_s"]
    observed = [True] * len(result["completed_lifetimes_s"]) + [False] * len(result["censored_lifetimes_s"])
    km = kaplan_meier(durations, observed)
    fig, ax = plt.subplots(figsize=(9, 6))
    if km:
        ax.step([r[0] for r in km], [r[1] for r in km], where="post")
    ax.set_xscale("log")
    ax.set_xlabel("RFQ age (seconds, log scale)")
    ax.set_ylabel("Kaplan–Meier survival")
    ax.set_title("RFQ lifecycle trigger — receive-sorted 2m-row prefix with right censoring\n" + BANNER)
    ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(chart_dir / "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01__trigger_survival.png", dpi=150)
    plt.close(fig)
    compact = {key: value for key, value in result.items()
               if key not in ("target_cost_e6", "contracts_e2", "completed_lifetimes_s", "censored_lifetimes_s")}
    compact.update({
        "target_cost_n": len(result["target_cost_e6"]),
        "contracts_n": len(result["contracts_e2"]),
        "completed_lifetime_n": len(result["completed_lifetimes_s"]),
        "right_censored_n": len(result["censored_lifetimes_s"]),
        "dependent_test_opened": False,
        "manifest_bound_input_objects": result.get("input_objects"),
        "overlapping_objects_deduplicated": len(overlaps),
        "overlap_keys_sha_identical": True,
        "status": "PRIOR_TRIGGER_REPRODUCED_BEFORE_DEPENDENT_TEST",
    })
    (run_dir / "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json").write_text(
        json.dumps(compact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=Path("/srv/w09-research/cache"))
    parser.add_argument("--max-rows", type=int, default=2_000_000)
    args = parser.parse_args()
    paths, overlaps = discover_manifest_bound_paths(args.cache_root)
    if not paths:
        raise SystemExit("RFQ inputs missing")
    result = scan(paths, args.max_rows)
    result["input_objects"] = len(paths)
    render(result, args.run_dir, overlaps)
    print("RFQ_TRIGGER_REPRODUCED", result["rows"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
