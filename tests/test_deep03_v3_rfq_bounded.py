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
import deep03_v3_methods as methods  # noqa: E402
import fresh_rfq_exact_reader as exact_reader  # noqa: E402
import fresh_rfq_receipts as fresh  # noqa: E402
import test_fresh_rfq_base_binding as base_fixture  # noqa: E402
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


def _overlay_cache(tmp_path, monkeypatch, *, return_base=False):
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
    result = (ready_path, authority, manifest, bodies)
    return result + (base_raw,) if return_base else result


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
        expected_eligible_dates=["2026-07-17"],
    )
    assert len(clients) == 1
    assert clients[0].head_calls == 24
    assert clients[0].get_calls == 24
    assert report["old_lineage_state"] == "DATA_INTEGRITY_BLOCKED_NOT_READ"
    assert report["inputs"]["exact_object_count"] == 24
    assert report["inputs"]["source_gap_gate"].startswith("PASS")
    assert report["inputs"]["resource_contract"]["state"] == "PASS"
    assert report["inputs"]["resource_contract"]["partial_date_reuse"] is False
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
    assert report["d03_lifecycle"]["observed_lifetime"]["exact"]["p50"] == 10_000_000
    assert report["d03_lifecycle"]["observed_lifetime"][
        "display_milliseconds"
    ]["p50"] == 10_000
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
        expected_eligible_dates=["2026-07-17"],
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


def test_exact_eligible_date_pin_rejects_wrong_overlay_before_client(
    tmp_path, monkeypatch,
):
    ready, authority, _manifest, _bodies = _overlay_cache(tmp_path, monkeypatch)
    called = False

    def factory(_descriptor):
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(bounded.FreshRfqResearchError, match="ELIGIBLE_DATE_MISMATCH"):
        bounded.run_bounded_fresh_rfq(
            overlay_ready_paths=[ready], fresh_authority=authority,
            expected_eligible_dates=["2026-07-20"],
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


def _event_row(
    rfq_id, *, economic, raw, event_type="CREATE", ts=1_000_000,
    recv_us=None, market="KX-A", clock_state=None, source_line=1,
):
    if recv_us is None:
        recv_us = ts
    skew_us = recv_us - ts
    if clock_state is None:
        clock_state = bounded._event_clock_state(ts, recv_us * 1_000)
    return {
        "analysis_date": "2026-07-19", "event_type": event_type,
        "rfq_id": rfq_id, "rfq_id_sha256": _sha(rfq_id),
        "exchange_ts_us": ts, "recv_wall_ns": recv_us * 1000,
        "clock_skew_us": skew_us, "clock_state": clock_state,
        "market_ticker": market,
        "creator_hash": None, "contracts_e2": 100,
        "target_cost_e6": None, "mve_collection_ticker": None,
        "legs_json": "[]", "leg_count": 0, "yes_leg_count": 0,
        "no_leg_count": 0, "unknown_side_leg_count": 0,
        "raw_sha256": raw, "economic_sha256": economic,
        "source_key": "ec2/raw/date=2026-07-19/rfq_00.ndjson",
        "source_version_id": "v1", "source_line": source_line,
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


def test_lifecycle_delete_endpoint_classes_are_mutually_exclusive(tmp_path):
    rows = []
    for index, request_id in enumerate(("ok", "inconsistent", "clock", "none")):
        rows.append(_event_row(
            request_id, economic=_sha(f"create-{request_id}"),
            raw=_sha(f"create-raw-{request_id}"), source_line=index + 1,
        ))
    rows.extend([
        _event_row(
            "ok", economic=_sha("delete-ok"), raw=_sha("delete-ok"),
            event_type="DELETE", ts=2_000_000, source_line=10,
        ),
        _event_row(
            "ok", economic=_sha("delete-ok-bad"), raw=_sha("delete-ok-bad"),
            event_type="DELETE", ts=2_500_000, market="KX-WRONG",
            source_line=11,
        ),
        _event_row(
            "inconsistent", economic=_sha("delete-inconsistent"),
            raw=_sha("delete-inconsistent"), event_type="DELETE",
            ts=500_000, source_line=12,
        ),
        _event_row(
            "clock", economic=_sha("delete-clock"), raw=_sha("delete-clock"),
            event_type="DELETE", ts=2_000_000, recv_us=8_000_001,
            source_line=13,
        ),
    ])
    source = tmp_path / "events.ndjson"
    source.write_bytes(b"".join(
        bounded.canonical_bytes(row) + b"\n" for row in rows
    ))
    parquet = tmp_path / "events.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY ({bounded._json_select(source, bounded.EVENT_COLUMNS)}) "
            f"TO {bounded.quote(parquet)} (FORMAT PARQUET)"
        )
        values = con.execute(
            bounded._lifecycle_sql([parquet], 20_000_000)
        ).fetchall()
        names = [item[0] for item in con.description]
        result = {dict(zip(names, row))["rfq_id"]: dict(zip(names, row))
                  for row in values}
        assert result["ok"]["delete_endpoint_state"] == "CONSISTENT_DELETE_OBSERVED"
        assert result["ok"]["consistent_delete_count"] == 1
        assert result["ok"]["payload_inconsistent_delete_count"] == 1
        assert result["ok"]["delete_candidate_count"] == 2
        assert result["inconsistent"]["delete_endpoint_state"] == (
            "CENSORED_INCONSISTENT_DELETE"
        )
        assert result["inconsistent"]["temporal_inconsistent_delete_count"] == 1
        assert result["clock"]["delete_endpoint_state"] == "CENSORED_DELETE_CLOCK"
        assert result["clock"]["clock_censored_delete_count"] == 1
        assert result["none"]["delete_endpoint_state"] == "RIGHT_CENSORED_NO_DELETE"
        assert sum(
            row["consistent_delete_count"]
            + row["inconsistent_delete_count"]
            + row["clock_censored_delete_count"]
            for row in result.values()
        ) == sum(row["delete_candidate_count"] for row in result.values())
    finally:
        con.close()


def test_future_clock_create_is_not_admitted_to_lifecycle(tmp_path):
    rows = [
        _event_row(
            "valid", economic=_sha("valid"), raw=_sha("valid"),
        ),
        _event_row(
            "future", economic=_sha("future"), raw=_sha("future"),
            ts=8_000_001, recv_us=1_000_000,
        ),
    ]
    source = tmp_path / "events.ndjson"
    source.write_bytes(b"".join(
        bounded.canonical_bytes(row) + b"\n" for row in rows
    ))
    parquet = tmp_path / "events.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY ({bounded._json_select(source, bounded.EVENT_COLUMNS)}) "
            f"TO {bounded.quote(parquet)} (FORMAT PARQUET)"
        )
        result = con.execute(
            bounded._lifecycle_sql([parquet], 20_000_000)
        ).fetchall()
        assert [row[1] for row in result] == ["valid"]
    finally:
        con.close()


