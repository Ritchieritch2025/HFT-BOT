#!/usr/bin/env bash
# W-A3 — full-fleet MD5 verification of vaulted objects. Runs ON THE EC2 BOX.
#     bash deploy/vault_verify.sh <md5_manifest> <local_root_strip> <s3_prefix>
# The manifest is "md5  path" lines produced on the Mac (md5 -r, sorted).
# For every line: fetch the S3 object's ETag and compare to the Mac MD5.
# Works because vault_sync.sh forces single-part uploads (ETag == MD5).
# Prints only mismatches/missing; summary line at the end. Exit 1 on any bad.
set -euo pipefail
MANIFEST="$1"; STRIP="$2"; PREFIX="$3"
# shellcheck disable=SC1090
source "$HOME/.kalshi/env.sh"
bad=0; n=0
while read -r want path; do
  key="$PREFIX${path#"$STRIP"}"
  n=$((n+1))
  etag=$(aws s3api head-object --bucket kalshi-vault-ritcardo --key "$key" \
           --query ETag --output text 2>/dev/null | tr -d '"') || etag=MISSING
  if [ "$etag" != "$want" ]; then
    echo "BAD $key want=$want got=$etag"
    bad=$((bad+1))
  fi
done < "$MANIFEST"
echo "VERIFY: $n objects checked, $bad mismatches"
[ "$bad" -eq 0 ]
