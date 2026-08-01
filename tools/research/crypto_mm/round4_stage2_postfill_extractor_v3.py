#!/usr/bin/env python3
"""Authoritative six-parent source transform for Stage-2 V3.

The only construction path starts from a V1 exact-version authority, a typed
V2 bound-input manifest, and that manifest's content root.  V3 rebinds the
triple, reads every parent through byte EOF, reads every source row, invokes
the receive-clock normalization/reconstruction pipeline internally, and emits
immutable replay rows.

Replay rows retain normalized snapshots/deltas and after-state hashes, rather
than copying the full book into every envelope.  They are sufficient to
deterministically materialize the complete latest-as-of YES/NO L2 and
whole-cent visible-FOK surfaces at any admitted decision clock.  The receipt
builder replays the full spine and recomputes every book and FOK hash.  Callers
cannot supply raw rows, coverage, counts, or hashes.

This is an authoritative *source transform*, not an accepted V4.2 contract
adapter.  Extraction, fitting, candidate selection, shadow, and live use
remain disabled.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import ModuleType

from tools.research.crypto_mm import round4_stage2_postfill_extractor as V1
from tools.research.crypto_mm import round4_stage2_postfill_extractor_v2 as V2


RESULT_SCHEMA = "round4-stage2-authoritative-source-result-v3"
RECEIPT_SCHEMA = "round4-stage2-authoritative-source-receipt-v3"
STATUS = "AUTHORITATIVE_SOURCE_TRANSFORM_ONLY"
CONTRACT_ADAPTER_STATUS = "V4_2_PENDING"
_FACTORY_TOKEN = object()


class Stage2AuthoritativeV3Error(RuntimeError):
    """An exact-parent, EOF, causal-state, or receipt invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise Stage2AuthoritativeV3Error(code, detail)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and V1.SHA256_RE.fullmatch(value) is not None
    )


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        _fail("INVALID_SHA256", label)
    return str(value)


@dataclass(frozen=True)
class TransformCodeComponentV3:
    module: str
    sha256: str
    size: int

    def receipt(self) -> dict[str, object]:
        return {
            "module": self.module,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class SourceEventRefV3:
    kind: str
    stable_source_id: str
    source_ordinal: int
    source_rows_sha256: str
    parent_logical_source_key: str
    parent_version_id: str
    parent_sha256: str
    parent_size: int
    content_root: str

    def receipt(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stable_source_id": self.stable_source_id,
            "source_ordinal": self.source_ordinal,
            "source_rows_sha256": self.source_rows_sha256,
            "parent_logical_source_key": self.parent_logical_source_key,
            "parent_version_id": self.parent_version_id,
            "parent_sha256": self.parent_sha256,
            "parent_size": self.parent_size,
            "content_root": self.content_root,
        }


@dataclass(frozen=True)
class ParentEOFReadV3:
    source_date_utc: str
    table: str
    logical_source_key: str
    physical_path: str
    version_id: str
    bound_sha256: str
    bound_size: int
    byte_count_per_eof_pass: int
    byte_eof_pass_count: int
    byte_eof_reached: bool
    parser_input_from_controlled_snapshot: bool
    parser_snapshot_postparse_verified: bool
    parser_input_sha256: str
    parser_input_size: int
    parent_total_row_count: int
    roster_selected_row_count: int
    nonselected_row_count: int
    roster_selected_stream_exhausted: bool
    selected_raw_rows_sha256: str
    normalized_event_spine_sha256: str
    transform_code_sha256: str

    def receipt(self) -> dict[str, object]:
        return {
            "source_date_utc": self.source_date_utc,
            "table": self.table,
            "logical_source_key": self.logical_source_key,
            "physical_path": self.physical_path,
            "VersionId": self.version_id,
            "bound_sha256": self.bound_sha256,
            "bound_size": self.bound_size,
            "byte_count_per_eof_pass": self.byte_count_per_eof_pass,
            "byte_eof_pass_count": self.byte_eof_pass_count,
            "byte_eof_reached": self.byte_eof_reached,
            "parser_input_from_controlled_snapshot": (
                self.parser_input_from_controlled_snapshot
            ),
            "parser_snapshot_postparse_verified": (
                self.parser_snapshot_postparse_verified
            ),
            "parser_input_sha256": self.parser_input_sha256,
            "parser_input_size": self.parser_input_size,
            "parent_total_row_count": self.parent_total_row_count,
            "parent_total_row_operation": "COUNT_ONLY",
            "roster_selected_row_count": self.roster_selected_row_count,
            "roster_selected_row_operation": "PARSED_AND_NORMALIZED",
            "nonselected_row_count": self.nonselected_row_count,
            "nonselected_row_parse_claimed": False,
            "roster_selected_stream_exhausted": (
                self.roster_selected_stream_exhausted
            ),
            "selected_raw_rows_sha256": self.selected_raw_rows_sha256,
            "normalized_event_spine_sha256": (
                self.normalized_event_spine_sha256
            ),
            "transform_code_sha256": self.transform_code_sha256,
        }


@dataclass(frozen=True)
class BookLevelV3:
    price_e4: int
    qty_e4: int

    def receipt(self) -> dict[str, int]:
        return {"price_e4": self.price_e4, "qty_e4": self.qty_e4}


@dataclass(frozen=True)
class VisibleFokLevelV3:
    price_e4: int
    qty_e4: int

    def receipt(self) -> dict[str, object]:
        return {
            "price_e4": self.price_e4,
            "qty_e4": self.qty_e4,
            "qty_fp": format(Decimal(self.qty_e4) / Decimal(10_000), "f"),
        }


@dataclass(frozen=True)
class NormalizedBookEventV3:
    kind: str
    side: str | None
    price_e4: int | None
    delta_e4: int | None
    yes_levels: tuple[BookLevelV3, ...]
    no_levels: tuple[BookLevelV3, ...]

    def receipt(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "side": self.side,
            "price_e4": self.price_e4,
            "delta_e4": self.delta_e4,
            "yes_levels": [item.receipt() for item in self.yes_levels],
            "no_levels": [item.receipt() for item in self.no_levels],
        }


@dataclass(frozen=True)
class AuthoritativeStateRowV3:
    state_index: int
    market_ticker: str
    source_date_utc: str
    recv_mono_ns: int
    recv_wall_ns: int
    atomic_no_precedence: bool
    atomic_source_rows_sha256: str
    source_events: tuple[SourceEventRefV3, ...]
    event_kinds: tuple[str, ...]
    event_count: int
    l2_event_count: int
    trade_event_count: int
    first_valid_snapshot: bool
    book_events: tuple[NormalizedBookEventV3, ...]
    observed_yes_price_e4_added: tuple[int, ...]
    observed_no_price_e4_added: tuple[int, ...]
    book_level_count_after: int
    visible_fok_ask_level_count_after: int
    visible_fok_bid_level_count_after: int
    book_state_sha256: str
    visible_fok_surface_sha256: str

    def receipt(self) -> dict[str, object]:
        return {
            "state_index": self.state_index,
            "market_ticker": self.market_ticker,
            "source_date_utc": self.source_date_utc,
            "recv_mono_ns": self.recv_mono_ns,
            "recv_wall_ns": self.recv_wall_ns,
            "atomic_no_precedence": self.atomic_no_precedence,
            "atomic_source_rows_sha256": self.atomic_source_rows_sha256,
            "source_events": [item.receipt() for item in self.source_events],
            "event_kinds": list(self.event_kinds),
            "event_count": self.event_count,
            "l2_event_count": self.l2_event_count,
            "trade_event_count": self.trade_event_count,
            "first_valid_snapshot": self.first_valid_snapshot,
            "book_events": [item.receipt() for item in self.book_events],
            "observed_yes_price_e4_added": list(
                self.observed_yes_price_e4_added
            ),
            "observed_no_price_e4_added": list(
                self.observed_no_price_e4_added
            ),
            "book_level_count_after": self.book_level_count_after,
            "visible_fok_ask_level_count_after": (
                self.visible_fok_ask_level_count_after
            ),
            "visible_fok_bid_level_count_after": (
                self.visible_fok_bid_level_count_after
            ),
            "book_state_sha256": self.book_state_sha256,
            "visible_fok_surface_sha256": (
                self.visible_fok_surface_sha256
            ),
        }


@dataclass(frozen=True)
class ReplayedAsOfStateV3:
    market_ticker: str
    source_state_index: int
    recv_mono_ns: int
    recv_wall_ns: int
    event_kinds: tuple[str, ...]
    yes_levels: tuple[BookLevelV3, ...]
    no_levels: tuple[BookLevelV3, ...]
    visible_fok_ask_levels: tuple[VisibleFokLevelV3, ...]
    visible_fok_bid_levels: tuple[VisibleFokLevelV3, ...]
    book_state_sha256: str
    visible_fok_surface_sha256: str


@dataclass(frozen=True)
class MarketCoverageV3:
    market_ticker: str
    source_date_utc: str
    first_valid_snapshot_recv_mono_ns: int
    first_valid_snapshot_recv_wall_ns: int
    state_row_count: int
    l2_event_count: int
    trade_event_count: int
    observed_yes_price_e4: tuple[int, ...]
    observed_no_price_e4: tuple[int, ...]
    initial_snapshot_is_first_event: bool = True
    continuous_book_usable: bool = True

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
            "state_row_count": self.state_row_count,
            "l2_event_count": self.l2_event_count,
            "trade_event_count": self.trade_event_count,
            "observed_yes_price_e4": list(
                self.observed_yes_price_e4
            ),
            "observed_no_price_e4": list(
                self.observed_no_price_e4
            ),
            "initial_snapshot_is_first_event": (
                self.initial_snapshot_is_first_event
            ),
            "continuous_book_usable": self.continuous_book_usable,
        }


