"""Immutable provenance and deterministic receipt helpers for PnL runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,1024}$")
VALID_CHANNELS = frozenset(
    {"L1", "L2", "TRADES", "CATALOG", "SETTLEMENT", "PRIVATE_FILLS", "LATENCY"}
)


class ProvenanceError(ValueError):
    """An immutable source or receipt binding is absent or malformed."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProvenanceError(f"value is not canonicalizable JSON: {exc}") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ProvenanceError(f"{name} must be a lowercase SHA-256")
    return value


def _plain_int(name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProvenanceError(f"{name} must be a plain integer >= {minimum}")
    return value


def _safe_logical_key(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProvenanceError("logical_key must be a non-empty POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise ProvenanceError("logical_key is unsafe")
    return value


@dataclass(frozen=True)
class ExactSourceObject:
    logical_key: str
    version_id: str
    sha256: str
    size_bytes: int
    channel: str
    date: str

    def __post_init__(self) -> None:
        _safe_logical_key(self.logical_key)
        if (
            not isinstance(self.version_id, str)
            or VERSION_RE.fullmatch(self.version_id) is None
        ):
            raise ProvenanceError("version_id is missing or malformed")
        _sha("sha256", self.sha256)
        _plain_int("size_bytes", self.size_bytes)
        if self.channel not in VALID_CHANNELS:
            raise ProvenanceError(f"unknown source channel: {self.channel!r}")
        if not isinstance(self.date, str) or DATE_RE.fullmatch(self.date) is None:
            raise ProvenanceError("date must be YYYY-MM-DD")


@dataclass(frozen=True)
class ExactReleaseBinding:
    release_id: str
    date: str
    manifest_sha256: str
    manifest_version_id: str
    evidence_tier: str
    objects: tuple[ExactSourceObject, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or not self.release_id:
            raise ProvenanceError("release_id is required")
        if not isinstance(self.date, str) or DATE_RE.fullmatch(self.date) is None:
            raise ProvenanceError("release date must be YYYY-MM-DD")
        _sha("manifest_sha256", self.manifest_sha256)
        if (
            not isinstance(self.manifest_version_id, str)
            or VERSION_RE.fullmatch(self.manifest_version_id) is None
        ):
            raise ProvenanceError("manifest_version_id is required")
        if self.evidence_tier not in {
            "SEALED_CONFIRMATION",
            "SEALED_DEGRADED_EVIDENCE",
        }:
            raise ProvenanceError("unknown evidence_tier")
        if not self.objects:
            raise ProvenanceError("an exact release must bind at least one object")
        identities: set[tuple[str, str]] = set()
        for row in self.objects:
            if not isinstance(row, ExactSourceObject):
                raise ProvenanceError("objects must contain ExactSourceObject rows")
            if row.date != self.date:
                raise ProvenanceError("source object escaped exact release date")
            identity = (row.logical_key, row.version_id)
            if identity in identities:
                raise ProvenanceError("duplicate exact source object binding")
            identities.add(identity)


@dataclass(frozen=True)
class RunBinding:
    schema_version: str
    run_id: str
    code_sha256: str
    frozen_experiment_sha256: str
    fee_facts_sha256: str
    latency_receipt_sha256: str
    risk_policy_sha256: str
    terminal_contract_sha256: str
    releases: tuple[ExactReleaseBinding, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "pnl-spine-run-binding-v1":
            raise ProvenanceError("unknown run-binding schema_version")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ProvenanceError("run_id is required")
        for name in (
            "code_sha256",
            "frozen_experiment_sha256",
            "fee_facts_sha256",
            "latency_receipt_sha256",
            "risk_policy_sha256",
            "terminal_contract_sha256",
        ):
            _sha(name, getattr(self, name))
        if not self.releases:
            raise ProvenanceError("at least one exact release is required")
        dates = [row.date for row in self.releases]
        release_ids = [row.release_id for row in self.releases]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ProvenanceError("release dates must be unique and sorted")
        if len(release_ids) != len(set(release_ids)):
            raise ProvenanceError("duplicate release_id")

    def canonical_payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.canonical_payload()))


def validate_local_payloads(
    root: Path,
    objects: Iterable[ExactSourceObject],
    *,
    local_paths: Mapping[tuple[str, str], str],
) -> dict[str, int]:
    """Validate materialized payloads without weakening exact-version binding."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError(f"payload root is unavailable: {exc}") from exc
    if not resolved_root.is_dir():
        raise ProvenanceError("payload root must be a directory")
    checked = 0
    bytes_checked = 0
    for source in objects:
        identity = (source.logical_key, source.version_id)
        relative_value = local_paths.get(identity)
        if relative_value is None:
            raise ProvenanceError("exact source object has no local materialization")
        relative = _safe_logical_key(relative_value)
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        cursor = root
        for part in PurePosixPath(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ProvenanceError("local payload path contains a symlink")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_root)
            mode = resolved.stat().st_mode
        except (OSError, ValueError) as exc:
            raise ProvenanceError("local payload escapes the trusted root") from exc
        if not stat.S_ISREG(mode):
            raise ProvenanceError("local payload is not a regular file")
        size = resolved.stat().st_size
        if size != source.size_bytes:
            raise ProvenanceError("local payload byte size mismatch")
        if sha256_file(resolved) != source.sha256:
            raise ProvenanceError("local payload SHA-256 mismatch")
        checked += 1
        bytes_checked += size
    return {"objects_checked": checked, "bytes_checked": bytes_checked}


def atomic_write_receipt(path: Path, payload: Mapping[str, Any]) -> str:
    """Write canonical JSON atomically and return its content SHA-256."""
    raw = canonical_json_bytes(dict(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return sha256_bytes(raw)
