#!/usr/bin/env bash
# W-A3 — box-side S3 vault sync (PLAN_AWS_MIGRATION). Runs ON THE EC2 BOX.
#     bash deploy/vault_sync.sh <staging_subdir> <s3_prefix>
# e.g. bash deploy/vault_sync.sh raw/date=2026-07-06 mac-vault/raw/date=2026-07-06
#
# Rules it enforces (W-A3 seven fields):
#   - NEVER --delete (D1: nothing is ever removed, on S3 or anywhere).
#   - Single-part uploads (multipart threshold 2 GB > largest file 663 MB) so
#     every S3 ETag == the file's MD5 — full-fleet md5 verification for free.
#   - Credentials come from ~/.kalshi/env.sh at runtime (S4: operator-created;
#     same pattern as the production supervisor). Never printed.
set -euo pipefail
SRC="$HOME/vault_staging/$1"
DST="s3://kalshi-vault-ritcardo/$2"
[ -d "$SRC" ] || { echo "no such staging dir: $SRC"; exit 2; }
# shellcheck disable=SC1090
source "$HOME/.kalshi/env.sh"
# one-time idempotent config: ETag==MD5 requires single-part PUTs
aws configure set default.s3.multipart_threshold 2GB
aws s3 sync "$SRC" "$DST" --no-progress
echo "SYNCED $SRC -> $DST"
