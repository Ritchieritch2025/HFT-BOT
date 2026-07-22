#!/bin/sh
set -eu

# Manual root control wrapper for one already-installed C1 AUTHORITY/ARM pair.
# It never creates or edits authority and it is not invoked by any timer.

GATE=/opt/w09/research/deploy/w09/c1_authority_gate.py
RUNNER=/opt/w09/research/tools/research/c1_real_fill_runner.py
PYTHON=/opt/w09/venv/bin/python
CHECKPOINT_ROOT=/srv/w09-research/checkpoints/source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979
CONFIG=/etc/w09/c1/C1_CONFIG.json
WRITE_ROOT=/srv/w09-research/c1-runs

refuse() {
    echo "C1_RUN_REFUSED: $*" >&2
    exit 77
}

[ "$(id -u)" -eq 0 ] || refuse "control wrapper must be invoked as root"
[ "$#" -eq 0 ] || refuse "this exact wrapper accepts no caller-supplied paths or argv"
[ "$(id -u nobody)" -ge 1 ] || refuse "dedicated unprivileged nobody identity is unavailable"
getent group nogroup >/dev/null 2>&1 || refuse "dedicated unprivileged nogroup is unavailable"
ubuntu_gid="$(getent group ubuntu | /usr/bin/cut -d: -f3)" \
    || refuse "checkpoint reader group is unavailable"
[ "$ubuntu_gid" = "1000" ] || refuse "ubuntu checkpoint reader group is not fixed gid 1000"
[ -x "$PYTHON" ] || refuse "pinned W09 Python is unavailable"
[ -f "$GATE" ] && [ ! -L "$GATE" ] || refuse "pinned authority gate is unavailable or linked"
[ -f "$RUNNER" ] && [ ! -L "$RUNNER" ] || refuse "pinned C1 runner is unavailable or linked"
[ -f "$CONFIG" ] && [ ! -L "$CONFIG" ] || refuse "installed C1 config is unavailable or linked"
[ -d "$CHECKPOINT_ROOT" ] && [ ! -L "$CHECKPOINT_ROOT" ] || refuse "checkpoint namespace is unavailable or linked"
[ -d "$WRITE_ROOT" ] && [ ! -L "$WRITE_ROOT" ] || refuse "fixed write root is unavailable or linked"
[ "$(stat -c %u "$WRITE_ROOT")" -eq 0 ] || refuse "fixed write root is not root-owned"
[ "$(stat -c %a "$WRITE_ROOT")" = 755 ] || refuse "fixed write root mode is not exactly 0755"

# Bind execution to the real EC2 identity without retrieving any credentials.
imds_token="$(curl -fsS --max-time 3 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" \
    || refuse "IMDSv2 token request failed"
identity_document="$(curl -fsS --max-time 3 \
    -H "X-aws-ec2-metadata-token: $imds_token" \
    http://169.254.169.254/latest/dynamic/instance-identity/document)" \
    || refuse "EC2 identity document request failed"
printf '%s' "$identity_document" | "$PYTHON" -c \
    'import json,sys; d=json.load(sys.stdin); assert d.get("instanceId")=="i-0e53d134dceffe166" and d.get("instanceType")=="r8g.2xlarge"' \
    || refuse "real EC2 instance identity differs from fixed W09"
imds_role="$(curl -fsS --max-time 3 \
    -H "X-aws-ec2-metadata-token: $imds_token" \
    http://169.254.169.254/latest/meta-data/iam/security-credentials/)" \
    || refuse "instance-profile role identity request failed"
[ "$imds_role" = "w09-research-runner" ] || refuse "real instance-profile role differs from fixed W09 role"

for path in \
    "$CHECKPOINT_ROOT" \
    "$CHECKPOINT_ROOT/l2_availability" \
    "$CHECKPOINT_ROOT/l2_replay" \
    "$CHECKPOINT_ROOT/l2_episodes" \
    "$CHECKPOINT_ROOT/trades_market" \
    "$CHECKPOINT_ROOT/dim_market_date"
do
    [ -d "$path" ] && [ ! -L "$path" ] || refuse "checkpoint stage is missing or linked: $path"
    if /usr/bin/setpriv --reuid=nobody --regid=nogroup --groups=1000 \
        /usr/bin/test -w "$path"
    then
        refuse "checkpoint input is writable by the nobody runner: $path"
    fi
    /usr/bin/setpriv --reuid=nobody --regid=nogroup --groups=1000 \
        /usr/bin/test -r "$path" \
        || refuse "checkpoint input is unreadable by nobody plus gid 1000: $path"
