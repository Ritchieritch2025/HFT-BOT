#!/usr/bin/env python3
"""Independent V2 integrity boundary for Stage-2 source preparation.

V2 fixes two fail-open surfaces in the V1 preparation foundation without
changing V1:

* local parent objects are opened only through a canonical, symlink-free
  relative path below one explicit content root; and
* completeness receipts are derived only from an immutable result emitted by
  the real normalization/reconstruction pipeline.

This module remains source preparation only.  It does not claim that the six
warehouse parents have been read by an installed adapter, does not implement
the rejected V4.1 contract, and cannot authorize extraction, fitting,
candidate selection, shadow, or live use.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from tools.research.crypto_mm import round4_stage2_postfill_extractor as V1


BOUND_MANIFEST_SCHEMA = "round4-stage2-bound-input-manifest-v2"
CAUSAL_RESULT_SCHEMA = "round4-stage2-causal-preparation-result-v2"
SOURCE_PREPARATION_RECEIPT_SCHEMA = (
    "round4-stage2-source-preparation-receipt-v2"
)
SOURCE_PREPARATION_STATUS = "SOURCE_PREPARATION_ONLY"
CONTRACT_ADAPTER_STATUS = "V4_2_PENDING"
REAL_WAREHOUSE_ADAPTER_STATUS = "PENDING"

_RESULT_FACTORY_TOKEN = object()


class Stage2PreparationV2Error(RuntimeError):
    """A V2 path, byte, typed-result, or causal invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise Stage2PreparationV2Error(code, detail)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and V1.SHA256_RE.fullmatch(value) is not None
    )


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        _fail("INVALID_SHA256", label)
    return str(value)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("INVALID_TEXT", label)
    return value


def _canonical_logical_source_key(value: object) -> tuple[str, ...]:
    """Return canonical POSIX path parts or reject before any filesystem IO."""
    key = _require_text(value, "logical_source_key")
    if (
        "\x00" in key
        or "\\" in key
        or PurePosixPath(key).is_absolute()
        or key.startswith("/")
    ):
        _fail("LOGICAL_SOURCE_KEY_INVALID", repr(key))
    parts = key.split("/")
    if (
        not parts
        or any(part in ("", ".", "..") for part in parts)
        or "/".join(parts) != key
    ):
        _fail("LOGICAL_SOURCE_KEY_INVALID", repr(key))
    return tuple(parts)


def _lexical_absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail("LOCAL_SOURCE_UNREADABLE", f"{label}:{path}: {exc}")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("SYMLINK_FORBIDDEN", f"{label}:{path}")
    return metadata


def _validate_root_without_symlinks(root: Path) -> Path:
    """Reject a symlink at the root or in its lexical filesystem ancestry."""
    ancestry = list(root.parents)
    ancestry.reverse()
    for ancestor in ancestry:
        # The filesystem anchor itself is not a directory entry.
        if ancestor == Path(ancestor.anchor):
            continue
        metadata = _reject_symlink(ancestor, label="content-root-ancestor")
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("CONTENT_ROOT_INVALID", str(ancestor))
    root_metadata = _reject_symlink(root, label="content-root")
    if not stat.S_ISDIR(root_metadata.st_mode):
        _fail("CONTENT_ROOT_INVALID", str(root))
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        _fail("CONTENT_ROOT_INVALID", f"{root}: {exc}")


