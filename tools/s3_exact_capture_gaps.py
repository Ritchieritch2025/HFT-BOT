#!/usr/bin/env python3
"""Rebuild one capture-gap receipt from seal-bound exact S3 versions.

This is the zero-copy historical companion to :mod:`capture_gaps`.  It reads
only same-date ``firehose_*`` objects named by a verified ``full_v2`` seal:

1. versionless ``HeadObject`` resolves the current non-null VersionId and
   checks the seal-bound byte size;
2. ``GetObject`` is then issued for that exact VersionId;
3. the response VersionId/length and the complete body size/SHA-256 are
   checked before any local output is published.

The AWS CLI writes each GET body into a private inherited pipe.  Object bytes
are never written to disk.  Timestamps retain ``capture_gaps.scan_date``'s
global-sort semantics.  Their sort has an explicit working-set budget; only
derived int64 timestamp runs may spill to a private temporary directory when
the requested budget is too small for an in-memory global sort.

RFQ, L2, cross-date raw, list/latest fallback after HEAD, repair, copy, S3
write, tag, and delete operations are deliberately absent.  Any seal, HEAD,
GET, body, checksum, UTF-8, line, or timestamp anomaly fails closed before a
receipt is written.
"""

from __future__ import annotations

import argparse
from array import array
import contextlib
import csv
import datetime as dt
import hashlib
import heapq
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Iterator, Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture_gaps as cg  # noqa: E402


