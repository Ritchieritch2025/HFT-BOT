import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative):
    return (ROOT / relative).read_text()


def test_installer_uses_sanitized_fixed_git_for_user_owned_source():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in installer
    assert "GIT_BIN=/usr/bin/git" in installer
    assert "GIT_CONFIG_NOSYSTEM=1" in installer
    assert "GIT_CONFIG_GLOBAL=/dev/null" in installer
    assert "GIT_OPTIONAL_LOCKS=0" in installer
    assert 'safe.directory=$1' in installer
    assert "core.fsmonitor=false" in installer
    assert "core.hooksPath=/dev/null" in installer
    assert "core.untrackedCache=false" in installer
    assert "SOURCE_TOPLEVEL=\"$(source_git rev-parse --show-toplevel)\"" \
        in installer
    assert "clone --quiet --no-checkout --no-hardlinks --local" in installer
    assert 'git -C "$ROOT"' not in installer


def test_installer_requires_publisher_and_gates_full_mode_on_broker_pair():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert "validate_encrypted_credential \"$PUB_CRED\"" in installer
    assert ('if [ -e "$IDENTITY_EVIDENCE" ] || '
            '[ -L "$IDENTITY_EVIDENCE" ]; then') in installer
    assert 'IDENTITY_EVIDENCE_READY=1' in installer
    assert 'BROKER_CREDENTIAL_READY=1' in installer
    assert 'FULL_PUBLICATION_READY=1' in installer
    assert 'if [ "$IDENTITY_EVIDENCE_READY" -eq 1 ]; then' in installer
    assert installer.index(
        'FULL_PUBLICATION_READY=1') < installer.index(
        'if [ "$FULL_PUBLICATION_READY" -eq 1 ]; then')
    assert "canonical-ephemeral-tagger-identities-v2" in installer
    assert "canonical-credential-broker" in installer
    assert ("BROKER_CRED=/etc/credstore.encrypted/"
            "kalshi-research-v3-credential-broker.env") in installer
    assert "LEGACY_TAG_CRED=/etc/credstore.encrypted/" \
        "kalshi-research-v3-tagger.credentials" in installer
    assert "quiesced legacy standing tagger credential blob" in installer
    assert "encrypted credential directory must be root:root 0700" in installer
    assert '[ "$(stat -c %a "$path")" != 600 ]' in installer
    assert "MAX_CRED_BYTES=1048576" in installer
    assert "AUTOMATION_FIRST_DATE=2026-07-10" in installer
    assert '[[ "$date" < "$AUTOMATION_FIRST_DATE" ]]' in installer


def test_installer_quiesces_legacy_full_unit_before_credential_refusal():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    timer_stop = installer.index(
        "systemctl disable --now kalshi-research-v3-daily.timer")
    service_stop = installer.index(
        "systemctl stop kalshi-research-v3-daily.service")
    legacy_blob_gate = installer.index('if [ -e "$LEGACY_TAG_CRED" ]')
    assert timer_stop < legacy_blob_gate
    assert service_stop < legacy_blob_gate
    assert "legacy static full publication did not quiesce" in installer


def test_installer_builds_minimal_root_owned_python_and_refuses_stale_release():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert "SYSTEM_PYTHON=/usr/bin/python3" in installer
    assert '"$SYSTEM_PYTHON" -I -m venv --without-pip' in installer
    assert "SYSTEM_PYTHON_REAL" in installer
    assert "runtime symlink escapes reviewed roots" in installer
    assert "immutable release for this commit already exists" in installer
    assert "cp -a /home/ubuntu/hft-bot/.venv" not in installer
    assert "DUCKDB_WHEEL_SHA256=" in installer
    assert "manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl" in installer
    assert "0c72b1dcf27a71ef5f3dc14b92b9ed9274c5584bb0e88590b78907cbb8e254f3" \
        in installer
    assert 'if [ "$(uname -m)" != aarch64 ]; then' in installer
    assert "x86_64.whl" not in installer
    assert "-m ensurepip --default-pip" in installer
    assert "pip install" in installer
    assert "PIP_NO_INDEX=1" in installer
    assert "--no-index --no-deps --no-cache-dir" in installer
    assert 'duckdb.__version__ == "1.4.5"' in installer
    assert 'if [ ! -e "$RUNTIME_RELEASES/$COMMIT" ]' not in installer
    assert 'if [ ! -e "$HISTORY_RELEASES/$COMMIT" ]' not in installer


