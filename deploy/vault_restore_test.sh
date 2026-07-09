#!/usr/bin/env bash
# W-A3 — MANDATORY RESTORE TEST. Runs ON THE EC2 BOX.
#     bash deploy/vault_restore_test.sh <s3_key_prefix_or_key> <scratch_subdir>
# Pulls the sample back from S3 into ~/vault_scratch/<scratch_subdir> and
# prints md5sums of everything restored. The session then compares these
# against the Mac originals byte-for-byte (scp back + cmp on the Mac).
# Read-only against S3 (Get); never touches vault_staging or the Mac.
set -euo pipefail
KEY="$1"; OUT="$HOME/vault_scratch/$2"
mkdir -p "$OUT"
# shellcheck disable=SC1090
source "$HOME/.kalshi/env.sh"
# single object first; only fall back to prefix-recursive if that fails.
# (NOT the other way round: `cp --recursive` on an exact key treats it as a
# prefix, matches nothing, and still exits 0 — a silent no-op restore.)
aws s3 cp "s3://kalshi-vault-ritcardo/$KEY" "$OUT/" --no-progress \
  || aws s3 cp "s3://kalshi-vault-ritcardo/$KEY" "$OUT/" --recursive --no-progress
RESTORED=$(find "$OUT" -type f | wc -l)
[ "$RESTORED" -gt 0 ] || { echo "RESTORE FAILED: zero files under $OUT"; exit 1; }
find "$OUT" -type f -exec md5sum {} \; | sort -k2
