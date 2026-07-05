#!/usr/bin/env bash
# One-command exchange readiness check (PLAN_LIVE_VALIDATION P2).
#
# Runs the full read-only exchange surface in sequence and prints one PASS/FAIL
# summary. Everything here is READ-ONLY: preflight's order round-trip is never
# invoked, and ws_shadow refuses live mode by construction, so this is safe to
# run against demo or prod.
#
#   tools/exchange_check.sh [--env demo|prod] [--tickers T1,T2] [--ws-seconds 30]
#
# Steps:
#   a. ./build/preflight --orderbook <tickers>
#        status / clock skew / balance+auth / markets / account limits+costs /
#        top-5 orderbook levels + spread per ticker
#   b. ws_shadow (data_collect): subscribe orderbook_delta -> capture NDJSON ->
#        at-exit REST batch cross-check
#   c. tools/verify_ws_capture.py: structural validation of the capture
#
# Requires KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in the environment for the
# signed REST/WS handshakes. Binaries must be built (`make build/preflight
# build/ws_shadow`).
set -u
cd "$(dirname "$0")/.."

ENV="demo"
TICKERS=""
WS_SECONDS=30
while [ $# -gt 0 ]; do
  case "$1" in
    --env)        ENV="$2"; shift 2 ;;
    --tickers)    TICKERS="$2"; shift 2 ;;
    --ws-seconds) WS_SECONDS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

export KALSHI_ENV="$ENV"
if [ "$ENV" = "prod" ]; then
  export KALSHI_ALLOW_PROD=1
fi

CAPTURE="work/exchange_check_capture.ndjson"
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
    echo "FAIL: could not auto-discover open-market tickers; pass --tickers T1,T2" >&2
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
   KALSHI_SHADOW_XCHECK=1 \
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

# --- summary -------------------------------------------------------------------
step "summary"
printf 'env=%s tickers=%s ws-seconds=%s capture=%s\n' \
  "$ENV" "$TICKERS" "$WS_SECONDS" "$CAPTURE"
if [ "$FAILED" -eq 0 ]; then
  echo "EXCHANGE CHECK PASS"
  exit 0
else
  echo "EXCHANGE CHECK FAIL"
  exit 1
fi
