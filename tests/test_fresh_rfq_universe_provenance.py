#!/usr/bin/env python3
"""Real-Parquet tests for fresh RFQ L1/L2 universe provenance."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import fresh_rfq_base_binding as binding  # noqa: E402
import fresh_rfq_universe_provenance as provenance  # noqa: E402
import research_reference as reference  # noqa: E402
from test_fresh_rfq_base_binding import (  # noqa: E402
    DATE,
    _complete_manifest,
    _raw_and_identity,
    _retarget_manifest,
)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _day_lo() -> int:
    return (dt.date.fromisoformat(DATE) - dt.date(1970, 1, 1)).days * 86_400_000_000


def _parquet_bytes(
    tmp_path: Path,
    name: str,
    rows: list[tuple],
    *,
    ts_type: str = "BIGINT",
    ticker_type: str = "VARCHAR",
) -> bytes:
    path = tmp_path / f"fixture-{name}.parquet"
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            f"CREATE TABLE data(ts_utc {ts_type}, market_ticker {ticker_type})"
        )
        if rows:
            connection.executemany("INSERT INTO data VALUES (?, ?)", rows)
        connection.execute("COPY data TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        connection.close()
    return path.read_bytes()


def _inputs(
    tmp_path: Path,
    *,
    l1_bodies: list[bytes] | None = None,
    l2_bodies: list[bytes] | None = None,
) -> dict:
    lo = _day_lo()
    l1_bodies = l1_bodies or [
        _parquet_bytes(
            tmp_path,
            "l1-a",
            [(lo, "KX-A"), (lo + 1, "KX-A"), (lo + 2, "KX-Case")],
        ),
        _parquet_bytes(
            tmp_path,
            "l1-b",
            [(lo + 3, "KX-B"), (lo + 4, "kx-a")],
        ),
    ]
    l2_bodies = l2_bodies or [
        _parquet_bytes(
            tmp_path,
            "l2-a",
            [(lo, "KX-A"), (lo + 1, "KX-L2")],
        ),
        _parquet_bytes(tmp_path, "l2-b", [(lo + 2, "KX-B")]),
    ]
    manifest = _complete_manifest()
    templates = {
        family: copy.deepcopy(next(
            row for row in manifest["objects"]
            if row.get("kind") == "facts" and row.get("channel") == family
        ))
        for family in provenance.FAMILIES
    }
    manifest["objects"] = [
        row for row in manifest["objects"]
        if not (
            row.get("kind") == "facts"
            and row.get("channel") in provenance.FAMILIES
        )
    ]
    exact_objects: dict[str, list[dict]] = {}
    for family, bodies in (
        ("orderbooks_l1", l1_bodies),
        ("orderbooks_full", l2_bodies),
    ):
        exact_objects[family] = []
        for index, body in enumerate(bodies):
            logical = (
                f"warehouse/facts/{family}/category=Sports/"
                f"subcategory=Part{index}/date={DATE}/part-{index}.parquet"
            )
            row = copy.deepcopy(templates[family])
            row.update({
                "logical_key": logical,
                "source_key": "ec2/" + logical,
                "source_version_id": f"version-{family}-{index}-{_sha(body)[:16]}",
                "size": len(body),
                "sha256": _sha(body),
            })
            manifest["objects"].append(row)
            exact_objects[family].append({
                "logical_key": logical,
                "bucket": row["source_bucket"],
                "key": row["source_key"],
                "version_id": row["source_version_id"],
                "size": row["size"],
                "sha256": row["sha256"],
                "body": body,
            })
    _retarget_manifest(manifest)
    reference.validate_manifest(manifest)
    raw, manifest_identity = _raw_and_identity(manifest)
    return {
        "manifest_bytes": raw,
        "manifest_exact_identity": manifest_identity,
        "date": DATE,
        "orderbooks_l1_objects": exact_objects["orderbooks_l1"],
        "orderbooks_full_objects": exact_objects["orderbooks_full"],
    }


def _build(tmp_path: Path, **overrides) -> dict:
    inputs = _inputs(tmp_path)
    inputs.update(overrides)
    return provenance.build_universe_provenance(**inputs)


def _assert_code(code: str, callable_, *args, **kwargs) -> None:
    with pytest.raises(provenance.FreshRfqUniverseProvenanceError) as exc_info:
        callable_(*args, **kwargs)
    assert exc_info.value.code == code


def test_real_parquet_happy_path_is_body_free_and_mapping_compatible(tmp_path):
    inputs = _inputs(tmp_path)
    result = provenance.build_universe_provenance(**inputs)
    rebuilt_base = binding.build_base_binding(
        manifest_bytes=inputs["manifest_bytes"],
        manifest_exact_identity=inputs["manifest_exact_identity"],
        date=DATE,
    )

    assert result["schema"] == "fresh-rfq-universe-provenance-v1"
    assert result["base_binding_sha256"] == rebuilt_base["binding_sha256"]
    assert result["families"]["orderbooks_l1"]["market_universe"] == [
        {"analysis_date": DATE, "market_ticker": "KX-A"},
        {"analysis_date": DATE, "market_ticker": "KX-B"},
        {"analysis_date": DATE, "market_ticker": "KX-Case"},
        {"analysis_date": DATE, "market_ticker": "kx-a"},
    ]
    assert result["families"]["orderbooks_l1"]["parquet_row_count"] == 5
    assert result["families"]["orderbooks_l1"][
        "cross_object_market_ticker_overlap_count"
    ] == 0
    assert result["families"]["orderbooks_full"]["market_universe"] == [
        {"analysis_date": DATE, "market_ticker": "KX-A"},
        {"analysis_date": DATE, "market_ticker": "KX-B"},
        {"analysis_date": DATE, "market_ticker": "KX-L2"},
    ]
    assert result["source_object_count"] == 4
    assert result["ephemeral_input_parquet_files_written"] == 4
    assert result["ephemeral_input_parquet_bytes_written"] == result[
        "source_total_bytes"
    ]
    assert result["extraction_contract"]["duckdb_spill_scope"] == (
        "PRIVATE_EPHEMERAL_SCRATCH"
    )
    assert result["ephemeral_temp_deleted_before_return"] is True
    assert result["all_input_bodies_omitted_from_output"] is True
    assert '"body"' not in json.dumps(result, sort_keys=True)
    assert result["source_objects_exact_get_verified"] is False
    assert result["aws_read_performed_by_module"] is False
    assert result["aws_write_authorized"] is False
    assert result["research_ready"] is False
    assert provenance.validate_universe_provenance(result, **inputs) == result


def test_object_input_permutation_is_deterministic(tmp_path):
    inputs = _inputs(tmp_path)
    first = provenance.build_universe_provenance(**inputs)
    permuted = copy.deepcopy(inputs)
    permuted["orderbooks_l1_objects"].reverse()
    permuted["orderbooks_full_objects"].reverse()
    second = provenance.build_universe_provenance(**permuted)
    assert second == first


@pytest.mark.parametrize("family", provenance.FAMILIES)
def test_missing_and_duplicate_object_fail_closed(tmp_path, family):
    inputs = _inputs(tmp_path)
    missing = copy.deepcopy(inputs)
    missing[f"{family}_objects"].pop()
    _assert_code(
        "BASE_FAMILY_SET_MISMATCH",
        provenance.build_universe_provenance,
        **missing,
    )

    duplicate = copy.deepcopy(inputs)
    duplicate[f"{family}_objects"].append(
        copy.deepcopy(duplicate[f"{family}_objects"][0])
    )
    _assert_code(
        "DUPLICATE_OBJECT",
        provenance.build_universe_provenance,
        **duplicate,
    )


def test_cross_family_and_version_identity_mismatch_fail_closed(tmp_path):
    inputs = _inputs(tmp_path)
    crossed = copy.deepcopy(inputs)
    crossed["orderbooks_l1_objects"][0] = copy.deepcopy(
        crossed["orderbooks_full_objects"][0]
    )
    _assert_code(
        "CROSS_FAMILY_OBJECT",
        provenance.build_universe_provenance,
        **crossed,
    )

    changed = copy.deepcopy(inputs)
    changed["orderbooks_l1_objects"][0]["version_id"] += "-other"
    _assert_code(
        "BASE_FAMILY_SET_MISMATCH",
        provenance.build_universe_provenance,
        **changed,
    )


@pytest.mark.parametrize("mutation", ["body", "size", "null_version", "extra"])
def test_body_and_identity_shapes_are_exact(tmp_path, mutation):
    inputs = _inputs(tmp_path)
    row = inputs["orderbooks_l1_objects"][0]
    expected = None
    if mutation == "body":
        row["body"] += b"x"
        expected = "BODY_SIZE_MISMATCH"
    elif mutation == "size":
        row["size"] = True
        expected = "INVALID_SIZE"
    elif mutation == "null_version":
        row["version_id"] = "null"
        expected = "VERSION_REQUIRED"
    else:
        row["extra"] = None
        expected = "SCHEMA_FIELDS"
    _assert_code(
        expected,
        provenance.build_universe_provenance,
        **inputs,
    )


def test_same_size_body_change_fails_sha_before_parquet_read(tmp_path):
    inputs = _inputs(tmp_path)
    row = inputs["orderbooks_l1_objects"][0]
    original = row["body"]
    row["body"] = bytes([original[0] ^ 1]) + original[1:]
    assert len(row["body"]) == row["size"]
    _assert_code(
        "BODY_SHA_MISMATCH",
        provenance.build_universe_provenance,
        **inputs,
    )


@pytest.mark.parametrize(
    ("ts_type", "ticker_type"),
    [("VARCHAR", "VARCHAR"), ("BIGINT", "BIGINT")],
)
def test_required_parquet_schema_is_exact(tmp_path, ts_type, ticker_type):
    lo = _day_lo()
    ts_value = str(lo) if ts_type == "VARCHAR" else lo
    ticker_value = 1 if ticker_type == "BIGINT" else "KX-A"
    bad = _parquet_bytes(
        tmp_path,
        f"bad-schema-{ts_type}-{ticker_type}",
        [(ts_value, ticker_value)],
        ts_type=ts_type,
        ticker_type=ticker_type,
    )
    inputs = _inputs(tmp_path, l1_bodies=[bad])
    _assert_code(
        "PARQUET_SCHEMA_INVALID",
        provenance.build_universe_provenance,
        **inputs,
    )


def test_invalid_parquet_has_no_format_fallback(tmp_path):
    inputs = _inputs(tmp_path, l1_bodies=[b"not-a-parquet-file"])
    _assert_code(
        "PARQUET_READ_FAILED",
        provenance.build_universe_provenance,
        **inputs,
    )


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ([(None, "KX-A")], "NULL_TS_UTC"),
        ([(_day_lo() - 1, "KX-A")], "CROSS_DATE_ROWS"),
        ([(_day_lo() + 86_400_000_000, "KX-A")], "CROSS_DATE_ROWS"),
        ([(_day_lo(), None)], "NULL_MARKET_TICKER"),
        ([(_day_lo(), " KX-A")], "INVALID_MARKET_TICKER"),
    ],
)
def test_date_null_and_identifier_contracts_fail_closed(tmp_path, rows, code):
    bad = _parquet_bytes(tmp_path, f"bad-{code}", rows)
    inputs = _inputs(tmp_path, l1_bodies=[bad])
    _assert_code(code, provenance.build_universe_provenance, **inputs)


def test_half_open_date_boundaries_are_accepted(tmp_path):
    lo = _day_lo()
    body = _parquet_bytes(
        tmp_path,
        "boundaries",
        [(lo, "KX-LO"), (lo + 86_400_000_000 - 1, "KX-HI")],
    )
    inputs = _inputs(tmp_path, l1_bodies=[body])
    result = provenance.build_universe_provenance(**inputs)
    assert {
        row["market_ticker"]
        for row in result["families"]["orderbooks_l1"]["market_universe"]
    } == {"KX-LO", "KX-HI"}


def test_tamper_and_rehash_still_fails_exact_rebuild(tmp_path):
    inputs = _inputs(tmp_path)
    result = provenance.build_universe_provenance(**inputs)
    result["families"]["orderbooks_l1"]["market_universe"][0][
        "market_ticker"
    ] = "KX-FORGED"
    unsigned = copy.deepcopy(result)
    unsigned.pop("provenance_sha256")
    result["provenance_sha256"] = provenance.canonical_sha256(unsigned)
    _assert_code(
        "PROVENANCE_REBUILD_MISMATCH",
        provenance.validate_universe_provenance,
        result,
        **inputs,
    )


def test_bool_to_int_tamper_and_full_rehash_fails_exact_rebuild(tmp_path):
    inputs = _inputs(tmp_path)
    result = provenance.build_universe_provenance(**inputs)
    result["research_ready"] = 0
    unsigned = copy.deepcopy(result)
    unsigned.pop("provenance_sha256")
    result["provenance_sha256"] = provenance.canonical_sha256(unsigned)
    _assert_code(
        "PROVENANCE_REBUILD_MISMATCH",
        provenance.validate_universe_provenance,
        result,
        **inputs,
    )


def test_private_scratch_is_cleaned_on_success_and_failure(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(provenance.tempfile, "tempdir", str(scratch))
    good = _inputs(tmp_path)
    provenance.build_universe_provenance(**good)
    assert list(scratch.iterdir()) == []

    bad_body = _parquet_bytes(
        tmp_path,
        "cleanup-failure",
        [(_day_lo(), " bad")],
    )
    bad = _inputs(tmp_path, l1_bodies=[bad_body])
    _assert_code(
        "INVALID_MARKET_TICKER",
        provenance.build_universe_provenance,
        **bad,
    )
    assert list(scratch.iterdir()) == []
