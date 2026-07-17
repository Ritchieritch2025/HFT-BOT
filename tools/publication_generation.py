#!/usr/bin/env python3
"""Crash-aware publication generations for catalog and dated dim groups.

The locks live outside the published directories, so replacing a data file can
never replace the inode on which the lock is held.  Every cooperating reader
must take a shared lock; producers stage complete bytes first and take an
exclusive lock only for the short replace/manifest transaction.

The transaction rolls back ordinary process errors.  A machine loss between
data replacement and manifest replacement is still detectable: the old
generation manifest remains and strict readers reject the mismatching bytes.
"""
import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import stat
import tempfile
import time


SCHEMA_VERSION = "publication-generation-v1"
LOCK_ORDER = ("catalog", "dim")
LOCK_DIR = ".publication-locks"
MANIFEST_DIR = ".publication-generations"


class GenerationError(RuntimeError):
    """A generation is busy, malformed, incomplete, or changed."""


def canonical_sha256(value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_relative_path(value):
    if (not isinstance(value, str) or not value or "\x00" in value
            or "\\" in value):
        raise GenerationError("invalid relative path %r" % value)
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise GenerationError("unsafe relative path %r" % value)
    normalized = "/".join(path.parts)
    if normalized != value.replace(os.sep, "/"):
        raise GenerationError("non-canonical relative path %r" % value)
    return normalized


def _validate_date(group, date):
    if group == "catalog":
        if date is not None:
            raise GenerationError("catalog generation cannot be dated")
        return
    if group != "dim" or not isinstance(date, str) or len(date) != 10:
        raise GenerationError("dim generation requires an ISO date")
    try:
        parsed = datetime.date.fromisoformat(date)
    except ValueError as exc:
        raise GenerationError("invalid dim generation date: %s" % exc)
    if parsed.isoformat() != date:
        raise GenerationError("dim generation date is not canonical")


def _reject_symlink_components(root, rel):
    root = os.path.abspath(root)
    if os.path.lexists(root) and os.path.islink(root):
        raise GenerationError("generation root is a symlink")
    current = root
    parts = safe_relative_path(rel).split("/")
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current) and os.path.islink(current):
            raise GenerationError("generation path contains a symlink: %s" % rel)


