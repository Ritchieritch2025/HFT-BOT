#!/usr/bin/env bash
# STP-P00-ISO-W01 (release STP-R003-TEST-ISOLATION): isolated test harness.
#
# Runs the complete offline test sequence
#
#     make all
#     make check
#     tests/run_pipeline.sh
#     python3 tools/check_registry.py --require-built
#
# inside a FRESH ISOLATED ROOT under /private/tmp/stp-p00-test-isolation*
# (a `git clone --no-local` of this repository at HEAD), with a scrubbed
# environment, and PROVES the operational worktree is unchanged by taking
# complete before/after snapshots of operational state (work/** content
# manifest, .pytest_cache, git status incl. untracked, hashes of the
# specifically forbidden operational files).
#
# FAIL-CLOSED preconditions (any violation => "ISOLATION REJECT", exit 2):
#   - the isolation root must exist and be an empty directory;
#   - its realpath must live under /private/tmp/stp-p00-test-isolation
#     (or /tmp/stp-p00-test-isolation on hosts without /private — on macOS
#     /tmp resolves to /private/tmp, so these are the same tree);
#   - it must not resolve to, contain, or be contained in the operational
#     worktree (symlink tricks are defeated via realpath);
#   - the inherited environment must not carry production authority:
#     no KALSHI_*/AWS_* variables, no variable whose name looks like a
#     secret (SECRET/TOKEN/PASSWORD/PRIVATE_KEY/ACCESS_KEY/CREDENTIAL),
#     no value referencing the operational work/** tree, and no value
#     containing a production API endpoint (kalshi.com / amazonaws.com /
#     the EC2 EIP). Violations are reported by VARIABLE NAME ONLY —
#     values are never printed.
#
# The four commands run under `env -i` with a minimal allowlist
# (PATH/HOME/TMPDIR pointing INTO the isolated root, PYTHONUSERBASE for
# the user-site pytest/duckdb/pandas install, LANG/LC_ALL=C, MAKEFLAGS,
# PIPELINE_PORT_BASE). No credentials exist in the child environment by
# construction; safe-mode defaults (KALSHI_ENV=local_mock,
# KALSHI_MODE=data_collect) come from src/env.cpp / the tools' own
# fail-closed defaults, exactly as in a normal offline run.
#
# Usage:
#   tests/isolated_run.sh                    # full run, auto-created fresh root
#   tests/isolated_run.sh --new              # same as above
#   tests/isolated_run.sh --root DIR         # full run in existing EMPTY DIR
#   tests/isolated_run.sh --preflight DIR    # precondition checks only
#
# Exit codes: 0 = success; 2 = ISOLATION REJECT (fail-closed precondition);
#             1 = a test command failed or operational state changed.
#
# Negative controls for the fail-closed behavior live in
# tests/test_isolation_controls.sh (wired into tests/run_pipeline.sh).
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
PY=python3

MODE=full
ROOT_ARG="__NEW__"
case "${1:-}" in
  --preflight) MODE=preflight; ROOT_ARG="${2-}" ;;
  --root)      ROOT_ARG="${2-}" ;;
  --new|"")    ROOT_ARG="__NEW__" ;;
  *) echo "usage: $0 [--new | --root DIR | --preflight DIR]" >&2; exit 64 ;;
esac