@dataclass(frozen=True)
class AuthoritativeSourceResultV3:
    schema: str
    status: str
    contract_adapter_status: str
    content_root: str
    authority_sha256: str
    bound_manifest_sha256: str
    transform_code_components: tuple[TransformCodeComponentV3, ...]
    transform_code_sha256: str
    parent_reads: tuple[ParentEOFReadV3, ...]
    parent_spine_sha256: str
    state_rows: tuple[AuthoritativeStateRowV3, ...]
    state_row_count: int
    source_event_count: int
    state_spine_sha256: str
    market_coverage: tuple[MarketCoverageV3, ...]
    market_coverage_sha256: str
    terminal_l2_sha256: str
    transformation_root_sha256: str
    authoritative_source_transform: bool = True
    exact_parent_eof_coverage: bool = True
    durable_eligibility_chain_revalidated_in_v3: bool = False
    extraction_authorized: bool = False
    _factory_token: object = field(
        default=None,
        repr=False,
        compare=False,
    )


def _module_component(
    module: ModuleType,
    *,
    name: str,
) -> TransformCodeComponentV3:
    source = getattr(module, "__file__", None)
    if not isinstance(source, str):
        _fail("TRANSFORM_CODE_UNREADABLE", name)
    path = Path(source)
    if path.suffix in (".pyc", ".pyo"):
        path = path.with_suffix(".py")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail("TRANSFORM_CODE_UNREADABLE", f"{name}:{exc}")
    return TransformCodeComponentV3(
        module=name,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _transform_code_identity() -> tuple[
    tuple[TransformCodeComponentV3, ...],
    str,
]:
    import sys

    current = sys.modules[__name__]
    components = (
        _module_component(V1, name="round4_stage2_postfill_extractor_v1"),
        _module_component(V2, name="round4_stage2_postfill_extractor_v2"),
        _module_component(current, name="round4_stage2_postfill_extractor_v3"),
    )
    return components, V1.canonical_sha256(
        [item.receipt() for item in components]
    )


def _day_roster(day: str) -> frozenset[str]:
    return frozenset(
        ticker
        for ticker in V1.EXPECTED_ROSTER
        if V1.MARKET_SOURCE_DAY[ticker] == day
    )


def _count_all_source_rows(path: str, table: str) -> int:
    try:
        import duckdb
    except ImportError as exc:
        _fail("DUCKDB_UNAVAILABLE", str(exc))
    connection = duckdb.connect(":memory:")
    try:
        if table == "orderbooks_full":
            value = connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)",
                [path],
            ).fetchone()[0]
        elif table == "trades":
            value = connection.execute(
                """
                SELECT COUNT(*)
                FROM read_csv(
                    ?,
                    header = true,
                    types = {'taker_side': 'VARCHAR'}
                )
                """,
                [path],
            ).fetchone()[0]
        else:
            _fail("PARENT_TABLE_INVALID", table)
    except Stage2AuthoritativeV3Error:
        raise
    except Exception as exc:
        _fail("PARENT_ROW_READ_FAILED", f"{table}:{path}:{exc}")
    finally:
        connection.close()
    if type(value) is not int or value < 0:
        _fail("PARENT_ROW_READ_FAILED", f"{table}:invalid count")
    return value


def _read_selected_rows(
    parent: V2.BoundExactParentV2,
) -> tuple[dict[str, object], ...]:
    roster = _day_roster(parent.source_date_utc)
    if parent.table == "orderbooks_full":
        return V1.read_l2_parquet_rows(parent.physical_path, roster)
    if parent.table == "trades":
        return V1.read_trade_csv_rows(parent.physical_path, roster)
    _fail("PARENT_TABLE_INVALID", parent.table)


def _parent_key(day: str, table: str) -> tuple[str, str]:
    return day, table


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _fail("CONTROLLED_SNAPSHOT_WRITE_FAILED", "short write")
        view = view[written:]