def _regular_file_attestation(path):
    """Hash one pinned regular file and reject links/path replacement."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise GenerationError("cannot open generation file %s: %s" %
                              (path, exc))
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise GenerationError("generation member is not regular: %s" % path)
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        try:
            named = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise GenerationError("generation path changed: %s: %s" %
                                  (path, exc))
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                              s.st_ctime_ns)
        if (not stat.S_ISREG(named.st_mode) or identity(before) != identity(after)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
                or size != after.st_size):
            raise GenerationError("generation file changed while reading: %s" %
                                  path)
        return {"relative_path": None, "size": size,
                "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def attest_files(root, relative_paths):
    root = os.path.abspath(root)
    rows = []
    seen = set()
    for raw in sorted(relative_paths):
        rel = safe_relative_path(raw)
        if rel in seen:
            raise GenerationError("duplicate generation member %s" % rel)
        seen.add(rel)
        _reject_symlink_components(root, rel)
        row = _regular_file_attestation(
            os.path.join(root, *rel.split("/")))
        row["relative_path"] = rel
        rows.append(row)
    if not rows:
        raise GenerationError("generation has no files")
    return rows


def build_manifest(group, files, *, date=None,
                   source_catalog_generation_id=None):
    if group not in LOCK_ORDER:
        raise GenerationError("unsupported generation group %r" % group)
    _validate_date(group, date)
    if group == "catalog" and source_catalog_generation_id is not None:
        raise GenerationError("catalog generation cannot name a source catalog")
    if group == "dim" and (not isinstance(source_catalog_generation_id, str)
                           or len(source_catalog_generation_id) != 64
                           or any(ch not in "0123456789abcdef"
                                  for ch in source_catalog_generation_id)):
        raise GenerationError("dim source catalog generation is invalid")
    clean = []
    for raw in files:
        if not isinstance(raw, dict):
            raise GenerationError("generation file entry is not an object")
        rel = safe_relative_path(raw.get("relative_path"))
        if not rel.startswith(group + "/"):
            raise GenerationError("generation member escapes %s" % group)
        size, digest = raw.get("size"), raw.get("sha256")
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)):
            raise GenerationError("invalid generation attestation for %s" % rel)
        clean.append({"relative_path": rel, "size": size, "sha256": digest})
    clean.sort(key=lambda row: row["relative_path"])
    if not clean or len({row["relative_path"] for row in clean}) != len(clean):
        raise GenerationError("generation members are empty or duplicated")
    core = {
        "schema_version": SCHEMA_VERSION,
        "group": group,
        "date": date,
        "source_catalog_generation_id": source_catalog_generation_id,
        "files": clean,
    }
    return dict(core, generation_id=canonical_sha256(core))


def manifest_path(warehouse_root, group, date=None):
    _validate_date(group, date)
    root = os.path.abspath(warehouse_root)
    if os.path.lexists(root) and os.path.islink(root):
        raise GenerationError("warehouse root is a symlink")
    control = os.path.join(root, MANIFEST_DIR)
    if os.path.lexists(control) and os.path.islink(control):
        raise GenerationError("generation manifest directory is a symlink")
    base = os.path.join(control, group)
    if group == "dim" and os.path.lexists(base) and os.path.islink(base):
        raise GenerationError("dim generation manifest directory is a symlink")
    return (base + ".json" if group == "catalog"
            else os.path.join(base, "date=%s.json" % date))


def _read_regular_bytes(path):
    row = _regular_file_attestation(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        payload = b""
        while len(payload) < row["size"]:
            chunk = os.read(fd, min(1 << 20, row["size"] - len(payload)))
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(fd)
    if len(payload) != row["size"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
        raise GenerationError("manifest changed while reading: %s" % path)
    return payload


def validate_manifest(payload, warehouse_root, *, group, date=None,
                      expected_paths=None, verify_files=True):
    try:
        manifest = (json.loads(payload.decode("utf-8"))
                    if isinstance(payload, bytes) else json.loads(payload))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GenerationError("invalid generation manifest JSON: %s" % exc)
    if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "group", "date",
            "source_catalog_generation_id", "files", "generation_id"}:
        raise GenerationError("generation manifest shape is invalid")
    core = {key: manifest[key] for key in (
        "schema_version", "group", "date", "source_catalog_generation_id",
        "files")}
    if (manifest["schema_version"] != SCHEMA_VERSION
            or manifest["group"] != group or manifest["date"] != date
            or manifest["generation_id"] != canonical_sha256(core)):
        raise GenerationError("generation manifest identity is invalid")
    rebuilt = build_manifest(
        group, manifest["files"], date=date,
        source_catalog_generation_id=manifest["source_catalog_generation_id"])
    if rebuilt != manifest:
        raise GenerationError("generation manifest is not canonical")
    paths = [row["relative_path"] for row in manifest["files"]]
    if expected_paths is not None and set(paths) != {
            safe_relative_path(path) for path in expected_paths}:
        raise GenerationError("generation manifest member set mismatch")
    prefix = group + "/"
    if any(not path.startswith(prefix) for path in paths):
        raise GenerationError("generation member escapes %s" % group)
    if verify_files:
        current = attest_files(warehouse_root, paths)
        if current != manifest["files"]:
            raise GenerationError("generation bytes do not match manifest")
    return manifest


def load_manifest(warehouse_root, group, *, date=None, expected_paths=None,
                  verify_files=True, allow_missing=False):
    path = manifest_path(warehouse_root, group, date)
    try:
        payload = _read_regular_bytes(path)
    except GenerationError:
        if allow_missing and not os.path.lexists(path):
            return None
        raise
    return validate_manifest(
        payload, warehouse_root, group=group, date=date,
        expected_paths=expected_paths, verify_files=verify_files)


def verified_or_synthesized_manifest(warehouse_root, group, relative_paths,
                                     *, date=None,
                                     source_catalog_generation_id=None):
    """Use a producer manifest when present; otherwise bind legacy bytes.

    The fallback keeps existing fixture/upgrade callers compatible.  It is
    explicit in the returned binding and still hashes the complete group while
    callers hold the shared locks.
    """
    manifest = load_manifest(
        warehouse_root, group, date=date, expected_paths=relative_paths,
        verify_files=True, allow_missing=True)
    if manifest is not None:
        return manifest, "PRODUCER_MANIFEST_VERIFIED"
    manifest = build_manifest(
        group, attest_files(warehouse_root, relative_paths), date=date,
        source_catalog_generation_id=source_catalog_generation_id)
    return manifest, "LEGACY_SYNTHESIZED_UNDER_SHARED_LOCK"


def _lock_path(warehouse_root, name):
    if name not in LOCK_ORDER:
        raise GenerationError("unknown lock %r" % name)
    root = os.path.abspath(warehouse_root)
    if os.path.lexists(root) and os.path.islink(root):
        raise GenerationError("warehouse root is a symlink")
    lock_dir = os.path.join(root, LOCK_DIR)
    if os.path.lexists(lock_dir) and os.path.islink(lock_dir):
        raise GenerationError("lock directory is a symlink")
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    path = os.path.join(lock_dir, name + ".lock")
    if os.path.lexists(path) and os.path.islink(path):
        raise GenerationError("lock file is a symlink")
    return path


@contextlib.contextmanager
def generation_locks(warehouse_root, requests, timeout=5.0, poll=0.05):
    """Acquire catalog/dim advisory locks in a single global order.

    ``requests`` maps ``catalog``/``dim`` to ``shared`` or ``exclusive``.
    A zero timeout is fail-fast.
    """
    if not isinstance(requests, dict) or any(
            mode not in ("shared", "exclusive") for mode in requests.values()):
        raise GenerationError("invalid generation lock request")
    unknown = set(requests) - set(LOCK_ORDER)
    if unknown or timeout is None or timeout < 0:
        raise GenerationError("invalid generation lock request")
    deadline = time.monotonic() + timeout
    held = []
    try:
        for name in LOCK_ORDER:
            if name not in requests:
                continue
            path = _lock_path(warehouse_root, name)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            operation = (fcntl.LOCK_SH if requests[name] == "shared"
                         else fcntl.LOCK_EX) | fcntl.LOCK_NB
            while True:
                try:
                    fcntl.flock(fd, operation)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        os.close(fd)
                        raise
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        raise GenerationError(
                            "%s generation is busy (%s lock timeout %.3fs)" %
                            (name, requests[name], timeout))
                    time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
            held.append(fd)
        yield
    finally:
        for fd in reversed(held):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _atomic_manifest_bytes(path, payload):
    parent = os.path.dirname(path)
    if os.path.lexists(parent) and os.path.islink(parent):
        raise GenerationError("manifest directory is a symlink")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-generation-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync; rename remains
            # atomic and the strict byte/manifest verifier still fails closed.
            pass
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def publish_transaction(warehouse_root, staged, manifest, *, aliases=None,
                        replace_func=None):
    """Replace a staged file group and publish its manifest last.

    The caller must hold the matching exclusive lock (and, for dim, a shared
    catalog lock).  ``staged`` maps canonical warehouse-relative paths to
    staged regular files. ``aliases`` may explicitly map an unversioned
    convenience path to one manifest-bound member with identical bytes;
    aliases share the rollback window but not the dated generation identity.
    All ordinary failures restore the previous bytes.
    """
    warehouse_root = os.path.abspath(warehouse_root)
    replace_func = replace_func or os.replace
    if not isinstance(staged, dict) or not staged:
        raise GenerationError("staged generation is empty")
    normalized = {}
    for raw_rel, raw_src in staged.items():
        rel = safe_relative_path(raw_rel)
        if not rel.startswith(manifest["group"] + "/"):
            raise GenerationError("staged path escapes generation group")
        if rel in normalized:
            raise GenerationError("duplicate staged path %s" % rel)
        _regular_file_attestation(raw_src)
        normalized[rel] = os.path.abspath(raw_src)
    manifest_paths = {row["relative_path"] for row in manifest["files"]}
    aliases = aliases or {}
    if not isinstance(aliases, dict):
        raise GenerationError("generation aliases are invalid")
    normalized_aliases = {}
    for raw_alias, raw_target in aliases.items():
        alias = safe_relative_path(raw_alias)
        target = safe_relative_path(raw_target)
        if (alias in manifest_paths or alias in normalized_aliases
                or target not in manifest_paths):
            raise GenerationError("generation alias binding is invalid")
        normalized_aliases[alias] = target
    if manifest_paths | set(normalized_aliases) != set(normalized):
        raise GenerationError(
            "staged member set differs from generation manifest")
    manifest_rows = {
        row["relative_path"]: row for row in manifest["files"]}
    for alias, target in normalized_aliases.items():
        observed = _regular_file_attestation(normalized[alias])
        expected_alias = manifest_rows[target]
        if (observed["size"] != expected_alias["size"]
                or observed["sha256"] != expected_alias["sha256"]):
            raise GenerationError(
                "generation alias bytes differ from bound member")
    staged_attestations = []
    for rel in sorted(manifest_paths):
        row = _regular_file_attestation(normalized[rel])
        row["relative_path"] = rel
        staged_attestations.append(row)
    expected = build_manifest(
        manifest["group"], staged_attestations, date=manifest["date"],
        source_catalog_generation_id=manifest[
            "source_catalog_generation_id"])
    if expected != manifest:
        raise GenerationError("staged bytes do not match generation manifest")

    backup_parent = os.path.join(warehouse_root, ".publication-backups")
    if os.path.lexists(backup_parent) and os.path.islink(backup_parent):
        raise GenerationError("backup directory is a symlink")
    os.makedirs(backup_parent, mode=0o700, exist_ok=True)
    backup = tempfile.mkdtemp(prefix="generation-", dir=backup_parent)
    installed = []
    old_manifest_path = manifest_path(
        warehouse_root, manifest["group"], manifest["date"])
    old_manifest = (_read_regular_bytes(old_manifest_path)
                    if os.path.exists(old_manifest_path) else None)
    try:
        for rel in sorted(normalized):
            dst = os.path.join(warehouse_root, *rel.split("/"))
            _reject_symlink_components(warehouse_root, rel)
            parent = os.path.dirname(dst)
            if os.path.lexists(parent) and os.path.islink(parent):
                raise GenerationError("canonical parent is a symlink: %s" % rel)
            os.makedirs(parent, exist_ok=True)
            backup_path = os.path.join(backup, *rel.split("/"))
            if os.path.lexists(dst):
                if os.path.islink(dst) or not os.path.isfile(dst):
                    raise GenerationError("canonical member is unsafe: %s" % rel)
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                try:
                    os.link(dst, backup_path)
                except OSError:
                    shutil.copy2(dst, backup_path, follow_symlinks=False)
            replace_func(normalized[rel], dst)
            installed.append((rel, dst, backup_path))

        payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(
            "utf-8")
        _atomic_manifest_bytes(old_manifest_path, payload)
    except BaseException:
        for _rel, dst, backup_path in reversed(installed):
            try:
                if os.path.exists(backup_path):
                    os.replace(backup_path, dst)
                elif os.path.lexists(dst):
                    os.unlink(dst)
            except OSError:
                pass
        try:
            if old_manifest is not None:
                _atomic_manifest_bytes(old_manifest_path, old_manifest)
            elif os.path.lexists(old_manifest_path):
                os.unlink(old_manifest_path)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
