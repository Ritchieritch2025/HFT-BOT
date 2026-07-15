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
REPAIR_DECLARATION = Path(
    "DATA_INTEGRITY/repairs/repair-01/QUARANTINE_DECLARATION.json"
)
REPAIR_MALFORMED_RECEIPT = Path(
    "DATA_INTEGRITY/repairs/repair-01/MALFORMED_OBJECT_RECEIPT.json"
)
REPAIR_REGISTRATION_RECEIPT = Path(
    "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
)
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
    "RFQ findings use only the retained observed subset after one exact whole-object "
    "quarantine; the missing interval can create temporal-selection bias, so these "
    "findings are partial descriptive diagnostics, not full-coverage estimates."
)
RFQ_REOPEN_CONDITION = (
    "Reopen the frozen RFQ studies only after a structurally valid immutable replacement "
    "or governed repair restores the quarantined interval, then rerun strict parsing from "
    "new scratch with complete object coverage and additional quality-assessed sealed days."
)

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
        "observed subset after one exact whole-object quarantine, while snapshot-aware "
        "L2 has its own receipt. The missing RFQ interval can create temporal-selection "
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
        "RFQ coverage is partial: one exact malformed manifest object is wholly quarantined with no line salvage.",
        "RFQ findings use the retained observed subset and may have temporal-selection bias from the missing interval.",
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
            + "\n- RFQ status: `PARTIAL_OBJECT_COVERAGE_QUARANTINED`; the malformed "
            "object was excluded in full with no line salvage. Failure-state, resource, "
            "scratch-preservation, declaration, receipt, VersionId, set-hash, and gap-QC "
            "bindings were verified before finalization. The registration change was data-"
            "integrity handling only: one exact whole-object quarantine plus conservative "
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
        f"- initial/previous source manifest SHA-256: `{quarantine['previous_source_manifest_sha256']}`",
        f"- current source manifest SHA-256: `{quarantine['current_source_manifest_sha256']}`",
        f"- initial/previous source checksum-set SHA-256: `{quarantine['previous_source_sha256s_sha256']}`",
        f"- current source checksum-set SHA-256: `{quarantine['current_source_sha256s_sha256']}`",
        f"- initial/previous query-set SHA-256: `{quarantine['previous_query_set_sha256']}`",
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
            "- handling change: `ONE_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING`",
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
            "from new scratch, with the one receipt-bound object wholly quarantined and "
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
            "observed subset; the missing interval may create temporal-selection bias."
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
    if manifest.get("status") != REPAIR_PENDING_STATUS:
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
            "observed subset; the missing interval may create temporal-selection bias."
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
