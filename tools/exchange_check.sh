#!/usr/bin/env bash
# One-command exchange readiness check (PLAN_LIVE_VALIDATION P2).
#
# Runs the full read-only exchange surface in sequence and prints one PASS/FAIL
# summary. Everything here is READ-ONLY: preflight's order round-trip is never
# invoked, and ws_shadow refuses live mode by construction, so this is safe to
# run against prod. (Kalshi's demo exchange is unavailable and unsupported.)
#
#   tools/exchange_check.sh [--env prod] [--tickers T1,T2]
#       [--ws-seconds 30] [--metrics work/metrics.ndjson]
#
# Steps:
#   a. ./build/preflight --orderbook <tickers>
#        status / clock skew / balance+auth / markets / account limits+costs /
#        top-5 orderbook levels + spread per ticker
#   b. ws_shadow (data_collect): subscribe orderbook_delta -> capture NDJSON ->
#        at-exit REST batch cross-check
#   c. tools/verify_ws_capture.py: structural validation of the capture
#   d. tools/verify_feed_metrics.py: dashboard metrics contain fresh real feed rows
#
# Requires KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in the environment for the
# signed REST/WS handshakes. Binaries must be built (`make build/preflight
# build/ws_shadow`).
set -u
cd "$(dirname "$0")/.."

ENV="prod"
TICKERS=""
WS_SECONDS=30
METRICS="${KALSHI_SHADOW_METRICS:-work/metrics.ndjson}"
# Streaming + storing are unified: on a clean capture we load it straight into
# the DuckDB/Parquet research warehouse. Disable with --no-warehouse.
LOAD_WAREHOUSE=1
WAREHOUSE_DIR="work/warehouse"
WAREHOUSE_RUN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env)           ENV="$2"; shift 2 ;;
    --tickers)       TICKERS="$2"; shift 2 ;;
    --ws-seconds)    WS_SECONDS="$2"; shift 2 ;;
    --metrics)       METRICS="$2"; shift 2 ;;
    --no-warehouse)  LOAD_WAREHOUSE=0; shift ;;
    --warehouse-dir) WAREHOUSE_DIR="$2"; shift 2 ;;
    --run-id)        WAREHOUSE_RUN_ID="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

export KALSHI_ENV="$ENV"
if [ "$ENV" = "prod" ]; then
  export KALSHI_ALLOW_PROD=1
fi

CAPTURE="work/exchange_check_capture.ndjson"
CHANNELS="${KALSHI_WS_CHANNELS:-orderbook_delta,trade,ticker}"
mkdir -p work
rm -f "$CAPTURE"

FAILED=0
step() { printf '\n=== %s ===\n' "$1"; }

# --- ticker discovery (if not supplied): parse preflight's markets line --------
if [ -z "$TICKERS" ]; then
  step "discovering open markets"
  DISCOVER="$(./build/preflight 2>/dev/null || true)"
  # The markets line looks like: "PASS  market data parses (GET /markets) — T1 (..) T2 (..)"
  TICKERS="$(printf '%s\n' "$DISCOVER" \
    | sed -n 's/.*GET \/markets) — //p' \
    | grep -oE '[A-Z0-9._-]+ \(' \
    | sed 's/ (//' \
    | head -2 | paste -sd, -)"
  if [ -z "$TICKERS" ]; then
    echo "FAIL: could not auto-discover open-market tickers from preflight (GET /markets)." >&2
    echo "  Likely causes:" >&2
    echo "    - credentials missing/invalid (need KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH exported)" >&2
    echo "    - transient markets outage, or no open markets at this moment" >&2
    echo "  Fix: confirm creds are set (do not print them), then re-run with an explicit open market:" >&2
    echo "    $0 --env $ENV --tickers TICKER1,TICKER2 --ws-seconds $WS_SECONDS" >&2
    echo "EXCHANGE CHECK FAIL"
    exit 1
  fi
  echo "discovered tickers: $TICKERS"
fi

