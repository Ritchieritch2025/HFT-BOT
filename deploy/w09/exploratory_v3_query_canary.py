#!/usr/bin/env python3
"""MODE 1 exploratory query canary for verified W09 v3 releases.

This entrypoint is intentionally separate from ``v3_query_canary.py``.  The
strict canary continues to require ``SEALED_CONFIRMATION``.  This canary may
consume ``SEALED_DEGRADED_EVIDENCE`` only under the explicitly branded
``MODE 1 / EXPLORATORY_AUTORESEARCH`` contract.  It never reads RFQ and never
emits a strict-acceptance, promotion, profitability, or trading claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import duckdb
import research_reference
import v3_query_canary as strict


MODE = "MODE 1 / EXPLORATORY_AUTORESEARCH"
RECEIPT_SCHEMA = "w09-v3-exploratory-query-canary-v1"
PASS_STATE = "W09_V3_EXPLORATORY_QUERY_CANARY_PASS"
REFUSED_STATE = "W09_V3_EXPLORATORY_QUERY_CANARY_REFUSED"
ACCEPTED_EVIDENCE_TIERS = frozenset({
    "SEALED_CONFIRMATION",
    "SEALED_DEGRADED_EVIDENCE",
})


def build_receipt(cache: Path, release_id: str) -> dict[str, Any]:
    try:
        cache = cache.resolve(strict=True)
    except OSError as exc:
        raise strict.CanaryError(
            "verified cache is missing or inaccessible"
        ) from exc
    if not cache.is_dir():
        raise strict.CanaryError("verified cache is not a directory")

    release = strict._release_dir(cache, release_id)
    manifest, manifest_raw = strict._read_json(
        release / "MANIFEST.json",
        strict.MAX_MANIFEST_BYTES,
        "v3 manifest",
    )
    try:
        descriptor = research_reference.validate_manifest(manifest, release_id)
    except research_reference.ReferenceManifestError as exc:
        raise strict.CanaryError("manifest contract rejected: %s" % exc) from exc
    marker, marker_raw = strict._read_json(
        release / ".VERIFIED.json",
        strict.MAX_CONTROL_BYTES,
        "verification marker",
    )
    provenance, provenance_raw = strict._read_json(
        cache / "view" / ".view_provenance.json",
        strict.MAX_CONTROL_BYTES,
        "view provenance",
    )

    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    evidence_tier = descriptor.get("evidence_tier")
    if descriptor.get("storage_mode") != "CANONICAL_REFERENCE":
        raise strict.CanaryError("manifest is not CANONICAL_REFERENCE")
    if evidence_tier not in ACCEPTED_EVIDENCE_TIERS:
        raise strict.CanaryError(
            "evidence tier is outside the MODE 1 exploratory allowlist"
        )
    if descriptor.get("rfq_included") is not False:
        raise strict.CanaryError("RFQ must be OFF for exploratory autoresearch")

    expected_marker = {
        "schema": "research-reference-verified-v1",
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "release_id": release_id,
        "date": descriptor["date"],
        "manifest_sha256": manifest_sha256,
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
        "evidence_tier": evidence_tier,
        "rfq_included": False,
        "rfq_status": "ABSENT_FROM_RELEASE",
        "canonical_receipt_verified": True,
    }
    for field, expected in expected_marker.items():
        if marker.get(field) != expected:
            raise strict.CanaryError(
                "verification marker binding mismatch: %s" % field
            )
    manifest_version_id = marker.get("manifest_version_id")
    if (
        not isinstance(manifest_version_id, str)
        or not manifest_version_id
        or len(manifest_version_id) > 1024
        or any(ord(character) < 0x20 for character in manifest_version_id)
    ):
        raise strict.CanaryError(
            "verification marker has no manifest VersionId"
        )
    verified_releases = provenance.get("verified_releases")
    if (
        not isinstance(verified_releases, dict)
        or verified_releases.get(descriptor["date"]) != release_id
    ):
        raise strict.CanaryError(
            "exploratory view provenance does not bind date to release id"
        )
    quarantined = provenance.get("quarantined_legacy_overrides")
    if not isinstance(quarantined, dict) or descriptor["date"] in quarantined:
        raise strict.CanaryError(
            "view provenance marks the release as quarantined"
        )

    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET threads=1")
        tables = [
            strict._run_table_query(
                connection, cache, release, descriptor, table
            )
            for table in sorted(descriptor["tables"])
        ]
    finally:
        connection.close()
    if not tables:
        raise strict.CanaryError("validated manifest contains no facts tables")

    sources = [
        {
            "table": row["table"],
            "binding_sha256": row["source"]["binding_sha256"],
            "sha256": row["source"]["sha256"],
            "source_version_id": row["source"]["source_version_id"],
        }
        for row in tables
    ]
    queries = [
        {
            "table": row["table"],
            "describe_query_sha256": row["describe"]["query_sha256"],
            "sample_query_sha256": row["sample"]["query_sha256"],
        }
        for row in tables
    ]
    manifest_binding = {
        "sha256": manifest_sha256,
        "version_id": manifest_version_id,
        "version_binding_sha256": strict._canonical_sha256({
            "release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "manifest_version_id": manifest_version_id,
        }),
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
    }
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "state": PASS_STATE,
        "generated_at_utc": strict._utc_now(),
        "mode": MODE,
        "result_class": "EXPLORATORY_ONLY_NOT_STRICT_ACCEPTANCE",
        "strict_acceptance_claimed": False,
        "release_id": release_id,
        "date": descriptor["date"],
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "evidence_tier": evidence_tier,
        "evidence_policy": {
            "accepted_tiers": sorted(ACCEPTED_EVIDENCE_TIERS),
            "degraded_evidence_requires_mode": MODE,
        },
        "rfq": "OFF",
        "manifest": manifest_binding,
        "verification_marker_sha256": hashlib.sha256(marker_raw).hexdigest(),
        "view_provenance": {
            "sha256": hashlib.sha256(provenance_raw).hexdigest(),
            "date": descriptor["date"],
            "release_id": release_id,
        },
        "source_set_sha256": strict._canonical_sha256(sources),
        "query_set_sha256": strict._canonical_sha256(queries),
        "table_count": len(tables),
        "tables": tables,
        "limitations": [
            "NO_STRICT_ACCEPTANCE_CLAIM",
            "NO_CONFIRMATORY_OR_PROMOTION_CLAIM",
            "NO_PROFITABILITY_OR_ORDER_AUTHORITY",
            "RFQ_EXCLUDED",
        ],
    }
    receipt["receipt_payload_sha256"] = strict._canonical_sha256(receipt)
    return receipt


def _refusal(error: BaseException, release_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "state": REFUSED_STATE,
        "generated_at_utc": strict._utc_now(),
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "release_id": release_id,
        "error": {
            "class": type(error).__name__,
            "message": str(error),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    receipt_path = Path(args.receipt).absolute()
    try:
        receipt = build_receipt(Path(args.cache), args.release)
        file_sha = strict._atomic_json(receipt_path, receipt)
    except (strict.CanaryError, OSError, duckdb.Error) as exc:
        try:
            strict._atomic_json(receipt_path, _refusal(exc, args.release))
        except (strict.CanaryError, OSError):
            pass
        print("%s: %s" % (REFUSED_STATE, exc), file=sys.stderr)
        return 2
    print(
        "%s mode=%r release=%s tier=%s tables=%d receipt=%s file_sha256=%s"
        % (
            PASS_STATE,
            MODE,
            args.release,
            receipt["evidence_tier"],
            receipt["table_count"],
            receipt_path,
            file_sha,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