done

if /usr/bin/find "$CHECKPOINT_ROOT" -xdev -perm /0022 -print -quit | /usr/bin/grep -q .
then
    refuse "checkpoint tree contains a group/world-writable input"
fi
if /usr/bin/setpriv --reuid=nobody --regid=nogroup --groups=1000 \
    /usr/bin/find "$CHECKPOINT_ROOT" -xdev ! -readable -print -quit \
    | /usr/bin/grep -q .
then
    refuse "checkpoint tree contains an input unreadable by the C1 identity"
fi

validation_json="$($PYTHON "$GATE" validate)" || refuse "authority validation failed"
run_id="$(printf '%s' "$validation_json" | "$PYTHON" -c \
    'import json,sys; v=json.load(sys.stdin); r=v["run_id"]; assert isinstance(r,str) and r.startswith("c1-"); print(r)')" \
    || refuse "validated run identity is malformed"
run_dir="$WRITE_ROOT/$run_id"
control_dir="$run_dir/control"
work_dir="$run_dir/work"
output_dir="$work_dir/results"
case "$run_dir" in
    /srv/w09-research/c1-runs/c1-*) ;;
    *) refuse "run directory escaped the fixed c1-* namespace" ;;
esac
[ ! -e "$run_dir" ] && [ ! -L "$run_dir" ] || refuse "run directory already exists"

# O_EXCL consumption happens before any run/output directory is created.
consumed_json="$($PYTHON "$GATE" consume)" || refuse "one-shot ARM consumption failed"
consumed_run_id="$(printf '%s' "$consumed_json" | "$PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["run_id"])')" \
    || refuse "consumption result is malformed"
runtime_seconds="$(printf '%s' "$consumed_json" | "$PYTHON" -c \
    'import json,sys; v=json.load(sys.stdin)["effective_runtime_seconds"]; assert type(v) is int and 0 < v <= 14400; print(v)')" \
    || refuse "consumption runtime is malformed"
[ "$consumed_run_id" = "$run_id" ] || refuse "validated and consumed run identities differ"

# The consumed ARM can now receive immutable run-local control receipts.  A
# failure here burns the ARM but cannot start computation or dirty Deep03.
umask 027
/usr/bin/install -d -m 0750 -o root -g root "$run_dir"
/usr/bin/install -d -m 0750 -o root -g root "$control_dir"
recorded_json="$($PYTHON "$GATE" record)" || refuse "control receipt recording failed"
recorded_run_id="$(printf '%s' "$recorded_json" | "$PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["run_id"])')" \
    || refuse "control receipt result is malformed"
[ "$recorded_run_id" = "$run_id" ] || refuse "recorded and consumed run identities differ"
/usr/bin/chmod 0755 "$run_dir"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir/tmp"
[ ! -e "$output_dir" ] && [ ! -L "$output_dir" ] \
    || refuse "runner output directory must not exist before runner entry"

# No credentials or network are available to the exact audited runner.  A
# private mount namespace remounts the checkpoint tree read-only for the life
# of this process only; no mount propagates or survives.  The process is then
# irreversibly dropped to nobody:nogroup with only supplementary gid 1000.
exec /usr/bin/systemd-inhibit \
    --what=shutdown \
    --mode=block \
    --who=w09-c1-kill-test \
    --why="one-shot C1 offline research" \
    -- /usr/bin/unshare --mount --net -- /bin/sh -eu -c '
        /usr/bin/mount --make-rprivate /
        /usr/bin/mount --bind "$1" "$1"
        /usr/bin/mount -o remount,bind,ro "$1"
        exec /usr/bin/setpriv \
            --reuid=nobody \
            --regid=nogroup \
            --groups=1000 \
            --reset-env \
            -- /usr/bin/env \
                HOME="$2" \
                TMPDIR="$2/tmp" \
                PYTHONDONTWRITEBYTECODE=1 \
                PYTHONNOUSERSITE=1 \
                AWS_EC2_METADATA_DISABLED=true \
                AWS_SHARED_CREDENTIALS_FILE=/dev/null \
                AWS_CONFIG_FILE=/dev/null \
            /usr/bin/timeout \
                --signal=TERM \
                --kill-after=30s \
                "${5}s" \
            "$6" "$7" \
                --checkpoint-root "$1" \
                --output-dir "$3" \
                --config "$4"
    ' c1-private-run "$CHECKPOINT_ROOT" "$work_dir" "$output_dir" "$CONFIG" \
        "$runtime_seconds" "$PYTHON" "$RUNNER"
