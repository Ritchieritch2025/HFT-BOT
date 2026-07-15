#!/usr/bin/env python3
"""Fail-closed MODE-1 mission finalizer for SPORTS-AUTORESEARCH-01.

The finalizer does not estimate a research effect.  It consumes the frozen
Cycle-1 stage receipts, applies one outcome-independent conservative rule to
the three RFQ cards, and refuses to complete unless every preregistered card
has a terminal exploratory status.  ``RUN_MANIFEST.json`` is the last file
written and is therefore the commit record for mission completion.

This module has no network, AWS, exchange, order, or S3-write capability.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence


EXPECTED_MISSION_SHA = (
    "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
)
EXPECTED_MODE = "EXPLORATORY_AUTORESEARCH"
EXPECTED_EVIDENCE = "SEALED_DEGRADED_EVIDENCE"
EXPECTED_SPLIT = "EXPLORATORY_ONLY"
EXPECTED_TIMESTAMP = "TL1"
NO_LIVE_BANNER = "NOT A LIVE-TRADING AUTHORIZATION"
EXPECTED_W09_INSTALLATION_SHA256 = {
    "/usr/local/bin/research_data": "68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f",
    "/opt/w09/research/tools/research_data_instance_profile.py": "af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf",
    "/usr/local/bin/w09-run": "6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321",
    "/etc/w09/cost-contract.json": "bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352",
}
EXPECTED_RELEASE_BINDINGS = {
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03": {
        "date": "2026-07-12",
        "manifest_sha256": "6fedbd5d2b0d811b5189a953331bd236aeea3fb5843a5a549d1100d5ee290505",
        "object_version_set_sha256": "73ca061942dff3d6554ae4d69073d2f64c2337e18eae64ef1aca06dd8a7dff32",
        "version_bindings_sha256": "8de524a053105dcd33a828bd8dd782ef255b7d51ce83398ca28fcfeb954c69f0",
    },
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5": {
        "date": "2026-07-13",
        "manifest_sha256": "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd",
        "object_version_set_sha256": "60846563c6ffcb1c34e7b926ee44a281aecbaa2a81edcf6cc00d5d070abd68d4",
        "version_bindings_sha256": "f79e3114155408e9467bfa67fffdd12069355d2af9adbf66e38eeffd99322643",
    },
}

HYPOTHESIS_IDS = (
    "C1-SPREAD-CAPTURE-01",
    "C1-HFOLLOW-RETREAT-01",
    "C1-DEPLETION-REFILL-01",
    "C1-THREEWAY-OVERROUND-01",
    "C1-SOCCER-POISSON-RV-01",
    "C1-LARGE-FLOW-CONTINUATION-01",
    "C1-PREMATCH-TTS-01",
    "C1-RFQ-CLOB-01",
    "C1-ANOM-RFQ-SIZE-TAIL-01",
    "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
)
CORE_TEST_IDS = {
    "C1-SPREAD-CAPTURE-01",
    "C1-LARGE-FLOW-CONTINUATION-01",
    "C1-PREMATCH-TTS-01",
}
L2_TEST_IDS = {"C1-HFOLLOW-RETREAT-01", "C1-DEPLETION-REFILL-01"}
RV_IDS = {"C1-THREEWAY-OVERROUND-01", "C1-SOCCER-POISSON-RV-01"}
RFQ_IDS = {
    "C1-RFQ-CLOB-01",
    "C1-ANOM-RFQ-SIZE-TAIL-01",
    "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
}
RFQ_TRIAL_ORDER = (
    "C1-RFQ-CLOB-01",
    "C1-ANOM-RFQ-SIZE-TAIL-01",
    "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
)

REPAIR_PENDING_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED"
REPAIR02_PENDING_STATUS = (
    "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
)
REPAIR03_PENDING_STATUS = (
    "CYCLE1_CORE_COMPLETE_RFQ_PARSER_REPAIR03_REGISTERED"
)
REPAIR04_PENDING_STATUS = (
    "CYCLE1_CORE_COMPLETE_RFQ_RESOURCE_REPAIR04_REGISTERED"
)
REPAIR05_PENDING_STATUS = (
    "CYCLE1_CORE_COMPLETE_RFQ_CONSUMER_WIRING_REPAIR05_REGISTERED"
)
RFQ_PARTIAL_STATUS = "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
RFQ_ANALYSIS_SCOPE = "DESCRIPTIVE_DISCOVERY_ONLY"
RFQ_DECLARATION = Path("DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE.json")
RFQ_MALFORMED_RECEIPT = Path(
    "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT.json"
)
RFQ_INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
RFQ_QUARANTINE_GAPS = Path("REPORT/tables/rfq_quarantine_gaps.csv")
RFQ_HOUR_COVERAGE = Path("REPORT/tables/rfq_hour_coverage.csv")
ACTIVE_RFQ_STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
ACTIVE_RFQ_REPAIR_RESOURCE = Path("logs/resources/rfq_full_stage_repair01.json")
RFQ_DECLARATION_02 = Path("DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE_02.json")
RFQ_MALFORMED_RECEIPT_02 = Path(
    "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT_02.json"
)
RFQ_REPAIR_02_AUTHORIZATION = Path(
    "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
)
RFQ_STRUCTURAL_AUDIT_02 = Path(
    "DATA_INTEGRITY/RFQ_STRUCTURAL_INTEGRITY_AUDIT_02.json"
)
RFQ_STRUCTURAL_AUDIT_RESOURCE_02 = Path(
    "logs/resources/rfq_structural_integrity_audit02.json"
)
RFQ_SECOND_BLOCKER = Path(
    "DATA_INTEGRITY/RFQ_SECOND_MALFORMED_OBJECT_BLOCKER.json"
)
SESSION_RESUME_02 = Path("DATA_INTEGRITY/SESSION_RESUME_02.json")
ACTIVE_RFQ_REPAIR_RESOURCE_02 = Path(
    "logs/resources/rfq_full_stage_repair02.json"
)
ACTIVE_RFQ_REPAIR_RESOURCE_03 = Path(
    "logs/resources/rfq_full_stage_repair03.json"
)
ACTIVE_RFQ_REPAIR_RESOURCE_04 = Path(
    "logs/resources/rfq_full_stage_repair04.json"
)
ACTIVE_RFQ_REPAIR_RESOURCE_05 = Path(
    "logs/resources/rfq_full_stage_repair05.json"
)
REPAIR_DECLARATION = Path(
    "DATA_INTEGRITY/repairs/repair-01/QUARANTINE_DECLARATION.json"
)
REPAIR_MALFORMED_RECEIPT = Path(
    "DATA_INTEGRITY/repairs/repair-01/MALFORMED_OBJECT_RECEIPT.json"
)
REPAIR_REGISTRATION_RECEIPT = Path(
    "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
)
REPAIR_02_ROOT = Path("DATA_INTEGRITY/repairs/repair-02")
REPAIR_DECLARATION_02 = REPAIR_02_ROOT / "QUARANTINE_DECLARATION.json"
REPAIR_MALFORMED_RECEIPT_02 = REPAIR_02_ROOT / "MALFORMED_OBJECT_RECEIPT.json"
REPAIR_AUTHORIZATION_02 = REPAIR_02_ROOT / "USER_AUTHORIZATION.json"
REPAIR_REGISTRATION_RECEIPT_02 = REPAIR_02_ROOT / "REPAIR_REGISTRATION.json"
REPAIR_02_TRANSACTION_JOURNAL = REPAIR_02_ROOT / "TRANSACTION_JOURNAL.json"
REPAIR_02_PRE_ROOT = REPAIR_02_ROOT / "pre_repair"
REPAIR_03_ROOT = Path("DATA_INTEGRITY/repairs/repair-03")
REPAIR_REGISTRATION_RECEIPT_03 = REPAIR_03_ROOT / "REPAIR_REGISTRATION.json"
REPAIR_03_TRANSACTION_JOURNAL = REPAIR_03_ROOT / "TRANSACTION_JOURNAL.json"
REPAIR_03_AUTHORITY_BASIS = REPAIR_03_ROOT / "AUTHORITY_BASIS.json"
REPAIR_03_PARSER_CONTRACT = (
    REPAIR_03_ROOT / "RFQ_INNER_PAYLOAD_PARSER_CONTRACT.json"
)
REPAIR_03_AUDIT_SOURCE = (
    REPAIR_03_ROOT / "RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_SOURCE.py"
)
REPAIR_03_SOURCE_ATTESTATION_ACTIVE = Path(
    "DATA_INTEGRITY/REPAIR_03_SOURCE_ATTESTATION.json"
)
REPAIR_03_PRE_ROOT = REPAIR_03_ROOT / "pre_repair"
REPAIR_03_SOURCE_ATTESTATION_ARCHIVE = (
    REPAIR_03_PRE_ROOT / REPAIR_03_SOURCE_ATTESTATION_ACTIVE
)
FAILED_RFQ_STATE_03 = REPAIR_03_PRE_ROOT / ACTIVE_RFQ_STATE
FAILED_RFQ_RESOURCE_03 = REPAIR_03_PRE_ROOT / ACTIVE_RFQ_REPAIR_RESOURCE_02
FAILED_RFQ_SCRATCH_RECEIPT_03 = (
    REPAIR_03_PRE_ROOT / "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json"
)
FAILED_RFQ_INPUT_IDENTITY_03 = REPAIR_03_PRE_ROOT / RFQ_INPUT_IDENTITY
ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03 = (
    REPAIR_03_PRE_ROOT
    / "DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json"
)
ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_RESOURCE_03 = (
    REPAIR_03_PRE_ROOT
    / "logs/resources/rfq_inner_payload_contract_audit03_final.json"
)
ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_SOURCE_03 = (
    REPAIR_03_PRE_ROOT / "tmp/rfq_inner_payload_contract_audit03.py"
)
REPAIR_04_ROOT = Path("DATA_INTEGRITY/repairs/repair-04")
REPAIR_04_EXECUTION_QUERY = Path("queries/rfq_full_stage.py")
REPAIR_REGISTRATION_RECEIPT_04 = REPAIR_04_ROOT / "REPAIR_REGISTRATION.json"
REPAIR_04_TRANSACTION_JOURNAL = REPAIR_04_ROOT / "TRANSACTION_JOURNAL.json"
REPAIR_04_AUTHORITY_BASIS = REPAIR_04_ROOT / "AUTHORITY_BASIS.json"
REPAIR_04_RESOURCE_CONTRACT = REPAIR_04_ROOT / "RFQ_RESOURCE_CONTRACT.json"
REPAIR_04_SOURCE_ATTESTATION_ACTIVE = Path(
    "DATA_INTEGRITY/REPAIR_04_SOURCE_ATTESTATION.json"
)
REPAIR_04_PRE_ROOT = REPAIR_04_ROOT / "pre_repair"
REPAIR_04_SOURCE_ATTESTATION_ARCHIVE = (
    REPAIR_04_PRE_ROOT / REPAIR_04_SOURCE_ATTESTATION_ACTIVE
)
REPAIR_04_W09_ATTESTATION_ARCHIVE = (
    REPAIR_04_PRE_ROOT / "DATA_INTEGRITY/W09_ATTESTATION.json"
)
FAILED_RFQ_STATE_04 = REPAIR_04_PRE_ROOT / ACTIVE_RFQ_STATE
FAILED_RFQ_RESOURCE_04 = REPAIR_04_PRE_ROOT / ACTIVE_RFQ_REPAIR_RESOURCE_03
FAILED_RFQ_SCRATCH_RECEIPT_04 = (
    REPAIR_04_PRE_ROOT / "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json"
)
FAILED_RFQ_INPUT_IDENTITY_04 = REPAIR_04_PRE_ROOT / RFQ_INPUT_IDENTITY
REPAIR_05_ROOT = Path("DATA_INTEGRITY/repairs/repair-05")
REPAIR_05_EXECUTION_QUERY = Path("queries/rfq_full_stage.py")
REPAIR_REGISTRATION_RECEIPT_05 = REPAIR_05_ROOT / "REPAIR_REGISTRATION.json"
REPAIR_05_TRANSACTION_JOURNAL = REPAIR_05_ROOT / "TRANSACTION_JOURNAL.json"
REPAIR_05_AUTHORITY_BASIS = REPAIR_05_ROOT / "AUTHORITY_BASIS.json"
REPAIR_05_WIRING_CONTRACT = (
    REPAIR_05_ROOT / "RFQ_CONSUMER_VALIDATION_WIRING_CONTRACT.json"
)
REPAIR_05_PRE_ROOT = REPAIR_05_ROOT / "pre_repair"
REPAIR_05_BLOCKER_ACTIVE = Path(
    "DATA_INTEGRITY/RFQ_REPAIR04_CONSUMER_BLOCKER_05.json"
)
REPAIR_05_BLOCKER_ARCHIVE = REPAIR_05_PRE_ROOT / REPAIR_05_BLOCKER_ACTIVE
FAILED_RFQ_RESOURCE_05 = REPAIR_05_PRE_ROOT / ACTIVE_RFQ_REPAIR_RESOURCE_04
UNCHANGED_RFQ_STATE_05 = REPAIR_05_PRE_ROOT / ACTIVE_RFQ_STATE
UNCHANGED_RFQ_INPUT_IDENTITY_05 = REPAIR_05_PRE_ROOT / RFQ_INPUT_IDENTITY
REPAIR_05_W09_ATTESTATION_ARCHIVE = (
    REPAIR_05_PRE_ROOT / "DATA_INTEGRITY/W09_ATTESTATION.json"
)
REPAIR_05_SOURCE_ATTESTATION_ACTIVE = Path(
    "DATA_INTEGRITY/REPAIR_05_SOURCE_ATTESTATION.json"
)
REPAIR_05_SOURCE_ATTESTATION_ARCHIVE = (
    REPAIR_05_PRE_ROOT / REPAIR_05_SOURCE_ATTESTATION_ACTIVE
)
FAILED_RFQ_STATE_02 = REPAIR_02_PRE_ROOT / ACTIVE_RFQ_STATE
FAILED_RFQ_RESOURCE_02 = (
    REPAIR_02_PRE_ROOT / "logs/resources/rfq_full_stage_repair01.json"
)
FAILED_RFQ_SCRATCH_RECEIPT_02 = (
    REPAIR_02_PRE_ROOT / RFQ_MALFORMED_RECEIPT_02.parent
    / "RFQ_FAILED_SCRATCH_RECEIPT_02.json"
)
FAILED_RFQ_INPUT_IDENTITY_02 = REPAIR_02_PRE_ROOT / RFQ_INPUT_IDENTITY
ARCHIVED_RFQ_STRUCTURAL_AUDIT_02 = REPAIR_02_PRE_ROOT / RFQ_STRUCTURAL_AUDIT_02
ARCHIVED_RFQ_STRUCTURAL_AUDIT_RESOURCE_02 = (
    REPAIR_02_PRE_ROOT / RFQ_STRUCTURAL_AUDIT_RESOURCE_02
)
ARCHIVED_RFQ_SECOND_BLOCKER = REPAIR_02_PRE_ROOT / RFQ_SECOND_BLOCKER
ARCHIVED_SESSION_RESUME_02 = REPAIR_02_PRE_ROOT / SESSION_RESUME_02
FAILED_RFQ_STATE = Path(
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/REPORT/tables/"
    "RFQ_FULL_STAGE_STATE.json"
)
FAILED_RFQ_RESOURCE = Path(
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/logs/resources/"
    "rfq_full_stage.json"
)
FAILED_RFQ_SCRATCH_RECEIPT = Path(
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "RFQ_FAILED_SCRATCH_RECEIPT.json"
)
FAILED_RFQ_INPUT_IDENTITY = Path(
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "RFQ_FULL_INPUT_IDENTITY.json"
)
ACTIVE_CYCLE1_DUCKDB_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
ARCHIVED_CYCLE1_DUCKDB_BINDING = Path(
    "DATA_INTEGRITY/repairs/repair-01/pre_repair/DATA_INTEGRITY/"
    "CYCLE1_DUCKDB_BINDING.json"
)
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RFQ_PARTIAL_BOUNDARY = (
    "RFQ findings use only the retained observed subset after exactly two registered "
    "adjacent whole-object quarantines forming one contiguous missing interval; this gap "
    "can create temporal-selection bias, so these "
    "findings are partial descriptive diagnostics, not full-coverage estimates."
)
RFQ_REOPEN_CONDITION = (
    "Reopen the frozen RFQ studies only after a structurally valid immutable replacement "
    "or governed repair restores the quarantined interval, then rerun strict parsing from "
    "new scratch with complete object coverage and additional quality-assessed sealed days."
)

EXPECTED_RFQ_FULL_OBJECTS = 284
EXPECTED_RFQ_FULL_LOGICAL_BINDINGS = 296
EXPECTED_RFQ_FULL_BYTES = 59_719_895_414
EXPECTED_RFQ_FULL_LINES = 58_690_563
EXPECTED_RFQ_RETAINED_OBJECTS = 282
EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS = 294
EXPECTED_RFQ_RETAINED_BYTES = 59_185_856_724
EXPECTED_RFQ_QUARANTINED_OBJECTS = 2
EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS = 2
EXPECTED_RFQ_QUARANTINED_BYTES = 534_038_690
EXPECTED_RFQ_FULL_SET_SHA256 = (
    "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71"
)
EXPECTED_RFQ_RETAINED_SET_SHA256 = (
    "cf6885a13ac50369fbfb19aba3809cd7e83c8bdabe4381ec926ed1c61d47b652"
)
EXPECTED_RFQ_QUARANTINED_SET_SHA256 = (
    "cf0e874f65aad791c5a23164a34511bd0482d7ece9de0706841c5b8bc92808eb"
)
EXPECTED_RFQ_REPAIR02_SELECTION_SHA256 = (
    "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
)
EXPECTED_RFQ_RETAINED_LINES = 58_144_912
EXPECTED_RFQ_EMPTY_CONTROL_ROWS = 1_111
EXPECTED_RFQ_CONTROL_MARKER_COUNTS = {
    "hour_open": 575,
    "transport_close": 536,
}
EXPECTED_RFQ_BLANK_CONTROL_MARKERS = (
    "gap",
    "hour_open",
    "loss",
    "transport_close",
    "transport_error",
)
EXPECTED_RFQ_FAILED_STATE_03_SHA256 = (
    "cc59ac42d6a8204650d3f60554fddfce2af9e849522f0370ea030f08c9a078b6"
)
EXPECTED_RFQ_FAILED_RESOURCE_03_SHA256 = (
    "a5adca32034c7db02652433ca733fa3657ffeaadbda0809d7f33df8f11230766"
)
EXPECTED_RFQ_FAILED_INPUT_03_SHA256 = (
    "68a7e3aeb22851195769c5e4ab5b614979212e0469ac2584afb4de50870560e1"
)
EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_03_SHA256 = (
    "6cc399ef7dbcfa4c32adbbdcf507940d0253c2e7bbc6e40000da1cc3c176c197"
)
EXPECTED_RFQ_FAILED_SCRATCH_03_SHA256 = (
    "32b2352dc0390fa5a4f42f2a483bdfa570b9e2a7a6de77d51847afb38fd1cff5"
)
EXPECTED_RFQ_FAILED_SCRATCH_03_BYTES = 28_731_781_120
EXPECTED_RFQ_FAILED_SCRATCH_03_MTIME = "2026-07-15T15:28:30.567397717+00:00"
EXPECTED_RFQ_FAILED_STATE_04_SHA256 = (
    "3c826ee72fa69f9c02c4a38fa33d3c65006ba33374dc592c269388228d506e57"
)
EXPECTED_RFQ_FAILED_RESOURCE_04_SHA256 = (
    "08690ed35a3ef21a78654bec58f5637d31b9d97704d00cfbdb672a29adee5193"
)
EXPECTED_RFQ_FAILED_INPUT_04_SHA256 = (
    "c0ee7ed24d27c58eff4600d33bf7b6d283aaea38020b3a7aee0e63c9c06c2bbb"
)
EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_04_SHA256 = (
    "d850e185a4a46dcf711e8bad7e081be431cceae64ebd8bdc7a32fa5beecc5fb9"
)
EXPECTED_RFQ_FAILED_SCRATCH_04_SHA256 = (
    "f2174bfe998b992964ca8becf0c7191dc143587f5a9a326d4529e204bcd88d6a"
)
EXPECTED_RFQ_FAILED_SCRATCH_04_BYTES = 28_761_927_680
EXPECTED_RFQ_FAILED_SCRATCH_04_MTIME = "2026-07-15T17:00:33.148336873+00:00"
EXPECTED_RFQ_FAILED_RESOURCE_04_WALL_SECONDS = 909.999
EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_RSS_KIB = 40_379_312
EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_TEMP_BYTES = 15_023_231_048
EXPECTED_RFQ_FAILED_RESOURCE_04_MIN_FREE_BYTES = 109_311_180_800
EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256 = (
    "42c2cc06c4b3073c0e6a1b5a681e86eb03d7a0f3afda8f694e2d0f0932fdaa5f"
)
EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256 = (
    "e37d019ea272f9acacbe2c5b2464daebcb5a33b2eb54b1bf2665b3db6bcf11ef"
)
EXPECTED_RFQ_FAILED_RESOURCE_05_WALL_SECONDS = 18.118
EXPECTED_W09_MEMTOTAL_BYTES = 66_194_702_336
EXPECTED_RFQ_OOM_ERROR_04 = (
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
EXPECTED_RFQ_INNER_AUDIT_03_SHA256 = (
    "6d0dc5c16ddad6772657ed7a4ede1f90d255e6629ffd1babbd837b3863de7a0d"
)
EXPECTED_RFQ_INNER_AUDIT_RESOURCE_03_SHA256 = (
    "d820ac998a57428e64a46485cd17319c0430ef9c4c4e516afaac67bec4a3c05d"
)
EXPECTED_RFQ_INNER_AUDIT_SOURCE_03_SHA256 = (
    "27199557ea7aacf9a19f66d16f5fd3770ee0149ef0ed4cd081169a9e0ccb01ea"
)
EXPECTED_RFQ_AUDITED_PARSER_CONTRACT_03_SHA256 = (
    "9fd339e35584a38372ad4f14ba89f0d7a8a38bdd17ebcc3ab53c3d77d19c6389"
)
REPAIR_03_GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
REPAIR_03_SNAPSHOT_SOURCE_MODE = (
    "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
)
REPAIR_03_SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
EXPECTED_REPAIR_03_CHANGED_PATHS = [
    f"{REPAIR_03_SOURCE_RELATIVE}/finalize_mission.py",
    f"{REPAIR_03_SOURCE_RELATIVE}/repair03_registration.py",
    f"{REPAIR_03_SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{REPAIR_03_SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{REPAIR_03_SOURCE_RELATIVE}/test_repair03_registration.py",
    f"{REPAIR_03_SOURCE_RELATIVE}/test_rfq_full_stage.py",
]
REPAIR_04_GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
REPAIR_04_SNAPSHOT_SOURCE_MODE = (
    "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
)
REPAIR_04_SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
EXPECTED_REPAIR_04_CHANGED_PATHS = [
    f"{REPAIR_04_SOURCE_RELATIVE}/finalize_mission.py",
    f"{REPAIR_04_SOURCE_RELATIVE}/repair04_registration.py",
    f"{REPAIR_04_SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{REPAIR_04_SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{REPAIR_04_SOURCE_RELATIVE}/test_repair04_registration.py",
    f"{REPAIR_04_SOURCE_RELATIVE}/test_rfq_full_stage.py",
]
REPAIR_05_GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
REPAIR_05_SNAPSHOT_SOURCE_MODE = (
    "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
)
REPAIR_05_SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
EXPECTED_REPAIR_05_CHANGED_PATHS = [
    f"{REPAIR_05_SOURCE_RELATIVE}/finalize_mission.py",
    f"{REPAIR_05_SOURCE_RELATIVE}/repair05_registration.py",
    f"{REPAIR_05_SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{REPAIR_05_SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{REPAIR_05_SOURCE_RELATIVE}/test_repair05_registration.py",
    f"{REPAIR_05_SOURCE_RELATIVE}/test_rfq_full_stage.py",
]

# Closed MODE-1 outcomes.  CANDIDATE/DESCRIPTIVE_SURVIVOR/FROZEN are not
# terminal condition 4; promotion, confirmation and verdict labels are never
# accepted by this finalizer.
TERMINAL_MODE1_STATUSES = {
    "COLLECT_MORE",
    "DATA_STARVED",
    "REJECTED",
    "INVALIDATED_BY_DATA",
    "INVALIDATED_BY_LEAKAGE",
}
BANNED_TERMINAL_STATUSES = {
    "PROMOTION_READY",
    "VERDICT_PASS",
    "VERDICT_FAIL",
    "TRAIN_SURVIVOR",
    "VALIDATION",
    "HISTORICAL_CONFIRMATION",
    "LIVE_READY",
}

CORE_SUMMARY = Path("REPORT/CYCLE1_CORE_SUMMARY.json")
CORE_HYPOTHESIS_SUMMARY = Path("REPORT/tables/CORE_HYPOTHESIS_TESTS.json")
RFQ_SUMMARY = Path("REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json")
RFQ_CONCLUSIONS = Path("REPORT/tables/RFQ_HYPOTHESIS_CONCLUSIONS.json")
L2_SUMMARY_CANDIDATES = (
    Path("REPORT/tables/L2_HYPOTHESIS_TESTS.json"),
    Path("REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json"),
    Path("REPORT/tables/L2_HYPOTHESIS_SUMMARY.json"),
    Path("REPORT/L2_HYPOTHESIS_TESTS.json"),
    Path("REPORT/L2_HYPOTHESIS_STAGE_SUMMARY.json"),
)

REPORT_SECTION_TITLES = (
    "Executive conclusion",
    "What data was actually available",
    "What data was unavailable",
    "Evidence/timestamp/split labels",
    "Data-integrity findings",
    "Market atlas",
    "Regime atlas",
    "Market-making findings",
    "Directional findings",
    "Relative-value findings",
    "RFQ findings",
    "Novel anomalies unique to our data",
    "Hypothesis trial registry summary",
    "Multiple-testing correction",
    "Rejected hypotheses and why they died",
    "Data-starved hypotheses",
    "Strategy shortlist",
    "Detailed candidate dossiers",
    "Capacity and infrastructure implications",
    "Required additional data",
    "Required engine improvements",
    "Recommended next experiments",
    "Resource/cost report",
    "Full reproducibility instructions",
    "Limitations and prohibited interpretations",
)


class MissionFinalizationError(RuntimeError):
    """A fail-closed mission completion condition was not satisfied."""


class Conclusion(NamedTuple):
    hypothesis_id: str
    status: str
    reason: str
    source_path: str
    source_pointer: str

    def as_dict(self) -> dict[str, str]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "status": self.status,
            "status_reason": self.reason,
            "source_path": self.source_path,
            "source_json_pointer": self.source_pointer,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, value: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    if executable:
        temporary.chmod(
            temporary.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    os.replace(temporary, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MissionFinalizationError(f"required JSON artifact missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MissionFinalizationError(f"JSON artifact is not an object: {path}")
    return value


def require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MissionFinalizationError(f"{label} must be a non-negative integer")
    return value


def require_positive_int(value: Any, label: str) -> int:
    result = require_nonnegative_int(value, label)
    if result == 0:
        raise MissionFinalizationError(f"{label} must be positive")
    return result


def require_hex(value: Any, label: str, pattern: re.Pattern[str] = HEX64) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise MissionFinalizationError(f"{label} must be a canonical hexadecimal digest")
    return value


def checked_relative_path(run_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise MissionFinalizationError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise MissionFinalizationError(f"unsafe {label} path: {value}")
    absolute = run_dir / relative
    if not absolute.is_file():
        raise MissionFinalizationError(f"{label} artifact is missing: {value}")
    return absolute


def require_path_hash(
    run_dir: Path,
    relative: Path,
    expected: Any,
    label: str,
) -> Path:
    digest = require_hex(expected, f"{label} SHA-256")
    path = checked_relative_path(run_dir, relative.as_posix(), label)
    if sha256(path) != digest:
        raise MissionFinalizationError(f"{label} SHA-256 binding mismatch")
    return path


def object_set_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        key = row.get("key")
        size = row.get("size")
        digest = row.get("sha256")
        if not isinstance(key, str) or not key or key in seen:
            raise MissionFinalizationError(
                f"RFQ object-set row {index} has an invalid/duplicate key"
            )
        seen.add(key)
        require_positive_int(size, f"RFQ object-set row {index}.size")
        require_hex(digest, f"RFQ object-set row {index}.sha256")
        canonical.append(f"{key}\t{size}\t{digest}")
    return hashlib.sha256("\n".join(sorted(canonical)).encode("utf-8")).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def normalize_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().upper().replace("-", "_").replace(" ", "_")


def reason_from_record(record: Mapping[str, Any]) -> str:
    for key in ("status_reason", "reason", "conclusion_reason", "result_reason"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "No stage reason was supplied."


def _json_pointer(parent: str, key: Any) -> str:
    encoded = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{encoded}"


def extract_conclusions(
    document: Mapping[str, Any],
    *,
    source_path: str,
    allowed_ids: set[str],
) -> list[Conclusion]:
    """Extract explicitly hypothesis-bound terminal records from a stage JSON.

    Supported schemas are intentionally narrow but adaptable: a record can name
    ``hypothesis_id``, ``study_id``, ``study_ids``, or be stored beneath a key
    equal to the hypothesis id.  A free-floating ``status`` is never accepted.
    """

    found: list[Conclusion] = []
    seen_nodes: set[tuple[str, str]] = set()

    def add(ids: Iterable[str], record: Mapping[str, Any], pointer: str) -> None:
        raw_status = record.get("hypothesis_status", record.get("status"))
        status = normalize_status(raw_status)
        if status is None:
            return
        for hypothesis_id in ids:
            if hypothesis_id not in allowed_ids:
                continue
            node_key = (hypothesis_id, pointer)
            if node_key in seen_nodes:
                continue
            seen_nodes.add(node_key)
            if status in BANNED_TERMINAL_STATUSES:
                raise MissionFinalizationError(
                    f"forbidden Mode-1 status {status} for {hypothesis_id} at "
                    f"{source_path}{pointer}"
                )
            if status not in TERMINAL_MODE1_STATUSES:
                continue
            found.append(
                Conclusion(
                    hypothesis_id=hypothesis_id,
                    status=status,
                    reason=reason_from_record(record),
                    source_path=source_path,
                    source_pointer=pointer,
                )
            )

    def walk(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            explicit: list[str] = []
            for key in ("hypothesis_id", "study_id", "trial_id"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    explicit.append(candidate)
            candidates = value.get("study_ids")
            if isinstance(candidates, list):
                explicit.extend(item for item in candidates if isinstance(item, str))
            if explicit:
                add(explicit, value, pointer)
            for key, child in value.items():
                child_pointer = _json_pointer(pointer, key)
                if key in allowed_ids and isinstance(child, dict):
                    add([key], child, child_pointer)
                walk(child, child_pointer)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, _json_pointer(pointer, index))

    walk(document, "")
    return found


def resolve_unique_conclusions(
    records: Iterable[Conclusion], required_ids: set[str], stage_name: str
) -> dict[str, Conclusion]:
    grouped: dict[str, list[Conclusion]] = {key: [] for key in required_ids}
    for record in records:
        if record.hypothesis_id in grouped:
            grouped[record.hypothesis_id].append(record)
    result: dict[str, Conclusion] = {}
    for hypothesis_id in sorted(required_ids):
        candidates = grouped[hypothesis_id]
        if not candidates:
            raise MissionFinalizationError(
                f"{stage_name} has no terminal result for {hypothesis_id}"
            )
        statuses = {item.status for item in candidates}
        if len(statuses) != 1:
            details = ", ".join(
                f"{item.status}@{item.source_path}{item.source_pointer}"
                for item in candidates
            )
            raise MissionFinalizationError(
                f"conflicting terminal results for {hypothesis_id}: {details}"
            )
        # Prefer the most deeply nested record because it normally contains the
        # stage-specific reason rather than a ledger mirror.
        result[hypothesis_id] = max(
            candidates,
            key=lambda item: (item.source_pointer.count("/"), len(item.reason)),
        )
    return result


def validate_run_identity(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_json(run_dir / "RUN_MANIFEST.json")
    ledger = load_json(run_dir / "HYPOTHESIS_LEDGER.json")
    if manifest.get("run_id") != run_dir.name:
        raise MissionFinalizationError("RUN_MANIFEST run_id does not match directory name")
    if manifest.get("mode") != EXPECTED_MODE:
        raise MissionFinalizationError("finalizer accepts MODE 1 only")
    if manifest.get("explicit_degraded_admission") is not True:
        raise MissionFinalizationError("SEALED_DEGRADED_EVIDENCE was not explicitly admitted")
    mission = manifest.get("mission")
    if not isinstance(mission, dict) or mission.get("sha256") != EXPECTED_MISSION_SHA:
        raise MissionFinalizationError("operator-pinned mission SHA-256 mismatch")
    banners = manifest.get("banners")
    if not isinstance(banners, dict):
        raise MissionFinalizationError("manifest banners are missing")
    expected_banners = {
        "data_evidence": EXPECTED_EVIDENCE,
        "experiment_split": EXPECTED_SPLIT,
        "timestamp_discipline": EXPECTED_TIMESTAMP,
        "authorization": NO_LIVE_BANNER,
    }
    for key, expected in expected_banners.items():
        if banners.get(key) != expected:
            raise MissionFinalizationError(f"manifest banner mismatch: {key}")
    gates = manifest.get("gates")
    if not isinstance(gates, dict):
        raise MissionFinalizationError("execution gate receipts are missing")
    for gate in ("gate_a", "gate_b"):
        status = (gates.get(gate) or {}).get("status")
        if not isinstance(status, str) or not status.startswith("PASS_"):
            raise MissionFinalizationError(f"{gate} is not PASS")
    gate_c = (gates.get("gate_c") or {}).get("status")
    if not isinstance(gate_c, str) or not gate_c.startswith("PASS_MODE1"):
        raise MissionFinalizationError("Gate C does not bind MODE 1")
    if (gates.get("gate_c") or {}).get("mode2_authorized") is not False:
        raise MissionFinalizationError("Mode 2 must be explicitly unauthorized")
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    attestation = load_json(attestation_path)
    attestation_sha = (gates.get("gate_b") or {}).get("attestation_sha256")
    if not isinstance(attestation_sha, str) or sha256(attestation_path) != attestation_sha:
        raise MissionFinalizationError("Gate B does not bind the current W09 attestation bytes")
    expected_attestation = {
        "instance_id": "i-0e53d134dceffe166",
        "region": "us-east-2",
        "role": "w09-research-runner",
        "instance_profile": "w09-research-runner",
        "architecture": "aarch64",
        "duckdb": "1.4.5",
        "w09_run_inhibitor_present": True,
        "w09_run_inhibitor_is_ancestor": True,
        "static_credentials_present": False,
        "trading_credentials_present": False,
        "ambient_aws_or_kalshi_variables": [],
        "static_credential_paths_present": [],
        "installation_sha256": EXPECTED_W09_INSTALLATION_SHA256,
        "s3_access": "READ_ONLY_RESEARCH_PREFIX",
    }
    for key, expected in expected_attestation.items():
        if attestation.get(key) != expected:
            raise MissionFinalizationError(f"W09 attestation mismatch: {key}")
    releases = manifest.get("selected_releases")
    if not isinstance(releases, list) or len(releases) != 2:
        raise MissionFinalizationError("exactly two immutable selected release bindings are required")
    if {item.get("release_id") for item in releases if isinstance(item, dict)} != set(
        EXPECTED_RELEASE_BINDINGS
    ):
        raise MissionFinalizationError("selected release IDs are not the exact approved set")
    for release in releases:
        if not isinstance(release, dict):
            raise MissionFinalizationError("invalid selected release entry")
        required = (
            "date",
            "release_id",
            "manifest_sha256",
            "object_version_set_sha256",
            "version_bindings_sha256",
            "seal_sha256",
            "publication_state_sha256",
        )
        if any(not release.get(key) for key in required):
            raise MissionFinalizationError(
                f"selected release binding incomplete: {release.get('release_id')}"
            )
        if release.get("evidence_tier") != EXPECTED_EVIDENCE:
            raise MissionFinalizationError("selected release evidence tier mismatch")
        if release.get("tl1_status") != EXPECTED_TIMESTAMP:
            raise MissionFinalizationError("selected release timestamp tier mismatch")
        if release.get("include") != EXPECTED_SPLIT:
            raise MissionFinalizationError("selected release split/admission mismatch")
        expected_release = EXPECTED_RELEASE_BINDINGS[release["release_id"]]
        for key, expected in expected_release.items():
            if release.get(key) != expected:
                raise MissionFinalizationError(
                    f"selected release exact binding mismatch: {release['release_id']}:{key}"
                )
    publication = manifest.get("publication")
    if not isinstance(publication, dict):
        raise MissionFinalizationError("publication authority receipt is missing")
    if publication.get("s3_report_archive_status") != "DEFERRED_AUTHORITY_CONFLICT":
        raise MissionFinalizationError("S3 report write must remain unavailable/unattempted")
    cards = ledger.get("hypotheses")
    if not isinstance(cards, list):
        raise MissionFinalizationError("hypothesis ledger cards are missing")
    ids = [card.get("hypothesis_id") for card in cards if isinstance(card, dict)]
    if len(ids) != len(HYPOTHESIS_IDS) or set(ids) != set(HYPOTHESIS_IDS):
        raise MissionFinalizationError(
            "hypothesis ledger must contain exactly the ten frozen Cycle-1 cards"
        )
    if len(ids) != len(set(ids)):
        raise MissionFinalizationError("duplicate hypothesis id in ledger")
    for card in cards:
        if card.get("split") != EXPECTED_SPLIT:
            raise MissionFinalizationError(f"split mismatch: {card.get('hypothesis_id')}")
        if card.get("data_evidence") != EXPECTED_EVIDENCE:
            raise MissionFinalizationError(
                f"evidence mismatch: {card.get('hypothesis_id')}"
            )
    return manifest, ledger


def validate_frozen_query_source(run_dir: Path, manifest: Mapping[str, Any]) -> None:
    """Bind this executing source to the pre-results frozen query receipt."""

    sums_path = run_dir / "QUERY_SHA256SUMS.txt"
    frozen_path = run_dir / "queries/finalize_mission.py"
    if not sums_path.is_file() or not frozen_path.is_file():
        raise MissionFinalizationError("frozen finalizer query receipt/copy is missing")
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise MissionFinalizationError("manifest repository identity is missing")
    if repository.get("query_set_sha256") != sha256(sums_path):
        raise MissionFinalizationError("QUERY_SHA256SUMS is not bound to RUN_MANIFEST")
    expected: str | None = None
    seen_paths: set[str] = set()
    for line_number, line in enumerate(
        sums_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise MissionFinalizationError(
                f"invalid frozen query checksum line {line_number}"
            )
        digest, relative = parts
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not relative.startswith("queries/"):
            raise MissionFinalizationError(f"unsafe frozen query path: {relative}")
        if relative in seen_paths:
            raise MissionFinalizationError(f"duplicate frozen query path: {relative}")
        seen_paths.add(relative)
        target = run_dir / candidate
        if not target.is_file() or sha256(target) != digest:
            raise MissionFinalizationError(f"frozen query hash mismatch: {relative}")
        if relative == "queries/finalize_mission.py":
            expected = digest
    if expected is None:
        raise MissionFinalizationError("finalize_mission.py is absent from frozen query set")
    if sha256(frozen_path) != expected or sha256(Path(__file__).resolve()) != expected:
        raise MissionFinalizationError(
            "executing finalizer source does not equal the pre-results frozen query copy"
        )


def validate_stage_header(
    document: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    stage_name: str,
    schema_prefix: str,
) -> None:
    run_id = document.get("run_id")
    if run_id is not None and run_id != manifest.get("run_id"):
        raise MissionFinalizationError(f"{stage_name} run_id mismatch")
    mode = document.get("mode")
    if mode is not None and mode != EXPECTED_MODE:
        raise MissionFinalizationError(f"{stage_name} is not MODE 1")
    evidence = document.get("data_evidence", document.get("evidence"))
    banner = document.get("banner")
    if evidence is None and isinstance(banner, dict):
        evidence = banner.get("data_evidence")
    if evidence != EXPECTED_EVIDENCE:
        raise MissionFinalizationError(f"{stage_name} evidence tier mismatch")
    schema = document.get("schema_version", document.get("schema"))
    if not isinstance(schema, str) or not schema.startswith(schema_prefix):
        raise MissionFinalizationError(
            f"{stage_name} schema is not supported: {schema!r}"
        )


def _load_exact_version_row(
    run_dir: Path,
    release: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    relative = release.get("exact_version_list_path")
    path = checked_relative_path(run_dir, relative, "selected release version list")
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line:
                raise MissionFinalizationError(
                    f"blank selected release version row: {relative}:{line_number}"
                )
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                raise MissionFinalizationError(
                    f"invalid selected release version row: {relative}:{line_number}"
                )
            if row["key"] in seen:
                raise MissionFinalizationError(
                    f"duplicate selected release version key: {row['key']}"
                )
            seen.add(row["key"])
            if row["key"] == key:
                matches.append(row)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(
            f"invalid selected release version list {relative}: {exc}"
        ) from exc
    if len(matches) != 1:
        raise MissionFinalizationError(
            "quarantined RFQ key does not have one exact VersionId binding"
        )
    return matches[0]


def _read_csv_rows(path: Path, required: set[str], label: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise MissionFinalizationError(f"{label} table is missing")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not required <= set(reader.fieldnames):
                raise MissionFinalizationError(f"{label} columns are incomplete")
            return list(reader)
    except OSError as exc:
        raise MissionFinalizationError(f"cannot read {label}: {exc}") from exc


def _require_utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MissionFinalizationError(f"{label} UTC timestamp is missing")
    match = re.fullmatch(
        r"(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
        r"(?P<fraction>\.[0-9]{1,9})?"
        r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    )
    if match is None:
        raise MissionFinalizationError(f"{label} UTC timestamp is invalid")
    if match.group("zone") not in {"Z", "+00:00"}:
        raise MissionFinalizationError(f"{label} timestamp is not UTC")

    # ``datetime`` only retains microseconds.  Receipts remain byte-for-byte
    # immutable; padding/truncation is solely for calendar/time validation here.
    fraction = match.group("fraction") or ""
    normalized_fraction = (
        "." + fraction[1:][:6].ljust(6, "0") if fraction else ""
    )
    try:
        parsed = dt.datetime.fromisoformat(
            match.group("base") + normalized_fraction + "+00:00"
        )
    except ValueError as exc:
        raise MissionFinalizationError(f"{label} UTC timestamp is invalid") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise MissionFinalizationError(f"{label} timestamp is not UTC")
    return value


def _utc_timestamp_key(value: Any, label: str) -> tuple[int, ...]:
    """Return a nanosecond-preserving comparison key for a validated UTC time."""
    timestamp = _require_utc_timestamp(value, label)
    without_zone = timestamp[:-1] if timestamp.endswith("Z") else timestamp[:-6]
    base, separator, fraction = without_zone.partition(".")
    if not separator:
        fraction = ""
    parsed = dt.datetime.fromisoformat(base + "+00:00")
    nanosecond = int(fraction.ljust(9, "0")) if fraction else 0
    return (
        parsed.year,
        parsed.month,
        parsed.day,
        parsed.hour,
        parsed.minute,
        parsed.second,
        nanosecond,
    )


def _require_active_archive_equal(
    run_dir: Path,
    active: Path,
    archived: Path,
    expected_sha: Any,
    label: str,
) -> tuple[dict[str, Any], str]:
    digest = require_hex(expected_sha, f"{label} SHA-256")
    active_path = require_path_hash(run_dir, active, digest, f"active {label}")
    archived_path = require_path_hash(
        run_dir, archived, digest, f"archived {label}"
    )
    if active_path.read_bytes() != archived_path.read_bytes():
        raise MissionFinalizationError(f"active/archived {label} bytes differ")
    return load_json(active_path), digest


def _validate_repair_record_receipt(
    run_dir: Path,
    repair: Mapping[str, Any],
    expected_path: Path,
    label: str,
) -> str:
    if repair.get("repair_receipt_path") != expected_path.as_posix():
        raise MissionFinalizationError(f"{label} receipt path mismatch")
    receipt_sha = require_hex(
        repair.get("repair_receipt_sha256"), f"{label} receipt SHA-256"
    )
    receipt_path = require_path_hash(
        run_dir, expected_path, receipt_sha, f"{label} receipt"
    )
    receipt = load_json(receipt_path)
    self_fields = {"repair_receipt_path", "repair_receipt_sha256"}
    if set(repair) != set(receipt) | self_fields or any(
        repair.get(field) != value for field, value in receipt.items()
    ):
        raise MissionFinalizationError(f"{label} receipt/manifest record mismatch")
    return receipt_sha


def _validate_archive_inventory(
    run_dir: Path,
    repair: Mapping[str, Any],
    expected_root: Path,
    required: set[str],
    label: str,
) -> dict[str, dict[str, Any]]:
    if repair.get("archive_path") != expected_root.as_posix():
        raise MissionFinalizationError(f"{label} archive path mismatch")
    rows = repair.get("archive_inventory")
    if not isinstance(rows, list) or not rows:
        raise MissionFinalizationError(f"{label} archive inventory is missing")
    declared: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise MissionFinalizationError(
                f"{label} archive inventory row is invalid: {index}"
            )
        value = row.get("path")
        relative = Path(value) if isinstance(value, str) else Path("/")
        if (
            not isinstance(value, str)
            or relative.is_absolute()
            or ".." in relative.parts
            or value in declared
        ):
            raise MissionFinalizationError(f"{label} archive path is unsafe: {value}")
        require_hex(row.get("sha256"), f"{label} archive {value} SHA-256")
        require_nonnegative_int(row.get("bytes"), f"{label} archive {value} bytes")
        declared[value] = row
    root = run_dir / expected_root
    if not root.is_dir() or root.is_symlink():
        raise MissionFinalizationError(f"{label} archive directory is missing/unsafe")
    actual: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MissionFinalizationError(f"{label} archive contains a symlink")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            actual[relative] = {
                "path": relative,
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
    if declared != actual:
        raise MissionFinalizationError(
            f"{label} archive inventory does not exactly match archived files"
        )
    if not required <= set(actual):
        raise MissionFinalizationError(f"{label} archive lacks a required receipt")
    return actual


def _validate_failed_attempt_files(
    run_dir: Path,
    run_id: str,
    *,
    state_path: Path,
    state_sha: Any,
    resource_path: Path,
    resource_sha: Any,
    scratch_path: Path,
    scratch_sha: Any,
    expected_label: str,
    expected_key: str,
    expected_fingerprint: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = load_json(require_path_hash(run_dir, state_path, state_sha, f"{label} state"))
    resource = load_json(
        require_path_hash(run_dir, resource_path, resource_sha, f"{label} resource")
    )
    scratch = load_json(
        require_path_hash(run_dir, scratch_path, scratch_sha, f"{label} scratch receipt")
    )
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("status") != "FAILED_RESUMABLE"
        or state.get("resume") is not False
        or state.get("input_fingerprint") != expected_fingerprint
        or not isinstance(state.get("scratch"), str)
        or not state["scratch"]
        or expected_key not in str(state.get("error", ""))
        or not isinstance(state.get("error_type"), str)
        or not state["error_type"]
        or resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != expected_label
        or resource.get("return_code") != 1
        or not isinstance(resource.get("command"), list)
        or "--resume" in resource.get("command", [])
    ):
        raise MissionFinalizationError(f"{label} is not a structural non-resume failure")
    if (
        scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or scratch.get("run_id") != run_id
        or scratch.get("original_scratch_path") != state.get("scratch")
        or not isinstance(scratch.get("preserved_scratch_path"), str)
        or not scratch["preserved_scratch_path"]
        or scratch["preserved_scratch_path"] == scratch["original_scratch_path"]
        or scratch.get("input_fingerprint") != expected_fingerprint
        or scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch.get("resume_allowed") is not False
    ):
        raise MissionFinalizationError(f"{label} scratch preservation mismatch")
    require_hex(scratch.get("sha256"), f"{label} scratch SHA-256")
    require_positive_int(scratch.get("bytes"), f"{label} scratch bytes")
    _require_utc_timestamp(scratch.get("mtime_utc"), f"{label} scratch mtime")
    return state, resource, scratch


def _validate_malformed_object_receipt(
    run_dir: Path,
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    declared: Mapping[str, Any],
    expected_receipt_path: Path,
    label: str,
) -> dict[str, Any]:
    invalid_count = require_positive_int(
        receipt.get("invalid_line_count"), f"{label} invalid_line_count"
    )
    invalid_lines = receipt.get("invalid_lines")
    if (
        receipt.get("schema_version") != "rfq-malformed-object-receipt-v1"
        or receipt.get("run_id") != manifest.get("run_id")
        or receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED"
        or receipt.get("raw_payload_redacted") is not True
        or not isinstance(invalid_lines, list)
        or len(invalid_lines) != invalid_count
        or require_positive_int(receipt.get("total_lines"), f"{label} total_lines")
        < invalid_count
        or receipt.get("expected_size") != receipt.get("observed_size")
        or receipt.get("expected_sha256") != receipt.get("observed_sha256")
    ):
        raise MissionFinalizationError(f"{label} malformed receipt is not exact")
    for index, line in enumerate(invalid_lines):
        if not isinstance(line, dict):
            raise MissionFinalizationError(f"{label} malformed-line row is invalid")
        require_positive_int(
            line.get("line_number"), f"{label} invalid_lines[{index}].line_number"
        )
        require_positive_int(
            line.get("line_bytes"), f"{label} invalid_lines[{index}].line_bytes"
        )
        require_hex(
            line.get("line_sha256"), f"{label} invalid_lines[{index}].line_sha256"
        )
        if not isinstance(line.get("error_type"), str) or not line["error_type"]:
            raise MissionFinalizationError(f"{label} malformed-line error is missing")
    release_id = declared.get("release_id")
    key = declared.get("key")
    version_id = declared.get("version_id")
    size = require_positive_int(declared.get("size"), f"{label} object size")
    object_sha = require_hex(declared.get("sha256"), f"{label} object SHA-256")
    manifest_sha = require_hex(
        declared.get("manifest_sha256"), f"{label} manifest SHA-256"
    )
    if (
        not isinstance(key, str)
        or not key.startswith("raw_rfq/")
        or not isinstance(version_id, str)
        or not version_id
        or declared.get("receipt") != expected_receipt_path.as_posix()
        or declared.get("invalid_line_count") != invalid_count
        or not isinstance(declared.get("reason"), str)
        or not declared["reason"]
        or receipt.get("release_id") != release_id
        or receipt.get("key") != key
        or receipt.get("expected_size") != size
        or receipt.get("expected_sha256") != object_sha
    ):
        raise MissionFinalizationError(f"{label} declaration/receipt identity mismatch")
    selected = [
        row
        for row in manifest.get("selected_releases", [])
        if isinstance(row, dict) and row.get("release_id") == release_id
    ]
    if len(selected) != 1 or selected[0].get("manifest_sha256") != manifest_sha:
        raise MissionFinalizationError(f"{label} selected manifest binding mismatch")
    if _load_exact_version_row(run_dir, selected[0], key) != {
        "key": key,
        "sha256": object_sha,
        "size": size,
        "version_id": version_id,
    }:
        raise MissionFinalizationError(f"{label} exact VersionId binding mismatch")
    return {
        "release_id": release_id,
        "key": key,
        "size": size,
        "sha256": object_sha,
        "version_id": version_id,
        "manifest_sha256": manifest_sha,
        "invalid_line_count": invalid_count,
        "reason": declared["reason"],
    }


def _validate_cycle1_binding_for_repairs(
    run_dir: Path,
    manifest: Mapping[str, Any],
    repairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    active_path = run_dir / ACTIVE_CYCLE1_DUCKDB_BINDING
    archived_path = run_dir / ARCHIVED_CYCLE1_DUCKDB_BINDING
    if not active_path.is_file() or not archived_path.is_file():
        raise MissionFinalizationError("Cycle-1 DuckDB binding receipt is missing")
    if active_path.read_bytes() != archived_path.read_bytes():
        raise MissionFinalizationError("active/archived Cycle-1 DuckDB receipt bytes differ")
    receipt = load_json(active_path)
    receipt_sha = sha256(active_path)
    if sha256(archived_path) != receipt_sha:
        raise MissionFinalizationError("active/archived Cycle-1 DuckDB receipt hash differs")
    db_sha = require_hex(receipt.get("sha256"), "Cycle-1 DuckDB SHA-256")
    db_bytes = require_positive_int(receipt.get("bytes"), "Cycle-1 DuckDB bytes")
    if (
        receipt.get("schema_version") != "cycle1-derived-duckdb-binding-v1"
        or receipt.get("run_id") != manifest.get("run_id")
        or receipt.get("duckdb_version") != "1.4.5"
        or receipt.get("created_before_rfq_repair_registration") is not True
        or receipt.get("core_result_disposition")
        != "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        or receipt.get("core_summary_path") != CORE_SUMMARY.as_posix()
        or receipt.get("core_summary_sha256") != sha256(run_dir / CORE_SUMMARY)
        or receipt.get("core_stage_resource_path") != "logs/resources/cycle1_core.json"
    ):
        raise MissionFinalizationError("Cycle-1 DuckDB binding receipt mismatch")
    resource = checked_relative_path(
        run_dir, receipt["core_stage_resource_path"], "Cycle-1 core resource"
    )
    resource_sha = require_hex(
        receipt.get("core_stage_resource_sha256"), "Cycle-1 core resource SHA-256"
    )
    if sha256(resource) != resource_sha:
        raise MissionFinalizationError("Cycle-1 core resource hash mismatch")
    _require_utc_timestamp(receipt.get("mtime_utc"), "Cycle-1 DuckDB mtime")
    database = run_dir / "cache/cycle1.duckdb"
    if manifest.get("status") != "COMPLETE" and (
        not database.is_file()
        or database.stat().st_size != db_bytes
        or sha256(database) != db_sha
    ):
        raise MissionFinalizationError("Cycle-1 DuckDB bytes do not match binding")
    binding = {
        "active_path": ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        "active_sha256": receipt_sha,
        "archived_path": ARCHIVED_CYCLE1_DUCKDB_BINDING.as_posix(),
        "archived_sha256": receipt_sha,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": manifest.get("run_id"),
        "duckdb_path": receipt.get("path"),
        "duckdb_sha256": db_sha,
        "duckdb_bytes": db_bytes,
        "core_summary_path": CORE_SUMMARY.as_posix(),
        "core_summary_sha256": receipt["core_summary_sha256"],
        "core_stage_resource_path": receipt["core_stage_resource_path"],
        "core_stage_resource_sha256": resource_sha,
    }
    if any(repair.get("cycle1_duckdb_binding") != binding for repair in repairs):
        raise MissionFinalizationError("repair chain changed Cycle-1 DuckDB binding")
    return binding


def _validate_release_receipt_rows(
    observed: Any,
    expected: Sequence[Mapping[str, Any]],
    label: str,
) -> None:
    if not isinstance(observed, list) or len(observed) != len(expected):
        raise MissionFinalizationError(f"{label} release receipts are missing")
    for wanted, actual in zip(expected, observed):
        if not isinstance(actual, dict) or any(
            actual.get(field) != value for field, value in wanted.items()
        ):
            raise MissionFinalizationError(
                f"{label} release receipt mismatch: {wanted.get('release_id')}"
            )
        require_hex(actual.get("verified_marker_sha256"), f"{label} marker SHA")


def _validate_failed_full_identity_v1(
    identity: Mapping[str, Any],
    manifest: Mapping[str, Any],
    union: Sequence[Mapping[str, Any]],
    logical_bindings: int,
    release_receipts: Sequence[Mapping[str, Any]],
    full_set_sha: str,
) -> None:
    overlap_count = sum(1 for row in union if len(row["bound_release_ids"]) > 1)
    if (
        identity.get("schema") != "rfq-full-input-identity-v1"
        or identity.get("release_ids")
        != [row["release_id"] for row in manifest["selected_releases"]]
        or identity.get("objects") != list(union)
        or identity.get("logical_manifest_bindings") != logical_bindings
        or identity.get("unique_objects") != len(union)
        or identity.get("deduplicated_overlapping_objects") != overlap_count
        or identity.get("path_size_sha_fingerprint") != full_set_sha
    ):
        raise MissionFinalizationError(
            "attempt-01 failed input identity is not the immutable RFQ union"
        )
    _validate_release_receipt_rows(
        identity.get("releases"), release_receipts, "attempt-01 input identity"
    )


def _validate_rfq_quarantine_boundary_counts(
    coverage: Mapping[str, Any], counts: Mapping[str, Any]
) -> None:
    """Separate the exact quarantine boundaries from all transport/loss boundaries."""
    total = counts.get("observation_boundary_timestamps")
    if (
        coverage.get("quarantine_gap_count") != 1
        or coverage.get("quarantined_object_count") != 2
        or coverage.get("quarantine_observation_boundary_count") != 2
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 2
    ):
        raise MissionFinalizationError(
            "repair-02 requires two quarantine boundaries for one contiguous gap"
        )


def _validate_repair02_success_envelope(
    *,
    rfq_input: Mapping[str, Any],
    active_identity: Mapping[str, Any],
    active_state: Mapping[str, Any],
    summary_expected: Mapping[str, Any],
    identity_expected: Mapping[str, Any],
    run_id: str,
    selection_sha: str,
    repair_chain: Sequence[Mapping[str, Any]],
    failed_bindings: Sequence[Mapping[str, Any]],
    gap_plan: Mapping[str, Any],
    expected_resource: Mapping[str, Any],
) -> None:
    """Validate the three independently emitted repair-02 success identities."""
    for field, value in summary_expected.items():
        if rfq_input.get(field) != value:
            raise MissionFinalizationError(
                f"repair-02 RFQ summary input mismatch: {field}"
            )
    if dict(active_identity) != dict(identity_expected):
        raise MissionFinalizationError(
            "active repair-02 RFQ input identity does not mirror the exact retained set"
        )
    if (
        active_state.get("schema") != "rfq-full-stage-state-v1"
        or active_state.get("run_id") != run_id
        or active_state.get("status")
        != "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        or active_state.get("phase") != "COMPLETE"
        or active_state.get("resume") is not False
        or active_state.get("input_fingerprint") != selection_sha
        or active_state.get("summary") != RFQ_SUMMARY.as_posix()
        or active_state.get("repair_chain") != list(repair_chain)
        or active_state.get("failed_attempt_bindings") != list(failed_bindings)
        or active_state.get("quarantine_gap_plan") != gap_plan
        or active_state.get("expected_success_resource") != expected_resource
    ):
        raise MissionFinalizationError("repair-02 completion state binding mismatch")
    _require_utc_timestamp(
        active_state.get("completed_at_utc"), "repair-02 completion state"
    )


def _rebuild_selected_rfq_union(
    run_dir: Path,
    selected_releases: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Rebuild the authoritative RFQ union from the two immutable manifests."""

    by_key: dict[str, dict[str, Any]] = {}
    logical_bindings = 0
    release_receipts: list[dict[str, Any]] = []
    for release in selected_releases:
        release_id = release.get("release_id")
        relative = Path("DATA_INTEGRITY/manifests") / f"{release_id}.json"
        path = checked_relative_path(run_dir, relative.as_posix(), "selected manifest")
        if sha256(path) != release.get("manifest_sha256"):
            raise MissionFinalizationError(
                f"selected immutable manifest SHA mismatch: {release_id}"
            )
        document = load_json(path)
        if (
            document.get("release_id") != release_id
            or document.get("date") != release.get("date")
            or not isinstance(document.get("objects"), list)
        ):
            raise MissionFinalizationError(
                f"selected immutable manifest identity mismatch: {release_id}"
            )
        release_seen: set[str] = set()
        release_count = 0
        release_bytes = 0
        for index, raw in enumerate(document["objects"]):
            if not isinstance(raw, dict):
                raise MissionFinalizationError(
                    f"selected manifest object is invalid: {release_id}:{index}"
                )
            key = raw.get("key")
            if not isinstance(key, str) or not key.startswith("raw_rfq/"):
                continue
            if key in release_seen:
                raise MissionFinalizationError(
                    f"duplicate RFQ key in selected manifest: {release_id}:{key}"
                )
            release_seen.add(key)
            size = require_positive_int(
                raw.get("size"), f"selected manifest RFQ size {release_id}:{key}"
            )
            digest = require_hex(
                raw.get("sha256"), f"selected manifest RFQ SHA {release_id}:{key}"
            )
            if not isinstance(raw.get("version_id"), str) or not raw["version_id"]:
                raise MissionFinalizationError(
                    f"selected manifest RFQ VersionId is missing: {release_id}:{key}"
                )
            release_count += 1
            release_bytes += size
            logical_bindings += 1
            prior = by_key.get(key)
            if prior is None:
                by_key[key] = {
                    "key": key,
                    "sha256": digest,
                    "size": size,
                    "bound_release_ids": [release_id],
                }
            else:
                if prior["sha256"] != digest or prior["size"] != size:
                    raise MissionFinalizationError(
                        f"conflicting overlapping RFQ manifest object: {key}"
                    )
                prior["bound_release_ids"].append(release_id)
        if release_count == 0:
            raise MissionFinalizationError(
                f"selected manifest contains no RFQ objects: {release_id}"
            )
        release_receipts.append(
            {
                "release_id": release_id,
                "manifest_sha256": release["manifest_sha256"],
                "rfq_objects": release_count,
                "rfq_bytes": release_bytes,
            }
        )
    return (
        [by_key[key] for key in sorted(by_key)],
        logical_bindings,
        release_receipts,
    )


def validate_rfq_partial_quarantine(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the single registered RFQ whole-object quarantine fail closed.

    This deliberately reconstructs the consumed/quarantined set identities from
    the input-identity receipt and binds the original structural failure.  A
    status label alone can never turn a partial scan into an accepted stage.
    """

    repairs = manifest.get("data_integrity_repairs")
    if (
        isinstance(repairs, list)
        and len(repairs) == 5
        and isinstance(repairs[4], dict)
        and repairs[4].get("repair_id") == "repair-05"
    ):
        return validate_rfq_consumer_wiring_repair05(run_dir, manifest, rfq)
    if (
        isinstance(repairs, list)
        and len(repairs) == 4
        and isinstance(repairs[3], dict)
        and repairs[3].get("repair_id") == "repair-04"
    ):
        return validate_rfq_resource_repair04(run_dir, manifest, rfq)
    if (
        isinstance(repairs, list)
        and len(repairs) == 3
        and isinstance(repairs[2], dict)
        and repairs[2].get("repair_id") == "repair-03"
    ):
        return validate_rfq_parser_repair03(run_dir, manifest, rfq)
    if (
        isinstance(repairs, list)
        and len(repairs) == 2
        and isinstance(repairs[1], dict)
        and repairs[1].get("repair_id") == "repair-02"
    ):
        return validate_rfq_double_quarantine(run_dir, manifest, rfq)

    if rfq.get("run_id") != manifest.get("run_id"):
        raise MissionFinalizationError("RFQ partial summary run_id mismatch")
    if rfq.get("status") != RFQ_PARTIAL_STATUS or rfq.get(
        "analysis_scope"
    ) != RFQ_ANALYSIS_SCOPE:
        raise MissionFinalizationError(
            "RFQ stage must be partial quarantined descriptive discovery"
        )
    if manifest.get("status") not in {REPAIR_PENDING_STATUS, "COMPLETE"}:
        raise MissionFinalizationError(
            "manifest is not at the registered RFQ quarantine-repair stage"
        )

    declaration_path = run_dir / RFQ_DECLARATION
    receipt_path = run_dir / RFQ_MALFORMED_RECEIPT
    declaration = load_json(declaration_path)
    receipt = load_json(receipt_path)
    declaration_sha = sha256(declaration_path)
    receipt_sha = sha256(receipt_path)
    strict_policy = (
        "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
    )
    expected_declaration = {
        "schema_version": "rfq-object-quarantine-v1",
        "run_id": manifest.get("run_id"),
        "mode": EXPECTED_MODE,
        "finding": "MALFORMED_NDJSON_OBJECT",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "remaining_object_parse_policy": strict_policy,
    }
    for field, expected in expected_declaration.items():
        if declaration.get(field) != expected:
            raise MissionFinalizationError(
                f"RFQ quarantine declaration mismatch: {field}"
            )
    for field in ("authority_basis", "selection_rule", "result_use_prohibited"):
        if not isinstance(declaration.get(field), str) or not declaration[field]:
            raise MissionFinalizationError(
                f"RFQ quarantine declaration lacks {field}"
            )
    previous_commit = require_hex(
        declaration.get("source_execution_commit"),
        "quarantine declaration source commit",
        HEX40,
    )
    declared_objects = declaration.get("quarantined_objects")
    if not isinstance(declared_objects, list) or len(declared_objects) != 1:
        raise MissionFinalizationError(
            "exactly one declared RFQ whole-object quarantine is required"
        )
    quarantined = declared_objects[0]
    if not isinstance(quarantined, dict):
        raise MissionFinalizationError("RFQ quarantined object is invalid")

    invalid_count = require_positive_int(
        receipt.get("invalid_line_count"), "malformed receipt invalid_line_count"
    )
    invalid_lines = receipt.get("invalid_lines")
    if (
        receipt.get("schema_version") != "rfq-malformed-object-receipt-v1"
        or receipt.get("run_id") != manifest.get("run_id")
        or receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED"
        or receipt.get("raw_payload_redacted") is not True
        or not isinstance(invalid_lines, list)
        or len(invalid_lines) != invalid_count
        or require_positive_int(
            receipt.get("total_lines"), "malformed receipt total_lines"
        ) < invalid_count
        or receipt.get("expected_size") != receipt.get("observed_size")
        or receipt.get("expected_sha256") != receipt.get("observed_sha256")
    ):
        raise MissionFinalizationError("RFQ malformed-object receipt is not exact")
    for index, line in enumerate(invalid_lines):
        if not isinstance(line, dict):
            raise MissionFinalizationError("RFQ malformed-line receipt is invalid")
        require_positive_int(line.get("line_number"), f"invalid_lines[{index}].line_number")
        require_positive_int(line.get("line_bytes"), f"invalid_lines[{index}].line_bytes")
        require_hex(line.get("line_sha256"), f"invalid_lines[{index}].line_sha256")
        if not isinstance(line.get("error_type"), str) or not line["error_type"]:
            raise MissionFinalizationError("RFQ malformed-line error type is missing")

    release_id = quarantined.get("release_id")
    key = quarantined.get("key")
    version_id = quarantined.get("version_id")
    size = require_positive_int(quarantined.get("size"), "quarantined object size")
    object_sha = require_hex(quarantined.get("sha256"), "quarantined object SHA-256")
    manifest_sha = require_hex(
        quarantined.get("manifest_sha256"), "quarantined manifest SHA-256"
    )
    if (
        not isinstance(key, str)
        or not key.startswith("raw_rfq/")
        or not isinstance(version_id, str)
        or not version_id
        or quarantined.get("receipt") != RFQ_MALFORMED_RECEIPT.as_posix()
        or quarantined.get("invalid_line_count") != invalid_count
        or not isinstance(quarantined.get("reason"), str)
        or not quarantined["reason"]
        or receipt.get("release_id") != release_id
        or receipt.get("key") != key
        or receipt.get("expected_size") != size
        or receipt.get("expected_sha256") != object_sha
    ):
        raise MissionFinalizationError(
            "RFQ declaration/receipt object identity mismatch"
        )
    selected = [
        row
        for row in manifest.get("selected_releases", [])
        if isinstance(row, dict) and row.get("release_id") == release_id
    ]
    if len(selected) != 1 or selected[0].get("manifest_sha256") != manifest_sha:
        raise MissionFinalizationError(
            "quarantined object is not bound to one selected release manifest"
        )
    exact_version = _load_exact_version_row(run_dir, selected[0], key)
    if exact_version != {
        "key": key,
        "sha256": object_sha,
        "size": size,
        "version_id": version_id,
    }:
        raise MissionFinalizationError(
            "quarantined object exact VersionId/size/SHA binding mismatch"
        )
    authoritative_union, authoritative_logical_bindings, release_receipts = (
        _rebuild_selected_rfq_union(run_dir, manifest["selected_releases"])
    )
    authoritative_manifest_set_sha = object_set_sha256(authoritative_union)
    authoritative_quarantine = [
        row for row in authoritative_union if row["key"] == key
    ]
    if authoritative_quarantine != [
        {
            "key": key,
            "sha256": object_sha,
            "size": size,
            "bound_release_ids": [release_id],
        }
    ]:
        raise MissionFinalizationError(
            "quarantined object does not exactly match the immutable RFQ manifest union"
        )

    repairs = manifest.get("data_integrity_repairs")
    if not isinstance(repairs, list) or len(repairs) != 1 or not isinstance(
        repairs[0], dict
    ):
        raise MissionFinalizationError("exactly one data-integrity repair is required")
    repair = repairs[0]
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise MissionFinalizationError("repair repository identity is missing")
    current_commit = require_hex(
        repository.get("execution_commit"), "current execution commit", HEX40
    )
    previous_source = require_hex(
        repair.get("previous_source_manifest_sha256"),
        "previous source manifest SHA-256",
    )
    current_source = require_hex(
        repository.get("source_manifest_sha256"), "current source manifest SHA-256"
    )
    previous_source_sums = require_hex(
        repair.get("previous_source_sha256s_sha256"),
        "previous source checksum-set SHA-256",
    )
    current_source_sums = require_hex(
        repository.get("source_sha256s_sha256"),
        "current source checksum-set SHA-256",
    )
    previous_query = require_hex(
        repair.get("previous_query_set_sha256"), "previous query set SHA-256"
    )
    current_query = require_hex(
        repository.get("query_set_sha256"), "current query set SHA-256"
    )
    repair_exact = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "post_repair_status": REPAIR_PENDING_STATUS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "declaration_input_path": RFQ_DECLARATION.as_posix(),
        "receipt_input_path": RFQ_MALFORMED_RECEIPT.as_posix(),
        "declaration_path": REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": declaration_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt_sha,
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "current_source_manifest_sha256": current_source,
        "current_source_sha256s_sha256": current_source_sums,
        "current_query_set_sha256": current_query,
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
        "failed_state_path": FAILED_RFQ_STATE.as_posix(),
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY.as_posix(),
    }
    for field, expected in repair_exact.items():
        if repair.get(field) != expected:
            raise MissionFinalizationError(
                f"RFQ repair registration mismatch: {field}"
            )
    if repair.get("quarantined_objects") != declared_objects:
        raise MissionFinalizationError(
            "repair/quarantine declaration object binding mismatch"
        )
    if (
        repair.get("previous_source_manifest_sha256") != previous_source
        or repair.get("current_source_manifest_sha256") != current_source
        or repair.get("previous_query_set_sha256") != previous_query
        or repair.get("current_query_set_sha256") != current_query
        or repository.get("initial_execution_commit") != previous_commit
        or repository.get("previous_execution_commit") != previous_commit
        or repository.get("initial_source_manifest_sha256") != previous_source
        or repository.get("previous_source_manifest_sha256") != previous_source
        or repository.get("initial_source_sha256s_sha256") != previous_source_sums
        or repository.get("previous_source_sha256s_sha256") != previous_source_sums
        or repair.get("current_source_sha256s_sha256") != current_source_sums
        or repository.get("initial_query_set_sha256") != previous_query
        or repository.get("previous_query_set_sha256") != previous_query
        or repository.get("registration_repair_id") != "repair-01"
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR_BEFORE_RFQ_RESULT"
    ):
        raise MissionFinalizationError("RFQ repair repository history mismatch")
    if sha256(run_dir / "SOURCE_MANIFEST.json") != current_source or sha256(
        run_dir / "QUERY_SHA256SUMS.txt"
    ) != current_query:
        raise MissionFinalizationError("current repair source/query receipt mismatch")
    if sha256(run_dir / "SOURCE_SHA256SUMS.txt") != current_source_sums:
        raise MissionFinalizationError("current source checksum-set receipt mismatch")
    previous_source_path = Path(
        "DATA_INTEGRITY/repairs/repair-01/pre_repair/SOURCE_MANIFEST.json"
    )
    previous_query_path = Path(
        "DATA_INTEGRITY/repairs/repair-01/pre_repair/QUERY_SHA256SUMS.txt"
    )
    previous_source_sums_path = Path(
        "DATA_INTEGRITY/repairs/repair-01/pre_repair/SOURCE_SHA256SUMS.txt"
    )
    require_path_hash(
        run_dir, previous_source_path, previous_source, "pre-repair source manifest"
    )
    require_path_hash(run_dir, previous_query_path, previous_query, "pre-repair query set")
    require_path_hash(
        run_dir,
        previous_source_sums_path,
        previous_source_sums,
        "pre-repair source checksum set",
    )
    require_path_hash(
        run_dir, REPAIR_DECLARATION, declaration_sha, "archived quarantine declaration"
    )
    require_path_hash(
        run_dir, REPAIR_MALFORMED_RECEIPT, receipt_sha, "archived malformed receipt"
    )
    if load_json(run_dir / REPAIR_DECLARATION) != declaration or load_json(
        run_dir / REPAIR_MALFORMED_RECEIPT
    ) != receipt:
        raise MissionFinalizationError("active/archive RFQ integrity receipts differ")
    if repair.get("repair_receipt_path") != REPAIR_REGISTRATION_RECEIPT.as_posix():
        raise MissionFinalizationError("repair registration receipt path mismatch")
    repair_receipt_sha = require_hex(
        repair.get("repair_receipt_sha256"), "repair registration receipt SHA-256"
    )
    repair_receipt_path = require_path_hash(
        run_dir,
        REPAIR_REGISTRATION_RECEIPT,
        repair_receipt_sha,
        "repair registration receipt",
    )
    repair_receipt = load_json(repair_receipt_path)
    self_fields = {"repair_receipt_path", "repair_receipt_sha256"}
    if set(repair) != set(repair_receipt) | self_fields or any(
        repair.get(field) != value for field, value in repair_receipt.items()
    ):
        raise MissionFinalizationError(
            "repair registration receipt/manifest record mismatch"
        )
    core_artifacts = repair.get("core_result_artifacts")
    expected_core_paths = {
        "REPORT/CYCLE1_CORE_SUMMARY.json",
        "REPORT/tables/CORE_HYPOTHESIS_TESTS.json",
        "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json",
        "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json",
    }
    if not isinstance(core_artifacts, list) or len(core_artifacts) != len(
        expected_core_paths
    ):
        raise MissionFinalizationError("preserved core-result inventory is incomplete")
    seen_core_paths: set[str] = set()
    for index, row in enumerate(core_artifacts):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise MissionFinalizationError(
                f"preserved core-result row is invalid: {index}"
            )
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or relative not in expected_core_paths
            or relative in seen_core_paths
        ):
            raise MissionFinalizationError(
                f"unsafe/duplicate preserved core-result path: {relative}"
            )
        seen_core_paths.add(relative)
        path = checked_relative_path(run_dir, relative, "preserved core result")
        if (
            row.get("sha256") != sha256(path)
            or row.get("bytes") != path.stat().st_size
        ):
            raise MissionFinalizationError(
                f"preserved core-result artifact changed: {relative}"
            )
    if seen_core_paths != expected_core_paths:
        raise MissionFinalizationError("preserved core-result path set mismatch")

    failed_state_sha = require_hex(
        repair.get("failed_state_sha256"), "failed RFQ state SHA-256"
    )
    failed_resource_sha = require_hex(
        repair.get("failed_resource_receipt_sha256"),
        "failed RFQ resource receipt SHA-256",
    )
    failed_scratch_sha = require_hex(
        repair.get("failed_scratch_receipt_sha256"),
        "failed RFQ scratch receipt SHA-256",
    )
    failed_state_path = require_path_hash(
        run_dir, FAILED_RFQ_STATE, failed_state_sha, "failed RFQ state"
    )
    failed_resource_path = require_path_hash(
        run_dir,
        FAILED_RFQ_RESOURCE,
        failed_resource_sha,
        "failed RFQ resource receipt",
    )
    failed_scratch_path = require_path_hash(
        run_dir,
        FAILED_RFQ_SCRATCH_RECEIPT,
        failed_scratch_sha,
        "failed RFQ scratch receipt",
    )
    failed_state = load_json(failed_state_path)
    failed_resource = load_json(failed_resource_path)
    failed_scratch = load_json(failed_scratch_path)
    if (
        failed_state.get("schema") != "rfq-full-stage-state-v1"
        or failed_state.get("status") != "FAILED_RESUMABLE"
        or failed_state.get("resume") is not False
        or not isinstance(failed_state.get("scratch"), str)
        or failed_state.get("input_fingerprint") != authoritative_manifest_set_sha
        or not isinstance(failed_state.get("error_type"), str)
        or not failed_state["error_type"]
        or key not in str(failed_state.get("error", ""))
        or failed_resource.get("schema_version") != "w09-stage-resource-v1"
        or failed_resource.get("label") != "rfq_full_stage"
        or failed_resource.get("return_code") != 1
        or not isinstance(failed_resource.get("command"), list)
        or "--resume" in failed_resource.get("command", [])
    ):
        raise MissionFinalizationError("failed RFQ attempt receipt is not structural/non-resume")
    if (
        failed_scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or failed_scratch.get("run_id") != manifest.get("run_id")
        or not isinstance(failed_scratch.get("original_scratch_path"), str)
        or not isinstance(failed_scratch.get("preserved_scratch_path"), str)
        or failed_scratch.get("original_scratch_path")
        == failed_scratch.get("preserved_scratch_path")
        or failed_scratch.get("original_scratch_path") != failed_state.get("scratch")
        or failed_scratch.get("input_fingerprint")
        != failed_state.get("input_fingerprint")
        or failed_scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or failed_scratch.get("resume_allowed") is not False
    ):
        raise MissionFinalizationError("failed RFQ scratch preservation receipt mismatch")
    require_hex(failed_scratch.get("sha256"), "failed RFQ scratch SHA-256")
    require_positive_int(failed_scratch.get("bytes"), "failed RFQ scratch bytes")
    mtime = failed_scratch.get("mtime_utc")
    try:
        parsed_mtime = dt.datetime.fromisoformat(
            mtime[:-1] + "+00:00" if isinstance(mtime, str) and mtime.endswith("Z") else mtime
        )
    except (TypeError, ValueError):
        parsed_mtime = None
    if parsed_mtime is None or parsed_mtime.utcoffset() != dt.timedelta(0):
        raise MissionFinalizationError("failed RFQ scratch UTC mtime is missing")

    failed_input_sha = require_hex(
        repair.get("failed_input_identity_sha256"),
        "failed RFQ input-identity SHA-256",
    )
    failed_input_path = require_path_hash(
        run_dir,
        FAILED_RFQ_INPUT_IDENTITY,
        failed_input_sha,
        "failed RFQ input identity",
    )
    failed_input = load_json(failed_input_path)
    authoritative_overlap_keys = [
        row["key"]
        for row in authoritative_union
        if len(row["bound_release_ids"]) > 1
    ]
    if (
        failed_input.get("schema") != "rfq-full-input-identity-v1"
        or failed_input.get("release_ids")
        != [row["release_id"] for row in manifest["selected_releases"]]
        or failed_input.get("logical_manifest_bindings")
        != authoritative_logical_bindings
        or failed_input.get("unique_objects") != len(authoritative_union)
        or failed_input.get("deduplicated_overlapping_objects")
        != len(authoritative_overlap_keys)
        or failed_input.get("path_size_sha_fingerprint")
        != authoritative_manifest_set_sha
        or failed_input.get("objects") != authoritative_union
        or failed_state.get("input_fingerprint") != authoritative_manifest_set_sha
    ):
        raise MissionFinalizationError(
            "archived failed RFQ input identity is not the immutable manifest union"
        )
    failed_releases = failed_input.get("releases")
    if not isinstance(failed_releases, list) or len(failed_releases) != len(
        release_receipts
    ):
        raise MissionFinalizationError("archived failed RFQ release receipts are missing")
    for expected, observed in zip(release_receipts, failed_releases):
        if not isinstance(observed, dict) or any(
            observed.get(field) != value for field, value in expected.items()
        ):
            raise MissionFinalizationError(
                "archived failed RFQ release-manifest receipt mismatch"
            )

    active_cycle_path = run_dir / ACTIVE_CYCLE1_DUCKDB_BINDING
    archived_cycle_path = run_dir / ARCHIVED_CYCLE1_DUCKDB_BINDING
    active_cycle = load_json(active_cycle_path)
    archived_cycle = load_json(archived_cycle_path)
    active_cycle_sha = sha256(active_cycle_path)
    archived_cycle_sha = sha256(archived_cycle_path)
    if active_cycle != archived_cycle or active_cycle_sha != archived_cycle_sha:
        raise MissionFinalizationError("active/archived Cycle-1 DuckDB receipts differ")
    cycle_db_sha = require_hex(active_cycle.get("sha256"), "Cycle-1 DuckDB SHA-256")
    cycle_db_bytes = require_positive_int(
        active_cycle.get("bytes"), "Cycle-1 DuckDB bytes"
    )
    if (
        active_cycle.get("schema_version") != "cycle1-derived-duckdb-binding-v1"
        or active_cycle.get("run_id") != manifest.get("run_id")
        or active_cycle.get("duckdb_version") != "1.4.5"
        or active_cycle.get("created_before_rfq_repair_registration") is not True
        or active_cycle.get("core_result_disposition")
        != "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        or active_cycle.get("core_summary_path") != CORE_SUMMARY.as_posix()
        or active_cycle.get("core_summary_sha256")
        != sha256(run_dir / CORE_SUMMARY)
        or active_cycle.get("core_stage_resource_path")
        != "logs/resources/cycle1_core.json"
    ):
        raise MissionFinalizationError("Cycle-1 DuckDB binding receipt mismatch")
    core_resource_path = checked_relative_path(
        run_dir,
        active_cycle["core_stage_resource_path"],
        "Cycle-1 core resource receipt",
    )
    core_resource_sha = require_hex(
        active_cycle.get("core_stage_resource_sha256"),
        "Cycle-1 core resource receipt SHA-256",
    )
    if sha256(core_resource_path) != core_resource_sha:
        raise MissionFinalizationError("Cycle-1 core resource receipt hash mismatch")
    cycle_mtime = active_cycle.get("mtime_utc")
    try:
        parsed_cycle_mtime = dt.datetime.fromisoformat(
            cycle_mtime[:-1] + "+00:00"
            if isinstance(cycle_mtime, str) and cycle_mtime.endswith("Z")
            else cycle_mtime
        )
    except (TypeError, ValueError):
        parsed_cycle_mtime = None
    if parsed_cycle_mtime is None or parsed_cycle_mtime.utcoffset() != dt.timedelta(0):
        raise MissionFinalizationError("Cycle-1 DuckDB binding UTC mtime is invalid")
    cycle_database = run_dir / "cache/cycle1.duckdb"
    if manifest.get("status") != "COMPLETE":
        if (
            not cycle_database.is_file()
            or cycle_database.stat().st_size != cycle_db_bytes
            or sha256(cycle_database) != cycle_db_sha
        ):
            raise MissionFinalizationError("Cycle-1 DuckDB bytes do not match binding")
    cycle_binding = {
        "active_path": ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        "active_sha256": active_cycle_sha,
        "archived_path": ARCHIVED_CYCLE1_DUCKDB_BINDING.as_posix(),
        "archived_sha256": archived_cycle_sha,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": manifest.get("run_id"),
        "duckdb_path": active_cycle.get("path"),
        "duckdb_sha256": cycle_db_sha,
        "duckdb_bytes": cycle_db_bytes,
        "core_summary_path": CORE_SUMMARY.as_posix(),
        "core_summary_sha256": active_cycle["core_summary_sha256"],
        "core_stage_resource_path": active_cycle["core_stage_resource_path"],
        "core_stage_resource_sha256": core_resource_sha,
    }
    if repair.get("cycle1_duckdb_binding") != cycle_binding:
        raise MissionFinalizationError("repair Cycle-1 DuckDB binding mismatch")

    trial_binding = repair.get("trial_registry")
    if not isinstance(trial_binding, dict):
        raise MissionFinalizationError("RFQ repair trial-registry binding is missing")
    previous_trial_bytes = require_positive_int(
        trial_binding.get("previous_bytes"), "pre-repair trial-registry bytes"
    )
    current_trial_bytes = require_positive_int(
        trial_binding.get("current_bytes"), "repair trial-registry bytes"
    )
    if (
        current_trial_bytes <= previous_trial_bytes
        or trial_binding.get("strict_previous_bytes_prefix") is not True
        or trial_binding.get("appended_records") != 2
        or trial_binding.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"]
    ):
        raise MissionFinalizationError("RFQ repair trial-registry append policy mismatch")
    trial_bytes = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    if len(trial_bytes) < current_trial_bytes:
        raise MissionFinalizationError("RFQ repair trial-registry prefix is truncated")
    previous_prefix = trial_bytes[:previous_trial_bytes]
    repair_prefix = trial_bytes[:current_trial_bytes]
    if (
        not previous_prefix.endswith(b"\n")
        or not repair_prefix.endswith(b"\n")
        or hashlib.sha256(previous_prefix).hexdigest()
        != trial_binding.get("previous_sha256")
        or hashlib.sha256(repair_prefix).hexdigest()
        != trial_binding.get("current_sha256")
    ):
        raise MissionFinalizationError("RFQ repair trial-registry prefix hash mismatch")
    try:
        appended = [
            json.loads(line)
            for line in repair_prefix[previous_trial_bytes:].decode("utf-8").splitlines()
            if line
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(
            f"invalid RFQ repair trial-registry records: {exc}"
        ) from exc
    if len(appended) != 2 or any(not isinstance(row, dict) for row in appended):
        raise MissionFinalizationError("RFQ repair trial-registry append count mismatch")
    failure_trial, repair_trial = appended
    common_trial = {
        "trial_ids": list(RFQ_TRIAL_ORDER),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
    }
    for field, expected in common_trial.items():
        if failure_trial.get(field) != expected or repair_trial.get(field) != expected:
            raise MissionFinalizationError(
                f"RFQ repair trial-registry common binding mismatch: {field}"
            )
    expected_failure_trial = {
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_01",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE",
        "failure_class": "MALFORMED_NDJSON_OBJECT",
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt_sha,
        "failure_state_path": FAILED_RFQ_STATE.as_posix(),
        "failure_state_sha256": failed_state_sha,
        "failure_resource_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failure_resource_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "execution_commit": previous_commit,
        "source_manifest_sha256": previous_source,
        "query_set_sha256": previous_query,
    }
    expected_repair_trial = {
        "trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR_PREREGISTRATION",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "declaration_path": REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": declaration_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt_sha,
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "previous_source_manifest_sha256": previous_source,
        "current_source_manifest_sha256": current_source,
        "previous_query_set_sha256": previous_query,
        "current_query_set_sha256": current_query,
        "threshold_feature_test_or_hypothesis_status_changed": False,
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
        "core_results_recomputed": False,
    }
    for field, expected in expected_failure_trial.items():
        if failure_trial.get(field) != expected:
            raise MissionFinalizationError(
                f"RFQ failure trial-registry binding mismatch: {field}"
            )
    for field, expected in expected_repair_trial.items():
        if repair_trial.get(field) != expected:
            raise MissionFinalizationError(
                f"RFQ repair trial-registry binding mismatch: {field}"
            )

    archive_relative = Path("DATA_INTEGRITY/repairs/repair-01/pre_repair")
    if repair.get("archive_path") != archive_relative.as_posix():
        raise MissionFinalizationError("pre-repair archive path mismatch")
    inventory = repair.get("archive_inventory")
    if not isinstance(inventory, list) or not inventory:
        raise MissionFinalizationError("pre-repair archive inventory is missing")
    inventory_by_path: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(inventory):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise MissionFinalizationError(
                f"pre-repair archive inventory row is invalid: {index}"
            )
        relative_value = row.get("path")
        if not isinstance(relative_value, str):
            raise MissionFinalizationError("pre-repair archive path is invalid")
        relative_path = Path(relative_value)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_value in inventory_by_path
        ):
            raise MissionFinalizationError(
                f"unsafe/duplicate pre-repair archive path: {relative_value}"
            )
        require_hex(row.get("sha256"), f"archive inventory {relative_value} SHA")
        require_nonnegative_int(row.get("bytes"), f"archive inventory {relative_value} bytes")
        inventory_by_path[relative_value] = row
    archive_root = run_dir / archive_relative
    if not archive_root.is_dir():
        raise MissionFinalizationError("pre-repair archive directory is missing")
    actual_archive: dict[str, dict[str, Any]] = {}
    for path in sorted(archive_root.rglob("*")):
        if path.is_symlink():
            raise MissionFinalizationError("pre-repair archive contains a symlink")
        if not path.is_file():
            continue
        relative_value = path.relative_to(archive_root).as_posix()
        actual_archive[relative_value] = {
            "path": relative_value,
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
    if inventory_by_path != actual_archive:
        raise MissionFinalizationError(
            "pre-repair archive inventory does not exactly match archived files"
        )
    required_archive = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "logs/resources/rfq_full_stage.json",
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT.json",
        "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json",
    }
    if not required_archive <= set(actual_archive):
        raise MissionFinalizationError("pre-repair archive lacks a required receipt")
    archived_manifest = load_json(archive_root / "RUN_MANIFEST.json")
    archived_repository = archived_manifest.get("repository")
    if (
        archived_manifest.get("run_id") != manifest.get("run_id")
        or archived_manifest.get("status")
        != "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING"
        or not isinstance(archived_repository, dict)
    ):
        raise MissionFinalizationError("archived pre-repair manifest identity mismatch")
    previous_repository_identity = {
        "execution_commit": previous_commit,
        "source_manifest_sha256": previous_source,
        "source_sha256s_sha256": previous_source_sums,
        "query_set_sha256": previous_query,
        "query_files": archived_repository.get("query_files"),
    }
    current_repository_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": current_source,
        "source_sha256s_sha256": current_source_sums,
        "query_set_sha256": current_query,
        "query_files": repository.get("query_files"),
    }
    if (
        repair.get("previous_repository_identity") != previous_repository_identity
        or repair.get("current_repository_identity") != current_repository_identity
        or any(
            archived_repository.get(field) != value
            for field, value in previous_repository_identity.items()
            if field != "source_sha256s_sha256"
        )
        or repository.get("initial_identity") != previous_repository_identity
        or repository.get("previous_identity") != previous_repository_identity
        or repository.get("query_files") != archived_repository.get("query_files")
    ):
        raise MissionFinalizationError("archived repository identity/history mismatch")
    query_files = archived_repository.get("query_files")
    if (
        not isinstance(query_files, list)
        or not query_files
        or any(
            not isinstance(item, str) or item not in actual_archive
            for item in query_files
        )
    ):
        raise MissionFinalizationError("archived frozen query set is incomplete")
    archived_trial = (archive_root / "TRIAL_REGISTRY.jsonl").read_bytes()
    if (
        archived_trial != previous_prefix
        or len(archived_trial) != previous_trial_bytes
        or hashlib.sha256(archived_trial).hexdigest()
        != trial_binding.get("previous_sha256")
    ):
        raise MissionFinalizationError("archived pre-repair trial registry mismatch")

    rfq_input = rfq.get("input")
    if not isinstance(rfq_input, dict):
        raise MissionFinalizationError("RFQ partial input identity is missing")
    expected_release_ids = [
        row["release_id"] for row in manifest.get("selected_releases", [])
    ]
    input_exact = {
        "release_ids": expected_release_ids,
        "coverage_status": RFQ_PARTIAL_STATUS,
        "full_object_coverage": False,
        "whole_object_quarantine": True,
        "line_salvage": False,
        "outer_parser": "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort",
    }
    for field, expected in input_exact.items():
        if rfq_input.get(field) != expected:
            raise MissionFinalizationError(f"RFQ partial input mismatch: {field}")
    failed_binding = rfq_input.get("failed_attempt_binding")
    expected_failure_binding = {
        "repair_id": "repair-01",
        "failed_state_path": FAILED_RFQ_STATE.as_posix(),
        "failed_state_sha256": failed_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY.as_posix(),
        "failed_input_identity_sha256": failed_input_sha,
    }
    if failed_binding != expected_failure_binding:
        raise MissionFinalizationError("RFQ summary failed-attempt binding mismatch")
    if rfq_input.get("cycle1_duckdb_binding") != cycle_binding:
        raise MissionFinalizationError("RFQ summary Cycle-1 DuckDB binding mismatch")

    total_objects = require_positive_int(
        rfq_input.get("unique_objects_total"), "RFQ unique_objects_total"
    )
    total_bindings = require_positive_int(
        rfq_input.get("logical_manifest_bindings_total"),
        "RFQ logical_manifest_bindings_total",
    )
    total_bytes = require_positive_int(
        rfq_input.get("unique_bytes_total"), "RFQ unique_bytes_total"
    )
    consumed_objects = require_positive_int(
        rfq_input.get("consumed_unique_objects"), "RFQ consumed_unique_objects"
    )
    consumed_bindings = require_positive_int(
        rfq_input.get("consumed_logical_bindings"),
        "RFQ consumed_logical_bindings",
    )
    consumed_bytes = require_positive_int(
        rfq_input.get("consumed_bytes"), "RFQ consumed_bytes"
    )
    quarantined_objects = require_positive_int(
        rfq_input.get("quarantined_unique_objects"),
        "RFQ quarantined_unique_objects",
    )
    quarantined_bindings = require_positive_int(
        rfq_input.get("quarantined_logical_bindings"),
        "RFQ quarantined_logical_bindings",
    )
    quarantined_bytes = require_positive_int(
        rfq_input.get("quarantined_bytes"), "RFQ quarantined_bytes"
    )
    if (
        quarantined_objects != 1
        or quarantined_bindings != 1
        or quarantined_bytes != size
        or consumed_objects + quarantined_objects != total_objects
        or consumed_bindings + quarantined_bindings != total_bindings
        or consumed_bytes + quarantined_bytes != total_bytes
        or total_bindings < total_objects
        or total_objects != len(authoritative_union)
        or total_bindings != authoritative_logical_bindings
        or total_bytes != sum(row["size"] for row in authoritative_union)
    ):
        raise MissionFinalizationError("RFQ partial object count/byte arithmetic mismatch")
    expected_detail = {
        "release_id": release_id,
        "key": key,
        "size": size,
        "sha256": object_sha,
        "version_id": version_id,
        "manifest_sha256": manifest_sha,
        "invalid_line_count": invalid_count,
        "reason": quarantined["reason"],
        "receipt_sha256": receipt_sha,
        "declaration_sha256": declaration_sha,
    }
    if (
        rfq_input.get("quarantine_details") != [expected_detail]
        or rfq_input.get("quarantine_reasons") != [quarantined["reason"]]
    ):
        raise MissionFinalizationError("RFQ quarantine summary detail mismatch")

    identity = load_json(run_dir / RFQ_INPUT_IDENTITY)
    if identity.get("schema") != "rfq-full-input-identity-v2":
        raise MissionFinalizationError("RFQ input-identity schema mismatch")
    if identity.get("run_id") != manifest.get("run_id"):
        raise MissionFinalizationError("RFQ input-identity run_id mismatch")
    mirrored_fields = (
        "release_ids",
        "coverage_status",
        "full_object_coverage",
        "whole_object_quarantine",
        "line_salvage",
        "logical_manifest_bindings_total",
        "unique_objects_total",
        "unique_bytes_total",
        "consumed_unique_objects",
        "consumed_logical_bindings",
        "consumed_bytes",
        "consumed_object_set_sha256",
        "quarantined_unique_objects",
        "quarantined_logical_bindings",
        "quarantined_bytes",
        "quarantined_object_set_sha256",
        "quarantine_reasons",
        "quarantine_details",
        "failed_attempt_binding",
        "cycle1_duckdb_binding",
        "manifest_object_set_sha256",
        "selection_fingerprint_sha256",
    )
    for field in mirrored_fields:
        if identity.get(field) != rfq_input.get(field):
            raise MissionFinalizationError(
                f"RFQ summary/input-identity mismatch: {field}"
            )
    identity_releases = identity.get("releases")
    if not isinstance(identity_releases, list) or len(identity_releases) != len(
        release_receipts
    ):
        raise MissionFinalizationError("RFQ input-identity release receipts are missing")
    for expected, observed in zip(release_receipts, identity_releases):
        if not isinstance(observed, dict) or any(
            observed.get(field) != value for field, value in expected.items()
        ):
            raise MissionFinalizationError(
                f"RFQ input-identity release manifest mismatch: {expected['release_id']}"
            )
    consumed_rows = identity.get("consumed_objects")
    if not isinstance(consumed_rows, list) or len(consumed_rows) != consumed_objects:
        raise MissionFinalizationError("RFQ consumed-object inventory count mismatch")
    logical_count = 0
    for index, row in enumerate(consumed_rows):
        if not isinstance(row, dict):
            raise MissionFinalizationError("RFQ consumed-object row is invalid")
        releases = row.get("bound_release_ids")
        if (
            not isinstance(releases, list)
            or not releases
            or any(item not in expected_release_ids for item in releases)
            or len(releases) != len(set(releases))
            or row.get("key") == key
        ):
            raise MissionFinalizationError(
                f"RFQ consumed-object release binding mismatch: row {index}"
            )
        logical_count += len(releases)
    if logical_count != consumed_bindings or sum(
        require_positive_int(row.get("size"), "RFQ consumed object size")
        for row in consumed_rows
    ) != consumed_bytes:
        raise MissionFinalizationError("RFQ consumed-object counts/bytes mismatch")
    authoritative_consumed = [
        row for row in authoritative_union if row["key"] != key
    ]
    if consumed_rows != authoritative_consumed:
        raise MissionFinalizationError(
            "RFQ consumed inventory is not the exact immutable manifest union minus quarantine"
        )
    authoritative_overlap_keys = [
        row["key"]
        for row in authoritative_union
        if len(row["bound_release_ids"]) > 1
    ]
    if (
        rfq_input.get("deduplicated_overlapping_objects")
        != len(authoritative_overlap_keys)
        or rfq_input.get("overlap_keys") != authoritative_overlap_keys
    ):
        raise MissionFinalizationError("RFQ overlapping-object union receipt mismatch")
    consumed_set_sha = object_set_sha256(consumed_rows)
    quarantined_set_sha = object_set_sha256([quarantined])
    manifest_set_sha = object_set_sha256([*consumed_rows, quarantined])
    if (
        rfq_input.get("consumed_object_set_sha256") != consumed_set_sha
        or rfq_input.get("quarantined_object_set_sha256") != quarantined_set_sha
        or rfq_input.get("manifest_object_set_sha256") != manifest_set_sha
    ):
        raise MissionFinalizationError("RFQ object-set fingerprint mismatch")
    selection_payload = {
        "total": manifest_set_sha,
        "consumed": consumed_set_sha,
        "quarantined": quarantined_set_sha,
        "receipt": receipt_sha,
        "declaration": declaration_sha,
    }
    selection_sha = canonical_json_sha256(selection_payload)
    if rfq_input.get("selection_fingerprint_sha256") != selection_sha:
        raise MissionFinalizationError("RFQ selection fingerprint mismatch")

    coverage = rfq.get("coverage")
    if not isinstance(coverage, dict):
        raise MissionFinalizationError("RFQ partial coverage QC is missing")
    gap_count = require_positive_int(
        coverage.get("quarantine_gap_count"), "RFQ quarantine_gap_count"
    )
    gap_ranges = coverage.get("quarantine_gap_ranges")
    if gap_count != 1 or not isinstance(gap_ranges, list) or len(gap_ranges) != 1:
        raise MissionFinalizationError("RFQ quarantine gap count mismatch")
    gap = gap_ranges[0]
    if not isinstance(gap, dict):
        raise MissionFinalizationError("RFQ quarantine gap receipt is invalid")
    gap_start_ns = require_positive_int(gap.get("gap_start_ns"), "RFQ gap_start_ns")
    gap_end_ns = require_positive_int(gap.get("gap_end_ns"), "RFQ gap_end_ns")
    expected_gap = {
        "release_id": release_id,
        "key": key,
        "sha256": object_sha,
        "gap_start_ns": gap_start_ns,
        "gap_end_ns": gap_end_ns,
        "gap_start_us": gap_start_ns // 1000,
        "gap_end_us": (gap_end_ns + 999) // 1000,
        "boundary_reason": "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON",
    }
    if gap != expected_gap or gap_end_ns <= gap_start_ns:
        raise MissionFinalizationError("RFQ quarantine gap boundary QC mismatch")
    gap_set_sha = canonical_json_sha256(gap_ranges)
    if (
        coverage.get("rfq_object_coverage") != RFQ_PARTIAL_STATUS
        or coverage.get("quarantine_gap_set_sha256") != gap_set_sha
        or coverage.get("quarantined_hours_are_not_observed_zero") is not True
        or coverage.get("capture_completeness_is_not_lifecycle_join_completeness")
        is not True
    ):
        raise MissionFinalizationError("RFQ quarantine coverage semantics mismatch")
    partial_hours = require_positive_int(
        coverage.get("partial_object_coverage_hours"),
        "RFQ partial_object_coverage_hours",
    )
    gap_csv = _read_csv_rows(
        run_dir / RFQ_QUARANTINE_GAPS,
        {
            "release_id",
            "key",
            "sha256",
            "gap_start_ns",
            "gap_end_ns",
            "gap_start_us",
            "gap_end_us",
            "boundary_reason",
            "previous_filename",
            "next_filename",
        },
        "RFQ quarantine gaps",
    )
    if len(gap_csv) != 1:
        raise MissionFinalizationError("RFQ quarantine gap table must contain one row")
    csv_gap = gap_csv[0]
    for field, expected in expected_gap.items():
        actual: Any = csv_gap.get(field)
        if isinstance(expected, int):
            try:
                actual = int(actual or "")
            except ValueError as exc:
                raise MissionFinalizationError(
                    f"RFQ quarantine gap table has invalid {field}"
                ) from exc
        if actual != expected:
            raise MissionFinalizationError(
                f"RFQ quarantine gap table mismatch: {field}"
            )
    if not csv_gap.get("previous_filename") or not csv_gap.get("next_filename"):
        raise MissionFinalizationError("RFQ quarantine adjacent-object QC is missing")
    hour_rows = _read_csv_rows(
        run_dir / RFQ_HOUR_COVERAGE,
        {"object_coverage_status", "zero_interpretation"},
        "RFQ hour coverage",
    )
    partial_rows = [
        row for row in hour_rows if row.get("object_coverage_status") == RFQ_PARTIAL_STATUS
    ]
    if len(partial_rows) != partial_hours or any(
        row.get("zero_interpretation")
        != "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP"
        for row in partial_rows
    ):
        raise MissionFinalizationError("RFQ partial-hour gap QC mismatch")

    active_state_path = checked_relative_path(
        run_dir, ACTIVE_RFQ_STATE.as_posix(), "completed RFQ repair state"
    )
    active_state = load_json(active_state_path)
    if (
        active_state.get("schema") != "rfq-full-stage-state-v1"
        or active_state.get("status")
        != "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        or active_state.get("phase") != "COMPLETE"
        or active_state.get("resume") is not False
        or active_state.get("input_fingerprint") != selection_sha
        or active_state.get("summary") != RFQ_SUMMARY.as_posix()
        or not isinstance(active_state.get("completed_at_utc"), str)
        or not active_state["completed_at_utc"]
    ):
        raise MissionFinalizationError("active RFQ repair completion state mismatch")
    active_state_sha = sha256(active_state_path)

    repair_resource_path = checked_relative_path(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE.as_posix(),
        "successful RFQ repair resource receipt",
    )
    repair_resource = load_json(repair_resource_path)
    repair_command = repair_resource.get("command")
    if (
        repair_resource.get("schema_version") != "w09-stage-resource-v1"
        or repair_resource.get("label") != "rfq_full_stage_repair01"
        or repair_resource.get("return_code") != 0
        or not isinstance(repair_command, list)
        or not repair_command
        or "--resume" in repair_command
    ):
        raise MissionFinalizationError("successful RFQ repair resource receipt mismatch")
    source_arguments = [
        value
        for value in repair_command
        if isinstance(value, str) and value.endswith("/source/rfq_full_stage.py")
    ]
    try:
        run_index = repair_command.index("--run-dir")
        command_run_dir = repair_command[run_index + 1]
    except (ValueError, IndexError):
        command_run_dir = None
    if (
        len(source_arguments) != 1
        or not Path(source_arguments[0]).is_absolute()
        or not isinstance(command_run_dir, str)
        or Path(command_run_dir).name != manifest.get("run_id")
    ):
        raise MissionFinalizationError("RFQ repair resource command identity mismatch")
    active_source = checked_relative_path(
        run_dir, "source/rfq_full_stage.py", "current RFQ repair source"
    )
    frozen_source = checked_relative_path(
        run_dir, "queries/rfq_full_stage.py", "frozen RFQ repair query"
    )
    if sha256(active_source) != sha256(frozen_source):
        raise MissionFinalizationError("executed/frozen RFQ repair source mismatch")
    repair_resource_sha = sha256(repair_resource_path)

    return {
        "status": RFQ_PARTIAL_STATUS,
        "analysis_scope": RFQ_ANALYSIS_SCOPE,
        "retained_population": "RETAINED_OBSERVED_SUBSET",
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_object": expected_detail,
        "unique_objects_total": total_objects,
        "consumed_unique_objects": consumed_objects,
        "quarantined_unique_objects": quarantined_objects,
        "unique_bytes_total": total_bytes,
        "consumed_bytes": consumed_bytes,
        "quarantined_bytes": quarantined_bytes,
        "manifest_object_set_sha256": manifest_set_sha,
        "consumed_object_set_sha256": consumed_set_sha,
        "quarantined_object_set_sha256": quarantined_set_sha,
        "selection_fingerprint_sha256": selection_sha,
        "quarantine_gap_set_sha256": gap_set_sha,
        "quarantine_gap": gap,
        "partial_object_coverage_hours": partial_hours,
        "initial_execution_commit": previous_commit,
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "initial_source_manifest_sha256": previous_source,
        "previous_source_manifest_sha256": previous_source,
        "current_source_manifest_sha256": current_source,
        "initial_source_sha256s_sha256": previous_source_sums,
        "previous_source_sha256s_sha256": previous_source_sums,
        "current_source_sha256s_sha256": current_source_sums,
        "initial_query_set_sha256": previous_query,
        "previous_query_set_sha256": previous_query,
        "current_query_set_sha256": current_query,
        "declaration_path": RFQ_DECLARATION.as_posix(),
        "declaration_sha256": declaration_sha,
        "receipt_path": RFQ_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt_sha,
        "repair_receipt_path": REPAIR_REGISTRATION_RECEIPT.as_posix(),
        "repair_receipt_sha256": repair_receipt_sha,
        "failed_state_path": FAILED_RFQ_STATE.as_posix(),
        "failed_state_sha256": failed_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY.as_posix(),
        "failed_input_identity_sha256": failed_input_sha,
        "cycle1_duckdb_binding": cycle_binding,
        "completed_state_path": ACTIVE_RFQ_STATE.as_posix(),
        "completed_state_sha256": active_state_sha,
        "successful_resource_receipt_path": ACTIVE_RFQ_REPAIR_RESOURCE.as_posix(),
        "successful_resource_receipt_sha256": repair_resource_sha,
        "claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }


def _validate_repair03_parser_contract(
    contract: Mapping[str, Any],
    *,
    run_id: str,
    registered_query_sha256: str,
    audit_path: Path,
    audit_sha256: str,
) -> None:
    """Validate the outcome-independent parser correction registered for repair-03."""
    expected = {
        "schema_version": "rfq-inner-payload-parser-contract-v1",
        "run_id": run_id,
        "finding": "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD",
        "mission_sha256": EXPECTED_MISSION_SHA,
        "selection_fingerprint_sha256": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "retained_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "retained_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "outer_ndjson_policy": "STRICT_NDJSON_IGNORE_ERRORS_FALSE",
        "expected_blank_control_markers": list(EXPECTED_RFQ_BLANK_CONTROL_MARKERS),
        "expected_blank_raw_representation": "EXACT_EMPTY_STRING",
        "non_control_payload_policy": (
            "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED"
        ),
        "unexpected_payload_policy": "ABORT_BEFORE_RESULT",
        "line_salvage": False,
        "data_selection_change": False,
        "hypothesis_design_change": False,
        "registered_query_path": "queries/rfq_full_stage.py",
        "registered_query_sha256": registered_query_sha256,
        "audit_path": audit_path.as_posix(),
        "audit_sha256": audit_sha256,
    }
    if set(contract) != set(expected) | {"created_at_utc"}:
        raise MissionFinalizationError("repair-03 parser-contract field set mismatch")
    for field, value in expected.items():
        if contract.get(field) != value:
            raise MissionFinalizationError(
                f"repair-03 parser-contract mismatch: {field}"
            )
    _require_utc_timestamp(contract.get("created_at_utc"), "repair-03 parser contract")


def _validate_repair03_inner_payload_audit(
    audit: Mapping[str, Any],
    *,
    run_id: str,
    input_identity_sha256: str,
    audit_source_sha256: str,
    expected_objects: Sequence[Mapping[str, Any]],
) -> None:
    """Recompute the decisive audit totals without trusting its top-level status."""
    audit_contract = {
        "schema_version": "rfq-inner-payload-parser-contract-v1",
        "marker_case": "EXACT_CASE_SENSITIVE",
        "data_frame_marker": "MARKER_FIELD_ABSENT",
        "data_frame_raw": "NONEMPTY_JSON_OBJECT",
        "json_object_payload_markers": ["segment_receipt"],
        "empty_control_markers": list(EXPECTED_RFQ_BLANK_CONTROL_MARKERS),
        "empty_control_raw": "EXACT_EMPTY_STRING",
        "explicit_null_marker": "FAIL_CLOSED",
        "unknown_or_invalid_marker": "FAIL_CLOSED",
        "missing_null_or_non_string_raw": "FAIL_CLOSED",
        "valid_non_object_inner_json": "FAIL_CLOSED",
        "raw_b64_without_raw": "FAIL_CLOSED",
    }
    expected_markers = {
        **EXPECTED_RFQ_CONTROL_MARKER_COUNTS,
        "segment_receipt": 560,
    }
    expected_fields = {
        "analysis_result_opened",
        "audit_script_path",
        "audit_script_sha256",
        "bytes_scanned",
        "completed_at_utc",
        "data_frame_rows",
        "expected_empty_control_marker_allowlist",
        "expected_empty_control_marker_rows",
        "identity_mismatch_count",
        "identity_mismatches",
        "input_fingerprint",
        "input_identity_path",
        "input_identity_sha256",
        "json_object_payload_marker_contract",
        "lines_scanned",
        "marker_counts",
        "object_summaries",
        "objects_scanned",
        "outer_invalid_line_count",
        "outer_invalid_object_count",
        "parser_contract",
        "parser_contract_sha256",
        "raw_payload_redacted",
        "run_id",
        "schema_version",
        "scope",
        "segment_receipt_rows",
        "started_at_utc",
        "status",
        "unexpected_inner_payload_object_count",
        "unexpected_inner_payload_objects",
        "unexpected_inner_payload_row_count",
        "wall_seconds",
        "workers",
    }
    if (
        set(audit) != expected_fields
        or audit.get("schema_version") != "rfq-inner-payload-contract-audit-v1"
        or audit.get("run_id") != run_id
        or audit.get("scope")
        != "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT"
        or audit.get("status") != "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("raw_payload_redacted") is not True
        or audit.get("audit_script_path")
        != "tmp/rfq_inner_payload_contract_audit03.py"
        or audit.get("audit_script_sha256") != audit_source_sha256
        or audit.get("workers") != 8
        or not isinstance(audit.get("wall_seconds"), (int, float))
        or audit.get("wall_seconds") <= 0
        or audit.get("input_identity_path") != RFQ_INPUT_IDENTITY.as_posix()
        or audit.get("input_identity_sha256") != input_identity_sha256
        or audit.get("input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or audit.get("parser_contract") != audit_contract
        or audit.get("parser_contract_sha256")
        != canonical_json_sha256(audit_contract)
        or audit.get("parser_contract_sha256")
        != EXPECTED_RFQ_AUDITED_PARSER_CONTRACT_03_SHA256
        or audit.get("expected_empty_control_marker_allowlist")
        != list(EXPECTED_RFQ_BLANK_CONTROL_MARKERS)
        or audit.get("json_object_payload_marker_contract")
        != ["<marker field absent>", "segment_receipt"]
        or audit.get("objects_scanned") != EXPECTED_RFQ_RETAINED_OBJECTS
        or audit.get("bytes_scanned") != EXPECTED_RFQ_RETAINED_BYTES
        or audit.get("lines_scanned") != EXPECTED_RFQ_RETAINED_LINES
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("outer_invalid_object_count") != 0
        or audit.get("outer_invalid_line_count") != 0
        or audit.get("expected_empty_control_marker_rows")
        != EXPECTED_RFQ_EMPTY_CONTROL_ROWS
        or audit.get("data_frame_rows") != 58_143_241
        or audit.get("segment_receipt_rows") != 560
        or audit.get("marker_counts") != expected_markers
        or audit.get("unexpected_inner_payload_object_count") != 0
        or audit.get("unexpected_inner_payload_row_count") != 0
        or audit.get("unexpected_inner_payload_objects") != []
    ):
        raise MissionFinalizationError("repair-03 inner-payload audit mismatch")
    _require_utc_timestamp(audit.get("started_at_utc"), "repair-03 audit start")
    _require_utc_timestamp(audit.get("completed_at_utc"), "repair-03 audit completion")
    summaries = audit.get("object_summaries")
    if not isinstance(summaries, list) or len(summaries) != EXPECTED_RFQ_RETAINED_OBJECTS:
        raise MissionFinalizationError("repair-03 audit object summaries are incomplete")
    keys: set[str] = set()
    total_bytes = 0
    total_lines = 0
    total_controls = 0
    total_frames = 0
    total_receipts = 0
    marker_counts: dict[str, int] = {}
    observed_objects: dict[str, tuple[int, str]] = {}
    for index, row in enumerate(summaries):
        if not isinstance(row, dict):
            raise MissionFinalizationError(
                f"repair-03 audit object summary is invalid: {index}"
            )
        key = row.get("key")
        if not isinstance(key, str) or not key.startswith("raw_rfq/") or key in keys:
            raise MissionFinalizationError("repair-03 audit object key is invalid")
        keys.add(key)
        expected_size = require_positive_int(
            row.get("expected_size"), f"repair-03 audit object {index} size"
        )
        lines = require_positive_int(
            row.get("total_lines"), f"repair-03 audit object {index} lines"
        )
        require_hex(row.get("expected_sha256"), "repair-03 audit expected SHA")
        if (
            row.get("observed_size") != expected_size
            or row.get("observed_sha256") != row.get("expected_sha256")
            or row.get("identity_match") is not True
            or row.get("outer_invalid_rows") != 0
            or row.get("unexpected_inner_payload_rows") != 0
            or row.get("unexpected_details") != []
            or row.get("raw_payload_redacted") is not True
        ):
            raise MissionFinalizationError(
                f"repair-03 audit object is not clean: {key}"
            )
        observed_objects[key] = (expected_size, row["expected_sha256"])
        total_bytes += expected_size
        total_lines += lines
        total_controls += require_nonnegative_int(
            row.get("expected_empty_control_marker_rows"),
            f"repair-03 audit object {index} controls",
        )
        total_frames += require_nonnegative_int(
            row.get("data_frame_rows"), f"repair-03 audit object {index} frames"
        )
        total_receipts += require_nonnegative_int(
            row.get("segment_receipt_rows"),
            f"repair-03 audit object {index} receipts",
        )
        row_markers = row.get("marker_counts")
        if not isinstance(row_markers, dict):
            raise MissionFinalizationError("repair-03 audit marker counts are invalid")
        for marker, count in row_markers.items():
            if not isinstance(marker, str):
                raise MissionFinalizationError("repair-03 audit marker is invalid")
            marker_counts[marker] = marker_counts.get(marker, 0) + require_nonnegative_int(
                count, f"repair-03 audit marker {marker}"
            )
    if (
        total_bytes != audit["bytes_scanned"]
        or total_lines != audit["lines_scanned"]
        or total_controls != audit["expected_empty_control_marker_rows"]
        or total_frames != audit["data_frame_rows"]
        or total_receipts != audit["segment_receipt_rows"]
        or marker_counts != expected_markers
    ):
        raise MissionFinalizationError("repair-03 audit object totals do not reconcile")
    expected_object_map = {
        row.get("key"): (row.get("size"), row.get("sha256"))
        for row in expected_objects
        if isinstance(row, Mapping)
    }
    if (
        len(expected_object_map) != EXPECTED_RFQ_RETAINED_OBJECTS
        or None in expected_object_map
        or observed_objects != expected_object_map
    ):
        raise MissionFinalizationError(
            "repair-03 audit object identities do not match the retained set"
        )


def _validate_repair03_audit_resource(
    resource: Mapping[str, Any], *, run_id: str
) -> None:
    command = resource.get("command")
    expected_command = [
        "/opt/w09/venv/bin/python",
        (
            f"/srv/w09-research/runs/{run_id}/"
            "tmp/rfq_inner_payload_contract_audit03.py"
        ),
        "--run-dir",
        f"/srv/w09-research/runs/{run_id}",
        "--cache-root",
        "/srv/w09-research/cache",
        "--workers",
        "8",
    ]
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_inner_payload_contract_audit03_final"
        or resource.get("return_code") != 0
        or command != expected_command
    ):
        raise MissionFinalizationError("repair-03 audit resource receipt mismatch")


def _validate_repair03_source_verification(
    run_dir: Path,
    *,
    run_id: str,
    repair03: Mapping[str, Any],
    previous_identity: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> None:
    """Validate either local-Git provenance or the W09 gitless source snapshot."""
    mode = repair03.get("source_verification_mode")
    snapshot_fields = {
        "source_snapshot_attestation_active_path",
        "source_snapshot_attestation_path",
        "source_snapshot_attestation_sha256",
        "source_snapshot_attestation",
    }
    if mode == REPAIR_03_GIT_SOURCE_MODE:
        active_attestation = run_dir / REPAIR_03_SOURCE_ATTESTATION_ACTIVE
        archived_attestation = run_dir / REPAIR_03_SOURCE_ATTESTATION_ARCHIVE
        if (
            any(field in repair03 for field in snapshot_fields)
            or active_attestation.exists()
            or active_attestation.is_symlink()
            or archived_attestation.exists()
            or archived_attestation.is_symlink()
        ):
            raise MissionFinalizationError(
                "repair-03 local-Git mode contains snapshot attestation evidence"
            )
        return
    if mode != REPAIR_03_SNAPSHOT_SOURCE_MODE:
        raise MissionFinalizationError("repair-03 source verification mode is invalid")

    attestation_sha = require_hex(
        repair03.get("source_snapshot_attestation_sha256"),
        "repair-03 source snapshot attestation SHA-256",
    )
    if (
        repair03.get("source_snapshot_attestation_active_path")
        != REPAIR_03_SOURCE_ATTESTATION_ACTIVE.as_posix()
        or repair03.get("source_snapshot_attestation_path")
        != REPAIR_03_SOURCE_ATTESTATION_ARCHIVE.as_posix()
    ):
        raise MissionFinalizationError(
            "repair-03 source snapshot attestation path mismatch"
        )
    active = require_path_hash(
        run_dir,
        REPAIR_03_SOURCE_ATTESTATION_ACTIVE,
        attestation_sha,
        "repair-03 active source snapshot attestation",
    )
    archived = require_path_hash(
        run_dir,
        REPAIR_03_SOURCE_ATTESTATION_ARCHIVE,
        attestation_sha,
        "repair-03 archived source snapshot attestation",
    )
    if active.read_bytes() != archived.read_bytes():
        raise MissionFinalizationError(
            "repair-03 active/archived source snapshot attestations differ"
        )
    attestation = load_json(active)
    expected_fields = {
        "schema_version",
        "run_id",
        "mission_sha256",
        "parent_execution_commit",
        "execution_commit",
        "direct_parent_verified",
        "git_tree",
        "source_relative",
        "source_tree_clean_at_attestation",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "commit_changed_paths",
        "attested_at_utc",
    }
    git_tree = attestation.get("git_tree")
    if (
        set(attestation) != expected_fields
        or attestation.get("schema_version")
        != "sports-autoresearch-source-snapshot-attestation-v1"
        or attestation.get("run_id") != run_id
        or attestation.get("mission_sha256") != EXPECTED_MISSION_SHA
        or attestation.get("parent_execution_commit")
        != previous_identity.get("execution_commit")
        or attestation.get("execution_commit")
        != current_identity.get("execution_commit")
        or attestation.get("direct_parent_verified") is not True
        or not isinstance(git_tree, str)
        or HEX40.fullmatch(git_tree) is None
        or attestation.get("source_relative") != REPAIR_03_SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256")
        != current_identity.get("source_manifest_sha256")
        or attestation.get("source_sha256s_sha256")
        != current_identity.get("source_sha256s_sha256")
        or attestation.get("commit_changed_paths")
        != EXPECTED_REPAIR_03_CHANGED_PATHS
    ):
        raise MissionFinalizationError(
            "repair-03 source snapshot attestation mismatch"
        )
    _require_utc_timestamp(
        attestation.get("attested_at_utc"),
        "repair-03 source snapshot attestation",
    )
    expected_binding = {
        "schema_version": "sports-autoresearch-source-snapshot-attestation-v1",
        "execution_commit": current_identity["execution_commit"],
        "git_tree": git_tree,
        "direct_parent_verified": True,
        "source_relative": REPAIR_03_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "commit_changed_paths": EXPECTED_REPAIR_03_CHANGED_PATHS,
    }
    if repair03.get("source_snapshot_attestation") != expected_binding:
        raise MissionFinalizationError(
            "repair-03 nested source snapshot attestation mismatch"
        )


def _validate_repair04_source_verification(
    run_dir: Path,
    *,
    run_id: str,
    repair04: Mapping[str, Any],
    previous_identity: Mapping[str, Any],
    current_identity: Mapping[str, Any],
    active_boundary_root: Path = Path("."),
) -> None:
    """Validate repair-04's committed source or gitless snapshot provenance."""
    mode = repair04.get("source_verification_mode")
    snapshot_fields = {
        "source_snapshot_attestation_active_path",
        "source_snapshot_attestation_path",
        "source_snapshot_attestation_sha256",
        "source_snapshot_attestation",
    }
    if mode == REPAIR_04_GIT_SOURCE_MODE:
        active_attestation = (
            run_dir / active_boundary_root / REPAIR_04_SOURCE_ATTESTATION_ACTIVE
        )
        archived_attestation = run_dir / REPAIR_04_SOURCE_ATTESTATION_ARCHIVE
        if (
            any(field in repair04 for field in snapshot_fields)
            or active_attestation.exists()
            or active_attestation.is_symlink()
            or archived_attestation.exists()
            or archived_attestation.is_symlink()
        ):
            raise MissionFinalizationError(
                "repair-04 local-Git mode contains snapshot attestation evidence"
            )
        return
    if mode != REPAIR_04_SNAPSHOT_SOURCE_MODE:
        raise MissionFinalizationError("repair-04 source verification mode is invalid")

    attestation_sha = require_hex(
        repair04.get("source_snapshot_attestation_sha256"),
        "repair-04 source snapshot attestation SHA-256",
    )
    if (
        repair04.get("source_snapshot_attestation_active_path")
        != REPAIR_04_SOURCE_ATTESTATION_ACTIVE.as_posix()
        or repair04.get("source_snapshot_attestation_path")
        != REPAIR_04_SOURCE_ATTESTATION_ARCHIVE.as_posix()
    ):
        raise MissionFinalizationError(
            "repair-04 source snapshot attestation path mismatch"
        )
    active = require_path_hash(
        run_dir,
        active_boundary_root / REPAIR_04_SOURCE_ATTESTATION_ACTIVE,
        attestation_sha,
        "repair-04 active source snapshot attestation",
    )
    archived = require_path_hash(
        run_dir,
        REPAIR_04_SOURCE_ATTESTATION_ARCHIVE,
        attestation_sha,
        "repair-04 archived source snapshot attestation",
    )
    if active.read_bytes() != archived.read_bytes():
        raise MissionFinalizationError(
            "repair-04 active/archived source snapshot attestations differ"
        )
    attestation = load_json(active)
    expected_fields = {
        "schema_version",
        "run_id",
        "mission_sha256",
        "parent_execution_commit",
        "execution_commit",
        "direct_parent_verified",
        "git_tree",
        "source_relative",
        "source_tree_clean_at_attestation",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "commit_changed_paths",
        "attested_at_utc",
    }
    git_tree = attestation.get("git_tree")
    if (
        set(attestation) != expected_fields
        or attestation.get("schema_version")
        != "sports-autoresearch-source-snapshot-attestation-v1"
        or attestation.get("run_id") != run_id
        or attestation.get("mission_sha256") != EXPECTED_MISSION_SHA
        or attestation.get("parent_execution_commit")
        != previous_identity.get("execution_commit")
        or attestation.get("execution_commit")
        != current_identity.get("execution_commit")
        or attestation.get("direct_parent_verified") is not True
        or not isinstance(git_tree, str)
        or HEX40.fullmatch(git_tree) is None
        or attestation.get("source_relative") != REPAIR_04_SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256")
        != current_identity.get("source_manifest_sha256")
        or attestation.get("source_sha256s_sha256")
        != current_identity.get("source_sha256s_sha256")
        or attestation.get("commit_changed_paths")
        != EXPECTED_REPAIR_04_CHANGED_PATHS
    ):
        raise MissionFinalizationError(
            "repair-04 source snapshot attestation mismatch"
        )
    _require_utc_timestamp(
        attestation.get("attested_at_utc"),
        "repair-04 source snapshot attestation",
    )
    expected_binding = {
        "schema_version": "sports-autoresearch-source-snapshot-attestation-v1",
        "execution_commit": current_identity["execution_commit"],
        "git_tree": git_tree,
        "direct_parent_verified": True,
        "source_relative": REPAIR_04_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "commit_changed_paths": EXPECTED_REPAIR_04_CHANGED_PATHS,
    }
    if repair04.get("source_snapshot_attestation") != expected_binding:
        raise MissionFinalizationError(
            "repair-04 nested source snapshot attestation mismatch"
        )


def _validate_repair05_source_verification(
    run_dir: Path,
    *,
    run_id: str,
    repair05: Mapping[str, Any],
    previous_identity: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> None:
    """Validate repair-05's committed source or gitless snapshot provenance."""
    mode = repair05.get("source_verification_mode")
    snapshot_fields = {
        "source_snapshot_attestation_active_path",
        "source_snapshot_attestation_path",
        "source_snapshot_attestation_sha256",
        "source_snapshot_attestation",
    }
    if mode == REPAIR_05_GIT_SOURCE_MODE:
        active_attestation = run_dir / REPAIR_05_SOURCE_ATTESTATION_ACTIVE
        archived_attestation = run_dir / REPAIR_05_SOURCE_ATTESTATION_ARCHIVE
        if (
            any(field in repair05 for field in snapshot_fields)
            or active_attestation.exists()
            or active_attestation.is_symlink()
            or archived_attestation.exists()
            or archived_attestation.is_symlink()
        ):
            raise MissionFinalizationError(
                "repair-05 local-Git mode contains snapshot attestation evidence"
            )
        return
    if mode != REPAIR_05_SNAPSHOT_SOURCE_MODE:
        raise MissionFinalizationError("repair-05 source verification mode is invalid")

    attestation_sha = require_hex(
        repair05.get("source_snapshot_attestation_sha256"),
        "repair-05 source snapshot attestation SHA-256",
    )
    if (
        repair05.get("source_snapshot_attestation_active_path")
        != REPAIR_05_SOURCE_ATTESTATION_ACTIVE.as_posix()
        or repair05.get("source_snapshot_attestation_path")
        != REPAIR_05_SOURCE_ATTESTATION_ARCHIVE.as_posix()
    ):
        raise MissionFinalizationError(
            "repair-05 source snapshot attestation path mismatch"
        )
    active = require_path_hash(
        run_dir,
        REPAIR_05_SOURCE_ATTESTATION_ACTIVE,
        attestation_sha,
        "repair-05 active source snapshot attestation",
    )
    archived = require_path_hash(
        run_dir,
        REPAIR_05_SOURCE_ATTESTATION_ARCHIVE,
        attestation_sha,
        "repair-05 archived source snapshot attestation",
    )
    if active.read_bytes() != archived.read_bytes():
        raise MissionFinalizationError(
            "repair-05 active/archived source snapshot attestations differ"
        )
    attestation = load_json(active)
    expected_fields = {
        "schema_version",
        "run_id",
        "mission_sha256",
        "parent_execution_commit",
        "execution_commit",
        "direct_parent_verified",
        "git_tree",
        "source_relative",
        "source_tree_clean_at_attestation",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "commit_changed_paths",
        "attested_at_utc",
    }
    git_tree = attestation.get("git_tree")
    if (
        set(attestation) != expected_fields
        or attestation.get("schema_version")
        != "sports-autoresearch-source-snapshot-attestation-v1"
        or attestation.get("run_id") != run_id
        or attestation.get("mission_sha256") != EXPECTED_MISSION_SHA
        or attestation.get("parent_execution_commit")
        != previous_identity.get("execution_commit")
        or attestation.get("execution_commit")
        != current_identity.get("execution_commit")
        or attestation.get("direct_parent_verified") is not True
        or not isinstance(git_tree, str)
        or HEX40.fullmatch(git_tree) is None
        or attestation.get("source_relative") != REPAIR_05_SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256")
        != current_identity.get("source_manifest_sha256")
        or attestation.get("source_sha256s_sha256")
        != current_identity.get("source_sha256s_sha256")
        or attestation.get("commit_changed_paths")
        != EXPECTED_REPAIR_05_CHANGED_PATHS
    ):
        raise MissionFinalizationError(
            "repair-05 source snapshot attestation mismatch"
        )
    _require_utc_timestamp(
        attestation.get("attested_at_utc"),
        "repair-05 source snapshot attestation",
    )
    expected_binding = {
        "schema_version": "sports-autoresearch-source-snapshot-attestation-v1",
        "execution_commit": current_identity["execution_commit"],
        "git_tree": git_tree,
        "direct_parent_verified": True,
        "source_relative": REPAIR_05_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "commit_changed_paths": EXPECTED_REPAIR_05_CHANGED_PATHS,
    }
    if repair05.get("source_snapshot_attestation") != expected_binding:
        raise MissionFinalizationError(
            "repair-05 nested source snapshot attestation mismatch"
        )


def validate_rfq_double_quarantine(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    *,
    prefix_only: bool = False,
    active_boundary_root: Path = Path("."),
) -> dict[str, Any]:
    """Validate the append-only repair-01 -> repair-02 RFQ evidence chain."""
    run_id = manifest.get("run_id")
    if rfq.get("run_id") != run_id:
        raise MissionFinalizationError("repair-02 RFQ summary run_id mismatch")
    if (
        rfq.get("status") != RFQ_PARTIAL_STATUS
        or rfq.get("analysis_scope") != RFQ_ANALYSIS_SCOPE
    ):
        raise MissionFinalizationError(
            "repair-02 RFQ stage is not partial descriptive discovery"
        )
    repairs_raw = manifest.get("data_integrity_repairs")
    if (
        not isinstance(repairs_raw, list)
        or len(repairs_raw) != 2
        or any(not isinstance(row, dict) for row in repairs_raw)
        or [row.get("repair_id") for row in repairs_raw] != ["repair-01", "repair-02"]
    ):
        raise MissionFinalizationError(
            "repair-02 requires the ordered repair-01/repair-02 chain"
        )
    repair01, repair02 = repairs_raw
    if (
        repair01.get("schema_version")
        != "sports-autoresearch-data-integrity-repair-v1"
        or repair02.get("schema_version")
        != "sports-autoresearch-data-integrity-repair-v2"
        or repair02.get("parent_repair_id") != "repair-01"
        or manifest.get("status") not in {repair02.get("post_repair_status"), "COMPLETE"}
    ):
        raise MissionFinalizationError("repair-02 registration identity/status mismatch")

    authoritative_union, logical_bindings, release_receipts = (
        _rebuild_selected_rfq_union(run_dir, manifest.get("selected_releases", []))
    )
    full_bytes = sum(row["size"] for row in authoritative_union)
    full_set_sha = object_set_sha256(authoritative_union)
    if (
        len(authoritative_union) != EXPECTED_RFQ_FULL_OBJECTS
        or logical_bindings != EXPECTED_RFQ_FULL_LOGICAL_BINDINGS
        or full_bytes != EXPECTED_RFQ_FULL_BYTES
        or full_set_sha != EXPECTED_RFQ_FULL_SET_SHA256
    ):
        raise MissionFinalizationError(
            "immutable RFQ manifest union is not the registered 284-object full set"
        )

    declaration01, declaration01_sha = _require_active_archive_equal(
        run_dir,
        RFQ_DECLARATION,
        REPAIR_DECLARATION,
        repair01.get("declaration_sha256"),
        "repair-01 quarantine declaration",
    )
    receipt01, receipt01_sha = _require_active_archive_equal(
        run_dir,
        RFQ_MALFORMED_RECEIPT,
        REPAIR_MALFORMED_RECEIPT,
        repair01.get("receipt_sha256"),
        "repair-01 malformed-object receipt",
    )
    declaration02, declaration02_sha = _require_active_archive_equal(
        run_dir,
        RFQ_DECLARATION_02,
        REPAIR_DECLARATION_02,
        repair02.get("declaration_sha256"),
        "repair-02 quarantine declaration",
    )
    receipt02, receipt02_sha = _require_active_archive_equal(
        run_dir,
        RFQ_MALFORMED_RECEIPT_02,
        REPAIR_MALFORMED_RECEIPT_02,
        repair02.get("receipt_sha256"),
        "repair-02 malformed-object receipt",
    )
    authorization, authorization_sha = _require_active_archive_equal(
        run_dir,
        RFQ_REPAIR_02_AUTHORIZATION,
        REPAIR_AUTHORIZATION_02,
        repair02.get("authorization_evidence_sha256"),
        "repair-02 operator authorization",
    )
    strict_policy = (
        "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
    )
    if (
        declaration01.get("schema_version") != "rfq-object-quarantine-v1"
        or declaration01.get("run_id") != run_id
        or declaration01.get("mode") != EXPECTED_MODE
        or declaration01.get("remaining_object_parse_policy") != strict_policy
        or declaration01.get("created_after_structural_failure_before_rfq_result")
        is not True
        or declaration01.get("dependent_rfq_result_opened") is not False
    ):
        raise MissionFinalizationError("repair-01 declaration was not preserved")
    declared01 = declaration01.get("quarantined_objects")
    newly02 = declaration02.get("newly_quarantined_objects")
    cumulative02 = declaration02.get("cumulative_quarantined_objects")
    if (
        set(declaration02) != {
            "schema_version",
            "run_id",
            "mode",
            "finding",
            "disposition",
            "authority_basis",
            "created_at_utc",
            "created_after_structural_failure_before_rfq_result",
            "dependent_rfq_result_opened",
            "source_execution_commit",
            "parent_repair_id",
            "previous_declaration_path",
            "previous_declaration_sha256",
            "authorization_evidence_path",
            "authorization_evidence_sha256",
            "newly_quarantined_objects",
            "cumulative_quarantined_objects",
            "remaining_object_parse_policy",
            "selection_rule",
            "result_use_prohibited",
            "trial_disposition_if_repair_fails",
        }
        or
        declaration02.get("schema_version") != "rfq-object-quarantine-v2"
        or declaration02.get("run_id") != run_id
        or declaration02.get("mode") != EXPECTED_MODE
        or declaration02.get("finding")
        != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or declaration02.get("disposition") != "WHOLE_OBJECT_QUARANTINE"
        or declaration02.get("authority_basis")
        != "EXPLICIT_OPERATOR_AUTHORIZATION_REPAIR_02"
        or declaration02.get("created_after_structural_failure_before_rfq_result")
        is not True
        or declaration02.get("dependent_rfq_result_opened") is not False
        or declaration02.get("source_execution_commit")
        != repair01.get("current_execution_commit")
        or declaration02.get("parent_repair_id") != "repair-01"
        or declaration02.get("previous_declaration_path")
        != REPAIR_DECLARATION.as_posix()
        or declaration02.get("previous_declaration_sha256") != declaration01_sha
        or declaration02.get("authorization_evidence_path")
        != RFQ_REPAIR_02_AUTHORIZATION.as_posix()
        or declaration02.get("authorization_evidence_sha256") != authorization_sha
        or declaration02.get("remaining_object_parse_policy") != strict_policy
        or not isinstance(declared01, list)
        or len(declared01) != 1
        or not isinstance(newly02, list)
        or len(newly02) != 1
        or not isinstance(cumulative02, list)
        or len(cumulative02) != 2
        or cumulative02 != sorted([declared01[0], newly02[0]], key=lambda row: row["key"])
    ):
        raise MissionFinalizationError("repair-02 cumulative declaration mismatch")
    for field in ("selection_rule", "result_use_prohibited"):
        if not isinstance(declaration02.get(field), str) or not declaration02[field]:
            raise MissionFinalizationError(f"repair-02 declaration lacks {field}")
    if (
        "no line-level salvage" not in declaration02["result_use_prohibited"]
        or "ABORT_WITHOUT_RFQ_RESULT"
        not in str(declaration02.get("trial_disposition_if_repair_fails"))
    ):
        raise MissionFinalizationError("repair-02 declaration result boundary mismatch")
    _require_utc_timestamp(
        declaration02.get("created_at_utc"), "repair-02 declaration"
    )

    detail01 = _validate_malformed_object_receipt(
        run_dir,
        manifest,
        receipt01,
        declared01[0],
        RFQ_MALFORMED_RECEIPT,
        "repair-01",
    )
    detail02 = _validate_malformed_object_receipt(
        run_dir,
        manifest,
        receipt02,
        newly02[0],
        RFQ_MALFORMED_RECEIPT_02,
        "repair-02",
    )
    receipt01_lines = receipt01.get("invalid_lines")
    receipt02_lines = receipt02.get("invalid_lines")
    if (
        not isinstance(receipt01_lines, list)
        or len(receipt01_lines) != 1
        or receipt01_lines[0].get("line_number") != 848
        or receipt01_lines[0].get("line_sha256")
        != "ecf5a1b6e309d44d862d0d2899328f51bffe28a97c90120920efd86ce328cc3d"
        or not isinstance(receipt02_lines, list)
        or len(receipt02_lines) != 1
        or receipt02_lines[0].get("line_number") != 264979
        or receipt02_lines[0].get("line_sha256")
        != "aaaf7ec39cd2213f0bc3e73c180678e3bffbddd7962f5d537f227a78c0e3f9cd"
        or
        receipt02.get("manifest_sha256") != detail02["manifest_sha256"]
        or receipt02.get("version_id") != detail02["version_id"]
        or receipt02.get("quarantine_authorized") is not False
    ):
        raise MissionFinalizationError("repair-02 pre-authorization receipt mismatch")
    details = sorted(
        [
            {**detail01, "receipt_sha256": receipt01_sha,
             "declaration_sha256": declaration01_sha},
            {**detail02, "receipt_sha256": receipt02_sha,
             "declaration_sha256": declaration02_sha},
        ],
        key=lambda row: row["key"],
    )
    quarantine_keys = [row["key"] for row in details]
    authoritative_quarantine = [
        row for row in authoritative_union if row["key"] in set(quarantine_keys)
    ]
    if (
        len(authoritative_quarantine) != 2
        or any(row["bound_release_ids"] != [detail["release_id"]]
               for row, detail in zip(authoritative_quarantine, details))
        or any(
            {key: row[key] for key in ("key", "sha256", "size")}
            != {key: detail[key] for key in ("key", "sha256", "size")}
            for row, detail in zip(authoritative_quarantine, details)
        )
    ):
        raise MissionFinalizationError(
            "two quarantines do not exactly match the immutable manifest union"
        )
    retained = [row for row in authoritative_union if row["key"] not in quarantine_keys]
    retained_bindings = sum(len(row["bound_release_ids"]) for row in retained)
    retained_bytes = sum(row["size"] for row in retained)
    retained_sha = object_set_sha256(retained)
    quarantine_sha = object_set_sha256(authoritative_quarantine)
    if (
        len(retained) != EXPECTED_RFQ_RETAINED_OBJECTS
        or retained_bindings != EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS
        or retained_bytes != EXPECTED_RFQ_RETAINED_BYTES
        or retained_sha != EXPECTED_RFQ_RETAINED_SET_SHA256
        or len(authoritative_quarantine) != EXPECTED_RFQ_QUARANTINED_OBJECTS
        or sum(len(row["bound_release_ids"]) for row in authoritative_quarantine)
        != EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS
        or sum(row["size"] for row in authoritative_quarantine)
        != EXPECTED_RFQ_QUARANTINED_BYTES
        or quarantine_sha != EXPECTED_RFQ_QUARANTINED_SET_SHA256
    ):
        raise MissionFinalizationError("repair-02 retained/quarantine arithmetic mismatch")

    action = authorization.get("authorized_action")
    if (
        set(authorization) != {
            "schema_version",
            "run_id",
            "repair_id",
            "authorized_at_utc",
            "authority_source",
            "authorized_action",
        }
        or
        authorization.get("schema_version")
        != "sports-autoresearch-repair-authorization-v1"
        or authorization.get("run_id") != run_id
        or authorization.get("repair_id") != "repair-02"
        or authorization.get("authority_source") != "operator_chat_message"
        or not isinstance(action, str)
        or detail02["key"] not in action
        or "282/284" not in action
        or "59,185,856,724" not in action
        or RFQ_PARTIAL_STATUS not in action
        or "禁止逐行修补" not in action
        or "从新 scratch 重跑 RFQ" not in action
    ):
        raise MissionFinalizationError("repair-02 operator authorization is insufficient")
    _require_utc_timestamp(
        authorization.get("authorized_at_utc"), "repair-02 authorization"
    )

    audit, audit_sha = _require_active_archive_equal(
        run_dir,
        RFQ_STRUCTURAL_AUDIT_02,
        ARCHIVED_RFQ_STRUCTURAL_AUDIT_02,
        repair02.get("structural_audit_sha256"),
        "RFQ structural audit-02",
    )
    audit_resource, audit_resource_sha = _require_active_archive_equal(
        run_dir,
        RFQ_STRUCTURAL_AUDIT_RESOURCE_02,
        ARCHIVED_RFQ_STRUCTURAL_AUDIT_RESOURCE_02,
        repair02.get("structural_audit_resource_sha256"),
        "RFQ structural audit-02 resource",
    )
    blocker, blocker_sha = _require_active_archive_equal(
        run_dir,
        RFQ_SECOND_BLOCKER,
        ARCHIVED_RFQ_SECOND_BLOCKER,
        repair02.get("blocker_evidence_sha256"),
        "RFQ second-object blocker",
    )
    if (
        audit.get("schema_version") != "rfq-structural-integrity-audit-v1"
        or audit.get("run_id") != run_id
        or audit.get("scope") != "OUTER_NDJSON_STRUCTURE_ONLY_NO_RESEARCH_RESULT"
        or audit.get("status") != "COMPLETE_STRUCTURAL_AUDIT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("raw_payload_redacted") is not True
        or audit.get("objects_scanned") != EXPECTED_RFQ_FULL_OBJECTS
        or audit.get("bytes_scanned") != EXPECTED_RFQ_FULL_BYTES
        or audit.get("lines_scanned") != EXPECTED_RFQ_FULL_LINES
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("invalid_object_count") != 2
        or audit.get("invalid_line_count") != 2
        or audit.get("input_fingerprint") != full_set_sha
        or audit.get("input_identity_path") != FAILED_RFQ_INPUT_IDENTITY.as_posix()
        or audit.get("input_identity_sha256")
        != repair01.get("failed_input_identity_sha256")
    ):
        raise MissionFinalizationError("complete RFQ structural audit mismatch")
    invalid_objects = audit.get("invalid_objects")
    if not isinstance(invalid_objects, list) or len(invalid_objects) != 2:
        raise MissionFinalizationError("structural audit invalid-object set mismatch")
    audit_by_key = {
        row.get("key"): row for row in invalid_objects if isinstance(row, dict)
    }
    if set(audit_by_key) != set(quarantine_keys):
        raise MissionFinalizationError("structural audit found an unexpected bad object")
    receipt_by_key = {detail01["key"]: receipt01, detail02["key"]: receipt02}
    for detail in details:
        row = audit_by_key[detail["key"]]
        receipt = receipt_by_key[detail["key"]]
        if (
            row.get("identity_match") is not True
            or row.get("expected_size") != detail["size"]
            or row.get("observed_size") != detail["size"]
            or row.get("expected_sha256") != detail["sha256"]
            or row.get("observed_sha256") != detail["sha256"]
            or row.get("invalid_line_count") != 1
            or row.get("invalid_lines") != receipt.get("invalid_lines")
            or row.get("total_lines") != receipt.get("total_lines")
            or row.get("raw_payload_redacted") is not True
        ):
            raise MissionFinalizationError(
                f"structural audit object receipt mismatch: {detail['key']}"
            )
    if (
        audit_resource.get("schema_version") != "w09-stage-resource-v1"
        or audit_resource.get("label") != "rfq_structural_integrity_audit02"
        or audit_resource.get("return_code") != 0
        or not isinstance(audit_resource.get("command"), list)
        or "--resume" in audit_resource.get("command", [])
    ):
        raise MissionFinalizationError("structural audit resource receipt mismatch")
    if (
        blocker.get("schema_version") != "rfq-data-integrity-blocker-v1"
        or blocker.get("run_id") != run_id
        or blocker.get("status") != "DATA_INTEGRITY_BLOCKER"
        or blocker.get("blocker")
        != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or blocker.get("failed_attempt") != "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02"
        or blocker.get("analysis_result_opened") is not False
        or blocker.get("automatic_additional_quarantine_prohibited") is not True
        or blocker.get("next_required_authority")
        != "EXPLICIT_REPAIR_02_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or blocker.get("repair_execution_commit")
        != repair01.get("current_execution_commit")
        or blocker.get("malformed_object_receipt_path")
        != RFQ_MALFORMED_RECEIPT_02.as_posix()
        or blocker.get("malformed_object_receipt_sha256") != receipt02_sha
        or blocker.get("failed_state_path") != ACTIVE_RFQ_STATE.as_posix()
        or blocker.get("failed_state_sha256")
        != repair02.get("failed_state_sha256")
        or blocker.get("resource_receipt_path")
        != ACTIVE_RFQ_REPAIR_RESOURCE.as_posix()
        or blocker.get("resource_receipt_sha256")
        != repair02.get("failed_resource_receipt_sha256")
        or blocker.get("preserved_scratch_receipt_path")
        != "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json"
        or blocker.get("preserved_scratch_receipt_sha256")
        != repair02.get("failed_scratch_receipt_sha256")
        or blocker.get("complete_structural_audit", {}).get("sha256") != audit_sha
        or blocker.get("complete_structural_audit", {}).get("resource_sha256")
        != audit_resource_sha
        or blocker.get("complete_structural_audit", {}).get("objects_scanned")
        != EXPECTED_RFQ_FULL_OBJECTS
        or blocker.get("complete_structural_audit", {}).get("bytes_scanned")
        != EXPECTED_RFQ_FULL_BYTES
        or blocker.get("complete_structural_audit", {}).get("lines_scanned")
        != EXPECTED_RFQ_FULL_LINES
        or blocker.get("complete_structural_audit", {}).get("invalid_object_count")
        != 2
        or blocker.get("complete_structural_audit", {}).get("invalid_line_count")
        != 2
        or blocker.get("complete_structural_audit", {}).get("identity_mismatch_count")
        != 0
    ):
        raise MissionFinalizationError("second malformed-object blocker mismatch")

    # Remaining chain, attempt, output and gap validation continues below.
    return _validate_rfq_double_chain_and_outputs(
        run_dir=run_dir,
        manifest=manifest,
        rfq=rfq,
        repair01=repair01,
        repair02=repair02,
        authoritative_union=authoritative_union,
        logical_bindings=logical_bindings,
        release_receipts=release_receipts,
        retained=retained,
        details=details,
        full_set_sha=full_set_sha,
        retained_sha=retained_sha,
        quarantine_sha=quarantine_sha,
        declaration01_sha=declaration01_sha,
        declaration02_sha=declaration02_sha,
        receipt01_sha=receipt01_sha,
        receipt02_sha=receipt02_sha,
        authorization_sha=authorization_sha,
        audit_sha=audit_sha,
        audit_resource_sha=audit_resource_sha,
        blocker_sha=blocker_sha,
        prefix_only=prefix_only,
        active_boundary_root=active_boundary_root,
    )


def _validate_rfq_double_chain_and_outputs(
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    repair01: Mapping[str, Any],
    repair02: Mapping[str, Any],
    authoritative_union: Sequence[Mapping[str, Any]],
    logical_bindings: int,
    release_receipts: Sequence[Mapping[str, Any]],
    retained: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, Any]],
    full_set_sha: str,
    retained_sha: str,
    quarantine_sha: str,
    declaration01_sha: str,
    declaration02_sha: str,
    receipt01_sha: str,
    receipt02_sha: str,
    authorization_sha: str,
    audit_sha: str,
    audit_resource_sha: str,
    blocker_sha: str,
    prefix_only: bool = False,
    active_boundary_root: Path = Path("."),
) -> dict[str, Any]:
    """Finish the independent repair-02 chain and successful-output audit."""
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise MissionFinalizationError("repair-02 run identity is missing")
    detail01 = next(
        (row for row in details if row.get("declaration_sha256") == declaration01_sha),
        None,
    )
    detail02 = next(
        (row for row in details if row.get("declaration_sha256") == declaration02_sha),
        None,
    )
    if not isinstance(detail01, Mapping) or not isinstance(detail02, Mapping):
        raise MissionFinalizationError("repair-02 quarantine provenance is ambiguous")

    repair01_exact = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "post_repair_status": REPAIR_PENDING_STATUS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "declaration_input_path": RFQ_DECLARATION.as_posix(),
        "receipt_input_path": RFQ_MALFORMED_RECEIPT.as_posix(),
        "declaration_path": REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": declaration01_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt01_sha,
        "failed_state_path": FAILED_RFQ_STATE.as_posix(),
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY.as_posix(),
        "failed_input_fingerprint": full_set_sha,
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
    }
    for field, expected in repair01_exact.items():
        if repair01.get(field) != expected:
            raise MissionFinalizationError(
                f"repair-01 append-chain field mismatch: {field}"
            )
    repair02_exact = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v2",
        "repair_id": "repair-02",
        "parent_repair_id": "repair-01",
        "pre_repair_status": REPAIR_PENDING_STATUS,
        "post_repair_status": REPAIR02_PENDING_STATUS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "declaration_input_path": RFQ_DECLARATION_02.as_posix(),
        "receipt_input_path": RFQ_MALFORMED_RECEIPT_02.as_posix(),
        "authorization_evidence_input_path": RFQ_REPAIR_02_AUTHORIZATION.as_posix(),
        "declaration_path": REPAIR_DECLARATION_02.as_posix(),
        "declaration_sha256": declaration02_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": receipt02_sha,
        "authorization_evidence_path": REPAIR_AUTHORIZATION_02.as_posix(),
        "authorization_evidence_sha256": authorization_sha,
        "authorization_evidence_schema": (
            "sports-autoresearch-repair-authorization-v1"
        ),
        "failed_state_path": FAILED_RFQ_STATE_02.as_posix(),
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_02.as_posix(),
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_02.as_posix(),
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_02.as_posix(),
        "structural_audit_path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        "structural_audit_sha256": audit_sha,
        "structural_audit_resource_path": (
            ARCHIVED_RFQ_STRUCTURAL_AUDIT_RESOURCE_02.as_posix()
        ),
        "structural_audit_resource_sha256": audit_resource_sha,
        "blocker_evidence_path": ARCHIVED_RFQ_SECOND_BLOCKER.as_posix(),
        "blocker_evidence_sha256": blocker_sha,
        "session_resume_evidence_path": ARCHIVED_SESSION_RESUME_02.as_posix(),
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "data_integrity_handling_change": (
            "SECOND_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
        ),
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "transaction_journal_path": REPAIR_02_TRANSACTION_JOURNAL.as_posix(),
    }
    for field, expected in repair02_exact.items():
        if repair02.get(field) != expected:
            raise MissionFinalizationError(
                f"repair-02 registration field mismatch: {field}"
            )
    declared_object01 = {
        **{
            key: value for key, value in detail01.items()
            if key not in {"receipt_sha256", "declaration_sha256"}
        },
        "receipt": RFQ_MALFORMED_RECEIPT.as_posix(),
    }
    declared_object02 = {
        **{
            key: value for key, value in detail02.items()
            if key not in {"receipt_sha256", "declaration_sha256"}
        },
        "receipt": RFQ_MALFORMED_RECEIPT_02.as_posix(),
    }
    if (
        repair01.get("quarantined_objects") != [declared_object01]
        or repair02.get("newly_quarantined_objects") != [declared_object02]
        or repair02.get("cumulative_quarantined_objects")
        != sorted([declared_object01, declared_object02], key=lambda row: row["key"])
    ):
        raise MissionFinalizationError("repair-01/repair-02 object append chain mismatch")
    _require_utc_timestamp(repair02.get("applied_at_utc"), "repair-02 registration")

    repair01_receipt_sha = _validate_repair_record_receipt(
        run_dir, repair01, REPAIR_REGISTRATION_RECEIPT, "repair-01 registration"
    )
    repair02_receipt_sha = _validate_repair_record_receipt(
        run_dir, repair02, REPAIR_REGISTRATION_RECEIPT_02, "repair-02 registration"
    )
    repair01_record_payload = (
        json.dumps(repair01, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if (
        repair02.get("previous_repair_registration_path")
        != REPAIR_REGISTRATION_RECEIPT.as_posix()
        or repair02.get("previous_repair_registration_sha256")
        != repair01_receipt_sha
        or repair02.get("previous_repair_record_sha256")
        != hashlib.sha256(repair01_record_payload).hexdigest()
    ):
        raise MissionFinalizationError("repair registration receipt chain mismatch")

    journal_sha = require_hex(
        repair02.get("transaction_journal_sha256"),
        "repair-02 transaction journal SHA-256",
    )
    journal_path = require_path_hash(
        run_dir,
        REPAIR_02_TRANSACTION_JOURNAL,
        journal_sha,
        "repair-02 transaction journal",
    )
    journal = load_json(journal_path)
    journal_mutations = journal.get("active_mutations")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *repair02.get("previous_repository_identity", {}).get("query_files", []),
        "TRIAL_REGISTRY.jsonl",
    ]
    archived_pre_manifest = run_dir / REPAIR_02_PRE_ROOT / "RUN_MANIFEST.json"
    if (
        journal.get("schema_version") != "repair02-registration-transaction-v1"
        or journal.get("repair_id") != "repair-02"
        or journal.get("run_id") != run_id
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or not archived_pre_manifest.is_file()
        or journal.get("original_manifest_sha256") != sha256(archived_pre_manifest)
        or not isinstance(journal_mutations, list)
        or [row.get("path") for row in journal_mutations if isinstance(row, dict)]
        != expected_mutation_paths
    ):
        raise MissionFinalizationError("repair-02 transaction journal identity mismatch")
    for row in journal_mutations:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "original_archive_path",
            "original_sha256",
            "replacement_sha256",
            "append_only_registry",
        }:
            raise MissionFinalizationError("repair-02 transaction mutation row is invalid")
        relative = row["path"]
        expected_archive = (REPAIR_02_PRE_ROOT / relative).as_posix()
        if (
            row.get("original_archive_path") != expected_archive
            or row.get("append_only_registry")
            is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise MissionFinalizationError(
                f"repair-02 transaction mutation policy mismatch: {relative}"
            )
        original_path = checked_relative_path(
            run_dir, expected_archive, "repair-02 transaction original"
        )
        replacement_relative = (REPAIR_02_ROOT / "post_repair" / relative).as_posix()
        replacement_path = checked_relative_path(
            run_dir, replacement_relative, "repair-02 transaction replacement"
        )
        active_path = checked_relative_path(
            run_dir,
            (active_boundary_root / relative).as_posix(),
            "repair-02 transaction active artifact",
        )
        if (
            row.get("original_sha256") != sha256(original_path)
            or row.get("replacement_sha256") != sha256(replacement_path)
        ):
            raise MissionFinalizationError(
                f"repair-02 transaction mutation hash mismatch: {relative}"
            )
        if relative == "TRIAL_REGISTRY.jsonl":
            replacement_bytes = replacement_path.read_bytes()
            if not active_path.read_bytes().startswith(replacement_bytes):
                raise MissionFinalizationError(
                    "repair-02 append-only registry replacement is not a prefix"
                )
        elif sha256(active_path) != row.get("replacement_sha256"):
            raise MissionFinalizationError(
                f"repair-02 active transaction result changed: {relative}"
            )
    actual_repair_files = sorted(
        path.relative_to(run_dir / REPAIR_02_ROOT).as_posix()
        for path in (run_dir / REPAIR_02_ROOT).rglob("*")
        if path.is_file()
    )
    if journal.get("expected_repair_files") != actual_repair_files:
        raise MissionFinalizationError("repair-02 transaction file set mismatch")

    archive01_required = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        ACTIVE_RFQ_STATE.as_posix(),
        "logs/resources/rfq_full_stage.json",
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT.json",
        RFQ_INPUT_IDENTITY.as_posix(),
        ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
    }
    archive02_required = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        ACTIVE_RFQ_STATE.as_posix(),
        "logs/resources/rfq_full_stage_repair01.json",
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json",
        RFQ_INPUT_IDENTITY.as_posix(),
        RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        RFQ_STRUCTURAL_AUDIT_RESOURCE_02.as_posix(),
        RFQ_SECOND_BLOCKER.as_posix(),
        ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        SESSION_RESUME_02.as_posix(),
    }
    archive01 = _validate_archive_inventory(
        run_dir,
        repair01,
        Path("DATA_INTEGRITY/repairs/repair-01/pre_repair"),
        archive01_required,
        "repair-01",
    )
    archive02 = _validate_archive_inventory(
        run_dir, repair02, REPAIR_02_PRE_ROOT, archive02_required, "repair-02"
    )

    repository = manifest.get("repository")
    initial_identity = repair01.get("previous_repository_identity")
    middle_identity = repair01.get("current_repository_identity")
    current_identity = repair02.get("current_repository_identity")
    identity_chain = repair02.get("repository_identity_chain")
    if (
        not isinstance(repository, dict)
        or not all(
            isinstance(item, dict)
            for item in (initial_identity, middle_identity, current_identity)
        )
        or repair02.get("initial_repository_identity") != initial_identity
        or repair02.get("previous_repository_identity") != middle_identity
        or identity_chain != [initial_identity, middle_identity, current_identity]
        or repository.get("initial_identity") != initial_identity
        or repository.get("previous_identity") != middle_identity
        or repository.get("identity_history") != identity_chain
        or repository.get("registration_repair_id") != "repair-02"
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
    ):
        raise MissionFinalizationError("repair-02 repository identity history mismatch")
    identity_fields = (
        "execution_commit",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "query_set_sha256",
        "query_files",
    )
    if any(set(item) != set(identity_fields) for item in identity_chain):
        raise MissionFinalizationError("repository identity field set mismatch")
    if len({canonical_json_sha256(item) for item in identity_chain}) != 3:
        raise MissionFinalizationError("repository identity revisions are not distinct")
    for item_index, identity in enumerate(identity_chain):
        require_hex(identity.get("execution_commit"), f"identity {item_index} commit", HEX40)
        for field in (
            "source_manifest_sha256",
            "source_sha256s_sha256",
            "query_set_sha256",
        ):
            require_hex(identity.get(field), f"identity {item_index} {field}")
    if (
        repair01.get("previous_execution_commit") != initial_identity["execution_commit"]
        or repair01.get("current_execution_commit") != middle_identity["execution_commit"]
        or repair02.get("previous_execution_commit") != middle_identity["execution_commit"]
        or repair02.get("current_execution_commit") != current_identity["execution_commit"]
        or any(repository.get(field) != current_identity[field] for field in identity_fields)
        or repository.get("previous_execution_commit") != middle_identity["execution_commit"]
        or repository.get("initial_execution_commit") != initial_identity["execution_commit"]
    ):
        raise MissionFinalizationError("repository commit append chain mismatch")
    scalar_bindings = (
        (repair01, "previous", initial_identity),
        (repair01, "current", middle_identity),
        (repair02, "previous", middle_identity),
        (repair02, "current", current_identity),
    )
    for record, prefix, identity in scalar_bindings:
        for record_suffix, identity_field in (
            ("source_manifest_sha256", "source_manifest_sha256"),
            ("source_sha256s_sha256", "source_sha256s_sha256"),
            ("query_set_sha256", "query_set_sha256"),
        ):
            if record.get(f"{prefix}_{record_suffix}") != identity[identity_field]:
                raise MissionFinalizationError(
                    f"repository {prefix} {record_suffix} chain mismatch"
                )

    stages = (
        (Path("DATA_INTEGRITY/repairs/repair-01/pre_repair"), initial_identity),
        (REPAIR_02_PRE_ROOT, middle_identity),
        (active_boundary_root, current_identity),
    )
    for root, identity in stages:
        for relative, field in (
            (Path("SOURCE_MANIFEST.json"), "source_manifest_sha256"),
            (Path("SOURCE_SHA256SUMS.txt"), "source_sha256s_sha256"),
            (Path("QUERY_SHA256SUMS.txt"), "query_set_sha256"),
        ):
            require_path_hash(
                run_dir,
                root / relative,
                identity[field],
                f"repository identity {root / relative}",
            )
        query_files = identity.get("query_files")
        if (
            not isinstance(query_files, list)
            or not query_files
            or any(not isinstance(value, str) for value in query_files)
        ):
            raise MissionFinalizationError("repository query-file identity is invalid")
        for relative in query_files:
            checked_relative_path(
                run_dir, (root / relative).as_posix(), "frozen repository query"
            )

    archived01_manifest = load_json(
        run_dir / "DATA_INTEGRITY/repairs/repair-01/pre_repair/RUN_MANIFEST.json"
    )
    archived02_manifest = load_json(run_dir / REPAIR_02_PRE_ROOT / "RUN_MANIFEST.json")
    if (
        archived01_manifest.get("run_id") != run_id
        or archived01_manifest.get("status")
        != "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING"
        or archived02_manifest.get("run_id") != run_id
        or archived02_manifest.get("status") != REPAIR_PENDING_STATUS
        or archived02_manifest.get("data_integrity_repairs") != [repair01]
        or archived02_manifest.get("session_resumes") != manifest.get("session_resumes")
    ):
        raise MissionFinalizationError("repair pre-state manifest append chain mismatch")
    archived02_repository = archived02_manifest.get("repository")
    if not isinstance(archived02_repository, dict) or any(
        archived02_repository.get(field) != middle_identity[field]
        for field in identity_fields
    ):
        raise MissionFinalizationError("repair-02 archived repository identity mismatch")

    core01 = repair01.get("core_result_artifacts")
    core02 = repair02.get("core_result_artifacts")
    expected_core_paths = {
        "REPORT/CYCLE1_CORE_SUMMARY.json",
        "REPORT/tables/CORE_HYPOTHESIS_TESTS.json",
        "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json",
        "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json",
    }
    if not isinstance(core01, list) or core02 != core01 or len(core01) != 4:
        raise MissionFinalizationError("repair chain changed the Cycle-1 result inventory")
    seen_core: set[str] = set()
    for row in core01:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise MissionFinalizationError("preserved Cycle-1 result row is invalid")
        relative = row.get("path")
        if not isinstance(relative, str) or relative in seen_core:
            raise MissionFinalizationError("preserved Cycle-1 result path is invalid")
        seen_core.add(relative)
        path = checked_relative_path(run_dir, relative, "preserved Cycle-1 result")
        if row.get("sha256") != sha256(path) or row.get("bytes") != path.stat().st_size:
            raise MissionFinalizationError(f"Cycle-1 result changed: {relative}")
    if seen_core != expected_core_paths:
        raise MissionFinalizationError("preserved Cycle-1 result path set mismatch")

    cycle_binding = _validate_cycle1_binding_for_repairs(
        run_dir, manifest, [repair01, repair02]
    )
    archived02_cycle = run_dir / REPAIR_02_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING
    active_cycle = run_dir / active_boundary_root / ACTIVE_CYCLE1_DUCKDB_BINDING
    if (
        not archived02_cycle.is_file()
        or archived02_cycle.read_bytes() != active_cycle.read_bytes()
    ):
        raise MissionFinalizationError("repair-02 changed the Cycle-1 DuckDB binding")

    # These are referenced below and also prove that the inventory rows were not
    # merely self-consistent declarations detached from the actual archives.
    if not archive01_required <= set(archive01) or not archive02_required <= set(archive02):
        raise MissionFinalizationError("repair archive evidence set is incomplete")

    failed01_state_sha = require_hex(
        repair01.get("failed_state_sha256"), "attempt-01 failed state SHA-256"
    )
    failed01_resource_sha = require_hex(
        repair01.get("failed_resource_receipt_sha256"),
        "attempt-01 resource SHA-256",
    )
    failed01_scratch_sha = require_hex(
        repair01.get("failed_scratch_receipt_sha256"),
        "attempt-01 scratch receipt SHA-256",
    )
    failed01_input_sha = require_hex(
        repair01.get("failed_input_identity_sha256"),
        "attempt-01 input identity SHA-256",
    )
    _validate_failed_attempt_files(
        run_dir,
        run_id,
        state_path=FAILED_RFQ_STATE,
        state_sha=failed01_state_sha,
        resource_path=FAILED_RFQ_RESOURCE,
        resource_sha=failed01_resource_sha,
        scratch_path=FAILED_RFQ_SCRATCH_RECEIPT,
        scratch_sha=failed01_scratch_sha,
        expected_label="rfq_full_stage",
        expected_key=str(detail01["key"]),
        expected_fingerprint=full_set_sha,
        label="attempt-01",
    )
    failed01_identity = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_INPUT_IDENTITY,
            failed01_input_sha,
            "attempt-01 input identity",
        )
    )
    _validate_failed_full_identity_v1(
        failed01_identity,
        manifest,
        authoritative_union,
        logical_bindings,
        release_receipts,
        full_set_sha,
    )
    base_failed_binding01 = {
        "repair_id": "repair-01",
        "failed_state_path": FAILED_RFQ_STATE.as_posix(),
        "failed_state_sha256": failed01_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": failed01_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed01_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY.as_posix(),
        "failed_input_identity_sha256": failed01_input_sha,
    }
    failed01_record = repair01.get("failed_attempt")
    if (
        not isinstance(failed01_record, dict)
        or failed01_record.get("failed_input_fingerprint") != full_set_sha
        or failed01_record.get("state_status") != "FAILED_RESUMABLE"
        or failed01_record.get("return_code") != 1
        or failed01_record.get("scratch_disposition")
        != "PRESERVED_RENAMED_NO_RESUME"
        or failed01_record.get("retry_requirement")
        != "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME"
        or failed01_record.get("state_sha256") != failed01_state_sha
        or failed01_record.get("resource_sha256") != failed01_resource_sha
        or failed01_record.get("scratch_receipt_sha256") != failed01_scratch_sha
        or failed01_record.get("failed_input_identity_sha256") != failed01_input_sha
    ):
        raise MissionFinalizationError("attempt-01 nested failure binding mismatch")

    old_quarantine = [
        row for row in authoritative_union if row.get("key") == detail01["key"]
    ]
    old_retained = [
        row for row in authoritative_union if row.get("key") != detail01["key"]
    ]
    old_retained_sha = object_set_sha256(old_retained)
    old_quarantine_sha = object_set_sha256(old_quarantine)
    old_selection_sha = canonical_json_sha256(
        {
            "total": full_set_sha,
            "consumed": old_retained_sha,
            "quarantined": old_quarantine_sha,
            "receipt": receipt01_sha,
            "declaration": declaration01_sha,
        }
    )
    failed02_state_sha = require_hex(
        repair02.get("failed_state_sha256"), "attempt-02 failed state SHA-256"
    )
    failed02_resource_sha = require_hex(
        repair02.get("failed_resource_receipt_sha256"),
        "attempt-02 resource SHA-256",
    )
    failed02_scratch_sha = require_hex(
        repair02.get("failed_scratch_receipt_sha256"),
        "attempt-02 scratch receipt SHA-256",
    )
    failed02_input_sha = require_hex(
        repair02.get("failed_input_identity_sha256"),
        "attempt-02 input identity SHA-256",
    )
    failed02_state, _, _ = _validate_failed_attempt_files(
        run_dir,
        run_id,
        state_path=FAILED_RFQ_STATE_02,
        state_sha=failed02_state_sha,
        resource_path=FAILED_RFQ_RESOURCE_02,
        resource_sha=failed02_resource_sha,
        scratch_path=FAILED_RFQ_SCRATCH_RECEIPT_02,
        scratch_sha=failed02_scratch_sha,
        expected_label="rfq_full_stage_repair01",
        expected_key=str(detail02["key"]),
        expected_fingerprint=old_selection_sha,
        label="attempt-02",
    )
    failed02_identity = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_INPUT_IDENTITY_02,
            failed02_input_sha,
            "attempt-02 input identity",
        )
    )
    old_detail = dict(detail01)
    overlap_keys = [
        row["key"] for row in authoritative_union
        if len(row.get("bound_release_ids", [])) > 1
    ]
    if (
        failed02_identity.get("schema") != "rfq-full-input-identity-v2"
        or failed02_identity.get("run_id") != run_id
        or failed02_identity.get("release_ids")
        != [row["release_id"] for row in manifest.get("selected_releases", [])]
        or failed02_identity.get("releases") != failed01_identity.get("releases")
        or failed02_identity.get("logical_manifest_bindings_total") != logical_bindings
        or failed02_identity.get("unique_objects_total") != len(authoritative_union)
        or failed02_identity.get("unique_bytes_total")
        != sum(row["size"] for row in authoritative_union)
        or failed02_identity.get("deduplicated_overlapping_objects")
        != len(overlap_keys)
        or failed02_identity.get("manifest_object_set_sha256") != full_set_sha
        or failed02_identity.get("coverage_status") != RFQ_PARTIAL_STATUS
        or failed02_identity.get("full_object_coverage") is not False
        or failed02_identity.get("whole_object_quarantine") is not True
        or failed02_identity.get("line_salvage") is not False
        or failed02_identity.get("consumed_unique_objects") != len(old_retained)
        or failed02_identity.get("consumed_logical_bindings")
        != sum(len(row["bound_release_ids"]) for row in old_retained)
        or failed02_identity.get("consumed_bytes")
        != sum(row["size"] for row in old_retained)
        or failed02_identity.get("consumed_objects") != old_retained
        or failed02_identity.get("consumed_object_set_sha256") != old_retained_sha
        or failed02_identity.get("quarantined_unique_objects") != 1
        or failed02_identity.get("quarantined_logical_bindings") != 1
        or failed02_identity.get("quarantined_bytes") != detail01["size"]
        or failed02_identity.get("quarantined_object_set_sha256")
        != old_quarantine_sha
        or failed02_identity.get("quarantine_reasons") != [detail01["reason"]]
        or failed02_identity.get("quarantine_details") != [old_detail]
        or failed02_identity.get("selection_fingerprint_sha256") != old_selection_sha
        or failed02_identity.get("failed_attempt_binding") != base_failed_binding01
        or failed02_identity.get("cycle1_duckdb_binding") != cycle_binding
    ):
        raise MissionFinalizationError(
            "attempt-02 input identity is not exactly repair-01's retained set"
        )
    if repair02.get("failed_input_fingerprint") != old_selection_sha:
        raise MissionFinalizationError("attempt-02 registered fingerprint mismatch")

    expected_failed02_record = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02",
        "state_active_path": ACTIVE_RFQ_STATE.as_posix(),
        "state_path": FAILED_RFQ_STATE_02.as_posix(),
        "state_sha256": failed02_state_sha,
        "state_status": "FAILED_RESUMABLE",
        "resource_active_path": ACTIVE_RFQ_REPAIR_RESOURCE.as_posix(),
        "resource_path": FAILED_RFQ_RESOURCE_02.as_posix(),
        "resource_sha256": failed02_resource_sha,
        "resource_label": "rfq_full_stage_repair01",
        "return_code": 1,
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json"
        ),
        "scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_02.as_posix(),
        "scratch_receipt_sha256": failed02_scratch_sha,
        "input_identity_active_path": RFQ_INPUT_IDENTITY.as_posix(),
        "input_identity_path": FAILED_RFQ_INPUT_IDENTITY_02.as_posix(),
        "input_identity_sha256": failed02_input_sha,
        "input_fingerprint": old_selection_sha,
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    }
    expected_structural_record = {
        "path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        "sha256": audit_sha,
        "resource_path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_RESOURCE_02.as_posix(),
        "resource_sha256": audit_resource_sha,
        "objects_scanned": EXPECTED_RFQ_FULL_OBJECTS,
        "bytes_scanned": EXPECTED_RFQ_FULL_BYTES,
        "lines_scanned": EXPECTED_RFQ_FULL_LINES,
        "invalid_object_count": 2,
        "invalid_line_count": 2,
        "identity_mismatch_count": 0,
        "analysis_result_opened": False,
    }
    if (
        repair02.get("failed_attempt") != expected_failed02_record
        or repair02.get("structural_audit") != expected_structural_record
        or repair02.get("blocker_evidence")
        != {
            "path": ARCHIVED_RFQ_SECOND_BLOCKER.as_posix(),
            "sha256": blocker_sha,
            "status": "DATA_INTEGRITY_BLOCKER",
        }
        or repair02.get("authorization_evidence")
        != {
            "path": REPAIR_AUTHORIZATION_02.as_posix(),
            "sha256": authorization_sha,
            "schema_version": "sports-autoresearch-repair-authorization-v1",
            "authority_source": "operator_chat_message",
        }
    ):
        raise MissionFinalizationError("repair-02 nested evidence binding mismatch")

    coverage_record = repair02.get("coverage")
    selection_sha = canonical_json_sha256(
        {
            "schema": "rfq-partial-object-selection-v2",
            "total": full_set_sha,
            "retained": retained_sha,
            "quarantined": quarantine_sha,
            "declaration": declaration02_sha,
            "receipts": [receipt01_sha, receipt02_sha],
        }
    )
    expected_coverage_record = {
        "status": RFQ_PARTIAL_STATUS,
        "full_unique_objects": EXPECTED_RFQ_FULL_OBJECTS,
        "full_logical_manifest_bindings": EXPECTED_RFQ_FULL_LOGICAL_BINDINGS,
        "full_unique_bytes": EXPECTED_RFQ_FULL_BYTES,
        "retained_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "retained_logical_manifest_bindings": EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS,
        "retained_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "quarantined_logical_manifest_bindings": (
            EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS
        ),
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "full_object_set_sha256": full_set_sha,
        "retained_object_set_sha256": retained_sha,
        "quarantined_object_set_sha256": quarantine_sha,
        "retained_selection_fingerprint_sha256": selection_sha,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    if coverage_record != expected_coverage_record:
        raise MissionFinalizationError("repair-02 registered coverage receipt mismatch")
    if selection_sha != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256:
        raise MissionFinalizationError("repair-02 production selection fingerprint mismatch")

    session_evidence, session_evidence_sha = _require_active_archive_equal(
        run_dir,
        SESSION_RESUME_02,
        ARCHIVED_SESSION_RESUME_02,
        repair02.get("session_resume_evidence_sha256"),
        "repair-02 session-resume evidence",
    )
    session_records = manifest.get("session_resumes")
    session_record = repair02.get("session_resume")
    if (
        not isinstance(session_records, list)
        or len(session_records) != 1
        or not isinstance(session_records[0], dict)
        or not isinstance(session_record, dict)
        or set(session_record)
        != {"manifest_record", "evidence_path", "evidence_sha256", "preserved_in_run_manifest"}
        or session_record.get("manifest_record") != session_records[0]
        or session_record.get("evidence_path") != ARCHIVED_SESSION_RESUME_02.as_posix()
        or session_record.get("evidence_sha256") != session_evidence_sha
        or session_record.get("preserved_in_run_manifest") is not True
        or session_records[0].get("evidence_path") != SESSION_RESUME_02.as_posix()
        or session_records[0].get("evidence_sha256") != session_evidence_sha
        or session_evidence.get("schema_version")
        != "sports-autoresearch-session-resume-v1"
        or session_evidence.get("run_id") != run_id
        or session_evidence.get("session_id") != session_records[0].get("session_id")
        or session_evidence.get("resumed_at_utc")
        != session_records[0].get("resumed_at_utc")
    ):
        raise MissionFinalizationError("repair-02 session-resume chain mismatch")
    _require_utc_timestamp(session_records[0].get("resumed_at_utc"), "session resume")
    verified = session_evidence.get("verified_identities")
    if (
        not isinstance(verified, dict)
        or verified.get("mission_sha256") != EXPECTED_MISSION_SHA
        or verified.get("canonical_prompt_sha256")
        != (manifest.get("canonical_prompt") or {}).get("sha256")
        or verified.get("repository_execution_commit") != middle_identity["execution_commit"]
        or verified.get("source_manifest_sha256") != middle_identity["source_manifest_sha256"]
        or verified.get("source_sha256s_sha256") != middle_identity["source_sha256s_sha256"]
        or verified.get("query_set_sha256") != middle_identity["query_set_sha256"]
        or verified.get("repair01_registration_sha256") != repair01_receipt_sha
        or verified.get("active_failed_rfq_input_identity_sha256") != failed02_input_sha
        or verified.get("structural_audit_sha256") != audit_sha
        or verified.get("trial_registry_sha256")
        != repair01.get("trial_registry", {}).get("current_sha256")
        or verified.get("trial_registry_bytes")
        != repair01.get("trial_registry", {}).get("current_bytes")
        or verified.get("remote_source_checksum_verification") != "PASS"
        or verified.get("remote_query_checksum_verification") != "PASS"
    ):
        raise MissionFinalizationError("session-resume verified identity mismatch")

    registry = checked_relative_path(
        run_dir,
        (active_boundary_root / "TRIAL_REGISTRY.jsonl").as_posix(),
        "repair-02 trial registry",
    ).read_bytes()
    archived_registry01 = (
        run_dir
        / "DATA_INTEGRITY/repairs/repair-01/pre_repair/TRIAL_REGISTRY.jsonl"
    ).read_bytes()
    archived_registry02 = (run_dir / REPAIR_02_PRE_ROOT / "TRIAL_REGISTRY.jsonl").read_bytes()
    trial01 = repair01.get("trial_registry")
    trial02 = repair02.get("trial_registry")
    if not isinstance(trial01, dict) or not isinstance(trial02, dict):
        raise MissionFinalizationError("repair trial-registry bindings are missing")
    trial01_previous = require_positive_int(
        trial01.get("previous_bytes"), "repair-01 prior trial bytes"
    )
    trial01_current = require_positive_int(
        trial01.get("current_bytes"), "repair-01 current trial bytes"
    )
    trial02_previous = require_positive_int(
        trial02.get("previous_bytes"), "repair-02 prior trial bytes"
    )
    trial02_current = require_positive_int(
        trial02.get("current_bytes"), "repair-02 current trial bytes"
    )
    if (
        trial01.get("strict_previous_bytes_prefix") is not True
        or trial01.get("appended_records") != 2
        or trial01.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"]
        or trial02.get("strict_previous_bytes_prefix") is not True
        or trial02.get("appended_records") != 2
        or trial02.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02"]
        or trial01_current != trial02_previous
        or trial01_previous >= trial01_current
        or trial02_previous >= trial02_current
        or archived_registry01 != registry[:trial01_previous]
        or archived_registry02 != registry[:trial01_current]
        or len(archived_registry01) != trial01_previous
        or len(archived_registry02) != trial02_previous
        or len(registry) < trial02_current
        or hashlib.sha256(archived_registry01).hexdigest()
        != trial01.get("previous_sha256")
        or hashlib.sha256(archived_registry02).hexdigest()
        != trial01.get("current_sha256")
        or trial02.get("previous_sha256") != trial01.get("current_sha256")
        or hashlib.sha256(registry[:trial02_current]).hexdigest()
        != trial02.get("current_sha256")
    ):
        raise MissionFinalizationError("repair-01/repair-02 trial append chain mismatch")

    def parse_trial_append(payload: bytes, start: int, end: int, label: str) -> list[dict[str, Any]]:
        try:
            rows = [
                json.loads(line)
                for line in payload[start:end].decode("utf-8").splitlines()
                if line
            ]
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MissionFinalizationError(f"invalid {label} trial append: {exc}") from exc
        if len(rows) != 2 or any(not isinstance(row, dict) for row in rows):
            raise MissionFinalizationError(f"{label} must append exactly two records")
        return rows

    trial01_rows = parse_trial_append(
        registry, trial01_previous, trial01_current, "repair-01"
    )
    trial02_rows = parse_trial_append(
        registry, trial02_previous, trial02_current, "repair-02"
    )
    for rows, expected_ids, parent in (
        (
            trial01_rows,
            ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"],
            None,
        ),
        (
            trial02_rows,
            ["RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02"],
            "repair-01",
        ),
    ):
        if [row.get("trial_registration_id") for row in rows] != expected_ids:
            raise MissionFinalizationError("repair trial registration order mismatch")
        for row in rows:
            if (
                row.get("trial_ids") != list(RFQ_TRIAL_ORDER)
                or row.get("result_opened") is not False
                or row.get("hypothesis_conclusion_opened") is not False
                or (parent is not None and row.get("parent_repair_id") != parent)
            ):
                raise MissionFinalizationError("repair trial governance boundary mismatch")

    failure01, prereg01 = trial01_rows
    failure02, prereg02 = trial02_rows
    expected_failure01 = {
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE",
        "failure_class": "MALFORMED_NDJSON_OBJECT",
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt01_sha,
        "failure_state_path": FAILED_RFQ_STATE.as_posix(),
        "failure_state_sha256": failed01_state_sha,
        "failure_resource_path": FAILED_RFQ_RESOURCE.as_posix(),
        "failure_resource_sha256": failed01_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed01_scratch_sha,
        "execution_commit": initial_identity["execution_commit"],
        "source_manifest_sha256": initial_identity["source_manifest_sha256"],
        "query_set_sha256": initial_identity["query_set_sha256"],
    }
    expected_prereg01 = {
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR_PREREGISTRATION",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "declaration_path": REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": declaration01_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": receipt01_sha,
        "previous_execution_commit": initial_identity["execution_commit"],
        "current_execution_commit": middle_identity["execution_commit"],
        "previous_source_manifest_sha256": initial_identity["source_manifest_sha256"],
        "current_source_manifest_sha256": middle_identity["source_manifest_sha256"],
        "previous_query_set_sha256": initial_identity["query_set_sha256"],
        "current_query_set_sha256": middle_identity["query_set_sha256"],
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "core_results_recomputed": False,
    }
    expected_failure02 = {
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR01",
        "failure_class": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "failed_state_path": FAILED_RFQ_STATE_02.as_posix(),
        "failed_state_sha256": failed02_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_02.as_posix(),
        "failed_resource_receipt_sha256": failed02_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_02.as_posix(),
        "failed_scratch_receipt_sha256": failed02_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_02.as_posix(),
        "failed_input_identity_sha256": failed02_input_sha,
        "failed_input_fingerprint": old_selection_sha,
        "structural_audit_path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        "structural_audit_sha256": audit_sha,
        "execution_commit": middle_identity["execution_commit"],
        "source_manifest_sha256": middle_identity["source_manifest_sha256"],
        "source_sha256s_sha256": middle_identity["source_sha256s_sha256"],
        "query_set_sha256": middle_identity["query_set_sha256"],
    }
    expected_prereg02 = {
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR02_PREREGISTRATION",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
        "declaration_path": REPAIR_DECLARATION_02.as_posix(),
        "declaration_sha256": declaration02_sha,
        "receipt_path": REPAIR_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": receipt02_sha,
        "authorization_evidence_path": REPAIR_AUTHORIZATION_02.as_posix(),
        "authorization_evidence_sha256": authorization_sha,
        "structural_audit_path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        "structural_audit_sha256": audit_sha,
        "newly_quarantined_objects": repair02.get("newly_quarantined_objects"),
        "cumulative_quarantined_objects": repair02.get("cumulative_quarantined_objects"),
        "coverage": expected_coverage_record,
        "previous_execution_commit": middle_identity["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": middle_identity["source_manifest_sha256"],
        "previous_source_sha256s_sha256": middle_identity["source_sha256s_sha256"],
        "current_source_manifest_sha256": current_identity["source_manifest_sha256"],
        "current_source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "previous_query_set_sha256": middle_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    for row, expected, label in (
        (failure01, expected_failure01, "repair-01 failure trial"),
        (prereg01, expected_prereg01, "repair-01 preregistration trial"),
        (failure02, expected_failure02, "repair-02 failure trial"),
        (prereg02, expected_prereg02, "repair-02 preregistration trial"),
    ):
        for field, value in expected.items():
            if row.get(field) != value:
                raise MissionFinalizationError(f"{label} mismatch: {field}")

    failed_binding01 = {
        **base_failed_binding01,
        "attempt_id": "RFQ_FULL_STAGE_ATTEMPT_01",
        "input_fingerprint": full_set_sha,
        "resource_label": "rfq_full_stage",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    }
    failed_binding02 = {
        "repair_id": "repair-02",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02",
        "failed_state_path": FAILED_RFQ_STATE_02.as_posix(),
        "failed_state_sha256": failed02_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_02.as_posix(),
        "failed_resource_receipt_sha256": failed02_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_02.as_posix(),
        "failed_scratch_receipt_sha256": failed02_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_02.as_posix(),
        "failed_input_identity_sha256": failed02_input_sha,
        "input_fingerprint": old_selection_sha,
        "resource_label": "rfq_full_stage_repair01",
        "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
    }
    failed_bindings = [failed_binding01, failed_binding02]
    expected_repair_chain = [
        {
            "repair_id": "repair-01",
            "registration_path": REPAIR_REGISTRATION_RECEIPT.as_posix(),
            "registration_sha256": repair01_receipt_sha,
            "declaration_path": REPAIR_DECLARATION.as_posix(),
            "declaration_sha256": declaration01_sha,
            "receipt_path": REPAIR_MALFORMED_RECEIPT.as_posix(),
            "receipt_sha256": receipt01_sha,
        },
        {
            "repair_id": "repair-02",
            "registration_path": REPAIR_REGISTRATION_RECEIPT_02.as_posix(),
            "registration_sha256": repair02_receipt_sha,
            "declaration_path": REPAIR_DECLARATION_02.as_posix(),
            "declaration_sha256": declaration02_sha,
            "receipt_path": REPAIR_MALFORMED_RECEIPT_02.as_posix(),
            "receipt_sha256": receipt02_sha,
            "authorization_path": REPAIR_AUTHORIZATION_02.as_posix(),
            "authorization_sha256": authorization_sha,
            "structural_audit_path": ARCHIVED_RFQ_STRUCTURAL_AUDIT_02.as_posix(),
            "structural_audit_sha256": audit_sha,
            "structural_audit_resource_path": (
                ARCHIVED_RFQ_STRUCTURAL_AUDIT_RESOURCE_02.as_posix()
            ),
            "structural_audit_resource_sha256": audit_resource_sha,
            "blocker_evidence_path": ARCHIVED_RFQ_SECOND_BLOCKER.as_posix(),
            "blocker_evidence_sha256": blocker_sha,
            "session_resume_evidence_path": ARCHIVED_SESSION_RESUME_02.as_posix(),
            "session_resume_evidence_sha256": session_evidence_sha,
        },
    ]
    expected_success_resource = {
        "label": "rfq_full_stage_repair02",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
    }

    message_pattern = re.compile(
        r"raw_rfq/date=(\d{4}-\d{2}-\d{2})/rfq_(\d{2})\.ndjson(?:\.(\d+))?"
    )

    def message_order(row: Mapping[str, Any]) -> tuple[str, int, int, str] | None:
        key = row.get("key")
        match = message_pattern.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            return None
        return match.group(1), int(match.group(2)), int(match.group(3) or 0), key

    ordered_messages = sorted(
        [row for row in authoritative_union if message_order(row) is not None],
        key=lambda row: message_order(row),
    )
    ordered_keys = [row["key"] for row in ordered_messages]
    quarantine_keys = sorted(row["key"] for row in details)
    try:
        quarantine_indices = sorted(ordered_keys.index(key) for key in quarantine_keys)
    except ValueError as exc:
        raise MissionFinalizationError("quarantined object is not an RFQ message shard") from exc
    if (
        quarantine_indices != list(
            range(quarantine_indices[0], quarantine_indices[-1] + 1)
        )
        or quarantine_indices[0] == 0
        or quarantine_indices[-1] + 1 >= len(ordered_messages)
    ):
        raise MissionFinalizationError(
            "the two quarantined objects are not one retained-neighbor-bounded contiguous gap"
        )
    previous_retained = ordered_messages[quarantine_indices[0] - 1]
    next_retained = ordered_messages[quarantine_indices[-1] + 1]
    if (
        previous_retained["key"] in quarantine_keys
        or next_retained["key"] in quarantine_keys
        or len({row.get("release_id") for row in details}) != 1
    ):
        raise MissionFinalizationError(
            "quarantine objects do not form one retained-neighbor-bounded release gap"
        )
    anchor_key = quarantine_keys[0]
    anchor_detail = next(row for row in details if row["key"] == anchor_key)
    expected_gap_plan = {
        "quarantined_object_count": 2,
        "contiguous_gap_count": 1,
        "observation_boundary_count": 2,
        "contiguous_runs": [
            {
                "release_id": anchor_detail["release_id"],
                "anchor_key": anchor_key,
                "quarantined_keys": quarantine_keys,
                "quarantined_object_set_sha256": quarantine_sha,
                "previous_retained_key": previous_retained["key"],
                "next_retained_key": next_retained["key"],
            }
        ],
    }

    if prefix_only:
        # repair-03 freezes the complete repair-02 boundary before changing any
        # executable source.  Returning this context only after all repair-01/02
        # receipts, journals, archives, identities and trial prefixes above have
        # been independently revalidated lets the repair-03 validator extend the
        # chain without weakening the existing two-repair finalization path.
        return {
            "prefix_only": True,
            "run_id": run_id,
            "selected_releases": [
                dict(row) for row in manifest.get("selected_releases", [])
            ],
            "repair01": dict(repair01),
            "repair02": dict(repair02),
            "initial_identity": dict(initial_identity),
            "middle_identity": dict(middle_identity),
            "current_identity": dict(current_identity),
            "authoritative_union": [dict(row) for row in authoritative_union],
            "release_input_receipts": list(failed01_identity.get("releases", [])),
            "overlap_keys": list(overlap_keys),
            "retained_objects": [dict(row) for row in retained],
            "quarantine_details": [dict(row) for row in details],
            "full_set_sha256": full_set_sha,
            "retained_set_sha256": retained_sha,
            "quarantined_set_sha256": quarantine_sha,
            "selection_fingerprint_sha256": selection_sha,
            "coverage": expected_coverage_record,
            "repair_chain": expected_repair_chain,
            "failed_attempt_bindings": failed_bindings,
            "quarantine_gap_plan": expected_gap_plan,
            "cycle1_duckdb_binding": cycle_binding,
            "repair01_receipt_sha256": repair01_receipt_sha,
            "repair02_receipt_sha256": repair02_receipt_sha,
            "repair02_trial_registry_sha256": trial02.get("current_sha256"),
            "repair02_trial_registry_bytes": trial02_current,
            "audit02_sha256": audit_sha,
            "audit02_resource_sha256": audit_resource_sha,
            "authorization02_sha256": authorization_sha,
            "blocker02_sha256": blocker_sha,
            "session_resume02_sha256": session_evidence_sha,
        }

    rfq_input = rfq.get("input")
    if not isinstance(rfq_input, dict):
        raise MissionFinalizationError("repair-02 RFQ summary input is missing")
    expected_release_ids = [
        row["release_id"] for row in manifest.get("selected_releases", [])
    ]
    summary_input_expected = {
        "release_ids": expected_release_ids,
        "coverage_status": RFQ_PARTIAL_STATUS,
        "full_object_coverage": False,
        "whole_object_quarantine": True,
        "line_salvage": False,
        "logical_manifest_bindings_total": EXPECTED_RFQ_FULL_LOGICAL_BINDINGS,
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "consumed_logical_bindings": EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "consumed_object_set_sha256": retained_sha,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "quarantined_logical_bindings": EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "quarantined_object_set_sha256": quarantine_sha,
        "quarantine_reasons": [row["reason"] for row in details],
        "quarantine_details": list(details),
        "quarantine_gap_plan": expected_gap_plan,
        "failed_attempt_binding": None,
        "failed_attempt_bindings": failed_bindings,
        "repair_chain": expected_repair_chain,
        "expected_success_resource": expected_success_resource,
        "cycle1_duckdb_binding": cycle_binding,
        "deduplicated_overlapping_objects": len(overlap_keys),
        "overlap_keys": overlap_keys,
        "manifest_object_set_sha256": full_set_sha,
        "selection_fingerprint_sha256": selection_sha,
        "outer_parser": (
            "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort"
        ),
    }
    active_identity = load_json(run_dir / RFQ_INPUT_IDENTITY)
    expected_active_identity = {
        "schema": "rfq-full-input-identity-v2",
        "run_id": run_id,
        "release_ids": expected_release_ids,
        "releases": failed01_identity.get("releases"),
        **{
            field: value
            for field, value in summary_input_expected.items()
            if field not in {"outer_parser", "overlap_keys"}
        },
        "consumed_objects": list(retained),
    }
    coverage = rfq.get("coverage")
    counts = rfq.get("counts")
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise MissionFinalizationError("repair-02 RFQ coverage/count receipts are missing")
    gap_ranges = coverage.get("quarantine_gap_ranges")
    if not isinstance(gap_ranges, list) or len(gap_ranges) != 1:
        raise MissionFinalizationError("two quarantines must produce exactly one gap")
    gap = gap_ranges[0]
    if not isinstance(gap, dict):
        raise MissionFinalizationError("repair-02 RFQ gap receipt is invalid")
    gap_start_ns = require_positive_int(gap.get("gap_start_ns"), "RFQ gap start ns")
    gap_end_ns = require_positive_int(gap.get("gap_end_ns"), "RFQ gap end ns")
    expected_gap = {
        "release_id": anchor_detail["release_id"],
        "key": anchor_key,
        "sha256": anchor_detail["sha256"],
        "quarantined_keys_json": json.dumps(
            quarantine_keys, sort_keys=True, separators=(",", ":")
        ),
        "quarantined_object_count": 2,
        "quarantined_object_set_sha256": quarantine_sha,
        "gap_start_ns": gap_start_ns,
        "gap_end_ns": gap_end_ns,
        "gap_start_us": gap_start_ns // 1000,
        "gap_end_us": (gap_end_ns + 999) // 1000,
        "boundary_reason": "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON",
    }
    if gap != expected_gap or gap_end_ns <= gap_start_ns:
        raise MissionFinalizationError("repair-02 contiguous gap boundary mismatch")
    gap_set_sha = canonical_json_sha256(gap_ranges)
    _validate_rfq_quarantine_boundary_counts(coverage, counts)
    if (
        coverage.get("rfq_object_coverage") != RFQ_PARTIAL_STATUS
        or coverage.get("quarantine_gap_set_sha256") != gap_set_sha
        or coverage.get("quarantined_hours_are_not_observed_zero") is not True
        or coverage.get("capture_completeness_is_not_lifecycle_join_completeness")
        is not True
    ):
        raise MissionFinalizationError("repair-02 one-gap/two-boundary semantics mismatch")
    partial_hours = require_positive_int(
        coverage.get("partial_object_coverage_hours"),
        "RFQ partial-object coverage hours",
    )

    gap_csv = _read_csv_rows(
        run_dir / RFQ_QUARANTINE_GAPS,
        {
            "release_id",
            "key",
            "sha256",
            "quarantined_keys_json",
            "quarantined_object_count",
            "quarantined_object_set_sha256",
            "previous_filename",
            "next_filename",
            "gap_start_ns",
            "gap_end_ns",
            "gap_start_us",
            "gap_end_us",
            "boundary_reason",
        },
        "repair-02 RFQ quarantine gaps",
    )
    if len(gap_csv) != 1:
        raise MissionFinalizationError("repair-02 gap table must contain one row")
    csv_gap = gap_csv[0]
    for field, value in expected_gap.items():
        observed: Any = csv_gap.get(field)
        if isinstance(value, int):
            try:
                observed = int(observed or "")
            except ValueError as exc:
                raise MissionFinalizationError(f"invalid gap-table integer: {field}") from exc
        if observed != value:
            raise MissionFinalizationError(f"repair-02 gap-table mismatch: {field}")
    previous_filename = csv_gap.get("previous_filename", "")
    next_filename = csv_gap.get("next_filename", "")
    if (
        not previous_filename.endswith(previous_retained["key"])
        or not next_filename.endswith(next_retained["key"])
        or previous_filename.endswith(anchor_key)
        or next_filename.endswith(anchor_key)
    ):
        raise MissionFinalizationError(
            "gap table does not bind the immediate retained message-shard neighbors"
        )
    hour_rows = _read_csv_rows(
        run_dir / RFQ_HOUR_COVERAGE,
        {"object_coverage_status", "zero_interpretation"},
        "RFQ hour coverage",
    )
    partial_rows = [
        row for row in hour_rows
        if row.get("object_coverage_status") == RFQ_PARTIAL_STATUS
    ]
    if len(partial_rows) != partial_hours or any(
        row.get("zero_interpretation")
        != "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP"
        for row in partial_rows
    ):
        raise MissionFinalizationError("repair-02 partial-hour coverage semantics mismatch")

    active_state_path = checked_relative_path(
        run_dir, ACTIVE_RFQ_STATE.as_posix(), "repair-02 completed RFQ state"
    )
    active_state = load_json(active_state_path)
    _validate_repair02_success_envelope(
        rfq_input=rfq_input,
        active_identity=active_identity,
        active_state=active_state,
        summary_expected=summary_input_expected,
        identity_expected=expected_active_identity,
        run_id=run_id,
        selection_sha=selection_sha,
        repair_chain=expected_repair_chain,
        failed_bindings=failed_bindings,
        gap_plan=expected_gap_plan,
        expected_resource=expected_success_resource,
    )
    active_state_sha = sha256(active_state_path)

    success_resource_path = checked_relative_path(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
        "repair-02 successful RFQ resource",
    )
    success_resource = load_json(success_resource_path)
    success_command = success_resource.get("command")
    if (
        success_resource.get("schema_version") != "w09-stage-resource-v1"
        or success_resource.get("label") != "rfq_full_stage_repair02"
        or success_resource.get("return_code") != 0
        or not isinstance(success_command, list)
        or not success_command
        or "--resume" in success_command
    ):
        raise MissionFinalizationError("repair-02 successful resource receipt mismatch")
    source_arguments = [
        value
        for value in success_command
        if isinstance(value, str) and value.endswith("/source/rfq_full_stage.py")
    ]
    try:
        run_index = success_command.index("--run-dir")
        command_run_dir = success_command[run_index + 1]
    except (ValueError, IndexError):
        command_run_dir = None
    if (
        len(source_arguments) != 1
        or not Path(source_arguments[0]).is_absolute()
        or not isinstance(command_run_dir, str)
        or Path(command_run_dir).name != run_id
    ):
        raise MissionFinalizationError("repair-02 resource command identity mismatch")
    active_source = checked_relative_path(
        run_dir, "source/rfq_full_stage.py", "repair-02 active RFQ source"
    )
    frozen_source = checked_relative_path(
        run_dir, "queries/rfq_full_stage.py", "repair-02 frozen RFQ source"
    )
    if sha256(active_source) != sha256(frozen_source):
        raise MissionFinalizationError("repair-02 executed/frozen RFQ source mismatch")
    success_resource_sha = sha256(success_resource_path)

    return {
        "status": RFQ_PARTIAL_STATUS,
        "analysis_scope": RFQ_ANALYSIS_SCOPE,
        "retained_population": "RETAINED_OBSERVED_SUBSET",
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_objects": list(details),
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "manifest_object_set_sha256": full_set_sha,
        "consumed_object_set_sha256": retained_sha,
        "quarantined_object_set_sha256": quarantine_sha,
        "selection_fingerprint_sha256": selection_sha,
        "quarantine_gap_plan": expected_gap_plan,
        "quarantine_gap_set_sha256": gap_set_sha,
        "quarantine_gap": gap,
        "partial_object_coverage_hours": partial_hours,
        "initial_execution_commit": initial_identity["execution_commit"],
        "previous_execution_commit": middle_identity["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "initial_source_manifest_sha256": initial_identity["source_manifest_sha256"],
        "previous_source_manifest_sha256": middle_identity["source_manifest_sha256"],
        "current_source_manifest_sha256": current_identity["source_manifest_sha256"],
        "initial_source_sha256s_sha256": initial_identity["source_sha256s_sha256"],
        "previous_source_sha256s_sha256": middle_identity["source_sha256s_sha256"],
        "current_source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "initial_query_set_sha256": initial_identity["query_set_sha256"],
        "previous_query_set_sha256": middle_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "repair_chain": expected_repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "declaration_path": RFQ_DECLARATION_02.as_posix(),
        "declaration_sha256": declaration02_sha,
        "receipt_path": RFQ_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": receipt02_sha,
        "authorization_path": RFQ_REPAIR_02_AUTHORIZATION.as_posix(),
        "authorization_sha256": authorization_sha,
        "repair_receipt_path": REPAIR_REGISTRATION_RECEIPT_02.as_posix(),
        "repair_receipt_sha256": repair02_receipt_sha,
        "failed_state_path": FAILED_RFQ_STATE_02.as_posix(),
        "failed_state_sha256": failed02_state_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_02.as_posix(),
        "failed_resource_receipt_sha256": failed02_resource_sha,
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_02.as_posix(),
        "failed_scratch_receipt_sha256": failed02_scratch_sha,
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_02.as_posix(),
        "failed_input_identity_sha256": failed02_input_sha,
        "structural_audit_path": RFQ_STRUCTURAL_AUDIT_02.as_posix(),
        "structural_audit_sha256": audit_sha,
        "blocker_evidence_path": RFQ_SECOND_BLOCKER.as_posix(),
        "blocker_evidence_sha256": blocker_sha,
        "session_resume_evidence_path": SESSION_RESUME_02.as_posix(),
        "session_resume_evidence_sha256": session_evidence_sha,
        "cycle1_duckdb_binding": cycle_binding,
        "completed_state_path": ACTIVE_RFQ_STATE.as_posix(),
        "completed_state_sha256": active_state_sha,
        "successful_resource_receipt_path": ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
        "successful_resource_receipt_sha256": success_resource_sha,
        "claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }

def _validate_repair03_selection_identity(
    document: Mapping[str, Any],
    *,
    prefix: Mapping[str, Any],
    run_id: str,
    require_consumed_objects: bool,
) -> None:
    """Prove repair-03 preserved repair-02's exact 282-object population."""
    retained = prefix["retained_objects"]
    details = prefix["quarantine_details"]
    expected = {
        "release_ids": [
            row["release_id"] for row in prefix.get("selected_releases", [])
        ],
        "coverage_status": RFQ_PARTIAL_STATUS,
        "full_object_coverage": False,
        "whole_object_quarantine": True,
        "line_salvage": False,
        "logical_manifest_bindings_total": EXPECTED_RFQ_FULL_LOGICAL_BINDINGS,
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "consumed_logical_bindings": EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "consumed_object_set_sha256": prefix["retained_set_sha256"],
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "quarantined_logical_bindings": EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "quarantined_object_set_sha256": prefix["quarantined_set_sha256"],
        "quarantine_reasons": [row["reason"] for row in details],
        "quarantine_details": list(details),
        "quarantine_gap_plan": prefix["quarantine_gap_plan"],
        "cycle1_duckdb_binding": prefix["cycle1_duckdb_binding"],
        "deduplicated_overlapping_objects": len(prefix["overlap_keys"]),
        "manifest_object_set_sha256": prefix["full_set_sha256"],
        "selection_fingerprint_sha256": prefix["selection_fingerprint_sha256"],
    }
    if document.get("run_id") != run_id:
        raise MissionFinalizationError("repair-03 RFQ input run identity mismatch")
    for field, value in expected.items():
        if document.get(field) != value:
            raise MissionFinalizationError(
                f"repair-03 changed the retained RFQ selection: {field}"
            )
    if require_consumed_objects and document.get("consumed_objects") != retained:
        raise MissionFinalizationError("repair-03 consumed-object inventory changed")


def _validate_repair03_failed_evidence(
    run_dir: Path,
    *,
    run_id: str,
    repair03: Mapping[str, Any],
    prefix: Mapping[str, Any],
) -> dict[str, Any]:
    expected_evidence_paths = {
        "failed_state_path": FAILED_RFQ_STATE_03.as_posix(),
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
    }
    if any(
        repair03.get(field) != value
        for field, value in expected_evidence_paths.items()
    ):
        raise MissionFinalizationError("attempt-03 archived evidence path mismatch")
    state_sha = require_hex(
        repair03.get("failed_state_sha256"), "attempt-03 failed state SHA-256"
    )
    resource_sha = require_hex(
        repair03.get("failed_resource_receipt_sha256"),
        "attempt-03 resource SHA-256",
    )
    scratch_sha = require_hex(
        repair03.get("failed_scratch_receipt_sha256"),
        "attempt-03 scratch receipt SHA-256",
    )
    input_sha = require_hex(
        repair03.get("failed_input_identity_sha256"),
        "attempt-03 input identity SHA-256",
    )
    if (
        state_sha != EXPECTED_RFQ_FAILED_STATE_03_SHA256
        or resource_sha != EXPECTED_RFQ_FAILED_RESOURCE_03_SHA256
        or scratch_sha != EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_03_SHA256
        or input_sha != EXPECTED_RFQ_FAILED_INPUT_03_SHA256
    ):
        raise MissionFinalizationError(
            "attempt-03 evidence is not the approved failure boundary"
        )
    state = load_json(
        require_path_hash(
            run_dir, FAILED_RFQ_STATE_03, state_sha, "attempt-03 failed state"
        )
    )
    resource = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_RESOURCE_03,
            resource_sha,
            "attempt-03 resource receipt",
        )
    )
    scratch = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_SCRATCH_RECEIPT_03,
            scratch_sha,
            "attempt-03 scratch receipt",
        )
    )
    identity = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_INPUT_IDENTITY_03,
            input_sha,
            "attempt-03 input identity",
        )
    )
    expected_resource = {
        "label": "rfq_full_stage_repair02",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
    }
    expected_scratch = (
        f"/srv/w09-research/runs/{run_id}/cache/rfq_full_scratch.duckdb"
    )
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("run_id") != run_id
        or state.get("status")
        != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or state.get("resume") is not False
        or state.get("input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or state.get("next_required_authority")
        != "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or state.get("scratch") != expected_scratch
        or state.get("error")
        != "unexpected malformed inner RFQ payload in a consumed object; aborting"
        or state.get("error_type") != "RFQStageError"
        or state.get("expected_success_resource") != expected_resource
        or state.get("repair_chain") != prefix["repair_chain"]
        or state.get("failed_attempt_bindings")
        != prefix["failed_attempt_bindings"]
    ):
        raise MissionFinalizationError("attempt-03 parser failure state mismatch")
    command = resource.get("command")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair02"
        or resource.get("return_code") != 1
        or not isinstance(command, list)
        or "--resume" in command
    ):
        raise MissionFinalizationError("attempt-03 resource receipt mismatch")
    sources = [
        item for item in command
        if isinstance(item, str) and item.endswith("/source/rfq_full_stage.py")
    ]
    if len(sources) != 1 or not Path(sources[0]).is_absolute():
        raise MissionFinalizationError("attempt-03 resource source identity mismatch")
    if (
        set(scratch)
        != {
            "bytes",
            "disposition",
            "input_fingerprint",
            "mtime_utc",
            "original_scratch_path",
            "preserved_scratch_path",
            "resume_allowed",
            "run_id",
            "schema_version",
            "sha256",
        }
        or
        scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or scratch.get("run_id") != run_id
        or scratch.get("original_scratch_path") != expected_scratch
        or scratch.get("preserved_scratch_path")
        != str(Path(expected_scratch).with_name(
            "rfq_full_scratch.attempt03_failed.duckdb"
        ))
        or scratch.get("input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch.get("resume_allowed") is not False
        or scratch.get("sha256") != EXPECTED_RFQ_FAILED_SCRATCH_03_SHA256
        or scratch.get("bytes") != EXPECTED_RFQ_FAILED_SCRATCH_03_BYTES
        or scratch.get("mtime_utc") != EXPECTED_RFQ_FAILED_SCRATCH_03_MTIME
    ):
        raise MissionFinalizationError("attempt-03 scratch preservation mismatch")
    preserved_sha = require_hex(
        scratch.get("sha256"), "attempt-03 scratch SHA-256"
    )
    preserved_bytes = require_positive_int(
        scratch.get("bytes"), "attempt-03 scratch bytes"
    )
    _require_utc_timestamp(scratch.get("mtime_utc"), "attempt-03 scratch mtime")
    preserved_path = checked_relative_path(
        run_dir,
        "cache/rfq_full_scratch.attempt03_failed.duckdb",
        "attempt-03 preserved scratch",
    )
    if (
        preserved_path.stat().st_size != preserved_bytes
        or sha256(preserved_path) != preserved_sha
    ):
        raise MissionFinalizationError("attempt-03 preserved scratch identity mismatch")
    if (
        identity.get("schema") != "rfq-full-input-identity-v2"
        or identity.get("releases") != prefix["release_input_receipts"]
        or identity.get("repair_chain") != prefix["repair_chain"]
        or identity.get("failed_attempt_bindings")
        != prefix["failed_attempt_bindings"]
        or identity.get("expected_success_resource") != expected_resource
        or state.get("repair_chain") != identity.get("repair_chain")
        or state.get("failed_attempt_bindings")
        != identity.get("failed_attempt_bindings")
    ):
        raise MissionFinalizationError("attempt-03 state/input chain mismatch")
    prefix_with_releases = {
        **prefix,
        "selected_releases": prefix["selected_releases"],
    }
    _validate_repair03_selection_identity(
        identity,
        prefix=prefix_with_releases,
        run_id=run_id,
        require_consumed_objects=True,
    )
    return {
        "state": state,
        "resource": resource,
        "scratch": scratch,
        "input": identity,
        "state_sha256": state_sha,
        "resource_sha256": resource_sha,
        "scratch_sha256": scratch_sha,
        "input_sha256": input_sha,
        "preserved_scratch_sha256": preserved_sha,
        "preserved_scratch_bytes": preserved_bytes,
    }


def _validate_repair03_prefix_overlay(
    run_dir: Path,
    *,
    run_id: str,
    repair01: Mapping[str, Any],
    repair02: Mapping[str, Any],
    rfq: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Replay the strict repair-02 validator against repair-03's frozen boundary."""
    pre_manifest_path = checked_relative_path(
        run_dir,
        (REPAIR_03_PRE_ROOT / "RUN_MANIFEST.json").as_posix(),
        "repair-03 pre-registration manifest",
    )
    pre_manifest = load_json(pre_manifest_path)
    if (
        pre_manifest.get("run_id") != run_id
        or pre_manifest.get("status") != REPAIR02_PENDING_STATUS
        or pre_manifest.get("registration_state")
        != "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        or pre_manifest.get("data_integrity_repairs") != [repair01, repair02]
    ):
        raise MissionFinalizationError("repair-03 did not preserve the repair-02 manifest")
    prefix = validate_rfq_double_quarantine(
        run_dir,
        pre_manifest,
        rfq,
        prefix_only=True,
        active_boundary_root=REPAIR_03_PRE_ROOT,
    )
    if prefix.get("prefix_only") is not True:
        raise MissionFinalizationError("repair-03 synthetic prefix audit did not complete")
    return prefix, pre_manifest, pre_manifest_path


def validate_rfq_parser_repair03(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    *,
    prefix_only: bool = False,
    active_boundary_root: Path = Path("."),
) -> dict[str, Any]:
    """Validate repair-01 -> repair-02 -> code-only parser repair-03."""
    run_id = manifest.get("run_id")
    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(repairs, list)
        or len(repairs) != 3
        or any(not isinstance(row, dict) for row in repairs)
        or [row.get("repair_id") for row in repairs]
        != ["repair-01", "repair-02", "repair-03"]
    ):
        raise MissionFinalizationError(
            "repair-03 requires the ordered repair-01/repair-02/repair-03 chain"
        )
    repair01, repair02, repair03 = repairs
    if (
        repair03.get("schema_version")
        != "sports-autoresearch-rfq-parser-repair-v1"
        or repair03.get("parent_repair_id") != "repair-02"
        or repair03.get("pre_repair_status") != REPAIR02_PENDING_STATUS
        or repair03.get("post_repair_status") != REPAIR03_PENDING_STATUS
        or manifest.get("status")
        not in {REPAIR03_PENDING_STATUS, "COMPLETE"}
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_PARSER_CORRECTION_REPAIR03_BEFORE_RFQ_RESULT"
    ):
        raise MissionFinalizationError("repair-03 registration identity/status mismatch")

    # Synthetic overlay: immutable repair evidence remains rooted at run_dir,
    # while every mutable repair-02 boundary is resolved from repair-03/pre_repair.
    # The existing validator therefore replays the complete repair-01/02 audit
    # without pretending that failed attempt 03 was a successful repair-02 run.
    prefix, pre_manifest, pre_manifest_path = _validate_repair03_prefix_overlay(
        run_dir,
        run_id=run_id,
        repair01=repair01,
        repair02=repair02,
        rfq=rfq,
    )

    repair03_receipt_sha = _validate_repair_record_receipt(
        run_dir,
        repair03,
        REPAIR_REGISTRATION_RECEIPT_03,
        "repair-03 registration",
    )
    previous_record_payload = (
        json.dumps(repair02, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if (
        repair03.get("previous_repair_registration_path")
        != REPAIR_REGISTRATION_RECEIPT_02.as_posix()
        or repair03.get("previous_repair_registration_sha256")
        != prefix["repair02_receipt_sha256"]
        or repair03.get("previous_repair_record_sha256")
        != hashlib.sha256(previous_record_payload).hexdigest()
    ):
        raise MissionFinalizationError("repair-03 registration receipt chain mismatch")

    query_files = prefix["current_identity"].get("query_files")
    if not isinstance(query_files, list) or not query_files:
        raise MissionFinalizationError("repair-03 previous query-file set is missing")
    archive03_required = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *(str(value) for value in query_files),
        ACTIVE_RFQ_STATE.as_posix(),
        ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json",
        RFQ_INPUT_IDENTITY.as_posix(),
        "DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json",
        "logs/resources/rfq_inner_payload_contract_audit03_final.json",
        "tmp/rfq_inner_payload_contract_audit03.py",
        ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
    }
    source_mode = repair03.get("source_verification_mode")
    if source_mode == REPAIR_03_SNAPSHOT_SOURCE_MODE:
        archive03_required.add(REPAIR_03_SOURCE_ATTESTATION_ACTIVE.as_posix())
    elif source_mode != REPAIR_03_GIT_SOURCE_MODE:
        raise MissionFinalizationError("repair-03 source verification mode is invalid")
    _validate_archive_inventory(
        run_dir, repair03, REPAIR_03_PRE_ROOT, archive03_required, "repair-03"
    )

    failed = _validate_repair03_failed_evidence(
        run_dir,
        run_id=run_id,
        repair03=repair03,
        prefix=prefix,
    )
    archived_cycle03 = REPAIR_03_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING
    cycle03_sha = require_hex(
        repair03.get("cycle1_binding_sha256"),
        "repair-03 Cycle-1 binding SHA-256",
    )
    if repair03.get("cycle1_binding_path") != archived_cycle03.as_posix():
        raise MissionFinalizationError("repair-03 Cycle-1 binding path mismatch")
    active_cycle_path = require_path_hash(
        run_dir,
        active_boundary_root / ACTIVE_CYCLE1_DUCKDB_BINDING,
        cycle03_sha,
        "repair-03 active Cycle-1 binding",
    )
    archived_cycle_path = require_path_hash(
        run_dir,
        archived_cycle03,
        cycle03_sha,
        "repair-03 archived Cycle-1 binding",
    )
    if active_cycle_path.read_bytes() != archived_cycle_path.read_bytes():
        raise MissionFinalizationError("repair-03 Cycle-1 binding copies differ")
    audit_sha = require_hex(
        repair03.get("inner_payload_audit_sha256"),
        "repair-03 inner-payload audit SHA-256",
    )
    audit_resource_sha = require_hex(
        repair03.get("inner_payload_audit_resource_sha256"),
        "repair-03 audit resource SHA-256",
    )
    audit_source_sha = require_hex(
        repair03.get("inner_payload_audit_source_sha256"),
        "repair-03 audit source SHA-256",
    )
    if (
        audit_sha != EXPECTED_RFQ_INNER_AUDIT_03_SHA256
        or audit_resource_sha != EXPECTED_RFQ_INNER_AUDIT_RESOURCE_03_SHA256
        or audit_source_sha != EXPECTED_RFQ_INNER_AUDIT_SOURCE_03_SHA256
    ):
        raise MissionFinalizationError(
            "repair-03 audit evidence is not the approved boundary"
        )
    if (
        repair03.get("inner_payload_audit_path")
        != ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix()
        or repair03.get("inner_payload_audit_resource_path")
        != ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_RESOURCE_03.as_posix()
        or repair03.get("inner_payload_audit_source_path")
        != REPAIR_03_AUDIT_SOURCE.as_posix()
        or repair03.get("inner_payload_audit_source_archive_path")
        != ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_SOURCE_03.as_posix()
    ):
        raise MissionFinalizationError("repair-03 audit evidence paths mismatch")
    audit = load_json(
        require_path_hash(
            run_dir,
            ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03,
            audit_sha,
            "repair-03 inner-payload audit",
        )
    )
    audit_resource = load_json(
        require_path_hash(
            run_dir,
            ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_RESOURCE_03,
            audit_resource_sha,
            "repair-03 audit resource",
        )
    )
    canonical_audit_source = require_path_hash(
        run_dir,
        REPAIR_03_AUDIT_SOURCE,
        audit_source_sha,
        "repair-03 canonical audit source",
    )
    archived_audit_source = require_path_hash(
        run_dir,
        ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_SOURCE_03,
        audit_source_sha,
        "repair-03 archived audit source",
    )
    if canonical_audit_source.read_bytes() != archived_audit_source.read_bytes():
        raise MissionFinalizationError("repair-03 audit source copies differ")
    _validate_repair03_inner_payload_audit(
        audit,
        run_id=run_id,
        input_identity_sha256=failed["input_sha256"],
        audit_source_sha256=audit_source_sha,
        expected_objects=prefix["retained_objects"],
    )
    _validate_repair03_audit_resource(audit_resource, run_id=run_id)
    expected_inner_payload_audit = {
        "path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "sha256": audit_sha,
        "resource_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_RESOURCE_03.as_posix(),
        "resource_sha256": audit_resource_sha,
        "source_path": REPAIR_03_AUDIT_SOURCE.as_posix(),
        "source_sha256": audit_source_sha,
        "objects_scanned": EXPECTED_RFQ_RETAINED_OBJECTS,
        "bytes_scanned": EXPECTED_RFQ_RETAINED_BYTES,
        "lines_scanned": EXPECTED_RFQ_RETAINED_LINES,
        "data_frame_rows": 58_143_241,
        "segment_receipt_rows": 560,
        "expected_blank_control_marker_rows": EXPECTED_RFQ_EMPTY_CONTROL_ROWS,
        "expected_blank_control_marker_counts": dict(
            EXPECTED_RFQ_CONTROL_MARKER_COUNTS
        ),
        "outer_invalid_object_count": 0,
        "outer_invalid_line_count": 0,
        "identity_mismatch_count": 0,
        "unexpected_inner_payload_object_count": 0,
        "unexpected_inner_payload_row_count": 0,
        "audited_parser_contract_sha256": audit["parser_contract_sha256"],
        "analysis_result_opened": False,
    }
    if repair03.get("inner_payload_audit") != expected_inner_payload_audit:
        raise MissionFinalizationError("repair-03 nested audit binding mismatch")

    authority_sha = require_hex(
        repair03.get("authority_basis_sha256"), "repair-03 authority basis SHA-256"
    )
    if repair03.get("authority_basis_path") != REPAIR_03_AUTHORITY_BASIS.as_posix():
        raise MissionFinalizationError("repair-03 authority-basis path mismatch")
    authority = load_json(
        require_path_hash(
            run_dir,
            REPAIR_03_AUTHORITY_BASIS,
            authority_sha,
            "repair-03 authority basis",
        )
    )
    expected_authority = {
        "schema_version": "sports-autoresearch-autonomous-code-repair-authority-v1",
        "run_id": run_id,
        "mission_sha256": EXPECTED_MISSION_SHA,
        "authority_basis": "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_CORRECTION",
        "permitted_change": "PARSER_CONTRACT_CORRECTION_ONLY",
        "operator_repair02_authorization_reused": False,
        "new_data_integrity_decision": False,
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
        "prerequisite_audit_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "prerequisite_audit_sha256": audit_sha,
    }
    if set(authority) != set(expected_authority) | {"recorded_at_utc"} or any(
        authority.get(field) != value for field, value in expected_authority.items()
    ):
        raise MissionFinalizationError("repair-03 authority basis mismatch")
    _require_utc_timestamp(authority.get("recorded_at_utc"), "repair-03 authority basis")
    expected_authority_binding = {
        "path": REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "sha256": authority_sha,
        "schema_version": expected_authority["schema_version"],
        "authority_basis": expected_authority["authority_basis"],
        "permitted_change": expected_authority["permitted_change"],
    }
    if repair03.get("authority_basis") != expected_authority_binding:
        raise MissionFinalizationError("repair-03 nested authority binding mismatch")

    repository = manifest.get("repository")
    current_identity = repair03.get("current_repository_identity")
    previous_identity = prefix["current_identity"]
    identity_chain = repair03.get("repository_identity_chain")
    identity_fields = {
        "execution_commit",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "query_set_sha256",
        "query_files",
    }
    if (
        not isinstance(repository, dict)
        or not isinstance(current_identity, dict)
        or set(current_identity) != identity_fields
        or repair03.get("initial_repository_identity")
        != prefix["initial_identity"]
        or repair03.get("previous_repository_identity") != previous_identity
        or identity_chain
        != [
            prefix["initial_identity"],
            prefix["middle_identity"],
            previous_identity,
            current_identity,
        ]
        or repository.get("identity_history") != identity_chain
        or repository.get("initial_identity") != prefix["initial_identity"]
        or repository.get("previous_identity") != previous_identity
        or repository.get("registration_repair_id") != "repair-03"
        or repository.get("source_tree_dirty_at_freeze") is not False
        or any(repository.get(field) != current_identity[field] for field in identity_fields)
        or repair03.get("previous_execution_commit")
        != previous_identity["execution_commit"]
        or repair03.get("current_execution_commit")
        != current_identity["execution_commit"]
    ):
        raise MissionFinalizationError("repair-03 repository identity chain mismatch")
    for identity_index, identity in enumerate(identity_chain):
        if not isinstance(identity, dict) or set(identity) != identity_fields:
            raise MissionFinalizationError("repair-03 repository identity fields mismatch")
        require_hex(identity.get("execution_commit"), f"repair-03 identity {identity_index} commit", HEX40)
        for field in (
            "source_manifest_sha256",
            "source_sha256s_sha256",
            "query_set_sha256",
        ):
            require_hex(identity.get(field), f"repair-03 identity {identity_index} {field}")
    if len({canonical_json_sha256(identity) for identity in identity_chain}) != 4:
        raise MissionFinalizationError("repair-03 repository identities are not distinct")
    scalar_identity_fields = (
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "query_set_sha256",
    )
    if any(
        repair03.get(f"previous_{field}") != previous_identity[field]
        or repair03.get(f"current_{field}") != current_identity[field]
        for field in scalar_identity_fields
    ):
        raise MissionFinalizationError("repair-03 scalar repository boundary mismatch")
    for root, identity in (
        (REPAIR_03_PRE_ROOT, previous_identity),
        (active_boundary_root, current_identity),
    ):
        for relative, field in (
            (Path("SOURCE_MANIFEST.json"), "source_manifest_sha256"),
            (Path("SOURCE_SHA256SUMS.txt"), "source_sha256s_sha256"),
            (Path("QUERY_SHA256SUMS.txt"), "query_set_sha256"),
        ):
            require_path_hash(
                run_dir,
                root / relative,
                identity[field],
                f"repair-03 repository boundary {root / relative}",
            )
        if identity.get("query_files") != query_files:
            raise MissionFinalizationError("repair-03 changed the query-file path set")
        for relative in query_files:
            checked_relative_path(
                run_dir, (root / relative).as_posix(), "repair-03 frozen query"
            )

    _validate_repair03_source_verification(
        run_dir,
        run_id=run_id,
        repair03=repair03,
        previous_identity=previous_identity,
        current_identity=current_identity,
    )

    parser_contract_sha = require_hex(
        repair03.get("parser_contract_sha256"), "repair-03 parser-contract SHA-256"
    )
    if repair03.get("parser_contract_path") != REPAIR_03_PARSER_CONTRACT.as_posix():
        raise MissionFinalizationError("repair-03 parser-contract path mismatch")
    parser_contract = load_json(
        require_path_hash(
            run_dir,
            REPAIR_03_PARSER_CONTRACT,
            parser_contract_sha,
            "repair-03 parser contract",
        )
    )
    frozen_rfq_path = checked_relative_path(
        run_dir,
        (active_boundary_root / "queries/rfq_full_stage.py").as_posix(),
        "repair-03 frozen RFQ query",
    )
    registered_rfq_query_sha = sha256(frozen_rfq_path)
    if registered_rfq_query_sha != repair03.get("registered_rfq_query_sha256"):
        raise MissionFinalizationError("repair-03 registered RFQ query hash mismatch")
    _validate_repair03_parser_contract(
        parser_contract,
        run_id=run_id,
        registered_query_sha256=registered_rfq_query_sha,
        audit_path=ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03,
        audit_sha256=audit_sha,
    )
    if (
        parser_contract.get("created_at_utc") != repair03.get("applied_at_utc")
        or authority.get("recorded_at_utc") != repair03.get("applied_at_utc")
    ):
        raise MissionFinalizationError(
            "repair-03 registration/parser/authority timestamps differ"
        )
    expected_parser_binding = {
        "path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "sha256": parser_contract_sha,
        "schema_version": "rfq-inner-payload-parser-contract-v1",
        "registered_query_sha256": registered_rfq_query_sha,
        "audited_parser_contract_sha256": audit["parser_contract_sha256"],
        "audited_parser_contract": audit["parser_contract"],
    }
    if repair03.get("parser_contract") != expected_parser_binding:
        raise MissionFinalizationError("repair-03 nested parser binding mismatch")

    journal_sha = require_hex(
        repair03.get("transaction_journal_sha256"),
        "repair-03 transaction journal SHA-256",
    )
    if repair03.get("transaction_journal_path") != REPAIR_03_TRANSACTION_JOURNAL.as_posix():
        raise MissionFinalizationError("repair-03 transaction journal path mismatch")
    journal = load_json(
        require_path_hash(
            run_dir,
            REPAIR_03_TRANSACTION_JOURNAL,
            journal_sha,
            "repair-03 transaction journal",
        )
    )
    mutations = journal.get("active_mutations")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *query_files,
        "TRIAL_REGISTRY.jsonl",
    ]
    if (
        journal.get("schema_version") != "repair03-registration-transaction-v1"
        or journal.get("repair_id") != "repair-03"
        or journal.get("run_id") != run_id
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or [row.get("path") for row in mutations if isinstance(row, dict)]
        != expected_mutation_paths
    ):
        raise MissionFinalizationError("repair-03 transaction journal identity mismatch")
    for row in mutations:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "original_archive_path",
            "original_sha256",
            "replacement_sha256",
            "append_only_registry",
        }:
            raise MissionFinalizationError("repair-03 transaction mutation row is invalid")
        relative = row["path"]
        archive_relative = (REPAIR_03_PRE_ROOT / relative).as_posix()
        if (
            row.get("original_archive_path") != archive_relative
            or row.get("append_only_registry")
            is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise MissionFinalizationError(
                f"repair-03 transaction mutation policy mismatch: {relative}"
            )
        original = checked_relative_path(
            run_dir, archive_relative, "repair-03 transaction original"
        )
        replacement = checked_relative_path(
            run_dir,
            (REPAIR_03_ROOT / "post_repair" / relative).as_posix(),
            "repair-03 transaction replacement",
        )
        active = checked_relative_path(
            run_dir,
            (active_boundary_root / relative).as_posix(),
            "repair-03 transaction active artifact",
        )
        if (
            row.get("original_sha256") != sha256(original)
            or row.get("replacement_sha256") != sha256(replacement)
        ):
            raise MissionFinalizationError(
                f"repair-03 transaction mutation hash mismatch: {relative}"
            )
        if relative == "TRIAL_REGISTRY.jsonl":
            if not active.read_bytes().startswith(replacement.read_bytes()):
                raise MissionFinalizationError(
                    "repair-03 trial-registry replacement is not an active prefix"
                )
        elif sha256(active) != row.get("replacement_sha256"):
            raise MissionFinalizationError(
                f"repair-03 active transaction result changed: {relative}"
            )
    actual_repair03_files = sorted(
        path.relative_to(run_dir / REPAIR_03_ROOT).as_posix()
        for path in (run_dir / REPAIR_03_ROOT).rglob("*")
        if path.is_file()
    )
    if journal.get("expected_repair_files") != actual_repair03_files:
        raise MissionFinalizationError("repair-03 transaction file set mismatch")

    if (
        repair03.get("finding")
        != "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD"
        or repair03.get("registration_change_class")
        != "PARSER_CONTRACT_CORRECTION_ONLY_NO_DATA_SELECTION_OR_HYPOTHESIS_CHANGE"
        or repair03.get("retry_requirement")
        != "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
        or repair03.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair03.get("failed_stage_status")
        != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or repair03.get("quarantine_policy")
        != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
        or repair03.get("expected_success_resource")
        != {
            "label": "rfq_full_stage_repair03",
            "path": ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
        }
        or repair03.get("newly_quarantined_objects") != []
        or repair03.get("cumulative_quarantined_objects")
        != repair02.get("cumulative_quarantined_objects")
        or repair03.get("coverage") != prefix["coverage"]
        or repair03.get("selection_identity_unchanged") is not True
        or repair03.get("previous_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair03.get("current_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair03.get("failed_input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair03.get("hypothesis_design_change") != "NONE"
        or repair03.get("data_selection_change") != "NONE"
        or repair03.get("quarantine_change") != "NONE"
        or repair03.get("core_results_recomputed") is not False
        or repair03.get("threshold_feature_test_or_hypothesis_status_changed")
        is not False
        or repair03.get("core_result_disposition")
        != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair03.get("core_result_artifacts")
        != repair02.get("core_result_artifacts")
        or repair03.get("cycle1_duckdb_binding")
        != prefix["cycle1_duckdb_binding"]
    ):
        raise MissionFinalizationError("repair-03 code-only/no-selection-change boundary mismatch")

    # Trial prefix and the two adaptive-choice records are validated before
    # accepting any successful RFQ output.
    previous_registry = checked_relative_path(
        run_dir,
        (REPAIR_03_PRE_ROOT / "TRIAL_REGISTRY.jsonl").as_posix(),
        "repair-03 previous trial registry",
    ).read_bytes()
    registry = checked_relative_path(
        run_dir,
        (active_boundary_root / "TRIAL_REGISTRY.jsonl").as_posix(),
        "repair-03 trial registry",
    ).read_bytes()
    trial = repair03.get("trial_registry")
    if not isinstance(trial, dict):
        raise MissionFinalizationError("repair-03 trial-registry binding is missing")
    previous_bytes = require_positive_int(
        trial.get("previous_bytes"), "repair-03 previous trial bytes"
    )
    current_bytes = require_positive_int(
        trial.get("current_bytes"), "repair-03 current trial bytes"
    )
    if (
        previous_bytes != prefix["repair02_trial_registry_bytes"]
        or previous_registry != registry[:previous_bytes]
        or len(previous_registry) != previous_bytes
        or hashlib.sha256(previous_registry).hexdigest()
        != prefix["repair02_trial_registry_sha256"]
        or trial.get("previous_sha256")
        != prefix["repair02_trial_registry_sha256"]
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03"]
        or current_bytes <= previous_bytes
        or len(registry) < current_bytes
        or hashlib.sha256(registry[:current_bytes]).hexdigest()
        != trial.get("current_sha256")
    ):
        raise MissionFinalizationError("repair-03 trial append chain mismatch")
    try:
        trial_rows = [
            json.loads(line)
            for line in registry[previous_bytes:current_bytes]
            .decode("utf-8")
            .splitlines()
            if line
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(
            f"invalid repair-03 trial append: {exc}"
        ) from exc
    if (
        len(trial_rows) != 2
        or any(not isinstance(row, dict) for row in trial_rows)
        or [row.get("trial_registration_id") for row in trial_rows]
        != ["RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03"]
        or any(
            row.get("trial_ids") != list(RFQ_TRIAL_ORDER)
            or row.get("result_opened") is not False
            or row.get("hypothesis_conclusion_opened") is not False
            or row.get("parent_repair_id") != "repair-02"
            for row in trial_rows
        )
    ):
        raise MissionFinalizationError("repair-03 trial governance boundary mismatch")
    failure_trial, prereg_trial = trial_rows
    applied_at = _require_utc_timestamp(
        repair03.get("applied_at_utc"), "repair-03 registration"
    )
    common_trial = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(RFQ_TRIAL_ORDER),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": "repair-02",
    }
    expected_failure_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_03",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR02",
        "failure_class": (
            "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD"
        ),
        "failure_disposition": "PARSER_CONTRACT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "failed_state_path": FAILED_RFQ_STATE_03.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "failed_input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "inner_payload_audit_path": (
            ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix()
        ),
        "inner_payload_audit_sha256": audit_sha,
        "execution_commit": previous_identity["execution_commit"],
        "source_manifest_sha256": previous_identity["source_manifest_sha256"],
        "source_sha256s_sha256": previous_identity["source_sha256s_sha256"],
        "query_set_sha256": previous_identity["query_set_sha256"],
    }
    expected_prereg_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_PARSER_CONTRACT_REPAIR_03",
        "record_type": "PARSER_CONTRACT_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR03_PREREGISTRATION",
        "finding": repair03["finding"],
        "registration_change_class": repair03["registration_change_class"],
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": repair03["retry_requirement"],
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": parser_contract_sha,
        "inner_payload_audit_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "inner_payload_audit_sha256": audit_sha,
        "authority_basis_path": REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": repair02["cumulative_quarantined_objects"],
        "coverage": prefix["coverage"],
        "previous_execution_commit": previous_identity["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_selection_fingerprint_sha256": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "current_selection_fingerprint_sha256": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "previous_source_manifest_sha256": previous_identity[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": previous_identity[
            "source_sha256s_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": previous_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "hypothesis_design_change": "NONE",
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    for row, expected, label in (
        (failure_trial, expected_failure_trial, "repair-03 failure trial"),
        (prereg_trial, expected_prereg_trial, "repair-03 preregistration trial"),
    ):
        if row != expected:
            raise MissionFinalizationError(f"{label} mismatch")

    expected_failed03_record = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "state_active_path": ACTIVE_RFQ_STATE.as_posix(),
        "state_path": FAILED_RFQ_STATE_03.as_posix(),
        "state_sha256": failed["state_sha256"],
        "state_status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "resource_active_path": ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
        "resource_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "resource_sha256": failed["resource_sha256"],
        "resource_label": "rfq_full_stage_repair02",
        "return_code": 1,
        "scratch_receipt_active_path": "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json",
        "scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "scratch_receipt_sha256": failed["scratch_sha256"],
        "preserved_scratch_active_path": (
            "cache/rfq_full_scratch.attempt03_failed.duckdb"
        ),
        "preserved_scratch_sha256": failed["preserved_scratch_sha256"],
        "preserved_scratch_bytes": failed["preserved_scratch_bytes"],
        "input_identity_active_path": RFQ_INPUT_IDENTITY.as_posix(),
        "input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "input_identity_sha256": failed["input_sha256"],
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": (
            "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
        ),
    }
    if repair03.get("failed_attempt") != expected_failed03_record:
        raise MissionFinalizationError("repair-03 nested failed-attempt binding mismatch")

    if prefix_only:
        return {
            "prefix_only": True,
            "prefix": prefix,
            "pre_manifest": pre_manifest,
            "repair03": repair03,
            "repair03_receipt_sha256": repair03_receipt_sha,
            "parser_contract_sha256": parser_contract_sha,
            "authority_basis_sha256": authority_sha,
            "inner_payload_audit_sha256": audit_sha,
            "failed": failed,
            "current_identity": current_identity,
            "registered_rfq_query_sha256": registered_rfq_query_sha,
        }

    return _validate_repair03_success_outputs(
        run_dir=run_dir,
        manifest=manifest,
        rfq=rfq,
        repair03=repair03,
        repair03_receipt_sha=repair03_receipt_sha,
        parser_contract_sha=parser_contract_sha,
        authority_sha=authority_sha,
        audit_sha=audit_sha,
        prefix=prefix,
        failed=failed,
        current_identity=current_identity,
    )


def _validate_repair03_success_outputs(
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    repair03: Mapping[str, Any],
    repair03_receipt_sha: str,
    parser_contract_sha: str,
    authority_sha: str,
    audit_sha: str,
    prefix: Mapping[str, Any],
    failed: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the fresh-scratch repair-03 success without relaxing repair-02."""
    run_id = manifest["run_id"]
    summary_path = checked_relative_path(
        run_dir, RFQ_SUMMARY.as_posix(), "repair-03 RFQ summary"
    )
    if load_json(summary_path) != dict(rfq):
        raise MissionFinalizationError(
            "repair-03 RFQ summary argument/file binding mismatch"
        )
    chain03 = {
        "repair_id": "repair-03",
        "registration_path": REPAIR_REGISTRATION_RECEIPT_03.as_posix(),
        "registration_sha256": repair03_receipt_sha,
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": parser_contract_sha,
        "authority_basis_path": REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "inner_payload_audit_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "inner_payload_audit_sha256": audit_sha,
    }
    expected_repair_chain = [*prefix["repair_chain"], chain03]
    failed03_binding = {
        "repair_id": "repair-03",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "failed_state_path": FAILED_RFQ_STATE_03.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "resource_label": "rfq_full_stage_repair02",
        "retry_requirement": (
            "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
        ),
    }
    expected_failed_bindings = [
        *prefix["failed_attempt_bindings"], failed03_binding
    ]
    expected_resource = {
        "label": "rfq_full_stage_repair03",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
    }
    parser_binding = {
        "path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "sha256": parser_contract_sha,
        "schema_version": "rfq-inner-payload-parser-contract-v1",
    }
    registered_query_sha = repair03["registered_rfq_query_sha256"]

    rfq_input = rfq.get("input")
    if not isinstance(rfq_input, dict):
        raise MissionFinalizationError("repair-03 RFQ summary input is missing")
    _validate_repair03_selection_identity(
        {**rfq_input, "run_id": run_id},
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=False,
    )
    if (
        rfq.get("schema") != "sports-autoresearch-rfq-full-stage-v1"
        or rfq.get("run_id") != run_id
        or rfq.get("status") != RFQ_PARTIAL_STATUS
        or rfq.get("analysis_scope") != RFQ_ANALYSIS_SCOPE
        or rfq_input.get("failed_attempt_binding") is not None
        or rfq_input.get("repair_chain") != expected_repair_chain
        or rfq_input.get("failed_attempt_bindings") != expected_failed_bindings
        or rfq_input.get("expected_success_resource") != expected_resource
        or rfq_input.get("inner_payload_parser_contract") != parser_binding
        or rfq_input.get("registered_rfq_query_sha256")
        != registered_query_sha
        or rfq_input.get("outer_parser")
        != "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort"
        or rfq_input.get("overlap_keys") != prefix["overlap_keys"]
    ):
        raise MissionFinalizationError("repair-03 RFQ summary governance binding mismatch")
    summary_generated_key = _utc_timestamp_key(
        rfq.get("generated_at_utc"), "repair-03 RFQ summary generation"
    )

    active_identity = load_json(
        checked_relative_path(
            run_dir, RFQ_INPUT_IDENTITY.as_posix(), "repair-03 active input identity"
        )
    )
    _validate_repair03_selection_identity(
        active_identity,
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=True,
    )
    if (
        active_identity.get("schema") != "rfq-full-input-identity-v3"
        or active_identity.get("releases") != prefix["release_input_receipts"]
        or active_identity.get("repair_chain") != expected_repair_chain
        or active_identity.get("failed_attempt_bindings")
        != expected_failed_bindings
        or active_identity.get("expected_success_resource") != expected_resource
        or active_identity.get("inner_payload_parser_contract") != parser_binding
        or active_identity.get("registered_rfq_query_sha256")
        != registered_query_sha
    ):
        raise MissionFinalizationError("repair-03 active input identity mismatch")

    # Apart from the append-only repair/failure/resource/parser governance
    # fields, the failed and successful identities must be byte-semantically
    # identical.  This catches coordinated count or object-list rewrites.
    mutable_identity_fields = {
        "repair_chain",
        "failed_attempt_bindings",
        "expected_success_resource",
        "inner_payload_parser_contract",
        "registered_rfq_query_sha256",
        "schema",
    }
    failed_stable = {
        key: value for key, value in failed["input"].items()
        if key not in mutable_identity_fields
    }
    active_stable = {
        key: value for key, value in active_identity.items()
        if key not in mutable_identity_fields
    }
    if active_stable != failed_stable:
        raise MissionFinalizationError("repair-03 changed stable RFQ input identity fields")

    active_state_path = checked_relative_path(
        run_dir, ACTIVE_RFQ_STATE.as_posix(), "repair-03 completed RFQ state"
    )
    active_state = load_json(active_state_path)
    _validate_repair02_success_envelope(
        rfq_input=rfq_input,
        active_identity=active_identity,
        active_state=active_state,
        summary_expected={
            key: value for key, value in rfq_input.items()
        },
        identity_expected=active_identity,
        run_id=run_id,
        selection_sha=EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        repair_chain=expected_repair_chain,
        failed_bindings=expected_failed_bindings,
        gap_plan=prefix["quarantine_gap_plan"],
        expected_resource=expected_resource,
    )
    if (
        active_state.get("registered_rfq_query_sha256") != registered_query_sha
        or active_state.get("inner_payload_parser_contract") != parser_binding
    ):
        raise MissionFinalizationError(
            "repair-03 completion state parser/query binding mismatch"
        )
    state_started_key = _utc_timestamp_key(
        active_state.get("started_at_utc"), "repair-03 running state start"
    )
    state_completed_key = _utc_timestamp_key(
        active_state.get("completed_at_utc"), "repair-03 completion state"
    )
    active_state_sha = sha256(active_state_path)

    coverage = rfq.get("coverage")
    counts = rfq.get("counts")
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise MissionFinalizationError("repair-03 RFQ coverage/count receipts are missing")
    _validate_rfq_quarantine_boundary_counts(coverage, counts)
    gaps = coverage.get("quarantine_gap_ranges")
    if (
        coverage.get("rfq_object_coverage") != RFQ_PARTIAL_STATUS
        or not isinstance(gaps, list)
        or len(gaps) != 1
        or coverage.get("quarantine_gap_set_sha256")
        != canonical_json_sha256(gaps)
        or coverage.get("quarantined_hours_are_not_observed_zero") is not True
        or coverage.get("capture_completeness_is_not_lifecycle_join_completeness")
        is not True
    ):
        raise MissionFinalizationError("repair-03 RFQ quarantine-gap semantics mismatch")
    gap = gaps[0]
    if not isinstance(gap, dict):
        raise MissionFinalizationError("repair-03 RFQ gap receipt is invalid")
    gap_start = require_positive_int(gap.get("gap_start_ns"), "repair-03 gap start")
    gap_end = require_positive_int(gap.get("gap_end_ns"), "repair-03 gap end")
    if gap_end <= gap_start or gap.get("quarantined_object_count") != 2:
        raise MissionFinalizationError("repair-03 RFQ gap range is invalid")
    if (
        gap.get("quarantined_object_set_sha256")
        != prefix["quarantined_set_sha256"]
        or gap.get("boundary_reason")
        != "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON"
    ):
        raise MissionFinalizationError("repair-03 RFQ gap identity changed")
    gap_csv = _read_csv_rows(
        run_dir / RFQ_QUARANTINE_GAPS,
        {
            "release_id", "key", "sha256", "quarantined_keys_json",
            "quarantined_object_count", "quarantined_object_set_sha256",
            "previous_filename", "next_filename", "gap_start_ns", "gap_end_ns",
            "gap_start_us", "gap_end_us", "boundary_reason",
        },
        "repair-03 RFQ quarantine gaps",
    )
    if len(gap_csv) != 1:
        raise MissionFinalizationError("repair-03 gap table must contain one row")
    for field, value in gap.items():
        if field not in gap_csv[0]:
            raise MissionFinalizationError(f"repair-03 gap-table field missing: {field}")
        observed: Any = gap_csv[0][field]
        if isinstance(value, int):
            try:
                observed = int(observed)
            except ValueError as exc:
                raise MissionFinalizationError(
                    f"repair-03 invalid gap-table integer: {field}"
                ) from exc
        if observed != value:
            raise MissionFinalizationError(f"repair-03 gap-table mismatch: {field}")
    partial_hours = require_positive_int(
        coverage.get("partial_object_coverage_hours"),
        "repair-03 partial-object coverage hours",
    )
    hour_rows = _read_csv_rows(
        run_dir / RFQ_HOUR_COVERAGE,
        {"object_coverage_status", "zero_interpretation"},
        "repair-03 RFQ hour coverage",
    )
    partial_rows = [
        row for row in hour_rows
        if row.get("object_coverage_status") == RFQ_PARTIAL_STATUS
    ]
    if len(partial_rows) != partial_hours or any(
        row.get("zero_interpretation")
        != "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP"
        for row in partial_rows
    ):
        raise MissionFinalizationError("repair-03 partial-hour semantics mismatch")

    success_path = checked_relative_path(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
        "repair-03 successful RFQ resource",
    )
    success = load_json(success_path)
    command = success.get("command")
    resolved_run_dir = run_dir.resolve()
    expected_command = [
        "/opt/w09/venv/bin/python",
        str(resolved_run_dir / "source/rfq_full_stage.py"),
        "--run-dir",
        str(resolved_run_dir),
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "40GB",
        "--max-temp-size",
        "120GB",
        "--threads",
        "8",
        "--min-free-gib",
        "120",
        "--clob-max-per-root",
        "50",
    ]
    wall_seconds = success.get("wall_seconds")
    if (
        success.get("schema_version") != "w09-stage-resource-v1"
        or success.get("label") != "rfq_full_stage_repair03"
        or success.get("return_code") != 0
        or command != expected_command
        or isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or wall_seconds <= 0
    ):
        raise MissionFinalizationError("repair-03 successful resource receipt mismatch")
    resource_started_key = _utc_timestamp_key(
        success.get("started_at_utc"), "repair-03 resource start"
    )
    resource_completed_key = _utc_timestamp_key(
        success.get("completed_at_utc"), "repair-03 resource completion"
    )
    if not (
        resource_started_key
        <= state_started_key
        <= summary_generated_key
        <= state_completed_key
        <= resource_completed_key
    ):
        raise MissionFinalizationError("repair-03 resource/summary/state time order mismatch")
    active_source = checked_relative_path(
        run_dir, "source/rfq_full_stage.py", "repair-03 active RFQ source"
    )
    frozen_source = checked_relative_path(
        run_dir, "queries/rfq_full_stage.py", "repair-03 frozen RFQ source"
    )
    active_source_sha = sha256(active_source)
    if (
        active_source_sha != sha256(frozen_source)
        or active_source_sha != registered_query_sha
    ):
        raise MissionFinalizationError(
            "repair-03 executed/frozen/registered RFQ source mismatch"
        )
    success_sha = sha256(success_path)

    details = prefix["quarantine_details"]
    return {
        "status": RFQ_PARTIAL_STATUS,
        "analysis_scope": RFQ_ANALYSIS_SCOPE,
        "retained_population": "RETAINED_OBSERVED_SUBSET",
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_objects": list(details),
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "manifest_object_set_sha256": prefix["full_set_sha256"],
        "consumed_object_set_sha256": prefix["retained_set_sha256"],
        "quarantined_object_set_sha256": prefix["quarantined_set_sha256"],
        "selection_fingerprint_sha256": prefix["selection_fingerprint_sha256"],
        "quarantine_gap_plan": prefix["quarantine_gap_plan"],
        "quarantine_gap_set_sha256": canonical_json_sha256(gaps),
        "quarantine_gap": gap,
        "partial_object_coverage_hours": partial_hours,
        "initial_execution_commit": prefix["initial_identity"]["execution_commit"],
        "previous_execution_commit": prefix["current_identity"]["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "initial_source_manifest_sha256": prefix["initial_identity"]["source_manifest_sha256"],
        "previous_source_manifest_sha256": prefix["current_identity"]["source_manifest_sha256"],
        "current_source_manifest_sha256": current_identity["source_manifest_sha256"],
        "initial_source_sha256s_sha256": prefix["initial_identity"]["source_sha256s_sha256"],
        "previous_source_sha256s_sha256": prefix["current_identity"]["source_sha256s_sha256"],
        "current_source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "initial_query_set_sha256": prefix["initial_identity"]["query_set_sha256"],
        "previous_query_set_sha256": prefix["current_identity"]["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "repair_chain": expected_repair_chain,
        "failed_attempt_bindings": expected_failed_bindings,
        "declaration_path": RFQ_DECLARATION_02.as_posix(),
        "declaration_sha256": repair03.get("inherited_declaration_sha256", prefix["repair02"].get("declaration_sha256")),
        "receipt_path": RFQ_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": repair03.get("inherited_receipt_sha256", prefix["repair02"].get("receipt_sha256")),
        "authorization_path": RFQ_REPAIR_02_AUTHORIZATION.as_posix(),
        "authorization_sha256": prefix["authorization02_sha256"],
        "repair_receipt_path": REPAIR_REGISTRATION_RECEIPT_03.as_posix(),
        "repair_receipt_sha256": repair03_receipt_sha,
        "failed_state_path": FAILED_RFQ_STATE_03.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "inner_payload_audit_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "inner_payload_audit_sha256": audit_sha,
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": parser_contract_sha,
        "authority_basis_path": REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "cycle1_duckdb_binding": prefix["cycle1_duckdb_binding"],
        "completed_state_path": ACTIVE_RFQ_STATE.as_posix(),
        "completed_state_sha256": active_state_sha,
        "successful_resource_receipt_path": ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
        "successful_resource_receipt_sha256": success_sha,
        "claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }


def _repair03_runtime_bindings(
    context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Rebuild the exact repair-03 runtime chain inherited by repair-04."""
    prefix = context["prefix"]
    failed = context["failed"]
    chain03 = {
        "repair_id": "repair-03",
        "registration_path": REPAIR_REGISTRATION_RECEIPT_03.as_posix(),
        "registration_sha256": context["repair03_receipt_sha256"],
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": context["parser_contract_sha256"],
        "authority_basis_path": REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": context["authority_basis_sha256"],
        "inner_payload_audit_path": (
            ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix()
        ),
        "inner_payload_audit_sha256": context["inner_payload_audit_sha256"],
    }
    failed03 = {
        "repair_id": "repair-03",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "failed_state_path": FAILED_RFQ_STATE_03.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "resource_label": "rfq_full_stage_repair02",
        "retry_requirement": (
            "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
        ),
    }
    parser_binding = {
        "path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "sha256": context["parser_contract_sha256"],
        "schema_version": "rfq-inner-payload-parser-contract-v1",
    }
    return (
        [*prefix["repair_chain"], chain03],
        [*prefix["failed_attempt_bindings"], failed03],
        parser_binding,
    )


def _validate_repair04_prefix_overlay(
    run_dir: Path,
    *,
    run_id: str,
    repair01: Mapping[str, Any],
    repair02: Mapping[str, Any],
    repair03: Mapping[str, Any],
    rfq: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Replay repair-03 against repair-04's immutable pre-registration root."""
    pre_manifest_path = checked_relative_path(
        run_dir,
        (REPAIR_04_PRE_ROOT / "RUN_MANIFEST.json").as_posix(),
        "repair-04 pre-registration manifest",
    )
    pre_manifest = load_json(pre_manifest_path)
    if (
        pre_manifest.get("run_id") != run_id
        or pre_manifest.get("status") != REPAIR03_PENDING_STATUS
        or pre_manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_PARSER_CORRECTION_REPAIR03_BEFORE_RFQ_RESULT"
        or pre_manifest.get("data_integrity_repairs")
        != [repair01, repair02, repair03]
    ):
        raise MissionFinalizationError(
            "repair-04 did not preserve the repair-03 manifest"
        )
    context = validate_rfq_parser_repair03(
        run_dir,
        pre_manifest,
        rfq,
        prefix_only=True,
        active_boundary_root=REPAIR_04_PRE_ROOT,
    )
    if context.get("prefix_only") is not True:
        raise MissionFinalizationError(
            "repair-04 synthetic repair-03 prefix audit did not complete"
        )
    return context, pre_manifest, pre_manifest_path


def _validate_repair05_prefix_overlay(
    run_dir: Path,
    *,
    run_id: str,
    repairs: Sequence[Mapping[str, Any]],
    rfq: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Replay all repair-01..04 checks at repair-05's frozen boundary."""
    pre_manifest_path = checked_relative_path(
        run_dir,
        (REPAIR_05_PRE_ROOT / "RUN_MANIFEST.json").as_posix(),
        "repair-05 pre-registration manifest",
    )
    pre_manifest = load_json(pre_manifest_path)
    if (
        pre_manifest.get("run_id") != run_id
        or pre_manifest.get("status") != REPAIR04_PENDING_STATUS
        or pre_manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_RESOURCE_RETUNE_REPAIR04_BEFORE_RFQ_RESULT"
        or pre_manifest.get("data_integrity_repairs") != list(repairs[:4])
    ):
        raise MissionFinalizationError(
            "repair-05 did not preserve the repair-04 manifest"
        )
    context = validate_rfq_resource_repair04(
        run_dir,
        pre_manifest,
        rfq,
        prefix_only=True,
        active_boundary_root=REPAIR_05_PRE_ROOT,
    )
    if context.get("prefix_only") is not True:
        raise MissionFinalizationError(
            "repair-05 synthetic repair-04 prefix audit did not complete"
        )
    return context, pre_manifest, pre_manifest_path


def _repair04_expected_retry_command(run_id: str) -> list[str]:
    remote_run = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{remote_run}/{REPAIR_04_EXECUTION_QUERY.as_posix()}",
        "--run-dir",
        remote_run,
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "46GB",
        "--max-temp-size",
        "70GB",
        "--threads",
        "4",
        "--min-free-gib",
        "100",
        "--clob-max-per-root",
        "50",
    ]


def _repair05_expected_retry_command(run_id: str) -> list[str]:
    """Repair-05 retains repair-04's exact executable resource envelope."""
    remote_run = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{remote_run}/{REPAIR_05_EXECUTION_QUERY.as_posix()}",
        "--run-dir",
        remote_run,
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "46GB",
        "--max-temp-size",
        "70GB",
        "--threads",
        "4",
        "--min-free-gib",
        "100",
        "--clob-max-per-root",
        "50",
    ]


def _validate_repair04_resource_and_authority(
    run_dir: Path,
    *,
    run_id: str,
    repair04: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    """Validate the only authorized existing-W09 execution retune."""
    contract_sha = require_hex(
        repair04.get("resource_contract_sha256"),
        "repair-04 resource contract SHA-256",
    )
    if repair04.get("resource_contract_path") != REPAIR_04_RESOURCE_CONTRACT.as_posix():
        raise MissionFinalizationError("repair-04 resource-contract path mismatch")
    contract = load_json(
        require_path_hash(
            run_dir,
            REPAIR_04_RESOURCE_CONTRACT,
            contract_sha,
            "repair-04 resource contract",
        )
    )
    failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
        "state_path": FAILED_RFQ_STATE_04.as_posix(),
        "state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "resource_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "resource_sha256": EXPECTED_RFQ_FAILED_RESOURCE_04_SHA256,
        "input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
        "scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "scratch_receipt_sha256": EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_04_SHA256,
        "error_type": "OutOfMemoryException",
        "error": EXPECTED_RFQ_OOM_ERROR_04,
        "return_code": 1,
    }
    previous_runtime = {
        "memory_limit": "40GB",
        "max_temp_size": "120GB",
        "threads": 8,
        "min_free_gib": 120.0,
        "clob_max_per_root": 50,
        "resume": False,
        "keep_scratch": False,
    }
    current_runtime = {
        "memory_limit": "46GB",
        "max_temp_size": "70GB",
        "threads": 4,
        "min_free_gib": 100.0,
        "clob_max_per_root": 50,
        "resume": False,
        "keep_scratch": False,
    }
    expected_contract = {
        "schema_version": "rfq-execution-resource-contract-v1",
        "run_id": run_id,
        "mission_sha256": EXPECTED_MISSION_SHA,
        "parent_repair_id": "repair-03",
        "finding": "DUCKDB_OUT_OF_MEMORY_DURING_RFQ_DEDUPLICATION_WINDOW",
        "failure_disposition": "RESOURCE_CAP_FAILURE_BEFORE_RESULT",
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "change_class": (
            "EXISTING_W09_EXECUTION_RESOURCE_CONTRACT_RETUNE_ONLY_NO_DATA_"
            "PARSER_QUERY_SELECTION_QUARANTINE_OR_HYPOTHESIS_CHANGE"
        ),
        "failed_attempt": failed_attempt,
        "w09": {
            "instance_id": "i-0e53d134dceffe166",
            "region": "us-east-2",
            "role": "w09-research-runner",
            "memtotal_bytes": EXPECTED_W09_MEMTOTAL_BYTES,
            "same_existing_instance_required": True,
            "resize_allowed": False,
            "replacement_instance_allowed": False,
        },
        "previous_runtime": previous_runtime,
        "current_runtime": current_runtime,
        "memory_safety": {
            "memory_limit_bytes_decimal": 46_000_000_000,
            "memtotal_bytes": EXPECTED_W09_MEMTOTAL_BYTES,
            "memory_limit_fraction_of_memtotal": (
                46_000_000_000 / EXPECTED_W09_MEMTOTAL_BYTES
            ),
            "memory_limit_percent_of_memtotal_rounded_2dp": 69.49,
            "unallocated_memtotal_bytes": (
                EXPECTED_W09_MEMTOTAL_BYTES - 46_000_000_000
            ),
            "threads_reduced_from": 8,
            "threads_reduced_to": 4,
        },
        "disk_safety": {
            "current_free_after_preserving_all_failed_scratch_bytes": (
                124_324_511_744
            ),
            "previous_attempt_disk_free_before_bytes": 153_086_521_344,
            "previous_attempt_minimum_disk_free_bytes": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_MIN_FREE_BYTES
            ),
            "previous_attempt_maximum_disk_delta_bytes": 43_775_340_544,
            "projected_minimum_disk_free_bytes_using_previous_delta": (
                80_549_171_200
            ),
            "previous_attempt_peak_temp_bytes": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_TEMP_BYTES
            ),
            "new_max_temp_size_bytes_decimal": 70_000_000_000,
            "new_min_free_bytes": 100 * 1024**3,
            "failed_scratch_deletion_allowed": False,
        },
        "expected_command": _repair04_expected_retry_command(run_id),
        "fresh_scratch_required": True,
        "resume_allowed": False,
        "failed_scratch_preservation_required": True,
        "expected_success_resource": {
            "label": "rfq_full_stage_repair04",
            "path": ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
    }
    if set(contract) != set(expected_contract) | {"created_at_utc"} or any(
        contract.get(field) != value for field, value in expected_contract.items()
    ):
        raise MissionFinalizationError("repair-04 resource contract mismatch")
    _require_utc_timestamp(
        contract.get("created_at_utc"), "repair-04 resource contract"
    )

    authority_sha = require_hex(
        repair04.get("authority_basis_sha256"),
        "repair-04 authority basis SHA-256",
    )
    if repair04.get("authority_basis_path") != REPAIR_04_AUTHORITY_BASIS.as_posix():
        raise MissionFinalizationError("repair-04 authority-basis path mismatch")
    authority = load_json(
        require_path_hash(
            run_dir,
            REPAIR_04_AUTHORITY_BASIS,
            authority_sha,
            "repair-04 authority basis",
        )
    )
    expected_authority = {
        "schema_version": (
            "sports-autoresearch-existing-w09-resource-retune-authority-v1"
        ),
        "run_id": run_id,
        "mission_sha256": EXPECTED_MISSION_SHA,
        "authority_class": "MISSION_AUTHORIZED_EXISTING_W09_RESOURCE_RETUNING",
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": contract_sha,
        "existing_w09_instance_id": "i-0e53d134dceffe166",
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }
    if set(authority) != set(expected_authority) | {"recorded_at_utc"} or any(
        authority.get(field) != value for field, value in expected_authority.items()
    ):
        raise MissionFinalizationError("repair-04 authority basis mismatch")
    _require_utc_timestamp(
        authority.get("recorded_at_utc"), "repair-04 authority basis"
    )
    if contract.get("created_at_utc") != authority.get("recorded_at_utc"):
        raise MissionFinalizationError(
            "repair-04 contract/authority timestamps differ"
        )
    return contract, contract_sha, authority, authority_sha


def _validate_repair04_failed_evidence(
    run_dir: Path,
    *,
    run_id: str,
    repair04: Mapping[str, Any],
    repair03_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate attempt-04 as a caught DuckDB cap OOM, not a data failure."""
    expected_paths = {
        "failed_state_path": FAILED_RFQ_STATE_04.as_posix(),
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
    }
    if any(repair04.get(field) != value for field, value in expected_paths.items()):
        raise MissionFinalizationError("attempt-04 archived evidence path mismatch")
    state_sha = require_hex(
        repair04.get("failed_state_sha256"), "attempt-04 failed state SHA-256"
    )
    resource_sha = require_hex(
        repair04.get("failed_resource_receipt_sha256"),
        "attempt-04 resource SHA-256",
    )
    scratch_sha = require_hex(
        repair04.get("failed_scratch_receipt_sha256"),
        "attempt-04 scratch receipt SHA-256",
    )
    input_sha = require_hex(
        repair04.get("failed_input_identity_sha256"),
        "attempt-04 input identity SHA-256",
    )
    if (
        state_sha != EXPECTED_RFQ_FAILED_STATE_04_SHA256
        or resource_sha != EXPECTED_RFQ_FAILED_RESOURCE_04_SHA256
        or scratch_sha != EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_04_SHA256
        or input_sha != EXPECTED_RFQ_FAILED_INPUT_04_SHA256
    ):
        raise MissionFinalizationError(
            "attempt-04 evidence is not the approved OOM boundary"
        )
    state = load_json(
        require_path_hash(
            run_dir, FAILED_RFQ_STATE_04, state_sha, "attempt-04 failed state"
        )
    )
    resource = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_RESOURCE_04,
            resource_sha,
            "attempt-04 resource receipt",
        )
    )
    scratch = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_SCRATCH_RECEIPT_04,
            scratch_sha,
            "attempt-04 scratch receipt",
        )
    )
    identity = load_json(
        require_path_hash(
            run_dir,
            FAILED_RFQ_INPUT_IDENTITY_04,
            input_sha,
            "attempt-04 input identity",
        )
    )
    repair_chain, failed_bindings, parser_binding = _repair03_runtime_bindings(
        repair03_context
    )
    expected_resource = {
        "label": "rfq_full_stage_repair03",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
    }
    expected_scratch = (
        f"/srv/w09-research/runs/{run_id}/cache/rfq_full_scratch.duckdb"
    )
    if (
        set(state)
        != {
            "error",
            "error_type",
            "expected_success_resource",
            "failed_at_utc",
            "failed_attempt_bindings",
            "inner_payload_parser_contract",
            "input_fingerprint",
            "next_required_authority",
            "quarantine_gap_plan",
            "registered_rfq_query_sha256",
            "repair_chain",
            "resume",
            "run_id",
            "schema",
            "scratch",
            "started_at_utc",
            "status",
        }
        or state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("run_id") != run_id
        or state.get("status")
        != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or state.get("resume") is not False
        or state.get("input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or state.get("next_required_authority")
        != "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or state.get("scratch") != expected_scratch
        or state.get("error_type") != "OutOfMemoryException"
        or state.get("error") != EXPECTED_RFQ_OOM_ERROR_04
        or state.get("started_at_utc") != "2026-07-15T16:45:59Z"
        or state.get("failed_at_utc") != "2026-07-15T17:00:32Z"
        or state.get("expected_success_resource") != expected_resource
        or state.get("repair_chain") != repair_chain
        or state.get("failed_attempt_bindings") != failed_bindings
        or state.get("inner_payload_parser_contract") != parser_binding
        or state.get("registered_rfq_query_sha256")
        != repair03_context["registered_rfq_query_sha256"]
        or state.get("quarantine_gap_plan")
        != repair03_context["prefix"]["quarantine_gap_plan"]
    ):
        raise MissionFinalizationError("attempt-04 OOM state mismatch")

    expected_failed_command = [
        "/opt/w09/venv/bin/python",
        f"/srv/w09-research/runs/{run_id}/source/rfq_full_stage.py",
        "--run-dir",
        f"/srv/w09-research/runs/{run_id}",
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "40GB",
        "--max-temp-size",
        "120GB",
        "--threads",
        "8",
        "--min-free-gib",
        "120",
        "--clob-max-per-root",
        "50",
    ]
    expected_resource_fields = {
        "command",
        "completed_at_utc",
        "cost_rate_usd_per_hour",
        "cpu_hours",
        "cpu_system_seconds",
        "cpu_user_seconds",
        "cumulative_children_max_rss_kib",
        "disk_free_after_bytes",
        "disk_free_before_bytes",
        "estimated_compute_cost_usd",
        "label",
        "minimum_disk_free_bytes_polled",
        "peak_process_tree_rss_kib_polled",
        "peak_stage_cache_bytes_polled",
        "peak_temp_bytes_polled",
        "poll_samples",
        "poll_seconds",
        "return_code",
        "rss_note",
        "s3_bytes_read_by_analysis",
        "s3_note",
        "schema_version",
        "started_at_utc",
        "wall_seconds",
    }
    if (
        set(resource) != expected_resource_fields
        or resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair03"
        or resource.get("return_code") != 1
        or resource.get("command") != expected_failed_command
        or resource.get("started_at_utc") != "2026-07-15T16:45:24.076899Z"
        or resource.get("completed_at_utc") != "2026-07-15T17:00:34.075652Z"
        or resource.get("wall_seconds")
        != EXPECTED_RFQ_FAILED_RESOURCE_04_WALL_SECONDS
        or resource.get("peak_process_tree_rss_kib_polled")
        != EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_RSS_KIB
        or resource.get("cumulative_children_max_rss_kib") != 40_429_620
        or resource.get("peak_temp_bytes_polled")
        != EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_TEMP_BYTES
        or resource.get("minimum_disk_free_bytes_polled")
        != EXPECTED_RFQ_FAILED_RESOURCE_04_MIN_FREE_BYTES
        or resource.get("disk_free_before_bytes") != 153_086_521_344
        or resource.get("disk_free_after_bytes") != 124_324_532_224
        or resource.get("peak_stage_cache_bytes_polled") != 114_093_269_135
        or resource.get("poll_samples") != 182
        or resource.get("poll_seconds") != 5.0
        or resource.get("s3_bytes_read_by_analysis") != 0
    ):
        raise MissionFinalizationError("attempt-04 OOM resource receipt mismatch")
    resource_start = _utc_timestamp_key(
        resource.get("started_at_utc"), "attempt-04 resource start"
    )
    state_start = _utc_timestamp_key(
        state.get("started_at_utc"), "attempt-04 state start"
    )
    failed_at = _utc_timestamp_key(
        state.get("failed_at_utc"), "attempt-04 failure"
    )
    resource_complete = _utc_timestamp_key(
        resource.get("completed_at_utc"), "attempt-04 resource completion"
    )
    if not resource_start <= state_start <= failed_at <= resource_complete:
        raise MissionFinalizationError("attempt-04 resource/state time order mismatch")

    if (
        set(scratch)
        != {
            "bytes",
            "disposition",
            "input_fingerprint",
            "mtime_utc",
            "original_scratch_path",
            "preserved_scratch_path",
            "resume_allowed",
            "run_id",
            "schema_version",
            "sha256",
        }
        or scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or scratch.get("run_id") != run_id
        or scratch.get("original_scratch_path") != expected_scratch
        or scratch.get("preserved_scratch_path")
        != str(Path(expected_scratch).with_name(
            "rfq_full_scratch.attempt04_failed.duckdb"
        ))
        or scratch.get("input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch.get("resume_allowed") is not False
        or scratch.get("sha256") != EXPECTED_RFQ_FAILED_SCRATCH_04_SHA256
        or scratch.get("bytes") != EXPECTED_RFQ_FAILED_SCRATCH_04_BYTES
        or scratch.get("mtime_utc") != EXPECTED_RFQ_FAILED_SCRATCH_04_MTIME
    ):
        raise MissionFinalizationError("attempt-04 scratch preservation mismatch")
    preserved_path = checked_relative_path(
        run_dir,
        "cache/rfq_full_scratch.attempt04_failed.duckdb",
        "attempt-04 preserved scratch",
    )
    if (
        preserved_path.stat().st_ino != 9_700_311
        or preserved_path.stat().st_size != EXPECTED_RFQ_FAILED_SCRATCH_04_BYTES
        or sha256(preserved_path) != EXPECTED_RFQ_FAILED_SCRATCH_04_SHA256
        or (run_dir / "cache/rfq_full_scratch.duckdb").exists()
        or (run_dir / "cache/rfq_full_scratch.duckdb.wal").exists()
    ):
        raise MissionFinalizationError("attempt-04 preserved scratch identity mismatch")

    prefix = repair03_context["prefix"]
    expected_identity_fields = {
        "consumed_bytes",
        "consumed_logical_bindings",
        "consumed_object_set_sha256",
        "consumed_objects",
        "consumed_unique_objects",
        "coverage_status",
        "cycle1_duckdb_binding",
        "deduplicated_overlapping_objects",
        "expected_success_resource",
        "failed_attempt_binding",
        "failed_attempt_bindings",
        "full_object_coverage",
        "inner_payload_parser_contract",
        "line_salvage",
        "logical_manifest_bindings_total",
        "manifest_object_set_sha256",
        "quarantine_details",
        "quarantine_gap_plan",
        "quarantine_reasons",
        "quarantined_bytes",
        "quarantined_logical_bindings",
        "quarantined_object_set_sha256",
        "quarantined_unique_objects",
        "registered_rfq_query_sha256",
        "release_ids",
        "releases",
        "repair_chain",
        "run_id",
        "schema",
        "selection_fingerprint_sha256",
        "unique_bytes_total",
        "unique_objects_total",
        "whole_object_quarantine",
    }
    if set(identity) != expected_identity_fields:
        raise MissionFinalizationError(
            "attempt-04 input identity field set mismatch"
        )
    _validate_repair03_selection_identity(
        identity,
        prefix={**prefix, "selected_releases": prefix["selected_releases"]},
        run_id=run_id,
        require_consumed_objects=True,
    )
    if (
        identity.get("schema") != "rfq-full-input-identity-v3"
        or identity.get("releases") != prefix["release_input_receipts"]
        or identity.get("repair_chain") != repair_chain
        or identity.get("failed_attempt_bindings") != failed_bindings
        or identity.get("expected_success_resource") != expected_resource
        or identity.get("inner_payload_parser_contract") != parser_binding
        or identity.get("registered_rfq_query_sha256")
        != repair03_context["registered_rfq_query_sha256"]
        or state.get("repair_chain") != identity.get("repair_chain")
        or state.get("failed_attempt_bindings")
        != identity.get("failed_attempt_bindings")
    ):
        raise MissionFinalizationError("attempt-04 state/input chain mismatch")
    return {
        "state": state,
        "resource": resource,
        "scratch": scratch,
        "input": identity,
        "state_sha256": state_sha,
        "resource_sha256": resource_sha,
        "scratch_sha256": scratch_sha,
        "input_sha256": input_sha,
        "preserved_scratch_sha256": EXPECTED_RFQ_FAILED_SCRATCH_04_SHA256,
        "preserved_scratch_bytes": EXPECTED_RFQ_FAILED_SCRATCH_04_BYTES,
    }


def validate_rfq_resource_repair04(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    *,
    prefix_only: bool = False,
    active_boundary_root: Path = Path("."),
) -> dict[str, Any]:
    """Validate repair-01 -> 02 -> 03 -> resource-only repair-04."""
    run_id = manifest.get("run_id")
    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(repairs, list)
        or len(repairs) != 4
        or any(not isinstance(row, dict) for row in repairs)
        or [row.get("repair_id") for row in repairs]
        != ["repair-01", "repair-02", "repair-03", "repair-04"]
    ):
        raise MissionFinalizationError(
            "repair-04 requires the ordered repair-01/02/03/04 chain"
        )
    repair01, repair02, repair03, repair04 = repairs
    source_mode = repair04.get("source_verification_mode")
    record_fields = {
        "schema_version",
        "repair_id",
        "parent_repair_id",
        "applied_at_utc",
        "pre_repair_status",
        "post_repair_status",
        "failed_stage_status",
        "rfq_result_state",
        "retry_requirement",
        "finding",
        "failure_disposition",
        "registration_change_class",
        "source_verification_mode",
        "quarantine_policy",
        "previous_repair_registration_path",
        "previous_repair_registration_sha256",
        "previous_repair_record_sha256",
        "failed_state_path",
        "failed_state_sha256",
        "failed_resource_receipt_path",
        "failed_resource_receipt_sha256",
        "failed_scratch_receipt_path",
        "failed_scratch_receipt_sha256",
        "failed_input_identity_path",
        "failed_input_identity_sha256",
        "resource_contract_path",
        "resource_contract_sha256",
        "authority_basis_path",
        "authority_basis_sha256",
        "cycle1_binding_path",
        "cycle1_binding_sha256",
        "w09_attestation_path",
        "w09_attestation_sha256",
        "registered_rfq_query_sha256",
        "parent_registered_rfq_query_sha256",
        "parser_contract_path",
        "parser_contract_sha256",
        "parser_contract",
        "authority_basis",
        "failed_attempt",
        "expected_success_resource",
        "resource_contract",
        "newly_quarantined_objects",
        "cumulative_quarantined_objects",
        "coverage",
        "selection_identity_unchanged",
        "previous_selection_fingerprint_sha256",
        "current_selection_fingerprint_sha256",
        "failed_input_fingerprint",
        "cycle1_duckdb_binding",
        "previous_execution_commit",
        "current_execution_commit",
        "initial_repository_identity",
        "previous_repository_identity",
        "current_repository_identity",
        "repository_identity_chain",
        "previous_source_manifest_sha256",
        "current_source_manifest_sha256",
        "previous_source_sha256s_sha256",
        "current_source_sha256s_sha256",
        "previous_query_set_sha256",
        "current_query_set_sha256",
        "core_result_disposition",
        "core_results_recomputed",
        "core_result_artifacts",
        "trial_registry",
        "data_selection_change",
        "parser_contract_change",
        "query_semantics_change",
        "quarantine_change",
        "hypothesis_design_change",
        "threshold_feature_test_or_hypothesis_status_changed",
        "archive_path",
        "archive_inventory",
        "transaction_journal_path",
        "transaction_journal_sha256",
        "repair_receipt_path",
        "repair_receipt_sha256",
    }
    if source_mode == REPAIR_04_SNAPSHOT_SOURCE_MODE:
        record_fields.update(
            {
                "source_snapshot_attestation_active_path",
                "source_snapshot_attestation_path",
                "source_snapshot_attestation_sha256",
                "source_snapshot_attestation",
            }
        )
    if set(repair04) != record_fields:
        raise MissionFinalizationError(
            "repair-04 registration record field set mismatch"
        )
    if (
        repair04.get("schema_version")
        != "sports-autoresearch-resource-contract-repair-v1"
        or repair04.get("parent_repair_id") != "repair-03"
        or repair04.get("pre_repair_status") != REPAIR03_PENDING_STATUS
        or repair04.get("post_repair_status") != REPAIR04_PENDING_STATUS
        or manifest.get("status") not in {REPAIR04_PENDING_STATUS, "COMPLETE"}
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_RESOURCE_RETUNE_REPAIR04_BEFORE_RFQ_RESULT"
    ):
        raise MissionFinalizationError(
            "repair-04 registration identity/status mismatch"
        )

    repair03_context, _, pre_manifest_path = _validate_repair04_prefix_overlay(
        run_dir,
        run_id=run_id,
        repair01=repair01,
        repair02=repair02,
        repair03=repair03,
        rfq=rfq,
    )
    repair04_receipt_sha = _validate_repair_record_receipt(
        run_dir,
        repair04,
        REPAIR_REGISTRATION_RECEIPT_04,
        "repair-04 registration",
    )
    previous_record_payload = (
        json.dumps(repair03, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if (
        repair04.get("previous_repair_registration_path")
        != REPAIR_REGISTRATION_RECEIPT_03.as_posix()
        or repair04.get("previous_repair_registration_sha256")
        != repair03_context["repair03_receipt_sha256"]
        or repair04.get("previous_repair_record_sha256")
        != hashlib.sha256(previous_record_payload).hexdigest()
    ):
        raise MissionFinalizationError("repair-04 registration receipt chain mismatch")

    query_files = repair03_context["current_identity"].get("query_files")
    if not isinstance(query_files, list) or not query_files:
        raise MissionFinalizationError("repair-04 previous query-file set is missing")
    archive_required = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *(str(value) for value in query_files),
        ACTIVE_RFQ_STATE.as_posix(),
        ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json",
        RFQ_INPUT_IDENTITY.as_posix(),
        ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        "DATA_INTEGRITY/W09_ATTESTATION.json",
    }
    if source_mode == REPAIR_04_SNAPSHOT_SOURCE_MODE:
        archive_required.add(REPAIR_04_SOURCE_ATTESTATION_ACTIVE.as_posix())
    elif source_mode != REPAIR_04_GIT_SOURCE_MODE:
        raise MissionFinalizationError("repair-04 source verification mode is invalid")
    _validate_archive_inventory(
        run_dir, repair04, REPAIR_04_PRE_ROOT, archive_required, "repair-04"
    )

    failed = _validate_repair04_failed_evidence(
        run_dir,
        run_id=run_id,
        repair04=repair04,
        repair03_context=repair03_context,
    )
    archived_cycle = REPAIR_04_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING
    cycle_sha = require_hex(
        repair04.get("cycle1_binding_sha256"),
        "repair-04 Cycle-1 binding SHA-256",
    )
    if repair04.get("cycle1_binding_path") != archived_cycle.as_posix():
        raise MissionFinalizationError("repair-04 Cycle-1 binding path mismatch")
    active_cycle = require_path_hash(
        run_dir,
        active_boundary_root / ACTIVE_CYCLE1_DUCKDB_BINDING,
        cycle_sha,
        "repair-04 active Cycle-1 binding",
    )
    archived_cycle_path = require_path_hash(
        run_dir,
        archived_cycle,
        cycle_sha,
        "repair-04 archived Cycle-1 binding",
    )
    if active_cycle.read_bytes() != archived_cycle_path.read_bytes():
        raise MissionFinalizationError("repair-04 Cycle-1 binding copies differ")
    w09_sha = require_hex(
        repair04.get("w09_attestation_sha256"),
        "repair-04 W09 attestation SHA-256",
    )
    if repair04.get("w09_attestation_path") != (
        REPAIR_04_W09_ATTESTATION_ARCHIVE.as_posix()
    ):
        raise MissionFinalizationError("repair-04 W09 attestation path mismatch")
    active_w09 = require_path_hash(
        run_dir,
        active_boundary_root / "DATA_INTEGRITY/W09_ATTESTATION.json",
        w09_sha,
        "repair-04 active W09 attestation",
    )
    archived_w09 = require_path_hash(
        run_dir,
        REPAIR_04_W09_ATTESTATION_ARCHIVE,
        w09_sha,
        "repair-04 archived W09 attestation",
    )
    if active_w09.read_bytes() != archived_w09.read_bytes():
        raise MissionFinalizationError(
            "repair-04 active/archived W09 attestations differ"
        )
    w09 = load_json(active_w09)
    if (
        w09.get("instance_id") != "i-0e53d134dceffe166"
        or w09.get("region") != "us-east-2"
        or w09.get("role") != "w09-research-runner"
        or w09.get("instance_profile") != "w09-research-runner"
        or w09.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"
        or w09.get("static_credentials_present") is not False
        or w09.get("trading_credentials_present") is not False
        or w09.get("ambient_aws_or_kalshi_variables") != []
        or w09.get("static_credential_paths_present") != []
    ):
        raise MissionFinalizationError(
            "repair-04 W09 identity/isolation attestation mismatch"
        )

    contract, contract_sha, _, authority_sha = (
        _validate_repair04_resource_and_authority(
            run_dir, run_id=run_id, repair04=repair04
        )
    )
    applied_at = _require_utc_timestamp(
        repair04.get("applied_at_utc"), "repair-04 registration"
    )
    if contract.get("created_at_utc") != repair04.get("applied_at_utc"):
        raise MissionFinalizationError(
            "repair-04 registration/resource-contract timestamps differ"
        )
    expected_contract_record = {
        "path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "sha256": contract_sha,
        "schema_version": "rfq-execution-resource-contract-v1",
        "previous_runtime": contract["previous_runtime"],
        "current_runtime": contract["current_runtime"],
        "expected_command": _repair04_expected_retry_command(run_id),
        "w09_memtotal_bytes": EXPECTED_W09_MEMTOTAL_BYTES,
        "memory_limit_percent_of_memtotal_rounded_2dp": 69.49,
    }
    expected_authority_record = {
        "path": REPAIR_04_AUTHORITY_BASIS.as_posix(),
        "sha256": authority_sha,
        "schema_version": (
            "sports-autoresearch-existing-w09-resource-retune-authority-v1"
        ),
        "authority_class": "MISSION_AUTHORIZED_EXISTING_W09_RESOURCE_RETUNING",
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
    }
    if (
        repair04.get("resource_contract") != expected_contract_record
        or repair04.get("authority_basis") != expected_authority_record
    ):
        raise MissionFinalizationError(
            "repair-04 nested resource/authority binding mismatch"
        )
    if (
        repair04.get("parser_contract_path")
        != repair03_context["repair03"].get("parser_contract_path")
        or repair04.get("parser_contract_sha256")
        != repair03_context["parser_contract_sha256"]
        or repair04.get("parser_contract")
        != repair03_context["repair03"].get("parser_contract")
        or repair04.get("parent_registered_rfq_query_sha256")
        != repair03_context["registered_rfq_query_sha256"]
    ):
        raise MissionFinalizationError(
            "repair-04 changed the inherited parser/query boundary"
        )

    previous_identity = repair03_context["current_identity"]
    current_identity = repair04.get("current_repository_identity")
    identity_chain = repair04.get("repository_identity_chain")
    parent_chain = repair03.get("repository_identity_chain")
    repository = manifest.get("repository")
    identity_fields = {
        "execution_commit",
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "query_set_sha256",
        "query_files",
    }
    if (
        not isinstance(repository, dict)
        or not isinstance(current_identity, dict)
        or set(current_identity) != identity_fields
        or not isinstance(parent_chain, list)
        or len(parent_chain) != 4
        or parent_chain[-1] != previous_identity
        or identity_chain != [*parent_chain, current_identity]
        or repository.get("identity_history") != identity_chain
        or repository.get("initial_identity")
        != repair03_context["prefix"]["initial_identity"]
        or repository.get("previous_identity") != previous_identity
        or repository.get("registration_repair_id") != "repair-04"
        or repository.get("source_tree_dirty_at_freeze") is not False
        or any(
            repository.get(field) != current_identity[field]
            for field in identity_fields
        )
        or repair04.get("initial_repository_identity")
        != repair03_context["prefix"]["initial_identity"]
        or repair04.get("previous_repository_identity") != previous_identity
        or repair04.get("previous_execution_commit")
        != previous_identity["execution_commit"]
        or repair04.get("current_execution_commit")
        != current_identity["execution_commit"]
    ):
        raise MissionFinalizationError("repair-04 repository identity chain mismatch")
    for index, identity in enumerate(identity_chain):
        if not isinstance(identity, dict) or set(identity) != identity_fields:
            raise MissionFinalizationError(
                "repair-04 repository identity fields mismatch"
            )
        require_hex(
            identity.get("execution_commit"),
            f"repair-04 identity {index} commit",
            HEX40,
        )
        for field in (
            "source_manifest_sha256",
            "source_sha256s_sha256",
            "query_set_sha256",
        ):
            require_hex(
                identity.get(field), f"repair-04 identity {index} {field}"
            )
    if len({canonical_json_sha256(row) for row in identity_chain}) != 5:
        raise MissionFinalizationError(
            "repair-04 repository identities are not distinct"
        )
    for field in (
        "source_manifest_sha256",
        "source_sha256s_sha256",
        "query_set_sha256",
    ):
        if (
            repair04.get(f"previous_{field}") != previous_identity[field]
            or repair04.get(f"current_{field}") != current_identity[field]
        ):
            raise MissionFinalizationError(
                "repair-04 scalar repository boundary mismatch"
            )
    for root, identity in (
        (REPAIR_04_PRE_ROOT, previous_identity),
        (active_boundary_root, current_identity),
    ):
        for relative, field in (
            (Path("SOURCE_MANIFEST.json"), "source_manifest_sha256"),
            (Path("SOURCE_SHA256SUMS.txt"), "source_sha256s_sha256"),
            (Path("QUERY_SHA256SUMS.txt"), "query_set_sha256"),
        ):
            require_path_hash(
                run_dir,
                root / relative,
                identity[field],
                f"repair-04 repository boundary {root / relative}",
            )
        if identity.get("query_files") != query_files:
            raise MissionFinalizationError(
                "repair-04 changed the query-file path set"
            )
        for relative in query_files:
            checked_relative_path(
                run_dir,
                (root / relative).as_posix(),
                "repair-04 frozen query",
            )
    _validate_repair04_source_verification(
        run_dir,
        run_id=run_id,
        repair04=repair04,
        previous_identity=previous_identity,
        current_identity=current_identity,
        active_boundary_root=active_boundary_root,
    )
    active_rfq_query = checked_relative_path(
        run_dir,
        (active_boundary_root / REPAIR_04_EXECUTION_QUERY).as_posix(),
        "repair-04 registered RFQ query",
    )
    registered_query_sha = sha256(active_rfq_query)
    if registered_query_sha != repair04.get("registered_rfq_query_sha256"):
        raise MissionFinalizationError("repair-04 registered RFQ query hash mismatch")

    journal_sha = require_hex(
        repair04.get("transaction_journal_sha256"),
        "repair-04 transaction journal SHA-256",
    )
    if (
        repair04.get("transaction_journal_path")
        != REPAIR_04_TRANSACTION_JOURNAL.as_posix()
    ):
        raise MissionFinalizationError("repair-04 transaction journal path mismatch")
    journal = load_json(
        require_path_hash(
            run_dir,
            REPAIR_04_TRANSACTION_JOURNAL,
            journal_sha,
            "repair-04 transaction journal",
        )
    )
    mutations = journal.get("active_mutations")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *query_files,
        "TRIAL_REGISTRY.jsonl",
    ]
    if (
        journal.get("schema_version") != "repair04-registration-transaction-v1"
        or journal.get("repair_id") != "repair-04"
        or journal.get("run_id") != run_id
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256") != sha256(pre_manifest_path)
        or not isinstance(mutations, list)
        or [row.get("path") for row in mutations if isinstance(row, dict)]
        != expected_mutation_paths
    ):
        raise MissionFinalizationError(
            "repair-04 transaction journal identity mismatch"
        )
    for row in mutations:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "original_archive_path",
            "original_sha256",
            "replacement_sha256",
            "append_only_registry",
        }:
            raise MissionFinalizationError(
                "repair-04 transaction mutation row is invalid"
            )
        relative = row["path"]
        archive_relative = (REPAIR_04_PRE_ROOT / relative).as_posix()
        if (
            row.get("original_archive_path") != archive_relative
            or row.get("append_only_registry")
            is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise MissionFinalizationError(
                f"repair-04 transaction mutation policy mismatch: {relative}"
            )
        original = checked_relative_path(
            run_dir, archive_relative, "repair-04 transaction original"
        )
        replacement = checked_relative_path(
            run_dir,
            (REPAIR_04_ROOT / "post_repair" / relative).as_posix(),
            "repair-04 transaction replacement",
        )
        active = checked_relative_path(
            run_dir,
            (active_boundary_root / relative).as_posix(),
            "repair-04 transaction active artifact",
        )
        if (
            row.get("original_sha256") != sha256(original)
            or row.get("replacement_sha256") != sha256(replacement)
        ):
            raise MissionFinalizationError(
                f"repair-04 transaction mutation hash mismatch: {relative}"
            )
        if relative == "TRIAL_REGISTRY.jsonl":
            if not active.read_bytes().startswith(replacement.read_bytes()):
                raise MissionFinalizationError(
                    "repair-04 trial-registry replacement is not an active prefix"
                )
        elif sha256(active) != row.get("replacement_sha256"):
            raise MissionFinalizationError(
                f"repair-04 active transaction result changed: {relative}"
            )
    actual_repair_files = sorted(
        path.relative_to(run_dir / REPAIR_04_ROOT).as_posix()
        for path in (run_dir / REPAIR_04_ROOT).rglob("*")
        if path.is_file()
    )
    if journal.get("expected_repair_files") != actual_repair_files:
        raise MissionFinalizationError("repair-04 transaction file set mismatch")

    prefix = repair03_context["prefix"]
    expected_runtime = {
        "memory_limit": "46GB",
        "max_temp_size": "70GB",
        "threads": 4,
        "min_free_gib": 100.0,
        "clob_max_per_root": 50,
        "resume": False,
        "keep_scratch": False,
    }
    expected_previous_runtime = {
        "memory_limit": "40GB",
        "max_temp_size": "120GB",
        "threads": 8,
        "min_free_gib": 120.0,
        "clob_max_per_root": 50,
        "resume": False,
        "keep_scratch": False,
    }
    if (
        repair04.get("finding")
        != "DUCKDB_OUT_OF_MEMORY_DURING_RFQ_DEDUPLICATION_WINDOW"
        or repair04.get("registration_change_class")
        != (
            "EXISTING_W09_EXECUTION_RESOURCE_CONTRACT_RETUNE_ONLY_NO_DATA_"
            "PARSER_QUERY_SELECTION_QUARANTINE_OR_HYPOTHESIS_CHANGE"
        )
        or repair04.get("failure_disposition")
        != "RESOURCE_CAP_FAILURE_BEFORE_RESULT"
        or repair04.get("retry_requirement")
        != (
            "FRESH_SCRATCH_SAME_SELECTION_SAME_PARSER_QUERY_AND_HYPOTHESES_"
            "RETUNED_RESOURCES_NO_RESUME"
        )
        or repair04.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair04.get("failed_stage_status")
        != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or repair04.get("quarantine_policy")
        != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
        or repair04.get("expected_success_resource")
        != {
            "label": "rfq_full_stage_repair04",
            "path": ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
        }
        or repair04.get("newly_quarantined_objects") != []
        or repair04.get("cumulative_quarantined_objects")
        != repair03.get("cumulative_quarantined_objects")
        or repair04.get("coverage") != prefix["coverage"]
        or repair04.get("selection_identity_unchanged") is not True
        or repair04.get("previous_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair04.get("current_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair04.get("failed_input_fingerprint")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair04.get("data_selection_change") != "NONE"
        or repair04.get("parser_contract_change") != "NONE"
        or repair04.get("query_semantics_change") != "NONE"
        or repair04.get("quarantine_change") != "NONE"
        or repair04.get("hypothesis_design_change") != "NONE"
        or repair04.get("core_results_recomputed") is not False
        or repair04.get("threshold_feature_test_or_hypothesis_status_changed")
        is not False
        or repair04.get("core_result_disposition")
        != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair04.get("core_result_artifacts")
        != repair03.get("core_result_artifacts")
        or repair04.get("cycle1_duckdb_binding")
        != prefix["cycle1_duckdb_binding"]
    ):
        raise MissionFinalizationError(
            "repair-04 resource-only/no-semantic-change boundary mismatch"
        )

    previous_registry = checked_relative_path(
        run_dir,
        (REPAIR_04_PRE_ROOT / "TRIAL_REGISTRY.jsonl").as_posix(),
        "repair-04 previous trial registry",
    ).read_bytes()
    registry = checked_relative_path(
        run_dir,
        (active_boundary_root / "TRIAL_REGISTRY.jsonl").as_posix(),
        "repair-04 trial registry",
    ).read_bytes()
    trial = repair04.get("trial_registry")
    if not isinstance(trial, dict):
        raise MissionFinalizationError("repair-04 trial-registry binding is missing")
    previous_bytes = require_positive_int(
        trial.get("previous_bytes"), "repair-04 previous trial bytes"
    )
    current_bytes = require_positive_int(
        trial.get("current_bytes"), "repair-04 current trial bytes"
    )
    parent_trial = repair03.get("trial_registry", {})
    if (
        previous_bytes != parent_trial.get("current_bytes")
        or previous_registry != registry[:previous_bytes]
        or len(previous_registry) != previous_bytes
        or hashlib.sha256(previous_registry).hexdigest()
        != parent_trial.get("current_sha256")
        or trial.get("previous_sha256") != parent_trial.get("current_sha256")
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_04", "RFQ_RESOURCE_CONTRACT_REPAIR_04"]
        or current_bytes <= previous_bytes
        or len(registry) < current_bytes
        or hashlib.sha256(registry[:current_bytes]).hexdigest()
        != trial.get("current_sha256")
    ):
        raise MissionFinalizationError("repair-04 trial append chain mismatch")
    try:
        trial_rows = [
            json.loads(line)
            for line in registry[previous_bytes:current_bytes]
            .decode("utf-8")
            .splitlines()
            if line
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(
            f"invalid repair-04 trial append: {exc}"
        ) from exc
    if len(trial_rows) != 2 or any(not isinstance(row, dict) for row in trial_rows):
        raise MissionFinalizationError("repair-04 trial append shape mismatch")
    common_trial = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(RFQ_TRIAL_ORDER),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": "repair-03",
    }
    expected_failure_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_04",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR03",
        "failure_class": repair04["finding"],
        "failure_disposition": "RESOURCE_CAP_FAILURE_BEFORE_RESULT",
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "error_type": "OutOfMemoryException",
        "hypothesis_conclusion": "NONE",
        "failed_state_path": FAILED_RFQ_STATE_04.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "failed_input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "resource_label": "rfq_full_stage_repair03",
        "return_code": 1,
        "resource_metrics": {
            "wall_seconds": EXPECTED_RFQ_FAILED_RESOURCE_04_WALL_SECONDS,
            "peak_process_tree_rss_kib_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_RSS_KIB
            ),
            "cumulative_children_max_rss_kib": 40_429_620,
            "peak_temp_bytes_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_TEMP_BYTES
            ),
            "minimum_disk_free_bytes_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_MIN_FREE_BYTES
            ),
        },
        "execution_commit": previous_identity["execution_commit"],
        "source_manifest_sha256": previous_identity["source_manifest_sha256"],
        "source_sha256s_sha256": previous_identity["source_sha256s_sha256"],
        "query_set_sha256": previous_identity["query_set_sha256"],
    }
    expected_prereg_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_RESOURCE_CONTRACT_REPAIR_04",
        "record_type": "RESOURCE_CONTRACT_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR04_PREREGISTRATION",
        "finding": repair04["finding"],
        "registration_change_class": repair04["registration_change_class"],
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": repair04["retry_requirement"],
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": contract_sha,
        "authority_basis_path": REPAIR_04_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "previous_runtime": expected_previous_runtime,
        "current_runtime": expected_runtime,
        "expected_success_resource": repair04["expected_success_resource"],
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": repair03[
            "cumulative_quarantined_objects"
        ],
        "coverage": prefix["coverage"],
        "previous_selection_fingerprint_sha256": (
            EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        ),
        "current_selection_fingerprint_sha256": (
            EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        ),
        "previous_execution_commit": previous_identity["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": previous_identity[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": previous_identity[
            "source_sha256s_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": previous_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    if trial_rows != [expected_failure_trial, expected_prereg_trial]:
        raise MissionFinalizationError("repair-04 trial governance boundary mismatch")

    expected_failed_record = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
        "state_active_path": ACTIVE_RFQ_STATE.as_posix(),
        "state_path": FAILED_RFQ_STATE_04.as_posix(),
        "state_sha256": failed["state_sha256"],
        "state_status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "error_type": "OutOfMemoryException",
        "error": EXPECTED_RFQ_OOM_ERROR_04,
        "failure_phase": "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP",
        "resource_active_path": ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
        "resource_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "resource_sha256": failed["resource_sha256"],
        "resource_label": "rfq_full_stage_repair03",
        "return_code": 1,
        "resource_command": [
            "/opt/w09/venv/bin/python",
            f"/srv/w09-research/runs/{run_id}/source/rfq_full_stage.py",
            "--run-dir",
            f"/srv/w09-research/runs/{run_id}",
            "--cache-root",
            "/srv/w09-research/cache",
            "--memory-limit",
            "40GB",
            "--max-temp-size",
            "120GB",
            "--threads",
            "8",
            "--min-free-gib",
            "120",
            "--clob-max-per-root",
            "50",
        ],
        "resource_metrics": {
            "started_at_utc": "2026-07-15T16:45:24.076899Z",
            "completed_at_utc": "2026-07-15T17:00:34.075652Z",
            "wall_seconds": EXPECTED_RFQ_FAILED_RESOURCE_04_WALL_SECONDS,
            "peak_process_tree_rss_kib_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_RSS_KIB
            ),
            "cumulative_children_max_rss_kib": 40_429_620,
            "peak_temp_bytes_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_PEAK_TEMP_BYTES
            ),
            "minimum_disk_free_bytes_polled": (
                EXPECTED_RFQ_FAILED_RESOURCE_04_MIN_FREE_BYTES
            ),
            "disk_free_before_bytes": 153_086_521_344,
            "disk_free_after_bytes": 124_324_532_224,
        },
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json"
        ),
        "scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "scratch_receipt_sha256": failed["scratch_sha256"],
        "preserved_scratch_active_path": (
            "cache/rfq_full_scratch.attempt04_failed.duckdb"
        ),
        "preserved_scratch_sha256": failed["preserved_scratch_sha256"],
        "preserved_scratch_bytes": failed["preserved_scratch_bytes"],
        "preserved_scratch_mtime_utc": EXPECTED_RFQ_FAILED_SCRATCH_04_MTIME,
        "preserved_scratch_original_inode": 9_700_311,
        "input_identity_active_path": RFQ_INPUT_IDENTITY.as_posix(),
        "input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "input_identity_sha256": failed["input_sha256"],
        "input_identity_schema": "rfq-full-input-identity-v3",
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": repair04["retry_requirement"],
    }
    if repair04.get("failed_attempt") != expected_failed_record:
        raise MissionFinalizationError(
            "repair-04 nested failed-attempt binding mismatch"
        )

    if prefix_only:
        return {
            "prefix_only": True,
            "repair03_context": repair03_context,
            "pre_manifest": pre_manifest_path,
            "repair04": repair04,
            "repair04_receipt_sha256": repair04_receipt_sha,
            "resource_contract_sha256": contract_sha,
            "authority_basis_sha256": authority_sha,
            "failed": failed,
            "current_identity": current_identity,
            "registered_rfq_query_sha256": registered_query_sha,
        }

    return _validate_repair04_success_outputs(
        run_dir=run_dir,
        manifest=manifest,
        rfq=rfq,
        repair04=repair04,
        repair04_receipt_sha=repair04_receipt_sha,
        resource_contract_sha=contract_sha,
        authority_sha=authority_sha,
        repair03_context=repair03_context,
        failed=failed,
        current_identity=current_identity,
    )


def _repair04_runtime_bindings(
    *,
    repair03_context: Mapping[str, Any],
    repair04_receipt_sha: str,
    resource_contract_sha: str,
    authority_sha: str,
    failed: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    """Build the exact append-only runtime governance inherited by attempt 05."""
    repair_chain, failed_bindings, parser_binding = _repair03_runtime_bindings(
        repair03_context
    )
    chain04 = {
        "repair_id": "repair-04",
        "registration_path": REPAIR_REGISTRATION_RECEIPT_04.as_posix(),
        "registration_sha256": repair04_receipt_sha,
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": resource_contract_sha,
        "authority_basis_path": REPAIR_04_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
    }
    failed04 = {
        "repair_id": "repair-04",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
        "failed_state_path": FAILED_RFQ_STATE_04.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "resource_label": "rfq_full_stage_repair03",
        "retry_requirement": (
            "FRESH_SCRATCH_SAME_SELECTION_SAME_PARSER_QUERY_AND_HYPOTHESES_"
            "RETUNED_RESOURCES_NO_RESUME"
        ),
    }
    resource_binding = {
        "path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "sha256": resource_contract_sha,
        "schema_version": "rfq-execution-resource-contract-v1",
        "current_runtime": {
            "memory_limit": "46GB",
            "max_temp_size": "70GB",
            "threads": 4,
            "min_free_gib": 100.0,
            "clob_max_per_root": 50,
            "resume": False,
            "keep_scratch": False,
        },
    }
    return (
        [*repair_chain, chain04],
        [*failed_bindings, failed04],
        parser_binding,
        resource_binding,
    )


def _validate_repair05_blocker_and_failed_resource(
    run_dir: Path,
    *,
    run_id: str,
    repair04_context: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    """Bind attempt 05's repair-04 consumer failure before new RFQ evidence."""
    blocker_active = require_path_hash(
        run_dir,
        REPAIR_05_BLOCKER_ACTIVE,
        EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256,
        "repair-05 active consumer blocker",
    )
    blocker_archive = require_path_hash(
        run_dir,
        REPAIR_05_BLOCKER_ARCHIVE,
        EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256,
        "repair-05 archived consumer blocker",
    )
    if blocker_active.read_bytes() != blocker_archive.read_bytes():
        raise MissionFinalizationError(
            "repair-05 active/archived consumer blockers differ"
        )
    blocker = load_json(blocker_active)
    expected_blocker = {
        "schema_version": "rfq-repair04-consumer-pre-evidence-blocker-v1",
        "run_id": run_id,
        "mission_sha256": EXPECTED_MISSION_SHA,
        "created_at_utc": "2026-07-15T18:37:00Z",
        "finding": "REPAIR04_CONSUMER_CYCLE1_BINDING_LOOKUP_KEYERROR",
        "failure_phase": "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION",
        "error_type": "KeyError",
        "error": "KeyError: 'cycle1_duckdb_binding'",
        "offending_function": "_validate_repair04_failed_boundary",
        "offending_expression": "base_result['cycle1_duckdb_binding']",
        "traceback_call_chain": [
            "main",
            "apply_object_quarantine",
            "_apply_repair04_resource_contract",
            "_validate_repair04_failed_boundary",
        ],
        "execution_commit": repair04_context["current_identity"][
            "execution_commit"
        ],
        "parent_registration_path": (
            REPAIR_REGISTRATION_RECEIPT_04.as_posix()
        ),
        "parent_registration_sha256": repair04_context[
            "repair04_receipt_sha256"
        ],
        "registered_query_path": REPAIR_04_EXECUTION_QUERY.as_posix(),
        "registered_query_sha256": repair04_context[
            "registered_rfq_query_sha256"
        ],
        "repair04_resource_contract_path": (
            REPAIR_04_RESOURCE_CONTRACT.as_posix()
        ),
        "repair04_resource_contract_sha256": repair04_context[
            "resource_contract_sha256"
        ],
        "failed_resource_receipt_path": (
            ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix()
        ),
        "failed_resource_receipt_sha256": (
            EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256
        ),
        "resource_label": "rfq_full_stage_repair04",
        "resource_return_code": 1,
        "active_state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "active_input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "new_state_written": False,
        "new_input_identity_written": False,
        "analysis_stage_started": False,
        "dependent_rfq_result_opened": False,
        "whole_object_quarantine_unchanged": True,
        "line_salvage": False,
        "source_change_scope": "CONSUMER_VALIDATION_WIRING_ONLY",
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "authority_basis": (
            "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_"
            "SPEND_DATA_OR_FROZEN_VALIDATION_SET"
        ),
        "next_required_action": (
            "APPEND_ONLY_REPAIR05_CONSUMER_VALIDATION_WIRING_CORRECTION_"
            "AND_FRESH_SCRATCH_RETRY"
        ),
    }
    if blocker != expected_blocker:
        raise MissionFinalizationError("repair-05 consumer blocker mismatch")

    resource_active = require_path_hash(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE_04,
        EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
        "repair-05 active failed repair-04 resource",
    )
    resource_archive = require_path_hash(
        run_dir,
        FAILED_RFQ_RESOURCE_05,
        EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
        "repair-05 archived failed repair-04 resource",
    )
    if resource_active.read_bytes() != resource_archive.read_bytes():
        raise MissionFinalizationError(
            "repair-05 active/archived failed resources differ"
        )
    resource = load_json(resource_active)
    remote_run = f"/srv/w09-research/runs/{run_id}"
    expected_resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage_repair04",
        "started_at_utc": "2026-07-15T18:33:21.211957Z",
        "completed_at_utc": "2026-07-15T18:33:39.330283Z",
        "wall_seconds": EXPECTED_RFQ_FAILED_RESOURCE_05_WALL_SECONDS,
        "return_code": 1,
        "command": [
            "/opt/w09/venv/bin/python",
            f"{remote_run}/{REPAIR_04_EXECUTION_QUERY.as_posix()}",
            "--run-dir",
            remote_run,
            "--cache-root",
            "/srv/w09-research/cache",
            "--memory-limit",
            "46GB",
            "--max-temp-size",
            "70GB",
            "--threads",
            "4",
            "--min-free-gib",
            "100",
            "--clob-max-per-root",
            "50",
        ],
        "cost_rate_usd_per_hour": 0.4713,
        "cpu_hours": 0.005018,
        "cpu_system_seconds": 1.638,
        "cpu_user_seconds": 16.426,
        "cumulative_children_max_rss_kib": 35_976,
        "disk_free_after_bytes": 124_105_936_896,
        "disk_free_before_bytes": 124_105_936_896,
        "estimated_compute_cost_usd": 0.002372,
        "minimum_disk_free_bytes_polled": 124_105_936_896,
        "peak_process_tree_rss_kib_polled": 35_988,
        "peak_stage_cache_bytes_polled": 114_093_269_135,
        "peak_temp_bytes_polled": 20_552,
        "poll_samples": 4,
        "poll_seconds": 5.0,
        "rss_note": (
            "Process-tree RSS is sampled and may miss sub-poll peaks; "
            "cumulative_children_max_rss_kib is an upper-bound cross-stage "
            "diagnostic, not stage-specific."
        ),
        "s3_bytes_read_by_analysis": 0,
        "s3_note": (
            "Analysis reads the already verified local immutable cache; gate "
            "verification is tracked separately and exposes no per-command S3 "
            "byte counter."
        ),
    }
    if resource != expected_resource:
        raise MissionFinalizationError(
            "repair-05 failed repair-04 resource receipt mismatch"
        )

    for relative, expected_sha, label in (
        (
            UNCHANGED_RFQ_STATE_05,
            EXPECTED_RFQ_FAILED_STATE_04_SHA256,
            "repair-05 unchanged attempt-04 state",
        ),
        (
            UNCHANGED_RFQ_INPUT_IDENTITY_05,
            EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
            "repair-05 unchanged attempt-04 input identity",
        ),
    ):
        require_path_hash(run_dir, relative, expected_sha, label)
    for relative in (
        REPAIR_05_PRE_ROOT / "cache/rfq_full_scratch.duckdb",
        REPAIR_05_PRE_ROOT / "cache/rfq_full_scratch.duckdb.wal",
        Path("cache/rfq_full_scratch.duckdb"),
        Path("cache/rfq_full_scratch.duckdb.wal"),
    ):
        candidate = run_dir / relative
        if candidate.exists() or candidate.is_symlink():
            raise MissionFinalizationError(
                f"repair-05 found prohibited attempt-05 scratch evidence: {relative}"
            )
    return (
        blocker,
        EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256,
        resource,
        EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
    )


def _validate_repair05_contracts(
    run_dir: Path,
    *,
    run_id: str,
    repair05: Mapping[str, Any],
    repair04_context: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    """Validate the wiring-only contract and its no-expansion authority."""
    wiring_sha = require_hex(
        repair05.get("wiring_contract_sha256"),
        "repair-05 wiring contract SHA-256",
    )
    if repair05.get("wiring_contract_path") != REPAIR_05_WIRING_CONTRACT.as_posix():
        raise MissionFinalizationError("repair-05 wiring-contract path mismatch")
    wiring = load_json(
        require_path_hash(
            run_dir,
            REPAIR_05_WIRING_CONTRACT,
            wiring_sha,
            "repair-05 wiring contract",
        )
    )
    current_query_sha = require_hex(
        repair05.get("registered_rfq_query_sha256"),
        "repair-05 registered RFQ query SHA-256",
    )
    expected_wiring = {
        "schema_version": "rfq-consumer-validation-wiring-contract-v1",
        "run_id": run_id,
        "created_at_utc": repair05.get("applied_at_utc"),
        "mission_sha256": EXPECTED_MISSION_SHA,
        "repair_id": "repair-05",
        "parent_repair_id": "repair-04",
        "finding": "REPAIR04_CONSUMER_CYCLE1_BINDING_LOOKUP_KEYERROR",
        "failure_disposition": (
            "CONSUMER_PRE_EVIDENCE_VALIDATION_FAILURE_BEFORE_RESULT"
        ),
        "failure_phase": "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION",
        "change_class": (
            "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY_NO_DATA_PARSER_QUERY_"
            "SEMANTICS_SELECTION_QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_"
            "CHANGE"
        ),
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
            "blocker_path": REPAIR_05_BLOCKER_ARCHIVE.as_posix(),
            "blocker_sha256": (
                EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256
            ),
            "resource_path": FAILED_RFQ_RESOURCE_05.as_posix(),
            "resource_sha256": EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
            "resource_label": "rfq_full_stage_repair04",
            "return_code": 1,
            "command": _repair05_expected_retry_command(run_id),
            "error_type": "KeyError",
            "error": "KeyError: 'cycle1_duckdb_binding'",
            "new_state_written": False,
            "new_input_identity_written": False,
            "analysis_stage_started": False,
            "active_state_path": ACTIVE_RFQ_STATE.as_posix(),
            "active_state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
            "active_input_identity_path": RFQ_INPUT_IDENTITY.as_posix(),
            "active_input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
            "active_scratch_absent": True,
            "active_wal_absent": True,
        },
        "defect": {
            "offending_function": "_validate_repair04_failed_boundary",
            "offending_expression": "base_result['cycle1_duckdb_binding']",
            "offending_parent_source_path": REPAIR_04_EXECUTION_QUERY.as_posix(),
            "offending_parent_source_sha256": repair04_context[
                "registered_rfq_query_sha256"
            ],
            "offending_parent_source_line": 3822,
            "invalid_binding_source": "repair03_quarantine_consumer_base_result",
            "required_binding_source": (
                "repair04_and_repair03_registration_cycle1_duckdb_binding_"
                "plus_immutable_active_and_archived_binding"
            ),
        },
        "cycle1_binding": {
            "active_path": ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
            "active_sha256": repair05.get("cycle1_binding_sha256"),
            "archived_path": repair05.get("cycle1_binding_path"),
            "archived_sha256": repair05.get("cycle1_binding_sha256"),
            "registered_cycle1_duckdb_binding": repair04_context[
                "repair03_context"
            ]["prefix"]["cycle1_duckdb_binding"],
        },
        "correction": {
            "scope": "CONSUMER_VALIDATION_WIRING_ONLY",
            "registered_query_path": REPAIR_05_EXECUTION_QUERY.as_posix(),
            "parent_registered_query_sha256": repair04_context[
                "registered_rfq_query_sha256"
            ],
            "current_registered_query_sha256": current_query_sha,
            "expected_command": _repair05_expected_retry_command(run_id),
            "expected_success_resource": {
                "label": "rfq_full_stage_repair05",
                "path": ACTIVE_RFQ_REPAIR_RESOURCE_05.as_posix(),
            },
            "fresh_scratch_required": True,
            "resume_allowed": False,
        },
        "inherited_contracts": {
            "repair04_resource_contract_path": (
                REPAIR_04_RESOURCE_CONTRACT.as_posix()
            ),
            "repair04_resource_contract_sha256": repair04_context[
                "resource_contract_sha256"
            ],
            "runtime": {
                "memory_limit": "46GB",
                "max_temp_size": "70GB",
                "threads": 4,
                "min_free_gib": 100.0,
                "clob_max_per_root": 50,
                "resume": False,
                "keep_scratch": False,
            },
            "selection_fingerprint_sha256": (
                EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
            ),
            "retained_object_set_sha256": EXPECTED_RFQ_RETAINED_SET_SHA256,
            "quarantined_object_set_sha256": (
                EXPECTED_RFQ_QUARANTINED_SET_SHA256
            ),
            "full_object_set_sha256": EXPECTED_RFQ_FULL_SET_SHA256,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }
    if wiring != expected_wiring:
        raise MissionFinalizationError("repair-05 wiring contract mismatch")
    _require_utc_timestamp(
        wiring.get("created_at_utc"), "repair-05 wiring contract"
    )

    authority_sha = require_hex(
        repair05.get("authority_basis_sha256"),
        "repair-05 authority basis SHA-256",
    )
    if repair05.get("authority_basis_path") != REPAIR_05_AUTHORITY_BASIS.as_posix():
        raise MissionFinalizationError("repair-05 authority-basis path mismatch")
    authority = load_json(
        require_path_hash(
            run_dir,
            REPAIR_05_AUTHORITY_BASIS,
            authority_sha,
            "repair-05 authority basis",
        )
    )
    expected_authority = {
        "schema_version": (
            "sports-autoresearch-consumer-validation-wiring-authority-v1"
        ),
        "run_id": run_id,
        "recorded_at_utc": repair05.get("applied_at_utc"),
        "mission_sha256": EXPECTED_MISSION_SHA,
        "authority_class": (
            "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_SPEND_"
            "DATA_OR_FROZEN_VALIDATION_SET"
        ),
        "permitted_change": "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY",
        "wiring_contract_path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "wiring_contract_sha256": wiring_sha,
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
    if authority != expected_authority:
        raise MissionFinalizationError("repair-05 authority basis mismatch")
    _require_utc_timestamp(
        authority.get("recorded_at_utc"), "repair-05 authority basis"
    )
    return wiring, wiring_sha, authority, authority_sha


def _repair05_runtime_bindings(
    *,
    repair04_context: Mapping[str, Any],
    repair05_receipt_sha: str,
    wiring_contract_sha: str,
    authority_sha: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Build the exact append-only governance consumed by repair-05."""
    (
        repair_chain,
        failed_bindings,
        parser_binding,
        resource_binding,
    ) = _repair04_runtime_bindings(
        repair03_context=repair04_context["repair03_context"],
        repair04_receipt_sha=repair04_context["repair04_receipt_sha256"],
        resource_contract_sha=repair04_context["resource_contract_sha256"],
        authority_sha=repair04_context["authority_basis_sha256"],
        failed=repair04_context["failed"],
    )
    chain05 = {
        "repair_id": "repair-05",
        "registration_path": REPAIR_REGISTRATION_RECEIPT_05.as_posix(),
        "registration_sha256": repair05_receipt_sha,
        "wiring_contract_path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "wiring_contract_sha256": wiring_contract_sha,
        "authority_basis_path": REPAIR_05_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
    }
    failed05 = {
        "repair_id": "repair-05",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
        "blocker_path": REPAIR_05_BLOCKER_ARCHIVE.as_posix(),
        "blocker_sha256": EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_05.as_posix(),
        "failed_resource_receipt_sha256": EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
        "unchanged_state_path": UNCHANGED_RFQ_STATE_05.as_posix(),
        "unchanged_state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "unchanged_input_identity_path": (
            UNCHANGED_RFQ_INPUT_IDENTITY_05.as_posix()
        ),
        "unchanged_input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
        "new_state_written": False,
        "new_input_identity_written": False,
        "input_fingerprint": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "resource_label": "rfq_full_stage_repair04",
        "retry_requirement": (
            "APPEND_ONLY_REPAIR05_CONSUMER_VALIDATION_WIRING_CORRECTION_AND_"
            "FRESH_SCRATCH_RETRY"
        ),
    }
    wiring_binding = {
        "path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "sha256": wiring_contract_sha,
        "schema_version": "rfq-consumer-validation-wiring-contract-v1",
    }
    return (
        [*repair_chain, chain05],
        [*failed_bindings, failed05],
        parser_binding,
        resource_binding,
        wiring_binding,
    )


def _validate_repair05_transaction(
    run_dir: Path,
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    repair05: Mapping[str, Any],
    pre_manifest_path: Path,
    query_files: Sequence[str],
) -> dict[str, Any]:
    """Validate repair-05's durable CAS journal at its committed boundary."""
    journal_sha = require_hex(
        repair05.get("transaction_journal_sha256"),
        "repair-05 transaction journal SHA-256",
    )
    if (
        repair05.get("transaction_journal_path")
        != REPAIR_05_TRANSACTION_JOURNAL.as_posix()
    ):
        raise MissionFinalizationError(
            "repair-05 transaction journal path mismatch"
        )
    journal_path = require_path_hash(
        run_dir,
        REPAIR_05_TRANSACTION_JOURNAL,
        journal_sha,
        "repair-05 transaction journal",
    )
    if journal_path.is_symlink() or pre_manifest_path.is_symlink():
        raise MissionFinalizationError(
            "repair-05 transaction journal/manifest boundary is symlinked"
        )
    journal = load_json(journal_path)
    expected_journal_fields = {
        "schema_version",
        "repair_id",
        "run_id",
        "state",
        "original_manifest_sha256",
        "active_mutations",
        "manifest_mutation",
        "expected_repair_files",
    }
    mutations = journal.get("active_mutations")
    manifest_mutation = journal.get("manifest_mutation")
    expected_mutation_paths = [
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        *query_files,
        "TRIAL_REGISTRY.jsonl",
    ]
    pre_manifest_bytes = pre_manifest_path.read_bytes()
    if (
        set(journal) != expected_journal_fields
        or journal.get("schema_version")
        != "repair05-registration-transaction-v1"
        or journal.get("repair_id") != "repair-05"
        or journal.get("run_id") != run_id
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or journal.get("original_manifest_sha256")
        != hashlib.sha256(pre_manifest_bytes).hexdigest()
        or not isinstance(mutations, list)
        or [row.get("path") for row in mutations if isinstance(row, dict)]
        != expected_mutation_paths
        or not isinstance(manifest_mutation, dict)
    ):
        raise MissionFinalizationError(
            "repair-05 transaction journal mismatch"
        )

    mutation_fields = {
        "path",
        "original_archive_path",
        "original_sha256",
        "replacement_staging_path",
        "replacement_sha256",
        "displaced_path",
        "append_only_registry",
    }
    repair_root = run_dir / REPAIR_05_ROOT

    def validate_displaced_absent(relative: str, expected: Path, label: str) -> None:
        if relative != expected.as_posix():
            raise MissionFinalizationError(
                f"repair-05 {label} displaced path mismatch"
            )
        displaced = run_dir / expected
        try:
            displaced.resolve().relative_to(repair_root.resolve())
        except ValueError as exc:
            raise MissionFinalizationError(
                f"repair-05 {label} displaced path escapes repair root"
            ) from exc
        if os.path.lexists(displaced):
            raise MissionFinalizationError(
                f"repair-05 committed displaced path still exists: {expected}"
            )

    for row in mutations:
        if not isinstance(row, dict) or set(row) != mutation_fields:
            raise MissionFinalizationError(
                "repair-05 transaction mutation field set is invalid"
            )
        relative = row["path"]
        expected_original = REPAIR_05_PRE_ROOT / relative
        expected_staging = REPAIR_05_ROOT / "post_repair" / relative
        expected_displaced = (
            REPAIR_05_ROOT
            / "cas_displaced"
            / f"{relative.replace('/', '__')}.original"
        )
        if (
            row.get("original_archive_path") != expected_original.as_posix()
            or row.get("replacement_staging_path")
            != expected_staging.as_posix()
            or row.get("append_only_registry")
            is not (relative == "TRIAL_REGISTRY.jsonl")
        ):
            raise MissionFinalizationError(
                f"repair-05 transaction mutation policy mismatch: {relative}"
            )
        original = checked_relative_path(
            run_dir,
            expected_original.as_posix(),
            "repair-05 transaction archived original",
        )
        replacement = checked_relative_path(
            run_dir,
            expected_staging.as_posix(),
            "repair-05 transaction staged replacement",
        )
        active = checked_relative_path(
            run_dir,
            relative,
            "repair-05 transaction active replacement",
        )
        if original.is_symlink() or replacement.is_symlink() or active.is_symlink():
            raise MissionFinalizationError(
                f"repair-05 transaction path is symlinked: {relative}"
            )
        original_bytes = original.read_bytes()
        replacement_bytes = replacement.read_bytes()
        active_bytes = active.read_bytes()
        original_sha = require_hex(
            row.get("original_sha256"),
            f"repair-05 transaction original SHA-256: {relative}",
        )
        replacement_sha = require_hex(
            row.get("replacement_sha256"),
            f"repair-05 transaction replacement SHA-256: {relative}",
        )
        if (
            hashlib.sha256(original_bytes).hexdigest() != original_sha
            or hashlib.sha256(replacement_bytes).hexdigest() != replacement_sha
            or hashlib.sha256(active_bytes).hexdigest() != replacement_sha
            or active_bytes != replacement_bytes
        ):
            raise MissionFinalizationError(
                f"repair-05 archive/staging/active CAS mismatch: {relative}"
            )
        validate_displaced_absent(
            row.get("displaced_path"),
            expected_displaced,
            f"active mutation {relative}",
        )

    if set(manifest_mutation) != mutation_fields:
        raise MissionFinalizationError(
            "repair-05 manifest mutation field set is invalid"
        )
    expected_manifest_original = REPAIR_05_PRE_ROOT / "RUN_MANIFEST.json"
    expected_manifest_staging = (
        REPAIR_05_ROOT / "post_repair/RUN_MANIFEST.json"
    )
    expected_manifest_displaced = (
        REPAIR_05_ROOT / "cas_displaced/RUN_MANIFEST.json.original"
    )
    if (
        manifest_mutation.get("path") != "RUN_MANIFEST.json"
        or manifest_mutation.get("original_archive_path")
        != expected_manifest_original.as_posix()
        or manifest_mutation.get("original_sha256")
        != hashlib.sha256(pre_manifest_bytes).hexdigest()
        or manifest_mutation.get("replacement_staging_path")
        != expected_manifest_staging.as_posix()
        or manifest_mutation.get("replacement_sha256") is not None
        or manifest_mutation.get("append_only_registry") is not False
    ):
        raise MissionFinalizationError(
            "repair-05 manifest mutation policy mismatch"
        )
    staged_manifest_path = checked_relative_path(
        run_dir,
        expected_manifest_staging.as_posix(),
        "repair-05 staged manifest replacement",
    )
    active_manifest_path = checked_relative_path(
        run_dir,
        "RUN_MANIFEST.json",
        "repair-05 active manifest replacement",
    )
    if staged_manifest_path.is_symlink() or active_manifest_path.is_symlink():
        raise MissionFinalizationError(
            "repair-05 staged/active manifest replacement is symlinked"
        )
    staged_manifest_bytes = staged_manifest_path.read_bytes()
    active_manifest_bytes = active_manifest_path.read_bytes()
    if (
        staged_manifest_bytes != active_manifest_bytes
        or load_json(staged_manifest_path) != dict(manifest)
        or load_json(active_manifest_path) != dict(manifest)
    ):
        raise MissionFinalizationError(
            "repair-05 staged/active manifest replacement mismatch"
        )
    validate_displaced_absent(
        manifest_mutation.get("displaced_path"),
        expected_manifest_displaced,
        "manifest mutation",
    )

    records = manifest.get("data_integrity_repairs")
    repository = manifest.get("repository")
    receipt_sha = require_hex(
        repair05.get("repair_receipt_sha256"),
        "repair-05 registration receipt SHA-256",
    )
    receipt_path = run_dir / REPAIR_REGISTRATION_RECEIPT_05
    if (
        manifest.get("run_id") != run_id
        or manifest.get("status") != REPAIR05_PENDING_STATUS
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_CONSUMER_WIRING_REPAIR05_BEFORE_RFQ_RESULT"
        or not isinstance(records, list)
        or len(records) != 5
        or records[-1] != repair05
        or repair05.get("transaction_journal_path")
        != REPAIR_05_TRANSACTION_JOURNAL.as_posix()
        or repair05.get("transaction_journal_sha256") != journal_sha
        or repair05.get("repair_receipt_path")
        != REPAIR_REGISTRATION_RECEIPT_05.as_posix()
        or receipt_path.is_symlink()
        or not receipt_path.is_file()
        or sha256(receipt_path) != receipt_sha
        or not isinstance(repository, dict)
        or repository.get("registration_repair_id") != "repair-05"
        or repository.get("execution_commit")
        != repair05.get("current_execution_commit")
        or repository.get("source_manifest_sha256")
        != repair05.get("current_source_manifest_sha256")
        or repository.get("source_sha256s_sha256")
        != repair05.get("current_source_sha256s_sha256")
        or repository.get("query_set_sha256")
        != repair05.get("current_query_set_sha256")
    ):
        raise MissionFinalizationError(
            "repair-05 committed manifest self-binding mismatch"
        )
    receipt = load_json(receipt_path)
    record_without_self = dict(repair05)
    record_without_self.pop("repair_receipt_path", None)
    record_without_self.pop("repair_receipt_sha256", None)
    if receipt != record_without_self:
        raise MissionFinalizationError(
            "repair-05 receipt/manifest self-binding mismatch"
        )

    expected_repair_files = journal.get("expected_repair_files")
    actual_repair_files = sorted(
        path.relative_to(repair_root).as_posix()
        for path in repair_root.rglob("*")
        if path.is_file()
    )
    if (
        not isinstance(expected_repair_files, list)
        or expected_repair_files != actual_repair_files
        or "post_repair/RUN_MANIFEST.json" not in expected_repair_files
    ):
        raise MissionFinalizationError(
            "repair-05 transaction expected file set mismatch"
        )
    return journal


def validate_rfq_consumer_wiring_repair05(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate repair-01..05 without treating attempt 05 as RFQ evidence."""
    run_id = manifest.get("run_id")
    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(repairs, list)
        or len(repairs) != 5
        or any(not isinstance(row, dict) for row in repairs)
        or [row.get("repair_id") for row in repairs]
        != ["repair-01", "repair-02", "repair-03", "repair-04", "repair-05"]
    ):
        raise MissionFinalizationError(
            "repair-05 requires the ordered repair-01/02/03/04/05 chain"
        )
    repair04 = repairs[3]
    repair05 = repairs[4]
    record_fields = {
        "schema_version", "repair_id", "parent_repair_id", "applied_at_utc",
        "pre_repair_status", "post_repair_status", "rfq_result_state",
        "retry_requirement", "finding", "failure_disposition", "failure_phase",
        "registration_change_class", "source_verification_mode",
        "quarantine_policy", "previous_repair_registration_path",
        "previous_repair_registration_sha256", "previous_repair_record_sha256",
        "blocker_path", "blocker_sha256", "failed_resource_receipt_path",
        "failed_resource_receipt_sha256", "unchanged_state_path",
        "unchanged_state_sha256", "unchanged_input_identity_path",
        "unchanged_input_identity_sha256", "cycle1_binding_path",
        "cycle1_binding_sha256", "failed_attempt",
        "wiring_contract_path", "wiring_contract_sha256", "wiring_contract",
        "authority_basis_path", "authority_basis_sha256", "authority_basis",
        "resource_contract_path", "resource_contract_sha256", "resource_contract",
        "expected_success_resource", "registered_rfq_query_sha256",
        "parent_registered_rfq_query_sha256", "parser_contract_path",
        "parser_contract_sha256", "parser_contract", "newly_quarantined_objects",
        "cumulative_quarantined_objects", "coverage", "selection_identity_unchanged",
        "previous_selection_fingerprint_sha256",
        "current_selection_fingerprint_sha256", "cycle1_duckdb_binding",
        "previous_execution_commit", "current_execution_commit",
        "initial_repository_identity", "previous_repository_identity",
        "current_repository_identity", "repository_identity_chain",
        "previous_source_manifest_sha256", "current_source_manifest_sha256",
        "previous_source_sha256s_sha256", "current_source_sha256s_sha256",
        "previous_query_set_sha256", "current_query_set_sha256",
        "core_result_disposition", "core_results_recomputed",
        "core_result_artifacts", "trial_registry", "data_selection_change",
        "parser_contract_change", "query_semantics_change", "quarantine_change",
        "resource_contract_change", "hypothesis_design_change",
        "threshold_feature_test_or_hypothesis_status_changed", "archive_path",
        "archive_inventory", "transaction_journal_path",
        "transaction_journal_sha256", "repair_receipt_path",
        "repair_receipt_sha256",
    }
    if set(repair05) != record_fields:
        raise MissionFinalizationError(
            "repair-05 registration record field set mismatch"
        )
    if (
        repair05.get("schema_version")
        != "sports-autoresearch-consumer-validation-wiring-repair-v1"
        or repair05.get("parent_repair_id") != "repair-04"
        or repair05.get("pre_repair_status") != REPAIR04_PENDING_STATUS
        or repair05.get("post_repair_status") != REPAIR05_PENDING_STATUS
        or manifest.get("status") not in {REPAIR05_PENDING_STATUS, "COMPLETE"}
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_RFQ_CONSUMER_WIRING_REPAIR05_BEFORE_RFQ_RESULT"
        or repair05.get("source_verification_mode") != REPAIR_05_GIT_SOURCE_MODE
    ):
        raise MissionFinalizationError(
            "repair-05 registration identity/status mismatch"
        )

    repair04_context, _, pre_manifest_path = _validate_repair05_prefix_overlay(
        run_dir, run_id=run_id, repairs=repairs, rfq=rfq
    )
    repair05_receipt_sha = _validate_repair_record_receipt(
        run_dir,
        repair05,
        REPAIR_REGISTRATION_RECEIPT_05,
        "repair-05 registration",
    )
    previous_record_payload = (
        json.dumps(repair04, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if (
        repair05.get("previous_repair_registration_path")
        != REPAIR_REGISTRATION_RECEIPT_04.as_posix()
        or repair05.get("previous_repair_registration_sha256")
        != repair04_context["repair04_receipt_sha256"]
        or repair05.get("previous_repair_record_sha256")
        != hashlib.sha256(previous_record_payload).hexdigest()
    ):
        raise MissionFinalizationError(
            "repair-05 registration receipt chain mismatch"
        )

    query_files = repair04_context["current_identity"].get("query_files")
    if not isinstance(query_files, list) or not query_files:
        raise MissionFinalizationError("repair-05 parent query-file set is missing")
    archive_required = {
        "RUN_MANIFEST.json", "TRIAL_REGISTRY.jsonl", "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt", "QUERY_SHA256SUMS.txt",
        *(str(value) for value in query_files), ACTIVE_RFQ_STATE.as_posix(),
        RFQ_INPUT_IDENTITY.as_posix(), ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
        REPAIR_05_BLOCKER_ACTIVE.as_posix(),
        ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        "DATA_INTEGRITY/W09_ATTESTATION.json",
    }
    _validate_archive_inventory(
        run_dir, repair05, REPAIR_05_PRE_ROOT, archive_required, "repair-05"
    )
    blocker, blocker_sha, failed_resource, failed_resource_sha = (
        _validate_repair05_blocker_and_failed_resource(
            run_dir, run_id=run_id, repair04_context=repair04_context
        )
    )
    wiring, wiring_sha, authority, authority_sha = _validate_repair05_contracts(
        run_dir,
        run_id=run_id,
        repair05=repair05,
        repair04_context=repair04_context,
    )
    applied_at = _require_utc_timestamp(
        repair05.get("applied_at_utc"), "repair-05 registration"
    )
    if (
        wiring.get("created_at_utc") != applied_at
        or authority.get("recorded_at_utc") != applied_at
    ):
        raise MissionFinalizationError(
            "repair-05 registration/contract/authority timestamps differ"
        )
    if (
        repair05.get("blocker_path") != REPAIR_05_BLOCKER_ARCHIVE.as_posix()
        or repair05.get("blocker_sha256") != blocker_sha
        or repair05.get("failed_resource_receipt_path")
        != FAILED_RFQ_RESOURCE_05.as_posix()
        or repair05.get("failed_resource_receipt_sha256") != failed_resource_sha
        or repair05.get("unchanged_state_path")
        != UNCHANGED_RFQ_STATE_05.as_posix()
        or repair05.get("unchanged_state_sha256")
        != EXPECTED_RFQ_FAILED_STATE_04_SHA256
        or repair05.get("unchanged_input_identity_path")
        != UNCHANGED_RFQ_INPUT_IDENTITY_05.as_posix()
        or repair05.get("unchanged_input_identity_sha256")
        != EXPECTED_RFQ_FAILED_INPUT_04_SHA256
    ):
        raise MissionFinalizationError("repair-05 failure evidence binding mismatch")
    expected_failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
        "resource_active_path": ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
        "resource_path": FAILED_RFQ_RESOURCE_05.as_posix(),
        "resource_sha256": failed_resource_sha,
        "resource_label": "rfq_full_stage_repair04",
        "return_code": 1,
        "resource_command": _repair05_expected_retry_command(run_id),
        "resource_metrics": {
            key: failed_resource[key]
            for key in (
                "started_at_utc", "completed_at_utc", "wall_seconds",
                "cpu_user_seconds", "cpu_system_seconds", "cpu_hours",
                "peak_process_tree_rss_kib_polled",
                "cumulative_children_max_rss_kib", "peak_temp_bytes_polled",
                "minimum_disk_free_bytes_polled", "disk_free_before_bytes",
                "disk_free_after_bytes", "estimated_compute_cost_usd",
            )
        },
        "blocker_active_path": REPAIR_05_BLOCKER_ACTIVE.as_posix(),
        "blocker_path": REPAIR_05_BLOCKER_ARCHIVE.as_posix(),
        "blocker_sha256": blocker_sha,
        "error_type": "KeyError",
        "error": "KeyError: 'cycle1_duckdb_binding'",
        "failure_phase": "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION",
        "offending_function": "_validate_repair04_failed_boundary",
        "offending_expression": "base_result['cycle1_duckdb_binding']",
        "new_state_written": False,
        "new_input_identity_written": False,
        "analysis_stage_started": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "unchanged_state_active_path": ACTIVE_RFQ_STATE.as_posix(),
        "unchanged_state_path": UNCHANGED_RFQ_STATE_05.as_posix(),
        "unchanged_state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "unchanged_input_identity_active_path": RFQ_INPUT_IDENTITY.as_posix(),
        "unchanged_input_identity_path": (
            UNCHANGED_RFQ_INPUT_IDENTITY_05.as_posix()
        ),
        "unchanged_input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
    }
    if repair05.get("failed_attempt") != expected_failed_attempt:
        raise MissionFinalizationError(
            "repair-05 nested failed-attempt binding mismatch"
        )

    expected_wiring_record = {
        "path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "sha256": wiring_sha,
        "schema_version": "rfq-consumer-validation-wiring-contract-v1",
        "expected_command": _repair05_expected_retry_command(run_id),
    }
    expected_authority_record = {
        "path": REPAIR_05_AUTHORITY_BASIS.as_posix(),
        "sha256": authority_sha,
        "schema_version": (
            "sports-autoresearch-consumer-validation-wiring-authority-v1"
        ),
        "authority_class": (
            "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_SPEND_"
            "DATA_OR_FROZEN_VALIDATION_SET"
        ),
        "permitted_change": "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY",
    }
    if (
        repair05.get("wiring_contract") != expected_wiring_record
        or repair05.get("authority_basis") != expected_authority_record
        or repair05.get("resource_contract_path")
        != repair04.get("resource_contract_path")
        or repair05.get("resource_contract_sha256")
        != repair04_context["resource_contract_sha256"]
        or repair05.get("resource_contract") != repair04.get("resource_contract")
        or repair05.get("parser_contract_path")
        != repair04.get("parser_contract_path")
        or repair05.get("parser_contract_sha256")
        != repair04.get("parser_contract_sha256")
        or repair05.get("parser_contract") != repair04.get("parser_contract")
        or repair05.get("parent_registered_rfq_query_sha256")
        != repair04_context["registered_rfq_query_sha256"]
    ):
        raise MissionFinalizationError(
            "repair-05 inherited contract boundary mismatch"
        )

    prefix = repair04_context["repair03_context"]["prefix"]
    if (
        repair05.get("finding")
        != "REPAIR04_CONSUMER_CYCLE1_BINDING_LOOKUP_KEYERROR"
        or repair05.get("failure_disposition")
        != "CONSUMER_PRE_EVIDENCE_VALIDATION_FAILURE_BEFORE_RESULT"
        or repair05.get("failure_phase")
        != "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION"
        or repair05.get("registration_change_class")
        != (
            "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY_NO_DATA_PARSER_QUERY_"
            "SEMANTICS_SELECTION_QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_"
            "CHANGE"
        )
        or repair05.get("retry_requirement")
        != (
            "APPEND_ONLY_REPAIR05_CONSUMER_VALIDATION_WIRING_CORRECTION_AND_"
            "FRESH_SCRATCH_RETRY"
        )
        or repair05.get("rfq_result_state") != "NO_RFQ_RESULT_OPENED"
        or repair05.get("quarantine_policy")
        != "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
        or repair05.get("expected_success_resource")
        != {
            "label": "rfq_full_stage_repair05",
            "path": ACTIVE_RFQ_REPAIR_RESOURCE_05.as_posix(),
        }
        or repair05.get("newly_quarantined_objects") != []
        or repair05.get("cumulative_quarantined_objects")
        != repair04.get("cumulative_quarantined_objects")
        or repair05.get("coverage") != repair04.get("coverage")
        or repair05.get("selection_identity_unchanged") is not True
        or repair05.get("previous_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair05.get("current_selection_fingerprint_sha256")
        != EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
        or repair05.get("cycle1_duckdb_binding")
        != prefix["cycle1_duckdb_binding"]
        or repair05.get("core_result_disposition")
        != "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
        or repair05.get("core_results_recomputed") is not False
        or repair05.get("core_result_artifacts")
        != repair04.get("core_result_artifacts")
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
        raise MissionFinalizationError(
            "repair-05 wiring-only/no-semantic-change boundary mismatch"
        )

    # Active and archived Cycle-1/W09 evidence must remain byte-identical.
    cycle_sha = require_hex(
        repair05.get("cycle1_binding_sha256"),
        "repair-05 Cycle-1 binding SHA-256",
    )
    if repair05.get("cycle1_binding_path") != (
        REPAIR_05_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING
    ).as_posix():
        raise MissionFinalizationError("repair-05 Cycle-1 binding path mismatch")
    active_cycle = require_path_hash(
        run_dir, ACTIVE_CYCLE1_DUCKDB_BINDING, cycle_sha,
        "repair-05 active Cycle-1 binding",
    )
    archived_cycle = require_path_hash(
        run_dir, REPAIR_05_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING, cycle_sha,
        "repair-05 archived Cycle-1 binding",
    )
    if active_cycle.read_bytes() != archived_cycle.read_bytes():
        raise MissionFinalizationError("repair-05 Cycle-1 binding copies differ")
    w09_sha = repair04.get("w09_attestation_sha256")
    require_hex(w09_sha, "repair-05 inherited W09 SHA-256")
    active_w09 = require_path_hash(
        run_dir, Path("DATA_INTEGRITY/W09_ATTESTATION.json"), w09_sha,
        "repair-05 active W09 attestation",
    )
    archived_w09 = require_path_hash(
        run_dir, REPAIR_05_W09_ATTESTATION_ARCHIVE, w09_sha,
        "repair-05 archived W09 attestation",
    )
    if active_w09.read_bytes() != archived_w09.read_bytes():
        raise MissionFinalizationError("repair-05 W09 attestation copies differ")

    previous_identity = repair04_context["current_identity"]
    current_identity = repair05.get("current_repository_identity")
    identity_chain = repair05.get("repository_identity_chain")
    parent_chain = repair04.get("repository_identity_chain")
    repository = manifest.get("repository")
    identity_fields = {
        "execution_commit", "source_manifest_sha256", "source_sha256s_sha256",
        "query_set_sha256", "query_files",
    }
    if (
        not isinstance(current_identity, dict)
        or set(current_identity) != identity_fields
        or not isinstance(parent_chain, list)
        or len(parent_chain) != 5
        or identity_chain != [*parent_chain, current_identity]
        or not isinstance(repository, dict)
        or repository.get("identity_history") != identity_chain
        or repository.get("initial_identity") != prefix["initial_identity"]
        or repository.get("previous_identity") != previous_identity
        or repository.get("registration_repair_id") != "repair-05"
        or repository.get("source_tree_dirty_at_freeze") is not False
        or any(repository.get(field) != current_identity[field] for field in identity_fields)
        or repair05.get("initial_repository_identity") != prefix["initial_identity"]
        or repair05.get("previous_repository_identity") != previous_identity
        or repair05.get("previous_execution_commit")
        != previous_identity["execution_commit"]
        or repair05.get("current_execution_commit")
        != current_identity["execution_commit"]
    ):
        raise MissionFinalizationError("repair-05 repository identity chain mismatch")
    if len(identity_chain) != 6 or len(
        {canonical_json_sha256(row) for row in identity_chain}
    ) != 6:
        raise MissionFinalizationError(
            "repair-05 repository identities are not six distinct boundaries"
        )
    for index, identity in enumerate(identity_chain):
        if not isinstance(identity, dict) or set(identity) != identity_fields:
            raise MissionFinalizationError(
                "repair-05 repository identity fields mismatch"
            )
        require_hex(identity["execution_commit"], f"repair-05 identity {index} commit", HEX40)
        for field in (
            "source_manifest_sha256", "source_sha256s_sha256", "query_set_sha256"
        ):
            require_hex(identity[field], f"repair-05 identity {index} {field}")
    for field in (
        "source_manifest_sha256", "source_sha256s_sha256", "query_set_sha256"
    ):
        if (
            repair05.get(f"previous_{field}") != previous_identity[field]
            or repair05.get(f"current_{field}") != current_identity[field]
        ):
            raise MissionFinalizationError(
                "repair-05 scalar repository boundary mismatch"
            )
    for root, identity in (
        (REPAIR_05_PRE_ROOT, previous_identity), (Path("."), current_identity)
    ):
        for relative, field in (
            (Path("SOURCE_MANIFEST.json"), "source_manifest_sha256"),
            (Path("SOURCE_SHA256SUMS.txt"), "source_sha256s_sha256"),
            (Path("QUERY_SHA256SUMS.txt"), "query_set_sha256"),
        ):
            require_path_hash(
                run_dir, root / relative, identity[field],
                f"repair-05 repository boundary {root / relative}",
            )
        if identity.get("query_files") != query_files:
            raise MissionFinalizationError("repair-05 changed query-file path set")
        for relative in query_files:
            checked_relative_path(
                run_dir, (root / relative).as_posix(), "repair-05 frozen query"
            )
    _validate_repair05_source_verification(
        run_dir,
        run_id=run_id,
        repair05=repair05,
        previous_identity=previous_identity,
        current_identity=current_identity,
    )
    active_query = checked_relative_path(
        run_dir, REPAIR_05_EXECUTION_QUERY.as_posix(),
        "repair-05 registered RFQ query",
    )
    if (
        sha256(active_query) != repair05.get("registered_rfq_query_sha256")
        or repair05.get("registered_rfq_query_sha256")
        != wiring["correction"]["current_registered_query_sha256"]
    ):
        raise MissionFinalizationError("repair-05 registered RFQ query mismatch")

    _validate_repair05_transaction(
        run_dir,
        run_id=run_id,
        manifest=manifest,
        repair05=repair05,
        pre_manifest_path=pre_manifest_path,
        query_files=query_files,
    )

    previous_registry = (run_dir / REPAIR_05_PRE_ROOT / "TRIAL_REGISTRY.jsonl").read_bytes()
    registry = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    trial = repair05.get("trial_registry")
    parent_trial = repair04.get("trial_registry", {})
    if (
        not isinstance(trial, dict)
        or set(trial) != {
            "previous_sha256", "current_sha256", "previous_bytes", "current_bytes",
            "strict_previous_bytes_prefix", "appended_records", "trial_registration_ids",
        }
        or trial.get("previous_bytes") != parent_trial.get("current_bytes")
        or previous_registry != registry[: len(previous_registry)]
        or trial.get("previous_sha256") != hashlib.sha256(previous_registry).hexdigest()
        or trial.get("previous_bytes") != len(previous_registry)
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("appended_records") != 2
        or trial.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_05", "RFQ_CONSUMER_VALIDATION_WIRING_REPAIR_05"]
        or not isinstance(trial.get("current_bytes"), int)
        or trial["current_bytes"] <= len(previous_registry)
        or len(registry) < trial["current_bytes"]
        or hashlib.sha256(registry[: trial["current_bytes"]]).hexdigest()
        != trial.get("current_sha256")
    ):
        raise MissionFinalizationError("repair-05 trial append chain mismatch")
    try:
        trial_rows = [
            json.loads(line)
            for line in registry[len(previous_registry): trial["current_bytes"]]
            .decode("utf-8").splitlines() if line
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MissionFinalizationError(f"invalid repair-05 trial append: {exc}") from exc
    common_trial = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(RFQ_TRIAL_ORDER),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": "repair-04",
    }
    expected_failure_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_05",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR04",
        "failure_class": repair05["finding"],
        "failure_disposition": repair05["failure_disposition"],
        "failure_phase": repair05["failure_phase"],
        "error_type": "KeyError", "error": "KeyError: 'cycle1_duckdb_binding'",
        "hypothesis_conclusion": "NONE",
        "blocker_path": REPAIR_05_BLOCKER_ARCHIVE.as_posix(),
        "blocker_sha256": blocker_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_05.as_posix(),
        "failed_resource_receipt_sha256": failed_resource_sha,
        "unchanged_state_path": UNCHANGED_RFQ_STATE_05.as_posix(),
        "unchanged_state_sha256": EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "unchanged_input_identity_path": UNCHANGED_RFQ_INPUT_IDENTITY_05.as_posix(),
        "unchanged_input_identity_sha256": EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
        "cycle1_binding_path": (
            REPAIR_05_PRE_ROOT / ACTIVE_CYCLE1_DUCKDB_BINDING
        ).as_posix(),
        "cycle1_binding_sha256": cycle_sha,
        "new_state_written": False, "new_input_identity_written": False,
        "active_scratch_absent": True, "active_wal_absent": True,
        "resource_label": "rfq_full_stage_repair04", "return_code": 1,
        "resource_metrics": {
            key: failed_resource[key] for key in (
                "started_at_utc", "completed_at_utc", "wall_seconds",
                "cpu_user_seconds", "cpu_system_seconds",
                "peak_process_tree_rss_kib_polled", "peak_temp_bytes_polled",
                "minimum_disk_free_bytes_polled",
            )
        },
        "execution_commit": previous_identity["execution_commit"],
        "source_manifest_sha256": previous_identity["source_manifest_sha256"],
        "source_sha256s_sha256": previous_identity["source_sha256s_sha256"],
        "query_set_sha256": previous_identity["query_set_sha256"],
    }
    expected_prereg_trial = {
        **common_trial,
        "trial_registration_id": "RFQ_CONSUMER_VALIDATION_WIRING_REPAIR_05",
        "record_type": "CONSUMER_VALIDATION_WIRING_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR05_PREREGISTRATION",
        "finding": repair05["finding"],
        "registration_change_class": repair05["registration_change_class"],
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": repair05["retry_requirement"],
        "wiring_contract_path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "wiring_contract_sha256": wiring_sha,
        "authority_basis_path": REPAIR_05_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": repair04_context["resource_contract_sha256"],
        "runtime": wiring["inherited_contracts"]["runtime"],
        "expected_success_resource": repair05["expected_success_resource"],
        "newly_quarantined_objects": [], "coverage": repair05["coverage"],
        "previous_selection_fingerprint_sha256": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "current_selection_fingerprint_sha256": EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "previous_execution_commit": previous_identity["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": previous_identity["source_manifest_sha256"],
        "previous_source_sha256s_sha256": previous_identity["source_sha256s_sha256"],
        "current_source_manifest_sha256": current_identity["source_manifest_sha256"],
        "current_source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "previous_query_set_sha256": previous_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "data_selection_change": "NONE", "parser_contract_change": "NONE",
        "query_semantics_change": "NONE", "quarantine_change": "NONE",
        "resource_contract_change": "NONE", "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    if trial_rows != [expected_failure_trial, expected_prereg_trial]:
        raise MissionFinalizationError("repair-05 trial governance boundary mismatch")

    return _validate_repair05_success_outputs(
        run_dir=run_dir,
        manifest=manifest,
        rfq=rfq,
        repair05=repair05,
        repair05_receipt_sha=repair05_receipt_sha,
        wiring_contract_sha=wiring_sha,
        authority_sha=authority_sha,
        repair04_context=repair04_context,
        current_identity=current_identity,
    )


def _validate_repair04_success_outputs(
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    repair04: Mapping[str, Any],
    repair04_receipt_sha: str,
    resource_contract_sha: str,
    authority_sha: str,
    repair03_context: Mapping[str, Any],
    failed: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate attempt 05 under the exact repair-04 resource envelope."""
    run_id = manifest["run_id"]
    summary_path = checked_relative_path(
        run_dir, RFQ_SUMMARY.as_posix(), "repair-04 RFQ summary"
    )
    if load_json(summary_path) != dict(rfq):
        raise MissionFinalizationError(
            "repair-04 RFQ summary argument/file binding mismatch"
        )
    (
        expected_repair_chain,
        expected_failed_bindings,
        parser_binding,
        resource_binding,
    ) = _repair04_runtime_bindings(
        repair03_context=repair03_context,
        repair04_receipt_sha=repair04_receipt_sha,
        resource_contract_sha=resource_contract_sha,
        authority_sha=authority_sha,
        failed=failed,
    )
    expected_resource = {
        "label": "rfq_full_stage_repair04",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
    }
    registered_query_sha = repair04.get("registered_rfq_query_sha256")
    require_hex(registered_query_sha, "repair-04 registered RFQ query SHA-256")
    prefix = repair03_context["prefix"]

    rfq_input = rfq.get("input")
    if not isinstance(rfq_input, dict):
        raise MissionFinalizationError("repair-04 RFQ summary input is missing")
    _validate_repair03_selection_identity(
        {**rfq_input, "run_id": run_id},
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=False,
    )
    if (
        rfq.get("schema") != "sports-autoresearch-rfq-full-stage-v1"
        or rfq.get("run_id") != run_id
        or rfq.get("status") != RFQ_PARTIAL_STATUS
        or rfq.get("analysis_scope") != RFQ_ANALYSIS_SCOPE
        or rfq_input.get("failed_attempt_binding") is not None
        or rfq_input.get("repair_chain") != expected_repair_chain
        or rfq_input.get("failed_attempt_bindings") != expected_failed_bindings
        or rfq_input.get("expected_success_resource") != expected_resource
        or rfq_input.get("inner_payload_parser_contract") != parser_binding
        or rfq_input.get("registered_rfq_query_sha256") != registered_query_sha
        or rfq_input.get("rfq_resource_contract") != resource_binding
        or rfq_input.get("outer_parser")
        != "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort"
        or rfq_input.get("overlap_keys") != prefix["overlap_keys"]
    ):
        raise MissionFinalizationError(
            "repair-04 RFQ summary governance binding mismatch"
        )
    summary_generated_key = _utc_timestamp_key(
        rfq.get("generated_at_utc"), "repair-04 RFQ summary generation"
    )

    active_identity = load_json(
        checked_relative_path(
            run_dir, RFQ_INPUT_IDENTITY.as_posix(), "repair-04 active input identity"
        )
    )
    _validate_repair03_selection_identity(
        active_identity,
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=True,
    )
    if (
        active_identity.get("schema") != "rfq-full-input-identity-v4"
        or active_identity.get("releases") != prefix["release_input_receipts"]
        or active_identity.get("repair_chain") != expected_repair_chain
        or active_identity.get("failed_attempt_bindings")
        != expected_failed_bindings
        or active_identity.get("expected_success_resource") != expected_resource
        or active_identity.get("inner_payload_parser_contract") != parser_binding
        or active_identity.get("registered_rfq_query_sha256")
        != registered_query_sha
        or active_identity.get("rfq_resource_contract") != resource_binding
    ):
        raise MissionFinalizationError("repair-04 active input identity mismatch")

    # Repair-04 may change only append-only governance and the execution
    # resource envelope.  Parser, object selection, quarantine, gaps, and all
    # other input semantics must remain exactly equal to failed attempt 04.
    mutable_identity_fields = {
        "repair_chain",
        "failed_attempt_bindings",
        "expected_success_resource",
        "registered_rfq_query_sha256",
        "rfq_resource_contract",
        "schema",
    }
    failed_stable = {
        key: value
        for key, value in failed["input"].items()
        if key not in mutable_identity_fields
    }
    active_stable = {
        key: value
        for key, value in active_identity.items()
        if key not in mutable_identity_fields
    }
    if active_stable != failed_stable:
        raise MissionFinalizationError(
            "repair-04 changed stable RFQ input identity fields"
        )

    active_state_path = checked_relative_path(
        run_dir, ACTIVE_RFQ_STATE.as_posix(), "repair-04 completed RFQ state"
    )
    active_state = load_json(active_state_path)
    _validate_repair02_success_envelope(
        rfq_input=rfq_input,
        active_identity=active_identity,
        active_state=active_state,
        summary_expected=dict(rfq_input),
        identity_expected=active_identity,
        run_id=run_id,
        selection_sha=EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        repair_chain=expected_repair_chain,
        failed_bindings=expected_failed_bindings,
        gap_plan=prefix["quarantine_gap_plan"],
        expected_resource=expected_resource,
    )
    if (
        active_state.get("registered_rfq_query_sha256") != registered_query_sha
        or active_state.get("inner_payload_parser_contract") != parser_binding
        or active_state.get("rfq_resource_contract") != resource_binding
    ):
        raise MissionFinalizationError(
            "repair-04 completion state parser/query/resource binding mismatch"
        )
    state_started_key = _utc_timestamp_key(
        active_state.get("started_at_utc"), "repair-04 running state start"
    )
    state_completed_key = _utc_timestamp_key(
        active_state.get("completed_at_utc"), "repair-04 completion state"
    )
    active_state_sha = sha256(active_state_path)

    coverage = rfq.get("coverage")
    counts = rfq.get("counts")
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise MissionFinalizationError(
            "repair-04 RFQ coverage/count receipts are missing"
        )
    _validate_rfq_quarantine_boundary_counts(coverage, counts)
    gaps = coverage.get("quarantine_gap_ranges")
    if (
        coverage.get("rfq_object_coverage") != RFQ_PARTIAL_STATUS
        or not isinstance(gaps, list)
        or len(gaps) != 1
        or coverage.get("quarantine_gap_set_sha256")
        != canonical_json_sha256(gaps)
        or coverage.get("quarantined_hours_are_not_observed_zero") is not True
        or coverage.get("capture_completeness_is_not_lifecycle_join_completeness")
        is not True
    ):
        raise MissionFinalizationError(
            "repair-04 RFQ quarantine-gap semantics mismatch"
        )
    gap = gaps[0]
    if not isinstance(gap, dict):
        raise MissionFinalizationError("repair-04 RFQ gap receipt is invalid")
    gap_start = require_positive_int(gap.get("gap_start_ns"), "repair-04 gap start")
    gap_end = require_positive_int(gap.get("gap_end_ns"), "repair-04 gap end")
    if gap_end <= gap_start or gap.get("quarantined_object_count") != 2:
        raise MissionFinalizationError("repair-04 RFQ gap range is invalid")
    if (
        gap.get("quarantined_object_set_sha256")
        != prefix["quarantined_set_sha256"]
        or gap.get("boundary_reason")
        != "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON"
    ):
        raise MissionFinalizationError("repair-04 RFQ gap identity changed")
    gap_csv = _read_csv_rows(
        run_dir / RFQ_QUARANTINE_GAPS,
        {
            "release_id",
            "key",
            "sha256",
            "quarantined_keys_json",
            "quarantined_object_count",
            "quarantined_object_set_sha256",
            "previous_filename",
            "next_filename",
            "gap_start_ns",
            "gap_end_ns",
            "gap_start_us",
            "gap_end_us",
            "boundary_reason",
        },
        "repair-04 RFQ quarantine gaps",
    )
    if len(gap_csv) != 1:
        raise MissionFinalizationError("repair-04 gap table must contain one row")
    for field, value in gap.items():
        if field not in gap_csv[0]:
            raise MissionFinalizationError(
                f"repair-04 gap-table field missing: {field}"
            )
        observed: Any = gap_csv[0][field]
        if isinstance(value, int):
            try:
                observed = int(observed)
            except ValueError as exc:
                raise MissionFinalizationError(
                    f"repair-04 invalid gap-table integer: {field}"
                ) from exc
        if observed != value:
            raise MissionFinalizationError(
                f"repair-04 gap-table mismatch: {field}"
            )
    partial_hours = require_positive_int(
        coverage.get("partial_object_coverage_hours"),
        "repair-04 partial-object coverage hours",
    )
    hour_rows = _read_csv_rows(
        run_dir / RFQ_HOUR_COVERAGE,
        {"object_coverage_status", "zero_interpretation"},
        "repair-04 RFQ hour coverage",
    )
    partial_rows = [
        row
        for row in hour_rows
        if row.get("object_coverage_status") == RFQ_PARTIAL_STATUS
    ]
    if len(partial_rows) != partial_hours or any(
        row.get("zero_interpretation")
        != "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP"
        for row in partial_rows
    ):
        raise MissionFinalizationError("repair-04 partial-hour semantics mismatch")

    success_path = checked_relative_path(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
        "repair-04 successful RFQ resource",
    )
    success = load_json(success_path)
    resolved_run_dir = run_dir.resolve()
    expected_command = [
        "/opt/w09/venv/bin/python",
        str(resolved_run_dir / REPAIR_04_EXECUTION_QUERY),
        "--run-dir",
        str(resolved_run_dir),
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "46GB",
        "--max-temp-size",
        "70GB",
        "--threads",
        "4",
        "--min-free-gib",
        "100",
        "--clob-max-per-root",
        "50",
    ]
    wall_seconds = success.get("wall_seconds")
    if (
        success.get("schema_version") != "w09-stage-resource-v1"
        or success.get("label") != "rfq_full_stage_repair04"
        or success.get("return_code") != 0
        or success.get("command") != expected_command
        or isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or wall_seconds <= 0
        or success.get("s3_bytes_read_by_analysis", 0) != 0
    ):
        raise MissionFinalizationError(
            "repair-04 successful resource receipt mismatch"
        )
    resource_started_key = _utc_timestamp_key(
        success.get("started_at_utc"), "repair-04 resource start"
    )
    resource_completed_key = _utc_timestamp_key(
        success.get("completed_at_utc"), "repair-04 resource completion"
    )
    if not (
        resource_started_key
        <= state_started_key
        <= summary_generated_key
        <= state_completed_key
        <= resource_completed_key
    ):
        raise MissionFinalizationError(
            "repair-04 resource/summary/state time order mismatch"
        )
    executed_query = checked_relative_path(
        run_dir,
        REPAIR_04_EXECUTION_QUERY.as_posix(),
        "repair-04 executed frozen RFQ query",
    )
    if sha256(executed_query) != registered_query_sha:
        raise MissionFinalizationError(
            "repair-04 executed/frozen/registered RFQ query mismatch"
        )
    success_sha = sha256(success_path)

    details = prefix["quarantine_details"]
    repair03 = repair03_context["repair03"]
    repair02 = prefix["repair02"]
    return {
        "status": RFQ_PARTIAL_STATUS,
        "analysis_scope": RFQ_ANALYSIS_SCOPE,
        "retained_population": "RETAINED_OBSERVED_SUBSET",
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_objects": list(details),
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "manifest_object_set_sha256": prefix["full_set_sha256"],
        "consumed_object_set_sha256": prefix["retained_set_sha256"],
        "quarantined_object_set_sha256": prefix["quarantined_set_sha256"],
        "selection_fingerprint_sha256": prefix["selection_fingerprint_sha256"],
        "quarantine_gap_plan": prefix["quarantine_gap_plan"],
        "quarantine_gap_set_sha256": canonical_json_sha256(gaps),
        "quarantine_gap": gap,
        "partial_object_coverage_hours": partial_hours,
        "initial_execution_commit": prefix["initial_identity"]["execution_commit"],
        "previous_execution_commit": repair03_context["current_identity"][
            "execution_commit"
        ],
        "current_execution_commit": current_identity["execution_commit"],
        "initial_source_manifest_sha256": prefix["initial_identity"][
            "source_manifest_sha256"
        ],
        "previous_source_manifest_sha256": repair03_context["current_identity"][
            "source_manifest_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "initial_source_sha256s_sha256": prefix["initial_identity"][
            "source_sha256s_sha256"
        ],
        "previous_source_sha256s_sha256": repair03_context["current_identity"][
            "source_sha256s_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "initial_query_set_sha256": prefix["initial_identity"][
            "query_set_sha256"
        ],
        "previous_query_set_sha256": repair03_context["current_identity"][
            "query_set_sha256"
        ],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "repair_chain": expected_repair_chain,
        "failed_attempt_bindings": expected_failed_bindings,
        "declaration_path": RFQ_DECLARATION_02.as_posix(),
        "declaration_sha256": repair03.get(
            "inherited_declaration_sha256", repair02.get("declaration_sha256")
        ),
        "receipt_path": RFQ_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": repair03.get(
            "inherited_receipt_sha256", repair02.get("receipt_sha256")
        ),
        "authorization_path": RFQ_REPAIR_02_AUTHORIZATION.as_posix(),
        "authorization_sha256": prefix["authorization02_sha256"],
        "repair_receipt_path": REPAIR_REGISTRATION_RECEIPT_04.as_posix(),
        "repair_receipt_sha256": repair04_receipt_sha,
        "failed_state_path": FAILED_RFQ_STATE_04.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_04.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "inner_payload_audit_path": ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "inner_payload_audit_sha256": repair03_context[
            "inner_payload_audit_sha256"
        ],
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": repair03_context["parser_contract_sha256"],
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": resource_contract_sha,
        "authority_basis_path": REPAIR_04_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "cycle1_duckdb_binding": prefix["cycle1_duckdb_binding"],
        "completed_state_path": ACTIVE_RFQ_STATE.as_posix(),
        "completed_state_sha256": active_state_sha,
        "successful_resource_receipt_path": (
            ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix()
        ),
        "successful_resource_receipt_sha256": success_sha,
        "claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }


def _validate_repair05_success_outputs(
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    repair05: Mapping[str, Any],
    repair05_receipt_sha: str,
    wiring_contract_sha: str,
    authority_sha: str,
    repair04_context: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the repair-05 success under unchanged data/resource semantics."""
    run_id = manifest["run_id"]
    summary_path = checked_relative_path(
        run_dir, RFQ_SUMMARY.as_posix(), "repair-05 RFQ summary"
    )
    if load_json(summary_path) != dict(rfq):
        raise MissionFinalizationError(
            "repair-05 RFQ summary argument/file binding mismatch"
        )
    (
        expected_repair_chain,
        expected_failed_bindings,
        parser_binding,
        resource_binding,
        wiring_binding,
    ) = _repair05_runtime_bindings(
        repair04_context=repair04_context,
        repair05_receipt_sha=repair05_receipt_sha,
        wiring_contract_sha=wiring_contract_sha,
        authority_sha=authority_sha,
    )
    expected_resource = {
        "label": "rfq_full_stage_repair05",
        "path": ACTIVE_RFQ_REPAIR_RESOURCE_05.as_posix(),
    }
    registered_query_sha = require_hex(
        repair05.get("registered_rfq_query_sha256"),
        "repair-05 registered RFQ query SHA-256",
    )
    repair03_context = repair04_context["repair03_context"]
    prefix = repair03_context["prefix"]

    rfq_input = rfq.get("input")
    if not isinstance(rfq_input, dict):
        raise MissionFinalizationError("repair-05 RFQ summary input is missing")
    _validate_repair03_selection_identity(
        {**rfq_input, "run_id": run_id},
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=False,
    )
    if (
        rfq.get("schema") != "sports-autoresearch-rfq-full-stage-v1"
        or rfq.get("run_id") != run_id
        or rfq.get("status") != RFQ_PARTIAL_STATUS
        or rfq.get("analysis_scope") != RFQ_ANALYSIS_SCOPE
        or rfq_input.get("failed_attempt_binding") is not None
        or rfq_input.get("repair_chain") != expected_repair_chain
        or rfq_input.get("failed_attempt_bindings") != expected_failed_bindings
        or rfq_input.get("expected_success_resource") != expected_resource
        or rfq_input.get("inner_payload_parser_contract") != parser_binding
        or rfq_input.get("registered_rfq_query_sha256") != registered_query_sha
        or rfq_input.get("rfq_resource_contract") != resource_binding
        or rfq_input.get("consumer_wiring_contract") != wiring_binding
        or rfq_input.get("outer_parser")
        != "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort"
        or rfq_input.get("overlap_keys") != prefix["overlap_keys"]
    ):
        raise MissionFinalizationError(
            "repair-05 RFQ summary governance binding mismatch"
        )
    summary_generated_key = _utc_timestamp_key(
        rfq.get("generated_at_utc"), "repair-05 RFQ summary generation"
    )

    active_identity_path = checked_relative_path(
        run_dir, RFQ_INPUT_IDENTITY.as_posix(), "repair-05 active input identity"
    )
    active_identity = load_json(active_identity_path)
    _validate_repair03_selection_identity(
        active_identity,
        prefix=prefix,
        run_id=run_id,
        require_consumed_objects=True,
    )
    if (
        active_identity.get("schema") != "rfq-full-input-identity-v5"
        or active_identity.get("releases") != prefix["release_input_receipts"]
        or active_identity.get("repair_chain") != expected_repair_chain
        or active_identity.get("failed_attempt_bindings")
        != expected_failed_bindings
        or active_identity.get("expected_success_resource") != expected_resource
        or active_identity.get("inner_payload_parser_contract") != parser_binding
        or active_identity.get("registered_rfq_query_sha256")
        != registered_query_sha
        or active_identity.get("rfq_resource_contract") != resource_binding
        or active_identity.get("consumer_wiring_contract") != wiring_binding
    ):
        raise MissionFinalizationError("repair-05 active input identity mismatch")

    # Attempt 05 failed before writing evidence, so the only permitted input
    # changes from attempt 04 are append-only governance, the query source
    # identity for this wiring correction, and the schema version.
    mutable_identity_fields = {
        "repair_chain",
        "failed_attempt_bindings",
        "expected_success_resource",
        "registered_rfq_query_sha256",
        "rfq_resource_contract",
        "consumer_wiring_contract",
        "schema",
    }
    failed04_identity = repair04_context["failed"]["input"]
    failed_stable = {
        key: value
        for key, value in failed04_identity.items()
        if key not in mutable_identity_fields
    }
    active_stable = {
        key: value
        for key, value in active_identity.items()
        if key not in mutable_identity_fields
    }
    if active_stable != failed_stable:
        raise MissionFinalizationError(
            "repair-05 changed stable RFQ input identity fields"
        )

    active_state_path = checked_relative_path(
        run_dir, ACTIVE_RFQ_STATE.as_posix(), "repair-05 completed RFQ state"
    )
    active_state = load_json(active_state_path)
    _validate_repair02_success_envelope(
        rfq_input=rfq_input,
        active_identity=active_identity,
        active_state=active_state,
        summary_expected=dict(rfq_input),
        identity_expected=active_identity,
        run_id=run_id,
        selection_sha=EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        repair_chain=expected_repair_chain,
        failed_bindings=expected_failed_bindings,
        gap_plan=prefix["quarantine_gap_plan"],
        expected_resource=expected_resource,
    )
    if (
        active_state.get("registered_rfq_query_sha256") != registered_query_sha
        or active_state.get("inner_payload_parser_contract") != parser_binding
        or active_state.get("rfq_resource_contract") != resource_binding
        or active_state.get("consumer_wiring_contract") != wiring_binding
    ):
        raise MissionFinalizationError(
            "repair-05 completion state governance binding mismatch"
        )
    state_started_key = _utc_timestamp_key(
        active_state.get("started_at_utc"), "repair-05 running state start"
    )
    state_completed_key = _utc_timestamp_key(
        active_state.get("completed_at_utc"), "repair-05 completion state"
    )
    active_state_sha = sha256(active_state_path)

    coverage = rfq.get("coverage")
    counts = rfq.get("counts")
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise MissionFinalizationError(
            "repair-05 RFQ coverage/count receipts are missing"
        )
    _validate_rfq_quarantine_boundary_counts(coverage, counts)
    gaps = coverage.get("quarantine_gap_ranges")
    if (
        coverage.get("rfq_object_coverage") != RFQ_PARTIAL_STATUS
        or not isinstance(gaps, list)
        or len(gaps) != 1
        or coverage.get("quarantine_gap_set_sha256")
        != canonical_json_sha256(gaps)
        or coverage.get("quarantined_hours_are_not_observed_zero") is not True
        or coverage.get("capture_completeness_is_not_lifecycle_join_completeness")
        is not True
    ):
        raise MissionFinalizationError(
            "repair-05 RFQ quarantine-gap semantics mismatch"
        )
    gap = gaps[0]
    if not isinstance(gap, dict):
        raise MissionFinalizationError("repair-05 RFQ gap receipt is invalid")
    gap_start = require_positive_int(gap.get("gap_start_ns"), "repair-05 gap start")
    gap_end = require_positive_int(gap.get("gap_end_ns"), "repair-05 gap end")
    if (
        gap_end <= gap_start
        or gap.get("quarantined_object_count") != 2
        or gap.get("quarantined_object_set_sha256")
        != prefix["quarantined_set_sha256"]
        or gap.get("boundary_reason")
        != "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON"
    ):
        raise MissionFinalizationError("repair-05 RFQ gap identity changed")
    gap_csv = _read_csv_rows(
        run_dir / RFQ_QUARANTINE_GAPS,
        {
            "release_id", "key", "sha256", "quarantined_keys_json",
            "quarantined_object_count", "quarantined_object_set_sha256",
            "previous_filename", "next_filename", "gap_start_ns", "gap_end_ns",
            "gap_start_us", "gap_end_us", "boundary_reason",
        },
        "repair-05 RFQ quarantine gaps",
    )
    if len(gap_csv) != 1:
        raise MissionFinalizationError("repair-05 gap table must contain one row")
    for field, value in gap.items():
        if field not in gap_csv[0]:
            raise MissionFinalizationError(
                f"repair-05 gap-table field missing: {field}"
            )
        observed: Any = gap_csv[0][field]
        if isinstance(value, int):
            try:
                observed = int(observed)
            except ValueError as exc:
                raise MissionFinalizationError(
                    f"repair-05 invalid gap-table integer: {field}"
                ) from exc
        if observed != value:
            raise MissionFinalizationError(
                f"repair-05 gap-table mismatch: {field}"
            )
    partial_hours = require_positive_int(
        coverage.get("partial_object_coverage_hours"),
        "repair-05 partial-object coverage hours",
    )
    hour_rows = _read_csv_rows(
        run_dir / RFQ_HOUR_COVERAGE,
        {"object_coverage_status", "zero_interpretation"},
        "repair-05 RFQ hour coverage",
    )
    partial_rows = [
        row
        for row in hour_rows
        if row.get("object_coverage_status") == RFQ_PARTIAL_STATUS
    ]
    if len(partial_rows) != partial_hours or any(
        row.get("zero_interpretation")
        != "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP"
        for row in partial_rows
    ):
        raise MissionFinalizationError("repair-05 partial-hour semantics mismatch")

    success_path = checked_relative_path(
        run_dir,
        ACTIVE_RFQ_REPAIR_RESOURCE_05.as_posix(),
        "repair-05 successful RFQ resource",
    )
    success = load_json(success_path)
    resolved_run_dir = run_dir.resolve()
    expected_command = [
        "/opt/w09/venv/bin/python",
        str(resolved_run_dir / REPAIR_05_EXECUTION_QUERY),
        "--run-dir",
        str(resolved_run_dir),
        "--cache-root",
        "/srv/w09-research/cache",
        "--memory-limit",
        "46GB",
        "--max-temp-size",
        "70GB",
        "--threads",
        "4",
        "--min-free-gib",
        "100",
        "--clob-max-per-root",
        "50",
    ]
    wall_seconds = success.get("wall_seconds")
    if (
        success.get("schema_version") != "w09-stage-resource-v1"
        or success.get("label") != "rfq_full_stage_repair05"
        or success.get("return_code") != 0
        or success.get("command") != expected_command
        or isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or wall_seconds <= 0
        or success.get("s3_bytes_read_by_analysis", 0) != 0
    ):
        raise MissionFinalizationError(
            "repair-05 successful resource receipt mismatch"
        )
    resource_started_key = _utc_timestamp_key(
        success.get("started_at_utc"), "repair-05 resource start"
    )
    resource_completed_key = _utc_timestamp_key(
        success.get("completed_at_utc"), "repair-05 resource completion"
    )
    if not (
        resource_started_key
        <= state_started_key
        <= summary_generated_key
        <= state_completed_key
        <= resource_completed_key
    ):
        raise MissionFinalizationError(
            "repair-05 resource/summary/state time order mismatch"
        )
    executed_query = checked_relative_path(
        run_dir,
        REPAIR_05_EXECUTION_QUERY.as_posix(),
        "repair-05 executed frozen RFQ query",
    )
    if sha256(executed_query) != registered_query_sha:
        raise MissionFinalizationError(
            "repair-05 executed/frozen/registered RFQ query mismatch"
        )

    repair03 = repair03_context["repair03"]
    repair02 = prefix["repair02"]
    return {
        "status": RFQ_PARTIAL_STATUS,
        "analysis_scope": RFQ_ANALYSIS_SCOPE,
        "retained_population": "RETAINED_OBSERVED_SUBSET",
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_objects": list(prefix["quarantine_details"]),
        "unique_objects_total": EXPECTED_RFQ_FULL_OBJECTS,
        "consumed_unique_objects": EXPECTED_RFQ_RETAINED_OBJECTS,
        "quarantined_unique_objects": EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "unique_bytes_total": EXPECTED_RFQ_FULL_BYTES,
        "consumed_bytes": EXPECTED_RFQ_RETAINED_BYTES,
        "quarantined_bytes": EXPECTED_RFQ_QUARANTINED_BYTES,
        "manifest_object_set_sha256": prefix["full_set_sha256"],
        "consumed_object_set_sha256": prefix["retained_set_sha256"],
        "quarantined_object_set_sha256": prefix["quarantined_set_sha256"],
        "selection_fingerprint_sha256": prefix["selection_fingerprint_sha256"],
        "quarantine_gap_plan": prefix["quarantine_gap_plan"],
        "quarantine_gap_set_sha256": canonical_json_sha256(gaps),
        "quarantine_gap": gap,
        "partial_object_coverage_hours": partial_hours,
        "initial_execution_commit": prefix["initial_identity"]["execution_commit"],
        "previous_execution_commit": repair04_context["current_identity"][
            "execution_commit"
        ],
        "current_execution_commit": current_identity["execution_commit"],
        "initial_source_manifest_sha256": prefix["initial_identity"][
            "source_manifest_sha256"
        ],
        "previous_source_manifest_sha256": repair04_context["current_identity"][
            "source_manifest_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "initial_source_sha256s_sha256": prefix["initial_identity"][
            "source_sha256s_sha256"
        ],
        "previous_source_sha256s_sha256": repair04_context["current_identity"][
            "source_sha256s_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "initial_query_set_sha256": prefix["initial_identity"][
            "query_set_sha256"
        ],
        "previous_query_set_sha256": repair04_context["current_identity"][
            "query_set_sha256"
        ],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "repair_chain": expected_repair_chain,
        "failed_attempt_bindings": expected_failed_bindings,
        "declaration_path": RFQ_DECLARATION_02.as_posix(),
        "declaration_sha256": repair03.get(
            "inherited_declaration_sha256", repair02.get("declaration_sha256")
        ),
        "receipt_path": RFQ_MALFORMED_RECEIPT_02.as_posix(),
        "receipt_sha256": repair03.get(
            "inherited_receipt_sha256", repair02.get("receipt_sha256")
        ),
        "authorization_path": RFQ_REPAIR_02_AUTHORIZATION.as_posix(),
        "authorization_sha256": prefix["authorization02_sha256"],
        "repair_receipt_path": REPAIR_REGISTRATION_RECEIPT_05.as_posix(),
        "repair_receipt_sha256": repair05_receipt_sha,
        "failed_resource_receipt_path": FAILED_RFQ_RESOURCE_05.as_posix(),
        "failed_resource_receipt_sha256": EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256,
        "consumer_blocker_path": REPAIR_05_BLOCKER_ARCHIVE.as_posix(),
        "consumer_blocker_sha256": (
            EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256
        ),
        "inner_payload_audit_path": (
            ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix()
        ),
        "inner_payload_audit_sha256": repair03_context[
            "inner_payload_audit_sha256"
        ],
        "parser_contract_path": REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": repair03_context["parser_contract_sha256"],
        "resource_contract_path": REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": repair04_context[
            "resource_contract_sha256"
        ],
        "wiring_contract_path": REPAIR_05_WIRING_CONTRACT.as_posix(),
        "wiring_contract_sha256": wiring_contract_sha,
        "authority_basis_path": REPAIR_05_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "cycle1_duckdb_binding": prefix["cycle1_duckdb_binding"],
        "completed_state_path": ACTIVE_RFQ_STATE.as_posix(),
        "completed_state_sha256": active_state_sha,
        "successful_resource_receipt_path": (
            ACTIVE_RFQ_REPAIR_RESOURCE_05.as_posix()
        ),
        "successful_resource_receipt_sha256": sha256(success_path),
        "claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }


def find_l2_summary(run_dir: Path) -> Path:
    existing = [path for path in L2_SUMMARY_CANDIDATES if (run_dir / path).is_file()]
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        raise MissionFinalizationError(
            "multiple canonical L2 summary candidates exist: "
            + ", ".join(str(item) for item in existing)
        )
    # Schema-adaptive fallback is limited to REPORT JSONs that explicitly bind
    # both frozen L2 hypothesis ids.  Ambiguity is fatal.
    matches: list[Path] = []
    for absolute in sorted((run_dir / "REPORT").rglob("*.json")):
        relative = absolute.relative_to(run_dir)
        if relative in {CORE_SUMMARY, CORE_HYPOTHESIS_SUMMARY, RFQ_SUMMARY, RFQ_CONCLUSIONS}:
            continue
        try:
            document = load_json(absolute)
        except MissionFinalizationError:
            continue
        text = json.dumps(document, sort_keys=True)
        schema = str(document.get("schema_version", document.get("schema", ""))).lower()
        if "l2" in schema and all(hypothesis_id in text for hypothesis_id in L2_TEST_IDS):
            matches.append(relative)
    if len(matches) != 1:
        raise MissionFinalizationError(
            "expected exactly one L2 hypothesis summary; found "
            + str([str(item) for item in matches])
        )
    return matches[0]


def read_size_observation_counts(run_dir: Path) -> dict[str, int]:
    path = run_dir / "REPORT/tables/rfq_size_summary.csv"
    if not path.is_file():
        raise MissionFinalizationError("RFQ size summary table is missing")
    contracts_n = 0
    target_n = 0
    rows = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"contracts_n", "target_n"} <= set(reader.fieldnames):
                raise MissionFinalizationError("RFQ size summary columns are incomplete")
            for row in reader:
                rows += 1
                for key in ("contracts_n", "target_n"):
                    raw = row.get(key)
                    try:
                        number = int(raw or 0)
                    except ValueError as exc:
                        raise MissionFinalizationError(
                            f"RFQ size summary has invalid {key}: {raw!r}"
                        ) from exc
                    if number < 0:
                        raise MissionFinalizationError("RFQ size counts cannot be negative")
                    if key == "contracts_n":
                        contracts_n += number
                    else:
                        target_n += number
    except OSError as exc:
        raise MissionFinalizationError(f"cannot read RFQ size summary: {exc}") from exc
    return {"table_rows": rows, "contracts_n": contracts_n, "target_n": target_n}


def build_rfq_conclusions(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    quarantine: Mapping[str, Any],
    *,
    completed_at_utc: str,
) -> dict[str, Any]:
    """Apply the frozen, sign-independent conservative RFQ terminal rule.

    Necessary input/joinable sample equal to zero => DATA_STARVED.  Otherwise
    every RFQ card is COLLECT_MORE because two degraded day blocks cannot
    complete its frozen dependent test.  No effect, p-value, sign, or chart is
    consulted by this rule.
    """

    counts = rfq.get("counts")
    clob = rfq.get("clob")
    if not isinstance(counts, dict) or not isinstance(clob, dict):
        raise MissionFinalizationError("RFQ summary lacks counts/clob receipts")
    valid_requests = require_nonnegative_int(
        counts.get("valid_requests"), "RFQ counts.valid_requests"
    )
    observed_deletes = require_nonnegative_int(
        counts.get("observed_first_valid_deletes"),
        "RFQ counts.observed_first_valid_deletes",
    )
    right_censored = require_nonnegative_int(
        counts.get("right_censored_creates"),
        "RFQ counts.right_censored_creates",
    )
    matched_pairs = require_nonnegative_int(
        clob.get("matched_pairs"), "RFQ clob.matched_pairs"
    )
    causal_anchors = require_nonnegative_int(
        clob.get("causal_eligible_endpoint_anchors"),
        "RFQ clob.causal_eligible_endpoint_anchors",
    )
    size_counts = read_size_observation_counts(run_dir)
    size_observations = size_counts["contracts_n"] + size_counts["target_n"]

    def terminal(has_necessary_input: bool) -> str:
        return "COLLECT_MORE" if has_necessary_input else "DATA_STARVED"

    conclusions = {
        "C1-RFQ-CLOB-01": {
            "hypothesis_id": "C1-RFQ-CLOB-01",
            "hypothesis_status": terminal(
                valid_requests > 0 and causal_anchors > 0 and matched_pairs > 0
            ),
            "status_reason": (
                "Required RFQ-to-CLOB matched samples exist, but the frozen negative-control "
                "suite, full matching/balance proof, executable fee model and economic-cost "
                "test are incomplete across only two degraded days. "
                + RFQ_PARTIAL_BOUNDARY
                if valid_requests > 0 and causal_anchors > 0 and matched_pairs > 0
                else "No causally eligible matched RFQ-to-CLOB sample exists for the frozen test. "
                + RFQ_PARTIAL_BOUNDARY
            ),
            "necessary_input_rule": {
                "valid_requests_gt_zero": valid_requests > 0,
                "causal_eligible_endpoint_anchors_gt_zero": causal_anchors > 0,
                "matched_pairs_gt_zero": matched_pairs > 0,
            },
            "unfinished_components": [
                "future/unrelated-event/market-label negative controls",
                "complete matched-balance assessment",
                "executable fee, latency, slippage and capacity economics",
                "additional independent sealed day blocks",
            ],
            "reopen_condition": (
                RFQ_REOPEN_CONDITION + " Then complete the frozen matched negative-control "
                "and executable-cost study."
            ),
        },
        "C1-ANOM-RFQ-SIZE-TAIL-01": {
            "hypothesis_id": "C1-ANOM-RFQ-SIZE-TAIL-01",
            "hypothesis_status": terminal(valid_requests > 0 and size_observations > 0),
            "status_reason": (
                "Validated RFQ size observations exist, but the dependent tail-vs-ordinary "
                "matched survival/CLOB-impact test, unit-rescaling control and economic-cost "
                "test are incomplete across only two degraded days. "
                + RFQ_PARTIAL_BOUNDARY
                if valid_requests > 0 and size_observations > 0
                else "No validated RFQ size observation exists for the frozen dependent test. "
                + RFQ_PARTIAL_BOUNDARY
            ),
            "necessary_input_rule": {
                "valid_requests_gt_zero": valid_requests > 0,
                "validated_size_observations_gt_zero": size_observations > 0,
            },
            "unfinished_components": [
                "tail-vs-ordinary matched dependent test",
                "censor-aware survival contrast",
                "unit-rescaling and pseudo-RFQ controls",
                "root-event clustered CLOB impact and economic-cost test",
            ],
            "reopen_condition": (
                RFQ_REOPEN_CONDITION + " Then complete the frozen matched dependent test "
                "with unit controls."
            ),
        },
        "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01": {
            "hypothesis_id": "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
            "hypothesis_status": terminal(valid_requests > 0 and observed_deletes > 0),
            "status_reason": (
                "Observed RFQ lifecycle endpoints exist, but a predictive mixture or "
                "competing-risk test, arrival-only comparison, boundary-shift control and "
                "root-event CLOB-impact test are incomplete across only two degraded days. "
                + RFQ_PARTIAL_BOUNDARY
                if valid_requests > 0 and observed_deletes > 0
                else "No observed valid RFQ lifecycle endpoint exists for the frozen mixture test. "
                + RFQ_PARTIAL_BOUNDARY
            ),
            "necessary_input_rule": {
                "valid_requests_gt_zero": valid_requests > 0,
                "observed_first_valid_deletes_gt_zero": observed_deletes > 0,
            },
            "unfinished_components": [
                "predictive mixture or competing-risk model",
                "arrival-only baseline comparison",
                "scan-boundary shift and future-delete controls",
                "root-event clustered incremental CLOB impact test",
            ],
            "reopen_condition": (
                RFQ_REOPEN_CONDITION + " Then complete the frozen censor-aware "
                "predictive/competing-risk test."
            ),
        },
    }
    return {
        "schema_version": "sports-autoresearch-rfq-hypothesis-conclusions-v1",
        "run_id": manifest["run_id"],
        "generated_at_utc": completed_at_utc,
        "mode": EXPECTED_MODE,
        "data_evidence": EXPECTED_EVIDENCE,
        "split": EXPECTED_SPLIT,
        "artifact_status": "DIAGNOSTIC_ONLY",
        "coverage_status": RFQ_PARTIAL_STATUS,
        "analysis_population": quarantine["retained_population"],
        "whole_object_quarantine": True,
        "line_salvage": False,
        "quarantined_object_set_sha256": quarantine[
            "quarantined_object_set_sha256"
        ],
        "source_summary": str(RFQ_SUMMARY),
        "source_summary_sha256": sha256(run_dir / RFQ_SUMMARY),
        "frozen_outcome_independent_rule": (
            "Necessary input or joinable sample equals zero => DATA_STARVED; otherwise "
            "COLLECT_MORE. Effect signs, magnitudes, p-values and charts cannot change status."
        ),
        "observed_counts": {
            "valid_requests": valid_requests,
            "observed_first_valid_deletes": observed_deletes,
            "right_censored_creates": right_censored,
            "causal_eligible_endpoint_anchors": causal_anchors,
            "matched_pairs": matched_pairs,
            "rfq_size_summary_rows": size_counts["table_rows"],
            "contracts_size_observations": size_counts["contracts_n"],
            "target_cost_observations": size_counts["target_n"],
        },
        "hypotheses": conclusions,
        "claim_boundary": (
            "This conservative status receipt is not a completed dependent RFQ test, does "
            "not infer RFQ acceptance/fill/PnL, and creates no promotion, verdict, "
            "validation, confirmation or live authority. Whole-object quarantine is true "
            "and line salvage is false. " + RFQ_PARTIAL_BOUNDARY
        ),
        "reopen_condition": RFQ_REOPEN_CONDITION,
    }


def collect_terminal_conclusions(
    run_dir: Path,
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
    rfq_conclusions: Mapping[str, Any],
) -> tuple[dict[str, Conclusion], dict[str, Any], Path]:
    core_summary = load_json(run_dir / CORE_SUMMARY)
    if core_summary.get("run_id") != manifest.get("run_id"):
        raise MissionFinalizationError("Cycle-1 core summary run_id mismatch")
    boundary = core_summary.get("boundary")
    if not isinstance(boundary, str) or "pending" not in boundary.lower():
        raise MissionFinalizationError("Cycle-1 core summary lacks its partial-stage boundary")

    core_tests = load_json(run_dir / CORE_HYPOTHESIS_SUMMARY)
    validate_stage_header(
        core_tests,
        manifest=manifest,
        stage_name="core hypothesis tests",
        schema_prefix="sports-autoresearch-core-hypothesis-tests-v1",
    )
    core_records = extract_conclusions(
        core_tests,
        source_path=str(CORE_HYPOTHESIS_SUMMARY),
        allowed_ids=CORE_TEST_IDS,
    )
    core = resolve_unique_conclusions(core_records, CORE_TEST_IDS, "core tests")

    # Only the core-stage receipt is an input.  Ignore ``final_result`` mirrors
    # on an idempotent rerun so provenance cannot drift from cycle1_result to
    # the finalizer's own output.
    rv_records: list[Conclusion] = []
    for index, card in enumerate(ledger.get("hypotheses", [])):
        if not isinstance(card, dict) or card.get("hypothesis_id") not in RV_IDS:
            continue
        stage_result = card.get("cycle1_result")
        if not isinstance(stage_result, dict):
            continue
        status = normalize_status(
            stage_result.get("hypothesis_status", stage_result.get("status"))
        )
        if status in BANNED_TERMINAL_STATUSES:
            raise MissionFinalizationError(
                f"forbidden Mode-1 RV status {status}: {card['hypothesis_id']}"
            )
        if status in TERMINAL_MODE1_STATUSES:
            rv_records.append(
                Conclusion(
                    hypothesis_id=card["hypothesis_id"],
                    status=status,
                    reason=reason_from_record(stage_result),
                    source_path="HYPOTHESIS_LEDGER.json",
                    source_pointer=f"/hypotheses/{index}/cycle1_result",
                )
            )
    rv = resolve_unique_conclusions(rv_records, RV_IDS, "core relative-value screen")

    rfq = resolve_unique_conclusions(
        extract_conclusions(
            rfq_conclusions,
            source_path=str(RFQ_CONCLUSIONS),
            allowed_ids=RFQ_IDS,
        ),
        RFQ_IDS,
        "RFQ conservative conclusions",
    )

    l2_path = find_l2_summary(run_dir)
    l2_summary = load_json(run_dir / l2_path)
    validate_stage_header(
        l2_summary,
        manifest=manifest,
        stage_name="L2 hypothesis stage",
        schema_prefix="sports-autoresearch-l2",
    )
    if l2_summary.get("stage") != "L2_HYPOTHESIS_TESTS":
        raise MissionFinalizationError("L2 stage identity mismatch")
    if normalize_status(l2_summary.get("status")) not in TERMINAL_MODE1_STATUSES:
        raise MissionFinalizationError("L2 top-level status is not terminal MODE 1")
    l2_banner = l2_summary.get("banner")
    if not isinstance(l2_banner, dict) or any(
        l2_banner.get(key) != expected
        for key, expected in {
            "data_evidence": EXPECTED_EVIDENCE,
            "timestamp_discipline": EXPECTED_TIMESTAMP,
            "experiment_split": EXPECTED_SPLIT,
            "artifact_status": "DIAGNOSTIC_ONLY",
            "authorization": NO_LIVE_BANNER,
        }.items()
    ):
        raise MissionFinalizationError("L2 stage banner/authority boundary mismatch")
    for key in ("data_binding", "receipt_quality", "replay_qc"):
        if not isinstance(l2_summary.get(key), dict):
            raise MissionFinalizationError(f"L2 stage {key} receipt is missing")
    expected_releases = [item["release_id"] for item in manifest["selected_releases"]]
    if l2_summary["data_binding"].get("release_ids") != expected_releases:
        raise MissionFinalizationError("L2 stage release binding mismatch")
    l2 = resolve_unique_conclusions(
        extract_conclusions(
            l2_summary,
            source_path=str(l2_path),
            allowed_ids=L2_TEST_IDS,
        ),
        L2_TEST_IDS,
        "L2 hypothesis stage",
    )

    combined = {**core, **rv, **rfq, **l2}
    if set(combined) != set(HYPOTHESIS_IDS):
        missing = sorted(set(HYPOTHESIS_IDS) - set(combined))
        extra = sorted(set(combined) - set(HYPOTHESIS_IDS))
        raise MissionFinalizationError(
            f"terminal result set mismatch; missing={missing}, extra={extra}"
        )
    for item in combined.values():
        if item.status not in TERMINAL_MODE1_STATUSES:
            raise MissionFinalizationError(
                f"nonterminal status survived finalization: {item.hypothesis_id}={item.status}"
            )
    return combined, l2_summary, l2_path


def merge_coverage(
    run_dir: Path,
    manifest: Mapping[str, Any],
    rfq: Mapping[str, Any],
    quarantine: Mapping[str, Any],
    l2: Mapping[str, Any],
    l2_path: Path,
    *,
    completed_at_utc: str,
) -> dict[str, Any]:
    coverage_path = run_dir / "DATA_COVERAGE.json"
    coverage = load_json(coverage_path)
    schema = coverage.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith("sports-autoresearch-coverage-v"):
        raise MissionFinalizationError("unsupported DATA_COVERAGE schema")
    if not coverage.get("coverage_cube") or not (run_dir / coverage["coverage_cube"]).is_file():
        raise MissionFinalizationError("core data coverage cube is missing")
    channels = coverage.get("channels")
    if not isinstance(channels, list) or not channels:
        raise MissionFinalizationError("core channel coverage is missing")
    rfq_counts = rfq.get("counts")
    rfq_input = rfq.get("input")
    if not isinstance(rfq_counts, dict) or not isinstance(rfq_input, dict):
        raise MissionFinalizationError("RFQ coverage summary is incomplete")
    l2_records = extract_conclusions(
        l2, source_path=str(l2_path), allowed_ids=L2_TEST_IDS
    )
    if not l2_records:
        raise MissionFinalizationError("L2 coverage has no bound hypothesis receipts")
    l2_hypotheses = l2.get("hypotheses")
    if not isinstance(l2_hypotheses, dict):
        raise MissionFinalizationError("L2 summary hypotheses must be an id-keyed object")
    coverage["coverage_status"] = (
        "PARTIAL_MODE1_RFQ_OBJECT_COVERAGE_QUARANTINED"
    )
    coverage["finalized_at_utc"] = completed_at_utc
    coverage["scope_note"] = (
        "Core fact rows remain in the market-hour cube. RFQ uses only the retained "
        "observed subset after two exact adjacent whole-object quarantines forming one "
        "contiguous gap, while snapshot-aware L2 has its own receipt. The missing RFQ "
        "interval can create temporal-selection "
        "bias; no RFQ rows or object bytes are fabricated inside the core cube."
    )
    coverage["stage_completeness"] = {
        "core_fact_cube": {
            "status": "COMPLETE_FOR_SELECTED_RELEASES",
            "path": coverage["coverage_cube"],
            "cells": coverage.get("coverage_cube_cells"),
        },
        "rfq_full_stage": {
            "status": RFQ_PARTIAL_STATUS,
            "analysis_scope": RFQ_ANALYSIS_SCOPE,
            "analysis_population": quarantine["retained_population"],
            "whole_object_quarantine": True,
            "line_salvage": False,
            "path": str(RFQ_SUMMARY),
            "sha256": sha256(run_dir / RFQ_SUMMARY),
            "release_ids": rfq_input.get("release_ids"),
            "logical_manifest_bindings_total": rfq_input.get(
                "logical_manifest_bindings_total"
            ),
            "unique_objects_total": quarantine["unique_objects_total"],
            "retained_unique_objects": quarantine["consumed_unique_objects"],
            "quarantined_unique_objects": quarantine[
                "quarantined_unique_objects"
            ],
            "unique_bytes_total": quarantine["unique_bytes_total"],
            "retained_bytes": quarantine["consumed_bytes"],
            "quarantined_bytes": quarantine["quarantined_bytes"],
            "manifest_object_set_sha256": quarantine[
                "manifest_object_set_sha256"
            ],
            "retained_object_set_sha256": quarantine[
                "consumed_object_set_sha256"
            ],
            "quarantined_object_set_sha256": quarantine[
                "quarantined_object_set_sha256"
            ],
            "counts": rfq_counts,
            "coverage": rfq.get("coverage"),
            "hard_truth": rfq.get("hard_truth"),
            "quarantine": dict(quarantine),
        },
        "l2_snapshot_aware_stage": {
            "status": "COMPLETE_EXPLORATORY_HYPOTHESIS_STAGE",
            "path": str(l2_path),
            "sha256": sha256(run_dir / l2_path),
            "schema": l2.get("schema_version", l2.get("schema")),
            "coverage": l2.get("coverage", l2.get("input_coverage")),
            "data_binding": l2.get("data_binding"),
            "receipt_quality": l2.get("receipt_quality"),
            "replay_qc": l2.get("replay_qc"),
            "artifacts": l2.get("artifacts"),
            "limitations": l2.get("limitations"),
            "hypothesis_sample_counts": {
                hypothesis_id: record.get("sample_counts")
                for hypothesis_id, record in l2_hypotheses.items()
                if isinstance(record, dict)
            },
        },
    }
    limitations = coverage.setdefault("limitations", [])
    if not isinstance(limitations, list):
        raise MissionFinalizationError("coverage limitations must be a list")
    final_limits = (
        "Both selected releases are SEALED_DEGRADED_EVIDENCE; no confirmation-grade claim is possible.",
        "Only two prior-exposed UTC day blocks are available.",
        "RFQ coverage is partial: two exact adjacent malformed manifest objects are wholly quarantined with no line salvage.",
        "RFQ findings use the retained observed subset and may have temporal-selection bias from the one contiguous missing interval.",
        RFQ_REOPEN_CONDITION,
        "W09 has no S3 write path; durable S3 report publication was unavailable and not attempted.",
    )
    for item in final_limits:
        if item not in limitations:
            limitations.append(item)
    atomic_json(coverage_path, coverage)

    lines = [
        "# DATA COVERAGE — final MODE-1 snapshot",
        "",
        f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
        f"> **{NO_LIVE_BANNER}**",
        "",
        f"Status: `{coverage['coverage_status']}`.",
        "",
        f"Core cube: `{coverage['coverage_cube']}`; cells: "
        f"`{coverage.get('coverage_cube_cells')}` "
        "(provenance: `DATA_COVERAGE.json#/coverage_cube_cells`).",
        "",
        "## Core channel totals",
        "",
    ]
    for index, row in enumerate(channels):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"- `{row.get('channel')}`: rows `{row.get('included_rows')}`, markets "
            f"`{row.get('n_markets')}`, root events `{row.get('n_games')}`, day blocks "
            f"`{row.get('n_day_blocks')}` (provenance: "
            f"`DATA_COVERAGE.json#/channels/{index}`)."
        )
    lines.extend(
        [
            "",
            "## RFQ partial object coverage",
            "",
            f"- Status: `{RFQ_PARTIAL_STATUS}`; population: "
            "`RETAINED_OBSERVED_SUBSET`; whole-object quarantine: `true`; line salvage: `false`.",
            f"- Objects retained/quarantined/total: `{quarantine['consumed_unique_objects']}` / "
            f"`{quarantine['quarantined_unique_objects']}` / `{quarantine['unique_objects_total']}`.",
            f"- Bytes retained/quarantined/total: `{quarantine['consumed_bytes']}` / "
            f"`{quarantine['quarantined_bytes']}` / `{quarantine['unique_bytes_total']}`.",
            f"- Valid requests: `{rfq_counts.get('valid_requests')}` "
            "(provenance: `REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json#/counts/valid_requests`).",
            f"- Matched CLOB pairs: `{(rfq.get('clob') or {}).get('matched_pairs')}` "
            "(provenance: `REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json#/clob/matched_pairs`).",
            f"- {RFQ_PARTIAL_BOUNDARY}",
            f"- Reopen condition: {RFQ_REOPEN_CONDITION}",
            "",
            "## L2 stage",
            "",
            f"The snapshot-aware L2 hypothesis receipt is `{l2_path}` (SHA-256 "
            f"`{sha256(run_dir / l2_path)}`; provenance: file bytes).",
            "",
            "## Explicit limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in limitations)
    atomic_text(run_dir / "DATA_COVERAGE.md", "\n".join(lines) + "\n")
    return coverage


def update_ledger(
    run_dir: Path,
    ledger: dict[str, Any],
    conclusions: Mapping[str, Conclusion],
    *,
    completed_at_utc: str,
) -> dict[str, Any]:
    cards = ledger["hypotheses"]
    for card in cards:
        hypothesis_id = card["hypothesis_id"]
        conclusion = conclusions[hypothesis_id]
        card["status"] = conclusion.status
        card["artifact_status"] = "DIAGNOSTIC_ONLY"
        card["final_result"] = {
            **conclusion.as_dict(),
            "recorded_at_utc": completed_at_utc,
            "mode": EXPECTED_MODE,
            "split": EXPECTED_SPLIT,
            "data_evidence": EXPECTED_EVIDENCE,
            "authorization": NO_LIVE_BANNER,
        }
    ledger["finalized_at_utc"] = completed_at_utc
    ledger["terminal_condition"] = {
        "id": 4,
        "description": "All remaining hypotheses are COLLECT_MORE or DATA_STARVED; closed rejected/invalidated hypotheses, if any, remain terminal.",
    }
    ledger["mode"] = EXPECTED_MODE
    ledger["claim_boundary"] = (
        "No card is promotion-ready, a verdict, validation/confirmation evidence, "
        "live-ready, or authorized for live trading."
    )
    atomic_json(run_dir / "HYPOTHESIS_LEDGER.json", ledger)
    lines = [
        "# HYPOTHESIS LEDGER — final MODE-1 snapshot",
        "",
        f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
        f"> **{NO_LIVE_BANNER}**",
        "",
    ]
    for card in cards:
        result = card["final_result"]
        lines.extend(
            [
                f"## {card['hypothesis_id']}",
                "",
                card.get("sentence", "No sentence supplied."),
                "",
                f"- terminal status: `{card['status']}`",
                f"- reason: {result['status_reason']}",
                f"- provenance: `{result['source_path']}{result['source_json_pointer']}`",
                f"- reopen: {card.get('reopen', 'Not registered.')}",
                "",
            ]
        )
    atomic_text(run_dir / "HYPOTHESIS_LEDGER.md", "\n".join(lines))
    return ledger


def append_final_registry_records(
    run_dir: Path,
    manifest: Mapping[str, Any],
    conclusions: Mapping[str, Conclusion],
    *,
    completed_at_utc: str,
) -> None:
    path = run_dir / "TRIAL_REGISTRY.jsonl"
    if not path.is_file():
        raise MissionFinalizationError("TRIAL_REGISTRY.jsonl is missing")
    existing: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MissionFinalizationError(
                f"invalid trial registry JSON at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise MissionFinalizationError(
                f"trial registry record {line_number} is not an object"
            )
        if record.get("record_type") == "MISSION_FINAL_RESULT":
            hypothesis_id = record.get("trial_id")
            if hypothesis_id in existing:
                raise MissionFinalizationError(
                    f"duplicate mission-final registry record: {hypothesis_id}"
                )
            existing[hypothesis_id] = record
    append: list[str] = []
    release_ids = [item["release_id"] for item in manifest["selected_releases"]]
    for hypothesis_id in HYPOTHESIS_IDS:
        conclusion = conclusions[hypothesis_id]
        if hypothesis_id in existing:
            old = existing[hypothesis_id]
            if old.get("hypothesis_status") != conclusion.status:
                raise MissionFinalizationError(
                    f"idempotence conflict in final registry: {hypothesis_id}"
                )
            continue
        record = {
            "recorded_at_utc": completed_at_utc,
            "trial_id": hypothesis_id,
            "stage": "MISSION_FINALIZATION",
            "record_type": "MISSION_FINAL_RESULT",
            "result_opened": True,
            "hypothesis_status": conclusion.status,
            "artifact_status": "DIAGNOSTIC_ONLY",
            "mode": EXPECTED_MODE,
            "split": EXPECTED_SPLIT,
            "data_evidence": EXPECTED_EVIDENCE,
            "release_ids": release_ids,
            "result_artifact": conclusion.source_path,
            "result_json_pointer": conclusion.source_pointer,
            "status_reason": conclusion.reason,
            "authorization": NO_LIVE_BANNER,
        }
        append.append(json.dumps(record, sort_keys=True, ensure_ascii=False))
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(append) + "\n")


def status_counts(conclusions: Mapping[str, Conclusion]) -> dict[str, int]:
    return {
        status: sum(item.status == status for item in conclusions.values())
        for status in sorted(TERMINAL_MODE1_STATUSES)
    }


def source_ref(path: str, pointer: str = "") -> str:
    return f"`{path}{pointer}`"


def metric(value: Any, path: str, pointer: str) -> str:
    return f"`{value}` (provenance: {source_ref(path, pointer)})"


def hypothesis_lines(
    ids: Iterable[str], conclusions: Mapping[str, Conclusion]
) -> list[str]:
    lines = []
    for hypothesis_id in sorted(ids):
        item = conclusions[hypothesis_id]
        lines.append(
            f"- `{hypothesis_id}` — **{item.status}**: {item.reason} "
            f"(provenance: {source_ref(item.source_path, item.source_pointer)})."
        )
    return lines


def write_hypothesis_artifacts(
    run_dir: Path,
    ledger: Mapping[str, Any],
    conclusions: Mapping[str, Conclusion],
) -> dict[str, str]:
    cards = {card["hypothesis_id"]: card for card in ledger["hypotheses"]}
    artifacts: dict[str, str] = {}
    for directory in (
        "CANDIDATE_DOSSIERS",
        "COLLECT_MORE_HYPOTHESES",
        "REJECTED_HYPOTHESES",
        "DATA_STARVED_HYPOTHESES",
    ):
        (run_dir / directory).mkdir(parents=True, exist_ok=True)
    for hypothesis_id in HYPOTHESIS_IDS:
        card = cards[hypothesis_id]
        result = conclusions[hypothesis_id]
        if result.status == "COLLECT_MORE":
            relative = Path("COLLECT_MORE_HYPOTHESES") / f"{hypothesis_id}.md"
            # This is a status dossier, not a shortlist/promotion dossier.  All
            # unavailable components are identified instead of synthesized.
            section_content = (
                ("NAME AND STATUS", f"{hypothesis_id} — COLLECT_MORE."),
                ("ONE-SENTENCE HYPOTHESIS", card.get("sentence")),
                ("ECONOMIC MECHANISM", card.get("mechanism")),
                ("WHY THE EDGE MAY PERSIST", "Not established on current evidence."),
                ("EXACT MARKET/SPORT/REGIME POPULATION", card.get("population")),
                ("EXACT CAUSAL FEATURE FORMULAS", card.get("feature")),
                ("DECISION CLOCK", card.get("decision_clock")),
                ("ENTRY, QUOTE, CANCEL, EXIT, AND HEDGE PSEUDOCODE", "Not emitted: no executable candidate survived."),
                ("DATA COVERAGE AND EVIDENCE TIERS", f"{EXPECTED_EVIDENCE}; {EXPECTED_TIMESTAMP}; {EXPECTED_SPLIT}."),
                ("FROZEN TEST DESIGN", card.get("test_family")),
                ("TRIAL COUNT AND MULTIPLICITY ADJUSTMENT", card.get("multiplicity_policy")),
                ("DESCRIPTIVE RESULTS", result.reason),
                ("NET ECONOMIC RESULTS", "Unavailable or incomplete; no net-profit claim."),
                ("EVENT-LEVEL CONFIDENCE INTERVALS", "Insufficient independent day blocks for a candidate conclusion."),
                ("FILL AND FEE MODEL", f"{card.get('fill_policy')}; {card.get('fee_policy')}."),
                ("LATENCY SENSITIVITY", "Not completed for promotion."),
                ("PARAMETER SENSITIVITY", "Not completed for promotion."),
                ("SPORT/REGIME/FOLD HETEROGENEITY", "Not completed for promotion."),
                ("TAILS AND DRAWDOWN", "No authorized strategy PnL series exists."),
                ("CONCENTRATION", "Root-event/day support remains insufficient."),
                ("CAPACITY", "Not established."),
                ("NEGATIVE CONTROLS", ", ".join(card.get("negative_controls", []))),
                ("CLEAN-ROOM CROSS-CHECK", "Not applicable without a finalist."),
                ("REQUIRED ENGINE CAPABILITIES", ", ".join(card.get("required_engine_capabilities", []))),
                ("CURRENT ENGINE CAPABILITY GAPS", result.reason),
                ("FAILURE MODES", card.get("rejection")),
                ("REOPEN/COLLECT-MORE CONDITIONS", card.get("reopen")),
                ("PROPOSED MICRO-LIVE DESIGN", "Not emitted and not authorized."),
                ("KILL CONDITIONS", card.get("rejection")),
                ("SUCCESS/ABORT METRICS", card.get("economic_threshold")),
                ("EXPLICITLY UNAUTHORIZED BANNER", NO_LIVE_BANNER),
            )
            lines = [
                f"# {hypothesis_id} — COLLECT_MORE status dossier",
                "",
                f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
                f"> **{NO_LIVE_BANNER}**",
                "",
                f"Result provenance: `{result.source_path}{result.source_pointer}`.",
                "",
            ]
            for index, (title, value) in enumerate(section_content, 1):
                lines.extend([f"## {index}. {title}", "", str(value or "Not available."), ""])
        else:
            directory = (
                "DATA_STARVED_HYPOTHESES"
                if result.status == "DATA_STARVED"
                else "REJECTED_HYPOTHESES"
            )
            relative = Path(directory) / f"{hypothesis_id}.md"
            lines = [
                f"# {hypothesis_id} — {result.status}",
                "",
                f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
                f"> **{NO_LIVE_BANNER}**",
                "",
                "## Frozen hypothesis",
                "",
                card.get("sentence", "Not available."),
                "",
                "## Terminal evidence",
                "",
                result.reason,
                "",
                f"Provenance: `{result.source_path}{result.source_pointer}`.",
                "",
                "## Failure class",
                "",
                result.status,
                "",
                "## Reopen condition",
                "",
                card.get("reopen", "Not registered."),
                "",
                "## Authority boundary",
                "",
                NO_LIVE_BANNER,
                "",
            ]
        atomic_text(run_dir / relative, "\n".join(lines))
        artifacts[hypothesis_id] = str(relative)
    return artifacts


def build_report_sections(
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
    coverage: Mapping[str, Any],
    rfq: Mapping[str, Any],
    quarantine: Mapping[str, Any],
    l2_path: Path,
    conclusions: Mapping[str, Conclusion],
    dossier_paths: Mapping[str, str],
    resource: Mapping[str, Any],
    trial_registry_records: int,
) -> list[tuple[str, str]]:
    counts = status_counts(conclusions)
    releases = manifest["selected_releases"]
    release_lines = []
    for index, release in enumerate(releases):
        release_lines.append(
            f"- `{release['release_id']}` / date `{release['date']}` / bytes "
            f"{metric(release.get('byte_count'), 'RUN_MANIFEST.json', f'/selected_releases/{index}/byte_count')} / "
            f"objects {metric(release.get('object_count'), 'RUN_MANIFEST.json', f'/selected_releases/{index}/object_count')}."
        )
    integrity = coverage.get("exclusions", {})
    integrity_lines = []
    if isinstance(integrity, dict):
        for key, value in sorted(integrity.items()):
            integrity_lines.append(
                f"- `{key}`: {metric(value, 'DATA_COVERAGE.json', f'/exclusions/{key}')}"
            )
    if not integrity_lines:
        integrity_lines.append("- No exclusion dictionary was supplied; completion would have failed if core coverage were absent.")
    rejected = [key for key, item in conclusions.items() if item.status == "REJECTED"]
    starved = [
        key
        for key, item in conclusions.items()
        if item.status in {"DATA_STARVED", "INVALIDATED_BY_DATA", "INVALIDATED_BY_LEAKAGE"}
    ]
    collect_more = [
        key for key, item in conclusions.items() if item.status == "COLLECT_MORE"
    ]
    registry_text = metric(
        trial_registry_records,
        "REPORT/tables/FINAL_MISSION_SUMMARY.json",
        "/trial_registry_records",
    )
    rfq_counts = rfq["counts"]
    clob = rfq["clob"]
    resource_cost = resource.get("estimated_total_compute_cost_usd")
    resource_wall = resource.get("w09_driver_wall_seconds")

    sections: list[tuple[str, str]] = [
        (
            REPORT_SECTION_TITLES[0],
            "The run stopped under terminal condition 4. Every frozen card has a conservative "
            "MODE-1 terminal status. Counts: COLLECT_MORE "
            f"{metric(counts['COLLECT_MORE'], 'REPORT/tables/FINAL_MISSION_SUMMARY.json', '/status_counts/COLLECT_MORE')}; "
            "DATA_STARVED "
            f"{metric(counts['DATA_STARVED'], 'REPORT/tables/FINAL_MISSION_SUMMARY.json', '/status_counts/DATA_STARVED')}; "
            "REJECTED "
            f"{metric(counts['REJECTED'], 'REPORT/tables/FINAL_MISSION_SUMMARY.json', '/status_counts/REJECTED')}.\n\n"
            "Claim ladder: pattern exists — descriptive observations only, no promoted causal claim; "
            "predicts something — not established; executable — not established; survives costs — no; "
            "survives conservative fills — no; survives unseen data — no untouched set was opened; "
            "authorized for live testing — no. " + NO_LIVE_BANNER,
        ),
        (
            REPORT_SECTION_TITLES[1],
            "Only immutable, exact-VersionId W05 release objects selected in RUN_MANIFEST were used.\n\n"
            + "\n".join(release_lines)
            + "\n\nCore fact cube: `"
            + str(coverage.get("coverage_cube"))
            + "`; RFQ partial-stage summary: `"
            + str(RFQ_SUMMARY)
            + "`; L2 snapshot-aware summary: `"
            + str(l2_path)
            + "`.\n\n"
            + RFQ_PARTIAL_BOUNDARY
            + " Whole-object quarantine is true and line salvage is false; retained RFQ "
            f"objects/bytes are `{quarantine['consumed_unique_objects']}` / "
            f"`{quarantine['consumed_bytes']}` of `{quarantine['unique_objects_total']}` / "
            f"`{quarantine['unique_bytes_total']}`.",
        ),
        (
            REPORT_SECTION_TITLES[2],
            "Confirmation-grade days, ratified exact fee/latency/order-lifecycle economics, "
            "authoritative exhaustive family roles, broad causal historical dimensions, accepted "
            "RFQ quote/fill outcomes, and an S3 report-write path were unavailable. No unavailable "
            "field was inferred. Provenance: `DATA_COVERAGE.json#/limitations` and "
            "`REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json#/hard_truth`.",
        ),
        (
            REPORT_SECTION_TITLES[3],
            f"Data evidence: `{EXPECTED_EVIDENCE}`; timestamp: `{EXPECTED_TIMESTAMP}` for fact clocks "
            f"with explicitly MIXED/post-hoc dimensions; split: `{EXPECTED_SPLIT}`; artifact: "
            f"`DIAGNOSTIC_ONLY`; mode: `{EXPECTED_MODE}`. Provenance: `RUN_MANIFEST.json#/banners`.",
        ),
        (
            REPORT_SECTION_TITLES[4],
            "\n".join(integrity_lines)
            + "\n- RFQ status: `PARTIAL_OBJECT_COVERAGE_QUARANTINED`; the two adjacent "
            "malformed objects were excluded in full as one contiguous gap with no line "
            "salvage. Failure-state, resource, "
            "scratch-preservation, declaration, receipt, VersionId, set-hash, and gap-QC "
            "bindings were verified before finalization. The registration change was data-"
            "integrity handling only: two exact adjacent whole-object quarantines forming "
            "one contiguous gap plus conservative "
            "gap censoring, with no hypothesis-design, threshold, feature, test, or status "
            "change.\n- "
            + RFQ_PARTIAL_BOUNDARY,
        ),
        (
            REPORT_SECTION_TITLES[5],
            "The atlas is a root-event-first diagnostic with provisional heuristic game mapping. "
            f"Coverage cells: {metric(coverage.get('coverage_cube_cells'), 'DATA_COVERAGE.json', '/coverage_cube_cells')}. "
            "No row/message count is treated as an independent inferential sample. Provenance: "
            "`REPORT/CYCLE1_CORE_SUMMARY.json#/atlas`.",
        ),
        (
            REPORT_SECTION_TITLES[6],
            "Transparent spread/activity/time-to-start regimes were evaluated by the frozen core "
            "stage. They remain descriptive or terminal COLLECT_MORE/DATA_STARVED; no regime was "
            "converted into a trading rule. Provenance: `REPORT/tables/CORE_HYPOTHESIS_TESTS.json#/regime_thresholds`.",
        ),
        (REPORT_SECTION_TITLES[7], "\n".join(hypothesis_lines({"C1-SPREAD-CAPTURE-01", *L2_TEST_IDS, "C1-PREMATCH-TTS-01"}, conclusions))),
        (REPORT_SECTION_TITLES[8], "\n".join(hypothesis_lines({"C1-LARGE-FLOW-CONTINUATION-01"}, conclusions))),
        (REPORT_SECTION_TITLES[9], "\n".join(hypothesis_lines(RV_IDS, conclusions))),
        (
            REPORT_SECTION_TITLES[10],
            RFQ_PARTIAL_BOUNDARY
            + "\n\n"
            + "\n".join(hypothesis_lines(RFQ_IDS, conclusions))
            + "\n\nValid requests: "
            + metric(rfq_counts.get("valid_requests"), str(RFQ_SUMMARY), "/counts/valid_requests")
            + "; matched CLOB pairs: "
            + metric(clob.get("matched_pairs"), str(RFQ_SUMMARY), "/clob/matched_pairs")
            + ". Broadcast hard truth remains: no accepted quote/fill or actual RFQ PnL is observed.\n\n"
            + "Reopen condition: "
            + RFQ_REOPEN_CONDITION,
        ),
        (
            REPORT_SECTION_TITLES[11],
            "The two preregistered own-data anomalies are the RFQ size tail and lifecycle mixture. "
            "Both require private broadcast/lifecycle/CLOB linkage and both ended conservatively "
            "without using effect direction to select status. Provenance: "
            "`REPORT/tables/RFQ_HYPOTHESIS_CONCLUSIONS.json#/hypotheses` and the trigger charts.",
        ),
        (
            REPORT_SECTION_TITLES[12],
            f"Append-only registry records: {registry_text}. All ten cards include registration, "
            "stage receipts, and one idempotent MISSION_FINAL_RESULT record; failed/inconclusive "
            "states were not dropped.",
        ),
        (
            REPORT_SECTION_TITLES[13],
            "The frozen cards declare BH-FDR by family. No formal shortlist primary test or Holm "
            "gate was opened. Underpowered results were not converted into rejections, and RFQ "
            "terminal rules did not inspect effect signs or p-values. Provenance: "
            "`HYPOTHESIS_LEDGER.json#/hypotheses` and "
            "`REPORT/tables/RFQ_HYPOTHESIS_CONCLUSIONS.json#/frozen_outcome_independent_rule`.",
        ),
        (
            REPORT_SECTION_TITLES[14],
            "None." if not rejected else "\n".join(hypothesis_lines(rejected, conclusions)),
        ),
        (
            REPORT_SECTION_TITLES[15],
            "None." if not starved else "\n".join(hypothesis_lines(starved, conclusions)),
        ),
        (
            REPORT_SECTION_TITLES[16],
            "No strategy was shortlisted. COLLECT_MORE is not a shortlist, promotion, validation, "
            "verdict, or live-ready status. COLLECT_MORE cards: "
            + (", ".join(f"`{item}`" for item in sorted(collect_more)) or "none")
            + ". Provenance: `REPORT/tables/FINAL_MISSION_SUMMARY.json#/hypotheses`.",
        ),
        (
            REPORT_SECTION_TITLES[17],
            "`CANDIDATE_DOSSIERS/` is intentionally empty because there is no shortlist. Each card "
            "has a terminal artifact. `COLLECT_MORE_HYPOTHESES/` status dossiers explicitly leave "
            "unmeasured economics/capacity/live design unavailable; DATA_STARVED/REJECTED cards "
            "record evidence and reopen conditions.\n\n"
            + "\n".join(f"- `{key}`: `{value}`" for key, value in sorted(dossier_paths.items())),
        ),
        (
            REPORT_SECTION_TITLES[18],
            "No capacity claim is supported. The completed analyses ran only on the bound existing "
            "W09 envelope. No resize, new machine, production EC2, or production database was used. "
            "Provenance: `RUN_MANIFEST.json#/compute` and `RESOURCE_USAGE.json`.",
        ),
        (
            REPORT_SECTION_TITLES[19],
            "Highest priority: additional quality-assessed sealed day blocks. Also required: "
            "authoritative root/family roles, causal dimension history, exact fees/order latency, "
            "and RFQ outcome fields if acceptance/fill/PnL questions are ever authorized. "
            + RFQ_REOPEN_CONDITION,
        ),
        (
            REPORT_SECTION_TITLES[20],
            "Ratify fee and order-lifecycle inputs; improve causal historical dimension coverage; "
            "retain snapshot-aware L2 replay and sealed stream receipts; add a governed write-capable "
            "publication path only under separate authority. These are gaps, not in-mission changes.",
        ),
        (
            REPORT_SECTION_TITLES[21],
            "Collect new post-start sealed days, run PIPE-W03 quality assessment, then execute the "
            "already frozen dependent tests without retuning thresholds. Formal validation/confirmation "
            "requires separate authority and an untouched split. For RFQ specifically: "
            + RFQ_REOPEN_CONDITION,
        ),
        (
            REPORT_SECTION_TITLES[22],
            "W09 driver wall seconds: "
            + metric(resource_wall, "RESOURCE_USAGE.json", "/w09_driver_wall_seconds")
            + "; estimated compute cost USD: "
            + metric(resource_cost, "RESOURCE_USAGE.json", "/estimated_total_compute_cost_usd")
            + ". Actual billed cost was unavailable. Shutdown confirmation remains a driver action "
            "after artifact sync and must not be inferred by this running W09 process.",
        ),
        (
            REPORT_SECTION_TITLES[23],
            "Use `REPRODUCE.md` for identities and stage order, and `reproduce.sh` for offline "
            "artifact verification. Exact release VersionIds, query hashes, feature hash, method hash, "
            "environment and seeds remain bound in RUN_MANIFEST/DATA_INTEGRITY/queries.",
        ),
        (
            REPORT_SECTION_TITLES[24],
            f"Two degraded prior-exposed days cannot establish persistence, conservative fill "
            "economics, unseen-data survival, or live readiness. Post-hoc/provisional dimensions "
            "remain limited. The RFQ retained observed subset is partial and may have "
            "temporal-selection bias from its quarantined interval; no line was salvaged. "
            "S3 write publication was unavailable and not attempted. No production "
            "EC2 access, S3 mutation, exchange action, RFQ post/response, shadow order, or live trade "
            f"was authorized or executed. **{NO_LIVE_BANNER}.**",
        ),
    ]
    if tuple(title for title, _ in sections) != REPORT_SECTION_TITLES:
        raise MissionFinalizationError("internal 25-section report contract mismatch")
    return sections


def markdown_report(run_id: str, sections: Sequence[tuple[str, str]]) -> str:
    lines = [
        f"# SPORTS-AUTORESEARCH-01 — full report — {run_id}",
        "",
        f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
        f"> **{NO_LIVE_BANNER}**",
        "",
    ]
    for index, (title, body) in enumerate(sections, 1):
        lines.extend([f"## {index}. {title}", "", body, ""])
    return "\n".join(lines)


def executive_summary(
    run_id: str,
    conclusions: Mapping[str, Conclusion],
    resource: Mapping[str, Any],
    quarantine: Mapping[str, Any],
) -> str:
    counts = status_counts(conclusions)
    return "\n".join(
        [
            f"# SPORTS-AUTORESEARCH-01 — executive summary — {run_id}",
            "",
            f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
            f"> **{NO_LIVE_BANNER}**",
            "",
            "The run reached terminal condition 4: every frozen hypothesis is closed or needs "
            "more/adequate data. No strategy was shortlisted.",
            "",
            f"RFQ status: `{RFQ_PARTIAL_STATUS}`. {RFQ_PARTIAL_BOUNDARY}",
            "Whole-object quarantine: `true`; line salvage: `false`; retained/quarantined "
            f"objects: `{quarantine['consumed_unique_objects']}` / "
            f"`{quarantine['quarantined_unique_objects']}`.",
            f"Reopen condition: {RFQ_REOPEN_CONDITION}",
            "",
            f"- hypotheses tested/screened: `{len(conclusions)}` (provenance: `REPORT/tables/FINAL_MISSION_SUMMARY.json#/hypotheses_tested`)",
            f"- COLLECT_MORE: `{counts['COLLECT_MORE']}` (provenance: `REPORT/tables/FINAL_MISSION_SUMMARY.json#/status_counts/COLLECT_MORE`)",
            f"- DATA_STARVED: `{counts['DATA_STARVED']}` (provenance: `REPORT/tables/FINAL_MISSION_SUMMARY.json#/status_counts/DATA_STARVED`)",
            f"- REJECTED: `{counts['REJECTED']}` (provenance: `REPORT/tables/FINAL_MISSION_SUMMARY.json#/status_counts/REJECTED`)",
            "",
            "## Claim ladder",
            "",
            "- Pattern exists: descriptive observations only; no promoted causal pattern.",
            "- Predicts something: not established.",
            "- Executable: not established.",
            "- Survives costs: no.",
            "- Survives conservative fills: no.",
            "- Survives unseen data: no untouched set was opened.",
            "- Authorized for live testing: no.",
            "",
            f"Estimated W09 compute cost: `{resource.get('estimated_total_compute_cost_usd')}` USD "
            "(provenance: `RESOURCE_USAGE.json#/estimated_total_compute_cost_usd`).",
            "",
            "Highest-priority gap: additional quality-assessed sealed independent day blocks, "
            "followed by authoritative mapping and exact execution economics.",
            "",
            "No live trading was authorized or executed.",
            "",
        ]
    )


def chart_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def html_report(
    run_id: str,
    sections: Sequence[tuple[str, str]],
    chart_dir: Path,
) -> str:
    nav = []
    bodies = []
    for index, (title, body) in enumerate(sections, 1):
        anchor = f"section-{index}"
        nav.append(f'<a href="#{anchor}">{index}. {html.escape(title)}</a>')
        paragraphs = "<br>".join(html.escape(line) for line in body.splitlines())
        bodies.append(
            f'<section id="{anchor}" data-search="{html.escape(title + " " + body)}">'
            f"<h2>{index}. {html.escape(title)}</h2><p>{paragraphs}</p></section>"
        )
    figures = []
    if chart_dir.is_dir():
        for path in sorted(chart_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
                continue
            figures.append(
                "<figure>"
                f'<img loading="lazy" src="{chart_data_uri(path)}" alt="{html.escape(path.stem)}">'
                f"<figcaption>{html.escape(path.name)} — provenance: REPORT/charts/{html.escape(path.name)}; "
                f"SHA-256 {sha256(path)}. Evidence: {EXPECTED_EVIDENCE}; timestamp: {EXPECTED_TIMESTAMP}; "
                f"split: {EXPECTED_SPLIT}; artifact: DIAGNOSTIC_ONLY.</figcaption></figure>"
            )
    charts = (
        '<section id="charts"><h2>Embedded charts</h2><div class="charts">'
        + "".join(figures)
        + "</div></section>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SPORTS-AUTORESEARCH-01 — {html.escape(run_id)}</title>
<style>
:root{{--bg:#07111f;--panel:#0e1c2f;--ink:#e9f1fb;--muted:#9db0c9;--accent:#54c6ff;--warn:#ffcc66}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}
header{{padding:28px;position:sticky;top:0;background:#07111ff2;border-bottom:1px solid #233651;z-index:3}}
h1{{margin:0 0 8px;font-size:22px}} .banner{{color:var(--warn);font-weight:700}} input{{width:100%;max-width:620px;padding:10px;background:#0b1728;color:var(--ink);border:1px solid #345;border-radius:7px}}
.layout{{display:grid;grid-template-columns:280px minmax(0,1fr);gap:20px;max-width:1500px;margin:auto;padding:20px}}
nav{{position:sticky;top:145px;height:calc(100vh - 170px);overflow:auto}} nav a{{display:block;color:var(--muted);padding:5px 8px;text-decoration:none}} nav a:hover{{color:var(--accent)}}
main{{min-width:0}} section{{background:var(--panel);padding:20px;margin:0 0 16px;border:1px solid #1d3450;border-radius:10px}} h2{{color:var(--accent);font-size:19px}} p{{white-space:normal;overflow-wrap:anywhere}} .charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px}} figure{{margin:0;background:#081321;padding:12px;border-radius:8px}} img{{display:block;width:100%;height:auto}} figcaption{{color:var(--muted);font-size:12px;margin-top:8px}} .hidden{{display:none}}
@media(max-width:800px){{.layout{{display:block}}nav{{position:static;height:auto;margin-bottom:15px}}}}
</style></head><body>
<header><h1>SPORTS-AUTORESEARCH-01 — {html.escape(run_id)}</h1>
<div class="banner">{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY — {NO_LIVE_BANNER}</div>
<input id="filter" aria-label="Filter report sections" placeholder="Filter sections"></header>
<div class="layout"><nav>{''.join(nav)}<a href="#charts">Embedded charts</a></nav><main>{''.join(bodies)}{charts}</main></div>
<script>
const q=document.getElementById('filter');q.addEventListener('input',()=>{{const x=q.value.toLowerCase();document.querySelectorAll('main section[data-search]').forEach(s=>s.classList.toggle('hidden',!s.dataset.search.toLowerCase().includes(x)))}});
</script></body></html>"""


def write_reproduction(
    run_dir: Path,
    manifest: Mapping[str, Any],
    l2_path: Path,
    quarantine: Mapping[str, Any],
) -> None:
    releases = manifest["selected_releases"]
    double_quarantine = quarantine.get("quarantined_unique_objects") == 2
    handling_change = (
        "TWO_EXACT_ADJACENT_WHOLE_OBJECT_QUARANTINES_ONE_CONTIGUOUS_GAP"
        if double_quarantine
        else "ONE_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
    )
    lines = [
        "# REPRODUCE",
        "",
        f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
        f"> **{NO_LIVE_BANNER}**",
        "",
        "## Frozen identity",
        "",
        f"- run_id: `{manifest['run_id']}`",
        f"- repository commit: `{manifest.get('repo_commit', (manifest.get('repository') or {}).get('commit'))}`",
        f"- initial execution commit: `{quarantine['initial_execution_commit']}`",
        f"- previous execution commit: `{quarantine['previous_execution_commit']}`",
        f"- current repair execution commit: `{quarantine['current_execution_commit']}`",
        f"- initial source manifest SHA-256: `{quarantine.get('initial_source_manifest_sha256', quarantine['previous_source_manifest_sha256'])}`",
        f"- previous source manifest SHA-256: `{quarantine['previous_source_manifest_sha256']}`",
        f"- current source manifest SHA-256: `{quarantine['current_source_manifest_sha256']}`",
        f"- initial source checksum-set SHA-256: `{quarantine.get('initial_source_sha256s_sha256', quarantine['previous_source_sha256s_sha256'])}`",
        f"- previous source checksum-set SHA-256: `{quarantine['previous_source_sha256s_sha256']}`",
        f"- current source checksum-set SHA-256: `{quarantine['current_source_sha256s_sha256']}`",
        f"- initial query-set SHA-256: `{quarantine.get('initial_query_set_sha256', quarantine['previous_query_set_sha256'])}`",
        f"- previous query-set SHA-256: `{quarantine['previous_query_set_sha256']}`",
        f"- current query-set SHA-256: `{quarantine['current_query_set_sha256']}`",
        f"- mission SHA-256: `{manifest['mission']['sha256']}`",
        f"- prompt SHA-256: `{manifest['canonical_prompt']['sha256']}`",
    ]
    for release in releases:
        lines.append(
            f"- `{release['release_id']}`: manifest `{release['manifest_sha256']}`, "
            f"VersionId-set `{release['object_version_set_sha256']}`"
        )
    lines.extend(
        [
            "",
            "## RFQ integrity repair binding",
            "",
            f"- status/scope: `{RFQ_PARTIAL_STATUS}` / `{RFQ_ANALYSIS_SCOPE}`",
            "- population: `RETAINED_OBSERVED_SUBSET`",
            "- whole-object quarantine: `true`",
            "- line salvage: `false`",
            "- registration change class: `DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE`",
            f"- handling change: `{handling_change}`",
            f"- quarantined objects / contiguous gaps / quarantine boundaries: `{quarantine['quarantined_unique_objects']}` / `{(quarantine.get('quarantine_gap_plan') or {}).get('contiguous_gap_count', 1)}` / `{(quarantine.get('quarantine_gap_plan') or {}).get('observation_boundary_count', 2)}`",
            "- hypothesis-design change: `NONE`",
            f"- declaration: `{quarantine['declaration_path']}` / `{quarantine['declaration_sha256']}`",
            f"- malformed-object receipt: `{quarantine['receipt_path']}` / `{quarantine['receipt_sha256']}`",
            f"- repair registration receipt: `{quarantine['repair_receipt_path']}` / `{quarantine['repair_receipt_sha256']}`",
            f"- failed state: `{quarantine['failed_state_path']}` / `{quarantine['failed_state_sha256']}`",
            f"- failed resource receipt: `{quarantine['failed_resource_receipt_path']}` / `{quarantine['failed_resource_receipt_sha256']}`",
            f"- failed scratch receipt: `{quarantine['failed_scratch_receipt_path']}` / `{quarantine['failed_scratch_receipt_sha256']}`",
            f"- archived failed input identity: `{quarantine['failed_input_identity_path']}` / `{quarantine['failed_input_identity_sha256']}`",
            f"- Cycle-1 DuckDB binding: `{quarantine['cycle1_duckdb_binding']['active_path']}` / `{quarantine['cycle1_duckdb_binding']['active_sha256']}`; archived `{quarantine['cycle1_duckdb_binding']['archived_path']}` / `{quarantine['cycle1_duckdb_binding']['archived_sha256']}`; DB `{quarantine['cycle1_duckdb_binding']['duckdb_sha256']}`",
            f"- completed partial-stage state: `{quarantine['completed_state_path']}` / `{quarantine['completed_state_sha256']}`",
            f"- successful repair resource receipt: `{quarantine['successful_resource_receipt_path']}` / `{quarantine['successful_resource_receipt_sha256']}`",
            f"- quarantined object-set SHA-256: `{quarantine['quarantined_object_set_sha256']}`",
            f"- retained object-set SHA-256: `{quarantine['consumed_object_set_sha256']}`",
            f"- manifest RFQ object-set SHA-256: `{quarantine['manifest_object_set_sha256']}`",
            f"- quarantine gap-set SHA-256: `{quarantine['quarantine_gap_set_sha256']}`",
            f"- claim boundary: {RFQ_PARTIAL_BOUNDARY}",
            f"- reopen condition: {RFQ_REOPEN_CONDITION}",
            "",
            "## W09-only stage order",
            "",
            "The original heavy run must remain inside `sudo /usr/local/bin/w09-run ...` on the "
            "bound W09 instance. Reverify both immutable releases through `research_data`, then run "
            "the frozen query sources in this order:",
            "",
            "1. `queries/rfq_trigger.py`",
            "2. `queries/run_cycle1.py`",
            "3. `queries/core_hypothesis_tests.py`",
            "4. Preserve the failed scratch receipt and run `queries/rfq_full_stage.py` "
            "from new scratch, with every registered receipt-bound object wholly quarantined and "
            "strict parsing for every retained object; never resume or salvage a line.",
            f"5. L2 stage producing `{l2_path}`",
            "6. `source/resource_finalize.py` (freeze resource/cost receipt)",
            "7. `queries/finalize_mission.py` (write RUN_MANIFEST last)",
            "",
            "The finalizer itself runs directly after `resource_finalize.py`; do not wrap it in "
            "`resource_runner.py`, because a wrapper write after the subprocess exits would mutate "
            "`RESOURCE_USAGE.json` after the final artifact hashes and manifest commit.",
            "",
            "Use the exact CLI arguments and environment recorded in `logs/`, "
            "`RUN_MANIFEST.json`, `RESOURCE_USAGE.json`, `QUERY_SHA256SUMS.txt`, and "
            "`DATA_INTEGRITY/W09_ATTESTATION.json`. Do not substitute local Mac data, a production "
            "database, or unbound object versions.",
            "",
            "## Offline artifact verification",
            "",
            "Run `./reproduce.sh`. The standard-library Python verifier is portable across W09 "
            "and macOS (no `sha256sum` dependency): it verifies every path enumerated in "
            "`ARTIFACT_SHA256SUMS` and checks the terminal Mode-1 manifest/ledger boundary.",
            "",
            "## Publication limitation",
            "",
            "W09's instance profile is read-only and provides no S3 report-write path. The canonical "
            "archive is this synced run directory; no S3 mutation was attempted.",
            "",
        ]
    )
    if double_quarantine:
        chain_lines = ["", "### Append-only repair chain", ""]
        for row in quarantine.get("repair_chain", []):
            chain_lines.append(
                f"- `{row['repair_id']}` registration `{row['registration_path']}` / "
                f"`{row['registration_sha256']}`; declaration `{row['declaration_path']}` / "
                f"`{row['declaration_sha256']}`; receipt `{row['receipt_path']}` / "
                f"`{row['receipt_sha256']}`."
            )
        insertion = lines.index("## W09-only stage order")
        lines[insertion:insertion] = chain_lines
    atomic_text(run_dir / "REPRODUCE.md", "\n".join(lines))
    script = """#!/usr/bin/env bash
set -euo pipefail
RUN_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$RUN_DIR"
python3 queries/finalize_mission.py --run-dir "$RUN_DIR" --verify-only
"""
    atomic_text(run_dir / "reproduce.sh", script, executable=True)


def write_daily_digest(
    run_dir: Path,
    completed_at_utc: str,
    conclusions: Mapping[str, Conclusion],
) -> None:
    date = completed_at_utc[:10]
    killed = [
        item.hypothesis_id
        for item in conclusions.values()
        if item.status in {"REJECTED", "INVALIDATED_BY_DATA", "INVALIDATED_BY_LEAKAGE"}
    ]
    text = "\n".join(
        [
            f"# DAILY DIGEST — {date}",
            "",
            f"> **{EXPECTED_EVIDENCE} / {EXPECTED_TIMESTAMP} / {EXPECTED_SPLIT} / DIAGNOSTIC_ONLY**",
            f"> **{NO_LIVE_BANNER}**",
            "",
            "## New atlas/report pages",
            "",
            "- Final data coverage, 25-section report, self-contained HTML, RFQ partial-stage and "
            "snapshot-aware L2 receipts are linked from `REPORT/index.html`.",
            "",
            "## New/killed hypotheses",
            "",
            "- Final statuses: "
            + ", ".join(
                f"`{key}`={value.status}" for key, value in sorted(conclusions.items())
            )
            + ".",
            "- Killed hypotheses: " + (", ".join(f"`{item}`" for item in killed) or "none") + ".",
            "",
            "## Anomalies",
            "",
            "- RFQ size-tail and lifecycle-mixture anomalies remain COLLECT_MORE or DATA_STARVED "
            "under the frozen sign-independent rule; they are not strategy claims.",
            f"- {RFQ_PARTIAL_BOUNDARY}",
            "- Whole-object quarantine was used and line salvage was not used. Reopen only "
            f"under this condition: {RFQ_REOPEN_CONDITION}",
            "",
            "## Next-day plan",
            "",
            "- Collect new sealed days, complete W03 quality assessment, preserve them for governed "
            "future use, improve authoritative mapping/execution inputs, then rerun frozen tests "
            "without post-outcome adjustment.",
            "",
            "## Methods links",
            "",
            "- See `METHODS.md`; no new inferential method was introduced by mission finalization.",
            "",
        ]
    )
    atomic_text(run_dir / "REPORT/DAILY_DIGEST.md", text)


def artifact_hash_lines(run_dir: Path) -> list[str]:
    excluded_roots = {"cache", "tmp", "logs"}
    excluded_files = {"ARTIFACT_SHA256SUMS", "RUN_MANIFEST.json"}
    paths = []
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir)
        if relative.parts and relative.parts[0] in excluded_roots:
            continue
        if str(relative) in excluded_files or path.name.endswith(".tmp"):
            continue
        paths.append(relative)
    return [f"{sha256(run_dir / relative)}  {relative.as_posix()}" for relative in sorted(paths)]


def write_artifact_hashes(run_dir: Path) -> None:
    lines = artifact_hash_lines(run_dir)
    if not lines:
        raise MissionFinalizationError("artifact hash inventory is empty")
    atomic_text(run_dir / "ARTIFACT_SHA256SUMS", "\n".join(lines) + "\n")


def verify_artifacts(run_dir: Path) -> None:
    manifest, ledger = validate_run_identity(run_dir)
    validate_frozen_query_source(run_dir, manifest)
    if manifest.get("status") != "COMPLETE":
        raise MissionFinalizationError("RUN_MANIFEST is not COMPLETE")
    terminal = manifest.get("terminal_condition") or {}
    if terminal.get("id") != 4:
        raise MissionFinalizationError("terminal condition is not 4")
    cards = ledger.get("hypotheses")
    if not isinstance(cards, list) or len(cards) != len(HYPOTHESIS_IDS):
        raise MissionFinalizationError("final ledger does not contain ten cards")
    for card in cards:
        status = normalize_status(card.get("status"))
        if status not in TERMINAL_MODE1_STATUSES:
            raise MissionFinalizationError(
                f"nonterminal/forbidden final status: {card.get('hypothesis_id')}={status}"
            )
    sums = run_dir / "ARTIFACT_SHA256SUMS"
    if not sums.is_file():
        raise MissionFinalizationError("ARTIFACT_SHA256SUMS is missing")
    finalization = manifest.get("finalization")
    if not isinstance(finalization, dict) or finalization.get(
        "artifact_sha256s_sha256"
    ) != sha256(sums):
        raise MissionFinalizationError(
            "artifact checksum inventory is not bound to RUN_MANIFEST"
        )
    for line_number, line in enumerate(sums.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise MissionFinalizationError(f"invalid artifact hash line {line_number}")
        expected, relative = parts
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise MissionFinalizationError(f"unsafe artifact hash path: {relative}")
        target = run_dir / relative
        if not target.is_file() or sha256(target) != expected:
            raise MissionFinalizationError(f"artifact hash mismatch: {relative}")

    summary_path = run_dir / "REPORT/tables/FINAL_MISSION_SUMMARY.json"
    resource_path = run_dir / "RESOURCE_USAGE.json"
    if finalization.get("summary") != "REPORT/tables/FINAL_MISSION_SUMMARY.json":
        raise MissionFinalizationError("final summary path binding mismatch")
    if finalization.get("summary_sha256") != sha256(summary_path):
        raise MissionFinalizationError("final summary SHA-256 binding mismatch")
    if finalization.get("report") != "REPORT/FULL_REPORT.md" or finalization.get(
        "html"
    ) != "REPORT/index.html":
        raise MissionFinalizationError("final report path binding mismatch")

    resource_binding = manifest.get("resource_usage")
    if (
        not isinstance(resource_binding, dict)
        or resource_binding.get("path") != "RESOURCE_USAGE.json"
        or resource_binding.get("sha256") != sha256(resource_path)
    ):
        raise MissionFinalizationError("resource usage manifest binding mismatch")
    resource = load_json(resource_path)
    if resource_binding.get("estimated_total_compute_cost_usd") != resource.get(
        "estimated_total_compute_cost_usd"
    ):
        raise MissionFinalizationError("resource usage cost binding mismatch")

    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise MissionFinalizationError("repository binding receipt is missing")
    repository_files = {
        "source_manifest_sha256": repository.get("source_manifest_path"),
        "feature_definition_sha256": "FEATURE_DICTIONARY.json",
        "method_definition_sha256": "METHODS.md",
        "query_set_sha256": "QUERY_SHA256SUMS.txt",
    }
    if repository.get("source_manifest_path") != "SOURCE_MANIFEST.json":
        raise MissionFinalizationError("source manifest path binding mismatch")
    for field, relative in repository_files.items():
        if not isinstance(relative, str) or repository.get(field) != sha256(
            run_dir / relative
        ):
            raise MissionFinalizationError(f"repository artifact binding mismatch: {field}")

    summary = load_json(summary_path)
    if (
        summary.get("run_id") != manifest.get("run_id")
        or summary.get("terminal_condition") != 4
        or summary.get("hypotheses_tested") != len(HYPOTHESIS_IDS)
        or summary.get("promotion_ready") != 0
        or summary.get("formal_verdict_pass") != 0
        or summary.get("shortlisted_strategies") != []
    ):
        raise MissionFinalizationError("final summary terminal binding mismatch")
    rfq = load_json(run_dir / RFQ_SUMMARY)
    validate_stage_header(
        rfq,
        manifest=manifest,
        stage_name="RFQ partial stage",
        schema_prefix="sports-autoresearch-rfq-full-stage-v1",
    )
    quarantine = validate_rfq_partial_quarantine(run_dir, manifest, rfq)
    if (
        summary.get("rfq_partial_quarantine") != quarantine
        or summary.get("rfq_claim_boundary") != RFQ_PARTIAL_BOUNDARY
        or summary.get("rfq_reopen_condition") != RFQ_REOPEN_CONDITION
        or summary.get("rfq_integrity_statement")
        != (
            "Whole-object quarantine is true and line salvage is false for the retained "
            "observed subset; one contiguous missing interval may create temporal-selection bias."
        )
        or finalization.get("rfq_coverage_status") != RFQ_PARTIAL_STATUS
        or finalization.get("rfq_quarantined_object_set_sha256")
        != quarantine["quarantined_object_set_sha256"]
        or finalization.get("rfq_line_salvage") is not False
        or finalization.get("rfq_completed_state_sha256")
        != quarantine["completed_state_sha256"]
        or finalization.get("rfq_successful_resource_receipt_sha256")
        != quarantine["successful_resource_receipt_sha256"]
        or finalization.get("rfq_repair_registration_receipt_sha256")
        != quarantine["repair_receipt_sha256"]
    ):
        raise MissionFinalizationError("final RFQ partial-quarantine binding mismatch")
    final_coverage = load_json(run_dir / "DATA_COVERAGE.json")
    if (
        final_coverage.get("coverage_status")
        != "PARTIAL_MODE1_RFQ_OBJECT_COVERAGE_QUARANTINED"
        or (final_coverage.get("stage_completeness") or {})
        .get("rfq_full_stage", {})
        .get("status")
        != RFQ_PARTIAL_STATUS
    ):
        raise MissionFinalizationError("final data coverage falsely claims completion")
    required_partial_outputs = (
        Path("REPORT/FULL_REPORT.md"),
        Path("REPORT/EXECUTIVE_SUMMARY.md"),
        Path("REPORT/index.html"),
        RFQ_CONCLUSIONS,
        Path("REPRODUCE.md"),
    )
    for relative in required_partial_outputs:
        text = (run_dir / relative).read_text(encoding="utf-8")
        if (
            "retained observed subset" not in text.lower()
            or "temporal-selection bias" not in text.lower()
            or "line salvage" not in text.lower()
        ):
            raise MissionFinalizationError(
                f"partial RFQ boundary missing from final output: {relative}"
            )
    ledger_counts = {status: 0 for status in sorted(TERMINAL_MODE1_STATUSES)}
    ledger_statuses = {}
    for card in cards:
        hypothesis_id = card.get("hypothesis_id")
        status = normalize_status(card.get("status"))
        ledger_counts[status] += 1
        ledger_statuses[hypothesis_id] = status
    summary_hypotheses = summary.get("hypotheses")
    if not isinstance(summary_hypotheses, dict) or {
        key: normalize_status(value.get("status"))
        for key, value in summary_hypotheses.items()
        if isinstance(value, dict)
    } != ledger_statuses:
        raise MissionFinalizationError("final summary/ledger hypothesis binding mismatch")
    if summary.get("status_counts") != ledger_counts:
        raise MissionFinalizationError("final summary status-count binding mismatch")
    for key in (
        "hypotheses_tested",
        "status_counts",
        "promotion_ready",
        "formal_verdict_pass",
        "shortlisted_strategies",
    ):
        if finalization.get(key) != summary.get(key):
            raise MissionFinalizationError(f"manifest/final-summary binding mismatch: {key}")


def finalize(run_dir: Path) -> None:
    manifest, ledger = validate_run_identity(run_dir)
    if manifest.get("status") == "COMPLETE":
        # A completed archive is immutable.  A rerun is verification-only and
        # must never manufacture a fresh checksum receipt for changed bytes.
        verify_artifacts(run_dir)
        print(
            "AUTORESEARCH_COMPLETE_VERIFIED "
            f"run_id={manifest['run_id']} report={run_dir / 'REPORT/FULL_REPORT.md'}"
        )
        return
    repairs = manifest.get("data_integrity_repairs")
    expected_pending_status = (
        REPAIR05_PENDING_STATUS
        if isinstance(repairs, list)
        and len(repairs) == 5
        and isinstance(repairs[4], dict)
        and repairs[4].get("repair_id") == "repair-05"
        else (
        REPAIR04_PENDING_STATUS
        if isinstance(repairs, list)
        and len(repairs) == 4
        and isinstance(repairs[3], dict)
        and repairs[3].get("repair_id") == "repair-04"
        else (
        REPAIR03_PENDING_STATUS
        if isinstance(repairs, list)
        and len(repairs) == 3
        and isinstance(repairs[2], dict)
        and repairs[2].get("repair_id") == "repair-03"
        else (
            REPAIR02_PENDING_STATUS
            if isinstance(repairs, list)
            and len(repairs) == 2
            and isinstance(repairs[1], dict)
            and repairs[1].get("repair_id") == "repair-02"
            else REPAIR_PENDING_STATUS
        )
        )
        )
    )
    if manifest.get("status") != expected_pending_status:
        raise MissionFinalizationError(
            "manifest is not at the registered RFQ quarantine-repair stage"
        )
    validate_frozen_query_source(run_dir, manifest)
    resource_path = run_dir / "RESOURCE_USAGE.json"
    resource_binding = manifest.get("resource_usage")
    if (
        not isinstance(resource_binding, dict)
        or resource_binding.get("path") != "RESOURCE_USAGE.json"
        or not resource_path.is_file()
        or resource_binding.get("sha256") != sha256(resource_path)
    ):
        raise MissionFinalizationError(
            "RESOURCE_USAGE.json is not bound to the resource-finalize manifest receipt"
        )
    resource = load_json(resource_path)
    if resource_binding.get("estimated_total_compute_cost_usd") != resource.get(
        "estimated_total_compute_cost_usd"
    ):
        raise MissionFinalizationError("resource cost estimate binding mismatch")
    completed_at_utc = manifest.get("completed_at_utc") or utc_now()

    core_path = run_dir / CORE_SUMMARY
    core = load_json(core_path)
    if core.get("run_id") != manifest["run_id"]:
        raise MissionFinalizationError("core summary run_id mismatch")
    rfq = load_json(run_dir / RFQ_SUMMARY)
    validate_stage_header(
        rfq,
        manifest=manifest,
        stage_name="RFQ partial stage",
        schema_prefix="sports-autoresearch-rfq-full-stage-v1",
    )
    quarantine = validate_rfq_partial_quarantine(run_dir, manifest, rfq)
    hard_truth = rfq.get("hard_truth")
    if not isinstance(hard_truth, dict) or hard_truth.get(
        "broadcast_contains_accepted_quote_or_fill"
    ) is not False:
        raise MissionFinalizationError("RFQ hard-truth boundary is missing")

    rfq_conclusions = build_rfq_conclusions(
        run_dir,
        manifest,
        rfq,
        quarantine,
        completed_at_utc=completed_at_utc,
    )
    atomic_json(run_dir / RFQ_CONCLUSIONS, rfq_conclusions)

    conclusions, l2, l2_path = collect_terminal_conclusions(
        run_dir, manifest, ledger, rfq_conclusions
    )
    coverage = merge_coverage(
        run_dir,
        manifest,
        rfq,
        quarantine,
        l2,
        l2_path,
        completed_at_utc=completed_at_utc,
    )
    ledger = update_ledger(
        run_dir,
        ledger,
        conclusions,
        completed_at_utc=completed_at_utc,
    )
    append_final_registry_records(
        run_dir,
        manifest,
        conclusions,
        completed_at_utc=completed_at_utc,
    )
    if resource.get("shutdown_confirmation") not in {
        "PENDING_MISSION_END",
        "PENDING_DRIVER_STOP_AFTER_SYNC",
        "CONFIRMED_STOPPED",
    }:
        raise MissionFinalizationError("resource shutdown state is missing or invalid")

    dossier_paths = write_hypothesis_artifacts(run_dir, ledger, conclusions)
    trial_records = sum(
        1
        for line in (run_dir / "TRIAL_REGISTRY.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    final_summary = {
        "schema_version": "sports-autoresearch-final-mission-summary-v1",
        "run_id": manifest["run_id"],
        "completed_at_utc": completed_at_utc,
        "mode": EXPECTED_MODE,
        "data_evidence": EXPECTED_EVIDENCE,
        "timestamp_discipline": EXPECTED_TIMESTAMP,
        "split": EXPECTED_SPLIT,
        "artifact_status": "DIAGNOSTIC_ONLY",
        "authorization": NO_LIVE_BANNER,
        "terminal_condition": 4,
        "hypotheses_tested": len(conclusions),
        "status_counts": status_counts(conclusions),
        "promotion_ready": 0,
        "formal_verdict_pass": 0,
        "shortlisted_strategies": [],
        "hypotheses": {
            key: {**value.as_dict(), "artifact": dossier_paths[key]}
            for key, value in sorted(conclusions.items())
        },
        "trial_registry_records": trial_records,
        "stage_sources": {
            "core": str(CORE_SUMMARY),
            "core_hypotheses": str(CORE_HYPOTHESIS_SUMMARY),
            "rfq": str(RFQ_SUMMARY),
            "rfq_conclusions": str(RFQ_CONCLUSIONS),
            "l2": str(l2_path),
        },
        "rfq_partial_quarantine": dict(quarantine),
        "rfq_claim_boundary": RFQ_PARTIAL_BOUNDARY,
        "rfq_reopen_condition": RFQ_REOPEN_CONDITION,
        "rfq_integrity_statement": (
            "Whole-object quarantine is true and line salvage is false for the retained "
            "observed subset; one contiguous missing interval may create temporal-selection bias."
        ),
        "publication": {
            "canonical_local_archive": str(run_dir),
            "s3_write_available": False,
            "s3_write_attempted": False,
            "reason": "W09 is read-only and this authorization provides no S3 mutation path.",
        },
        "live_trading_authorized": False,
        "live_trading_executed": False,
    }
    final_summary_path = run_dir / "REPORT/tables/FINAL_MISSION_SUMMARY.json"
    atomic_json(final_summary_path, final_summary)

    # build_report_sections expects the canonical run path only for an optional
    # registry count.  The definitive count always lives in FINAL_MISSION_SUMMARY.
    sections = build_report_sections(
        manifest,
        ledger,
        coverage,
        rfq,
        quarantine,
        l2_path,
        conclusions,
        dossier_paths,
        resource,
        trial_records,
    )
    report_dir = run_dir / "REPORT"
    atomic_text(
        report_dir / "FULL_REPORT.md",
        markdown_report(manifest["run_id"], sections),
    )
    atomic_text(
        report_dir / "EXECUTIVE_SUMMARY.md",
        executive_summary(manifest["run_id"], conclusions, resource, quarantine),
    )
    atomic_text(
        report_dir / "index.html",
        html_report(manifest["run_id"], sections, report_dir / "charts"),
    )
    write_daily_digest(run_dir, completed_at_utc, conclusions)
    write_reproduction(run_dir, manifest, l2_path, quarantine)

    write_artifact_hashes(run_dir)

    # RUN_MANIFEST is deliberately the final atomic write.  It is excluded
    # from ARTIFACT_SHA256SUMS because it commits that checksum inventory.
    manifest["status"] = "COMPLETE"
    manifest["completed_at_utc"] = completed_at_utc
    manifest["terminal_condition"] = {
        "id": 4,
        "description": "Remaining hypotheses are COLLECT_MORE or DATA_STARVED; closed terminal outcomes remain closed.",
    }
    manifest["finalization"] = {
        "schema_version": "sports-autoresearch-mission-finalization-v1",
        "summary": str(final_summary_path.relative_to(run_dir)),
        "summary_sha256": sha256(final_summary_path),
        "report": "REPORT/FULL_REPORT.md",
        "html": "REPORT/index.html",
        "artifact_sha256s": "ARTIFACT_SHA256SUMS",
        "artifact_sha256s_sha256": sha256(run_dir / "ARTIFACT_SHA256SUMS"),
        "hypotheses_tested": len(conclusions),
        "status_counts": status_counts(conclusions),
        "promotion_ready": 0,
        "formal_verdict_pass": 0,
        "shortlisted_strategies": [],
        "s3_write_available": False,
        "s3_write_attempted": False,
        "shutdown_confirmation": resource.get("shutdown_confirmation"),
        "no_live_trading_authorized_or_executed": True,
        "rfq_coverage_status": RFQ_PARTIAL_STATUS,
        "rfq_quarantined_object_set_sha256": quarantine[
            "quarantined_object_set_sha256"
        ],
        "rfq_line_salvage": False,
        "rfq_completed_state_sha256": quarantine["completed_state_sha256"],
        "rfq_successful_resource_receipt_sha256": quarantine[
            "successful_resource_receipt_sha256"
        ],
        "rfq_repair_registration_receipt_sha256": quarantine[
            "repair_receipt_sha256"
        ],
    }
    atomic_json(run_dir / "RUN_MANIFEST.json", manifest)
    print(
        "AUTORESEARCH_COMPLETE "
        f"run_id={manifest['run_id']} report={run_dir / 'REPORT/FULL_REPORT.md'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    try:
        if args.verify_only:
            verify_artifacts(run_dir)
            print(f"ARTIFACT_VERIFICATION_PASS run_id={run_dir.name}")
        else:
            finalize(run_dir)
    except Exception as exc:
        print(
            f"MISSION_FINALIZATION_BLOCKED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
