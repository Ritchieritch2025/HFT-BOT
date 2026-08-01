#!/usr/bin/env python3
"""Offline verifier for the post-fill fee precision-boundary addendum v2."""

from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import statistics


REPORT_SHA256 = (
    "ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8"
)
EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
OLD_FEE_SOURCE_SHA256 = (
    "81c35b2982db9d892fb2c97d55f572681d5a60631b6367023e9330cdfc0feb5b"
)
EXECUTABLE_POLICIES = (
    "IOC_60MS",
    "WAIT_0P25",
    "WAIT_0P5",
    "WAIT_1",
    "WAIT_2",
    "WAIT_5",
    "WAIT_10",
    "WAIT_30",
    "WAIT_60",
    "CURRENT_DIST2_TTL60",
)


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--episodes", required=True)
    parser.add_argument("--old-fee-source", required=True)
    parser.add_argument("--addendum-json", required=True)
    args = parser.parse_args()

    assert sha256_path(args.report) == REPORT_SHA256
    assert sha256_path(args.episodes) == EPISODES_SHA256
    assert sha256_path(args.old_fee_source) == OLD_FEE_SOURCE_SHA256

    report = json.loads(Path(args.report).read_text())
    with gzip.open(args.episodes, "rt", encoding="utf-8") as handle:
        episodes = json.load(handle)
    addendum = json.loads(Path(args.addendum_json).read_text())

    assert report["date_2026_07_23_read"] is False
    assert episodes["date_2026_07_23_read"] is False
    assert addendum["date_2026_07_23_read"] is False
    assert addendum["candidate"] is False
    assert addendum["deployable"] is False
    assert addendum["live_authorized"] is False
    assert (
        addendum["classification"]
        == "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
    )
    assert (
        addendum["identification"]["current_account_exact_point_identified"]
        is False
    )

    by_policy = defaultdict(list)
    for row in episodes["policy_rows"]:
        by_policy[row["policy"]].append(row)
    recorded = addendum["strict_upper_bound"]["policies"]
    assert set(by_policy) == set(recorded)
    for policy, rows in by_policy.items():
        old_exact = statistics.fmean(float(row["pnl_c"]) for row in rows)
        ioc_rows = sum(row["exit_kind"] == "ioc" for row in rows)
        strict_upper = old_exact + ioc_rows / len(rows) * 0.99
        assert old_exact == recorded[policy]["old_exact_ev_c"]
        assert ioc_rows == recorded[policy]["ioc_rows"]
        assert strict_upper == recorded[policy]["strict_ev_upper_c"]
    assert all(
        recorded[policy]["strict_ev_upper_c"] < 0
        for policy in EXECUTABLE_POLICIES
    )
    assert (
        addendum["strict_upper_bound"][
            "all_executable_fixed_policy_upper_bounds_negative"
        ]
        is True
    )

    print(
        json.dumps(
            {
                "status": "PASS",
                "report_sha256": REPORT_SHA256,
                "episodes_sha256": EPISODES_SHA256,
                "old_fee_source_sha256": OLD_FEE_SOURCE_SHA256,
                "addendum_json_sha256": sha256_path(args.addendum_json),
                "policy_count": len(by_policy),
                "all_executable_strict_upper_bounds_negative": True,
                "current_account_exact_point_identified": False,
                "date_2026_07_23_read": False,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
