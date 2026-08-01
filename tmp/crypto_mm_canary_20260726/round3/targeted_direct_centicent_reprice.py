#!/usr/bin/env python3
"""Targeted fee-only repricing of sealed ROUND3 orphan IOC exits.

The strategy is not re-evaluated.  For every already-sealed orphan cycle this
script reconstructs the causal book immediately before the sealed IOC exit
timestamp, reproduces the sealed whole-cent-fee PnL exactly, then substitutes
only the requested direct-account centicent fee.  Failure to reproduce even
one sealed PnL makes the complete sensitivity void.
"""
from __future__ import annotations

import argparse
import copy
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

import duckdb


ROUND3_PATH = os.environ.get(
    "ROUND3_SCRIPT",
    "/tmp/z3_price_allocation_round3.py",
)
ORIGINAL_RAW_PATH = os.environ.get(
    "ROUND3_ORIGINAL_RAW",
    "/tmp/z3_price_allocation_round3_raw.json",
)
ROUND3_SHA256 = (
    "293ab66c1ce753dde7893e481d06032c74f87594bd0cffac47f0a53043fa64da"
)
ORIGINAL_RAW_SHA256 = (
    "a9bca54a22b0ae06076bd81192538a23d01058e965e0961bddaeae24ee9d3b90"
)
ARM_NAMES = (
    "A0_COMMON_TOUCH",
    "A1_SPEND_ALL_SLOW",
    "A2_FULL_MINIMAX_ETA",
    "A3_FULL_EV_GATE",
)
DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
CLIP_E4 = 10_000
FEE_RATE = Decimal("0.07")
SCALE = Decimal("10000")
CENTICENT_DOLLARS = Decimal("0.0001")

# One cycle has equal YES/NO entry prices.  The causal trade at first_ts is
# taker_side=no, which consumes the YES bid.  This exact evidence is audited
# independently from the date=2026-07-22 trade fact.
SIDE_OVERRIDE = {
    (
        "A3_FULL_EV_GATE",
        "KXBTC15M-26JUL221915-15",
        1784761909380420,
    ): {
        "side": "yes",
        "trade_id": "5d0c82f0-bef2-7c6d-59c2-db56efdef88a",
        "yes_price_e4": 1100,
        "count_e4": 568700,
        "taker_side": "no",
        "local_recv_ts_us": 1784761909380420,
        "recv_wall_ns": 1784761909380420446,
        "recv_mono_ns": 974138241462268,
    },
}
SIDE_OVERRIDE_TRADE_PATH = (
    "/home/ubuntu/hft-bot/work/warehouse/facts/trades/"
    "category=Crypto/subcategory=BTC/date=2026-07-22/"
    "trades__Crypto__BTC__2026-07-22.csv.gz"
)
SIDE_OVERRIDE_TRADE_SHA256 = (
    "0ceefe810a3206992da85204967001a3c4b3d68617cbd3f369da381925b27700"
)

R3 = None


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


def load_module(path, expected_sha, name):
    require_sha(path, expected_sha, name)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def direct_fee_c(fills):
    with localcontext() as context:
        context.prec = 50
        dollars = Decimal("0")
        for price_e4, qty_e4 in fills:
            price = Decimal(int(price_e4)) / SCALE
            quantity = Decimal(int(qty_e4)) / SCALE
            dollars += FEE_RATE * quantity * price * (Decimal("1") - price)
        if not dollars:
            return 0.0
        return float(
            dollars.quantize(
                CENTICENT_DOLLARS,
                rounding=ROUND_CEILING,
            )
            * Decimal("100")
        )


def load_original():
    require_sha(ORIGINAL_RAW_PATH, ORIGINAL_RAW_SHA256, "sealed raw")
    with open(ORIGINAL_RAW_PATH, "rb") as handle:
        raw = json.load(handle)
    if (
        raw.get("schema") != "z3-price-allocation-round3-raw-v1"
        or raw.get("this_run_read_2026_07_23") is not False
        or tuple(raw.get("arms", {})) != ARM_NAMES
    ):
        raise RuntimeError("sealed raw contract mismatch")
    return raw


