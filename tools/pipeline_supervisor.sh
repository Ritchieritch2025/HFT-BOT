#!/usr/bin/env bash
# 24/7 all-markets data pipeline supervisor — three layers.
#
#   LAYER 1  ws_shadow firehose (ticker+trade, ALL markets) appends to hourly
#            raw logs:   work/raw/date=<YYYY-MM-DD>/firehose_<HH>.ndjson
#            (ws_shadow opens the capture append-only, so restarts within the
#            same hour keep appending — no truncation, no data loss)
#   LAYER 2  tools/ingest.py --loop tails the raw logs every ~60s into
#            work/warehouse/staging.duckdb (change-only + hourly heartbeats,
#            checkpointed byte offsets — restart-safe)
#   LAYER 3  tools/export_day.py archives each completed UTC day (sorted,
#            zstd-15 Parquet / csv.gz), verifies, manifests, prunes staging
#
# Meant to be kept alive by a launchd LaunchAgent
# (deploy/com.ritcardo.kalshi-pipeline.plist).
#
# Credentials are sourced from ~/.kalshi/env.sh which YOU create (never stored
# by the tooling):
#   export KALSHI_API_KEY_ID=...
#   export KALSHI_PRIVATE_KEY_PATH=$HOME/.kalshi/private_key.pem
#
# Tunables (env): CATALOG_EVERY_HOURS (default 1), FULL_CATALOG_EVERY_HOURS
# (default 6), RAW_RETENTION_DAYS (default 2, matches config/warehouse.yaml),
# AUTO_RESEARCH (default 0 on the production data plane).
set -u
cd "$(dirname "$0")/.."

CREDS="$HOME/.kalshi/env.sh"
if [ ! -f "$CREDS" ]; then
  echo "[supervisor] FATAL: $CREDS not found. Create it with your KALSHI_API_KEY_ID"
  echo "             and KALSHI_PRIVATE_KEY_PATH exports, then reload the LaunchAgent."
  exit 78   # EX_CONFIG
fi
# Capture the service/operator setting before sourcing the credential file.
# A stray variable in ~/.kalshi/env.sh must not widen production behavior.
AUTO_RESEARCH_REQUESTED="${AUTO_RESEARCH:-0}"
# shellcheck disable=SC1090
source "$CREDS"
export KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=data_collect

CATALOG_EVERY_HOURS="${CATALOG_EVERY_HOURS:-1}"
FULL_CATALOG_EVERY_HOURS="${FULL_CATALOG_EVERY_HOURS:-6}"
# 3 -> 2: operator ruling 2026-07-10 (audit B3 disk math) — raw is vaulted
# to S3 hourly since W-A5, so 2 local days is a safe window on the 200GB box.
RAW_RETENTION_DAYS="${RAW_RETENTION_DAYS:-2}"
# PIPE-HOTFIX-02 (operator-approved 2026-07-11): heavy sealed-day research is
# fail-closed OFF on the production capture box.  A malformed value must never
# widen permissions.  Seal verification, capture_gaps and coverage_audit stay
# enabled; only mm_scan/mm_backtest/mm_calibrate are fused off.
AUTO_RESEARCH="$AUTO_RESEARCH_REQUESTED"
unset AUTO_RESEARCH_REQUESTED
case "$AUTO_RESEARCH" in
  0|1) ;;
  *)
    echo "[supervisor] WARN invalid AUTO_RESEARCH=$AUTO_RESEARCH; forcing 0"
    AUTO_RESEARCH=0
    ;;
esac

RAW="work/raw"; LIVE="work/live"; mkdir -p "$RAW" "$LIVE"
WAREHOUSE_ROOT="work/warehouse"
SEAL_ALARM="$LIVE/seal_alarm.json"

