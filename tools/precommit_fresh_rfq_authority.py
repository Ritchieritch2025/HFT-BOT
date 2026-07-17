#!/usr/bin/env python3
"""Durably precommit one local fresh-RFQ epoch authority, create-only.

This tool is deliberately local-only.  It performs no AWS or network calls and
never replaces an existing path.  The large historical deny set is written to
the authority file but is never copied to stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import stat
import sys
from typing import Any, Callable


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import fresh_rfq_receipts as fresh  # noqa: E402


DEFAULT_MIN_LEAD_SECONDS = 300
DEFAULT_MAX_CREATED_AGE_SECONDS = 600
MAX_INPUT_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
MINIMUM_LEAD_SECONDS = 300
PRECOMMIT_ENVELOPE_SCHEMA = "fresh-rfq-authority-precommit-envelope-v1"
PRECOMMIT_RECEIPT_SCHEMA = "fresh-rfq-authority-precommit-receipt-v2"


class PrecommitError(RuntimeError):
    """A fail-closed local precommit error with a stable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str) -> None:
    raise PrecommitError(code, detail)


def _json_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("DUPLICATE_JSON_KEY", f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_no_duplicates)
    except PrecommitError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise PrecommitError("INVALID_JSON", f"{label}: {exc}") from exc


def _read_limited_stream(stream, label: str) -> bytes:
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        _fail("INVALID_INPUT", f"{label} did not return bytes or text")
    if len(raw) > MAX_INPUT_BYTES:
        _fail("INPUT_TOO_LARGE", f"{label} exceeds {MAX_INPUT_BYTES} bytes")
    return raw


def load_authority(path: str | None = None, *, stdin=None) -> Any:
    """Load one bounded JSON authority from ``path`` or stdin."""
    if path in (None, "-"):
        source = stdin if stdin is not None else sys.stdin.buffer
        raw = _read_limited_stream(source, "stdin")
    else:
        try:
            with open(path, "rb") as handle:
                raw = _read_limited_stream(handle, str(path))
        except OSError as exc:
            raise PrecommitError(
                "INPUT_READ_FAILED", f"cannot read input: {exc}") from exc
    return _decode_json(raw, "authority input")


