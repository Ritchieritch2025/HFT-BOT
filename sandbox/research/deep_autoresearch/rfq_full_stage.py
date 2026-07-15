#!/usr/bin/env python3
"""Full RFQ exploratory stage for SPORTS-AUTORESEARCH-01.

The stage scans every RFQ recorder row in the two operator-approved releases,
but keeps the RFQ-to-CLOB expansion bounded with a deterministic root-event
sample.  It is deliberately separate from ``run_cycle1.py`` so a long raw
JSON scan can be resumed without changing the frozen registration or the run
manifest.

Safety and inference boundaries:

* local files only; no network, API, S3 write, order, quote, or RFQ action;
* exact release allow-list and locally VERSION_BOUND verification markers;
* lifecycle and event studies use the outer-envelope receive clock;
* lifecycle joins stop at the earlier of the next genuine same-ID create or an
  explicit observation boundary; only uninterrupted residuals censor at scan end;
* requester identifiers are salted+hashed before any persistent output;
* no acceptance, fill, quote-price, competitiveness, or RFQ-PnL inference;
* all outputs are EXPLORATORY_ONLY / DIAGNOSTIC_ONLY.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Sequence


RELEASE_IDS = (
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5",
)
EXPECTED_INSTANCE = "i-0e53d134dceffe166"
EXPECTED_REGION = "us-east-2"
EXPECTED_ROLE = "w09-research-runner"
EXPECTED_DUCKDB = "1.4.5"
EXPECTED_W09_INSTALLATION_SHA256 = {
    "/usr/local/bin/research_data": "68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f",
    "/opt/w09/research/tools/research_data_instance_profile.py": "af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf",
    "/usr/local/bin/w09-run": "6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321",
    "/etc/w09/cost-contract.json": "bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352",
}
EXPECTED_MISSION_SHA256 = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
BANNER = "EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION"
EVIDENCE = "SEALED_DEGRADED_EVIDENCE"
EXPECTED_MANIFEST_SHA256 = {
    RELEASE_IDS[0]: "6fedbd5d2b0d811b5189a953331bd236aeea3fb5843a5a549d1100d5ee290505",
    RELEASE_IDS[1]: "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd",
}
WINDOWS_US = (
    ("m30_m10", -30_000_000, -10_000_000),
    ("m10_m1", -10_000_000, -1_000_000),
    ("m1_0", -1_000_000, 0),
    ("p0_100ms", 0, 100_000),
    ("p100ms_1s", 100_000, 1_000_000),
    ("p1s_3s", 1_000_000, 3_000_000),
    ("p3s_10s", 3_000_000, 10_000_000),
    ("p10s_30s", 10_000_000, 30_000_000),
    ("p30s_120s", 30_000_000, 120_000_000),
)
BOOK_AGE_CAP_US = 5_000_000
CONTROL_MIN_SHIFT_US = 300_000_000
CONTROL_SHIFT_SPAN_US = 300_000_000
CONTROL_CAUSAL_LOOKBACK_US = 120_000_000


class RFQStageError(RuntimeError):
    """Fail-closed RFQ-stage error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def path_list(paths: Sequence[Path]) -> str:
    if not paths:
        raise RFQStageError("empty path list")
    return "[" + ",".join(quote(path) for path in paths) + "]"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(
        value, indent=2, sort_keys=True, default=str, ensure_ascii=False
    ) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def fixed_exact(value: object, places: int) -> int | None:
    """Convert an exact fixed-point value without rounding.

    This pure helper mirrors the SQL expression used in the full scan and is
    intentionally strict: excess non-zero decimal places, non-finite values,
    booleans, and BIGINT overflow are rejected.
    """
    from decimal import Decimal, InvalidOperation

    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not raw.is_finite():
        return None
    scaled = raw * (Decimal(10) ** places)
    if scaled != scaled.to_integral_value():
        return None
    result = int(scaled)
    return result if -(2**63) <= result <= 2**63 - 1 else None


def fixed_sql(expression: str, places: int) -> str:
    """DuckDB SQL equivalent of :func:`fixed_exact`."""
    scale = 10**places
    decimal = f"try_cast(({expression}) AS DECIMAL(38,10))"
    scaled = f"(({decimal})*{scale})"
    return (
        "CASE WHEN "
        f"{decimal} IS NOT NULL AND {scaled}=trunc({scaled}) "
        f"AND abs({scaled})<=9223372036854775807 "
        f"THEN cast({scaled} AS BIGINT) END"
    )


def requester_hash(value: str | None, run_id: str) -> str | None:
    """Return a run-scoped pseudonym; never persist ``value`` itself."""
    if not value:
        return None
    material = f"SPORTS-AUTORESEARCH-01|{run_id}|{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def validate_windows(windows: Sequence[tuple[str, int, int]] = WINDOWS_US) -> None:
    labels = set()
    previous_end = None
    for label, start, end in windows:
        if label in labels or not label:
            raise RFQStageError("duplicate/empty event-study window label")
        if start >= end:
            raise RFQStageError(f"invalid event-study window: {label}")
        if previous_end is not None and start != previous_end:
            raise RFQStageError("event-study windows must be contiguous and ordered")
        labels.add(label)
        previous_end = end


def kaplan_meier_from_counts(
    counts: Iterable[tuple[int, int, int]],
) -> list[tuple[int, int, int, int, float]]:
    """Return ``time, at_risk, deaths, censored, survival`` from grouped counts."""
    rows = sorted((int(t), int(d), int(c)) for t, d, c in counts if t >= 0)
    if any(d < 0 or c < 0 for _, d, c in rows):
        raise ValueError("negative KM count")
    at_risk = sum(d + c for _, d, c in rows)
    survival = 1.0
    output = []
    for duration, deaths, censored in rows:
        if deaths > at_risk:
            raise ValueError("KM deaths exceed risk set")
        if deaths:
            survival *= 1.0 - deaths / at_risk
        output.append((duration, at_risk, deaths, censored, survival))
        at_risk -= deaths + censored
    return output


def table_exists(connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [name],
        ).fetchone()[0]
    )


def scalar(connection, sql: str, parameters: Sequence[object] | None = None):
    return connection.execute(sql, parameters or []).fetchone()[0]


def rows_as_dicts(connection, sql: str) -> list[dict]:
    cursor = connection.execute(sql)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def discover_inputs(cache_root: Path) -> dict:
    identities = []
    by_object_key: dict[str, dict] = {}
    logical_bindings = 0
    for release_id in RELEASE_IDS:
        base = cache_root / "releases" / release_id
        manifest = base / "MANIFEST.json"
        marker_path = base / ".VERIFIED.json"
        if not manifest.is_file() or not marker_path.is_file():
            raise RFQStageError(f"release is not locally verified: {release_id}")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("release_id") != release_id:
            raise RFQStageError(f"verification identity mismatch: {release_id}")
        if marker.get("version_binding_mode") != "VERSION_BOUND":
            raise RFQStageError(f"release is not VERSION_BOUND: {release_id}")
        if marker.get("evidence_tier") != EVIDENCE:
            raise RFQStageError(f"unexpected evidence tier: {release_id}")
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        objects = [
            row for row in manifest_value.get("objects", [])
            if isinstance(row, dict) and str(row.get("key", "")).startswith("raw_rfq/")
        ]
        if not objects:
            raise RFQStageError(f"RFQ input absent: {release_id}")
        release_bytes = 0
        for row in objects:
            logical_bindings += 1
            key = str(row.get("key", ""))
            digest = str(row.get("sha256", ""))
            size = row.get("size")
            if not key.startswith("raw_rfq/") or len(digest) != 64 or type(size) is not int:
                raise RFQStageError(f"invalid RFQ manifest object: {release_id}:{key}")
            path = base / key
            if not path.is_file():
                raise RFQStageError(f"manifest-bound RFQ object missing: {release_id}:{key}")
            if path.stat().st_size != size:
                raise RFQStageError(f"manifest-bound RFQ size mismatch: {release_id}:{key}")
            release_bytes += size
            prior = by_object_key.get(key)
            if prior is not None:
                if prior["sha256"] != digest or prior["size"] != size:
                    raise RFQStageError(
                        f"overlapping RFQ object key has conflicting bytes: {key}"
                    )
                prior["bound_release_ids"].append(release_id)
                continue
            by_object_key[key] = {
                "key": key, "sha256": digest, "size": size, "path": path,
                "bound_release_ids": [release_id],
            }
        identities.append(
            {
                "release_id": release_id,
                "verified_marker_sha256": sha256(marker_path),
                "manifest_sha256": sha256(manifest),
                "rfq_objects": len(objects),
                "rfq_bytes": release_bytes,
            }
        )
    objects = [by_object_key[key] for key in sorted(by_object_key)]
    all_paths = [row["path"] for row in objects]
    overlaps = [row for row in objects if len(row["bound_release_ids"]) > 1]
    payload = {
        "release_ids": list(RELEASE_IDS),
        "releases": identities,
        "paths": all_paths,
        "objects_detail": objects,
        "objects": len(all_paths),
        "logical_manifest_bindings": logical_bindings,
        "deduplicated_overlapping_objects": len(overlaps),
        "overlap_keys": [row["key"] for row in overlaps],
        "bytes": sum(row["size"] for row in objects),
    }
    fingerprint_rows = [
        f"{row['key']}\t{row['size']}\t{row['sha256']}" for row in objects
    ]
    payload["path_size_fingerprint_sha256"] = hashlib.sha256(
        "\n".join(fingerprint_rows).encode("utf-8")
    ).hexdigest()
    return payload


