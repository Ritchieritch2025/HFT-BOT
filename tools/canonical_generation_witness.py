#!/usr/bin/env python3
"""Publish exact canonical generation witnesses without research-side copies.

``publish-future`` is the producer hook intended to run from the production
``ec2_s3_sync daily`` path after a day is sealed.  It holds shared catalog/dim
generation locks only long enough to verify coherent producer manifests and
copy a private stable snapshot.  After releasing both locks it syncs those
snapshot bytes to the original canonical keys, exact-reads every resulting
VersionId, and conditionally creates one small content-addressed witness.

``migrate-legacy`` is intentionally restricted to 2026-07-10, 2026-07-11,
2026-07-15, and 2026-07-16.  It never uploads a catalog, dim, seal, raw, or
fact object.  It reads bounded S3 version histories, proves the known unique
first complete catalog batch after the *local* seal ``sealed_at``, exact-reads
dated dim and seal versions, writes a local content-addressed history proof,
and conditionally creates only the small witness.

Both commands require the fixed vaultWriter identity and an explicit operator
flag.  They never write below ``research/``, never tag, copy, or delete, and
never include RFQ.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

import canonical_receipts as cr
import forward_canonical_receipts as fcr
import git_provenance as gp
import publication_generation as pg


DEFAULT_BUCKET = "kalshi-vault-ritcardo"
DEFAULT_PREFIX = "ec2"
DEFAULT_PUBLISHER_ARN = "arn:aws:iam::321572485933:user/vaultWriter"
PINNED_AWS_CLI = "/snap/aws-cli/current/bin/aws"
PRODUCTION_RAW_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/raw")
PRODUCTION_WAREHOUSE_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/warehouse")
PRODUCTION_QUALITY_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/event_packs")
PRODUCTION_PROOF_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/canonical_receipts/"
    "generation-migration-proofs")
PRODUCTION_WRITER_LOCK = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/canonical_receipts/"
    "generation-witness.lock")
PRODUCTION_INTENT_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/canonical_receipts/"
    "generation-witness-intents")
DEFAULT_AUTHORIZATION_FILE = pathlib.Path(
    "/etc/kalshi-research-v3/approvals/cutover-approved")
AUTOMATION_AUTHORIZATION_SHA256 = (
    "1b14001428f2387f3e62c531a8d8ce3dd4f8bd726d6c8b94b4d6c0d893020761")
ROOT = pathlib.Path(__file__).resolve().parents[1]
MUTATION_PROVENANCE_PATHS = (
    "tools/git_provenance.py",
    "tools/canonical_receipts.py",
    "tools/publication_generation.py",
    "tools/forward_canonical_receipts.py",
    "tools/canonical_generation_witness.py",
    "tools/canonical_generation_daily.py",
    "docs/plan_releases/pipeline/"
    "W-PUB-REF-01C_AUTOMATION_EXECUTION_AUTHORIZATION_2026-07-17.json",
)
FUTURE_GENERATION_FIRST_DATE = "2026-07-17"
MAX_HISTORY_PAGES = 64
MAX_HISTORY_ROWS = 4096
MAX_WITNESS_BYTES = 8 * 1024 * 1024
MAX_PROOF_BYTES = 16 * 1024 * 1024
MAX_INTENTION_BYTES = 2 * 1024 * 1024
LEGACY_BATCH_WINDOW = dt.timedelta(
    seconds=fcr.MAX_LEGACY_CATALOG_BATCH_WINDOW_SECONDS)
LEGACY_EXPECTED_BATCH_MINUTES = {
    "2026-07-10": "2026-07-12T03:10",
    "2026-07-11": "2026-07-13T03:10",
    "2026-07-15": "2026-07-17T03:10",
    "2026-07-16": "2026-07-17T22:07",
}


class WitnessError(RuntimeError):
    """One stable fail-closed producer or migration refusal."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _read_authorization(path: pathlib.Path) -> str:
    path = pathlib.Path(path)
    if path != DEFAULT_AUTHORIZATION_FILE:
        raise WitnessError(
            "AUTHORIZATION_INVALID", "authorization path is fixed")
    try:
        item = os.stat(path, follow_symlinks=False)
        parent = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise WitnessError("AUTHORIZATION_INVALID", str(exc)) from exc
    if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or item.st_uid not in {0, os.geteuid()} or item.st_mode & 0o022
            or item.st_size <= 0 or item.st_size > 64 * 1024
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode) or parent.st_mode & 0o022):
        raise WitnessError(
            "AUTHORIZATION_INVALID", "authorization ownership/mode invalid")
    try:
        raw, _fingerprint = cr._freeze_file(str(path), max_bytes=64 * 1024)
    except cr.ReceiptError as exc:
        raise WitnessError("AUTHORIZATION_INVALID", str(exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != AUTOMATION_AUTHORIZATION_SHA256:
        raise WitnessError(
            "AUTHORIZATION_INVALID", "authorization SHA-256 mismatch")
    return digest


def _clean_mutation_provenance() -> str:
    try:
        return gp.require_clean_head(ROOT, MUTATION_PROVENANCE_PATHS)
    except gp.GitProvenanceError as exc:
        raise WitnessError(exc.code, exc.detail) from exc


def _sterile_publisher_environment() -> dict[str, str]:
    allowed = {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION", "AWS_REGION",
    }
    if (os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
            or os.environ.get("AWS_CONFIG_FILE")):
        raise WitnessError(
            "PUBLISHER_CREDENTIAL_MODE_INVALID",
            "profile/shared AWS configuration is forbidden")
    values = {name: os.environ[name] for name in allowed
              if os.environ.get(name)}
    if (not values.get("AWS_ACCESS_KEY_ID")
            or not values.get("AWS_SECRET_ACCESS_KEY")):
        raise WitnessError(
            "PUBLISHER_CREDENTIAL_MODE_INVALID", "publisher keys missing")
    values.setdefault("AWS_DEFAULT_REGION", "us-east-2")
    values.setdefault("AWS_REGION", values["AWS_DEFAULT_REGION"])
    return {
        "HOME": "/nonexistent", "USER": "canonical-generation-witness",
        "LOGNAME": "canonical-generation-witness",
        "PATH": "/snap/aws-cli/current/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "AWS_PAGER": "",
        "AWS_EC2_METADATA_DISABLED": "true", **values,
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CLI_HISTORY_FILE": "/dev/null",
        "AWS_CLI_HISTORY_ENABLED": "false",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_CLI_AUTO_PROMPT": "off",
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
    }


def _exact_existing_production_dir(path, expected, label):
    path = pathlib.Path(path)
    expected = pathlib.Path(expected)
    if not path.is_absolute():
        raise WitnessError("PRODUCTION_PATH_INVALID", f"{label} not absolute")
    try:
        actual = path.resolve(strict=True)
        wanted = expected.resolve(strict=True)
    except OSError as exc:
        raise WitnessError("PRODUCTION_PATH_INVALID", f"{label}: {exc}") from exc
    if (path != expected or actual != wanted or actual != expected
            or not actual.is_dir()):
        raise WitnessError(
            "PRODUCTION_PATH_INVALID", f"{label} resolved to {actual}")
    return actual


@contextlib.contextmanager
def _production_writer_lock(client):
    """Serialize every real producer/migration run before any remote read."""
    if not isinstance(client, AwsCli):
        yield
        return
    parent = _exact_existing_production_dir(
        PRODUCTION_WRITER_LOCK.parent,
        PRODUCTION_WRITER_LOCK.parent, "generation writer lock parent")
    path = parent / PRODUCTION_WRITER_LOCK.name
    if path != PRODUCTION_WRITER_LOCK:
        raise WitnessError(
            "PRODUCTION_PATH_INVALID", "writer lock path is not fixed")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise WitnessError("PRODUCER_LOCK_INVALID", str(exc)) from exc
    try:
        item = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(item.st_mode) or not stat.S_ISREG(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or item.st_uid != os.geteuid()
                or stat.S_IMODE(item.st_mode) != 0o600
                or (item.st_dev, item.st_ino) != (named.st_dev, named.st_ino)):
            raise WitnessError(
                "PRODUCER_LOCK_INVALID", "writer lock ownership/mode invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise WitnessError(
                    "PRODUCER_BUSY", "another generation writer is active") \
                    from exc
            raise WitnessError("PRODUCER_LOCK_INVALID", str(exc)) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _valid_version(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and value.strip().lower() != "null")


def _utc(value, label: str) -> str:
    try:
        return cr._canonical_utc(value, label)
    except cr.ReceiptError as exc:
        raise WitnessError("REMOTE_METADATA_INVALID", str(exc)) from exc


def _utc_time(value, label: str) -> dt.datetime:
    canonical = _utc(value, label)
    return dt.datetime.fromisoformat(canonical.replace("Z", "+00:00"))


def _attest_file(path: pathlib.Path) -> tuple[int, str]:
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WitnessError("LOCAL_INPUT_INVALID", f"{path}: {exc}") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WitnessError("LOCAL_INPUT_INVALID", str(path))
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda item: (
            item.st_dev, item.st_ino, item.st_size,
            item.st_mtime_ns, item.st_ctime_ns)
        if (not stat.S_ISREG(named.st_mode)
                or identity(before) != identity(after)
                or (named.st_dev, named.st_ino)
                != (after.st_dev, after.st_ino)
                or size != after.st_size):
            raise WitnessError("LOCAL_INPUT_CHANGED", str(path))
        return size, digest.hexdigest()
    except OSError as exc:
        raise WitnessError("LOCAL_INPUT_CHANGED", f"{path}: {exc}") from exc
    finally:
        os.close(fd)


def _snapshot_file(source, destination, expected):
    """Atomically copy one pinned member while its shared lock is held."""
    source = pathlib.Path(source).absolute()
    destination = pathlib.Path(destination).absolute()
    _secure_directory(destination.parent)
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    source_fd = output_fd = None
    temporary = None
    try:
        source_fd = os.open(source, read_flags)
        output_fd, temporary_value = tempfile.mkstemp(
            prefix=".tmp-snapshot-", dir=str(destination.parent))
        temporary = pathlib.Path(temporary_value)
        os.fchmod(output_fd, 0o600)
    except OSError as exc:
        if output_fd is not None:
            os.close(output_fd)
        if source_fd is not None:
            os.close(source_fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise WitnessError("LOCAL_SNAPSHOT_INVALID", str(exc)) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise WitnessError("LOCAL_SNAPSHOT_INVALID", str(source))
        while True:
            chunk = os.read(source_fd, 1 << 20)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise WitnessError(
                        "LOCAL_SNAPSHOT_INVALID", "short snapshot write")
                view = view[written:]
            digest.update(chunk)
            size += len(chunk)
        os.fsync(output_fd)
        after = os.fstat(source_fd)
        named = os.stat(source, follow_symlinks=False)
        identity = lambda item: (
            item.st_dev, item.st_ino, item.st_size,
            item.st_mtime_ns, item.st_ctime_ns)
        observed = (size, digest.hexdigest())
        if (not stat.S_ISREG(named.st_mode)
                or identity(before) != identity(after)
                or (named.st_dev, named.st_ino)
                != (after.st_dev, after.st_ino)
                or observed != expected):
            raise WitnessError(
                "LOCAL_SNAPSHOT_INVALID", f"changed/mismatched {source}")
    except OSError as exc:
        raise WitnessError("LOCAL_SNAPSHOT_INVALID", str(exc)) from exc
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if source_fd is not None:
            os.close(source_fd)
    if _attest_file(temporary) != expected:
        raise WitnessError(
            "LOCAL_SNAPSHOT_INVALID", f"snapshot readback {temporary}")
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError:
        if _attest_file(destination) != expected:
            raise WitnessError(
                "LOCAL_SNAPSHOT_INVALID",
                f"snapshot destination conflict {destination}")
    except OSError as exc:
        raise WitnessError("LOCAL_SNAPSHOT_INVALID", str(exc)) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _fsync_directory(destination.parent)
    _secure_regular_file(destination, expected=expected)
    return destination


def _fsync_directory(path):
    """Persist one directory entry transaction without following a link."""
    path = pathlib.Path(path).absolute()
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"cannot fsync {path}: {exc}") \
            from exc
    try:
        opened = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (named.st_dev, named.st_ino)):
            raise WitnessError(
                "LOCAL_INTENTION_INVALID", f"directory changed: {path}")
        os.fsync(fd)
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"cannot fsync {path}: {exc}") \
            from exc
    finally:
        os.close(fd)