def test_publisher_only_durable_unit_has_one_credential_and_no_tagger_surface():
    service = _text("deploy/kalshi-research-v3-durable.service")
    timer = _text("deploy/kalshi-research-v3-durable.timer")
    assert "LoadCredentialEncrypted=publisher.env:" in service
    assert "--durable-only" in service
    assert "--publisher-env-file %d/publisher.env" in service
    coordinator = _text("tools/research_v3_daily.py")
    assert "/run/credentials/kalshi-research-v3-durable.service" in coordinator
    assert "DEFAULT_DURABLE_PUBLISHER_ENV_FILE" in coordinator
    assert "--operator-approved" in service
    assert "tagger" not in service.lower()
    assert "credential-broker" not in service.lower()
    assert "research/releases" not in service
    assert "kalshi-pipeline.service" not in "\n".join(
        line for line in service.splitlines() if not line.startswith("#"))
    assert "Unit=kalshi-research-v3-durable.service" in timer
    assert "Persistent=true" in timer


def test_installer_keeps_durable_fallback_when_full_daily_is_enabled():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert 'if [ "$FULL_PUBLICATION_READY" -eq 1 ]; then' in installer
    assert 'systemctl disable --now "$unit"' in installer
    assert "enable kalshi-research-v3-daily.timer" in installer
    assert "enable kalshi-research-v3-durable.timer" in installer
    assert ("is-enabled --quiet kalshi-research-v3-durable.timer"
            in installer)
    assert ("! systemctl is-enabled --quiet "
            "kalshi-research-v3-daily.timer") in installer
    assert ("systemctl disable kalshi-research-v3-daily.timer"
            in installer)
    assert 'systemctl stop "$unit"' in installer
    assert ("is-active --quiet kalshi-research-v3-durable.service"
            in installer)
    assert ("is-active --quiet kalshi-research-v3-daily.timer"
            in installer)
    assert ("is-active --quiet kalshi-research-v3-durable.timer"
            in installer)
    assert "systemctl start" not in installer
    assert "enable --now" not in installer


def test_generation_witness_is_independent_publisher_only_path_and_retry():
    service = _text("deploy/kalshi-canonical-generation-witness.service")
    path = _text("deploy/kalshi-canonical-generation-witness.path")
    timer = _text("deploy/kalshi-canonical-generation-witness.timer")
    legacy = _text("deploy/kalshi-canonical-generation-legacy.service")
    lock = "/var/lib/kalshi-research-v3-locks/generation-witness.lock"

    assert service.count("LoadCredentialEncrypted=") == 1
    assert "LoadCredentialEncrypted=publisher.env:" in service
    assert "tagger" not in service.lower()
    assert "credential-broker" not in service.lower()
    assert "rfq" not in service.lower()
    assert "research_v3_daily.py" not in service
    assert "canonical_generation_daily.py" in service
    assert "--publisher-env-file %d/publisher.env" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "/snap/aws-cli/current/bin" in service
    assert "generation-witness-intents" in service
    assert "generation-witness.lock" in service
    assert lock in service
    assert "OnSuccess=kalshi-research-v3-durable.service" in service
    assert "OnSuccess=kalshi-research-v3-daily.service" not in service
    assert "work/raw" in service and "work/warehouse" in service
    assert "kalshi-pipeline.service" not in "\n".join(
        line for line in service.splitlines() if not line.startswith("#"))

    assert "PathChanged=" in path
    assert "publication-generations/dim" in path
    assert "Unit=kalshi-canonical-generation-witness.service" in path
    assert "Persistent=true" in timer
    assert "Unit=kalshi-canonical-generation-witness.service" in timer

    assert legacy.count("LoadCredentialEncrypted=") == 1
    assert "--migrate-all-legacy" in legacy
    assert "generation-migration-proofs" in legacy
    assert lock in legacy
    assert "tagger" not in legacy.lower()
    assert "credential-broker" not in legacy.lower()
    assert "rfq" not in legacy.lower()


def test_success_chain_only_enables_full_publisher_when_broker_is_ready():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    durable = _text("deploy/kalshi-research-v3-durable.service")
    dropin = _text("deploy/kalshi-research-v3-durable-on-success.conf")

    assert "OnSuccess=" not in durable
    assert "OnSuccess=kalshi-research-v3-daily.service" in dropin
    assert "kalshi-pipeline.service" not in dropin
    assert 'if [ "$FULL_PUBLICATION_READY" -eq 1 ]; then' in installer
    assert '"$IMMUTABLE_RUNTIME/deploy/' \
           'kalshi-research-v3-durable-on-success.conf"' in installer
    assert 'rm -f -- "$DURABLE_CHAIN_DROPIN"' in installer
    assert 'test -f "$DURABLE_CHAIN_DROPIN"' in installer
    assert 'test ! -e "$DURABLE_CHAIN_DROPIN"' in installer
    assert "unmanaged durable service drop-in" in installer
    assert "WITNESS_ON_SUCCESS" in installer
    assert "DURABLE_ON_SUCCESS" in installer
    assert "systemctl show" in installer
    # Independent retry timers remain the fail-safe path after any failed edge.
    assert "enable kalshi-research-v3-durable.timer" in installer
    assert "enable kalshi-research-v3-daily.timer" in installer


