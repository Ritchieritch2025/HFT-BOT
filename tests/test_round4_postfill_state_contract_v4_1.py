from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path

import duckdb
import pytest

from tools.research.crypto_mm import (
    round4_postfill_state_contract_v4_1 as C,
)
from tools.research.crypto_mm.round4_table_builder import (
    EXPERIMENT_ID,
    Round4ContractError,
    insert_postfill_v41_tables,
)


DAY = "2026-07-20"
BASE_WALL_NS = 1_784_548_800_000_000_000
BASE_MONO_NS = 8_000_000_000_000_000
MARKET_CLOSE_MS = Decimal("120000.5")
SETTLEMENT_MS = Decimal("120500.5")
ROOT = Path(__file__).resolve().parents[1]
DDL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract_v4_1.sql"
)
VALIDATOR_SQL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_v4_1_validator.sql"
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def legal_tick_below(price_e4: int) -> int:
    candidate = price_e4 - 1
    while not C.is_legal_tapered_price(candidate):
        candidate -= 1
    return candidate


def legal_tick_above(price_e4: int) -> int:
    candidate = price_e4 + 1
    while not C.is_legal_tapered_price(candidate):
        candidate += 1
    return candidate


def make_metadata(ticker: str, market_id: str) -> dict[str, object]:
    row: dict[str, object] = {
        "market_ticker": ticker,
        "market_id": market_id,
        "source_date_utc": DAY,
        "market_window_open_wall_ns": BASE_WALL_NS - 60_000_000_000,
        "market_window_close_wall_ns": (
            BASE_WALL_NS + int(MARKET_CLOSE_MS * Decimal("1000000"))
        ),
        "price_level_structure": C.PRICE_LEVEL_STRUCTURE,
        "market_min_fill_increment_fp": Decimal("0.01"),
        "market_fill_increment_guarantees_true_fill_min": True,
        "market_metadata_stable_source_id": f"{market_id}-metadata",
        "provenance": C.PUBLIC_MARKET_METADATA_PROVENANCE,
    }
    row["source_rows_sha256"] = C.market_metadata_sha256(row)
    return row


def make_settlement(
    ticker: str,
    market_id: str,
    *,
    result: str = "NO",
) -> dict[str, object]:
    recv_wall = BASE_WALL_NS + int(
        SETTLEMENT_MS * Decimal("1000000")
    )
    recv_mono = BASE_MONO_NS + int(
        SETTLEMENT_MS * Decimal("1000000")
    )
    row: dict[str, object] = {
        "market_ticker": ticker,
        "market_id": market_id,
        "official_market_result": result,
        "market_status": "FINALIZED",
        "settlement_recv_wall_ns": recv_wall,
        "settlement_recv_mono_ns": recv_mono,
        "settlement_ingest_sequence": 10_000,
        "settlement_stable_source_id": f"{market_id}-finalized",
        "determined_wall_ns": recv_wall - 200_000_000,
        "finalized_wall_ns": recv_wall - 100_000_000,
        "provenance": C.PUBLIC_SETTLEMENT_RECEIPT_PROVENANCE,
    }
    row["source_rows_sha256"] = C.settlement_receipt_sha256(row)
    return row


def make_proxy(
    *,
    evidence_id: str,
    proxy_kind: str,
    ticker: str,
    market_id: str,
    outcome_side: str,
    order_price_e4: int,
    qty: Decimal,
    recv_wall_ns: int,
    recv_mono_ns: int,
    ingest_sequence: int,
    stable_source_id: str,
) -> dict[str, object]:
    maker_yes_price_e4 = (
        order_price_e4
        if outcome_side == "YES"
        else 10_000 - order_price_e4
    )
    maker_yes_book_side = "BID" if outcome_side == "YES" else "ASK"
    taker_outcome_side = "NO" if outcome_side == "YES" else "YES"
    taker_book_side = "ASK" if outcome_side == "YES" else "BID"
    trigger_yes_price_e4 = (
        legal_tick_below(maker_yes_price_e4)
        if outcome_side == "YES"
        else legal_tick_above(maker_yes_price_e4)
    )
    trade: dict[str, object] = {
        "trade_id": stable_source_id,
        "ticker": ticker,
        "taker_book_side": taker_book_side,
        "taker_outcome_side": taker_outcome_side,
        "yes_price_e4": trigger_yes_price_e4,
        "count_fp": qty,
        "is_block_trade": False,
        "recv_wall_ns": recv_wall_ns,
        "recv_mono_ns": recv_mono_ns,
        "ingest_sequence": ingest_sequence,
        "stable_source_id": stable_source_id,
    }
    trade["source_rows_sha256"] = C.public_trade_row_sha256(trade)
    public_trade_rows = [trade]
    row: dict[str, object] = {
        "evidence_id": evidence_id,
        "proxy_kind": proxy_kind,
        "market_ticker": ticker,
        "market_id": market_id,
        "maker_order_yes_book_side": maker_yes_book_side,
        "maker_order_outcome_side": outcome_side,
        "maker_order_outcome_price_e4": order_price_e4,
        "maker_order_yes_price_e4": maker_yes_price_e4,
        "maker_order_qty_fp": qty,
        "trigger_public_trade_row_id": f"{evidence_id}::TRADE::0",
        "derived_cumulative_strict_through_qty_fp": qty,
        "public_trade_spine_sha256": C.public_trade_spine_sha256(
            public_trade_rows
        ),
        "recv_wall_ns": recv_wall_ns,
        "recv_mono_ns": recv_mono_ns,
        "ingest_sequence": ingest_sequence,
        "stable_source_id": stable_source_id,
        "public_trade_rows": public_trade_rows,
    }
    row["source_rows_sha256"] = C.public_proxy_evidence_sha256(row)
    return row


def correct_public_taker_semantics(
    proxy: dict[str, object],
) -> None:
    maker_outcome = str(proxy["maker_order_outcome_side"])
    outcome_price = int(proxy["maker_order_outcome_price_e4"])
    maker_yes_price = (
        outcome_price
        if maker_outcome == "YES"
        else 10_000 - outcome_price
    )
    proxy["maker_order_yes_book_side"] = (
        "BID" if maker_outcome == "YES" else "ASK"
    )
    proxy["maker_order_yes_price_e4"] = maker_yes_price
    trade = proxy["public_trade_rows"][-1]
    trade["taker_book_side"] = (
        "ASK" if maker_outcome == "YES" else "BID"
    )
    trade["taker_outcome_side"] = (
        "NO" if maker_outcome == "YES" else "YES"
    )
    trade["yes_price_e4"] = (
        legal_tick_below(maker_yes_price)
        if maker_outcome == "YES"
        else legal_tick_above(maker_yes_price)
    )
    trade["source_rows_sha256"] = C.public_trade_row_sha256(trade)
    proxy["public_trade_spine_sha256"] = C.public_trade_spine_sha256(
        proxy["public_trade_rows"]
    )
    proxy["source_rows_sha256"] = C.public_proxy_evidence_sha256(proxy)