# ---------------------------------------------------------------- preflight --
# All fail-closed checks live in one python helper (realpath-safe, env-scan).
# Arguments: <mode> <root> <ops_root>. Exit 2 + "ISOLATION REJECT: ..." on any
# violation; prints "PREFLIGHT OK: root=<realpath>" on success.
preflight() {
  "$PY" - "$1" "$2" "$OPS_ROOT" <<'PYEOF'
import os, re, sys

mode, root, ops = sys.argv[1], sys.argv[2], sys.argv[3]

def reject(msg):
    sys.stderr.write("ISOLATION REJECT: %s\n" % msg)
    sys.exit(2)

# C1/C2: the isolation root must exist as a directory.
if not root:
    reject("missing isolation root (no path given)")
if not os.path.isdir(root):
    reject("missing isolation root: '%s' is not an existing directory" % root)

rr = os.path.realpath(root)
ro = os.path.realpath(ops)

# C3: must not resolve to / contain / be contained in the operational worktree.
if rr == ro or rr.startswith(ro + os.sep) or ro.startswith(rr + os.sep):
    reject("isolation root resolves to (or overlaps) the operational worktree:"
           " root realpath='%s', worktree='%s'" % (rr, ro))

# C4: must live under the release's isolated_test_root_prefix. On macOS
# /tmp -> /private/tmp, so realpaths always carry /private; the bare /tmp
# form is accepted for Linux hosts (same tree semantics).
if not re.match(r"^(/private)?/tmp/stp-p00-test-isolation", rr):
    reject("isolation root '%s' (realpath '%s') is outside the authorized "
           "prefix /private/tmp/stp-p00-test-isolation" % (root, rr))

# C5: inherited environment must carry no production authority. Report
# variable NAMES only — never values (they may be secrets).
denied_name = re.compile(
    r"(^KALSHI_|^AWS_|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY|CREDENTIAL)",
    re.I)
prod_url = re.compile(r"(kalshi\.com|amazonaws\.com|3\.130\.232\.109)", re.I)
work_trees = {os.path.join(ops, "work"), os.path.join(ro, "work")}
for k in sorted(os.environ):
    v = os.environ[k]
    if denied_name.search(k):
        reject("inherited production credential/config variable: %s "
               "(value redacted) — unset it and retry" % k)
    for wt in work_trees:
        if wt in v:
            reject("environment variable %s references the production "
                   "work/** tree (value redacted)" % k)
    if prod_url.search(v):
        reject("environment variable %s contains a production API endpoint "
               "(value redacted)" % k)

# C6: the root must be fresh (empty).
if os.listdir(rr):
    reject("isolation root '%s' is not empty (a fresh root is required)" % rr)

print("PREFLIGHT OK: root=%s" % rr)
PYEOF
}

if [ "$MODE" = "preflight" ]; then
  preflight preflight "$ROOT_ARG"
  exit $?
fi

# ---------------------------------------------------------------- full run ---
if [ "$ROOT_ARG" = "__NEW__" ]; then
  TMPBASE=/private/tmp
  [ -d "$TMPBASE" ] || TMPBASE=/tmp
  ROOT="$(mktemp -d "$TMPBASE/stp-p00-test-isolation.XXXXXX")" || {
    echo "ISOLATION REJECT: could not create a fresh isolation root under $TMPBASE" >&2
    exit 2
  }
else
  ROOT="$ROOT_ARG"
fi

preflight full "$ROOT" || exit 2
REAL_ROOT="$(cd "$ROOT" && pwd -P)"

PROOF="$REAL_ROOT/proof"
REPO="$REAL_ROOT/repo"
mkdir -p "$PROOF" "$REAL_ROOT/home" "$REAL_ROOT/tmp"

echo "== isolated_run: root=$REAL_ROOT ops=$OPS_ROOT =="

FORBIDDEN="work/lifecycle_status.json work/lifecycle_events.ndjson \
work/test_results_latest.json work/test_results.ndjson work/live/alerts.log"

# snapshot <tag>: complete content manifest of operational work/**,
# .pytest_cache, git status (incl. untracked), forbidden-file hashes.
snapshot() {
  local tag="$1"
  ( cd "$OPS_ROOT" || exit 1
    find work -type f -print0 2>/dev/null \
      | xargs -0 -P 4 -n 1 shasum -a 256 2>/dev/null \
      | sort -k 2 > "$PROOF/ops_work_manifest_$tag.txt"
    if [ -d .pytest_cache ]; then
      find .pytest_cache -type f -print0 \
        | xargs -0 -P 4 -n 1 shasum -a 256 \
        | sort -k 2 > "$PROOF/ops_pytest_cache_$tag.txt"
    else
      echo "ABSENT .pytest_cache" > "$PROOF/ops_pytest_cache_$tag.txt"
    fi
    { git --no-optional-locks -c core.fsmonitor=false status --porcelain -uall | sort
      printf 'HEAD %s\n' "$(git rev-parse HEAD)"
    } > "$PROOF/ops_git_status_$tag.txt"
    for f in $FORBIDDEN; do
      if [ -f "$f" ]; then shasum -a 256 "$f"; else echo "ABSENT  $f"; fi
    done > "$PROOF/ops_forbidden_$tag.txt"
  )
}

