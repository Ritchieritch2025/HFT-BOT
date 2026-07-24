from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request

import pytest

from tools.research.pnl_spine.contracts import canonical_sha256
from tools.research.pnl_spine.production_lineage import (
    MANIFEST_PINS_SCHEMA,
    PARQUET_RUNTIME_POLICY,
    RECORD_SPEC_SCHEMA,
    ProductionLineageError,
    W09ExactVersionS3Reader,
    W09_INSTANCE_PROFILE_ARN,
    W09_REGION,
    _IMDSv2Credentials,
    _RejectRedirectHandler,
    _VerifiedParquetRuntime,
    _connect_pinned_duckdb,
    _direct_no_redirect_opener,
    _extract_parquet,
    _open_fixed_url,
    _materialize_verified_objects,
    _sha256_regular_file,
    _validate_parquet_runtime_receipt_shape,
    build_parquet_runtime_receipt,
    main,
    produce_lineage_receipt,
    production_extractor_code_sha256,
    refuse_non_imds_credentials,
    require_production_record_spec_v2,
    validate_extractor_code_pin,
    validate_parquet_runtime_receipt,
    write_receipt_create_once,
)
from tools.research.pnl_spine.runner import (
    LINEAGE_RECORD_SCHEMA_SHA256,
    _validate_lineage_receipt,
)


BUCKET = "kalshi-vault-ritcardo"
ROOT = Path(__file__).resolve().parents[1]
REAL_PIN_CONFIG = (
    ROOT
    / "Deepresearch V3"
    / "pnl_spine"
    / "inputs"
    / "EXACT_V3_RELEASES_20260710_17.json"
)
REAL_PIN_CONFIG_SHA256 = (
    "eef0c38ff454da87a713d825ed18b665bb1b8fd87aa55e1a3b68807168b7e8f6"
)


def raw_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def utc_seconds(day: int) -> int:
    return int(
        datetime(
            2026, 7, 10 + day, tzinfo=timezone.utc
        ).timestamp()
    )