def test_km_compares_exact_microseconds_at_horizon_boundary():
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TEMP TABLE life(deletion_observed BOOLEAN,"
            "observed_duration_us BIGINT)"
        )
        con.executemany(
            "INSERT INTO life VALUES (?,?)",
            [(True, 1_000_000), (True, 1_000_001), (False, 2_000_000)],
        )
        first = bounded._kaplan_meier_us(con, "life")[0]
        assert first["horizon_ms"] == 1_000
        assert first["horizon_us"] == 1_000_000
        assert first["deletions_through_horizon"] == 1
        assert first["event_time_count"] == 1
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
    return {
        "mapping_sha256": _sha("mapping"), "mapping_rows": rows,
        "mapping_input_ticker_count": 1,
        "pre_event_window_ms": 1_000, "post_event_window_ms": 1_000,
    }


def _reader_attestation(identities):
    """Full-shape exact-reader core attestation over an exact object set."""
    objects = sorted(
        (
            {
                key: row[key]
                for key in ("bucket", "key", "version_id", "size", "sha256")
            }
            for row in identities
        ),
        key=lambda row: (row["key"], row["version_id"]),
    )
    ledger = [
        {"key": row["key"], "version_id": row["version_id"]}
        for row in objects
    ]
    total = sum(row["size"] for row in objects)
    value = {
        "schema": exact_reader.ATTESTATION_SCHEMA,
        "state": "ALL_EXPECTED_CALLER_IDENTITIES_BODY_VERIFIED",
        "verification_state":
            "INJECTED_CLIENT_RESPONSE_AND_FULL_BODY_SHA256_VERIFIED",
        "source_bucket": exact_reader.SOURCE_BUCKET,
        "caller_declared_transport_kind": "INJECTED_EXACT_VERSION_CLIENT",
        "transport_attestation_state": "CALLER_ADAPTER_UNVERIFIED",
        "objects": objects,
        "read_ledger": ledger,
        "read_ledger_sha256": bounded.canonical_sha256(ledger),
        "expected_object_count": len(objects),
        "verified_object_count": len(objects),
        "expected_total_bytes": total,
        "verified_total_bytes": total,
        "expected_object_set_sha256": bounded.canonical_sha256(objects),
        "verified_object_set_sha256": bounded.canonical_sha256(objects),
        "all_expected_objects_verified": True,
        "unexpected_object_count": 0,
        "duplicate_read_count": 0,
        "module_read_api_methods_invoked": ["head", "get_exact"],
        "module_head_call_count": len(objects),
        "module_get_exact_call_count": len(objects),
        "version_id_argument_supplied_on_all_calls": True,
        "module_list_api_call_count": 0,
        "module_write_api_call_count": 0,
        "exact_body_identity_verified": True,
        "source_objects_exact_get_verified": False,
        "aws_transport_verified": False,
        "aws_no_write_verified": False,
        "requires_external_iam_and_operation_audit": True,
        "max_active_object_count": 1,
        "ephemeral_temp_directory_mode": "0700",
        "ephemeral_temp_file_mode": "0600",
        "ephemeral_files_created": len(objects),
        "ephemeral_bytes_staged": total,
        "ephemeral_temp_deleted_before_return": True,
        "input_bodies_omitted": True,
        "module_durable_data_copy_count": 0,
    }
    value["attestation_sha256"] = bounded.canonical_sha256(value)
    return value


def _producer_receipt():
    value = {
        "schema": bounded.D07_PRODUCER_SCHEMA,
        "state": "AUDITED_PASS",
        "algorithm_id": "d07-clob-impact-v1",
        "producer_module_sha256": _sha("d07-producer-module"),
        "audit_receipt_sha256": _sha("d07-producer-audit"),
    }
    value["receipt_sha256"] = bounded.canonical_sha256(value)
    return value


def _external_anchor(producer_shas, attestation_shas, partition_set_shas):
    value = {
        "schema": bounded.D07_EXTERNAL_ANCHOR_SCHEMA,
        "state": "INDEPENDENT_AUDIT_PASS",
        "audit_authority": (
            "docs/plan_audits/AUDIT_DEEP03_RFQ_D07_RUNTIME_PASS "
            "(root-installed 0444 runtime AUTHORITY)"
        ),
        "producer_receipt_sha256s": sorted(producer_shas),
        "reader_attestation_sha256s": sorted(attestation_shas),
        "partition_receipt_set_sha256s": sorted(partition_set_shas),
    }
    value["anchor_sha256"] = bounded.canonical_sha256(value)
    return value


def _partition_receipts(sources, observation_count, evidence_digests=None):
    evidence_digests = evidence_digests or {}
    empty_digest = bounded.canonical_sha256([])
    receipts = []
    for family in bounded.IMPACT_SOURCE_FAMILIES:
        value = {
            "schema": bounded.D07_PARTITION_RECEIPT_SCHEMA,
            "analysis_date": "2026-07-19",
            "family": family,
            "source_object_count": sources[family]["exact_object_count"],
            "source_row_count": sources[family]["source_row_count"],
            "component_row_count": observation_count,
            "evidence_row_set_sha256": evidence_digests.get(
                family, empty_digest,
            ),
            "content_sha256": _sha(f"partition-{family}"),
        }
        value["receipt_sha256"] = bounded.canonical_sha256(value)
        receipts.append(value)
    return receipts


def _evidence_digests(rows):
    evidence = {family: set() for family in bounded.IMPACT_SOURCE_FAMILIES}
    for row in rows:
        for side in ("pre_book", "post_book"):
            book = row.get(side)
            if book is not None:
                evidence[book["source_family"]].add(book["source_row_sha256"])
    return {
        family: bounded.canonical_sha256(sorted(hashes))
        for family, hashes in evidence.items()
    }


