#!/usr/bin/env python3
"""Explicit seeds for the 106 DeepResearch atomic experiments.

This module contains data only.  It deliberately does not import or mutate
``tools.experiment_registry`` so that the registry builder can consume and
validate these seeds without a circular dependency.

An atomic experiment is not automatically a strategy.  ``target_class`` is
``EXECUTABLE_STRATEGY`` only when the seed names a deterministic action
profile; research questions, controls, and model components remain visibly
non-executable.  A seed saying that a signal can be measured is never a PnL
claim.
"""

from __future__ import annotations

from typing import Any


CARD_SEEDS: dict[str, dict[str, Any]] = {}

_RECEIVE = "LOCAL_RECEIVE_CLOCK_PAST_ONLY"
_RFQ_RECEIVE = "RFQ_LOCAL_RECEIVE_CLOCK_PAST_ONLY"
_PRIVATE_RFQ = "PRIVATE_RFQ_EVENT_RECEIVE_CLOCK_PAST_ONLY"
_EXTERNAL_CLOCK = "EXTERNAL_PUBLICATION_AND_LOCAL_RECEIVE_CLOCK_PAST_ONLY"
_TERMINAL_CLOCK = "VENUE_LIFECYCLE_RECEIVE_CLOCK_PAST_ONLY"
_DAY_CLOCK = "UTC_DAY_BOUNDARY_WITH_PAST_ONLY_FEATURES"


def _register(
    experiment_id: str,
    *,
    hypothesis: str,
    trigger: str,
    direction_semantics: str,
    decision_clock: str,
    forecast_horizon: str,
    falsifier: str,
    action_profile: str,
    data_profile: str,
    external_dependency: str = "NOT_REQUIRED",
    target_class: str,
    result_status: str,
    claim_tier: str,
    extra_blockers: tuple[str, ...] = (),
) -> None:
    """Register one fully written seed and reject accidental overwrites."""

    if experiment_id in CARD_SEEDS:
        raise ValueError(f"duplicate experiment seed: {experiment_id}")
    CARD_SEEDS[experiment_id] = {
        "hypothesis": hypothesis,
        "signal": {
            "trigger": trigger,
            "direction_semantics": direction_semantics,
            "decision_clock": decision_clock,
            "forecast_horizon": forecast_horizon,
            "falsifier": falsifier,
        },
        "action_profile": action_profile,
        "data_profile": data_profile,
        "external_dependency": external_dependency,
        "target_class": target_class,
        "result_status": result_status,
        "claim_tier": claim_tier,
        "extra_blockers": list(extra_blockers),
    }


