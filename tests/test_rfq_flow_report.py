"""Synthetic official-schema fixtures for the exploratory RFQ flow atlas."""

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "research" / "rfq_flow_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("rfq_flow_report", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


rfq = load_module()
UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 12, 0, 0, tzinfo=UTC)


def outer(when, frame):
    return json.dumps({
        "recv_mono_ns": int(when.timestamp() * 1e9) - 123,
        "recv_wall_ns": int(when.timestamp() * 1e9),
        "source": "Kalshi",
        "channel": frame.get("type", "unknown"),
        "raw": json.dumps(frame, separators=(",", ":")),
    }) + "\n"


def marker_outer(when, marker):
    return json.dumps({
        "recv_mono_ns": int(when.timestamp() * 1e9) - 123,
        "recv_wall_ns": int(when.timestamp() * 1e9),
        "source": "Kalshi", "channel": "", "source_ticker": "",
        "marker": marker, "raw": "",
    }) + "\n"


def receipt_outer(when, segment_hour, capture_path, status="PASS"):
    import hashlib
    capture = Path(capture_path)
    receipt = {
        "type": "rfq_segment_receipt", "schema": "rfq-segment-receipt-v2",
        "segment_hour": segment_hour, "status": status, "findings": [],
        "subscription_proven": True, "boundary_closed": True,
        "end_reason": "boundary", "close_stability_ms": 2_000,
        "capture_bytes_before": 0,
        "capture_relpath": "/".join(capture.parts[-2:]),
        "capture_origin_path": "/home/ubuntu/hft-bot/work/raw/" +
                               "/".join(capture.parts[-2:]),
        "capture_bytes_at_close": capture.stat().st_size,
        "capture_sha256_at_close": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "raw_evidence": {"subscribed_communications": 0, "findings": [],
                         "markers": {"hour_open": 1}},
        "metrics_evidence": {"connected_valid_rows": 3500, "max_reconnects": 0,
                             "findings": []},
    }
    return json.dumps({
        "recv_mono_ns": int(when.timestamp() * 1e9) - 123,
        "recv_wall_ns": int(when.timestamp() * 1e9), "source": "Kalshi",
        "channel": "rfq_segment_receipt", "source_ticker": "",
        "marker": "segment_receipt",
        "raw": json.dumps(receipt, separators=(",", ":")),
    }) + "\n"