def _d07_context(mapping):
    identities = {}
    families = {}
    for family in ("orderbooks_l1", "orderbooks_full"):
        identity = {
            "logical_key": f"warehouse/facts/{family}/date=2026-07-19/part.parquet",
            "bucket": "kalshi-vault-ritcardo",
            "key": f"ec2/warehouse/facts/{family}/date=2026-07-19/part.parquet",
            "version_id": f"version-{family}", "size": 100,
            "sha256": _sha(family),
        }
        identities[family] = [identity]
        families[family] = {
            "object_count": 1,
            "set_sha256": bounded.canonical_sha256([identity]),
            "objects": [identity],
        }
    exact_base = {
        "binding_sha256": _sha("base"), "release_id": "release-2026-07-19",
        "manifest_exact_identity": {
            "bucket": "kalshi-vault-ritcardo",
            "key": "research/releases/release-2026-07-19/MANIFEST.json",
            "version_id": "manifest-version", "size": 1000,
            "sha256": _sha("manifest"),
        },
        "base_exact_set_sha256": _sha("base-set"), "families": families,
    }
    provenance_families = {}
    for family in ("orderbooks_l1", "orderbooks_full"):
        receipt = {
            **identities[family][0], "row_count": 10,
            "min_ts_utc": 1, "max_ts_utc": 2,
            "body_size_verified": True, "body_sha256_verified": True,
        }
        provenance_families[family] = {
            "body_verified_object_count": 1, "object_receipts": [receipt],
        }
    descriptor = {
        "date": "2026-07-19", "source_gate_sha256": _sha("gate"),
        "base_binding_sha256": exact_base["binding_sha256"],
        "base_binding": exact_base,
        "manifest": {
            "universe_provenance": {"families": provenance_families},
        },
    }
    quality = {
        "state": "PASS", "gate_sha256": _sha("quality"), "blockers": [],
        "base_release_id": exact_base["release_id"],
    }
    event_us = bounded._parse_utc_us(
        mapping["mapping_rows"][0]["created_ts"], "event",
    )
    events = {
        "r1": {
            "create_recv_wall_ns": event_us * 1_000,
            "legs_json": "[]", "leg_count": 0,
        },
    }
    return descriptor, exact_base, quality, events


def _book(ts_us, bid_e6, ask_e6, bid_depth_e2, ask_depth_e2, logical_key):
    content = {
        "ts_us": ts_us, "bid_e6": bid_e6, "ask_e6": ask_e6,
        "bid_depth_e2": bid_depth_e2, "ask_depth_e2": ask_depth_e2,
        "source_family": "orderbooks_l1",
        "source_logical_key": logical_key,
    }
    # The row hash binds the row's own canonical bytes (module contract).
    return {**content, "source_row_sha256": _rebind_book(content)}


def _rebind_book(book):
    return bounded.canonical_sha256({
        field: book[field]
        for field in sorted(bounded.IMPACT_BOOK_FIELDS - {"source_row_sha256"})
    })


def _impact(mapping, descriptor, exact_base, quality, *, status="OBSERVED"):
    observed = status == "OBSERVED"
    event_us = bounded._parse_utc_us(
        mapping["mapping_rows"][0]["created_ts"], "event",
    )
    l1_logical = exact_base["families"]["orderbooks_l1"]["objects"][0][
        "logical_key"
    ]
    rows = [{
        "request_id": "r1", "component_index": 0,
        "market_ticker": "KX-A", "status": status,
        "event_ts_us": event_us, "rfq_recv_wall_ns": event_us * 1_000,
        "rfq_clock_skew_us": 0,
        "pre_window_start_us": event_us - 1_000_000,
        "pre_window_end_us": event_us,
        "post_window_start_us": event_us,
        "post_window_end_us": event_us + 1_000_000,
        "pre_observation_ts_us": event_us - 500_000 if observed else None,
        "post_observation_ts_us": event_us + 500_000 if observed else None,
        "pre_mid_e6": 400_000 if observed else None,
        "post_mid_e6": 410_000 if observed else None,
        "pre_spread_e6": 20_000 if observed else None,
        "post_spread_e6": 10_000 if observed else None,
        "pre_depth_e2": 1_000 if observed else None,
        "post_depth_e2": 1_200 if observed else None,
        "l1_pre_rows": 1 if observed else 0,
        "l1_post_rows": 1 if observed else 0,
        "l2_pre_rows": 1 if observed else 0,
        "l2_post_rows": 1 if observed else 0,
        "gap_rows": 0,
        "censor_reason": None if observed else {
            "CENSORED_GAP": "L2_SEQUENCE_GAP",
        }.get(status),
        "pre_book": _book(
            event_us - 500_000, 390_000, 410_000, 500, 500, l1_logical,
        ) if observed else None,
        "post_book": _book(
            event_us + 500_000, 405_000, 415_000, 600, 600, l1_logical,
        ) if observed else None,
    }]
    sources = {
        family: bounded._base_source_attestation(descriptor, family)
        for family in ("orderbooks_l1", "orderbooks_full")
    }
    partitions = _partition_receipts(sources, len(rows), _evidence_digests(rows))
    value = {
        "schema": bounded.IMPACT_SCHEMA, "state": "COMPLETE",
        "analysis_date": "2026-07-19",
        "mapping_sha256": mapping["mapping_sha256"],
        "source_gate_sha256": _sha("gate"),
        "base_binding_sha256": exact_base["binding_sha256"],
        "base_release_id": exact_base["release_id"],
        "base_manifest_exact_identity": exact_base["manifest_exact_identity"],
        "base_exact_object_set_sha256": exact_base["base_exact_set_sha256"],
        "source_attestations": sources,
        "source_attestation_set_sha256": bounded.canonical_sha256(sources),
        "producer_receipt": _producer_receipt(),
        "source_reader_attestation": _reader_attestation([
            row
            for family in bounded.IMPACT_SOURCE_FAMILIES
            for row in exact_base["families"][family]["objects"]
        ]),
        "partition_receipts": partitions,
        "partition_receipt_set_sha256": bounded.canonical_sha256(partitions),
        "l2_quality_gate_sha256": quality["gate_sha256"],
        "window_contract": {
            "pre_event_window_ms": 1_000, "post_event_window_ms": 1_000,
            "pre_interval": "[EVENT_MINUS_PRE,EVENT]",
            "post_interval": "(EVENT,EVENT_PLUS_POST]",
            "event_clock": "RFQ_EXCHANGE_CREATED_TS_US",
            "receive_clock": "RFQ_ENVELOPE_RECV_WALL_NS",
            "max_abs_exchange_receive_skew_us": (
                bounded.MAX_RFQ_CLOCK_ABS_SKEW_US
            ),
            "clock_tolerance_authority": (
                bounded.RFQ_CLOCK_TOLERANCE_AUTHORITY
            ),
            "cross_date_borrow": False,
        },
        "observation_count": 1, "observations": rows,
        "observations_sha256": bounded.canonical_sha256(rows),
    }
    value["adapter_sha256"] = bounded.canonical_sha256(value)
    return value


def _resign_impact(value):
    value["observations_sha256"] = bounded.canonical_sha256(
        value["observations"]
    )
    value["adapter_sha256"] = bounded.canonical_sha256({
        key: item for key, item in value.items() if key != "adapter_sha256"
    })
    return value


