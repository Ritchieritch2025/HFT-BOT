import importlib.util
import hashlib
import json
import datetime as dt
from pathlib import Path

import duckdb


path = Path(__file__).with_name("rfq_full_stage.py")
spec = importlib.util.spec_from_file_location("rfq_full_stage", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def recorder(wall_ns, frame=None, *, mono_ns=None, epoch=1, marker=None):
    row = {
        "recv_wall_ns": wall_ns,
        "recv_mono_ns": mono_ns if mono_ns is not None else wall_ns,
        "stream_epoch": epoch,
    }
    if frame is not None:
        row["raw"] = json.dumps(frame, separators=(",", ":"))
    if marker is not None:
        row["marker"] = marker
    return row


def frame(kind, rid, market, timestamp, **extra):
    field = "created_ts" if kind == "rfq_created" else "deleted_ts"
    msg = {"id": rid, "creator_id": extra.pop("creator_id", ""),
           "market_ticker": market, field: timestamp}
    msg.update(extra)
    return {"type": kind, "sid": 7, "msg": msg}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_fixed_exact_rejects_rounding_and_overflow():
    assert mod.fixed_exact("1.25", 2) == 125
    assert mod.fixed_exact(10, 6) == 10_000_000
    assert mod.fixed_exact("1.234", 2) is None
    assert mod.fixed_exact(float("inf"), 2) is None
    assert mod.fixed_exact(True, 2) is None
    assert mod.fixed_exact("100000000000000000000", 2) is None


def test_requester_hash_is_scoped_and_empty_stays_missing():
    assert mod.requester_hash("abc", "r1") == mod.requester_hash("abc", "r1")
    assert mod.requester_hash("abc", "r1") != mod.requester_hash("abc", "r2")
    assert mod.requester_hash("", "r1") is None
    assert "abc" not in mod.requester_hash("abc", "r1")


def test_kaplan_meier_grouped_censoring():
    rows = mod.kaplan_meier_from_counts([(1, 1, 0), (2, 1, 1), (3, 1, 0)])
    assert rows[0] == (1, 4, 1, 0, 0.75)
    assert rows[1][1:4] == (3, 1, 1)
    assert rows[-1][-1] == 0.0


def test_windows_are_contiguous_and_causal_boundaries():
    mod.validate_windows()
    assert mod.WINDOWS_US[0][1] == -30_000_000
    assert mod.WINDOWS_US[-1][2] == 120_000_000
    for left, right in zip(mod.WINDOWS_US, mod.WINDOWS_US[1:]):
        assert left[2] == right[1]


def test_schema_first_dedupe_lifecycle_legs_and_receive_clock(tmp_path):
    release0 = mod.RELEASE_IDS[0]
    base = tmp_path / "releases" / release0 / "raw_rfq" / "date=2026-07-12"
    rows = [
        # Delete before any create remains unmatched.
        recorder(500_000_000, frame("rfq_deleted", "B", "M-B", "2026-07-12T00:00:00Z")),
        recorder(1_000_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:10Z",
            contracts_fp="1.25", target_cost_dollars="10.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E", "market_ticker": "M-A", "side": "YES",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        # Same exchange identity is a recorder duplicate even with later receive.
        recorder(1_100_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:10Z",
            contracts_fp="1.25", target_cost_dollars="10.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E", "market_ticker": "M-A", "side": "YES",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        # Inconsistent market is not a valid lifecycle endpoint.
        recorder(1_500_000_000, frame(
            "rfq_deleted", "A", "M-X", "2026-07-12T00:00:11Z", creator_id="secret-id"
        )),
        # Exchange timestamp goes backwards, but TL1 receive ordering is authoritative.
        recorder(2_000_000_000, frame(
            "rfq_deleted", "A", "M-A", "2026-07-12T00:00:09Z", creator_id="secret-id"
        )),
        # Re-used ID starts a second cycle and is censored.
        recorder(3_000_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:12Z", contracts_fp="2.00"
        )),
        recorder(4_000_000_000, frame(
            "rfq_created", "C", "M-C", "2026-07-12T00:00:13Z", contracts_fp="1.234"
        )),  # invalid fixed point; schema-audited but excluded
        recorder(5_000_000_000, {"type": "subscribed", "msg": {"channel": "communications", "sid": 7}}),
    ]
    source = base / "rfq_00.ndjson"
    write_rows(source, rows)

    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "test-run")
    mod.build_request_tables(connection, "test-run")

    counts = connection.execute(
        "SELECT recorder_rows,deduplicated_valid_frames FROM rfq_scan_counts"
    ).fetchone()
    assert counts == (len(rows), 5)
    requests = connection.execute(
        "SELECT cycle_no,delete_recv_ns,requester_hash,inconsistent_delete_rows "
        "FROM rfq_requests_base ORDER BY cycle_no"
    ).fetchall()
    assert len(requests) == 2
    assert requests[0][0] == 1 and requests[0][1] == 2_000_000_000
    assert requests[0][2] == mod.requester_hash("secret-id", "test-run")
    assert requests[0][3] == 1
    assert requests[1][0] == 2 and requests[1][1] is None
    lifecycle = connection.execute(
        "SELECT cycle_no,duration_us,delete_observed,exchange_order_anomaly "
        "FROM rfq_lifecycle_base ORDER BY cycle_no"
    ).fetchall()
    assert lifecycle[0] == (1, 1_000_000, True, True)
    assert lifecycle[1][2] is False
    legs = connection.execute(
        "SELECT leg_index,market_ticker,side,yes_settlement_value_e6,leg_schema_valid "
        "FROM rfq_legs_base"
    ).fetchall()
    assert legs == [(0, "M-A", "yes", 1_000_000, True)]
    # Raw requester identifiers must not be present in persistent output schemas/values.
    columns = [row[1] for row in connection.execute("PRAGMA table_info('rfq_requests_base')").fetchall()]
    assert "creator_id" not in columns and "delete_creator_id" not in columns
    assert not any("secret-id" in str(value) for row in requests for value in row)
    audit_fields = {
        row[0] for row in connection.execute(
            "SELECT field_name FROM rfq_schema_field_audit WHERE event_type='rfq_created'"
        ).fetchall()
    }
    assert {"id", "market_ticker", "contracts_fp", "mve_selected_legs"} <= audit_fields
    connection.close()


def test_nullable_requester_and_observation_boundary_censoring(tmp_path):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    delete_without_requester = frame(
        "rfq_deleted", "X", "M-X", "2026-07-12T00:00:03Z"
    )
    delete_without_requester["msg"].pop("creator_id")
    write_rows(source, [
        recorder(1_000_000_000, frame(
            "rfq_created", "X", "M-X", "2026-07-12T00:00:01Z", creator_id=None
        )),
        recorder(2_000_000_000, marker="transport_close"),
        recorder(3_000_000_000, delete_without_requester),
        # A present non-string requester is a schema error; missing/JSON null is not.
        recorder(4_000_000_000, frame(
            "rfq_created", "Y", "M-Y", "2026-07-12T00:00:04Z", creator_id=123
        )),
        recorder(5_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "boundary-run")
    mod.build_request_tables(connection, "boundary-run")

    assert connection.execute(
        "SELECT valid_contract_frames,invalid_contract_frames FROM rfq_scan_counts"
    ).fetchone() == (2, 1)
    request = connection.execute("""
      SELECT matched_delete_recv_ns,delete_recv_ns,requester_known,
        delete_crosses_observation_boundary
      FROM rfq_requests_base
    """).fetchone()
    assert request == (3_000_000_000, None, False, True)
    lifecycle = connection.execute("""
      SELECT endpoint_receive_us,duration_us,delete_observed,endpoint_type,
        observation_boundary_reason
      FROM rfq_lifecycle_base
    """).fetchone()
    assert lifecycle == (
        2_000_000, 1_000_000, False,
        "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY", "MARKER_TRANSPORT_CLOSE",
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1
    connection.close()


def test_mutation_guard_next_create_censors_prior_and_delete_stays_new_cycle(tmp_path):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    write_rows(source, [
        recorder(1_000_000_000, frame(
            "rfq_created", "REUSED", "M-A", "2026-07-12T00:00:01Z"
        )),
        recorder(3_000_000_000, frame(
            "rfq_created", "REUSED", "M-A", "2026-07-12T00:00:03Z"
        )),
        # This delete belongs only to cycle 2 and must never backfill cycle 1.
        recorder(4_000_000_000, frame(
            "rfq_deleted", "REUSED", "M-A", "2026-07-12T00:00:04Z"
        )),
        recorder(5_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "replacement-run")
    mod.build_request_tables(connection, "replacement-run")

    lifecycle = connection.execute("""
      SELECT cycle_no,endpoint_receive_us,duration_us,delete_observed,endpoint_type,
        next_create_receive_us
      FROM rfq_lifecycle_base ORDER BY cycle_no
    """).fetchall()
    assert lifecycle == [
        (1, 3_000_000, 2_000_000, False, "RIGHT_CENSORED_AT_NEXT_CREATE", 3_000_000),
        (2, 4_000_000, 1_000_000, True, "FIRST_VALID_DELETE", None),
    ]
    requests = connection.execute("""
      SELECT cycle_no,delete_recv_ns,censor_boundary_type
      FROM rfq_requests_base ORDER BY cycle_no
    """).fetchall()
    assert requests == [
        (1, None, "NEXT_CREATE_REPLACEMENT"),
        (2, 4_000_000_000, None),
    ]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes WHERE disposition='MATCHED_ENDPOINT'
    """).fetchone()[0] == 1
    connection.close()


def test_manifest_object_key_sha_overlap_is_deduplicated_and_conflict_fails(tmp_path):
    key = "raw_rfq/date=2026-07-13/rfq_00.ndjson"
    body = json.dumps(recorder(1_000_000_000, {"type": "subscribed"})) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()
    for release in mod.RELEASE_IDS:
        base = tmp_path / "releases" / release
        path = base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        (base / ".VERIFIED.json").write_text(json.dumps({
            "release_id": release,
            "version_binding_mode": "VERSION_BOUND",
            "evidence_tier": mod.EVIDENCE,
        }), encoding="utf-8")
        (base / "MANIFEST.json").write_text(json.dumps({
            "objects": [{"key": key, "sha256": digest, "size": len(body.encode())}]
        }), encoding="utf-8")
        # Glob-visible but not manifest-bound: discovery must ignore it.
        extra = base / "raw_rfq/date=2026-07-13/rfq_01.ndjson"
        extra.write_text(body, encoding="utf-8")
    found = mod.discover_inputs(tmp_path)
    assert found["logical_manifest_bindings"] == 2
    assert found["objects"] == 1
    assert found["deduplicated_overlapping_objects"] == 1
    assert found["overlap_keys"] == [key]

    second = tmp_path / "releases" / mod.RELEASE_IDS[1] / "MANIFEST.json"
    second.write_text(json.dumps({
        "objects": [{"key": key, "sha256": "f" * 64, "size": len(body.encode())}]
    }), encoding="utf-8")
    try:
        mod.discover_inputs(tmp_path)
    except mod.RFQStageError as exc:
        assert "conflicting bytes" in str(exc)
    else:
        raise AssertionError("same key with a different manifest SHA must fail")


def test_mutation_guard_control_dim_lookback_and_synthetic_clob_stage(tmp_path):
    day = dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc)
    anchor_us = int(day.timestamp() * 1_000_000)
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_12.ndjson"
    )
    write_rows(source, [
        recorder(anchor_us * 1000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T12:00:00Z",
            creator_id="requester", contracts_fp="10.00", target_cost_dollars="4.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E-A", "market_ticker": "M-A", "side": "yes",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        recorder((anchor_us + 2_000_000) * 1000, frame(
            "rfq_deleted", "A", "M-A", "2026-07-12T12:00:02Z", creator_id="requester"
        )),
        recorder((anchor_us + 130_000_000) * 1000,
                 {"type": "subscribed", "msg": {"channel": "communications", "sid": 7}}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "synthetic-run")
    mod.build_request_tables(connection, "synthetic-run")
    request_key = connection.execute(
        "SELECT request_key FROM rfq_requests_base"
    ).fetchone()[0]
    create_control_shift_us = connection.execute("""
      SELECT ? + abs(hash(?,?,'CREATE')) % ?
    """, [mod.CONTROL_MIN_SHIFT_US, request_key, "M-A",
           mod.CONTROL_SHIFT_SPAN_US]).fetchone()[0]
    # Control anchor is 60s after dim-effective, but its required 120s lookback
    # starts 60s before dim-effective. It must therefore remain ineligible.
    dim_effective_us = anchor_us - create_control_shift_us - 60_000_000

    core_path = tmp_path / "cycle1.duckdb"
    core = duckdb.connect(str(core_path))
    core.execute("""
      CREATE TABLE universe(date DATE,market_ticker VARCHAR,sport VARCHAR,league VARCHAR,
        root_event_id VARCHAR,root_map_status VARCHAR,occurrence_datetime TIMESTAMPTZ,
        dim_effective_us BIGINT)
    """)
    core.execute("""
      INSERT INTO universe VALUES (
        DATE '2026-07-12','M-A','Soccer','L','ROOT',
        'PROVISIONAL_HEURISTIC_MATCHUP_TIME',
        TIMESTAMPTZ '2026-07-12 13:00:00+00',?)
    """, [dim_effective_us])
    core.execute("""
      CREATE TABLE l1_real(market_ticker VARCHAR,t_us BIGINT,recv_mono_ns BIGINT,
        yes_bid_e4 INTEGER,yes_ask_e4 INTEGER,yes_bid_qty_e4 BIGINT,yes_ask_qty_e4 BIGINT)
    """)
    l1 = []
    for offset_s in range(-700, 132):
        t_us = anchor_us + offset_s * 1_000_000
        l1.append(("M-A", t_us, t_us * 1000, 3900, 4100, 100_000, 100_000))
    core.executemany("INSERT INTO l1_real VALUES (?,?,?,?,?,?,?)", l1)
    core.execute("CREATE TABLE trades_safe(market_ticker VARCHAR,t_us BIGINT,taker_sign INTEGER,count_e4 BIGINT)")
    core.execute("INSERT INTO trades_safe VALUES ('M-A',?,1,10000)", [anchor_us + 1_000_000])
    core.execute("CREATE TABLE l2_all(market_ticker VARCHAR,t_us BIGINT)")
    core.executemany("INSERT INTO l2_all VALUES ('M-A',?)", [
        [anchor_us - 2_000_000], [anchor_us + 1_000_000]
    ])
    core.execute("CREATE TABLE capture_gaps(start_us BIGINT,end_us BIGINT)")
    core.close()

    mod.attach_core_and_enrich(connection, core_path)
    mod.build_descriptive_tables(connection)
    result = mod.build_clob_context(connection, max_per_root=50)
    assert result["candidate_anchor_markets"] == 2
    assert result["candidate_endpoint_anchors"] == 4
    assert result["causal_eligible_endpoint_anchors"] == 4
    assert result["posthoc_endpoint_anchors_excluded"] == 0
    assert result["sampled_endpoint_anchors"] == 4
    assert result["control_anchors_excluded_before_dim_effective"] >= 2
    assert (result["control_anchors_excluded_before_dim_effective"]
            + result["control_anchors_dim_eligible"] == 4)
    assert result["clob_context_rows"] == 72
    assert connection.execute("""
      SELECT count(*) FROM rfq_clob_anchors
      WHERE endpoint_type='CREATE' AND control_anchor_us>=dim_effective_us
        AND control_earliest_required_us<dim_effective_us
        AND NOT control_dim_eligible
    """).fetchone()[0] == 2
    assert connection.execute("""
      SELECT count(*) FROM rfq_clob_context
      WHERE endpoint_type='CREATE' AND cohort='PRIOR_CONTROL' AND valid_receive_window
    """).fetchone()[0] == 0
    assert connection.execute("""
      SELECT count(*) FROM rfq_control_balance
      WHERE endpoint_type='CREATE' AND matched
    """).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM rfq_clob_context WHERE valid_receive_window"
    ).fetchone()[0] > 0
    # Strict ASOF: the trade at +1s is excluded from the boundary exactly at +1s.
    exact = connection.execute("""
      SELECT trades FROM rfq_clob_context
      WHERE endpoint_type='CREATE' AND cohort='RFQ' AND window_label='p100ms_1s'
    """).fetchone()[0]
    assert exact == 0
    assert connection.execute("SELECT count(*) FROM rfq_combo_clob_proxy").fetchone()[0] == 1
    run_dir = tmp_path / "synthetic-run"
    exports = mod.export_outputs(connection, run_dir)
    charts = mod.render_charts(connection, run_dir) if importlib.util.find_spec("matplotlib") else []
    summary = mod.build_summary(connection, {
        "objects": 1, "bytes": source.stat().st_size,
        "logical_manifest_bindings": 1, "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "path_size_fingerprint_sha256": "a" * 64,
    }, result, 1.0)
    assert len(exports["parquet_tables"]) == 4
    assert "REPORT/tables/rfq_observation_boundaries.csv" in exports["csv_tables"]
    assert len(charts) in (0, 9)
    assert summary["hard_truth"]["broadcast_contains_accepted_quote_or_fill"] is False
    connection.close()
    mod.write_catalog(run_dir)
    catalog = duckdb.connect(str(run_dir / "cache/rfq_full_catalog.duckdb"), read_only=True)
    assert catalog.execute("SELECT count(*) FROM rfq_requests").fetchone()[0] == 1
    catalog.close()