# A — microstructure, single-name book dynamics, and maker policies.
_register(
    "A01-SPREAD-CAPTURE",
    hypothesis=(
        "In eligible two-sided states, passive spread capture remains positive "
        "after strict fills, adverse selection, forced exit, and exact fees."
    ),
    trigger="executable spread exceeds frozen fee, markout, exit, and safety buffers",
    direction_semantics="two-sided passive quote; no directional forecast",
    decision_clock=_RECEIVE,
    forecast_horizon="fill path through 100ms, 1s, 5s, and frozen exit deadline",
    falsifier="pessimistic root-event fee-after PnL is non-positive on any required clean date",
    action_profile="MAKER_SPREAD",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="PARTIAL_EVIDENCE",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FEE_UNKNOWN", "FILL_MODEL_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "A02-TOXICITY-MARKOUT",
    hypothesis=(
        "A past-only toxicity score identifies resting quotes whose cancellation "
        "avoids more adverse-selection loss than the spread opportunity forfeited."
    ),
    trigger="signed flow and quote-state toxicity crosses the frozen vulnerable-side threshold",
    direction_semantics="cancel the quote exposed to the predicted adverse move; never open risk",
    decision_clock=_RECEIVE,
    forecast_horizon="cancel-effective time, 100ms, 1s, and 5s",
    falsifier="signal-gated maker does not beat the identical unfiltered maker after costs",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("OWN_ORDER_CALIBRATION_MISSING", "LATENCY_UNMEASURED", "FEE_UNKNOWN"),
)
_register(
    "A03-DEPLETION-REFILL",
    hypothesis=(
        "Persistent refill after a level depletion creates a safer passive "
        "re-entry window than immediate replacement or permanent abstention."
    ),
    trigger="registered level depletes and refill hazard plus persistence gates both pass",
    direction_semantics="post only on the enumerated depleted side after refill confirmation",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms through 1s refill window plus frozen inventory exit",
    falsifier="refill-conditioned strict-fill PnL is non-positive or no better than immediate re-entry",
    action_profile="MAKER_REFILL",
    data_profile="L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="PARTIAL_EVIDENCE",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=(
        "OWN_ORDER_CALIBRATION_MISSING",
        "FEE_UNKNOWN",
        "INSUFFICIENT_QUALITY_DATES",
    ),
)
_register(
    "A04-QUEUE-LAMBDA-DISTANCE",
    hypothesis=(
        "Quote distance and age provide an own-order-calibrated fill-intensity "
        "surface that improves the join, improve, or abstain decision."
    ),
    trigger="calibrated benign-fill intensity at a registered distance exceeds its adverse-fill hazard",
    direction_semantics="place one passive quote on the side and distance explicitly enumerated by the owner card",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime through fill, cancel acknowledgement, or timeout",
    falsifier="public queue proxy fails to predict private own-order fills or fee-after PnL",
    action_profile="PASSIVE_FADE",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("OWN_ORDER_CALIBRATION_MISSING", "FILL_MODEL_MISSING", "FEE_UNKNOWN"),
)
_register(
    "A05-SNBD-LIQUIDITY-REGIMES",
    hypothesis=(
        "Past-only single-name book states select maker regimes with higher "
        "fee-after PnL than quoting throughout every valid state."
    ),
    trigger="registered SNBD state enters a state-action cell with positive frozen training edge",
    direction_semantics="two-sided or enumerated one-sided passive action owned by the selected state cell",
    decision_clock=_RECEIVE,
    forecast_horizon="state dwell, transition, fill path, and frozen exit deadline",
    falsifier="state filtering has no incremental root-event PnL over the always-valid-state maker",
    action_profile="MAKER_FILTER",
    data_profile="L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("OWN_ORDER_CALIBRATION_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A06-QUOTE-LIFETIME-BURST",
    hypothesis=(
        "Very short quote lifetimes and update bursts lead toxic moves early "
        "enough for cancellation to improve a maker's PnL."
    ),
    trigger="quote-lifetime lower-tail event coincides with a frozen update-rate burst",
    direction_semantics="cancel or widen the affected side; an unsigned burst cancels both sides",
    decision_clock=_RECEIVE,
    forecast_horizon="cancel-effective latency, 100ms, 1s, and 5s",
    falsifier="the burst does not lead adverse executable movement or false pauses erase avoided loss",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("OWN_ORDER_CALIBRATION_MISSING", "LATENCY_UNMEASURED", "FEE_UNKNOWN"),
)
_register(
    "A07A-OCCUPANCY-FINGERPRINT",
    hypothesis=(
        "An occupancy fingerprint adds fee-after market-selection value beyond "
        "volume, spread, and activity without asserting participant identity."
    ),
    trigger="past-only occupancy score admits a market after simple-selector controls",
    direction_semantics="route only an already registered child action; occupancy supplies no side",
    decision_clock=_RECEIVE,
    forecast_horizon="selected child root-event path and UTC-day portfolio result",
    falsifier="nested out-of-fold child PnL does not improve over the simple selector",
    action_profile="ROUTER",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A07B-COORDINATED-RETREAT",
    hypothesis=(
        "Coordinated multi-level retreat leads adverse movement by more than "
        "cancel-effective latency, so a retreat kill reduces losses."
    ),
    trigger="frozen multi-level depth-retreat score crosses its kill threshold",
    direction_semantics="cancel the side losing depth; cancel both if retreat direction is ambiguous",
    decision_clock=_RECEIVE,
    forecast_horizon="retreat onset to cancel-effective time, 100ms, 1s, and 5s",
    falsifier="retreat has no safety lead or avoided loss is smaller than false-kill opportunity cost",
    action_profile="MAKER_FILTER",
    data_profile="L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="PARTIAL_EVIDENCE",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("LATENCY_UNMEASURED", "OWN_ORDER_CALIBRATION_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A07C-ADVERSE-SELECTION-TRANSFER",
    hypothesis=(
        "Toxic flow or retreat in one canonically linked market transfers "
        "adverse selection to a sibling book before that book reprices."
    ),
    trigger="age-aligned source toxicity event occurs while the target quote remains unchanged",
    direction_semantics="cancel the target side exposed by the canonical outcome mapping; abstain if mapping is unknown",
    decision_clock=_RECEIVE,
    forecast_horizon="source event through target cancel-effective time and 5s",
    falsifier="target adverse markout is absent after age, root, liquidity, and placebo controls",
    action_profile="MAKER_FILTER",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_MAPPING",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN", "LATENCY_UNMEASURED", "FEE_UNKNOWN"),
)
_register(
    "A07D-ANCHOR-MANIPULABILITY",
    hypothesis=(
        "Rejecting a fair-value anchor when its visible cost-to-move is small "
        "prevents more downstream strategy loss than it sacrifices."
    ),
    trigger="anchor depth and cost-to-move fall below frozen robustness thresholds",
    direction_semantics="cancel affected child quotes and forbid new risk; the anchor never creates a directional trade",
    decision_clock=_RECEIVE,
    forecast_horizon="anchor fragility episode through child recovery and exit",
    falsifier="fragility gating does not improve the identical child policy or an independent anchor is unaffected",
    action_profile="MAKER_FILTER",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("MISSING_SIGNAL_INPUT", "OWN_ORDER_CALIBRATION_MISSING", "FEE_UNKNOWN"),
)
_register(
    "A08-PRICE-GRID-ROUND-NUMBER",
    hypothesis=(
        "Round-number price levels have a reproducible fill-minus-markout "
        "difference large enough to justify a one-tick quote adjustment."
    ),
    trigger="quote enters a frozen grid-distance cell with positive incremental training economics",
    direction_semantics="cancel and replace at the enumerated safer or higher-value tick; no side is inferred from roundness",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime, fill path, 1s markout, and exit",
    falsifier="grid-conditioned fee-after PnL is no better than the same quote without grid adjustment",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("OWN_ORDER_CALIBRATION_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "A09-LIQUIDITY-SEASONALITY",
    hypothesis=(
        "A frozen time or phase schedule can abstain from loss-making maker "
        "windows while retaining positive windows."
    ),
    trigger="past-only time-phase cell is on the frozen allowlist and data quality is valid",
    direction_semantics="time selects an authorized child but supplies no market side",
    decision_clock=_RECEIVE,
    forecast_horizon="active window, child fill path, and UTC-day PnL",
    falsifier="scheduled routing fails to beat the same child active throughout all valid windows",
    action_profile="ROUTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FEE_UNKNOWN", "FILL_MODEL_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A10-CROSS-MARKET-QUOTE-SPILLOVER",
    hypothesis=(
        "A canonically linked leader quote change reaches a follower slowly "
        "enough for defensive cancellation or repricing to reduce loss."
    ),
    trigger="age-aligned leader moves while follower executable quote is stale",
    direction_semantics="map leader outcome to follower side through audited identity; unknown mapping means abstain",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms through 5s plus follower cancel-effective time",
    falsifier="time reversal, unrelated-root, or stale-age controls match the observed follower response",
    action_profile="MAKER_FILTER",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_MAPPING",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN", "LATENCY_UNMEASURED", "FEE_UNKNOWN"),
)
_register(
    "A11-ONE-SIDED-PROVISION",
    hypothesis=(
        "Supplying the missing side of a persistent one-sided book earns "
        "positive fee-after PnL after inventory exit risk."
    ),
    trigger="valid bid-only or ask-only state persists past the frozen dwell gate",
    direction_semantics="post only the explicitly missing YES-normalized side",
    decision_clock=_RECEIVE,
    forecast_horizon="one-sided dwell through fill, topology recovery, or forced exit",
    falsifier="missing-side strict fills are absent or their adverse/exit loss exceeds captured spread",
    action_profile="PASSIVE_FADE",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "A12-GENERIC-PASSIVE-SCALPING",
    hypothesis=(
        "A passive entry and passive exit inside a stable range survives "
        "strict two-leg fills, fees, and breakout loss."
    ),
    trigger="frozen range, spread, volatility, and depth gates admit a round-trip opportunity",
    direction_semantics="enter on the enumerated cheap side and exit the same inventory; never count unmatched legs as profit",
    decision_clock=_RECEIVE,
    forecast_horizon="entry timeout through second fill or forced-exit deadline",
    falsifier="strict round trips vanish or breakout and forced-exit losses make root-event PnL non-positive",
    action_profile="PASSIVE_FADE",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "A13-T90-RANGE",
    hypothesis=(
        "Past-only Tennis leader episodes in the 88–93 cent range provide a "
        "repeatable passive range-making edge after retreat and terminal risks."
    ),
    trigger="authoritative Tennis mapping, prior two-way tug, 88–93 leader state, and depth gates all pass",
    direction_semantics="quote the registered high/low outcome mapping; leader flips cancel the episode",
    decision_clock=_RECEIVE,
    forecast_horizon="T90 episode through retreat, timeout, forced exit, or settlement",
    falsifier="eligible episodes are too rare or pessimistic fee-after PnL is non-positive",
    action_profile="T90_RANGE",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("VENUE_PERMISSION_MISSING", "INSUFFICIENT_QUALITY_DATES", "FEE_UNKNOWN"),
)
_register(
    "A14-T90-REBUILD",
    hypothesis=(
        "After a completed T90 retreat kill and zero-resting reconciliation, "
        "one rebuild re-entry adds positive PnL over never re-entering."
    ),
    trigger="parent retreat completed, zero resting confirmed, and frozen rebuilt-depth gate passes",
    direction_semantics="one passive re-entry in the parent orientation; no second cycle",
    decision_clock=_RECEIVE,
    forecast_horizon="rebuild window through one fill cycle, renewed retreat, or exit",
    falsifier="rebuild window closes before fill or paired incremental PnL over the parent is non-positive",
    action_profile="T90_REBUILD",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("VENUE_PERMISSION_MISSING", "OWN_ORDER_CALIBRATION_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A15-FAIR-MICROPRICE-PAST-FLOW",
    hypothesis=(
        "A past-flow microprice estimates near-term executable fair value better "
        "than midpoint and improves maker repricing PnL."
    ),
    trigger="microprice displacement exceeds the frozen reprice threshold and costs",
    direction_semantics="cancel and replace quotes around the signed microprice; no aggressive opening",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms, 100ms, 1s, and maker exit",
    falsifier="microprice forecast or fee-after maker increment does not beat midpoint",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "A16-INVENTORY-RESERVATION-SKEW",
    hypothesis=(
        "Position-aware reservation-price skew reduces inventory tail loss "
        "without erasing spread capture."
    ),
    trigger="authoritative position and frozen fair/volatility inputs imply a non-zero reservation skew",
    direction_semantics="widen the inventory-increasing side and improve only the reducing side",
    decision_clock=_RECEIVE,
    forecast_horizon="position path through flat, risk timeout, or terminal",
    falsifier="skewed policy does not beat the same symmetric maker on PnL and tail risk",
    action_profile="SIZE_RISK",
    data_profile="L1_PRIVATE",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("PRIVATE_ACCOUNT_STATE_MISSING", "FEE_UNKNOWN", "RISK_GATES_MISSING"),
)
_register(
    "A17-DYNAMIC-QUOTE-RADIUS",
    hypothesis=(
        "A radius driven by past-only volatility and toxicity outperforms one "
        "fixed quote radius after fill and fee costs."
    ),
    trigger="frozen volatility-toxicity cell maps to a radius different from the fixed baseline",
    direction_semantics="symmetrically widen or narrow unless a separately registered side signal exists",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime, 1s adverse markout, and forced exit",
    falsifier="dynamic radius has no paired fee-after PnL improvement over fixed radius",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "A18-TIME-SHRINKING-CAP",
    hypothesis=(
        "Reducing inventory caps as scheduled start or terminal approaches "
        "improves tail-adjusted PnL and capital use versus a constant cap."
    ),
    trigger="time-to-start or time-to-terminal crosses a frozen cap step",
    direction_semantics="reduce or forbid risk-increasing orders; exits remain allowed",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="cap step through start, settlement, or complete unwind",
    falsifier="shrinking caps do not improve worst-tail loss or commercial return on capital",
    action_profile="SIZE_RISK",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "PRIVATE_ACCOUNT_STATE_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "A19-NO-TRADE-QUOTE-JUMP",
    hypothesis=(
        "A quote jump with no intervening public trade reveals information risk "
        "that a trade-only kill misses."
    ),
    trigger="executable quote moves by the frozen threshold with zero intervening trades",
    direction_semantics="cancel the side made stale by the quote jump; ambiguous gaps cancel both",
    decision_clock=_RECEIVE,
    forecast_horizon="jump through cancel-effective time, 100ms, 1s, and 5s",
    falsifier="no-trade jumps do not lead adverse fills or are explained by gaps and reconnects",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("LATENCY_UNMEASURED", "OWN_ORDER_CALIBRATION_MISSING", "FEE_UNKNOWN"),
)
_register(
    "A20-SYSTEM-LOAD-QUEUE-GROWTH",
    hypothesis=(
        "Local decision and send-queue growth predicts cancel-safety failure, "
        "so a load kill prevents losses."
    ),
    trigger="decision age, send queue, or acknowledgement latency exceeds a frozen load threshold",
    direction_semantics="cancel all resting risk and forbid new risk until telemetry recovers",
    decision_clock="LOCAL_MONOTONIC_SYSTEM_AND_VENUE_RECEIVE_CLOCKS",
    forecast_horizon="load breach through cancel acknowledgement and reconciliation",
    falsifier="load telemetry has no relation to cancel misses or the kill loses more than it avoids",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("MISSING_SIGNAL_INPUT", "LATENCY_UNMEASURED", "OWN_ORDER_CALIBRATION_MISSING"),
)


# B — directional, reversion, volatility, and certainty-tail policies.
_register(
    "B01-MOMENTUM-CONTINUATION",
    hypothesis=(
        "Signed flow, quote velocity, and depletion identify continuation large "
        "enough to survive aggressive entry, exit, fees, and latency."
    ),
    trigger="frozen signed-flow continuation score exceeds all-in executable cost plus safety buffer",
    direction_semantics="positive score buys YES and negative score buys NO through an exhaustive orientation map",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms, 1s, 5s, and frozen time or reversal exit",
    falsifier="executable continuation after latency is non-positive or fee-after root PnL is non-positive",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="DESCRIPTIVE_ONLY",
    extra_blockers=("VENUE_PERMISSION_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "B02-MEAN-REVERSION",
    hypothesis=(
        "A shock followed by deceleration and opposing refill creates a passive "
        "fade whose reversal gain exceeds continuation loss and costs."
    ),
    trigger="shock, deceleration, opposing refill, and stable-anchor gates all pass",
    direction_semantics="post one passive contract opposite the enumerated shock direction",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms through 30s or frozen continuation stop",
    falsifier="continuation-adjusted fee-after fade PnL is non-positive",
    action_profile="PASSIVE_FADE",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "B03-POST-JUMP-CONTINUATION-VS-REVERSAL",
    hypothesis=(
        "Past-only post-jump state distinguishes follow from fade well enough "
        "to outperform either unconditional action."
    ),
    trigger="jump classifier assigns follow or reverse with confidence above the frozen abstention threshold",
    direction_semantics="follow maps to jump sign, reverse maps against it, and uncertain state takes no order",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms, 1s, 5s, and registered stop",
    falsifier="chronological paired PnL fails to beat both always-follow and always-fade baselines",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="DESCRIPTIVE_ONLY",
    extra_blockers=("VENUE_PERMISSION_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "B04-LARGE-FLOW-CONTINUATION-VS-REVERSAL",
    hypothesis=(
        "Large public trades have a conditional continuation or reversal "
        "response that remains tradable after impact and costs."
    ),
    trigger="trade size percentile and frozen post-trade book response select follow or fade",
    direction_semantics="use audited aggressor orientation; unknown trade side means abstain",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms through 30s and frozen exit",
    falsifier="large-flow conditional executable return is non-positive after fees and impact",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="DESCRIPTIVE_ONLY",
    extra_blockers=("VENUE_PERMISSION_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "B05-OFI-PREDICTIVITY",
    hypothesis=(
        "Sequence-valid order-flow imbalance adds signed executable-return "
        "information beyond public trade flow."
    ),
    trigger="past-only OFI crosses a frozen side-specific threshold after simple-flow controls",
    direction_semantics="positive normalized OFI buys YES and negative normalized OFI buys NO",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms, 1s, 5s, and 30s",
    falsifier="OFI has no chronological incremental return or fees consume the predicted move",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("INSUFFICIENT_QUALITY_DATES", "FEE_UNKNOWN", "VENUE_PERMISSION_MISSING"),
)
_register(
    "B06-BOOK-IMBALANCE-PREDICTIVITY",
    hypothesis=(
        "Top-k depth imbalance predicts signed executable returns after spread, "
        "liquidity, and price controls."
    ),
    trigger="sequence-valid top-k imbalance exceeds the frozen directional threshold",
    direction_semantics="buy the outcome whose normalized same-side pressure is positive; unknown depth abstains",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms, 1s, 5s, and 30s",
    falsifier="shuffle, matched-state, or chronological tests remove the directional increment",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("INSUFFICIENT_QUALITY_DATES", "FEE_UNKNOWN", "VENUE_PERMISSION_MISSING"),
)
_register(
    "B07-MICROPRICE-PREDICTIVITY",
    hypothesis=(
        "Microprice displacement predicts a large enough bid/ask move to cover "
        "crossing costs and latency."
    ),
    trigger="microprice minus midpoint exceeds the frozen executable-cost threshold",
    direction_semantics="positive displacement buys YES and negative displacement buys NO",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms, 100ms, 1s, and 5s",
    falsifier="future executable return fails to exceed midpoint baseline and all-in cost",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("FEE_UNKNOWN", "LATENCY_UNMEASURED", "VENUE_PERMISSION_MISSING"),
)
_register(
    "B08-CONTINUOUS-PRICE-CALIBRATION",
    hypothesis=(
        "Outside the registered E04 near-terminal window, a continuously "
        "point-in-time calibrated probability differs from executable price "
        "enough to support a bounded hold-to-terminal action."
    ),
    trigger=(
        "outside E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW, chronological calibrated "
        "probability minus executable price exceeds fee and tail buffers"
    ),
    direction_semantics="buy YES when calibrated probability is higher and buy NO when its complement is higher",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="non-E04 lifecycle entry through frozen exit or actual terminal settlement",
    falsifier="calibration vanishes out of sample or settlement PnL is non-positive",
    action_profile="TERMINAL_ENTRY",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "FEE_UNKNOWN", "VENUE_PERMISSION_MISSING"),
)
_register(
    "B09-LISTING-TO-START-DRIFT",
    hypothesis=(
        "A frozen listing-to-scheduled-start phase has a repeatable signed drift "
        "that remains after executable costs."
    ),
    trigger="time-since-listing and time-to-scheduled-start enter an admitted drift cell",
    direction_semantics="direction is the cell's training-frozen sign; no actual game state is inferred",
    decision_clock=_RECEIVE,
    forecast_horizon="next phase boundary or frozen pre-start exit",
    falsifier="leave-day-out drift changes sign or executable fee-after PnL is non-positive",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FEE_UNKNOWN", "LATENCY_UNMEASURED", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "B10-INPLAY-PROXY-PRICE-EROSION",
    hypothesis=(
        "The explicitly labeled scheduled-start proxy has a repeatable signed "
        "price-erosion pattern that survives executable costs; it is not a claim "
        "about actual live-game state."
    ),
    trigger="scheduled-start proxy boundary and a training-frozen signed erosion cell are both active",
    direction_semantics="trade only the cell's frozen observed price sign; never call it score, serve, period, inning, or true in-play state",
    decision_clock=_RECEIVE,
    forecast_horizon="scheduled-start proxy through the frozen post-proxy exit",
    falsifier="signed post-proxy drift is unstable or non-positive after executable costs and start-time mismatch stress",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    external_dependency="OPTIONAL_REFINEMENT",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="NO_CLAIM",
    extra_blockers=("FEE_UNKNOWN", "LATENCY_UNMEASURED", "VENUE_PERMISSION_MISSING"),
)
_register(
    "B11-REALIZED-VOL-BY-TIME",
    hypothesis=(
        "Past-only time and phase volatility forecasts improve maker quote "
        "radius relative to one fixed radius."
    ),
    trigger="time-phase conditional volatility maps to a different frozen safety radius",
    direction_semantics="volatility is unsigned and changes width only",
    decision_clock=_RECEIVE,
    forecast_horizon="next 1s, 30s, 5m, and child quote lifetime",
    falsifier="conditional volatility has no out-of-sample forecast or maker-PnL increment",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "B12-JUMP-HAZARD",
    hypothesis=(
        "A past-only jump hazard rises early enough for cancellation to reduce "
        "a maker's adverse tail."
    ),
    trigger="frozen jump-hazard estimate exceeds its cancel threshold",
    direction_semantics="unsigned hazard cancels both sides; a separately audited side signal may cancel one",
    decision_clock=_RECEIVE,
    forecast_horizon="cancel-effective time, 100ms, 1s, and 5s",
    falsifier="hazard lacks cancel headroom or false-pause cost exceeds avoided jump loss",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("LATENCY_UNMEASURED", "OWN_ORDER_CALIBRATION_MISSING", "FEE_UNKNOWN"),
)
_register(
    "B13-CALM-TO-BURST",
    hypothesis=(
        "Activity acceleration during a calm state predicts a burst early enough "
        "for a temporary risk-off action to add PnL."
    ),
    trigger="calm dwell plus quote/trade acceleration crosses the frozen burst threshold",
    direction_semantics="burst probability is unsigned and cancels or widens both sides",
    decision_clock=_RECEIVE,
    forecast_horizon="burst onset through frozen recovery dwell",
    falsifier="burst precision after latency is low or false pauses erase avoided loss",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("LATENCY_UNMEASURED", "FILL_MODEL_MISSING", "FEE_UNKNOWN"),
)
_register(
    "B14-VOL-HARVEST",
    hypothesis=(
        "When executable spread is wide relative to past-only volatility and "
        "jump risk, selective passive quoting has positive fee-after PnL."
    ),
    trigger="spread exceeds predicted adverse move, exact fees, exit cost, and tail buffer",
    direction_semantics="two-sided passive quote; no volatility-derived direction",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime through jump kill, fill, and forced exit",
    falsifier="adverse-jump expected shortfall or strict-fill loss exceeds captured spread",
    action_profile="MAKER_SPREAD",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "B15-VOL-BREAKOUT",
    hypothesis=(
        "A calm-to-burst transition with independent directional confirmation "
        "continues far enough to support an aggressive breakout trade."
    ),
    trigger="burst detector and signed flow or quote-velocity confirmation agree",
    direction_semantics="trade only the confirmed sign; unsigned bursts abstain",
    decision_clock=_RECEIVE,
    forecast_horizon="100ms, 1s, 5s, or frozen reversal stop",
    falsifier="false breakouts or latency consume the continuation edge",
    action_profile="TAKER_DIRECTIONAL",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="DESCRIPTIVE_ONLY",
    extra_blockers=("VENUE_PERMISSION_MISSING", "FEE_UNKNOWN", "LATENCY_UNMEASURED"),
)
_register(
    "B16-CS-ASK",
    hypothesis=(
        "At verified 97 or 98 cent high-side states, economically selling high "
        "by buying the low-side outcome earns positive exact settlement PnL."
    ),
    trigger="registered high-side ask persists at 97c or 98c with complete terminal and fee facts",
    direction_semantics="from flat buy the low-side outcome at 1-q; never assume a naked high-side sale",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="actual settlement including void, retirement, and postponement paths",
    falsifier="conditional upset rate plus exact fee exceeds break-even or settlement-PnL lower bound is non-positive",
    action_profile="CS_ASK",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "VENUE_PERMISSION_MISSING", "FEE_UNKNOWN"),
)
_register(
    "B17-CS-BID-CONTROL",
    hypothesis=(
        "On exactly the B16 ASK fill IDs, the counterfactual BID payoff does not "
        "explain the proposed ASK edge."
    ),
    trigger="a B16 strict ASK fill has exact q, terminal Y, and counterfactual fee facts",
    direction_semantics="payoff transform only; it submits no BID and has no fill or capacity claim",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="same terminal outcome as the paired B16 fill",
    falsifier="paired ASK-minus-BID payoff contrast is absent or reverses",
    action_profile="CONTROL_ONLY",
    data_profile="TERMINAL",
    target_class="RESEARCH_QUESTION",
    result_status="CONTROL_ONLY",
    claim_tier="CONTROL_ONLY",
    extra_blockers=("SETTLEMENT_MISSING", "FILL_MODEL_MISSING", "FEE_UNKNOWN"),
)


# C — cross-market and payout-constrained relative value.
_register(
    "C01-THREEWAY-OVERROUND",
    hypothesis=(
        "An exhaustive three-way outcome family occasionally has an all-leg "
        "executable basket residual larger than fees and orphan risk."
    ),
    trigger="sum of synchronized executable leg costs violates the exact exhaustive payout bound",
    direction_semantics="buy or hedge the complete payout vector specified by the audited residual; never trade one leg alone",
    decision_clock=_RECEIVE,
    forecast_horizon="all-leg execution through hedge completion or joint terminal settlement",
    falsifier="no residual survives quote age, all-leg depth, exact fees, and worst-sequence execution",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "FEE_UNKNOWN"),
)
_register(
    "C02-BRACKET-MONOTONICITY",
    hypothesis=(
        "Ordered bracket contracts contain a statewise monotonicity violation "
        "that can be hedged for positive all-leg PnL."
    ),
    trigger="audited adjacent-bracket or cumulative probability constraint is violated after costs",
    direction_semantics="buy the underpriced payout vector and hedge the dominated vector using exact bracket states",
    decision_clock=_RECEIVE,
    forecast_horizon="multi-leg completion, unwind, or joint terminal",
    falsifier="monotonicity residual disappears under payout mapping, executable depth, or orphan stress",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "INSTRUMENT_MAPPING_UNKNOWN"),
)
_register(
    "C03-THRESHOLD-MONOTONICITY",
    hypothesis=(
        "Nested threshold contracts sometimes violate statewise probability "
        "ordering by more than joint execution costs."
    ),
    trigger="a higher threshold is priced above a lower threshold after synchronized executable-cost adjustment",
    direction_semantics="trade the exact lower/higher threshold hedge implied by the statewise dominance proof",
    decision_clock=_RECEIVE,
    forecast_horizon="all-leg completion, forced unwind, or joint terminal",
    falsifier="no violation survives exact threshold semantics, fees, depth, and serial-leg risk",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "INSTRUMENT_MAPPING_UNKNOWN"),
)
_register(
    "C04-SAME-EVENT-FAMILY-COHERENCE",
    hypothesis=(
        "Canonically linked same-event contracts have a payout-constrained "
        "incoherence that remains after age, fees, and all-leg execution."
    ),
    trigger="family residual exceeds its frozen cost and mapping-confidence threshold",
    direction_semantics="execute only the full statewise-verified family hedge; unknown orientation abstains",
    decision_clock=_RECEIVE,
    forecast_horizon="joint execution through unwind or terminal",
    falsifier="residual is explained by quote age, incomplete payout states, or infeasible fills",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "FEE_UNKNOWN"),
)
_register(
    "C05-SOCCER-POISSON",
    hypothesis=(
        "A real Soccer Poisson estimate built from authoritative point-in-time "
        "sport data can expose a payout-verified relative-value discrepancy; "
        "internal implied-rate consistency alone is not a trading strategy."
    ),
    trigger="external-as-of fitted score-rate distribution and mapped contracts produce an all-leg residual above costs",
    direction_semantics="trade only the full payout-mapped vector; never infer score, lineup, or match state from prices",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="external observation through multi-leg completion, pre-start unwind, or terminal",
    falsifier="external model is uncalibrated, one common rate fits within uncertainty, or execution removes the residual",
    action_profile="RELATIVE_VALUE",
    data_profile="EXTERNAL",
    external_dependency="REQUIRED",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_EXTERNAL",
    claim_tier="NO_CLAIM",
    extra_blockers=(
        "EXTERNAL_SPORTS_DATA_MISSING",
        "EXTERNAL_API_UNVERIFIED",
        "PAYOUT_EXHAUSTIVENESS_MISSING",
        "MULTI_LEG_ENGINE_MISSING",
    ),
)
_register(
    "C06-WITHIN-EVENT-LEADLAG",
    hypothesis=(
        "A frozen same-event leader reprices before a mapped follower often "
        "enough for a defensive follower cancel to improve PnL."
    ),
    trigger="age-aligned leader move occurs while the canonical follower quote remains stale",
    direction_semantics="cancel the follower side exposed by audited payout orientation; this seed does not authorize a directional opener",
    decision_clock=_RECEIVE,
    forecast_horizon="10ms through 5s and follower cancel-effective time",
    falsifier="time-reversal or unrelated-root placebo matches the lag, or cancel headroom is absent",
    action_profile="MAKER_FILTER",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_MAPPING",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN", "LATENCY_UNMEASURED", "FEE_UNKNOWN"),
)
_register(
    "C07-STALE-LEG",
    hypothesis=(
        "After a mapped leader reprices, a stale sibling leg can be traded and "
        "hedged for positive joint PnL."
    ),
    trigger="leader shock plus follower quote-age gap creates a payout-verified residual above all costs",
    direction_semantics="take the stale leg and execute the exact registered hedge vector",
    decision_clock=_RECEIVE,
    forecast_horizon="stale-leg entry through hedge completion, unwind, or terminal",
    falsifier="joint fill, latency, fee, or orphan accounting makes residual non-positive",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("MULTI_LEG_ENGINE_MISSING", "PAYOUT_EXHAUSTIVENESS_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "C08-COMBO-VS-LEG-COST",
    hypothesis=(
        "A verified MVE or combo package and its complete leg replication differ "
        "by more than all execution, fee, and orphan costs."
    ),
    trigger="package-versus-legs executable residual exceeds the registered joint safety buffer",
    direction_semantics="buy the cheaper complete payout and hedge with the more expensive complete replication",
    decision_clock=_RECEIVE,
    forecast_horizon="package and leg completion through unwind or terminal",
    falsifier="no residual survives exact package semantics, synchronized depth, and partial-leg stress",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("VENUE_PERMISSION_MISSING", "MULTI_LEG_ENGINE_MISSING", "PAYOUT_EXHAUSTIVENESS_MISSING"),
)
_register(
    "C09-PAYOUT-STATE-DOMINANCE",
    hypothesis=(
        "One verified contract portfolio statewise dominates another while "
        "costing less by more than joint execution costs."
    ),
    trigger="machine-audited payout matrix proves dominance and executable price difference exceeds costs",
    direction_semantics="buy the dominating portfolio and hedge or sell only through a legally supported complete vector",
    decision_clock=_RECEIVE,
    forecast_horizon="joint execution, residual unwind, or all legal terminal states",
    falsifier="any legal payout state breaks dominance or joint PnL is non-positive",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "INSTRUMENT_MAPPING_UNKNOWN"),
)
_register(
    "C10-MULTIDAY-SERIES",
    hypothesis=(
        "Matched contracts across dates have a term-structure residual that can "
        "be hedged without assuming unlike economic exposure is equivalent."
    ),
    trigger="audited exposure-equivalent dates show an executable residual above fees and capital cost",
    direction_semantics="buy the cheap maturity and hedge the exact mapped exposure in the rich maturity",
    decision_clock=_RECEIVE,
    forecast_horizon="two-leg completion through roll, unwind, or both terminals",
    falsifier="exposure mapping, carry, capital, or serial-leg risk removes the residual",
    action_profile="RELATIVE_VALUE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN", "MULTI_LEG_ENGINE_MISSING", "FEE_UNKNOWN"),
)


