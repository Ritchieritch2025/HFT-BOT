#!/usr/bin/env python3
"""Drive future canonical generation witnesses from sealed producer metadata.

This coordinator is deliberately small and publisher-only.  It scans bounded
producer dim-generation manifests, launches one isolated witness process per
eligible sealed date, and records a local terminal marker only after the child
returns an exact version-bound witness.  A failure for one date is reported but
does not prevent later dates from running.

The systemd credential is parsed as data; it is never sourced by a shell.
No RFQ input, research computation, object tagging, copy, or delete operation
is available from this coordinator.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys

import canonical_receipts as cr


ROOT = pathlib.Path(__file__).resolve().parents[1]
WITNESS_TOOL = ROOT / "tools" / "canonical_generation_witness.py"
PYTHON = ROOT / ".venv" / "bin" / "python"
RAW_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/raw")
WAREHOUSE_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/warehouse")
QUALITY_DIR = pathlib.Path("/home/ubuntu/hft-bot/work/event_packs")
DIM_MANIFEST_ROOT = WAREHOUSE_ROOT / ".publication-generations" / "dim"
STATE_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/canonical_receipts/generation-daily")
AUTHORIZATION_FILE = pathlib.Path(
    "/etc/kalshi-research-v3/approvals/cutover-approved")
PUBLISHER_ENV_FILE = pathlib.Path(
    "/run/credentials/kalshi-canonical-generation-witness.service/"
    "publisher.env")
LEGACY_PUBLISHER_ENV_FILE = pathlib.Path(
    "/run/credentials/kalshi-canonical-generation-legacy.service/"
    "publisher.env")
AWS_CLI = "/snap/aws-cli/current/bin/aws"
PUBLISHER_ARN = "arn:aws:iam::321572485933:user/vaultWriter"
FIRST_FUTURE_DATE = "2026-07-17"
PRODUCER_GENERATION_AUTHORITY = "PRODUCER_COHERENT"
LEGACY_GENERATION_AUTHORITY = "LEGACY_MIGRATION_GENERATION"
LEGACY_DATES = frozenset({
    "2026-07-10", "2026-07-11", "2026-07-15", "2026-07-16"})
LEGACY_PROOF_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/canonical_receipts/"
    "generation-migration-proofs")
MAX_MANIFESTS = 4096
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_CHILD_SECONDS = 24 * 60 * 60
RECENT_RECHECK_DAYS = 7
RECENT_RECHECK_INTERVAL = dt.timedelta(hours=24)
AUTHORIZATION_SHA256 = (
    "1b14001428f2387f3e62c531a8d8ce3dd4f8bd726d6c8b94b4d6c0d893020761")
DATE_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})\.json")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
VERSION_RE = re.compile(r"[^\x00-\x20]+")
ALLOWED_AWS_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
}


class DailyWitnessError(RuntimeError):
    """One stable fail-closed coordinator refusal."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fingerprint(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
            item.st_ctime_ns)


def _read_regular(path: pathlib.Path, limit: int, label: str) -> bytes:
    """Read one stable regular file without following its final symlink."""
    path = pathlib.Path(path).absolute()
    if not hasattr(os, "O_NOFOLLOW"):
        raise DailyWitnessError(
            "LOCAL_SAFETY_UNAVAILABLE", "O_NOFOLLOW is required")
    try:
        fd = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise DailyWitnessError(
            "LOCAL_INPUT_INVALID", f"{label}: {exc}") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            raise DailyWitnessError(
                "LOCAL_INPUT_INVALID", f"{label} size/type invalid")
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (total > limit or total != before.st_size
                or _fingerprint(before) != _fingerprint(after)
                or stat.S_ISLNK(named.st_mode)
                or (named.st_dev, named.st_ino)
                != (after.st_dev, after.st_ino)):
            raise DailyWitnessError("LOCAL_INPUT_CHANGED", label)
        return b"".join(chunks)
    except OSError as exc:
        raise DailyWitnessError("LOCAL_INPUT_CHANGED", f"{label}: {exc}") \
            from exc
    finally:
        os.close(fd)


