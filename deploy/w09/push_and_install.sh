#!/bin/bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SOURCE_REPO="${W09_SOURCE_REPO:-/Users/ritcardo/HFT-BOT-pipeline-recovery}"
HOST="${W09_HOST:-ubuntu@18.226.151.192}"
KEY="${W09_SSH_KEY:-$HOME/.ssh/kalshi-key.pem}"
REMOTE="/tmp/w09-bringup"
READER_MODULE_MANIFEST="$HERE/research_reader_modules.sha256"
DEEP03_MODULE_MANIFEST="$HERE/deep03_open_discovery_modules.sha256"

if [ "${W09_SHUTDOWN_BEHAVIOR_CONFIRMED:-}" != "stop" ]; then
    echo "W09_SHUTDOWN_GATE: first confirm InstanceInitiatedShutdownBehavior=stop, then run with W09_SHUTDOWN_BEHAVIOR_CONFIRMED=stop" >&2
    exit 77
fi
if [ ! -f "$READER_MODULE_MANIFEST" ]; then
    echo "W09_SOURCE_GATE: reader module manifest missing" >&2
    exit 65
fi
if ! (cd "$SOURCE_REPO" && \
      shasum -a 256 -c "$READER_MODULE_MANIFEST" >/dev/null); then
    echo "W09_SOURCE_GATE: pinned reader module set changed" >&2
    exit 65
fi
if [ ! -f "$DEEP03_MODULE_MANIFEST" ]; then
    echo "W09_SOURCE_GATE: Deep03 module manifest missing" >&2
    exit 65
fi
if ! (cd "$SOURCE_REPO" && \
      shasum -a 256 -c "$DEEP03_MODULE_MANIFEST" >/dev/null); then
    echo "W09_SOURCE_GATE: pinned Deep03 module set changed" >&2
    exit 65
fi
if [ ! -f "$KEY" ]; then
    echo "W09_SSH_GATE: key missing: $KEY" >&2
    exit 66
fi

tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT
mkdir -p "$tmp/tools/research" "$tmp/config" "$tmp/deploy/w09"
cp "$SOURCE_REPO/tools/research_data.py" "$tmp/tools/"
cp "$SOURCE_REPO/tools/research_reference.py" "$tmp/tools/"
cp "$SOURCE_REPO/tools/warehouse_common.py" "$tmp/tools/"
for module in deep03_v3_common.py deep03_v3_prepare.py \
              deep03_v3_methods.py deep03_v3_runner.py; do
    cp "$SOURCE_REPO/tools/research/$module" "$tmp/tools/research/"
done
cp "$SOURCE_REPO/config/warehouse.yaml" "$tmp/config/"
cp "$READER_MODULE_MANIFEST" "$tmp/deploy/w09/"
cp "$DEEP03_MODULE_MANIFEST" "$tmp/deploy/w09/"
for file in \
    acceptance_on_host.sh amazon-time-sync.sources cost-contract.json \
    install_on_host.sh README.md research_data_instance_profile.py \
    run_acceptance.sh select_newest_release.py w09-idle-check.service \
    w09-idle-check.timer w09-run w09-inhibit-run \
    w09-inhibit-run.sudoers w09_idle_check.py \
    w09_idle_confirm_stop.py w09_idle_proof.py; do
    cp "$HERE/$file" "$tmp/deploy/w09/$file"
done
printf '%s\n' 'instance=i-0e53d134dceffe166 behavior=stop operator-confirmed' \
    > "$tmp/shutdown-behavior-stop.confirmed"

tar -C "$tmp" -czf - . | ssh -i "$KEY" -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new "$HOST" \
    "rm -rf '$REMOTE' && mkdir -p '$REMOTE' && tar -xzf - -C '$REMOTE' && sudo bash '$REMOTE/deploy/w09/install_on_host.sh' '$REMOTE'"
