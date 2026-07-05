#!/usr/bin/env bash
# Grep-based safety gates for the Token Rule System (docs/PLAN_TOKEN_RULES.md).
# Each gate is a forbidden-pattern search over the live source; any hit fails the
# build. Gates are added phase-by-phase; run via `make check`.
#
# Scope note: we search src/ apps/ include/ (live code). tests/ and docs/ are
# excluded — a test or a doc may legitimately name a forbidden string to prove it
# is rejected or to document it.
set -u
cd "$(dirname "$0")/.." || exit 2

fail=0
# Live source only (exclude tests/, docs/, third_party/, build/).
SRC='src include apps'

gate() {  # gate <name> <grep-args...>
  local name="$1"; shift
  local hits
  hits=$(grep -rnI "$@" $SRC 2>/dev/null)
  if [ -n "$hits" ]; then
    echo "GATE FAIL: $name"
    echo "$hits" | sed 's/^/  /'
    fail=1
  else
    echo "gate ok: $name"
  fi
}

# T0 — no substring host matching. Host membership must be exact equality
# (host_in), never find("demo")/find("prod")-style base_url sniffing (the old
# anti-pattern this repo purged).
gate 'no find("demo") host sniffing' -e 'find("demo")' -e "find('demo')"
gate 'no base_url substring host checks' -E 'base_url\.(find|substr|rfind)\("(demo|prod|elections|external)'

# T1 — the pre-v3.23.0 account endpoint paths are banned anywhere in live source
# (acceptance criteria). Renamed to /account/limits and /account/endpoint_costs.
gate 'no /account/api_limits path' -e '/account/api_limits'
gate 'no /account/non-default-endpoint-costs path' -e '/account/non-default-endpoint-costs'

# T5 — no auth secret (private key / signature) may be handed to a logging call.
# Auth headers reach stderr only through the redacting curl debug function.
gate 'no auth secret in log calls' -nE \
  '(fprintf|printf|fputs|puts|std::cout|std::cerr)[^;]*(private_key_pem|sig_header|ACCESS-SIGNATURE|access_signature)'

# P1 — tool registry (tools.json) schema + Makefile coverage. Existence of every
# built binary is checked separately in run_pipeline.sh (--require-built) after a
# full make; here we validate structure + coverage, which needs no binaries.
if command -v python3 >/dev/null 2>&1; then
  if python3 tools/check_registry.py; then :; else fail=1; fi
else
  echo "gate SKIP: tool registry (python3 not found)"
fi

exit $fail
