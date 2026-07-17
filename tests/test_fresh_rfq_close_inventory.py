#!/usr/bin/env python3
"""Pure tests for the D+1 fresh-RFQ close-family inventory snapshot."""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fresh_rfq_close_inventory as inventory  # noqa: E402


DATE = "2026-07-13"
NEXT_DATE = "2026-07-14"
PREFIX = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"


def _version(key: str, version_id: str, size: int, latest: bool) -> dict:
    return {
        "key": key,
        "version_id": version_id,
        "is_latest": latest,
        "size": size,
    }


def _delete(key: str, version_id: str, latest: bool = False) -> dict:
    return {
        "key": key,
        "version_id": version_id,
        "is_latest": latest,
    }


def _identity(key: str, version_id: str, size: int) -> dict:
    return {
        "bucket": inventory.SOURCE_BUCKET,
        "key": key,
        "version_id": version_id,
        "size": size,
        "sha256": hashlib.sha256(
            f"{key}\0{version_id}\0{size}".encode("utf-8")
        ).hexdigest(),
    }


def _page(
    *,
    request=(None, None),
    response=None,
    truncated=False,
    next_marker=(None, None),
    versions=None,
    delete_markers=None,
) -> dict:
    if response is None:
        response = request
    return {
        "request_key_marker": request[0],
        "request_version_id_marker": request[1],
        "response_key_marker": response[0],
        "response_version_id_marker": response[1],
        "is_truncated": truncated,
        "next_key_marker": next_marker[0],
        "next_version_id_marker": next_marker[1],
        "versions": versions or [],
        "delete_markers": delete_markers or [],
    }


def _fixture() -> tuple[list[dict], list[dict]]:
    base = PREFIX
    shard1 = PREFIX + ".1"
    shard2 = PREFIX + ".2"
    continuation = (shard1, "old-shard-1")
    pages = [
        _page(
            truncated=True,
            next_marker=continuation,
            versions=[
                _version(shard1, "old-shard-1", 4, False),
                _version(base, "base-current", 101, True),
                _version(base, "base-old", 99, False),
            ],
        ),
        _page(
            request=continuation,
            versions=[
                _version(shard2, "shard-2-current", 303, True),
                _version(shard1, "shard-1-current", 202, True),
            ],
            delete_markers=[_delete(shard1, "old-delete")],
        ),
    ]
    identities = [
        _identity(shard2, "shard-2-current", 303),
        _identity(base, "base-current", 101),
        _identity(shard1, "shard-1-current", 202),
    ]
    return pages, identities


def _build(
    pages: list[dict] | None = None, identities: list[dict] | None = None
) -> dict:
    fixture_pages, fixture_identities = _fixture()
    return inventory.build_close_inventory_snapshot(
        analysis_date=DATE,
        pages=fixture_pages if pages is None else pages,
        exact_identities=fixture_identities if identities is None else identities,
        bucket=inventory.SOURCE_BUCKET,
        prefix=PREFIX,
    )


def _code(error: pytest.ExceptionInfo) -> str:
    return error.value.code


def test_builds_deterministic_body_free_multishard_snapshot():
    pages, identities = _fixture()
    result = _build(pages, identities)

    assert result["schema"] == "fresh-rfq-close-inventory-snapshot-v1"
    assert result["state"] == (
        "CALLER_PROJECTION_VALIDATED_AWS_LIST_ATTESTATION_REQUIRED"
    )
    assert result["analysis_date"] == DATE
    assert result["inventory_date"] == NEXT_DATE
    assert result["bucket"] == inventory.SOURCE_BUCKET
    assert result["prefix"] == PREFIX
    assert result["page_count"] == 2
    assert result["version_entry_count"] == 5
    assert result["delete_marker_entry_count"] == 1
    assert result["family_key_count"] == 3
    assert result["latest_version_count"] == 3
    assert result["latest_shard_ordinals"] == [0, 1, 2]
    assert result["caller_projection_marker_chain_terminated"] is True
    assert result["all_family_keys_have_exactly_one_latest"] is True
    assert result["latest_delete_marker_count"] == 0
    assert result["latest_keys_contiguous_from_base"] is True
    assert result["input_bodies_omitted"] is True
    assert result["data_objects_copied"] == 0
    assert result["module_write_api_call_count"] == 0
    assert result["aws_list_performed_by_module"] is False
    assert result["aws_inventory_transport_verified"] is False
    assert result["requires_external_aws_list_attestation"] is True
    assert result["research_ready"] is False
    assert result["snapshot_sha256"] == inventory.canonical_sha256(
        {key: value for key, value in result.items() if key != "snapshot_sha256"}
    )
    assert "body" not in repr(result).lower()
    inventory.validate_close_inventory_snapshot(result)

    permuted_pages = copy.deepcopy(pages)
    for page in permuted_pages:
        page["versions"].reverse()
        page["delete_markers"].reverse()
    assert _build(permuted_pages, list(reversed(identities))) == result


