#!/usr/bin/env python3
"""Fee-only direct-centicent repricing of the sealed post-fill replay.

Every strategy action, fill classification, held side, entry, exit timestamp,
and IOC book walk is frozen.  The causal book immediately before each frozen
IOC timer is reconstructed.  The generic whole-cent fee must reproduce every
sealed PnL exactly before only that fee is replaced by the authenticated
direct-account centicent formula.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import statistics
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, localcontext
from pathlib import Path

import duckdb


REPORT_PATH = os.environ.get(
    "Z3_POSTFILL_SEALED_REPORT",
    "/tmp/z3_postfill_receive_clock_sampled_a1skip_report_20260726T082350Z.json",
)
EPISODES_PATH = os.environ.get(
    "Z3_POSTFILL_SEALED_EPISODES",
    "/tmp/z3_postfill_receive_clock_sampled_a1skip_episodes_20260726T082350Z.json.gz",
)
FULL_CONSUMER = os.environ.get(
    "ROUND3_FULL_CONSUMER",
    "/tmp/z3_postfill_stopping_receive_clock.py",
)
PROBE_PATH = os.environ.get(
    "DIRECT_ACCOUNT_PROBE",
    "/tmp/position_value_contract_probe_result.json",
)
REPORT_SHA256 = (
    "ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8"
)
EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
FULL_CONSUMER_SHA256 = (
    "ee2e49cb011557e207bd0774f05573ca0f0b35563341d6647bf7547a7bbe1a13"
)
PROBE_SHA256 = (
    "816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6"
)
DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FEE_RATE = Decimal("0.07")
SCALE = Decimal("10000")
CENTICENT_DOLLARS = Decimal("0.0001")
CLIP_E4 = 10_000

os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    "/tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = "/tmp/z3_price_allocation_train.py"
os.environ["ROUND3_CAUSAL_CONTRACT"] = "/tmp/causal_replay_contract.py"

F = None


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha(path, expected, label):
    actual = sha256_path(path)
    if actual != expected:
        raise RuntimeError(
            f"{label} SHA mismatch expected={expected} actual={actual}"
        )
    return actual


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def canonical_sha(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_full():
    require_sha(FULL_CONSUMER, FULL_CONSUMER_SHA256, "full consumer")
    spec = importlib.util.spec_from_file_location(
        "postfill_direct_fee_full", FULL_CONSUMER
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_probe():
    require_sha(PROBE_PATH, PROBE_SHA256, "direct account probe")
    with open(PROBE_PATH, "rb") as handle:
        probe = json.load(handle)
    if (
        probe.get("schema") != "kalshi-position-value-contract-probe-v1"
        or probe.get("probe_kind")
        != "authenticated-production-one-contract-round-trip"
        or probe.get("exchange_index") != 0
        or not probe.get("flat_after")
    ):
        raise RuntimeError("direct account probe contract mismatch")
    for label in ("entry", "exit"):
        row = probe[label]["response"]
        if (
            Decimal(row["fill_count"]) != Decimal("1.00")
            or Decimal(row["average_fill_price"]) != Decimal("0.6800")
            or Decimal(row["average_fee_paid"]) != Decimal("0.0153")
        ):
            raise RuntimeError(f"direct probe {label} fields changed")
    calculated = (
        FEE_RATE * Decimal("0.68") * (Decimal("1") - Decimal("0.68"))
    ).quantize(CENTICENT_DOLLARS, rounding=ROUND_CEILING)
    if calculated != Decimal("0.0153"):
        raise RuntimeError("direct probe fee formula mismatch")
    return {
        "sha256": PROBE_SHA256,
        "authenticated_production": True,
        "exchange_index": 0,
        "one_contract_roundtrip": True,
        "price_dollars": "0.6800",
        "fee_dollars_each_leg": "0.0153",
        "formula": (
            "ceil_$0.0001(sum_fill(0.07*quantity*p*(1-p)))"
        ),
    }


def direct_fee_c(fills):
    with localcontext() as context:
        context.prec = 50
        dollars = Decimal("0")
        for price_e4, qty_e4 in fills:
            price = Decimal(int(price_e4)) / SCALE
            quantity = Decimal(int(qty_e4)) / SCALE
            dollars += (
                FEE_RATE
                * quantity
                * price
                * (Decimal("1") - price)
            )
        if not dollars:
            return 0.0
        return float(
            dollars.quantize(
                CENTICENT_DOLLARS,
                rounding=ROUND_CEILING,
            )
            * Decimal("100")
        )


def load_sealed():
    require_sha(REPORT_PATH, REPORT_SHA256, "sealed report")
    require_sha(EPISODES_PATH, EPISODES_SHA256, "sealed episodes")
    with open(REPORT_PATH, "rb") as handle:
        report = json.load(handle)
    with gzip.open(EPISODES_PATH, "rt", encoding="utf-8") as handle:
        episodes = json.load(handle)
    if (
        report.get("date_2026_07_23_read") is not False
        or tuple(report.get("dates_opened", ())) != DATES
        or report.get("source", {}).get("script_sha256")
        != "b667c811707b7203bc532b5d51accc1ff2e082d2fff284e76b7377e8945d1b09"
        or episodes.get("date_2026_07_23_read") is not False
        or episodes.get("status") != "DISCOVERY_ONLY"
    ):
        raise RuntimeError("sealed post-fill artifact contract mismatch")
    rows = episodes.get("policy_rows") or []
    if len(rows) != 1202 * len(report["policy_order"]):
        raise RuntimeError("sealed policy row cardinality mismatch")
    if episodes.get("shadow_current_audit") != report.get(
        "shadow_current_audit"
    ):
        raise RuntimeError("sealed shadow audit mismatch")
    return report, episodes


def utc_date(timestamp_us):
    return dt.datetime.fromtimestamp(
        int(timestamp_us) / 1e6,
        tz=dt.timezone.utc,
    ).date().isoformat()


def build_targets(rows):
    by_date = {date: defaultdict(list) for date in DATES}
    targets = 0
    for row_index, row in enumerate(rows):
        kind = row.get("exit_kind")
        if kind == "maker_pair":
            continue
        if kind != "ioc":
            raise RuntimeError(f"unsupported frozen exit kind {kind!r}")
        date = utc_date(row["exit_ts"])
        if date not in by_date:
            raise RuntimeError(f"IOC outside sealed dates {date}")
        held = row.get("held_side")
        if held not in ("y", "n"):
            raise RuntimeError("missing frozen held side")
        target = {
            "row_index": row_index,
            "date": date,
            "policy": row["policy"],
            "episode_id": row["episode_id"],
            "market": row["market"],
            "exit_ts": int(row["exit_ts"]),
            "first_ts": int(row["first_ts"]),
            "held_side": held,
            "entry_e4": int(row["entry_e4"]),
            "sealed_pnl_c": float(row["pnl_c"]),
            "sealed_ioc_book_asof_ts": (
                int(row["ioc_book_asof_ts"])
                if row.get("ioc_book_asof_ts") is not None
                else None
            ),
            "selected_source_policy": row.get(
                "selected_source_policy"
            ),
        }
        by_date[date][row["market"]].append(target)
        targets += 1
    for markets in by_date.values():
        for market_targets in markets.values():
            market_targets.sort(
                key=lambda row: (
                    row["exit_ts"],
                    row["row_index"],
                )
            )
    return by_date, targets


def evaluate_target(books, result, target, book_asof_ts):
    side = "yes" if target["held_side"] == "y" else "no"
    fills, filled = F.B.walk_book_sell(books[side], CLIP_E4)
    remaining = CLIP_E4 - filled
    entry = target["entry_e4"]
    gross_c = sum(
        (price - entry) / 100.0 * (qty / 10_000.0)
        for price, qty in fills
    )
    old_fee_c = F.B.taker_fee_c_per_order(fills) if filled else 0.0
    new_fee_c = direct_fee_c(fills) if filled else 0.0
    settlement_c = 0.0
    if remaining:
        won = (result == "yes") == (target["held_side"] == "y")
        settlement_c = (
            ((10_000 if won else 0) - entry)
            / 100.0
            * (remaining / 10_000.0)
        )
    old_pnl_c = gross_c - old_fee_c + settlement_c
    direct_pnl_c = gross_c - new_fee_c + settlement_c
    if not math.isclose(
        old_pnl_c,
        target["sealed_pnl_c"],
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise RuntimeError(
            "sealed PnL reproduction failed "
            f"target={target} fills={fills} asof={book_asof_ts} "
            f"reproduced={old_pnl_c}"
        )
    expected_asof = target["sealed_ioc_book_asof_ts"]
    if expected_asof is not None and expected_asof != book_asof_ts:
        raise RuntimeError(
            "sealed IOC book_asof mismatch "
            f"expected={expected_asof} actual={book_asof_ts} "
            f"target={target}"
        )
    saving = direct_pnl_c - target["sealed_pnl_c"]
    if saving < -1e-9 or saving >= 1.0 + 1e-9:
        raise RuntimeError(f"invalid fee-only saving {saving}")
    return {
        **target,
        "book_asof_ts": book_asof_ts,
        "fills": [
            [int(price), int(qty)] for price, qty in fills
        ],
        "filled_e4": int(filled),
        "remaining_e4": int(remaining),
        "gross_c": gross_c,
        "settlement_c": settlement_c,
        "whole_cent_fee_c": float(old_fee_c),
        "direct_centicent_fee_c": float(new_fee_c),
        "reproduced_whole_cent_pnl_c": old_pnl_c,
        "direct_centicent_pnl_c": direct_pnl_c,
        "fee_saving_c": saving,
        "sealed_old_pnl_exact_match": True,
    }


def scan_date(args):
    date, targets_by_market = args
    F.C.load_gap_receipt(date)
    l2_root = F.B.L2_GLOB.split("/**", 1)[0]
    l2_paths = [
        str(path)
        for path in Path(l2_root).glob(
            f"subcategory=*/date={date}/*.parquet"
        )
    ]
    if not l2_paths:
        raise RuntimeError(f"{date}: no L2 input")
    for path in l2_paths:
        F.C.assert_allowed_date_path(path, date)
    meta = F.B.load_meta()
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='8GB'")
    cursor = connection.execute(
        """
        SELECT market_ticker, recv_wall_ns, recv_mono_ns,
               local_recv_ts_us, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR),
               ws_sid, ws_seq,
               concat_ws('|',
                   coalesce(CAST(ws_sid AS VARCHAR), '<NULL>'),
                   coalesce(CAST(ws_seq AS VARCHAR), '<NULL>'),
                   coalesce(msg_type, '<NULL>'),
                   coalesce(side, '<NULL>'),
                   coalesce(CAST(price_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(delta_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(yes_levels AS VARCHAR), '<NULL>'),
                   coalesce(CAST(no_levels AS VARCHAR), '<NULL>')
               ) AS stable_id
        FROM read_parquet(?, union_by_name=true)
        WHERE series_ticker='KXBTC15M'
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, ws_seq,
                 stable_id
        """,
        [l2_paths],
    )
    output = []
    current_market = None
    targets = []
    target_index = 0
    guard = None
    book_asof_ts = None

    def emit_until(timestamp):
        nonlocal target_index
        if guard is None:
            return
        result = meta.get(current_market, (None, None))[1]
        if result not in ("yes", "no"):
            raise RuntimeError(f"missing result {current_market}")
        while (
            target_index < len(targets)
            and targets[target_index]["exit_ts"] <= timestamp
        ):
            output.append(
                evaluate_target(
                    guard.books,
                    result,
                    targets[target_index],
                    book_asof_ts,
                )
            )
            target_index += 1

    def finish_market():
        if current_market is None or guard is None:
            return
        emit_until(1 << 63)
        if target_index != len(targets):
            raise RuntimeError(f"unpriced targets {current_market}")

    while True:
        rows = cursor.fetchmany(250_000)
        if not rows:
            break
        for (
            market,
            wall,
            mono,
            local_us,
            msg_type,
            side,
            price,
            delta,
            yes_raw,
            no_raw,
            ws_sid,
            ws_seq,
            stable_id,
        ) in rows:
            if market != current_market:
                finish_market()
                current_market = market
                targets = targets_by_market.get(market, [])
                target_index = 0
                guard = (
                    F.C.BookReconstructor(market)
                    if targets
                    else None
                )
                book_asof_ts = None
            if guard is None:
                continue
            timestamp = F.C.assert_receive_clock(
                wall,
                mono,
                local_us,
                f"{date}/{market}/book/{ws_sid}/{ws_seq}",
            )
            # All timed exits are evaluated before the first causal event at
            # or after the frozen timer; the current book is therefore the
            # one already applied strictly before this event.
            emit_until(timestamp)
            if msg_type == "snapshot":
                guard.snapshot(
                    json.loads(yes_raw or "[]"),
                    json.loads(no_raw or "[]"),
                    int(ws_sid),
                    int(ws_seq),
                    int(wall),
                    int(mono),
                    int(local_us),
                    stable_id,
                )
            elif msg_type == "delta":
                guard.delta(
                    side,
                    int(price),
                    int(delta),
                    int(ws_sid),
                    int(ws_seq),
                    int(wall),
                    int(mono),
                    int(local_us),
                    stable_id,
                )
            else:
                raise RuntimeError(f"unknown book event {msg_type}")
            book_asof_ts = int(timestamp)
    finish_market()
    connection.close()
    expected = sum(len(rows) for rows in targets_by_market.values())
    if len(output) != expected:
        raise RuntimeError(
            f"{date}: repriced {len(output)} expected {expected}"
        )
    return date, output


def summary(values):
    values = [float(value) for value in values]
    if not values:
        return {
            "count": 0,
            "sum": 0.0,
            "mean": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(values),
        "sum": round(sum(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def action_projection(rows):
    projected = []
    for row in rows:
        item = dict(row)
        item.pop("pnl_c", None)
        projected.append(item)
    return projected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-out",
        default="/tmp/z3_postfill_direct_centicent_reprice_audit.json",
    )
    parser.add_argument(
        "--sidecar-out",
        default="/tmp/z3_postfill_direct_centicent_sidecar.json",
    )
    args = parser.parse_args()

    global F
    F = load_full()
    probe = verify_probe()
    report, episodes = load_sealed()
    original_rows = episodes["policy_rows"]
    by_date, expected_targets = build_targets(original_rows)
    if mp.get_start_method(allow_none=True) not in (None, "fork"):
        raise RuntimeError("direct repricing requires Linux fork")
    with mp.Pool(processes=3) as pool:
        results = pool.map(
            scan_date,
            [(date, by_date[date]) for date in DATES],
        )
    repriced = [
        row
        for _date, rows in sorted(results)
        for row in rows
    ]
    if len(repriced) != expected_targets:
        raise RuntimeError("incomplete direct repricing population")
    by_index = {row["row_index"]: row for row in repriced}
    if len(by_index) != len(repriced):
        raise RuntimeError("duplicate repriced row index")

    adjusted_rows = copy.deepcopy(original_rows)
    for index, row in enumerate(adjusted_rows):
        if row["exit_kind"] == "ioc":
            row["pnl_c"] = by_index[index][
                "direct_centicent_pnl_c"
            ]
    if action_projection(original_rows) != action_projection(
        adjusted_rows
    ):
        raise RuntimeError("fee sidecar changed frozen action projection")
    policy_order = report["policy_order"]
    by_policy = {
        policy: [
            row for row in adjusted_rows
            if row["policy"] == policy
        ]
        for policy in policy_order
    }
    if any(len(rows) != 1202 for rows in by_policy.values()):
        raise RuntimeError("adjusted policy cardinality mismatch")
    current_by_episode = {
        row["episode_id"]: row
        for row in by_policy[F.D.CURRENT_POLICY]
    }
    direct_metrics = {
        policy: F.D.policy_metrics(
            policy,
            by_policy[policy],
            current_by_episode,
        )
        for policy in policy_order
    }
    causal_policies = [
        policy
        for policy in policy_order
        if policy != F.D.ORACLE_POLICY
    ]
    if any(
        direct_metrics[policy]["ev_c_per_first_fill"] >= 0
        for policy in causal_policies
    ):
        raise RuntimeError("direct fee unexpectedly flips causal policy")

    per_policy_rows = defaultdict(list)
    for row in repriced:
        per_policy_rows[row["policy"]].append(row)
    fee_summaries = {
        policy: {
            "ioc_exits": len(per_policy_rows[policy]),
            "fee_saving_c": summary(
                row["fee_saving_c"]
                for row in per_policy_rows[policy]
            ),
            "whole_cent_fee_c": summary(
                row["whole_cent_fee_c"]
                for row in per_policy_rows[policy]
            ),
            "direct_centicent_fee_c": summary(
                row["direct_centicent_fee_c"]
                for row in per_policy_rows[policy]
            ),
            "partial_ioc_count": sum(
                row["remaining_e4"] > 0
                for row in per_policy_rows[policy]
            ),
            "multi_level_ioc_count": sum(
                len(row["fills"]) > 1
                for row in per_policy_rows[policy]
            ),
        }
        for policy in policy_order
    }
    strict_upper_bounds = {
        policy: {
            "old_ev_c": report["metrics"][policy][
                "ev_c_per_first_fill"
            ],
            "ioc_exits": report["metrics"][policy]["ioc_exits"],
            "max_fee_improvement_c": round(
                report["metrics"][policy]["ioc_exits"]
                / report["metrics"][policy]["n"]
                * 0.99,
                4,
            ),
            "optimistic_upper_ev_c": round(
                report["metrics"][policy]["ev_c_per_first_fill"]
                + report["metrics"][policy]["ioc_exits"]
                / report["metrics"][policy]["n"]
                * 0.99,
                4,
            ),
        }
        for policy in policy_order
    }

    audit = {
        "schema": "z3-postfill-direct-centicent-reprice-audit-v1",
        "analysis_class": (
            "POST_HOC_ACCOUNT_SPECIFIC_DIRECT_CENTICENT_FEE_ONLY"
        ),
        "script_sha256": sha256_path(__file__),
        "sealed_report_sha256": REPORT_SHA256,
        "sealed_episodes_sha256": EPISODES_SHA256,
        "full_consumer_sha256": FULL_CONSUMER_SHA256,
        "dates": list(DATES),
        "date_2026_07_23_read": False,
        "authenticated_probe": probe,
        "frozen_fields": (
            "policy, selected_source_policy, episode identity, market, held "
            "side, entry, exit kind, exit timestamp, maker pairing and IOC "
            "causal book walk; only pnl_c fee component changes"
        ),
        "oracle_selection_frozen": True,
        "markouts_repriced": False,
        "all_sealed_old_pnl_reproduced": True,
        "reproduced_ioc_rows": len(repriced),
        "maker_pair_rows_unchanged": sum(
            row["exit_kind"] == "maker_pair"
            for row in adjusted_rows
        ),
        "action_projection_sha256": canonical_sha(
            action_projection(adjusted_rows)
        ),
        "fee_delta_bound": (
            "0 <= direct_pnl_c-whole_cent_pnl_c < 1.00c per IOC"
        ),
        "timer_book_semantics": (
            "latest reconstructed book already applied before first causal "
            "book event at/after frozen exit_ts; any intervening trade does "
            "not change that book"
        ),
        "fee_summary_by_policy": fee_summaries,
        "rows": repriced,
    }
    audit_bytes = canonical_bytes(audit)
    audit_sha = hashlib.sha256(audit_bytes).hexdigest()
    sidecar = {
        "schema": "z3-postfill-direct-centicent-sidecar-v1",
        "status": "POST_HOC_FEE_ONLY_SENSITIVITY",
        "historical_validation_claim": False,
        "deployable": False,
        "date_2026_07_23_read": False,
        "sealed_report": {
            "path": REPORT_PATH,
            "sha256": REPORT_SHA256,
            "fee_model": (
                "generic 7% quadratic, whole-cent ceiling per IOC"
            ),
            "modified": False,
        },
        "account_specific_fee_model": (
            "authenticated direct route; Decimal aggregate raw quadratic "
            "fee, ceiling once to $0.0001 per IOC"
        ),
        "scope_limit": (
            "fee-only sensitivity; no policy/action/fill/timing reselection. "
            "Markout table remains on the sealed whole-cent fee."
        ),
        "strict_optimistic_upper_bounds": strict_upper_bounds,
        "old_metrics": report["metrics"],
        "direct_centicent_metrics": direct_metrics,
        "all_causal_policies_remain_negative": True,
        "oracle_selection_frozen": True,
        "reprice_audit": {
            "path": args.audit_out,
            "sha256": audit_sha,
            "ioc_rows": len(repriced),
            "all_sealed_old_pnl_reproduced": True,
            "action_projection_sha256": audit[
                "action_projection_sha256"
            ],
        },
    }
    sidecar_bytes = canonical_bytes(sidecar)
    with open(args.audit_out, "wb") as handle:
        handle.write(audit_bytes)
    with open(args.sidecar_out, "wb") as handle:
        handle.write(sidecar_bytes)
    print(
        json.dumps(
            {
                "status": "DIRECT_CENTICENT_FEE_ONLY_REPRICE_OK",
                "ioc_rows": len(repriced),
                "all_old_pnl_reproduced": True,
                "all_causal_policies_remain_negative": True,
                "audit_path": args.audit_out,
                "audit_sha256": audit_sha,
                "sidecar_path": args.sidecar_out,
                "sidecar_sha256": hashlib.sha256(
                    sidecar_bytes
                ).hexdigest(),
                "direct_metrics": direct_metrics,
                "fee_summary_by_policy": fee_summaries,
            },
            indent=2,
            allow_nan=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