def test_d07_adapter_requires_exact_mapping_and_censor_semantics():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    value = _impact(mapping, descriptor, exact_base, quality)
    kwargs = {
        "exact_base": exact_base, "l2_quality_gate": quality,
        "rfq_events": events, "producer_receipt": _producer_receipt(),
    }
    assert bounded._validate_impact_adapter(
        value, descriptor, mapping, **kwargs,
    ) == value
    missing = copy.deepcopy(value)
    missing["observations"] = []
    missing["observation_count"] = 0
    missing["observations_sha256"] = bounded.canonical_sha256([])
    missing["partition_receipts"] = _partition_receipts(
        missing["source_attestations"], 0,
    )
    missing["partition_receipt_set_sha256"] = bounded.canonical_sha256(
        missing["partition_receipts"]
    )
    missing["adapter_sha256"] = bounded.canonical_sha256({
        key: item for key, item in missing.items() if key != "adapter_sha256"
    })
    with pytest.raises(bounded.FreshRfqResearchError, match="COVERAGE"):
        bounded._validate_impact_adapter(missing, descriptor, mapping, **kwargs)
    bad_censor = _impact(
        mapping, descriptor, exact_base, quality, status="CENSORED_GAP",
    )
    with pytest.raises(bounded.FreshRfqResearchError, match="lacks gap"):
        bounded._validate_impact_adapter(
            bad_censor, descriptor, mapping, **kwargs,
        )


def test_d07_rejects_future_pre_state_and_forged_exact_source():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    kwargs = {
        "exact_base": exact_base, "l2_quality_gate": quality,
        "rfq_events": events, "producer_receipt": _producer_receipt(),
    }
    future_pre = _impact(mapping, descriptor, exact_base, quality)
    future_pre["observations"][0]["pre_observation_ts_us"] = (
        future_pre["observations"][0]["event_ts_us"] + 1
    )
    _resign_impact(future_pre)
    with pytest.raises(bounded.FreshRfqResearchError, match="invalid observed"):
        bounded._validate_impact_adapter(
            future_pre, descriptor, mapping, **kwargs,
        )

    forged = _impact(mapping, descriptor, exact_base, quality)
    forged["source_attestations"]["orderbooks_l1"]["exact_objects"][0][
        "version_id"
    ] = "forged-latest"
    forged["source_attestation_set_sha256"] = bounded.canonical_sha256(
        forged["source_attestations"]
    )
    _resign_impact(forged)
    with pytest.raises(bounded.FreshRfqResearchError, match="BINDING"):
        bounded._validate_impact_adapter(
            forged, descriptor, mapping, **kwargs,
        )


def test_d07_future_exchange_clock_must_be_exclusively_clock_censored():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    value = _impact(mapping, descriptor, exact_base, quality)
    row = value["observations"][0]
    recv_ns = (row["event_ts_us"] - bounded.MAX_RFQ_CLOCK_ABS_SKEW_US - 1) * 1_000
    events["r1"]["create_recv_wall_ns"] = recv_ns
    row["rfq_recv_wall_ns"] = recv_ns
    row["rfq_clock_skew_us"] = recv_ns // 1_000 - row["event_ts_us"]
    _resign_impact(value)
    with pytest.raises(bounded.FreshRfqResearchError, match="must be CENSORED_CLOCK"):
        bounded._validate_impact_adapter(
            value, descriptor, mapping, exact_base=exact_base,
            l2_quality_gate=quality, rfq_events=events,
            producer_receipt=_producer_receipt(),
        )

    for field in (
        "pre_observation_ts_us", "post_observation_ts_us", "pre_mid_e6",
        "post_mid_e6", "pre_spread_e6", "post_spread_e6",
        "pre_depth_e2", "post_depth_e2", "pre_book", "post_book",
    ):
        row[field] = None
    for field in (
        "l1_pre_rows", "l1_post_rows", "l2_pre_rows", "l2_post_rows",
        "gap_rows",
    ):
        row[field] = 0
    row["status"] = "CENSORED_CLOCK"
    row["censor_reason"] = "FUTURE_EXCHANGE_CLOCK"
    # No book evidence remains, so the anchored partition receipts must
    # carry the empty evidence row-set digest.
    value["partition_receipts"] = _partition_receipts(
        value["source_attestations"], 1,
    )
    value["partition_receipt_set_sha256"] = bounded.canonical_sha256(
        value["partition_receipts"]
    )
    _resign_impact(value)
    assert bounded._validate_impact_adapter(
        value, descriptor, mapping, exact_base=exact_base,
        l2_quality_gate=quality, rfq_events=events,
        producer_receipt=_producer_receipt(),
    ) == value


REAL_L2_RECEIPTS = Path(__file__).parent / "data"


def _canonical_l2_receipt(date, **overrides):
    receipt = {
        "schema_version": "l2-gap-receipt-v1",
        "date": date,
        "generated_at_utc": "2026-07-14T04:05:06Z",
        "raw_root": "/srv/kalshi/raw",
        "files": ["l2_23.ndjson"],
        "file_inventory": [
            {"file": f"date={date}/l2_23.ndjson", "bytes": 19},
        ],
        "no_l2_files": False,
        "lines": 4,
        "parse_errors": 0,
        "sids_total": 1,
        "sids_with_seq_gaps": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "seq_regressions": 0,
        "stream_restarts": 0,
        "recorder_markers": {},
        "markers_lost_frames": 0,
        "snapshot_re_anchors_total": 0,
        "per_market": {"KX-A": {"msgs": 4, "snapshots": 1, "re_anchors": 0}},
    }
    receipt.update(overrides)
    return receipt


def _retarget_dates(value, old, new):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_retarget_dates(item, old, new) for item in value]
    if isinstance(value, dict):
        return {
            _retarget_dates(key, old, new): _retarget_dates(item, old, new)
            for key, item in value.items()
        }
    return value


def _quality_fixture(receipt=None, *, body=None, date=None):
    """Exact base manifest + attested quality receipt chain.

    ``body`` may be the byte-exact content of a REAL sealed production
    receipt; ``date`` retargets the synthetic base release so a real
    receipt for another date binds to a same-date exact release.
    """
    date = date or base_fixture.DATE
    if body is None:
        if receipt is None:
            receipt = _canonical_l2_receipt(date)
        body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    sha = hashlib.sha256(body).hexdigest()
    manifest = base_fixture._complete_manifest()
    if date != base_fixture.DATE:
        manifest = _retarget_dates(manifest, base_fixture.DATE, date)
        manifest["publication_state"]["canonical_receipt_binding_sha256"] = (
            bounded.canonical_sha256(
                manifest["canonical_receipt"]["receipt_object"]
            )
        )
    row = next(
        item for item in manifest["objects"]
        if item["kind"] == "l2_quality_receipt"
    )
    row["sha256"] = sha
    row["size"] = len(body)
    row["source_key"] = (
        "ec2/control/quality/v1/date=%s/l2_gaps/sha256=%s/l2_gaps.json"
        % (date, sha)
    )
    base_fixture._retarget_manifest(manifest)
    raw, _manifest_identity = base_fixture._raw_and_identity(manifest)
    identity = {
        "bucket": row["source_bucket"], "key": row["source_key"],
        "version_id": row["source_version_id"], "size": len(body),
        "sha256": sha,
    }
    client = LocalExactClient({
        (identity["key"], identity["version_id"]): body,
    })
    session = exact_reader.ExactReadSession(
        [identity], client, transport_kind="INJECTED_EXACT_VERSION_CLIENT",
    )
    with session:
        with session.open_exact(identity):
            pass
    return raw, identity, body, session.attestation, manifest