def _capture_parent_to_controlled_snapshot(
    *,
    parent: V2.BoundExactParentV2,
    root_lexical: Path,
    snapshot_path: Path,
) -> tuple[int, str]:
    """Copy one verified descriptor stream into a private parser snapshot."""
    parts = V2._canonical_logical_source_key(parent.logical_source_key)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    source_descriptors: list[int] = []
    destination_descriptor: int | None = None
    try:
        current = os.open(
            os.fspath(root_lexical),
            os.O_RDONLY | directory | nofollow,
        )
        source_descriptors.append(current)
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | nofollow
            if index < len(parts) - 1:
                flags |= directory
            current = os.open(part, flags, dir_fd=current)
            source_descriptors.append(current)
        source_descriptor = source_descriptors[-1]
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            _fail("REGULAR_FILE_REQUIRED", parent.logical_source_key)

        destination_descriptor = os.open(
            os.fspath(snapshot_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_count += len(block)
            _write_all(destination_descriptor, block)
        os.fsync(destination_descriptor)
        os.fchmod(destination_descriptor, 0o400)
        final_source_stat = os.fstat(source_descriptor)
        if (
            source_stat.st_dev != final_source_stat.st_dev
            or source_stat.st_ino != final_source_stat.st_ino
            or source_stat.st_size != final_source_stat.st_size
            or byte_count != final_source_stat.st_size
        ):
            _fail(
                "PARENT_CHANGED_DURING_CAPTURE",
                parent.logical_source_key,
            )
    except Stage2AuthoritativeV3Error:
        raise
    except OSError as exc:
        _fail(
            "CONTROLLED_SNAPSHOT_FAILED",
            f"{parent.logical_source_key}:{exc}",
        )
    finally:
        if destination_descriptor is not None:
            try:
                os.close(destination_descriptor)
            except OSError:
                pass
        for descriptor in reversed(source_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
    actual_sha = digest.hexdigest()
    if byte_count != parent.size or actual_sha != parent.sha256:
        _fail(
            "PARENT_REBIND_FAILED",
            (
                f"{parent.logical_source_key}:"
                f"expected=({parent.size},{parent.sha256}) "
                f"captured=({byte_count},{actual_sha})"
            ),
        )
    return byte_count, actual_sha


def _hash_controlled_snapshot(path: Path) -> tuple[int, str]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("CONTROLLED_SNAPSHOT_INVALID", str(path))
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_count += len(block)
    except Stage2AuthoritativeV3Error:
        raise
    except OSError as exc:
        _fail("CONTROLLED_SNAPSHOT_INVALID", f"{path}:{exc}")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return byte_count, digest.hexdigest()


def _read_parent_to_eof(
    *,
    parent: V2.BoundExactParentV2,
    root_lexical: Path,
    root_resolved: Path,
    snapshot_path: Path,
    transform_code_sha256: str,
) -> tuple[ParentEOFReadV3, tuple[dict[str, object], ...]]:
    parts = V2._canonical_logical_source_key(parent.logical_source_key)
    captured_size, captured_sha = _capture_parent_to_controlled_snapshot(
        parent=parent,
        root_lexical=root_lexical,
        snapshot_path=snapshot_path,
    )
    parser_parent = replace(
        parent,
        physical_path=str(snapshot_path),
    )
    total_rows = _count_all_source_rows(
        parser_parent.physical_path,
        parent.table,
    )
    selected_rows = _read_selected_rows(parser_parent)
    parser_size_after, parser_sha_after = _hash_controlled_snapshot(
        snapshot_path
    )
    if (
        parser_size_after != captured_size
        or parser_sha_after != captured_sha
    ):
        _fail(
            "CONTROLLED_SNAPSHOT_CHANGED_DURING_PARSE",
            parent.logical_source_key,
        )

    second_path, second_size, second_sha = (
        V2._read_symlink_free_regular_file(
            root=root_lexical,
            root_resolved=root_resolved,
            parts=parts,
        )
    )
    if (
        str(second_path) != parent.physical_path
        or second_size != captured_size
        or second_sha != captured_sha
    ):
        _fail(
            "PARENT_CHANGED_DURING_READ",
            parent.logical_source_key,
        )
    if total_rows == 0 or len(selected_rows) == 0:
        _fail(
            "PARENT_ZERO_ROWS",
            (
                f"{parent.logical_source_key}:"
                f"source={total_rows} selected={len(selected_rows)}"
            ),
        )
    if len(selected_rows) > total_rows:
        _fail(
            "PARENT_ROW_COUNT_INVALID",
            parent.logical_source_key,
        )
    return (
        ParentEOFReadV3(
            source_date_utc=parent.source_date_utc,
            table=parent.table,
            logical_source_key=parent.logical_source_key,
            physical_path=parent.physical_path,
            version_id=parent.version_id,
            bound_sha256=parent.sha256,
            bound_size=parent.size,
            byte_count_per_eof_pass=captured_size,
            byte_eof_pass_count=2,
            byte_eof_reached=True,
            parser_input_from_controlled_snapshot=True,
            parser_snapshot_postparse_verified=True,
            parser_input_sha256=captured_sha,
            parser_input_size=captured_size,
            parent_total_row_count=total_rows,
            roster_selected_row_count=len(selected_rows),
            nonselected_row_count=total_rows - len(selected_rows),
            roster_selected_stream_exhausted=True,
            selected_raw_rows_sha256=V1.canonical_sha256(
                list(selected_rows)
            ),
            normalized_event_spine_sha256="0" * 64,
            transform_code_sha256=transform_code_sha256,
        ),
        selected_rows,
    )


def _book_levels(
    levels: Mapping[int, int],
) -> tuple[BookLevelV3, ...]:
    return tuple(
        BookLevelV3(price_e4=int(price), qty_e4=int(quantity))
        for price, quantity in sorted(levels.items())
    )


def _normalized_book_events(
    events: tuple[V1.SourceEvent, ...],
) -> tuple[NormalizedBookEventV3, ...]:
    result: list[NormalizedBookEventV3] = []
    for event in events:
        if event.kind == "BOOK_SNAPSHOT":
            result.append(
                NormalizedBookEventV3(
                    kind=event.kind,
                    side=None,
                    price_e4=None,
                    delta_e4=None,
                    yes_levels=tuple(
                        BookLevelV3(int(price), int(quantity))
                        for price, quantity in event.payload["yes_levels"]
                    ),
                    no_levels=tuple(
                        BookLevelV3(int(price), int(quantity))
                        for price, quantity in event.payload["no_levels"]
                    ),
                )
            )
        elif event.kind == "BOOK_DELTA":
            result.append(
                NormalizedBookEventV3(
                    kind=event.kind,
                    side=str(event.payload["side"]),
                    price_e4=int(event.payload["price_e4"]),
                    delta_e4=int(event.payload["delta_e4"]),
                    yes_levels=(),
                    no_levels=(),
                )
            )
    return tuple(result)


def _expected_fok_surfaces(
    yes_levels: tuple[BookLevelV3, ...],
    no_levels: tuple[BookLevelV3, ...],
) -> tuple[tuple[VisibleFokLevelV3, ...], tuple[VisibleFokLevelV3, ...]]:
    ask = tuple(
        VisibleFokLevelV3(item.price_e4, item.qty_e4)
        for item in sorted(
            (
                level
                for level in yes_levels
                if level.price_e4 % 100 == 0
            ),
            key=lambda item: item.price_e4,
            reverse=True,
        )
    )
    bid = tuple(
        sorted(
            (
                VisibleFokLevelV3(
                    10_000 - item.price_e4,
                    item.qty_e4,
                )
                for item in no_levels
                if (10_000 - item.price_e4) % 100 == 0
            ),
            key=lambda item: item.price_e4,
        )
    )
    return ask, bid


def _surface_sha256(
    ask: tuple[VisibleFokLevelV3, ...],
    bid: tuple[VisibleFokLevelV3, ...],
) -> str:
    return V1.canonical_sha256(
        {
            "ASK": [item.receipt() for item in ask],
            "BID": [item.receipt() for item in bid],
        }
    )


def _normalize_internal_events(
    *,
    raw_l2_rows: tuple[dict[str, object], ...],
    raw_trade_rows: tuple[dict[str, object], ...],
) -> tuple[V1.SourceEvent, ...]:
    l2 = tuple(
        V1.normalize_l2_row(
            raw,
            source_ordinal=index,
            roster=V1.EXPECTED_ROSTER_SET,
        )
        for index, raw in enumerate(
            V2._stable_raw_rows(raw_l2_rows, label="l2")
        )
    )
    trades = tuple(
        V1.normalize_trade_row(
            raw,
            source_ordinal=index,
            roster=V1.EXPECTED_ROSTER_SET,
        )
        for index, raw in enumerate(
            V2._stable_raw_rows(raw_trade_rows, label="trade")
        )
    )
    return (*l2, *trades)


def _state_rows_from_events(
    events: tuple[V1.SourceEvent, ...],
    *,
    parent_bindings: Mapping[tuple[str, str], V2.BoundExactParentV2],
    content_root: str,
) -> tuple[AuthoritativeStateRowV3, ...]:
    envelopes = V1.group_atomic_envelopes(events)
    replay = V1.CausalBookReconstructor(V1.EXPECTED_ROSTER_SET)
    rows: list[AuthoritativeStateRowV3] = []
    first_seen: set[str] = set()
    for state_index, envelope in enumerate(envelopes):
        ticker = envelope.market_ticker
        before_usable = replay.is_usable(ticker)
        replay.consume_atomic(envelope)
        after_usable = replay.is_usable(ticker)
        if not before_usable and not after_usable:
            _fail(
                "LATE_FIRST_SNAPSHOT",
                (
                    f"{ticker}:event before valid snapshot at "
                    f"{envelope.recv_mono_ns}"
                ),
            )
        if before_usable and not after_usable:
            _fail(
                "CAUSAL_BOOK_GAP",
                f"{ticker}:{envelope.recv_mono_ns}",
            )
        if not after_usable:
            _fail("CAUSAL_BOOK_GAP", ticker)

        is_first = ticker not in first_seen
        event_kinds = tuple(item.kind for item in envelope.events)
        if is_first and (
            before_usable
            or "BOOK_SNAPSHOT" not in event_kinds
        ):
            _fail(
                "LATE_FIRST_SNAPSHOT",
                f"{ticker}:first state is not a valid snapshot",
            )
        first_seen.add(ticker)
        book = replay.book(ticker)
        if (
            book["yes"]
            and book["no"]
            and max(book["yes"]) + max(book["no"]) >= 10_000
        ):
            _fail(
                "CROSSED_BOOK_STATE",
                f"{ticker}:{envelope.recv_mono_ns}",
            )
        yes = _book_levels(book["yes"])
        no = _book_levels(book["no"])
        ask, bid = _expected_fok_surfaces(yes, no)
        yes_added, no_added = V2._event_observed_prices(envelope.events)
        source_events = tuple(
            SourceEventRefV3(
                kind=item.kind,
                stable_source_id=item.stable_source_id,
                source_ordinal=item.source_ordinal,
                source_rows_sha256=item.source_rows_sha256,
                parent_logical_source_key=parent_bindings[
                    _parent_key(
                        V1.MARKET_SOURCE_DAY[ticker],
                        (
                            "trades"
                            if item.kind == "TRADE"
                            else "orderbooks_full"
                        ),
                    )
                ].logical_source_key,
                parent_version_id=parent_bindings[
                    _parent_key(
                        V1.MARKET_SOURCE_DAY[ticker],
                        (
                            "trades"
                            if item.kind == "TRADE"
                            else "orderbooks_full"
                        ),
                    )
                ].version_id,
                parent_sha256=parent_bindings[
                    _parent_key(
                        V1.MARKET_SOURCE_DAY[ticker],
                        (
                            "trades"
                            if item.kind == "TRADE"
                            else "orderbooks_full"
                        ),
                    )
                ].sha256,
                parent_size=parent_bindings[
                    _parent_key(
                        V1.MARKET_SOURCE_DAY[ticker],
                        (
                            "trades"
                            if item.kind == "TRADE"
                            else "orderbooks_full"
                        ),
                    )
                ].size,
                content_root=content_root,
            )
            for item in envelope.events
        )
        rows.append(
            AuthoritativeStateRowV3(
                state_index=state_index,
                market_ticker=ticker,
                source_date_utc=V1.MARKET_SOURCE_DAY[ticker],
                recv_mono_ns=envelope.recv_mono_ns,
                recv_wall_ns=envelope.recv_wall_ns,
                atomic_no_precedence=envelope.atomic_no_precedence,
                atomic_source_rows_sha256=envelope.source_rows_sha256,
                source_events=source_events,
                event_kinds=event_kinds,
                event_count=len(envelope.events),
                l2_event_count=sum(
                    item.kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
                    for item in envelope.events
                ),
                trade_event_count=sum(
                    item.kind == "TRADE" for item in envelope.events
                ),
                first_valid_snapshot=is_first,
                book_events=_normalized_book_events(envelope.events),
                observed_yes_price_e4_added=tuple(sorted(yes_added)),
                observed_no_price_e4_added=tuple(sorted(no_added)),
                book_level_count_after=len(yes) + len(no),
                visible_fok_ask_level_count_after=len(ask),
                visible_fok_bid_level_count_after=len(bid),
                book_state_sha256=V1.canonical_sha256(book),
                visible_fok_surface_sha256=_surface_sha256(ask, bid),
            )
        )
    return tuple(rows)


def _coverage_from_state_rows(
    rows: tuple[AuthoritativeStateRowV3, ...],
) -> tuple[MarketCoverageV3, ...]:
    grouped: dict[str, list[AuthoritativeStateRowV3]] = {
        ticker: [] for ticker in V1.EXPECTED_ROSTER
    }
    for row in rows:
        if row.market_ticker not in grouped:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                row.market_ticker,
            )
        grouped[row.market_ticker].append(row)
    coverage: list[MarketCoverageV3] = []
    for ticker in V1.EXPECTED_ROSTER:
        market_rows = grouped[ticker]
        if not market_rows:
            _fail("ROSTER_INCOMPLETE", f"{ticker}:no state rows")
        first = market_rows[0]
        if not first.first_valid_snapshot:
            _fail("LATE_FIRST_SNAPSHOT", ticker)
        if any(row.first_valid_snapshot for row in market_rows[1:]):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"{ticker}:multiple initial snapshots",
            )
        coverage.append(
            MarketCoverageV3(
                market_ticker=ticker,
                source_date_utc=V1.MARKET_SOURCE_DAY[ticker],
                first_valid_snapshot_recv_mono_ns=first.recv_mono_ns,
                first_valid_snapshot_recv_wall_ns=first.recv_wall_ns,
                state_row_count=len(market_rows),
                l2_event_count=sum(
                    row.l2_event_count for row in market_rows
                ),
                trade_event_count=sum(
                    row.trade_event_count for row in market_rows
                ),
                observed_yes_price_e4=tuple(
                    sorted(
                        {
                            price
                            for row in market_rows
                            for price in row.observed_yes_price_e4_added
                        }
                    )
                ),
                observed_no_price_e4=tuple(
                    sorted(
                        {
                            price
                            for row in market_rows
                            for price in row.observed_no_price_e4_added
                        }
                    )
                ),
            )
        )
    return tuple(coverage)


def _event_spines_by_parent(
    rows: tuple[AuthoritativeStateRowV3, ...],
) -> dict[tuple[str, str], tuple[str, ...]]:
    collected: dict[tuple[str, str], list[str]] = {
        _parent_key(day, table): []
        for day in V1.DISCOVERY_DATES
        for table in ("orderbooks_full", "trades")
    }
    for row in rows:
        for event in row.source_events:
            table = "trades" if event.kind == "TRADE" else "orderbooks_full"
            collected[_parent_key(row.source_date_utc, table)].append(
                V1.canonical_sha256(event.receipt())
            )
    return {
        key: tuple(values)
        for key, values in collected.items()
    }


def _transformation_root(
    *,
    content_root: str,
    authority_sha256: str,
    bound_manifest_sha256: str,
    transform_code_sha256: str,
    parent_spine_sha256: str,
    state_spine_sha256: str,
    market_coverage_sha256: str,
    terminal_l2_sha256: str,
    source_event_count: int,
) -> str:
    return V1.canonical_sha256(
        {
            "schema": RESULT_SCHEMA,
            "status": STATUS,
            "content_root": content_root,
            "authority_sha256": authority_sha256,
            "bound_manifest_sha256": bound_manifest_sha256,
            "transform_code_sha256": transform_code_sha256,
            "parent_spine_sha256": parent_spine_sha256,
            "state_spine_sha256": state_spine_sha256,
            "market_coverage_sha256": market_coverage_sha256,
            "terminal_l2_sha256": terminal_l2_sha256,
            "source_event_count": source_event_count,
            "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        }
    )


def _build_authoritative_source_result_v3(
    *,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
) -> AuthoritativeSourceResultV3:
    if type(authority) is not V1.SourcePreparationAuthority:
        _fail("AUTHORITY_TYPE_INVALID", repr(type(authority)))
    if type(input_manifest) is not V2.BoundInputManifestV2:
        _fail(
            "TYPED_BOUND_MANIFEST_REQUIRED",
            repr(type(input_manifest)),
        )
    try:
        rebound = V2.build_bound_input_manifest_v2(
            authority=authority,
            content_root=content_root,
        )
    except V2.Stage2PreparationV2Error as exc:
        _fail("PARENT_REBIND_FAILED", f"{exc.code}:{exc.detail}")
    if rebound != input_manifest:
        _fail(
            "BOUND_MANIFEST_REBIND_MISMATCH",
            (
                f"provided={input_manifest.payload_sha256} "
                f"rebound={rebound.payload_sha256}"
            ),
        )

    code_components, code_sha = _transform_code_identity()
    root_lexical = V2._lexical_absolute(content_root)
    root_resolved = Path(rebound.content_root)
    parent_reads: list[ParentEOFReadV3] = []
    l2_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(
        prefix="round4-stage2-v3-controlled-",
    ) as snapshot_directory:
        for index, parent in enumerate(rebound.parents):
            suffix = (
                ".parquet"
                if parent.table == "orderbooks_full"
                else ".csv.gz"
            )
            snapshot_path = (
                Path(snapshot_directory) / f"parent-{index}{suffix}"
            )
            read_receipt, selected = _read_parent_to_eof(
                parent=parent,
                root_lexical=root_lexical,
                root_resolved=root_resolved,
                snapshot_path=snapshot_path,
                transform_code_sha256=code_sha,
            )
            parent_reads.append(read_receipt)
            if parent.table == "orderbooks_full":
                l2_rows.extend(selected)
            else:
                trade_rows.extend(selected)

    events = _normalize_internal_events(
        raw_l2_rows=tuple(l2_rows),
        raw_trade_rows=tuple(trade_rows),
    )
    parent_bindings = {
        _parent_key(parent.source_date_utc, parent.table): parent
        for parent in rebound.parents
    }
    state_rows = _state_rows_from_events(
        events,
        parent_bindings=parent_bindings,
        content_root=rebound.content_root,
    )

    # Independent V2 parity check from the internally read rows.  No V2
    # result or caller coverage enters this API.
    causal = V2.prepare_causal_result(
        raw_l2_rows=tuple(l2_rows),
        raw_trade_rows=tuple(trade_rows),
    )
    if len(causal.causal_rows) != len(state_rows):
        _fail(
            "CAUSAL_PIPELINE_PARITY_FAILED",
            "envelope count differs",
        )
    for state, causal_row in zip(state_rows, causal.causal_rows):
        if (
            state.market_ticker != causal_row.market_ticker
            or state.recv_mono_ns != causal_row.recv_mono_ns
            or state.recv_wall_ns != causal_row.recv_wall_ns
            or state.atomic_source_rows_sha256
            != causal_row.atomic_source_rows_sha256
            or state.book_state_sha256
            != causal_row.book_state_sha256_after
            or state.event_count != causal_row.event_count
        ):
            _fail(
                "CAUSAL_PIPELINE_PARITY_FAILED",
                f"state={state.state_index}",
            )

    event_spines = _event_spines_by_parent(state_rows)
    updated_parent_reads: list[ParentEOFReadV3] = []
    for parent in parent_reads:
        refs = event_spines[
            _parent_key(parent.source_date_utc, parent.table)
        ]
        if len(refs) != parent.roster_selected_row_count:
            _fail(
                "PARENT_EVENT_COUNT_MISMATCH",
                parent.logical_source_key,
            )
        updated_parent_reads.append(
            replace(
                parent,
                normalized_event_spine_sha256=V1.canonical_sha256(
                    list(refs)
                ),
            )
        )
    parents = tuple(updated_parent_reads)
    coverage = _coverage_from_state_rows(state_rows)
    if len(coverage) != 72:
        _fail("ROSTER_INCOMPLETE", str(len(coverage)))

    authority_sha = V1.canonical_sha256(authority.receipt())
    parent_spine = V1.canonical_sha256(
        [item.receipt() for item in parents]
    )
    state_spine = V1.canonical_sha256(
        [item.receipt() for item in state_rows]
    )
    coverage_sha = V1.canonical_sha256(
        [item.receipt() for item in coverage]
    )
    terminal_l2_rows, terminal_l2_sha = _terminal_l2_receipt_rows(
        state_rows
    )
    del terminal_l2_rows
    source_event_count = sum(item.event_count for item in state_rows)
    transformation_root = _transformation_root(
        content_root=rebound.content_root,
        authority_sha256=authority_sha,
        bound_manifest_sha256=rebound.payload_sha256,
        transform_code_sha256=code_sha,
        parent_spine_sha256=parent_spine,
        state_spine_sha256=state_spine,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_l2_sha,
        source_event_count=source_event_count,
    )
    return AuthoritativeSourceResultV3(
        schema=RESULT_SCHEMA,
        status=STATUS,
        contract_adapter_status=CONTRACT_ADAPTER_STATUS,
        content_root=rebound.content_root,
        authority_sha256=authority_sha,
        bound_manifest_sha256=rebound.payload_sha256,
        transform_code_components=code_components,
        transform_code_sha256=code_sha,
        parent_reads=parents,
        parent_spine_sha256=parent_spine,
        state_rows=state_rows,
        state_row_count=len(state_rows),
        source_event_count=source_event_count,
        state_spine_sha256=state_spine,
        market_coverage=coverage,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_l2_sha,
        transformation_root_sha256=transformation_root,
        _factory_token=_FACTORY_TOKEN,
    )


def build_authoritative_source_result_v3(
    *,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
) -> AuthoritativeSourceResultV3:
    """Read and transform the six exact parents; accept no derived inputs."""
    try:
        return _build_authoritative_source_result_v3(
            authority=authority,
            input_manifest=input_manifest,
            content_root=content_root,
        )
    except Stage2AuthoritativeV3Error:
        raise
    except V2.Stage2PreparationV2Error as exc:
        _fail(exc.code, exc.detail)
    except V1.Stage2ExtractionError as exc:
        _fail(exc.code, exc.detail)


def _validate_book_levels(
    levels: tuple[BookLevelV3, ...],
    *,
    label: str,
) -> dict[int, int]:
    if not isinstance(levels, tuple):
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", f"{label}:type")
    result: dict[int, int] = {}
    for item in levels:
        if (
            type(item) is not BookLevelV3
            or type(item.price_e4) is not int
            or not V1.is_legal_observed_price(item.price_e4)
            or type(item.qty_e4) is not int
            or item.qty_e4 <= 0
            or item.price_e4 in result
        ):
            _fail("AUTHORITATIVE_RESULT_INCONSISTENT", label)
        result[item.price_e4] = item.qty_e4
    if tuple(sorted(result)) != tuple(item.price_e4 for item in levels):
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", f"{label}:order")
    return result


def _validate_state_row(
    row: AuthoritativeStateRowV3,
    *,
    expected_index: int,
) -> None:
    if type(row) is not AuthoritativeStateRowV3:
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "state row type")
    if (
        type(row.state_index) is not int
        or row.state_index != expected_index
        or row.market_ticker not in V1.EXPECTED_ROSTER_SET
        or row.source_date_utc != V1.MARKET_SOURCE_DAY[row.market_ticker]
        or type(row.recv_mono_ns) is not int
        or row.recv_mono_ns <= 0
        or type(row.recv_wall_ns) is not int
        or row.recv_wall_ns <= 0
        or row.atomic_no_precedence is not True
        or type(row.event_count) is not int
        or row.event_count <= 0
        or type(row.l2_event_count) is not int
        or row.l2_event_count < 0
        or type(row.trade_event_count) is not int
        or row.trade_event_count < 0
        or not isinstance(row.source_events, tuple)
        or not isinstance(row.event_kinds, tuple)
        or not isinstance(row.book_events, tuple)
        or row.event_count != len(row.source_events)
        or row.event_count != len(row.event_kinds)
        or row.event_count
        != row.l2_event_count + row.trade_event_count
        or tuple(item.kind for item in row.source_events)
        != row.event_kinds
        or type(row.first_valid_snapshot) is not bool
        or len(row.book_events) != row.l2_event_count
        or tuple(item.kind for item in row.book_events)
        != tuple(
            kind
            for kind in row.event_kinds
            if kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
        )
        or type(row.book_level_count_after) is not int
        or row.book_level_count_after <= 0
        or type(row.visible_fok_ask_level_count_after) is not int
        or row.visible_fok_ask_level_count_after < 0
        or type(row.visible_fok_bid_level_count_after) is not int
        or row.visible_fok_bid_level_count_after < 0
        or not isinstance(row.observed_yes_price_e4_added, tuple)
        or not isinstance(row.observed_no_price_e4_added, tuple)
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{expected_index}",
        )
    for event in row.source_events:
        if (
            type(event) is not SourceEventRefV3
            or event.kind not in ("BOOK_SNAPSHOT", "BOOK_DELTA", "TRADE")
            or not event.stable_source_id
            or type(event.source_ordinal) is not int
            or event.source_ordinal < 0
            or not event.parent_logical_source_key
            or not event.parent_version_id
            or type(event.parent_size) is not int
            or event.parent_size <= 0
            or not event.content_root
        ):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"state:{expected_index}:event",
            )
        _require_sha256(
            event.source_rows_sha256,
            f"state:{expected_index}:event-sha",
        )
        _require_sha256(
            event.parent_sha256,
            f"state:{expected_index}:parent-sha",
        )
    if (
        sum(
            kind in ("BOOK_SNAPSHOT", "BOOK_DELTA")
            for kind in row.event_kinds
        )
        != row.l2_event_count
        or sum(kind == "TRADE" for kind in row.event_kinds)
        != row.trade_event_count
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{expected_index}:event-counts",
        )
    for event_index, event in enumerate(row.book_events):
        if type(event) is not NormalizedBookEventV3:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"state:{expected_index}:book-event-type",
            )
        if event.kind == "BOOK_SNAPSHOT":
            if (
                event.side is not None
                or event.price_e4 is not None
                or event.delta_e4 is not None
            ):
                _fail(
                    "AUTHORITATIVE_RESULT_INCONSISTENT",
                    f"state:{expected_index}:snapshot-fields",
                )
            yes = _validate_book_levels(
                event.yes_levels,
                label=f"state:{expected_index}:snapshot-yes",
            )
            no = _validate_book_levels(
                event.no_levels,
                label=f"state:{expected_index}:snapshot-no",
            )
            if not yes and not no:
                _fail(
                    "AUTHORITATIVE_RESULT_INCONSISTENT",
                    f"state:{expected_index}:empty-snapshot",
                )
        elif event.kind == "BOOK_DELTA":
            if (
                event.side not in ("yes", "no")
                or type(event.price_e4) is not int
                or not V1.is_legal_observed_price(event.price_e4)
                or type(event.delta_e4) is not int
                or event.delta_e4 == 0
                or event.yes_levels
                or event.no_levels
            ):
                _fail(
                    "AUTHORITATIVE_RESULT_INCONSISTENT",
                    f"state:{expected_index}:delta:{event_index}",
                )
        else:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"state:{expected_index}:book-event-kind",
            )
    for price in (
        *row.observed_yes_price_e4_added,
        *row.observed_no_price_e4_added,
    ):
        if type(price) is not int or not V1.is_legal_observed_price(price):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"state:{expected_index}:observed-price",
            )
    _require_sha256(
        row.atomic_source_rows_sha256,
        f"state:{expected_index}:atomic-sha",
    )
    _require_sha256(
        row.book_state_sha256,
        f"state:{expected_index}:book-sha",
    )
    _require_sha256(
        row.visible_fok_surface_sha256,
        f"state:{expected_index}:fok-sha",
    )


