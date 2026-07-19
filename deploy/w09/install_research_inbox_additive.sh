#!/bin/sh
set -eu

# Add the generic Research Inbox beside the already-audited Deep03 .03
# runtime.  This installer intentionally does not touch release-commit.txt,
# any Deep03 module/unit, AUTHORITY/ARM, the one-shot claim root, or a timer.

PAYLOAD_ROOT=${1:?usage: install_research_inbox_additive.sh PAYLOAD_ROOT}
INSTALL_ROOT=/opt/w09/research
EXPECTED_RUNTIME=3f2071493967f05cd466230c3e8eeb83ac241dab

test "$(tr -d '\n' < "$INSTALL_ROOT/release-commit.txt")" = "$EXPECTED_RUNTIME"
(cd "$PAYLOAD_ROOT" && sha256sum -c deploy/w09/research_inbox_payload.sha256 >/dev/null)

install -d -m 0755 "$INSTALL_ROOT/tools/research/plugins" "$INSTALL_ROOT/deploy/w09"
for module in inbox.py plan_contract.py data_catalog.py data_resolver.py plugin_api.py; do
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

install -d -m 0755 /usr/local/libexec /etc/w09
install -m 0755 "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-control" \
    /usr/local/libexec/w09-research-inbox-control
install -m 0440 "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-control.sudoers" \
    /etc/sudoers.d/w09-research-inbox-control
install -m 0644 "$PAYLOAD_ROOT/deploy/w09/w09-research-inbox-worker@.service" \
    /etc/systemd/system/w09-research-inbox-worker@.service
/usr/sbin/visudo -cf /etc/sudoers.d/w09-research-inbox-control >/dev/null

install -d -o ubuntu -g ubuntu -m 0750 \
    /srv/w09-research/inbox /srv/w09-research/inbox/jobs \
    /srv/w09-research/inbox/.incoming /srv/w09-research/inbox/.control \
    /srv/w09-research/inbox/.locks

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

systemctl daemon-reload
test "$(tr -d '\n' < "$INSTALL_ROOT/release-commit.txt")" = "$EXPECTED_RUNTIME"
test "$(systemctl is-active w09-exploratory-autoresearch.service 2>/dev/null || true)" != active
echo "W09_RESEARCH_INBOX_ADDITIVE_READY runtime=$EXPECTED_RUNTIME"
