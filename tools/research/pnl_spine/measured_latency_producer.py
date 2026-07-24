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
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
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


def _read_trace_bytes(
    path: Path | str,
    *,
    require_root_read_only: bool,
) -> bytes:
    trace_path = _absolute_no_symlink_path(
        path, allow_missing_leaf=False, label="private trace"
    )
    if require_root_read_only:
        _require_root_control_boundary(trace_path, label="private trace")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(trace_path, flags)
    except OSError as exc:
        raise MeasuredLatencyError(
            f"cannot open private trace safely: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MeasuredLatencyError("private trace must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise MeasuredLatencyError("private trace exceeds 16 MiB")
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
            raise MeasuredLatencyError("private trace changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


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


def load_private_trace(
    path: Path | str,
    *,
    expected_source_sha256: str,
    expected_environment_fingerprint_sha256: str,
    expected_producer_code_sha256: str,
    expected_producer_config_sha256: str,
    expected_place_cancel_live_authority_sha256: str,
    expected_ioc_exit_live_authority_sha256: str,
    expected_clock_quality_receipt_sha256: str,
    expected_execution_host_fingerprint_sha256: str,
    require_root_read_only: bool = False,
) -> tuple[dict[str, Any], str]:
    """Load and externally pin one canonical, private causal trace."""

    raw_bytes = _read_trace_bytes(
        path, require_root_read_only=require_root_read_only
    )
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    _pinned_sha("source_sha256", source_sha256, expected_source_sha256)
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
    _pinned_sha(
        "clock_quality_receipt_sha256",
        root["clock_quality_receipt_sha256"],
        expected_clock_quality_receipt_sha256,
    )
    _pinned_sha(
        "environment_fingerprint_sha256",
        root["environment_fingerprint_sha256"],
        expected_environment_fingerprint_sha256,
    )
    _pinned_sha(
        "producer_code_sha256",
        root["producer_code_sha256"],
        expected_producer_code_sha256,
    )
    _pinned_sha(
        "execution_host_fingerprint_sha256",
        root["execution_host_fingerprint_sha256"],
        expected_execution_host_fingerprint_sha256,
    )
    _pinned_sha(
        "producer_config_sha256",
        root["producer_config_sha256"],
        expected_producer_config_sha256,
    )
    try:
        require_sha256(
            "expected_place_cancel_live_authority_sha256",
            expected_place_cancel_live_authority_sha256,
        )
        require_sha256(
            "expected_ioc_exit_live_authority_sha256",
            expected_ioc_exit_live_authority_sha256,
        )
    except ValueError as exc:
        raise MeasuredLatencyError(str(exc)) from exc
    if (
        expected_place_cancel_live_authority_sha256
        == expected_ioc_exit_live_authority_sha256
    ):
        raise MeasuredLatencyError(
            "PLACE/CANCEL and IOC_EXIT require distinct live authorities"
        )
    samples = root["samples"]
    if not isinstance(samples, list) or not samples:
        raise MeasuredLatencyError("samples must be a nonempty array")

    seen_paths: set[str] = set()
    seen_events: set[str] = set()
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
            expected_ioc_exit_live_authority_sha256
            if path_name == "IOC_EXIT"
            else expected_place_cancel_live_authority_sha256
        )
        if sample["live_authority_sha256"] != expected_authority:
            raise MeasuredLatencyError(
                f"{name}.live_authority_sha256 does not match external pin"
            )
        for field, root_field in (
            ("clock_quality_receipt_sha256", "clock_quality_receipt_sha256"),
            (
                "environment_fingerprint_sha256",
                "environment_fingerprint_sha256",
            ),
            (
                "execution_host_fingerprint_sha256",
                "execution_host_fingerprint_sha256",
            ),
        ):
            if sample[field] != root[root_field]:
                raise MeasuredLatencyError(
                    f"{name}.{field} differs from trace execution context"
                )
        if sample["source_event_sha256"] in seen_events:
            raise MeasuredLatencyError(
                f"{name}.source_event_sha256 is duplicated"
            )
        seen_events.add(sample["source_event_sha256"])
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
        _integer(
            f"{name}.matching_engine_ts_ms",
            sample["matching_engine_ts_ms"],
            minimum=1,
        )
        maximum_engine_ts_ms = max(
            maximum_engine_ts_ms, sample["matching_engine_ts_ms"]
        )
        minimum_engine_ts_ms = (
            sample["matching_engine_ts_ms"]
            if minimum_engine_ts_ms is None
            else min(minimum_engine_ts_ms, sample["matching_engine_ts_ms"])
        )
        _validate_reconciliation(path_name, sample["fill_reconciliation"], sample_index=index)
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
            ):
                raise MeasuredLatencyError(
                    f"{name} PLACE/CANCEL must bind non-reduce-only GTC facts"
                )
        else:
            before = sample["fill_reconciliation"]["position_before_e4"]
            required_side = "ASK" if before > 0 else "BID"
            if (
                time_in_force != "IMMEDIATE_OR_CANCEL"
                or sample["request_reduce_only"] is not True
                or book_side != required_side
            ):
                raise MeasuredLatencyError(
                    f"{name} IOC_EXIT direction/reduce-only/IOC facts "
                    "do not reduce the signed pre-order position"
                )
        # Retained for the lifecycle checks below; these assignments also
        # make intentional use of every structural fact validated above.
        _ = ticker_sha256, subaccount, requested_price_e4
        samples_by_path[path_name].append(sample)
        seen_paths.add(path_name)
    missing = set(REQUIRED_PATHS) - seen_paths
    if missing:
        raise MeasuredLatencyError(
            f"private trace is incomplete; missing paths={sorted(missing)}"
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
    for ioc_exit in samples_by_path["IOC_EXIT"]:
        if ioc_exit["order_ref_sha256"] in lifecycle_order_refs:
            raise MeasuredLatencyError(
                "IOC_EXIT must have a separate order lifecycle"
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
            "externally SHA-pinned private causal order trace"
        )
    )
    parser.add_argument("private_trace", type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument(
        "--expected-environment-fingerprint-sha256", required=True
    )
    parser.add_argument("--expected-producer-code-sha256", required=True)
    parser.add_argument("--expected-producer-config-sha256", required=True)
    parser.add_argument(
        "--expected-place-cancel-live-authority-sha256", required=True
    )
    parser.add_argument(
        "--expected-ioc-exit-live-authority-sha256", required=True
    )
    parser.add_argument(
        "--expected-clock-quality-receipt-sha256", required=True
    )
    parser.add_argument(
        "--expected-execution-host-fingerprint-sha256", required=True
    )
    parser.add_argument("--measured-out", type=Path, required=True)
    parser.add_argument("--inventory-out", type=Path, required=True)
    parser.add_argument("--aggregate-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        private_trace, source_sha256 = load_private_trace(
            args.private_trace,
            expected_source_sha256=args.expected_source_sha256,
            expected_environment_fingerprint_sha256=(
                args.expected_environment_fingerprint_sha256
            ),
            expected_producer_code_sha256=(
                args.expected_producer_code_sha256
            ),
            expected_producer_config_sha256=(
                args.expected_producer_config_sha256
            ),
            expected_place_cancel_live_authority_sha256=(
                args.expected_place_cancel_live_authority_sha256
            ),
            expected_ioc_exit_live_authority_sha256=(
                args.expected_ioc_exit_live_authority_sha256
            ),
            expected_clock_quality_receipt_sha256=(
                args.expected_clock_quality_receipt_sha256
            ),
            expected_execution_host_fingerprint_sha256=(
                args.expected_execution_host_fingerprint_sha256
            ),
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
