#!/usr/bin/env python3
"""Produce runner-ready latency evidence from a private causal trace.

The private input is deliberately stricter than the public measured-latency
receipt.  It binds every action to request/response bytes, a current live
authority, and a fixed-point fill/position reconciliation.  The two emitted
receipts contain only hashed order references.

This module has no network or order API.  It never obtains credentials and
cannot transmit an order.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Mapping, Sequence

from .contracts import (
    canonical_json_bytes,
    canonical_sha256,
    require_int,
    require_nonempty,
    require_sha256,
)
from .latency_evidence import (
    ACTION_SEMANTICS,
    INVENTORY_SCHEMA_VERSION,
    LATENCY_AGGREGATE_READY,
    REAL_ORDER_MEASUREMENT,
    REQUIRED_PATHS,
    LatencyEvidenceInventory,
    RejectedArtifact,
    TraceCandidate,
    build_latency_audit_receipt,
)


PRIVATE_TRACE_SCHEMA_VERSION = "pnl-spine-private-causal-latency-trace-v1"
MEASURED_RECEIPT_SCHEMA_VERSION = "pnl-spine-measured-latency-receipt-v1"
PRODUCER_CONFIG_SCHEMA_VERSION = "pnl-spine-latency-probe-config-v2"
ENVIRONMENT_SCHEMA_VERSION = "pnl-spine-execution-environment-receipt-v1"
HOST_SCHEMA_VERSION = "pnl-spine-execution-host-receipt-v1"
CLOCK_SCHEMA_VERSION = "pnl-spine-clock-quality-receipt-v1"
PLACE_AUTHORITY_SCHEMA_VERSION = "pnl-spine-latency-probe-authority-v2"
IOC_AUTHORITY_SCHEMA_VERSION = "pnl-spine-ioc-exit-authority-v2"
PLACE_CONSUMPTION_SCHEMA_VERSION = (
    "pnl-spine-latency-authority-consumption-v2"
)
PLACE_TERMINAL_SCHEMA_VERSION = "pnl-spine-latency-authority-terminal-v2"
IOC_CONSUMPTION_SCHEMA_VERSION = (
    "pnl-spine-ioc-exit-authority-consumption-v2"
)
IOC_TERMINAL_SCHEMA_VERSION = "pnl-spine-ioc-exit-authority-terminal-v2"
PROMOTION_SCHEMA_VERSION = "pnl-spine-root-promotion-receipt-v1"
PROBE_CLOCK_ID = "STD_STEADY_CLOCK:probe-process"
MAX_CLOCK_AGE_MS = 5 * 60 * 1_000
MAX_FUTURE_SKEW_MS = 5 * 1_000

_ROOT_KEYS = frozenset(
    (
        "schema_version",
        "receipt_id",
        "measurement_mode",
        "clock_id",
        "measured_on",
        "created_at_ns",
        "created_at_wall_utc_ms",
        "clock_quality_receipt_sha256",
        "environment_fingerprint_sha256",
        "execution_host_fingerprint_sha256",
        "producer_code_sha256",
        "producer_config_sha256",
        "samples",
    )
)
_SAMPLE_KEYS = frozenset(
    (
        "path",
        "action_semantics",
        "clock_quality_receipt_sha256",
        "decision_ns",
        "sent_ns",
        "acknowledged_ns",
        "effective_ns",
        "order_ref_sha256",
        "request_sha256",
        "response_sha256",
        "source_event_sha256",
        "http_status",
        "live_authority_sha256",
        "matching_engine_ts_ms",
        "average_fee_paid_e6",
        "average_fill_price_e4",
        "book_side",
        "request_reduce_only",
        "requested_price_e4",
        "subaccount",
        "ticker_sha256",
        "time_in_force",
        "fill_reconciliation",
        "environment_fingerprint_sha256",
        "execution_host_fingerprint_sha256",
    )
)
_RECONCILIATION_KEYS = frozenset(
    (
        "requested_quantity_e4",
        "filled_quantity_e4",
        "canceled_quantity_e4",
        "remaining_quantity_e4",
        "position_before_e4",
        "position_after_e4",
        "reconciled",
        "reduce_only",
        "order_status",
    )
)


class MeasuredLatencyError(ValueError):
    """The private trace cannot support a real latency receipt."""


@dataclass(frozen=True)
class TrustedPublicationContext:
    now_wall_utc_ms: int
    producer_code_sha256: str
    producer_config_sha256: str
    environment_sha256: str
    host_sha256: str
    clock_sha256: str
    clock_id: str
    hostname: str
    instance_id: str
    machine_id_sha256: str
    ca_bundle_sha256: str
    execution_uid: int
    ticker: str
    ticker_sha256: str
    place_authority_sha256: str
    ioc_authority_sha256: str
    place_issued_at_ms: int
    place_expires_at_ms: int
    ioc_issued_at_ms: int
    ioc_expires_at_ms: int
    place_consumed_at_ms: int
    place_finished_at_ms: int
    ioc_consumed_at_ms: int
    ioc_finished_at_ms: int
    place_quantity_e4: int
    place_price_cap_e4: int
    ioc_expected_position_before_e4: int
    ioc_book_side: str
    ioc_outcome_side: str
    ioc_price_limit_semantics: str
    ioc_subaccount: int
    ioc_quantity_e4: int
    ioc_price_limit_e4: int
    cancel_risk_reduction_after_expiry: bool
    promoted_trace_sha256: str


def _absolute_no_symlink_path(
    path: Path | str,
    *,
    allow_missing_leaf: bool,
    label: str,
) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise MeasuredLatencyError(f"{label} path must be absolute")
    normalized = Path(os.path.normpath(os.fspath(candidate)))
    if normalized != candidate:
        raise MeasuredLatencyError(
            f"{label} path must be lexically normalized"
        )
    current = Path(candidate.anchor)
    components = candidate.parts[1:]
    for index, component in enumerate(components):
        current /= component
        is_leaf = index == len(components) - 1
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and is_leaf:
                break
            raise MeasuredLatencyError(
                f"{label} path component is missing: {current}"
            )
        except OSError as exc:
            raise MeasuredLatencyError(
                f"{label} path cannot be inspected safely"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise MeasuredLatencyError(
                f"{label} path may not traverse symlinks"
            )
    return candidate


def _require_root_control_boundary(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise MeasuredLatencyError(
                f"{label} root control boundary is unavailable"
            ) from exc
        if info.st_uid != 0 or stat.S_ISLNK(info.st_mode):
            raise MeasuredLatencyError(
                f"{label} must remain under a root-owned no-symlink path"
            )
        if current == path:
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
                raise MeasuredLatencyError(
                    f"{label} must be a root-owned read-only regular file"
                )
        elif not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise MeasuredLatencyError(
                f"{label} parent directories must be root-owned and "
                "not group/world writable"
            )


def _read_regular_bytes(
    path: Path | str,
    *,
    label: str,
    require_root_read_only: bool,
    maximum_bytes: int,
) -> bytes:
    artifact_path = _absolute_no_symlink_path(
        path, allow_missing_leaf=False, label=label
    )
    if require_root_read_only:
        _require_root_control_boundary(artifact_path, label=label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(artifact_path, flags)
    except OSError as exc:
        raise MeasuredLatencyError(
            f"cannot open {label} safely: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MeasuredLatencyError(f"{label} must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise MeasuredLatencyError(
                    f"{label} exceeds {maximum_bytes} bytes"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MeasuredLatencyError(f"{label} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_trace_bytes(
    path: Path | str,
    *,
    require_root_read_only: bool,
) -> bytes:
    return _read_regular_bytes(
        path,
        label="private trace",
        require_root_read_only=require_root_read_only,
        maximum_bytes=16 * 1024 * 1024,
    )


def _mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MeasuredLatencyError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise MeasuredLatencyError(f"{name} requires string keys")
    return value


def _exact_keys(
    name: str,
    value: Mapping[str, Any],
    expected: frozenset[str],
) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise MeasuredLatencyError(
            f"{name} keys differ; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _reject_float(_: str) -> object:
    raise MeasuredLatencyError("private trace forbids floating-point JSON")


def _reject_constant(value: str) -> object:
    raise MeasuredLatencyError(
        f"private trace forbids JSON constant {value}"
    )


def _no_duplicate_object(
    pairs: Sequence[tuple[str, Any]],
) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MeasuredLatencyError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _pinned_sha(name: str, actual: object, expected: str) -> str:
    try:
        actual_sha = require_sha256(name, actual)
        expected_sha = require_sha256(f"expected_{name}", expected)
    except ValueError as exc:
        raise MeasuredLatencyError(str(exc)) from exc
    if actual_sha != expected_sha:
        raise MeasuredLatencyError(f"{name} does not match external pin")
    return actual_sha


def _integer(name: str, value: object, *, minimum: int = 0) -> int:
    try:
        return require_int(name, value, minimum=minimum)
    except ValueError as exc:
        raise MeasuredLatencyError(str(exc)) from exc


def _string(name: str, value: object) -> str:
    try:
        return require_nonempty(name, value)
    except ValueError as exc:
        raise MeasuredLatencyError(str(exc)) from exc


def _sha(name: str, value: object) -> str:
    try:
        return require_sha256(name, value)
    except ValueError as exc:
        raise MeasuredLatencyError(str(exc)) from exc


def _validate_reconciliation(
    path: str,
    value: object,
    *,
    sample_index: int,
) -> None:
    name = f"samples[{sample_index}].fill_reconciliation"
    reconciliation = _mapping(name, value)
    _exact_keys(name, reconciliation, _RECONCILIATION_KEYS)
    requested = _integer(
        f"{name}.requested_quantity_e4",
        reconciliation["requested_quantity_e4"],
        minimum=1,
    )
    filled = _integer(
        f"{name}.filled_quantity_e4",
        reconciliation["filled_quantity_e4"],
    )
    canceled = _integer(
        f"{name}.canceled_quantity_e4",
        reconciliation["canceled_quantity_e4"],
    )
    remaining = _integer(
        f"{name}.remaining_quantity_e4",
        reconciliation["remaining_quantity_e4"],
    )
    before = _integer(
        f"{name}.position_before_e4",
        abs(reconciliation["position_before_e4"])
        if type(reconciliation["position_before_e4"]) is int
        else reconciliation["position_before_e4"],
    )
    after = _integer(
        f"{name}.position_after_e4",
        abs(reconciliation["position_after_e4"])
        if type(reconciliation["position_after_e4"]) is int
        else reconciliation["position_after_e4"],
    )
    raw_before = reconciliation["position_before_e4"]
    raw_after = reconciliation["position_after_e4"]
    if type(raw_before) is not int or type(raw_after) is not int:
        raise MeasuredLatencyError(f"{name} positions must be integers")
    if reconciliation["reconciled"] is not True:
        raise MeasuredLatencyError(f"{name}.reconciled must be true")
    if requested != filled + canceled + remaining:
        raise MeasuredLatencyError(
            f"{name} violates requested=filled+canceled+remaining"
        )
    status = _string(f"{name}.order_status", reconciliation["order_status"])

    if path == "PLACE":
        if reconciliation["reduce_only"] is not False:
            raise MeasuredLatencyError(f"{name} PLACE must not be reduce-only")
        if status != "RESTING":
            raise MeasuredLatencyError(f"{name} PLACE must end RESTING")
        if filled != 0 or canceled != 0 or remaining != requested:
            raise MeasuredLatencyError(
                f"{name} PLACE must be wholly resting; partially filled "
                "or canceled orders are not successful probes"
            )
        if raw_after != raw_before:
            raise MeasuredLatencyError(
                f"{name} PLACE changed position or partially filled"
            )
    elif path == "CANCEL":
        if reconciliation["reduce_only"] is not False:
            raise MeasuredLatencyError(f"{name} CANCEL must not be reduce-only")
        if status != "CANCELED":
            raise MeasuredLatencyError(f"{name} CANCEL must end CANCELED")
        if filled != 0 or canceled != requested or remaining != 0:
            raise MeasuredLatencyError(
                f"{name} CANCEL must cancel the full unfilled quantity"
            )
        if raw_after != raw_before:
            raise MeasuredLatencyError(
                f"{name} CANCEL changed position or partially filled"
            )
    elif path == "IOC_EXIT":
        if reconciliation["reduce_only"] is not True:
            raise MeasuredLatencyError(
                f"{name} IOC_EXIT must be position-reducing"
            )
        if status != "EXECUTED":
            raise MeasuredLatencyError(f"{name} IOC_EXIT must end EXECUTED")
        if before == 0:
            raise MeasuredLatencyError(
                f"{name} IOC_EXIT requires a pre-existing position"
            )
        if (
            filled != requested
            or canceled != 0
            or remaining != 0
            or after != 0
            or filled != before
            or raw_after != 0
        ):
            raise MeasuredLatencyError(
                f"{name} IOC_EXIT must fully fill and flatten the position"
            )


def _load_json_artifact(
    path: Path | str,
    *,
    label: str,
    require_root_read_only: bool,
) -> tuple[dict[str, Any], str, bytes]:
    raw = _read_regular_bytes(
        path,
        label=label,
        require_root_read_only=require_root_read_only,
        maximum_bytes=16 * 1024 * 1024,
    )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeasuredLatencyError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    document = dict(_mapping(label, value))
    if canonical_json_bytes(document) != raw:
        raise MeasuredLatencyError(f"{label} is not exact canonical JSON")
    return document, hashlib.sha256(raw).hexdigest(), raw


def _hash_artifact(
    path: Path | str,
    *,
    label: str,
    require_root_read_only: bool,
    maximum_bytes: int = 256 * 1024 * 1024,
) -> tuple[str, int]:
    raw = _read_regular_bytes(
        path,
        label=label,
        require_root_read_only=require_root_read_only,
        maximum_bytes=maximum_bytes,
    )
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _normalized_absolute_path_text(path: Path | str, *, label: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise MeasuredLatencyError(f"{label} must be absolute")
    normalized = Path(os.path.normpath(os.fspath(candidate)))
    if normalized != candidate:
        raise MeasuredLatencyError(f"{label} must be lexically normalized")
    return str(candidate)


def _document_time_ms(
    name: str,
    value: object,
    *,
    now_wall_utc_ms: int,
    allow_old: bool,
) -> int:
    timestamp = _integer(name, value, minimum=1)
    if timestamp > now_wall_utc_ms + MAX_FUTURE_SKEW_MS:
        raise MeasuredLatencyError(f"{name} is future-dated")
    if not allow_old and now_wall_utc_ms - timestamp > MAX_CLOCK_AGE_MS:
        raise MeasuredLatencyError(f"{name} is stale")
    return timestamp


def _validate_authority_window(
    document: Mapping[str, Any],
    *,
    label: str,
) -> tuple[int, int]:
    issued = _integer(
        f"{label}.issued_at_unix_s",
        document["issued_at_unix_s"],
        minimum=1,
    )
    expires = _integer(
        f"{label}.expires_at_unix_s",
        document["expires_at_unix_s"],
        minimum=1,
    )
    if expires <= issued or expires - issued > 15 * 60:
        raise MeasuredLatencyError(f"{label} time window is invalid")
    if document["single_use"] is not True:
        raise MeasuredLatencyError(f"{label} must be single-use")
    if _integer(
        f"{label}.max_attempts", document["max_attempts"], minimum=1
    ) != 1:
        raise MeasuredLatencyError(f"{label} max_attempts must be one")
    return issued * 1_000, expires * 1_000


def _transaction_id(
    authority_sha256: str,
    nonce_sha256: str,
    receipt_id: str,
) -> str:
    return hashlib.sha256(
        (authority_sha256 + nonce_sha256 + receipt_id).encode("ascii")
    ).hexdigest()


def _load_trusted_publication_context(
    *,
    private_trace_path: Path | str,
    private_trace_raw: bytes,
    private_trace: Mapping[str, Any],
    producer_binary_path: Path | str,
    producer_config_path: Path | str,
    environment_receipt_path: Path | str,
    execution_host_receipt_path: Path | str,
    clock_quality_receipt_path: Path | str,
    ca_bundle_path: Path | str,
    place_authority_path: Path | str,
    place_consumption_path: Path | str,
    place_terminal_path: Path | str,
    ioc_authority_path: Path | str,
    ioc_consumption_path: Path | str,
    ioc_terminal_path: Path | str,
    promotion_receipt_path: Path | str,
    require_root_read_only: bool,
    now_wall_utc_ms: int | None,
) -> TrustedPublicationContext:
    if now_wall_utc_ms is not None and require_root_read_only:
        raise MeasuredLatencyError(
            "production publisher forbids caller-supplied current time"
        )
    now_ms = (
        int(time.time_ns() // 1_000_000)
        if now_wall_utc_ms is None
        else _integer("now_wall_utc_ms", now_wall_utc_ms, minimum=1)
    )
    trace_sha = hashlib.sha256(private_trace_raw).hexdigest()
    binary_sha, _ = _hash_artifact(
        producer_binary_path,
        label="producer binary",
        require_root_read_only=require_root_read_only,
    )
    config, config_sha, _ = _load_json_artifact(
        producer_config_path,
        label="producer config",
        require_root_read_only=require_root_read_only,
    )
    _exact_keys(
        "producer config",
        config,
        frozenset(
            {
                "ca_bundle_path",
                "ca_bundle_sha256",
                "execution_gid",
                "execution_uid",
                "max_cash_loss_e6",
                "max_fee_e6",
                "place_post_only",
                "place_side",
                "place_time_in_force",
                "price_cap_e4",
                "quantity_e4",
                "schema_version",
            }
        ),
    )
    if config["schema_version"] != PRODUCER_CONFIG_SCHEMA_VERSION:
        raise MeasuredLatencyError("producer config schema is unsupported")
    if (
        config["place_post_only"] is not True
        or config["place_side"] != "BID"
        or config["place_time_in_force"] != "GOOD_TILL_CANCELED"
        or _integer("config.quantity_e4", config["quantity_e4"], minimum=1)
        != 10_000
        or _integer("config.price_cap_e4", config["price_cap_e4"], minimum=1)
        != 100
        or _integer(
            "config.max_cash_loss_e6",
            config["max_cash_loss_e6"],
            minimum=0,
        )
        != 10_000
        or _integer("config.max_fee_e6", config["max_fee_e6"], minimum=0)
        != 10_000
    ):
        raise MeasuredLatencyError("producer config safety constants differ")
    execution_uid = _integer(
        "config.execution_uid", config["execution_uid"], minimum=1
    )
    _integer("config.execution_gid", config["execution_gid"], minimum=1)
    configured_ca_path = _normalized_absolute_path_text(
        config["ca_bundle_path"], label="config.ca_bundle_path"
    )
    actual_ca_path = _normalized_absolute_path_text(
        ca_bundle_path, label="CA bundle path"
    )
    if configured_ca_path != actual_ca_path:
        raise MeasuredLatencyError("CA bundle path differs from config")
    ca_sha, _ = _hash_artifact(
        ca_bundle_path,
        label="CA bundle",
        require_root_read_only=require_root_read_only,
    )
    _pinned_sha(
        "config.ca_bundle_sha256",
        config["ca_bundle_sha256"],
        ca_sha,
    )

    environment, environment_sha, _ = _load_json_artifact(
        environment_receipt_path,
        label="environment receipt",
        require_root_read_only=require_root_read_only,
    )
    _exact_keys(
        "environment receipt",
        environment,
        frozenset(
            {
                "ca_bundle_sha256",
                "hostname",
                "instance_id",
                "kalshi_environment",
                "kalshi_mode",
                "machine_id_sha256",
                "producer_code_sha256",
                "producer_config_sha256",
                "schema_version",
            }
        ),
    )
    if (
        environment["schema_version"] != ENVIRONMENT_SCHEMA_VERSION
        or environment["kalshi_environment"] != "prod"
        or environment["kalshi_mode"] != "live"
    ):
        raise MeasuredLatencyError("environment receipt is not prod/live")
    for field, expected in (
        ("producer_code_sha256", binary_sha),
        ("producer_config_sha256", config_sha),
        ("ca_bundle_sha256", ca_sha),
    ):
        _pinned_sha(f"environment.{field}", environment[field], expected)
    hostname = _string("environment.hostname", environment["hostname"])
    instance_id = _string(
        "environment.instance_id", environment["instance_id"]
    )
    machine_id_sha = _sha(
        "environment.machine_id_sha256",
        environment["machine_id_sha256"],
    )

    host, host_sha, _ = _load_json_artifact(
        execution_host_receipt_path,
        label="execution host receipt",
        require_root_read_only=require_root_read_only,
    )
    _exact_keys(
        "execution host receipt",
        host,
        frozenset(
            {
                "captured_at_wall_utc_ms",
                "environment_fingerprint_sha256",
                "hostname",
                "instance_id",
                "machine_id_sha256",
                "schema_version",
            }
        ),
    )
    if host["schema_version"] != HOST_SCHEMA_VERSION:
        raise MeasuredLatencyError("execution host receipt schema differs")
    _document_time_ms(
        "host.captured_at_wall_utc_ms",
        host["captured_at_wall_utc_ms"],
        now_wall_utc_ms=now_ms,
        allow_old=True,
    )
    for field, expected in (
        ("hostname", hostname),
        ("instance_id", instance_id),
        ("machine_id_sha256", machine_id_sha),
        ("environment_fingerprint_sha256", environment_sha),
    ):
        if host[field] != expected:
            raise MeasuredLatencyError(f"host.{field} differs")

    clock, clock_sha, _ = _load_json_artifact(
        clock_quality_receipt_path,
        label="clock quality receipt",
        require_root_read_only=require_root_read_only,
    )
    _exact_keys(
        "clock quality receipt",
        clock,
        frozenset(
            {
                "captured_at_wall_utc_ms",
                "clock_id",
                "hostname",
                "instance_id",
                "machine_id_sha256",
                "max_error_ns",
                "schema_version",
                "source",
                "synchronized",
            }
        ),
    )
    if (
        clock["schema_version"] != CLOCK_SCHEMA_VERSION
        or clock["clock_id"] != PROBE_CLOCK_ID
        or clock["synchronized"] is not True
        or _integer(
            "clock.max_error_ns", clock["max_error_ns"], minimum=0
        )
        > 10_000_000
    ):
        raise MeasuredLatencyError("clock quality receipt is not acceptable")
    clock_captured_ms = _document_time_ms(
        "clock.captured_at_wall_utc_ms",
        clock["captured_at_wall_utc_ms"],
        now_wall_utc_ms=now_ms,
        allow_old=False,
    )
    _string("clock.source", clock["source"])
    for field, expected in (
        ("hostname", hostname),
        ("instance_id", instance_id),
        ("machine_id_sha256", machine_id_sha),
    ):
        if clock[field] != expected:
            raise MeasuredLatencyError(f"clock.{field} differs")

    place_authority, place_authority_sha, _ = _load_json_artifact(
        place_authority_path,
        label="PLACE/CANCEL authority",
        require_root_read_only=require_root_read_only,
    )
    place_keys = frozenset(
        {
            "allow_ioc_exit",
            "allow_place_cancel",
            "ca_bundle_sha256",
            "clock_quality_receipt_sha256",
            "consumption_ledger_path_sha256",
            "environment_fingerprint_sha256",
            "execution_host_fingerprint_sha256",
            "expires_at_unix_s",
            "issued_at_unix_s",
            "max_attempts",
            "max_cancel_orders",
            "max_cash_loss_e6",
            "max_fee_e6",
            "max_place_orders",
            "measured_on",
            "nonce_sha256",
            "price_cap_e4",
            "producer_code_sha256",
            "producer_config_sha256",
            "quantity_e4",
            "receipt_id",
            "schema_version",
            "single_use",
            "terminal_consumption_receipt_path_sha256",
            "ticker",
            "trace_output_path_sha256",
        }
    )
    _exact_keys("PLACE/CANCEL authority", place_authority, place_keys)
    if (
        place_authority["schema_version"]
        != PLACE_AUTHORITY_SCHEMA_VERSION
        or place_authority["allow_place_cancel"] is not True
        or place_authority["allow_ioc_exit"] is not False
        or _integer(
            "place.max_place_orders",
            place_authority["max_place_orders"],
            minimum=1,
        )
        != 1
        or _integer(
            "place.max_cancel_orders",
            place_authority["max_cancel_orders"],
            minimum=1,
        )
        != 1
        or _integer(
            "place.quantity_e4",
            place_authority["quantity_e4"],
            minimum=1,
        )
        != 10_000
        or _integer(
            "place.price_cap_e4",
            place_authority["price_cap_e4"],
            minimum=1,
        )
        != 100
        or _integer(
            "place.max_cash_loss_e6",
            place_authority["max_cash_loss_e6"],
            minimum=0,
        )
        != 10_000
        or _integer(
            "place.max_fee_e6",
            place_authority["max_fee_e6"],
            minimum=0,
        )
        != 10_000
    ):
        raise MeasuredLatencyError("PLACE/CANCEL authority action differs")
    place_issued_ms, place_expires_ms = _validate_authority_window(
        place_authority, label="PLACE/CANCEL authority"
    )

    ioc_authority, ioc_authority_sha, _ = _load_json_artifact(
        ioc_authority_path,
        label="IOC_EXIT authority",
        require_root_read_only=require_root_read_only,
    )
    ioc_keys = frozenset(
        {
            "allow_ioc_exit",
            "book_side",
            "ca_bundle_sha256",
            "clock_quality_receipt_sha256",
            "consumption_ledger_path_sha256",
            "environment_fingerprint_sha256",
            "expected_position_before_e4",
            "execution_host_fingerprint_sha256",
            "expires_at_unix_s",
            "issued_at_unix_s",
            "max_attempts",
            "max_cash_loss_e6",
            "max_fee_e6",
            "max_ioc_exit_orders",
            "measured_on",
            "nonce_sha256",
            "outcome_side",
            "price_limit_e4",
            "price_limit_semantics",
            "producer_code_sha256",
            "producer_config_sha256",
            "quantity_e4",
            "receipt_id",
            "schema_version",
            "single_use",
            "subaccount",
            "terminal_consumption_receipt_path_sha256",
            "ticker",
            "trace_output_path_sha256",
        }
    )
    _exact_keys("IOC_EXIT authority", ioc_authority, ioc_keys)
    expected_position = ioc_authority["expected_position_before_e4"]
    if type(expected_position) is not int or expected_position == 0:
        raise MeasuredLatencyError(
            "IOC_EXIT authority signed position is invalid"
        )
    ioc_quantity = _integer(
        "ioc.quantity_e4",
        ioc_authority["quantity_e4"],
        minimum=1,
    )
    required_ioc_book_side = "bid" if expected_position < 0 else "ask"
    required_ioc_outcome_side = "yes" if expected_position < 0 else "no"
    required_limit_semantics = (
        "MAXIMUM_BUY_YES_PRICE_CAP"
        if expected_position < 0
        else "MINIMUM_SELL_YES_PRICE_FLOOR"
    )
    ioc_maximum_cash_loss_e6 = _integer(
        "ioc.max_cash_loss_e6",
        ioc_authority["max_cash_loss_e6"],
        minimum=0,
    )
    ioc_maximum_fee_e6 = _integer(
        "ioc.max_fee_e6",
        ioc_authority["max_fee_e6"],
        minimum=0,
    )
    if (
        ioc_authority["schema_version"] != IOC_AUTHORITY_SCHEMA_VERSION
        or ioc_authority["allow_ioc_exit"] is not True
        or _integer(
            "ioc.max_ioc_exit_orders",
            ioc_authority["max_ioc_exit_orders"],
            minimum=1,
        )
        != 1
        or abs(expected_position) != ioc_quantity
        or ioc_quantity % 100 != 0
        or _integer(
            "ioc.price_limit_e4",
            ioc_authority["price_limit_e4"],
            minimum=1,
        )
        >= 10_000
        or ioc_maximum_cash_loss_e6 <= 0
        or ioc_maximum_fee_e6 <= 0
        or ioc_maximum_fee_e6 > ioc_maximum_cash_loss_e6
        or ioc_authority["book_side"] != required_ioc_book_side
        or ioc_authority["outcome_side"] != required_ioc_outcome_side
        or ioc_authority["price_limit_semantics"]
        != required_limit_semantics
        or _integer(
            "ioc.subaccount", ioc_authority["subaccount"]
        )
        != 0
    ):
        raise MeasuredLatencyError("IOC_EXIT authority action differs")
    ioc_issued_ms, ioc_expires_ms = _validate_authority_window(
        ioc_authority, label="IOC_EXIT authority"
    )
    if place_authority_sha == ioc_authority_sha:
        raise MeasuredLatencyError("action authorities must be distinct")
    if place_authority["nonce_sha256"] == ioc_authority["nonce_sha256"]:
        raise MeasuredLatencyError("action authority nonces must be distinct")

    receipt_id = _string("trace.receipt_id", private_trace["receipt_id"])
    ticker = _string("place.ticker", place_authority["ticker"])
    if (
        ioc_authority["ticker"] != ticker
        or place_authority["receipt_id"] != receipt_id
        or ioc_authority["receipt_id"] != receipt_id
    ):
        raise MeasuredLatencyError("authority ticker/receipt binding differs")
    for label, authority in (
        ("place", place_authority),
        ("ioc", ioc_authority),
    ):
        for field, expected in (
            ("producer_code_sha256", binary_sha),
            ("producer_config_sha256", config_sha),
            ("environment_fingerprint_sha256", environment_sha),
            ("execution_host_fingerprint_sha256", host_sha),
            ("clock_quality_receipt_sha256", clock_sha),
            ("ca_bundle_sha256", ca_sha),
            ("measured_on", hostname),
        ):
            if authority[field] != expected:
                raise MeasuredLatencyError(
                    f"{label} authority {field} differs"
                )
        _sha(f"{label}.nonce_sha256", authority["nonce_sha256"])
        price_field = (
            "price_limit_e4" if label == "ioc" else "price_cap_e4"
        )
        if (
            _integer(
                f"{label}.quantity_e4", authority["quantity_e4"], minimum=1
            )
            <= 0
            or _integer(
                f"{label}.{price_field}",
                authority[price_field],
                minimum=1,
            )
            >= 10_000
        ):
            raise MeasuredLatencyError(f"{label} authority price/quantity")

    promotion, _, _ = _load_json_artifact(
        promotion_receipt_path,
        label="root promotion receipt",
        require_root_read_only=require_root_read_only,
    )
    _exact_keys(
        "root promotion receipt",
        promotion,
        frozenset(
            {
                "producer_code_sha256",
                "promoted_at_wall_utc_ms",
                "promoted_mode_octal",
                "promoted_owner_uid",
                "promoted_path",
                "promoted_raw_sha256",
                "promoted_size_bytes",
                "promoter_uid",
                "schema_version",
                "source_device",
                "source_inode",
                "source_mode_octal",
                "source_path",
                "source_raw_sha256",
                "source_size_bytes",
                "source_uid",
            }
        ),
    )
    if (
        promotion["schema_version"] != PROMOTION_SCHEMA_VERSION
        or promotion["promoter_uid"] != 0
        or promotion["promoted_owner_uid"] != 0
        or promotion["promoted_mode_octal"] not in ("0400", "0444")
        or promotion["source_mode_octal"] != "0400"
        or promotion["source_uid"] != 0
        or promotion["producer_code_sha256"] != binary_sha
    ):
        raise MeasuredLatencyError("root promotion receipt identity differs")
    _integer("promotion.source_device", promotion["source_device"])
    _integer("promotion.source_inode", promotion["source_inode"], minimum=1)
    promoted_at_ms = _document_time_ms(
        "promotion.promoted_at_wall_utc_ms",
        promotion["promoted_at_wall_utc_ms"],
        now_wall_utc_ms=now_ms,
        allow_old=True,
    )
    promoted_path = _normalized_absolute_path_text(
        private_trace_path, label="promoted private trace path"
    )
    if require_root_read_only:
        promoted_info = os.lstat(promoted_path)
        promoted_mode = f"{stat.S_IMODE(promoted_info.st_mode):04o}"
        if (
            promoted_info.st_uid != promotion["promoted_owner_uid"]
            or promoted_mode != promotion["promoted_mode_octal"]
            or promoted_info.st_size != promotion["promoted_size_bytes"]
        ):
            raise MeasuredLatencyError(
                "root promotion receipt differs from promoted file metadata"
            )
    if (
        promotion["promoted_path"] != promoted_path
        or promotion["promoted_raw_sha256"] != trace_sha
        or promotion["source_raw_sha256"] != trace_sha
        or promotion["promoted_size_bytes"] != len(private_trace_raw)
        or promotion["source_size_bytes"] != len(private_trace_raw)
    ):
        raise MeasuredLatencyError("root promotion trace binding differs")
    source_path = _normalized_absolute_path_text(
        promotion["source_path"], label="promotion.source_path"
    )
    for label, authority in (
        ("place", place_authority),
        ("ioc", ioc_authority),
    ):
        if authority["trace_output_path_sha256"] != hashlib.sha256(
            source_path.encode()
        ).hexdigest():
            raise MeasuredLatencyError(
                f"{label} authority trace output path differs"
            )

    def load_consumption(
        *,
        label: str,
        authority: Mapping[str, Any],
        authority_sha: str,
        consumption_path: Path | str,
        terminal_path: Path | str,
        consumption_schema: str,
        terminal_schema: str,
        action: str,
    ) -> tuple[int, int, bool, str]:
        consumption, _, _ = _load_json_artifact(
            consumption_path,
            label=f"{label} consumption",
            require_root_read_only=require_root_read_only,
        )
        base_keys = {
            "authority_sha256",
            "ca_bundle_sha256",
            "clock_quality_receipt_sha256",
            "consumed_at_wall_utc_ms",
            "environment_fingerprint_sha256",
            "execution_host_fingerprint_sha256",
            "max_attempts",
            "nonce_sha256",
            "producer_code_sha256",
            "producer_config_sha256",
            "receipt_id",
            "schema_version",
            "terminal_consumption_receipt_path_sha256",
            "trace_output_path_sha256",
            "transaction_id",
        }
        if action == "PLACE_CANCEL":
            base_keys |= {
                "max_cancel_delete_attempts",
                "max_place_post_attempts",
            }
        else:
            base_keys |= {
                "expected_position_before_e4",
                "max_ioc_exit_post_attempts",
                "subaccount",
            }
        _exact_keys(
            f"{label} consumption",
            consumption,
            frozenset(base_keys),
        )
        if consumption["schema_version"] != consumption_schema:
            raise MeasuredLatencyError(f"{label} consumption schema differs")
        transaction_id = _transaction_id(
            authority_sha,
            authority["nonce_sha256"],
            authority["receipt_id"],
        )
        consumption_path_text = _normalized_absolute_path_text(
            consumption_path, label=f"{label} consumption path"
        )
        terminal_path_text = _normalized_absolute_path_text(
            terminal_path, label=f"{label} terminal path"
        )
        if (
            authority["consumption_ledger_path_sha256"]
            != hashlib.sha256(consumption_path_text.encode()).hexdigest()
            or authority["terminal_consumption_receipt_path_sha256"]
            != hashlib.sha256(terminal_path_text.encode()).hexdigest()
        ):
            raise MeasuredLatencyError(f"{label} receipt path binding differs")
        for field, expected in (
            ("authority_sha256", authority_sha),
            ("ca_bundle_sha256", ca_sha),
            ("clock_quality_receipt_sha256", clock_sha),
            ("environment_fingerprint_sha256", environment_sha),
            ("execution_host_fingerprint_sha256", host_sha),
            ("nonce_sha256", authority["nonce_sha256"]),
            ("producer_code_sha256", binary_sha),
            ("producer_config_sha256", config_sha),
            ("receipt_id", receipt_id),
            (
                "terminal_consumption_receipt_path_sha256",
                authority["terminal_consumption_receipt_path_sha256"],
            ),
            (
                "trace_output_path_sha256",
                authority["trace_output_path_sha256"],
            ),
            ("transaction_id", transaction_id),
        ):
            if consumption[field] != expected:
                raise MeasuredLatencyError(
                    f"{label} consumption {field} differs"
                )
        if _integer(
            f"{label}.consumption.max_attempts",
            consumption["max_attempts"],
            minimum=1,
        ) != 1:
            raise MeasuredLatencyError(f"{label} consumption attempt differs")
        if action == "PLACE_CANCEL":
            if (
                consumption["max_place_post_attempts"] != 1
                or consumption["max_cancel_delete_attempts"] != 1
            ):
                raise MeasuredLatencyError(
                    "PLACE/CANCEL consumption maxima differ"
                )
        elif consumption["max_ioc_exit_post_attempts"] != 1:
            raise MeasuredLatencyError("IOC consumption maxima differ")
        elif (
            consumption["expected_position_before_e4"]
            != authority["expected_position_before_e4"]
            or consumption["subaccount"] != authority["subaccount"]
        ):
            raise MeasuredLatencyError(
                "IOC consumption signed position/subaccount differs"
            )
        consumed_ms = _integer(
            f"{label}.consumed_at_wall_utc_ms",
            consumption["consumed_at_wall_utc_ms"],
            minimum=1,
        )
        if consumed_ms > now_ms + MAX_FUTURE_SKEW_MS:
            raise MeasuredLatencyError(
                f"{label} durable consumption is future-dated"
            )

        terminal, _, _ = _load_json_artifact(
            terminal_path,
            label=f"{label} terminal consumption",
            require_root_read_only=require_root_read_only,
        )
        if action == "PLACE_CANCEL":
            expected_terminal_keys = frozenset(
                {
                    "authority_sha256",
                    "cancel_delete_attempts",
                    "cancel_risk_reduction_after_expiry",
                    "client_order_id_sha256",
                    "finished_at_wall_utc_ms",
                    "place_post_attempts",
                    "schema_version",
                    "terminal_state",
                    "trace_source_sha256",
                    "transaction_id",
                }
            )
        else:
            expected_terminal_keys = frozenset(
                {
                    "authority_sha256",
                    "client_order_id_sha256",
                    "finished_at_wall_utc_ms",
                    "ioc_exit_post_attempts",
                    "schema_version",
                    "terminal_state",
                    "trace_source_sha256",
                    "transaction_id",
                }
            )
        _exact_keys(
            f"{label} terminal consumption",
            terminal,
            expected_terminal_keys,
        )
        expected_state = (
            "PLACE_CANCEL_RECONCILED"
            if action == "PLACE_CANCEL"
            else "IOC_EXIT_RECONCILED"
        )
        if (
            terminal["schema_version"] != terminal_schema
            or terminal["authority_sha256"] != authority_sha
            or terminal["transaction_id"] != transaction_id
            or terminal["terminal_state"] != expected_state
            or terminal["trace_source_sha256"] != trace_sha
        ):
            raise MeasuredLatencyError(f"{label} terminal binding differs")
        client_order_id_sha = _sha(
            f"{label}.terminal.client_order_id_sha256",
            terminal["client_order_id_sha256"],
        )
        cancel_after_expiry = False
        if action == "PLACE_CANCEL":
            if (
                terminal["place_post_attempts"] != 1
                or terminal["cancel_delete_attempts"] != 1
                or type(
                    terminal["cancel_risk_reduction_after_expiry"]
                )
                is not bool
            ):
                raise MeasuredLatencyError(
                    "PLACE/CANCEL terminal mutation counts differ"
                )
            cancel_after_expiry = terminal[
                "cancel_risk_reduction_after_expiry"
            ]
        elif terminal["ioc_exit_post_attempts"] != 1:
            raise MeasuredLatencyError("IOC terminal mutation count differs")
        finished_ms = _integer(
            f"{label}.finished_at_wall_utc_ms",
            terminal["finished_at_wall_utc_ms"],
            minimum=1,
        )
        if (
            finished_ms < consumed_ms
            or finished_ms > now_ms + MAX_FUTURE_SKEW_MS
        ):
            raise MeasuredLatencyError(
                f"{label} terminal time is outside its transaction"
            )
        return (
            consumed_ms,
            finished_ms,
            cancel_after_expiry,
            client_order_id_sha,
        )

    (
        place_consumed_ms,
        place_finished_ms,
        cancel_after_expiry,
        place_client_order_id_sha,
    ) = load_consumption(
        label="PLACE/CANCEL",
        authority=place_authority,
        authority_sha=place_authority_sha,
        consumption_path=place_consumption_path,
        terminal_path=place_terminal_path,
        consumption_schema=PLACE_CONSUMPTION_SCHEMA_VERSION,
        terminal_schema=PLACE_TERMINAL_SCHEMA_VERSION,
        action="PLACE_CANCEL",
    )
    (
        ioc_consumed_ms,
        ioc_finished_ms,
        _,
        ioc_client_order_id_sha,
    ) = load_consumption(
        label="IOC_EXIT",
        authority=ioc_authority,
        authority_sha=ioc_authority_sha,
        consumption_path=ioc_consumption_path,
        terminal_path=ioc_terminal_path,
        consumption_schema=IOC_CONSUMPTION_SCHEMA_VERSION,
        terminal_schema=IOC_TERMINAL_SCHEMA_VERSION,
        action="IOC_EXIT",
    )
    if place_client_order_id_sha == ioc_client_order_id_sha:
        raise MeasuredLatencyError(
            "PLACE/CANCEL and IOC_EXIT client order IDs must be distinct"
        )
    if not (
        place_issued_ms <= place_consumed_ms < place_expires_ms
        and ioc_issued_ms <= ioc_consumed_ms < ioc_expires_ms
    ):
        raise MeasuredLatencyError(
            "durable authority consumption falls outside authority window"
        )
    created_at_wall_ms = _integer(
        "trace.created_at_wall_utc_ms",
        private_trace["created_at_wall_utc_ms"],
        minimum=1,
    )
    if (
        created_at_wall_ms > now_ms + MAX_FUTURE_SKEW_MS
        or created_at_wall_ms < clock_captured_ms
        or created_at_wall_ms - clock_captured_ms > MAX_CLOCK_AGE_MS
        or place_finished_ms < created_at_wall_ms
        or ioc_finished_ms < created_at_wall_ms
        or promoted_at_ms < created_at_wall_ms
        or promoted_at_ms < place_finished_ms
        or promoted_at_ms < ioc_finished_ms
    ):
        raise MeasuredLatencyError(
            "trace/promotion time is outside trusted clock window"
        )
    return TrustedPublicationContext(
        now_wall_utc_ms=now_ms,
        producer_code_sha256=binary_sha,
        producer_config_sha256=config_sha,
        environment_sha256=environment_sha,
        host_sha256=host_sha,
        clock_sha256=clock_sha,
        clock_id=clock["clock_id"],
        hostname=hostname,
        instance_id=instance_id,
        machine_id_sha256=machine_id_sha,
        ca_bundle_sha256=ca_sha,
        execution_uid=execution_uid,
        ticker=ticker,
        ticker_sha256=hashlib.sha256(ticker.encode()).hexdigest(),
        place_authority_sha256=place_authority_sha,
        ioc_authority_sha256=ioc_authority_sha,
        place_issued_at_ms=place_issued_ms,
        place_expires_at_ms=place_expires_ms,
        ioc_issued_at_ms=ioc_issued_ms,
        ioc_expires_at_ms=ioc_expires_ms,
        place_consumed_at_ms=place_consumed_ms,
        place_finished_at_ms=place_finished_ms,
        ioc_consumed_at_ms=ioc_consumed_ms,
        ioc_finished_at_ms=ioc_finished_ms,
        place_quantity_e4=place_authority["quantity_e4"],
        place_price_cap_e4=place_authority["price_cap_e4"],
        ioc_expected_position_before_e4=expected_position,
        ioc_book_side=ioc_authority["book_side"],
        ioc_outcome_side=ioc_authority["outcome_side"],
        ioc_price_limit_semantics=ioc_authority[
            "price_limit_semantics"
        ],
        ioc_subaccount=ioc_authority["subaccount"],
        ioc_quantity_e4=ioc_authority["quantity_e4"],
        ioc_price_limit_e4=ioc_authority["price_limit_e4"],
        cancel_risk_reduction_after_expiry=cancel_after_expiry,
        promoted_trace_sha256=trace_sha,
    )


def load_private_trace(
    path: Path | str,
    *,
    producer_binary_path: Path | str,
    producer_config_path: Path | str,
    environment_receipt_path: Path | str,
    execution_host_receipt_path: Path | str,
    clock_quality_receipt_path: Path | str,
    ca_bundle_path: Path | str,
    place_authority_path: Path | str,
    place_consumption_path: Path | str,
    place_terminal_path: Path | str,
    ioc_authority_path: Path | str,
    ioc_consumption_path: Path | str,
    ioc_terminal_path: Path | str,
    promotion_receipt_path: Path | str,
    require_root_read_only: bool = False,
    now_wall_utc_ms: int | None = None,
) -> tuple[dict[str, Any], str]:
    """Load one promoted trace and independently verify its full lineage."""

    raw_bytes = _read_trace_bytes(
        path, require_root_read_only=require_root_read_only
    )
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeasuredLatencyError(
            f"private trace is not valid UTF-8 JSON: {exc}"
        ) from exc
    root = dict(_mapping("private trace", payload))
    if canonical_json_bytes(root) != raw_bytes:
        raise MeasuredLatencyError(
            "private trace bytes are not exact canonical JSON"
        )
    _exact_keys("private trace", root, _ROOT_KEYS)
    if root["schema_version"] != PRIVATE_TRACE_SCHEMA_VERSION:
        raise MeasuredLatencyError(
            f"unsupported private trace schema {root['schema_version']}"
        )
    if root["measurement_mode"] != REAL_ORDER_MEASUREMENT:
        raise MeasuredLatencyError(
            "measurement_mode must be REAL_ORDER_MEASURED"
        )
    receipt_id = _string("receipt_id", root["receipt_id"])
    clock_id = _string("clock_id", root["clock_id"])
    measured_on = _string("measured_on", root["measured_on"])
    for field, value in (
        ("receipt_id", receipt_id),
        ("clock_id", clock_id),
        ("measured_on", measured_on),
    ):
        if not value.isascii():
            raise MeasuredLatencyError(
                f"{field} must be ASCII for downstream canonical parity"
            )
    created_at_ns = _integer("created_at_ns", root["created_at_ns"])
    created_at_wall_utc_ms = _integer(
        "created_at_wall_utc_ms",
        root["created_at_wall_utc_ms"],
        minimum=1,
    )
    context = _load_trusted_publication_context(
        private_trace_path=path,
        private_trace_raw=raw_bytes,
        private_trace=root,
        producer_binary_path=producer_binary_path,
        producer_config_path=producer_config_path,
        environment_receipt_path=environment_receipt_path,
        execution_host_receipt_path=execution_host_receipt_path,
        clock_quality_receipt_path=clock_quality_receipt_path,
        ca_bundle_path=ca_bundle_path,
        place_authority_path=place_authority_path,
        place_consumption_path=place_consumption_path,
        place_terminal_path=place_terminal_path,
        ioc_authority_path=ioc_authority_path,
        ioc_consumption_path=ioc_consumption_path,
        ioc_terminal_path=ioc_terminal_path,
        promotion_receipt_path=promotion_receipt_path,
        require_root_read_only=require_root_read_only,
        now_wall_utc_ms=now_wall_utc_ms,
    )
    if source_sha256 != context.promoted_trace_sha256:
        raise MeasuredLatencyError("promoted trace SHA-256 differs")
    for field, actual, expected in (
        ("clock_id", clock_id, context.clock_id),
        ("measured_on", measured_on, context.hostname),
        (
            "clock_quality_receipt_sha256",
            root["clock_quality_receipt_sha256"],
            context.clock_sha256,
        ),
        (
            "environment_fingerprint_sha256",
            root["environment_fingerprint_sha256"],
            context.environment_sha256,
        ),
        (
            "execution_host_fingerprint_sha256",
            root["execution_host_fingerprint_sha256"],
            context.host_sha256,
        ),
        (
            "producer_code_sha256",
            root["producer_code_sha256"],
            context.producer_code_sha256,
        ),
        (
            "producer_config_sha256",
            root["producer_config_sha256"],
            context.producer_config_sha256,
        ),
    ):
        if actual != expected:
            raise MeasuredLatencyError(
                f"trace {field} differs from independently loaded artifact"
            )
    samples = root["samples"]
    if not isinstance(samples, list) or len(samples) != len(REQUIRED_PATHS):
        raise MeasuredLatencyError(
            "private trace requires exactly one PLACE, one CANCEL, and one "
            "IOC_EXIT sample"
        )

    seen_paths: set[str] = set()
    seen_events: set[str] = set()
    seen_requests: set[str] = set()
    seen_responses: set[str] = set()
    samples_by_path: dict[str, list[Mapping[str, Any]]] = {
        path_name: [] for path_name in REQUIRED_PATHS
    }
    maximum_effective = 0
    maximum_engine_ts_ms = 0
    minimum_engine_ts_ms: int | None = None
    for index, value in enumerate(samples):
        name = f"samples[{index}]"
        sample = _mapping(name, value)
        _exact_keys(name, sample, _SAMPLE_KEYS)
        path_name = sample["path"]
        if path_name not in REQUIRED_PATHS:
            raise MeasuredLatencyError(f"{name}.path is unsupported")
        if path_name in seen_paths:
            raise MeasuredLatencyError(
                f"private trace has more than one {path_name} sample"
            )
        if sample["action_semantics"] != ACTION_SEMANTICS[path_name]:
            raise MeasuredLatencyError(
                f"{name}.action_semantics does not match {path_name}"
            )
        timestamps = [
            _integer(f"{name}.{field}", sample[field])
            for field in (
                "decision_ns",
                "sent_ns",
                "acknowledged_ns",
                "effective_ns",
            )
        ]
        if not (
            timestamps[0]
            <= timestamps[1]
            <= timestamps[2]
            <= timestamps[3]
            and timestamps[3] > timestamps[0]
        ):
            raise MeasuredLatencyError(
                f"{name} timestamps are not causal on {clock_id}"
            )
        maximum_effective = max(maximum_effective, timestamps[3])
        for field in (
            "order_ref_sha256",
            "request_sha256",
            "response_sha256",
            "source_event_sha256",
            "live_authority_sha256",
            "clock_quality_receipt_sha256",
            "environment_fingerprint_sha256",
            "execution_host_fingerprint_sha256",
        ):
            _sha(f"{name}.{field}", sample[field])
        expected_authority = (
            context.ioc_authority_sha256
            if path_name == "IOC_EXIT"
            else context.place_authority_sha256
        )
        if sample["live_authority_sha256"] != expected_authority:
            raise MeasuredLatencyError(
                f"{name}.live_authority_sha256 differs from raw authority"
            )
        for field, expected in (
            ("clock_quality_receipt_sha256", context.clock_sha256),
            (
                "environment_fingerprint_sha256",
                context.environment_sha256,
            ),
            (
                "execution_host_fingerprint_sha256",
                context.host_sha256,
            ),
        ):
            if sample[field] != expected:
                raise MeasuredLatencyError(
                    f"{name}.{field} differs from trace execution context"
                )
        if sample["source_event_sha256"] in seen_events:
            raise MeasuredLatencyError(
                f"{name}.source_event_sha256 is duplicated"
            )
        seen_events.add(sample["source_event_sha256"])
        for field, seen in (
            ("request_sha256", seen_requests),
            ("response_sha256", seen_responses),
        ):
            if sample[field] in seen:
                raise MeasuredLatencyError(f"{name}.{field} is duplicated")
            seen.add(sample[field])
        http_status = _integer(
            f"{name}.http_status", sample["http_status"], minimum=100
        )
        if http_status < 200 or http_status >= 300:
            raise MeasuredLatencyError(f"{name}.http_status is not successful")
        expected_http_status = 200 if path_name == "CANCEL" else 201
        if http_status != expected_http_status:
            raise MeasuredLatencyError(
                f"{name}.http_status does not match V2 {path_name}"
            )
        engine_ts_ms = _integer(
            f"{name}.matching_engine_ts_ms",
            sample["matching_engine_ts_ms"],
            minimum=1,
        )
        if engine_ts_ms > context.now_wall_utc_ms + MAX_FUTURE_SKEW_MS:
            raise MeasuredLatencyError(
                f"{name}.matching_engine_ts_ms is future-dated"
            )
        if path_name == "PLACE":
            if not (
                context.place_issued_at_ms
                <= engine_ts_ms
                < context.place_expires_at_ms
                and context.place_consumed_at_ms
                <= engine_ts_ms
                <= context.place_finished_at_ms
            ):
                raise MeasuredLatencyError(
                    "PLACE mutation falls outside its consumed transaction"
                )
        elif path_name == "CANCEL":
            if (
                engine_ts_ms < context.place_issued_at_ms
                or engine_ts_ms > context.place_finished_at_ms
            ):
                raise MeasuredLatencyError(
                    "CANCEL mutation falls outside its transaction"
                )
            cancel_after_expiry = (
                engine_ts_ms >= context.place_expires_at_ms
            )
            if (
                cancel_after_expiry
                and not context.cancel_risk_reduction_after_expiry
            ):
                raise MeasuredLatencyError(
                    "CANCEL expiry classification differs from terminal "
                    "consumption"
                )
        else:
            if not (
                context.ioc_issued_at_ms
                <= engine_ts_ms
                < context.ioc_expires_at_ms
                and context.ioc_consumed_at_ms
                <= engine_ts_ms
                <= context.ioc_finished_at_ms
            ):
                raise MeasuredLatencyError(
                    "IOC_EXIT mutation falls outside its consumed transaction"
                )
        maximum_engine_ts_ms = max(
            maximum_engine_ts_ms, engine_ts_ms
        )
        minimum_engine_ts_ms = (
            engine_ts_ms
            if minimum_engine_ts_ms is None
            else min(minimum_engine_ts_ms, engine_ts_ms)
        )
        _validate_reconciliation(
            path_name,
            sample["fill_reconciliation"],
            sample_index=index,
        )
        filled_e4 = sample["fill_reconciliation"]["filled_quantity_e4"]
        average_fee = sample["average_fee_paid_e6"]
        average_fill_price = sample["average_fill_price_e4"]
        if filled_e4 == 0:
            if average_fee is not None or average_fill_price is not None:
                raise MeasuredLatencyError(
                    f"{name} average fee and fill price must be null "
                    "with zero fills"
                )
        else:
            _integer(
                f"{name}.average_fee_paid_e6",
                average_fee,
                minimum=0,
            )
            _integer(
                f"{name}.average_fill_price_e4",
                average_fill_price,
                minimum=1,
            )
            if average_fill_price > 10_000:
                raise MeasuredLatencyError(
                    f"{name}.average_fill_price_e4 exceeds $1"
                )
        ticker_sha256 = _sha(
            f"{name}.ticker_sha256", sample["ticker_sha256"]
        )
        if ticker_sha256 != context.ticker_sha256:
            raise MeasuredLatencyError(
                f"{name}.ticker_sha256 differs from authority ticker"
            )
        subaccount = _integer(
            f"{name}.subaccount", sample["subaccount"]
        )
        if subaccount > 32:
            raise MeasuredLatencyError(f"{name}.subaccount exceeds 32")
        book_side = sample["book_side"]
        if book_side not in ("BID", "ASK"):
            raise MeasuredLatencyError(
                f"{name}.book_side must be BID or ASK"
            )
        time_in_force = sample["time_in_force"]
        if time_in_force not in (
            "GOOD_TILL_CANCELED",
            "IMMEDIATE_OR_CANCEL",
        ):
            raise MeasuredLatencyError(f"{name}.time_in_force is unsupported")
        if type(sample["request_reduce_only"]) is not bool:
            raise MeasuredLatencyError(
                f"{name}.request_reduce_only must be boolean"
            )
        requested_price_e4 = _integer(
            f"{name}.requested_price_e4",
            sample["requested_price_e4"],
            minimum=1,
        )
        if requested_price_e4 >= 10_000:
            raise MeasuredLatencyError(
                f"{name}.requested_price_e4 must be below $1"
            )
        if path_name in ("PLACE", "CANCEL"):
            if (
                time_in_force != "GOOD_TILL_CANCELED"
                or sample["request_reduce_only"] is not False
                or sample["fill_reconciliation"][
                    "requested_quantity_e4"
                ]
                != context.place_quantity_e4
                or requested_price_e4 > context.place_price_cap_e4
            ):
                raise MeasuredLatencyError(
                    f"{name} PLACE/CANCEL differs from authority facts"
                )
        else:
            before = sample["fill_reconciliation"]["position_before_e4"]
            authority_book_side = context.ioc_book_side.upper()
            if context.ioc_price_limit_semantics == (
                "MAXIMUM_BUY_YES_PRICE_CAP"
            ):
                price_within_authority = (
                    requested_price_e4 <= context.ioc_price_limit_e4
                )
            elif context.ioc_price_limit_semantics == (
                "MINIMUM_SELL_YES_PRICE_FLOOR"
            ):
                price_within_authority = (
                    requested_price_e4 >= context.ioc_price_limit_e4
                )
            else:
                price_within_authority = False
            if (
                time_in_force != "IMMEDIATE_OR_CANCEL"
                or sample["request_reduce_only"] is not True
                or before != context.ioc_expected_position_before_e4
                or book_side != authority_book_side
                or subaccount != context.ioc_subaccount
                or sample["fill_reconciliation"][
                    "requested_quantity_e4"
                ]
                != context.ioc_quantity_e4
                or not price_within_authority
            ):
                raise MeasuredLatencyError(
                    f"{name} IOC_EXIT differs from authority or does not "
                    "reduce the signed pre-order position"
                )
        # Retained for the lifecycle checks below; these assignments also
        # make intentional use of every structural fact validated above.
        _ = ticker_sha256, subaccount, requested_price_e4
        samples_by_path[path_name].append(sample)
        seen_paths.add(path_name)
    if seen_paths != set(REQUIRED_PATHS):
        raise MeasuredLatencyError(
            "private trace does not contain exactly the three required paths"
        )
    if created_at_ns < maximum_effective:
        raise MeasuredLatencyError(
            "created_at_ns precedes a sample effective timestamp"
        )
    if created_at_wall_utc_ms < maximum_engine_ts_ms:
        raise MeasuredLatencyError(
            "created_at_wall_utc_ms precedes matching-engine processing"
        )
    if (
        minimum_engine_ts_ms is not None
        and created_at_wall_utc_ms - minimum_engine_ts_ms > 5 * 60 * 1_000
    ):
        raise MeasuredLatencyError(
            "matching-engine timestamps fall outside the five-minute "
            "clock-quality window"
        )

    places_by_order: dict[str, Mapping[str, Any]] = {}
    for place in samples_by_path["PLACE"]:
        order_ref = place["order_ref_sha256"]
        if order_ref in places_by_order:
            raise MeasuredLatencyError("duplicate PLACE lifecycle order_ref")
        places_by_order[order_ref] = place
    cancels_by_order: dict[str, Mapping[str, Any]] = {}
    for cancel in samples_by_path["CANCEL"]:
        order_ref = cancel["order_ref_sha256"]
        if order_ref in cancels_by_order:
            raise MeasuredLatencyError("duplicate CANCEL lifecycle order_ref")
        cancels_by_order[order_ref] = cancel
    if set(places_by_order) != set(cancels_by_order):
        raise MeasuredLatencyError(
            "PLACE and CANCEL must bind the same order_ref lifecycle"
        )
    for order_ref, place in places_by_order.items():
        cancel = cancels_by_order[order_ref]
        if cancel["decision_ns"] < place["effective_ns"]:
            raise MeasuredLatencyError(
                "CANCEL decision precedes PLACE effective readback"
            )
        if (
            cancel["matching_engine_ts_ms"]
            < place["matching_engine_ts_ms"]
        ):
            raise MeasuredLatencyError(
                "CANCEL matching-engine timestamp precedes PLACE"
            )
        for field in (
            "ticker_sha256",
            "subaccount",
            "book_side",
            "requested_price_e4",
            "time_in_force",
        ):
            if cancel[field] != place[field]:
                raise MeasuredLatencyError(
                    f"PLACE/CANCEL lifecycle fact {field} differs"
                )
    lifecycle_order_refs = set(places_by_order)
    ioc_order_refs: set[str] = set()
    for ioc_exit in samples_by_path["IOC_EXIT"]:
        if ioc_exit["order_ref_sha256"] in lifecycle_order_refs:
            raise MeasuredLatencyError(
                "IOC_EXIT must have a separate order lifecycle"
            )
        if ioc_exit["order_ref_sha256"] in ioc_order_refs:
            raise MeasuredLatencyError("duplicate IOC_EXIT lifecycle order_ref")
        ioc_order_refs.add(ioc_exit["order_ref_sha256"])
    if len(lifecycle_order_refs | ioc_order_refs) != 2:
        raise MeasuredLatencyError(
            "global order lifecycle cardinality must be exactly two"
        )
    return root, source_sha256


def build_receipts(
    private_trace: Mapping[str, Any],
    *,
    source_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build measured, inventory, and aggregate receipts."""

    _sha("source_sha256", source_sha256)
    measured_samples: list[dict[str, Any]] = []
    trace_candidates: list[dict[str, Any]] = []
    for sample in private_trace["samples"]:
        sample_identity = {
            "path": sample["path"],
            "order_ref_sha256": sample["order_ref_sha256"],
            "decision_ns": sample["decision_ns"],
            "sent_ns": sample["sent_ns"],
            "acknowledged_ns": sample["acknowledged_ns"],
            "effective_ns": sample["effective_ns"],
            "source_event_sha256": sample["source_event_sha256"],
        }
        measured_samples.append(
            {
                "sample_id": canonical_sha256(sample_identity),
                # The runner calls this order_id, but only a one-way reference
                # ever crosses the private/public boundary.
                "order_id": sample["order_ref_sha256"],
                "path": sample["path"],
                "decision_ns": sample["decision_ns"],
                "sent_ns": sample["sent_ns"],
                "acknowledged_ns": sample["acknowledged_ns"],
                "effective_ns": sample["effective_ns"],
                "source_event_sha256": sample["source_event_sha256"],
            }
        )
        trace_candidates.append(
            {
                "acknowledged_ns": sample["acknowledged_ns"],
                "action_semantics": sample["action_semantics"],
                "clock_id": private_trace["clock_id"],
                "decision_ns": sample["decision_ns"],
                "effective_ns": sample["effective_ns"],
                "measurement_mode": private_trace["measurement_mode"],
                "order_ref_sha256": sample["order_ref_sha256"],
                "path": sample["path"],
                "real_order_source_sha256": source_sha256,
                "sent_ns": sample["sent_ns"],
                "source_event_sha256": sample["source_event_sha256"],
            }
        )
    measured: dict[str, Any] = {
        "schema_version": MEASURED_RECEIPT_SCHEMA_VERSION,
        "receipt_id": private_trace["receipt_id"],
        "measurement_mode": private_trace["measurement_mode"],
        "clock_id": private_trace["clock_id"],
        "measured_on": private_trace["measured_on"],
        "created_at_ns": private_trace["created_at_ns"],
        "environment_fingerprint_sha256": (
            private_trace["environment_fingerprint_sha256"]
        ),
        "source_sha256": source_sha256,
        "samples": measured_samples,
    }
    measured["payload_sha256"] = canonical_sha256(measured)

    inventory: dict[str, Any] = {
        "audit_id": private_trace["receipt_id"],
        "audited_at_ns": private_trace["created_at_ns"],
        "clock_id": private_trace["clock_id"],
        "environment_fingerprint_sha256": (
            private_trace["environment_fingerprint_sha256"]
        ),
        "measured_on": private_trace["measured_on"],
        "rejected_artifacts": [],
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "trace_candidates": trace_candidates,
    }
    inventory_sha256 = hashlib.sha256(
        canonical_json_bytes(inventory)
    ).hexdigest()
    typed_inventory = LatencyEvidenceInventory(
        audit_id=inventory["audit_id"],
        audited_at_ns=inventory["audited_at_ns"],
        clock_id=inventory["clock_id"],
        measured_on=inventory["measured_on"],
        environment_fingerprint_sha256=(
            inventory["environment_fingerprint_sha256"]
        ),
        trace_candidates=tuple(
            TraceCandidate(
                measurement_mode=item["measurement_mode"],
                path=item["path"],
                action_semantics=item["action_semantics"],
                clock_id=item["clock_id"],
                order_ref_sha256=item["order_ref_sha256"],
                real_order_source_sha256=item["real_order_source_sha256"],
                source_event_sha256=item["source_event_sha256"],
                decision_ns=item["decision_ns"],
                sent_ns=item["sent_ns"],
                acknowledged_ns=item["acknowledged_ns"],
                effective_ns=item["effective_ns"],
            )
            for item in trace_candidates
        ),
        rejected_artifacts=tuple(),
        inventory_sha256=inventory_sha256,
    )
    aggregate = build_latency_audit_receipt(typed_inventory)
    if aggregate["state"] != LATENCY_AGGREGATE_READY:
        raise MeasuredLatencyError(
            "validated private trace unexpectedly produced blocked aggregate"
        )
    return measured, inventory, aggregate


