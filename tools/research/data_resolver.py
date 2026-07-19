#!/usr/bin/env python3
"""Resolve one research job to deterministic, exact local V3 releases."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

try:
    from .data_catalog import canonical_sha256
except ImportError:  # pragma: no cover - supports direct script loading on W09
    from data_catalog import canonical_sha256


SELECTION_SCHEMA = "research-data-selection-v1"
PREFLIGHT_SCHEMA = "research-data-preflight-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DataResolverError(ValueError):
    """The job or catalog does not satisfy the resolver contract."""


def _families(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DataResolverError(f"{label} must be a list of data families")
    normalized = [item.strip().upper() for item in value]
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        raise DataResolverError(f"{label} contains invalid or duplicate families")
    return normalized


def _catalog_contract(catalog: dict[str, Any]) -> None:
    if not isinstance(catalog, dict) or catalog.get("schema_version") != "research-data-catalog-v1":
        raise DataResolverError("unsupported data catalog")
    digest = catalog.get("catalog_sha256")
    unsigned = {key: value for key, value in catalog.items() if key != "catalog_sha256"}
    if not isinstance(digest, str) or digest != canonical_sha256(unsigned):
        raise DataResolverError("data catalog digest mismatch")
    if catalog.get("network_reads") != 0 or catalog.get("copied_bytes") != 0:
        raise DataResolverError("data catalog is not zero-copy/local-only")
    if not isinstance(catalog.get("releases"), list):
        raise DataResolverError("data catalog releases are invalid")


def _date(value: Any, label: str) -> str:
    if value == "AUTO":
        return value
    if not isinstance(value, str):
        raise DataResolverError(f"{label} is invalid")
    try:
        if dt.date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise DataResolverError(f"{label} must be YYYY-MM-DD or AUTO") from exc
    return value


def _days(start: str, end: str) -> list[str]:
    current = dt.date.fromisoformat(start)
    final = dt.date.fromisoformat(end)
    values = []
    while current <= final:
        values.append(current.isoformat())
        current += dt.timedelta(days=1)
    return values


def _terminal_selection(
    *,
    state: str,
    job_spec: dict[str, Any],
    catalog: dict[str, Any],
    required: list[str],
    optional: list[str],
    forbidden: list[str],
    selector: str,
    start: str,
    end: str,
    reasons: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection: dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA,
        "state": state,
        "plan_sha256": job_spec["plan_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "selector": selector,
        "requested_date_window": {"start": start, "end": end},
        "resolved_date_window": None,
        "required_families": required,
        "optional_families": optional,
        "forbidden_families": forbidden,
        "release_ids": [],
        "releases": [],
        "reasons": reasons,
        "exact_version_required": True,
        "zero_copy": True,
    }
    selection["selection_sha256"] = canonical_sha256(selection)
    preflight = _preflight(job_spec, catalog, selection)
    return selection, preflight


def _preflight(
    job_spec: dict[str, Any],
    catalog: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    ready = selection["state"] == "SELECTED"
    payload: dict[str, Any] = {
        "schema_version": PREFLIGHT_SCHEMA,
        "state": "READY" if ready else selection["state"],
        "plan_sha256": job_spec["plan_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "selection_sha256": selection["selection_sha256"],
        "release_count": len(selection["release_ids"]),
        "release_ids": list(selection["release_ids"]),
        "object_count": sum(int(row["object_count"]) for row in selection["releases"]),
        "object_bytes": sum(int(row["object_bytes"]) for row in selection["releases"]),
        "required_families": list(selection["required_families"]),
        "optional_families_available": sorted({
            family
            for row in selection["releases"]
            for family in selection["optional_families"]
            if family in row["data_families"]
        }),
        "optional_families_missing": sorted({
            family
            for family in selection["optional_families"]
            if any(family not in row["data_families"] for row in selection["releases"])
        }),
        "reasons": list(selection["reasons"]),
        "network_reads": 0,
        "copied_bytes": 0,
        "zero_copy": True,
        "research_execution_started": False,
    }
    payload["preflight_sha256"] = canonical_sha256(payload)
    return payload


def resolve_data(
    job_spec: dict[str, Any], catalog: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(DATA_SELECTION, PREFLIGHT)`` without reading any data bytes."""
    if not isinstance(job_spec, dict) or job_spec.get("schema_version") != "research-job-spec-v1":
        raise DataResolverError("unsupported research job spec")
    plan_sha = job_spec.get("plan_sha256")
    if not isinstance(plan_sha, str) or SHA256_RE.fullmatch(plan_sha) is None:
        raise DataResolverError("job plan SHA-256 is invalid")
    _catalog_contract(catalog)

    requirements = job_spec.get("data_requirements")
    if not isinstance(requirements, dict):
        raise DataResolverError("job data requirements are invalid")
    required = _families(requirements.get("required", []), "required")
    optional = _families(requirements.get("optional", []), "optional")
    forbidden = _families(requirements.get("forbidden", []), "forbidden")
    if (set(required) & set(optional)) or (set(required) & set(forbidden)) or (
        set(optional) & set(forbidden)
    ):
        raise DataResolverError("data family scopes overlap")
    selector = requirements.get("selector", "AUTO_AVAILABLE_CONTIGUOUS")
    date_window = job_spec.get("date_window") or {}
    if not isinstance(date_window, dict):
        raise DataResolverError("job date window is invalid")
    start = _date(date_window.get("start", "AUTO"), "date_window.start")
    end = _date(date_window.get("end", "AUTO"), "date_window.end")

    if catalog.get("state") in {"PARTIAL", "REFUSED"}:
        return _terminal_selection(
            state="REFUSED",
            job_spec=job_spec,
            catalog=catalog,
            required=required,
            optional=optional,
            forbidden=forbidden,
            selector=str(selector),
            start=start,
            end=end,
            reasons=[{
                "code": "CATALOG_INTEGRITY_NOT_CLOSED",
                "rejected_releases": list(catalog.get("rejected_releases") or []),
            }],
        )

    if job_spec.get("state") != "READY":
        return _terminal_selection(
            state="BLOCKED",
            job_spec=job_spec,
            catalog=catalog,
            required=required,
            optional=optional,
            forbidden=forbidden,
            selector=str(selector),
            start=start,
            end=end,
            reasons=[{"code": "METHOD_NOT_READY"}],
        )
    if selector != "AUTO_AVAILABLE_CONTIGUOUS":
        return _terminal_selection(
            state="BLOCKED",
            job_spec=job_spec,
            catalog=catalog,
            required=required,
            optional=optional,
            forbidden=forbidden,
            selector=str(selector),
            start=start,
            end=end,
            reasons=[{"code": "SELECTOR_NOT_IMPLEMENTED", "selector": selector}],
        )

    releases = list(catalog["releases"])
    available_dates = sorted({str(row.get("date")) for row in releases})
    resolved_start = start if start != "AUTO" else (available_dates[0] if available_dates else None)
    resolved_end = end if end != "AUTO" else (available_dates[-1] if available_dates else None)
    if resolved_start is None or resolved_end is None or resolved_start > resolved_end:
        return _terminal_selection(
            state="BLOCKED",
            job_spec=job_spec,
            catalog=catalog,
            required=required,
            optional=optional,
            forbidden=forbidden,
            selector=selector,
            start=start,
            end=end,
            reasons=[{"code": "NO_RELEASES_IN_WINDOW"}],
        )

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in releases:
        date = str(row.get("date"))
        if resolved_start <= date <= resolved_end:
            by_date.setdefault(date, []).append(row)

    selected: list[dict[str, Any]] = []
    for date in _days(resolved_start, resolved_end):
        candidates = by_date.get(date, [])
        if not candidates:
            return _terminal_selection(
                state="BLOCKED",
                job_spec=job_spec,
                catalog=catalog,
                required=required,
                optional=optional,
                forbidden=forbidden,
                selector=selector,
                start=start,
                end=end,
                reasons=[{"code": "DATE_MISSING", "date": date}],
            )
        clean = [
            row
            for row in candidates
            if not (set(row.get("data_families") or []) & set(forbidden))
        ]
        if not clean:
            present = sorted({
                family
                for row in candidates
                for family in forbidden
                if family in (row.get("data_families") or [])
            })
            return _terminal_selection(
                state="REFUSED",
                job_spec=job_spec,
                catalog=catalog,
                required=required,
                optional=optional,
                forbidden=forbidden,
                selector=selector,
                start=start,
                end=end,
                reasons=[{"code": "FORBIDDEN_FAMILY_PRESENT", "date": date, "families": present}],
            )
        eligible = [
            row
            for row in clean
            if set(required).issubset(set(row.get("data_families") or []))
        ]
        if not eligible:
            present = set().union(*(set(row.get("data_families") or []) for row in clean))
            return _terminal_selection(
                state="BLOCKED",
                job_spec=job_spec,
                catalog=catalog,
                required=required,
                optional=optional,
                forbidden=forbidden,
                selector=selector,
                start=start,
                end=end,
                reasons=[{
                    "code": "REQUIRED_FAMILY_MISSING",
                    "date": date,
                    "families": sorted(set(required) - present),
                }],
            )
        selected.append(max(
            eligible,
            key=lambda row: (row["published_at_utc"], row["release_id"]),
        ))

    release_rows = [
        {
            "release_id": row["release_id"],
            "date": row["date"],
            "manifest_sha256": row["manifest_sha256"],
            "manifest_version_id": row["manifest_version_id"],
            "evidence_tier": row["evidence_tier"],
            "data_families": list(row["data_families"]),
            "channels": list(row["channels"]),
            "object_count": row["object_count"],
            "object_bytes": row["object_bytes"],
            "rfq": dict(row["rfq"]),
        }
        for row in selected
    ]
    selection: dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA,
        "state": "SELECTED",
        "plan_sha256": plan_sha,
        "catalog_sha256": catalog["catalog_sha256"],
        "selector": selector,
        "requested_date_window": {"start": start, "end": end},
        "resolved_date_window": {"start": resolved_start, "end": resolved_end},
        "required_families": required,
        "optional_families": optional,
        "forbidden_families": forbidden,
        "release_ids": [row["release_id"] for row in release_rows],
        "releases": release_rows,
        "reasons": [],
        "exact_version_required": True,
        "zero_copy": True,
    }
    selection["selection_sha256"] = canonical_sha256(selection)
    return selection, _preflight(job_spec, catalog, selection)


__all__ = ["DataResolverError", "resolve_data"]
