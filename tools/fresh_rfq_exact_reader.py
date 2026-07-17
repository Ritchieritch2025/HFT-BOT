#!/usr/bin/env python3
"""Bounded exact-VersionId reader for the isolated fresh-RFQ lane.

This module is transport-neutral.  Callers inject a client exposing
``head(bucket, key, version_id)`` and
``get_exact(bucket, key, version_id, destination)``.  Every object is already
pinned to a non-null VersionId before this module is entered; there is no
latest-version resolution, listing, fallback, or AWS implementation here.
The core verifies the responses and downloaded bytes it receives, but it does
not attest the adapter's implementation, AWS transport, IAM, or side effects.

An :class:`ExactReadSession` validates the complete expected identity set
before the first client call.  ``open_exact`` downloads one object into a
private ephemeral directory, verifies both response identities and the full
body size/SHA-256, then yields a read-only record containing a temporary path.
Only one object may be active at a time, and the temporary tree is removed in
``finally``.  A successful session emits a deterministic, body-free
attestation only after every expected object was consumed exactly once.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Iterator, Mapping


SOURCE_BUCKET = "kalshi-vault-ritcardo"
ATTESTATION_SCHEMA = "fresh-rfq-exact-reader-core-attestation-v1"
IDENTITY_FIELDS = {"bucket", "key", "version_id", "size", "sha256"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSPORT_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
READ_CHUNK_BYTES = 1 << 20


class FreshRfqExactReaderError(RuntimeError):
    """Stable fail-closed error raised by the exact-reader boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise FreshRfqExactReaderError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _safe_key(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        _fail("OBJECT_IDENTITY_INVALID", f"{label} is not a safe S3 key")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("OBJECT_IDENTITY_INVALID", f"{label} has an unsafe path component")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("OBJECT_IDENTITY_INVALID", f"{label} contains a control character")
    return value


def _version(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.lower() == "null"
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        _fail("VERSION_REQUIRED", f"{label} requires a pinned non-null VersionId")
    return value


def _normalize_identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS:
        _fail("OBJECT_IDENTITY_FIELDS", f"{label} fields differ from the contract")
    if value["bucket"] != SOURCE_BUCKET:
        _fail("SOURCE_BUCKET_MISMATCH", f"{label}.bucket is not the fixed source")
    size = value["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        _fail("OBJECT_SIZE_INVALID", f"{label}.size must be a non-negative integer")
    digest = value["sha256"]
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        _fail("OBJECT_SHA256_INVALID", f"{label}.sha256 is not lowercase SHA-256")
    return {
        "bucket": SOURCE_BUCKET,
        "key": _safe_key(value["key"], f"{label}.key"),
        "version_id": _version(value["version_id"], f"{label}.version_id"),
        "size": size,
        "sha256": digest,
    }


def _identity_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return value["bucket"], value["key"], value["version_id"]


def _normalize_expected(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail("EXPECTED_SET_INVALID", "expected_objects must be a non-empty list")
    rows: list[dict[str, Any]] = []
    physical: set[tuple[str, str, str]] = set()
    keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        row = _normalize_identity(raw, f"expected_objects[{index}]")
        exact = _identity_key(row)
        logical = (row["bucket"], row["key"])
        if exact in physical:
            _fail("EXPECTED_SET_DUPLICATE", f"duplicate exact object {row['key']}")
        if logical in keys:
            _fail(
                "EXPECTED_KEY_AMBIGUOUS",
                f"expected set names multiple versions of {row['key']}",
            )
        physical.add(exact)
        keys.add(logical)
        rows.append(row)
    rows.sort(key=lambda row: (row["key"], row["version_id"]))
    return rows


def _validate_client_response(
    response: Any, identity: Mapping[str, Any], label: str,
) -> tuple[str, int]:
    if not isinstance(response, dict):
        _fail("CLIENT_RESPONSE_INVALID", f"{label} response is not an object")
    version_id = response.get("VersionId")
    content_length = response.get("ContentLength")
    if version_id != identity["version_id"]:
        _fail(
            "RESPONSE_VERSION_MISMATCH",
            f"{label} VersionId differs for {identity['key']}",
        )
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length != identity["size"]
    ):
        _fail(
            "RESPONSE_SIZE_MISMATCH",
            f"{label} ContentLength differs for {identity['key']}",
        )
    return version_id, content_length


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _best_effort_discard_temp_root(root: Path | None) -> None:
    """Remove only a root just returned by our own ``mkdtemp`` call."""
    if root is None:
        return
    try:
        if os.path.lexists(root):
            shutil.rmtree(root)
    except OSError:
        # The caller will still fail closed with the original creation/mode
        # error.  This helper exists to prevent ordinary partial-setup leaks.
        pass


def _private_temp_root(
    parent: str | os.PathLike[str] | None,
) -> tuple[Path, os.stat_result]:
    root: Path | None = None
    try:
        root = Path(tempfile.mkdtemp(
            prefix="fresh-rfq-exact-",
            dir=None if parent is None else os.fspath(parent),
        ))
        os.chmod(root, 0o700)
        observed = root.lstat()
    except OSError as exc:
        _best_effort_discard_temp_root(root)
        _fail("TEMP_CREATE_FAILED", str(exc))
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o700
        or observed.st_nlink < 1
    ):
        _best_effort_discard_temp_root(root)
        _fail("TEMP_MODE_INVALID", "private temporary directory is not mode 0700")
    return root, observed


def _assert_private_root(root: Path, created: os.stat_result) -> os.stat_result:
    try:
        observed = root.lstat()
    except OSError as exc:
        _fail("TEMP_ROOT_INVALID", str(exc))
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o700
        or observed.st_nlink < created.st_nlink
        or (observed.st_dev, observed.st_ino)
        != (created.st_dev, created.st_ino)
    ):
        _fail(
            "TEMP_ROOT_INVALID",
            "private temporary directory was replaced or changed from mode 0700",
        )
    return observed


def _create_private_file(path: Path) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            observed = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        _fail("TEMP_CREATE_FAILED", str(exc))
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_nlink != 1
    ):
        _fail("TEMP_FILE_INVALID", "ephemeral object is not one regular 0600 file")
    return observed


