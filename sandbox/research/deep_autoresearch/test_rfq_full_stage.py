import importlib.util
import hashlib
import json
import datetime as dt
from pathlib import Path

import duckdb
import pytest


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


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def quarantine_fixture(tmp_path):
    """Build a tiny exact-binding analogue using the known production failure key."""
    cache = tmp_path / "cache"
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()
    base_ns = int(dt.datetime(
        2026, 7, 14, tzinfo=dt.timezone.utc
    ).timestamp() * 1_000_000_000)
    known_key = "raw_rfq/date=2026-07-14/rfq_00.ndjson"
    pre_key = "raw_rfq/date=2026-07-13/rfq_23.ndjson"
    next_key = known_key + ".1"
    receipt_key = "raw_rfq/date=2026-07-14/rfq_receipts_00.ndjson"
    other_key = "raw_rfq/date=2026-07-12/rfq_00.ndjson"
    bodies = {
        other_key: json.dumps(recorder(
            base_ns - 86_400_000_000_000, {"type": "subscribed", "sid": 7}
        )) + "\n",
        pre_key: "\n".join([
            json.dumps(recorder(base_ns + 1_000_000_000, frame(
                "rfq_created", "CROSS-GAP", "M-A", "2026-07-14T00:00:01Z"
            ))),
            json.dumps(recorder(
                base_ns + 2_000_000_000, {"type": "subscribed", "sid": 7}
            )),
        ]) + "\n",
        # Deliberately invalid outer NDJSON. A successful scan proves this path was not opened.
        known_key: '{"recv_wall_ns":123,"raw":"unterminated"\n',
        next_key: "\n".join([
            json.dumps(recorder(base_ns + 7_204_000_000_000, frame(
                "rfq_deleted", "CROSS-GAP", "M-A", "2026-07-14T02:00:04Z"
            ))),
            json.dumps(recorder(base_ns + 7_204_500_000_000, frame(
                "rfq_created", "POST-GAP", "M-B", "2026-07-14T02:00:04.5Z"
            ))),
            json.dumps(recorder(base_ns + 7_205_000_000_000, frame(
                "rfq_deleted", "POST-GAP", "M-B", "2026-07-14T02:00:05Z"
            ))),
        ]) + "\n",
        receipt_key: json.dumps(recorder(
            base_ns + 3_600_000_000_000,
            {"status": "PASS", "subscription_proven": True,
             "boundary_closed": True, "end_reason": "boundary", "findings": []},
            marker="segment_receipt",
        )) + "\n",
    }
    release_keys = {
        mod.RELEASE_IDS[0]: [other_key],
        mod.RELEASE_IDS[1]: [pre_key, known_key, next_key, receipt_key],
    }
    manifest_sha = {}
    version_paths = {}
    object_rows = {}
    for index, release_id in enumerate(mod.RELEASE_IDS):
        base = cache / "releases" / release_id
        rows = []
        versions = []
        for key in release_keys[release_id]:
            body = bodies[key]
            target = base / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            row = {
                "key": key,
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "size": len(body.encode()),
            }
            rows.append(row)
            object_rows[key] = row
            versions.append({**row, "version_id": f"fixture-version-{index}-{len(rows)}"})
        write_json(base / ".VERIFIED.json", {
            "release_id": release_id,
            "version_binding_mode": "VERSION_BOUND",
            "evidence_tier": mod.EVIDENCE,
        })
        write_json(base / "MANIFEST.json", {"objects": rows})
        manifest_sha[release_id] = mod.sha256(base / "MANIFEST.json")
        version_path = f"DATA_INTEGRITY/version_ids/release-{index}.jsonl"
        version_paths[release_id] = version_path
        path = run_dir / version_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in versions), encoding="utf-8")

    selected = [{
        "release_id": release_id,
        "manifest_sha256": manifest_sha[release_id],
        "exact_version_list_path": version_paths[release_id],
    } for release_id in mod.RELEASE_IDS]
    execution_commit = "1" * 40
    manifest = {
        "selected_releases": selected,
        "repository": {
            "execution_commit": execution_commit,
            "source_manifest_sha256": "2" * 64,
            "query_set_sha256": "3" * 64,
        },
    }
    failed = object_rows[known_key]
    version_id = "fixture-version-1-2"
    receipt = {
        "schema_version": "rfq-malformed-object-receipt-v1",
        "run_id": run_dir.name,
        "release_id": mod.RELEASE_IDS[1],
        "key": known_key,
        "expected_size": failed["size"],
        "observed_size": failed["size"],
        "expected_sha256": failed["sha256"],
        "observed_sha256": failed["sha256"],
        "invalid_line_count": 1,
        "invalid_lines": [{
            "line_number": 1,
            "line_sha256": hashlib.sha256(bodies[known_key].encode()).hexdigest(),
            "error_type": "JSONDecodeError",
        }],
        "total_lines": 1,
        "raw_payload_redacted": True,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
    }
    write_json(run_dir / mod.MALFORMED_OBJECT_RECEIPT, receipt)
    declaration = {
        "schema_version": "rfq-object-quarantine-v1",
        "run_id": run_dir.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "remaining_object_parse_policy": mod.STRICT_REMAINING_PARSE_POLICY,
        "authority_basis": "Mission section 22 isolated-channel quarantine.",
        "selection_rule": "Exact manifest, VersionId, and receipt binding; whole object only.",
        "result_use_prohibited": "No row or result from the quarantined object.",
        "source_execution_commit": execution_commit,
        "quarantined_objects": [{
            "release_id": mod.RELEASE_IDS[1],
            "key": known_key,
            "size": failed["size"],
            "sha256": failed["sha256"],
            "version_id": version_id,
            "manifest_sha256": manifest_sha[mod.RELEASE_IDS[1]],
            "invalid_line_count": 1,
            "reason": "Strict parsing proved malformed bytes; whole object excluded.",
            "receipt": mod.MALFORMED_OBJECT_RECEIPT,
        }],
    }
    write_json(run_dir / mod.QUARANTINE_DECLARATION, declaration)
    return {
        "cache": cache,
        "run_dir": run_dir,
        "manifest": manifest,
        "declaration": declaration,
        "receipt": receipt,
        "known_key": known_key,
        "known_path": cache / "releases" / mod.RELEASE_IDS[1] / known_key,
        "next_path": cache / "releases" / mod.RELEASE_IDS[1] / next_key,
        "receipt_key": receipt_key,
        "receipt_path": cache / "releases" / mod.RELEASE_IDS[1] / receipt_key,
        "base_ns": base_ns,
    }


