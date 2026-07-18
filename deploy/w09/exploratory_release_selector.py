#!/usr/bin/env python3
"""Select one explicit RFQ-free v3 release per date for MODE 1 research."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import research_reference


MODE = "MODE 1 / EXPLORATORY_AUTORESEARCH"
SCHEMA = "w09-exploratory-v3-release-selection-v1"
ACCEPTED_EVIDENCE_TIERS = frozenset({
    "SEALED_CONFIRMATION",
    "SEALED_DEGRADED_EVIDENCE",
})


class SelectionError(RuntimeError):
    """A fail-closed local selection error."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _load(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise SelectionError("unreadable manifest %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise SelectionError("manifest root is not an object: %s" % path)
    return value, hashlib.sha256(raw).hexdigest()


def _dates(start: str, end: str) -> list[str]:
    try:
        current = dt.date.fromisoformat(start)
        final = dt.date.fromisoformat(end)
    except ValueError as exc:
        raise SelectionError("date is not YYYY-MM-DD: %s" % exc) from exc
    if final < current:
        raise SelectionError("end date precedes start date")
    rows = []
    while current <= final:
        rows.append(current.isoformat())
        current += dt.timedelta(days=1)
    return rows


def select(
    cache: Path,
    *,
    start_date: str,
    end_date: str | None = None,
    require_contiguous: bool = True,
) -> dict[str, Any]:
    cache = Path(cache).resolve()
    manifests: dict[str, tuple[dict[str, Any], str]] = {}
    for area in ("reference_manifests", "releases"):
        root = cache / area
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/MANIFEST.json")):
            manifest, digest = _load(path)
            release_id = manifest.get("release_id")
            if not isinstance(release_id, str) or not release_id:
                continue
            previous = manifests.get(release_id)
            if previous is not None and previous[1] != digest:
                raise SelectionError(
                    "one release id has divergent cached manifests: %s" % release_id
                )
            manifests[release_id] = (manifest, digest)

    candidates: list[dict[str, Any]] = []
    for release_id, (manifest, digest) in sorted(manifests.items()):
        if not research_reference.is_reference_manifest(manifest):
            continue
        try:
            descriptor = research_reference.validate_manifest(manifest, release_id)
        except research_reference.ReferenceManifestError as exc:
            raise SelectionError(
                "v3 manifest contract rejected for %s: %s" % (release_id, exc)
            ) from exc
        tier = descriptor.get("evidence_tier")
        if tier not in ACCEPTED_EVIDENCE_TIERS:
            continue
        if descriptor.get("rfq_included") is not False:
            continue
        if descriptor["date"] < start_date:
            continue
        if end_date is not None and descriptor["date"] > end_date:
            continue
        candidates.append({
            "release_id": release_id,
            "date": descriptor["date"],
            "published_at_utc": descriptor["published_at_utc"],
            "evidence_tier": tier,
            "rfq_included": False,
            "manifest_sha256": digest,
            "reference_set_sha256": descriptor["reference_set_sha256"],
            "object_count": len(descriptor["objects"]),
            "object_bytes": sum(int(row["size"]) for row in descriptor["objects"]),
        })
    if not candidates:
        raise SelectionError("no RFQ-free MODE 1 v3 release is available")

    by_date: dict[str, dict[str, Any]] = {}
    for row in candidates:
        old = by_date.get(row["date"])
        rank = (row["published_at_utc"], row["release_id"])
        old_rank = (
            (old["published_at_utc"], old["release_id"])
            if old is not None
            else None
        )
        if old_rank is None or rank > old_rank:
            by_date[row["date"]] = row
    selected_end = end_date or max(by_date)
    requested_dates = _dates(start_date, selected_end)
    missing = [date for date in requested_dates if date not in by_date]
    if require_contiguous and missing:
        raise SelectionError(
            "contiguous release window has missing dates: %s" % ",".join(missing)
        )
    releases = [by_date[date] for date in requested_dates if date in by_date]
    if not releases:
        raise SelectionError("selected release window is empty")
    identity = {
        "schema_version": SCHEMA,
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "start_date": start_date,
        "end_date": selected_end,
        "require_contiguous": require_contiguous,
        "rfq": "OFF",
        "release_ids": [row["release_id"] for row in releases],
        "releases": releases,
    }
    identity["selection_sha256"] = hashlib.sha256(
        _canonical_bytes(identity)
    ).hexdigest()
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date")
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="explicitly permit missing dates (default is fail closed)",
    )
    args = parser.parse_args(argv)
    try:
        result = select(
            args.cache,
            start_date=args.start_date,
            end_date=args.end_date,
            require_contiguous=not args.allow_gaps,
        )
    except SelectionError as exc:
        print("W09_EXPLORATORY_SELECTION_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
