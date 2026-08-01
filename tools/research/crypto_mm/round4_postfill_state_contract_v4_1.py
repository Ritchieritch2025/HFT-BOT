#!/usr/bin/env python3
"""ROUND4 Stage-2 post-fill contract V4.1.

V4.1 is intentionally a separate boundary from the rejected V4 artifacts.
It is a historical public-proxy simulation contract only: it cannot fit,
select, deploy, or authorize a live strategy.

The contract is deliberately conservative in three places:

* historical cancel state is simulated and is always ``NONE`` at a
  decision; FOK book evidence must be strictly pre-effective;
* maker trade fee zero is not treated as total fee zero; balance-rounding
  risk is carried in the first-leg basis and complement reservation;
* capital remains locked until a simulated cancel-effective/order-expiry
  event and the public FINALIZED/settlement receipt, never merely until the
  scheduled market close.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import re

from tools.research.crypto_mm.round4_postfill_state_contract import (
    preflight_canary_postfill_paths,
)
from tools.research.crypto_mm.round4_table_builder import (
    EXPERIMENT_ID,
    ForbiddenSourceError,
    Round4ContractError,
)


CONTRACT_VERSION = "ROUND4_POSTFILL_PUBLIC_PROXY_V4_1"
ACTION_FAMILY_VERSION = "ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_1"
ALLOWED_ACTIONS = frozenset(("KEEP", "FLATTEN_FOK"))
FOK_EFFECTIVE_LATENCY_MS = 60
FOK_TIME_IN_FORCE = "fill_or_kill"
FOK_TERMINALS = frozenset(("CANCEL_RACE_PAIR", "FOK_FULL", "FOK_ZERO"))
CONTINUOUS_TERMINALS = frozenset(("COMPLEMENT_FILL", "HARD_FALLBACK"))
ZERO_TIME_TERMINAL = "ZERO_TIME_ATOM"

DATA_ORIGIN = "PUBLIC_RAW"
FILL_PROVENANCE = "PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY"
EXECUTION_NATURE = "SYNTHETIC"
MAKER_TRADE_FEE_PROVENANCE = "SCHEDULE_ZERO_MAKER_TRADE_FEE"
MAKER_NET_FEE_BOUND_PROVENANCE = "NO_REBATE_ACCOUNT_ROUNDING_SAFE_UPPER"
FOK_EXECUTION_PROVENANCE = "STRICT_PRE_EFFECTIVE_PUBLIC_L2_COUNTERFACTUAL"
CANCEL_TIMING_PROVENANCE = "SYNTHETIC_60MS_SCENARIO"
SETTLEMENT_PROVENANCE = "PUBLIC_SETTLEMENT_RECEIPT_SIMULATION"
ORDER_RELEASE_PROVENANCE = (
    "SYNTHETIC_PUBLIC_LIFECYCLE_ORDER_EXPIRY_SCENARIO"
)
PUBLIC_MARKET_METADATA_PROVENANCE = "PUBLIC_MARKET_METADATA"
PUBLIC_SETTLEMENT_RECEIPT_PROVENANCE = "PUBLIC_FINALIZED_MARKET_RECEIPT"
FEE_SCHEDULE_PROVENANCE = "OFFICIAL_KALSHI_FEE_ROUNDING"
FEE_SCHEDULE_SOURCE = "https://docs.kalshi.com/getting_started/fee_rounding"
FEE_SCHEDULE_RECEIPT_ID = "KXBTC15M_FEE_SCHEDULE_2026_07_07"
FEE_SCHEDULE_SERIES_TICKER = "KXBTC15M"
FEE_SCHEDULE_EFFECTIVE_DATE_UTC = "2026-07-07"
FEE_SCHEDULE_PDF_URL = (
    "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
)
FEE_SCHEDULE_PDF_LOCAL_PATH = (
    "/Applications/Research Ritch/kalshi-fee-schedule July'.pdf"
)
FEE_SCHEDULE_PDF_LOCAL_SIZE_BYTES = 382_507
FEE_SCHEDULE_PDF_LOCAL_MODIFIED_AT = "2026-07-24T22:57:50-04:00"
FEE_SCHEDULE_PDF_WHERE_FROMS = (
    '["https://kalshi.com/docs/kalshi-fee-schedule.pdf",'
    '"https://kalshi.com/docs/kalshi-fee-schedule.pdf"]'
)
FEE_SCHEDULE_PDF_WHERE_FROMS_XATTR_SHA256 = (
    "1f0c5b6d7b9718fbe402ee43501b9aa6"
    "49b97a69abba972f1639fb3c71fceaee"
)
FEE_SCHEDULE_PDF_PAGE_COUNT = 12
FEE_SCHEDULE_PDF_RAW_SHA256 = (
    "815e2d5127d02d2fb90773d1a3844dc1"
    "5a987696171eddc4e58de87b59c6124c"
)
FEE_SCHEDULE_PDF_RAW_BYTES_AUTHENTICATED = True
FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_SHA256 = (
    "5f90733a0dae6e3d9577efb37895011d"
    "dda96385731481a7706c48643b58276d"
)
FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_PROVENANCE = (
    "LOCAL_AUTHENTICATED_PDF_PYMUPDF_PAGE_TEXT_JOIN_LF"
)
FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_CHAR_COUNT = 9_612
FEE_SERIES_GET_CAPTURED_AT_UTC = "2026-07-26T11:43:46Z"
FEE_SERIES_GET_RAW_SHA256 = (
    "be24516ae4825de53af89b12ead76e3a"
    "93ac52e18ef67bf519621adde3ed5bc2"
)
FEE_SERIES_CHANGES_CAPTURED_AT_UTC = "2026-07-26T11:43:51Z"
FEE_SERIES_CHANGES_RAW_SHA256 = (
    "6780c8eb7edbb5e1ca3b15166f17cef"
    "054befacb2cc8f4bc479c54074a9a2c18"
)
FEE_SERIES_TYPE = "quadratic"
FEE_SERIES_MULTIPLIER = Decimal("1")
FEE_SERIES_LAST_UPDATED_TS = "2026-07-01T18:05:04.527379Z"
FEE_MAKER_RATE = Decimal("0.0175")
FEE_MAKER_MULTIPLIER = Decimal("0")
FEE_TAKER_MULTIPLIER = Decimal("1")
FIXED_POINT_SOURCE = (
    "https://docs.kalshi.com/getting_started/fixed_point_migration"
)
SETTLEMENT_SOURCE = (
    "https://docs.kalshi.com/getting_started/market_settlement"
)
PRICE_LEVEL_STRUCTURE = "tapered_deci_cent"
MIN_FILL_INCREMENT_FP = Decimal("0.01")
TRADE_FEE_QUANTUM_USD = Decimal("0.0001")
CONSERVATIVE_ACCOUNT_ROUNDING_TARGET_USD = Decimal("0.01")
TARGET_ACCOUNT_CLASS = "DIRECT"
TARGET_ACCOUNT_ROUNDING_TARGET_USD = Decimal("0.0001")
TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256 = (
    "816528a20ee70ffc7536c02b8984a69f"
    "8730f8304656a259e2edd124e6db8da6"
)
TARGET_ACCOUNT_PRECISION_RECEIPT_PATH = (
    "tmp/crypto_mm_canary_20260726/"
    "position_value_contract_probe_result.json"
)
FEE_RATE = Decimal("0.07")
E4 = Decimal("10000")
LEGAL_PRICE_MIN_E4 = 10
LEGAL_PRICE_MAX_E4 = 9_990
FEE_BOUND_SELECTED_FOR_CANDIDATE = (
    "PARTITION_DP_NO_REBATE_SAFE_UPPER"
)
DOC_LITERAL_TIGHT_SENSITIVITY = (
    "DOC_LITERAL_RAW_PLUS_NMAX_CENTICENT_PLUS_ONE_CENT_PER_ORDER"
)
FORWARD_ACTUAL_FEE_PROVENANCE = "UNAVAILABLE_HISTORICAL_PUBLIC_L2"
LIVE_AUTHORIZED = False
FIT_AUTHORIZED = False
CANDIDATE_SELECTION_AUTHORIZED = False

DECISION_GRID_MS = (
    0,
    250,
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    30_000,
    60_000,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DDL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract_v4_1.sql"
)
DEFAULT_VALIDATOR_SQL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_v4_1_validator.sql"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MARKET_TICKER_DATE_RE = re.compile(
    r"^KXBTC15M-([0-9]{2})(JAN|FEB|MAR|APR|MAY|JUN|"
    r"JUL|AUG|SEP|OCT|NOV|DEC)([0-9]{2})"
)


class PostfillV41ContractError(Round4ContractError):
    """A V4.1 source row violates the sealed contract."""


class MarketDayRollbackRequired(PostfillV41ContractError):
    """No row from the affected source day may be committed."""

    def __init__(
        self,
        source_dates: Sequence[str],
        reason_code: str,
        detail: str,
    ) -> None:
        self.source_dates = tuple(sorted(set(source_dates)))
        self.reason_code = reason_code
        self.detail = detail
        day_text = ",".join(self.source_dates) or "UNKNOWN_DAY"
        super().__init__(
            f"MARKET_DAY_ROLLBACK[{day_text}][{reason_code}]: {detail}"
        )


@dataclass(frozen=True)
class PostfillV41DDLBatch:
    """Immutable table payload after whole-day validation."""

    source_dates: tuple[str, ...]
    guarded_source_paths: tuple[str, ...]
    episode_count: int
    postfill_v41_causal_state: tuple[dict[str, object], ...]
    postfill_v41_action: tuple[dict[str, object], ...]
    postfill_v41_keep_transition: tuple[dict[str, object], ...]
    postfill_v41_flatten_fok_outcome: tuple[dict[str, object], ...]
    postfill_v41_fok_slice: tuple[dict[str, object], ...]
    postfill_v41_public_proxy_evidence: tuple[dict[str, object], ...]
    postfill_v41_public_trade_row: tuple[dict[str, object], ...]
    postfill_v41_market_metadata_receipt: tuple[dict[str, object], ...]
    postfill_v41_settlement_receipt: tuple[dict[str, object], ...]
    postfill_v41_fee_schedule_receipt: tuple[dict[str, object], ...]
    postfill_v41_zero_time_atom: tuple[dict[str, object], ...]

    def as_ddl_rows(self) -> dict[str, tuple[dict[str, object], ...]]:
        return {
            "postfill_v41_causal_state": self.postfill_v41_causal_state,
            "postfill_v41_action": self.postfill_v41_action,
            "postfill_v41_keep_transition": (
                self.postfill_v41_keep_transition
            ),
            "postfill_v41_flatten_fok_outcome": (
                self.postfill_v41_flatten_fok_outcome
            ),
            "postfill_v41_fok_slice": self.postfill_v41_fok_slice,
            "postfill_v41_public_proxy_evidence": (
                self.postfill_v41_public_proxy_evidence
            ),
            "postfill_v41_public_trade_row": (
                self.postfill_v41_public_trade_row
            ),
            "postfill_v41_market_metadata_receipt": (
                self.postfill_v41_market_metadata_receipt
            ),
            "postfill_v41_settlement_receipt": (
                self.postfill_v41_settlement_receipt
            ),
            "postfill_v41_fee_schedule_receipt": (
                self.postfill_v41_fee_schedule_receipt
            ),
            "postfill_v41_zero_time_atom": (
                self.postfill_v41_zero_time_atom
            ),
        }

    def canonical_sha256(self) -> str:
        payload = {
            "contract_version": CONTRACT_VERSION,
            "action_family_version": ACTION_FAMILY_VERSION,
            "source_dates": self.source_dates,
            "tables": self.as_ddl_rows(),
        }
        return _canonical_sha256(payload)

    def receipt(self) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "action_family_version": ACTION_FAMILY_VERSION,
            "source_dates": list(self.source_dates),
            "episode_count": self.episode_count,
            "decision_grid_ms": list(DECISION_GRID_MS),
            "strict_pre_effective_public_book": True,
            "historical_simulated_cancel_state": "NONE",
            "fee_bound_selected_for_candidate": (
                FEE_BOUND_SELECTED_FOR_CANDIDATE
            ),
            "fee_formula": (
                "F_candidate=partition-DP no-rebate upper; "
                "F_sql_safe=Tmax+n_max*h; "
                "Tmax=sum_i((q_i/delta)*"
                "ceil_tau(0.07*delta*p_i*(1-p_i))); "
                "delta=0.01 contracts; tau=0.0001 USD; "
                "target account DIRECT h=0.0001 USD, pinned by receipt sha"
            ),
            "target_account_class": TARGET_ACCOUNT_CLASS,
            "target_account_rounding_target_usd": str(
                TARGET_ACCOUNT_ROUNDING_TARGET_USD
            ),
            "target_account_precision_receipt_sha256": (
                TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
            ),
            "doc_literal_tight_sensitivity_formula": (
                "raw_total+n_max*0.0001+0.01/order; sensitivity only"
            ),
            "capital_formulas": {
                "PAIR_OR_RACE_t": "(B+R)*t",
                "FOK_FULL_x_f": "(B+R)*x+(B+C)*(f-x)",
                "FOK_ZERO_x_f_r": (
                    "(B+R)*x+(B+C)*(f-x)+B*(r-f)"
                ),
                "HARD_x_to_r": "(B+R)*x+B*(r-x)",
            },
            "official_sources": [
                FEE_SCHEDULE_SOURCE,
                FIXED_POINT_SOURCE,
                SETTLEMENT_SOURCE,
            ],
            "payload_sha256": self.canonical_sha256(),
            "fit_authorized": FIT_AUTHORIZED,
            "candidate_selection_authorized": (
                CANDIDATE_SELECTION_AUTHORIZED
            ),
            "live_authorized": LIVE_AUTHORIZED,
        }


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PostfillV41ContractError(
            f"{label}: floats and booleans are forbidden"
        )
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise PostfillV41ContractError(
            f"{label}: expected exact decimal"
        ) from exc
    if not parsed.is_finite():
        raise PostfillV41ContractError(f"{label}: non-finite decimal")
    return parsed


def _integer(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise PostfillV41ContractError(f"{label}: expected integer")
    if minimum is not None and value < minimum:
        raise PostfillV41ContractError(f"{label}: below minimum")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise PostfillV41ContractError(f"{label}: expected boolean")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PostfillV41ContractError(f"{label}: expected non-empty text")
    return value


def _sha(value: object, label: str) -> str:
    text = _text(value, label)
    if not SHA256_RE.fullmatch(text):
        raise PostfillV41ContractError(
            f"{label}: expected lowercase sha256"
        )
    return text


def _require(row: Mapping[str, object], fields: Sequence[str], label: str) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise PostfillV41ContractError(f"{label}: missing {missing}")


def _only(
    row: Mapping[str, object],
    fields: Sequence[str],
    label: str,
) -> None:
    unknown = sorted(set(row) - set(fields))
    if unknown:
        raise PostfillV41ContractError(f"{label}: unsealed fields {unknown}")


def _day(value: object) -> str:
    text = _text(value, "source_date_utc")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise PostfillV41ContractError(
            f"invalid source_date_utc {text!r}"
        ) from exc


def _utc_day_from_ns(value: int) -> str:
    return datetime.fromtimestamp(
        value / 1_000_000_000,
        tz=timezone.utc,
    ).date().isoformat()


def _elapsed_ns(elapsed_ms: Decimal | int) -> int:
    exact = _decimal(elapsed_ms, "elapsed_ms") * Decimal("1000000")
    if exact != exact.to_integral_value():
        raise PostfillV41ContractError(
            "elapsed milliseconds do not map to integer nanoseconds"
        )
    return int(exact)


def _multiple_of(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        return False
    quotient = value / increment
    return quotient == quotient.to_integral_value()


def is_legal_tapered_price(price_e4: int) -> bool:
    if not LEGAL_PRICE_MIN_E4 <= price_e4 <= LEGAL_PRICE_MAX_E4:
        return False
    if price_e4 < 1_000 or price_e4 > 9_000:
        return price_e4 % 10 == 0
    return price_e4 % 100 == 0


def _legal_price(value: object, label: str) -> int:
    price = _integer(value, label)
    if not is_legal_tapered_price(price):
        raise PostfillV41ContractError(
            f"{label}: off tapered_deci_cent grid"
        )
    return price


def _canonical_flatten_book_side(first_side: str) -> str:
    if first_side == "YES":
        return "ASK"
    if first_side == "NO":
        return "BID"
    raise PostfillV41ContractError("first_side must be YES or NO")


def _adverse_limit(
    book_side: str,
    worst_price_e4: int | None,
) -> tuple[int, int, bool]:
    if worst_price_e4 is None:
        return (
            LEGAL_PRICE_MIN_E4 if book_side == "ASK" else LEGAL_PRICE_MAX_E4,
            0,
            True,
        )
    direction = -1 if book_side == "ASK" else 1
    candidate = worst_price_e4 + direction
    while (
        LEGAL_PRICE_MIN_E4 <= candidate <= LEGAL_PRICE_MAX_E4
        and not is_legal_tapered_price(candidate)
    ):
        candidate += direction
    if not LEGAL_PRICE_MIN_E4 <= candidate <= LEGAL_PRICE_MAX_E4:
        candidate = worst_price_e4
    return candidate, abs(candidate - worst_price_e4), False


def _normalize_levels(
    raw: object,
    *,
    label: str,
    book_side: str,
    delta_qty: Decimal,
    limit_price_e4: int | None = None,
) -> tuple[tuple[int, Decimal], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PostfillV41ContractError(f"{label}: expected sequence")
    normalized: list[tuple[int, Decimal]] = []
    previous: int | None = None
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PostfillV41ContractError(
                f"{label}[{index}]: expected mapping"
            )
        _only(item, ("price_e4", "qty_fp"), f"{label}[{index}]")
        _require(item, ("price_e4", "qty_fp"), f"{label}[{index}]")
        price = _legal_price(item["price_e4"], f"{label}[{index}].price_e4")
        qty = _decimal(item["qty_fp"], f"{label}[{index}].qty_fp")
        if qty <= 0 or not _multiple_of(qty, delta_qty):
            raise PostfillV41ContractError(
                f"{label}[{index}]: qty must be positive legal increment"
            )
        if previous is not None:
            if book_side == "ASK" and price >= previous:
                raise PostfillV41ContractError(
                    f"{label}: ASK levels must be strictly descending"
                )
            if book_side == "BID" and price <= previous:
                raise PostfillV41ContractError(
                    f"{label}: BID levels must be strictly ascending"
                )
        if limit_price_e4 is not None:
            if (
                book_side == "ASK"
                and price < limit_price_e4
            ) or (
                book_side == "BID"
                and price > limit_price_e4
            ):
                raise PostfillV41ContractError(
                    f"{label}: level violates frozen FOK limit"
                )
        normalized.append((price, qty))
        previous = price
    return tuple(normalized)


def _ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    if value == 0:
        return Decimal("0")
    units = (value / quantum).to_integral_value(rounding=ROUND_CEILING)
    return units * quantum


def _fee_bound_for_levels(
    levels: Sequence[tuple[int, Decimal]],
    *,
    delta_qty: Decimal,
    account_rounding_target: Decimal,
    book_side: str = "BID",
) -> dict[str, Decimal | int]:
    raw_total = Decimal("0")
    aggregate_level_scenario = Decimal("0")
    trade_upper = Decimal("0")
    partition_dp_upper = Decimal("0")
    n_max = 0
    for price_e4, qty in levels:
        if not _multiple_of(qty, delta_qty):
            raise PostfillV41ContractError(
                "fee partition qty is not an integer min-fill increment"
            )
        price = Decimal(price_e4) / E4
        raw_level = FEE_RATE * qty * price * (Decimal("1") - price)
        raw_total += raw_level
        aggregate_level_scenario += _ceil_quantum(
            raw_level,
            TRADE_FEE_QUANTUM_USD,
        )
        level_n = int(qty / delta_qty)
        n_max += level_n
        min_fill_raw = (
            FEE_RATE
            * delta_qty
            * price
            * (Decimal("1") - price)
        )
        trade_upper += Decimal(level_n) * _ceil_quantum(
            min_fill_raw,
            TRADE_FEE_QUANTUM_USD,
        )
        # Exact no-rebate upper over every legal private fill partition of
        # this public L2 level.  A fill of k*delta contributes rounded trade
        # fee plus the account-precision remainder of its signed balance.
        dp = [Decimal("0")] + [Decimal("-Infinity")] * level_n
        for consumed in range(1, level_n + 1):
            best = Decimal("-Infinity")
            for fill_units in range(1, consumed + 1):
                fill_qty = Decimal(fill_units) * delta_qty
                trade_fee = _ceil_quantum(
                    FEE_RATE
                    * fill_qty
                    * price
                    * (Decimal("1") - price),
                    TRADE_FEE_QUANTUM_USD,
                )
                signed_revenue = (
                    fill_qty * price
                    if book_side == "ASK"
                    else -fill_qty * price
                )
                balance = signed_revenue - trade_fee
                floor_units = (
                    balance / account_rounding_target
                ).to_integral_value(rounding=ROUND_FLOOR)
                rounding_residual = (
                    balance
                    - account_rounding_target * floor_units
                )
                contribution = trade_fee + rounding_residual
                candidate = dp[consumed - fill_units] + contribution
                if candidate > best:
                    best = candidate
            dp[consumed] = best
        partition_dp_upper += dp[level_n]
    aggregate_lower = _ceil_quantum(
        raw_total,
        TRADE_FEE_QUANTUM_USD,
    )
    rounding_safe = Decimal(n_max) * account_rounding_target
    sql_safe_upper = trade_upper + rounding_safe
    doc_literal = (
        raw_total
        + Decimal(n_max) * TRADE_FEE_QUANTUM_USD
        + (Decimal("0.01") if n_max else Decimal("0"))
    )
    return {
        "raw_total": raw_total,
        "aggregate_centicent_lower": aggregate_lower,
        "public_l2_level_scenario": aggregate_level_scenario,
        "n_max": n_max,
        "trade_upper_before_account_rounding": trade_upper,
        "account_rounding_safe_upper": rounding_safe,
        "partition_dp_no_rebate_upper": partition_dp_upper,
        "sql_safe_upper": sql_safe_upper,
        "safe_upper": partition_dp_upper,
        "doc_literal_tight_sensitivity": doc_literal,
    }


def _maker_net_fee_safe_upper(
    qty: Decimal,
    *,
    price_e4: int,
    delta_qty: Decimal,
    account_rounding_target: Decimal,
) -> tuple[int, Decimal]:
    if not _multiple_of(qty, delta_qty):
        raise PostfillV41ContractError(
            "maker quantity is not a legal min-fill increment"
        )
    n_max = int(qty / delta_qty)
    min_fill_principal = (
        Decimal(price_e4) / E4 * delta_qty
    )
    cent_aligned_partition = _multiple_of(
        min_fill_principal,
        account_rounding_target,
    )
    if (
        cent_aligned_partition
        and _multiple_of(
            TRADE_FEE_QUANTUM_USD,
            account_rounding_target,
        )
    ):
        return n_max, Decimal("0")
    return n_max, Decimal(n_max) * account_rounding_target


FEE_SCHEDULE_RECEIPT_HASH_FIELDS = (
    "fee_schedule_receipt_id",
    "series_ticker",
    "schedule_effective_date_utc",
    "schedule_pdf_url",
    "schedule_pdf_local_path",
    "schedule_pdf_local_size_bytes",
    "schedule_pdf_local_modified_at",
    "schedule_pdf_where_froms",
    "schedule_pdf_where_froms_xattr_sha256",
    "schedule_pdf_page_count",
    "schedule_pdf_raw_sha256",
    "schedule_pdf_raw_bytes_authenticated",
    "schedule_pdf_normalized_content_sha256",
    "schedule_pdf_normalized_content_provenance",
    "schedule_pdf_normalized_content_char_count",
    "series_get_captured_at_utc",
    "series_get_raw_sha256",
    "series_fee_changes_captured_at_utc",
    "series_fee_changes_raw_sha256",
    "series_fee_changes_show_historical",
    "series_fee_change_count",
    "series_fee_type",
    "series_fee_multiplier",
    "series_last_updated_ts",
    "general_taker_rate",
    "general_maker_rate",
    "general_maker_default_multiplier",
    "series_maker_multiplier",
    "series_taker_multiplier",
    "settlement_fee_usd",
)


def fee_schedule_receipt_sha256(
    receipt: Mapping[str, object],
) -> str:
    return _canonical_sha256(
        {
            field: receipt[field]
            for field in FEE_SCHEDULE_RECEIPT_HASH_FIELDS
        }
    )


def _frozen_fee_schedule_receipt() -> dict[str, object]:
    row: dict[str, object] = {
        "fee_schedule_receipt_id": FEE_SCHEDULE_RECEIPT_ID,
        "contract_version": CONTRACT_VERSION,
        "series_ticker": FEE_SCHEDULE_SERIES_TICKER,
        "schedule_effective_date_utc": (
            FEE_SCHEDULE_EFFECTIVE_DATE_UTC
        ),
        "schedule_pdf_url": FEE_SCHEDULE_PDF_URL,
        "schedule_pdf_local_path": FEE_SCHEDULE_PDF_LOCAL_PATH,
        "schedule_pdf_local_size_bytes": (
            FEE_SCHEDULE_PDF_LOCAL_SIZE_BYTES
        ),
        "schedule_pdf_local_modified_at": (
            FEE_SCHEDULE_PDF_LOCAL_MODIFIED_AT
        ),
        "schedule_pdf_where_froms": FEE_SCHEDULE_PDF_WHERE_FROMS,
        "schedule_pdf_where_froms_xattr_sha256": (
            FEE_SCHEDULE_PDF_WHERE_FROMS_XATTR_SHA256
        ),
        "schedule_pdf_page_count": FEE_SCHEDULE_PDF_PAGE_COUNT,
        "schedule_pdf_raw_sha256": FEE_SCHEDULE_PDF_RAW_SHA256,
        "schedule_pdf_raw_bytes_authenticated": (
            FEE_SCHEDULE_PDF_RAW_BYTES_AUTHENTICATED
        ),
        "schedule_pdf_normalized_content_sha256": (
            FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_SHA256
        ),
        "schedule_pdf_normalized_content_provenance": (
            FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_PROVENANCE
        ),
        "schedule_pdf_normalized_content_char_count": (
            FEE_SCHEDULE_PDF_NORMALIZED_CONTENT_CHAR_COUNT
        ),
        "series_get_captured_at_utc": FEE_SERIES_GET_CAPTURED_AT_UTC,
        "series_get_raw_sha256": FEE_SERIES_GET_RAW_SHA256,
        "series_fee_changes_captured_at_utc": (
            FEE_SERIES_CHANGES_CAPTURED_AT_UTC
        ),
        "series_fee_changes_raw_sha256": (
            FEE_SERIES_CHANGES_RAW_SHA256
        ),
        "series_fee_changes_show_historical": True,
        "series_fee_change_count": 0,
        "series_fee_type": FEE_SERIES_TYPE,
        "series_fee_multiplier": FEE_SERIES_MULTIPLIER,
        "series_last_updated_ts": FEE_SERIES_LAST_UPDATED_TS,
        "general_taker_rate": FEE_RATE,
        "general_maker_rate": FEE_MAKER_RATE,
        "general_maker_default_multiplier": FEE_MAKER_MULTIPLIER,
        "series_maker_multiplier": FEE_MAKER_MULTIPLIER,
        "series_taker_multiplier": FEE_TAKER_MULTIPLIER,
        "settlement_fee_usd": Decimal("0"),
    }
    row["source_rows_sha256"] = fee_schedule_receipt_sha256(row)
    return row


MARKET_METADATA_HASH_FIELDS = (
    "market_ticker",
    "market_id",
    "source_date_utc",
    "market_window_open_wall_ns",
    "market_window_close_wall_ns",
    "price_level_structure",
    "market_min_fill_increment_fp",
    "market_fill_increment_guarantees_true_fill_min",
    "market_metadata_stable_source_id",
)


def market_metadata_sha256(metadata: Mapping[str, object]) -> str:
    return _canonical_sha256(
        {field: metadata[field] for field in MARKET_METADATA_HASH_FIELDS}
    )


SETTLEMENT_HASH_FIELDS = (
    "market_ticker",
    "market_id",
    "official_market_result",
    "market_status",
    "settlement_recv_wall_ns",
    "settlement_recv_mono_ns",
    "settlement_ingest_sequence",
    "settlement_stable_source_id",
    "determined_wall_ns",
    "finalized_wall_ns",
)


def settlement_receipt_sha256(receipt: Mapping[str, object]) -> str:
    return _canonical_sha256(
        {field: receipt[field] for field in SETTLEMENT_HASH_FIELDS}
    )


PROXY_HASH_FIELDS = (
    "evidence_id",
    "proxy_kind",
    "market_ticker",
    "market_id",
    "maker_order_yes_book_side",
    "maker_order_outcome_side",
    "maker_order_outcome_price_e4",
    "maker_order_yes_price_e4",
    "maker_order_qty_fp",
    "trigger_public_trade_row_id",
    "derived_cumulative_strict_through_qty_fp",
    "public_trade_spine_sha256",
    "recv_wall_ns",
    "recv_mono_ns",
    "ingest_sequence",
    "stable_source_id",
)


def public_proxy_evidence_sha256(evidence: Mapping[str, object]) -> str:
    return _canonical_sha256(
        {field: evidence[field] for field in PROXY_HASH_FIELDS}
    )


PUBLIC_TRADE_HASH_FIELDS = (
    "trade_id",
    "ticker",
    "taker_book_side",
    "taker_outcome_side",
    "yes_price_e4",
    "count_fp",
    "is_block_trade",
    "recv_wall_ns",
    "recv_mono_ns",
    "ingest_sequence",
    "stable_source_id",
)


def public_trade_row_sha256(trade: Mapping[str, object]) -> str:
    return _canonical_sha256(
        {field: trade[field] for field in PUBLIC_TRADE_HASH_FIELDS}
    )


def public_trade_spine_sha256(
    trades: Sequence[Mapping[str, object]],
) -> str:
    return _canonical_sha256(
        [
            {
                field: trade[field]
                for field in (*PUBLIC_TRADE_HASH_FIELDS, "source_rows_sha256")
            }
            for trade in trades
        ]
    )


BOOK_HASH_FIELDS = (
    "market_ticker",
    "market_id",
    "recv_wall_ns",
    "recv_mono_ns",
    "ingest_sequence",
    "stable_source_id",
)


def public_book_source_sha256(book: Mapping[str, object]) -> str:
    levels = book["levels"]
    return _canonical_sha256(
        {
            **{field: book[field] for field in BOOK_HASH_FIELDS},
            "levels": levels,
        }
    )


def _normalize_market_metadata(
    raw: object,
    *,
    episode_ticker: str,
    episode_market_id: str,
    source_day: str,
    first_wall_ns: int,
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise PostfillV41ContractError("market_metadata: expected mapping")
    fields = (
        *MARKET_METADATA_HASH_FIELDS,
        "provenance",
        "source_rows_sha256",
    )
    _only(raw, fields, "market_metadata")
    _require(raw, fields, "market_metadata")
    row = dict(raw)
    if (
        row["market_ticker"] != episode_ticker
        or row["market_id"] != episode_market_id
        or _day(row["source_date_utc"]) != source_day
    ):
        raise PostfillV41ContractError(
            "market ticker/id/source-date metadata binding mismatch"
        )
    ticker_match = MARKET_TICKER_DATE_RE.match(episode_ticker)
    if ticker_match is None:
        raise PostfillV41ContractError(
            "market ticker lacks sealed KXBTC15M expiry-date identity"
        )
    ticker_day = datetime.strptime(
        "".join(ticker_match.groups()).title(),
        "%y%b%d",
    ).date().isoformat()
    if ticker_day != source_day:
        raise PostfillV41ContractError(
            "market ticker expiry date does not match source/window date"
        )
    if row["provenance"] != PUBLIC_MARKET_METADATA_PROVENANCE:
        raise PostfillV41ContractError("market metadata provenance mismatch")
    open_wall = _integer(
        row["market_window_open_wall_ns"],
        "market_window_open_wall_ns",
        minimum=1,
    )
    close_wall = _integer(
        row["market_window_close_wall_ns"],
        "market_window_close_wall_ns",
        minimum=1,
    )
    if (
        not open_wall <= first_wall_ns < close_wall
        or _utc_day_from_ns(open_wall) != source_day
        or _utc_day_from_ns(first_wall_ns) != source_day
    ):
        raise PostfillV41ContractError(
            "first fill is outside ticker/date market metadata window"
        )
    if row["price_level_structure"] != PRICE_LEVEL_STRUCTURE:
        raise PostfillV41ContractError(
            "price grid must be pinned from market price_level_structure"
        )
    delta = _decimal(
        row["market_min_fill_increment_fp"],
        "market_min_fill_increment_fp",
    )
    if delta != MIN_FILL_INCREMENT_FP:
        raise PostfillV41ContractError(
            "unknown/non-pinned minimum true-fill increment"
        )
    if not _boolean(
        row["market_fill_increment_guarantees_true_fill_min"],
        "market_fill_increment_guarantees_true_fill_min",
    ):
        raise PostfillV41ContractError(
            "metadata does not prove every true fill qty >= delta_qty"
        )
    _text(
        row["market_metadata_stable_source_id"],
        "market_metadata_stable_source_id",
    )
    supplied = _sha(row["source_rows_sha256"], "market metadata source hash")
    if supplied != market_metadata_sha256(row):
        raise PostfillV41ContractError("market metadata source hash mismatch")
    row.update(
        {
            "market_metadata_receipt_id": row[
                "market_metadata_stable_source_id"
            ],
            "contract_version": CONTRACT_VERSION,
            "market_window_open_wall_ns": open_wall,
            "market_window_close_wall_ns": close_wall,
            "market_min_fill_increment_fp": delta,
            "source_rows_sha256": supplied,
        }
    )
    return row


def _normalize_settlement(
    raw: object,
    *,
    market_ticker: str,
    market_id: str,
    source_day: str,
    first_wall_ns: int,
    first_mono_ns: int,
    market_close_elapsed_ms: Decimal,
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise PostfillV41ContractError(
            "public_settlement_receipt: expected mapping"
        )
    fields = (
        *SETTLEMENT_HASH_FIELDS,
        "provenance",
        "source_rows_sha256",
    )
    _only(raw, fields, "public_settlement_receipt")
    _require(raw, fields, "public_settlement_receipt")
    row = dict(raw)
    if (
        row["market_ticker"] != market_ticker
        or row["market_id"] != market_id
    ):
        raise PostfillV41ContractError(
            "official result belongs to another market"
        )
    if (
        row["provenance"] != PUBLIC_SETTLEMENT_RECEIPT_PROVENANCE
        or row["market_status"] != "FINALIZED"
        or row["official_market_result"] not in ("YES", "NO")
    ):
        raise PostfillV41ContractError(
            "settlement receipt is not a public FINALIZED market result"
        )
    recv_wall = _integer(
        row["settlement_recv_wall_ns"],
        "settlement_recv_wall_ns",
        minimum=1,
    )
    recv_mono = _integer(
        row["settlement_recv_mono_ns"],
        "settlement_recv_mono_ns",
        minimum=1,
    )
    _integer(
        row["settlement_ingest_sequence"],
        "settlement_ingest_sequence",
        minimum=0,
    )
    _text(row["settlement_stable_source_id"], "settlement_stable_source_id")
    determined = _integer(
        row["determined_wall_ns"],
        "determined_wall_ns",
        minimum=1,
    )
    finalized = _integer(
        row["finalized_wall_ns"],
        "finalized_wall_ns",
        minimum=1,
    )
    elapsed = Decimal(recv_wall - first_wall_ns) / Decimal("1000000")
    if (
        recv_mono - first_mono_ns != recv_wall - first_wall_ns
        or elapsed < market_close_elapsed_ms
        or not determined <= finalized <= recv_wall
    ):
        raise PostfillV41ContractError(
            "settlement receipt must be received at/after close with "
            "conserving public clocks"
        )
    supplied = _sha(row["source_rows_sha256"], "settlement source hash")
    if supplied != settlement_receipt_sha256(row):
        raise PostfillV41ContractError("settlement source hash mismatch")
    row.update(
        {
            "settlement_receipt_id": row[
                "settlement_stable_source_id"
            ],
            "contract_version": CONTRACT_VERSION,
            "source_date_utc": source_day,
            "settlement_recv_wall_ns": recv_wall,
            "settlement_recv_mono_ns": recv_mono,
            "settlement_release_elapsed_ms": elapsed,
            "source_rows_sha256": supplied,
        }
    )
    return row


def _market_metadata_receipt_row(
    metadata: Mapping[str, object],
) -> dict[str, object]:
    fields = (
        "market_metadata_receipt_id",
        "contract_version",
        *MARKET_METADATA_HASH_FIELDS,
        "provenance",
        "source_rows_sha256",
    )
    return {field: metadata[field] for field in fields}


def _settlement_receipt_row(
    settlement: Mapping[str, object],
) -> dict[str, object]:
    fields = (
        "settlement_receipt_id",
        "contract_version",
        "source_date_utc",
        *SETTLEMENT_HASH_FIELDS,
        "provenance",
        "source_rows_sha256",
    )
    return {field: settlement[field] for field in fields}


def _normalize_proxy_evidence(
    raw: object,
    *,
    proxy_kind: str,
    episode_id: str,
    source_day: str,
    market_ticker: str,
    market_id: str,
    order_side: str,
    order_price_e4: int,
    order_qty_fp: Decimal,
    expected_wall_ns: int,
    expected_mono_ns: int,
    expected_sequence: int,
    expected_stable_source_id: str,
    delta_qty: Decimal,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not isinstance(raw, Mapping):
        raise PostfillV41ContractError(
            f"{proxy_kind} public_proxy_evidence: expected mapping"
        )
    fields = (*PROXY_HASH_FIELDS, "public_trade_rows", "source_rows_sha256")
    _only(raw, fields, f"{proxy_kind} public_proxy_evidence")
    _require(raw, fields, f"{proxy_kind} public_proxy_evidence")
    row = dict(raw)
    for field in (
        "evidence_id",
        "stable_source_id",
        "market_ticker",
        "market_id",
        "trigger_public_trade_row_id",
    ):
        row[field] = _text(row[field], field)
    if (
        row["proxy_kind"] != proxy_kind
        or row["market_ticker"] != market_ticker
        or row["market_id"] != market_id
    ):
        raise PostfillV41ContractError(
            f"{proxy_kind} evidence market/kind binding mismatch"
        )
    expected_maker_book_side = "BID" if order_side == "YES" else "ASK"
    expected_taker_book_side = "ASK" if order_side == "YES" else "BID"
    expected_taker_outcome_side = "NO" if order_side == "YES" else "YES"
    order_price = _legal_price(
        row["maker_order_outcome_price_e4"],
        "maker_order_outcome_price_e4",
    )
    maker_yes_price = (
        order_price
        if order_side == "YES"
        else 10_000 - order_price
    )
    supplied_maker_yes_price = _legal_price(
        row["maker_order_yes_price_e4"],
        "maker_order_yes_price_e4",
    )
    order_qty = _decimal(
        row["maker_order_qty_fp"],
        "maker_order_qty_fp",
    )
    if (
        row["maker_order_yes_book_side"] != expected_maker_book_side
        or row["maker_order_outcome_side"] != order_side
        or order_price != order_price_e4
        or supplied_maker_yes_price != maker_yes_price
        or order_qty != order_qty_fp
    ):
        raise PostfillV41ContractError(
            "maker outcome-cost fields do not map to the single YES book"
        )
    recv_wall = _integer(row["recv_wall_ns"], "recv_wall_ns", minimum=1)
    recv_mono = _integer(row["recv_mono_ns"], "recv_mono_ns", minimum=1)
    sequence = _integer(
        row["ingest_sequence"],
        "ingest_sequence",
        minimum=0,
    )
    if (
        recv_wall != expected_wall_ns
        or recv_mono != expected_mono_ns
        or sequence != expected_sequence
        or row["stable_source_id"] != expected_stable_source_id
    ):
        raise PostfillV41ContractError(
            f"{proxy_kind} evidence receipt/stable-id binding mismatch"
        )
    raw_trades = row["public_trade_rows"]
    if not isinstance(raw_trades, list) or not raw_trades:
        raise PostfillV41ContractError(
            "strict full proxy requires a non-empty public trade-row spine"
        )
    trade_fields = (*PUBLIC_TRADE_HASH_FIELDS, "source_rows_sha256")
    normalized_trades: list[dict[str, object]] = []
    prior_key: tuple[int, int, int, str] | None = None
    for trade_index, raw_trade in enumerate(raw_trades):
        if not isinstance(raw_trade, Mapping):
            raise PostfillV41ContractError(
                "public trade-row spine contains a non-mapping row"
            )
        _only(raw_trade, trade_fields, "public trade row")
        _require(raw_trade, trade_fields, "public trade row")
        trade = dict(raw_trade)
        trade_id = _text(trade["trade_id"], "trade_id")
        ticker = _text(trade["ticker"], "trade ticker")
        stable_id = _text(
            trade["stable_source_id"],
            "trade stable_source_id",
        )
        yes_price = _legal_price(
            trade["yes_price_e4"],
            "trade yes_price_e4",
        )
        count = _decimal(trade["count_fp"], "trade count_fp")
        trade_wall = _integer(
            trade["recv_wall_ns"],
            "trade recv_wall_ns",
            minimum=1,
        )
        trade_mono = _integer(
            trade["recv_mono_ns"],
            "trade recv_mono_ns",
            minimum=1,
        )
        trade_sequence = _integer(
            trade["ingest_sequence"],
            "trade ingest_sequence",
            minimum=0,
        )
        strict_yes_price_through = (
            yes_price < maker_yes_price
            if order_side == "YES"
            else yes_price > maker_yes_price
        )
        if (
            ticker != market_ticker
            or trade["taker_book_side"] != expected_taker_book_side
            or trade["taker_outcome_side"]
            != expected_taker_outcome_side
            or _boolean(trade["is_block_trade"], "is_block_trade")
            or not strict_yes_price_through
            or count <= 0
            or not _multiple_of(count, delta_qty)
        ):
            raise PostfillV41ContractError(
                "public trade spine is not non-block taker flow strictly "
                "through the resting maker price on the YES-book scale"
            )
        key = (trade_wall, trade_mono, trade_sequence, stable_id)
        if (
            key > (recv_wall, recv_mono, sequence, row["stable_source_id"])
            or (prior_key is not None and key <= prior_key)
        ):
            raise PostfillV41ContractError(
                "public trade spine receipt order is non-causal"
            )
        prior_key = key
        supplied_trade_sha = _sha(
            trade["source_rows_sha256"],
            "public trade source hash",
        )
        if supplied_trade_sha != public_trade_row_sha256(trade):
            raise PostfillV41ContractError(
                "public trade row source hash mismatch"
            )
        normalized_trades.append(
            {
                "public_trade_row_id": (
                    f"{row['evidence_id']}::TRADE::{trade_index}"
                ),
                "evidence_id": row["evidence_id"],
                "contract_version": CONTRACT_VERSION,
                "source_date_utc": source_day,
                "market_ticker": market_ticker,
                "market_id": market_id,
                "postfill_episode_id": episode_id,
                "trade_index": trade_index,
                "trade_id": trade_id,
                "taker_book_side": trade["taker_book_side"],
                "taker_outcome_side": trade["taker_outcome_side"],
                "yes_price_e4": yes_price,
                "count_fp": count,
                "is_block_trade": False,
                "recv_wall_ns": trade_wall,
                "recv_mono_ns": trade_mono,
                "ingest_sequence": trade_sequence,
                "stable_source_id": stable_id,
                "source_rows_sha256": supplied_trade_sha,
            }
        )
    trigger = normalized_trades[-1]
    cumulative = sum(
        (trade["count_fp"] for trade in normalized_trades),
        Decimal("0"),
    )
    raw_spine = [dict(trade) for trade in raw_trades]
    spine_sha = public_trade_spine_sha256(raw_spine)
    for normalized_trade in normalized_trades:
        normalized_trade["public_trade_spine_sha256"] = spine_sha
    supplied_cumulative = _decimal(
        row["derived_cumulative_strict_through_qty_fp"],
        "derived_cumulative_strict_through_qty_fp",
    )
    if (
        row["trigger_public_trade_row_id"]
        != trigger["public_trade_row_id"]
        or trigger["recv_wall_ns"] != recv_wall
        or trigger["recv_mono_ns"] != recv_mono
        or trigger["ingest_sequence"] != sequence
        or trigger["stable_source_id"] != row["stable_source_id"]
        or supplied_cumulative != cumulative
        or cumulative < order_qty
        or _sha(
            row["public_trade_spine_sha256"],
            "public trade spine hash",
        )
        != spine_sha
    ):
        raise PostfillV41ContractError(
            "public proxy summary is not derived from its trade-row spine"
        )
    supplied = _sha(row["source_rows_sha256"], "proxy evidence source hash")
    if supplied != public_proxy_evidence_sha256(row):
        raise PostfillV41ContractError("proxy evidence source hash mismatch")
    row.update(
        {
            "postfill_episode_id": episode_id,
            "source_date_utc": source_day,
            "maker_order_outcome_price_e4": order_price,
            "maker_order_yes_price_e4": maker_yes_price,
            "maker_order_qty_fp": order_qty,
            "derived_cumulative_strict_through_qty_fp": cumulative,
            "public_trade_spine_sha256": spine_sha,
            "recv_wall_ns": recv_wall,
            "recv_mono_ns": recv_mono,
            "ingest_sequence": sequence,
            "source_rows_sha256": supplied,
        }
    )
    del row["public_trade_rows"]
    return row, normalized_trades


def required_decision_grid(
    terminal_elapsed_ms: Decimal,
    *,
    zero_time_atom: bool,
) -> tuple[tuple[int, int], ...]:
    if zero_time_atom:
        if terminal_elapsed_ms != 0:
            raise PostfillV41ContractError(
                "zero-time atom must terminate at elapsed zero"
            )
        return ()
    if terminal_elapsed_ms <= 0:
        raise PostfillV41ContractError(
            "continuous episode must terminate after zero"
        )
    return tuple(
        (index, elapsed)
        for index, elapsed in enumerate(DECISION_GRID_MS)
        if Decimal(elapsed) < terminal_elapsed_ms
    )


def _normalize_episode(
    raw: Mapping[str, object],
    expected_days: Sequence[str],
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object] | None,
]:
    common_fields = (
        "experiment_id",
        "data_role",
        "source_date_utc",
        "market_ticker",
        "market_id",
        "market_metadata",
        "postfill_episode_id",
        "entry_episode_id",
        "entry_action_id",
        "source_rows_sha256",
        "data_origin",
        "first_fill_provenance",
        "first_fill_execution_nature",
        "first_fill_trade_fee_provenance",
        "first_fill_side",
        "first_fill_price_e4",
        "first_fill_qty_fp",
        "first_fill_trade_fee_usd",
        "first_fill_recv_wall_ns",
        "first_fill_recv_mono_ns",
        "first_fill_ingest_sequence",
        "first_fill_stable_source_id",
        "first_fill_public_proxy_evidence",
        "first_fill_elapsed_ms",
        "first_fill_tte_ms",
        "market_close_elapsed_ms",
        "complement_order_id",
        "complement_price_e4",
        "complement_order_age_at_first_fill_ms",
        "public_settlement_receipt",
        "terminal_elapsed_ms",
        "terminal_wall_ns",
        "terminal_mono_ns",
        "terminal_type",
        "zero_time_atom",
        "market_day_gate_pass",
        "reconciliation_ok",
        "data_invalid",
        "account_rounding_target_usd",
        "account_rounding_target_provenance",
        "account_precision_authenticated",
        "target_account_class",
        "account_precision_receipt_path",
        "account_precision_receipt_sha256",
        "fee_schedule_provenance",
        "fee_schedule_source",
    )
    complement_fields = (
        "terminal_complement_fill_price_e4",
        "terminal_complement_fill_qty_fp",
        "terminal_complement_trade_fee_usd",
        "terminal_complement_fill_provenance",
        "terminal_complement_execution_nature",
        "terminal_complement_trade_fee_provenance",
        "terminal_complement_fill_stable_source_id",
        "terminal_complement_fill_ingest_sequence",
        "terminal_complement_public_proxy_evidence",
    )
    hard_fields = (
        "complement_release_elapsed_ms",
        "complement_release_wall_ns",
        "complement_release_mono_ns",
        "complement_release_event_id",
        "complement_release_source_rows_sha256",
        "complement_release_provenance",
    )
    zero_fields = (
        "receipt_envelope_id",
        "complement_side",
        "complement_fill_price_e4",
        "complement_fill_qty_fp",
        "complement_fill_trade_fee_usd",
        "complement_fill_provenance",
        "complement_execution_nature",
        "complement_trade_fee_provenance",
        "complement_fill_stable_source_id",
        "complement_fill_ingest_sequence",
        "complement_public_proxy_evidence",
    )
    allowed = (*common_fields, *complement_fields, *hard_fields, *zero_fields)
    _only(raw, allowed, "V4.1 episode manifest")
    _require(raw, common_fields, "V4.1 episode manifest")
    row = dict(raw)
    day = _day(row["source_date_utc"])
    if day not in expected_days:
        raise PostfillV41ContractError("episode source day is unexpected")
    if row["experiment_id"] != EXPERIMENT_ID or row["data_role"] != "DISCOVERY":
        raise PostfillV41ContractError("experiment/data-role mismatch")
    if row["data_origin"] != DATA_ORIGIN:
        raise PostfillV41ContractError("episode data_origin must be PUBLIC_RAW")
    if (
        _boolean(row["data_invalid"], "data_invalid")
        or not _boolean(row["market_day_gate_pass"], "market_day_gate_pass")
        or not _boolean(row["reconciliation_ok"], "reconciliation_ok")
    ):
        raise PostfillV41ContractError("non-green market day")
    for field in (
        "market_ticker",
        "market_id",
        "postfill_episode_id",
        "entry_episode_id",
        "entry_action_id",
        "complement_order_id",
        "first_fill_stable_source_id",
    ):
        row[field] = _text(row[field], field)
    row["source_rows_sha256"] = _sha(
        row["source_rows_sha256"],
        "episode source_rows_sha256",
    )
    if (
        row["first_fill_provenance"] != FILL_PROVENANCE
        or row["first_fill_execution_nature"] != EXECUTION_NATURE
        or row["first_fill_trade_fee_provenance"]
        != MAKER_TRADE_FEE_PROVENANCE
    ):
        raise PostfillV41ContractError("first fill provenance mismatch")
    if (
        row["fee_schedule_provenance"] != FEE_SCHEDULE_PROVENANCE
        or row["fee_schedule_source"] != FEE_SCHEDULE_SOURCE
    ):
        raise PostfillV41ContractError("fee schedule is not officially pinned")
    account_target = _decimal(
        row["account_rounding_target_usd"],
        "account_rounding_target_usd",
    )
    authenticated = _boolean(
        row["account_precision_authenticated"],
        "account_precision_authenticated",
    )
    if (
        not authenticated
        or row["target_account_class"] != TARGET_ACCOUNT_CLASS
        or account_target != TARGET_ACCOUNT_ROUNDING_TARGET_USD
        or row["account_precision_receipt_path"]
        != TARGET_ACCOUNT_PRECISION_RECEIPT_PATH
        or _sha(
            row["account_precision_receipt_sha256"],
            "account_precision_receipt_sha256",
        )
        != TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
    ):
        raise PostfillV41ContractError(
            "target DIRECT account precision receipt is not pinned"
        )
    _text(
        row["account_rounding_target_provenance"],
        "account_rounding_target_provenance",
    )
    first_wall = _integer(
        row["first_fill_recv_wall_ns"],
        "first_fill_recv_wall_ns",
        minimum=1,
    )
    first_mono = _integer(
        row["first_fill_recv_mono_ns"],
        "first_fill_recv_mono_ns",
        minimum=1,
    )
    first_sequence = _integer(
        row["first_fill_ingest_sequence"],
        "first_fill_ingest_sequence",
        minimum=0,
    )
    metadata = _normalize_market_metadata(
        row["market_metadata"],
        episode_ticker=row["market_ticker"],
        episode_market_id=row["market_id"],
        source_day=day,
        first_wall_ns=first_wall,
    )
    delta = metadata["market_min_fill_increment_fp"]
    first_side = str(row["first_fill_side"])
    if first_side not in ("YES", "NO"):
        raise PostfillV41ContractError("first_fill_side invalid")
    first_price = _legal_price(
        row["first_fill_price_e4"],
        "first_fill_price_e4",
    )
    complement_price = _legal_price(
        row["complement_price_e4"],
        "complement_price_e4",
    )
    if first_price % 100 != 0 or complement_price % 100 != 0:
        raise PostfillV41ContractError(
            "primary candidate maker quotes must be whole-cent aligned; "
            "tapered subcent quotes are sensitivity-only"
        )
    first_qty = _decimal(row["first_fill_qty_fp"], "first_fill_qty_fp")
    if first_qty <= 0 or not _multiple_of(first_qty, delta):
        raise PostfillV41ContractError(
            "first fill qty is not a legal minimum increment"
        )
    first_trade_fee = _decimal(
        row["first_fill_trade_fee_usd"],
        "first_fill_trade_fee_usd",
    )
    if first_trade_fee != 0:
        raise PostfillV41ContractError(
            "maker trade fee must be zero; net rounding fee is separate"
        )
    maker_nmax, first_fee_upper = _maker_net_fee_safe_upper(
        first_qty,
        price_e4=first_price,
        delta_qty=delta,
        account_rounding_target=account_target,
    )
    _complement_nmax, complement_fee_upper = _maker_net_fee_safe_upper(
        first_qty,
        price_e4=complement_price,
        delta_qty=delta,
        account_rounding_target=account_target,
    )
    _integer(row["first_fill_tte_ms"], "first_fill_tte_ms", minimum=0)
    first_elapsed = _decimal(
        row["first_fill_elapsed_ms"],
        "first_fill_elapsed_ms",
    )
    if first_elapsed < 0:
        raise PostfillV41ContractError("negative first_fill_elapsed_ms")
    market_close = _decimal(
        row["market_close_elapsed_ms"],
        "market_close_elapsed_ms",
    )
    metadata_close_elapsed = (
        Decimal(
            metadata["market_window_close_wall_ns"] - first_wall
        )
        / Decimal("1000000")
    )
    if market_close != metadata_close_elapsed or market_close <= 0:
        raise PostfillV41ContractError(
            "market close elapsed does not match bound market window"
        )
    settlement = _normalize_settlement(
        row["public_settlement_receipt"],
        market_ticker=row["market_ticker"],
        market_id=row["market_id"],
        source_day=day,
        first_wall_ns=first_wall,
        first_mono_ns=first_mono,
        market_close_elapsed_ms=market_close,
    )
    first_evidence, first_trade_rows = _normalize_proxy_evidence(
        row["first_fill_public_proxy_evidence"],
        proxy_kind="FIRST_FILL",
        episode_id=row["postfill_episode_id"],
        source_day=day,
        market_ticker=row["market_ticker"],
        market_id=row["market_id"],
        order_side=first_side,
        order_price_e4=first_price,
        order_qty_fp=first_qty,
        expected_wall_ns=first_wall,
        expected_mono_ns=first_mono,
        expected_sequence=first_sequence,
        expected_stable_source_id=row["first_fill_stable_source_id"],
        delta_qty=delta,
    )
    first_evidence["market_metadata_receipt_id"] = metadata[
        "market_metadata_receipt_id"
    ]
    first_evidence["market_metadata_source_rows_sha256"] = metadata[
        "source_rows_sha256"
    ]
    terminal = _decimal(row["terminal_elapsed_ms"], "terminal_elapsed_ms")
    terminal_wall = _integer(
        row["terminal_wall_ns"],
        "terminal_wall_ns",
        minimum=1,
    )
    terminal_mono = _integer(
        row["terminal_mono_ns"],
        "terminal_mono_ns",
        minimum=1,
    )
    terminal_ns = _elapsed_ns(terminal)
    if (
        terminal_wall != first_wall + terminal_ns
        or terminal_mono != first_mono + terminal_ns
    ):
        raise PostfillV41ContractError("terminal receipt clocks do not conserve")
    terminal_type = str(row["terminal_type"])
    zero = _boolean(row["zero_time_atom"], "zero_time_atom")
    grid = required_decision_grid(terminal, zero_time_atom=zero)
    evidence_rows = [first_evidence]
    public_trade_rows = list(first_trade_rows)
    complement_side = "NO" if first_side == "YES" else "YES"
    zero_atom: dict[str, object] | None = None
    release_elapsed: Decimal | None = None
    if zero:
        if terminal_type != ZERO_TIME_TERMINAL:
            raise PostfillV41ContractError("zero atom terminal type mismatch")
        _require(row, zero_fields, "zero-time atom")
        if (
            row["complement_side"] != complement_side
            or _legal_price(
                row["complement_fill_price_e4"],
                "complement_fill_price_e4",
            )
            != complement_price
            or _decimal(
                row["complement_fill_qty_fp"],
                "complement_fill_qty_fp",
            )
            != first_qty
            or _decimal(
                row["complement_fill_trade_fee_usd"],
                "complement_fill_trade_fee_usd",
            )
            != 0
            or row["complement_fill_provenance"] != FILL_PROVENANCE
            or row["complement_execution_nature"] != EXECUTION_NATURE
            or row["complement_trade_fee_provenance"]
            != MAKER_TRADE_FEE_PROVENANCE
        ):
            raise PostfillV41ContractError("zero atom complement mismatch")
        comp_sequence = _integer(
            row["complement_fill_ingest_sequence"],
            "complement_fill_ingest_sequence",
            minimum=0,
        )
        comp_stable = _text(
            row["complement_fill_stable_source_id"],
            "complement_fill_stable_source_id",
        )
        if comp_sequence <= first_sequence:
            raise PostfillV41ContractError(
                "same-envelope zero atom requires later ingest sequence"
            )
        complement_evidence, complement_trade_rows = _normalize_proxy_evidence(
            row["complement_public_proxy_evidence"],
            proxy_kind="COMPLEMENT_FILL",
            episode_id=row["postfill_episode_id"],
            source_day=day,
            market_ticker=row["market_ticker"],
            market_id=row["market_id"],
            order_side=complement_side,
            order_price_e4=complement_price,
            order_qty_fp=first_qty,
            expected_wall_ns=first_wall,
            expected_mono_ns=first_mono,
            expected_sequence=comp_sequence,
            expected_stable_source_id=comp_stable,
            delta_qty=delta,
        )
        complement_evidence["market_metadata_receipt_id"] = metadata[
            "market_metadata_receipt_id"
        ]
        complement_evidence[
            "market_metadata_source_rows_sha256"
        ] = metadata["source_rows_sha256"]
        evidence_rows.append(complement_evidence)
        public_trade_rows.extend(complement_trade_rows)
    elif terminal_type == "COMPLEMENT_FILL":
        _require(row, complement_fields, "continuous complement terminal")
        if terminal >= market_close:
            raise PostfillV41ContractError(
                "COMPLEMENT_FILL must be strictly before market close"
            )
        comp_price = _legal_price(
            row["terminal_complement_fill_price_e4"],
            "terminal_complement_fill_price_e4",
        )
        comp_qty = _decimal(
            row["terminal_complement_fill_qty_fp"],
            "terminal_complement_fill_qty_fp",
        )
        comp_trade_fee = _decimal(
            row["terminal_complement_trade_fee_usd"],
            "terminal_complement_trade_fee_usd",
        )
        comp_sequence = _integer(
            row["terminal_complement_fill_ingest_sequence"],
            "terminal_complement_fill_ingest_sequence",
            minimum=0,
        )
        comp_stable = _text(
            row["terminal_complement_fill_stable_source_id"],
            "terminal_complement_fill_stable_source_id",
        )
        if (
            comp_price != complement_price
            or comp_qty != first_qty
            or comp_trade_fee != 0
            or row["terminal_complement_fill_provenance"]
            != FILL_PROVENANCE
            or row["terminal_complement_execution_nature"]
            != EXECUTION_NATURE
            or row["terminal_complement_trade_fee_provenance"]
            != MAKER_TRADE_FEE_PROVENANCE
            or comp_sequence <= first_sequence
        ):
            raise PostfillV41ContractError(
                "continuous complement public proxy mismatch"
            )
        complement_evidence, complement_trade_rows = _normalize_proxy_evidence(
            row["terminal_complement_public_proxy_evidence"],
            proxy_kind="COMPLEMENT_FILL",
            episode_id=row["postfill_episode_id"],
            source_day=day,
            market_ticker=row["market_ticker"],
            market_id=row["market_id"],
            order_side=complement_side,
            order_price_e4=complement_price,
            order_qty_fp=first_qty,
            expected_wall_ns=terminal_wall,
            expected_mono_ns=terminal_mono,
            expected_sequence=comp_sequence,
            expected_stable_source_id=comp_stable,
            delta_qty=delta,
        )
        complement_evidence["market_metadata_receipt_id"] = metadata[
            "market_metadata_receipt_id"
        ]
        complement_evidence[
            "market_metadata_source_rows_sha256"
        ] = metadata["source_rows_sha256"]
        evidence_rows.append(complement_evidence)
        public_trade_rows.extend(complement_trade_rows)
        release_elapsed = terminal
    elif terminal_type == "HARD_FALLBACK":
        _require(row, hard_fields, "hard-fallback release event")
        release_elapsed = _decimal(
            row["complement_release_elapsed_ms"],
            "complement_release_elapsed_ms",
        )
        release_ns = _elapsed_ns(release_elapsed)
        if (
            release_elapsed < market_close
            or release_elapsed > settlement["settlement_release_elapsed_ms"]
            or _integer(
                row["complement_release_wall_ns"],
                "complement_release_wall_ns",
                minimum=1,
            )
            != first_wall + release_ns
            or _integer(
                row["complement_release_mono_ns"],
                "complement_release_mono_ns",
                minimum=1,
            )
            != first_mono + release_ns
            or row["complement_release_provenance"]
            != ORDER_RELEASE_PROVENANCE
        ):
            raise PostfillV41ContractError(
                "HARD complement release is not a sealed post-close "
                "cancel-effective/order-expiry scenario"
            )
        release_id = _text(
            row["complement_release_event_id"],
            "complement_release_event_id",
        )
        expected_release_sha = _canonical_sha256(
            {
                "market_ticker": row["market_ticker"],
                "market_id": row["market_id"],
                "release_elapsed_ms": release_elapsed,
                "release_wall_ns": row["complement_release_wall_ns"],
                "release_mono_ns": row["complement_release_mono_ns"],
                "release_event_id": release_id,
                "provenance": ORDER_RELEASE_PROVENANCE,
            }
        )
        if _sha(
            row["complement_release_source_rows_sha256"],
            "complement release source hash",
        ) != expected_release_sha:
            raise PostfillV41ContractError(
                "complement release scenario source hash mismatch"
            )
        if terminal != settlement["settlement_release_elapsed_ms"]:
            raise PostfillV41ContractError(
                "HARD terminal must be actual public settlement receipt"
            )
        if (
            terminal_wall != settlement["settlement_recv_wall_ns"]
            or terminal_mono != settlement["settlement_recv_mono_ns"]
        ):
            raise PostfillV41ContractError(
                "HARD terminal is backfilled rather than public receipt"
            )
    else:
        raise PostfillV41ContractError(
            f"unknown continuous terminal {terminal_type!r}"
        )
    basis_principal = first_qty * Decimal(first_price) / E4
    complement_principal = first_qty * Decimal(complement_price) / E4
    first_basis = basis_principal + first_fee_upper
    complement_reservation = (
        complement_principal + complement_fee_upper
    )
    locked = first_basis + complement_reservation
    row.update(
        {
            "source_date_utc": day,
            "market_metadata": metadata,
            "public_settlement_receipt": settlement,
            "fee_schedule_receipt": _frozen_fee_schedule_receipt(),
            "first_fill_price_e4": first_price,
            "first_fill_qty_fp": first_qty,
            "first_fill_trade_fee_usd": first_trade_fee,
            "first_fill_recv_wall_ns": first_wall,
            "first_fill_recv_mono_ns": first_mono,
            "first_fill_ingest_sequence": first_sequence,
            "first_fill_elapsed_ms": first_elapsed,
            "market_close_elapsed_ms": market_close,
            "complement_price_e4": complement_price,
            "terminal_elapsed_ms": terminal,
            "terminal_wall_ns": terminal_wall,
            "terminal_mono_ns": terminal_mono,
            "terminal_type": terminal_type,
            "zero_time_atom": zero,
            "required_grid": grid,
            "delta_qty_fp": delta,
            "account_rounding_target_usd": account_target,
            "maker_true_fill_n_max": maker_nmax,
            "first_maker_net_fee_safe_upper_usd": first_fee_upper,
            "complement_maker_net_fee_safe_upper_usd": (
                complement_fee_upper
            ),
            "first_leg_principal_usd": basis_principal,
            "first_leg_basis_safe_upper_usd": first_basis,
            "complement_principal_usd": complement_principal,
            "complement_reservation_safe_upper_usd": (
                complement_reservation
            ),
            "locked_pair_capital_safe_upper_usd": locked,
            "settlement_release_elapsed_ms": settlement[
                "settlement_release_elapsed_ms"
            ],
            "complement_release_elapsed_ms": release_elapsed,
            "first_proxy_evidence_id": first_evidence["evidence_id"],
            "complement_proxy_evidence_id": (
                evidence_rows[1]["evidence_id"]
                if len(evidence_rows) == 2
                else None
            ),
        }
    )
    if zero:
        zero_atom = {
            "postfill_episode_id": row["postfill_episode_id"],
            "contract_version": CONTRACT_VERSION,
            "source_date_utc": day,
            "market_ticker": row["market_ticker"],
            "market_id": row["market_id"],
            "market_metadata_receipt_id": metadata[
                "market_metadata_receipt_id"
            ],
            "settlement_receipt_id": settlement[
                "settlement_receipt_id"
            ],
            "fee_schedule_receipt_id": FEE_SCHEDULE_RECEIPT_ID,
            "data_origin": DATA_ORIGIN,
            "first_fill_provenance": FILL_PROVENANCE,
            "complement_fill_provenance": FILL_PROVENANCE,
            "first_fill_execution_nature": EXECUTION_NATURE,
            "complement_execution_nature": EXECUTION_NATURE,
            "first_fill_trade_fee_provenance": (
                MAKER_TRADE_FEE_PROVENANCE
            ),
            "complement_trade_fee_provenance": (
                MAKER_TRADE_FEE_PROVENANCE
            ),
            "first_fill_public_proxy_evidence_id": (
                first_evidence["evidence_id"]
            ),
            "complement_public_proxy_evidence_id": (
                evidence_rows[1]["evidence_id"]
            ),
            "receipt_envelope_id": _text(
                row["receipt_envelope_id"],
                "receipt_envelope_id",
            ),
            "atom_recv_wall_ns": first_wall,
            "atom_recv_mono_ns": first_mono,
            "first_fill_ingest_sequence": first_sequence,
            "complement_fill_ingest_sequence": row[
                "complement_fill_ingest_sequence"
            ],
            "first_side": first_side,
            "complement_side": complement_side,
            "first_price_e4": first_price,
            "complement_price_e4": complement_price,
            "first_qty_fp": first_qty,
            "complement_qty_fp": first_qty,
            "first_leg_basis_safe_upper_usd": first_basis,
            "complement_reservation_safe_upper_usd": (
                complement_reservation
            ),
            "locked_pair_capital_safe_upper_usd": locked,
            "source_rows_sha256": row["source_rows_sha256"],
            "reconciliation_ok": True,
        }
    return row, evidence_rows, public_trade_rows, zero_atom


STATE_COMMON_FIELDS = (
    "source_date_utc",
    "postfill_episode_id",
    "decision_index",
    "decision_elapsed_ms",
    "action_family_version",
    "action_kind",
    "causal_source_rows_sha256",
    "source_max_recv_wall_ns",
    "source_max_recv_mono_ns",
    "source_max_ingest_sequence",
    "source_max_stable_id",
    "decision_recv_wall_ns",
    "decision_recv_mono_ns",
    "simulated_cancel_state",
    "simulated_available_cash_before_cancel_usd",
    "flatten_visible_slices",
    "simulated_strategy_order_registry_complete",
    "simulated_strategy_order_registry_sha256",
    "legal_action",
    "skip_reason",
    "effective_latency_ms",
    "time_in_force",
    "self_trade_prevention_type",
    "fok_requires_prior_synthetic_cancel_applied",
    "fok_book_side",
    "fok_limit_price_e4",
    "fok_limit_fallback",
    "reconciliation_ok",
    "data_invalid",
)


def _normalize_state(
    raw: Mapping[str, object],
    episode: Mapping[str, object],
) -> dict[str, object]:
    _only(raw, STATE_COMMON_FIELDS, "V4.1 compact state/action")
    _require(raw, STATE_COMMON_FIELDS, "V4.1 compact state/action")
    row = dict(raw)
    if (
        _day(row["source_date_utc"]) != episode["source_date_utc"]
        or row["postfill_episode_id"] != episode["postfill_episode_id"]
        or row["action_family_version"] != ACTION_FAMILY_VERSION
        or row["action_kind"] not in ALLOWED_ACTIONS
    ):
        raise PostfillV41ContractError("state/action linkage mismatch")
    index = _integer(row["decision_index"], "decision_index", minimum=0)
    elapsed = _integer(
        row["decision_elapsed_ms"],
        "decision_elapsed_ms",
        minimum=0,
    )
    if (index, elapsed) not in episode["required_grid"]:
        raise PostfillV41ContractError("state is not a sealed at-risk grid")
    elapsed_ns = elapsed * 1_000_000
    decision_wall = _integer(
        row["decision_recv_wall_ns"],
        "decision_recv_wall_ns",
        minimum=1,
    )
    decision_mono = _integer(
        row["decision_recv_mono_ns"],
        "decision_recv_mono_ns",
        minimum=1,
    )
    if (
        decision_wall != episode["first_fill_recv_wall_ns"] + elapsed_ns
        or decision_mono != episode["first_fill_recv_mono_ns"] + elapsed_ns
    ):
        raise PostfillV41ContractError("decision grid clocks do not conserve")
    source_wall = _integer(
        row["source_max_recv_wall_ns"],
        "source_max_recv_wall_ns",
        minimum=1,
    )
    source_mono = _integer(
        row["source_max_recv_mono_ns"],
        "source_max_recv_mono_ns",
        minimum=1,
    )
    source_sequence = _integer(
        row["source_max_ingest_sequence"],
        "source_max_ingest_sequence",
        minimum=0,
    )
    if (
        not episode["first_fill_recv_wall_ns"] <= source_wall <= decision_wall
        or not episode["first_fill_recv_mono_ns"]
        <= source_mono
        <= decision_mono
        or (
            source_wall == episode["first_fill_recv_wall_ns"]
            and source_mono == episode["first_fill_recv_mono_ns"]
            and source_sequence < episode["first_fill_ingest_sequence"]
        )
    ):
        raise PostfillV41ContractError("causal state receipt seal violated")
    if row["simulated_cancel_state"] != "NONE":
        raise PostfillV41ContractError(
            "historical simulated_cancel_state must be NONE"
        )
    if not _boolean(
        row["simulated_strategy_order_registry_complete"],
        "simulated_strategy_order_registry_complete",
    ):
        raise PostfillV41ContractError(
            "simulated strategy order registry is incomplete"
        )
    registry_sha = _sha(
        row["simulated_strategy_order_registry_sha256"],
        "simulated strategy registry hash",
    )
    causal_sha = _sha(
        row["causal_source_rows_sha256"],
        "causal_source_rows_sha256",
    )
    source_id = _text(row["source_max_stable_id"], "source_max_stable_id")
    if elapsed == 0 and (
        source_wall != episode["first_fill_recv_wall_ns"]
        or source_mono != episode["first_fill_recv_mono_ns"]
        or source_sequence != episode["first_fill_ingest_sequence"]
        or source_id != episode["first_fill_stable_source_id"]
        or causal_sha
        != episode["first_fill_public_proxy_evidence"][
            "source_rows_sha256"
        ]
    ):
        raise PostfillV41ContractError(
            "t=0 state must seal exactly at the first-fill receipt; "
            "same-time future rows are forbidden"
        )
    available_before = _decimal(
        row["simulated_available_cash_before_cancel_usd"],
        "simulated_available_cash_before_cancel_usd",
    )
    if available_before < 0:
        raise PostfillV41ContractError("negative simulated available cash")
    delta = episode["delta_qty_fp"]
    book_side = _canonical_flatten_book_side(episode["first_fill_side"])
    visible = _normalize_levels(
        row["flatten_visible_slices"],
        label="flatten_visible_slices",
        book_side=book_side,
        delta_qty=delta,
    )
    visible_qty = sum((qty for _price, qty in visible), Decimal("0"))
    worst = (
        visible[-1][0]
        if visible and visible_qty >= episode["first_fill_qty_fp"]
        else None
    )
    limit, adverse_tick, fallback = _adverse_limit(book_side, worst)
    quantity = episode["first_fill_qty_fp"]
    nmax = int(quantity / delta)
    limit_fee = _fee_bound_for_levels(
        ((limit, quantity),),
        delta_qty=delta,
        account_rounding_target=episode["account_rounding_target_usd"],
        book_side=book_side,
    )
    fee_upper = limit_fee["safe_upper"]
    principal = (
        quantity * Decimal(limit) / E4
        if book_side == "BID"
        else Decimal("0")
    )
    proceeds = (
        quantity * Decimal(limit) / E4
        if book_side == "ASK"
        else Decimal("0")
    )
    required_cash = (
        principal + fee_upper
        if book_side == "BID"
        else max(Decimal("0"), fee_upper - proceeds)
    )
    available_after = (
        available_before
        + episode["complement_reservation_safe_upper_usd"]
    )
    feasible = available_after >= required_cash
    candidate_reserve = required_cash
    peak_capital = max(
        episode["locked_pair_capital_safe_upper_usd"],
        episode["first_leg_basis_safe_upper_usd"] + candidate_reserve,
    )
    if not feasible:
        raise PostfillV41ContractError(
            "FOK principal+fee safe-upper cash requirement is infeasible"
        )
    if (
        _boolean(row["data_invalid"], "data_invalid")
        or not _boolean(row["reconciliation_ok"], "reconciliation_ok")
        or not _boolean(row["legal_action"], "legal_action")
        or row["skip_reason"] is not None
    ):
        raise PostfillV41ContractError("state/action is invalid or illegal")
    kind = str(row["action_kind"])
    latency = _decimal(row["effective_latency_ms"], "effective_latency_ms")
    if kind == "KEEP":
        if (
            latency != 0
            or row["time_in_force"] is not None
            or row["self_trade_prevention_type"] is not None
            or row["fok_requires_prior_synthetic_cancel_applied"] is not False
            or row["fok_book_side"] is not None
            or row["fok_limit_price_e4"] is not None
            or row["fok_limit_fallback"] is not None
        ):
            raise PostfillV41ContractError("KEEP execution fields invalid")
    else:
        if (
            latency != FOK_EFFECTIVE_LATENCY_MS
            or row["time_in_force"] != FOK_TIME_IN_FORCE
            or row["self_trade_prevention_type"] != "taker_at_cross"
            or row["fok_requires_prior_synthetic_cancel_applied"] is not True
            or row["fok_book_side"] != book_side
            or _legal_price(
                row["fok_limit_price_e4"],
                "fok_limit_price_e4",
            )
            != limit
            or _boolean(
                row["fok_limit_fallback"],
                "fok_limit_fallback",
            )
            != fallback
        ):
            raise PostfillV41ContractError(
                "FLATTEN_FOK frozen execution fields invalid"
            )
    row.update(
        {
            "decision_index": index,
            "decision_elapsed_ms": elapsed,
            "decision_recv_wall_ns": decision_wall,
            "decision_recv_mono_ns": decision_mono,
            "source_max_recv_wall_ns": source_wall,
            "source_max_recv_mono_ns": source_mono,
            "source_max_ingest_sequence": source_sequence,
            "source_max_stable_id": source_id,
            "causal_source_rows_sha256": causal_sha,
            "simulated_strategy_order_registry_sha256": registry_sha,
            "simulated_available_cash_before_cancel_usd": available_before,
            "simulated_available_cash_after_cancel_usd": available_after,
            "flatten_visible_slices": visible,
            "flatten_visible_slices_sha256": _canonical_sha256(visible),
            "flatten_book_side": book_side,
            "flatten_limit_price_e4": limit,
            "flatten_adverse_tick_e4": adverse_tick,
            "flatten_limit_fallback": fallback,
            "fok_fee_n_max": nmax,
            "fok_required_principal_usd": principal,
            "fok_expected_sale_proceeds_usd": proceeds,
            "fok_required_fee_safe_upper_usd": fee_upper,
            "fok_required_cash_with_fee_safe_upper_usd": required_cash,
            "fok_candidate_reserve_safe_upper_usd": candidate_reserve,
            "peak_strategy_capital_safe_upper_usd": peak_capital,
            "fok_cash_fee_feasible": feasible,
        }
    )
    return row


def _state_signature(state: Mapping[str, object]) -> str:
    excluded = {
        "action_kind",
        "effective_latency_ms",
        "time_in_force",
        "self_trade_prevention_type",
        "fok_requires_prior_synthetic_cancel_applied",
        "fok_book_side",
        "fok_limit_price_e4",
        "fok_limit_fallback",
    }
    return _canonical_sha256(
        {key: value for key, value in state.items() if key not in excluded}
    )


def _decision_id(episode_id: str, index: int) -> str:
    return f"{episode_id}::V41::GRID::{index}"


def _action_id(episode_id: str, index: int, action: str) -> str:
    return f"{_decision_id(episode_id, index)}::{action}"


def _serialize_state(
    state: Mapping[str, object],
    episode: Mapping[str, object],
    fingerprint: str,
) -> dict[str, object]:
    return {
        "postfill_decision_id": _decision_id(
            episode["postfill_episode_id"],
            state["decision_index"],
        ),
        "contract_version": CONTRACT_VERSION,
        "action_family_version": ACTION_FAMILY_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "data_origin": DATA_ORIGIN,
        "source_date_utc": episode["source_date_utc"],
        "market_ticker": episode["market_ticker"],
        "market_id": episode["market_id"],
        "market_metadata_receipt_id": episode["market_metadata"][
            "market_metadata_receipt_id"
        ],
        "market_metadata_source_rows_sha256": episode["market_metadata"][
            "source_rows_sha256"
        ],
        "fee_schedule_receipt_id": FEE_SCHEDULE_RECEIPT_ID,
        "fee_schedule_source_rows_sha256": episode[
            "fee_schedule_receipt"
        ]["source_rows_sha256"],
        "postfill_episode_id": episode["postfill_episode_id"],
        "entry_episode_id": episode["entry_episode_id"],
        "entry_action_id": episode["entry_action_id"],
        "decision_index": state["decision_index"],
        "decision_elapsed_ms": Decimal(state["decision_elapsed_ms"]),
        "decision_recv_wall_ns": state["decision_recv_wall_ns"],
        "decision_recv_mono_ns": state["decision_recv_mono_ns"],
        "source_max_recv_wall_ns": state["source_max_recv_wall_ns"],
        "source_max_recv_mono_ns": state["source_max_recv_mono_ns"],
        "source_max_ingest_sequence": state["source_max_ingest_sequence"],
        "source_max_stable_id": state["source_max_stable_id"],
        "causal_source_rows_sha256": state["causal_source_rows_sha256"],
        "paired_state_fingerprint_sha256": fingerprint,
        "simulated_cancel_state": "NONE",
        "first_side": episode["first_fill_side"],
        "first_price_e4": episode["first_fill_price_e4"],
        "first_qty_fp": episode["first_fill_qty_fp"],
        "first_maker_trade_fee_usd": Decimal("0"),
        "first_maker_net_fee_safe_upper_usd": episode[
            "first_maker_net_fee_safe_upper_usd"
        ],
        "first_leg_principal_usd": episode["first_leg_principal_usd"],
        "first_leg_basis_safe_upper_usd": episode[
            "first_leg_basis_safe_upper_usd"
        ],
        "complement_side": (
            "NO" if episode["first_fill_side"] == "YES" else "YES"
        ),
        "complement_price_e4": episode["complement_price_e4"],
        "complement_maker_trade_fee_usd": Decimal("0"),
        "complement_maker_net_fee_safe_upper_usd": episode[
            "complement_maker_net_fee_safe_upper_usd"
        ],
        "complement_principal_usd": episode["complement_principal_usd"],
        "complement_reservation_safe_upper_usd": episode[
            "complement_reservation_safe_upper_usd"
        ],
        "locked_pair_capital_safe_upper_usd": episode[
            "locked_pair_capital_safe_upper_usd"
        ],
        "market_close_elapsed_ms": episode["market_close_elapsed_ms"],
        "flatten_book_side": state["flatten_book_side"],
        "flatten_limit_price_e4": state["flatten_limit_price_e4"],
        "flatten_limit_fallback": state["flatten_limit_fallback"],
        "flatten_adverse_tick_e4": state["flatten_adverse_tick_e4"],
        "flatten_visible_slices_sha256": state[
            "flatten_visible_slices_sha256"
        ],
        "market_min_fill_increment_fp": episode["delta_qty_fp"],
        "trade_fee_quantum_usd": TRADE_FEE_QUANTUM_USD,
        "account_rounding_target_usd": episode[
            "account_rounding_target_usd"
        ],
        "target_account_class": TARGET_ACCOUNT_CLASS,
        "account_precision_receipt_sha256": (
            TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
        ),
        "fok_fee_n_max": state["fok_fee_n_max"],
        "fok_required_principal_usd": state[
            "fok_required_principal_usd"
        ],
        "fok_expected_sale_proceeds_usd": state[
            "fok_expected_sale_proceeds_usd"
        ],
        "fok_required_fee_safe_upper_usd": state[
            "fok_required_fee_safe_upper_usd"
        ],
        "simulated_available_cash_before_cancel_usd": state[
            "simulated_available_cash_before_cancel_usd"
        ],
        "simulated_available_cash_after_cancel_usd": state[
            "simulated_available_cash_after_cancel_usd"
        ],
        "fok_required_cash_with_fee_safe_upper_usd": state[
            "fok_required_cash_with_fee_safe_upper_usd"
        ],
        "fok_candidate_reserve_safe_upper_usd": state[
            "fok_candidate_reserve_safe_upper_usd"
        ],
        "peak_strategy_capital_safe_upper_usd": state[
            "peak_strategy_capital_safe_upper_usd"
        ],
        "fok_cash_fee_feasible": state["fok_cash_fee_feasible"],
        "fee_bound_selected_for_candidate": (
            FEE_BOUND_SELECTED_FOR_CANDIDATE
        ),
        "fee_accumulator_receipt_authenticated": False,
        "simulated_strategy_order_registry_sha256": state[
            "simulated_strategy_order_registry_sha256"
        ],
        "reconciliation_ok": True,
    }


def _serialize_action(
    state: Mapping[str, object],
    episode: Mapping[str, object],
    fingerprint: str,
) -> dict[str, object]:
    kind = state["action_kind"]
    return {
        "postfill_action_id": _action_id(
            episode["postfill_episode_id"],
            state["decision_index"],
            kind,
        ),
        "postfill_decision_id": _decision_id(
            episode["postfill_episode_id"],
            state["decision_index"],
        ),
        "contract_version": CONTRACT_VERSION,
        "action_family_version": ACTION_FAMILY_VERSION,
        "source_date_utc": episode["source_date_utc"],
        "market_ticker": episode["market_ticker"],
        "market_id": episode["market_id"],
        "postfill_episode_id": episode["postfill_episode_id"],
        "decision_index": state["decision_index"],
        "action_kind": kind,
        "paired_state_fingerprint_sha256": fingerprint,
        "requested_qty_fp": episode["first_fill_qty_fp"],
        "complement_old_price_e4": episode["complement_price_e4"],
        "fok_book_side": (
            state["flatten_book_side"] if kind == "FLATTEN_FOK" else None
        ),
        "fok_limit_price_e4": (
            state["flatten_limit_price_e4"]
            if kind == "FLATTEN_FOK"
            else None
        ),
        "fok_limit_fallback": (
            state["flatten_limit_fallback"]
            if kind == "FLATTEN_FOK"
            else None
        ),
        "effective_latency_ms": _decimal(
            state["effective_latency_ms"],
            "effective_latency_ms",
        ),
        "time_in_force": state["time_in_force"],
        "self_trade_prevention_type": state[
            "self_trade_prevention_type"
        ],
        "fok_requires_prior_synthetic_cancel_applied": state[
            "fok_requires_prior_synthetic_cancel_applied"
        ],
        "simulated_cancel_state_before_action": "NONE",
        "complement_reservation_safe_upper_usd": episode[
            "complement_reservation_safe_upper_usd"
        ],
        "locked_pair_capital_safe_upper_usd": episode[
            "locked_pair_capital_safe_upper_usd"
        ],
        "simulated_available_cash_before_cancel_usd": state[
            "simulated_available_cash_before_cancel_usd"
        ],
        "simulated_available_cash_after_cancel_usd": state[
            "simulated_available_cash_after_cancel_usd"
        ],
        "fok_required_principal_usd": (
            state["fok_required_principal_usd"]
            if kind == "FLATTEN_FOK"
            else Decimal("0")
        ),
        "fok_expected_sale_proceeds_usd": (
            state["fok_expected_sale_proceeds_usd"]
            if kind == "FLATTEN_FOK"
            else Decimal("0")
        ),
        "fok_required_fee_safe_upper_usd": (
            state["fok_required_fee_safe_upper_usd"]
            if kind == "FLATTEN_FOK"
            else Decimal("0")
        ),
        "fok_required_cash_with_fee_safe_upper_usd": (
            state["fok_required_cash_with_fee_safe_upper_usd"]
            if kind == "FLATTEN_FOK"
            else Decimal("0")
        ),
        "fok_candidate_reserve_safe_upper_usd": (
            state["fok_candidate_reserve_safe_upper_usd"]
            if kind == "FLATTEN_FOK"
            else Decimal("0")
        ),
        "peak_strategy_capital_safe_upper_usd": state[
            "peak_strategy_capital_safe_upper_usd"
        ],
        "fok_cash_fee_feasible": (
            state["fok_cash_fee_feasible"]
            if kind == "FLATTEN_FOK"
            else True
        ),
        "fee_bound_selected_for_candidate": (
            FEE_BOUND_SELECTED_FOR_CANDIDATE
        ),
        "legal_action": True,
    }


OUTCOME_REPORTED_FIELDS = (
    "reported_gross_pnl_usd",
    "reported_taker_fee_raw_total_usd",
    "reported_taker_fee_aggregate_centicent_lower_usd",
    "reported_taker_fee_public_l2_level_scenario_usd",
    "reported_taker_fee_trade_upper_before_account_rounding_usd",
    "reported_taker_fee_account_rounding_safe_upper_usd",
    "reported_taker_fee_partition_dp_no_rebate_upper_usd",
    "reported_taker_fee_sql_safe_upper_usd",
    "reported_taker_fee_safe_upper_usd",
    "reported_taker_fee_doc_literal_tight_sensitivity_usd",
    "reported_conservative_net_pnl_usd",
    "reported_capital_dollar_seconds",
)


OUTCOME_FIELDS = (
    "source_date_utc",
    "postfill_episode_id",
    "decision_index",
    "postfill_action_id",
    "terminal_type",
    "planned_effective_elapsed_ms",
    "synthetic_cancel_applied_wall_ns",
    "synthetic_cancel_applied_mono_ns",
    "synthetic_cancel_applied_sequence",
    "synthetic_cancel_applied_stable_source_id",
    "fok_processed_wall_ns",
    "fok_processed_mono_ns",
    "fok_processed_sequence",
    "fok_processed_stable_source_id",
    "fok_sent",
    "simulated_no_self_cross_verified_before_fok",
    "simulated_strategy_order_registry_sha256",
    "pre_effective_public_book",
    "race_complement_public_proxy_evidence_id",
    "fee_bound_selected_for_candidate",
    "fee_accumulator_receipt_authenticated",
    *OUTCOME_REPORTED_FIELDS,
    "reconciliation_ok",
    "data_invalid",
)


def _derive_execution(
    levels: Sequence[tuple[int, Decimal]],
    requested: Decimal,
) -> tuple[tuple[int, Decimal, Decimal], ...]:
    remaining = requested
    rows: list[tuple[int, Decimal, Decimal]] = []
    for price, source_qty in levels:
        executed = min(source_qty, remaining) if remaining > 0 else Decimal("0")
        rows.append((price, source_qty, executed))
        remaining -= executed
    return tuple(rows)


def _execution_economics(
    execution: Sequence[tuple[int, Decimal, Decimal]],
    *,
    first_side: str,
    first_price_e4: int,
    delta_qty: Decimal,
    account_rounding_target: Decimal,
    book_side: str,
) -> tuple[Decimal, dict[str, Decimal | int]]:
    gross = Decimal("0")
    executed_levels: list[tuple[int, Decimal]] = []
    for price, _source_qty, executed in execution:
        if executed <= 0:
            continue
        if first_side == "YES":
            unit = Decimal(price - first_price_e4) / E4
        else:
            unit = Decimal(10_000 - price - first_price_e4) / E4
        gross += unit * executed
        executed_levels.append((price, executed))
    fees = _fee_bound_for_levels(
        executed_levels,
        delta_qty=delta_qty,
        account_rounding_target=account_rounding_target,
        book_side=book_side,
    )
    return gross, fees


def derive_reported_outcome_fields(
    *,
    episode: Mapping[str, object],
    state: Mapping[str, object],
    terminal_type: str,
    pre_effective_book: Mapping[str, object] | None,
) -> dict[str, Decimal]:
    """Fixture/extractor helper; the SQL gate independently recomputes it."""
    q = episode["first_fill_qty_fp"]
    delta = episode["delta_qty_fp"]
    h = episode["account_rounding_target_usd"]
    effective = Decimal(state["decision_elapsed_ms"] + FOK_EFFECTIVE_LATENCY_MS)
    first_fee_upper = episode["first_maker_net_fee_safe_upper_usd"]
    first_basis = episode["first_leg_basis_safe_upper_usd"]
    locked = episode["locked_pair_capital_safe_upper_usd"]
    cancel_effective = effective
    fok_terminal = effective
    candidate_reserve = state[
        "fok_candidate_reserve_safe_upper_usd"
    ]
    settlement_elapsed = episode["settlement_release_elapsed_ms"]
    if terminal_type == "CANCEL_RACE_PAIR":
        terminal = episode["terminal_elapsed_ms"]
        gross = (
            Decimal(10_000)
            - Decimal(episode["first_fill_price_e4"])
            - Decimal(episode["complement_price_e4"])
        ) / E4 * q
        zero_fees = _fee_bound_for_levels(
            (),
            delta_qty=delta,
            account_rounding_target=h,
            book_side=state["flatten_book_side"],
        )
        conservative = (
            gross
            - first_fee_upper
            - episode["complement_maker_net_fee_safe_upper_usd"]
        )
        capital = locked * terminal / Decimal("1000")
        fees = zero_fees
    else:
        if pre_effective_book is None:
            raise PostfillV41ContractError("FOK outcome lacks public book")
        levels = tuple(
            (
                _legal_price(item["price_e4"], "book price"),
                _decimal(item["qty_fp"], "book qty"),
            )
            for item in pre_effective_book["levels"]
        )
        execution = _derive_execution(levels, q)
        available = sum((qty for _p, qty in levels), Decimal("0"))
        if terminal_type == "FOK_FULL" and available < q:
            raise PostfillV41ContractError("FOK_FULL lacks full public depth")
        if terminal_type == "FOK_ZERO" and available >= q:
            raise PostfillV41ContractError(
                "FOK_ZERO contradicts full public depth"
            )
        if terminal_type == "FOK_ZERO":
            execution = tuple(
                (price, source_qty, Decimal("0"))
                for price, source_qty in levels
            )
        gross, fees = _execution_economics(
            execution,
            first_side=episode["first_fill_side"],
            first_price_e4=episode["first_fill_price_e4"],
            delta_qty=delta,
            account_rounding_target=h,
            book_side=state["flatten_book_side"],
        )
        if terminal_type == "FOK_FULL":
            conservative = (
                gross - first_fee_upper - fees["safe_upper"]
            )
            capital = (
                locked * cancel_effective
                + (first_basis + candidate_reserve)
                * (fok_terminal - cancel_effective)
            ) / Decimal("1000")
        else:
            result = episode["public_settlement_receipt"][
                "official_market_result"
            ]
            settlement_gross = (
                (
                    Decimal(10_000 - episode["first_fill_price_e4"])
                    / E4
                )
                if result == episode["first_fill_side"]
                else -Decimal(episode["first_fill_price_e4"]) / E4
            ) * q
            gross = settlement_gross
            conservative = gross - first_fee_upper
            capital = (
                locked * cancel_effective
                + (first_basis + candidate_reserve)
                * (fok_terminal - cancel_effective)
                + first_basis * (settlement_elapsed - fok_terminal)
            ) / Decimal("1000")
            fees = _fee_bound_for_levels(
                (),
                delta_qty=delta,
                account_rounding_target=h,
                book_side=state["flatten_book_side"],
            )
    return {
        "reported_gross_pnl_usd": gross,
        "reported_taker_fee_raw_total_usd": fees["raw_total"],
        "reported_taker_fee_aggregate_centicent_lower_usd": fees[
            "aggregate_centicent_lower"
        ],
        "reported_taker_fee_public_l2_level_scenario_usd": fees[
            "public_l2_level_scenario"
        ],
        "reported_taker_fee_trade_upper_before_account_rounding_usd": fees[
            "trade_upper_before_account_rounding"
        ],
        "reported_taker_fee_account_rounding_safe_upper_usd": fees[
            "account_rounding_safe_upper"
        ],
        "reported_taker_fee_partition_dp_no_rebate_upper_usd": fees[
            "partition_dp_no_rebate_upper"
        ],
        "reported_taker_fee_sql_safe_upper_usd": fees[
            "sql_safe_upper"
        ],
        "reported_taker_fee_safe_upper_usd": fees["safe_upper"],
        "reported_taker_fee_doc_literal_tight_sensitivity_usd": fees[
            "doc_literal_tight_sensitivity"
        ],
        "reported_conservative_net_pnl_usd": conservative,
        "reported_capital_dollar_seconds": capital,
    }


def _normalize_book(
    raw: object,
    *,
    episode: Mapping[str, object],
    state: Mapping[str, object],
    effective_wall: int,
    effective_mono: int,
) -> tuple[dict[str, object], tuple[tuple[int, Decimal], ...]]:
    if not isinstance(raw, Mapping):
        raise PostfillV41ContractError(
            "pre_effective_public_book: expected mapping"
        )
    fields = (*BOOK_HASH_FIELDS, "levels", "source_rows_sha256")
    _only(raw, fields, "pre_effective_public_book")
    _require(raw, fields, "pre_effective_public_book")
    row = dict(raw)
    if (
        row["market_ticker"] != episode["market_ticker"]
        or row["market_id"] != episode["market_id"]
    ):
        raise PostfillV41ContractError("FOK book belongs to another market")
    recv_wall = _integer(row["recv_wall_ns"], "book recv_wall_ns", minimum=1)
    recv_mono = _integer(row["recv_mono_ns"], "book recv_mono_ns", minimum=1)
    sequence = _integer(
        row["ingest_sequence"],
        "book ingest_sequence",
        minimum=0,
    )
    stable = _text(row["stable_source_id"], "book stable_source_id")
    if not (
        state["decision_recv_wall_ns"] <= recv_wall < effective_wall
        and state["decision_recv_mono_ns"] <= recv_mono < effective_mono
    ):
        raise PostfillV41ContractError(
            "public FOK book must be strictly pre-effective"
        )
    levels = _normalize_levels(
        row["levels"],
        label="pre_effective_public_book.levels",
        book_side=state["flatten_book_side"],
        delta_qty=episode["delta_qty_fp"],
        limit_price_e4=state["flatten_limit_price_e4"],
    )
    if not levels:
        raise PostfillV41ContractError(
            "empty public FOK book has no normalized slice spine"
        )
    if (
        state["flatten_limit_price_e4"] % 100 != 0
        or any(price % 100 != 0 for price, _qty in levels)
    ):
        raise PostfillV41ContractError(
            "subcent tapered FOK book/limit is sensitivity-only and "
            "cannot enter candidate rows"
        )
    canonical_raw = {
        "market_ticker": row["market_ticker"],
        "market_id": row["market_id"],
        "recv_wall_ns": recv_wall,
        "recv_mono_ns": recv_mono,
        "ingest_sequence": sequence,
        "stable_source_id": stable,
        "levels": [
            {"price_e4": price, "qty_fp": qty}
            for price, qty in levels
        ],
    }
    supplied = _sha(row["source_rows_sha256"], "book source_rows_sha256")
    if supplied != public_book_source_sha256(canonical_raw):
        raise PostfillV41ContractError("public FOK book source hash mismatch")
    canonical_raw["source_rows_sha256"] = supplied
    return canonical_raw, levels


def _normalize_outcome(
    raw: Mapping[str, object],
    *,
    state: Mapping[str, object],
    episode: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    _only(raw, OUTCOME_FIELDS, "V4.1 FLATTEN_FOK outcome")
    _require(raw, OUTCOME_FIELDS, "V4.1 FLATTEN_FOK outcome")
    row = dict(raw)
    index = _integer(row["decision_index"], "decision_index", minimum=0)
    expected_action_id = _action_id(
        episode["postfill_episode_id"],
        index,
        "FLATTEN_FOK",
    )
    if (
        _day(row["source_date_utc"]) != episode["source_date_utc"]
        or row["postfill_episode_id"] != episode["postfill_episode_id"]
        or index != state["decision_index"]
        or row["postfill_action_id"] != expected_action_id
    ):
        raise PostfillV41ContractError("FOK outcome linkage mismatch")
    if (
        _boolean(row["data_invalid"], "data_invalid")
        or not _boolean(row["reconciliation_ok"], "reconciliation_ok")
    ):
        raise PostfillV41ContractError("invalid FOK outcome")
    if (
        row["fee_bound_selected_for_candidate"]
        != FEE_BOUND_SELECTED_FOR_CANDIDATE
        or _boolean(
            row["fee_accumulator_receipt_authenticated"],
            "fee_accumulator_receipt_authenticated",
        )
    ):
        raise PostfillV41ContractError(
            "DOC_LITERAL tight accumulator bound cannot be selected for "
            "candidate/live safety without authenticated recurrence"
        )
    registry_sha = _sha(
        row["simulated_strategy_order_registry_sha256"],
        "outcome registry hash",
    )
    if registry_sha != state["simulated_strategy_order_registry_sha256"]:
        raise PostfillV41ContractError("outcome registry drift")
    terminal_type = str(row["terminal_type"])
    if terminal_type not in FOK_TERMINALS:
        raise PostfillV41ContractError("unknown FOK terminal")
    planned = _integer(
        row["planned_effective_elapsed_ms"],
        "planned_effective_elapsed_ms",
        minimum=0,
    )
    if planned != state["decision_elapsed_ms"] + FOK_EFFECTIVE_LATENCY_MS:
        raise PostfillV41ContractError("FOK effective latency drift")
    effective_wall = (
        episode["first_fill_recv_wall_ns"] + planned * 1_000_000
    )
    effective_mono = (
        episode["first_fill_recv_mono_ns"] + planned * 1_000_000
    )
    slice_rows: list[dict[str, object]] = []
    if terminal_type == "CANCEL_RACE_PAIR":
        if not (
            episode["terminal_type"] == "COMPLEMENT_FILL"
            and Decimal(state["decision_elapsed_ms"])
            < episode["terminal_elapsed_ms"]
            < Decimal(planned)
            and row["pre_effective_public_book"] is None
            and row["race_complement_public_proxy_evidence_id"]
            == episode["complement_proxy_evidence_id"]
            and row["fok_sent"] is False
            and row["simulated_no_self_cross_verified_before_fok"] is None
        ):
            raise PostfillV41ContractError(
                "CANCEL_RACE_PAIR lacks traceable pre-cancel complement proxy"
            )
        for field in (
            "synthetic_cancel_applied_wall_ns",
            "synthetic_cancel_applied_mono_ns",
            "synthetic_cancel_applied_sequence",
            "synthetic_cancel_applied_stable_source_id",
            "fok_processed_wall_ns",
            "fok_processed_mono_ns",
            "fok_processed_sequence",
            "fok_processed_stable_source_id",
        ):
            if row[field] is not None:
                raise PostfillV41ContractError(
                    "race must suppress synthetic cancel and FOK"
                )
        execution: tuple[tuple[int, Decimal, Decimal], ...] = ()
        book = None
    else:
        if row["race_complement_public_proxy_evidence_id"] is not None:
            raise PostfillV41ContractError(
                "non-race outcome cannot carry race evidence"
            )
        if (
            row["fok_sent"] is not True
            or row["simulated_no_self_cross_verified_before_fok"] is not True
        ):
            raise PostfillV41ContractError("FOK send/self-cross gate failed")
        expected_protocol = (
            ("synthetic_cancel_applied_wall_ns", effective_wall),
            ("synthetic_cancel_applied_mono_ns", effective_mono),
            ("synthetic_cancel_applied_sequence", 0),
            ("fok_processed_wall_ns", effective_wall),
            ("fok_processed_mono_ns", effective_mono),
            ("fok_processed_sequence", 1),
        )
        for field, expected in expected_protocol:
            if row[field] != expected:
                raise PostfillV41ContractError(
                    "synthetic cancel seq0/FOK seq1 protocol mismatch"
                )
        for field in (
            "synthetic_cancel_applied_stable_source_id",
            "fok_processed_stable_source_id",
        ):
            _text(row[field], field)
        book, levels = _normalize_book(
            row["pre_effective_public_book"],
            episode=episode,
            state=state,
            effective_wall=effective_wall,
            effective_mono=effective_mono,
        )
        available = sum((qty for _price, qty in levels), Decimal("0"))
        if terminal_type == "FOK_FULL" and available < episode["first_fill_qty_fp"]:
            raise PostfillV41ContractError(
                "FOK_FULL cannot be self-reported without full public depth"
            )
        if terminal_type == "FOK_ZERO" and available >= episode["first_fill_qty_fp"]:
            raise PostfillV41ContractError(
                "FOK_ZERO contradicts canonical full public depth"
            )
        execution = _derive_execution(levels, episode["first_fill_qty_fp"])
        if terminal_type == "FOK_ZERO":
            execution = tuple(
                (price, source_qty, Decimal("0"))
                for price, source_qty in levels
            )
    expected = derive_reported_outcome_fields(
        episode=episode,
        state=state,
        terminal_type=terminal_type,
        pre_effective_book=book,
    )
    for field in OUTCOME_REPORTED_FIELDS:
        supplied = _decimal(row[field], field)
        if supplied != expected[field]:
            raise PostfillV41ContractError(
                f"{field} disagrees with V4.1 recomputation"
            )
        row[field] = supplied
    if terminal_type == "FOK_FULL":
        gross, fees = _execution_economics(
            execution,
            first_side=episode["first_fill_side"],
            first_price_e4=episode["first_fill_price_e4"],
            delta_qty=episode["delta_qty_fp"],
            account_rounding_target=episode[
                "account_rounding_target_usd"
            ],
            book_side=state["flatten_book_side"],
        )
        execution_hash = _canonical_sha256(
            [
                {"price_e4": price, "qty_fp": executed}
                for price, _source, executed in execution
                if executed > 0
            ]
        )
    else:
        gross = expected["reported_gross_pnl_usd"]
        fees = _fee_bound_for_levels(
            (),
            delta_qty=episode["delta_qty_fp"],
            account_rounding_target=episode[
                "account_rounding_target_usd"
            ],
            book_side=state["flatten_book_side"],
        )
        execution_hash = _canonical_sha256([])
    outcome_id = (
        f"{expected_action_id}::OUTCOME::{terminal_type}"
    )
    if book is not None:
        for slice_index, (price, source_qty, executed_qty) in enumerate(
            execution
        ):
            price_decimal = Decimal(price) / E4
            raw_fee = (
                FEE_RATE
                * executed_qty
                * price_decimal
                * (Decimal("1") - price_decimal)
            )
            slice_rows.append(
                {
                    "fok_slice_id": f"{outcome_id}::SLICE::{slice_index}",
                    "postfill_fok_outcome_id": outcome_id,
                    "contract_version": CONTRACT_VERSION,
                    "source_date_utc": episode["source_date_utc"],
                    "market_ticker": episode["market_ticker"],
                    "market_id": episode["market_id"],
                    "postfill_episode_id": episode["postfill_episode_id"],
                    "decision_index": index,
                    "slice_index": slice_index,
                    "fok_book_side": state["flatten_book_side"],
                    "fok_limit_price_e4": state[
                        "flatten_limit_price_e4"
                    ],
                    "source_price_e4": price,
                    "source_qty_fp": source_qty,
                    "executed_qty_fp": executed_qty,
                    "raw_trade_fee_usd": raw_fee,
                    "market_min_fill_increment_fp": episode["delta_qty_fp"],
                    "pre_effective_book_source_rows_sha256": book[
                        "source_rows_sha256"
                    ],
                    "execution_slices_sha256": execution_hash,
                }
            )
    q = episode["first_fill_qty_fp"]
    fok_qty = (
        q if terminal_type == "FOK_FULL" else Decimal("0")
    )
    complement_qty = (
        q if terminal_type == "CANCEL_RACE_PAIR" else Decimal("0")
    )
    residual = (
        q if terminal_type == "FOK_ZERO" else Decimal("0")
    )
    row.update(
        {
            "postfill_fok_outcome_id": outcome_id,
            "contract_version": CONTRACT_VERSION,
            "action_family_version": ACTION_FAMILY_VERSION,
            "source_date_utc": episode["source_date_utc"],
            "market_ticker": episode["market_ticker"],
            "market_id": episode["market_id"],
            "decision_index": index,
            "planned_effective_elapsed_ms": Decimal(planned),
            "terminal_elapsed_ms": (
                episode["terminal_elapsed_ms"]
                if terminal_type == "CANCEL_RACE_PAIR"
                else Decimal(planned)
            ),
            "settlement_release_elapsed_ms": episode[
                "settlement_release_elapsed_ms"
            ],
            "first_side": episode["first_fill_side"],
            "first_price_e4": episode["first_fill_price_e4"],
            "first_qty_fp": q,
            "first_maker_trade_fee_usd": Decimal("0"),
            "first_maker_net_fee_safe_upper_usd": episode[
                "first_maker_net_fee_safe_upper_usd"
            ],
            "first_leg_basis_safe_upper_usd": episode[
                "first_leg_basis_safe_upper_usd"
            ],
            "complement_reservation_safe_upper_usd": episode[
                "complement_reservation_safe_upper_usd"
            ],
            "locked_pair_capital_safe_upper_usd": episode[
                "locked_pair_capital_safe_upper_usd"
            ],
            "requested_qty_fp": q,
            "complement_fill_qty_fp": complement_qty,
            "fok_fill_qty_fp": fok_qty,
            "residual_inventory_fp": residual,
            "fok_book_side": state["flatten_book_side"],
            "fok_limit_price_e4": state["flatten_limit_price_e4"],
            "pre_effective_book_recv_wall_ns": (
                book["recv_wall_ns"] if book else None
            ),
            "pre_effective_book_recv_mono_ns": (
                book["recv_mono_ns"] if book else None
            ),
            "pre_effective_book_ingest_sequence": (
                book["ingest_sequence"] if book else None
            ),
            "pre_effective_book_stable_source_id": (
                book["stable_source_id"] if book else None
            ),
            "pre_effective_book_source_rows_sha256": (
                book["source_rows_sha256"] if book else None
            ),
            "execution_slices_sha256": execution_hash,
            "gross_pnl_usd": expected["reported_gross_pnl_usd"],
            "taker_fee_raw_total_usd": expected[
                "reported_taker_fee_raw_total_usd"
            ],
            "taker_fee_aggregate_centicent_lower_usd": expected[
                "reported_taker_fee_aggregate_centicent_lower_usd"
            ],
            "taker_fee_public_l2_level_scenario_usd": expected[
                "reported_taker_fee_public_l2_level_scenario_usd"
            ],
            "taker_fee_n_max": fees["n_max"],
            "taker_fee_trade_upper_before_account_rounding_usd": expected[
                "reported_taker_fee_trade_upper_before_account_rounding_usd"
            ],
            "taker_fee_account_rounding_safe_upper_usd": expected[
                "reported_taker_fee_account_rounding_safe_upper_usd"
            ],
            "taker_fee_partition_dp_no_rebate_upper_usd": expected[
                "reported_taker_fee_partition_dp_no_rebate_upper_usd"
            ],
            "taker_fee_sql_safe_upper_usd": expected[
                "reported_taker_fee_sql_safe_upper_usd"
            ],
            "taker_fee_safe_upper_usd": expected[
                "reported_taker_fee_safe_upper_usd"
            ],
            "taker_fee_doc_literal_tight_sensitivity_usd": expected[
                "reported_taker_fee_doc_literal_tight_sensitivity_usd"
            ],
            "fee_bound_selected_for_candidate": (
                FEE_BOUND_SELECTED_FOR_CANDIDATE
            ),
            "fee_accumulator_receipt_authenticated": False,
            "fee_schedule_provenance": FEE_SCHEDULE_PROVENANCE,
            "fee_schedule_source": FEE_SCHEDULE_SOURCE,
            "fee_partition_bound_kind": (
                "MIN_TRUE_FILL_PARTITION_NO_REBATE_SAFE"
            ),
            "market_min_fill_increment_fp": episode["delta_qty_fp"],
            "trade_fee_quantum_usd": TRADE_FEE_QUANTUM_USD,
            "account_rounding_target_usd": episode[
                "account_rounding_target_usd"
            ],
            "target_account_class": TARGET_ACCOUNT_CLASS,
            "account_precision_receipt_sha256": (
                TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
            ),
            "doc_literal_tight_sensitivity_kind": (
                DOC_LITERAL_TIGHT_SENSITIVITY
            ),
            "forward_actual_taker_fee_usd": None,
            "forward_actual_taker_fee_provenance": (
                FORWARD_ACTUAL_FEE_PROVENANCE
            ),
            "conservative_net_pnl_usd": expected[
                "reported_conservative_net_pnl_usd"
            ],
            "capital_dollar_seconds": expected[
                "reported_capital_dollar_seconds"
            ],
            "synthetic_cancel_effective_elapsed_ms": Decimal(planned),
            "fok_terminal_elapsed_ms": Decimal(planned),
            "fok_candidate_reserve_safe_upper_usd": state[
                "fok_candidate_reserve_safe_upper_usd"
            ],
            "peak_strategy_capital_safe_upper_usd": state[
                "peak_strategy_capital_safe_upper_usd"
            ],
            "capital_released_at_fok_terminal": (
                terminal_type != "FOK_ZERO"
            ),
            "market_metadata_receipt_id": episode["market_metadata"][
                "market_metadata_receipt_id"
            ],
            "fee_schedule_receipt_id": FEE_SCHEDULE_RECEIPT_ID,
            "public_settlement_receipt_id": episode[
                "public_settlement_receipt"
            ]["settlement_stable_source_id"],
            "public_settlement_recv_wall_ns": episode[
                "public_settlement_receipt"
            ]["settlement_recv_wall_ns"],
            "public_settlement_recv_mono_ns": episode[
                "public_settlement_receipt"
            ]["settlement_recv_mono_ns"],
            "public_settlement_status": episode[
                "public_settlement_receipt"
            ]["market_status"],
            "public_settlement_provenance": (
                SETTLEMENT_PROVENANCE
            ),
            "official_market_result": episode[
                "public_settlement_receipt"
            ]["official_market_result"],
            "public_settlement_source_rows_sha256": episode[
                "public_settlement_receipt"
            ]["source_rows_sha256"],
            "reconciliation_ok": True,
        }
    )
    for field in OUTCOME_REPORTED_FIELDS:
        del row[field]
    del row["pre_effective_public_book"]
    return row, slice_rows


def _capital_increment(
    *,
    start_ms: Decimal,
    stop_ms: Decimal,
    episode: Mapping[str, object],
) -> Decimal:
    locked = episode["locked_pair_capital_safe_upper_usd"]
    basis = episode["first_leg_basis_safe_upper_usd"]
    if episode["terminal_type"] != "HARD_FALLBACK":
        return locked * (stop_ms - start_ms) / Decimal("1000")
    release = episode["complement_release_elapsed_ms"]
    if stop_ms <= release:
        return locked * (stop_ms - start_ms) / Decimal("1000")
    if start_ms >= release:
        return basis * (stop_ms - start_ms) / Decimal("1000")
    return (
        locked * (release - start_ms)
        + basis * (stop_ms - release)
    ) / Decimal("1000")


def _build_keep_transitions(
    episode: Mapping[str, object],
    states: Mapping[int, Mapping[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    required = tuple(episode["required_grid"])
    terminal = episode["terminal_elapsed_ms"]
    for index, elapsed_int in required:
        start = Decimal(elapsed_int)
        next_index = index + 1
        next_elapsed = (
            Decimal(DECISION_GRID_MS[next_index])
            if next_index < len(DECISION_GRID_MS)
            else None
        )
        if next_elapsed is not None and next_elapsed < terminal:
            stop = next_elapsed
            transition_type = "NEXT_STATE"
            keep_terminal_type = None
            next_decision_id = _decision_id(
                episode["postfill_episode_id"],
                next_index,
            )
            gross = Decimal("0")
            fee_upper = Decimal("0")
            conservative = Decimal("0")
            terminal_evidence_id = None
        else:
            stop = terminal
            transition_type = "KEEP_TO_TERMINAL"
            keep_terminal_type = episode["terminal_type"]
            next_decision_id = None
            if keep_terminal_type == "COMPLEMENT_FILL":
                gross = (
                    Decimal(10_000)
                    - Decimal(episode["first_fill_price_e4"])
                    - Decimal(episode["complement_price_e4"])
                ) / E4 * episode["first_fill_qty_fp"]
                fee_upper = (
                    episode["first_maker_net_fee_safe_upper_usd"]
                    + episode[
                        "complement_maker_net_fee_safe_upper_usd"
                    ]
                )
                terminal_evidence_id = episode[
                    "complement_proxy_evidence_id"
                ]
            else:
                result = episode["public_settlement_receipt"][
                    "official_market_result"
                ]
                gross = (
                    (
                        Decimal(10_000 - episode["first_fill_price_e4"])
                        / E4
                    )
                    if result == episode["first_fill_side"]
                    else -Decimal(episode["first_fill_price_e4"]) / E4
                ) * episode["first_fill_qty_fp"]
                fee_upper = episode[
                    "first_maker_net_fee_safe_upper_usd"
                ]
                terminal_evidence_id = episode[
                    "public_settlement_receipt"
                ]["settlement_stable_source_id"]
            conservative = gross - fee_upper
        start_ns = _elapsed_ns(start)
        stop_ns = _elapsed_ns(stop)
        rows.append(
            {
                "postfill_keep_transition_id": (
                    f"{_action_id(episode['postfill_episode_id'], index, 'KEEP')}"
                    "::TRANSITION"
                ),
                "keep_action_id": _action_id(
                    episode["postfill_episode_id"],
                    index,
                    "KEEP",
                ),
                "from_decision_id": _decision_id(
                    episode["postfill_episode_id"],
                    index,
                ),
                "contract_version": CONTRACT_VERSION,
                "source_date_utc": episode["source_date_utc"],
                "market_ticker": episode["market_ticker"],
                "market_id": episode["market_id"],
                "postfill_episode_id": episode["postfill_episode_id"],
                "from_decision_index": index,
                "interval_start_elapsed_ms": start,
                "interval_stop_elapsed_ms": stop,
                "interval_start_wall_ns": (
                    episode["first_fill_recv_wall_ns"] + start_ns
                ),
                "interval_start_mono_ns": (
                    episode["first_fill_recv_mono_ns"] + start_ns
                ),
                "interval_stop_wall_ns": (
                    episode["first_fill_recv_wall_ns"] + stop_ns
                ),
                "interval_stop_mono_ns": (
                    episode["first_fill_recv_mono_ns"] + stop_ns
                ),
                "transition_type": transition_type,
                "keep_terminal_type": keep_terminal_type,
                "next_decision_id": next_decision_id,
                "terminal_public_evidence_id": terminal_evidence_id,
                "market_close_elapsed_ms": episode[
                    "market_close_elapsed_ms"
                ],
                "complement_release_elapsed_ms": episode[
                    "complement_release_elapsed_ms"
                ],
                "settlement_release_elapsed_ms": episode[
                    "settlement_release_elapsed_ms"
                ],
                "market_metadata_receipt_id": episode["market_metadata"][
                    "market_metadata_receipt_id"
                ],
                "fee_schedule_receipt_id": FEE_SCHEDULE_RECEIPT_ID,
                "public_settlement_receipt_id": episode[
                    "public_settlement_receipt"
                ]["settlement_stable_source_id"],
                "public_settlement_recv_wall_ns": episode[
                    "public_settlement_receipt"
                ]["settlement_recv_wall_ns"],
                "public_settlement_recv_mono_ns": episode[
                    "public_settlement_receipt"
                ]["settlement_recv_mono_ns"],
                "public_settlement_status": episode[
                    "public_settlement_receipt"
                ]["market_status"],
                "official_market_result": episode[
                    "public_settlement_receipt"
                ]["official_market_result"],
                "public_settlement_source_rows_sha256": episode[
                    "public_settlement_receipt"
                ]["source_rows_sha256"],
                "public_settlement_provenance": (
                    SETTLEMENT_PROVENANCE
                ),
                "first_side": episode["first_fill_side"],
                "first_price_e4": episode["first_fill_price_e4"],
                "first_qty_fp": episode["first_fill_qty_fp"],
                "first_leg_basis_safe_upper_usd": episode[
                    "first_leg_basis_safe_upper_usd"
                ],
                "complement_reservation_safe_upper_usd": episode[
                    "complement_reservation_safe_upper_usd"
                ],
                "locked_pair_capital_safe_upper_usd": episode[
                    "locked_pair_capital_safe_upper_usd"
                ],
                "immediate_gross_pnl_usd": gross,
                "maker_net_fee_safe_upper_usd": fee_upper,
                "conservative_net_pnl_usd": conservative,
                "fee_bound_selected_for_candidate": (
                    FEE_BOUND_SELECTED_FOR_CANDIDATE
                ),
                "capital_dollar_seconds_increment": _capital_increment(
                    start_ms=start,
                    stop_ms=stop,
                    episode=episode,
                ),
                "source_rows_sha256": (
                    states[next_index]["causal_source_rows_sha256"]
                    if transition_type == "NEXT_STATE"
                    else episode["source_rows_sha256"]
                ),
                "reconciliation_ok": True,
            }
        )
    return rows


def _rollback(days: Sequence[str], code: str, detail: str) -> None:
    raise MarketDayRollbackRequired(days, code, detail)


def validate_and_serialize_postfill_rows(
    *,
    logical_source_paths: Mapping[
        object, Sequence[os.PathLike[str] | str]
    ],
    expected_episode_ids_by_day: Mapping[object, Sequence[str]],
    episode_manifest: Iterable[Mapping[str, object]],
    compact_rows: Iterable[Mapping[str, object]],
    flatten_fok_outcomes: Iterable[Mapping[str, object]],
) -> PostfillV41DDLBatch:
    """Validate a whole discovery-day batch before returning any DDL row."""
    expected, guarded = preflight_canary_postfill_paths(
        logical_source_paths,
        expected_episode_ids_by_day,
    )
    days = tuple(sorted(expected))
    try:
        raw_episodes = tuple(episode_manifest)
        raw_states = tuple(compact_rows)
        raw_outcomes = tuple(flatten_fok_outcomes)
    except Exception as exc:
        _rollback(days, "INPUT_ITERATION_FAILED", str(exc))
    episodes: dict[str, dict[str, object]] = {}
    evidence_rows: list[dict[str, object]] = []
    public_trade_rows: list[dict[str, object]] = []
    metadata_by_id: dict[str, dict[str, object]] = {}
    settlement_by_id: dict[str, dict[str, object]] = {}
    zero_rows: list[dict[str, object]] = []
    try:
        for raw in raw_episodes:
            (
                episode,
                episode_evidence,
                episode_trade_rows,
                atom,
            ) = _normalize_episode(raw, days)
            episode_id = episode["postfill_episode_id"]
            if episode_id in episodes:
                raise PostfillV41ContractError("duplicate episode id")
            episodes[episode_id] = episode
            evidence_rows.extend(episode_evidence)
            public_trade_rows.extend(episode_trade_rows)
            metadata_row = _market_metadata_receipt_row(
                episode["market_metadata"]
            )
            metadata_id = str(
                metadata_row["market_metadata_receipt_id"]
            )
            prior_metadata = metadata_by_id.get(metadata_id)
            if prior_metadata is not None and prior_metadata != metadata_row:
                raise PostfillV41ContractError(
                    "market metadata receipt id reused with different facts"
                )
            metadata_by_id[metadata_id] = metadata_row
            settlement_row = _settlement_receipt_row(
                episode["public_settlement_receipt"]
            )
            settlement_id = str(
                settlement_row["settlement_receipt_id"]
            )
            prior_settlement = settlement_by_id.get(settlement_id)
            if (
                prior_settlement is not None
                and prior_settlement != settlement_row
            ):
                raise PostfillV41ContractError(
                    "settlement receipt id reused with different facts"
                )
            settlement_by_id[settlement_id] = settlement_row
            if atom is not None:
                zero_rows.append(atom)
        for day, ids in expected.items():
            actual = sorted(
                episode_id
                for episode_id, episode in episodes.items()
                if episode["source_date_utc"] == day
            )
            if actual != list(ids):
                raise PostfillV41ContractError(
                    f"episode roster mismatch expected={list(ids)} "
                    f"actual={actual}"
                )
        evidence_ids = [row["evidence_id"] for row in evidence_rows]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise PostfillV41ContractError("duplicate proxy evidence id")
        trade_ids = [
            (row["market_ticker"], row["trade_id"])
            for row in public_trade_rows
        ]
        if len(trade_ids) != len(set(trade_ids)):
            raise PostfillV41ContractError(
                "duplicate public trade id in V4.1 batch"
            )
    except (PostfillV41ContractError, ForbiddenSourceError) as exc:
        _rollback(days, "EPISODE_MANIFEST_INVALID", str(exc))
    paired: dict[
        tuple[str, int], dict[str, dict[str, object]]
    ] = {}
    try:
        for raw in raw_states:
            episode_id = str(raw.get("postfill_episode_id") or "")
            episode = episodes.get(episode_id)
            if episode is None or episode["zero_time_atom"]:
                raise PostfillV41ContractError(
                    "state has no continuous episode"
                )
            state = _normalize_state(raw, episode)
            key = (episode_id, state["decision_index"])
            by_action = paired.setdefault(key, {})
            if state["action_kind"] in by_action:
                raise PostfillV41ContractError("duplicate action state")
            by_action[state["action_kind"]] = state
    except PostfillV41ContractError as exc:
        _rollback(days, "COMPACT_ROW_INVALID", str(exc))
    state_rows: list[dict[str, object]] = []
    action_rows: list[dict[str, object]] = []
    keep_rows: list[dict[str, object]] = []
    canonical_states: dict[tuple[str, int], dict[str, object]] = {}
    try:
        for episode_id, episode in sorted(episodes.items()):
            if episode["zero_time_atom"]:
                continue
            states_by_index: dict[int, dict[str, object]] = {}
            expected_indices = [index for index, _ in episode["required_grid"]]
            actual_indices = sorted(
                index
                for candidate, index in paired
                if candidate == episode_id
            )
            if expected_indices != actual_indices:
                raise PostfillV41ContractError(
                    "decision-grid full coverage mismatch"
                )
            for index in expected_indices:
                pair = paired[(episode_id, index)]
                if set(pair) != ALLOWED_ACTIONS:
                    raise PostfillV41ContractError(
                        "KEEP/FLATTEN_FOK pair missing"
                    )
                keep = pair["KEEP"]
                flatten = pair["FLATTEN_FOK"]
                keep_fingerprint = _state_signature(keep)
                if keep_fingerprint != _state_signature(flatten):
                    raise PostfillV41ContractError("paired causal state drift")
                states_by_index[index] = keep
                canonical_states[(episode_id, index)] = keep
                state_rows.append(
                    _serialize_state(keep, episode, keep_fingerprint)
                )
                action_rows.extend(
                    (
                        _serialize_action(
                            keep,
                            episode,
                            keep_fingerprint,
                        ),
                        _serialize_action(
                            flatten,
                            episode,
                            keep_fingerprint,
                        ),
                    )
                )
            keep_rows.extend(_build_keep_transitions(episode, states_by_index))
    except PostfillV41ContractError as exc:
        _rollback(days, "FULL_COVERAGE_CONSERVATION_FAILED", str(exc))
    outcome_rows: list[dict[str, object]] = []
    slice_rows: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    try:
        for raw in raw_outcomes:
            episode_id = str(raw.get("postfill_episode_id") or "")
            index = raw.get("decision_index")
            if type(index) is not int:
                raise PostfillV41ContractError(
                    "outcome decision_index must be integer"
                )
            key = (episode_id, index)
            if key in seen:
                raise PostfillV41ContractError("duplicate FOK outcome")
            state = canonical_states.get(key)
            episode = episodes.get(episode_id)
            if state is None or episode is None:
                raise PostfillV41ContractError(
                    "FOK outcome has no causal state"
                )
            normalized, normalized_slices = _normalize_outcome(
                raw,
                state=state,
                episode=episode,
            )
            seen.add(key)
            outcome_rows.append(normalized)
            slice_rows.extend(normalized_slices)
        if seen != set(canonical_states):
            raise PostfillV41ContractError(
                "FOK full/zero/race outcome coverage mismatch"
            )
    except PostfillV41ContractError as exc:
        _rollback(days, "FLATTEN_FOK_OUTCOME_INVALID", str(exc))
    guarded_paths = tuple(
        sorted(
            os.fspath(path)
            for paths in guarded.values()
            for path in paths
        )
    )
    return PostfillV41DDLBatch(
        source_dates=days,
        guarded_source_paths=guarded_paths,
        episode_count=len(episodes),
        postfill_v41_causal_state=tuple(state_rows),
        postfill_v41_action=tuple(action_rows),
        postfill_v41_keep_transition=tuple(keep_rows),
        postfill_v41_flatten_fok_outcome=tuple(outcome_rows),
        postfill_v41_fok_slice=tuple(slice_rows),
        postfill_v41_public_proxy_evidence=tuple(evidence_rows),
        postfill_v41_public_trade_row=tuple(public_trade_rows),
        postfill_v41_market_metadata_receipt=tuple(
            metadata_by_id[key] for key in sorted(metadata_by_id)
        ),
        postfill_v41_settlement_receipt=tuple(
            settlement_by_id[key] for key in sorted(settlement_by_id)
        ),
        postfill_v41_fee_schedule_receipt=(
            _frozen_fee_schedule_receipt(),
        ),
        postfill_v41_zero_time_atom=tuple(zero_rows),
    )


def create_postfill_v41_schema(
    connection: object,
    ddl_path: os.PathLike[str] | str = DEFAULT_DDL_PATH,
) -> Path:
    path = Path(ddl_path)
    connection.execute(path.read_text())
    return path