def synthetic_cycle1_binding(run_id="synthetic-run"):
    return {
        "active_path": mod.CYCLE1_DUCKDB_BINDING,
        "active_sha256": "1" * 64,
        "archived_path": mod.CYCLE1_DUCKDB_BINDING_ARCHIVE,
        "archived_sha256": "1" * 64,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run_id,
        "duckdb_path": f"/srv/w09-research/runs/{run_id}/cache/cycle1.duckdb",
        "duckdb_bytes": 4096,
        "duckdb_sha256": "2" * 64,
        "core_stage_resource_path": mod.CYCLE1_CORE_RESOURCE,
        "core_stage_resource_sha256": "3" * 64,
        "core_summary_path": mod.CYCLE1_CORE_SUMMARY,
        "core_summary_sha256": "4" * 64,
    }


def cycle1_binding_fixture(tmp_path):
    run_dir = tmp_path / "binding-run"
    database = run_dir / "cache/cycle1.duckdb"
    database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(database))
    connection.execute("CREATE TABLE preserved_core(value INTEGER)")
    connection.execute("INSERT INTO preserved_core VALUES (1)")
    connection.close()

    summary_path = run_dir / mod.CYCLE1_CORE_SUMMARY
    write_json(summary_path, {
        "run_id": run_dir.name,
        "banner": mod.BANNER,
        "boundary": "No result is promotion ready.",
    })
    resource_path = run_dir / mod.CYCLE1_CORE_RESOURCE
    write_json(resource_path, {
        "schema_version": "w09-stage-resource-v1",
        "label": "cycle1_core",
        "return_code": 0,
        "command": ["python", "run_cycle1.py", "--run-dir", str(run_dir)],
    })
    receipt = {
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run_dir.name,
        "path": str(database.resolve()),
        "bytes": database.stat().st_size,
        "sha256": mod.sha256(database),
        "mtime_utc": dt.datetime.fromtimestamp(
            database.stat().st_mtime, tz=dt.timezone.utc
        ).isoformat(),
        "duckdb_version": mod.EXPECTED_DUCKDB,
        "created_before_rfq_repair_registration": True,
        "core_result_disposition": (
            "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        ),
        "core_summary_path": mod.CYCLE1_CORE_SUMMARY,
        "core_summary_sha256": mod.sha256(summary_path),
        "core_stage_resource_path": mod.CYCLE1_CORE_RESOURCE,
        "core_stage_resource_sha256": mod.sha256(resource_path),
    }
    active = run_dir / mod.CYCLE1_DUCKDB_BINDING
    archived = run_dir / mod.CYCLE1_DUCKDB_BINDING_ARCHIVE
    write_json(active, receipt)
    write_json(archived, receipt)
    binding = {
        "active_path": mod.CYCLE1_DUCKDB_BINDING,
        "active_sha256": mod.sha256(active),
        "archived_path": mod.CYCLE1_DUCKDB_BINDING_ARCHIVE,
        "archived_sha256": mod.sha256(archived),
        "schema_version": receipt["schema_version"],
        "run_id": receipt["run_id"],
        "duckdb_path": receipt["path"],
        "duckdb_bytes": receipt["bytes"],
        "duckdb_sha256": receipt["sha256"],
        "core_stage_resource_path": receipt["core_stage_resource_path"],
        "core_stage_resource_sha256": receipt["core_stage_resource_sha256"],
        "core_summary_path": receipt["core_summary_path"],
        "core_summary_sha256": receipt["core_summary_sha256"],
    }
    manifest = {
        "data_integrity_repairs": [{
            "repair_id": "repair-01",
            "finding": "MALFORMED_NDJSON_OBJECT",
            "cycle1_duckdb_binding": binding,
        }],
    }
    return {
        "run_dir": run_dir,
        "database": database,
        "active": active,
        "archived": archived,
        "receipt": receipt,
        "binding": binding,
        "manifest": manifest,
    }


def double_quarantine_contract_fixture(tmp_path):
    run_dir = tmp_path / "double-contract-run"
    run_dir.mkdir()
    release_id = mod.RELEASE_IDS[1]
    manifest_sha = "9" * 64
    rows = []
    specs = [
        ("raw_rfq/date=2026-07-13/rfq_23.ndjson.1", 101, "1" * 64),
        ("raw_rfq/date=2026-07-13/rfq_23.ndjson.2", 102, "2" * 64),
        ("raw_rfq/date=2026-07-14/rfq_00.ndjson", 103, "3" * 64),
        ("raw_rfq/date=2026-07-14/rfq_00.ndjson.1", 104, "4" * 64),
    ]
    for key, size, digest in specs:
        rows.append({
            "key": key, "size": size, "sha256": digest,
            "path": tmp_path / "cache" / release_id / key,
            "bound_release_ids": [release_id],
        })
    full_fp = mod._object_fingerprint(rows)
    inputs = {
        "release_ids": list(mod.RELEASE_IDS),
        "releases": [{"release_id": release_id, "manifest_sha256": manifest_sha}],
        "paths": [row["path"] for row in rows],
        "objects_detail": rows,
        "objects": 4,
        "logical_manifest_bindings": 4,
        "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "bytes": sum(row["size"] for row in rows),
        "path_size_fingerprint_sha256": full_fp,
    }
    version_path = "DATA_INTEGRITY/version_ids/double.jsonl"
    version_file = run_dir / version_path
    version_file.parent.mkdir(parents=True)
    version_rows = []
    for index, row in enumerate(rows):
        version_rows.append({
            "key": row["key"], "size": row["size"], "sha256": row["sha256"],
            "version_id": f"version-{index}",
        })
    version_file.write_text(
        "".join(json.dumps(row) + "\n" for row in version_rows), encoding="utf-8"
    )
    first_object = {
        "release_id": release_id,
        "key": rows[2]["key"], "size": rows[2]["size"],
        "sha256": rows[2]["sha256"], "version_id": "version-2",
        "manifest_sha256": manifest_sha, "invalid_line_count": 1,
        "reason": "repair-01 no line-level salvage",
        "receipt": mod.MALFORMED_OBJECT_RECEIPT,
    }
    second_object = {
        "release_id": release_id,
        "key": rows[1]["key"], "size": rows[1]["size"],
        "sha256": rows[1]["sha256"], "version_id": "version-1",
        "manifest_sha256": manifest_sha, "invalid_line_count": 1,
        "reason": "repair-02 no line-level salvage",
        "receipt": mod.MALFORMED_OBJECT_RECEIPT_02,
    }
    cumulative = [second_object, first_object]
    old_commit, new_commit = "a" * 40, "b" * 40
    auth = {
        "schema_version": "sports-autoresearch-repair-authorization-v1",
        "run_id": run_dir.name,
        "repair_id": "repair-02",
        "authorized_at_utc": "2026-07-15T14:40:39Z",
        "authority_source": "operator_chat_message",
        "authorized_action": mod.REPAIR02_AUTHORIZED_ACTION,
    }
    auth_path = run_dir / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
    write_json(auth_path, auth)
    declaration = {
        "schema_version": "rfq-object-quarantine-v2", "run_id": run_dir.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "authority_basis": "EXPLICIT_OPERATOR_AUTHORIZATION_REPAIR_02",
        "created_at_utc": "2026-07-15T14:42:31Z",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "source_execution_commit": old_commit,
        "parent_repair_id": "repair-01",
        "previous_declaration_path": mod.REPAIR_DECLARATION_ARCHIVE,
        "previous_declaration_sha256": "5" * 64,
        "authorization_evidence_path": "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json",
        "authorization_evidence_sha256": mod.sha256(auth_path),
        "newly_quarantined_objects": [second_object],
        "cumulative_quarantined_objects": cumulative,
        "remaining_object_parse_policy": mod.STRICT_REMAINING_PARSE_POLICY,
        "selection_rule": "Exclude exactly two objects.",
        "result_use_prohibited": "No result; no line-level salvage is permitted.",
        "trial_disposition_if_repair_fails": (
            "ABORT_WITHOUT_RFQ_RESULT; new preregistration required."
        ),
    }
    declaration_path = run_dir / mod.QUARANTINE_DECLARATION_02
    write_json(declaration_path, declaration)
    receipt = {
        "schema_version": "rfq-malformed-object-receipt-v1",
        "run_id": run_dir.name, "release_id": release_id,
        "key": second_object["key"], "expected_size": second_object["size"],
        "observed_size": second_object["size"],
        "expected_sha256": second_object["sha256"],
        "observed_sha256": second_object["sha256"],
        "manifest_sha256": manifest_sha, "version_id": "version-1",
        "invalid_line_count": 1,
        "invalid_lines": [{
            "line_number": 1, "line_sha256": "6" * 64,
            "error_type": "JSONDecodeError",
        }],
        "total_lines": 1, "raw_payload_redacted": True,
        "final_line_newline_terminated": True,
        "quarantine_authorized": False,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
    }
    receipt_path = run_dir / mod.MALFORMED_OBJECT_RECEIPT_02
    write_json(receipt_path, receipt)
    repair01 = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01", "current_execution_commit": old_commit,
        "current_source_manifest_sha256": "7" * 64,
        "current_query_set_sha256": "8" * 64,
        "declaration_sha256": "5" * 64,
        "receipt_sha256": "4" * 64,
        "quarantined_objects": [first_object],
    }
    retained = [rows[0], rows[3]]
    quarantined = [rows[1], rows[2]]
    selection = mod._repair02_selection_fingerprint(
        full_fp, retained, quarantined, mod.sha256(declaration_path),
        [repair01["receipt_sha256"], mod.sha256(receipt_path)],
    )
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": 4, "full_logical_manifest_bindings": 4,
        "full_unique_bytes": inputs["bytes"],
        "retained_unique_objects": 2, "retained_logical_manifest_bindings": 2,
        "retained_bytes": sum(row["size"] for row in retained),
        "quarantined_unique_objects": 2,
        "quarantined_logical_manifest_bindings": 2,
        "quarantined_bytes": sum(row["size"] for row in quarantined),
        "full_object_set_sha256": full_fp,
        "retained_object_set_sha256": mod._object_fingerprint(retained),
        "quarantined_object_set_sha256": mod._object_fingerprint(quarantined),
        "retained_selection_fingerprint_sha256": selection,
        "whole_object_quarantine": True, "line_salvage": False,
    }
    repair02 = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v2",
        "repair_id": "repair-02", "parent_repair_id": "repair-01",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED",
        "post_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        "previous_execution_commit": old_commit,
        "current_execution_commit": new_commit,
        "previous_source_manifest_sha256": "7" * 64,
        "current_source_manifest_sha256": "c" * 64,
        "previous_query_set_sha256": "8" * 64,
        "current_query_set_sha256": "d" * 64,
        "newly_quarantined_objects": [second_object],
        "cumulative_quarantined_objects": cumulative,
        "coverage": coverage,
    }
    manifest = {
        "status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        "selected_releases": [{
            "release_id": release_id, "manifest_sha256": manifest_sha,
            "exact_version_list_path": version_path,
        }],
        "repository": {
            "execution_commit": new_commit, "source_manifest_sha256": "c" * 64,
            "query_set_sha256": "d" * 64,
        },
        "data_integrity_repairs": [repair01, repair02],
    }
    first = {
        "quarantined_unique_objects": 1,
        "quarantine_details": [{
            **{key: first_object[key] for key in (
                "release_id", "key", "size", "sha256", "version_id",
                "manifest_sha256", "invalid_line_count", "reason",
            )},
            "receipt_sha256": repair01["receipt_sha256"],
            "declaration_sha256": repair01["declaration_sha256"],
        }],
        "failed_attempt_binding": {"repair_id": "repair-01"},
        "selection_fingerprint_sha256": "e" * 64,
    }
    return {
        "run_dir": run_dir, "inputs": inputs, "manifest": manifest,
        "first": first, "selection": selection, "second": second_object,
    }


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