def test_installer_quiesces_and_installs_generation_units_before_cutover():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    tmpfiles = _text("deploy/kalshi-research-v3-daily.tmpfiles.conf")
    cutover = installer.index('ln -sfn "$RUNTIME_RELEASES/$COMMIT"')

    for unit in (
            "kalshi-canonical-generation-witness.path",
            "kalshi-canonical-generation-witness.timer",
            "kalshi-canonical-generation-witness.service",
            "kalshi-canonical-generation-legacy.service"):
        assert installer.index(unit) < cutover
        assert f"$IMMUTABLE_RUNTIME/deploy/{unit}" in installer
    assert "enable kalshi-canonical-generation-witness.path" in installer
    assert "enable kalshi-canonical-generation-witness.timer" in installer
    assert "enable kalshi-canonical-generation-legacy.service" not in installer
    assert 'LEGACY_ENABLEMENT="$(' in installer
    assert '[ "$LEGACY_ENABLEMENT" != static ]' in installer
    assert "generation-witness-intents 0700" in tmpfiles
    assert "generation-witness.lock 0600" in tmpfiles
    assert "generation-migration-proofs 0750" in tmpfiles
    assert "generation-daily 0750" in tmpfiles


def test_installer_validates_generation_lock_below_root_owned_parent():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    tmpfiles = _text("deploy/kalshi-research-v3-daily.tmpfiles.conf")

    assert ("GENERATION_WRITER_LOCK=/var/lib/kalshi-research-v3-locks/"
            "generation-witness.lock") in installer
    assert ("/var/lib/kalshi-research-v3-locks 0750 root "
            "kalshi-research-v3") in tmpfiles
    assert ("/var/lib/kalshi-research-v3-locks/generation-witness.lock "
            "0600 kalshi-research-v3 kalshi-research-v3") in tmpfiles
    assert 'stat -c %u "$GENERATION_WRITER_LOCK_PARENT"' in installer
    assert 'stat -c %a "$GENERATION_WRITER_LOCK_PARENT"' in installer
    assert '[ -L "$GENERATION_WRITER_LOCK" ]' in installer
    assert 'stat -c %a "$GENERATION_WRITER_LOCK"' in installer
    assert 'stat -c %h "$GENERATION_WRITER_LOCK"' in installer
    assert 'setfacl -b "$GENERATION_WRITER_LOCK"' not in installer
    assert 'chown "$SERVICE_USER:$SERVICE_GROUP" "$GENERATION_WRITER_LOCK"' \
        not in installer
    assert 'chmod 0600 "$GENERATION_WRITER_LOCK"' not in installer


def test_installer_restores_private_witness_intention_modes_after_acl_pass():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    root = ("WITNESS_INTENTION_ROOT=/home/ubuntu/hft-bot/work/live/"
            "canonical_receipts/generation-witness-intents")
    assert root in installer
    acl_pass = installer.index('for root in "${DAILY_MUTABLE_ROOTS[@]}"')
    normalize = installer.index(
        'setfacl -R -b -k "$WITNESS_INTENTION_ROOT"')
    assert normalize > acl_pass
    assert ('find -P "$WITNESS_INTENTION_ROOT" -type d '
            '-exec chmod 0700 {} +') in installer
    assert ('find -P "$WITNESS_INTENTION_ROOT" -type f '
            '-exec chmod 0600 {} +') in installer
    assert ('find -P "$WITNESS_INTENTION_ROOT" -mindepth 1 '
            '\\\n  ! -type d ! -type f -print -quit') in installer


def test_installer_repairs_generation_manifests_for_shared_read_only_group():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert ("GENERATION_MANIFEST_ROOT=/home/ubuntu/hft-bot/work/warehouse/"
            ".publication-generations") in installer
    assert 'chgrp -R "$LOCK_GROUP" "$GENERATION_MANIFEST_ROOT"' in installer
    assert ('find -P "$GENERATION_MANIFEST_ROOT" -type d '
            '-exec chmod 2750 {} +') in installer
    assert ('find -P "$GENERATION_MANIFEST_ROOT" -type f '
            '-exec chmod 0640 {} +') in installer