class FakeExactReader:
    def __init__(self, objects: dict[tuple[str, str, str], bytes]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str, str]] = []

    def iter_exact_version(
        self,
        bucket: str,
        key: str,
        version_id: str,
    ) -> Iterable[bytes]:
        identity = (bucket, key, version_id)
        self.calls.append(identity)
        try:
            payload = self.objects[identity]
        except KeyError as exc:
            raise AssertionError(f"unexpected exact read: {identity}") from exc
        midpoint = max(1, len(payload) // 2)
        yield payload[:midpoint]
        if payload[midpoint:]:
            yield payload[midpoint:]


def field_map(
    *fields: str,
    source_alias: str = "source",
    literals: dict[str, object] | None = None,
) -> dict:
    mapping = {
        field: {
            "source_alias": source_alias,
            "source_path": [field],
        }
        for field in fields
    }
    for field, value in (literals or {}).items():
        mapping[field] = {"literal": value}
    return {"schema_version": "field-map-v2", "fields": mapping}


def production_case() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    FakeExactReader,
]:
    rows_source = {
        "rows": [
            {
                "row_id": "row-1",
                "decision_ts_ns": utc_seconds(0) * 1_000_000_000 + 10,
                "signal_e4": 101,
            },
            {
                "row_id": "row-2",
                "decision_ts_ns": utc_seconds(0) * 1_000_000_000 + 20,
                "signal_e4": 202,
            },
        ]
    }
    trade_source = {
        "record": {
            "trade_id": "trade-1",
            "timestamp_us": utc_seconds(1) * 1_000_000 + 10,
            "price_e4": 5_100,
        }
    }
    l2_source = {
        "record": {
            "snapshot_id": "snapshot-1",
            "receive_timestamp_us": utc_seconds(2) * 1_000_000 + 20,
            "yes_bids": [[5_000, 10_000]],
        }
    }
    settlement_source = {
        "record": {
            "settlement_id": "settlement-1",
            "observed_at_ns": utc_seconds(3) * 1_000_000_000 + 30,
            "finalized": True,
        }
    }
    source_values = [
        rows_source,
        trade_source,
        l2_source,
        settlement_source,
        {"unused": {"day": 4}},
        {"unused": {"day": 5}},
        {"unused": {"day": 6}},
        {"unused": {"day": 7}},
    ]
    channels = [
        ("facts", "orderbooks_l1"),
        ("facts", "trades"),
        ("facts", "orderbooks_full"),
        ("catalog", None),
        ("catalog", None),
        ("catalog", None),
        ("catalog", None),
        ("catalog", None),
    ]
    logicals = [
        "warehouse/facts/orderbooks_l1/date=2026-07-10/part.parquet",
        "warehouse/facts/trades/date=2026-07-11/part.parquet",
        "warehouse/facts/orderbooks_full/date=2026-07-12/part.parquet",
        "warehouse/catalog/settlements/part-00000.parquet",
        "warehouse/catalog/series/part-00000.parquet",
        "warehouse/catalog/events/part-00000.parquet",
        "warehouse/catalog/markets/part-00000.parquet",
        "warehouse/catalog/series_classified/part-00000.parquet",
    ]
    runner_channels = [
        "L1",
        "TRADES",
        "L2",
        "SETTLEMENT",
        "CATALOG",
        "CATALOG",
        "CATALOG",
        "CATALOG",
    ]
    objects: dict[tuple[str, str, str], bytes] = {}
    pins: list[dict[str, str]] = []
    releases: list[dict[str, Any]] = []
    source_descriptors: list[dict[str, Any]] = []
    for index, source_value in enumerate(source_values):
        date = f"2026-07-{10 + index:02d}"
        body = raw_json(source_value)
        body_sha = hashlib.sha256(body).hexdigest()
        seal_sha = f"{index + 1:064x}"
        publication_sha = f"{index + 101:064x}"
        release_id = (
            f"{date}__v3ref__seal-{seal_sha[:8]}"
            f"__pub-{publication_sha[:16]}"
        )
        version_id = f"source-version-{index}"
        source_key = f"ec2/test/date={date}/object-{index}.json"
        item = {
            "logical_key": logicals[index],
            "source_bucket": BUCKET,
            "source_key": source_key,
            "source_version_id": version_id,
            "size": len(body),
            "sha256": body_sha,
            "kind": channels[index][0],
            "channel": channels[index][1],
            "date": date,
            "required": True,
            "seal_binding": seal_sha,
            "evidence_binding": {"fixture": True},
        }
        manifest = {
            "schema": "research-release-manifest-v3-reference",
            "schema_version": 3,
            "storage_mode": "CANONICAL_REFERENCE",
            "publication_status": "PUBLISHED",
            "release_id": release_id,
            "date": date,
            "publication_state_sha256": publication_sha,
            "source_seal": {"sha256": seal_sha},
            "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
            "objects": [item],
        }
        manifest_raw = raw_json(manifest)
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        manifest_version = f"manifest-version-{index}"
        manifest_key = f"research/releases/{release_id}/MANIFEST.json"
        objects[(BUCKET, manifest_key, manifest_version)] = manifest_raw
        objects[(BUCKET, source_key, version_id)] = body
        pins.append(
            {
                "bucket": BUCKET,
                "manifest_key": manifest_key,
                "manifest_version_id": manifest_version,
                "manifest_raw_sha256": manifest_sha,
                "release_id": release_id,
                "date": date,
            }
        )
        descriptor = {
            "logical_key": logicals[index],
            "version_id": version_id,
            "sha256": body_sha,
            "size_bytes": len(body),
            "channel": runner_channels[index],
            "date": date,
        }
        source_descriptors.append(descriptor)
        releases.append(
            {
                "release_id": release_id,
                "date": date,
                "manifest_sha256": manifest_sha,
                "manifest_version_id": manifest_version,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "objects": [descriptor],
            }
        )

    rows = rows_source["rows"]
    public_trade = {
        **trade_source["record"],
        "source_sha256": source_descriptors[1]["sha256"],
    }
    l2_snapshot = {
        **l2_source["record"],
        "source_sha256": source_descriptors[2]["sha256"],
    }
    settlement = {
        **settlement_source["record"],
        "source_sha256": source_descriptors[3]["sha256"],
    }
    fixture: dict[str, Any] = {
        "run_id": "production-lineage-fixture-1",
        "provenance": {
            "releases": releases,
        },
        "rows": rows,
        "public_trades": [public_trade],
        "exit_snapshots": [l2_snapshot],
        "settlements": [settlement],
        "evidence_bindings": [],
    }
    records = [
        ("NORMALIZED_ROW", row["row_id"], row, 0)
        for row in rows
    ] + [
        ("PUBLIC_TRADE", "trade-1", public_trade, 1),
        ("L2_SNAPSHOT", "snapshot-1", l2_snapshot, 2),
        ("SETTLEMENT", "settlement-1", settlement, 3),
    ]
    fixture["evidence_bindings"] = [
        {
            "kind": kind,
            "record_id": record_id,
            "record_sha256": canonical_sha256(record),
            "release_id": releases[index]["release_id"],
            "source_object_logical_key": logicals[index],
            "source_object_version_id": source_descriptors[index]["version_id"],
            "source_object_sha256": source_descriptors[index]["sha256"],
        }
        for kind, record_id, record, index in records
    ]
    by_key = {
        (kind, record_id): (record, index)
        for kind, record_id, record, index in records
    }
    spec_rows: list[dict[str, Any]] = []
    for kind, record_id in sorted(by_key):
        record, index = by_key[(kind, record_id)]
        if kind == "NORMALIZED_ROW":
            row_number = 0 if record_id == "row-1" else 1
            locator = {
                "schema_version": "json-pointer-v1",
                "pointer": f"/rows/{row_number}",
            }
            transform = field_map(
                "row_id", "decision_ts_ns", "signal_e4"
            )
        else:
            locator = {
                "schema_version": "json-pointer-v1",
                "pointer": "/record",
            }
            source_fields = [
                field for field in record if field != "source_sha256"
            ]
            transform = field_map(
                *source_fields,
                literals={
                    "source_sha256": source_descriptors[index]["sha256"]
                },
            )
        spec_rows.append(
            {
                "kind": kind,
                "record_id": record_id,
                "mode": "DERIVED",
                "sources": [
                    {
                        "alias": "source",
                        "release_id": releases[index]["release_id"],
                        "logical_key": logicals[index],
                        "version_id": source_descriptors[index]["version_id"],
                        "locator": locator,
                    }
                ],
                "transform": transform,
            }
        )
    manifest_pins = {
        "schema_version": MANIFEST_PINS_SCHEMA,
        "pins": pins,
    }
    record_spec = {
        "schema_version": RECORD_SPEC_SCHEMA,
        "run_id": fixture["run_id"],
        "records": spec_rows,
    }
    return fixture, manifest_pins, record_spec, FakeExactReader(objects)


