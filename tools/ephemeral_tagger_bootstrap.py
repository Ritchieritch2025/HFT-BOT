#!/usr/bin/env python3
"""Fail-closed ephemeral credential bootstrap for the canonical tagger.

The dedicated tagger IAM user must have no standing access key.  A live run
performs this bounded transaction under an exclusive host lock::

    exact vaultWriter STS identity
    -> ListAccessKeys == 0
    -> CreateAccessKey (response retained in memory only)
    -> exact tagger STS ARN *and UserId*
    -> canonical non-RFQ tagger
    -> finally Inactivate + Delete
    -> two consecutive ListAccessKeys == 0 observations

CHECK is the default and performs no subprocess or AWS call.  Live execution
requires explicit operator and dedicated-service-isolation attestations.

Linux hardening sets this process non-dumpable and starts children with
``no_new_privs``.  This is defense in depth, not complete same-UID process
isolation.  Production must run this helper as a dedicated service user with
systemd ``ProtectProc=invisible`` (and no unrelated process under that UID).
The attestation flag records that external requirement; the helper cannot
prove the entire service-manager boundary itself.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
TAGGER = (ROOT / "tools" / "canonical_eligibility_tagger.py").resolve()
USER_NAME = "canonical-eligibility-tagger"
TAGGER_ARN = "arn:aws:iam::321572485933:user/canonical-eligibility-tagger"
BOOTSTRAP_ARN = "arn:aws:iam::321572485933:user/vaultWriter"
ACCOUNT = "321572485933"
REGION = "us-east-2"
BUCKET = "kalshi-vault-ritcardo"
PREFIX = "ec2"
LOCK_PATH = pathlib.Path("/tmp/canonical-eligibility-tagger-bootstrap.lock")
MAX_AWS_RESPONSE_BYTES = 64 * 1024
MAX_TAGGER_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TAGGER_TIMEOUT = 4 * 60 * 60
ZERO_CONFIRMATIONS_REQUIRED = 2
ZERO_CONFIRMATION_NOT_BEFORE_SECONDS = 2.0
CLEANUP_BACKOFF_SECONDS = (0.0, 0.25, 0.75, 1.5, 3.0, 5.0, 8.0, 12.0)
ACCESS_KEY_ID_RE = re.compile(r"^[A-Z0-9]{16,128}$")
SECRET_ACCESS_KEY_RE = re.compile(r"^[A-Za-z0-9/+=]{20,128}$")
IAM_USER_ID_RE = re.compile(r"^[A-Z0-9]{16,128}$")
TRUSTED_PATH = "/snap/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38

TAGGER_VALUE_OPTIONS = frozenset({
    "--receipt-index",
    "--single-writer-audit",
    "--policy-evidence",
    "--bucket",
    "--prefix",
    "--output-root",
})
TAGGER_FLAG_OPTIONS = frozenset({"--operator-approved"})
TAGGER_REQUIRED_OPTIONS = frozenset({
    "--receipt-index",
    "--single-writer-audit",
    "--policy-evidence",
    "--output-root",
    "--operator-approved",
})
TAGGER_RFQ_OPTIONS = (
    "--include-sealed-rfq",
    "--rfq-eligibility-evidence",
)


class GateError(RuntimeError):
    """Stable fail-closed error whose detail contains no credential value."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class BootstrapSignal(BaseException):
    """SIGINT/SIGTERM converted to an exception so cleanup always runs."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class RuntimePins:
    aws_cli: pathlib.Path
    python: pathlib.Path
    tagger: pathlib.Path
    production: bool = True


PRODUCTION_PINS = RuntimePins(
    aws_cli=pathlib.Path("/snap/bin/aws"),
    python=pathlib.Path("/usr/bin/python3"),
    tagger=TAGGER,
    production=True,
)


@dataclass(repr=False)
class EphemeralCredential:
    access_key_id: str
    secret_access_key: str
    status: str

    def __repr__(self) -> str:
        return "EphemeralCredential(<redacted>)"


@dataclass(frozen=True)
class AccessKeyRecord:
    access_key_id: str
    status: str


@dataclass(frozen=True)
class TaggerResult:
    stdout: str
    stderr: str
    returncode: int


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _json_object(raw: str, *, label: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_AWS_RESPONSE_BYTES:
        raise GateError("AWS_RESPONSE_INVALID", f"{label} response is too large")
    try:
        value = json.loads(raw or "{}", object_pairs_hook=_reject_duplicate_pairs)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise GateError("AWS_RESPONSE_INVALID", f"{label} JSON is invalid") from None
    if not isinstance(value, dict):
        raise GateError("AWS_RESPONSE_INVALID", f"{label} root is not an object")
    return value


def _validate_user_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or IAM_USER_ID_RE.fullmatch(value) is None:
        raise GateError("IDENTITY_EVIDENCE_INVALID", f"{label} UserId is invalid")
    return value


def _sterile_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal AWS environment instead of filtering an ambient one."""
    ambient = os.environ if source is None else source
    env = {
        "PATH": TRUSTED_PATH,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CLI_HISTORY_FILE": "/dev/null",
        "AWS_CLI_HISTORY_ENABLED": "false",
        "AWS_CLI_AUTO_PROMPT": "off",
        "AWS_PAGER": "",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
    }
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        value = ambient.get(name)
        if value:
            env[name] = value
    if not env.get("AWS_ACCESS_KEY_ID") or not env.get("AWS_SECRET_ACCESS_KEY"):
        raise GateError(
            "BOOTSTRAP_CREDENTIALS_MISSING",
            "vaultWriter access-key environment is required",
        )
    return env


