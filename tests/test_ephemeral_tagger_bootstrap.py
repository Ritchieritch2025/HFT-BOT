import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import traceback

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_eligibility_tagger as canonical_tagger  # noqa: E402
import ephemeral_tagger_bootstrap as bootstrap  # noqa: E402


KEY_ID = "AKIAABCDEFGHIJKLMNOP"
SECRET = "fixtureSecretAccessKey0000000000000000000"
BOOTSTRAP_KEY = "AKIABOOTSTRAP00000000"
BOOTSTRAP_SECRET = "bootstrapSecret000000000000000000000000"
BOOTSTRAP_USER_ID = "AIDAVAULTWRITER000000"
TAGGER_USER_ID = "AIDATAGGERUSER0000000"


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


@pytest.fixture
def pins(tmp_path):
    aws = tmp_path / "aws"
    aws.write_text("#!/bin/sh\nexit 1\n")
    aws.chmod(0o700)
    return bootstrap.RuntimePins(
        aws_cli=aws.absolute(),
        python=pathlib.Path(sys.executable).absolute(),
        tagger=bootstrap.TAGGER,
        production=False,
    )


def _tagger_input(pins, *extra):
    return [
        str(pins.python),
        str(pins.tagger),
        "--receipt-index", "/tmp/DURABLE.json",
        "--single-writer-audit", "/tmp/AUDIT.json",
        "--policy-evidence", "/tmp/POLICY.json",
        "--bucket", bootstrap.BUCKET,
        "--prefix", bootstrap.PREFIX,
        "--output-root", "/tmp/tagged",
        "--operator-approved",
        *extra,
    ]


def _main_argv(pins, *options, command=None):
    return [
        "--bootstrap-user-id", BOOTSTRAP_USER_ID,
        "--tagger-user-id", TAGGER_USER_ID,
        *options,
        "--",
        *(command or _tagger_input(pins)),
    ]


@pytest.fixture
def bootstrap_credential_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", BOOTSTRAP_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", BOOTSTRAP_SECRET)
    monkeypatch.setenv("AWS_PROFILE", "vaultWriter")
    monkeypatch.setenv("KALSHI_API_KEY", "must-not-leak")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/tmp/poison")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/poison.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/poison.dylib")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://attacker.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://attacker.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://attacker.invalid")
    monkeypatch.setenv("http_proxy", "http://attacker.invalid")


