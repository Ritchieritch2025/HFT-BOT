#!/usr/bin/env python3
"""Produce a root-pinnable PnL-spine lineage receipt from exact S3 versions.

The production transport in this module is intentionally narrower than a
general S3 client:

* it runs only on the fixed W09 instance;
* it obtains temporary credentials only from IMDSv2;
* it accepts only the ``w09-research-runner`` instance profile and role;
* it implements only ``GetObjectVersion`` (a GET carrying ``versionId``);
* it refuses static/profile/web-identity/container credential configuration;
* it verifies the full byte count and SHA-256 before parsing any source.

Tests and offline audits inject an ``ExactVersionReader``.  That injectable
boundary does not weaken the CLI: ``main`` always constructs the W09 reader.
The resulting document is accepted by :mod:`tools.research.pnl_spine.runner`
only after an operator-controlled external SHA pin is supplied to that runner.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
from decimal import Decimal
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any, BinaryIO, Iterable, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import terminal_lineage_bridge as _terminal_bridge
except ImportError:  # pragma: no cover - direct root-owned script execution
    import terminal_lineage_bridge as _terminal_bridge


MANIFEST_PINS_SCHEMA = "pnl-spine-exact-manifest-pins-v1"
RECORD_SPEC_SCHEMA = "pnl-spine-record-extraction-spec-v2"
HYBRID_RECORD_SPEC_SCHEMA = "pnl-spine-record-extraction-spec-v3"
LEGACY_RECORD_SPEC_SCHEMA = "pnl-spine-record-extraction-spec-v1"
EXPECTED_MANIFEST_COUNT = 8
TRUSTED_BUCKET = "kalshi-vault-ritcardo"
W09_INSTANCE_ID = "i-0e53d134dceffe166"
W09_ACCOUNT_ID = "321572485933"
W09_PROFILE = "w09-research-runner"
W09_ROLE = "w09-research-runner"
W09_INSTANCE_PROFILE_ARN = (
    f"arn:aws:iam::{W09_ACCOUNT_ID}:instance-profile/{W09_PROFILE}"
)
W09_REGION = "us-east-2"
W09_S3_HOST = f"{TRUSTED_BUCKET}.s3.{W09_REGION}.amazonaws.com"
IMDS_ROOT = "http://169.254.169.254/latest"
REFRESH_SKEW_SECONDS = 300
MAX_CONTROL_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELEASE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})__v3ref__seal-([0-9a-f]{8})"
    r"__pub-([0-9a-f]{16})$"
)
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SOURCE_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
PARQUET_RUNTIME_RECEIPT_SCHEMA = "pnl-spine-parquet-runtime-receipt-v1"
PARQUET_RUNTIME_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
PARQUET_RUNTIME_POLICY = {
    "threads": 1,
    "memory_limit_bytes": PARQUET_RUNTIME_MEMORY_LIMIT_BYTES,
    "max_temp_directory_bytes": 0,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "enable_object_cache": False,
}


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay an IMDS token or SigV4 header across a redirect."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _direct_no_redirect_opener() -> urllib.request.OpenerDirector:
    """Build a transport that ignores proxy environment and forbids redirects."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirectHandler(),
    )