def _build_quality_gate(raw, identity, body, attestation, date=None):
    return bounded.build_l2_quality_gate(
        analysis_date=date or base_fixture.DATE, exact_identity=identity,
        body=body, reader_attestation=attestation,
        base_manifest_bytes=raw,
    )


def test_l2_quality_gate_requires_canonical_attested_base_bound_receipt():
    raw, identity, body, attestation, manifest = _quality_fixture()
    gate = _build_quality_gate(raw, identity, body, attestation)
    assert gate["state"] == "PASS"
    assert gate["schema"] == "fresh-rfq-d07-l2-quality-gate-v2"
    assert gate["receipt_schema_version"] == "l2-gap-receipt-v1"
    assert gate["base_release_id"] == manifest["release_id"]
    assert gate["logical_key"] == (
        f"control/quality/v1/date={base_fixture.DATE}/l2_gaps.json"
    )
    assert gate["reader_attestation_sha256"] == (
        attestation["attestation_sha256"]
    )
    assert gate["salvage_allowed"] is False

    with pytest.raises(bounded.FreshRfqResearchError, match="SHA-256 differs"):
        _build_quality_gate(raw, identity, body[:-2] + b"x\n", attestation)

    refused_raw, refused_identity, refused_body, refused_att, _m = (
        _quality_fixture(_canonical_l2_receipt(
            base_fixture.DATE, seq_gap_events=1, seq_missed_total=3,
            sids_with_seq_gaps=1,
        ))
    )
    refused = _build_quality_gate(
        refused_raw, refused_identity, refused_body, refused_att,
    )
    assert refused["state"] == "REFUSED"
    assert refused["blockers"] == [
        "seq_gap_events=1", "seq_missed_total=3",
    ]


def test_l2_quality_gate_accepts_real_production_receipts():
    """Byte-exact REAL sealed receipts must satisfy the canonical gate.

    Guards against schema drift from reality (audit F1): the required field
    set is asserted against a real production receipt, a clean real date
    must reach state PASS, and a real gap date must be REFUSED with its
    actual counters, never a schema error.
    """
    real_clean = (REAL_L2_RECEIPTS / "l2_gaps_2026-07-12.json").read_bytes()
    parsed = json.loads(real_clean.decode("utf-8"))
    # Tripwire: canonical schema == the real writer's field set, exactly.
    assert set(parsed) == bounded.L2_QUALITY_RECEIPT_FIELDS
    raw, identity, body, attestation, manifest = _quality_fixture(
        body=real_clean, date="2026-07-12",
    )
    gate = _build_quality_gate(
        raw, identity, body, attestation, date="2026-07-12",
    )
    assert gate["state"] == "PASS"
    assert gate["blockers"] == []
    assert gate["lines"] == parsed["lines"]
    assert gate["base_release_id"] == manifest["release_id"]

    real_gap = (REAL_L2_RECEIPTS / "l2_gaps_2026-07-13.json").read_bytes()
    raw, identity, body, attestation, _m = _quality_fixture(body=real_gap)
    refused = _build_quality_gate(raw, identity, body, attestation)
    assert refused["state"] == "REFUSED"
    assert refused["blockers"] == ["seq_gap_events=1", "seq_missed_total=8"]


def test_l2_quality_gate_fails_closed_on_missing_extra_or_mistyped_fields():
    missing = _canonical_l2_receipt(base_fixture.DATE)
    missing.pop("seq_missed_total")
    raw, identity, body, attestation, _m = _quality_fixture(missing)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_L2_QUALITY_SCHEMA",
    ):
        _build_quality_gate(raw, identity, body, attestation)

    for bad in (
        _canonical_l2_receipt(base_fixture.DATE, seq_regressions="0"),
        _canonical_l2_receipt(base_fixture.DATE, parse_errors=False),
        _canonical_l2_receipt(base_fixture.DATE, forgiven=True),
        _canonical_l2_receipt(
            base_fixture.DATE, recorder_markers={"loss": "many"},
        ),
        _canonical_l2_receipt(base_fixture.DATE, no_l2_files=True),
        _canonical_l2_receipt(base_fixture.DATE, generated_at_utc=20260714),
        _canonical_l2_receipt(
            base_fixture.DATE, generated_at_utc="2026-07-14 04:05:06",
        ),
        _canonical_l2_receipt(
            base_fixture.DATE, generated_at_utc="2026-07-14T04:05:06.5Z",
        ),
    ):
        raw, identity, body, attestation, _m = _quality_fixture(bad)
        with pytest.raises(
            bounded.FreshRfqResearchError, match="D07_L2_QUALITY_SCHEMA",
        ):
            _build_quality_gate(raw, identity, body, attestation)

    marker_loss = _canonical_l2_receipt(
        base_fixture.DATE, recorder_markers={"loss": 2},
        markers_lost_frames=2,
    )
    raw, identity, body, attestation, _m = _quality_fixture(marker_loss)
    refused = _build_quality_gate(raw, identity, body, attestation)
    assert refused["state"] == "REFUSED"
    assert refused["blockers"] == [
        "markers_lost_frames=2", "recorder_marker:loss=2",
    ]


def test_l2_quality_gate_rejects_substituted_object_and_forged_attestation():
    raw, identity, body, attestation, _m = _quality_fixture()
    substituted = {**identity, "version_id": "forged-other-version"}
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_L2_QUALITY_BASE_BINDING",
    ):
        _build_quality_gate(raw, substituted, body, attestation)

    unsigned_tamper = copy.deepcopy(attestation)
    unsigned_tamper["module_write_api_call_count"] = 1
    with pytest.raises(
        bounded.FreshRfqResearchError, match="DIGEST_MISMATCH",
    ):
        _build_quality_gate(raw, identity, body, unsigned_tamper)

    resigned_tamper = copy.deepcopy(attestation)
    resigned_tamper["objects"] = [
        {**resigned_tamper["objects"][0], "version_id": "forged-latest"},
    ]
    resigned_tamper["attestation_sha256"] = bounded.canonical_sha256({
        key: item for key, item in resigned_tamper.items()
        if key != "attestation_sha256"
    })
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_L2_QUALITY_ATTESTATION",
    ):
        _build_quality_gate(raw, identity, body, resigned_tamper)


