#!/usr/bin/env python3
"""Read-only diagnostic for the corrected crypto-MM shadow session.

The script consumes a frozen local copy of EC2 NDJSON receipts.  It never
imports the engine, connects to the exchange, or mutates the source files.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "source_snapshot"
JSON_OUT = ROOT / "shadow_24h_diagnostic.json"
MD_OUT = ROOT / "shadow_24h_diagnostic.md"
SUMS_OUT = ROOT / "SHA256SUMS"
CHECKPOINTS_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
NS_PER_S = 1_000_000_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iso_ns(wall_ns: int | None) -> str | None:
    if wall_ns is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(
        wall_ns / NS_PER_S, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def source_ref(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": event["_file"],
        "line": event["_line"],
        "src": event.get("src"),
        "wall_ns": event.get("wall_ns"),
        "time_utc": iso_ns(event.get("wall_ns")),
    }


def load_events() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    ordinal = 0
    for path in sorted(SOURCE_DIR.glob("mm_*.ndjson")):
        lines = 0
        first_ns = None
        last_ns = None
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise ValueError(
                        f"incomplete final line: {path.name}:{line_no}"
                    )
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(
                        f"non-object receipt: {path.name}:{line_no}"
                    )
                wall_ns = row.get("wall_ns")
                if type(wall_ns) is not int or wall_ns <= 0:
                    raise ValueError(
                        f"invalid wall_ns: {path.name}:{line_no}"
                    )
                row["_file"] = path.name
                row["_line"] = line_no
                row["_ordinal"] = ordinal
                ordinal += 1
                events.append(row)
                lines += 1
                first_ns = wall_ns if first_ns is None else min(
                    first_ns, wall_ns
                )
                last_ns = wall_ns if last_ns is None else max(
                    last_ns, wall_ns
                )
        manifests.append({
            "file": path.name,
            "bytes": path.stat().st_size,
            "lines": lines,
            "sha256": sha256(path),
            "first_wall_ns": first_ns,
            "first_time_utc": iso_ns(first_ns),
            "last_wall_ns": last_ns,
            "last_time_utc": iso_ns(last_ns),
        })
    if not events:
        raise ValueError("no source NDJSON receipts found")
    events.sort(key=lambda row: (row["wall_ns"], row["_ordinal"]))
    return events, manifests


def quantile(values: list[float], q: float) -> float | None:
    """R-7/NumPy-style linear quantile."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float]) -> dict[str, Any]:
    clean = [float(value) for value in values]
    if not clean:
        return {
            "n": 0,
            "sum": 0.0,
            "mean": None,
            "stdev_sample": None,
            "min": None,
            "p05": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p95": None,
            "max": None,
        }
    return {
        "n": len(clean),
        "sum": sum(clean),
        "mean": statistics.fmean(clean),
        "stdev_sample": (
            statistics.stdev(clean) if len(clean) >= 2 else None
        ),
        "min": min(clean),
        "p05": quantile(clean, 0.05),
        "p25": quantile(clean, 0.25),
        "p50": quantile(clean, 0.50),
        "p75": quantile(clean, 0.75),
        "p95": quantile(clean, 0.95),
        "max": max(clean),
    }


def event_slice(
    events: list[dict[str, Any]], start_ns: int, end_ns: int
) -> list[dict[str, Any]]:
    return [
        event for event in events
        if start_ns <= event["wall_ns"] < end_ns
    ]