def test_cycle1_duckdb_binding_validates_and_returns_exact_nested_identity(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    observed = mod.validate_cycle1_duckdb_binding(
        fixture["run_dir"], fixture["manifest"], fixture["database"]
    )
    assert observed == fixture["binding"]


def test_cycle1_duckdb_binding_rejects_database_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    with fixture["database"].open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(mod.RFQStageError, match="byte count|SHA-256"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_cycle1_duckdb_binding_rejects_same_size_database_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    with fixture["database"].open("r+b") as handle:
        handle.seek(-1, 2)
        original = handle.read(1)
        handle.seek(-1, 2)
        handle.write(bytes([original[0] ^ 0xFF]))
    assert fixture["database"].stat().st_size == fixture["receipt"]["bytes"]
    with pytest.raises(mod.RFQStageError, match="SHA-256"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_cycle1_duckdb_binding_rejects_receipt_or_manifest_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    active_receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    active_receipt["core_summary_sha256"] = "f" * 64
    write_json(fixture["active"], active_receipt)
    with pytest.raises(mod.RFQStageError, match="active/archived"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("core_summary_sha256", "core summary SHA"),
        ("core_stage_resource_sha256", "core resource receipt SHA"),
    ],
)
def test_cycle1_binding_rehashes_core_artifacts_even_if_receipts_are_reforged(
    tmp_path, field, message
):
    fixture = cycle1_binding_fixture(tmp_path)
    receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    receipt[field] = "f" * 64
    write_json(fixture["active"], receipt)
    write_json(fixture["archived"], receipt)
    forged_binding = dict(fixture["binding"])
    forged_binding.update({
        "active_sha256": mod.sha256(fixture["active"]),
        "archived_sha256": mod.sha256(fixture["archived"]),
        field: receipt[field],
    })
    fixture["manifest"]["data_integrity_repairs"][0][
        "cycle1_duckdb_binding"
    ] = forged_binding
    with pytest.raises(mod.RFQStageError, match=message):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )

    fixture = cycle1_binding_fixture(tmp_path / "manifest")
    fixture["manifest"]["data_integrity_repairs"][0][
        "cycle1_duckdb_binding"
    ]["duckdb_sha256"] = "f" * 64
    with pytest.raises(mod.RFQStageError, match="repair Cycle-1"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


@pytest.mark.parametrize("run_id", [None, "wrong-run"])
def test_cycle1_duckdb_binding_rejects_missing_or_mismatched_run_id(
    tmp_path, run_id
):
    fixture = cycle1_binding_fixture(tmp_path)
    receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    if run_id is None:
        receipt.pop("run_id")
    else:
        receipt["run_id"] = run_id
    write_json(fixture["active"], receipt)
    write_json(fixture["archived"], receipt)
    with pytest.raises(mod.RFQStageError, match="field set|identity mismatch"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_rfq_summary_rejects_absent_run_id_before_querying():
    with pytest.raises(mod.RFQStageError, match="summary run_id is missing"):
        mod.build_summary(
            None,
            {"cycle1_duckdb_binding": synthetic_cycle1_binding()},
            {},
            0.0,
        )


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


@pytest.mark.parametrize(
    "mutation",
    ["forged_sha", "unbound_version", "unbound_execution", "receipt_key", "zero_invalid"],
)
def test_quarantine_mutations_and_unbound_evidence_fail_closed(tmp_path, mutation):
    fixture = quarantine_fixture(tmp_path)
    declaration = json.loads(json.dumps(fixture["declaration"]))
    receipt = json.loads(json.dumps(fixture["receipt"]))
    if mutation == "forged_sha":
        declaration["quarantined_objects"][0]["sha256"] = "f" * 64
    elif mutation == "unbound_version":
        declaration["quarantined_objects"][0]["version_id"] = "forged-version"
    elif mutation == "unbound_execution":
        declaration["source_execution_commit"] = "9" * 40
    elif mutation == "receipt_key":
        receipt["key"] += ".forged"
    elif mutation == "zero_invalid":
        receipt["invalid_line_count"] = 0
        receipt["invalid_lines"] = []
    write_json(fixture["run_dir"] / mod.QUARANTINE_DECLARATION, declaration)
    write_json(fixture["run_dir"] / mod.MALFORMED_OBJECT_RECEIPT, receipt)
    discovered = mod.discover_inputs(fixture["cache"])
    with pytest.raises(mod.RFQStageError):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"], discovered
        )


def test_receipt_without_preregistered_declaration_is_rejected(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    (fixture["run_dir"] / mod.QUARANTINE_DECLARATION).unlink()
    with pytest.raises(mod.RFQStageError, match="requires both"):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"],
            mod.discover_inputs(fixture["cache"]),
        )


def test_registered_code_repair_binds_archived_failure_and_scratch_receipts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    run_dir = fixture["run_dir"]
    discovered_full_set = mod.discover_inputs(fixture["cache"])
    declaration_source = run_dir / mod.QUARANTINE_DECLARATION
    receipt_source = run_dir / mod.MALFORMED_OBJECT_RECEIPT
    archived_declaration = run_dir / mod.REPAIR_DECLARATION_ARCHIVE
    archived_receipt = run_dir / mod.REPAIR_RECEIPT_ARCHIVE
    archived_declaration.parent.mkdir(parents=True, exist_ok=True)
    archived_declaration.write_bytes(declaration_source.read_bytes())
    archived_receipt.write_bytes(receipt_source.read_bytes())
    failed_state = run_dir / mod.FAILED_STATE_ARCHIVE
    original_scratch = "/srv/w09/runs/test-run/cache/rfq_full_scratch.duckdb"
    failed_input_fingerprint = discovered_full_set["path_size_fingerprint_sha256"]
    write_json(failed_state, {
        "schema": "rfq-full-stage-state-v1",
        "status": "FAILED_RESUMABLE",
        "resume": False,
        "scratch": original_scratch,
        "input_fingerprint": failed_input_fingerprint,
        "error": f"Malformed JSON in {fixture['known_key']}",
    })
    failed_resource = run_dir / mod.FAILED_RESOURCE_ARCHIVE
    write_json(failed_resource, {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage",
        "return_code": 1,
        "command": ["python", "rfq_full_stage.py"],
    })
    failed_scratch = run_dir / mod.FAILED_SCRATCH_RECEIPT_ARCHIVE
    write_json(failed_scratch, {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run_dir.name,
        "original_scratch_path": original_scratch,
        "preserved_scratch_path": "/srv/w09/runs/test-run/cache/rfq_full_scratch.failed.duckdb",
        "sha256": "4" * 64,
        "bytes": 4096,
        "mtime_utc": "2026-07-15T13:20:32Z",
        "input_fingerprint": failed_input_fingerprint,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
    })
    failed_input_identity = run_dir / mod.FAILED_INPUT_IDENTITY_ARCHIVE
    write_json(
        failed_input_identity,
        mod._legacy_full_input_identity(discovered_full_set),
    )
    old_commit = fixture["declaration"]["source_execution_commit"]
    new_commit = "6" * 40
    repository = fixture["manifest"]["repository"]
    repository.update({
        "initial_execution_commit": old_commit,
        "previous_execution_commit": old_commit,
        "execution_commit": new_commit,
        "source_manifest_sha256": "7" * 64,
        "query_set_sha256": "8" * 64,
    })
    repair = {
        "repair_id": "repair-01",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "previous_execution_commit": old_commit,
        "current_execution_commit": new_commit,
        "previous_source_manifest_sha256": "2" * 64,
        "current_source_manifest_sha256": "7" * 64,
        "previous_query_set_sha256": "3" * 64,
        "current_query_set_sha256": "8" * 64,
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "data_integrity_handling_change": (
            "ONE_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
        ),
        "frozen_design_change": (
            "NO_HYPOTHESIS_DESIGN_CHANGE; DATA_INTEGRITY_HANDLING_CHANGED"
        ),
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "declaration_input_path": mod.QUARANTINE_DECLARATION,
        "declaration_path": mod.REPAIR_DECLARATION_ARCHIVE,
        "declaration_sha256": mod.sha256(archived_declaration),
        "receipt_input_path": mod.MALFORMED_OBJECT_RECEIPT,
        "receipt_path": mod.REPAIR_RECEIPT_ARCHIVE,
        "receipt_sha256": mod.sha256(archived_receipt),
        "failed_state_path": mod.FAILED_STATE_ARCHIVE,
        "failed_state_sha256": mod.sha256(failed_state),
        "failed_resource_receipt_path": mod.FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": mod.sha256(failed_resource),
        "failed_scratch_receipt_path": mod.FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_scratch_receipt_sha256": mod.sha256(failed_scratch),
        "failed_input_identity_path": mod.FAILED_INPUT_IDENTITY_ARCHIVE,
        "failed_input_identity_sha256": mod.sha256(failed_input_identity),
        "quarantined_objects": fixture["declaration"]["quarantined_objects"],
    }
    fixture["manifest"]["data_integrity_repairs"] = [repair]
    inputs = mod.apply_object_quarantine(
        run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
    )
    assert inputs["failed_attempt_binding"] == {
        "repair_id": "repair-01",
        "failed_state_path": mod.FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair["failed_state_sha256"],
        "failed_resource_receipt_path": mod.FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair["failed_resource_receipt_sha256"],
        "failed_scratch_receipt_path": mod.FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_scratch_receipt_sha256": repair["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": mod.FAILED_INPUT_IDENTITY_ARCHIVE,
        "failed_input_identity_sha256": repair["failed_input_identity_sha256"],
    }
    forged_failed_identity = mod._legacy_full_input_identity(discovered_full_set)
    forged_failed_identity["unique_objects"] += 1
    write_json(failed_input_identity, forged_failed_identity)
    repair["failed_input_identity_sha256"] = mod.sha256(failed_input_identity)
    with pytest.raises(mod.RFQStageError, match="manifest-derived full set"):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )
    write_json(
        failed_input_identity,
        mod._legacy_full_input_identity(discovered_full_set),
    )
    repair["failed_input_identity_sha256"] = mod.sha256(failed_input_identity)
    repair["hypothesis_design_change"] = "FORGED_DESIGN_CHANGE"
    with pytest.raises(
        mod.RFQStageError,
        match="RFQ structural repair binding mismatch: hypothesis_design_change",
    ):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )
    repair["hypothesis_design_change"] = "NONE"
    fixture["manifest"]["data_integrity_repairs"][0]["failed_state_sha256"] = "0" * 64
    with pytest.raises(mod.RFQStageError, match="failed state SHA"):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )


