#!/bin/bash
# ec2_monitor.sh — READ-ONLY EC2 production monitoring snapshot (seal/ingest
# progress + backlog). Sibling of ec2_health.sh under the SAME operator
# permission model (plan-A, extended 2026-07-13): a FIXED read-only command
# set by construction — ls/stat/grep/tail/pgrep/free/df ONLY. NO kill, no
# restart, no systemctl-mutate, no rm, no write of any kind. Editing this file
# invalidates the standing allow-rule — treat any change as a new W + re-approval.
set -u
KEY="$HOME/.ssh/kalshi-key.pem"
HOST="ubuntu@3.130.232.109"
exec ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" '
cd /home/ubuntu/hft-bot || { echo "REPO_DIR_MISSING"; exit 9; }
echo "NOW_UTC=$(date -u +%FT%TZ)"
echo "SEALS:"; ls work/warehouse/seals/ 2>/dev/null | sed "s/^/  /"
[ -f work/live/seal_alarm.json ] && sed "s/^/SEAL_ALARM: /" work/live/seal_alarm.json || echo "SEAL_ALARM=NONE"
[ -f work/live/export_pause ] && echo "EXPORT_PAUSE=PRESENT ($(cat work/live/export_pause 2>/dev/null | head -c 80))" || echo "EXPORT_PAUSE=NONE"
echo "INGEST_DAEMONS=$(pgrep -c -f "[i]ngest.py --loop" 2>/dev/null || echo 0)"
echo "CATCHUP_LAST: $(grep -E "INGEST CATCH-UP (PASS|FAIL) 2026-[0-9-]+|sealed final archive" work/live/export.log 2>/dev/null | tail -1 | head -c 140)"
echo "BEHIND_LAST: $(grep -oE "behind raw: [0-9]+ file" work/live/export.log 2>/dev/null | tail -1)"
tail -3 work/live/ingest.log 2>/dev/null | sed "s/^/INGEST_LOG: /"
stat -c "STAGING_SIZE=%s STAGING_MTIME=%y" work/warehouse/staging.duckdb 2>/dev/null
find work/raw -type f -name "*.ndjson*" -printf "%T@ %p\n" 2>/dev/null | sort -nr | head -1 | sed "s/^/RAW_NEWEST=/"
df -h /home/ubuntu | tail -1 | awk "{print \"DISK_USED=\" \$5 \" AVAIL=\" \$4}"
free -m | awk "/^Mem:/ {print \"MEM_AVAIL_MB=\" \$7} /^Swap:/ {print \"SWAP_USED_MB=\" \$3}"
[ -f work/live/raw_retention_alert.json ] && sed "s/^/RETENTION_ALERT: /" work/live/raw_retention_alert.json || echo "RETENTION_ALERT=NONE"
'