def validate_run(run_dir: Path) -> dict:
    manifest_path = run_dir / "RUN_MANIFEST.json"
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    if not manifest_path.is_file() or not attestation_path.is_file():
        raise RFQStageError("run manifest or W09 attestation missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_dir.name:
        raise RFQStageError("run id/path mismatch")
    if manifest.get("mode") != "EXPLORATORY_AUTORESEARCH":
        raise RFQStageError("RFQ stage requires MODE 1")
    if manifest.get("mission", {}).get("sha256") != EXPECTED_MISSION_SHA256:
        raise RFQStageError("approved mission SHA mismatch")
    if not manifest.get("explicit_degraded_admission"):
        raise RFQStageError("SEALED_DEGRADED_EVIDENCE was not explicitly admitted")
    if not manifest.get("analysis_started"):
        raise RFQStageError("analysis gate has not been opened")
    if manifest.get("status") not in {
        "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "CYCLE1_CORE_RUNNING",
    }:
        raise RFQStageError("Cycle-1 core is not in an RFQ-stage-compatible state")
    gates = manifest.get("gates", {})
    if not str(gates.get("gate_a", {}).get("status", "")).startswith("PASS_"):
        raise RFQStageError("Gate A is not PASS")
    if not str(gates.get("gate_b", {}).get("status", "")).startswith("PASS_"):
        raise RFQStageError("Gate B is not PASS")
    if (not str(gates.get("gate_c", {}).get("status", "")).startswith("PASS_")
            or gates.get("gate_c", {}).get("mode2_authorized") is not False):
        raise RFQStageError("Gate C is not bound to MODE 1 only")
    if gates.get("gate_b", {}).get("attestation_sha256") != sha256(attestation_path):
        raise RFQStageError("Gate B attestation SHA mismatch")
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    if (
        attestation.get("instance_id") != EXPECTED_INSTANCE
        or attestation.get("region") != EXPECTED_REGION
        or attestation.get("role") != EXPECTED_ROLE
        or attestation.get("instance_profile") != EXPECTED_ROLE
        or attestation.get("architecture") != "aarch64"
        or attestation.get("w09_run_inhibitor_present") is not True
        or attestation.get("w09_run_inhibitor_is_ancestor") is not True
        or attestation.get("static_credentials_present") is not False
        or attestation.get("trading_credentials_present") is not False
        or attestation.get("ambient_aws_or_kalshi_variables") != []
        or attestation.get("static_credential_paths_present") != []
        or attestation.get("installation_sha256") != EXPECTED_W09_INSTALLATION_SHA256
        or attestation.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"
        or attestation.get("duckdb") != EXPECTED_DUCKDB
    ):
        raise RFQStageError("W09 identity/isolation attestation mismatch")
    selected = manifest.get("selected_releases")
    if (not isinstance(selected, list) or len(selected) != len(RELEASE_IDS)
            or {row.get("release_id") for row in selected} != set(RELEASE_IDS)):
        raise RFQStageError("selected release set mismatch")
    for row in selected:
        release_id = row["release_id"]
        expected = EXPECTED_MANIFEST_SHA256[release_id]
        manifest_copy = run_dir / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
        if (row.get("evidence_tier") != EVIDENCE
                or row.get("include") != "EXPLORATORY_ONLY"
                or row.get("manifest_sha256") != expected or not manifest_copy.is_file()
                or sha256(manifest_copy) != expected):
            raise RFQStageError(f"selected manifest SHA mismatch: {release_id}")
    return manifest


def validate_input_bindings(manifest: dict, inputs: dict) -> None:
    selected = {row["release_id"]: row for row in manifest["selected_releases"]}
    discovered = {row["release_id"]: row for row in inputs["releases"]}
    if set(selected) != set(discovered) or set(selected) != set(RELEASE_IDS):
        raise RFQStageError("selected/cache release identity mismatch")
    for release_id in RELEASE_IDS:
        expected = EXPECTED_MANIFEST_SHA256[release_id]
        if selected[release_id].get("manifest_sha256") != expected:
            raise RFQStageError(f"run manifest binding mismatch: {release_id}")
        if discovered[release_id].get("manifest_sha256") != expected:
            raise RFQStageError(f"cache manifest binding mismatch: {release_id}")


def configure(
    connection, run_dir: Path, memory_limit: str, max_temp_size: str, threads: int
) -> None:
    temp_dir = run_dir / "tmp/rfq_full_spill"
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection.execute("SET TimeZone='UTC'")
    connection.execute(f"SET memory_limit={quote(memory_limit)}")
    connection.execute(f"SET max_temp_directory_size={quote(max_temp_size)}")
    connection.execute(f"SET threads={int(threads)}")
    connection.execute(f"SET temp_directory={quote(temp_dir)}")
    connection.execute("SET preserve_insertion_order=false")


def scan_sql(paths: Sequence[Path], run_id: str) -> str:
    raw = path_list(paths)
    contracts_text = "json_extract_string(inner_json,'$.msg.contracts_fp')"
    target_text = "json_extract_string(inner_json,'$.msg.target_cost_dollars')"
    return f"""
      CREATE TABLE rfq_scan_rows AS
      WITH outer_rows AS (
        SELECT filename,try_cast(recv_wall_ns AS BIGINT) AS recv_wall_ns,
               try_cast(recv_mono_ns AS BIGINT) AS recv_mono_ns,
               stream_epoch,marker,raw,try_cast(raw AS JSON) AS inner_json
        FROM read_json({raw},format='newline_delimited',
          columns={{'recv_wall_ns':'UBIGINT','recv_mono_ns':'UBIGINT',
                   'stream_epoch':'BIGINT','marker':'VARCHAR','raw':'VARCHAR'}},
          ignore_errors=false,filename=true)
      ), decoded AS (
        SELECT *,json_extract_string(inner_json,'$.type') AS event_type,
          json_extract(inner_json,'$.msg') AS msg_json
        FROM outer_rows
      )
      SELECT
        filename,
        CASE
          WHEN contains(filename,{quote(RELEASE_IDS[0])}) THEN {quote(RELEASE_IDS[0])}
          WHEN contains(filename,{quote(RELEASE_IDS[1])}) THEN {quote(RELEASE_IDS[1])}
        END AS release_id,
        try_cast(regexp_extract(filename,'date=([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})',1) AS DATE)
          AS path_date,
        try_cast(regexp_extract(filename,'rfq_([0-9]{{2}})',1) AS INTEGER) AS path_hour,
        recv_wall_ns,recv_mono_ns,stream_epoch,marker,event_type,
        recv_wall_ns//1000 AS receive_us,
        cast(to_timestamp(recv_wall_ns/1000000000.0) AS DATE) AS receive_date,
        extract('hour' FROM to_timestamp(recv_wall_ns/1000000000.0))::INTEGER
          AS receive_hour,
        json_valid(raw) AS inner_json_valid,
        json_extract_string(inner_json,'$.msg.id') AS rfq_id,
        CASE WHEN length(json_extract_string(inner_json,'$.msg.creator_id'))>0
          THEN sha256({quote(f'SPORTS-AUTORESEARCH-01|{run_id}|')} ||
            json_extract_string(inner_json,'$.msg.creator_id')) END AS creator_hash,
        json_extract_string(inner_json,'$.msg.market_ticker') AS market_ticker,
        json_extract_string(inner_json,'$.msg.event_ticker') AS event_ticker,
        json_extract_string(inner_json,'$.msg.created_ts') AS created_ts_text,
        json_extract_string(inner_json,'$.msg.deleted_ts') AS deleted_ts_text,
        try_cast(coalesce(json_extract_string(inner_json,'$.msg.created_ts'),
                          json_extract_string(inner_json,'$.msg.deleted_ts'))
                 AS TIMESTAMPTZ) AS exchange_ts,
        {fixed_sql(contracts_text, 2)} AS contracts_e2,
        {fixed_sql(target_text, 6)} AS target_cost_e6,
        json_extract_string(inner_json,'$.msg.mve_collection_ticker')
          AS mve_collection_ticker,
        cast(json_extract(inner_json,'$.msg.mve_selected_legs') AS VARCHAR)
          AS mve_legs_json,
        json_type(inner_json,'$.msg.mve_selected_legs') AS mve_legs_type,
        coalesce(json_array_length(inner_json,'$.msg.mve_selected_legs'),0)::BIGINT
          AS leg_count_raw,
        try_cast(json_extract(inner_json,'$.sid') AS BIGINT) AS sid,
        try_cast(json_extract(inner_json,'$.seq') AS BIGINT) AS seq,
        cast(json_structure(msg_json) AS VARCHAR) AS msg_structure,
        json_extract_string(inner_json,'$.status') AS receipt_status,
        try_cast(json_extract(inner_json,'$.subscription_proven') AS BOOLEAN)
          AS receipt_subscription_proven,
        try_cast(json_extract(inner_json,'$.boundary_closed') AS BOOLEAN)
          AS receipt_boundary_closed,
        json_extract_string(inner_json,'$.end_reason') AS receipt_end_reason,
        cast(json_extract(inner_json,'$.findings') AS VARCHAR) AS receipt_findings,
        CASE WHEN recv_wall_ns>0 AND event_type IN ('rfq_created','rfq_deleted')
          AND try_cast(json_extract(inner_json,'$.sid') AS BIGINT)>0
          AND json_type(inner_json,'$.msg.id')='VARCHAR'
          AND length(json_extract_string(inner_json,'$.msg.id'))>0
          AND (json_type(inner_json,'$.msg.creator_id') IS NULL
               OR json_type(inner_json,'$.msg.creator_id') IN ('NULL','VARCHAR'))
          AND json_type(inner_json,'$.msg.market_ticker')='VARCHAR'
          AND length(json_extract_string(inner_json,'$.msg.market_ticker'))>0
          AND ((event_type='rfq_created'
                AND json_type(inner_json,'$.msg.created_ts')='VARCHAR'
                AND try_cast(json_extract_string(inner_json,'$.msg.created_ts')
                             AS TIMESTAMPTZ) IS NOT NULL)
            OR (event_type='rfq_deleted'
                AND json_type(inner_json,'$.msg.deleted_ts')='VARCHAR'
                AND try_cast(json_extract_string(inner_json,'$.msg.deleted_ts')
                             AS TIMESTAMPTZ) IS NOT NULL))
          AND (json_type(inner_json,'$.msg.event_ticker') IS NULL
               OR json_type(inner_json,'$.msg.event_ticker')='VARCHAR')
          AND (json_type(inner_json,'$.msg.mve_collection_ticker') IS NULL
               OR json_type(inner_json,'$.msg.mve_collection_ticker')='VARCHAR')
          AND (NOT ({contracts_text} IS NOT NULL) OR {fixed_sql(contracts_text, 2)} IS NOT NULL)
          AND (NOT ({target_text} IS NOT NULL) OR {fixed_sql(target_text, 6)} IS NOT NULL)
          AND (json_type(inner_json,'$.msg.mve_selected_legs') IS NULL
               OR json_type(inner_json,'$.msg.mve_selected_legs')='ARRAY')
          THEN true ELSE false END AS valid_contract
      FROM decoded
    """


def build_scan_tables(connection, paths: Sequence[Path], run_id: str = "_TEST") -> None:
    # A completed normalization phase intentionally drops its two largest
    # intermediates so their pages can be reused by lifecycle construction.
    if (table_exists(connection, "rfq_events_ordered")
            and table_exists(connection, "rfq_scan_counts")
            and table_exists(connection, "rfq_observation_boundaries")
            and not table_exists(connection, "rfq_scan_rows")):
        return
    if (not table_exists(connection, "rfq_scan_rows")
            and not table_exists(connection, "rfq_events_valid")):
        connection.execute(scan_sql(paths, run_id))
    if not table_exists(connection, "rfq_schema_signatures"):
        connection.execute("""
          CREATE TABLE rfq_schema_signatures AS
          SELECT event_type,msg_structure,count(*) AS rows
          FROM rfq_scan_rows
          WHERE event_type IN ('rfq_created','rfq_deleted')
          GROUP BY event_type,msg_structure
          ORDER BY event_type,rows DESC,msg_structure
        """)
    if not table_exists(connection, "rfq_schema_field_audit"):
        connection.execute("""
          CREATE TABLE rfq_schema_field_audit AS
          SELECT r.event_type,j.key AS field_name,count(*) AS present_rows,
                 count(*) FILTER (WHERE cast(j.value AS VARCHAR)<>'\"NULL\"')
                   AS nonnull_rows,
                 count(DISTINCT cast(j.value AS VARCHAR)) AS observed_type_structures,
                 string_agg(DISTINCT cast(j.value AS VARCHAR),';' ORDER BY cast(j.value AS VARCHAR))
                   AS type_structures
          FROM rfq_scan_rows r,json_each(try_cast(r.msg_structure AS JSON)) j
          WHERE r.event_type IN ('rfq_created','rfq_deleted')
          GROUP BY r.event_type,j.key
          ORDER BY r.event_type,j.key
        """)
    if not table_exists(connection, "rfq_channel_qc"):
        connection.execute("""
          CREATE TABLE rfq_channel_qc AS
          SELECT release_id,coalesce(receive_date,path_date) AS date,
            count(*) AS recorder_rows,
            min(recv_wall_ns) FILTER (WHERE recv_wall_ns>0) AS first_recv_wall_ns,
            max(recv_wall_ns) FILTER (WHERE recv_wall_ns>0) AS last_recv_wall_ns,
            count(*) FILTER (WHERE recv_wall_ns IS NULL OR recv_wall_ns<=0)
              AS invalid_outer_receive_rows,
            count(*) FILTER (WHERE inner_json_valid IS FALSE) AS invalid_inner_json_rows,
            count(*) FILTER (WHERE path_date IS NOT NULL AND receive_date<>path_date)
              AS partition_date_mismatches,
            count(*) FILTER (WHERE path_hour IS NOT NULL AND receive_hour<>path_hour)
              AS partition_hour_mismatches,
            count(*) FILTER (WHERE marker IS NOT NULL) AS marker_rows,
            count(*) FILTER (WHERE lower(coalesce(marker,'')) IN
              ('loss','gap','transport_close','transport_error')) AS loss_gap_markers,
            count(*) FILTER (WHERE event_type='subscribed') AS subscribed_frames,
            count(*) FILTER (WHERE event_type IN ('error','unsubscribed'))
              AS error_or_unsubscribed_frames,
            count(*) FILTER (WHERE event_type IN ('rfq_created','rfq_deleted'))
              AS rfq_frames,
            count(*) FILTER (WHERE event_type IN ('rfq_created','rfq_deleted') AND sid IS NOT NULL)
              AS rfq_sid_rows,
            count(*) FILTER (WHERE event_type IN ('rfq_created','rfq_deleted') AND seq IS NOT NULL)
              AS rfq_seq_rows,
            count(DISTINCT stream_epoch) FILTER (WHERE stream_epoch IS NOT NULL)
              AS stream_epochs,
            count(*) FILTER (WHERE marker='segment_receipt') AS receipt_rows,
            count(*) FILTER (WHERE marker='segment_receipt' AND receipt_status='PASS'
              AND receipt_subscription_proven AND receipt_boundary_closed
              AND receipt_end_reason='boundary'
              AND coalesce(receipt_findings,'[]') IN ('[]','null')) AS healthy_receipts
          FROM rfq_scan_rows
          GROUP BY release_id,coalesce(receive_date,path_date)
          ORDER BY date,release_id
        """)
    if not table_exists(connection, "rfq_observation_boundaries"):
        connection.execute("""
          CREATE TABLE rfq_observation_boundaries AS
          WITH ordered AS (
            SELECT recv_wall_ns,recv_mono_ns,stream_epoch,marker,event_type,
              lag(stream_epoch) OVER (ORDER BY recv_wall_ns,recv_mono_ns NULLS LAST,filename)
                AS prior_stream_epoch
            FROM rfq_scan_rows WHERE recv_wall_ns>0
          ), evidence AS (
            SELECT recv_wall_ns AS boundary_ns,
              'MARKER_'||upper(marker) AS reason
            FROM ordered WHERE lower(coalesce(marker,'')) IN
              ('loss','gap','transport_close','transport_error')
            UNION ALL
            SELECT recv_wall_ns,'FRAME_'||upper(event_type)
            FROM ordered WHERE event_type IN ('error','unsubscribed')
            UNION ALL
            SELECT recv_wall_ns,'STREAM_EPOCH_CHANGE'
            FROM ordered WHERE stream_epoch IS NOT NULL AND prior_stream_epoch IS NOT NULL
              AND stream_epoch<>prior_stream_epoch
          ) SELECT boundary_ns,string_agg(DISTINCT reason,';' ORDER BY reason) AS reason,
              count(*) AS evidence_rows
            FROM evidence GROUP BY boundary_ns ORDER BY boundary_ns
        """)
    if not table_exists(connection, "rfq_events_valid"):
        connection.execute("""
          CREATE TABLE rfq_events_valid AS
          WITH ranked AS (
            SELECT *,coalesce(created_ts_text,deleted_ts_text) AS exchange_ts_text,
              row_number() OVER (
                PARTITION BY event_type,rfq_id,coalesce(created_ts_text,deleted_ts_text),
                             market_ticker
                ORDER BY recv_wall_ns,recv_mono_ns NULLS LAST,filename
              ) AS duplicate_rank
            FROM rfq_scan_rows WHERE valid_contract
          )
          SELECT release_id,receive_date,receive_us,recv_wall_ns,recv_mono_ns,
            stream_epoch,event_type,rfq_id,creator_hash,market_ticker,event_ticker,
            exchange_ts,contracts_e2,target_cost_e6,mve_collection_ticker,
            mve_legs_json,mve_legs_type,leg_count_raw,
            sha256(concat_ws('|',event_type,rfq_id,exchange_ts_text,market_ticker))
              AS event_key
          FROM ranked WHERE duplicate_rank=1
        """)
    if not table_exists(connection, "rfq_scan_counts"):
        connection.execute("""
          CREATE TABLE rfq_scan_counts AS
          SELECT count(*) AS recorder_rows,
            count(*) FILTER (WHERE event_type IN ('rfq_created','rfq_deleted'))
              AS raw_rfq_frames,
            count(*) FILTER (WHERE valid_contract) AS valid_contract_frames,
            count(*) FILTER (WHERE event_type IN ('rfq_created','rfq_deleted')
                              AND NOT valid_contract) AS invalid_contract_frames,
            (SELECT count(*) FROM rfq_events_valid) AS deduplicated_valid_frames,
            min(recv_wall_ns) FILTER (WHERE recv_wall_ns>0) AS scan_start_ns,
            max(recv_wall_ns) FILTER (WHERE recv_wall_ns>0) AS scan_end_ns
          FROM rfq_scan_rows
        """)
    # Release the widest materialization before the global lifecycle sort.
    # DuckDB can reuse these pages for the narrow ordered event table.
    if table_exists(connection, "rfq_scan_rows"):
        connection.execute("DROP TABLE rfq_scan_rows")
    if not table_exists(connection, "rfq_events_ordered"):
        if not table_exists(connection, "rfq_events_valid"):
            raise RFQStageError("deduplicated RFQ events missing before ordering")
        connection.execute("""
          CREATE TABLE rfq_events_ordered AS
          SELECT *,sum(CASE WHEN event_type='rfq_created' THEN 1 ELSE 0 END) OVER (
              PARTITION BY rfq_id ORDER BY recv_wall_ns,recv_mono_ns NULLS LAST,
                CASE WHEN event_type='rfq_created' THEN 0 ELSE 1 END,event_key
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )::BIGINT AS cycle_no
          FROM rfq_events_valid
        """)
    if table_exists(connection, "rfq_events_valid"):
        connection.execute("DROP TABLE rfq_events_valid")


def build_request_tables(connection, run_id: str) -> None:
    outputs = (
        "rfq_requests_base", "rfq_lifecycle_base", "rfq_legs_base",
        "rfq_unmatched_deletes",
    )
    if all(table_exists(connection, name) for name in outputs):
        return
    if not table_exists(connection, "rfq_events_ordered"):
        raise RFQStageError("normalized RFQ events missing for lifecycle phase")
    if not table_exists(connection, "rfq_creates"):
        connection.execute("""
          CREATE TABLE rfq_creates AS
          SELECT *,sha256(concat_ws('|',rfq_id,cycle_no,event_key)) AS request_key,
            lead(recv_wall_ns) OVER (
              PARTITION BY rfq_id ORDER BY cycle_no,recv_wall_ns,
                recv_mono_ns NULLS LAST,event_key
            ) AS next_create_recv_ns
          FROM rfq_events_ordered WHERE event_type='rfq_created'
        """)
    if not table_exists(connection, "rfq_create_boundaries"):
        connection.execute("""
          CREATE TABLE rfq_create_boundaries AS
          WITH candidates AS (
            SELECT c.request_key,c.next_create_recv_ns,
              b.boundary_ns AS observation_boundary_ns,
              b.reason AS observation_boundary_reason
            FROM (SELECT request_key,recv_wall_ns,next_create_recv_ns FROM rfq_creates
                ORDER BY recv_wall_ns,request_key) c
            ASOF LEFT JOIN (SELECT * FROM rfq_observation_boundaries ORDER BY boundary_ns) b
              ON c.recv_wall_ns<b.boundary_ns
          ) SELECT *,CASE
              WHEN observation_boundary_ns IS NULL THEN next_create_recv_ns
              WHEN next_create_recv_ns IS NULL THEN observation_boundary_ns
              ELSE least(observation_boundary_ns,next_create_recv_ns) END AS boundary_ns,
            CASE WHEN next_create_recv_ns IS NOT NULL
                       AND (observation_boundary_ns IS NULL
                            OR next_create_recv_ns<=observation_boundary_ns)
                 THEN 'NEXT_CREATE_REPLACEMENT'
                 WHEN observation_boundary_ns IS NOT NULL THEN 'OBSERVATION_BOUNDARY'
                 END AS censor_boundary_type
          FROM candidates
        """)
    if not table_exists(connection, "rfq_delete_candidates"):
        connection.execute("""
          CREATE TABLE rfq_delete_candidates AS
          SELECT c.request_key,c.rfq_id,c.cycle_no,c.recv_wall_ns AS create_recv_ns,
                 d.event_key AS delete_event_key,d.recv_wall_ns AS delete_recv_ns,
                 d.recv_mono_ns AS delete_recv_mono_ns,d.exchange_ts AS delete_exchange_ts,
                 d.creator_hash AS delete_creator_hash,d.market_ticker AS delete_market_ticker,
                 d.event_ticker AS delete_event_ticker,d.contracts_e2 AS delete_contracts_e2,
                 d.target_cost_e6 AS delete_target_cost_e6,
                 (d.market_ticker=c.market_ticker
                  AND (d.contracts_e2 IS NULL OR c.contracts_e2 IS NULL
                       OR d.contracts_e2=c.contracts_e2)
                  AND (d.target_cost_e6 IS NULL OR c.target_cost_e6 IS NULL
                       OR d.target_cost_e6=c.target_cost_e6)) AS join_consistent,
                 (d.exchange_ts<c.exchange_ts) AS exchange_order_anomaly,
                 row_number() OVER (
                   PARTITION BY c.request_key,
                     (d.market_ticker=c.market_ticker
                      AND (d.contracts_e2 IS NULL OR c.contracts_e2 IS NULL
                           OR d.contracts_e2=c.contracts_e2)
                      AND (d.target_cost_e6 IS NULL OR c.target_cost_e6 IS NULL
                           OR d.target_cost_e6=c.target_cost_e6))
                   ORDER BY d.recv_wall_ns,d.recv_mono_ns NULLS LAST,d.event_key
                 ) AS consistency_rank
          FROM rfq_creates c JOIN rfq_events_ordered d
            ON d.rfq_id=c.rfq_id AND d.cycle_no=c.cycle_no
               AND d.event_type='rfq_deleted' AND d.recv_wall_ns>=c.recv_wall_ns
        """)
    if not table_exists(connection, "rfq_matched_deletes"):
        connection.execute("""
          CREATE TABLE rfq_matched_deletes AS
          SELECT * FROM rfq_delete_candidates
          WHERE join_consistent AND consistency_rank=1
        """)
    if not table_exists(connection, "rfq_delete_cycle_stats"):
        connection.execute("""
          CREATE TABLE rfq_delete_cycle_stats AS
          SELECT c.request_key,
            count(d.event_key) AS delete_rows_in_cycle,
            count(d.event_key) FILTER (WHERE NOT coalesce(x.join_consistent,false))
              AS inconsistent_delete_rows
          FROM rfq_creates c
          LEFT JOIN rfq_events_ordered d
            ON d.rfq_id=c.rfq_id AND d.cycle_no=c.cycle_no
              AND d.event_type='rfq_deleted'
          LEFT JOIN rfq_delete_candidates x
            ON x.request_key=c.request_key AND x.delete_event_key=d.event_key
          GROUP BY c.request_key
        """)
    if not table_exists(connection, "rfq_requests_base"):
        connection.execute(f"""
          CREATE TABLE rfq_requests_base AS
          SELECT c.request_key,c.release_id,c.receive_date AS create_date,
            c.receive_us AS create_receive_us,c.recv_wall_ns AS create_recv_wall_ns,
            c.recv_mono_ns AS create_recv_mono_ns,c.stream_epoch AS create_stream_epoch,
            sha256(c.rfq_id) AS rfq_id_hash,c.cycle_no,
            c.market_ticker,c.event_ticker,
            split_part(c.market_ticker,'-',1) AS market_family,
            c.contracts_e2,c.target_cost_e6,
            CASE WHEN c.contracts_e2 IS NULL AND c.target_cost_e6 IS NULL THEN 'missing'
                 WHEN c.contracts_e2 IS NOT NULL AND c.target_cost_e6 IS NOT NULL THEN 'both_reported'
                 WHEN c.contracts_e2 IS NOT NULL THEN 'contracts'
                 ELSE 'target_cost' END AS size_mode,
            c.mve_collection_ticker,c.leg_count_raw,
            (c.mve_collection_ticker IS NOT NULL OR c.leg_count_raw>0) AS known_combo,
            coalesce(CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                          THEN m.delete_creator_hash END,c.creator_hash) AS requester_hash,
            coalesce(CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                          THEN m.delete_creator_hash END,c.creator_hash) IS NOT NULL
              AS requester_known,
            m.delete_event_key,m.delete_recv_ns AS matched_delete_recv_ns,
            CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                 THEN m.delete_recv_ns END AS delete_recv_ns,
            CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                 THEN m.delete_recv_mono_ns END AS delete_recv_mono_ns,
            CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                 THEN m.delete_exchange_ts END AS delete_exchange_ts,
            b.boundary_ns,b.censor_boundary_type,b.next_create_recv_ns,
            b.observation_boundary_ns,b.observation_boundary_reason,
            (m.delete_recv_ns IS NOT NULL AND b.observation_boundary_ns IS NOT NULL
             AND m.delete_recv_ns>=b.observation_boundary_ns)
              AS delete_crosses_observation_boundary,
            (m.delete_recv_ns IS NOT NULL AND b.boundary_ns IS NOT NULL
             AND m.delete_recv_ns>=b.boundary_ns) AS delete_crosses_censor_boundary,
            coalesce(CASE WHEN b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns
                          THEN m.exchange_order_anomaly END,false) AS exchange_order_anomaly,
            (c.creator_hash IS NOT NULL AND m.delete_creator_hash IS NOT NULL
             AND (b.boundary_ns IS NULL OR m.delete_recv_ns<b.boundary_ns)
             AND c.creator_hash<>m.delete_creator_hash) AS requester_conflict,
            coalesce(s.inconsistent_delete_rows,0) AS inconsistent_delete_rows,
            coalesce(s.delete_rows_in_cycle,0) AS delete_rows_in_cycle
          FROM rfq_creates c LEFT JOIN rfq_matched_deletes m USING(request_key)
          LEFT JOIN rfq_delete_cycle_stats s USING(request_key)
          LEFT JOIN rfq_create_boundaries b USING(request_key)
        """)
    if not table_exists(connection, "rfq_lifecycle_base"):
        connection.execute("""
          CREATE TABLE rfq_lifecycle_base AS
          WITH boundary AS (SELECT scan_end_ns FROM rfq_scan_counts)
          SELECT r.request_key,r.rfq_id_hash,r.create_date,r.create_receive_us,
            r.delete_recv_ns//1000 AS delete_receive_us,
            coalesce(r.delete_recv_ns,r.boundary_ns,b.scan_end_ns)//1000 AS endpoint_receive_us,
            greatest(0,coalesce(r.delete_recv_ns,r.boundary_ns,b.scan_end_ns)-r.create_recv_wall_ns)
              //1000 AS duration_us,
            r.delete_recv_ns IS NOT NULL AS delete_observed,
            CASE WHEN r.delete_recv_ns IS NOT NULL THEN 'FIRST_VALID_DELETE'
                 WHEN r.censor_boundary_type='NEXT_CREATE_REPLACEMENT'
                   THEN 'RIGHT_CENSORED_AT_NEXT_CREATE'
                 WHEN r.censor_boundary_type='OBSERVATION_BOUNDARY'
                   THEN 'RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY'
                 ELSE 'RIGHT_CENSORED_AT_SCAN_END' END AS endpoint_type,
            r.cycle_no>1 AS repeated_rfq_id,r.cycle_no,r.delete_rows_in_cycle,
            r.inconsistent_delete_rows,r.exchange_order_anomaly,r.requester_conflict,
            r.requester_known,r.requester_hash,
            r.boundary_ns//1000 AS censor_boundary_receive_us,r.censor_boundary_type,
            r.next_create_recv_ns//1000 AS next_create_receive_us,
            r.observation_boundary_ns//1000 AS observation_boundary_receive_us,
            r.observation_boundary_reason,r.delete_crosses_observation_boundary,
            r.delete_crosses_censor_boundary,
            b.scan_end_ns//1000 AS scan_end_receive_us
          FROM rfq_requests_base r CROSS JOIN boundary b
        """)
    if not table_exists(connection, "rfq_legs_base"):
        leg_value = "try_cast(j.value AS JSON)"
        settlement = f"json_extract_string({leg_value},'$.yes_settlement_value_dollars')"
        connection.execute(f"""
          CREATE TABLE rfq_legs_base AS
          SELECT c.request_key,c.receive_date AS create_date,c.receive_us AS create_receive_us,
            try_cast(j.key AS INTEGER) AS leg_index,
            json_extract_string({leg_value},'$.event_ticker') AS event_ticker,
            json_extract_string({leg_value},'$.market_ticker') AS market_ticker,
            lower(json_extract_string({leg_value},'$.side')) AS side,
            {fixed_sql(settlement, 6)} AS yes_settlement_value_e6,
            CASE WHEN json_type({leg_value})='OBJECT'
              AND (json_type({leg_value},'$.event_ticker') IS NULL
                   OR json_type({leg_value},'$.event_ticker')='VARCHAR')
              AND (json_type({leg_value},'$.market_ticker') IS NULL
                   OR json_type({leg_value},'$.market_ticker')='VARCHAR')
              AND (json_type({leg_value},'$.side') IS NULL
                   OR json_type({leg_value},'$.side')='VARCHAR')
              AND ({settlement} IS NULL OR {fixed_sql(settlement, 6)} IS NOT NULL)
              THEN true ELSE false END AS leg_schema_valid
          FROM rfq_creates c,json_each(try_cast(c.mve_legs_json AS JSON)) j
          WHERE c.mve_legs_type='ARRAY'
        """)
    if not table_exists(connection, "rfq_unmatched_deletes"):
        connection.execute("""
          CREATE TABLE rfq_unmatched_deletes AS
          SELECT sha256(d.rfq_id) AS rfq_id_hash,d.receive_date,d.receive_us,
                 d.market_ticker,d.event_ticker,d.cycle_no,
                 CASE WHEN d.cycle_no=0 THEN 'DELETE_BEFORE_ANY_CREATE'
                      WHEN r.delete_event_key IS NOT NULL AND r.delete_recv_ns IS NOT NULL
                        THEN 'MATCHED_ENDPOINT'
                      WHEN r.delete_event_key IS NOT NULL
                           AND r.delete_crosses_observation_boundary
                        THEN 'CROSS_OBSERVATION_BOUNDARY_DELETE'
                      WHEN EXISTS (SELECT 1 FROM rfq_delete_candidates c
                                   WHERE c.delete_event_key=d.event_key AND NOT c.join_consistent)
                        THEN 'JOIN_INCONSISTENT'
                      ELSE 'EXTRA_OR_UNMATCHED_DELETE' END AS disposition
          FROM rfq_events_ordered d
          LEFT JOIN rfq_requests_base r ON r.delete_event_key=d.event_key
          WHERE d.event_type='rfq_deleted'
        """)
    for name in (
        "rfq_delete_cycle_stats", "rfq_matched_deletes", "rfq_delete_candidates",
        "rfq_create_boundaries", "rfq_creates", "rfq_events_ordered",
    ):
        connection.execute(f"DROP TABLE {name}")


def attach_core_and_enrich(connection, core_db: Path) -> None:
    if not core_db.is_file():
        raise RFQStageError(f"Cycle-1 DuckDB missing: {core_db}")
    attached = {row[1] for row in connection.execute("PRAGMA database_list").fetchall()}
    if "core" not in attached:
        connection.execute(f"ATTACH {quote(core_db)} AS core (READ_ONLY)")
    required = ("universe", "l1_real", "trades_safe", "l2_all", "capture_gaps")
    for table in required:
        if not scalar(
            connection,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog='core' AND table_name=?",
            [table],
        ):
            raise RFQStageError(f"Cycle-1 core table missing: {table}")
    if not table_exists(connection, "rfq_requests"):
        connection.execute("""
          CREATE TABLE rfq_requests AS
          SELECT r.*,
            CASE WHEN u.market_ticker IS NOT NULL THEN 'Sports'
                 ELSE '_NOT_SPORTS_OR_UNMAPPED' END AS category,
            u.sport,u.league,u.root_event_id,u.root_map_status,u.occurrence_datetime,
            u.dim_effective_us,
            CASE WHEN u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
                       AND u.dim_effective_us IS NOT NULL
                       AND r.create_receive_us>=u.dim_effective_us
                 THEN 'CAUSAL_AS_OF_CREATE'
                 WHEN u.market_ticker IS NOT NULL THEN 'POSTHOC_DIM_NOT_CAUSAL_AT_CREATE'
                 ELSE 'UNMAPPED' END AS dimension_causality,
            CASE WHEN u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
                       AND u.dim_effective_us IS NOT NULL
                       AND r.create_receive_us>=u.dim_effective_us THEN
              (epoch_us(u.occurrence_datetime)-r.create_receive_us)/1000000.0 END
              AS time_to_start_s,
            CASE WHEN u.occurrence_datetime IS NOT NULL THEN
              (epoch_us(u.occurrence_datetime)-r.create_receive_us)/1000000.0 END
              AS posthoc_time_to_start_s,
            CASE WHEN u.root_map_status<>'PROVISIONAL_HEURISTIC_MATCHUP_TIME'
                       OR u.dim_effective_us IS NULL
                       OR r.create_receive_us<u.dim_effective_us THEN 'UNKNOWN_POSTHOC_DIM'
                 WHEN epoch_us(u.occurrence_datetime)>r.create_receive_us THEN 'PRE_MATCH'
                 ELSE 'IN_PLAY_OR_POST_START' END AS match_phase
          FROM rfq_requests_base r LEFT JOIN core.universe u
            ON u.date=r.create_date AND u.market_ticker=r.market_ticker
        """)
    if not table_exists(connection, "rfq_lifecycle"):
        connection.execute("""
          CREATE TABLE rfq_lifecycle AS
          WITH joined AS (
            SELECT l.*,r.market_ticker,r.event_ticker,r.market_family,r.category,
                   r.sport,r.league,r.root_event_id,r.root_map_status,
                   r.known_combo,r.leg_count_raw,r.size_mode,r.contracts_e2,
                   r.target_cost_e6,r.time_to_start_s,r.posthoc_time_to_start_s,
                   r.match_phase,r.dim_effective_us,r.dimension_causality
            FROM rfq_lifecycle_base l JOIN rfq_requests r USING(request_key)
          ) SELECT *,create_receive_us-lag(delete_receive_us) OVER (
              PARTITION BY rfq_id_hash ORDER BY create_receive_us,cycle_no,request_key
            ) AS replacement_gap_us
            FROM joined
        """)
    if not table_exists(connection, "rfq_legs"):
        connection.execute("""
          CREATE TABLE rfq_legs AS
          SELECT l.*,
            u.sport,u.league,u.root_event_id,u.root_map_status,u.dim_effective_us,
            CASE WHEN u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
                       AND u.dim_effective_us IS NOT NULL
                       AND l.create_receive_us>=u.dim_effective_us
                 THEN 'CAUSAL_AS_OF_CREATE'
                 WHEN u.market_ticker IS NOT NULL THEN 'POSTHOC_DIM_ONLY'
                 ELSE 'UNMAPPED' END AS dimension_status,
            CASE WHEN u.market_ticker IS NOT NULL THEN 'Sports'
                 ELSE '_NOT_SPORTS_OR_UNMAPPED' END AS category
          FROM rfq_legs_base l LEFT JOIN core.universe u
            ON u.date=l.create_date AND u.market_ticker=l.market_ticker
        """)


def build_descriptive_tables(connection) -> None:
    needs_interarrival = not (
        table_exists(connection, "rfq_interarrival")
        and table_exists(connection, "rfq_interarrival_histogram")
    )
    if needs_interarrival and not table_exists(connection, "rfq_interarrival_events"):
        connection.execute("""
          CREATE TABLE rfq_interarrival_events AS
          SELECT create_date,category,sport,
            create_receive_us-lag(create_receive_us) OVER (
              PARTITION BY create_date,category,sport ORDER BY create_receive_us,request_key)
              AS interarrival_us
          FROM rfq_requests
        """)
    statements = {
        "rfq_flow_daily": """
          SELECT create_date,dayname(create_date) AS day_of_week,category,
            coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            count(*) FILTER (WHERE known_combo) AS known_combo_requests,
            count(*) FILTER (WHERE requester_known) AS known_requester_requests
          FROM rfq_requests
          GROUP BY create_date,day_of_week,category,coalesce(sport,'_UNMAPPED')
          ORDER BY create_date,category,sport
        """,
        "rfq_event_flow_hourly": """
          WITH events AS (
            SELECT create_date AS date,create_receive_us AS receive_us,'rfq_created' AS event_type
            FROM rfq_requests
            UNION ALL
            SELECT receive_date AS date,receive_us,'rfq_deleted' AS event_type
            FROM rfq_unmatched_deletes
          ) SELECT date,extract('hour' FROM to_timestamp(receive_us/1000000.0))::INTEGER
              AS utc_hour,event_type,count(*) AS events
            FROM events GROUP BY date,utc_hour,event_type ORDER BY date,utc_hour,event_type
        """,
        "rfq_flow_hourly": """
          SELECT create_date,extract('hour' FROM to_timestamp(create_receive_us/1000000.0))::INTEGER
                   AS utc_hour,
            category,coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            count(*) FILTER (WHERE known_combo) AS known_combo_requests,
            count(*) FILTER (WHERE requester_known) AS known_requester_requests,
            count(DISTINCT root_event_id) FILTER (WHERE root_event_id IS NOT NULL) AS root_events
          FROM rfq_requests GROUP BY create_date,utc_hour,category,coalesce(sport,'_UNMAPPED')
          ORDER BY create_date,utc_hour,category,sport
        """,
        "rfq_flow_minute": """
          SELECT create_date,(create_receive_us//60000000)*60000000 AS minute_receive_us,
            category,coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            min(create_receive_us) AS first_receive_us,max(create_receive_us) AS last_receive_us
          FROM rfq_requests GROUP BY create_date,minute_receive_us,category,coalesce(sport,'_UNMAPPED')
        """,
        "rfq_burst_summary": """
          WITH thresholds AS (
            SELECT create_date,category,sport,avg(requests) AS mean_requests_per_minute,
              stddev_pop(requests) AS sd_requests_per_minute,
              quantile_cont(requests,0.99) AS p99_requests_per_minute,
              max(requests) AS peak_requests_per_minute
            FROM rfq_flow_minute GROUP BY create_date,category,sport
          ) SELECT t.*,count(*) AS observed_active_minutes,
              count(*) FILTER (WHERE m.requests>=greatest(5,t.p99_requests_per_minute))
                AS p99_burst_minutes,
              sum(m.requests) FILTER (WHERE m.requests>=greatest(5,t.p99_requests_per_minute))
                AS requests_in_p99_burst_minutes
            FROM thresholds t JOIN rfq_flow_minute m
              USING(create_date,category,sport)
            GROUP BY ALL
        """,
        "rfq_interarrival": """
          SELECT create_date,category,coalesce(sport,'_UNMAPPED') AS sport,
            count(interarrival_us) AS n,
            quantile_cont(interarrival_us,0.50) AS p50_us,
            quantile_cont(interarrival_us,0.90) AS p90_us,
            quantile_cont(interarrival_us,0.99) AS p99_us,
            max(interarrival_us) AS max_us
          FROM rfq_interarrival_events
          GROUP BY create_date,category,coalesce(sport,'_UNMAPPED')
        """,
        "rfq_interarrival_histogram": """
          SELECT floor(ln(greatest(1,interarrival_us)::DOUBLE)/ln(10)*20)/20.0
              AS log10_interarrival_us_bin,
            count(*) AS observations
          FROM rfq_interarrival_events WHERE interarrival_us IS NOT NULL
          GROUP BY log10_interarrival_us_bin ORDER BY log10_interarrival_us_bin
        """,
        "rfq_size_histogram": """
          WITH values AS (
            SELECT 'contracts_e2' AS field,contracts_e2 AS value FROM rfq_requests
            WHERE contracts_e2>0
            UNION ALL
            SELECT 'target_cost_e6',target_cost_e6 FROM rfq_requests WHERE target_cost_e6>0
          ) SELECT field,floor(ln(value::DOUBLE)/ln(10)*20)/20.0 AS log10_value_bin,
            count(*) AS observations FROM values
          GROUP BY field,log10_value_bin ORDER BY field,log10_value_bin
        """,
        "rfq_size_summary": """
          SELECT create_date,category,coalesce(sport,'_UNMAPPED') AS sport,
            known_combo,size_mode,count(*) AS requests,
            count(contracts_e2) AS contracts_n,median(contracts_e2) AS contracts_p50_e2,
            quantile_cont(contracts_e2,0.99) AS contracts_p99_e2,max(contracts_e2) AS contracts_max_e2,
            count(target_cost_e6) AS target_n,median(target_cost_e6) AS target_p50_e6,
            quantile_cont(target_cost_e6,0.99) AS target_p99_e6,max(target_cost_e6) AS target_max_e6,
            avg(cast((contracts_e2%10000)=0 AS INTEGER)) FILTER (WHERE contracts_e2 IS NOT NULL)
              AS whole_100_contract_share,
            avg(cast((target_cost_e6%1000000)=0 AS INTEGER)) FILTER (WHERE target_cost_e6 IS NOT NULL)
              AS whole_dollar_share
          FROM rfq_requests
          GROUP BY create_date,category,coalesce(sport,'_UNMAPPED'),known_combo,size_mode
        """,
        "rfq_lifecycle_summary": """
          SELECT create_date,category,coalesce(sport,'_UNMAPPED') AS sport,
            known_combo,match_phase,delete_observed,count(*) AS requests,
            median(duration_us)/1000000.0 AS duration_p50_s,
            quantile_cont(duration_us,0.90)/1000000.0 AS duration_p90_s,
            quantile_cont(duration_us,0.99)/1000000.0 AS duration_p99_s,
            max(duration_us)/1000000.0 AS duration_max_s,
            count(*) FILTER (WHERE repeated_rfq_id) AS repeated_id_requests,
            count(*) FILTER (WHERE replacement_gap_us BETWEEN 0 AND 60000000)
              AS rapid_replacements_60s,
            count(*) FILTER (WHERE exchange_order_anomaly) AS exchange_order_anomalies,
            count(*) FILTER (WHERE inconsistent_delete_rows>0) AS join_mismatch_requests
          FROM rfq_lifecycle
          GROUP BY create_date,category,coalesce(sport,'_UNMAPPED'),known_combo,match_phase,delete_observed
        """,
        "rfq_lifecycle_strata": """
          SELECT create_date,category,coalesce(sport,'_UNMAPPED') AS sport,market_family,
            known_combo,
            CASE WHEN coalesce(contracts_e2,0)>0 THEN
              cast(floor(ln(contracts_e2::DOUBLE)/ln(10)) AS VARCHAR)
              WHEN coalesce(target_cost_e6,0)>0 THEN
              'target_'||cast(floor(ln(target_cost_e6::DOUBLE)/ln(10)) AS VARCHAR)
              ELSE '_MISSING' END AS size_log10_bucket,
            count(*) AS requests,avg(cast(delete_observed AS INTEGER)) AS delete_observed_share,
            median(duration_us)/1000000.0 AS duration_p50_s,
            quantile_cont(duration_us,0.90)/1000000.0 AS duration_p90_s
          FROM rfq_lifecycle
          GROUP BY create_date,category,coalesce(sport,'_UNMAPPED'),market_family,
                   known_combo,size_log10_bucket
        """,
        "rfq_population_mix": """
          SELECT create_date,category,coalesce(sport,'_UNMAPPED') AS sport,market_family,
            match_phase,count(*) AS requests,
            count(DISTINCT root_event_id) FILTER (WHERE root_event_id IS NOT NULL) AS root_events
          FROM rfq_requests
          GROUP BY create_date,category,coalesce(sport,'_UNMAPPED'),market_family,match_phase
        """,
        "rfq_tts_size_summary": """
          SELECT create_date,sport,
            CASE WHEN time_to_start_s<0 THEN 'IN_PLAY_OR_POST_START'
                 WHEN time_to_start_s<=900 THEN '00_0_15m'
                 WHEN time_to_start_s<=3600 THEN '01_15_60m'
                 WHEN time_to_start_s<=21600 THEN '02_1_6h'
                 WHEN time_to_start_s<=86400 THEN '03_6_24h'
                 WHEN time_to_start_s IS NOT NULL THEN '04_gt24h' ELSE 'UNKNOWN' END
              AS tts_bucket,
            count(*) AS requests,median(contracts_e2) AS contracts_p50_e2,
            median(target_cost_e6) AS target_cost_p50_e6
          FROM rfq_requests GROUP BY create_date,sport,tts_bucket
        """,
        "rfq_root_event_concentration": """
          SELECT create_date,sport,root_event_id,count(*) AS requests,
            count(DISTINCT market_ticker) AS markets
          FROM rfq_requests WHERE root_event_id IS NOT NULL
          GROUP BY create_date,sport,root_event_id ORDER BY requests DESC,root_event_id
        """,
        "rfq_combo_summary": """
          WITH bundle AS (
            SELECT r.request_key,r.create_date,r.mve_collection_ticker,r.known_combo,
              count(l.leg_index) AS legs,
              count(DISTINCT l.event_ticker) FILTER (WHERE l.event_ticker IS NOT NULL) AS leg_events,
              count(*) FILTER (WHERE l.leg_index IS NOT NULL
                AND NOT coalesce(l.leg_schema_valid,false)) AS invalid_legs,
              sha256(string_agg(concat_ws('|',coalesce(l.market_ticker,''),coalesce(l.side,''),
                coalesce(cast(l.yes_settlement_value_e6 AS VARCHAR),'')),';' ORDER BY l.leg_index))
                AS bundle_hash
            FROM rfq_requests r LEFT JOIN rfq_legs l USING(request_key)
            GROUP BY r.request_key,r.create_date,r.mve_collection_ticker,r.known_combo
          ) SELECT create_date,known_combo,legs,
            CASE WHEN leg_events<=1 THEN 'SAME_EVENT_OR_UNKNOWN' ELSE 'CROSS_EVENT' END
              AS event_structure,
            count(*) AS requests,count(DISTINCT bundle_hash) AS distinct_bundles,
            count(*) FILTER (WHERE invalid_legs>0) AS requests_with_invalid_legs,
            count(DISTINCT mve_collection_ticker) FILTER (WHERE mve_collection_ticker IS NOT NULL)
              AS collections
          FROM bundle GROUP BY create_date,known_combo,legs,event_structure
        """,
        "rfq_combo_side_summary": """
          SELECT create_date,coalesce(side,'_MISSING') AS selected_side,
            leg_schema_valid,count(*) AS legs,count(DISTINCT request_key) AS requests
          FROM rfq_legs GROUP BY create_date,coalesce(side,'_MISSING'),leg_schema_valid
          ORDER BY create_date,selected_side,leg_schema_valid
        """,
        "rfq_bundle_frequency": """
          WITH bundles AS (
            SELECT request_key,sha256(string_agg(concat_ws('|',coalesce(market_ticker,''),
              coalesce(side,''),coalesce(cast(yes_settlement_value_e6 AS VARCHAR),'')),
              ';' ORDER BY leg_index)) AS bundle_hash,count(*) AS legs
            FROM rfq_legs WHERE leg_schema_valid GROUP BY request_key
          ), frequency AS (
            SELECT bundle_hash,legs,count(*) AS requests FROM bundles
            GROUP BY bundle_hash,legs HAVING count(*)>=2
          ) SELECT * FROM frequency ORDER BY requests DESC,bundle_hash LIMIT 100000
        """,
        "rfq_collection_concentration": """
          SELECT mve_collection_ticker,count(*) AS requests,
            count(DISTINCT request_key) AS distinct_requests
          FROM rfq_requests WHERE mve_collection_ticker IS NOT NULL
          GROUP BY mve_collection_ticker ORDER BY requests DESC,mve_collection_ticker
        """,
        "rfq_requester_summary": """
          WITH x AS (
            SELECT requester_hash,request_key,create_date,create_receive_us,category,sport,
              known_combo,contracts_e2,target_cost_e6,
              create_receive_us-lag(create_receive_us) OVER (
                PARTITION BY requester_hash ORDER BY create_receive_us,request_key) AS repeat_interval_us
            FROM rfq_requests WHERE requester_hash IS NOT NULL
          ) SELECT requester_hash,count(*) AS requests,count(DISTINCT create_date) AS days,
            count(DISTINCT category) AS category_breadth,count(DISTINCT sport) AS sport_breadth,
            count(*) FILTER (WHERE known_combo) AS known_combo_requests,
            sum(contracts_e2) AS contracts_e2_sum,sum(target_cost_e6) AS target_cost_e6_sum,
            median(repeat_interval_us) AS repeat_interval_p50_us,
            quantile_cont(repeat_interval_us,0.90) AS repeat_interval_p90_us
          FROM x GROUP BY requester_hash ORDER BY requests DESC,requester_hash
        """,
        "rfq_requester_concentration": """
          WITH ranked AS (
            SELECT requests,row_number() OVER (ORDER BY requests DESC,requester_hash) AS rank,
              count(*) OVER () AS requester_count,sum(requests) OVER () AS known_requests
            FROM rfq_requester_summary
          ) SELECT max(requester_count) AS known_requesters,
            max(known_requests) AS known_requester_requests,
            sum((requests::DOUBLE/known_requests)*(requests::DOUBLE/known_requests)) AS hhi,
            sum(requests) FILTER (WHERE rank<=1)::DOUBLE/max(known_requests) AS top_1_share,
            sum(requests) FILTER (WHERE rank<=5)::DOUBLE/max(known_requests) AS top_5_share,
            sum(requests) FILTER (WHERE rank<=10)::DOUBLE/max(known_requests) AS top_10_share,
            sum(requests) FILTER (
              WHERE rank<=greatest(1,ceil(requester_count*0.01)))::DOUBLE/max(known_requests)
              AS top_1_percent_share
          FROM ranked
        """,
        "rfq_lifecycle_km_counts": """
          SELECT duration_us//1000 AS duration_ms,
            count(*) FILTER (WHERE delete_observed) AS deaths,
            count(*) FILTER (WHERE NOT delete_observed) AS censored
          FROM rfq_lifecycle GROUP BY duration_ms ORDER BY duration_ms
        """,
        "rfq_unmatched_delete_summary": """
          SELECT receive_date,disposition,count(*) AS delete_rows,
            count(DISTINCT rfq_id_hash) AS rfq_ids
          FROM rfq_unmatched_deletes GROUP BY receive_date,disposition
          ORDER BY receive_date,disposition
        """,
    }
    for name, query in statements.items():
        if not table_exists(connection, name):
            connection.execute(f"CREATE TABLE {name} AS {query}")
    if table_exists(connection, "rfq_interarrival_events"):
        connection.execute("DROP TABLE rfq_interarrival_events")
    if not table_exists(connection, "rfq_lifecycle_km_curve"):
        connection.execute("""
          CREATE TABLE rfq_lifecycle_km_curve AS
          WITH risk AS (
            SELECT duration_ms,deaths,censored,
              sum(deaths+censored) OVER ()
                -coalesce(sum(deaths+censored) OVER (
                  ORDER BY duration_ms ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ),0) AS at_risk
            FROM rfq_lifecycle_km_counts
          ), factors AS (
            SELECT *,CASE WHEN deaths>=at_risk AND deaths>0 THEN 1 ELSE 0 END AS zero_factor,
              CASE WHEN deaths=0 THEN 0.0
                   WHEN deaths<at_risk THEN ln(1.0-deaths::DOUBLE/at_risk)
                   ELSE 0.0 END AS log_factor
            FROM risk
          ), cumulative AS (
            SELECT *,sum(zero_factor) OVER (ORDER BY duration_ms) AS zero_factors_to_date,
              sum(log_factor) OVER (ORDER BY duration_ms) AS cumulative_log_survival
            FROM factors
          ) SELECT duration_ms,at_risk,deaths,censored,
              CASE WHEN zero_factors_to_date>0 THEN 0.0
                   ELSE exp(cumulative_log_survival) END AS survival
            FROM cumulative ORDER BY duration_ms
        """)


def _logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,{column}))"
        return f"ln(({clipped})/(10000.0-({clipped})))"
    return f"(({logit(bid)})+({logit(ask)}))/2.0"