def make_episode(
    episode_id: str = "ep",
    *,
    terminal_ms: Decimal | int = Decimal("400"),
    terminal_type: str = "COMPLEMENT_FILL",
    first_side: str = "YES",
    first_price_e4: int | None = None,
    complement_price_e4: int | None = None,
    zero: bool = False,
) -> dict[str, object]:
    first_price = (
        first_price_e4
        if first_price_e4 is not None
        else (3_000 if first_side == "YES" else 6_900)
    )
    complement_price = (
        complement_price_e4
        if complement_price_e4 is not None
        else (6_900 if first_side == "YES" else 3_000)
    )
    ticker = f"KXBTC15M-26JUL20-{episode_id.upper()}"
    market_id = f"market-{episode_id}"
    quantity = Decimal("2")
    first_stable = f"{episode_id}-first"
    first_sequence = 10
    first_proxy = make_proxy(
        evidence_id=f"{episode_id}-first-evidence",
        proxy_kind="FIRST_FILL",
        ticker=ticker,
        market_id=market_id,
        outcome_side=first_side,
        order_price_e4=first_price,
        qty=quantity,
        recv_wall_ns=BASE_WALL_NS,
        recv_mono_ns=BASE_MONO_NS,
        ingest_sequence=first_sequence,
        stable_source_id=first_stable,
    )
    terminal = Decimal(terminal_ms)
    if terminal_type == "HARD_FALLBACK":
        terminal = SETTLEMENT_MS
    if zero:
        terminal = Decimal("0")
        terminal_type = C.ZERO_TIME_TERMINAL
    terminal_ns = int(terminal * Decimal("1000000"))
    row: dict[str, object] = {
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "source_date_utc": DAY,
        "market_ticker": ticker,
        "market_id": market_id,
        "market_metadata": make_metadata(ticker, market_id),
        "postfill_episode_id": episode_id,
        "entry_episode_id": f"entry-{episode_id}",
        "entry_action_id": "ENTRY_PAIR_99",
        "source_rows_sha256": digest(f"episode-{episode_id}"),
        "data_origin": C.DATA_ORIGIN,
        "first_fill_provenance": C.FILL_PROVENANCE,
        "first_fill_execution_nature": C.EXECUTION_NATURE,
        "first_fill_trade_fee_provenance": (
            C.MAKER_TRADE_FEE_PROVENANCE
        ),
        "first_fill_side": first_side,
        "first_fill_price_e4": first_price,
        "first_fill_qty_fp": quantity,
        "first_fill_trade_fee_usd": Decimal("0"),
        "first_fill_recv_wall_ns": BASE_WALL_NS,
        "first_fill_recv_mono_ns": BASE_MONO_NS,
        "first_fill_ingest_sequence": first_sequence,
        "first_fill_stable_source_id": first_stable,
        "first_fill_public_proxy_evidence": first_proxy,
        "first_fill_elapsed_ms": Decimal("125"),
        "first_fill_tte_ms": 120_000,
        "market_close_elapsed_ms": MARKET_CLOSE_MS,
        "complement_order_id": f"order-{episode_id}",
        "complement_price_e4": complement_price,
        "complement_order_age_at_first_fill_ms": 100,
        "public_settlement_receipt": make_settlement(
            ticker,
            market_id,
        ),
        "terminal_elapsed_ms": terminal,
        "terminal_wall_ns": BASE_WALL_NS + terminal_ns,
        "terminal_mono_ns": BASE_MONO_NS + terminal_ns,
        "terminal_type": terminal_type,
        "zero_time_atom": zero,
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
        "account_rounding_target_usd": Decimal("0.0001"),
        "account_rounding_target_provenance": (
            "TARGET_DIRECT_ACCOUNT_PROBE"
        ),
        "account_precision_authenticated": True,
        "target_account_class": C.TARGET_ACCOUNT_CLASS,
        "account_precision_receipt_path": (
            C.TARGET_ACCOUNT_PRECISION_RECEIPT_PATH
        ),
        "account_precision_receipt_sha256": (
            C.TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
        ),
        "fee_schedule_provenance": C.FEE_SCHEDULE_PROVENANCE,
        "fee_schedule_source": C.FEE_SCHEDULE_SOURCE,
    }
    complement_side = "NO" if first_side == "YES" else "YES"
    if zero:
        complement_sequence = first_sequence + 1
        complement_stable = f"{episode_id}-complement"
        row.update(
            {
                "receipt_envelope_id": f"envelope-{episode_id}",
                "complement_side": complement_side,
                "complement_fill_price_e4": complement_price,
                "complement_fill_qty_fp": quantity,
                "complement_fill_trade_fee_usd": Decimal("0"),
                "complement_fill_provenance": C.FILL_PROVENANCE,
                "complement_execution_nature": C.EXECUTION_NATURE,
                "complement_trade_fee_provenance": (
                    C.MAKER_TRADE_FEE_PROVENANCE
                ),
                "complement_fill_stable_source_id": complement_stable,
                "complement_fill_ingest_sequence": complement_sequence,
                "complement_public_proxy_evidence": make_proxy(
                    evidence_id=f"{episode_id}-complement-evidence",
                    proxy_kind="COMPLEMENT_FILL",
                    ticker=ticker,
                    market_id=market_id,
                    outcome_side=complement_side,
                    order_price_e4=complement_price,
                    qty=quantity,
                    recv_wall_ns=BASE_WALL_NS,
                    recv_mono_ns=BASE_MONO_NS,
                    ingest_sequence=complement_sequence,
                    stable_source_id=complement_stable,
                ),
            }
        )
    elif terminal_type == "COMPLEMENT_FILL":
        complement_sequence = 20
        complement_stable = f"{episode_id}-complement"
        row.update(
            {
                "terminal_complement_fill_price_e4": complement_price,
                "terminal_complement_fill_qty_fp": quantity,
                "terminal_complement_trade_fee_usd": Decimal("0"),
                "terminal_complement_fill_provenance": C.FILL_PROVENANCE,
                "terminal_complement_execution_nature": C.EXECUTION_NATURE,
                "terminal_complement_trade_fee_provenance": (
                    C.MAKER_TRADE_FEE_PROVENANCE
                ),
                "terminal_complement_fill_stable_source_id": (
                    complement_stable
                ),
                "terminal_complement_fill_ingest_sequence": (
                    complement_sequence
                ),
                "terminal_complement_public_proxy_evidence": make_proxy(
                    evidence_id=f"{episode_id}-complement-evidence",
                    proxy_kind="COMPLEMENT_FILL",
                    ticker=ticker,
                    market_id=market_id,
                    outcome_side=complement_side,
                    order_price_e4=complement_price,
                    qty=quantity,
                    recv_wall_ns=BASE_WALL_NS + terminal_ns,
                    recv_mono_ns=BASE_MONO_NS + terminal_ns,
                    ingest_sequence=complement_sequence,
                    stable_source_id=complement_stable,
                ),
            }
        )
    elif terminal_type == "HARD_FALLBACK":
        release = Decimal("120100.5")
        release_ns = int(release * Decimal("1000000"))
        release_id = f"{episode_id}-synthetic-expiry"
        release_payload = {
            "market_ticker": ticker,
            "market_id": market_id,
            "release_elapsed_ms": release,
            "release_wall_ns": BASE_WALL_NS + release_ns,
            "release_mono_ns": BASE_MONO_NS + release_ns,
            "release_event_id": release_id,
            "provenance": C.ORDER_RELEASE_PROVENANCE,
        }
        row.update(
            {
                "complement_release_elapsed_ms": release,
                "complement_release_wall_ns": BASE_WALL_NS + release_ns,
                "complement_release_mono_ns": BASE_MONO_NS + release_ns,
                "complement_release_event_id": release_id,
                "complement_release_source_rows_sha256": (
                    C._canonical_sha256(release_payload)
                ),
                "complement_release_provenance": (
                    C.ORDER_RELEASE_PROVENANCE
                ),
            }
        )
    return row