def _apply_and_verify_book_row(
    row: AuthoritativeStateRowV3,
    prior_book: dict[str, dict[int, int]] | None,
) -> tuple[dict[str, dict[int, int]], ReplayedAsOfStateV3]:
    snapshots = [
        event
        for event in row.book_events
        if event.kind == "BOOK_SNAPSHOT"
    ]
    deltas = [
        event
        for event in row.book_events
        if event.kind == "BOOK_DELTA"
    ]
    if len(snapshots) > 1 or (snapshots and deltas):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{row.state_index}:ambiguous-book-tie",
        )
    is_initial = prior_book is None
    if row.first_valid_snapshot is not is_initial:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{row.state_index}:initial-marker",
        )
    if snapshots:
        snapshot = snapshots[0]
        book = {
            "yes": {
                item.price_e4: item.qty_e4
                for item in snapshot.yes_levels
            },
            "no": {
                item.price_e4: item.qty_e4
                for item in snapshot.no_levels
            },
        }
    else:
        if prior_book is None:
            _fail(
                "LATE_FIRST_SNAPSHOT",
                f"{row.market_ticker}:state:{row.state_index}",
            )
        book = {
            "yes": dict(prior_book["yes"]),
            "no": dict(prior_book["no"]),
        }
        aggregated: dict[tuple[str, int], int] = {}
        for event in deltas:
            key = (str(event.side), int(event.price_e4))
            aggregated[key] = aggregated.get(key, 0) + int(
                event.delta_e4
            )
        for (side, price), delta in sorted(aggregated.items()):
            updated = book[side].get(price, 0) + delta
            if updated < 0:
                _fail(
                    "AUTHORITATIVE_RESULT_INCONSISTENT",
                    f"state:{row.state_index}:negative-level",
                )
            if updated == 0:
                book[side].pop(price, None)
            else:
                book[side][price] = updated

    if not book["yes"] and not book["no"]:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{row.state_index}:book-gap",
        )
    if (
        book["yes"]
        and book["no"]
        and max(book["yes"]) + max(book["no"]) >= 10_000
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{row.state_index}:crossed-book",
        )
    yes = _book_levels(book["yes"])
    no = _book_levels(book["no"])
    ask, bid = _expected_fok_surfaces(yes, no)
    book_sha = V1.canonical_sha256(book)
    surface_sha = _surface_sha256(ask, bid)
    if (
        row.book_level_count_after != len(yes) + len(no)
        or row.visible_fok_ask_level_count_after != len(ask)
        or row.visible_fok_bid_level_count_after != len(bid)
        or row.book_state_sha256 != book_sha
        or row.visible_fok_surface_sha256 != surface_sha
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            f"state:{row.state_index}:replay-parity",
        )
    replayed = ReplayedAsOfStateV3(
        market_ticker=row.market_ticker,
        source_state_index=row.state_index,
        recv_mono_ns=row.recv_mono_ns,
        recv_wall_ns=row.recv_wall_ns,
        event_kinds=row.event_kinds,
        yes_levels=yes,
        no_levels=no,
        visible_fok_ask_levels=ask,
        visible_fok_bid_levels=bid,
        book_state_sha256=book_sha,
        visible_fok_surface_sha256=surface_sha,
    )
    return book, replayed


