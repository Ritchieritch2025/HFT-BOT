import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import pytest


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location(
    "deep_core_hypothesis_tests", HERE / "core_hypothesis_tests.py"
)
core = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(core)


def valid_manifest():
    return {
        "mode": "EXPLORATORY_AUTORESEARCH",
        "mission": {"sha256": core.EXPECTED_MISSION_SHA},
        "status": core.EXPECTED_MANIFEST_STATUS,
        "analysis_started": True,
        "gates": {
            "gate_a": {"status": "PASS_W05_EXPLORATORY_READY"},
            "gate_b": {"status": "PASS_W09_ATTESTED"},
            "gate_c": {"status": "PASS_MODE1_ONLY", "mode2_authorized": False},
        },
        "selected_releases": [
            {
                "release_id": release_id,
                "manifest_sha256": manifest_sha,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
            }
            for release_id, manifest_sha in core.EXPECTED_RELEASE_MANIFESTS.items()
        ],
    }


def valid_attestation():
    return {
        "instance_id": core.EXPECTED_INSTANCE,
        "region": core.EXPECTED_REGION,
        "role": core.EXPECTED_ROLE,
        "instance_profile": core.EXPECTED_ROLE,
        "architecture": "aarch64",
        "duckdb": core.EXPECTED_DUCKDB,
        "w09_run_inhibitor_present": True,
        "w09_run_inhibitor_is_ancestor": True,
        "static_credentials_present": False,
        "trading_credentials_present": False,
        "ambient_aws_or_kalshi_variables": [],
        "static_credential_paths_present": [],
        "installation_sha256": core.EXPECTED_W09_INSTALLATION_SHA256,
        "s3_access": "READ_ONLY_RESEARCH_PREFIX",
    }


def test_one_day_bootstrap_is_executed_but_explicitly_degenerate():
    rows = [
        {"date": "2026-07-13", "root_event_id": "r1", "effect_logodds": 0.1},
        {"date": "2026-07-13", "root_event_id": "r2", "effect_logodds": -0.05},
    ]
    result = core.event_inference(rows, replicates=1000, seed=7)
    assert result["bootstrap"]["replicates"] == 1000
    assert result["bootstrap"]["n_days"] == 1
    assert result["bootstrap"]["degenerate"] is True
    assert result["bootstrap"]["diagnostic_only"] is True
    assert result["bootstrap"]["ci95_lower"] == pytest.approx(0.025)
    assert result["bootstrap"]["ci95_upper"] == pytest.approx(0.025)


def test_underpowered_sample_is_collect_more_never_rejected():
    status, reason = core.exploratory_status(
        n_roots=1000, n_days=1, integrity_ok=True
    )
    assert status == "COLLECT_MORE"
    assert "No rejection" in reason
    status, _ = core.exploratory_status(n_roots=0, n_days=0, integrity_ok=True)
    assert status == "DATA_STARVED"


def test_w09_mode1_contract_is_fail_closed():
    core.validate_mode1_contract(
        valid_manifest(), valid_attestation(), system="Linux", machine="aarch64"
    )
    with pytest.raises(core.CoreHypothesisError, match="W09-only"):
        core.validate_mode1_contract(
            valid_manifest(), valid_attestation(), system="Darwin", machine="arm64"
        )
    bad = valid_manifest()
    bad["selected_releases"][0]["evidence_tier"] = "SEALED_CONFIRMATION"
    with pytest.raises(core.CoreHypothesisError, match="SEALED_DEGRADED"):
        core.validate_mode1_contract(
            bad, valid_attestation(), system="Linux", machine="aarch64"
        )


