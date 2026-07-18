#!/usr/bin/env python3
"""Idempotent W09 MODE 1 selector/fetch/verify/query/research cycle.

The cycle consumes only published, RFQ-free, canonical-reference v3 releases.
Every S3 data read remains delegated to the instance-profile research reader,
which requests the exact VersionId and revalidates the eligibility tags.  A
successful cycle runs the bounded Deep03 D3-W2A discovery payload and records
an immutable completion pointer.  Nothing in this module can write to S3.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

import deep03_authority_gate


MODE = "MODE 1 / EXPLORATORY_AUTORESEARCH"
STATUS_SCHEMA = "w09-exploratory-autoresearch-status-v1"
COMPLETION_SCHEMA = "w09-exploratory-autoresearch-completion-v1"
STATIC_CREDENTIAL_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_PROFILE",
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
)


class AutoResearchError(RuntimeError):
    """A fail-closed automatic consumer or research error."""


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink():
        raise AutoResearchError("refusing linked state path: %s" % path)
    raw = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    temporary = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o640)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _refuse_credentials() -> None:
    present = sorted(name for name in STATIC_CREDENTIAL_NAMES if os.environ.get(name))
    if present:
        raise AutoResearchError(
            "static/trading credential environment is forbidden: %s"
            % ",".join(present)
        )


def _run_command(
    command: Sequence[str],
    *,
    step: str,
    log_root: Path,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(
            list(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in STATIC_CREDENTIAL_NAMES
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise AutoResearchError(
            "%s exceeded the exact-release runtime window" % step
        ) from exc
    output = result.stdout or ""
    _atomic_text(log_root / (step + ".log"), output)
    if result.returncode != 0:
        tail = " ".join(output.strip().splitlines()[-3:])[:1000]
        raise AutoResearchError(
            "%s failed rc=%d: %s" % (step, result.returncode, tail)
        )
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AutoResearchError("%s is unreadable: %s" % (label, exc)) from exc
    if not isinstance(value, dict):
        raise AutoResearchError("%s root is not an object" % label)
    return value


def _safe_hook(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise AutoResearchError("optional research hook is not a regular file")
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AutoResearchError(
            "optional research hook must be root-owned and not group/world writable"
        )
    if not os.access(path, os.X_OK):
        raise AutoResearchError("optional research hook is not executable")
    return True


def _completion_is_current(
    path: Path,
    *,
    selection_sha256: str,
    release_ids: list[str],
    authority_release_id: str,
    authority_sha256: str,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    value = _load_json(path, "completion pointer")
    return (
        value.get("schema_version") == COMPLETION_SCHEMA
        and value.get("state") == "RESEARCH_COMPLETE"
        and value.get("mode") == MODE
        and value.get("strict_acceptance_claimed") is False
        and value.get("selection_sha256") == selection_sha256
        and value.get("release_ids") == release_ids
        and value.get("authority_release_id") == authority_release_id
        and value.get("authority_sha256") == authority_sha256
        and value.get("rfq") == "OFF"
    )


def run_cycle(
    *,
    cache: Path,
    state_root: Path,
    run_root: Path,
    start_date: str,
    end_date: str | None,
    python: str,
    tools_root: Path,
    w09_tools_root: Path,
    hook: Path,
    authority_binding: dict[str, Any],
    required_release_ids: list[str],
    max_attempts: int = 3,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    _refuse_credentials()
    authority_release_id = authority_binding.get("release_id")
    authority_sha256 = authority_binding.get("authority_sha256")
    max_runtime_seconds = authority_binding.get("effective_runtime_seconds")
    if (
        authority_binding.get("state") != "AUTHORIZED"
        or authority_binding.get("mode") != MODE
        or not isinstance(authority_release_id, str)
        or not isinstance(authority_sha256, str)
        or len(authority_sha256) != 64
        or not isinstance(max_runtime_seconds, int)
        or isinstance(max_runtime_seconds, bool)
        or max_runtime_seconds <= 0
    ):
        raise AutoResearchError("exact-release authority binding is absent")
    if not required_release_ids or len(required_release_ids) != len(
        set(required_release_ids)
    ):
        raise AutoResearchError("authority has no unique exact input release set")
    if max_attempts < 1 or max_attempts > 10:
        raise AutoResearchError("max_attempts must be in [1,10]")
    deadline = time.monotonic() + max_runtime_seconds

    def remaining_seconds() -> int:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise AutoResearchError("exact-release runtime window expired")
        return remaining
    cache = Path(cache).resolve()
    state_root = Path(state_root).resolve()
    run_root = Path(run_root).resolve()
    for path in (cache, state_root, run_root):
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
    lock_path = state_root / "cycle.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AutoResearchError("another exploratory cycle is active") from exc

        cycle_id = _utc_now().replace("-", "").replace(":", "")
        log_root = state_root / "logs" / cycle_id
        status_path = state_root / "status.json"
        current_step = "START"

        def status(state: str, **extra: Any) -> dict[str, Any]:
            payload = {
                "schema_version": STATUS_SCHEMA,
                "state": state,
                "updated_at_utc": _utc_now(),
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "rfq": "OFF",
                "authority_release_id": authority_release_id,
                "authority_sha256": authority_sha256,
                "step": current_step,
                **extra,
            }
            _atomic_json(status_path, payload)
            return payload

        status("RUNNING")
        try:
            reader = [
                python,
                str(w09_tools_root / "research_data_instance_profile.py"),
                "--cache",
                str(cache),
            ]
            current_step = "INVENTORY"
            status("RUNNING")
            _run_command(
                [*reader, "inventory"],
                step="01-inventory",
                log_root=log_root,
                timeout_seconds=remaining_seconds(),
                runner=runner,
            )

            current_step = "SELECT"
            status("RUNNING")
            selector_command = [
                python,
                str(w09_tools_root / "exploratory_release_selector.py"),
                "--cache",
                str(cache),
                "--start-date",
                start_date,
            ]
            if end_date is not None:
                selector_command.extend(["--end-date", end_date])
            selected_process = _run_command(
                selector_command,
                step="02-selection",
                log_root=log_root,
                timeout_seconds=remaining_seconds(),
                runner=runner,
            )
            try:
                selection = json.loads(selected_process.stdout)
            except (TypeError, ValueError) as exc:
                raise AutoResearchError("selector output is not JSON") from exc
            release_ids = selection.get("release_ids")
            selection_sha = selection.get("selection_sha256")
            if (
                selection.get("mode") != MODE
                or selection.get("rfq") != "OFF"
                or not isinstance(release_ids, list)
                or not release_ids
                or not isinstance(selection_sha, str)
                or len(selection_sha) != 64
            ):
                raise AutoResearchError("selector output contract mismatch")
            if release_ids != required_release_ids:
                raise AutoResearchError(
                    "selected release set differs from exact-release authority"
                )
            selection_path = state_root / "selections" / (selection_sha + ".json")
            _atomic_json(selection_path, selection)

            required = sum(
                int(row.get("object_bytes") or 0)
                for row in selection.get("releases", [])
            ) + 10 * 1024**3
            available = shutil.disk_usage(cache).free
            if available < required:
                raise AutoResearchError(
                    "disk gate failed: need=%d available=%d" % (required, available)
                )

            for index, release_id in enumerate(release_ids, 1):
                current_step = "FETCH_%d_OF_%d" % (index, len(release_ids))
                status("RUNNING", selection_sha256=selection_sha)
                _run_command(
                    [*reader, "fetch", "--release", release_id],
                    step="03-fetch-%03d" % index,
                    log_root=log_root,
                    timeout_seconds=remaining_seconds(),
                    runner=runner,
                )
                current_step = "VERIFY_%d_OF_%d" % (index, len(release_ids))
                status("RUNNING", selection_sha256=selection_sha)
                _run_command(
                    [*reader, "verify", "--release", release_id],
                    step="04-verify-%03d" % index,
                    log_root=log_root,
                    timeout_seconds=remaining_seconds(),
                    runner=runner,
                )

            current_step = "EXPLORATORY_VIEW"
            status("RUNNING", selection_sha256=selection_sha)
            _run_command(
                [*reader, "view", "--include-non-confirmation"],
                step="05-exploratory-view",
                log_root=log_root,
                timeout_seconds=remaining_seconds(),
                runner=runner,
            )

            canary_root = state_root / "canaries" / selection_sha
            for index, release_id in enumerate(release_ids, 1):
                current_step = "QUERY_CANARY_%d_OF_%d" % (index, len(release_ids))
                status("RUNNING", selection_sha256=selection_sha)
                receipt = canary_root / (release_id + ".json")
                _run_command(
                    [
                        python,
                        str(w09_tools_root / "exploratory_v3_query_canary.py"),
                        "--cache",
                        str(cache),
                        "--release",
                        release_id,
                        "--receipt",
                        str(receipt),
                    ],
                    step="06-query-canary-%03d" % index,
                    log_root=log_root,
                    timeout_seconds=remaining_seconds(),
                    runner=runner,
                )
                canary = _load_json(receipt, "exploratory canary receipt")
                if (
                    canary.get("state") != "W09_V3_EXPLORATORY_QUERY_CANARY_PASS"
                    or canary.get("mode") != MODE
                    or canary.get("strict_acceptance_claimed") is not False
                    or canary.get("release_id") != release_id
                    or canary.get("rfq") != "OFF"
                ):
                    raise AutoResearchError("exploratory canary receipt mismatch")

            completion_path = state_root / "completed" / (
                selection_sha + "-" + authority_sha256[:12] + ".json"
            )
            if _completion_is_current(
                completion_path,
                selection_sha256=selection_sha,
                release_ids=release_ids,
                authority_release_id=authority_release_id,
                authority_sha256=authority_sha256,
            ):
                current_step = "IDEMPOTENT_COMPLETE"
                return status(
                    "RESEARCH_COMPLETE",
                    selection_sha256=selection_sha,
                    release_ids=release_ids,
                    idempotent_noop=True,
                    completion_pointer=str(completion_path),
                )

            prefix = "mode1-%s-%s-%s-%s" % (
                selection["start_date"].replace("-", ""),
                selection["end_date"].replace("-", ""),
                selection_sha[:12],
                authority_sha256[:12],
            )
            existing = sorted(run_root.glob(prefix + "-a*"))
            attempt = len(existing) + 1
            if attempt > max_attempts:
                raise AutoResearchError(
                    "automatic research attempt limit reached for selection %s"
                    % selection_sha
                )
            run_id = "%s-a%d" % (prefix, attempt)
            run_dir = run_root / run_id
            current_step = "RESEARCH_PREPARE"
            status(
                "DATA_READY",
                selection_sha256=selection_sha,
                release_ids=release_ids,
                run_id=run_id,
            )
            prepare_command = [
                python,
                str(tools_root / "research" / "deep03_v3_prepare.py"),
                "--cache",
                str(cache),
                "--run-root",
                str(run_root),
                "--run-id",
                run_id,
            ]
            for release_id in release_ids:
                prepare_command.extend(["--release", release_id])
            _run_command(
                prepare_command,
                step="07-research-prepare",
                log_root=log_root,
                timeout_seconds=remaining_seconds(),
                runner=runner,
            )

            current_step = "RESEARCH_RUN"
            status(
                "RESEARCH_RUNNING",
                selection_sha256=selection_sha,
                release_ids=release_ids,
                run_id=run_id,
            )
            _run_command(
                [
                    python,
                    str(tools_root / "research" / "deep03_v3_runner.py"),
                    "--run-dir",
                    str(run_dir),
                    "--memory-limit",
                    "32GB",
                    "--threads",
                    "8",
                ],
                step="08-research-run",
                log_root=log_root,
                timeout_seconds=remaining_seconds(),
                runner=runner,
            )
            complete = _load_json(run_dir / "RUN_COMPLETE.json", "RUN_COMPLETE")
            if (
                complete.get("state") != "RUN_COMPLETE"
                or complete.get("mode") != MODE
                or complete.get("strict_acceptance_claimed") is not False
                or complete.get("rfq_reads") != 0
                or complete.get("release_ids") != release_ids
            ):
                raise AutoResearchError("Deep03 completion contract mismatch")

            hook_ran = False
            if _safe_hook(hook):
                current_step = "POST_RESEARCH_HOOK"
                status(
                    "RESEARCH_RUNNING",
                    selection_sha256=selection_sha,
                    release_ids=release_ids,
                    run_id=run_id,
                )
                _run_command(
                    [
                        str(hook),
                        "--run-dir",
                        str(run_dir),
                        "--selection",
                        str(selection_path),
                    ],
                    step="09-post-research-hook",
                    log_root=log_root,
                    timeout_seconds=remaining_seconds(),
                    runner=runner,
                )
                hook_ran = True

            completion = {
                "schema_version": COMPLETION_SCHEMA,
                "state": "RESEARCH_COMPLETE",
                "completed_at_utc": _utc_now(),
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "selection_sha256": selection_sha,
                "release_ids": release_ids,
                "authority_release_id": authority_release_id,
                "authority_sha256": authority_sha256,
                "arm_sha256": authority_binding.get("arm_sha256"),
                "adopted_plan_sha256": authority_binding.get(
                    "adopted_plan_sha256"
                ),
                "rfq": "OFF",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "run_complete_sha256": hashlib.sha256(
                    (run_dir / "RUN_COMPLETE.json").read_bytes()
                ).hexdigest(),
                "post_research_hook_ran": hook_ran,
            }
            _atomic_json(completion_path, completion)
            current_step = "COMPLETE"
            return status(
                "RESEARCH_COMPLETE",
                selection_sha256=selection_sha,
                release_ids=release_ids,
                run_id=run_id,
                idempotent_noop=False,
                completion_pointer=str(completion_path),
            )
        except BaseException as exc:
            status(
                "REFUSED",
                error={"class": type(exc).__name__, "message": str(exc)[:2000]},
            )
            raise
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default="/srv/w09-research/cache")
    parser.add_argument("--state-root", default="/srv/w09-research/automation")
    parser.add_argument("--run-root", default="/srv/w09-research/runs")
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--arm-file", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runtime-commit", required=True, type=Path)
    parser.add_argument("--python", default="/opt/w09/venv/bin/python")
    parser.add_argument("--tools-root", default="/opt/w09/research/tools")
    parser.add_argument("--w09-tools-root", default="/opt/w09/research/tools")
    parser.add_argument(
        "--hook",
        default="/opt/w09/research/hooks/after_exploratory_autoresearch",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        authority = deep03_authority_gate.validate_authority(
            authority_path=args.authority,
            arm_path=args.arm_file,
            plan_path=args.plan,
            runtime_commit_path=args.runtime_commit,
        )
        result = run_cycle(
            cache=Path(args.cache),
            state_root=Path(args.state_root),
            run_root=Path(args.run_root),
            start_date=authority["input_start_date"],
            end_date=authority["input_end_date"],
            python=args.python,
            tools_root=Path(args.tools_root),
            w09_tools_root=Path(args.w09_tools_root),
            hook=Path(args.hook),
            authority_binding=authority,
            required_release_ids=authority["authorized_input_release_ids"],
            max_attempts=args.max_attempts,
        )
    except (
        AutoResearchError,
        deep03_authority_gate.AuthorityError,
        OSError,
        ValueError,
    ) as exc:
        print("W09_EXPLORATORY_AUTORESEARCH_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(
        "W09_EXPLORATORY_AUTORESEARCH_%s mode=%r selection=%s"
        % (result["state"], MODE, result.get("selection_sha256"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