def _read_symlink_free_regular_file(
    *,
    root: Path,
    root_resolved: Path,
    parts: tuple[str, ...],
) -> tuple[Path, int, str]:
    """Open a file component-by-component with ``O_NOFOLLOW``.

    The lstat/resolve checks make the intended invariant explicit in the
    receipt.  The descriptor walk avoids silently following a link introduced
    at the final component between the check and the read.
    """
    candidate = root
    for index, part in enumerate(parts):
        candidate = candidate / part
        metadata = _reject_symlink(
            candidate,
            label="source-path-component",
        )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            _fail("LOCAL_SOURCE_UNREADABLE", f"ancestor-not-directory:{candidate}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("REGULAR_FILE_REQUIRED", str(candidate))

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        _fail("LOCAL_SOURCE_UNREADABLE", f"{candidate}: {exc}")
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        _fail(
            "CONTENT_ROOT_ESCAPE",
            f"root={root_resolved} source={resolved}",
        )

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        current = os.open(
            os.fspath(root),
            os.O_RDONLY | directory | nofollow,
        )
        descriptors.append(current)
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | nofollow
            if index < len(parts) - 1:
                flags |= directory
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        source_fd = descriptors[-1]
        opened_stat = os.fstat(source_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            _fail("REGULAR_FILE_REQUIRED", str(candidate))
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    except Stage2PreparationV2Error:
        raise
    except OSError as exc:
        # ELOOP is the usual O_NOFOLLOW symlink signal.  Static symlinks have
        # already been classified above; classify a race the same way.
        if getattr(exc, "errno", None) == errno.ELOOP:
            _fail("SYMLINK_FORBIDDEN", f"{candidate}: {exc}")
        _fail("LOCAL_SOURCE_UNREADABLE", f"{candidate}: {exc}")
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
    return resolved, size, digest.hexdigest()


@dataclass(frozen=True)
class BoundExactParentV2:
    source_date_utc: str
    table: str
    logical_source_key: str
    physical_path: str
    version_id: str
    sha256: str
    size: int
    regular_file: bool = True
    symlink_free: bool = True

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "logical_source_key": self.logical_source_key,
            "physical_path": self.physical_path,
            "VersionId": self.version_id,
            "sha256": self.sha256,
            "size": self.size,
            "regular_file": self.regular_file,
            "symlink_free": self.symlink_free,
        }


@dataclass(frozen=True)
class BoundInputManifestV2:
    schema: str
    status: str
    content_root: str
    source_dates: tuple[str, ...]
    parents: tuple[BoundExactParentV2, ...]
    file_count: int
    authority_sha256: str
    payload_sha256: str
    content_root_bound: bool = True
    source_preparation_only: bool = True

    def receipt(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "content_root": self.content_root,
            "content_root_bound": self.content_root_bound,
            "source_preparation_only": self.source_preparation_only,
            "source_dates": list(self.source_dates),
            "file_count": self.file_count,
            "parents": [item.receipt() for item in self.parents],
            "authority_sha256": self.authority_sha256,
            "payload_sha256": self.payload_sha256,
        }


def _validate_authority_identity(
    authority: V1.SourcePreparationAuthority,
) -> None:
    if tuple(authority.source_dates) != V1.DISCOVERY_DATES:
        _fail("AUTHORITY_SOURCE_DATES_INVALID", repr(authority.source_dates))
    if (
        authority.source_preparation_allowed is not True
        or authority.extraction_allowed is not False
        or authority.contract_adapter_status != CONTRACT_ADAPTER_STATUS
    ):
        _fail("AUTHORITY_SCOPE_INVALID", "source preparation only required")
    if len(authority.exact_version_objects) != 6:
        _fail(
            "BOUND_INPUT_COUNT_MISMATCH",
            str(len(authority.exact_version_objects)),
        )

    expected = {
        (day, table, key)
        for day in V1.DISCOVERY_DATES
        for table, key in V1.required_btc_fact_keys(day).items()
    }
    actual = {
        (item.source_date_utc, item.table, item.logical_source_key)
        for item in authority.exact_version_objects
    }
    if actual != expected:
        _fail("REQUIRED_SOURCE_SET_INVALID", repr(sorted(actual)))

    if len(authority.eligible_days) != 3:
        _fail("AUTHORITY_DAY_CHAIN_INVALID", "expected three eligible days")
    day_objects = {
        (
            item.source_date_utc,
            item.table,
            item.logical_source_key,
            item.version_id,
            item.sha256,
            item.size,
        )
        for day in authority.eligible_days
        for item in day.exact_version_objects
    }
    exact_objects = {
        (
            item.source_date_utc,
            item.table,
            item.logical_source_key,
            item.version_id,
            item.sha256,
            item.size,
        )
        for item in authority.exact_version_objects
    }
    if day_objects != exact_objects:
        _fail("AUTHORITY_DAY_CHAIN_INVALID", "day/object binding mismatch")


def build_bound_input_manifest_v2(
    *,
    authority: V1.SourcePreparationAuthority,
    content_root: Path | str,
) -> BoundInputManifestV2:
    """Bind exactly six exact-version parents under a symlink-free root."""
    if type(authority) is not V1.SourcePreparationAuthority:
        _fail("AUTHORITY_TYPE_INVALID", repr(type(authority)))

    # Reject every malformed caller path before comparing the authority sets.
    path_parts: dict[int, tuple[str, ...]] = {}
    for item in authority.exact_version_objects:
        if type(item) is not V1.EligibleExactVersionObject:
            _fail("AUTHORITY_OBJECT_TYPE_INVALID", repr(type(item)))
        path_parts[id(item)] = _canonical_logical_source_key(
            item.logical_source_key
        )
    _validate_authority_identity(authority)

    root = _lexical_absolute(content_root)
    root_resolved = _validate_root_without_symlinks(root)
    parents: list[BoundExactParentV2] = []
    for item in sorted(
        authority.exact_version_objects,
        key=lambda row: (
            row.source_date_utc,
            row.table,
            row.logical_source_key,
        ),
    ):
        if (
            not _is_sha256(item.sha256)
            or type(item.size) is not int
            or item.size <= 0
            or not item.version_id
            or item.verification_state != "EXACT_VERSION_FULL_SHA256"
            or item.version_resolution != "SEALED_CURRENT_EXACT"
        ):
            _fail(
                "SOURCE_NOT_EXACT_VERSION",
                item.logical_source_key,
            )
        resolved, actual_size, actual_sha = (
            _read_symlink_free_regular_file(
                root=root,
                root_resolved=root_resolved,
                parts=path_parts[id(item)],
            )
        )
        if actual_size != item.size or actual_sha != item.sha256:
            _fail(
                "LOCAL_SOURCE_DRIFT",
                (
                    f"{item.logical_source_key}:"
                    f"expected=({item.size},{item.sha256}) "
                    f"actual=({actual_size},{actual_sha})"
                ),
            )
        parents.append(
            BoundExactParentV2(
                source_date_utc=item.source_date_utc,
                table=item.table,
                logical_source_key=item.logical_source_key,
                physical_path=str(resolved),
                version_id=item.version_id,
                sha256=actual_sha,
                size=actual_size,
            )
        )

    authority_sha = V1.canonical_sha256(authority.receipt())
    payload_without_sha: dict[str, object] = {
        "schema": BOUND_MANIFEST_SCHEMA,
        "status": "EXACT_VERSION_LOCAL_BYTES_VERIFIED",
        "content_root": str(root_resolved),
        "content_root_bound": True,
        "source_preparation_only": True,
        "source_dates": list(authority.source_dates),
        "file_count": len(parents),
        "parents": [item.receipt() for item in parents],
        "authority_sha256": authority_sha,
    }
    payload_sha = V1.canonical_sha256(payload_without_sha)
    return BoundInputManifestV2(
        schema=BOUND_MANIFEST_SCHEMA,
        status="EXACT_VERSION_LOCAL_BYTES_VERIFIED",
        content_root=str(root_resolved),
        source_dates=tuple(authority.source_dates),
        parents=tuple(parents),
        file_count=len(parents),
        authority_sha256=authority_sha,
        payload_sha256=payload_sha,
    )


@dataclass(frozen=True)
class CausalEnvelopeRow:
    envelope_index: int
    market_ticker: str
    source_date_utc: str
    recv_mono_ns: int
    recv_wall_ns: int
    event_count: int
    l2_event_count: int
    trade_event_count: int
    event_kinds: tuple[str, ...]
    source_event_refs: tuple[str, ...]
    atomic_source_rows_sha256: str
    atomic_no_precedence: bool
    book_usable_before: bool
    book_usable_after: bool
    became_usable_from_valid_snapshot: bool
    first_valid_snapshot: bool
    ignored_pre_snapshot_delta_increment: int
    invalid_snapshot_increment: int
    causal_gap_increment: int
    observed_yes_price_e4_added: tuple[int, ...]
    observed_no_price_e4_added: tuple[int, ...]
    book_state_sha256_after: str | None

    def receipt(self) -> dict[str, object]:
        return {
            "envelope_index": self.envelope_index,
            "market_ticker": self.market_ticker,
            "source_date_utc": self.source_date_utc,
            "recv_mono_ns": self.recv_mono_ns,
            "recv_wall_ns": self.recv_wall_ns,
            "event_count": self.event_count,
            "l2_event_count": self.l2_event_count,
            "trade_event_count": self.trade_event_count,
            "event_kinds": list(self.event_kinds),
            "source_event_refs": list(self.source_event_refs),
            "atomic_source_rows_sha256": self.atomic_source_rows_sha256,
            "atomic_no_precedence": self.atomic_no_precedence,
            "book_usable_before": self.book_usable_before,
            "book_usable_after": self.book_usable_after,
            "became_usable_from_valid_snapshot": (
                self.became_usable_from_valid_snapshot
            ),
            "first_valid_snapshot": self.first_valid_snapshot,
            "ignored_pre_snapshot_delta_increment": (
                self.ignored_pre_snapshot_delta_increment
            ),
            "invalid_snapshot_increment": self.invalid_snapshot_increment,
            "causal_gap_increment": self.causal_gap_increment,
            "observed_yes_price_e4_added": list(
                self.observed_yes_price_e4_added
            ),
            "observed_no_price_e4_added": list(
                self.observed_no_price_e4_added
            ),
            "book_state_sha256_after": self.book_state_sha256_after,
        }


@dataclass(frozen=True)
class MarketCausalCompleteness:
    market_ticker: str
    source_date_utc: str
    first_valid_snapshot_recv_mono_ns: int | None
    first_valid_snapshot_recv_wall_ns: int | None
    ignored_pre_snapshot_deltas: int
    invalid_or_empty_snapshots: int
    causal_gap_count: int
    l2_rows: int
    trade_rows: int
    atomic_envelope_count: int
    pre_snapshot_envelope_count: int
    left_truncated: bool
    observed_yes_price_e4: tuple[int, ...]
    observed_no_price_e4: tuple[int, ...]

    def receipt(self) -> dict[str, object]:
        return {
            "market_ticker": self.market_ticker,
            "source_date_utc": self.source_date_utc,
            "first_valid_snapshot_recv_mono_ns": (
                self.first_valid_snapshot_recv_mono_ns
            ),
            "first_valid_snapshot_recv_wall_ns": (
                self.first_valid_snapshot_recv_wall_ns
            ),
            "ignored_pre_snapshot_deltas": (
                self.ignored_pre_snapshot_deltas
            ),
            "invalid_or_empty_snapshots": (
                self.invalid_or_empty_snapshots
            ),
            "causal_gap_count": self.causal_gap_count,
            "l2_rows": self.l2_rows,
            "trade_rows": self.trade_rows,
            "atomic_envelope_count": self.atomic_envelope_count,
            "pre_snapshot_envelope_count": (
                self.pre_snapshot_envelope_count
            ),
            "left_truncated": self.left_truncated,
            "observed_yes_price_e4": list(
                self.observed_yes_price_e4
            ),
            "observed_no_price_e4": list(
                self.observed_no_price_e4
            ),
        }


@dataclass(frozen=True)
class CausalPreparationResult:
    schema: str
    status: str
    roster: tuple[str, ...]
    causal_rows: tuple[CausalEnvelopeRow, ...]
    market_completeness: tuple[MarketCausalCompleteness, ...]
    causal_row_count: int
    causal_spine_sha256: str
    source_event_spine_sha256: str
    roster_sha256: str
    real_warehouse_coverage_claimed: bool = False
    contract_adapter_status: str = CONTRACT_ADAPTER_STATUS
    _factory_token: object = field(
        default=None,
        repr=False,
        compare=False,
    )


def _stable_raw_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    label: str,
) -> tuple[Mapping[str, object], ...]:
    copied: list[dict[str, object]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            _fail("RAW_ROW_TYPE_INVALID", f"{label}:{index}:{type(raw)!r}")
        copied.append(dict(raw))
    # The canonical digest removes caller iteration order from source_ordinal.
    return tuple(sorted(copied, key=V1.canonical_sha256))


def _event_observed_prices(
    events: tuple[V1.SourceEvent, ...],
) -> tuple[set[int], set[int]]:
    yes: set[int] = set()
    no: set[int] = set()
    for event in events:
        if event.kind == "BOOK_SNAPSHOT":
            yes.update(int(item[0]) for item in event.payload["yes_levels"])
            no.update(int(item[0]) for item in event.payload["no_levels"])
        elif event.kind == "BOOK_DELTA":
            side = str(event.payload["side"])
            price = int(event.payload["price_e4"])
            (yes if side == "yes" else no).add(price)
    return yes, no


def _completeness_from_rows(
    rows: tuple[CausalEnvelopeRow, ...],
    *,
    validate_transitions: bool,
) -> tuple[MarketCausalCompleteness, ...]:
    by_market: dict[str, list[CausalEnvelopeRow]] = {
        ticker: [] for ticker in V1.EXPECTED_ROSTER
    }
    for row in rows:
        if row.market_ticker not in by_market:
            _fail("CAUSAL_RESULT_INCONSISTENT", row.market_ticker)
        by_market[row.market_ticker].append(row)

    result: list[MarketCausalCompleteness] = []
    for ticker in V1.EXPECTED_ROSTER:
        market_rows = by_market[ticker]
        first_mono: int | None = None
        first_wall: int | None = None
        first_index: int | None = None
        prior_clock: tuple[int, int] | None = None
        prior_usable = False
        yes: set[int] = set()
        no: set[int] = set()
        for index, row in enumerate(market_rows):
            if validate_transitions:
                if row.book_usable_before is not prior_usable:
                    _fail(
                        "CAUSAL_RESULT_INCONSISTENT",
                        f"{ticker}:book transition at {row.envelope_index}",
                    )
                if prior_clock is not None and (
                    row.recv_mono_ns <= prior_clock[0]
                    or row.recv_wall_ns <= prior_clock[1]
                ):
                    _fail(
                        "CAUSAL_RESULT_INCONSISTENT",
                        f"{ticker}:receive clock regression",
                    )
                if row.first_valid_snapshot:
                    if first_mono is not None or not row.book_usable_after:
                        _fail(
                            "CAUSAL_RESULT_INCONSISTENT",
                            f"{ticker}:invalid first snapshot marker",
                        )
                if (
                    row.became_usable_from_valid_snapshot
                    != (
                        not row.book_usable_before
                        and row.book_usable_after
                        and "BOOK_SNAPSHOT" in row.event_kinds
                    )
                ):
                    _fail(
                        "CAUSAL_RESULT_INCONSISTENT",
                        f"{ticker}:invalid usable transition marker",
                    )
            if row.first_valid_snapshot:
                first_mono = row.recv_mono_ns
                first_wall = row.recv_wall_ns
                first_index = index
            prior_clock = (row.recv_mono_ns, row.recv_wall_ns)
            prior_usable = row.book_usable_after
            yes.update(row.observed_yes_price_e4_added)
            no.update(row.observed_no_price_e4_added)
        pre_snapshot = (
            len(market_rows) if first_index is None else first_index
        )
        result.append(
            MarketCausalCompleteness(
                market_ticker=ticker,
                source_date_utc=V1.MARKET_SOURCE_DAY[ticker],
                first_valid_snapshot_recv_mono_ns=first_mono,
                first_valid_snapshot_recv_wall_ns=first_wall,
                ignored_pre_snapshot_deltas=sum(
                    row.ignored_pre_snapshot_delta_increment
                    for row in market_rows
                ),
                invalid_or_empty_snapshots=sum(
                    row.invalid_snapshot_increment for row in market_rows
                ),
                causal_gap_count=sum(
                    row.causal_gap_increment for row in market_rows
                ),
                l2_rows=sum(row.l2_event_count for row in market_rows),
                trade_rows=sum(
                    row.trade_event_count for row in market_rows
                ),
                atomic_envelope_count=len(market_rows),
                pre_snapshot_envelope_count=pre_snapshot,
                left_truncated=pre_snapshot > 0,
                observed_yes_price_e4=tuple(sorted(yes)),
                observed_no_price_e4=tuple(sorted(no)),
            )
        )
    return tuple(result)


def _prepare_causal_result(
    *,
    raw_l2_rows: Iterable[Mapping[str, object]],
    raw_trade_rows: Iterable[Mapping[str, object]],
) -> CausalPreparationResult:
    roster_set = V1.EXPECTED_ROSTER_SET
    normalized_l2 = tuple(
        V1.normalize_l2_row(
            raw,
            source_ordinal=index,
            roster=roster_set,
        )
        for index, raw in enumerate(
            _stable_raw_rows(raw_l2_rows, label="l2")
        )
    )
    normalized_trades = tuple(
        V1.normalize_trade_row(
            raw,
            source_ordinal=index,
            roster=roster_set,
        )
        for index, raw in enumerate(
            _stable_raw_rows(raw_trade_rows, label="trade")
        )
    )
    envelopes = V1.group_atomic_envelopes(
        (*normalized_l2, *normalized_trades)
    )
    replay = V1.CausalBookReconstructor(roster_set)
    causal_rows: list[CausalEnvelopeRow] = []
    for envelope_index, envelope in enumerate(envelopes):
        ticker = envelope.market_ticker
        before_usable = replay.is_usable(ticker)
        before_coverage = replay.coverage(ticker)
        yes_added, no_added = _event_observed_prices(envelope.events)
        replay.consume_atomic(envelope)
        after_usable = replay.is_usable(ticker)
        after_coverage = replay.coverage(ticker)
        first_snapshot = (
            before_coverage["first_valid_snapshot_recv_mono_ns"] is None
            and after_coverage["first_valid_snapshot_recv_mono_ns"]
            is not None
        )
        event_refs = tuple(
            (
                f"{item.kind}|{item.stable_source_id}|"
                f"{item.source_rows_sha256}"
            )
            for item in envelope.events
        )
        event_kinds = tuple(item.kind for item in envelope.events)
        if after_usable:
            book_state_sha: str | None = V1.canonical_sha256(
                replay.book(ticker)
            )
        else:
            book_state_sha = None
        causal_rows.append(
            CausalEnvelopeRow(
                envelope_index=envelope_index,
                market_ticker=ticker,
                source_date_utc=V1.MARKET_SOURCE_DAY[ticker],
                recv_mono_ns=envelope.recv_mono_ns,
                recv_wall_ns=envelope.recv_wall_ns,
                event_count=len(envelope.events),
                l2_event_count=sum(
                    item.kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
                    for item in envelope.events
                ),
                trade_event_count=sum(
                    item.kind == "TRADE" for item in envelope.events
                ),
                event_kinds=event_kinds,
                source_event_refs=event_refs,
                atomic_source_rows_sha256=envelope.source_rows_sha256,
                atomic_no_precedence=envelope.atomic_no_precedence,
                book_usable_before=before_usable,
                book_usable_after=after_usable,
                became_usable_from_valid_snapshot=(
                    not before_usable
                    and after_usable
                    and "BOOK_SNAPSHOT" in event_kinds
                ),
                first_valid_snapshot=first_snapshot,
                ignored_pre_snapshot_delta_increment=(
                    int(after_coverage["ignored_pre_snapshot_deltas"])
                    - int(before_coverage["ignored_pre_snapshot_deltas"])
                ),
                invalid_snapshot_increment=(
                    int(after_coverage["invalid_or_empty_snapshots"])
                    - int(before_coverage["invalid_or_empty_snapshots"])
                ),
                causal_gap_increment=(
                    int(after_coverage["causal_gap_count"])
                    - int(before_coverage["causal_gap_count"])
                ),
                observed_yes_price_e4_added=tuple(sorted(yes_added)),
                observed_no_price_e4_added=tuple(sorted(no_added)),
                book_state_sha256_after=book_state_sha,
            )
        )

    rows = tuple(causal_rows)
    completeness = _completeness_from_rows(
        rows,
        validate_transitions=True,
    )
    causal_spine = V1.canonical_sha256(
        [item.receipt() for item in rows]
    )
    source_event_spine = V1.canonical_sha256(
        [
            reference
            for item in rows
            for reference in item.source_event_refs
        ]
    )
    return CausalPreparationResult(
        schema=CAUSAL_RESULT_SCHEMA,
        status=SOURCE_PREPARATION_STATUS,
        roster=V1.EXPECTED_ROSTER,
        causal_rows=rows,
        market_completeness=completeness,
        causal_row_count=len(rows),
        causal_spine_sha256=causal_spine,
        source_event_spine_sha256=source_event_spine,
        roster_sha256=V1.canonical_sha256(list(V1.EXPECTED_ROSTER)),
        _factory_token=_RESULT_FACTORY_TOKEN,
    )


def prepare_causal_result(
    *,
    raw_l2_rows: Iterable[Mapping[str, object]],
    raw_trade_rows: Iterable[Mapping[str, object]],
) -> CausalPreparationResult:
    """Normalize, atomically group, and reconstruct one immutable spine."""
    try:
        return _prepare_causal_result(
            raw_l2_rows=raw_l2_rows,
            raw_trade_rows=raw_trade_rows,
        )
    except Stage2PreparationV2Error:
        raise
    except V1.Stage2ExtractionError as exc:
        _fail(exc.code, exc.detail)


def _validate_causal_row(
    row: CausalEnvelopeRow,
    *,
    expected_index: int,
) -> None:
    if type(row) is not CausalEnvelopeRow:
        _fail("CAUSAL_RESULT_INCONSISTENT", "causal row type")
    if (
        type(row.envelope_index) is not int
        or row.envelope_index != expected_index
        or row.market_ticker not in V1.EXPECTED_ROSTER_SET
        or row.source_date_utc != V1.MARKET_SOURCE_DAY[row.market_ticker]
        or type(row.recv_mono_ns) is not int
        or row.recv_mono_ns <= 0
        or type(row.recv_wall_ns) is not int
        or row.recv_wall_ns <= 0
        or type(row.event_count) is not int
        or row.event_count <= 0
        or type(row.l2_event_count) is not int
        or row.l2_event_count < 0
        or type(row.trade_event_count) is not int
        or row.trade_event_count < 0
        or row.event_count
        != row.l2_event_count + row.trade_event_count
        or len(row.event_kinds) != row.event_count
        or len(row.source_event_refs) != row.event_count
        or row.atomic_no_precedence is not True
        or type(row.book_usable_before) is not bool
        or type(row.book_usable_after) is not bool
        or type(row.first_valid_snapshot) is not bool
        or type(row.became_usable_from_valid_snapshot) is not bool
    ):
        _fail(
            "CAUSAL_RESULT_INCONSISTENT",
            f"row:{expected_index}",
        )
    if any(
        kind not in ("BOOK_SNAPSHOT", "BOOK_DELTA", "TRADE")
        for kind in row.event_kinds
    ):
        _fail("CAUSAL_RESULT_INCONSISTENT", f"row-kind:{expected_index}")
    if (
        sum(
            kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
            for kind in row.event_kinds
        )
        != row.l2_event_count
        or sum(kind == "TRADE" for kind in row.event_kinds)
        != row.trade_event_count
    ):
        _fail("CAUSAL_RESULT_INCONSISTENT", f"row-count:{expected_index}")
    for label, value in (
        ("ignored", row.ignored_pre_snapshot_delta_increment),
        ("invalid", row.invalid_snapshot_increment),
        ("gap", row.causal_gap_increment),
    ):
        if type(value) is not int or value < 0:
            _fail(
                "CAUSAL_RESULT_INCONSISTENT",
                f"row:{expected_index}:{label}",
            )
    for price in (
        *row.observed_yes_price_e4_added,
        *row.observed_no_price_e4_added,
    ):
        if type(price) is not int or not V1.is_legal_observed_price(price):
            _fail(
                "CAUSAL_RESULT_INCONSISTENT",
                f"row:{expected_index}:observed-price",
            )
    _require_sha256(
        row.atomic_source_rows_sha256,
        f"row:{expected_index}:atomic sha",
    )
    if row.book_usable_after:
        _require_sha256(
            row.book_state_sha256_after,
            f"row:{expected_index}:book sha",
        )
    elif row.book_state_sha256_after is not None:
        _fail(
            "CAUSAL_RESULT_INCONSISTENT",
            f"row:{expected_index}:unusable book hash",
        )


def build_source_preparation_receipt_v2(
    result: CausalPreparationResult,
) -> dict[str, object]:
    """Recompute completeness and hashes from typed causal rows only."""
    if type(result) is not CausalPreparationResult:
        _fail("TYPED_RESULT_REQUIRED", repr(type(result)))
    if result._factory_token is not _RESULT_FACTORY_TOKEN:
        _fail("TYPED_RESULT_REQUIRED", "result was not emitted by pipeline")
    if (
        result.schema != CAUSAL_RESULT_SCHEMA
        or result.status != SOURCE_PREPARATION_STATUS
        or result.real_warehouse_coverage_claimed is not False
        or result.contract_adapter_status != CONTRACT_ADAPTER_STATUS
        or tuple(result.roster) != V1.EXPECTED_ROSTER
    ):
        _fail("CAUSAL_RESULT_INCONSISTENT", "result header")
    if not result.causal_rows:
        _fail("CAUSAL_RESULT_INCONSISTENT", "causal rows empty")

    rows = tuple(result.causal_rows)
    prior_global: tuple[int, int, str] | None = None
    for index, row in enumerate(rows):
        _validate_causal_row(row, expected_index=index)
        current_global = (
            row.recv_mono_ns,
            row.recv_wall_ns,
            row.market_ticker,
        )
        if prior_global is not None and current_global < prior_global:
            _fail(
                "CAUSAL_RESULT_INCONSISTENT",
                f"global row order:{index}",
            )
        prior_global = current_global

    recomputed_count = len(rows)
    recomputed_spine = V1.canonical_sha256(
        [item.receipt() for item in rows]
    )
    recomputed_event_spine = V1.canonical_sha256(
        [
            reference
            for item in rows
            for reference in item.source_event_refs
        ]
    )
    recomputed_roster_sha = V1.canonical_sha256(
        list(V1.EXPECTED_ROSTER)
    )
    if (
        result.causal_row_count != recomputed_count
        or result.causal_spine_sha256 != recomputed_spine
        or result.source_event_spine_sha256 != recomputed_event_spine
        or result.roster_sha256 != recomputed_roster_sha
    ):
        _fail(
            "CAUSAL_RESULT_INCONSISTENT",
            "caller count/hash differs from typed rows",
        )

    recomputed_completeness = _completeness_from_rows(
        rows,
        validate_transitions=True,
    )
    if any(
        type(item) is not MarketCausalCompleteness
        for item in result.market_completeness
    ):
        _fail("CAUSAL_RESULT_INCONSISTENT", "market coverage type")
    if tuple(result.market_completeness) != recomputed_completeness:
        _fail(
            "CAUSAL_RESULT_INCONSISTENT",
            "market coverage differs from typed rows",
        )

    markets_per_day = {day: 0 for day in V1.DISCOVERY_DATES}
    for item in recomputed_completeness:
        if (
            type(item.first_valid_snapshot_recv_mono_ns) is not int
            or type(item.first_valid_snapshot_recv_wall_ns) is not int
            or item.atomic_envelope_count <= 0
        ):
            _fail(
                "ROSTER_INCOMPLETE",
                f"{item.market_ticker}:no valid causal snapshot",
            )
        markets_per_day[item.source_date_utc] += 1
    if (
        len(recomputed_completeness) != 72
        or markets_per_day
        != {
            "2026-07-20": 24,
            "2026-07-21": 24,
            "2026-07-22": 24,
        }
    ):
        _fail(
            "ROSTER_INCOMPLETE",
            f"markets={len(recomputed_completeness)} days={markets_per_day}",
        )

    payload: dict[str, object] = {
        "schema": SOURCE_PREPARATION_RECEIPT_SCHEMA,
        "status": SOURCE_PREPARATION_STATUS,
        "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        "real_warehouse_adapter_status": REAL_WAREHOUSE_ADAPTER_STATUS,
        "real_warehouse_coverage_claimed": False,
        "coverage_computed_from_typed_rows": True,
        "market_count": len(recomputed_completeness),
        "markets_per_day": markets_per_day,
        "roster_sha256": recomputed_roster_sha,
        "causal_row_count": recomputed_count,
        "causal_spine_sha256": recomputed_spine,
        "source_event_spine_sha256": recomputed_event_spine,
        "market_completeness_sha256": V1.canonical_sha256(
            [item.receipt() for item in recomputed_completeness]
        ),
        "market_completeness": [
            item.receipt() for item in recomputed_completeness
        ],
        "terminal_label_firewall": {
            "terminal_result_role": "LABEL_ONLY",
            "terminal_result_feature_use": False,
            "terminal_join_requires_sealed_decision_action_spines": True,
        },
        "extraction_authorized": False,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "shadow_authorized": False,
        "live_authorized": False,
    }
    payload["payload_sha256"] = V1.canonical_sha256(payload)
    return payload