def write_new_canonical(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    require_root_parent: bool = False,
) -> str:
    """Create a canonical receipt without overwriting an existing artifact."""

    hashes = write_canonical_bundle(
        {"receipt": (path, payload)},
        require_root_parent=require_root_parent,
    )
    return hashes["receipt"]


def _open_output_parent(
    path: Path | str,
    *,
    label: str,
    require_root_parent: bool,
) -> tuple[Path, int, os.stat_result]:
    output = _absolute_no_symlink_path(
        path, allow_missing_leaf=True, label=label
    )
    parent = output.parent
    _absolute_no_symlink_path(
        parent, allow_missing_leaf=False, label=f"{label} parent"
    )
    if require_root_parent:
        if os.geteuid() != 0:
            raise MeasuredLatencyError(
                "production receipt publication must run as root after the "
                "networkless private trace is promoted"
            )
        current = Path(parent.anchor)
        for component in parent.parts[1:]:
            current /= component
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise MeasuredLatencyError(
                    f"{label} parent boundary is unavailable"
                ) from exc
            if (
                info.st_uid != 0
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_mode & 0o022
            ):
                raise MeasuredLatencyError(
                    f"{label} parent must be root-owned, no-symlink, and "
                    "not group/world writable"
                )
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        raise MeasuredLatencyError(
            f"{label} parent cannot be opened safely"
        ) from exc
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(directory_fd)
        raise MeasuredLatencyError(f"{label} parent is not a directory")
    return output, directory_fd, info


