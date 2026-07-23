#!/usr/bin/env python3
"""Build and validate the DeepResearch monetizable experiment registry.

The registry deliberately separates a market observation from a strategy.
An entry is an ``EXECUTABLE_STRATEGY`` only when it has a falsifiable signal,
a deterministic action, an execution venue, an executable baseline, and a
fee-after pathwise PnL contract.  Missing critical data changes the computed
class to ``BLOCKED``; it never turns unknown PnL into zero.

This tool is pure/offline.  It performs no network, AWS, venue, or account
operation.  ``--write`` only refreshes the checked-in derived JSON artifact.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    ROOT / "Deepresearch V3" / "registry" / "EXPERIMENT_REGISTRY_V1.json"
)
SCHEMA_VERSION = "monetizable-experiment-registry-v1"
REGISTRY_ID = "DEEP03-ALL-ANGLES-MONETIZATION-V1"


ATOMIC_IDS = (
    "A01-SPREAD-CAPTURE",
    "A02-TOXICITY-MARKOUT",
    "A03-DEPLETION-REFILL",
    "A04-QUEUE-LAMBDA-DISTANCE",
    "A05-SNBD-LIQUIDITY-REGIMES",
    "A06-QUOTE-LIFETIME-BURST",
    "A07A-OCCUPANCY-FINGERPRINT",
    "A07B-COORDINATED-RETREAT",
    "A07C-ADVERSE-SELECTION-TRANSFER",
    "A07D-ANCHOR-MANIPULABILITY",
    "A08-PRICE-GRID-ROUND-NUMBER",
    "A09-LIQUIDITY-SEASONALITY",
    "A10-CROSS-MARKET-QUOTE-SPILLOVER",
    "A11-ONE-SIDED-PROVISION",
    "A12-GENERIC-PASSIVE-SCALPING",
    "A13-T90-RANGE",
    "A14-T90-REBUILD",
    "A15-FAIR-MICROPRICE-PAST-FLOW",
    "A16-INVENTORY-RESERVATION-SKEW",
    "A17-DYNAMIC-QUOTE-RADIUS",
    "A18-TIME-SHRINKING-CAP",
    "A19-NO-TRADE-QUOTE-JUMP",
    "A20-SYSTEM-LOAD-QUEUE-GROWTH",
    "B01-MOMENTUM-CONTINUATION",
    "B02-MEAN-REVERSION",
    "B03-POST-JUMP-CONTINUATION-VS-REVERSAL",
    "B04-LARGE-FLOW-CONTINUATION-VS-REVERSAL",
    "B05-OFI-PREDICTIVITY",
    "B06-BOOK-IMBALANCE-PREDICTIVITY",
    "B07-MICROPRICE-PREDICTIVITY",
    "B08-CONTINUOUS-PRICE-CALIBRATION",
    "B09-LISTING-TO-START-DRIFT",
    "B10-INPLAY-PROXY-PRICE-EROSION",
    "B11-REALIZED-VOL-BY-TIME",
    "B12-JUMP-HAZARD",
    "B13-CALM-TO-BURST",
    "B14-VOL-HARVEST",
    "B15-VOL-BREAKOUT",
    "B16-CS-ASK",
    "B17-CS-BID-CONTROL",
    "C01-THREEWAY-OVERROUND",
    "C02-BRACKET-MONOTONICITY",
    "C03-THRESHOLD-MONOTONICITY",
    "C04-SAME-EVENT-FAMILY-COHERENCE",
    "C05-SOCCER-POISSON",
    "C06-WITHIN-EVENT-LEADLAG",
    "C07-STALE-LEG",
    "C08-COMBO-VS-LEG-COST",
    "C09-PAYOUT-STATE-DOMINANCE",
    "C10-MULTIDAY-SERIES",
    "D01-RFQ-FLOW-CENSUS",
    "D02-RFQ-SIZE-INTENT",
    "D03-RFQ-LIFECYCLE-SURVIVAL",
    "D04-RFQ-COMBO-DEMAND-LEG-PRESSURE",
    "D05-RFQ-REQUESTER-HASH",
    "D06-RFQ-DIRECTION-VOL-SIGNAL",
    "D07-RFQ-TO-CLOB-IMPACT",
    "D08-RFQ-DIRECT-QUOTE-PNL",
    "D09-MVE-CATALOG-SCHEMA",
    "D10-MVE-DIRECT-TRADING",
    "E01-SETTLEMENT-CONVERGENCE",
    "E02-EXPIRY-LIQUIDITY-MIGRATION",
    "E03-FREEZE-WINDOW",
    "E04-SETTLEMENT-CALIBRATION",
    "E05-TERMINAL-EXCEPTION-STATES",
    "E06-HOLD-TO-TERMINAL-TAIL",
    "F01-PARTICIPANT-MIX-PROXY",
    "F02-FEE-STRUCTURE-EFFECT",
    "F03-NEW-MARKET-COLD-START",
    "F04-CROSS-SECTIONAL-RANK",
    "F05-SCANNER-SPREAD-FLOW-DEPTH",
    "F06-NICHE-COMPOSITE",
    "F07-LOW-OCC-INDEPENDENT-FAIR",
    "F08-SPORT-FACTORY",
    "F09-TENNIS-BO3-BO5",
    "F10-BASEBALL-ONE-SIDE",
    "F11-GOLF-THIN-LONG-LIFECYCLE",
    "F12-EXTERNAL-ODDS-LEADLAG",
    "F13-EXTERNAL-SPORT-STATE",
    "F14-PROVIDER-PRICE-LATENCY-TIER",
    "F15-BLINDNESS-EVPI",
    "F16-COURTSIDE-SPEED-OUT-OF-SCOPE",
    "F17-CRYPTO-SPOT-ANCHOR-OUT-OF-SCOPE",
    "G01-CHANGEPOINT-REGIME",
    "G02-HMM-HSMM-STATE",
    "G03-POINT-PROCESS-HAWKES",
    "G04-GARCH-STOCHASTIC-VOL",
    "G05-STATE-SPACE-KALMAN",
    "G06-PCA-FACTOR-CLUSTER",
    "G07-COINTEGRATION-VECM",
    "G08-REGULARIZED-GLM-SURVIVAL",
    "G09-TREE-BOOSTING",
    "G10-NEURAL-SEQUENCE",
    "G11-ENSEMBLE-STACKING",
    "G12-DRIFT-CALIBRATION-RETRAINING",
    "P01-STRATEGY-OVERLAP-PRECEDENCE",
    "P02-ROUTER-ABSTENTION-SWITCH",
    "P03-COMMON-ROOT-COVARIANCE-COTAIL",
    "P04-MULTIMARKET-ALLOCATION",
    "P05-BRACKET-HEDGE",
    "P06-DYNAMIC-DAILY-SELECTION",
    "P07-CAPACITY-FILL-DECAY-IMPACT",
    "P08-PROGRESSIVE-SCALING",
    "P09-MANUAL-SYSTEM-COEXISTENCE",
    "P10-COMMERCIAL-ROC-COST",
    "P11-UNCLASSIFIED-BACKLOG",
)


FAMILY_NAMES = {
    "A": "MICROSTRUCTURE_AND_MAKER",
    "B": "DIRECTIONAL_AND_VOLATILITY",
    "C": "RELATIVE_VALUE",
    "D": "RFQ_AND_MVE",
    "E": "TERMINAL_AND_SETTLEMENT",
    "F": "ECOLOGY_EXTERNAL_AND_SPORT",
    "G": "MODEL_METHODS",
    "P": "PORTFOLIO_AND_META",
}

EXECUTION_VENUES = {
    "NONE",
    "KALSHI_CLOB",
    "KALSHI_RFQ",
    "KALSHI_CLOB_AND_RFQ",
    "KALSHI_MVE_PRIVATE_EXECUTION",
    "KALSHI_MULTI_MARKET_CLOB",
    "POLICY_ROUTER_OVER_KALSHI",
}

EXTERNAL_DEPENDENCIES = {
    "NOT_REQUIRED",
    "REQUIRED",
    "OPTIONAL_REFINEMENT",
    "INHERIT_OWNER",
    "INHERIT_CHILDREN",
}

CHILD_DEPENDENT_ACTION_PROFILES = {
    "MAKER_FILTER",
    "RFQ_GUARD",
    "ROUTER",
    "SIZE_RISK",
    "CHILD_HEDGE",
    "T90_REBUILD",
    "COMMERCIAL_ROC",
}

CRITICAL_BLOCKERS = {
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

BLOCKER_REMEDIATION_CATALOG = {
    "MISSING_SIGNAL_INPUT": (
        "The card-specific real-time signal field is not captured.",
        "Add an append-only source adapter with schema, event time, receive time, mapping and quality receipts.",
        "SIGNAL",
    ),
    "TIMESTAMP_SEMANTICS_UNKNOWN": (
        "Publication/event/as-of and local receive clocks are not proven causal.",
        "Retain raw payloads plus source, wall and monotonic receive clocks; audit revisions and future-as-of sentinels.",
        "SIGNAL",
    ),
    "EXTERNAL_SPORTS_DATA_MISSING": (
        "The named direct sports-state or odds variables are absent.",
        "Capture an approved official/licensed source forward, or buy a point-in-time historical product after license review.",
        "SIGNAL",
    ),
    "EXTERNAL_API_UNVERIFIED": (
        "API fields, coverage, point-in-time behavior, license or cost are unverified.",
        "Run a read-only provider canary and schema/clock/license/spend audit before admitting a release.",
        "SIGNAL",
    ),
    "VENUE_PERMISSION_MISSING": (
        "The experiment has no shadow, RFQ-write or live-order authority.",
        "Request a separate least-privilege execution authority only after the research card is frozen.",
        "EXECUTION",
    ),
    "INSTRUMENT_MAPPING_UNKNOWN": (
        "Outcome, root, leg or external-event mapping is not authoritative.",
        "Build a versioned catalog/rules mapping with manual exception review and exact hash receipts.",
        "SIGNAL",
    ),
    "ACTION_INCOMPLETE": (
        "No deterministic side/price/quantity/timing/exit action is bound.",
        "Create a new immutable action-card revision; do not promote the research direction itself.",
        "ACTION",
    ),
    "BASELINE_INCOMPLETE": (
        "No comparable executable control policy is frozen.",
        "Define a same-opportunity no-trade or executable policy baseline before reading outcomes.",
        "DESIGN",
    ),
    "FEE_UNKNOWN": (
        "Exact order-level maker/taker fee, rounding, rebate or override is absent.",
        "Version fee facts by series/date/role/account and reconcile simulated fees to private account charges.",
        "PNL",
    ),
    "FILL_MODEL_MISSING": (
        "Strict stateful fill, partial fill, depth, cancel race or impact is not calibrated.",
        "Run pessimistic replay, then shadow own orders and reconcile to separately authorized micro-live fills.",
        "EXECUTION",
    ),
    "OWN_ORDER_CALIBRATION_MISSING": (
        "Public book data does not reveal own queue position or order lifecycle.",
        "Capture client order IDs, acknowledgements, queue proxies, cancels and fills in shadow/micro-live.",
        "EXECUTION",
    ),
    "LATENCY_UNMEASURED": (
        "Decision, place, cancel-effective, hedge or exit latency is not measured.",
        "Instrument monotonic timestamps at decision/send/ack/effective/fill and replay p50/p95/p99 paths.",
        "EXECUTION",
    ),
    "SETTLEMENT_MISSING": (
        "Terminal payout and every legal void/cancel/retire/postpone path are incomplete.",
        "Capture settlements and lifecycle changes append-only and bind versioned market rules to each contract.",
        "PNL",
    ),
    "PAYOUT_EXHAUSTIVENESS_MISSING": (
        "The proposed leg set is not proven exhaustive across all legal payout states.",
        "Create an audited state-by-leg payout matrix from market rules and settlement metadata.",
        "SIGNAL",
    ),
    "RFQ_PRIVATE_EVENTS_MISSING": (
        "Accepted side, quote, confirmation, execution, fill or fee events are absent.",
        "Use a separately authorized RFQ participant to capture private lifecycle receipts and correlate them to fills.",
        "EXECUTION",
    ),
    "RFQ_PUBLIC_DATA_UNRELEASED": (
        "No eligible fresh exact-version RFQ release is bound to the experiment.",
        "Complete the fresh RFQ seal/quality/manifest chain and same-date CLOB join; never repair the old damaged set.",
        "SIGNAL",
    ),
    "MVE_PRIVATE_EVENTS_MISSING": (
        "MVE package/leg order, acknowledgement, fill and fee lifecycle is absent.",
        "Capture the admitted MVE private execution interface and reconcile package/leg events to positions and settlement.",
        "EXECUTION",
    ),
    "MULTI_LEG_ENGINE_MISSING": (
        "Atomic/orphan-safe multi-leg execution and reconciliation do not exist.",
        "Build a joint replay/shadow engine with worst-sequence reserve, partial-leg unwind and shared risk ledger.",
        "EXECUTION",
    ),
    "CONFIRMED_CHILD_STRATEGIES_MISSING": (
        "The router, portfolio or increment has no confirmed executable child policies.",
        "Confirm children independently, then bind immutable child fingerprints and a common-root ledger.",
        "DESIGN",
    ),
    "PRIVATE_ACCOUNT_STATE_MISSING": (
        "Authoritative positions, open orders, fills or ownership state are absent.",
        "Capture least-privilege read-only account snapshots and reconcile system-owned order/position ledgers.",
        "RISK",
    ),
    "INSUFFICIENT_QUALITY_DATES": (
        "Independent clean dates/roots are below the frozen evidence requirement.",
        "Continue the automatic sealed daily pipeline to a preregistered fixed end; never extend based on results.",
        "INFERENCE",
    ),
    "SAMPLE_TOO_SMALL": (
        "Eligible independent roots/events are too sparse for the frozen estimand.",
        "Accrue to the preregistered end or close as not estimable; do not pool away the niche.",
        "INFERENCE",
    ),
    "DATA_INTEGRITY_BLOCKED": (
        "An input object or release failed immutable integrity checks.",
        "Quarantine the whole damaged object/release and use only a newly sealed clean acquisition.",
        "DATA",
    ),
    "RISK_GATES_MISSING": (
        "Capital, inventory, tail, concentration, stop or emergency-exit limits are not complete.",
        "Freeze root/event/day caps, collateral, tail limits, emergency tokens and reduce-only exits.",
        "RISK",
    ),
    "OPERATING_COST_UNKNOWN": (
        "Provider, compute/storage/egress, labor/on-call or incident cost is not measured.",
        "Bind invoices and metered usage to UTC day/card/root allocation rules; unknown operating cost blocks commercial ROC.",
        "PNL",
    ),
    "OUT_OF_SCOPE_CURRENT_PROGRAM": (
        "The direction is excluded from the current Sports program.",
        "Keep it closed unless a new operator-approved program and lineage explicitly admit it.",
        "PROGRAM",
    ),
}

CARD_SPECIFIC_GAP_OVERRIDES = {
    ("A07D-ANCHOR-MANIPULABILITY", "MISSING_SIGNAL_INPUT"): (
        "No independently sourced fair anchor and visible cost-to-move series is bound.",
        "Define an actually independent anchor, prove it excludes circular Kalshi inputs, and capture its as-of path.",
    ),
    ("A20-SYSTEM-LOAD-QUEUE-GROWTH", "MISSING_SIGNAL_INPUT"): (
        "Decision-queue age, event-loop lag, send backlog and cancel-effective system telemetry are absent.",
        "Instrument the production/shadow engine with monotonic queue/load/send/ack timestamps and seal daily telemetry.",
    ),
    ("F07-LOW-OCC-INDEPENDENT-FAIR", "MISSING_SIGNAL_INPUT"): (
        "No independent fair-value source or model with non-circular inputs is available.",
        "Register the fair model/source, its inputs and as-of clock; prove no contemporaneous target-book leakage.",
    ),
    ("F15-BLINDNESS-EVPI", "CONFIRMED_CHILD_STRATEGIES_MISSING"): (
        "No immutable eligible owner card, decision/opportunity ledger, loss functional, or future-oracle rule is bound.",
        "Confirm and fingerprint one owner revision, seal its decision ledger, then freeze the loss functional and non-causal oracle rule before outcome reads.",
    ),
    ("A16-INVENTORY-RESERVATION-SKEW", "PRIVATE_ACCOUNT_STATE_MISSING"): (
        "The strategy cannot observe reconciled own inventory and resting-order exposure.",
        "Add read-only positions/open-orders/fills snapshots and a system-owned order reconciliation ledger.",
    ),
    ("E03-FREEZE-WINDOW", "PRIVATE_ACCOUNT_STATE_MISSING"): (
        "The early-exit policy lacks an authoritative position to reduce.",
        "Bind read-only position/open-order snapshots at every freeze decision and reconcile the reduce-only quantity.",
    ),
    ("E06-HOLD-TO-TERMINAL-TAIL", "PRIVATE_ACCOUNT_STATE_MISSING"): (
        "The hold-versus-exit comparison lacks exact owned fill and residual position identity.",
        "Bind the original private fill, subsequent position ledger and paired executable exit opportunity.",
    ),
    ("P05-BRACKET-HEDGE", "CONFIRMED_CHILD_STRATEGIES_MISSING"): (
        "The hedge has no confirmed primary child exposure to offset.",
        "Confirm and fingerprint the primary child first, then bind the exact bracket hedge legs.",
    ),
    ("P09-MANUAL-SYSTEM-COEXISTENCE", "PRIVATE_ACCOUNT_STATE_MISSING"): (
        "Manual and automated order ownership, precedence and self-trade state are not observable.",
        "Add system ownership tags, read-only account reconciliation and explicit manual/system precedence receipts.",
    ),
}


COMMON_COST_COMPONENTS = [
    {
        "type": "EXCHANGE_MAKER_FEE",
        "role": "CASH_DEDUCTION",
        "status": "MISSING",
        "rule": "exact per-fill fee_cost or versioned series/date fee formula",
    },
    {
        "type": "EXCHANGE_TAKER_FEE",
        "role": "CASH_DEDUCTION",
        "status": "MISSING",
        "rule": "exact entry/exit/hedge fee including rounding and rebates",
    },
    {
        "type": "SPREAD_CROSSING",
        "role": "EXECUTION_MODEL_INPUT",
        "status": "CONSERVATIVE_MODELED",
        "rule": "use executable ask on buys and executable bid on sells",
    },
    {
        "type": "SLIPPAGE_MARKET_IMPACT",
        "role": "EXECUTION_MODEL_INPUT",
        "status": "MISSING",
        "rule": "walk exact L2 for requested quantity at decision-effective time",
    },
    {
        "type": "PLACE_CANCEL_EXIT_LATENCY",
        "role": "EXECUTION_MODEL_INPUT",
        "status": "MISSING",
        "rule": "separate measured placement, cancel-effective, hedge and exit paths",
    },
    {
        "type": "INVENTORY_TERMINAL_VOID",
        "role": "RISK_DIAGNOSTIC",
        "status": "MISSING",
        "rule": "joint worst legal terminal state unless actual settlement is bound",
    },
    {
        "type": "COLLATERAL_AND_CAPITAL",
        "role": "OPERATING_EXPENSE",
        "status": "MISSING",
        "rule": "peak collateral, capital-time and capacity reported separately",
    },
]

COST_TYPES = {
    "EXCHANGE_MAKER_FEE",
    "EXCHANGE_TAKER_FEE",
    "SPREAD_CROSSING",
    "SLIPPAGE_MARKET_IMPACT",
    "PLACE_CANCEL_EXIT_LATENCY",
    "INVENTORY_TERMINAL_VOID",
    "COLLATERAL_AND_CAPITAL",
    "RFQ_SELECTION_AND_CONFIRMATION",
    "MVE_PACKAGE_EXECUTION_AND_PARTIAL",
    "DATA_API",
    "POLICY_SWITCH_AND_OVERLAP",
    "DATA_PROVIDER_LICENSE_API",
    "CLOUD_COMPUTE_STORAGE_EGRESS",
    "OPS_LABOR_AND_INCIDENT",
}
COST_ROLES = {
    "CASH_DEDUCTION",
    "EXECUTION_MODEL_INPUT",
    "OPERATING_EXPENSE",
    "RISK_DIAGNOSTIC",
}
COST_STATUSES = {"MISSING", "CONSERVATIVE_MODELED"}
BASELINE_TYPES = {
    "NOT_APPLICABLE",
    "NO_TRADE",
    "UNFILTERED_QUOTER",
    "CUSTOM_EXECUTABLE",
}


def _action_profile(name: str) -> dict[str, Any]:
    profiles: dict[str, dict[str, Any]] = {
        "NO_ACTION": {
            "status": "NO_ACTION_DEFINED",
            "action_type": [],
            "signal_to_action_mapping": None,
            "instrument_side": None,
            "order_style": None,
            "price_rule": None,
            "quantity_rule": None,
            "timing_and_cancel": None,
            "exit_or_settlement": None,
            "kill_conditions": [],
        },
        "MAKER_SPREAD": {
            "status": "DEFINED_FOR_BACKTEST_DESIGN",
            "action_type": ["PLACE_PASSIVE_QUOTE", "CANCEL", "REPRICE"],
            "signal_to_action_mapping": (
                "When the registered eligibility and net-edge trigger is true, "
                "place one post-only quote on each permitted side; cancel both "
                "when the trigger becomes false, the book gaps, or data is stale."
            ),
            "instrument_side": "TWO_SIDED_YES_NORMALIZED",
            "order_style": "POST_ONLY_LIMIT",
            "price_rule": "registered tick-valid bid/ask offsets from executable book",
            "quantity_rule": "one contract per active side until capacity is proven",
            "timing_and_cancel": "decision clock plus measured placement/cancel latency",
            "exit_or_settlement": "cancel sibling, reconcile, then bounded reduce-only IOC",
            "kill_conditions": ["gap", "stale_book", "unknown_fee", "position_mismatch"],
        },
        "MAKER_FILTER": {
            "status": "DEFINED_FOR_BACKTEST_DESIGN",
            "action_type": ["CANCEL", "REPRICE", "RESIZE", "CANCEL_AND_REPLACE"],
            "signal_to_action_mapping": (
                "Apply the signal as a risk switch to the registered maker: "
                "cancel the vulnerable side, widen, or reduce size; restore only "
                "after the signal clears and state is reconciled."
            ),
            "instrument_side": "DERIVED_BY_SIGNAL_WITH_BOTH_SIDES_ENUMERATED",
            "order_style": "POST_ONLY_LIMIT_AND_CANCEL",
            "price_rule": "registered maker price plus signal-conditioned safety buffer",
            "quantity_rule": "never exceed the unfiltered baseline inventory cap",
            "timing_and_cancel": "cancel-effective latency is part of treatment",
            "exit_or_settlement": "reconcile residual fills, then reduce-only exit",
            "kill_conditions": ["direction_unknown", "cancel_race", "gap", "stale_book"],
        },
        "MAKER_REFILL": {
            "status": "DEFINED_FOR_BACKTEST_DESIGN",
            "action_type": ["PLACE_PASSIVE_QUOTE", "CANCEL_AND_REPLACE"],
            "signal_to_action_mapping": (
                "After registered depletion, quote or requote only when refill "
                "state and price-band gates pass; cancel at the frozen refill "
                "deadline if recovery is absent."
            ),
            "instrument_side": "DEPLETED_SIDE_ENUMERATED_IN_YES_NORMALIZED_BOOK",
            "order_style": "POST_ONLY_LIMIT",
            "price_rule": "same tick or registered adjacent tick after depletion",
            "quantity_rule": "one contract; no refill-size extrapolation",
            "timing_and_cancel": "frozen refill window plus measured cancel-effective latency",
            "exit_or_settlement": "cancel sibling and reduce-only unwind after any fill",
            "kill_conditions": ["no_refill_by_deadline", "gap", "queue_unknown", "stale_book"],
        },
        "T90_RANGE": {
            "status": "DEFINED_BUT_SCOPE_AND_EXECUTION_BLOCKED",
            "action_type": [
                "PLACE_PASSIVE_QUOTE",
                "CANCEL",
                "REDUCE_ONLY_EXIT",
            ],
            "signal_to_action_mapping": (
                "After the frozen Tennis 88-93 leader band, causal two-way tug, "
                "flat inventory and no-retreat gates pass, post one contract at "
                "each touch (or the one preregistered behind1 variant). On any "
                "fill cancel the sibling, reconcile, and reduce only."
            ),
            "instrument_side": "FROZEN_LEADER_NORMALIZED_TWO_SIDED_PAIR",
            "order_style": "NON_ATOMIC_POST_ONLY_PAIR_WITH_WORST_SEQUENCE_RESERVE",
            "price_rule": "touch or one legal tick behind, frozen before YES/NO mapping",
            "quantity_rule": "one contract per side; absolute root inventory <= 1",
            "timing_and_cancel": "cancel-before-replace; 250ms normal reprice minimum",
            "exit_or_settlement": (
                "band exit, leader change, hard flip, timeout or safety kill at "
                "effective executable depth"
            ),
            "kill_conditions": [
                "retreat",
                "gap",
                "stale",
                "pause",
                "terminal",
                "leader_change",
            ],
        },
        "T90_REBUILD": {
            "status": "DEFINED_BUT_SCOPE_AND_EXECUTION_BLOCKED",
            "action_type": [
                "PLACE_PASSIVE_QUOTE",
                "CANCEL",
                "REDUCE_ONLY_EXIT",
            ],
            "signal_to_action_mapping": (
                "Only after the parent retreat kill is complete and zero-resting "
                "is proven: require a latched burst, 3s without retreat, top-3 "
                "depth recovery >=80% of pre-kill, spread >=1.25x baseline, "
                "<=1 tick movement and the 88-93 band; then quote touch one "
                "contract per side for at most one rebuild cycle."
            ),
            "instrument_side": "FROZEN_T90_LEADER_NORMALIZED_TWO_SIDED_PAIR",
            "order_style": "ONE_CYCLE_NON_ATOMIC_POST_ONLY_PAIR",
            "price_rule": "touch only, bound to the frozen pre-kill baseline",
            "quantity_rule": "one contract per side; one rebuild cycle per root",
            "timing_and_cancel": "zero-resting precedes entry; maximum episode 30s",
            "exit_or_settlement": (
                "exit at spread <=1.10x pre-kill baseline or immediately on a "
                "new burst, renewed retreat, gap, stale or root stop"
            ),
            "kill_conditions": [
                "renewed_retreat",
                "new_burst",
                "gap",
                "stale",
                "root_stop",
                "second_cycle",
            ],
        },
        "PASSIVE_FADE": {
            "status": "DEFINED_FOR_BACKTEST_DESIGN",
            "action_type": ["PLACE_PASSIVE_QUOTE", "CANCEL"],
            "signal_to_action_mapping": (
                "Place a post-only quote on the registered missing/cheap side "
                "only inside the eligible state; cancel on timeout or continuation."
            ),
            "instrument_side": "DERIVED_BY_SIGNAL_WITH_YES_NO_MAPPING",
            "order_style": "POST_ONLY_LIMIT",
            "price_rule": "registered tick at or behind the restored side",
            "quantity_rule": "one contract until real-fill calibration passes",
            "timing_and_cancel": "frozen fade horizon and measured cancel latency",
            "exit_or_settlement": "time exit or state-recovery exit at executable price",
            "kill_conditions": ["continuation", "timeout", "gap", "external_state_missing"],
        },
        "TAKER_DIRECTIONAL": {
            "status": "DEFINED_FOR_BACKTEST_DESIGN",
            "action_type": ["TAKE_LIQUIDITY", "HEDGE"],
            "signal_to_action_mapping": (
                "Map the registered signed signal to BUY_YES or BUY_NO, enter "
                "with a marketable limit after measured latency, and exit at the "
                "registered horizon or reversal using executable depth."
            ),
            "instrument_side": "DERIVED_BY_SIGNAL_EXHAUSTIVE_YES_NO_MAP",
            "order_style": "MARKETABLE_LIMIT",
            "price_rule": "walk current exact L2 up to frozen slippage cap",
            "quantity_rule": "largest quantity whose pessimistic marginal edge remains positive",
            "timing_and_cancel": "entry and exit latency measured separately",
            "exit_or_settlement": "time/reversal exit; no silent upgrade to settlement hold",
            "kill_conditions": ["unsigned_signal", "edge_below_cost", "depth_shortfall", "gap"],
        },
        "RELATIVE_VALUE": {
            "status": "DEFINED_BUT_ENGINE_BLOCKED",
            "action_type": ["MULTI_LEG_ATOMIC", "HEDGE"],
            "signal_to_action_mapping": (
                "When a payout-exhaustive executable residual exceeds all-leg "
                "cost plus buffer, execute the full registered leg set or abstain."
            ),
            "instrument_side": "EXACT_LEG_VECTOR_WITH_PAYOUT_STATES",
            "order_style": "BOUNDED_MARKETABLE_LIMIT_PER_LEG",
            "price_rule": "all-leg executable L2, never mids",
            "quantity_rule": "minimum fillable quantity across every required leg",
            "timing_and_cancel": "serial-leg risk and partial fills explicitly bounded",
            "exit_or_settlement": "complete hedge or joint worst-state terminal valuation",
            "kill_conditions": ["payout_incomplete", "partial_leg", "mapping_drift", "fee_unknown"],
        },
        "EXPIRY_ROLL": {
            "status": "DEFINED_BUT_MAPPING_AND_ENGINE_BLOCKED",
            "action_type": [
                "REDUCE_OLD_EXPOSURE",
                "OPEN_EQUIVALENT_NEW_EXPOSURE",
                "RECONCILE",
            ],
            "signal_to_action_mapping": (
                "For an authoritative existing position, close the old expiry "
                "and open only the audited exposure-equivalent new contract when "
                "the complete two-leg roll cost is below holding cost."
            ),
            "instrument_side": "EXACT_EXISTING_EXPOSURE_TO_AUDITED_EQUIVALENT_VECTOR",
            "order_style": "BOUNDED_MARKETABLE_LIMIT_PER_ROLL_LEG",
            "price_rule": "synchronized executable depth including orphan unwind",
            "quantity_rule": "no more than the reconciled old exposure and new-leg depth",
            "timing_and_cancel": "serial-leg risk and partial roll are explicitly bounded",
            "exit_or_settlement": "complete, unwind, or retain the frozen original exposure baseline",
            "kill_conditions": [
                "mapping_unknown",
                "partial_leg",
                "roll_cost_above_hold",
                "position_mismatch",
            ],
        },
        "CHILD_HEDGE": {
            "status": "DEFINED_BUT_CHILD_MAPPING_AND_ENGINE_BLOCKED",
            "action_type": ["OPEN_HEDGE_VECTOR", "RECONCILE", "UNWIND_HEDGE"],
            "signal_to_action_mapping": (
                "Given an authoritative confirmed child exposure, execute the "
                "exact payout-verified hedge vector only when its reduction in "
                "tail/capital cost exceeds all-leg execution cost."
            ),
            "instrument_side": "EXACT_CONFIRMED_CHILD_EXPOSURE_AND_HEDGE_VECTOR",
            "order_style": "BOUNDED_MARKETABLE_LIMIT_PER_HEDGE_LEG",
            "price_rule": "synchronized executable all-leg depth, never mids",
            "quantity_rule": "hedge no more than the reconciled child exposure",
            "timing_and_cancel": "partial hedge and child-exit races are explicitly bounded",
            "exit_or_settlement": "joint child-plus-hedge unwind or legal joint terminal value",
            "kill_conditions": [
                "child_unconfirmed",
                "payout_incomplete",
                "partial_leg",
                "tail_benefit_below_cost",
            ],
        },
        "RFQ_GUARD": {
            "status": "DEFINED_BUT_DATA_BLOCKED",
            "action_type": ["CANCEL", "REPRICE", "RESIZE"],
            "signal_to_action_mapping": (
                "Treat a public RFQ as unsigned toxicity/volatility information: "
                "cancel, widen, or shrink related CLOB quotes. Never infer a "
                "direction from public create/delete or combo composition."
            ),
            "instrument_side": "BOTH_RELATED_CLOB_SIDES_PUBLIC_UNSIGNED",
            "order_style": "POST_ONLY_LIMIT_AND_CANCEL",
            "price_rule": "unfiltered maker price plus RFQ risk buffer",
            "quantity_rule": "at most baseline resting quantity",
            "timing_and_cancel": "RFQ receive clock to cancel-effective clock",
            "exit_or_settlement": "reconcile any cancel-race fill",
            "kill_conditions": ["rfq_gap", "mapping_ambiguous", "direction_inferred_publicly"],
        },
        "RFQ_MAKER": {
            "status": "DEFINED_BUT_PERMISSION_AND_DATA_BLOCKED",
            "action_type": ["RFQ_SUBMIT_QUOTE", "RFQ_CONFIRM", "HEDGE"],
            "signal_to_action_mapping": (
                "For a full-size hedgeable RFQ, submit bounded yes_bid/no_bid; "
                "after a private accepted_side event confirm, correlate executed "
                "orders to fills, hedge, and reconcile residual inventory."
            ),
            "instrument_side": "TWO_SIDED_RFQ_BID_THEN_PRIVATE_ACCEPTED_SIDE",
            "order_style": "RFQ_QUOTE_PLUS_CLOB_MARKETABLE_LIMIT",
            "price_rule": "bid no higher than fair value minus all hedge and tail costs",
            "quantity_rule": "entire RFQ size only when full size is collateralized and hedgeable",
            "timing_and_cancel": "standard/HVM confirmation and execution clocks measured",
            "exit_or_settlement": "CLOB hedge, executable unwind, or bound settlement",
            "kill_conditions": ["private_event_missing", "hedge_depth_shortfall", "permission_missing"],
        },
        "MVE_DIRECT": {
            "status": "DEFINED_BUT_PRIVATE_MVE_AND_ENGINE_BLOCKED",
            "action_type": [
                "MVE_SUBMIT_PACKAGE_ORDER",
                "CANCEL",
                "HEDGE_LEGS",
                "RECONCILE",
                "UNWIND",
            ],
            "signal_to_action_mapping": (
                "Only for an exact-version MVE package with an audited leg, "
                "orientation and payout-state map: submit one fully collateralized "
                "package order when package price plus synchronized executable "
                "hedge cost leaves positive worst-state PnL; otherwise abstain."
            ),
            "instrument_side": "EXACT_MVE_PACKAGE_ORIENTATION_AND_AUDITED_HEDGE_VECTOR",
            "order_style": "PRIVATE_MVE_PACKAGE_ORDER_PLUS_BOUNDED_LEG_LIMITS",
            "price_rule": (
                "exact executable package price plus synchronized all-leg L2, "
                "fees, orphan-unwind and tail buffer; never use mids"
            ),
            "quantity_rule": "one complete package at the minimum fully hedgeable size",
            "timing_and_cancel": (
                "retain package submit/ack/cancel/fill clocks and bound serial-leg "
                "hedge latency and partial-package races"
            ),
            "exit_or_settlement": (
                "complete the audited hedge, execute the frozen orphan unwind, "
                "or use the joint legal terminal payout matrix"
            ),
            "kill_conditions": [
                "mve_catalog_or_orientation_unknown",
                "private_package_event_missing",
                "partial_package_or_leg",
                "hedge_depth_shortfall",
                "fee_or_terminal_state_unknown",
            ],
        },
        "TERMINAL_POLICY": {
            "status": "DEFINED_BUT_SETTLEMENT_BLOCKED",
            "action_type": ["HOLD", "TAKE_LIQUIDITY", "CANCEL"],
            "signal_to_action_mapping": (
                "At the registered terminal-state signal, choose the frozen "
                "hold or executable exit branch; cancel all risk-adding orders first."
            ),
            "instrument_side": "EXACT_EXISTING_POSITION",
            "order_style": "CANCEL_THEN_REDUCE_ONLY_MARKETABLE_LIMIT_OR_HOLD",
            "price_rule": "executable unwind depth or authoritative terminal value",
            "quantity_rule": "no more than reconciled absolute position",
            "timing_and_cancel": "freeze/settlement clock with separate exit latency",
            "exit_or_settlement": "frozen exit rule or actual 0/1/void settlement",
            "kill_conditions": ["terminal_state_unknown", "void_rule_unknown", "position_mismatch"],
        },
        "TERMINAL_ENTRY": {
            "status": "DEFINED_BUT_SETTLEMENT_BLOCKED",
            "action_type": ["TAKE_LIQUIDITY", "CANCEL", "HOLD"],
            "signal_to_action_mapping": (
                "From flat, buy exactly the registered underpriced YES or NO "
                "outcome with a marketable limit only when the terminal-value "
                "gap exceeds all fees, exit and legal-state buffers."
            ),
            "instrument_side": "EXPLICIT_BUY_YES_OR_BUY_NO_FROM_FLAT",
            "order_style": "ONE_CONTRACT_MARKETABLE_LIMIT",
            "price_rule": "executable ask no higher than terminal value minus all costs",
            "quantity_rule": "one contract from flat; no pyramiding or naked short",
            "timing_and_cancel": "decision-effective depth plus measured entry/exit latency",
            "exit_or_settlement": "frozen executable exit or authoritative 0/1/void settlement",
            "kill_conditions": [
                "terminal_state_unknown",
                "void_rule_unknown",
                "edge_below_cost",
                "depth_shortfall",
            ],
        },
        "CS_ASK": {
            "status": "DEFINED_BUT_SCOPE_SETTLEMENT_AND_EXECUTION_BLOCKED",
            "action_type": ["PLACE_PASSIVE_QUOTE", "CANCEL", "HOLD_TO_SETTLEMENT"],
            "signal_to_action_mapping": (
                "Freeze the high outcome and q in {0.97,0.98}; after the exact "
                "book-regime and 30s persistence gate, economically sell high "
                "from flat by posting to buy the low outcome at 1-q. Allow one "
                "contract and one fill per root with no replenish."
            ),
            "instrument_side": "FROM_FLAT_BUY_LOW_OUTCOME_AT_ONE_MINUS_Q",
            "order_style": "POST_ONLY_LIMIT_ONE_FILL_MAX",
            "price_rule": "q is the intended high-side ask; low-side buy price is exactly 1-q",
            "quantity_rule": "one contract, one fill maximum per root, no replenish",
            "timing_and_cancel": "intended quote state persists 30s; quote age 30s",
            "exit_or_settlement": "actual authoritative settlement only for binding PnL",
            "kill_conditions": [
                "price_cell_exit",
                "high_side_bid_retreat_one_tick",
                "better_ask",
                "leader_flip",
                "stale",
                "gap",
                "pause",
                "terminal",
                "fee_or_tick_unknown",
            ],
        },
        "EARLY_EXIT": {
            "status": "DEFINED_BUT_PRIVATE_AND_TERMINAL_DATA_BLOCKED",
            "action_type": ["CANCEL", "REDUCE_ONLY_EXIT"],
            "signal_to_action_mapping": (
                "When the frozen pre-freeze deterioration signal fires, cancel "
                "risk-adding orders and unwind only the reconciled existing "
                "position at bounded executable depth."
            ),
            "instrument_side": "EXACT_EXISTING_POSITION_REDUCE_ONLY",
            "order_style": "CANCEL_THEN_REDUCE_ONLY_MARKETABLE_LIMIT",
            "price_rule": "decision-effective executable depth with slippage cap",
            "quantity_rule": "no more than reconciled absolute position",
            "timing_and_cancel": "cancel-effective and exit latency are distinct",
            "exit_or_settlement": "early executable exit before freeze",
            "kill_conditions": [
                "position_unknown",
                "precursor_stale",
                "depth_shortfall",
                "terminal_state_unknown",
            ],
        },
        "EXTERNAL_ACTION": {
            "status": "DEFINED_BUT_EXTERNAL_DATA_BLOCKED",
            "action_type": ["CANCEL", "REPRICE", "TAKE_LIQUIDITY"],
            "signal_to_action_mapping": (
                "After a verified external event/odds update with trustworthy "
                "event time, cancel stale quotes or trade only the registered "
                "venue-price discrepancy; no proxy or imputation is allowed."
            ),
            "instrument_side": "DERIVED_FROM_VERIFIED_EXTERNAL_TO_KALSHI_MAPPING",
            "order_style": "CANCEL_OR_MARKETABLE_LIMIT",
            "price_rule": "verified external fair value versus executable Kalshi L2",
            "quantity_rule": "depth- and latency-bounded one-contract pilot",
            "timing_and_cancel": "external event time, ingest time, and venue send time all retained",
            "exit_or_settlement": "time/reversal exit or explicitly bound settlement",
            "kill_conditions": ["external_stale", "mapping_unverified", "license_unknown", "clock_unknown"],
        },
        "ROUTER": {
            "status": "DEFINED_BUT_CHILDREN_BLOCKED",
            "action_type": ["RESIZE", "CANCEL", "REPRICE"],
            "signal_to_action_mapping": (
                "Select, size, or abstain among already confirmed child policies "
                "using only past state; route no action to an unconfirmed child."
            ),
            "instrument_side": "INHERITED_FROM_SELECTED_CONFIRMED_CHILD",
            "order_style": "INHERITED_FROM_SELECTED_CONFIRMED_CHILD",
            "price_rule": "unchanged child price rule",
            "quantity_rule": "shared root/event/capital cap across children",
            "timing_and_cancel": "router decision and switch latency included",
            "exit_or_settlement": "unchanged child exit plus portfolio reconciliation",
            "kill_conditions": ["child_unconfirmed", "overlap_unresolved", "capital_breach"],
        },
        "COMMERCIAL_ROC": {
            "status": "DEFINED_BUT_CHILDREN_AND_FULL_COST_LEDGER_BLOCKED",
            "action_type": [
                "CONTINUE_CONFIRMED_CHILD",
                "SCALE_DOWN",
                "STOP_NEW_RISK",
            ],
            "signal_to_action_mapping": (
                "At the frozen UTC decision boundary, continue the immutable "
                "confirmed child only when fully allocated trailing commercial "
                "NetPnL and ROC clear their preregistered gates; scale down at "
                "the warning gate and stop new risk at the failure gate."
            ),
            "instrument_side": "INHERITED_UNCHANGED_FROM_IMMUTABLE_CONFIRMED_CHILD",
            "order_style": "INHERITED_CHILD_OR_STOP_NEW_RISK",
            "price_rule": "unchanged immutable child price rule",
            "quantity_rule": "frozen full, reduced or zero-new-risk ladder only",
            "timing_and_cancel": "UTC boundary decision using past-only closed accounting periods",
            "exit_or_settlement": (
                "stopped children add no new risk and retain their registered "
                "reduce-only exit and reconciliation path"
            ),
            "kill_conditions": [
                "child_fingerprint_missing",
                "invoice_or_usage_unallocated",
                "capital_ledger_incomplete",
                "incident_cost_unknown",
            ],
        },
        "SIZE_RISK": {
            "status": "DEFINED_BUT_PRIVATE_OR_CHILD_DATA_BLOCKED",
            "action_type": ["RESIZE", "CANCEL", "HEDGE"],
            "signal_to_action_mapping": (
                "Translate the registered capacity/risk signal into a bounded "
                "size or no-new-risk decision; reconcile before any size increase."
            ),
            "instrument_side": "INHERITED_FROM_REGISTERED_CHILD_OR_POSITION",
            "order_style": "INHERITED_CHILD_WITH_REDUCE_ONLY_EMERGENCY_EXIT",
            "price_rule": "unchanged registered child price rule",
            "quantity_rule": "frozen size ladder bounded by depth, collateral and root cap",
            "timing_and_cancel": "size change only after prior orders reconcile",
            "exit_or_settlement": "reduce-only exit derived from authoritative position",
            "kill_conditions": ["capacity_curve_negative", "tail_breach", "position_unknown"],
        },
        "MODEL_COMPONENT": {
            "status": "NO_STANDALONE_ACTION_MODEL_COMPONENT",
            "action_type": [],
            "signal_to_action_mapping": None,
            "instrument_side": None,
            "order_style": None,
            "price_rule": None,
            "quantity_rule": None,
            "timing_and_cancel": None,
            "exit_or_settlement": None,
            "kill_conditions": [],
        },
        "CONTROL_ONLY": {
            "status": "NO_ACTION_CONTROL_ONLY",
            "action_type": [],
            "signal_to_action_mapping": None,
            "instrument_side": None,
            "order_style": None,
            "price_rule": None,
            "quantity_rule": None,
            "timing_and_cancel": None,
            "exit_or_settlement": None,
            "kill_conditions": [],
        },
    }
    return copy.deepcopy(profiles[name])


def _venue_for(action_profile: str) -> dict[str, Any]:
    venue_by_profile = {
        "NO_ACTION": "NONE",
        "MODEL_COMPONENT": "NONE",
        "CONTROL_ONLY": "NONE",
        "MAKER_SPREAD": "KALSHI_CLOB",
        "MAKER_FILTER": "KALSHI_CLOB",
        "MAKER_REFILL": "KALSHI_CLOB",
        "T90_RANGE": "KALSHI_CLOB",
        "T90_REBUILD": "KALSHI_CLOB",
        "PASSIVE_FADE": "KALSHI_CLOB",
        "TAKER_DIRECTIONAL": "KALSHI_CLOB",
        "RELATIVE_VALUE": "KALSHI_MULTI_MARKET_CLOB",
        "EXPIRY_ROLL": "KALSHI_MULTI_MARKET_CLOB",
        "CHILD_HEDGE": "KALSHI_MULTI_MARKET_CLOB",
        "RFQ_GUARD": "KALSHI_CLOB",
        "RFQ_MAKER": "KALSHI_CLOB_AND_RFQ",
        "MVE_DIRECT": "KALSHI_MVE_PRIVATE_EXECUTION",
        "TERMINAL_POLICY": "KALSHI_CLOB",
        "TERMINAL_ENTRY": "KALSHI_CLOB",
        "CS_ASK": "KALSHI_CLOB",
        "EARLY_EXIT": "KALSHI_CLOB",
        "EXTERNAL_ACTION": "KALSHI_CLOB",
        "ROUTER": "POLICY_ROUTER_OVER_KALSHI",
        "COMMERCIAL_ROC": "POLICY_ROUTER_OVER_KALSHI",
        "SIZE_RISK": "POLICY_ROUTER_OVER_KALSHI",
    }
    venue = venue_by_profile[action_profile]
    return {
        "execution_venue": venue,
        "required_permission": (
            "NONE"
            if venue == "NONE"
            else "SEPARATE_SHADOW_OR_LIVE_AUTHORITY; registry grants none"
        ),
        "availability_receipt": None,
    }


def _baseline_for(action_profile: str) -> dict[str, Any]:
    if action_profile in {"NO_ACTION", "MODEL_COMPONENT", "CONTROL_ONLY"}:
        return {
            "baseline_id": None,
            "baseline_type": "NOT_APPLICABLE",
            "policy": None,
            "comparability": None,
            "secondary_baselines": [],
        }
    if action_profile in {"MAKER_FILTER", "RFQ_GUARD"}:
        return {
            "baseline_id": "UNFILTERED_SAME_MAKER",
            "baseline_type": "UNFILTERED_QUOTER",
            "policy": "identical maker without the tested signal gate",
            "comparability": "same roots, capital, latency, fill and fee assumptions",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "MAKER_REFILL":
        return {
            "baseline_id": "IMMEDIATE_SAME_PRICE_REENTRY",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": (
                "replace immediately at the same tick after depletion without "
                "waiting for the tested refill/persistence gate"
            ),
            "comparability": "same roots, price, size, latency, fill and fee assumptions",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "T90_RANGE":
        return {
            "baseline_id": "NO_TRADE_SAME_OPPORTUNITIES",
            "baseline_type": "NO_TRADE",
            "policy": "remain flat for every eligible T90 episode",
            "comparability": "same roots, band, tug, clock, capital and data exclusions",
            "secondary_baselines": ["SAME_T90_RANGE_WITHOUT_RETREAT_KILL"],
        }
    if action_profile == "T90_REBUILD":
        return {
            "baseline_id": "T90_PARENT_NEVER_REENTER",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": (
                "same-root T90 parent transitions OFF_FOR_ROOT after the first "
                "valid retreat kill and never re-enters"
            ),
            "comparability": "same parent roots, fills, kill, fees, capital and terminal path",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "ROUTER":
        return {
            "baseline_id": "BEST_FIXED_CONFIRMED_CHILD_TRAIN_ONLY",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": "best fixed child chosen without holdout information",
            "comparability": "same roots, capital, risk caps and execution engine",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "SIZE_RISK":
        return {
            "baseline_id": "FIXED_ONE_CONTRACT_SAME_CHILD",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": "same child policy at one contract and unchanged risk caps",
            "comparability": "same roots, prices, timing, fill and fee assumptions",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "TERMINAL_POLICY":
        return {
            "baseline_id": "FIXED_IMMEDIATE_EXECUTABLE_EXIT",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": "cancel then unwind at the first eligible terminal decision",
            "comparability": "same inventory, roots, costs and terminal states",
            "secondary_baselines": ["NO_TRADE_OR_HOLD_AS_CARD_SPECIFIES"],
        }
    if action_profile == "TERMINAL_ENTRY":
        return {
            "baseline_id": "NO_TRADE_SAME_OPPORTUNITIES",
            "baseline_type": "NO_TRADE",
            "policy": "remain flat and record zero cash flow for every eligible opportunity",
            "comparability": "same roots, clock, capital availability and data exclusions",
            "secondary_baselines": ["SIMPLE_UNCALIBRATED_EXECUTABLE_PRICE_RULE"],
        }
    if action_profile == "CS_ASK":
        return {
            "baseline_id": "NO_TRADE_SAME_CS_OPPORTUNITIES",
            "baseline_type": "NO_TRADE",
            "policy": "remain flat for every eligible 97/98 price/regime/phase cell",
            "comparability": "same roots, q cells, persistence, fees and settlement coverage",
            "secondary_baselines": [
                "CS_94_TO_96_PLACEBO",
                "CS_BID_PAYOFF_CONTROL_ON_EXACT_ASK_FILL_IDS",
            ],
        }
    if action_profile == "EARLY_EXIT":
        return {
            "baseline_id": "WAIT_UNTIL_FREEZE_OR_TERMINAL",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": (
                "retain the same reconciled position through the precursor and "
                "follow the frozen later exit/terminal rule"
            ),
            "comparability": "same position, roots, clocks, fees and terminal paths",
            "secondary_baselines": ["IMMEDIATE_EXIT_BEFORE_PRECURSOR"],
        }
    if action_profile == "EXPIRY_ROLL":
        return {
            "baseline_id": "HOLD_ORIGINAL_EXPOSURE",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": "do not roll; retain the exact original exposure under its frozen exit/terminal rule",
            "comparability": "same starting position, roots, capital clock and legal terminal states",
            "secondary_baselines": ["IMMEDIATE_EXIT_ORIGINAL_EXPOSURE"],
        }
    if action_profile == "CHILD_HEDGE":
        return {
            "baseline_id": "UNHEDGED_CONFIRMED_CHILD",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": "run the identical confirmed child without the proposed hedge",
            "comparability": "same child fills, roots, capital clock, exits and terminal states",
            "secondary_baselines": ["NO_TRADE_SAME_OPPORTUNITIES"],
        }
    if action_profile == "MVE_DIRECT":
        return {
            "baseline_id": "NO_MVE_PACKAGE_TRADE_SAME_OPPORTUNITIES",
            "baseline_type": "NO_TRADE",
            "policy": "submit no MVE package or hedge legs and record zero cash flow",
            "comparability": "same package catalog, clocks, collateral and mapping exclusions",
            "secondary_baselines": ["LEG_REPLICATION_ONLY_IF_SEPARATELY_REGISTERED"],
        }
    if action_profile == "COMMERCIAL_ROC":
        return {
            "baseline_id": "UNCHANGED_CONFIRMED_CHILD_WITHOUT_COMMERCIAL_GATE",
            "baseline_type": "CUSTOM_EXECUTABLE",
            "policy": (
                "run the same immutable confirmed child at its frozen size and "
                "risk limits without the tested commercial continue/scale/stop gate"
            ),
            "comparability": (
                "same child fingerprint, roots, fills, capital clock, risk limits "
                "and fully allocated operating-cost ledger"
            ),
            "secondary_baselines": ["NO_TRADE_SAME_ACCOUNTING_PERIODS"],
        }
    return {
        "baseline_id": "NO_TRADE_SAME_OPPORTUNITIES",
        "baseline_type": "NO_TRADE",
        "policy": "record every eligible opportunity with zero cash flow",
        "comparability": "same roots, clock, capital availability and data exclusions",
        "secondary_baselines": [],
    }


def _pnl_for(action_profile: str) -> dict[str, Any]:
    if action_profile in {"NO_ACTION", "MODEL_COMPONENT", "CONTROL_ONLY"}:
        return {
            "status": "NOT_DEFINED_NOT_A_STRATEGY",
            "basis": "NONE",
            "formula": None,
            "incremental_formula": None,
            "aggregation_unit": None,
            "zero_fill_treatment": None,
            "capacity_rule": None,
        }
    if action_profile in {"MAKER_FILTER", "RFQ_GUARD"}:
        formula = (
            "NetPnL_policy = cash_in - cash_out - exact_fees - realized_variable_costs "
            "+ terminal_value(residual_inventory)"
        )
        incremental = (
            "DeltaPnL = NetPnL_signal_gated_maker - NetPnL_unfiltered_same_maker"
        )
    elif action_profile == "RFQ_MAKER":
        formula = (
            "NetPnL = RFQ_and_hedge_cash_in - RFQ_and_hedge_cash_out "
            "- exact_RFQ_and_CLOB_fees + terminal_value(residual_inventory)"
        )
        incremental = "DeltaPnL = NetPnL_RFQ_quote_policy - 0_for_no_quote_baseline"
    elif action_profile == "MVE_DIRECT":
        formula = (
            "NetPnL = MVE_package_and_hedge_cash_in - "
            "MVE_package_and_hedge_cash_out - exact_package_and_leg_fees "
            "- partial_package_orphan_unwind_cost + joint_terminal_value"
        )
        incremental = (
            "DeltaPnL = NetPnL_MVE_direct_policy - "
            "0_for_same_opportunity_no_MVE_trade_baseline"
        )
    elif action_profile == "RELATIVE_VALUE":
        formula = (
            "NetPnL = sum(executed_leg_cashflows) - sum(exact_leg_fees) "
            "- hedge_and_partial_leg_cost + joint_terminal_value"
        )
        incremental = "DeltaPnL = NetPnL_full_leg_policy - 0_for_no_trade_baseline"
    elif action_profile == "ROUTER":
        formula = (
            "NetPnL = sum(root_deduped_child_cashflows) - exact_fees "
            "- switch_cost + joint_terminal_value"
        )
        incremental = "DeltaPnL = NetPnL_router - NetPnL_best_fixed_train_only_child"
    elif action_profile == "COMMERCIAL_ROC":
        formula = (
            "CommercialNetPnL = child_realized_and_terminal_NetPnL "
            "- exchange_fees - data_provider_license_API_cost "
            "- cloud_compute_storage_egress_cost - operations_labor_oncall_cost "
            "- incident_loss_and_remediation_cost - capital_and_collateral_cost"
        )
        incremental = (
            "DeltaCommercialPnL = CommercialNetPnL_ROC_gate - "
            "CommercialNetPnL_same_child_without_commercial_gate"
        )
    elif action_profile == "SIZE_RISK":
        formula = (
            "NetPnL = child_executed_cashflows - exact_fees - impact "
            "- tail_and_unwind_cost + terminal_value"
        )
        incremental = "DeltaPnL = NetPnL_sized_policy - NetPnL_same_child_at_one_contract"
    elif action_profile == "TERMINAL_ENTRY":
        formula = (
            "NetPnL = terminal_or_exit_cash_in - entry_cash_out - exact_entry_and_exit_fees "
            "- realized_variable_costs"
        )
        incremental = "DeltaPnL = NetPnL_terminal_entry - 0_for_flat_no_trade_baseline"
    elif action_profile == "CS_ASK":
        formula = (
            "NetPnL_per_strict_fill = q - Y_high_outcome_terminal "
            "- exact_net_fee_for_low_side_buy"
        )
        incremental = (
            "DeltaPnL = sum(NetPnL_on_exact_CS_ASK_fills) - "
            "0_for_same_opportunity_no_trade_baseline"
        )
    elif action_profile == "T90_REBUILD":
        formula = (
            "NetPnL = parent_and_rebuild_executed_cash_in - executed_cash_out "
            "- exact_fees - forced_exit_cost + terminal_value(residual_inventory)"
        )
        incremental = (
            "DeltaPnL = NetPnL_T90_with_one_rebuild - "
            "NetPnL_same_root_T90_never_reenter"
        )
    elif action_profile == "EARLY_EXIT":
        formula = (
            "NetPnL = executable_exit_cash - original_position_cost - exact_fees "
            "- realized_exit_cost"
        )
        incremental = (
            "DeltaPnL = NetPnL_early_reduce_only_exit - "
            "NetPnL_same_position_wait_until_freeze_or_terminal"
        )
    elif action_profile == "EXPIRY_ROLL":
        formula = (
            "NetPnL = old_close_cashflow + new_exposure_cashflow - all_leg_fees "
            "- orphan_and_carry_cost + terminal_value(new_residual)"
        )
        incremental = (
            "DeltaPnL = NetPnL_roll_policy - NetPnL_hold_original_exposure"
        )
    elif action_profile == "CHILD_HEDGE":
        formula = (
            "NetPnL = child_cashflows + hedge_cashflows - all_leg_fees "
            "- partial_hedge_cost + joint_terminal_value"
        )
        incremental = (
            "DeltaPnL = NetPnL_child_plus_hedge - NetPnL_unhedged_same_child"
        )
    else:
        formula = (
            "NetPnL = executed_cash_in - executed_cash_out - exact_venue_fees "
            "- realized_variable_costs + terminal_value(residual_inventory)"
        )
        incremental = "DeltaPnL = NetPnL_strategy - NetPnL_registered_baseline"
    result = {
        "status": "DEFINED_NOT_YET_ESTIMABLE",
        "basis": "PATHWISE_AFTER_COST",
        "formula": formula,
        "incremental_formula": incremental,
        "aggregation_unit": "ROOT_EVENT_AND_UTC_DAY",
        "denominators": [
            "per_opportunity",
            "per_trigger",
            "per_fill",
            "per_contract",
            "per_day",
            "per_peak_capital",
        ],
        "zero_fill_treatment": "eligible no-trigger/no-fill/no-accept rows contribute zero",
        "capacity_rule": "no linear extrapolation beyond executable depth and tested size",
        "markout_rule": "mid/markout is diagnostic and never cash PnL",
    }
    if action_profile == "COMMERCIAL_ROC":
        result["roc_definition"] = (
            "CommercialROC = CommercialNetPnL / max(peak_committed_capital, "
            "fully_allocated_cash_operating_cost); report both denominators separately"
        )
        result["cost_allocation_rule"] = (
            "Fully allocated provider, cloud, labor/on-call and incident costs "
            "use each sealed UTC accounting period, then measured card/root usage; "
            "shared fixed costs use a frozen driver and never disappear from the denominator."
        )
    return result


def _costs_for(
    action_profile: str,
    external_dependency: str,
) -> list[dict[str, str]]:
    if action_profile in {"NO_ACTION", "MODEL_COMPONENT", "CONTROL_ONLY"}:
        return []
    costs = copy.deepcopy(COMMON_COST_COMPONENTS)
    if action_profile == "RFQ_MAKER":
        costs.append(
            {
                "type": "RFQ_SELECTION_AND_CONFIRMATION",
                "role": "RISK_DIAGNOSTIC",
                "status": "MISSING",
                "rule": "requester side selection, confirmation, execution and no-accept loss",
            }
        )
    if external_dependency == "REQUIRED":
        costs.append(
            {
                "type": "DATA_API",
                "role": "OPERATING_EXPENSE",
                "status": "MISSING",
                "rule": "provider subscription, redistribution and historical backfill cost",
            }
        )
    if action_profile == "MVE_DIRECT":
        costs.append(
            {
                "type": "MVE_PACKAGE_EXECUTION_AND_PARTIAL",
                "role": "EXECUTION_MODEL_INPUT",
                "status": "MISSING",
                "rule": (
                    "private package quote/order/fill fees plus worst-sequence "
                    "partial-package and orphan-leg unwind cost"
                ),
            }
        )
    if action_profile == "ROUTER":
        costs.append(
            {
                "type": "POLICY_SWITCH_AND_OVERLAP",
                "role": "EXECUTION_MODEL_INPUT",
                "status": "MISSING",
                "rule": "switch latency plus common-root capital and co-tail use",
            }
        )
    if action_profile == "COMMERCIAL_ROC":
        costs.extend(
            [
                {
                    "type": "DATA_PROVIDER_LICENSE_API",
                    "role": "OPERATING_EXPENSE",
                    "status": "MISSING",
                    "rule": "invoice-backed provider, license, API and historical-data allocation",
                },
                {
                    "type": "CLOUD_COMPUTE_STORAGE_EGRESS",
                    "role": "OPERATING_EXPENSE",
                    "status": "MISSING",
                    "rule": "metered compute, storage, request and egress allocation by UTC period and card",
                },
                {
                    "type": "OPS_LABOR_AND_INCIDENT",
                    "role": "OPERATING_EXPENSE",
                    "status": "MISSING",
                    "rule": "labor, on-call, incident loss and remediation under a frozen allocation rule",
                },
            ]
        )
    return costs


def _data_profile(name: str) -> dict[str, Any]:
    profiles: dict[str, dict[str, Any]] = {
        "L1": {
            "inputs": ["KALSHI_L1", "PUBLIC_TRADES", "CATALOG"],
            "can_compute_now": {
                "signal_event": "YES",
                "conditional_markout": "YES",
                "strict_fill": "PARTIAL",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "8 exact V3 dates can describe L1/trade signal incidence and "
                "conditional markout; this is not executable PnL."
            ),
            "missing": [
                "strategy-specific stateful strict-fill replay",
                "versioned exact fee/rounding at each simulated fill",
                "measured placement/cancel/exit latency",
                "untouched quality dates for validation",
            ],
            "acquisition_paths": [
                "existing sealed Kalshi L1/trade pipeline",
                "shadow engine actual decision and order-state ledgers",
                "Kalshi private fills for later micro-live reconciliation",
            ],
            "blockers": [
                "FILL_MODEL_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
                "INSUFFICIENT_QUALITY_DATES",
            ],
        },
        "L2": {
            "inputs": ["SEQUENCE_VALID_KALSHI_L2", "KALSHI_L1", "PUBLIC_TRADES"],
            "can_compute_now": {
                "signal_event": "YES",
                "conditional_markout": "YES",
                "strict_fill": "PARTIAL",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Depth/refill/retreat signals are descriptive on clean dates "
                "2026-07-12, 2026-07-15 and 2026-07-17 only."
            ),
            "missing": [
                "own queue position and order acknowledgements",
                "strategy-specific strict-fill and cancel-race replay",
                "exact fees, latency and at least 20 independent clean days",
            ],
            "acquisition_paths": [
                "continue sequence-valid L2 capture and daily quality receipts",
                "own-order shadow/micro-live calibration",
                "private order/fill/cancel acknowledgement ledger",
            ],
            "blockers": [
                "OWN_ORDER_CALIBRATION_MISSING",
                "FILL_MODEL_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
                "INSUFFICIENT_QUALITY_DATES",
            ],
        },
        "L1_L2": {
            "inputs": [
                "KALSHI_L1",
                "PUBLIC_TRADES",
                "SEQUENCE_VALID_KALSHI_L2",
                "CATALOG",
            ],
            "can_compute_now": {
                "signal_event": "YES",
                "conditional_markout": "YES",
                "strict_fill": "PARTIAL",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "L1 signal and 3-clean-day L2 mechanism measurements; no "
                "own-order, fee-after or realized PnL."
            ),
            "missing": [
                "own fills/queue/cancel-effective state",
                "exact fee and latency path",
                "additional clean L2 dates",
            ],
            "acquisition_paths": [
                "existing L1/L2 collectors",
                "stateful shadow order ledger",
                "private fills and acknowledgements after separate authority",
            ],
            "blockers": [
                "OWN_ORDER_CALIBRATION_MISSING",
                "FILL_MODEL_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
                "INSUFFICIENT_QUALITY_DATES",
            ],
        },
        "L1_PRIVATE": {
            "inputs": [
                "KALSHI_L1",
                "PUBLIC_TRADES",
                "CATALOG",
                "FROZEN_PAST_ONLY_FAIR_AND_VOLATILITY",
                "PRIVATE_ACCOUNT_ORDERS_POSITIONS_FILLS",
            ],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "PARTIAL",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Public L1/trades can produce candidate fair and volatility "
                "features, but the reservation-skew signal is not observable "
                "without authoritative point-in-time own inventory and orders."
            ),
            "missing": [
                "authoritative point-in-time position and resting-order exposure",
                "immutable child maker definition and fair/volatility specification",
                "own order acknowledgements, fills, fees and reconciliation",
            ],
            "acquisition_paths": [
                "existing sealed Kalshi L1/trade/catalog pipeline",
                "read-only private positions/open-orders/fills snapshots",
                "system-owned shadow order ledger with frozen fair/volatility model",
            ],
            "blockers": [
                "PRIVATE_ACCOUNT_STATE_MISSING",
                "OWN_ORDER_CALIBRATION_MISSING",
                "FILL_MODEL_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
                "INSUFFICIENT_QUALITY_DATES",
            ],
        },
        "FAMILY": {
            "inputs": ["CATALOG", "FAMILY_MAP", "KALSHI_L1", "KALSHI_L2"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "PARTIAL",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Market-graph price residuals are monitors only; the prior "
                "B03 run found zero payout-exhaustive, fill-feasible candidates."
            ),
            "missing": [
                "authoritative exhaustive payout-state mapping",
                "all-leg exact fees and synchronized executable depth",
                "partial-leg/orphan unwind engine",
            ],
            "acquisition_paths": [
                "Kalshi catalog/market rules and settlement metadata",
                "manual-plus-machine audited payout mapping",
                "stateful multi-leg shadow engine and private fills",
            ],
            "blockers": [
                "PAYOUT_EXHAUSTIVENESS_MISSING",
                "MULTI_LEG_ENGINE_MISSING",
                "FEE_UNKNOWN",
            ],
        },
        "TERMINAL": {
            "inputs": ["KALSHI_L1", "CATALOG", "SETTLEMENT_AND_LIFECYCLE"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "YES",
                "strict_fill": "PARTIAL",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "PARTIAL",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Price/lifecycle paths are observable; terminal coverage is not "
                "yet exact-release-bound and exhaustive for every legal state."
            ),
            "missing": [
                "100% resolved/void/retire/postpone terminal state coverage",
                "exact fill selection and fees",
                "capital-hours and residual inventory reconciliation",
            ],
            "acquisition_paths": [
                "Kalshi settlements and lifecycle APIs captured append-only",
                "versioned market rules and settlement-source mapping",
                "private fills for realized reconciliation",
            ],
            "blockers": [
                "SETTLEMENT_MISSING",
                "FILL_MODEL_MISSING",
                "FEE_UNKNOWN",
            ],
        },
        "RFQ_PUBLIC": {
            "inputs": ["FRESH_RFQ_CREATED_DELETED", "KALSHI_L1", "KALSHI_L2"],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Code for D01-D07 exists, but the unified durable release chain "
                "has not produced a locally available eligible RFQ result."
            ),
            "missing": [
                "eligible fresh exact-version RFQ releases",
                "same-date exact CLOB joins",
                "own CLOB order counterfactual for risk-off PnL",
            ],
            "acquisition_paths": [
                "finish RFQ unified seal/verify/release chain",
                "run D01-D07 against same-date L1/L2",
                "capture own shadow maker order state",
            ],
            "blockers": [
                "RFQ_PUBLIC_DATA_UNRELEASED",
                "FILL_MODEL_MISSING",
                "LATENCY_UNMEASURED",
            ],
        },
        "RFQ_PRIVATE": {
            "inputs": [
                "FRESH_RFQ_CREATED_DELETED",
                "PRIVATE_QUOTE_ACCEPT_CONFIRM_EXECUTE",
                "PRIVATE_FILLS_FEES",
                "KALSHI_L2",
            ],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Public RFQ create/delete cannot establish direction, acceptance, "
                "fill, fee, inventory, hedge or realized PnL."
            ),
            "missing": [
                "separate RFQ quote-write authority",
                "private accepted_side/confirm/executed lifecycle",
                "quote-to-fill correlation, exact fees and hedge depth",
            ],
            "acquisition_paths": [
                "separately authorized RFQ shadow/micro-live participant",
                "communications private events",
                "Kalshi portfolio fills and position/settlement reconciliation",
            ],
            "blockers": [
                "RFQ_PUBLIC_DATA_UNRELEASED",
                "RFQ_PRIVATE_EVENTS_MISSING",
                "VENUE_PERMISSION_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
            ],
        },
        "MVE_PRIVATE": {
            "inputs": [
                "EXACT_VERSION_MVE_CATALOG_AND_RULES",
                "AUDITED_MVE_LEG_ORIENTATION_AND_PAYOUT_MATRIX",
                "PRIVATE_MVE_PACKAGE_ORDER_ACK_FILL_FEE",
                "PRIVATE_LEG_FILLS_POSITIONS_AND_SETTLEMENT",
                "SEQUENCE_VALID_KALSHI_L2",
            ],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "No admitted private MVE package lifecycle, exact leg/payout "
                "mapping, or package-to-position reconciliation is bound. RFQ "
                "public/private events are not an MVE substitute."
            ),
            "missing": [
                "exact-version MVE catalog, leg orientation and payout matrix",
                "private MVE package submit/ack/cancel/fill/fee lifecycle",
                "synchronized hedge-leg L2, fills, positions and settlement",
            ],
            "acquisition_paths": [
                "capture the admitted MVE catalog and rules append-only",
                "separately authorized private MVE execution/shadow receipts",
                "join package and leg events to positions, fees and settlements",
            ],
            "blockers": [
                "MVE_PRIVATE_EVENTS_MISSING",
                "INSTRUMENT_MAPPING_UNKNOWN",
                "PAYOUT_EXHAUSTIVENESS_MISSING",
                "MULTI_LEG_ENGINE_MISSING",
                "VENUE_PERMISSION_MISSING",
                "FEE_UNKNOWN",
                "LATENCY_UNMEASURED",
            ],
        },
        "EXTERNAL": {
            "inputs": ["EXTERNAL_SPORTS_ASOF", "KALSHI_L1", "KALSHI_L2"],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "No score, serve, injury, lineup or external-odds feed is present "
                "in the exact DeepResearch data. Price proxies are forbidden."
            ),
            "missing": [
                "licensed direct sports/odds source",
                "source event timestamp and local receive clocks",
                "versioned event/market mapping and point-in-time snapshots",
            ],
            "acquisition_paths": [
                "append-only Kalshi Milestones/Live Data capture for direct labels",
                "licensed provider historical backfill where point-in-time semantics exist",
                "forward commercial real-time feed with raw bytes and dual receive clocks",
            ],
            "blockers": [
                "EXTERNAL_SPORTS_DATA_MISSING",
                "EXTERNAL_API_UNVERIFIED",
                "TIMESTAMP_SEMANTICS_UNKNOWN",
            ],
        },
        "CATALOG": {
            "inputs": ["KALSHI_CATALOG_AND_MARKET_RULES"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": "Schema/coverage only; no action or cash PnL.",
            "missing": ["versioned semantics for any unsupported instrument"],
            "acquisition_paths": ["Kalshi public catalog/rules captured by exact version"],
            "blockers": [],
        },
        "MODEL": {
            "inputs": ["OWNER_EXPERIMENT_FEATURES_AND_LABEL"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "PARTIAL",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Chronological forecast comparisons are possible only after a "
                "specific owner, label and split are bound; a model is not a strategy."
            ),
            "missing": [
                "one owner experiment with frozen action and outcome label",
                "nested chronological split and simple-model baseline",
                "owner execution PnL",
            ],
            "acquisition_paths": ["create a separate G-method x owner experiment revision"],
            "blockers": ["ACTION_INCOMPLETE"],
        },
        "META": {
            "inputs": ["CONFIRMED_CHILD_STRATEGY_OUTPUTS", "COMMON_ROOT_LEDGER"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "Overlap/covariance descriptions are possible; a router or "
                "portfolio PnL is not estimable without confirmed children."
            ),
            "missing": [
                "at least one or two confirmed child strategies as applicable",
                "joint root-deduped execution and capital ledger",
                "switch/overlap/correlation costs",
            ],
            "acquisition_paths": [
                "complete child confirmation first",
                "joint shadow replay with shared order/risk ledger",
            ],
            "blockers": ["CONFIRMED_CHILD_STRATEGIES_MISSING"],
        },
        "PRIVATE": {
            "inputs": ["PRIVATE_ACCOUNT_ORDERS_POSITIONS_FILLS"],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": "Public research data cannot establish account ownership conflicts.",
            "missing": [
                "private open-order/position/fill ownership state",
                "manual/system precedence and STP receipts",
            ],
            "acquisition_paths": [
                "read-only private account snapshots",
                "system-owned order registry and reconciliation ledger",
            ],
            "blockers": ["PRIVATE_ACCOUNT_STATE_MISSING"],
        },
        "OUT_OF_SCOPE": {
            "inputs": [],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": "No computation in the current Sports program.",
            "missing": ["separate program authority and registry lineage"],
            "acquisition_paths": ["new operator-approved program, if ever desired"],
            "blockers": ["OUT_OF_SCOPE_CURRENT_PROGRAM"],
        },
        "INTERNAL_CONTROL": {
            "inputs": ["INTERNAL_SEALED_RESEARCH_DATA"],
            "can_compute_now": {
                "signal_event": "PARTIAL",
                "conditional_markout": "PARTIAL",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": "Diagnostic/control statistic only; no trading action.",
            "missing": [],
            "acquisition_paths": ["existing sealed research releases"],
            "blockers": [],
        },
        "OWNER_EVPI": {
            "inputs": [
                "IMMUTABLE_OWNER_CARD_ID_REVISION_DEFINITION_SHA",
                "OWNER_DECISION_AND_OPPORTUNITY_LEDGER",
                "FROZEN_OWNER_LOSS_FUNCTIONAL",
                "FROZEN_NON_CAUSAL_FUTURE_ORACLE_RULE",
                "KALSHI_L1",
                "PUBLIC_TRADES",
                "SEQUENCE_VALID_KALSHI_L2",
                "CATALOG",
            ],
            "can_compute_now": {
                "signal_event": "NO",
                "conditional_markout": "NO",
                "strict_fill": "NO",
                "fee_after_shadow_pnl": "NO",
                "settlement_pnl": "NO",
                "realized_pnl": "NO",
            },
            "currently_calculable": (
                "The future L1/L2 path exists, but EVPI is undefined until an "
                "immutable eligible owner card, its actual decision/opportunity "
                "ledger, loss functional and oracle rule are frozen."
            ),
            "missing": [
                "confirmed immutable owner card ID, revision and definition SHA",
                "owner decision/opportunity ledger including abstentions and zeros",
                "frozen loss functional and non-causal future-oracle rule",
            ],
            "acquisition_paths": [
                "confirm an eligible owner experiment first",
                "seal the owner's shadow decision/opportunity ledger by exact release",
                "preregister loss and oracle functions before future-path evaluation",
            ],
            "blockers": ["CONFIRMED_CHILD_STRATEGIES_MISSING"],
        },
    }
    return copy.deepcopy(profiles[name])


EXTERNAL_SOURCE_CATALOG = {
    "current_system_state": (
        "No direct external sports adapter, decoder, schema or capture is present. "
        "All external sports variables are MISSING until point-in-time evidence, "
        "clock semantics and license are independently verified."
    ),
    "sources": [
        {
            "source_id": "KALSHI_MILESTONES_LIVE_DATA",
            "kind": "DIRECT_SPORT_LABEL_AND_LIVE_STATE",
            "status": "AVAILABLE_API_NOT_CAPTURED",
            "official_docs": [
                "https://docs.kalshi.com/api-reference/milestone/get-milestones",
                "https://docs.kalshi.com/api-reference/live-data/get-live-data",
                "https://docs.kalshi.com/api-reference/live-data/get-multiple-live-data",
                "https://docs.kalshi.com/api-reference/live-data/get-game-stats",
            ],
            "historical_backfill": "SNAPSHOT_ONLY_NOT_POINT_IN_TIME",
            "live_use": "POSSIBLE_AFTER_APPEND_ONLY_CAPTURE_AND_CLOCK_AUDIT",
            "notes": (
                "Milestones can map sports entities to Kalshi events; Live Data "
                "can expose score/period/game state. Mutable current snapshots "
                "must never be backfilled as if known historically."
            ),
        },
        {
            "source_id": "THE_ODDS_API",
            "kind": "EXTERNAL_ODDS",
            "status": "CANDIDATE_NOT_LICENSED_OR_CAPTURED",
            "official_docs": [
                "https://the-odds-api.com/liveapi/guides/v4/",
            ],
            "historical_backfill": "PREGAME_SNAPSHOT_CANDIDATE",
            "live_use": "LATENCY_TOO_COARSE_FOR_UNAUDITED_FAST_ACTION",
            "notes": "Provider time, latency, commercial use and exact fields require receipt.",
        },
        {
            "source_id": "OPTICODDS",
            "kind": "ODDS_RESULTS_LINEUPS_INJURIES",
            "status": "CANDIDATE_NOT_LICENSED_OR_CAPTURED",
            "official_docs": [
                "https://developer.opticodds.com/docs/odds-api-getting-started-guide",
            ],
            "historical_backfill": "PREGAME_ODDS_CANDIDATE_WITH_RETENTION_LIMIT",
            "live_use": "SSE_CANDIDATE_AFTER_COMMERCIAL_AND_CLOCK_AUDIT",
            "notes": "Do not treat an endpoint name as verified availability.",
        },
        {
            "source_id": "SPORTRADAR",
            "kind": "PLAY_BY_PLAY_ROSTER_INJURY_AND_ODDS_PRODUCTS",
            "status": "CANDIDATE_NOT_LICENSED_OR_CAPTURED",
            "official_docs": [
                "https://developer.sportradar.com/",
            ],
            "historical_backfill": "SPORT_AND_PRODUCT_SPECIFIC_UNKNOWN_UNTIL_CONTRACT",
            "live_use": "CANDIDATE_AFTER_COMMERCIAL_AND_CLOCK_AUDIT",
            "notes": "Product, league, timestamps, storage and trading use require verification.",
        },
        {
            "source_id": "PINNACLE_DIRECT",
            "kind": "EXTERNAL_ODDS",
            "status": "NOT_AVAILABLE_BY_DEFAULT_LEGAL_AUTH_REQUIRED",
            "official_docs": [],
            "historical_backfill": "NO_DEFAULT_PUBLIC_ACCESS",
            "live_use": "NO_DEFAULT_PUBLIC_ACCESS",
            "notes": "No scraping or assumed access.",
        },
        {
            "source_id": "NOAA_NWS_NCEI",
            "kind": "WEATHER_CONTROL",
            "status": "PUBLIC_CANDIDATE_NOT_MAPPED",
            "official_docs": [
                "https://www.weather.gov/documentation/services-web-api",
                "https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation",
            ],
            "historical_backfill": "POSSIBLE_AFTER_VENUE_STATION_MAPPING",
            "live_use": "SLOW_CONTROL_NOT_LOW_LATENCY_TRIGGER",
            "notes": "Venue coordinates, roof state, QC delay and receive clock remain required.",
        },
    ],
}


SOURCE_BINDINGS = [
    {
        "role": "ATOMIC_DIRECTION_AUTHORITY",
        "path": "Deepresearch V3/registry/sources/"
        "DEEP03_ATOMIC_DIRECTION_AUTHORITY_V1.json",
        "sha256": "85db70587525ab34af7b5e1cba8bb70929581ea2ff64eb075eabaf5f98ea337e",
        "local_required": True,
        "note": (
            "Local immutable extraction of plan section 4.7.2; it binds the "
            "origin document SHA ded84065... and the exact 106-ID universe."
        ),
    },
    {
        "role": "CURRENT_RESULT_EVIDENCE",
        "path": "Deepresearch V3/run_2026-07-22_D3-W2A/RESULTS.json",
        "sha256": "e2265284704a828ec9dd0c700c66f16bdf11c9729530b583e9578561e27782f6",
        "local_required": True,
        "note": "Descriptive results only; no card inherits a PnL claim from this file.",
    },
    {
        "role": "CURRENT_ESTIMABILITY_EVIDENCE",
        "path": "Deepresearch V3/run_2026-07-22_D3-W2A/ESTIMABILITY_PREFLIGHT.json",
        "sha256": "50a63f633db2edccfc315d482684d5d6ea488fdbd5526106e9a2c60b5831c7de",
        "local_required": True,
        "note": "Used to distinguish observable signals from executable economics.",
    },
    {
        "role": "CURRENT_L2_SIGNAL_EVIDENCE",
        "path": "Deepresearch V3/run_2026-07-22_D3-W2A/"
        "FULLSCOPE_L2_REPORT_TABLES.json",
        "sha256": "e97fe0376cf4700829ed5afccc6eeaa10f4c368b417d335b0e1af7df490d0f52",
        "local_required": True,
        "note": "Direct source for A03 refill and A07B retreat/control observations.",
    },
    {
        "role": "CURRENT_EXCLUSION_EVIDENCE",
        "path": "Deepresearch V3/run_2026-07-22_D3-W2A/EXCLUSION_WATERFALL.json",
        "sha256": "3e8136820158d320bbc1df52edb7239af9d29c281308c7c97dad3297d0deef4a",
        "local_required": True,
        "note": "Direct source for the C04 payout/fill feasibility exclusion.",
    },
]

AVAILABLE_RELEASE_DATES = [
    "2026-07-10",
    "2026-07-11",
    "2026-07-12",
    "2026-07-13",
    "2026-07-14",
    "2026-07-15",
    "2026-07-16",
    "2026-07-17",
]
L2_ESTIMATOR_CLEAN_DATES = ["2026-07-12", "2026-07-15", "2026-07-17"]

OBSERVED_SIGNAL_EVIDENCE = {
    "A01-SPREAD-CAPTURE": {
        "status": "DESCRIPTIVE_GROSS_SIGNAL_ONLY",
        "values": {
            "pooled_signed_markout_logodds": {
                "1s": 0.038,
                "5s": 0.046,
                "30s": 0.056,
            },
            "interpretation": (
                "Pooled half-spread exceeded signed markout in most large-sport "
                "cells, before fees, fills, latency and executable exit."
            ),
        },
        "source": "RESULTS.json::base_methods[D3-B01-MARKOUT]",
    },
    "A03-DEPLETION-REFILL": {
        "status": "DESCRIPTIVE_CLEAN_L2_DATES_ONLY",
        "values": {
            "dates": copy.deepcopy(L2_ESTIMATOR_CLEAN_DATES),
            "refill_hazard_0_100ms": [0.245, 0.293, 0.308],
            "cumulative_refill_probability_1s": [0.572, 0.505, 0.520],
            "interpretation": (
                "Displayed depth often refilled quickly; this is not own-order "
                "queue survival, fill probability or PnL."
            ),
        },
        "source": "FULLSCOPE_L2_REPORT_TABLES.json::refill_hazard",
    },
    "A07B-COORDINATED-RETREAT": {
        "status": "OUTCOME_NOT_ESTIMABLE_FROM_DELIVERED_TABLES",
        "values": {
            "retreat_episode_rows": 853294,
            "matched_control_rates": [0.019, 0.019, 0.016],
            "depth_smd": [-0.32, -0.31, -0.40],
            "interpretation": (
                "Event extraction exists, but delivered tables lack the required "
                "event-minus-control price outcome and depth matching is imbalanced."
            ),
        },
        "source": "FULLSCOPE_L2_REPORT_TABLES.json::match_coverage",
    },
    "A11-ONE-SIDED-PROVISION": {
        "status": "DESCRIPTIVE_NARROW_PRESTART_SIGNAL_ONLY",
        "values": {
            "tennis_prestart_duration_seconds": {"p50": 1.001, "p95": 30.999},
            "basketball_prestart_p50_seconds": 5.001,
            "interpretation": (
                "Only a narrow pre-scheduled-start cell looked transient; the "
                "broad one-sided fade was contradicted and no fill/PnL was measured."
            ),
        },
        "source": "RESULTS.json::base_methods[D3-B02-ONESIDE]",
    },
    "C04-SAME-EVENT-FAMILY-COHERENCE": {
        "status": "NOT_ESTIMABLE",
        "values": {
            "candidate_families": 28080,
            "payout_exhaustive_survivors": 0,
            "fee_and_fill_feasible_survivors": 0,
        },
        "source": "EXCLUSION_WATERFALL.json::D3-B03-XMKT",
    },
}

OVERLAP_PRECEDENCE_BY_EXPERIMENT = {
    "B08-CONTINUOUS-PRICE-CALIBRATION": (
        "B08 owns only lifecycle opportunities outside "
        "E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW; it abstains inside E04's window."
    ),
    "E04-SETTLEMENT-CALIBRATION": (
        "E04 exclusively owns E04_PRE_FREEZE_NEAR_TERMINAL_WINDOW; B08 is "
        "ineligible there, and common-root capital/PnL is counted once."
    ),
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_card_seeds() -> dict[str, dict[str, Any]]:
    # Support both ``python tools/experiment_registry.py`` and module imports.
    try:
        from experiment_registry_seeds import CARD_SEEDS
    except ImportError:  # pragma: no cover - exercised by module-style imports
        from tools.experiment_registry_seeds import CARD_SEEDS

    return copy.deepcopy(CARD_SEEDS)


EXTERNAL_VARIABLES_BY_EXPERIMENT = {
    "B10-INPLAY-PROXY-PRICE-EROSION": [
        "official game live/pre/post status",
        "period or set/inning/round",
        "score and authoritative game clock",
    ],
    "C05-SOCCER-POISSON": [
        "fixture identity and kickoff",
        "goals and match state",
        "team/lineup inputs if used by the frozen Poisson specification",
    ],
    "F09-TENNIS-BO3-BO5": [
        "authoritative best-of-three or best-of-five format",
        "set/game/point score and server if used",
    ],
    "F11-GOLF-THIN-LONG-LIFECYCLE": [
        "tournament round, cut status and field status",
    ],
    "F12-EXTERNAL-ODDS-LEADLAG": [
        "bookmaker, market, selection and decimal price",
        "provider price-change event time and suspension state",
    ],
    "F13-EXTERNAL-SPORT-STATE": [
        "sport-specific score, period/set/inning/round and game clock",
        "serve, possession, outs/bases, lineup or injury only when named by the card",
    ],
    "F14-PROVIDER-PRICE-LATENCY-TIER": [
        "same odds update from each named provider",
        "provider event time, provider publication time and local receive time",
    ],
    "F15-BLINDNESS-EVPI": [
        "the exact external variables available to the informed policy",
        "provider event/publication time and local receive time",
    ],
    "F16-COURTSIDE-SPEED-OUT-OF-SCOPE": [
        "authoritative venue event time and courtside receive time",
    ],
}


def _external_contract(dependency: str, atomic_id: str) -> dict[str, Any]:
    if dependency == "NOT_REQUIRED":
        return {
            "dependency": dependency,
            "availability": "NOT_APPLICABLE",
            "required_variables": [],
            "approved_source": None,
            "provider_candidates": [],
            "asof_clock_status": "NOT_APPLICABLE",
            "license_status": "NOT_APPLICABLE",
            "historical_backfill_status": "NOT_APPLICABLE",
            "missing_policy": "NOT_APPLICABLE",
            "proxy_policy": "PRICE_IS_NOT_SPORT_STATE",
            "blockers": [],
        }
    if dependency == "OPTIONAL_REFINEMENT":
        required_variables = EXTERNAL_VARIABLES_BY_EXPERIMENT.get(
            atomic_id,
            ["the explicitly named direct sports-state refinement"],
        )
        return {
            "dependency": dependency,
            "availability": "MISSING_OPTIONAL_REFINEMENT",
            "required_variables": required_variables,
            "approved_source": None,
            "provider_candidates": [
                "KALSHI_MILESTONES_LIVE_DATA",
                "OPTICODDS",
                "SPORTRADAR",
            ],
            "asof_clock_status": "UNVERIFIED",
            "license_status": "UNVERIFIED",
            "historical_backfill_status": "UNVERIFIED_POINT_IN_TIME",
            "missing_policy": "DO_NOT_IMPUTE_CORE_MAY_RUN_WITHOUT_FIELD",
            "proxy_policy": (
                "The core experiment keeps its internal proxy label and never "
                "renames it as actual sports state."
            ),
            "blockers": [],
        }
    if dependency == "REQUIRED":
        required_variables = list(
            EXTERNAL_VARIABLES_BY_EXPERIMENT.get(
                atomic_id,
                ["direct event or game state named by the experiment"],
            )
        )
        for required in (
            "provider event/publication timestamp",
            "local receive wall clock and monotonic clock",
            "versioned provider-event-to-Kalshi root/market/outcome mapping",
        ):
            if required not in required_variables:
                required_variables.append(required)
        return {
            "dependency": dependency,
            "availability": "MISSING",
            "required_variables": required_variables,
            "approved_source": None,
            "provider_candidates": [
                "KALSHI_MILESTONES_LIVE_DATA",
                "THE_ODDS_API",
                "OPTICODDS",
                "SPORTRADAR",
            ],
            "asof_clock_status": "UNVERIFIED",
            "license_status": "UNVERIFIED",
            "historical_backfill_status": "UNVERIFIED_POINT_IN_TIME",
            "missing_policy": "BLOCK_NO_IMPUTE",
            "proxy_policy": (
                "Do not infer score, serve, inning, pitch, injury, lineup, "
                "round, cut, or external odds from Kalshi price/order flow."
            ),
            "blockers": [
                "EXTERNAL_SPORTS_DATA_MISSING",
                "EXTERNAL_API_UNVERIFIED",
                "TIMESTAMP_SEMANTICS_UNKNOWN",
            ],
        }
    if dependency in {"INHERIT_OWNER", "INHERIT_CHILDREN"}:
        return {
            "dependency": dependency,
            "availability": "UNRESOLVED_UNTIL_BINDING",
            "required_variables": [
                "the exact external-data contract of the bound owner or child card"
            ],
            "approved_source": None,
            "provider_candidates": [],
            "asof_clock_status": "INHERITED",
            "license_status": "INHERITED",
            "historical_backfill_status": "INHERITED",
            "missing_policy": "BLOCK_IF_BOUND_CARD_REQUIRES_IT",
            "proxy_policy": "PRICE_IS_NOT_SPORT_STATE",
            "blockers": [],
        }
    raise ValueError(f"unknown external dependency: {dependency}")


def _study_dates(data_profile: str, signal_event_status: str) -> dict[str, Any]:
    l2_dependent = data_profile in {
        "L2",
        "L1_L2",
        "FAMILY",
        "MVE_PRIVATE",
        "OWNER_EVPI",
    }
    potential_dates = (
        copy.deepcopy(L2_ESTIMATOR_CLEAN_DATES)
        if l2_dependent
        else copy.deepcopy(AVAILABLE_RELEASE_DATES)
    )
    return {
        "available_exact_release_dates": copy.deepcopy(AVAILABLE_RELEASE_DATES),
        "eligible_for_current_signal_description": (
            potential_dates if signal_event_status == "YES" else []
        ),
        "potential_dates_after_remaining_signal_gates_close": (
            potential_dates if signal_event_status == "PARTIAL" else []
        ),
        "signal_event_status": signal_event_status,
        "l2_estimator_clean_dates": copy.deepcopy(L2_ESTIMATOR_CLEAN_DATES),
        "quality_rule": (
            "L2 estimands use only 2026-07-12/15/17. Damaged L2 dates "
            "2026-07-13/14/16 stay in the coverage ledger only; 2026-07-10/11 "
            "are L2-missing. Every experiment re-runs its own input gate."
        ),
        "confirmation_status": "NO_UNTOUCHED_CONFIRMATION_COHORT_ALLOCATED",
    }


def _cost_blockers(costs: list[dict[str, str]]) -> list[str]:
    blocker_by_cost_type = {
        "EXCHANGE_MAKER_FEE": "FEE_UNKNOWN",
        "EXCHANGE_TAKER_FEE": "FEE_UNKNOWN",
        "SLIPPAGE_MARKET_IMPACT": "FILL_MODEL_MISSING",
        "PLACE_CANCEL_EXIT_LATENCY": "LATENCY_UNMEASURED",
        "INVENTORY_TERMINAL_VOID": "SETTLEMENT_MISSING",
        "COLLATERAL_AND_CAPITAL": "RISK_GATES_MISSING",
        "RFQ_SELECTION_AND_CONFIRMATION": "RFQ_PRIVATE_EVENTS_MISSING",
        "MVE_PACKAGE_EXECUTION_AND_PARTIAL": "MVE_PRIVATE_EVENTS_MISSING",
        "DATA_API": "EXTERNAL_API_UNVERIFIED",
        "POLICY_SWITCH_AND_OVERLAP": "RISK_GATES_MISSING",
        "DATA_PROVIDER_LICENSE_API": "OPERATING_COST_UNKNOWN",
        "CLOUD_COMPUTE_STORAGE_EGRESS": "OPERATING_COST_UNKNOWN",
        "OPS_LABOR_AND_INCIDENT": "OPERATING_COST_UNKNOWN",
    }
    blockers: list[str] = []
    for cost in costs:
        if cost["status"] != "MISSING":
            continue
        blocker = blocker_by_cost_type.get(cost["type"])
        if blocker is None:
            raise ValueError(
                f"missing cost {cost['type']} has no fail-closed blocker mapping"
            )
        blockers.append(blocker)
    return blockers


def _expected_blockers(
    seed: dict[str, Any],
    data: dict[str, Any],
    external: dict[str, Any],
    costs: list[dict[str, str]],
    has_action: bool,
) -> list[str]:
    action_profile = seed["action_profile"]
    data_profile = seed["data_profile"]
    blockers = (
        list(data["blockers"])
        if has_action
        else _signal_level_blockers(data_profile)
    )
    if has_action:
        blockers.extend(_cost_blockers(costs))
    if action_profile in CHILD_DEPENDENT_ACTION_PROFILES:
        blockers.append("CONFIRMED_CHILD_STRATEGIES_MISSING")
    if has_action and seed["external_dependency"] == "REQUIRED":
        blockers.append("INSTRUMENT_MAPPING_UNKNOWN")
    blockers.extend(external["blockers"])
    blockers.extend(seed["extra_blockers"])
    return sorted(set(blockers))


def _gap_contract(atomic_id: str, blocker: str) -> dict[str, Any]:
    missing, acquisition, blocking_stage = BLOCKER_REMEDIATION_CATALOG[blocker]
    override = CARD_SPECIFIC_GAP_OVERRIDES.get((atomic_id, blocker))
    if override is not None:
        missing, acquisition = override
    return {
        "blocker": blocker,
        "blocking_stage": blocking_stage,
        "missing_evidence": missing,
        "acquisition_or_build_path": acquisition,
        "external_api_candidate_ids": (
            [
                "KALSHI_MILESTONES_LIVE_DATA",
                "THE_ODDS_API",
                "OPTICODDS",
                "SPORTRADAR",
            ]
            if blocker
            in {
                "EXTERNAL_SPORTS_DATA_MISSING",
                "EXTERNAL_API_UNVERIFIED",
            }
            else []
        ),
        "unknown_value_policy": "BLOCK_NOT_ZERO",
    }


def _signal_level_blockers(data_profile: str) -> list[str]:
    return {
        "RFQ_PUBLIC": ["RFQ_PUBLIC_DATA_UNRELEASED"],
        "RFQ_PRIVATE": [
            "RFQ_PUBLIC_DATA_UNRELEASED",
            "RFQ_PRIVATE_EVENTS_MISSING",
        ],
        "MVE_PRIVATE": [
            "MVE_PRIVATE_EVENTS_MISSING",
            "INSTRUMENT_MAPPING_UNKNOWN",
            "PAYOUT_EXHAUSTIVENESS_MISSING",
        ],
        "EXTERNAL": [
            "EXTERNAL_SPORTS_DATA_MISSING",
            "EXTERNAL_API_UNVERIFIED",
            "TIMESTAMP_SEMANTICS_UNKNOWN",
        ],
        "MODEL": ["ACTION_INCOMPLETE"],
        "META": ["CONFIRMED_CHILD_STRATEGIES_MISSING"],
        "PRIVATE": ["PRIVATE_ACCOUNT_STATE_MISSING"],
        "L1_PRIVATE": ["PRIVATE_ACCOUNT_STATE_MISSING"],
        "OUT_OF_SCOPE": ["OUT_OF_SCOPE_CURRENT_PROGRAM"],
        "OWNER_EVPI": ["CONFIRMED_CHILD_STRATEGIES_MISSING"],
    }.get(data_profile, [])


def _base_class(
    target_class: str,
    action: dict[str, Any],
    venue: dict[str, Any],
    baseline: dict[str, Any],
    pnl: dict[str, Any],
) -> str:
    has_action = bool(action["action_type"])
    if target_class == "RESEARCH_QUESTION":
        return "RESEARCH_QUESTION"
    if not has_action:
        return "SIGNAL"
    if (
        venue["execution_venue"] != "NONE"
        and baseline["baseline_id"] is not None
        and pnl["basis"] == "PATHWISE_AFTER_COST"
        and pnl["incremental_formula"]
    ):
        return "EXECUTABLE_STRATEGY"
    return "SIGNAL"


def _classification(
    target_class: str,
    base_class: str,
    blockers: list[str],
) -> dict[str, Any]:
    if blockers:
        computed = "BLOCKED"
        blocked_from = base_class
        lifecycle = "REGISTERED_BLOCKED"
    else:
        computed = base_class
        blocked_from = None
        lifecycle = (
            "REGISTERED_ACTION_SPEC_COMPLETE"
            if base_class == "EXECUTABLE_STRATEGY"
            else "REGISTERED_RESEARCH_ONLY"
        )
    return {
        "target_class": target_class,
        "computed_class": computed,
        "blocked_from": blocked_from,
        "strategy_eligible": computed == "EXECUTABLE_STRATEGY",
        "lifecycle": lifecycle,
        "promotion_rule": (
            "Only a new immutable revision with all critical blockers closed, "
            "an untouched confirmation cohort, and a fee-after pathwise PnL "
            "receipt may become a deployment candidate."
        ),
    }


def _claim_tier_for_observation(observation_status: str) -> str:
    if observation_status.startswith("DESCRIPTIVE"):
        return "DESCRIPTIVE_ONLY_NO_PNL"
    if "NOT_ESTIMABLE" in observation_status:
        return "NOT_ESTIMABLE"
    return "NONE"


def _build_card(atomic_id: str, seed: dict[str, Any]) -> dict[str, Any]:
    action_profile = seed["action_profile"]
    data_profile = seed["data_profile"]
    action = _action_profile(action_profile)
    action["profile_id"] = action_profile
    action["current_execution_authority"] = "NONE"
    venue = _venue_for(action_profile)
    baseline = _baseline_for(action_profile)
    pnl = _pnl_for(action_profile)
    costs = _costs_for(action_profile, seed["external_dependency"])
    data = _data_profile(data_profile)
    external = _external_contract(seed["external_dependency"], atomic_id)

    has_action = bool(action["action_type"])
    blockers = _expected_blockers(seed, data, external, costs, has_action)
    if "MISSING_SIGNAL_INPUT" in blockers:
        data["can_compute_now"]["signal_event"] = "NO"
        data["currently_calculable"] = (
            "The base feed may exist, but this experiment's named signal input "
            "is absent; current signal incidence and effect are not calculable."
        )
    critical_gaps = [
        _gap_contract(atomic_id, blocker) for blocker in blockers
    ]

    base_class = _base_class(
        seed["target_class"], action, venue, baseline, pnl
    )
    classification = _classification(
        seed["target_class"], base_class, blockers
    )
    trigger = seed["signal"]["trigger"]
    if has_action:
        action["registered_signal_to_action"] = (
            f"IF [{trigger}] THEN apply [{action_profile}] exactly as specified "
            "below; ELSE preserve the registered baseline/no-trade branch."
        )
    else:
        action["registered_signal_to_action"] = None
    falsifiable_statement = (
        (
            f"Under preregistered eligibility, if [{trigger}], the registered "
            f"{action_profile} policy must improve its primary estimand against "
            "the registered baseline; otherwise the hypothesis fails or "
            "remains non-strategic."
        )
        if has_action
        else (
            f"Under preregistered eligibility, test whether [{trigger}] changes "
            "the registered signal/outcome contrast versus its stated control. "
            "Failure of that contrast rejects the signal; no action or PnL "
            "claim follows from this card."
        )
    )
    observed_signal = copy.deepcopy(
        OBSERVED_SIGNAL_EVIDENCE.get(
            atomic_id,
            {
                "status": "NOT_MEASURED_IN_CURRENT_RELEASE",
                "values": None,
                "source": None,
            },
        )
    )

    card = {
        "identity": {
            "experiment_id": atomic_id,
            "registry_id": REGISTRY_ID,
            "version": 1,
            "family_code": atomic_id[0],
            "family_name": FAMILY_NAMES[atomic_id[0]],
            "lineage_status": "BREADTH_ATOMIC_DIRECTION",
            "source_atomic_id": atomic_id,
        },
        "classification": classification,
        "hypothesis": {
            "mechanism": seed["hypothesis"],
            "falsifiable_statement": falsifiable_statement,
            "primary_estimand": (
                "fee-after pathwise incremental NetPnL versus registered baseline"
                if has_action
                else "registered signal/outcome contrast; no PnL claim"
            ),
            "failure_conditions": {
                "signal": seed["signal"]["falsifier"],
                "execution": (
                    "No pessimistically valid fills/acceptances after latency, "
                    "queue, depth and reconciliation rules."
                    if has_action
                    else "No action is defined, so execution and PnL are not testable."
                ),
                "economics": (
                    "Out-of-sample incremental NetPnL after every registered "
                    "cost is <= 0 or its lower confidence bound misses the "
                    "frozen commercial threshold."
                    if has_action
                    else "NOT_APPLICABLE_UNTIL_ACTION_AND_PNL_EXIST"
                ),
                "capacity": (
                    "Positive one-contract economics disappear at the first "
                    "tested size step or peak-capital return is below threshold."
                    if has_action
                    else "NOT_APPLICABLE_UNTIL_ACTION_AND_PNL_EXIST"
                ),
            },
        },
        "signal": copy.deepcopy(seed["signal"]),
        "action": action,
        "venue": venue,
        "baseline": baseline,
        "pnl": pnl,
        "costs": costs,
        "data": {
            "profile_id": data_profile,
            **data,
            "critical_gaps": critical_gaps,
            "study_dates": _study_dates(
                data_profile,
                data["can_compute_now"]["signal_event"],
            ),
        },
        "external_sports_data": external,
        "experiment_design": {
            "unit": "ROOT_EVENT_X_MARKET_X_DECISION_OPPORTUNITY",
            "eligibility": "past-only, gap-free, exact-release-bound card-specific gate",
            "clock_rule": seed["signal"]["decision_clock"],
            "forecast_horizon": seed["signal"]["forecast_horizon"],
            "comparison": (
                baseline["policy"]
                if baseline["policy"] is not None
                else "signal/control contrast only; no executable comparison"
            ),
            "overlap_and_precedence": OVERLAP_PRECEDENCE_BY_EXPERIMENT.get(
                atomic_id,
                (
                    "No card-specific overlap exception registered; shared roots, "
                    "orders, capital and PnL must be deduplicated at portfolio evaluation."
                ),
            ),
            "split_status": "NOT_FROZEN_FOR_THIS_CARD",
            "minimum_evidence": (
                "independent days and roots, zeros retained, root/day clustered "
                "uncertainty, untouched validation and confirmation"
            ),
            "multiple_testing_family": f"DEEP03-{atomic_id[0]}-BREADTH",
            "stop_rule": (
                "Fail or remain blocked; never extend data because the observed "
                "result is unprofitable or insignificant."
            ),
        },
        "current_result": {
            "status": observed_signal["status"],
            "claim_tier": _claim_tier_for_observation(
                observed_signal["status"]
            ),
            "registry_design_status": seed["result_status"],
            "registered_claim_ceiling": seed["claim_tier"],
            "observed_signal": observed_signal,
            "strategy_pnl_claim": "NONE",
            "live_pnl_claim": "NONE",
            "note": (
                "A descriptive signal result does not establish fillability, "
                "fee-after PnL, capacity, or live profitability."
            ),
        },
        "blockers": blockers,
        "next_evidence": {
            "to_measure_signal": data["acquisition_paths"],
            "to_measure_pnl": (
                [
                    "freeze this exact card and baseline before outcome reads",
                    "stateful pessimistic shadow replay with exact fees and clocks",
                    "own-order shadow then separately authorized one-contract micro-live",
                    "untouched chronological confirmation with root/day accounting",
                ]
                if has_action
                else [
                    "define a deterministic action, executable baseline and PnL first"
                ]
            ),
            "external_source_ids": external["provider_candidates"],
        },
    }
    card["definition_sha256"] = _sha256(card)
    return card


def _summary_for_cards(cards: list[dict[str, Any]]) -> dict[str, Any]:
    family_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    for card in cards:
        family = card["identity"]["family_code"]
        computed = card["classification"]["computed_class"]
        target = card["classification"]["target_class"]
        family_counts[family] = family_counts.get(family, 0) + 1
        class_counts[computed] = class_counts.get(computed, 0) + 1
        target_counts[target] = target_counts.get(target, 0) + 1
    return {
        "total_cards": len(cards),
        "family_counts": family_counts,
        "computed_class_counts": class_counts,
        "target_class_counts": target_counts,
        "strategy_eligible_now": sum(
            card["classification"]["strategy_eligible"] for card in cards
        ),
        "warning": (
            "A registered action-shaped experiment is not evidence of profit "
            "and carries no permission to shadow, quote, trade or spend."
        ),
    }


def build_registry() -> dict[str, Any]:
    seeds = _load_card_seeds()
    if set(seeds) != set(ATOMIC_IDS):
        missing = sorted(set(ATOMIC_IDS) - set(seeds))
        extra = sorted(set(seeds) - set(ATOMIC_IDS))
        raise ValueError(f"seed ID mismatch: missing={missing}, extra={extra}")
    hypothesis_signal_pairs = [
        (seeds[atomic_id]["hypothesis"], seeds[atomic_id]["signal"]["trigger"])
        for atomic_id in ATOMIC_IDS
    ]
    if len(set(hypothesis_signal_pairs)) != len(hypothesis_signal_pairs):
        raise ValueError("duplicate hypothesis/signal seed pair")
    cards = [_build_card(atomic_id, seeds[atomic_id]) for atomic_id in ATOMIC_IDS]
    registry = {
        "schema_version": SCHEMA_VERSION,
        "registry_id": REGISTRY_ID,
        "generated_by": "tools/experiment_registry.py",
        "scope": {
            "objective": (
                "Convert the full Deep03 idea surface into falsifiable, "
                "action-linked, cost-complete PnL experiments."
            ),
            "breadth_policy": "REGISTER_ALL_106_BEFORE_DEPTH_RANKING",
            "strategy_definition": (
                "No concrete action, venue, executable baseline and after-cost "
                "PnL definition means it is not a strategy."
            ),
            "deployment_authority": "NONE",
            "order_authority": "NONE",
            "action_authority": "NONE",
            "external_data_policy": "MISSING_EXTERNAL_DATA_BLOCK_NO_IMPUTE",
        },
        "source_bindings": copy.deepcopy(SOURCE_BINDINGS),
        "classification_contract": {
            "classes": [
                "RESEARCH_QUESTION",
                "SIGNAL",
                "EXECUTABLE_STRATEGY",
                "BLOCKED",
            ],
            "rule": (
                "Target class records intent; computed class records present "
                "readiness. A critical missing input yields BLOCKED and preserves "
                "blocked_from. Unknown PnL is never zero."
            ),
        },
        "pnl_contract": {
            "primary": "incremental pathwise NetPnL after all costs",
            "cash_authority": "executable fills or separately authorized actual fills only",
            "diagnostic_only": ["mid return", "markout", "microprice", "spread opportunity"],
            "denominator_policy": (
                "retain eligible no-trigger, no-fill and no-accept opportunities; "
                "report per opportunity, trigger, fill, contract, day and peak capital"
            ),
            "capacity_policy": "no linear extrapolation beyond tested executable depth",
        },
        "external_source_catalog": copy.deepcopy(EXTERNAL_SOURCE_CATALOG),
        "summary": _summary_for_cards(cards),
        "experiments": cards,
    }
    registry["registry_sha256"] = _sha256(registry)
    return registry


def _expect(
    errors: list[str],
    condition: bool,
    message: str,
) -> None:
    if not condition:
        errors.append(message)


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    _expect(errors, registry.get("schema_version") == SCHEMA_VERSION, "bad schema_version")
    _expect(errors, registry.get("registry_id") == REGISTRY_ID, "bad registry_id")

    scope = registry.get("scope", {})
    _expect(errors, isinstance(scope, dict), "scope must be an object")
    if isinstance(scope, dict):
        for authority_field in (
            "deployment_authority",
            "order_authority",
            "action_authority",
        ):
            _expect(
                errors,
                scope.get(authority_field) == "NONE",
                f"scope.{authority_field} must remain NONE",
            )
        _expect(
            errors,
            scope.get("breadth_policy") == "REGISTER_ALL_106_BEFORE_DEPTH_RANKING",
            "scope breadth policy drift",
        )
        _expect(
            errors,
            scope.get("external_data_policy")
            == "MISSING_EXTERNAL_DATA_BLOCK_NO_IMPUTE",
            "scope external-data fail-closed policy drift",
        )

    source_bindings = registry.get("source_bindings")
    _expect(errors, isinstance(source_bindings, list), "source_bindings must be a list")
    if isinstance(source_bindings, list):
        _expect(
            errors,
            source_bindings == SOURCE_BINDINGS,
            "source bindings differ from the five canonical evidence roles",
        )
        for binding in source_bindings:
            if not isinstance(binding, dict):
                errors.append("source binding must be an object")
                continue
            role = binding.get("role", "<missing>")
            relative_path = binding.get("path")
            if not isinstance(relative_path, str):
                errors.append(f"{role}: source path missing")
                continue
            resolved = (ROOT / relative_path).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{role}: source path escapes repository root")
                continue
            if binding.get("local_required") and not resolved.is_file():
                errors.append(f"{role}: required source file missing")
                continue
            if resolved.is_file():
                _expect(
                    errors,
                    _file_sha256(resolved) == binding.get("sha256"),
                    f"{role}: source file SHA mismatch",
                )

        authority_bindings = [
            binding
            for binding in source_bindings
            if binding.get("role") == "ATOMIC_DIRECTION_AUTHORITY"
        ]
        _expect(
            errors,
            len(authority_bindings) == 1,
            "exactly one atomic direction authority binding is required",
        )
        if len(authority_bindings) == 1:
            authority_path = (ROOT / authority_bindings[0]["path"]).resolve()
            if authority_path.is_file():
                try:
                    authority = json.loads(
                        authority_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"atomic direction authority unreadable: {exc}")
                else:
                    _expect(
                        errors,
                        tuple(authority.get("atomic_ids", [])) == ATOMIC_IDS,
                        "atomic direction authority IDs differ from generator",
                    )
                    _expect(
                        errors,
                        authority.get("source_document_sha256")
                        == "ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36",
                        "atomic authority origin document SHA mismatch",
                    )

    _expect(
        errors,
        registry.get("external_source_catalog") == EXTERNAL_SOURCE_CATALOG,
        "external source catalog drift",
    )

    experiments = registry.get("experiments")
    _expect(errors, isinstance(experiments, list), "experiments must be a list")
    if not isinstance(experiments, list):
        return errors

    ids = [
        card.get("identity", {}).get("experiment_id")
        for card in experiments
        if isinstance(card, dict)
    ]
    _expect(errors, len(experiments) == 106, f"expected 106 cards, got {len(experiments)}")
    _expect(errors, tuple(ids) == ATOMIC_IDS, "card IDs/order differ from atomic authority")
    _expect(errors, len(set(ids)) == len(ids), "duplicate experiment IDs")

    expected_family_counts = {
        "A": 23,
        "B": 17,
        "C": 10,
        "D": 10,
        "E": 6,
        "F": 17,
        "G": 12,
        "P": 11,
    }
    actual_family_counts: dict[str, int] = {}
    seeds = _load_card_seeds()
    for card in experiments:
        if not isinstance(card, dict):
            errors.append("experiment card must be an object")
            continue
        identity = card.get("identity", {})
        card_id = identity.get("experiment_id", "<missing>")
        family = identity.get("family_code")
        actual_family_counts[family] = actual_family_counts.get(family, 0) + 1

        stored_card_sha = card.get("definition_sha256")
        without_card_sha = copy.deepcopy(card)
        without_card_sha.pop("definition_sha256", None)
        _expect(
            errors,
            stored_card_sha == _sha256(without_card_sha),
            f"{card_id}: definition SHA mismatch",
        )

        required_sections = {
            "classification",
            "hypothesis",
            "signal",
            "action",
            "venue",
            "baseline",
            "pnl",
            "costs",
            "data",
            "external_sports_data",
            "experiment_design",
            "current_result",
            "blockers",
            "next_evidence",
        }
        _expect(
            errors,
            required_sections <= set(card),
            f"{card_id}: missing required sections",
        )
        if not required_sections <= set(card):
            continue

        seed = seeds.get(card_id)
        _expect(errors, seed is not None, f"{card_id}: no canonical seed")
        if seed is None:
            continue

        action = card["action"]
        venue = card["venue"]
        baseline = card["baseline"]
        pnl = card["pnl"]
        classification = card["classification"]
        costs = card["costs"]
        data = card["data"]
        ext = card["external_sports_data"]
        blockers = card["blockers"]

        _expect(errors, isinstance(action, dict), f"{card_id}: action must be an object")
        _expect(errors, isinstance(venue, dict), f"{card_id}: venue must be an object")
        _expect(errors, isinstance(baseline, dict), f"{card_id}: baseline must be an object")
        _expect(errors, isinstance(pnl, dict), f"{card_id}: pnl must be an object")
        _expect(errors, isinstance(costs, list), f"{card_id}: costs must be a list")
        _expect(errors, isinstance(data, dict), f"{card_id}: data must be an object")
        _expect(errors, isinstance(ext, dict), f"{card_id}: external contract must be an object")
        _expect(errors, isinstance(blockers, list), f"{card_id}: blockers must be a list")
        if not all(
            (
                isinstance(action, dict),
                isinstance(venue, dict),
                isinstance(baseline, dict),
                isinstance(pnl, dict),
                isinstance(costs, list),
                isinstance(data, dict),
                isinstance(ext, dict),
                isinstance(blockers, list),
            )
        ):
            continue

        expected_identity = {
            "experiment_id": card_id,
            "registry_id": REGISTRY_ID,
            "version": 1,
            "family_code": card_id[0],
            "family_name": FAMILY_NAMES[card_id[0]],
            "lineage_status": "BREADTH_ATOMIC_DIRECTION",
            "source_atomic_id": card_id,
        }
        _expect(
            errors,
            identity == expected_identity,
            f"{card_id}: identity contract drift",
        )
        _expect(
            errors,
            card["hypothesis"].get("mechanism") == seed["hypothesis"],
            f"{card_id}: hypothesis differs from canonical seed",
        )
        _expect(
            errors,
            card["signal"] == seed["signal"],
            f"{card_id}: signal differs from canonical seed",
        )

        action_profile = action.get("profile_id")
        expected_profile = seed["action_profile"]
        _expect(
            errors,
            action_profile == expected_profile,
            f"{card_id}: action profile differs from canonical seed",
        )
        expected_action = _action_profile(expected_profile)
        expected_action["profile_id"] = expected_profile
        expected_action["current_execution_authority"] = "NONE"
        if expected_action["action_type"]:
            expected_action["registered_signal_to_action"] = (
                f"IF [{seed['signal']['trigger']}] THEN apply "
                f"[{expected_profile}] exactly as specified below; ELSE "
                "preserve the registered baseline/no-trade branch."
            )
        else:
            expected_action["registered_signal_to_action"] = None
        _expect(
            errors,
            action == expected_action,
            f"{card_id}: action contract drift",
        )

        has_action = bool(action.get("action_type"))

        _expect(
            errors,
            bool(card["hypothesis"]["mechanism"])
            and bool(card["hypothesis"]["falsifiable_statement"]),
            f"{card_id}: empty hypothesis",
        )
        for key in (
            "trigger",
            "direction_semantics",
            "decision_clock",
            "forecast_horizon",
            "falsifier",
        ):
            _expect(
                errors,
                bool(card["signal"].get(key)),
                f"{card_id}: missing signal.{key}",
            )
        _expect(
            errors,
            venue.get("execution_venue") in EXECUTION_VENUES,
            f"{card_id}: unknown venue",
        )
        _expect(
            errors,
            action.get("current_execution_authority") == "NONE",
            f"{card_id}: execution authority must remain NONE",
        )
        _expect(
            errors,
            venue == _venue_for(expected_profile),
            f"{card_id}: venue contract drift",
        )
        _expect(
            errors,
            baseline == _baseline_for(expected_profile),
            f"{card_id}: baseline contract drift",
        )
        _expect(
            errors,
            pnl == _pnl_for(expected_profile),
            f"{card_id}: PnL contract drift",
        )
        expected_costs = _costs_for(
            expected_profile,
            seed["external_dependency"],
        )
        _expect(
            errors,
            costs == expected_costs,
            f"{card_id}: cost contract drift",
        )
        expected_external = _external_contract(
            seed["external_dependency"],
            card_id,
        )
        _expect(
            errors,
            ext == expected_external,
            f"{card_id}: external-data contract drift",
        )
        _expect(
            errors,
            ext.get("dependency") in EXTERNAL_DEPENDENCIES,
            f"{card_id}: unknown external dependency",
        )

        if not has_action:
            _expect(
                errors,
                venue["execution_venue"] == "NONE",
                f"{card_id}: no-action card has venue",
            )
            _expect(
                errors,
                pnl["status"] == "NOT_DEFINED_NOT_A_STRATEGY",
                f"{card_id}: no-action card has PnL",
            )
            _expect(
                errors,
                not classification["strategy_eligible"],
                f"{card_id}: no-action card marked strategy",
            )
            _expect(
                errors,
                not card["costs"],
                f"{card_id}: no-action card has execution costs",
            )
        else:
            for action_field in (
                "signal_to_action_mapping",
                "instrument_side",
                "order_style",
                "price_rule",
                "quantity_rule",
                "timing_and_cancel",
                "exit_or_settlement",
                "kill_conditions",
                "registered_signal_to_action",
            ):
                _expect(
                    errors,
                    bool(action.get(action_field)),
                    f"{card_id}: action.{action_field} is empty",
                )
            _expect(
                errors,
                venue["execution_venue"] != "NONE",
                f"{card_id}: action lacks venue",
            )
            _expect(
                errors,
                baseline["baseline_id"] is not None,
                f"{card_id}: action lacks baseline",
            )
            _expect(
                errors,
                baseline.get("baseline_type") in BASELINE_TYPES
                and bool(baseline.get("policy"))
                and bool(baseline.get("comparability")),
                f"{card_id}: action baseline policy/comparability is incomplete",
            )
            _expect(
                errors,
                pnl["basis"] == "PATHWISE_AFTER_COST"
                and bool(pnl.get("formula"))
                and bool(pnl.get("incremental_formula"))
                and bool(pnl.get("markout_rule"))
                and bool(pnl.get("denominators"))
                and bool(pnl.get("zero_fill_treatment"))
                and bool(pnl.get("capacity_rule")),
                f"{card_id}: action lacks after-cost incremental PnL",
            )
            _expect(errors, bool(costs), f"{card_id}: action lacks costs")
            valid_cost_contracts = True
            for index, cost in enumerate(costs):
                cost_is_valid = (
                    isinstance(cost, dict)
                    and cost.get("type") in COST_TYPES
                    and cost.get("role") in COST_ROLES
                    and cost.get("status") in COST_STATUSES
                    and bool(cost.get("rule"))
                )
                _expect(
                    errors,
                    cost_is_valid,
                    f"{card_id}: invalid cost contract at index {index}",
                )
                valid_cost_contracts = valid_cost_contracts and cost_is_valid
            if valid_cost_contracts:
                missing_cost_blockers = set(_cost_blockers(costs))
                _expect(
                    errors,
                    missing_cost_blockers <= set(blockers),
                    f"{card_id}: missing cost is not represented by a critical blocker",
                )

        _expect(
            errors,
            all(blocker in CRITICAL_BLOCKERS for blocker in blockers),
            f"{card_id}: unknown blocker",
        )
        base_data = _data_profile(seed["data_profile"])
        expected_blockers = _expected_blockers(
            seed,
            base_data,
            expected_external,
            expected_costs,
            bool(expected_action["action_type"]),
        )
        _expect(
            errors,
            blockers == expected_blockers,
            f"{card_id}: blocker set differs from fail-closed contract",
        )
        if "MISSING_SIGNAL_INPUT" in expected_blockers:
            base_data["can_compute_now"]["signal_event"] = "NO"
            base_data["currently_calculable"] = (
                "The base feed may exist, but this experiment's named signal input "
                "is absent; current signal incidence and effect are not calculable."
            )
        expected_data = {
            "profile_id": seed["data_profile"],
            **base_data,
            "critical_gaps": [
                _gap_contract(card_id, blocker)
                for blocker in expected_blockers
            ],
            "study_dates": _study_dates(
                seed["data_profile"],
                base_data["can_compute_now"]["signal_event"],
            ),
        }
        _expect(
            errors,
            data == expected_data,
            f"{card_id}: data and study-date contract drift",
        )
        gap_contracts = data.get("critical_gaps", [])
        _expect(
            errors,
            [gap.get("blocker") for gap in gap_contracts] == blockers,
            f"{card_id}: critical gap contracts do not match blockers",
        )
        _expect(
            errors,
            all(
                gap.get("missing_evidence")
                and gap.get("acquisition_or_build_path")
                and gap.get("unknown_value_policy") == "BLOCK_NOT_ZERO"
                for gap in gap_contracts
            ),
            f"{card_id}: incomplete blocker remediation contract",
        )
        base_class = _base_class(
            seed["target_class"],
            expected_action,
            _venue_for(expected_profile),
            _baseline_for(expected_profile),
            _pnl_for(expected_profile),
        )
        expected_classification = _classification(
            seed["target_class"], base_class, expected_blockers
        )
        _expect(
            errors,
            classification == expected_classification,
            f"{card_id}: computed classification drift",
        )

        if ext.get("dependency") == "REQUIRED":
            _expect(
                errors,
                ext["availability"] == "MISSING"
                and ext["missing_policy"] == "BLOCK_NO_IMPUTE",
                f"{card_id}: required external data not fail-closed",
            )
            _expect(
                errors,
                "EXTERNAL_SPORTS_DATA_MISSING" in blockers,
                f"{card_id}: external blocker absent",
            )
            _expect(
                errors,
                not classification["strategy_eligible"],
                f"{card_id}: missing external data promoted",
            )
            _expect(
                errors,
                "versioned provider-event-to-Kalshi root/market/outcome mapping"
                in ext.get("required_variables", []),
                f"{card_id}: external mapping variable absent",
            )
            if has_action:
                _expect(
                    errors,
                    "INSTRUMENT_MAPPING_UNKNOWN" in blockers,
                    f"{card_id}: external action mapping blocker absent",
                )
                _expect(
                    errors,
                    any(cost.get("type") == "DATA_API" for cost in costs),
                    f"{card_id}: external action omits data/API cost",
                )
        if ext.get("dependency") == "OPTIONAL_REFINEMENT":
            _expect(
                errors,
                ext["availability"] == "MISSING_OPTIONAL_REFINEMENT"
                and ext["missing_policy"]
                == "DO_NOT_IMPUTE_CORE_MAY_RUN_WITHOUT_FIELD"
                and not ext["blockers"],
                f"{card_id}: optional external refinement contract is invalid",
            )

        signal_event_status = data["can_compute_now"]["signal_event"]
        _expect(
            errors,
            data.get("study_dates")
            == _study_dates(seed["data_profile"], signal_event_status),
            f"{card_id}: fixed study dates drift",
        )
        if signal_event_status != "YES":
            _expect(
                errors,
                not data["study_dates"][
                    "eligible_for_current_signal_description"
                ],
                f"{card_id}: dates marked eligible although signal_event is "
                f"{signal_event_status}",
            )
        if "MISSING_SIGNAL_INPUT" in blockers:
            _expect(
                errors,
                signal_event_status == "NO",
                f"{card_id}: missing signal input still marked calculable",
            )

        observed_expected = copy.deepcopy(
            OBSERVED_SIGNAL_EVIDENCE.get(
                card_id,
                {
                    "status": "NOT_MEASURED_IN_CURRENT_RELEASE",
                    "values": None,
                    "source": None,
                },
            )
        )
        _expect(
            errors,
            card["current_result"].get("observed_signal") == observed_expected,
            f"{card_id}: current evidence binding drift",
        )
        _expect(
            errors,
            card["current_result"].get("registry_design_status")
            == seed["result_status"]
            and card["current_result"].get("registered_claim_ceiling")
            == seed["claim_tier"],
            f"{card_id}: design status differs from canonical seed",
        )
        _expect(
            errors,
            card["current_result"]["strategy_pnl_claim"] == "NONE"
            and card["current_result"]["live_pnl_claim"] == "NONE",
            f"{card_id}: unsupported PnL claim",
        )
        _expect(
            errors,
            card == _build_card(card_id, seed),
            f"{card_id}: card differs from canonical materialization",
        )

    _expect(
        errors,
        actual_family_counts == expected_family_counts,
        f"family counts mismatch: {actual_family_counts}",
    )

    # Public RFQ create/delete and combo fields are strictly unsigned.  The
    # signed D06 control is private-only and still has no order action.
    by_id = {card["identity"]["experiment_id"]: card for card in experiments}
    for card_id in (
        "D01-RFQ-FLOW-CENSUS",
        "D04-RFQ-COMBO-DEMAND-LEG-PRESSURE",
        "D06-RFQ-DIRECTION-VOL-SIGNAL",
    ):
        if card_id in by_id:
            direction = by_id[card_id]["signal"]["direction_semantics"].upper()
            _expect(
                errors,
                "UNSIGNED" in direction or "NO_DIRECTION" in direction,
                f"{card_id}: public RFQ direction must remain unsigned",
            )
    for card_id in (
        "D02-RFQ-SIZE-INTENT",
        "D03-RFQ-LIFECYCLE-SURVIVAL",
        "D05-RFQ-REQUESTER-HASH",
        "D07-RFQ-TO-CLOB-IMPACT",
    ):
        if card_id in by_id:
            card = by_id[card_id]
            _expect(
                errors,
                card["action"]["profile_id"] == "RFQ_GUARD"
                and card["action"]["instrument_side"]
                == "BOTH_RELATED_CLOB_SIDES_PUBLIC_UNSIGNED"
                and card["action"]["action_type"]
                == ["CANCEL", "REPRICE", "RESIZE"],
                f"{card_id}: public RFQ guard action drift",
            )
            action_text = _canonical_json(card["action"]).decode("utf-8").upper()
            _expect(
                errors,
                all(
                    token not in action_text
                    for token in (
                        "BUY_YES",
                        "BUY_NO",
                        "SELL_YES",
                        "SELL_NO",
                        "ACCEPTED_SIDE",
                    )
                ),
                f"{card_id}: public RFQ guard contains directional action",
            )
    if "D06-RFQ-DIRECTION-VOL-SIGNAL" in by_id:
        d06 = by_id["D06-RFQ-DIRECTION-VOL-SIGNAL"]
        _expect(
            errors,
            d06["data"]["profile_id"] == "RFQ_PRIVATE"
            and not d06["action"]["action_type"],
            "D06: signed accepted-side analysis must remain private and no-action",
        )

    for card in experiments:
        if not isinstance(card, dict):
            continue
        profile = card.get("action", {}).get("profile_id")
        if profile in CHILD_DEPENDENT_ACTION_PROFILES:
            _expect(
                errors,
                "CONFIRMED_CHILD_STRATEGIES_MISSING" in card.get("blockers", []),
                f"{card.get('identity', {}).get('experiment_id')}: "
                "child-dependent profile lacks immutable child binding blocker",
            )

    if "E01-SETTLEMENT-CONVERGENCE" in by_id:
        e01 = by_id["E01-SETTLEMENT-CONVERGENCE"]
        _expect(
            errors,
            e01["action"]["profile_id"] == "CONTROL_ONLY"
            and not e01["action"]["action_type"]
            and "never triggers an order"
            in e01["signal"]["direction_semantics"],
            "E01: final settlement label must remain a no-action retrospective control",
        )
    if "D09-MVE-CATALOG-SCHEMA" in by_id:
        d09 = by_id["D09-MVE-CATALOG-SCHEMA"]
        _expect(
            errors,
            d09["venue"]["execution_venue"] == "NONE"
            and "VENUE_PERMISSION_MISSING" not in d09["blockers"],
            "D09: read-only catalog card must not request venue permission",
        )
    if "D10-MVE-DIRECT-TRADING" in by_id:
        d10 = by_id["D10-MVE-DIRECT-TRADING"]
        _expect(
            errors,
            d10["action"]["profile_id"] == "MVE_DIRECT"
            and d10["data"]["profile_id"] == "MVE_PRIVATE"
            and d10["venue"]["execution_venue"]
            == "KALSHI_MVE_PRIVATE_EXECUTION"
            and "RFQ_PUBLIC_DATA_UNRELEASED" not in d10["blockers"],
            "D10: MVE execution must not reuse RFQ contracts",
        )

    _expect(
        errors,
        registry.get("summary") == _summary_for_cards(experiments),
        "registry summary does not recompute from cards",
    )

    stored_registry_sha = registry.get("registry_sha256")
    without_registry_sha = copy.deepcopy(registry)
    without_registry_sha.pop("registry_sha256", None)
    _expect(
        errors,
        stored_registry_sha == _sha256(without_registry_sha),
        "registry SHA mismatch",
    )
    return errors


def _write_registry(registry: dict[str, Any]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        registry,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    REGISTRY_PATH.write_text(payload + "\n", encoding="utf-8")


def _read_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _print_summary(registry: dict[str, Any]) -> None:
    summary = registry["summary"]
    print(
        "experiment registry ok: "
        f"{summary['total_cards']} cards; "
        f"families={summary['family_counts']}; "
        f"computed={summary['computed_class_counts']}; "
        f"strategy_eligible_now={summary['strategy_eligible_now']}; "
        f"sha256={registry['registry_sha256']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="write the canonical derived JSON artifact",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate the checked-in artifact and prove it is generator-current",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print classification and SHA summary",
    )
    args = parser.parse_args(argv)

    expected = build_registry()
    if args.write:
        errors = validate_registry(expected)
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
            return 1
        _write_registry(expected)
        actual = _read_registry()
    else:
        if not REGISTRY_PATH.exists():
            print(f"FAIL: missing registry artifact: {REGISTRY_PATH}", file=sys.stderr)
            return 1
        actual = _read_registry()

    errors = validate_registry(actual)
    if actual != expected:
        errors.append(
            "checked-in registry differs from generator output; run "
            "python3 tools/experiment_registry.py --write"
        )
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if args.summary or args.write or args.check:
        _print_summary(actual)
    else:
        _print_summary(actual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
