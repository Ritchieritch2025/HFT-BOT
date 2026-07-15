#!/usr/bin/env python3
"""W09-only frozen Cycle-1 hypothesis tests.

This stage consumes the read-only ``cycle1.duckdb`` produced by
``run_cycle1.py``.  It completes causal, event-level *exploratory* subtests for
the three registered core cards without changing the run manifest or the
hypothesis ledger.  It intentionally cannot turn unknown fees, latency, or an
unobserved own-order lifecycle into PnL.

Every strategy-relevant price quantity is computed in log-odds.  E4 values are
retained only as presentation diagnostics.  All uncertainty is clustered at
root-event/day level; the current 2026-07-13 evaluation therefore has one
degenerate day block and can never support a verdict or promotion.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import platform
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from stats import block_bootstrap_mean, finite, percentile


EXPECTED_INSTANCE = "i-0e53d134dceffe166"
EXPECTED_REGION = "us-east-2"
EXPECTED_ROLE = "w09-research-runner"
EXPECTED_DUCKDB = "1.4.5"
EXPECTED_W09_INSTALLATION_SHA256 = {
    "/usr/local/bin/research_data": "68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f",
    "/opt/w09/research/tools/research_data_instance_profile.py": "af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf",
    "/usr/local/bin/w09-run": "6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321",
    "/etc/w09/cost-contract.json": "bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352",
}
EXPECTED_EVIDENCE = "SEALED_DEGRADED_EVIDENCE"
EXPECTED_MISSION_SHA = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
EXPECTED_MANIFEST_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING"
EXPECTED_RELEASE_MANIFESTS = {
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03":
        "6fedbd5d2b0d811b5189a953331bd236aeea3fb5843a5a549d1100d5ee290505",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5":
        "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd",
}
TRAIN_DATE = "2026-07-12"
EVAL_DATE = "2026-07-13"
BOOTSTRAP_SEED = 20260715
MIN_BOOTSTRAP_REPLICATES = 1000
DEFAULT_BOOTSTRAP_REPLICATES = 2000
MIN_THRESHOLD_OBSERVATIONS = 500
MIN_RELEVANT_ROOT_EVENTS = 200
MIN_INDEPENDENT_DAY_BLOCKS = 20
BOOK_AGE_CAP_US = 5_000_000
ACTIVE_LOOKBACK_US = 60_000_000
UNRELATED_FLOW_LOOKBACK_US = 1_000_000
DELAYED_EXECUTION_US = 1_000_000
FLOW_HORIZONS_US = (100_000, 1_000_000, 3_000_000, 10_000_000, 30_000_000, 120_000_000)
SPREAD_HORIZONS_US = (100_000, 1_000_000, 5_000_000, 30_000_000, 120_000_000)
TTS_HORIZONS_US = (1_000_000, 10_000_000, 30_000_000, 120_000_000)
STAGE = "CYCLE1_CORE_HYPOTHESIS_TESTS"
BANNER = "EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION"


class CoreHypothesisError(RuntimeError):
    """Fail-closed prerequisite or data-integrity error."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def rows_as_dicts(con, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def scalar(con, sql: str):
    return con.execute(sql).fetchone()[0]


def mid_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,{column}))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"(({logit(bid)})+({logit(ask)}))/2.0"


def spread_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,{column}))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"({logit(ask)})-({logit(bid)})"


def distribution(values: Iterable[float | int | None]) -> dict:
    xs = finite(values)
    if not xs:
        return {
            "n": 0,
            "mean": None,
            "min": None,
            "p01": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "n": len(xs),
        "mean": sum(xs) / len(xs),
        "min": min(xs),
        "p01": percentile(xs, 0.01),
        "p05": percentile(xs, 0.05),
        "p50": percentile(xs, 0.50),
        "p95": percentile(xs, 0.95),
        "p99": percentile(xs, 0.99),
        "max": max(xs),
    }