def test_full_daily_has_unique_uid_and_shared_hardened_ephemeral_lock():
    service = _text("deploy/kalshi-research-v3-daily.service")
    durable = _text("deploy/kalshi-research-v3-durable.service")
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    tmpfiles = _text("deploy/kalshi-research-v3-daily.tmpfiles.conf")
    lock = "/var/lib/kalshi-research-v3-locks/ephemeral-tagger.lock"

    assert "User=kalshi-research-v3-credential-broker" in service
    assert "ProtectProc=invisible" in service
    assert "PrivateTmp=true" in service
    assert "--dedicated-service-isolation-attested" in service
    assert service.count("LoadCredentialEncrypted=") == 2
    assert "LoadCredentialEncrypted=publisher.env:" in service
    assert "LoadCredentialEncrypted=credential-broker.env:" in service
    assert "--credential-broker-env-file %d/credential-broker.env" in service
    assert "TimeoutStopSec=40min" in service
    assert "KillMode=control-group" in service
    assert "KillSignal=SIGTERM" in service
    assert lock in service
    assert (f"{lock} 0600 kalshi-research-v3-credential-broker "
            "kalshi-research-v3") in tmpfiles
    assert ("DAILY_SERVICE_USER=kalshi-research-v3-credential-broker"
            in installer)
    assert "id -u \"$DAILY_SERVICE_USER\"" in installer
    assert "stat -c %h \"$EPHEMERAL_TAGGER_LOCK\"" in installer
    assert "runuser -u \"$DAILY_SERVICE_USER\" -- test -w " \
        "\"$EPHEMERAL_TAGGER_LOCK\"" in installer
    orchestrator = (
        "/var/lib/kalshi-research-v3-locks/research-v3-orchestrator.lock")
    assert orchestrator in service and orchestrator in durable
    assert f"{orchestrator} 0660 root kalshi-research-v3" in tmpfiles
    assert "stat -c %h \"$ORCHESTRATOR_LOCK\"" in installer
    assert "research orchestrator lock ownership/mode is not exact" in installer


def test_broker_policy_is_exact_target_minimum_and_never_on_shared_units():
    relative = (
        "docs/plan_releases/pipeline/"
        "W-PUB-REF-01C_CREDENTIAL_BROKER_IDENTITY_POLICY.json")
    raw = (ROOT / relative).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == \
        "a73b56a72034e7933fac913639102dafb29aa026b5b96fb3d1cde0fc4a84c872"
    policy = json.loads(raw)
    assert policy["Version"] == "2012-10-17"
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement == {
        "Sid": "ManageOnlyCanonicalEligibilityTaggerEphemeralKeys",
        "Effect": "Allow",
        "Action": [
            "iam:GetUser",
            "iam:ListAccessKeys",
            "iam:CreateAccessKey",
            "iam:UpdateAccessKey",
            "iam:DeleteAccessKey",
        ],
        "Resource": (
            "arn:aws:iam::321572485933:user/"
            "canonical-eligibility-tagger"),
    }
    for unit in (
            "deploy/kalshi-research-v3-durable.service",
            "deploy/kalshi-canonical-generation-witness.service",
            "deploy/kalshi-canonical-generation-legacy.service"):
        text = _text(unit).lower()
        assert "credential-broker" not in text
        assert "iam:createaccesskey" not in text


def test_systemd_stop_window_exceeds_internal_cleanup_by_safe_margin():
    service = _text("deploy/kalshi-research-v3-daily.service")
    coordinator = _text("tools/research_v3_daily.py")
    assert "EPHEMERAL_CLEANUP_GRACE_SECONDS = 30 * 60" in coordinator
    assert "TimeoutStopSec=40min" in service
    assert 40 * 60 > 30 * 60 + 90


def test_installer_uses_immutable_release_for_all_privileged_installs():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert 'IMMUTABLE_RUNTIME="$RUNTIME_RELEASES/$COMMIT"' in installer
    assert 'IMMUTABLE_AUTHORIZATION="$IMMUTABLE_RUNTIME/$AUTHORIZATION_REL"' \
        in installer
    assert 'install -o root -g root -m 0444 "$IMMUTABLE_AUTHORIZATION"' \
        in installer
    assert '"$ROOT/deploy/' not in installer
    immutable_deploy_installs = {
        line.strip().split("/")[-1].rstrip('" \\')
        for line in installer.splitlines()
        if '"$IMMUTABLE_RUNTIME/deploy/' in line
    }
    assert immutable_deploy_installs == {
        "kalshi-research-v3-daily.tmpfiles.conf",
        "kalshi-research-v3-daily.service",
        "kalshi-research-v3-daily.timer",
        "kalshi-research-v3-durable.service",
        "kalshi-research-v3-durable.timer",
        "kalshi-canonical-generation-witness.service",
        "kalshi-canonical-generation-witness.path",
        "kalshi-canonical-generation-witness.timer",
        "kalshi-canonical-generation-legacy.service",
        "kalshi-research-v3-durable-on-success.conf",
    }
