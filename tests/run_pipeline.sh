#!/bin/bash
# End-to-end test of tradingd (single-process engine) against local stand-ins:
# mini_redis.py for the cold-path telemetry, mock_server.py as the exchange.
#
# usage: tests/run_pipeline.sh [redis_port] [mock_port]
set -euo pipefail
cd "$(dirname "$0")/.."

REDIS_PORT_=${1:-16380}
MOCK_PORT=${2:-18091}
OUT=${PIPELINE_OUT:-build/pipeline-test}
mkdir -p "$OUT"
rm -f "$OUT/capture.jsonl"

OSSL=third_party/openssl/bin/openssl
[ -x "$OSSL" ] || OSSL=openssl

$OSSL genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$OUT/key.pem" 2>/dev/null
$OSSL pkey -in "$OUT/key.pem" -pubout -out "$OUT/pub.pem" 2>/dev/null

cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
PIDS=()
trap cleanup EXIT

python3 tests/mini_redis.py "$REDIS_PORT_" >"$OUT/redis.log" 2>&1 &
PIDS+=($!)
python3 tests/mock_server.py "$MOCK_PORT" "$OUT/capture.jsonl" >"$OUT/mock.log" 2>&1 &
PIDS+=($!)
sleep 0.7

export REDIS_HOST=127.0.0.1 REDIS_PORT="$REDIS_PORT_"
export KALSHI_API_KEY_ID=pipeline-test KALSHI_PRIVATE_KEY_PATH="$OUT/key.pem"
export KALSHI_BASE_URL="http://127.0.0.1:$MOCK_PORT"

# tradingd runs the tape on its hot thread, drains its ring, and exits.
./build/tradingd --synthetic 100 5 >"$OUT/tradingd.log" 2>&1
sleep 0.3  # let telemetry finish its final drain writes

# Analysis runs while mini-redis is still up (reads exec:results over RESP).
python3 - "$OUT" "$REDIS_PORT_" <<'EOF'
import json, re, socket, sys

out, port = sys.argv[1], int(sys.argv[2])

def resp_cmd(sock, *args):
    payload = b"*%d\r\n" % len(args)
    for a in args:
        b = a if isinstance(a, bytes) else a.encode()
        payload += b"$%d\r\n%s\r\n" % (len(b), b)
    sock.sendall(payload)
    buf = b""
    while True:
        buf += sock.recv(65536)
        try:
            return parse(buf)[0]
        except IndexError:
            continue

def parse(buf):
    line, rest = buf.split(b"\r\n", 1)
    t, v = chr(line[0]), line[1:]
    if t in "+-": return v.decode(), rest
    if t == ":": return int(v), rest
    if t == "$":
        n = int(v)
        if n == -1: return None, rest
        return rest[:n], rest[n + 2:]
    if t == "*":
        items, r = [], rest
        for _ in range(int(v)):
            item, r = parse(r)
            items.append(item)
        return items, r
    raise ValueError(t)

fails = 0
def check(ok, what):
    global fails
    print(("PASS: " if ok else "FAIL: ") + what)
    if not ok: fails += 1

# 1. Orders that reached the (mock) exchange — same bar as the old pipeline.
orders = []
for line in open(f"{out}/capture.jsonl"):
    r = json.loads(line)
    if r["method"] == "POST" and r["path"].endswith("/portfolio/orders"):
        orders.append(r)
check(len(orders) >= 5, f"orders reached the exchange ({len(orders)})")

cid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
bodies = [json.loads(o["body"]) for o in orders]
check(all(b["ticker"].startswith("TEST-MKT-") for b in bodies), "all orders target TEST tickers")
check(all(b["type"] == "limit" and 1 <= b["yes_price"] <= 99 for b in bodies), "all orders are sane limit orders")
check(all(cid_re.match(b["client_order_id"]) for b in bodies), "client_order_ids are UUID-shaped")
check(len({b["client_order_id"] for b in bodies}) == len(bodies), "client_order_ids unique")
check(all(o["sig"] and o["ts"] and o["key"] == "pipeline-test" for o in orders), "auth headers present on every order")

# 2. Telemetry trail (cold path) matches what hit the exchange.
s = socket.create_connection(("127.0.0.1", port))
results = resp_cmd(s, "LRANGE", "exec:results", "0", "-1")
recs = [json.loads(r) for r in results]
submitted = [r for r in recs if r.get("kind") == "submitted"]
check(len(submitted) == len(orders), f"telemetry matches orders ({len(submitted)}/{len(orders)})")
check(all(r.get("http") == 200 for r in submitted), "all submissions got HTTP 200")
cids_wire = {b["client_order_id"] for b in bodies}
cids_tel = {r["cid"] for r in submitted}
check(cids_wire == cids_tel, "telemetry cids == exchange cids")

def pct(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else 0

qs = [r["queue_us"] for r in submitted]
sg = [r["sign_us"] for r in submitted]
sd = [r["send_us"] for r in submitted]
print(f"in-process handoff (decision->pop) us: p50={pct(qs,.5)} p99={pct(qs,.99)} max={max(qs, default=0)}")
print(f"RSA-PSS sign us:                       p50={pct(sg,.5)} p99={pct(sg,.99)}")
print(f"lane send us (loopback mock):          p50={pct(sd,.5)} p99={pct(sd,.99)}")
check(bool(qs) and pct(qs, .5) < 500, "median in-process handoff under 500us (was 88us via Redis relay)")

sys.exit(1 if fails else 0)
EOF
STATUS=$?

# 3. The exchange-side check Kalshi would run: verify captured signatures.
python3 tests/verify_captured.py "$OUT/capture.jsonl" "$OUT/pub.pem" "$OSSL" 25 || STATUS=1

if [ "$STATUS" -eq 0 ]; then echo "PIPELINE PASS"; else echo "PIPELINE FAIL"; fi
exit "$STATUS"
