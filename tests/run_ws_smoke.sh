#!/bin/bash
# WS integration smoke: mock_ws_exchange.py + ws_smoke (ixwebsocket transport).
# Verifies handshake with auth headers, subscribe->snapshot->deltas, and the
# heartbeat pong echo. No real network, no orders.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${1:-18200}
python3 tests/mock_ws_exchange.py "$PORT" >/tmp/mock_ws.$PORT.log 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
sleep 0.6

./build/ws_smoke "ws://127.0.0.1:$PORT/trade-api/ws/v2"
SMOKE=$?
sleep 0.3

echo "--- mock server log ---"
cat /tmp/mock_ws.$PORT.log
grep -q HANDSHAKE_OK /tmp/mock_ws.$PORT.log || { echo "no handshake"; exit 1; }
grep -q HEARTBEAT_OK /tmp/mock_ws.$PORT.log || { echo "no heartbeat pong echo"; exit 1; }
exit $SMOKE
