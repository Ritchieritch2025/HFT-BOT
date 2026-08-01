#!/usr/bin/env python3
"""Validated, atomic control-file protocol for the crypto MM prototype.

The control plane never calls an exchange API.  It only publishes a small
JSON document that the engine may read.  Runtime limits are bounded by
startup hard ceilings, and every accepted update carries a monotone revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


CONTROL_SCHEMA = "crypto-mm-control-v1"
STATUS_SCHEMA = "crypto-mm-control-status-v1"
CONTROL_FIELDS = {
    "schema_version",
    "revision",
    "updated_at",
    "max_open_cost",
    "clip",
    "max_net",
    "paused",
    "kill",
    "note",
}
PATCH_FIELDS = {"max_open_cost", "clip", "max_net", "paused", "kill", "note"}


class ControlError(ValueError):
    """A control document is malformed or exceeds a startup hard ceiling."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _plain_int(name: str, value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ControlError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _plain_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ControlError(f"{name} must be boolean")
    return value


def _finite_decimal(name: str, value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ControlError(f"{name} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ControlError(f"{name} must be numeric") from exc
    if not number.is_finite():
        raise ControlError(f"{name} must be finite")
    return number


def canonical_clip(value: object, *, hard_max_clip: Decimal) -> str:
    number = _finite_decimal("clip", value)
    if number < Decimal("0.01") or number > hard_max_clip:
        raise ControlError(f"clip must be in [0.01, {hard_max_clip}]")
    if number.as_tuple().exponent < -2:
        raise ControlError("clip supports at most two decimal places")
    return f"{number.quantize(Decimal('0.01')):.2f}"


def default_control(
    *,
    max_open_cost: float,
    clip: object,
    max_net: int,
    paused: bool,
) -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA,
        "revision": 0,
        "updated_at": utc_now(),
        "max_open_cost": float(max_open_cost),
        "clip": str(clip),
        "max_net": int(max_net),
        "paused": bool(paused),
        "kill": False,
        "note": "",
    }


def validate_control(
    value: object,
    *,
    hard_max_cost: float,
    hard_max_clip: object,
    hard_max_net: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlError("control document must be a JSON object")
    unknown = set(value) - CONTROL_FIELDS
    missing = CONTROL_FIELDS - set(value)
    if unknown:
        raise ControlError(f"unknown control fields: {','.join(sorted(unknown))}")
    if missing:
        raise ControlError(f"missing control fields: {','.join(sorted(missing))}")
    if value["schema_version"] != CONTROL_SCHEMA:
        raise ControlError(f"schema_version must be {CONTROL_SCHEMA}")

    revision = _plain_int("revision", value["revision"], 0, 2**63 - 1)
    updated_at = value["updated_at"]
    if not isinstance(updated_at, str) or not updated_at or len(updated_at) > 64:
        raise ControlError("updated_at must be a short timestamp string")

    hard_cost = _finite_decimal("hard_max_cost", hard_max_cost)
    cost = _finite_decimal("max_open_cost", value["max_open_cost"])
    if cost < 0 or cost > hard_cost:
        raise ControlError(f"max_open_cost must be in [0, {hard_cost}]")
    if cost.as_tuple().exponent < -2:
        raise ControlError("max_open_cost supports at most two decimal places")

    hard_clip = _finite_decimal("hard_max_clip", hard_max_clip)
    clip = canonical_clip(value["clip"], hard_max_clip=hard_clip)
    max_net = _plain_int("max_net", value["max_net"], 0, hard_max_net)
    paused = _plain_bool("paused", value["paused"])
    kill = _plain_bool("kill", value["kill"])
    note = value["note"]
    if not isinstance(note, str) or len(note) > 200:
        raise ControlError("note must be a string of at most 200 characters")
    if kill and not paused:
        raise ControlError("kill=true requires paused=true")

    return {
        "schema_version": CONTROL_SCHEMA,
        "revision": revision,
        "updated_at": updated_at,
        "max_open_cost": float(cost),
        "clip": clip,
        "max_net": max_net,
        "paused": paused,
        "kill": kill,
        "note": note,
    }


def next_control(
    current: Mapping[str, Any],
    patch: object,
    *,
    hard_max_cost: float,
    hard_max_clip: object,
    hard_max_net: int,
) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise ControlError("patch must be a JSON object")
    unknown = set(patch) - PATCH_FIELDS
    if unknown:
        raise ControlError(f"unknown patch fields: {','.join(sorted(unknown))}")
    if not patch:
        raise ControlError("patch must change at least one control field")

    candidate = dict(current)
    candidate.update(patch)
    candidate["schema_version"] = CONTROL_SCHEMA
    candidate["revision"] = int(current["revision"]) + 1
    candidate["updated_at"] = utc_now()
    return validate_control(
        candidate,
        hard_max_cost=hard_max_cost,
        hard_max_clip=hard_max_clip,
        hard_max_net=hard_max_net,
    )


def read_control(
    path: Path,
    *,
    hard_max_cost: float,
    hard_max_clip: object,
    hard_max_net: int,
) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read control document: {exc}") from exc
    return validate_control(
        value,
        hard_max_cost=hard_max_cost,
        hard_max_clip=hard_max_clip,
        hard_max_net=hard_max_net,
    )


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create/replace *path* atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_name = handle.name
            os.chmod(tmp_name, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support fsync on directories.  The file
            # replacement itself remains atomic.
            pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
