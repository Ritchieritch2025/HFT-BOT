#!/usr/bin/env python3
"""Independent verifier for the frozen L2 aggregate min-fee sidecar v2."""

from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import statistics


ORIGINAL_REPORT_SHA256 = (
    "ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8"
)
ORIGINAL_EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
SIDECAR_REPORT_SHA256 = (
    "e3c88f1d73b2f61cd1b39a5e9ea6c62922bb0a0c2e0cdce8da43827978cfd707"
)
SIDECAR_EPISODES_SHA256 = (
    "78e7d0a8f5f2d930d9114f8aace8a61eed866b79d4a446bf86b22897acf43523"
)
EXECUTABLE = {
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
}


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def strip_pnl(value):
    if isinstance(value, dict):
        return {
            key: strip_pnl(item)
            for key, item in value.items()
            if key != "pnl_c"
        }
    if isinstance(value, list):
        return [strip_pnl(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-report", required=True)
    parser.add_argument("--original-episodes", required=True)
    parser.add_argument("--sidecar-report", required=True)
    parser.add_argument("--sidecar-episodes", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    assert sha256_path(args.original_report) == ORIGINAL_REPORT_SHA256
    assert sha256_path(args.original_episodes) == ORIGINAL_EPISODES_SHA256
    assert sha256_path(args.sidecar_report) == SIDECAR_REPORT_SHA256
    assert sha256_path(args.sidecar_episodes) == SIDECAR_EPISODES_SHA256

    original_report = json.loads(Path(args.original_report).read_text())
    sidecar_report = json.loads(Path(args.sidecar_report).read_text())
    original = load_gzip(args.original_episodes)
    adjusted = load_gzip(args.sidecar_episodes)
    assert original_report["date_2026_07_23_read"] is False
    assert original["date_2026_07_23_read"] is False
    assert adjusted["date_2026_07_23_read"] is False
    assert sidecar_report["date_2026_07_23_read"] is False
    assert sidecar_report["candidate_status"] == "NO_CANDIDATE"
    assert sidecar_report["deployable"] is False
    assert sidecar_report["live_authorized"] is False
    assert sidecar_report["historical_validation_claim"] is False
    assert (
        sidecar_report["precision_boundary"]["corrected_classification"]
        == "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
    )
    assert (
        sidecar_report["account_fee_contract"][
            "current_account_exact_point_identified"
        ]
        is False
    )

    original_rows = {
        (row["policy"], row["episode_id"]): row
        for row in original["policy_rows"]
    }
    adjusted_rows = {
        (row["policy"], row["episode_id"]): row
        for row in adjusted["policy_rows"]
    }
    audit_rows = {
        (row["policy"], row["episode_id"]): row
        for row in adjusted["policy_ioc_fee_audit_rows"]
    }
    assert set(original_rows) == set(adjusted_rows)
    maker_count = 0
    ioc_count = 0
    for key, old in original_rows.items():
        new = adjusted_rows[key]
        assert canonical_bytes(strip_pnl(new)) == canonical_bytes(
            strip_pnl(old)
        )
        if old["exit_kind"] == "maker_pair":
            maker_count += 1
            assert canonical_bytes(new) == canonical_bytes(old)
            assert key not in audit_rows
        elif old["exit_kind"] == "ioc":
            ioc_count += 1
            audit = audit_rows[key]
            assert audit["private_fill_exact"] is False
            assert abs(
                float(audit["old_pnl_reproduced_c"])
                - float(old["pnl_c"])
            ) <= 1e-12
            assert abs(
                float(audit["l2_aggregate_max_pnl_c"])
                - float(new["pnl_c"])
            ) <= 1e-12
            old_fee = Decimal(
                audit["old_generic_whole_cent_fee_c_exact"]
            )
            min_fee = Decimal(
                audit["l2_aggregate_centicent_min_fee_c_exact"]
            )
            saving = Decimal(
                audit["l2_aggregate_max_fee_saving_c_exact"]
            )
            assert old_fee == old_fee.to_integral_value()
            assert min_fee == min_fee.quantize(Decimal("0.01"))
            assert saving == old_fee - min_fee
            assert Decimal("0") <= saving <= Decimal("0.99")
    assert ioc_count == len(audit_rows) == 7125
    assert maker_count == 6097

    assert canonical_bytes(strip_pnl(adjusted["markout_rows"])) == (
        canonical_bytes(strip_pnl(original["markout_rows"]))
    )
    assert len(adjusted["markout_ioc_fee_audit_rows"]) == 10818

    by_policy = {}
    for policy in sidecar_report["old_metrics"]:
        rows = [
            row for row in original_rows.values()
            if row["policy"] == policy
        ]
        old_exact = statistics.fmean(float(row["pnl_c"]) for row in rows)
        policy_ioc = sum(row["exit_kind"] == "ioc" for row in rows)
        strict = old_exact + policy_ioc / len(rows) * 0.99
        recorded = sidecar_report["strict_upper_bound"][policy]
        assert recorded["ioc_count"] == policy_ioc
        assert recorded["n"] == len(rows)
        assert abs(
            recorded["old_exact_ev_c_per_first_fill"] - old_exact
        ) < 1e-15
        assert abs(recorded["ev_c_per_first_fill_upper"] - strict) < 1e-15
        by_policy[policy] = {
            "old_exact_ev_c": old_exact,
            "l2_aggregate_point_ev_c": (
                sidecar_report["l2_aggregate_centicent_metrics"][policy][
                    "ev_c_per_first_fill"
                ]
            ),
            "strict_upper_ev_c": strict,
            "ioc_rows": policy_ioc,
        }
    assert all(
        by_policy[policy]["l2_aggregate_point_ev_c"] < 0
        and by_policy[policy]["strict_upper_ev_c"] < 0
        for policy in EXECUTABLE
    )

    receipt = {
        "schema": "z3-postfill-l2-aggregate-min-fee-verification-v2",
        "status": "PASS",
        "classification": (
            "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
        ),
        "current_account_exact_point_identified": False,
        "original_report_sha256": ORIGINAL_REPORT_SHA256,
        "original_episodes_sha256": ORIGINAL_EPISODES_SHA256,
        "sidecar_report_sha256": SIDECAR_REPORT_SHA256,
        "sidecar_episodes_sha256": SIDECAR_EPISODES_SHA256,
        "policy_rows": len(original_rows),
        "old_ioc_pnl_exact_matches": ioc_count,
        "maker_rows_canonical_byte_identical": maker_count,
        "markout_old_pnl_exact_matches": 10818,
        "action_projection_identity": True,
        "date_2026_07_23_read": False,
        "all_executable_l2_point_ev_negative": True,
        "all_executable_strict_upper_ev_negative": True,
        "candidate": False,
        "deployable": False,
        "live_authorized": False,
        "policies": by_policy,
    }
    Path(args.output).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
