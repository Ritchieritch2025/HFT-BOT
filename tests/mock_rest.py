#!/usr/bin/env python3
"""Mock of the Kalshi v2 REST API for typed-endpoint tests.

Usage: mock_rest.py <port>

Routes (all under /trade-api/v2):
  GET /exchange/status                       -> active status
  GET /markets?...&series_ticker=SCENARIO    -> pagination / 429 matrix / errors
  GET /markets/{ticker}                      -> single market
  GET /markets/{ticker}/orderbook            -> dollar-string book, or cents for
                                                ticker CENTS-MKT

Scenarios selected via the series_ticker query param so they route through the
typed markets() method:
  PAGED         two pages via cursor, then empty cursor
  RETRY_NONE    429 (no Retry-After) x2, then 200
  RETRY_DELTA   429 + "Retry-After: 3600" x1, then 200 (tests clamp)
  RETRY_GARBAGE 429 + "Retry-After: notanumber" x1, then 200 (treated absent)
  ERR_AUTH      401 with a Kalshi error body
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

lock = threading.Lock()
counters = {}


def bump(key):
    with lock:
        counters[key] = counters.get(key, 0) + 1
        return counters[key]


def market_obj(ticker, status="active"):
    return {
        "ticker": ticker,
        "status": status,
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4500",
        "last_price_dollars": "0.4300",
        "volume_fp": "1234.00",
        "open_interest_fp": "5000.00",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, extra_headers=None):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        # Debug: report how many non-debug requests the server has served. Used by
        # test_request_executor to prove a locally-refused reservation emits no HTTP.
        if path == "/trade-api/v2/debug/requests":
            with lock:
                return self._send(200, {"count": counters.get("served", 0)})
        bump("served")  # count every real (non-debug) request that reaches the server

        # Debug: a 200 with a non-JSON body (malformed response handling).
        if path == "/trade-api/v2/debug/malformed":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = b"not json {{{"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if path == "/trade-api/v2/exchange/status":
            return self._send(200, {"exchange_active": True, "trading_active": True})

        if path == "/trade-api/v2/account/limits":
            # Nested v3.23.0 schema: usage_tier + read/write BucketLimit + grants.
            return self._send(200, {
                "usage_tier": "advanced",
                "read": {"refill_rate": 300, "bucket_capacity": 300},
                "write": {"refill_rate": 300, "bucket_capacity": 300},
                "grants": [
                    {"exchange_instance": "event_contract", "level": "advanced",
                     "source": "volume"},  # permanent (no expires_ts)
                    {"exchange_instance": "margined", "level": "advanced",
                     "source": "manual", "expires_ts": 1893456000},  # expiring
                ]})

        if path == "/trade-api/v2/account/endpoint_costs":
            return self._send(200, {"default_cost": 10, "endpoint_costs": [
                {"method": "POST", "path": "/trade-api/v2/portfolio/orders", "cost": 10},
                {"method": "DELETE", "path": "/trade-api/v2/portfolio/orders/{order_id}", "cost": 2},
                {"method": "GET", "path": "/trade-api/v2/markets", "cost": 1},
            ]})

        if path == "/trade-api/v2/markets/orderbooks":
            tickers = qs.get("tickers", [])
            obs = []
            for t in tickers:
                obs.append({"market_ticker": t, "orderbook_fp": {
                    "yes_dollars": [["0.4200", "100.00"]],
                    "no_dollars": [["0.5500", "20.00"]]}})
            return self._send(200, {"orderbooks": obs})

        if path == "/trade-api/v2/markets":
            series = (qs.get("series_ticker", [""])[0])
            cursor = (qs.get("cursor", [""])[0])

            if series == "RETRY_NONE":
                if bump("RETRY_NONE") <= 2:
                    return self._send(429, {"error": {"code": "rate_limit", "message": "slow down"}})
                return self._send(200, {"markets": [market_obj("RN-1")], "cursor": ""})
            if series == "RETRY_DELTA":
                if bump("RETRY_DELTA") <= 1:
                    return self._send(429, {"error": {"code": "rate_limit", "message": "wait"}},
                                      {"Retry-After": "3600"})
                return self._send(200, {"markets": [market_obj("RD-1")], "cursor": ""})
            if series == "RETRY_GARBAGE":
                if bump("RETRY_GARBAGE") <= 1:
                    return self._send(429, {"error": {"code": "rate_limit", "message": "wait"}},
                                      {"Retry-After": "notanumber"})
                return self._send(200, {"markets": [market_obj("RG-1")], "cursor": ""})
            if series == "ERR_AUTH":
                return self._send(401, {"error": {
                    "code": "authentication_error",
                    "message": "invalid signature", "details": "bad"}})
            if series == "PAGED":
                if cursor == "":
                    return self._send(200, {"markets": [market_obj("P-1"), market_obj("P-2")],
                                            "cursor": "PAGE2"})
                if cursor == "PAGE2":
                    return self._send(200, {"markets": [market_obj("P-3")], "cursor": ""})
                return self._send(200, {"markets": [], "cursor": ""})

            # default: a couple of markets, no cursor
            return self._send(200, {"markets": [market_obj("DEF-1")], "cursor": ""})

        if path.endswith("/orderbook") and path.startswith("/trade-api/v2/markets/"):
            ticker = path[len("/trade-api/v2/markets/"):-len("/orderbook")]
            if ticker == "CENTS-MKT":
                # legacy integer-cents schema
                return self._send(200, {"orderbook": {
                    "yes": [[42, 100], [8, 300]],
                    "no": [[55, 20], [56, 146]]}})
            return self._send(200, {"orderbook_fp": {
                "yes_dollars": [["0.4200", "100.00"], ["0.0800", "300.00"]],
                "no_dollars": [["0.5500", "20.00"], ["0.5600", "146.00"]]}})

        if path.startswith("/trade-api/v2/markets/"):
            ticker = path[len("/trade-api/v2/markets/"):]
            return self._send(200, {"market": market_obj(ticker)})

        return self._send(404, {"error": {"code": "not_found", "message": path}})


def main():
    port = int(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock_rest on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
