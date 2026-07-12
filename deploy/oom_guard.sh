#!/usr/bin/env bash
# One-box CPU/memory isolation guard (W-A1, 16 GB box — PLAN_AWS_MIGRATION
# W-A0 REVISION guardrails). Runs as root every minute via
# kalshi-oom-guard.timer. Idempotent; processes may exit mid-scan, so every
# action tolerates a vanished PID.
#
# Ranking it enforces, worst-case-OOM order (first killed at the top):
#   +300  export_day / ingest / catalog / mm_* python jobs  (batch, restartable
#         — the supervisor revives ingest within 60 s, export retries hourly)
#   -600  the supervisor tree default (unit OOMScoreAdjust)
#  -1000  PRIMARY firehose ws_shadow (capture is NEVER the OOM victim)
#   +300  SECONDARY RFQ ws_shadow (isolated/recoverable; it must never displace
#         the primary firehose under pressure)
#
# CPU/IO: the batch python jobs are also reniced to +10 / io best-effort-7 so
# the nightly gold build cannot starve capture or (Phase 4) the trading
# process on 2 vCPUs. ws_shadow keeps default priority (it needs ~0.1% CPU).
set -u

# Primary capture: pin to -1000. The passive RFQ service deliberately runs a
# second binary with the same process name, so distinguish it by systemd cgroup
# instead of accidentally giving the optional channel equal OOM priority.
for pid in $(pgrep -x ws_shadow 2>/dev/null); do
  CGROUP="$(cat "/proc/$pid/cgroup" 2>/dev/null || true)"
  if printf '%s' "$CGROUP" | grep -Eq 'kalshi(-|\\x2d)rfq(-|\\x2d)capture\.service'; then
    choom -p "$pid" -n 300 >/dev/null 2>&1 || true
    renice -n 10 -p "$pid" >/dev/null 2>&1 || true
    ionice -c 3 -p "$pid" >/dev/null 2>&1 || true
  else
    choom -p "$pid" -n -1000 >/dev/null 2>&1 || true
  fi
done

# batch layer: first OOM victim, lowest CPU/IO priority
BATCH_RE='tools/(export_day|ingest|catalog_sync|build_classification|dim_snapshot|capture_gaps|coverage_audit|mm_scan|mm_backtest|mm_calibrate)\.py'
for pid in $(pgrep -f "$BATCH_RE" 2>/dev/null); do
  choom  -p "$pid" -n 300 >/dev/null 2>&1 || true
  renice -n 10 -p "$pid"  >/dev/null 2>&1 || true
  ionice -c 2 -n 7 -p "$pid" >/dev/null 2>&1 || true
done

exit 0
