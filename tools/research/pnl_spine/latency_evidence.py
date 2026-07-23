"""De-identified audit of real-order latency evidence.

Network RTT, authenticated GETs, ping, local signing benchmarks, and exchange
order snapshots are useful diagnostics, but none is a causal execution trace.
This module keeps that distinction executable.

Input inventories may contain private, de-identified trace references.  Output
receipts never contain trace or order references, nor raw timestamps.  They
contain only per-path aggregates, blocker codes, source artifact SHA-256
bindings, and a canonical payload SHA-256.

No network or order API exists in this module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    canonical_json_bytes,
    canonical_sha256,
    require_int,
    require_nonempty,
    require_sha256,
)


INVENTORY_SCHEMA_VERSION = "pnl-spine-latency-evidence-inventory-v1"
RECEIPT_SCHEMA_VERSION = "pnl-spine-latency-audit-receipt-v1"

LATENCY_BLOCKED = "LATENCY_BLOCKED"
LATENCY_AGGREGATE_READY = "LATENCY_AGGREGATE_READY"

REQUIRED_PATHS = ("PLACE", "CANCEL", "IOC_EXIT")
REAL_ORDER_MEASUREMENT = "REAL_ORDER_MEASURED"

ACTION_SEMANTICS = {
    "PLACE": "NEW_ORDER_PLACE",
    "CANCEL": "RESTING_ORDER_CANCEL",
    "IOC_EXIT": "IOC_POSITION_REDUCING_EXIT",
}

NON_QUALIFYING_KINDS = frozenset(
    (
        "AUTHENTICATED_READ_ONLY_GET_RTT",
        "PUBLIC_READ_ONLY_GET_RTT",
        "NETWORK_PING_RTT",
        "CPU_ONLY_BENCH",
        "ORDER_ACTIVITY_SNAPSHOT",
        "READ_ONLY_MONITOR_CODE",
    )
)


class LatencyEvidenceError(ValueError):
    """Latency evidence is malformed or cannot be trusted."""


def _plain_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LatencyEvidenceError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise LatencyEvidenceError(f"{name} requires string keys")
    return value


def _array(name: str, value: object) -> Sequence[Any]:
    if not isinstance(value, list):
        raise LatencyEvidenceError(f"{name} must be an array")
    return value


def _exact_keys(
    name: str,
    value: Mapping[str, Any],
    expected: Sequence[str],
) -> None:
    actual = frozenset(value)
    wanted = frozenset(expected)
    if actual != wanted:
        raise LatencyEvidenceError(
            f"{name} keys differ; "
            f"missing={sorted(wanted - actual)}, "
            f"extra={sorted(actual - wanted)}"
        )


def _reject_float(_: str) -> object:
    raise LatencyEvidenceError(
        "latency evidence forbids floating-point JSON numbers"
    )


def _reject_constant(value: str) -> object:
    raise LatencyEvidenceError(
        f"latency evidence forbids JSON constant {value}"
    )


def _no_duplicate_object(
    pairs: Sequence[tuple[str, Any]],
) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LatencyEvidenceError(f"duplicate JSON key {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class TraceCandidate:
    """One de-identified candidate causal trace.

    Optional fields allow the auditor to retain a partial candidate and state
    exactly why it failed.  No raw order identifier is accepted.
    """

    measurement_mode: str
    path: str
    action_semantics: str
    clock_id: str | None
    order_ref_sha256: str | None
    real_order_source_sha256: str | None
    source_event_sha256: str | None
    decision_ns: int | None
    sent_ns: int | None
    acknowledged_ns: int | None
    effective_ns: int | None

    def audit_reasons(
        self,
        *,
        expected_clock_id: str,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.measurement_mode != REAL_ORDER_MEASUREMENT:
            reasons.append("NOT_REAL_ORDER_MEASUREMENT")
        if self.path not in REQUIRED_PATHS:
            reasons.append("UNKNOWN_PATH")
        elif self.action_semantics != ACTION_SEMANTICS[self.path]:
            reasons.append("WRONG_ACTION_SEMANTICS")
        if not isinstance(self.clock_id, str) or not self.clock_id.strip():
            reasons.append("CLOCK_ID_MISSING")
        elif self.clock_id != expected_clock_id:
            reasons.append("CLOCK_ID_MISMATCH")
        for field_name, value in (
            ("ORDER_REF_SHA_MISSING", self.order_ref_sha256),
            (
                "REAL_ORDER_SOURCE_SHA_MISSING",
                self.real_order_source_sha256,
            ),
            ("SOURCE_EVENT_SHA_MISSING", self.source_event_sha256),
        ):
            try:
                require_sha256(field_name, value)
            except ValueError:
                reasons.append(field_name)
        timestamps = (
            self.decision_ns,
            self.sent_ns,
            self.acknowledged_ns,
            self.effective_ns,
        )
        labels = (
            "DECISION_TIMESTAMP_MISSING",
            "SENT_TIMESTAMP_MISSING",
            "ACKNOWLEDGED_TIMESTAMP_MISSING",
            "EFFECTIVE_TIMESTAMP_MISSING",
        )
        for label, value in zip(labels, timestamps):
            if not _plain_int(value):
                reasons.append(label)
        if all(_plain_int(value) for value in timestamps):
            if not (
                timestamps[0]
                <= timestamps[1]
                <= timestamps[2]
                <= timestamps[3]
                and timestamps[3] > timestamps[0]
            ):
                reasons.append("NON_CAUSAL_TIMESTAMPS")
        return tuple(sorted(set(reasons)))

    @property
    def total_ns(self) -> int:
        if not (
            _plain_int(self.decision_ns)
            and _plain_int(self.effective_ns)
        ):
            raise LatencyEvidenceError(
                "partial trace has no total latency"
            )
        return self.effective_ns - self.decision_ns


@dataclass(frozen=True)
class RejectedArtifact:
    """Aggregate description of evidence that is not an order trace."""

    artifact_sha256: str
    artifact_type: str
    record_count: int
    observed_fields: tuple[str, ...]
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        require_sha256("artifact_sha256", self.artifact_sha256)
        require_nonempty("artifact_type", self.artifact_type)
        if self.artifact_type not in NON_QUALIFYING_KINDS:
            raise LatencyEvidenceError(
                f"unknown rejected artifact type {self.artifact_type}"
            )
        require_int("record_count", self.record_count, minimum=0)
        if (
            not isinstance(self.observed_fields, tuple)
            or tuple(sorted(set(self.observed_fields)))
            != self.observed_fields
        ):
            raise LatencyEvidenceError(
                "observed_fields must be a sorted unique tuple"
            )
        if (
            not isinstance(self.rejection_reasons, tuple)
            or not self.rejection_reasons
            or tuple(sorted(set(self.rejection_reasons)))
            != self.rejection_reasons
        ):
            raise LatencyEvidenceError(
                "rejection_reasons must be a nonempty sorted unique tuple"
            )
        for field in self.observed_fields:
            require_nonempty("observed_field", field)
        for reason in self.rejection_reasons:
            require_nonempty("rejection_reason", reason)


@dataclass(frozen=True)
class LatencyEvidenceInventory:
    audit_id: str
    audited_at_ns: int
    clock_id: str
    measured_on: str
    environment_fingerprint_sha256: str
    trace_candidates: tuple[TraceCandidate, ...]
    rejected_artifacts: tuple[RejectedArtifact, ...]
    inventory_sha256: str

    def __post_init__(self) -> None:
        require_nonempty("audit_id", self.audit_id)
        require_int("audited_at_ns", self.audited_at_ns, minimum=0)
        require_nonempty("clock_id", self.clock_id)
        require_nonempty("measured_on", self.measured_on)
        require_sha256(
            "environment_fingerprint_sha256",
            self.environment_fingerprint_sha256,
        )
        require_sha256("inventory_sha256", self.inventory_sha256)
        artifact_shas = [
            artifact.artifact_sha256
            for artifact in self.rejected_artifacts
        ]
        if len(artifact_shas) != len(set(artifact_shas)):
            raise LatencyEvidenceError(
                "duplicate rejected artifact SHA-256"
            )


def _nearest_rank(values: Sequence[int], percentile: int) -> int:
    ordered = sorted(values)
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[rank - 1]


def _path_summary(
    path: str,
    candidates: Sequence[TraceCandidate],
    *,
    expected_clock_id: str,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    valid: list[TraceCandidate] = []
    invalid_reason_counts: Counter[str] = Counter()
    invalid_count = 0
    for candidate in candidates:
        reasons = candidate.audit_reasons(
            expected_clock_id=expected_clock_id
        )
        if reasons:
            invalid_count += 1
            invalid_reason_counts.update(reasons)
        else:
            valid.append(candidate)
    durations = [candidate.total_ns for candidate in valid]
    if durations:
        aggregate: dict[str, object] = {
            "path": path,
            "valid_causal_sample_count": len(durations),
            "invalid_candidate_count": invalid_count,
            "invalid_reason_counts": dict(
                sorted(invalid_reason_counts.items())
            ),
            "p50_ns": _nearest_rank(durations, 50),
            "p95_ns": _nearest_rank(durations, 95),
            "p99_ns": _nearest_rank(durations, 99),
            "maximum_ns": max(durations),
        }
        return aggregate, []
    aggregate = {
        "path": path,
        "valid_causal_sample_count": 0,
        "invalid_candidate_count": invalid_count,
        "invalid_reason_counts": dict(
            sorted(invalid_reason_counts.items())
        ),
        "p50_ns": None,
        "p95_ns": None,
        "p99_ns": None,
        "maximum_ns": None,
    }
    blocker = {
        "code": f"LATENCY_{path}_REAL_CAUSAL_TRACE_MISSING",
        "path": path,
        "detail": (
            f"{path} requires a real order-specific "
            "decision<=sent<=acknowledged<=effective trace on one clock"
        ),
    }
    return aggregate, [blocker]


def build_latency_audit_receipt(
    inventory: LatencyEvidenceInventory,
) -> dict[str, object]:
    """Build a de-identified, canonical aggregate readiness receipt."""

    candidates_by_path = {
        path: [
            candidate
            for candidate in inventory.trace_candidates
            if candidate.path == path
        ]
        for path in REQUIRED_PATHS
    }
    summaries: list[dict[str, object]] = []
    blockers: list[dict[str, str]] = []
    for path in REQUIRED_PATHS:
        summary, path_blockers = _path_summary(
            path,
            candidates_by_path[path],
            expected_clock_id=inventory.clock_id,
        )
        summaries.append(summary)
        blockers.extend(path_blockers)

    unknown_path_count = sum(
        candidate.path not in REQUIRED_PATHS
        for candidate in inventory.trace_candidates
    )
    if unknown_path_count:
        blockers.append(
            {
                "code": "LATENCY_UNKNOWN_PATH_CANDIDATES",
                "path": "UNRECOGNIZED",
                "detail": (
                    f"{unknown_path_count} trace candidates claim "
                    "an unsupported path"
                ),
            }
        )

    rejected = [
        {
            "artifact_sha256": artifact.artifact_sha256,
            "artifact_type": artifact.artifact_type,
            "record_count": artifact.record_count,
            "observed_fields": list(artifact.observed_fields),
            "rejection_reasons": list(artifact.rejection_reasons),
        }
        for artifact in sorted(
            inventory.rejected_artifacts,
            key=lambda item: (
                item.artifact_type,
                item.artifact_sha256,
            ),
        )
    ]
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": inventory.audit_id,
        "state": (
            LATENCY_BLOCKED
            if blockers
            else LATENCY_AGGREGATE_READY
        ),
        "audited_at_ns": inventory.audited_at_ns,
        "clock_id": inventory.clock_id,
        "measured_on": inventory.measured_on,
        "environment_fingerprint_sha256": (
            inventory.environment_fingerprint_sha256
        ),
        "required_paths": list(REQUIRED_PATHS),
        "path_summaries": summaries,
        "blockers": sorted(
            blockers,
            key=lambda blocker: (
                blocker["path"],
                blocker["code"],
            ),
        ),
        "non_qualifying_artifacts": rejected,
        "source_inventory_sha256": inventory.inventory_sha256,
        "policy": {
            "ping_or_http_rtt_accepted_as_order_latency": False,
            "cpu_benchmark_accepted_as_exchange_latency": False,
            "order_snapshot_accepted_as_causal_trace": False,
            "raw_order_or_trace_identifiers_emitted": False,
        },
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def _parse_candidate(
    value: object,
    *,
    index: int,
) -> TraceCandidate:
    raw = _mapping(f"trace_candidates[{index}]", value)
    keys = (
        "acknowledged_ns",
        "action_semantics",
        "clock_id",
        "decision_ns",
        "effective_ns",
        "measurement_mode",
        "order_ref_sha256",
        "path",
        "real_order_source_sha256",
        "sent_ns",
        "source_event_sha256",
    )
    _exact_keys(f"trace_candidates[{index}]", raw, keys)
    return TraceCandidate(
        measurement_mode=raw["measurement_mode"],
        path=raw["path"],
        action_semantics=raw["action_semantics"],
        clock_id=raw["clock_id"],
        order_ref_sha256=raw["order_ref_sha256"],
        real_order_source_sha256=raw["real_order_source_sha256"],
        source_event_sha256=raw["source_event_sha256"],
        decision_ns=raw["decision_ns"],
        sent_ns=raw["sent_ns"],
        acknowledged_ns=raw["acknowledged_ns"],
        effective_ns=raw["effective_ns"],
    )


def _parse_artifact(
    value: object,
    *,
    index: int,
) -> RejectedArtifact:
    raw = _mapping(f"rejected_artifacts[{index}]", value)
    _exact_keys(
        f"rejected_artifacts[{index}]",
        raw,
        (
            "artifact_sha256",
            "artifact_type",
            "observed_fields",
            "record_count",
            "rejection_reasons",
        ),
    )
    observed_fields = tuple(
        sorted(
            set(
                require_nonempty("observed_field", item)
                for item in _array(
                    f"rejected_artifacts[{index}].observed_fields",
                    raw["observed_fields"],
                )
            )
        )
    )
    rejection_reasons = tuple(
        sorted(
            set(
                require_nonempty("rejection_reason", item)
                for item in _array(
                    f"rejected_artifacts[{index}].rejection_reasons",
                    raw["rejection_reasons"],
                )
            )
        )
    )
    return RejectedArtifact(
        artifact_sha256=raw["artifact_sha256"],
        artifact_type=raw["artifact_type"],
        record_count=raw["record_count"],
        observed_fields=observed_fields,
        rejection_reasons=rejection_reasons,
    )


def load_latency_evidence_inventory(
    path: Path | str,
    *,
    expected_inventory_sha256: str | None = None,
) -> LatencyEvidenceInventory:
    """Load a canonical local inventory without network access."""

    inventory_path = Path(path)
    if inventory_path.is_symlink():
        raise LatencyEvidenceError(
            "latency inventory path may not be a symlink"
        )
    try:
        raw_bytes = inventory_path.read_bytes()
    except OSError as exc:
        raise LatencyEvidenceError(
            f"cannot read latency inventory: {exc}"
        ) from exc
    inventory_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if expected_inventory_sha256 is not None:
        require_sha256(
            "expected_inventory_sha256",
            expected_inventory_sha256,
        )
        if inventory_sha256 != expected_inventory_sha256:
            raise LatencyEvidenceError(
                "latency inventory SHA-256 mismatch"
            )
    document_bytes = (
        raw_bytes[:-1]
        if raw_bytes.endswith(b"\n")
        else raw_bytes
    )
    try:
        payload = json.loads(
            document_bytes.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LatencyEvidenceError(
            f"latency inventory is not valid UTF-8 JSON: {exc}"
        ) from exc
    root = _mapping("latency inventory", payload)
    if canonical_json_bytes(root) != document_bytes:
        raise LatencyEvidenceError(
            "latency inventory bytes are not canonical JSON"
        )
    _exact_keys(
        "latency inventory",
        root,
        (
            "audit_id",
            "audited_at_ns",
            "clock_id",
            "environment_fingerprint_sha256",
            "measured_on",
            "rejected_artifacts",
            "schema_version",
            "trace_candidates",
        ),
    )
    if root["schema_version"] != INVENTORY_SCHEMA_VERSION:
        raise LatencyEvidenceError(
            f"unsupported inventory schema {root['schema_version']}"
        )
    candidates = tuple(
        _parse_candidate(item, index=index)
        for index, item in enumerate(
            _array("trace_candidates", root["trace_candidates"])
        )
    )
    artifacts = tuple(
        _parse_artifact(item, index=index)
        for index, item in enumerate(
            _array("rejected_artifacts", root["rejected_artifacts"])
        )
    )
    return LatencyEvidenceInventory(
        audit_id=root["audit_id"],
        audited_at_ns=root["audited_at_ns"],
        clock_id=root["clock_id"],
        measured_on=root["measured_on"],
        environment_fingerprint_sha256=(
            root["environment_fingerprint_sha256"]
        ),
        trace_candidates=candidates,
        rejected_artifacts=artifacts,
        inventory_sha256=inventory_sha256,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Print one canonical de-identified receipt to stdout."""

    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Audit real-order latency evidence without network I/O"
    )
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--expected-inventory-sha256")
    args = parser.parse_args(argv)
    try:
        inventory = load_latency_evidence_inventory(
            args.inventory,
            expected_inventory_sha256=(
                args.expected_inventory_sha256
            ),
        )
        receipt = build_latency_audit_receipt(inventory)
    except (LatencyEvidenceError, ValueError) as exc:
        print(f"latency evidence refused: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(receipt) + b"\n")
    return 0 if receipt["state"] == LATENCY_AGGREGATE_READY else 3


if __name__ == "__main__":
    raise SystemExit(main())