def _preflight_descriptor():
    return {
        "date": "2026-07-19",
        "analysis_rfq_objects": [
            {"key": "k", "version_id": "v", "size": 10, "sha256": _sha("x")},
        ],
        "manifest": {
            "request_provenance": {
                "rfq_created_occurrence_count": 1,
                "rfq_created_unique_count": 1,
            },
        },
    }


def test_resource_preflight_has_hard_event_and_disk_bounds(tmp_path, monkeypatch):
    descriptor = _preflight_descriptor()
    receipt = bounded._resource_preflight(
        [descriptor], tmp_path, tmp_path / "exact-temp",
    )
    assert receipt["resume_granularity"] == "COMPLETE_EXACT_DATE_STAGE_ONLY"
    assert receipt["checkpoint_required_free_bytes"] == (
        10 * bounded.RFQ_CHECKPOINT_EXPANSION_FACTOR
        + bounded.RFQ_DUCKDB_SPILL_CAP_BYTES
        + bounded.RFQ_CHECKPOINT_RESERVE_BYTES
    )
    assert receipt["exact_temp_required_free_bytes"] == (
        10 + bounded.RFQ_EXACT_TEMP_RESERVE_BYTES
    )
    assert receipt["duckdb_spill_cap_bytes"] == (
        bounded.RFQ_DUCKDB_SPILL_CAP_BYTES
    )
    assert receipt["checkpoint_expansion_factor"] == 8
    assert "8x" in receipt["checkpoint_expansion_justification"]
    assert receipt["exact_temp_max_active_objects"] == 1

    monkeypatch.setattr(
        bounded.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    with pytest.raises(bounded.FreshRfqResearchError, match="filesystem needs"):
        bounded._resource_preflight([descriptor], tmp_path)

    too_many = copy.deepcopy(descriptor)
    too_many["manifest"]["request_provenance"][
        "rfq_created_occurrence_count"
    ] = bounded.request_provenance.MAX_ACCUMULATED_RFQ_OCCURRENCES + 1
    with pytest.raises(bounded.FreshRfqResearchError, match="event counts"):
        bounded._resource_preflight([too_many], tmp_path)


def test_resource_preflight_covers_exact_temp_demand_on_shared_filesystem(
    tmp_path, monkeypatch,
):
    descriptor = _preflight_descriptor()
    baseline = bounded._resource_preflight(
        [descriptor], tmp_path, tmp_path / "exact-temp",
    )
    assert baseline["checkpoint_and_temp_share_filesystem"] is True
    combined = (
        baseline["checkpoint_required_free_bytes"]
        + baseline["exact_temp_required_free_bytes"]
    )

    monkeypatch.setattr(
        bounded.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": combined - 1})(),
    )
    # One byte below the combined checkpoint+exact-temp demand must fail:
    # the exact-temp staging demand is part of the same filesystem budget.
    with pytest.raises(bounded.FreshRfqResearchError, match="filesystem needs"):
        bounded._resource_preflight(
            [descriptor], tmp_path, tmp_path / "exact-temp",
        )

    monkeypatch.setattr(
        bounded.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": combined})(),
    )
    receipt = bounded._resource_preflight(
        [descriptor], tmp_path, tmp_path / "exact-temp",
    )
    assert receipt["state"] == "PASS"


def test_overlay_terminal_and_embedded_base_identity_must_match(
    tmp_path, monkeypatch,
):
    ready, authority, _manifest, _bodies = _overlay_cache(tmp_path, monkeypatch)
    value = json.loads(ready.read_text())
    value["base_terminal_file_sha256"] = _sha("other-terminal")
    value["ready_sha256"] = bounded.canonical_sha256({
        key: item for key, item in value.items() if key != "ready_sha256"
    })
    _write(ready, value)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="BASE_TERMINAL_MISMATCH",
    ):
        bounded.load_overlay_descriptor(ready, authority)


def test_exact_base_manifest_bytes_rebuild_embedded_binding(
    tmp_path, monkeypatch,
):
    ready, authority, _manifest, _bodies, base_raw = _overlay_cache(
        tmp_path, monkeypatch, return_base=True,
    )
    descriptor = bounded.load_overlay_descriptor(ready, authority)
    rebuilt = bounded._rebuild_exact_base(descriptor, base_raw)
    assert rebuilt == descriptor["base_binding"]
    with pytest.raises(bounded.FreshRfqResearchError, match="EXACT_REBUILD"):
        bounded._rebuild_exact_base(descriptor, base_raw + b" ")


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


def test_d07_recomputes_pre_post_values_from_attested_book_evidence():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    kwargs = {
        "exact_base": exact_base, "l2_quality_gate": quality,
        "rfq_events": events, "producer_receipt": _producer_receipt(),
    }
    value = _impact(mapping, descriptor, exact_base, quality)
    assert bounded._validate_impact_adapter(
        value, descriptor, mapping, **kwargs,
    ) == value

    forged_mid = _impact(mapping, descriptor, exact_base, quality)
    forged_mid["observations"][0]["pre_mid_e6"] = 401_000
    _resign_impact(forged_mid)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_RECOMPUTE",
    ):
        bounded._validate_impact_adapter(
            forged_mid, descriptor, mapping, **kwargs,
        )

    forged_depth = _impact(mapping, descriptor, exact_base, quality)
    forged_depth["observations"][0]["post_depth_e2"] = 1_300
    _resign_impact(forged_depth)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_RECOMPUTE",
    ):
        bounded._validate_impact_adapter(
            forged_depth, descriptor, mapping, **kwargs,
        )

    stray_book = _impact(mapping, descriptor, exact_base, quality)
    stray = stray_book["observations"][0]["post_book"]
    stray["source_logical_key"] = (
        "warehouse/facts/orderbooks_l1/date=2026-07-18/part.parquet"
    )
    # Re-bind the row hash so the unattested-object check itself is hit.
    stray["source_row_sha256"] = _rebind_book(stray)
    _resign_impact(stray_book)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_BOOK",
    ):
        bounded._validate_impact_adapter(
            stray_book, descriptor, mapping, **kwargs,
        )

    censored_with_books = _impact(
        mapping, descriptor, exact_base, quality, status="UNMAPPED",
    )
    row = censored_with_books["observations"][0]
    mapping_unmapped = copy.deepcopy(mapping)
    mapping_unmapped["mapping_rows"][0]["mapping_state"] = "MAPPED_L1_ONLY"
    row["censor_reason"] = "MAPPED_L1_ONLY"
    row["pre_book"] = _book(
        row["event_ts_us"] - 500_000, 390_000, 410_000, 500, 500,
        exact_base["families"]["orderbooks_l1"]["objects"][0]["logical_key"],
    )
    _resign_impact(censored_with_books)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="carries values",
    ):
        bounded._validate_impact_adapter(
            censored_with_books, descriptor, mapping_unmapped, **kwargs,
        )


