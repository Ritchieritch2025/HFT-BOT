#!/usr/bin/env python3
"""Build real Stage-2 V1/V2 receipts from the published local receipt chain.

This is a source-preparation utility only.  It reads exactly the published
2026-07-20/21/22 STATUS -> DURABLE -> TAGGED-DURABLE -> receipt chain,
reuses the V1 eligibility verifier, and then asks V2 to hash the six local
warehouse parents.  It does not invoke V3, fit a model, connect V4, or submit
orders.

Every referenced local object is opened beneath an explicit root using a
component-by-component ``O_NOFOLLOW`` descriptor walk.  Output files are
canonical JSON plus one newline and are created with ``O_EXCL``.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any

from tools.research.crypto_mm import round4_stage2_postfill_extractor as V1
from tools.research.crypto_mm import round4_stage2_postfill_extractor_v2 as V2


REAL_AUTHORITY_SCHEMA = "round4-stage2-real-authority-builder-v1"
SOURCE_AUTHORITY_FILENAME = (
    "round4_stage2_source_preparation_authority_v1.json"
)
BOUND_MANIFEST_FILENAME = "round4_stage2_bound_input_manifest_v2.json"
MAX_CONTROL_JSON_BYTES = 32 * 1024 * 1024
_STATUS_DIR_RE = re.compile(r"^receipt=([0-9a-f]{64})$")
_TAGGED_INDEX_RE = re.compile(r"^TAGGED-DURABLE-([0-9a-f]{64})\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RealAuthorityError(RuntimeError):
    """A local path, byte binding, chain, or output invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise RealAuthorityError(code, detail)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", label)
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("INVALID_TEXT", label)
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail("INVALID_SIZE", label)
    return value