def multisource_production_case() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    FakeExactReader,
]:
    fixture, pins, spec, reader = production_case()
    pin = pins["pins"][0]
    manifest_identity = (
        pin["bucket"],
        pin["manifest_key"],
        pin["manifest_version_id"],
    )
    manifest = json.loads(reader.objects[manifest_identity])
    release = fixture["provenance"]["releases"][0]
    date = pin["date"]
    seal_sha = manifest["source_seal"]["sha256"]
    additions = [
        (
            "threshold",
            "warehouse/catalog/series_classified/part-00000.parquet",
            "CATALOG",
            "catalog",
            None,
            {"record": {"train_threshold_e4": 303}},
        ),
        (
            "l2",
            (
                "warehouse/facts/orderbooks_full/"
                "date=2026-07-10/extra.parquet"
            ),
            "L2",
            "facts",
            "orderbooks_full",
            {"record": {"depth_e4": 404}},
        ),
    ]
    descriptors: dict[str, dict[str, Any]] = {}
    for suffix, logical, runner_channel, kind, channel, value in additions:
        body = raw_json(value)
        digest = hashlib.sha256(body).hexdigest()
        version = f"{suffix}-version"
        source_key = f"ec2/test/date={date}/{suffix}.json"
        manifest["objects"].append(
            {
                "logical_key": logical,
                "source_bucket": BUCKET,
                "source_key": source_key,
                "source_version_id": version,
                "size": len(body),
                "sha256": digest,
                "kind": kind,
                "channel": channel,
                "date": date,
                "required": True,
                "seal_binding": seal_sha,
                "evidence_binding": {"fixture": True},
            }
        )
        descriptor = {
            "logical_key": logical,
            "version_id": version,
            "sha256": digest,
            "size_bytes": len(body),
            "channel": runner_channel,
            "date": date,
        }
        release["objects"].append(descriptor)
        descriptors[suffix] = descriptor
        reader.objects[(BUCKET, source_key, version)] = body

    manifest_raw = raw_json(manifest)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    reader.objects[manifest_identity] = manifest_raw
    pin["manifest_raw_sha256"] = manifest_sha
    release["manifest_sha256"] = manifest_sha

    row = fixture["rows"][0]
    row["depth_e4"] = 404
    row["train_threshold_e4"] = 303
    binding = next(
        value
        for value in fixture["evidence_bindings"]
        if value["kind"] == "NORMALIZED_ROW"
        and value["record_id"] == "row-1"
    )
    binding["record_sha256"] = canonical_sha256(row)
    spec_row = next(
        value
        for value in spec["records"]
        if value["kind"] == "NORMALIZED_ROW"
        and value["record_id"] == "row-1"
    )
    l1 = spec_row["sources"][0]
    l1["alias"] = "l1"
    threshold = {
        "alias": "threshold",
        "release_id": release["release_id"],
        "logical_key": descriptors["threshold"]["logical_key"],
        "version_id": descriptors["threshold"]["version_id"],
        "locator": {
            "schema_version": "json-pointer-v1",
            "pointer": "/record",
        },
    }
    l2 = {
        "alias": "l2",
        "release_id": release["release_id"],
        "logical_key": descriptors["l2"]["logical_key"],
        "version_id": descriptors["l2"]["version_id"],
        "locator": {
            "schema_version": "json-pointer-v1",
            "pointer": "/record",
        },
    }
    spec_row["sources"] = sorted(
        [l1, l2, threshold],
        key=lambda source: (
            source["release_id"],
            source["logical_key"],
            source["version_id"],
            source["locator"]["schema_version"],
            raw_json(source["locator"]).decode("ascii"),
        ),
    )
    spec_row["transform"] = {
        "schema_version": "field-map-v2",
        "fields": {
            "row_id": {
                "source_alias": "l1",
                "source_path": ["row_id"],
            },
            "decision_ts_ns": {
                "source_alias": "l1",
                "source_path": ["decision_ts_ns"],
            },
            "signal_e4": {
                "source_alias": "l1",
                "source_path": ["signal_e4"],
            },
            "depth_e4": {
                "source_alias": "l2",
                "source_path": ["depth_e4"],
            },
            "train_threshold_e4": {
                "source_alias": "threshold",
                "source_path": ["train_threshold_e4"],
            },
        },
    }
    return fixture, pins, spec, reader


def test_production_receipt_is_runner_accepted_and_hashes_shared_object_once():
    fixture, pins, spec, reader = production_case()

    receipt = produce_lineage_receipt(
        fixture=fixture,
        manifest_pins=pins,
        record_spec=spec,
        reader=reader,
    )

    assert receipt["schema_version"] == "pnl-spine-lineage-receipt-v1"
    assert receipt["record_schema_sha256"] == LINEAGE_RECORD_SCHEMA_SHA256
    assert len(receipt["records"]) == 5
    assert len(receipt["object_reads"]) == 4
    assert all(
        row["mode"] == "DERIVED"
        for row in receipt["records"]
        if row["kind"] == "NORMALIZED_ROW"
    )
    _validate_lineage_receipt(
        fixture,
        receipt,
        expected_sha256=canonical_sha256(receipt),
    )
    # Eight exact manifests plus four unique physical data objects.  The two
    # normalized rows fan out from one verified object.
    assert len(reader.calls) == 12
    row_object = (
        BUCKET,
        "ec2/test/date=2026-07-10/object-0.json",
        "source-version-0",
    )
    assert reader.calls.count(row_object) == 1


def test_exported_api_rejects_wrong_code_pin_before_any_exact_read():
    fixture, pins, spec, reader = production_case()

    with pytest.raises(
        ProductionLineageError,
        match="extractor code-bundle SHA-256 differs from external pin",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
            expected_extractor_code_sha256="0" * 64,
        )

    assert reader.calls == []


def test_invalid_transform_fails_local_preflight_before_any_exact_read():
    fixture, pins, spec, reader = production_case()
    spec["records"][0]["transform"]["schema_version"] = "field-map-v1"

    with pytest.raises(
        ProductionLineageError,
        match="DERIVED requires field-map-v2",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )

    assert reader.calls == []


