#!/usr/bin/env bash
# Install the isolated v3 daily units.  This script never accepts or prints a
# plaintext AWS key.  Publisher-only durable automation can be installed while
# the independent tagger credential remains absent; full publication stays
# inert and fail-closed until both encrypted payloads are present.
set -euo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PUB_CRED=/etc/credstore.encrypted/kalshi-research-v3-publisher.env
TAG_CRED=/etc/credstore.encrypted/kalshi-research-v3-tagger.credentials
CRED_DIR=/etc/credstore.encrypted
MAX_CRED_BYTES=1048576
SERVICE_USER=kalshi-research-v3
SERVICE_GROUP=kalshi-research-v3
LOCK_GROUP=kalshi-publication
RUNTIME_LINK=/opt/kalshi-research-v3
RUNTIME_RELEASES=/opt/kalshi-research-v3-releases
HISTORY_LINK=/opt/kalshi-research-v3-historical-canonical-receipts
HISTORY_RELEASES=/opt/kalshi-research-v3-historical-receipt-releases
HISTORY_SOURCE=/home/ubuntu/hft-bot/work/live/canonical_receipts
APPROVAL_DIR=/etc/kalshi-research-v3/approvals
GIT_CONFIG=/etc/kalshi-research-v3/gitconfig
AWS_CLI=/snap/aws-cli/current/bin/aws
GIT_BIN=/usr/bin/git
SYSTEM_PYTHON=/usr/bin/python3
DUCKDB_WHEEL_REL=vendor/python/duckdb-1.4.5-cp312-cp312-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl
DUCKDB_WHEEL_SHA256=0c72b1dcf27a71ef5f3dc14b92b9ed9274c5584bb0e88590b78907cbb8e254f3
AUTHORIZATION_REL=docs/plan_releases/pipeline/W-PUB-REF-01C_AUTOMATION_EXECUTION_AUTHORIZATION_2026-07-17.json
AUTHORIZATION_SHA256=1b14001428f2387f3e62c531a8d8ce3dd4f8bd726d6c8b94b4d6c0d893020761
AUTOMATION_FIRST_DATE=2026-07-10
STAGE=""
TAG_CRED_READY=0

git_clean_env() {
  env -i \
    HOME=/nonexistent PATH="$PATH" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 \
    "$GIT_BIN" \
    -c "safe.directory=$1" \
    -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null \
    -c core.untrackedCache=false "${@:2}"
}

source_git() {
  git_clean_env "$ROOT" -C "$ROOT" "$@"
}

stage_git() {
  git_clean_env "$STAGE/runtime" -C "$STAGE/runtime" "$@"
}

MUTABLE_ROOTS=(
  /home/ubuntu/hft-bot/work/live/canonical_receipts/controls
  /home/ubuntu/hft-bot/work/live/canonical_receipts/durable
  /home/ubuntu/hft-bot/work/live/canonical_receipts/tagged
  /home/ubuntu/hft-bot/work/live/canonical_receipts/tag-precommit
  /home/ubuntu/hft-bot/work/live/canonical_receipts/rfq-eligibility
  /home/ubuntu/hft-bot/work/live/canonical_receipts/forward-aux
  /home/ubuntu/hft-bot/work/live/canonical_receipts/forward-metadata-preflight
  /home/ubuntu/hft-bot/work/live/canonical_receipts/forward-version-bindings
  /home/ubuntu/hft-bot/work/live/canonical_receipts/generation-daily
  /home/ubuntu/hft-bot/work/live/canonical_receipts/generation-migration-proofs
  /home/ubuntu/hft-bot/work/live/canonical_receipts/generation-witness-intents
  /home/ubuntu/hft-bot/work/live/research_v3_audit
  /home/ubuntu/hft-bot/work/live/research_v3_daily
  /home/ubuntu/hft-bot/work/research_stage
)
LOCK_ROOT=/home/ubuntu/hft-bot/work/warehouse/.publication-locks
GENERATION_WRITER_LOCK=/var/lib/kalshi-research-v3-locks/generation-witness.lock

cleanup() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
  echo "REFUSED: run as root" >&2
  exit 2
fi
if [ ! -d "$CRED_DIR" ] || [ -L "$CRED_DIR" ] || \
   [ "$(stat -c %u "$CRED_DIR")" -ne 0 ] || \
   [ "$(stat -c %g "$CRED_DIR")" -ne 0 ] || \
   [ "$(stat -c %a "$CRED_DIR")" != 700 ]; then
  echo "REFUSED: encrypted credential directory must be root:root 0700" >&2
  exit 2
