#!/usr/bin/env python3
"""Durable per-date coordinator for the independent fresh-RFQ research lane.

The queue never drops a date because it aged out of a lookback window.  Every
date advances independently through eligibility, streaming overlay creation,
and exact-version tag/publication.  Until the separately reviewed IAM
capability exists, the honest terminal-for-now state is ``WAITING_IAM``; it is
not reported as research-ready and remains automatically resumable.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fresh_rfq_daily_eligibility as gate  # noqa: E402
import fresh_rfq_daily_runner as producer  # noqa: E402
import fresh_rfq_overlay_release as overlay  # noqa: E402


SCHEMA = "fresh-rfq-daily-pipeline-state-v1"
CAPABILITY_SCHEMA = "fresh-rfq-iam-capability-v1"
CAPABILITY_STATE = "FRESH_RFQ_EXACT_TAG_AND_READ_IAM_APPLIED"
PUBLISHED_STATE = "RFQ_OVERLAY_PUBLISHED_EXACT_VERSION_VERIFIED"
STATE_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/fresh_rfq_pipeline")
BASE_STATUS_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/live/research_v3_daily")
CAPABILITY_PATH = pathlib.Path(
    "/var/lib/kalshi-rfq-fresh/control/fresh-rfq-20260720-01/"
    "IAM-CAPABILITY.json")
MAX_STATE_BYTES = 4 << 20
MAX_TERMINAL_BYTES = 16 << 20
MAX_QUEUE_DATES = 4096
MAX_TERMINALS_PER_DATE = 128
MAX_BASE_MANIFEST_BYTES = 16 << 20
BASE_RETRY_SECONDS = 3600
MAX_RETRY_SECONDS = 86400
DATE_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})\.json$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class PipelineError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise PipelineError(code, detail)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PipelineError("NON_CANONICAL_VALUE", str(exc)) from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(
        microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any, label: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError) as exc:
        raise PipelineError("TIME_INVALID", f"{label}: {exc}") from exc


def _read_json(path: pathlib.Path, limit: int = MAX_STATE_BYTES) -> Any:
    path = pathlib.Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PipelineError("LOCAL_INPUT_INVALID", f"{path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            _fail("LOCAL_INPUT_INVALID", str(path))
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or len(raw) > limit
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            _fail("LOCAL_INPUT_CHANGED", str(path))
    finally:
        os.close(fd)
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PipelineError("JSON_INVALID", f"{path}: {exc}") from exc


def _atomic_state(path: pathlib.Path, value: dict[str, Any]) -> None:
    path = pathlib.Path(path).absolute()
    value = copy.deepcopy(value)
    value.pop("state_sha256", None)
    value["state_sha256"] = _sha(value)
    raw = _canonical(value) + b"\n"
    if len(raw) > MAX_STATE_BYTES:
        _fail("STATE_TOO_LARGE", str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        _fail("STATE_PATH_INVALID", str(path))
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _validate_state(value: Any, date: str) -> dict[str, Any]:
    fields = {
        "schema_version", "date", "state", "first_enqueued_at_utc",
        "updated_at_utc", "attempt_count", "next_attempt_at_utc",
        "eligibility_path", "overlay_ready_path", "publication_proof",
        "last_error", "aws_write_state", "resume_stage", "state_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        _fail("QUEUE_STATE_INVALID", f"{date}: fields differ")
    supplied = value.get("state_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("state_sha256")
    if supplied != _sha(unsigned) or value.get("schema_version") != SCHEMA \
            or value.get("date") != date:
        _fail("QUEUE_STATE_INVALID", date)
    if type(value.get("attempt_count")) is not int \
            or value["attempt_count"] < 0:
        _fail("QUEUE_STATE_INVALID", f"{date}: attempt count")
    _parse_utc(value.get("first_enqueued_at_utc"), "first enqueue")
    _parse_utc(value.get("updated_at_utc"), "updated")
    _parse_utc(value.get("next_attempt_at_utc"), "next attempt")
    return copy.deepcopy(value)


def _new_state(date: str, now: dt.datetime) -> dict[str, Any]:
    text = _utc(now)
    value = {
        "schema_version": SCHEMA,
        "date": date,
        "state": "PENDING_ELIGIBILITY",
        "first_enqueued_at_utc": text,
        "updated_at_utc": text,
        "attempt_count": 0,
        "next_attempt_at_utc": text,
        "eligibility_path": None,
        "overlay_ready_path": None,
        "publication_proof": None,
        "last_error": None,
        "aws_write_state": "NOT_AUTHORIZED",
        "resume_stage": "ELIGIBILITY",
    }
    value["state_sha256"] = _sha(value)
    return value


def _queue_path(root: pathlib.Path, date: str) -> pathlib.Path:
    return pathlib.Path(root).absolute() / "queue" / f"date={date}.json"


def seed_queue(root: pathlib.Path, now: dt.datetime) -> list[str]:
    first = dt.date.fromisoformat(gate.PRODUCTION_STRICT_T0_UTC[:10])
    last = now.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1)
    if last < first:
        return []
    count = (last - first).days + 1
    if count > MAX_QUEUE_DATES:
        _fail("QUEUE_DATE_BOUND", str(count))
    dates = [(first + dt.timedelta(days=index)).isoformat()
             for index in range(count)]
    for date in dates:
        path = _queue_path(root, date)
        if not os.path.lexists(path):
            _atomic_state(path, _new_state(date, now))
    return dates


def load_queue(root: pathlib.Path) -> list[tuple[pathlib.Path, dict[str, Any]]]:
    queue = pathlib.Path(root).absolute() / "queue"
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = []
    paths = sorted(queue.iterdir(), key=lambda path: path.name)
    if len(paths) > MAX_QUEUE_DATES:
        _fail("QUEUE_DATE_BOUND", str(len(paths)))
    for path in paths:
        match = DATE_RE.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            _fail("QUEUE_PATH_INVALID", str(path))
        date = match.group(1)
        try:
            if dt.date.fromisoformat(date).isoformat() != date:
                raise ValueError("noncanonical")
        except ValueError as exc:
            raise PipelineError("QUEUE_PATH_INVALID", str(path)) from exc
        entries.append((path, _validate_state(_read_json(path), date)))
    return entries


def _terminal_identity(status: dict[str, Any]) -> dict[str, Any]:
    manifest = status["manifest_object"]
    return {
        "bucket": manifest["bucket"], "key": manifest["key"],
        "version_id": manifest["VersionId"], "size": manifest["size"],
        "sha256": manifest["sha256"],
    }


def discover_base_terminal(date: str, root: pathlib.Path
                           ) -> tuple[pathlib.Path, dict[str, Any],
                                      dict[str, Any]]:
    date_root = pathlib.Path(root).absolute() / f"date={date}"
    if not date_root.is_dir() or date_root.is_symlink():
        _fail("BASE_NOT_READY", str(date_root))
    candidates = sorted(date_root.glob("receipt=*/STATUS.json"))
    direct = date_root / "STATUS.json"
    if direct.exists():
        candidates.append(direct)
    if not candidates or len(candidates) > MAX_TERMINALS_PER_DATE:
        _fail("BASE_NOT_READY", f"terminal count={len(candidates)}")
    valid = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            _fail("BASE_TERMINAL_INVALID", str(path))
        raw = producer._read_regular(path, MAX_TERMINAL_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"))
            checked = overlay._validate_base_terminal(value, raw, date)
        except Exception:
            continue
        valid.append((path, checked, _terminal_identity(checked)))
    if not valid:
        _fail("BASE_NOT_READY", date)
    bindings = {_canonical(identity) for _path, _status, identity in valid}
    if len(bindings) != 1:
        _fail("BASE_TERMINAL_AMBIGUOUS", date)
    return valid[-1]


def load_capability(path: pathlib.Path) -> dict[str, Any] | None:
    path = pathlib.Path(path).absolute()
    if not os.path.lexists(path):
        return None
    value = _read_json(path)
    fields = {
        "schema_version", "state", "generation", "authority_sha256",
        "tagger_policy_sha256", "bucket_policy_sha256",
        "w09_policy_sha256", "applied_at_utc", "capability_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        _fail("IAM_CAPABILITY_INVALID", "fields differ")
    supplied = value["capability_sha256"]
    unsigned = copy.deepcopy(value)
    unsigned.pop("capability_sha256")
    fixed = {
        "schema_version": CAPABILITY_SCHEMA,
        "state": CAPABILITY_STATE,
        "generation": gate.PRODUCTION_GENERATION,
        "authority_sha256": gate.PRODUCTION_AUTHORITY_SHA256,
    }
    if (any(value.get(key) != wanted for key, wanted in fixed.items())
            or supplied != _sha(unsigned)
            or any(SHA_RE.fullmatch(str(value.get(field))) is None for field in (
                "tagger_policy_sha256", "bucket_policy_sha256",
                "w09_policy_sha256"))):
        _fail("IAM_CAPABILITY_INVALID", "binding differs")
    _parse_utc(value["applied_at_utc"], "capability applied")
    return copy.deepcopy(value)


def _next_retry(now: dt.datetime, attempts: int) -> str:
    exponent = min(max(attempts - 1, 0), 5)
    delay = min(BASE_RETRY_SECONDS * (2 ** exponent), MAX_RETRY_SECONDS)
    return _utc(now + dt.timedelta(seconds=delay))


def _advance(state: dict[str, Any], *, now: dt.datetime, new_state: str,
             resume_stage: str, next_attempt: str | None = None,
             error: dict[str, Any] | None = None, **updates: Any
             ) -> dict[str, Any]:
    result = copy.deepcopy(state)
    result.update(updates)
    result["state"] = new_state
    result["resume_stage"] = resume_stage
    result["updated_at_utc"] = _utc(now)
    result["next_attempt_at_utc"] = next_attempt or _utc(now)
    result["last_error"] = error
    result.pop("state_sha256", None)
    result["state_sha256"] = _sha(result)
    return result


def _error_state(state: dict[str, Any], *, now: dt.datetime,
                 stage: str, exc: BaseException) -> dict[str, Any]:
    attempts = state["attempt_count"] + 1
    code = getattr(exc, "code", type(exc).__name__)
    permanent = code in {
        "OLD_LINEAGE_OVERLAP", "CAPTURE_ALERT_PRESENT",
        "MULTIPLE_CAPTURE_SESSIONS", "BASE_TERMINAL_AMBIGUOUS"}
    return _advance(
        {**state, "attempt_count": attempts}, now=now,
        new_state=("QUARANTINED_DATA" if permanent else "RETRY_SCHEDULED"),
        resume_stage=stage,
        next_attempt=("9999-12-31T23:59:59Z" if permanent
                      else _next_retry(now, attempts)),
        error={"stage": stage, "code": str(code),
               "detail": str(exc)[-1000:]})


def _purge_exact_cache(reader: producer.ExactS3Reader) -> None:
    cache = reader.scratch_root / "exact-object-cache"
    try:
        resolved_root = reader.scratch_root.resolve(strict=True)
        resolved_parent = cache.parent.resolve(strict=True)
    except OSError:
        return
    if resolved_parent != resolved_root or cache.is_symlink():
        _fail("SCRATCH_CACHE_PATH_INVALID", str(cache))
    shutil.rmtree(cache, ignore_errors=False) if cache.exists() else None


Publisher = Callable[[pathlib.Path, dict[str, Any]], dict[str, Any]]


def process_date(
        state: dict[str, Any], *, now: dt.datetime,
        transport: producer.AwsReadOnly, state_root: pathlib.Path,
        base_status_root: pathlib.Path, capability_path: pathlib.Path,
        publisher: Publisher | None = None) -> dict[str, Any]:
    date = state["date"]
    if state["state"] in {"PUBLISHED", "QUARANTINED_DATA"}:
        return state
    if _parse_utc(state["next_attempt_at_utc"], "next attempt") > now:
        return state
    try:
        eligible_path = pathlib.Path(state["eligibility_path"]) \
            if state["eligibility_path"] else None
        if eligible_path is None:
            eligible_path = producer.produce(
                date, transport=transport,
                output_root=producer.OUTPUT_ROOT,
                scratch_root=producer.SCRATCH_ROOT,
                generated_at_utc=_utc(now))
            state = _advance(
                state, now=now, new_state="ELIGIBLE_WAITING_BASE",
                resume_stage="BASE", eligibility_path=str(eligible_path))
            _atomic_state(_queue_path(state_root, date), state)
        else:
            gate.load_package(eligible_path, expected_date=date)
    except BaseException as exc:
        return _error_state(state, now=now, stage="ELIGIBILITY", exc=exc)

    try:
        terminal_path, terminal, manifest_identity = discover_base_terminal(
            date, base_status_root)
    except BaseException as exc:
        return _error_state(state, now=now, stage="BASE", exc=exc)
    if (manifest_identity["size"] <= 0
            or manifest_identity["size"] > MAX_BASE_MANIFEST_BYTES):
        return _error_state(
            state, now=now, stage="BASE",
            exc=PipelineError("BASE_MANIFEST_BOUND", date))

    ready_path = pathlib.Path(state["overlay_ready_path"]) \
        if state["overlay_ready_path"] else None
    reader = producer.ExactS3Reader(
        transport, producer.SCRATCH_ROOT / f"date={date}")
    if ready_path is None:
        try:
            with reader.open_exact(manifest_identity) as opened:
                ready_path = overlay.create_overlay_cache_from_reader(
                    date=date, eligibility_path=eligible_path,
                    base_terminal_path=terminal_path,
                    base_manifest_path=opened.path,
                    base_manifest_exact_identity=manifest_identity,
                    open_exact=reader.open_exact,
                    checkpoint_root=pathlib.Path(state_root) / "checkpoints",
                    output_root=overlay.DEFAULT_OUTPUT_ROOT)
            state = _advance(
                state, now=now, new_state="OVERLAY_READY",
                resume_stage="IAM", overlay_ready_path=str(ready_path))
            _atomic_state(_queue_path(state_root, date), state)
            _purge_exact_cache(reader)
        except BaseException as exc:
            return _error_state(state, now=now, stage="OVERLAY", exc=exc)
    elif not ready_path.is_file() or ready_path.is_symlink():
        return _error_state(
            state, now=now, stage="OVERLAY",
            exc=PipelineError("OVERLAY_READY_INVALID", str(ready_path)))

    try:
        capability = load_capability(capability_path)
    except BaseException as exc:
        return _error_state(state, now=now, stage="IAM", exc=exc)
    if capability is None:
        return _advance(
            state, now=now, new_state="WAITING_IAM", resume_stage="IAM",
            next_attempt=_utc(now + dt.timedelta(hours=1)),
            aws_write_state="NOT_AUTHORIZED",
            error={
                "stage": "IAM", "code": "WAITING_IAM",
                "detail": str(pathlib.Path(capability_path).absolute())})
    if publisher is None:
        return _advance(
            state, now=now, new_state="WAITING_PUBLISHER_CREDENTIALS",
            resume_stage="TAG_AND_PUBLISH",
            next_attempt=_utc(now + dt.timedelta(hours=1)),
            aws_write_state="IAM_ATTESTED_NO_CREDENTIAL_ADAPTER",
            error={
                "stage": "TAG_AND_PUBLISH",
                "code": "WAITING_PUBLISHER_CREDENTIALS",
                "detail": "exact tag/publish adapter is not installed"})
    try:
        proof = publisher(ready_path, capability)
        if (not isinstance(proof, dict)
                or proof.get("state") != PUBLISHED_STATE
                or proof.get("date") != date
                or proof.get("exact_version_tag_readback") is not True
                or proof.get("manifest_exact_version_verified") is not True):
            _fail("PUBLICATION_PROOF_INVALID", date)
    except BaseException as exc:
        return _error_state(
            state, now=now, stage="TAG_AND_PUBLISH", exc=exc)
    return _advance(
        state, now=now, new_state="PUBLISHED", resume_stage="COMPLETE",
        next_attempt="9999-12-31T23:59:59Z",
        publication_proof=proof, aws_write_state="EXACT_WRITES_VERIFIED")


def run(*, now: dt.datetime, transport: producer.AwsReadOnly,
        state_root: pathlib.Path = STATE_ROOT,
        base_status_root: pathlib.Path = BASE_STATUS_ROOT,
        capability_path: pathlib.Path = CAPABILITY_PATH,
        publisher: Publisher | None = None) -> dict[str, Any]:
    state_root = pathlib.Path(state_root).absolute()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_root / "pipeline.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("PIPELINE_ALREADY_RUNNING", str(lock_path))
        seed_queue(state_root, now)
        results = []
        for path, state in load_queue(state_root):
            updated = process_date(
                state, now=now, transport=transport,
                state_root=state_root, base_status_root=base_status_root,
                capability_path=capability_path, publisher=publisher)
            if updated != state:
                _atomic_state(path, updated)
            results.append({
                "date": updated["date"], "state": updated["state"],
                "resume_stage": updated["resume_stage"],
                "next_attempt_at_utc": updated["next_attempt_at_utc"],
            })
    finally:
        os.close(lock_fd)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "schema_version": "fresh-rfq-daily-pipeline-run-v1",
        "state": "DURABLE_DATE_QUEUE_UPDATED",
        "dates": results,
        "state_counts": counts,
        "aws_writes_by_coordinator": 0 if publisher is None else None,
        "lookback_expiry": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aws-cli", type=pathlib.Path,
                        default=producer.AWS_CLI)
    parser.add_argument("--state-root", type=pathlib.Path,
                        default=STATE_ROOT)
    parser.add_argument("--base-status-root", type=pathlib.Path,
                        default=BASE_STATUS_ROOT)
    parser.add_argument("--capability", type=pathlib.Path,
                        default=CAPABILITY_PATH)
    args = parser.parse_args(argv)
    try:
        result = run(
            now=dt.datetime.now(dt.timezone.utc),
            transport=producer.AwsReadOnly(args.aws_cli),
            state_root=args.state_root,
            base_status_root=args.base_status_root,
            capability_path=args.capability)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            f"FRESH_RFQ_DAILY_PIPELINE_BLOCKED "
            f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
            file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