def session_summaries(
    events: list[dict[str, Any]], cutoff_ns: int
) -> list[dict[str, Any]]:
    starts = [event for event in events if event.get("ev") == "START"]
    summaries = []
    for index, start in enumerate(starts):
        end_ns = (
            starts[index + 1]["wall_ns"]
            if index + 1 < len(starts) else cutoff_ns + 1
        )
        segment = event_slice(events, start["wall_ns"], end_ns)
        dones = [
            event for event in segment
            if event.get("ev") == "PAIR_CYCLE_DONE"
        ]
        counters = [event.get("completed_cycles") for event in dones]
        expected = list(range(1, len(counters) + 1))
        health = [event for event in segment if event.get("ev") == "HEALTH"]
        locks = [event for event in segment if event.get("ev") == "PAIR_LOCK"]
        summaries.append({
            "index": index + 1,
            "start_wall_ns": start["wall_ns"],
            "start_time_utc": iso_ns(start["wall_ns"]),
            "end_wall_ns_exclusive": end_ns,
            "end_time_utc_exclusive": iso_ns(end_ns),
            "duration_s": (end_ns - start["wall_ns"]) / NS_PER_S,
            "start_source": source_ref(start),
            "config": {
                key: start.get(key) for key in (
                    "mode",
                    "clip",
                    "pair",
                    "pair_prequote",
                    "pair_min_depth_ct",
                    "pair_lock_c",
                    "unpaired_age_s",
                    "shadow_queue_sim",
                    "max_open_cost",
                    "max_net",
                )
            },
            "cycle_done_n": len(dones),
            "completed_cycle_counters": counters,
            "completed_cycles_contiguous_from_one": counters == expected,
            "last_pair_locked_total": (
                dones[-1].get("pair_locked_total") if dones else 0.0
            ),
            "last_lock_realized": (
                locks[-1].get("realized") if locks else 0.0
            ),
            "last_health_realized": (
                health[-1].get("realized") if health else None
            ),
            "last_health_source": (
                source_ref(health[-1]) if health else None
            ),
        })
    return summaries


def select_corrected_session(
    summaries: list[dict[str, Any]]
) -> dict[str, Any]:
    if not summaries:
        raise ValueError("no START receipt found")
    latest = summaries[-1]
    config = latest["config"]
    reasons = []
    if config.get("mode") == "shadow":
        reasons.append("mode=shadow")
    if config.get("pair") is True and config.get("pair_prequote") is True:
        reasons.append("pair and pair_prequote enabled")
    if config.get("shadow_queue_sim") is True:
        reasons.append("shadow queue simulation enabled")
    if float(config.get("unpaired_age_s") or -1) == 60.0:
        reasons.append("explicit corrected unpaired_age_s=60")
    if latest["completed_cycles_contiguous_from_one"]:
        reasons.append("completed_cycles is contiguous from 1")
    realized_values = [
        latest["last_pair_locked_total"],
        latest["last_lock_realized"],
        latest["last_health_realized"],
    ]
    finite = [
        float(value) for value in realized_values if value is not None
    ]
    if finite and max(finite) - min(finite) <= 0.00011:
        reasons.append(
            "pair_locked_total/PAIR_LOCK.realized/HEALTH.realized agree"
        )
    if (
        float(config.get("unpaired_age_s") or -1) != 60.0
        or config.get("shadow_queue_sim") is not True
        or not latest["completed_cycles_contiguous_from_one"]
    ):
        raise ValueError(
            "latest START is not the expected corrected shadow session"
        )
    return {
        "selected_session_index": latest["index"],
        "start_wall_ns": latest["start_wall_ns"],
        "start_time_utc": latest["start_time_utc"],
        "selection_reasons": reasons,
        "excluded_earlier_sessions": latest["index"] - 1,
    }