# --- LAYER 1b (PIPE-W06 Stage 1): targeted sports L2 — OPTIONAL layer ---------
# A SECOND read-only ws_shadow on its own WS connection (orderbook_delta,
# explicit tickers from the hourly l2_targets selector), capturing to its own
# raw family work/raw/date=<D>/l2_<HH>.ndjson. Everything about this layer is
# fail-closed and firehose-independent (P4): it launches backgrounded, all of
# its failure modes stay inside run_l2_shadow, and one-touch disable is
# `touch work/live/l2_disable` (checked before each hourly start AND polled
# mid-segment). orderbook_delta ONLY: ws_shadow sends ONE subscribe command
# with ONE market filter, so a market-less global market_lifecycle_v2
# subscription cannot ride this instance (it would be filtered to the target
# list, not global) — global lifecycle stays a Stage-2 item.
L2_TARGETS_CSV="$LIVE/l2_targets.csv"
L2_DISABLE="$LIVE/l2_disable"
L2_LOG="$LIVE/l2_shadow.log"
L2_LOCK="$LIVE/l2_shadow.lock"
L2_ALERT="$LIVE/l2_alert.json"
L2_TARGETS_MAX_AGE_SECS="${L2_TARGETS_MAX_AGE_SECS:-7200}"
L2_POLL_SECS="${L2_POLL_SECS:-15}"
L2_MIN_SEGMENT_SECS="${L2_MIN_SEGMENT_SECS:-60}"

# --- single-instance lock: two supervisors = two ws_shadow writers appending --
# --- to the SAME hourly raw file = interleaved corrupt lines. Never allow it. --
LOCK="$LIVE/supervisor.lock"
if mkdir "$LOCK" 2>/dev/null; then
  echo $$ > "$LOCK/pid"
elif kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then
  echo "[supervisor] another instance (pid $(cat "$LOCK/pid")) is running; exiting"
  exit 0
else
  rm -rf "$LOCK"; mkdir "$LOCK"; echo $$ > "$LOCK/pid"
fi
cleanup() {
  [ -f "$LIVE/ingest.pid" ] && kill "$(cat "$LIVE/ingest.pid")" 2>/dev/null
  kill "$WATCHDOG_PID" 2>/dev/null
  [ -n "${WS_PID:-}" ] && kill "$WS_PID" 2>/dev/null
  [ -n "${SEAL_PID:-}" ] && kill "$SEAL_PID" 2>/dev/null
  # LAYER 1b: stop the l2 runner subshell (its TERM trap kills its ws_shadow)
  # plus a best-effort direct kill of the l2 ws_shadow via the lock's pid file.
  [ -n "${L2_PID:-}" ] && kill "$L2_PID" 2>/dev/null
  [ -f "$L2_LOCK/ws_pid" ] && kill "$(cat "$L2_LOCK/ws_pid")" 2>/dev/null
  rm -rf "$LOCK"
}
# W-A5 (audit finding 3): a signal trap in bash RESUMES execution after the
# handler — the old `trap cleanup EXIT INT TERM` cleaned up and then kept
# looping, so every systemctl stop escalated to SIGKILL after 30 s. Split:
# INT/TERM exit(143) -> the EXIT trap runs cleanup exactly once.
trap cleanup EXIT
trap 'exit 143' INT TERM

echo "[supervisor] start pid=$$ raw=$RAW retention=${RAW_RETENTION_DAYS}d auto_research=$AUTO_RESEARCH"

