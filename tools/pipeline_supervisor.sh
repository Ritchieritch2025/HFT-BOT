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
# (default 6), RAW_RETENTION_DAYS (default 3, matches config/warehouse.yaml).
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
RAW_RETENTION_DAYS="${RAW_RETENTION_DAYS:-3}"

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
  rm -rf "$LOCK"
}
trap cleanup EXIT INT TERM

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
LAST_EXPORTED=""
LAST_SWEPT=""
while true; do
  ingest_alive || start_ingest

  # --- reference catalog + classification + dim snapshots (background) --------
  if [ $((hour_cycle % FULL_CATALOG_EVERY_HOURS)) -eq 0 ]; then
    ( python3 tools/catalog_sync.py --settled-pages 40 &&
      python3 tools/build_classification.py &&
      python3 tools/dim_snapshot.py ) >> "$LIVE/catalog.log" 2>&1 &
  elif [ $((hour_cycle % CATALOG_EVERY_HOURS)) -eq 0 ]; then
    ( python3 tools/catalog_sync.py --skip-markets --settled-pages 0 &&
      python3 tools/build_classification.py ) >> "$LIVE/catalog.log" 2>&1 &
  fi

  # --- LAYER 3: export any completed UTC day exactly once ----------------------
  # DuckDB is single-writer: pause the ingest daemon for the export window.
  YESTERDAY="$(date -u -v-1d +%F 2>/dev/null || date -u -d 'yesterday' +%F)"
  if [ "$LAST_EXPORTED" != "$YESTERDAY" ]; then
    touch "$LIVE/export_pause"
    if ingest_alive; then kill "$(cat "$LIVE/ingest.pid")" 2>/dev/null; sleep 2; fi
    if python3 tools/export_day.py --date "$YESTERDAY" >> "$LIVE/export.log" 2>&1; then
      LAST_EXPORTED="$YESTERDAY"
      echo "[supervisor] exported $YESTERDAY"
    else
      # already-archived (write-once) or dead archive path: both are fine to
      # retry/skip next hour; details are in export.log
      grep -q "EXPORT PASS\|already archived" "$LIVE/export.log" && LAST_EXPORTED="$YESTERDAY"
    fi
    rm -f "$LIVE/export_pause"
    ingest_alive || start_ingest
    # W-C2.1: record the just-completed day's capture gaps into the durable
    # structured record (event_validate V-EP15's source) BEFORE its raw ages out
    # of the 3-day retention — an unscanned pruned day loses its gaps forever.
    # Read-only over raw; writes ONLY the derived work/event_packs/capture_gaps.csv.
    # Backgrounded + placed AFTER the pause is lifted and ingest is back, so it
    # extends neither the ingest pause nor the ws_shadow relaunch (P4 preserved).
    python3 tools/capture_gaps.py --date "$YESTERDAY" >> "$LIVE/capture_gaps.log" 2>&1 &
    # daily research refresh on the freshly archived day (read-only, background)
    ( python3 tools/mm_scan.py --date "$YESTERDAY" &&
      python3 tools/mm_backtest.py --date "$YESTERDAY" --from-scan 15 &&
      python3 tools/mm_calibrate.py --date "$YESTERDAY" ) \
      >> "$LIVE/mm_research.log" 2>&1 &
  fi

  # --- second-pass export sweep (2026-07-07 incident): the midnight export can
  # race the ingest backlog, so rows for yesterday landing in staging minutes
  # later miss the write-once archive (day-06 lost 45.9k trades until force-
  # re-exported by hand). Once per day, after 02:00 UTC, re-export yesterday
  # with --force to sweep late-ingested rows before the staging prune window.
  HOUR_NOW="$(date -u +%H)"
  if [ "$LAST_EXPORTED" = "$YESTERDAY" ] && [ "$LAST_SWEPT" != "$YESTERDAY" ] \
     && [ "$HOUR_NOW" -ge 2 ]; then
    touch "$LIVE/export_pause"
    if ingest_alive; then kill "$(cat "$LIVE/ingest.pid")" 2>/dev/null; sleep 2; fi
    if python3 tools/export_day.py --date "$YESTERDAY" --force --no-prune \
         >> "$LIVE/export.log" 2>&1; then
      LAST_SWEPT="$YESTERDAY"
      echo "[supervisor] second-pass sweep exported $YESTERDAY"
    fi
    rm -f "$LIVE/export_pause"
    ingest_alive || start_ingest
  fi

  # --- LAYER 1: firehose the rest of this UTC hour into the hourly raw log ----
  DAY="$(date -u +%F)"; HH="$(date -u +%H)"
  DAYDIR="$RAW/date=$DAY"; mkdir -p "$DAYDIR"
  CAP="$DAYDIR/firehose_$HH.ndjson"
  SECS_LEFT=$(( 3600 - 10#$(date -u +%M) * 60 - 10#$(date -u +%S) ))
  [ "$SECS_LEFT" -lt 30 ] && SECS_LEFT=30
  KALSHI_WS_FIREHOSE=1 KALSHI_SHADOW_SECONDS="$SECS_LEFT" \
    KALSHI_SHADOW_CAPTURE="$CAP" KALSHI_SHADOW_METRICS=work/metrics.ndjson \
    ./build/ws_shadow >> "$LIVE/ws_shadow.log" 2>&1 || \
    { echo "[supervisor] ws_shadow exited non-zero (will retry in 15s)"; sleep 15; }

  # --- prune raw logs older than retention (archive parquet is kept forever) --
  find "$RAW" -name '*.ndjson*' -type f -mtime +"$RAW_RETENTION_DAYS" -delete 2>/dev/null
  find "$RAW" -type d -empty -delete 2>/dev/null

  hour_cycle=$((hour_cycle + 1))
done