class FakeAwsAndTagger:
    def __init__(self, *, bootstrap_arn=bootstrap.BOOTSTRAP_ARN,
                 bootstrap_user_id=BOOTSTRAP_USER_ID,
                 tagger_arn=bootstrap.TAGGER_ARN,
                 tagger_user_id=TAGGER_USER_ID,
                 get_user_id=TAGGER_USER_ID,
                 tagger_returncode=0, tagger_output="tagger-ok\n",
                 create_status="Active", initial_records=None,
                 cleanup_lists=None):
        self.calls = []
        self.list_count = 0
        self.bootstrap_arn = bootstrap_arn
        self.bootstrap_user_id = bootstrap_user_id
        self.tagger_arn = tagger_arn
        self.tagger_user_id = tagger_user_id
        self.get_user_id = get_user_id
        self.tagger_returncode = tagger_returncode
        self.tagger_output = tagger_output
        self.create_status = create_status
        self.initial_records = initial_records or []
        self.cleanup_lists = list(cleanup_lists or [[], []])

    @staticmethod
    def _metadata(rows):
        return [{
            "UserName": bootstrap.USER_NAME,
            "AccessKeyId": key,
            "Status": status,
        } for key, status in rows]

    def __call__(self, command, **kwargs):
        env = kwargs["env"]
        self.calls.append((list(command), dict(env), kwargs.get("input_text")))
        flat = " ".join(command)
        if "sts get-caller-identity" in flat:
            if env.get("AWS_ACCESS_KEY_ID") == BOOTSTRAP_KEY:
                value = {
                    "Account": bootstrap.ACCOUNT,
                    "Arn": self.bootstrap_arn,
                    "UserId": self.bootstrap_user_id,
                }
            else:
                assert env["AWS_ACCESS_KEY_ID"] == KEY_ID
                assert env["AWS_SECRET_ACCESS_KEY"] == SECRET
                value = {
                    "Account": bootstrap.ACCOUNT,
                    "Arn": self.tagger_arn,
                    "UserId": self.tagger_user_id,
                }
            return _completed(command, stdout=json.dumps(value))
        if "iam list-access-keys" in flat:
            self.list_count += 1
            rows = (self.initial_records if self.list_count == 1 else
                    self.cleanup_lists.pop(0) if self.cleanup_lists else [])
            return _completed(command, stdout=json.dumps({
                "AccessKeyMetadata": self._metadata(rows),
                "IsTruncated": False,
            }))
        if "iam get-user" in flat:
            return _completed(command, stdout=json.dumps({"User": {
                "UserName": bootstrap.USER_NAME,
                "Arn": bootstrap.TAGGER_ARN,
                "UserId": self.get_user_id,
            }}))
        if "iam create-access-key" in flat:
            return _completed(command, stdout=json.dumps({"AccessKey": {
                "UserName": bootstrap.USER_NAME,
                "AccessKeyId": KEY_ID,
                "SecretAccessKey": SECRET,
                "Status": self.create_status,
            }}))
        if "iam update-access-key" in flat:
            payload = json.loads(kwargs["input_text"])
            assert payload == {
                "UserName": bootstrap.USER_NAME,
                "AccessKeyId": KEY_ID,
                "Status": "Inactive",
            }
            assert KEY_ID not in command
            assert env["AWS_ACCESS_KEY_ID"] == BOOTSTRAP_KEY
            return _completed(command, stdout="{}")
        if "iam delete-access-key" in flat:
            payload = json.loads(kwargs["input_text"])
            assert payload["AccessKeyId"] == KEY_ID
            assert KEY_ID not in command
            assert env["AWS_ACCESS_KEY_ID"] == BOOTSTRAP_KEY
            return _completed(command, stdout="{}")

        assert command[:4] == [
            str(pathlib.Path(sys.executable).absolute()), "-E", "-s",
            str(bootstrap.TAGGER),
        ]
        assert env["AWS_ACCESS_KEY_ID"] == KEY_ID
        assert env["AWS_SECRET_ACCESS_KEY"] == SECRET
        _assert_sterile(env)
        return _completed(
            command,
            returncode=self.tagger_returncode,
            stdout=self.tagger_output,
            stderr="tagger-failed" if self.tagger_returncode else "",
        )


def _assert_sterile(env):
    forbidden = (
        "KALSHI", "TELEGRAM", "PYTHON", "LD_", "DYLD_",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "AWS_ENDPOINT_URL",
    )
    assert not any(name.startswith(forbidden) for name in env)
    assert env["AWS_CONFIG_FILE"] == "/dev/null"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert env["AWS_CLI_HISTORY_FILE"] == "/dev/null"
    assert env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def _execute(pins, tmp_path, monkeypatch, fake, **kwargs):
    monkeypatch.setattr(bootstrap, "_run_process", fake)
    command = bootstrap.validate_tagger_command(_tagger_input(pins), pins)
    return bootstrap.execute_tagger(
        command,
        pins=pins,
        bootstrap_user_id=BOOTSTRAP_USER_ID,
        tagger_user_id=TAGGER_USER_ID,
        lock_path=tmp_path / "bootstrap.lock",
        sleep=lambda _seconds: None,
        harden_process=False,
        validate_self=False,
        **kwargs,
    )


def _actions(fake):
    rows = []
    for command, _env, _input in fake.calls:
        flat = " ".join(command)
        if "get-caller-identity" in flat:
            rows.append("sts")
        elif "get-user" in flat:
            rows.append("get-user")
        elif "list-access-keys" in flat:
            rows.append("list")
        elif "create-access-key" in flat:
            rows.append("create")
        elif "update-access-key" in flat:
            rows.append("inactive")
        elif "delete-access-key" in flat:
            rows.append("delete")
        else:
            rows.append("tagger")
    return rows


