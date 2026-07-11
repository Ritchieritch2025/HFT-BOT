#!/usr/bin/env bash
# STP-P00-ISO-W01 (release STP-R003-TEST-ISOLATION, requirement 9): fail-closed
# NEGATIVE CONTROLS for tests/isolated_run.sh. Each case invokes the harness
# preflight under a sanitized base environment (env -i) with exactly one
# injected violation and asserts the harness REJECTS it (exit 2). One positive
# case proves a valid fresh root is accepted (exit 0), so the rejections are
# meaningful rather than "everything fails".
#
# Controls proven here:
#   1. missing isolation root (empty arg, and a nonexistent path);
#   2. isolation root resolving to the operational worktree (directly, and
#      via a symlink planted under the authorized prefix);
#   3. inherited production credentials (KALSHI_*, AWS_*) and production
#      API URLs (kalshi.com endpoint in any variable value);
#   4. a production work/** path carried in the environment;
#   plus: non-empty (stale) isolation root; root outside the authorized prefix.
#
# Prints PASS:/FAIL: per case (run_pipeline.sh counts these); exits nonzero
# if any control fails. Only writes under /private/tmp (or /tmp) using the
# stp-p00-test-isolation prefix — the release's allowed derived-write area.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
HARNESS="$SCRIPT_DIR/isolated_run.sh"

TMPBASE=/private/tmp
[ -d "$TMPBASE" ] || TMPBASE=/tmp

FAILS=0
CASES=0

# Sanitized base environment: controls must be deterministic regardless of
# the caller's shell (each case injects exactly one violation on top).
BASE_ENV=(env -i "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "HOME=$TMPBASE" "TMPDIR=$TMPBASE")

# run_case <name> <expected_rc> <cmd...>
run_case() {
  local name="$1" want="$2"; shift 2
  local out rc
  CASES=$((CASES + 1))
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then
    echo "PASS: $name (rc=$rc as expected)"
  else
    echo "FAIL: $name (rc=$rc, expected $want)"
    printf '%s\n' "$out" | sed 's/^/    | /'
    FAILS=1
  fi
}

EMPTY="$(mktemp -d "$TMPBASE/stp-p00-test-isolation-ctl.XXXXXX")" || {
  echo "FAIL: cannot create control scratch dir under $TMPBASE"; exit 1; }
LINK="$TMPBASE/stp-p00-test-isolation-ctl-link.$$"
cleanup() { rm -rf "$EMPTY"; rm -f "$LINK"; }
trap cleanup EXIT

# --- 1. missing isolation root -----------------------------------------------
run_case "missing-root-empty-arg" 2 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight ""
run_case "missing-root-nonexistent-path" 2 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$TMPBASE/stp-p00-test-isolation-missing.$$"

# --- 2. root resolving to the operational worktree ---------------------------
run_case "root-is-operational-worktree" 2 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$OPS_ROOT"
if ln -s "$OPS_ROOT" "$LINK" 2>/dev/null; then
  run_case "root-symlinks-to-operational-worktree" 2 \
    "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$LINK"
  rm -f "$LINK"
else
  echo "FAIL: could not plant control symlink $LINK"; FAILS=1
fi

# --- 3. inherited production credentials / API URLs --------------------------
run_case "inherited-kalshi-credential" 2 \
  "${BASE_ENV[@]}" "KALSHI_API_KEY_ID=control-not-a-real-key" \
  bash "$HARNESS" --preflight "$EMPTY"
run_case "inherited-aws-credential" 2 \
  "${BASE_ENV[@]}" "AWS_SECRET_ACCESS_KEY=control-not-a-real-key" \
  bash "$HARNESS" --preflight "$EMPTY"
run_case "inherited-production-api-url" 2 \
  "${BASE_ENV[@]}" "STP_CTL_URL=https://api.elections.kalshi.com/trade-api/v2" \
  bash "$HARNESS" --preflight "$EMPTY"

# --- 4. production work/** path in the environment ---------------------------
run_case "production-work-path-in-env" 2 \
  "${BASE_ENV[@]}" "STP_CTL_PATH=$OPS_ROOT/work/test_results.ndjson" \
  bash "$HARNESS" --preflight "$EMPTY"

# --- guards: prefix + freshness ----------------------------------------------
run_case "root-outside-authorized-prefix" 2 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$TMPBASE"

# --- positive control: a valid fresh empty root IS accepted ------------------
run_case "valid-fresh-root-accepted" 0 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$EMPTY"

# non-empty (stale) root is rejected
: > "$EMPTY/stale-marker"
run_case "non-empty-root-rejected" 2 \
  "${BASE_ENV[@]}" bash "$HARNESS" --preflight "$EMPTY"
rm -f "$EMPTY/stale-marker"

if [ "$FAILS" -eq 0 ]; then
  echo "isolation controls: all $CASES cases behaved fail-closed"
  exit 0
fi
echo "isolation controls: FAILURES present ($CASES cases run)"
exit 1