def default_visible(first_side: str) -> list[dict[str, object]]:
    if first_side == "YES":
        return [
            {"price_e4": 3_100, "qty_fp": Decimal("1")},
            {"price_e4": 3_000, "qty_fp": Decimal("1")},
        ]
    return [
        {"price_e4": 6_900, "qty_fp": Decimal("1")},
        {"price_e4": 7_000, "qty_fp": Decimal("1")},
    ]


def make_compact_pair(
    spec: dict[str, object],
    decision_index: int,
    *,
    visible: list[dict[str, object]] | None = None,
    available_before: Decimal = Decimal("10"),
) -> list[dict[str, object]]:
    elapsed = C.DECISION_GRID_MS[decision_index]
    wall = BASE_WALL_NS + elapsed * 1_000_000
    mono = BASE_MONO_NS + elapsed * 1_000_000
    visible_rows = deepcopy(
        default_visible(str(spec["first_fill_side"]))
        if visible is None
        else visible
    )
    side = C._canonical_flatten_book_side(str(spec["first_fill_side"]))
    normalized = tuple(
        (int(level["price_e4"]), Decimal(level["qty_fp"]))
        for level in visible_rows
    )
    visible_qty = sum((qty for _price, qty in normalized), Decimal("0"))
    worst = (
        normalized[-1][0]
        if visible_qty >= Decimal(spec["first_fill_qty_fp"])
        else None
    )
    limit, _tick, fallback = C._adverse_limit(side, worst)
    common: dict[str, object] = {
        "source_date_utc": DAY,
        "postfill_episode_id": spec["postfill_episode_id"],
        "decision_index": decision_index,
        "decision_elapsed_ms": elapsed,
        "action_family_version": C.ACTION_FAMILY_VERSION,
        "causal_source_rows_sha256": (
            spec["first_fill_public_proxy_evidence"]["source_rows_sha256"]
            if decision_index == 0
            else digest(
                f"{spec['postfill_episode_id']}-{decision_index}-state"
            )
        ),
        "source_max_recv_wall_ns": wall,
        "source_max_recv_mono_ns": mono,
        "source_max_ingest_sequence": 10 + decision_index,
        "source_max_stable_id": (
            spec["first_fill_stable_source_id"]
            if decision_index == 0
            else f"{spec['postfill_episode_id']}-{decision_index}-source"
        ),
        "decision_recv_wall_ns": wall,
        "decision_recv_mono_ns": mono,
        "simulated_cancel_state": "NONE",
        "simulated_available_cash_before_cancel_usd": available_before,
        "flatten_visible_slices": visible_rows,
        "simulated_strategy_order_registry_complete": True,
        "simulated_strategy_order_registry_sha256": digest(
            f"{spec['postfill_episode_id']}-{decision_index}-registry"
        ),
        "legal_action": True,
        "skip_reason": None,
        "reconciliation_ok": True,
        "data_invalid": False,
    }
    keep = deepcopy(common)
    keep.update(
        {
            "action_kind": "KEEP",
            "effective_latency_ms": Decimal("0"),
            "time_in_force": None,
            "self_trade_prevention_type": None,
            "fok_requires_prior_synthetic_cancel_applied": False,
            "fok_book_side": None,
            "fok_limit_price_e4": None,
            "fok_limit_fallback": None,
        }
    )
    flatten = deepcopy(common)
    flatten.update(
        {
            "action_kind": "FLATTEN_FOK",
            "effective_latency_ms": Decimal("60"),
            "time_in_force": C.FOK_TIME_IN_FORCE,
            "self_trade_prevention_type": "taker_at_cross",
            "fok_requires_prior_synthetic_cancel_applied": True,
            "fok_book_side": side,
            "fok_limit_price_e4": limit,
            "fok_limit_fallback": fallback,
        }
    )
    return [keep, flatten]


