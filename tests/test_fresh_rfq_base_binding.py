#!/usr/bin/env python3
"""Focused pure tests for the fresh RFQ base exact-binding contract."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import fresh_rfq_base_binding as binding  # noqa: E402
import research_reference as reference  # noqa: E402
from test_research_reference_consumer import build_release  # noqa: E402


DATE = "2026-07-13"
PUBLISHED = "2026-07-14T04:00:00Z"
ANALYSIS_END = "2026-07-14T00:00:00Z"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _retarget_manifest(manifest: dict) -> None:
    manifest["objects"].sort(key=lambda row: row["logical_key"])
    reference_sha = reference.reference_set_sha256(manifest["objects"])
    semantics_sha = reference.object_semantics_sha256(manifest["objects"])
    manifest["reference_set_sha256"] = reference_sha
    manifest["object_semantics_sha256"] = semantics_sha
    state = manifest["publication_state"]
    state["reference_set_sha256"] = reference_sha
    state["object_semantics_sha256"] = semantics_sha
    state_sha = reference.canonical_sha256(state)
    manifest["publication_state_sha256"] = state_sha
    manifest["release_id"] = "%s__v3ref__seal-%s__pub-%s" % (
        manifest["date"],
        manifest["source_seal"]["sha256"][:8],
        state_sha[:16],
    )
    manifest["version_binding"]["bindings_sha256"] = reference_sha
    manifest["post_upload_verification"]["objects_verified"] = len(
        manifest["objects"]
    )


def _complete_manifest(*, with_rfq: bool = False) -> dict:
    _rid, manifest, _source = build_release(with_rfq=with_rfq)
    template = next(
        row for row in manifest["objects"] if row["kind"] == "catalog"
    )
    for name in ("settlements", "series_classified"):
        logical = f"warehouse/catalog/{name}/part-00000.parquet"
        source = f"ec2/warehouse/catalog/{name}/part-00000.parquet"
        payload = f"catalog-{name}".encode("utf-8")
        row = copy.deepcopy(template)
        row.update(
            {
                "logical_key": logical,
                "source_key": source,
                "source_version_id": f"version-{_sha(source.encode())[:20]}",
                "size": len(payload),
                "sha256": _sha(payload),
            }
        )
        manifest["objects"].append(row)
    _retarget_manifest(manifest)
    reference.validate_manifest(manifest)
    return manifest


def _raw_and_identity(manifest: dict) -> tuple[bytes, dict]:
    raw = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    identity = {
        "bucket": reference.TRUSTED_BUCKET,
        "key": f"research/releases/{manifest['release_id']}/MANIFEST.json",
        "version_id": "manifest-version-exact",
        "size": len(raw),
        "sha256": _sha(raw),
    }
    return raw, identity


def _build(manifest: dict | None = None) -> dict:
    manifest = manifest or _complete_manifest()
    raw, identity = _raw_and_identity(manifest)
    return binding.build_base_binding(
        manifest_bytes=raw,
        manifest_exact_identity=identity,
        date=DATE,
    )


def test_builds_body_free_exact_binding_for_all_required_base_families():
    one = _build()
    two = _build()
    assert one == two
    assert one["schema"] == "fresh-rfq-base-binding-v1"
    assert one["verification_state"] == (
        "REFERENCE_V3_MANIFEST_EXACT_VALIDATED"
    )
    assert one["source_objects_exact_get_verified"] is False
    assert one["durable_receipt_exact_get_verified"] is False
    assert one["as_of_cutoff_utc"] == PUBLISHED
    assert one["analysis_data_end_utc"] == ANALYSIS_END
    assert one["data_objects_copied"] == 0
    assert one["aws_write_authorized"] is False
    assert one["research_eligible"] is False
    assert one["rfq_objects_in_base"] == 0
    assert one["base_object_count"] == 11
    assert set(one["families"]) == {
        "orderbooks_l1",
        "orderbooks_full",
        "trades",
        "dated_dim",
        "catalogs",
    }
    assert one["families"]["dated_dim"]["object_count"] == 3
    assert one["families"]["catalogs"]["object_count"] == 5
    assert one["source_seal"]["version_id"]
    assert one["canonical_receipt"]["receipt_set_sha256"]
    assert one["canonical_receipt"]["receipt_object"]["version_id"]
    for family in one["families"].values():
        assert len(family["set_sha256"]) == 64
        for row in family["objects"]:
            assert set(row) == {
                "logical_key",
                "bucket",
                "key",
                "version_id",
                "size",
                "sha256",
            }
    unsigned = copy.deepcopy(one)
    supplied = unsigned.pop("binding_sha256")
    assert supplied == binding.canonical_sha256(unsigned)


@pytest.mark.parametrize(
    "family",
    ["orderbooks_l1", "orderbooks_full", "trades"],
)
def test_rejects_missing_fact_family_even_when_reference_v3_still_valid(family):
    manifest = _complete_manifest()
    manifest["objects"] = [
        row
        for row in manifest["objects"]
        if not (row["kind"] == "facts" and row["channel"] == family)
    ]
    manifest["tables"].pop(family)
    channel_name = "orderbooks_l2" if family == "orderbooks_full" else family
    manifest["channels"][channel_name]["status"] = "ABSENT_FROM_THIS_RELEASE"
    if family == "orderbooks_full":
        manifest["objects"] = [
            row
            for row in manifest["objects"]
            if row["kind"] != "l2_quality_receipt"
        ]
        manifest["l2_quality"] = None
        manifest["channels"]["orderbooks_l2"]["seq_quality"] = None
        manifest["publication_state"]["l2_quality_digest"] = (
            reference.canonical_sha256(None)
        )
        manifest["publication_components"]["gap_evidence"][
            "l2_gaps_sha256"
        ] = None
        manifest["publication_state"]["gap_evidence_digest"] = (
            reference.canonical_sha256(
                manifest["publication_components"]["gap_evidence"]
            )
        )
    _retarget_manifest(manifest)
    reference.validate_manifest(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        _build(manifest)
    assert error.value.code == "BASE_FAMILY_MISSING"


@pytest.mark.parametrize(
    "catalog",
    [
        "series/part-00000.parquet",
        "events/part-00000.parquet",
        "markets/part-00000.parquet",
        "settlements/part-00000.parquet",
        "series_classified/part-00000.parquet",
    ],
)
def test_rejects_any_missing_member_of_the_five_catalog_set(catalog):
    manifest = _complete_manifest()
    logical = f"warehouse/catalog/{catalog}"
    manifest["objects"] = [
        row for row in manifest["objects"] if row["logical_key"] != logical
    ]
    _retarget_manifest(manifest)
    if catalog in {
        "series/part-00000.parquet",
        "events/part-00000.parquet",
        "markets/part-00000.parquet",
    }:
        with pytest.raises(binding.FreshRfqBaseBindingError) as error:
            _build(manifest)
        assert error.value.code == "REFERENCE_MANIFEST_INVALID"
    else:
        reference.validate_manifest(manifest)
        with pytest.raises(binding.FreshRfqBaseBindingError) as error:
            _build(manifest)
        assert error.value.code == "BASE_FAMILY_MISSING"


def test_rejects_rfq_objects_instead_of_silently_filtering_them():
    manifest = _complete_manifest(with_rfq=True)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        _build(manifest)
    assert error.value.code == "RFQ_BASE_CONTAMINATION"


def test_rejects_wrong_object_date_and_null_source_version():
    manifest = _complete_manifest()
    fact = next(row for row in manifest["objects"] if row["kind"] == "facts")
    fact["date"] = "2026-07-12"
    raw, identity = _raw_and_identity(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        binding.build_base_binding(
            manifest_bytes=raw,
            manifest_exact_identity=identity,
            date=DATE,
        )
    assert error.value.code == "REFERENCE_MANIFEST_INVALID"

    manifest = _complete_manifest()
    next(row for row in manifest["objects"] if row["kind"] == "facts")[
        "source_version_id"
    ] = "null"
    raw, identity = _raw_and_identity(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        binding.build_base_binding(
            manifest_bytes=raw,
            manifest_exact_identity=identity,
            date=DATE,
        )
    assert error.value.code == "REFERENCE_MANIFEST_INVALID"


def test_rejects_hidden_second_date_segment_in_otherwise_valid_fact_path():
    manifest = _complete_manifest()
    fact = next(row for row in manifest["objects"] if row["kind"] == "facts")
    suffix = "/date=2026-07-12/part.csv"
    fact["logical_key"] = fact["logical_key"].replace("/part.csv", suffix)
    fact["source_key"] = fact["source_key"].replace("/part.csv", suffix)
    _retarget_manifest(manifest)
    # The shared v3 validator permits the historical path shape because D is
    # present.  The fresh overlay contract is deliberately stricter: exactly
    # one date partition is allowed and it must be D.
    reference.validate_manifest(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        _build(manifest)
    assert error.value.code == "CROSS_DATE_OBJECT"


def test_rejects_duplicate_and_internal_reference_digest_mismatch():
    manifest = _complete_manifest()
    manifest["objects"].append(copy.deepcopy(manifest["objects"][0]))
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        _build(manifest)
    assert error.value.code == "REFERENCE_MANIFEST_INVALID"

    manifest = _complete_manifest()
    manifest["reference_set_sha256"] = "0" * 64
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        _build(manifest)
    assert error.value.code == "REFERENCE_MANIFEST_INVALID"


@pytest.mark.parametrize("mutation", ["sha256", "size", "version", "key"])
def test_rejects_manifest_exact_identity_mismatch(mutation):
    manifest = _complete_manifest()
    raw, identity = _raw_and_identity(manifest)
    if mutation == "sha256":
        identity["sha256"] = "0" * 64
    elif mutation == "size":
        identity["size"] += 1
    elif mutation == "version":
        identity["version_id"] = "null"
    else:
        identity["key"] = "research/releases/wrong/MANIFEST.json"
    with pytest.raises(binding.FreshRfqBaseBindingError):
        binding.build_base_binding(
            manifest_bytes=raw,
            manifest_exact_identity=identity,
            date=DATE,
        )


def test_rejects_wrong_requested_date_and_cutoff_before_publication():
    manifest = _complete_manifest()
    raw, identity = _raw_and_identity(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        binding.build_base_binding(
            manifest_bytes=raw,
            manifest_exact_identity=identity,
            date="2026-07-12",
        )
    assert error.value.code == "DATE_MISMATCH"
    for arbitrary_cutoff in (
        "2026-07-14T03:59:59Z",
        "2099-01-01T00:00:00Z",
    ):
        with pytest.raises(binding.FreshRfqBaseBindingError) as error:
            binding.build_base_binding(
                manifest_bytes=raw,
                manifest_exact_identity=identity,
                date=DATE,
                as_of_cutoff_utc=arbitrary_cutoff,
            )
        assert error.value.code == "CUTOFF_INVALID"


def test_accepts_only_redundant_as_of_equal_to_exact_manifest_publication():
    manifest = _complete_manifest()
    raw, identity = _raw_and_identity(manifest)
    result = binding.build_base_binding(
        manifest_bytes=raw,
        manifest_exact_identity=identity,
        date=DATE,
        as_of_cutoff_utc=PUBLISHED,
    )
    assert result["as_of_cutoff_utc"] == manifest["published_at_utc"]


def test_rejects_manifest_published_before_the_complete_analysis_day():
    manifest = _complete_manifest()
    manifest["published_at_utc"] = "2026-07-13T23:59:59Z"
    raw, identity = _raw_and_identity(manifest)
    with pytest.raises(binding.FreshRfqBaseBindingError) as error:
        binding.build_base_binding(
            manifest_bytes=raw,
            manifest_exact_identity=identity,
            date=DATE,
        )
    assert error.value.code == "CUTOFF_INVALID"