def _secure_directory(path, *, create=False):
    """Require an euid-owned, non-link 0700 directory."""
    path = pathlib.Path(path).absolute()
    created = False
    if create:
        try:
            os.mkdir(path, 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise WitnessError(
                "LOCAL_INTENTION_INVALID", f"cannot create {path}: {exc}") \
                from exc
    try:
        item = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"cannot inspect {path}: {exc}") \
            from exc
    if (not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o700):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID",
            f"directory ownership/mode invalid: {path}")
    if created:
        _fsync_directory(path.parent)
    return path


def _secure_regular_file(path, *, expected=None, max_bytes=None):
    """Attest an euid-owned, non-link 0600 persistent control or snapshot."""
    path = pathlib.Path(path).absolute()
    try:
        item = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"cannot inspect {path}: {exc}") \
            from exc
    if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o600
            or (max_bytes is not None
                and (item.st_size <= 0 or item.st_size > max_bytes))):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID",
            f"file ownership/mode/size invalid: {path}")
    observed = _attest_file(path)
    if expected is not None and observed != expected:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", f"persistent bytes differ: {path}")
    return observed


def _persist_content_addressed_json(parent, label, payload):
    """Atomically create one canonical 0600 JSON object and fsync its name."""
    parent = _secure_directory(parent)
    raw = cr.canonical_bytes(payload)
    if not raw or len(raw) > MAX_INTENTION_BYTES:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"{label} size {len(raw)}")
    digest = hashlib.sha256(raw).hexdigest()
    final = parent / f"{label}-{digest}.json"
    expected = (len(raw), digest)
    if os.path.lexists(final):
        _secure_regular_file(
            final, expected=expected, max_bytes=MAX_INTENTION_BYTES)
        return final, digest
    fd = None
    temporary = None
    try:
        fd, temporary_value = tempfile.mkstemp(
            prefix=f".tmp-{label}-", dir=str(parent))
        temporary = pathlib.Path(temporary_value)
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise WitnessError(
                    "LOCAL_INTENTION_INVALID", "short intention write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(temporary, final, follow_symlinks=False)
        except FileExistsError:
            pass
        _secure_regular_file(
            final, expected=expected, max_bytes=MAX_INTENTION_BYTES)
        _fsync_directory(parent)
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"cannot persist {label}: {exc}") \
            from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return final, digest


def _intention_storage_root(client, warehouse_root, requested=None):
    """Resolve the test root or the one fixed production receipt subtree."""
    if isinstance(client, AwsCli):
        if (requested is not None
                and pathlib.Path(requested) != PRODUCTION_INTENT_ROOT):
            raise WitnessError(
                "PRODUCTION_PATH_INVALID", "intention root is fixed")
        parent = _exact_existing_production_dir(
            PRODUCTION_INTENT_ROOT.parent,
            PRODUCTION_INTENT_ROOT.parent, "canonical receipt root")
        if PRODUCTION_INTENT_ROOT.parent != parent:
            raise WitnessError(
                "PRODUCTION_PATH_INVALID", "intention parent is fixed")
        return _secure_directory(PRODUCTION_INTENT_ROOT, create=True)
    root = (pathlib.Path(requested).absolute() if requested is not None
            else pathlib.Path(warehouse_root).absolute().parent
            / "canonical_receipts" / "generation-witness-intents")
    parent = root.parent
    if not os.path.lexists(parent):
        try:
            os.mkdir(parent, 0o700)
        except OSError as exc:
            raise WitnessError(
                "LOCAL_INTENTION_INVALID",
                f"cannot create intention parent: {exc}") from exc
        _fsync_directory(parent.parent)
    _secure_directory(parent)
    return _secure_directory(root, create=True)


def _future_snapshot_members(date, seal_binding, catalog, dim):
    seal_rel = f"seals/date={date}.json"
    rows = [{
        "source_relative_path": seal_rel,
        "snapshot_relative_path": seal_rel,
        "logical_source_key": "warehouse/" + seal_rel,
        "key": cr._join_key(DEFAULT_PREFIX, "warehouse", seal_rel),
        "size": seal_binding["size"],
        "sha256": seal_binding["sha256"],
    }]
    for manifest in (catalog, dim):
        for item in manifest["files"]:
            rel = pg.safe_relative_path(item["relative_path"])
            rows.append({
                "source_relative_path": rel,
                "snapshot_relative_path": rel,
                "logical_source_key": "warehouse/" + rel,
                "key": cr._join_key(DEFAULT_PREFIX, "warehouse", rel),
                "size": item["size"], "sha256": item["sha256"],
            })
    rows.sort(key=lambda row: row["logical_source_key"])
    if len({row["logical_source_key"] for row in rows}) != len(rows):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "duplicate snapshot member")
    return rows


def _build_future_intention(date, seal, seal_binding, catalog, dim):
    members = _future_snapshot_members(
        date, seal_binding, catalog, dim)
    return {
        "schema_version": "canonical-generation-upload-intention-v1",
        "state": "PERSISTED_BEFORE_FIRST_REMOTE_PUT",
        "generation_authority": fcr.GENERATION_AUTHORITY_PRODUCER,
        "catalog_dim_coherence_claim": True,
        "date": date, "bucket": DEFAULT_BUCKET, "prefix": DEFAULT_PREFIX,
        "seal": {
            "relative_path": f"seals/date={date}.json",
            "size": seal_binding["size"],
            "sha256": seal_binding["sha256"],
            "sealed_at_utc": _utc(
                seal.get("sealed_at"), "local seal sealed_at"),
        },
        "catalog_generation": catalog,
        "dim_generation": dim,
        "snapshot_members": members,
        "snapshot_set_sha256": cr.canonical_sha256(members),
    }


def _validate_future_intention(payload, date, seal, seal_binding):
    required = {
        "schema_version", "state", "generation_authority",
        "catalog_dim_coherence_claim", "date", "bucket", "prefix",
        "seal", "catalog_generation", "dim_generation",
        "snapshot_members", "snapshot_set_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "intention shape invalid")
    if (payload["schema_version"]
            != "canonical-generation-upload-intention-v1"
            or payload["state"] != "PERSISTED_BEFORE_FIRST_REMOTE_PUT"
            or payload["generation_authority"]
            != fcr.GENERATION_AUTHORITY_PRODUCER
            or payload["catalog_dim_coherence_claim"] is not True
            or payload["date"] != date
            or payload["bucket"] != DEFAULT_BUCKET
            or payload["prefix"] != DEFAULT_PREFIX):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "intention identity invalid")
    seal_row = payload["seal"]
    if (not isinstance(seal_row, dict) or set(seal_row) != {
            "relative_path", "size", "sha256", "sealed_at_utc"}
            or seal_row["relative_path"] != f"seals/date={date}.json"
            or (seal_row["size"], seal_row["sha256"]) != (
                seal_binding["size"], seal_binding["sha256"])
            or seal_row["sealed_at_utc"] != _utc(
                seal.get("sealed_at"), "local seal sealed_at")):
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "sealed day differs from intention")
    catalog, dim = (payload["catalog_generation"],
                    payload["dim_generation"])
    try:
        if pg.build_manifest(
                "catalog", catalog["files"]) != catalog:
            raise pg.GenerationError("catalog intention is not canonical")
        if pg.build_manifest(
                "dim", dim["files"], date=date,
                source_catalog_generation_id=
                dim["source_catalog_generation_id"]) != dim:
            raise pg.GenerationError("dim intention is not canonical")
    except (KeyError, TypeError, pg.GenerationError) as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", str(exc)) from exc
    catalog_allowed = {
        "catalog/" + rel for rel in cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL}
    catalog_required = {"catalog/" + rel for rel in cr.CATALOG_REQUIRED}
    catalog_paths = {row["relative_path"] for row in catalog["files"]}
    dim_paths = {row["relative_path"] for row in dim["files"]}
    expected_dim = {
        f"dim/snapshots/date={date}/{name}" for name in cr.DIM_REQUIRED}
    if (not catalog_required <= catalog_paths
            or not catalog_paths <= catalog_allowed
            or dim_paths != expected_dim
            or dim["source_catalog_generation_id"]
            != catalog["generation_id"]):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "intended generation is incoherent")
    members = _future_snapshot_members(
        date, seal_binding, catalog, dim)
    if (payload["snapshot_members"] != members
            or payload["snapshot_set_sha256"]
            != cr.canonical_sha256(members)):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "snapshot set differs from intention")
    return catalog, dim