def normalize_for_outcome(
    spec: dict[str, object],
    raw_state: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    episode, _evidence, _trades, _atom = C._normalize_episode(
        spec,
        (DAY,),
    )
    state = C._normalize_state(raw_state, episode)
    return episode, state


def make_book(
    episode: dict[str, object],
    state: dict[str, object],
    *,
    full: bool,
    same_effective: bool = False,
) -> dict[str, object]:
    effective_ns = (
        state["decision_elapsed_ms"] + C.FOK_EFFECTIVE_LATENCY_MS
    ) * 1_000_000
    recv_offset = effective_ns if same_effective else effective_ns - 1
    levels = (
        deepcopy(state["flatten_visible_slices"])
        if full
        else [
            {
                "price_e4": state["flatten_visible_slices"][0][0],
                "qty_fp": Decimal("1"),
            }
        ]
    )
    levels_json = [
        {"price_e4": int(price), "qty_fp": Decimal(qty)}
        if not isinstance(price, dict)
        else price
        for price, qty in ()
    ]
    if full:
        levels_json = [
            {"price_e4": int(price), "qty_fp": Decimal(qty)}
            for price, qty in state["flatten_visible_slices"]
        ]
    else:
        levels_json = levels
    row: dict[str, object] = {
        "market_ticker": episode["market_ticker"],
        "market_id": episode["market_id"],
        "recv_wall_ns": BASE_WALL_NS + recv_offset,
        "recv_mono_ns": BASE_MONO_NS + recv_offset,
        "ingest_sequence": 9_999,
        "stable_source_id": (
            "zzzz-same-effective"
            if same_effective
            else f"{episode['postfill_episode_id']}-pre-effective-book"
        ),
        "levels": levels_json,
    }
    row["source_rows_sha256"] = C.public_book_source_sha256(row)
    return row


def make_outcome(
    spec: dict[str, object],
    raw_state: dict[str, object],
    terminal_type: str = "FOK_FULL",
    *,
    same_effective_book: bool = False,
) -> dict[str, object]:
    episode, state = normalize_for_outcome(spec, raw_state)
    planned = state["decision_elapsed_ms"] + C.FOK_EFFECTIVE_LATENCY_MS
    effective_wall = BASE_WALL_NS + planned * 1_000_000
    effective_mono = BASE_MONO_NS + planned * 1_000_000
    race = terminal_type == "CANCEL_RACE_PAIR"
    book = (
        None
        if race
        else make_book(
            episode,
            state,
            full=terminal_type == "FOK_FULL",
            same_effective=same_effective_book,
        )
    )
    reported = C.derive_reported_outcome_fields(
        episode=episode,
        state=state,
        terminal_type=terminal_type,
        pre_effective_book=book,
    )
    row: dict[str, object] = {
        "source_date_utc": DAY,
        "postfill_episode_id": spec["postfill_episode_id"],
        "decision_index": state["decision_index"],
        "postfill_action_id": (
            f"{spec['postfill_episode_id']}::V41::GRID::"
            f"{state['decision_index']}::FLATTEN_FOK"
        ),
        "terminal_type": terminal_type,
        "planned_effective_elapsed_ms": planned,
        "synthetic_cancel_applied_wall_ns": (
            None if race else effective_wall
        ),
        "synthetic_cancel_applied_mono_ns": (
            None if race else effective_mono
        ),
        "synthetic_cancel_applied_sequence": None if race else 0,
        "synthetic_cancel_applied_stable_source_id": (
            None if race else f"{spec['postfill_episode_id']}-cancel"
        ),
        "fok_processed_wall_ns": None if race else effective_wall,
        "fok_processed_mono_ns": None if race else effective_mono,
        "fok_processed_sequence": None if race else 1,
        "fok_processed_stable_source_id": (
            None if race else f"{spec['postfill_episode_id']}-fok"
        ),
        "fok_sent": not race,
        "simulated_no_self_cross_verified_before_fok": (
            None if race else True
        ),
        "simulated_strategy_order_registry_sha256": state[
            "simulated_strategy_order_registry_sha256"
        ],
        "pre_effective_public_book": book,
        "race_complement_public_proxy_evidence_id": (
            episode["complement_proxy_evidence_id"] if race else None
        ),
        "fee_bound_selected_for_candidate": (
            C.FEE_BOUND_SELECTED_FOR_CANDIDATE
        ),
        "fee_accumulator_receipt_authenticated": False,
        **reported,
        "reconciliation_ok": True,
        "data_invalid": False,
    }
    return row


def make_inputs(
    spec: dict[str, object],
    *,
    outcome_kind: str = "FOK_FULL",
    visible: list[dict[str, object]] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    compact: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    terminal = Decimal(spec["terminal_elapsed_ms"])
    zero = bool(spec["zero_time_atom"])
    for index, _elapsed in C.required_decision_grid(
        terminal,
        zero_time_atom=zero,
    ):
        pair = make_compact_pair(spec, index, visible=visible)
        compact.extend(pair)
        kind = outcome_kind
        if (
            spec["terminal_type"] == "COMPLEMENT_FILL"
            and Decimal(pair[0]["decision_elapsed_ms"])
            < terminal
            < Decimal(pair[0]["decision_elapsed_ms"] + 60)
        ):
            kind = "CANCEL_RACE_PAIR"
        outcomes.append(make_outcome(spec, pair[0], kind))
    return compact, outcomes


def validate(
    spec: dict[str, object],
    compact: list[dict[str, object]],
    outcomes: list[dict[str, object]],
) -> C.PostfillV41DDLBatch:
    return C.validate_and_serialize_postfill_rows(
        logical_source_paths={DAY: [f"/sealed/{DAY}/public.jsonl"]},
        expected_episode_ids_by_day={
            DAY: [str(spec["postfill_episode_id"])]
        },
        episode_manifest=[spec],
        compact_rows=compact,
        flatten_fok_outcomes=outcomes,
    )


def one_grid(
    *,
    outcome_kind: str = "FOK_FULL",
    first_side: str = "YES",
    visible: list[dict[str, object]] | None = None,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    terminal = 30 if outcome_kind == "CANCEL_RACE_PAIR" else 100
    spec = make_episode(
        terminal_ms=terminal,
        first_side=first_side,
    )
    compact = make_compact_pair(spec, 0, visible=visible)
    outcomes = [make_outcome(spec, compact[0], outcome_kind)]
    return spec, compact, outcomes


def test_v41_full_fee_partition_and_capital_are_conservative() -> None:
    visible = [{"price_e4": 3_000, "qty_fp": Decimal("2")}]
    spec, compact, outcomes = one_grid(visible=visible)
    batch = validate(spec, compact, outcomes)
    outcome = batch.postfill_v41_flatten_fok_outcome[0]
    assert outcome["taker_fee_raw_total_usd"] == Decimal("0.0294")
    assert (
        outcome["taker_fee_trade_upper_before_account_rounding_usd"]
        == Decimal("0.04")
    )
    assert (
        outcome["taker_fee_partition_dp_no_rebate_upper_usd"]
        == Decimal("0.04")
    )
    assert outcome["taker_fee_sql_safe_upper_usd"] == Decimal("0.06")
    assert Decimal("0.03") < outcome["taker_fee_safe_upper_usd"]
    assert (
        outcome["fee_bound_selected_for_candidate"]
        == C.FEE_BOUND_SELECTED_FOR_CANDIDATE
    )
    assert outcome["capital_dollar_seconds"] == (
        outcome["locked_pair_capital_safe_upper_usd"]
        * outcome["synthetic_cancel_effective_elapsed_ms"]
        + (
            outcome["first_leg_basis_safe_upper_usd"]
            + outcome["fok_candidate_reserve_safe_upper_usd"]
        )
        * (
            outcome["fok_terminal_elapsed_ms"]
            - outcome["synthetic_cancel_effective_elapsed_ms"]
        )
    ) / Decimal("1000")


def test_v41_historical_cancel_state_is_always_none() -> None:
    spec, compact, outcomes = one_grid()
    for row in compact:
        row["simulated_cancel_state"] = "ACKED"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="simulated_cancel_state must be NONE",
    ):
        validate(spec, compact, outcomes)


def test_v41_same_effective_public_book_is_fail_closed_even_zzzz() -> None:
    spec, compact, _outcomes = one_grid()
    outcome = make_outcome(
        spec,
        compact[0],
        "FOK_FULL",
        same_effective_book=True,
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="strictly pre-effective",
    ):
        validate(spec, compact, [outcome])


def test_v41_t0_state_cannot_consume_same_time_future_row() -> None:
    spec, compact, outcomes = one_grid()
    for row in compact:
        row["source_max_ingest_sequence"] = 11
        row["source_max_stable_id"] = "zzzz-same-time-future"
        row["causal_source_rows_sha256"] = digest("includes-future")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="t=0 state must seal exactly",
    ):
        validate(spec, compact, outcomes)


def test_v41_execution_slices_are_derived_not_self_reported() -> None:
    spec, compact, outcomes = one_grid()
    forged = deepcopy(outcomes[0])
    forged["fok_fill_slices"] = [
        {"price_e4": 3_000, "qty_fp": Decimal("2")}
    ]
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="unsealed fields",
    ):
        validate(spec, compact, [forged])
    batch = validate(spec, compact, outcomes)
    assert sum(
        row["executed_qty_fp"]
        for row in batch.postfill_v41_fok_slice
    ) == Decimal("2")