# D — RFQ and MVE.  Public RFQ data is always unsigned.
_register(
    "D01-RFQ-FLOW-CENSUS",
    hypothesis=(
        "RFQ arrival intensity varies enough by market and time to define "
        "capacity and risk strata, but arrival alone supplies no trading side."
    ),
    trigger="fresh exact-version rfq_created or rfq_deleted event enters a registered stratum",
    direction_semantics="unsigned census only; no order",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="same RFQ lifecycle and UTC-day count",
    falsifier="eligible fresh RFQ coverage is absent or intensity has no stable strata",
    action_profile="NO_ACTION",
    data_profile="RFQ_PUBLIC",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED",),
)
_register(
    "D02-RFQ-SIZE-INTENT",
    hypothesis=(
        "RFQ contracts or target-cost size predicts unsigned CLOB toxicity "
        "strongly enough for a size-conditioned quote guard."
    ),
    trigger="public RFQ size bucket exceeds the frozen absolute-risk threshold",
    direction_semantics="size is not buy or sell intent; cancel or widen both related CLOB sides",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="RFQ creation through 100ms, 1s, 5s, and deletion",
    falsifier="size does not predict absolute move, spread, or depth loss, or guard PnL fails",
    action_profile="RFQ_GUARD",
    data_profile="RFQ_PUBLIC",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED", "FILL_MODEL_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "D03-RFQ-LIFECYCLE-SURVIVAL",
    hypothesis=(
        "RFQ lifetime and deletion survival identify how long a CLOB maker "
        "should remain in an unsigned risk-off state."
    ),
    trigger="RFQ survival state enters a frozen high-risk lifetime bucket",
    direction_semantics="lifecycle is unsigned; guard both related CLOB sides",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="creation through deletion plus frozen recovery dwell",
    falsifier="lifetime does not condition CLOB risk or a fixed guard window performs as well",
    action_profile="RFQ_GUARD",
    data_profile="RFQ_PUBLIC",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED", "FILL_MODEL_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "D04-RFQ-COMBO-DEMAND-LEG-PRESSURE",
    hypothesis=(
        "A combo RFQ may predict absolute pressure in its listed legs, but its "
        "combo side never reveals the requester's transaction direction."
    ),
    trigger="public combo RFQ with an auditable leg map is received",
    direction_semantics="UNSIGNED_ONLY; combo side describes contract composition, not RFQ buy or sell direction",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="RFQ creation through each leg's 100ms, 1s, and 5s absolute response",
    falsifier="listed legs show no absolute pressure beyond matched non-combo RFQs",
    action_profile="NO_ACTION",
    data_profile="RFQ_PUBLIC",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED", "INSTRUMENT_MAPPING_UNKNOWN"),
)
_register(
    "D05-RFQ-REQUESTER-HASH",
    hypothesis=(
        "A stable non-identifying requester hash can segment subsequent unsigned "
        "toxicity enough to improve an RFQ CLOB guard."
    ),
    trigger="requester hash has sufficient past support and exceeds its frozen absolute-impact risk score",
    direction_semantics="requester strata never infer identity or side; high risk guards both sides",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="creation through deletion and 5s post-RFQ response",
    falsifier="out-of-fold requester strata are unstable or add no guard PnL",
    action_profile="RFQ_GUARD",
    data_profile="RFQ_PUBLIC",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED", "SAMPLE_TOO_SMALL", "FILL_MODEL_MISSING"),
)
_register(
    "D06-RFQ-DIRECTION-VOL-SIGNAL",
    hypothesis=(
        "Only a private accepted RFQ side can support a signed RFQ-to-CLOB "
        "return test; public create/delete and combo composition cannot."
    ),
    trigger="private quote_accepted event with exact accepted_side and correlated RFQ is received",
    direction_semantics=(
        "PUBLIC_UNSIGNED; PRIVATE accepted_side is authoritative for signed "
        "analysis; combo side and public RFQ fields are forbidden substitutes"
    ),
    decision_clock=_PRIVATE_RFQ,
    forecast_horizon="acceptance through confirmation, hedge, 100ms, 1s, and 5s",
    falsifier="accepted-side signed return is absent after latency or cannot cover hedge costs",
    action_profile="NO_ACTION",
    data_profile="RFQ_PRIVATE",
    target_class="SIGNAL",
    result_status="BLOCKED_DATA_AND_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PRIVATE_EVENTS_MISSING", "VENUE_PERMISSION_MISSING", "RFQ_PUBLIC_DATA_UNRELEASED"),
)
_register(
    "D07-RFQ-TO-CLOB-IMPACT",
    hypothesis=(
        "A public RFQ creation event predicts unsigned short-horizon CLOB "
        "toxicity early enough for cancellation or widening to reduce maker loss."
    ),
    trigger="fresh mapped RFQ arrives while a related CLOB maker quote is resting",
    direction_semantics="guard both sides unless a separate private-direction card is authorized",
    decision_clock=_RFQ_RECEIVE,
    forecast_horizon="RFQ receive through cancel-effective time, 100ms, 1s, and 5s",
    falsifier="RFQ impact lacks cancel headroom or the identical unguarded maker has equal or higher PnL",
    action_profile="RFQ_GUARD",
    data_profile="RFQ_PUBLIC",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PUBLIC_DATA_UNRELEASED", "FILL_MODEL_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "D08-RFQ-DIRECT-QUOTE-PNL",
    hypothesis=(
        "A fully collateralized two-sided RFQ quote can earn positive PnL after "
        "selection, confirmation, CLOB hedge, fees, and residual inventory."
    ),
    trigger="full RFQ size is hedgeable and both bounded yes_bid and no_bid satisfy frozen net-edge limits",
    direction_semantics="quote both sides, then use only private accepted_side to confirm and hedge",
    decision_clock=_PRIVATE_RFQ,
    forecast_horizon="quote submission through acceptance, confirmation, fills, hedge, and terminal residual",
    falsifier="acceptance-weighted RFQ plus hedge PnL is non-positive or tail inventory breaches limits",
    action_profile="RFQ_MAKER",
    data_profile="RFQ_PRIVATE",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA_AND_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=("RFQ_PRIVATE_EVENTS_MISSING", "VENUE_PERMISSION_MISSING", "FEE_UNKNOWN"),
)
_register(
    "D09-MVE-CATALOG-SCHEMA",
    hypothesis=(
        "MVE catalog records can reconstruct every leg, orientation, and payout "
        "state without heuristic inference."
    ),
    trigger="an admitted MVE catalog object is available under exact version",
    direction_semantics="schema and coverage only; no order",
    decision_clock=_RECEIVE,
    forecast_horizon="catalog-version validity interval",
    falsifier="any leg, orientation, version, or payout state remains ambiguous",
    action_profile="NO_ACTION",
    data_profile="CATALOG",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_MAPPING",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN",),
)
_register(
    "D10-MVE-DIRECT-TRADING",
    hypothesis=(
        "An MVE package can be directly traded and hedged at positive joint PnL "
        "after private fills, fees, and partial-package risk."
    ),
    trigger="payout-verified package residual exceeds all package, leg, hedge, and tail costs",
    direction_semantics="execute the exact package orientation and complete registered hedge vector",
    decision_clock="PRIVATE_MVE_EVENT_RECEIVE_CLOCK_PAST_ONLY",
    forecast_horizon="package entry through hedge completion, unwind, or joint terminal",
    falsifier="private execution, fees, or partial-package stress makes joint PnL non-positive",
    action_profile="MVE_DIRECT",
    data_profile="MVE_PRIVATE",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA_AND_AUTHORITY",
    claim_tier="NO_CLAIM",
    extra_blockers=(
        "MVE_PRIVATE_EVENTS_MISSING",
        "VENUE_PERMISSION_MISSING",
        "MULTI_LEG_ENGINE_MISSING",
        "PAYOUT_EXHAUSTIVENESS_MISSING",
        "INSTRUMENT_MAPPING_UNKNOWN",
    ),
)