def verify_side_override_source():
    require_sha(
        SIDE_OVERRIDE_TRADE_PATH,
        SIDE_OVERRIDE_TRADE_SHA256,
        "ambiguous-side trade fact",
    )
    key, expected = next(iter(SIDE_OVERRIDE.items()))
    connection = duckdb.connect()
    rows = connection.execute(
        """
        SELECT market_ticker, trade_id, yes_price_e4, count_e4, taker_side,
               local_recv_ts_us, recv_wall_ns, recv_mono_ns
        FROM read_csv(?, header=true, union_by_name=true,
                      types={'taker_side':'VARCHAR'})
        WHERE market_ticker=? AND trade_id=?
        """,
        [SIDE_OVERRIDE_TRADE_PATH, key[1], expected["trade_id"]],
    ).fetchall()
    connection.close()
    if len(rows) != 1:
        raise RuntimeError(f"ambiguous-side trade cardinality {len(rows)}")
    (
        market,
        trade_id,
        yes_price,
        quantity,
        taker,
        local_us,
        wall,
        mono,
    ) = rows[0]
    observed = {
        "side": "yes",
        "trade_id": trade_id,
        "yes_price_e4": int(yes_price),
        "count_e4": int(quantity),
        "taker_side": taker,
        "local_recv_ts_us": int(local_us),
        "recv_wall_ns": int(wall),
        "recv_mono_ns": int(mono),
    }
    if market != key[1] or observed != expected:
        raise RuntimeError(
            f"ambiguous-side source mismatch {market=} {observed=}"
        )
    R3._CONTRACT.assert_receive_clock(
        wall,
        mono,
        local_us,
        "ambiguous-side trade source",
    )
    if int(local_us) != key[2]:
        raise RuntimeError("ambiguous-side trade is not at sealed first_ts")
    if taker != "no" or int(yes_price) != 1100:
        raise RuntimeError("ambiguous-side direction/price contract failed")
    return {
        **expected,
        "arm": key[0],
        "market": key[1],
        "first_ts": key[2],
        "source_path": SIDE_OVERRIDE_TRADE_PATH,
        "source_sha256": SIDE_OVERRIDE_TRADE_SHA256,
        "source_row_count": 1,
        "causal_clock_identity_pass": True,
        "direction_reason": (
            "sealed fill mapping is YES-resting-bid <- taker_side=no; "
            "the 1100 YES trade at sealed first_ts therefore fills the "
            "1100 YES quote, leaving held_side=YES"
        ),
    }


def build_targets(raw, verified_side_override):
    by_date = {date: defaultdict(list) for date in DATES}
    total = 0
    for arm in ARM_NAMES:
        cycles = raw["arms"][arm]["cycles"]
        orphan_count = sum(not row["paired"] for row in cycles)
        if raw["arms"][arm]["flattened"] != orphan_count:
            raise RuntimeError(f"{arm}: not one IOC flatten per orphan")
        for index, row in enumerate(cycles):
            if row["paired"]:
                continue
            if row["date"] not in by_date:
                raise RuntimeError("cycle outside discovery dates")
            if row["trigger_ts"] is None:
                raise RuntimeError("orphan without IOC trigger")
            entry_e4 = int(round(float(row["entry_c"]) * 100.0))
            features = row["admission_features"]
            candidates = []
            if entry_e4 == int(features["allocated_yes_e4"]):
                candidates.append("yes")
            if entry_e4 == int(features["allocated_no_e4"]):
                candidates.append("no")
            override = SIDE_OVERRIDE.get(
                (arm, row["market"], int(row["first_ts"]))
            )
            if override is not None:
                if (
                    verified_side_override["arm"] != arm
                    or verified_side_override["market"] != row["market"]
                    or verified_side_override["first_ts"] != int(row["first_ts"])
                ):
                    raise RuntimeError("verified side override key mismatch")
                candidates = [verified_side_override["side"]]
            if not candidates:
                raise RuntimeError(
                    f"cannot infer held side {arm}/{row['market']}/{index}"
                )
            by_date[row["date"]][row["market"]].append({
                "arm": arm,
                "cycle_index": index,
                "market": row["market"],
                "date": row["date"],
                "exit_ts": int(row["exit_ts"]),
                "first_ts": int(row["first_ts"]),
                "entry_e4": entry_e4,
                "sealed_pnl_c": float(row["pnl_c"]),
                "candidate_sides": candidates,
                "side_override": override,
            })
            total += 1
    for markets in by_date.values():
        for targets in markets.values():
            targets.sort(
                key=lambda row: (
                    row["exit_ts"],
                    row["arm"],
                    row["cycle_index"],
                )
            )
    return by_date, total