def _json_mapping(raw: bytes, label: str) -> Mapping[str, object]:
    def unique_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("DUPLICATE_JSON_KEY", f"{label}:{key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except RealAuthorityError:
        raise
    except Exception as exc:
        _fail("INVALID_JSON", f"{label}:{exc}")
    if not isinstance(value, Mapping):
        _fail("INVALID_JSON", f"{label}:root must be an object")
    return value


def _absolute_root(value: Path | str, label: str) -> Path:
    text = os.fspath(value)
    if not text or "\x00" in text or not os.path.isabs(text):
        _fail("ROOT_PATH_INVALID", f"{label}:{text!r}")
    raw_parts = PurePosixPath(text).parts
    if any(part in (".", "..") for part in raw_parts):
        _fail("PATH_TRAVERSAL", f"{label}:{text}")
    normalized = os.path.normpath(text)
    if normalized != text.rstrip("/") and not (
        normalized == "/" and text == "/"
    ):
        _fail("ROOT_PATH_INVALID", f"{label}:non-canonical:{text}")
    return Path(normalized)


def _platform_flags() -> tuple[int, int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if nofollow == 0 or directory == 0:
        _fail(
            "PLATFORM_UNSUPPORTED",
            "O_NOFOLLOW and O_DIRECTORY are required",
        )
    return nofollow, directory, cloexec


def _component(value: str, label: str) -> str:
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        _fail("PATH_TRAVERSAL", f"{label}:{value!r}")
    return value


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> int:
    name = _component(name, label)
    nofollow, directory, cloexec = _platform_flags()
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        _fail("PATH_UNREADABLE", f"{label}:{name}:{exc}")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("SYMLINK_FORBIDDEN", f"{label}:{name}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("DIRECTORY_REQUIRED", f"{label}:{name}")
    try:
        return os.open(
            name,
            os.O_RDONLY | directory | nofollow | cloexec,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            _fail("SYMLINK_FORBIDDEN", f"{label}:{name}")
        _fail("PATH_UNREADABLE", f"{label}:{name}:{exc}")


def _open_root_directory(root: Path, label: str) -> int:
    nofollow, directory, cloexec = _platform_flags()
    try:
        current = os.open(
            root.anchor,
            os.O_RDONLY | directory | nofollow | cloexec,
        )
    except OSError as exc:
        _fail("ROOT_PATH_INVALID", f"{label}:{root}:{exc}")
    try:
        for index, part in enumerate(root.parts[1:]):
            next_fd = _open_child_directory(
                current,
                part,
                label=f"{label}:component:{index}",
            )
            os.close(current)
            current = next_fd
        return current
    except Exception:
        os.close(current)
        raise


def _open_directory_under(
    root: Path,
    parts: Sequence[str],
    *,
    label: str,
) -> int:
    current = _open_root_directory(root, label)
    try:
        for index, part in enumerate(parts):
            next_fd = _open_child_directory(
                current,
                part,
                label=f"{label}:relative:{index}",
            )
            os.close(current)
            current = next_fd
        return current
    except Exception:
        os.close(current)
        raise


@dataclass(frozen=True)
class FileEvidence:
    path: str
    sha256: str
    size: int

    def receipt(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }


def _read_file_under(
    root: Path,
    parts: Sequence[str],
    *,
    label: str,
    maximum_bytes: int = MAX_CONTROL_JSON_BYTES,
) -> tuple[bytes, FileEvidence]:
    if not parts:
        _fail("PATH_TRAVERSAL", f"{label}:empty relative path")
    clean = tuple(
        _component(str(part), f"{label}:component")
        for part in parts
    )
    directory_fd = _open_directory_under(
        root,
        clean[:-1],
        label=label,
    )
    source_fd: int | None = None
    try:
        name = clean[-1]
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            _fail("PATH_UNREADABLE", f"{label}:{name}:{exc}")
        if stat.S_ISLNK(metadata.st_mode):
            _fail("SYMLINK_FORBIDDEN", f"{label}:{name}")
        if not stat.S_ISREG(metadata.st_mode):
            _fail("REGULAR_FILE_REQUIRED", f"{label}:{name}")
        nofollow, _directory, cloexec = _platform_flags()
        try:
            source_fd = os.open(
                name,
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                _fail("SYMLINK_FORBIDDEN", f"{label}:{name}")
            _fail("PATH_UNREADABLE", f"{label}:{name}:{exc}")
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            _fail("REGULAR_FILE_REQUIRED", f"{label}:{name}")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            _fail(
                "CONTROL_FILE_SIZE_INVALID",
                f"{label}:{before.st_size}",
            )
        chunks: list[bytes] = []
        total = 0
        digest = hashlib.sha256()
        while True:
            block = os.read(source_fd, min(1024 * 1024, maximum_bytes + 1))
            if not block:
                break
            total += len(block)
            if total > maximum_bytes:
                _fail("CONTROL_FILE_SIZE_INVALID", f"{label}:too large")
            chunks.append(block)
            digest.update(block)
        after = os.fstat(source_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or total != after.st_size:
            _fail("CONTROL_FILE_MUTATED", label)
        raw = b"".join(chunks)
        return raw, FileEvidence(
            path=str(root.joinpath(*clean)),
            sha256=digest.hexdigest(),
            size=total,
        )
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(directory_fd)


def _read_json_under(
    root: Path,
    parts: Sequence[str],
    *,
    label: str,
) -> tuple[Mapping[str, object], bytes, FileEvidence]:
    raw, evidence = _read_file_under(root, parts, label=label)
    return _json_mapping(raw, label), raw, evidence


def _discover_status(
    live_root: Path,
    day: str,
) -> tuple[Mapping[str, object], FileEvidence, str]:
    day_parts = ("research_v3_daily", f"date={day}")
    day_fd = _open_directory_under(
        live_root,
        day_parts,
        label=f"{day}:status-day",
    )
    candidates: list[tuple[tuple[str, ...], str]] = []
    try:
        for name in sorted(os.listdir(day_fd)):
            match = _STATUS_DIR_RE.fullmatch(name)
            if match is None:
                continue
            receipt_fd = _open_child_directory(
                day_fd,
                name,
                label=f"{day}:status-receipt-directory",
            )
            try:
                try:
                    metadata = os.stat(
                        "STATUS.json",
                        dir_fd=receipt_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _fail("PATH_UNREADABLE", f"{day}:STATUS.json:{exc}")
                if stat.S_ISLNK(metadata.st_mode):
                    _fail(
                        "SYMLINK_FORBIDDEN",
                        f"{day}:{name}/STATUS.json",
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    _fail(
                        "REGULAR_FILE_REQUIRED",
                        f"{day}:{name}/STATUS.json",
                    )
                candidates.append(
                    ((*day_parts, name, "STATUS.json"), match.group(1))
                )
            finally:
                os.close(receipt_fd)
    finally:
        os.close(day_fd)
    if not candidates:
        _fail(
            "STATUS_CARDINALITY_MISMATCH",
            f"{day}:published_v2=0:total_status=0",
        )
    loaded: list[
        tuple[Mapping[str, object], FileEvidence, str]
    ] = []
    for index, (parts, directory_digest) in enumerate(candidates):
        status, _raw, evidence = _read_json_under(
            live_root,
            parts,
            label=f"{day}:STATUS:{index}",
        )
        loaded.append((status, evidence, directory_digest))
    published = [
        item
        for item in loaded
        if (
            item[0].get("schema_version")
            == "research-v3-daily-status-v2"
            and item[0].get("state") == "V3_REFERENCE_PUBLISHED"
            and item[0].get("date") == day
        )
    ]
    if len(published) == 1:
        return published[0]
    # Preserve a precise wrong-status rejection when there is only one
    # terminal file; multiple possible authorities are a cardinality error.
    if len(loaded) == 1:
        return loaded[0]
    _fail(
        "STATUS_CARDINALITY_MISMATCH",
        (
            f"{day}:published_v2={len(published)}"
            f":total_status={len(loaded)}"
        ),
    )


def _validate_status(
    *,
    status: Mapping[str, object],
    evidence: FileEvidence,
    day: str,
    directory_digest: str,
    live_root: Path,
) -> str:
    durable_digest = _require_sha256(
        status.get("durable_receipt_set_sha256"),
        f"{day}:durable_receipt_set_sha256",
    )
    if (
        status.get("schema_version") != "research-v3-daily-status-v2"
        or status.get("state") != "V3_REFERENCE_PUBLISHED"
        or status.get("date") != day
        or status.get("manifest_commit_state")
        != "REFERENCE_MANIFEST_COMMITTED"
        or durable_digest != directory_digest
        or status.get("rfq") != "OFF"
        or status.get("live_dir") != str(live_root)
    ):
        _fail("STATUS_NOT_PUBLISHED", evidence.path)
    return durable_digest


def _exact_tagged_index_parts(
    *,
    tagged_index: object,
    live_root: Path,
    day: str,
) -> tuple[tuple[str, ...], str]:
    value = _require_text(tagged_index, f"{day}:tagged_index")
    if "\x00" in value or "\\" in value or not value.startswith("/"):
        _fail("PATH_TRAVERSAL", f"{day}:tagged_index:{value!r}")
    path = PurePosixPath(value)
    if any(part in (".", "..") for part in path.parts):
        _fail("PATH_TRAVERSAL", f"{day}:tagged_index:{value}")
    expected_parent = (
        live_root
        / "canonical_receipts"
        / "tagged"
        / f"date={day}"
    )
    lexical = Path(value)
    if lexical.parent != expected_parent:
        _fail(
            "TAGGED_INDEX_PATH_MISMATCH",
            f"{day}:expected-parent={expected_parent}:actual={lexical}",
        )
    match = _TAGGED_INDEX_RE.fullmatch(lexical.name)
    if match is None:
        _fail("TAGGED_INDEX_PATH_MISMATCH", f"{day}:{lexical.name}")
    try:
        relative = lexical.relative_to(live_root)
    except ValueError:
        _fail("PATH_TRAVERSAL", f"{day}:tagged_index:{value}")
    return tuple(relative.parts), match.group(1)


def _validate_receipt_binding(
    *,
    root: Path,
    index: Mapping[str, object],
    index_evidence: FileEvidence,
    index_parts: Sequence[str],
    day: str,
    expected_digest: str,
    tagged: bool,
    byte_attestation_digest: str | None = None,
) -> tuple[Mapping[str, object], bytes, FileEvidence]:
    digest = _require_sha256(
        index.get("receipt_set_sha256"),
        f"{day}:receipt_set_sha256",
    )
    binding = index.get("receipt_object")
    if not isinstance(binding, Mapping):
        _fail("RECEIPT_BINDING_INVALID", f"{day}:receipt_object")
    expected_index_name = (
        f"TAGGED-DURABLE-{digest}.json"
        if tagged
        else f"DURABLE-{digest}.json"
    )
    if (
        digest != expected_digest
        or Path(index_evidence.path).name != expected_index_name
        or index.get("schema_version")
        != "canonical-durable-receipt-index-v1"
        or index.get("state") != "DURABLE_RECEIPT_VERIFIED"
        or index.get("date") != day
        or index.get("complete") is not True
    ):
        _fail("RECEIPT_INDEX_INVALID", index_evidence.path)
    if tagged and (
        index.get("completed") is not True
        or index.get("receipt_phase")
        != "TAGGED_ELIGIBILITY_VERIFIED"
        or index.get("byte_attestation_receipt_set_sha256")
        != byte_attestation_digest
        or index.get("receipt_object_eligibility_tag_state")
        != "TAGGED_VERIFIED"
    ):
        _fail("RECEIPT_INDEX_INVALID", index_evidence.path)

    receipt_name = f"receipt-{digest}.json"
    key = _require_text(
        binding.get("key"),
        f"{day}:receipt_object:key",
    )
    expected_key = (
        f"ec2/control/canonical-receipts/v1/date={day}/{receipt_name}"
    )
    expected_size = _require_positive_int(
        binding.get("size"),
        f"{day}:receipt_object:size",
    )
    expected_sha = _require_sha256(
        binding.get("sha256"),
        f"{day}:receipt_object:sha256",
    )
    if (
        key != expected_key
        or not isinstance(binding.get("bucket"), str)
        or not binding.get("bucket")
        or not isinstance(binding.get("VersionId"), str)
        or not binding.get("VersionId")
        or binding.get("verification_state")
        != "EXACT_VERSION_FULL_SHA256"
    ):
        _fail("RECEIPT_BINDING_INVALID", f"{day}:{receipt_name}")
    receipt_parts = (*tuple(index_parts[:-1]), receipt_name)
    receipt, raw, evidence = _read_json_under(
        root,
        receipt_parts,
        label=f"{day}:{'tagged' if tagged else 'durable'}-receipt",
    )
    if evidence.size != expected_size or evidence.sha256 != expected_sha:
        _fail(
            "RECEIPT_BYTE_MISMATCH",
            (
                f"{evidence.path}:expected=({expected_size},{expected_sha})"
                f":actual=({evidence.size},{evidence.sha256})"
            ),
        )
    if tagged and (
        index.get("receipt_payload_size") != evidence.size
        or index.get("receipt_payload_sha256") != evidence.sha256
    ):
        _fail("RECEIPT_BYTE_MISMATCH", f"{day}:tagged payload fields")
    if (
        receipt.get("schema_version") != "canonical-object-receipt-v1"
        or receipt.get("state") != "DURABLE_RECEIPT_VERIFIED"
        or receipt.get("date") != day
        or receipt.get("receipt_set_sha256") != digest
        or receipt.get("authority") != "CANONICAL_CONTROL_PLANE"
        or receipt.get("authoritative") is not True
        or receipt.get("s3_published") is not True
    ):
        _fail("RECEIPT_PAYLOAD_INVALID", evidence.path)
    if tagged:
        if (
            receipt.get("receipt_phase")
            != "TAGGED_ELIGIBILITY_VERIFIED"
            or receipt.get("byte_attestation_receipt_set_sha256")
            != byte_attestation_digest
        ):
            _fail("RECEIPT_PAYLOAD_INVALID", evidence.path)
    else:
        seal = receipt.get("seal")
        if not isinstance(seal, Mapping) or (
            seal.get("date") != day
            or seal.get("status") != "SEALED"
            or seal.get("version") != 2
        ):
            _fail("RECEIPT_PAYLOAD_INVALID", evidence.path)
    return receipt, raw, evidence


@dataclass(frozen=True)
class DayChainEvidence:
    source_date_utc: str
    durable_receipt_set_sha256: str
    tagged_receipt_set_sha256: str
    status: FileEvidence
    durable_index: FileEvidence
    durable_receipt: FileEvidence
    tagged_index: FileEvidence
    tagged_receipt: FileEvidence

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "durable_receipt_set_sha256": (
                self.durable_receipt_set_sha256
            ),
            "tagged_receipt_set_sha256": (
                self.tagged_receipt_set_sha256
            ),
            "status": self.status.receipt(),
            "durable_index": self.durable_index.receipt(),
            "durable_receipt": self.durable_receipt.receipt(),
            "tagged_index": self.tagged_index.receipt(),
            "tagged_receipt": self.tagged_receipt.receipt(),
        }


@dataclass(frozen=True)
class RealAuthorityBuild:
    authority: V1.SourcePreparationAuthority
    bound_manifest: V2.BoundInputManifestV2
    chain: tuple[DayChainEvidence, ...]


def build_real_source_receipts(
    *,
    live_root: Path | str,
    content_root: Path | str,
    dates: Sequence[str] = V1.DISCOVERY_DATES,
) -> RealAuthorityBuild:
    """Verify the exact three-day chain and derive typed V1/V2 receipts."""
    selected_dates = tuple(dates)
    if any(day in V1.FORBIDDEN_DATES for day in selected_dates):
        _fail("FORBIDDEN_DATE", repr(selected_dates))
    if selected_dates != V1.DISCOVERY_DATES:
        _fail(
            "DATE_SET_MISMATCH",
            (
                f"expected={V1.DISCOVERY_DATES}"
                f":actual={selected_dates}"
            ),
        )
    live = _absolute_root(live_root, "live-root")
    content = _absolute_root(content_root, "content-root")
    probe_fd = _open_root_directory(live, "live-root")
    os.close(probe_fd)

    eligible_days: dict[str, V1.EligibleDay] = {}
    chain: list[DayChainEvidence] = []
    for day in selected_dates:
        status, status_evidence, directory_digest = _discover_status(
            live,
            day,
        )
        durable_digest = _validate_status(
            status=status,
            evidence=status_evidence,
            day=day,
            directory_digest=directory_digest,
            live_root=live,
        )
        durable_parts = (
            "canonical_receipts",
            "durable",
            f"date={day}",
            f"DURABLE-{durable_digest}.json",
        )
        durable_index, _durable_index_raw, durable_index_evidence = (
            _read_json_under(
                live,
                durable_parts,
                label=f"{day}:DURABLE",
            )
        )
        (
            _durable_receipt,
            _durable_receipt_raw,
            durable_receipt_evidence,
        ) = _validate_receipt_binding(
            root=live,
            index=durable_index,
            index_evidence=durable_index_evidence,
            index_parts=durable_parts,
            day=day,
            expected_digest=durable_digest,
            tagged=False,
        )

        tagged_parts, tagged_digest = _exact_tagged_index_parts(
            tagged_index=status.get("tagged_index"),
            live_root=live,
            day=day,
        )
        tagged_index, _tagged_index_raw, tagged_index_evidence = (
            _read_json_under(
                live,
                tagged_parts,
                label=f"{day}:TAGGED-DURABLE",
            )
        )
        tagged_receipt, tagged_receipt_raw, tagged_receipt_evidence = (
            _validate_receipt_binding(
                root=live,
                index=tagged_index,
                index_evidence=tagged_index_evidence,
                index_parts=tagged_parts,
                day=day,
                expected_digest=tagged_digest,
                tagged=True,
                byte_attestation_digest=durable_digest,
            )
        )
        # Parse and byte-bind above, then reuse the published V1 authority
        # contract rather than duplicating its eligibility policy here.
        del tagged_receipt
        eligible_days[day] = V1.verify_day_eligibility(
            day=day,
            status=status,
            durable_index=tagged_index,
            tagged_receipt_bytes=tagged_receipt_raw,
            required_logical_sources=V1.required_btc_fact_keys(day),
        )
        chain.append(
            DayChainEvidence(
                source_date_utc=day,
                durable_receipt_set_sha256=durable_digest,
                tagged_receipt_set_sha256=tagged_digest,
                status=status_evidence,
                durable_index=durable_index_evidence,
                durable_receipt=durable_receipt_evidence,
                tagged_index=tagged_index_evidence,
                tagged_receipt=tagged_receipt_evidence,
            )
        )

    authority = V1.prepare_source_authority(
        eligible_days=eligible_days,
    )
    bound_manifest = V2.build_bound_input_manifest_v2(
        authority=authority,
        content_root=content,
    )
    return RealAuthorityBuild(
        authority=authority,
        bound_manifest=bound_manifest,
        chain=tuple(chain),
    )


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            _fail("OUTPUT_WRITE_FAILED", "short write")
        offset += written


def _exclusive_write_pair(
    *,
    output_dir: Path,
    authority_bytes: bytes,
    manifest_bytes: bytes,
) -> None:
    directory_fd = _open_root_directory(output_dir, "output-dir")
    descriptors: dict[str, int] = {}
    created: list[str] = []
    nofollow, _directory, cloexec = _platform_flags()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
    try:
        for name in (SOURCE_AUTHORITY_FILENAME, BOUND_MANIFEST_FILENAME):
            try:
                descriptor = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                _fail("OUTPUT_EXISTS", str(output_dir / name))
            except OSError as exc:
                _fail("OUTPUT_CREATE_FAILED", f"{output_dir / name}:{exc}")
            descriptors[name] = descriptor
            created.append(name)
        _write_all(
            descriptors[SOURCE_AUTHORITY_FILENAME],
            authority_bytes,
        )
        _write_all(
            descriptors[BOUND_MANIFEST_FILENAME],
            manifest_bytes,
        )
        for descriptor in descriptors.values():
            os.fsync(descriptor)
        os.fsync(directory_fd)
    except Exception:
        for descriptor in descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        descriptors.clear()
        for name in created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        for descriptor in descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        os.close(directory_fd)


def materialize_real_source_receipts(
    *,
    live_root: Path | str,
    content_root: Path | str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Build and exclusively write the exact V1/V2 JSON receipts."""
    result = build_real_source_receipts(
        live_root=live_root,
        content_root=content_root,
    )
    output = _absolute_root(output_dir, "output-dir")
    authority_bytes = _canonical_json_bytes(result.authority.receipt())
    manifest_bytes = _canonical_json_bytes(result.bound_manifest.receipt())
    _exclusive_write_pair(
        output_dir=output,
        authority_bytes=authority_bytes,
        manifest_bytes=manifest_bytes,
    )
    authority_path = output / SOURCE_AUTHORITY_FILENAME
    manifest_path = output / BOUND_MANIFEST_FILENAME
    return {
        "schema": REAL_AUTHORITY_SCHEMA,
        "status": "SOURCE_PREPARATION_RECEIPTS_WRITTEN",
        "source_preparation_only": True,
        "contract_adapter_status": V1.CONTRACT_ADAPTER_STATUS,
        "extraction_authorized": False,
        "live_authorized": False,
        "source_dates": list(V1.DISCOVERY_DATES),
        "forbidden_dates_excluded": sorted(V1.FORBIDDEN_DATES),
        "authority_receipt": {
            "path": str(authority_path),
            "raw_byte_sha256": hashlib.sha256(
                authority_bytes
            ).hexdigest(),
            "size": len(authority_bytes),
        },
        "bound_manifest_receipt": {
            "path": str(manifest_path),
            "raw_byte_sha256": hashlib.sha256(
                manifest_bytes
            ).hexdigest(),
            "size": len(manifest_bytes),
            "file_count": result.bound_manifest.file_count,
            "authority_canonical_payload_sha256": (
                result.bound_manifest.authority_sha256
            ),
        },
        "chain": [item.receipt() for item in result.chain],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build real Stage-2 V1/V2 source-preparation receipts from "
            "the published 2026-07-20/21/22 local receipt chain."
        )
    )
    parser.add_argument("--live-root", required=True)
    parser.add_argument("--content-root", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = materialize_real_source_receipts(
            live_root=args.live_root,
            content_root=args.content_root,
            output_dir=args.output_dir,
        )
    except (
        RealAuthorityError,
        V1.Stage2ExtractionError,
        V2.Stage2PreparationV2Error,
    ) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        detail = getattr(exc, "detail", str(exc))
        sys.stderr.buffer.write(
            _canonical_json_bytes(
                {
                    "schema": REAL_AUTHORITY_SCHEMA,
                    "status": "REJECTED",
                    "code": code,
                    "detail": detail,
                }
            )
        )
        return 2
    sys.stdout.buffer.write(_canonical_json_bytes(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