def test_v41_zero_capital_keeps_b_only_until_public_settlement() -> None:
    spec, compact, outcomes = one_grid(outcome_kind="FOK_ZERO")
    batch = validate(spec, compact, outcomes)
    outcome = batch.postfill_v41_flatten_fok_outcome[0]
    x = outcome["synthetic_cancel_effective_elapsed_ms"]
    f = outcome["fok_terminal_elapsed_ms"]
    r = outcome["settlement_release_elapsed_ms"]
    b = outcome["first_leg_basis_safe_upper_usd"]
    locked = outcome["locked_pair_capital_safe_upper_usd"]
    c = outcome["fok_candidate_reserve_safe_upper_usd"]
    assert outcome["capital_dollar_seconds"] == (
        locked * x + (b + c) * (f - x) + b * (r - f)
    ) / Decimal("1000")
    assert r > spec["market_close_elapsed_ms"]


def test_v41_hard_capital_uses_x_then_public_settlement_r() -> None:
    spec = make_episode(terminal_type="HARD_FALLBACK")
    compact, outcomes = make_inputs(spec, outcome_kind="FOK_ZERO")
    batch = validate(spec, compact, outcomes)
    transitions = batch.postfill_v41_keep_transition
    total = sum(
        (
            row["capital_dollar_seconds_increment"]
            for row in transitions
        ),
        Decimal("0"),
    )
    final = transitions[-1]
    x = Decimal(spec["complement_release_elapsed_ms"])
    r = SETTLEMENT_MS
    b = final["first_leg_basis_safe_upper_usd"]
    locked = final["locked_pair_capital_safe_upper_usd"]
    assert x > MARKET_CLOSE_MS
    assert final["interval_stop_elapsed_ms"] == r
    assert total == (
        locked * x + b * (r - x)
    ) / Decimal("1000")


def test_v41_other_market_official_result_is_rejected() -> None:
    spec = make_episode(terminal_type="HARD_FALLBACK")
    settlement = spec["public_settlement_receipt"]
    settlement["market_ticker"] = "KXBTC15M-OTHER"
    settlement["source_rows_sha256"] = C.settlement_receipt_sha256(
        settlement
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="another market",
    ):
        validate(spec, [], [])


def test_v41_complement_fill_equal_close_is_rejected() -> None:
    spec = make_episode(
        terminal_ms=MARKET_CLOSE_MS,
        terminal_type="COMPLEMENT_FILL",
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="strictly before market close",
    ):
        validate(spec, [], [])


def test_v41_held_no_fok_cash_requires_principal_plus_fee() -> None:
    visible = [{"price_e4": 3_000, "qty_fp": Decimal("2")}]
    spec, compact, outcomes = one_grid(
        first_side="NO",
        visible=visible,
    )
    batch = validate(spec, compact, outcomes)
    state = batch.postfill_v41_causal_state[0]
    assert state["flatten_book_side"] == "BID"
    assert state["flatten_limit_price_e4"] == 3_100
    assert state["fok_required_principal_usd"] == Decimal("0.62")
    assert state["fok_required_fee_safe_upper_usd"] == Decimal("0.04")
    assert (
        state["fok_required_cash_with_fee_safe_upper_usd"]
        == Decimal("0.66")
    )
    for row in compact:
        row["simulated_available_cash_before_cancel_usd"] = Decimal("0")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="cash requirement is infeasible",
    ):
        validate(spec, compact, outcomes)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("first_fill_price_e4", 3_001),
        ("complement_price_e4", 6_901),
    ),
)
def test_v41_off_grid_episode_prices_are_rejected(
    field: str,
    value: int,
) -> None:
    spec = make_episode()
    spec[field] = value
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="tapered_deci_cent grid",
    ):
        validate(spec, [], [])


def test_v41_ticker_date_window_metadata_binding_is_required() -> None:
    spec = make_episode()
    spec["market_ticker"] = "KXBTC15M-26JUL21-MISMATCH"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="metadata binding mismatch",
    ):
        validate(spec, [], [])
    self_consistent_wrong_day = make_episode()
    self_consistent_wrong_day["market_ticker"] = (
        "KXBTC15M-26JUL21-MISMATCH"
    )
    metadata = self_consistent_wrong_day["market_metadata"]
    metadata["market_ticker"] = self_consistent_wrong_day["market_ticker"]
    metadata["source_rows_sha256"] = C.market_metadata_sha256(metadata)
    settlement = self_consistent_wrong_day["public_settlement_receipt"]
    settlement["market_ticker"] = self_consistent_wrong_day["market_ticker"]
    settlement["source_rows_sha256"] = C.settlement_receipt_sha256(
        settlement
    )
    evidence = self_consistent_wrong_day["first_fill_public_proxy_evidence"]
    evidence["market_ticker"] = self_consistent_wrong_day["market_ticker"]
    for trade in evidence["public_trade_rows"]:
        trade["ticker"] = self_consistent_wrong_day["market_ticker"]
        trade["source_rows_sha256"] = C.public_trade_row_sha256(trade)
    evidence["public_trade_spine_sha256"] = C.public_trade_spine_sha256(
        evidence["public_trade_rows"]
    )
    evidence["source_rows_sha256"] = C.public_proxy_evidence_sha256(
        evidence
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="expiry date does not match",
    ):
        validate(self_consistent_wrong_day, [], [])


