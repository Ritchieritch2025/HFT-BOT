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


def test_installer_requires_safe_publisher_but_allows_absent_tagger_blob():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert "validate_encrypted_credential \"$PUB_CRED\"" in installer
    assert 'if [ -e "$TAG_CRED" ] || [ -L "$TAG_CRED" ]; then' in installer
    assert "validate_encrypted_credential \"$TAG_CRED\"" in installer
    assert 'TAG_CRED_READY=1' in installer
    assert "encrypted credential directory must be root:root 0700" in installer
    assert '[ "$(stat -c %a "$path")" != 600 ]' in installer
    assert "MAX_CRED_BYTES=1048576" in installer
    assert "AUTOMATION_FIRST_DATE=2026-07-10" in installer
    assert '[[ "$date" < "$AUTOMATION_FIRST_DATE" ]]' in installer


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
    assert "--operator-approved" in service
    assert "tagger" not in service.lower()
    assert "research/releases" not in service
    assert "kalshi-pipeline.service" not in "\n".join(
        line for line in service.splitlines() if not line.startswith("#"))
    assert "Unit=kalshi-research-v3-durable.service" in timer
    assert "Persistent=true" in timer


def test_installer_selects_only_the_credential_compatible_timer():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert 'if [ "$TAG_CRED_READY" -eq 1 ]; then' in installer
    assert 'systemctl disable --now "$unit"' in installer
    assert "enable kalshi-research-v3-daily.timer" in installer
    assert "enable kalshi-research-v3-durable.timer" in installer
    assert 'is-enabled --quiet "$SELECTED_TIMER"' in installer
    assert 'is-enabled --quiet "$DISABLED_TIMER"' in installer
    assert 'is-active --quiet "$DISABLED_TIMER"' in installer
    assert 'systemctl stop "$unit"' in installer
    assert 'is-active --quiet "$SELECTED_TIMER"' in installer
    assert 'is-active --quiet "$DISABLED_SERVICE"' in installer
    assert "systemctl start" not in installer
    assert "enable --now" not in installer


def test_generation_witness_is_independent_publisher_only_path_and_retry():
    service = _text("deploy/kalshi-canonical-generation-witness.service")
    path = _text("deploy/kalshi-canonical-generation-witness.path")
    timer = _text("deploy/kalshi-canonical-generation-witness.timer")
    legacy = _text("deploy/kalshi-canonical-generation-legacy.service")

    assert service.count("LoadCredentialEncrypted=") == 1
    assert "LoadCredentialEncrypted=publisher.env:" in service
    assert "tagger" not in service.lower()
    assert "rfq" not in service.lower()
    assert "research_v3_daily.py" not in service
    assert "canonical_generation_daily.py" in service
    assert "--publisher-env-file %d/publisher.env" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "/snap/aws-cli/current/bin" in service
    assert "generation-witness-intents" in service
    assert "generation-witness.lock" in service
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
    assert "tagger" not in legacy.lower()
    assert "rfq" not in legacy.lower()


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


def test_installer_restores_exact_generation_writer_lock_after_recursive_acl():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")

    broad_acl = installer.index('setfacl -R -m "u:$SERVICE_USER:r-X"')
    clear_lock_acl = installer.index('setfacl -b "$GENERATION_WRITER_LOCK"')
    exact_lock_mode = installer.index('chmod 0600 "$GENERATION_WRITER_LOCK"')
    lock_write_probe = installer.index(
        'runuser -u "$SERVICE_USER" -- test -w "$GENERATION_WRITER_LOCK"')

    assert broad_acl < clear_lock_acl < exact_lock_mode < lock_write_probe
    assert '[ -L "$GENERATION_WRITER_LOCK" ]' in installer
    assert 'stat -c %a "$GENERATION_WRITER_LOCK"' in installer


def test_installer_uses_immutable_release_for_all_privileged_installs():
    installer = _text("deploy/install_kalshi_research_v3_daily.sh")
    assert 'IMMUTABLE_RUNTIME="$RUNTIME_RELEASES/$COMMIT"' in installer
    assert 'IMMUTABLE_AUTHORIZATION="$IMMUTABLE_RUNTIME/$AUTHORIZATION_REL"' \
        in installer
    assert 'install -o root -g root -m 0444 "$IMMUTABLE_AUTHORIZATION"' \
        in installer
    assert '"$ROOT/deploy/' not in installer
    assert installer.count('"$IMMUTABLE_RUNTIME/deploy/') == 9
