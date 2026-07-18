#!/bin/bash
set -Eeuo pipefail

EXPECTED_INSTANCE_ID="i-0e53d134dceffe166"
EXPECTED_PROFILE="w09-research-runner"
EXPECTED_ROLE="w09-research-runner"
PAYLOAD_ROOT="${1:-/tmp/w09-bringup}"
INSTALL_ROOT="/opt/w09/research"
VENV="/opt/w09/venv"
CACHE_ROOT="/srv/w09-research/cache"

if [ "$(id -u)" -ne 0 ]; then
    echo "W09_INSTALL_REFUSED: run as root" >&2
    exit 77
fi
for module in research_data.py research_reference.py warehouse_common.py; do
    if [ ! -f "$PAYLOAD_ROOT/tools/$module" ]; then
        echo "W09_INSTALL_REFUSED: missing reader module: $module" >&2
        exit 66
    fi
done
for module in deep03_v3_common.py deep03_v3_prepare.py \
              deep03_v3_methods.py deep03_v3_runner.py; do
    if [ ! -f "$PAYLOAD_ROOT/tools/research/$module" ]; then
        echo "W09_INSTALL_REFUSED: missing Deep03 module: $module" >&2
        exit 66
    fi
done
if [ ! -f "$PAYLOAD_ROOT/deploy/w09/research_reader_modules.sha256" ]; then
    echo "W09_INSTALL_REFUSED: missing reader module manifest" >&2
    exit 66
fi
if ! (cd "$PAYLOAD_ROOT" && sha256sum -c \
      deploy/w09/research_reader_modules.sha256 >/dev/null); then
    echo "W09_INSTALL_REFUSED: reader module integrity mismatch" >&2
    exit 65
fi
for file in v3_query_canary.py v3_query_canary.sha256; do
    if [ ! -f "$PAYLOAD_ROOT/deploy/w09/$file" ]; then
        echo "W09_INSTALL_REFUSED: missing v3 query canary payload: $file" >&2
        exit 66
    fi
done
if ! (cd "$PAYLOAD_ROOT" && sha256sum -c \
      deploy/w09/v3_query_canary.sha256 >/dev/null); then
    echo "W09_INSTALL_REFUSED: v3 query canary integrity mismatch" >&2
    exit 65
fi
if [ ! -f "$PAYLOAD_ROOT/deploy/w09/deep03_open_discovery_modules.sha256" ]; then
    echo "W09_INSTALL_REFUSED: Deep03 module manifest missing" >&2
    exit 66
fi
if ! (cd "$PAYLOAD_ROOT" && sha256sum -c \
      deploy/w09/deep03_open_discovery_modules.sha256 >/dev/null); then
    echo "W09_INSTALL_REFUSED: Deep03 module integrity mismatch" >&2
    exit 65
fi
if ! grep -qx 'instance=i-0e53d134dceffe166 behavior=stop operator-confirmed' \
    "$PAYLOAD_ROOT/shutdown-behavior-stop.confirmed" 2>/dev/null; then
    echo "W09_INSTALL_REFUSED: stop-not-terminate behavior was not confirmed" >&2
    exit 77
fi

install -d -m 0755 /run
printf 'provisioning\n' > /run/w09-idle-provisioning
fail() {
    rc=$?
    echo "W09_INSTALL_FAILED rc=$rc; provisioning marker retained" >&2
    exit "$rc"
}
trap fail ERR

if [ "$(uname -m)" != "aarch64" ]; then
    echo "W09_INSTALL_REFUSED: expected arm64, got $(uname -m)" >&2
    exit 77
fi
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    echo "W09_INSTALL_REFUSED: expected Ubuntu 24.04" >&2
    exit 77
fi

IMDS_TOKEN="$(curl -fsS --max-time 3 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \
  http://169.254.169.254/latest/api/token)"
INSTANCE_DOCUMENT="$(curl -fsS --max-time 3 \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/dynamic/instance-identity/document)"
INSTANCE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["instanceId"])' \
  <<<"$INSTANCE_DOCUMENT")"
REGION="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])' \
  <<<"$INSTANCE_DOCUMENT")"