def test_v41_first_proxy_recomputes_full_strict_trade_through() -> None:
    spec = make_episode()
    evidence = spec["first_fill_public_proxy_evidence"]
    evidence["derived_cumulative_strict_through_qty_fp"] = Decimal("1")
    evidence["source_rows_sha256"] = C.public_proxy_evidence_sha256(
        evidence
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="trade-row spine",
    ):
        validate(spec, [], [])


def test_v41_tight_fee_sensitivity_cannot_be_candidate_gate() -> None:
    spec, compact, outcomes = one_grid()
    outcomes[0]["fee_bound_selected_for_candidate"] = (
        C.DOC_LITERAL_TIGHT_SENSITIVITY
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="tight accumulator bound cannot be selected",
    ):
        validate(spec, compact, outcomes)


@pytest.mark.parametrize(
    ("first_side", "first_price_e4", "complement_price_e4"),
    (
        ("YES", 3_000, 6_900),
        ("NO", 6_900, 3_000),
    ),
)
def test_v41_proxy_uses_public_taker_direction_and_yes_price_scale(
    first_side: str,
    first_price_e4: int,
    complement_price_e4: int,
) -> None:
    spec = make_episode(
        first_side=first_side,
        first_price_e4=first_price_e4,
        complement_price_e4=complement_price_e4,
    )
    first_proxy = spec["first_fill_public_proxy_evidence"]
    complement_proxy = spec[
        "terminal_complement_public_proxy_evidence"
    ]
    correct_public_taker_semantics(first_proxy)
    correct_public_taker_semantics(complement_proxy)
    episode, evidence, trade_rows, _atom = C._normalize_episode(
        spec,
        (DAY,),
    )
    by_kind = {row["proxy_kind"]: row for row in evidence}
    assert episode["first_fill_side"] == first_side
    first_trade = next(
        row
        for row in trade_rows
        if row["evidence_id"] == by_kind["FIRST_FILL"]["evidence_id"]
    )
    assert first_trade["taker_book_side"] == (
        "ASK" if first_side == "YES" else "BID"
    )
    complement_side = "NO" if first_side == "YES" else "YES"
    complement_yes_price = (
        complement_price_e4
        if complement_side == "YES"
        else 10_000 - complement_price_e4
    )
    complement_trade = next(
        row
        for row in trade_rows
        if row["evidence_id"]
        == by_kind["COMPLEMENT_FILL"]["evidence_id"]
    )
    complement_trigger_yes_price = int(complement_trade["yes_price_e4"])
    assert (
        complement_trigger_yes_price > complement_yes_price
        if complement_side == "NO"
        else complement_trigger_yes_price < complement_yes_price
    )


def test_v41_direct_precision_probe_is_mandatory_and_not_discovery_pnl() -> None:
    spec = make_episode()
    spec["account_precision_receipt_sha256"] = digest("forged")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="precision receipt is not pinned",
    ):
        validate(spec, [], [])
    valid = make_episode()
    compact, outcomes = make_inputs(valid)
    batch = validate(valid, compact, outcomes)
    receipt = batch.receipt()
    assert (
        receipt["target_account_precision_receipt_sha256"]
        == C.TARGET_ACCOUNT_PRECISION_RECEIPT_SHA256
    )
    assert receipt["source_dates"] == [DAY]
    assert "2026-07-26" not in receipt["source_dates"]


def test_v41_unknown_min_increment_and_subcent_maker_are_not_candidates() -> None:
    unknown_delta = make_episode()
    metadata = unknown_delta["market_metadata"]
    metadata["market_min_fill_increment_fp"] = Decimal("0.1")
    metadata["source_rows_sha256"] = C.market_metadata_sha256(metadata)
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="minimum true-fill increment",
    ):
        validate(unknown_delta, [], [])

    subcent = make_episode(
        first_price_e4=550,
        complement_price_e4=9_400,
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="sensitivity-only",
    ):
        validate(subcent, [], [])


def test_v41_zero_atom_has_nonnull_ticker_and_provenance() -> None:
    spec = make_episode(episode_id="atom", zero=True)
    batch = validate(spec, [], [])
    atom = batch.postfill_v41_zero_time_atom[0]
    assert atom["market_ticker"]
    assert atom["market_id"]
    assert atom["data_origin"] == C.DATA_ORIGIN
    assert atom["first_fill_provenance"] == C.FILL_PROVENANCE
    assert atom["complement_fill_provenance"] == C.FILL_PROVENANCE
    assert (
        atom["complement_fill_ingest_sequence"]
        > atom["first_fill_ingest_sequence"]
    )


def test_v41_zero_atom_ddl_rejects_null_provenance() -> None:
    spec = make_episode(episode_id="atom", zero=True)
    batch = validate(spec, [], [])
    rows = {
        table: [deepcopy(row) for row in table_rows]
        for table, table_rows in batch.as_ddl_rows().items()
    }
    rows["postfill_v41_zero_time_atom"][0][
        "complement_fill_provenance"
    ] = None
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    with pytest.raises(duckdb.ConstraintException):
        insert_postfill_v41_tables(
            connection,
            rows,
            validator_sql_path=VALIDATOR_SQL,
        )


def test_v41_sql_gate_rejects_fake_123_and_capital_mutation() -> None:
    spec, compact, outcomes = one_grid()
    batch = validate(spec, compact, outcomes)
    forged_rows = {
        table: [deepcopy(row) for row in rows]
        for table, rows in batch.as_ddl_rows().items()
    }
    outcome = forged_rows["postfill_v41_flatten_fok_outcome"][0]
    outcome["gross_pnl_usd"] = Decimal("123")
    outcome["conservative_net_pnl_usd"] = Decimal("123")
    outcome["capital_dollar_seconds"] = Decimal("123")
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    with pytest.raises(
        Round4ContractError,
        match="V4.1 normative SQL validation failed",
    ):
        insert_postfill_v41_tables(
            connection,
            forged_rows,
            validator_sql_path=VALIDATOR_SQL,
        )
    assert connection.execute(
        "SELECT count(*) FROM postfill_v41_flatten_fok_outcome"
    ).fetchone() == (0,)


