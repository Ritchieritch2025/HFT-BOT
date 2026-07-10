#!/usr/bin/env python3
"""Mock exchange for the panic-CLI drills (W-K2). Localhost only.

Serves the four endpoints panic touches, with seedable state and fault
injection, all openapi-shaped:

  GET    /trade-api/v2/portfolio/orders?status=resting   (cursor pagination)
  GET    /trade-api/v2/portfolio/positions
  GET    /trade-api/v2/markets/{ticker}
  DELETE /trade-api/v2/portfolio/events/orders/{id}
  POST   /trade-api/v2/portfolio/events/orders            (IOC liquidation)

Fault modes (env, comma-separated in MOCK_FAULTS):
  cancel_ack_loss    first DELETE per order: the cancel IS applied but the
                     response is HTTP 500 (ack lost); the retry sees 404
                     (already canceled) — proves retry-idempotency semantics
  cancel_refuse:ID   every DELETE for that order id returns 500 and the order
                     stays — proves loud partial failure
  order_ack_loss     first POST per client_order_id: fill applied, response
                     500; the reissue with the SAME client_order_id returns
                     409 duplicate — proves contract #9 (no double fill)
  endless_pages      orders listing always returns a cursor — proves
                     truncated enumeration is never trusted

Seed state (env): MOCK_ORDERS n, MOCK_POSITIONS "TICKER:pos_fp,..." — each
position gets a market with bid 0.4000 / ask 0.6000. Every applied action is
journaled to MOCK_JOURNAL (ndjson) so tests can assert what the exchange
actually saw (double-fill detection).
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"orders": {}, "positions": {}, "faults": set(),
         "cancel_500_seen": set(), "post_500_seen": set(),
         "coid_seen": {}, "journal": None, "lock": threading.Lock()}


def journal(rec):
    if STATE["journal"]:
        with open(STATE["journal"], "a") as f:
            f.write(json.dumps(rec) + "\n")


def seed():
    n = int(os.environ.get("MOCK_ORDERS", "0"))
    for i in range(n):
        oid = "ord-%04d-0000-0000-0000-00000000000%d" % (i, i % 10)
        STATE["orders"][oid] = {
            "order_id": oid, "user_id": "u", "client_order_id": "c%d" % i,
            "ticker": "KXMOCK-25DEC31-B%d" % i, "side": "yes", "action": "buy",
            "outcome_side": "yes", "book_side": "bid", "type": "limit",
            "status": "resting", "yes_price_dollars": "0.4000",
            "no_price_dollars": "0.6000", "fill_count_fp": "0.00",
            "remaining_count_fp": "5.00", "initial_count_fp": "5.00",
            "taker_fees_dollars": "0.0000", "maker_fees_dollars": "0.0000",
            "taker_fill_cost_dollars": "0.0000",
            "maker_fill_cost_dollars": "0.0000"}
    for part in filter(None, os.environ.get("MOCK_POSITIONS", "").split(",")):
        tk, fp = part.split(":")
        STATE["positions"][tk] = fp
    STATE["faults"] = set(filter(None, os.environ.get("MOCK_FAULTS", "").split(",")))
    STATE["journal"] = os.environ.get("MOCK_JOURNAL")


def _mkt_pos(tk, fp):
    return {"ticker": tk, "total_traded_dollars": "1.0000", "position_fp": fp,
            "market_exposure_dollars": "1.0000",
            "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000",
            "last_updated_ts": "2026-07-10T00:00:00Z"}


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        with STATE["lock"]:
            if self.path.startswith("/trade-api/v2/portfolio/orders"):
                if "endless_pages" in STATE["faults"]:
                    return self._json(200, {"orders": [], "cursor": "MORE"})
                resting = [o for o in STATE["orders"].values()
                           if o["status"] == "resting"]
                return self._json(200, {"orders": resting, "cursor": ""})
            if self.path.startswith("/trade-api/v2/portfolio/positions"):
                mp = [_mkt_pos(t, fp) for t, fp in STATE["positions"].items()
                      if any(c in "123456789" for c in fp)]
                return self._json(200, {"market_positions": mp,
                                        "event_positions": [], "cursor": ""})
            m = re.match(r"^/trade-api/v2/markets/([A-Z0-9._-]+)$", self.path)
            if m:
                return self._json(200, {"market": {
                    "ticker": m.group(1), "status": "active",
                    "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.6000"}})
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.match(r"^/trade-api/v2/portfolio/events/orders/(.+)$", self.path)
        if not m:
            return self._json(404, {"error": "not found"})
        oid = m.group(1)
        with STATE["lock"]:
            refuse = "cancel_refuse:%s" % oid in STATE["faults"]
            o = STATE["orders"].get(oid)
            if refuse:
                journal({"op": "cancel_refused", "order_id": oid})
                return self._json(500, {"error": "internal"})
            if o is None or o["status"] != "resting":
                return self._json(404, {"error": "order not found"})
            if ("cancel_ack_loss" in STATE["faults"]
                    and oid not in STATE["cancel_500_seen"]):
                STATE["cancel_500_seen"].add(oid)
                o["status"] = "canceled"                 # applied, ack lost
                journal({"op": "cancel_applied_ack_lost", "order_id": oid})
                return self._json(500, {"error": "internal"})
            o["status"] = "canceled"
            journal({"op": "cancel", "order_id": oid})
            return self._json(200, {"order": o})

    def do_POST(self):
        if self.path != "/trade-api/v2/portfolio/events/orders":
            return self._json(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with STATE["lock"]:
            coid = body.get("client_order_id", "")
            if coid in STATE["coid_seen"]:
                journal({"op": "duplicate_coid_rejected", "coid": coid})
                return self._json(409, {"error": "duplicate client_order_id"})
            # validate the panic order contract
            if body.get("time_in_force") != "immediate_or_cancel" or \
                    body.get("reduce_only") is not True:
                journal({"op": "bad_order_contract", "body": body})
                return self._json(400, {"error": "panic contract violated"})
            tk = body.get("ticker", "")
            fp = STATE["positions"].get(tk)
            apply_fill = fp is not None
            if apply_fill:
                STATE["positions"][tk] = "0.00"          # IOC fully fills
            if ("order_ack_loss" in STATE["faults"]
                    and coid not in STATE["post_500_seen"]):
                STATE["post_500_seen"].add(coid)
                if apply_fill:
                    STATE["coid_seen"][coid] = True      # landed, ack lost
                journal({"op": "order_applied_ack_lost", "coid": coid,
                         "ticker": tk, "body": body})
                return self._json(500, {"error": "internal"})
            STATE["coid_seen"][coid] = True
            journal({"op": "order_filled", "coid": coid, "ticker": tk,
                     "side": body.get("side"), "price": body.get("price"),
                     "count": body.get("count")})
            return self._json(201, {"order": {"order_id": "liq-" + coid[:8],
                                              "status": "executed"}})

    def log_message(self, *a):
        pass


def main():
    seed()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18310
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print("mock_exchange_panic on %d" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
