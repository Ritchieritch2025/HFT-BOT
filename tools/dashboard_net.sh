#!/usr/bin/env bash
# Launch the ops console with network_read probes ENABLED (real, READ-ONLY Kalshi
# calls with your key). live_order tools (bench_order/fill_test/tradingd/
# account_upgrade) stay forbidden regardless — that gate is not bypassable.
#
# Run it YOURSELF so you authorize prod exposure (the agent can't arm prod):
#     bash tools/dashboard_net.sh <YOUR_KEY_ID>
# or export KALSHI_API_KEY_ID first. Optional env: KALSHI_ENV (default prod),
# KALSHI_BASE_URL, PORT (default 8765), KALSHI_PRIVATE_KEY_PATH.
set -u
cd "$(dirname "$0")/.."

# Key id: positional arg 1 wins, else env, else error.
KEY_ID="${1:-${KALSHI_API_KEY_ID:-}}"
if [ -z "$KEY_ID" ]; then
  echo "usage: bash tools/dashboard_net.sh <YOUR_KEY_ID>" >&2
  exit 2
fi
export KALSHI_API_KEY_ID="$KEY_ID"
export KALSHI_ENV="${KALSHI_ENV:-prod}"
export KALSHI_ALLOW_PROD="${KALSHI_ALLOW_PROD:-1}"
export KALSHI_BASE_URL="${KALSHI_BASE_URL:-https://api.elections.kalshi.com}"
export KALSHI_PRIVATE_KEY_PATH="${KALSHI_PRIVATE_KEY_PATH:-$HOME/.kalshi/private_key.pem}"
PORT="${PORT:-8765}"

echo "key id .....: ${KEY_ID:0:8}…  (env=$KALSHI_ENV host=$KALSHI_BASE_URL)"
if [ ! -f "$KALSHI_PRIVATE_KEY_PATH" ]; then
  echo "WARNING: key file not found at $KALSHI_PRIVATE_KEY_PATH" >&2
fi

# Free the port (kill whatever safe/old instance holds it).
PIDS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
if [ -n "$PIDS" ]; then echo "stopping old instance ($PIDS)"; kill $PIDS 2>/dev/null; sleep 1; fi

mkdir -p work
# Fully detach: nohup + </dev/null + background + disown so it survives the
# launching shell exiting.
nohup python3 dashboard_server.py --metrics work/metrics.ndjson \
  --results work/test_results.ndjson --port "$PORT" --host 127.0.0.1 --allow-network \
  </dev/null >work/dash.log 2>&1 &
disown
sleep 1.5

net="$(curl -s -m 3 "http://127.0.0.1:$PORT/api/tools" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["allow_network"])' 2>/dev/null || echo error)"
if [ "$net" = "True" ]; then
  echo "network console UP -> http://127.0.0.1:$PORT   allow_network=True (live_order still forbidden)"
  echo "HARD-REFRESH the browser tab (Cmd+Shift+R)."
else
  echo "FAILED (allow_network=$net). Last log lines:" >&2
  tail -6 work/dash.log >&2
fi