def _terminal_l2_receipt_rows(
    rows: tuple[AuthoritativeStateRowV3, ...],
) -> tuple[list[dict[str, object]], str]:
    books: dict[str, dict[str, dict[int, int]]] = {}
    latest: dict[str, ReplayedAsOfStateV3] = {}
    for row in rows:
        books[row.market_ticker], latest[row.market_ticker] = (
            _apply_and_verify_book_row(
                row,
                books.get(row.market_ticker),
            )
        )
    if set(latest) != V1.EXPECTED_ROSTER_SET:
        _fail(
            "ROSTER_INCOMPLETE",
            f"terminal-l2={len(latest)} expected=72",
        )
    payload: list[dict[str, object]] = []
    for ticker in V1.EXPECTED_ROSTER:
        state = latest[ticker]
        payload.append(
            {
                "market_ticker": ticker,
                "source_date_utc": V1.MARKET_SOURCE_DAY[ticker],
                "source_state_index": state.source_state_index,
                "recv_mono_ns": state.recv_mono_ns,
                "recv_wall_ns": state.recv_wall_ns,
                "yes_levels": [
                    item.receipt() for item in state.yes_levels
                ],
                "no_levels": [
                    item.receipt() for item in state.no_levels
                ],
                "visible_fok_ask_levels": [
                    item.receipt()
                    for item in state.visible_fok_ask_levels
                ],
                "visible_fok_bid_levels": [
                    item.receipt()
                    for item in state.visible_fok_bid_levels
                ],
                "book_state_sha256": state.book_state_sha256,
                "visible_fok_surface_sha256": (
                    state.visible_fok_surface_sha256
                ),
            }
        )
    return payload, V1.canonical_sha256(payload)


