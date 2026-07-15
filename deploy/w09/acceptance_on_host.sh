#!/bin/bash
set -Eeuo pipefail

CACHE="/srv/w09-research/cache"
LOG_ROOT="/srv/w09-research/acceptance"
mkdir -p "$LOG_ROOT"
chmod 0750 "$LOG_ROOT"

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
research_data inventory | tee "$LOG_ROOT/inventory.txt"
RID="$(/opt/w09/venv/bin/python \
  /opt/w09/research/tools/select_newest_release.py --cache "$CACHE")"
/opt/w09/venv/bin/python \
  /opt/w09/research/tools/select_newest_release.py \
  --cache "$CACHE" --json | tee "$LOG_ROOT/selected_release.json"

required="$(python3 -c 'import json; print(json.load(open("'"$LOG_ROOT"'/selected_release.json"))["object_bytes"] + 10*1024**3)')"
available="$(df -B1 --output=avail "$CACHE" | tail -1 | tr -d ' ')"
if [ "$available" -lt "$required" ]; then
    echo "W09_DISK_GATE: need=$required available=$available" >&2
    exit 75
fi

w09-run research_data fetch --release "$RID" --with-rfq \
  | tee "$LOG_ROOT/fetch.txt"
w09-run research_data verify --release "$RID" \
  | tee "$LOG_ROOT/verify.txt"

python3 - "$CACHE/releases/$RID/.VERIFIED.json" <<'PY'
import json, sys
marker = json.load(open(sys.argv[1]))
if marker.get("version_binding_mode") != "VERSION_BOUND":
    raise SystemExit("VERSION_GATE: verification is not VERSION_BOUND")
print("VERSION_BOUND_VERIFIED release=%s tier=%s tl1=%s" %
      (marker.get("release_id"), marker.get("evidence_tier"),
       marker.get("tl1_status")))
PY

echo "W09_READY release_id=$RID source=S3_ONLY production_ssh=NONE compute_usd_per_running_hour=0.4713 track_A=HELD_PENDING_W05_ACCEPTED"