def build_cycles(
    session_events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles = []
    previous_total = 0.0
    previous_done_index = -1
    for event_index, event in enumerate(session_events):
        if event.get("ev") != "PAIR_CYCLE_DONE":
            continue
        interval = session_events[previous_done_index + 1:event_index + 1]
        locks = [row for row in interval if row.get("ev") == "PAIR_LOCK"]
        forced = [
            row for row in interval
            if row.get("ev") == "SHADOW_TAKER_FILL_SIM"
            and row.get("reason") == "unpaired_age"
            and row.get("mt") == event.get("mt")
        ]
        fills = [
            row for row in interval
            if row.get("ev") == "FILL"
            and row.get("ticker") == event.get("mt")
        ]
        external_fills = [
            row for row in fills
            if not (
                str(row.get("fill_id") or "").startswith("shadow:")
                or str(row.get("fill_id") or "").startswith("shadow-ioc-")
                or str(row.get("order_id") or "").startswith("shadow-")
            )
        ]
        pair_total = float(event["pair_locked_total"])
        pnl = pair_total - previous_total
        waits = [
            float(lock["pair_wait_s"])
            for lock in locks if lock.get("pair_wait_s") is not None
        ]
        observed_nets = [
            float(row["net"])
            for row in interval
            if row.get("ev") == "QUOTE_EVAL"
            and row.get("net") is not None
        ]
        cycle_class = (
            "external_fill_contaminated"
            if external_fills
            else "unpaired_age_forced"
            if forced
            else "natural_maker_pair"
        )
        cycle = {
            "completed_cycle": int(event["completed_cycles"]),
            "market": event.get("mt"),
            "class": cycle_class,
            "pnl_usd": pnl,
            "pair_locked_total_after_usd": pair_total,
            "pair_wait_s": max(waits) if waits else None,
            "pair_lock_event_n": len(locks),
            "paired_contracts": sum(
                float(lock.get("ct", 0.0)) for lock in locks
            ),
            "lock_pnl_rounded_sum_usd": sum(
                float(lock.get("locked_usd", 0.0)) for lock in locks
            ),
            "fee_usd": sum(
                float(lock.get("fee_usd", 0.0)) for lock in locks
            ),
            "fill_n": len(fills),
            "external_fill_n": len(external_fills),
            "max_abs_net_observed": (
                max((abs(value) for value in observed_nets), default=None)
            ),
            "limit_breach_event_n": sum(
                row.get("ev") == "LIMIT_BREACH" for row in interval
            ),
            "external_fills": [
                {
                    "side": fill.get("side"),
                    "price_dollars": fill.get("px_dollars"),
                    "count": fill.get("count"),
                    "fee_dollars": fill.get("fee_dollars"),
                    "fill_id": fill.get("fill_id"),
                    "trade_id": fill.get("trade_id"),
                    "order_id": fill.get("order_id"),
                    "source": source_ref(fill),
                }
                for fill in external_fills
            ],
            "first_fill": (
                {
                    "side": fills[0].get("side"),
                    "price_dollars": fills[0].get("px_dollars"),
                    "count": fills[0].get("count"),
                    "exchange_ts_s": fills[0].get("ts_s"),
                    "source": source_ref(fills[0]),
                }
                if fills else None
            ),
            "forced_fill": (
                {
                    "risk_price_dollars": forced[-1].get("risk_px"),
                    "count": forced[-1].get("count"),
                    "fee_usd": forced[-1].get("fee_usd"),
                    "reason": forced[-1].get("reason"),
                    "source": source_ref(forced[-1]),
                }
                if forced else None
            ),
            "pair_locks": [
                {
                    "ct": lock.get("ct"),
                    "px_a": lock.get("px_a"),
                    "px_b": lock.get("px_b"),
                    "locked_usd": lock.get("locked_usd"),
                    "pair_wait_s": lock.get("pair_wait_s"),
                    "fee_usd": lock.get("fee_usd"),
                    "source": source_ref(lock),
                }
                for lock in locks
            ],
            "done_source": source_ref(event),
        }
        cycles.append(cycle)
        previous_total = pair_total
        previous_done_index = event_index
    unresolved_tail = session_events[previous_done_index + 1:]
    return cycles, unresolved_tail


def taker_fee(price: float, count: float = 1.0) -> float:
    p = Decimal(str(price))
    c = Decimal(str(count))
    position_cost = p * c
    raw = Decimal("0.07") * c * p * (Decimal(1) - p)
    total = (position_cost + raw).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING
    )
    return float(total - position_cost)


