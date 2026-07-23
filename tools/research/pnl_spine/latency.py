"""Integrity-bound measured latency receipts.

There is intentionally no caller-controlled ``measured=True`` flag.  The only
way to obtain a :class:`MeasuredLatencyProfile` is to supply an untampered,
causal receipt containing place, cancel, and exit observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .contracts import (
    canonical_sha256,
    require_int,
    require_nonempty,
    require_sha256,
)


class LatencyTruthUnavailable(RuntimeError):
    """A latency profile is partial, non-causal, or lacks integrity."""


class LatencyPath(str, Enum):
    PLACE = "PLACE"
    CANCEL = "CANCEL"
    EXIT = "EXIT"


REQUIRED_MEASURED_PATHS = frozenset(
    {LatencyPath.PLACE, LatencyPath.CANCEL, LatencyPath.EXIT}
)


@dataclass(frozen=True)
class LatencySample:
    sample_id: str
    order_id: str
    path: LatencyPath
    decision_ns: int
    sent_ns: int
    acknowledged_ns: int
    effective_ns: int

    def __post_init__(self) -> None:
        require_nonempty("sample_id", self.sample_id)
        require_nonempty("order_id", self.order_id)
        if not isinstance(self.path, LatencyPath):
            raise LatencyTruthUnavailable("path must be a LatencyPath")
        for name in (
            "decision_ns",
            "sent_ns",
            "acknowledged_ns",
            "effective_ns",
        ):
            require_int(name, getattr(self, name), minimum=0)
        if not (
            self.decision_ns
            <= self.sent_ns
            <= self.acknowledged_ns
            <= self.effective_ns
        ):
            raise LatencyTruthUnavailable(
                f"non-causal latency sample {self.sample_id}"
            )

    @property
    def total_ns(self) -> int:
        return self.effective_ns - self.decision_ns


def _receipt_payload(
    *,
    receipt_id: str,
    clock_id: str,
    measured_on: str,
    created_at_ns: int,
    source_sha256: str,
    samples: tuple[LatencySample, ...],
) -> dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "clock_id": clock_id,
        "measured_on": measured_on,
        "created_at_ns": created_at_ns,
        "source_sha256": source_sha256,
        "samples": samples,
    }


@dataclass(frozen=True)
class LatencyReceipt:
    receipt_id: str
    clock_id: str
    measured_on: str
    created_at_ns: int
    source_sha256: str
    samples: tuple[LatencySample, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        require_nonempty("receipt_id", self.receipt_id)
        require_nonempty("clock_id", self.clock_id)
        require_nonempty("measured_on", self.measured_on)
        require_int("created_at_ns", self.created_at_ns, minimum=0)
        require_sha256("source_sha256", self.source_sha256)
        require_sha256("payload_sha256", self.payload_sha256)
        if not isinstance(self.samples, tuple) or not self.samples:
            raise LatencyTruthUnavailable(
                "latency receipt requires at least one sample"
            )
        canonical_order = tuple(
            sorted(
                self.samples,
                key=lambda sample: (sample.path.value, sample.sample_id),
            )
        )
        if self.samples != canonical_order:
            raise LatencyTruthUnavailable(
                "latency samples are not in canonical order"
            )
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise LatencyTruthUnavailable("duplicate latency sample_id")
        if self.created_at_ns < max(
            sample.effective_ns for sample in self.samples
        ):
            raise LatencyTruthUnavailable(
                "receipt predates an effective latency observation"
            )
        expected = canonical_sha256(
            _receipt_payload(
                receipt_id=self.receipt_id,
                clock_id=self.clock_id,
                measured_on=self.measured_on,
                created_at_ns=self.created_at_ns,
                source_sha256=self.source_sha256,
                samples=self.samples,
            )
        )
        if self.payload_sha256 != expected:
            raise LatencyTruthUnavailable("latency receipt SHA-256 mismatch")

    @classmethod
    def create(
        cls,
        *,
        receipt_id: str,
        clock_id: str,
        measured_on: str,
        created_at_ns: int,
        source_sha256: str,
        samples: Iterable[LatencySample],
    ) -> "LatencyReceipt":
        canonical_samples = tuple(
            sorted(
                samples,
                key=lambda sample: (sample.path.value, sample.sample_id),
            )
        )
        payload = _receipt_payload(
            receipt_id=receipt_id,
            clock_id=clock_id,
            measured_on=measured_on,
            created_at_ns=created_at_ns,
            source_sha256=source_sha256,
            samples=canonical_samples,
        )
        return cls(
            receipt_id=receipt_id,
            clock_id=clock_id,
            measured_on=measured_on,
            created_at_ns=created_at_ns,
            source_sha256=source_sha256,
            samples=canonical_samples,
            payload_sha256=canonical_sha256(payload),
        )

    @property
    def observed_paths(self) -> frozenset[LatencyPath]:
        return frozenset(sample.path for sample in self.samples)

    def require_paths(
        self,
        required: frozenset[LatencyPath] = REQUIRED_MEASURED_PATHS,
    ) -> None:
        missing = required - self.observed_paths
        if missing:
            raise LatencyTruthUnavailable(
                "latency receipt missing paths: "
                + ",".join(sorted(path.value for path in missing))
            )


def _nearest_rank(values: tuple[int, ...], percentile: int) -> int:
    require_int("percentile", percentile, minimum=1, maximum=100)
    ordered = tuple(sorted(values))
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[rank - 1]


@dataclass(frozen=True)
class LatencyPathSummary:
    path: LatencyPath
    sample_count: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    maximum_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, LatencyPath):
            raise LatencyTruthUnavailable("summary path must be LatencyPath")
        require_int("sample_count", self.sample_count, minimum=1)
        for name in ("p50_ns", "p95_ns", "p99_ns", "maximum_ns"):
            require_int(name, getattr(self, name), minimum=0)
        if not (
            self.p50_ns
            <= self.p95_ns
            <= self.p99_ns
            <= self.maximum_ns
        ):
            raise LatencyTruthUnavailable(
                f"non-monotone latency quantiles for {self.path.value}"
            )


@dataclass(frozen=True)
class MeasuredLatencyProfile:
    """A complete, integrity-bound place/cancel/exit profile."""

    receipt_sha256: str
    summaries: tuple[LatencyPathSummary, ...]

    def __post_init__(self) -> None:
        require_sha256("receipt_sha256", self.receipt_sha256)
        if not isinstance(self.summaries, tuple):
            raise LatencyTruthUnavailable("summaries must be an immutable tuple")
        paths = tuple(summary.path for summary in self.summaries)
        if len(paths) != len(set(paths)):
            raise LatencyTruthUnavailable("duplicate measured latency path")
        if set(paths) != set(REQUIRED_MEASURED_PATHS):
            raise LatencyTruthUnavailable(
                "measured profile requires place, cancel, and exit"
            )
        if paths != tuple(sorted(paths, key=lambda path: path.value)):
            raise LatencyTruthUnavailable(
                "measured summaries are not in canonical order"
            )

    @classmethod
    def from_receipt(
        cls,
        receipt: LatencyReceipt,
    ) -> "MeasuredLatencyProfile":
        receipt.require_paths(REQUIRED_MEASURED_PATHS)
        summaries: list[LatencyPathSummary] = []
        for path in sorted(
            REQUIRED_MEASURED_PATHS,
            key=lambda item: item.value,
        ):
            durations = tuple(
                sample.total_ns
                for sample in receipt.samples
                if sample.path is path
            )
            summaries.append(
                LatencyPathSummary(
                    path=path,
                    sample_count=len(durations),
                    p50_ns=_nearest_rank(durations, 50),
                    p95_ns=_nearest_rank(durations, 95),
                    p99_ns=_nearest_rank(durations, 99),
                    maximum_ns=max(durations),
                )
            )
        return cls(
            receipt_sha256=receipt.payload_sha256,
            summaries=tuple(summaries),
        )