ROLE="$(curl -fsS --max-time 3 \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/)"
PROFILE_ARN="$(curl -fsS --max-time 3 \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/info \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["InstanceProfileArn"])')"
PROFILE_NAME="${PROFILE_ARN##*/}"
if [ "$INSTANCE_ID" != "$EXPECTED_INSTANCE_ID" ] || \
   [ "$REGION" != "us-east-2" ] || \
   [ "$PROFILE_NAME" != "$EXPECTED_PROFILE" ] || \
   [ "$ROLE" != "$EXPECTED_ROLE" ]; then
    echo "W09_INSTALL_REFUSED: identity mismatch instance=$INSTANCE_ID region=$REGION profile=$PROFILE_NAME role=$ROLE" >&2
    exit 77
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    ca-certificates chrony curl iproute2 python3 python3-pip python3-venv \
    procps sudo util-linux
timedatectl set-timezone UTC
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/amazon-time-sync.sources" \
    /etc/chrony/sources.d/amazon-time-sync.sources
systemctl enable --now chrony.service
systemctl restart chrony.service

install -d -m 0755 /opt/w09 "$INSTALL_ROOT/tools" \
    "$INSTALL_ROOT/tools/research" "$INSTALL_ROOT/config" /etc/w09
install -m 0644 "$PAYLOAD_ROOT/tools/research_data.py" \
    "$INSTALL_ROOT/tools/research_data.py"
install -m 0644 "$PAYLOAD_ROOT/tools/research_reference.py" \
    "$INSTALL_ROOT/tools/research_reference.py"
install -m 0644 "$PAYLOAD_ROOT/tools/warehouse_common.py" \
    "$INSTALL_ROOT/tools/warehouse_common.py"
for module in deep03_v3_common.py deep03_v3_prepare.py \
              deep03_v3_methods.py deep03_v3_runner.py; do
    install -m 0644 "$PAYLOAD_ROOT/tools/research/$module" \
        "$INSTALL_ROOT/tools/research/$module"
done
install -m 0644 \
    "$PAYLOAD_ROOT/deploy/w09/deep03_open_discovery_modules.sha256" \
    "$INSTALL_ROOT/deep03_open_discovery_modules.sha256"
install -m 0644 "$PAYLOAD_ROOT/config/warehouse.yaml" \
    "$INSTALL_ROOT/config/warehouse.yaml"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/research_data_instance_profile.py" \
    "$INSTALL_ROOT/tools/research_data_instance_profile.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/select_newest_release.py" \
    "$INSTALL_ROOT/tools/select_newest_release.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/v3_query_canary.py" \
    "$INSTALL_ROOT/tools/v3_query_canary.py"
QUERY_CANARY_SHA="$(awk \
    '$2 == "deploy/w09/v3_query_canary.py" {print $1}' \
    "$PAYLOAD_ROOT/deploy/w09/v3_query_canary.sha256")"
if ! [[ "$QUERY_CANARY_SHA" =~ ^[0-9a-f]{64}$ ]]; then
    echo "W09_INSTALL_REFUSED: invalid v3 query canary SHA manifest" >&2
    exit 65
fi
printf '%s  %s\n' "$QUERY_CANARY_SHA" \
    "$INSTALL_ROOT/tools/v3_query_canary.py" \
    > /etc/w09/v3_query_canary.sha256
chmod 0444 /etc/w09/v3_query_canary.sha256
sha256sum -c /etc/w09/v3_query_canary.sha256 >/dev/null

python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# duckdb pinned to production 1.4.5 (asserted below). numpy/pandas for
# strategy math + small result frames; scipy/statsmodels for the mission's
# multiple-comparison correction + bootstrap + regression diagnostics;
# matplotlib for REPORT/charts/**; pyarrow for parquet interop with DuckDB.
"$VENV/bin/pip" install --quiet \
    'duckdb==1.4.5' numpy pandas scipy statsmodels matplotlib pyarrow pytest pyyaml
"$VENV/bin/python" -c \
    'import duckdb; assert duckdb.__version__ == "1.4.5"; print("duckdb=1.4.5")'