def test_root_production_mode_fails_before_any_exact_read(
    monkeypatch: pytest.MonkeyPatch,
):
    fixture, pins, spec, reader = production_case()
    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage.os.geteuid",
        lambda: 0,
    )

    with pytest.raises(
        ProductionLineageError,
        match="dedicated non-root identity",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
            expected_extractor_code_sha256="0" * 64,
            require_root_read_only_code=True,
        )

    assert reader.calls == []


def test_late_fixture_pin_drift_fails_before_any_exact_read():
    fixture, pins, spec, reader = production_case()
    fixture["provenance"]["releases"][-1]["manifest_version_id"] = (
        "locally-drifted-version"
    )

    with pytest.raises(
        ProductionLineageError,
        match="differs from manifest pin",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )

    assert reader.calls == []


def test_late_evidence_binding_drift_fails_before_any_exact_read():
    fixture, pins, spec, reader = production_case()
    fixture["evidence_bindings"][-1]["source_object_version_id"] = (
        "locally-drifted-version"
    )

    with pytest.raises(
        ProductionLineageError,
        match="evidence binding matches no source",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )

    assert reader.calls == []


def test_record_spec_source_absent_from_fixture_fails_before_exact_read():
    fixture, pins, spec, reader = production_case()
    spec["records"][-1]["sources"][0]["version_id"] = (
        "locally-absent-version"
    )

    with pytest.raises(
        ProductionLineageError,
        match="record spec object is absent from exact manifests",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )

    assert reader.calls == []


def test_multisource_normalized_join_binds_l1_l2_and_train_artifact():
    fixture, pins, spec, reader = multisource_production_case()

    receipt = produce_lineage_receipt(
        fixture=fixture,
        manifest_pins=pins,
        record_spec=spec,
        reader=reader,
    )

    row = next(
        value
        for value in receipt["records"]
        if value["kind"] == "NORMALIZED_ROW"
        and value["record_id"] == "row-1"
    )
    assert row["mode"] == "DERIVED"
    assert [member["channel"] for member in row["source_members"]] == [
        "CATALOG",
        "L2",
        "L1",
    ]
    assert row["input_set_sha256"] == canonical_sha256(
        row["source_members"]
    )
    _validate_lineage_receipt(
        fixture,
        receipt,
        expected_sha256=canonical_sha256(receipt),
    )


def multisource_row(spec: dict[str, Any]) -> dict[str, Any]:
    return next(
        value
        for value in spec["records"]
        if value["kind"] == "NORMALIZED_ROW"
        and value["record_id"] == "row-1"
    )