def pnl_for_side(base, books, result, target, side):
    fills, filled = base.walk_book_sell(books[side], CLIP_E4)
    remaining = CLIP_E4 - filled
    entry = target["entry_e4"]
    gross_c = sum(
        (price - entry) / 100.0 * (quantity / 10_000.0)
        for price, quantity in fills
    )
    old_fee_c = base.taker_fee_c_per_order(fills) if filled else 0.0
    new_fee_c = direct_fee_c(fills) if filled else 0.0
    settlement_c = 0.0
    if remaining:
        won = (result == "yes") == (side == "yes")
        settlement_c = (
            ((10_000 if won else 0) - entry)
            / 100.0
            * (remaining / 10_000.0)
        )
    old_pnl_c = gross_c - old_fee_c + settlement_c
    new_pnl_c = gross_c - new_fee_c + settlement_c
    return {
        "side": side,
        "fills": [[int(price), int(quantity)] for price, quantity in fills],
        "filled_e4": int(filled),
        "remaining_e4": int(remaining),
        "gross_c": gross_c,
        "settlement_c": settlement_c,
        "old_fee_c": float(old_fee_c),
        "direct_fee_c": float(new_fee_c),
        "reproduced_old_pnl_c": old_pnl_c,
        "direct_pnl_c": new_pnl_c,
    }


