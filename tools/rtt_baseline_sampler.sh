#!/bin/bash
# ---------------------------------------------------------------------------
# rtt_baseline_sampler.sh — pre-migration cross-time RTT baseline collector.
#
# WHY THIS EXISTS (operator request 2026-07-08): the existing kalshi_facts RTT
# number (p50 35.6 ms) is a SINGLE back-to-back burst taken once on 2026-07-07.
# The Mac->Kalshi cross-region path is PERISHABLE data — once W-A4 cutover moves
# capture into AWS us-east-2, the old cross-region latency can never be measured
# over time again. This sampler fires ONE bench_rtt burst on a schedule (launchd,
# default every 5 min) and appends a structured record, so we accumulate a real
# cross-time "before" curve to diff against the post-migration EC2 numbers.
#
# It is DELIBERATELY minimal and separate from the pipeline:
#   * runs CREDENTIAL-FREE (bench_rtt hits a public endpoint with a throwaway
#     key when no creds are set) — never touches ~/.kalshi (S4-trivial);
#   * does NOT touch capture/ingest/export — it only spawns bench_rtt and appends
#     to its own file under work/ (S5, off the hot path);
#   * failures are RECORDED (ok:false rows), never silently dropped (D2 spirit).
#
# STOP IT AT CUTOVER: this adds periodic REST calls from the Mac. During the
# W-A4 overlap there must be a SINGLE REST owner, so unload this launchd agent
# before/at cutover (see deploy/com.ritcardo.rtt-baseline.plist header).
#
# Output: work/latency_baseline/samples.ndjson  (one JSON object per line)
# Retire: after W-A5 the permanent sampler is W-D2 (observatory) on EC2.
# ---------------------------------------------------------------------------
set -uo pipefail

# Repo root = parent of this script's dir (works regardless of launchd's cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

BENCH="$ROOT/build/bench_rtt"
# Warm requests per burst. 20 is too few — a 20-sample p99 collapses onto p90.
# 100 makes the per-burst p90/p99 meaningful; ~3.5 s wall at ~35 ms each, every
# 5 min, well inside the read-rate budget (100x10=1000 tokens over ~3.5 s ≈
# 285 tok/s < 300 refill, no throttle). Override with RTT_N if desired.
# NOTE: this stores per-burst SUMMARY percentiles only. A true cross-time p99.9
# needs RAW per-request samples pooled over the whole horizon (percentiles do
# NOT aggregate) — that rigorous baseline is W-LAT-BENCH (playbook 3h), not this.
N="${RTT_N:-100}"
OUT_DIR="$ROOT/work/latency_baseline"
OUT_FILE="$OUT_DIR/samples.ndjson"
mkdir -p "$OUT_DIR"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

emit() {                               # append one line to the file AND stdout
  printf '%s\n' "$1" >>"$OUT_FILE"
  printf '%s\n' "$1"
}

json_err() {                           # collapse arbitrary text into a safe JSON string
  printf '%s' "$1" | tr -d '\n' | tr '"' "'" | cut -c1-160
}

if [[ ! -x "$BENCH" ]]; then
  emit "{\"ts_utc\":\"$TS\",\"ok\":false,\"error\":\"bench_rtt not found or not executable at $BENCH\"}"
  exit 0
fi

OUT="$("$BENCH" "$N" 2>&1)"; RC=$?
WARM="$(printf '%s\n' "$OUT" | grep '^warm lane RTT' || true)"

if [[ $RC -ne 0 || -z "$WARM" ]]; then
  emit "{\"ts_utc\":\"$TS\",\"ok\":false,\"rc\":$RC,\"error\":\"$(json_err "$OUT")\"}"
  exit 0
fi

TARGET="$(printf '%s\n' "$OUT" | grep '^target:'     | sed -E 's/^target: //')"
COLD="$(printf '%s\n' "$OUT"   | grep '^cold'         | sed -E 's/.*: ([0-9.]+) ms$/\1/')"
NN="$(printf '%s\n' "$WARM"    | sed -E 's/.*n=([0-9]+):.*/\1/')"
MN="$(printf '%s\n' "$WARM"    | sed -E 's/.*min=([0-9.]+).*/\1/')"
P50="$(printf '%s\n' "$WARM"   | sed -E 's/.*p50=([0-9.]+).*/\1/')"
P90="$(printf '%s\n' "$WARM"   | sed -E 's/.*p90=([0-9.]+).*/\1/')"
P99="$(printf '%s\n' "$WARM"   | sed -E 's/.*p99=([0-9.]+).*/\1/')"
SKEW="$(printf '%s\n' "$OUT"   | grep '^clock skew'   | sed -E 's/.*: (-?[0-9]+)ms.*/\1/')"
[[ -z "$SKEW" ]] && SKEW="null"

emit "{\"ts_utc\":\"$TS\",\"target\":\"$TARGET\",\"cold_ms\":$COLD,\"warm_min_ms\":$MN,\"warm_p50_ms\":$P50,\"warm_p90_ms\":$P90,\"warm_p99_ms\":$P99,\"n\":$NN,\"clock_skew_ms\":$SKEW,\"ok\":true}"
exit 0