def _validate_parent_reads(
    result: AuthoritativeSourceResultV3,
) -> str:
    if len(result.parent_reads) != 6:
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "parent count")
    expected_keys = {
        (
            day,
            table,
            logical_key,
        )
        for day in V1.DISCOVERY_DATES
        for table, logical_key in V1.required_btc_fact_keys(day).items()
    }
    actual_keys: set[tuple[str, str, str]] = set()
    for parent in result.parent_reads:
        if type(parent) is not ParentEOFReadV3:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                "parent type",
            )
        actual_keys.add(
            (
                parent.source_date_utc,
                parent.table,
                parent.logical_source_key,
            )
        )
        try:
            logical_parts = V2._canonical_logical_source_key(
                parent.logical_source_key
            )
        except V2.Stage2PreparationV2Error as exc:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"parent-path:{exc.code}",
            )
        expected_physical_path = str(
            Path(result.content_root).joinpath(*logical_parts)
        )
        if (
            not parent.version_id
            or parent.physical_path != expected_physical_path
            or parent.byte_eof_reached is not True
            or parent.parser_input_from_controlled_snapshot is not True
            or parent.parser_snapshot_postparse_verified is not True
            or parent.parser_input_sha256 != parent.bound_sha256
            or parent.parser_input_size != parent.bound_size
            or parent.roster_selected_stream_exhausted is not True
            or parent.byte_eof_pass_count != 2
            or parent.byte_count_per_eof_pass != parent.bound_size
            or type(parent.bound_size) is not int
            or parent.bound_size <= 0
            or type(parent.parent_total_row_count) is not int
            or parent.parent_total_row_count <= 0
            or type(parent.roster_selected_row_count) is not int
            or parent.roster_selected_row_count <= 0
            or parent.roster_selected_row_count
            > parent.parent_total_row_count
            or parent.nonselected_row_count
            != (
                parent.parent_total_row_count
                - parent.roster_selected_row_count
            )
            or parent.transform_code_sha256
            != result.transform_code_sha256
        ):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"parent:{parent.logical_source_key}",
            )
        for value, label in (
            (parent.bound_sha256, "bound"),
            (parent.selected_raw_rows_sha256, "raw rows"),
            (parent.normalized_event_spine_sha256, "event spine"),
        ):
            _require_sha256(value, f"{parent.logical_source_key}:{label}")
    if actual_keys != expected_keys:
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "parent identities")
    return V1.canonical_sha256(
        [item.receipt() for item in result.parent_reads]
    )


def _validate_result_header(
    result: AuthoritativeSourceResultV3,
) -> None:
    if type(result) is not AuthoritativeSourceResultV3:
        _fail("TYPED_RESULT_REQUIRED", repr(type(result)))
    if result._factory_token is not _FACTORY_TOKEN:
        _fail("TYPED_RESULT_REQUIRED", "not emitted by V3 transform")
    if (
        result.schema != RESULT_SCHEMA
        or result.status != STATUS
        or result.contract_adapter_status != CONTRACT_ADAPTER_STATUS
        or result.authoritative_source_transform is not True
        or result.exact_parent_eof_coverage is not True
        or result.durable_eligibility_chain_revalidated_in_v3 is not False
        or result.extraction_authorized is not False
    ):
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "header")
    for value, label in (
        (result.authority_sha256, "authority"),
        (result.bound_manifest_sha256, "bound manifest"),
        (result.transform_code_sha256, "transform code"),
        (result.parent_spine_sha256, "parent spine"),
        (result.state_spine_sha256, "state spine"),
        (result.market_coverage_sha256, "market coverage"),
        (result.terminal_l2_sha256, "terminal L2"),
        (result.transformation_root_sha256, "transformation root"),
    ):
        _require_sha256(value, label)


