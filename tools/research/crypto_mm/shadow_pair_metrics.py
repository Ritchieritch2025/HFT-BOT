#!/usr/bin/env python3
"""Deterministic metrics for one shadow engine run.

The log directory is append-only across process starts.  By default this
tool finds the latest START receipt and ignores every earlier row, preventing
warm-up/restart rows from contaminating a strategy comparison.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
from typing import Iterable, Iterator, Mapping


ZERO = Decimal("0")


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is not numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal error varies
        raise ValueError(f"{field} is not numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} is not finite")
    return result


def iter_rows(paths: Iterable[Path]) -> Iterator[dict[str, object]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{path}:{line_number}: malformed JSON"
                    ) from exc
                if not isinstance(row, dict):
                    raise ValueError(
                        f"{path}:{line_number}: row is not an object"
                    )
                yield row


def latest_start_wall_ns(paths: Iterable[Path]) -> int:
    starts: list[int] = []
    for row in iter_rows(paths):
        if row.get("ev") != "START":
            continue
        wall_ns = row.get("wall_ns")
        if isinstance(wall_ns, bool) or not isinstance(wall_ns, int):
            raise ValueError("START wall_ns must be an integer")
        starts.append(wall_ns)
    if not starts:
        raise ValueError("no START receipt found")
    return max(starts)


def _mean(total: Decimal, count: int) -> Decimal | None:
    return total / Decimal(count) if count else None


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def summarize(
    rows: Iterable[Mapping[str, object]],
    *,
    start_wall_ns: int,
) -> dict[str, object]:
    natural_n = 0
    forced_n = 0
    natural_pnl = ZERO
    forced_pnl = ZERO
    natural_contracts = ZERO
    forced_contracts = ZERO
    forced_fees = ZERO
    first_wall_ns: int | None = None
    last_wall_ns: int | None = None

    for row in rows:
        wall_ns = row.get("wall_ns")
        if isinstance(wall_ns, bool) or not isinstance(wall_ns, int):
            continue
        if wall_ns < start_wall_ns or row.get("ev") != "PAIR_LOCK":
            continue

        pnl = _decimal(row.get("locked_usd"), "locked_usd")
        fee = _decimal(row.get("fee_usd"), "fee_usd")
        contracts = _decimal(row.get("ct"), "ct")
        if contracts <= ZERO or fee < ZERO:
            raise ValueError("PAIR_LOCK quantity/fee is invalid")

        first_wall_ns = (
            wall_ns if first_wall_ns is None else min(first_wall_ns, wall_ns)
        )
        last_wall_ns = (
            wall_ns if last_wall_ns is None else max(last_wall_ns, wall_ns)
        )

        # KXBTC15M maker multiplier is zero in this frozen shadow release.
        # A positive PAIR_LOCK fee therefore identifies the taker close.
        if fee == ZERO:
            natural_n += 1
            natural_pnl += pnl
            natural_contracts += contracts
        else:
            forced_n += 1
            forced_pnl += pnl
            forced_contracts += contracts
            forced_fees += fee

    total_n = natural_n + forced_n
    avg_gain = _mean(natural_pnl, natural_n)
    avg_forced_loss = (
        -_mean(forced_pnl, forced_n)
        if forced_n and forced_pnl < ZERO
        else None
    )
    break_even: Decimal | None = None
    if (
        avg_gain is not None
        and avg_forced_loss is not None
        and avg_gain > ZERO
    ):
        break_even = avg_forced_loss / (avg_gain + avg_forced_loss)

    return {
        "schema_version": "crypto-mm-shadow-pair-metrics-v1",
        "start_wall_ns": start_wall_ns,
        "first_pair_wall_ns": first_wall_ns,
        "last_pair_wall_ns": last_wall_ns,
        "pair_lock_count": total_n,
        "natural_maker_pair": {
            "count": natural_n,
            "contracts": _text(natural_contracts),
            "pnl_usd": _text(natural_pnl),
            "mean_pnl_usd": _text(avg_gain),
        },
        "forced_taker_close": {
            "count": forced_n,
            "contracts": _text(forced_contracts),
            "pnl_usd": _text(forced_pnl),
            "fees_usd": _text(forced_fees),
            "mean_loss_usd": _text(avg_forced_loss),
        },
        "observed_natural_completion_rate": _text(
            Decimal(natural_n) / Decimal(total_n) if total_n else None
        ),
        "break_even_completion_rate_from_observed_severity": _text(
            break_even
        ),
        "total_pnl_usd": _text(natural_pnl + forced_pnl),
        "classification": (
            "fee_usd==0 maker pair; fee_usd>0 taker close; "
            "valid for frozen KXBTC15M zero-maker-fee shadow release"
        ),
    }


def report(paths: Iterable[Path]) -> dict[str, object]:
    ordered = tuple(sorted(paths))
    if not ordered:
        raise ValueError("no log paths supplied")
    start_wall_ns = latest_start_wall_ns(ordered)
    return summarize(iter_rows(ordered), start_wall_ns=start_wall_ns)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", type=Path, nargs="+")
    args = parser.parse_args()
    print(
        json.dumps(
            report(args.logs),
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
