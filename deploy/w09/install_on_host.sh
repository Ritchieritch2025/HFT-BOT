#!/bin/bash
set -Eeuo pipefail

EXPECTED_INSTANCE_ID="i-0e53d134dceffe166"
EXPECTED_PROFILE="w09-research-runner"
EXPECTED_ROLE="w09-research-runner"
PAYLOAD_ROOT="${1:-/tmp/w09-bringup}"
INSTALL_ROOT="/opt/w09/research"
VENV="/opt/w09/venv"
CACHE_ROOT="/srv/w09-research/cache"
INBOX_ROOT="/srv/w09-research/inbox"

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
for module in deep03_v3_common.py deep03_v3_w1_preflight.py \
              deep03_v3_prepare.py deep03_v3_methods.py \
              deep03_v3_runner.py; do
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
if [ ! -f "$PAYLOAD_ROOT/deploy/w09/exploratory_autoresearch_payload.sha256" ]; then
    echo "W09_INSTALL_REFUSED: exploratory autoresearch manifest missing" >&2
    exit 66
fi
if ! (cd "$PAYLOAD_ROOT" && sha256sum -c \
      deploy/w09/exploratory_autoresearch_payload.sha256 >/dev/null); then
    echo "W09_INSTALL_REFUSED: exploratory autoresearch payload mismatch" >&2
    exit 65
fi
if [ ! -f "$PAYLOAD_ROOT/deploy/w09/research_inbox_payload.sha256" ]; then
    echo "W09_INSTALL_REFUSED: Research Inbox payload manifest missing" >&2
    exit 66
fi
if ! (cd "$PAYLOAD_ROOT" && sha256sum -c \
      deploy/w09/research_inbox_payload.sha256 >/dev/null); then
    echo "W09_INSTALL_REFUSED: Research Inbox payload mismatch" >&2
    exit 65
fi
if ! grep -Eq '^[0-9a-f]{40}$' \
    "$PAYLOAD_ROOT/deploy/w09/source-commit.txt" 2>/dev/null; then
    echo "W09_INSTALL_REFUSED: missing or invalid exact source commit" >&2
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
    "$INSTALL_ROOT/tools/research" "$INSTALL_ROOT/tools/research/plugins" \
    "$INSTALL_ROOT/deploy/w09" "$INSTALL_ROOT/config" /etc/w09 \
    /etc/w09/deep03 /etc/w09/deep03/approvals
install -m 0644 "$PAYLOAD_ROOT/tools/research_data.py" \
    "$INSTALL_ROOT/tools/research_data.py"
install -m 0644 "$PAYLOAD_ROOT/tools/research_reference.py" \
    "$INSTALL_ROOT/tools/research_reference.py"
install -m 0644 "$PAYLOAD_ROOT/tools/warehouse_common.py" \
    "$INSTALL_ROOT/tools/warehouse_common.py"
for module in deep03_v3_common.py deep03_v3_w1_preflight.py \
              deep03_v3_prepare.py deep03_v3_methods.py \
              deep03_v3_runner.py; do
    install -m 0644 "$PAYLOAD_ROOT/tools/research/$module" \
        "$INSTALL_ROOT/tools/research/$module"
done
for module in inbox.py plan_contract.py data_catalog.py data_resolver.py \
              plugin_api.py; do
    install -m 0644 "$PAYLOAD_ROOT/tools/research/$module" \
        "$INSTALL_ROOT/tools/research/$module"
done
install -m 0644 "$PAYLOAD_ROOT/tools/research/plugins/__init__.py" \
    "$INSTALL_ROOT/tools/research/plugins/__init__.py"
install -m 0444 "$PAYLOAD_ROOT/tools/research/plugins/deep03.py" \
    "$INSTALL_ROOT/tools/research/plugins/deep03.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/research_job_worker.py" \
    "$INSTALL_ROOT/deploy/w09/research_job_worker.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/research_inbox_control.py" \
    "$INSTALL_ROOT/tools/research_inbox_control.py"
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
install -m 0755 \
    "$PAYLOAD_ROOT/deploy/w09/exploratory_v3_query_canary.py" \
    "$INSTALL_ROOT/tools/exploratory_v3_query_canary.py"
install -m 0755 \
    "$PAYLOAD_ROOT/deploy/w09/exploratory_release_selector.py" \
    "$INSTALL_ROOT/tools/exploratory_release_selector.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/exploratory_autoresearch.py" \
    "$INSTALL_ROOT/tools/exploratory_autoresearch.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/deep03_authority_gate.py" \
    "$INSTALL_ROOT/tools/deep03_authority_gate.py"
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/deep03_one_shot_arm.py" \
    "$INSTALL_ROOT/tools/deep03_one_shot_arm.py"
install -m 0444 "$PAYLOAD_ROOT/deploy/w09/source-commit.txt" \
    "$INSTALL_ROOT/release-commit.txt"
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

install -d -o ubuntu -g ubuntu -m 0750 \
    /srv/w09-research "$CACHE_ROOT" "$INBOX_ROOT" \
    "$INBOX_ROOT/jobs" "$INBOX_ROOT/.incoming" \
    "$INBOX_ROOT/.control" "$INBOX_ROOT/.locks"