def test_v41_sql_gate_independently_rejects_capital_only_mutation() -> None:
    spec, compact, outcomes = one_grid()
    batch = validate(spec, compact, outcomes)
    forged_rows = {
        table: [deepcopy(row) for row in rows]
        for table, rows in batch.as_ddl_rows().items()
    }
    forged_rows["postfill_v41_flatten_fok_outcome"][0][
        "capital_dollar_seconds"
    ] += Decimal("1")
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    with pytest.raises(
        Round4ContractError,
        match="CAPITAL_RECOMPUTE",
    ):
        insert_postfill_v41_tables(
            connection,
            forged_rows,
            validator_sql_path=VALIDATOR_SQL,
        )
    assert connection.execute(
        "SELECT count(*) FROM postfill_v41_flatten_fok_outcome"
    ).fetchone() == (0,)


def test_v41_sql_gate_accepts_complete_batch() -> None:
    spec, compact, outcomes = one_grid()
    batch = validate(spec, compact, outcomes)
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    receipt = insert_postfill_v41_tables(
        connection,
        batch.as_ddl_rows(),
        validator_sql_path=VALIDATOR_SQL,
    )
    assert receipt["status"] == "POSTFILL_V41_SQL_GATE_VALID"
    assert connection.execute(
        "SELECT count(*) FROM postfill_v41_fok_slice"
    ).fetchone() == (2,)


def test_v41_batch_normalizes_schedule_metadata_settlement_and_trade_spine(
) -> None:
    spec, compact, outcomes = one_grid()
    batch = validate(spec, compact, outcomes)
    tables = batch.as_ddl_rows()
    assert {
        "postfill_v41_fee_schedule_receipt",
        "postfill_v41_market_metadata_receipt",
        "postfill_v41_settlement_receipt",
        "postfill_v41_public_trade_row",
    } <= set(tables)
    assert len(tables["postfill_v41_fee_schedule_receipt"]) == 1
    assert len(tables["postfill_v41_market_metadata_receipt"]) == 1
    assert len(tables["postfill_v41_settlement_receipt"]) == 1
    assert len(tables["postfill_v41_public_trade_row"]) == 2
    schedule = tables["postfill_v41_fee_schedule_receipt"][0]
    assert schedule["schedule_pdf_raw_bytes_authenticated"] is True
    assert schedule["schedule_pdf_raw_sha256"] == (
        "815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c"
    )
    assert schedule["schedule_pdf_page_count"] == 12
    assert schedule["series_fee_type"] == "quadratic"
    assert schedule["series_fee_change_count"] == 0


@pytest.mark.parametrize(
    "scenario",
    ("FOK_ZERO", "CANCEL_RACE_PAIR", "HARD_FALLBACK", "ZERO_ATOM"),
)
def test_v41_sql_gate_accepts_every_terminal_branch(
    scenario: str,
) -> None:
    if scenario == "HARD_FALLBACK":
        spec = make_episode(terminal_type="HARD_FALLBACK")
        compact, outcomes = make_inputs(spec, outcome_kind="FOK_ZERO")
    elif scenario == "ZERO_ATOM":
        spec = make_episode(episode_id="atom", zero=True)
        compact, outcomes = [], []
    else:
        spec, compact, outcomes = one_grid(outcome_kind=scenario)
    batch = validate(spec, compact, outcomes)
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    receipt = insert_postfill_v41_tables(
        connection,
        batch.as_ddl_rows(),
        validator_sql_path=VALIDATOR_SQL,
    )
    assert receipt["status"] == "POSTFILL_V41_SQL_GATE_VALID"


def _one_grid_ddl_rows() -> dict[str, list[dict[str, object]]]:
    spec, compact, outcomes = one_grid()
    batch = validate(spec, compact, outcomes)
    return {
        table: [deepcopy(row) for row in table_rows]
        for table, table_rows in batch.as_ddl_rows().items()
    }


def _assert_v41_sql_gate_rejects(
    rows: dict[str, list[dict[str, object]]],
) -> None:
    connection = duckdb.connect(":memory:")
    connection.execute(DDL.read_text())
    with pytest.raises(
        (Round4ContractError, duckdb.ConstraintException),
    ):
        insert_postfill_v41_tables(
            connection,
            rows,
            validator_sql_path=VALIDATOR_SQL,
        )