# E — terminal, settlement, and lifecycle behavior.
_register(
    "E01-SETTLEMENT-CONVERGENCE",
    hypothesis=(
        "Executable prices converge toward later actual settlement value as "
        "point-in-time venue lifecycle states approach terminal resolution."
    ),
    trigger=(
        "point-in-time venue lifecycle snapshots can be joined to a later exact "
        "settlement label without exposing that label at the snapshot clock"
    ),
    direction_semantics=(
        "retrospective convergence control only; the later settlement label "
        "never triggers an order"
    ),
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="actual settlement, void, retirement, cancellation, or postponement resolution",
    falsifier="price error does not shrink with lifecycle proximity or legal-state mapping is incomplete",
    action_profile="CONTROL_ONLY",
    data_profile="TERMINAL",
    target_class="SIGNAL",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING",),
)
_register(
    "E02-EXPIRY-LIQUIDITY-MIGRATION",
    hypothesis=(
        "Rolling an exposure from a deteriorating expiry into an audited "
        "equivalent contract reduces execution and capital cost."
    ),
    trigger="old-contract liquidity deteriorates while an exposure-equivalent mapped contract passes the roll-cost gate",
    direction_semantics="close the exact old exposure and open only the audited equivalent new exposure",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="two-leg roll through reconciliation or both terminal states",
    falsifier="mapping, two-leg fees, orphan risk, or carry makes the roll worse than holding",
    action_profile="EXPIRY_ROLL",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("INSTRUMENT_MAPPING_UNKNOWN", "MULTI_LEG_ENGINE_MISSING", "SETTLEMENT_MISSING"),
)
_register(
    "E03-FREEZE-WINDOW",
    hypothesis=(
        "Observable pre-freeze liquidity deterioration permits an earlier "
        "reduce-only exit that avoids more loss than it costs."
    ),
    trigger="time-to-freeze and update/depth precursor enter the frozen exit-risk cell",
    direction_semantics="cancel all risk-adding orders and reduce only the authoritative position",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="precursor through freeze, executable exit, or terminal",
    falsifier="precursor does not lead freeze or early exit underperforms waiting under identical inventory",
    action_profile="EARLY_EXIT",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "PRIVATE_ACCOUNT_STATE_MISSING", "LATENCY_UNMEASURED"),
)
_register(
    "E04-SETTLEMENT-CALIBRATION",
    hypothesis=(
        "Inside a preregistered pre-freeze near-terminal lifecycle window, "
        "exception-aware settlement frequency has a stable executable "
        "calibration bias relative to market price."
    ),
    trigger=(
        "inside E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW, out-of-fold lifecycle- and "
        "exception-aware terminal probability differs from executable price "
        "by more than exact cost and uncertainty"
    ),
    direction_semantics="buy YES or NO according to the signed calibrated probability gap",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="actual terminal settlement",
    falsifier="bias vanishes on untouched dates or fill-conditioned terminal PnL is non-positive",
    action_profile="TERMINAL_ENTRY",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "FILL_MODEL_MISSING", "FEE_UNKNOWN"),
)
_register(
    "E05-TERMINAL-EXCEPTION-STATES",
    hypothesis=(
        "Void, retirement, postponement, cancellation, and other exception "
        "states materially change candidate-strategy tail economics."
    ),
    trigger="a versioned lifecycle path enters a non-standard legal terminal state",
    direction_semantics="diagnostic enumeration only; this base experiment submits no order",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="exception declaration through final cash-flow reconciliation",
    falsifier="exception frequency and cash-flow effect are immaterial under every owner policy",
    action_profile="NO_ACTION",
    data_profile="TERMINAL",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING",),
)
_register(
    "E06-HOLD-TO-TERMINAL-TAIL",
    hypothesis=(
        "For a specified strict fill, holding to terminal has better paired PnL "
        "than the frozen early exit without unacceptable tail loss."
    ),
    trigger="existing reconciled position enters a terminal-authorized hold cell with complete lifecycle facts",
    direction_semantics="hold only the existing position; never convert this decision into a new opening trade",
    decision_clock=_TERMINAL_CLOCK,
    forecast_horizon="actual terminal versus paired frozen executable exit",
    falsifier="paired hold increment is non-positive or terminal expected shortfall breaches the frozen limit",
    action_profile="TERMINAL_POLICY",
    data_profile="TERMINAL",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_TERMINAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("SETTLEMENT_MISSING", "PRIVATE_ACCOUNT_STATE_MISSING", "FEE_UNKNOWN"),
)


