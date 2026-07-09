#!/usr/bin/env bash
# W-A5 ③ / rider (e): app-layer alert notifier. Runs every minute via
# kalshi-alert.timer. READ-ONLY over pipeline state (S5, off the hot path).
#
# Checks (each -> one line in work/live/alerts.log, the dashboard stream):
#   capture   work/live/capture_alert.json status != "ok"
#   freshness newest raw file older than FRESH_LIMIT_S (default 120 s)
#   disk      / usage above DISK_LIMIT_PCT (default 80%)
# Delivery: PRIMARY Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from
# ~/.kalshi/env.sh, operator-created, S4 — a plain HTTPS POST). Unconfigured
# token => log-only (stated loudly once per boot, D2). Email backup rides
# CloudWatch/SNS (operator console, see SECURITY_CHECKLIST/W-A5 notes).
# Anti-spam: one notification per state-CHANGE (hash stamp file), plus a
# re-send every REALERT_MIN (default 30) minutes while still bad.
# ALERT_DRY=1 prints instead of POSTing (used by the D4 test).
set -u
cd "$HOME/hft-bot" 2>/dev/null || cd "$(dirname "$0")/.."
LIVE=work/live
LOG="$LIVE/alerts.log"
STAMP="$LIVE/.alert_state"
FRESH_LIMIT_S="${FRESH_LIMIT_S:-120}"
DISK_LIMIT_PCT="${DISK_LIMIT_PCT:-80}"
REALERT_MIN="${REALERT_MIN:-30}"
[ -f "$HOME/.kalshi/env.sh" ] && . "$HOME/.kalshi/env.sh"

BAD=""
# capture alert (W-C2.1 live detector output)
if [ -f "$LIVE/capture_alert.json" ]; then
  st="$(python3 -c "import json;print(json.load(open('$LIVE/capture_alert.json')).get('status','?'))" 2>/dev/null || echo parse_error)"
  [ "$st" = "ok" ] || BAD="$BAD capture:$st"
fi
# raw freshness (newest file mtime anywhere under work/raw)
if [ -d work/raw ]; then
  newest="$(find work/raw -type f -newermt "-${FRESH_LIMIT_S} seconds" | head -1)"
  [ -n "$newest" ] || BAD="$BAD feed_stale>${FRESH_LIMIT_S}s"
fi
# disk
pct="$(df / | awk 'NR==2 {gsub("%","",$5); print $5}')"
[ "$pct" -le "$DISK_LIMIT_PCT" ] || BAD="$BAD disk:${pct}%"

now_min=$(( $(date -u +%s) / 60 ))
prev="$(cat "$STAMP" 2>/dev/null || echo "OK 0")"
prev_state="${prev%% *}"; prev_min="${prev##* }"
state="OK"; [ -n "$BAD" ] && state="BAD"

notify() {
  msg="$1"
  echo "$(date -u +%FT%TZ) $msg" >> "$LOG"
  if [ "${ALERT_DRY:-0}" = "1" ]; then
    echo "DRY-NOTIFY: $msg"
  elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -sm 10 -o /dev/null "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=[kalshi-ec2] $msg" || \
      echo "$(date -u +%FT%TZ) WARN telegram send failed" >> "$LOG"
  else
    echo "$(date -u +%FT%TZ) WARN telegram UNCONFIGURED (log-only): $msg" >> "$LOG"
  fi
}

if [ "$state" = "BAD" ]; then
  if [ "$prev_state" != "BAD" ] || [ $(( now_min - prev_min )) -ge "$REALERT_MIN" ]; then
    notify "ALERT:$BAD"
    echo "BAD $now_min" > "$STAMP"
  fi
else
  if [ "$prev_state" = "BAD" ]; then
    notify "RECOVERED (all checks green)"
  fi
  echo "OK $now_min" > "$STAMP"
fi
exit 0