# --- LAYER 2: one ingest daemon, watched every 60s -----------------------------
ingest_alive() {
  [ -f "$LIVE/ingest.pid" ] && kill -0 "$(cat "$LIVE/ingest.pid")" 2>/dev/null
}
start_ingest() {
  python3 tools/ingest.py --loop >> "$LIVE/ingest.log" 2>&1 &
  echo $! > "$LIVE/ingest.pid"
  echo "[supervisor] ingest daemon started pid=$(cat "$LIVE/ingest.pid")"
}
stop_ingest_for_export() {
  ingest_alive || return 0
  ingest_pid="$(cat "$LIVE/ingest.pid")"
  kill "$ingest_pid" 2>/dev/null || true
  # Do not race DuckDB's writer teardown.  If it cannot exit, fail this export
  # attempt and let capture continue; never open the DB after an arbitrary sleep.
  for _wait_i in $(seq 1 120); do
    if ! kill -0 "$ingest_pid" 2>/dev/null; then
      rm -f "$LIVE/ingest.pid"
      return 0
    fi
    sleep 1
  done
  echo "[supervisor] ingest pid=$ingest_pid did not stop within 120s; export deferred"
  return 1
}
write_seal_alarm() {
  # Durable 02:00-line alarm artifact (operator seal ruling: 02:00 is an
  # ALARM line, not a scheduling gate). Cleared only by a verified seal.
  python3 - "$1" "$2" "$SEAL_ALARM" <<'PY'
import datetime, json, os, sys
date, kind, path = sys.argv[1:]
rec = {"date": date, "kind": kind,
       "observed_at_utc": datetime.datetime.now(datetime.timezone.utc)
       .strftime("%Y-%m-%dT%H:%M:%SZ")}
if os.path.exists(path):
    try:
        prior = json.load(open(path))
        rec["first_observed_utc"] = prior.get("first_observed_utc",
                                              prior.get("observed_at_utc"))
        rec["occurrences"] = int(prior.get("occurrences", 0)) + 1
    except Exception:
        rec["occurrences"] = 1
else:
    rec["first_observed_utc"] = rec["observed_at_utc"]
    rec["occurrences"] = 1
tmp = path + ".tmp"
json.dump(rec, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY
}

seal_chain_active() {
  [ -n "${SEAL_PID:-}" ] && kill -0 "$SEAL_PID" 2>/dev/null
}

# --- LAYER 1b helpers (PIPE-W06 Stage 1) --------------------------------------
l2_alert() {
  # Durable LAYER 1b state artifact (D2: a skipped/failed/disabled optional
  # layer is surfaced on disk, never silent). Overwritten on state change;
  # removed on a healthy start.
  printf '{"status":"%s","reason":"%s","ts_utc":"%s"}\n' \
    "$1" "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$L2_ALERT.tmp" \
    && mv "$L2_ALERT.tmp" "$L2_ALERT"
}

run_l2_shadow() {
  # LAYER 1b body — ALWAYS invoked backgrounded from the main loop, never on
  # the LAYER 1 launch path (P4): selector refresh, freshness gate, lock and
  # the l2 ws_shadow itself all live inside this subshell, so no L2 failure
  # mode (REST down, stale targets, crash loop, slow exchange) can delay or
  # block the firehose. Every refusal is logged AND alerted (D2).
  trap 'kill "${L2_WS_PID:-}" 2>/dev/null; rm -rf "$L2_LOCK"; exit 143' TERM INT
  if [ -f "$L2_DISABLE" ]; then
    echo "[l2_shadow] one-touch disable present ($L2_DISABLE); layer off"
    l2_alert "disabled" "l2_disable flag present"
    return 0
  fi
  # Hourly selector refresh BEFORE start (spec §2). W-A4 single-REST-owner:
  # rest_disabled suspends this REST spender too; targets then age past
  # L2_TARGETS_MAX_AGE_SECS and the gate below stops the layer (fail-closed).
  if [ -f "$LIVE/rest_disabled" ]; then
    echo "[l2_shadow] REST disabled ($LIVE/rest_disabled present): selector refresh skipped (stale targets will stop this layer, fail-closed)"
  else
    python3 tools/l2_targets.py >> "$LIVE/l2_targets.log" 2>&1 \
      || echo "[l2_shadow] selector refresh FAILED (previous targets kept; the layer stops once they go stale)"
  fi
  # Fail-closed start gate: stale/missing/empty targets => L2 does not start.
  L2_TICKERS="$(python3 tools/l2_targets.py --check \
      --max-age-secs "$L2_TARGETS_MAX_AGE_SECS" 2>>"$LIVE/l2_targets.log")" || {
    echo "[l2_shadow] targets stale/missing/empty; LAYER 1b not started (fail-closed)"
    l2_alert "no_targets" "l2_targets.csv stale/missing/empty (see l2_targets.log)"
    return 0
  }
  # Single-instance lock: two l2 writers appending to the SAME hourly raw file
  # would interleave corrupt lines (same failure class as the supervisor lock).
  if mkdir "$L2_LOCK" 2>/dev/null; then
    echo "${BASHPID:-$$}" > "$L2_LOCK/pid"
  elif kill -0 "$(cat "$L2_LOCK/pid" 2>/dev/null)" 2>/dev/null; then
    echo "[l2_shadow] another instance (pid $(cat "$L2_LOCK/pid")) is running; skipping this start"
    return 0
  else
    rm -rf "$L2_LOCK"; mkdir "$L2_LOCK"; echo "${BASHPID:-$$}" > "$L2_LOCK/pid"
  fi
  rm -f "$L2_ALERT"
  # Own metrics file, own rotation — NEVER work/metrics.ndjson (double-writer
  # lesson); rotate between segments while the writer is not running.
  bash tools/rotate_metrics.sh "$LIVE/l2_metrics.ndjson" || true
  while true; do
    if [ -f "$L2_DISABLE" ]; then
      echo "[l2_shadow] disable flag appeared; layer stopping"
      l2_alert "disabled" "l2_disable flag present"
      break
    fi
    L2_DAY="$(date -u +%F)"; L2_HH="$(date -u +%H)"
    L2_DAYDIR="$RAW/date=$L2_DAY"; mkdir -p "$L2_DAYDIR"
    L2_SECS=$(( 3600 - 10#$(date -u +%M) * 60 - 10#$(date -u +%S) ))
    if [ "$L2_SECS" -lt "$L2_MIN_SEGMENT_SECS" ]; then
      break  # too close to the hour boundary; the next cycle owns the next hour
    fi
    # ws_shadow opens the capture append-only, so a within-hour relaunch keeps
    # appending to l2_<HH>.ndjson (no truncation — same contract as LAYER 1).
    KALSHI_WS_FIREHOSE=0 KALSHI_MODE=data_collect \
      KALSHI_WS_CHANNELS=orderbook_delta \
      KALSHI_WS_TICKERS="$L2_TICKERS" \
      KALSHI_SHADOW_SECONDS="$L2_SECS" \
      KALSHI_SHADOW_CAPTURE="$L2_DAYDIR/l2_$L2_HH.ndjson" \
      KALSHI_SHADOW_METRICS="$LIVE/l2_metrics.ndjson" \
      KALSHI_SHADOW_XCHECK=0 \
      ./build/ws_shadow >> "$L2_LOG" 2>&1 &
    L2_WS_PID=$!
    echo "$L2_WS_PID" > "$L2_LOCK/ws_pid"
    echo "[l2_shadow] segment started pid=$L2_WS_PID capture=$L2_DAYDIR/l2_$L2_HH.ndjson secs=$L2_SECS tickers=$(printf '%s' "$L2_TICKERS" | awk -F, '{print NF}')"
    # Mid-segment one-touch disable: poll the flag and kill the segment early
    # (the rest_disabled/RFQ-style flag pattern; ws_shadow shuts down cleanly
    # on TERM and flushes its recorder).
    while kill -0 "$L2_WS_PID" 2>/dev/null; do
      if [ -f "$L2_DISABLE" ]; then
        echo "[l2_shadow] disable flag appeared mid-segment; stopping pid $L2_WS_PID"
        kill "$L2_WS_PID" 2>/dev/null
      fi
      sleep "$L2_POLL_SECS" & wait $!
    done
    wait "$L2_WS_PID" 2>/dev/null; l2_rc=$?
    L2_WS_PID=""
    rm -f "$L2_LOCK/ws_pid"
    if [ -f "$L2_DISABLE" ]; then
      l2_alert "disabled" "segment stopped mid-hour by l2_disable"
      break
    fi
    if [ "$l2_rc" -eq 0 ]; then
      break  # clean bounded-session end at the hour boundary
    fi
    echo "[l2_shadow] ws_shadow exited rc=$l2_rc; retrying in 15s (bounded to this hour)"
    l2_alert "retrying" "l2 ws_shadow exited rc=$l2_rc"
    sleep 15 & wait $!
  done
  rm -rf "$L2_LOCK"
}

# BACKLOG B4 (2026-07-11 incident): the chain used to `touch`/`rm -f` the
# export_pause unconditionally and deleted the operator's 10:45Z backfill
# pause, restarting daemons into a manual backfill. The pause now carries an
# ownership token; the chain refuses its export window while a FOREIGN pause
# exists and only ever removes a pause it wrote itself. A stale pause from a
# dead seal chain (supervisor kill mid-window) is reclaimed loudly; anything
# else is operator property and is never touched.
acquire_export_pause() {
  pause="$LIVE/export_pause"
  if [ -f "$pause" ]; then
    owner_pid="$(sed -n 's/^seal_chain pid=\([0-9][0-9]*\)$/\1/p' "$pause" 2>/dev/null)"
    if [ -z "$owner_pid" ]; then
      echo "[supervisor] FOREIGN export_pause present (operator/manual); export window refused, pause left in place"
      return 1
    fi
    if kill -0 "$owner_pid" 2>/dev/null; then
      echo "[supervisor] export_pause held by live seal chain pid=$owner_pid; export window refused"
      return 1
    fi
    echo "[supervisor] reclaiming stale seal-chain export_pause (dead pid=$owner_pid)"
  fi
  # BASHPID = this background chain's own pid (bash>=4, production); the $$
  # fallback (parent pid) only serves bash 3.2 test hosts — ownership match
  # stays exact either way because release greps the same expansion.
  echo "seal_chain pid=${BASHPID:-$$}" > "$pause"
}
release_export_pause() {
  pause="$LIVE/export_pause"
  [ -f "$pause" ] || return 0
  if grep -qx "seal_chain pid=${BASHPID:-$$}" "$pause" 2>/dev/null; then
    rm -f "$pause"
  else
    echo "[supervisor] export_pause is not ours (foreign/replaced); left in place"
  fi
}

# Watchdog: the main loop blocks inside hour-long ws_shadow runs, so a crashed
# ingest daemon must be revived independently (skipped during the export pause).
( while true; do
    sleep 60
    # W-C2.1: refresh the live capture-gap alert every cycle. Read-only over raw
    # (checks the newest firehose segment's freshness), writes ONLY
    # work/live/capture_alert.json + exits non-zero on an active gap. It never
    # touches ws_shadow/ingest/export, so it cannot affect capture continuity (P4).
    python3 tools/capture_gaps.py --live >/dev/null 2>&1
    [ -f "$LIVE/export_pause" ] && continue
    ingest_alive || start_ingest
  done ) &
WATCHDOG_PID=$!

hour_cycle=0
SEAL_PID=""
EXPORT_ATTEMPT_LOG="$LIVE/export_attempt.log"

seal_identity() {
  python3 - "$1" <<'PY'
import hashlib
import json
import os
import sys
sys.path.insert(0, "tools")
import warehouse_common as wc
date = sys.argv[1]
cfg = wc.load_config()
path = wc.seal_path(cfg["warehouse_root"], date)
with open(path, "rb") as f:
    raw = f.read()
seal = json.loads(raw)
print(hashlib.sha256(raw).hexdigest() + " " +
      str(seal.get("manifest_date_sha256", "")))
PY
}

publish_research_receipt() {
  research_date="$1"; seal_sha="$2"; manifest_sha="$3"; receipt="$4"
  python3 - "$research_date" "$seal_sha" "$manifest_sha" "$receipt" <<'PY'
import json
import os
import sys
date, seal_sha, manifest_sha, path = sys.argv[1:]
payload = {
    "date": date,
    "status": "NON_GATE",
    "capture_quality": "UNASSESSED_PENDING_PIPE_W03",
    "fees_fills_queue": "NOT_VALIDATED",
    "seal_sha256": seal_sha,
    "manifest_date_sha256": manifest_sha,
}
tmp = path + ".tmp.%d" % os.getpid()
with open(tmp, "w") as f:
    json.dump(payload, f, sort_keys=True)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, path)
PY
}

research_receipt_current() {
  research_date="$1"; receipt="$2"
  [ -f "$receipt" ] || return 1
  python3 tools/export_day.py --date "$research_date" --verify-seal \
    >> "$LIVE/mm_research.log" 2>&1 || return 1
  identity="$(seal_identity "$research_date")" || return 1
  seal_sha="${identity%% *}"
  manifest_sha="${identity#* }"
  python3 - "$receipt" "$research_date" "$seal_sha" "$manifest_sha" <<'PY'
import json
import sys
path, date, seal_sha, manifest_sha = sys.argv[1:]
try:
    with open(path) as f:
        receipt = json.load(f)
except Exception:
    raise SystemExit(1)
ok = (receipt.get("date") == date and receipt.get("status") == "NON_GATE" and
      receipt.get("seal_sha256") == seal_sha and
      receipt.get("manifest_date_sha256") == manifest_sha)
raise SystemExit(0 if ok else 1)
PY
}

run_daily_research() {
  # Runs SYNCHRONOUSLY inside the single-instance background seal chain
  # (SEAL_PID guard): no duplicate launches, no orphaned grandchildren.
  research_date="$1"
  research_done="$LIVE/research_${research_date}.done.json"
  # These consumers are archive-only by contract.  A failed/missing sealed
  # archive must stop the chain rather than fall back to live staging.
  ( identity_start="$(seal_identity "$research_date")" || exit 1
    if python3 tools/export_day.py --date "$research_date" --verify-seal \
         >> "$LIVE/mm_research.log" 2>&1 &&
       python3 tools/coverage_audit.py --date "$research_date" \
         >> "$LIVE/coverage_audit.log" 2>&1; then
      if [ "$AUTO_RESEARCH" != "1" ]; then
        echo "[supervisor] AUTO_RESEARCH_DISABLED date=$research_date; seal verification and coverage audit completed; mm_scan/mm_backtest/mm_calibrate skipped"
      elif ( python3 tools/mm_scan.py --date "$research_date" --archive-only &&
             python3 tools/mm_backtest.py --date "$research_date" --from-scan 15 --archive-only &&
             python3 tools/mm_calibrate.py --date "$research_date" --archive-only ) \
             >> "$LIVE/mm_research.log" 2>&1; then
        # Bind derived success to the exact source seal.  Verify immediately
        # before publish and require the seal identity to remain unchanged
        # across that verification.  Consumers re-check the binding below.
        if python3 tools/export_day.py --date "$research_date" --verify-seal \
             >> "$LIVE/mm_research.log" 2>&1; then
          identity_end="$(seal_identity "$research_date")" || exit 1
          if [ "$identity_start" = "$identity_end" ]; then
            seal_sha="${identity_end%% *}"
            manifest_sha="${identity_end#* }"
            publish_research_receipt "$research_date" "$seal_sha" \
              "$manifest_sha" "$research_done"
          else
            echo "[supervisor] seal changed during research run; receipt blocked"
          fi
        else
          echo "[supervisor] final seal verification failed; receipt blocked"
        fi
      else
        echo "[supervisor] research failed for $research_date; will retry next cycle"
      fi
    else
      echo "[supervisor] coverage audit failed for $research_date; research blocked"
    fi )
}

run_seal_chain() {
  # One completed day: verify-or-create the WRITE-ONCE seal, then gaps +
  # research. Runs in the background (capture never waits on seal work, P4).
  CHAIN_DATE="$1"
  SEAL_FILE="$WAREHOUSE_ROOT/seals/date=${CHAIN_DATE}.json"
  if python3 tools/export_day.py --date "$CHAIN_DATE" --verify-seal \
       >> "$LIVE/export.log" 2>&1; then
    rm -f "$SEAL_ALARM"
  elif [ -f "$SEAL_FILE" ]; then
    # WRITE-ONCE: an existing seal that fails verification is an operator
    # incident (needs --operator-invalidate-seal + rebuild). Never auto-fixed.
    echo "[supervisor] SEAL CORRUPT for $CHAIN_DATE — operator remediation required"
    write_seal_alarm "$CHAIN_DATE" "CORRUPT_SEAL_OPERATOR_REMEDIATION"
    return 1
  elif [ "$(date -u +%H)" -ge 2 ]; then
    : > "$EXPORT_ATTEMPT_LOG"
    chain_ok=0
    # B4: a foreign (operator/manual) pause blocks the window AND stays down —
    # neither the pause file nor the paused ingest daemon is touched.
    if acquire_export_pause; then
      if stop_ingest_for_export; then
        if python3 tools/export_day.py --date "$CHAIN_DATE" --check-caught-up \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1 &&
           python3 tools/export_day.py --date "$CHAIN_DATE" --force --no-prune \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1 &&
           python3 tools/export_day.py --date "$CHAIN_DATE" --seal \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1; then
          chain_ok=1
        fi
      fi
      release_export_pause
      ingest_alive || start_ingest
    fi
    cat "$EXPORT_ATTEMPT_LOG" >> "$LIVE/export.log"
    if [ "$chain_ok" -eq 1 ]; then
      echo "[supervisor] sealed final archive $CHAIN_DATE"
      rm -f "$SEAL_ALARM"
    else
      echo "[supervisor] daily seal failed for $CHAIN_DATE; research blocked"
      if [ "$(date -u +%H)" -ge 3 ]; then
        write_seal_alarm "$CHAIN_DATE" "UNSEALED_PAST_ALARM_LINE"
      fi
      return 1
    fi
  else
    return 0
  fi
  # Valid seal from here on: capture-gap record + gated research.
  if [ ! -f "$LIVE/gaps_${CHAIN_DATE}.done" ]; then
    python3 tools/capture_gaps.py --date "$CHAIN_DATE" \
      >> "$LIVE/capture_gaps.log" 2>&1 && touch "$LIVE/gaps_${CHAIN_DATE}.done"
  fi
  # PIPE-W06: per-day L2 seq-continuity/quality record — post-seal EVIDENCE,
  # non-gating, same posture as capture_gaps (counted-not-hidden, D2).
  # capture_gaps itself is untouched: it owns firehose market-wide silence;
  # targeted-L2 quality has different semantics (only subscribed markets emit).
  if [ ! -f "$LIVE/l2_gaps_${CHAIN_DATE}.done" ]; then
    python3 tools/l2_gap_check.py --date "$CHAIN_DATE" \
      >> "$LIVE/l2_gap_check.log" 2>&1 && touch "$LIVE/l2_gaps_${CHAIN_DATE}.done"
  fi
  # PIPE-W05 Phase A: publish the sealed day as an immutable research release
  # (research/releases/<id>/, MANIFEST last). Additive, archive-only, inside
  # the async seal chain so capture never waits (P4). NO done-file (operator
  # correction order 2026-07-12 fix 1): the publisher is state-aware — a new
  # corrections/gap-evidence/rfq state publishes a DISTINCT release id, and
  # an already-published state is a cheap no-op probe. Failure is logged and
  # retried on the next cycle, never blocking the chain.
  bash deploy/ec2_s3_sync.sh research_sync "$CHAIN_DATE" \
    >> "$LIVE/research_release.log" 2>&1 \
    || echo "[supervisor] research release publish failed for $CHAIN_DATE (state-aware retry next cycle)"
  research_done="$LIVE/research_${CHAIN_DATE}.done.json"
  if [ -f "$research_done" ] && \
     ! research_receipt_current "$CHAIN_DATE" "$research_done"; then
    echo "[supervisor] stale research receipt removed for $CHAIN_DATE"
    rm -f "$research_done"
  fi
  if [ ! -f "$research_done" ]; then
    run_daily_research "$CHAIN_DATE"
  fi
}

while true; do
  [ -f "$LIVE/export_pause" ] || ingest_alive || start_ingest

  # --- reference catalog + classification + dim snapshots (background) --------
  # W-A4 single-REST-owner gate: these three tools are this script's ONLY REST
  # spenders. Touch work/live/rest_disabled to run WS-capture-only (zero REST)
  # during the Mac<->EC2 overlap; remove the flag to become the REST owner.
  # The skip is logged every cycle — a silently-suppressed catalog is D2 rot.
  if [ -f "$LIVE/rest_disabled" ]; then
    echo "[supervisor] REST disabled ($LIVE/rest_disabled present): catalog/classification/dim block skipped this cycle (W-A4 single-REST-owner)"
  elif [ $((hour_cycle % FULL_CATALOG_EVERY_HOURS)) -eq 0 ]; then
    ( python3 tools/catalog_sync.py --settled-pages 40 &&
      python3 tools/build_classification.py &&
      python3 tools/dim_snapshot.py ) >> "$LIVE/catalog.log" 2>&1 &
  elif [ $((hour_cycle % CATALOG_EVERY_HOURS)) -eq 0 ]; then
    ( python3 tools/catalog_sync.py --skip-markets --settled-pages 0 &&
      python3 tools/build_classification.py ) >> "$LIVE/catalog.log" 2>&1 &
  fi

  # --- LAYER 3 (ASYNC): write-once day seal + gated research -------------------
  # Capture continuity (P4): the seal chain NEVER runs on the ws_shadow launch
  # path — it runs at most once at a time in the background, and ws_shadow
  # relaunches immediately regardless of seal work. There is intentionally NO
  # midnight/provisional export or research; 02:00 is the earliest seal
  # attempt and 03:00 is the durable ALARM line for a still-unsealed day.
  YESTERDAY="$(date -u -v-1d +%F 2>/dev/null || date -u -d 'yesterday' +%F)"
  if seal_chain_active; then
    echo "[supervisor] seal/research chain still active (pid=$SEAL_PID)"
  else
    run_seal_chain "$YESTERDAY" &
    SEAL_PID=$!
  fi

  # --- LAYER 1b (ASYNC, OPTIONAL): targeted sports L2 for this hour -----------
  # Fire-and-forget: run_l2_shadow refreshes the selector, applies the
  # fail-closed freshness gate + l2_disable switch, and (only then) runs the
  # second ws_shadow — all inside a backgrounded subshell, so NO LAYER 1b
  # failure or absence can ever delay or block the LAYER 1 firehose below (P4).
  run_l2_shadow >> "$L2_LOG" 2>&1 &
  L2_PID=$!

  # --- LAYER 1: firehose the rest of this UTC hour into the hourly raw log ----
  DAY="$(date -u +%F)"; HH="$(date -u +%H)"
  DAYDIR="$RAW/date=$DAY"; mkdir -p "$DAYDIR"
  CAP="$DAYDIR/firehose_$HH.ndjson"
  SECS_LEFT=$(( 3600 - 10#$(date -u +%M) * 60 - 10#$(date -u +%S) ))
  [ "$SECS_LEFT" -lt 30 ] && SECS_LEFT=30
  # rider (a): rotate metrics between capture segments (writer not running).
  # NO redirect (audit B1, 2026-07-10): systemd creates supervisor.out.log as
  # root via StandardOutput=append:, so a ubuntu-uid `>>` open FAILS and kills
  # the command before rotate_metrics runs (the `|| true` swallowed exactly
  # that for two cycles). Plain stdout already lands in that file via systemd.
  bash tools/rotate_metrics.sh work/metrics.ndjson || true
  # W-A5 (audit finding 3): ws_shadow runs BACKGROUNDED + wait — a foreground
  # child blocks bash signal-trap delivery for the whole hour, which is why
  # systemctl stop used to time out into SIGKILL. `wait` is interruptible.
  KALSHI_WS_FIREHOSE=1 KALSHI_SHADOW_SECONDS="$SECS_LEFT" \
    KALSHI_SHADOW_CAPTURE="$CAP" KALSHI_SHADOW_METRICS=work/metrics.ndjson \
    ./build/ws_shadow >> "$LIVE/ws_shadow.log" 2>&1 &
  WS_PID=$!
  if ! wait "$WS_PID"; then
    echo "[supervisor] ws_shadow exited non-zero (will retry in 15s)"
    sleep 15 & wait $!
  fi
  WS_PID=""

  # Seal-gated raw pruning (operator rulings 2026-07-10 retention + 2026-07-11
  # seals): a receipt day is deletable only when its (and its cross-day
  # successor's) seals exist; unsealed-dependency files are always retained.
  # Fail-closed: prune_raw deletes NOTHING on any error and always writes
  # work/live/raw_retention_alert.json (retained-overdue files + reasons).
  ( python3 tools/prune_raw.py --retention-days "$RAW_RETENTION_DAYS" \
      >> "$LIVE/prune_raw.log" 2>&1 \
      || echo "[supervisor] prune_raw NONZERO (fail-closed, nothing deleted)" ) &

  hour_cycle=$((hour_cycle + 1))
done
