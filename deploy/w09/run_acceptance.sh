#!/bin/bash
set -Eeuo pipefail

HOST="${W09_HOST:-ubuntu@18.226.151.192}"
KEY="${W09_SSH_KEY:-$HOME/.ssh/kalshi-key.pem}"

if [ "${W09_CONTROL_PLANE_STOP_OBSERVED:-}" != "stopped" ]; then
    echo "W09_IDLE_PROOF_GATE: after observing EC2 state=stopped and restarting, run with W09_CONTROL_PLANE_STOP_OBSERVED=stopped" >&2
    exit 77
fi

exec ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    'sudo /usr/local/sbin/w09-idle-confirm-stop >/srv/w09-research/idle-control-plane-stop.json && /usr/local/bin/w09-accept'