def test_d07_requires_audited_producer_reader_attestation_and_partitions():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    kwargs = {
        "exact_base": exact_base, "l2_quality_gate": quality,
        "rfq_events": events, "producer_receipt": _producer_receipt(),
    }

    wrong_producer = _impact(mapping, descriptor, exact_base, quality)
    other = {
        "schema": bounded.D07_PRODUCER_SCHEMA, "state": "AUDITED_PASS",
        "algorithm_id": "d07-clob-impact-v2",
        "producer_module_sha256": _sha("other-module"),
        "audit_receipt_sha256": _sha("other-audit"),
    }
    other["receipt_sha256"] = bounded.canonical_sha256(other)
    wrong_producer["producer_receipt"] = other
    _resign_impact(wrong_producer)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_PRODUCER",
    ):
        bounded._validate_impact_adapter(
            wrong_producer, descriptor, mapping, **kwargs,
        )

    forged_attestation = _impact(mapping, descriptor, exact_base, quality)
    attestation = forged_attestation["source_reader_attestation"]
    attestation["objects"] = [
        {**attestation["objects"][0], "version_id": "forged-latest"},
        *attestation["objects"][1:],
    ]
    attestation["attestation_sha256"] = bounded.canonical_sha256({
        key: item for key, item in attestation.items()
        if key != "attestation_sha256"
    })
    _resign_impact(forged_attestation)
    with pytest.raises(
        bounded.FreshRfqResearchError,
        match="IMPACT_ADAPTER_SOURCE_ATTESTATION",
    ):
        bounded._validate_impact_adapter(
            forged_attestation, descriptor, mapping, **kwargs,
        )

    forged_partition = _impact(mapping, descriptor, exact_base, quality)
    receipt = forged_partition["partition_receipts"][0]
    receipt["component_row_count"] = 2
    receipt["receipt_sha256"] = bounded.canonical_sha256({
        key: item for key, item in receipt.items()
        if key != "receipt_sha256"
    })
    forged_partition["partition_receipt_set_sha256"] = (
        bounded.canonical_sha256(forged_partition["partition_receipts"])
    )
    _resign_impact(forged_partition)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_PARTITION",
    ):
        bounded._validate_impact_adapter(
            forged_partition, descriptor, mapping, **kwargs,
        )

    no_authority = _impact(mapping, descriptor, exact_base, quality)
    del no_authority["window_contract"]["clock_tolerance_authority"]
    _resign_impact(no_authority)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="IMPACT_ADAPTER_BINDING",
    ):
        bounded._validate_impact_adapter(
            no_authority, descriptor, mapping, **kwargs,
        )


def test_d07_blocks_without_producer_and_hard_fails_on_release_mismatch():
    descriptors = [{"date": "2026-07-19"}]
    quality = {
        "state": "PASS", "gate_sha256": _sha("quality"), "blockers": [],
        "base_release_id": "release-2026-07-19",
    }
    blocked = bounded._d07_result(
        descriptors, [], {"2026-07-19": {}},
        exact_bases={"2026-07-19": {"release_id": "release-2026-07-19"}},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events={}, producer_receipt=None, external_anchor=None,
    )
    assert blocked["status"] == "BLOCKED_PRODUCER_AUDIT_NOT_SUPPLIED"
    assert blocked["claim"] == "NO_RFQ_TO_CLOB_IMPACT_RESULT"
    assert blocked["contract"]["audited_producer_receipt_required"] is True
    assert blocked["contract"]["external_audit_anchor_required"] is True
    assert blocked["contract"]["pre_post_values_recomputed_from_book_evidence"] is True

    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_L2_QUALITY_RELEASE",
    ):
        bounded._d07_result(
            descriptors, [], {"2026-07-19": {}},
            exact_bases={"2026-07-19": {"release_id": "release-other"}},
            l2_quality_gates={"2026-07-19": quality},
            rfq_events={}, producer_receipt=_producer_receipt(),
            external_anchor=None,
        )


def test_run_rejects_l2_quality_without_exact_base_bytes(tmp_path, monkeypatch):
    ready, authority, _manifest, bodies = _overlay_cache(tmp_path, monkeypatch)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_L2_QUALITY_BASE_REQUIRED",
    ):
        bounded.run_bounded_fresh_rfq(
            overlay_ready_paths=[ready], fresh_authority=authority,
            client_factory=lambda _descriptor: LocalExactClient(bodies),
            checkpoint_root=tmp_path / "cp", hash_buckets=1,
            l2_quality_receipts_by_date={
                "2026-07-17": {
                    "exact_identity": {}, "body": b"",
                    "reader_attestation": {},
                },
            },
        )


def test_rfq_clock_tolerance_is_bound_to_method_registry_authority():
    assert bounded.MAX_RFQ_CLOCK_ABS_SKEW_US == methods.MAX_BOOK_AGE_US
    assert bounded.MAX_RFQ_CLOCK_ABS_SKEW_US == 5_000_000
    assert (
        "deep03_v3_methods.MAX_BOOK_AGE_US"
        in bounded.RFQ_CLOCK_TOLERANCE_AUTHORITY
    )


def _anchored_d07_context():
    mapping = _minimal_mapping()
    descriptor, exact_base, quality, events = _d07_context(mapping)
    producer = _producer_receipt()
    adapter = _impact(mapping, descriptor, exact_base, quality)
    anchor = _external_anchor(
        [producer["receipt_sha256"]],
        [adapter["source_reader_attestation"]["attestation_sha256"]],
        [adapter["partition_receipt_set_sha256"]],
    )
    return mapping, descriptor, exact_base, quality, events, producer, \
        adapter, anchor


def test_d07_anchored_full_path_produces_exploratory_observed():
    (mapping, descriptor, exact_base, quality, events, producer, adapter,
     anchor) = _anchored_d07_context()
    result = bounded._d07_result(
        [descriptor], [{"mapping": mapping}], {"2026-07-19": adapter},
        exact_bases={"2026-07-19": exact_base},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events=events,
        producer_receipt=producer,
        external_anchor=anchor,
    )
    assert result["status"] == "EXPLORATORY_OBSERVED"
    assert result["observed_components"] == 1
    assert result["producer_receipt_sha256"] == producer["receipt_sha256"]
    assert result["external_anchor_sha256"] == anchor["anchor_sha256"]
    assert result["component_status_conservation_pass"] is True
    assert result["unaligned_market_response"]["mid_change_e6"]["n"] == 1
    assert result["unaligned_market_response"]["mid_change_e6"]["p50"] == 10_000