# Record the inherited environment: names always, values only for a safe
# allowlist; everything else redacted (secrets must never reach the proof).
env | LC_ALL=C sort | "$PY" -c '
import sys
safe = {"PATH","PWD","SHELL","TERM","LANG","USER","LOGNAME","HOME","TMPDIR",
        "SHLVL","OLDPWD","_"}
for line in sys.stdin:
    line = line.rstrip("\n")
    if "=" not in line: continue
    k, v = line.split("=", 1)
    print("%s=%s" % (k, v if k in safe else "<redacted>"))
' > "$PROOF/inherited_env_redacted.txt"

echo "-- snapshot(before): hashing operational work/** (139G tree; minutes) --"
snapshot before

# Isolated repository copy: clone at HEAD (objects copied, not hardlinked).
OPS_HEAD="$(cd "$OPS_ROOT" && git rev-parse HEAD)"
echo "-- clone at $OPS_HEAD --"
git clone --no-local --quiet "$OPS_ROOT" "$REPO" || {
  echo "ISOLATION FAIL: git clone into the isolation root failed" >&2; exit 1; }
ISO_HEAD="$(cd "$REPO" && git rev-parse HEAD)"
if [ "$ISO_HEAD" != "$OPS_HEAD" ]; then
  echo "ISOLATION FAIL: isolated HEAD $ISO_HEAD != operational HEAD $OPS_HEAD" >&2
  exit 1
fi
printf 'ops_head %s\niso_head %s\n' "$OPS_HEAD" "$ISO_HEAD" > "$PROOF/heads.txt"

# Pre-run symlink scan: a fresh clone of this repo must contain no symlink
# that escapes the isolation root (defeats "iso work/ -> production work/").
symlink_scan() {  # symlink_scan <outfile> ; echoes number of ESCAPES-to-ops
  local out="$1"
  find "$REAL_ROOT" -type l -print0 2>/dev/null \
    | "$PY" -c '
import os, sys
root, ops = sys.argv[1], sys.argv[2]
bad = 0
items = sys.stdin.buffer.read().split(b"\0")
for raw in items:
    if not raw: continue
    p = raw.decode("utf-8", "replace")
    t = os.path.realpath(p)
    if t == root or t.startswith(root + os.sep):
        kind = "inside"
    elif t == ops or t.startswith(ops + os.sep):
        kind = "ESCAPES-TO-OPERATIONAL"; bad += 1
    else:
        kind = "escapes-elsewhere(warn)"
    print("%s %s -> %s" % (kind, p, t))
print("escapes_to_operational=%d" % bad)
sys.exit(0)
' "$REAL_ROOT" "$OPS_ROOT" > "$out"
  grep -c '^ESCAPES-TO-OPERATIONAL' "$out" || true
}
PRE_ESCAPES="$(symlink_scan "$PROOF/symlinks_pre.txt")"
if [ "${PRE_ESCAPES:-0}" -ne 0 ]; then
  echo "ISOLATION REJECT: symlink(s) in the isolation root resolve into the operational worktree (see $PROOF/symlinks_pre.txt)" >&2
  exit 2
fi

# Scrubbed child environment (allowlist only; nothing inherited).
NCPU="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
PYUSERBASE="$("$PY" -c 'import site; print(site.USER_BASE)')"
PORT_BASE="${STP_ISO_PORT_BASE:-18400}"
CHILD_ENV=(
  "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
  "HOME=$REAL_ROOT/home"
  "TMPDIR=$REAL_ROOT/tmp"
  "PYTHONUSERBASE=$PYUSERBASE"
  "LANG=C" "LC_ALL=C"
  "MAKEFLAGS=-j$NCPU"
  "PIPELINE_PORT_BASE=$PORT_BASE"
)
printf '%s\n' "${CHILD_ENV[@]}" > "$PROOF/child_env.txt"

