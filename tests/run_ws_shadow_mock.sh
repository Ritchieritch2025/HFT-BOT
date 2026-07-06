#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p build/scratch
make build/ws_shadow >/dev/null

PORT="${KALSHI_TEST_WS_PORT:-18877}"
CAPTURE="build/scratch/ws_shadow_mock_capture.ndjson"
METRICS="build/scratch/ws_shadow_mock_metrics.ndjson"
SERVER_LOG="build/scratch/ws_shadow_mock_server.log"
OUT_LOG="build/scratch/ws_shadow_mock.out"
ERR_LOG="build/scratch/ws_shadow_mock.err"
rm -f "$CAPTURE" "$METRICS" "$SERVER_LOG" "$OUT_LOG" "$ERR_LOG"

python3 tests/mock_ws_exchange.py "$PORT" >"$SERVER_LOG" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT
sleep 0.3

KALSHI_ENV=local_mock \
KALSHI_MODE=data_collect \
KALSHI_WS_TICKERS=MKT-A \
KALSHI_WS_CHANNELS=orderbook_delta,trade,ticker \
KALSHI_SHADOW_SECONDS=2 \
KALSHI_SHADOW_CAPTURE="$CAPTURE" \
KALSHI_SHADOW_METRICS="$METRICS" \
./build/ws_shadow "ws://127.0.0.1:${PORT}/trade-api/ws/v2" >"$OUT_LOG" 2>"$ERR_LOG"

grep -q "WS SHADOW PASS" "$OUT_LOG"
grep -q '"type":"feed"' "$METRICS"
grep -q '"source":"kalshi_ws"' "$METRICS"
grep -q '"type":"market_data"' "$METRICS"
grep -q '"market_ticker":"MKT-A"' "$METRICS"
grep -q '"synthetic":false' "$METRICS"
python3 tools/verify_ws_capture.py "$CAPTURE" --tickers MKT-A >/dev/null
python3 tools/verify_feed_metrics.py "$METRICS" \
  --source kalshi_ws --capture "$CAPTURE" --tickers MKT-A >/dev/null
echo "WS SHADOW MOCK PASS"
