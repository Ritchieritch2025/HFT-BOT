#!/usr/bin/env python3
"""Read-only, fail-closed readiness gate for frozen PnL-spine experiments.

The gate deliberately distinguishes two claims:

* ``ENGINEERING_REPLAY_READY`` means that a hash-bound frozen experiment can
  be replayed against the three approved L2 engineering dates.
* ``NET_PNL_READY`` additionally means that effective-dated fee facts, real
  measured PLACE/CANCEL/IOC_EXIT latency, complete path closure, and any
  experiment-specific training artifacts are bound.

Unknown facts never become zero or a default.  This module uses only the
standard library, performs no network calls, and writes only canonical JSON to
stdout when invoked as a CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "pnl-spine-preflight-result-v1"
ENGINEERING_READY = "ENGINEERING_REPLAY_READY"
ENGINEERING_BLOCKED = "ENGINEERING_REPLAY_BLOCKED"
NET_READY = "NET_PNL_READY"
NET_BLOCKED = "NET_PNL_BLOCKED"

FREEZE_SCHEMA = "pnl-spine-experiment-freeze-v1"
FEE_SCHEMA = "pnl-spine-fee-facts-receipt-v1"
LATENCY_SCHEMA = "pnl-spine-measured-latency-receipt-v1"
RELEASE_DQ_SCHEMA = "pnl-spine-exact-release-dq-receipt-v1"
TERMINAL_SCHEMA = "pnl-spine-terminal-coverage-receipt-v1"

ENGINEERING_COHORT_ID = "L2-CLEAN-ENGINEERING-20260712-15-17"
ENGINEERING_L2_DATES = (
    "2026-07-12",
    "2026-07-15",
    "2026-07-17",
)
LATENCY_PATHS = ("PLACE", "CANCEL", "IOC_EXIT")
ZERO_SHA256 = "0" * 64
MAX_INPUT_BYTES = 16 * 1024 * 1024


class ReadOnlyInputError(ValueError):
    """A local JSON input could not be read without weakening the gate."""


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _canonical_stdout(value: Any) -> bytes:
    return _canonical_json_bytes(value) + b"\n"


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value == ZERO_SHA256:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _is_plain_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _is_nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _blocker(
    code: str,
    stage: str,
    input_name: str,
    path: str,
    detail: str,
) -> dict[str, str]:
    return {
        "code": code,
        "stage": stage,
        "input": input_name,
        "path": path,
        "detail": detail,
    }


def _sorted_unique_blockers(
    blockers: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    unique: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for row in blockers:
        key = (
            row["stage"],
            row["input"],
            row["path"],
            row["code"],
            row["detail"],
        )
        unique[key] = dict(row)
    return [
        unique[key]
        for key in sorted(unique)
    ]


def _schema_and_keys(
    document: Mapping[str, Any],
    *,
    input_name: str,
    expected_schema: str,
    expected_keys: set[str],
    stage: str,
    prefix: str,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if document.get("schema_version") != expected_schema:
        blockers.append(
            _blocker(
                f"{prefix}_SCHEMA_INVALID",
                stage,
                input_name,
                "/schema_version",
                f"schema_version must equal {expected_schema}",
            )
        )
    unknown = sorted(set(document) - expected_keys)
    if unknown:
        blockers.append(
            _blocker(
                f"{prefix}_UNKNOWN_FIELDS",
                stage,
                input_name,
                "/",
                "schema drift is not allowed: " + ",".join(unknown),
            )
        )
    return blockers


def _payload_integrity(
    document: Mapping[str, Any],
    *,
    input_name: str,
    stage: str,
    prefix: str,
) -> list[dict[str, str]]:
    supplied = document.get("payload_sha256")
    if not _is_sha256(supplied):
        return [
            _blocker(
                f"{prefix}_PAYLOAD_SHA_MISSING",
                stage,
                input_name,
                "/payload_sha256",
                "a non-placeholder lowercase SHA-256 is required",
            )
        ]
    payload = dict(document)
    payload.pop("payload_sha256", None)
    try:
        calculated = _sha256_json(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        return [
            _blocker(
                f"{prefix}_PAYLOAD_NOT_CANONICAL",
                stage,
                input_name,
                "/",
                "receipt payload is not canonical finite JSON",
            )
        ]
    if supplied != calculated:
        return [
            _blocker(
                f"{prefix}_PAYLOAD_SHA_MISMATCH",
                stage,
                input_name,
                "/payload_sha256",
                "payload_sha256 does not bind the canonical receipt payload",
            )
        ]
    return []


def _exact_date_set(
    value: object,
    *,
    input_name: str,
    path: str,
    stage: str,
    prefix: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not all(isinstance(row, str) for row in value):
        return [
            _blocker(
                f"{prefix}_DATE_SET_MISSING",
                stage,
                input_name,
                path,
                "dates must be the explicit three-date engineering cohort",
            )
        ]
    blockers: list[dict[str, str]] = []
    expected = list(ENGINEERING_L2_DATES)
    disallowed = sorted(set(value) - set(expected))
    if disallowed:
        blockers.append(
            _blocker(
                f"{prefix}_DATE_NOT_ALLOWED",
                stage,
                input_name,
                path,
                "current engineering L2 permits only 2026-07-12, 2026-07-15, "
                "and 2026-07-17; disallowed=" + ",".join(disallowed),
            )
        )
    if value != expected:
        blockers.append(
            _blocker(
                f"{prefix}_DATE_SET_MISMATCH",
                stage,
                input_name,
                path,
                "dates must be unique, sorted, and exactly match the engineering cohort",
            )
        )
    return blockers


def _validate_freeze(
    document: object,
    experiment_id: str,
) -> tuple[list[dict[str, str]], Mapping[str, Any] | None, dict[str, Any]]:
    stage = "FREEZE"
    input_name = "frozen_experiment"
    blockers: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return (
            [
                _blocker(
                    "FROZEN_EXPERIMENT_MISSING",
                    stage,
                    input_name,
                    "/",
                    "a frozen experiment JSON object is required",
                )
            ],
            None,
            facts,
        )

    if document.get("schema_version") != FREEZE_SCHEMA:
        blockers.append(
            _blocker(
                "FREEZE_SCHEMA_INVALID",
                stage,
                input_name,
                "/schema_version",
                f"schema_version must equal {FREEZE_SCHEMA}",
            )
        )
    if document.get("freeze_id") != "PNL-SPINE-EXPERIMENTS-V1":
        blockers.append(
            _blocker(
                "FREEZE_ID_INVALID",
                stage,
                input_name,
                "/freeze_id",
                "freeze_id must equal PNL-SPINE-EXPERIMENTS-V1",
            )
        )

    supplied_freeze_sha = document.get("freeze_sha256")
    if not _is_sha256(supplied_freeze_sha):
        blockers.append(
            _blocker(
                "FREEZE_SHA_MISSING",
                stage,
                input_name,
                "/freeze_sha256",
                "freeze_sha256 is missing or malformed",
            )
        )
    else:
        freeze_payload = dict(document)
        freeze_payload.pop("freeze_sha256", None)
        try:
            calculated = _sha256_json(freeze_payload)
        except (TypeError, ValueError, UnicodeEncodeError):
            calculated = None
        if calculated != supplied_freeze_sha:
            blockers.append(
                _blocker(
                    "FREEZE_SHA_MISMATCH",
                    stage,
                    input_name,
                    "/freeze_sha256",
                    "freeze_sha256 does not bind the canonical frozen package",
                )
            )
        else:
            facts["freeze_sha256"] = supplied_freeze_sha

    claim = document.get("claim_contract")
    if not isinstance(claim, Mapping):
        blockers.append(
            _blocker(
                "CLAIM_CONTRACT_MISSING",
                stage,
                input_name,
                "/claim_contract",
                "the immutable claim contract is required",
            )
        )
    else:
        required_claims = {
            "claim_tier": "ENGINEERING_ONLY",
            "execution_authority": "NONE",
            "live_order_authority": "NONE",
            "promotion_allowed": False,
            "pnl_basis": "PATHWISE_AFTER_COST",
        }
        for field, expected in required_claims.items():
            if claim.get(field) != expected:
                blockers.append(
                    _blocker(
                        "CLAIM_CONTRACT_UNSAFE",
                        stage,
                        input_name,
                        f"/claim_contract/{field}",
                        f"{field} must remain {expected!r}",
                    )
                )
        facts["claim_tier"] = claim.get("claim_tier")
        facts["promotion_allowed"] = claim.get("promotion_allowed")

    cohort = document.get("cohorts")
    engineering = (
        cohort.get("engineering_acceptance")
        if isinstance(cohort, Mapping)
        else None
    )
    if not isinstance(engineering, Mapping):
        blockers.append(
            _blocker(
                "ENGINEERING_COHORT_MISSING",
                stage,
                input_name,
                "/cohorts/engineering_acceptance",
                "the frozen engineering cohort is required",
            )
        )
    else:
        if engineering.get("cohort_id") != ENGINEERING_COHORT_ID:
            blockers.append(
                _blocker(
                    "ENGINEERING_COHORT_ID_INVALID",
                    stage,
                    input_name,
                    "/cohorts/engineering_acceptance/cohort_id",
                    f"cohort_id must equal {ENGINEERING_COHORT_ID}",
                )
            )
        blockers.extend(
            _exact_date_set(
                engineering.get("dates_utc"),
                input_name=input_name,
                path="/cohorts/engineering_acceptance/dates_utc",
                stage=stage,
                prefix="ENGINEERING_COHORT",
            )
        )
        if (
            engineering.get("claim_tier") != "ENGINEERING_ONLY"
            or engineering.get("inference_allowed") is not False
            or engineering.get("promotion_allowed") is not False
        ):
            blockers.append(
                _blocker(
                    "ENGINEERING_COHORT_SCOPE_UNSAFE",
                    stage,
                    input_name,
                    "/cohorts/engineering_acceptance",
                    "engineering dates cannot support inference or promotion",
                )
            )

    execution = document.get("execution_contract")
    if not isinstance(execution, Mapping):
        blockers.append(
            _blocker(
                "EXECUTION_CONTRACT_MISSING",
                stage,
                input_name,
                "/execution_contract",
                "strict execution semantics are required",
            )
        )
    else:
        expected_execution = {
            "stage": "OFFLINE_ENGINEERING_REPLAY",
            "fill_authority": (
                "STRICT_THROUGH_PUBLIC_EVIDENCE_OR_SEPARATELY_AUTHORIZED_ACTUAL_FILL"
            ),
            "at_price_fill": False,
            "same_timestamp_ordering": "ADVERSE_EVENT_FIRST",
            "public_volume_allocation": (
                "ALLOCATE_EACH_PUBLIC_PRINT_ONCE_GLOBALLY_ACROSS_ALL_ORDERS"
            ),
            "cancel_before_replace": True,
            "forced_exit_tif": "IOC",
            "forced_exit_post_only": False,
            "forced_exit_reduce_only": True,
            "exit_price": "EFFECTIVE_TIME_EXACT_L2_WALK",
            "midpoint_exit_allowed": False,
        }
        for field, expected in expected_execution.items():
            if execution.get(field) != expected:
                blockers.append(
                    _blocker(
                        "EXECUTION_CONTRACT_UNSAFE",
                        stage,
                        input_name,
                        f"/execution_contract/{field}",
                        f"{field} must remain {expected!r}",
                    )
                )

    revisions = document.get("revisions")
    matches: list[Mapping[str, Any]] = []
    if isinstance(revisions, list):
        matches = [
            revision
            for revision in revisions
            if isinstance(revision, Mapping)
            and isinstance(revision.get("source_card"), Mapping)
            and revision["source_card"].get("experiment_id") == experiment_id
        ]
    if len(matches) != 1:
        blockers.append(
            _blocker(
                "EXPERIMENT_REVISION_NOT_UNIQUE",
                stage,
                input_name,
                "/revisions",
                "exactly one revision must match the requested experiment_id",
            )
        )
        return blockers, None, facts

    revision = matches[0]
    supplied_revision_sha = revision.get("revision_definition_sha256")
    if not _is_sha256(supplied_revision_sha):
        blockers.append(
            _blocker(
                "REVISION_SHA_MISSING",
                stage,
                input_name,
                "/revisions/revision_definition_sha256",
                "revision_definition_sha256 is missing or malformed",
            )
        )
    else:
        revision_payload = dict(revision)
        revision_payload.pop("revision_definition_sha256", None)
        try:
            calculated = _sha256_json(revision_payload)
        except (TypeError, ValueError, UnicodeEncodeError):
            calculated = None
        if calculated != supplied_revision_sha:
            blockers.append(
                _blocker(
                    "REVISION_SHA_MISMATCH",
                    stage,
                    input_name,
                    "/revisions/revision_definition_sha256",
                    "revision_definition_sha256 does not bind the selected revision",
                )
            )
        else:
            facts["revision_definition_sha256"] = supplied_revision_sha

    source_card = revision.get("source_card")
    if not isinstance(source_card, Mapping):
        blockers.append(
            _blocker(
                "SOURCE_CARD_BINDING_MISSING",
                stage,
                input_name,
                "/revisions/source_card",
                "the source registry card binding is required",
            )
        )
    else:
        for field in ("registry_sha256", "card_definition_sha256"):
            if not _is_sha256(source_card.get(field)):
                blockers.append(
                    _blocker(
                        "SOURCE_CARD_SHA_MISSING",
                        stage,
                        input_name,
                        f"/revisions/source_card/{field}",
                        f"{field} must be a non-placeholder SHA-256",
                    )
                )

    if experiment_id in {
        "A01-SPREAD-CAPTURE",
        "A11-ONE-SIDED-PROVISION",
    }:
        parameters = revision.get("parameter_freeze")
        if not isinstance(parameters, Mapping):
            blockers.append(
                _blocker(
                    "PARAMETER_FREEZE_MISSING",
                    stage,
                    input_name,
                    "/revisions/parameter_freeze",
                    "a parameter freeze is required",
                )
            )
        else:
            for name, row in sorted(parameters.items()):
                if isinstance(row, Mapping) and "frozen" in row and row["frozen"] is None:
                    blockers.append(
                        _blocker(
                            "PARAMETER_NOT_FROZEN",
                            stage,
                            input_name,
                            f"/revisions/parameter_freeze/{name}/frozen",
                            "engineering replay cannot choose a parameter at runtime",
                        )
                    )

    facts["experiment_id"] = experiment_id
    facts["revision_id"] = revision.get("revision_id")
    return blockers, revision, facts


def _validate_release_dq(
    document: object,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    input_name = "release_dq"
    blockers: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return (
            [
                _blocker(
                    "EXACT_RELEASE_DQ_RECEIPT_MISSING",
                    "DATA",
                    input_name,
                    "/",
                    "an exact-version release and L2 DQ receipt is required",
                ),
                _blocker(
                    "ROOT_START_BINDINGS_MISSING",
                    "ROOT_START",
                    input_name,
                    "/root_start_bindings",
                    "root map and scheduled-start bindings are required",
                ),
            ],
            facts,
        )

    expected_keys = {
        "schema_version",
        "receipt_id",
        "cohort_id",
        "dates",
        "root_start_bindings",
        "root_map_sha256",
        "scheduled_start_sha256",
        "source_sha256",
        "payload_sha256",
    }
    blockers.extend(
        _schema_and_keys(
            document,
            input_name=input_name,
            expected_schema=RELEASE_DQ_SCHEMA,
            expected_keys=expected_keys,
            stage="DATA",
            prefix="RELEASE_DQ",
        )
    )
    blockers.extend(
        _payload_integrity(
            document,
            input_name=input_name,
            stage="DATA",
            prefix="RELEASE_DQ",
        )
    )
    if not _is_nonempty(document.get("receipt_id")):
        blockers.append(
            _blocker(
                "RELEASE_DQ_RECEIPT_ID_MISSING",
                "DATA",
                input_name,
                "/receipt_id",
                "receipt_id is required",
            )
        )
    if document.get("cohort_id") != ENGINEERING_COHORT_ID:
        blockers.append(
            _blocker(
                "DATA_COHORT_ID_INVALID",
                "DATA",
                input_name,
                "/cohort_id",
                f"cohort_id must equal {ENGINEERING_COHORT_ID}",
            )
        )

    rows = document.get("dates")
    row_dates: list[str] = []
    release_ids: list[str] = []
    if not isinstance(rows, list):
        blockers.append(
            _blocker(
                "DATA_DATE_SET_MISSING",
                "DATA",
                input_name,
                "/dates",
                "three exact release rows are required",
            )
        )
    else:
        for index, row in enumerate(rows):
            path = f"/dates/{index}"
            if not isinstance(row, Mapping):
                blockers.append(
                    _blocker(
                        "DATA_RELEASE_ROW_INVALID",
                        "DATA",
                        input_name,
                        path,
                        "each release row must be an object",
                    )
                )
                continue
            expected_row_keys = {
                "date",
                "release_id",
                "manifest_version_id",
                "manifest_sha256",
                "evidence_tier",
                "l2_quality_state",
                "l2_quality_receipt_sha256",
                "l2_row_count",
                "l2_objects_sha256",
            }
            unknown = sorted(set(row) - expected_row_keys)
            if unknown:
                blockers.append(
                    _blocker(
                        "DATA_RELEASE_ROW_UNKNOWN_FIELDS",
                        "DATA",
                        input_name,
                        path,
                        "schema drift is not allowed: " + ",".join(unknown),
                    )
                )
            date = row.get("date")
            if isinstance(date, str):
                row_dates.append(date)
            else:
                blockers.append(
                    _blocker(
                        "DATA_RELEASE_DATE_MISSING",
                        "DATA",
                        input_name,
                        f"{path}/date",
                        "an explicit UTC date is required",
                    )
                )
            release_id = row.get("release_id")
            if (
                not _is_nonempty(release_id)
                or not isinstance(date, str)
                or not release_id.startswith(f"{date}__v3ref__")
            ):
                blockers.append(
                    _blocker(
                        "DATA_RELEASE_NOT_EXACT",
                        "DATA",
                        input_name,
                        f"{path}/release_id",
                        "release_id must be the date-bound __v3ref__ exact release",
                    )
                )
            else:
                release_ids.append(release_id)
            version_id = row.get("manifest_version_id")
            if (
                not _is_nonempty(version_id)
                or str(version_id).lower() in {"latest", "null", "none"}
            ):
                blockers.append(
                    _blocker(
                        "DATA_MANIFEST_VERSION_MISSING",
                        "DATA",
                        input_name,
                        f"{path}/manifest_version_id",
                        "an exact immutable manifest VersionId is required",
                    )
                )
            for field in (
                "manifest_sha256",
                "l2_quality_receipt_sha256",
                "l2_objects_sha256",
            ):
                if not _is_sha256(row.get(field)):
                    blockers.append(
                        _blocker(
                            "DATA_SOURCE_SHA_MISSING",
                            "DATA",
                            input_name,
                            f"{path}/{field}",
                            f"{field} must be a non-placeholder SHA-256",
                        )
                    )
            if row.get("evidence_tier") not in {
                "SEALED_DEGRADED_EVIDENCE",
                "SEALED_CONFIRMATION",
            }:
                blockers.append(
                    _blocker(
                        "DATA_EVIDENCE_TIER_INVALID",
                        "DATA",
                        input_name,
                        f"{path}/evidence_tier",
                        "only a sealed evidence tier is allowed",
                    )
                )
            if row.get("l2_quality_state") != "PASS":
                blockers.append(
                    _blocker(
                        "DATA_L2_DQ_NOT_PASS",
                        "DATA",
                        input_name,
                        f"{path}/l2_quality_state",
                        "every engineering L2 date must have an explicit PASS",
                    )
                )
            if not _is_plain_int(row.get("l2_row_count"), minimum=1):
                blockers.append(
                    _blocker(
                        "DATA_L2_ROWS_MISSING",
                        "DATA",
                        input_name,
                        f"{path}/l2_row_count",
                        "l2_row_count must be a positive measured integer",
                    )
                )
        blockers.extend(
            _exact_date_set(
                row_dates,
                input_name=input_name,
                path="/dates",
                stage="DATA",
                prefix="DATA",
            )
        )
        if len(release_ids) != len(set(release_ids)):
            blockers.append(
                _blocker(
                    "DATA_DUPLICATE_RELEASE_ID",
                    "DATA",
                    input_name,
                    "/dates",
                    "release_id values must be unique",
                )
            )

    bindings = document.get("root_start_bindings")
    required_bindings = (
        "root_map_version",
        "scheduled_start_source",
        "scheduled_start_asof_semantics",
        "tick_table_version",
        "lifecycle_version",
    )
    if not isinstance(bindings, Mapping):
        blockers.append(
            _blocker(
                "ROOT_START_BINDINGS_MISSING",
                "ROOT_START",
                input_name,
                "/root_start_bindings",
                "root map and scheduled-start bindings are required",
            )
        )
    else:
        unknown = sorted(set(bindings) - set(required_bindings))
        if unknown:
            blockers.append(
                _blocker(
                    "ROOT_START_UNKNOWN_FIELDS",
                    "ROOT_START",
                    input_name,
                    "/root_start_bindings",
                    "schema drift is not allowed: " + ",".join(unknown),
                )
            )
        code_by_field = {
            "root_map_version": "ROOT_MAP_MISSING",
            "scheduled_start_source": "SCHEDULED_START_SOURCE_MISSING",
            "scheduled_start_asof_semantics": "SCHEDULED_START_ASOF_MISSING",
            "tick_table_version": "TICK_TABLE_MISSING",
            "lifecycle_version": "LIFECYCLE_VERSION_MISSING",
        }
        for field in required_bindings:
            if not _is_nonempty(bindings.get(field)):
                blockers.append(
                    _blocker(
                        code_by_field[field],
                        "ROOT_START",
                        input_name,
                        f"/root_start_bindings/{field}",
                        f"{field} is required; no inferred value is allowed",
                    )
                )
    for field, code in (
        ("root_map_sha256", "ROOT_MAP_SHA_MISSING"),
        ("scheduled_start_sha256", "SCHEDULED_START_SHA_MISSING"),
    ):
        if not _is_sha256(document.get(field)):
            blockers.append(
                _blocker(
                    code,
                    "ROOT_START",
                    input_name,
                    f"/{field}",
                    f"{field} must be a non-placeholder SHA-256",
                )
            )
    if not _is_sha256(document.get("source_sha256")):
        blockers.append(
            _blocker(
                "RELEASE_DQ_SOURCE_SHA_MISSING",
                "DATA",
                input_name,
                "/source_sha256",
                "the source receipt chain must be hash-bound",
            )
        )
    facts["release_ids"] = release_ids
    facts["engineering_l2_dates"] = row_dates
    if isinstance(bindings, Mapping):
        facts["root_start_bindings"] = {
            field: bindings.get(field)
            for field in required_bindings
        }
    return blockers, facts


def _validate_fee(
    document: object,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stage = "FEE"
    input_name = "fee_facts"
    blockers: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return (
            [
                _blocker(
                    "FEE_FACTS_RECEIPT_MISSING",
                    stage,
                    input_name,
                    "/",
                    "effective-dated fee facts are required; fees cannot default to zero",
                )
            ],
            facts,
        )
    expected_keys = {
        "schema_version",
        "receipt_id",
        "status",
        "coverage_dates",
        "coverage_scope",
        "bindings",
        "source_sha256",
        "event_override_history_complete",
        "order_rounding_rebate_complete",
        "private_fee_precedence",
        "payload_sha256",
    }
    blockers.extend(
        _schema_and_keys(
            document,
            input_name=input_name,
            expected_schema=FEE_SCHEMA,
            expected_keys=expected_keys,
            stage=stage,
            prefix="FEE",
        )
    )
    blockers.extend(
        _payload_integrity(
            document,
            input_name=input_name,
            stage=stage,
            prefix="FEE",
        )
    )
    if not _is_nonempty(document.get("receipt_id")):
        blockers.append(
            _blocker(
                "FEE_RECEIPT_ID_MISSING",
                stage,
                input_name,
                "/receipt_id",
                "receipt_id is required",
            )
        )
    if document.get("status") != "VERIFIED_EFFECTIVE_DATED":
        blockers.append(
            _blocker(
                "FEE_FACTS_NOT_VERIFIED",
                stage,
                input_name,
                "/status",
                "fee receipt status must be VERIFIED_EFFECTIVE_DATED",
            )
        )
    blockers.extend(
        _exact_date_set(
            document.get("coverage_dates"),
            input_name=input_name,
            path="/coverage_dates",
            stage=stage,
            prefix="FEE",
        )
    )
    if document.get("coverage_scope") != "ALL_ELIGIBLE_SERIES_EVENTS_AND_ROLES":
        blockers.append(
            _blocker(
                "FEE_SCOPE_INCOMPLETE",
                stage,
                input_name,
                "/coverage_scope",
                "fees must cover every eligible series, event override, and liquidity role",
            )
        )
    bindings = document.get("bindings")
    required = {
        "fee_facts_sha256": ("FEE_FACTS_SHA_MISSING", "sha"),
        "maker_fee_formula_id": ("FEE_MAKER_FORMULA_MISSING", "text"),
        "taker_fee_formula_id": ("FEE_TAKER_FORMULA_MISSING", "text"),
        "account_class": ("FEE_ACCOUNT_CLASS_MISSING", "text"),
        "target_balance_precision": ("FEE_BALANCE_PRECISION_MISSING", "int"),
        "fee_rounding_accumulator_version": ("FEE_ROUNDING_MISSING", "text"),
        "rebate_and_event_override_version": ("FEE_OVERRIDE_VERSION_MISSING", "text"),
    }
    if not isinstance(bindings, Mapping):
        blockers.append(
            _blocker(
                "FEE_BINDINGS_MISSING",
                stage,
                input_name,
                "/bindings",
                "all fee bindings are required",
            )
        )
    else:
        unknown = sorted(set(bindings) - set(required))
        if unknown:
            blockers.append(
                _blocker(
                    "FEE_BINDINGS_UNKNOWN_FIELDS",
                    stage,
                    input_name,
                    "/bindings",
                    "schema drift is not allowed: " + ",".join(unknown),
                )
            )
        for field, (code, kind) in required.items():
            value = bindings.get(field)
            valid = (
                _is_sha256(value)
                if kind == "sha"
                else _is_plain_int(value, minimum=1)
                if kind == "int"
                else _is_nonempty(value)
            )
            if not valid:
                blockers.append(
                    _blocker(
                        code,
                        stage,
                        input_name,
                        f"/bindings/{field}",
                        f"{field} is required; no default or zero fee is allowed",
                    )
                )
    if not _is_sha256(document.get("source_sha256")):
        blockers.append(
            _blocker(
                "FEE_SOURCE_SHA_MISSING",
                stage,
                input_name,
                "/source_sha256",
                "fee source facts must be hash-bound",
            )
        )
    for field, code in (
        ("event_override_history_complete", "FEE_EVENT_OVERRIDE_COVERAGE_INCOMPLETE"),
        ("order_rounding_rebate_complete", "FEE_ROUNDING_REBATE_INCOMPLETE"),
        ("private_fee_precedence", "FEE_PRIVATE_PRECEDENCE_UNBOUND"),
    ):
        if document.get(field) is not True:
            blockers.append(
                _blocker(
                    code,
                    stage,
                    input_name,
                    f"/{field}",
                    f"{field} must be explicitly true",
                )
            )
    if isinstance(bindings, Mapping):
        facts["fee_facts_sha256"] = bindings.get("fee_facts_sha256")
        facts["account_class"] = bindings.get("account_class")
    return blockers, facts


def _validate_latency(
    document: object,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stage = "LATENCY"
    input_name = "measured_latency"
    blockers: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return (
            [
                _blocker(
                    "MEASURED_LATENCY_RECEIPT_MISSING",
                    stage,
                    input_name,
                    "/",
                    "real PLACE, CANCEL, and IOC_EXIT measurements are required",
                )
            ],
            facts,
        )
    expected_keys = {
        "schema_version",
        "receipt_id",
        "measurement_mode",
        "clock_id",
        "measured_on",
        "created_at_ns",
        "environment_fingerprint_sha256",
        "source_sha256",
        "samples",
        "payload_sha256",
    }
    blockers.extend(
        _schema_and_keys(
            document,
            input_name=input_name,
            expected_schema=LATENCY_SCHEMA,
            expected_keys=expected_keys,
            stage=stage,
            prefix="LATENCY",
        )
    )
    blockers.extend(
        _payload_integrity(
            document,
            input_name=input_name,
            stage=stage,
            prefix="LATENCY",
        )
    )
    if not _is_nonempty(document.get("receipt_id")):
        blockers.append(
            _blocker(
                "LATENCY_RECEIPT_ID_MISSING",
                stage,
                input_name,
                "/receipt_id",
                "receipt_id is required",
            )
        )
    if document.get("measurement_mode") != "REAL_ORDER_MEASURED":
        blockers.append(
            _blocker(
                "LATENCY_MEASUREMENT_NOT_REAL",
                stage,
                input_name,
                "/measurement_mode",
                "measurement_mode must be REAL_ORDER_MEASURED",
            )
        )
    for field in ("clock_id", "measured_on"):
        if not _is_nonempty(document.get(field)):
            blockers.append(
                _blocker(
                    "LATENCY_CLOCK_CONTEXT_MISSING",
                    stage,
                    input_name,
                    f"/{field}",
                    f"{field} is required",
                )
            )
    for field in ("environment_fingerprint_sha256", "source_sha256"):
        if not _is_sha256(document.get(field)):
            blockers.append(
                _blocker(
                    "LATENCY_SOURCE_SHA_MISSING",
                    stage,
                    input_name,
                    f"/{field}",
                    f"{field} must be a non-placeholder SHA-256",
                )
            )
    created_at = document.get("created_at_ns")
    if not _is_plain_int(created_at):
        blockers.append(
            _blocker(
                "LATENCY_CREATED_AT_INVALID",
                stage,
                input_name,
                "/created_at_ns",
                "created_at_ns must be a causal integer timestamp",
            )
        )

    samples = document.get("samples")
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    max_effective: int | None = None
    sample_counts = {path: 0 for path in LATENCY_PATHS}
    if not isinstance(samples, list):
        blockers.append(
            _blocker(
                "LATENCY_SAMPLES_MISSING",
                stage,
                input_name,
                "/samples",
                "measured samples are required",
            )
        )
    else:
        for index, sample in enumerate(samples):
            path = f"/samples/{index}"
            if not isinstance(sample, Mapping):
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_INVALID",
                        stage,
                        input_name,
                        path,
                        "each latency sample must be an object",
                    )
                )
                continue
            expected_sample_keys = {
                "sample_id",
                "order_id",
                "path",
                "decision_ns",
                "sent_ns",
                "acknowledged_ns",
                "effective_ns",
                "source_event_sha256",
            }
            unknown = sorted(set(sample) - expected_sample_keys)
            if unknown:
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_UNKNOWN_FIELDS",
                        stage,
                        input_name,
                        path,
                        "schema drift is not allowed: " + ",".join(unknown),
                    )
                )
            sample_id = sample.get("sample_id")
            if not _is_nonempty(sample_id):
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_ID_MISSING",
                        stage,
                        input_name,
                        f"{path}/sample_id",
                        "sample_id is required",
                    )
                )
            elif sample_id in seen_ids:
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_ID_DUPLICATE",
                        stage,
                        input_name,
                        f"{path}/sample_id",
                        "sample_id must be unique",
                    )
                )
            else:
                seen_ids.add(sample_id)
            if not _is_nonempty(sample.get("order_id")):
                blockers.append(
                    _blocker(
                        "LATENCY_ORDER_ID_MISSING",
                        stage,
                        input_name,
                        f"{path}/order_id",
                        "the real measured order identity is required",
                    )
                )
            latency_path = sample.get("path")
            if latency_path not in LATENCY_PATHS:
                blockers.append(
                    _blocker(
                        "LATENCY_PATH_INVALID",
                        stage,
                        input_name,
                        f"{path}/path",
                        "path must be PLACE, CANCEL, or IOC_EXIT",
                    )
                )
            else:
                seen_paths.add(latency_path)
                sample_counts[latency_path] += 1
            timestamps = [
                sample.get("decision_ns"),
                sample.get("sent_ns"),
                sample.get("acknowledged_ns"),
                sample.get("effective_ns"),
            ]
            if not all(_is_plain_int(value) for value in timestamps):
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_TIMESTAMP_MISSING",
                        stage,
                        input_name,
                        path,
                        "decision, sent, acknowledged, and effective ns are required",
                    )
                )
            elif not (
                timestamps[0]
                <= timestamps[1]
                <= timestamps[2]
                <= timestamps[3]
                and timestamps[3] > timestamps[0]
            ):
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_NON_CAUSAL",
                        stage,
                        input_name,
                        path,
                        "timestamps must satisfy decision <= sent <= ack <= effective "
                        "with positive total latency",
                    )
                )
            else:
                effective = timestamps[3]
                max_effective = (
                    effective
                    if max_effective is None
                    else max(max_effective, effective)
                )
            if not _is_sha256(sample.get("source_event_sha256")):
                blockers.append(
                    _blocker(
                        "LATENCY_SAMPLE_SOURCE_SHA_MISSING",
                        stage,
                        input_name,
                        f"{path}/source_event_sha256",
                        "every measured sample must bind its causal source events",
                    )
                )
        for latency_path in LATENCY_PATHS:
            if latency_path not in seen_paths:
                blockers.append(
                    _blocker(
                        f"LATENCY_{latency_path}_SAMPLES_MISSING",
                        stage,
                        input_name,
                        "/samples",
                        f"at least one real {latency_path} sample is required",
                    )
                )
    if (
        _is_plain_int(created_at)
        and max_effective is not None
        and created_at < max_effective
    ):
        blockers.append(
            _blocker(
                "LATENCY_RECEIPT_PRECEDES_SAMPLE",
                stage,
                input_name,
                "/created_at_ns",
                "receipt creation cannot precede a sample effective timestamp",
            )
        )
    facts["latency_paths"] = sorted(seen_paths)
    facts["latency_sample_counts"] = sample_counts
    return blockers, facts


def _validate_terminal(
    document: object,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stage = "CLOSURE"
    input_name = "terminal_coverage"
    blockers: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return (
            [
                _blocker(
                    "TERMINAL_COVERAGE_RECEIPT_MISSING",
                    stage,
                    input_name,
                    "/",
                    "complete exit or final settlement coverage is required",
                )
            ],
            facts,
        )
    expected_keys = {
        "schema_version",
        "receipt_id",
        "status",
        "coverage_dates",
        "closure_scope",
        "bindings",
        "path_count",
        "closed_path_count",
        "unresolved_path_count",
        "residual_quantity_e4",
        "all_paths_have_exit_or_final_settlement",
        "append_only_lifecycle_complete",
        "source_sha256",
        "payload_sha256",
    }
    blockers.extend(
        _schema_and_keys(
            document,
            input_name=input_name,
            expected_schema=TERMINAL_SCHEMA,
            expected_keys=expected_keys,
            stage=stage,
            prefix="TERMINAL",
        )
    )
    blockers.extend(
        _payload_integrity(
            document,
            input_name=input_name,
            stage=stage,
            prefix="TERMINAL",
        )
    )
    if not _is_nonempty(document.get("receipt_id")):
        blockers.append(
            _blocker(
                "TERMINAL_RECEIPT_ID_MISSING",
                stage,
                input_name,
                "/receipt_id",
                "receipt_id is required",
            )
        )
    if document.get("status") != "COMPLETE":
        blockers.append(
            _blocker(
                "TERMINAL_COVERAGE_NOT_COMPLETE",
                stage,
                input_name,
                "/status",
                "terminal coverage status must be COMPLETE",
            )
        )
    blockers.extend(
        _exact_date_set(
            document.get("coverage_dates"),
            input_name=input_name,
            path="/coverage_dates",
            stage=stage,
            prefix="TERMINAL",
        )
    )
    if document.get("closure_scope") != "EVERY_STRATEGY_AND_BASELINE_PATH":
        blockers.append(
            _blocker(
                "TERMINAL_SCOPE_INCOMPLETE",
                stage,
                input_name,
                "/closure_scope",
                "closure must cover every strategy and registered baseline path",
            )
        )

    required = {
        "terminal_contract_version": ("TERMINAL_CONTRACT_VERSION_MISSING", "text"),
        "legal_exception_state_table_sha256": (
            "TERMINAL_EXCEPTION_TABLE_SHA_MISSING",
            "sha",
        ),
        "ioc_round_cap": ("TERMINAL_IOC_ROUND_CAP_MISSING", "int"),
        "ioc_time_cap_ms": ("TERMINAL_IOC_TIME_CAP_MISSING", "int"),
        "residual_policy_version": ("TERMINAL_RESIDUAL_POLICY_MISSING", "text"),
        "realized_outcome_requirement": (
            "TERMINAL_REALIZED_OUTCOME_RULE_MISSING",
            "text",
        ),
    }
    bindings = document.get("bindings")
    if not isinstance(bindings, Mapping):
        blockers.append(
            _blocker(
                "TERMINAL_BINDINGS_MISSING",
                stage,
                input_name,
                "/bindings",
                "terminal contract and legal exception bindings are required",
            )
        )
    else:
        unknown = sorted(set(bindings) - set(required))
        if unknown:
            blockers.append(
                _blocker(
                    "TERMINAL_BINDINGS_UNKNOWN_FIELDS",
                    stage,
                    input_name,
                    "/bindings",
                    "schema drift is not allowed: " + ",".join(unknown),
                )
            )
        for field, (code, kind) in required.items():
            value = bindings.get(field)
            valid = (
                _is_sha256(value)
                if kind == "sha"
                else _is_plain_int(value, minimum=1)
                if kind == "int"
                else _is_nonempty(value)
            )
            if not valid:
                blockers.append(
                    _blocker(
                        code,
                        stage,
                        input_name,
                        f"/bindings/{field}",
                        f"{field} must be explicitly bound",
                    )
                )

    path_count = document.get("path_count")
    closed_count = document.get("closed_path_count")
    unresolved = document.get("unresolved_path_count")
    residual = document.get("residual_quantity_e4")
    if not _is_plain_int(path_count, minimum=1):
        blockers.append(
            _blocker(
                "TERMINAL_PATH_COUNT_MISSING",
                stage,
                input_name,
                "/path_count",
                "path_count must be a positive measured integer",
            )
        )
    if not _is_plain_int(closed_count):
        blockers.append(
            _blocker(
                "TERMINAL_CLOSED_PATH_COUNT_MISSING",
                stage,
                input_name,
                "/closed_path_count",
                "closed_path_count must be an explicit measured integer",
            )
        )
    if (
        _is_plain_int(path_count, minimum=1)
        and _is_plain_int(closed_count)
        and closed_count != path_count
    ):
        blockers.append(
            _blocker(
                "TERMINAL_PATHS_NOT_ALL_CLOSED",
                stage,
                input_name,
                "/closed_path_count",
                "closed_path_count must equal path_count",
            )
        )
    if unresolved != 0 or type(unresolved) is not int:
        blockers.append(
            _blocker(
                "TERMINAL_UNRESOLVED_PATHS",
                stage,
                input_name,
                "/unresolved_path_count",
                "unresolved_path_count must be exactly zero",
            )
        )
    if residual != 0 or type(residual) is not int:
        blockers.append(
            _blocker(
                "TERMINAL_RESIDUAL_QUANTITY",
                stage,
                input_name,
                "/residual_quantity_e4",
                "residual_quantity_e4 must be exactly zero after exit or settlement",
            )
        )
    if document.get("all_paths_have_exit_or_final_settlement") is not True:
        blockers.append(
            _blocker(
                "TERMINAL_EXIT_OR_SETTLEMENT_MISSING",
                stage,
                input_name,
                "/all_paths_have_exit_or_final_settlement",
                "every path needs a strict exit or finalized exact settlement",
            )
        )
    if document.get("append_only_lifecycle_complete") is not True:
        blockers.append(
            _blocker(
                "TERMINAL_LIFECYCLE_INCOMPLETE",
                stage,
                input_name,
                "/append_only_lifecycle_complete",
                "the append-only lifecycle must be complete and reconciled",
            )
        )
    if not _is_sha256(document.get("source_sha256")):
        blockers.append(
            _blocker(
                "TERMINAL_SOURCE_SHA_MISSING",
                stage,
                input_name,
                "/source_sha256",
                "terminal evidence must be hash-bound",
            )
        )
    facts["path_count"] = path_count
    facts["closed_path_count"] = closed_count
    facts["unresolved_path_count"] = unresolved
    facts["residual_quantity_e4"] = residual
    return blockers, facts


def _validate_b09_training(
    revision: Mapping[str, Any] | None,
    experiment_id: str,
) -> list[dict[str, str]]:
    if experiment_id != "B09-LISTING-TO-START-DRIFT" or revision is None:
        return []
    blockers: list[dict[str, str]] = []
    stage = "TRAINING"
    input_name = "frozen_experiment"
    freeze = revision.get("parameter_freeze")
    if not isinstance(freeze, Mapping):
        return [
            _blocker(
                "B09_TRAINING_FREEZE_MISSING",
                stage,
                input_name,
                "/revisions/parameter_freeze",
                "B09 training protocol and outputs are required for NetPnL",
            )
        ]
    protocol = freeze.get("training_validation_protocol")
    artifact = (
        protocol.get("train_artifact_sha256")
        if isinstance(protocol, Mapping)
        else None
    )
    if not _is_sha256(artifact):
        blockers.append(
            _blocker(
                "B09_TRAIN_ARTIFACT_MISSING",
                stage,
                input_name,
                "/revisions/parameter_freeze/training_validation_protocol/"
                "train_artifact_sha256",
                "B09 NetPnL requires a sealed train-only artifact",
            )
        )
    outputs = freeze.get("unresolved_training_outputs")
    for field, code in (
        ("admitted_cells", "B09_ADMITTED_CELLS_NOT_FROZEN"),
        ("direction_by_cell", "B09_DIRECTION_BY_CELL_NOT_FROZEN"),
    ):
        row = outputs.get(field) if isinstance(outputs, Mapping) else None
        value = row.get("frozen") if isinstance(row, Mapping) else None
        status = row.get("status") if isinstance(row, Mapping) else None
        if (
            value is None
            or value == []
            or value == {}
            or status not in {"BOUND", "FROZEN", "TRAINED_AND_FROZEN"}
        ):
            blockers.append(
                _blocker(
                    code,
                    stage,
                    input_name,
                    f"/revisions/parameter_freeze/unresolved_training_outputs/"
                    f"{field}",
                    f"{field} must be produced by training and immutably frozen",
                )
            )
    bindings = freeze.get("pretraining_required_bindings")
    if not isinstance(bindings, list):
        blockers.append(
            _blocker(
                "B09_PRETRAINING_BINDINGS_MISSING",
                stage,
                input_name,
                "/revisions/parameter_freeze/pretraining_required_bindings",
                "all pretraining choices must be bound before NetPnL",
            )
        )
    else:
        expected_fields = {
            "training_validation_allocation_receipt_sha256",
            "minimum_roots_per_cell",
            "minimum_independent_days_per_cell",
            "leave_day_out_sign_stability_rule",
            "entry_exit_horizon_registry",
            "reversal_rule",
            "slippage_cap_e4",
            "quantity_rule",
        }
        by_field = {
            row.get("field"): row
            for row in bindings
            if isinstance(row, Mapping) and isinstance(row.get("field"), str)
        }
        for field in sorted(expected_fields):
            row = by_field.get(field)
            value = row.get("value") if isinstance(row, Mapping) else None
            status = row.get("status") if isinstance(row, Mapping) else None
            valid_value = (
                _is_sha256(value)
                if field.endswith("_sha256")
                else value is not None and value != "" and type(value) is not bool
            )
            if not valid_value or status not in {"BOUND", "FROZEN"}:
                blockers.append(
                    _blocker(
                        "B09_PRETRAINING_BINDING_MISSING",
                        stage,
                        input_name,
                        "/revisions/parameter_freeze/"
                        f"pretraining_required_bindings/{field}",
                        f"{field} must be explicitly bound before NetPnL",
                    )
                )
    return blockers


def evaluate_readiness(
    *,
    experiment_id: str,
    frozen_experiment: object,
    fee_facts_receipt: object = None,
    measured_latency_receipt: object = None,
    release_dq_receipt: object = None,
    terminal_coverage_receipt: object = None,
    input_sha256s: Mapping[str, str | None] | None = None,
    input_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate both readiness tiers without reading, writing, or networking."""

    if not _is_nonempty(experiment_id):
        experiment_id = ""
    freeze_blockers, revision, freeze_facts = _validate_freeze(
        frozen_experiment,
        experiment_id,
    )
    data_blockers, data_facts = _validate_release_dq(release_dq_receipt)
    fee_blockers, fee_facts = _validate_fee(fee_facts_receipt)
    latency_blockers, latency_facts = _validate_latency(measured_latency_receipt)
    terminal_blockers, terminal_facts = _validate_terminal(
        terminal_coverage_receipt
    )
    training_blockers = _validate_b09_training(revision, experiment_id)

    engineering_blockers = list(freeze_blockers) + list(data_blockers)
    net_only_blockers = (
        list(fee_blockers)
        + list(latency_blockers)
        + list(terminal_blockers)
        + list(training_blockers)
    )
    if input_errors:
        engineering_inputs = {"frozen_experiment", "release_dq"}
        for input_name, detail in sorted(input_errors.items()):
            row = _blocker(
                "INPUT_READ_ERROR",
                "INPUT",
                input_name,
                "/",
                detail,
            )
            if input_name in engineering_inputs:
                engineering_blockers.append(row)
            else:
                net_only_blockers.append(row)

    engineering_blockers = _sorted_unique_blockers(engineering_blockers)
    net_blockers = _sorted_unique_blockers(
        list(engineering_blockers) + net_only_blockers
    )
    all_blockers = _sorted_unique_blockers(
        list(engineering_blockers) + net_only_blockers
    )

    hashes = {
        "frozen_experiment": None,
        "fee_facts": None,
        "measured_latency": None,
        "release_dq": None,
        "terminal_coverage": None,
    }
    if input_sha256s:
        for key in hashes:
            supplied = input_sha256s.get(key)
            hashes[key] = supplied if _is_sha256(supplied) else None

    validated_facts = {
        "freeze": freeze_facts,
        "release_dq": data_facts,
        "fee_facts": fee_facts,
        "measured_latency": latency_facts,
        "terminal_coverage": terminal_facts,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id or None,
        "freeze_id": (
            frozen_experiment.get("freeze_id")
            if isinstance(frozen_experiment, Mapping)
            else None
        ),
        "claim_tier": freeze_facts.get("claim_tier"),
        "promotion_allowed": freeze_facts.get("promotion_allowed"),
        "engineering_l2_dates": list(ENGINEERING_L2_DATES),
        "input_sha256s": hashes,
        "validated_facts": validated_facts,
        "engineering_replay": {
            "state": (
                ENGINEERING_READY
                if not engineering_blockers
                else ENGINEERING_BLOCKED
            ),
            "blockers": engineering_blockers,
        },
        "net_pnl": {
            "state": NET_READY if not net_blockers else NET_BLOCKED,
            "blockers": net_blockers,
        },
        "all_blockers": all_blockers,
    }