def _read_canonical_json(path, expected_digest):
    _secure_regular_file(path, max_bytes=MAX_INTENTION_BYTES)
    try:
        raw, _fingerprint = cr._freeze_file(
            str(path), max_bytes=MAX_INTENTION_BYTES)
        payload = json.loads(raw, object_pairs_hook=_json_no_duplicates)
    except WitnessError:
        raise
    except (UnicodeDecodeError, ValueError, cr.ReceiptError) as exc:
        raise WitnessError("LOCAL_INTENTION_INVALID", str(exc)) from exc
    if (hashlib.sha256(raw).hexdigest() != expected_digest
            or not isinstance(payload, dict)
            or raw != cr.canonical_bytes(payload)):
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", f"non-canonical object: {path}")
    return payload


def _date_intention_entries(date_dir):
    intentions, snapshots, ready = [], [], []
    for path in date_dir.iterdir():
        name = path.name
        if re.fullmatch(r"intention-[0-9a-f]{64}\.json", name):
            intentions.append(path)
        elif re.fullmatch(r"snapshot-[0-9a-f]{64}", name):
            snapshots.append(path)
        elif re.fullmatch(r"ready-[0-9a-f]{64}\.json", name):
            ready.append(path)
        elif name.startswith(".tmp-"):
            _secure_regular_file(path)
        else:
            raise WitnessError(
                "LOCAL_INTENTION_CONFLICT", f"unexpected entry {path}")
    return sorted(intentions), sorted(snapshots), sorted(ready)


def _ready_payload(date, intention_sha, members):
    return {
        "schema_version": "canonical-generation-snapshot-ready-v1",
        "state": "DURABLE_BEFORE_FIRST_REMOTE_PUT",
        "date": date,
        "intention_sha256": intention_sha,
        "snapshot_set_sha256": cr.canonical_sha256(members),
        "snapshot_members": members,
    }


def _load_active_future_intention(
        root, date, seal, seal_binding):
    date_dir = pathlib.Path(root) / f"date={date}"
    if not os.path.lexists(date_dir):
        return None
    _secure_directory(date_dir)
    intentions, snapshots, ready = _date_intention_entries(date_dir)
    if not intentions:
        if snapshots or ready:
            raise WitnessError(
                "LOCAL_INTENTION_CONFLICT", "snapshot exists without intent")
        return None
    if len(intentions) != 1:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "multiple intentions for date")
    path = intentions[0]
    match = re.fullmatch(r"intention-([0-9a-f]{64})\.json", path.name)
    digest = match.group(1)
    payload = _read_canonical_json(path, digest)
    catalog, dim = _validate_future_intention(
        payload, date, seal, seal_binding)
    snapshot_root = date_dir / f"snapshot-{digest}"
    if snapshots and snapshots != [snapshot_root]:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "snapshot generation conflicts")
    expected_ready_payload = _ready_payload(
        date, digest, payload["snapshot_members"])
    ready_raw = cr.canonical_bytes(expected_ready_payload)
    expected_ready = date_dir / (
        "ready-" + hashlib.sha256(ready_raw).hexdigest() + ".json")
    if ready and ready != [expected_ready]:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "ready marker conflicts")
    if ready:
        _read_canonical_json(
            expected_ready,
            hashlib.sha256(ready_raw).hexdigest())
    return {
        "date_dir": date_dir, "intention_path": path,
        "intention_sha256": digest, "payload": payload,
        "catalog": catalog, "dim": dim,
        "snapshot_root": snapshot_root,
        "ready_path": expected_ready if ready else None,
    }


def _secure_snapshot_destination(snapshot_root, relative, *, create_dirs):
    relative = pg.safe_relative_path(relative)
    root = pathlib.Path(snapshot_root).absolute()
    if create_dirs:
        _secure_directory(root, create=True)
    else:
        _secure_directory(root)
    current = root
    for part in pathlib.PurePosixPath(relative).parts[:-1]:
        current = current / part
        if create_dirs:
            _secure_directory(current, create=True)
        else:
            _secure_directory(current)
    destination = current / pathlib.PurePosixPath(relative).name
    if destination.parent != current:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID", "snapshot destination escaped")
    return destination


def _audit_snapshot_tree(active):
    """Reject foreign entries; discard only owned pre-READY temp files."""
    root = pathlib.Path(active["snapshot_root"])
    if not os.path.lexists(root):
        return
    expected_files = {
        pg.safe_relative_path(row["snapshot_relative_path"])
        for row in active["payload"]["snapshot_members"]}
    expected_dirs = set()
    for rel in expected_files:
        parts = pathlib.PurePosixPath(rel).parts[:-1]
        for index in range(1, len(parts) + 1):
            expected_dirs.add("/".join(parts[:index]))

    def visit(directory, prefix=""):
        _secure_directory(directory)
        for child in directory.iterdir():
            rel = child.name if not prefix else prefix + "/" + child.name
            try:
                item = os.stat(child, follow_symlinks=False)
            except OSError as exc:
                raise WitnessError(
                    "LOCAL_INTENTION_INVALID", f"snapshot tree: {exc}") \
                    from exc
            if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
                if rel not in expected_dirs:
                    raise WitnessError(
                        "LOCAL_INTENTION_CONFLICT",
                        f"unexpected snapshot directory {rel}")
                visit(child, rel)
                continue
            if (stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode)
                    and rel in expected_files):
                _secure_regular_file(child)
                continue
            if (stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode)
                    and child.name.startswith(".tmp-snapshot-")
                    and active["ready_path"] is None
                    and item.st_uid == os.geteuid()
                    and stat.S_IMODE(item.st_mode) == 0o600):
                try:
                    child.unlink()
                except OSError as exc:
                    raise WitnessError(
                        "LOCAL_INTENTION_INVALID",
                        f"cannot discard partial snapshot temp: {exc}") \
                        from exc
                _fsync_directory(directory)
                continue
            raise WitnessError(
                "LOCAL_INTENTION_CONFLICT",
                f"unexpected snapshot entry {rel}")

    visit(root)


def _snapshot_is_complete(active):
    root = active["snapshot_root"]
    if not os.path.lexists(root):
        if active["ready_path"] is not None:
            raise WitnessError(
                "LOCAL_INTENTION_CONFLICT",
                "READY snapshot root is missing")
        return False
    _audit_snapshot_tree(active)
    _secure_directory(root)
    complete = True
    for row in active["payload"]["snapshot_members"]:
        relative = pg.safe_relative_path(row["snapshot_relative_path"])
        current = pathlib.Path(root)
        missing_parent = False
        for part in pathlib.PurePosixPath(relative).parts[:-1]:
            current = current / part
            if not os.path.lexists(current):
                if active["ready_path"] is not None:
                    raise WitnessError(
                        "LOCAL_INTENTION_CONFLICT",
                        f"READY snapshot directory is missing: {current}")
                complete = False
                missing_parent = True
                break
            _secure_directory(current)
        if missing_parent:
            continue
        path = current / pathlib.PurePosixPath(relative).name
        if not os.path.lexists(path):
            if active["ready_path"] is not None:
                raise WitnessError(
                    "LOCAL_INTENTION_CONFLICT",
                    f"READY snapshot file is missing: {path}")
            complete = False
            continue
        try:
            _secure_regular_file(
                path, expected=(row["size"], row["sha256"]))
        except WitnessError as exc:
            if (exc.code == "LOCAL_INTENTION_CONFLICT"
                    and active["ready_path"] is None):
                complete = False
                continue
            raise
    return complete


def _materialize_future_snapshot(active, warehouse_root):
    root = active["snapshot_root"]
    _audit_snapshot_tree(active)
    for row in active["payload"]["snapshot_members"]:
        destination = _secure_snapshot_destination(
            root, row["snapshot_relative_path"], create_dirs=True)
        expected = (row["size"], row["sha256"])
        if os.path.lexists(destination):
            try:
                _secure_regular_file(destination, expected=expected)
                continue
            except WitnessError as exc:
                if (exc.code != "LOCAL_INTENTION_CONFLICT"
                        or active["ready_path"] is not None):
                    raise
                try:
                    destination.unlink()
                except OSError as unlink_exc:
                    raise WitnessError(
                        "LOCAL_SNAPSHOT_INVALID",
                        f"cannot replace partial snapshot: {unlink_exc}") \
                        from unlink_exc
                _fsync_directory(destination.parent)
        source = pathlib.Path(
            warehouse_root, *row["source_relative_path"].split("/"))
        _snapshot_file(source, destination, expected)
        _secure_regular_file(destination, expected=expected)
        _fsync_directory(destination.parent)
    if not _snapshot_is_complete(active):
        raise WitnessError(
            "LOCAL_SNAPSHOT_INVALID", "persistent snapshot incomplete")
    ready_payload = _ready_payload(
        active["payload"]["date"], active["intention_sha256"],
        active["payload"]["snapshot_members"])
    ready_path, _digest = _persist_content_addressed_json(
        active["date_dir"], "ready", ready_payload)
    active["ready_path"] = ready_path
    return active