def _ephemeral_environment(base: dict[str, str],
                           credential: EphemeralCredential) -> dict[str, str]:
    env = {key: value for key, value in base.items()
           if key not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                          "AWS_SESSION_TOKEN")}
    env["AWS_ACCESS_KEY_ID"] = credential.access_key_id
    env["AWS_SECRET_ACCESS_KEY"] = credential.secret_access_key
    return env


def _prctl(option: int, value: int) -> bool:
    if not sys.platform.startswith("linux"):
        return True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(option, value, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def _harden_current_process() -> None:
    if not _prctl(PR_SET_DUMPABLE, 0):
        raise GateError(
            "PROCESS_HARDENING_FAILED", "could not set Linux dumpable=0")


def _child_hardening() -> None:
    """Async-child hook: fail closed before exec if prctl cannot be applied."""
    if sys.platform.startswith("linux"):
        if not _prctl(PR_SET_NO_NEW_PRIVS, 1) or not _prctl(PR_SET_DUMPABLE, 0):
            os._exit(126)


def _terminate_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _run_process(command: list[str], *, env: dict[str, str], timeout: int,
                 label: str, input_text: str | None = None,
                 max_output_bytes: int = MAX_AWS_RESPONSE_BYTES
                 ) -> subprocess.CompletedProcess:
    try:
        process = subprocess.Popen(
            command,
            env=env,
            cwd=str(ROOT),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            close_fds=True,
            start_new_session=True,
            preexec_fn=_child_hardening if sys.platform.startswith("linux") else None,
        )
    except (OSError, subprocess.SubprocessError):
        raise GateError("COMMAND_FAILED", f"{label} could not start") from None
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_child(process)
        raise GateError("COMMAND_TIMEOUT", f"{label} timed out") from None
    except BaseException:
        _terminate_child(process)
        raise
    stdout = stdout or ""
    stderr = stderr or ""
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > max_output_bytes:
        raise GateError("COMMAND_OUTPUT_INVALID", f"{label} output is too large")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _validate_executable(path: pathlib.Path, *, label: str,
                         production_system_binary: bool,
                         require_executable: bool = True) -> pathlib.Path:
    if not path.is_absolute():
        raise GateError("RUNTIME_PIN_INVALID", f"{label} path is not absolute")
    try:
        resolved = path.resolve(strict=True)
        item = resolved.stat()
    except OSError:
        raise GateError("RUNTIME_PIN_INVALID", f"{label} is unavailable") from None
    allowed_owner = 0 if production_system_binary else os.geteuid()
    if (not stat.S_ISREG(item.st_mode)
            or item.st_uid not in ({0} if production_system_binary
                                   else {0, allowed_owner})
            or item.st_mode & 0o022
            or (require_executable and not item.st_mode & stat.S_IXUSR)):
        raise GateError(
            "RUNTIME_PIN_INVALID", f"{label} ownership or mode is unsafe")
    return resolved


def validate_runtime_pins(pins: RuntimePins) -> None:
    if pins.production and pins != PRODUCTION_PINS:
        raise GateError("RUNTIME_PIN_INVALID", "production pins may not be overridden")
    _validate_executable(
        pins.aws_cli, label="AWS CLI",
        production_system_binary=pins.production)
    _validate_executable(
        pins.python, label="Python",
        production_system_binary=pins.production)
    resolved_tagger = _validate_executable(
        pins.tagger, label="canonical tagger",
        production_system_binary=False, require_executable=False)
    if resolved_tagger != TAGGER:
        raise GateError(
            "RUNTIME_PIN_INVALID", "canonical tagger realpath is not pinned")


def validate_self_runtime(pins: RuntimePins) -> None:
    """Require the bootstrap itself to run under pinned isolated Python."""
    try:
        actual = pathlib.Path(sys.executable).resolve(strict=True)
        expected = pins.python.resolve(strict=True)
    except OSError:
        raise GateError(
            "RUNTIME_PIN_INVALID", "bootstrap Python realpath is unavailable") from None
    if actual != expected:
        raise GateError(
            "RUNTIME_PIN_INVALID", "bootstrap is not using pinned Python")
    if not sys.flags.isolated:
        raise GateError(
            "RUNTIME_PIN_INVALID", "bootstrap Python must be launched with -I")


def _looks_like_rfq_option(option: str) -> bool:
    lowered = option.lower()
    return ("rfq" in lowered
            or any(forbidden.startswith(lowered)
                   for forbidden in TAGGER_RFQ_OPTIONS))


def _strict_tagger_arguments(arguments: Iterable[str]) -> list[str]:
    tokens = list(arguments)
    normalized = []
    seen = set()
    values = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(token, str) or not token.startswith("--"):
            raise GateError(
                "TAGGER_ARGUMENT_INVALID", "positional/short tagger arguments are forbidden")
        option, separator, attached = token.partition("=")
        if _looks_like_rfq_option(option):
            raise GateError("RFQ_GATE", "RFQ tagger options are forbidden")
        if option in seen:
            raise GateError("TAGGER_ARGUMENT_INVALID", f"duplicate {option}")
        if option in TAGGER_FLAG_OPTIONS:
            if separator:
                raise GateError(
                    "TAGGER_ARGUMENT_INVALID", f"{option} does not accept a value")
            normalized.append(option)
            seen.add(option)
            index += 1
            continue
        if option not in TAGGER_VALUE_OPTIONS:
            raise GateError(
                "TAGGER_ARGUMENT_INVALID", f"tagger option {option!r} is not allowlisted")
        if separator:
            value = attached
        else:
            index += 1
            if index >= len(tokens):
                raise GateError(
                    "TAGGER_ARGUMENT_INVALID", f"{option} value is missing")
            value = tokens[index]
        if not value or value.startswith("-"):
            raise GateError(
                "TAGGER_ARGUMENT_INVALID", f"{option} value is invalid")
        normalized.extend([option, value])
        values[option] = value
        seen.add(option)
        index += 1
    missing = sorted(TAGGER_REQUIRED_OPTIONS - seen)
    if missing:
        raise GateError(
            "TAGGER_ARGUMENT_INVALID", "required tagger options are missing")
    if values.get("--bucket", BUCKET) != BUCKET:
        raise GateError("TAGGER_ARGUMENT_INVALID", "tagger bucket is fixed")
    if values.get("--prefix", PREFIX) != PREFIX:
        raise GateError("TAGGER_ARGUMENT_INVALID", "tagger prefix is fixed")
    return normalized


def validate_tagger_command(command: Iterable[str],
                            pins: RuntimePins = PRODUCTION_PINS) -> list[str]:
    """Require pinned Python + exact script + a non-RFQ argument allowlist."""
    normalized = list(command)
    if normalized and normalized[0] == "--":
        normalized.pop(0)
    if len(normalized) < 2:
        raise GateError("TAGGER_COMMAND_INVALID", "tagger command is incomplete")
    # Direct shebang execution is forbidden.  The lexical paths are pinned and
    # their real files are checked again immediately before live execution.
    if pathlib.Path(normalized[0]).absolute() != pins.python.absolute():
        raise GateError("TAGGER_COMMAND_INVALID", "Python path is not pinned")
    if pathlib.Path(normalized[1]).absolute() != pins.tagger.absolute():
        raise GateError("TAGGER_COMMAND_INVALID", "tagger path is not pinned")
    arguments = _strict_tagger_arguments(normalized[2:])
    return [
        # ``-I`` is intentionally not used for the tagger: isolated mode
        # removes the script directory from sys.path and breaks its pinned
        # sibling imports.  ``-E -s`` ignores PYTHON* injection and user-site
        # packages while retaining the exact script directory.
        str(pins.python), "-E", "-s", str(pins.tagger), *arguments,
        "--aws-cli", str(pins.aws_cli),
    ]


def _validate_exact_identity(value: dict, *, expected_arn: str,
                             expected_user_id: str, label: str) -> None:
    if (value.get("Arn") != expected_arn
            or value.get("Account") != ACCOUNT
            or value.get("UserId") != expected_user_id):
        raise GateError(
            "CALLER_IDENTITY_MISMATCH", f"{label} identity does not match evidence")


def _redact(text: str, credential: EphemeralCredential | None) -> str:
    safe = text or ""
    if credential is not None:
        for value in (credential.access_key_id, credential.secret_access_key):
            safe = safe.replace(value, "[REDACTED_EPHEMERAL_CREDENTIAL]")
    return safe


class AwsIamBootstrap:
    """AWS CLI adapter which never emits AWS response bodies."""

    def __init__(self, executable: pathlib.Path, environment: dict[str, str]):
        self.executable = str(executable)
        self.environment = environment

    def _json(self, arguments: list[str], *, label: str,
              environment: dict[str, str] | None = None,
              input_payload: dict | None = None) -> dict:
        command = [self.executable, *arguments, "--output", "json"]
        input_text = None
        if input_payload is not None:
            command.extend(["--cli-input-json", "file:///dev/stdin"])
            input_text = json.dumps(
                input_payload, sort_keys=True, separators=(",", ":"))
        result = _run_process(
            command,
            env=self.environment if environment is None else environment,
            timeout=60,
            label=label,
            input_text=input_text,
        )
        if result.returncode:
            raise GateError(
                "AWS_COMMAND_FAILED", f"{label} rc={result.returncode}")
        return _json_object(result.stdout or "{}", label=label)

    def list_access_keys(self) -> tuple[AccessKeyRecord, ...]:
        value = self._json([
            "iam", "list-access-keys", "--user-name", USER_NAME,
            "--no-paginate",
        ], label="ListAccessKeys")
        rows = value.get("AccessKeyMetadata")
        if not isinstance(rows, list) or value.get("IsTruncated") not in (None, False):
            raise GateError(
                "ACCESS_KEY_LIST_INVALID", "ListAccessKeys is incomplete")
        records = []
        for row in rows:
            if (not isinstance(row, dict)
                    or row.get("UserName") != USER_NAME
                    or row.get("Status") not in ("Active", "Inactive")
                    or not isinstance(row.get("AccessKeyId"), str)
                    or ACCESS_KEY_ID_RE.fullmatch(row["AccessKeyId"]) is None):
                raise GateError(
                    "ACCESS_KEY_LIST_INVALID", "metadata row is invalid")
            records.append(AccessKeyRecord(row["AccessKeyId"], row["Status"]))
        identifiers = [row.access_key_id for row in records]
        if len(identifiers) != len(set(identifiers)) or len(records) > 2:
            raise GateError("ACCESS_KEY_LIST_INVALID", "metadata set is invalid")
        return tuple(records)

    def create_access_key(self) -> EphemeralCredential:
        value = self._json([
            "iam", "create-access-key", "--user-name", USER_NAME,
        ], label="CreateAccessKey")
        row = value.get("AccessKey")
        if (not isinstance(row, dict)
                or row.get("UserName") != USER_NAME
                or row.get("Status") not in ("Active", "Inactive")
                or not isinstance(row.get("AccessKeyId"), str)
                or ACCESS_KEY_ID_RE.fullmatch(row["AccessKeyId"]) is None
                or not isinstance(row.get("SecretAccessKey"), str)
                or SECRET_ACCESS_KEY_RE.fullmatch(row["SecretAccessKey"]) is None):
            raise GateError(
                "ACCESS_KEY_CREATE_INVALID", "CreateAccessKey response is invalid")
        return EphemeralCredential(
            access_key_id=row["AccessKeyId"],
            secret_access_key=row["SecretAccessKey"],
            status=row["Status"],
        )

    def update_access_key_inactive(self, access_key_id: str) -> None:
        self._json(
            ["iam", "update-access-key"],
            label="UpdateAccessKeyInactive",
            input_payload={
                "UserName": USER_NAME,
                "AccessKeyId": access_key_id,
                "Status": "Inactive",
            },
        )

    def delete_access_key(self, access_key_id: str) -> None:
        self._json(
            ["iam", "delete-access-key"],
            label="DeleteAccessKey",
            input_payload={
                "UserName": USER_NAME,
                "AccessKeyId": access_key_id,
            },
        )

    def caller_identity(self, environment: dict[str, str]) -> dict:
        return self._json(
            ["sts", "get-caller-identity"],
            label="GetCallerIdentity",
            environment=environment,
        )


class _SignalGuard:
    def __init__(self):
        self.original = {}
        self.cleaning = False
        self.pending: int | None = None

    def _handler(self, signum, _frame):
        if self.cleaning:
            if self.pending is None:
                self.pending = signum
            return
        raise BootstrapSignal(signum)

    def __enter__(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.original[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handler)
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        for signum, handler in self.original.items():
            signal.signal(signum, handler)
        return False


@contextlib.contextmanager
def _exclusive_lock(path: pathlib.Path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode)
                or item.st_uid != os.geteuid()
                or item.st_mode & 0o077):
            raise GateError("LOCK_INVALID", "bootstrap lock ownership/mode is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except GateError:
        if "fd" in locals():
            os.close(fd)
        raise
    except (OSError, BlockingIOError):
        if "fd" in locals():
            os.close(fd)
        raise GateError("BOOTSTRAP_ALREADY_RUNNING", "exclusive lock unavailable") from None
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _cleanup(aws: AwsIamBootstrap, credential: EphemeralCredential | None,
             *, create_attempted: bool,
             sleep: Callable[[float], None] = time.sleep) -> None:
    if not create_attempted:
        return
    pending = ({credential.access_key_id} if credential is not None else set())
    zero_streak = 0
    elapsed_backoff = 0.0
    for delay in CLEANUP_BACKOFF_SECONDS:
        if delay:
            sleep(delay)
            elapsed_backoff += delay
        for access_key_id in sorted(pending):
            try:
                aws.update_access_key_inactive(access_key_id)
            except GateError:
                pass
            try:
                aws.delete_access_key(access_key_id)
            except GateError:
                pass
        pending = set()
        try:
            records = aws.list_access_keys()
        except GateError:
            zero_streak = 0
            continue
        if not records:
            # Two immediately empty reads are not sufficient after an
            # uncertain CreateAccessKey response.  Let the bounded IAM
            # consistency window elapse before counting final observations.
            if elapsed_backoff < ZERO_CONFIRMATION_NOT_BEFORE_SECONDS:
                zero_streak = 0
                continue
            zero_streak += 1
            if zero_streak >= ZERO_CONFIRMATIONS_REQUIRED:
                return
            continue
        zero_streak = 0
        pending = {record.access_key_id for record in records}
    raise GateError(
        "CREDENTIAL_CLEANUP_FAILED",
        "bounded cleanup did not observe two consecutive zero-key lists",
    ) from None


def execute_tagger(command: list[str], *, pins: RuntimePins,
                   bootstrap_user_id: str, tagger_user_id: str,
                   tagger_timeout: int = DEFAULT_TAGGER_TIMEOUT,
                   lock_path: pathlib.Path = LOCK_PATH,
                   sleep: Callable[[float], None] = time.sleep,
                   harden_process: bool = True,
                   validate_self: bool = True) -> TaggerResult:
    """Run the live transaction after caller-side authorization gates."""
    bootstrap_user_id = _validate_user_id(
        bootstrap_user_id, label="vaultWriter")
    tagger_user_id = _validate_user_id(tagger_user_id, label="tagger")
    validate_runtime_pins(pins)
    if validate_self:
        validate_self_runtime(pins)
    if harden_process:
        _harden_current_process()
    bootstrap_env = _sterile_environment()
    aws = AwsIamBootstrap(pins.aws_cli, bootstrap_env)
    credential = None
    ephemeral_env = None
    create_attempted = False
    tagger_result = None
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None

    with _exclusive_lock(lock_path):
        guard = _SignalGuard()
        with guard:
            try:
                bootstrap_identity = aws.caller_identity(bootstrap_env)
                _validate_exact_identity(
                    bootstrap_identity,
                    expected_arn=BOOTSTRAP_ARN,
                    expected_user_id=bootstrap_user_id,
                    label="bootstrap",
                )
                initial = aws.list_access_keys()
                if initial:
                    raise GateError(
                        "STANDING_ACCESS_KEYS_REFUSED",
                        f"initial tagger access-key count is {len(initial)}, expected 0",
                    )
                create_attempted = True
                credential = aws.create_access_key()
                if credential.status != "Active":
                    raise GateError(
                        "ACCESS_KEY_NOT_ACTIVE", "created key is not Active")
                ephemeral_env = _ephemeral_environment(bootstrap_env, credential)
                tagger_identity = aws.caller_identity(ephemeral_env)
                _validate_exact_identity(
                    tagger_identity,
                    expected_arn=TAGGER_ARN,
                    expected_user_id=tagger_user_id,
                    label="tagger",
                )
                result = _run_process(
                    command,
                    env=ephemeral_env,
                    timeout=tagger_timeout,
                    label="canonical eligibility tagger",
                    max_output_bytes=MAX_TAGGER_OUTPUT_BYTES,
                )
                tagger_result = TaggerResult(
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                    returncode=result.returncode,
                )
                if result.returncode:
                    detail = _redact(
                        (result.stderr or result.stdout or "").strip()[-2000:],
                        credential,
                    )
                    raise GateError(
                        "TAGGER_COMMAND_FAILED",
                        f"canonical tagger rc={result.returncode}: "
                        f"{detail or 'no detail'}",
                    )
            except BaseException as exc:
                primary_error = exc
            finally:
                guard.cleaning = True
                try:
                    _cleanup(
                        aws, credential, create_attempted=create_attempted,
                        sleep=sleep)
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    if ephemeral_env is not None:
                        ephemeral_env.clear()
                    guard.cleaning = False

        if cleanup_error is not None:
            if isinstance(cleanup_error, GateError):
                raise cleanup_error from None
            raise GateError(
                "CREDENTIAL_CLEANUP_FAILED", "unexpected cleanup failure") from None
        if primary_error is None and guard.pending is not None:
            primary_error = BootstrapSignal(guard.pending)
        if primary_error is not None:
            raise primary_error from None
    if tagger_result is None:
        raise GateError("TAGGER_COMMAND_FAILED", "tagger returned no result")
    return TaggerResult(
        stdout=_redact(tagger_result.stdout, credential),
        stderr=_redact(tagger_result.stderr, credential),
        returncode=tagger_result.returncode,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument("--dedicated-service-isolation-attested",
                        action="store_true")
    parser.add_argument("--bootstrap-user-id", required=True)
    parser.add_argument("--tagger-user-id", required=True)
    parser.add_argument("--tagger-timeout", type=int,
                        default=DEFAULT_TAGGER_TIMEOUT)
    parser.add_argument("tagger_command", nargs=argparse.REMAINDER)
    return parser


def _plan(mode: str, command: list[str]) -> dict:
    digest = hashlib.sha256(
        json.dumps(command, ensure_ascii=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "state": "EPHEMERAL_TAGGER_BOOTSTRAP_PLAN",
        "mode": mode,
        "bootstrap_arn": BOOTSTRAP_ARN,
        "tagger_arn": TAGGER_ARN,
        "initial_access_keys_required": 0,
        "final_zero_observations_required": ZERO_CONFIRMATIONS_REQUIRED,
        "zero_confirmation_not_before_seconds":
            ZERO_CONFIRMATION_NOT_BEFORE_SECONDS,
        "credential_storage": "MEMORY_ONLY",
        "aws_cli": str(PRODUCTION_PINS.aws_cli),
        "python": str(PRODUCTION_PINS.python),
        "bootstrap_python_mode": "ISOLATED_-I_REQUIRED",
        "tagger_python_mode": "-E_-s_PINNED_SCRIPT",
        "rfq": "OFF",
        "same_uid_process_isolation": "DEDICATED_UID_AND_PROTECTPROC_REQUIRED",
        "tagger_command_sha256": digest,
        "aws_mutations": 0 if mode in ("CHECK", "DRY_RUN") else "GATED",
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bootstrap_user_id = _validate_user_id(
            args.bootstrap_user_id, label="vaultWriter")
        tagger_user_id = _validate_user_id(
            args.tagger_user_id, label="tagger")
        if args.tagger_timeout <= 0:
            raise GateError("ARGUMENT_INVALID", "--tagger-timeout must be positive")
        command = validate_tagger_command(
            args.tagger_command, pins=PRODUCTION_PINS)
        mode = "RUN" if args.execute else "DRY_RUN" if args.dry_run else "CHECK"
        print(json.dumps(_plan(mode, command), sort_keys=True))
        if not args.execute:
            return 0
        if not args.operator_approved:
            raise GateError(
                "OPERATOR_GATE", "--execute requires --operator-approved")
        if not args.dedicated_service_isolation_attested:
            raise GateError(
                "PROCESS_ISOLATION_GATE",
                "dedicated service UID and ProtectProc attestation is required",
            )
        result = execute_tagger(
            command,
            pins=PRODUCTION_PINS,
            bootstrap_user_id=bootstrap_user_id,
            tagger_user_id=tagger_user_id,
            tagger_timeout=args.tagger_timeout,
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stdout.write("\n")
        if result.stderr:
            sys.stderr.write(result.stderr)
            if not result.stderr.endswith("\n"):
                sys.stderr.write("\n")
        print(json.dumps({
            "state": "EPHEMERAL_TAGGER_COMMAND_COMPLETE",
            "tagger_arn": TAGGER_ARN,
            "initial_access_keys": 0,
            "final_zero_observations": ZERO_CONFIRMATIONS_REQUIRED,
            "credential_storage": "MEMORY_ONLY",
            "rfq": "OFF",
        }, sort_keys=True))
        return 0
    except BootstrapSignal as exc:
        print(
            f"SIGNAL_CLEANUP_COMPLETE: signal={exc.signum}; "
            "two consecutive zero-key observations complete",
            file=sys.stderr,
        )
        return 128 + exc.signum
    except GateError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