def test_quarantine_without_two_valid_gap_neighbors_is_rejected(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    discovered = mod.discover_inputs(fixture["cache"])
    pre_key = "raw_rfq/date=2026-07-13/rfq_23.ndjson"
    discovered["objects_detail"] = [
        row for row in discovered["objects_detail"] if row["key"] != pre_key
    ]
    with pytest.raises(mod.RFQStageError, match="preceding and following"):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"], discovered
        )


def test_manifest_bound_whole_object_quarantine_is_gap_safe(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    discovered = mod.discover_inputs(fixture["cache"])
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"], discovered
    )
    assert inputs["coverage_status"] == "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    assert inputs["full_object_coverage"] is False
    assert inputs["unique_objects_total"] == 5
    assert inputs["consumed_unique_objects"] == 4
    assert inputs["quarantined_unique_objects"] == 1
    assert inputs["quarantined_bytes"] == fixture["known_path"].stat().st_size
    assert mod.stage_completion_status(inputs) == (
        "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    )
    assert fixture["known_path"] not in inputs["paths"]
    assert all(row["key"] != fixture["known_key"] for row in inputs["objects_detail"])
    assert fixture["receipt_path"] in inputs["paths"]
    assert any(row["key"] == fixture["receipt_key"] for row in inputs["objects_detail"])

    connection = duckdb.connect()
    connection.execute("SET TimeZone='UTC'")
    # The excluded file is malformed outer NDJSON; success proves it was never opened.
    mod.build_scan_tables(
        connection, inputs["paths"], fixture["run_dir"].name,
        inputs["quarantine_boundaries"],
    )
    gap = connection.execute("""
      SELECT gap_start_ns,gap_end_ns FROM rfq_quarantine_gaps
    """).fetchone()
    assert gap == (
        fixture["base_ns"] + 2_000_000_001,
        fixture["base_ns"] + 7_204_000_000_000,
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_observation_boundaries
      WHERE contains(reason,'WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON')
    """).fetchone()[0] == 2

    mod.build_request_tables(connection, fixture["run_dir"].name)
    lifecycle = connection.execute("""
      SELECT endpoint_type,delete_observed,observation_boundary_reason
      FROM rfq_lifecycle_base WHERE rfq_id_hash=sha256('CROSS-GAP')
    """).fetchone()
    assert lifecycle[0] == "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY"
    assert lifecycle[1] is False
    assert "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON_GAP_START" in lifecycle[2]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1

    core_path = tmp_path / "quarantine-core.duckdb"
    core = duckdb.connect(str(core_path))
    core.execute("""
      CREATE TABLE universe(date DATE,market_ticker VARCHAR,sport VARCHAR,league VARCHAR,
        root_event_id VARCHAR,root_map_status VARCHAR,occurrence_datetime TIMESTAMPTZ,
        dim_effective_us BIGINT)
    """)
    base_us = fixture["base_ns"] // 1000
    core.executemany("""
      INSERT INTO universe VALUES (DATE '2026-07-14',?,'Soccer','L',?,
        'PROVISIONAL_HEURISTIC_MATCHUP_TIME',
        TIMESTAMPTZ '2026-07-14 03:00:00+00',?)
    """, [("M-A", "ROOT-A", base_us - 3_600_000_000),
           ("M-B", "ROOT-B", base_us - 3_600_000_000)])
    core.execute("""
      CREATE TABLE l1_real(market_ticker VARCHAR,t_us BIGINT,recv_mono_ns BIGINT,
        yes_bid_e4 INTEGER,yes_ask_e4 INTEGER,yes_bid_qty_e4 BIGINT,yes_ask_qty_e4 BIGINT)
    """)
    l1 = []
    for market, anchor in (("M-A", base_us + 1_000_000),
                           ("M-B", base_us + 7_204_500_000)):
        for offset_s in range(-700, 132):
            t_us = anchor + offset_s * 1_000_000
            l1.append((market, t_us, t_us * 1000, 3900, 4100, 100_000, 100_000))
    core.executemany("INSERT INTO l1_real VALUES (?,?,?,?,?,?,?)", l1)
    core.execute(
        "CREATE TABLE trades_safe(market_ticker VARCHAR,t_us BIGINT,"
        "taker_sign INTEGER,count_e4 BIGINT)"
    )
    core.execute("CREATE TABLE l2_all(market_ticker VARCHAR,t_us BIGINT)")
    core.execute("CREATE TABLE capture_gaps(start_us BIGINT,end_us BIGINT)")
    core.close()

    mod.attach_core_and_enrich(connection, core_path)
    mod.build_descriptive_tables(connection)
    # Both creates are in different observation segments: no interarrival crosses the gap.
    assert connection.execute("SELECT coalesce(sum(n),0) FROM rfq_interarrival").fetchone()[0] == 0
    missing_hour = connection.execute("""
      SELECT requests_in_consumed_objects,object_coverage_status,zero_interpretation
      FROM rfq_hour_coverage WHERE hour_start_us=?
    """, [base_us + 3_600_000_000]).fetchone()
    assert missing_hour == (
        0, "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP",
    )
    for table in (
        "rfq_flow_daily", "rfq_event_flow_hourly", "rfq_flow_hourly",
        "rfq_flow_minute", "rfq_burst_summary",
    ):
        assert "object_coverage_status" in {
            row[1] for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        }

    mod.build_clob_context(connection, max_per_root=50)
    invalid_context = connection.execute("""
      SELECT count(*) FROM rfq_clob_context
      WHERE market_ticker='M-B' AND endpoint_type='CREATE' AND cohort='RFQ'
        AND window_label='m1_0' AND NOT valid_receive_window
    """).fetchone()[0]
    assert invalid_context == 1, {
        "requests": connection.execute(
            "SELECT market_ticker,create_date,dimension_causality FROM rfq_requests"
        ).fetchall(),
        "anchors": connection.execute(
            "SELECT market_ticker,endpoint_type,anchor_role FROM rfq_clob_anchors"
        ).fetchall(),
        "context": connection.execute(
            "SELECT market_ticker,endpoint_type,cohort,window_label,valid_receive_window "
            "FROM rfq_clob_context_unmatched WHERE market_ticker='M-B'"
        ).fetchall(),
    }
    control = connection.execute("""
      SELECT prior_control_lookback_observed,future_control_outcome_observed,matched
      FROM rfq_control_balance
      WHERE market_ticker='M-B' AND endpoint_type='CREATE' AND anchor_role='REQUEST_MARKET'
    """).fetchone()
    assert control == (False, False, False)
    inputs["run_id"] = fixture["run_dir"].name
    inputs["cycle1_duckdb_binding"] = synthetic_cycle1_binding(
        fixture["run_dir"].name
    )
    summary = mod.build_summary(connection, inputs, {
        "candidate_anchor_markets": 0,
    }, 1.0)
    assert summary["run_id"] == fixture["run_dir"].name
    assert summary["input"]["cycle1_duckdb_binding"] == inputs[
        "cycle1_duckdb_binding"
    ]
    assert summary["status"] == "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    assert summary["analysis_scope"] == "DESCRIPTIVE_DISCOVERY_ONLY"
    assert summary["input"]["whole_object_quarantine"] is True
    assert summary["input"]["line_salvage"] is False
    assert summary["coverage"]["quarantine_gap_count"] == 1
    source_text = path.read_text(encoding="utf-8")
    assert "Full-scan RFQ" not in source_text
    assert "# Full RFQ exploratory stage" not in source_text
    connection.close()


def test_adjacent_double_quarantine_is_one_contiguous_gap_with_two_boundaries(
    tmp_path,
):
    base_ns = 10_000_000_000
    before = tmp_path / "raw_rfq/date=2026-07-13/rfq_23.ndjson.1"
    after = tmp_path / "raw_rfq/date=2026-07-14/rfq_00.ndjson.1"
    write_rows(before, [
        recorder(base_ns, frame(
            "rfq_created", "CROSS-DOUBLE-GAP", "M-A", "2026-07-13T23:59:59Z"
        )),
        recorder(base_ns + 1_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    write_rows(after, [
        recorder(base_ns + 8_000_000_000, frame(
            "rfq_deleted", "CROSS-DOUBLE-GAP", "M-A", "2026-07-14T00:00:07Z"
        )),
        recorder(base_ns + 9_000_000_000, frame(
            "rfq_created", "AFTER", "M-B", "2026-07-14T00:00:08Z"
        )),
    ])
    bad_keys = [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    boundary = {
        "release_id": mod.RELEASE_IDS[1],
        "key": bad_keys[0],
        "sha256": "1" * 64,
        "quarantined_keys": bad_keys,
        "quarantined_object_set_sha256": "2" * 64,
        "previous_path": before,
        "next_path": after,
    }
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [before, after], "double-gap", [boundary])
    gap = connection.execute("""
      SELECT key,quarantined_keys_json,quarantined_object_count,
        quarantined_object_set_sha256,gap_start_ns,gap_end_ns
      FROM rfq_quarantine_gaps
    """).fetchone()
    assert gap == (
        bad_keys[0],
        json.dumps(bad_keys, separators=(",", ":")),
        2,
        "2" * 64,
        base_ns + 1_000_000_001,
        base_ns + 8_000_000_000,
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_observation_boundaries
      WHERE contains(reason,'WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON')
    """).fetchone()[0] == 2
    mod.build_request_tables(connection, "double-gap")
    lifecycle = connection.execute("""
      SELECT endpoint_type,delete_observed,observation_boundary_reason
      FROM rfq_lifecycle_base
      WHERE rfq_id_hash=sha256('CROSS-DOUBLE-GAP')
    """).fetchone()
    assert lifecycle[0] == "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY"
    assert lifecycle[1] is False
    assert "_GAP_START" in lifecycle[2]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1
    connection.close()


def test_repair02_selection_fingerprint_is_deterministic_and_binds_evidence():
    rows = [
        {"key": "b", "size": 2, "sha256": "2" * 64},
        {"key": "a", "size": 1, "sha256": "1" * 64},
        {"key": "c", "size": 3, "sha256": "3" * 64},
    ]
    first = mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "d" * 64, ["a" * 64, "b" * 64]
    )
    assert first == mod._repair02_selection_fingerprint(
        "f" * 64, list(reversed(rows[1:])), rows[:1], "d" * 64,
        ["a" * 64, "b" * 64],
    )
    assert first != mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "e" * 64, ["a" * 64, "b" * 64]
    )
    assert first != mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "d" * 64, ["b" * 64, "a" * 64]
    )