def test_accepts_single_base_object_and_derives_prefix_when_omitted():
    base = PREFIX
    pages = [_page(versions=[_version(base, "only-version", 1, True)])]
    identities = [_identity(base, "only-version", 1)]
    result = inventory.build_close_inventory_snapshot(
        analysis_date=DATE,
        pages=pages,
        exact_identities=identities,
    )
    assert result["latest_shard_ordinals"] == [0]
    assert result["prefix"] == PREFIX


def test_rejects_truncated_tail():
    pages, identities = _fixture()
    pages.pop()
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "TRUNCATED_TAIL"


def test_rejects_marker_chain_discontinuity():
    pages, identities = _fixture()
    pages[1]["request_version_id_marker"] = "different"
    pages[1]["response_version_id_marker"] = "different"
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "MARKER_CHAIN_BROKEN"


def test_rejects_response_marker_that_does_not_echo_request():
    pages, identities = _fixture()
    pages[1]["response_version_id_marker"] = "different"
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "RESPONSE_MARKER_MISMATCH"


def test_rejects_marker_loop():
    pages, identities = _fixture()
    pages[1]["is_truncated"] = True
    pages[1]["next_key_marker"] = pages[1]["request_key_marker"]
    pages[1]["next_version_id_marker"] = pages[1]["request_version_id_marker"]
    pages.append(
        _page(
            request=(pages[1]["next_key_marker"], pages[1]["next_version_id_marker"]),
            versions=[_version(PREFIX + ".2", "extra", 3, False)],
        )
    )
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "MARKER_LOOP"


def test_rejects_regressing_marker_and_out_of_page_key_range():
    pages, identities = _fixture()
    pages[0]["next_key_marker"] = PREFIX + ".2"
    pages[0]["next_version_id_marker"] = "forward"
    pages[1]["request_key_marker"] = PREFIX + ".2"
    pages[1]["request_version_id_marker"] = "forward"
    pages[1]["response_key_marker"] = PREFIX + ".2"
    pages[1]["response_version_id_marker"] = "forward"
    pages[1]["is_truncated"] = True
    pages[1]["next_key_marker"] = PREFIX + ".1"
    pages[1]["next_version_id_marker"] = "backward"
    pages.append(
        _page(
            request=(PREFIX + ".1", "backward"),
            versions=[_version(PREFIX + ".2", "tail", 1, False)],
        )
    )
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "MARKER_NOT_FORWARD"

    pages, identities = _fixture()
    pages[0]["versions"].append(
        _version(PREFIX + ".2", "too-far-for-page", 1, False)
    )
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "PAGE_ENTRY_RANGE"


@pytest.mark.parametrize(
    "bad_key",
    [
        PREFIX + ".0",
        PREFIX + ".01",
        PREFIX + ".x",
        PREFIX + ".1.extra",
        f"ec2/raw/date={DATE}/rfq_receipts_02.ndjson",
        f"ec2/raw/date={NEXT_DATE}/rfq_receipts_01.ndjson",
    ],
)
def test_rejects_bad_family_suffix_or_scope(bad_key):
    pages, identities = _fixture()
    pages[0]["versions"][0]["key"] = bad_key
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "FAMILY_KEY_MISMATCH"


def test_rejects_latest_ordinal_gap():
    pages, identities = _fixture()
    shard1 = PREFIX + ".1"
    for page in pages:
        page["versions"] = [
            row for row in page["versions"] if row["key"] != shard1
        ]
        page["delete_markers"] = [
            row for row in page["delete_markers"] if row["key"] != shard1
        ]
    identities = [row for row in identities if row["key"] != shard1]
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "SHARD_ORDINAL_GAP"


def test_rejects_huge_shard_ordinal_without_native_exception():
    pages, identities = _fixture()
    pages[0]["versions"][0]["key"] = PREFIX + ".100000000000000000000"
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "SHARD_ORDINAL_INVALID"


