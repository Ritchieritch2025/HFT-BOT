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
# (default 6), RAW_RETENTION_DAYS (default 2, matches config/warehouse.yaml).
set -u
cd "$(dirname "$0")/.."

CREDS="$HOME/.kalshi/env.sh"
if [ ! -f "$CREDS" ]; then
  echo "[supervisor] FATAL: $CREDS not found. Create it with your KALSHI_API_KEY_ID"
  echo "             and KALSHI_PRIVATE_KEY_PATH exports, then reload the LaunchAgent."
  exit 78   # EX_CONFIG
fi
# shellcheck disable=SC1090
source "$CREDS"
export KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=data_collect

CATALOG_EVERY_HOURS="${CATALOG_EVERY_HOURS:-1}"
FULL_CATALOG_EVERY_HOURS="${FULL_CATALOG_EVERY_HOURS:-6}"
# 3 -> 2: operator ruling 2026-07-10 (audit B3 disk math) — raw is vaulted
# to S3 hourly since W-A5, so 2 local days is a safe window on the 200GB box.
RAW_RETENTION_DAYS="${RAW_RETENTION_DAYS:-2}"

RAW="work/raw"; LIVE="work/live"; mkdir -p "$RAW" "$LIVE"

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
  [ -n "${RESEARCH_PID:-}" ] && kill "$RESEARCH_PID" 2>/dev/null
  rm -rf "$LOCK"
}
# W-A5 (audit finding 3): a signal trap in bash RESUMES execution after the
# handler — the old `trap cleanup EXIT INT TERM` cleaned up and then kept
# looping, so every systemctl stop escalated to SIGKILL after 30 s. Split:
# INT/TERM exit(143) -> the EXIT trap runs cleanup exactly once.
trap cleanup EXIT
trap 'exit 143' INT TERM

