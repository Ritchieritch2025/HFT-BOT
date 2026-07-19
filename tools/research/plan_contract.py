#!/usr/bin/env python3
"""Compile a Markdown research request into a non-executable job spec.

Markdown is data, never code.  A plan becomes runnable only when it selects a
registered, hash-pinned method plugin.  Unknown methods remain useful intake
records in NEEDS_METHOD state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA = "research-job-spec-v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_SPEND_USD = 15.0
MAX_RUNTIME_SECONDS = 86400
DEEP03_PLAN_SHA256 = "ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36"
REGISTERED_PLUGINS = {
    "deep03": {
        "aliases": {"deep03", "d3-w2a", "deep03-open-discovery"},
        "execution_class": "READONLY_EXPLORATORY",
        # The audited 8-day .03 window has L1/trades/market graph throughout
        # and L2 from 2026-07-12 onward.  L2 enriches the estimable methods but
        # cannot be a per-day hard gate for the authorized 07-10..17 release.
        "default_required": ["L1", "TRADES", "MARKET_GRAPH"],
        "default_optional": ["L2"],
        "default_forbidden": ["RFQ"],
    }
}
FAMILY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk-proj-|sk-)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+]{32,}"),
)


class PlanContractError(ValueError):
    """Plan bytes cannot safely enter the Research Inbox."""


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        raise PlanContractError("YAML frontmatter is not closed")
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise PlanContractError("YAML frontmatter requires PyYAML") from exc
    try:
        value = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise PlanContractError("YAML frontmatter is invalid") from exc
    if not isinstance(value, dict):
        raise PlanContractError("YAML frontmatter root must be an object")
    if len(value) > 64:
        raise PlanContractError("YAML frontmatter has too many fields")
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise PlanContractError("YAML frontmatter keys must be text")
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, (dt.date, dt.datetime)):
            return item.isoformat()
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise PlanContractError("YAML frontmatter must contain JSON-safe values")

    value = normalize(value)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PlanContractError("YAML frontmatter must contain JSON-safe values") from exc
    return value, "\n".join(lines[closing + 1 :])


def _title(metadata: dict[str, Any], body: str, filename: str) -> str:
    value = metadata.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()[:240]
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()[:240]
    return Path(filename or "PLAN.md").stem[:240] or "Untitled research"


def _plugin(metadata: dict[str, Any], plan_sha256: str) -> tuple[str | None, str | None]:
    requested = metadata.get("plugin_id", metadata.get("plugin", metadata.get("method")))
    if requested is not None and not isinstance(requested, str):
        raise PlanContractError("plugin_id must be text")
    requested = requested.strip().lower() if isinstance(requested, str) else None
    if plan_sha256 == DEEP03_PLAN_SHA256 and requested is None:
        return "deep03", "deep03"
    if requested:
        for plugin_id, plugin in REGISTERED_PLUGINS.items():
            if requested == plugin_id or requested in plugin["aliases"]:
                return plugin_id, requested
    return None, requested


def _family_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PlanContractError("%s data families must be a list of names" % label)
    values = [item.strip().upper() for item in value]
    if any(FAMILY_RE.fullmatch(item) is None for item in values):
        raise PlanContractError("%s contains an invalid data family" % label)
    if len(values) != len(set(values)):
        raise PlanContractError("%s contains duplicate data families" % label)
    return values


def _data_requirements(metadata: dict[str, Any], plugin_id: str | None) -> dict[str, Any]:
    data = metadata.get("data") or metadata.get("data_requirements") or {}
    if not isinstance(data, dict):
        raise PlanContractError("data requirements must be an object")
    defaults = REGISTERED_PLUGINS.get(plugin_id or "", {})
    required = _family_list(data.get("required"), "required")
    optional = _family_list(data.get("optional"), "optional")
    forbidden = _family_list(data.get("forbidden"), "forbidden")
    if not required and plugin_id:
        required = list(defaults["default_required"])
    if not optional and plugin_id:
        optional = [
            family
            for family in defaults["default_optional"]
            if family not in required and family not in forbidden
        ]
    if not forbidden and plugin_id:
        forbidden = list(defaults["default_forbidden"])
    overlap = (set(required) & set(optional)) | (set(required) & set(forbidden)) | (
        set(optional) & set(forbidden)
    )
    if overlap:
        raise PlanContractError("data family cannot be required/optional/forbidden together")
    selector = data.get("selector", "AUTO_AVAILABLE_CONTIGUOUS")
    if selector not in {"AUTO_AVAILABLE_CONTIGUOUS", "EXACT_RELEASE_IDS"}:
        raise PlanContractError("unsupported data selector")
    return {
        "required": required,
        "optional": optional,
        "forbidden": forbidden,
        "selector": selector,
        "zero_copy": True,
        "exact_version_required": True,
    }


def _date_window(metadata: dict[str, Any]) -> dict[str, str]:
    value = metadata.get("date_window") or {}
    if not isinstance(value, dict):
        raise PlanContractError("date_window must be an object")
    start = value.get("start", "AUTO")
    end = value.get("end", "AUTO")
    for label, item in (("start", start), ("end", end)):
        if item != "AUTO":
            if not isinstance(item, (str, dt.date)):
                raise PlanContractError("date_window.%s is invalid" % label)
            item = item.isoformat() if isinstance(item, dt.date) else item
            if DATE_RE.fullmatch(item) is None:
                raise PlanContractError("date_window.%s must be YYYY-MM-DD or AUTO" % label)
        if label == "start":
            start = item
        else:
            end = item
    if start != "AUTO" and end != "AUTO" and start > end:
        raise PlanContractError("date_window start is after end")
    return {"start": start, "end": end}


def _budget(metadata: dict[str, Any]) -> dict[str, Any]:
    value = metadata.get("budget") or {}
    if not isinstance(value, dict):
        raise PlanContractError("budget must be an object")
    runtime = value.get("max_runtime_seconds", MAX_RUNTIME_SECONDS)
    spend = value.get("max_spend_usd", MAX_SPEND_USD)
    if not isinstance(runtime, int) or isinstance(runtime, bool) or not 1 <= runtime <= MAX_RUNTIME_SECONDS:
        raise PlanContractError("max_runtime_seconds must be in [1,86400]")
    if not isinstance(spend, (int, float)) or isinstance(spend, bool) or not 0 < spend <= MAX_SPEND_USD:
        raise PlanContractError("max_spend_usd must be in (0,15]")
    return {"max_runtime_seconds": runtime, "max_spend_usd": float(spend)}


def compile_plan(plan_text: str, filename: str = "PLAN.md") -> dict[str, Any]:
    """Return the canonical safe job spec for exact Markdown bytes."""
    if not isinstance(plan_text, str) or not plan_text.strip():
        raise PlanContractError("research plan is empty")
    try:
        raw = plan_text.encode("utf-8")
    except UnicodeError as exc:
        raise PlanContractError("research plan is not valid UTF-8") from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanContractError("research plan exceeds 2 MiB")
    if "\x00" in plan_text:
        raise PlanContractError("research plan contains a NUL byte")
    if any(pattern.search(plan_text) for pattern in SECRET_PATTERNS):
        raise PlanContractError("research plan appears to contain a credential or private key")

    metadata, body = _frontmatter(plan_text)
    plan_sha = hashlib.sha256(raw).hexdigest()
    plugin_id, requested_plugin = _plugin(metadata, plan_sha)
    requirements = _data_requirements(metadata, plugin_id)
    date_window = _date_window(metadata)
    # This adopted Deep03 release was audited for exactly 2026-07-10..17.
    # Its Markdown predates Research Inbox front matter; treating the absent
    # field as AUTO would silently broaden the one-shot input on a later day.
    if plan_sha == DEEP03_PLAN_SHA256 and "date_window" not in metadata:
        date_window = {"start": "2026-07-10", "end": "2026-07-17"}
    state = "READY" if plugin_id else "NEEDS_METHOD"
    return {
        "schema_version": SCHEMA,
        "state": state,
        "plan_sha256": plan_sha,
        "plan_bytes": len(raw),
        "source_filename": Path(filename or "PLAN.md").name,
        "title": _title(metadata, body, filename),
        "objective": metadata.get("objective"),
        "plugin_id": plugin_id,
        "requested_plugin_id": requested_plugin,
        "plugin_registry_version": "research-method-registry-v1",
        "execution_class": "READONLY_EXPLORATORY",
        "data_requirements": requirements,
        "date_window": date_window,
        "budget": _budget(metadata),
        "report_requirements": metadata.get(
            "report", ["SUMMARY", "METHOD", "RESULTS", "LIMITATIONS", "RECEIPTS"]
        ),
        "arbitrary_plan_code_execution": False,
        "safety": {
            "s3_write": False,
            "production_mutation": False,
            "trading_credentials": False,
            "orders": False,
            "external_paid_api": False,
            "w09_isolated_execution": True,
        },
    }
