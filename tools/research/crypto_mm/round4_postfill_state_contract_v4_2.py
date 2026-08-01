#!/usr/bin/env python3
"""ROUND4 Stage-2 post-fill data-integrity contract V4.2.

This module owns only an offline integrity boundary.  It cannot fit, select,
deploy, access an account, or trade.  The source-construction entry points
remain deliberately closed until the independently reviewed Stage-2 V2
typed causal result is pinned and consumed.

V4.1 is a frozen economic-normalization dependency; V4.2 never modifies its
Python, tests, DDL, validator, preregistration, or receipts.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path


CONTRACT_VERSION = "ROUND4_POSTFILL_PUBLIC_PROXY_V4_2"
ACTION_FAMILY_VERSION = "ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_2"
EXACT_SOURCE_FORMAT = "ROUND4_V42_EXACT_BOOK_TRADE_SOURCE_V1"
EXACT_SOURCE_VERSION = "KXBTC15M_PUBLIC_RECEIPT_EXACT_V1"
FROZEN_V41_CONTRACT_SHA256 = (
    "2642797235851cfd172ac27a2ccf1b964d65e8b3e13cbc132761fbce5db165ab"
)

SOURCE_ADAPTER_STATUS = "STAGE2_V3_AUTHORITATIVE_EOF_ADAPTER_PENDING"
SOURCE_ADAPTER_AUTHORIZED = False
FIT_AUTHORIZED = False
CANDIDATE_SELECTION_AUTHORIZED = False
DEPLOYABLE = False
LIVE_AUTHORIZED = False

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DDL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract_v4_2.sql"
)
DEFAULT_VALIDATOR_SQL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_v4_2_validator.sql"
)

POSTFILL_V42_TABLES = (
    "postfill_v42_contract_seal",
    "postfill_v42_source_manifest",
    "postfill_v42_fee_schedule_receipt",
    "postfill_v42_episode",
    "postfill_v42_episode_source_binding",
    "postfill_v42_market_metadata_receipt",
    "postfill_v42_settlement_receipt",
    "postfill_v42_projection_row",
    "postfill_v42_source_record",
    "postfill_v42_book_snapshot",
    "postfill_v42_fok_book_binding",
    "postfill_v42_trade_row",
    "postfill_v42_book_spine",
    "postfill_v42_trade_spine",
)


class PostfillV42ContractError(RuntimeError):
    """A proposed V4.2 row set is not proven by the sealed boundary."""


@dataclass(frozen=True)
class PostfillV42DDLBatch:
    """Future V2-backed normalized payload.

    No public constructor currently returns this type.  Keeping the table
    roster explicit makes the commit boundary and its later V2 adapter
    mechanically reviewable.
    """

    source_dates: tuple[str, ...]
    episode_count: int
    source_reverification: object
    postfill_v42_contract_seal: tuple[dict[str, object], ...]
    postfill_v42_source_manifest: tuple[dict[str, object], ...]
    postfill_v42_fee_schedule_receipt: tuple[dict[str, object], ...]
    postfill_v42_episode: tuple[dict[str, object], ...]
    postfill_v42_episode_source_binding: tuple[dict[str, object], ...]
    postfill_v42_market_metadata_receipt: tuple[dict[str, object], ...]
    postfill_v42_settlement_receipt: tuple[dict[str, object], ...]
    postfill_v42_projection_row: tuple[dict[str, object], ...]
    postfill_v42_source_record: tuple[dict[str, object], ...]
    postfill_v42_book_snapshot: tuple[dict[str, object], ...]
    postfill_v42_fok_book_binding: tuple[dict[str, object], ...]
    postfill_v42_trade_row: tuple[dict[str, object], ...]
    postfill_v42_book_spine: tuple[dict[str, object], ...]
    postfill_v42_trade_spine: tuple[dict[str, object], ...]

    def as_ddl_rows(self) -> dict[str, tuple[dict[str, object], ...]]:
        return {
            table: getattr(self, table)
            for table in POSTFILL_V42_TABLES
        }

    def canonical_sha256(self) -> str:
        return payload_sha256(
            {
                "contract_version": CONTRACT_VERSION,
                "source_dates": self.source_dates,
                "tables": self.as_ddl_rows(),
            }
        )


def json_ready(value: object) -> object:
    """Return the sole canonical JSON representation used by V4.2."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {
            str(key): json_ready(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (tuple, list)):
        return [json_ready(item) for item in value]
    if isinstance(value, Path):
        return os.fspath(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise PostfillV42ContractError(
        f"non-canonical JSON value type: {type(value).__name__}"
    )


def canonical_json(value: object) -> str:
    return json.dumps(
        json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _row_hash(
    row: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    canonical_booleans: bool,
) -> str:
    values = []
    for field in fields:
        value = row[field]
        if canonical_booleans and value is True:
            values.append("true")
        elif canonical_booleans and value is False:
            values.append("false")
        else:
            values.append(str(value))
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def book_snapshot_row_sha256(row: Mapping[str, object]) -> str:
    return _row_hash(
        row,
        (
            "source_record_id",
            "source_manifest_id",
            "postfill_episode_id",
            "decision_index",
            "market_ticker",
            "market_id",
            "book_side",
            "recv_wall_ns",
            "recv_mono_ns",
            "ingest_sequence",
            "stable_source_id",
            "atomic_group_id",
            "atomic_member_sequence",
            "atomic_group_terminal",
            "levels_json",
            "v41_book_source_rows_sha256",
            "source_payload_sha256",
            "content_root_sha256",
        ),
        canonical_booleans=True,
    )


def trade_row_sha256(row: Mapping[str, object]) -> str:
    return _row_hash(
        row,
        (
            "public_trade_row_id",
            "source_record_id",
            "source_manifest_id",
            "postfill_episode_id",
            "market_ticker",
            "market_id",
            "trade_id",
            "stable_source_id",
            "recv_wall_ns",
            "recv_mono_ns",
            "ingest_sequence",
            "trade_payload_json",
            "source_payload_sha256",
            "content_root_sha256",
        ),
        canonical_booleans=False,
    )


def _source_adapter_pending() -> None:
    raise PostfillV42ContractError(
        "SOURCE_ADAPTER_V3_PENDING: V4.2 requires an independently "
        "accepted authoritative Stage-2 adapter that couples six "
        "exact-version parent bytes to an EOF-complete atomic book-state "
        "spine; caller-authored complete files/manifests are forbidden"
    )


def build_exact_source_bytes(*args: object, **kwargs: object) -> bytes:
    """Reject the removed circular V4.1-to-source generator."""
    del args, kwargs
    _source_adapter_pending()


def exact_source_manifest(
    *args: object,
    **kwargs: object,
) -> dict[str, object]:
    """Reject caller-authored source replacement manifests."""
    del args, kwargs
    _source_adapter_pending()


def validate_and_serialize_postfill_rows(
    *args: object,
    **kwargs: object,
) -> PostfillV42DDLBatch:
    """Remain closed until the authoritative Stage-2 adapter is pinned."""
    del args, kwargs
    _source_adapter_pending()


def reverify_source_bound_rows(
    *args: object,
    **kwargs: object,
) -> None:
    """Prevent any commit while the authoritative adapter is pending."""
    del args, kwargs
    _source_adapter_pending()


def create_postfill_v42_schema(
    connection: object,
    ddl_path: os.PathLike[str] | str = DEFAULT_DDL_PATH,
) -> Path:
    path = Path(ddl_path)
    connection.execute(path.read_text())
    return path
