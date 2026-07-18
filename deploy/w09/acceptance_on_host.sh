#!/bin/bash
set -Eeuo pipefail

CACHE="${W09_ACCEPTANCE_CACHE:-/srv/w09-research/cache-v3-canary}"
LOG_ROOT="/srv/w09-research/acceptance"
mkdir -p "$LOG_ROOT" "$CACHE"
chmod 0750 "$LOG_ROOT" "$CACHE"
RD=(/opt/w09/venv/bin/python
    /opt/w09/research/tools/research_data_instance_profile.py
    --cache "$CACHE")

if [ "$(id -u)" -eq 0 ]; then
    echo "W09_ACCEPTANCE_REFUSED: run as ubuntu, not root" >&2
    exit 77
fi
for name in KALSHI_API_KEY_ID KALSHI_PRIVATE_KEY_PATH \
            AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
    if [ -n "${!name:-}" ]; then
        echo "W09_ACCEPTANCE_REFUSED: static/trading credential environment present ($name)" >&2
        exit 77
    fi
done

sudo /usr/local/sbin/w09-idle-proof | tee "$LOG_ROOT/idle-proof.json"

timedatectl show -p Timezone --value | grep -qx UTC
systemctl is-active --quiet chrony.service
chronyc tracking > "$LOG_ROOT/chrony.txt"
/opt/w09/venv/bin/python -c \
  'import duckdb; assert duckdb.__version__ == "1.4.5"'
cat /etc/w09/cost-contract.json | tee "$LOG_ROOT/cost-contract.json"

# The SSH session protects inventory. Detached fetch/verify additionally uses
# a shutdown inhibitor so acceptance is safe even if the controlling shell dies.
"${RD[@]}" inventory | tee "$LOG_ROOT/inventory.txt"
RID="$(/opt/w09/venv/bin/python \
  /opt/w09/research/tools/select_newest_release.py --cache "$CACHE" \
  --require-v3-reference)"
/opt/w09/venv/bin/python \
  /opt/w09/research/tools/select_newest_release.py \
  --cache "$CACHE" --require-v3-reference --json \
  | tee "$LOG_ROOT/selected_release.json"

required="$(python3 -c 'import json; print(json.load(open("'"$LOG_ROOT"'/selected_release.json"))["object_bytes"] + 10*1024**3)')"
available="$(df -B1 --output=avail "$CACHE" | tail -1 | tr -d ' ')"
if [ "$available" -lt "$required" ]; then
    echo "W09_DISK_GATE: need=$required available=$available" >&2
    exit 75
fi

w09-run "${RD[@]}" fetch --release "$RID" \
  | tee "$LOG_ROOT/fetch.txt"
w09-run "${RD[@]}" verify --release "$RID" \
  | tee "$LOG_ROOT/verify.txt"

python3 - "$CACHE/releases/$RID/.VERIFIED.json" <<'PY'
import json, sys
marker = json.load(open(sys.argv[1]))
if (marker.get("storage_mode") != "REFERENCE_V3" or
        marker.get("version_binding_mode") != "CANONICAL_REFERENCE"):
    raise SystemExit("VERSION_GATE: verification is not zero-copy REFERENCE_V3")
if marker.get("rfq_included") is not False:
    raise SystemExit("RFQ_GATE: standard v3 canary must keep RFQ excluded")
print("CANONICAL_REFERENCE_VERIFIED release=%s tier=%s tl1=%s" %
      (marker.get("release_id"), marker.get("evidence_tier"),
       marker.get("tl1_status")))
PY

sha256sum -c /etc/w09/v3_query_canary.sha256
QUERY_RECEIPT="$LOG_ROOT/v3-query-canary-$RID.json"
w09-run /opt/w09/venv/bin/python \
  /opt/w09/research/tools/v3_query_canary.py \
  --cache "$CACHE" --release "$RID" --receipt "$QUERY_RECEIPT" \
  | tee "$LOG_ROOT/v3-query-canary.txt"
python3 - "$QUERY_RECEIPT" "$RID" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1]))
if (receipt.get("state") != "W09_V3_DUCKDB_QUERY_CANARY_PASS"
        or receipt.get("release_id") != sys.argv[2]
        or receipt.get("storage_mode") != "REFERENCE_V3"
        or receipt.get("version_binding_mode") != "CANONICAL_REFERENCE"
        or receipt.get("evidence_tier") != "SEALED_CONFIRMATION"
        or receipt.get("rfq") != "OFF"
        or not isinstance(receipt.get("table_count"), int)
        or receipt["table_count"] < 1):
    raise SystemExit("QUERY_CANARY_GATE: receipt contract mismatch")
print("DUCKDB_QUERY_CANARY_VERIFIED release=%s tables=%d" %
      (receipt["release_id"], receipt["table_count"]))
PY

echo "W09_READY release_id=$RID mode=REFERENCE_V3 storage=CANONICAL_REFERENCE rfq=OFF duckdb_query_canary=PASS source=S3_ONLY production_ssh=NONE compute_usd_per_running_hour=0.4713 track_A=HELD_PENDING_W05_ACCEPTED"