def _assert_single_private_file(
    root: Path, path: Path, created: os.stat_result,
) -> os.stat_result:
    try:
        entries = list(root.iterdir())
        observed = path.lstat()
    except OSError as exc:
        _fail("TEMP_FILE_INVALID", str(exc))
    if entries != [path]:
        _fail("TEMP_SCOPE_VIOLATION", "client created an unexpected temporary entry")
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != (created.st_dev, created.st_ino)
    ):
        _fail("TEMP_FILE_INVALID", "client replaced or changed the private 0600 file")
    return observed


def _stream_verify(
    path: Path, identity: Mapping[str, Any], created: os.stat_result,
) -> tuple[os.stat_result, int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail("TEMP_FILE_INVALID", str(exc))
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_nlink,
                stat.S_IMODE(before.st_mode))
            != (created.st_dev, created.st_ino, created.st_nlink,
                stat.S_IMODE(created.st_mode))
        ):
            _fail(
                "TEMP_FILE_INVALID",
                "opened download differs from the pre-created regular 0600 file",
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > identity["size"]:
                _fail("BODY_SIZE_MISMATCH", f"body is oversized for {identity['key']}")
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _signature(before) != _signature(after):
        _fail("TEMP_FILE_CHANGED", f"body changed while hashing {identity['key']}")
    if total != identity["size"] or before.st_size != identity["size"]:
        _fail("BODY_SIZE_MISMATCH", f"body size differs for {identity['key']}")
    observed_sha256 = digest.hexdigest()
    if observed_sha256 != identity["sha256"]:
        _fail("BODY_SHA256_MISMATCH", f"body SHA-256 differs for {identity['key']}")
    return after, total, observed_sha256


def _remove_private_root(root: Path) -> None:
    try:
        if os.path.lexists(root):
            shutil.rmtree(root)
    except OSError as exc:
        _fail("TEMP_CLEANUP_FAILED", str(exc))
    if os.path.lexists(root):
        _fail("TEMP_CLEANUP_FAILED", f"private temporary root remains: {root}")


@dataclass(frozen=True)
class VerifiedExactObject:
    """Read-only view valid only inside ``session.open_exact(...)``."""

    path: Path
    identity: Mapping[str, Any]


class ExactReadSession:
    """Verify one complete pinned exact-object set with bounded scratch."""

    def __init__(
        self,
        expected_objects: list[dict[str, Any]],
        client: Any,
        *,
        transport_kind: str,
        temp_parent: str | os.PathLike[str] | None = None,
    ):
        self._expected = _normalize_expected(expected_objects)
        self._expected_by_exact = {
            _identity_key(row): row for row in self._expected
        }
        if not callable(getattr(client, "head", None)) or not callable(
            getattr(client, "get_exact", None)
        ):
            _fail(
                "CLIENT_INTERFACE_INVALID",
                "client must expose the required head/get_exact call surface",
            )
        self._client = client
        if (
            not isinstance(transport_kind, str)
            or TRANSPORT_KIND_RE.fullmatch(transport_kind) is None
        ):
            _fail(
                "TRANSPORT_KIND_INVALID",
                "transport_kind must be an explicit canonical adapter kind",
            )
        self._transport_kind = transport_kind
        self._temp_parent = temp_parent
        self._verified: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._read_ledger: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._entered = False
        self._exited = False
        self._active = False
        self._active_root: Path | None = None
        self._object_lock = threading.Lock()
        self._poison_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._poisoned: tuple[str, str] | None = None
        self._head_calls = 0
        self._get_calls = 0
        self._ephemeral_files = 0
        self._ephemeral_bytes = 0
        self._peak_active = 0
        self._attestation: dict[str, Any] | None = None

    @property
    def expected_objects(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._expected)

    @property
    def attestation(self) -> dict[str, Any]:
        if self._attestation is None:
            _fail(
                "ATTESTATION_NOT_AVAILABLE",
                "attestation exists only after a successful complete session",
            )
        return copy.deepcopy(self._attestation)

    def __enter__(self) -> "ExactReadSession":
        with self._state_lock:
            if self._entered or self._exited:
                _fail("SESSION_STATE", "exact-read sessions are single-use")
            self._entered = True
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> bool:
        with self._state_lock:
            self._exited = True
            active = self._active
            active_root = self._active_root
            self._active_root = None
        if active:
            self._remember_failure(FreshRfqExactReaderError(
                "SESSION_STATE", "session exited while an exact object was active",
            ))
        if active_root is not None:
            try:
                _remove_private_root(active_root)
            except FreshRfqExactReaderError as cleanup_error:
                self._remember_failure(cleanup_error)
        if exc_type is not None:
            return False
        with self._poison_lock:
            poisoned = self._poisoned
        if poisoned is not None:
            code, detail = poisoned
            _fail("SESSION_POISONED", f"{code}: {detail}")
        expected = set(self._expected_by_exact)
        verified = set(self._verified)
        if verified != expected:
            missing = sorted(
                self._expected_by_exact[key]["key"]
                for key in expected - verified
            )
            extra = sorted(
                self._verified[key]["key"] for key in verified - expected
            )
            _fail(
                "INCOMPLETE_EXACT_SET",
                f"verified set differs: missing={missing} extra={extra}",
            )
        self._attestation = self._build_attestation()
        return False

    def _remember_failure(self, exc: BaseException) -> None:
        with self._poison_lock:
            if self._poisoned is None:
                if isinstance(exc, FreshRfqExactReaderError):
                    self._poisoned = (exc.code, exc.detail)
                else:
                    self._poisoned = (
                        "OBJECT_CONSUMER_FAILED", exc.__class__.__name__,
                    )

    def _build_attestation(self) -> dict[str, Any]:
        identities = [copy.deepcopy(self._verified[_identity_key(row)])
                      for row in self._expected]
        read_ledger = [copy.deepcopy(self._read_ledger[_identity_key(row)])
                       for row in self._expected]
        expected_sha = canonical_sha256(self._expected)
        result = {
            "schema": ATTESTATION_SCHEMA,
            "state": "ALL_EXPECTED_CALLER_IDENTITIES_BODY_VERIFIED",
            "verification_state":
                "INJECTED_CLIENT_RESPONSE_AND_FULL_BODY_SHA256_VERIFIED",
            "source_bucket": SOURCE_BUCKET,
            "caller_declared_transport_kind": self._transport_kind,
            "transport_attestation_state": "CALLER_ADAPTER_UNVERIFIED",
            "objects": identities,
            "read_ledger": read_ledger,
            "read_ledger_sha256": canonical_sha256(read_ledger),
            "expected_object_count": len(self._expected),
            "verified_object_count": len(identities),
            "expected_total_bytes": sum(row["size"] for row in self._expected),
            "verified_total_bytes": sum(row["size"] for row in identities),
            "expected_object_set_sha256": expected_sha,
            "verified_object_set_sha256": canonical_sha256(identities),
            "all_expected_objects_verified": True,
            "unexpected_object_count": 0,
            "duplicate_read_count": 0,
            "module_read_api_methods_invoked": ["head", "get_exact"],
            "module_head_call_count": self._head_calls,
            "module_get_exact_call_count": self._get_calls,
            "version_id_argument_supplied_on_all_calls": True,
            "module_list_api_call_count": 0,
            "module_write_api_call_count": 0,
            "exact_body_identity_verified": True,
            "source_objects_exact_get_verified": False,
            "aws_transport_verified": False,
            "aws_no_write_verified": False,
            "requires_external_iam_and_operation_audit": True,
            "max_active_object_count": self._peak_active,
            "ephemeral_temp_directory_mode": "0700",
            "ephemeral_temp_file_mode": "0600",
            "ephemeral_files_created": self._ephemeral_files,
            "ephemeral_bytes_staged": self._ephemeral_bytes,
            "ephemeral_temp_deleted_before_return": True,
            "input_bodies_omitted": True,
            "module_durable_data_copy_count": 0,
        }
        result["attestation_sha256"] = canonical_sha256(result)
        return result

    @contextmanager
    def open_exact(self, identity: dict[str, Any]) -> Iterator[VerifiedExactObject]:
        with self._state_lock:
            if not self._entered or self._exited:
                _fail("SESSION_STATE", "open_exact requires one active session")
        with self._poison_lock:
            poisoned = self._poisoned
        if poisoned is not None:
            _fail("SESSION_POISONED", "a prior exact-read violation occurred")
        lock_acquired = self._object_lock.acquire(blocking=False)
        if not lock_acquired:
            error = FreshRfqExactReaderError(
                "CONCURRENT_OBJECT_OPEN",
                "only one exact object may be active at a time",
            )
            self._remember_failure(error)
            raise error

        root: Path | None = None
        root_created: os.stat_result | None = None
        try:
            try:
                normalized = _normalize_identity(identity, "open_exact.identity")
            except FreshRfqExactReaderError as exc:
                self._remember_failure(exc)
                raise
            exact = _identity_key(normalized)
            expected = self._expected_by_exact.get(exact)
            if expected is None or canonical_bytes(normalized) != canonical_bytes(expected):
                error = FreshRfqExactReaderError(
                    "UNEXPECTED_OBJECT",
                    f"object is outside the expected exact set: {normalized['key']}",
                )
                self._remember_failure(error)
                raise error
            if exact in self._verified:
                error = FreshRfqExactReaderError(
                    "DUPLICATE_READ",
                    f"exact object was already consumed: {normalized['key']}",
                )
                self._remember_failure(error)
                raise error

            with self._state_lock:
                if self._exited:
                    _fail("SESSION_STATE", "session exited before object setup")
                self._active = True
            self._peak_active = max(self._peak_active, 1)
            root, root_created = _private_temp_root(self._temp_parent)
            with self._state_lock:
                if self._exited:
                    _fail("SESSION_STATE", "session exited during object setup")
                self._active_root = root

            self._head_calls += 1
            try:
                head = self._client.head(
                    normalized["bucket"], normalized["key"],
                    normalized["version_id"],
                )
            except FreshRfqExactReaderError:
                raise
            except Exception as exc:
                _fail("CLIENT_HEAD_FAILED", str(exc))
            head_version, head_length = _validate_client_response(
                head, normalized, "HEAD",
            )

            path = root / "exact-version.bin"
            created = _create_private_file(path)
            self._get_calls += 1
            try:
                get_response = self._client.get_exact(
                    normalized["bucket"], normalized["key"],
                    normalized["version_id"], os.fspath(path),
                )
            except FreshRfqExactReaderError:
                raise
            except Exception as exc:
                _fail("CLIENT_GET_FAILED", str(exc))
            get_version, get_length = _validate_client_response(
                get_response, normalized, "GET",
            )
            _assert_private_root(root, root_created)
            _assert_single_private_file(root, path, created)
            (verified_stat, observed_size,
             observed_sha256) = _stream_verify(path, normalized, created)
            verified_signature = _signature(verified_stat)
            _assert_private_root(root, root_created)
            self._ephemeral_files += 1
            self._ephemeral_bytes += normalized["size"]

            record = VerifiedExactObject(
                path=path,
                identity=MappingProxyType(copy.deepcopy(normalized)),
            )
            yield record

            try:
                after_consumer = path.lstat()
            except OSError as exc:
                _fail("TEMP_FILE_CHANGED", str(exc))
            _assert_private_root(root, root_created)
            _assert_single_private_file(root, path, created)
            if _signature(after_consumer) != verified_signature:
                _fail(
                    "TEMP_FILE_CHANGED",
                    f"consumer changed verified body {normalized['key']}",
                )
            self._verified[exact] = copy.deepcopy(normalized)
            self._read_ledger[exact] = {
                "bucket": normalized["bucket"],
                "key": normalized["key"],
                "requested_version_id": normalized["version_id"],
                "head_response_version_id": head_version,
                "head_response_content_length": head_length,
                "get_response_version_id": get_version,
                "get_response_content_length": get_length,
                "expected_size": normalized["size"],
                "observed_size": observed_size,
                "expected_sha256": normalized["sha256"],
                "observed_sha256": observed_sha256,
                "verification_state":
                    "VERSION_ARGUMENT_HEAD_GET_RESPONSE_AND_FULL_BODY_SHA256_VERIFIED",
            }
        except BaseException as exc:
            self._remember_failure(exc)
            raise
        finally:
            cleanup_error: FreshRfqExactReaderError | None = None
            if root is not None:
                try:
                    _remove_private_root(root)
                except FreshRfqExactReaderError as exc:
                    cleanup_error = exc
                    self._remember_failure(exc)
            with self._state_lock:
                if self._active_root == root:
                    self._active_root = None
                self._active = False
            if lock_acquired:
                self._object_lock.release()
            if cleanup_error is not None:
                raise cleanup_error


__all__ = [
    "ATTESTATION_SCHEMA",
    "ExactReadSession",
    "FreshRfqExactReaderError",
    "SOURCE_BUCKET",
    "VerifiedExactObject",
    "canonical_bytes",
    "canonical_sha256",
]