def build_clob_context(connection, max_per_root: int) -> dict:
    """Build a deterministic root-balanced RFQ/CLOB event-study cohort.

    Full RFQ flow/lifecycle remains unsampled.  Only the nine-window CLOB
    expansion is bounded because expanding roughly two hundred million raw
    rows would otherwise create a multi-billion-row intermediate.
    """
    if max_per_root < 1:
        raise RFQStageError("clob-max-per-root must be positive")
    if not table_exists(connection, "rfq_anchor_markets_all"):
        connection.execute("""
          CREATE TABLE rfq_anchor_markets_all AS
          WITH request_market AS (
            SELECT r.request_key,r.create_date,r.create_receive_us,r.delete_recv_ns//1000
                     AS delete_receive_us,
              r.market_ticker,'REQUEST_MARKET' AS anchor_role,r.known_combo,r.leg_count_raw,
              r.contracts_e2,r.target_cost_e6,r.requester_known,r.match_phase,
              u.sport,u.league,u.root_event_id,u.root_map_status,u.dim_effective_us,
              u.occurrence_datetime
            FROM rfq_requests r JOIN core.universe u
              ON u.date=r.create_date AND u.market_ticker=r.market_ticker
            WHERE u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
          ), leg_market AS (
            SELECT r.request_key,r.create_date,r.create_receive_us,r.delete_recv_ns//1000
                     AS delete_receive_us,
              l.market_ticker,'COMBO_LEG' AS anchor_role,r.known_combo,r.leg_count_raw,
              r.contracts_e2,r.target_cost_e6,r.requester_known,r.match_phase,
              u.sport,u.league,u.root_event_id,u.root_map_status,u.dim_effective_us,
              u.occurrence_datetime
            FROM rfq_requests r JOIN rfq_legs l USING(request_key)
            JOIN core.universe u ON u.date=l.create_date AND u.market_ticker=l.market_ticker
            WHERE l.leg_schema_valid
              AND u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
          )
          SELECT DISTINCT * FROM (SELECT * FROM request_market UNION ALL SELECT * FROM leg_market)
        """)
    if not table_exists(connection, "rfq_clob_anchors"):
        connection.execute(f"""
          CREATE TABLE rfq_clob_anchors AS
          WITH endpoints AS (
            SELECT *, 'CREATE' AS endpoint_type,create_receive_us AS anchor_us
            FROM rfq_anchor_markets_all
            UNION ALL
            SELECT *, 'DELETE' AS endpoint_type,delete_receive_us AS anchor_us
            FROM rfq_anchor_markets_all WHERE delete_receive_us IS NOT NULL
          ), causal_endpoints AS (
            SELECT *,'CAUSAL_DIM_AS_OF_ANCHOR' AS dimension_causality_at_anchor
            FROM endpoints
            WHERE dim_effective_us IS NOT NULL AND anchor_us>=dim_effective_us
          ), ranked AS (
            SELECT *,row_number() OVER (
              PARTITION BY create_date,root_event_id,endpoint_type,anchor_role
              ORDER BY hash(request_key,market_ticker,endpoint_type),anchor_us,request_key
            ) AS root_sample_rank,
            count(*) OVER (
              PARTITION BY create_date,root_event_id,endpoint_type,anchor_role
            ) AS root_population
            FROM causal_endpoints
          ), controls AS (
            SELECT *,anchor_us-( {CONTROL_MIN_SHIFT_US} +
              abs(hash(request_key,market_ticker,endpoint_type))%{CONTROL_SHIFT_SPAN_US})::BIGINT
              AS control_anchor_us
            FROM ranked WHERE root_sample_rank<={int(max_per_root)}
          )
          SELECT *,least(1.0,{int(max_per_root)}::DOUBLE/root_population) AS inclusion_probability,
            control_anchor_us-{CONTROL_CAUSAL_LOOKBACK_US} AS control_earliest_required_us,
            dim_effective_us IS NOT NULL
              AND control_anchor_us-{CONTROL_CAUSAL_LOOKBACK_US}>=dim_effective_us
              AS control_dim_eligible
          FROM controls
        """)
    if not table_exists(connection, "rfq_relevant_markets"):
        connection.execute("""
          CREATE TABLE rfq_relevant_markets AS
          SELECT DISTINCT market_ticker FROM rfq_clob_anchors
        """)
    if not table_exists(connection, "rfq_l1_cumulative"):
        connection.execute("""
          CREATE TABLE rfq_l1_cumulative AS
          WITH same_tick AS (
            SELECT l.market_ticker,l.t_us,
              arg_max(l.yes_bid_e4,coalesce(l.recv_mono_ns,0)) AS yes_bid_e4,
              arg_max(l.yes_ask_e4,coalesce(l.recv_mono_ns,0)) AS yes_ask_e4,
              arg_max(l.yes_bid_qty_e4,coalesce(l.recv_mono_ns,0)) AS yes_bid_qty_e4,
              arg_max(l.yes_ask_qty_e4,coalesce(l.recv_mono_ns,0)) AS yes_ask_qty_e4,
              count(*) AS messages_at_tick
            FROM core.l1_real l JOIN rfq_relevant_markets m USING(market_ticker)
            WHERE l.t_us IS NOT NULL GROUP BY l.market_ticker,l.t_us
          ) SELECT *,sum(messages_at_tick) OVER (
              PARTITION BY market_ticker ORDER BY t_us ROWS BETWEEN UNBOUNDED PRECEDING
              AND CURRENT ROW) AS message_cum
            FROM same_tick
        """)
    if not table_exists(connection, "rfq_trade_cumulative"):
        connection.execute("""
          CREATE TABLE rfq_trade_cumulative AS
          WITH same_tick AS (
            SELECT t.market_ticker,t.t_us,count(*) AS trades_at_tick,
              sum(t.taker_sign*t.count_e4) AS signed_count_e4_at_tick
            FROM core.trades_safe t JOIN rfq_relevant_markets m USING(market_ticker)
            WHERE t.t_us IS NOT NULL GROUP BY t.market_ticker,t.t_us
          ) SELECT *,
            sum(trades_at_tick) OVER (PARTITION BY market_ticker ORDER BY t_us
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS trade_cum,
            sum(signed_count_e4_at_tick) OVER (PARTITION BY market_ticker ORDER BY t_us
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS signed_count_e4_cum
          FROM same_tick
        """)
    if not table_exists(connection, "rfq_l2_cumulative"):
        connection.execute("""
          CREATE TABLE rfq_l2_cumulative AS
          WITH same_tick AS (
            SELECT l.market_ticker,l.t_us,count(*) AS messages_at_tick
            FROM core.l2_all l JOIN rfq_relevant_markets m USING(market_ticker)
            WHERE l.t_us IS NOT NULL GROUP BY l.market_ticker,l.t_us
          ) SELECT *,sum(messages_at_tick) OVER (
              PARTITION BY market_ticker ORDER BY t_us ROWS BETWEEN UNBOUNDED PRECEDING
              AND CURRENT ROW) AS l2_message_cum
            FROM same_tick
        """)
    if not table_exists(connection, "rfq_market_event_times"):
        connection.execute("""
          CREATE TABLE rfq_market_event_times AS
          SELECT DISTINCT market_ticker,create_receive_us AS event_us
          FROM rfq_anchor_markets_all
          UNION
          SELECT DISTINCT market_ticker,delete_receive_us AS event_us
          FROM rfq_anchor_markets_all WHERE delete_receive_us IS NOT NULL
        """)
    if not table_exists(connection, "rfq_control_last_event"):
        connection.execute("""
          CREATE TABLE rfq_control_last_event AS
          WITH classified AS (
            SELECT *,CASE
                WHEN occurrence_datetime IS NULL THEN 'UNKNOWN'
                WHEN epoch_us(occurrence_datetime)-anchor_us<0 THEN 'IN_PLAY_OR_POST_START'
                WHEN epoch_us(occurrence_datetime)-anchor_us<=900000000 THEN '0_15M'
                WHEN epoch_us(occurrence_datetime)-anchor_us<=7200000000 THEN '15M_2H'
                WHEN epoch_us(occurrence_datetime)-anchor_us<=86400000000 THEN '2H_24H'
                ELSE 'GT_24H' END AS anchor_tts_regime,
              CASE
                WHEN occurrence_datetime IS NULL THEN 'UNKNOWN'
                WHEN epoch_us(occurrence_datetime)-control_anchor_us<0 THEN 'IN_PLAY_OR_POST_START'
                WHEN epoch_us(occurrence_datetime)-control_anchor_us<=900000000 THEN '0_15M'
                WHEN epoch_us(occurrence_datetime)-control_anchor_us<=7200000000 THEN '15M_2H'
                WHEN epoch_us(occurrence_datetime)-control_anchor_us<=86400000000 THEN '2H_24H'
                ELSE 'GT_24H' END AS control_tts_regime
            FROM rfq_clob_anchors
          ) SELECT a.request_key,a.market_ticker,a.endpoint_type,a.anchor_role,a.control_anchor_us,
            a.control_earliest_required_us,a.control_dim_eligible,
            e.event_us AS prior_rfq_event_us,
            e.event_us IS NULL OR a.control_anchor_us-e.event_us>120000000 AS prior_120s_rfq_free,
            cast(to_timestamp(a.control_anchor_us/1000000.0) AS DATE)=a.create_date
              AS same_receive_date,
            extract('hour' FROM to_timestamp(a.control_anchor_us/1000000.0))
              =extract('hour' FROM to_timestamp(a.anchor_us/1000000.0)) AS same_receive_hour,
            a.anchor_tts_regime=a.control_tts_regime AS same_tts_regime,
            a.anchor_tts_regime,a.control_tts_regime
          FROM (SELECT * FROM classified ORDER BY market_ticker,control_anchor_us) a
          ASOF LEFT JOIN (SELECT * FROM rfq_market_event_times ORDER BY market_ticker,event_us) e
            ON a.market_ticker=e.market_ticker AND a.control_anchor_us>e.event_us
        """)
    if not table_exists(connection, "rfq_control_future_events"):
        connection.execute("""
          CREATE TABLE rfq_control_future_events AS
          SELECT a.request_key,a.market_ticker,a.endpoint_type,a.anchor_role,
            count(e.event_us) AS rfq_events_in_control_outcome_120s
          FROM rfq_clob_anchors a LEFT JOIN rfq_market_event_times e
            ON e.market_ticker=a.market_ticker
              AND e.event_us>a.control_anchor_us
              AND e.event_us<=a.control_anchor_us+120000000
          GROUP BY a.request_key,a.market_ticker,a.endpoint_type,a.anchor_role
        """)
    if not table_exists(connection, "rfq_windows"):
        values = ",".join(f"({quote(label)},{start},{end})" for label, start, end in WINDOWS_US)
        connection.execute(
            "CREATE TABLE rfq_windows(window_label,start_offset_us,end_offset_us) AS "
            f"VALUES {values}"
        )
    if not table_exists(connection, "rfq_boundary_queries"):
        connection.execute("""
          CREATE TABLE rfq_boundary_queries AS
          WITH cohorts AS (
            SELECT *, 'RFQ' AS cohort,anchor_us AS cohort_anchor_us FROM rfq_clob_anchors
            UNION ALL
            SELECT *, 'PRIOR_CONTROL' AS cohort,control_anchor_us AS cohort_anchor_us
            FROM rfq_clob_anchors
          ), expanded AS (
            SELECT c.*,w.window_label,w.start_offset_us,w.end_offset_us,
              c.cohort_anchor_us+w.start_offset_us AS start_us,
              c.cohort_anchor_us+w.end_offset_us AS end_us
            FROM cohorts c CROSS JOIN rfq_windows w
          )
          SELECT *,start_us AS boundary_us,'START' AS boundary_role FROM expanded
          UNION ALL
          SELECT *,end_us AS boundary_us,'END' AS boundary_role FROM expanded
        """)
    if not table_exists(connection, "rfq_boundary_l1"):
        connection.execute("""
          CREATE TABLE rfq_boundary_l1 AS
          SELECT q.*,b.t_us AS book_t_us,b.yes_bid_e4,b.yes_ask_e4,
            b.yes_bid_qty_e4,b.yes_ask_qty_e4,b.message_cum
          FROM (SELECT * FROM rfq_boundary_queries ORDER BY market_ticker,boundary_us) q
          ASOF LEFT JOIN (SELECT * FROM rfq_l1_cumulative ORDER BY market_ticker,t_us) b
            ON q.market_ticker=b.market_ticker AND q.boundary_us>b.t_us
        """)
    if not table_exists(connection, "rfq_boundary_all"):
        connection.execute("""
          CREATE TABLE rfq_boundary_all AS
          SELECT q.*,t.t_us AS trade_t_us,t.trade_cum,t.signed_count_e4_cum
          FROM (SELECT * FROM rfq_boundary_l1 ORDER BY market_ticker,boundary_us) q
          ASOF LEFT JOIN (SELECT * FROM rfq_trade_cumulative ORDER BY market_ticker,t_us) t
            ON q.market_ticker=t.market_ticker AND q.boundary_us>t.t_us
        """)
    if not table_exists(connection, "rfq_boundary_complete"):
        connection.execute("""
          CREATE TABLE rfq_boundary_complete AS
          SELECT q.*,l.t_us AS l2_t_us,l.l2_message_cum
          FROM (SELECT * FROM rfq_boundary_all ORDER BY market_ticker,boundary_us) q
          ASOF LEFT JOIN (SELECT * FROM rfq_l2_cumulative ORDER BY market_ticker,t_us) l
            ON q.market_ticker=l.market_ticker AND q.boundary_us>l.t_us
        """)
    if not table_exists(connection, "rfq_clob_context_unmatched"):
        start_mid = _logodds_sql("s.yes_bid_e4", "s.yes_ask_e4")
        end_mid = _logodds_sql("e.yes_bid_e4", "e.yes_ask_e4")
        connection.execute(f"""
          CREATE TABLE rfq_clob_context_unmatched AS
          SELECT s.request_key,s.create_date,s.market_ticker,s.anchor_role,s.endpoint_type,
            s.anchor_us,s.control_anchor_us,s.root_sample_rank,s.root_population,
            s.inclusion_probability,s.sport,s.league,s.root_event_id,s.known_combo,
            s.leg_count_raw,s.contracts_e2,s.target_cost_e6,s.requester_known,s.match_phase,
            s.dim_effective_us,s.control_earliest_required_us,s.control_dim_eligible,
            s.cohort,s.cohort_anchor_us,s.window_label,s.start_us,s.end_us,
            s.book_t_us AS start_book_t_us,e.book_t_us AS end_book_t_us,
            s.boundary_us-s.book_t_us AS start_book_age_us,
            e.boundary_us-e.book_t_us AS end_book_age_us,
            s.yes_bid_e4 AS start_bid_e4,s.yes_ask_e4 AS start_ask_e4,
            e.yes_bid_e4 AS end_bid_e4,e.yes_ask_e4 AS end_ask_e4,
            s.yes_bid_qty_e4 AS start_bid_qty_e4,s.yes_ask_qty_e4 AS start_ask_qty_e4,
            e.yes_bid_qty_e4 AS end_bid_qty_e4,e.yes_ask_qty_e4 AS end_ask_qty_e4,
            CASE WHEN s.yes_bid_qty_e4+s.yes_ask_qty_e4>0 THEN
              (s.yes_bid_qty_e4-s.yes_ask_qty_e4)::DOUBLE/
                (s.yes_bid_qty_e4+s.yes_ask_qty_e4) END AS start_imbalance,
            CASE WHEN e.yes_bid_qty_e4+e.yes_ask_qty_e4>0 THEN
              (e.yes_bid_qty_e4-e.yes_ask_qty_e4)::DOUBLE/
                (e.yes_bid_qty_e4+e.yes_ask_qty_e4) END AS end_imbalance,
            ({start_mid}) AS start_mid_logodds,({end_mid}) AS end_mid_logodds,
            ({end_mid})-({start_mid}) AS mid_logodds_change,
            abs(({end_mid})-({start_mid}))>=0.10 AS fixed_logodds_jump,
            (ln(greatest(1.0,least(9999.0,e.yes_ask_e4))/(10000.0-greatest(1.0,least(9999.0,e.yes_ask_e4))))
             -ln(greatest(1.0,least(9999.0,e.yes_bid_e4))/(10000.0-greatest(1.0,least(9999.0,e.yes_bid_e4)))))
              -(ln(greatest(1.0,least(9999.0,s.yes_ask_e4))/(10000.0-greatest(1.0,least(9999.0,s.yes_ask_e4))))
             -ln(greatest(1.0,least(9999.0,s.yes_bid_e4))/(10000.0-greatest(1.0,least(9999.0,s.yes_bid_e4)))))
              AS spread_logodds_change,
            (e.yes_bid_qty_e4+e.yes_ask_qty_e4)-(s.yes_bid_qty_e4+s.yes_ask_qty_e4)
              AS depth_change_e4,
            greatest(0,(s.yes_bid_qty_e4+s.yes_ask_qty_e4)
                       -(e.yes_bid_qty_e4+e.yes_ask_qty_e4)) AS net_depleted_depth_e4,
            greatest(0,(e.yes_bid_qty_e4+e.yes_ask_qty_e4)
                       -(s.yes_bid_qty_e4+s.yes_ask_qty_e4)) AS net_refilled_depth_e4,
            (coalesce(e.message_cum,0)-coalesce(s.message_cum,0)) AS l1_messages,
            (coalesce(e.trade_cum,0)-coalesce(s.trade_cum,0)) AS trades,
            (coalesce(e.signed_count_e4_cum,0)-coalesce(s.signed_count_e4_cum,0))
              AS signed_trade_count_e4,
            (coalesce(e.l2_message_cum,0)-coalesce(s.l2_message_cum,0)) AS l2_messages,
            s.book_t_us IS NOT NULL AND e.book_t_us IS NOT NULL
              AND s.boundary_us-s.book_t_us<={BOOK_AGE_CAP_US}
              AND e.boundary_us-e.book_t_us<={BOOK_AGE_CAP_US}
              AND s.yes_bid_e4>0 AND s.yes_ask_e4<10000 AND s.yes_bid_e4<s.yes_ask_e4
              AND e.yes_bid_e4>0 AND e.yes_ask_e4<10000 AND e.yes_bid_e4<e.yes_ask_e4
              AND (s.cohort='RFQ' OR s.control_dim_eligible)
              AND NOT EXISTS (SELECT 1 FROM core.capture_gaps g
                WHERE g.start_us<s.end_us AND g.end_us>s.start_us)
              AS valid_receive_window
          FROM rfq_boundary_complete s JOIN rfq_boundary_complete e
            ON e.request_key=s.request_key AND e.market_ticker=s.market_ticker
              AND e.endpoint_type=s.endpoint_type AND e.anchor_role=s.anchor_role
              AND e.cohort=s.cohort
              AND e.window_label=s.window_label AND e.boundary_role='END'
          WHERE s.boundary_role='START'
        """)
    if not table_exists(connection, "rfq_control_balance"):
        connection.execute("""
          CREATE TABLE rfq_control_balance AS
          WITH baseline AS (
            SELECT request_key,market_ticker,endpoint_type,anchor_role,cohort,
              start_mid_logodds,start_bid_e4,start_ask_e4,
              start_bid_qty_e4+start_ask_qty_e4 AS start_depth_e4,l1_messages,
              valid_receive_window
            FROM rfq_clob_context_unmatched WHERE window_label='m10_m1'
          ), paired AS (
            SELECT t.request_key,t.market_ticker,t.endpoint_type,t.anchor_role,
              t.valid_receive_window AS treatment_valid,c.valid_receive_window AS control_valid,
              floor((10000.0/(1.0+exp(-t.start_mid_logodds)))/1000.0) AS treatment_price_band,
              floor((10000.0/(1.0+exp(-c.start_mid_logodds)))/1000.0) AS control_price_band,
              floor((ln(greatest(1.0,least(9999.0,t.start_ask_e4))/(10000.0-greatest(1.0,least(9999.0,t.start_ask_e4))))
                -ln(greatest(1.0,least(9999.0,t.start_bid_e4))/(10000.0-greatest(1.0,least(9999.0,t.start_bid_e4)))))/0.05)
                AS treatment_spread_bucket,
              floor((ln(greatest(1.0,least(9999.0,c.start_ask_e4))/(10000.0-greatest(1.0,least(9999.0,c.start_ask_e4))))
                -ln(greatest(1.0,least(9999.0,c.start_bid_e4))/(10000.0-greatest(1.0,least(9999.0,c.start_bid_e4)))))/0.05)
                AS control_spread_bucket,
              floor(ln(1+greatest(0,t.start_depth_e4)/10000.0)/ln(2)) AS treatment_depth_bucket,
              floor(ln(1+greatest(0,c.start_depth_e4)/10000.0)/ln(2)) AS control_depth_bucket,
              floor(ln(1+greatest(0,t.l1_messages))/ln(2)) AS treatment_activity_bucket,
              floor(ln(1+greatest(0,c.l1_messages))/ln(2)) AS control_activity_bucket
            FROM baseline t JOIN baseline c
              USING(request_key,market_ticker,endpoint_type,anchor_role)
            WHERE t.cohort='RFQ' AND c.cohort='PRIOR_CONTROL'
          ) SELECT p.*,treatment_valid AND control_valid
            AND treatment_price_band=control_price_band
            AND treatment_spread_bucket=control_spread_bucket
            AND treatment_depth_bucket=control_depth_bucket
            AND abs(treatment_activity_bucket-control_activity_bucket)<=1
            AND q.prior_120s_rfq_free AND f.rfq_events_in_control_outcome_120s=0
            AND q.same_receive_date AND q.same_receive_hour AND q.same_tts_regime
            AND q.anchor_tts_regime<>'UNKNOWN' AND q.control_dim_eligible AS matched,
            q.prior_120s_rfq_free,f.rfq_events_in_control_outcome_120s,
            q.same_receive_date,q.same_receive_hour,q.same_tts_regime,
            q.anchor_tts_regime,q.control_tts_regime,q.control_earliest_required_us,
            q.control_dim_eligible,q.prior_rfq_event_us
          FROM paired p JOIN rfq_control_last_event q
            USING(request_key,market_ticker,endpoint_type,anchor_role)
          JOIN rfq_control_future_events f
            USING(request_key,market_ticker,endpoint_type,anchor_role)
        """)
    if not table_exists(connection, "rfq_clob_context"):
        connection.execute("""
          CREATE TABLE rfq_clob_context AS
          SELECT c.*,b.matched AS matched_control_pair,
            'RECEIVE_CLOCK_BOUNDARY_ASOF' AS clock_method,
            'RFQ_CLOB_EVENT_STUDY_ROOT_BALANCED_SAMPLE' AS population_method
          FROM rfq_clob_context_unmatched c JOIN rfq_control_balance b
            USING(request_key,market_ticker,endpoint_type,anchor_role)
        """)
    if not table_exists(connection, "rfq_clob_event_study_summary"):
        connection.execute("""
          CREATE TABLE rfq_clob_event_study_summary AS
          WITH paired AS (
            SELECT t.create_date,t.sport,t.endpoint_type,t.anchor_role,t.window_label,
              t.root_event_id,t.request_key,t.market_ticker,
              t.mid_logodds_change-c.mid_logodds_change AS paired_mid_logodds_effect,
              abs(t.mid_logodds_change)-abs(c.mid_logodds_change) AS paired_abs_logodds_effect,
              t.spread_logodds_change-c.spread_logodds_change AS paired_spread_effect,
              t.depth_change_e4-c.depth_change_e4 AS paired_depth_effect_e4,
              t.net_depleted_depth_e4-c.net_depleted_depth_e4
                AS paired_net_depletion_effect_e4,
              t.net_refilled_depth_e4-c.net_refilled_depth_e4
                AS paired_net_refill_effect_e4,
              (t.end_imbalance-t.start_imbalance)-(c.end_imbalance-c.start_imbalance)
                AS paired_imbalance_effect,
              t.l1_messages-c.l1_messages AS paired_l1_message_effect,
              t.l2_messages-c.l2_messages AS paired_l2_message_effect,
              t.trades-c.trades AS paired_trade_count_effect,
              t.signed_trade_count_e4-c.signed_trade_count_e4 AS paired_signed_trade_effect_e4,
              cast(t.fixed_logodds_jump AS INTEGER)-cast(c.fixed_logodds_jump AS INTEGER)
                AS paired_jump_probability_effect
            FROM rfq_clob_context t JOIN rfq_clob_context c
              USING(request_key,market_ticker,endpoint_type,anchor_role,window_label)
            WHERE t.cohort='RFQ' AND c.cohort='PRIOR_CONTROL'
              AND t.matched_control_pair AND t.valid_receive_window AND c.valid_receive_window
          ), root_first AS (
            SELECT create_date,sport,endpoint_type,anchor_role,window_label,root_event_id,
              avg(paired_mid_logodds_effect) AS root_mid_logodds_effect,
              avg(paired_abs_logodds_effect) AS root_abs_logodds_effect,
              avg(paired_spread_effect) AS root_spread_effect,
              avg(paired_depth_effect_e4) AS root_depth_effect_e4,
              avg(paired_net_depletion_effect_e4) AS root_net_depletion_effect_e4,
              avg(paired_net_refill_effect_e4) AS root_net_refill_effect_e4,
              avg(paired_imbalance_effect) AS root_imbalance_effect,
              avg(paired_l1_message_effect) AS root_l1_message_effect,
              avg(paired_l2_message_effect) AS root_l2_message_effect,
              avg(paired_trade_count_effect) AS root_trade_count_effect,
              avg(paired_signed_trade_effect_e4) AS root_signed_trade_effect_e4,
              avg(paired_jump_probability_effect) AS root_jump_probability_effect,
              count(*) AS request_market_pairs
            FROM paired GROUP BY create_date,sport,endpoint_type,anchor_role,window_label,root_event_id
          ) SELECT create_date,sport,endpoint_type,anchor_role,window_label,
            count(*) AS root_events,sum(request_market_pairs) AS request_market_pairs,
            avg(root_mid_logodds_effect) AS mean_root_mid_logodds_effect,
            median(root_mid_logodds_effect) AS median_root_mid_logodds_effect,
            avg(root_abs_logodds_effect) AS mean_root_abs_logodds_effect,
            avg(root_spread_effect) AS mean_root_spread_effect,
            avg(root_depth_effect_e4) AS mean_root_depth_effect_e4,
            avg(root_net_depletion_effect_e4) AS mean_root_net_depletion_effect_e4,
            avg(root_net_refill_effect_e4) AS mean_root_net_refill_effect_e4,
            avg(root_imbalance_effect) AS mean_root_imbalance_effect,
            avg(root_l1_message_effect) AS mean_root_l1_message_effect,
            avg(root_l2_message_effect) AS mean_root_l2_message_effect,
            avg(root_trade_count_effect) AS mean_root_trade_count_effect,
            avg(root_signed_trade_effect_e4) AS mean_root_signed_trade_effect_e4,
            avg(root_jump_probability_effect) AS mean_root_jump_probability_effect
          FROM root_first GROUP BY create_date,sport,endpoint_type,anchor_role,window_label
          ORDER BY create_date,sport,endpoint_type,anchor_role,window_label
        """)
    if not table_exists(connection, "rfq_combo_clob_proxy"):
        connection.execute("""
          CREATE TABLE rfq_combo_clob_proxy AS
          WITH leg_pre AS (
            SELECT r.request_key,r.create_date,r.root_event_id,r.sport,r.leg_count_raw,
              r.contracts_e2,r.target_cost_e6,l.leg_index,l.market_ticker,l.side,
              c.start_bid_e4,c.start_ask_e4,c.start_bid_qty_e4,c.start_ask_qty_e4,
              c.valid_receive_window,
              CASE WHEN l.side='yes' THEN c.start_ask_e4
                   WHEN l.side='no' THEN 10000-c.start_bid_e4 END
                AS indicative_leg_taker_cost_e4
            FROM rfq_requests r JOIN rfq_legs l USING(request_key)
            JOIN rfq_clob_context c
              ON c.request_key=l.request_key AND c.market_ticker=l.market_ticker
            WHERE r.known_combo AND l.leg_schema_valid AND c.endpoint_type='CREATE'
              AND c.anchor_role='COMBO_LEG' AND c.cohort='RFQ'
              AND c.window_label='p0_100ms'
          ) SELECT request_key,create_date,root_event_id,sport,leg_count_raw,
            count(*) AS observed_legs,
            count(indicative_leg_taker_cost_e4) FILTER (WHERE valid_receive_window)
              AS priced_legs,
            sum(indicative_leg_taker_cost_e4) FILTER (WHERE valid_receive_window)
              AS indicative_leg_cost_sum_e4,
            min(least(start_bid_qty_e4,start_ask_qty_e4)) FILTER (WHERE valid_receive_window)
              AS minimum_top_depth_e4,
            CASE WHEN contracts_e2>0 AND target_cost_e6 IS NOT NULL
              THEN target_cost_e6::DOUBLE/contracts_e2 END AS target_intent_per_contract_e4,
            CASE WHEN count(indicative_leg_taker_cost_e4) FILTER (WHERE valid_receive_window)
                       =leg_count_raw
                    AND contracts_e2>0 AND target_cost_e6 IS NOT NULL
              THEN target_cost_e6::DOUBLE/contracts_e2
                   -sum(indicative_leg_taker_cost_e4) FILTER (WHERE valid_receive_window)
              END AS indicative_intent_minus_leg_cost_proxy_e4,
            'INDICATIVE_CLOB_PROXY_NOT_RFQ_QUOTE_OR_PNL' AS interpretation
          FROM leg_pre
          GROUP BY request_key,create_date,root_event_id,sport,leg_count_raw,
                   contracts_e2,target_cost_e6
        """)
    return {
        "candidate_anchor_markets": scalar(
            connection, "SELECT count(*) FROM rfq_anchor_markets_all"
        ),
        "candidate_endpoint_anchors": scalar(connection, """
          SELECT count(*) FROM (
            SELECT create_receive_us AS anchor_us,dim_effective_us FROM rfq_anchor_markets_all
            UNION ALL
            SELECT delete_receive_us,dim_effective_us FROM rfq_anchor_markets_all
              WHERE delete_receive_us IS NOT NULL
          )
        """),
        "causal_eligible_endpoint_anchors": scalar(connection, """
          SELECT count(*) FROM (
            SELECT create_receive_us AS anchor_us,dim_effective_us FROM rfq_anchor_markets_all
            UNION ALL
            SELECT delete_receive_us,dim_effective_us FROM rfq_anchor_markets_all
              WHERE delete_receive_us IS NOT NULL
          ) WHERE dim_effective_us IS NOT NULL AND anchor_us>=dim_effective_us
        """),
        "posthoc_endpoint_anchors_excluded": scalar(connection, """
          SELECT count(*) FROM (
            SELECT create_receive_us AS anchor_us,dim_effective_us FROM rfq_anchor_markets_all
            UNION ALL
            SELECT delete_receive_us,dim_effective_us FROM rfq_anchor_markets_all
              WHERE delete_receive_us IS NOT NULL
          ) WHERE dim_effective_us IS NULL OR anchor_us<dim_effective_us
        """),
        "sampled_endpoint_anchors": scalar(connection, "SELECT count(*) FROM rfq_clob_anchors"),
        "control_anchors_excluded_before_dim_effective": scalar(
            connection,
            "SELECT count(*) FROM rfq_clob_anchors WHERE NOT control_dim_eligible",
        ),
        "control_anchors_dim_eligible": scalar(
            connection,
            "SELECT count(*) FROM rfq_clob_anchors WHERE control_dim_eligible",
        ),
        "clob_context_rows": scalar(connection, "SELECT count(*) FROM rfq_clob_context"),
        "matched_pairs": scalar(connection, "SELECT count(*) FROM rfq_control_balance WHERE matched"),
        "max_per_root_stratum": max_per_root,
    }