PYTHONPATH="$INSTALL_ROOT/tools" "$VENV/bin/python" -c \
    'import research_data as rd, research_reference as rr; assert rd.ref is rr; print("research_reader=v2+v3")'

install -d -o ubuntu -g ubuntu -m 0750 /srv/w09-research "$CACHE_ROOT"
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/cost-contract.json" \
    /etc/w09/cost-contract.json
cat > /usr/local/bin/research_data <<'EOF'
#!/bin/sh
set -eu
exec /opt/w09/venv/bin/python \
  /opt/w09/research/tools/research_data_instance_profile.py \
  --cache /srv/w09-research/cache "$@"
EOF
chmod 0755 /usr/local/bin/research_data
cat > /usr/local/bin/deep03-v3-prepare <<'EOF'
#!/bin/sh
set -eu
exec /opt/w09/venv/bin/python \
  /opt/w09/research/tools/research/deep03_v3_prepare.py "$@"
EOF
chmod 0755 /usr/local/bin/deep03-v3-prepare
cat > /usr/local/bin/deep03-v3-run <<'EOF'
#!/bin/sh
set -eu
exec /opt/w09/venv/bin/python \
  /opt/w09/research/tools/research/deep03_v3_runner.py "$@"
EOF
chmod 0755 /usr/local/bin/deep03-v3-run

install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09_idle_check.py" \
    /usr/local/sbin/w09-idle-check
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09_idle_proof.py" \
    /usr/local/sbin/w09-idle-proof
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09_idle_confirm_stop.py" \
    /usr/local/sbin/w09-idle-confirm-stop
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09-run" /usr/local/bin/w09-run
install -d -m 0755 /usr/local/libexec
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09-inhibit-run" \
    /usr/local/libexec/w09-inhibit-run
install -m 0440 "$PAYLOAD_ROOT/deploy/w09/w09-inhibit-run.sudoers" \
    /etc/sudoers.d/w09-inhibit-run
/usr/sbin/visudo -cf /etc/sudoers.d/w09-inhibit-run >/dev/null
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/acceptance_on_host.sh" \
    /usr/local/bin/w09-accept
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/w09-idle-check.service" \
    /etc/systemd/system/w09-idle-check.service
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/w09-idle-check.timer" \
    /etc/systemd/system/w09-idle-check.timer

# W09 holds no trading credential or reusable private key.  The Mac SSH key is
# used by the operator only and is never copied by push_and_install.sh.
if find /home/ubuntu -xdev -type f \
     \( -name '*.pem' -o -name 'rfq_readonly.env.sh' \
        -o -name 'research_s3.env.sh' \) -print -quit | grep -q .; then
    echo "W09_INSTALL_REFUSED: credential/private-key file found" >&2
    exit 77
fi
if [ -d /home/ubuntu/.kalshi ]; then
    echo "W09_INSTALL_REFUSED: /home/ubuntu/.kalshi must not exist" >&2
    exit 77
fi

systemctl daemon-reload
systemctl disable --now w09-idle-check.timer >/dev/null 2>&1 || true

# Prove both fail-safe busy modes before arming the timer.
systemctl start w09-idle-check.service
grep -q '"reason": "provisioning"' /var/lib/w09-idle/events.jsonl
rm -f /run/w09-idle-provisioning
systemctl start w09-idle-check.service
grep -Eq '"reason": "ssh-(tcp|session)"' /var/lib/w09-idle/events.jsonl

systemctl enable --now w09-idle-check.timer
systemctl is-active --quiet chrony.service
systemctl is-active --quiet w09-idle-check.timer
timedatectl show -p Timezone --value | grep -qx UTC
chown -R ubuntu:ubuntu /srv/w09-research

trap - ERR
echo "W09_INSTALL_COMPLETE instance=$INSTANCE_ID profile=$PROFILE_NAME role=$ROLE arch=$(uname -m) duckdb=1.4.5 timezone=UTC idle=1800s"
echo "W09_COST compute_usd_per_running_hour=0.4713 effective_running_with_300GB_gp3_and_ipv4=0.50918"
echo "Disconnect every SSH/ControlMaster session; the real idle proof starts on the next timer observation."