def test_repair02_contract_excludes_exactly_two_adjacent_whole_objects(
    tmp_path, monkeypatch
):
    fixture = double_quarantine_contract_fixture(tmp_path)
    monkeypatch.setattr(
        mod, "apply_object_quarantine",
        lambda run_dir, manifest, inputs, _ignore_repair02=False: fixture["first"],
    )
    monkeypatch.setattr(
        mod, "_validate_repair02_archives",
        lambda *args: ([{"repair_id": "repair-01"}, {"repair_id": "repair-02"}],
                       [{"repair_id": "repair-01"}, {"repair_id": "repair-02"}]),
    )
    result = mod._apply_double_object_quarantine(
        fixture["run_dir"], fixture["manifest"], fixture["inputs"]
    )
    assert result["consumed_unique_objects"] == 2
    assert result["consumed_logical_bindings"] == 2
    assert result["quarantined_unique_objects"] == 2
    assert result["quarantined_logical_bindings"] == 2
    assert result["selection_fingerprint_sha256"] == fixture["selection"]
    assert [row["key"] for row in result["quarantine_details"]] == [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    assert len(result["quarantine_boundaries"]) == 1
    assert result["quarantine_boundaries"][0]["quarantined_keys"] == [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    assert result["expected_success_resource"] == {
        "label": "rfq_full_stage_repair02",
        "path": "logs/resources/rfq_full_stage_repair02.json",
    }


@pytest.mark.parametrize(
    "mutation",
    ("repair_order", "cumulative_drop", "coverage_fingerprint", "receipt_sha", "auth"),
)
def test_repair02_contract_mutations_fail_closed(
    tmp_path, monkeypatch, mutation
):
    fixture = double_quarantine_contract_fixture(tmp_path)
    monkeypatch.setattr(
        mod, "apply_object_quarantine",
        lambda run_dir, manifest, inputs, _ignore_repair02=False: fixture["first"],
    )
    monkeypatch.setattr(mod, "_validate_repair02_archives", lambda *args: ([], []))
    repair02 = fixture["manifest"]["data_integrity_repairs"][1]
    if mutation == "repair_order":
        fixture["manifest"]["data_integrity_repairs"].reverse()
    elif mutation == "cumulative_drop":
        declaration_path = fixture["run_dir"] / mod.QUARANTINE_DECLARATION_02
        declaration = json.loads(declaration_path.read_text())
        declaration["cumulative_quarantined_objects"] = (
            declaration["cumulative_quarantined_objects"][:1]
        )
        write_json(declaration_path, declaration)
    elif mutation == "coverage_fingerprint":
        repair02["coverage"]["retained_selection_fingerprint_sha256"] = "0" * 64
    elif mutation == "receipt_sha":
        receipt_path = fixture["run_dir"] / mod.MALFORMED_OBJECT_RECEIPT_02
        receipt = json.loads(receipt_path.read_text())
        receipt["observed_sha256"] = "0" * 64
        write_json(receipt_path, receipt)
    elif mutation == "auth":
        auth_path = fixture["run_dir"] / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
        auth = json.loads(auth_path.read_text())
        auth["authorized_action"] = "forged"
        write_json(auth_path, auth)
    with pytest.raises(mod.RFQStageError):
        mod._apply_double_object_quarantine(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_any_second_malformed_consumed_object_still_aborts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    with fixture["next_path"].open("a", encoding="utf-8") as handle:
        handle.write('{"second":"malformed"\n')
    connection = duckdb.connect()
    with pytest.raises(Exception, match="Malformed JSON|unexpected character"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


def test_any_malformed_inner_payload_in_consumed_object_still_aborts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    with fixture["next_path"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(recorder(
            fixture["base_ns"] + 7_206_000_000_000, frame=None
        ) | {"raw": "{malformed-inner"}) + "\n")
    connection = duckdb.connect()
    with pytest.raises(mod.RFQStageError, match="malformed inner RFQ payload"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


def test_rfq_receipt_sidecar_is_retained_and_strictly_parsed(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    assert fixture["receipt_path"] in inputs["paths"]
    with fixture["receipt_path"].open("a", encoding="utf-8") as handle:
        handle.write('{"malformed-receipt-sidecar"\n')
    connection = duckdb.connect()
    with pytest.raises(Exception, match="Malformed JSON|unexpected character"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


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
        "run_id": "synthetic-run",
        "cycle1_duckdb_binding": synthetic_cycle1_binding(),
        "objects": 1, "bytes": source.stat().st_size,
        "logical_manifest_bindings": 1, "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "path_size_fingerprint_sha256": "a" * 64,
        "coverage_status": "COMPLETE_MANIFEST_OBJECT_COVERAGE",
        "full_object_coverage": True,
        "unique_objects_total": 1,
        "unique_bytes_total": source.stat().st_size,
        "consumed_unique_objects": 1,
        "consumed_logical_bindings": 1,
        "consumed_bytes": source.stat().st_size,
        "consumed_object_set_sha256": "a" * 64,
        "quarantined_unique_objects": 0,
        "quarantined_logical_bindings": 0,
        "quarantined_bytes": 0,
        "quarantined_object_set_sha256": hashlib.sha256(b"").hexdigest(),
        "quarantine_reasons": [],
        "quarantine_details": [],
        "selection_fingerprint_sha256": "a" * 64,
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