def target_sample(
    cycles: list[dict[str, Any]],
    session_events: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = [
        cycle for cycle in cycles
        if cycle["class"] == "unpaired_age_forced"
        and cycle["first_fill"] is not None
        and cycle["first_fill"]["side"] == "bid"
        and abs(float(cycle["first_fill"]["price_dollars"]) - 0.13) < 1e-12
        and abs(
            float(cycle["forced_fill"]["risk_price_dollars"]) - 0.96
        ) < 1e-12
    ]
    if not matches:
        raise ValueError("YES 13c -> NO 96c forced sample not found")
    cycle = matches[-1]
    first_ns = cycle["first_fill"]["source"]["wall_ns"]
    force_ns = cycle["forced_fill"]["source"]["wall_ns"]
    market = cycle["market"]
    interval = [
        event for event in session_events
        if first_ns <= event["wall_ns"] <= force_ns
        and event.get("mt") == market
    ]
    quote_evals = [
        event for event in interval if event.get("ev") == "QUOTE_EVAL"
    ]
    if not quote_evals:
        raise ValueError("target sample has no QUOTE_EVAL path")
    prior_places = [
        event for event in session_events
        if event.get("ev") == "INTENT_PLACE"
        and event.get("mt") == market
        and event.get("risk_px") is not None
        and event["wall_ns"] <= first_ns
    ]
    maker_exit = prior_places[-1] if prior_places else None
    entry_price = float(cycle["first_fill"]["price_dollars"])
    path = []
    for checkpoint in CHECKPOINTS_S:
        target_ns = first_ns + round(checkpoint * NS_PER_S)
        available = [
            event for event in quote_evals if event["wall_ns"] <= target_ns
        ]
        observation = available[-1] if available else quote_evals[0]
        yb = int(observation["yb"])
        touch_price = 1.0 - yb / 10_000.0
        touch_fee = taker_fee(touch_price)
        ioc_limit = min(0.99, touch_price + 0.01)
        ioc_fee = taker_fee(ioc_limit)
        path.append({
            "checkpoint_s": checkpoint,
            "method": (
                "last QUOTE_EVAL receipt at or before checkpoint; "
                "no look-ahead"
            ),
            "observation_elapsed_s": (
                observation["wall_ns"] - first_ns
            ) / NS_PER_S,
            "observation_age_at_checkpoint_s": (
                target_ns - observation["wall_ns"]
            ) / NS_PER_S,
            "yes_best_bid_e4": yb,
            "no_touch_ask_dollars": touch_price,
            "touch_depth_contracts": observation.get("y_touch_ct"),
            "touch_taker_fee_usd": touch_fee,
            "touch_pair_pnl_usd": (
                1.0 - entry_price - touch_price - touch_fee
            ),
            "engine_ioc_limit_dollars": ioc_limit,
            "engine_ioc_fee_usd": ioc_fee,
            "engine_ioc_pair_pnl_usd": (
                1.0 - entry_price - ioc_limit - ioc_fee
            ),
            "maker_exit_quote_dollars": (
                maker_exit.get("risk_px") if maker_exit else None
            ),
            "maker_exit_queue_age_s": observation.get("no_order_age_s"),
            "source": source_ref(observation),
        })
    actual_forced = cycle["forced_fill"]
    actual_price = float(actual_forced["risk_price_dollars"])
    actual_fee = float(actual_forced["fee_usd"])
    cancels = [
        event for event in interval
        if event.get("ev") == "INTENT_CANCEL"
        and event.get("reason") == "cancel_maker_before_ioc"
    ]
    return {
        "completed_cycle": cycle["completed_cycle"],
        "market": market,
        "entry": cycle["first_fill"],
        "maker_exit_order": (
            {
                "side": maker_exit.get("side"),
                "risk_price_dollars": maker_exit.get("risk_px"),
                "quantity": maker_exit.get("qty"),
                "source": source_ref(maker_exit),
            }
            if maker_exit else None
        ),
        "maker_cancel_before_ioc": (
            source_ref(cancels[-1]) if cancels else None
        ),
        "forced": actual_forced,
        "elapsed_to_forced_fill_s": (force_ns - first_ns) / NS_PER_S,
        "pair_wait_logged_s": cycle["pair_wait_s"],
        "actual_pair_pnl_usd": cycle["pnl_usd"],
        "pnl_recomputed_usd": (
            1.0 - entry_price - actual_price - actual_fee
        ),
        "checkpoint_path": path,
        "checkpoint_limitation": (
            "QUOTE_EVAL is throttled to roughly 1 Hz. The 0.25s and 0.5s "
            "rows carry forward the first post-fill receipt; this NDJSON "
            "cannot support tick-perfect sub-second book reconstruction."
        ),
    }


def rounded(value: Any, digits: int = 9) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [rounded(item, digits) for item in value]
    if isinstance(value, dict):
        return {
            key: rounded(item, digits) for key, item in value.items()
        }
    return value


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def cents(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.3f}c"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(report: dict[str, Any]) -> str:
    session = report["corrected_session"]
    groups = report["cycle_outcomes"]
    natural = groups["natural_maker_pair"]
    forced = groups["unpaired_age_forced"]
    contaminated = groups["external_fill_contaminated"]
    completion = report["completion"]
    target = report["target_yes_13c_to_no_96c"]
    contaminated_cycles = [
        cycle for cycle in groups["cycles"]
        if cycle["class"] == "external_fill_contaminated"
    ]
    contamination_rows = []
    for cycle in contaminated_cycles:
        fill_text = "; ".join(
            "{side} {price:.1f}c order `{order}` trade `{trade}`".format(
                side=fill["side"],
                price=float(fill["price_dollars"]) * 100,
                order=fill["order_id"],
                trade=fill["trade_id"],
            )
            for fill in cycle["external_fills"]
        )
        contamination_rows.append(
            "- Cycle {cycle}: {fills} non-shadow FILL receipts, "
            "{contracts:g} paired contracts, contribution {pnl}, "
            "max observed |net| {net:g}, LIMIT_BREACH receipts {breaches}. "
            "{fill_text}".format(
                cycle=cycle["completed_cycle"],
                fills=cycle["external_fill_n"],
                contracts=cycle["paired_contracts"],
                pnl=cents(cycle["pnl_usd"]),
                net=cycle["max_abs_net_observed"],
                breaches=cycle["limit_breach_event_n"],
                fill_text=fill_text,
            )
        )
    path_rows = []
    for row in target["checkpoint_path"]:
        path_rows.append(
            "| {cp:g} | {obs:.3f} | {touch:.1f}c | {depth} | {tpnl} | "
            "{limit:.1f}c | {lpnl} | {age} |".format(
                cp=row["checkpoint_s"],
                obs=row["observation_elapsed_s"],
                touch=row["no_touch_ask_dollars"] * 100,
                depth=num(row["touch_depth_contracts"], 2),
                tpnl=cents(row["touch_pair_pnl_usd"]),
                limit=row["engine_ioc_limit_dollars"] * 100,
                lpnl=cents(row["engine_ioc_pair_pnl_usd"]),
                age=num(row["observation_age_at_checkpoint_s"], 3),
            )
        )
    session_rows = []
    for item in report["all_detected_sessions"]:
        session_rows.append(
            "| {index} | {start} | {age} | {cycles} | {total} | {contig} |".format(
                index=item["index"],
                start=item["start_time_utc"],
                age=item["config"].get("unpaired_age_s"),
                cycles=item["cycle_done_n"],
                total=cents(item["last_pair_locked_total"]),
                contig=item["completed_cycles_contiguous_from_one"],
            )
        )
    return f"""# Corrected crypto-MM shadow diagnostic

## Verdict

The source directory is nominally a 24-hour experiment, but the frozen
receipt set contains only **{report['source_window']['duration_s'] / 60:.1f}
minutes**, and the selected corrected session contains **{session['duration_s'] / 60:.1f}
minutes**. These are an early diagnostic, not a 24-hour acceptance result.

The corrected session emitted **{completion['raw_cycle_done_events']} raw
cycle-done receipts**. One receipt was contaminated by
**{contaminated['external_fill_n']} non-shadow fills** and is not a valid
strategy outcome. The clean denominator therefore contains
**{completion['clean_resolved_first_leg_episodes']} first-leg episodes**:
**{natural['pnl_usd']['n']} natural maker pairs** and
**{forced['pnl_usd']['n']} `unpaired_age` forced closes**.

Natural maker completion was
**{pct(completion['natural_completion_rate'])}**, below the empirical
break-even requirement of
**{pct(completion['break_even_natural_completion_rate'])}**. Strategy-native
realized contribution was
**{cents(groups['strategy_native_net_pnl_usd'])}**; the raw engine counter was
**{cents(groups['raw_engine_net_pnl_usd'])}**, including
**{cents(groups['external_contamination_pnl_usd'])}** from the contaminated
cycle.

## Corrected-session boundary

Selected START: `{session['start_time_utc']}` through evidence cutoff
`{session['cutoff_time_utc']}`.

Selection facts:

{chr(10).join('- ' + reason for reason in session['selection_reasons'])}

| session | START UTC | unpaired age | completed cycles | ending P&L | contiguous |
| ---: | --- | ---: | ---: | ---: | --- |
{chr(10).join(session_rows)}

Earlier START segments are excluded because process restarts reset
`completed_cycles` and `realized`. The current segment is internally
continuous from cycle 1 through cycle
{completion['raw_cycle_done_events']}. Counter continuity establishes the
boundary; it does not establish shadow purity.

## Outcome decomposition

| class | n cycles | total | mean | p05 | p50 | p95 | wait p50 | wait p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| natural maker | {natural['pnl_usd']['n']} | {cents(natural['pnl_usd']['sum'])} | {cents(natural['pnl_usd']['mean'])} | {cents(natural['pnl_usd']['p05'])} | {cents(natural['pnl_usd']['p50'])} | {cents(natural['pnl_usd']['p95'])} | {num(natural['pair_wait_s']['p50'])}s | {num(natural['pair_wait_s']['p95'])}s |
| unpaired-age forced | {forced['pnl_usd']['n']} | {cents(forced['pnl_usd']['sum'])} | {cents(forced['pnl_usd']['mean'])} | {cents(forced['pnl_usd']['p05'])} | {cents(forced['pnl_usd']['p50'])} | {cents(forced['pnl_usd']['p95'])} | {num(forced['pair_wait_s']['p50'])}s | {num(forced['pair_wait_s']['p95'])}s |
| external-fill contaminated | {contaminated['pnl_usd']['n']} | {cents(contaminated['pnl_usd']['sum'])} | {cents(contaminated['pnl_usd']['mean'])} | {cents(contaminated['pnl_usd']['p05'])} | {cents(contaminated['pnl_usd']['p50'])} | {cents(contaminated['pnl_usd']['p95'])} | {num(contaminated['pair_wait_s']['p50'])}s | {num(contaminated['pair_wait_s']['p95'])}s |

Definitions:

- Natural completion rate = natural maker closes / clean resolved first-leg
  episodes. Any cycle interval containing a non-shadow `FILL` is excluded
  from both numerator and denominator.
- Break-even rate uses the empirical class means:
  `-mean(forced) / (mean(natural) - mean(forced))`.
- Mechanical closure includes forced IOC exits. At cutoff it was
  **{pct(completion['mechanical_closure_rate'])}**, with
  **{completion['open_first_leg_episodes_at_cutoff']}** unresolved episode(s).
- P&L comes from consecutive `PAIR_CYCLE_DONE.pair_locked_total` differences,
  so partial `PAIR_LOCK` receipts are not double-counted.

## Shadow-purity exception

{chr(10).join(contamination_rows)}

The raw ending counter remains reconciled, but this cycle cannot be used to
estimate strategy-native natural completion or outcome distributions.

## YES 13c → NO 96c forced-close replay

- Market: `{target['market']}`
- First fill: YES at **13.0c**
- Preserved maker exit: NO at
  **{float(target['maker_exit_order']['risk_price_dollars']) * 100:.1f}c**
- Forced fill: NO at
  **{float(target['forced']['risk_price_dollars']) * 100:.1f}c** plus
  **{float(target['forced']['fee_usd']) * 100:.3f}c** fee
- Receipt wait: **{target['pair_wait_logged_s']:.3f}s**
- Realized result: **{cents(target['actual_pair_pnl_usd'])}**

| target after first fill | observed at | touch NO ask | touch depth | touch-cross pair P&L | engine +1c IOC limit | conservative IOC pair P&L | quote age |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(path_rows)}

The path shows the practical failure mode: the old 85c maker exit retained
queue age while the executable NO ask moved away. An immediate touch cross
was already outside the 99c maker-pair objective, but its modeled loss was
far smaller than waiting for the 60-second 96c forced fill.

Sub-second limitation: {target['checkpoint_limitation']}

## Evidence integrity

- Source snapshot cutoff: `{report['source_window']['cutoff_time_utc']}`
- Source files: {len(report['source_manifest'])}
- Parsed receipts: {report['source_window']['receipt_n']}
- Malformed receipts: 0
- No engine/service mutation and no trading action were performed.

Exact cycle rows, distributions, checkpoint sources, source hashes, and
session-boundary receipts are in `shadow_24h_diagnostic.json`.
"""


def main() -> None:
    events, manifest = load_events()
    cutoff_ns = events[-1]["wall_ns"]
    window_start_ns = cutoff_ns - 24 * 60 * 60 * NS_PER_S
    window_events = [
        event for event in events if event["wall_ns"] >= window_start_ns
    ]
    summaries = session_summaries(window_events, cutoff_ns)
    selection = select_corrected_session(summaries)
    session_events = [
        event for event in window_events
        if event["wall_ns"] >= selection["start_wall_ns"]
    ]
    cycles, unresolved_tail = build_cycles(session_events)
    groups: dict[str, dict[str, Any]] = {}
    for class_name in (
        "natural_maker_pair",
        "unpaired_age_forced",
        "external_fill_contaminated",
    ):
        selected = [
            cycle for cycle in cycles if cycle["class"] == class_name
        ]
        groups[class_name] = {
            "pnl_usd": distribution(
                cycle["pnl_usd"] for cycle in selected
            ),
            "pair_wait_s": distribution(
                cycle["pair_wait_s"] for cycle in selected
                if cycle["pair_wait_s"] is not None
            ),
            "fee_usd": distribution(
                cycle["fee_usd"] for cycle in selected
            ),
            "pair_lock_event_n": sum(
                cycle["pair_lock_event_n"] for cycle in selected
            ),
            "paired_contracts": sum(
                cycle["paired_contracts"] for cycle in selected
            ),
            "cycle_ids": [
                cycle["completed_cycle"] for cycle in selected
            ],
            "external_fill_n": sum(
                cycle["external_fill_n"] for cycle in selected
            ),
        }
    natural_n = groups["natural_maker_pair"]["pnl_usd"]["n"]
    forced_n = groups["unpaired_age_forced"]["pnl_usd"]["n"]
    contaminated_n = groups["external_fill_contaminated"]["pnl_usd"]["n"]
    clean_resolved = natural_n + forced_n
    natural_rate = (
        natural_n / clean_resolved if clean_resolved else None
    )
    natural_mean = groups["natural_maker_pair"]["pnl_usd"]["mean"]
    forced_mean = groups["unpaired_age_forced"]["pnl_usd"]["mean"]
    break_even = None
    if (
        natural_mean is not None
        and forced_mean is not None
        and natural_mean > 0
        and forced_mean < 0
    ):
        break_even = -forced_mean / (natural_mean - forced_mean)
    last_health = next(
        (
            event for event in reversed(session_events)
            if event.get("ev") == "HEALTH"
        ),
        None,
    )
    open_at_cutoff = int(
        bool(
            last_health
            and (
                abs(float(last_health.get("open_cost", 0.0))) > 1e-9
                or int(last_health.get("orders", 0)) > 0
            )
            and any(event.get("ev") == "FILL" for event in unresolved_tail)
        )
    )
    raw_attempted = len(cycles) + open_at_cutoff
    raw_net_pnl = sum(cycle["pnl_usd"] for cycle in cycles)
    strategy_native_net_pnl = sum(
        cycle["pnl_usd"] for cycle in cycles
        if cycle["class"] != "external_fill_contaminated"
    )
    contamination_pnl = groups[
        "external_fill_contaminated"
    ]["pnl_usd"]["sum"]
    latest_summary = summaries[-1]
    report = {
        "schema": "crypto-mm-corrected-shadow-diagnostic-v2",
        "generated_from_frozen_receipts": True,
        "read_only": True,
        "nominal_requested_window_hours": 24,
        "source_window": {
            "start_wall_ns": window_events[0]["wall_ns"],
            "start_time_utc": iso_ns(window_events[0]["wall_ns"]),
            "cutoff_wall_ns": cutoff_ns,
            "cutoff_time_utc": iso_ns(cutoff_ns),
            "duration_s": (
                cutoff_ns - window_events[0]["wall_ns"]
            ) / NS_PER_S,
            "receipt_n": len(window_events),
            "event_counts": dict(sorted(Counter(
                event.get("ev") for event in window_events
            ).items())),
            "coverage_warning": (
                "Available receipts cover less than 24 hours; metrics are "
                "not a 24-hour acceptance result."
            ),
        },
        "source_manifest": manifest,
        "all_detected_sessions": summaries,
        "corrected_session": {
            **selection,
            "cutoff_wall_ns": cutoff_ns,
            "cutoff_time_utc": iso_ns(cutoff_ns),
            "duration_s": (
                cutoff_ns - selection["start_wall_ns"]
            ) / NS_PER_S,
            "receipt_n": len(session_events),
            "config": latest_summary["config"],
            "ending_pair_locked_total_usd": (
                latest_summary["last_pair_locked_total"]
            ),
            "ending_health_realized_usd": (
                latest_summary["last_health_realized"]
            ),
        },
        "cycle_outcomes": {
            **groups,
            "raw_engine_net_pnl_usd": raw_net_pnl,
            "strategy_native_net_pnl_usd": strategy_native_net_pnl,
            "external_contamination_pnl_usd": contamination_pnl,
            "cycle_n": len(cycles),
            "cycles": cycles,
        },
        "completion": {
            "raw_cycle_done_events": len(cycles),
            "clean_resolved_first_leg_episodes": clean_resolved,
            "natural_maker_completions": natural_n,
            "unpaired_age_forced_completions": forced_n,
            "external_fill_contaminated_cycle_events": contaminated_n,
            "natural_completion_rate": natural_rate,
            "forced_completion_rate": (
                forced_n / clean_resolved if clean_resolved else None
            ),
            "break_even_natural_completion_rate": break_even,
            "completion_rate_minus_break_even": (
                natural_rate - break_even
                if natural_rate is not None and break_even is not None
                else None
            ),
            "open_first_leg_episodes_at_cutoff": open_at_cutoff,
            "mechanical_closure_rate": (
                len(cycles) / raw_attempted if raw_attempted else None
            ),
            "definition": (
                "A first-leg episode begins when maker simulation leaves "
                "unpaired inventory. Natural completion means the opposite "
                "maker leg fills before unpaired_age; forced completion "
                "contains SHADOW_TAKER_FILL_SIM reason=unpaired_age. Cycles "
                "containing non-shadow FILL receipts are excluded from both "
                "clean classes."
            ),
        },
        "target_yes_13c_to_no_96c": target_sample(
            cycles, session_events
        ),
        "integrity_checks": {
            "completed_cycles_contiguous": (
                [cycle["completed_cycle"] for cycle in cycles]
                == list(range(1, len(cycles) + 1))
            ),
            "cycle_pnl_sum_matches_ending_total": abs(
                raw_net_pnl
                - float(latest_summary["last_pair_locked_total"])
            ) <= 1e-8,
            "last_health_realized_matches_ending_total": (
                latest_summary["last_health_realized"] is not None
                and abs(
                    float(latest_summary["last_health_realized"])
                    - float(latest_summary["last_pair_locked_total"])
                ) <= 0.00011
            ),
            "target_pnl_recomputes": abs(
                target_sample(cycles, session_events)[
                    "actual_pair_pnl_usd"
                ]
                - target_sample(cycles, session_events)[
                    "pnl_recomputed_usd"
                ]
            ) <= 1e-9,
        },
    }
    report = rounded(report)
    JSON_OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    MD_OUT.write_text(render_markdown(report), encoding="utf-8")
    hash_paths = [
        Path(__file__).resolve(),
        JSON_OUT,
        MD_OUT,
        *sorted(SOURCE_DIR.glob("mm_*.ndjson")),
    ]
    SUMS_OUT.write_text(
        "".join(
            f"{sha256(path)}  {path.relative_to(ROOT)}\n"
            for path in hash_paths
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
