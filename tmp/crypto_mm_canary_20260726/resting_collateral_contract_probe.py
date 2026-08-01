#!/usr/bin/env python3
"""One-contract production probe for Kalshi resting-order collateral semantics.

The probe refuses to run unless the bound primary subaccount is flat and has
no resting orders.  It posts one 0.1-cent YES bid far below the live touch,
captures authenticated account snapshots, and cancels the exact client/order
identity in a finally block.  It is not a strategy test.
"""

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path


def iso_to_epoch(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def call(engine, method, path, body=None):
    code, payload = engine.rest(method, path, body, host=engine.V2O)
    return {
        "code": code,
        "payload": payload if isinstance(payload, dict) else {"raw": payload},
    }


def rows(result, key):
    payload = result.get("payload")
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, list) else []


def balance_view(result):
    payload = result["payload"]
    return {
        key: payload.get(key)
        for key in (
            "balance",
            "balance_dollars",
            "portfolio_value",
            "updated_ts",
            "balance_breakdown",
        )
    }


def active_order_views(result, client_order_id):
    return [
        {
            key: row.get(key)
            for key in (
                "order_id",
                "client_order_id",
                "ticker",
                "status",
                "outcome_side",
                "book_side",
                "action",
                "yes_price_dollars",
                "no_price_dollars",
                "remaining_count_fp",
                "initial_count_fp",
                "subaccount_number",
            )
        }
        for row in rows(result, "orders")
        if row.get("client_order_id") == client_order_id
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.release_dir)
    import mm_engine as engine

    report = {
        "schema": "kalshi-resting-collateral-contract-probe-v1",
        "probe_kind": "authenticated-production-one-contract",
        "subaccount": 0,
        "exchange_index": 0,
        "price_dollars": "0.0010",
        "count": "1.00",
        "max_contract_cost_dollars": "0.0010",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    client_order_id = f"budget-contract-probe-{uuid.uuid4()}"
    order_id = None
    ticker = None
    started_s = int(time.time())

    before_orders = call(
        engine,
        "GET",
        "/portfolio/orders?status=resting&limit=1000&subaccount=0",
    )
    before_positions = call(
        engine,
        "GET",
        "/portfolio/positions?count_filter=position&limit=1000&subaccount=0",
    )
    before_balance = call(
        engine,
        "GET",
        "/portfolio/balance?subaccount=0&exchange_index=0",
    )
    report["before"] = {
        "balance_code": before_balance["code"],
        "balance": balance_view(before_balance),
        "resting_orders_code": before_orders["code"],
        "resting_orders_count": len(rows(before_orders, "orders")),
        "positions_code": before_positions["code"],
        "positions_count": len(rows(before_positions, "market_positions")),
    }
    if (
        before_orders["code"] != 200
        or before_positions["code"] != 200
        or before_balance["code"] != 200
        or rows(before_orders, "orders")
        or rows(before_positions, "market_positions")
    ):
        raise RuntimeError("probe requires a proven flat account with no resting orders")

    markets = call(
        engine,
        "GET",
        "/markets?series_ticker=KXBTC15M&status=open&limit=100",
    )
    now = time.time()
    candidates = []
    for market in rows(markets, "markets"):
        try:
            tte = iso_to_epoch(market["close_time"]) - now
            bid = float(market["yes_bid_dollars"])
            ask = float(market["yes_ask_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if market.get("status") == "active" and 180 < tte < 900 and bid > 0.001 and ask > 0.001:
            candidates.append((tte, market))
    if not candidates:
        raise RuntimeError("no safely distant active KXBTC15M market")
    ticker = max(candidates, key=lambda item: item[0])[1]["ticker"]
    report["ticker"] = ticker

    body = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": "bid",
        "count": "1.00",
        "price": "0.0010",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "maker",
        "post_only": True,
        "cancel_order_on_pause": True,
        "subaccount": 0,
        "exchange_index": 0,
    }

    try:
        created = call(engine, "POST", "/portfolio/events/orders", body)
        report["create"] = {
            "code": created["code"],
            "response": created["payload"],
        }
        payload = created["payload"]
        nested = payload.get("order") if isinstance(payload.get("order"), dict) else {}
        order_id = (
            payload.get("order_id")
            or payload.get("id")
            or nested.get("order_id")
        )
        if created["code"] not in (200, 201):
            raise RuntimeError(f"create rejected with HTTP {created['code']}")

        observations = []
        for _ in range(8):
            live_orders = call(
                engine,
                "GET",
                f"/portfolio/orders?status=resting&ticker={ticker}"
                "&limit=1000&subaccount=0",
            )
            if order_id is None:
                matches = active_order_views(live_orders, client_order_id)
                if matches:
                    order_id = matches[0]["order_id"]
            live_balance = call(
                engine,
                "GET",
                "/portfolio/balance?subaccount=0&exchange_index=0",
            )
            user_ts = call(engine, "GET", "/exchange/user_data_timestamp")
            observations.append(
                {
                    "observed_at_utc":
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    "balance_code": live_balance["code"],
                    "balance": balance_view(live_balance),
                    "orders_code": live_orders["code"],
                    "matching_orders":
                        active_order_views(live_orders, client_order_id),
                    "user_data_timestamp_code": user_ts["code"],
                    "user_data_timestamp":
                        user_ts["payload"].get("as_of_time"),
                }
            )
            if observations[-1]["matching_orders"]:
                break
            time.sleep(0.2)
        report["while_resting"] = observations
        if order_id is None:
            raise RuntimeError("create succeeded but exact order identity is unresolved")
    finally:
        cleanup = []
        if ticker is not None:
            live_orders = call(
                engine,
                "GET",
                f"/portfolio/orders?status=resting&ticker={ticker}"
                "&limit=1000&subaccount=0",
            )
            identities = {
                row.get("order_id")
                for row in rows(live_orders, "orders")
                if row.get("client_order_id") == client_order_id
                and isinstance(row.get("order_id"), str)
            }
            if isinstance(order_id, str):
                identities.add(order_id)
            for identity in sorted(identities):
                cleanup.append(
                    {
                        "order_id": identity,
                        "cancel": call(
                            engine,
                            "DELETE",
                            f"/portfolio/events/orders/{identity}",
                        ),
                    }
                )
        report["cleanup"] = cleanup

        after_observations = []
        for _ in range(12):
            after_orders = call(
                engine,
                "GET",
                "/portfolio/orders?status=resting&limit=1000&subaccount=0",
            )
            after_balance = call(
                engine,
                "GET",
                "/portfolio/balance?subaccount=0&exchange_index=0",
            )
            after_positions = call(
                engine,
                "GET",
                "/portfolio/positions?count_filter=position"
                "&limit=1000&subaccount=0",
            )
            fills = (
                call(
                    engine,
                    "GET",
                    f"/portfolio/fills?ticker={ticker}&min_ts={started_s - 5}"
                    "&limit=1000&subaccount=0",
                )
                if ticker is not None
                else {"code": -1, "payload": {"fills": []}}
            )
            matching_fills = [
                {
                    key: fill.get(key)
                    for key in (
                        "order_id",
                        "ticker",
                        "outcome_side",
                        "book_side",
                        "count_fp",
                        "yes_price_dollars",
                        "no_price_dollars",
                        "fee_cost",
                        "created_time",
                        "subaccount_number",
                    )
                }
                for fill in rows(fills, "fills")
                if order_id is not None and fill.get("order_id") == order_id
            ]
            after_observations.append(
                {
                    "observed_at_utc":
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    "balance_code": after_balance["code"],
                    "balance": balance_view(after_balance),
                    "resting_orders_code": after_orders["code"],
                    "resting_orders_count": len(rows(after_orders, "orders")),
                    "positions_code": after_positions["code"],
                    "positions_count":
                        len(rows(after_positions, "market_positions")),
                    "fills_code": fills["code"],
                    "matching_fills": matching_fills,
                }
            )
            if (
                after_orders["code"] == 200
                and not rows(after_orders, "orders")
                and after_balance["code"] == 200
            ):
                break
            time.sleep(0.2)
        report["after"] = after_observations
        report["finished_at_utc"] = (
            dt.datetime.now(dt.timezone.utc).isoformat()
        )
        Path(args.out).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
