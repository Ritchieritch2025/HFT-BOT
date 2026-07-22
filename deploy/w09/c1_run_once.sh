#!/bin/sh
set -eu

# Manual root control wrapper for one already-installed C1 AUTHORITY/ARM pair.
# It never creates or edits authority and it is not invoked by any timer.

PATH=/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C
HOME=/root
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
export PATH LC_ALL HOME PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT LD_PRELOAD LD_LIBRARY_PATH
unset ENV BASH_ENV CDPATH

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

[ "$(/usr/bin/id -u)" -eq 0 ] || refuse "control wrapper must be invoked as root"
[ "$#" -eq 0 ] || refuse "this exact wrapper accepts no caller-supplied paths or argv"
[ "$(/usr/bin/id -u nobody)" -ge 1 ] || refuse "dedicated unprivileged nobody identity is unavailable"
/usr/bin/getent group nogroup >/dev/null 2>&1 || refuse "dedicated unprivileged nogroup is unavailable"
ubuntu_gid="$(/usr/bin/getent group ubuntu | /usr/bin/cut -d: -f3)" \
    || refuse "checkpoint reader group is unavailable"
[ "$ubuntu_gid" = "1000" ] || refuse "ubuntu checkpoint reader group is not fixed gid 1000"
[ -x "$PYTHON" ] || refuse "pinned W09 Python is unavailable"
[ -f "$GATE" ] && [ ! -L "$GATE" ] || refuse "pinned authority gate is unavailable or linked"
[ -f "$RUNNER" ] && [ ! -L "$RUNNER" ] || refuse "pinned C1 runner is unavailable or linked"
[ -f "$CONFIG" ] && [ ! -L "$CONFIG" ] || refuse "installed C1 config is unavailable or linked"
[ -d "$CHECKPOINT_ROOT" ] && [ ! -L "$CHECKPOINT_ROOT" ] || refuse "checkpoint namespace is unavailable or linked"
[ -d "$WRITE_ROOT" ] && [ ! -L "$WRITE_ROOT" ] || refuse "fixed write root is unavailable or linked"
[ "$(/usr/bin/stat -c %u -- "$WRITE_ROOT")" -eq 0 ] || refuse "fixed write root is not root-owned"
[ "$(/usr/bin/stat -c %a -- "$WRITE_ROOT")" = 755 ] || refuse "fixed write root mode is not exactly 0755"

python_real="$(/usr/bin/readlink -f -- "$PYTHON")" || refuse "pinned Python cannot be resolved"
[ "$python_real" = /usr/bin/python3.12 ] || refuse "pinned Python resolves to an unexpected binary"
[ "$(/usr/bin/sha256sum -- /usr/bin/python3.12 | /usr/bin/cut -d' ' -f1)" = \
    a7d56a8a764faf7bbf5c164055a48fd072be52287bdeb523a9e07b2042f4e7e1 ] \
    || refuse "Python binary SHA-256 differs from audited W09 runtime"
DUCKDB_BINARY=/opt/w09/venv/lib/python3.12/site-packages/_duckdb.cpython-312-aarch64-linux-gnu.so
[ "$(/usr/bin/sha256sum -- "$DUCKDB_BINARY" | /usr/bin/cut -d' ' -f1)" = \
    184620a897f5c1b3dddfa217fe22cd98d614489d8e6bdbfa5fa0b86388af669a ] \
    || refuse "DuckDB extension SHA-256 differs from audited W09 runtime"
"$PYTHON" -I -c \
    'import duckdb,_duckdb,os,sys; assert sys.version.split()[0]=="3.12.3"; assert duckdb.__version__=="1.4.5"; assert os.path.realpath(_duckdb.__file__)=="/opt/w09/venv/lib/python3.12/site-packages/_duckdb.cpython-312-aarch64-linux-gnu.so"' \
    || refuse "Python/DuckDB imported version/path differs from audited W09 runtime"

# Bind execution to the real EC2 identity without retrieving any credentials.
imds_token="$(/usr/bin/curl -fsS --max-time 3 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" \
    || refuse "IMDSv2 token request failed"
identity_document="$(/usr/bin/curl -fsS --max-time 3 \
    -H "X-aws-ec2-metadata-token: $imds_token" \
    http://169.254.169.254/latest/dynamic/instance-identity/document)" \
    || refuse "EC2 identity document request failed"
printf '%s' "$identity_document" | "$PYTHON" -I -c \
    'import json,sys; d=json.load(sys.stdin); assert d.get("instanceId")=="i-0e53d134dceffe166" and d.get("instanceType")=="r8g.2xlarge"' \
    || refuse "real EC2 instance identity differs from fixed W09"
imds_role="$(/usr/bin/curl -fsS --max-time 3 \
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