# --- a. preflight read-only surface --------------------------------------------
step "preflight (read-only + orderbook detail)"
if ./build/preflight --orderbook "$TICKERS"; then
  echo "preflight: PASS"
else
  echo "preflight: FAIL"; FAILED=1
fi

# --- b. ws_shadow capture ------------------------------------------------------
step "ws_shadow capture (${WS_SECONDS}s, data_collect + REST cross-check)"
if KALSHI_MODE=data_collect \
   KALSHI_SHADOW_SECONDS="$WS_SECONDS" \
   KALSHI_SHADOW_CAPTURE="$CAPTURE" \
   KALSHI_SHADOW_METRICS="$METRICS" \
   KALSHI_SHADOW_XCHECK=1 \
   KALSHI_WS_CHANNELS="$CHANNELS" \
   KALSHI_WS_TICKERS="$TICKERS" \
   ./build/ws_shadow; then
  echo "ws_shadow: PASS"
else
  echo "ws_shadow: FAIL"; FAILED=1
fi

# --- c. capture structure validation -------------------------------------------
step "verify_ws_capture"
# Require the capture to span most of the requested window (allow ~25% slack for
# connect/teardown) so a mid-session disconnect is caught.
MIN_SPAN="$(awk "BEGIN{printf \"%.0f\", $WS_SECONDS * 0.75}")"
if python3 tools/verify_ws_capture.py "$CAPTURE" \
     --tickers "$TICKERS" --min-span-seconds "$MIN_SPAN"; then
  echo "verify_ws_capture: PASS"
else
  echo "verify_ws_capture: FAIL"; FAILED=1
fi

# --- d. dashboard feed metrics validation --------------------------------------
step "verify_feed_metrics"
MAX_AGE_MS=$(( (WS_SECONDS + 30) * 1000 ))
if python3 tools/verify_feed_metrics.py "$METRICS" \
     --source kalshi_ws --capture "$CAPTURE" --tickers "$TICKERS" \
     --max-age-ms "$MAX_AGE_MS"; then
  echo "verify_feed_metrics: PASS"
else
  echo "verify_feed_metrics: FAIL"; FAILED=1
fi

# --- e. ingest the capture into the staging warehouse (three-layer pipeline) ---
# Same capture that fed the dashboard lands in staging.duckdb via the
# checkpointed change-only ingester, so one command both streams (dashboard)
# and stores (warehouse). The day-rollover exporter archives it at UTC midnight.
WAREHOUSE_RESULT="disabled"
if [ "$LOAD_WAREHOUSE" -eq 1 ]; then
  step "warehouse ingest (capture -> staging.duckdb)"
  if [ "$FAILED" -ne 0 ]; then
    echo "warehouse: SKIPPED (upstream steps failed; not storing a bad capture)"
    WAREHOUSE_RESULT="skipped"
  elif [ ! -s "$CAPTURE" ]; then
    echo "warehouse: SKIPPED (capture is empty; nothing to store)"
    WAREHOUSE_RESULT="skipped"
  else
    if python3 tools/ingest.py --warehouse "$WAREHOUSE_DIR" \
         --staging "$WAREHOUSE_DIR/staging.duckdb" "$CAPTURE"; then
      echo "warehouse: PASS (staged -> $WAREHOUSE_DIR/staging.duckdb)"
      WAREHOUSE_RESULT="pass staged"
    else
      echo "warehouse: FAIL"; FAILED=1
      WAREHOUSE_RESULT="fail"
    fi
  fi
fi

# --- summary -------------------------------------------------------------------
step "summary"
printf 'env=%s tickers=%s ws-seconds=%s channels=%s capture=%s metrics=%s warehouse=%s\n' \
  "$ENV" "$TICKERS" "$WS_SECONDS" "$CHANNELS" "$CAPTURE" "$METRICS" "$WAREHOUSE_RESULT"
if [ "$FAILED" -eq 0 ]; then
  echo "EXCHANGE CHECK PASS"
  exit 0
else
  echo "EXCHANGE CHECK FAIL"
  exit 1
fi
