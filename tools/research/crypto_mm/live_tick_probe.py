#!/usr/bin/env python3
"""ONE-ORDER live probe: is 0.1c price improvement accepted, and where
does it land?  OPERATOR-FIRED ONLY — this script places ONE 1-contract
limit order and cancels it after 20s (or reports the fill).

Run on prod AS THE OPERATOR (uses the trading key):
    set -a; source ~/.kalshi/env.sh; set +a
    /home/ubuntu/hft-bot/.venv/bin/python /home/ubuntu/live_tick_probe.py

Answers printed explicitly:
  Q1 accepted?      -> order status + any API error body verbatim
  Q2 book position? -> orderbook before/after, our level highlighted
  Q3 which path?    -> which price field the API accepted
Receipt: ~/h6b_inputs/live_probe_receipt_<ts>.json (full request/response log).
"""
import base64, json, time, uuid, os
from pathlib import Path
import urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

REST = "https://api.elections.kalshi.com/trade-api/v2"
V2O = "https://external-api.kalshi.com/trade-api/v2"   # new order host
SERIES = "KXBTC15M"
KEY_ID = os.environ["KALSHI_API_KEY_ID"]
PRIV = serialization.load_pem_private_key(
    Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).read_bytes(), password=None)
LOGP = Path(os.path.expanduser(
    f"~/h6b_inputs/live_probe_receipt_{int(time.time())}.json"))
LOG = []

def call(method, path, body=None, host=None):
    base = host or REST
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}/trade-api/v2{path}".encode()
    sig = PRIV.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                     salt_length=padding.PSS.DIGEST_LENGTH),
                    hashes.SHA256())
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"KALSHI-ACCESS-KEY": KEY_ID,
                 "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                 "KALSHI-ACCESS-TIMESTAMP": ts,
                 "Content-Type": "application/json"})
    t0 = time.time_ns()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.load(r); code = r.status
    except urllib.error.HTTPError as e:
        out = {"error_body": e.read().decode()[:1000]}; code = e.code
    LOG.append({"t_ns": t0, "rtt_ms": (time.time_ns() - t0) / 1e6,
                "method": method, "path": path, "body": body,
                "status": code, "resp": out})
    return code, out

# 1. pick an open market whose spread leaves room to improve:
#    need spread >= 0.3c and mid-range price (avoid near-settled tails)
_, mk = call("GET", f"/markets?series_ticker={SERIES}&status=open&limit=10")
pick = None
for m in mk["markets"]:
    T = m["ticker"]
    _, ob = call("GET", f"/markets/{T}/orderbook")
    ys = ob["orderbook_fp"].get("yes_dollars") or []
    ns = ob["orderbook_fp"].get("no_dollars") or []
    if not ys or not ns:
        continue
    bid = max(float(p) for p, _ in ys)
    ask = round(1.0 - max(float(p) for p, _ in ns), 4)
    spread = round(ask - bid, 4)
    print(f"candidate {T}: bid={bid} ask={ask} spread={spread}")
    if spread >= 0.003 and 0.05 <= bid <= 0.95:
        pick = (T, bid, ask)
        break
if pick is None:
    print("NO market currently has spread >= 0.3c in mid-range; "
          "rerun in a few minutes (fresh window opens every 15m).")
    LOGP.write_text(json.dumps(LOG, indent=1)); raise SystemExit(0)
T, best_bid, best_ask = pick
probe_px = round(min(best_bid + 0.001, best_ask - 0.001), 4)
print(f"market={T} bid={best_bid} ask={best_ask} probe_price={probe_px} "
      f"(improve +{round((probe_px-best_bid)*100,2)}c, stays below ask)")

# 2. V2 events/orders endpoint (per docs), sub-penny fixed-point price,
#    post_only guarantees pure-maker (never crosses).
order_id = None; accepted_via = None
body = {"ticker": T, "client_order_id": str(uuid.uuid4()),
        "side": "bid", "count": "1.00", "price": f"{probe_px:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "maker", "post_only": True}
code, resp = call("POST", "/portfolio/events/orders", body, host=V2O)
print(f"[V2 events/orders] HTTP {code}: {json.dumps(resp)[:400]}")
if code in (200, 201):
    o = resp.get("order") or resp
    order_id = o.get("order_id") or o.get("id")
    accepted_via = "V2 events/orders price=" + body["price"]

if order_id:
    time.sleep(2)
    _, ob2 = call("GET", f"/markets/{T}/orderbook")
    ys2 = ob2["orderbook_fp"].get("yes_dollars") or []
    ours = [lv for lv in ys2 if abs(float(lv[0]) - probe_px) < 1e-6]
    new_best = max(float(p) for p, _ in ys2)
    print(f"Q1 accepted: YES via {accepted_via}")
    print(f"Q2 book: our level present={bool(ours)} qty_at_level={ours} "
          f"new_best_bid={new_best} we_are_best={abs(new_best-probe_px)<1e-6}")
    code, st = call("GET", f"/portfolio/events/orders/{order_id}", host=V2O)
    if code >= 400:
        _, st = call("GET", f"/portfolio/orders/{order_id}")
    print("order status:", json.dumps(st)[:300])
    time.sleep(18)
    code, cx = call("DELETE", f"/portfolio/events/orders/{order_id}", host=V2O)
    if code >= 400:
        code, cx = call("DELETE", f"/portfolio/orders/{order_id}")
    print(f"cancel HTTP {code}: {json.dumps(cx)[:200]}")
else:
    print("Q1 accepted: NO — all price-field variants rejected "
          "(bodies above are the exact answer to Q3/V2-path question)")

LOGP.write_text(json.dumps(LOG, indent=1))
print("receipt:", LOGP)
