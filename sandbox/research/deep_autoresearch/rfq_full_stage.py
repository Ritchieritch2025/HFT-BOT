#!/usr/bin/env python3
"""Retained-object RFQ exploratory stage for SPORTS-AUTORESEARCH-01.

The stage strictly scans every row in the retained, manifest-bound RFQ object
set, but keeps the RFQ-to-CLOB expansion bounded with a deterministic
root-event sample. A pre-result whole-object quarantine is permitted only
through the exact fail-closed binding below and makes coverage explicitly
partial. It is deliberately separate from ``run_cycle1.py`` so a long raw JSON
scan can be resumed without changing the registered analysis logic.

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
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
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
QUARANTINE_DECLARATION = "DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE.json"
MALFORMED_OBJECT_RECEIPT = "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT.json"
STRICT_REMAINING_PARSE_POLICY = (
    "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
)
EXPECTED_BLANK_CONTROL_MARKERS = (
    "gap",
    "hour_open",
    "loss",
    "transport_close",
    "transport_error",
)
REPAIR_DECLARATION_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/QUARANTINE_DECLARATION.json"
)
REPAIR_RECEIPT_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/MALFORMED_OBJECT_RECEIPT.json"
)
FAILED_STATE_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/REPORT/tables/"
    "RFQ_FULL_STAGE_STATE.json"
)
FAILED_RESOURCE_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/logs/resources/rfq_full_stage.json"
)
FAILED_SCRATCH_RECEIPT_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "RFQ_FAILED_SCRATCH_RECEIPT.json"
)
FAILED_INPUT_IDENTITY_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "RFQ_FULL_INPUT_IDENTITY.json"
)
CYCLE1_DUCKDB_BINDING = "DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json"
CYCLE1_DUCKDB_BINDING_ARCHIVE = (
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "CYCLE1_DUCKDB_BINDING.json"
)
CYCLE1_CORE_SUMMARY = "REPORT/CYCLE1_CORE_SUMMARY.json"
CYCLE1_CORE_RESOURCE = "logs/resources/cycle1_core.json"
REPAIR01_REGISTRATION = (
    "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
)
QUARANTINE_DECLARATION_02 = "DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE_02.json"
MALFORMED_OBJECT_RECEIPT_02 = "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT_02.json"
REPAIR02_ROOT = "DATA_INTEGRITY/repairs/repair-02"
REPAIR02_DECLARATION_ARCHIVE = f"{REPAIR02_ROOT}/QUARANTINE_DECLARATION.json"
REPAIR02_RECEIPT_ARCHIVE = f"{REPAIR02_ROOT}/MALFORMED_OBJECT_RECEIPT.json"
REPAIR02_AUTHORIZATION_ARCHIVE = f"{REPAIR02_ROOT}/USER_AUTHORIZATION.json"
REPAIR02_REGISTRATION = f"{REPAIR02_ROOT}/REPAIR_REGISTRATION.json"
REPAIR02_PRE_ROOT = f"{REPAIR02_ROOT}/pre_repair"
REPAIR02_FAILED_STATE_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/REPORT/tables/RFQ_FULL_STAGE_STATE.json"
)
REPAIR02_FAILED_RESOURCE_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/logs/resources/rfq_full_stage_repair01.json"
)
REPAIR02_FAILED_SCRATCH_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json"
)
REPAIR02_FAILED_INPUT_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
)
REPAIR02_STRUCTURAL_AUDIT_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/DATA_INTEGRITY/RFQ_STRUCTURAL_INTEGRITY_AUDIT_02.json"
)
REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/logs/resources/rfq_structural_integrity_audit02.json"
)
REPAIR02_BLOCKER_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/DATA_INTEGRITY/RFQ_SECOND_MALFORMED_OBJECT_BLOCKER.json"
)
REPAIR02_SESSION_RESUME_ARCHIVE = (
    f"{REPAIR02_PRE_ROOT}/DATA_INTEGRITY/SESSION_RESUME_02.json"
)
REPAIR02_SUCCESS_RESOURCE_LABEL = "rfq_full_stage_repair02"
REPAIR02_SUCCESS_RESOURCE_PATH = "logs/resources/rfq_full_stage_repair02.json"
REPAIR03_ROOT = "DATA_INTEGRITY/repairs/repair-03"
REPAIR03_PRE_ROOT = f"{REPAIR03_ROOT}/pre_repair"
REPAIR03_REGISTRATION = f"{REPAIR03_ROOT}/REPAIR_REGISTRATION.json"
REPAIR03_TRANSACTION_JOURNAL = f"{REPAIR03_ROOT}/TRANSACTION_JOURNAL.json"
REPAIR03_PARSER_CONTRACT = (
    f"{REPAIR03_ROOT}/RFQ_INNER_PAYLOAD_PARSER_CONTRACT.json"
)
REPAIR03_AUTHORITY_BASIS = f"{REPAIR03_ROOT}/AUTHORITY_BASIS.json"
REPAIR03_AUDIT_SOURCE = (
    f"{REPAIR03_ROOT}/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_SOURCE.py"
)
REPAIR03_FAILED_STATE_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/REPORT/tables/RFQ_FULL_STAGE_STATE.json"
)
REPAIR03_FAILED_RESOURCE_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/logs/resources/rfq_full_stage_repair02.json"
)
REPAIR03_FAILED_SCRATCH_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json"
)
REPAIR03_FAILED_INPUT_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
)
REPAIR03_AUDIT_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json"
)
REPAIR03_AUDIT_RESOURCE_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/logs/resources/"
    "rfq_inner_payload_contract_audit03_final.json"
)
REPAIR03_AUDIT_SOURCE_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/tmp/rfq_inner_payload_contract_audit03.py"
)
REPAIR03_CYCLE1_BINDING_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json"
)
REPAIR03_PRESERVED_SCRATCH = "cache/rfq_full_scratch.attempt03_failed.duckdb"
REPAIR03_SUCCESS_RESOURCE_LABEL = "rfq_full_stage_repair03"
REPAIR03_SUCCESS_RESOURCE_PATH = "logs/resources/rfq_full_stage_repair03.json"
REPAIR03_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_PARSER_REPAIR03_REGISTERED"
REPAIR03_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_PARSER_CORRECTION_REPAIR03_BEFORE_RFQ_RESULT"
)
REPAIR03_SCHEMA = "sports-autoresearch-rfq-parser-repair-v1"
REPAIR03_PARSER_SCHEMA = "rfq-inner-payload-parser-contract-v1"
REPAIR03_AUTHORITY_SCHEMA = (
    "sports-autoresearch-autonomous-code-repair-authority-v1"
)
REPAIR03_GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
REPAIR03_SNAPSHOT_SOURCE_MODE = (
    "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
)
REPAIR03_SOURCE_ATTESTATION = (
    "DATA_INTEGRITY/REPAIR_03_SOURCE_ATTESTATION.json"
)
REPAIR03_SOURCE_ATTESTATION_ARCHIVE = (
    f"{REPAIR03_PRE_ROOT}/{REPAIR03_SOURCE_ATTESTATION}"
)
REPAIR03_SOURCE_ATTESTATION_SCHEMA = (
    "sports-autoresearch-source-snapshot-attestation-v1"
)
REPAIR03_SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
REPAIR03_CHANGED_PATHS = [
    f"{REPAIR03_SOURCE_RELATIVE}/finalize_mission.py",
    f"{REPAIR03_SOURCE_RELATIVE}/repair03_registration.py",
    f"{REPAIR03_SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{REPAIR03_SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{REPAIR03_SOURCE_RELATIVE}/test_repair03_registration.py",
    f"{REPAIR03_SOURCE_RELATIVE}/test_rfq_full_stage.py",
]
REPAIR03_FINDING = (
    "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD"
)
REPAIR03_CHANGE_CLASS = (
    "PARSER_CONTRACT_CORRECTION_ONLY_NO_DATA_SELECTION_OR_HYPOTHESIS_CHANGE"
)
REPAIR03_RETRY_REQUIREMENT = (
    "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
)
REPAIR03_AUDITED_PARSER_CONTRACT = {
    "data_frame_marker": "MARKER_FIELD_ABSENT",
    "data_frame_raw": "NONEMPTY_JSON_OBJECT",
    "empty_control_markers": list(EXPECTED_BLANK_CONTROL_MARKERS),
    "empty_control_raw": "EXACT_EMPTY_STRING",
    "explicit_null_marker": "FAIL_CLOSED",
    "json_object_payload_markers": ["segment_receipt"],
    "marker_case": "EXACT_CASE_SENSITIVE",
    "missing_null_or_non_string_raw": "FAIL_CLOSED",
    "raw_b64_without_raw": "FAIL_CLOSED",
    "schema_version": REPAIR03_PARSER_SCHEMA,
    "unknown_or_invalid_marker": "FAIL_CLOSED",
    "valid_non_object_inner_json": "FAIL_CLOSED",
}
REPAIR03_EXPECTED_LINES = 58_144_912
REPAIR03_EXPECTED_BLANK_CONTROLS = 1_111
REPAIR03_EXPECTED_DATA_FRAMES = 58_143_241
REPAIR03_EXPECTED_SEGMENT_RECEIPTS = 560
REPAIR03_EXPECTED_MARKER_COUNTS = {
    "hour_open": 575,
    "segment_receipt": 560,
    "transport_close": 536,
}
# Attempt-04 may consume repair-03 only from this operator-approved, immutable
# preregistration boundary.  These are deliberately independent of the hashes
# repeated inside RUN_MANIFEST/REPAIR_REGISTRATION: a coordinated rewrite of
# both JSON documents must not be able to manufacture a new parser authority.
REPAIR03_APPROVED_FAILED_STATE_SHA256 = (
    "cc59ac42d6a8204650d3f60554fddfce2af9e849522f0370ea030f08c9a078b6"
)
REPAIR03_APPROVED_FAILED_RESOURCE_SHA256 = (
    "a5adca32034c7db02652433ca733fa3657ffeaadbda0809d7f33df8f11230766"
)
REPAIR03_APPROVED_FAILED_INPUT_SHA256 = (
    "68a7e3aeb22851195769c5e4ab5b614979212e0469ac2584afb4de50870560e1"
)
REPAIR03_APPROVED_FAILED_SCRATCH_RECEIPT_SHA256 = (
    "6cc399ef7dbcfa4c32adbbdcf507940d0253c2e7bbc6e40000da1cc3c176c197"
)
REPAIR03_APPROVED_FAILED_SCRATCH_SHA256 = (
    "32b2352dc0390fa5a4f42f2a483bdfa570b9e2a7a6de77d51847afb38fd1cff5"
)
REPAIR03_APPROVED_FAILED_SCRATCH_BYTES = 28_731_781_120
REPAIR03_APPROVED_AUDIT_SHA256 = (
    "6d0dc5c16ddad6772657ed7a4ede1f90d255e6629ffd1babbd837b3863de7a0d"
)
REPAIR03_APPROVED_AUDIT_RESOURCE_SHA256 = (
    "d820ac998a57428e64a46485cd17319c0430ef9c4c4e516afaac67bec4a3c05d"
)
REPAIR03_APPROVED_AUDIT_SOURCE_SHA256 = (
    "27199557ea7aacf9a19f66d16f5fd3770ee0149ef0ed4cd081169a9e0ccb01ea"
)
REPAIR03_APPROVED_AUDITED_PARSER_CONTRACT_SHA256 = (
    "9fd339e35584a38372ad4f14ba89f0d7a8a38bdd17ebcc3ab53c3d77d19c6389"
)
# Repair-04 is an append-only execution-resource correction.  It is allowed to
# inherit repair-03's parser, retained object set, quarantine, Cycle-1 result,
# and hypothesis registry only after the failed attempt-04 artifacts below are
# matched byte-for-byte.  No SQL or research-design change is authorized.
REPAIR04_ROOT = "DATA_INTEGRITY/repairs/repair-04"
REPAIR04_PRE_ROOT = f"{REPAIR04_ROOT}/pre_repair"
REPAIR04_REGISTRATION = f"{REPAIR04_ROOT}/REPAIR_REGISTRATION.json"
REPAIR04_TRANSACTION_JOURNAL = f"{REPAIR04_ROOT}/TRANSACTION_JOURNAL.json"
REPAIR04_RESOURCE_CONTRACT_PATH = f"{REPAIR04_ROOT}/RFQ_RESOURCE_CONTRACT.json"
REPAIR04_AUTHORITY_BASIS = f"{REPAIR04_ROOT}/AUTHORITY_BASIS.json"
REPAIR04_FAILED_STATE_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/REPORT/tables/RFQ_FULL_STAGE_STATE.json"
)
REPAIR04_FAILED_RESOURCE_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/logs/resources/rfq_full_stage_repair03.json"
)
REPAIR04_FAILED_SCRATCH_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json"
)
REPAIR04_FAILED_INPUT_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
)
REPAIR04_CYCLE1_BINDING_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json"
)
REPAIR04_W09_ATTESTATION_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/DATA_INTEGRITY/W09_ATTESTATION.json"
)
REPAIR04_PRESERVED_SCRATCH = "cache/rfq_full_scratch.attempt04_failed.duckdb"
REPAIR04_EXECUTION_QUERY = "queries/rfq_full_stage.py"
REPAIR04_SUCCESS_RESOURCE_LABEL = "rfq_full_stage_repair04"
REPAIR04_SUCCESS_RESOURCE_PATH = "logs/resources/rfq_full_stage_repair04.json"
REPAIR04_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_RESOURCE_REPAIR04_REGISTERED"
REPAIR04_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_RESOURCE_RETUNE_REPAIR04_BEFORE_RFQ_RESULT"
)
REPAIR04_SCHEMA = "sports-autoresearch-resource-contract-repair-v1"
REPAIR04_RESOURCE_SCHEMA = "rfq-execution-resource-contract-v1"
REPAIR04_AUTHORITY_SCHEMA = (
    "sports-autoresearch-existing-w09-resource-retune-authority-v1"
)
REPAIR04_GIT_SOURCE_MODE = REPAIR03_GIT_SOURCE_MODE
REPAIR04_SNAPSHOT_SOURCE_MODE = REPAIR03_SNAPSHOT_SOURCE_MODE
REPAIR04_SOURCE_ATTESTATION = (
    "DATA_INTEGRITY/REPAIR_04_SOURCE_ATTESTATION.json"
)
REPAIR04_SOURCE_ATTESTATION_ARCHIVE = (
    f"{REPAIR04_PRE_ROOT}/{REPAIR04_SOURCE_ATTESTATION}"
)
REPAIR04_SOURCE_ATTESTATION_SCHEMA = REPAIR03_SOURCE_ATTESTATION_SCHEMA
REPAIR04_SOURCE_RELATIVE = REPAIR03_SOURCE_RELATIVE
REPAIR04_CHANGED_PATHS = [
    f"{REPAIR04_SOURCE_RELATIVE}/finalize_mission.py",
    f"{REPAIR04_SOURCE_RELATIVE}/repair04_registration.py",
    f"{REPAIR04_SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{REPAIR04_SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{REPAIR04_SOURCE_RELATIVE}/test_repair04_registration.py",
    f"{REPAIR04_SOURCE_RELATIVE}/test_rfq_full_stage.py",
]
REPAIR04_FINDING = "DUCKDB_OUT_OF_MEMORY_DURING_RFQ_DEDUPLICATION_WINDOW"
REPAIR04_CHANGE_CLASS = (
    "EXISTING_W09_EXECUTION_RESOURCE_CONTRACT_RETUNE_ONLY_NO_DATA_PARSER_QUERY_"
    "SELECTION_QUARANTINE_OR_HYPOTHESIS_CHANGE"
)
REPAIR04_RETRY_REQUIREMENT = (
    "FRESH_SCRATCH_SAME_SELECTION_SAME_PARSER_QUERY_AND_HYPOTHESES_"
    "RETUNED_RESOURCES_NO_RESUME"
)
REPAIR04_FAILURE_DISPOSITION = "RESOURCE_CAP_FAILURE_BEFORE_RESULT"
REPAIR04_AUTHORITY_CLASS = "MISSION_AUTHORIZED_EXISTING_W09_RESOURCE_RETUNING"
REPAIR04_RESEARCH_TRIAL_IDS = [
    "C1-RFQ-CLOB-01",
    "C1-ANOM-RFQ-SIZE-TAIL-01",
    "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
]
REPAIR04_RUNTIME_CONTRACT = {
    "memory_limit": "46GB",
    "max_temp_size": "70GB",
    "threads": 4,
    "min_free_gib": 100.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}
REPAIR04_PREVIOUS_RESOURCE_CONTRACT = {
    "memory_limit": "40GB",
    "max_temp_size": "120GB",
    "threads": 8,
    "min_free_gib": 120.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}
REPAIR05_ROOT = "DATA_INTEGRITY/repairs/repair-05"
REPAIR05_PRE_ROOT = f"{REPAIR05_ROOT}/pre_repair"
REPAIR05_REGISTRATION = f"{REPAIR05_ROOT}/REPAIR_REGISTRATION.json"
REPAIR05_TRANSACTION_JOURNAL = f"{REPAIR05_ROOT}/TRANSACTION_JOURNAL.json"
REPAIR05_AUTHORITY_BASIS = f"{REPAIR05_ROOT}/AUTHORITY_BASIS.json"
REPAIR05_WIRING_CONTRACT = (
    f"{REPAIR05_ROOT}/RFQ_CONSUMER_VALIDATION_WIRING_CONTRACT.json"
)
REPAIR05_BLOCKER_ACTIVE = (
    "DATA_INTEGRITY/RFQ_REPAIR04_CONSUMER_BLOCKER_05.json"
)
REPAIR05_BLOCKER_ARCHIVE = f"{REPAIR05_PRE_ROOT}/{REPAIR05_BLOCKER_ACTIVE}"
REPAIR05_FAILED_RESOURCE_ARCHIVE = (
    f"{REPAIR05_PRE_ROOT}/{REPAIR04_SUCCESS_RESOURCE_PATH}"
)
REPAIR05_UNCHANGED_STATE_ARCHIVE = (
    f"{REPAIR05_PRE_ROOT}/REPORT/tables/RFQ_FULL_STAGE_STATE.json"
)
REPAIR05_UNCHANGED_INPUT_ARCHIVE = (
    f"{REPAIR05_PRE_ROOT}/DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
)
REPAIR05_CYCLE1_BINDING_ARCHIVE = (
    f"{REPAIR05_PRE_ROOT}/{CYCLE1_DUCKDB_BINDING}"
)
REPAIR05_W09_ATTESTATION_ARCHIVE = (
    f"{REPAIR05_PRE_ROOT}/DATA_INTEGRITY/W09_ATTESTATION.json"
)
REPAIR05_EXECUTION_QUERY = REPAIR04_EXECUTION_QUERY
REPAIR05_SUCCESS_RESOURCE_LABEL = "rfq_full_stage_repair05"
REPAIR05_SUCCESS_RESOURCE_PATH = "logs/resources/rfq_full_stage_repair05.json"
REPAIR05_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_CONSUMER_WIRING_REPAIR05_REGISTERED"
REPAIR05_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_CONSUMER_WIRING_REPAIR05_BEFORE_RFQ_RESULT"
)
REPAIR05_SCHEMA = "sports-autoresearch-consumer-validation-wiring-repair-v1"
REPAIR05_WIRING_SCHEMA = "rfq-consumer-validation-wiring-contract-v1"
REPAIR05_AUTHORITY_SCHEMA = (
    "sports-autoresearch-consumer-validation-wiring-authority-v1"
)
REPAIR05_FINDING = "REPAIR04_CONSUMER_CYCLE1_BINDING_LOOKUP_KEYERROR"
REPAIR05_FAILURE_DISPOSITION = (
    "CONSUMER_PRE_EVIDENCE_VALIDATION_FAILURE_BEFORE_RESULT"
)
REPAIR05_FAILURE_PHASE = "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION"
REPAIR05_CHANGE_CLASS = (
    "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY_NO_DATA_PARSER_QUERY_SEMANTICS_"
    "SELECTION_QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_CHANGE"
)
REPAIR05_RETRY_REQUIREMENT = (
    "APPEND_ONLY_REPAIR05_CONSUMER_VALIDATION_WIRING_CORRECTION_AND_FRESH_"
    "SCRATCH_RETRY"
)
REPAIR05_AUTHORITY_CLASS = (
    "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_SPEND_DATA_OR_"
    "FROZEN_VALIDATION_SET"
)
REPAIR05_BLOCKER_SHA256 = (
    "42c2cc06c4b3073c0e6a1b5a681e86eb03d7a0f3afda8f694e2d0f0932fdaa5f"
)
REPAIR05_FAILED_RESOURCE_SHA256 = (
    "e37d019ea272f9acacbe2c5b2464daebcb5a33b2eb54b1bf2665b3db6bcf11ef"
)
REPAIR05_PARENT_RESOURCE_CONTRACT_SHA256 = (
    "8c36b2747d9e5f991ecd5cf18e9c44fd5cea5f5725fc2a622113d42102e88325"
)
REPAIR05_PARENT_RFQ_QUERY_SHA256 = (
    "3bf8b255f624aae4fc6f2a00a8e5ffb5037d701077435d5bddc3c4d093ba4ba4"
)
REPAIR05_FAILED_RESOURCE_WALL_SECONDS = 18.118
# Repair-06 is a status-dispatch wiring correction only.  Attempt-06 failed in
# validate_run before input discovery, scratch creation, or any RFQ evidence
# write because the already registered repair-05 status was missing from the
# RFQ-stage allowlist.  The descendant must replay repair-05 from its immutable
# pre-repair boundary and may change no research semantics or resources.
REPAIR06_ROOT = "DATA_INTEGRITY/repairs/repair-06"
REPAIR06_PRE_ROOT = f"{REPAIR06_ROOT}/pre_repair"
REPAIR06_REGISTRATION = f"{REPAIR06_ROOT}/REPAIR_REGISTRATION.json"
REPAIR06_TRANSACTION_JOURNAL = f"{REPAIR06_ROOT}/TRANSACTION_JOURNAL.json"
REPAIR06_AUTHORITY_BASIS = f"{REPAIR06_ROOT}/AUTHORITY_BASIS.json"
REPAIR06_STATUS_WIRING_CONTRACT = (
    f"{REPAIR06_ROOT}/RFQ_VALIDATE_RUN_STATUS_WIRING_CONTRACT.json"
)
REPAIR06_BLOCKER_ACTIVE = "DATA_INTEGRITY/RFQ_REPAIR05_STATUS_BLOCKER_06.json"
REPAIR06_BLOCKER_ARCHIVE = f"{REPAIR06_PRE_ROOT}/{REPAIR06_BLOCKER_ACTIVE}"
REPAIR06_FAILED_RESOURCE_ARCHIVE = (
    f"{REPAIR06_PRE_ROOT}/{REPAIR05_SUCCESS_RESOURCE_PATH}"
)
REPAIR06_UNCHANGED_STATE_ARCHIVE = (
    f"{REPAIR06_PRE_ROOT}/REPORT/tables/RFQ_FULL_STAGE_STATE.json"
)
REPAIR06_UNCHANGED_INPUT_ARCHIVE = (
    f"{REPAIR06_PRE_ROOT}/DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
)
REPAIR06_CYCLE1_BINDING_ARCHIVE = (
    f"{REPAIR06_PRE_ROOT}/{CYCLE1_DUCKDB_BINDING}"
)
REPAIR06_EXECUTION_QUERY = REPAIR05_EXECUTION_QUERY
REPAIR06_SUCCESS_RESOURCE_LABEL = "rfq_full_stage_repair06"
REPAIR06_SUCCESS_RESOURCE_PATH = "logs/resources/rfq_full_stage_repair06.json"
REPAIR06_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_STATUS_WIRING_REPAIR06_REGISTERED"
REPAIR06_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_STATUS_WIRING_REPAIR06_BEFORE_RFQ_RESULT"
)
REPAIR06_SCHEMA = "sports-autoresearch-validate-run-status-wiring-repair-v1"
REPAIR06_STATUS_WIRING_SCHEMA = "rfq-validate-run-status-wiring-contract-v1"
REPAIR06_AUTHORITY_SCHEMA = (
    "sports-autoresearch-validate-run-status-wiring-authority-v1"
)
REPAIR06_FINDING = "REPAIR05_STATUS_NOT_ACCEPTED_BY_VALIDATE_RUN"
REPAIR06_FAILURE_PHASE = "PRE_EVIDENCE_VALIDATE_RUN_STATUS_GATE"
REPAIR06_FAILURE_DISPOSITION = "STATUS_WIRING_FAILURE_BEFORE_RESULT"
REPAIR06_CHANGE_CLASS = "VALIDATE_RUN_STATUS_WIRING_ONLY"
REPAIR06_REGISTRATION_CHANGE_CLASS = (
    "VALIDATE_RUN_STATUS_WIRING_ONLY_NO_DATA_PARSER_QUERY_SEMANTICS_SELECTION_"
    "QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_CHANGE"
)
REPAIR06_RETRY_REQUIREMENT = (
    "APPEND_ONLY_REPAIR06_VALIDATE_RUN_STATUS_WIRING_CORRECTION_AND_FRESH_"
    "SCRATCH_RETRY"
)
REPAIR06_AUTHORITY_CLASS = REPAIR05_AUTHORITY_CLASS
REPAIR06_BLOCKER_SHA256 = (
    "8afa321af51d6a23a958de9348413eed22df60173cd9b64a09612840dac0077f"
)
REPAIR06_FAILED_RESOURCE_SHA256 = (
    "3adc0f3f56dad9483e3215f172c547c97f5b85890def8512053eeb958f64fc24"
)
REPAIR06_PARENT_RFQ_QUERY_SHA256 = (
    "e40d8c1c8560d9cb08904dd3b58259f8e4c91da5c2221015f7b558dc0f1c90ca"
)
REPAIR06_FAILED_RESOURCE_WALL_SECONDS = 0.114
REPAIR06_PARENT_MANIFEST_SHA256 = (
    "a3af7f05cba25a56e1ae1f2d7541588f67e487c8292f62aeb5f1bd52a767383a"
)
REPAIR06_PARENT_RECEIPT_SHA256 = (
    "13d01e2d4122dc801a88f8c1e7a6824a1f928181900415f922a0908b31b9893d"
)
REPAIR06_PARENT_JOURNAL_SHA256 = (
    "4eef0a29c181ed7b0c28017ba9246831fd8c36e74fd0843f6da665415533aedc"
)
REPAIR06_PARENT_WIRING_SHA256 = (
    "dc2a4f058eab3ff684ee3e9324f58cf19976c54dec9c453bf082d17269e2aafb"
)
REPAIR06_PARENT_AUTHORITY_SHA256 = (
    "c462f9409f9b25ad2d86561d908a17c7856b9a754bed796fd78c9bffd493d073"
)
REPAIR06_PRESERVED_SCRATCH_POLICY = (
    "FRESH_SCRATCH_REQUIRED_NO_RESUME_NO_PRIOR_SCRATCH_OR_WAL"
)
REPAIR02_APPROVED_SELECTION_FINGERPRINT = (
    "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
)
REPAIR02_APPROVED_RETAINED_FINGERPRINT = (
    "cf6885a13ac50369fbfb19aba3809cd7e83c8bdabe4381ec926ed1c61d47b652"
)
REPAIR02_APPROVED_FULL_FINGERPRINT = (
    "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71"
)
REPAIR02_APPROVED_QUARANTINED_FINGERPRINT = (
    "cf0e874f65aad791c5a23164a34511bd0482d7ece9de0706841c5b8bc92808eb"
)
REPAIR04_MEMTOTAL_BYTES = 66_194_702_336
REPAIR04_APPROVED_FAILED_STATE_SHA256 = (
    "3c826ee72fa69f9c02c4a38fa33d3c65006ba33374dc592c269388228d506e57"
)
REPAIR04_APPROVED_FAILED_RESOURCE_SHA256 = (
    "08690ed35a3ef21a78654bec58f5637d31b9d97704d00cfbdb672a29adee5193"
)
REPAIR04_APPROVED_FAILED_INPUT_SHA256 = (
    "c0ee7ed24d27c58eff4600d33bf7b6d283aaea38020b3a7aee0e63c9c06c2bbb"
)
REPAIR04_APPROVED_FAILED_SCRATCH_RECEIPT_SHA256 = (
    "d850e185a4a46dcf711e8bad7e081be431cceae64ebd8bdc7a32fa5beecc5fb9"
)
REPAIR04_APPROVED_FAILED_SCRATCH_SHA256 = (
    "f2174bfe998b992964ca8becf0c7191dc143587f5a9a326d4529e204bcd88d6a"
)
REPAIR04_APPROVED_FAILED_SCRATCH_BYTES = 28_761_927_680
REPAIR04_APPROVED_FAILED_SCRATCH_MTIME = (
    "2026-07-15T17:00:33.148336873+00:00"
)
REPAIR04_APPROVED_FAILED_SCRATCH_INODE = 9_700_311
REPAIR04_APPROVED_RESOURCE_WALL_SECONDS = 909.999
REPAIR04_APPROVED_RESOURCE_PEAK_RSS_KIB = 40_379_312
REPAIR04_APPROVED_RESOURCE_PEAK_TEMP_BYTES = 15_023_231_048
REPAIR04_APPROVED_RESOURCE_MIN_FREE_BYTES = 109_311_180_800
REPAIR04_CURRENT_FREE_AFTER_PRESERVE_BYTES = 124_324_511_744
REPAIR04_PRIOR_DISK_FREE_BEFORE_BYTES = 153_086_521_344
REPAIR04_PRIOR_MAX_DISK_DELTA_BYTES = 43_775_340_544
REPAIR04_PROJECTED_MIN_FREE_BYTES = 80_549_171_200
REPAIR04_APPROVED_FAILED_ERROR_TYPE = "OutOfMemoryException"
REPAIR04_APPROVED_FAILED_ERROR = (
    "Out of Memory Error: failed to pin block of size 256.0 KiB "
    "(37.2 GiB/37.2 GiB used)\n\n"
    "Possible solutions:\n"
    "* Reducing the number of threads (SET threads=X)\n"
    "* Disabling insertion-order preservation "
    "(SET preserve_insertion_order=false)\n"
    "* Increasing the memory limit (SET memory_limit='...GB')\n\n"
    "See also https://duckdb.org/docs/stable/guides/performance/"
    "how_to_tune_workloads"
)
REPAIR03_EXPECTED_RETAINED_OBJECTS = 282
REPAIR03_EXPECTED_RETAINED_BYTES = 59_185_856_724
REPAIR02_EXPECTED_TOTAL_UNIQUE_OBJECTS = 284
REPAIR02_EXPECTED_TOTAL_LOGICAL_BINDINGS = 296
REPAIR02_EXPECTED_TOTAL_BYTES = 59_719_895_414
REPAIR02_EXPECTED_RETAINED_UNIQUE_OBJECTS = 282
REPAIR02_EXPECTED_RETAINED_LOGICAL_BINDINGS = 294
REPAIR02_EXPECTED_RETAINED_BYTES = 59_185_856_724
REPAIR02_EXPECTED_QUARANTINED_BYTES = 534_038_690
REPAIR02_AUTHORIZED_ACTION = (
    "批准 SPORTS-AUTORESEARCH-01 repair-02:整对象隔离 "
    "raw_rfq/date=2026-07-13/rfq_23.ndjson.2,禁止逐行修补;以 282/284 "
    "个对象、59,185,856,724 字节的 PARTIAL_OBJECT_COVERAGE_QUARANTINED "
    "数据集重新注册并从新 scratch 重跑 RFQ。"
)


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


def stage_completion_status(inputs: dict) -> str:
    return (
        "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        if not inputs.get("full_object_coverage", True)
        else "COMPLETE_EXPLORATORY_ONLY"
    )


def quarantine_gap_plan(inputs: dict) -> dict:
    boundaries = inputs.get("quarantine_boundaries", [])
    return {
        "quarantined_object_count": inputs.get("quarantined_unique_objects", 0),
        "contiguous_gap_count": len(boundaries),
        "observation_boundary_count": len(boundaries) * 2,
        "contiguous_runs": [
            {
                "release_id": row["release_id"],
                "anchor_key": row["key"],
                "quarantined_keys": list(row.get("quarantined_keys", [row["key"]])),
                "quarantined_object_set_sha256": row.get(
                    "quarantined_object_set_sha256", row["sha256"]
                ),
                "previous_retained_key": row.get("previous_key"),
                "next_retained_key": row.get("next_key"),
            }
            for row in boundaries
        ],
    }


def fixed_exact(value: object, places: int) -> int | None:
    """Convert an exact fixed-point value without rounding.

    This pure helper mirrors the SQL expression used in the retained-object scan and is
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


def read_json_object(path: Path, label: str) -> dict:
    """Read a required JSON object and turn every structural error into fail-closed state."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RFQStageError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RFQStageError(f"{label} must be a JSON object: {path}")
    return value


def _object_fingerprint(objects: Sequence[dict]) -> str:
    rows = [
        f"{row['key']}\t{row['size']}\t{row['sha256']}"
        for row in sorted(objects, key=lambda item: item["key"])
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _json_payload_sha256(value: object) -> str:
    """Hash the repository's canonical pretty-printed JSON payload."""
    payload = json.dumps(
        value, indent=2, sort_keys=True, ensure_ascii=False, default=str
    ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _repository_identity(repository: dict) -> dict:
    return {
        "execution_commit": repository.get("execution_commit"),
        "source_manifest_sha256": repository.get("source_manifest_sha256"),
        "source_sha256s_sha256": repository.get("source_sha256s_sha256"),
        "query_set_sha256": repository.get("query_set_sha256"),
        "query_files": list(repository.get("query_files", [])),
    }


def _read_registry_rows(raw: bytes, label: str) -> list[dict]:
    if not raw or not raw.endswith(b"\n"):
        raise RFQStageError(f"{label} is empty or not newline terminated")
    rows = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RFQStageError(
                f"{label} row {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise RFQStageError(f"{label} row {line_number} is not an object")
        rows.append(row)
    return rows


def _validate_query_receipt(
    run_dir: Path,
    repository: dict,
    *,
    active_root: Path | None = None,
) -> dict[str, str]:
    receipt_root = active_root or run_dir
    query_files = repository.get("query_files")
    if (
        not isinstance(query_files, list)
        or not query_files
        or len(query_files) != len(set(query_files))
        or any(not isinstance(path, str) for path in query_files)
    ):
        raise RFQStageError("repair-03 repository query-file set is invalid")
    receipt = _require_run_relative_file(
        receipt_root, "QUERY_SHA256SUMS.txt", "repair-03 query receipt"
    )
    if sha256(receipt) != repository.get("query_set_sha256"):
        raise RFQStageError("repair-03 active query receipt SHA mismatch")
    rows: dict[str, str] = {}
    for line_number, line in enumerate(receipt.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None or match.group(2) in rows:
            raise RFQStageError(f"invalid repair-03 query receipt row: {line_number}")
        rows[match.group(2)] = match.group(1)
    if set(rows) != set(query_files):
        raise RFQStageError("repair-03 query receipt/file-set mismatch")
    for relative, expected in rows.items():
        path = _require_run_relative_file(
            receipt_root, relative, "repair-03 query file"
        )
        if sha256(path) != expected:
            raise RFQStageError(f"repair-03 registered query changed: {relative}")
    return rows


def _repair02_selection_fingerprint(
    full_fingerprint: str,
    retained_objects: Sequence[dict],
    quarantined_objects: Sequence[dict],
    declaration_sha256: str,
    receipt_sha256s: Sequence[str],
) -> str:
    """Return the preregistered repair-02 retained-set selection identity."""
    payload = {
        "schema": "rfq-partial-object-selection-v2",
        "total": full_fingerprint,
        "retained": _object_fingerprint(retained_objects),
        "quarantined": _object_fingerprint(quarantined_objects),
        "declaration": declaration_sha256,
        "receipts": list(receipt_sha256s),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _legacy_full_input_identity(inputs: dict) -> dict:
    """Rebuild the failed v1 full-set identity from discovered manifests."""
    return {
        "schema": "rfq-full-input-identity-v1",
        "release_ids": list(RELEASE_IDS),
        "releases": inputs["releases"],
        "objects": [
            {
                key: row[key]
                for key in ("key", "sha256", "size", "bound_release_ids")
            }
            for row in inputs["objects_detail"]
        ],
        "unique_objects": inputs["objects"],
        "logical_manifest_bindings": inputs["logical_manifest_bindings"],
        "deduplicated_overlapping_objects": inputs[
            "deduplicated_overlapping_objects"
        ],
        "path_size_sha_fingerprint": inputs["path_size_fingerprint_sha256"],
    }


def _read_captured_json(raw: bytes, path: Path, label: str) -> dict:
    """Decode captured bytes so the parsed object and its hash are the same read."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RFQStageError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RFQStageError(f"{label} must be a JSON object: {path}")
    return value


def _require_run_relative_file(run_dir: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RFQStageError(f"{label} path is missing")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RFQStageError(f"unsafe {label} path: {relative}")
    candidate = run_dir / relative_path
    if candidate.is_symlink():
        raise RFQStageError(f"{label} must not be a symlink: {relative}")
    path = candidate.resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RFQStageError(f"{label} path escapes run directory") from exc
    if not path.is_file():
        raise RFQStageError(f"{label} is missing or unsafe: {relative}")
    return path


def validate_cycle1_duckdb_binding(
    run_dir: Path, manifest: dict, core_database: Path | None = None
) -> dict:
    """Fail closed on the archived, manifest-bound Cycle-1 derived database.

    Receipt bytes and the repair registration are validated before any core artifact
    or database byte is trusted.  The returned object is the exact nested identity
    mirrored into the RFQ input identity and summary.
    """
    run_dir = run_dir.resolve()
    expected_database_path = run_dir / "cache/cycle1.duckdb"
    database_path = core_database or expected_database_path
    if expected_database_path.is_symlink() or database_path.is_symlink():
        raise RFQStageError("Cycle-1 DuckDB must not be a symlink")
    expected_database = expected_database_path.resolve()
    database = database_path.resolve()
    if database != expected_database:
        raise RFQStageError("Cycle-1 DuckDB path is not the run-local derived database")

    active_path = run_dir / CYCLE1_DUCKDB_BINDING
    archived_path = run_dir / CYCLE1_DUCKDB_BINDING_ARCHIVE
    if active_path.is_symlink() or archived_path.is_symlink():
        raise RFQStageError("Cycle-1 DuckDB binding receipts must not be symlinks")
    try:
        active_raw = active_path.read_bytes()
        archived_raw = archived_path.read_bytes()
    except OSError as exc:
        raise RFQStageError(f"Cycle-1 DuckDB binding receipt is missing: {exc}") from exc
    if active_raw != archived_raw:
        raise RFQStageError("active/archived Cycle-1 DuckDB receipt bytes differ")
    active_sha = hashlib.sha256(active_raw).hexdigest()
    archived_sha = hashlib.sha256(archived_raw).hexdigest()
    if active_sha != archived_sha:
        raise RFQStageError("active/archived Cycle-1 DuckDB receipt hashes differ")
    receipt = _read_captured_json(
        active_raw, active_path, "Cycle-1 DuckDB binding receipt"
    )
    exact_receipt_fields = {
        "bytes",
        "core_result_disposition",
        "core_stage_resource_path",
        "core_stage_resource_sha256",
        "core_summary_path",
        "core_summary_sha256",
        "created_before_rfq_repair_registration",
        "duckdb_version",
        "mtime_utc",
        "path",
        "run_id",
        "schema_version",
        "sha256",
    }
    if set(receipt) != exact_receipt_fields:
        raise RFQStageError("Cycle-1 DuckDB binding receipt field set mismatch")
    digest_fields = (
        "sha256",
        "core_summary_sha256",
        "core_stage_resource_sha256",
    )
    if any(
        not isinstance(receipt.get(field), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None
        for field in digest_fields
    ):
        raise RFQStageError("Cycle-1 DuckDB binding receipt contains an invalid SHA-256")
    if (
        receipt.get("schema_version") != "cycle1-derived-duckdb-binding-v1"
        or receipt.get("run_id") != run_dir.name
        or receipt.get("duckdb_version") != EXPECTED_DUCKDB
        or receipt.get("created_before_rfq_repair_registration") is not True
        or receipt.get("core_result_disposition")
        != "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        or receipt.get("path") != str(database)
        or type(receipt.get("bytes")) is not int
        or receipt["bytes"] <= 0
        or receipt.get("core_summary_path") != CYCLE1_CORE_SUMMARY
        or receipt.get("core_stage_resource_path") != CYCLE1_CORE_RESOURCE
    ):
        raise RFQStageError("Cycle-1 DuckDB binding receipt identity mismatch")
    try:
        parsed_mtime = dt.datetime.fromisoformat(
            receipt["mtime_utc"][:-1] + "+00:00"
            if isinstance(receipt.get("mtime_utc"), str)
            and receipt["mtime_utc"].endswith("Z")
            else receipt.get("mtime_utc")
        )
    except (TypeError, ValueError):
        parsed_mtime = None
    if parsed_mtime is None or parsed_mtime.utcoffset() != dt.timedelta(0):
        raise RFQStageError("Cycle-1 DuckDB binding UTC mtime is invalid")

    cycle_binding = {
        "active_path": CYCLE1_DUCKDB_BINDING,
        "active_sha256": active_sha,
        "archived_path": CYCLE1_DUCKDB_BINDING_ARCHIVE,
        "archived_sha256": archived_sha,
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
    repairs = manifest.get("data_integrity_repairs")
    candidates = [
        row
        for row in repairs if isinstance(row, dict)
        and row.get("repair_id") == "repair-01"
        and row.get("finding") == "MALFORMED_NDJSON_OBJECT"
    ] if isinstance(repairs, list) else []
    if len(candidates) != 1:
        raise RFQStageError("Cycle-1 DuckDB requires exactly one repair registration")
    if candidates[0].get("cycle1_duckdb_binding") != cycle_binding:
        raise RFQStageError("repair Cycle-1 DuckDB binding mismatch")

    summary_path = _require_run_relative_file(
        run_dir, receipt["core_summary_path"], "Cycle-1 core summary"
    )
    resource_path = _require_run_relative_file(
        run_dir, receipt["core_stage_resource_path"], "Cycle-1 core resource receipt"
    )
    try:
        summary_raw = summary_path.read_bytes()
        resource_raw = resource_path.read_bytes()
    except OSError as exc:
        raise RFQStageError(f"Cycle-1 core binding read failed: {exc}") from exc
    if hashlib.sha256(summary_raw).hexdigest() != receipt["core_summary_sha256"]:
        raise RFQStageError("Cycle-1 core summary SHA binding mismatch")
    if hashlib.sha256(resource_raw).hexdigest() != receipt[
        "core_stage_resource_sha256"
    ]:
        raise RFQStageError("Cycle-1 core resource receipt SHA binding mismatch")
    summary = _read_captured_json(summary_raw, summary_path, "Cycle-1 core summary")
    resource = _read_captured_json(
        resource_raw, resource_path, "Cycle-1 core resource receipt"
    )
    if summary.get("run_id") != run_dir.name or summary.get("banner") != BANNER:
        raise RFQStageError("Cycle-1 core summary run identity mismatch")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "cycle1_core"
        or resource.get("return_code") != 0
        or not isinstance(resource.get("command"), list)
        or "--resume" in resource["command"]
    ):
        raise RFQStageError("Cycle-1 core resource receipt is invalid")
    command = resource["command"]
    if command.count("--run-dir") != 1:
        raise RFQStageError("Cycle-1 core resource run identity is missing")
    run_dir_index = command.index("--run-dir")
    if (
        run_dir_index + 1 >= len(command)
        or command[run_dir_index + 1] != str(run_dir)
    ):
        raise RFQStageError("Cycle-1 core resource run identity mismatch")

    if not database.is_file():
        raise RFQStageError("Cycle-1 DuckDB is missing or unsafe")
    if database.stat().st_size != receipt["bytes"]:
        raise RFQStageError("Cycle-1 DuckDB byte count does not match binding")
    if sha256(database) != receipt["sha256"]:
        raise RFQStageError("Cycle-1 DuckDB SHA-256 does not match binding")
    return cycle_binding


def _rfq_object_order(key: str) -> tuple[str, int, int, str]:
    match = re.fullmatch(
        r"raw_rfq/date=(\d{4}-\d{2}-\d{2})/rfq_(\d{2})\.ndjson(?:\.(\d+))?",
        key,
    )
    if not match:
        raise RFQStageError(f"quarantine-safe RFQ object ordering unavailable: {key}")
    return match.group(1), int(match.group(2)), int(match.group(3) or 0), key


def _rfq_message_shard_order(key: str) -> tuple[str, int, int, str] | None:
    """Return ordering only for recorder message shards, never receipt sidecars."""
    if not re.fullmatch(
        r"raw_rfq/date=\d{4}-\d{2}-\d{2}/rfq_\d{2}\.ndjson(?:\.\d+)?",
        key,
    ):
        return None
    return _rfq_object_order(key)


def _load_version_binding(run_dir: Path, selected: dict, key: str) -> dict:
    relative = selected.get("exact_version_list_path")
    if not isinstance(relative, str) or not relative.startswith(
        "DATA_INTEGRITY/version_ids/"
    ) or not relative.endswith(".jsonl"):
        raise RFQStageError("selected release has no safe exact version-list path")
    path = (run_dir / relative).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RFQStageError("exact version-list path escapes run directory") from exc
    if not path.is_file():
        raise RFQStageError(f"exact version-list missing: {relative}")
    found = None
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise RFQStageError(
                        f"blank exact version-list row: {relative}:{line_number}"
                    )
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise RFQStageError(
                        f"non-object exact version-list row: {relative}:{line_number}"
                    )
                row_key = row.get("key")
                if not isinstance(row_key, str) or row_key in seen:
                    raise RFQStageError(
                        f"invalid/duplicate exact version binding: {relative}:{line_number}"
                    )
                seen.add(row_key)
                if row_key == key:
                    found = row
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RFQStageError(f"invalid exact version list: {relative}: {exc}") from exc
    if found is None:
        raise RFQStageError(f"quarantined object absent from exact version list: {key}")
    return found


def _validate_repair_binding(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    declaration: dict,
    receipt_path: Path,
    declaration_path: Path,
) -> dict | None:
    """Bind a pre-failure declaration to either the current or registered repair code."""
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise RFQStageError("run repository binding missing")
    declared_commit = declaration.get("source_execution_commit")
    current_commit = repository.get("execution_commit")
    if not isinstance(declared_commit, str) or len(declared_commit) != 40:
        raise RFQStageError("quarantine declaration execution commit is invalid")
    if declared_commit == current_commit:
        return None

    repairs = manifest.get("data_integrity_repairs")
    if not isinstance(repairs, list):
        raise RFQStageError("quarantine declaration is not bound to current execution code")
    candidates = [
        row for row in repairs
        if isinstance(row, dict)
        and row.get("repair_id") == "repair-01"
        and row.get("finding") == "MALFORMED_NDJSON_OBJECT"
    ]
    if len(candidates) != 1:
        raise RFQStageError("exactly one RFQ structural repair registration is required")
    repair = candidates[0]
    repair02 = next(
        (
            row for row in repairs
            if isinstance(row, dict) and row.get("repair_id") == "repair-02"
        ),
        None,
    )
    repair01_current_commit = repair.get("current_execution_commit")
    repair01_current_source = repair.get("current_source_manifest_sha256")
    repair01_current_query = repair.get("current_query_set_sha256")
    required = {
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "previous_execution_commit": declared_commit,
        "current_execution_commit": repair01_current_commit,
        "current_source_manifest_sha256": repair01_current_source,
        "current_query_set_sha256": repair01_current_query,
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
        "declaration_path": REPAIR_DECLARATION_ARCHIVE,
        "declaration_sha256": sha256(declaration_path),
        "declaration_input_path": QUARANTINE_DECLARATION,
        "receipt_path": REPAIR_RECEIPT_ARCHIVE,
        "receipt_sha256": sha256(receipt_path),
        "receipt_input_path": MALFORMED_OBJECT_RECEIPT,
        "failed_state_path": FAILED_STATE_ARCHIVE,
        "failed_resource_receipt_path": FAILED_RESOURCE_ARCHIVE,
        "failed_scratch_receipt_path": FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_input_identity_path": FAILED_INPUT_IDENTITY_ARCHIVE,
    }
    for field, expected in required.items():
        if repair.get(field) != expected:
            raise RFQStageError(f"RFQ structural repair binding mismatch: {field}")
    if repair02 is None:
        if (
            current_commit != repair01_current_commit
            or repository.get("source_manifest_sha256") != repair01_current_source
            or repository.get("query_set_sha256") != repair01_current_query
        ):
            raise RFQStageError("repair-01 is not bound to current repository identity")
    elif (
        repair02.get("parent_repair_id") != "repair-01"
        or repair02.get("previous_execution_commit") != repair01_current_commit
        or repair02.get("previous_source_manifest_sha256") != repair01_current_source
        or repair02.get("previous_query_set_sha256") != repair01_current_query
        or repair02.get("current_execution_commit") != current_commit
        or repair02.get("current_source_manifest_sha256")
            != repository.get("source_manifest_sha256")
        or repair02.get("current_query_set_sha256")
            != repository.get("query_set_sha256")
    ):
        raise RFQStageError("repair-01/repair-02 repository append chain mismatch")
    if (
        repository.get("initial_execution_commit") != declared_commit
        or (
            repair02 is None
            and repository.get("previous_execution_commit") != declared_commit
        )
        or (
            repair02 is not None
            and repository.get("previous_execution_commit") != repair01_current_commit
        )
        or not isinstance(repair.get("previous_source_manifest_sha256"), str)
        or len(repair["previous_source_manifest_sha256"]) != 64
        or not isinstance(repair.get("previous_query_set_sha256"), str)
        or len(repair["previous_query_set_sha256"]) != 64
    ):
        raise RFQStageError("RFQ structural repair provenance is incomplete")
    if repair.get("quarantined_objects") != declaration.get("quarantined_objects"):
        raise RFQStageError("repair/declaration quarantined-object binding mismatch")
    archived_declaration = run_dir / REPAIR_DECLARATION_ARCHIVE
    archived_receipt = run_dir / REPAIR_RECEIPT_ARCHIVE
    failed_state_path = run_dir / FAILED_STATE_ARCHIVE
    failed_resource_path = run_dir / FAILED_RESOURCE_ARCHIVE
    failed_scratch_receipt_path = run_dir / FAILED_SCRATCH_RECEIPT_ARCHIVE
    failed_input_identity_path = run_dir / FAILED_INPUT_IDENTITY_ARCHIVE
    for label, path, expected_sha in (
        ("archived declaration", archived_declaration, repair["declaration_sha256"]),
        ("archived receipt", archived_receipt, repair["receipt_sha256"]),
        ("failed state", failed_state_path, repair.get("failed_state_sha256")),
        (
            "failed resource receipt",
            failed_resource_path,
            repair.get("failed_resource_receipt_sha256"),
        ),
        (
            "failed scratch receipt",
            failed_scratch_receipt_path,
            repair.get("failed_scratch_receipt_sha256"),
        ),
        (
            "failed input identity",
            failed_input_identity_path,
            repair.get("failed_input_identity_sha256"),
        ),
    ):
        if (
            not path.is_file()
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or sha256(path) != expected_sha
        ):
            raise RFQStageError(f"RFQ repair {label} SHA binding mismatch")
    if (
        sha256(archived_declaration) != sha256(declaration_path)
        or sha256(archived_receipt) != sha256(receipt_path)
    ):
        raise RFQStageError("active/archive RFQ repair evidence differs")
    failed_state = read_json_object(failed_state_path, "archived failed RFQ state")
    failed_resource = read_json_object(
        failed_resource_path, "archived failed RFQ resource receipt"
    )
    failed_scratch = read_json_object(
        failed_scratch_receipt_path, "archived failed RFQ scratch receipt"
    )
    failed_input_identity = read_json_object(
        failed_input_identity_path, "archived failed RFQ input identity"
    )
    declared_key = declaration["quarantined_objects"][0]["key"]
    if (
        failed_state.get("schema") != "rfq-full-stage-state-v1"
        or failed_state.get("status") != "FAILED_RESUMABLE"
        or failed_state.get("resume") is not False
        or declared_key not in str(failed_state.get("error", ""))
        or failed_resource.get("schema_version") != "w09-stage-resource-v1"
        or failed_resource.get("label") != "rfq_full_stage"
        or failed_resource.get("return_code") != 1
        or not isinstance(failed_resource.get("command"), list)
        or "--resume" in failed_resource["command"]
        or failed_scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or failed_scratch.get("run_id") != run_dir.name
        or not isinstance(failed_scratch.get("original_scratch_path"), str)
        or not failed_scratch["original_scratch_path"]
        or not isinstance(failed_scratch.get("preserved_scratch_path"), str)
        or not failed_scratch["preserved_scratch_path"]
        or failed_scratch["preserved_scratch_path"]
            == failed_scratch["original_scratch_path"]
        or failed_scratch["original_scratch_path"] != failed_state.get("scratch")
        or not isinstance(failed_scratch.get("sha256"), str)
        or len(failed_scratch["sha256"]) != 64
        or type(failed_scratch.get("bytes")) is not int
        or failed_scratch["bytes"] <= 0
        or not isinstance(failed_scratch.get("mtime_utc"), str)
        or not failed_scratch["mtime_utc"]
        or not isinstance(failed_scratch.get("input_fingerprint"), str)
        or len(failed_scratch["input_fingerprint"]) != 64
        or failed_scratch["input_fingerprint"] != failed_state.get("input_fingerprint")
        or failed_state.get("input_fingerprint")
            != inputs.get("path_size_fingerprint_sha256")
        or failed_scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or failed_scratch.get("resume_allowed") is not False
    ):
        raise RFQStageError("archived failed RFQ attempt is not structurally bound")
    if failed_input_identity != _legacy_full_input_identity(inputs):
        raise RFQStageError(
            "archived failed RFQ input identity is not the manifest-derived full set"
        )
    return {
        "repair_id": "repair-01",
        "failed_state_path": FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair["failed_state_sha256"],
        "failed_resource_receipt_path": FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair["failed_resource_receipt_sha256"],
        "failed_scratch_receipt_path": FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_scratch_receipt_sha256": repair["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": FAILED_INPUT_IDENTITY_ARCHIVE,
        "failed_input_identity_sha256": repair["failed_input_identity_sha256"],
    }


def _artifact_sha(run_dir: Path, relative: str, expected_sha: object, label: str) -> str:
    path = _require_run_relative_file(run_dir, relative, label)
    if (
        not isinstance(expected_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
        or sha256(path) != expected_sha
    ):
        raise RFQStageError(f"{label} SHA-256 binding mismatch")
    return expected_sha


def _archive_inventory(root: Path) -> list[dict]:
    if not root.is_dir() or root.is_symlink():
        raise RFQStageError(f"repair archive is missing or unsafe: {root}")
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RFQStageError(f"repair archive contains a symlink: {path}")
        if path.is_file():
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            })
    return rows


def _validate_repair02_receipt(receipt: dict, run_id: str) -> None:
    invalid_lines = receipt.get("invalid_lines")
    if (
        receipt.get("schema_version") != "rfq-malformed-object-receipt-v1"
        or receipt.get("run_id") != run_id
        or receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED"
        or receipt.get("raw_payload_redacted") is not True
        or receipt.get("quarantine_authorized") is not False
        or type(receipt.get("invalid_line_count")) is not int
        or receipt["invalid_line_count"] != 1
        or not isinstance(invalid_lines, list)
        or len(invalid_lines) != 1
        or type(receipt.get("total_lines")) is not int
        or receipt["total_lines"] < 1
        or receipt.get("expected_size") != receipt.get("observed_size")
        or receipt.get("expected_sha256") != receipt.get("observed_sha256")
        or receipt.get("final_line_newline_terminated") is not True
    ):
        raise RFQStageError("repair-02 malformed-object receipt is not exclusion-grade")
    finding = invalid_lines[0]
    if (
        not isinstance(finding, dict)
        or type(finding.get("line_number")) is not int
        or finding["line_number"] <= 0
        or not isinstance(finding.get("line_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", finding["line_sha256"]) is None
        or finding.get("error_type") != "JSONDecodeError"
    ):
        raise RFQStageError("repair-02 malformed line receipt is invalid")


def _apply_double_object_quarantine(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    repair02_registry_boundary: bytes | None = None,
    enforce_active_archive_pairs: bool = True,
) -> dict:
    """Apply the preregistered repair-02 append chain to the immutable full set."""
    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(repairs, list)
        or len(repairs) != 2
        or [row.get("repair_id") if isinstance(row, dict) else None for row in repairs]
            != ["repair-01", "repair-02"]
    ):
        raise RFQStageError("RFQ repair chain must be exactly repair-01 then repair-02")
    repair01, repair02 = repairs
    if (
        repair01.get("schema_version")
            != "sports-autoresearch-data-integrity-repair-v1"
        or repair02.get("schema_version")
            != "sports-autoresearch-data-integrity-repair-v2"
        or repair02.get("parent_repair_id") != "repair-01"
        or repair02.get("finding")
            != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or repair02.get("pre_repair_status")
            != "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED"
        or repair02.get("post_repair_status")
            != "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
        or manifest.get("status")
            != "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
    ):
        raise RFQStageError("RFQ repair-02 append-chain identity mismatch")

    # Revalidate repair-01 in the context of the append-only repository chain.
    first = apply_object_quarantine(run_dir, manifest, inputs, _ignore_repair02=True)
    if (
        first.get("quarantined_unique_objects") != 1
        or len(first.get("quarantine_details", [])) != 1
        or not isinstance(first.get("failed_attempt_binding"), dict)
    ):
        raise RFQStageError("repair-01 did not reproduce its exact partial selection")

    declaration_path = run_dir / QUARANTINE_DECLARATION_02
    receipt_path = run_dir / MALFORMED_OBJECT_RECEIPT_02
    auth_relative = "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
    auth_path = run_dir / auth_relative
    declaration = read_json_object(declaration_path, "repair-02 quarantine declaration")
    receipt = read_json_object(receipt_path, "repair-02 malformed-object receipt")
    authorization = read_json_object(auth_path, "repair-02 user authorization")
    declaration_fields = {
        "schema_version", "run_id", "mode", "finding", "disposition",
        "authority_basis", "created_at_utc",
        "created_after_structural_failure_before_rfq_result",
        "dependent_rfq_result_opened", "source_execution_commit",
        "parent_repair_id", "previous_declaration_path",
        "previous_declaration_sha256", "authorization_evidence_path",
        "authorization_evidence_sha256", "newly_quarantined_objects",
        "cumulative_quarantined_objects", "remaining_object_parse_policy",
        "selection_rule", "result_use_prohibited",
        "trial_disposition_if_repair_fails",
    }
    if set(declaration) != declaration_fields:
        raise RFQStageError("repair-02 quarantine declaration field set mismatch")
    if (
        declaration.get("schema_version") != "rfq-object-quarantine-v2"
        or declaration.get("run_id") != run_dir.name
        or declaration.get("mode") != "EXPLORATORY_AUTORESEARCH"
        or declaration.get("finding")
            != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or declaration.get("disposition") != "WHOLE_OBJECT_QUARANTINE"
        or declaration.get("authority_basis")
            != "EXPLICIT_OPERATOR_AUTHORIZATION_REPAIR_02"
        or declaration.get("created_after_structural_failure_before_rfq_result")
            is not True
        or declaration.get("dependent_rfq_result_opened") is not False
        or declaration.get("parent_repair_id") != "repair-01"
        or declaration.get("previous_declaration_path")
            != REPAIR_DECLARATION_ARCHIVE
        or declaration.get("previous_declaration_sha256")
            != repair01.get("declaration_sha256")
        or declaration.get("authorization_evidence_path") != auth_relative
        or declaration.get("remaining_object_parse_policy")
            != STRICT_REMAINING_PARSE_POLICY
        or "no line-level salvage" not in declaration.get("result_use_prohibited", "")
        or "ABORT_WITHOUT_RFQ_RESULT" not in declaration.get(
            "trial_disposition_if_repair_fails", ""
        )
    ):
        raise RFQStageError("repair-02 quarantine declaration policy mismatch")
    repository = manifest.get("repository", {})
    if (
        declaration.get("source_execution_commit")
            != repair02.get("previous_execution_commit")
        or repair02.get("current_execution_commit")
            != repository.get("execution_commit")
        or repair02.get("current_source_manifest_sha256")
            != repository.get("source_manifest_sha256")
        or repair02.get("current_query_set_sha256")
            != repository.get("query_set_sha256")
    ):
        raise RFQStageError("repair-02 repository identity chain mismatch")
    auth_fields = {
        "schema_version", "run_id", "repair_id", "authorized_at_utc",
        "authority_source", "authorized_action",
    }
    if (
        set(authorization) != auth_fields
        or authorization.get("schema_version")
            != "sports-autoresearch-repair-authorization-v1"
        or authorization.get("run_id") != run_dir.name
        or authorization.get("repair_id") != "repair-02"
        or authorization.get("authority_source") != "operator_chat_message"
        or authorization.get("authorized_action") != REPAIR02_AUTHORIZED_ACTION
        or declaration.get("authorization_evidence_sha256") != sha256(auth_path)
    ):
        raise RFQStageError("repair-02 user authorization binding mismatch")

    _validate_repair02_receipt(receipt, run_dir.name)
    newly = declaration.get("newly_quarantined_objects")
    cumulative = declaration.get("cumulative_quarantined_objects")
    if (
        not isinstance(newly, list) or len(newly) != 1
        or not isinstance(cumulative, list) or len(cumulative) != 2
        or cumulative != sorted(cumulative, key=lambda row: row.get("key", ""))
        or newly[0] not in cumulative
        or repair01.get("quarantined_objects") != [
            row for row in cumulative if row != newly[0]
        ]
        or repair02.get("newly_quarantined_objects") != newly
        or repair02.get("cumulative_quarantined_objects") != cumulative
    ):
        raise RFQStageError("repair-02 cumulative quarantine chain mismatch")
    second = newly[0]
    if (
        not isinstance(second, dict)
        or second.get("receipt") != MALFORMED_OBJECT_RECEIPT_02
        or second.get("invalid_line_count") != receipt.get("invalid_line_count")
        or second.get("release_id") != receipt.get("release_id")
        or second.get("key") != receipt.get("key")
        or second.get("size") != receipt.get("expected_size")
        or second.get("sha256") != receipt.get("expected_sha256")
        or second.get("version_id") != receipt.get("version_id")
        or second.get("manifest_sha256") != receipt.get("manifest_sha256")
        or not isinstance(second.get("reason"), str)
        or "no line-level salvage" not in second["reason"]
    ):
        raise RFQStageError("repair-02 declaration/receipt object identity mismatch")

    selected_by_release = {
        row.get("release_id"): row for row in manifest.get("selected_releases", [])
        if isinstance(row, dict)
    }
    full_by_key = {row["key"]: row for row in inputs["objects_detail"]}
    quarantine_rows = []
    detail_by_key = {row["key"]: row for row in first["quarantine_details"]}
    for declared in cumulative:
        key = declared.get("key")
        manifest_object = full_by_key.get(key)
        selected = selected_by_release.get(declared.get("release_id"))
        if (
            not isinstance(manifest_object, dict)
            or not isinstance(selected, dict)
            or manifest_object.get("size") != declared.get("size")
            or manifest_object.get("sha256") != declared.get("sha256")
            or manifest_object.get("bound_release_ids") != [declared.get("release_id")]
            or selected.get("manifest_sha256") != declared.get("manifest_sha256")
            or _rfq_message_shard_order(str(key)) is None
        ):
            raise RFQStageError(f"repair-02 manifest object binding mismatch: {key}")
        version = _load_version_binding(run_dir, selected, key)
        if any(
            version.get(field) != declared.get(field)
            for field in ("key", "size", "sha256", "version_id")
        ):
            raise RFQStageError(f"repair-02 VersionId binding mismatch: {key}")
        quarantine_rows.append(manifest_object)
        if key == second["key"]:
            detail_by_key[key] = {
                "release_id": declared["release_id"],
                "key": key,
                "size": declared["size"],
                "sha256": declared["sha256"],
                "version_id": declared["version_id"],
                "manifest_sha256": declared["manifest_sha256"],
                "invalid_line_count": declared["invalid_line_count"],
                "reason": declared["reason"],
                "receipt_sha256": sha256(receipt_path),
                "declaration_sha256": sha256(declaration_path),
            }
    quarantined_keys = [row["key"] for row in quarantine_rows]
    consumed = [
        row for row in inputs["objects_detail"] if row["key"] not in quarantined_keys
    ]
    if len(consumed) + len(quarantine_rows) != len(inputs["objects_detail"]):
        raise RFQStageError("repair-02 quarantine arithmetic is not an exact partition")

    ordered_messages = sorted(
        (
            row for row in inputs["objects_detail"]
            if _rfq_message_shard_order(row["key"]) is not None
        ),
        key=lambda row: _rfq_message_shard_order(row["key"]),
    )
    ordered_keys = [row["key"] for row in ordered_messages]
    indices = sorted(ordered_keys.index(key) for key in quarantined_keys)
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise RFQStageError("repair-02 objects are not one contiguous message-shard gap")
    previous = ordered_messages[indices[0] - 1] if indices[0] else None
    following = (
        ordered_messages[indices[-1] + 1]
        if indices[-1] + 1 < len(ordered_messages) else None
    )
    if (
        previous is None or following is None
        or previous["key"] in quarantined_keys
        or following["key"] in quarantined_keys
    ):
        raise RFQStageError("repair-02 contiguous gap lacks retained causal neighbors")

    retained_fingerprint = _object_fingerprint(consumed)
    quarantined_fingerprint = _object_fingerprint(quarantine_rows)
    selection_fingerprint = _repair02_selection_fingerprint(
        inputs["path_size_fingerprint_sha256"],
        consumed,
        quarantine_rows,
        sha256(declaration_path),
        [repair01["receipt_sha256"], sha256(receipt_path)],
    )
    counts = {
        "full_unique_objects": inputs["objects"],
        "full_logical_manifest_bindings": inputs["logical_manifest_bindings"],
        "full_unique_bytes": inputs["bytes"],
        "retained_unique_objects": len(consumed),
        "retained_logical_manifest_bindings": sum(
            len(row["bound_release_ids"]) for row in consumed
        ),
        "retained_bytes": sum(row["size"] for row in consumed),
        "quarantined_unique_objects": len(quarantine_rows),
        "quarantined_logical_manifest_bindings": sum(
            len(row["bound_release_ids"]) for row in quarantine_rows
        ),
        "quarantined_bytes": sum(row["size"] for row in quarantine_rows),
    }
    expected_counts = {
        "full_unique_objects": REPAIR02_EXPECTED_TOTAL_UNIQUE_OBJECTS,
        "full_logical_manifest_bindings": REPAIR02_EXPECTED_TOTAL_LOGICAL_BINDINGS,
        "full_unique_bytes": REPAIR02_EXPECTED_TOTAL_BYTES,
        "retained_unique_objects": REPAIR02_EXPECTED_RETAINED_UNIQUE_OBJECTS,
        "retained_logical_manifest_bindings": REPAIR02_EXPECTED_RETAINED_LOGICAL_BINDINGS,
        "retained_bytes": REPAIR02_EXPECTED_RETAINED_BYTES,
        "quarantined_unique_objects": 2,
        "quarantined_logical_manifest_bindings": 2,
        "quarantined_bytes": REPAIR02_EXPECTED_QUARANTINED_BYTES,
    }
    # Synthetic unit fixtures exercise the same arithmetic; the authorized production run
    # additionally must reproduce the exact operator-approved counts.
    if run_dir.name == "20260715T112538Z__c21a79a8cff__deep01" and counts != expected_counts:
        raise RFQStageError("repair-02 authorized production coverage arithmetic mismatch")

    coverage = repair02.get("coverage")
    if not isinstance(coverage, dict):
        raise RFQStageError("repair-02 registered coverage is missing")
    coverage_required = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": counts["full_unique_objects"],
        "full_logical_manifest_bindings": counts["full_logical_manifest_bindings"],
        "full_unique_bytes": counts["full_unique_bytes"],
        "retained_unique_objects": counts["retained_unique_objects"],
        "retained_logical_manifest_bindings": counts[
            "retained_logical_manifest_bindings"
        ],
        "retained_bytes": counts["retained_bytes"],
        "quarantined_unique_objects": counts["quarantined_unique_objects"],
        "quarantined_logical_manifest_bindings": counts[
            "quarantined_logical_manifest_bindings"
        ],
        "quarantined_bytes": counts["quarantined_bytes"],
        "full_object_set_sha256": inputs["path_size_fingerprint_sha256"],
        "retained_object_set_sha256": retained_fingerprint,
        "quarantined_object_set_sha256": quarantined_fingerprint,
        "retained_selection_fingerprint_sha256": selection_fingerprint,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    if set(coverage) != set(coverage_required):
        raise RFQStageError("repair-02 registered coverage field set mismatch")
    for field, expected in coverage_required.items():
        if coverage.get(field) != expected:
            raise RFQStageError(f"repair-02 registered coverage mismatch: {field}")

    repair_chain, failed_bindings = _validate_repair02_archives(
        run_dir, manifest, repair01, repair02, declaration, receipt, authorization,
        inputs, first, selection_fingerprint, cumulative,
        repair02_registry_boundary, enforce_active_archive_pairs,
    )
    result = dict(inputs)
    result.update({
        "paths": [row["path"] for row in consumed],
        "objects_detail": consumed,
        "coverage_status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_object_coverage": False,
        "unique_objects_total": inputs["objects"],
        "unique_bytes_total": inputs["bytes"],
        "consumed_unique_objects": len(consumed),
        "consumed_logical_bindings": counts["retained_logical_manifest_bindings"],
        "consumed_bytes": counts["retained_bytes"],
        "quarantined_unique_objects": 2,
        "quarantined_logical_bindings": 2,
        "quarantined_bytes": counts["quarantined_bytes"],
        "quarantined_object_set_sha256": quarantined_fingerprint,
        "quarantine_reasons": [detail_by_key[key]["reason"] for key in sorted(detail_by_key)],
        "quarantine_details": [detail_by_key[key] for key in sorted(detail_by_key)],
        "quarantine_boundaries": [{
            "release_id": quarantine_rows[0]["bound_release_ids"][0],
            "key": sorted(quarantined_keys)[0],
            "sha256": full_by_key[sorted(quarantined_keys)[0]]["sha256"],
            "quarantined_keys": sorted(quarantined_keys),
            "quarantined_object_set_sha256": quarantined_fingerprint,
            "previous_key": previous["key"],
            "next_key": following["key"],
            "previous_path": previous["path"],
            "next_path": following["path"],
        }],
        "consumed_object_set_sha256": retained_fingerprint,
        "selection_fingerprint_sha256": selection_fingerprint,
        "failed_attempt_binding": None,
        "failed_attempt_bindings": failed_bindings,
        "repair_chain": repair_chain,
        "expected_success_resource": {
            "label": REPAIR02_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR02_SUCCESS_RESOURCE_PATH,
        },
    })
    return result


def _validate_repair02_archives(
    run_dir: Path,
    manifest: dict,
    repair01: dict,
    repair02: dict,
    declaration: dict,
    receipt: dict,
    authorization: dict,
    inputs: dict,
    first: dict,
    selection_fingerprint: str,
    cumulative: Sequence[dict],
    registry_boundary: bytes | None = None,
    enforce_active_archive_pairs: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Validate every repair-02 prerequisite before any result or scratch mutation."""
    declaration_sha = sha256(run_dir / QUARANTINE_DECLARATION_02)
    receipt_sha = sha256(run_dir / MALFORMED_OBJECT_RECEIPT_02)
    auth_sha = sha256(run_dir / declaration["authorization_evidence_path"])
    flat_evidence = {
        "declaration_path": (REPAIR02_DECLARATION_ARCHIVE, declaration_sha),
        "receipt_path": (REPAIR02_RECEIPT_ARCHIVE, receipt_sha),
        "authorization_evidence_path": (REPAIR02_AUTHORIZATION_ARCHIVE, auth_sha),
        "failed_state_path": (
            REPAIR02_FAILED_STATE_ARCHIVE,
            repair02.get("failed_state_sha256"),
        ),
        "failed_resource_receipt_path": (
            REPAIR02_FAILED_RESOURCE_ARCHIVE,
            repair02.get("failed_resource_receipt_sha256"),
        ),
        "failed_scratch_receipt_path": (
            REPAIR02_FAILED_SCRATCH_ARCHIVE,
            repair02.get("failed_scratch_receipt_sha256"),
        ),
        "failed_input_identity_path": (
            REPAIR02_FAILED_INPUT_ARCHIVE,
            repair02.get("failed_input_identity_sha256"),
        ),
        "structural_audit_path": (
            REPAIR02_STRUCTURAL_AUDIT_ARCHIVE,
            repair02.get("structural_audit_sha256"),
        ),
        "structural_audit_resource_path": (
            REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE,
            repair02.get("structural_audit_resource_sha256"),
        ),
        "blocker_evidence_path": (
            REPAIR02_BLOCKER_ARCHIVE,
            repair02.get("blocker_evidence_sha256"),
        ),
        "session_resume_evidence_path": (
            REPAIR02_SESSION_RESUME_ARCHIVE,
            repair02.get("session_resume_evidence_sha256"),
        ),
    }
    for field, (relative, expected_sha) in flat_evidence.items():
        if repair02.get(field) != relative:
            raise RFQStageError(f"repair-02 evidence path mismatch: {field}")
        _artifact_sha(run_dir, relative, expected_sha, f"repair-02 {field}")

    active_archive_pairs = (
        (QUARANTINE_DECLARATION_02, REPAIR02_DECLARATION_ARCHIVE, "declaration"),
        (MALFORMED_OBJECT_RECEIPT_02, REPAIR02_RECEIPT_ARCHIVE, "receipt"),
        (declaration["authorization_evidence_path"], REPAIR02_AUTHORIZATION_ARCHIVE,
         "authorization"),
        ("REPORT/tables/RFQ_FULL_STAGE_STATE.json", REPAIR02_FAILED_STATE_ARCHIVE,
         "failed state"),
        ("logs/resources/rfq_full_stage_repair01.json",
         REPAIR02_FAILED_RESOURCE_ARCHIVE, "failed resource"),
        ("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json",
         REPAIR02_FAILED_SCRATCH_ARCHIVE, "failed scratch receipt"),
        ("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
         REPAIR02_FAILED_INPUT_ARCHIVE, "failed input identity"),
        ("DATA_INTEGRITY/RFQ_STRUCTURAL_INTEGRITY_AUDIT_02.json",
         REPAIR02_STRUCTURAL_AUDIT_ARCHIVE, "structural audit"),
        ("logs/resources/rfq_structural_integrity_audit02.json",
         REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE, "structural audit resource"),
        ("DATA_INTEGRITY/RFQ_SECOND_MALFORMED_OBJECT_BLOCKER.json",
         REPAIR02_BLOCKER_ARCHIVE, "blocker evidence"),
        ("DATA_INTEGRITY/SESSION_RESUME_02.json",
         REPAIR02_SESSION_RESUME_ARCHIVE, "session-resume evidence"),
    )
    if enforce_active_archive_pairs:
        for active_rel, archive_rel, label in active_archive_pairs:
            active = _require_run_relative_file(run_dir, active_rel, f"active {label}")
            archived = _require_run_relative_file(run_dir, archive_rel, f"archived {label}")
            if active.read_bytes() != archived.read_bytes():
                raise RFQStageError(f"repair-02 active/archive {label} bytes differ")

    if (
        read_json_object(run_dir / REPAIR02_DECLARATION_ARCHIVE, "archived declaration")
            != declaration
        or read_json_object(run_dir / REPAIR02_RECEIPT_ARCHIVE, "archived receipt")
            != receipt
        or read_json_object(run_dir / REPAIR02_AUTHORIZATION_ARCHIVE,
                            "archived authorization") != authorization
    ):
        raise RFQStageError("repair-02 active/archive parsed evidence differs")

    session = repair02.get("session_resume")
    session_records = manifest.get("session_resumes")
    session_evidence = read_json_object(
        run_dir / REPAIR02_SESSION_RESUME_ARCHIVE,
        "repair-02 archived session-resume evidence",
    )
    if (
        not isinstance(session, dict)
        or set(session) != {
            "manifest_record", "evidence_path", "evidence_sha256",
            "preserved_in_run_manifest",
        }
        or not isinstance(session_records, list) or len(session_records) != 1
        or session.get("manifest_record") != session_records[0]
        or session.get("evidence_path") != REPAIR02_SESSION_RESUME_ARCHIVE
        or session.get("evidence_sha256")
            != repair02.get("session_resume_evidence_sha256")
        or session.get("preserved_in_run_manifest") is not True
        or session_evidence.get("schema_version")
            != "sports-autoresearch-session-resume-v1"
        or session_evidence.get("run_id") != run_dir.name
        or session_evidence.get("session_id")
            != session_records[0].get("session_id")
        or session_records[0].get("evidence_path")
            != "DATA_INTEGRITY/SESSION_RESUME_02.json"
        or session_records[0].get("evidence_sha256")
            != repair02.get("session_resume_evidence_sha256")
    ):
        raise RFQStageError("repair-02 session-resume append chain mismatch")

    repair01_path = _require_run_relative_file(
        run_dir, REPAIR01_REGISTRATION, "repair-01 registration"
    )
    repair01_file_sha = sha256(repair01_path)
    repair01_file = read_json_object(repair01_path, "repair-01 registration")
    repair01_without_receipt = {
        key: value for key, value in repair01.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair01_file != repair01_without_receipt
        or repair01.get("repair_receipt_path") != REPAIR01_REGISTRATION
        or repair01.get("repair_receipt_sha256") != repair01_file_sha
        or repair02.get("previous_repair_registration_path")
            != REPAIR01_REGISTRATION
        or repair02.get("previous_repair_registration_sha256")
            != repair01_file_sha
        or repair02.get("previous_repair_record_sha256")
            != _json_payload_sha256(repair01)
    ):
        raise RFQStageError("repair-01/repair-02 registration receipt chain mismatch")

    repair02_path = _require_run_relative_file(
        run_dir, REPAIR02_REGISTRATION, "repair-02 registration"
    )
    repair02_file_sha = sha256(repair02_path)
    repair02_file = read_json_object(repair02_path, "repair-02 registration")
    repair02_without_receipt = {
        key: value for key, value in repair02.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair02_file != repair02_without_receipt
        or repair02.get("repair_receipt_path") != REPAIR02_REGISTRATION
        or repair02.get("repair_receipt_sha256") != repair02_file_sha
    ):
        raise RFQStageError("repair-02 registration receipt mismatch")

    state = read_json_object(
        run_dir / REPAIR02_FAILED_STATE_ARCHIVE, "repair-02 archived failed state"
    )
    resource = read_json_object(
        run_dir / REPAIR02_FAILED_RESOURCE_ARCHIVE,
        "repair-02 archived failed resource receipt",
    )
    scratch = read_json_object(
        run_dir / REPAIR02_FAILED_SCRATCH_ARCHIVE,
        "repair-02 archived failed scratch receipt",
    )
    failed_input = read_json_object(
        run_dir / REPAIR02_FAILED_INPUT_ARCHIVE,
        "repair-02 archived failed input identity",
    )
    second_key = declaration["newly_quarantined_objects"][0]["key"]
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("status") != "FAILED_RESUMABLE"
        or state.get("resume") is not False
        or state.get("input_fingerprint") != first["selection_fingerprint_sha256"]
        or second_key not in str(state.get("error", ""))
        or resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair01"
        or resource.get("return_code") != 1
        or not isinstance(resource.get("command"), list)
        or "--resume" in resource["command"]
        or scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or scratch.get("run_id") != run_dir.name
        or scratch.get("input_fingerprint") != state.get("input_fingerprint")
        or scratch.get("original_scratch_path") != state.get("scratch")
        or scratch.get("preserved_scratch_path") == state.get("scratch")
        or scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch.get("resume_allowed") is not False
        or type(scratch.get("bytes")) is not int or scratch["bytes"] <= 0
        or not isinstance(scratch.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", scratch["sha256"]) is None
    ):
        raise RFQStageError("repair-02 failed attempt is not structurally bound")
    if (
        failed_input.get("schema") != "rfq-full-input-identity-v2"
        or failed_input.get("run_id") != run_dir.name
        or failed_input.get("unique_objects_total") != inputs["objects"]
        or failed_input.get("unique_bytes_total") != inputs["bytes"]
        or failed_input.get("consumed_unique_objects")
            != first["consumed_unique_objects"]
        or failed_input.get("consumed_bytes") != first["consumed_bytes"]
        or failed_input.get("quarantined_unique_objects") != 1
        or failed_input.get("selection_fingerprint_sha256")
            != first["selection_fingerprint_sha256"]
        or failed_input.get("quarantine_details") != first["quarantine_details"]
        or failed_input.get("line_salvage") is not False
    ):
        raise RFQStageError("repair-02 archived failed input identity mismatch")

    audit = read_json_object(
        run_dir / REPAIR02_STRUCTURAL_AUDIT_ARCHIVE,
        "repair-02 structural integrity audit",
    )
    audit_resource = read_json_object(
        run_dir / REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE,
        "repair-02 structural audit resource receipt",
    )
    audit_invalid = audit.get("invalid_objects")
    if (
        audit.get("schema_version") != "rfq-structural-integrity-audit-v1"
        or audit.get("run_id") != run_dir.name
        or audit.get("status") != "COMPLETE_STRUCTURAL_AUDIT"
        or audit.get("scope") != "OUTER_NDJSON_STRUCTURE_ONLY_NO_RESEARCH_RESULT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("objects_scanned") != inputs["objects"]
        or audit.get("bytes_scanned") != inputs["bytes"]
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("invalid_object_count") != 2
        or audit.get("invalid_line_count") != 2
        or audit.get("raw_payload_redacted") is not True
        or audit.get("input_fingerprint") != inputs["path_size_fingerprint_sha256"]
        or audit.get("input_identity_path") != FAILED_INPUT_IDENTITY_ARCHIVE
        or audit.get("input_identity_sha256")
            != repair01.get("failed_input_identity_sha256")
        or not isinstance(audit_invalid, list) or len(audit_invalid) != 2
        or audit_resource.get("schema_version") != "w09-stage-resource-v1"
        or audit_resource.get("label") != "rfq_structural_integrity_audit02"
        or audit_resource.get("return_code") != 0
    ):
        raise RFQStageError("repair-02 complete structural audit binding mismatch")
    declared_by_key = {row["key"]: row for row in cumulative}
    if {row.get("key") for row in audit_invalid} != set(declared_by_key):
        raise RFQStageError("repair-02 audit invalid-object set mismatch")
    for row in audit_invalid:
        declared = declared_by_key[row["key"]]
        if (
            row.get("identity_match") is not True
            or row.get("raw_payload_redacted") is not True
            or row.get("expected_size") != declared["size"]
            or row.get("observed_size") != declared["size"]
            or row.get("expected_sha256") != declared["sha256"]
            or row.get("observed_sha256") != declared["sha256"]
            or row.get("invalid_line_count") != declared["invalid_line_count"]
            or not isinstance(row.get("invalid_lines"), list)
            or len(row["invalid_lines"]) != declared["invalid_line_count"]
        ):
            raise RFQStageError(f"repair-02 audit object mismatch: {row.get('key')}")

    blocker = read_json_object(
        run_dir / REPAIR02_BLOCKER_ARCHIVE, "repair-02 blocker evidence"
    )
    complete_audit = blocker.get("complete_structural_audit")
    if (
        blocker.get("schema_version") != "rfq-data-integrity-blocker-v1"
        or blocker.get("run_id") != run_dir.name
        or blocker.get("status") != "DATA_INTEGRITY_BLOCKER"
        or blocker.get("blocker")
            != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or blocker.get("analysis_result_opened") is not False
        or blocker.get("automatic_additional_quarantine_prohibited") is not True
        or blocker.get("next_required_authority")
            != "EXPLICIT_REPAIR_02_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or blocker.get("failed_state_sha256")
            != repair02.get("failed_state_sha256")
        or blocker.get("resource_receipt_sha256")
            != repair02.get("failed_resource_receipt_sha256")
        or blocker.get("preserved_scratch_receipt_sha256")
            != repair02.get("failed_scratch_receipt_sha256")
        or blocker.get("malformed_object_receipt_sha256") != receipt_sha
        or not isinstance(complete_audit, dict)
        or complete_audit.get("sha256") != repair02.get("structural_audit_sha256")
        or complete_audit.get("resource_sha256")
            != repair02.get("structural_audit_resource_sha256")
        or complete_audit.get("objects_scanned") != inputs["objects"]
        or complete_audit.get("bytes_scanned") != inputs["bytes"]
        or complete_audit.get("invalid_object_count") != 2
    ):
        raise RFQStageError("repair-02 blocker evidence binding mismatch")

    failed_record = repair02.get("failed_attempt")
    structural_record = repair02.get("structural_audit")
    blocker_record = repair02.get("blocker_evidence")
    authorization_record = repair02.get("authorization_evidence")
    expected_failed_record = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02",
        "state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "state_path": REPAIR02_FAILED_STATE_ARCHIVE,
        "state_sha256": repair02.get("failed_state_sha256"),
        "state_status": "FAILED_RESUMABLE",
        "resource_active_path": "logs/resources/rfq_full_stage_repair01.json",
        "resource_path": REPAIR02_FAILED_RESOURCE_ARCHIVE,
        "resource_sha256": repair02.get("failed_resource_receipt_sha256"),
        "resource_label": "rfq_full_stage_repair01",
        "return_code": 1,
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json"
        ),
        "scratch_receipt_path": REPAIR02_FAILED_SCRATCH_ARCHIVE,
        "scratch_receipt_sha256": repair02.get("failed_scratch_receipt_sha256"),
        "input_identity_active_path": "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "input_identity_path": REPAIR02_FAILED_INPUT_ARCHIVE,
        "input_identity_sha256": repair02.get("failed_input_identity_sha256"),
        "input_fingerprint": first["selection_fingerprint_sha256"],
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    }
    expected_structural_record = {
        "path": REPAIR02_STRUCTURAL_AUDIT_ARCHIVE,
        "sha256": repair02.get("structural_audit_sha256"),
        "resource_path": REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE,
        "resource_sha256": repair02.get("structural_audit_resource_sha256"),
        "objects_scanned": inputs["objects"],
        "bytes_scanned": inputs["bytes"],
        "lines_scanned": audit.get("lines_scanned"),
        "invalid_object_count": 2,
        "invalid_line_count": 2,
        "identity_mismatch_count": 0,
        "analysis_result_opened": False,
    }
    if (
        failed_record != expected_failed_record
        or structural_record != expected_structural_record
        or blocker_record != {
            "path": REPAIR02_BLOCKER_ARCHIVE,
            "sha256": repair02.get("blocker_evidence_sha256"),
            "status": "DATA_INTEGRITY_BLOCKER",
        }
        or authorization_record != {
            "path": REPAIR02_AUTHORIZATION_ARCHIVE,
            "sha256": auth_sha,
            "schema_version": "sports-autoresearch-repair-authorization-v1",
            "authority_source": "operator_chat_message",
        }
        or repair02.get("cycle1_duckdb_binding")
            != repair01.get("cycle1_duckdb_binding")
    ):
        raise RFQStageError("repair-02 nested evidence record mismatch")

    archive_root = run_dir / REPAIR02_PRE_ROOT
    if (
        repair02.get("archive_path") != REPAIR02_PRE_ROOT
        or repair02.get("archive_inventory") != _archive_inventory(archive_root)
    ):
        raise RFQStageError("repair-02 archive inventory mismatch")
    core_inventory = repair02.get("core_result_artifacts")
    if not isinstance(core_inventory, list) or not core_inventory:
        raise RFQStageError("repair-02 core-result inventory is missing")
    for item in core_inventory:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "bytes", "sha256"}
            or type(item.get("bytes")) is not int
        ):
            raise RFQStageError("repair-02 core-result inventory row is invalid")
        path = _require_run_relative_file(
            run_dir, item["path"], "repair-02 preserved core result"
        )
        if path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise RFQStageError("repair-02 preserved core result changed")

    repository = manifest.get("repository")
    identity_chain = repair02.get("repository_identity_chain")
    if (
        not isinstance(repository, dict)
        or not isinstance(identity_chain, list) or len(identity_chain) != 3
        or repository.get("identity_history") != identity_chain
        or repair02.get("initial_repository_identity") != identity_chain[0]
        or repair02.get("previous_repository_identity") != identity_chain[1]
        or repair02.get("current_repository_identity") != identity_chain[2]
        or identity_chain[1].get("execution_commit")
            != repair01.get("current_execution_commit")
        or identity_chain[2].get("execution_commit")
            != repository.get("execution_commit")
    ):
        raise RFQStageError("repair-02 repository identity history mismatch")
    registry_path = _require_run_relative_file(
        run_dir, "TRIAL_REGISTRY.jsonl", "active trial registry"
    )
    active_registry = registry_path.read_bytes()
    registry = active_registry if registry_boundary is None else registry_boundary
    if registry_boundary is not None and not active_registry.startswith(registry_boundary):
        raise RFQStageError("repair-02 registry boundary is not an active strict prefix")
    trial = repair02.get("trial_registry")
    archived_registry = _require_run_relative_file(
        run_dir, f"{REPAIR02_PRE_ROOT}/TRIAL_REGISTRY.jsonl",
        "repair-02 prior trial registry",
    ).read_bytes()
    if (
        not isinstance(trial, dict)
        or trial.get("trial_registration_ids")
            != ["RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02"]
        or trial.get("appended_records") != 2
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("previous_bytes") != len(archived_registry)
        or trial.get("previous_sha256")
            != hashlib.sha256(archived_registry).hexdigest()
        or trial.get("current_bytes") != len(registry)
        or trial.get("current_sha256") != hashlib.sha256(registry).hexdigest()
        or not registry.startswith(archived_registry)
    ):
        raise RFQStageError("repair-02 trial-registry append boundary mismatch")

    if (
        repair02.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair02.get("quarantine_policy")
            != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE"
        or repair02.get("core_result_disposition")
            != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair02.get("core_results_recomputed") is not False
        or repair02.get("registration_change_class")
            != "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        or repair02.get("hypothesis_design_change") != "NONE"
        or repair02.get("threshold_feature_test_or_hypothesis_status_changed")
            is not False
    ):
        raise RFQStageError("repair-02 governance boundary mismatch")

    failed01 = dict(first["failed_attempt_binding"])
    failed01.update({
        "attempt_id": "RFQ_FULL_STAGE_ATTEMPT_01",
        "input_fingerprint": inputs["path_size_fingerprint_sha256"],
        "resource_label": "rfq_full_stage",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    })
    failed02 = {
        "repair_id": "repair-02",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02",
        "failed_state_path": REPAIR02_FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair02["failed_state_sha256"],
        "failed_resource_receipt_path": REPAIR02_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair02[
            "failed_resource_receipt_sha256"
        ],
        "failed_scratch_receipt_path": REPAIR02_FAILED_SCRATCH_ARCHIVE,
        "failed_scratch_receipt_sha256": repair02[
            "failed_scratch_receipt_sha256"
        ],
        "failed_input_identity_path": REPAIR02_FAILED_INPUT_ARCHIVE,
        "failed_input_identity_sha256": repair02["failed_input_identity_sha256"],
        "input_fingerprint": state["input_fingerprint"],
        "resource_label": "rfq_full_stage_repair01",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    }
    repair_chain = [
        {
            "repair_id": "repair-01",
            "registration_path": REPAIR01_REGISTRATION,
            "registration_sha256": repair01_file_sha,
            "declaration_path": REPAIR_DECLARATION_ARCHIVE,
            "declaration_sha256": repair01["declaration_sha256"],
            "receipt_path": REPAIR_RECEIPT_ARCHIVE,
            "receipt_sha256": repair01["receipt_sha256"],
        },
        {
            "repair_id": "repair-02",
            "registration_path": REPAIR02_REGISTRATION,
            "registration_sha256": repair02_file_sha,
            "declaration_path": REPAIR02_DECLARATION_ARCHIVE,
            "declaration_sha256": declaration_sha,
            "receipt_path": REPAIR02_RECEIPT_ARCHIVE,
            "receipt_sha256": receipt_sha,
            "authorization_path": REPAIR02_AUTHORIZATION_ARCHIVE,
            "authorization_sha256": auth_sha,
            "structural_audit_path": REPAIR02_STRUCTURAL_AUDIT_ARCHIVE,
            "structural_audit_sha256": repair02["structural_audit_sha256"],
            "structural_audit_resource_path": REPAIR02_STRUCTURAL_RESOURCE_ARCHIVE,
            "structural_audit_resource_sha256": repair02[
                "structural_audit_resource_sha256"
            ],
            "blocker_evidence_path": REPAIR02_BLOCKER_ARCHIVE,
            "blocker_evidence_sha256": repair02["blocker_evidence_sha256"],
            "session_resume_evidence_path": REPAIR02_SESSION_RESUME_ARCHIVE,
            "session_resume_evidence_sha256": repair02[
                "session_resume_evidence_sha256"
            ],
        },
    ]
    return repair_chain, [failed01, failed02]


def _repair03_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise RFQStageError(f"{label} must be a nonnegative integer")
    return value


def _validate_repair03_inner_payload_audit(
    audit: dict,
    *,
    run_id: str,
    active_input: dict,
    active_input_sha256: str,
    audit_source_sha256: str,
    base_result: dict,
) -> dict[str, int]:
    """Reconcile every approved audit object with the frozen active input.

    The fixed evidence hashes are checked by the caller before this semantic
    validation.  Consequently a coordinated rewrite of the input identity,
    object summaries, aggregate totals, and registration receipt cannot create
    an alternative parser-contract authority.
    """
    expected_fields = {
        "analysis_result_opened", "audit_script_path", "audit_script_sha256",
        "bytes_scanned", "completed_at_utc", "data_frame_rows",
        "expected_empty_control_marker_allowlist",
        "expected_empty_control_marker_rows", "identity_mismatch_count",
        "identity_mismatches", "input_fingerprint", "input_identity_path",
        "input_identity_sha256", "json_object_payload_marker_contract",
        "lines_scanned", "marker_counts", "object_summaries",
        "objects_scanned", "outer_invalid_line_count",
        "outer_invalid_object_count", "parser_contract",
        "parser_contract_sha256", "raw_payload_redacted", "run_id",
        "schema_version", "scope", "segment_receipt_rows", "started_at_utc",
        "status", "unexpected_inner_payload_object_count",
        "unexpected_inner_payload_objects", "unexpected_inner_payload_row_count",
        "wall_seconds", "workers",
    }
    audited = audit.get("parser_contract")
    audited_sha = _compact_json_sha256(audited)
    if (
        set(audit) != expected_fields
        or audit.get("schema_version") != "rfq-inner-payload-contract-audit-v1"
        or audit.get("run_id") != run_id
        or audit.get("status") != "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT"
        or audit.get("scope")
            != "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("raw_payload_redacted") is not True
        or audit.get("input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or audit.get("input_identity_path")
            != "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
        or audit.get("input_identity_sha256") != active_input_sha256
        or audit.get("expected_empty_control_marker_allowlist")
            != list(EXPECTED_BLANK_CONTROL_MARKERS)
        or audit.get("objects_scanned") != REPAIR03_EXPECTED_RETAINED_OBJECTS
        or audit.get("objects_scanned") != base_result["consumed_unique_objects"]
        or audit.get("bytes_scanned") != REPAIR03_EXPECTED_RETAINED_BYTES
        or audit.get("bytes_scanned") != base_result["consumed_bytes"]
        or audit.get("lines_scanned") != REPAIR03_EXPECTED_LINES
        or audit.get("data_frame_rows") != REPAIR03_EXPECTED_DATA_FRAMES
        or audit.get("segment_receipt_rows") != REPAIR03_EXPECTED_SEGMENT_RECEIPTS
        or audit.get("expected_empty_control_marker_rows")
            != REPAIR03_EXPECTED_BLANK_CONTROLS
        or audit.get("marker_counts") != REPAIR03_EXPECTED_MARKER_COUNTS
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("outer_invalid_object_count") != 0
        or audit.get("outer_invalid_line_count") != 0
        or audit.get("unexpected_inner_payload_object_count") != 0
        or audit.get("unexpected_inner_payload_row_count") != 0
        or audit.get("unexpected_inner_payload_objects") != []
        or audit.get("json_object_payload_marker_contract")
            != ["<marker field absent>", "segment_receipt"]
        or audited != REPAIR03_AUDITED_PARSER_CONTRACT
        or audit.get("parser_contract_sha256")
            != REPAIR03_APPROVED_AUDITED_PARSER_CONTRACT_SHA256
        or audited_sha != REPAIR03_APPROVED_AUDITED_PARSER_CONTRACT_SHA256
        or audit.get("audit_script_path")
            != "tmp/rfq_inner_payload_contract_audit03.py"
        or audit.get("audit_script_sha256") != audit_source_sha256
        or audit.get("workers") != 8
        or not isinstance(audit.get("wall_seconds"), (int, float))
        or isinstance(audit.get("wall_seconds"), bool)
        or audit.get("wall_seconds") <= 0
    ):
        raise RFQStageError(
            "repair-03 audit does not prove the exact approved retained set"
        )
    for field in ("started_at_utc", "completed_at_utc"):
        value = audit.get(field)
        try:
            parsed = dt.datetime.fromisoformat(
                value[:-1] + "+00:00"
                if isinstance(value, str) and value.endswith("Z") else str(value)
            )
        except ValueError as exc:
            raise RFQStageError(f"repair-03 audit {field} is invalid") from exc
        if parsed.utcoffset() != dt.timedelta(0):
            raise RFQStageError(f"repair-03 audit {field} is not UTC")

    consumed = active_input.get("consumed_objects")
    if (
        not isinstance(consumed, list)
        or len(consumed) != REPAIR03_EXPECTED_RETAINED_OBJECTS
    ):
        raise RFQStageError("repair-03 active input object set is incomplete")
    expected_objects: dict[str, tuple[int, str]] = {}
    for index, row in enumerate(consumed):
        if not isinstance(row, dict):
            raise RFQStageError(f"repair-03 active input object {index} is invalid")
        key = row.get("key")
        size = row.get("size")
        digest = row.get("sha256")
        if (
            not isinstance(key, str)
            or not key.startswith("raw_rfq/")
            or key in expected_objects
            or type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise RFQStageError(f"repair-03 active input object {index} is invalid")
        expected_objects[key] = (size, digest)

    summaries = audit.get("object_summaries")
    if (
        not isinstance(summaries, list)
        or len(summaries) != REPAIR03_EXPECTED_RETAINED_OBJECTS
    ):
        raise RFQStageError("repair-03 audit object summaries are incomplete")
    observed_objects: dict[str, tuple[int, str]] = {}
    total_bytes = 0
    total_lines = 0
    total_controls = 0
    total_frames = 0
    total_receipts = 0
    marker_counts: dict[str, int] = {}
    for index, row in enumerate(summaries):
        if not isinstance(row, dict):
            raise RFQStageError(f"repair-03 audit object summary {index} is invalid")
        key = row.get("key")
        expected_size = row.get("expected_size")
        expected_sha = row.get("expected_sha256")
        lines = row.get("total_lines")
        if (
            not isinstance(key, str)
            or not key.startswith("raw_rfq/")
            or key in observed_objects
            or type(expected_size) is not int
            or expected_size <= 0
            or type(lines) is not int
            or lines <= 0
            or not isinstance(expected_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            or expected_objects.get(key) != (expected_size, expected_sha)
            or row.get("observed_size") != expected_size
            or row.get("observed_sha256") != expected_sha
            or row.get("identity_match") is not True
            or row.get("outer_invalid_rows") != 0
            or row.get("unexpected_inner_payload_rows") != 0
            or row.get("unexpected_details") != []
            or row.get("raw_payload_redacted") is not True
        ):
            raise RFQStageError(
                f"repair-03 audit object identity/contract mismatch: {key}"
            )
        controls = _repair03_nonnegative_int(
            row.get("expected_empty_control_marker_rows"),
            f"repair-03 audit object {index} controls",
        )
        frames = _repair03_nonnegative_int(
            row.get("data_frame_rows"), f"repair-03 audit object {index} frames"
        )
        receipts = _repair03_nonnegative_int(
            row.get("segment_receipt_rows"),
            f"repair-03 audit object {index} receipts",
        )
        if controls + frames + receipts != lines:
            raise RFQStageError(f"repair-03 audit object row classes mismatch: {key}")
        row_markers = row.get("marker_counts")
        if not isinstance(row_markers, dict):
            raise RFQStageError(f"repair-03 audit object marker counts invalid: {key}")
        for marker, count in row_markers.items():
            if marker not in REPAIR03_EXPECTED_MARKER_COUNTS:
                raise RFQStageError(f"repair-03 audit object marker is invalid: {key}")
            marker_counts[marker] = marker_counts.get(marker, 0) + (
                _repair03_nonnegative_int(
                    count, f"repair-03 audit object {index} marker {marker}"
                )
            )
        observed_objects[key] = (expected_size, expected_sha)
        total_bytes += expected_size
        total_lines += lines
        total_controls += controls
        total_frames += frames
        total_receipts += receipts
    if (
        observed_objects != expected_objects
        or total_bytes != REPAIR03_EXPECTED_RETAINED_BYTES
        or total_lines != REPAIR03_EXPECTED_LINES
        or total_controls != REPAIR03_EXPECTED_BLANK_CONTROLS
        or total_frames != REPAIR03_EXPECTED_DATA_FRAMES
        or total_receipts != REPAIR03_EXPECTED_SEGMENT_RECEIPTS
        or marker_counts != REPAIR03_EXPECTED_MARKER_COUNTS
    ):
        raise RFQStageError("repair-03 audit object totals do not reconcile")
    return marker_counts


def _apply_repair03_parser_contract(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    *,
    descendant_boundary_root: Path | None = None,
) -> dict:
    """Validate repair-03 and reproduce repair-02's exact retained selection.

    A registered descendant is allowed to point this validator at its immutable
    pre-repair snapshot for files that repair-04 legitimately replaced
    (manifest receipts, registered queries, and the registry).  Repair-03's
    approval-grade evidence remains anchored at its own archive and fixed
    hashes; it is never accepted from a descendant-controlled active copy.
    """
    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(repairs, list)
        or len(repairs) != 3
        or [row.get("repair_id") if isinstance(row, dict) else None for row in repairs]
        != ["repair-01", "repair-02", "repair-03"]
    ):
        raise RFQStageError("RFQ repair-03 chain must be exactly repair-01/02/03")
    repair01, repair02, repair03 = repairs
    record_fields = {
        "schema_version", "repair_id", "parent_repair_id", "applied_at_utc",
        "pre_repair_status", "post_repair_status", "failed_stage_status",
        "rfq_result_state", "retry_requirement", "finding",
        "registration_change_class", "source_verification_mode",
        "quarantine_policy",
        "previous_repair_registration_path",
        "previous_repair_registration_sha256", "previous_repair_record_sha256",
        "failed_state_path", "failed_state_sha256",
        "failed_resource_receipt_path", "failed_resource_receipt_sha256",
        "failed_scratch_receipt_path", "failed_scratch_receipt_sha256",
        "failed_input_identity_path", "failed_input_identity_sha256",
        "inner_payload_audit_path", "inner_payload_audit_sha256",
        "inner_payload_audit_resource_path",
        "inner_payload_audit_resource_sha256", "inner_payload_audit_source_path",
        "inner_payload_audit_source_sha256",
        "inner_payload_audit_source_archive_path", "parser_contract_path",
        "parser_contract_sha256", "authority_basis_path",
        "authority_basis_sha256", "cycle1_binding_path", "cycle1_binding_sha256",
        "registered_rfq_query_sha256", "parser_contract", "authority_basis",
        "newly_quarantined_objects", "cumulative_quarantined_objects", "coverage",
        "selection_identity_unchanged", "previous_selection_fingerprint_sha256",
        "current_selection_fingerprint_sha256", "failed_input_fingerprint",
        "failed_attempt", "expected_success_resource", "inner_payload_audit",
        "cycle1_duckdb_binding", "previous_execution_commit",
        "current_execution_commit", "initial_repository_identity",
        "previous_repository_identity", "current_repository_identity",
        "repository_identity_chain", "previous_source_manifest_sha256",
        "current_source_manifest_sha256", "previous_source_sha256s_sha256",
        "current_source_sha256s_sha256", "previous_query_set_sha256",
        "current_query_set_sha256", "core_result_disposition",
        "core_results_recomputed", "core_result_artifacts", "trial_registry",
        "hypothesis_design_change", "data_selection_change", "quarantine_change",
        "threshold_feature_test_or_hypothesis_status_changed", "archive_path",
        "archive_inventory", "transaction_journal_path",
        "transaction_journal_sha256", "repair_receipt_path",
        "repair_receipt_sha256",
    }
    source_mode = repair03.get("source_verification_mode")
    snapshot_fields = {
        "source_snapshot_attestation_active_path",
        "source_snapshot_attestation_path",
        "source_snapshot_attestation_sha256",
        "source_snapshot_attestation",
    }
    if source_mode == REPAIR03_SNAPSHOT_SOURCE_MODE:
        record_fields.update(snapshot_fields)
    elif source_mode != REPAIR03_GIT_SOURCE_MODE:
        raise RFQStageError("repair-03 source verification mode is invalid")
    if set(repair03) != record_fields:
        raise RFQStageError("repair-03 registration record field set mismatch")
    if (
        manifest.get("status") != REPAIR03_STATUS
        or manifest.get("registration_state") != REPAIR03_REGISTRATION_STATE
        or repair03.get("schema_version") != REPAIR03_SCHEMA
        or repair03.get("parent_repair_id") != "repair-02"
        or repair03.get("pre_repair_status")
            != "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
        or repair03.get("post_repair_status") != REPAIR03_STATUS
        or repair03.get("failed_stage_status")
            != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or repair03.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair03.get("retry_requirement") != REPAIR03_RETRY_REQUIREMENT
        or repair03.get("finding") != REPAIR03_FINDING
        or repair03.get("registration_change_class") != REPAIR03_CHANGE_CLASS
        or repair03.get("quarantine_policy")
            != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
    ):
        raise RFQStageError("repair-03 append-chain identity/governance mismatch")
    applied_at = repair03.get("applied_at_utc")
    try:
        parsed_applied_at = dt.datetime.fromisoformat(
            applied_at[:-1] + "+00:00" if isinstance(applied_at, str)
            and applied_at.endswith("Z") else str(applied_at)
        )
    except ValueError as exc:
        raise RFQStageError("repair-03 applied timestamp is invalid") from exc
    if parsed_applied_at.utcoffset() != dt.timedelta(0):
        raise RFQStageError("repair-03 applied timestamp is not UTC")

    if source_mode == REPAIR03_SNAPSHOT_SOURCE_MODE:
        active_attestation = _require_run_relative_file(
            run_dir, REPAIR03_SOURCE_ATTESTATION,
            "repair-03 active source snapshot attestation",
        )
        archived_attestation = _require_run_relative_file(
            run_dir, REPAIR03_SOURCE_ATTESTATION_ARCHIVE,
            "repair-03 archived source snapshot attestation",
        )
        attestation_sha = sha256(active_attestation)
        attestation = read_json_object(
            active_attestation, "repair-03 source snapshot attestation"
        )
        if (
            active_attestation.read_bytes() != archived_attestation.read_bytes()
            or sha256(archived_attestation) != attestation_sha
            or repair03.get("source_snapshot_attestation_active_path")
                != REPAIR03_SOURCE_ATTESTATION
            or repair03.get("source_snapshot_attestation_path")
                != REPAIR03_SOURCE_ATTESTATION_ARCHIVE
            or repair03.get("source_snapshot_attestation_sha256")
                != attestation_sha
            or set(attestation) != {
                "schema_version", "run_id", "mission_sha256",
                "parent_execution_commit", "execution_commit",
                "direct_parent_verified", "git_tree", "source_relative",
                "source_tree_clean_at_attestation", "source_manifest_sha256",
                "source_sha256s_sha256", "commit_changed_paths",
                "attested_at_utc",
            }
        ):
            raise RFQStageError("repair-03 source snapshot attestation binding mismatch")
        git_tree = attestation.get("git_tree")
        attested_at = attestation.get("attested_at_utc")
        try:
            parsed_attested_at = dt.datetime.fromisoformat(
                attested_at[:-1] + "+00:00"
                if isinstance(attested_at, str) and attested_at.endswith("Z")
                else str(attested_at)
            )
        except ValueError as exc:
            raise RFQStageError(
                "repair-03 source snapshot attestation timestamp is invalid"
            ) from exc
        if (
            attestation.get("schema_version")
                != REPAIR03_SOURCE_ATTESTATION_SCHEMA
            or attestation.get("run_id") != run_dir.name
            or attestation.get("mission_sha256") != EXPECTED_MISSION_SHA256
            or attestation.get("parent_execution_commit")
                != repair03.get("previous_execution_commit")
            or attestation.get("execution_commit")
                != repair03.get("current_execution_commit")
            or attestation.get("direct_parent_verified") is not True
            or not isinstance(git_tree, str)
            or len(git_tree) != 40
            or any(character not in "0123456789abcdef" for character in git_tree)
            or attestation.get("source_relative") != REPAIR03_SOURCE_RELATIVE
            or attestation.get("source_tree_clean_at_attestation") is not True
            or attestation.get("source_manifest_sha256")
                != repair03.get("current_source_manifest_sha256")
            or attestation.get("source_sha256s_sha256")
                != repair03.get("current_source_sha256s_sha256")
            or attestation.get("commit_changed_paths") != REPAIR03_CHANGED_PATHS
            or parsed_attested_at.utcoffset() != dt.timedelta(0)
            or repair03.get("source_snapshot_attestation") != {
                "schema_version": REPAIR03_SOURCE_ATTESTATION_SCHEMA,
                "execution_commit": repair03.get("current_execution_commit"),
                "git_tree": git_tree,
                "direct_parent_verified": True,
                "source_relative": REPAIR03_SOURCE_RELATIVE,
                "source_tree_clean_at_attestation": True,
                "commit_changed_paths": REPAIR03_CHANGED_PATHS,
            }
        ):
            raise RFQStageError("repair-03 source snapshot attestation mismatch")

    registration_path = _require_run_relative_file(
        run_dir, REPAIR03_REGISTRATION, "repair-03 registration receipt"
    )
    registration_sha = sha256(registration_path)
    receipt = read_json_object(registration_path, "repair-03 registration receipt")
    without_receipt = {
        key: value for key, value in repair03.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair03.get("repair_receipt_path") != REPAIR03_REGISTRATION
        or repair03.get("repair_receipt_sha256") != registration_sha
        or receipt != without_receipt
    ):
        raise RFQStageError("repair-03 registration receipt binding mismatch")

    repair_root = run_dir / REPAIR03_ROOT
    pre_root = run_dir / REPAIR03_PRE_ROOT
    if (
        repair03.get("archive_path") != REPAIR03_PRE_ROOT
        or repair03.get("archive_inventory") != _archive_inventory(pre_root)
    ):
        raise RFQStageError("repair-03 pre-repair archive inventory mismatch")
    pre_manifest_path = _require_run_relative_file(
        run_dir, f"{REPAIR03_PRE_ROOT}/RUN_MANIFEST.json",
        "repair-03 archived repair-02 manifest",
    )
    pre_manifest = read_json_object(
        pre_manifest_path, "repair-03 archived repair-02 manifest"
    )
    if (
        pre_manifest.get("status")
            != "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
        or pre_manifest.get("registration_state")
            != "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        or pre_manifest.get("data_integrity_repairs") != repairs[:2]
        or pre_manifest.get("run_id") != run_dir.name
    ):
        raise RFQStageError("repair-03 archived manifest is not the repair-02 boundary")
    pre_registry_path = _require_run_relative_file(
        run_dir, f"{REPAIR03_PRE_ROOT}/TRIAL_REGISTRY.jsonl",
        "repair-03 archived repair-02 registry",
    )
    pre_registry = pre_registry_path.read_bytes()
    repair03_active_root = descendant_boundary_root or run_dir
    active_registry_path = _require_run_relative_file(
        repair03_active_root,
        "TRIAL_REGISTRY.jsonl",
        "repair-03 active trial registry",
    )
    active_registry = active_registry_path.read_bytes()
    if (
        active_registry == pre_registry
        or not active_registry.startswith(pre_registry)
    ):
        raise RFQStageError("repair-02 registry is not a strict repair-03 prefix")

    base_result = _apply_double_object_quarantine(
        run_dir, pre_manifest, inputs, pre_registry, False
    )
    if (
        base_result.get("selection_fingerprint_sha256")
            != repair03.get("current_selection_fingerprint_sha256")
        or repair03.get("selection_identity_unchanged") is not True
        or repair03.get("previous_selection_fingerprint_sha256")
            != base_result.get("selection_fingerprint_sha256")
        or repair03.get("failed_input_fingerprint")
            != base_result.get("selection_fingerprint_sha256")
        or repair03.get("newly_quarantined_objects") != []
        or repair03.get("cumulative_quarantined_objects")
            != repair02.get("cumulative_quarantined_objects")
    ):
        raise RFQStageError("repair-03 changed the registered RFQ selection")
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": inputs["objects"],
        "full_logical_manifest_bindings": inputs["logical_manifest_bindings"],
        "full_unique_bytes": inputs["bytes"],
        "retained_unique_objects": base_result["consumed_unique_objects"],
        "retained_logical_manifest_bindings": base_result[
            "consumed_logical_bindings"
        ],
        "retained_bytes": base_result["consumed_bytes"],
        "quarantined_unique_objects": base_result["quarantined_unique_objects"],
        "quarantined_logical_manifest_bindings": base_result[
            "quarantined_logical_bindings"
        ],
        "quarantined_bytes": base_result["quarantined_bytes"],
        "full_object_set_sha256": inputs["path_size_fingerprint_sha256"],
        "retained_object_set_sha256": base_result["consumed_object_set_sha256"],
        "quarantined_object_set_sha256": base_result[
            "quarantined_object_set_sha256"
        ],
        "retained_selection_fingerprint_sha256": base_result[
            "selection_fingerprint_sha256"
        ],
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    if repair03.get("coverage") != coverage:
        raise RFQStageError("repair-03 registered coverage changed")

    evidence_pairs = (
        ("REPORT/tables/RFQ_FULL_STAGE_STATE.json", REPAIR03_FAILED_STATE_ARCHIVE),
        ("logs/resources/rfq_full_stage_repair02.json",
         REPAIR03_FAILED_RESOURCE_ARCHIVE),
        ("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json",
         REPAIR03_FAILED_SCRATCH_ARCHIVE),
        ("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
         REPAIR03_FAILED_INPUT_ARCHIVE),
        ("DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json",
         REPAIR03_AUDIT_ARCHIVE),
        ("logs/resources/rfq_inner_payload_contract_audit03_final.json",
         REPAIR03_AUDIT_RESOURCE_ARCHIVE),
        ("tmp/rfq_inner_payload_contract_audit03.py",
         REPAIR03_AUDIT_SOURCE_ARCHIVE),
        (CYCLE1_DUCKDB_BINDING, REPAIR03_CYCLE1_BINDING_ARCHIVE),
    )
    if descendant_boundary_root is None:
        for active_relative, archive_relative in evidence_pairs:
            active = _require_run_relative_file(
                run_dir, active_relative, "repair-03 active preregistration evidence"
            )
            archived = _require_run_relative_file(
                run_dir, archive_relative, "repair-03 archived preregistration evidence"
            )
            if active.read_bytes() != archived.read_bytes():
                raise RFQStageError(
                    f"repair-03 active/archive evidence differs: {active_relative}"
                )

    approved_evidence = (
        (
            REPAIR03_FAILED_STATE_ARCHIVE,
            "failed_state_sha256",
            REPAIR03_APPROVED_FAILED_STATE_SHA256,
        ),
        (
            REPAIR03_FAILED_RESOURCE_ARCHIVE,
            "failed_resource_receipt_sha256",
            REPAIR03_APPROVED_FAILED_RESOURCE_SHA256,
        ),
        (
            REPAIR03_FAILED_SCRATCH_ARCHIVE,
            "failed_scratch_receipt_sha256",
            REPAIR03_APPROVED_FAILED_SCRATCH_RECEIPT_SHA256,
        ),
        (
            REPAIR03_FAILED_INPUT_ARCHIVE,
            "failed_input_identity_sha256",
            REPAIR03_APPROVED_FAILED_INPUT_SHA256,
        ),
        (
            REPAIR03_AUDIT_ARCHIVE,
            "inner_payload_audit_sha256",
            REPAIR03_APPROVED_AUDIT_SHA256,
        ),
        (
            REPAIR03_AUDIT_RESOURCE_ARCHIVE,
            "inner_payload_audit_resource_sha256",
            REPAIR03_APPROVED_AUDIT_RESOURCE_SHA256,
        ),
        (
            REPAIR03_AUDIT_SOURCE,
            "inner_payload_audit_source_sha256",
            REPAIR03_APPROVED_AUDIT_SOURCE_SHA256,
        ),
    )
    for relative, record_field, approved_sha in approved_evidence:
        if repair03.get(record_field) != approved_sha:
            raise RFQStageError(
                f"repair-03 approved evidence identity changed: {record_field}"
            )
        _artifact_sha(
            run_dir, relative, approved_sha,
            f"repair-03 approved evidence {record_field}",
        )
    _artifact_sha(
        run_dir,
        REPAIR03_AUDIT_SOURCE_ARCHIVE,
        REPAIR03_APPROVED_AUDIT_SOURCE_SHA256,
        "repair-03 approved archived audit source",
    )

    flat_artifacts = (
        ("failed_state_path", "failed_state_sha256", REPAIR03_FAILED_STATE_ARCHIVE),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256",
         REPAIR03_FAILED_RESOURCE_ARCHIVE),
        ("failed_scratch_receipt_path", "failed_scratch_receipt_sha256",
         REPAIR03_FAILED_SCRATCH_ARCHIVE),
        ("failed_input_identity_path", "failed_input_identity_sha256",
         REPAIR03_FAILED_INPUT_ARCHIVE),
        ("inner_payload_audit_path", "inner_payload_audit_sha256",
         REPAIR03_AUDIT_ARCHIVE),
        ("inner_payload_audit_resource_path", "inner_payload_audit_resource_sha256",
         REPAIR03_AUDIT_RESOURCE_ARCHIVE),
        ("inner_payload_audit_source_path", "inner_payload_audit_source_sha256",
         REPAIR03_AUDIT_SOURCE),
        ("authority_basis_path", "authority_basis_sha256", REPAIR03_AUTHORITY_BASIS),
        ("parser_contract_path", "parser_contract_sha256", REPAIR03_PARSER_CONTRACT),
        ("cycle1_binding_path", "cycle1_binding_sha256",
         REPAIR03_CYCLE1_BINDING_ARCHIVE),
        ("transaction_journal_path", "transaction_journal_sha256",
         REPAIR03_TRANSACTION_JOURNAL),
    )
    for path_field, sha_field, expected_path in flat_artifacts:
        if repair03.get(path_field) != expected_path:
            raise RFQStageError(f"repair-03 artifact path mismatch: {path_field}")
        _artifact_sha(
            run_dir, expected_path, repair03.get(sha_field), f"repair-03 {path_field}"
        )
    if (
        repair03.get("inner_payload_audit_source_archive_path")
            != REPAIR03_AUDIT_SOURCE_ARCHIVE
        or sha256(run_dir / REPAIR03_AUDIT_SOURCE_ARCHIVE)
            != repair03.get("inner_payload_audit_source_sha256")
        or repair03.get("previous_repair_registration_path")
            != repair02.get("repair_receipt_path")
        or repair03.get("previous_repair_registration_sha256")
            != repair02.get("repair_receipt_sha256")
        or repair03.get("previous_repair_record_sha256")
            != _json_payload_sha256(repair02)
    ):
        raise RFQStageError("repair-02/repair-03 receipt/evidence chain mismatch")

    parser_contract = read_json_object(
        run_dir / REPAIR03_PARSER_CONTRACT, "repair-03 parser contract"
    )
    expected_parser_fields = {
        "schema_version", "run_id", "created_at_utc", "finding", "mission_sha256",
        "selection_fingerprint_sha256", "retained_unique_objects", "retained_bytes",
        "outer_ndjson_policy", "expected_blank_control_markers",
        "expected_blank_raw_representation", "non_control_payload_policy",
        "unexpected_payload_policy", "line_salvage", "data_selection_change",
        "hypothesis_design_change", "registered_query_path",
        "registered_query_sha256", "audit_path", "audit_sha256",
    }
    if set(parser_contract) != expected_parser_fields:
        raise RFQStageError("repair-03 parser contract field set mismatch")
    if (
        parser_contract.get("schema_version") != REPAIR03_PARSER_SCHEMA
        or parser_contract.get("run_id") != run_dir.name
        or parser_contract.get("created_at_utc") != applied_at
        or parser_contract.get("finding") != REPAIR03_FINDING
        or parser_contract.get("mission_sha256") != EXPECTED_MISSION_SHA256
        or parser_contract.get("selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or parser_contract.get("retained_unique_objects")
            != base_result["consumed_unique_objects"]
        or parser_contract.get("retained_bytes") != base_result["consumed_bytes"]
        or parser_contract.get("outer_ndjson_policy")
            != "STRICT_NDJSON_IGNORE_ERRORS_FALSE"
        or parser_contract.get("expected_blank_control_markers")
            != list(EXPECTED_BLANK_CONTROL_MARKERS)
        or parser_contract.get("expected_blank_raw_representation")
            != "EXACT_EMPTY_STRING"
        or parser_contract.get("non_control_payload_policy")
            != "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED"
        or parser_contract.get("unexpected_payload_policy") != "ABORT_BEFORE_RESULT"
        or parser_contract.get("line_salvage") is not False
        or parser_contract.get("data_selection_change") is not False
        or parser_contract.get("hypothesis_design_change") is not False
        or parser_contract.get("registered_query_path") != "queries/rfq_full_stage.py"
        or parser_contract.get("registered_query_sha256")
            != repair03.get("registered_rfq_query_sha256")
        or parser_contract.get("audit_path") != REPAIR03_AUDIT_ARCHIVE
        or parser_contract.get("audit_sha256")
            != repair03.get("inner_payload_audit_sha256")
    ):
        raise RFQStageError("repair-03 parser contract content mismatch")
    audited_contract_sha = _compact_json_sha256(REPAIR03_AUDITED_PARSER_CONTRACT)
    if audited_contract_sha != REPAIR03_APPROVED_AUDITED_PARSER_CONTRACT_SHA256:
        raise RFQStageError("repair-03 approved parser-contract identity changed")
    expected_parser_record = {
        "path": REPAIR03_PARSER_CONTRACT,
        "sha256": repair03["parser_contract_sha256"],
        "schema_version": REPAIR03_PARSER_SCHEMA,
        "registered_query_sha256": repair03["registered_rfq_query_sha256"],
        "audited_parser_contract_sha256": audited_contract_sha,
        "audited_parser_contract": REPAIR03_AUDITED_PARSER_CONTRACT,
    }
    if repair03.get("parser_contract") != expected_parser_record:
        raise RFQStageError("repair-03 nested parser contract mismatch")

    authority = read_json_object(
        run_dir / REPAIR03_AUTHORITY_BASIS, "repair-03 authority basis"
    )
    expected_authority = {
        "schema_version": REPAIR03_AUTHORITY_SCHEMA,
        "run_id": run_dir.name,
        "recorded_at_utc": applied_at,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "authority_basis": "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_CORRECTION",
        "permitted_change": "PARSER_CONTRACT_CORRECTION_ONLY",
        "operator_repair02_authorization_reused": False,
        "new_data_integrity_decision": False,
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
        "prerequisite_audit_path": REPAIR03_AUDIT_ARCHIVE,
        "prerequisite_audit_sha256": repair03["inner_payload_audit_sha256"],
    }
    if authority != expected_authority or repair03.get("authority_basis") != {
        "path": REPAIR03_AUTHORITY_BASIS,
        "sha256": repair03["authority_basis_sha256"],
        "schema_version": REPAIR03_AUTHORITY_SCHEMA,
        "authority_basis": expected_authority["authority_basis"],
        "permitted_change": expected_authority["permitted_change"],
    }:
        raise RFQStageError("repair-03 authority basis mismatch")

    audit = read_json_object(run_dir / REPAIR03_AUDIT_ARCHIVE, "repair-03 audit")
    active_input_path = _require_run_relative_file(
        run_dir,
        (
            REPAIR03_FAILED_INPUT_ARCHIVE
            if descendant_boundary_root is not None
            else "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
        ),
        "repair-03 active failed input identity",
    )
    active_input_sha = sha256(active_input_path)
    if active_input_sha != REPAIR03_APPROVED_FAILED_INPUT_SHA256:
        raise RFQStageError("repair-03 active input is not the approved evidence")
    failed_input = read_json_object(active_input_path, "repair-03 failed input identity")
    marker_counts = _validate_repair03_inner_payload_audit(
        audit,
        run_id=run_dir.name,
        active_input=failed_input,
        active_input_sha256=active_input_sha,
        audit_source_sha256=REPAIR03_APPROVED_AUDIT_SOURCE_SHA256,
        base_result=base_result,
    )
    audit_record = {
        "path": REPAIR03_AUDIT_ARCHIVE,
        "sha256": repair03["inner_payload_audit_sha256"],
        "resource_path": REPAIR03_AUDIT_RESOURCE_ARCHIVE,
        "resource_sha256": repair03["inner_payload_audit_resource_sha256"],
        "source_path": REPAIR03_AUDIT_SOURCE,
        "source_sha256": repair03["inner_payload_audit_source_sha256"],
        "objects_scanned": audit["objects_scanned"],
        "bytes_scanned": audit["bytes_scanned"],
        "lines_scanned": audit["lines_scanned"],
        "data_frame_rows": audit["data_frame_rows"],
        "segment_receipt_rows": audit["segment_receipt_rows"],
        "expected_blank_control_marker_rows": audit[
            "expected_empty_control_marker_rows"
        ],
        "expected_blank_control_marker_counts": {
            marker: marker_counts[marker]
            for marker in EXPECTED_BLANK_CONTROL_MARKERS
            if marker_counts.get(marker, 0)
        },
        "outer_invalid_object_count": 0,
        "outer_invalid_line_count": 0,
        "identity_mismatch_count": 0,
        "unexpected_inner_payload_object_count": 0,
        "unexpected_inner_payload_row_count": 0,
        "audited_parser_contract_sha256": audited_contract_sha,
        "analysis_result_opened": False,
    }
    if repair03.get("inner_payload_audit") != audit_record:
        raise RFQStageError("repair-03 nested audit record mismatch")
    audit_resource = read_json_object(
        run_dir / REPAIR03_AUDIT_RESOURCE_ARCHIVE, "repair-03 audit resource"
    )
    command = audit_resource.get("command")
    expected_audit_command = [
        "/opt/w09/venv/bin/python",
        (
            f"/srv/w09-research/runs/{run_dir.name}/"
            "tmp/rfq_inner_payload_contract_audit03.py"
        ),
        "--run-dir", f"/srv/w09-research/runs/{run_dir.name}",
        "--cache-root", "/srv/w09-research/cache", "--workers", "8",
    ]
    if (
        audit_resource.get("schema_version") != "w09-stage-resource-v1"
        or audit_resource.get("label")
            != "rfq_inner_payload_contract_audit03_final"
        or audit_resource.get("return_code") != 0
        or command != expected_audit_command
    ):
        raise RFQStageError("repair-03 audit resource receipt mismatch")

    failed_state = read_json_object(
        run_dir / REPAIR03_FAILED_STATE_ARCHIVE, "repair-03 failed state"
    )
    failed_resource = read_json_object(
        run_dir / REPAIR03_FAILED_RESOURCE_ARCHIVE, "repair-03 failed resource"
    )
    failed_scratch = read_json_object(
        run_dir / REPAIR03_FAILED_SCRATCH_ARCHIVE, "repair-03 failed scratch receipt"
    )
    preserved = _require_run_relative_file(
        run_dir, REPAIR03_PRESERVED_SCRATCH, "repair-03 preserved failed scratch"
    )
    active_scratch = run_dir / "cache/rfq_full_scratch.duckdb"
    if (
        failed_state.get("schema") != "rfq-full-stage-state-v1"
        or failed_state.get("status")
            != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or failed_state.get("resume") is not False
        or failed_state.get("next_required_authority")
            != "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or failed_state.get("input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or failed_state.get("error")
            != "unexpected malformed inner RFQ payload in a consumed object; aborting"
        or failed_state.get("expected_success_resource")
            != {"label": REPAIR02_SUCCESS_RESOURCE_LABEL,
                "path": REPAIR02_SUCCESS_RESOURCE_PATH}
        or failed_resource.get("schema_version") != "w09-stage-resource-v1"
        or failed_resource.get("label") != REPAIR02_SUCCESS_RESOURCE_LABEL
        or failed_resource.get("return_code") != 1
        or not isinstance(failed_resource.get("command"), list)
        or "--resume" in failed_resource["command"]
        or failed_input.get("schema") != "rfq-full-input-identity-v2"
        or failed_input.get("selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or failed_input.get("repair_chain") != base_result["repair_chain"]
        or failed_input.get("failed_attempt_bindings")
            != base_result["failed_attempt_bindings"]
        or failed_input.get("expected_success_resource")
            != failed_state.get("expected_success_resource")
        or failed_scratch.get("schema_version")
            != "rfq-failed-scratch-receipt-v1"
        or failed_scratch.get("run_id") != run_dir.name
        or failed_scratch.get("input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or failed_scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or failed_scratch.get("resume_allowed") is not False
        or failed_scratch.get("preserved_scratch_path")
            != str((run_dir / REPAIR03_PRESERVED_SCRATCH).resolve())
        or failed_scratch.get("bytes") != REPAIR03_APPROVED_FAILED_SCRATCH_BYTES
        or failed_scratch.get("sha256")
            != REPAIR03_APPROVED_FAILED_SCRATCH_SHA256
        or preserved.stat().st_size != failed_scratch.get("bytes")
        or sha256(preserved) != failed_scratch.get("sha256")
        or active_scratch.exists()
        or Path(str(active_scratch) + ".wal").exists()
    ):
        raise RFQStageError("repair-03 failed attempt/fresh-scratch boundary mismatch")
    failed_attempt_record = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "state_path": REPAIR03_FAILED_STATE_ARCHIVE,
        "state_sha256": repair03["failed_state_sha256"],
        "state_status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "resource_active_path": "logs/resources/rfq_full_stage_repair02.json",
        "resource_path": REPAIR03_FAILED_RESOURCE_ARCHIVE,
        "resource_sha256": repair03["failed_resource_receipt_sha256"],
        "resource_label": REPAIR02_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json"
        ),
        "scratch_receipt_path": REPAIR03_FAILED_SCRATCH_ARCHIVE,
        "scratch_receipt_sha256": repair03["failed_scratch_receipt_sha256"],
        "preserved_scratch_active_path": REPAIR03_PRESERVED_SCRATCH,
        "preserved_scratch_sha256": failed_scratch["sha256"],
        "preserved_scratch_bytes": failed_scratch["bytes"],
        "input_identity_active_path": "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "input_identity_path": REPAIR03_FAILED_INPUT_ARCHIVE,
        "input_identity_sha256": repair03["failed_input_identity_sha256"],
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": REPAIR03_RETRY_REQUIREMENT,
    }
    if repair03.get("failed_attempt") != failed_attempt_record:
        raise RFQStageError("repair-03 nested failed-attempt record mismatch")

    pre_repository = pre_manifest.get("repository")
    repository = manifest.get("repository")
    if not isinstance(pre_repository, dict) or not isinstance(repository, dict):
        raise RFQStageError("repair-03 repository transfer is missing")
    pre_identity = _repository_identity(pre_repository)
    current_identity = _repository_identity(repository)
    identity_chain = repository.get("identity_history")
    if (
        not isinstance(identity_chain, list) or len(identity_chain) != 4
        or identity_chain != repair03.get("repository_identity_chain")
        or identity_chain[:-1] != pre_repository.get("identity_history")
        or identity_chain[-1] != current_identity
        or repair03.get("initial_repository_identity") != identity_chain[0]
        or repair03.get("previous_repository_identity") != pre_identity
        or repair03.get("current_repository_identity") != current_identity
        or repository.get("registration_repair_id") != "repair-03"
        or repair03.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repair03.get("current_execution_commit")
            != repository.get("execution_commit")
        or repair03.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repair03.get("current_source_manifest_sha256")
            != repository.get("source_manifest_sha256")
        or repair03.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repair03.get("current_source_sha256s_sha256")
            != repository.get("source_sha256s_sha256")
        or repair03.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repair03.get("current_query_set_sha256")
            != repository.get("query_set_sha256")
        or repository.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repository.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repository.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repository.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repository.get("previous_identity") != pre_identity
    ):
        raise RFQStageError("repair-03 repository identity transfer mismatch")
    if (
        sha256(repair03_active_root / "SOURCE_MANIFEST.json")
            != repository.get("source_manifest_sha256")
        or sha256(repair03_active_root / "SOURCE_SHA256SUMS.txt")
            != repository.get("source_sha256s_sha256")
    ):
        raise RFQStageError("repair-03 active source receipts changed")
    query_rows = _validate_query_receipt(
        run_dir, repository, active_root=repair03_active_root
    )
    registered_query_sha = query_rows.get("queries/rfq_full_stage.py")
    if (
        registered_query_sha != repair03.get("registered_rfq_query_sha256")
        or registered_query_sha != parser_contract.get("registered_query_sha256")
    ):
        raise RFQStageError("repair-03 registered RFQ query SHA mismatch")

    journal = read_json_object(
        run_dir / REPAIR03_TRANSACTION_JOURNAL, "repair-03 transaction journal"
    )
    mutations = journal.get("active_mutations")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt", "QUERY_SHA256SUMS.txt",
        *pre_repository.get("query_files", []), "TRIAL_REGISTRY.jsonl",
    ]
    repair_inventory = _archive_inventory(repair_root)
    if (
        set(journal) != {
            "schema_version", "repair_id", "run_id", "state",
            "original_manifest_sha256", "active_mutations", "expected_repair_files",
        }
        or journal.get("schema_version") != "repair03-registration-transaction-v1"
        or journal.get("repair_id") != "repair-03"
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or [row.get("path") if isinstance(row, dict) else None for row in mutations]
            != expected_mutation_paths
        or set(journal.get("expected_repair_files", []))
            != {row["path"] for row in repair_inventory}
    ):
        raise RFQStageError("repair-03 transaction journal mismatch")
    for mutation in mutations:
        if set(mutation) != {
            "path", "original_archive_path", "original_sha256",
            "replacement_sha256", "append_only_registry",
        }:
            raise RFQStageError("repair-03 transaction mutation field set mismatch")
        relative = mutation["path"]
        original_relative = f"{REPAIR03_PRE_ROOT}/{relative}"
        if (
            mutation.get("original_archive_path") != original_relative
            or mutation.get("original_sha256")
                != sha256(_require_run_relative_file(
                    run_dir, original_relative, "repair-03 original mutation payload"
                ))
            or mutation.get("replacement_sha256")
                != sha256(_require_run_relative_file(
                    repair03_active_root,
                    relative,
                    "repair-03 replacement mutation payload",
                ))
            or mutation.get("append_only_registry")
                is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise RFQStageError(f"repair-03 transaction mutation mismatch: {relative}")

    trial = repair03.get("trial_registry")
    if (
        not isinstance(trial, dict)
        or set(trial) != {
            "previous_sha256", "current_sha256", "previous_bytes", "current_bytes",
            "strict_previous_bytes_prefix", "appended_records",
            "trial_registration_ids",
        }
        or trial.get("previous_sha256") != hashlib.sha256(pre_registry).hexdigest()
        or trial.get("current_sha256") != hashlib.sha256(active_registry).hexdigest()
        or trial.get("previous_bytes") != len(pre_registry)
        or trial.get("current_bytes") != len(active_registry)
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids")
            != ["RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03"]
    ):
        raise RFQStageError("repair-03 active trial-registry boundary mismatch")
    suffix = active_registry[len(pre_registry):]
    suffix_rows = _read_registry_rows(suffix, "repair-03 registry suffix")
    if (
        len(suffix_rows) != 2
        or [row.get("trial_registration_id") for row in suffix_rows]
            != trial["trial_registration_ids"]
        or any(row.get("result_opened") is not False for row in suffix_rows)
        or any(row.get("hypothesis_conclusion_opened") is not False
               for row in suffix_rows)
        or suffix_rows[0].get("failed_state_sha256")
            != repair03.get("failed_state_sha256")
        or suffix_rows[0].get("failed_resource_receipt_sha256")
            != repair03.get("failed_resource_receipt_sha256")
        or suffix_rows[0].get("failed_scratch_receipt_sha256")
            != repair03.get("failed_scratch_receipt_sha256")
        or suffix_rows[0].get("failed_input_identity_sha256")
            != repair03.get("failed_input_identity_sha256")
        or suffix_rows[1].get("parser_contract_sha256")
            != repair03.get("parser_contract_sha256")
        or suffix_rows[1].get("current_selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or suffix_rows[1].get("retry_requirement") != REPAIR03_RETRY_REQUIREMENT
    ):
        raise RFQStageError("repair-03 registry records mismatch")

    if (
        repair03.get("expected_success_resource")
            != {"label": REPAIR03_SUCCESS_RESOURCE_LABEL,
                "path": REPAIR03_SUCCESS_RESOURCE_PATH}
        or repair03.get("cycle1_duckdb_binding")
            != repair02.get("cycle1_duckdb_binding")
        or repair03.get("core_result_disposition")
            != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair03.get("core_results_recomputed") is not False
        or repair03.get("core_result_artifacts")
            != repair02.get("core_result_artifacts")
        or repair03.get("hypothesis_design_change") != "NONE"
        or repair03.get("data_selection_change") != "NONE"
        or repair03.get("quarantine_change") != "NONE"
        or repair03.get("threshold_feature_test_or_hypothesis_status_changed")
            is not False
    ):
        raise RFQStageError("repair-03 preserved-results governance mismatch")
    for result_relative in (
        "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json", "REPORT/RFQ_FULL_STAGE.md",
        "cache/rfq_full_catalog.duckdb",
    ):
        if (run_dir / result_relative).exists():
            raise RFQStageError("repair-03 consumer started after an RFQ result existed")

    failed03 = {
        "repair_id": "repair-03",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "failed_state_path": REPAIR03_FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair03["failed_state_sha256"],
        "failed_resource_receipt_path": REPAIR03_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair03[
            "failed_resource_receipt_sha256"
        ],
        "failed_scratch_receipt_path": REPAIR03_FAILED_SCRATCH_ARCHIVE,
        "failed_scratch_receipt_sha256": repair03["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": REPAIR03_FAILED_INPUT_ARCHIVE,
        "failed_input_identity_sha256": repair03["failed_input_identity_sha256"],
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "resource_label": REPAIR02_SUCCESS_RESOURCE_LABEL,
        "retry_requirement": REPAIR03_RETRY_REQUIREMENT,
    }
    repair03_chain = {
        "repair_id": "repair-03",
        "registration_path": REPAIR03_REGISTRATION,
        "registration_sha256": registration_sha,
        "parser_contract_path": REPAIR03_PARSER_CONTRACT,
        "parser_contract_sha256": repair03["parser_contract_sha256"],
        "authority_basis_path": REPAIR03_AUTHORITY_BASIS,
        "authority_basis_sha256": repair03["authority_basis_sha256"],
        "inner_payload_audit_path": REPAIR03_AUDIT_ARCHIVE,
        "inner_payload_audit_sha256": repair03["inner_payload_audit_sha256"],
    }
    parser_binding = {
        "path": REPAIR03_PARSER_CONTRACT,
        "sha256": repair03["parser_contract_sha256"],
        "schema_version": REPAIR03_PARSER_SCHEMA,
    }
    result = dict(base_result)
    result.update({
        "repair_chain": [*base_result["repair_chain"], repair03_chain],
        "failed_attempt_bindings": [
            *base_result["failed_attempt_bindings"], failed03,
        ],
        "expected_success_resource": {
            "label": REPAIR03_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR03_SUCCESS_RESOURCE_PATH,
        },
        "inner_payload_parser_contract": parser_binding,
        "registered_rfq_query_sha256": registered_query_sha,
    })
    return result


def _repair04_failed_command(run_id: str) -> list[str]:
    remote_run = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{remote_run}/source/rfq_full_stage.py",
        "--run-dir", remote_run,
        "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "40GB",
        "--max-temp-size", "120GB",
        "--threads", "8",
        "--min-free-gib", "120",
        "--clob-max-per-root", "50",
    ]


def _repair04_retry_command(run_id: str) -> list[str]:
    remote_run = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{remote_run}/{REPAIR04_EXECUTION_QUERY}",
        "--run-dir", remote_run,
        "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "46GB",
        "--max-temp-size", "70GB",
        "--threads", "4",
        "--min-free-gib", "100",
        "--clob-max-per-root", "50",
    ]


def _repair05_retry_command(run_id: str) -> list[str]:
    remote_run = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{remote_run}/{REPAIR05_EXECUTION_QUERY}",
        "--run-dir", remote_run,
        "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "46GB",
        "--max-temp-size", "70GB",
        "--threads", "4",
        "--min-free-gib", "100",
        "--clob-max-per-root", "50",
    ]


def _expected_repair05_wiring_contract(
    run_id: str,
    created_at: str,
    evidence: dict,
    current_query_sha256: str,
) -> dict:
    """Mirror repair05_registration.make_wiring_contract exactly."""
    return {
        "schema_version": REPAIR05_WIRING_SCHEMA,
        "run_id": run_id,
        "created_at_utc": created_at,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "repair_id": "repair-05",
        "parent_repair_id": "repair-04",
        "finding": REPAIR05_FINDING,
        "failure_disposition": REPAIR05_FAILURE_DISPOSITION,
        "failure_phase": REPAIR05_FAILURE_PHASE,
        "change_class": REPAIR05_CHANGE_CLASS,
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
            "blocker_path": evidence["blocker_path"],
            "blocker_sha256": evidence["blocker_sha256"],
            "resource_path": evidence["failed_resource_receipt_path"],
            "resource_sha256": evidence["failed_resource_receipt_sha256"],
            "resource_label": REPAIR04_SUCCESS_RESOURCE_LABEL,
            "return_code": 1,
            "command": _repair05_retry_command(run_id),
            "error_type": "KeyError",
            "error": "KeyError: 'cycle1_duckdb_binding'",
            "new_state_written": False,
            "new_input_identity_written": False,
            "analysis_stage_started": False,
            "active_state_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
            "active_state_sha256": evidence["unchanged_state_sha256"],
            "active_input_identity_path": (
                "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
            ),
            "active_input_identity_sha256": evidence[
                "unchanged_input_identity_sha256"
            ],
            "active_scratch_absent": True,
            "active_wal_absent": True,
        },
        "defect": {
            "offending_function": "_validate_repair04_failed_boundary",
            "offending_expression": "base_result['cycle1_duckdb_binding']",
            "offending_parent_source_path": REPAIR05_EXECUTION_QUERY,
            "offending_parent_source_sha256": REPAIR05_PARENT_RFQ_QUERY_SHA256,
            "offending_parent_source_line": 3822,
            "invalid_binding_source": "repair03_quarantine_consumer_base_result",
            "required_binding_source": (
                "repair04_and_repair03_registration_cycle1_duckdb_binding_"
                "plus_immutable_active_and_archived_binding"
            ),
        },
        "cycle1_binding": {
            "active_path": CYCLE1_DUCKDB_BINDING,
            "active_sha256": evidence["cycle1_binding_sha256"],
            "archived_path": evidence["cycle1_binding_path"],
            "archived_sha256": evidence["cycle1_binding_sha256"],
            "registered_cycle1_duckdb_binding": copy.deepcopy(
                evidence["registered_cycle1_duckdb_binding"]
            ),
        },
        "correction": {
            "scope": "CONSUMER_VALIDATION_WIRING_ONLY",
            "registered_query_path": REPAIR05_EXECUTION_QUERY,
            "parent_registered_query_sha256": REPAIR05_PARENT_RFQ_QUERY_SHA256,
            "current_registered_query_sha256": current_query_sha256,
            "expected_command": _repair05_retry_command(run_id),
            "expected_success_resource": {
                "label": REPAIR05_SUCCESS_RESOURCE_LABEL,
                "path": REPAIR05_SUCCESS_RESOURCE_PATH,
            },
            "fresh_scratch_required": True,
            "resume_allowed": False,
        },
        "inherited_contracts": {
            "repair04_resource_contract_path": REPAIR04_RESOURCE_CONTRACT_PATH,
            "repair04_resource_contract_sha256": (
                REPAIR05_PARENT_RESOURCE_CONTRACT_SHA256
            ),
            "runtime": dict(REPAIR04_RUNTIME_CONTRACT),
            "selection_fingerprint_sha256": REPAIR02_APPROVED_SELECTION_FINGERPRINT,
            "retained_object_set_sha256": REPAIR02_APPROVED_RETAINED_FINGERPRINT,
            "quarantined_object_set_sha256": (
                REPAIR02_APPROVED_QUARANTINED_FINGERPRINT
            ),
            "full_object_set_sha256": REPAIR02_APPROVED_FULL_FINGERPRINT,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def _expected_repair05_authority_basis(
    run_id: str,
    recorded_at: str,
    wiring_contract_sha256: str,
) -> dict:
    """Mirror repair05_registration.make_authority_basis exactly."""
    return {
        "schema_version": REPAIR05_AUTHORITY_SCHEMA,
        "run_id": run_id,
        "recorded_at_utc": recorded_at,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "authority_class": REPAIR05_AUTHORITY_CLASS,
        "permitted_change": "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY",
        "wiring_contract_path": REPAIR05_WIRING_CONTRACT,
        "wiring_contract_sha256": wiring_contract_sha256,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def _repair06_retry_command(run_id: str) -> list[str]:
    """Repair-06 keeps the exact repair-05 runtime command and resources."""
    return _repair05_retry_command(run_id)


def _repair06_preflight_contract(run_id: str) -> dict:
    return {
        "cli_flags": [
            "--validate-run-preflight-only",
            "--validate-run-preflight-overlay",
        ],
        "overlay_path": f"{REPAIR06_ROOT}/post_repair",
        "expected_stdout": (
            f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_id}"
        ),
        "read_only": True,
        "stop_before_input_discovery": True,
        "stop_before_repair_replay": True,
        "stop_before_duckdb_import": True,
        "stop_before_state_input_scratch_or_result_write": True,
    }


def _expected_repair06_status_wiring_contract(
    run_id: str,
    applied_at: str,
    repair06: dict,
    repair05: dict,
    parent_manifest_sha256: str,
) -> dict:
    """Canonical repair-06 status-wiring-only contract."""
    return {
        "schema_version": REPAIR06_STATUS_WIRING_SCHEMA,
        "repair_id": "repair-06",
        "run_id": run_id,
        "applied_at_utc": applied_at,
        "pre_repair_status": REPAIR05_STATUS,
        "post_repair_status": REPAIR06_STATUS,
        "registration_state": REPAIR06_REGISTRATION_STATE,
        "change_class": REPAIR06_CHANGE_CLASS,
        "failure_evidence": {
            "blocker_active_path": REPAIR06_BLOCKER_ACTIVE,
            "blocker_archived_path": REPAIR06_BLOCKER_ARCHIVE,
            "blocker_sha256": REPAIR06_BLOCKER_SHA256,
            "failed_resource_active_path": REPAIR05_SUCCESS_RESOURCE_PATH,
            "failed_resource_archived_path": REPAIR06_FAILED_RESOURCE_ARCHIVE,
            "failed_resource_sha256": REPAIR06_FAILED_RESOURCE_SHA256,
            "unchanged_state_active_path": (
                "REPORT/tables/RFQ_FULL_STAGE_STATE.json"
            ),
            "unchanged_state_archived_path": REPAIR06_UNCHANGED_STATE_ARCHIVE,
            "unchanged_state_sha256": REPAIR04_APPROVED_FAILED_STATE_SHA256,
            "unchanged_input_identity_active_path": (
                "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
            ),
            "unchanged_input_identity_archived_path": (
                REPAIR06_UNCHANGED_INPUT_ARCHIVE
            ),
            "unchanged_input_identity_sha256": (
                REPAIR04_APPROVED_FAILED_INPUT_SHA256
            ),
            "analysis_stage_started": False,
            "new_state_written": False,
            "new_input_identity_written": False,
            "active_scratch_absent": True,
            "active_wal_absent": True,
        },
        "parent_repair05": {
            "manifest_sha256": parent_manifest_sha256,
            "repair_receipt_path": repair05["repair_receipt_path"],
            "repair_receipt_sha256": repair05["repair_receipt_sha256"],
            "transaction_journal_path": repair05["transaction_journal_path"],
            "transaction_journal_sha256": repair05[
                "transaction_journal_sha256"
            ],
            "wiring_contract_path": repair05["wiring_contract_path"],
            "wiring_contract_sha256": repair05["wiring_contract_sha256"],
            "authority_basis_path": repair05["authority_basis_path"],
            "authority_basis_sha256": repair05["authority_basis_sha256"],
            "core_result_artifacts": copy.deepcopy(
                repair05["core_result_artifacts"]
            ),
        },
        "status_wiring": {
            "offending_function": "validate_run",
            "error": (
                "RFQStageError: Cycle-1 core is not in an RFQ-stage-compatible "
                "state"
            ),
            "error_type": "RFQStageError",
            "failure_phase": REPAIR06_FAILURE_PHASE,
            "finding": REPAIR06_FINDING,
            "accepted_parent_status": REPAIR05_STATUS,
            "accepted_parent_registration_state": REPAIR05_REGISTRATION_STATE,
            "registered_post_status": REPAIR06_STATUS,
            "registered_post_registration_state": REPAIR06_REGISTRATION_STATE,
            "source_change_scope": REPAIR06_CHANGE_CLASS,
        },
        "inherited_contracts": {
            "coverage": copy.deepcopy(repair05["coverage"]),
            "cycle1_duckdb_binding": copy.deepcopy(
                repair05["cycle1_duckdb_binding"]
            ),
            "parser_contract": copy.deepcopy(repair05["parser_contract"]),
            "resource_contract": copy.deepcopy(repair05["resource_contract"]),
            "runtime": dict(REPAIR04_RUNTIME_CONTRACT),
        },
        "preflight": _repair06_preflight_contract(run_id),
        "retry": {
            "requirement": REPAIR06_RETRY_REQUIREMENT,
            "expected_success_resource": {
                "label": REPAIR06_SUCCESS_RESOURCE_LABEL,
                "path": REPAIR06_SUCCESS_RESOURCE_PATH,
            },
            "command": _repair06_retry_command(run_id),
            "fresh_scratch": True,
            "resume": False,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
    }


def _expected_repair06_authority_basis(
    run_id: str,
    applied_at: str,
    status_wiring_contract_sha256: str,
) -> dict:
    return {
        "schema_version": REPAIR06_AUTHORITY_SCHEMA,
        "repair_id": "repair-06",
        "run_id": run_id,
        "created_at_utc": applied_at,
        "authority_class": REPAIR06_AUTHORITY_CLASS,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "permitted_change": REPAIR06_CHANGE_CLASS,
        "status_wiring_contract_path": REPAIR06_STATUS_WIRING_CONTRACT,
        "status_wiring_contract_sha256": status_wiring_contract_sha256,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def _expected_repair05_trial_rows(
    applied_at: str,
    old_repository: dict,
    current_identity: dict,
    repair05: dict,
    parent: dict,
    resource: dict,
) -> list[dict]:
    """Mirror repair05_registration.make_trial_records exactly."""
    common = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(REPAIR04_RESEARCH_TRIAL_IDS),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": "repair-04",
    }
    failure = {
        **common,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_05",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR04",
        "failure_class": REPAIR05_FINDING,
        "failure_disposition": REPAIR05_FAILURE_DISPOSITION,
        "failure_phase": REPAIR05_FAILURE_PHASE,
        "error_type": "KeyError",
        "error": "KeyError: 'cycle1_duckdb_binding'",
        "hypothesis_conclusion": "NONE",
        "blocker_path": repair05["blocker_path"],
        "blocker_sha256": repair05["blocker_sha256"],
        "failed_resource_receipt_path": repair05[
            "failed_resource_receipt_path"
        ],
        "failed_resource_receipt_sha256": repair05[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_path": repair05["unchanged_state_path"],
        "unchanged_state_sha256": repair05["unchanged_state_sha256"],
        "unchanged_input_identity_path": repair05[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": repair05[
            "unchanged_input_identity_sha256"
        ],
        "cycle1_binding_path": repair05["cycle1_binding_path"],
        "cycle1_binding_sha256": repair05["cycle1_binding_sha256"],
        "new_state_written": False,
        "new_input_identity_written": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "resource_label": REPAIR04_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "resource_metrics": {
            "started_at_utc": resource["started_at_utc"],
            "completed_at_utc": resource["completed_at_utc"],
            "wall_seconds": resource["wall_seconds"],
            "cpu_user_seconds": resource["cpu_user_seconds"],
            "cpu_system_seconds": resource["cpu_system_seconds"],
            "peak_process_tree_rss_kib_polled": resource[
                "peak_process_tree_rss_kib_polled"
            ],
            "peak_temp_bytes_polled": resource["peak_temp_bytes_polled"],
            "minimum_disk_free_bytes_polled": resource[
                "minimum_disk_free_bytes_polled"
            ],
        },
        "execution_commit": old_repository["execution_commit"],
        "source_manifest_sha256": old_repository["source_manifest_sha256"],
        "source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "query_set_sha256": old_repository["query_set_sha256"],
    }
    preregistration = {
        **common,
        "trial_registration_id": "RFQ_CONSUMER_VALIDATION_WIRING_REPAIR_05",
        "record_type": "CONSUMER_VALIDATION_WIRING_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR05_PREREGISTRATION",
        "finding": REPAIR05_FINDING,
        "registration_change_class": REPAIR05_CHANGE_CLASS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": REPAIR05_RETRY_REQUIREMENT,
        "wiring_contract_path": repair05["wiring_contract_path"],
        "wiring_contract_sha256": repair05["wiring_contract_sha256"],
        "authority_basis_path": repair05["authority_basis_path"],
        "authority_basis_sha256": repair05["authority_basis_sha256"],
        "resource_contract_path": parent["resource_contract_path"],
        "resource_contract_sha256": parent["resource_contract_sha256"],
        "runtime": dict(REPAIR04_RUNTIME_CONTRACT),
        "expected_success_resource": {
            "label": REPAIR05_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR05_SUCCESS_RESOURCE_PATH,
        },
        "newly_quarantined_objects": [],
        "coverage": copy.deepcopy(parent["coverage"]),
        "previous_selection_fingerprint_sha256": (
            REPAIR02_APPROVED_SELECTION_FINGERPRINT
        ),
        "current_selection_fingerprint_sha256": (
            REPAIR02_APPROVED_SELECTION_FINGERPRINT
        ),
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": old_repository[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": old_repository[
            "source_sha256s_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": old_repository["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return [failure, preregistration]


def _expected_repair04_trial_rows(
    applied_at: str,
    previous_repository: dict,
    current_identity: dict,
    repair04: dict,
    coverage: dict,
    cumulative_quarantined_objects: list[dict],
    failed_resource: dict,
) -> list[dict]:
    """Reproduce the registrar's exact two-row append-only trial suffix."""
    common = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(REPAIR04_RESEARCH_TRIAL_IDS),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": "repair-03",
    }
    failure = {
        **common,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_04",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR03",
        "failure_class": REPAIR04_FINDING,
        "failure_disposition": REPAIR04_FAILURE_DISPOSITION,
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "error_type": REPAIR04_APPROVED_FAILED_ERROR_TYPE,
        "hypothesis_conclusion": "NONE",
        "failed_state_path": REPAIR04_FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair04.get("failed_state_sha256"),
        "failed_resource_receipt_path": REPAIR04_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair04.get(
            "failed_resource_receipt_sha256"
        ),
        "failed_scratch_receipt_path": REPAIR04_FAILED_SCRATCH_ARCHIVE,
        "failed_scratch_receipt_sha256": repair04.get(
            "failed_scratch_receipt_sha256"
        ),
        "failed_input_identity_path": REPAIR04_FAILED_INPUT_ARCHIVE,
        "failed_input_identity_sha256": repair04.get(
            "failed_input_identity_sha256"
        ),
        "failed_input_fingerprint": repair04.get("failed_input_fingerprint"),
        "resource_label": REPAIR03_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "resource_metrics": {
            "wall_seconds": failed_resource.get("wall_seconds"),
            "peak_process_tree_rss_kib_polled": failed_resource.get(
                "peak_process_tree_rss_kib_polled"
            ),
            "cumulative_children_max_rss_kib": failed_resource.get(
                "cumulative_children_max_rss_kib"
            ),
            "peak_temp_bytes_polled": failed_resource.get(
                "peak_temp_bytes_polled"
            ),
            "minimum_disk_free_bytes_polled": failed_resource.get(
                "minimum_disk_free_bytes_polled"
            ),
        },
        "execution_commit": previous_repository.get("execution_commit"),
        "source_manifest_sha256": previous_repository.get(
            "source_manifest_sha256"
        ),
        "source_sha256s_sha256": previous_repository.get(
            "source_sha256s_sha256"
        ),
        "query_set_sha256": previous_repository.get("query_set_sha256"),
    }
    preregistration = {
        **common,
        "trial_registration_id": "RFQ_RESOURCE_CONTRACT_REPAIR_04",
        "record_type": "RESOURCE_CONTRACT_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR04_PREREGISTRATION",
        "finding": REPAIR04_FINDING,
        "registration_change_class": REPAIR04_CHANGE_CLASS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": REPAIR04_RETRY_REQUIREMENT,
        "resource_contract_path": REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": repair04.get("resource_contract_sha256"),
        "authority_basis_path": REPAIR04_AUTHORITY_BASIS,
        "authority_basis_sha256": repair04.get("authority_basis_sha256"),
        "previous_runtime": dict(REPAIR04_PREVIOUS_RESOURCE_CONTRACT),
        "current_runtime": dict(REPAIR04_RUNTIME_CONTRACT),
        "expected_success_resource": {
            "label": REPAIR04_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR04_SUCCESS_RESOURCE_PATH,
        },
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": cumulative_quarantined_objects,
        "coverage": coverage,
        "previous_selection_fingerprint_sha256": repair04.get(
            "previous_selection_fingerprint_sha256"
        ),
        "current_selection_fingerprint_sha256": repair04.get(
            "current_selection_fingerprint_sha256"
        ),
        "previous_execution_commit": previous_repository.get("execution_commit"),
        "current_execution_commit": current_identity.get("execution_commit"),
        "previous_source_manifest_sha256": previous_repository.get(
            "source_manifest_sha256"
        ),
        "previous_source_sha256s_sha256": previous_repository.get(
            "source_sha256s_sha256"
        ),
        "current_source_manifest_sha256": current_identity.get(
            "source_manifest_sha256"
        ),
        "current_source_sha256s_sha256": current_identity.get(
            "source_sha256s_sha256"
        ),
        "previous_query_set_sha256": previous_repository.get("query_set_sha256"),
        "current_query_set_sha256": current_identity.get("query_set_sha256"),
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return [failure, preregistration]


def _expected_repair04_resource_contract(
    run_id: str,
    applied_at: str,
    repair04: dict,
) -> dict:
    return {
        "schema_version": REPAIR04_RESOURCE_SCHEMA,
        "run_id": run_id,
        "created_at_utc": applied_at,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "parent_repair_id": "repair-03",
        "finding": REPAIR04_FINDING,
        "failure_disposition": REPAIR04_FAILURE_DISPOSITION,
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "change_class": REPAIR04_CHANGE_CLASS,
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
            "state_path": REPAIR04_FAILED_STATE_ARCHIVE,
            "state_sha256": repair04.get("failed_state_sha256"),
            "resource_path": REPAIR04_FAILED_RESOURCE_ARCHIVE,
            "resource_sha256": repair04.get("failed_resource_receipt_sha256"),
            "input_identity_path": REPAIR04_FAILED_INPUT_ARCHIVE,
            "input_identity_sha256": repair04.get("failed_input_identity_sha256"),
            "scratch_receipt_path": REPAIR04_FAILED_SCRATCH_ARCHIVE,
            "scratch_receipt_sha256": repair04.get(
                "failed_scratch_receipt_sha256"
            ),
            "error_type": REPAIR04_APPROVED_FAILED_ERROR_TYPE,
            "error": REPAIR04_APPROVED_FAILED_ERROR,
            "return_code": 1,
        },
        "w09": {
            "instance_id": EXPECTED_INSTANCE,
            "region": EXPECTED_REGION,
            "role": EXPECTED_ROLE,
            "memtotal_bytes": REPAIR04_MEMTOTAL_BYTES,
            "same_existing_instance_required": True,
            "resize_allowed": False,
            "replacement_instance_allowed": False,
        },
        "previous_runtime": dict(REPAIR04_PREVIOUS_RESOURCE_CONTRACT),
        "current_runtime": dict(REPAIR04_RUNTIME_CONTRACT),
        "memory_safety": {
            "memory_limit_bytes_decimal": 46_000_000_000,
            "memtotal_bytes": REPAIR04_MEMTOTAL_BYTES,
            "memory_limit_fraction_of_memtotal": (
                46_000_000_000 / REPAIR04_MEMTOTAL_BYTES
            ),
            "memory_limit_percent_of_memtotal_rounded_2dp": 69.49,
            "unallocated_memtotal_bytes": (
                REPAIR04_MEMTOTAL_BYTES - 46_000_000_000
            ),
            "threads_reduced_from": 8,
            "threads_reduced_to": 4,
        },
        "disk_safety": {
            "current_free_after_preserving_all_failed_scratch_bytes": (
                REPAIR04_CURRENT_FREE_AFTER_PRESERVE_BYTES
            ),
            "previous_attempt_disk_free_before_bytes": (
                REPAIR04_PRIOR_DISK_FREE_BEFORE_BYTES
            ),
            "previous_attempt_minimum_disk_free_bytes": (
                REPAIR04_APPROVED_RESOURCE_MIN_FREE_BYTES
            ),
            "previous_attempt_maximum_disk_delta_bytes": (
                REPAIR04_PRIOR_MAX_DISK_DELTA_BYTES
            ),
            "projected_minimum_disk_free_bytes_using_previous_delta": (
                REPAIR04_PROJECTED_MIN_FREE_BYTES
            ),
            "previous_attempt_peak_temp_bytes": (
                REPAIR04_APPROVED_RESOURCE_PEAK_TEMP_BYTES
            ),
            "new_max_temp_size_bytes_decimal": 70_000_000_000,
            "new_min_free_bytes": 100 * 1024**3,
            "failed_scratch_deletion_allowed": False,
        },
        "expected_command": _repair04_retry_command(run_id),
        "fresh_scratch_required": True,
        "resume_allowed": False,
        "failed_scratch_preservation_required": True,
        "expected_success_resource": {
            "label": REPAIR04_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR04_SUCCESS_RESOURCE_PATH,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
    }


def _validate_repair04_contract_artifacts(
    run_dir: Path,
    repair04: dict,
    applied_at: str,
) -> tuple[dict, dict]:
    if repair04.get("resource_contract_path") != REPAIR04_RESOURCE_CONTRACT_PATH:
        raise RFQStageError("repair-04 resource-contract path mismatch")
    contract_path = _require_run_relative_file(
        run_dir, REPAIR04_RESOURCE_CONTRACT_PATH, "repair-04 resource contract"
    )
    contract_sha = sha256(contract_path)
    if repair04.get("resource_contract_sha256") != contract_sha:
        raise RFQStageError("repair-04 resource-contract SHA mismatch")
    contract = read_json_object(contract_path, "repair-04 resource contract")
    expected_contract = _expected_repair04_resource_contract(
        run_dir.name, applied_at, repair04
    )
    if contract != expected_contract:
        raise RFQStageError("repair-04 resource contract content mismatch")

    if repair04.get("authority_basis_path") != REPAIR04_AUTHORITY_BASIS:
        raise RFQStageError("repair-04 authority path mismatch")
    authority_path = _require_run_relative_file(
        run_dir, REPAIR04_AUTHORITY_BASIS, "repair-04 authority basis"
    )
    authority_sha = sha256(authority_path)
    if repair04.get("authority_basis_sha256") != authority_sha:
        raise RFQStageError("repair-04 authority SHA mismatch")
    authority = read_json_object(authority_path, "repair-04 authority basis")
    expected_authority = {
        "schema_version": REPAIR04_AUTHORITY_SCHEMA,
        "run_id": run_dir.name,
        "recorded_at_utc": applied_at,
        "mission_sha256": EXPECTED_MISSION_SHA256,
        "authority_class": REPAIR04_AUTHORITY_CLASS,
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
        "resource_contract_path": REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": contract_sha,
        "existing_w09_instance_id": EXPECTED_INSTANCE,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }
    if authority != expected_authority:
        raise RFQStageError("repair-04 authority basis mismatch")
    return contract, authority


def _validate_repair04_source_attestation(
    run_dir: Path,
    repair04: dict,
) -> None:
    source_mode = repair04.get("source_verification_mode")
    if source_mode == REPAIR04_GIT_SOURCE_MODE:
        return
    if source_mode != REPAIR04_SNAPSHOT_SOURCE_MODE:
        raise RFQStageError("repair-04 source verification mode is invalid")
    active = _require_run_relative_file(
        run_dir,
        REPAIR04_SOURCE_ATTESTATION,
        "repair-04 active source snapshot attestation",
    )
    archived = _require_run_relative_file(
        run_dir,
        REPAIR04_SOURCE_ATTESTATION_ARCHIVE,
        "repair-04 archived source snapshot attestation",
    )
    digest = sha256(active)
    attestation = read_json_object(active, "repair-04 source snapshot attestation")
    if (
        active.read_bytes() != archived.read_bytes()
        or sha256(archived) != digest
        or repair04.get("source_snapshot_attestation_active_path")
            != REPAIR04_SOURCE_ATTESTATION
        or repair04.get("source_snapshot_attestation_path")
            != REPAIR04_SOURCE_ATTESTATION_ARCHIVE
        or repair04.get("source_snapshot_attestation_sha256") != digest
        or set(attestation) != {
            "schema_version", "run_id", "mission_sha256",
            "parent_execution_commit", "execution_commit",
            "direct_parent_verified", "git_tree", "source_relative",
            "source_tree_clean_at_attestation", "source_manifest_sha256",
            "source_sha256s_sha256", "commit_changed_paths", "attested_at_utc",
        }
    ):
        raise RFQStageError("repair-04 source snapshot attestation binding mismatch")
    attested_at = attestation.get("attested_at_utc")
    try:
        parsed = dt.datetime.fromisoformat(
            attested_at[:-1] + "+00:00"
            if isinstance(attested_at, str) and attested_at.endswith("Z")
            else str(attested_at)
        )
    except ValueError as exc:
        raise RFQStageError(
            "repair-04 source snapshot attestation timestamp is invalid"
        ) from exc
    git_tree = attestation.get("git_tree")
    expected_nested = {
        "schema_version": REPAIR04_SOURCE_ATTESTATION_SCHEMA,
        "execution_commit": repair04.get("current_execution_commit"),
        "git_tree": git_tree,
        "direct_parent_verified": True,
        "source_relative": REPAIR04_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "commit_changed_paths": REPAIR04_CHANGED_PATHS,
    }
    if (
        attestation.get("schema_version") != REPAIR04_SOURCE_ATTESTATION_SCHEMA
        or attestation.get("run_id") != run_dir.name
        or attestation.get("mission_sha256") != EXPECTED_MISSION_SHA256
        or attestation.get("parent_execution_commit")
            != repair04.get("previous_execution_commit")
        or attestation.get("execution_commit")
            != repair04.get("current_execution_commit")
        or attestation.get("direct_parent_verified") is not True
        or not isinstance(git_tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", git_tree) is None
        or attestation.get("source_relative") != REPAIR04_SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256")
            != repair04.get("current_source_manifest_sha256")
        or attestation.get("source_sha256s_sha256")
            != repair04.get("current_source_sha256s_sha256")
        or attestation.get("commit_changed_paths") != REPAIR04_CHANGED_PATHS
        or parsed.utcoffset() != dt.timedelta(0)
        or repair04.get("source_snapshot_attestation") != expected_nested
    ):
        raise RFQStageError("repair-04 source snapshot attestation mismatch")


def _validate_repair04_failed_boundary(
    run_dir: Path,
    repair04: dict,
    base_result: dict,
    cycle1_duckdb_binding: dict,
) -> dict:
    """Bind attempt-04's OOM and preserved scratch without opening a result."""
    active_archive_pairs = (
        ("REPORT/tables/RFQ_FULL_STAGE_STATE.json", REPAIR04_FAILED_STATE_ARCHIVE),
        (REPAIR03_SUCCESS_RESOURCE_PATH, REPAIR04_FAILED_RESOURCE_ARCHIVE),
        (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json",
            REPAIR04_FAILED_SCRATCH_ARCHIVE,
        ),
        (
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
            REPAIR04_FAILED_INPUT_ARCHIVE,
        ),
        (CYCLE1_DUCKDB_BINDING, REPAIR04_CYCLE1_BINDING_ARCHIVE),
    )
    for active_relative, archive_relative in active_archive_pairs:
        active = _require_run_relative_file(
            run_dir, active_relative, "repair-04 active failed-attempt evidence"
        )
        archived = _require_run_relative_file(
            run_dir, archive_relative, "repair-04 archived failed-attempt evidence"
        )
        if active.read_bytes() != archived.read_bytes():
            raise RFQStageError(
                f"repair-04 active/archive evidence differs: {active_relative}"
            )

    approved = (
        (
            REPAIR04_FAILED_STATE_ARCHIVE,
            "failed_state_sha256",
            REPAIR04_APPROVED_FAILED_STATE_SHA256,
        ),
        (
            REPAIR04_FAILED_RESOURCE_ARCHIVE,
            "failed_resource_receipt_sha256",
            REPAIR04_APPROVED_FAILED_RESOURCE_SHA256,
        ),
        (
            REPAIR04_FAILED_SCRATCH_ARCHIVE,
            "failed_scratch_receipt_sha256",
            REPAIR04_APPROVED_FAILED_SCRATCH_RECEIPT_SHA256,
        ),
        (
            REPAIR04_FAILED_INPUT_ARCHIVE,
            "failed_input_identity_sha256",
            REPAIR04_APPROVED_FAILED_INPUT_SHA256,
        ),
    )
    for relative, field, expected_sha in approved:
        if repair04.get(field) != expected_sha:
            raise RFQStageError(f"repair-04 approved evidence identity changed: {field}")
        _artifact_sha(run_dir, relative, expected_sha, f"repair-04 {field}")

    state = read_json_object(
        run_dir / REPAIR04_FAILED_STATE_ARCHIVE, "repair-04 failed state"
    )
    resource = read_json_object(
        run_dir / REPAIR04_FAILED_RESOURCE_ARCHIVE, "repair-04 failed resource"
    )
    failed_input = read_json_object(
        run_dir / REPAIR04_FAILED_INPUT_ARCHIVE, "repair-04 failed input"
    )
    scratch_receipt = read_json_object(
        run_dir / REPAIR04_FAILED_SCRATCH_ARCHIVE,
        "repair-04 failed scratch receipt",
    )

    expected_resource = {
        "label": REPAIR03_SUCCESS_RESOURCE_LABEL,
        "path": REPAIR03_SUCCESS_RESOURCE_PATH,
    }
    if (
        set(state) != {
            "schema", "run_id", "started_at_utc", "status", "input_fingerprint",
            "scratch", "resume", "repair_chain", "failed_attempt_bindings",
            "quarantine_gap_plan", "expected_success_resource",
            "inner_payload_parser_contract", "registered_rfq_query_sha256",
            "failed_at_utc", "error_type", "error", "next_required_authority",
        }
        or state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("run_id") != run_dir.name
        or state.get("started_at_utc") != "2026-07-15T16:45:59Z"
        or state.get("failed_at_utc") != "2026-07-15T17:00:32Z"
        or state.get("status")
            != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or state.get("resume") is not False
        or state.get("next_required_authority")
            != "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or state.get("input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or state.get("repair_chain") != base_result["repair_chain"]
        or state.get("failed_attempt_bindings")
            != base_result["failed_attempt_bindings"]
        or state.get("quarantine_gap_plan")
            != quarantine_gap_plan(base_result)
        or state.get("expected_success_resource") != expected_resource
        or state.get("inner_payload_parser_contract")
            != base_result["inner_payload_parser_contract"]
        or state.get("registered_rfq_query_sha256")
            != base_result["registered_rfq_query_sha256"]
        or state.get("error_type") != REPAIR04_APPROVED_FAILED_ERROR_TYPE
        or state.get("error") != REPAIR04_APPROVED_FAILED_ERROR
        or state.get("scratch")
            != f"/srv/w09-research/runs/{run_dir.name}/cache/rfq_full_scratch.duckdb"
        or "phase" in state
    ):
        raise RFQStageError("repair-04 failed state is not the approved OOM boundary")

    expected_resource_fields = {
        "schema_version", "label", "command", "started_at_utc",
        "completed_at_utc", "return_code", "wall_seconds", "cpu_user_seconds",
        "cpu_system_seconds", "cpu_hours", "peak_process_tree_rss_kib_polled",
        "cumulative_children_max_rss_kib", "rss_note", "poll_samples",
        "poll_seconds", "disk_free_before_bytes", "disk_free_after_bytes",
        "minimum_disk_free_bytes_polled", "peak_temp_bytes_polled",
        "peak_stage_cache_bytes_polled", "s3_bytes_read_by_analysis", "s3_note",
        "cost_rate_usd_per_hour", "estimated_compute_cost_usd",
    }
    if (
        set(resource) != expected_resource_fields
        or resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != REPAIR03_SUCCESS_RESOURCE_LABEL
        or resource.get("command") != _repair04_failed_command(run_dir.name)
        or resource.get("return_code") != 1
        or resource.get("started_at_utc") != "2026-07-15T16:45:24.076899Z"
        or resource.get("completed_at_utc") != "2026-07-15T17:00:34.075652Z"
        or resource.get("wall_seconds") != REPAIR04_APPROVED_RESOURCE_WALL_SECONDS
        or resource.get("peak_process_tree_rss_kib_polled")
            != REPAIR04_APPROVED_RESOURCE_PEAK_RSS_KIB
        or resource.get("peak_temp_bytes_polled")
            != REPAIR04_APPROVED_RESOURCE_PEAK_TEMP_BYTES
        or resource.get("minimum_disk_free_bytes_polled")
            != REPAIR04_APPROVED_RESOURCE_MIN_FREE_BYTES
        or resource.get("disk_free_before_bytes") != 153_086_521_344
        or resource.get("disk_free_after_bytes") != 124_324_532_224
        or resource.get("s3_bytes_read_by_analysis") != 0
    ):
        raise RFQStageError("repair-04 failed resource receipt mismatch")

    expected_consumed = [
        {key: row[key] for key in ("key", "sha256", "size", "bound_release_ids")}
        for row in base_result["objects_detail"]
    ]
    if (
        set(failed_input) != {
            "consumed_bytes", "consumed_logical_bindings",
            "consumed_object_set_sha256", "consumed_objects",
            "consumed_unique_objects", "coverage_status", "cycle1_duckdb_binding",
            "deduplicated_overlapping_objects", "expected_success_resource",
            "failed_attempt_binding", "failed_attempt_bindings", "full_object_coverage",
            "inner_payload_parser_contract", "line_salvage",
            "logical_manifest_bindings_total", "manifest_object_set_sha256",
            "quarantine_details", "quarantine_gap_plan", "quarantine_reasons",
            "quarantined_bytes", "quarantined_logical_bindings",
            "quarantined_object_set_sha256", "quarantined_unique_objects",
            "registered_rfq_query_sha256", "release_ids", "releases",
            "repair_chain", "run_id", "schema", "selection_fingerprint_sha256",
            "unique_bytes_total", "unique_objects_total", "whole_object_quarantine",
        }
        or failed_input.get("schema") != "rfq-full-input-identity-v3"
        or failed_input.get("run_id") != run_dir.name
        or failed_input.get("release_ids") != list(RELEASE_IDS)
        or failed_input.get("releases") != base_result["releases"]
        or failed_input.get("selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or failed_input.get("coverage_status")
            != base_result["coverage_status"]
        or failed_input.get("full_object_coverage") is not False
        or failed_input.get("whole_object_quarantine") is not True
        or failed_input.get("line_salvage") is not False
        or failed_input.get("logical_manifest_bindings_total")
            != base_result["logical_manifest_bindings"]
        or failed_input.get("unique_objects_total") != base_result["objects"]
        or failed_input.get("unique_bytes_total") != base_result["bytes"]
        or failed_input.get("manifest_object_set_sha256")
            != base_result["path_size_fingerprint_sha256"]
        or failed_input.get("consumed_unique_objects")
            != base_result["consumed_unique_objects"]
        or failed_input.get("consumed_logical_bindings")
            != base_result["consumed_logical_bindings"]
        or failed_input.get("consumed_bytes") != base_result["consumed_bytes"]
        or failed_input.get("consumed_object_set_sha256")
            != base_result["consumed_object_set_sha256"]
        or failed_input.get("quarantined_unique_objects")
            != base_result["quarantined_unique_objects"]
        or failed_input.get("quarantined_logical_bindings")
            != base_result["quarantined_logical_bindings"]
        or failed_input.get("quarantined_bytes")
            != base_result["quarantined_bytes"]
        or failed_input.get("quarantined_object_set_sha256")
            != base_result["quarantined_object_set_sha256"]
        or failed_input.get("quarantine_reasons")
            != base_result["quarantine_reasons"]
        or failed_input.get("quarantine_details")
            != base_result["quarantine_details"]
        or failed_input.get("quarantine_gap_plan")
            != quarantine_gap_plan(base_result)
        or failed_input.get("deduplicated_overlapping_objects")
            != base_result["deduplicated_overlapping_objects"]
        or failed_input.get("consumed_objects") != expected_consumed
        or failed_input.get("failed_attempt_binding") is not None
        or failed_input.get("repair_chain") != base_result["repair_chain"]
        or failed_input.get("failed_attempt_bindings")
            != base_result["failed_attempt_bindings"]
        or failed_input.get("expected_success_resource") != expected_resource
        or failed_input.get("inner_payload_parser_contract")
            != base_result["inner_payload_parser_contract"]
        or failed_input.get("registered_rfq_query_sha256")
            != base_result["registered_rfq_query_sha256"]
        or failed_input.get("cycle1_duckdb_binding")
            != cycle1_duckdb_binding
    ):
        raise RFQStageError("repair-04 failed v3 input changed inherited semantics")

    preserved = _require_run_relative_file(
        run_dir, REPAIR04_PRESERVED_SCRATCH, "repair-04 preserved failed scratch"
    )
    active_scratch = run_dir / "cache/rfq_full_scratch.duckdb"
    if (
        set(scratch_receipt) != {
            "schema_version", "run_id", "original_scratch_path",
            "preserved_scratch_path", "bytes", "sha256", "mtime_utc",
            "input_fingerprint", "disposition", "resume_allowed",
        }
        or scratch_receipt.get("schema_version")
            != "rfq-failed-scratch-receipt-v1"
        or scratch_receipt.get("run_id") != run_dir.name
        or scratch_receipt.get("input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or scratch_receipt.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch_receipt.get("resume_allowed") is not False
        or scratch_receipt.get("original_scratch_path")
            != f"/srv/w09-research/runs/{run_dir.name}/cache/rfq_full_scratch.duckdb"
        or scratch_receipt.get("preserved_scratch_path")
            != f"/srv/w09-research/runs/{run_dir.name}/{REPAIR04_PRESERVED_SCRATCH}"
        or scratch_receipt.get("bytes") != REPAIR04_APPROVED_FAILED_SCRATCH_BYTES
        or scratch_receipt.get("sha256") != REPAIR04_APPROVED_FAILED_SCRATCH_SHA256
        or scratch_receipt.get("mtime_utc")
            != REPAIR04_APPROVED_FAILED_SCRATCH_MTIME
        or preserved.stat().st_ino != REPAIR04_APPROVED_FAILED_SCRATCH_INODE
        or preserved.stat().st_size != REPAIR04_APPROVED_FAILED_SCRATCH_BYTES
        or sha256(preserved) != REPAIR04_APPROVED_FAILED_SCRATCH_SHA256
        or active_scratch.exists()
        or Path(str(active_scratch) + ".wal").exists()
    ):
        raise RFQStageError("repair-04 failed scratch/fresh-scratch boundary mismatch")
    return {
        "state": state,
        "resource": resource,
        "input": failed_input,
        "scratch_receipt": scratch_receipt,
    }


def _apply_repair04_resource_contract(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    *,
    descendant_boundary_root: Path | None = None,
) -> dict:
    """Validate the append-only resource-only repair-04 registration."""
    repairs = manifest.get("data_integrity_repairs")
    repair_ids = [
        row.get("repair_id") if isinstance(row, dict) else None
        for row in (repairs if isinstance(repairs, list) else [])
    ]
    if (
        not isinstance(repairs, list)
        or len(repairs) != 4
        or repair_ids != ["repair-01", "repair-02", "repair-03", "repair-04"]
    ):
        raise RFQStageError("RFQ repair-04 chain must be exactly repair-01/02/03/04")
    repair01, repair02, repair03, repair04 = repairs
    repair04_active_root = descendant_boundary_root or run_dir
    record_fields = {
        "schema_version", "repair_id", "parent_repair_id", "applied_at_utc",
        "pre_repair_status", "post_repair_status", "failed_stage_status",
        "rfq_result_state", "retry_requirement", "finding",
        "failure_disposition", "registration_change_class",
        "source_verification_mode", "quarantine_policy",
        "previous_repair_registration_path",
        "previous_repair_registration_sha256", "previous_repair_record_sha256",
        "failed_state_path", "failed_state_sha256",
        "failed_resource_receipt_path", "failed_resource_receipt_sha256",
        "failed_scratch_receipt_path", "failed_scratch_receipt_sha256",
        "failed_input_identity_path", "failed_input_identity_sha256",
        "resource_contract_path", "resource_contract_sha256",
        "authority_basis_path", "authority_basis_sha256",
        "cycle1_binding_path", "cycle1_binding_sha256",
        "w09_attestation_path", "w09_attestation_sha256",
        "registered_rfq_query_sha256", "parent_registered_rfq_query_sha256",
        "parser_contract_path", "parser_contract_sha256", "parser_contract",
        "authority_basis", "failed_attempt", "expected_success_resource",
        "resource_contract", "newly_quarantined_objects",
        "cumulative_quarantined_objects", "coverage",
        "selection_identity_unchanged", "previous_selection_fingerprint_sha256",
        "current_selection_fingerprint_sha256", "failed_input_fingerprint",
        "cycle1_duckdb_binding", "previous_execution_commit",
        "current_execution_commit", "initial_repository_identity",
        "previous_repository_identity", "current_repository_identity",
        "repository_identity_chain", "previous_source_manifest_sha256",
        "current_source_manifest_sha256", "previous_source_sha256s_sha256",
        "current_source_sha256s_sha256", "previous_query_set_sha256",
        "current_query_set_sha256", "core_result_disposition",
        "core_results_recomputed", "core_result_artifacts", "trial_registry",
        "data_selection_change", "parser_contract_change",
        "query_semantics_change", "quarantine_change", "hypothesis_design_change",
        "threshold_feature_test_or_hypothesis_status_changed", "archive_path",
        "archive_inventory", "transaction_journal_path",
        "transaction_journal_sha256", "repair_receipt_path",
        "repair_receipt_sha256",
    }
    source_mode = repair04.get("source_verification_mode")
    if source_mode == REPAIR04_SNAPSHOT_SOURCE_MODE:
        record_fields.update({
            "source_snapshot_attestation_active_path",
            "source_snapshot_attestation_path",
            "source_snapshot_attestation_sha256",
            "source_snapshot_attestation",
        })
    if set(repair04) != record_fields:
        raise RFQStageError("repair-04 registration record field set mismatch")
    if (
        manifest.get("status") != REPAIR04_STATUS
        or manifest.get("registration_state") != REPAIR04_REGISTRATION_STATE
        or repair04.get("schema_version") != REPAIR04_SCHEMA
        or repair04.get("repair_id") != "repair-04"
        or repair04.get("parent_repair_id") != "repair-03"
        or repair04.get("pre_repair_status") != REPAIR03_STATUS
        or repair04.get("post_repair_status") != REPAIR04_STATUS
        or repair04.get("failed_stage_status")
            != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or repair04.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair04.get("retry_requirement") != REPAIR04_RETRY_REQUIREMENT
        or repair04.get("finding") != REPAIR04_FINDING
        or repair04.get("failure_disposition") != REPAIR04_FAILURE_DISPOSITION
        or repair04.get("registration_change_class") != REPAIR04_CHANGE_CLASS
        or repair04.get("quarantine_policy")
            != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
    ):
        raise RFQStageError("repair-04 append-chain identity/governance mismatch")
    applied_at = repair04.get("applied_at_utc")
    try:
        parsed_applied = dt.datetime.fromisoformat(
            applied_at[:-1] + "+00:00"
            if isinstance(applied_at, str) and applied_at.endswith("Z")
            else str(applied_at)
        )
    except ValueError as exc:
        raise RFQStageError("repair-04 applied timestamp is invalid") from exc
    if parsed_applied.utcoffset() != dt.timedelta(0):
        raise RFQStageError("repair-04 applied timestamp is not UTC")

    _validate_repair04_source_attestation(run_dir, repair04)
    registration_path = _require_run_relative_file(
        run_dir, REPAIR04_REGISTRATION, "repair-04 registration receipt"
    )
    registration_sha = sha256(registration_path)
    registration = read_json_object(
        registration_path, "repair-04 registration receipt"
    )
    without_self = {
        key: value for key, value in repair04.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair04.get("repair_receipt_path") != REPAIR04_REGISTRATION
        or repair04.get("repair_receipt_sha256") != registration_sha
        or registration != without_self
    ):
        raise RFQStageError("repair-04 registration receipt binding mismatch")

    pre_root = run_dir / REPAIR04_PRE_ROOT
    repair_root = run_dir / REPAIR04_ROOT
    if (
        repair04.get("archive_path") != REPAIR04_PRE_ROOT
        or repair04.get("archive_inventory") != _archive_inventory(pre_root)
    ):
        raise RFQStageError("repair-04 pre-repair archive inventory mismatch")
    pre_manifest_path = _require_run_relative_file(
        run_dir, f"{REPAIR04_PRE_ROOT}/RUN_MANIFEST.json",
        "repair-04 archived repair-03 manifest",
    )
    pre_manifest = read_json_object(
        pre_manifest_path, "repair-04 archived repair-03 manifest"
    )
    if (
        pre_manifest.get("run_id") != run_dir.name
        or pre_manifest.get("status") != REPAIR03_STATUS
        or pre_manifest.get("registration_state") != REPAIR03_REGISTRATION_STATE
        or pre_manifest.get("data_integrity_repairs") != repairs[:3]
    ):
        raise RFQStageError("repair-04 archive is not the exact repair-03 boundary")
    base_result = _apply_repair03_parser_contract(
        run_dir,
        pre_manifest,
        inputs,
        descendant_boundary_root=pre_root,
    )

    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": inputs["objects"],
        "full_logical_manifest_bindings": inputs["logical_manifest_bindings"],
        "full_unique_bytes": inputs["bytes"],
        "retained_unique_objects": base_result["consumed_unique_objects"],
        "retained_logical_manifest_bindings": base_result[
            "consumed_logical_bindings"
        ],
        "retained_bytes": base_result["consumed_bytes"],
        "quarantined_unique_objects": base_result["quarantined_unique_objects"],
        "quarantined_logical_manifest_bindings": base_result[
            "quarantined_logical_bindings"
        ],
        "quarantined_bytes": base_result["quarantined_bytes"],
        "full_object_set_sha256": inputs["path_size_fingerprint_sha256"],
        "retained_object_set_sha256": base_result["consumed_object_set_sha256"],
        "quarantined_object_set_sha256": base_result[
            "quarantined_object_set_sha256"
        ],
        "retained_selection_fingerprint_sha256": base_result[
            "selection_fingerprint_sha256"
        ],
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    if (
        repair04.get("selection_identity_unchanged") is not True
        or repair04.get("previous_selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or repair04.get("current_selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or repair04.get("failed_input_fingerprint")
            != base_result["selection_fingerprint_sha256"]
        or repair04.get("newly_quarantined_objects") != []
        or repair04.get("cumulative_quarantined_objects")
            != repair03.get("cumulative_quarantined_objects")
        or repair04.get("coverage") != coverage
        or repair04.get("cycle1_duckdb_binding")
            != repair03.get("cycle1_duckdb_binding")
        or repair04.get("core_result_disposition")
            != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair04.get("core_results_recomputed") is not False
        or repair04.get("core_result_artifacts")
            != repair03.get("core_result_artifacts")
        or repair04.get("data_selection_change") != "NONE"
        or repair04.get("parser_contract_change") != "NONE"
        or repair04.get("query_semantics_change") != "NONE"
        or repair04.get("quarantine_change") != "NONE"
        or repair04.get("hypothesis_design_change") != "NONE"
        or repair04.get("threshold_feature_test_or_hypothesis_status_changed")
            is not False
        or repair04.get("parser_contract_path")
            != repair03.get("parser_contract_path")
        or repair04.get("parser_contract_sha256")
            != repair03.get("parser_contract_sha256")
        or repair04.get("parser_contract") != repair03.get("parser_contract")
        or repair04.get("parent_registered_rfq_query_sha256")
            != repair03.get("registered_rfq_query_sha256")
    ):
        raise RFQStageError("repair-04 changed inherited research semantics")

    boundary = _validate_repair04_failed_boundary(
        run_dir,
        repair04,
        base_result,
        repair03.get("cycle1_duckdb_binding"),
    )
    contract, authority = _validate_repair04_contract_artifacts(
        run_dir, repair04, applied_at
    )
    expected_contract_record = {
        "path": REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": repair04["resource_contract_sha256"],
        "schema_version": REPAIR04_RESOURCE_SCHEMA,
        "previous_runtime": dict(REPAIR04_PREVIOUS_RESOURCE_CONTRACT),
        "current_runtime": dict(REPAIR04_RUNTIME_CONTRACT),
        "expected_command": _repair04_retry_command(run_dir.name),
        "w09_memtotal_bytes": REPAIR04_MEMTOTAL_BYTES,
        "memory_limit_percent_of_memtotal_rounded_2dp": 69.49,
    }
    expected_authority_record = {
        "path": REPAIR04_AUTHORITY_BASIS,
        "sha256": repair04["authority_basis_sha256"],
        "schema_version": REPAIR04_AUTHORITY_SCHEMA,
        "authority_class": REPAIR04_AUTHORITY_CLASS,
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
    }
    if (
        repair04.get("resource_contract") != expected_contract_record
        or repair04.get("authority_basis") != expected_authority_record
        or contract.get("expected_success_resource")
            != repair04.get("expected_success_resource")
        or repair04.get("expected_success_resource")
            != {
                "label": REPAIR04_SUCCESS_RESOURCE_LABEL,
                "path": REPAIR04_SUCCESS_RESOURCE_PATH,
            }
    ):
        raise RFQStageError("repair-04 nested resource/authority record mismatch")

    archived_cycle1_path = _require_run_relative_file(
        run_dir,
        REPAIR04_CYCLE1_BINDING_ARCHIVE,
        "repair-04 archived Cycle-1 binding",
    )
    active_w09_path = _require_run_relative_file(
        run_dir,
        "DATA_INTEGRITY/W09_ATTESTATION.json",
        "repair-04 active W09 attestation",
    )
    archived_w09_path = _require_run_relative_file(
        run_dir,
        REPAIR04_W09_ATTESTATION_ARCHIVE,
        "repair-04 archived W09 attestation",
    )
    if (
        repair04.get("previous_repair_registration_path")
            != repair03.get("repair_receipt_path")
        or repair04.get("previous_repair_registration_sha256")
            != repair03.get("repair_receipt_sha256")
        or repair04.get("previous_repair_record_sha256")
            != _json_payload_sha256(repair03)
        or repair04.get("cycle1_binding_path")
            != REPAIR04_CYCLE1_BINDING_ARCHIVE
        or repair04.get("cycle1_binding_sha256")
            != sha256(archived_cycle1_path)
        or repair04.get("w09_attestation_path")
            != REPAIR04_W09_ATTESTATION_ARCHIVE
        or repair04.get("w09_attestation_sha256")
            != sha256(archived_w09_path)
        or active_w09_path.read_bytes() != archived_w09_path.read_bytes()
    ):
        raise RFQStageError("repair-03/repair-04 evidence chain mismatch")

    pre_repository = pre_manifest.get("repository")
    repository = manifest.get("repository")
    if not isinstance(pre_repository, dict) or not isinstance(repository, dict):
        raise RFQStageError("repair-04 repository transfer is missing")
    pre_identity = _repository_identity(pre_repository)
    current_identity = _repository_identity(repository)
    identity_chain = repository.get("identity_history")
    if (
        not isinstance(identity_chain, list)
        or len(identity_chain) != 5
        or identity_chain != repair04.get("repository_identity_chain")
        or identity_chain[:-1] != pre_repository.get("identity_history")
        or identity_chain[-1] != current_identity
        or repair04.get("initial_repository_identity") != identity_chain[0]
        or repair04.get("previous_repository_identity") != pre_identity
        or repair04.get("current_repository_identity") != current_identity
        or repository.get("registration_repair_id") != "repair-04"
        or repair04.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repair04.get("current_execution_commit")
            != repository.get("execution_commit")
        or repair04.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repair04.get("current_source_manifest_sha256")
            != repository.get("source_manifest_sha256")
        or repair04.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repair04.get("current_source_sha256s_sha256")
            != repository.get("source_sha256s_sha256")
        or repair04.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repair04.get("current_query_set_sha256")
            != repository.get("query_set_sha256")
        or repository.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repository.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repository.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repository.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repository.get("previous_identity") != pre_identity
    ):
        raise RFQStageError("repair-04 repository identity transfer mismatch")
    if (
        sha256(repair04_active_root / "SOURCE_MANIFEST.json")
            != repository.get("source_manifest_sha256")
        or sha256(repair04_active_root / "SOURCE_SHA256SUMS.txt")
            != repository.get("source_sha256s_sha256")
    ):
        raise RFQStageError("repair-04 active source receipts changed")
    query_rows = _validate_query_receipt(
        run_dir, repository, active_root=repair04_active_root
    )
    registered_query_sha = query_rows.get(REPAIR04_EXECUTION_QUERY)
    if registered_query_sha != repair04.get("registered_rfq_query_sha256"):
        raise RFQStageError("repair-04 registered RFQ query SHA mismatch")

    pre_registry_path = _require_run_relative_file(
        run_dir, f"{REPAIR04_PRE_ROOT}/TRIAL_REGISTRY.jsonl",
        "repair-04 archived repair-03 registry",
    )
    active_registry_path = _require_run_relative_file(
        repair04_active_root,
        "TRIAL_REGISTRY.jsonl",
        "repair-04 active registry",
    )
    pre_registry = pre_registry_path.read_bytes()
    active_registry = active_registry_path.read_bytes()
    trial = repair04.get("trial_registry")
    if (
        not isinstance(trial, dict)
        or set(trial) != {
            "previous_sha256", "current_sha256", "previous_bytes", "current_bytes",
            "strict_previous_bytes_prefix", "appended_records",
            "trial_registration_ids",
        }
        or active_registry == pre_registry
        or not active_registry.startswith(pre_registry)
        or trial.get("previous_sha256") != hashlib.sha256(pre_registry).hexdigest()
        or trial.get("current_sha256") != hashlib.sha256(active_registry).hexdigest()
        or trial.get("previous_bytes") != len(pre_registry)
        or trial.get("current_bytes") != len(active_registry)
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids")
            != ["RFQ_FULL_STAGE_ATTEMPT_04", "RFQ_RESOURCE_CONTRACT_REPAIR_04"]
    ):
        raise RFQStageError("repair-04 active trial-registry boundary mismatch")
    suffix_rows = _read_registry_rows(
        active_registry[len(pre_registry):], "repair-04 registry suffix"
    )
    expected_suffix_rows = _expected_repair04_trial_rows(
        applied_at,
        pre_repository,
        current_identity,
        repair04,
        coverage,
        repair04["cumulative_quarantined_objects"],
        boundary["resource"],
    )
    if suffix_rows != expected_suffix_rows:
        raise RFQStageError("repair-04 registry records mismatch")

    journal = read_json_object(
        run_dir / REPAIR04_TRANSACTION_JOURNAL, "repair-04 transaction journal"
    )
    mutations = journal.get("active_mutations")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt", "QUERY_SHA256SUMS.txt",
        *pre_repository.get("query_files", []), "TRIAL_REGISTRY.jsonl",
    ]
    if (
        repair04.get("transaction_journal_path") != REPAIR04_TRANSACTION_JOURNAL
        or repair04.get("transaction_journal_sha256")
            != sha256(run_dir / REPAIR04_TRANSACTION_JOURNAL)
        or set(journal) != {
            "schema_version", "repair_id", "run_id", "state",
            "original_manifest_sha256", "active_mutations", "expected_repair_files",
        }
        or journal.get("schema_version") != "repair04-registration-transaction-v1"
        or journal.get("repair_id") != "repair-04"
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or [row.get("path") if isinstance(row, dict) else None for row in mutations]
            != expected_mutation_paths
        or set(journal.get("expected_repair_files", []))
            != {row["path"] for row in _archive_inventory(repair_root)}
    ):
        raise RFQStageError("repair-04 transaction journal mismatch")
    for mutation in mutations:
        relative = mutation.get("path") if isinstance(mutation, dict) else None
        if (
            not isinstance(mutation, dict)
            or set(mutation) != {
                "path", "original_archive_path", "original_sha256",
                "replacement_sha256", "append_only_registry",
            }
            or mutation.get("original_archive_path")
                != f"{REPAIR04_PRE_ROOT}/{relative}"
            or mutation.get("original_sha256")
                != sha256(_require_run_relative_file(
                    run_dir, f"{REPAIR04_PRE_ROOT}/{relative}",
                    "repair-04 original mutation payload",
                ))
            or mutation.get("replacement_sha256")
                != sha256(_require_run_relative_file(
                    repair04_active_root,
                    relative,
                    "repair-04 replacement mutation payload",
                ))
            or mutation.get("append_only_registry")
                is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise RFQStageError(f"repair-04 transaction mutation mismatch: {relative}")

    expected_failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
        "state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "state_path": REPAIR04_FAILED_STATE_ARCHIVE,
        "state_sha256": repair04["failed_state_sha256"],
        "state_status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "error_type": REPAIR04_APPROVED_FAILED_ERROR_TYPE,
        "error": REPAIR04_APPROVED_FAILED_ERROR,
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "resource_active_path": REPAIR03_SUCCESS_RESOURCE_PATH,
        "resource_path": REPAIR04_FAILED_RESOURCE_ARCHIVE,
        "resource_sha256": repair04["failed_resource_receipt_sha256"],
        "resource_label": REPAIR03_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "resource_command": _repair04_failed_command(run_dir.name),
        "resource_metrics": {
            "started_at_utc": "2026-07-15T16:45:24.076899Z",
            "completed_at_utc": "2026-07-15T17:00:34.075652Z",
            "wall_seconds": REPAIR04_APPROVED_RESOURCE_WALL_SECONDS,
            "peak_process_tree_rss_kib_polled": (
                REPAIR04_APPROVED_RESOURCE_PEAK_RSS_KIB
            ),
            "cumulative_children_max_rss_kib": 40_429_620,
            "peak_temp_bytes_polled": REPAIR04_APPROVED_RESOURCE_PEAK_TEMP_BYTES,
            "minimum_disk_free_bytes_polled": (
                REPAIR04_APPROVED_RESOURCE_MIN_FREE_BYTES
            ),
            "disk_free_before_bytes": REPAIR04_PRIOR_DISK_FREE_BEFORE_BYTES,
            "disk_free_after_bytes": 124_324_532_224,
        },
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json"
        ),
        "scratch_receipt_path": REPAIR04_FAILED_SCRATCH_ARCHIVE,
        "scratch_receipt_sha256": repair04["failed_scratch_receipt_sha256"],
        "preserved_scratch_active_path": REPAIR04_PRESERVED_SCRATCH,
        "preserved_scratch_sha256": REPAIR04_APPROVED_FAILED_SCRATCH_SHA256,
        "preserved_scratch_bytes": REPAIR04_APPROVED_FAILED_SCRATCH_BYTES,
        "preserved_scratch_mtime_utc": REPAIR04_APPROVED_FAILED_SCRATCH_MTIME,
        "preserved_scratch_original_inode": REPAIR04_APPROVED_FAILED_SCRATCH_INODE,
        "input_identity_active_path": "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "input_identity_path": REPAIR04_FAILED_INPUT_ARCHIVE,
        "input_identity_sha256": repair04["failed_input_identity_sha256"],
        "input_identity_schema": "rfq-full-input-identity-v3",
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": REPAIR04_RETRY_REQUIREMENT,
    }
    if repair04.get("failed_attempt") != expected_failed_attempt:
        raise RFQStageError("repair-04 nested failed-attempt record mismatch")
    del boundary, authority, repair01, repair02

    for result_relative in (
        "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json", "REPORT/RFQ_FULL_STAGE.md",
        "cache/rfq_full_catalog.duckdb",
    ):
        if (run_dir / result_relative).exists():
            raise RFQStageError("repair-04 consumer started after an RFQ result existed")

    failed04 = {
        "repair_id": "repair-04",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
        "failed_state_path": REPAIR04_FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair04["failed_state_sha256"],
        "failed_resource_receipt_path": REPAIR04_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair04[
            "failed_resource_receipt_sha256"
        ],
        "failed_scratch_receipt_path": REPAIR04_FAILED_SCRATCH_ARCHIVE,
        "failed_scratch_receipt_sha256": repair04["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": REPAIR04_FAILED_INPUT_ARCHIVE,
        "failed_input_identity_sha256": repair04["failed_input_identity_sha256"],
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "resource_label": REPAIR03_SUCCESS_RESOURCE_LABEL,
        "retry_requirement": REPAIR04_RETRY_REQUIREMENT,
    }
    repair04_chain = {
        "repair_id": "repair-04",
        "registration_path": REPAIR04_REGISTRATION,
        "registration_sha256": registration_sha,
        "resource_contract_path": REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": repair04["resource_contract_sha256"],
        "authority_basis_path": REPAIR04_AUTHORITY_BASIS,
        "authority_basis_sha256": repair04["authority_basis_sha256"],
    }
    resource_binding = {
        "path": REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": repair04["resource_contract_sha256"],
        "schema_version": REPAIR04_RESOURCE_SCHEMA,
        "current_runtime": dict(REPAIR04_RUNTIME_CONTRACT),
    }
    result = dict(base_result)
    result.update({
        "repair_chain": [*base_result["repair_chain"], repair04_chain],
        "failed_attempt_bindings": [
            *base_result["failed_attempt_bindings"], failed04,
        ],
        "expected_success_resource": {
            "label": REPAIR04_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR04_SUCCESS_RESOURCE_PATH,
        },
        "rfq_resource_contract": resource_binding,
        "registered_rfq_query_sha256": registered_query_sha,
    })
    return result


def _validate_repair05_fresh_scratch_absence(run_dir: Path) -> None:
    """Reject existing, dangling, symlinked, or escaped repair-05 scratch paths."""
    cache_dir = run_dir / "cache"
    scratch = cache_dir / "rfq_full_scratch.duckdb"
    if (
        cache_dir.is_symlink()
        or not cache_dir.is_dir()
        or cache_dir.resolve() != run_dir.resolve() / "cache"
    ):
        raise RFQStageError("repair-05 scratch parent is missing or unsafe")
    if os.path.lexists(scratch) or os.path.lexists(Path(str(scratch) + ".wal")):
        raise RFQStageError("repair-05 active scratch or WAL is not absent")


def _validate_repair05_failure_evidence(
    run_dir: Path,
    repair05: dict,
    repair04: dict,
    *,
    descendant_boundary_root: Path | None = None,
) -> dict:
    """Validate attempt-05's pre-evidence KeyError and unchanged boundary."""
    repair05_active_root = descendant_boundary_root or run_dir
    pairs = (
        (REPAIR05_BLOCKER_ACTIVE, REPAIR05_BLOCKER_ARCHIVE),
        (REPAIR04_SUCCESS_RESOURCE_PATH, REPAIR05_FAILED_RESOURCE_ARCHIVE),
        ("REPORT/tables/RFQ_FULL_STAGE_STATE.json", REPAIR05_UNCHANGED_STATE_ARCHIVE),
        (
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
            REPAIR05_UNCHANGED_INPUT_ARCHIVE,
        ),
        (CYCLE1_DUCKDB_BINDING, REPAIR05_CYCLE1_BINDING_ARCHIVE),
    )
    for active_relative, archive_relative in pairs:
        active = _require_run_relative_file(
            repair05_active_root,
            active_relative,
            "repair-05 active pre-evidence boundary",
        )
        archived = _require_run_relative_file(
            run_dir, archive_relative, "repair-05 archived pre-evidence boundary"
        )
        if active.read_bytes() != archived.read_bytes():
            raise RFQStageError(
                f"repair-05 active/archive boundary differs: {active_relative}"
            )

    blocker_path = _require_run_relative_file(
        run_dir, REPAIR05_BLOCKER_ARCHIVE, "repair-05 blocker"
    )
    resource_path = _require_run_relative_file(
        run_dir, REPAIR05_FAILED_RESOURCE_ARCHIVE, "repair-05 failed resource"
    )
    state_path = _require_run_relative_file(
        run_dir, REPAIR05_UNCHANGED_STATE_ARCHIVE, "repair-05 unchanged state"
    )
    input_path = _require_run_relative_file(
        run_dir, REPAIR05_UNCHANGED_INPUT_ARCHIVE, "repair-05 unchanged input"
    )
    if (
        sha256(blocker_path) != REPAIR05_BLOCKER_SHA256
        or sha256(resource_path) != REPAIR05_FAILED_RESOURCE_SHA256
        or sha256(state_path) != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or sha256(input_path) != REPAIR04_APPROVED_FAILED_INPUT_SHA256
        or repair05.get("blocker_path") != REPAIR05_BLOCKER_ARCHIVE
        or repair05.get("blocker_sha256") != REPAIR05_BLOCKER_SHA256
        or repair05.get("failed_resource_receipt_path")
            != REPAIR05_FAILED_RESOURCE_ARCHIVE
        or repair05.get("failed_resource_receipt_sha256")
            != REPAIR05_FAILED_RESOURCE_SHA256
        or repair05.get("unchanged_state_path") != REPAIR05_UNCHANGED_STATE_ARCHIVE
        or repair05.get("unchanged_state_sha256")
            != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or repair05.get("unchanged_input_identity_path")
            != REPAIR05_UNCHANGED_INPUT_ARCHIVE
        or repair05.get("unchanged_input_identity_sha256")
            != REPAIR04_APPROVED_FAILED_INPUT_SHA256
    ):
        raise RFQStageError("repair-05 pre-evidence hashes or paths changed")

    blocker = read_json_object(blocker_path, "repair-05 blocker")
    if (
        blocker.get("schema_version")
            != "rfq-repair04-consumer-pre-evidence-blocker-v1"
        or blocker.get("run_id") != run_dir.name
        or blocker.get("mission_sha256") != EXPECTED_MISSION_SHA256
        or blocker.get("finding") != REPAIR05_FINDING
        or blocker.get("failure_phase") != REPAIR05_FAILURE_PHASE
        or blocker.get("error_type") != "KeyError"
        or blocker.get("error") != "KeyError: 'cycle1_duckdb_binding'"
        or blocker.get("offending_function")
            != "_validate_repair04_failed_boundary"
        or blocker.get("offending_expression")
            != "base_result['cycle1_duckdb_binding']"
        or blocker.get("failed_resource_receipt_path")
            != REPAIR04_SUCCESS_RESOURCE_PATH
        or blocker.get("failed_resource_receipt_sha256")
            != REPAIR05_FAILED_RESOURCE_SHA256
        or blocker.get("parent_registration_sha256")
            != repair04.get("repair_receipt_sha256")
        or blocker.get("repair04_resource_contract_sha256")
            != repair04.get("resource_contract_sha256")
        or blocker.get("registered_query_sha256")
            != repair04.get("registered_rfq_query_sha256")
        or blocker.get("active_state_sha256")
            != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or blocker.get("active_input_identity_sha256")
            != REPAIR04_APPROVED_FAILED_INPUT_SHA256
        or blocker.get("new_state_written") is not False
        or blocker.get("new_input_identity_written") is not False
        or blocker.get("analysis_stage_started") is not False
        or blocker.get("active_scratch_absent") is not True
        or blocker.get("active_wal_absent") is not True
        or blocker.get("dependent_rfq_result_opened") is not False
        or any(
            blocker.get(field) != "NONE"
            for field in (
                "data_selection_change", "parser_contract_change",
                "query_semantics_change", "quarantine_change",
                "hypothesis_design_change",
            )
        )
        or blocker.get("resource_contract_change") != "NONE"
    ):
        raise RFQStageError("repair-05 blocker content mismatch")

    resource = read_json_object(resource_path, "repair-05 failed resource")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != REPAIR04_SUCCESS_RESOURCE_LABEL
        or resource.get("command") != _repair05_retry_command(run_dir.name)
        or resource.get("return_code") != 1
        or resource.get("started_at_utc") != "2026-07-15T18:33:21.211957Z"
        or resource.get("completed_at_utc") != "2026-07-15T18:33:39.330283Z"
        or resource.get("wall_seconds") != REPAIR05_FAILED_RESOURCE_WALL_SECONDS
        or resource.get("s3_bytes_read_by_analysis") != 0
    ):
        raise RFQStageError("repair-05 failed resource receipt mismatch")

    _validate_repair05_fresh_scratch_absence(run_dir)
    if (
        any(
            (run_dir / relative).exists()
            for relative in (
                "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json",
                "REPORT/RFQ_FULL_STAGE.md",
                "cache/rfq_full_catalog.duckdb",
            )
        )
    ):
        raise RFQStageError("repair-05 did not preserve its pre-result boundary")

    cycle_path = _require_run_relative_file(
        run_dir, REPAIR05_CYCLE1_BINDING_ARCHIVE, "repair-05 Cycle-1 binding"
    )
    cycle_sha = sha256(cycle_path)
    registered_cycle = repair04.get("cycle1_duckdb_binding")
    if (
        not isinstance(registered_cycle, dict)
        or repair05.get("cycle1_duckdb_binding") != registered_cycle
        or repair05.get("cycle1_binding_path") != REPAIR05_CYCLE1_BINDING_ARCHIVE
        or repair05.get("cycle1_binding_sha256") != cycle_sha
        or registered_cycle.get("active_sha256") != cycle_sha
    ):
        raise RFQStageError("repair-05 Cycle-1 registered evidence chain mismatch")

    return {
        "blocker": blocker,
        "resource": resource,
        "cycle1_duckdb_binding": registered_cycle,
    }


def _apply_repair05_consumer_wiring(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    *,
    descendant_boundary_root: Path | None = None,
) -> dict:
    """Validate repair-05 and expose only its preregistered runtime bindings."""
    repairs = manifest.get("data_integrity_repairs")
    repair_ids = [
        row.get("repair_id") if isinstance(row, dict) else None
        for row in repairs if isinstance(repairs, list)
    ] if isinstance(repairs, list) else []
    if repair_ids != ["repair-01", "repair-02", "repair-03", "repair-04", "repair-05"]:
        raise RFQStageError("RFQ repair-05 chain must be exactly repair-01/02/03/04/05")
    repair01, repair02, repair03, repair04, repair05 = repairs
    repair05_active_root = descendant_boundary_root or run_dir
    required_fields = {
        "schema_version", "repair_id", "parent_repair_id", "applied_at_utc",
        "pre_repair_status", "post_repair_status", "rfq_result_state",
        "retry_requirement", "finding", "failure_disposition", "failure_phase",
        "registration_change_class", "source_verification_mode",
        "quarantine_policy", "previous_repair_registration_path",
        "previous_repair_registration_sha256", "previous_repair_record_sha256",
        "blocker_path", "blocker_sha256", "failed_resource_receipt_path",
        "failed_resource_receipt_sha256", "unchanged_state_path",
        "unchanged_state_sha256", "unchanged_input_identity_path",
        "unchanged_input_identity_sha256", "failed_attempt",
        "wiring_contract_path", "wiring_contract_sha256", "wiring_contract",
        "authority_basis_path", "authority_basis_sha256", "authority_basis",
        "resource_contract_path", "resource_contract_sha256", "resource_contract",
        "expected_success_resource", "registered_rfq_query_sha256",
        "parent_registered_rfq_query_sha256", "parser_contract_path",
        "parser_contract_sha256", "parser_contract", "newly_quarantined_objects",
        "cumulative_quarantined_objects", "coverage", "selection_identity_unchanged",
        "previous_selection_fingerprint_sha256", "current_selection_fingerprint_sha256",
        "cycle1_binding_path", "cycle1_binding_sha256",
        "cycle1_duckdb_binding", "previous_execution_commit",
        "current_execution_commit", "initial_repository_identity",
        "previous_repository_identity", "current_repository_identity",
        "repository_identity_chain", "previous_source_manifest_sha256",
        "current_source_manifest_sha256", "previous_source_sha256s_sha256",
        "current_source_sha256s_sha256", "previous_query_set_sha256",
        "current_query_set_sha256", "core_result_disposition",
        "core_results_recomputed", "core_result_artifacts", "trial_registry",
        "data_selection_change", "parser_contract_change", "query_semantics_change",
        "quarantine_change", "resource_contract_change", "hypothesis_design_change",
        "threshold_feature_test_or_hypothesis_status_changed", "archive_path",
        "archive_inventory", "transaction_journal_path", "transaction_journal_sha256",
        "repair_receipt_path", "repair_receipt_sha256",
    }
    if set(repair05) != required_fields:
        raise RFQStageError("repair-05 registration record field set mismatch")
    if (
        manifest.get("status") != REPAIR05_STATUS
        or manifest.get("registration_state") != REPAIR05_REGISTRATION_STATE
        or repair05.get("schema_version") != REPAIR05_SCHEMA
        or repair05.get("parent_repair_id") != "repair-04"
        or repair05.get("pre_repair_status") != REPAIR04_STATUS
        or repair05.get("post_repair_status") != REPAIR05_STATUS
        or repair05.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair05.get("retry_requirement") != REPAIR05_RETRY_REQUIREMENT
        or repair05.get("finding") != REPAIR05_FINDING
        or repair05.get("failure_disposition") != REPAIR05_FAILURE_DISPOSITION
        or repair05.get("failure_phase") != REPAIR05_FAILURE_PHASE
        or repair05.get("registration_change_class") != REPAIR05_CHANGE_CLASS
        or repair05.get("source_verification_mode") != REPAIR04_GIT_SOURCE_MODE
        or repair05.get("quarantine_policy")
            != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
    ):
        raise RFQStageError("repair-05 identity/governance mismatch")
    try:
        applied_at = dt.datetime.fromisoformat(
            repair05["applied_at_utc"].replace("Z", "+00:00")
        )
    except (KeyError, AttributeError, ValueError) as exc:
        raise RFQStageError("repair-05 timestamp is invalid") from exc
    if applied_at.utcoffset() != dt.timedelta(0):
        raise RFQStageError("repair-05 timestamp is not UTC")

    receipt_path = _require_run_relative_file(
        run_dir, REPAIR05_REGISTRATION, "repair-05 registration receipt"
    )
    receipt_sha = sha256(receipt_path)
    receipt = read_json_object(receipt_path, "repair-05 registration receipt")
    expected_receipt = {
        key: value for key, value in repair05.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair05.get("repair_receipt_path") != REPAIR05_REGISTRATION
        or repair05.get("repair_receipt_sha256") != receipt_sha
        or receipt != expected_receipt
    ):
        raise RFQStageError("repair-05 registration receipt binding mismatch")

    pre_root = run_dir / REPAIR05_PRE_ROOT
    pre_manifest_path = _require_run_relative_file(
        run_dir, f"{REPAIR05_PRE_ROOT}/RUN_MANIFEST.json",
        "repair-05 archived repair-04 manifest",
    )
    pre_manifest = read_json_object(pre_manifest_path, "repair-05 parent manifest")
    if (
        repair05.get("archive_path") != REPAIR05_PRE_ROOT
        or repair05.get("archive_inventory") != _archive_inventory(pre_root)
        or pre_manifest.get("status") != REPAIR04_STATUS
        or pre_manifest.get("registration_state") != REPAIR04_REGISTRATION_STATE
        or pre_manifest.get("data_integrity_repairs") != repairs[:4]
    ):
        raise RFQStageError("repair-05 archive is not the exact repair-04 boundary")
    base_result = _apply_repair04_resource_contract(
        run_dir,
        pre_manifest,
        inputs,
        descendant_boundary_root=pre_root,
    )
    evidence = _validate_repair05_failure_evidence(
        run_dir,
        repair05,
        repair04,
    )
    resource = evidence["resource"]
    expected_failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
        "resource_active_path": REPAIR04_SUCCESS_RESOURCE_PATH,
        "resource_path": repair05["failed_resource_receipt_path"],
        "resource_sha256": repair05["failed_resource_receipt_sha256"],
        "resource_label": REPAIR04_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "resource_command": _repair05_retry_command(run_dir.name),
        "resource_metrics": {
            key: resource[key]
            for key in (
                "started_at_utc", "completed_at_utc", "wall_seconds",
                "cpu_user_seconds", "cpu_system_seconds", "cpu_hours",
                "peak_process_tree_rss_kib_polled",
                "cumulative_children_max_rss_kib", "peak_temp_bytes_polled",
                "minimum_disk_free_bytes_polled", "disk_free_before_bytes",
                "disk_free_after_bytes", "estimated_compute_cost_usd",
            )
        },
        "blocker_active_path": REPAIR05_BLOCKER_ACTIVE,
        "blocker_path": repair05["blocker_path"],
        "blocker_sha256": repair05["blocker_sha256"],
        "error_type": "KeyError",
        "error": "KeyError: 'cycle1_duckdb_binding'",
        "failure_phase": REPAIR05_FAILURE_PHASE,
        "offending_function": "_validate_repair04_failed_boundary",
        "offending_expression": "base_result['cycle1_duckdb_binding']",
        "new_state_written": False,
        "new_input_identity_written": False,
        "analysis_stage_started": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "unchanged_state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "unchanged_state_path": repair05["unchanged_state_path"],
        "unchanged_state_sha256": repair05["unchanged_state_sha256"],
        "unchanged_input_identity_active_path": (
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
        ),
        "unchanged_input_identity_path": repair05[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": repair05[
            "unchanged_input_identity_sha256"
        ],
    }

    if (
        repair05.get("previous_repair_registration_path")
            != repair04.get("repair_receipt_path")
        or repair05.get("previous_repair_registration_sha256")
            != repair04.get("repair_receipt_sha256")
        or repair05.get("previous_repair_record_sha256")
            != _json_payload_sha256(repair04)
        or repair05.get("resource_contract_path")
            != repair04.get("resource_contract_path")
        or repair05.get("resource_contract_sha256")
            != repair04.get("resource_contract_sha256")
        or repair05.get("resource_contract") != repair04.get("resource_contract")
        or repair05.get("parser_contract_path") != repair04.get("parser_contract_path")
        or repair05.get("parser_contract_sha256")
            != repair04.get("parser_contract_sha256")
        or repair05.get("parser_contract") != repair04.get("parser_contract")
        or repair05.get("parent_registered_rfq_query_sha256")
            != repair04.get("registered_rfq_query_sha256")
        or repair05.get("coverage") != repair04.get("coverage")
        or repair05.get("cumulative_quarantined_objects")
            != repair04.get("cumulative_quarantined_objects")
        or repair05.get("newly_quarantined_objects") != []
        or repair05.get("failed_attempt") != expected_failed_attempt
        or repair05.get("expected_success_resource") != {
            "label": REPAIR05_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR05_SUCCESS_RESOURCE_PATH,
        }
        or repair05.get("selection_identity_unchanged") is not True
        or repair05.get("previous_selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or repair05.get("current_selection_fingerprint_sha256")
            != base_result["selection_fingerprint_sha256"]
        or repair05.get("core_results_recomputed") is not False
        or repair05.get("core_result_disposition")
            != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair05.get("core_result_artifacts") != repair04.get("core_result_artifacts")
        or any(
            repair05.get(field) != "NONE"
            for field in (
                "data_selection_change", "parser_contract_change",
                "query_semantics_change", "quarantine_change",
                "resource_contract_change", "hypothesis_design_change",
            )
        )
        or repair05.get("threshold_feature_test_or_hypothesis_status_changed")
            is not False
    ):
        raise RFQStageError("repair-05 changed inherited research semantics")

    pre_repository = pre_manifest.get("repository")
    repository = manifest.get("repository")
    if not isinstance(pre_repository, dict) or not isinstance(repository, dict):
        raise RFQStageError("repair-05 repository identity is missing")
    pre_identity = _repository_identity(pre_repository)
    current_identity = _repository_identity(repository)
    identity_chain = repository.get("identity_history")
    if (
        not isinstance(identity_chain, list)
        or len(identity_chain) != 6
        or len({_json_payload_sha256(row) for row in identity_chain}) != 6
        or identity_chain[:-1] != pre_repository.get("identity_history")
        or identity_chain[-1] != current_identity
        or identity_chain != repair05.get("repository_identity_chain")
        or repair05.get("initial_repository_identity") != identity_chain[0]
        or repair05.get("previous_repository_identity") != pre_identity
        or repair05.get("current_repository_identity") != current_identity
        or repair05.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repair05.get("current_execution_commit")
            != repository.get("execution_commit")
        or repair05.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repair05.get("current_source_manifest_sha256")
            != repository.get("source_manifest_sha256")
        or repair05.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repair05.get("current_source_sha256s_sha256")
            != repository.get("source_sha256s_sha256")
        or repair05.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repair05.get("current_query_set_sha256")
            != repository.get("query_set_sha256")
        or repository.get("registration_repair_id") != "repair-05"
        or repository.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repository.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repository.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repository.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repository.get("previous_identity") != pre_identity
        or repository.get("source_tree_dirty_at_freeze") is not False
    ):
        raise RFQStageError("repair-05 repository transfer mismatch")
    if (
        sha256(repair05_active_root / "SOURCE_MANIFEST.json")
            != repository.get("source_manifest_sha256")
        or sha256(repair05_active_root / "SOURCE_SHA256SUMS.txt")
            != repository.get("source_sha256s_sha256")
    ):
        raise RFQStageError("repair-05 active source receipts changed")
    query_rows = _validate_query_receipt(
        run_dir, repository, active_root=repair05_active_root
    )
    registered_query_sha = query_rows.get(REPAIR05_EXECUTION_QUERY)
    if registered_query_sha != repair05.get("registered_rfq_query_sha256"):
        raise RFQStageError("repair-05 registered RFQ query SHA mismatch")

    repair_root = run_dir / REPAIR05_ROOT
    journal_path = _require_run_relative_file(
        run_dir, REPAIR05_TRANSACTION_JOURNAL, "repair-05 transaction journal"
    )
    journal_sha = sha256(journal_path)
    journal = read_json_object(journal_path, "repair-05 transaction journal")
    mutations = journal.get("active_mutations")
    manifest_mutation = journal.get("manifest_mutation")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *pre_repository.get("query_files", []),
        "TRIAL_REGISTRY.jsonl",
    ]
    expected_repair_files = sorted({
        *(
            f"pre_repair/{row['path']}"
            for row in repair05["archive_inventory"]
        ),
        *(f"post_repair/{relative}" for relative in expected_mutation_paths),
        "post_repair/RUN_MANIFEST.json",
        Path(REPAIR05_WIRING_CONTRACT).name,
        Path(REPAIR05_AUTHORITY_BASIS).name,
        Path(REPAIR05_REGISTRATION).name,
        Path(REPAIR05_TRANSACTION_JOURNAL).name,
        "REGISTRY_APPEND_PROGRESS.json",
    })
    actual_repair_files = sorted(
        row["path"] for row in _archive_inventory(repair_root)
    )
    if (
        repair05.get("transaction_journal_path")
            != REPAIR05_TRANSACTION_JOURNAL
        or repair05.get("transaction_journal_sha256") != journal_sha
        or set(journal) != {
            "schema_version", "repair_id", "run_id", "state",
            "original_manifest_sha256", "active_mutations",
            "manifest_mutation", "expected_repair_files",
        }
        or journal.get("schema_version") != "repair05-registration-transaction-v1"
        or journal.get("repair_id") != "repair-05"
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or not isinstance(manifest_mutation, dict)
        or [
            row.get("path") if isinstance(row, dict) else None
            for row in mutations
        ] != expected_mutation_paths
        or journal.get("expected_repair_files") != expected_repair_files
        or actual_repair_files != expected_repair_files
    ):
        raise RFQStageError("repair-05 transaction journal mismatch")
    for mutation in mutations:
        relative = mutation.get("path") if isinstance(mutation, dict) else None
        original_relative = f"{REPAIR05_PRE_ROOT}/{relative}"
        staging_relative = f"{REPAIR05_ROOT}/post_repair/{relative}"
        displaced_relative = (
            f"{REPAIR05_ROOT}/cas_displaced/"
            f"{str(relative).replace('/', '__')}.original"
        )
        original = _require_run_relative_file(
            run_dir, original_relative, "repair-05 original mutation payload"
        )
        staging = _require_run_relative_file(
            run_dir, staging_relative, "repair-05 staged mutation payload"
        )
        active = _require_run_relative_file(
            repair05_active_root,
            relative,
            "repair-05 active mutation payload",
        )
        if (
            not isinstance(mutation, dict)
            or set(mutation) != {
                "path", "original_archive_path", "original_sha256",
                "replacement_staging_path", "replacement_sha256",
                "displaced_path", "append_only_registry",
            }
            or mutation.get("original_archive_path") != original_relative
            or mutation.get("original_sha256") != sha256(original)
            or mutation.get("replacement_staging_path") != staging_relative
            or mutation.get("replacement_sha256") != sha256(staging)
            or sha256(active) != mutation.get("replacement_sha256")
            or active.read_bytes() != staging.read_bytes()
            or mutation.get("displaced_path") != displaced_relative
            or os.path.lexists(run_dir / displaced_relative)
            or mutation.get("append_only_registry")
                is not (relative == "TRIAL_REGISTRY.jsonl")
            or (
                relative == "TRIAL_REGISTRY.jsonl"
                and not active.read_bytes().startswith(original.read_bytes())
            )
        ):
            raise RFQStageError(
                f"repair-05 transaction mutation mismatch: {relative}"
            )

    manifest_original_relative = f"{REPAIR05_PRE_ROOT}/RUN_MANIFEST.json"
    manifest_staging_relative = (
        f"{REPAIR05_ROOT}/post_repair/RUN_MANIFEST.json"
    )
    manifest_displaced_relative = (
        f"{REPAIR05_ROOT}/cas_displaced/RUN_MANIFEST.json.original"
    )
    staged_manifest_path = _require_run_relative_file(
        run_dir, manifest_staging_relative, "repair-05 staged manifest"
    )
    active_manifest_path = _require_run_relative_file(
        repair05_active_root, "RUN_MANIFEST.json", "repair-05 active manifest"
    )
    staged_manifest = read_json_object(
        staged_manifest_path, "repair-05 staged manifest"
    )
    staged_records = staged_manifest.get("data_integrity_repairs")
    staged_record = (
        staged_records[-1]
        if isinstance(staged_records, list) and staged_records
        else None
    )
    if (
        set(manifest_mutation) != {
            "path", "original_archive_path", "original_sha256",
            "replacement_staging_path", "replacement_sha256",
            "displaced_path", "append_only_registry",
        }
        or manifest_mutation.get("path") != "RUN_MANIFEST.json"
        or manifest_mutation.get("original_archive_path")
            != manifest_original_relative
        or manifest_mutation.get("original_sha256") != sha256(pre_manifest_path)
        or manifest_mutation.get("replacement_staging_path")
            != manifest_staging_relative
        or manifest_mutation.get("replacement_sha256") is not None
        or manifest_mutation.get("displaced_path")
            != manifest_displaced_relative
        or manifest_mutation.get("append_only_registry") is not False
        or os.path.lexists(run_dir / manifest_displaced_relative)
        or active_manifest_path.read_bytes() != staged_manifest_path.read_bytes()
        or staged_manifest != manifest
        or staged_manifest.get("run_id") != run_dir.name
        or staged_manifest.get("status") != REPAIR05_STATUS
        or staged_manifest.get("registration_state") != REPAIR05_REGISTRATION_STATE
        or not isinstance(staged_records, list)
        or len(staged_records) != 5
        or not isinstance(staged_record, dict)
        or staged_record.get("repair_id") != "repair-05"
        or staged_record.get("transaction_journal_sha256") != journal_sha
        or staged_record.get("repair_receipt_sha256") != receipt_sha
    ):
        raise RFQStageError("repair-05 manifest transaction mutation mismatch")

    contract_path = _require_run_relative_file(
        run_dir, REPAIR05_WIRING_CONTRACT, "repair-05 wiring contract"
    )
    contract_sha = sha256(contract_path)
    contract = read_json_object(contract_path, "repair-05 wiring contract")
    contract_evidence = {
        "blocker_path": repair05["blocker_path"],
        "blocker_sha256": repair05["blocker_sha256"],
        "failed_resource_receipt_path": repair05["failed_resource_receipt_path"],
        "failed_resource_receipt_sha256": repair05[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_sha256": repair05["unchanged_state_sha256"],
        "unchanged_input_identity_sha256": repair05[
            "unchanged_input_identity_sha256"
        ],
        "parent_registered_rfq_query_sha256": repair04[
            "registered_rfq_query_sha256"
        ],
        "parent_resource_contract_sha256": repair04["resource_contract_sha256"],
        "cycle1_binding_path": repair05["cycle1_binding_path"],
        "cycle1_binding_sha256": repair05["cycle1_binding_sha256"],
        "registered_cycle1_duckdb_binding": repair05[
            "cycle1_duckdb_binding"
        ],
    }
    expected_contract = _expected_repair05_wiring_contract(
        run_dir.name, repair05["applied_at_utc"], contract_evidence,
        registered_query_sha,
    )
    if (
        repair05.get("wiring_contract_path") != REPAIR05_WIRING_CONTRACT
        or repair05.get("wiring_contract_sha256") != contract_sha
        or contract != expected_contract
        or repair05.get("wiring_contract") != {
            "path": REPAIR05_WIRING_CONTRACT,
            "sha256": contract_sha,
            "schema_version": REPAIR05_WIRING_SCHEMA,
            "expected_command": _repair05_retry_command(run_dir.name),
        }
    ):
        raise RFQStageError("repair-05 wiring contract mismatch")
    authority_path = _require_run_relative_file(
        run_dir, REPAIR05_AUTHORITY_BASIS, "repair-05 authority basis"
    )
    authority_sha = sha256(authority_path)
    authority = read_json_object(authority_path, "repair-05 authority basis")
    if (
        repair05.get("authority_basis_path") != REPAIR05_AUTHORITY_BASIS
        or repair05.get("authority_basis_sha256") != authority_sha
        or authority != _expected_repair05_authority_basis(
            run_dir.name, repair05["applied_at_utc"], contract_sha
        )
        or repair05.get("authority_basis") != {
            "path": REPAIR05_AUTHORITY_BASIS,
            "sha256": authority_sha,
            "schema_version": REPAIR05_AUTHORITY_SCHEMA,
            "authority_class": REPAIR05_AUTHORITY_CLASS,
            "permitted_change": "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY",
        }
    ):
        raise RFQStageError("repair-05 authority basis mismatch")

    pre_registry = _require_run_relative_file(
        run_dir, f"{REPAIR05_PRE_ROOT}/TRIAL_REGISTRY.jsonl",
        "repair-05 parent registry",
    ).read_bytes()
    active_registry = _require_run_relative_file(
        repair05_active_root, "TRIAL_REGISTRY.jsonl", "repair-05 active registry"
    ).read_bytes()
    trial = repair05.get("trial_registry")
    if (
        not isinstance(trial, dict)
        or set(trial) != {
            "previous_sha256", "current_sha256", "previous_bytes",
            "current_bytes", "strict_previous_bytes_prefix",
            "appended_records", "trial_registration_ids",
        }
        or active_registry == pre_registry
        or not active_registry.startswith(pre_registry)
        or trial.get("previous_sha256") != hashlib.sha256(pre_registry).hexdigest()
        or trial.get("current_sha256") != hashlib.sha256(active_registry).hexdigest()
        or trial.get("previous_bytes") != len(pre_registry)
        or trial.get("current_bytes") != len(active_registry)
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids") != [
            "RFQ_FULL_STAGE_ATTEMPT_05",
            "RFQ_CONSUMER_VALIDATION_WIRING_REPAIR_05",
        ]
    ):
        raise RFQStageError("repair-05 trial-registry boundary mismatch")
    suffix_rows = _read_registry_rows(
        active_registry[len(pre_registry):], "repair-05 registry suffix"
    )
    expected_suffix_rows = _expected_repair05_trial_rows(
        repair05["applied_at_utc"],
        pre_repository,
        current_identity,
        repair05,
        repair04,
        resource,
    )
    if suffix_rows != expected_suffix_rows:
        raise RFQStageError("repair-05 trial-registry suffix mismatch")

    failed05 = {
        "repair_id": "repair-05",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
        "blocker_path": repair05["blocker_path"],
        "blocker_sha256": repair05["blocker_sha256"],
        "failed_resource_receipt_path": repair05["failed_resource_receipt_path"],
        "failed_resource_receipt_sha256": repair05[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_path": repair05["unchanged_state_path"],
        "unchanged_state_sha256": repair05["unchanged_state_sha256"],
        "unchanged_input_identity_path": repair05[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": repair05[
            "unchanged_input_identity_sha256"
        ],
        "new_state_written": False,
        "new_input_identity_written": False,
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "resource_label": REPAIR04_SUCCESS_RESOURCE_LABEL,
        "retry_requirement": REPAIR05_RETRY_REQUIREMENT,
    }
    chain05 = {
        "repair_id": "repair-05",
        "registration_path": REPAIR05_REGISTRATION,
        "registration_sha256": receipt_sha,
        "wiring_contract_path": REPAIR05_WIRING_CONTRACT,
        "wiring_contract_sha256": contract_sha,
        "authority_basis_path": REPAIR05_AUTHORITY_BASIS,
        "authority_basis_sha256": authority_sha,
    }
    result = dict(base_result)
    result.update({
        "repair_chain": [*base_result["repair_chain"], chain05],
        "failed_attempt_bindings": [
            *base_result["failed_attempt_bindings"], failed05,
        ],
        "expected_success_resource": {
            "label": REPAIR05_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR05_SUCCESS_RESOURCE_PATH,
        },
        "consumer_wiring_contract": {
            "path": REPAIR05_WIRING_CONTRACT,
            "sha256": contract_sha,
            "schema_version": REPAIR05_WIRING_SCHEMA,
        },
        "registered_rfq_query_sha256": registered_query_sha,
    })
    del repair01, repair02, repair03, evidence
    return result


def _validate_repair06_failure_evidence(
    run_dir: Path,
    repair06: dict,
    repair05: dict,
) -> dict:
    """Bind attempt-06's validate_run failure and unchanged pre-evidence state."""
    pairs = (
        (REPAIR06_BLOCKER_ACTIVE, REPAIR06_BLOCKER_ARCHIVE),
        (REPAIR05_SUCCESS_RESOURCE_PATH, REPAIR06_FAILED_RESOURCE_ARCHIVE),
        (
            "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
            REPAIR06_UNCHANGED_STATE_ARCHIVE,
        ),
        (
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
            REPAIR06_UNCHANGED_INPUT_ARCHIVE,
        ),
        (CYCLE1_DUCKDB_BINDING, REPAIR06_CYCLE1_BINDING_ARCHIVE),
    )
    for active_relative, archive_relative in pairs:
        active = _require_run_relative_file(
            run_dir, active_relative, "repair-06 active pre-evidence boundary"
        )
        archived = _require_run_relative_file(
            run_dir, archive_relative, "repair-06 archived pre-evidence boundary"
        )
        if active.read_bytes() != archived.read_bytes():
            raise RFQStageError(
                f"repair-06 active/archive boundary differs: {active_relative}"
            )

    blocker_path = _require_run_relative_file(
        run_dir, REPAIR06_BLOCKER_ARCHIVE, "repair-06 blocker"
    )
    resource_path = _require_run_relative_file(
        run_dir, REPAIR06_FAILED_RESOURCE_ARCHIVE, "repair-06 failed resource"
    )
    state_path = _require_run_relative_file(
        run_dir, REPAIR06_UNCHANGED_STATE_ARCHIVE, "repair-06 unchanged state"
    )
    input_path = _require_run_relative_file(
        run_dir, REPAIR06_UNCHANGED_INPUT_ARCHIVE, "repair-06 unchanged input"
    )
    if (
        sha256(blocker_path) != REPAIR06_BLOCKER_SHA256
        or sha256(resource_path) != REPAIR06_FAILED_RESOURCE_SHA256
        or sha256(state_path) != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or sha256(input_path) != REPAIR04_APPROVED_FAILED_INPUT_SHA256
        or repair06.get("blocker_path") != REPAIR06_BLOCKER_ARCHIVE
        or repair06.get("blocker_sha256") != REPAIR06_BLOCKER_SHA256
        or repair06.get("failed_resource_receipt_path")
            != REPAIR06_FAILED_RESOURCE_ARCHIVE
        or repair06.get("failed_resource_receipt_sha256")
            != REPAIR06_FAILED_RESOURCE_SHA256
        or repair06.get("unchanged_state_path")
            != REPAIR06_UNCHANGED_STATE_ARCHIVE
        or repair06.get("unchanged_state_sha256")
            != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or repair06.get("unchanged_input_identity_path")
            != REPAIR06_UNCHANGED_INPUT_ARCHIVE
        or repair06.get("unchanged_input_identity_sha256")
            != REPAIR04_APPROVED_FAILED_INPUT_SHA256
    ):
        raise RFQStageError("repair-06 pre-evidence hashes or paths changed")

    blocker = read_json_object(blocker_path, "repair-06 blocker")
    if (
        blocker.get("schema_version")
            != "rfq-repair05-status-pre-evidence-blocker-v1"
        or blocker.get("run_id") != run_dir.name
        or blocker.get("mission_sha256") != EXPECTED_MISSION_SHA256
        or blocker.get("finding") != REPAIR06_FINDING
        or blocker.get("failure_phase") != REPAIR06_FAILURE_PHASE
        or blocker.get("error_type") != "RFQStageError"
        or blocker.get("error")
            != "RFQStageError: Cycle-1 core is not in an RFQ-stage-compatible state"
        or blocker.get("offending_function") != "validate_run"
        or blocker.get("traceback_call_chain") != ["main", "validate_run"]
        or blocker.get("source_change_scope") != REPAIR06_CHANGE_CLASS
        or blocker.get("failed_resource_receipt_path")
            != REPAIR05_SUCCESS_RESOURCE_PATH
        or blocker.get("failed_resource_receipt_sha256")
            != REPAIR06_FAILED_RESOURCE_SHA256
        or blocker.get("parent_registration_path") != REPAIR05_REGISTRATION
        or blocker.get("parent_registration_sha256")
            != repair05.get("repair_receipt_sha256")
        or blocker.get("repair05_wiring_contract_path")
            != REPAIR05_WIRING_CONTRACT
        or blocker.get("repair05_wiring_contract_sha256")
            != repair05.get("wiring_contract_sha256")
        or blocker.get("registered_query_path") != REPAIR05_EXECUTION_QUERY
        or blocker.get("registered_query_sha256")
            != REPAIR06_PARENT_RFQ_QUERY_SHA256
        or blocker.get("resource_label") != REPAIR05_SUCCESS_RESOURCE_LABEL
        or blocker.get("resource_return_code") != 1
        or blocker.get("active_state_sha256")
            != REPAIR04_APPROVED_FAILED_STATE_SHA256
        or blocker.get("active_input_identity_sha256")
            != REPAIR04_APPROVED_FAILED_INPUT_SHA256
        or blocker.get("new_state_written") is not False
        or blocker.get("new_input_identity_written") is not False
        or blocker.get("analysis_stage_started") is not False
        or blocker.get("active_scratch_absent") is not True
        or blocker.get("active_wal_absent") is not True
        or blocker.get("dependent_rfq_result_opened") is not False
        or blocker.get("whole_object_quarantine_unchanged") is not True
        or blocker.get("line_salvage") is not False
        or any(
            blocker.get(field) != "NONE"
            for field in (
                "data_selection_change", "parser_contract_change",
                "query_semantics_change", "quarantine_change",
                "resource_contract_change", "hypothesis_design_change",
            )
        )
    ):
        raise RFQStageError("repair-06 blocker content mismatch")

    resource = read_json_object(resource_path, "repair-06 failed resource")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != REPAIR05_SUCCESS_RESOURCE_LABEL
        or resource.get("command") != _repair06_retry_command(run_dir.name)
        or resource.get("return_code") != 1
        or resource.get("started_at_utc") != "2026-07-15T19:51:52.268627Z"
        or resource.get("completed_at_utc")
            != "2026-07-15T19:51:52.382793Z"
        or resource.get("wall_seconds") != REPAIR06_FAILED_RESOURCE_WALL_SECONDS
        or resource.get("s3_bytes_read_by_analysis") != 0
    ):
        raise RFQStageError("repair-06 failed resource receipt mismatch")

    _validate_repair05_fresh_scratch_absence(run_dir)
    if any(
        os.path.lexists(run_dir / relative)
        for relative in (
            "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json",
            "REPORT/RFQ_FULL_STAGE.md",
            "cache/rfq_full_catalog.duckdb",
        )
    ):
        raise RFQStageError("repair-06 did not preserve its pre-result boundary")

    cycle_path = _require_run_relative_file(
        run_dir, REPAIR06_CYCLE1_BINDING_ARCHIVE, "repair-06 Cycle-1 binding"
    )
    registered_cycle = repair05.get("cycle1_duckdb_binding")
    if (
        not isinstance(registered_cycle, dict)
        or repair06.get("cycle1_duckdb_binding") != registered_cycle
        or repair06.get("cycle1_binding_path")
            != REPAIR06_CYCLE1_BINDING_ARCHIVE
        or repair06.get("cycle1_binding_sha256") != sha256(cycle_path)
        or registered_cycle.get("active_sha256") != sha256(cycle_path)
    ):
        raise RFQStageError("repair-06 Cycle-1 evidence chain mismatch")
    return {"blocker": blocker, "resource": resource}


def _apply_repair06_status_wiring(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    *,
    active_root: Path | None = None,
) -> dict:
    """Validate repair-06 and expose its status-only runtime binding."""
    repairs = manifest.get("data_integrity_repairs")
    repair_ids = [
        row.get("repair_id") if isinstance(row, dict) else None
        for row in (repairs if isinstance(repairs, list) else [])
    ]
    if repair_ids != [
        "repair-01", "repair-02", "repair-03", "repair-04", "repair-05",
        "repair-06",
    ]:
        raise RFQStageError("RFQ repair-06 chain must be exactly repair-01/02/03/04/05/06")
    repair01, repair02, repair03, repair04, repair05, repair06 = repairs
    repair06_active_root = active_root or run_dir
    required_fields = {
        "schema_version", "repair_id", "parent_repair_id", "applied_at_utc",
        "pre_repair_status", "post_repair_status", "rfq_result_state",
        "retry_requirement", "registration_change_class", "finding",
        "failure_disposition", "failure_phase", "failed_attempt",
        "blocker_path", "blocker_sha256", "failed_resource_receipt_path",
        "failed_resource_receipt_sha256", "unchanged_state_path",
        "unchanged_state_sha256", "unchanged_input_identity_path",
        "unchanged_input_identity_sha256", "cycle1_binding_path",
        "cycle1_binding_sha256", "cycle1_duckdb_binding",
        "parent_manifest_sha256", "parent_repair_registration_path",
        "parent_repair_registration_sha256", "parent_transaction_journal_path",
        "parent_transaction_journal_sha256", "parent_wiring_contract_path",
        "parent_wiring_contract_sha256", "parent_authority_basis_path",
        "parent_authority_basis_sha256", "status_wiring_contract",
        "status_wiring_contract_path", "status_wiring_contract_sha256",
        "authority_basis", "authority_basis_path", "authority_basis_sha256",
        "previous_repair_record_sha256", "coverage",
        "cumulative_quarantined_objects", "core_result_artifacts",
        "core_result_disposition", "core_results_recomputed", "parser_contract",
        "parser_contract_path", "parser_contract_sha256", "resource_contract",
        "resource_contract_path", "resource_contract_sha256",
        "expected_success_resource", "registered_rfq_query_sha256",
        "previous_execution_commit", "current_execution_commit",
        "previous_repository_identity", "current_repository_identity",
        "repository_identity_chain", "source_verification_mode",
        "preserved_scratch_policy", "main_preflight", "data_selection_change",
        "parser_contract_change", "query_semantics_change", "quarantine_change",
        "resource_contract_change", "hypothesis_design_change",
        "threshold_feature_test_or_hypothesis_status_changed", "archive_path",
        "archive_inventory", "trial_registry", "transaction_journal_path",
        "transaction_journal_sha256", "repair_receipt_path",
        "repair_receipt_sha256",
    }
    if set(repair06) != required_fields:
        raise RFQStageError("repair-06 registration record field set mismatch")
    if (
        manifest.get("status") != REPAIR06_STATUS
        or manifest.get("registration_state") != REPAIR06_REGISTRATION_STATE
        or repair06.get("schema_version") != REPAIR06_SCHEMA
        or repair06.get("repair_id") != "repair-06"
        or repair06.get("parent_repair_id") != "repair-05"
        or repair06.get("pre_repair_status") != REPAIR05_STATUS
        or repair06.get("post_repair_status") != REPAIR06_STATUS
        or repair06.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair06.get("retry_requirement") != REPAIR06_RETRY_REQUIREMENT
        or repair06.get("finding") != REPAIR06_FINDING
        or repair06.get("failure_disposition") != REPAIR06_FAILURE_DISPOSITION
        or repair06.get("failure_phase") != REPAIR06_FAILURE_PHASE
        or repair06.get("registration_change_class")
            != REPAIR06_REGISTRATION_CHANGE_CLASS
        or repair06.get("source_verification_mode") != REPAIR04_GIT_SOURCE_MODE
        or repair06.get("preserved_scratch_policy")
            != REPAIR06_PRESERVED_SCRATCH_POLICY
    ):
        raise RFQStageError("repair-06 identity/governance mismatch")
    try:
        applied_at = dt.datetime.fromisoformat(
            repair06["applied_at_utc"].replace("Z", "+00:00")
        )
    except (KeyError, AttributeError, ValueError) as exc:
        raise RFQStageError("repair-06 timestamp is invalid") from exc
    if applied_at.utcoffset() != dt.timedelta(0):
        raise RFQStageError("repair-06 timestamp is not UTC")

    receipt_path = _require_run_relative_file(
        run_dir, REPAIR06_REGISTRATION, "repair-06 registration receipt"
    )
    receipt_sha = sha256(receipt_path)
    receipt = read_json_object(receipt_path, "repair-06 registration receipt")
    expected_receipt = {
        key: value for key, value in repair06.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    if (
        repair06.get("repair_receipt_path") != REPAIR06_REGISTRATION
        or repair06.get("repair_receipt_sha256") != receipt_sha
        or receipt != expected_receipt
    ):
        raise RFQStageError("repair-06 registration receipt binding mismatch")

    pre_root = run_dir / REPAIR06_PRE_ROOT
    pre_manifest_path = _require_run_relative_file(
        run_dir, f"{REPAIR06_PRE_ROOT}/RUN_MANIFEST.json",
        "repair-06 archived repair-05 manifest",
    )
    pre_manifest = read_json_object(pre_manifest_path, "repair-06 parent manifest")
    if (
        repair06.get("archive_path") != REPAIR06_PRE_ROOT
        or repair06.get("archive_inventory") != _archive_inventory(pre_root)
        or pre_manifest.get("run_id") != run_dir.name
        or pre_manifest.get("status") != REPAIR05_STATUS
        or pre_manifest.get("registration_state") != REPAIR05_REGISTRATION_STATE
        or pre_manifest.get("data_integrity_repairs") != repairs[:5]
        or repair06.get("parent_manifest_sha256") != sha256(pre_manifest_path)
        or repair06.get("parent_manifest_sha256")
            != REPAIR06_PARENT_MANIFEST_SHA256
    ):
        raise RFQStageError("repair-06 archive is not the exact repair-05 boundary")
    base_result = _apply_repair05_consumer_wiring(
        run_dir,
        pre_manifest,
        inputs,
        descendant_boundary_root=pre_root,
    )
    evidence = _validate_repair06_failure_evidence(run_dir, repair06, repair05)
    resource = evidence["resource"]
    expected_failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR05_ATTEMPT_06",
        "error": (
            "RFQStageError: Cycle-1 core is not in an RFQ-stage-compatible state"
        ),
        "error_type": "RFQStageError",
        "failure_phase": REPAIR06_FAILURE_PHASE,
        "finding": REPAIR06_FINDING,
        "blocker_active_path": REPAIR06_BLOCKER_ACTIVE,
        "blocker_archived_path": REPAIR06_BLOCKER_ARCHIVE,
        "blocker_sha256": REPAIR06_BLOCKER_SHA256,
        "failed_resource_active_path": REPAIR05_SUCCESS_RESOURCE_PATH,
        "failed_resource_archived_path": REPAIR06_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_sha256": REPAIR06_FAILED_RESOURCE_SHA256,
        "unchanged_state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "unchanged_state_archived_path": REPAIR06_UNCHANGED_STATE_ARCHIVE,
        "unchanged_state_sha256": REPAIR04_APPROVED_FAILED_STATE_SHA256,
        "unchanged_input_identity_active_path": (
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
        ),
        "unchanged_input_identity_archived_path": (
            REPAIR06_UNCHANGED_INPUT_ARCHIVE
        ),
        "unchanged_input_identity_sha256": (
            REPAIR04_APPROVED_FAILED_INPUT_SHA256
        ),
        "analysis_stage_started": False,
        "new_state_written": False,
        "new_input_identity_written": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "resource_label": REPAIR05_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "resource_command": _repair06_retry_command(run_dir.name),
    }
    if (
        repair06.get("failed_attempt") != expected_failed_attempt
        or repair06.get("parent_repair_registration_path")
            != repair05.get("repair_receipt_path")
        or repair06.get("parent_repair_registration_sha256")
            != repair05.get("repair_receipt_sha256")
        or repair06.get("parent_repair_registration_sha256")
            != REPAIR06_PARENT_RECEIPT_SHA256
        or repair06.get("parent_transaction_journal_path")
            != repair05.get("transaction_journal_path")
        or repair06.get("parent_transaction_journal_sha256")
            != repair05.get("transaction_journal_sha256")
        or repair06.get("parent_transaction_journal_sha256")
            != REPAIR06_PARENT_JOURNAL_SHA256
        or repair06.get("parent_wiring_contract_path")
            != repair05.get("wiring_contract_path")
        or repair06.get("parent_wiring_contract_sha256")
            != repair05.get("wiring_contract_sha256")
        or repair06.get("parent_wiring_contract_sha256")
            != REPAIR06_PARENT_WIRING_SHA256
        or repair06.get("parent_authority_basis_path")
            != repair05.get("authority_basis_path")
        or repair06.get("parent_authority_basis_sha256")
            != repair05.get("authority_basis_sha256")
        or repair06.get("parent_authority_basis_sha256")
            != REPAIR06_PARENT_AUTHORITY_SHA256
        or repair06.get("previous_repair_record_sha256")
            != _json_payload_sha256(repair05)
        or repair06.get("coverage") != repair05.get("coverage")
        or repair06.get("cumulative_quarantined_objects")
            != repair05.get("cumulative_quarantined_objects")
        or repair06.get("core_result_artifacts")
            != repair05.get("core_result_artifacts")
        or repair06.get("core_result_disposition")
            != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair06.get("core_results_recomputed") is not False
        or repair06.get("parser_contract") != repair05.get("parser_contract")
        or repair06.get("parser_contract_path")
            != repair05.get("parser_contract_path")
        or repair06.get("parser_contract_sha256")
            != repair05.get("parser_contract_sha256")
        or repair06.get("resource_contract") != repair05.get("resource_contract")
        or repair06.get("resource_contract_path")
            != repair05.get("resource_contract_path")
        or repair06.get("resource_contract_sha256")
            != repair05.get("resource_contract_sha256")
        or repair06.get("cycle1_duckdb_binding")
            != repair05.get("cycle1_duckdb_binding")
        or repair06.get("expected_success_resource") != {
            "label": REPAIR06_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR06_SUCCESS_RESOURCE_PATH,
        }
        or repair06.get("main_preflight")
            != _repair06_preflight_contract(run_dir.name)
        or any(
            repair06.get(field) != "NONE"
            for field in (
                "data_selection_change", "parser_contract_change",
                "query_semantics_change", "quarantine_change",
                "resource_contract_change", "hypothesis_design_change",
            )
        )
        or repair06.get("threshold_feature_test_or_hypothesis_status_changed")
            is not False
    ):
        raise RFQStageError("repair-06 changed inherited research semantics")

    pre_repository = pre_manifest.get("repository")
    repository = manifest.get("repository")
    if not isinstance(pre_repository, dict) or not isinstance(repository, dict):
        raise RFQStageError("repair-06 repository identity is missing")
    pre_identity = _repository_identity(pre_repository)
    current_identity = _repository_identity(repository)
    identity_chain = repository.get("identity_history")
    if (
        not isinstance(identity_chain, list)
        or len(identity_chain) != 7
        or len({_json_payload_sha256(row) for row in identity_chain}) != 7
        or identity_chain[:-1] != pre_repository.get("identity_history")
        or identity_chain[-1] != current_identity
        or identity_chain != repair06.get("repository_identity_chain")
        or repair06.get("previous_repository_identity") != pre_identity
        or repair06.get("current_repository_identity") != current_identity
        or repair06.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repair06.get("current_execution_commit")
            != repository.get("execution_commit")
        or repository.get("registration_repair_id") != "repair-06"
        or repository.get("previous_execution_commit")
            != pre_repository.get("execution_commit")
        or repository.get("previous_source_manifest_sha256")
            != pre_repository.get("source_manifest_sha256")
        or repository.get("previous_source_sha256s_sha256")
            != pre_repository.get("source_sha256s_sha256")
        or repository.get("previous_query_set_sha256")
            != pre_repository.get("query_set_sha256")
        or repository.get("previous_identity") != pre_identity
        or repository.get("source_tree_dirty_at_freeze") is not False
    ):
        raise RFQStageError("repair-06 repository transfer mismatch")
    if (
        sha256(repair06_active_root / "SOURCE_MANIFEST.json")
            != repository.get("source_manifest_sha256")
        or sha256(repair06_active_root / "SOURCE_SHA256SUMS.txt")
            != repository.get("source_sha256s_sha256")
    ):
        raise RFQStageError("repair-06 active source receipts changed")
    query_rows = _validate_query_receipt(
        run_dir, repository, active_root=repair06_active_root
    )
    registered_query_sha = query_rows.get(REPAIR06_EXECUTION_QUERY)
    if (
        repair05.get("registered_rfq_query_sha256")
            != REPAIR06_PARENT_RFQ_QUERY_SHA256
        or registered_query_sha != repair06.get("registered_rfq_query_sha256")
        or registered_query_sha == REPAIR06_PARENT_RFQ_QUERY_SHA256
    ):
        raise RFQStageError("repair-06 registered RFQ query SHA mismatch")

    contract_path = _require_run_relative_file(
        run_dir, REPAIR06_STATUS_WIRING_CONTRACT,
        "repair-06 status wiring contract",
    )
    contract_sha = sha256(contract_path)
    contract = read_json_object(contract_path, "repair-06 status wiring contract")
    expected_contract = _expected_repair06_status_wiring_contract(
        run_dir.name,
        repair06["applied_at_utc"],
        repair06,
        repair05,
        sha256(pre_manifest_path),
    )
    if (
        repair06.get("status_wiring_contract_path")
            != REPAIR06_STATUS_WIRING_CONTRACT
        or repair06.get("status_wiring_contract_sha256") != contract_sha
        or contract != expected_contract
        or repair06.get("status_wiring_contract") != expected_contract
    ):
        raise RFQStageError("repair-06 status wiring contract mismatch")
    authority_path = _require_run_relative_file(
        run_dir, REPAIR06_AUTHORITY_BASIS, "repair-06 authority basis"
    )
    authority_sha = sha256(authority_path)
    authority = read_json_object(authority_path, "repair-06 authority basis")
    if (
        repair06.get("authority_basis_path") != REPAIR06_AUTHORITY_BASIS
        or repair06.get("authority_basis_sha256") != authority_sha
        or authority != _expected_repair06_authority_basis(
            run_dir.name, repair06["applied_at_utc"], contract_sha
        )
        or repair06.get("authority_basis") != authority
    ):
        raise RFQStageError("repair-06 authority basis mismatch")

    pre_registry = _require_run_relative_file(
        run_dir, f"{REPAIR06_PRE_ROOT}/TRIAL_REGISTRY.jsonl",
        "repair-06 parent registry",
    ).read_bytes()
    active_registry = _require_run_relative_file(
        repair06_active_root, "TRIAL_REGISTRY.jsonl", "repair-06 active registry"
    ).read_bytes()
    trial = repair06.get("trial_registry")
    expected_trial_ids = [
        "RFQ_FULL_STAGE_ATTEMPT_06",
        "RFQ_VALIDATE_RUN_STATUS_WIRING_REPAIR_06",
    ]
    if (
        not isinstance(trial, dict)
        or set(trial) != {
            "previous_sha256", "current_sha256", "previous_bytes",
            "current_bytes", "strict_previous_bytes_prefix",
            "appended_records", "trial_registration_ids",
        }
        or active_registry == pre_registry
        or not active_registry.startswith(pre_registry)
        or trial.get("previous_sha256") != hashlib.sha256(pre_registry).hexdigest()
        or trial.get("current_sha256")
            != hashlib.sha256(active_registry).hexdigest()
        or trial.get("previous_bytes") != len(pre_registry)
        or trial.get("current_bytes") != len(active_registry)
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids") != expected_trial_ids
    ):
        raise RFQStageError("repair-06 trial-registry boundary mismatch")
    suffix_rows = _read_registry_rows(
        active_registry[len(pre_registry):], "repair-06 registry suffix"
    )
    if (
        len(suffix_rows) != 2
        or [row.get("trial_registration_id") for row in suffix_rows]
            != expected_trial_ids
        or any(row.get("parent_repair_id") != "repair-05" for row in suffix_rows)
        or any(row.get("result_opened") is not False for row in suffix_rows)
        or any(
            row.get("hypothesis_conclusion_opened") is not False
            for row in suffix_rows
        )
    ):
        raise RFQStageError("repair-06 trial-registry suffix mismatch")

    repair_root = run_dir / REPAIR06_ROOT
    journal_path = _require_run_relative_file(
        run_dir, REPAIR06_TRANSACTION_JOURNAL, "repair-06 transaction journal"
    )
    journal_sha = sha256(journal_path)
    journal = read_json_object(journal_path, "repair-06 transaction journal")
    mutations = journal.get("active_mutations")
    manifest_mutation = journal.get("manifest_mutation")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt", "QUERY_SHA256SUMS.txt",
        *pre_repository.get("query_files", []), "TRIAL_REGISTRY.jsonl",
    ]
    expected_repair_files = sorted({
        *(f"pre_repair/{row['path']}" for row in repair06["archive_inventory"]),
        *(f"post_repair/{relative}" for relative in expected_mutation_paths),
        "post_repair/RUN_MANIFEST.json",
        Path(REPAIR06_STATUS_WIRING_CONTRACT).name,
        Path(REPAIR06_AUTHORITY_BASIS).name,
        Path(REPAIR06_REGISTRATION).name,
        Path(REPAIR06_TRANSACTION_JOURNAL).name,
        "REGISTRY_APPEND_PROGRESS.json",
    })
    actual_repair_files = sorted(
        row["path"] for row in _archive_inventory(repair_root)
    )
    if (
        repair06.get("transaction_journal_path")
            != REPAIR06_TRANSACTION_JOURNAL
        or repair06.get("transaction_journal_sha256") != journal_sha
        or set(journal) != {
            "schema_version", "repair_id", "run_id", "state",
            "original_manifest_sha256", "active_mutations",
            "manifest_mutation", "expected_repair_files",
        }
        or journal.get("schema_version") != "repair06-registration-transaction-v1"
        or journal.get("repair_id") != "repair-06"
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or not isinstance(manifest_mutation, dict)
        or [row.get("path") if isinstance(row, dict) else None for row in mutations]
            != expected_mutation_paths
        or journal.get("expected_repair_files") != expected_repair_files
        or actual_repair_files != expected_repair_files
    ):
        raise RFQStageError("repair-06 transaction journal mismatch")
    for mutation in mutations:
        relative = mutation.get("path") if isinstance(mutation, dict) else None
        original_relative = f"{REPAIR06_PRE_ROOT}/{relative}"
        staging_relative = f"{REPAIR06_ROOT}/post_repair/{relative}"
        displaced_relative = (
            f"{REPAIR06_ROOT}/cas_displaced/"
            f"{str(relative).replace('/', '__')}.original"
        )
        original = _require_run_relative_file(
            run_dir, original_relative, "repair-06 original mutation payload"
        )
        staging = _require_run_relative_file(
            run_dir, staging_relative, "repair-06 staged mutation payload"
        )
        active = _require_run_relative_file(
            repair06_active_root, relative, "repair-06 active mutation payload"
        )
        if (
            not isinstance(mutation, dict)
            or set(mutation) != {
                "path", "original_archive_path", "original_sha256",
                "replacement_staging_path", "replacement_sha256",
                "displaced_path", "append_only_registry",
            }
            or mutation.get("original_archive_path") != original_relative
            or mutation.get("original_sha256") != sha256(original)
            or mutation.get("replacement_staging_path") != staging_relative
            or mutation.get("replacement_sha256") != sha256(staging)
            or sha256(active) != mutation.get("replacement_sha256")
            or active.read_bytes() != staging.read_bytes()
            or mutation.get("displaced_path") != displaced_relative
            or os.path.lexists(run_dir / displaced_relative)
            or mutation.get("append_only_registry")
                is not (relative == "TRIAL_REGISTRY.jsonl")
            or (
                relative == "TRIAL_REGISTRY.jsonl"
                and not active.read_bytes().startswith(original.read_bytes())
            )
        ):
            raise RFQStageError(
                f"repair-06 transaction mutation mismatch: {relative}"
            )

    manifest_staging_relative = f"{REPAIR06_ROOT}/post_repair/RUN_MANIFEST.json"
    manifest_displaced_relative = (
        f"{REPAIR06_ROOT}/cas_displaced/RUN_MANIFEST.json.original"
    )
    staged_manifest_path = _require_run_relative_file(
        run_dir, manifest_staging_relative, "repair-06 staged manifest"
    )
    active_manifest_path = _require_run_relative_file(
        repair06_active_root, "RUN_MANIFEST.json", "repair-06 active manifest"
    )
    if (
        set(manifest_mutation) != {
            "path", "original_archive_path", "original_sha256",
            "replacement_staging_path", "replacement_sha256",
            "displaced_path", "append_only_registry",
        }
        or manifest_mutation.get("path") != "RUN_MANIFEST.json"
        or manifest_mutation.get("original_archive_path")
            != f"{REPAIR06_PRE_ROOT}/RUN_MANIFEST.json"
        or manifest_mutation.get("original_sha256") != sha256(pre_manifest_path)
        or manifest_mutation.get("replacement_staging_path")
            != manifest_staging_relative
        or manifest_mutation.get("replacement_sha256") is not None
        or manifest_mutation.get("displaced_path") != manifest_displaced_relative
        or manifest_mutation.get("append_only_registry") is not False
        or os.path.lexists(run_dir / manifest_displaced_relative)
        or active_manifest_path.read_bytes() != staged_manifest_path.read_bytes()
        or read_json_object(staged_manifest_path, "repair-06 staged manifest")
            != manifest
        or repair06.get("transaction_journal_sha256") != journal_sha
        or repair06.get("repair_receipt_sha256") != receipt_sha
    ):
        raise RFQStageError("repair-06 manifest transaction mutation mismatch")

    failed06 = {
        "repair_id": "repair-06",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR05_ATTEMPT_06",
        "blocker_path": repair06["blocker_path"],
        "blocker_sha256": repair06["blocker_sha256"],
        "failed_resource_receipt_path": repair06[
            "failed_resource_receipt_path"
        ],
        "failed_resource_receipt_sha256": repair06[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_path": repair06["unchanged_state_path"],
        "unchanged_state_sha256": repair06["unchanged_state_sha256"],
        "unchanged_input_identity_path": repair06[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": repair06[
            "unchanged_input_identity_sha256"
        ],
        "new_state_written": False,
        "new_input_identity_written": False,
        "input_fingerprint": base_result["selection_fingerprint_sha256"],
        "resource_label": REPAIR05_SUCCESS_RESOURCE_LABEL,
        "retry_requirement": REPAIR06_RETRY_REQUIREMENT,
    }
    chain06 = {
        "repair_id": "repair-06",
        "registration_path": REPAIR06_REGISTRATION,
        "registration_sha256": receipt_sha,
        "status_wiring_contract_path": REPAIR06_STATUS_WIRING_CONTRACT,
        "status_wiring_contract_sha256": contract_sha,
        "authority_basis_path": REPAIR06_AUTHORITY_BASIS,
        "authority_basis_sha256": authority_sha,
    }
    result = dict(base_result)
    result.update({
        "repair_chain": [*base_result["repair_chain"], chain06],
        "failed_attempt_bindings": [
            *base_result["failed_attempt_bindings"], failed06,
        ],
        "expected_success_resource": {
            "label": REPAIR06_SUCCESS_RESOURCE_LABEL,
            "path": REPAIR06_SUCCESS_RESOURCE_PATH,
        },
        "validate_run_status_wiring_contract": {
            "path": REPAIR06_STATUS_WIRING_CONTRACT,
            "sha256": contract_sha,
            "schema_version": REPAIR06_STATUS_WIRING_SCHEMA,
        },
        "registered_rfq_query_sha256": registered_query_sha,
    })
    del repair01, repair02, repair03, repair04, evidence
    return result


def apply_object_quarantine(
    run_dir: Path,
    manifest: dict,
    inputs: dict,
    _ignore_repair02: bool = False,
    _active_root: Path | None = None,
) -> dict:
    """Apply an exact, pre-result whole-object quarantine without opening its bytes.

    The immutable manifest and version list remain the authority.  A receipt alone
    cannot exclude data, and a declaration alone cannot do so either.  Both must be
    present and cross-bind every identity field before the selected path list changes.
    """
    declaration02_path = run_dir / QUARANTINE_DECLARATION_02
    receipt02_path = run_dir / MALFORMED_OBJECT_RECEIPT_02
    if not _ignore_repair02:
        if declaration02_path.is_file() != receipt02_path.is_file():
            raise RFQStageError(
                "repair-02 requires both its declaration and malformed-object receipt"
            )
        if declaration02_path.is_file():
            repair_ids = [
                row.get("repair_id") if isinstance(row, dict) else None
                for row in manifest.get("data_integrity_repairs", [])
            ]
            if repair_ids == [
                "repair-01", "repair-02", "repair-03", "repair-04",
                "repair-05", "repair-06",
            ]:
                return _apply_repair06_status_wiring(
                    run_dir, manifest, inputs, active_root=_active_root
                )
            if repair_ids == [
                "repair-01", "repair-02", "repair-03", "repair-04", "repair-05"
            ]:
                return _apply_repair05_consumer_wiring(run_dir, manifest, inputs)
            if repair_ids == ["repair-01", "repair-02", "repair-03", "repair-04"]:
                return _apply_repair04_resource_contract(run_dir, manifest, inputs)
            if repair_ids == ["repair-01", "repair-02", "repair-03"]:
                return _apply_repair03_parser_contract(run_dir, manifest, inputs)
            return _apply_double_object_quarantine(run_dir, manifest, inputs)

    declaration_path = run_dir / QUARANTINE_DECLARATION
    receipt_path = run_dir / MALFORMED_OBJECT_RECEIPT
    declaration_exists = declaration_path.is_file()
    receipt_exists = receipt_path.is_file()
    if not declaration_exists and not receipt_exists:
        result = dict(inputs)
        result.update({
            "coverage_status": "COMPLETE_MANIFEST_OBJECT_COVERAGE",
            "full_object_coverage": True,
            "unique_objects_total": inputs["objects"],
            "unique_bytes_total": inputs["bytes"],
            "consumed_unique_objects": inputs["objects"],
            "consumed_logical_bindings": inputs["logical_manifest_bindings"],
            "consumed_bytes": inputs["bytes"],
            "quarantined_unique_objects": 0,
            "quarantined_logical_bindings": 0,
            "quarantined_bytes": 0,
            "quarantined_object_set_sha256": hashlib.sha256(b"").hexdigest(),
            "quarantine_reasons": [],
            "quarantine_details": [],
            "quarantine_boundaries": [],
            "consumed_object_set_sha256": inputs["path_size_fingerprint_sha256"],
            "selection_fingerprint_sha256": inputs["path_size_fingerprint_sha256"],
        })
        return result
    if declaration_exists != receipt_exists:
        raise RFQStageError(
            "RFQ quarantine requires both declaration and malformed-object receipt"
        )

    declaration = read_json_object(declaration_path, "RFQ quarantine declaration")
    receipt = read_json_object(receipt_path, "RFQ malformed-object receipt")
    if (
        declaration.get("schema_version") != "rfq-object-quarantine-v1"
        or declaration.get("run_id") != run_dir.name
        or declaration.get("mode") != "EXPLORATORY_AUTORESEARCH"
        or declaration.get("disposition") != "WHOLE_OBJECT_QUARANTINE"
        or declaration.get("finding") != "MALFORMED_NDJSON_OBJECT"
        or declaration.get("created_after_structural_failure_before_rfq_result") is not True
        or declaration.get("dependent_rfq_result_opened") is not False
        or declaration.get("remaining_object_parse_policy")
            != STRICT_REMAINING_PARSE_POLICY
        or not isinstance(declaration.get("authority_basis"), str)
        or not declaration["authority_basis"]
        or not isinstance(declaration.get("selection_rule"), str)
        or not declaration["selection_rule"]
        or not isinstance(declaration.get("result_use_prohibited"), str)
        or not declaration["result_use_prohibited"]
    ):
        raise RFQStageError("RFQ quarantine declaration policy binding mismatch")
    completed_summary = run_dir / "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json"
    if completed_summary.exists():
        raise RFQStageError("RFQ quarantine was not registered before an RFQ result")
    declared_objects = declaration.get("quarantined_objects")
    if not isinstance(declared_objects, list) or len(declared_objects) != 1:
        raise RFQStageError("exactly one receipt-bound RFQ quarantine object is required")
    declared = declared_objects[0]
    if not isinstance(declared, dict):
        raise RFQStageError("RFQ quarantined-object declaration is invalid")

    invalid_count = receipt.get("invalid_line_count")
    invalid_lines = receipt.get("invalid_lines")
    if (
        receipt.get("schema_version") != "rfq-malformed-object-receipt-v1"
        or receipt.get("run_id") != run_dir.name
        or receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED"
        or receipt.get("raw_payload_redacted") is not True
        or type(invalid_count) is not int
        or invalid_count <= 0
        or not isinstance(invalid_lines, list)
        or len(invalid_lines) != invalid_count
        or type(receipt.get("total_lines")) is not int
        or receipt["total_lines"] < invalid_count
        or receipt.get("expected_size") != receipt.get("observed_size")
        or receipt.get("expected_sha256") != receipt.get("observed_sha256")
    ):
        raise RFQStageError("RFQ malformed-object receipt is not exclusion-grade")
    for finding in invalid_lines:
        if (
            not isinstance(finding, dict)
            or type(finding.get("line_number")) is not int
            or finding["line_number"] <= 0
            or not isinstance(finding.get("line_sha256"), str)
            or len(finding["line_sha256"]) != 64
            or not isinstance(finding.get("error_type"), str)
            or not finding["error_type"]
        ):
            raise RFQStageError("RFQ malformed line receipt is invalid")

    release_id = declared.get("release_id")
    key = declared.get("key")
    size = declared.get("size")
    digest = declared.get("sha256")
    if (
        declared.get("receipt") != MALFORMED_OBJECT_RECEIPT
        or declared.get("invalid_line_count") != invalid_count
        or type(size) is not int
        or size <= 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(declared.get("reason"), str)
        or not declared["reason"]
        or receipt.get("release_id") != release_id
        or receipt.get("key") != key
        or receipt.get("expected_size") != size
        or receipt.get("expected_sha256") != digest
    ):
        raise RFQStageError("RFQ declaration/receipt object identity mismatch")

    selected_by_release = {
        row.get("release_id"): row for row in manifest.get("selected_releases", [])
        if isinstance(row, dict)
    }
    discovered_by_release = {
        row.get("release_id"): row for row in inputs.get("releases", [])
        if isinstance(row, dict)
    }
    selected = selected_by_release.get(release_id)
    discovered_release = discovered_by_release.get(release_id)
    if not isinstance(selected, dict) or not isinstance(discovered_release, dict):
        raise RFQStageError("quarantined release is outside the selected manifest set")
    manifest_sha = declared.get("manifest_sha256")
    if (
        manifest_sha != selected.get("manifest_sha256")
        or manifest_sha != discovered_release.get("manifest_sha256")
    ):
        raise RFQStageError("quarantine manifest SHA binding mismatch")

    matches = [row for row in inputs["objects_detail"] if row["key"] == key]
    if len(matches) != 1:
        raise RFQStageError("quarantined key does not resolve to one manifest object")
    manifest_object = matches[0]
    if (
        manifest_object["size"] != size
        or manifest_object["sha256"] != digest
        or manifest_object["bound_release_ids"] != [release_id]
    ):
        raise RFQStageError("quarantine is not exact/unambiguous at manifest-object scope")
    version = _load_version_binding(run_dir, selected, key)
    if (
        version.get("key") != key
        or version.get("size") != size
        or version.get("sha256") != digest
        or version.get("version_id") != declared.get("version_id")
        or not isinstance(declared.get("version_id"), str)
        or not declared["version_id"]
    ):
        raise RFQStageError("quarantine exact VersionId binding mismatch")
    failed_attempt_binding = _validate_repair_binding(
        run_dir, manifest, inputs, declaration, receipt_path, declaration_path
    )

    consumed = [row for row in inputs["objects_detail"] if row["key"] != key]
    if not consumed:
        raise RFQStageError("RFQ quarantine leaves no independently valid object")
    if _rfq_message_shard_order(key) is None:
        raise RFQStageError("only an RFQ recorder message shard may be quarantined")
    release_objects = [
        row for row in inputs["objects_detail"]
        if release_id in row["bound_release_ids"]
        and _rfq_message_shard_order(row["key"]) is not None
    ]
    ordered = sorted(
        release_objects,
        key=lambda row: _rfq_message_shard_order(row["key"]),
    )
    index = next(i for i, row in enumerate(ordered) if row["key"] == key)
    previous = ordered[index - 1] if index else None
    following = ordered[index + 1] if index + 1 < len(ordered) else None
    if previous is None or following is None:
        raise RFQStageError(
            "quarantine gap requires valid immediately preceding and following RFQ objects"
        )

    quarantined = [{
        "release_id": release_id,
        "key": key,
        "size": size,
        "sha256": digest,
        "version_id": declared["version_id"],
        "manifest_sha256": manifest_sha,
        "invalid_line_count": invalid_count,
        "reason": declared["reason"],
        "receipt_sha256": sha256(receipt_path),
        "declaration_sha256": sha256(declaration_path),
    }]
    consumed_fingerprint = _object_fingerprint(consumed)
    quarantined_fingerprint = _object_fingerprint([manifest_object])
    selection_payload = {
        "total": inputs["path_size_fingerprint_sha256"],
        "consumed": consumed_fingerprint,
        "quarantined": quarantined_fingerprint,
        "receipt": quarantined[0]["receipt_sha256"],
        "declaration": quarantined[0]["declaration_sha256"],
    }
    result = dict(inputs)
    result.update({
        "paths": [row["path"] for row in consumed],
        "objects_detail": consumed,
        "coverage_status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_object_coverage": False,
        "unique_objects_total": inputs["objects"],
        "unique_bytes_total": inputs["bytes"],
        "consumed_unique_objects": len(consumed),
        "consumed_logical_bindings": sum(
            len(row["bound_release_ids"]) for row in consumed
        ),
        "consumed_bytes": sum(row["size"] for row in consumed),
        "quarantined_unique_objects": 1,
        "quarantined_logical_bindings": len(manifest_object["bound_release_ids"]),
        "quarantined_bytes": size,
        "quarantined_object_set_sha256": quarantined_fingerprint,
        "quarantine_reasons": [declared["reason"]],
        "quarantine_details": quarantined,
        "quarantine_boundaries": [{
            "release_id": release_id,
            "key": key,
            "sha256": digest,
            "quarantined_keys": [key],
            "quarantined_object_set_sha256": quarantined_fingerprint,
            "previous_key": previous["key"],
            "next_key": following["key"],
            "previous_path": previous["path"],
            "next_path": following["path"],
        }],
        "consumed_object_set_sha256": consumed_fingerprint,
        "selection_fingerprint_sha256": hashlib.sha256(
            json.dumps(selection_payload, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest(),
        "failed_attempt_binding": failed_attempt_binding,
    })
    return result


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


def validate_run(
    run_dir: Path, *, manifest_root: Path | None = None
) -> dict:
    manifest_path = (manifest_root or run_dir) / "RUN_MANIFEST.json"
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
        "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED",
        "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        REPAIR03_STATUS,
        REPAIR04_STATUS,
        REPAIR05_STATUS,
        REPAIR06_STATUS,
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
    # DuckDB's typed JSON projection represents both an absent outer ``marker``
    # field and an explicit JSON null as SQL NULL.  For repair-03, ``marker IS
    # NULL`` is therefore authorized here only because main() has already run
    # _apply_repair03_parser_contract: that verifier hard-binds the approved
    # bytewise audit and reconciles all 282 object summaries with the frozen
    # active input identity.  The audit proves data-frame markers are *absent*
    # and declares explicit null fail-closed.  Never reorder the SQL scan ahead
    # of that preregistration proof or treat this typed projection as presence
    # evidence by itself.
    raw = path_list(paths)
    blank_control_markers = "(" + ",".join(
        quote(marker) for marker in EXPECTED_BLANK_CONTROL_MARKERS
    ) + ")"
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
        CASE WHEN marker IN {blank_control_markers} AND raw=''
          THEN true ELSE false END AS expected_blank_control_marker,
        CASE
          WHEN marker IN {blank_control_markers} THEN coalesce(raw='',false)
          WHEN marker IS NULL OR marker='segment_receipt' THEN
            json_valid(raw) IS TRUE
            AND coalesce(json_type(inner_json)='OBJECT',false)
          ELSE false
        END AS inner_payload_contract_valid,
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


def install_quarantine_gaps(connection, boundaries: Sequence[dict]) -> None:
    """Materialize conservative RFQ-channel gaps without opening excluded objects."""
    if table_exists(connection, "rfq_quarantine_gaps"):
        rows = rows_as_dicts(connection, """
          SELECT release_id,key,sha256,quarantined_keys_json,
            quarantined_object_count,quarantined_object_set_sha256,
            previous_filename,next_filename
          FROM rfq_quarantine_gaps ORDER BY key
        """)
        expected = sorted(({
            "release_id": item["release_id"],
            "key": item["key"],
            "sha256": item["sha256"],
            "quarantined_keys_json": json.dumps(
                item.get("quarantined_keys", [item["key"]]),
                sort_keys=True, separators=(",", ":"),
            ),
            "quarantined_object_count": len(
                item.get("quarantined_keys", [item["key"]])
            ),
            "quarantined_object_set_sha256": item.get(
                "quarantined_object_set_sha256", item["sha256"]
            ),
            "previous_filename": str(item["previous_path"]),
            "next_filename": str(item["next_path"]),
        } for item in boundaries), key=lambda row: row["key"])
        if rows != expected:
            raise RFQStageError(
                "existing RFQ quarantine-gap table is stale/incomplete; fresh scratch required"
            )
        return
    connection.execute("""
      CREATE TABLE rfq_quarantine_gaps(
        release_id VARCHAR,key VARCHAR,sha256 VARCHAR,
        quarantined_keys_json VARCHAR,quarantined_object_count INTEGER,
        quarantined_object_set_sha256 VARCHAR,
        previous_filename VARCHAR,next_filename VARCHAR,
        gap_start_ns UBIGINT,gap_end_ns UBIGINT,gap_start_us BIGINT,gap_end_us BIGINT,
        boundary_reason VARCHAR
      )
    """)
    for item in boundaries:
        previous_path = str(item["previous_path"])
        next_path = str(item["next_path"])
        previous_end = scalar(
            connection,
            "SELECT max(recv_wall_ns) FROM rfq_scan_rows "
            "WHERE filename=? AND recv_wall_ns>0",
            [previous_path],
        )
        next_start = scalar(
            connection,
            "SELECT min(recv_wall_ns) FROM rfq_scan_rows "
            "WHERE filename=? AND recv_wall_ns>0",
            [next_path],
        )
        if (
            previous_end is None
            or next_start is None
            or int(previous_end) <= 0
            or int(next_start) <= int(previous_end) + 1
        ):
            raise RFQStageError(
                f"cannot establish causal quarantine gap boundaries: {item['key']}"
            )
        start_ns = int(previous_end) + 1
        end_ns = int(next_start)
        reason = "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON"
        keys = item.get("quarantined_keys", [item["key"]])
        keys_json = json.dumps(keys, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO rfq_quarantine_gaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                item["release_id"], item["key"], item["sha256"],
                keys_json, len(keys), item.get(
                    "quarantined_object_set_sha256", item["sha256"]
                ), previous_path, next_path, start_ns, end_ns,
                start_ns // 1000, (end_ns + 999) // 1000, reason,
            ],
        )


def add_quarantine_observation_boundaries(connection) -> None:
    if not table_exists(connection, "rfq_quarantine_gaps"):
        raise RFQStageError("RFQ quarantine-gap table missing")
    if not scalar(connection, "SELECT count(*) FROM rfq_quarantine_gaps"):
        return
    installed = scalar(connection, """
      SELECT count(*) FROM rfq_quarantine_gaps g
      WHERE EXISTS (
        SELECT 1 FROM rfq_observation_boundaries b
        WHERE b.boundary_ns=g.gap_start_ns
          AND contains(b.reason,g.boundary_reason||'_GAP_START')
      ) AND EXISTS (
        SELECT 1 FROM rfq_observation_boundaries b
        WHERE b.boundary_ns=g.gap_end_ns
          AND contains(b.reason,g.boundary_reason||'_GAP_END')
      )
    """)
    gaps = scalar(connection, "SELECT count(*) FROM rfq_quarantine_gaps")
    if installed == gaps:
        return
    if installed:
        raise RFQStageError("partially installed RFQ quarantine boundaries")
    connection.execute("""
      CREATE TABLE rfq_observation_boundaries_with_quarantine AS
      WITH evidence AS (
        SELECT boundary_ns,reason,evidence_rows FROM rfq_observation_boundaries
        UNION ALL
        SELECT gap_start_ns,
          boundary_reason||'_GAP_START' AS reason,1 AS evidence_rows
        FROM rfq_quarantine_gaps
        UNION ALL
        SELECT gap_end_ns,
          boundary_reason||'_GAP_END' AS reason,1 AS evidence_rows
        FROM rfq_quarantine_gaps
      )
      SELECT boundary_ns,string_agg(DISTINCT reason,';' ORDER BY reason) AS reason,
        sum(evidence_rows)::BIGINT AS evidence_rows
      FROM evidence GROUP BY boundary_ns ORDER BY boundary_ns
    """)
    connection.execute("DROP TABLE rfq_observation_boundaries")
    connection.execute(
        "ALTER TABLE rfq_observation_boundaries_with_quarantine "
        "RENAME TO rfq_observation_boundaries"
    )


def build_scan_tables(
    connection,
    paths: Sequence[Path],
    run_id: str = "_TEST",
    quarantine_boundaries: Sequence[dict] = (),
) -> None:
    # A completed normalization phase intentionally drops its two largest
    # intermediates so their pages can be reused by lifecycle construction.
    if (table_exists(connection, "rfq_events_ordered")
            and table_exists(connection, "rfq_scan_counts")
            and table_exists(connection, "rfq_observation_boundaries")
            and table_exists(connection, "rfq_quarantine_gaps")
            and not table_exists(connection, "rfq_scan_rows")):
        install_quarantine_gaps(connection, quarantine_boundaries)
        add_quarantine_observation_boundaries(connection)
        return
    if (not table_exists(connection, "rfq_scan_rows")
            and not table_exists(connection, "rfq_events_valid")):
        connection.execute(scan_sql(paths, run_id))
    if table_exists(connection, "rfq_scan_rows") and scalar(
        connection,
        "SELECT count(*) FROM rfq_scan_rows "
        "WHERE inner_payload_contract_valid IS NOT TRUE",
    ):
        raise RFQStageError(
            "unexpected malformed inner RFQ payload or marker contract violation "
            "in a consumed object; aborting"
        )
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
            count(*) FILTER (WHERE inner_json_valid IS FALSE
              AND NOT expected_blank_control_marker) AS invalid_inner_json_rows,
            count(*) FILTER (WHERE inner_payload_contract_valid IS NOT TRUE)
              AS invalid_inner_payload_contract_rows,
            count(*) FILTER (WHERE expected_blank_control_marker)
              AS expected_blank_control_marker_rows,
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
    install_quarantine_gaps(connection, quarantine_boundaries)
    add_quarantine_observation_boundaries(connection)
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
          WITH boundary_index AS (
            SELECT boundary_ns,row_number() OVER (ORDER BY boundary_ns)::BIGINT
              AS observation_segment
            FROM rfq_observation_boundaries
          ), segmented AS (
            SELECT r.*,coalesce(b.observation_segment,0)::BIGINT AS observation_segment
            FROM (SELECT * FROM rfq_requests_base ORDER BY create_recv_wall_ns) r
            ASOF LEFT JOIN (SELECT * FROM boundary_index ORDER BY boundary_ns) b
              ON r.create_recv_wall_ns>=b.boundary_ns
          )
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
          FROM segmented r LEFT JOIN core.universe u
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
                   r.match_phase,r.dim_effective_us,r.dimension_causality,
                   r.observation_segment
            FROM rfq_lifecycle_base l JOIN rfq_requests r USING(request_key)
          ) SELECT *,create_receive_us-lag(delete_receive_us) OVER (
              PARTITION BY rfq_id_hash,observation_segment
              ORDER BY create_receive_us,cycle_no,request_key
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
          SELECT create_date,category,sport,observation_segment,
            create_receive_us-lag(create_receive_us) OVER (
              PARTITION BY create_date,category,sport,observation_segment
              ORDER BY create_receive_us,request_key)
              AS interarrival_us
          FROM rfq_requests
        """)
    statements = {
        "rfq_flow_daily": """
          SELECT create_date,dayname(create_date) AS day_of_week,category,
            coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            count(*) FILTER (WHERE known_combo) AS known_combo_requests,
            count(*) FILTER (WHERE requester_known) AS known_requester_requests,
            'RETAINED_OBSERVED_SUBSET' AS analysis_population,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE cast(to_timestamp(g.gap_start_us/1000000.0) AS DATE)<=create_date
                AND cast(to_timestamp((g.gap_end_us-1)/1000000.0) AS DATE)>=create_date
            ) THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
              ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status
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
          ), hourly AS (
            SELECT *,(receive_us//3600000000)*3600000000 AS hour_start_us FROM events
          ) SELECT date,extract('hour' FROM to_timestamp(hour_start_us/1000000.0))::INTEGER
              AS utc_hour,event_type,count(*) AS events,
            'RETAINED_OBSERVED_SUBSET' AS analysis_population,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE g.gap_start_us<hour_start_us+3600000000
                AND g.gap_end_us>hour_start_us
            ) THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
              ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status
            FROM hourly GROUP BY date,utc_hour,event_type,hour_start_us
            ORDER BY date,utc_hour,event_type
        """,
        "rfq_flow_hourly": """
          WITH hourly AS (
            SELECT *,(create_receive_us//3600000000)*3600000000 AS hour_start_us
            FROM rfq_requests
          ) SELECT create_date,
            extract('hour' FROM to_timestamp(hour_start_us/1000000.0))::INTEGER AS utc_hour,
            category,coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            count(*) FILTER (WHERE known_combo) AS known_combo_requests,
            count(*) FILTER (WHERE requester_known) AS known_requester_requests,
            count(DISTINCT root_event_id) FILTER (WHERE root_event_id IS NOT NULL) AS root_events,
            'RETAINED_OBSERVED_SUBSET' AS analysis_population,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE g.gap_start_us<hour_start_us+3600000000
                AND g.gap_end_us>hour_start_us
            ) THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
              ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status
          FROM hourly GROUP BY create_date,utc_hour,category,coalesce(sport,'_UNMAPPED')
            ,hour_start_us
          ORDER BY create_date,utc_hour,category,sport
        """,
        "rfq_hour_coverage": """
          WITH limits AS (
            SELECT (scan_start_ns//3600000000000)*3600000000000 AS start_ns,
              (scan_end_ns//3600000000000)*3600000000000 AS end_ns
            FROM rfq_scan_counts
          ), hours AS (
            SELECT hour_start_ns
            FROM limits,range(start_ns,end_ns+3600000000000,3600000000000)
              AS x(hour_start_ns)
          ), requests AS (
            SELECT (create_receive_us//3600000000)*3600000000 AS hour_start_us,
              count(*) AS observed_requests
            FROM rfq_requests GROUP BY hour_start_us
          )
          SELECT cast(to_timestamp(h.hour_start_ns/1000000000.0) AS DATE) AS date,
            extract('hour' FROM to_timestamp(h.hour_start_ns/1000000000.0))::INTEGER
              AS utc_hour,
            h.hour_start_ns//1000 AS hour_start_us,
            coalesce(r.observed_requests,0) AS requests_in_consumed_objects,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE g.gap_start_us<h.hour_start_ns//1000+3600000000
                AND g.gap_end_us>h.hour_start_ns//1000
            ) THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
              ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE g.gap_start_us<h.hour_start_ns//1000+3600000000
                AND g.gap_end_us>h.hour_start_ns//1000
            ) THEN 'NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP'
              ELSE 'COUNT_WITHIN_CONSUMED_OBJECTS' END AS zero_interpretation
          FROM hours h LEFT JOIN requests r
            ON r.hour_start_us=h.hour_start_ns//1000
          ORDER BY h.hour_start_ns
        """,
        "rfq_flow_minute": """
          WITH minute AS (
            SELECT *,(create_receive_us//60000000)*60000000 AS minute_receive_us
            FROM rfq_requests
          ) SELECT create_date,minute_receive_us,
            category,coalesce(sport,'_UNMAPPED') AS sport,count(*) AS requests,
            min(create_receive_us) AS first_receive_us,max(create_receive_us) AS last_receive_us,
            'RETAINED_OBSERVED_SUBSET' AS analysis_population,
            CASE WHEN EXISTS (
              SELECT 1 FROM rfq_quarantine_gaps g
              WHERE g.gap_start_us<minute_receive_us+60000000
                AND g.gap_end_us>minute_receive_us
            ) THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
              ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status
          FROM minute GROUP BY create_date,minute_receive_us,category,coalesce(sport,'_UNMAPPED')
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
                AS requests_in_p99_burst_minutes,
              'RETAINED_OBSERVED_SUBSET' AS analysis_population,
              CASE WHEN bool_or(m.object_coverage_status=
                    'PARTIAL_OBJECT_COVERAGE_QUARANTINED')
                THEN 'PARTIAL_OBJECT_COVERAGE_QUARANTINED'
                ELSE 'NO_KNOWN_OBJECT_QUARANTINE_OVERLAP' END AS object_coverage_status
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
              known_combo,contracts_e2,target_cost_e6,observation_segment,
              create_receive_us-lag(create_receive_us) OVER (
                PARTITION BY requester_hash,observation_segment
                ORDER BY create_receive_us,request_key) AS repeat_interval_us
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

    Retained-object RFQ flow/lifecycle remains unsampled. Only the nine-window CLOB
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
            NOT EXISTS (SELECT 1 FROM rfq_quarantine_gaps q
              WHERE q.gap_start_us<a.control_anchor_us
                AND q.gap_end_us>a.control_anchor_us-120000000)
              AND NOT EXISTS (SELECT 1 FROM rfq_observation_boundaries b
                WHERE b.boundary_ns>a.control_anchor_us*1000-120000000000
                  AND b.boundary_ns<=a.control_anchor_us*1000)
              AS prior_control_lookback_observed,
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
          WITH anchors AS (
            SELECT a.*,
              NOT EXISTS (SELECT 1 FROM rfq_quarantine_gaps q
                WHERE q.gap_start_us<a.control_anchor_us+120000000
                  AND q.gap_end_us>a.control_anchor_us)
              AND NOT EXISTS (SELECT 1 FROM rfq_observation_boundaries b
                WHERE b.boundary_ns>a.control_anchor_us*1000
                  AND b.boundary_ns<=a.control_anchor_us*1000+120000000000)
                AS future_control_outcome_observed
            FROM rfq_clob_anchors a
          )
          SELECT a.request_key,a.market_ticker,a.endpoint_type,a.anchor_role,
            count(e.event_us) AS rfq_events_in_control_outcome_120s,
            a.future_control_outcome_observed
          FROM anchors a LEFT JOIN rfq_market_event_times e
            ON e.market_ticker=a.market_ticker
              AND e.event_us>a.control_anchor_us
              AND e.event_us<=a.control_anchor_us+120000000
          GROUP BY a.request_key,a.market_ticker,a.endpoint_type,a.anchor_role,
            a.future_control_outcome_observed
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
              AND NOT EXISTS (SELECT 1 FROM rfq_quarantine_gaps q
                WHERE q.gap_start_us<s.end_us AND q.gap_end_us>s.start_us)
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
            AND q.prior_control_lookback_observed
            AND f.future_control_outcome_observed
            AND q.same_receive_date AND q.same_receive_hour AND q.same_tts_regime
            AND q.anchor_tts_regime<>'UNKNOWN' AND q.control_dim_eligible AS matched,
            q.prior_120s_rfq_free,q.prior_control_lookback_observed,
            f.rfq_events_in_control_outcome_120s,f.future_control_outcome_observed,
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
        "rfq_observation_boundaries", "rfq_quarantine_gaps",
        "rfq_flow_daily", "rfq_event_flow_hourly", "rfq_flow_hourly",
        "rfq_hour_coverage", "rfq_flow_minute",
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
    coverage_label = (
        "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        if scalar(connection, "SELECT count(*) FROM rfq_quarantine_gaps")
        else "COMPLETE_MANIFEST_OBJECT_COVERAGE"
    )
    chart_banner = coverage_label + " · " + BANNER

    hourly = connection.execute("""
      SELECT date,utc_hour,requests_in_consumed_objects,object_coverage_status
      FROM rfq_hour_coverage ORDER BY date,utc_hour
    """).fetchall()
    dates = sorted({str(row[0]) for row in hourly})
    matrix = [[0] * 24 for _ in dates]
    for date, hour, value, coverage_status in hourly:
        matrix[dates.index(str(date))][int(hour)] = (
            math.nan if coverage_status == "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
            else int(value)
        )
    fig, ax = plt.subplots(figsize=(12, 3 + len(dates)))
    colour_map = plt.get_cmap("viridis").copy()
    colour_map.set_bad("#bdbdbd")
    image = ax.imshow(matrix, aspect="auto", cmap=colour_map)
    ax.set_yticks(range(len(dates)), dates); ax.set_xticks(range(24))
    ax.set_xlabel("UTC hour"); ax.set_title(
        "RFQ create flow by receive-clock hour (grey = quarantined/unknown)\n"
        + chart_banner
    )
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
    ax.set_title("RFQ create inter-arrival distribution\n" + chart_banner)
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
    ax.set_title("Retained-object RFQ size and target-cost tails\n" + chart_banner)
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
    ax.set_title(
        "Known-combo leg-count distribution (lower-bound population)\n" + chart_banner
    )
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
    ax.set_title("RFQ time-to-start / match-phase mix\n" + chart_banner)
    fig.tight_layout(); path = chart_dir / "rfq_tts_mix.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))

    km = read_downsampled_km(connection, points=2000)
    fig, ax = plt.subplots(figsize=(10, 6))
    if km:
        ax.step([max(0.001, row[0] / 1000.0) for row in km], [row[4] for row in km], where="post")
    ax.set_xscale("log"); ax.set_xlabel("RFQ receive-clock age (seconds)")
    ax.set_ylabel("Kaplan–Meier survival")
    ax.set_title(
        "Retained-object RFQ lifecycle with replacement/boundary censoring\n"
        + chart_banner
    )
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
    ax.set_title("Requester concentration (known-ID subset only)\n" + chart_banner)
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
    ax.set_title("Lifecycle censoring and requester-ID coverage\n" + chart_banner)
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
    fig.suptitle(
        "RFQ-create → CLOB receive-clock event study (matched diagnostics)\n"
        + chart_banner
    )
    fig.tight_layout(); path = chart_dir / "rfq_clob_event_study.png"
    fig.savefig(path, dpi=150); plt.close(fig); output.append(str(path.relative_to(run_dir)))
    return output


def build_summary(connection, inputs: dict, clob: dict, elapsed: float) -> dict:
    run_id = inputs.get("run_id")
    cycle1_binding = inputs.get("cycle1_duckdb_binding")
    if not isinstance(run_id, str) or not run_id:
        raise RFQStageError("RFQ summary run_id is missing")
    if not isinstance(cycle1_binding, dict):
        raise RFQStageError("RFQ summary Cycle-1 DuckDB binding is missing")
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
    gap_rows = rows_as_dicts(connection, """
      SELECT release_id,key,sha256,quarantined_keys_json,
        quarantined_object_count,quarantined_object_set_sha256,
        gap_start_ns,gap_end_ns,gap_start_us,gap_end_us,boundary_reason
      FROM rfq_quarantine_gaps ORDER BY gap_start_ns,gap_end_ns,key
    """)
    gap_set_sha256 = hashlib.sha256(
        json.dumps(gap_rows, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")
    ).hexdigest()
    summary = {
        "schema": "sports-autoresearch-rfq-full-stage-v1",
        "run_id": run_id,
        "generated_at_utc": utc_now(),
        "banner": BANNER,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "evidence": EVIDENCE,
        "timestamp": "TL1_RECEIVE_CLOCK",
        "analysis_scope": "DESCRIPTIVE_DISCOVERY_ONLY",
        "status": (
            "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
            if not inputs["full_object_coverage"]
            else "DESCRIPTIVE_DISCOVERY_ONLY"
        ),
        "input": {
            "release_ids": list(RELEASE_IDS),
            "coverage_status": inputs["coverage_status"],
            "full_object_coverage": inputs["full_object_coverage"],
            "whole_object_quarantine": inputs["quarantined_unique_objects"] > 0,
            "line_salvage": False,
            "logical_manifest_bindings_total": inputs["logical_manifest_bindings"],
            "unique_objects_total": inputs["unique_objects_total"],
            "unique_bytes_total": inputs["unique_bytes_total"],
            "consumed_unique_objects": inputs["consumed_unique_objects"],
            "consumed_logical_bindings": inputs["consumed_logical_bindings"],
            "consumed_bytes": inputs["consumed_bytes"],
            "consumed_object_set_sha256": inputs["consumed_object_set_sha256"],
            "quarantined_unique_objects": inputs["quarantined_unique_objects"],
            "quarantined_logical_bindings": inputs["quarantined_logical_bindings"],
            "quarantined_bytes": inputs["quarantined_bytes"],
            "quarantined_object_set_sha256": inputs["quarantined_object_set_sha256"],
            "quarantine_reasons": inputs["quarantine_reasons"],
            "quarantine_details": inputs["quarantine_details"],
            "quarantine_gap_plan": quarantine_gap_plan(inputs),
            "failed_attempt_binding": inputs.get("failed_attempt_binding"),
            "failed_attempt_bindings": inputs.get("failed_attempt_bindings", []),
            "repair_chain": inputs.get("repair_chain", []),
            "expected_success_resource": inputs.get("expected_success_resource"),
            "cycle1_duckdb_binding": cycle1_binding,
            "deduplicated_overlapping_objects": inputs["deduplicated_overlapping_objects"],
            "overlap_keys": inputs["overlap_keys"],
            "manifest_object_set_sha256": inputs["path_size_fingerprint_sha256"],
            "selection_fingerprint_sha256": inputs["selection_fingerprint_sha256"],
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
            "rfq_object_coverage": inputs["coverage_status"],
            "quarantine_gap_count": len(gap_rows),
            "quarantined_object_count": inputs["quarantined_unique_objects"],
            "quarantine_observation_boundary_count": len(gap_rows) * 2,
            "quarantine_gap_ranges": gap_rows,
            "quarantine_gap_set_sha256": gap_set_sha256,
            "partial_object_coverage_hours": scalar(
                connection,
                "SELECT count(*) FROM rfq_hour_coverage "
                "WHERE object_coverage_status='PARTIAL_OBJECT_COVERAGE_QUARANTINED'",
            ),
            "quarantined_hours_are_not_observed_zero": True,
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
            "RFQ object coverage is partial when a manifest-bound whole-object quarantine is listed; no full-scan or complete-flow claim is permitted.",
            "RFQ per-event sequence is not mandatory; sequence completeness is not claimed.",
            "Lifecycle and interarrival calculations are segmented at every conservative quarantine gap; adjacent quarantined shards form one contiguous gap, and CLOB windows intersecting any gap are invalid.",
            "A next genuine same-ID create or the first explicit loss/close/error/epoch boundary, whichever occurs first, censors the prior lifecycle; residual scan-end censoring can still combine true survival with unobserved endpoints.",
            "Known-combo share is a lower bound; no-combo-evidence is not proof of single/HVM status.",
            "CLOB event studies use only dim-effective anchors and a deterministic root-balanced sample, not the retained RFQ population.",
            "Prior controls are match-eligible only when their full 120-second causal lookback starts at or after dim_effective_us.",
            "Control matching adjusts only observed state and cannot remove unobserved game-state confounding.",
        ],
        "wall_seconds": round(elapsed, 3),
    }
    parser_binding = inputs.get("inner_payload_parser_contract")
    if parser_binding is not None:
        summary["input"].update({
            "inner_payload_parser_contract": parser_binding,
            "registered_rfq_query_sha256": inputs.get(
                "registered_rfq_query_sha256"
            ),
        })
    if inputs.get("rfq_resource_contract") is not None:
        summary["input"]["rfq_resource_contract"] = inputs[
            "rfq_resource_contract"
        ]
    if inputs.get("consumer_wiring_contract") is not None:
        summary["input"]["consumer_wiring_contract"] = inputs[
            "consumer_wiring_contract"
        ]
    if inputs.get("validate_run_status_wiring_contract") is not None:
        summary["input"]["validate_run_status_wiring_contract"] = inputs[
            "validate_run_status_wiring_contract"
        ]
    return summary


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


def _enforce_registered_resource_contract(
    args, inputs: dict, run_dir: Path
) -> None:
    """Reject repair-04 command or execution-entry drift before new evidence."""
    registered = inputs.get("rfq_resource_contract")
    if registered is None:
        return
    if (
        not isinstance(registered, dict)
        or set(registered) != {
            "path", "sha256", "schema_version", "current_runtime",
        }
        or registered.get("path") != REPAIR04_RESOURCE_CONTRACT_PATH
        or not isinstance(registered.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", registered["sha256"]) is None
        or registered.get("schema_version") != REPAIR04_RESOURCE_SCHEMA
        or registered.get("current_runtime") != REPAIR04_RUNTIME_CONTRACT
    ):
        raise RFQStageError("repair-04 registered resource contract is invalid")
    observed = {
        "memory_limit": args.memory_limit,
        "max_temp_size": args.max_temp_size,
        "threads": args.threads,
        "min_free_gib": float(args.min_free_gib),
        "clob_max_per_root": args.clob_max_per_root,
        "resume": bool(args.resume),
        "keep_scratch": bool(args.keep_scratch),
    }
    if observed != REPAIR04_RUNTIME_CONTRACT:
        raise RFQStageError(
            "repair-04 runtime arguments differ from the preregistered resource contract"
        )
    registered_query_sha = inputs.get("registered_rfq_query_sha256")
    expected_entry = run_dir.resolve() / REPAIR04_EXECUTION_QUERY
    executed_entry = Path(__file__).resolve()
    if (
        not isinstance(registered_query_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", registered_query_sha) is None
        or expected_entry.is_symlink()
        or not expected_entry.is_file()
        or executed_entry != expected_entry.resolve()
        or sha256(expected_entry) != registered_query_sha
    ):
        raise RFQStageError(
            "repair-04 execution entry is not the registered frozen RFQ query"
        )


def _enforce_registered_consumer_wiring_contract(
    inputs: dict, run_dir: Path
) -> None:
    """Reject repair-05 wiring drift before state, input, or scratch mutation."""
    binding = inputs.get("consumer_wiring_contract")
    if binding is None:
        return
    if (
        not isinstance(binding, dict)
        or set(binding) != {"path", "sha256", "schema_version"}
        or binding.get("path") != REPAIR05_WIRING_CONTRACT
        or binding.get("schema_version") != REPAIR05_WIRING_SCHEMA
        or not isinstance(binding.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
    ):
        raise RFQStageError("repair-05 registered consumer wiring contract is invalid")
    contract = _require_run_relative_file(
        run_dir, binding["path"], "repair-05 registered consumer wiring contract"
    )
    if sha256(contract) != binding["sha256"]:
        raise RFQStageError("repair-05 registered consumer wiring contract changed")


def _enforce_registered_validate_run_status_wiring_contract(
    inputs: dict, run_dir: Path
) -> None:
    """Reject repair-06 status-wiring drift before any governed RFQ write."""
    binding = inputs.get("validate_run_status_wiring_contract")
    if binding is None:
        return
    if (
        not isinstance(binding, dict)
        or set(binding) != {"path", "sha256", "schema_version"}
        or binding.get("path") != REPAIR06_STATUS_WIRING_CONTRACT
        or binding.get("schema_version") != REPAIR06_STATUS_WIRING_SCHEMA
        or not isinstance(binding.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
    ):
        raise RFQStageError(
            "repair-06 registered validate_run status wiring contract is invalid"
        )
    contract = _require_run_relative_file(
        run_dir,
        binding["path"],
        "repair-06 registered validate_run status wiring contract",
    )
    if sha256(contract) != binding["sha256"]:
        raise RFQStageError(
            "repair-06 registered validate_run status wiring contract changed"
        )


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
    parser.add_argument("--validate-run-preflight-only", action="store_true")
    parser.add_argument("--validate-run-preflight-overlay", type=Path)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("threads must be positive")
    started = time.time()
    run_dir = args.run_dir.resolve()
    preflight_overlay = args.validate_run_preflight_overlay
    if preflight_overlay is not None:
        if not args.validate_run_preflight_only:
            raise RFQStageError(
                "validate_run preflight overlay requires preflight-only mode"
            )
        expected_overlay = (
            run_dir / REPAIR06_ROOT / "post_repair"
        ).resolve()
        if (
            preflight_overlay.is_symlink()
            or not preflight_overlay.is_dir()
            or preflight_overlay.resolve() != expected_overlay
        ):
            raise RFQStageError(
                "repair-06 validate_run preflight overlay is missing or unsafe"
            )
        preflight_overlay = expected_overlay
    manifest = (
        validate_run(run_dir)
        if preflight_overlay is None
        else validate_run(run_dir, manifest_root=preflight_overlay)
    )
    if args.validate_run_preflight_only:
        print(f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_dir.name}")
        return 0
    inputs = discover_inputs(args.cache_root.resolve())
    validate_input_bindings(manifest, inputs)
    # Security ordering: repair-03's immutable, per-object absence proof must
    # complete before scan_sql sees typed marker NULL values (typed JSON cannot
    # distinguish a missing field from explicit JSON null).
    inputs = apply_object_quarantine(run_dir, manifest, inputs)
    _enforce_registered_resource_contract(args, inputs, run_dir)
    _enforce_registered_consumer_wiring_contract(inputs, run_dir)
    _enforce_registered_validate_run_status_wiring_contract(inputs, run_dir)
    scratch = run_dir / "cache/rfq_full_scratch.duckdb"
    state_path = run_dir / "REPORT/tables/RFQ_FULL_STAGE_STATE.json"
    registered_repair_active = bool(inputs.get("repair_chain"))
    if registered_repair_active and args.resume:
        raise RFQStageError(
            "preregistered RFQ repair forbids resume; a fresh scratch is mandatory"
        )
    if registered_repair_active and (
        os.path.lexists(scratch) or os.path.lexists(Path(str(scratch) + ".wal"))
    ):
        raise RFQStageError(
            "preregistered RFQ repair fresh scratch or WAL path already exists; "
            "aborting without resume"
        )
    import duckdb

    if duckdb.__version__ != EXPECTED_DUCKDB:
        raise RFQStageError(
            f"DuckDB version drift (runner={duckdb.__version__}, expected={EXPECTED_DUCKDB})"
        )
    core_database = run_dir / "cache/cycle1.duckdb"
    inputs["run_id"] = run_dir.name
    inputs["cycle1_duckdb_binding"] = validate_cycle1_duckdb_binding(
        run_dir, manifest, core_database
    )
    input_identity = {
        "schema": (
            "rfq-full-input-identity-v6"
            if inputs.get("validate_run_status_wiring_contract") is not None
            else (
                "rfq-full-input-identity-v5"
                if inputs.get("consumer_wiring_contract") is not None
                else (
                    "rfq-full-input-identity-v4"
                    if inputs.get("rfq_resource_contract") is not None
                    else (
                        "rfq-full-input-identity-v3"
                        if inputs.get("inner_payload_parser_contract") is not None
                        else "rfq-full-input-identity-v2"
                    )
                )
            )
        ),
        "run_id": run_dir.name,
        "release_ids": list(RELEASE_IDS),
        "releases": inputs["releases"],
        "coverage_status": inputs["coverage_status"],
        "full_object_coverage": inputs["full_object_coverage"],
        "whole_object_quarantine": inputs["quarantined_unique_objects"] > 0,
        "line_salvage": False,
        "logical_manifest_bindings_total": inputs["logical_manifest_bindings"],
        "unique_objects_total": inputs["unique_objects_total"],
        "unique_bytes_total": inputs["unique_bytes_total"],
        "consumed_unique_objects": inputs["consumed_unique_objects"],
        "consumed_logical_bindings": inputs["consumed_logical_bindings"],
        "consumed_bytes": inputs["consumed_bytes"],
        "consumed_object_set_sha256": inputs["consumed_object_set_sha256"],
        "quarantined_unique_objects": inputs["quarantined_unique_objects"],
        "quarantined_logical_bindings": inputs["quarantined_logical_bindings"],
        "quarantined_bytes": inputs["quarantined_bytes"],
        "quarantined_object_set_sha256": inputs["quarantined_object_set_sha256"],
        "quarantine_reasons": inputs["quarantine_reasons"],
        "quarantine_details": inputs["quarantine_details"],
        "quarantine_gap_plan": quarantine_gap_plan(inputs),
        "failed_attempt_binding": inputs.get("failed_attempt_binding"),
        "failed_attempt_bindings": inputs.get("failed_attempt_bindings", []),
        "repair_chain": inputs.get("repair_chain", []),
        "expected_success_resource": inputs.get("expected_success_resource"),
        "cycle1_duckdb_binding": inputs["cycle1_duckdb_binding"],
        "deduplicated_overlapping_objects": inputs["deduplicated_overlapping_objects"],
        "manifest_object_set_sha256": inputs["path_size_fingerprint_sha256"],
        "selection_fingerprint_sha256": inputs["selection_fingerprint_sha256"],
        "consumed_objects": [
            {key: row[key] for key in ("key", "sha256", "size", "bound_release_ids")}
            for row in inputs["objects_detail"]
        ],
    }
    if inputs.get("inner_payload_parser_contract") is not None:
        input_identity.update({
            "inner_payload_parser_contract": inputs[
                "inner_payload_parser_contract"
            ],
            "registered_rfq_query_sha256": inputs[
                "registered_rfq_query_sha256"
            ],
        })
    if inputs.get("rfq_resource_contract") is not None:
        input_identity["rfq_resource_contract"] = inputs["rfq_resource_contract"]
    if inputs.get("consumer_wiring_contract") is not None:
        input_identity["consumer_wiring_contract"] = inputs[
            "consumer_wiring_contract"
        ]
    if inputs.get("validate_run_status_wiring_contract") is not None:
        input_identity["validate_run_status_wiring_contract"] = inputs[
            "validate_run_status_wiring_contract"
        ]
    free = shutil.disk_usage(run_dir).free
    required = max(args.min_free_gib * 2**30, inputs["consumed_bytes"] * 1.75)
    if free < required:
        raise RFQStageError(
            f"insufficient disk headroom: free={free/2**30:.1f}GiB "
            f"required={required/2**30:.1f}GiB"
        )
    if scratch.exists() and not args.resume:
        raise RFQStageError("RFQ scratch DB exists; pass --resume after inspecting stage state")
    previous_state = None
    if scratch.exists() and args.resume:
        if not state_path.is_file():
            raise RFQStageError("resume requested but RFQ stage state is missing")
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))
        if previous_state.get("input_fingerprint") != inputs["selection_fingerprint_sha256"]:
            raise RFQStageError("resume input fingerprint mismatch")

    state = dict(previous_state or {})
    state.update({
        "schema": "rfq-full-stage-state-v1", "run_id": run_dir.name,
        "started_at_utc": utc_now(),
        "status": "RUNNING", "input_fingerprint": inputs["selection_fingerprint_sha256"],
        "scratch": str(scratch), "resume": args.resume,
        "repair_chain": inputs.get("repair_chain", []),
        "failed_attempt_bindings": inputs.get("failed_attempt_bindings", []),
        "quarantine_gap_plan": quarantine_gap_plan(inputs),
        "expected_success_resource": inputs.get("expected_success_resource"),
    })
    if inputs.get("inner_payload_parser_contract") is not None:
        state.update({
            "inner_payload_parser_contract": inputs[
                "inner_payload_parser_contract"
            ],
            "registered_rfq_query_sha256": inputs[
                "registered_rfq_query_sha256"
            ],
        })
    if inputs.get("rfq_resource_contract") is not None:
        state["rfq_resource_contract"] = inputs["rfq_resource_contract"]
    if inputs.get("consumer_wiring_contract") is not None:
        state["consumer_wiring_contract"] = inputs[
            "consumer_wiring_contract"
        ]
    if inputs.get("validate_run_status_wiring_contract") is not None:
        state["validate_run_status_wiring_contract"] = inputs[
            "validate_run_status_wiring_contract"
        ]
    connection = None
    attempt_started = False
    try:
        # This is the active-evidence transaction boundary.  Every failure from
        # the first governed input/RUNNING write through connect, configure, and all stage
        # work is converted into the governed failed-attempt state below.
        attempt_started = True
        write_json(state_path, state)
        write_json(
            run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json", input_identity
        )
        connection = duckdb.connect(str(scratch))
        configure(
            connection, run_dir, args.memory_limit, args.max_temp_size, args.threads
        )
        build_scan_tables(
            connection, inputs["paths"], run_dir.name, inputs["quarantine_boundaries"]
        )
        state.update({"phase": "RAW_SCHEMA_AND_DEDUP_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        build_request_tables(connection, run_dir.name)
        state.update({"phase": "LIFECYCLE_AND_LEGS_COMPLETE", "updated_at_utc": utc_now()})
        write_json(state_path, state)
        runtime_cycle1_binding = validate_cycle1_duckdb_binding(
            run_dir, manifest, core_database
        )
        if runtime_cycle1_binding != inputs["cycle1_duckdb_binding"]:
            raise RFQStageError("Cycle-1 DuckDB binding changed during RFQ scan")
        attach_core_and_enrich(connection, core_database)
        build_descriptive_tables(connection)
        state.update({
            "phase": "RETAINED_OBJECT_DESCRIPTIVE_COMPLETE",
            "updated_at_utc": utc_now(),
        })
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
        report_title = (
            "# Partial RFQ exploratory stage"
            if not inputs["full_object_coverage"]
            else "# RFQ retained-object exploratory stage"
        )
        note = [
            report_title, "", f"> **{inputs['coverage_status']} · {BANNER}**", "",
            "The consumed manifest-bound RFQ objects were scanned with strict parsing. The exact",
            "whole-object quarantine recorded in the input identity contributes no row or result;",
            "RFQ object coverage is therefore partial and this is not a full-coverage claim.",
            "Lifecycle endpoints use the",
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
            "status": stage_completion_status(inputs), "phase": "COMPLETE",
            "completed_at_utc": utc_now(), "wall_seconds": round(time.time() - started, 3),
            "summary": "REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json",
        })
        write_json(state_path, state)
    except Exception as exc:
        if attempt_started:
            state.update({
                "status": (
                    "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
                    if registered_repair_active else "FAILED_RESUMABLE"
                ),
                "resume": False if registered_repair_active else args.resume,
                "next_required_authority": (
                    "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
                    if registered_repair_active else None
                ),
                "failed_at_utc": utc_now(),
                "error_type": type(exc).__name__, "error": str(exc)[:2000],
            })
            write_json(state_path, state)
        raise
    finally:
        if connection is not None:
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