run_step() {  # run_step <n> <name> <command...>
  local n="$1" name="$2"; shift 2
  local log="$PROOF/step${n}_${name}.log" t0 t1 rc
  echo "-- step $n: $* --"
  t0="$(date +%s)"
  ( cd "$REPO" && env -i "${CHILD_ENV[@]}" "$@" ) > "$log" 2>&1
  rc=$?
  t1="$(date +%s)"
  printf 'step=%s name=%s rc=%d duration_s=%d cmd=%s\n' \
    "$n" "$name" "$rc" "$((t1 - t0))" "$*" >> "$PROOF/steps.txt"
  if [ "$rc" -ne 0 ]; then
    echo "ISOLATION FAIL: step $n ($*) exited $rc — see $log" >&2
    STEP_FAIL=1
  fi
  return "$rc"
}

STEP_FAIL=0
run_step 1 make_all       make all
[ "$STEP_FAIL" -eq 0 ] && run_step 2 make_check     make check
[ "$STEP_FAIL" -eq 0 ] && run_step 3 run_pipeline   tests/run_pipeline.sh
[ "$STEP_FAIL" -eq 0 ] && run_step 4 check_registry python3 tools/check_registry.py --require-built

# Post-run symlink scan (tests may create symlinks; none may reach ops).
POST_ESCAPES="$(symlink_scan "$PROOF/symlinks_post.txt")"

echo "-- snapshot(after): re-hashing operational work/** --"
snapshot after

# Compare every snapshot pair; any diff = operational state changed.
STATE_FAIL=0
for pair in ops_work_manifest ops_pytest_cache ops_git_status ops_forbidden; do
  if diff -u "$PROOF/${pair}_before.txt" "$PROOF/${pair}_after.txt" \
       > "$PROOF/${pair}_diff.txt" 2>&1; then
    echo "   $pair: UNCHANGED"
  else
    echo "   $pair: CHANGED — see $PROOF/${pair}_diff.txt" >&2
    STATE_FAIL=1
  fi
done
if [ "${POST_ESCAPES:-0}" -ne 0 ]; then
  echo "   symlinks: ESCAPE INTO OPERATIONAL WORKTREE — see $PROOF/symlinks_post.txt" >&2
  STATE_FAIL=1
fi

# Result extraction (isolated run_pipeline results, never the operational file).
"$PY" - "$REPO/work/test_results.ndjson" <<'PYEOF' > "$PROOF/pipeline_counts.txt"
import json, sys
path = sys.argv[1]
suites = passed = failed = a_pass = a_fail = 0
try:
    for line in open(path):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        if r.get("type") != "test_suite": continue
        suites += 1
        if r.get("status") == "pass": passed += 1
        else: failed += 1
        a_pass += int(r.get("passed", 0)); a_fail += int(r.get("failed", 0))
    print("pipeline_suites=%d pass=%d fail=%d assertions_pass=%d assertions_fail=%d"
          % (suites, passed, failed, a_pass, a_fail))
except FileNotFoundError:
    print("pipeline_suites=ABSENT (run_pipeline did not run)")
PYEOF
cat "$PROOF/pipeline_counts.txt"

VERDICT=PASS
[ "$STEP_FAIL" -ne 0 ] && VERDICT=FAIL_TESTS
[ "$STATE_FAIL" -ne 0 ] && VERDICT=FAIL_STATE_CHANGED
{
  echo "verdict=$VERDICT"
  echo "root=$REAL_ROOT"
  echo "ops_root=$OPS_ROOT"
  echo "head=$OPS_HEAD"
  echo "port_base=$PORT_BASE"
  cat "$PROOF/steps.txt" 2>/dev/null
  cat "$PROOF/pipeline_counts.txt"
  for pair in ops_work_manifest ops_pytest_cache ops_git_status ops_forbidden; do
    s="$(shasum -a 256 "$PROOF/${pair}_before.txt" | cut -d' ' -f1)"
    t="$(shasum -a 256 "$PROOF/${pair}_after.txt"  | cut -d' ' -f1)"
    echo "${pair}_before_sha256=$s"
    echo "${pair}_after_sha256=$t"
  done
  echo "work_files_before=$(grep -c . "$PROOF/ops_work_manifest_before.txt")"
  echo "work_files_after=$(grep -c . "$PROOF/ops_work_manifest_after.txt")"
} > "$PROOF/RUN_SUMMARY.txt"
cat "$PROOF/RUN_SUMMARY.txt"

echo "== isolated_run VERDICT: $VERDICT (proof: $PROOF) =="
[ "$VERDICT" = "PASS" ] || exit 1
exit 0
