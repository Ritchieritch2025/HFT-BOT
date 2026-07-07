#!/bin/zsh
# WP-05 discovery probe (throwaway, sandbox-only): dump RESPONSE headers of one
# harmless authenticated GET to check whether Kalshi exposes rate-limit quota
# headers. Read-only; zero mutations; never prints key material or request
# headers. Cost: 1 read token-bucket charge (GET /markets, default cost 10).
#
# Usage: source ~/.kalshi/env.sh && ./rest_headers_probe.sh
set -euo pipefail

OSSL="/Users/ritcardo/HFT BOT/third_party/openssl/bin/openssl"
HOST="https://external-api.kalshi.com"
SIGPATH="/trade-api/v2/markets"       # signed without query per docs
QUERY="?limit=1"

: "${KALSHI_API_KEY_ID:?export KALSHI_API_KEY_ID first}"
: "${KALSHI_PRIVATE_KEY_PATH:?export KALSHI_PRIVATE_KEY_PATH first}"

TS=$(python3 -c 'import time; print(int(time.time()*1000))')
MSG="${TS}GET${SIGPATH}"

SIG=$(printf '%s' "$MSG" \
  | "$OSSL" dgst -sha256 -binary \
  | "$OSSL" pkeyutl -sign -inkey "$KALSHI_PRIVATE_KEY_PATH" \
      -pkeyopt digest:sha256 -pkeyopt rsa_padding_mode:pss \
      -pkeyopt rsa_pss_saltlen:digest \
  | "$OSSL" base64 -A)

# -s silent, -D - dump RESPONSE headers to stdout, body to /dev/null.
curl -s --max-time 20 -D - -o /dev/null \
  -H "KALSHI-ACCESS-KEY: ${KALSHI_API_KEY_ID}" \
  -H "KALSHI-ACCESS-SIGNATURE: ${SIG}" \
  -H "KALSHI-ACCESS-TIMESTAMP: ${TS}" \
  "${HOST}${SIGPATH}${QUERY}"
