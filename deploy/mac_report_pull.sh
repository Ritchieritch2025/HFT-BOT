#!/usr/bin/env bash
# W-A5 report flow-back, Mac side. Called daily by
# com.ritcardo.kalshi-report-pull.plist. Two stages because macOS TCC blocks
# background (launchd) jobs from writing to ~/Desktop until the operator
# grants Full Disk Access to /bin/bash (System Settings → Privacy & Security
# → Full Disk Access): stage 1 always works (inbox outside TCC scope);
# stage 2 degrades to a WARN until the grant exists — late, never lost (the
# reports also live on EC2 and in S3).
set -u
INBOX="$HOME/kalshi_reports_inbox"
DEST="$HOME/Desktop/TradingSys Report"
LOG="/tmp/kalshi-report-pull.log"
mkdir -p "$INBOX"
rsync -a -e "ssh -i $HOME/.ssh/kalshi-key.pem -o ConnectTimeout=15" \
  ubuntu@3.130.232.109:hft-bot/reports/ "$INBOX/" \
  >> "$LOG" 2>&1 || { echo "$(date) WARN: EC2 pull failed (box offline?)" >> "$LOG"; exit 0; }
if cp -R "$INBOX/." "$DEST/" 2>>"$LOG"; then
  echo "$(date) OK: reports landed in Desktop/TradingSys Report" >> "$LOG"
else
  echo "$(date) WARN: Desktop blocked by macOS privacy (grant Full Disk Access to /bin/bash); reports waiting in $INBOX" >> "$LOG"
fi
exit 0
