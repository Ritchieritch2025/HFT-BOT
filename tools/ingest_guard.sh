# B11 (W-A 2026-07-14): single-writer ingest lifecycle guard.
# Sourced by tools/pipeline_supervisor.sh (and by tests/test_ingest_guard.sh —
# functions only, NO side effects at source time). Requires $LIVE to be set.
#
# Why this exists (BACKLOG B11, root cause of the all-day 07-12 seal block):
# the old start/stop pair trusted work/live/ingest.pid alone. Under a crash
# loop or heavy backlog the watchdog and the main loop could each spawn an
# ingest daemon (two writers, ONE pidfile), and stop_ingest_for_export then
# killed only the pidfile's pid — the orphan kept the staging writer lock and
# export_day --force lost the ATTACH race cycle after cycle, all day.
#
# The guard's contract:
#   * the PROCESS TABLE is the truth, the pidfile is only a hint: every
#     decision enumerates live ingest daemons (same uid + exact cmdline +,
#     where /proc exists, cwd == this repo) instead of trusting the pidfile;
#   * start_ingest never spawns while an export_pause exists, never spawns a
#     second daemon next to a live one (it adopts the survivor into the
#     pidfile instead), and re-checks the pause AFTER spawning — if a seal
#     chain acquired the pause in that window, the fresh daemon is killed
#     again immediately (TOCTOU closed from the starter's side);
#   * stop_ingest_for_export TERMs EVERY live ingest daemon (orphans
#     included) and only returns 0 once the process table shows zero.

INGEST_CMD_PATTERN="${INGEST_CMD_PATTERN:-tools/ingest\.py --loop}"

ingest_procs() {
  # Every live ingest daemon of ours: same uid, exact cmdline; on hosts with
  # /proc (production Linux) additionally cwd == $PWD, so an operator's
  # manual run in a scratch clone is never touched. macOS test hosts have no
  # /proc — there the uid+cmdline match stands alone.
  pgrep -u "$(id -u)" -f "$INGEST_CMD_PATTERN" 2>/dev/null | while read -r _p; do
    if [ -d /proc ] && [ -e "/proc/$_p/cwd" ]; then
      [ "$(readlink "/proc/$_p/cwd" 2>/dev/null)" = "$PWD" ] || continue
    fi
    echo "$_p"
  done
}

ingest_alive() {
  [ -n "$(ingest_procs)" ]
}

start_ingest() {
  # Never start a writer under a pause (seal chain owns the DB window).
  [ -f "$LIVE/export_pause" ] && return 0
  _live="$(ingest_procs)"
  if [ -n "$_live" ]; then
    # A daemon (possibly an orphan from a lost pidfile) is already writing:
    # adopt it instead of spawning a second writer.
    printf '%s\n' "$_live" | head -1 > "$LIVE/ingest.pid"
    return 0
  fi
  python3 tools/ingest.py --loop >> "$LIVE/ingest.log" 2>&1 &
  echo $! > "$LIVE/ingest.pid"
  # Close the pause-vs-spawn TOCTOU: if a seal chain acquired the pause while
  # we were spawning, undo ours right here — the chain's verified stop then
  # sees the dying pid and waits it out.
  if [ -f "$LIVE/export_pause" ]; then
    kill "$(cat "$LIVE/ingest.pid")" 2>/dev/null
    rm -f "$LIVE/ingest.pid"
    return 0
  fi
  echo "[supervisor] ingest daemon started pid=$(cat "$LIVE/ingest.pid")"
}

stop_ingest_for_export() {
  # Verified stop (recover_rfq_seal clauses 16-18 posture): TERM every live
  # daemon and only report stopped when the PROCESS TABLE shows zero. Do not
  # race DuckDB's writer teardown; if they cannot all exit, fail this export
  # attempt and let capture continue — never open the DB after a blind sleep.
  _pids="$(ingest_procs)"
  if [ -z "$_pids" ]; then
    rm -f "$LIVE/ingest.pid"
    return 0
  fi
  for _p in $_pids; do kill "$_p" 2>/dev/null || true; done
  for _wait_i in $(seq 1 120); do
    _pids="$(ingest_procs)"
    if [ -z "$_pids" ]; then
      rm -f "$LIVE/ingest.pid"
      return 0
    fi
    sleep 1
  done
  echo "[supervisor] ingest daemon(s) $_pids did not stop within 120s; export deferred"
  return 1
}