def test_multisource_missing_transform_source_fails_closed():
    fixture, pins, spec, reader = multisource_production_case()
    row = multisource_row(spec)
    row["sources"] = [
        source for source in row["sources"] if source["alias"] != "l2"
    ]

    with pytest.raises(
        ProductionLineageError,
        match="unknown source alias",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_multisource_swapped_source_fails_closed():
    fixture, pins, spec, reader = multisource_production_case()
    row = multisource_row(spec)
    l1 = next(source for source in row["sources"] if source["alias"] == "l1")
    l2 = next(source for source in row["sources"] if source["alias"] == "l2")
    l2.update(
        {
            "release_id": l1["release_id"],
            "logical_key": l1["logical_key"],
            "version_id": l1["version_id"],
            "locator": {
                "schema_version": "json-pointer-v1",
                "pointer": "/rows/1",
            },
        }
    )
    row["sources"].sort(
        key=lambda source: (
            source["release_id"],
            source["logical_key"],
            source["version_id"],
            source["locator"]["schema_version"],
            raw_json(source["locator"]).decode("ascii"),
        )
    )

    with pytest.raises(
        ProductionLineageError,
        match="source_path member is absent",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_multisource_unsorted_or_duplicate_sources_fail_closed():
    fixture, pins, spec, reader = multisource_production_case()
    row = multisource_row(spec)
    row["sources"].reverse()

    with pytest.raises(
        ProductionLineageError,
        match="sources must be sorted",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )

    fixture, pins, spec, reader = multisource_production_case()
    row = multisource_row(spec)
    duplicate = copy.deepcopy(row["sources"][0])
    duplicate["alias"] = "duplicate"
    row["sources"].insert(1, duplicate)

    with pytest.raises(
        ProductionLineageError,
        match="duplicate source member",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_legacy_v1_single_source_spec_remains_accepted():
    fixture, pins, spec, reader = production_case()
    spec["schema_version"] = "pnl-spine-record-extraction-spec-v1"
    for row in spec["records"]:
        source = row.pop("sources")[0]
        row.update(
            {
                "release_id": source["release_id"],
                "logical_key": source["logical_key"],
                "version_id": source["version_id"],
                "locator": source["locator"],
            }
        )
        row["transform"]["schema_version"] = "field-map-v1"
        for selector in row["transform"]["fields"].values():
            selector.pop("source_alias", None)

    receipt = produce_lineage_receipt(
        fixture=fixture,
        manifest_pins=pins,
        record_spec=spec,
        reader=reader,
    )

    _validate_lineage_receipt(
        fixture,
        receipt,
        expected_sha256=canonical_sha256(receipt),
    )

    with pytest.raises(
        ProductionLineageError,
        match="production CLI requires.*v2",
    ):
        require_production_record_spec_v2(spec)


def test_two_logical_bindings_to_one_physical_version_hash_once(
    tmp_path: Path,
):
    body = b'{"shared":"catalog"}'
    identity = (BUCKET, "ec2/shared/catalog.json", "shared-version")
    digest = hashlib.sha256(body).hexdigest()
    descriptors = [
        {
            "bucket": BUCKET,
            "source_key": identity[1],
            "version_id": identity[2],
            "source_object_sha256": digest,
            "size_bytes": len(body),
            "release_id": f"release-{index}",
            "logical_key": f"logical-{index}",
        }
        for index in range(2)
    ]
    reader = FakeExactReader({identity: body})

    paths, verified = _materialize_verified_objects(
        reader, descriptors, tmp_path
    )

    assert reader.calls == [identity]
    assert set(paths) == {identity}
    assert verified == {identity: (digest, len(body))}


def test_repo_production_pin_config_is_the_reviewed_eight_release_set():
    raw = REAL_PIN_CONFIG.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REAL_PIN_CONFIG_SHA256
    document = json.loads(raw)
    assert set(document) == {"schema_version", "pins"}
    assert document["schema_version"] == MANIFEST_PINS_SCHEMA
    assert [row["date"] for row in document["pins"]] == [
        f"2026-07-{day:02d}" for day in range(10, 18)
    ]
    assert len(document["pins"]) == 8
    for row in document["pins"]:
        assert set(row) == {
            "bucket",
            "manifest_key",
            "manifest_version_id",
            "manifest_raw_sha256",
            "release_id",
            "date",
        }
        assert row["bucket"] == BUCKET
        assert row["manifest_key"] == (
            f"research/releases/{row['release_id']}/MANIFEST.json"
        )


def test_data_object_sha_or_size_drift_fails_before_receipt():
    fixture, pins, spec, reader = production_case()
    identity = (
        BUCKET,
        "ec2/test/date=2026-07-11/object-1.json",
        "source-version-1",
    )
    reader.objects[identity] += b" "

    with pytest.raises(
        ProductionLineageError,
        match="byte size mismatch",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_manifest_raw_sha_drift_fails_closed():
    fixture, pins, spec, reader = production_case()
    manifest_identity = (
        pins["pins"][0]["bucket"],
        pins["pins"][0]["manifest_key"],
        pins["pins"][0]["manifest_version_id"],
    )
    reader.objects[manifest_identity] += b" "

    with pytest.raises(
        ProductionLineageError,
        match="manifest .* raw SHA-256 mismatch",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_normalized_direct_mode_is_forbidden():
    fixture, pins, spec, reader = production_case()
    target = next(
        row for row in spec["records"] if row["kind"] == "NORMALIZED_ROW"
    )
    target["mode"] = "DIRECT"
    target["transform"] = {"schema_version": "identity-v1"}

    with pytest.raises(ProductionLineageError, match="must be DERIVED"):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_manifest_pin_set_must_be_exactly_eight_and_sorted():
    fixture, pins, spec, reader = production_case()
    pins["pins"].pop()

    with pytest.raises(ProductionLineageError, match="exactly 8"):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_record_extraction_mismatch_fails_closed_even_when_object_is_valid():
    fixture, pins, spec, reader = production_case()
    target = next(
        row
        for row in spec["records"]
        if row["kind"] == "PUBLIC_TRADE"
    )
    target["transform"]["fields"]["price_e4"] = {"literal": 9_999}

    with pytest.raises(
        ProductionLineageError,
        match="differs from the runtime fixture",
    ):
        produce_lineage_receipt(
            fixture=fixture,
            manifest_pins=pins,
            record_spec=spec,
            reader=reader,
        )


def test_w09_request_contains_version_id_and_temporary_token_only():
    class Credentials:
        def current(self) -> tuple[str, str, str]:
            return ("TEMPKEY", "TEMPSECRET", "TEMPTOKEN")

    reader = object.__new__(W09ExactVersionS3Reader)
    reader.region = "us-east-2"
    reader.credentials = Credentials()

    request = reader._request(
        BUCKET,
        "ec2/test/object.json",
        "exact-version+with/slash=",
    )

    parsed = urlsplit(request.full_url)
    assert parsed.hostname == (
        "kalshi-vault-ritcardo.s3.us-east-2.amazonaws.com"
    )
    assert parsed.username is None
    assert parsed.password is None
    assert parse_qs(parsed.query) == {
        "versionId": ["exact-version+with/slash="]
    }
    assert request.method == "GET"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-amz-security-token"] == "TEMPTOKEN"
    assert "AWS4-HMAC-SHA256" in headers["authorization"]
    assert "access_key" not in parsed.query.lower()


def test_production_transport_disables_proxy_and_redirect_replay(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "https://attacker.invalid:8443"},
    )
    opener = _direct_no_redirect_opener()
    proxy_handlers = [
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    redirect_handlers = [
        handler
        for handler in opener.handlers
        if isinstance(handler, _RejectRedirectHandler)
    ]

    assert all(handler.proxies == {} for handler in proxy_handlers)
    assert len(redirect_handlers) == 1
    sensitive = urllib.request.Request(
        "https://kalshi-vault-ritcardo.s3.us-east-2.amazonaws.com/object",
        headers={
            "Authorization": "AWS4-HMAC-SHA256 secret-signature",
            "x-amz-security-token": "temporary-token",
        },
    )
    assert (
        redirect_handlers[0].redirect_request(
            sensitive,
            None,  # type: ignore[arg-type]
            307,
            "Temporary Redirect",
            {},
            "https://attacker.invalid/collect",
        )
        is None
    )


def test_sensitive_headers_are_not_replayed_to_redirect_target():
    observed: list[dict[str, str]] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            observed.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            del args

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_url = f"http://127.0.0.1:{target.server_port}/collect"

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(307)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            del args

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{redirect.server_port}/signed",
            headers={
                "Authorization": "AWS4-HMAC-SHA256 secret-signature",
                "x-amz-security-token": "temporary-token",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            _open_fixed_url(request, timeout=2)
        assert raised.value.code == 307
        assert observed == []
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "region",
    [
        "us-east-2@attacker.invalid",
        "us-east-2/attacker",
        "us-east-2?x=@attacker.invalid",
        "https://attacker.invalid",
        "US-EAST-2",
        "",
    ],
)
def test_w09_reader_rejects_every_nonfixed_region(region: str):
    with pytest.raises(
        ProductionLineageError,
        match=f"must be exactly {W09_REGION}",
    ):
        W09ExactVersionS3Reader(region=region)


def test_extractor_code_requires_external_pin_and_rejects_symlink(
    tmp_path: Path,
):
    actual = production_extractor_code_sha256()
    assert validate_extractor_code_pin(actual) == actual
    with pytest.raises(
        ProductionLineageError,
        match="differs from external pin",
    ):
        validate_extractor_code_pin("0" * 64)

    target = tmp_path / "producer.py"
    target.write_text("pass\n", encoding="utf-8")
    link = tmp_path / "producer-link.py"
    link.symlink_to(target)
    with pytest.raises(
        ProductionLineageError,
        match="contains a symlink",
    ):
        _sha256_regular_file(link, require_root_read_only=False)


def test_readme_command_pins_the_current_producer_bytes():
    readme = (
        ROOT / "Deepresearch V3" / "pnl_spine" / "README.md"
    ).read_text(encoding="utf-8")
    assert production_extractor_code_sha256() in readme


def test_production_main_rejects_legacy_v1_before_constructing_s3_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    fixture, pins, spec, _reader = production_case()
    spec["schema_version"] = "pnl-spine-record-extraction-spec-v1"
    for row in spec["records"]:
        source = row.pop("sources")[0]
        row.update(
            {
                "release_id": source["release_id"],
                "logical_key": source["logical_key"],
                "version_id": source["version_id"],
                "locator": source["locator"],
            }
        )
        row["transform"]["schema_version"] = "field-map-v1"
        for selector in row["transform"]["fields"].values():
            selector.pop("source_alias", None)

    fixture_path = tmp_path / "fixture.json"
    pins_path = tmp_path / "pins.json"
    spec_path = tmp_path / "spec.json"
    for path, value in (
        (fixture_path, fixture),
        (pins_path, pins),
        (spec_path, spec),
    ):
        path.write_bytes(raw_json(value))
    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage."
        "validate_extractor_code_pin",
        lambda expected, require_root_read_only: expected,
    )

    def forbidden_reader(*args: object, **kwargs: object) -> None:
        raise AssertionError("legacy spec reached the S3 reader")

    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage."
        "W09ExactVersionS3Reader",
        forbidden_reader,
    )
    args = [
        "--fixture",
        str(fixture_path),
        "--fixture-sha256",
        hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "--manifest-pins",
        str(pins_path),
        "--manifest-pins-sha256",
        hashlib.sha256(pins_path.read_bytes()).hexdigest(),
        "--record-spec",
        str(spec_path),
        "--record-spec-sha256",
        hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        "--expected-extractor-code-sha256",
        "1" * 64,
        "--output",
        str(tmp_path / "receipt.json"),
    ]

    with pytest.raises(
        ProductionLineageError,
        match="production CLI requires.*v2",
    ):
        main(args)


def test_production_code_mode_requires_root_owned_read_only_deployment():
    with pytest.raises(
        ProductionLineageError,
        match="root-owned and read-only",
    ):
        production_extractor_code_sha256(require_root_read_only=True)


def test_receipt_output_is_create_once_and_refuses_regular_or_symlink(
    tmp_path: Path,
):
    payload = {"schema_version": "test-receipt-v1", "value": 1}
    output = tmp_path / "receipt.json"

    raw_sha = write_receipt_create_once(output, payload)

    assert hashlib.sha256(output.read_bytes()).hexdigest() == raw_sha
    assert output.stat().st_mode & 0o777 == 0o400
    original = output.read_bytes()
    with pytest.raises(
        ProductionLineageError,
        match="create-once refuses overwrite",
    ):
        write_receipt_create_once(output, {**payload, "value": 2})
    assert output.read_bytes() == original

    target = tmp_path / "target.json"
    target.write_text("untouched", encoding="utf-8")
    symlink_output = tmp_path / "symlink-receipt.json"
    symlink_output.symlink_to(target)
    with pytest.raises(
        ProductionLineageError,
        match="create-once refuses overwrite",
    ):
        write_receipt_create_once(symlink_output, payload)
    assert target.read_text(encoding="utf-8") == "untouched"
    assert symlink_output.is_symlink()


def test_imds_rejects_same_profile_name_from_wrong_account(
    monkeypatch: pytest.MonkeyPatch,
):
    wrong_arn = (
        "arn:aws:iam::999999999999:"
        "instance-profile/w09-research-runner"
    )

    def fake_request(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout: float = 2.0,
    ) -> bytes:
        del method, headers, timeout
        if url.endswith("/api/token"):
            return b"token"
        if url.endswith("/meta-data/instance-id"):
            return b"i-0e53d134dceffe166"
        if url.endswith("/meta-data/iam/info"):
            return raw_json({"InstanceProfileArn": wrong_arn})
        raise AssertionError(f"unexpected IMDS request after bad ARN: {url}")

    monkeypatch.setattr(
        _IMDSv2Credentials,
        "_request",
        staticmethod(fake_request),
    )

    with pytest.raises(
        ProductionLineageError,
        match="fixed account and profile",
    ):
        _IMDSv2Credentials().refresh()


def test_imds_accepts_only_exact_account_profile_and_role(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_request(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout: float = 2.0,
    ) -> bytes:
        del method, headers, timeout
        if url.endswith("/api/token"):
            return b"token"
        if url.endswith("/meta-data/instance-id"):
            return b"i-0e53d134dceffe166"
        if url.endswith("/meta-data/iam/info"):
            return raw_json(
                {"InstanceProfileArn": W09_INSTANCE_PROFILE_ARN}
            )
        if url.endswith("/meta-data/iam/security-credentials/"):
            return b"w09-research-runner"
        if url.endswith(
            "/meta-data/iam/security-credentials/w09-research-runner"
        ):
            return raw_json(
                {
                    "Code": "Success",
                    "AccessKeyId": "temporary-key",
                    "SecretAccessKey": "temporary-secret",
                    "Token": "temporary-token",
                    "Expiration": "2099-01-01T00:00:00Z",
                }
            )
        raise AssertionError(f"unexpected IMDS request: {url}")

    monkeypatch.setattr(
        _IMDSv2Credentials,
        "_request",
        staticmethod(fake_request),
    )
    credentials = _IMDSv2Credentials()

    credentials.refresh()

    assert credentials.current() == (
        "temporary-key",
        "temporary-secret",
        "temporary-token",
    )


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    ],
)
def test_alternate_credential_configuration_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
):
    monkeypatch.setenv(name, "present")

    with pytest.raises(
        ProductionLineageError,
        match="credential configuration is forbidden",
    ):
        refuse_non_imds_credentials()


def fake_parquet_runtime_receipt() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "pnl-spine-parquet-runtime-receipt-v1",
        "policy": dict(PARQUET_RUNTIME_POLICY),
        "python": {
            "base_prefix": "/usr",
            "cache_tag": "cpython-311",
            "executable": "/opt/w09-pnl-runtime/bin/python3",
            "executable_raw_sha256": "1" * 64,
            "executable_size_bytes": 10,
            "implementation": "cpython",
            "prefix": "/opt/w09-pnl-runtime",
            "version": "3.11.9",
            "flags": {
                "dont_write_bytecode": 1,
                "ignore_environment": 1,
                "isolated": 1,
                "no_user_site": 1,
            },
        },
        "duckdb": {
            "effective_settings": {
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "enable_object_cache": "false",
                "max_temp_directory_size": "0 bytes",
                "memory_limit": "4.0 GiB",
                "threads": "1",
            },
            "files": [
                {
                    "relative_path": (
                        "lib/python3.11/site-packages/_duckdb.so"
                    ),
                    "role": "DUCKDB_NATIVE_EXTENSION",
                    "size_bytes": 20,
                    "raw_sha256": "2" * 64,
                },
                {
                    "relative_path": (
                        "lib/python3.11/site-packages/duckdb/__init__.py"
                    ),
                    "role": "DUCKDB_PACKAGE",
                    "size_bytes": 30,
                    "raw_sha256": "3" * 64,
                },
            ],
            "module_relative_path": (
                "lib/python3.11/site-packages/duckdb/__init__.py"
            ),
            "native_extension_relative_path": (
                "lib/python3.11/site-packages/_duckdb.so"
            ),
            "native_version": "1.4.5",
            "version": "1.4.5",
        },
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def test_production_forbids_parquet_v1_and_binds_one_v2_runtime():
    _fixture, _pins, spec, _reader = production_case()
    locator = spec["records"][0]["sources"][0]["locator"]
    locator.clear()
    locator.update(
        {
            "schema_version": "parquet-key-v1",
            "match": {"row_id": "row-1"},
            "source_fields": ["row_id"],
        }
    )
    with pytest.raises(
        ProductionLineageError,
        match="forbids unpinned parquet-key-v1",
    ):
        require_production_record_spec_v2(spec)

    locator["schema_version"] = "parquet-key-v2"
    locator["runtime_receipt_raw_sha256"] = "4" * 64
    assert require_production_record_spec_v2(spec) == "4" * 64

    second = spec["records"][1]["sources"][0]["locator"]
    second.clear()
    second.update(
        {
            "schema_version": "parquet-key-v2",
            "match": {"trade_id": "trade-1"},
            "source_fields": ["trade_id"],
            "runtime_receipt_raw_sha256": "5" * 64,
        }
    )
    with pytest.raises(
        ProductionLineageError,
        match="cannot mix Parquet runtime receipts",
    ):
        require_production_record_spec_v2(spec)


def test_parquet_v2_locator_requires_verified_matching_runtime():
    path = Path("/does/not/matter.parquet")
    locator = {
        "schema_version": "parquet-key-v2",
        "match": {"row_id": "row-1"},
        "source_fields": ["row_id"],
        "runtime_receipt_raw_sha256": "4" * 64,
    }
    with pytest.raises(
        ProductionLineageError,
        match="requires a verified runtime receipt",
    ):
        _extract_parquet(path, locator, runtime=None)

    runtime = _VerifiedParquetRuntime(
        raw_sha256="5" * 64,
        receipt=fake_parquet_runtime_receipt(),
    )
    with pytest.raises(
        ProductionLineageError,
        match="differs from live pin",
    ):
        _extract_parquet(path, locator, runtime=runtime)


def test_runtime_receipt_is_strict_and_live_bytes_are_recomputed(
    monkeypatch: pytest.MonkeyPatch,
):
    receipt = fake_parquet_runtime_receipt()
    assert _validate_parquet_runtime_receipt_shape(receipt) == receipt
    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage."
        "build_parquet_runtime_receipt",
        lambda require_root_read_only: copy.deepcopy(receipt),
    )
    verified = validate_parquet_runtime_receipt(
        receipt,
        raw_sha256="6" * 64,
        require_root_read_only=True,
    )
    assert verified.raw_sha256 == "6" * 64

    changed = copy.deepcopy(receipt)
    changed["duckdb"]["version"] = "9.9.9"
    changed.pop("payload_sha256")
    changed["payload_sha256"] = canonical_sha256(changed)
    with pytest.raises(
        ProductionLineageError,
        match="live Python/DuckDB runtime differs",
    ):
        validate_parquet_runtime_receipt(
            changed,
            raw_sha256="7" * 64,
            require_root_read_only=True,
        )

    no_native = copy.deepcopy(receipt)
    no_native["duckdb"]["files"] = no_native["duckdb"]["files"][1:]
    no_native.pop("payload_sha256")
    no_native["payload_sha256"] = canonical_sha256(no_native)
    with pytest.raises(
        ProductionLineageError,
        match="native extension is absent",
    ):
        _validate_parquet_runtime_receipt_shape(no_native)