SOURCE_BUCKET = "kalshi-vault-ritcardo"
SOURCE_PREFIX = "ec2"
RECEIPT_SCHEMA = "capture-gap-scan-receipt-v1"
SOURCE_ATTESTATION_SCHEMA = "exact-version-s3-capture-scan-v1"
SEAL_METHOD = "full_v2"
MIN_GAP_US = 60 * 1_000_000
READ_CHUNK_BYTES = 1 << 20
MAX_LINE_BYTES = 1 << 20
DEFAULT_SORT_MEMORY_MIB = 3072
MIN_SORT_MEMORY_MIB = 64
MAX_SORT_MEMORY_MIB = 16384
SORT_FIXED_BYTES = 1 << 20
# Conservative transient cost while ``sorted(array('q'))`` and the source
# array coexist, plus the temporary array used to serialize a spill run.
SORT_BYTES_PER_TIMESTAMP = 56
RUN_READ_RECORDS = 8192
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIREHOSE_BASENAME_RE = re.compile(
    r"^firehose_[0-9]{2}\.ndjson(?:\.[0-9]+)?$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class ExactCaptureError(RuntimeError):
    """Stable fail-closed error whose detail never contains credentials."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise ExactCaptureError(code, detail)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", type(exc).__name__)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_date(value: Any) -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("DATE_INVALID", "date must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        _fail("DATE_INVALID", "date is not a real calendar date")
    if parsed.isoformat() != value:
        _fail("DATE_INVALID", "date is not canonical YYYY-MM-DD")
    return value


def _regular_file_bytes(path: str | os.PathLike[str], label: str,
                        max_bytes: int) -> bytes:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        _fail("LOCAL_INPUT_MISSING", f"{label}: {type(exc).__name__}")
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink < 1:
        _fail("LOCAL_INPUT_INVALID", f"{label} is not a regular file")
    if observed.st_size <= 0 or observed.st_size > max_bytes:
        _fail("LOCAL_INPUT_INVALID", f"{label} has invalid size")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) !=
                    (observed.st_dev, observed.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_size != observed.st_size):
                _fail("LOCAL_INPUT_CHANGED", f"{label} changed before read")
            chunks = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    _fail("LOCAL_INPUT_CHANGED", f"{label} truncated during read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                _fail("LOCAL_INPUT_CHANGED", f"{label} grew during read")
            after = os.fstat(fd)
        finally:
            os.close(fd)
    except ExactCaptureError:
        raise
    except OSError as exc:
        _fail("LOCAL_INPUT_READ_FAILED", f"{label}: {type(exc).__name__}")
    if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
         after.st_ctime_ns) !=
            (opened.st_dev, opened.st_ino, opened.st_size,
             opened.st_mtime_ns, opened.st_ctime_ns)):
        _fail("LOCAL_INPUT_CHANGED", f"{label} changed during read")
    return b"".join(chunks)


def _strict_json(payload: bytes, label: str) -> Any:
    def pairs(pairs_value):
        out = {}
        for key, value in pairs_value:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def bad_constant(_value):
        raise ValueError("non-finite value")

    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=bad_constant)
    except (UnicodeDecodeError, ValueError):
        _fail("SEAL_INVALID_JSON", f"{label} is not strict JSON")


def _safe_sealed_rel(value: Any, date: str) -> tuple[str, str] | None:
    """Return (rel, basename) only for the exact same-date firehose grammar."""
    if not isinstance(value, str) or "\\" in value or value.startswith("/"):
        return None
    parts = value.split("/")
    if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0] != f"date={date}":
        return None
    if FIREHOSE_BASENAME_RE.fullmatch(parts[1]) is None:
        return None
    return value, parts[1]


def _project_sealed_firehose(
    seal: Mapping[str, Any], date: str,
) -> list[dict[str, Any]]:
    raw_files = seal.get("raw_files")
    if not isinstance(raw_files, list):
        _fail("SEAL_INVALID", "raw_files is not a list")
    selected = []
    seen = set()
    for index, row in enumerate(raw_files):
        if not isinstance(row, dict):
            _fail("SEAL_INVALID", f"raw_files[{index}] is not an object")
        projected = _safe_sealed_rel(row.get("file"), date)
        if projected is None:
            # Cross-date, L2, RFQ, and unrelated raw are hard excluded.
            continue
        rel, _basename = projected
        if rel in seen:
            _fail("SEAL_INVALID", f"duplicate sealed firehose path {rel}")
        size = row.get("size")
        digest = row.get("sha256")
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            _fail("SEAL_INVALID", f"invalid sealed size for {rel}")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            _fail("SEAL_INVALID", f"invalid sealed SHA-256 for {rel}")
        seen.add(rel)
        selected.append({"file": rel, "size": size, "sha256": digest})
    selected.sort(key=lambda row: row["file"])
    if not selected:
        _fail("SEALED_FIREHOSE_EMPTY", "seal names no same-date firehose object")
    return selected


def load_sealed_firehose(
    seal_path: str | os.PathLike[str], date: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Load one full_v2 seal and project its exact same-date firehose set."""
    date = _valid_date(date)
    payload = _regular_file_bytes(seal_path, "seal", 16 << 20)
    seal = _strict_json(payload, "seal")
    if not isinstance(seal, dict):
        _fail("SEAL_INVALID", "seal is not an object")
    if (seal.get("date") != date or seal.get("status") != "SEALED"
            or seal.get("version") != 2
            or seal.get("method") != SEAL_METHOD):
        _fail("SEAL_NOT_VERIFIED", "date/status/version/method gate failed")
    selected = _project_sealed_firehose(seal, date)
    return seal, hashlib.sha256(payload).hexdigest(), selected


def _version(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or value.lower() == "null"
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        _fail("VERSIONING_REQUIRED", f"{label} has no non-null VersionId")
    return value


def _content_length(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail("S3_METADATA_INVALID", f"{label} has invalid ContentLength")
    return value


def _aws_error_code(stderr: str) -> str:
    match = re.search(r"An error occurred \(([A-Za-z0-9_.-]{1,128})\)", stderr)
    return match.group(1) if match else "UNCLASSIFIED"


class AwsCliExactGetStream:
    """One exact GET body delivered through an inherited pipe, never a file."""

    def __init__(self, executable: str, env: Mapping[str, str], bucket: str,
                 key: str, version_id: str, expected_size: int,
                 timeout: float):
        self.executable = executable
        self.env = dict(env)
        self.bucket = bucket
        self.key = key
        self.version_id = version_id
        self.expected_size = expected_size
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.body: BinaryIO | None = None
        self.observed = 0
        self.saw_eof = False
        self.metadata: dict[str, Any] | None = None

    def __enter__(self) -> "AwsCliExactGetStream":
        try:
            read_fd, write_fd = os.pipe()
        except OSError as exc:
            _fail("LOCAL_PIPE_FAILED", type(exc).__name__)
        command = [
            self.executable, "s3api", "get-object",
            "--bucket", self.bucket,
            "--key", self.key,
            "--version-id", self.version_id,
            "--checksum-mode", "ENABLED",
            f"/dev/fd/{write_fd}",
            "--output", "json",
            "--no-cli-pager",
        ]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.env, close_fds=True,
                pass_fds=(write_fd,))
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(read_fd)
            os.close(write_fd)
            _fail("S3_GET_START_FAILED", type(exc).__name__)
        os.close(write_fd)
        self.body = os.fdopen(read_fd, "rb", buffering=0)
        return self

    def read(self, size: int = READ_CHUNK_BYTES) -> bytes:
        if self.body is None or self.process is None:
            _fail("S3_GET_STATE_INVALID", "GET stream is not open")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            _fail("S3_GET_STATE_INVALID", "read size must be positive")
        try:
            chunk = self.body.read(size)
        except OSError as exc:
            _fail("S3_GET_BODY_FAILED", type(exc).__name__)
        if not isinstance(chunk, bytes):
            _fail("S3_GET_BODY_INVALID", "GET stream yielded non-bytes")
        if not chunk:
            self.saw_eof = True
            return b""
        self.observed += len(chunk)
        if self.observed > self.expected_size:
            _fail("S3_GET_BODY_OVERSIZED", f"GET body exceeds HEAD for {self.key}")
        return chunk

    def _stop(self) -> None:
        if self.body is not None:
            with contextlib.suppress(OSError):
                self.body.close()
            self.body = None
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    self.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=5)

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is not None:
            self._stop()
            return False
        if not self.saw_eof:
            self._stop()
            _fail("S3_GET_BODY_NOT_DRAINED", f"GET body not drained for {self.key}")
        if self.body is not None:
            self.body.close()
            self.body = None
        assert self.process is not None
        try:
            stdout, stderr = self.process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._stop()
            _fail("S3_GET_TIMEOUT", f"exact GET timed out for {self.key}")
        if self.process.returncode != 0:
            code = _aws_error_code(stderr.decode("utf-8", "replace"))
            _fail("S3_GET_FAILED", f"AWS CLI rc={self.process.returncode} code={code}")
        try:
            response = json.loads(stdout.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            _fail("S3_RESPONSE_INVALID", f"GET metadata invalid for {self.key}")
        if not isinstance(response, dict):
            _fail("S3_RESPONSE_INVALID", f"GET metadata is not an object for {self.key}")
        version_id = _version(response.get("VersionId"), "GET response")
        length = _content_length(response.get("ContentLength"), "GET response")
        if version_id != self.version_id:
            _fail("S3_GET_VERSION_MISMATCH", f"GET VersionId differs for {self.key}")
        if length != self.expected_size or self.observed != self.expected_size:
            _fail("S3_GET_SIZE_MISMATCH", f"GET size differs for {self.key}")
        self.metadata = {"VersionId": version_id, "ContentLength": length}
        return False


class AwsCliExactS3Client:
    """Read-only HEAD + exact-GET adapter using inherited AWS credentials."""

    def __init__(self, executable: str = "aws", timeout: float = 3600):
        if not isinstance(executable, str) or not executable:
            _fail("AWS_CLI_INVALID", "AWS CLI executable is empty")
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or timeout <= 0):
            _fail("AWS_CLI_INVALID", "timeout must be positive")
        self.executable = executable
        self.timeout = float(timeout)
        self.env = dict(os.environ)
        self.env["AWS_PAGER"] = ""
        self.env["AWS_CLI_PAGER"] = ""

    def head_current(self, bucket: str, key: str) -> dict[str, Any]:
        command = [
            self.executable, "s3api", "head-object",
            "--bucket", bucket, "--key", key,
            "--checksum-mode", "ENABLED",
            "--output", "json", "--no-cli-pager",
        ]
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True,
                env=self.env, timeout=min(self.timeout, 300), check=False)
        except subprocess.TimeoutExpired:
            _fail("S3_HEAD_TIMEOUT", f"HEAD timed out for {key}")
        except (OSError, subprocess.SubprocessError) as exc:
            _fail("S3_HEAD_FAILED", f"HEAD failed for {key}: {type(exc).__name__}")
        if result.returncode != 0:
            code = _aws_error_code(result.stderr.decode("utf-8", "replace"))
            _fail("S3_HEAD_FAILED", f"AWS CLI rc={result.returncode} code={code}")
        try:
            response = json.loads(result.stdout.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            _fail("S3_RESPONSE_INVALID", f"HEAD metadata invalid for {key}")
        if not isinstance(response, dict):
            _fail("S3_RESPONSE_INVALID", f"HEAD metadata is not an object for {key}")
        return {
            "VersionId": _version(response.get("VersionId"), "HEAD response"),
            "ContentLength": _content_length(
                response.get("ContentLength"), "HEAD response"),
        }

    def open_exact(self, bucket: str, key: str, version_id: str,
                   expected_size: int) -> AwsCliExactGetStream:
        return AwsCliExactGetStream(
            self.executable, self.env, bucket, key,
            _version(version_id, "exact GET request"), expected_size,
            self.timeout)


class TimestampSorter:
    """Global int64 sorter with a conservative explicit working-set budget."""

    def __init__(self, memory_limit_bytes: int,
                 scratch_parent: str | os.PathLike[str] | None = None):
        if (not isinstance(memory_limit_bytes, int)
                or isinstance(memory_limit_bytes, bool)
                or memory_limit_bytes <= SORT_FIXED_BYTES):
            _fail("MEMORY_LIMIT_INVALID", "timestamp sort budget is too small")
        self.memory_limit_bytes = memory_limit_bytes
        self.capacity = max(
            1, (memory_limit_bytes - SORT_FIXED_BYTES)
            // SORT_BYTES_PER_TIMESTAMP)
        self.scratch_parent = (None if scratch_parent is None
                               else os.fspath(scratch_parent))
        self.values = array("q")
        self.runs: list[Path] = []
        self.root: Path | None = None
        self.count = 0
        self.spilled_bytes = 0

    def __enter__(self) -> "TimestampSorter":
        return self

    def _ensure_root(self) -> Path:
        if self.root is None:
            try:
                self.root = Path(tempfile.mkdtemp(
                    prefix="exact-capture-ts-", dir=self.scratch_parent))
                os.chmod(self.root, 0o700)
            except OSError as exc:
                _fail("TIMESTAMP_SCRATCH_FAILED", type(exc).__name__)
        return self.root

    def add(self, value: int) -> None:
        if (not isinstance(value, int) or isinstance(value, bool)
                or not -(1 << 63) <= value < (1 << 63)):
            _fail("TIMESTAMP_INVALID", "timestamp is not signed int64")
        self.values.append(value)
        self.count += 1
        if len(self.values) >= self.capacity:
            self._flush_run()

    def _flush_run(self) -> None:
        if not self.values:
            return
        ordered = sorted(self.values)
        self.values = array("q")
        root = self._ensure_root()
        path = root / ("run-%06d.bin" % len(self.runs))
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                serialized = array("q", ordered)
                serialized.tofile(handle)
                handle.flush()
                os.fsync(handle.fileno())
            observed = os.lstat(path)
        except OSError as exc:
            _fail("TIMESTAMP_SCRATCH_FAILED", type(exc).__name__)
        expected = len(ordered) * array("q").itemsize
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != expected:
            _fail("TIMESTAMP_SCRATCH_FAILED", "timestamp run size mismatch")
        self.runs.append(path)
        self.spilled_bytes += expected

    @staticmethod
    def _iter_run(path: Path) -> Iterator[int]:
        try:
            with open(path, "rb", buffering=READ_CHUNK_BYTES) as handle:
                while True:
                    raw = handle.read(RUN_READ_RECORDS * array("q").itemsize)
                    if not raw:
                        return
                    if len(raw) % array("q").itemsize:
                        _fail("TIMESTAMP_SCRATCH_CORRUPT", "run is truncated")
                    values = array("q")
                    values.frombytes(raw)
                    yield from values
        except ExactCaptureError:
            raise
        except OSError as exc:
            _fail("TIMESTAMP_SCRATCH_FAILED", type(exc).__name__)

    def iter_sorted(self) -> Iterator[int]:
        if not self.runs:
            ordered = sorted(self.values)
            self.values = array("q")
            yield from ordered
            return
        self._flush_run()
        yield from heapq.merge(*(self._iter_run(path) for path in self.runs))

    def close(self) -> None:
        self.values = array("q")
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        self.close()
        return False


def _iter_stream_lines(stream: Any) -> Iterator[bytes]:
    carry = b""
    while True:
        try:
            chunk = stream.read(READ_CHUNK_BYTES)
        except ExactCaptureError:
            raise
        except BaseException as exc:
            _fail("S3_GET_BODY_FAILED", type(exc).__name__)
        if not isinstance(chunk, bytes):
            _fail("S3_GET_BODY_INVALID", "GET stream yielded non-bytes")
        if not chunk:
            break
        data = carry + chunk
        parts = data.split(b"\n")
        carry = parts.pop()
        for line in parts:
            if len(line) > MAX_LINE_BYTES:
                _fail("RAW_LINE_TOO_LARGE", "raw record exceeds line limit")
            yield line[:-1] if line.endswith(b"\r") else line
        if len(carry) > MAX_LINE_BYTES:
            _fail("RAW_LINE_TOO_LARGE", "raw record exceeds line limit")
    if carry:
        yield carry[:-1] if carry.endswith(b"\r") else carry


def _scan_one_body(stream: Any, sorter: TimestampSorter) -> int:
    records = 0
    for line in _iter_stream_lines(stream):
        # Account for bytes through a wrapper is simpler and less error-prone;
        # real clients expose ``observed``.  Fake/test streams need not.
        if not line.strip():
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            _fail("RAW_RECORD_UNPARSEABLE", "raw record is not UTF-8")
        timestamp = cg._extract_recv_us(text)
        if timestamp is None:
            _fail("RAW_RECORD_UNPARSEABLE", "raw record has invalid recv_wall_ns")
        sorter.add(timestamp)
        records += 1
    return records


class HashingStream:
    """Read facade that counts and hashes the exact bytes seen by the parser."""

    def __init__(self, source: Any):
        self.source = source
        self.size = 0
        self.digest = hashlib.sha256()

    def read(self, size: int) -> bytes:
        chunk = self.source.read(size)
        if not isinstance(chunk, bytes):
            _fail("S3_GET_BODY_INVALID", "GET stream yielded non-bytes")
        self.size += len(chunk)
        self.digest.update(chunk)
        return chunk


def _historical_day_bounds(date: str, now_us: int | None) -> tuple[int, int]:
    start, end = cg._day_bounds_us(date)
    observed_now = int(time.time() * 1_000_000) if now_us is None else now_us
    if (not isinstance(observed_now, int) or isinstance(observed_now, bool)):
        _fail("CLOCK_INVALID", "now_us must be an integer")
    if end > observed_now:
        _fail("DAY_NOT_COMPLETE", "exact S3 backfill requires an elapsed UTC day")
    return start, end


def scan_exact_day(
    *,
    date: str,
    seal: Mapping[str, Any],
    seal_sha256: str,
    firehose: list[dict[str, Any]],
    client: Any,
    memory_limit_bytes: int,
    scratch_parent: str | os.PathLike[str] | None = None,
    now_us: int | None = None,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """HEAD then exact-GET every sealed firehose object and build a receipt."""
    date = _valid_date(date)
    if seal.get("date") != date or seal.get("method") != SEAL_METHOD:
        _fail("SEAL_NOT_VERIFIED", "scan input is not the requested full_v2 day")
    if not isinstance(firehose, list) or not firehose:
        _fail("SEALED_FIREHOSE_EMPTY", "firehose projection is empty")
    if not isinstance(seal_sha256, str) or SHA256_RE.fullmatch(seal_sha256) is None:
        _fail("SEAL_INVALID", "seal SHA-256 is invalid")
    expected_firehose = _project_sealed_firehose(seal, date)
    if firehose != expected_firehose:
        _fail("FIREHOSE_SCOPE_VIOLATION", "scan projection differs from full seal")
    day_start, day_end = _historical_day_bounds(date, now_us)
    exact_rows = []
    total_records = 0

    with TimestampSorter(memory_limit_bytes, scratch_parent) as sorter:
        for index, proof in enumerate(firehose):
            if not isinstance(proof, dict):
                _fail("SEAL_INVALID", f"firehose[{index}] is not an object")
            rel = proof.get("file")
            projected = _safe_sealed_rel(rel, date)
            if projected is None:
                _fail("FIREHOSE_SCOPE_VIOLATION", "non-firehose object reached scanner")
            key = f"{SOURCE_PREFIX}/raw/{rel}"
            try:
                head = client.head_current(SOURCE_BUCKET, key)
            except ExactCaptureError:
                raise
            except BaseException as exc:
                _fail("S3_HEAD_FAILED", f"HEAD failed for {key}: {type(exc).__name__}")
            if not isinstance(head, dict):
                _fail("S3_RESPONSE_INVALID", f"HEAD response invalid for {key}")
            version_id = _version(head.get("VersionId"), "HEAD response")
            head_size = _content_length(head.get("ContentLength"), "HEAD response")
            if head_size != proof.get("size"):
                _fail("S3_HEAD_SIZE_MISMATCH", f"HEAD size differs for {key}")

            try:
                context = client.open_exact(
                    SOURCE_BUCKET, key, version_id, head_size)
                with context as raw_stream:
                    stream = HashingStream(raw_stream)
                    object_records = _scan_one_body(stream, sorter)
                get_meta = getattr(raw_stream, "metadata", None)
            except ExactCaptureError:
                raise
            except BaseException as exc:
                _fail("S3_GET_FAILED", f"exact GET failed for {key}: {type(exc).__name__}")
            if not isinstance(get_meta, dict):
                _fail("S3_RESPONSE_INVALID", f"GET response invalid for {key}")
            if _version(get_meta.get("VersionId"), "GET response") != version_id:
                _fail("S3_GET_VERSION_MISMATCH", f"GET VersionId differs for {key}")
            if (_content_length(get_meta.get("ContentLength"), "GET response")
                    != head_size or stream.size != head_size):
                _fail("S3_GET_SIZE_MISMATCH", f"GET size differs for {key}")
            body_sha = stream.digest.hexdigest()
            if body_sha != proof.get("sha256"):
                _fail("S3_GET_SHA256_MISMATCH", f"GET SHA-256 differs for {key}")
            total_records += object_records
            exact_rows.append({
                "file": rel,
                "key": key,
                "VersionId": version_id,
                "bytes": head_size,
                "sha256": body_sha,
            })

        first = previous = None
        gaps = []
        sorted_count = 0
        for timestamp in sorter.iter_sorted():
            sorted_count += 1
            if first is None:
                first = timestamp
            if previous is not None and timestamp - previous > MIN_GAP_US:
                gaps.append((previous, timestamp))
            previous = timestamp
        if sorted_count != total_records or first is None or previous is None:
            _fail("RAW_RECORDS_EMPTY", "sealed firehose yielded no valid records")
        if first - day_start > MIN_GAP_US:
            gaps.append((day_start, first))
        if day_end - previous > MIN_GAP_US:
            gaps.append((previous, day_end))
        gaps.sort()
        spill_bytes = sorter.spilled_bytes
        record_capacity = sorter.capacity

    inventory = [{"file": row["file"], "bytes": row["bytes"]}
                 for row in exact_rows]
    expected_inventory = [{"file": row["file"], "bytes": row["size"]}
                          for row in firehose]
    if inventory != expected_inventory:
        _fail("RECEIPT_INVENTORY_MISMATCH", "receipt inventory differs from seal")
    generated = generated_at_utc
    if generated is None:
        generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(generated, str) or not generated.endswith("Z"):
        _fail("GENERATED_AT_INVALID", "generated_at_utc must be canonical UTC")
    exact_projection = [{
        "file": row["file"], "key": row["key"],
        "VersionId": row["VersionId"], "bytes": row["bytes"],
        "sha256": row["sha256"],
    } for row in exact_rows]
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "date": date,
        "raw_root": f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/raw",
        "files": inventory,
        "n_files": len(inventory),
        "total_bytes": sum(row["bytes"] for row in inventory),
        "records": total_records,
        "unparsed": 0,
        "unreadable": False,
        "gaps": [{"start_us": start, "end_us": end}
                 for start, end in gaps],
        "generated_at_utc": generated,
        "source_attestation": {
            "schema_version": SOURCE_ATTESTATION_SCHEMA,
            "bucket": SOURCE_BUCKET,
            "prefix": f"{SOURCE_PREFIX}/raw",
            "seal_sha256": seal_sha256,
            "selection": "SAME_DATE_SEALED_FIREHOSE_ONLY_RFQ_L2_EXCLUDED",
            "transport": "AWS_CLI_HEAD_CURRENT_THEN_GET_EXACT_VERSION",
            "objects": exact_projection,
            "exact_version_set_sha256": _canonical_sha256(exact_projection),
            "sort_memory_limit_bytes": memory_limit_bytes,
            "sort_record_capacity": record_capacity,
            "timestamp_spill_bytes": spill_bytes,
            "raw_payload_bytes_written_to_disk": 0,
        },
    }
    return receipt


def _strict_gap_rows(payload: bytes) -> list[tuple[int, int]]:
    try:
        text = payload.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames != ["start_us", "end_us"]:
            _fail("GAP_RECORD_INVALID", "capture_gaps.csv header is invalid")
        rows = []
        previous = None
        for lineno, row in enumerate(reader, 2):
            if set(row) != {"start_us", "end_us"} or None in row:
                _fail("GAP_RECORD_INVALID", f"row {lineno} is malformed")
            try:
                start, end = int(row["start_us"]), int(row["end_us"])
            except (TypeError, ValueError):
                _fail("GAP_RECORD_INVALID", f"row {lineno} is not integer")
            if start >= end:
                _fail("GAP_RECORD_INVALID", f"row {lineno} is inverted")
            if previous is not None and start < previous:
                _fail("GAP_RECORD_INVALID", "rows overlap or are unsorted")
            rows.append((start, end))
            previous = end
        return rows
    except ExactCaptureError:
        raise
    except csv.Error:
        _fail("GAP_RECORD_INVALID", "capture_gaps.csv is invalid CSV")


def _existing_gap_rows(path: str | os.PathLike[str]) -> list[tuple[int, int]]:
    if not os.path.lexists(path):
        return []
    payload = _regular_file_bytes(path, "capture_gaps.csv", 64 << 20)
    return _strict_gap_rows(payload)


def _render_gap_record(existing: list[tuple[int, int]], receipt: Mapping[str, Any]) -> bytes:
    date = receipt["date"]
    day_start, day_end = cg._day_bounds_us(date)
    kept = [(start, end) for start, end in existing
            if not (day_start <= start < day_end)]
    current = []
    for index, row in enumerate(receipt.get("gaps") or []):
        if not isinstance(row, dict) or set(row) != {"start_us", "end_us"}:
            _fail("RECEIPT_INVALID", f"gaps[{index}] shape invalid")
        start, end = row["start_us"], row["end_us"]
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or not day_start <= start < end <= day_end):
            _fail("RECEIPT_INVALID", f"gaps[{index}] is outside the day")
        current.append((start, end))
    rows = sorted(set(kept + current))
    for prior, later in zip(rows, rows[1:]):
        if later[0] < prior[1]:
            _fail("GAP_RECORD_INVALID", "merged gaps overlap")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["start_us", "end_us"])
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _stage_atomic(path: Path, payload: bytes) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            _fail("OUTPUT_PATH_INVALID", "output parent is not a real directory")
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.pending-", dir=path.parent)
        pending = Path(name)
        try:
            os.fchmod(fd, 0o640)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    _fail("OUTPUT_WRITE_FAILED", "short local write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return pending
    except ExactCaptureError:
        raise
    except OSError as exc:
        _fail("OUTPUT_WRITE_FAILED", type(exc).__name__)


def publish_outputs(
    receipt: Mapping[str, Any], record_path: str | os.PathLike[str],
    receipt_dir: str | os.PathLike[str],
) -> tuple[str, str]:
    """Atomically replace the target day's gap rows and its scan receipt."""
    date = _valid_date(receipt.get("date"))
    expected = receipt.get("files")
    if (receipt.get("schema_version") != RECEIPT_SCHEMA
            or not isinstance(expected, list) or not expected):
        _fail("RECEIPT_INVALID", "receipt contract gate failed")
    record = Path(record_path)
    receipt_path = Path(receipt_dir) / f"capture_gap_receipt_{date}.json"
    existing = _existing_gap_rows(record)
    gap_payload = _render_gap_record(existing, receipt)
    receipt_payload = json.dumps(
        receipt, sort_keys=True, indent=2, ensure_ascii=True,
        allow_nan=False).encode("utf-8")
    pending_gap = pending_receipt = None
    try:
        pending_gap = _stage_atomic(record, gap_payload)
        pending_receipt = _stage_atomic(receipt_path, receipt_payload)
        os.replace(pending_gap, record)
        pending_gap = None
        os.replace(pending_receipt, receipt_path)
        pending_receipt = None
        for parent in {record.parent, receipt_path.parent}:
            with contextlib.suppress(OSError):
                fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except ExactCaptureError:
        raise
    except OSError as exc:
        _fail("OUTPUT_COMMIT_FAILED", type(exc).__name__)
    finally:
        for pending in (pending_gap, pending_receipt):
            if pending is not None:
                with contextlib.suppress(OSError):
                    os.unlink(pending)
    return str(record), str(receipt_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="elapsed UTC date YYYY-MM-DD")
    parser.add_argument("--seal", required=True, help="local full_v2 seal JSON")
    parser.add_argument("--record", required=True,
                        help="capture_gaps.csv to update after a complete scan")
    parser.add_argument("--receipt-dir", required=True,
                        help="directory for capture_gap_receipt_DATE.json")
    parser.add_argument("--aws-cli", default="aws",
                        help="AWS CLI executable (credentials are inherited)")
    parser.add_argument("--sort-memory-mib", type=int,
                        default=DEFAULT_SORT_MEMORY_MIB,
                        help="timestamp sort working-set budget")
    parser.add_argument("--scratch-dir",
                        help="private parent for derived timestamp spill runs")
    parser.add_argument("--timeout-seconds", type=float, default=3600,
                        help="per exact GET AWS CLI timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not MIN_SORT_MEMORY_MIB <= args.sort_memory_mib <= MAX_SORT_MEMORY_MIB:
            _fail(
                "MEMORY_LIMIT_INVALID",
                f"sort-memory-mib must be {MIN_SORT_MEMORY_MIB}..{MAX_SORT_MEMORY_MIB}")
        seal, seal_sha, firehose = load_sealed_firehose(args.seal, args.date)
        client = AwsCliExactS3Client(args.aws_cli, args.timeout_seconds)
        receipt = scan_exact_day(
            date=args.date, seal=seal, seal_sha256=seal_sha,
            firehose=firehose, client=client,
            memory_limit_bytes=args.sort_memory_mib << 20,
            scratch_parent=args.scratch_dir)
        record, receipt_path = publish_outputs(
            receipt, args.record, args.receipt_dir)
    except ExactCaptureError as exc:
        # Error details are deliberately credential-free.  Raw records and
        # AWS stderr are never echoed.
        sys.stderr.write(f"FAIL_CLOSED {exc.code}: {exc.detail}\n")
        return 2
    print(json.dumps({
        "state": "CAPTURE_GAP_RECEIPT_REBUILT_EXACT_VERSION",
        "date": args.date,
        "objects": receipt["n_files"],
        "bytes": receipt["total_bytes"],
        "records": receipt["records"],
        "gaps": len(receipt["gaps"]),
        "record": record,
        "receipt": receipt_path,
        "s3_writes": 0,
        "raw_payload_bytes_written_to_disk": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