def _secure_publisher_file(
        path: pathlib.Path, *, legacy: bool = False) -> pathlib.Path:
    path = pathlib.Path(path)
    expected = LEGACY_PUBLISHER_ENV_FILE if legacy else PUBLISHER_ENV_FILE
    if path != expected:
        raise DailyWitnessError(
            "CREDENTIAL_FILE_INVALID", "publisher credential path is fixed")
    try:
        item = os.stat(path, follow_symlinks=False)
        parent = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise DailyWitnessError("CREDENTIAL_FILE_INVALID", str(exc)) from exc
    file_mode = stat.S_IMODE(item.st_mode)
    parent_mode = stat.S_IMODE(parent.st_mode)
    # systemd 255 exposes encrypted credentials as root:root 0440 inside a
    # root:root 0550 private mount.  Unit tests/direct invocations use a
    # caller-owned 0600 file inside 0700.  Both shapes are non-writable by
    # every untrusted principal; no broader 0640/0750 form is accepted.
    systemd_shape = (
        item.st_uid == item.st_gid == 0 and file_mode in {0o400, 0o440}
        and parent.st_uid == parent.st_gid == 0
        and parent_mode in {0o500, 0o550})
    private_shape = (
        item.st_uid in {0, os.geteuid()} and file_mode == 0o600
        and parent.st_uid in {0, os.geteuid()} and parent_mode == 0o700)
    if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or item.st_size <= 0 or item.st_size > MAX_CREDENTIAL_BYTES
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or not (systemd_shape or private_shape)):
        raise DailyWitnessError(
            "CREDENTIAL_FILE_INVALID",
            "publisher credential/parent ownership or mode invalid")
    return path


def publisher_environment(
        path: pathlib.Path, *, legacy: bool = False) -> dict[str, str]:
    """Return a sterile environment from simple allowlisted assignments."""
    raw = _read_regular(
        _secure_publisher_file(path, legacy=legacy), MAX_CREDENTIAL_BYTES,
        "publisher credential")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DailyWitnessError(
            "CREDENTIAL_FILE_INVALID", "publisher credential is not UTF-8") \
            from exc
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise DailyWitnessError(
                "CREDENTIAL_FILE_INVALID", f"line {number}: {exc}") from exc
        if not tokens:
            continue
        if tokens[0] == "export":
            tokens = tokens[1:]
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise DailyWitnessError(
                "CREDENTIAL_FILE_INVALID",
                f"line {number} is not one assignment")
        key, value = tokens[0].split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise DailyWitnessError(
                "CREDENTIAL_FILE_INVALID", f"line {number} key invalid")
        if key.startswith("AWS_") and key not in ALLOWED_AWS_KEYS:
            raise DailyWitnessError(
                "CREDENTIAL_FILE_INVALID",
                f"line {number} sets forbidden {key}")
        if key in ALLOWED_AWS_KEYS:
            if any(marker in value for marker in ("$", "`", "\x00")):
                raise DailyWitnessError(
                    "CREDENTIAL_FILE_INVALID",
                    f"line {number} contains expansion syntax")
            values[key] = value
    if (not values.get("AWS_ACCESS_KEY_ID")
            or not values.get("AWS_SECRET_ACCESS_KEY")):
        raise DailyWitnessError(
            "CREDENTIAL_FILE_INVALID", "publisher keys are missing")
    values.setdefault("AWS_DEFAULT_REGION", "us-east-2")
    values.setdefault("AWS_REGION", values["AWS_DEFAULT_REGION"])
    return {
        "HOME": "/nonexistent",
        "USER": "canonical-generation-witness",
        "LOGNAME": "canonical-generation-witness",
        "PATH": "/snap/aws-cli/current/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "AWS_PAGER": "",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CLI_HISTORY_FILE": "/dev/null",
        "AWS_CLI_HISTORY_ENABLED": "false",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_CLI_AUTO_PROMPT": "off",
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        **values,
    }


def _date_from_manifest(path: pathlib.Path) -> str:
    match = DATE_RE.fullmatch(path.name)
    if match is None or path.parent != DIM_MANIFEST_ROOT:
        raise DailyWitnessError("MANIFEST_PATH_INVALID", str(path))
    value = match.group(1)
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise DailyWitnessError("MANIFEST_PATH_INVALID", str(path)) from exc
    if parsed.isoformat() != value:
        raise DailyWitnessError("MANIFEST_PATH_INVALID", str(path))
    return value