echo "[supervisor] start pid=$$ raw=$RAW retention=${RAW_RETENTION_DAYS}d"

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
stop_research_for_reseal() {
  [ -n "$RESEARCH_PID" ] || return 0
  if ! kill -0 "$RESEARCH_PID" 2>/dev/null; then
    RESEARCH_PID=""
    return 0
  fi
  # Research may have a Python child. Stop child first, then its subshell, and
  # do not rewrite archives until both are gone; otherwise stale output can
  # recreate a success receipt after reseal.
  pkill -TERM -P "$RESEARCH_PID" 2>/dev/null || true
  kill -TERM "$RESEARCH_PID" 2>/dev/null || true
  for _research_wait in $(seq 1 30); do
    if ! kill -0 "$RESEARCH_PID" 2>/dev/null; then
      wait "$RESEARCH_PID" 2>/dev/null || true
      RESEARCH_PID=""
      return 0
    fi
    sleep 1
  done
  pkill -KILL -P "$RESEARCH_PID" 2>/dev/null || true
  kill -KILL "$RESEARCH_PID" 2>/dev/null || true
  for _research_kill_wait in $(seq 1 5); do
    kill -0 "$RESEARCH_PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$RESEARCH_PID" 2>/dev/null; then
    return 1
  fi
  wait "$RESEARCH_PID" 2>/dev/null || true
  RESEARCH_PID=""
  return 0
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
LAST_SEALED=""
LAST_GAPS=""
RESEARCH_PID=""
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
  research_date="$1"
  research_done="$LIVE/research_${research_date}.done.json"
  if [ -n "$RESEARCH_PID" ] && kill -0 "$RESEARCH_PID" 2>/dev/null; then
    echo "[supervisor] research pid=$RESEARCH_PID still active; duplicate launch blocked"
    return 0
  fi
  # These consumers are archive-only by contract.  A failed/missing sealed
  # archive must stop the chain rather than fall back to live staging.
  ( identity_start="$(seal_identity "$research_date")" || exit 1
    if python3 tools/export_day.py --date "$research_date" --verify-seal \
         >> "$LIVE/mm_research.log" 2>&1 &&
       python3 tools/coverage_audit.py --date "$research_date" \
         >> "$LIVE/coverage_audit.log" 2>&1; then
      if ( python3 tools/mm_scan.py --date "$research_date" --archive-only &&
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
    fi ) &
  RESEARCH_PID=$!
}

while true; do
  ingest_alive || start_ingest

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

  # --- LAYER 3: seal a completed UTC day after the late-ingest window ----------
  # There is intentionally NO midnight/provisional export or research.  At/after
  # 02:00, pause ingest, prove all closed raw bytes are checkpointed, rebuild the
  # final archive once, prove row CONTENT parity, then atomically seal it.  Any
  # failed proof leaves research blocked and retries next hour.
  YESTERDAY="$(date -u -v-1d +%F 2>/dev/null || date -u -d 'yesterday' +%F)"
  HOUR_NOW="$(date -u +%H)"
  RESEAL_BLOCKED=0
  if [ "$LAST_SEALED" = "$YESTERDAY" ]; then
    if ! python3 tools/export_day.py --date "$YESTERDAY" --verify-seal \
         >> "$LIVE/export.log" 2>&1; then
      echo "[supervisor] prior seal became invalid for $YESTERDAY; reseal required"
      if stop_research_for_reseal; then
        LAST_SEALED=""
        rm -f "$LIVE/research_${YESTERDAY}.done.json"
      else
        RESEAL_BLOCKED=1
        echo "[supervisor] active research could not stop; reseal deferred"
      fi
    fi
  fi
  if [ "$RESEAL_BLOCKED" -eq 0 ] && [ "$LAST_SEALED" != "$YESTERDAY" ] \
     && [ "$HOUR_NOW" -ge 2 ]; then
    : > "$EXPORT_ATTEMPT_LOG"
    if python3 tools/export_day.py --date "$YESTERDAY" --verify-seal \
         >> "$EXPORT_ATTEMPT_LOG" 2>&1; then
      LAST_SEALED="$YESTERDAY"
      echo "[supervisor] reused valid day seal $YESTERDAY"
    else
      touch "$LIVE/export_pause"
      rm -f "$LIVE/research_${YESTERDAY}.done.json"
      if stop_ingest_for_export; then
        if python3 tools/export_day.py --date "$YESTERDAY" --check-caught-up \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1 &&
           python3 tools/export_day.py --date "$YESTERDAY" --force --no-prune \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1 &&
           python3 tools/export_day.py --date "$YESTERDAY" --seal \
             >> "$EXPORT_ATTEMPT_LOG" 2>&1; then
          LAST_SEALED="$YESTERDAY"
          echo "[supervisor] sealed final archive $YESTERDAY"
        else
          echo "[supervisor] daily seal failed for $YESTERDAY; research blocked"
        fi
      fi
      rm -f "$LIVE/export_pause"
      ingest_alive || start_ingest
    fi
    cat "$EXPORT_ATTEMPT_LOG" >> "$LIVE/export.log"
  fi

  if [ "$LAST_SEALED" = "$YESTERDAY" ]; then
    # W-C2.1: record the just-completed day's capture gaps into the durable
    # structured record (event_validate V-EP15's source) BEFORE its raw ages out
    # of the 3-day retention — an unscanned pruned day loses its gaps forever.
    # Read-only over raw; writes ONLY the derived work/event_packs/capture_gaps.csv.
    # Backgrounded + placed AFTER the pause is lifted and ingest is back, so it
    # extends neither the ingest pause nor the ws_shadow relaunch (P4 preserved).
    if [ "$LAST_GAPS" != "$YESTERDAY" ]; then
      python3 tools/capture_gaps.py --date "$YESTERDAY" \
        >> "$LIVE/capture_gaps.log" 2>&1 &
      LAST_GAPS="$YESTERDAY"
    fi
    research_done="$LIVE/research_${YESTERDAY}.done.json"
    if [ -f "$research_done" ] && \
       ! research_receipt_current "$YESTERDAY" "$research_done"; then
      echo "[supervisor] stale research receipt removed for $YESTERDAY"
      rm -f "$research_done"
    fi
    if [ ! -f "$research_done" ]; then
      run_daily_research "$YESTERDAY"
    fi
  fi

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

  # Raw deletion is deliberately disabled until PIPE-W04's per-file S3
  # checksum/version receipt gate exists.  A date seal is not a vault receipt.
  # Alert on pressure; retain source-of-truth bytes rather than deleting on mtime.
  find "$RAW" -name '*.ndjson*' -type f -mtime +"$RAW_RETENTION_DAYS" -print \
    > "$LIVE/raw_retention_pending.txt" 2>/dev/null

  hour_cycle=$((hour_cycle + 1))
done