def test_runtime_receipt_measures_real_duckdb_package_and_native_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    # The developer machine is intentionally not a production venv.  A fake
    # isolated-prefix view rooted at "/" lets this test exercise the real
    # package/native enumeration without weakening production's root checks.
    fake_sys = SimpleNamespace(
        base_prefix=sys.base_prefix,
        executable=str(Path(sys.executable).resolve()),
        flags=SimpleNamespace(
            dont_write_bytecode=1,
            ignore_environment=1,
            isolated=1,
            no_user_site=1,
        ),
        implementation=sys.implementation,
        modules={},
        prefix="/",
        version_info=sys.version_info,
    )
    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage.sys",
        fake_sys,
    )

    receipt = build_parquet_runtime_receipt(
        require_root_read_only=False
    )

    _validate_parquet_runtime_receipt_shape(receipt)
    assert receipt["duckdb"]["version"] == receipt["duckdb"]["native_version"]
    roles = {row["role"] for row in receipt["duckdb"]["files"]}
    assert roles == {"DUCKDB_PACKAGE", "DUCKDB_NATIVE_EXTENSION"}
    assert receipt["policy"] == PARQUET_RUNTIME_POLICY
    assert receipt["duckdb"]["effective_settings"]["threads"] == "1"
    assert (
        receipt["duckdb"]["effective_settings"][
            "autoinstall_known_extensions"
        ]
        == "false"
    )
    assert (
        receipt["duckdb"]["effective_settings"][
            "autoload_known_extensions"
        ]
        == "false"
    )


