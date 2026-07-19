#!/usr/bin/env bash
# Install, but do not run, the independent read-only fresh-RFQ producer timer.
set -euo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
RUNTIME=/opt/kalshi-fresh-rfq-daily
AUTHORITY=/var/lib/kalshi-rfq-fresh/control/fresh-rfq-20260720-01/authority-envelope.json
AUTHORITY_FILE_SHA=2fa1caf792d540e0b7ce4641bb82161f09ce3926a81652a2006468a9649ceaf2
AWS_CLI=/snap/aws-cli/current/bin/aws
SERVICE=kalshi-fresh-rfq-daily-producer.service
TIMER=kalshi-fresh-rfq-daily-producer.timer
TMPFILES=kalshi-fresh-rfq-daily-producer.tmpfiles.conf

git_safe() {
  directory=$1
  shift
  git -c "safe.directory=$directory" -c core.hooksPath=/dev/null \
    -c core.fsmonitor=false -C "$directory" "$@"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "REFUSED: run as root" >&2
  exit 2
fi
for command in git install readlink sha256sum stat systemctl systemd-tmpfiles; do
  if ! command -v "$command" >/dev/null; then
    echo "REFUSED: missing command: $command" >&2
    exit 2
  fi
done
if [ -n "$(git_safe "$ROOT" status --porcelain=v1 --untracked-files=all)" ]; then
  echo "REFUSED: reviewed source commit must be clean" >&2
  exit 2
fi
if [ ! -e "$RUNTIME/.git" ]; then
  echo "REFUSED: independent fresh-RFQ runtime checkout is absent" >&2
  exit 2
fi
ROOT_REAL="$(readlink -f -- "$ROOT")"
RUNTIME_REAL="$(readlink -f -- "$RUNTIME")"
if [ "$ROOT_REAL" = "$RUNTIME_REAL" ] || [ "$RUNTIME_REAL" = "/opt/kalshi-research-v3" ]; then
  echo "REFUSED: fresh-RFQ runtime must be independent of source/base runtime" >&2
  exit 2
fi
if [ -n "$(git_safe "$RUNTIME" status --porcelain=v1 --untracked-files=all)" ]; then
  echo "REFUSED: immutable fresh-RFQ runtime checkout is not clean" >&2
  exit 2
fi
runtime_owner="$(stat -c '%U:%G' -- "$RUNTIME")"
runtime_mode="$(stat -c '%a' -- "$RUNTIME")"
if [ "$runtime_owner" != "root:root" ] || [ $((8#$runtime_mode & 0022)) -ne 0 ]; then
  echo "REFUSED: independent runtime root must be root-owned and non-writable" >&2
  exit 2
fi
SOURCE_COMMIT="$(git_safe "$ROOT" rev-parse --verify HEAD^{commit})"
RUNTIME_COMMIT="$(git_safe "$RUNTIME" rev-parse --verify HEAD^{commit})"
if [ "$SOURCE_COMMIT" != "$RUNTIME_COMMIT" ]; then
  echo "REFUSED: /opt runtime is not the reviewed source commit" >&2
  exit 2
fi
if [ ! -x "$AWS_CLI" ]; then
  echo "REFUSED: fixed AWS CLI is unavailable" >&2
  exit 2
fi
read -r authority_sha _ < <(sha256sum -- "$AUTHORITY")
if [ "$authority_sha" != "$AUTHORITY_FILE_SHA" ]; then
  echo "REFUSED: production authority envelope bytes differ" >&2
  exit 2
fi
for path in \
  "$RUNTIME/tools/fresh_rfq_daily_pipeline.py" \
  "$RUNTIME/tools/fresh_rfq_daily_runner.py" \
  "$RUNTIME/tools/fresh_rfq_daily_eligibility.py" \
  "$RUNTIME/tools/fresh_rfq_overlay_release.py" \
  "$RUNTIME/tools/fresh_rfq_request_provenance.py" \
  "$RUNTIME/tools/fresh_rfq_universe_provenance.py" \
  "$ROOT/deploy/$SERVICE" \
  "$ROOT/deploy/$TIMER" \
  "$ROOT/deploy/$TMPFILES"; do
  if [ ! -f "$path" ] || [ -L "$path" ]; then
    echo "REFUSED: required reviewed file is missing or a symlink: $path" >&2
    exit 2
  fi
done

install -o root -g root -m 0644 "$ROOT/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
install -o root -g root -m 0644 "$ROOT/deploy/$TIMER" "/etc/systemd/system/$TIMER"
install -o root -g root -m 0644 "$ROOT/deploy/$TMPFILES" "/etc/tmpfiles.d/$TMPFILES"
systemd-tmpfiles --create "/etc/tmpfiles.d/$TMPFILES"
systemctl daemon-reload
systemctl enable "$TIMER"

echo "INSTALLED_NOT_STARTED commit=$SOURCE_COMMIT timer=$TIMER aws_writes=0"