def evaluate_target(base, books, result, target):
    candidates = [
        pnl_for_side(base, books, result, target, side)
        for side in target["candidate_sides"]
    ]
    matches = [
        row for row in candidates
        if math.isclose(
            row["reproduced_old_pnl_c"],
            target["sealed_pnl_c"],
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "sealed PnL reproduction failed "
            f"target={target} candidates={candidates}"
        )
    match = matches[0]
    delta = match["direct_pnl_c"] - target["sealed_pnl_c"]
    if delta < -1e-9 or delta >= 1.0 + 1e-9:
        raise RuntimeError(f"invalid fee-only PnL delta {delta}")
    return {
        **target,
        **match,
        "fee_saving_c": delta,
        "old_pnl_exact_match": True,
    }


def scan_date(args):
    date, targets_by_market = args
    R3._CONTRACT.load_gap_receipt(date)
    l2_paths = R3.exact_date_paths(R3._BASE.L2_GLOB, date, "parquet")
    meta = R3._BASE.load_meta()
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='8GB'")
    cursor = connection.execute(
        """
        SELECT market_ticker, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR),
               ws_sid, ws_seq, recv_wall_ns, recv_mono_ns,
               local_recv_ts_us
        FROM read_parquet(?, union_by_name=true)
        WHERE series_ticker=?
        ORDER BY market_ticker, recv_wall_ns, recv_mono_ns, ws_seq
        """,
        [l2_paths, "KXBTC15M"],
    )
    output = []
    current_ticker = None
    guard = None
    targets = []
    target_index = 0

    def emit_until(ts_exclusive_or_equal):
        nonlocal target_index
        if guard is None:
            return
        result = meta.get(current_ticker, (None, None))[1]
        if result not in ("yes", "no"):
            raise RuntimeError(f"missing result for {current_ticker}")
        while (
            target_index < len(targets)
            and targets[target_index]["exit_ts"] <= ts_exclusive_or_equal
        ):
            output.append(
                evaluate_target(
                    R3._BASE,
                    guard.books,
                    result,
                    targets[target_index],
                )
            )
            target_index += 1

    def finish_market():
        if current_ticker is None:
            return
        emit_until(1 << 63)
        if target_index != len(targets):
            raise RuntimeError(f"unpriced exits for {current_ticker}")

    while True:
        rows = cursor.fetchmany(250_000)
        if not rows:
            break
        for (
            ticker,
            msg_type,
            side,
            price,
            delta,
            yes_raw,
            no_raw,
            ws_sid,
            ws_seq,
            wall,
            mono,
            local_us,
        ) in rows:
            if ticker != current_ticker:
                finish_market()
                current_ticker = ticker
                targets = targets_by_market.get(ticker, [])
                target_index = 0
                guard = R3._CONTRACT.BookReconstructor(ticker)
            ts = R3._CONTRACT.assert_receive_clock(
                wall,
                mono,
                local_us,
                f"{date}/{ticker}/book/{ws_seq}",
            )
            # The simulator advances timers before applying the event.
            emit_until(ts)
            stable_id = f"{msg_type}:{ws_seq}"
            if msg_type == "snapshot":
                yes_levels = json.loads(yes_raw or "[]")
                no_levels = json.loads(no_raw or "[]")
                guard.snapshot(
                    yes_levels,
                    no_levels,
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
    finish_market()
    connection.close()

    expected = sum(len(rows) for rows in targets_by_market.values())
    if len(output) != expected:
        raise RuntimeError(
            f"{date}: priced {len(output)} of {expected} orphan exits"
        )
    return date, output


def summary(values):
    vals = [float(value) for value in values]
    return {
        "count": len(vals),
        "sum": round(sum(vals), 6),
        "mean": round(statistics.fmean(vals), 6),
        "min": round(min(vals), 6),
        "max": round(max(vals), 6),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adjusted-raw-out",
        default="/tmp/round3_account_direct_centicent_adjusted_raw.json",
    )
    parser.add_argument(
        "--audit-out",
        default="/tmp/round3_account_direct_centicent_reprice_audit.json",
    )
    args = parser.parse_args()

    global R3
    raw = load_original()
    R3 = load_module(
        ROUND3_PATH,
        ROUND3_SHA256,
        "targeted_reprice_sealed_round3",
    )
    R3.initialize_modules()
    verified_side_override = verify_side_override_source()
    by_date, expected_total = build_targets(raw, verified_side_override)
    if mp.get_start_method(allow_none=True) not in (None, "fork"):
        raise RuntimeError("targeted repricing requires Linux fork")
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
    if len(repriced) != expected_total:
        raise RuntimeError("complete orphan population not repriced")
    if {row["date"] for row in repriced} != set(DATES):
        raise RuntimeError("not all three discovery dates were repriced")

    adjusted_arms = copy.deepcopy(raw["arms"])
    by_arm = defaultdict(list)
    for row in repriced:
        cycle = adjusted_arms[row["arm"]]["cycles"][row["cycle_index"]]
        if not math.isclose(
            float(cycle["pnl_c"]),
            row["sealed_pnl_c"],
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError("sealed cycle moved before repricing")
        cycle["pnl_c"] = float(row["direct_pnl_c"])
        by_arm[row["arm"]].append(row)

    for arm in ARM_NAMES:
        old_paired = [
            row for row in raw["arms"][arm]["cycles"] if row["paired"]
        ]
        new_paired = [
            row for row in adjusted_arms[arm]["cycles"] if row["paired"]
        ]
        if old_paired != new_paired:
            raise RuntimeError(f"{arm}: paired cycles changed")

    audit = {
        "schema": "round3-targeted-direct-centicent-reprice-audit-v1",
        "analysis_class": "POST_HOC_ACCOUNT_SPECIFIC_DIRECT_CENTICENT",
        "analysis_script_sha256": sha256_path(__file__),
        "sealed_round3_raw_sha256": ORIGINAL_RAW_SHA256,
        "sealed_round3_script_sha256": ROUND3_SHA256,
        "dates": list(DATES),
        "this_run_read_2026_07_23": False,
        "all_three_discovery_dates_complete": (
            {row["date"] for row in repriced} == set(DATES)
        ),
        "all_sealed_old_pnl_reproduced": True,
        "reproduced_orphan_cycles": len(repriced),
        "one_ioc_per_orphan": True,
        "paired_cycles_byte_unchanged": True,
        "orphan_identity_frozen": (
            "arm, cycle_index, market, date, first_ts, exit_ts, entry_e4, "
            "held side, route=IOC, causal exit book walk; only pnl_c replaced"
        ),
        "fee_only_delta_bound": (
            "0 <= direct_pnl_c-sealed_pnl_c < 1.00c for every orphan; "
            "whole-cent ceil minus centicent ceil is at most 0.99c"
        ),
        "timer_book_semantics": (
            "book immediately before first causal event with local_recv_ts_us "
            ">= sealed exit_ts; simulator advances timer before event apply"
        ),
        "ambiguous_side_override": verified_side_override,
        "arms": {
            arm: {
                "orphan_cycles": len(by_arm[arm]),
                "fee_saving_c": summary(
                    row["fee_saving_c"] for row in by_arm[arm]
                ),
                "old_fee_c": summary(
                    row["old_fee_c"] for row in by_arm[arm]
                ),
                "direct_fee_c": summary(
                    row["direct_fee_c"] for row in by_arm[arm]
                ),
                "partial_ioc_count": sum(
                    row["remaining_e4"] > 0 for row in by_arm[arm]
                ),
                "multi_level_ioc_count": sum(
                    len(row["fills"]) > 1 for row in by_arm[arm]
                ),
            }
            for arm in ARM_NAMES
        },
        "cycles": repriced,
    }
    audit_bytes = json.dumps(
        audit,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    audit_sha = hashlib.sha256(audit_bytes).hexdigest()
    adjusted = {
        "schema": "round3-account-direct-centicent-adjusted-raw-v1",
        "analysis_class": "POST_HOC_ACCOUNT_SPECIFIC_DIRECT_CENTICENT",
        "historical_validation_claim": False,
        "deployable": False,
        "this_run_read_2026_07_23": False,
        "fee_only_sensitivity": True,
        "sealed_original_raw_sha256": ORIGINAL_RAW_SHA256,
        "targeted_reprice_audit": {
            "path": args.audit_out,
            "sha256": audit_sha,
            "all_sealed_old_pnl_reproduced": True,
            "cycles": len(repriced),
        },
        "arms": adjusted_arms,
    }
    adjusted_bytes = json.dumps(
        adjusted,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    with open(args.audit_out, "wb") as handle:
        handle.write(audit_bytes)
    with open(args.adjusted_raw_out, "wb") as handle:
        handle.write(adjusted_bytes)
    print(json.dumps({
        "status": "TARGETED_DIRECT_CENTICENT_REPRICE_OK",
        "orphan_cycles": len(repriced),
        "all_sealed_old_pnl_reproduced": True,
        "audit_out": args.audit_out,
        "audit_sha256": audit_sha,
        "adjusted_raw_out": args.adjusted_raw_out,
        "adjusted_raw_sha256": hashlib.sha256(adjusted_bytes).hexdigest(),
        "arms": audit["arms"],
    }, indent=2))


if __name__ == "__main__":
    main()