def test_duckdb_connection_policy_is_single_thread_bounded_and_extension_off():
    captured: dict[str, object] = {}

    class Connection:
        closed = False

        def execute(self, _query: str) -> "Connection":
            return self

        def fetchall(self) -> list[tuple[str, str]]:
            return [
                ("autoinstall_known_extensions", "false"),
                ("autoload_known_extensions", "false"),
                ("enable_object_cache", "false"),
                ("max_temp_directory_size", "0 bytes"),
                ("memory_limit", "4.0 GiB"),
                ("threads", "1"),
            ]

        def close(self) -> None:
            self.closed = True

    class DuckDB:
        def connect(
            self,
            *,
            database: str,
            config: dict[str, str],
        ) -> Connection:
            captured["database"] = database
            captured["config"] = config
            return Connection()

    connection, effective = _connect_pinned_duckdb(DuckDB())

    assert captured == {
        "database": ":memory:",
        "config": {
            "threads": "1",
            "memory_limit": "4294967296B",
            "max_temp_directory_size": "0B",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "enable_object_cache": "false",
        },
    }
    assert effective["threads"] == "1"
    assert effective["autoinstall_known_extensions"] == "false"
    assert effective["autoload_known_extensions"] == "false"
    connection.close()


def test_duckdb_connection_fails_if_auto_extension_setting_is_ignored():
    class Connection:
        closed = False

        def execute(self, _query: str) -> "Connection":
            return self

        def fetchall(self) -> list[tuple[str, str]]:
            return [
                ("autoinstall_known_extensions", "true"),
                ("autoload_known_extensions", "false"),
                ("enable_object_cache", "false"),
                ("max_temp_directory_size", "0 bytes"),
                ("memory_limit", "4.0 GiB"),
                ("threads", "1"),
            ]

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    class DuckDB:
        def connect(
            self,
            *,
            database: str,
            config: dict[str, str],
        ) -> Connection:
            del database, config
            return connection

    with pytest.raises(
        ProductionLineageError,
        match="settings were not applied",
    ):
        _connect_pinned_duckdb(DuckDB())
    assert connection.closed is True