def _discard_incomplete_future_intention(active):
    """Remove only one audited pre-READY date snapshot for safe rebuild."""
    if active["ready_path"] is not None:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "READY intention is immutable")
    root = pathlib.Path(active["snapshot_root"])
    if os.path.lexists(root):
        _audit_snapshot_tree(active)

        def remove_tree(directory):
            _secure_directory(directory)
            for child in list(directory.iterdir()):
                item = os.stat(child, follow_symlinks=False)
                if (stat.S_ISDIR(item.st_mode)
                        and not stat.S_ISLNK(item.st_mode)):
                    remove_tree(child)
                    try:
                        child.rmdir()
                    except OSError as exc:
                        raise WitnessError(
                            "LOCAL_INTENTION_INVALID",
                            f"cannot remove old snapshot directory: {exc}") \
                            from exc
                    _fsync_directory(directory)
                elif (stat.S_ISREG(item.st_mode)
                      and not stat.S_ISLNK(item.st_mode)
                      and item.st_uid == os.geteuid()
                      and stat.S_IMODE(item.st_mode) == 0o600):
                    try:
                        child.unlink()
                    except OSError as exc:
                        raise WitnessError(
                            "LOCAL_INTENTION_INVALID",
                            f"cannot remove old snapshot file: {exc}") \
                            from exc
                    _fsync_directory(directory)
                else:
                    raise WitnessError(
                        "LOCAL_INTENTION_CONFLICT",
                        f"unsafe old snapshot entry: {child}")

        remove_tree(root)
        try:
            root.rmdir()
        except OSError as exc:
            raise WitnessError(
                "LOCAL_INTENTION_INVALID",
                f"cannot remove old snapshot root: {exc}") from exc
        _fsync_directory(active["date_dir"])
    intention = pathlib.Path(active["intention_path"])
    expected = _attest_file(intention)
    if expected[1] != active["intention_sha256"]:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "intention changed before discard")
    _secure_regular_file(
        intention, expected=expected, max_bytes=MAX_INTENTION_BYTES)
    try:
        intention.unlink()
    except OSError as exc:
        raise WitnessError(
            "LOCAL_INTENTION_INVALID",
            f"cannot remove incomplete intention: {exc}") from exc
    _fsync_directory(active["date_dir"])
    intentions, snapshots, ready = _date_intention_entries(
        active["date_dir"])
    if intentions or snapshots or ready:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "incomplete intention remained")


def _persist_new_future_intention(
        root, date, seal, seal_binding, catalog, dim):
    date_dir = pathlib.Path(root) / f"date={date}"
    _secure_directory(date_dir, create=True)
    intentions, snapshots, ready = _date_intention_entries(date_dir)
    if intentions or snapshots or ready:
        raise WitnessError(
            "LOCAL_INTENTION_CONFLICT", "date intention appeared concurrently")
    payload = _build_future_intention(
        date, seal, seal_binding, catalog, dim)
    path, digest = _persist_content_addressed_json(
        date_dir, "intention", payload)
    return {
        "date_dir": date_dir, "intention_path": path,
        "intention_sha256": digest, "payload": payload,
        "catalog": catalog, "dim": dim,
        "snapshot_root": date_dir / f"snapshot-{digest}",
        "ready_path": None,
    }


class AwsCli:
    """Small explicit AWS surface: identity, read, original PUT, witness PUT."""

    def __init__(self, executable: str, bucket: str, authorization_file):
        if executable != PINNED_AWS_CLI:
            raise WitnessError(
                "AWS_BINARY_INVALID", "AWS CLI path is fixed")
        self.executable = PINNED_AWS_CLI
        self.bucket = bucket
        self.authorization_file = pathlib.Path(authorization_file)
        self.authorization_sha256 = _read_authorization(
            self.authorization_file)
        self.code_commit = _clean_mutation_provenance()
        self.environment = _sterile_publisher_environment()
        self.identity_verified = False

    def _mutation_gate(self):
        if not self.identity_verified:
            raise WitnessError(
                "PUBLISHER_IDENTITY_MISMATCH", "identity not verified")
        if _read_authorization(
                self.authorization_file) != self.authorization_sha256:
            raise WitnessError(
                "AUTHORIZATION_INVALID", "authorization changed")
        if _clean_mutation_provenance() != self.code_commit:
            raise WitnessError("GIT_PROVENANCE_DIRTY", "HEAD changed")

    def _command(self, args):
        return [self.executable, *args]

    def _run(self, args, label: str, *, allow_precondition=False):
        try:
            result = subprocess.run(
                self._command(args), capture_output=True, text=True,
                timeout=4 * 60 * 60, env=self.environment)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WitnessError("AWS_COMMAND_FAILED", f"{label}: {exc}") from exc
        detail = (result.stderr or result.stdout or "").strip()
        if result.returncode:
            if allow_precondition and re.search(
                    r"PreconditionFailed|pre-conditions", detail,
                    re.IGNORECASE):
                return None
            if re.search(
                    r"NoSuchKey|NotFound|Not Found|404", detail,
                    re.IGNORECASE):
                raise WitnessError("REMOTE_NOT_FOUND", label)
            raise WitnessError(
                "AWS_COMMAND_FAILED",
                f"{label} rc={result.returncode}: {detail[-1200:]}")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise WitnessError(
                "AWS_RESPONSE_INVALID", f"{label}: {exc}") from exc
        if not isinstance(payload, dict):
            raise WitnessError("AWS_RESPONSE_INVALID", label)
        return payload

    def identity(self, expected_arn: str):
        payload = self._run(
            ["sts", "get-caller-identity", "--output", "json"],
            "publisher identity")
        if payload.get("Arn") != expected_arn:
            raise WitnessError(
                "PUBLISHER_IDENTITY_MISMATCH", repr(payload.get("Arn")))
        self.identity_verified = True
        return payload

    def head(self, key: str, version_id: str | None = None):
        args = [
            "s3api", "head-object", "--bucket", self.bucket,
            "--key", key, "--output", "json"]
        if version_id is not None:
            args.extend(["--version-id", version_id])
        return self._run(args, f"HEAD {key}")

    def get_exact(self, key: str, version_id: str, output: pathlib.Path):
        return self._run([
            "s3api", "get-object", "--bucket", self.bucket,
            "--key", key, "--version-id", version_id,
            str(output), "--output", "json"], f"GET {key}@{version_id}")

    def put_original(self, key: str, body: pathlib.Path):
        self._mutation_gate()
        return self._run([
            "s3api", "put-object", "--bucket", self.bucket,
            "--key", key, "--body", str(body), "--output", "json"],
            f"PUT original {key}")

    def put_if_absent(self, key: str, body: pathlib.Path):
        self._mutation_gate()
        return self._run([
            "s3api", "put-object", "--bucket", self.bucket,
            "--key", key, "--body", str(body),
            "--content-type", "application/json",
            "--if-none-match", "*", "--output", "json"],
            f"conditional witness PUT {key}", allow_precondition=True)

    def list_current(self, prefix: str):
        return self._run([
            "s3api", "list-objects-v2", "--bucket", self.bucket,
            "--prefix", prefix, "--max-keys", "1000", "--no-paginate",
            "--output", "json"], f"LIST current {prefix}")

    def list_versions(self, key: str):
        versions, deletes = [], []
        pages = rows_seen = 0
        key_marker = version_marker = None
        while True:
            pages += 1
            if pages > MAX_HISTORY_PAGES:
                raise WitnessError("HISTORY_BOUNDED", key)
            args = [
                "s3api", "list-object-versions", "--bucket", self.bucket,
                "--prefix", key, "--max-keys", "1000", "--no-paginate",
                "--output", "json"]
            if key_marker is not None:
                args.extend(["--key-marker", key_marker])
                if version_marker is not None:
                    args.extend(["--version-id-marker", version_marker])
            page = self._run(args, f"LIST history {key} page {pages}")
            truncated = page.get("IsTruncated", False)
            if not isinstance(truncated, bool):
                raise WitnessError("HISTORY_INVALID", key)
            for field, target in (("Versions", versions),
                                  ("DeleteMarkers", deletes)):
                rows = page.get(field) or []
                if not isinstance(rows, list):
                    raise WitnessError("HISTORY_INVALID", f"{key} {field}")
                rows_seen += len(rows)
                if rows_seen > MAX_HISTORY_ROWS:
                    raise WitnessError("HISTORY_BOUNDED", key)
                for raw in rows:
                    if not isinstance(raw, dict) or raw.get("Key") != key:
                        continue
                    version_id = raw.get("VersionId")
                    if not _valid_version(version_id):
                        raise WitnessError("HISTORY_INVALID", key)
                    row = {
                        "Key": key, "VersionId": version_id,
                        "LastModified": _utc(
                            raw.get("LastModified"), "history LastModified"),
                    }
                    if field == "Versions":
                        size = raw.get("Size")
                        if (not isinstance(size, int) or isinstance(size, bool)
                                or size < 0):
                            raise WitnessError("HISTORY_INVALID", key)
                        row["Size"] = size
                    target.append(row)
            if not truncated:
                break
            next_key = page.get("NextKeyMarker")
            next_version = page.get("NextVersionIdMarker")
            if (not isinstance(next_key, str) or not next_key
                    or (next_key, next_version)
                    == (key_marker, version_marker)
                    or (next_version is not None
                        and not isinstance(next_version, str))):
                raise WitnessError("HISTORY_INVALID", f"pagination {key}")
            key_marker, version_marker = next_key, next_version
        physical = [(row["Key"], row["VersionId"])
                    for row in versions + deletes]
        if len(physical) != len(set(physical)):
            raise WitnessError("HISTORY_INVALID", f"duplicates {key}")
        versions.sort(key=lambda row: (
            row["LastModified"], row["VersionId"]))
        deletes.sort(key=lambda row: (
            row["LastModified"], row["VersionId"]))
        return {
            "key": key, "pages": pages, "rows_seen": rows_seen,
            "versions": versions, "delete_markers": deletes,
            "complete_history": True,
        }


