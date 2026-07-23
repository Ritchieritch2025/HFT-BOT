#!/usr/bin/env python3
"""Derive a redacted account-balance precision receipt from actual fills.

The input pair is deliberately private and read-only:

* an authenticated ``GET /portfolio/fills`` receipt whose identifiers were
  already replaced by SHA-256 references; and
* the production portfolio monitor state containing the corresponding order
  action and outcome side.

The output contains no order, fill, market, event, or series identifier.  It
only proves which documented balance precision is consistent with the exact
signed cash change on each joined actual fill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Sequence

from .provenance import atomic_write_receipt


SCHEMA_VERSION = "pnl-spine-account-precision-evidence-v1"
PRIVATE_SCHEMA = "pnl-spine-private-fee-truth-v1"
CENTICENT_E6 = 100
CENT_E6 = 10_000
MAX_INPUT_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AccountPrecisionError(ValueError):
    """Private evidence is malformed, unbound, or inconclusive."""


def _safe_file(path: Path, expected_sha256: str) -> bytes:
    if not path.is_absolute():
        raise AccountPrecisionError("input paths must be absolute")
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise AccountPrecisionError("expected SHA-256 is malformed")
    cursor = Path(path.parts[0])
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise AccountPrecisionError(f"input is unavailable: {path}") from exc
        if stat.S_ISLNK(mode):
            raise AccountPrecisionError(f"input contains a symlink: {path}")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise AccountPrecisionError(f"input is not a regular file: {path}")
    if metadata.st_size > MAX_INPUT_BYTES:
        raise AccountPrecisionError("input exceeds the bounded read limit")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AccountPrecisionError(f"input SHA-256 mismatch: {path}")
    return raw


def _no_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AccountPrecisionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(raw: bytes, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                AccountPrecisionError(
                    f"{name} contains non-finite number {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountPrecisionError(f"{name} is not strict JSON") from exc
    if not isinstance(value, Mapping):
        raise AccountPrecisionError(f"{name} must contain an object")
    return value


def _plain_int(name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AccountPrecisionError(
            f"{name} must be a plain integer >= {minimum}"
        )
    return value


def derive_account_precision(
    *,
    private_fill_receipt: Path,
    private_fill_receipt_sha256: str,
    production_monitor_state: Path,
    production_monitor_state_sha256: str,
) -> dict[str, Any]:
    """Return a row-free, identifier-free empirical precision receipt."""

    private_raw = _safe_file(
        private_fill_receipt, private_fill_receipt_sha256
    )
    monitor_raw = _safe_file(
        production_monitor_state, production_monitor_state_sha256
    )
    private = _load(private_raw, "private fill receipt")
    monitor = _load(monitor_raw, "production monitor state")
    if (
        private.get("schema_version") != PRIVATE_SCHEMA
        or private.get("state") != "PASS"
        or private.get("capture_method") != "AUTHENTICATED_GET_ONLY"
        or private.get("credential_material_serialized") is not False
    ):
        raise AccountPrecisionError(
            "private fill receipt is not an authenticated redacted PASS"
        )
    fills = private.get("fills")
    orders = monitor.get("orders")
    if not isinstance(fills, list) or not isinstance(orders, Mapping):
        raise AccountPrecisionError("fill/order collections are malformed")
    if private.get("fill_count") != len(fills):
        raise AccountPrecisionError("private fill_count contradicts rows")

    order_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw_order in orders.values():
        if not isinstance(raw_order, Mapping):
            raise AccountPrecisionError("monitor order is malformed")
        order_id = raw_order.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise AccountPrecisionError("monitor order_id is missing")
        order_ref = hashlib.sha256(order_id.encode("utf-8")).hexdigest()
        if order_ref in order_by_ref:
            raise AccountPrecisionError("duplicate monitor order reference")
        order_by_ref[order_ref] = raw_order

    matched = 0
    centicent_aligned = 0
    cent_aligned = 0
    buy_count = 0
    sell_count = 0
    maker_count = 0
    taker_count = 0
    seen_fill_refs: set[str] = set()
    for raw_fill in fills:
        if not isinstance(raw_fill, Mapping):
            raise AccountPrecisionError("private fill row is malformed")
        fill_ref = raw_fill.get("fill_ref")
        order_ref = raw_fill.get("order_ref")
        if (
            not isinstance(fill_ref, str)
            or SHA256_RE.fullmatch(fill_ref) is None
            or fill_ref in seen_fill_refs
        ):
            raise AccountPrecisionError(
                "private fill reference is malformed or duplicate"
            )
        seen_fill_refs.add(fill_ref)
        if not isinstance(order_ref, str):
            raise AccountPrecisionError("private order reference is malformed")
        order = order_by_ref.get(order_ref)
        if order is None:
            continue

        action = order.get("action")
        outcome = order.get("outcome_side")
        if action not in {"buy", "sell"} or outcome not in {"yes", "no"}:
            raise AccountPrecisionError(
                "joined order lacks canonical action/outcome"
            )
        price_field = "yes_price_e4" if outcome == "yes" else "no_price_e4"
        price_e4 = _plain_int(price_field, raw_fill.get(price_field))
        quantity_e2 = _plain_int(
            "quantity_e2", raw_fill.get("quantity_e2"), minimum=1
        )
        fee_cost_e6 = _plain_int(
            "fee_cost_e6", raw_fill.get("fee_cost_e6")
        )
        if price_e4 > 10_000:
            raise AccountPrecisionError("fill price exceeds one dollar")
        # (price / 1e4) * (quantity / 1e2) * 1e6 = price * quantity.
        notional_e6 = price_e4 * quantity_e2
        signed_balance_change_e6 = (
            -(notional_e6 + fee_cost_e6)
            if action == "buy"
            else notional_e6 - fee_cost_e6
        )
        matched += 1
        buy_count += action == "buy"
        sell_count += action == "sell"
        is_taker = raw_fill.get("is_taker")
        if type(is_taker) is not bool:
            raise AccountPrecisionError("is_taker must be boolean")
        taker_count += is_taker
        maker_count += not is_taker
        centicent_aligned += signed_balance_change_e6 % CENTICENT_E6 == 0
        cent_aligned += signed_balance_change_e6 % CENT_E6 == 0

    if matched == 0:
        raise AccountPrecisionError("no actual fills joined to canonical orders")
    direct_confirmed = (
        centicent_aligned == matched and cent_aligned < matched
    )
    state = "PASS" if direct_confirmed else "INCONCLUSIVE"
    precision = "DIRECT_CENTICENT" if direct_confirmed else None
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "claim_tier": "EMPIRICAL_ACCOUNT_BALANCE_PRECISION",
        "account_balance_precision": precision,
        "account_class_interpretation": (
            "DIRECT_MEMBER_PER_OFFICIAL_FEE_ROUNDING_TERMINOLOGY"
            if direct_confirmed
            else None
        ),
        "method": (
            "SHA256_JOIN_REDACTED_AUTHENTICATED_FILL_TO_CANONICAL_ORDER;"
            "SIGNED_PRINCIPAL_MINUS_ACTUAL_FEE_COST_BALANCE_ALIGNMENT"
        ),
        "official_rule_url": (
            "https://docs.kalshi.com/getting_started/fee_rounding"
        ),
        "source_bindings": {
            "private_fill_receipt_sha256": private_fill_receipt_sha256,
            "private_fill_content_sha256": private.get("content_sha256"),
            "production_monitor_state_sha256": (
                production_monitor_state_sha256
            ),
        },
        "private_fill_count": len(fills),
        "monitor_order_count": len(order_by_ref),
        "matched_actual_fill_count": matched,
        "buy_fill_count": buy_count,
        "sell_fill_count": sell_count,
        "maker_fill_count": maker_count,
        "taker_fill_count": taker_count,
        "centicent_aligned_fill_count": centicent_aligned,
        "cent_aligned_fill_count": cent_aligned,
        "cent_alignment_disproving_fill_count": matched - cent_aligned,
        "identifiers_serialized": False,
        "credentials_serialized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="derive redacted actual-fill account precision evidence"
    )
    parser.add_argument("--private-fill-receipt", required=True)
    parser.add_argument("--private-fill-receipt-sha256", required=True)
    parser.add_argument("--production-monitor-state", required=True)
    parser.add_argument("--production-monitor-state-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = derive_account_precision(
            private_fill_receipt=Path(args.private_fill_receipt),
            private_fill_receipt_sha256=args.private_fill_receipt_sha256,
            production_monitor_state=Path(args.production_monitor_state),
            production_monitor_state_sha256=(
                args.production_monitor_state_sha256
            ),
        )
        atomic_write_receipt(Path(args.output), receipt)
    except (AccountPrecisionError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if receipt["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AccountPrecisionError",
    "SCHEMA_VERSION",
    "derive_account_precision",
]