validation_json="$("$PYTHON" -I "$GATE" validate)" || refuse "authority validation failed"
run_id="$(printf '%s' "$validation_json" | "$PYTHON" -I -c \
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
consumed_json="$("$PYTHON" -I "$GATE" consume)" || refuse "one-shot ARM consumption failed"
consumed_run_id="$(printf '%s' "$consumed_json" | "$PYTHON" -I -c \
    'import json,sys; print(json.load(sys.stdin)["run_id"])')" \
    || refuse "consumption result is malformed"
[ "$consumed_run_id" = "$run_id" ] || refuse "validated and consumed run identities differ"

# The consumed ARM can now receive immutable run-local control receipts.  A
# failure here burns the ARM but cannot start computation or dirty Deep03.
umask 027
/usr/bin/install -d -m 0750 -o root -g root "$run_dir"
/usr/bin/install -d -m 0750 -o root -g root "$control_dir"
recorded_json="$("$PYTHON" -I "$GATE" record)" || refuse "control receipt recording failed"
recorded_run_id="$(printf '%s' "$recorded_json" | "$PYTHON" -I -c \
    'import json,sys; print(json.load(sys.stdin)["run_id"])')" \
    || refuse "control receipt result is malformed"
[ "$recorded_run_id" = "$run_id" ] || refuse "recorded and consumed run identities differ"
execution_deadline_utc="$(printf '%s' "$recorded_json" | "$PYTHON" -I -c \
    'import datetime,json,sys; v=json.load(sys.stdin); s=v["effective_runtime_seconds"]; d=v["execution_deadline_utc"]; p=datetime.datetime.fromisoformat(d.replace("Z","+00:00")); assert type(s) is int and 0 < s <= 14400 and p.utcoffset()==datetime.timedelta(0); print(d)')" \
    || refuse "recorded execution deadline is malformed"
/usr/bin/chmod 0755 "$run_dir"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir/tmp"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir/var-tmp"
/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir/dev-shm"
[ ! -e "$output_dir" ] && [ ! -L "$output_dir" ] \
    || refuse "runner output directory must not exist before runner entry"

# No credentials or network are available to the exact audited runner.  A
# private mount namespace remounts all /srv/w09-research read-only, then exposes
# only this run's work subtree as read-write.  /tmp, /var/tmp, and /dev/shm are
# rebound to directories below work.  Nothing propagates or survives exit.
exec /usr/bin/systemd-inhibit \
    --what=shutdown \
    --mode=block \
    --who=w09-c1-kill-test \
    --why="one-shot C1 offline research" \
    -- /usr/bin/unshare --mount --net -- /bin/sh -eu -c '
        /usr/bin/mount --make-rprivate /
        /usr/bin/mount -t tmpfs -o mode=0700,size=1m tmpfs /mnt
        /usr/bin/mkdir /mnt/c1-work-alias
        /usr/bin/mount --bind "$2" /mnt/c1-work-alias
        /usr/bin/mount --bind /srv/w09-research /srv/w09-research
        /usr/bin/mount -o remount,bind,ro /srv/w09-research
        /usr/bin/mount --bind /mnt/c1-work-alias "$2"
        /usr/bin/mount -o remount,bind,rw,nosuid,nodev "$2"
        /usr/bin/mount --bind "$2/tmp" /tmp
        /usr/bin/mount -o remount,bind,rw,nosuid,nodev,noexec /tmp
        /usr/bin/mount --bind "$2/var-tmp" /var/tmp
        /usr/bin/mount -o remount,bind,rw,nosuid,nodev,noexec /var/tmp
        /usr/bin/mount --bind "$2/dev-shm" /dev/shm
        /usr/bin/mount -o remount,bind,rw,nosuid,nodev,noexec /dev/shm
        remaining_seconds="$("$6" -I -c \
            '"'"'import datetime,sys; d=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); r=int((d-datetime.datetime.now(datetime.timezone.utc)).total_seconds()); assert 0 < r <= 14400; print(r)'"'"' \
            "$5")" || exit 77
        exec /usr/bin/setpriv \
            --reuid=nobody \
            --regid=nogroup \
            --groups=1000 \
            --reset-env \
            -- /usr/bin/env -i \
                HOME="$2" \
                PATH=/usr/bin:/bin \
                LC_ALL=C \
                TMPDIR="$2/tmp" \
                PYTHONDONTWRITEBYTECODE=1 \
                PYTHONNOUSERSITE=1 \
                AWS_EC2_METADATA_DISABLED=true \
                AWS_SHARED_CREDENTIALS_FILE=/dev/null \
                AWS_CONFIG_FILE=/dev/null \
            /usr/bin/timeout \
                --signal=TERM \
                --kill-after=30s \
                "${remaining_seconds}s" \
            "$6" "$7" \
                --checkpoint-root "$1" \
                --output-dir "$3" \
                --config "$4"
    ' c1-private-run "$CHECKPOINT_ROOT" "$work_dir" "$output_dir" "$CONFIG" \
        "$execution_deadline_utc" "$PYTHON" "$RUNNER"
