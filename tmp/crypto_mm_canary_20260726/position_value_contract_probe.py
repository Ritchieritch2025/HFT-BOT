#!/usr/bin/env python3
"""One-contract round trip to prove live position-value accounting semantics."""

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path


def iso_epoch(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def call(engine, method, path, body=None):
    code, payload = engine.rest(method, path, body, host=engine.V2O)
    return code, payload if isinstance(payload, dict) else {"raw": payload}


def account_snapshot(engine):
    cb, balance = call(
        engine, "GET", "/portfolio/balance?subaccount=0&exchange_index=0"
    )
    cp, positions = call(
        engine,
        "GET",
        "/portfolio/positions?count_filter=position&limit=1000&subaccount=0",
    )
    co, orders = call(
        engine,
        "GET",
        "/portfolio/orders?status=resting&limit=1000&subaccount=0",
    )
    return {
        "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "balance_code": cb,
        "balance": {
            key: balance.get(key)
            for key in (
                "balance",
                "balance_dollars",
                "portfolio_value",
                "updated_ts",
                "balance_breakdown",
            )
        },
        "positions_code": cp,
        "market_positions": positions.get("market_positions", []),
        "resting_orders_code": co,
        "resting_orders": orders.get("orders", []),
    }


def best_bid(orderbook, side):
    levels = orderbook.get("orderbook_fp", {}).get(f"{side}_dollars", [])
    if not isinstance(levels, list) or not levels:
        return None
    return max(float(level[0]) for level in levels if len(level) >= 2)


def post_ioc(
    engine,
    *,
    ticker,
    side,
    price,
    reduce_only,
    label,
):
    body = {
        "ticker": ticker,
        "client_order_id": f"budget-position-probe-{label}-{uuid.uuid4()}",
        "side": side,
        "count": "1.00",
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "reduce_only": bool(reduce_only),
        "subaccount": 0,
        "exchange_index": 0,
    }
    code, payload = call(engine, "POST", "/portfolio/events/orders", body)
    return {
        "code": code,
        "request": body,
        "response": payload,
    }


def fill_count(result):
    payload = result.get("response", {})
    nested = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    raw = payload.get("fill_count", nested.get("fill_count", "0"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def market_position(snapshot, ticker):
    for row in snapshot.get("market_positions", []):
        if row.get("ticker") == ticker:
            return row
    return None


def refresh_book(engine, ticker):
    code, payload = call(engine, "GET", f"/markets/{ticker}/orderbook")
    if code != 200:
        raise RuntimeError(f"orderbook HTTP {code}")
    yes_bid = best_bid(payload, "yes")
    no_bid = best_bid(payload, "no")
    if yes_bid is None or no_bid is None:
        raise RuntimeError("two-sided orderbook required")
    return yes_bid, no_bid


def exit_once(engine, ticker, direction, emergency=False):
    yes_bid, no_bid = refresh_book(engine, ticker)
    if direction == "yes":
        side = "ask"
        price = 0.001 if emergency else max(0.001, yes_bid - 0.010)
    else:
        side = "bid"
        yes_ask = 1.0 - no_bid
        price = 0.999 if emergency else min(0.999, yes_ask + 0.010)
    return post_ioc(
        engine,
        ticker=ticker,
        side=side,
        price=price,
        reduce_only=True,
        label="emergency-exit" if emergency else "exit",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.release_dir)
    import mm_engine as engine

    report = {
        "schema": "kalshi-position-value-contract-probe-v1",
        "probe_kind": "authenticated-production-one-contract-round-trip",
        "subaccount": 0,
        "exchange_index": 0,
        "max_entry_risk_dollars": "0.4500",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    ticker = None
    direction = None
    entry_filled = False

    before = account_snapshot(engine)
    report["before"] = before
    if (
        before["balance_code"] != 200
        or before["positions_code"] != 200
        or before["resting_orders_code"] != 200
        or before["market_positions"]
        or before["resting_orders"]
    ):
        raise RuntimeError("probe requires a proven flat account")

    code, payload = call(
        engine,
        "GET",
        "/markets?series_ticker=KXBTC15M&status=open&limit=100",
    )
    if code != 200:
        raise RuntimeError(f"markets HTTP {code}")
    now = time.time()
    candidates = []
    for market in payload.get("markets", []):
        try:
            tte = iso_epoch(market["close_time"]) - now
        except (KeyError, TypeError, ValueError):
            continue
        if market.get("status") == "active" and 180 < tte < 900:
            candidates.append((tte, market["ticker"]))
    if not candidates:
        raise RuntimeError("no active market with sufficient time remaining")
    ticker = max(candidates)[1]
    yes_bid, no_bid = refresh_book(engine, ticker)
    yes_ask = 1.0 - no_bid
    no_ask = 1.0 - yes_bid
    # The IOC limit crosses one whole-cent mid-band tick through the touch,
    # so require the executable touch itself to be <=44c.  This preserves a
    # strict <=45c price cap even if the top level disappears in flight.
    if min(yes_ask, no_ask) > 0.44:
        raise RuntimeError("cheapest outcome exceeds the 44-cent touch cap")

    if yes_ask <= no_ask:
        direction = "yes"
        entry_side = "bid"
        entry_price = min(0.999, yes_ask + 0.010)
        entry_risk = yes_ask
    else:
        direction = "no"
        entry_side = "ask"
        entry_price = max(0.001, yes_bid - 0.010)
        entry_risk = no_ask
    report.update(
        {
            "ticker": ticker,
            "direction": direction,
            "pre_entry_book": {
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
            },
            "estimated_entry_risk_dollars": round(entry_risk, 4),
        }
    )

    try:
        entry = post_ioc(
            engine,
            ticker=ticker,
            side=entry_side,
            price=entry_price,
            reduce_only=False,
            label="entry",
        )
        report["entry"] = entry
        if entry["code"] not in (200, 201):
            raise RuntimeError(f"entry HTTP {entry['code']}")
        entry_filled = fill_count(entry) > 0
        if not entry_filled:
            raise RuntimeError("entry IOC did not fill")

        open_snapshots = []
        for _ in range(15):
            snap = account_snapshot(engine)
            open_snapshots.append(snap)
            if market_position(snap, ticker) is not None:
                break
            time.sleep(0.15)
        report["while_position_open"] = open_snapshots
        if market_position(open_snapshots[-1], ticker) is None:
            raise RuntimeError("filled entry never became visible in positions")

        exit_result = exit_once(engine, ticker, direction)
        report["exit"] = exit_result
        if exit_result["code"] not in (200, 201) or fill_count(exit_result) <= 0:
            report["emergency_exit"] = exit_once(
                engine, ticker, direction, emergency=True
            )
    finally:
        cleanup_snapshots = []
        if ticker is not None and entry_filled:
            for attempt in range(20):
                snap = account_snapshot(engine)
                cleanup_snapshots.append(snap)
                if market_position(snap, ticker) is None:
                    break
                if attempt in (3, 8, 13):
                    report.setdefault("cleanup_exits", []).append(
                        exit_once(engine, ticker, direction, emergency=True)
                    )
                time.sleep(0.15)
        report["after"] = cleanup_snapshots
        report["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        report["flat_after"] = bool(
            not cleanup_snapshots
            or market_position(cleanup_snapshots[-1], ticker) is None
        )
        Path(args.out).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))

    if not report["flat_after"]:
        raise RuntimeError("position probe failed to flatten")


if __name__ == "__main__":
    main()