install -d -o root -g ubuntu -m 0750 /var/lib/w09-deep03 \
    /var/lib/w09-deep03/one-shot
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
cat > /usr/local/bin/deep03-v3-w1-preflight <<'EOF'
#!/bin/sh
set -eu
exec /opt/w09/venv/bin/python \
  /opt/w09/research/tools/research/deep03_v3_w1_preflight.py "$@"
EOF
chmod 0755 /usr/local/bin/deep03-v3-w1-preflight
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
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-control" \
    /usr/local/libexec/w09-research-inbox-control
install -m 0440 \
    "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-control.sudoers" \
    /etc/sudoers.d/w09-research-inbox-control
/usr/sbin/visudo -cf /etc/sudoers.d/w09-inhibit-run >/dev/null
/usr/sbin/visudo -cf /etc/sudoers.d/w09-research-inbox-control >/dev/null
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/acceptance_on_host.sh" \
    /usr/local/bin/w09-accept
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/w09-idle-check.service" \
    /etc/systemd/system/w09-idle-check.service
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/w09-idle-check.timer" \
    /etc/systemd/system/w09-idle-check.timer
install -m 0644 \
    "$PAYLOAD_ROOT/deploy/w09/w09-exploratory-autoresearch.service" \
    /etc/systemd/system/w09-exploratory-autoresearch.service
install -m 0644 \
    "$PAYLOAD_ROOT/deploy/w09/w09-exploratory-autoresearch.timer" \
    /etc/systemd/system/w09-exploratory-autoresearch.timer
install -m 0644 \
    "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-worker@.service" \
    /etc/systemd/system/w09-research-inbox-worker@.service

sha256sum \
    "$INSTALL_ROOT/deploy/w09/research_job_worker.py" \
    "$INSTALL_ROOT/tools/research_inbox_control.py" \
    "$INSTALL_ROOT/tools/research_reference.py" \
    "$INSTALL_ROOT/tools/research/inbox.py" \
    "$INSTALL_ROOT/tools/research/plan_contract.py" \
    "$INSTALL_ROOT/tools/research/data_catalog.py" \
    "$INSTALL_ROOT/tools/research/data_resolver.py" \
    "$INSTALL_ROOT/tools/research/plugin_api.py" \
    "$INSTALL_ROOT/tools/research/plugins/__init__.py" \
    "$INSTALL_ROOT/tools/research/plugins/deep03.py" \
    /usr/local/libexec/w09-research-inbox-control \
    /etc/systemd/system/w09-research-inbox-worker@.service \
    > /etc/w09/research_inbox.sha256
chmod 0444 /etc/w09/research_inbox.sha256
sha256sum -c /etc/w09/research_inbox.sha256 >/dev/null

sha256sum \
    "$INSTALL_ROOT/tools/v3_query_canary.py" \
    "$INSTALL_ROOT/tools/exploratory_v3_query_canary.py" \
    "$INSTALL_ROOT/tools/exploratory_release_selector.py" \
    "$INSTALL_ROOT/tools/exploratory_autoresearch.py" \
    "$INSTALL_ROOT/tools/deep03_authority_gate.py" \
    "$INSTALL_ROOT/tools/deep03_one_shot_arm.py" \
    "$INSTALL_ROOT/tools/research/deep03_v3_common.py" \
    "$INSTALL_ROOT/tools/research/deep03_v3_w1_preflight.py" \
    "$INSTALL_ROOT/tools/research/deep03_v3_prepare.py" \
    "$INSTALL_ROOT/tools/research/deep03_v3_methods.py" \
    "$INSTALL_ROOT/tools/research/deep03_v3_runner.py" \
    /usr/local/bin/w09-run \
    /usr/local/libexec/w09-inhibit-run \
    > /etc/w09/exploratory_autoresearch.sha256
chmod 0444 /etc/w09/exploratory_autoresearch.sha256
sha256sum -c /etc/w09/exploratory_autoresearch.sha256 >/dev/null

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
systemctl disable --now w09-exploratory-autoresearch.timer \
    >/dev/null 2>&1 || true

# Prove both fail-safe busy modes before arming the timer.
systemctl start w09-idle-check.service
grep -q '"reason": "provisioning"' /var/lib/w09-idle/events.jsonl
rm -f /run/w09-idle-provisioning
systemctl start w09-idle-check.service
grep -Eq '"reason": "ssh-(tcp|session)"' /var/lib/w09-idle/events.jsonl

chown -R ubuntu:ubuntu /srv/w09-research
systemctl enable --now w09-idle-check.timer
# The software bundle is installed, but an ordinary W09 bring-up is not an
# exact-SHA deep03 W/phase execution release.  Keep research disabled until the
# operator applies that separate authority and explicitly enables/starts it.
systemctl disable --now w09-exploratory-autoresearch.timer \
    >/dev/null 2>&1 || true
systemctl is-active --quiet chrony.service
systemctl is-active --quiet w09-idle-check.timer
test "$(systemctl is-enabled w09-exploratory-autoresearch.timer 2>/dev/null || true)" \
    = disabled
timedatectl show -p Timezone --value | grep -qx UTC

trap - ERR
echo "W09_INSTALL_COMPLETE instance=$INSTANCE_ID profile=$PROFILE_NAME role=$ROLE arch=$(uname -m) duckdb=1.4.5 timezone=UTC idle=1800s"
echo "W09_COST compute_usd_per_running_hour=0.4713 effective_running_with_300GB_gp3_and_ipv4=0.50918"
echo "Disconnect every SSH/ControlMaster session; the real idle proof starts on the next timer observation."