def _exact_remote_binding(client, key, version_id, expected, temp,
                          *, label):
    if not _valid_version(version_id):
        raise WitnessError("EXACT_VERSION_INVALID", label)
    head = client.head(key, version_id)
    size = head.get("ContentLength")
    modified = head.get("LastModified")
    if (head.get("VersionId") != version_id
            or not isinstance(size, int) or isinstance(size, bool)
            or size < 0 or modified is None):
        raise WitnessError("EXACT_HEAD_INVALID", label)
    output = pathlib.Path(temp) / (
        "exact-" + hashlib.sha256(
            (key + "\0" + version_id).encode()).hexdigest() + ".bin")
    try:
        response = client.get_exact(key, version_id, output)
        observed = _attest_file(output)
    finally:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
    if (response.get("VersionId") != version_id
            or response.get("ContentLength") != observed[0]
            or observed != expected or size != expected[0]):
        raise WitnessError("EXACT_BYTES_MISMATCH", label)
    return {
        "bucket": client.bucket, "key": key, "VersionId": version_id,
        "size": observed[0], "sha256": observed[1],
        "LastModified": _utc(modified, f"{label} LastModified"),
    }


def _current_exact_if_matching(client, key, expected, temp):
    try:
        head = client.head(key)
    except WitnessError as exc:
        if exc.code == "REMOTE_NOT_FOUND":
            return None
        raise
    version_id = head.get("VersionId")
    if (not _valid_version(version_id)
            or head.get("ContentLength") != expected[0]):
        return None
    try:
        return _exact_remote_binding(
            client, key, version_id, expected, temp, label=key)
    except WitnessError as exc:
        if exc.code == "EXACT_BYTES_MISMATCH":
            return None
        raise


def _sync_original(client, local_path, key, temp):
    expected = _attest_file(local_path)
    existing = _current_exact_if_matching(client, key, expected, temp)
    if existing is not None:
        return existing, False
    response = client.put_original(key, pathlib.Path(local_path))
    version_id = response.get("VersionId")
    if not _valid_version(version_id):
        raise WitnessError("ORIGINAL_PUT_INVALID", key)
    return _exact_remote_binding(
        client, key, version_id, expected, temp, label=key), True


def _resolve_existing_exact(client, local_path, key, temp):
    expected = _attest_file(local_path)
    current = _current_exact_if_matching(client, key, expected, temp)
    if current is not None:
        return current
    history = client.list_versions(key)
    matches = []
    for row in history["versions"]:
        if row["Size"] != expected[0]:
            continue
        try:
            binding = _exact_remote_binding(
                client, key, row["VersionId"], expected, temp, label=key)
        except WitnessError as exc:
            if exc.code == "EXACT_BYTES_MISMATCH":
                continue
            raise
        matches.append(binding)
    if not matches:
        raise WitnessError("EXACT_VERSION_NOT_FOUND", key)
    matches.sort(key=lambda row: (row["LastModified"], row["VersionId"]))
    return matches[-1]


def _validate_witness_listing(payload, prefix):
    contents = payload.get("Contents") or []
    if (payload.get("IsTruncated") is not False
            or not isinstance(contents, list)
            or payload.get("CommonPrefixes") not in (None, [])):
        raise WitnessError("WITNESS_PREFIX_INVALID", prefix)
    rows = []
    for raw in contents:
        if (not isinstance(raw, dict) or not isinstance(raw.get("Key"), str)
                or not isinstance(raw.get("Size"), int)
                or isinstance(raw.get("Size"), bool) or raw["Size"] < 0):
            raise WitnessError("WITNESS_PREFIX_INVALID", prefix)
        rows.append({"Key": raw["Key"], "Size": raw["Size"]})
    return rows


def _json_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WitnessError("WITNESS_INVALID", f"duplicate key {key!r}")
        value[key] = item
    return value


def _discover_existing_generation_witness(
        client, date, seal_binding, sealed_at, temp, *,
        expected_authority):
    """Return one fully validated terminal witness, or None when absent."""
    prefix = fcr.generation_witness_prefix(DEFAULT_PREFIX, date) + "/"
    rows = _validate_witness_listing(client.list_current(prefix), prefix)
    if not rows:
        return None
    if len(rows) != 1:
        raise WitnessError(
            "WITNESS_PREFIX_CONFLICT", "multiple witnesses already exist")
    listed = rows[0]
    match = re.fullmatch(
        re.escape(prefix) + r"witness-([0-9a-f]{64})\.json",
        listed["Key"])
    if (match is None or listed["Size"] <= 0
            or listed["Size"] > MAX_WITNESS_BYTES):
        raise WitnessError(
            "WITNESS_INVALID", "existing witness key/size invalid")
    key, digest = listed["Key"], match.group(1)
    head = client.head(key)
    version_id = head.get("VersionId")
    if (not _valid_version(version_id)
            or head.get("ContentLength") != listed["Size"]
            or head.get("LastModified") is None):
        raise WitnessError("WITNESS_INVALID", "existing witness HEAD invalid")
    output = pathlib.Path(temp) / "existing-generation-witness.json"
    try:
        response = client.get_exact(key, version_id, output)
        raw, _fingerprint = cr._freeze_file(
            str(output), max_bytes=MAX_WITNESS_BYTES)
    except cr.ReceiptError as exc:
        raise WitnessError("WITNESS_INVALID", str(exc)) from exc
    finally:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
    if (response.get("VersionId") != version_id
            or response.get("ContentLength") != len(raw)
            or len(raw) != listed["Size"]
            or hashlib.sha256(raw).hexdigest() != digest):
        raise WitnessError("WITNESS_INVALID", "existing witness bytes invalid")
    try:
        payload = json.loads(raw, object_pairs_hook=_json_no_duplicates)
    except WitnessError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise WitnessError("WITNESS_INVALID", str(exc)) from exc
    if (not isinstance(payload, dict)
            or raw != fcr.generation_witness_bytes(payload)
            or fcr.expected_generation_witness_key(
                DEFAULT_PREFIX, date, digest) != key):
        raise WitnessError("WITNESS_INVALID", "existing witness form invalid")
    try:
        validated = fcr.validate_generation_witness(
            payload, date, DEFAULT_BUCKET, DEFAULT_PREFIX,
            seal_binding["sha256"], seal_binding["size"], sealed_at)
    except cr.ReceiptError as exc:
        raise WitnessError("WITNESS_INVALID", str(exc)) from exc
    if expected_authority == fcr.GENERATION_AUTHORITY_PRODUCER:
        authority_valid = (
            validated["generation_authority"]
            == fcr.GENERATION_AUTHORITY_PRODUCER
            and validated["catalog_dim_coherence_claim"] is True
            and validated["legacy_migration"] is None)
    elif expected_authority == fcr.GENERATION_AUTHORITY_LEGACY:
        authority_valid = (
            validated["generation_authority"]
            == fcr.GENERATION_AUTHORITY_LEGACY
            and validated["catalog_dim_coherence_claim"] is False
            and isinstance(validated["legacy_migration"], dict)
            and validated["legacy_migration"].get("limitations")
            == list(fcr.LEGACY_MIGRATION_LIMITATIONS))
    else:
        raise WitnessError(
            "WITNESS_INVALID", "unsupported expected authority")
    if not authority_valid:
        raise WitnessError(
            "WITNESS_INVALID", "existing witness authority invalid")
    binding = {
        "bucket": DEFAULT_BUCKET, "key": key, "VersionId": version_id,
        "size": len(raw), "sha256": digest,
        "LastModified": _utc(
            head["LastModified"], "existing witness LastModified"),
    }
    return payload, binding


def _publish_witness(client, witness, temp):
    key, raw = fcr.generation_witness_artifact(witness)
    if len(raw) <= 0 or len(raw) > MAX_WITNESS_BYTES:
        raise WitnessError("WITNESS_SIZE_INVALID", str(len(raw)))
    prefix = fcr.generation_witness_prefix(
        witness["prefix"], witness["date"]) + "/"
    existing = _validate_witness_listing(client.list_current(prefix), prefix)
    if len(existing) > 1 or (existing and existing[0]["Key"] != key):
        raise WitnessError(
            "WITNESS_PREFIX_CONFLICT", "another witness already exists")
    expected = (len(raw), hashlib.sha256(raw).hexdigest())
    body = pathlib.Path(temp) / "generation-witness.json"
    body.write_bytes(raw)
    if existing:
        head = client.head(key)
        version_id = head.get("VersionId")
        binding = _exact_remote_binding(
            client, key, version_id, expected, temp, label="generation witness")
        return binding, False
    response = client.put_if_absent(key, body)
    if response is None:
        head = client.head(key)
        version_id = head.get("VersionId")
        created = False
    else:
        version_id = response.get("VersionId")
        created = True
    binding = _exact_remote_binding(
        client, key, version_id, expected, temp, label="generation witness")
    final = _validate_witness_listing(client.list_current(prefix), prefix)
    if len(final) != 1 or final[0]["Key"] != key \
            or final[0]["Size"] != len(raw):
        raise WitnessError("WITNESS_PREFIX_CONFLICT", prefix)
    return binding, created


def _authoritative_day(date, raw_root, warehouse_root):
    try:
        return cr._authoritative_day_inputs(date, raw_root, warehouse_root)
    except cr.ReceiptError as exc:
        raise WitnessError("SEALED_DAY_INVALID", str(exc)) from exc


def _coherent_local_generation(warehouse_root, date):
    catalog_allowed = {
        "catalog/" + rel for rel in cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL}
    catalog_required = {"catalog/" + rel for rel in cr.CATALOG_REQUIRED}
    dim_paths = {
        "dim/snapshots/date=%s/%s" % (date, name)
        for name in cr.DIM_REQUIRED}
    try:
        catalog = pg.load_manifest(
            warehouse_root, "catalog", verify_files=True)
        catalog_paths = {
            row["relative_path"] for row in catalog["files"]}
        if (not catalog_required <= catalog_paths
                or not catalog_paths <= catalog_allowed):
            raise pg.GenerationError("catalog generation member set invalid")
        dim = pg.load_manifest(
            warehouse_root, "dim", date=date,
            expected_paths=dim_paths, verify_files=True)
        if dim["source_catalog_generation_id"] != catalog["generation_id"]:
            raise pg.GenerationError("dim/catalog generation mismatch")
    except pg.GenerationError as exc:
        raise WitnessError("LOCAL_GENERATION_INVALID", str(exc)) from exc
    return catalog, dim


def publish_future_generation(date, raw_root, warehouse_root, client,
                              *, publisher_arn=DEFAULT_PUBLISHER_ARN,
                              lock_timeout=60.0, intention_root=None):
    with _production_writer_lock(client):
        return _publish_future_generation_locked(
            date, raw_root, warehouse_root, client,
            publisher_arn=publisher_arn, lock_timeout=lock_timeout,
            intention_root=intention_root)