def _open_fixed_url(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> BinaryIO:
    """Open one exact URL without proxying or following a Location header."""

    expected_url = request.full_url
    response = _direct_no_redirect_opener().open(request, timeout=timeout)
    if response.geturl() != expected_url:
        response.close()
        raise ProductionLineageError("transport response URL changed")
    return response


def _canonical_value(value: object) -> object:
    """Canonicalize fixed-point JSON without importing repository code."""

    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(
        f"unsupported canonical value {type(value).__name__}; "
        "floats are forbidden"
    )


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


LINEAGE_RECEIPT_SCHEMA = "pnl-spine-lineage-receipt-v1"
HYBRID_LINEAGE_RECEIPT_SCHEMA = "pnl-spine-lineage-receipt-v2"
LINEAGE_RECORD_SCHEMA_SHA256 = canonical_sha256(
    {
        "object_read_key": [
            "release_id",
            "logical_key",
            "version_id",
        ],
        "record_key": ["kind", "record_id"],
        "record_kinds": [
            "L2_SNAPSHOT",
            "NORMALIZED_ROW",
            "PUBLIC_TRADE",
            "SETTLEMENT",
        ],
        "source_member_key": [
            "release_id",
            "logical_key",
            "version_id",
            "locator_schema",
            "locator",
        ],
    }
)
HYBRID_LINEAGE_RECORD_SCHEMA_SHA256 = canonical_sha256(
    {
        "exact_object_read_key": [
            "release_id",
            "logical_key",
            "version_id",
        ],
        "record_key": ["kind", "record_id"],
        "record_kinds": [
            "L2_SNAPSHOT",
            "NORMALIZED_ROW",
            "PUBLIC_TRADE",
            "SETTLEMENT",
        ],
        "source_member_variants": {
            "EXACT_RELEASE_OBJECT": [
                "release_id",
                "logical_key",
                "version_id",
                "locator_schema",
                "locator",
            ],
            "OFFICIAL_TERMINAL_CAPTURE": [
                "shard_id",
                "ticker",
                "capture_receipt_raw_sha256",
                "raw_pins_raw_sha256",
                "normalized_output_raw_sha256",
                "selected_raw_response_sha256",
            ],
        },
        "terminal_temporality": (
            "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"
        ),
    }
)
EXTRACTOR_CODE_BUNDLE_SCHEMA = "pnl-spine-extractor-code-bundle-v1"
EXTRACTOR_CODE_BUNDLE_MEMBERS = (
    "production_lineage.py",
    "terminal_lineage_bridge.py",
)

RECORD_KINDS = frozenset(
    {"L2_SNAPSHOT", "NORMALIZED_ROW", "PUBLIC_TRADE", "SETTLEMENT"}
)
ID_FIELDS = {
    "NORMALIZED_ROW": "row_id",
    "PUBLIC_TRADE": "trade_id",
    "L2_SNAPSHOT": "snapshot_id",
    "SETTLEMENT": "settlement_id",
}
FIXTURE_COLLECTIONS = {
    "NORMALIZED_ROW": "rows",
    "PUBLIC_TRADE": "public_trades",
    "L2_SNAPSHOT": "exit_snapshots",
    "SETTLEMENT": "settlements",
}


class ProductionLineageError(ValueError):
    """A production extraction or immutable binding failed closed."""


def _require_non_root_execution() -> None:
    if os.geteuid() == 0:
        raise ProductionLineageError(
            "production lineage must run as a dedicated non-root identity"
        )


class _VerifiedParquetRuntime:
    """Opaque proof that this process matches one externally pinned runtime."""

    __slots__ = ("raw_sha256", "receipt")

    def __init__(
        self,
        *,
        raw_sha256: str,
        receipt: Mapping[str, Any],
    ) -> None:
        self.raw_sha256 = raw_sha256
        self.receipt = receipt


class ExactVersionReader(Protocol):
    """The only transport operation the extractor needs."""

    def iter_exact_version(
        self,
        bucket: str,
        key: str,
        version_id: str,
    ) -> Iterable[bytes]:
        """Yield the complete body for exactly one VersionId."""


def _strict_keys(
    label: str,
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    unknown = sorted(set(value) - required - optional)
    missing = sorted(required - set(value))
    if unknown:
        raise ProductionLineageError(
            f"{label} has unknown fields: {','.join(unknown)}"
        )
    if missing:
        raise ProductionLineageError(
            f"{label} is missing fields: {','.join(missing)}"
        )


def _mapping(label: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ProductionLineageError(f"{label} must be an object")
    return value


def _list(label: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ProductionLineageError(f"{label} must be an array")
    return value


def _text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProductionLineageError(f"{label} must be non-empty text")
    if any(ord(character) < 0x20 for character in value):
        raise ProductionLineageError(f"{label} contains a control character")
    return value


def _sha(label: str, value: object) -> str:
    result = _text(label, value)
    if SHA256_RE.fullmatch(result) is None:
        raise ProductionLineageError(f"{label} must be lowercase SHA-256")
    return result


def _plain_int(label: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProductionLineageError(
            f"{label} must be a plain integer >= {minimum}"
        )
    return value


def _date(label: str, value: object) -> str:
    result = _text(label, value)
    if DATE_RE.fullmatch(result) is None:
        raise ProductionLineageError(f"{label} must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(result)
    except ValueError as exc:
        raise ProductionLineageError(f"{label} is not a real date") from exc
    return result


def _safe_key(label: str, value: object) -> str:
    result = _text(label, value)
    if (
        result.startswith(("/", "\\"))
        or "\\" in result
        or any(part in {"", ".", ".."} for part in result.split("/"))
    ):
        raise ProductionLineageError(f"{label} is not a safe relative key")
    return result


def _version(label: str, value: object) -> str:
    result = _text(label, value)
    if result.strip().lower() == "null" or len(result) > 1024:
        raise ProductionLineageError(f"{label} is not an exact VersionId")
    return result


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionLineageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ProductionLineageError(
                    f"{label} contains non-finite JSON: {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionLineageError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    return _mapping(label, value)


def _canonical_hash(value: object, label: str) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as exc:
        raise ProductionLineageError(
            f"{label} is not fixed-point canonical JSON: {exc}"
        ) from exc


def _read_control_file(
    path: Path,
    *,
    expected_raw_sha256: str,
    label: str,
) -> Mapping[str, Any]:
    """Read a root-pinned control file once without following a final symlink."""

    expected = _sha(f"{label} expected SHA-256", expected_raw_sha256)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProductionLineageError(f"{label} cannot be opened: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProductionLineageError(f"{label} must be a regular file")
        if info.st_size > MAX_CONTROL_BYTES:
            raise ProductionLineageError(f"{label} exceeds 16 MiB")
        chunks: list[bytes] = []
        remaining = MAX_CONTROL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_CONTROL_BYTES:
        raise ProductionLineageError(f"{label} exceeds 16 MiB")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ProductionLineageError(f"{label} raw SHA-256 differs from pin")
    return _strict_json(raw, label)


def _absolute_path_without_symlinks(path: Path) -> Path:
    """Return an absolute path after rejecting every symlink component."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ProductionLineageError(
                f"code deployment path is unavailable: {current}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ProductionLineageError(
                f"code deployment path contains a symlink: {current}"
            )
    return absolute


def _sha256_regular_file(
    path: Path,
    *,
    require_root_read_only: bool,
) -> str:
    """Hash one regular file through a no-follow descriptor.

    The production mode additionally requires a root-owned, non-writable file
    below root-owned directories that are not writable by group/other.  W09
    runs the producer as an unprivileged account, so that deployment boundary
    prevents a post-import path or byte swap while the external code pin is
    checked and the receipt is generated.
    """

    absolute = _absolute_path_without_symlinks(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise ProductionLineageError(
            "extractor code cannot be opened without following symlinks"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProductionLineageError(
                "extractor code path is not a regular file"
            )
        if require_root_read_only:
            if info.st_uid != 0 or info.st_mode & 0o222:
                raise ProductionLineageError(
                    "production extractor code must be root-owned and "
                    "read-only"
                )
            current = absolute.parent
            while True:
                directory = os.lstat(current)
                if (
                    directory.st_uid != 0
                    or directory.st_mode & 0o022
                    or not stat.S_ISDIR(directory.st_mode)
                ):
                    raise ProductionLineageError(
                        "production extractor parent directories must be "
                        "root-owned and not group/other-writable"
                    )
                if current == Path(current.anchor):
                    break
                current = current.parent
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def production_extractor_code_sha256(
    *,
    require_root_read_only: bool = False,
) -> str:
    """Return the canonical hash of every repository code dependency.

    The producer intentionally contains its canonicalization and lineage
    constants locally and imports no repository module at runtime.  This
    leaves one reviewable repository-code member.  Standard-library/runtime
    identity is a separate execution-environment receipt; production Parquet
    locators additionally require their audited DuckDB runtime pin.
    """

    package = Path(__file__).parent
    members = [
        {
            "name": name,
            "raw_sha256": _sha256_regular_file(
                package / name,
                require_root_read_only=require_root_read_only,
            ),
        }
        for name in EXTRACTOR_CODE_BUNDLE_MEMBERS
    ]
    return canonical_sha256(
        {
            "schema_version": EXTRACTOR_CODE_BUNDLE_SCHEMA,
            "members": members,
        }
    )


def validate_extractor_code_pin(
    expected_sha256: object,
    *,
    require_root_read_only: bool = False,
) -> str:
    """Verify the loaded producer bundle against a reviewed external pin."""

    expected = _sha(
        "expected extractor code SHA-256", expected_sha256
    )
    actual = production_extractor_code_sha256(
        require_root_read_only=require_root_read_only
    )
    if actual != expected:
        raise ProductionLineageError(
            "installed extractor code-bundle SHA-256 differs from external pin"
        )
    return actual


def _path_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(path), os.fspath(root))) == (
            os.fspath(root)
        )
    except ValueError:
        return False


def _runtime_file_fact(
    path: Path,
    *,
    runtime_root: Path,
    role: str,
    require_root_read_only: bool,
) -> dict[str, Any]:
    absolute = _absolute_path_without_symlinks(path)
    if not _path_within(absolute, runtime_root):
        raise ProductionLineageError(
            "DuckDB runtime file escaped the isolated Python prefix"
        )
    try:
        info = os.lstat(absolute)
    except OSError as exc:
        raise ProductionLineageError(
            "DuckDB runtime file is unavailable"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProductionLineageError(
            "DuckDB runtime member must be a regular file"
        )
    return {
        "relative_path": absolute.relative_to(runtime_root).as_posix(),
        "role": role,
        "size_bytes": info.st_size,
        "raw_sha256": _sha256_regular_file(
            absolute,
            require_root_read_only=require_root_read_only,
        ),
    }


def _duckdb_setting_bytes(value: object, label: str) -> int:
    text_value = _text(label, value)
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?) (bytes|KiB|MiB|GiB|TiB)",
        text_value,
    )
    if match is None:
        raise ProductionLineageError(
            f"{label} has an unknown DuckDB byte-unit rendering"
        )
    multiplier = {
        "bytes": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }[match.group(2)]
    result = Decimal(match.group(1)) * multiplier
    if result != result.to_integral_value():
        raise ProductionLineageError(
            f"{label} does not resolve to a whole byte"
        )
    return int(result)


def _connect_pinned_duckdb(duckdb_module: Any) -> tuple[Any, dict[str, str]]:
    """Open one bounded, extension-off DuckDB session and verify settings."""

    config = {
        "threads": "1",
        "memory_limit": f"{PARQUET_RUNTIME_MEMORY_LIMIT_BYTES}B",
        "max_temp_directory_size": "0B",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_object_cache": "false",
    }
    connection: Any | None = None
    try:
        connection = duckdb_module.connect(database=":memory:", config=config)
        rows = connection.execute(
            "SELECT name,value FROM duckdb_settings() "
            "WHERE name IN ("
            "'threads','memory_limit','max_temp_directory_size',"
            "'autoinstall_known_extensions','autoload_known_extensions',"
            "'enable_object_cache') ORDER BY name"
        ).fetchall()
    except Exception as exc:
        if connection is not None:
            connection.close()
        raise ProductionLineageError(
            "DuckDB cannot enforce the fixed Parquet execution policy"
        ) from exc
    effective = {
        str(name): str(value)
        for name, value in rows
    }
    expected_names = {
        "threads",
        "memory_limit",
        "max_temp_directory_size",
        "autoinstall_known_extensions",
        "autoload_known_extensions",
        "enable_object_cache",
    }
    if set(effective) != expected_names:
        connection.close()
        raise ProductionLineageError(
            "DuckDB did not expose every fixed execution setting"
        )
    if (
        effective["threads"] != "1"
        or effective["autoinstall_known_extensions"].lower() != "false"
        or effective["autoload_known_extensions"].lower() != "false"
        or effective["enable_object_cache"].lower() != "false"
        or _duckdb_setting_bytes(
            effective["max_temp_directory_size"],
            "DuckDB max_temp_directory_size",
        )
        != 0
    ):
        connection.close()
        raise ProductionLineageError(
            "DuckDB fixed execution settings were not applied"
        )
    effective_memory = _duckdb_setting_bytes(
        effective["memory_limit"], "DuckDB memory_limit"
    )
    if (
        effective_memory <= 0
        or effective_memory > PARQUET_RUNTIME_MEMORY_LIMIT_BYTES
    ):
        connection.close()
        raise ProductionLineageError(
            "DuckDB effective memory_limit exceeds the fixed cap"
        )
    return connection, effective


def _runtime_root(require_root_read_only: bool) -> Path:
    root = _absolute_path_without_symlinks(Path(sys.prefix))
    base = _absolute_path_without_symlinks(Path(sys.base_prefix))
    if root == base:
        raise ProductionLineageError(
            "Parquet production requires a dedicated Python virtual environment"
        )
    if (
        sys.flags.isolated != 1
        or sys.flags.ignore_environment != 1
        or sys.flags.no_user_site != 1
        or sys.flags.dont_write_bytecode != 1
    ):
        raise ProductionLineageError(
            "Parquet production requires Python -I -B isolation"
        )
    if require_root_read_only:
        current = root
        while True:
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise ProductionLineageError(
                    "isolated Python runtime root is unavailable"
                ) from exc
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise ProductionLineageError(
                    "isolated Python runtime directories must be root-owned "
                    "and not group/other-writable"
                )
            if current == Path(current.anchor):
                break
            current = current.parent
        def refuse_walk_error(error: OSError) -> None:
            raise ProductionLineageError(
                "isolated Python runtime tree cannot be audited completely"
            ) from error

        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=refuse_walk_error,
            followlinks=False,
        ):
            directory_path = Path(directory)
            entries = [
                directory_path / name
                for name in sorted(directory_names + file_names)
            ]
            for path in entries:
                try:
                    info = os.lstat(path)
                except OSError as exc:
                    raise ProductionLineageError(
                        "isolated Python runtime tree changed during audit"
                    ) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise ProductionLineageError(
                        "isolated Python runtime tree contains a symlink"
                    )
                if info.st_uid != 0:
                    raise ProductionLineageError(
                        "isolated Python runtime tree must be root-owned"
                    )
                if stat.S_ISDIR(info.st_mode):
                    if info.st_mode & 0o022:
                        raise ProductionLineageError(
                            "isolated Python runtime directory is "
                            "group/other-writable"
                        )
                elif stat.S_ISREG(info.st_mode):
                    if info.st_mode & 0o222:
                        raise ProductionLineageError(
                            "isolated Python runtime files must be read-only"
                        )
                else:
                    raise ProductionLineageError(
                        "isolated Python runtime has a non-file entry"
                    )
    return root


def build_parquet_runtime_receipt(
    *,
    require_root_read_only: bool = True,
) -> dict[str, Any]:
    """Measure the exact isolated Python and DuckDB bytes in this process."""

    if "duckdb" in sys.modules or "_duckdb" in sys.modules:
        raise ProductionLineageError(
            "DuckDB was imported before runtime bytes were measured"
        )
    runtime_root = _runtime_root(require_root_read_only)
    base = _absolute_path_without_symlinks(Path(sys.base_prefix))
    executable = _absolute_path_without_symlinks(Path(sys.executable))
    if not _path_within(executable, runtime_root):
        raise ProductionLineageError(
            "Python executable escaped the isolated runtime prefix"
        )
    executable_fact = _runtime_file_fact(
        executable,
        runtime_root=runtime_root,
        role="PYTHON_EXECUTABLE",
        require_root_read_only=require_root_read_only,
    )

    package_spec = importlib.util.find_spec("duckdb")
    native_spec = importlib.util.find_spec("_duckdb")
    if (
        package_spec is None
        or package_spec.origin is None
        or package_spec.submodule_search_locations is None
        or native_spec is None
        or native_spec.origin is None
    ):
        raise ProductionLineageError(
            "isolated runtime lacks the DuckDB package/native extension"
        )
    package_locations = list(package_spec.submodule_search_locations)
    if len(package_locations) != 1:
        raise ProductionLineageError(
            "DuckDB package must have one fixed package root"
        )
    package_root = _absolute_path_without_symlinks(
        Path(package_locations[0])
    )
    module_path = _absolute_path_without_symlinks(Path(package_spec.origin))
    native_path = _absolute_path_without_symlinks(Path(native_spec.origin))
    if not any(
        os.fspath(native_path).endswith(suffix)
        for suffix in importlib.machinery.EXTENSION_SUFFIXES
    ):
        raise ProductionLineageError(
            "DuckDB native module is not a Python extension binary"
        )
    if (
        not _path_within(package_root, runtime_root)
        or not _path_within(module_path, package_root)
        or not _path_within(native_path, runtime_root)
    ):
        raise ProductionLineageError(
            "DuckDB package/native extension escaped the isolated runtime"
        )

    files: list[dict[str, Any]] = []
    native_in_package = False
    for directory, directory_names, file_names in os.walk(
        package_root,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for name in directory_names:
            if stat.S_ISLNK(os.lstat(directory_path / name).st_mode):
                raise ProductionLineageError(
                    "DuckDB package tree contains a symlink"
                )
        for name in file_names:
            path = directory_path / name
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ProductionLineageError(
                    "DuckDB package tree contains a symlink"
                )
            absolute_path = _absolute_path_without_symlinks(path)
            role = "DUCKDB_PACKAGE"
            if absolute_path == native_path:
                role = "DUCKDB_NATIVE_EXTENSION"
                native_in_package = True
            files.append(
                _runtime_file_fact(
                    absolute_path,
                    runtime_root=runtime_root,
                    role=role,
                    require_root_read_only=require_root_read_only,
                )
            )
    if not native_in_package:
        files.append(
            _runtime_file_fact(
                native_path,
                runtime_root=runtime_root,
                role="DUCKDB_NATIVE_EXTENSION",
                require_root_read_only=require_root_read_only,
            )
        )
    files.sort(key=lambda row: (row["relative_path"], row["role"]))
    if len({row["relative_path"] for row in files}) != len(files):
        raise ProductionLineageError(
            "DuckDB runtime inventory contains a duplicate path"
        )
    relative_module = module_path.relative_to(runtime_root).as_posix()
    relative_native = native_path.relative_to(runtime_root).as_posix()
    if relative_module not in {
        row["relative_path"] for row in files
    } or relative_native not in {
        row["relative_path"] for row in files
        if row["role"] == "DUCKDB_NATIVE_EXTENSION"
    }:
        raise ProductionLineageError(
            "DuckDB module/native extension is absent from its byte inventory"
        )

    try:
        import duckdb  # type: ignore
        import _duckdb  # type: ignore
    except ImportError as exc:
        raise ProductionLineageError(
            "measured DuckDB runtime cannot be imported"
        ) from exc
    if (
        _absolute_path_without_symlinks(Path(duckdb.__file__)) != module_path
        or _absolute_path_without_symlinks(Path(_duckdb.__file__)) != native_path
    ):
        raise ProductionLineageError(
            "imported DuckDB bytes differ from the measured module paths"
        )
    version = _text("DuckDB version", getattr(duckdb, "__version__", None))
    native_version = _text(
        "DuckDB native version", getattr(_duckdb, "__version__", None)
    )
    if native_version != version:
        raise ProductionLineageError(
            "DuckDB Python package/native extension versions differ"
        )
    connection, effective = _connect_pinned_duckdb(duckdb)
    connection.close()

    payload: dict[str, Any] = {
        "schema_version": PARQUET_RUNTIME_RECEIPT_SCHEMA,
        "policy": dict(PARQUET_RUNTIME_POLICY),
        "python": {
            "base_prefix": os.fspath(base),
            "cache_tag": _text(
                "Python cache tag", sys.implementation.cache_tag
            ),
            "executable": os.fspath(executable),
            "executable_raw_sha256": executable_fact["raw_sha256"],
            "executable_size_bytes": executable_fact["size_bytes"],
            "implementation": _text(
                "Python implementation", sys.implementation.name
            ),
            "prefix": os.fspath(runtime_root),
            "version": (
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}-{sys.version_info.releaselevel}."
                f"{sys.version_info.serial}"
            ),
            "flags": {
                "dont_write_bytecode": sys.flags.dont_write_bytecode,
                "ignore_environment": sys.flags.ignore_environment,
                "isolated": sys.flags.isolated,
                "no_user_site": sys.flags.no_user_site,
            },
        },
        "duckdb": {
            "effective_settings": effective,
            "files": files,
            "module_relative_path": relative_module,
            "native_extension_relative_path": relative_native,
            "native_version": native_version,
            "version": version,
        },
    }
    payload["payload_sha256"] = _canonical_hash(
        payload, "Parquet runtime receipt payload"
    )
    return payload


def _validate_parquet_runtime_receipt_shape(
    value: object,
) -> Mapping[str, Any]:
    receipt = _mapping("Parquet runtime receipt", value)
    top_fields = {
        "schema_version",
        "policy",
        "python",
        "duckdb",
        "payload_sha256",
    }
    _strict_keys(
        "Parquet runtime receipt",
        receipt,
        required=top_fields,
    )
    if receipt["schema_version"] != PARQUET_RUNTIME_RECEIPT_SCHEMA:
        raise ProductionLineageError("unknown Parquet runtime receipt schema")
    supplied_payload_sha256 = _sha(
        "Parquet runtime payload_sha256", receipt["payload_sha256"]
    )
    payload = dict(receipt)
    payload.pop("payload_sha256")
    if supplied_payload_sha256 != _canonical_hash(
        payload, "Parquet runtime receipt"
    ):
        raise ProductionLineageError(
            "Parquet runtime receipt self hash mismatch"
        )
    policy = _mapping("Parquet runtime policy", receipt["policy"])
    _strict_keys(
        "Parquet runtime policy",
        policy,
        required=set(PARQUET_RUNTIME_POLICY),
    )
    if dict(policy) != PARQUET_RUNTIME_POLICY:
        raise ProductionLineageError(
            "Parquet runtime receipt has a non-production execution policy"
        )
    python = _mapping("Parquet runtime Python", receipt["python"])
    _strict_keys(
        "Parquet runtime Python",
        python,
        required={
            "base_prefix",
            "cache_tag",
            "executable",
            "executable_raw_sha256",
            "executable_size_bytes",
            "implementation",
            "prefix",
            "version",
            "flags",
        },
    )
    for field in (
        "base_prefix",
        "cache_tag",
        "executable",
        "implementation",
        "prefix",
        "version",
    ):
        _text(f"Parquet runtime Python.{field}", python[field])
    _sha(
        "Parquet runtime Python.executable_raw_sha256",
        python["executable_raw_sha256"],
    )
    _plain_int(
        "Parquet runtime Python.executable_size_bytes",
        python["executable_size_bytes"],
        minimum=1,
    )
    flags = _mapping("Parquet runtime Python.flags", python["flags"])
    expected_flags = {
        "dont_write_bytecode": 1,
        "ignore_environment": 1,
        "isolated": 1,
        "no_user_site": 1,
    }
    _strict_keys(
        "Parquet runtime Python.flags",
        flags,
        required=set(expected_flags),
    )
    if dict(flags) != expected_flags:
        raise ProductionLineageError(
            "Parquet runtime receipt is not from Python -I -B"
        )

    duckdb = _mapping("Parquet runtime DuckDB", receipt["duckdb"])
    _strict_keys(
        "Parquet runtime DuckDB",
        duckdb,
        required={
            "effective_settings",
            "files",
            "module_relative_path",
            "native_extension_relative_path",
            "native_version",
            "version",
        },
    )
    for field in (
        "module_relative_path",
        "native_extension_relative_path",
        "native_version",
        "version",
    ):
        _text(f"Parquet runtime DuckDB.{field}", duckdb[field])
    effective = _mapping(
        "Parquet runtime DuckDB.effective_settings",
        duckdb["effective_settings"],
    )
    expected_setting_names = {
        "threads",
        "memory_limit",
        "max_temp_directory_size",
        "autoinstall_known_extensions",
        "autoload_known_extensions",
        "enable_object_cache",
    }
    _strict_keys(
        "Parquet runtime DuckDB.effective_settings",
        effective,
        required=expected_setting_names,
    )
    for name in expected_setting_names:
        _text(f"DuckDB effective setting {name}", effective[name])
    files = _list("Parquet runtime DuckDB.files", duckdb["files"])
    if not files:
        raise ProductionLineageError(
            "Parquet runtime DuckDB file inventory is empty"
        )
    identities: list[tuple[str, str]] = []
    native_paths: set[str] = set()
    all_paths: set[str] = set()
    for index, raw in enumerate(files):
        row = _mapping(f"Parquet runtime DuckDB.files[{index}]", raw)
        _strict_keys(
            f"Parquet runtime DuckDB.files[{index}]",
            row,
            required={"relative_path", "role", "size_bytes", "raw_sha256"},
        )
        path = _safe_key(
            f"Parquet runtime DuckDB.files[{index}].relative_path",
            row["relative_path"],
        )
        role = _text(
            f"Parquet runtime DuckDB.files[{index}].role", row["role"]
        )
        if role not in {"DUCKDB_PACKAGE", "DUCKDB_NATIVE_EXTENSION"}:
            raise ProductionLineageError(
                "Parquet runtime file has an unknown role"
            )
        _plain_int(
            f"Parquet runtime DuckDB.files[{index}].size_bytes",
            row["size_bytes"],
        )
        _sha(
            f"Parquet runtime DuckDB.files[{index}].raw_sha256",
            row["raw_sha256"],
        )
        identities.append((path, role))
        all_paths.add(path)
        if role == "DUCKDB_NATIVE_EXTENSION":
            native_paths.add(path)
    if identities != sorted(identities) or len(all_paths) != len(files):
        raise ProductionLineageError(
            "Parquet runtime file inventory is unsorted or duplicated"
        )
    if duckdb["module_relative_path"] not in all_paths:
        raise ProductionLineageError(
            "Parquet runtime module is absent from file inventory"
        )
    if duckdb["native_extension_relative_path"] not in native_paths:
        raise ProductionLineageError(
            "Parquet runtime native extension is absent from file inventory"
        )
    return receipt


def validate_parquet_runtime_receipt(
    value: object,
    *,
    raw_sha256: object,
    require_root_read_only: bool = True,
) -> _VerifiedParquetRuntime:
    """Compare an externally raw-pinned receipt with live runtime bytes."""

    receipt = _validate_parquet_runtime_receipt_shape(value)
    pin = _sha("Parquet runtime receipt raw SHA-256", raw_sha256)
    actual = build_parquet_runtime_receipt(
        require_root_read_only=require_root_read_only
    )
    if canonical_json_bytes(receipt) != canonical_json_bytes(actual):
        raise ProductionLineageError(
            "live Python/DuckDB runtime differs from the pinned receipt"
        )
    return _VerifiedParquetRuntime(raw_sha256=pin, receipt=receipt)


class _IMDSv2Credentials:
    """Expiry-aware W09 instance-role credentials; values are never logged."""

    def __init__(self) -> None:
        self.key_id: str | None = None
        self.secret: str | None = None
        self.session_token: str | None = None
        self.expiration: dt.datetime | None = None

    @staticmethod
    def _request(
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        timeout: float = 2.0,
    ) -> bytes:
        request = urllib.request.Request(url, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with _open_fixed_url(request, timeout=timeout) as response:
                return response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise ProductionLineageError(
                "IMDSv2 is unavailable; credential values were not printed"
            ) from exc

    def refresh(self) -> None:
        token = self._request(
            IMDS_ROOT + "/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        ).decode("utf-8").strip()
        if not token:
            raise ProductionLineageError("IMDSv2 returned an empty token")
        headers = {"X-aws-ec2-metadata-token": token}
        instance_id = self._request(
            IMDS_ROOT + "/meta-data/instance-id",
            headers=headers,
        ).decode("utf-8").strip()
        if instance_id != W09_INSTANCE_ID:
            raise ProductionLineageError(
                f"production lineage is restricted to W09 {W09_INSTANCE_ID}"
            )
        try:
            info = json.loads(
                self._request(
                    IMDS_ROOT + "/meta-data/iam/info",
                    headers=headers,
                )
            )
            profile_arn = info["InstanceProfileArn"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductionLineageError(
                "IMDSv2 returned invalid instance-profile identity"
            ) from exc
        if profile_arn != W09_INSTANCE_PROFILE_ARN:
            raise ProductionLineageError(
                "W09 instance-profile ARN differs from the fixed account "
                "and profile"
            )
        role = self._request(
            IMDS_ROOT + "/meta-data/iam/security-credentials/",
            headers=headers,
        ).decode("utf-8").strip()
        if role != W09_ROLE or "\n" in role:
            raise ProductionLineageError(
                f"W09 role is {role!r}, expected {W09_ROLE!r}"
            )
        try:
            payload = json.loads(
                self._request(
                    IMDS_ROOT
                    + "/meta-data/iam/security-credentials/"
                    + urllib.parse.quote(role, safe="-_.~"),
                    headers=headers,
                )
            )
            if payload.get("Code") != "Success":
                raise ValueError("credential status is not Success")
            expiration = dt.datetime.fromisoformat(
                payload["Expiration"].replace("Z", "+00:00")
            )
            key_id = payload["AccessKeyId"]
            secret = payload["SecretAccessKey"]
            session_token = payload["Token"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductionLineageError(
                "IMDSv2 returned invalid temporary credentials; values were "
                "not printed"
            ) from exc
        if (
            not isinstance(key_id, str)
            or not isinstance(secret, str)
            or not isinstance(session_token, str)
            or not key_id
            or not secret
            or not session_token
            or expiration.tzinfo is None
        ):
            raise ProductionLineageError(
                "IMDSv2 returned incomplete temporary credentials"
            )
        self.key_id = key_id
        self.secret = secret
        self.session_token = session_token
        self.expiration = expiration.astimezone(dt.timezone.utc)

    def current(self) -> tuple[str, str, str]:
        now = dt.datetime.now(dt.timezone.utc)
        if (
            self.expiration is None
            or self.expiration - now
            <= dt.timedelta(seconds=REFRESH_SKEW_SECONDS)
        ):
            self.refresh()
        assert self.key_id is not None
        assert self.secret is not None
        assert self.session_token is not None
        return self.key_id, self.secret, self.session_token


def refuse_non_imds_credentials() -> None:
    """Fail closed if an alternate AWS credential path is configured."""

    forbidden = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    )
    present = sorted(name for name in forbidden if os.environ.get(name))
    if present:
        raise ProductionLineageError(
            "static/profile/federated AWS credential configuration is "
            f"forbidden on W09: {','.join(present)}"
        )


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class W09ExactVersionS3Reader:
    """Read-only SigV4 transport exposing only exact-version GET."""

    def __init__(self, *, region: str = W09_REGION) -> None:
        if region != W09_REGION:
            raise ProductionLineageError(
                f"production S3 region must be exactly {W09_REGION}"
            )
        refuse_non_imds_credentials()
        self.region = W09_REGION
        self.credentials = _IMDSv2Credentials()

    def _request(
        self,
        bucket: str,
        key: str,
        version_id: str,
    ) -> urllib.request.Request:
        if BUCKET_RE.fullmatch(bucket) is None or bucket != TRUSTED_BUCKET:
            raise ProductionLineageError("S3 bucket is outside the trusted vault")
        safe_key = _safe_key("S3 key", key)
        exact_version = _version("S3 VersionId", version_id)
        key_id, secret, token = self.credentials.current()
        now = dt.datetime.now(dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        host = W09_S3_HOST
        canonical_uri = "/" + urllib.parse.quote(safe_key, safe="/-_.~")
        canonical_query = "versionId=" + urllib.parse.quote(
            exact_version, safe="-_.~"
        )
        empty_hash = hashlib.sha256(b"").hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": empty_hash,
            "x-amz-date": amzdate,
            "x-amz-security-token": token,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in sorted(headers)
        )
        canonical_request = "\n".join(
            [
                "GET",
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                empty_hash,
            ]
        )
        scope = f"{datestamp}/{W09_REGION}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amzdate,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
        for component in (W09_REGION, "s3", "aws4_request"):
            signing_key = _hmac(signing_key, component)
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(
            f"https://{host}{canonical_uri}?{canonical_query}",
            method="GET",
        )
        for name, value in headers.items():
            if name != "host":
                request.add_header(name, value)
        return request

    def iter_exact_version(
        self,
        bucket: str,
        key: str,
        version_id: str,
    ) -> Iterable[bytes]:
        request = self._request(bucket, key, version_id)
        try:
            response = _open_fixed_url(request, timeout=120)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(2048).decode("utf-8", "replace")
            except OSError:
                body = ""
            match = re.search(r"<Code>([^<]+)</Code>", body)
            reason = match.group(1) if match else "HTTP_ERROR"
            raise ProductionLineageError(
                f"S3 GetObjectVersion failed with HTTP {exc.code}/{reason}; "
                "credential values were not printed"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ProductionLineageError(
                "S3 GetObjectVersion transport failed; credential values "
                "were not printed"
            ) from exc
        try:
            returned_version = response.headers.get("x-amz-version-id")
            if returned_version != version_id:
                raise ProductionLineageError(
                    "S3 response VersionId differs from requested VersionId"
                )
            while True:
                chunk = response.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()


def _read_exact_to_bytes(
    reader: ExactVersionReader,
    *,
    bucket: str,
    key: str,
    version_id: str,
    expected_sha256: str,
    label: str,
) -> bytes:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    for chunk in reader.iter_exact_version(bucket, key, version_id):
        if not isinstance(chunk, bytes):
            raise ProductionLineageError(f"{label} reader yielded non-bytes")
        total += len(chunk)
        if total > MAX_CONTROL_BYTES:
            raise ProductionLineageError(f"{label} exceeds 16 MiB")
        digest.update(chunk)
        chunks.append(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ProductionLineageError(f"{label} raw SHA-256 mismatch")
    return b"".join(chunks)


def _validate_manifest_pins(value: object) -> list[dict[str, str]]:
    root = _mapping("manifest pins", value)
    _strict_keys(
        "manifest pins",
        root,
        required={"schema_version", "pins"},
    )
    if root["schema_version"] != MANIFEST_PINS_SCHEMA:
        raise ProductionLineageError("unknown manifest-pins schema")
    rows = _list("manifest pins.pins", root["pins"])
    if len(rows) != EXPECTED_MANIFEST_COUNT:
        raise ProductionLineageError(
            f"exactly {EXPECTED_MANIFEST_COUNT} manifest pins are required"
        )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(rows):
        row = _mapping(f"manifest pin {index}", raw)
        fields = {
            "bucket",
            "manifest_key",
            "manifest_version_id",
            "manifest_raw_sha256",
            "release_id",
            "date",
        }
        _strict_keys(f"manifest pin {index}", row, required=fields)
        release_id = _text(f"manifest pin {index}.release_id", row["release_id"])
        match = RELEASE_RE.fullmatch(release_id)
        date = _date(f"manifest pin {index}.date", row["date"])
        if match is None or match.group(1) != date:
            raise ProductionLineageError(
                f"manifest pin {index} release/date binding is invalid"
            )
        bucket = _text(f"manifest pin {index}.bucket", row["bucket"])
        if bucket != TRUSTED_BUCKET:
            raise ProductionLineageError(
                f"manifest pin {index} names an untrusted bucket"
            )
        manifest_key = _safe_key(
            f"manifest pin {index}.manifest_key", row["manifest_key"]
        )
        expected_key = f"research/releases/{release_id}/MANIFEST.json"
        if manifest_key != expected_key:
            raise ProductionLineageError(
                f"manifest pin {index} is outside its exact release prefix"
            )
        normalized = {
            "bucket": bucket,
            "manifest_key": manifest_key,
            "manifest_version_id": _version(
                f"manifest pin {index}.manifest_version_id",
                row["manifest_version_id"],
            ),
            "manifest_raw_sha256": _sha(
                f"manifest pin {index}.manifest_raw_sha256",
                row["manifest_raw_sha256"],
            ),
            "release_id": release_id,
            "date": date,
        }
        identity = (bucket, manifest_key, normalized["manifest_version_id"])
        if identity in seen:
            raise ProductionLineageError("duplicate exact manifest pin")
        seen.add(identity)
        result.append(normalized)
    order = [(row["date"], row["release_id"]) for row in result]
    if order != sorted(order) or len({row["date"] for row in result}) != len(
        result
    ):
        raise ProductionLineageError(
            "manifest pins must have unique, sorted release dates"
        )
    return result


def _channel_for_manifest_object(item: Mapping[str, Any]) -> str:
    channel = item.get("channel")
    kind = item.get("kind")
    logical = _text("manifest object logical_key", item.get("logical_key"))
    if kind == "rfq" or channel == "rfq":
        raise ProductionLineageError(
            "RFQ objects are outside this four-record lineage contract"
        )
    if channel == "orderbooks_l1":
        return "L1"
    if channel == "orderbooks_full":
        return "L2"
    if channel == "trades":
        return "TRADES"
    if logical == "warehouse/catalog/settlements/part-00000.parquet":
        return "SETTLEMENT"
    return "CATALOG"


def _parse_v3_manifest(
    raw: bytes,
    *,
    pin: Mapping[str, str],
) -> tuple[Mapping[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    manifest = _strict_json(raw, f"manifest {pin['release_id']}")
    if (
        manifest.get("schema") != "research-release-manifest-v3-reference"
        or manifest.get("schema_version") != 3
        or manifest.get("storage_mode") != "CANONICAL_REFERENCE"
        or manifest.get("publication_status") != "PUBLISHED"
    ):
        raise ProductionLineageError("manifest is not a published v3 reference")
    release_id = _text("manifest release_id", manifest.get("release_id"))
    date = _date("manifest date", manifest.get("date"))
    if release_id != pin["release_id"] or date != pin["date"]:
        raise ProductionLineageError("manifest differs from its external pin")
    match = RELEASE_RE.fullmatch(release_id)
    if match is None:
        raise ProductionLineageError("manifest release_id is malformed")
    publication_sha = _sha(
        "manifest publication_state_sha256",
        manifest.get("publication_state_sha256"),
    )
    source_seal = _mapping("manifest source_seal", manifest.get("source_seal"))
    seal_sha = _sha("manifest source_seal.sha256", source_seal.get("sha256"))
    if match.group(2) != seal_sha[:8] or match.group(3) != publication_sha[:16]:
        raise ProductionLineageError(
            "manifest release_id prefixes do not bind seal/publication state"
        )
    evidence_tier = _text(
        "manifest evidence_tier", manifest.get("evidence_tier")
    )
    if evidence_tier not in {
        "SEALED_CONFIRMATION",
        "SEALED_DEGRADED_EVIDENCE",
    }:
        raise ProductionLineageError("manifest evidence tier is unknown")
    objects: dict[tuple[str, str], dict[str, Any]] = {}
    physical: set[tuple[str, str, str]] = set()
    for index, raw_item in enumerate(
        _list("manifest objects", manifest.get("objects"))
    ):
        item = _mapping(f"manifest object {index}", raw_item)
        required = {
            "logical_key",
            "source_bucket",
            "source_key",
            "source_version_id",
            "size",
            "sha256",
            "kind",
            "channel",
            "date",
            "required",
            "seal_binding",
            "evidence_binding",
        }
        _strict_keys(
            f"manifest object {index}",
            item,
            required=required,
            optional={"source_last_modified_utc"},
        )
        logical = _safe_key(
            f"manifest object {index}.logical_key", item["logical_key"]
        )
        source_bucket = _text(
            f"manifest object {index}.source_bucket", item["source_bucket"]
        )
        if source_bucket != pin["bucket"]:
            raise ProductionLineageError(
                f"manifest object {index} escaped the trusted bucket"
            )
        source_key = _safe_key(
            f"manifest object {index}.source_key", item["source_key"]
        )
        version_id = _version(
            f"manifest object {index}.source_version_id",
            item["source_version_id"],
        )
        object_date = _date(
            f"manifest object {index}.date", item["date"]
        )
        if object_date != date:
            raise ProductionLineageError(
                f"manifest object {index} escaped the release date"
            )
        if item["required"] is not True:
            raise ProductionLineageError(
                f"manifest object {index} is not a required base object"
            )
        descriptor = {
            "release_id": release_id,
            "date": date,
            "logical_key": logical,
            "version_id": version_id,
            "channel": _channel_for_manifest_object(item),
            "source_object_sha256": _sha(
                f"manifest object {index}.sha256", item["sha256"]
            ),
            "size_bytes": _plain_int(
                f"manifest object {index}.size", item["size"]
            ),
            "bucket": source_bucket,
            "source_key": source_key,
        }
        logical_identity = (logical, version_id)
        if logical_identity in objects:
            raise ProductionLineageError(
                "manifest contains duplicate logical exact versions"
            )
        physical_identity = (source_bucket, source_key, version_id)
        if physical_identity in physical:
            raise ProductionLineageError(
                "manifest contains duplicate physical exact versions"
            )
        objects[logical_identity] = descriptor
        physical.add(physical_identity)
    if not objects:
        raise ProductionLineageError("manifest object set is empty")
    return manifest, objects


def _fixture_releases(
    fixture: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    provenance = _mapping("fixture provenance", fixture.get("provenance"))
    return [
        _mapping(f"fixture release {index}", raw)
        for index, raw in enumerate(
            _list("fixture provenance.releases", provenance.get("releases"))
        )
    ]


def _fixture_object_projection(
    release: Mapping[str, Any],
) -> set[tuple[str, str, str, int, str, str]]:
    result: set[tuple[str, str, str, int, str, str]] = set()
    for index, raw in enumerate(
        _list("fixture release.objects", release.get("objects"))
    ):
        item = _mapping(f"fixture release object {index}", raw)
        required = {
            "logical_key",
            "version_id",
            "sha256",
            "size_bytes",
            "channel",
            "date",
        }
        _strict_keys(f"fixture release object {index}", item, required=required)
        row = (
            _safe_key("fixture object logical_key", item["logical_key"]),
            _version("fixture object version_id", item["version_id"]),
            _sha("fixture object sha256", item["sha256"]),
            _plain_int("fixture object size_bytes", item["size_bytes"]),
            _text("fixture object channel", item["channel"]),
            _date("fixture object date", item["date"]),
        )
        if row in result:
            raise ProductionLineageError(
                "fixture release contains a duplicate exact object"
            )
        result.add(row)
    return result


def _preflight_manifest_controls(
    fixture: Mapping[str, Any],
    pin_document: object,
) -> tuple[
    list[dict[str, str]],
    dict[str, Mapping[str, Any]],
    dict[str, set[tuple[str, str, str, int, str, str]]],
    dict[tuple[str, str, str], dict[str, Any]],
]:
    """Validate all manifest/fixture facts available without S3 I/O."""

    pins = _validate_manifest_pins(pin_document)
    releases = _fixture_releases(fixture)
    if len(releases) != EXPECTED_MANIFEST_COUNT:
        raise ProductionLineageError(
            f"fixture must bind exactly {EXPECTED_MANIFEST_COUNT} releases"
        )
    release_by_id: dict[str, Mapping[str, Any]] = {}
    for release in releases:
        release_id = _text("fixture release_id", release.get("release_id"))
        if release_id in release_by_id:
            raise ProductionLineageError("fixture has duplicate release_id")
        release_by_id[release_id] = release
    pinned_release_ids = {row["release_id"] for row in pins}
    if set(release_by_id) != pinned_release_ids:
        raise ProductionLineageError(
            "fixture contains a release absent from manifest pins"
        )

    fixture_projections: dict[
        str, set[tuple[str, str, str, int, str, str]]
    ] = {}
    declared_objects: dict[
        tuple[str, str, str], dict[str, Any]
    ] = {}
    for pin in pins:
        try:
            release = release_by_id[pin["release_id"]]
        except KeyError as exc:
            raise ProductionLineageError(
                "fixture release set differs from manifest pins"
            ) from exc
        if (
            _date("fixture release date", release.get("date")) != pin["date"]
            or _sha(
                "fixture manifest_sha256", release.get("manifest_sha256")
            )
            != pin["manifest_raw_sha256"]
            or _version(
                "fixture manifest_version_id",
                release.get("manifest_version_id"),
            )
            != pin["manifest_version_id"]
        ):
            raise ProductionLineageError(
                f"fixture release {pin['release_id']} differs from manifest pin"
            )
        projection = _fixture_object_projection(release)
        fixture_projections[pin["release_id"]] = projection
        for logical_key, version_id, sha256, size, channel, date in projection:
            identity = (pin["release_id"], logical_key, version_id)
            if identity in declared_objects:
                raise ProductionLineageError(
                    "fixture declares a duplicate exact logical object"
                )
            declared_objects[identity] = {
                "release_id": pin["release_id"],
                "logical_key": logical_key,
                "version_id": version_id,
                "source_object_sha256": sha256,
                "size_bytes": size,
                "channel": channel,
                "date": date,
            }
    return (
        pins,
        release_by_id,
        fixture_projections,
        declared_objects,
    )


def _load_and_bind_manifests(
    fixture: Mapping[str, Any],
    pin_document: object,
    reader: ExactVersionReader,
) -> tuple[
    list[dict[str, str]],
    dict[tuple[str, str, str], dict[str, Any]],
]:
    (
        pins,
        release_by_id,
        fixture_projections,
        _declared_objects,
    ) = _preflight_manifest_controls(fixture, pin_document)

    physical_objects: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pin in pins:
        release = release_by_id[pin["release_id"]]
        raw = _read_exact_to_bytes(
            reader,
            bucket=pin["bucket"],
            key=pin["manifest_key"],
            version_id=pin["manifest_version_id"],
            expected_sha256=pin["manifest_raw_sha256"],
            label=f"manifest {pin['release_id']}",
        )
        manifest, manifest_objects = _parse_v3_manifest(raw, pin=pin)
        if _text(
            "fixture evidence_tier", release.get("evidence_tier")
        ) != _text("manifest evidence_tier", manifest.get("evidence_tier")):
            raise ProductionLineageError(
                f"fixture release {pin['release_id']} evidence tier drifted"
            )
        projected_manifest = {
            (
                item["logical_key"],
                item["version_id"],
                item["source_object_sha256"],
                item["size_bytes"],
                item["channel"],
                item["date"],
            )
            for item in manifest_objects.values()
        }
        if fixture_projections[pin["release_id"]] != projected_manifest:
            raise ProductionLineageError(
                f"fixture release {pin['release_id']} object set differs "
                "from its exact manifest"
            )
        for item in manifest_objects.values():
            key = (
                item["release_id"],
                item["logical_key"],
                item["version_id"],
            )
            physical_objects[key] = item
    return pins, physical_objects


def _record_date(kind: str, record: Mapping[str, Any]) -> str:
    if kind == "NORMALIZED_ROW":
        raw = _plain_int("decision_ts_ns", record.get("decision_ts_ns"))
        seconds = raw // 1_000_000_000
    elif kind in {"PUBLIC_TRADE", "L2_SNAPSHOT"}:
        field = (
            "timestamp_us"
            if kind == "PUBLIC_TRADE"
            else "receive_timestamp_us"
        )
        raw = _plain_int(field, record.get(field))
        seconds = raw // 1_000_000
    else:
        raw = _plain_int("observed_at_ns", record.get("observed_at_ns"))
        seconds = raw // 1_000_000_000
    try:
        return dt.datetime.fromtimestamp(
            seconds, tz=dt.timezone.utc
        ).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ProductionLineageError(
            f"{kind} timestamp is outside the UTC range"
        ) from exc


def _runtime_records(
    fixture: Mapping[str, Any],
) -> dict[tuple[str, str], tuple[Mapping[str, Any], str]]:
    result: dict[
        tuple[str, str], tuple[Mapping[str, Any], str]
    ] = {}
    for kind in sorted(RECORD_KINDS):
        collection = FIXTURE_COLLECTIONS[kind]
        id_field = ID_FIELDS[kind]
        for index, raw in enumerate(
            _list(f"fixture {collection}", fixture.get(collection))
        ):
            record = _mapping(f"fixture {collection}[{index}]", raw)
            record_id = _text(
                f"fixture {collection}[{index}].{id_field}",
                record.get(id_field),
            )
            key = (kind, record_id)
            if key in result:
                raise ProductionLineageError(
                    f"duplicate runtime record {kind}/{record_id}"
                )
            result[key] = (record, _record_date(kind, record))
    if not result:
        raise ProductionLineageError("fixture contains no runtime records")
    return result


def _evidence_bindings(
    fixture: Mapping[str, Any],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    required = {
        "kind",
        "record_id",
        "record_sha256",
        "release_id",
        "source_object_logical_key",
        "source_object_version_id",
        "source_object_sha256",
    }
    for index, raw in enumerate(
        _list("fixture evidence_bindings", fixture.get("evidence_bindings"))
    ):
        row = _mapping(f"fixture evidence_binding {index}", raw)
        _strict_keys(
            f"fixture evidence_binding {index}", row, required=required
        )
        key = (
            _text("evidence kind", row["kind"]),
            _text("evidence record_id", row["record_id"]),
        )
        if key in result:
            raise ProductionLineageError("duplicate fixture evidence binding")
        result[key] = row
    return result


def _validate_record_spec(
    fixture: Mapping[str, Any],
    value: object,
    exact_objects: (
        Mapping[tuple[str, str, str], Mapping[str, Any]] | None
    ),
) -> tuple[
    list[dict[str, Any]],
    dict[tuple[str, str], tuple[Mapping[str, Any], str]],
]:
    root = _mapping("record spec", value)
    _strict_keys(
        "record spec",
        root,
        required={"schema_version", "run_id", "records"},
    )
    schema_version = root["schema_version"]
    if schema_version not in {
        RECORD_SPEC_SCHEMA,
        LEGACY_RECORD_SPEC_SCHEMA,
    }:
        raise ProductionLineageError("unknown record-spec schema")
    if _text("record spec run_id", root["run_id"]) != _text(
        "fixture run_id", fixture.get("run_id")
    ):
        raise ProductionLineageError("record spec run_id differs from fixture")
    runtime = _runtime_records(fixture)
    bindings = _evidence_bindings(fixture)
    if set(bindings) != set(runtime):
        raise ProductionLineageError(
            "fixture evidence bindings are not exhaustive"
        )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    legacy_required = {
        "kind",
        "record_id",
        "mode",
        "release_id",
        "logical_key",
        "version_id",
        "locator",
        "transform",
    }
    current_required = {
        "kind",
        "record_id",
        "mode",
        "sources",
        "transform",
    }
    for index, raw in enumerate(_list("record spec.records", root["records"])):
        row = _mapping(f"record spec row {index}", raw)
        _strict_keys(
            f"record spec row {index}",
            row,
            required=(
                legacy_required
                if schema_version == LEGACY_RECORD_SPEC_SCHEMA
                else current_required
            ),
        )
        kind = _text(f"record spec row {index}.kind", row["kind"])
        record_id = _text(
            f"record spec row {index}.record_id", row["record_id"]
        )
        if kind not in RECORD_KINDS:
            raise ProductionLineageError("record spec has an unknown kind")
        key = (kind, record_id)
        if key in seen:
            raise ProductionLineageError("record spec contains a duplicate record")
        seen.add(key)
        if key not in runtime:
            raise ProductionLineageError("record spec contains an extra record")
        mode = _text(f"record spec row {index}.mode", row["mode"])
        if mode not in {"DIRECT", "DERIVED"}:
            raise ProductionLineageError("record spec mode is unknown")
        if kind == "NORMALIZED_ROW" and mode != "DERIVED":
            raise ProductionLineageError("NORMALIZED_ROW must be DERIVED")
        runtime_record, record_date = runtime[key]
        if schema_version == LEGACY_RECORD_SPEC_SCHEMA:
            raw_sources: list[object] = [
                {
                    "alias": "source",
                    "release_id": row["release_id"],
                    "logical_key": row["logical_key"],
                    "version_id": row["version_id"],
                    "locator": row["locator"],
                }
            ]
        else:
            raw_sources = _list(
                f"record spec row {index}.sources", row["sources"]
            )
        if not raw_sources:
            raise ProductionLineageError(
                f"record spec {kind}/{record_id} has no sources"
            )
        sources: list[dict[str, Any]] = []
        aliases: set[str] = set()
        source_member_keys: set[
            tuple[str, str, str, str, str]
        ] = set()
        source_allowed_channels = {
            "NORMALIZED_ROW": frozenset({"L1", "L2", "CATALOG"}),
            "PUBLIC_TRADE": frozenset({"TRADES"}),
            "L2_SNAPSHOT": frozenset({"L2"}),
            "SETTLEMENT": frozenset({"SETTLEMENT"}),
        }[kind]
        for source_index, raw_source in enumerate(raw_sources):
            source_row = _mapping(
                f"record spec row {index}.sources[{source_index}]",
                raw_source,
            )
            source_required = {
                "alias",
                "release_id",
                "logical_key",
                "version_id",
                "locator",
            }
            _strict_keys(
                f"record spec row {index}.sources[{source_index}]",
                source_row,
                required=source_required,
            )
            alias = _text(
                f"record spec row {index}.sources[{source_index}].alias",
                source_row["alias"],
            )
            if SOURCE_ALIAS_RE.fullmatch(alias) is None:
                raise ProductionLineageError(
                    "source alias is outside the fixed identifier grammar"
                )
            if alias in aliases:
                raise ProductionLineageError(
                    f"record spec {kind}/{record_id} has duplicate source alias"
                )
            aliases.add(alias)
            object_key = (
                _text(
                    (
                        f"record spec row {index}.sources"
                        f"[{source_index}].release_id"
                    ),
                    source_row["release_id"],
                ),
                _safe_key(
                    (
                        f"record spec row {index}.sources"
                        f"[{source_index}].logical_key"
                    ),
                    source_row["logical_key"],
                ),
                _version(
                    (
                        f"record spec row {index}.sources"
                        f"[{source_index}].version_id"
                    ),
                    source_row["version_id"],
                ),
            )
            locator = _mapping(
                (
                    f"record spec row {index}.sources"
                    f"[{source_index}].locator"
                ),
                source_row["locator"],
            )
            _validate_locator(locator)
            locator_text = canonical_json_bytes(locator).decode("utf-8")
            source_member_key = (
                object_key[0],
                object_key[1],
                object_key[2],
                _text("locator schema_version", locator["schema_version"]),
                locator_text,
            )
            if source_member_key in source_member_keys:
                raise ProductionLineageError(
                    f"record spec {kind}/{record_id} has a duplicate source "
                    "member"
                )
            source_member_keys.add(source_member_key)
            sources.append(
                {
                    "alias": alias,
                    "release_id": object_key[0],
                    "logical_key": object_key[1],
                    "version_id": object_key[2],
                    "locator": dict(locator),
                    "_member_sort_key": source_member_key,
                }
            )
            if exact_objects is not None:
                try:
                    source = exact_objects[object_key]
                except KeyError as exc:
                    raise ProductionLineageError(
                        "record spec object is absent from exact manifests"
                    ) from exc
                if source["date"] != record_date:
                    raise ProductionLineageError(
                        f"record spec {kind}/{record_id} escaped its UTC date"
                    )
                if source["channel"] not in source_allowed_channels:
                    raise ProductionLineageError(
                        f"record spec {kind}/{record_id} has a wrong-channel "
                        "source"
                    )
        source_order = [
            source["_member_sort_key"] for source in sources
        ]
        if source_order != sorted(source_order):
            raise ProductionLineageError(
                f"record spec {kind}/{record_id} sources must be sorted by "
                "object and locator"
            )
        if mode == "DIRECT" and len(sources) != 1:
            raise ProductionLineageError(
                "DIRECT record extraction requires exactly one source"
            )
        binding = bindings[key]
        binding_prefix = {
            "kind": kind,
            "record_id": record_id,
            "record_sha256": _canonical_hash(runtime_record, "runtime record"),
        }
        if any(binding.get(field) != expected for field, expected in (
            binding_prefix.items()
        )):
            raise ProductionLineageError(
                f"fixture evidence binding differs for {kind}/{record_id}"
            )
        binding_identity = (
            _text("evidence binding release_id", binding.get("release_id")),
            _safe_key(
                "evidence binding logical_key",
                binding.get("source_object_logical_key"),
            ),
            _version(
                "evidence binding version_id",
                binding.get("source_object_version_id"),
            ),
            _sha(
                "evidence binding source_object_sha256",
                binding.get("source_object_sha256"),
            ),
        )
        if exact_objects is not None:
            source_identities = {
                (
                    source["release_id"],
                    source["logical_key"],
                    source["version_id"],
                    exact_objects[
                        (
                            source["release_id"],
                            source["logical_key"],
                            source["version_id"],
                        )
                    ]["source_object_sha256"],
                )
                for source in sources
            }
            if binding_identity not in source_identities:
                raise ProductionLineageError(
                    f"fixture evidence binding matches no source for "
                    f"{kind}/{record_id}"
                )
        transform = _mapping(
            f"record spec row {index}.transform", row["transform"]
        )
        _validate_transform(
            mode,
            transform,
            source_aliases=aliases,
            record_spec_schema=schema_version,
        )
        rows.append(
            {
                "kind": kind,
                "record_id": record_id,
                "mode": mode,
                "sources": [
                    {
                        key: value
                        for key, value in source.items()
                        if key != "_member_sort_key"
                    }
                    for source in sources
                ],
                "transform": dict(transform),
            }
        )
    if seen != set(runtime):
        raise ProductionLineageError(
            "record spec is not exhaustive against runtime fixture"
        )
    order = [(row["kind"], row["record_id"]) for row in rows]
    if order != sorted(order):
        raise ProductionLineageError(
            "record spec rows must be sorted by kind/record_id"
        )
    return rows, runtime


def require_production_record_spec_v2(value: object) -> str | None:
    """Validate production schema and return its sole Parquet-runtime pin.

    ``parquet-key-v1`` deliberately remains available to offline library
    tests, but it can never cross the W09 CLI boundary.  Every v2 Parquet
    locator embeds the raw SHA of one externally reviewed runtime receipt.
    """

    root = _mapping("record spec", value)
    if root.get("schema_version") != RECORD_SPEC_SCHEMA:
        raise ProductionLineageError(
            "production CLI requires pnl-spine-record-extraction-spec-v2; "
            "legacy v1 is library-test compatibility only"
        )
    runtime_pins: set[str] = set()
    for record_index, raw_record in enumerate(
        _list("record spec.records", root.get("records"))
    ):
        record = _mapping(
            f"record spec row {record_index}", raw_record
        )
        for source_index, raw_source in enumerate(
            _list(
                f"record spec row {record_index}.sources",
                record.get("sources"),
            )
        ):
            source = _mapping(
                (
                    f"record spec row {record_index}.sources"
                    f"[{source_index}]"
                ),
                raw_source,
            )
            locator = _mapping(
                (
                    f"record spec row {record_index}.sources"
                    f"[{source_index}].locator"
                ),
                source.get("locator"),
            )
            schema = _text(
                "production locator schema_version",
                locator.get("schema_version"),
            )
            if schema == "parquet-key-v1":
                raise ProductionLineageError(
                    "production forbids unpinned parquet-key-v1; "
                    "use parquet-key-v2 with a runtime receipt"
                )
            _validate_locator(locator)
            if schema == "parquet-key-v2":
                runtime_pins.add(
                    _sha(
                        "parquet-key-v2 runtime_receipt_raw_sha256",
                        locator.get("runtime_receipt_raw_sha256"),
                    )
                )
    if len(runtime_pins) > 1:
        raise ProductionLineageError(
            "one production run cannot mix Parquet runtime receipts"
        )
    return next(iter(runtime_pins), None)


def require_production_hybrid_record_spec_v3(
    value: object,
) -> str | None:
    """Preflight production locators in the exact+terminal v3 contract."""

    root = _mapping("record spec", value)
    if root.get("schema_version") != HYBRID_RECORD_SPEC_SCHEMA:
        raise ProductionLineageError(
            "terminal root evidence requires "
            "pnl-spine-record-extraction-spec-v3"
        )
    runtime_pins: set[str] = set()
    for record_index, raw_record in enumerate(
        _list("record spec.records", root.get("records"))
    ):
        record = _mapping(
            f"record spec row {record_index}", raw_record
        )
        kind = _text(
            f"record spec row {record_index}.kind", record.get("kind")
        )
        for source_index, raw_source in enumerate(
            _list(
                f"record spec row {record_index}.sources",
                record.get("sources"),
            )
        ):
            source = _mapping(
                (
                    f"record spec row {record_index}.sources"
                    f"[{source_index}]"
                ),
                raw_source,
            )
            if "source_class" in source:
                if (
                    kind != "SETTLEMENT"
                    or source.get("source_class")
                    != "OFFICIAL_TERMINAL_CAPTURE"
                ):
                    raise ProductionLineageError(
                        "external source_class is valid only for "
                        "official SETTLEMENT"
                    )
                if "locator" in source:
                    raise ProductionLineageError(
                        "official terminal source forbids an object locator"
                    )
                continue
            locator = _mapping(
                (
                    f"record spec row {record_index}.sources"
                    f"[{source_index}].locator"
                ),
                source.get("locator"),
            )
            schema = _text(
                "production locator schema_version",
                locator.get("schema_version"),
            )
            if schema == "parquet-key-v1":
                raise ProductionLineageError(
                    "production forbids unpinned parquet-key-v1; "
                    "use parquet-key-v2 with a runtime receipt"
                )
            _validate_locator(locator)
            if schema == "parquet-key-v2":
                runtime_pins.add(
                    _sha(
                        "parquet-key-v2 runtime_receipt_raw_sha256",
                        locator.get("runtime_receipt_raw_sha256"),
                    )
                )
    if len(runtime_pins) > 1:
        raise ProductionLineageError(
            "one production run cannot mix Parquet runtime receipts"
        )
    return next(iter(runtime_pins), None)


def _validate_locator(locator: Mapping[str, Any]) -> None:
    schema = _text("locator schema_version", locator.get("schema_version"))
    if schema == "json-pointer-v1":
        _strict_keys(
            "json-pointer locator",
            locator,
            required={"schema_version", "pointer"},
        )
        pointer = locator["pointer"]
        if not isinstance(pointer, str) or (
            pointer and not pointer.startswith("/")
        ):
            raise ProductionLineageError("JSON pointer is malformed")
    elif schema == "ndjson-byte-range-v1":
        _strict_keys(
            "NDJSON locator",
            locator,
            required={
                "schema_version",
                "offset_bytes",
                "length_bytes",
                "line_sha256",
            },
        )
        _plain_int("NDJSON offset_bytes", locator["offset_bytes"])
        _plain_int(
            "NDJSON length_bytes", locator["length_bytes"], minimum=1
        )
        _sha("NDJSON line_sha256", locator["line_sha256"])
    elif schema in {"parquet-key-v1", "parquet-key-v2"}:
        required = {"schema_version", "match", "source_fields"}
        if schema == "parquet-key-v2":
            required.add("runtime_receipt_raw_sha256")
        _strict_keys(
            "Parquet locator",
            locator,
            required=required,
        )
        if schema == "parquet-key-v2":
            _sha(
                "Parquet runtime_receipt_raw_sha256",
                locator["runtime_receipt_raw_sha256"],
            )
        match = _mapping("Parquet locator.match", locator["match"])
        if not match:
            raise ProductionLineageError("Parquet match cannot be empty")
        fields = _list(
            "Parquet locator.source_fields", locator["source_fields"]
        )
        if not fields or not all(
            isinstance(field, str) and IDENTIFIER_RE.fullmatch(field)
            for field in fields
        ):
            raise ProductionLineageError(
                "Parquet source_fields must be safe identifiers"
            )
        if len(fields) != len(set(fields)):
            raise ProductionLineageError("Parquet source_fields are duplicated")
        for column, value in match.items():
            if IDENTIFIER_RE.fullmatch(column) is None:
                raise ProductionLineageError(
                    "Parquet match column is not a safe identifier"
                )
            _require_json_value(value, "Parquet match value")
    else:
        raise ProductionLineageError("unknown record locator schema")


def _validate_transform(
    mode: str,
    transform: Mapping[str, Any],
    *,
    source_aliases: set[str],
    record_spec_schema: object,
) -> None:
    schema = _text(
        "transform schema_version", transform.get("schema_version")
    )
    if mode == "DIRECT":
        _strict_keys(
            "DIRECT transform", transform, required={"schema_version"}
        )
        if schema != "identity-v1":
            raise ProductionLineageError("DIRECT requires identity-v1")
        return
    _strict_keys(
        "DERIVED transform",
        transform,
        required={"schema_version", "fields"},
    )
    expected_schema = (
        "field-map-v1"
        if record_spec_schema == LEGACY_RECORD_SPEC_SCHEMA
        else "field-map-v2"
    )
    if schema != expected_schema:
        raise ProductionLineageError(
            f"DERIVED requires {expected_schema}"
        )
    fields = _mapping("DERIVED transform.fields", transform["fields"])
    if not fields:
        raise ProductionLineageError("DERIVED field map cannot be empty")
    used_aliases: set[str] = set()
    for output_field, selector_raw in fields.items():
        if not output_field:
            raise ProductionLineageError("DERIVED output field is empty")
        selector = _mapping(
            f"DERIVED selector {output_field}", selector_raw
        )
        if (
            record_spec_schema == LEGACY_RECORD_SPEC_SCHEMA
            and set(selector) == {"source_path"}
        ):
            path = _list(
                f"DERIVED selector {output_field}.source_path",
                selector["source_path"],
            )
            alias = "source"
            used_aliases.add(alias)
            if alias not in source_aliases:
                raise ProductionLineageError(
                    "legacy transform lacks its implicit source"
                )
            if not path or not all(
                isinstance(part, (str, int))
                and not isinstance(part, bool)
                and (not isinstance(part, str) or bool(part))
                and (not isinstance(part, int) or part >= 0)
                for part in path
            ):
                raise ProductionLineageError(
                    "DERIVED source_path has invalid components"
                )
        elif (
            record_spec_schema == RECORD_SPEC_SCHEMA
            and set(selector) == {"source_alias", "source_path"}
        ):
            alias = _text(
                f"DERIVED selector {output_field}.source_alias",
                selector["source_alias"],
            )
            if alias not in source_aliases:
                raise ProductionLineageError(
                    "DERIVED selector names an unknown source alias"
                )
            used_aliases.add(alias)
            path = _list(
                f"DERIVED selector {output_field}.source_path",
                selector["source_path"],
            )
            if not path or not all(
                isinstance(part, (str, int))
                and not isinstance(part, bool)
                and (not isinstance(part, str) or bool(part))
                and (not isinstance(part, int) or part >= 0)
                for part in path
            ):
                raise ProductionLineageError(
                    "DERIVED source_path has invalid components"
                )
        elif set(selector) == {"literal"}:
            _require_json_value(
                selector["literal"],
                f"DERIVED selector {output_field}.literal",
            )
        else:
            raise ProductionLineageError(
                "DERIVED selector does not match its record-spec schema"
            )
    if used_aliases != source_aliases:
        missing = sorted(source_aliases - used_aliases)
        raise ProductionLineageError(
            "DERIVED transform does not consume every declared source alias: "
            + ",".join(missing)
        )


def _require_json_value(value: object, label: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, list):
        for item in value:
            _require_json_value(item, label)
        return
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        for item in value.values():
            _require_json_value(item, label)
        return
    raise ProductionLineageError(
        f"{label} must contain only fixed-point JSON values"
    )


def _normalize_source_value(value: object) -> object:
    """Normalize values returned by DuckDB without admitting binary floats."""

    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_normalize_source_value(item) for item in value]
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return {
            key: _normalize_source_value(item)
            for key, item in value.items()
        }
    raise ProductionLineageError(
        f"source contains unsupported {type(value).__name__}; "
        "binary floating point is forbidden"
    )


def _json_pointer(root: object, pointer: str) -> object:
    current = root
    if pointer == "":
        return current
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if part not in current:
                raise ProductionLineageError("JSON pointer member is absent")
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                raise ProductionLineageError("JSON pointer index is malformed")
            index = int(part)
            if index >= len(current):
                raise ProductionLineageError("JSON pointer index is out of range")
            current = current[index]
        else:
            raise ProductionLineageError("JSON pointer traverses a scalar")
    return current


def _extract_parquet(
    path: Path,
    locator: Mapping[str, Any],
    *,
    runtime: _VerifiedParquetRuntime | None,
) -> Mapping[str, Any]:
    schema = locator["schema_version"]
    if schema == "parquet-key-v2":
        if runtime is None:
            raise ProductionLineageError(
                "parquet-key-v2 requires a verified runtime receipt"
            )
        if (
            locator["runtime_receipt_raw_sha256"]
            != runtime.raw_sha256
        ):
            raise ProductionLineageError(
                "Parquet locator runtime receipt differs from live pin"
            )
    elif schema != "parquet-key-v1":
        raise ProductionLineageError("unknown Parquet locator schema")
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise ProductionLineageError(
            f"{schema} requires the DuckDB runtime"
        ) from exc
    fields = list(locator["source_fields"])
    match = _mapping("Parquet match", locator["match"])
    quoted_fields = ",".join(f'"{field}"' for field in fields)
    predicates = " AND ".join(
        f'"{field}" = ?' for field in sorted(match)
    )
    query = (
        f"SELECT {quoted_fields} FROM read_parquet(?) "
        f"WHERE {predicates} LIMIT 2"
    )
    connection, _effective = _connect_pinned_duckdb(duckdb)
    try:
        rows = connection.execute(
            query,
            [str(path)] + [match[field] for field in sorted(match)],
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ProductionLineageError(
            "Parquet locator did not select exactly one source row"
        )
    return {
        field: _normalize_source_value(value)
        for field, value in zip(fields, rows[0])
    }


def _extract_source_record(
    path: Path,
    locator: Mapping[str, Any],
    *,
    parquet_runtime: _VerifiedParquetRuntime | None,
) -> Mapping[str, Any]:
    schema = locator["schema_version"]
    if schema == "json-pointer-v1":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ProductionLineageError("verified JSON object vanished") from exc
        result = _json_pointer(
            _strict_json(raw, "verified JSON source"),
            locator["pointer"],
        )
    elif schema == "ndjson-byte-range-v1":
        try:
            with path.open("rb") as handle:
                handle.seek(locator["offset_bytes"])
                raw = handle.read(locator["length_bytes"])
        except OSError as exc:
            raise ProductionLineageError("verified NDJSON object vanished") from exc
        if len(raw) != locator["length_bytes"]:
            raise ProductionLineageError("NDJSON byte range is truncated")
        if hashlib.sha256(raw).hexdigest() != locator["line_sha256"]:
            raise ProductionLineageError("NDJSON line SHA-256 mismatch")
        stripped = raw.rstrip(b"\r\n")
        if not stripped or b"\n" in stripped or b"\r" in stripped:
            raise ProductionLineageError(
                "NDJSON byte range is not exactly one record"
            )
        result = _strict_json(stripped, "verified NDJSON record")
    elif schema in {"parquet-key-v1", "parquet-key-v2"}:
        result = _extract_parquet(
            path,
            locator,
            runtime=parquet_runtime,
        )
    else:
        raise ProductionLineageError("unknown record locator schema")
    normalized = _normalize_source_value(result)
    return _mapping("extracted source record", normalized)


def _apply_transform(
    sources: Mapping[str, Mapping[str, Any]],
    *,
    mode: str,
    transform: Mapping[str, Any],
) -> Mapping[str, Any]:
    if mode == "DIRECT":
        if len(sources) != 1:
            raise ProductionLineageError(
                "DIRECT transform received multiple sources"
            )
        return dict(next(iter(sources.values())))
    output: dict[str, Any] = {}
    for output_field, raw_selector in transform["fields"].items():
        selector = _mapping("transform selector", raw_selector)
        if "literal" in selector:
            value = selector["literal"]
        else:
            alias = selector.get("source_alias", "source")
            try:
                value: object = sources[alias]
            except KeyError as exc:
                raise ProductionLineageError(
                    "transform source alias is absent"
                ) from exc
            for part in selector["source_path"]:
                if isinstance(part, str) and isinstance(value, Mapping):
                    if part not in value:
                        raise ProductionLineageError(
                            "transform source_path member is absent"
                        )
                    value = value[part]
                elif (
                    isinstance(part, int)
                    and not isinstance(part, bool)
                    and isinstance(value, list)
                    and part < len(value)
                ):
                    value = value[part]
                else:
                    raise ProductionLineageError(
                        "transform source_path cannot be traversed"
                    )
        output[output_field] = _normalize_source_value(value)
    return output


def _materialize_verified_objects(
    reader: ExactVersionReader,
    descriptors: Sequence[Mapping[str, Any]],
    directory: Path,
) -> tuple[
    dict[tuple[str, str, str], Path],
    dict[tuple[str, str, str], tuple[str, int]],
]:
    """Hash every unique physical object once, then fan out its bindings."""

    expected: dict[
        tuple[str, str, str], tuple[str, int]
    ] = {}
    for descriptor in descriptors:
        identity = (
            descriptor["bucket"],
            descriptor["source_key"],
            descriptor["version_id"],
        )
        facts = (
            descriptor["source_object_sha256"],
            descriptor["size_bytes"],
        )
        previous = expected.setdefault(identity, facts)
        if previous != facts:
            raise ProductionLineageError(
                "one physical exact version has contradictory hash/size"
            )
    paths: dict[tuple[str, str, str], Path] = {}
    verified: dict[
        tuple[str, str, str], tuple[str, int]
    ] = {}
    for ordinal, identity in enumerate(sorted(expected)):
        expected_sha, expected_size = expected[identity]
        path = directory / f"object-{ordinal:06d}.verified"
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("xb") as handle:
                for chunk in reader.iter_exact_version(*identity):
                    if not isinstance(chunk, bytes):
                        raise ProductionLineageError(
                            "exact-version reader yielded non-bytes"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProductionLineageError(
                "cannot materialize verified exact object"
            ) from exc
        if total != expected_size:
            raise ProductionLineageError(
                "exact source object byte size mismatch"
            )
        actual_sha = digest.hexdigest()
        if actual_sha != expected_sha:
            raise ProductionLineageError(
                "exact source object SHA-256 mismatch"
            )
        path.chmod(0o400)
        paths[identity] = path
        verified[identity] = (actual_sha, total)
    return paths, verified


def produce_lineage_receipt(
    *,
    fixture: Mapping[str, Any],
    manifest_pins: Mapping[str, Any],
    record_spec: Mapping[str, Any],
    reader: ExactVersionReader,
    parquet_runtime: _VerifiedParquetRuntime | None = None,
    expected_extractor_code_sha256: str | None = None,
    require_root_read_only_code: bool = False,
) -> dict[str, Any]:
    """Verify exact versions and return ``pnl-spine-lineage-receipt-v1``."""

    # The exported API has the same fail-before-I/O contract as the CLI.
    # Validate the immutable code pin and every locally decidable fixture/spec
    # rule before constructing any authenticated S3 request.
    if require_root_read_only_code:
        _require_non_root_execution()
    if expected_extractor_code_sha256 is None:
        if require_root_read_only_code:
            raise ProductionLineageError(
                "root-read-only production mode requires an external "
                "extractor code pin"
            )
        extractor_code_sha256 = production_extractor_code_sha256()
    else:
        extractor_code_sha256 = validate_extractor_code_pin(
            expected_extractor_code_sha256,
            require_root_read_only=require_root_read_only_code,
        )
    extractor_config_sha256 = _canonical_hash(
        {
            "manifest_pins": manifest_pins,
            "record_spec": record_spec,
        },
        "extractor configuration",
    )
    (
        _preflight_pins,
        _preflight_releases,
        _preflight_projections,
        declared_objects,
    ) = _preflight_manifest_controls(fixture, manifest_pins)
    preflight_rows, _preflight_runtime = _validate_record_spec(
        fixture, record_spec, declared_objects
    )
    parquet_runtime_pins = {
        source["locator"]["runtime_receipt_raw_sha256"]
        for row in preflight_rows
        for source in row["sources"]
        if source["locator"]["schema_version"] == "parquet-key-v2"
    }
    if parquet_runtime_pins:
        if parquet_runtime is None:
            raise ProductionLineageError(
                "parquet-key-v2 requires a verified runtime receipt"
            )
        if parquet_runtime_pins != {parquet_runtime.raw_sha256}:
            raise ProductionLineageError(
                "record spec Parquet runtime pin differs from verified runtime"
            )

    _pins, exact_objects = _load_and_bind_manifests(
        fixture, manifest_pins, reader
    )
    spec_rows, runtime = _validate_record_spec(
        fixture, record_spec, exact_objects
    )
    used_logical = {
        (
            source["release_id"],
            source["logical_key"],
            source["version_id"],
        )
        for row in spec_rows
        for source in row["sources"]
    }
    descriptors = [exact_objects[key] for key in sorted(used_logical)]
    with tempfile.TemporaryDirectory(prefix="pnl-lineage-") as temporary:
        paths, verified = _materialize_verified_objects(
            reader, descriptors, Path(temporary)
        )
        object_reads: list[dict[str, Any]] = []
        for descriptor in descriptors:
            physical = (
                descriptor["bucket"],
                descriptor["source_key"],
                descriptor["version_id"],
            )
            actual_sha, actual_size = verified[physical]
            object_reads.append(
                {
                    "release_id": descriptor["release_id"],
                    "date": descriptor["date"],
                    "logical_key": descriptor["logical_key"],
                    "version_id": descriptor["version_id"],
                    "channel": descriptor["channel"],
                    "source_object_sha256": actual_sha,
                    "size_bytes": actual_size,
                    "bytes_verified": actual_size,
                }
            )
        object_reads.sort(
            key=lambda row: (
                row["release_id"],
                row["logical_key"],
                row["version_id"],
            )
        )

        records: list[dict[str, Any]] = []
        for spec in spec_rows:
            extracted_by_alias: dict[str, Mapping[str, Any]] = {}
            members: list[dict[str, Any]] = []
            for source in spec["sources"]:
                object_key = (
                    source["release_id"],
                    source["logical_key"],
                    source["version_id"],
                )
                descriptor = exact_objects[object_key]
                physical = (
                    descriptor["bucket"],
                    descriptor["source_key"],
                    descriptor["version_id"],
                )
                source_record = _extract_source_record(
                    paths[physical],
                    source["locator"],
                    parquet_runtime=parquet_runtime,
                )
                extracted_by_alias[source["alias"]] = source_record
                locator_text = canonical_json_bytes(
                    source["locator"]
                ).decode("utf-8")
                members.append(
                    {
                        "release_id": descriptor["release_id"],
                        "date": descriptor["date"],
                        "logical_key": descriptor["logical_key"],
                        "version_id": descriptor["version_id"],
                        "channel": descriptor["channel"],
                        "source_object_sha256": descriptor[
                            "source_object_sha256"
                        ],
                        "size_bytes": descriptor["size_bytes"],
                        "locator_schema": source["locator"][
                            "schema_version"
                        ],
                        "locator": locator_text,
                        "source_record_sha256": _canonical_hash(
                            source_record, "source record"
                        ),
                    }
                )
            produced = _apply_transform(
                extracted_by_alias,
                mode=spec["mode"],
                transform=spec["transform"],
            )
            runtime_record, record_date = runtime[
                (spec["kind"], spec["record_id"])
            ]
            if _canonical_hash(produced, "produced runtime record") != (
                _canonical_hash(runtime_record, "fixture runtime record")
            ):
                raise ProductionLineageError(
                    f"extracted {spec['kind']}/{spec['record_id']} differs "
                    "from the runtime fixture"
                )
            records.append(
                {
                    "kind": spec["kind"],
                    "record_id": spec["record_id"],
                    "record_sha256": _canonical_hash(
                        runtime_record, "runtime record"
                    ),
                    "record_date_utc": record_date,
                    "mode": spec["mode"],
                    "source_members": members,
                    "transform_code_sha256": extractor_code_sha256,
                    "transform_config_sha256": extractor_config_sha256,
                    "input_set_sha256": _canonical_hash(
                        members, "source member set"
                    ),
                }
            )
    records.sort(key=lambda row: (row["kind"], row["record_id"]))
    payload: dict[str, Any] = {
        "schema_version": LINEAGE_RECEIPT_SCHEMA,
        "run_id": _text("fixture run_id", fixture.get("run_id")),
        "release_set_sha256": _canonical_hash(
            _fixture_releases(fixture), "fixture release set"
        ),
        "extractor_code_sha256": extractor_code_sha256,
        "extractor_config_sha256": extractor_config_sha256,
        "record_schema_sha256": LINEAGE_RECORD_SCHEMA_SHA256,
        "object_reads": object_reads,
        "records": records,
        "records_sha256": _canonical_hash(records, "lineage records"),
    }
    payload["payload_sha256"] = _canonical_hash(
        payload, "lineage receipt payload"
    )
    # Force one final canonicalization before this object can cross the root
    # pin boundary.
    _canonical_hash(payload, "lineage receipt")
    return payload


def _validate_terminal_root_bundle_for_hybrid(
    bundle: _terminal_bridge.TerminalLineageBundle,
) -> tuple[
    Mapping[str, Any],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    """Revalidate the critical in-memory bridge facts before S3 I/O."""

    receipt = _mapping("terminal root receipt", bundle.receipt)
    required = {
        "schema_version",
        "temporality",
        "coverage_policy",
        "required_tickers",
        "required_tickers_sha256",
        "eligible_tickers",
        "eligible_tickers_sha256",
        "eligibility_exclusions",
        "eligibility_exclusions_sha256",
        "adapter_version_policy",
        "shards",
        "shards_sha256",
        "terminal_records_sha256",
        "terminal_record_index",
        "terminal_record_index_sha256",
        "metadata_evidence_sha256",
        "resolved_capabilities",
        "remaining_blockers",
        "historical_point_in_time_metadata_satisfied",
        "scheduled_start_authority_satisfied",
        "historical_lifecycle_intervals_satisfied",
        "network_reads_performed_by_bridge",
        "s3_writes",
        "financial_mutations",
        "payload_sha256",
    }
    _strict_keys("terminal root receipt", receipt, required=required)
    if receipt["schema_version"] != _terminal_bridge.ROOT_RECEIPT_SCHEMA:
        raise ProductionLineageError("unknown terminal root receipt schema")
    payload = dict(receipt)
    supplied_payload_sha = _sha(
        "terminal root payload SHA", payload.pop("payload_sha256")
    )
    if supplied_payload_sha != _canonical_hash(
        payload, "terminal root receipt payload"
    ):
        raise ProductionLineageError("terminal root receipt self hash mismatch")
    if (
        receipt["temporality"] != _terminal_bridge.TEMPORALITY
        or receipt["coverage_policy"] != _terminal_bridge.COVERAGE_POLICY
    ):
        raise ProductionLineageError(
            "terminal root receipt weakens temporality or exact coverage"
        )
    if any(
        receipt[field] is not False
        for field in (
            "historical_point_in_time_metadata_satisfied",
            "scheduled_start_authority_satisfied",
            "historical_lifecycle_intervals_satisfied",
        )
    ):
        raise ProductionLineageError(
            "terminal root receipt cannot satisfy historical metadata gates"
        )
    if receipt["remaining_blockers"] != list(
        _terminal_bridge.REQUIRED_REMAINING_BLOCKERS
    ):
        raise ProductionLineageError(
            "terminal root receipt removed a mandatory blocker"
        )
    if any(
        receipt[field] != 0
        for field in (
            "network_reads_performed_by_bridge",
            "s3_writes",
            "financial_mutations",
        )
    ):
        raise ProductionLineageError(
            "terminal root receipt is not an offline read-only bridge"
        )
    tickers = [
        _text(f"terminal required_tickers[{index}]", value)
        for index, value in enumerate(
            _list(
                "terminal root required_tickers",
                receipt["required_tickers"],
            )
        )
    ]
    if (
        not tickers
        or tickers != sorted(tickers)
        or len(tickers) != len(set(tickers))
    ):
        raise ProductionLineageError(
            "terminal root tickers must be nonempty, sorted and unique"
        )
    if _sha(
        "terminal required tickers SHA",
        receipt["required_tickers_sha256"],
    ) != _canonical_hash(tickers, "terminal required tickers"):
        raise ProductionLineageError("terminal required ticker hash mismatch")
    eligible_tickers = [
        _text(f"terminal eligible_tickers[{index}]", value)
        for index, value in enumerate(
            _list(
                "terminal root eligible_tickers",
                receipt["eligible_tickers"],
            )
        )
    ]
    if (
        not eligible_tickers
        or eligible_tickers != sorted(eligible_tickers)
        or len(eligible_tickers) != len(set(eligible_tickers))
        or _sha(
            "terminal eligible tickers SHA",
            receipt["eligible_tickers_sha256"],
        )
        != _canonical_hash(eligible_tickers, "terminal eligible tickers")
    ):
        raise ProductionLineageError(
            "terminal eligible ticker coverage is invalid"
        )
    exclusion_rows = _list(
        "terminal eligibility_exclusions",
        receipt["eligibility_exclusions"],
    )
    if _sha(
        "terminal eligibility exclusions SHA",
        receipt["eligibility_exclusions_sha256"],
    ) != _canonical_hash(
        exclusion_rows, "terminal eligibility exclusions"
    ):
        raise ProductionLineageError(
            "terminal eligibility exclusion hash mismatch"
        )
    exclusion_tickers: list[str] = []
    for index, raw in enumerate(exclusion_rows):
        exclusion = _mapping(
            f"terminal eligibility exclusion {index}", raw
        )
        ticker = _text(
            "terminal eligibility exclusion ticker",
            exclusion.get("ticker"),
        )
        if (
            exclusion.get("reason_code")
            != "NON_STANDARD_PRICE_LEVEL_STRUCTURE"
            or exclusion.get("observed_price_level_structure")
            != "tapered_deci_cent"
            or exclusion.get("economic_record_admitted") is not False
            or exclusion.get("temporality") != _terminal_bridge.TEMPORALITY
        ):
            raise ProductionLineageError(
                "terminal eligibility exclusion semantics drifted"
            )
        controls = _mapping(
            "terminal eligibility exclusion filesystem_controls",
            exclusion.get("filesystem_controls"),
        )
        for name in ("authority", "raw_response"):
            fact = _mapping(
                f"terminal eligibility exclusion {name}", controls.get(name)
            )
            if fact.get("root_read_only_verified") is not True:
                raise ProductionLineageError(
                    "terminal eligibility exclusion was not root/read-only "
                    "verified"
                )
        exclusion_tickers.append(ticker)
    if (
        exclusion_tickers != sorted(exclusion_tickers)
        or len(exclusion_tickers) != len(set(exclusion_tickers))
        or sorted(eligible_tickers + exclusion_tickers) != tickers
        or set(eligible_tickers) & set(exclusion_tickers)
    ):
        raise ProductionLineageError(
            "eligible plus excluded terminal tickers are not exact coverage"
        )
    shards = _list("terminal root shards", receipt["shards"])
    if not shards or _sha(
        "terminal shards SHA", receipt["shards_sha256"]
    ) != _canonical_hash(shards, "terminal root shards"):
        raise ProductionLineageError("terminal shard set hash mismatch")
    shard_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(shards):
        shard = _mapping(f"terminal shard {index}", raw)
        shard_id = _text("terminal shard_id", shard.get("shard_id"))
        if shard_id in shard_by_id:
            raise ProductionLineageError("duplicate terminal shard_id")
        controls = _mapping(
            "terminal shard filesystem_controls",
            shard.get("filesystem_controls"),
        )
        for name in (
            "authority",
            "capture_receipt",
            "raw_pins",
            "normalized_output",
        ):
            fact = _mapping(
                f"terminal shard filesystem_controls.{name}",
                controls.get(name),
            )
            if fact.get("root_read_only_verified") is not True:
                raise ProductionLineageError(
                    "terminal control was not root/read-only verified"
                )
        raw_responses = _list(
            "terminal shard raw_responses",
            shard.get("raw_responses"),
        )
        if not raw_responses:
            raise ProductionLineageError(
                "terminal shard contains no verified raw responses"
            )
        for response_index, response_raw in enumerate(raw_responses):
            response = _mapping(
                f"terminal raw response {response_index}", response_raw
            )
            filesystem = _mapping(
                "terminal raw response filesystem",
                response.get("filesystem"),
            )
            if filesystem.get("root_read_only_verified") is not True:
                raise ProductionLineageError(
                    "terminal raw response was not root/read-only verified"
                )
        shard_by_id[shard_id] = shard

    index_rows = _list(
        "terminal record index", receipt["terminal_record_index"]
    )
    if _sha(
        "terminal record index SHA",
        receipt["terminal_record_index_sha256"],
    ) != _canonical_hash(index_rows, "terminal record index"):
        raise ProductionLineageError("terminal record index hash mismatch")
    records_by_ticker = {
        _text("terminal bundle ticker", ticker): _mapping(
            f"terminal bundle record {ticker}", record
        )
        for ticker, record in bundle.terminal_records_by_ticker.items()
    }
    if sorted(records_by_ticker) != eligible_tickers:
        raise ProductionLineageError(
            "terminal bundle record coverage differs from root receipt"
        )
    index_by_ticker: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(index_rows):
        row = _mapping(f"terminal record index {index}", raw)
        required_index = {
            "ticker",
            "shard_id",
            "settlement_id",
            "record_sha256",
            "observed_at_ns",
            "selected_raw_response_sha256",
            "adapter_code_sha256",
            "adapter_config_sha256",
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "raw_pins_raw_sha256",
            "normalized_output_raw_sha256",
            "normalized_output_canonical_sha256",
        }
        _strict_keys(
            f"terminal record index {index}",
            row,
            required=required_index,
        )
        ticker = _text("terminal record index ticker", row["ticker"])
        if ticker in index_by_ticker:
            raise ProductionLineageError(
                "duplicate ticker in terminal record index"
            )
        try:
            record = records_by_ticker[ticker]
            shard = shard_by_id[_text("shard_id", row["shard_id"])]
        except KeyError as exc:
            raise ProductionLineageError(
                "terminal record index references an absent ticker or shard"
            ) from exc
        expected = {
            "settlement_id": record.get("settlement_id"),
            "record_sha256": _canonical_hash(
                record, "terminal normalized record"
            ),
            "observed_at_ns": record.get("observed_at_ns"),
            "selected_raw_response_sha256": record.get("source_sha256"),
            "adapter_code_sha256": shard.get("adapter_code_sha256"),
            "adapter_config_sha256": shard.get("adapter_config_sha256"),
            "authority_raw_sha256": shard.get("authority_raw_sha256"),
            "capture_receipt_raw_sha256": shard.get(
                "capture_receipt_raw_sha256"
            ),
            "raw_pins_raw_sha256": shard.get("raw_pins_raw_sha256"),
            "normalized_output_raw_sha256": shard.get(
                "normalized_output_raw_sha256"
            ),
            "normalized_output_canonical_sha256": shard.get(
                "normalized_output_canonical_sha256"
            ),
        }
        if any(row.get(field) != value for field, value in expected.items()):
            raise ProductionLineageError(
                f"terminal record index drifted for {ticker}"
            )
        index_by_ticker[ticker] = row
    if list(index_by_ticker) != eligible_tickers:
        raise ProductionLineageError(
            "terminal record index is not exact, sorted ticker coverage"
        )
    if _sha(
        "terminal records SHA", receipt["terminal_records_sha256"]
    ) != _canonical_hash(
        [records_by_ticker[ticker] for ticker in eligible_tickers],
        "terminal normalized records",
    ):
        raise ProductionLineageError(
            "terminal normalized record set hash mismatch"
        )
    return receipt, records_by_ticker, index_by_ticker


def _split_hybrid_inputs(
    *,
    fixture: Mapping[str, Any],
    record_spec: Mapping[str, Any],
    records_by_ticker: Mapping[str, Mapping[str, Any]],
    index_by_ticker: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
]:
    """Validate v3 and split its exact rows from external settlements."""

    root = _mapping("hybrid record spec", record_spec)
    _strict_keys(
        "hybrid record spec",
        root,
        required={"schema_version", "run_id", "records"},
    )
    if root["schema_version"] != HYBRID_RECORD_SPEC_SCHEMA:
        raise ProductionLineageError(
            "external terminal evidence requires record-extraction-spec-v3"
        )
    if _text("hybrid record spec run_id", root["run_id"]) != _text(
        "fixture run_id", fixture.get("run_id")
    ):
        raise ProductionLineageError(
            "hybrid record spec run_id differs from fixture"
        )
    runtime = _runtime_records(fixture)
    binding_rows = _list(
        "fixture evidence_bindings", fixture.get("evidence_bindings")
    )
    binding_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw in enumerate(binding_rows):
        binding = _mapping(f"fixture evidence_binding {index}", raw)
        key = (
            _text("evidence kind", binding.get("kind")),
            _text("evidence record_id", binding.get("record_id")),
        )
        if key in binding_by_key:
            raise ProductionLineageError(
                "duplicate fixture evidence binding"
            )
        binding_by_key[key] = binding
    if set(binding_by_key) != set(runtime):
        raise ProductionLineageError(
            "fixture evidence bindings are not exhaustive"
        )

    exact_spec_rows: list[dict[str, Any]] = []
    exact_binding_rows: list[dict[str, Any]] = []
    external_records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    spec_rows = _list("hybrid record spec.records", root["records"])
    for index, raw in enumerate(spec_rows):
        row = _mapping(f"hybrid record spec row {index}", raw)
        kind = _text("hybrid record kind", row.get("kind"))
        record_id = _text("hybrid record_id", row.get("record_id"))
        key = (kind, record_id)
        if key in seen:
            raise ProductionLineageError(
                "hybrid record spec contains a duplicate record"
            )
        seen.add(key)
        if key not in runtime:
            raise ProductionLineageError(
                "hybrid record spec contains an extra record"
            )
        if kind != "SETTLEMENT":
            exact_row = dict(row)
            _strict_keys(
                f"hybrid exact row {index}",
                exact_row,
                required={
                    "kind",
                    "record_id",
                    "mode",
                    "sources",
                    "transform",
                },
            )
            for source_raw in _list(
                f"hybrid exact row {index}.sources",
                exact_row["sources"],
            ):
                source = _mapping("hybrid exact source", source_raw)
                if "source_class" in source:
                    raise ProductionLineageError(
                        "external source_class is SETTLEMENT-only"
                    )
            exact_spec_rows.append(copy.deepcopy(exact_row))
            exact_binding_rows.append(
                copy.deepcopy(dict(binding_by_key[key]))
            )
            continue

        _strict_keys(
            f"hybrid settlement row {index}",
            row,
            required={
                "kind",
                "record_id",
                "mode",
                "sources",
                "transform",
            },
        )
        if row["mode"] != "DIRECT":
            raise ProductionLineageError(
                "external SETTLEMENT must use DIRECT mode"
            )
        transform = _mapping(
            "external settlement transform", row["transform"]
        )
        _strict_keys(
            "external settlement transform",
            transform,
            required={"schema_version"},
        )
        if transform["schema_version"] != "identity-v1":
            raise ProductionLineageError(
                "external SETTLEMENT requires identity-v1"
            )
        sources = _list(
            "external settlement sources", row["sources"]
        )
        if len(sources) != 1:
            raise ProductionLineageError(
                "external SETTLEMENT requires exactly one source"
            )
        source = _mapping("external settlement source", sources[0])
        _strict_keys(
            "external settlement source",
            source,
            required={"alias", "source_class", "ticker"},
        )
        alias = _text("external source alias", source["alias"])
        if SOURCE_ALIAS_RE.fullmatch(alias) is None:
            raise ProductionLineageError(
                "external source alias is malformed"
            )
        if source["source_class"] != "OFFICIAL_TERMINAL_CAPTURE":
            raise ProductionLineageError(
                "unknown external SETTLEMENT source class"
            )
        ticker = _text("external terminal ticker", source["ticker"])
        try:
            terminal_record = records_by_ticker[ticker]
            index_row = index_by_ticker[ticker]
        except KeyError as exc:
            raise ProductionLineageError(
                "external SETTLEMENT ticker is absent from terminal root"
            ) from exc
        runtime_record, record_date = runtime[key]
        if _canonical_hash(
            runtime_record, "fixture settlement"
        ) != _canonical_hash(terminal_record, "terminal settlement"):
            raise ProductionLineageError(
                f"fixture SETTLEMENT differs from terminal root: {ticker}"
            )
        binding = binding_by_key[key]
        external_binding_required = {
            "kind",
            "record_id",
            "record_sha256",
            "source_class",
            "market_ticker",
            "source_raw_response_sha256",
        }
        _strict_keys(
            "external settlement evidence binding",
            binding,
            required=external_binding_required,
        )
        if (
            binding["source_class"] != "OFFICIAL_TERMINAL_CAPTURE"
            or binding["market_ticker"] != ticker
            or binding["record_sha256"]
            != _canonical_hash(runtime_record, "runtime settlement")
            or binding["source_raw_response_sha256"]
            != terminal_record.get("source_sha256")
        ):
            raise ProductionLineageError(
                "external settlement evidence binding drifted"
            )
        external_records.append(
            {
                "kind": "SETTLEMENT",
                "record_id": record_id,
                "runtime_record": runtime_record,
                "record_date_utc": record_date,
                "ticker": ticker,
                "index": index_row,
            }
        )
    if seen != set(runtime):
        raise ProductionLineageError(
            "hybrid record spec is not exhaustive against runtime fixture"
        )
    order = [
        (_text("record kind", row.get("kind")), _text("record id", row.get("record_id")))
        for row in spec_rows
    ]
    if order != sorted(order):
        raise ProductionLineageError(
            "hybrid record spec rows must be sorted by kind/record_id"
        )
    settlement_tickers = sorted(
        row["ticker"] for row in external_records
    )
    if (
        settlement_tickers != sorted(records_by_ticker)
        or len(settlement_tickers) != len(set(settlement_tickers))
    ):
        raise ProductionLineageError(
            "runtime SETTLEMENT coverage is not exact against terminal root"
        )
    if not exact_spec_rows:
        raise ProductionLineageError(
            "hybrid lineage must retain exact-release L1/L2/trades evidence"
        )
    exact_fixture = copy.deepcopy(dict(fixture))
    exact_fixture["settlements"] = []
    exact_fixture["evidence_bindings"] = exact_binding_rows
    exact_spec = {
        "schema_version": RECORD_SPEC_SCHEMA,
        "run_id": root["run_id"],
        "records": exact_spec_rows,
    }
    return exact_fixture, exact_spec, external_records


def produce_hybrid_lineage_receipt(
    *,
    fixture: Mapping[str, Any],
    manifest_pins: Mapping[str, Any],
    record_spec: Mapping[str, Any],
    terminal_bundle: _terminal_bridge.TerminalLineageBundle,
    reader: ExactVersionReader,
    parquet_runtime: _VerifiedParquetRuntime | None = None,
    expected_extractor_code_sha256: str | None = None,
    require_root_read_only_code: bool = False,
) -> dict[str, Any]:
    """Produce v2 lineage with exact releases plus root terminal evidence."""

    (
        terminal_receipt,
        terminal_records,
        terminal_index,
    ) = _validate_terminal_root_bundle_for_hybrid(terminal_bundle)
    (
        exact_fixture,
        exact_record_spec,
        external_rows,
    ) = _split_hybrid_inputs(
        fixture=fixture,
        record_spec=record_spec,
        records_by_ticker=terminal_records,
        index_by_ticker=terminal_index,
    )
    base = produce_lineage_receipt(
        fixture=exact_fixture,
        manifest_pins=manifest_pins,
        record_spec=exact_record_spec,
        reader=reader,
        parquet_runtime=parquet_runtime,
        expected_extractor_code_sha256=expected_extractor_code_sha256,
        require_root_read_only_code=require_root_read_only_code,
    )
    extractor_code_sha256 = base["extractor_code_sha256"]
    extractor_config_sha256 = _canonical_hash(
        {
            "manifest_pins": manifest_pins,
            "record_spec": record_spec,
            "terminal_root_receipt": terminal_receipt,
        },
        "hybrid extractor configuration",
    )
    records = copy.deepcopy(base["records"])
    for row in records:
        row["transform_config_sha256"] = extractor_config_sha256
    terminal_root_sha = _canonical_hash(
        terminal_receipt, "terminal root receipt"
    )
    for row in external_rows:
        index = row["index"]
        member = {
            "source_class": "OFFICIAL_TERMINAL_CAPTURE",
            "shard_id": index["shard_id"],
            "ticker": row["ticker"],
            "channel": "SETTLEMENT",
            "temporality": _terminal_bridge.TEMPORALITY,
            "adapter_code_sha256": index["adapter_code_sha256"],
            "adapter_config_sha256": index["adapter_config_sha256"],
            "authority_raw_sha256": index["authority_raw_sha256"],
            "capture_receipt_raw_sha256": index[
                "capture_receipt_raw_sha256"
            ],
            "raw_pins_raw_sha256": index["raw_pins_raw_sha256"],
            "normalized_output_raw_sha256": index[
                "normalized_output_raw_sha256"
            ],
            "normalized_output_canonical_sha256": index[
                "normalized_output_canonical_sha256"
            ],
            "selected_raw_response_sha256": index[
                "selected_raw_response_sha256"
            ],
            "source_record_sha256": index["record_sha256"],
            "terminal_root_receipt_sha256": terminal_root_sha,
        }
        members = [member]
        runtime_record = row["runtime_record"]
        records.append(
            {
                "kind": "SETTLEMENT",
                "record_id": row["record_id"],
                "record_sha256": _canonical_hash(
                    runtime_record, "runtime settlement"
                ),
                "record_date_utc": row["record_date_utc"],
                "mode": "DIRECT",
                "source_members": members,
                "transform_code_sha256": extractor_code_sha256,
                "transform_config_sha256": extractor_config_sha256,
                "input_set_sha256": _canonical_hash(
                    members, "terminal source member set"
                ),
            }
        )
    records.sort(key=lambda row: (row["kind"], row["record_id"]))
    payload: dict[str, Any] = {
        "schema_version": HYBRID_LINEAGE_RECEIPT_SCHEMA,
        "run_id": _text("fixture run_id", fixture.get("run_id")),
        "release_set_sha256": _canonical_hash(
            _fixture_releases(fixture), "fixture release set"
        ),
        "extractor_code_sha256": extractor_code_sha256,
        "extractor_config_sha256": extractor_config_sha256,
        "record_schema_sha256": HYBRID_LINEAGE_RECORD_SCHEMA_SHA256,
        "object_reads": base["object_reads"],
        "external_terminal_evidence": terminal_receipt,
        "records": records,
        "records_sha256": _canonical_hash(records, "hybrid lineage records"),
    }
    payload["payload_sha256"] = _canonical_hash(
        payload, "hybrid lineage receipt payload"
    )
    _canonical_hash(payload, "hybrid lineage receipt")
    return payload


def write_receipt_create_once(
    path: Path,
    payload: Mapping[str, Any],
) -> str:
    """Atomically create a receipt without replacing any existing directory entry."""

    raw = canonical_json_bytes(dict(payload)) + b"\n"
    absolute = Path(os.path.abspath(os.fspath(path)))
    parent = _absolute_path_without_symlinks(absolute.parent)
    try:
        parent_info = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise ProductionLineageError(
            "receipt output parent is unavailable"
        ) from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ProductionLineageError(
            "receipt output parent must be an existing directory"
        )
    name = absolute.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ProductionLineageError("receipt output name is unsafe")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(parent, directory_flags)
    except OSError as exc:
        raise ProductionLineageError(
            "receipt output parent cannot be opened safely"
        ) from exc
    temporary_name = (
        f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
    )
    temporary_fd: int | None = None
    temporary_created = False
    try:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        temporary_fd = os.open(
            temporary_name,
            file_flags,
            0o400,
            dir_fd=directory_fd,
        )
        temporary_created = True
        view = memoryview(raw)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise ProductionLineageError(
                    "receipt output write made no progress"
                )
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ProductionLineageError(
                "receipt output already exists; create-once refuses overwrite"
            ) from exc
        except OSError as exc:
            raise ProductionLineageError(
                "receipt output cannot be linked create-once"
            ) from exc
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    return hashlib.sha256(raw).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="W09 exact-version production lineage producer"
    )
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--fixture-sha256", required=True)
    parser.add_argument("--manifest-pins", required=True)
    parser.add_argument("--manifest-pins-sha256", required=True)
    parser.add_argument("--record-spec", required=True)
    parser.add_argument("--record-spec-sha256", required=True)
    parser.add_argument("--terminal-root-spec")
    parser.add_argument("--terminal-root-spec-sha256")
    parser.add_argument(
        "--expected-extractor-code-sha256",
        required=True,
        help=(
            "reviewed canonical producer code-bundle SHA-256 "
            "(not the raw source-file SHA)"
        ),
    )
    parser.add_argument("--parquet-runtime-receipt")
    parser.add_argument("--parquet-runtime-receipt-sha256")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--region",
        choices=(W09_REGION,),
        default=W09_REGION,
    )
    return parser


def _runtime_receipt_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure an isolated, root-read-only Python/DuckDB runtime"
    )
    parser.add_argument(
        "--expected-extractor-code-sha256",
        required=True,
    )
    parser.add_argument("--output", required=True)
    return parser


def _write_runtime_receipt(argv: Sequence[str]) -> int:
    args = _runtime_receipt_parser().parse_args(argv)
    _require_non_root_execution()
    validate_extractor_code_pin(
        args.expected_extractor_code_sha256,
        require_root_read_only=True,
    )
    receipt = build_parquet_runtime_receipt(
        require_root_read_only=True
    )
    raw_sha256 = write_receipt_create_once(Path(args.output), receipt)
    os.write(
        1,
        canonical_json_bytes(
            {
                "schema_version": (
                    "pnl-spine-parquet-runtime-production-summary-v1"
                ),
                "output": args.output,
                "receipt_canonical_sha256": canonical_sha256(receipt),
                "receipt_raw_sha256": raw_sha256,
            }
        )
        + b"\n",
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "runtime-receipt":
        return _write_runtime_receipt(arguments[1:])
    args = _parser().parse_args(arguments)
    _require_non_root_execution()
    expected_code_sha256 = _sha(
        "expected extractor code SHA-256",
        args.expected_extractor_code_sha256,
    )
    # This first check is deliberately before any control-plane or S3 read.
    # ``produce_lineage_receipt`` repeats it immediately before stamping the
    # receipt.  Root-owned read-only deployment closes the intervening swap
    # boundary for the unprivileged W09 service account.
    validate_extractor_code_pin(
        expected_code_sha256,
        require_root_read_only=True,
    )
    fixture = _read_control_file(
        Path(args.fixture),
        expected_raw_sha256=args.fixture_sha256,
        label="runtime fixture",
    )
    manifest_pins = _read_control_file(
        Path(args.manifest_pins),
        expected_raw_sha256=args.manifest_pins_sha256,
        label="manifest pins",
    )
    record_spec = _read_control_file(
        Path(args.record_spec),
        expected_raw_sha256=args.record_spec_sha256,
        label="record spec",
    )
    terminal_spec_path = args.terminal_root_spec
    terminal_spec_sha256 = args.terminal_root_spec_sha256
    if (terminal_spec_path is None) != (terminal_spec_sha256 is None):
        raise ProductionLineageError(
            "terminal root spec path and raw SHA must be supplied together"
        )
    terminal_bundle: _terminal_bridge.TerminalLineageBundle | None = None
    if terminal_spec_path is None:
        parquet_runtime_pin = require_production_record_spec_v2(record_spec)
    else:
        parquet_runtime_pin = require_production_hybrid_record_spec_v3(
            record_spec
        )
        terminal_spec = _terminal_bridge.load_pinned_terminal_root_spec(
            Path(terminal_spec_path),
            expected_raw_sha256=_sha(
                "terminal root spec raw SHA-256",
                terminal_spec_sha256,
            ),
            require_root_read_only=True,
        )
        try:
            terminal_bundle = _terminal_bridge.build_terminal_root_bundle(
                terminal_spec,
                require_root_read_only=True,
            )
        except _terminal_bridge.TerminalLineageBridgeError as exc:
            raise ProductionLineageError(
                f"terminal root evidence refused: {exc}"
            ) from exc
    parquet_runtime: _VerifiedParquetRuntime | None = None
    runtime_path = args.parquet_runtime_receipt
    runtime_sha256 = args.parquet_runtime_receipt_sha256
    if parquet_runtime_pin is None:
        if runtime_path is not None or runtime_sha256 is not None:
            raise ProductionLineageError(
                "unused Parquet runtime receipt arguments are forbidden"
            )
    else:
        if runtime_path is None or runtime_sha256 is None:
            raise ProductionLineageError(
                "parquet-key-v2 requires runtime receipt path and raw SHA"
            )
        supplied_runtime_pin = _sha(
            "Parquet runtime receipt raw SHA-256", runtime_sha256
        )
        if supplied_runtime_pin != parquet_runtime_pin:
            raise ProductionLineageError(
                "CLI Parquet runtime pin differs from record spec"
            )
        runtime_receipt = _read_control_file(
            Path(runtime_path),
            expected_raw_sha256=supplied_runtime_pin,
            label="Parquet runtime receipt",
        )
        parquet_runtime = validate_parquet_runtime_receipt(
            runtime_receipt,
            raw_sha256=supplied_runtime_pin,
            require_root_read_only=True,
        )
    reader = W09ExactVersionS3Reader(region=args.region)
    if terminal_bundle is None:
        receipt = produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=manifest_pins,
            record_spec=record_spec,
            reader=reader,
            parquet_runtime=parquet_runtime,
            expected_extractor_code_sha256=expected_code_sha256,
            require_root_read_only_code=True,
        )
    else:
        receipt = produce_hybrid_lineage_receipt(
            fixture=fixture,
            manifest_pins=manifest_pins,
            record_spec=record_spec,
            terminal_bundle=terminal_bundle,
            reader=reader,
            parquet_runtime=parquet_runtime,
            expected_extractor_code_sha256=expected_code_sha256,
            require_root_read_only_code=True,
        )
    output = Path(args.output)
    raw_sha256 = write_receipt_create_once(output, receipt)
    summary = {
        "schema_version": "pnl-spine-lineage-production-summary-v1",
        "lineage_receipt_canonical_sha256": canonical_sha256(receipt),
        "lineage_receipt_raw_sha256": raw_sha256,
        "output": str(output),
        "records": len(receipt["records"]),
        "verified_logical_objects": len(receipt["object_reads"]),
        "external_terminal_tickers": len(
            receipt.get("external_terminal_evidence", {}).get(
                "required_tickers", []
            )
        ),
    }
    os.write(1, canonical_json_bytes(summary) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExactVersionReader",
    "MANIFEST_PINS_SCHEMA",
    "HYBRID_LINEAGE_RECEIPT_SCHEMA",
    "HYBRID_LINEAGE_RECORD_SCHEMA_SHA256",
    "HYBRID_RECORD_SPEC_SCHEMA",
    "PARQUET_RUNTIME_RECEIPT_SCHEMA",
    "ProductionLineageError",
    "RECORD_SPEC_SCHEMA",
    "W09_INSTANCE_PROFILE_ARN",
    "W09_REGION",
    "W09ExactVersionS3Reader",
    "build_parquet_runtime_receipt",
    "produce_lineage_receipt",
    "produce_hybrid_lineage_receipt",
    "production_extractor_code_sha256",
    "refuse_non_imds_credentials",
    "require_production_record_spec_v2",
    "require_production_hybrid_record_spec_v3",
    "validate_extractor_code_pin",
    "validate_parquet_runtime_receipt",
    "write_receipt_create_once",
]