fi
validate_encrypted_credential() {
  path=$1
  if [ ! -f "$path" ] || [ -L "$path" ] || \
     [ "$(stat -c %u "$path")" -ne 0 ] || \
     [ "$(stat -c %g "$path")" -ne 0 ] || \
     [ "$(stat -c %a "$path")" != 600 ] || \
     [ "$(stat -c %s "$path")" -le 0 ] || \
     [ "$(stat -c %s "$path")" -gt "$MAX_CRED_BYTES" ]; then
    echo "REFUSED: encrypted systemd credential is missing or unsafe: $path" >&2
    exit 2
  fi
}
validate_encrypted_credential "$PUB_CRED"
if [ -e "$TAG_CRED" ] || [ -L "$TAG_CRED" ]; then
  validate_encrypted_credential "$TAG_CRED"
  TAG_CRED_READY=1
fi

for command in env find findmnt flock getent groupadd install readlink \
  runuser setfacl setpriv sha256sum stat systemctl systemd-tmpfiles \
  uname useradd usermod; do
  if ! command -v "$command" >/dev/null; then
    echo "REFUSED: required command is missing: $command" >&2
    exit 2
  fi
done
if [ ! -x "$GIT_BIN" ]; then
  echo "REFUSED: fixed Git binary is missing: $GIT_BIN" >&2
  exit 2
fi
SOURCE_TOPLEVEL="$(source_git rev-parse --show-toplevel)"
if [ "$(readlink -f -- "$SOURCE_TOPLEVEL")" != "$ROOT" ]; then
  echo "REFUSED: source Git toplevel does not match installer root" >&2
  exit 2
fi
if [ -n "$(source_git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "REFUSED: runtime source must be a completely clean reviewed commit" >&2
  exit 2
fi
COMMIT="$(source_git rev-parse --verify HEAD^{commit})"
case "$COMMIT" in
  *[!0-9a-f]*|'') echo "REFUSED: invalid source commit" >&2; exit 2 ;;
esac
if [ "${#COMMIT}" -ne 40 ]; then
  echo "REFUSED: source commit is not a full SHA-1" >&2
  exit 2
fi
if [ ! -x "$SYSTEM_PYTHON" ]; then
  echo "REFUSED: fixed system Python is missing" >&2
  exit 2
fi
if [ "$(uname -m)" != aarch64 ]; then
  echo "REFUSED: production publisher runtime requires aarch64" >&2
  exit 2
fi
SYSTEM_PYTHON_REAL="$(readlink -f -- "$SYSTEM_PYTHON")"
case "$SYSTEM_PYTHON_REAL" in
  /usr/bin/python3.*) ;;
  *) echo "REFUSED: fixed system Python target is unexpected" >&2; exit 2 ;;
