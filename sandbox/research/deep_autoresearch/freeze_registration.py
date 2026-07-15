#!/usr/bin/env python3
"""Freeze Cycle-1 registration and exact data/code identities before results."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import bootstrap


EXPECTED_OBJECT_SET = {
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03":
        "73ca061942dff3d6554ae4d69073d2f64c2337e18eae64ef1aca06dd8a7dff32",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5":
        "60846563c6ffcb1c34e7b926ee44a281aecbaa2a81edcf6cc00d5d070abd68d4",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2,
                               ensure_ascii=False) + "\n", encoding="utf-8")


def version_list(manifest: dict) -> tuple[bytes, str]:
    rows = []
    for obj in sorted(manifest["objects"], key=lambda x: (x["key"], x.get("version_id") or "")):
        rows.append(json_bytes({
            "key": obj["key"], "version_id": obj.get("version_id"),
            "sha256": obj["sha256"], "size": obj["size"],
        }))
    payload = b"".join(rows)
    return payload, hashlib.sha256(payload).hexdigest()


def feature_dictionary() -> dict:
    common = {
        "source_channel": "orderbooks_l1/trades",
        "decision_timestamp": "local_recv_ts_us",
        "gap_behavior": "exclude any outcome window overlapping a declared gap",
        "causal_availability": "past/current receive-clock rows only",
        "tl1_eligibility": "TL1 only for sub-second claims",
        "implementation": "sandbox/research/deep_autoresearch/run_cycle1.py",
        "tests": ["sandbox/research/deep_autoresearch/test_stats_features.py"],
    }
    features = [
        {"name": "valid_two_sided", "definition": "both YES bid/ask present with 0<bid<ask<10000", "formula": "yes_bid_e4>0 AND yes_bid_e4<yes_ask_e4 AND yes_ask_e4<10000", "units": "boolean", "sign": "true means executable two-sided state", "source_fields": ["yes_bid_e4", "yes_ask_e4"], "lookback": "current last causal L1 row", "minimum": 1, "null": "false"},
        {"name": "midpoint_logodds", "definition": "mean of bid and ask logits with only the representable E4 boundary clip", "formula": "0.5*(logit(clip(yes_bid_e4,1,9999)/10000)+logit(clip(yes_ask_e4,1,9999)/10000))", "units": "log-odds", "sign": "larger means higher YES probability", "source_fields": ["yes_bid_e4", "yes_ask_e4"], "lookback": "current last causal L1 row", "minimum": 1, "null": "NULL unless valid_two_sided"},
        {"name": "spread_logodds", "definition": "executable YES ask logit minus YES bid logit", "formula": "logit(ask)-logit(bid)", "units": "log-odds", "sign": "nonnegative; larger means wider", "source_fields": ["yes_bid_e4", "yes_ask_e4"], "lookback": "current last causal L1 row", "minimum": 1, "null": "NULL unless valid_two_sided"},
        {"name": "l1_state_duration_us", "definition": "trusted nonstale time to the next receive-clock update, clipped at the selected day coverage end, 60 seconds, and any declared gap", "formula": "gap_overlap?0:max(0,min(next_receive,day_last_receive,t+60s)-t)", "units": "microseconds", "sign": "nonnegative descriptive duration weight", "source_fields": ["local_recv_ts_us", "recv_wall_ns", "recv_mono_ns"], "lookback": "next timestamp is an interval endpoint only and is never a decision feature", "minimum": 1, "null": "0"},
        {"name": "dim_effective_us", "definition": "conservative time after which the post-hoc market/event snapshot may enter a causal study", "formula": "max(epoch_us(markets.updated_time),epoch_us(events.last_updated_ts when event title is used))", "units": "UTC microseconds", "sign": "facts earlier than this are excluded from dimension-dependent causal analysis", "source_fields": ["updated_time", "last_updated_ts"], "source_channel": "dim/snapshots", "lookback": "daily snapshot; post-hoc mapping until this conservative gate", "minimum": 1, "null": "dimension-dependent causal observation excluded", "causal_availability": "never assume a latest snapshot value was known before dim_effective_us"},
        {"name": "time_to_start_s", "definition": "post-hoc snapshot occurrence time minus receive time, admitted to causal diagnostics only at or after dim_effective_us", "formula": "(epoch_us(occurrence_datetime)-local_recv_ts_us)/1e6, requiring local_recv_ts_us>=dim_effective_us", "units": "seconds", "sign": "positive pre-match", "source_fields": ["occurrence_datetime", "updated_time", "last_updated_ts", "local_recv_ts_us"], "source_channel": "dim/snapshots + orderbooks_l1", "lookback": "post-hoc daily snapshot with conservative effective-time gate", "minimum": 1, "null": "NULL/excluded when occurrence or effective-time evidence is missing", "causal_availability": "MIXED for atlas; causal subset only after dim_effective_us"},
        {"name": "large_flow_threshold_e4", "definition": "sport-level prior-day p99 trade count among provisional-root, dim-effective eligible trades", "formula": "q99(count_e4 | date=2026-07-12,provisional_root,trade_time>=dim_effective_us,sport)", "units": "E4 contracts", "sign": "larger threshold means larger flow", "source_fields": ["count_e4", "sport", "date", "dim_effective_us"], "lookback": "prior day only; applied to 2026-07-13", "minimum": 500, "null": "no treatment"},
        {"name": "prior_60s_l1_message_count", "definition": "count of eligible same-market L1 receive rows strictly before the decision row in the preceding 60 seconds", "formula": "count(*) RANGE BETWEEN 60s PRECEDING AND 1 microsecond PRECEDING, partitioned by date/market", "units": "messages", "sign": "larger means more recent displayed-book activity", "source_fields": ["local_recv_ts_us", "market_ticker"], "lookback": "strictly prior 60 seconds", "minimum": 0, "null": "0", "implementation": "sandbox/research/deep_autoresearch/core_hypothesis_tests.py:build_regimes", "tests": ["sandbox/research/deep_autoresearch/test_core_hypothesis_tests.py"]},
        {"name": "active_tight_regime", "definition": "predeclared sport-specific regime using only train-day thresholds", "formula": "prior_60s_l1_message_count >= train sport q75 AND spread_logodds <= train sport q25; in-play is a separate regime", "units": "categorical", "sign": "ACTIVE_TIGHT is the treatment label", "source_fields": ["spread_logodds", "prior_60s_l1_message_count", "occurrence_datetime"], "lookback": "thresholds fit on 2026-07-12 only and applied unchanged on 2026-07-13", "minimum": 500, "null": "sport excluded if train support is below the frozen minimum", "causal_availability": "requires fact receive time >= dim_effective_us", "implementation": "sandbox/research/deep_autoresearch/core_hypothesis_tests.py:build_regimes", "tests": ["sandbox/research/deep_autoresearch/test_core_hypothesis_tests.py"]},
        {"name": "causal_regime_transition", "definition": "a change from the immediately prior eligible regime into the current regime", "formula": "regime_t != lag(regime)_market,date ordered by receive time", "units": "event indicator", "sign": "transition into ACTIVE_TIGHT is treatment", "source_fields": ["active_tight_regime", "local_recv_ts_us", "recv_wall_ns", "recv_mono_ns"], "lookback": "immediately prior eligible state only", "minimum": 1, "null": "first state per market has no transition", "implementation": "sandbox/research/deep_autoresearch/core_hypothesis_tests.py:build_regimes", "tests": ["sandbox/research/deep_autoresearch/test_core_hypothesis_tests.py"]},
        {"name": "large_flow_match_stratum", "definition": "frozen exact-match covariate tuple for the large-flow diagnostic", "formula": "market,family,UTC-hour,8-bin midpoint logodds,5-bin spread logodds,5-bin past-60s activity", "units": "categorical tuple", "sign": "no directional meaning", "source_fields": ["market_ticker", "family_event_id", "midpoint_logodds", "spread_logodds", "prior_60s_l1_message_count"], "lookback": "current or strictly prior causal state; control outcome must complete before treatment", "minimum": 1, "null": "unmatched treatment retained in unmatched count but excluded from effect", "implementation": "sandbox/research/deep_autoresearch/core_hypothesis_tests.py:build_trade_covariates", "tests": ["sandbox/research/deep_autoresearch/test_core_hypothesis_tests.py"]},
        {"name": "gross_symmetric_quote_proxy_logodds", "definition": "half current log-odds spread minus absolute future midpoint move", "formula": "0.5*spread_logodds - abs(mid_logodds(t+h)-mid_logodds(t))", "units": "log-odds proxy", "sign": "larger is better gross displayed-spread resilience", "source_fields": ["yes_bid_e4", "yes_ask_e4", "local_recv_ts_us"], "lookback": "registered outcome horizons only", "minimum": 1, "null": "stale/gap/out-of-coverage rows excluded", "causal_availability": "diagnostic outcome only; never a fill, fee-adjusted return or PnL", "implementation": "sandbox/research/deep_autoresearch/core_hypothesis_tests.py:build_tts", "tests": ["sandbox/research/deep_autoresearch/test_core_hypothesis_tests.py"]},
        {"name": "l2_snapshot_anchored_book_state", "definition": "per-market two-sided L2 price-to-quantity maps reconstructed only after a full snapshot and then updated by causal deltas", "formula": "book=latest snapshot; book[side,price]+=delta; snapshot resets the complete side maps and increments snapshot_epoch", "units": "E4 displayed contracts by E4 price", "sign": "nonnegative quantities only", "source_fields": ["msg_type", "yes_levels", "no_levels", "side", "price_e4", "delta_e4", "recv_wall_ns", "recv_mono_ns"], "source_channel": "orderbooks_full L2", "lookback": "current snapshot epoch only", "minimum": 0, "null": "pre-snapshot or invalid book state excluded", "causal_availability": "stream order uses TL1 receive clock; flow-quality authority is sealed quality/l2_gaps.json, never per-market ws_seq gaps", "implementation": "sandbox/research/deep_autoresearch/l2_hypothesis_stage.py:Book,replay_rows", "tests": ["sandbox/research/deep_autoresearch/test_l2_hypothesis_stage.py"]},
        {"name": "l2_touch_depletion", "definition": "a negative delta at the current literal-side touch removing at least one contract and at least half of pre-delta touch quantity", "formula": "delta<0 AND price=pre_touch AND removed_e4>=10000 AND removed_e4/pre_touch_qty_e4>=0.50", "units": "boolean plus removed E4 contracts", "sign": "true means displayed touch retreat/depletion, not participant identity", "source_fields": ["side", "price_e4", "delta_e4", "snapshot-anchored book"], "source_channel": "orderbooks_full L2", "lookback": "pre-delta state in current snapshot epoch", "minimum": 1, "null": "invalid or unanchored book excluded", "implementation": "sandbox/research/deep_autoresearch/l2_hypothesis_stage.py:Book.delta,replay_rows", "tests": ["sandbox/research/deep_autoresearch/test_l2_hypothesis_stage.py"]},
        {"name": "hfollow_retreat_coordination_score", "definition": "number of distinct sibling markets in the same provisional root and literal side with causal touch retreat in the trailing 100ms", "formula": "count_distinct(market_ticker | same date/root/side, clock in [t-100ms,t])", "units": "markets", "sign": "larger means more coordinated displayed-depth retreat", "source_fields": ["l2_touch_depletion", "root_event_id", "side", "recv_wall_ns"], "source_channel": "orderbooks_full L2 + dim snapshots", "lookback": "trailing 100ms including current event", "minimum": 2, "null": "no treatment when train-date q90/minimum-two threshold cannot be frozen", "causal_availability": "requires t_us>=dim_effective_us; threshold fit only on 2026-07-12 and applied on 2026-07-13", "implementation": "sandbox/research/deep_autoresearch/l2_hypothesis_stage.py:annotate_retreats,hfollow_populations", "tests": ["sandbox/research/deep_autoresearch/test_l2_hypothesis_stage.py"]},
        {"name": "l2_refill_fraction_latency", "definition": "causal recovery of same-side displayed depth at the original depleted touch or better within the current snapshot epoch", "formula": "refill_fraction=(depth_at_original_touch_or_better-current_post_depletion_depth)/removed_e4; treatment when >=0.80 within 100ms or 1s; latency=refill_receive-depletion_receive", "units": "fraction and microseconds", "sign": "larger fraction/faster latency means more resilient refill", "source_fields": ["snapshot-anchored book", "original_touch_price_e4", "removed_e4", "recv_wall_ns"], "source_channel": "orderbooks_full L2", "lookback": "future endpoint is an outcome/decision trigger only; maximum 1s", "minimum": 0.8, "null": "right-censored at snapshot, invalid book, repeated depletion, day end or 1s", "causal_availability": "snapshot is a censor/reset and can never count as refill", "implementation": "sandbox/research/deep_autoresearch/l2_hypothesis_stage.py:replay_rows,depletion_populations", "tests": ["sandbox/research/deep_autoresearch/test_l2_hypothesis_stage.py"]},
        {"name": "l2_event_match_strata", "definition": "frozen exact-match covariates for H-FOLLOW and depletion/refill controls", "formula": "H-FOLLOW: date,sport,family,hour,side,price-band,spread-band,activity-band; depletion adds depletion-size-bin", "units": "categorical tuple", "sign": "no directional meaning", "source_fields": ["sport", "series_ticker", "receive hour", "side", "mid_logodds", "spread_logodds", "updates_1s", "removed_e4"], "source_channel": "replayed orderbooks_full L2", "lookback": "state observable at treatment/control decision clock", "minimum": 1, "null": "unmatched treatments retained in counts but excluded from paired effect", "implementation": "sandbox/research/deep_autoresearch/l2_hypothesis_stage.py:deterministic_match", "tests": ["sandbox/research/deep_autoresearch/test_l2_hypothesis_stage.py"]},
        {"name": "book_age_us", "definition": "receive-clock staleness of the last strictly prior decision book or last book at/before an outcome boundary", "formula": "decision_us-prior_book_us; target_us-outcome_book_us", "units": "microseconds", "sign": "larger is staler", "source_fields": ["local_recv_ts_us"], "lookback": "latest causal row; maximum 5 seconds", "minimum": 1, "null": "observation excluded"},
        {"name": "signed_future_logodds_move", "definition": "future midpoint move signed by observed taker side", "formula": "(+1 YES taker,-1 NO taker)*(mid_lo(last_book_at_or_before_t+h)-mid_lo(strictly_prior_book))", "units": "log-odds", "sign": "positive continuation", "source_fields": ["taker_side", "yes_bid_e4", "yes_ask_e4"], "lookback": "fixed horizons 1/10/25/50/100/250/500ms and 1/2/3/5/10/30/120s", "minimum": 1, "null": "exclude if stale, outside coverage, or gap-overlapping"},
        {"name": "strict_through_eligible_trade", "definition": "historical trade-through diagnostic relative to a strictly prior touch; not a reconstructed own-order fill", "formula": "YES taker: trade>prior ask; NO taker: trade<prior bid; equality=false", "units": "boolean diagnostic", "sign": "true means the historical trade passed the prior touch", "source_fields": ["taker_side", "yes_price_e4", "yes_bid_e4", "yes_ask_e4"], "lookback": "strictly prior L1 only", "minimum": 1, "null": "false; never interpreted as lifecycle fill"},
        {"name": "rfq_create_receive_us", "definition": "first valid deduplicated rfq_created outer-envelope local receipt time", "formula": "recv_wall_ns//1000", "units": "UTC microseconds", "sign": "event-study anchor", "source_fields": ["recv_wall_ns", "raw.type", "raw.msg.id"], "source_channel": "raw_rfq communications", "lookback": "current envelope only", "minimum": 1, "null": "invalid RFQ frame excluded", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_contracts_e2", "definition": "RFQ contracts_fp converted only when exactly representable at E2", "formula": "exact_decimal(contracts_fp)*100; reject rounding/overflow", "units": "E2 contracts", "sign": "nonnegative size where valid", "source_fields": ["contracts_fp"], "source_channel": "raw_rfq communications", "lookback": "create envelope", "minimum": 1, "null": "missing or schema-invalid", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:fixed_sql", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_target_cost_e6", "definition": "RFQ target_cost_dollars converted only when exactly representable at E6", "formula": "exact_decimal(target_cost_dollars)*1e6; reject rounding/overflow", "units": "E6 dollars", "sign": "nonnegative economic intent where valid", "source_fields": ["target_cost_dollars"], "source_channel": "raw_rfq communications", "lookback": "create envelope", "minimum": 1, "null": "missing or schema-invalid", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:fixed_sql", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_known_combo", "definition": "lower-bound combo recognition from a collection ticker or one or more selected legs", "formula": "mve_collection_ticker IS NOT NULL OR leg_count_raw>0", "units": "boolean lower bound", "sign": "true means observed combo evidence; false does not prove single-market", "source_fields": ["mve_collection_ticker", "mve_selected_legs"], "source_channel": "raw_rfq communications", "lookback": "create envelope", "minimum": 1, "null": "false/lower-bound unknown", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_lifecycle_duration_us", "definition": "receive-clock time from create to first valid delete, or right-censor at the first observation boundary/scan end", "formula": "min(first_valid_delete,first_loss_or_close_after_create,scan_end)-create_receive_us", "units": "microseconds", "sign": "nonnegative lifetime/censor age", "source_fields": ["rfq id", "recv_wall_ns", "marker", "stream_epoch"], "source_channel": "raw_rfq communications", "lookback": "future endpoint is outcome only and never a create-time feature", "minimum": 1, "null": "invalid lifecycle excluded", "causal_availability": "delete_observed only if it precedes the first observation boundary; otherwise right-censored", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_requester_hash", "definition": "run-scoped SHA-256 pseudonym for nonempty creator_id", "formula": "sha256('SPORTS-AUTORESEARCH-01|'||run_id||'|'||creator_id)", "units": "pseudonymous identifier", "sign": "identity/actor claims forbidden", "source_fields": ["creator_id"], "source_channel": "raw_rfq communications", "lookback": "current envelope", "minimum": 1, "null": "unknown requester", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:requester_hash", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_clob_window_change", "definition": "CLOB log-odds, spread, depth, imbalance, message and signed-flow change between strictly prior receive-clock boundaries around RFQ create/delete", "formula": "metric(last row < window_end)-metric(last row < window_start); windows [-30,-10],[-10,-1],[-1,0],[0,.1],[.1,1],[1,3],[3,10],[10,30],[30,120] seconds", "units": "metric-specific; probability prices transformed to log-odds", "sign": "positive follows the named metric", "source_fields": ["recv_wall_ns", "local_recv_ts_us", "L1", "L2", "trades"], "source_channel": "raw_rfq + orderbooks_l1/orderbooks_full/trades", "lookback": "fixed event-study windows; boundary book age <=5s", "minimum": 1, "null": "invalid if stale, gap-overlapping, outside coverage, or before dim_effective_us", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:build_clob_context", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_prior_control_anchor", "definition": "deterministic same-market prior pseudo-event anchor used only for matched diagnostic comparison", "formula": "anchor_us-(300s+abs(hash(request_key,market,endpoint)) mod 300s)", "units": "UTC microseconds", "sign": "control; no directional meaning", "source_fields": ["request_key", "market_ticker", "endpoint_type"], "source_channel": "derived RFQ/CLOB", "lookback": "5-10 minutes before endpoint; same receive date and prior 120s RFQ-free required", "minimum": 1, "null": "unmatched control excluded", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:build_clob_context", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
        {"name": "rfq_clob_root_sample", "definition": "deterministic bounded CLOB expansion while full RFQ flow/lifecycle remain unsampled", "formula": "rank hash(request_key,market,endpoint) within date/root/endpoint/role; retain rank<=50; inclusion=min(1,50/root_population)", "units": "sample indicator/probability", "sign": "descriptive only", "source_fields": ["request_key", "market_ticker", "root_event_id"], "source_channel": "derived RFQ/CLOB", "lookback": "fixed before results", "minimum": 1, "null": "unmapped root is not expanded", "implementation": "sandbox/research/deep_autoresearch/rfq_full_stage.py:build_clob_context", "tests": ["sandbox/research/deep_autoresearch/test_rfq_full_stage.py"]},
    ]
    return {"schema_version": "cycle1-feature-dictionary-v3",
            "frozen_before_results": True,
            "features": [{**common, **feature} for feature in features]}


def prior_exposure(run_id: str, cutoff: str) -> dict:
    inventory = [
        {"id": "2026-07-11__seal-de2e77c86c66", "type": "release", "status": "QUARANTINED_LEGACY", "selected": False, "reason": "no frozen publication/version binding"},
        {"id": "2026-07-12__seal-bc37de4c__pub-3e9c7603b8cab292", "type": "release", "status": "RFQ_EXCLUDED_PRIOR_VARIANT", "selected": False},
        {"id": "2026-07-12__seal-bc37de4c__pub-e1007e36c3cd927b", "type": "torn_publication_inventory", "status": "NO_MANIFEST", "selected": False, "holdout_classification": "PRIOR_EXPOSED_CONSERVATIVE"},
        {"id": bootstrap.RELEASES[0]["release_id"], "type": "release", "status": "RFQ_INCLUDED_VERSION_BOUND", "selected": True},
        {"id": "2026-07-13__seal-7f6e5c1b__pub-be44d2be5e80e7fd", "type": "release", "status": "RFQ_EXCLUDED_PRIOR_VARIANT", "selected": False},
        {"id": bootstrap.RELEASES[1]["release_id"], "type": "release", "status": "RFQ_INCLUDED_VERSION_BOUND", "selected": True},
    ]
    return {
        "schema_version": "sports-autoresearch-prior-exposure-v2",
        "run_id": run_id, "cutoff_utc": cutoff,
        "inventory_snapshot_status": "READ_ONLY_CUTOFF_INVENTORY_COMPLETE",
        "rule": "Every release, variant, torn inventory residual, report, dashboard and observation visible before cutoff is PRIOR_EXPOSED.",
        "sealed_dates_prior_exposed": ["2026-07-11", "2026-07-12", "2026-07-13"],
        "known_pre_cutoff_inventory": inventory,
        "previously_examined_dates": ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"],
        "prior_artifacts": [
            "docs/DISCOVERY_REPORT_2026-07-07.md",
            "docs/RESEARCH_EDGE_HYPOTHESES_2026-07-09.md",
            "docs/RFQ_CAPTURE_AND_48H_REPORT.md",
            "docs/research_notes/HYPOTHESIS_LEDGER.md",
            "sandbox/research/workbench/hypotheses.json",
            "work/research/auto_research/20260715T023720Z__c21a79a8cff__cycle0/**",
            "all operator/agent observations before cutoff",
        ],
        "post_cutoff_policy": {
            "classification": "RESERVED_UNOPENED", "fetch_allowed": False,
            "manifest_content_open_allowed": False, "summary_scan_allowed": False,
            "formal_confirmation_opened": False,
        },
    }


def freeze(run_dir: Path, cycle0: Path, source_dir: Path) -> None:
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "BOOTSTRAPPED_BEFORE_ANALYSIS" or manifest.get("analysis_started"):
        raise SystemExit("run is not in a freezeable no-result state")
    if (run_dir / "REPORT/CYCLE1_CORE_SUMMARY.json").exists():
        raise SystemExit("a result already exists; refusing to refreeze")
    cards = bootstrap.hypothesis_cards()
    if len(cards) != 10:
        raise SystemExit("exactly ten Cycle-1 cards are required")
    ledger = {
        "cycle": 1, "registered_before_results": True,
        "hypotheses": cards, "reserved_anomaly_slots": 0,
        "own_data_anomaly_hypotheses": [
            "C1-ANOM-RFQ-SIZE-TAIL-01",
            "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
        ],
    }
    write_json(run_dir / "HYPOTHESIS_LEDGER.json", ledger)
    (run_dir / "HYPOTHESIS_LEDGER.md").write_text(
        "# HYPOTHESIS LEDGER — Cycle 1 frozen before results\n\n"
        "> EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION\n\n"
        + "\n".join(f"## {c['hypothesis_id']}\n\n{c['sentence']}\n\n- status: `{c['status']}`\n- family: `{c['family']}`\n" for c in cards),
        encoding="utf-8",
    )
    with (run_dir / "TRIAL_REGISTRY.jsonl").open("w", encoding="utf-8") as handle:
        for card in cards:
            handle.write(json.dumps({
                "registered_at_utc": manifest["started_at_utc"],
                "trial_id": card["hypothesis_id"], "family": card["family"],
                "status": "REGISTERED", "result_opened": False,
                "split": "EXPLORATORY_ONLY",
            }, sort_keys=True) + "\n")
    (run_dir / "RESEARCH_DIRECTION_LEDGER.md").write_text(
        bootstrap.direction_ledger(), encoding="utf-8"
    )
    (run_dir / "METHODS.md").write_text(
        bootstrap.methods_markdown(), encoding="utf-8"
    )
    write_json(run_dir / "PRIOR_EXPOSURE.json", prior_exposure(
        manifest["run_id"], manifest["holdout_cutoff_utc"]
    ))
    features = feature_dictionary()
    write_json(run_dir / "FEATURE_DICTIONARY.json", features)
    (run_dir / "FEATURE_DICTIONARY.md").write_text(
        "# FEATURE DICTIONARY — frozen before results\n\n"
        "> EXPLORATORY_ONLY · receive-clock causal features\n\n"
        + "\n".join(f"## {f['name']}\n\n{f['definition']}\n\n- formula: `{f['formula']}`\n- units: {f['units']}\n- null: {f['null']}\n" for f in features["features"]),
        encoding="utf-8",
    )
    repo_root = source_dir.parents[2]
    execution_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()
    source_git_status = subprocess.check_output(
        ["git", "status", "--porcelain", "--", str(source_dir.relative_to(repo_root))],
        cwd=repo_root, text=True,
    ).strip()
    if source_git_status:
        raise SystemExit(
            "research source tree is not committed/clean at freeze:\n" + source_git_status
        )
    source_files = sorted(path for path in source_dir.glob("*")
                          if path.is_file() and path.suffix in (".py", ".sh"))
    source_manifest = [{"path": str(path.relative_to(source_dir.parent.parent.parent)),
                        "sha256": sha256(path), "bytes": path.stat().st_size}
                       for path in source_files]
    write_json(run_dir / "SOURCE_MANIFEST.json", source_manifest)
    (run_dir / "SOURCE_SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {Path(item['path']).name}\n" for item in source_manifest),
        encoding="utf-8",
    )
    query_dir = run_dir / "queries"
    query_dir.mkdir(parents=True, exist_ok=True)
    query_sources = [
        source_dir / "run_cycle1.py",
        source_dir / "core_hypothesis_tests.py",
        source_dir / "l2_hypothesis_stage.py",
        source_dir / "rfq_trigger.py",
        source_dir / "rfq_full_stage.py",
        source_dir / "finalize_mission.py",
    ]
    for path in query_sources:
        if not path.is_file():
            raise SystemExit(f"registered query implementation missing: {path}")
        shutil.copyfile(path, query_dir / path.name)
    (run_dir / "QUERY_SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  queries/{path.name}\n" for path in query_sources),
        encoding="utf-8",
    )
    source_manifest_sha = sha256(run_dir / "SOURCE_MANIFEST.json")
    selected = []
    for release in bootstrap.RELEASES:
        release_id = release["release_id"]
        source_manifest_path = cycle0 / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
        source_verified_path = cycle0 / "DATA_INTEGRITY/verified" / f"{release['date']}.json"
        data_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        verified = json.loads(source_verified_path.read_text(encoding="utf-8"))
        target_manifest = run_dir / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
        target_verified = run_dir / "DATA_INTEGRITY/verified" / f"{release['date']}.json"
        shutil.copyfile(source_manifest_path, target_manifest)
        shutil.copyfile(source_verified_path, target_verified)
        payload, object_digest = version_list(data_manifest)
        if object_digest != EXPECTED_OBJECT_SET[release_id]:
            raise SystemExit(f"object VersionId set mismatch: {release_id}")
        version_path = run_dir / "DATA_INTEGRITY/version_ids" / f"{release['date']}.jsonl"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_bytes(payload)
        selected.append({
            "date": release["date"], "release_id": release_id,
            "evidence_tier": verified["evidence_tier"], "tl1_status": verified["tl1_status"],
            "manifest_sha256": sha256(target_manifest),
            "seal_sha256": verified["seal_sha256"],
            "publication_state_sha256": verified["publication_state_sha256"],
            "version_binding_mode": verified["version_binding_mode"],
            "version_bindings_sha256": data_manifest["version_binding"]["bindings_sha256"],
            "object_version_set_sha256": object_digest,
            "object_count": verified["objects_verified"],
            "byte_count": verified["bytes_verified"],
            "corrections_total": verified["corrections_total"],
            "exact_version_list_path": str(version_path.relative_to(run_dir)),
            "include": "EXPLORATORY_ONLY",
        })
    manifest["schema_version"] = "sports-autoresearch-run-manifest-v2"
    manifest["status"] = "REGISTRATION_FROZEN_NO_RESULTS"
    manifest["analysis_started"] = False
    manifest["selected_releases"] = selected
    manifest["repository"] = {
        "bootstrap_commit": manifest["repo_commit"],
        "execution_commit": execution_commit,
        "source_tree_dirty_at_freeze": bool(source_git_status),
        "source_manifest_path": "SOURCE_MANIFEST.json",
        "source_manifest_sha256": source_manifest_sha,
        "feature_definition_sha256": sha256(run_dir / "FEATURE_DICTIONARY.json"),
        "method_definition_sha256": sha256(run_dir / "METHODS.md"),
        "query_set_sha256": sha256(run_dir / "QUERY_SHA256SUMS.txt"),
        "query_files": [f"queries/{path.name}" for path in query_sources],
        "simulation_or_replay": "SNAPSHOT_AWARE_L2_REPLAY_AND_STRICT_THROUGH_DIAGNOSTICS_ONLY; no own-order fill reconstruction",
    }
    manifest["gates"] = {
        "gate_a": {"status": "PENDING_SAME_RUN_REVERIFY"},
        "gate_b": {"status": "PENDING_SAME_RUN_W09_ATTESTATION"},
        "gate_c": {"status": "PASS_MODE1_ONLY", "mode2_authorized": False},
    }
    manifest["publication"] = {
        "canonical_local_archive": str(run_dir),
        "s3_report_archive_status": "DEFERRED_AUTHORITY_CONFLICT",
        "reason": "W09 is read-only and base mission forbids S3 modification; no write attempted.",
    }
    manifest["trial_policy"] = {
        "cycle": 1, "max_new_hypotheses": 10, "registered_initial": 10,
        "own_data_anomaly_hypotheses": 2, "append_only_registry": True,
    }
    release_path = run_dir / "OPERATOR_RELEASE.md"
    release_text = release_path.read_text(encoding="utf-8")
    release_text = release_text.replace(
        "# OPERATOR RELEASE\n\n",
        "# OPERATOR RELEASE\n\n"
        f"- run_id: `{manifest['run_id']}`\n"
        "- authority_status: `RELEASED_FOR_EXPLORATORY_RESEARCH_ONLY`\n"
        f"- repo_commit: `{manifest['repo_commit']}`\n"
        "- target_compute: `existing W09 only`\n",
        1,
    )
    release_text += (
        "\n## Authority boundary\n\n"
        "Production EC2 access, Mode 2, validation/confirmation, freeze, "
        "PROMOTION_READY, verdict, S3 modification and live trading are not authorized.\n"
    )
    release_path.write_text(release_text, encoding="utf-8")
    write_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cycle0-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    freeze(args.run_dir.resolve(), args.cycle0_dir.resolve(), args.source_dir.resolve())
    print("REGISTRATION_FROZEN", args.run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