def test_production_main_requires_parquet_runtime_before_s3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    fixture, pins, spec, _reader = production_case()
    locator = spec["records"][0]["sources"][0]["locator"]
    locator.clear()
    locator.update(
        {
            "schema_version": "parquet-key-v2",
            "match": {"row_id": "row-1"},
            "source_fields": ["row_id"],
            "runtime_receipt_raw_sha256": "8" * 64,
        }
    )
    fixture_path = tmp_path / "fixture.json"
    pins_path = tmp_path / "pins.json"
    spec_path = tmp_path / "spec.json"
    for path, value in (
        (fixture_path, fixture),
        (pins_path, pins),
        (spec_path, spec),
    ):
        path.write_bytes(raw_json(value))
    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage."
        "validate_extractor_code_pin",
        lambda expected, require_root_read_only: expected,
    )

    def forbidden_reader(*args: object, **kwargs: object) -> None:
        raise AssertionError("missing runtime receipt reached S3")

    monkeypatch.setattr(
        "tools.research.pnl_spine.production_lineage."
        "W09ExactVersionS3Reader",
        forbidden_reader,
    )
    args = [
        "--fixture",
        str(fixture_path),
        "--fixture-sha256",
        hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "--manifest-pins",
        str(pins_path),
        "--manifest-pins-sha256",
        hashlib.sha256(pins_path.read_bytes()).hexdigest(),
        "--record-spec",
        str(spec_path),
        "--record-spec-sha256",
        hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        "--expected-extractor-code-sha256",
        "1" * 64,
        "--output",
        str(tmp_path / "receipt.json"),
    ]

    with pytest.raises(
        ProductionLineageError,
        match="requires runtime receipt path and raw SHA",
    ):
        main(args)