def test_rejects_missing_base_even_if_shard_one_exists():
    pages, identities = _fixture()
    pages[0]["versions"] = [
        row for row in pages[0]["versions"] if row["key"] != PREFIX
    ]
    identities = [row for row in identities if row["key"] != PREFIX]
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "BASE_SHARD_MISSING"


def test_rejects_latest_delete_marker():
    pages, identities = _fixture()
    shard1 = PREFIX + ".1"
    for page in pages:
        for row in page["versions"]:
            if row["key"] == shard1:
                row["is_latest"] = False
    pages[1]["delete_markers"][0]["is_latest"] = True
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "LATEST_IS_DELETE_MARKER"


@pytest.mark.parametrize("container", ["versions", "delete_markers"])
def test_rejects_null_version_ids(container):
    pages, identities = _fixture()
    pages[1][container][0]["version_id"] = "null"
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "VERSION_ID_REQUIRED"


def test_rejects_multiple_latest_entries_for_one_key():
    pages, identities = _fixture()
    pages[0]["versions"][2]["is_latest"] = True
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "LATEST_CARDINALITY"


def test_rejects_duplicate_entry_across_version_and_delete_collections():
    pages, identities = _fixture()
    duplicate = pages[0]["versions"][0]
    pages[1]["delete_markers"].append(
        _delete(duplicate["key"], duplicate["version_id"])
    )
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "DUPLICATE_ENTRY"


@pytest.mark.parametrize("mutation", ["key", "version_id", "size", "missing", "extra"])
def test_rejects_exact_identity_set_mismatch(mutation):
    pages, identities = _fixture()
    if mutation == "key":
        identities[0]["key"] = PREFIX + ".3"
    elif mutation == "version_id":
        identities[0]["version_id"] = "wrong-version"
    elif mutation == "size":
        identities[0]["size"] += 1
    elif mutation == "missing":
        identities.pop()
    else:
        identities.append(_identity(PREFIX + ".3", "extra", 1))
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "IDENTITY_SET_MISMATCH"


def test_rejects_invalid_identity_sha_bucket_and_duplicate_key():
    pages, identities = _fixture()
    invalid_sha = copy.deepcopy(identities)
    invalid_sha[0]["sha256"] = "A" * 64
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, invalid_sha)
    assert _code(error) == "INVALID_SHA256"

    wrong_bucket = copy.deepcopy(identities)
    wrong_bucket[0]["bucket"] = "other-bucket"
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, wrong_bucket)
    assert _code(error) == "BUCKET_MISMATCH"

    duplicate = copy.deepcopy(identities)
    duplicate.append(copy.deepcopy(duplicate[0]))
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, duplicate)
    assert _code(error) == "DUPLICATE_IDENTITY"


def test_rejects_wrong_prefix_and_noncanonical_date():
    pages, identities = _fixture()
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        inventory.build_close_inventory_snapshot(
            analysis_date=DATE,
            pages=pages,
            exact_identities=identities,
            prefix=PREFIX + ".1",
        )
    assert _code(error) == "PREFIX_MISMATCH"

    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        inventory.build_close_inventory_snapshot(
            analysis_date="2026-02-30",
            pages=pages,
            exact_identities=identities,
        )
    assert _code(error) == "INVALID_DATE"

    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        inventory.build_close_inventory_snapshot(
            analysis_date="9999-12-31",
            pages=pages,
            exact_identities=identities,
        )
    assert _code(error) == "INVALID_DATE"


def test_rejects_snapshot_mutation_even_with_recomputed_digest():
    result = _build()
    mutated = copy.deepcopy(result)
    mutated["research_ready"] = True
    mutated["snapshot_sha256"] = inventory.canonical_sha256(
        {key: value for key, value in mutated.items() if key != "snapshot_sha256"}
    )
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        inventory.validate_close_inventory_snapshot(mutated)
    assert _code(error) == "SNAPSHOT_DERIVATION_MISMATCH"


def test_rejects_page_after_nontruncated_final_and_empty_page():
    pages, identities = _fixture()
    pages[0]["is_truncated"] = False
    pages[0]["next_key_marker"] = None
    pages[0]["next_version_id_marker"] = None
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "PAGE_AFTER_FINAL"

    pages, identities = _fixture()
    pages[0]["versions"] = []
    with pytest.raises(inventory.FreshRfqCloseInventoryError) as error:
        _build(pages, identities)
    assert _code(error) == "EMPTY_PAGE"
