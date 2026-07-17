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


def receipt_outer_v3(when, segment_hour, capture_paths, status="PASS"):
    import hashlib
    ordered = sorted((Path(path) for path in capture_paths),
                     key=lambda path: (0 if path.suffix == ".ndjson" else
                                       int(path.suffix[1:])))
    shards = []
    for ordinal, capture in enumerate(ordered):
        shards.append({
            "ordinal": ordinal,
            "relpath": "/".join(capture.parts[-2:]),
            "bytes_before": 0,
            "size": capture.stat().st_size,
            "parsed_bytes_at_close": capture.stat().st_size,
            "sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        })
    segment_start = dt.datetime.strptime(segment_hour, "%Y-%m-%dT%H").replace(
        tzinfo=UTC)
    start_ns = int(segment_start.timestamp() * 1_000_000_000)
    end_ns = int((segment_start + dt.timedelta(hours=1)).timestamp() *
                 1_000_000_000)
    receipt = {
        "type": "rfq_segment_receipt", "schema": "rfq-segment-receipt-v3",
        "segment_hour": segment_hour, "status": status, "findings": [],
        "subscription_proven": True, "boundary_closed": True,
        "end_reason": "boundary", "close_stability_ms": 2_000,
        "expected_start_wall_ns": start_ns,
        "expected_end_wall_ns": end_ns,
        "child_rc": None,
        # Legacy aliases are informational for v3. capture_shards is the
        # authoritative complete object set.
        "capture_bytes_before": 0,
        "capture_relpath": shards[0]["relpath"],
        "capture_origin_path": "/home/ubuntu/hft-bot/work/raw/" +
                               shards[0]["relpath"],
        "capture_bytes_at_close": sum(row["size"] for row in shards),
        "capture_sha256_at_close": (shards[0]["sha256"]
                                     if len(shards) == 1 else None),
        "capture_shards": shards,
        "capture_shard_count": len(shards),
        "capture_shard_set_sha256": rfq.capture_shard_set_sha256(shards),
        "raw_evidence": {
            "recorder_rows": 1, "subscribed_communications": 0,
            "markers": {"hour_open": 1}, "partition_mismatches": 0,
            "subscription_invalidations": 0, "findings": [],
        },
        "metrics_evidence": {
            "feed_rows": 3600, "connected_valid_rows": 3500,
            "min_reconnects": 0, "max_reconnects": 0,
            "min_disconnects": 0, "max_disconnects": 0,
            "min_errors": 0, "max_errors": 0,
            "max_recorder_dropped": 0, "max_recorder_write_failures": 0,
            "first_ts_ms": start_ns // 1_000_000 + 1_000,
            "last_ts_ms": end_ns // 1_000_000 - 1_000,
            "findings": [],
        },
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


def replace_hour_receipt(receipt_paths, segment_hour, replacement):
    removed = 0
    for path in receipt_paths:
        kept = []
        for line in Path(path).read_text().splitlines(True):
            outer_row = json.loads(line)
            if outer_row.get("marker") == "segment_receipt":
                receipt = json.loads(outer_row["raw"])
                if receipt.get("segment_hour") == segment_hour:
                    removed += 1
                    continue
            kept.append(line)
        Path(path).write_text("".join(kept))
    assert removed == 1
    with Path(receipt_paths[0]).open("a") as fh:
        fh.write(replacement)


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


def test_v3_multishard_receipt_binds_every_shard_and_day_seal(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    base = Path(paths[0])
    shard1 = Path(str(base) + ".1")
    shard_when = T0 + dt.timedelta(minutes=10)
    shard1.write_text(outer(
        shard_when, {"type": "quote_created", "sid": 15, "msg": {"id": "q"}}))
    replace_hour_receipt(
        receipt_paths, "2026-07-12T00",
        receipt_outer_v3(T0 + dt.timedelta(hours=1, seconds=2),
                         "2026-07-12T00", [base, shard1]))
    all_paths = paths + [str(shard1)] + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert report["window"]["complete"] is True
    receipt = next(row for row in report["segment_receipts"]
                   if row["segment_hour"] == "2026-07-12T00")
    assert receipt["schema"] == "rfq-segment-receipt-v3"
    assert [row["ordinal"] for row in receipt["capture_shards"]] == [0, 1]


def test_malformed_v3_shard_member_fails_closed_without_crashing():
    receipt = {
        "schema": "rfq-segment-receipt-v3",
        "status": "PASS",
        "findings": [],
        "subscription_proven": True,
        "boundary_closed": True,
        "end_reason": "boundary",
        "close_stability_ms": 1_000,
        "segment_hour": "2026-07-12T00",
        "capture_shards": [None],
        "capture_shard_count": 1,
        "capture_shard_set_sha256": "0" * 64,
    }
    errors = rfq.receipt_contract_errors(receipt)
    assert "v3 shard member is not an object" in errors


def test_flow_shard_set_digest_sorts_and_rejects_unsafe_sets(tmp_path):
    day = tmp_path / "date=2026-07-12"
    day.mkdir()
    base = day / "rfq_00.ndjson"
    shard1 = day / "rfq_00.ndjson.1"
    base.write_text("base\n")
    shard1.write_text("rotated\n")
    encoded = receipt_outer_v3(T0 + dt.timedelta(hours=1),
                               "2026-07-12T00", [base, shard1])
    receipt = json.loads(json.loads(encoded)["raw"])
    shards = receipt["capture_shards"]
    assert (rfq.capture_shard_set_sha256(shards) ==
            rfq.capture_shard_set_sha256(list(reversed(shards))))
    with pytest.raises(ValueError):
        rfq.capture_shard_set_sha256([shards[0], dict(
            shards[1], ordinal=0, relpath=shards[0]["relpath"])])
    with pytest.raises(ValueError):
        rfq.capture_shard_set_sha256([shards[0], dict(
            shards[1], ordinal=2,
            relpath="date=2026-07-12/rfq_00.ndjson.2")])
    with pytest.raises(ValueError):
        rfq.capture_shard_set_sha256([dict(
            shards[0], relpath="../rfq_00.ndjson")])


@pytest.mark.parametrize(("mutation", "expected"), [
    ("missing_raw", "raw_evidence is missing/invalid"),
    ("missing_metrics_field", "max_recorder_dropped is missing/non-zero"),
    ("bad_boundary", "expected_start_wall_ns does not match"),
    ("bad_child_rc", "child_rc must be null"),
    ("bad_hour_open", "hour_open count is not exactly one"),
    ("bad_partition", "partition_mismatches is not zero"),
    ("bad_raw_invalidation", "subscription_invalidations is not zero"),
    ("bad_raw_findings", "raw findings are missing/non-empty"),
    ("bad_metrics_findings", "metrics findings are missing/non-empty"),
    ("bad_reconnect", "max_reconnects is missing/non-zero"),
    ("bad_disconnect", "max_disconnects is missing/non-zero"),
    ("bad_metric_counter", "max_errors is missing/non-zero"),
    ("bad_drop", "max_recorder_dropped is missing/non-zero"),
    ("bad_write", "max_recorder_write_failures is missing/non-zero"),
    ("bad_metric_coverage", "do not cover the hour end"),
    ("unparsed_eof", "not parsed through exact EOF"),
])
def test_v3_receipt_health_evidence_is_required(tmp_path, mutation, expected):
    day = tmp_path / "date=2026-07-12"
    day.mkdir()
    base = day / "rfq_00.ndjson"
    base.write_text("base\n")
    encoded = receipt_outer_v3(T0 + dt.timedelta(hours=1),
                               "2026-07-12T00", [base])
    receipt = json.loads(json.loads(encoded)["raw"])
    assert rfq.receipt_contract_errors(receipt) == []
    if mutation == "missing_raw":
        receipt.pop("raw_evidence")
    elif mutation == "missing_metrics_field":
        receipt["metrics_evidence"].pop("max_recorder_dropped")
    elif mutation == "bad_boundary":
        receipt["expected_start_wall_ns"] += 1
    elif mutation == "bad_child_rc":
        receipt["child_rc"] = 0
    elif mutation == "bad_hour_open":
        receipt["raw_evidence"]["markers"]["hour_open"] = 2
    elif mutation == "bad_partition":
        receipt["raw_evidence"]["partition_mismatches"] = 1
    elif mutation == "bad_raw_invalidation":
        receipt["raw_evidence"]["subscription_invalidations"] = 1
    elif mutation == "bad_raw_findings":
        receipt["raw_evidence"]["findings"] = ["bad"]
    elif mutation == "bad_metrics_findings":
        receipt["metrics_evidence"]["findings"] = ["bad"]
    elif mutation == "bad_reconnect":
        receipt["metrics_evidence"]["max_reconnects"] = 1
    elif mutation == "bad_disconnect":
        receipt["metrics_evidence"]["max_disconnects"] = 1
    elif mutation == "bad_metric_counter":
        receipt["metrics_evidence"]["max_errors"] = 1
    elif mutation == "bad_drop":
        receipt["metrics_evidence"]["max_recorder_dropped"] = 1
    elif mutation == "bad_write":
        receipt["metrics_evidence"]["max_recorder_write_failures"] = 1
    elif mutation == "bad_metric_coverage":
        receipt["metrics_evidence"]["last_ts_ms"] -= 10_000
    elif mutation == "unparsed_eof":
        receipt["capture_shards"][0]["parsed_bytes_at_close"] -= 1
    assert any(expected in error
               for error in rfq.receipt_contract_errors(receipt))


def test_v2_reader_is_compatible_but_cannot_attest_extra_shard(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    shard1 = Path(paths[0] + ".1")
    shard1.write_text(outer(
        T0 + dt.timedelta(minutes=10),
        {"type": "quote_created", "sid": 15, "msg": {"id": "q"}}))
    all_paths = paths + [str(shard1)] + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    assert any("v2 PASS receipt does not bind complete shard set" in failure
               for failure in report["window"]["seal_failures"])
    assert report["window"]["complete"] is False


@pytest.mark.parametrize("mutation", ["append", "new_shard"])
def test_v3_receipt_fails_closed_on_post_receipt_shard_mutation(tmp_path, mutation):
    paths, receipt_paths = fixture_48h(tmp_path)
    base = Path(paths[0])
    shard1 = Path(str(base) + ".1")
    shard1.write_text(outer(
        T0 + dt.timedelta(minutes=10),
        {"type": "quote_created", "sid": 15, "msg": {"id": "q"}}))
    replace_hour_receipt(
        receipt_paths, "2026-07-12T00",
        receipt_outer_v3(T0 + dt.timedelta(hours=1, seconds=2),
                         "2026-07-12T00", [base, shard1]))
    capture_paths = paths + [str(shard1)]
    if mutation == "append":
        with shard1.open("a") as fh:
            fh.write(outer(T0 + dt.timedelta(minutes=11),
                           {"type": "quote_created", "sid": 15,
                            "msg": {"id": "late"}}))
    else:
        shard2 = Path(str(base) + ".2")
        shard2.write_text(outer(
            T0 + dt.timedelta(minutes=11),
            {"type": "quote_created", "sid": 15, "msg": {"id": "late"}}))
        capture_paths.append(str(shard2))
    all_paths = capture_paths + receipt_paths
    report = rfq.build_report(rfq.parse_paths(all_paths), {}, start=T0, hours=48,
                              seal_index=seal_index(all_paths))
    failures = report["window"]["seal_failures"]
    expected = ("post-receipt append" if mutation == "append" else
                "does not bind complete shard set")
    assert any(expected in failure for failure in failures)
    assert report["window"]["complete"] is False


def test_noncontiguous_and_symlink_shards_are_blocking(tmp_path):
    paths, receipt_paths = fixture_48h(tmp_path)
    gap = Path(paths[0] + ".2")
    gap.write_text(outer(
        T0 + dt.timedelta(minutes=10),
        {"type": "quote_created", "sid": 15, "msg": {"id": "gap"}}))
    target = tmp_path / "target.ndjson"
    target.write_text("target\n")
    link = Path(paths[1] + ".1")
    link.symlink_to(target)
    all_paths = paths + [str(gap), str(link)] + receipt_paths
    parsed = rfq.parse_paths(all_paths)
    report = rfq.build_report(parsed, {}, start=T0, hours=48,
                              seal_index=seal_index(paths + [str(gap)] + receipt_paths))
    dq = report["data_quality"]["parse_and_schema_counts"]
    assert dq["non_contiguous_capture_shards"] == 1
    assert dq["unsafe_or_unreadable_file"] == 1
    assert report["window"]["complete"] is False


def test_report_hashing_is_streaming_without_path_read_bytes(tmp_path, monkeypatch):
    paths, receipt_paths = fixture_48h(tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda _self: (_ for _ in ()).throw(
        AssertionError("RFQ report must hash incrementally")))
    parsed = rfq.parse_paths(paths + receipt_paths)
    assert parsed["input_files"]
    assert "read_bytes(" not in TOOL.read_text()


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