# F — ecology, selection, sport-specific data, and external sources.
_register(
    "F01-PARTICIPANT-MIX-PROXY",
    hypothesis=(
        "Non-identifying flow and occupancy proxies improve a maker risk filter "
        "without claiming to identify participant types."
    ),
    trigger="past-only proxy score enters a frozen high-toxicity or benign-flow cell",
    direction_semantics="proxy can cancel or admit an owner quote but never infers trader identity or side by itself",
    decision_clock=_RECEIVE,
    forecast_horizon="proxy event through child fill, cancel, and 5s markout",
    falsifier="proxy adds no out-of-fold child PnL beyond volume, spread, and activity",
    action_profile="MAKER_FILTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="MECHANISM_ONLY",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "F02-FEE-STRUCTURE-EFFECT",
    hypothesis=(
        "Exact series, date, role, and account fee facts change whether a child "
        "should make, take, or abstain."
    ),
    trigger="versioned exact fee lookup changes the frozen child's marginal net edge sign",
    direction_semantics="fee chooses action eligibility and order role, not a market direction",
    decision_clock=_RECEIVE,
    forecast_horizon="each intended fill through order-level fee accumulation and exit",
    falsifier="fee-aware routing has no incremental PnL or fee facts cannot reconcile to account charges",
    action_profile="ROUTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("FEE_UNKNOWN", "CONFIRMED_CHILD_STRATEGIES_MISSING"),
)
_register(
    "F03-NEW-MARKET-COLD-START",
    hypothesis=(
        "Waiting for a new market to pass topology, depth, and activity readiness "
        "gates improves maker PnL over quoting immediately after listing."
    ),
    trigger="market age plus two-sided uptime, depth, and activity pass the frozen readiness gate",
    direction_semantics="start the same two-sided maker only after readiness; listing age supplies no side",
    decision_clock=_RECEIVE,
    forecast_horizon="listing through readiness, fills, and UTC-day close",
    falsifier="delayed start does not beat immediate quoting or eliminates nearly all capacity",
    action_profile="MAKER_SPREAD",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "F04-CROSS-SECTIONAL-RANK",
    hypothesis=(
        "Ranking comparable markets by past-only expected net edge allocates a "
        "fixed child action better than equal-weight or all-eligible routing."
    ),
    trigger="market enters the frozen top rank after root, liquidity, and phase matching",
    direction_semantics="rank selects a confirmed child and inherits its side unchanged",
    decision_clock=_RECEIVE,
    forecast_horizon="ranking rebalance through child root-event and UTC-day PnL",
    falsifier="ranked child PnL does not beat equal-weight and best simple selector out of sample",
    action_profile="ROUTER",
    data_profile="L1",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "F05-SCANNER-SPREAD-FLOW-DEPTH",
    hypothesis=(
        "A spread, flow, and depth scanner selects child opportunities with "
        "higher fee-after PnL than any one scanner input alone."
    ),
    trigger="frozen composite scanner score passes the opportunity and data-quality gates",
    direction_semantics="scanner selects only; the registered child supplies side and order semantics",
    decision_clock=_RECEIVE,
    forecast_horizon="trigger through child fill, exit, and day result",
    falsifier="composite selector fails to beat spread-only, flow-only, and depth-only baselines",
    action_profile="ROUTER",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "F06-NICHE-COMPOSITE",
    hypothesis=(
        "A narrow sport, structure, phase, and liquidity niche retains "
        "incremental child PnL after matched controls and concentration tests."
    ),
    trigger="all frozen niche dimensions match and the selected child is authorized",
    direction_semantics="niche selects the child but does not alter its side",
    decision_clock=_RECEIVE,
    forecast_horizon="niche episode through child exit and day-level concentration",
    falsifier="delete-best event, league, or day removes the increment or support is insufficient",
    action_profile="ROUTER",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "SAMPLE_TOO_SMALL"),
)
_register(
    "F07-LOW-OCC-INDEPENDENT-FAIR",
    hypothesis=(
        "A low-occupancy market with a genuinely independent stable fair value "
        "supports a one-contract maker after adverse-selection costs."
    ),
    trigger="occupancy is below threshold and an audited independent fair plus spread gate passes",
    direction_semantics="quote around the independent fair; missing or circular fair means no order",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime through fill, cancel, and bounded exit",
    falsifier="empty books produce no fills, informed fills dominate, or the fair is not independent",
    action_profile="MAKER_SPREAD",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("MISSING_SIGNAL_INPUT", "FILL_MODEL_MISSING", "FEE_UNKNOWN"),
)
_register(
    "F08-SPORT-FACTORY",
    hypothesis=(
        "Mechanism effects differ across captured sports, so each sport must "
        "retain its own support and falsifier instead of being pooled away."
    ),
    trigger="a captured sport and atomic owner pair emits a support or zero-support row",
    direction_semantics="factory only; no order and no side",
    decision_clock=_RECEIVE,
    forecast_horizon="owner-defined horizon with sport-stratified reporting",
    falsifier="sport interaction is unsupported and the pooled mechanism is stable under leave-one-sport tests",
    action_profile="NO_ACTION",
    data_profile="CATALOG",
    target_class="RESEARCH_QUESTION",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="DESCRIPTIVE_ONLY",
)
_register(
    "F09-TENNIS-BO3-BO5",
    hypothesis=(
        "Authoritative best-of-three versus best-of-five format changes Tennis "
        "state transitions or a bound child policy's PnL."
    ),
    trigger="point-in-time authoritative match-format label joins a Tennis market before decision",
    direction_semantics="format selects a child stratum only; it never supplies side",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="match-format stratum through owner-defined exit or terminal",
    falsifier="matched BO3 and BO5 effects are equal or format support is too small",
    action_profile="NO_ACTION",
    data_profile="EXTERNAL",
    external_dependency="REQUIRED",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_EXTERNAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("EXTERNAL_SPORTS_DATA_MISSING", "EXTERNAL_API_UNVERIFIED"),
)
_register(
    "F10-BASEBALL-ONE-SIDE",
    hypothesis=(
        "Baseball one-sided-book episodes admit a missing-side passive quote "
        "with positive fee-after PnL without inferring inning or pitch state."
    ),
    trigger="authoritative Baseball identity plus persistent bid-only or ask-only state passes gates",
    direction_semantics="post the explicitly missing book side; no inning, pitch, or score is inferred",
    decision_clock=_RECEIVE,
    forecast_horizon="one-sided dwell through fill, recovery, or forced exit",
    falsifier="strict missing-side fills are absent or their adverse loss exceeds spread capture",
    action_profile="PASSIVE_FADE",
    data_profile="L1_L2",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="PNL_NOT_ESTIMATED",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "F11-GOLF-THIN-LONG-LIFECYCLE",
    hypothesis=(
        "Observable Golf market thinness and long lifecycle identify an internal "
        "one-contract passive window that survives fill, capital, and terminal risk."
    ),
    trigger="authoritative Golf identity plus catalog age and a frozen thin-book spread/liquidity gate",
    direction_semantics="quote the registered two-sided book without inferring round, cut, score, or field state",
    decision_clock=_RECEIVE,
    forecast_horizon="quote lifetime through executable exit or tournament terminal",
    falsifier="capital duration, no-fill rate, or terminal tail erases spread economics",
    action_profile="MAKER_SPREAD",
    data_profile="L1",
    external_dependency="OPTIONAL_REFINEMENT",
    target_class="EXECUTABLE_STRATEGY",
    result_status="DESCRIPTIVE_ONLY",
    claim_tier="NO_CLAIM",
    extra_blockers=("FILL_MODEL_MISSING", "FEE_UNKNOWN", "SETTLEMENT_MISSING"),
)
_register(
    "F12-EXTERNAL-ODDS-LEADLAG",
    hypothesis=(
        "A licensed external-odds update leads the mapped Kalshi executable "
        "price by more than provider latency and total trading cost."
    ),
    trigger="verified external fair-value update creates a mapped executable discrepancy above cost",
    direction_semantics="buy the Kalshi outcome underpriced relative to the audited external mapping",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="provider publication through Kalshi repricing or frozen exit",
    falsifier="lead disappears under publication/receive clocks or net PnL is non-positive",
    action_profile="EXTERNAL_ACTION",
    data_profile="EXTERNAL",
    external_dependency="REQUIRED",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_EXTERNAL",
    claim_tier="NO_CLAIM",
    extra_blockers=(
        "EXTERNAL_SPORTS_DATA_MISSING",
        "EXTERNAL_API_UNVERIFIED",
        "TIMESTAMP_SEMANTICS_UNKNOWN",
    ),
)
_register(
    "F13-EXTERNAL-SPORT-STATE",
    hypothesis=(
        "A licensed score, serve, injury, lineup, or official state change leads "
        "a mapped Kalshi repricing enough for cancellation or trade."
    ),
    trigger="verified point-in-time sport-state event changes the audited contract fair value above cost",
    direction_semantics="derive side from the explicit state-to-payout mapping; missing fields never impute direction",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="source publication through venue reaction, stop, or terminal",
    falsifier="state event has no latency-adjusted executable effect or data timing is unverifiable",
    action_profile="EXTERNAL_ACTION",
    data_profile="EXTERNAL",
    external_dependency="REQUIRED",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_EXTERNAL",
    claim_tier="NO_CLAIM",
    extra_blockers=(
        "EXTERNAL_SPORTS_DATA_MISSING",
        "EXTERNAL_API_UNVERIFIED",
        "TIMESTAMP_SEMANTICS_UNKNOWN",
    ),
)
_register(
    "F14-PROVIDER-PRICE-LATENCY-TIER",
    hypothesis=(
        "Provider usefulness depends on effective point-in-time coverage, "
        "latency, reliability, license, and cost rather than nominal feed speed."
    ),
    trigger="two or more verified provider streams expose comparable publication and receive timestamps",
    direction_semantics="provider-selection diagnostic only; it submits no order",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="publication-to-receive latency distribution and daily coverage",
    falsifier="no provider has positive value after latency, missingness, and commercial cost",
    action_profile="NO_ACTION",
    data_profile="EXTERNAL",
    external_dependency="REQUIRED",
    target_class="RESEARCH_QUESTION",
    result_status="BLOCKED_EXTERNAL",
    claim_tier="NO_CLAIM",
    extra_blockers=("EXTERNAL_API_UNVERIFIED", "TIMESTAMP_SEMANTICS_UNKNOWN"),
)
_register(
    "F15-BLINDNESS-EVPI",
    hypothesis=(
        "A non-causal future-information oracle on the same internal L1/L2 "
        "paths bounds how much loss an ideal external signal could avoid."
    ),
    trigger="an internal blind owner decision and its later same-path L1/L2 outcome can be paired with the frozen oracle rule",
    direction_semantics="future-information upper-bound control only; the oracle is never a live signal or order action",
    decision_clock=_RECEIVE,
    forecast_horizon="owner decision through its internal future-path outcome and avoided-loss ceiling",
    falsifier="oracle-minus-blind upper bound is below any plausible provider and implementation cost",
    action_profile="CONTROL_ONLY",
    data_profile="OWNER_EVPI",
    target_class="RESEARCH_QUESTION",
    result_status="CONTROL_ONLY",
    claim_tier="CONTROL_ONLY",
)
_register(
    "F16-COURTSIDE-SPEED-OUT-OF-SCOPE",
    hypothesis=(
        "Courtside-speed information would require separate legal, licensing, "
        "network, and action authority and is excluded from this program."
    ),
    trigger="none in the current program",
    direction_semantics="no order",
    decision_clock=_EXTERNAL_CLOCK,
    forecast_horizon="not applicable",
    falsifier="not evaluated unless a new durable program explicitly admits the source and action",
    action_profile="NO_ACTION",
    data_profile="OUT_OF_SCOPE",
    external_dependency="REQUIRED",
    target_class="RESEARCH_QUESTION",
    result_status="OUT_OF_SCOPE",
    claim_tier="NO_CLAIM",
    extra_blockers=("OUT_OF_SCOPE_CURRENT_PROGRAM", "EXTERNAL_SPORTS_DATA_MISSING"),
)
_register(
    "F17-CRYPTO-SPOT-ANCHOR-OUT-OF-SCOPE",
    hypothesis=(
        "Crypto spot anchoring has no admitted mechanism inside the Sports "
        "program and belongs to a separate registry."
    ),
    trigger="none in the Sports program",
    direction_semantics="no order",
    decision_clock=_RECEIVE,
    forecast_horizon="not applicable",
    falsifier="a separately authorized non-Sports program establishes an explicit mapped instrument",
    action_profile="NO_ACTION",
    data_profile="OUT_OF_SCOPE",
    target_class="RESEARCH_QUESTION",
    result_status="OUT_OF_SCOPE",
    claim_tier="NO_CLAIM",
    extra_blockers=("OUT_OF_SCOPE_CURRENT_PROGRAM",),
)