def _publish_future_generation_locked(
        date, raw_root, warehouse_root, client, *, publisher_arn,
        lock_timeout, intention_root):
    cr._validate_date(date)
    if date < FUTURE_GENERATION_FIRST_DATE:
        raise WitnessError(
            "FUTURE_DATE_FORBIDDEN",
            f"{date} requires no rebuild or the bounded legacy path")
    if isinstance(client, AwsCli):
        raw_root = str(_exact_existing_production_dir(
            raw_root, PRODUCTION_RAW_ROOT, "raw root"))
        warehouse_root = str(_exact_existing_production_dir(
            warehouse_root, PRODUCTION_WAREHOUSE_ROOT, "warehouse root"))
    client.identity(publisher_arn)
    (seal, seal_binding, *_rest) = _authoritative_day(
        date, raw_root, warehouse_root)
    seal_path = pathlib.Path(warehouse_root) / "seals" / f"date={date}.json"
    uploaded = 0
    try:
        with tempfile.TemporaryDirectory(
                prefix=f"generation-witness-{date}-") as temp_value:
            temp = pathlib.Path(temp_value)
            existing = _discover_existing_generation_witness(
                client, date, seal_binding, seal.get("sealed_at"), temp,
                expected_authority=fcr.GENERATION_AUTHORITY_PRODUCER)
            if existing is not None:
                existing_payload, exact_witness = existing
                return {
                    "schema_version":
                        "canonical-generation-witness-publication-v1",
                    "state": "PRODUCER_GENERATION_WITNESS_READY",
                    "date": date,
                    "generation_authority":
                        fcr.GENERATION_AUTHORITY_PRODUCER,
                    "catalog_dim_coherence_claim": True,
                    "catalog_generation_id": existing_payload[
                        "catalog_generation"]["generation_id"],
                    "dim_generation_id": existing_payload[
                        "dim_generation"]["generation_id"],
                    "witness_object": exact_witness,
                    "original_object_puts": 0,
                    "witness_puts": 0,
                    "existing_witness_reused": True,
                    "research_prefix_writes": 0,
                    "copy_operations": 0,
                    "tag_writes": 0,
                    "rfq": "OFF",
                    "seal_sha256": seal_binding["sha256"],
                }

            # READY permanently pins this date to one exact generation until
            # its witness is terminal.  Before READY there has been no remote
            # mutation, so an incomplete snapshot can be rebuilt safely.  All
            # snapshot bytes live below canonical_receipts across retries;
            # network upload never falls back to a process-temp snapshot.
            persistent_root = _intention_storage_root(
                client, warehouse_root, intention_root)
            active = _load_active_future_intention(
                persistent_root, date, seal, seal_binding)
            if active is None:
                with pg.generation_locks(
                        warehouse_root,
                        {"catalog": "shared", "dim": "shared"},
                        timeout=lock_timeout):
                    catalog, dim = _coherent_local_generation(
                        warehouse_root, date)
                    active = _persist_new_future_intention(
                        persistent_root, date, seal, seal_binding,
                        catalog, dim)
                    _materialize_future_snapshot(active, warehouse_root)
            else:
                catalog, dim = active["catalog"], active["dim"]
                if not _snapshot_is_complete(active):
                    with pg.generation_locks(
                            warehouse_root,
                            {"catalog": "shared", "dim": "shared"},
                            timeout=lock_timeout):
                        current_catalog, current_dim = \
                            _coherent_local_generation(warehouse_root, date)
                        if _attest_file(seal_path) != (
                                seal_binding["size"],
                                seal_binding["sha256"]):
                            raise WitnessError(
                                "LOCAL_INTENTION_CONFLICT",
                                "seal changed during intention recovery")
                        if (current_catalog != catalog
                                or current_dim != dim):
                            # READY is the durable boundary before all remote
                            # mutations.  Before it, an incomplete snapshot
                            # can be discarded safely and rebuilt from the
                            # newly coherent producer generation.
                            _discard_incomplete_future_intention(active)
                            active = _persist_new_future_intention(
                                persistent_root, date, seal, seal_binding,
                                current_catalog, current_dim)
                            catalog, dim = current_catalog, current_dim
                        _materialize_future_snapshot(
                            active, warehouse_root)
                elif active["ready_path"] is None:
                    # A crash can occur after the last snapshot fsync and
                    # before READY is linked.  No source read is needed.
                    _materialize_future_snapshot(active, warehouse_root)

            active = _load_active_future_intention(
                persistent_root, date, seal, seal_binding)
            if (active is None or active["ready_path"] is None
                    or not _snapshot_is_complete(active)):
                raise WitnessError(
                    "LOCAL_SNAPSHOT_INVALID",
                    "durable snapshot/intent not terminal before PUT")
            catalog, dim = active["catalog"], active["dim"]
            seal_snapshot = _secure_snapshot_destination(
                active["snapshot_root"], f"seals/date={date}.json",
                create_dirs=False)
            generation_snapshots = {
                row["source_relative_path"]:
                    _secure_snapshot_destination(
                        active["snapshot_root"],
                        row["snapshot_relative_path"], create_dirs=False)
                for row in active["payload"]["snapshot_members"]
                if row["source_relative_path"]
                != f"seals/date={date}.json"
            }

            # All network I/O is deliberately outside the producer locks.
            # The durable immutable snapshot above is the sole upload source.
            exact_seal, created = _sync_original(
                client, seal_snapshot,
                cr._join_key(
                    DEFAULT_PREFIX, "warehouse", "seals", f"date={date}.json"),
                temp)
            if (exact_seal["size"], exact_seal["sha256"]) != (
                    seal_binding["size"], seal_binding["sha256"]):
                raise WitnessError(
                    "SEALED_DAY_CHANGED", "seal changed after authority load")
            uploaded += int(created)
            objects = []
            for manifest in (catalog, dim):
                for row in manifest["files"]:
                    rel = row["relative_path"]
                    binding, created = _sync_original(
                        client, generation_snapshots[rel],
                        cr._join_key(DEFAULT_PREFIX, "warehouse", rel), temp)
                    uploaded += int(created)
                    objects.append({
                        "logical_source_key": "warehouse/" + rel,
                        **binding,
                    })
            objects.sort(key=lambda row: row["logical_source_key"])
            witness = fcr.build_generation_witness_payload(
                date, DEFAULT_BUCKET, DEFAULT_PREFIX, exact_seal,
                catalog, dim, objects)
            exact_witness, witness_created = _publish_witness(
                client, witness, temp)
    except pg.GenerationError as exc:
        raise WitnessError("LOCAL_GENERATION_BUSY", str(exc)) from exc
    return {
        "schema_version": "canonical-generation-witness-publication-v1",
        "state": "PRODUCER_GENERATION_WITNESS_READY",
        "date": date,
        "generation_authority": fcr.GENERATION_AUTHORITY_PRODUCER,
        "catalog_dim_coherence_claim": True,
        "catalog_generation_id": catalog["generation_id"],
        "dim_generation_id": dim["generation_id"],
        "witness_object": exact_witness,
        "original_object_puts": uploaded,
        "witness_puts": int(witness_created),
        "existing_witness_reused": False,
        "research_prefix_writes": 0,
        "copy_operations": 0,
        "tag_writes": 0,
        "rfq": "OFF",
        "seal_sha256": seal_binding["sha256"],
    }


def _catalog_specs():
    rows = []
    for rel in cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL:
        rows.append({
            "relative_path": "catalog/" + rel,
            "logical_source_key": "warehouse/catalog/" + rel,
            "key": cr._join_key(DEFAULT_PREFIX, "warehouse", "catalog", rel),
            "required": rel in cr.CATALOG_REQUIRED,
        })
    return rows


def _legacy_catalog_batch(date, histories, sealed_at):
    minute = LEGACY_EXPECTED_BATCH_MINUTES[date]
    anchor = dt.datetime.fromisoformat(minute + ":00+00:00")
    seal_time = _utc_time(sealed_at, "local seal sealed_at")
    if anchor <= seal_time:
        raise WitnessError("LEGACY_BATCH_INVALID", "batch precedes local seal")
    required = {
        row["logical_source_key"] for row in _catalog_specs()
        if row["required"]}
    versions_by_logical = {}
    deletes = []
    for logical, history in histories.items():
        if (not isinstance(history, dict)
                or history.get("complete_history") is not True
                or not isinstance(history.get("pages"), int)
                or isinstance(history.get("pages"), bool)
                or not 1 <= history["pages"] <= MAX_HISTORY_PAGES
                or not isinstance(history.get("rows_seen"), int)
                or isinstance(history.get("rows_seen"), bool)
                or not 0 <= history["rows_seen"] <= MAX_HISTORY_ROWS
                or not isinstance(history.get("versions"), list)
                or not isinstance(history.get("delete_markers"), list)
                or history["rows_seen"] < len(history["versions"])
                + len(history["delete_markers"])):
            raise WitnessError(
                "HISTORY_INCOMPLETE", f"bounded proof missing for {logical}")
        versions_by_logical[logical] = [
            dict(row, _time=_utc_time(
                row["LastModified"], "catalog LastModified"))
            for row in history["versions"]]
        deletes.extend((logical, _utc_time(
            row["LastModified"], "catalog delete LastModified"))
                       for row in history["delete_markers"])

    # Bounded complete histories must prove there is no earlier complete
    # <=15-minute catalog batch after the local seal.  This is deliberately
    # conservative: an ambiguous earlier window blocks migration.
    event_times = sorted({
        row["_time"] for rows in versions_by_logical.values() for row in rows
        if seal_time < row["_time"] < anchor})
    for start in event_times:
        end = start + LEGACY_BATCH_WINDOW
        covered = {
            logical for logical, rows in versions_by_logical.items()
            if any(start <= row["_time"] <= end for row in rows)}
        if required <= covered:
            raise WitnessError(
                "LEGACY_BATCH_AMBIGUOUS",
                f"an earlier complete catalog window starts {start.isoformat()}")

    window_end = anchor + LEGACY_BATCH_WINDOW
    if any(anchor <= when <= window_end for _logical, when in deletes):
        raise WitnessError(
            "LEGACY_BATCH_INVALID", "delete marker occurs in selected batch")
    selected = []
    for spec in _catalog_specs():
        matches = [
            row for row in versions_by_logical[spec["logical_source_key"]]
            if anchor <= row["_time"] <= window_end]
        if len(matches) != int(spec["required"] or bool(matches)):
            raise WitnessError(
                "LEGACY_BATCH_INVALID",
                f"batch membership {spec['logical_source_key']}")
        if matches:
            selected.append((spec, matches[0]))
    if {spec["logical_source_key"] for spec, _row in selected} < required:
        raise WitnessError("LEGACY_BATCH_INVALID", "required catalog missing")
    actual_start = min(row["_time"] for _spec, row in selected)
    actual_end = max(row["_time"] for _spec, row in selected)
    if actual_start.strftime("%Y-%m-%dT%H:%M") != minute:
        raise WitnessError("LEGACY_BATCH_INVALID", "known batch minute mismatch")
    return selected, actual_start, actual_end


