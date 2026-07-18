#!/usr/bin/env python3
"""Bounded, durable selector for historical days missing a write-once seal.

The tool deliberately does not create, repair, invalidate, or verify seals.
It only inventories completed raw-day directories, atomically leases a small
number of eligible dates to the existing supervisor seal chain, and records
per-day retry state.  The supervisor remains the sole owner of the export and
seal safety gates.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


SCHEMA_VERSION = 1
DAY_DIR_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
FIREHOSE_RE = re.compile(r"^firehose_\d{2}\.ndjson(?:\.\d+)?$")
UTC = dt.timezone.utc


def utc_text(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(UTC)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC)


def parse_day(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def state_lock(state_path: Path) -> Iterable[None]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def new_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "claims_by_utc_day": {},
        "days": {},
    }


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return new_state()
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"refusing to replace unreadable state {path}: {exc}")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported state schema in {path}: "
            f"{state.get('schema_version')!r}"
        )
    if not isinstance(state.get("days"), dict):
        raise RuntimeError(f"invalid days map in {path}")
    if not isinstance(state.get("claims_by_utc_day"), dict):
        raise RuntimeError(f"invalid claims map in {path}")
    return state


def seal_path(seal_root: Path, day: str) -> Path:
    return seal_root / f"date={day}.json"


def raw_facts(raw_root: Path, day: str) -> tuple[bool, int]:
    day_dir = raw_root / f"date={day}"
    if not day_dir.is_dir():
        return False, 0
    count = 0
    try:
        for item in day_dir.iterdir():
            if item.is_file() and FIREHOSE_RE.match(item.name):
                count += 1
    except OSError:
        return False, 0
    return True, count


def discover_days(
    raw_root: Path,
    today: dt.date,
    not_before: dt.date,
    lookback_days: int,
) -> List[str]:
    """Return completed historical raw-day directories in authorized scope.

    Yesterday stays on the normal supervisor path.  A missed day becomes a
    backlog candidate only after it is at least two UTC dates old.
    """
    lower = max(not_before, today - dt.timedelta(days=lookback_days))
    upper = today - dt.timedelta(days=2)
    if upper < lower or not raw_root.is_dir():
        return []
    found: List[str] = []
    for item in raw_root.iterdir():
        match = DAY_DIR_RE.match(item.name)
        if not match or not item.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if lower <= day <= upper:
            found.append(day.isoformat())
    return sorted(set(found))


def as_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def refresh_state(
    state: Dict[str, Any],
    *,
    raw_root: Path,
    seal_root: Path,
    now: dt.datetime,
    not_before: dt.date,
    lookback_days: int,
) -> None:
    now_text = utc_text(now)
    today = now.date()
    discovered = discover_days(raw_root, today, not_before, lookback_days)
    days: Dict[str, Dict[str, Any]] = state["days"]

    for day in discovered:
        entry = days.setdefault(
            day,
            {
                "date": day,
                "first_observed_utc": now_text,
                "attempts_total": 0,
                "attempts_by_utc_day": {},
                "consecutive_failures": 0,
            },
        )
        entry["last_observed_utc"] = now_text

    # Once discovered, an unresolved date remains visible even after it ages
    # out of the scan window or its raw directory disappears.  It can only be
    # resolved by an exact seal appearing, never by a newer day's success.
    for day, entry in list(days.items()):
        try:
            parsed_day = dt.date.fromisoformat(day)
        except ValueError:
            raise RuntimeError(f"invalid date key in state: {day!r}")
        if parsed_day < not_before or parsed_day >= today - dt.timedelta(days=1):
            # Do not let a configuration change claim an unauthorized or
            # incomplete day.  Preserve its history, but mark it out of scope.
            entry["status"] = "OUT_OF_SCOPE"
            continue

        raw_present, firehose_files = raw_facts(raw_root, day)
        exact_seal = seal_path(seal_root, day)
        entry["raw_path"] = str(raw_root / f"date={day}")
        entry["seal_path"] = str(exact_seal)
        entry["raw_present"] = raw_present
        entry["firehose_files"] = firehose_files
        entry["seal_present"] = exact_seal.is_file()
        entry["last_observed_utc"] = now_text

        if exact_seal.is_file():
            if entry.get("status") != "SEALED":
                entry["resolved_at_utc"] = now_text
            entry["status"] = "SEALED"
            entry["lease_until_utc"] = None
            entry["next_retry_utc"] = None
            continue
        entry.pop("resolved_at_utc", None)
        if not raw_present:
            entry["status"] = "RAW_MISSING"
            continue
        if firehose_files < 1:
            entry["status"] = "NO_FIREHOSE_RAW"
            continue

        lease_until = as_utc(entry.get("lease_until_utc"))
        if lease_until and lease_until > now:
            entry["status"] = "CLAIMED"
            continue
        entry["lease_until_utc"] = None
        retry_at = as_utc(entry.get("next_retry_utc"))
        if retry_at and retry_at > now:
            entry["status"] = "RETRY_WAIT"
        else:
            entry["status"] = "MISSING_SEAL"

    # Bound daily counters without deleting unresolved per-date history.
    keep_after = today - dt.timedelta(days=14)
    state["claims_by_utc_day"] = {
        day: count
        for day, count in state["claims_by_utc_day"].items()
        if _safe_day(day) is not None and _safe_day(day) >= keep_after
    }
    for entry in days.values():
        attempts = entry.get("attempts_by_utc_day", {})
        if not isinstance(attempts, dict):
            attempts = {}
        entry["attempts_by_utc_day"] = {
            day: count
            for day, count in attempts.items()
            if _safe_day(day) is not None and _safe_day(day) >= keep_after
        }

    state["authorization"] = {"not_before": not_before.isoformat()}
    state["discovery"] = {"lookback_days": lookback_days}
    state["updated_at_utc"] = now_text


def _safe_day(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def unresolved_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    active = {
        "MISSING_SEAL",
        "CLAIMED",
        "RETRY_WAIT",
        "RAW_MISSING",
        "NO_FIREHOSE_RAW",
    }
    return [
        entry
        for _, entry in sorted(state["days"].items())
        if entry.get("status") in active
    ]


def write_alarm(alarm_path: Path, state: Dict[str, Any], now: dt.datetime) -> None:
    unresolved = unresolved_entries(state)
    if not unresolved:
        try:
            alarm_path.unlink()
        except FileNotFoundError:
            pass
        return

    first = min(entry["first_observed_utc"] for entry in unresolved)
    if alarm_path.exists():
        try:
            with alarm_path.open(encoding="utf-8") as handle:
                prior = json.load(handle)
            first = prior.get("first_observed_utc", first)
        except (OSError, json.JSONDecodeError):
            # The durable state remains authoritative.  Replace a corrupt
            # derived alarm from it rather than losing the unresolved dates.
            pass
    summaries = []
    for entry in unresolved:
        summaries.append(
            {
                "date": entry["date"],
                "status": entry["status"],
                "attempts_total": int(entry.get("attempts_total", 0)),
                "last_attempt_utc": entry.get("last_attempt_utc"),
                "next_retry_utc": entry.get("next_retry_utc"),
                "last_error": entry.get("last_error"),
            }
        )
    atomic_json(
        alarm_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "UNSEALED_BACKLOG",
            "first_observed_utc": first,
            "observed_at_utc": utc_text(now),
            "unresolved_count": len(summaries),
            "unresolved_dates": [item["date"] for item in summaries],
            "days": summaries,
        },
    )


def save_all(
    state_path: Path,
    alarm_path: Path,
    state: Dict[str, Any],
    now: dt.datetime,
) -> None:
    atomic_json(state_path, state)
    write_alarm(alarm_path, state, now)


def common_refresh(args: argparse.Namespace, state: Dict[str, Any], now: dt.datetime) -> None:
    refresh_state(
        state,
        raw_root=Path(args.raw_root),
        seal_root=Path(args.seal_root),
        now=now,
        not_before=parse_day(args.not_before, "--not-before"),
        lookback_days=args.lookback_days,
    )


def claim(args: argparse.Namespace) -> int:
    now = parse_now(args.now)
    state_path, alarm_path = Path(args.state), Path(args.alarm)
    with state_lock(state_path):
        state = load_state(state_path)
        common_refresh(args, state, now)
        utc_day = now.date().isoformat()
        claimed_today = int(state["claims_by_utc_day"].get(utc_day, 0))
        remaining = max(0, args.max_per_day - claimed_today)
        limit = min(args.max_per_cycle, remaining)
        candidates = [
            entry
            for entry in unresolved_entries(state)
            if entry.get("status") == "MISSING_SEAL"
        ]
        candidates.sort(key=lambda entry: entry["date"])
        selected = candidates[:limit]
        for entry in selected:
            entry["status"] = "CLAIMED"
            entry["last_attempt_utc"] = utc_text(now)
            entry["lease_until_utc"] = utc_text(
                now + dt.timedelta(seconds=args.lease_seconds)
            )
            entry["attempts_total"] = int(entry.get("attempts_total", 0)) + 1
            attempts = entry.setdefault("attempts_by_utc_day", {})
            attempts[utc_day] = int(attempts.get(utc_day, 0)) + 1
        if selected:
            state["claims_by_utc_day"][utc_day] = claimed_today + len(selected)
        state["limits"] = {
            "max_per_cycle": args.max_per_cycle,
            "max_per_utc_day": args.max_per_day,
            "lease_seconds": args.lease_seconds,
        }
        state["updated_at_utc"] = utc_text(now)
        save_all(state_path, alarm_path, state, now)
    for entry in selected:
        print(entry["date"])
    return 0


def record(args: argparse.Namespace) -> int:
    now = parse_now(args.now)
    state_path, alarm_path = Path(args.state), Path(args.alarm)
    with state_lock(state_path):
        state = load_state(state_path)
        if args.date not in state["days"]:
            raise RuntimeError(f"date {args.date} was never claimed")
        entry = state["days"][args.date]
        entry["lease_until_utc"] = None
        if args.result == "succeeded":
            exact_seal = seal_path(Path(args.seal_root), args.date)
            if not exact_seal.is_file():
                args.result = "failed"
                args.error = "SUCCESS_REPORTED_WITHOUT_EXACT_SEAL"
            else:
                entry["status"] = "SEALED"
                entry["seal_present"] = True
                entry["last_success_utc"] = utc_text(now)
                entry["resolved_at_utc"] = utc_text(now)
                entry["next_retry_utc"] = None
                entry["last_error"] = None
                entry["consecutive_failures"] = 0
        if args.result == "failed":
            failures = int(entry.get("consecutive_failures", 0)) + 1
            delay = min(
                args.retry_max_seconds,
                args.retry_base_seconds * (2 ** min(failures - 1, 10)),
            )
            entry["status"] = "RETRY_WAIT"
            entry["consecutive_failures"] = failures
            entry["last_failure_utc"] = utc_text(now)
            entry["next_retry_utc"] = utc_text(now + dt.timedelta(seconds=delay))
            entry["last_error"] = (args.error or "seal chain failed")[:4096]

        common_refresh(args, state, now)
        state["updated_at_utc"] = utc_text(now)
        save_all(state_path, alarm_path, state, now)
    return 0


def status(args: argparse.Namespace) -> int:
    now = parse_now(args.now)
    state_path, alarm_path = Path(args.state), Path(args.alarm)
    with state_lock(state_path):
        state = load_state(state_path)
        common_refresh(args, state, now)
        save_all(state_path, alarm_path, state, now)
    json.dump(state, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--seal-root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--alarm", required=True)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--lookback-days", type=positive_int, required=True)
    parser.add_argument("--now", help="test/operator override; ISO-8601 UTC")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    claim_parser = commands.add_parser("claim", help="lease bounded candidate dates")
    add_common(claim_parser)
    claim_parser.add_argument("--max-per-cycle", type=positive_int, required=True)
    claim_parser.add_argument("--max-per-day", type=positive_int, required=True)
    claim_parser.add_argument("--lease-seconds", type=positive_int, required=True)
    claim_parser.set_defaults(func=claim)

    record_parser = commands.add_parser("record", help="persist a claimed result")
    add_common(record_parser)
    record_parser.add_argument("--date", required=True)
    record_parser.add_argument("--result", choices=("succeeded", "failed"), required=True)
    record_parser.add_argument("--error")
    record_parser.add_argument("--retry-base-seconds", type=positive_int, required=True)
    record_parser.add_argument("--retry-max-seconds", type=positive_int, required=True)
    record_parser.set_defaults(func=record)

    status_parser = commands.add_parser("status", help="refresh and print durable state")
    add_common(status_parser)
    status_parser.set_defaults(func=status)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"seal_backlog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