def seal_index(paths):
    import hashlib
    return {str(Path(p).resolve()): {"size": Path(p).stat().st_size,
                                     "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()}
            for p in paths}


def test_full_v2_prior_day_seal_indexes_cross_day_final_receipt(tmp_path):
    raw = tmp_path / "raw"
    receipt = raw / "date=2026-07-14" / "rfq_receipts_00.ndjson"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("receipt\n")
    seals = tmp_path / "seals"
    seals.mkdir()
    (seals / "date=2026-07-12.json").write_text(json.dumps({
        "status": "SEALED", "method": "full_v2", "raw_files": []}))
    import hashlib
    (seals / "date=2026-07-13.json").write_text(json.dumps({
        "status": "SEALED", "method": "full_v2",
        "receipt_cross_day_hours": 2,
        "raw_files": [{"file": "date=2026-07-14/rfq_receipts_00.ndjson",
                       "size": receipt.stat().st_size,
                       "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}],
    }))
    index, errors = rfq.load_seal_index(
        str(seals), str(raw), {"2026-07-12", "2026-07-13"})
    assert errors == []
    assert str(receipt.resolve()) in index


def created(rfq_id, when, market="KXNBA-26JUL12-BOS", creator="", **extra):
    msg = {"id": rfq_id, "creator_id": creator, "market_ticker": market,
           "event_ticker": market.rsplit("-", 1)[0],
           "created_ts": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
    msg.update(extra)
    return {"type": "rfq_created", "sid": 15, "msg": msg}


def deleted(rfq_id, when, creator, market="KXNBA-26JUL12-BOS", **extra):
    msg = {"id": rfq_id, "creator_id": creator, "market_ticker": market,
           "event_ticker": market.rsplit("-", 1)[0],
           "deleted_ts": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
    msg.update(extra)
    return {"type": "rfq_deleted", "sid": 15, "msg": msg}


def fixture_48h(tmp_path):
    paths = []
    for h in range(48):
        boundary = T0 + dt.timedelta(hours=h)
        when = boundary + dt.timedelta(minutes=1)
        d = tmp_path / ("date=" + when.strftime("%Y-%m-%d"))
        d.mkdir(exist_ok=True)
        p = d / ("rfq_" + when.strftime("%H") + ".ndjson")
        content = marker_outer(boundary, "hour_open")
        # One persistent connection authenticates once. Hourly reconnects are
        # deliberately absent; subsequent proof lives in the segment receipt.
        if h == 0:
            content += outer(when, {"type": "subscribed",
                                    "msg": {"channel": "communications", "sid": 15}})
        p.write_text(content)
        paths.append(str(p))
    p0 = Path(paths[0])
    t1 = T0 + dt.timedelta(minutes=5)
    c1 = created("r1", t1, contracts_fp="10.25")
    p0.write_text(p0.read_text() + outer(t1, c1))
    t2 = T0 + dt.timedelta(hours=1, minutes=5)
    combo = created(
        "r2", t2, market="KXMVE-26JUL12-COMBO", target_cost_dollars="0.0001",
        mve_collection_ticker="KXMVE-26JUL12",
        mve_selected_legs=[{"event_ticker": "KXNBA-26JUL12",
                            "market_ticker": "KXNBA-26JUL12-BOS", "side": "yes",
                            "yes_settlement_value_dollars": "1.0000"}],
    )
    Path(paths[1]).write_text(Path(paths[1]).read_text() + outer(t2, combo))
    td1 = T0 + dt.timedelta(hours=2)
    Path(paths[2]).write_text(Path(paths[2]).read_text() +
                              outer(td1, deleted("r1", td1, "comm_A",
                                                contracts_fp="10.25")))
    td2 = T0 + dt.timedelta(hours=3)
    Path(paths[3]).write_text(Path(paths[3]).read_text() +
                              outer(td2, deleted("r2", td2, "comm_A",
                                                market="KXMVE-26JUL12-COMBO",
                                                target_cost_dollars="0.0001")))
    receipt_paths = []
    for h, path in enumerate(paths):
        when = T0 + dt.timedelta(hours=h + 1, seconds=2)
        d = tmp_path / ("date=" + when.strftime("%Y-%m-%d"))
        d.mkdir(exist_ok=True)
        receipt_path = d / ("rfq_receipts_" + when.strftime("%H") + ".ndjson")
        with receipt_path.open("a") as fh:
            fh.write(receipt_outer(
                when, (T0 + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H"), path))
        receipt_paths.append(str(receipt_path))
    return paths, sorted(set(receipt_paths))


def test_exact_schema_join_dedupe_combo_and_hvm_lower_bound(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    all_paths = paths + receipt_paths
    parsed = rfq.parse_paths(all_paths)
    report = rfq.build_report(parsed, {"KXNBA": "Sports", "KXMVE": "Sports"},
                              start=T0, hours=48, seal_index=seal_index(all_paths))
    assert report["window"]["complete"] is True
    assert report["window"]["observed_hours"] == 48
    assert report["window"]["connection_ack_hours"] == 1
    assert report["counts"]["unique_requests"] == 2
    assert report["data_quality"]["parse_and_schema_counts"] == {}
    assert report["contracts_fp"]["n"] == 1
    assert report["contracts_fp"]["p50_e2"] == 1_025
    assert report["contracts_fp"]["histogram"] == [
        {"lo_e2": 1_025, "hi_e2": 1_025, "n": 1}]
    assert report["target_cost_dollars"]["p50_e6"] == 100
    assert report["combo"]["combo_n"] == 1
    assert report["combo"]["combo_share_bps"] == 5_000
    assert report["hvm"]["exact_share"] is None
    assert report["hvm"]["known_hvm_lower_bound_share_bps"] == 5_000
    # Both empty create IDs are recovered from deletes and collapse to comm_A.
    assert report["requesters"]["known_id_coverage_bps"] == 10_000
    assert report["requesters"]["distinct_known_ids"] == 1
    assert report["requesters"]["top1_share_bps"] == 10_000
    assert report["requests_by_day_category"] == {"2026-07-12": {"Sports": 2}}


def test_empty_creator_is_never_grouped_as_a_requester(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    # Remove all delete rows: creates remain right-censored with blank IDs.
    for p in paths:
        lines = [line for line in Path(p).read_text().splitlines()
                 if 'rfq_deleted' not in line]
        Path(p).write_text("\n".join(lines) + "\n")
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert report["requesters"]["known_id_requests"] == 0
    assert report["requesters"]["distinct_known_ids"] == 0
    assert report["requesters"]["ranked"] == []


def test_incomplete_window_is_explicit_and_html_has_all_charts(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    # Drop the final hour and its receipt; shared receipt files are rebuilt by
    # parsing only receipts for earlier segment hours.
    parsed = rfq.parse_paths(paths[:47] + receipt_paths)
    parsed["segment_receipts"] = [r for r in parsed["segment_receipts"]
                                   if r.get("segment_hour") != "2026-07-13T23"]
    report = rfq.build_report(parsed, {}, start=T0, hours=48,
                              seal_index=seal_index(paths[:47] + receipt_paths))
    assert report["window"]["complete"] is False
    assert report["window"]["missing_hours"] == ["2026-07-13T23"]
    page = rfq.render_html(report)
    assert "EXPLORATORY — NOT A TRADING GATE" in page
    assert page.count("<svg") == 8
    assert "HVM share: verifiable lower bound only" in page


def test_official_e2_e6_precision_is_exact_and_finer_is_rejected():
    assert rfq.fixed_exact("10.25", "contracts_fp", 2) == 1_025
    assert rfq.fixed_exact("0.000001", "target_cost_dollars", 6) == 1
    try:
        rfq.fixed_exact("0.0000001", "target_cost_dollars", 6)
        assert False, "must reject precision finer than official E6"
    except ValueError:
        pass
    try:
        rfq.fixed_exact("1e-2", "target_cost_dollars", 6)
        assert False, "scientific notation is not an official fixed-point string"
    except ValueError:
        pass


def test_complete_window_requires_exact_hour_aligned_t0(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    with pytest.raises(rfq.ReportError, match="aligned"):
        rfq.build_report(rfq.parse_paths(paths + receipt_paths), {},
                         start=T0 + dt.timedelta(minutes=30), hours=48,
                         seal_index=seal_index(paths + receipt_paths))


def test_loss_marker_blocks_complete_verdict(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    p = Path(paths[10])
    when = T0 + dt.timedelta(hours=10, minutes=2)
    marker = json.dumps({"recv_mono_ns": 1,
                         "recv_wall_ns": int(when.timestamp() * 1e9),
                         "source": "Kalshi", "channel": "", "source_ticker": "",
                         "source_sequence": 3, "marker": "loss", "raw": ""}) + "\n"
    p.write_text(p.read_text() + marker)
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert report["window"]["loss_or_gap_markers"] == 1
    assert report["window"]["complete"] is False


def test_last_millisecond_transport_close_blocks_the_old_hour(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    p = Path(paths[-1])
    when = T0 + dt.timedelta(hours=47, minutes=59, seconds=59,
                             microseconds=900_000)
    p.write_text(p.read_text() + marker_outer(when, "transport_close"))
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert report["window"]["blocking_stream_markers"] == 1
    assert report["window"]["complete"] is False


def test_duplicate_and_malformed_are_scoped_dq_and_block_complete(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    p0 = Path(paths[0])
    t1 = T0 + dt.timedelta(minutes=5)
    p0.write_text(p0.read_text() + outer(t1, created("r1", t1, contracts_fp="10.25")))
    bad = {"type": "rfq_created", "sid": 15,
           "msg": {"id": "bad", "creator_id": "", "created_ts": "2026-07-12T04:00:00Z"}}
    p4 = Path(paths[4])
    p4.write_text(p4.read_text() + outer(T0 + dt.timedelta(hours=4), bad))
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    dq = report["data_quality"]["parse_and_schema_counts"]
    assert dq["duplicate_event"] == 1
    assert dq["missing_required_field"] == 1
    assert report["window"]["complete"] is False


def test_unsealed_file_or_nonpass_receipt_blocks_complete(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    all_paths = paths + receipt_paths
    seals = seal_index(all_paths)
    seals.pop(str(Path(paths[7]).resolve()))
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seals)
    assert report["window"]["seal_failures"]
    assert report["window"]["complete"] is False

    # Add a second, non-PASS receipt for one hour: any retry/disabled segment
    # makes event completeness unproven even if a PASS receipt also exists.
    p = Path(receipt_paths[0])
    when = T0 + dt.timedelta(hours=8, minutes=59, seconds=58)
    p.write_text(p.read_text() + receipt_outer(
        when, "2026-07-12T08", paths[8], status="DISABLED_PARTIAL"))
    report2 = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                               seal_index=seal_index(all_paths))
    assert "2026-07-12T08" in report2["window"]["unhealthy_receipt_hours"]
    assert report2["window"]["complete"] is False


def test_append_after_hourly_receipt_is_caught_even_when_day_seal_matches(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    # Simulate a delayed old-hour row arriving after the supervisor hashed its
    # close receipt. The final day seal sees the append; receipt-vs-seal must
    # still reject the hour.
    p = Path(paths[6])
    when = T0 + dt.timedelta(hours=6, minutes=59, seconds=59)
    p.write_text(p.read_text() + outer(when, {"type": "quote_created", "sid": 15,
                                              "msg": {"id": "late"}}))
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert any("post-receipt append" in x for x in report["window"]["seal_failures"])
    assert report["window"]["complete"] is False


def test_out_of_window_duplicate_and_dq_do_not_contaminate_window(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    old_dir = tmp_path / "date=2026-07-11"
    old_dir.mkdir(exist_ok=True)
    old = old_dir / "rfq_23.ndjson"
    before = T0 - dt.timedelta(minutes=5)
    duplicate_identity = created("r1", T0 + dt.timedelta(minutes=5),
                                 contracts_fp="10.25")
    old.write_text(outer(before, duplicate_identity) + "not-json\n")
    all_paths = [str(old)] + paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(paths + receipt_paths))
    assert report["counts"]["unique_requests"] == 2
    assert report["data_quality"]["parse_and_schema_counts"] == {}
    assert report["window"]["complete"] is True


def test_invalid_sid_and_create_delete_mismatch_are_dq(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    t = T0 + dt.timedelta(hours=5, minutes=5)
    bad_sid = created("r_bad_sid", t, contracts_fp="1.00")
    bad_sid.pop("sid", None)
    p5 = Path(paths[5])
    p5.write_text(p5.read_text() + outer(t, bad_sid))
    create3 = created("r3", t, contracts_fp="2.00")
    delete3 = deleted("r3", t + dt.timedelta(minutes=1), "comm_B",
                      market="KXNBA-OTHER-MKT", contracts_fp="2.00")
    p5.write_text(p5.read_text() + outer(t, create3) +
                  outer(t + dt.timedelta(minutes=1), delete3))
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    dq = report["data_quality"]["parse_and_schema_counts"]
    assert dq["invalid_or_missing_sid"] == 1
    assert dq["create_delete_join_mismatch"] == 1
    assert report["window"]["complete"] is False


def test_rfq_id_collision_and_unsubscribe_are_blocking_dq(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    when = T0 + dt.timedelta(hours=9, minutes=5)
    p = Path(paths[9])
    p.write_text(p.read_text() +
                 outer(when, created("r1", when, market="KXOTHER-ONE",
                                     contracts_fp="1.00")) +
                 outer(when + dt.timedelta(seconds=1),
                       {"type": "unsubscribed", "sid": 15, "msg": {}}))
    all_paths = paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    dq = report["data_quality"]["parse_and_schema_counts"]
    assert dq["rfq_id_collision"] == 1
    assert dq["unexpected_communications_unsubscribed"] == 1
    assert report["window"]["complete"] is False