def build_authoritative_source_receipt_v3(
    result: AuthoritativeSourceResultV3,
) -> dict[str, object]:
    """Recompute books, FOK surfaces, coverage, spines, and transform root."""
    _validate_result_header(result)
    code_components, code_sha = _transform_code_identity()
    if (
        result.transform_code_components != code_components
        or result.transform_code_sha256 != code_sha
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "transform code identity",
        )
    parent_spine = _validate_parent_reads(result)
    if parent_spine != result.parent_spine_sha256:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "parent spine",
        )

    if not result.state_rows:
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "empty state rows")
    prior_global: tuple[int, int, str] | None = None
    prior_market_clock: dict[str, tuple[int, int]] = {}
    replay_books: dict[str, dict[str, dict[int, int]]] = {}
    for index, row in enumerate(result.state_rows):
        _validate_state_row(row, expected_index=index)
        global_clock = (
            row.recv_mono_ns,
            row.recv_wall_ns,
            row.market_ticker,
        )
        if prior_global is not None and global_clock < prior_global:
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"global clock:{index}",
            )
        prior_global = global_clock
        prior = prior_market_clock.get(row.market_ticker)
        if prior is not None and (
            row.recv_mono_ns <= prior[0]
            or row.recv_wall_ns <= prior[1]
        ):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"market clock:{index}",
            )
        prior_market_clock[row.market_ticker] = (
            row.recv_mono_ns,
            row.recv_wall_ns,
        )
        replay_books[row.market_ticker], _ = _apply_and_verify_book_row(
            row,
            replay_books.get(row.market_ticker),
        )

    state_spine = V1.canonical_sha256(
        [item.receipt() for item in result.state_rows]
    )
    source_event_count = sum(
        item.event_count for item in result.state_rows
    )
    if (
        result.state_row_count != len(result.state_rows)
        or result.source_event_count != source_event_count
        or result.state_spine_sha256 != state_spine
        or result.source_event_count
        != sum(
            item.roster_selected_row_count
            for item in result.parent_reads
        )
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "state count/spine",
        )

    event_spines = _event_spines_by_parent(result.state_rows)
    parents_by_key = {
        _parent_key(parent.source_date_utc, parent.table): parent
        for parent in result.parent_reads
    }
    ordinals_by_table: dict[str, list[int]] = {
        "orderbooks_full": [],
        "trades": [],
    }
    for row in result.state_rows:
        for event in row.source_events:
            table = (
                "trades"
                if event.kind == "TRADE"
                else "orderbooks_full"
            )
            parent = parents_by_key[
                _parent_key(row.source_date_utc, table)
            ]
            if (
                event.parent_logical_source_key
                != parent.logical_source_key
                or event.parent_version_id != parent.version_id
                or event.parent_sha256 != parent.bound_sha256
                or event.parent_size != parent.bound_size
                or event.content_root != result.content_root
            ):
                _fail(
                    "AUTHORITATIVE_RESULT_INCONSISTENT",
                    f"event parent provenance:{row.state_index}",
                )
            ordinals_by_table[table].append(event.source_ordinal)
    for table, ordinals in ordinals_by_table.items():
        if sorted(ordinals) != list(range(len(ordinals))):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"source ordinals:{table}",
            )
    for parent in result.parent_reads:
        refs = event_spines[
            _parent_key(parent.source_date_utc, parent.table)
        ]
        if (
            len(refs) != parent.roster_selected_row_count
            or V1.canonical_sha256(list(refs))
            != parent.normalized_event_spine_sha256
        ):
            _fail(
                "AUTHORITATIVE_RESULT_INCONSISTENT",
                f"parent event spine:{parent.logical_source_key}",
            )

    coverage = _coverage_from_state_rows(result.state_rows)
    coverage_sha = V1.canonical_sha256(
        [item.receipt() for item in coverage]
    )
    if (
        result.market_coverage != coverage
        or result.market_coverage_sha256 != coverage_sha
        or len(coverage) != 72
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "market coverage",
        )
    markets_per_day = {day: 0 for day in V1.DISCOVERY_DATES}
    for item in coverage:
        markets_per_day[item.source_date_utc] += 1
    if markets_per_day != {day: 24 for day in V1.DISCOVERY_DATES}:
        _fail("ROSTER_INCOMPLETE", repr(markets_per_day))
    terminal_l2_rows, terminal_l2_sha = _terminal_l2_receipt_rows(
        result.state_rows
    )
    if result.terminal_l2_sha256 != terminal_l2_sha:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "terminal L2 spine",
        )

    root = _transformation_root(
        content_root=result.content_root,
        authority_sha256=result.authority_sha256,
        bound_manifest_sha256=result.bound_manifest_sha256,
        transform_code_sha256=code_sha,
        parent_spine_sha256=parent_spine,
        state_spine_sha256=state_spine,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_l2_sha,
        source_event_count=source_event_count,
    )
    if result.transformation_root_sha256 != root:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "transformation root",
        )

    payload: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "status": STATUS,
        "authoritative_source_transform": True,
        "exact_parent_eof_coverage": True,
        "authority_trust_boundary": (
            "TYPED_V1_AUTHORITY_REBOUND_TO_TYPED_V2_MANIFEST"
        ),
        "durable_eligibility_chain_revalidated_in_v3": False,
        "content_root": result.content_root,
        "authority_sha256": result.authority_sha256,
        "bound_manifest_sha256": result.bound_manifest_sha256,
        "transform_code_sha256": code_sha,
        "transform_code_components": [
            item.receipt() for item in code_components
        ],
        "parent_count": len(result.parent_reads),
        "parent_spine_sha256": parent_spine,
        "parent_reads": [
            item.receipt() for item in result.parent_reads
        ],
        "parent_row_scope": {
            "parent_total_rows": "COUNT_ONLY",
            "roster_selected_rows": "PARSED_AND_NORMALIZED",
            "nonselected_rows_parsed_claimed": False,
        },
        "source_event_count": source_event_count,
        "state_row_count": len(result.state_rows),
        "state_spine_sha256": state_spine,
        "complete_normalized_replay_spine_exported": True,
        "normalized_replay_rows": [
            item.receipt() for item in result.state_rows
        ],
        "market_count": len(coverage),
        "markets_per_day": markets_per_day,
        "market_coverage_sha256": coverage_sha,
        "market_coverage": [item.receipt() for item in coverage],
        "terminal_l2_market_count": len(terminal_l2_rows),
        "terminal_l2_sha256": terminal_l2_sha,
        "terminal_l2_states": terminal_l2_rows,
        "transformation_root_sha256": root,
        "contract_adapter_status": CONTRACT_ADAPTER_STATUS,
        "v4_2_contract_coverage_claimed": False,
        "extraction_authorized": False,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "shadow_authorized": False,
        "live_authorized": False,
        "terminal_label_firewall": {
            "terminal_result_role": "LABEL_ONLY",
            "terminal_result_feature_use": False,
            "terminal_join_requires_sealed_decision_action_spines": True,
        },
    }
    payload["payload_sha256"] = V1.canonical_sha256(payload)
    return payload


def visible_fok_levels_v3(
    state: ReplayedAsOfStateV3,
    *,
    flatten_book_side: str,
    limit_price_e4: int,
) -> tuple[VisibleFokLevelV3, ...]:
    """Recompute the visible whole-cent FOK levels from full typed L2."""
    if type(state) is not ReplayedAsOfStateV3:
        _fail("TYPED_REPLAYED_STATE_REQUIRED", repr(type(state)))
    if (
        type(limit_price_e4) is not int
        or not V1.is_legal_observed_price(limit_price_e4)
        or limit_price_e4 % 100 != 0
    ):
        _fail("FOK_LIMIT_SENSITIVITY_ONLY", repr(limit_price_e4))
    ask, bid = _expected_fok_surfaces(state.yes_levels, state.no_levels)
    if (
        ask != state.visible_fok_ask_levels
        or bid != state.visible_fok_bid_levels
        or state.visible_fok_surface_sha256 != _surface_sha256(ask, bid)
    ):
        _fail("AUTHORITATIVE_RESULT_INCONSISTENT", "FOK surface")
    if flatten_book_side == "ASK":
        return tuple(
            item for item in ask if item.price_e4 >= limit_price_e4
        )
    if flatten_book_side == "BID":
        return tuple(
            item for item in bid if item.price_e4 <= limit_price_e4
        )
    _fail("FOK_BOOK_SIDE_INVALID", flatten_book_side)


def _validate_query_result_integrity(
    result: AuthoritativeSourceResultV3,
) -> None:
    _validate_result_header(result)
    state_spine = V1.canonical_sha256(
        [item.receipt() for item in result.state_rows]
    )
    if (
        result.state_row_count != len(result.state_rows)
        or result.state_spine_sha256 != state_spine
        or result.source_event_count
        != sum(item.event_count for item in result.state_rows)
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "query state spine",
        )
    coverage = _coverage_from_state_rows(result.state_rows)
    coverage_sha = V1.canonical_sha256(
        [item.receipt() for item in coverage]
    )
    if (
        result.market_coverage != coverage
        or result.market_coverage_sha256 != coverage_sha
    ):
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "query market coverage",
        )
    _terminal_rows, terminal_l2_sha = _terminal_l2_receipt_rows(
        result.state_rows
    )
    if result.terminal_l2_sha256 != terminal_l2_sha:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "query terminal L2",
        )
    parent_spine = _validate_parent_reads(result)
    if parent_spine != result.parent_spine_sha256:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "query parent spine",
        )
    root = _transformation_root(
        content_root=result.content_root,
        authority_sha256=result.authority_sha256,
        bound_manifest_sha256=result.bound_manifest_sha256,
        transform_code_sha256=result.transform_code_sha256,
        parent_spine_sha256=parent_spine,
        state_spine_sha256=state_spine,
        market_coverage_sha256=coverage_sha,
        terminal_l2_sha256=terminal_l2_sha,
        source_event_count=result.source_event_count,
    )
    if root != result.transformation_root_sha256:
        _fail(
            "AUTHORITATIVE_RESULT_INCONSISTENT",
            "query transformation root",
        )


def latest_asof_state_v3(
    result: AuthoritativeSourceResultV3,
    *,
    market_ticker: str,
    decision_recv_mono_ns: int,
    decision_recv_wall_ns: int,
) -> ReplayedAsOfStateV3:
    """Return the latest atomic state strictly pre-effective by mono clock.

    ``recv_mono_ns`` is the ordering authority.  ``recv_wall_ns`` is only a
    consistency attestation; it never independently admits a row.
    """
    _validate_query_result_integrity(result)
    if market_ticker not in V1.EXPECTED_ROSTER_SET:
        _fail("MARKET_OUTSIDE_ROSTER", market_ticker)
    if (
        type(decision_recv_mono_ns) is not int
        or decision_recv_mono_ns <= 0
        or type(decision_recv_wall_ns) is not int
        or decision_recv_wall_ns <= 0
    ):
        _fail("DECISION_CLOCK_INVALID", market_ticker)
    book: dict[str, dict[int, int]] | None = None
    latest: ReplayedAsOfStateV3 | None = None
    for row in result.state_rows:
        if row.market_ticker != market_ticker:
            continue
        mono_before = row.recv_mono_ns < decision_recv_mono_ns
        mono_after = row.recv_mono_ns > decision_recv_mono_ns
        wall_before = row.recv_wall_ns < decision_recv_wall_ns
        wall_after = row.recv_wall_ns > decision_recv_wall_ns
        if (
            (mono_before and wall_after)
            or (mono_after and wall_before)
        ):
            _fail(
                "DECISION_CLOCK_DIVERGENCE",
                (
                    f"{market_ticker}:decision="
                    f"{(decision_recv_mono_ns, decision_recv_wall_ns)} "
                    f"source={(row.recv_mono_ns, row.recv_wall_ns)}"
                ),
            )
        if mono_before:
            _validate_state_row(row, expected_index=row.state_index)
            book, latest = _apply_and_verify_book_row(row, book)
    if latest is None:
        _fail(
            "NO_LATEST_ASOF_STATE",
            (
                f"{market_ticker}:"
                f"{decision_recv_mono_ns}:{decision_recv_wall_ns}"
            ),
        )
    return latest