esac
if [ ! -f "$SYSTEM_PYTHON_REAL" ] || [ -L "$SYSTEM_PYTHON_REAL" ] || \
   [ "$(stat -c %u "$SYSTEM_PYTHON_REAL")" -ne 0 ] || \
   [ "$(stat -c %g "$SYSTEM_PYTHON_REAL")" -ne 0 ] || \
   [ $((8#$(stat -c %a "$SYSTEM_PYTHON_REAL") & 8#022)) -ne 0 ]; then
  echo "REFUSED: system Python target must be root-owned and non-writable" >&2
  exit 2
fi
AUTHORIZATION="$ROOT/$AUTHORIZATION_REL"
if [ ! -f "$AUTHORIZATION" ] || [ -L "$AUTHORIZATION" ] || \
   ! source_git ls-files --error-unmatch -- "$AUTHORIZATION_REL" \
     >/dev/null 2>&1; then
  echo "REFUSED: operator authorization must be one tracked regular file" >&2
  exit 2
fi
read -r authorization_sha _ < <(sha256sum -- "$AUTHORIZATION")
if [ "$authorization_sha" != "$AUTHORIZATION_SHA256" ]; then
  echo "REFUSED: operator authorization hash mismatch" >&2
  exit 2
fi
if [ ! -x "$AWS_CLI" ]; then
  echo "REFUSED: direct non-confined AWS CLI is missing: $AWS_CLI" >&2
  exit 2
fi
AWS_CLI_REAL="$(readlink -f -- "$AWS_CLI")"
AWS_CLI_REVISION="${AWS_CLI_REAL#/snap/aws-cli/}"
AWS_CLI_REVISION="${AWS_CLI_REVISION%%/*}"
case "$AWS_CLI_REVISION" in
  ''|*[!0-9]*)
    echo "REFUSED: AWS CLI target is not a numeric snap revision" >&2
    exit 2
    ;;
esac
if [ "$AWS_CLI_REAL" != "/snap/aws-cli/$AWS_CLI_REVISION/aws/dist/aws" ] || \
   [ ! -f "$AWS_CLI_REAL" ] || [ -L "$AWS_CLI_REAL" ] || \
   [ "$(stat -c %u "$AWS_CLI_REAL")" -ne 0 ] || \
   [ $((8#$(stat -c %a "$AWS_CLI_REAL") & 8#022)) -ne 0 ]; then
  echo "REFUSED: AWS CLI target must be the root-owned immutable snap binary" >&2
  exit 2
fi
AWS_CLI_MOUNT_OPTIONS="$(findmnt -n -T "$AWS_CLI_REAL" -o OPTIONS)"
case ",$AWS_CLI_MOUNT_OPTIONS," in
  *,ro,*) ;;
  *) echo "REFUSED: AWS CLI target is not on a read-only mount" >&2; exit 2 ;;
esac
if [ -L "$HISTORY_SOURCE" ] || [ ! -d "$HISTORY_SOURCE/durable" ] || \
   [ -L "$HISTORY_SOURCE/durable" ]; then
  echo "REFUSED: historical canonical durable root is missing or unsafe: $HISTORY_SOURCE" >&2
  exit 2
fi

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! getent group "$LOCK_GROUP" >/dev/null; then
  groupadd --system "$LOCK_GROUP"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  useradd --system --gid "$SERVICE_GROUP" \
    --home-dir /var/lib/kalshi-research-v3 --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if [ "$(id -gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]; then
  echo "REFUSED: $SERVICE_USER has an unexpected primary group" >&2
  exit 2
fi
usermod -a -G "$LOCK_GROUP" "$SERVICE_USER"
usermod -a -G "$LOCK_GROUP" ubuntu
if ! runuser -u "$SERVICE_USER" -- env -i \
    HOME=/var/lib/kalshi-research-v3 PATH=/snap/aws-cli/current/bin:/usr/bin:/bin \
    setpriv --no-new-privs "$AWS_CLI" --version >/dev/null 2>&1; then
  echo "REFUSED: direct AWS CLI cannot execute as the sandboxed service user" >&2
  exit 2
fi

STAGE="$(mktemp -d /opt/.kalshi-research-v3-install.XXXXXX)"
git_clean_env "$ROOT" clone --quiet --no-checkout --no-hardlinks --local -- \
  "$ROOT" "$STAGE/runtime"
stage_git checkout --quiet --detach "$COMMIT"
if [ -n "$(stage_git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "REFUSED: staged runtime is not clean" >&2
  exit 2
fi
if ! stage_git ls-files --error-unmatch -- "$DUCKDB_WHEEL_REL" \
    >/dev/null 2>&1; then
  echo "REFUSED: pinned DuckDB wheel is not tracked in the reviewed commit" >&2
  exit 2
fi
DUCKDB_WHEEL="$STAGE/runtime/$DUCKDB_WHEEL_REL"
if [ ! -f "$DUCKDB_WHEEL" ] || [ -L "$DUCKDB_WHEEL" ] || \
   [ "$(stat -c %u "$DUCKDB_WHEEL")" -ne 0 ] || \
   [ "$(stat -c %g "$DUCKDB_WHEEL")" -ne 0 ] || \
   [ $((8#$(stat -c %a "$DUCKDB_WHEEL") & 8#022)) -ne 0 ] || \
   [ "$(stat -c %s "$DUCKDB_WHEEL")" -le 0 ] || \
   [ "$(stat -c %s "$DUCKDB_WHEEL")" -gt 67108864 ]; then
  echo "REFUSED: pinned DuckDB wheel ownership/mode/size is unsafe" >&2
  exit 2
fi
read -r duckdb_wheel_sha _ < <(sha256sum -- "$DUCKDB_WHEEL")
if [ "$duckdb_wheel_sha" != "$DUCKDB_WHEEL_SHA256" ]; then
  echo "REFUSED: pinned DuckDB wheel hash mismatch" >&2
  exit 2
fi
# Never copy or execute the ubuntu-owned producer virtualenv.  Build a minimal
# publisher runtime from the root-owned OS interpreter plus one exact reviewed
# binary wheel.  No network or ambient pip configuration participates.
"$SYSTEM_PYTHON" -I -m venv --without-pip "$STAGE/runtime/.venv"
env -i HOME=/nonexistent PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_REQUIRE_VIRTUALENV=1 \
  "$STAGE/runtime/.venv/bin/python" -I -m ensurepip --default-pip \
  >/dev/null
env -i HOME=/nonexistent PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_REQUIRE_VIRTUALENV=1 \
  "$STAGE/runtime/.venv/bin/python" -I -m pip install \
  --no-index --no-deps --no-cache-dir --disable-pip-version-check \
  "$DUCKDB_WHEEL" >/dev/null
"$STAGE/runtime/.venv/bin/python" -I -c \
  'import duckdb,sys; assert sys.prefix != sys.base_prefix; assert sys.version_info[:2] == (3,12); assert duckdb.__version__ == "1.4.5"; c=duckdb.connect(":memory:"); c.execute("SET memory_limit=\x2764MB\x27"); assert c.execute("SELECT 1").fetchone() == (1,)'
# Reject checkout/venv links that escape the immutable runtime except for the
# venv's fixed root-owned OS interpreter target.
while IFS= read -r -d '' link; do
  resolved="$(readlink -f -- "$link")"
  case "$resolved" in
    "$STAGE/runtime"/*|"$SYSTEM_PYTHON_REAL") ;;
    *) echo "REFUSED: runtime symlink escapes reviewed roots: $link" >&2; exit 2 ;;
  esac
done < <(find -P "$STAGE/runtime" -type l -print0)
mkdir -p "$STAGE/history/durable"
declare -A history_dates=()
history_count=0
while IFS= read -r -d '' index_path; do
  index_name="$(basename -- "$index_path")"
  date_name="$(basename -- "$(dirname -- "$index_path")")"
  if [[ ! "$date_name" =~ ^date=([0-9]{4}-[0-9]{2}-[0-9]{2})$ ]]; then
    echo "REFUSED: historical durable index has invalid date directory: $index_path" >&2
    exit 2
  fi
  date="${BASH_REMATCH[1]}"
  if [[ "$date" < "$AUTOMATION_FIRST_DATE" ]]; then
    echo "REFUSED: historical durable date is outside automation authorization: $date" >&2
    exit 2
  fi
  digest="${index_name#DURABLE-}"
  digest="${digest%.json}"
  if [[ ! "$index_name" =~ ^DURABLE-[0-9a-f]{64}\.json$ ]] || \
     [ -n "${history_dates[$date]:-}" ]; then
    echo "REFUSED: historical durable authority is ambiguous or malformed for $date" >&2
    exit 2
  fi
  receipt_path="$(dirname -- "$index_path")/receipt-$digest.json"
  if [ ! -f "$receipt_path" ] || [ -L "$index_path" ] || [ -L "$receipt_path" ]; then
    echo "REFUSED: historical durable receipt pair is missing or unsafe for $date" >&2
    exit 2
  fi
  destination="$STAGE/history/durable/$date_name"
  install -d -m 0755 "$destination"
  install -m 0444 "$index_path" "$destination/$index_name"
  install -m 0444 "$receipt_path" "$destination/receipt-$digest.json"
  PYTHONPATH="$STAGE/runtime/tools" "$STAGE/runtime/.venv/bin/python" -c \
    'import pathlib,sys; import research_v3_daily as d; d._validate_durable_index(pathlib.Path(sys.argv[1]), sys.argv[2])' \
    "$destination/$index_name" "$date"
  history_dates[$date]=1
  history_count=$((history_count + 1))
done < <(find -P "$HISTORY_SOURCE/durable" -mindepth 2 -maxdepth 2 \
  -type f -name 'DURABLE-*.json' -print0)
if [ "$history_count" -eq 0 ]; then
  echo "REFUSED: historical source has no final durable authority pairs" >&2
  exit 2
fi

# Freeze executable bits as supplied while removing every non-root write bit.
chown -R root:root "$STAGE/runtime" "$STAGE/history"
chmod -R a-w "$STAGE/runtime" "$STAGE/history"
install -d -o root -g root -m 0755 "$RUNTIME_RELEASES" "$HISTORY_RELEASES"
if [ -e "$RUNTIME_RELEASES/$COMMIT" ] || \
   [ -L "$RUNTIME_RELEASES/$COMMIT" ] || \
   [ -e "$HISTORY_RELEASES/$COMMIT" ] || \
   [ -L "$HISTORY_RELEASES/$COMMIT" ]; then
  echo "REFUSED: immutable release for this commit already exists" >&2
  exit 2
fi
mv "$STAGE/runtime" "$RUNTIME_RELEASES/$COMMIT"
mv "$STAGE/history" "$HISTORY_RELEASES/$COMMIT"
IMMUTABLE_RUNTIME="$RUNTIME_RELEASES/$COMMIT"
IMMUTABLE_AUTHORIZATION="$IMMUTABLE_RUNTIME/$AUTHORIZATION_REL"
read -r immutable_authorization_sha _ < <(
  sha256sum -- "$IMMUTABLE_AUTHORIZATION")
if [ "$immutable_authorization_sha" != "$AUTHORIZATION_SHA256" ]; then
  echo "REFUSED: immutable authorization hash mismatch" >&2
  exit 2
fi

# Quiesce every old consumer before either immutable release symlink moves.
# This prevents an already-running coordinator from spawning later children
# out of a different commit during an upgrade.  Capture/sync units are not in
# this list and remain untouched.
for unit in kalshi-research-v3-daily.timer \
            kalshi-research-v3-durable.timer \
            kalshi-canonical-generation-witness.path \
            kalshi-canonical-generation-witness.timer; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$unit"; then
    echo "REFUSED: old research trigger remained active before cutover: $unit" >&2
    exit 2
  fi
done
for unit in kalshi-research-v3-daily.service \
            kalshi-research-v3-durable.service \
            kalshi-canonical-generation-witness.service \
            kalshi-canonical-generation-legacy.service; do
  systemctl stop "$unit" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$unit"; then
    echo "REFUSED: old research unit remained active before cutover: $unit" >&2
    exit 2
  fi
done
ln -sfn "$RUNTIME_RELEASES/$COMMIT" "$RUNTIME_LINK.new"
mv -Tf "$RUNTIME_LINK.new" "$RUNTIME_LINK"
ln -sfn "$HISTORY_RELEASES/$COMMIT" "$HISTORY_LINK.new"
mv -Tf "$HISTORY_LINK.new" "$HISTORY_LINK"

install -d -o root -g root -m 0755 "$APPROVAL_DIR"
GIT_CONFIG_STAGE="$STAGE/gitconfig"
printf '%s\n' \
  '[safe]' \
  "    directory = $RUNTIME_RELEASES/$COMMIT" \
  '[core]' \
  '    fsmonitor = false' \
  '    hooksPath = /dev/null' \
  '    untrackedCache = false' >"$GIT_CONFIG_STAGE"
install -o root -g root -m 0444 "$GIT_CONFIG_STAGE" "$GIT_CONFIG"
for target_name in cutover-approved publish-approved; do
  install -o root -g root -m 0444 "$IMMUTABLE_AUTHORIZATION" \
    "$APPROVAL_DIR/$target_name"
  read -r marker_sha _ < <(sha256sum -- "$APPROVAL_DIR/$target_name")
  if [ "$marker_sha" != "$AUTHORIZATION_SHA256" ]; then
    echo "REFUSED: installed approval marker hash mismatch: $target_name" >&2
    exit 2
  fi
done

install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-research-v3-daily.tmpfiles.conf" \
  /etc/tmpfiles.d/kalshi-research-v3-daily.conf
systemd-tmpfiles --create /etc/tmpfiles.d/kalshi-research-v3-daily.conf

# The service can read sealed inputs but cannot write any producer directory.
# Output roots are owned separately by tmpfiles and systemd's path sandbox.
setfacl -m "u:$SERVICE_USER:--x" /home/ubuntu /home/ubuntu/hft-bot \
  /home/ubuntu/hft-bot/work
for root in \
  /home/ubuntu/hft-bot/work/raw \
  /home/ubuntu/hft-bot/work/warehouse \
  /home/ubuntu/hft-bot/work/event_packs \
  /home/ubuntu/hft-bot/work/live; do
  setfacl -R -m "u:$SERVICE_USER:r-X" "$root"
  find "$root" -type d -exec setfacl -m "d:u:$SERVICE_USER:rx" {} +
done
# Re-open only the explicit output roots after the broad read-only input ACL.
# This also repairs existing ubuntu-owned date directories left by manual
# backfills; named user ACLs otherwise override the publication group.
for root in "${MUTABLE_ROOTS[@]}" "$LOCK_ROOT"; do
  setfacl -R -m "u:$SERVICE_USER:rwX" "$root"
  find "$root" -type d -exec setfacl -m "d:u:$SERVICE_USER:rwx" {} +
  blocked="$(runuser -u "$SERVICE_USER" -- \
    find "$root" -type d ! -writable -print -quit)"
  if [ -n "$blocked" ]; then
    echo "REFUSED: service output directory remains unwritable: $blocked" >&2
    exit 2
  fi
done
# Keep the shared writer lock outside every ubuntu-owned capture tree.  Its
# direct parent is root-owned and not writable by either ubuntu or the service,
# so the checks below cannot race a user-controlled path replacement.  Do not
# mutate the path after checking it; tmpfiles established the exact shape.
GENERATION_WRITER_LOCK_PARENT=${GENERATION_WRITER_LOCK%/*}
if [ ! -d "$GENERATION_WRITER_LOCK_PARENT" ] || \
   [ -L "$GENERATION_WRITER_LOCK_PARENT" ] || \
   [ "$(readlink -f -- "$GENERATION_WRITER_LOCK_PARENT")" != \
       "$GENERATION_WRITER_LOCK_PARENT" ] || \
   [ "$(stat -c %u "$GENERATION_WRITER_LOCK_PARENT")" -ne 0 ] || \
   [ "$(stat -c %g "$GENERATION_WRITER_LOCK_PARENT")" -ne \
       "$(id -g "$SERVICE_USER")" ] || \
   [ "$(stat -c %a "$GENERATION_WRITER_LOCK_PARENT")" != 750 ]; then
  echo "REFUSED: generation writer lock parent is not exact" >&2
  exit 2
fi
if [ ! -f "$GENERATION_WRITER_LOCK" ] || [ -L "$GENERATION_WRITER_LOCK" ] || \
   [ "$(readlink -f -- "$GENERATION_WRITER_LOCK")" != \
       "$GENERATION_WRITER_LOCK" ] || \
   [ "$(stat -c %h "$GENERATION_WRITER_LOCK")" -ne 1 ] || \
   [ "$(stat -c %u "$GENERATION_WRITER_LOCK")" -ne \
       "$(id -u "$SERVICE_USER")" ] || \
   [ "$(stat -c %g "$GENERATION_WRITER_LOCK")" -ne \
       "$(id -g "$SERVICE_USER")" ] || \
   [ "$(stat -c %a "$GENERATION_WRITER_LOCK")" != 600 ]; then
  echo "REFUSED: generation writer lock ownership/mode is not exact" >&2
  exit 2
fi
runuser -u "$SERVICE_USER" -- test -w "$GENERATION_WRITER_LOCK"
runuser -u "$SERVICE_USER" -- test -r \
  "$RUNTIME_LINK/tools/research_v3_daily.py"
runuser -u "$SERVICE_USER" -- test -r \
  "$RUNTIME_LINK/tools/canonical_generation_daily.py"
runuser -u "$SERVICE_USER" -- test -r \
  "$RUNTIME_LINK/tools/canonical_generation_witness.py"
runuser -u "$SERVICE_USER" -- env -i \
  HOME=/nonexistent PATH=/usr/bin:/bin \
  "$RUNTIME_LINK/.venv/bin/python" -I -c \
  'import duckdb; assert duckdb.__version__ == "1.4.5"; c=duckdb.connect(":memory:"); c.execute("SET memory_limit=\x2764MB\x27"); assert c.execute("SELECT 1").fetchone() == (1,)'
runuser -u "$SERVICE_USER" -- test -r "$HISTORY_LINK"
for lock_name in catalog dim; do
  lock_path="$LOCK_ROOT/$lock_name.lock"
  runuser -u "$SERVICE_USER" -- test -w "$lock_path"
  runuser -u ubuntu -- test -w "$lock_path"
  runuser -u "$SERVICE_USER" -- flock -n "$lock_path" true
  runuser -u ubuntu -- flock -n "$lock_path" true
done

# End-to-end read-only provenance smoke: the repository is root-owned and has
# no write bit, while the exact production caller must still prove a clean HEAD
# without creating .git/index.lock or trusting ambient Git configuration.
resolved_commit="$(runuser -u "$SERVICE_USER" -- env -i \
  HOME=/var/lib/kalshi-research-v3 PATH=/usr/bin:/bin \
  GIT_CONFIG_SYSTEM="$GIT_CONFIG" GIT_CONFIG_GLOBAL=/dev/null \
  GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 \
  /usr/bin/git -C "$RUNTIME_LINK" rev-parse --verify HEAD^{commit})"
if [ "$resolved_commit" != "$COMMIT" ]; then
  echo "REFUSED: root-owned runtime Git configuration did not resolve exact HEAD" >&2
  exit 2
fi
runuser -u "$SERVICE_USER" -- env -i \
  HOME=/var/lib/kalshi-research-v3 PATH=/usr/bin:/bin \
  PYTHONPATH="$RUNTIME_LINK/tools" \
  "$RUNTIME_LINK/.venv/bin/python" -c \
  'import git_provenance as g, pathlib; root=pathlib.Path("/opt/kalshi-research-v3"); head=g.require_clean_head(root, ("tools/git_provenance.py",)); assert len(head) == 40'

install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-research-v3-daily.service" \
  /etc/systemd/system/kalshi-research-v3-daily.service
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-research-v3-daily.timer" \
  /etc/systemd/system/kalshi-research-v3-daily.timer
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-research-v3-durable.service" \
  /etc/systemd/system/kalshi-research-v3-durable.service
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-research-v3-durable.timer" \
  /etc/systemd/system/kalshi-research-v3-durable.timer
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-canonical-generation-witness.service" \
  /etc/systemd/system/kalshi-canonical-generation-witness.service
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-canonical-generation-witness.path" \
  /etc/systemd/system/kalshi-canonical-generation-witness.path
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-canonical-generation-witness.timer" \
  /etc/systemd/system/kalshi-canonical-generation-witness.timer
install -o root -g root -m 0644 \
  "$IMMUTABLE_RUNTIME/deploy/kalshi-canonical-generation-legacy.service" \
  /etc/systemd/system/kalshi-canonical-generation-legacy.service
systemctl daemon-reload
if [ "$TAG_CRED_READY" -eq 1 ]; then
  systemctl enable kalshi-research-v3-daily.timer
  SELECTED_TIMER=kalshi-research-v3-daily.timer
  DISABLED_TIMER=kalshi-research-v3-durable.timer
  DISABLED_SERVICE=kalshi-research-v3-durable.service
  INSTALL_MODE=full-publication
else
  systemctl enable kalshi-research-v3-durable.timer
  SELECTED_TIMER=kalshi-research-v3-durable.timer
  DISABLED_TIMER=kalshi-research-v3-daily.timer
  DISABLED_SERVICE=kalshi-research-v3-daily.service
  INSTALL_MODE=publisher-only-durable
fi
systemctl enable kalshi-canonical-generation-witness.path
systemctl enable kalshi-canonical-generation-witness.timer
LEGACY_ENABLEMENT="$(
  systemctl is-enabled kalshi-canonical-generation-legacy.service \
    2>/dev/null || true)"
if ! systemctl is-enabled --quiet "$SELECTED_TIMER" || \
   systemctl is-enabled --quiet "$DISABLED_TIMER" || \
   ! systemctl is-enabled --quiet kalshi-canonical-generation-witness.path || \
   ! systemctl is-enabled --quiet kalshi-canonical-generation-witness.timer || \
   systemctl is-active --quiet "$SELECTED_TIMER" || \
   systemctl is-active --quiet "$DISABLED_TIMER" || \
   systemctl is-active --quiet kalshi-canonical-generation-witness.path || \
   systemctl is-active --quiet kalshi-canonical-generation-witness.timer || \
   systemctl is-active --quiet kalshi-canonical-generation-witness.service || \
   systemctl is-active --quiet kalshi-canonical-generation-legacy.service || \
   [ "$LEGACY_ENABLEMENT" != static ] || \
   systemctl is-active --quiet "$DISABLED_SERVICE" || \
   systemctl is-active --quiet kalshi-research-v3-daily.service || \
   systemctl is-active --quiet kalshi-research-v3-durable.service; then
  echo "REFUSED: daily and durable timer selection is not mutually exclusive" >&2
  exit 2
fi

echo "installed reviewed commit $COMMIT mode=$INSTALL_MODE; research and generation triggers enabled but not started"
