#!/usr/bin/env python3
"""Durable read-only V3 release consumer for the W09 research cache.

This unit is intentionally narrower than a research runner.  It discovers
published V3 reference manifests, keeps every discovered date in a durable
local backlog, and asks the existing canonical consumer to fetch and verify
the selected exact-VersionId objects.  It never executes a research plan,
never writes S3, and never opts in to RFQ.

The configurable lookback is a *continuity/priority observation window*; it
is not a retention boundary.  Dates already discovered outside that window
remain in STATE.json until a verified local release exists.  This prevents a
temporarily failing historical date from silently aging out of the queue.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any, Callable, Iterator


HERE = Path(__file__).resolve().parent
REPO_TOOLS = HERE.parents[1] / "tools"
for candidate in (HERE, REPO_TOOLS):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import research_data as rd  # noqa: E402
import research_data_instance_profile as instance_profile  # noqa: E402
import research_reference as reference  # noqa: E402


STATE_SCHEMA = "w09-research-cache-sync-state-v1"
STATUS_SCHEMA = "w09-research-cache-sync-status-v1"
RECEIPT_SCHEMA = "w09-research-cache-sync-receipt-v1"
DEFAULT_ROOT = rd.ROOT_DEFAULT
DEFAULT_CACHE_ROOT = Path("/srv/w09-research/cache")
DEFAULT_LOOKBACK_DAYS = 35
MAX_CONTROL_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
V3_RELEASE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})__v3ref__seal-[0-9a-f]{8}"
    r"__pub-[0-9a-f]{16}$"
)
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(?:\.[0-9]{6})?Z-[0-9]+-[0-9a-f]{8}$")
PENDING_STATES = frozenset({"PENDING", "RETRY"})
VERIFIED_STATE = "VERIFIED"
RFQ_SAFE_MARKERS = frozenset({"ABSENT_FROM_RELEASE", "NOT_FETCHED_OPT_IN"})


class CacheSyncError(RuntimeError):
    """Fail-closed consumer error safe to summarize in local receipts."""


class AlreadyRunning(CacheSyncError):
    """A previous hourly invocation still owns the local sync lock."""


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise CacheSyncError("UTC timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_date(value: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CacheSyncError("release date is not strict ISO-8601") from exc
    if parsed.isoformat() != value:
        raise CacheSyncError("release date is not canonical ISO-8601")
    return parsed


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise CacheSyncError("control receipt is not canonical JSON") from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_object(raw: bytes, *, label: str, limit: int) -> dict[str, Any]:
    if not raw or len(raw) > limit:
        raise CacheSyncError(f"{label} byte size is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CacheSyncError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise CacheSyncError(f"{label} root must be an object")
    return value


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:1024]


def _ensure_directory(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CacheSyncError(f"unsafe control directory: {path}")


def _sync_root(cache_root: Path) -> Path:
    cache_root = cache_root.expanduser()
    _ensure_directory(cache_root, 0o700)
    if cache_root.is_symlink():
        raise CacheSyncError("cache root must not be a symlink")
    root = cache_root / ".research-cache-sync"
    _ensure_directory(root, 0o700)
    _ensure_directory(root / "receipts", 0o700)
    return root


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    _ensure_directory(path.parent, 0o700)
    if path.is_symlink():
        raise CacheSyncError(f"refusing symlink control file: {path}")
    if not replace and path.exists():
        raise CacheSyncError(f"immutable receipt already exists: {path.name}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise CacheSyncError(f"immutable receipt already exists: {path.name}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_control(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if missing is None:
            raise CacheSyncError(f"required control file is missing: {path.name}")
        return missing
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CacheSyncError(f"unsafe control file: {path.name}")
    if info.st_size <= 0 or info.st_size > MAX_CONTROL_BYTES:
        raise CacheSyncError(f"control file size is invalid: {path.name}")
    return _json_object(path.read_bytes(), label=path.name, limit=MAX_CONTROL_BYTES)


@contextlib.contextmanager
def exclusive_lock(sync_root: Path) -> Iterator[None]:
    lock_path = sync_root / "LOCK"
    if lock_path.is_symlink():
        raise CacheSyncError("sync lock must not be a symlink")
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("another research cache sync is active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _empty_state(now_text: str) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "created_at_utc": now_text,
        "updated_at_utc": now_text,
        "generation": 0,
        "rfq_policy": "OFF",
        "manifest_namespace": "research/releases/*/MANIFEST.json",
        "dates": {},
        "discovery_errors": {},
        "backlog_cursor": {
            "oldest_pending_date": None,
            "pending_dates": [],
            "pending_count": 0,
        },
        "last_run_id": None,
    }


def _validate_state(state: dict[str, Any]) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise CacheSyncError("unsupported or corrupt cache sync state schema")
    if state.get("rfq_policy") != "OFF":
        raise CacheSyncError("cache sync state attempted to enable RFQ")
    dates = state.get("dates")
    errors = state.get("discovery_errors")
    if not isinstance(dates, dict) or not isinstance(errors, dict):
        raise CacheSyncError("cache sync state maps are malformed")
    for date_text, row in dates.items():
        _parse_date(date_text)
        if not isinstance(row, dict) or row.get("date") != date_text:
            raise CacheSyncError("cache sync date entry is malformed")
        release_id = row.get("selected_release_id")
        match = V3_RELEASE_RE.fullmatch(str(release_id))
        if match is None or match.group("date") != date_text:
            raise CacheSyncError("cache sync date release identity is malformed")
        if row.get("status") not in PENDING_STATES | {VERIFIED_STATE}:
            raise CacheSyncError("cache sync date status is malformed")
        if not isinstance(row.get("attempts"), int) or row["attempts"] < 0:
            raise CacheSyncError("cache sync attempt counter is malformed")
        if (
            not isinstance(row.get("correction_rank"), int)
            or row["correction_rank"] < 0
        ):
            raise CacheSyncError("cache sync correction rank is malformed")


def _load_state(sync_root: Path, now_text: str) -> dict[str, Any]:
    state = _read_control(
        sync_root / "STATE.json", missing=_empty_state(now_text)
    )
    _validate_state(state)
    return state


def _write_state(sync_root: Path, state: dict[str, Any], now_text: str) -> bytes:
    state["updated_at_utc"] = now_text
    state["generation"] = int(state.get("generation") or 0) + 1
    _refresh_cursor(state)
    _validate_state(state)
    payload = _canonical_bytes(state)
    if len(payload) > MAX_CONTROL_BYTES:
        raise CacheSyncError("cache sync state exceeds bounded control size")
    _atomic_write(sync_root / "STATE.json", payload, replace=True)
    return payload


def _refresh_cursor(state: dict[str, Any]) -> dict[str, Any]:
    pending = sorted(
        date_text
        for date_text, row in state["dates"].items()
        if row["status"] in PENDING_STATES
    )
    cursor = {
        "oldest_pending_date": pending[0] if pending else None,
        "pending_dates": pending,
        "pending_count": len(pending),
    }
    state["backlog_cursor"] = cursor
    return cursor


def _run_id(now: dt.datetime) -> str:
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{os.getpid()}-{secrets.token_hex(4)}"


def _manifest_paths(store: Any) -> tuple[list[tuple[str, int]], int]:
    rows = store.list("releases/")
    if not isinstance(rows, list):
        raise CacheSyncError("manifest inventory response is malformed")
    accepted: dict[str, int] = {}
    ignored = 0
    for row in rows:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 2
            or not isinstance(row[0], str)
            or not isinstance(row[1], int)
            or row[1] < 0
        ):
            raise CacheSyncError("manifest inventory row is malformed")
        key, size = row
        parts = key.split("/")
        if (
            len(parts) != 3
            or parts[0] != "releases"
            or parts[2] != "MANIFEST.json"
            or V3_RELEASE_RE.fullmatch(parts[1]) is None
        ):
            ignored += 1
            continue
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise CacheSyncError(f"V3 manifest listing size is invalid: {parts[1]}")
        if key in accepted and accepted[key] != size:
            raise CacheSyncError(f"duplicate manifest inventory conflict: {parts[1]}")
        accepted[key] = size
    return sorted(accepted.items()), ignored


def discover_releases(
    store: Any,
    validator: Callable[[dict[str, Any], str], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, int]]:
    """Return the newest strictly valid V3 manifest for every known date."""
    paths, ignored = _manifest_paths(store)
    by_date: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for key, listed_size in paths:
        release_id = key.split("/")[1]
        try:
            raw, version_id = store.get_bytes_with_version(key)
            if not isinstance(raw, bytes) or len(raw) != listed_size:
                raise CacheSyncError("manifest bytes differ from listing size")
            if (
                not isinstance(version_id, str)
                or not version_id.strip()
                or version_id.strip().lower() == "null"
            ):
                raise CacheSyncError("manifest has no exact S3 VersionId")
            manifest = _json_object(
                raw, label=f"V3 MANIFEST {release_id}", limit=MAX_MANIFEST_BYTES
            )
            descriptor = validator(manifest, release_id)
            if not isinstance(descriptor, dict):
                raise CacheSyncError("V3 validator returned no descriptor")
            match = V3_RELEASE_RE.fullmatch(release_id)
            assert match is not None
            date_text = match.group("date")
            if (
                descriptor.get("schema") != reference.SCHEMA
                or descriptor.get("storage_mode") != reference.STORAGE_MODE
                or descriptor.get("release_id") != release_id
                or descriptor.get("date") != date_text
            ):
                raise CacheSyncError("V3 descriptor identity is inconsistent")
            corrections = descriptor.get("corrections") or {}
            correction_rank = int(corrections.get("included_files") or 0) + int(
                corrections.get("ledger_day_entries") or 0
            )
            candidate = {
                "release_id": release_id,
                "date": date_text,
                "published_at_utc": descriptor.get("published_at_utc"),
                "manifest_sha256": _sha256_bytes(raw),
                "manifest_version_id": version_id,
                "reference_set_sha256": descriptor.get("reference_set_sha256"),
                "publication_state_sha256": descriptor.get(
                    "publication_state_sha256"
                ),
                "evidence_tier": descriptor.get("evidence_tier"),
                "rfq_included_in_manifest": bool(descriptor.get("rfq_included")),
                "correction_rank": correction_rank,
            }
            if not isinstance(candidate["published_at_utc"], str):
                raise CacheSyncError("V3 published_at_utc is missing")
            rank = (
                candidate["correction_rank"],
                candidate["published_at_utc"],
                release_id,
            )
            current = by_date.get(date_text)
            if current is None or rank > current["_rank"]:
                candidate["_rank"] = rank
                by_date[date_text] = candidate
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            errors[release_id] = _safe_error(exc)
    for candidate in by_date.values():
        candidate.pop("_rank", None)
    stats = {
        "listed_objects": len(paths) + ignored,
        "v3_manifest_candidates": len(paths),
        "valid_selected_dates": len(by_date),
        "ignored_non_v3_or_non_manifest": ignored,
        "invalid_v3_manifests": len(errors),
    }
    return by_date, errors, stats


def _superseded_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "selected_release_id",
            "manifest_sha256",
            "manifest_version_id",
            "published_at_utc",
            "verified_at_utc",
        )
    }


def _merge_discovery(
    state: dict[str, Any],
    selected: dict[str, dict[str, Any]],
    errors: dict[str, str],
    now_text: str,
) -> None:
    state["discovery_errors"] = {
        release_id: {"last_seen_at_utc": now_text, "error": detail}
        for release_id, detail in sorted(errors.items())
    }
    for date_text, existing in state["dates"].items():
        if date_text not in selected:
            state["discovery_errors"].setdefault(
                existing["selected_release_id"],
                {
                    "last_seen_at_utc": now_text,
                    "error": (
                        "SELECTED_RELEASE_ABSENT: no valid V3 manifest was "
                        "discovered for this previously known date"
                    ),
                },
            )
    for date_text, candidate in sorted(selected.items()):
        existing = state["dates"].get(date_text)
        if existing is None:
            state["dates"][date_text] = {
                "date": date_text,
                "selected_release_id": candidate["release_id"],
                "manifest_sha256": candidate["manifest_sha256"],
                "manifest_version_id": candidate["manifest_version_id"],
                "published_at_utc": candidate["published_at_utc"],
                "reference_set_sha256": candidate["reference_set_sha256"],
                "publication_state_sha256": candidate[
                    "publication_state_sha256"
                ],
                "evidence_tier": candidate["evidence_tier"],
                "correction_rank": candidate["correction_rank"],
                "rfq_included_in_manifest": candidate[
                    "rfq_included_in_manifest"
                ],
                "rfq_fetch_policy": "OFF",
                "status": "PENDING",
                "attempts": 0,
                "first_discovered_at_utc": now_text,
                "last_discovered_at_utc": now_text,
                "last_attempt_at_utc": None,
                "verified_at_utc": None,
                "last_error": None,
                "superseded_releases": [],
            }
            continue
        existing["last_discovered_at_utc"] = now_text
        if existing["selected_release_id"] == candidate["release_id"]:
            if (
                existing.get("manifest_sha256") != candidate["manifest_sha256"]
                or existing.get("manifest_version_id")
                != candidate["manifest_version_id"]
            ):
                existing["status"] = "RETRY"
                existing["last_error"] = (
                    "IMMUTABILITY_VIOLATION: selected MANIFEST identity changed"
                )
            continue
        existing_rank = (
            existing["correction_rank"],
            existing["published_at_utc"],
            existing["selected_release_id"],
        )
        candidate_rank = (
            candidate["correction_rank"],
            candidate["published_at_utc"],
            candidate["release_id"],
        )
        if candidate_rank <= existing_rank:
            # S3 is strongly consistent, so a regression means an access,
            # inventory, or integrity fault.  Keep the already selected newer
            # correction and make the run unhealthy instead of silently
            # exposing an older publication state.
            state["discovery_errors"][existing["selected_release_id"]] = {
                "last_seen_at_utc": now_text,
                "error": (
                    "MONOTONIC_SELECTION_REFUSED: latest release regressed "
                    f"to {candidate['release_id']}"
                ),
            }
            continue
        superseded = existing.setdefault("superseded_releases", [])
        if not isinstance(superseded, list):
            raise CacheSyncError("superseded release history is malformed")
        superseded.append(_superseded_record(existing))
        existing.update({
            "selected_release_id": candidate["release_id"],
            "manifest_sha256": candidate["manifest_sha256"],
            "manifest_version_id": candidate["manifest_version_id"],
            "published_at_utc": candidate["published_at_utc"],
            "reference_set_sha256": candidate["reference_set_sha256"],
            "publication_state_sha256": candidate[
                "publication_state_sha256"
            ],
            "evidence_tier": candidate["evidence_tier"],
            "correction_rank": candidate["correction_rank"],
            "rfq_included_in_manifest": candidate["rfq_included_in_manifest"],
            "rfq_fetch_policy": "OFF",
            "status": "PENDING",
            "attempts": 0,
            "last_attempt_at_utc": None,
            "verified_at_utc": None,
            "last_error": None,
        })


def _continuity(selected_dates: list[str], lookback_days: int) -> dict[str, Any]:
    if not selected_dates:
        return {
            "lookback_days": lookback_days,
            "start_date": None,
            "end_date": None,
            "selected_dates": [],
            "missing_dates": [],
            "status": "EMPTY",
        }
    parsed_dates = {_parse_date(value) for value in selected_dates}
    end = max(parsed_dates)
    # Never invent a pre-publication backlog.  The observation window starts
    # at the later of the configured horizon or the first V3 date actually
    # published; genuine holes *between* published dates still fail loudly.
    start = max(
        min(parsed_dates), end - dt.timedelta(days=lookback_days - 1)
    )
    selected = {
        parsed
        for parsed in parsed_dates
        if parsed >= start
    }
    expected = {
        start + dt.timedelta(days=offset)
        for offset in range((end - start).days + 1)
    }
    missing = sorted(expected - selected)
    return {
        "lookback_days": lookback_days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "selected_dates": [value.isoformat() for value in sorted(selected)],
        "missing_dates": [value.isoformat() for value in missing],
        "status": "CONTIGUOUS" if not missing else "GAPPED",
    }


def _marker_path(cache_root: Path, release_id: str) -> Path:
    return cache_root / "releases" / release_id / ".VERIFIED.json"


def _read_marker(cache_root: Path, release_id: str) -> dict[str, Any]:
    marker_path = _marker_path(cache_root, release_id)
    return _read_control(marker_path)


def _validate_marker(row: dict[str, Any], marker: dict[str, Any]) -> None:
    expected = {
        "schema": "research-reference-verified-v1",
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": reference.STORAGE_MODE,
        "release_id": row["selected_release_id"],
        "date": row["date"],
        "manifest_sha256": row["manifest_sha256"],
        "manifest_version_id": row["manifest_version_id"],
        "reference_set_sha256": row["reference_set_sha256"],
        "publication_state_sha256": row["publication_state_sha256"],
    }
    for field, value in expected.items():
        if marker.get(field) != value:
            raise CacheSyncError(f"verified marker mismatch on {field}")
    if marker.get("rfq_status") not in RFQ_SAFE_MARKERS:
        raise CacheSyncError(
            "RFQ is materialized in the selected release; daily sync is RFQ-OFF"
        )


def _reopen_missing_local_entries(state: dict[str, Any], cache_root: Path) -> None:
    for row in state["dates"].values():
        if row["status"] != VERIFIED_STATE:
            continue
        try:
            _validate_marker(
                row, _read_marker(cache_root, row["selected_release_id"])
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            row["status"] = "RETRY"
            row["last_error"] = "LOCAL_VERIFICATION_LOST: " + _safe_error(exc)


def canonical_fetcher(
    store: Any, cache_root: Path, row: dict[str, Any]
) -> dict[str, Any]:
    """Use the canonical exact-version fetch/verify API with RFQ forced off."""
    marker_path = _marker_path(cache_root, row["selected_release_id"])
    if marker_path.exists():
        existing = _read_marker(cache_root, row["selected_release_id"])
        if existing.get("rfq_status") == "VERIFIED_SEALED_RAW":
            raise CacheSyncError(
                "existing cache materializes RFQ; daily RFQ-OFF sync refuses it"
            )
    result = rd.cmd_fetch(
        store,
        str(cache_root),
        row["selected_release_id"],
        False,
        allow_legacy=False,
    )
    if result != 0:
        raise CacheSyncError(f"canonical exact-version fetch returned {result}")
    marker = _read_marker(cache_root, row["selected_release_id"])
    _validate_marker(row, marker)
    return marker


def sync_once(
    *,
    store: Any,
    cache_root: Path,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_releases_per_run: int = 0,
    validator: Callable[[dict[str, Any], str], dict[str, Any]] = reference.validate_manifest,
    fetcher: Callable[[Any, Path, dict[str, Any]], dict[str, Any]] = canonical_fetcher,
    now: dt.datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= lookback_days <= 3660:
        raise CacheSyncError("lookback_days must be between 1 and 3660")
    if not 0 <= max_releases_per_run <= 10000:
        raise CacheSyncError("max_releases_per_run must be between 0 and 10000")
    now = now or _now_utc()
    now_text = _utc_text(now)
    run_id = run_id or _run_id(now)
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise CacheSyncError("run_id is not path-safe")
    cache_root = cache_root.expanduser()
    sync_root = _sync_root(cache_root)
    state = _load_state(sync_root, now_text)

    selected, discovery_errors, scan_stats = discover_releases(store, validator)
    _merge_discovery(state, selected, discovery_errors, now_text)
    _reopen_missing_local_entries(state, cache_root)
    state["last_run_id"] = run_id
    # Persist discovery before any large fetch.  A crash therefore cannot
    # make a newly discovered historical date disappear from the backlog.
    _write_state(sync_root, state, now_text)

    pending = sorted(
        date_text
        for date_text, row in state["dates"].items()
        if row["status"] in PENDING_STATES
    )
    scheduled = pending[
        : max_releases_per_run or None
    ]
    attempts: list[dict[str, Any]] = []
    for date_text in scheduled:
        row = state["dates"][date_text]
        row["attempts"] += 1
        row["last_attempt_at_utc"] = now_text
        try:
            marker = fetcher(store, cache_root, row)
            _validate_marker(row, marker)
            row["status"] = VERIFIED_STATE
            row["verified_at_utc"] = now_text
            row["last_error"] = None
            attempts.append({
                "date": date_text,
                "release_id": row["selected_release_id"],
                "status": "VERIFIED",
                "rfq_status": marker["rfq_status"],
            })
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            row["status"] = "RETRY"
            row["last_error"] = _safe_error(exc)
            attempts.append({
                "date": date_text,
                "release_id": row["selected_release_id"],
                "status": "RETRY",
                "error": row["last_error"],
            })
        # Commit progress after each date so an interruption resumes at the
        # precise oldest remaining backlog cursor.
        _write_state(sync_root, state, now_text)

    cursor = _refresh_cursor(state)
    continuity = _continuity(sorted(selected), lookback_days)
    if not state["dates"]:
        outcome = "EMPTY_RETRY"
    elif (
        state["discovery_errors"]
        or cursor["pending_count"]
        or continuity["missing_dates"]
    ):
        outcome = "RETRY_REQUIRED"
    else:
        outcome = "PASS"
    state_payload = _write_state(sync_root, state, now_text)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "started_and_finished_at_utc": now_text,
        "outcome": outcome,
        "consumer_mode": "READ_ONLY_EXACT_VERSION_V3",
        "rfq_policy": "OFF",
        "lookback_is_not_retention": True,
        "scan": scan_stats,
        "continuity": continuity,
        "attempts": attempts,
        "backlog_cursor": cursor,
        "known_dates": sorted(state["dates"]),
        "selected_releases": {
            date_text: state["dates"][date_text]["selected_release_id"]
            for date_text in sorted(state["dates"])
        },
        "discovery_errors": state["discovery_errors"],
        "state_sha256": _sha256_bytes(state_payload),
    }
    receipt_payload = _canonical_bytes(receipt)
    receipt_name = f"{run_id}.json"
    _atomic_write(
        sync_root / "receipts" / receipt_name,
        receipt_payload,
        replace=False,
    )
    status = {
        "schema": STATUS_SCHEMA,
        "updated_at_utc": now_text,
        "outcome": outcome,
        "run_id": run_id,
        "receipt": f"receipts/{receipt_name}",
        "receipt_sha256": _sha256_bytes(receipt_payload),
        "backlog_cursor": cursor,
        "continuity": continuity,
        "rfq_policy": "OFF",
    }
    _atomic_write(sync_root / "STATUS.json", _canonical_bytes(status), replace=True)
    return {
        "exit_code": 0 if outcome == "PASS" else 2,
        "outcome": outcome,
        "run_id": run_id,
        "receipt": str(sync_root / "receipts" / receipt_name),
        "backlog_cursor": cursor,
        "continuity": continuity,
    }


def _write_fatal_status(cache_root: Path, exc: BaseException) -> None:
    try:
        root = _sync_root(cache_root)
        payload = {
            "schema": STATUS_SCHEMA,
            "updated_at_utc": _utc_text(_now_utc()),
            "outcome": "FATAL_RETRY",
            "error": _safe_error(exc),
            "rfq_policy": "OFF",
        }
        _atomic_write(root / "STATUS.json", _canonical_bytes(payload), replace=True)
    except BaseException:
        # The original failure remains authoritative.  In particular, never
        # replace corrupt state merely to make a status write look healthy.
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS
    )
    parser.add_argument("--max-releases-per-run", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Static credentials are forbidden even if a future refactor changes
        # the store factory.  The W09 instance profile is the only S3 identity.
        instance_profile.refuse_static_credentials()
        store = instance_profile.make_instance_profile_store(args.root)
        root = _sync_root(args.cache_root)
        with exclusive_lock(root):
            result = sync_once(
                store=store,
                cache_root=args.cache_root,
                lookback_days=args.lookback_days,
                max_releases_per_run=args.max_releases_per_run,
            )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return int(result["exit_code"])
    except AlreadyRunning as exc:
        sys.stderr.write(f"RETRY: {_safe_error(exc)}\n")
        return 75
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        _write_fatal_status(args.cache_root, exc)
        sys.stderr.write(f"RETRY: {_safe_error(exc)}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