def test_d07_refuses_offline_forged_evidence_chain():
    """Auditor forgeries I1/I2/I3: every trust-chain element must anchor."""
    (mapping, descriptor, exact_base, quality, events, producer, adapter,
     anchor) = _anchored_d07_context()
    kwargs = {
        "exact_base": exact_base, "l2_quality_gate": quality,
        "rfq_events": events, "producer_receipt": producer,
    }
    l1_logical = exact_base["families"]["orderbooks_l1"]["objects"][0][
        "logical_key"
    ]

    # I1: fabricated book state with an invented source_row_sha256 -- the
    # hash no longer binds the row's canonical bytes and is refused.
    i1 = _impact(mapping, descriptor, exact_base, quality)
    row = i1["observations"][0]
    row["pre_book"] = {
        "ts_us": row["pre_observation_ts_us"], "bid_e6": 100_000,
        "ask_e6": 900_000, "bid_depth_e2": 1, "ask_depth_e2": 1,
        "source_family": "orderbooks_l1", "source_logical_key": l1_logical,
        "source_row_sha256": "11" * 32,
    }
    row["pre_mid_e6"] = 500_000
    row["pre_spread_e6"] = 800_000
    row["pre_depth_e2"] = 2
    _resign_impact(i1)
    with pytest.raises(
        bounded.FreshRfqResearchError, match="does not bind",
    ):
        bounded._validate_impact_adapter(i1, descriptor, mapping, **kwargs)

    # I1b: the attacker re-binds the fabricated row's hash and re-signs the
    # partition receipts.  Adapter-level consistency then holds by
    # construction -- which is exactly why the external audit anchor is the
    # backstop: the mutated partition set digest is unanchored.
    i1b = copy.deepcopy(i1)
    book = i1b["observations"][0]["pre_book"]
    book["source_row_sha256"] = _rebind_book(book)
    i1b["partition_receipts"] = _partition_receipts(
        i1b["source_attestations"], 1,
        _evidence_digests(i1b["observations"]),
    )
    i1b["partition_receipt_set_sha256"] = bounded.canonical_sha256(
        i1b["partition_receipts"]
    )
    _resign_impact(i1b)
    assert bounded._validate_impact_adapter(
        i1b, descriptor, mapping, **kwargs,
    ) == i1b
    blocked = bounded._d07_result(
        [descriptor], [{"mapping": mapping}], {"2026-07-19": i1b},
        exact_bases={"2026-07-19": exact_base},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events=events, producer_receipt=producer,
        external_anchor=anchor,
    )
    assert blocked["status"] == "BLOCKED_UNANCHORED_EVIDENCE"
    assert any(
        entry.startswith("2026-07-19:partition_receipt_set:")
        for entry in blocked["unanchored_evidence"]
    )

    # I2: a self-signed producer receipt with arbitrary SHAs and
    # state=AUDITED_PASS is not audited evidence -- it is unanchored.
    forged_producer = {
        "schema": bounded.D07_PRODUCER_SCHEMA, "state": "AUDITED_PASS",
        "algorithm_id": "totally-unaudited-algo",
        "producer_module_sha256": _sha("forged-module"),
        "audit_receipt_sha256": _sha("forged-audit"),
    }
    forged_producer["receipt_sha256"] = bounded.canonical_sha256(
        forged_producer
    )
    forged_adapter = _impact(mapping, descriptor, exact_base, quality)
    forged_adapter["producer_receipt"] = forged_producer
    _resign_impact(forged_adapter)
    blocked = bounded._d07_result(
        [descriptor], [{"mapping": mapping}], {"2026-07-19": forged_adapter},
        exact_bases={"2026-07-19": exact_base},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events=events, producer_receipt=forged_producer,
        external_anchor=anchor,
    )
    assert blocked["status"] == "BLOCKED_UNANCHORED_EVIDENCE"
    assert (
        f"producer_receipt:{forged_producer['receipt_sha256']}"
        in blocked["unanchored_evidence"]
    )

    # I3: an offline-synthesized reader attestation (internally consistent,
    # no reads performed) has a digest the independent audit never anchored.
    i3 = _impact(mapping, descriptor, exact_base, quality)
    synthetic = i3["source_reader_attestation"]
    synthetic["ephemeral_files_created"] = 99
    synthetic["attestation_sha256"] = bounded.canonical_sha256({
        key: item for key, item in synthetic.items()
        if key != "attestation_sha256"
    })
    _resign_impact(i3)
    blocked = bounded._d07_result(
        [descriptor], [{"mapping": mapping}], {"2026-07-19": i3},
        exact_bases={"2026-07-19": exact_base},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events=events, producer_receipt=producer,
        external_anchor=anchor,
    )
    assert blocked["status"] == "BLOCKED_UNANCHORED_EVIDENCE"
    assert any(
        entry.startswith("2026-07-19:source_reader_attestation:")
        for entry in blocked["unanchored_evidence"]
    )

    # No anchor at all: self-consistent objects are never proof.
    blocked = bounded._d07_result(
        [descriptor], [{"mapping": mapping}], {"2026-07-19": adapter},
        exact_bases={"2026-07-19": exact_base},
        l2_quality_gates={"2026-07-19": quality},
        rfq_events=events, producer_receipt=producer,
        external_anchor=None,
    )
    assert blocked["status"] == "BLOCKED_UNANCHORED_EVIDENCE"
    assert "self-consistent" in blocked["detail"]


def test_external_anchor_requires_independent_audit_pass():
    anchor = _external_anchor([_sha("p")], [_sha("a")], [_sha("s")])
    assert bounded._validate_d07_external_anchor(anchor) == anchor

    self_declared = {**anchor, "state": "SELF_DECLARED"}
    self_declared["anchor_sha256"] = bounded.canonical_sha256({
        key: item for key, item in self_declared.items()
        if key != "anchor_sha256"
    })
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_ANCHOR_INVALID",
    ):
        bounded._validate_d07_external_anchor(self_declared)

    tampered = {**anchor, "producer_receipt_sha256s": [_sha("other")]}
    with pytest.raises(
        bounded.FreshRfqResearchError, match="DIGEST_MISMATCH",
    ):
        bounded._validate_d07_external_anchor(tampered)

    unnamed = {**anchor, "audit_authority": " "}
    unnamed["anchor_sha256"] = bounded.canonical_sha256({
        key: item for key, item in unnamed.items()
        if key != "anchor_sha256"
    })
    with pytest.raises(
        bounded.FreshRfqResearchError, match="D07_ANCHOR_INVALID",
    ):
        bounded._validate_d07_external_anchor(unnamed)