# G — model methods.  Each base method is a signal component, never a strategy.
def _register_model(
    experiment_id: str,
    *,
    hypothesis: str,
    trigger: str,
    forecast_horizon: str,
    falsifier: str,
) -> None:
    _register(
        experiment_id,
        hypothesis=hypothesis,
        trigger=trigger,
        direction_semantics=(
            "inherits the exact owner label and orientation; the model method "
            "cannot invent a side, fill, action, or missing sports state"
        ),
        decision_clock=_RECEIVE,
        forecast_horizon=forecast_horizon,
        falsifier=falsifier,
        action_profile="MODEL_COMPONENT",
        data_profile="MODEL",
        external_dependency="INHERIT_OWNER",
        target_class="SIGNAL",
        result_status="BLOCKED_OWNER",
        claim_tier="MODEL_COMPONENT_ONLY",
        extra_blockers=("ACTION_INCOMPLETE", "INSUFFICIENT_QUALITY_DATES"),
    )


_register_model(
    "G01-CHANGEPOINT-REGIME",
    hypothesis="A past-only change-point detector improves an owner's regime forecast over a fixed rolling window.",
    trigger="online change probability crosses the owner-frozen regime boundary",
    forecast_horizon="owner horizon from detected break through next stable segment",
    falsifier="chronological owner loss and eventual fee-after PnL do not beat the fixed-window baseline",
)
_register_model(
    "G02-HMM-HSMM-STATE",
    hypothesis="Filtered HMM or HSMM state and dwell improve an owner outcome over deterministic SNBD state.",
    trigger="filtered, never smoothed, state posterior crosses the owner-frozen threshold",
    forecast_horizon="owner horizon plus latent-state dwell",
    falsifier="latent states are unstable or add no calibrated owner increment out of sample",
)
_register_model(
    "G03-POINT-PROCESS-HAWKES",
    hypothesis="History-only point-process intensity improves an owner's fill, jump, or flow hazard forecast.",
    trigger="causal self- or cross-excitation intensity exceeds the owner-frozen hazard threshold",
    forecast_horizon="next event time and owner hazard horizon",
    falsifier="Poisson or empirical hazard matches the model chronologically or owner economics do not improve",
)
_register_model(
    "G04-GARCH-STOCHASTIC-VOL",
    hypothesis="Conditional GARCH or stochastic-volatility forecasts improve an owner's risk or radius decision.",
    trigger="one-step conditional volatility enters a different owner-frozen action cell",
    forecast_horizon="next 1s, 30s, 5m, or owner-defined quote horizon",
    falsifier="rolling realized volatility forecasts as well or the owner action gains no PnL",
)
_register_model(
    "G05-STATE-SPACE-KALMAN",
    hypothesis="A filtered state-space fair value and uncertainty improve an owner action over midpoint or EWMA.",
    trigger="filtered fair displacement and posterior uncertainty pass the owner gate",
    forecast_horizon="owner fair-value and exit horizon",
    falsifier="midpoint, microprice, or EWMA matches forecast and owner PnL",
)
_register_model(
    "G06-PCA-FACTOR-CLUSTER",
    hypothesis="Past-fit common factors expose owner residuals that survive matched cross-market controls.",
    trigger="factor-neutral residual exceeds the owner-frozen threshold in a mapped comparable panel",
    forecast_horizon="owner convergence or risk horizon",
    falsifier="residual vanishes under rolling fit, leave-root-out tests, or joint execution costs",
)
_register_model(
    "G07-COINTEGRATION-VECM",
    hypothesis="A past-fit long-run relation produces a stable error-correction signal for an owner relative-value card.",
    trigger="causal error-correction residual exceeds the owner-frozen entry boundary",
    forecast_horizon="owner convergence, stop, or joint terminal horizon",
    falsifier="relation breaks chronologically or executable joint PnL is non-positive",
)
_register_model(
    "G08-REGULARIZED-GLM-SURVIVAL",
    hypothesis="A regularized GLM or survival model adds calibrated owner prediction beyond a univariate hazard.",
    trigger="out-of-fold probability or hazard crosses the owner-frozen decision threshold",
    forecast_horizon="owner binary, return, fill, or survival horizon",
    falsifier="simple empirical or univariate baseline matches calibration and owner economics",
)
_register_model(
    "G09-TREE-BOOSTING",
    hypothesis="Tree boosting captures stable nonlinear owner interactions beyond a regularized GLM.",
    trigger="chronological out-of-fold tree score crosses the owner-frozen threshold",
    forecast_horizon="owner outcome horizon",
    falsifier="nested out-of-fold increment, calibration, or eventual owner PnL is absent",
)
_register_model(
    "G10-NEURAL-SEQUENCE",
    hypothesis="A causal neural sequence model adds owner value beyond tree and GLM baselines after latency and drift costs.",
    trigger="causal sequence score crosses the owner-frozen threshold within its latency budget",
    forecast_horizon="owner sequence outcome horizon",
    falsifier="simpler models match performance or inference, drift, and execution costs erase the increment",
)
_register_model(
    "G11-ENSEMBLE-STACKING",
    hypothesis="An out-of-fold-only stack improves owner calibration and PnL over the best frozen single model.",
    trigger="meta-score built only from out-of-fold base predictions crosses the owner gate",
    forecast_horizon="owner outcome and action horizon",
    falsifier="the best single model matches the stack or switching and compute costs erase the increment",
)
_register_model(
    "G12-DRIFT-CALIBRATION-RETRAINING",
    hypothesis="A frozen drift detector and retraining cadence improve live-like owner calibration and PnL.",
    trigger="calibration or feature drift crosses the frozen retrain or abstention boundary",
    forecast_horizon="walk-forward owner horizon through the next retraining checkpoint",
    falsifier="never-retrain or fixed-cadence baseline matches performance after retraining costs",
)