def read_downsampled_km(connection, points: int = 5000) -> list[tuple[int, int, int, int, float]]:
    rows = scalar(connection, "SELECT count(*) FROM rfq_lifecycle_km_curve")
    stride = max(1, math.ceil(rows / points))
    return connection.execute(f"""
      WITH x AS (
        SELECT *,row_number() OVER (ORDER BY duration_ms) AS rn,
          count(*) OVER () AS total_rows
        FROM rfq_lifecycle_km_curve
      ) SELECT duration_ms,at_risk,deaths,censored,survival FROM x
        WHERE rn=1 OR rn=total_rows OR rn%{stride}=0 ORDER BY duration_ms
    """).fetchall()


def write_km_table(connection, table_dir: Path) -> list[tuple[int, int, int, int, float]]:
    km = read_downsampled_km(connection)
    path = table_dir / "rfq_lifetime_km.csv"
    payload = "duration_ms,at_risk,deaths,censored,survival\n" + "".join(
        ",".join(map(str, row)) + "\n" for row in km
    )
    write_text_atomic(path, payload)
    return km


def export_table(connection, table: str, path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        partial.unlink()
    order_by = {
        "rfq_requests": "create_date,create_receive_us,request_key",
        "rfq_lifecycle": "create_date,create_receive_us,request_key",
        "rfq_legs": "create_date,create_receive_us,request_key,leg_index,market_ticker",
        "rfq_clob_context": (
            "create_date,anchor_us,request_key,market_ticker,endpoint_type,"
            "anchor_role,cohort,window_label"
        ),
    }.get(table)
    if order_by is None:
        raise RFQStageError(f"no deterministic Parquet export ordering registered: {table}")
    connection.execute(
        f"COPY (SELECT * FROM {table} ORDER BY {order_by}) TO {quote(partial)} "
        "(FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 100000)"
    )
    os.replace(partial, path)


def export_csv(connection, table: str, path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        partial.unlink()
    connection.execute(
        f"COPY (SELECT * FROM {table} ORDER BY ALL) TO {quote(partial)} "
        "(FORMAT CSV,HEADER,DELIMITER ',')"
    )
    os.replace(partial, path)


def export_outputs(connection, run_dir: Path) -> dict:
    table_dir = run_dir / "REPORT/tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    large = ("rfq_requests", "rfq_lifecycle", "rfq_legs", "rfq_clob_context")
    for table in large:
        export_table(connection, table, table_dir / f"{table}.parquet")
    small = (
        "rfq_schema_signatures", "rfq_schema_field_audit", "rfq_channel_qc",
        "rfq_observation_boundaries",
        "rfq_flow_daily", "rfq_event_flow_hourly", "rfq_flow_hourly", "rfq_flow_minute",
        "rfq_burst_summary",
        "rfq_interarrival", "rfq_interarrival_histogram", "rfq_size_summary",
        "rfq_size_histogram", "rfq_tts_size_summary", "rfq_population_mix",
        "rfq_root_event_concentration", "rfq_lifecycle_summary", "rfq_lifecycle_strata",
        "rfq_combo_summary", "rfq_combo_side_summary", "rfq_bundle_frequency",
        "rfq_collection_concentration", "rfq_combo_clob_proxy",
        "rfq_requester_summary", "rfq_requester_concentration",
        "rfq_unmatched_delete_summary",
        "rfq_clob_event_study_summary", "rfq_control_balance",
    )
    for table in small:
        path = table_dir / f"{table}.csv"
        export_csv(connection, table, path)
    km = write_km_table(connection, table_dir)
    return {
        "parquet_tables": [f"REPORT/tables/{name}.parquet" for name in large],
        "csv_tables": [f"REPORT/tables/{name}.csv" for name in small]
        + ["REPORT/tables/rfq_lifetime_km.csv"],
        "km_points": len(km),
    }


def _downsample(rows: Sequence, points: int = 2000) -> list:
    if len(rows) <= points:
        return list(rows)
    indexes = sorted({round(i * (len(rows) - 1) / (points - 1)) for i in range(points)})
    return [rows[index] for index in indexes]


def render_charts(connection, run_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_dir = run_dir / "REPORT/charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    output = []

    hourly = connection.execute("""
      SELECT create_date,utc_hour,sum(requests) FROM rfq_flow_hourly
      GROUP BY create_date,utc_hour ORDER BY create_date,utc_hour
    """).fetchall()
    dates = sorted({str(row[0]) for row in hourly})
    matrix = [[0] * 24 for _ in dates]
    for date, hour, value in hourly:
        matrix[dates.index(str(date))][int(hour)] = int(value)
    fig, ax = plt.subplots(figsize=(12, 3 + len(dates)))
    image = ax.imshow(matrix, aspect="auto", cmap="viridis")
    ax.set_yticks(range(len(dates)), dates); ax.set_xticks(range(24))
    ax.set_xlabel("UTC hour"); ax.set_title("RFQ create flow by receive-clock hour\n" + BANNER)
    fig.colorbar(image, ax=ax, label="requests")
    fig.tight_layout(); path = chart_dir / "rfq_hourly_heatmap.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    interarrival = connection.execute("""
      SELECT log10_interarrival_us_bin,observations FROM rfq_interarrival_histogram
      ORDER BY log10_interarrival_us_bin
    """).fetchall()
    fig, ax = plt.subplots(figsize=(9, 6))
    if interarrival:
        total = sum(int(row[1]) for row in interarrival)
        running = 0
        ys = []
        for _, count in interarrival:
            running += int(count); ys.append(running / total)
        ax.step([10 ** float(row[0]) for row in interarrival], ys, where="post")
    ax.set_xscale("log"); ax.set_xlabel("Create inter-arrival (receive µs; log-binned)")
    ax.set_ylabel("Approximate ECDF")
    ax.set_title("RFQ create inter-arrival distribution\n" + BANNER)
    ax.grid(alpha=.25); fig.tight_layout(); path = chart_dir / "rfq_interarrival_ecdf.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    size_rows = connection.execute("""
      SELECT field,log10_value_bin,observations FROM rfq_size_histogram
      ORDER BY field,log10_value_bin
    """).fetchall()
    fig, ax = plt.subplots(figsize=(9, 6))
    for field in sorted({row[0] for row in size_rows}):
        rows = [row for row in size_rows if row[0] == field]
        total = sum(int(row[2]) for row in rows)
        remaining = total
        ys = []
        for row in rows:
            ys.append(remaining / total if total else 0); remaining -= int(row[2])
        ax.step([10 ** float(row[1]) for row in rows], ys, where="post", label=field)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("Stored fixed-point magnitude (log-binned)"); ax.set_ylabel("Approximate P(X ≥ x)")
    ax.set_title("Full-scan RFQ size and target-cost tails\n" + BANNER)
    ax.legend(); ax.grid(alpha=.25); fig.tight_layout(); path = chart_dir / "rfq_size_target_ccdf.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    leg_counts = connection.execute("""
      SELECT legs,sum(requests) FROM rfq_combo_summary WHERE known_combo
      GROUP BY legs ORDER BY legs
    """).fetchall()
    fig, ax = plt.subplots(figsize=(9, 5))
    if leg_counts:
        ax.bar([str(row[0]) for row in leg_counts], [int(row[1]) for row in leg_counts])
    ax.set_xlabel("Observed selected-leg count"); ax.set_ylabel("Known-combo requests")
    ax.set_title("Known-combo leg-count distribution (lower-bound population)\n" + BANNER)
    fig.tight_layout(); path = chart_dir / "rfq_combo_leg_count.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    tts = connection.execute("""
      SELECT tts_bucket,sum(requests) FROM rfq_tts_size_summary
      GROUP BY tts_bucket ORDER BY tts_bucket
    """).fetchall()
    fig, ax = plt.subplots(figsize=(10, 5))
    if tts:
        ax.bar([row[0] for row in tts], [int(row[1]) for row in tts], color="#6a994e")
    ax.set_xlabel("Time-to-start bucket"); ax.set_ylabel("Mapped Sports RFQs")
    ax.tick_params(axis="x", rotation=25)
    ax.set_title("RFQ time-to-start / match-phase mix\n" + BANNER)
    fig.tight_layout(); path = chart_dir / "rfq_tts_mix.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    km = read_downsampled_km(connection, points=2000)
    fig, ax = plt.subplots(figsize=(10, 6))
    if km:
        ax.step([max(0.001, row[0] / 1000.0) for row in km], [row[4] for row in km], where="post")
    ax.set_xscale("log"); ax.set_xlabel("RFQ receive-clock age (seconds)")
    ax.set_ylabel("Kaplan–Meier survival")
    ax.set_title("Full-scan RFQ lifecycle with replacement/boundary censoring\n" + BANNER)
    ax.grid(alpha=.25); fig.tight_layout(); path = chart_dir / "rfq_lifetime_survival_full.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    requester_counts = [row[0] for row in connection.execute(
        "SELECT requests FROM rfq_requester_summary ORDER BY requests"
    ).fetchall()]
    fig, ax = plt.subplots(figsize=(9, 6))
    if requester_counts:
        total = sum(requester_counts)
        cumulative = [0.0]
        running = 0
        for value in requester_counts:
            running += value; cumulative.append(running / total)
        population = [i / len(requester_counts) for i in range(len(requester_counts) + 1)]
        ax.plot(population, cumulative, label="known requester subset")
    ax.plot([0, 1], [0, 1], linestyle="--", color="grey", label="equality")
    ax.set_xlabel("Cumulative share of hashed requesters")
    ax.set_ylabel("Cumulative share of requests")
    ax.set_title("Requester concentration (known-ID subset only)\n" + BANNER)
    ax.legend(); ax.grid(alpha=.25); fig.tight_layout(); path = chart_dir / "rfq_requester_lorenz.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    coverage = connection.execute("""
      SELECT count(*),count(*) FILTER (WHERE delete_observed),
        count(*) FILTER (WHERE requester_known) FROM rfq_lifecycle
    """).fetchone()
    total = coverage[0] or 1
    fig, ax = plt.subplots(figsize=(8, 5))
    labels = ["valid creates", "delete endpoint", "known requester"]
    values = [1.0, coverage[1] / total, coverage[2] / total]
    ax.bar(labels, values, color=["#4da3ff", "#52b788", "#f4a261"])
    ax.set_ylim(0, 1); ax.set_ylabel("Share of valid create cohort")
    ax.set_title("Lifecycle censoring and requester-ID coverage\n" + BANNER)
    fig.tight_layout(); path = chart_dir / "rfq_censor_requester_coverage.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    studies = connection.execute("""
      SELECT window_label,sum(request_market_pairs),
        avg(mean_root_abs_logodds_effect),avg(mean_root_l1_message_effect)
      FROM rfq_clob_event_study_summary WHERE endpoint_type='CREATE'
      GROUP BY window_label
      ORDER BY CASE window_label
        WHEN 'm30_m10' THEN 1 WHEN 'm10_m1' THEN 2 WHEN 'm1_0' THEN 3
        WHEN 'p0_100ms' THEN 4 WHEN 'p100ms_1s' THEN 5 WHEN 'p1s_3s' THEN 6
        WHEN 'p3s_10s' THEN 7 WHEN 'p10s_30s' THEN 8 ELSE 9 END
    """).fetchall()
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    labels = [row[0] for row in studies]
    axes[0].plot(labels, [float(row[2] or 0) for row in studies], marker="o")
    axes[0].axhline(0, color="grey", linewidth=1); axes[0].set_ylabel("Paired |Δ log-odds| effect")
    axes[1].plot(labels, [float(row[3] or 0) for row in studies], marker="o", color="#e76f51")
    axes[1].axhline(0, color="grey", linewidth=1); axes[1].set_ylabel("Paired L1-message effect")
    axes[1].tick_params(axis="x", rotation=35)
    fig.suptitle("RFQ-create → CLOB receive-clock event study (matched diagnostics)\n" + BANNER)
    fig.tight_layout(); path = chart_dir / "rfq_clob_event_study.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))
    return output


def build_summary(connection, inputs: dict, clob: dict, elapsed: float) -> dict:
    total = scalar(connection, "SELECT count(*) FROM rfq_requests")
    observed = scalar(connection, "SELECT count(*) FROM rfq_lifecycle WHERE delete_observed")
    requester = scalar(connection, "SELECT count(*) FROM rfq_requests WHERE requester_known")
    combos = scalar(connection, "SELECT count(*) FROM rfq_requests WHERE known_combo")
    scan_counts = rows_as_dicts(connection, "SELECT * FROM rfq_scan_counts")[0]
    valid_frames = scan_counts["deduplicated_valid_frames"]
    raw_rfq_frames = scan_counts["raw_rfq_frames"]
    duplicate_frames = scan_counts["valid_contract_frames"] - valid_frames
    boundary_censored = scalar(
        connection,
        "SELECT count(*) FROM rfq_lifecycle "
        "WHERE endpoint_type='RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY'",
    )
    next_create_censored = scalar(
        connection,
        "SELECT count(*) FROM rfq_lifecycle "
        "WHERE endpoint_type='RIGHT_CENSORED_AT_NEXT_CREATE'",
    )
    scan_end_censored = scalar(
        connection,
        "SELECT count(*) FROM rfq_lifecycle "
        "WHERE endpoint_type='RIGHT_CENSORED_AT_SCAN_END'",
    )
    cross_boundary_requests = scalar(
        connection,
        "SELECT count(*) FROM rfq_requests WHERE delete_crosses_observation_boundary",
    )
    cross_boundary_deletes = scalar(
        connection,
        "SELECT count(*) FROM rfq_unmatched_deletes "
        "WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'",
    )
    causal_dim_requests = scalar(
        connection,
        "SELECT count(*) FROM rfq_requests WHERE dimension_causality='CAUSAL_AS_OF_CREATE'",
    )
    posthoc_dim_requests = scalar(
        connection,
        "SELECT count(*) FROM rfq_requests "
        "WHERE dimension_causality='POSTHOC_DIM_NOT_CAUSAL_AT_CREATE'",
    )
    requester_rows = connection.execute(
        "SELECT requests FROM rfq_requester_summary ORDER BY requests DESC"
    ).fetchall()
    known_total = sum(int(row[0]) for row in requester_rows)
    hhi = (
        sum((int(row[0]) / known_total) ** 2 for row in requester_rows)
        if known_total else None
    )
    return {
        "schema": "sports-autoresearch-rfq-full-stage-v1",
        "generated_at_utc": utc_now(),
        "banner": BANNER,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "evidence": EVIDENCE,
        "timestamp": "TL1_RECEIVE_CLOCK",
        "status": "DESCRIPTIVE_DISCOVERY_ONLY",
        "input": {
            "release_ids": list(RELEASE_IDS),
            "objects": inputs["objects"],
            "logical_manifest_bindings": inputs["logical_manifest_bindings"],
            "deduplicated_overlapping_objects": inputs["deduplicated_overlapping_objects"],
            "overlap_keys": inputs["overlap_keys"],
            "bytes": inputs["bytes"],
            "path_size_fingerprint_sha256": inputs["path_size_fingerprint_sha256"],
            "outer_parser": "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort",
        },
        "counts": {
            "raw_rfq_frames": raw_rfq_frames,
            "valid_deduplicated_frames": valid_frames,
            "deduplicated_valid_frame_copies": duplicate_frames,
            "valid_requests": total,
            "observed_first_valid_deletes": observed,
            "right_censored_creates": total - observed,
            "right_censored_at_next_same_id_create": next_create_censored,
            "right_censored_at_observation_boundary": boundary_censored,
            "right_censored_at_scan_end": scan_end_censored,
            "observation_boundary_timestamps": scalar(
                connection, "SELECT count(*) FROM rfq_observation_boundaries"
            ),
            "cross_observation_boundary_requests": cross_boundary_requests,
            "cross_observation_boundary_delete_rows": cross_boundary_deletes,
            "known_combo_lower_bound": combos,
            "known_requester_requests": requester,
            "causal_dim_requests_at_create": causal_dim_requests,
            "posthoc_dim_requests_excluded_from_causal_tts": posthoc_dim_requests,
        },
        "coverage": {
            "delete_endpoint_share": observed / total if total else None,
            "right_censored_share": (total - observed) / total if total else None,
            "requester_id_share": requester / total if total else None,
            "known_combo_lower_bound_share": combos / total if total else None,
            "requester_hhi_known_subset": hhi,
            "causal_dim_share": causal_dim_requests / total if total else None,
            "capture_completeness_is_not_lifecycle_join_completeness": True,
        },
        "clob": clob,
        "hard_truth": {
            "broadcast_contains_accepted_quote_or_fill": False,
            "forbidden_claims": [
                "RFQ acceptance rate", "RFQ fill rate", "quote hit rate",
                "actual RFQ maker PnL", "actual quote competitiveness",
                "winner's curse requiring an observed accepted quote",
            ],
            "indicative_clob_only": True,
        },
        "limitations": [
            "Only two prior-exposed degraded day blocks are available.",
            "RFQ per-event sequence is not mandatory; sequence completeness is not claimed.",
            "A next genuine same-ID create or the first explicit loss/close/error/epoch boundary, whichever occurs first, censors the prior lifecycle; residual scan-end censoring can still combine true survival with unobserved endpoints.",
            "Known-combo share is a lower bound; no-combo-evidence is not proof of single/HVM status.",
            "CLOB event studies use only dim-effective anchors and a deterministic root-balanced sample, not the full RFQ population.",
            "Prior controls are match-eligible only when their full 120-second causal lookback starts at or after dim_effective_us.",
            "Control matching adjusts only observed state and cannot remove unobserved game-state confounding.",
        ],
        "wall_seconds": round(elapsed, 3),
    }


def write_catalog(run_dir: Path) -> None:
    import duckdb

    path = run_dir / "cache/rfq_full_catalog.duckdb"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        for table in ("rfq_requests", "rfq_lifecycle", "rfq_legs", "rfq_clob_context"):
            parquet = run_dir / "REPORT/tables" / f"{table}.parquet"
            connection.execute(
                f"CREATE VIEW {table} AS SELECT * FROM read_parquet({quote(parquet)})"
            )
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    validate_windows()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--cache-root", default="/srv/w09-research/cache", type=Path)
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--max-temp-size", default="120GB")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--min-free-gib", type=float, default=120.0)
    parser.add_argument("--clob-max-per-root", type=int, default=50)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keep-scratch", action="store_true")
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("threads must be positive")
    started = time.time()
    run_dir = args.run_dir.resolve()
    manifest = validate_run(run_dir)
    inputs = discover_inputs(args.cache_root.resolve())
    validate_input_bindings(manifest, inputs)

    import duckdb

    if duckdb.__version__ != EXPECTED_DUCKDB:
        raise RFQStageError(
            f"DuckDB version drift (runner={duckdb.__version__}, expected={EXPECTED_DUCKDB})"
        )
    input_identity = {
        "schema": "rfq-full-input-identity-v1",
        "release_ids": list(RELEASE_IDS),
        "releases": inputs["releases"],
        "unique_objects": inputs["objects"],
        "logical_manifest_bindings": inputs["logical_manifest_bindings"],
        "deduplicated_overlapping_objects": inputs["deduplicated_overlapping_objects"],
        "path_size_sha_fingerprint": inputs["path_size_fingerprint_sha256"],
        "objects": [
            {key: row[key] for key in ("key", "sha256", "size", "bound_release_ids")}
            for row in inputs["objects_detail"]
        ],
    }
    write_json(run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json", input_identity)
    free = shutil.disk_usage(run_dir).free
    required = max(args.min_free_gib * 2**30, inputs["bytes"] * 1.75)
    if free < required:
        raise RFQStageError(
            f"insufficient disk headroom: free={free/2**30:.1f}GiB "
            f"required={required/2**30:.1f}GiB"
        )
    scratch = run_dir / "cache/rfq_full_scratch.duckdb"
    state_path = run_dir / "REPORT/tables/RFQ_FULL_STAGE_STATE.json"
    if scratch.exists() and not args.resume:
        raise RFQStageError("RFQ scratch DB exists; pass --resume after inspecting stage state")
    previous_state = None
    if scratch.exists() and args.resume:
        if not state_path.is_file():
            raise RFQStageError("resume requested but RFQ stage state is missing")
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))
        if previous_state.get("input_fingerprint") != inputs["path_size_fingerprint_sha256"]:
            raise RFQStageError("resume input fingerprint mismatch")

    connection = duckdb.connect(str(scratch))
    configure(connection, run_dir, args.memory_limit, args.max_temp_size, args.threads)
    state = dict(previous_state or {})
    state.update({
        "schema": "rfq-full-stage-state-v1", "started_at_utc": utc_now(),
        "status": "RUNNING", "input_fingerprint": inputs["path_size_fingerprint_sha256"],
        "scratch": str(scratch), "resume": args.resume,
    })
    write_json(state_path, state)
    try:
        build_scan_tables(connection, inputs["paths"], run_dir.name)
        state.update({"phase": "RAW_SCHEMA_AND_DEDUP_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        build_request_tables(connection, run_dir.name)
        state.update({"phase": "LIFECYCLE_AND_LEGS_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        attach_core_and_enrich(connection, run_dir / "cache/cycle1.duckdb")
        build_descriptive_tables(connection)
        state.update({"phase": "FULL_DESCRIPTIVE_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        clob = build_clob_context(connection, args.clob_max_per_root)
        state.update({"phase": "CLOB_EVENT_STUDY_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        exports = export_outputs(connection, run_dir)
        charts = render_charts(connection, run_dir)
        summary = build_summary(connection, inputs, clob, time.time() - started)
        summary["tables"] = exports
        summary["charts"] = charts
        write_json(run_dir / "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json", summary)
        note = [
            "# Full RFQ exploratory stage", "", f"> **{BANNER}**", "",
            "The two approved RFQ releases were scanned in full. Lifecycle endpoints use the",
            "outer-envelope receive clock. The next genuine same-ID create or first explicit",
            "observation boundary, whichever is earlier, right-censors the lifecycle; only",
            "uninterrupted residuals are censored at scan end.",
            "Requester identifiers appear only as run-scoped hashes.", "",
            "Only dim-effective RFQ anchors enter the RFQ→CLOB expansion, which is a deterministic",
            "root-event-balanced diagnostic sample",
            f"(maximum {args.clob_max_per_root} endpoints per date/root/event/role stratum).",
            "A prior control is match-eligible only if its 120-second lookback begins after",
            "the market dimension's conservative effective timestamp.",
            "It is not a population-wide strategy estimate.", "",
            "Broadcast data does not establish quote price, acceptance, fill, winner, or PnL.",
        ]
        write_text_atomic(run_dir / "REPORT/RFQ_FULL_STAGE.md", "\n".join(note) + "\n")
        write_catalog(run_dir)
        state.update({
            "status": "COMPLETE_EXPLORATORY_ONLY", "phase": "COMPLETE",
            "completed_at_utc": utc_now(), "wall_seconds": round(time.time() - started, 3),
            "summary": "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json",
        })
        write_json(state_path, state)
    except Exception as exc:
        state.update({
            "status": "FAILED_RESUMABLE", "failed_at_utc": utc_now(),
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
        })
        write_json(state_path, state)
        raise
    finally:
        connection.close()
    if not args.keep_scratch:
        scratch.unlink(missing_ok=True)
        wal = Path(str(scratch) + ".wal")
        wal.unlink(missing_ok=True)
    print(
        f"RFQ_FULL_STAGE_COMPLETE run_id={run_dir.name} "
        f"wall_seconds={time.time()-started:.3f} {BANNER}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