evaluate_preflight = evaluate_readiness


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ReadOnlyInputError("JSON contains a duplicate object key")
        document[key] = value
    return document


def _reject_nonfinite(value: str) -> None:
    raise ReadOnlyInputError(f"JSON contains non-finite numeric token {value}")


def _read_json_object(path_text: str) -> tuple[Mapping[str, Any], str]:
    path = Path(path_text)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ReadOnlyInputError("input file does not exist") from exc
    except OSError as exc:
        raise ReadOnlyInputError("input file cannot be opened read-only") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReadOnlyInputError("input must be a regular file")
        if metadata.st_size > MAX_INPUT_BYTES:
            raise ReadOnlyInputError("input exceeds the 16 MiB safety limit")
        chunks: list[bytes] = []
        remaining = MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_INPUT_BYTES:
            raise ReadOnlyInputError("input exceeds the 16 MiB safety limit")
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(payload).hexdigest()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReadOnlyInputError("input is not valid UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ReadOnlyInputError) as exc:
        raise ReadOnlyInputError("input is not strict JSON") from exc
    if not isinstance(document, Mapping):
        raise ReadOnlyInputError("input JSON root must be an object")
    return document, digest


def _minimal_internal_error(experiment_id: str) -> dict[str, Any]:
    blocker = _blocker(
        "PREFLIGHT_INTERNAL_ERROR",
        "INPUT",
        "preflight",
        "/",
        "preflight could not safely complete",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id or None,
        "freeze_id": None,
        "claim_tier": None,
        "promotion_allowed": None,
        "engineering_l2_dates": list(ENGINEERING_L2_DATES),
        "input_sha256s": {
            "frozen_experiment": None,
            "fee_facts": None,
            "measured_latency": None,
            "release_dq": None,
            "terminal_coverage": None,
        },
        "validated_facts": {},
        "engineering_replay": {
            "state": ENGINEERING_BLOCKED,
            "blockers": [blocker],
        },
        "net_pnl": {
            "state": NET_BLOCKED,
            "blockers": [blocker],
        },
        "all_blockers": [blocker],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only fail-closed PnL-spine readiness gate"
    )
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--frozen-experiment", required=True)
    parser.add_argument("--fee-facts")
    parser.add_argument("--measured-latency")
    parser.add_argument("--release-dq")
    parser.add_argument("--terminal-coverage")
    parser.add_argument(
        "--required-tier",
        required=True,
        choices=(ENGINEERING_READY, NET_READY),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    file_arguments = {
        "frozen_experiment": args.frozen_experiment,
        "fee_facts": args.fee_facts,
        "measured_latency": args.measured_latency,
        "release_dq": args.release_dq,
        "terminal_coverage": args.terminal_coverage,
    }
    documents: dict[str, object] = {}
    hashes: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for input_name, path_text in file_arguments.items():
        if path_text is None:
            documents[input_name] = None
            hashes[input_name] = None
            continue
        try:
            document, digest = _read_json_object(path_text)
        except ReadOnlyInputError as exc:
            documents[input_name] = None
            hashes[input_name] = None
            errors[input_name] = str(exc)
        else:
            documents[input_name] = document
            hashes[input_name] = digest
    try:
        result = evaluate_readiness(
            experiment_id=args.experiment_id,
            frozen_experiment=documents["frozen_experiment"],
            fee_facts_receipt=documents["fee_facts"],
            measured_latency_receipt=documents["measured_latency"],
            release_dq_receipt=documents["release_dq"],
            terminal_coverage_receipt=documents["terminal_coverage"],
            input_sha256s=hashes,
            input_errors=errors,
        )
    except Exception:
        result = _minimal_internal_error(args.experiment_id)
    sys.stdout.buffer.write(_canonical_stdout(result))
    selected = (
        result["engineering_replay"]["state"]
        if args.required_tier == ENGINEERING_READY
        else result["net_pnl"]["state"]
    )
    return 0 if selected == args.required_tier else 2


if __name__ == "__main__":
    raise SystemExit(main())