# P — portfolio, routing, risk, capacity, and commercial operation.
_register(
    "P01-STRATEGY-OVERLAP-PRECEDENCE",
    hypothesis=(
        "Frozen precedence among overlapping confirmed strategies prevents "
        "duplicate or contradictory root risk and improves joint PnL."
    ),
    trigger="two confirmed child intents overlap on the same root, market, or outcome exposure",
    direction_semantics="select one precedence winner, cancel conflicts, reach zero resting, then route its unchanged side",
    decision_clock=_RECEIVE,
    forecast_horizon="conflict through cancellation, reconciliation, child exit, and day PnL",
    falsifier="joint precedence PnL does not beat independently running children under shared risk",
    action_profile="ROUTER",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P02-ROUTER-ABSTENTION-SWITCH",
    hypothesis=(
        "A frozen router with abstention and hysteresis outperforms the best "
        "fixed confirmed child after switch costs."
    ),
    trigger="past-only regime score selects a different child beyond the frozen hysteresis boundary",
    direction_semantics="no new risk, cancel, zero resting, reconcile, then inherit the selected child's side",
    decision_clock=_RECEIVE,
    forecast_horizon="router state through switch completion and child root-event outcome",
    falsifier="router fails to beat the best fixed training-selected child on untouched evidence",
    action_profile="ROUTER",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P03-COMMON-ROOT-COVARIANCE-COTAIL",
    hypothesis=(
        "Aggregating exposure by canonical root and enforcing a co-tail cap "
        "reduces portfolio tail loss without destroying expected PnL."
    ),
    trigger="root exposure, covariance, or co-tail stress exceeds a frozen joint risk limit",
    direction_semantics="deny new risk and preserve reduce-only actions across every affected child",
    decision_clock=_RECEIVE,
    forecast_horizon="risk breach through joint unwind, terminal, and daily tail result",
    falsifier="joint cap does not reduce expected shortfall or sacrifices more PnL than the frozen tolerance",
    action_profile="SIZE_RISK",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P04-MULTIMARKET-ALLOCATION",
    hypothesis=(
        "Allocating fixed risk units by marginal net edge, covariance, capital, "
        "and token use beats equal allocation."
    ),
    trigger="confirmed-child scores and joint risk ledger produce a frozen non-equal allocation",
    direction_semantics="allocation changes size only and inherits every child's side unchanged",
    decision_clock=_DAY_CLOCK,
    forecast_horizon="allocation interval through root-deduped day PnL and capital peak",
    falsifier="allocation fails to beat equal weight and best single child after turnover and costs",
    action_profile="SIZE_RISK",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P05-BRACKET-HEDGE",
    hypothesis=(
        "A payout-verified bracket hedge reduces a confirmed child's tail or "
        "capital use by more than all-leg execution cost."
    ),
    trigger="authoritative child position plus bracket hedge residual passes the frozen benefit threshold",
    direction_semantics="execute the exact statewise hedge vector; never use a name-only bracket match",
    decision_clock=_RECEIVE,
    forecast_horizon="hedge entry through child exit, orphan unwind, or joint terminal",
    falsifier="joint fill costs or any legal payout state makes the hedge increment non-positive",
    action_profile="CHILD_HEDGE",
    data_profile="FAMILY",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_ENGINE",
    claim_tier="NO_CLAIM",
    extra_blockers=("PAYOUT_EXHAUSTIVENESS_MISSING", "MULTI_LEG_ENGINE_MISSING", "CONFIRMED_CHILD_STRATEGIES_MISSING"),
)
_register(
    "P06-DYNAMIC-DAILY-SELECTION",
    hypothesis=(
        "Selecting only past-predicted positive confirmed children each day "
        "outperforms static daily activation after turnover."
    ),
    trigger="prior-day-only quality, capacity, and edge score places a child on the daily allowlist",
    direction_semantics="daily selector inherits child actions and sides; excluded children submit nothing",
    decision_clock=_DAY_CLOCK,
    forecast_horizon="next UTC day with no intraday refit",
    falsifier="daily selector fails to beat always-on and best fixed child on untouched days",
    action_profile="ROUTER",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "INSUFFICIENT_QUALITY_DATES"),
)
_register(
    "P07-CAPACITY-FILL-DECAY-IMPACT",
    hypothesis=(
        "Fill probability decays and impact grows with size, producing a finite "
        "PnL-maximizing capacity rather than linear scale."
    ),
    trigger="marginal tested size has positive pessimistic PnL after fill decay and impact",
    direction_semantics="change quantity only and inherit the child side",
    decision_clock=_RECEIVE,
    forecast_horizon="order lifetime through fills, impact, unwind, and daily capacity",
    falsifier="no size above one has positive marginal PnL or capacity estimates fail micro-live calibration",
    action_profile="SIZE_RISK",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "OWN_ORDER_CALIBRATION_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P08-PROGRESSIVE-SCALING",
    hypothesis=(
        "A maximum two-times size ladder increases dollar PnL only while every "
        "tier retains positive marginal economics and tail safety."
    ),
    trigger="previous tier passes frozen PnL, drawdown, impact, and reconciliation gates",
    direction_semantics="raise or lower size only; inherit child side and immediately allow reduce-only exit",
    decision_clock=_DAY_CLOCK,
    forecast_horizon="one complete tier evaluation window before any next step",
    falsifier="a tier has non-positive marginal PnL, excess tail, or unmodeled impact",
    action_profile="SIZE_RISK",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING", "RISK_GATES_MISSING", "OWN_ORDER_CALIBRATION_MISSING"),
)
_register(
    "P09-MANUAL-SYSTEM-COEXISTENCE",
    hypothesis=(
        "Authoritative ownership, self-trade prevention, and precedence allow "
        "manual and system orders to coexist without conflict loss."
    ),
    trigger="private account state reports overlapping manual and system exposure or orders",
    direction_semantics="forbid system new risk, cancel only system-owned conflicts, and reconcile before resuming",
    decision_clock="PRIVATE_ACCOUNT_EVENT_RECEIVE_CLOCK_PAST_ONLY",
    forecast_horizon="conflict detection through cancel, reconciliation, and position normalization",
    falsifier="ownership cannot be proven or coexistence produces duplicate fills, self-trades, or unexplained exposure",
    action_profile="SIZE_RISK",
    data_profile="PRIVATE",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_DATA",
    claim_tier="NO_CLAIM",
    extra_blockers=("PRIVATE_ACCOUNT_STATE_MISSING", "RISK_GATES_MISSING"),
)
_register(
    "P10-COMMERCIAL-ROC-COST",
    hypothesis=(
        "A confirmed strategy remains commercially worthwhile after exchange, "
        "data, cloud, capital, labor, and incident costs."
    ),
    trigger="rolling commercial net PnL and return on committed capital pass frozen continue or stop gates",
    direction_semantics="deploy, continue, scale down, or stop a confirmed child; no independent market side",
    decision_clock=_DAY_CLOCK,
    forecast_horizon="daily, monthly, and capital-payback windows",
    falsifier="commercial net PnL or risk-adjusted return on cost is non-positive",
    action_profile="COMMERCIAL_ROC",
    data_profile="META",
    external_dependency="INHERIT_CHILDREN",
    target_class="EXECUTABLE_STRATEGY",
    result_status="BLOCKED_CHILDREN",
    claim_tier="NO_CLAIM",
    extra_blockers=("CONFIRMED_CHILD_STRATEGIES_MISSING",),
)
_register(
    "P11-UNCLASSIFIED-BACKLOG",
    hypothesis=(
        "Valid observations with no falsifiable action and PnL contract should "
        "remain unclassified rather than be promoted by narrative."
    ),
    trigger="valid evidence matches no atomic experiment with a complete target and action",
    direction_semantics="no order",
    decision_clock=_RECEIVE,
    forecast_horizon="not applicable until a new audited lineage exists",
    falsifier="an independently audited taxonomy proposal supplies a unique action, baseline, and PnL contract",
    action_profile="NO_ACTION",
    data_profile="INTERNAL_CONTROL",
    external_dependency="INHERIT_CHILDREN",
    target_class="RESEARCH_QUESTION",
    result_status="UNCLASSIFIED_BACKLOG",
    claim_tier="NO_CLAIM",
    extra_blockers=("ACTION_INCOMPLETE", "BASELINE_INCOMPLETE"),
)