def event_inference(
    rows: Sequence[Mapping[str, object]],
    *,
    value_key: str = "effect_logodds",
    replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Summarize one already root-aggregated effect per date/root.

    A one-day bootstrap is run, rather than suppressed, so the artifact proves
    the registered code path executed.  It is prominently marked degenerate:
    resampling a single day cannot estimate day-to-day uncertainty.
    """
    if replicates < MIN_BOOTSTRAP_REPLICATES:
        raise ValueError(f"replicates must be >= {MIN_BOOTSTRAP_REPLICATES}")
    by_day: dict[str, dict[str, float]] = defaultdict(dict)
    values: list[float] = []
    for row in rows:
        value = row.get(value_key)
        day = row.get("date")
        root = row.get("root_event_id")
        if value is None or day is None or root is None:
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        key = str(root)
        if key in by_day[str(day)]:
            raise ValueError("event table must contain one row per date/root")
        by_day[str(day)][key] = number
        values.append(number)
    boot = block_bootstrap_mean(by_day, replicates=replicates, seed=seed)
    n_days = int(boot["n_days"])
    boot["diagnostic_only"] = True
    boot["degenerate"] = n_days <= 1
    boot["degenerate_reason"] = (
        "Only one evaluation calendar-day block; every resample is the same day and "
        "the interval is not an uncertainty or power estimate."
        if n_days <= 1
        else None
    )
    day_means = {
        day: sum(event_values.values()) / len(event_values)
        for day, event_values in sorted(by_day.items())
    }
    delete_best_root = None
    if len(values) > 1:
        reduced = list(values)
        reduced.remove(max(reduced))
        delete_best_root = sum(reduced) / len(reduced)
    delete_best_day = None
    if len(day_means) > 1:
        best_day = max(day_means, key=day_means.get)
        reduced = [
            number
            for day, event_values in by_day.items()
            if day != best_day
            for number in event_values.values()
        ]
        if reduced:
            delete_best_day = sum(reduced) / len(reduced)
    return {
        "unit_of_inference": "root_event",
        "day_block": "UTC calendar date",
        "effect_distribution": distribution(values),
        "day_means": day_means,
        "delete_best_root_event_mean": delete_best_root,
        "delete_best_day_mean": delete_best_day,
        "bootstrap": boot,
    }


def exploratory_status(
    *,
    n_roots: int,
    n_days: int,
    integrity_ok: bool,
    estimand_available: bool = True,
) -> tuple[str, str]:
    """Allowed MODE-1 status; never reject solely from one/two day blocks."""
    if not integrity_ok or not estimand_available or n_roots == 0 or n_days == 0:
        return "DATA_STARVED", "Required causal estimand or integrity support is absent."
    if n_days < MIN_INDEPENDENT_DAY_BLOCKS or n_roots < MIN_RELEVANT_ROOT_EVENTS:
        return (
            "COLLECT_MORE",
            f"Observed {n_roots} relevant root events and {n_days} evaluation day blocks; "
            f"the frozen minimum is {MIN_RELEVANT_ROOT_EVENTS} roots and "
            f"{MIN_INDEPENDENT_DAY_BLOCKS} independent days. No rejection is inferred "
            "from an underpowered one/two-day sample.",
        )
    return (
        "CANDIDATE",
        "Minimum descriptive support exists; status remains MODE-1 exploratory and is not a verdict.",
    )


def validate_mode1_contract(
    manifest: Mapping[str, object],
    attestation: Mapping[str, object],
    *,
    system: str,
    machine: str,
) -> None:
    if manifest.get("mode") != "EXPLORATORY_AUTORESEARCH":
        raise CoreHypothesisError("run is not MODE-1 EXPLORATORY_AUTORESEARCH")
    if (manifest.get("mission") or {}).get("sha256") != EXPECTED_MISSION_SHA:
        raise CoreHypothesisError("operator-pinned mission SHA mismatch")
    if manifest.get("status") != EXPECTED_MANIFEST_STATUS or not manifest.get(
        "analysis_started"
    ):
        raise CoreHypothesisError("manifest is not at the exact completed Cycle-1 core state")
    gates = manifest.get("gates") or {}
    if not str((gates.get("gate_a") or {}).get("status", "")).startswith("PASS_"):
        raise CoreHypothesisError("Gate A is not same-run PASS")
    if not str((gates.get("gate_b") or {}).get("status", "")).startswith("PASS_"):
        raise CoreHypothesisError("Gate B is not same-run PASS")
    gate_c = gates.get("gate_c") or {}
    if gate_c.get("status") != "PASS_MODE1_ONLY" or gate_c.get("mode2_authorized") is not False:
        raise CoreHypothesisError("Gate C does not explicitly prohibit MODE-2")
    selected = list(manifest.get("selected_releases") or [])
    release_ids = {str(item.get("release_id")) for item in selected}
    if len(selected) != 2 or release_ids != set(EXPECTED_RELEASE_MANIFESTS):
        raise CoreHypothesisError("selected release IDs are not the exact frozen two-release set")
    if any(item.get("evidence_tier") != EXPECTED_EVIDENCE for item in selected):
        raise CoreHypothesisError("selected releases are not exclusively SEALED_DEGRADED_EVIDENCE")
    if any(
        item.get("manifest_sha256") != EXPECTED_RELEASE_MANIFESTS[item["release_id"]]
        for item in selected
    ):
        raise CoreHypothesisError("selected release manifest SHA mismatch")
    if system != "Linux" or machine.lower() not in {"aarch64", "arm64"}:
        raise CoreHypothesisError("heavy stage is W09-only and requires Linux arm64")
    if (
        attestation.get("instance_id") != EXPECTED_INSTANCE
        or attestation.get("region") != EXPECTED_REGION
        or attestation.get("role") != EXPECTED_ROLE
        or attestation.get("instance_profile") != EXPECTED_ROLE
        or attestation.get("architecture") != "aarch64"
        or attestation.get("duckdb") != EXPECTED_DUCKDB
        or attestation.get("w09_run_inhibitor_present") is not True
        or attestation.get("w09_run_inhibitor_is_ancestor") is not True
        or attestation.get("static_credentials_present") is not False
        or attestation.get("trading_credentials_present") is not False
        or attestation.get("ambient_aws_or_kalshi_variables") != []
        or attestation.get("static_credential_paths_present") != []
        or attestation.get("installation_sha256") != EXPECTED_W09_INSTALLATION_SHA256
        or attestation.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"
    ):
        raise CoreHypothesisError("W09 identity/version/inhibitor attestation mismatch")


def validate_run_identity(run_dir: Path) -> tuple[dict, dict]:
    manifest_path = run_dir / "RUN_MANIFEST.json"
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    source_manifest_path = run_dir / "SOURCE_MANIFEST.json"
    for path in (manifest_path, attestation_path, source_manifest_path):
        if not path.is_file():
            raise CoreHypothesisError(f"required frozen artifact missing: {path.name}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_dir.name:
        raise CoreHypothesisError("run id/path mismatch")
    validate_mode1_contract(
        manifest,
        attestation,
        system=platform.system(),
        machine=platform.machine(),
    )
    gates = manifest["gates"]
    expected_attestation = (gates.get("gate_b") or {}).get("attestation_sha256")
    if not expected_attestation or sha256(attestation_path) != expected_attestation:
        raise CoreHypothesisError("Gate B is not bound to current W09 attestation bytes")
    repository = manifest.get("repository") or {}
    if sha256(source_manifest_path) != repository.get("source_manifest_sha256"):
        raise CoreHypothesisError("SOURCE_MANIFEST is not bound to RUN_MANIFEST")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    own_path = Path(__file__).resolve()
    own_entries = [item for item in source_manifest if Path(item["path"]).name == own_path.name]
    if len(own_entries) != 1 or own_entries[0].get("sha256") != sha256(own_path):
        raise CoreHypothesisError("running source is not the unique frozen source-manifest entry")
    selected = {item["release_id"]: item for item in manifest["selected_releases"]}
    for release_id, expected_sha in EXPECTED_RELEASE_MANIFESTS.items():
        release_manifest = (
            run_dir / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
        )
        if not release_manifest.is_file() or sha256(release_manifest) != expected_sha:
            raise CoreHypothesisError(
                f"run-internal exact release manifest mismatch: {release_id}"
            )
        if selected[release_id].get("manifest_sha256") != expected_sha:
            raise CoreHypothesisError(
                f"selected release is not bound to internal manifest: {release_id}"
            )
    if not (run_dir / "REPORT/CYCLE1_CORE_SUMMARY.json").is_file():
        raise CoreHypothesisError("Cycle-1 core did not complete before full hypothesis tests")
    return manifest, attestation


def validate_database(con) -> None:
    required = {
        "capture_gaps",
        "flow_thresholds",
        "l1_day_bounds",
        "l1_intervals",
        "l1_real",
        "markout_events",
        "trade_pre",
        "universe",
    }
    available = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    missing = sorted(required - available)
    if missing:
        raise CoreHypothesisError(f"cycle1 database missing tables: {missing}")
    if scalar(con, "SELECT count(*) FROM markout_events WHERE book_t_us>=t_us"):
        raise CoreHypothesisError("strict decision-book lookahead sentinel failed")
    if scalar(con, "SELECT count(*) FROM markout_events WHERE post_book_t_us>target_us"):
        raise CoreHypothesisError("outcome-book lookahead sentinel failed")


def configure(con, run_dir: Path, memory_limit: str, threads: int, temp_limit: str) -> None:
    temp = run_dir / "tmp/core_hypothesis_tests"
    temp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit={quote(memory_limit)}")
    con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET temp_directory={quote(temp)}")
    con.execute(f"SET max_temp_directory_size={quote(temp_limit)}")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET TimeZone='UTC'")


def build_regimes(con) -> dict:
    spread = spread_logodds_sql("l.yes_bid_e4", "l.yes_ask_e4")
    mid = mid_logodds_sql("l.yes_bid_e4", "l.yes_ask_e4")
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_l1_features AS
      WITH eligible AS (
        SELECT l.date,l.t_us,l.recv_wall_ns,l.recv_mono_ns,l.market_ticker,
               u.family_event_id,u.root_event_id,u.sport,u.league,
               epoch_us(u.occurrence_datetime) AS occurrence_us,u.dim_effective_us,
               l.yes_bid_e4,l.yes_ask_e4,
               ({spread}) AS spread_logodds,({mid}) AS midpoint_logodds
        FROM l1_real l JOIN universe u USING(date,market_ticker)
        WHERE l.date IN (DATE {quote(TRAIN_DATE)},DATE {quote(EVAL_DATE)})
          AND u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
          AND u.root_event_id IS NOT NULL
          AND u.dim_effective_us IS NOT NULL AND l.t_us>=u.dim_effective_us
          AND l.yes_bid_e4>0 AND l.yes_ask_e4<10000
          AND l.yes_bid_e4<l.yes_ask_e4
          AND NOT EXISTS (
            SELECT 1 FROM capture_gaps g
            WHERE g.start_us<=l.t_us AND g.end_us>l.t_us
          )
      )
      SELECT *,count(*) OVER (
               PARTITION BY date,market_ticker ORDER BY t_us
               RANGE BETWEEN {ACTIVE_LOOKBACK_US} PRECEDING AND 1 PRECEDING
             ) AS prior_60s_message_count
      FROM eligible
    """)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_regime_thresholds AS
      SELECT sport,quantile_cont(spread_logodds,0.25) AS tight_q25_spread_logodds,
             quantile_cont(prior_60s_message_count,0.75) AS active_q75_prior_60s_messages,
             count(*) AS train_observations,count(DISTINCT root_event_id) AS train_roots,
             count(DISTINCT market_ticker) AS train_markets
      FROM ch_l1_features WHERE date=DATE {quote(TRAIN_DATE)}
      GROUP BY sport HAVING count(*)>={MIN_THRESHOLD_OBSERVATIONS}
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_l1_regimes AS
      SELECT f.*,t.tight_q25_spread_logodds,t.active_q75_prior_60s_messages,
        CASE WHEN f.t_us>=f.occurrence_us THEN 'IN_PLAY'
             WHEN f.prior_60s_message_count>=t.active_q75_prior_60s_messages
                  AND f.spread_logodds<=t.tight_q25_spread_logodds THEN 'ACTIVE_TIGHT'
             WHEN f.prior_60s_message_count>=t.active_q75_prior_60s_messages
               THEN 'ACTIVE_WIDE'
             ELSE 'DORMANT' END AS regime
      FROM ch_l1_features f JOIN ch_regime_thresholds t USING(sport)
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_regime_transitions AS
      WITH ordered AS (
        SELECT *,lag(regime) OVER (
                 PARTITION BY date,market_ticker
                 ORDER BY t_us,recv_wall_ns,recv_mono_ns
               ) AS prior_regime,
               lag(t_us) OVER (
                 PARTITION BY date,market_ticker
                 ORDER BY t_us,recv_wall_ns,recv_mono_ns
               ) AS prior_t_us
        FROM ch_l1_regimes
      )
      SELECT * FROM ordered
      WHERE prior_regime IS NOT NULL AND regime<>prior_regime
    """)
    input_screen = rows_as_dicts(
        con,
        f"""
        SELECT l.date,
          count(*) AS provisional_root_l1_rows,
          count(*) FILTER (
            WHERE u.dim_effective_us IS NULL OR l.t_us<u.dim_effective_us
          ) AS excluded_before_dim_effective,
          count(*) FILTER (
            WHERE NOT (l.yes_bid_e4>0 AND l.yes_ask_e4<10000
                       AND l.yes_bid_e4<l.yes_ask_e4)
          ) AS excluded_invalid_two_sided,
          count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM capture_gaps g
            WHERE g.start_us<=l.t_us AND g.end_us>l.t_us
          )) AS excluded_inside_declared_gap
        FROM l1_real l JOIN universe u USING(date,market_ticker)
        WHERE l.date IN (DATE {quote(TRAIN_DATE)},DATE {quote(EVAL_DATE)})
          AND u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
          AND u.root_event_id IS NOT NULL
        GROUP BY l.date ORDER BY l.date
        """,
    )
    return {
        "definition": {
            "train_date": TRAIN_DATE,
            "evaluation_date": EVAL_DATE,
            "tight": "sport q25 current executable log-odds spread on train date",
            "active": "sport q75 strictly-prior 60-second L1 receive-message count on train date",
            "active_lookback_us": ACTIVE_LOOKBACK_US,
            "minimum_train_observations_per_sport": MIN_THRESHOLD_OBSERVATIONS,
            "thresholds_are_outcome_blind": True,
        },
        "thresholds": rows_as_dicts(
            con,
            "SELECT * FROM ch_regime_thresholds ORDER BY sport",
        ),
        "input_screen": input_screen,
        "sports_below_threshold_minimum": rows_as_dicts(
            con,
            "SELECT f.sport,count(*) AS eligible_train_rows "
            "FROM ch_l1_features f LEFT JOIN ch_regime_thresholds t USING(sport) "
            "WHERE f.date=DATE '2026-07-12' AND t.sport IS NULL "
            "GROUP BY f.sport ORDER BY f.sport",
        ),
        "train_feature_rows": scalar(
            con, f"SELECT count(*) FROM ch_l1_features WHERE date=DATE {quote(TRAIN_DATE)}"
        ),
        "eval_feature_rows": scalar(
            con, f"SELECT count(*) FROM ch_l1_features WHERE date=DATE {quote(EVAL_DATE)}"
        ),
        "eval_roots": scalar(
            con,
            f"SELECT count(DISTINCT root_event_id) FROM ch_l1_regimes "
            f"WHERE date=DATE {quote(EVAL_DATE)}",
        ),
        "eval_transitions": scalar(
            con,
            f"SELECT count(*) FROM ch_regime_transitions WHERE date=DATE {quote(EVAL_DATE)}",
        ),
    }


def build_trade_covariates(con) -> dict:
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_trade_context_base AS
      SELECT p.*,u.family_event_id,r.regime,r.spread_logodds,
             r.midpoint_logodds,r.prior_60s_message_count,
             f.q99_count_e4,f.train_trades,
             extract(hour FROM to_timestamp(p.t_us/1000000.0))::INTEGER AS utc_hour,
             CASE WHEN r.midpoint_logodds<-4.5 THEN 0
                  WHEN r.midpoint_logodds<-3.0 THEN 1
                  WHEN r.midpoint_logodds<-1.5 THEN 2
                  WHEN r.midpoint_logodds<0.0 THEN 3
                  WHEN r.midpoint_logodds<1.5 THEN 4
                  WHEN r.midpoint_logodds<3.0 THEN 5
                  WHEN r.midpoint_logodds<4.5 THEN 6 ELSE 7 END AS price_logodds_band,
             CASE WHEN r.spread_logodds<=0.05 THEN 0
                  WHEN r.spread_logodds<=0.10 THEN 1
                  WHEN r.spread_logodds<=0.25 THEN 2
                  WHEN r.spread_logodds<=0.50 THEN 3 ELSE 4 END AS spread_logodds_band,
             CASE WHEN r.prior_60s_message_count=0 THEN 0
                  WHEN r.prior_60s_message_count<=2 THEN 1
                  WHEN r.prior_60s_message_count<=9 THEN 2
                  WHEN r.prior_60s_message_count<=29 THEN 3 ELSE 4 END AS activity_band
      FROM (SELECT * FROM trade_pre ORDER BY date,market_ticker,t_us) p
      JOIN universe u USING(date,market_ticker)
      LEFT JOIN flow_thresholds f ON f.sport=p.sport
      ASOF LEFT JOIN (
        SELECT * FROM ch_l1_regimes ORDER BY date,market_ticker,t_us
      ) r
        ON p.date=r.date AND p.market_ticker=r.market_ticker AND p.t_us>r.t_us
      WHERE p.date IN (DATE {quote(TRAIN_DATE)},DATE {quote(EVAL_DATE)})
        AND p.root_event_id IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM capture_gaps g
          WHERE g.start_us<p.t_us AND g.end_us>p.book_t_us
        )
    """)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_trade_context AS
      WITH future AS (
        SELECT *,lead(count_e4) OVER (
                   PARTITION BY date,market_ticker ORDER BY t_us,trade_id
                 ) AS future_count_e4,
                 lead(t_us) OVER (
                   PARTITION BY date,market_ticker ORDER BY t_us,trade_id
                 ) AS future_t_us
        FROM ch_trade_context_base
        WHERE regime IS NOT NULL AND q99_count_e4 IS NOT NULL
      ), prevalence AS (
        SELECT *,count(*) FILTER (
                   WHERE date=DATE {quote(EVAL_DATE)} AND count_e4>=q99_count_e4
                 ) OVER (PARTITION BY date,sport,utc_hour) AS stratum_large_count,
                 row_number() OVER (
                   PARTITION BY date,sport,utc_hour
                   ORDER BY hash(trade_id,{BOOTSTRAP_SEED})
                 ) AS shuffled_rank
        FROM future
      )
      SELECT *,
             date=DATE {quote(EVAL_DATE)} AND count_e4>=q99_count_e4
               AS large_flow_eval,
             date=DATE {quote(EVAL_DATE)} AND future_t_us<=t_us+{ACTIVE_LOOKBACK_US}
               AND future_count_e4>=q99_count_e4 AS future_flow_eval,
             date=DATE {quote(EVAL_DATE)} AND shuffled_rank<=stratum_large_count
               AS event_label_shuffle_eval
      FROM prevalence
    """)
    # Rotate complete root events within date/sport, then expose only a prior
    # large-flow timestamp from the assigned unrelated root.  No actor identity
    # is inferred and no future unrelated event can enter the signal.
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_root_rotation AS
      WITH roots AS (
        SELECT DISTINCT date,sport,root_event_id FROM ch_trade_context
        WHERE date=DATE '2026-07-13'
      ), rotated AS (
        SELECT *,lead(root_event_id) OVER (
                   PARTITION BY date,sport ORDER BY hash(root_event_id,20260715)
                 ) AS next_root,
                 first_value(root_event_id) OVER (
                   PARTITION BY date,sport ORDER BY hash(root_event_id,20260715)
                 ) AS first_root,
                 count(*) OVER (PARTITION BY date,sport) AS n_roots
        FROM roots
      )
      SELECT date,sport,root_event_id AS target_root_event_id,
             coalesce(next_root,first_root) AS source_root_event_id
      FROM rotated WHERE n_roots>=2
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_unrelated_anchors AS
      SELECT r.date,r.sport,r.target_root_event_id,t.t_us AS unrelated_anchor_us
      FROM ch_root_rotation r JOIN ch_trade_context t
        ON t.date=r.date AND t.sport=r.sport
       AND t.root_event_id=r.source_root_event_id
      WHERE t.large_flow_eval
    """)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_trade_labels AS
      SELECT t.*,a.unrelated_anchor_us,
             a.unrelated_anchor_us IS NOT NULL
               AND t.t_us-a.unrelated_anchor_us<={UNRELATED_FLOW_LOOKBACK_US}
               AS unrelated_event_flow_eval
      FROM (SELECT * FROM ch_trade_context ORDER BY date,sport,root_event_id,t_us) t
      ASOF LEFT JOIN (
        SELECT * FROM ch_unrelated_anchors
        ORDER BY date,sport,target_root_event_id,unrelated_anchor_us
      ) a
        ON t.date=a.date AND t.sport=a.sport
       AND t.root_event_id=a.target_root_event_id
       AND t.t_us>a.unrelated_anchor_us
    """)
    return {
        "upstream_trade_pre_rows": scalar(con, "SELECT count(*) FROM trade_pre"),
        "trade_context_base_rows": scalar(con, "SELECT count(*) FROM ch_trade_context_base"),
        "excluded_missing_regime_or_flow_threshold": scalar(
            con,
            "SELECT count(*) FROM ch_trade_context_base "
            "WHERE regime IS NULL OR q99_count_e4 IS NULL",
        ),
        "trade_rows_with_causal_regime": scalar(con, "SELECT count(*) FROM ch_trade_context"),
        "evaluation_trade_rows": scalar(
            con, f"SELECT count(*) FROM ch_trade_context WHERE date=DATE {quote(EVAL_DATE)}"
        ),
        "large_flow_treatments": scalar(
            con, "SELECT count(*) FROM ch_trade_context WHERE large_flow_eval"
        ),
        "future_flow_negative_treatments": scalar(
            con, "SELECT count(*) FROM ch_trade_context WHERE future_flow_eval"
        ),
        "event_shuffle_negative_treatments": scalar(
            con, "SELECT count(*) FROM ch_trade_context WHERE event_label_shuffle_eval"
        ),
        "unrelated_flow_negative_treatments": scalar(
            con, "SELECT count(*) FROM ch_trade_labels WHERE unrelated_event_flow_eval"
        ),
        "sports_without_registered_flow_threshold": rows_as_dicts(
            con,
            "SELECT sport,count(*) AS trades FROM ch_trade_context_base "
            "WHERE q99_count_e4 IS NULL GROUP BY sport ORDER BY sport",
        ),
    }


def build_large_flow(con) -> tuple[list[dict], dict]:
    horizons = ",".join(str(value) for value in FLOW_HORIZONS_US)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_flow_scored AS
      WITH scored AS (
        SELECT m.date,m.trade_id,m.market_ticker,m.root_event_id,t.family_event_id,
               m.sport,m.t_us,m.target_us,m.horizon_us,m.signed_move_logodds,
               t.utc_hour,t.price_logodds_band,t.spread_logodds_band,t.activity_band,
               t.large_flow_eval,t.future_flow_eval,t.event_label_shuffle_eval,
               t.unrelated_event_flow_eval
        FROM markout_events m JOIN ch_trade_labels t USING(trade_id)
        WHERE m.date=DATE {quote(EVAL_DATE)} AND m.horizon_us IN ({horizons})
      )
      SELECT * EXCLUDE(large_flow_eval,future_flow_eval,event_label_shuffle_eval,
                       unrelated_event_flow_eval),
             'main_large_flow' AS signal_name,large_flow_eval AS treated FROM scored
      UNION ALL
      SELECT * EXCLUDE(large_flow_eval,future_flow_eval,event_label_shuffle_eval,
                       unrelated_event_flow_eval),
             'negative_future_flow' AS signal_name,future_flow_eval AS treated FROM scored
      UNION ALL
      SELECT * EXCLUDE(large_flow_eval,future_flow_eval,event_label_shuffle_eval,
                       unrelated_event_flow_eval),
             'negative_event_label_shuffle' AS signal_name,
             event_label_shuffle_eval AS treated FROM scored
      UNION ALL
      SELECT * EXCLUDE(large_flow_eval,future_flow_eval,event_label_shuffle_eval,
                       unrelated_event_flow_eval),
             'negative_unrelated_event_flow' AS signal_name,
             unrelated_event_flow_eval AS treated FROM scored
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_flow_pairs AS
      SELECT t.signal_name,t.date,t.trade_id AS treatment_trade_id,
             t.market_ticker,t.root_event_id,t.sport,t.horizon_us,t.t_us,
             t.signed_move_logodds AS treatment_signed_move_logodds,
             c.trade_id AS control_trade_id,c.target_us AS control_target_us,
             c.signed_move_logodds AS control_signed_move_logodds,
             t.signed_move_logodds-c.signed_move_logodds AS effect_logodds,
             t.t_us-c.target_us AS completed_control_lag_us
      FROM (
        SELECT * FROM ch_flow_scored WHERE treated
        ORDER BY signal_name,date,market_ticker,family_event_id,utc_hour,
                 price_logodds_band,spread_logodds_band,activity_band,horizon_us,t_us
      ) t
      ASOF LEFT JOIN (
        SELECT * FROM ch_flow_scored WHERE NOT treated
        ORDER BY signal_name,date,market_ticker,family_event_id,utc_hour,
                 price_logodds_band,spread_logodds_band,activity_band,horizon_us,target_us
      ) c
        ON t.signal_name=c.signal_name AND t.date=c.date
       AND t.market_ticker=c.market_ticker
       AND coalesce(t.family_event_id,'_NULL')=coalesce(c.family_event_id,'_NULL')
       AND t.utc_hour=c.utc_hour
       AND t.price_logodds_band=c.price_logodds_band
       AND t.spread_logodds_band=c.spread_logodds_band
       AND t.activity_band=c.activity_band
       AND t.horizon_us=c.horizon_us AND t.t_us>c.target_us
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_flow_event_effects AS
      SELECT signal_name,date,horizon_us,root_event_id,min(sport) AS sport,
             count(*) AS treatment_pairs,
             count(DISTINCT treatment_trade_id) AS treatment_signals,
             count(DISTINCT control_trade_id) AS unique_controls,
             avg(treatment_signed_move_logodds) AS treatment_mean_logodds,
             avg(control_signed_move_logodds) AS control_mean_logodds,
             avg(effect_logodds) AS effect_logodds
      FROM ch_flow_pairs WHERE control_trade_id IS NOT NULL
      GROUP BY signal_name,date,horizon_us,root_event_id
    """)
    event_rows = rows_as_dicts(
        con,
        "SELECT * FROM ch_flow_event_effects "
        "ORDER BY signal_name,horizon_us,date,root_event_id",
    )
    counts = rows_as_dicts(
        con,
        "SELECT signal_name,horizon_us,count(*) FILTER (WHERE treated) AS treatment_rows,"
        "count(*) FILTER (WHERE NOT treated) AS control_pool_rows "
        "FROM ch_flow_scored GROUP BY signal_name,horizon_us "
        "ORDER BY signal_name,horizon_us",
    )
    unmatched = rows_as_dicts(
        con,
        "SELECT signal_name,horizon_us,count(*) AS unmatched_treatments "
        "FROM ch_flow_pairs WHERE control_trade_id IS NULL "
        "GROUP BY signal_name,horizon_us ORDER BY signal_name,horizon_us",
    )
    analyses = analyze_effect_rows(event_rows, replicates=DEFAULT_BOOTSTRAP_REPLICATES)
    main_rows = [row for row in event_rows if row["signal_name"] == "main_large_flow"]
    sign_flip = [dict(row, effect_logodds=-float(row["effect_logodds"])) for row in main_rows]
    for row in sign_flip:
        row["signal_name"] = "negative_sign_flip"
    analyses.update(analyze_effect_rows(sign_flip, replicates=DEFAULT_BOOTSTRAP_REPLICATES))
    roots = len({str(row["root_event_id"]) for row in main_rows})
    days = len({str(row["date"]) for row in main_rows})
    status, reason = exploratory_status(
        n_roots=roots,
        n_days=days,
        integrity_ok=scalar(con, "SELECT count(*) FROM markout_events WHERE book_t_us>=t_us") == 0,
    )
    result = {
        "hypothesis_id": "C1-LARGE-FLOW-CONTINUATION-01",
        "hypothesis_status": status,
        "status_reason": reason,
        "estimand": "paired treatment-minus-control signed future log-odds move",
        "treatment": "2026-07-13 trade count >= frozen 2026-07-12 sport q99",
        "control": (
            "latest strictly prior non-treatment trade whose outcome completed before the "
            "treatment, exact-matched on market/family/UTC-hour/log-odds price band/"
            "log-odds spread band/past-60s activity band"
        ),
        "matching": {
            "direction": "prior-only",
            "control_outcome_must_complete_before_treatment": True,
            "with_replacement": True,
            "counts": counts,
            "unmatched": unmatched,
        },
        "registered_horizons_us": list(FLOW_HORIZONS_US),
        "root_events_with_matched_main_effect": roots,
        "evaluation_day_blocks": days,
        "inference": analyses,
        "negative_controls": [
            "sign flip",
            "future flow",
            "event-label shuffle preserving sport/hour prevalence",
            "causally prior flow from a deterministic unrelated root event",
        ],
        "economics": {
            "available": False,
            "reason": "Exact taker fees, latency, slippage, terminal exit and capacity are unratified.",
            "claim_boundary": "Predictive log-odds return only; no net-return or PnL claim.",
        },
        "multiplicity": {
            "status": "NOT_ESTIMABLE_ONE_EVALUATION_DAY",
            "policy": "BH-FDR within fixed horizon family once independent day support exists",
            "best_horizon_selection_forbidden": True,
        },
    }
    return event_rows + sign_flip, result


