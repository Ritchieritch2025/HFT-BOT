#!/bin/bash
# ec2_health.sh — READ-ONLY EC2 production health snapshot.
# Operator permission plan A (2026-07-11): this script is the ONLY
# classifier-exempt SSH path for agent sessions. It contains a FIXED,
# read-only command set by construction: status queries, stat, find, tail.
# NO kill/restart/systemctl-mutate/rm/write of any kind. Changing this file
# invalidates the permission grant — treat any edit as a new W + re-approval.
set -u
KEY="$HOME/.ssh/kalshi-key.pem"
HOST="ubuntu@3.130.232.109"
exec ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" '
cd /home/ubuntu/hft-bot || { echo "REPO_DIR_MISSING"; exit 9; }
echo "NOW_UTC=$(date -u +%FT%TZ)"
echo "SERVICE=$(systemctl is-active kalshi-pipeline 2>/dev/null)"
# bracket trick: the pattern must not match this scripts own remote cmdline
pgrep -f "[m]m_calibrate|[m]m_scan|[m]m_backtest" >/dev/null 2>&1 \
  && echo "RESEARCH_CHAIN=RUNNING" || echo "RESEARCH_CHAIN=GONE"
pid="$(cat work/live/ingest.pid 2>/dev/null || true)"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  echo "INGEST=ALIVE pid=$pid"
else
  echo "INGEST=DEAD"
fi
stat -c "STAGING_MTIME=%y SIZE=%s" work/warehouse/staging.duckdb 2>/dev/null
find work/raw -type f -name "*.ndjson*" -printf "%T@ %s %p\n" 2>/dev/null \
  | sort -nr | head -1 | sed "s/^/RAW_NEWEST=/"
df -h /home/ubuntu | tail -1 | awk "{print \"DISK_USED=\" \$5 \" AVAIL=\" \$4}"
free -m | awk "/^Mem:/ {print \"MEM_USED_MB=\" \$3 \" AVAIL_MB=\" \$7}"
tail -3 work/live/ingest.log 2>/dev/null | sed "s/^/INGEST_LOG: /"
'