def _write_history_proof(root, date, proof):
    digest = cr.canonical_sha256(proof)
    path = pathlib.Path(root) / f"date={date}" / \
        f"legacy-catalog-history-proof-{digest}.json"
    if os.path.lexists(path):
        if os.path.islink(path):
            raise WitnessError("LEGACY_PROOF_CONFLICT", "proof is a symlink")
        try:
            raw, _fingerprint = cr._freeze_file(
                str(path), max_bytes=MAX_PROOF_BYTES)
            existing = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError, cr.ReceiptError) as exc:
            raise WitnessError("LEGACY_PROOF_CONFLICT", str(exc)) from exc
        if existing != proof:
            raise WitnessError("LEGACY_PROOF_CONFLICT", str(path))
    else:
        cr.write_atomic_json(path, proof)
    return path, digest


def _historical_authority_binding(date, seal_binding):
    authority = seal_binding.get("historical_metadata_authority")
    required = {
        "schema_version", "state", "date", "seal_sha256",
        "manifest_date_sha256", "manifest_fact_projection_sha256",
        "raw_files_sha256",
        "archive_file_stats_sha256", "verified_fact_bytes_sha256",
        "capture_receipt", "l2_receipt", "local_raw_bytes_read",
        "raw_rebuild_or_download", "required_followup",
    }
    if (not isinstance(authority, dict) or set(authority) != required
            or authority["schema_version"]
            != fcr.HISTORICAL_METADATA_AUTHORITY_SCHEMA
            or authority["state"]
            != fcr.HISTORICAL_METADATA_AUTHORITY_STATE
            or authority["date"] != date
            or authority["seal_sha256"] != seal_binding["sha256"]
            or authority["local_raw_bytes_read"] != 0
            or isinstance(authority["local_raw_bytes_read"], bool)
            or authority["raw_rebuild_or_download"] is not False
            or authority["required_followup"]
            != "EXACT_S3_VERSION_SIZE_AND_SHA256_VERIFICATION"):
        raise WitnessError(
            "HISTORICAL_AUTHORITY_INVALID", "authority identity invalid")
    for name in (
            "seal_sha256", "manifest_date_sha256",
            "manifest_fact_projection_sha256", "raw_files_sha256",
            "archive_file_stats_sha256", "verified_fact_bytes_sha256"):
        value = authority[name]
        if not isinstance(value, str) or not cr.SHA256_RE.match(value):
            raise WitnessError(
                "HISTORICAL_AUTHORITY_INVALID", f"{name} invalid")
    for name in ("capture_receipt", "l2_receipt"):
        row = authority[name]
        if row is None:
            continue
        if (not isinstance(row, dict) or set(row) != {
                "size", "sha256", "sealed_inventory_sha256"}
                or not isinstance(row["size"], int)
                or isinstance(row["size"], bool) or row["size"] <= 0
                or not isinstance(row["sha256"], str)
                or not cr.SHA256_RE.match(row["sha256"])
                or not isinstance(row["sealed_inventory_sha256"], str)
                or not cr.SHA256_RE.match(
                    row["sealed_inventory_sha256"])):
            raise WitnessError(
                "HISTORICAL_AUTHORITY_INVALID", f"{name} invalid")
    if authority["capture_receipt"] is None:
        raise WitnessError(
            "HISTORICAL_AUTHORITY_INVALID", "capture receipt is unbound")
    requires_l2 = date in {"2026-07-15", "2026-07-16"}
    if (authority["l2_receipt"] is not None) != requires_l2:
        raise WitnessError(
            "HISTORICAL_AUTHORITY_INVALID", "L2 receipt binding mismatch")
    # Canonicalization is checked before the authority becomes part of the
    # content-addressed history proof and, transitively, the witness.
    cr.canonical_bytes(authority)
    return authority, cr.canonical_sha256(authority)


def _existing_legacy_history_proof(
        proof_root, date, witness, authority, authority_sha):
    migration = witness.get("legacy_migration")
    proof_sha = (migration or {}).get("catalog_history_proof_sha256")
    if not isinstance(proof_sha, str) or not cr.SHA256_RE.match(proof_sha):
        raise WitnessError(
            "LEGACY_PROOF_CONFLICT", "witness proof SHA invalid")
    path = pathlib.Path(proof_root) / f"date={date}" / \
        f"legacy-catalog-history-proof-{proof_sha}.json"
    if (not os.path.lexists(path) or os.path.islink(path)
            or not path.is_file()):
        raise WitnessError(
            "LEGACY_PROOF_CONFLICT", f"bound proof unavailable: {path}")
    try:
        raw, _fingerprint = cr._freeze_file(
            str(path), max_bytes=MAX_PROOF_BYTES)
        proof = json.loads(raw, object_pairs_hook=_json_no_duplicates)
    except WitnessError:
        raise
    except (UnicodeDecodeError, ValueError, cr.ReceiptError) as exc:
        raise WitnessError("LEGACY_PROOF_CONFLICT", str(exc)) from exc
    if (not isinstance(proof, dict)
            or cr.canonical_sha256(proof) != proof_sha
            or proof.get("schema_version")
            != "legacy-catalog-batch-history-proof-v1"
            or proof.get("state") != "BOUNDED_COMPLETE_HISTORY_PROOF"
            or proof.get("date") != date
            or proof.get("historical_metadata_authority") != authority
            or proof.get("historical_metadata_authority_sha256")
            != authority_sha):
        raise WitnessError(
            "LEGACY_PROOF_CONFLICT",
            "bound history proof/authority differs")
    return path, proof_sha


def migrate_legacy_generation(date, raw_root, warehouse_root, quality_dir,
                              proof_root, client, *,
                              publisher_arn=DEFAULT_PUBLISHER_ARN):
    with _production_writer_lock(client):
        return _migrate_legacy_generation_locked(
            date, raw_root, warehouse_root, quality_dir, proof_root, client,
            publisher_arn=publisher_arn)