def test_default_check_is_local_only(monkeypatch, pins, capsys):
    monkeypatch.setattr(bootstrap, "PRODUCTION_PINS", pins)
    monkeypatch.setattr(
        bootstrap, "_run_process",
        lambda *_a, **_kw: pytest.fail("CHECK spawned a subprocess"),
    )
    assert bootstrap.main(_main_argv(pins)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["mode"] == "CHECK"
    assert value["rfq"] == "OFF"
    assert value["aws_mutations"] == 0
    assert value["same_uid_process_isolation"].startswith("DEDICATED_UID")


@pytest.mark.parametrize("flag", [
    "--include-sealed-rfq",
    "--include-s",
    "--rfq-eligibility-evidence=/tmp/evidence.json",
    "--rf",
    "--no-rfq",
])
def test_rfq_exact_and_abbreviated_options_are_hard_rejected(flag, pins):
    with pytest.raises(bootstrap.GateError, match="RFQ_GATE"):
        bootstrap.validate_tagger_command(_tagger_input(pins, flag), pins)


@pytest.mark.parametrize("option", ["--receip", "--aws-cli", "--help", "-h"])
def test_unknown_abbreviated_or_escape_options_are_rejected(option, pins):
    with pytest.raises(bootstrap.GateError, match="TAGGER_ARGUMENT_INVALID"):
        bootstrap.validate_tagger_command(_tagger_input(pins, option), pins)


def test_all_daily_modes_have_disjoint_exact_argument_allowlists(pins):
    recover = _tagger_input(
        pins, "--recover-existing-only", "--pending-index", "/tmp/PENDING.json")
    verify = [
        str(pins.python), str(pins.tagger), "--verify-only",
        "--tagged-index", "/tmp/TAGGED.json",
        "--byte-receipt-index", "/tmp/DURABLE.json",
        "--expected-tagger-principal", bootstrap.TAGGER_ARN,
        "--bucket", bootstrap.BUCKET, "--prefix", bootstrap.PREFIX,
        "--proof-output-root", "/tmp/proof",
    ]
    patrol = [
        str(pins.python), str(pins.tagger), "--policy-canary",
        "--receipt-index", "/tmp/DURABLE.json",
        "--expected-tagger-principal", bootstrap.TAGGER_ARN,
        "--bucket", bootstrap.BUCKET, "--prefix", bootstrap.PREFIX,
        "--canary-output-root", "/tmp/canary",
        "--canary-target-key", "ec2/warehouse/seals/date=2026-07-14.json",
        "--canary-target-version-id", "version-1",
        "--operator-approved",
    ]
    for command in (recover, verify, patrol):
        validated = bootstrap.validate_tagger_command(command, pins)
        assert validated[-2:] == ["--aws-cli", str(pins.aws_cli)]
    with pytest.raises(bootstrap.GateError, match="mutually exclusive"):
        bootstrap.validate_tagger_command(
            [*verify, "--policy-canary", "--operator-approved"], pins)
    with pytest.raises(bootstrap.GateError, match="forbidden options"):
        bootstrap.validate_tagger_command(
            [*verify, "--operator-approved"], pins)


def test_canonical_tagger_argparse_disables_abbreviations(capsys):
    base = [
        "--receipt-index", "/tmp/DURABLE.json",
        "--single-writer-audit", "/tmp/AUDIT.json",
        "--policy-evidence", "/tmp/POLICY.json",
        "--operator-approved",
    ]
    for abbreviation in ("--include-s", "--rf"):
        with pytest.raises(SystemExit) as raised:
            canonical_tagger.main([*base, abbreviation])
        assert raised.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


def test_real_pinned_python_canonical_help_and_parser_smoke():
    """Exercise sibling imports and strict parsing in real child processes."""
    python = bootstrap.PRODUCTION_PINS.python
    if not python.exists():
        pytest.skip("production Python pin is unavailable on this test host")
    env = {
        "PATH": bootstrap.TRUSTED_PATH,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # The real child must ignore this injection because it is launched -E.
        "PYTHONPATH": "/definitely/not/a/trusted/module/path",
        "PYTHONINSPECT": "1",
    }
    base = [str(python), "-E", "-s", str(bootstrap.TAGGER)]
    help_result = subprocess.run(
        [*base, "--help"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--receipt-index" in help_result.stdout
    assert "--include-sealed-rfq" in help_result.stdout

    required = [
        "--receipt-index", "/tmp/DURABLE.json",
        "--single-writer-audit", "/tmp/AUDIT.json",
        "--policy-evidence", "/tmp/POLICY.json",
        "--operator-approved",
    ]
    for abbreviation in ("--include-s", "--rf"):
        refused = subprocess.run(
            [*base, *required, abbreviation], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert refused.returncode == 2
        assert "unrecognized arguments" in refused.stderr


def test_direct_shebang_and_lookalike_python_are_forbidden(pins, tmp_path):
    with pytest.raises(bootstrap.GateError, match="Python path is not pinned"):
        bootstrap.validate_tagger_command(_tagger_input(pins)[1:], pins)
    lookalike = tmp_path / "python3"
    lookalike.write_text("#!/bin/sh\nexit 0\n")
    lookalike.chmod(0o700)
    command = _tagger_input(pins)
    command[0] = str(lookalike)
    with pytest.raises(bootstrap.GateError, match="Python path is not pinned"):
        bootstrap.validate_tagger_command(command, pins)


def test_production_pins_are_fixed_and_overrides_fail_closed(pins):
    assert str(bootstrap.PRODUCTION_PINS.aws_cli) == \
        "/snap/aws-cli/current/bin/aws"
    assert str(bootstrap.PRODUCTION_PINS.aws_cli) != "/snap/bin/aws"
    assert str(bootstrap.PRODUCTION_PINS.python) == "/usr/bin/python3"
    overridden = bootstrap.RuntimePins(
        aws_cli=pins.aws_cli,
        python=pins.python,
        tagger=pins.tagger,
        production=True,
    )
    with pytest.raises(bootstrap.GateError, match="may not be overridden"):
        bootstrap.validate_runtime_pins(overridden)


def test_runtime_pin_requires_safe_regular_owner_and_mode(pins):
    bootstrap.validate_runtime_pins(pins)
    pins.aws_cli.chmod(0o722)
    with pytest.raises(bootstrap.GateError, match="ownership or mode"):
        bootstrap.validate_runtime_pins(pins)


def test_bootstrap_itself_requires_isolated_pinned_python(pins):
    # Pytest is intentionally not running under -I, so the production self
    # gate must reject this ambient interpreter even though its realpath is pinned.
    with pytest.raises(bootstrap.GateError, match="launched with -I"):
        bootstrap.validate_self_runtime(pins)


def test_sterile_environment_drops_all_ambient_injection_vectors():
    source = {
        "AWS_ACCESS_KEY_ID": BOOTSTRAP_KEY,
        "AWS_SECRET_ACCESS_KEY": BOOTSTRAP_SECRET,
        "AWS_SESSION_TOKEN": "session",
        "AWS_PROFILE": "vaultWriter",
        "AWS_ENDPOINT_URL": "https://attacker.invalid",
        "AWS_ENDPOINT_URL_STS": "https://attacker.invalid",
        "KALSHI_API_KEY": "secret",
        "TELEGRAM_BOT_TOKEN": "secret",
        "PYTHONPATH": "/tmp/poison",
        "PYTHONINSPECT": "1",
        "LD_PRELOAD": "/tmp/poison.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/poison.dylib",
        "HTTPS_PROXY": "https://attacker.invalid",
        "http_proxy": "http://attacker.invalid",
    }
    env = bootstrap._sterile_environment(source)
    _assert_sterile(env)
    assert env["AWS_ACCESS_KEY_ID"] == BOOTSTRAP_KEY
    assert env["AWS_SESSION_TOKEN"] == "session"
    assert "AWS_PROFILE" not in env


def test_happy_path_checks_both_exact_identities_and_two_zero_lists(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(tagger_output=f"{KEY_ID} {SECRET}\n")
    result = _execute(pins, tmp_path, monkeypatch, fake)
    actions = _actions(fake)
    assert actions[:8] == [
        "sts", "get-user", "list", "create", "sts", "tagger",
        "inactive", "delete",
    ]
    assert actions[8:] == ["list"] * 5
    assert KEY_ID not in result.stdout
    assert SECRET not in result.stdout
    assert "REDACTED_EPHEMERAL_CREDENTIAL" in result.stdout
    for _command, env, _input in fake.calls:
        _assert_sterile(env)


def test_bootstrap_identity_mismatch_refuses_before_list_or_create(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(
        bootstrap_arn="arn:aws:iam::321572485933:user/not-vaultWriter")
    with pytest.raises(bootstrap.GateError, match="CALLER_IDENTITY_MISMATCH"):
        _execute(pins, tmp_path, monkeypatch, fake)
    assert _actions(fake) == ["sts"]


def test_get_user_id_pin_refuses_before_list_or_create(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(get_user_id="AIDAWRONGTAGGER00000")
    with pytest.raises(bootstrap.GateError, match="CALLER_IDENTITY_MISMATCH"):
        _execute(pins, tmp_path, monkeypatch, fake)
    assert _actions(fake) == ["sts", "get-user"]


def test_initial_stale_key_is_deleted_double_zero_then_requires_rerun(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(initial_records=[(KEY_ID, "Active")])
    with pytest.raises(
            bootstrap.GateError,
            match="STALE_KEYS_RECONCILED_RETRY_REQUIRED"):
        _execute(pins, tmp_path, monkeypatch, fake)
    actions = _actions(fake)
    assert actions[:5] == ["sts", "get-user", "list", "inactive", "delete"]
    assert "create" not in actions
    assert "tagger" not in actions
    assert actions[-2:] == ["list", "list"]


def test_get_user_permission_failure_has_explicit_authority_code(
        monkeypatch, pins, bootstrap_credential_env):
    def denied(command, **_kwargs):
        return _completed(command, returncode=254, stderr="AccessDenied")

    monkeypatch.setattr(bootstrap, "_run_process", denied)
    aws = bootstrap.AwsIamBootstrap(
        pins.aws_cli, bootstrap._sterile_environment())
    with pytest.raises(
            bootstrap.GateError, match="CREDENTIAL_AUTHORITY_DENIED"):
        aws.get_user()


def test_production_lock_is_shared_outside_private_tmp():
    assert bootstrap.LOCK_PATH == pathlib.Path(
        "/var/lib/kalshi-research-v3-locks/ephemeral-tagger.lock")


def test_tagger_user_id_mismatch_still_inactivates_deletes_and_confirms_zero(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(tagger_user_id="AIDAWRONGTAGGER00000")
    with pytest.raises(bootstrap.GateError, match="CALLER_IDENTITY_MISMATCH"):
        _execute(pins, tmp_path, monkeypatch, fake)
    actions = _actions(fake)
    assert actions[-5:] == ["list"] * 5
    assert actions[-7:-5] == ["inactive", "delete"]


def test_inactive_create_is_never_used_and_is_cleaned(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(create_status="Inactive")
    with pytest.raises(bootstrap.GateError, match="ACCESS_KEY_NOT_ACTIVE"):
        _execute(pins, tmp_path, monkeypatch, fake)
    assert "tagger" not in _actions(fake)
    actions = _actions(fake)
    assert actions[-5:] == ["list"] * 5
    assert actions[-7:-5] == ["inactive", "delete"]


def test_cleanup_retries_eventual_visibility_then_requires_consecutive_zero(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    fake = FakeAwsAndTagger(cleanup_lists=[
        [(KEY_ID, "Inactive")], [], [],
    ])
    _execute(pins, tmp_path, monkeypatch, fake)
    actions = _actions(fake)
    assert actions.count("inactive") == 2
    assert actions.count("delete") == 2
    assert actions[-2:] == ["list", "list"]


def test_cleanup_is_bounded_and_fails_without_two_zero_observations(
        monkeypatch, pins, tmp_path, bootstrap_credential_env):
    persistent = [[(KEY_ID, "Active")]
                  for _ in bootstrap.CLEANUP_BACKOFF_SECONDS]
    fake = FakeAwsAndTagger(cleanup_lists=persistent)
    with pytest.raises(bootstrap.GateError, match="CREDENTIAL_CLEANUP_FAILED"):
        _execute(pins, tmp_path, monkeypatch, fake)
    assert fake.list_count == 1 + len(bootstrap.CLEANUP_BACKOFF_SECONDS)


def test_exclusive_flock_rejects_second_writer(tmp_path):
    lock = tmp_path / "bootstrap.lock"
    with bootstrap._exclusive_lock(lock):
        with pytest.raises(bootstrap.GateError, match="BOOTSTRAP_ALREADY_RUNNING"):
            with bootstrap._exclusive_lock(lock):
                pytest.fail("second writer acquired lock")


def test_stable_exception_suppresses_sensitive_exception_chain(monkeypatch):
    with pytest.raises(bootstrap.GateError) as parsed:
        bootstrap._json_object("{bad", label="fixture")
    rendered = "".join(traceback.format_exception(
        type(parsed.value), parsed.value, parsed.value.__traceback__))
    assert parsed.value.__suppress_context__ is True
    assert "JSONDecodeError" not in rendered

    def fail_popen(*_args, **_kwargs):
        raise OSError("ambient-secret-must-not-escape")

    monkeypatch.setattr(bootstrap.subprocess, "Popen", fail_popen)
    with pytest.raises(bootstrap.GateError) as process:
        bootstrap._run_process(
            ["/bin/false"], env={}, timeout=1, label="fixture")
    rendered = "".join(traceback.format_exception(
        type(process.value), process.value, process.value.__traceback__))
    assert process.value.__suppress_context__ is True
    assert "ambient-secret" not in rendered


def test_main_requires_real_user_ids_and_service_isolation_gate(
        monkeypatch, pins, capsys):
    monkeypatch.setattr(bootstrap, "PRODUCTION_PINS", pins)
    monkeypatch.setattr(
        bootstrap, "execute_tagger",
        lambda *_a, **_kw: pytest.fail("isolation gate happened too late"),
    )
    assert bootstrap.main(_main_argv(
        pins, "--execute", "--operator-approved")) == 2
    assert "PROCESS_ISOLATION_GATE" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bootstrap.main(["--execute", "--", *_tagger_input(pins)])


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_child_signal_terminates_child_and_runs_cleanup(
        signum, tmp_path, pins):
    log = tmp_path / "events.log"
    child_pid = tmp_path / "child.pid"
    lock = tmp_path / "bootstrap.lock"
    harness = tmp_path / "harness.py"
    harness.write_text(f"""
import os
import pathlib
import sys
sys.path.insert(0, {str(ROOT / 'tools')!r})
import ephemeral_tagger_bootstrap as b

LOG = pathlib.Path({str(log)!r})
CHILD_PID = pathlib.Path({str(child_pid)!r})
def note(value):
    with LOG.open('a') as handle:
        handle.write(value + '\\n')
        handle.flush()
        os.fsync(handle.fileno())

class FakeAws:
    def __init__(self, executable, environment):
        self.environment = environment
    def caller_identity(self, env):
        if env.get('AWS_ACCESS_KEY_ID') == {BOOTSTRAP_KEY!r}:
            note('sts-bootstrap')
            return {{'Account': b.ACCOUNT, 'Arn': b.BOOTSTRAP_ARN,
                    'UserId': {BOOTSTRAP_USER_ID!r}}}
        note('sts-tagger')
        return {{'Account': b.ACCOUNT, 'Arn': b.TAGGER_ARN,
                'UserId': {TAGGER_USER_ID!r}}}
    def get_user(self):
        note('get-user')
        return {{'UserName': b.USER_NAME, 'Arn': b.TAGGER_ARN,
                'UserId': {TAGGER_USER_ID!r}}}
    def list_access_keys(self):
        note('list-zero')
        return ()
    def create_access_key(self):
        note('create')
        return b.EphemeralCredential({KEY_ID!r}, {SECRET!r}, 'Active')
    def update_access_key_inactive(self, access_key_id):
        assert access_key_id == {KEY_ID!r}
        note('inactive')
    def delete_access_key(self, access_key_id):
        assert access_key_id == {KEY_ID!r}
        note('delete')

b.AwsIamBootstrap = FakeAws
pins = b.RuntimePins(pathlib.Path(sys.executable), pathlib.Path(sys.executable),
                     b.TAGGER, production=False)
child = [sys.executable, '-c',
         "import os,pathlib,time; "
         "pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
         "time.sleep(60)"]
try:
    b.execute_tagger(child, pins=pins,
        bootstrap_user_id={BOOTSTRAP_USER_ID!r},
        tagger_user_id={TAGGER_USER_ID!r},
        lock_path=pathlib.Path({str(lock)!r}), sleep=lambda _x: None,
        harden_process=False, validate_self=False)
except b.BootstrapSignal as exc:
    raise SystemExit(128 + exc.signum)
""")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "AWS_ACCESS_KEY_ID": BOOTSTRAP_KEY,
        "AWS_SECRET_ACCESS_KEY": BOOTSTRAP_SECRET,
    }
    process = subprocess.Popen(
        [sys.executable, str(harness)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.time() + 8
    while time.time() < deadline and not child_pid.exists():
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert child_pid.exists(), process.communicate(timeout=2)
    pid = int(child_pid.read_text())
    os.kill(process.pid, signum)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 128 + signum, (stdout, stderr)
    events = log.read_text().splitlines()
    assert events.count("inactive") == 1
    assert events.count("delete") == 1
    assert events[-2:] == ["list-zero", "list-zero"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
