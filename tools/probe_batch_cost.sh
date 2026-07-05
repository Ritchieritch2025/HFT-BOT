#!/usr/bin/env bash
# Empirical batch-orderbook token-cost probe (PLAN_TOKEN_RULES T6 / F8).
# READ-ONLY (no orders). Requires a real demo key. Record findings in
# docs/KALSHI_RULEBOOK.md (T9).
#
# Usage: KALSHI_ENV=demo KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=...
#        tools/probe_batch_cost.sh [seconds] [ticker ...]
set -euo pipefail
cd "$(dirname "$0")/.."

SECONDS_ARG="${1:-20}"
shift || true
TICKERS=("$@")
if [ "${#TICKERS[@]}" -eq 0 ]; then
  echo "provide one or more market tickers to batch (1..100)"; exit 2
fi

if [ ! -x build/probe_batch_cost ]; then
  echo "building probe..."; make build/probe_batch_cost
fi

echo "Probing batch-orderbook throughput for ${SECONDS_ARG}s against ${KALSHI_ENV:-?}..."
echo "Interpretation: per-batch token cost ~= read_refill_rate / (batch calls/s at steady state)."
echo "If calls/s scales ~1/N with batch size N, billing is per-item (F8 assumption holds)."
exec build/probe_batch_cost "$SECONDS_ARG" "${TICKERS[@]}"
