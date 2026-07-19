"""Adversarial offline tests for bounded fresh-RFQ D01--D07."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tools" / "research"), str(ROOT / "tests")]

import deep03_v3_rfq_bounded as bounded  # noqa: E402
import fresh_rfq_receipts as fresh  # noqa: E402
import test_fresh_rfq_receipts as overlay_fixture  # noqa: E402
import test_fresh_rfq_request_provenance as request_fixture  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class LocalExactClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.head_calls = 0
        self.get_calls = 0

    def head(self, bucket, key, version_id):
        del bucket
        self.head_calls += 1
        body = self.bodies[(key, version_id)]
        return {"VersionId": version_id, "ContentLength": len(body)}

    def get_exact(self, bucket, key, version_id, destination):
        del bucket
        self.get_calls += 1
        body = self.bodies[(key, version_id)]
        Path(destination).write_bytes(body)
        return {"VersionId": version_id, "ContentLength": len(body)}


def _write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bounded.canonical_bytes(value) + b"\n")
    return path


def _delete(request_id, market, deleted_ts, *, creator="public-one", **extra):
    msg = {
        "id": request_id,
        "creator_id": creator,
        "market_ticker": market,
        "deleted_ts": deleted_ts,
    }
    msg.update(extra)
    return json.dumps(
        {"type": "rfq_deleted", "sid": 17, "msg": msg},
        sort_keys=True, separators=(",", ":"),
    )


def _overlay_cache(tmp_path, monkeypatch):
    rows = request_fixture._happy_rows()
    rows[1].append(request_fixture._frame_line(
        1,
        _delete(
            "r-single", "KX-SINGLE", "2026-07-17T01:00:20Z",
            contracts_fp="10.25",
        ),
        second=20,
    ))
    (authority, receipts, _base, base_raw, base_identity,
     evidence) = overlay_fixture.overlay_inputs(monkeypatch, request_rows=rows)
    # test_fresh_rfq_receipts intentionally loads an isolated module object;
    # bind the production import to the same synthetic deny-set fingerprint.
    monkeypatch.setattr(
        fresh, "OLD_284_OBJECT_SET_SHA256",
        authority["old_284_object_set_sha256"],
    )
    inputs = overlay_fixture.mapping_provenance_inputs(receipts)
    manifest = fresh.build_overlay_manifest(
        authority=authority,
        base_manifest_bytes=base_raw,
        base_manifest_exact_identity=base_identity,
        hour_receipts=receipts,
        source_evidence=evidence,
        mapping_provenance_inputs=inputs,
    )
    date = manifest["eligible_date"]
    terminal_sha = _sha("base-terminal")
    receipt = {
        "schema_version": bounded.OVERLAY_RECEIPT_SCHEMA,
        "state": "RFQ_OVERLAY_LOCALLY_READY",
        "date": date,
        "base_state": "V3_REFERENCE_PUBLISHED",
        "base_rfq": "OFF",
        "base_terminal_file_sha256": terminal_sha,
        "base_manifest_exact_identity": base_identity,
        "base_binding_sha256": manifest["base_binding_sha256"],
        "eligibility_sha256": _sha("eligibility"),
        "authority_sha256": authority["authority_sha256"],
        "source_evidence_sha256": manifest["source_evidence_sha256"],
        "analysis_rfq_objects": manifest["analysis_rfq_objects"],
        "analysis_rfq_object_set_sha256": manifest[
            "analysis_rfq_object_set_sha256"
        ],
        "overlay_manifest_sha256": manifest["manifest_sha256"],
        "required_tags": {
            "research-eligible": "true", "research-channel": "rfq",
        },
        "tag_state": "NOT_ATTEMPTED",
        "publication_state": "NOT_ATTEMPTED",
        "base_mutations": 0,
        "data_objects_copied": 0,
        "aws_writes": 0,
    }
    receipt["overlay_receipt_sha256"] = bounded.canonical_sha256(receipt)
    ready = {
        "schema_version": bounded.OVERLAY_READY_SCHEMA,
        "state": "RFQ_OVERLAY_CACHE_READY",
        "date": date,
        "overlay_receipt_sha256": receipt["overlay_receipt_sha256"],
        "overlay_manifest_sha256": manifest["manifest_sha256"],
        "base_terminal_file_sha256": terminal_sha,
        "base_mutations": 0,
    }
    ready["ready_sha256"] = bounded.canonical_sha256(ready)
    root = tmp_path / "overlay"
    ready_path = _write(root / "READY.json", ready)
    _write(root / "OVERLAY-RECEIPT.json", receipt)
    _write(root / "MANIFEST.json", manifest)
    bodies = {
        (row["key"], row["version_id"]): row["body"]
        for row in inputs["exact_analysis_rfq_objects"]
    }
    return ready_path, authority, manifest, bodies


def test_end_to_end_fresh_only_exact_dedup_lifecycle_and_resume(
    tmp_path, monkeypatch,
):
    ready, authority, _manifest, bodies = _overlay_cache(tmp_path, monkeypatch)
    clients = []

    def factory(_descriptor):
        client = LocalExactClient(bodies)
        clients.append(client)
        return client

    report_path = tmp_path / "REPORT.json"
    report = bounded.run_bounded_fresh_rfq(
        overlay_ready_paths=[ready],
        fresh_authority=authority,
        client_factory=factory,
        checkpoint_root=tmp_path / "checkpoints",
        hash_buckets=1,
        report_path=report_path,
    )
    assert len(clients) == 1
    assert clients[0].head_calls == 24
    assert clients[0].get_calls == 24
    assert report["old_lineage_state"] == "DATA_INTEGRITY_BLOCKED_NOT_READ"
    assert report["inputs"]["exact_object_count"] == 24
    assert report["inputs"]["source_gap_gate"].startswith("PASS")
    assert report["conservation"] == {
        **report["conservation"],
        "source_create_occurrences": 3,
        "source_delete_occurrences": 2,
        "global_unique_create_ids": 2,
        "conflicting_create_ids_excluded": 0,
        "lifecycle_rows": 2,
        "identity_equation_pass": True,
        "orphan_delete_ids_excluded": 1,
    }
    assert report["d01_flow_census"]["unique_requests"] == 2
    assert report["d02_size_intent"]["contracts_fp"]["n"] == 1
    assert report["d02_size_intent"]["target_cost_dollars"]["n"] == 1
    assert report["d03_lifecycle"]["deletion_observed"] == 1
    assert report["d03_lifecycle"]["right_censored"] == 1
    assert report["d03_lifecycle"]["observed_lifetime_ms"]["p50"] == 10_000
    assert report["d04_combo_leg_pressure"]["combo_requests"] == 1
    assert report["d04_combo_leg_pressure"]["yes_side_legs"] == 1
    assert report["d04_combo_leg_pressure"]["no_side_legs"] == 1
    assert report["d05_requester_concentration"]["known_id_coverage_bps"] == 5_000
    assert report["d06_direction_volume_proxy"]["requests_with_observed_combo_leg_side"] == 1
    assert report["d07_rfq_to_clob_impact"]["status"] == "BLOCKED_ADAPTER_NOT_SUPPLIED"
    assert report["d08_profitability"]["status"].startswith("BLOCKED_")
    assert report["d08_profitability"]["pnl_claim"] is False
    saved = json.loads(report_path.read_text())
    assert saved == report
    unsigned = copy.deepcopy(report)
    supplied = unsigned.pop("report_sha256")
    assert supplied == bounded.canonical_sha256(unsigned)

    def must_not_read(_descriptor):
        raise AssertionError("complete checkpoint attempted a second exact read")

    resumed = bounded.run_bounded_fresh_rfq(
        overlay_ready_paths=[ready],
        fresh_authority=authority,
        client_factory=must_not_read,
        checkpoint_root=tmp_path / "checkpoints",
        hash_buckets=1,
    )
    assert resumed == report


def test_invalid_authority_is_rejected_before_exact_client(tmp_path, monkeypatch):
    ready, authority, _manifest, _bodies = _overlay_cache(tmp_path, monkeypatch)
    bad = copy.deepcopy(authority)
    bad["repair_state"] = "ALLOWED"
    called = False

    def factory(_descriptor):
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(bounded.FreshRfqResearchError, match="FRESH_AUTHORITY_INVALID"):
        bounded.run_bounded_fresh_rfq(
            overlay_ready_paths=[ready], fresh_authority=bad,
            client_factory=factory, checkpoint_root=tmp_path / "cp",
            hash_buckets=1,
        )
    assert called is False


def test_overlay_digest_tamper_is_rejected_before_exact_client(tmp_path, monkeypatch):
    ready, authority, _manifest, _bodies = _overlay_cache(tmp_path, monkeypatch)
    manifest_path = ready.parent / "MANIFEST.json"
    value = json.loads(manifest_path.read_text())
    value["research_ready"] = True
    # Deliberately do not reseal: both local-only state and digest must reject.
    manifest_path.write_bytes(bounded.canonical_bytes(value) + b"\n")
    with pytest.raises(bounded.FreshRfqResearchError, match="OVERLAY_MANIFEST_INVALID"):
        bounded.run_bounded_fresh_rfq(
            overlay_ready_paths=[ready], fresh_authority=authority,
            client_factory=lambda _descriptor: pytest.fail("client called"),
            checkpoint_root=tmp_path / "cp", hash_buckets=1,
        )


def test_exact_body_tamper_poison_fails_without_report(tmp_path, monkeypatch):
    ready, authority, _manifest, bodies = _overlay_cache(tmp_path, monkeypatch)
    tampered = copy.deepcopy(bodies)
    first = next(iter(tampered))
    tampered[first] = tampered[first] + b" "
    with pytest.raises(Exception, match="SIZE|size|mismatch"):
        bounded.run_bounded_fresh_rfq(
            overlay_ready_paths=[ready], fresh_authority=authority,
            client_factory=lambda _descriptor: LocalExactClient(tampered),
            checkpoint_root=tmp_path / "cp", hash_buckets=1,
            report_path=tmp_path / "must-not-exist.json",
        )
    assert not (tmp_path / "must-not-exist.json").exists()


def _event_row(rfq_id, *, economic, raw, event_type="CREATE", ts=1_000_000):
    return {
        "analysis_date": "2026-07-19", "event_type": event_type,
        "rfq_id": rfq_id, "rfq_id_sha256": _sha(rfq_id),
        "exchange_ts_us": ts, "recv_wall_ns": ts * 1000,
        "clock_skew_ms": 0, "market_ticker": "KX-A",
        "creator_hash": None, "contracts_e2": 100,
        "target_cost_e6": None, "mve_collection_ticker": None,
        "legs_json": "[]", "leg_count": 0, "yes_leg_count": 0,
        "no_leg_count": 0, "unknown_side_leg_count": 0,
        "raw_sha256": raw, "economic_sha256": economic,
        "source_key": "ec2/raw/date=2026-07-19/rfq_00.ndjson",
        "source_version_id": "v1", "source_line": 1,
    }


def test_global_full_id_reducer_excludes_conflict_without_cross_id_collision(tmp_path):
    con = duckdb.connect()
    try:
        rows = [
            _event_row("same-id", economic=_sha("econ-a"), raw=_sha("raw-a")),
            _event_row("same-id", economic=_sha("econ-b"), raw=_sha("raw-b"), ts=2_000_000),
            _event_row("other-id", economic=_sha("econ-c"), raw=_sha("raw-c")),
            # Exact duplicate of other-id must deduplicate, not conflict.
            _event_row("other-id", economic=_sha("econ-c"), raw=_sha("raw-c")),
        ]
        source = tmp_path / "events.ndjson"
        source.write_bytes(b"".join(bounded.canonical_bytes(row) + b"\n" for row in rows))
        parquet = tmp_path / "events.parquet"
        con.execute(
            f"COPY ({bounded._json_select(source, bounded.EVENT_COLUMNS)}) "
            f"TO {bounded.quote(parquet)} (FORMAT PARQUET)"
        )
        result = con.execute(
            bounded._lifecycle_sql([parquet], 10_000_000)
        ).fetchall()
        assert len(result) == 1
        names = [item[0] for item in con.description]
        row = dict(zip(names, result[0]))
        assert row["rfq_id"] == "other-id"
        assert row["exact_create_duplicate_count"] == 1
    finally:
        con.close()


def _minimal_mapping():
    rows = [{
        "request_id": "r1", "created_ts": "2026-07-19T00:00:01Z",
        "request_kind": "SINGLE", "component_index": 0,
        "source_field": "market_ticker", "market_ticker": "KX-A",
        "l1_present": True, "l2_present": True,
        "mapping_state": "MAPPED_L1_L2",
        "event_window_within_base_date": True,
    }]
    return {"mapping_sha256": _sha("mapping"), "mapping_rows": rows}


def _impact(mapping, *, status="OBSERVED"):
    observed = status == "OBSERVED"
    rows = [{
        "request_id": "r1", "component_index": 0,
        "market_ticker": "KX-A", "status": status,
        "pre_mid_e6": 400_000 if observed else None,
        "post_mid_e6": 410_000 if observed else None,
        "pre_spread_e6": 20_000 if observed else None,
        "post_spread_e6": 10_000 if observed else None,
        "pre_depth_e2": 1_000 if observed else None,
        "post_depth_e2": 1_200 if observed else None,
        "l1_rows": 2 if observed else 0, "l2_rows": 2 if observed else 0,
        "gap_rows": 0,
    }]
    value = {
        "schema": bounded.IMPACT_SCHEMA, "state": "COMPLETE",
        "analysis_date": "2026-07-19",
        "mapping_sha256": mapping["mapping_sha256"],
        "source_gate_sha256": _sha("gate"),
        "observation_count": 1, "observations": rows,
        "observations_sha256": bounded.canonical_sha256(rows),
    }
    value["adapter_sha256"] = bounded.canonical_sha256(value)
    return value


def test_d07_adapter_requires_exact_mapping_and_censor_semantics():
    mapping = _minimal_mapping()
    descriptor = {"date": "2026-07-19", "source_gate_sha256": _sha("gate")}
    value = _impact(mapping)
    assert bounded._validate_impact_adapter(value, descriptor, mapping) == value
    missing = copy.deepcopy(value)
    missing["observations"] = []
    missing["observation_count"] = 0
    missing["observations_sha256"] = bounded.canonical_sha256([])
    missing["adapter_sha256"] = bounded.canonical_sha256({
        key: item for key, item in missing.items() if key != "adapter_sha256"
    })
    with pytest.raises(bounded.FreshRfqResearchError, match="COVERAGE"):
        bounded._validate_impact_adapter(missing, descriptor, mapping)
    bad_censor = _impact(mapping, status="CENSORED_GAP")
    with pytest.raises(bounded.FreshRfqResearchError, match="lacks gap"):
        bounded._validate_impact_adapter(bad_censor, descriptor, mapping)


def test_combo_side_stays_attached_to_its_original_ticker():
    physical = request_fixture._happy_rows()[12][0]
    outer = json.loads(physical)
    frame = json.loads(outer["raw"])
    event = bounded._parse_event(
        frame, outer, analysis_date="2026-07-17",
        source_key="ec2/raw/date=2026-07-17/rfq_12.ndjson",
        source_version_id="v1", line_number=1,
    )
    assert json.loads(event["legs_json"]) == [
        {
            "market_ticker": "KX-LEG-A", "side": "no",
            "yes_settlement_value_e6": None,
        },
        {
            "market_ticker": "KX-LEG-B", "side": "yes",
            "yes_settlement_value_e6": 1_000_000,
        },
    ]


def test_source_hour_gap_gate_rejects_non_exact_receipt(tmp_path, monkeypatch):
    _ready, authority, manifest, _bodies = _overlay_cache(tmp_path, monkeypatch)
    broken = copy.deepcopy(manifest)
    broken["analysis_hours"][7]["resolution_state"] = "UNRESOLVED"
    with pytest.raises(Exception, match="SOURCE_RESOLUTION_REQUIRED|FRESH_HOUR_INVALID"):
        bounded._validate_overlay_hours(broken, authority, manifest["eligible_date"])