def eligible_dates(today: dt.date | None = None) -> list[str]:
    """Return sealed future dates; malformed control entries fail closed."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        root = DIM_MANIFEST_ROOT.resolve(strict=True)
        expected = DIM_MANIFEST_ROOT.absolute()
    except OSError as exc:
        raise DailyWitnessError("MANIFEST_ROOT_INVALID", str(exc)) from exc
    if root != expected or not root.is_dir():
        raise DailyWitnessError("MANIFEST_ROOT_INVALID", str(root))
    entries = list(root.iterdir())
    if len(entries) > MAX_MANIFESTS:
        raise DailyWitnessError(
            "MANIFEST_SCAN_BOUNDED", f"more than {MAX_MANIFESTS} entries")
    dates: list[str] = []
    for path in entries:
        if path.name.startswith("."):
            raise DailyWitnessError("MANIFEST_PATH_INVALID", str(path))
        date = _date_from_manifest(path)
        item = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode):
            raise DailyWitnessError("MANIFEST_PATH_INVALID", str(path))
        if date < FIRST_FUTURE_DATE or date >= today.isoformat():
            continue
        seal = WAREHOUSE_ROOT / "seals" / f"date={date}.json"
        try:
            seal_item = os.stat(seal, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(seal_item.st_mode)
                or stat.S_ISLNK(seal_item.st_mode)):
            raise DailyWitnessError("SEAL_PATH_INVALID", str(seal))
        dates.append(date)
    if len(dates) != len(set(dates)):
        raise DailyWitnessError("MANIFEST_PATH_INVALID", "duplicate dates")
    return sorted(dates)


def _marker_path(date: str) -> pathlib.Path:
    return STATE_ROOT / f"date={date}.json"


def _exact_witness_binding(witness: object, date: str) -> bool:
    if not isinstance(witness, dict):
        return False
    digest = str(witness.get("sha256") or "")
    expected_key = (
        "ec2/control/publication-generations/v1/"
        f"date={date}/witness-{digest}.json")
    version = str(witness.get("VersionId") or "")
    return (
        witness.get("bucket") == "kalshi-vault-ritcardo"
        and witness.get("key") == expected_key
        and HEX64_RE.fullmatch(digest) is not None
        and VERSION_RE.fullmatch(version) is not None
        and version.lower() != "null"
        and isinstance(witness.get("size"), int)
        and not isinstance(witness.get("size"), bool)
        and witness["size"] > 0
    )


def _terminal_result(payload: object, date: str) -> bool:
    if not isinstance(payload, dict):
        return False
    witness = payload.get("witness_object")
    return (
        payload.get("state") == "PRODUCER_GENERATION_WITNESS_READY"
        and payload.get("date") == date
        and payload.get("generation_authority")
        == PRODUCER_GENERATION_AUTHORITY
        and payload.get("catalog_dim_coherence_claim") is True
        and payload.get("rfq") == "OFF"
        and re.fullmatch(
            r"[0-9a-f]{40}",
            str(payload.get("publisher_code_commit") or "")) is not None
        and payload.get("authorization_sha256") == AUTHORIZATION_SHA256
        and _exact_witness_binding(witness, date)
    )


def _read_terminal_marker(date: str) -> dict | None:
    path = _marker_path(date)
    if not os.path.lexists(path):
        return None
    try:
        raw = _read_regular(path, 8 * 1024 * 1024, "terminal marker")
        payload = json.loads(raw)
    except (DailyWitnessError, UnicodeDecodeError, ValueError) as exc:
        raise DailyWitnessError("TERMINAL_MARKER_INVALID", str(exc)) from exc
    if (not isinstance(payload, dict) or set(payload) != {
            "schema_version", "state", "date", "cached_at_utc",
            "result_sha256", "result"}
            or payload.get("schema_version")
            != "canonical-generation-daily-marker-v1"
            or payload.get("state")
            != "LOCAL_SCHEDULER_CACHE_NOT_REMOTE_AUTHORITY"
            or payload.get("date") != date
            or not isinstance(payload.get("cached_at_utc"), str)
            or not payload["cached_at_utc"].endswith("Z")
            or HEX64_RE.fullmatch(str(payload.get("result_sha256") or ""))
            is None
            or payload["result_sha256"] != cr.canonical_sha256(
                payload.get("result"))
            or not _terminal_result(payload.get("result"), date)):
        raise DailyWitnessError("TERMINAL_MARKER_INVALID", str(path))
    return payload


def _write_terminal_marker(
        date: str, payload: dict, *, now: dt.datetime | None = None) \
        -> pathlib.Path:
    if not _terminal_result(payload, date):
        raise DailyWitnessError("CHILD_RESULT_INVALID", date)
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise DailyWitnessError("CLOCK_INVALID", "UTC-aware time required")
    if os.path.lexists(STATE_ROOT) and STATE_ROOT.is_symlink():
        raise DailyWitnessError("TERMINAL_MARKER_WRITE_FAILED", str(STATE_ROOT))
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = _marker_path(date)
    if os.path.lexists(path):
        _read_terminal_marker(date)
    marker = {
        "schema_version": "canonical-generation-daily-marker-v1",
        "state": "LOCAL_SCHEDULER_CACHE_NOT_REMOTE_AUTHORITY",
        "date": date,
        "cached_at_utc": now.astimezone(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "result_sha256": cr.canonical_sha256(payload),
        "result": payload,
    }
    try:
        cr.write_atomic_json(path, marker)
        os.chmod(path, 0o600, follow_symlinks=False)
    except (OSError, cr.ReceiptError) as exc:
        raise DailyWitnessError("TERMINAL_MARKER_WRITE_FAILED", str(exc)) \
            from exc
    return path


def _marker_needs_recheck(
        marker: dict, date: str, today: dt.date, now: dt.datetime) -> bool:
    parsed_date = dt.date.fromisoformat(date)
    if parsed_date < today - dt.timedelta(days=RECENT_RECHECK_DAYS):
        return False
    try:
        cached = dt.datetime.fromisoformat(
            marker["cached_at_utc"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise DailyWitnessError("TERMINAL_MARKER_INVALID", date) from exc
    return now - cached >= RECENT_RECHECK_INTERVAL


def _run_date(date: str, environment: dict[str, str]) -> dict:
    command = [
        str(PYTHON), str(WITNESS_TOOL), "publish-future",
        "--date", date,
        "--raw-root", str(RAW_ROOT),
        "--warehouse-root", str(WAREHOUSE_ROOT),
        "--publisher-principal", PUBLISHER_ARN,
        "--aws-cli", AWS_CLI,
        "--authorization-file", str(AUTHORIZATION_FILE),
        "--operator-approved",
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True,
            text=True, shell=False, timeout=MAX_CHILD_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DailyWitnessError("CHILD_EXEC_FAILED", f"{date}: {exc}") \
            from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise DailyWitnessError(
            "DATE_WITNESS_FAILED",
            f"{date} rc={result.returncode}: {detail}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DailyWitnessError("CHILD_RESULT_INVALID", date)
    try:
        payload = json.loads(lines[0])
    except ValueError as exc:
        raise DailyWitnessError("CHILD_RESULT_INVALID", date) from exc
    if not _terminal_result(payload, date):
        raise DailyWitnessError("CHILD_RESULT_INVALID", date)
    return payload


def _legacy_result(payload: object, date: str) -> bool:
    if not isinstance(payload, dict):
        return False
    witness = payload.get("witness_object")
    return (
        payload.get("state") == "LEGACY_GENERATION_WITNESS_READY"
        and payload.get("date") == date
        and payload.get("generation_authority")
        == LEGACY_GENERATION_AUTHORITY
        and payload.get("catalog_dim_coherence_claim") is False
        and payload.get("rfq") == "OFF"
        and payload.get("original_object_puts") == 0
        and payload.get("research_prefix_writes") == 0
        and payload.get("copy_operations") == 0
        and payload.get("tag_writes") == 0
        and re.fullmatch(
            r"[0-9a-f]{40}",
            str(payload.get("publisher_code_commit") or "")) is not None
        and payload.get("authorization_sha256") == AUTHORIZATION_SHA256
        and HEX64_RE.fullmatch(
            str(payload.get("history_proof_sha256") or "")) is not None
        and HEX64_RE.fullmatch(
            str(payload.get(
                "historical_metadata_authority_sha256") or "")) is not None
        and _exact_witness_binding(witness, date)
    )


def run_legacy(date: str, *,
               credential_file: pathlib.Path = LEGACY_PUBLISHER_ENV_FILE) \
        -> dict:
    if date not in LEGACY_DATES:
        raise DailyWitnessError(
            "LEGACY_DATE_FORBIDDEN", "legacy migration date is not allowed")
    environment = publisher_environment(credential_file, legacy=True)
    command = [
        str(PYTHON), str(WITNESS_TOOL), "migrate-legacy",
        "--date", date,
        "--raw-root", str(RAW_ROOT),
        "--warehouse-root", str(WAREHOUSE_ROOT),
        "--quality-dir", str(QUALITY_DIR),
        "--proof-root", str(LEGACY_PROOF_ROOT),
        "--publisher-principal", PUBLISHER_ARN,
        "--aws-cli", AWS_CLI,
        "--authorization-file", str(AUTHORIZATION_FILE),
        "--operator-approved",
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True,
            text=True, shell=False, timeout=MAX_CHILD_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DailyWitnessError("CHILD_EXEC_FAILED", f"{date}: {exc}") \
            from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise DailyWitnessError(
            "LEGACY_WITNESS_FAILED",
            f"{date} rc={result.returncode}: {detail}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DailyWitnessError("CHILD_RESULT_INVALID", date)
    try:
        payload = json.loads(lines[0])
    except ValueError as exc:
        raise DailyWitnessError("CHILD_RESULT_INVALID", date) from exc
    if not _legacy_result(payload, date):
        raise DailyWitnessError("CHILD_RESULT_INVALID", date)
    return payload


def run_all_legacy(*,
                   credential_file: pathlib.Path = LEGACY_PUBLISHER_ENV_FILE) \
        -> dict:
    completed: list[dict] = []
    failures: list[dict[str, str]] = []
    for date in sorted(LEGACY_DATES):
        try:
            payload = run_legacy(date, credential_file=credential_file)
            completed.append({
                "date": date,
                "witness": payload["witness_object"],
                "history_proof_sha256": payload["history_proof_sha256"],
                "historical_metadata_authority_sha256":
                    payload["historical_metadata_authority_sha256"],
            })
        except DailyWitnessError as exc:
            failures.append({
                "date": date, "code": exc.code, "detail": exc.detail})
    result = {
        "schema_version": "canonical-generation-legacy-batch-v1",
        "state": "COMPLETE" if not failures else "PARTIAL_FAILURE",
        "authorized_dates": sorted(LEGACY_DATES),
        "completed": completed,
        "failures": failures,
        "rfq": "OFF",
        "original_object_puts": 0,
        "copy_operations": 0,
        "tag_writes": 0,
        "research_computations": 0,
    }
    if failures:
        raise DailyWitnessError(
            "LEGACY_PARTIAL_FAILURE",
            json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result


def run(*, today: dt.date | None = None,
        credential_file: pathlib.Path = PUBLISHER_ENV_FILE) -> dict:
    environment = publisher_environment(credential_file)
    now = dt.datetime.now(dt.timezone.utc)
    today = today or now.date()
    dates = eligible_dates(today)
    completed: list[dict] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []
    for date in dates:
        try:
            marker = _read_terminal_marker(date)
            if marker is not None and not _marker_needs_recheck(
                    marker, date, today, now):
                skipped.append(date)
                continue
            payload = _run_date(date, environment)
            marker_path = _write_terminal_marker(date, payload, now=now)
            completed.append({
                "date": date,
                "marker": str(marker_path),
                "witness": payload["witness_object"],
            })
        except DailyWitnessError as exc:
            failures.append({"date": date, "code": exc.code,
                             "detail": exc.detail})
    result = {
        "schema_version": "canonical-generation-daily-v1",
        "state": "COMPLETE" if not failures else "PARTIAL_FAILURE",
        "eligible_dates": dates,
        "completed": completed,
        "already_terminal": skipped,
        "failures": failures,
        "rfq": "OFF",
        "copy_operations": 0,
        "tag_writes": 0,
        "research_computations": 0,
    }
    if failures:
        raise DailyWitnessError(
            "DAILY_PARTIAL_FAILURE",
            json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--publisher-env-file", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--legacy-date", choices=sorted(LEGACY_DATES))
    mode.add_argument("--migrate-all-legacy", action="store_true")
    parser.add_argument("--operator-approved", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.operator_approved:
            raise DailyWitnessError(
                "OPERATOR_GATE", "--operator-approved is required")
        expected_credential = (LEGACY_PUBLISHER_ENV_FILE
                               if args.legacy_date
                               or args.migrate_all_legacy
                               else PUBLISHER_ENV_FILE)
        if pathlib.Path(args.publisher_env_file) != expected_credential:
            raise DailyWitnessError(
                "CREDENTIAL_FILE_INVALID", "publisher credential path is fixed")
        if args.migrate_all_legacy:
            result = run_all_legacy(
                credential_file=pathlib.Path(args.publisher_env_file))
        elif args.legacy_date:
            result = run_legacy(
                args.legacy_date,
                credential_file=pathlib.Path(args.publisher_env_file))
        else:
            result = run(credential_file=pathlib.Path(args.publisher_env_file))
        print(json.dumps(result, sort_keys=True))
        return 0
    except DailyWitnessError as exc:
        print(f"GENERATION_DAILY_REFUSED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