def write_canonical_bundle(
    outputs: Mapping[str, tuple[Path | str, Mapping[str, Any]]],
    *,
    require_root_parent: bool = False,
) -> dict[str, str]:
    """Atomically reserve every bundle path before writing any receipt."""

    normalized: dict[str, tuple[Path, bytes, int]] = {}
    identities: set[tuple[int, int, str]] = set()
    try:
        for label, (raw_path, payload) in outputs.items():
            path, directory_fd, directory_info = _open_output_parent(
                raw_path,
                label=label,
                require_root_parent=require_root_parent,
            )
            identity = (
                directory_info.st_dev,
                directory_info.st_ino,
                path.name,
            )
            if identity in identities:
                os.close(directory_fd)
                raise MeasuredLatencyError(
                    "receipt bundle paths must be distinct"
                )
            identities.add(identity)
            normalized[label] = (
                path,
                canonical_json_bytes(payload),
                directory_fd,
            )
    except Exception:
        for _, _, opened_fd in normalized.values():
            try:
                os.close(opened_fd)
            except OSError:
                pass
        raise

    fds: dict[str, int] = {}
    created: list[str] = []
    try:
        for label, (path, _, directory_fd) in normalized.items():
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            fds[label] = os.open(
                path.name,
                create_flags,
                0o400,
                dir_fd=directory_fd,
            )
            file_info = os.fstat(fds[label])
            if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_nlink != 1
                or file_info.st_uid != os.geteuid()
            ):
                raise MeasuredLatencyError(
                    f"{label} output reservation is not a private regular file"
                )
            created.append(label)
        for label, (_, data, _) in normalized.items():
            fd = fds[label]
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("short receipt write")
                offset += written
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        for label, fd in tuple(fds.items()):
            os.close(fd)
            del fds[label]
        for _, _, directory_fd in normalized.values():
            os.fsync(directory_fd)
    except Exception:
        for fd in fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        for label in created:
            path, _, directory_fd = normalized[label]
            try:
                os.unlink(path.name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        for _, _, directory_fd in normalized.values():
            try:
                os.close(directory_fd)
            except OSError:
                pass
    return {
        label: hashlib.sha256(data).hexdigest()
        for label, (_, data, _) in normalized.items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Produce de-identified measured and aggregate receipts from an "
            "independently promoted private causal order trace"
        )
    )
    parser.add_argument("private_trace", type=Path)
    parser.add_argument("--producer-binary", type=Path, required=True)
    parser.add_argument("--producer-config", type=Path, required=True)
    parser.add_argument("--environment-receipt", type=Path, required=True)
    parser.add_argument("--execution-host-receipt", type=Path, required=True)
    parser.add_argument("--clock-quality-receipt", type=Path, required=True)
    parser.add_argument("--ca-bundle", type=Path, required=True)
    parser.add_argument("--place-authority", type=Path, required=True)
    parser.add_argument("--place-consumption", type=Path, required=True)
    parser.add_argument("--place-terminal", type=Path, required=True)
    parser.add_argument("--ioc-authority", type=Path, required=True)
    parser.add_argument("--ioc-consumption", type=Path, required=True)
    parser.add_argument("--ioc-terminal", type=Path, required=True)
    parser.add_argument("--promotion-receipt", type=Path, required=True)
    parser.add_argument("--measured-out", type=Path, required=True)
    parser.add_argument("--inventory-out", type=Path, required=True)
    parser.add_argument("--aggregate-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        private_trace, source_sha256 = load_private_trace(
            args.private_trace,
            producer_binary_path=args.producer_binary,
            producer_config_path=args.producer_config,
            environment_receipt_path=args.environment_receipt,
            execution_host_receipt_path=args.execution_host_receipt,
            clock_quality_receipt_path=args.clock_quality_receipt,
            ca_bundle_path=args.ca_bundle,
            place_authority_path=args.place_authority,
            place_consumption_path=args.place_consumption,
            place_terminal_path=args.place_terminal,
            ioc_authority_path=args.ioc_authority,
            ioc_consumption_path=args.ioc_consumption,
            ioc_terminal_path=args.ioc_terminal,
            promotion_receipt_path=args.promotion_receipt,
            require_root_read_only=True,
        )
        measured, inventory, aggregate = build_receipts(
            private_trace,
            source_sha256=source_sha256,
        )
        hashes = write_canonical_bundle(
            {
                "aggregate": (args.aggregate_out, aggregate),
                "inventory": (args.inventory_out, inventory),
                "measured": (args.measured_out, measured),
            },
            require_root_parent=True,
        )
    except (MeasuredLatencyError, ValueError, OSError) as exc:
        print(f"measured latency producer refused: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "aggregate_out": str(args.aggregate_out),
                "hashes": hashes,
                "inventory_out": str(args.inventory_out),
                "measured_out": str(args.measured_out),
                "source_sha256": source_sha256,
            }
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