def synthetic_cycle1_connection():
    con = duckdb.connect()
    con.execute("CREATE TABLE capture_gaps(start_us BIGINT,end_us BIGINT)")
    con.execute(
        """
        CREATE TABLE universe(
          date DATE,market_ticker VARCHAR,family_event_id VARCHAR,
          root_event_id VARCHAR,sport VARCHAR,league VARCHAR,
          occurrence_datetime TIMESTAMPTZ,dim_effective_us BIGINT,
          root_map_status VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE l1_real(
          date DATE,t_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
          market_ticker VARCHAR,yes_bid_e4 INTEGER,yes_ask_e4 INTEGER
        )
        """
    )
    bases = {}
    for day in (core.TRAIN_DATE, core.EVAL_DATE):
        start = dt.datetime.fromisoformat(day + "T12:00:00+00:00")
        start_us = int(start.timestamp() * 1_000_000)
        bases[day] = start_us
        occurrence = start + dt.timedelta(minutes=20)
        con.execute(
            "INSERT INTO universe VALUES (?,?,?,?,?,?,?,?,?)",
            [
                day,
                "M1",
                "F1",
                "R1",
                "basketball",
                "L",
                occurrence,
                start_us - 1,
                "PROVISIONAL_HEURISTIC_MATCHUP_TIME",
            ],
        )
        rows = []
        for second in range(1300):
            tight = (
                second % 4 == 0
                if day == core.TRAIN_DATE
                else (second // 180) % 2 == 0
            )
            bid, ask = (4900, 5100) if tight else (4000, 6000)
            t_us = start_us + second * 1_000_000
            rows.append((day, t_us, t_us * 1000, second, "M1", bid, ask))
        con.executemany("INSERT INTO l1_real VALUES (?,?,?,?,?,?,?)", rows)
    con.execute(
        """
        CREATE TABLE l1_day_bounds AS
        SELECT date,min(t_us) AS first_t_us,max(t_us) AS last_t_us,
               count(*) AS row_count,count(DISTINCT market_ticker) AS n_markets
        FROM l1_real GROUP BY date
        """
    )
    con.execute("CREATE TABLE l1_intervals(dummy INTEGER)")
    con.execute(
        """
        CREATE TABLE flow_thresholds(
          sport VARCHAR,q99_count_e4 DOUBLE,p50_count_e4 DOUBLE,train_trades BIGINT
        )
        """
    )
    con.execute("INSERT INTO flow_thresholds VALUES ('basketball',100,10,1000)")
    con.execute(
        """
        CREATE TABLE trade_pre(
          date DATE,trade_id VARCHAR,market_ticker VARCHAR,root_event_id VARCHAR,
          sport VARCHAR,t_us BIGINT,book_t_us BIGINT,count_e4 BIGINT,
          occurrence_datetime TIMESTAMPTZ,taker_sign INTEGER
        )
        """
    )
    trades = [
        (core.TRAIN_DATE, 240, "TRAIN", 10),
        (core.EVAL_DATE, 240, "CONTROL_TIGHT", 10),
        (core.EVAL_DATE, 370, "CONTROL_WIDE", 10),
        (core.EVAL_DATE, 720, "TREATMENT", 200),
    ]
    for day, second, trade_id, count_e4 in trades:
        start_us = bases[day]
        occurrence = dt.datetime.fromtimestamp(
            start_us / 1_000_000, dt.timezone.utc
        ) + dt.timedelta(minutes=20)
        con.execute(
            "INSERT INTO trade_pre VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                day,
                trade_id,
                "M1",
                "R1",
                "basketball",
                start_us + second * 1_000_000,
                start_us + (second - 1) * 1_000_000,
                count_e4,
                occurrence,
                1,
            ],
        )
    con.execute(
        """
        CREATE TABLE markout_events(
          date DATE,trade_id VARCHAR,market_ticker VARCHAR,root_event_id VARCHAR,
          sport VARCHAR,t_us BIGINT,target_us BIGINT,horizon_us BIGINT,
          signed_move_logodds DOUBLE,strict_through_eligible_trade BOOLEAN,
          taker_sign INTEGER,yes_bid_e4 INTEGER,yes_ask_e4 INTEGER,
          post_mid_logodds DOUBLE,hypothetical_touch_gross_markout_e4 DOUBLE,
          book_t_us BIGINT,post_book_t_us BIGINT
        )
        """
    )
    horizons = sorted(
        set(core.FLOW_HORIZONS_US + core.SPREAD_HORIZONS_US + (2_000_000,))
    )
    for day, second, trade_id, _ in trades[1:]:
        t_us = bases[day] + second * 1_000_000
        for horizon in horizons:
            treatment = trade_id == "TREATMENT"
            con.execute(
                "INSERT INTO markout_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    day,
                    trade_id,
                    "M1",
                    "R1",
                    "basketball",
                    t_us,
                    t_us + horizon,
                    horizon,
                    0.01 if treatment else 0.0,
                    True,
                    1,
                    4900,
                    5100,
                    0.001 if treatment else 0.0,
                    5.0,
                    t_us - 1_000_000,
                    t_us + horizon,
                ],
            )
    return con


def test_synthetic_fixture_executes_three_frozen_subtests():
    con = synthetic_cycle1_connection()
    try:
        core.validate_database(con)
        regimes = core.build_regimes(con)
        trades = core.build_trade_covariates(con)
        spread_rows, spread = core.build_spread_subtest(con)
        flow_rows, flow = core.build_large_flow(con)
        tts_rows, tts = core.build_tts(con)
    finally:
        con.close()
    assert regimes["thresholds"][0]["train_observations"] >= 500
    assert regimes["definition"]["active"].startswith("sport q75 strictly-prior")
    assert trades["large_flow_treatments"] == 1
    assert spread["hypothesis_status"] == "DATA_STARVED"
    assert spread["full_economic_test"]["executed"] is False
    assert spread["gross_markout_subtest"]["executed"] is True
    assert flow["hypothesis_status"] == "COLLECT_MORE"
    assert tts["hypothesis_status"] == "COLLECT_MORE"
    assert spread_rows and flow_rows and tts_rows
    assert all("effect_logodds" in row for row in flow_rows)


def test_trial_registry_append_is_idempotent_and_preserves_existing_line(tmp_path):
    original = json.dumps(
        {"trial_id": "C1-SPREAD-CAPTURE-01", "status": "REGISTERED"},
        sort_keys=True,
    )
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    registry.write_text(original + "\n", encoding="utf-8")
    manifest = {
        "selected_releases": [
            {"release_id": "r1", "evidence_tier": "SEALED_DEGRADED_EVIDENCE"}
        ]
    }
    results = {
        trial: {"hypothesis_status": status}
        for trial, status in (
            ("C1-SPREAD-CAPTURE-01", "DATA_STARVED"),
            ("C1-LARGE-FLOW-CONTINUATION-01", "COLLECT_MORE"),
            ("C1-PREMATCH-TTS-01", "COLLECT_MORE"),
        )
    }
    core.append_trial_registry(tmp_path, manifest, results, source_sha="a" * 64)
    once = registry.read_text(encoding="utf-8").splitlines()
    core.append_trial_registry(tmp_path, manifest, results, source_sha="a" * 64)
    twice = registry.read_text(encoding="utf-8").splitlines()
    assert once == twice
    assert twice[0] == original
    assert len(twice) == 4
    appended = [json.loads(line) for line in twice[1:]]
    assert len({(row["stage"], row["trial_id"]) for row in appended}) == 3
    assert all(row["source_sha256"] == row["query_sha256"] for row in appended)
    assert all(row["release_ids"] == ["r1"] for row in appended)