_EXPECTED_FAMILY_COUNTS = {
    "A": 23,
    "B": 17,
    "C": 10,
    "D": 10,
    "E": 6,
    "F": 17,
    "G": 12,
    "P": 11,
}
_CARD_KEYS = {
    "hypothesis",
    "signal",
    "action_profile",
    "data_profile",
    "external_dependency",
    "target_class",
    "result_status",
    "claim_tier",
    "extra_blockers",
}
_SIGNAL_KEYS = {
    "trigger",
    "direction_semantics",
    "decision_clock",
    "forecast_horizon",
    "falsifier",
}
_ACTION_PROFILES = {
    "NO_ACTION",
    "MAKER_SPREAD",
    "MAKER_FILTER",
    "MAKER_REFILL",
    "T90_RANGE",
    "T90_REBUILD",
    "PASSIVE_FADE",
    "TAKER_DIRECTIONAL",
    "RELATIVE_VALUE",
    "EXPIRY_ROLL",
    "CHILD_HEDGE",
    "RFQ_GUARD",
    "RFQ_MAKER",
    "MVE_DIRECT",
    "TERMINAL_POLICY",
    "TERMINAL_ENTRY",
    "CS_ASK",
    "EARLY_EXIT",
    "EXTERNAL_ACTION",
    "ROUTER",
    "COMMERCIAL_ROC",
    "SIZE_RISK",
    "MODEL_COMPONENT",
    "CONTROL_ONLY",
}
_DATA_PROFILES = {
    "L1",
    "L2",
    "L1_L2",
    "L1_PRIVATE",
    "FAMILY",
    "TERMINAL",
    "RFQ_PUBLIC",
    "RFQ_PRIVATE",
    "MVE_PRIVATE",
    "EXTERNAL",
    "CATALOG",
    "MODEL",
    "META",
    "PRIVATE",
    "OUT_OF_SCOPE",
    "INTERNAL_CONTROL",
    "OWNER_EVPI",
}
_EXTERNAL_DEPENDENCIES = {
    "NOT_REQUIRED",
    "REQUIRED",
    "OPTIONAL_REFINEMENT",
    "INHERIT_OWNER",
    "INHERIT_CHILDREN",
}
_TARGET_CLASSES = {"RESEARCH_QUESTION", "SIGNAL", "EXECUTABLE_STRATEGY"}
_KNOWN_BLOCKERS = {
    "MISSING_SIGNAL_INPUT",
    "TIMESTAMP_SEMANTICS_UNKNOWN",
    "EXTERNAL_SPORTS_DATA_MISSING",
    "EXTERNAL_API_UNVERIFIED",
    "VENUE_PERMISSION_MISSING",
    "INSTRUMENT_MAPPING_UNKNOWN",
    "ACTION_INCOMPLETE",
    "BASELINE_INCOMPLETE",
    "FEE_UNKNOWN",
    "FILL_MODEL_MISSING",
    "OWN_ORDER_CALIBRATION_MISSING",
    "LATENCY_UNMEASURED",
    "SETTLEMENT_MISSING",
    "PAYOUT_EXHAUSTIVENESS_MISSING",
    "RFQ_PRIVATE_EVENTS_MISSING",
    "RFQ_PUBLIC_DATA_UNRELEASED",
    "MVE_PRIVATE_EVENTS_MISSING",
    "MULTI_LEG_ENGINE_MISSING",
    "CONFIRMED_CHILD_STRATEGIES_MISSING",
    "PRIVATE_ACCOUNT_STATE_MISSING",
    "INSUFFICIENT_QUALITY_DATES",
    "SAMPLE_TOO_SMALL",
    "DATA_INTEGRITY_BLOCKED",
    "RISK_GATES_MISSING",
    "OPERATING_COST_UNKNOWN",
    "OUT_OF_SCOPE_CURRENT_PROGRAM",
}


def _validate_seeds() -> None:
    if len(CARD_SEEDS) != 106:
        raise ValueError(f"expected exactly 106 card seeds, got {len(CARD_SEEDS)}")

    family_counts = {family: 0 for family in _EXPECTED_FAMILY_COUNTS}
    hypotheses: set[str] = set()
    triggers: set[str] = set()
    for experiment_id, card in CARD_SEEDS.items():
        family = experiment_id[0]
        if family not in family_counts:
            raise ValueError(f"unknown family for {experiment_id}")
        family_counts[family] += 1

        if set(card) != _CARD_KEYS:
            raise ValueError(f"{experiment_id}: wrong card keys {set(card) ^ _CARD_KEYS}")
        if set(card["signal"]) != _SIGNAL_KEYS:
            raise ValueError(
                f"{experiment_id}: wrong signal keys "
                f"{set(card['signal']) ^ _SIGNAL_KEYS}"
            )
        if card["action_profile"] not in _ACTION_PROFILES:
            raise ValueError(f"{experiment_id}: unknown action profile")
        if card["data_profile"] not in _DATA_PROFILES:
            raise ValueError(f"{experiment_id}: unknown data profile")
        if card["external_dependency"] not in _EXTERNAL_DEPENDENCIES:
            raise ValueError(f"{experiment_id}: unknown external dependency")
        if card["target_class"] not in _TARGET_CLASSES:
            raise ValueError(f"{experiment_id}: unknown target class")
        unknown_blockers = set(card["extra_blockers"]) - _KNOWN_BLOCKERS
        if unknown_blockers:
            raise ValueError(f"{experiment_id}: unknown blockers {unknown_blockers}")

        hypothesis = card["hypothesis"]
        trigger = card["signal"]["trigger"]
        if hypothesis in hypotheses:
            raise ValueError(f"{experiment_id}: duplicate hypothesis")
        if trigger in triggers:
            raise ValueError(f"{experiment_id}: duplicate signal trigger")
        hypotheses.add(hypothesis)
        triggers.add(trigger)

        if (
            card["action_profile"] in {"NO_ACTION", "MODEL_COMPONENT", "CONTROL_ONLY"}
            and card["target_class"] == "EXECUTABLE_STRATEGY"
        ):
            raise ValueError(f"{experiment_id}: non-action cannot be a strategy")
        if (
            card["external_dependency"] == "REQUIRED"
            and card["data_profile"] not in {"EXTERNAL", "OUT_OF_SCOPE"}
        ):
            raise ValueError(f"{experiment_id}: required external data not explicit")

    if family_counts != _EXPECTED_FAMILY_COUNTS:
        raise ValueError(
            f"family counts differ: expected {_EXPECTED_FAMILY_COUNTS}, "
            f"got {family_counts}"
        )

    d04_direction = CARD_SEEDS["D04-RFQ-COMBO-DEMAND-LEG-PRESSURE"]["signal"][
        "direction_semantics"
    ]
    d06_direction = CARD_SEEDS["D06-RFQ-DIRECTION-VOL-SIGNAL"]["signal"][
        "direction_semantics"
    ]
    if "combo side" not in d04_direction or "not RFQ buy or sell" not in d04_direction:
        raise ValueError("D04 must state that combo side is not RFQ direction")
    if (
        "PUBLIC_UNSIGNED" not in d06_direction
        or "PRIVATE accepted_side" not in d06_direction
        or "forbidden substitutes" not in d06_direction
    ):
        raise ValueError("D06 must fail closed on public RFQ direction")


_validate_seeds()

__all__ = ["CARD_SEEDS"]
