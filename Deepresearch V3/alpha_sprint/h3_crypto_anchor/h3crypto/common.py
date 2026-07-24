"""Shared clocks, hashing, and append-only record helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import SCHEMA_VERSION


STREAM_FILES = {
    "coinbase": "coinbase_ws.ndjson",
    "kalshi_catalog": "kalshi_catalog.ndjson",
    "kalshi_market": "kalshi_market.ndjson",
    "receipts": "receipts.ndjson",
}


def utc_iso_from_ns(wall_ns: int) -> str:
    value = dt.datetime.fromtimestamp(wall_ns / 1_000_000_000, tz=dt.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def clock_sample(prefix: str = "local_record") -> Dict[str, Any]:
    wall_ns = time.time_ns()
    mono_ns = time.monotonic_ns()
    return {
        f"{prefix}_wall_ns": wall_ns,
        f"{prefix}_wall_utc": utc_iso_from_ns(wall_ns),
        f"{prefix}_monotonic_ns": mono_ns,
    }


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def is_rfc3339(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        normalized = value.replace("Z", "+00:00")
        # Python 3.9 is strict about fractional precision while exchange clocks
        # publish variable micro/nanoseconds. Pad/truncate only for validation;
        # the exact source string is retained in the raw envelope.
        match = re.fullmatch(
            r"(.+?)(?:\.(\d+))?([+-]\d{2}:\d{2})", normalized
        )
        if match and match.group(2) is not None:
            fraction = (match.group(2) + "000000")[:6]
            normalized = match.group(1) + "." + fraction + match.group(3)
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


class AppendOnlyLedger:
    """One-process append-only NDJSON ledger in a newly-created run directory."""

    def __init__(
        self,
        output_root: Path,
        run_id: Optional[str] = None,
        max_data_bytes: int = 50_000_000_000,
    ) -> None:
        if max_data_bytes <= 0:
            raise ValueError("max_data_bytes must be positive")
        output_root = Path(output_root).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        generated = run_id or (
            dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + "-"
            + uuid.uuid4().hex[:12]
        )
        if not generated or generated in {".", ".."} or "/" in generated:
            raise ValueError("run_id must be one path component")
        self.run_id = generated
        self.run_dir = output_root / generated
        self.run_dir.mkdir(mode=0o755, exist_ok=False)
        self._fds: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._counts = {name: 0 for name in STREAM_FILES}
        self._bytes = {name: 0 for name in STREAM_FILES}
        self._max_data_bytes = max_data_bytes
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        for stream, filename in STREAM_FILES.items():
            path = self.run_dir / filename
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | nofollow | cloexec
            self._fds[stream] = os.open(str(path), flags, 0o644)

    @property
    def counts(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._counts)

    @property
    def bytes_written(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._bytes)

    @property
    def max_data_bytes(self) -> int:
        return self._max_data_bytes

    def append(self, stream: str, record: Mapping[str, Any], fsync: bool = False) -> None:
        if stream not in self._fds:
            raise KeyError(f"unknown stream: {stream}")
        out = dict(record)
        out.setdefault("schema_version", SCHEMA_VERSION)
        out.setdefault("record_id", str(uuid.uuid4()))
        out.setdefault("run_id", self.run_id)
        for key, value in clock_sample().items():
            out.setdefault(key, value)
        line = canonical_json_bytes(out) + b"\n"
        with self._lock:
            data_bytes = sum(
                value for name, value in self._bytes.items() if name != "receipts"
            )
            if stream != "receipts" and data_bytes + len(line) > self._max_data_bytes:
                raise CaptureSizeLimit(
                    f"data byte cap {self._max_data_bytes} would be exceeded"
                )
            written = os.write(self._fds[stream], line)
            if written != len(line):
                raise OSError(f"short append: wrote {written} of {len(line)} bytes")
            self._counts[stream] += 1
            self._bytes[stream] += written
            if fsync:
                os.fsync(self._fds[stream])

    def receipt(self, event: str, fields: Optional[Mapping[str, Any]] = None) -> None:
        record: Dict[str, Any] = {
            "record_type": "capture_receipt",
            "source": "h3_capture",
            "event": event,
        }
        if fields:
            record.update(fields)
        self.append("receipts", record, fsync=True)

    def close(self) -> None:
        with self._lock:
            for fd in self._fds.values():
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._fds.clear()

    def __enter__(self) -> "AppendOnlyLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class CaptureSizeLimit(RuntimeError):
    """Raised before a data append would exceed the configured hard byte cap."""


class SequenceTracker:
    """Diagnose source sequence continuity without inventing missing messages."""

    def __init__(self) -> None:
        self._last: Dict[str, int] = {}

    def observe(
        self,
        connection_id: str,
        channel: str,
        products: Iterable[str],
        sequence_num: int,
    ) -> List[Dict[str, Any]]:
        scopes = sorted(set(products)) or ["__NO_PRODUCT__"]
        checks: List[Dict[str, Any]] = []
        for product in scopes:
            key = "\x1f".join((connection_id, channel, product))
            previous = self._last.get(key)
            if previous is None:
                status = "BASELINE"
                missing_count = 0
                self._last[key] = sequence_num
            elif sequence_num == previous + 1:
                status = "CONTIGUOUS"
                missing_count = 0
                self._last[key] = sequence_num
            elif sequence_num > previous + 1:
                status = "GAP"
                missing_count = sequence_num - previous - 1
                self._last[key] = sequence_num
            elif sequence_num == previous:
                status = "DUPLICATE"
                missing_count = 0
            else:
                status = "OUT_OF_ORDER"
                missing_count = 0
            checks.append(
                {
                    "connection_id": connection_id,
                    "channel": channel,
                    "product_id": product,
                    "previous_sequence_num": previous,
                    "sequence_num": sequence_num,
                    "status": status,
                    "missing_count": missing_count,
                }
            )
        return checks


def event_time_summary(values: Iterable[Any]) -> Dict[str, Any]:
    strings = [value for value in values if isinstance(value, str) and value]
    valid = [value for value in strings if is_rfc3339(value)]
    # RFC3339 timestamps emitted by these sources are UTC ISO strings; lexical
    # min/max are retained as diagnostics while every original value stays raw.
    return {
        "event_time_count": len(strings),
        "event_time_valid_count": len(valid),
        "event_time_invalid_count": len(strings) - len(valid),
        "event_time_min": min(valid) if valid else None,
        "event_time_max": max(valid) if valid else None,
    }