def reverify_authoritative_source_result_v3(
    *,
    prior_result: AuthoritativeSourceResultV3,
    authority: V1.SourcePreparationAuthority,
    input_manifest: V2.BoundInputManifestV2,
    content_root: Path | str,
) -> dict[str, object]:
    """Rebuild from the six parents immediately before an external commit."""
    _validate_result_header(prior_result)
    rebuilt = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=input_manifest,
        content_root=content_root,
    )
    if rebuilt != prior_result:
        _fail(
            "COMMIT_TIME_REVERIFY_MISMATCH",
            (
                f"prior={prior_result.transformation_root_sha256} "
                f"rebuilt={rebuilt.transformation_root_sha256}"
            ),
        )
    receipt = build_authoritative_source_receipt_v3(rebuilt)
    receipt["commit_time_reverified"] = True
    receipt["payload_sha256"] = V1.canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "payload_sha256"
        }
    )
    return receipt


def _load_json_receipt(
    path: Path | str,
    *,
    label: str,
    expected_sha256: str,
) -> Mapping[str, object]:
    _require_sha256(expected_sha256, f"{label}:expected receipt sha")
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        _fail("RECEIPT_UNREADABLE", f"{label}:{exc}")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        _fail(
            "RECEIPT_PIN_MISMATCH",
            (
                f"{label}:expected={expected_sha256} "
                f"actual={actual_sha256}"
            ),
        )
    try:
        return V1._load_json_bytes(payload, label)
    except V1.Stage2ExtractionError as exc:
        _fail(exc.code, exc.detail)


def load_source_authority_receipt_v3(
    path: Path | str,
    *,
    expected_sha256: str,
) -> V1.SourcePreparationAuthority:
    """Load the deterministic JSON emitted by ``V1 authority.receipt()``."""
    raw = _load_json_receipt(
        path,
        label="source-authority",
        expected_sha256=expected_sha256,
    )
    objects_raw = raw.get("exact_version_objects")
    days_raw = raw.get("days")
    source_dates = raw.get("source_dates")
    if (
        not isinstance(objects_raw, list)
        or not isinstance(days_raw, list)
        or not isinstance(source_dates, list)
    ):
        _fail("AUTHORITY_RECEIPT_INVALID", "lists missing")
    try:
        objects = tuple(
            V1.EligibleExactVersionObject(
                source_date_utc=str(item["source_date_utc"]),
                table=str(item["table"]),
                logical_source_key=str(item["logical_source_key"]),
                bucket=str(item["bucket"]),
                key=str(item["key"]),
                version_id=str(item["VersionId"]),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                verification_state=str(item["verification_state"]),
                version_resolution=str(item["version_resolution"]),
            )
            for item in objects_raw
            if isinstance(item, Mapping)
        )
        days = tuple(
            V1.EligibleDay(
                source_date_utc=str(item["source_date_utc"]),
                daily_status_sha256=str(item["daily_status_sha256"]),
                durable_index_sha256=str(item["durable_index_sha256"]),
                tagged_receipt_sha256=str(item["tagged_receipt_sha256"]),
                receipt_set_sha256=str(item["receipt_set_sha256"]),
                durable_receipt_set_sha256=str(
                    item["durable_receipt_set_sha256"]
                ),
                durability_set_sha256=str(item["durability_set_sha256"]),
                eligibility_single_writer_audit_sha256=str(
                    item["eligibility_single_writer_audit_sha256"]
                ),
                manifest_version_id=str(item["manifest_version_id"]),
                manifest_sha256=str(item["manifest_sha256"]),
                exact_version_objects=tuple(
                    obj
                    for obj in objects
                    if obj.source_date_utc == str(item["source_date_utc"])
                ),
            )
            for item in days_raw
            if isinstance(item, Mapping)
        )
    except (KeyError, TypeError, ValueError) as exc:
        _fail("AUTHORITY_RECEIPT_INVALID", str(exc))
    if len(objects) != len(objects_raw) or len(days) != len(days_raw):
        _fail("AUTHORITY_RECEIPT_INVALID", "non-object rows")
    authority = V1.SourcePreparationAuthority(
        source_dates=tuple(str(item) for item in source_dates),
        eligible_days=days,
        exact_version_objects=objects,
        source_preparation_allowed=(
            raw.get("source_preparation_allowed") is True
        ),
        extraction_allowed=raw.get("extraction_allowed") is True,
        contract_adapter_status=str(raw.get("contract_adapter_status")),
    )
    return authority


def load_bound_manifest_receipt_v3(
    path: Path | str,
    *,
    expected_sha256: str,
) -> V2.BoundInputManifestV2:
    """Load a typed V2 manifest from its deterministic JSON receipt."""
    raw = _load_json_receipt(
        path,
        label="bound-input-manifest",
        expected_sha256=expected_sha256,
    )
    parents_raw = raw.get("parents")
    source_dates = raw.get("source_dates")
    if not isinstance(parents_raw, list) or not isinstance(source_dates, list):
        _fail("BOUND_MANIFEST_RECEIPT_INVALID", "lists missing")
    try:
        parents = tuple(
            V2.BoundExactParentV2(
                source_date_utc=str(item["source_date_utc"]),
                table=str(item["table"]),
                logical_source_key=str(item["logical_source_key"]),
                physical_path=str(item["physical_path"]),
                version_id=str(item["VersionId"]),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                regular_file=item.get("regular_file") is True,
                symlink_free=item.get("symlink_free") is True,
            )
            for item in parents_raw
            if isinstance(item, Mapping)
        )
        manifest = V2.BoundInputManifestV2(
            schema=str(raw["schema"]),
            status=str(raw["status"]),
            content_root=str(raw["content_root"]),
            source_dates=tuple(str(item) for item in source_dates),
            parents=parents,
            file_count=int(raw["file_count"]),
            authority_sha256=str(raw["authority_sha256"]),
            payload_sha256=str(raw["payload_sha256"]),
            content_root_bound=raw.get("content_root_bound") is True,
            source_preparation_only=raw.get("source_preparation_only")
            is True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        _fail("BOUND_MANIFEST_RECEIPT_INVALID", str(exc))
    if len(parents) != len(parents_raw):
        _fail("BOUND_MANIFEST_RECEIPT_INVALID", "non-object parent")
    return manifest


def dry_run_authoritative_source_v3(
    *,
    authority_receipt_path: Path | str,
    authority_receipt_sha256: str,
    bound_manifest_receipt_path: Path | str,
    bound_manifest_receipt_sha256: str,
    content_root: Path | str,
) -> dict[str, object]:
    """Run the real six-file transform and return a non-authorizing receipt."""
    authority = load_source_authority_receipt_v3(
        authority_receipt_path,
        expected_sha256=authority_receipt_sha256,
    )
    manifest = load_bound_manifest_receipt_v3(
        bound_manifest_receipt_path,
        expected_sha256=bound_manifest_receipt_sha256,
    )
    result = build_authoritative_source_result_v3(
        authority=authority,
        input_manifest=manifest,
        content_root=content_root,
    )
    receipt = build_authoritative_source_receipt_v3(result)
    receipt["dry_run_input_receipt_pins"] = {
        "authority_receipt_sha256": authority_receipt_sha256,
        "bound_manifest_receipt_sha256": (
            bound_manifest_receipt_sha256
        ),
    }
    receipt["payload_sha256"] = V1.canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "payload_sha256"
        }
    )
    return receipt


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run the Stage-2 V3 authoritative source transform."
    )
    parser.add_argument("--authority-receipt", required=True)
    parser.add_argument("--authority-receipt-sha256", required=True)
    parser.add_argument("--bound-manifest-receipt", required=True)
    parser.add_argument("--bound-manifest-receipt-sha256", required=True)
    parser.add_argument("--content-root", required=True)
    parser.add_argument("--receipt-out")
    arguments = parser.parse_args()
    receipt = dry_run_authoritative_source_v3(
        authority_receipt_path=arguments.authority_receipt,
        authority_receipt_sha256=arguments.authority_receipt_sha256,
        bound_manifest_receipt_path=arguments.bound_manifest_receipt,
        bound_manifest_receipt_sha256=(
            arguments.bound_manifest_receipt_sha256
        ),
        content_root=arguments.content_root,
    )
    encoded = (
        json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    if arguments.receipt_out:
        try:
            with Path(arguments.receipt_out).open(
                "x",
                encoding="utf-8",
            ) as handle:
                handle.write(encoded)
        except OSError as exc:
            _fail("RECEIPT_WRITE_FAILED", str(exc))
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