def _coerce_now(now: dt.datetime | Callable[[], dt.datetime] | None
                ) -> dt.datetime:
    value = dt.datetime.now(dt.timezone.utc) if now is None else (
        now() if callable(now) else now)
    if (not isinstance(value, dt.datetime) or value.tzinfo is None or
            value.utcoffset() is None):
        _fail("INVALID_NOW", "now must be a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc)


def _seconds(value: int | float, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value < 0):
        _fail("INVALID_WINDOW", f"{label} must be a finite non-negative number")
    return float(value)


def _minimum_lead(value: Any) -> int:
    if type(value) is not int or value < MINIMUM_LEAD_SECONDS:
        _fail(
            "INVALID_WINDOW",
            f"minimum lead must be an integer >= {MINIMUM_LEAD_SECONDS}")
    return value


def _parse_utc_seconds(value: str, label: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise PrecommitError(
            "INVALID_AUTHORITY_TIME", f"{label}: {exc}") from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def _check_precommit_time(
    authority: dict[str, Any], *, now: dt.datetime,
    min_lead_seconds: int | float,
    max_created_age_seconds: int | float,
) -> None:
    min_lead = _minimum_lead(min_lead_seconds)
    max_age = _seconds(max_created_age_seconds, "max_created_age_seconds")
    t0 = _parse_utc_seconds(authority["strict_t0_utc"], "strict_t0_utc")
    created = _parse_utc_seconds(authority["created_at_utc"], "created_at_utc")
    if created > now:
        _fail("AUTHORITY_FROM_FUTURE", "created_at_utc is later than execution time")
    age = (now - created).total_seconds()
    if age > max_age:
        _fail(
            "AUTHORITY_REPLAY_WINDOW",
            f"authority age {age:.3f}s exceeds {max_age:.3f}s")
    lead = (t0 - now).total_seconds()
    if lead < min_lead:
        _fail(
            "T0_LEAD_TOO_SHORT",
            f"strict T0 lead {lead:.3f}s is below {min_lead:.3f}s")


def _utc_seconds(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_precommit_envelope(
    authority_value: Any, *, precommitted_at: dt.datetime,
    minimum_lead_seconds: int = MINIMUM_LEAD_SECONDS,
) -> dict[str, Any]:
    """Build the canonical durable object consumed by the capture service."""
    authority = fresh.validate_fresh_epoch_authority(authority_value)
    observed = _coerce_now(precommitted_at)
    minimum_lead = _minimum_lead(minimum_lead_seconds)
    envelope = {
        "schema": PRECOMMIT_ENVELOPE_SCHEMA,
        "authority": authority,
        "authority_sha256": authority["authority_sha256"],
        "operator_authorization_sha256":
            authority["operator_authorization_sha256"],
        "deployment_commit": authority["deployment_commit"],
        "strict_t0_utc": authority["strict_t0_utc"],
        "precommitted_at_utc": _utc_seconds(observed),
        "minimum_lead_seconds": minimum_lead,
    }
    envelope["envelope_sha256"] = fresh.canonical_sha256(envelope)
    return validate_precommit_envelope(envelope)


def validate_precommit_envelope(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "authority", "authority_sha256",
        "operator_authorization_sha256", "deployment_commit",
        "strict_t0_utc", "precommitted_at_utc", "minimum_lead_seconds",
        "envelope_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        _fail("ENVELOPE_SCHEMA", "precommit envelope fields differ from contract")
    if value["schema"] != PRECOMMIT_ENVELOPE_SCHEMA:
        _fail("ENVELOPE_SCHEMA", "wrong precommit envelope schema")
    authority = fresh.validate_fresh_epoch_authority(value["authority"])
    fixed = {
        "authority_sha256": authority["authority_sha256"],
        "operator_authorization_sha256":
            fresh.OPERATOR_AUTHORIZATION_SHA256,
        "deployment_commit": authority["deployment_commit"],
        "strict_t0_utc": authority["strict_t0_utc"],
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("ENVELOPE_BINDING", "envelope/authority binding mismatch")
    precommitted = _parse_utc_seconds(
        value["precommitted_at_utc"], "precommitted_at_utc")
    strict_t0 = _parse_utc_seconds(value["strict_t0_utc"], "strict_t0_utc")
    minimum_lead = _minimum_lead(value["minimum_lead_seconds"])
    if (strict_t0 - precommitted).total_seconds() < minimum_lead:
        _fail("ENVELOPE_TIME", "precommit timestamp does not meet T0 lead")
    supplied = value.get("envelope_sha256")
    if not isinstance(supplied, str) or fresh.SHA_RE.fullmatch(supplied) is None:
        _fail("ENVELOPE_DIGEST", "envelope_sha256 must be lowercase 64-hex")
    unsigned = dict(value)
    unsigned.pop("envelope_sha256")
    if supplied != fresh.canonical_sha256(unsigned):
        _fail("ENVELOPE_DIGEST", "envelope_sha256 mismatch")
    normalized = dict(value)
    normalized["authority"] = authority
    return normalized


def _fault(fault_hook: Callable[[str], None] | None, stage: str) -> None:
    if fault_hook is not None:
        fault_hook(stage)


def _output_parts(output: str | os.PathLike[str]) -> tuple[str, str, str]:
    raw = os.fspath(output)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        _fail("INVALID_OUTPUT", "output must be a non-empty filesystem path")
    absolute = os.path.abspath(raw)
    parent = os.path.dirname(absolute)
    name = os.path.basename(absolute)
    if name in ("", ".", ".."):
        _fail("INVALID_OUTPUT", "output must name one file")
    return absolute, parent, name


def _open_directory_nofollow(path: str) -> int:
    """Open every absolute directory component without following symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        _fail(
            "LOCAL_SAFETY_UNAVAILABLE",
            "O_NOFOLLOW and O_DIRECTORY are required")
    if not os.path.isabs(path):
        _fail("OUTPUT_PARENT_UNSAFE", "output parent must be absolute")
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
             getattr(os, "O_CLOEXEC", 0))
    fd = -1
    try:
        fd = os.open("/", flags)
        for part in [item for item in path.split(os.sep) if item]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise PrecommitError(
            "OUTPUT_PARENT_UNSAFE",
            f"cannot traverse output parent without symlinks: {exc}") from exc
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _open_parent(parent: str, *, expected_owner_uid: int | None) -> int:
    fd = _open_directory_nofollow(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            _fail("OUTPUT_PARENT_UNSAFE", "output parent is not a directory")
        if stat.S_IMODE(info.st_mode) & 0o022:
            _fail("OUTPUT_PARENT_UNSAFE",
                  "output parent must not be group/world writable")
        if expected_owner_uid is not None:
            if type(expected_owner_uid) is not int or expected_owner_uid < 0:
                _fail("OUTPUT_OWNER", "expected_owner_uid must be a non-negative int")
            if os.geteuid() != expected_owner_uid or info.st_uid != expected_owner_uid:
                _fail("OUTPUT_OWNER",
                      "effective UID and output parent owner must match expectation")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_identity(left, right) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _readback(
    *, parent_fd: int, name: str, created_info, expected: bytes,
    envelope: dict[str, Any], expected_owner_uid: int | None,
    fault_hook: Callable[[str], None] | None,
) -> tuple[bytes, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PrecommitError(
            "READBACK_OPEN_FAILED", f"cannot safely reopen output: {exc}") from exc
    try:
        _fault(fault_hook, "after_readback_open")
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or
                not _same_identity(before, created_info) or
                before.st_size != len(expected) or before.st_nlink != 1 or
                stat.S_IMODE(before.st_mode) != 0o444 or
                (expected_owner_uid is not None and
                 before.st_uid != expected_owner_uid)):
            _fail("READBACK_IDENTITY", "created output identity/mode/size changed")
        chunks = []
        total = 0
        while total <= len(expected):
            chunk = os.read(
                fd, min(READ_CHUNK_BYTES, len(expected) + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PrecommitError(
                "READBACK_IDENTITY", f"output disappeared during readback: {exc}") from exc
        fingerprint = lambda item: (  # noqa: E731 - compact stable tuple
            item.st_dev, item.st_ino, item.st_size,
            item.st_mtime_ns, item.st_ctime_ns)
        if (total != len(expected) or fingerprint(before) != fingerprint(after) or
                not stat.S_ISREG(named.st_mode) or
                not _same_identity(after, named)):
            _fail("READBACK_IDENTITY", "output changed during fixed-FD readback")
        raw = b"".join(chunks)
        _fault(fault_hook, "after_readback")
        if raw != expected:
            _fail("READBACK_BYTES", "stored authority bytes differ from precommit")
        readback_envelope = validate_precommit_envelope(
            _decode_json(raw, "precommit envelope readback"))
        if readback_envelope != envelope:
            _fail("READBACK_AUTHORITY", "readback envelope/digest mismatch")
        _fault(fault_hook, "after_readback_validation")
        return raw, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def _cleanup_created(parent_fd: int, name: str, created_info) -> None:
    """Remove only the directory entry that still names our new inode."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.fsync(parent_fd)
        return
    except OSError as exc:
        raise PrecommitError(
            "ROLLBACK_FAILED", f"cannot inspect partial output: {exc}") from exc
    if not _same_identity(current, created_info):
        _fail(
            "ROLLBACK_FAILED",
            "output identity changed; refusing to unlink an unowned object")
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise PrecommitError(
            "ROLLBACK_FAILED", f"cannot remove partial output durably: {exc}") from exc


def precommit_authority(
    authority_value: Any,
    output: str | os.PathLike[str],
    *,
    now: dt.datetime | Callable[[], dt.datetime] | None = None,
    min_lead_seconds: int | float = DEFAULT_MIN_LEAD_SECONDS,
    max_created_age_seconds: int | float = DEFAULT_MAX_CREATED_AGE_SECONDS,
    expected_owner_uid: int | None = 0,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Validate and durably create one immutable local authority file.

    ``fault_hook(stage)`` is a test-only transaction hook.  Any exception it
    raises follows the same rollback path as an I/O failure.
    """
    authority = fresh.validate_fresh_epoch_authority(authority_value)
    observed_now = _coerce_now(now)
    _check_precommit_time(
        authority, now=observed_now,
        min_lead_seconds=min_lead_seconds,
        max_created_age_seconds=max_created_age_seconds)
    envelope = build_precommit_envelope(
        authority, precommitted_at=observed_now,
        minimum_lead_seconds=_minimum_lead(min_lead_seconds))
    payload = fresh.canonical_bytes(envelope) + b"\n"
    if len(payload) > MAX_ENVELOPE_BYTES:
        _fail("ENVELOPE_TOO_LARGE",
              f"canonical envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    absolute, parent, name = _output_parts(output)
    parent_fd = _open_parent(parent, expected_owner_uid=expected_owner_uid)
    write_fd = -1
    created_info = None
    try:
        flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW |
                 getattr(os, "O_CLOEXEC", 0))
        try:
            write_fd = os.open(name, flags, 0o444, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise PrecommitError(
                "OUTPUT_EXISTS", "refusing to overwrite existing output") from exc
        except OSError as exc:
            raise PrecommitError(
                "OUTPUT_CREATE_FAILED", f"cannot create output safely: {exc}") from exc
        created_info = os.fstat(write_fd)
        if not stat.S_ISREG(created_info.st_mode):
            _fail("OUTPUT_CREATE_FAILED", "created output is not a regular file")
        created_info = os.fstat(write_fd)
        if (created_info.st_nlink != 1 or
                stat.S_IMODE(created_info.st_mode) != 0o444 or
                (expected_owner_uid is not None and
                 created_info.st_uid != expected_owner_uid)):
            _fail("OUTPUT_CREATE_FAILED", "created output ownership/mode is unsafe")
        _fault(fault_hook, "after_create")

        view = memoryview(payload)
        while view:
            try:
                written = os.write(write_fd, view)
            except OSError as exc:
                raise PrecommitError(
                    "OUTPUT_WRITE_FAILED", f"authority write failed: {exc}") from exc
            if written <= 0 or written > len(view):
                _fail("OUTPUT_WRITE_FAILED", "short authority write")
            view = view[written:]
            _fault(fault_hook, "after_write_chunk")
        _fault(fault_hook, "after_write")
        try:
            os.fsync(write_fd)
        except OSError as exc:
            raise PrecommitError(
                "OUTPUT_FSYNC_FAILED", f"authority fsync failed: {exc}") from exc
        _fault(fault_hook, "after_file_fsync")
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise PrecommitError(
                "OUTPUT_FSYNC_FAILED", f"parent fsync failed: {exc}") from exc
        _fault(fault_hook, "after_parent_fsync")

        _, file_sha256 = _readback(
            parent_fd=parent_fd, name=name, created_info=created_info,
            expected=payload, envelope=envelope,
            expected_owner_uid=expected_owner_uid, fault_hook=fault_hook)
        return {
            "schema": PRECOMMIT_RECEIPT_SCHEMA,
            "output": absolute,
            "authority_sha256": authority["authority_sha256"],
            "operator_authorization_sha256":
                authority["operator_authorization_sha256"],
            "deployment_commit": authority["deployment_commit"],
            "strict_t0_utc": authority["strict_t0_utc"],
            "created_at_utc": authority["created_at_utc"],
            "precommitted_at_utc": envelope["precommitted_at_utc"],
            "minimum_lead_seconds": envelope["minimum_lead_seconds"],
            "envelope_sha256": envelope["envelope_sha256"],
            "bytes": len(payload),
            "file_sha256": file_sha256,
        }
    except BaseException as original:
        if created_info is not None:
            try:
                _cleanup_created(parent_fd, name, created_info)
            except BaseException as rollback:
                raise PrecommitError(
                    "ROLLBACK_FAILED",
                    f"precommit failed and partial cleanup failed: {rollback}") from original
        raise
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(parent_fd)


# Descriptive alias for importers; both names share the identical transaction.
precommit_fresh_rfq_authority = precommit_authority


def main(
    argv: list[str] | None = None, *, stdin=None, stdout=None, stderr=None,
    now: dt.datetime | Callable[[], dt.datetime] | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input", default="-", help="authority JSON path; default '-' reads stdin")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--min-lead-seconds", type=int,
        default=DEFAULT_MIN_LEAD_SECONDS)
    parser.add_argument(
        "--max-created-age-seconds", type=float,
        default=DEFAULT_MAX_CREATED_AGE_SECONDS)
    parser.add_argument(
        "--expected-owner-uid", type=int, default=0,
        help="require both effective UID and output parent/file owner")
    args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        authority = load_authority(args.input, stdin=stdin)
        receipt = precommit_authority(
            authority, args.output, now=now,
            min_lead_seconds=args.min_lead_seconds,
            max_created_age_seconds=args.max_created_age_seconds,
            expected_owner_uid=args.expected_owner_uid,
            fault_hook=fault_hook)
    except (PrecommitError, fresh.FreshRfqError, OSError) as exc:
        print(f"REFUSED {exc}", file=err)
        return 78
    print(fresh.canonical_bytes(receipt).decode("ascii"), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
