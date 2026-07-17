#!/usr/bin/env bash
# W-A5 ②: EC2 -> S3 continuous vault sync. Runs ON THE BOX via systemd timers:
#   hourly: raw (completed hours only — current-hour GLOB excluded, the W-A3
#           torn-copy lesson: firehose_<HH>.ndjson* covers retry segments) +
#           reports/  ("prompt sub-daily sync" for the fragile early window)
#   daily:  everything hourly does PLUS the warehouse durable layer
#           (facts/dim/catalog/manifest — the archive), after the midnight
#           export + 02:00 sweep.
# Rules: NEVER --delete (D1). Single-part uploads (ETag==MD5, threshold 2GB).
# Creds from ~/.kalshi/env.sh at runtime (S4, supervisor pattern).
# Usage: ec2_s3_sync.sh hourly|daily|research_sync YYYY-MM-DD
#   research_sync keeps copied-v2 as the rollback/default path until the
#   separately audited v3 canary passes.  After that cutover only, the
#   operator-owned zero-copy approval file turns this into a logged no-op.
set -euo pipefail
MODE="${1:?usage: ec2_s3_sync.sh hourly|daily|research_sync YYYY-MM-DD}"
cd "$HOME/hft-bot"
if [ "$MODE" = research_sync ]; then
  RDATE="${2:?usage: ec2_s3_sync.sh research_sync YYYY-MM-DD}"
  if [ -f "$HOME/.kalshi/research_zero_copy_v3_cutover_approved" ]; then
    echo "[ec2_s3_sync] LEGACY_RESEARCH_COPY_DISABLED date=$RDATE mode=ZERO_COPY_V3_CUTOVER rfq=DATA_INTEGRITY_BLOCKED $(date -u +%FT%TZ)"
    exit 0
  fi
fi
# shellcheck disable=SC1090
source "$HOME/.kalshi/env.sh"
aws configure set default.s3.multipart_threshold 2GB
DST="s3://kalshi-vault-ritcardo/ec2"
HH="$(date -u +%H)"

if [ "$MODE" = research_sync ]; then
  # Before canary/cutover, preserve the verified v2 rollback path.  RFQ is
  # forced OFF on the command line so a stale environment variable or legacy
  # flag file cannot reopen the terminated branch.
  APPROVE=()
  [ -f "$HOME/.kalshi/research_publish_approved" ] && \
    APPROVE=(--operator-approved)
  python3 tools/research_release.py publish --date "$RDATE" --no-rfq \
    "${APPROVE[@]}"
  echo "[ec2_s3_sync] copied-v2 research_sync complete for $RDATE rfq=OFF $(date -u +%FT%TZ)"
  exit 0
fi

# raw: all day-dirs, excluding every channel family being written right now.
# RFQ capture is a separate connection/file family; uploading its active file
# would recreate the W-A3 torn-copy incident even though its volume is small.
aws s3 sync work/raw "$DST/raw" --no-progress \
  --exclude "*/firehose_${HH}.ndjson*" \
  --exclude "*/l2_${HH}.ndjson*" \
  --exclude "*/rfq_${HH}.ndjson*" \
  --exclude "*/rfq_receipts_${HH}.ndjson*"
# reports (may not exist yet — W-R builds the generator)
[ -d reports ] && aws s3 sync reports "$DST/reports" --no-progress

if [ "$MODE" = daily ]; then
  for d in facts dim catalog legacy_greed _meta seals corrections; do
    [ -d "work/warehouse/$d" ] && \
      aws s3 sync "work/warehouse/$d" "$DST/warehouse/$d" --no-progress
  done
  [ -f work/warehouse/manifest.csv ] && \
    aws s3 cp work/warehouse/manifest.csv "$DST/warehouse/manifest.csv" --no-progress
fi
echo "[ec2_s3_sync] $MODE sync complete $(date -u +%FT%TZ) (excluded active firehose/l2/rfq hour $HH)"