def _migrate_legacy_generation_locked(
        date, raw_root, warehouse_root, quality_dir, proof_root, client, *,
        publisher_arn):
    cr._validate_date(date)
    if date not in fcr.LEGACY_MIGRATION_DATES \
            or date not in LEGACY_EXPECTED_BATCH_MINUTES:
        raise WitnessError(
            "LEGACY_DATE_FORBIDDEN", "migration is restricted to four dates")
    if isinstance(client, AwsCli):
        raw_root = str(_exact_existing_production_dir(
            raw_root, PRODUCTION_RAW_ROOT, "raw root"))
        warehouse_root = str(_exact_existing_production_dir(
            warehouse_root, PRODUCTION_WAREHOUSE_ROOT, "warehouse root"))
        quality_dir = str(_exact_existing_production_dir(
            quality_dir, PRODUCTION_QUALITY_ROOT, "quality root"))
        if pathlib.Path(proof_root) != PRODUCTION_PROOF_ROOT:
            raise WitnessError(
                "PRODUCTION_PATH_INVALID", "legacy proof root is fixed")
    client.identity(publisher_arn)
    try:
        (seal, seal_binding, *_rest) = \
            fcr.historical_authoritative_day_inputs(
                date, raw_root, warehouse_root, quality_dir)
    except cr.ReceiptError as exc:
        raise WitnessError("SEALED_DAY_INVALID", str(exc)) from exc
    historical_authority, historical_authority_sha = \
        _historical_authority_binding(date, seal_binding)
    sealed_at = seal.get("sealed_at")
    _utc(sealed_at, "local seal sealed_at")
    seal_path = pathlib.Path(warehouse_root) / "seals" / f"date={date}.json"
    with tempfile.TemporaryDirectory(
            prefix=f"legacy-generation-witness-{date}-") as temp_value:
        temp = pathlib.Path(temp_value)
        existing = _discover_existing_generation_witness(
            client, date, seal_binding, sealed_at, temp,
            expected_authority=fcr.GENERATION_AUTHORITY_LEGACY)
        if existing is not None:
            existing_payload, exact_witness = existing
            proof_path, proof_sha = _existing_legacy_history_proof(
                proof_root, date, existing_payload,
                historical_authority, historical_authority_sha)
            return {
                "schema_version":
                    "canonical-generation-witness-migration-v1",
                "state": "LEGACY_GENERATION_WITNESS_READY",
                "date": date,
                "generation_authority":
                    fcr.GENERATION_AUTHORITY_LEGACY,
                "catalog_dim_coherence_claim": False,
                "catalog_generation_id": existing_payload[
                    "catalog_generation"]["generation_id"],
                "dim_generation_id": existing_payload[
                    "dim_generation"]["generation_id"],
                "history_proof": str(proof_path),
                "history_proof_sha256": proof_sha,
                "historical_metadata_authority_sha256":
                    historical_authority_sha,
                "witness_object": exact_witness,
                "original_object_puts": 0,
                "witness_puts": 0,
                "existing_witness_reused": True,
                "research_prefix_writes": 0,
                "copy_operations": 0,
                "tag_writes": 0,
                "rfq": "OFF",
                "seal_sha256": seal_binding["sha256"],
            }
        exact_seal = _resolve_existing_exact(
            client, seal_path,
            cr._join_key(
                DEFAULT_PREFIX, "warehouse", "seals", f"date={date}.json"),
            temp)
        if (exact_seal["size"], exact_seal["sha256"]) != (
                seal_binding["size"], seal_binding["sha256"]):
            raise WitnessError(
                "SEALED_DAY_CHANGED", "seal changed after authority load")
        histories = {}
        for spec in _catalog_specs():
            histories[spec["logical_source_key"]] = \
                client.list_versions(spec["key"])
        selected, batch_start, batch_end = _legacy_catalog_batch(
            date, histories, sealed_at)
        proof = {
            "schema_version": "legacy-catalog-batch-history-proof-v1",
            "state": "BOUNDED_COMPLETE_HISTORY_PROOF",
            "date": date, "bucket": DEFAULT_BUCKET,
            "prefix": DEFAULT_PREFIX,
            "selection_rule":
                "UNIQUE_EARLIEST_COMPLETE_CATALOG_BATCH_AFTER_LOCAL_SEAL",
            "seal_sha256": seal_binding["sha256"],
            "historical_metadata_authority": historical_authority,
            "historical_metadata_authority_sha256":
                historical_authority_sha,
            "seal_sealed_at_utc": _utc(sealed_at, "local seal sealed_at"),
            "known_batch_minute_utc":
                LEGACY_EXPECTED_BATCH_MINUTES[date],
            "history_limits": {
                "max_pages_per_key": MAX_HISTORY_PAGES,
                "max_rows_per_key": MAX_HISTORY_ROWS,
            },
            "catalog_histories": [
                {"logical_source_key": logical, **history}
                for logical, history in sorted(histories.items())],
            "selected_versions": [{
                "logical_source_key": spec["logical_source_key"],
                "key": spec["key"], "VersionId": row["VersionId"],
                "size": row["Size"],
                "LastModified": row["LastModified"],
            } for spec, row in selected],
        }
        proof_path, proof_sha = _write_history_proof(
            proof_root, date, proof)
        catalog_files, objects = [], []
        for spec, row in selected:
            output = temp / (
                "catalog-" + hashlib.sha256(
                    spec["key"].encode()).hexdigest() + ".bin")
            response = client.get_exact(
                spec["key"], row["VersionId"], output)
            observed = _attest_file(output)
            output.unlink()
            exact_head = client.head(spec["key"], row["VersionId"])
            if (response.get("VersionId") != row["VersionId"]
                    or response.get("ContentLength") != observed[0]
                    or observed[0] != row["Size"]
                    or exact_head.get("VersionId") != row["VersionId"]
                    or exact_head.get("ContentLength") != row["Size"]
                    or _utc(exact_head.get("LastModified"),
                            "catalog exact LastModified")
                    != row["LastModified"]):
                raise WitnessError(
                    "LEGACY_EXACT_READ_INVALID", spec["key"])
            catalog_files.append({
                "relative_path": spec["relative_path"],
                "size": observed[0], "sha256": observed[1],
            })
            objects.append({
                "logical_source_key": spec["logical_source_key"],
                "bucket": DEFAULT_BUCKET, "key": spec["key"],
                "VersionId": row["VersionId"], "size": observed[0],
                "sha256": observed[1],
                "LastModified": row["LastModified"],
            })
        catalog = pg.build_manifest("catalog", catalog_files)
        dim_files = []
        for name in cr.DIM_REQUIRED:
            rel = "dim/snapshots/date=%s/%s" % (date, name)
            logical = "warehouse/" + rel
            key = cr._join_key(DEFAULT_PREFIX, "warehouse", rel)
            head = client.head(key)
            version_id = head.get("VersionId")
            size = head.get("ContentLength")
            if (not _valid_version(version_id)
                    or not isinstance(size, int) or isinstance(size, bool)
                    or size < 0):
                raise WitnessError("LEGACY_DIM_INVALID", key)
            output = temp / ("dim-" + name.replace(".", "-") + ".bin")
            response = client.get_exact(key, version_id, output)
            observed = _attest_file(output)
            output.unlink()
            if (response.get("VersionId") != version_id
                    or response.get("ContentLength") != observed[0]
                    or observed[0] != size):
                raise WitnessError("LEGACY_DIM_INVALID", key)
            modified = _utc(head.get("LastModified"), "dim LastModified")
            dim_files.append({
                "relative_path": rel, "size": observed[0],
                "sha256": observed[1],
            })
            objects.append({
                "logical_source_key": logical, "bucket": DEFAULT_BUCKET,
                "key": key, "VersionId": version_id,
                "size": observed[0], "sha256": observed[1],
                "LastModified": modified,
            })
        unbound_dim_source_id = fcr.legacy_dim_unbound_source_id(
            date, DEFAULT_BUCKET, DEFAULT_PREFIX,
            sorted(dim_files, key=lambda row: row["relative_path"]))
        dim = pg.build_manifest(
            "dim", dim_files, date=date,
            source_catalog_generation_id=unbound_dim_source_id)
        objects.sort(key=lambda row: row["logical_source_key"])
        legacy = {
            "selection_authority": "FIRST_POST_SEAL_CATALOG_FULL_SYNC_BATCH",
            "seal_sealed_at_utc": _utc(sealed_at, "local seal sealed_at"),
            "catalog_batch_start_utc": batch_start.strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "catalog_batch_end_utc": batch_end.strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "selection_rule":
                "UNIQUE_EARLIEST_COMPLETE_CATALOG_BATCH_AFTER_SEAL",
            "dim_selection_rule": "DATED_EXACT_VERSION",
            "catalog_history_proof_sha256": proof_sha,
            "limitations": list(fcr.LEGACY_MIGRATION_LIMITATIONS),
        }
        witness = fcr.build_generation_witness_payload(
            date, DEFAULT_BUCKET, DEFAULT_PREFIX, exact_seal,
            catalog, dim, objects,
            generation_authority=fcr.GENERATION_AUTHORITY_LEGACY,
            legacy_migration=legacy,
            catalog_dim_coherence_claim=False)
        exact_witness, witness_created = _publish_witness(
            client, witness, temp)
    return {
        "schema_version": "canonical-generation-witness-migration-v1",
        "state": "LEGACY_GENERATION_WITNESS_READY",
        "date": date,
        "generation_authority": fcr.GENERATION_AUTHORITY_LEGACY,
        "catalog_dim_coherence_claim": False,
        "catalog_generation_id": catalog["generation_id"],
        "dim_generation_id": dim["generation_id"],
        "history_proof": str(proof_path),
        "history_proof_sha256": proof_sha,
        "historical_metadata_authority_sha256":
            historical_authority_sha,
        "witness_object": exact_witness,
        "original_object_puts": 0,
        "witness_puts": int(witness_created),
        "existing_witness_reused": False,
        "research_prefix_writes": 0,
        "copy_operations": 0,
        "tag_writes": 0,
        "rfq": "OFF",
        "seal_sha256": seal_binding["sha256"],
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("publish-future", "migrate-legacy"):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument("--date", required=True)
        command.add_argument("--raw-root", required=True)
        command.add_argument("--warehouse-root", required=True)
        command.add_argument("--bucket", default=DEFAULT_BUCKET)
        command.add_argument("--prefix", default=DEFAULT_PREFIX)
        command.add_argument(
            "--publisher-principal", default=DEFAULT_PUBLISHER_ARN)
        command.add_argument("--aws-cli", default=PINNED_AWS_CLI)
        command.add_argument(
            "--authorization-file", default=str(DEFAULT_AUTHORIZATION_FILE))
        command.add_argument("--operator-approved", action="store_true")
    sub.choices["publish-future"].add_argument(
        "--lock-timeout", type=float, default=60.0)
    sub.choices["migrate-legacy"].add_argument(
        "--proof-root", required=True)
    sub.choices["migrate-legacy"].add_argument(
        "--quality-dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if not args.operator_approved:
            raise WitnessError(
                "OPERATOR_GATE", "--operator-approved is required")
        if args.bucket != DEFAULT_BUCKET or args.prefix != DEFAULT_PREFIX:
            raise WitnessError(
                "CANONICAL_SCOPE_INVALID", "bucket/prefix are fixed")
        if args.publisher_principal != DEFAULT_PUBLISHER_ARN:
            raise WitnessError(
                "PUBLISHER_IDENTITY_MISMATCH", args.publisher_principal)
        raw_root = _exact_existing_production_dir(
            args.raw_root, PRODUCTION_RAW_ROOT, "raw root")
        warehouse_root = _exact_existing_production_dir(
            args.warehouse_root, PRODUCTION_WAREHOUSE_ROOT, "warehouse root")
        if (args.command == "migrate-legacy"
                and pathlib.Path(args.proof_root)
                != PRODUCTION_PROOF_ROOT):
            raise WitnessError(
                "PRODUCTION_PATH_INVALID", "legacy proof root is fixed")
        if args.command == "migrate-legacy":
            _exact_existing_production_dir(
                args.quality_dir, PRODUCTION_QUALITY_ROOT, "quality root")
            _exact_existing_production_dir(
                PRODUCTION_PROOF_ROOT.parent,
                PRODUCTION_PROOF_ROOT.parent,
                "canonical receipt root")
        client = AwsCli(
            args.aws_cli, args.bucket, args.authorization_file)
        if args.command == "publish-future":
            result = publish_future_generation(
                args.date, str(raw_root), str(warehouse_root), client,
                publisher_arn=args.publisher_principal,
                lock_timeout=args.lock_timeout)
        else:
            result = migrate_legacy_generation(
                args.date, str(raw_root), str(warehouse_root),
                str(PRODUCTION_QUALITY_ROOT), str(PRODUCTION_PROOF_ROOT),
                client,
                publisher_arn=args.publisher_principal)
        result["publisher_code_commit"] = client.code_commit
        result["authorization_sha256"] = client.authorization_sha256
        print(json.dumps(result, sort_keys=True))
        return 0
    except (WitnessError, cr.ReceiptError) as exc:
        print(f"GENERATION_WITNESS_REFUSED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
