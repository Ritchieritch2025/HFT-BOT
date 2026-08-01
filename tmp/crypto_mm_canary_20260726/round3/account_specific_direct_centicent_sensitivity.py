#!/usr/bin/env python3
"""Post-hoc ROUND3 fee sensitivity for the observed direct-account contract.

This is not a strategy rerun or a new selection pass.  It replays the sealed
ROUND3 event stream only to recover the exact already-selected IOC book walks,
changes the fee function from whole-cent ceiling to direct-account centicent
ceiling, and then proves every action/fill field is byte-equivalent after
excluding the two fee-dependent PnL fields.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import statistics
import sys
from decimal import Decimal, ROUND_CEILING, localcontext


ROUND3_PATH = os.environ.get(
    "ROUND3_SCRIPT",
    "/tmp/z3_price_allocation_round3.py",
)
ORIGINAL_RAW_PATH = os.environ.get(
    "ROUND3_ORIGINAL_RAW",
    "/tmp/z3_price_allocation_round3_raw.json",
)
ORIGINAL_REPORT_PATH = os.environ.get(
    "ROUND3_ORIGINAL_REPORT",
    "/tmp/z3_price_allocation_round3_report.json",
)
PROBE_PATH = os.environ.get(
    "DIRECT_ACCOUNT_PROBE",
    "/tmp/position_value_contract_probe_result.json",
)

ROUND3_SHA256 = (
    "293ab66c1ce753dde7893e481d06032c74f87594bd0cffac47f0a53043fa64da"
)
ORIGINAL_RAW_SHA256 = (
    "a9bca54a22b0ae06076bd81192538a23d01058e965e0961bddaeae24ee9d3b90"
)
ORIGINAL_REPORT_SHA256 = (
    "0ef11cdd374a75328663c315496d510d8d494f736ba988ebb84da1a8b01b1485"
)
PROBE_SHA256 = (
    "816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6"
)

FEE_RATE = Decimal("0.07")
PRICE_SCALE = Decimal("10000")
QTY_SCALE = Decimal("10000")
CENTICENT_DOLLARS = Decimal("0.0001")
CENT_PER_DOLLAR = Decimal("100")
ARM_NAMES = (
    "A0_COMMON_TOUCH",
    "A1_SPEND_ALL_SLOW",
    "A2_FULL_MINIMAX_ETA",
    "A3_FULL_EV_GATE",
)
DATES = ("2026-07-20", "2026-07-21", "2026-07-22")


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


def load_json(path):
    with open(path, "rb") as handle:
        return json.load(handle)


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


def load_module(path, expected_sha, name):
    require_sha(path, expected_sha, name)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def direct_centicent_fee_c_per_order(fills):
    """Return one IOC order's fee in cents, ceiled to $0.0001.

    The sealed simulator supplies exact fixed-point ``(price_e4, qty_e4)``
    book-walk slices.  The economic fee is summed over those slices and the
    order aggregate is ceiled once to the direct-account response precision.
    """
    with localcontext() as context:
        context.prec = 50
        dollars = Decimal("0")
        for price_e4, qty_e4 in fills:
            price = Decimal(int(price_e4)) / PRICE_SCALE
            quantity = Decimal(int(qty_e4)) / QTY_SCALE
            dollars += FEE_RATE * quantity * price * (Decimal("1") - price)
        if not dollars:
            return 0.0
        fee_dollars = dollars.quantize(
            CENTICENT_DOLLARS,
            rounding=ROUND_CEILING,
        )
        return float(fee_dollars * CENT_PER_DOLLAR)


def decimal_summary(values):
    vals = [float(value) for value in values]
    if not vals:
        return {
            "count": 0,
            "sum": 0.0,
            "mean": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(vals),
        "sum": round(sum(vals), 6),
        "mean": round(statistics.fmean(vals), 6),
        "min": round(min(vals), 6),
        "max": round(max(vals), 6),
    }


def verify_probe(probe):
    if probe.get("schema") != "kalshi-position-value-contract-probe-v1":
        raise RuntimeError("unexpected probe schema")
    if probe.get("probe_kind") != "authenticated-production-one-contract-round-trip":
        raise RuntimeError("probe is not authenticated production round-trip")
    if probe.get("exchange_index") != 0 or not probe.get("flat_after"):
        raise RuntimeError("probe exchange/flat contract mismatch")

    entry = probe["entry"]["response"]
    exit_row = probe["exit"]["response"]
    for label, row in (("entry", entry), ("exit", exit_row)):
        if Decimal(row["fill_count"]) != Decimal("1.00"):
            raise RuntimeError(f"{label} is not a one-contract fill")
        if Decimal(row["average_fill_price"]) != Decimal("0.6800"):
            raise RuntimeError(f"{label} fill price changed")
        if Decimal(row["average_fee_paid"]) != Decimal("0.0153"):
            raise RuntimeError(f"{label} fee response changed")

    calculated = (
        FEE_RATE * Decimal("0.68") * (Decimal("1") - Decimal("0.68"))
    ).quantize(CENTICENT_DOLLARS, rounding=ROUND_CEILING)
    if calculated != Decimal("0.0153"):
        raise RuntimeError("centicent formula does not reproduce probe")

    before = Decimal(probe["before"]["balance"]["balance_dollars"])
    open_balance = Decimal(
        probe["after"][0]["balance"]["balance_dollars"]
    )
    final = Decimal(probe["after"][1]["balance"]["balance_dollars"])
    collateral = Decimal(probe["after"][0]["market_positions"][0][
        "market_exposure_dollars"
    ])
    if before - open_balance != collateral + calculated:
        raise RuntimeError("entry balance identity does not match direct fee")
    if final - open_balance != collateral - calculated:
        raise RuntimeError("exit balance identity does not match direct fee")
    if final - before != -(calculated * Decimal("2")):
        raise RuntimeError("round-trip balance identity does not match two fees")

    return {
        "authenticated_production": True,
        "ticker": probe["ticker"],
        "exchange_index": probe["exchange_index"],
        "one_contract_each_leg": True,
        "average_fill_price_dollars": "0.6800",
        "average_fee_paid_dollars_each_leg": "0.0153",
        "formula_unrounded_dollars": "0.015232",
        "formula_ceiled_to_dollars": "0.0153",
        "balance_dollars": {
            "before": str(before),
            "after_entry": str(open_balance),
            "after_flat": str(final),
        },
        "balance_identities": {
            "entry_debit": (
                "19.8205-19.4852=0.3353=0.3200 collateral+0.0153 fee"
            ),
            "exit_credit": (
                "19.7899-19.4852=0.3047=0.3200 collateral-0.0153 fee"
            ),
            "roundtrip": "19.7899-19.8205=-0.0306=-2*0.0153",
        },
    }


def execution_projection(raw_arms):
    """Remove only fee-dependent values; keep every action/fill observable."""
    projected = copy.deepcopy(raw_arms)
    for arm in ARM_NAMES:
        for row in projected[arm]["cycles"]:
            row.pop("pnl_c", None)
            row.pop("trigger_est_pnl_c", None)
    return projected


def per_date_metric(cycles, date):
    rows = [row for row in cycles if row["date"] == date]
    paired = [row for row in rows if row["paired"]]
    orphans = [row for row in rows if not row["paired"]]
    pair_gain = statistics.fmean(row["pnl_c"] for row in paired)
    orphan_pnl = statistics.fmean(row["pnl_c"] for row in orphans)
    realized = statistics.fmean(row["pnl_c"] for row in rows)
    q_star = abs(orphan_pnl) / (pair_gain + abs(orphan_pnl))
    return {
        "cycles": len(rows),
        "paired": len(paired),
        "orphans": len(orphans),
        "completion": round(len(paired) / len(rows), 6),
        "pair_gain_c": round(pair_gain, 6),
        "orphan_pnl_c": round(orphan_pnl, 6),
        "q_star": round(q_star, 6),
        "realized_ev_c_per_cycle": round(realized, 6),
    }


def build_report(round3, old_raw_top, new_arms, source_days, probe_evidence):
    old_arms = old_raw_top["arms"]
    old_report = load_json(ORIGINAL_REPORT_PATH)

    old_projection = execution_projection(old_arms)
    new_projection = execution_projection(new_arms)
    old_projection_sha = canonical_sha(old_projection)
    new_projection_sha = canonical_sha(new_projection)
    if old_projection != new_projection:
        raise RuntimeError(
            "fee sensitivity changed action/fill projection; result void"
        )

    all_green = all(day["daily_receipt_gate_pass"] for day in source_days)
    metrics = {}
    per_date = {}
    for arm in ARM_NAMES:
        old_cycles = old_arms[arm]["cycles"]
        new_cycles = new_arms[arm]["cycles"]
        if len(old_cycles) != len(new_cycles):
            raise RuntimeError(f"{arm} cycle count changed")
        orphan_count = sum(not row["paired"] for row in new_cycles)
        if new_arms[arm]["flattened"] != orphan_count:
            raise RuntimeError(
                f"{arm} does not have exactly one IOC flatten per orphan"
            )

        paired_deltas = []
        orphan_deltas = []
        for old_row, new_row in zip(old_cycles, new_cycles):
            delta = float(new_row["pnl_c"]) - float(old_row["pnl_c"])
            if old_row["paired"]:
                paired_deltas.append(delta)
                if abs(delta) > 1e-12:
                    raise RuntimeError(f"{arm} maker-pair PnL changed")
            else:
                orphan_deltas.append(delta)
                if delta < -1e-12 or delta >= 1.0 + 1e-12:
                    raise RuntimeError(
                        f"{arm} invalid whole-cent-to-centicent delta {delta}"
                    )

        adjusted = round3._ROUND2.metric_block(arm, new_arms[arm])
        adjusted["strict_pass"] = bool(adjusted["strict_pass"] and all_green)
        original = old_report["metrics"][arm]
        metrics[arm] = {
            "cycles": adjusted["cycles"],
            "markets": adjusted["markets"],
            "paired": adjusted["paired"],
            "orphans": adjusted["orphans"],
            "completion": adjusted["completion"],
            "completion_wilson_lcb95": adjusted[
                "completion_wilson_lcb95"
            ],
            "original_whole_cent": {
                "orphan_pnl_c": original["orphan_loss_c"],
                "q_star": original["empirical_break_even_q_star"],
                "realized_ev_c_per_cycle": original[
                    "realized_ev_c_per_cycle"
                ],
                "ci95_market_cluster_c": original[
                    "ci95_market_cluster_c"
                ],
            },
            "account_direct_centicent": {
                "pair_gain_c": adjusted["pair_gain_c"],
                "orphan_pnl_c": adjusted["orphan_loss_c"],
                "q_star": adjusted["empirical_break_even_q_star"],
                "realized_ev_c_per_cycle": adjusted[
                    "realized_ev_c_per_cycle"
                ],
                "ci95_market_cluster_c": adjusted[
                    "ci95_market_cluster_c"
                ],
                "strict_pass": adjusted["strict_pass"],
                "positive_ev": bool(
                    adjusted["realized_ev_c_per_cycle"] > 0
                ),
            },
            "change": {
                "orphan_pnl_c": round(
                    adjusted["orphan_loss_c"] - original["orphan_loss_c"],
                    6,
                ),
                "q_star": round(
                    adjusted["empirical_break_even_q_star"]
                    - original["empirical_break_even_q_star"],
                    6,
                ),
                "realized_ev_c_per_cycle": round(
                    adjusted["realized_ev_c_per_cycle"]
                    - original["realized_ev_c_per_cycle"],
                    6,
                ),
                "orphan_cycle_fee_saving_c": decimal_summary(orphan_deltas),
                "paired_cycle_pnl_delta_c": decimal_summary(paired_deltas),
            },
        }
        per_date[arm] = {
            date: per_date_metric(new_cycles, date)
            for date in DATES
        }

    no_arm_positive = not any(
        row["account_direct_centicent"]["positive_ev"]
        for row in metrics.values()
    )
    if not no_arm_positive:
        decision = "AT_LEAST_ONE_ARM_TURNED_POSITIVE_POST_HOC"
    else:
        decision = "NO_ARM_TURNS_POSITIVE"

    theoretical_bounds = {}
    for arm in ARM_NAMES:
        original = old_report["metrics"][arm]
        orphan_rate = original["orphans"] / original["cycles"]
        upper = (
            original["realized_ev_c_per_cycle"]
            + orphan_rate * 0.99
        )
        best_orphan_pnl = original["orphan_loss_c"] + 0.99
        q_star_lower = (
            abs(best_orphan_pnl)
            / (original["pair_gain_c"] + abs(best_orphan_pnl))
        )
        ci_upper_bound = original["ci95_market_cluster_c"][1] + 0.99
        theoretical_bounds[arm] = {
            "old_ev_c_per_cycle": original["realized_ev_c_per_cycle"],
            "orphan_rate": round(orphan_rate, 9),
            "max_fee_saving_per_orphan_c": 0.99,
            "ev_upper_bound_c_per_cycle": round(upper, 6),
            "q_star_lower_bound": round(q_star_lower, 6),
            "completion_lcb95": original["completion_wilson_lcb95"],
            "cluster_ci95_upper_bound_c": round(ci_upper_bound, 6),
            "can_turn_positive": bool(upper > 0),
            "can_pass_completion_gate": bool(
                original["completion_wilson_lcb95"] > q_star_lower
            ),
            "can_have_positive_cluster_ci_lower": bool(ci_upper_bound > 0),
        }

    return {
        "schema": "round3-account-specific-direct-centicent-sensitivity-v1",
        "analysis_class": "POST_HOC_ACCOUNT_SPECIFIC_DIRECT_CENTICENT",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "historical_validation_claim": False,
        "deployable": False,
        "this_run_read_2026_07_23": False,
        "dates": list(DATES),
        "decision": decision,
        "no_arm_positive": no_arm_positive,
        "interpretation": (
            "Actions, fills, prices, times, exits, and pair outcomes are "
            "frozen to sealed ROUND3. Only realized IOC fee accounting is "
            "replaced; this cannot create a new strategy candidate."
        ),
        "fee_models": {
            "sealed_round3": (
                "ceil to $0.01 once per IOC order of "
                "sum(0.07*qty*p*(1-p))"
            ),
            "account_specific_direct": (
                "Decimal ceil to $0.0001 once per IOC order of "
                "sum(0.07*qty*p*(1-p)); this requested sensitivity does "
                "not add a separate rounding-fee/accumulator component"
            ),
            "maker_pair_entry_fee": "unchanged zero",
            "scope_limit": (
                "The authenticated probe identifies the base trade-fee "
                "contract for one whole contract at one price. It does not "
                "empirically identify direct-member rounding components for "
                "an IOC split across fractional fills; current API docs say "
                "such components can exist when balance change exceeds "
                "$0.0001 precision. Startup reconciliation remains mandatory."
            ),
            "precision": {
                "price": "exact price_e4 / 10000",
                "quantity": "exact qty_e4 / 10000",
                "decimal_context_digits": 50,
            },
        },
        "authenticated_probe": probe_evidence,
        "official_documentation_resolution": {
            "current_july_2026_fee_schedule": {
                "url": "https://kalshi.com/docs/kalshi-fee-schedule.pdf",
                "statement": (
                    "general 7% formula; July 7 schedule defines round up "
                    "so fee plus positionCost is rounded to a centicent"
                ),
            },
            "api_fee_rounding": {
                "url": "https://docs.kalshi.com/getting_started/fee_rounding",
                "statement": (
                    "trade fee is ceiled to $0.0001; direct-member target "
                    "balance precision is $0.0001 while non-direct is $0.01"
                ),
            },
            "direct_account_observation": (
                "average_fee_paid=$0.0153 on each 1-contract $0.68 fill, "
                "and balance_dollars moves by exact direct centicent amounts "
                "without an additional charge because the direct target "
                "precision is already $0.0001"
            ),
            "finding": (
                "The authenticated direct-account receipt agrees with the "
                "current route-specific API documentation. The conflict is "
                "with sealed ROUND3's stale generic whole-cent assumption "
                "and with reading the displayed whole-cent one-contract fee "
                "table as the direct-account response contract."
            ),
            "operational_rule": (
                "Because fee behavior is member/route-specific and can "
                "change, startup must authenticate, reconcile a bounded fee "
                "probe or recent fill+balance identity, and halt if the "
                "observed response contract differs"
            ),
        },
        "rigorous_no_flip_upper_bound": {
            "proof": (
                "Changing ceil-$0.01 to ceil-$0.0001 can improve at most "
                "$0.0099 = 0.99c per orphan IOC; paired cycles do not change. "
                "Thus EV_new <= EV_old + orphan_rate*0.99c."
            ),
            "arms": theoretical_bounds,
            "all_upper_bounds_negative": all(
                not row["can_turn_positive"]
                for row in theoretical_bounds.values()
            ),
            "strict_gate_possible_under_bound": any(
                row["can_turn_positive"]
                and row["can_pass_completion_gate"]
                and row["can_have_positive_cluster_ci_lower"]
                for row in theoretical_bounds.values()
            ),
        },
        "frozen_execution_audit": {
            "actions_fills_frozen": True,
            "projection_definition": (
                "complete raw arms with only cycle.pnl_c and "
                "cycle.trigger_est_pnl_c removed"
            ),
            "sealed_projection_sha256": old_projection_sha,
            "sensitivity_projection_sha256": new_projection_sha,
            "projection_byte_equivalent": True,
            "one_ioc_flatten_per_orphan_all_arms": True,
            "source_receipts_all_green": all_green,
            "source_clean_strategy_market_days": sum(
                day["clean_strategy_market_days"] for day in source_days
            ),
            "source_invalid_market_days": sum(
                day["invalid_market_days"] for day in source_days
            ),
        },
        "metrics": metrics,
        "per_discovery_date": per_date,
        "inputs": {
            "sealed_round3_script": {
                "path": ROUND3_PATH,
                "sha256": ROUND3_SHA256,
            },
            "sealed_round3_raw": {
                "path": ORIGINAL_RAW_PATH,
                "sha256": ORIGINAL_RAW_SHA256,
            },
            "sealed_round3_report": {
                "path": ORIGINAL_REPORT_PATH,
                "sha256": ORIGINAL_REPORT_SHA256,
            },
            "direct_account_probe": {
                "path": PROBE_PATH,
                "sha256": PROBE_SHA256,
            },
        },
        "original_report_addendum_binding": {
            "applies_to_sha256": ORIGINAL_REPORT_SHA256,
            "sealed_original_modified": False,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default="/tmp/round3_account_direct_centicent_sensitivity.json",
    )
    parser.add_argument(
        "--adjusted-raw-out",
        default="/tmp/round3_account_direct_centicent_adjusted_raw.json",
    )
    parser.add_argument(
        "--reuse-adjusted-raw",
        help=(
            "reuse a previously generated fee-only adjusted raw after "
            "rechecking the complete frozen execution projection"
        ),
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    require_sha(ORIGINAL_RAW_PATH, ORIGINAL_RAW_SHA256, "sealed raw")
    require_sha(
        ORIGINAL_REPORT_PATH,
        ORIGINAL_REPORT_SHA256,
        "sealed report",
    )
    require_sha(PROBE_PATH, PROBE_SHA256, "direct-account probe")
    probe_evidence = verify_probe(load_json(PROBE_PATH))
    fee_at_68_c = direct_centicent_fee_c_per_order([(6_800, 10_000)])
    if fee_at_68_c != 1.53:
        raise RuntimeError(f"fee self-test failed: {fee_at_68_c}")
    if args.self_test:
        print(json.dumps({
            "status": "ACCOUNT_DIRECT_CENTICENT_SELF_TEST_OK",
            "fee_at_68_cents": fee_at_68_c,
            "probe": probe_evidence,
        }, indent=2))
        return

    if mp.get_start_method(allow_none=True) not in (None, "fork"):
        raise RuntimeError("sensitivity replay requires Linux fork")
    round3 = load_module(
        ROUND3_PATH,
        ROUND3_SHA256,
        "round3_fee_sensitivity_sealed",
    )
    round3.initialize_modules()
    round3._BASE.taker_fee_c_per_order = direct_centicent_fee_c_per_order

    arms = round3.policies()
    if tuple(policy.name for policy in arms) != ARM_NAMES:
        raise RuntimeError("sealed arm list changed")
    targeted_audit_ref = None
    if args.reuse_adjusted_raw:
        adjusted_existing = load_json(args.reuse_adjusted_raw)
        if (
            adjusted_existing.get("schema")
            != "round3-account-direct-centicent-adjusted-raw-v1"
            or adjusted_existing.get("sealed_original_raw_sha256")
            != ORIGINAL_RAW_SHA256
            or not adjusted_existing.get("fee_only_sensitivity")
        ):
            raise RuntimeError("reused adjusted raw contract mismatch")
        targeted_audit_ref = adjusted_existing.get("targeted_reprice_audit")
        if not targeted_audit_ref:
            raise RuntimeError("reused adjusted raw lacks targeted audit binding")
        require_sha(
            targeted_audit_ref["path"],
            targeted_audit_ref["sha256"],
            "targeted reprice audit",
        )
        targeted_audit = load_json(targeted_audit_ref["path"])
        if (
            targeted_audit.get("schema")
            != "round3-targeted-direct-centicent-reprice-audit-v1"
            or not targeted_audit.get("all_sealed_old_pnl_reproduced")
            or targeted_audit.get("reproduced_orphan_cycles") != 4_401
            or not targeted_audit.get("paired_cycles_byte_unchanged")
            or not targeted_audit.get("all_three_discovery_dates_complete")
        ):
            raise RuntimeError("targeted reprice audit gates failed")
        new_arms = adjusted_existing["arms"]
        source_days = load_json(ORIGINAL_REPORT_PATH)["source_quality"]["days"]
    else:
        new_arms, source_days = round3.run_all_days(arms)
        new_arms = round3.json_safe(new_arms)
    old_raw_top = load_json(ORIGINAL_RAW_PATH)
    report = build_report(
        round3,
        old_raw_top,
        new_arms,
        source_days,
        probe_evidence,
    )
    if targeted_audit_ref is not None:
        report["targeted_reprice_audit"] = {
            **targeted_audit_ref,
            "schema": targeted_audit["schema"],
            "analysis_script_sha256": targeted_audit[
                "analysis_script_sha256"
            ],
            "all_sealed_old_pnl_reproduced": True,
            "reproduced_orphan_cycles": 4_401,
            "paired_cycles_byte_unchanged": True,
            "all_three_discovery_dates_complete": True,
        }

    if args.reuse_adjusted_raw:
        adjusted_sha = sha256_path(args.reuse_adjusted_raw)
        adjusted_path = args.reuse_adjusted_raw
    else:
        adjusted_raw = {
            "schema": "round3-account-direct-centicent-adjusted-raw-v1",
            "analysis_class": "POST_HOC_ACCOUNT_SPECIFIC_DIRECT_CENTICENT",
            "historical_validation_claim": False,
            "deployable": False,
            "this_run_read_2026_07_23": False,
            "fee_only_sensitivity": True,
            "sealed_original_raw_sha256": ORIGINAL_RAW_SHA256,
            "frozen_execution_projection_sha256": report[
                "frozen_execution_audit"
            ]["sealed_projection_sha256"],
            "arms": new_arms,
        }
        adjusted_bytes = json.dumps(
            adjusted_raw,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        adjusted_sha = hashlib.sha256(adjusted_bytes).hexdigest()
        adjusted_path = args.adjusted_raw_out
        with open(adjusted_path, "wb") as handle:
            handle.write(adjusted_bytes)
    report["adjusted_raw"] = {
        "path": adjusted_path,
        "sha256": adjusted_sha,
    }
    report["analysis_script_sha256"] = sha256_path(__file__)

    with open(args.out, "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    print(json.dumps({
        "decision": report["decision"],
        "no_arm_positive": report["no_arm_positive"],
        "metrics": {
            arm: report["metrics"][arm]["account_direct_centicent"]
            for arm in ARM_NAMES
        },
        "frozen_execution_audit": report["frozen_execution_audit"],
        "out": args.out,
        "adjusted_raw_out": adjusted_path,
        "adjusted_raw_sha256": adjusted_sha,
    }, indent=2))


if __name__ == "__main__":
    main()