def build_spread_subtest(con) -> tuple[list[dict], dict]:
    horizons = ",".join(str(value) for value in SPREAD_HORIZONS_US)
    bid_lo = mid_logodds_sql("m.yes_bid_e4", "m.yes_bid_e4")
    ask_lo = mid_logodds_sql("m.yes_ask_e4", "m.yes_ask_e4")
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_spread_scored_base AS
      SELECT m.date,m.trade_id,m.market_ticker,m.root_event_id,m.sport,m.horizon_us,
             m.t_us,m.taker_sign,t.regime,
             t.regime='ACTIVE_TIGHT' AND t.t_us<epoch_us(t.occurrence_datetime)
               AS active_tight_prematch,
             CASE WHEN m.taker_sign=1 THEN ({ask_lo})-m.post_mid_logodds
                  ELSE m.post_mid_logodds-({bid_lo}) END AS maker_gross_markout_logodds,
             m.hypothetical_touch_gross_markout_e4 AS presentation_only_gross_markout_e4
      FROM markout_events m JOIN ch_trade_labels t USING(trade_id)
      WHERE m.date=DATE {quote(EVAL_DATE)} AND m.horizon_us IN ({horizons})
        AND m.strict_through_eligible_trade
        AND t.t_us<epoch_us(t.occurrence_datetime)
    """)
    # Deterministic prevalence-preserving label shuffle across market/trade rows.
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_spread_scored AS
      WITH ranked AS (
        SELECT *,count(*) FILTER (WHERE active_tight_prematch) OVER (
                   PARTITION BY date,sport,horizon_us
                 ) AS treatment_count,
                 row_number() OVER (
                   PARTITION BY date,sport,horizon_us
                   ORDER BY hash(market_ticker,trade_id,20260715)
                 ) AS shuffled_rank
        FROM ch_spread_scored_base
      )
      SELECT *,'main_active_tight' AS signal_name,
             active_tight_prematch AS treated FROM ranked
      UNION ALL
      SELECT *,'negative_market_label_shuffle' AS signal_name,
             shuffled_rank<=treatment_count AS treated FROM ranked
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_spread_event_effects AS
      SELECT signal_name,date,horizon_us,root_event_id,min(sport) AS sport,
             count(*) FILTER (WHERE treated) AS treatment_rows,
             count(*) FILTER (WHERE NOT treated) AS control_rows,
             avg(maker_gross_markout_logodds) FILTER (WHERE treated)
               AS treatment_mean_logodds,
             avg(maker_gross_markout_logodds) FILTER (WHERE NOT treated)
               AS control_mean_logodds,
             treatment_mean_logodds-control_mean_logodds AS effect_logodds,
             avg(presentation_only_gross_markout_e4) FILTER (WHERE treated)
               AS presentation_only_treatment_mean_e4,
             avg(presentation_only_gross_markout_e4) FILTER (WHERE NOT treated)
               AS presentation_only_control_mean_e4
      FROM ch_spread_scored
      GROUP BY signal_name,date,horizon_us,root_event_id
      HAVING treatment_rows>0 AND control_rows>0
    """)
    event_rows = rows_as_dicts(
        con,
        "SELECT * FROM ch_spread_event_effects "
        "ORDER BY signal_name,horizon_us,date,root_event_id",
    )
    main_rows = [row for row in event_rows if row["signal_name"] == "main_active_tight"]
    sign_flip = [dict(row, effect_logodds=-float(row["effect_logodds"])) for row in main_rows]
    for row in sign_flip:
        row["signal_name"] = "negative_sign_flip"

    # Delayed-execution control: compare maker-direction midpoint moves from
    # t+1s to t+2s.  It is deliberately not called a fill or spread return.
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_spread_delayed_control AS
      SELECT a.date,a.root_event_id,a.sport,a.trade_id,
             1000000::BIGINT AS horizon_us,
             CASE WHEN a.taker_sign=1 THEN a.post_mid_logodds-b.post_mid_logodds
                  ELSE b.post_mid_logodds-a.post_mid_logodds END
               AS delayed_maker_direction_markout_logodds,
             t.regime='ACTIVE_TIGHT' AND t.t_us<epoch_us(t.occurrence_datetime)
               AS treated
      FROM markout_events a JOIN markout_events b USING(trade_id)
      JOIN ch_trade_labels t USING(trade_id)
      WHERE a.date=DATE '2026-07-13' AND a.horizon_us=1000000
        AND b.horizon_us=2000000 AND a.strict_through_eligible_trade
        AND t.t_us<epoch_us(t.occurrence_datetime)
    """)
    delayed = rows_as_dicts(
        con,
        "SELECT 'negative_delayed_execution_1s' AS signal_name,date,horizon_us,"
        "root_event_id,min(sport) AS sport,"
        "count(*) FILTER (WHERE treated) AS treatment_rows,"
        "count(*) FILTER (WHERE NOT treated) AS control_rows,"
        "avg(delayed_maker_direction_markout_logodds) FILTER (WHERE treated) "
        "AS treatment_mean_logodds,"
        "avg(delayed_maker_direction_markout_logodds) FILTER (WHERE NOT treated) "
        "AS control_mean_logodds,"
        "treatment_mean_logodds-control_mean_logodds AS effect_logodds "
        "FROM ch_spread_delayed_control GROUP BY date,horizon_us,root_event_id "
        "HAVING treatment_rows>0 AND control_rows>0 "
        "ORDER BY date,root_event_id",
    )
    all_rows = event_rows + sign_flip + delayed
    result = {
        "hypothesis_id": "C1-SPREAD-CAPTURE-01",
        "hypothesis_status": "DATA_STARVED",
        "status_reason": (
            "The frozen primary estimand is event NetPnL including every zero-quote/zero-fill "
            "event, exact fees, latency, cancel exposure and forced exit. Those inputs and a "
            "certified full own-order lifecycle engine do not exist in this run."
        ),
        "full_economic_test": {
            "executed": False,
            "estimand_available": False,
            "blockers": [
                "fees.verified=false / no exact maker fee ratification",
                "latency placeholders",
                "no certified quote/place/cancel/replace/forced-exit lifecycle",
                "no valid reconstruction of zero-quote and zero-fill event economics",
                "only one evaluation day block",
            ],
            "claim_boundary": "No fill, realized opportunity, net economics, return or PnL claim.",
        },
        "gross_markout_subtest": {
            "executed": True,
            "estimand": (
                "active-tight-minus-other-regime gross maker-direction log-odds markout among "
                "historical strict price-through opportunities"
            ),
            "strict_through_boundary": (
                "Observed trade price strictly passed the last prior touch; at-price is false. "
                "This is eligibility evidence, not proof our unobserved order filled."
            ),
            "registered_horizons_us": list(SPREAD_HORIZONS_US),
            "root_event_results": analyze_effect_rows(
                all_rows, replicates=DEFAULT_BOOTSTRAP_REPLICATES
            ),
            "treatment_rows": scalar(
                con,
                "SELECT count(*) FROM ch_spread_scored_base WHERE active_tight_prematch",
            ),
            "control_rows": scalar(
                con,
                "SELECT count(*) FROM ch_spread_scored_base WHERE NOT active_tight_prematch",
            ),
            "root_event_rows": len(main_rows),
            "input_attrition": {
                "evaluation_markout_rows_registered_horizons": scalar(
                    con,
                    f"SELECT count(*) FROM markout_events WHERE date=DATE {quote(EVAL_DATE)} "
                    f"AND horizon_us IN ({horizons})",
                ),
                "strict_through_eligible_rows": scalar(
                    con,
                    f"SELECT count(*) FROM markout_events WHERE date=DATE {quote(EVAL_DATE)} "
                    f"AND horizon_us IN ({horizons}) AND strict_through_eligible_trade",
                ),
                "causal_prematch_strict_through_rows": scalar(
                    con, "SELECT count(*) FROM ch_spread_scored_base"
                ),
                "capture_gap_policy": (
                    "Upstream markout_events already excludes every decision-to-outcome "
                    "window overlapping capture_gaps."
                ),
            },
        },
        "negative_controls": {
            "sign_flip": "executed; algebraic sign must reverse",
            "market_label_shuffle": (
                "executed; deterministic prevalence-preserving hashed reassignment across "
                "market/trade rows within date/sport/horizon"
            ),
            "delayed_execution": (
                "executed at 1s delay using the registered 1s and 2s books; midpoint "
                "maker-direction markout only, not a delayed fill"
            ),
            "lookahead_sentinel_rows": scalar(
                con,
                "SELECT count(*) FROM markout_events "
                "WHERE book_t_us>=t_us OR post_book_t_us>target_us",
            ),
        },
        "bootstrap_boundary": (
            "One evaluation day makes every day-block bootstrap degenerate and diagnostic only."
        ),
    }
    return all_rows, result


def build_tts(con) -> tuple[list[dict], dict]:
    horizons = ",".join(str(value) for value in TTS_HORIZONS_US)
    day_us = 86_400_000_000
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_tts_anchors AS
      WITH transitions AS (
        SELECT *,CASE WHEN regime='ACTIVE_TIGHT' THEN 'treatment' ELSE 'control' END AS cohort,
          epoch_us(cast(date AS TIMESTAMP)) AS day_start_us,
          3600000000+abs(hash(root_event_id,{BOOTSTRAP_SEED}))%43200000000 AS shuffle_offset_us
        FROM ch_regime_transitions WHERE date=DATE {quote(EVAL_DATE)}
      )
      SELECT date,market_ticker,root_event_id,sport,league,occurrence_us,dim_effective_us,
             'main_transition' AS signal_name,cohort,regime AS destination_regime,
             t_us AS original_transition_us,t_us AS decision_us FROM transitions
      UNION ALL
      SELECT date,market_ticker,root_event_id,sport,league,occurrence_us,dim_effective_us,
             'negative_future_regime' AS signal_name,cohort,regime,
             t_us,prior_t_us AS decision_us FROM transitions
      UNION ALL
      SELECT date,market_ticker,root_event_id,sport,league,occurrence_us,dim_effective_us,
             'negative_time_of_day_shuffle' AS signal_name,cohort,regime,t_us,
             day_start_us+(((t_us-day_start_us+shuffle_offset_us)%{day_us})+{day_us})%{day_us}
               AS decision_us
      FROM transitions
      UNION ALL
      SELECT date,market_ticker,root_event_id,sport,league,occurrence_us,dim_effective_us,
             'negative_delayed_execution' AS signal_name,cohort,regime,t_us,
             t_us+{DELAYED_EXECUTION_US} AS decision_us FROM transitions
    """)
    entry_mid = mid_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    entry_spread = spread_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_tts_entry AS
      SELECT a.*,b.t_us AS entry_book_t_us,b.yes_bid_e4,b.yes_ask_e4,
             ({entry_mid}) AS entry_midpoint_logodds,
             ({entry_spread}) AS entry_spread_logodds
      FROM (SELECT * FROM ch_tts_anchors ORDER BY date,market_ticker,decision_us) a
      JOIN l1_day_bounds d USING(date)
      ASOF LEFT JOIN (
        SELECT * FROM l1_real ORDER BY date,market_ticker,t_us
      ) b
        ON a.date=b.date AND a.market_ticker=b.market_ticker AND a.decision_us>=b.t_us
      WHERE a.decision_us BETWEEN d.first_t_us AND d.last_t_us
        AND a.decision_us>=a.dim_effective_us
        AND b.t_us IS NOT NULL AND a.decision_us-b.t_us<={BOOK_AGE_CAP_US}
        AND b.yes_bid_e4>0 AND b.yes_ask_e4<10000 AND b.yes_bid_e4<b.yes_ask_e4
        AND NOT EXISTS (
          SELECT 1 FROM capture_gaps g
          WHERE g.start_us<=a.decision_us AND g.end_us>b.t_us
        )
    """)
    horizon_values = " UNION ALL ".join(
        f"SELECT {value}::BIGINT AS horizon_us" for value in TTS_HORIZONS_US
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE ch_tts_horizons AS {horizon_values}")
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_tts_targets AS
      SELECT e.*,h.horizon_us,e.decision_us+h.horizon_us AS target_us
      FROM ch_tts_entry e CROSS JOIN ch_tts_horizons h
    """)
    outcome_mid = mid_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ch_tts_scored AS
      SELECT t.*,b.t_us AS outcome_book_t_us,({outcome_mid}) AS outcome_midpoint_logodds,
             abs(({outcome_mid})-t.entry_midpoint_logodds) AS adverse_move_abs_logodds,
             0.5*t.entry_spread_logodds
               -abs(({outcome_mid})-t.entry_midpoint_logodds)
               AS gross_symmetric_quote_proxy_logodds
      FROM (SELECT * FROM ch_tts_targets ORDER BY date,market_ticker,target_us) t
      JOIN l1_day_bounds d USING(date)
      ASOF LEFT JOIN (
        SELECT * FROM l1_real ORDER BY date,market_ticker,t_us
      ) b
        ON t.date=b.date AND t.market_ticker=b.market_ticker AND t.target_us>=b.t_us
      WHERE t.target_us<=d.last_t_us
        AND b.t_us IS NOT NULL AND t.target_us-b.t_us<={BOOK_AGE_CAP_US}
        AND b.yes_bid_e4>0 AND b.yes_ask_e4<10000 AND b.yes_bid_e4<b.yes_ask_e4
        AND NOT EXISTS (
          SELECT 1 FROM capture_gaps g
          WHERE g.start_us<t.target_us AND g.end_us>t.entry_book_t_us
        )
    """)
    con.execute("""
      CREATE OR REPLACE TEMP TABLE ch_tts_event_effects AS
      SELECT signal_name,date,horizon_us,root_event_id,min(sport) AS sport,
             count(*) FILTER (WHERE cohort='treatment') AS treatment_rows,
             count(*) FILTER (WHERE cohort='control') AS control_rows,
             count(DISTINCT market_ticker) FILTER (WHERE cohort='treatment')
               AS treatment_markets,
             count(DISTINCT market_ticker) FILTER (WHERE cohort='control')
               AS control_markets,
             avg(gross_symmetric_quote_proxy_logodds) FILTER (WHERE cohort='treatment')
               AS treatment_mean_logodds,
             avg(gross_symmetric_quote_proxy_logodds) FILTER (WHERE cohort='control')
               AS control_mean_logodds,
             treatment_mean_logodds-control_mean_logodds AS effect_logodds,
             avg(adverse_move_abs_logodds) FILTER (WHERE cohort='treatment')
               AS treatment_adverse_move_abs_logodds,
             avg(adverse_move_abs_logodds) FILTER (WHERE cohort='control')
               AS control_adverse_move_abs_logodds
      FROM ch_tts_scored GROUP BY signal_name,date,horizon_us,root_event_id
      HAVING treatment_rows>0 AND control_rows>0
    """)
    event_rows = rows_as_dicts(
        con,
        "SELECT * FROM ch_tts_event_effects "
        "ORDER BY signal_name,horizon_us,date,root_event_id",
    )
    main_rows = [row for row in event_rows if row["signal_name"] == "main_transition"]
    roots = len({str(row["root_event_id"]) for row in main_rows})
    days = len({str(row["date"]) for row in main_rows})
    status, reason = exploratory_status(
        n_roots=roots,
        n_days=days,
        integrity_ok=(
            scalar(
                con,
                "SELECT count(*) FROM ch_tts_scored "
                "WHERE entry_book_t_us>decision_us OR outcome_book_t_us>target_us",
            )
            == 0
        ),
    )
    all_eval_roots = scalar(
        con,
        f"SELECT count(DISTINCT root_event_id) FROM ch_l1_regimes "
        f"WHERE date=DATE {quote(EVAL_DATE)}",
    )
    result = {
        "hypothesis_id": "C1-PREMATCH-TTS-01",
        "hypothesis_status": status,
        "status_reason": reason,
        "treatment": "transition into active-tight on the evaluation day",
        "control": "transitions into dormant, active-wide, or in-play within root events",
        "estimand": (
            "per-root treatment-minus-control [half current log-odds spread minus absolute "
            "future log-odds midpoint move]"
        ),
        "proxy_boundary": (
            "Gross symmetric quote/adverse-markout proxy only; it has no fill, fee, latency, "
            "inventory or forced-exit meaning and is not economics or PnL."
        ),
        "registered_horizons_us": list(TTS_HORIZONS_US),
        "all_eligible_evaluation_roots": all_eval_roots,
        "paired_root_events": roots,
        "roots_without_a_paired_transition_proxy": max(0, int(all_eval_roots) - roots),
        "evaluation_day_blocks": days,
        "stage_counts": {
            "anchors": rows_as_dicts(
                con,
                "SELECT signal_name,cohort,count(*) AS rows,"
                "count(DISTINCT root_event_id) AS roots "
                "FROM ch_tts_anchors GROUP BY signal_name,cohort "
                "ORDER BY signal_name,cohort",
            ),
            "causal_fresh_entries": rows_as_dicts(
                con,
                "SELECT signal_name,cohort,count(*) AS rows,"
                "count(DISTINCT root_event_id) AS roots "
                "FROM ch_tts_entry GROUP BY signal_name,cohort "
                "ORDER BY signal_name,cohort",
            ),
            "gap_safe_fresh_outcomes": rows_as_dicts(
                con,
                "SELECT signal_name,cohort,horizon_us,count(*) AS rows,"
                "count(DISTINCT root_event_id) AS roots "
                "FROM ch_tts_scored GROUP BY signal_name,cohort,horizon_us "
                "ORDER BY signal_name,cohort,horizon_us",
            ),
        },
        "inference": analyze_effect_rows(
            event_rows, replicates=DEFAULT_BOOTSTRAP_REPLICATES
        ),
        "negative_controls": {
            "future_regime": "executed at the strictly prior row using the next regime label",
            "time_of_day_shuffle": (
                "executed with deterministic 1h-13h within-day offset, same sport/root/market"
            ),
            "delayed_execution": "executed with decision clock shifted by +1 second",
            "lookahead_sentinel_rows": scalar(
                con,
                "SELECT count(*) FROM ch_tts_scored "
                "WHERE entry_book_t_us>decision_us OR outcome_book_t_us>target_us",
            ),
        },
        "power_boundary": (
            "Current evaluation has one day. Bootstrap output is a degenerate execution "
            "receipt, not a confidence interval or rejection basis."
        ),
    }
    return event_rows, result


def analyze_effect_rows(rows: Sequence[Mapping[str, object]], *, replicates: int) -> dict:
    grouped: dict[tuple[str, int], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if row.get("effect_logodds") is None:
            continue
        grouped[(str(row["signal_name"]), int(row["horizon_us"]))].append(row)
    output = {}
    for (signal, horizon), values in sorted(grouped.items()):
        output.setdefault(signal, {})[str(horizon)] = event_inference(
            values,
            replicates=replicates,
            seed=BOOTSTRAP_SEED + int(horizon // 1000),
        )
    return output


def write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({str(key) for row in rows for key in row})
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})
    os.replace(temporary, path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def append_trial_registry(
    run_dir: Path,
    manifest: Mapping[str, object],
    results: Mapping[str, Mapping[str, object]],
    *,
    source_sha: str,
) -> None:
    registry = run_dir / "TRIAL_REGISTRY.jsonl"
    if not registry.is_file():
        raise CoreHypothesisError("append-only trial registry missing")
    existing: set[tuple[str, str]] = set()
    for line in registry.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        existing.add((str(row.get("stage")), str(row.get("trial_id"))))
    releases = [item["release_id"] for item in manifest.get("selected_releases", [])]
    records = []
    for trial_id, result in results.items():
        if (STAGE, trial_id) in existing:
            continue
        records.append(
            {
                "recorded_at_utc": utc_now(),
                "record_type": "RESULT_STAGE_APPEND",
                "stage": STAGE,
                "trial_id": trial_id,
                "result_opened": True,
                "hypothesis_status": result["hypothesis_status"],
                "artifact_status": "DIAGNOSTIC_ONLY",
                "split": "EXPLORATORY_ONLY",
                "data_evidence": EXPECTED_EVIDENCE,
                "source_sha256": source_sha,
                "query_sha256": source_sha,
                "release_ids": releases,
                "result_artifact": "REPORT/tables/CORE_HYPOTHESIS_TESTS.json",
                "claim_boundary": "NOT A LIVE-TRADING AUTHORIZATION",
            }
        )
    if records:
        with registry.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


def markdown_report(result: Mapping[str, object]) -> str:
    hypotheses = result["hypotheses"]
    lines = [
        "# Cycle-1 core frozen hypothesis tests",
        "",
        f"> **{BANNER}**",
        "",
        "All price effects below use log-odds and complete root-event aggregation. ",
        "The 2026-07-13 evaluation is one calendar-day block, so its bootstrap is",
        "necessarily degenerate and cannot establish power, rejection, profit, or promotion.",
        "",
        "## Status",
        "",
    ]
    for trial_id in (
        "C1-SPREAD-CAPTURE-01",
        "C1-LARGE-FLOW-CONTINUATION-01",
        "C1-PREMATCH-TTS-01",
    ):
        item = hypotheses[trial_id]
        lines.append(f"- `{trial_id}`: **{item['hypothesis_status']}** — {item['status_reason']}")
    lines.extend(
        [
            "",
            "## Economic boundary",
            "",
            "The spread card's frozen NetPnL estimand was not computed: exact fees,",
            "order latency, full quote/cancel lifecycle, forced exit, and zero-fill/zero-quote",
            "event accounting are unavailable. Its completed strict-through result is a gross",
            "markout opportunity subtest only. Large-flow and TTS results are predictive/gross",
            "proxies and are not net returns or PnL.",
            "",
            "Detailed counts, exclusions, negative controls, event effects, and bootstrap",
            "receipts are in `REPORT/tables/CORE_HYPOTHESIS_TESTS.json` and adjacent CSVs.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args) -> None:
    run_dir = Path(args.run_dir).resolve()
    manifest, attestation = validate_run_identity(run_dir)
    import duckdb

    if duckdb.__version__ != EXPECTED_DUCKDB:
        raise CoreHypothesisError(f"DuckDB drift: {duckdb.__version__} != {EXPECTED_DUCKDB}")
    db_path = run_dir / "cache/cycle1.duckdb"
    if not db_path.is_file():
        raise CoreHypothesisError("cycle1.duckdb missing")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        configure(con, run_dir, args.memory_limit, args.threads, args.max_temp_directory_size)
        validate_database(con)
        regimes = build_regimes(con)
        trades = build_trade_covariates(con)
        spread_rows, spread = build_spread_subtest(con)
        flow_rows, flow = build_large_flow(con)
        tts_rows, tts = build_tts(con)
    finally:
        con.close()
    table_dir = run_dir / "REPORT/tables"
    write_csv(table_dir / "core_spread_event_effects.csv", spread_rows)
    write_csv(table_dir / "core_large_flow_event_effects.csv", flow_rows)
    write_csv(table_dir / "core_tts_event_effects.csv", tts_rows)
    source_sha = sha256(Path(__file__).resolve())
    result = {
        "schema_version": "sports-autoresearch-core-hypothesis-tests-v1",
        "run_id": manifest["run_id"],
        "completed_at_utc": utc_now(),
        "banner": BANNER,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "data_evidence": EXPECTED_EVIDENCE,
        "source_sha256": source_sha,
        "w09": {
            "instance_id": attestation["instance_id"],
            "region": attestation["region"],
            "role": attestation["role"],
            "read_only_cycle1_database": True,
        },
        "split": {
            "threshold_train_date": TRAIN_DATE,
            "evaluation_date": EVAL_DATE,
            "classification": "PRIOR_EXPOSED_EXPLORATORY_ONLY",
            "evaluation_day_blocks": 1,
            "formal_holdout_opened": False,
        },
        "regime_thresholds": regimes,
        "trade_context": trades,
        "inference_contract": {
            "unit": "complete root event",
            "block": "UTC calendar date",
            "bootstrap_replicates": DEFAULT_BOOTSTRAP_REPLICATES,
            "minimum_bootstrap_replicates": MIN_BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "one_day_bootstrap": "DEGENERATE_DIAGNOSTIC_ONLY",
            "minimum_relevant_roots": MIN_RELEVANT_ROOT_EVENTS,
            "minimum_independent_days": MIN_INDEPENDENT_DAY_BLOCKS,
            "no_underpowered_rejection": True,
        },
        "hypotheses": {
            "C1-SPREAD-CAPTURE-01": spread,
            "C1-LARGE-FLOW-CONTINUATION-01": flow,
            "C1-PREMATCH-TTS-01": tts,
        },
        "claim_boundary": (
            "No promotion, verdict, validation, confirmation, live-readiness, net-return or "
            "live-trading authorization is created by this artifact."
        ),
    }
    output = table_dir / "CORE_HYPOTHESIS_TESTS.json"
    write_json(output, result)
    report_path = run_dir / "REPORT/CORE_HYPOTHESIS_TESTS.md"
    write_text_atomic(report_path, markdown_report(result))
    append_trial_registry(
        run_dir,
        manifest,
        result["hypotheses"],
        source_sha=source_sha,
    )
    print(
        "CORE_HYPOTHESIS_TESTS_COMPLETE "
        f"run_id={manifest['run_id']} artifact={output}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--max-temp-directory-size", default="120GB")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("threads must be positive")
    try:
        run(args)
    except Exception as exc:
        print(
            f"CORE_HYPOTHESIS_TESTS_BLOCKED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
