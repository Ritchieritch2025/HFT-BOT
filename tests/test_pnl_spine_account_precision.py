from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_TOOLS = ROOT / "tools" / "research"
if str(RESEARCH_TOOLS) not in sys.path:
    sys.path.insert(0, str(RESEARCH_TOOLS))

from pnl_spine.account_precision import (  # noqa: E402
    AccountPrecisionError,
    derive_account_precision,
)


def _write(path: Path, value: object) -> str:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    tmp_path: Path,
    *,
    discriminating: bool = True,
    include_observed_balance: bool = True,
    include_direct_account_field: bool = False,
):
    order_1 = "order-1"
    order_2 = "order-2"
    first = {
        "fill_ref": "1" * 64,
        "order_ref": hashlib.sha256(order_1.encode()).hexdigest(),
        "yes_price_e4": 4_000,
        "no_price_e4": 6_000,
        "quantity_e2": 100,
        "fee_cost_e6": 200 if discriminating else 0,
        "is_taker": True,
    }
    second = {
        "fill_ref": "2" * 64,
        "order_ref": hashlib.sha256(order_2.encode()).hexdigest(),
        "yes_price_e4": 4_000,
        "no_price_e4": 6_000,
        "quantity_e2": 100,
        "fee_cost_e6": 300 if discriminating else 0,
        "is_taker": False,
    }
    if include_observed_balance:
        first["observed_balance_change_e6"] = (
            -400_200 if discriminating else -400_000
        )
        second["observed_balance_change_e6"] = (
            599_700 if discriminating else 600_000
        )
    private = {
        "schema_version": "pnl-spine-private-fee-truth-v1",
        "state": "PASS",
        "capture_method": "AUTHENTICATED_GET_ONLY",
        "credential_material_serialized": False,
        "content_sha256": "a" * 64,
        "fill_count": 2,
        "fills": [first, second],
    }
    monitor = {
        "orders": {
            order_1: {
                "order_id": order_1,
                "action": "buy",
                "outcome_side": "yes",
            },
            order_2: {
                "order_id": order_2,
                "action": "sell",
                "outcome_side": "no",
            },
        }
    }
    if include_direct_account_field:
        monitor["orders"][order_1]["subaccount_number"] = 0
        monitor["orders"][order_2]["subaccount_number"] = 0
    private_path = tmp_path / "private.json"
    monitor_path = tmp_path / "monitor.json"
    return (
        private_path,
        _write(private_path, private),
        monitor_path,
        _write(monitor_path, monitor),
    )


def test_actual_signed_cash_alignment_proves_centicent_precision(
    tmp_path: Path,
):
    private, private_sha, monitor, monitor_sha = _fixture(tmp_path)
    receipt = derive_account_precision(
        private_fill_receipt=private,
        private_fill_receipt_sha256=private_sha,
        production_monitor_state=monitor,
        production_monitor_state_sha256=monitor_sha,
    )
    assert receipt["state"] == "PASS"
    assert receipt["account_balance_precision"] == "DIRECT_CENTICENT"
    assert receipt["matched_actual_fill_count"] == 2
    assert receipt["observed_centicent_aligned_fill_count"] == 2
    assert receipt["observed_cent_aligned_fill_count"] == 0
    assert receipt["identifiers_serialized"] is False


def test_cent_aligned_sample_is_inconclusive_not_guessed(tmp_path: Path):
    private, private_sha, monitor, monitor_sha = _fixture(
        tmp_path, discriminating=False
    )
    receipt = derive_account_precision(
        private_fill_receipt=private,
        private_fill_receipt_sha256=private_sha,
        production_monitor_state=monitor,
        production_monitor_state_sha256=monitor_sha,
    )
    assert receipt["state"] == "INCONCLUSIVE"
    assert receipt["account_balance_precision"] is None


def test_fee_principal_arithmetic_without_posted_balance_is_inconclusive(
    tmp_path: Path,
):
    private, private_sha, monitor, monitor_sha = _fixture(
        tmp_path, include_observed_balance=False
    )
    receipt = derive_account_precision(
        private_fill_receipt=private,
        private_fill_receipt_sha256=private_sha,
        production_monitor_state=monitor,
        production_monitor_state_sha256=monitor_sha,
    )
    assert receipt["state"] == "INCONCLUSIVE"
    assert receipt["observed_balance_change_fill_count"] == 0
    assert receipt["derived_centicent_aligned_fill_count"] == 2
    assert receipt["precision_blocker"].startswith(
        "MISSING_ACTUAL_POSTED_BALANCE_DELTA"
    )


def test_official_direct_order_field_proves_direct_precision(
    tmp_path: Path,
):
    private, private_sha, monitor, monitor_sha = _fixture(
        tmp_path,
        include_observed_balance=False,
        include_direct_account_field=True,
    )
    receipt = derive_account_precision(
        private_fill_receipt=private,
        private_fill_receipt_sha256=private_sha,
        production_monitor_state=monitor,
        production_monitor_state_sha256=monitor_sha,
    )
    assert receipt["state"] == "PASS"
    assert receipt["account_balance_precision"] == "DIRECT_CENTICENT"
    assert receipt["account_class_authority"] == (
        "AUTHENTICATED_ORDER_SUBACCOUNT_FIELD_OFFICIAL_CONTRACT"
    )
    assert receipt["valid_direct_subaccount_field_order_count"] == 2
    assert receipt["observed_balance_change_fill_count"] == 0


def test_sha_drift_and_symlink_fail_closed(tmp_path: Path):
    private, private_sha, monitor, monitor_sha = _fixture(tmp_path)
    with pytest.raises(AccountPrecisionError, match="SHA-256 mismatch"):
        derive_account_precision(
            private_fill_receipt=private,
            private_fill_receipt_sha256="0" * 64,
            production_monitor_state=monitor,
            production_monitor_state_sha256=monitor_sha,
        )

    link = tmp_path / "private-link.json"
    link.symlink_to(private)
    with pytest.raises(AccountPrecisionError, match="symlink"):
        derive_account_precision(
            private_fill_receipt=link,
            private_fill_receipt_sha256=private_sha,
            production_monitor_state=monitor,
            production_monitor_state_sha256=monitor_sha,
        )
