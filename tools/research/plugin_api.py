#!/usr/bin/env python3
"""Fail-closed API for hash-pinned Research Inbox method plugins.

Only a source file named in this built-in registry can be loaded.  Its exact
bytes are hashed before Python evaluates them, and its output is constrained
to a JSON-only planning payload.  Neither plugin IDs nor executable text are
accepted from a research plan as import paths or commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import types
from typing import Any, Callable, Mapping


REGISTRY_VERSION = "research-method-registry-v1"
REQUEST_SCHEMA = "research-execution-request-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_PLUGIN_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
FORBIDDEN_PAYLOAD_KEYS = {
    "argv",
    "command",
    "commands",
    "environment",
    "executable",
    "markdown",
    "plan_text",
    "script",
    "shell",
}


class PluginError(RuntimeError):
    """A method plugin was absent, changed, or violated its planning API."""


class PluginNotRegistered(PluginError):
    """A plan names no built-in method implementation."""


class PluginIntegrityError(PluginError):
    """Registered plugin source differs from its reviewed hash pin."""


class PluginContractError(PluginError):
    """A pinned plugin returned unsafe or malformed planning data."""


@dataclass(frozen=True)
class PluginRegistration:
    plugin_id: str
    plugin_version: str
    source_name: str
    source_sha256: str
    adapter_name: str
    payload_schema: str
    execution_class: str


@dataclass(frozen=True)
class LoadedPlugin:
    registration: PluginRegistration
    adapter: Callable[[dict[str, Any]], dict[str, Any]]


# The Deep03 digest is filled with the exact reviewed source SHA-256.  Changing
# even one byte of the adapter requires an explicit registry-pin update.
PLUGIN_REGISTRY: Mapping[str, PluginRegistration] = types.MappingProxyType({
    "deep03": PluginRegistration(
        plugin_id="deep03",
        plugin_version="1",
        source_name="deep03.py",
        source_sha256="46a0fb1425eaefc9c80a2e479f9db7217f528d6178bdb26d1c2fa0b295896b00",
        adapter_name="build_planning_payload",
        payload_schema="deep03-structured-execution-request-v1",
        execution_class="READONLY_EXPLORATORY",
    ),
})


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PluginContractError("plugin payload is not canonical JSON data") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def registration_for(
    plugin_id: Any,
    *,
    registry: Mapping[str, PluginRegistration] = PLUGIN_REGISTRY,
) -> PluginRegistration | None:
    if not isinstance(plugin_id, str) or not plugin_id:
        return None
    registration = registry.get(plugin_id)
    if registration is None or registration.plugin_id != plugin_id:
        return None
    return registration


def _source_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PluginIntegrityError("registered plugin source is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PluginIntegrityError("registered plugin source is not a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_PLUGIN_BYTES:
            raise PluginIntegrityError("registered plugin source size is invalid")
        raw = b""
        while len(raw) <= MAX_PLUGIN_BYTES:
            chunk = os.read(fd, min(256 * 1024, MAX_PLUGIN_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise PluginIntegrityError("registered plugin source changed while being read")
        return raw
    finally:
        os.close(fd)


def load_registered_plugin(
    plugin_id: Any,
    *,
    registry: Mapping[str, PluginRegistration] = PLUGIN_REGISTRY,
    plugin_root: Path | None = None,
) -> LoadedPlugin:
    """Load exactly one reviewed adapter after verifying its source bytes."""
    registration = registration_for(plugin_id, registry=registry)
    if registration is None:
        raise PluginNotRegistered("method plugin is not registered")
    if SHA256_RE.fullmatch(registration.source_sha256) is None:
        raise PluginIntegrityError("registered plugin SHA-256 pin is invalid")
    root = (plugin_root or (Path(__file__).resolve().parent / "plugins")).resolve()
    if Path(registration.source_name).name != registration.source_name:
        raise PluginIntegrityError("registered plugin source name is unsafe")
    source = root / registration.source_name
    if source.parent.resolve() != root:
        raise PluginIntegrityError("registered plugin escaped the plugin root")
    raw = _source_bytes(source)
    if hashlib.sha256(raw).hexdigest() != registration.source_sha256:
        raise PluginIntegrityError("registered plugin source SHA-256 mismatch")

    module = types.ModuleType(
        "_research_plugin_%s_%s" % (registration.plugin_id, registration.source_sha256[:12])
    )
    module.__file__ = str(source)
    module.__package__ = "research.plugins"
    try:
        code = compile(raw, str(source), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except Exception as exc:
        raise PluginIntegrityError("registered plugin could not be loaded") from exc
    descriptor = getattr(module, "PLUGIN_DESCRIPTOR", None)
    expected_descriptor = {
        "schema_version": "research-method-plugin-v1",
        "plugin_id": registration.plugin_id,
        "plugin_version": registration.plugin_version,
        "payload_schema": registration.payload_schema,
        "execution_class": registration.execution_class,
    }
    if descriptor != expected_descriptor:
        raise PluginContractError("plugin descriptor differs from registry")
    adapter = getattr(module, registration.adapter_name, None)
    if not callable(adapter):
        raise PluginContractError("registered plugin adapter is missing")
    return LoadedPlugin(registration=registration, adapter=adapter)


def _reject_executable_fields(value: Any, path: str = "plugin_payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PluginContractError("plugin payload keys must be text")
            if key.lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise PluginContractError("plugin payload contains executable field: %s" % path)
            _reject_executable_fields(child, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_executable_fields(child, "%s[%d]" % (path, index))


def build_execution_request(
    loaded: LoadedPlugin,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Wrap one plugin's JSON planning payload in the common safe envelope."""
    if not isinstance(context, dict):
        raise PluginContractError("plugin context must be an object")
    required = {
        "job_id",
        "plan_sha256",
        "job_spec_sha256",
        "catalog_sha256",
        "data_selection",
        "preflight",
        "job_spec",
        "automatic_execution_requested",
    }
    if set(context) != required:
        raise PluginContractError("plugin context fields differ from the fixed API")
    if context["preflight"].get("state") != "READY":
        raise PluginContractError("execution request requires a READY preflight")
    if context["data_selection"].get("state") != "SELECTED":
        raise PluginContractError("execution request requires an exact data selection")
    if context["job_spec"].get("plugin_id") != loaded.registration.plugin_id:
        raise PluginContractError("job plugin differs from loaded plugin")
    try:
        payload = loaded.adapter(context)
    except PluginError:
        raise
    except Exception as exc:
        raise PluginContractError("plugin adapter failed during planning") from exc
    if not isinstance(payload, dict):
        raise PluginContractError("plugin adapter must return an object")
    if len(canonical_bytes(payload)) > MAX_PAYLOAD_BYTES:
        raise PluginContractError("plugin planning payload is too large")
    _reject_executable_fields(payload)
    if payload.get("schema_version") != loaded.registration.payload_schema:
        raise PluginContractError("plugin payload schema differs from registry")
    if payload.get("plugin_id") != loaded.registration.plugin_id:
        raise PluginContractError("plugin payload identity differs from registry")
    if payload.get("plugin_version") != loaded.registration.plugin_version:
        raise PluginContractError("plugin payload version differs from registry")

    request: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA,
        "state": "PLANNED_AWAITING_AUTHORITY",
        "job_id": context["job_id"],
        "plan_sha256": context["plan_sha256"],
        "job_spec_sha256": context["job_spec_sha256"],
        "catalog_sha256": context["catalog_sha256"],
        "selection_sha256": context["data_selection"]["selection_sha256"],
        "preflight_sha256": context["preflight"]["preflight_sha256"],
        "plugin_registry_version": REGISTRY_VERSION,
        "plugin_id": loaded.registration.plugin_id,
        "plugin_version": loaded.registration.plugin_version,
        "plugin_source_sha256": loaded.registration.source_sha256,
        "execution_class": loaded.registration.execution_class,
        "automatic_execution_requested": bool(context["automatic_execution_requested"]),
        "plugin_payload": payload,
        "planning_only": True,
        "research_execution_started": False,
        "downstream_authority_required": True,
        "downstream_one_shot_gate_required": True,
        "arbitrary_plan_code_execution": False,
    }
    request["execution_request_sha256"] = canonical_sha256(request)
    return request


__all__ = [
    "LoadedPlugin",
    "PLUGIN_REGISTRY",
    "PluginContractError",
    "PluginError",
    "PluginIntegrityError",
    "PluginNotRegistered",
    "PluginRegistration",
    "REGISTRY_VERSION",
    "build_execution_request",
    "load_registered_plugin",
    "registration_for",
]