def test_v41_sql_gate_rejects_orphan_fok_slice() -> None:
    rows = _one_grid_ddl_rows()
    orphan = deepcopy(rows["postfill_v41_fok_slice"][0])
    orphan["fok_slice_id"] = "orphan-slice"
    orphan["postfill_fok_outcome_id"] = "missing-outcome"
    rows["postfill_v41_fok_slice"].append(orphan)
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_orphan_keep_action() -> None:
    rows = _one_grid_ddl_rows()
    orphan = next(
        deepcopy(row)
        for row in rows["postfill_v41_action"]
        if row["action_kind"] == "KEEP"
    )
    orphan["postfill_action_id"] = "orphan-keep"
    orphan["postfill_decision_id"] = "missing-decision"
    rows["postfill_v41_action"].append(orphan)
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_foreign_slice_identity_and_hashes() -> None:
    rows = _one_grid_ddl_rows()
    foreign_book_hash = digest("foreign-book")
    foreign_execution_hash = digest("foreign-execution")
    for slice_row in rows["postfill_v41_fok_slice"]:
        slice_row["source_date_utc"] = "2026-07-21"
        slice_row["market_ticker"] = "KXBTC15M-26JUL21-FOREIGN"
        slice_row["market_id"] = "foreign-market"
        slice_row["postfill_episode_id"] = "foreign-episode"
        slice_row["pre_effective_book_source_rows_sha256"] = (
            foreign_book_hash
        )
        slice_row["execution_slices_sha256"] = foreign_execution_hash
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_predecision_public_book() -> None:
    rows = _one_grid_ddl_rows()
    state = rows["postfill_v41_causal_state"][0]
    outcome = rows["postfill_v41_flatten_fok_outcome"][0]
    outcome["pre_effective_book_recv_wall_ns"] = (
        int(state["decision_recv_wall_ns"]) - 1
    )
    outcome["pre_effective_book_recv_mono_ns"] = (
        int(state["decision_recv_mono_ns"]) - 1
    )
    outcome["pre_effective_book_source_rows_sha256"] = digest(
        "predecision-book"
    )
    for slice_row in rows["postfill_v41_fok_slice"]:
        slice_row["pre_effective_book_source_rows_sha256"] = digest(
            "predecision-book"
        )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_zero_state_fee_upper() -> None:
    rows = _one_grid_ddl_rows()
    rows["postfill_v41_causal_state"][0][
        "fok_required_fee_safe_upper_usd"
    ] = Decimal("0")
    flatten = next(
        row
        for row in rows["postfill_v41_action"]
        if row["action_kind"] == "FLATTEN_FOK"
    )
    flatten["fok_required_fee_safe_upper_usd"] = Decimal("0")
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_coherent_50ms_latency_rewrite() -> None:
    rows = _one_grid_ddl_rows()
    outcome = rows["postfill_v41_flatten_fok_outcome"][0]
    fifty_ms = Decimal("50")
    fifty_ns = int(fifty_ms * Decimal("1000000"))
    pre_book_ns = int(Decimal("49") * Decimal("1000000"))
    outcome["planned_effective_elapsed_ms"] = fifty_ms
    outcome["synthetic_cancel_effective_elapsed_ms"] = fifty_ms
    outcome["fok_terminal_elapsed_ms"] = fifty_ms
    outcome["terminal_elapsed_ms"] = fifty_ms
    outcome["synthetic_cancel_applied_wall_ns"] = BASE_WALL_NS + fifty_ns
    outcome["synthetic_cancel_applied_mono_ns"] = BASE_MONO_NS + fifty_ns
    outcome["fok_processed_wall_ns"] = BASE_WALL_NS + fifty_ns
    outcome["fok_processed_mono_ns"] = BASE_MONO_NS + fifty_ns
    outcome["pre_effective_book_recv_wall_ns"] = BASE_WALL_NS + pre_book_ns
    outcome["pre_effective_book_recv_mono_ns"] = BASE_MONO_NS + pre_book_ns
    outcome["capital_dollar_seconds"] = (
        Decimal(outcome["locked_pair_capital_safe_upper_usd"])
        * fifty_ms
        / Decimal("1000")
    )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_unlinked_settlement_receipt() -> None:
    rows = _one_grid_ddl_rows()
    outcome = rows["postfill_v41_flatten_fok_outcome"][0]
    outcome["public_settlement_receipt_id"] = "other-finalized-receipt"
    outcome["public_settlement_source_rows_sha256"] = digest(
        "other-finalized-receipt"
    )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_foreign_first_proxy() -> None:
    rows = _one_grid_ddl_rows()
    first = next(
        row
        for row in rows["postfill_v41_public_proxy_evidence"]
        if row["proxy_kind"] == "FIRST_FILL"
    )
    first["market_ticker"] = "KXBTC15M-26JUL21-FOREIGN"
    first["market_id"] = "foreign-market"
    first["source_date_utc"] = "2026-07-21"
    first["source_rows_sha256"] = digest("foreign-first-proxy")
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_unpinned_outcome_fee_schedule() -> None:
    rows = _one_grid_ddl_rows()
    outcome = rows["postfill_v41_flatten_fok_outcome"][0]
    outcome["fee_schedule_provenance"] = "ARBITRARY"
    outcome["fee_schedule_source"] = "https://example.invalid/fee"
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_requires_public_trade_spine_rows() -> None:
    rows = _one_grid_ddl_rows()
    first = next(
        row
        for row in rows["postfill_v41_public_proxy_evidence"]
        if row["proxy_kind"] == "FIRST_FILL"
    )
    rows["postfill_v41_public_trade_row"] = [
        row
        for row in rows["postfill_v41_public_trade_row"]
        if row["evidence_id"] != first["evidence_id"]
    ]
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_derives_proxy_cumulative_from_trade_rows() -> None:
    rows = _one_grid_ddl_rows()
    first = next(
        row
        for row in rows["postfill_v41_public_proxy_evidence"]
        if row["proxy_kind"] == "FIRST_FILL"
    )
    first["derived_cumulative_strict_through_qty_fp"] = (
        Decimal(first["derived_cumulative_strict_through_qty_fp"])
        + Decimal("1")
    )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_rejects_wrong_public_taker_semantics() -> None:
    rows = _one_grid_ddl_rows()
    first = next(
        row
        for row in rows["postfill_v41_public_proxy_evidence"]
        if row["proxy_kind"] == "FIRST_FILL"
    )
    trade = next(
        row
        for row in rows["postfill_v41_public_trade_row"]
        if row["evidence_id"] == first["evidence_id"]
    )
    trade["taker_book_side"] = first["maker_order_yes_book_side"]
    trade["taker_outcome_side"] = first["maker_order_outcome_side"]
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_binds_state_to_market_metadata_receipt() -> None:
    rows = _one_grid_ddl_rows()
    metadata = rows["postfill_v41_market_metadata_receipt"][0]
    metadata["market_window_close_wall_ns"] = (
        int(metadata["market_window_close_wall_ns"]) + 1_000_000_000
    )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_binds_projection_to_settlement_receipt() -> None:
    rows = _one_grid_ddl_rows()
    settlement = rows["postfill_v41_settlement_receipt"][0]
    settlement["official_market_result"] = (
        "NO"
        if settlement["official_market_result"] == "YES"
        else "YES"
    )
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_pins_normalized_fee_schedule_hash() -> None:
    rows = _one_grid_ddl_rows()
    forged_hash = digest("coherent-foreign-fee-schedule")
    rows["postfill_v41_fee_schedule_receipt"][0][
        "source_rows_sha256"
    ] = forged_hash
    for state in rows["postfill_v41_causal_state"]:
        state["fee_schedule_source_rows_sha256"] = forged_hash
    _assert_v41_sql_gate_rejects(rows)


def test_v41_sql_gate_binds_first_proxy_maker_economics_to_state() -> None:
    rows = _one_grid_ddl_rows()
    first = next(
        row
        for row in rows["postfill_v41_public_proxy_evidence"]
        if row["proxy_kind"] == "FIRST_FILL"
    )
    first["maker_order_yes_book_side"] = "ASK"
    first["maker_order_outcome_side"] = "NO"
    first["maker_order_outcome_price_e4"] = 6_900
    first["maker_order_yes_price_e4"] = 3_100
    trade = next(
        row
        for row in rows["postfill_v41_public_trade_row"]
        if row["evidence_id"] == first["evidence_id"]
    )
    trade["taker_book_side"] = "BID"
    trade["taker_outcome_side"] = "YES"
    trade["yes_price_e4"] = 3_200
    _assert_v41_sql_gate_rejects(rows)
