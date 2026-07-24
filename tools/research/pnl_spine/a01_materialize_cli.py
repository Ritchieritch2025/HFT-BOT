"""Root-authorized, create-once CLI for A01 historical materialization.

The CLI deliberately does not discover files, calculate its own expected
pins, or convert parquet.  A root-owned read-only authority document must
name every input path and raw SHA-256 before this process starts.  Until a
separately audited parquet-to-contract adapter exists, production parquet is
reported as a machine blocker rather than interpreted heuristically.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
from typing import Any, Mapping, Sequence

AUTHORITY_SCHEMA = "pnl-spine-a01-materialization-authority-v1"
CLI_RECEIPT_SCHEMA = "pnl-spine-a01-materialization-cli-receipt-v1"
FIXTURE_FRAGMENT_SCHEMA = "pnl-spine-a01-fixture-fragment-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CODE_DEPENDENCY_CLOSURE = (
    "a01_materialize_cli.py",
    "a01_materializer.py",
    "contracts.py",
    "experiments.py",
)

INPUT_LAYOUT = {
    "freeze_document": "JSON_OBJECT_V1",
    "training_artifact": "JSON_OBJECT_V1",
    "source_coverage": "JSON_OBJECT_V1",
    "book_records": "JSON_ARRAY_V1",
    "metadata_records": "JSON_ARRAY_V1",
    "opportunity_records": "JSON_ARRAY_V1",
    "selection_records": "JSON_ARRAY_V1",
    "public_trade_records": "JSON_ARRAY_V1",
    "exit_requests": "JSON_ARRAY_V1",
    "settlement_records": "JSON_ARRAY_V1",
}

# These are explicit release locks, not inferred data blockers.  A subsequent
# independently audited commit must remove each lock only after the named
# consumer/evidence contract exists.  This prevents a synthetically complete
# input bundle from being mislabeled production READY.
UNRESOLVED_PRODUCTION_GATES = (
    {
        "code": "BLOCK_A01_LATENCY_FEE_DERIVATION_NOT_BOUND",
        "detail": (
            "latency values and net margin are not yet recomputed from "
            "the exact measured-latency and fee-facts receipts"
        ),
    },
    {
        "code": "BLOCK_A01_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
        "detail": (
            "full-day lifecycle, tick, and scheduled-start histories lack "
            "nonoverlapping point-in-time interval materialization"
        ),
    },
    {
        "code": "BLOCK_A01_TRADE_CLOCK_EPOCH_AND_NS_ENGINE_MISSING",
        "detail": (
            "trade receive wall/monotonic epoch reconciliation and "
            "nanosecond fill boundaries are not yet end-to-end"
        ),
    },
    {
        "code": "BLOCK_A01_OPPORTUNITY_DENOMINATOR_LEDGER_MISSING",
        "detail": (
            "runner fixture cannot yet retain every zero-trigger and "
            "zero-fill authorized root"
        ),
    },
)


class A01CliError(ValueError):
    """A trusted CLI control or pinned input is invalid."""


# These names remain deliberately unbound until the complete local dependency
# closure has passed the external authority's raw-byte pins.  In particular,
# production must execute this file by absolute path, never with ``python -m``:
# importing the package first would execute local code before the authority
# gate.
A01CollectionCoverage: Any = None
A01GateThreshold: Any = None
A01MaterializerError: Any = None
A01SourceCoverage: Any = None
A01TrainingArtifact: Any = None
SOURCE_COVERAGE_SCHEMA: Any = None
TRAIN_ARTIFACT_SCHEMA: Any = None
materialize_a01: Any = None


def canonical_json_bytes(value: object) -> bytes:
    """Canonical JSON implemented locally for the pre-import trust gate."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise A01CliError(f"{name} must be a lowercase SHA-256")
    return value


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise A01CliError(f"{name} must be nonempty text")
    return value


def _exact_keys(
    name: str,
    value: Mapping[str, Any],
    expected: set[str],
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise A01CliError(
            f"{name} keys mismatch; missing={missing}, extra={extra}"
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise A01CliError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise A01CliError(f"floating-point JSON is forbidden: {value}")


def _parse_json(
    raw: bytes,
    *,
    label: str,
    allow_float: bool = False,
) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise A01CliError(f"{label} is not UTF-8 JSON") from exc
    try:
        kwargs: dict[str, Any] = {
            "object_pairs_hook": _strict_object,
            "parse_constant": _reject_float,
        }
        if not allow_float:
            kwargs["parse_float"] = _reject_float
        return json.loads(text, **kwargs)
    except (json.JSONDecodeError, A01CliError) as exc:
        raise A01CliError(f"{label} is invalid strict JSON: {exc}") from exc


def _read_regular_file(
    path_text: str,
    *,
    label: str,
    require_root_read_only: bool,
) -> bytes:
    path = Path(path_text)
    if not path.is_absolute():
        raise A01CliError(f"{label} path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise A01CliError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise A01CliError(f"{label} must be a regular file")
        if require_root_read_only:
            if before.st_uid != 0:
                raise A01CliError(f"{label} must be owned by root")
            if before.st_mode & 0o022:
                raise A01CliError(
                    f"{label} must not be group/world writable"
                )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise A01CliError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_pinned_json(
    descriptor: Mapping[str, Any],
    *,
    name: str,
) -> tuple[object, str]:
    _exact_keys(
        f"authority.inputs.{name}",
        descriptor,
        {"path", "raw_sha256", "format"},
    )
    path = _text(f"{name}.path", descriptor.get("path"))
    expected_sha = _sha(
        f"{name}.raw_sha256", descriptor.get("raw_sha256")
    )
    declared_format = _text(
        f"{name}.format", descriptor.get("format")
    )
    expected_format = INPUT_LAYOUT[name]
    if declared_format.startswith("PARQUET"):
        raise A01CliError(
            "BLOCK_A01_PARQUET_ADAPTER_NOT_IMPLEMENTED:"
            f"{name}:{path}"
        )
    if declared_format != expected_format:
        raise A01CliError(
            f"{name} format must be {expected_format}"
        )
    raw = _read_regular_file(
        path,
        label=name,
        require_root_read_only=False,
    )
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        raise A01CliError(
            f"BLOCK_A01_RAW_SHA_MISMATCH:{name}"
        )
    document = _parse_json(
        raw,
        label=name,
        allow_float=name == "freeze_document",
    )
    if expected_format == "JSON_OBJECT_V1":
        if not isinstance(document, Mapping):
            raise A01CliError(f"{name} must contain one JSON object")
    elif not isinstance(document, list):
        raise A01CliError(f"{name} must contain one JSON array")
    return document, actual_sha


def _training_artifact(value: object) -> A01TrainingArtifact:
    if not isinstance(value, Mapping):
        raise A01CliError("training_artifact must be an object")
    _exact_keys(
        "training_artifact",
        value,
        {
            "schema_version",
            "freeze_sha256",
            "train_dates_utc",
            "quantile_rule",
            "source_manifest_sha256",
            "thresholds",
        },
    )
    raw_thresholds = value.get("thresholds")
    if not isinstance(raw_thresholds, list):
        raise A01CliError("training_artifact.thresholds must be an array")
    thresholds: list[A01GateThreshold] = []
    for index, raw in enumerate(raw_thresholds):
        if not isinstance(raw, Mapping):
            raise A01CliError(
                f"training_artifact.thresholds[{index}] must be an object"
            )
        _exact_keys(
            f"training_artifact.thresholds[{index}]",
            raw,
            {
                "stratum_key",
                "sample_count",
                "activity_p75",
                "toxicity_p90_e6",
                "adverse_width_p90_e4",
            },
        )
        try:
            thresholds.append(A01GateThreshold(**dict(raw)))
        except TypeError as exc:
            raise A01CliError(
                f"training_artifact.thresholds[{index}] is invalid"
            ) from exc
    dates = value.get("train_dates_utc")
    if not isinstance(dates, list):
        raise A01CliError(
            "training_artifact.train_dates_utc must be an array"
        )
    try:
        return A01TrainingArtifact(
            schema_version=value.get("schema_version"),
            freeze_sha256=value.get("freeze_sha256"),
            train_dates_utc=tuple(dates),
            quantile_rule=value.get("quantile_rule"),
            source_manifest_sha256=value.get(
                "source_manifest_sha256"
            ),
            thresholds=tuple(thresholds),
        )
    except (TypeError, A01MaterializerError) as exc:
        raise A01CliError(f"training_artifact is invalid: {exc}") from exc


def _source_coverage(value: object) -> A01SourceCoverage:
    if not isinstance(value, Mapping):
        raise A01CliError("source_coverage must be an object")
    _exact_keys(
        "source_coverage",
        value,
        {
            "schema_version",
            "stage",
            "cohort_dates_utc",
            "opportunity_policy_sha256",
            "opportunity_schedule_sha256",
            "fee_facts_sha256",
            "measured_latency_receipt_sha256",
            "selection_policy_sha256",
            "root_map_sha256",
            "scheduled_start_source_sha256",
            "risk_policy_sha256",
            "terminal_contract_sha256",
            "strict_fill_evidence_sha256",
            "place_latency_ns",
            "cancel_latency_ns",
            "ioc_exit_latency_ns",
            "collections",
        },
    )
    raw_collections = value.get("collections")
    if not isinstance(raw_collections, list):
        raise A01CliError("source_coverage.collections must be an array")
    collections: list[A01CollectionCoverage] = []
    for index, raw in enumerate(raw_collections):
        if not isinstance(raw, Mapping):
            raise A01CliError(
                f"source_coverage.collections[{index}] must be an object"
            )
        _exact_keys(
            f"source_coverage.collections[{index}]",
            raw,
            {
                "collection",
                "manifest_sha256",
                "records_sha256",
                "record_count",
            },
        )
        try:
            collections.append(A01CollectionCoverage(**dict(raw)))
        except (TypeError, A01MaterializerError) as exc:
            raise A01CliError(
                f"source_coverage.collections[{index}] is invalid: {exc}"
            ) from exc
    dates = value.get("cohort_dates_utc")
    if not isinstance(dates, list):
        raise A01CliError(
            "source_coverage.cohort_dates_utc must be an array"
        )
    try:
        return A01SourceCoverage(
            schema_version=value.get("schema_version"),
            stage=value.get("stage"),
            cohort_dates_utc=tuple(dates),
            opportunity_policy_sha256=value.get(
                "opportunity_policy_sha256"
            ),
            opportunity_schedule_sha256=value.get(
                "opportunity_schedule_sha256"
            ),
            fee_facts_sha256=value.get("fee_facts_sha256"),
            measured_latency_receipt_sha256=value.get(
                "measured_latency_receipt_sha256"
            ),
            selection_policy_sha256=value.get(
                "selection_policy_sha256"
            ),
            root_map_sha256=value.get("root_map_sha256"),
            scheduled_start_source_sha256=value.get(
                "scheduled_start_source_sha256"
            ),
            risk_policy_sha256=value.get("risk_policy_sha256"),
            terminal_contract_sha256=value.get(
                "terminal_contract_sha256"
            ),
            strict_fill_evidence_sha256=value.get(
                "strict_fill_evidence_sha256"
            ),
            place_latency_ns=value.get("place_latency_ns"),
            cancel_latency_ns=value.get("cancel_latency_ns"),
            ioc_exit_latency_ns=value.get("ioc_exit_latency_ns"),
            collections=tuple(collections),
        )
    except (TypeError, A01MaterializerError) as exc:
        raise A01CliError(f"source_coverage is invalid: {exc}") from exc


def _validate_authority(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise A01CliError("authority must be a JSON object")
    _exact_keys(
        "authority",
        value,
        {
            "schema_version",
            "authority_id",
            "stage",
            "code_files",
            "expected_training_artifact_sha256",
            "expected_source_coverage_sha256",
            "expected_opportunity_policy_sha256",
            "expected_fee_facts_sha256",
            "expected_measured_latency_receipt_sha256",
            "expected_selection_policy_sha256",
            "inputs",
            "output_path",
        },
    )
    if value.get("schema_version") != AUTHORITY_SCHEMA:
        raise A01CliError("authority schema_version is invalid")
    _text("authority_id", value.get("authority_id"))
    if value.get("stage") not in {"TRAIN", "VALIDATION"}:
        raise A01CliError("authority stage must be TRAIN or VALIDATION")
    for key in (
        "expected_training_artifact_sha256",
        "expected_source_coverage_sha256",
        "expected_opportunity_policy_sha256",
        "expected_fee_facts_sha256",
        "expected_measured_latency_receipt_sha256",
        "expected_selection_policy_sha256",
    ):
        _sha(key, value.get(key))
    code_files = value.get("code_files")
    if not isinstance(code_files, Mapping):
        raise A01CliError("authority.code_files must be an object")
    _exact_keys(
        "authority.code_files",
        code_files,
        set(CODE_DEPENDENCY_CLOSURE),
    )
    for filename in CODE_DEPENDENCY_CLOSURE:
        descriptor = code_files.get(filename)
        if not isinstance(descriptor, Mapping):
            raise A01CliError(
                f"authority.code_files.{filename} must be an object"
            )
        _exact_keys(
            f"authority.code_files.{filename}",
            descriptor,
            {"path", "raw_sha256"},
        )
        _text(
            f"authority.code_files.{filename}.path",
            descriptor.get("path"),
        )
        _sha(
            f"authority.code_files.{filename}.raw_sha256",
            descriptor.get("raw_sha256"),
        )
    inputs = value.get("inputs")
    if not isinstance(inputs, Mapping):
        raise A01CliError("authority.inputs must be an object")
    _exact_keys("authority.inputs", inputs, set(INPUT_LAYOUT))
    output_path = Path(_text("output_path", value.get("output_path")))
    if not output_path.is_absolute():
        raise A01CliError("authority output_path must be absolute")
    return value


def _verified_code_closure(
    code_files: Mapping[str, Any],
    *,
    require_root_read_only: bool,
) -> tuple[
    dict[str, str],
    dict[str, bytes],
    list[dict[str, str]],
]:
    """Validate every importable local byte before importing any of it."""

    invoked_cli = Path(__file__)
    if not invoked_cli.is_absolute():
        raise A01CliError(
            "production CLI must be invoked by an absolute script path"
        )
    code_dir = invoked_cli.parent
    if os.path.realpath(code_dir) != str(code_dir):
        raise A01CliError("A01 code directory may not traverse symlinks")
    if require_root_read_only:
        current = code_dir
        while True:
            try:
                info = os.stat(current, follow_symlinks=False)
            except OSError as exc:
                raise A01CliError(
                    "A01 code directory ancestry cannot be inspected"
                ) from exc
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise A01CliError(
                    "A01 code directory ancestry must be root-owned "
                    "and not group/world writable"
                )
            if current == current.parent:
                break
            current = current.parent

    actual_hashes: dict[str, str] = {}
    verified_sources: dict[str, bytes] = {}
    blockers: list[dict[str, str]] = []
    for filename in CODE_DEPENDENCY_CLOSURE:
        descriptor = code_files[filename]
        declared_path = str(descriptor["path"])
        expected_path = str(code_dir / filename)
        if declared_path != expected_path:
            blockers.append(
                {
                    "code": "BLOCK_A01_CODE_PATH_MISMATCH",
                    "detail": (
                        f"{filename}:authority path is not the exact "
                        "installed dependency path"
                    ),
                }
            )
            continue
        if os.path.realpath(declared_path) != declared_path:
            blockers.append(
                {
                    "code": "BLOCK_A01_CODE_PATH_SYMLINK",
                    "detail": filename,
                }
            )
            continue
        try:
            raw = _read_regular_file(
                declared_path,
                label=f"code:{filename}",
                require_root_read_only=require_root_read_only,
            )
        except A01CliError as exc:
            blockers.append(
                {
                    "code": "BLOCK_A01_CODE_NOT_ROOT_READ_ONLY",
                    "detail": f"{filename}:{exc}",
                }
            )
            continue
        actual_sha = hashlib.sha256(raw).hexdigest()
        actual_hashes[filename] = actual_sha
        if actual_sha != descriptor["raw_sha256"]:
            blockers.append(
                {
                    "code": "BLOCK_A01_CODE_PIN_MISMATCH",
                    "detail": filename,
                }
            )
        else:
            verified_sources[filename] = raw
    return actual_hashes, verified_sources, blockers


def _load_verified_dependencies(
    verified_sources: Mapping[str, bytes],
    code_raw_hashes: Mapping[str, str],
) -> None:
    """Compile exactly the verified bytes, bypassing sys.path and pyc."""

    global A01CollectionCoverage
    global A01GateThreshold
    global A01MaterializerError
    global A01SourceCoverage
    global A01TrainingArtifact
    global SOURCE_COVERAGE_SCHEMA
    global TRAIN_ARTIFACT_SCHEMA
    global materialize_a01

    code_dir = Path(__file__).parent
    namespace = (
        "_a01_verified_"
        + canonical_sha256(dict(code_raw_hashes))[:24]
    )
    package = types.ModuleType(namespace)
    package.__package__ = namespace
    package.__path__ = []  # type: ignore[attr-defined]
    package.__file__ = str(code_dir)
    sys.modules[namespace] = package

    loaded: dict[str, types.ModuleType] = {}
    try:
        for stem in ("contracts", "experiments", "a01_materializer"):
            filename = f"{stem}.py"
            module_name = f"{namespace}.{stem}"
            module = types.ModuleType(module_name)
            module.__file__ = str(code_dir / filename)
            module.__package__ = namespace
            sys.modules[module_name] = module
            source = verified_sources[filename]
            code = compile(
                source,
                module.__file__,
                "exec",
                dont_inherit=True,
            )
            exec(code, module.__dict__)
            loaded[stem] = module
    except Exception:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
        raise
    module = loaded["a01_materializer"]
    A01CollectionCoverage = module.A01CollectionCoverage
    A01GateThreshold = module.A01GateThreshold
    A01MaterializerError = module.A01MaterializerError
    A01SourceCoverage = module.A01SourceCoverage
    A01TrainingArtifact = module.A01TrainingArtifact
    SOURCE_COVERAGE_SCHEMA = module.SOURCE_COVERAGE_SCHEMA
    TRAIN_ARTIFACT_SCHEMA = module.TRAIN_ARTIFACT_SCHEMA
    materialize_a01 = module.materialize_a01


def execute_authority(
    authority: Mapping[str, Any],
    *,
    authority_raw_sha256: str,
    require_root_read_only_code: bool,
) -> dict[str, Any]:
    """Execute an already root-authenticated authority without any writes."""

    authority = _validate_authority(authority)
    _sha("authority_raw_sha256", authority_raw_sha256)
    input_raw_hashes: dict[str, str] = {}
    blockers: list[dict[str, str]] = []
    (
        code_raw_hashes,
        verified_sources,
        code_blockers,
    ) = _verified_code_closure(
        authority["code_files"],
        require_root_read_only=require_root_read_only_code,
    )
    blockers.extend(code_blockers)
    if not blockers:
        try:
            _load_verified_dependencies(
                verified_sources,
                code_raw_hashes,
            )
        except Exception as exc:
            blockers.append(
                {
                    "code": "BLOCK_A01_VERIFIED_CODE_LOAD_FAILED",
                    "detail": str(exc),
                }
            )

    documents: dict[str, object] = {}
    if not blockers:
        inputs = authority["inputs"]
        for name in INPUT_LAYOUT:
            try:
                document, raw_sha = _read_pinned_json(
                    inputs[name],
                    name=name,
                )
            except A01CliError as exc:
                text = str(exc)
                code = (
                    text.split(":", 1)[0]
                    if text.startswith("BLOCK_A01_")
                    else "BLOCK_A01_CLI_INPUT_INVALID"
                )
                blockers.append(
                    {"code": code, "detail": f"{name}:{text}"}
                )
                break
            documents[name] = document
            input_raw_hashes[name] = raw_sha

    result_payload: dict[str, Any] | None = None
    fixture_fragment: dict[str, Any] | None = None
    if not blockers:
        try:
            artifact = _training_artifact(
                documents["training_artifact"]
            )
            coverage = _source_coverage(
                documents["source_coverage"]
            )
            result = materialize_a01(
                stage=authority["stage"],
                freeze_document=documents["freeze_document"],
                training_artifact=artifact,
                expected_training_artifact_sha256=authority[
                    "expected_training_artifact_sha256"
                ],
                source_coverage=coverage,
                expected_source_coverage_sha256=authority[
                    "expected_source_coverage_sha256"
                ],
                expected_opportunity_policy_sha256=authority[
                    "expected_opportunity_policy_sha256"
                ],
                expected_fee_facts_sha256=authority[
                    "expected_fee_facts_sha256"
                ],
                expected_measured_latency_receipt_sha256=authority[
                    "expected_measured_latency_receipt_sha256"
                ],
                expected_selection_policy_sha256=authority[
                    "expected_selection_policy_sha256"
                ],
                book_records=documents["book_records"],
                metadata_records=documents["metadata_records"],
                opportunity_records=documents[
                    "opportunity_records"
                ],
                selection_records=documents["selection_records"],
                public_trade_records=documents["public_trade_records"],
                exit_requests=documents["exit_requests"],
                settlement_records=documents["settlement_records"],
            )
        except (A01CliError, A01MaterializerError, TypeError) as exc:
            blockers.append(
                {
                    "code": "BLOCK_A01_MATERIALIZER_CONTRACT_INVALID",
                    "detail": str(exc),
                }
            )
        else:
            result_payload = result.to_dict()
            blockers.extend(result.blockers)
            if result.ready:
                blockers.extend(UNRESOLVED_PRODUCTION_GATES)
            if result.ready and not blockers:
                fixture_fragment = {
                    "schema_version": FIXTURE_FRAGMENT_SCHEMA,
                    "stage": result.stage,
                    "cohort_dates_utc": list(
                        result.cohort_dates_utc
                    ),
                    "training_artifact_sha256": (
                        result.training_artifact_sha256
                    ),
                    "source_coverage_sha256": (
                        result.source_coverage_sha256
                    ),
                    "fee_facts_sha256": result.fee_facts_sha256,
                    "measured_latency_receipt_sha256": (
                        result.measured_latency_receipt_sha256
                    ),
                    "selection_policy_sha256": (
                        result.selection_policy_sha256
                    ),
                    "opportunity_policy_sha256": (
                        result.opportunity_policy_sha256
                    ),
                    "rows": list(result.rows),
                    "public_trades": list(result.public_trades),
                    "exit_snapshots": list(result.exit_snapshots),
                    "closures": list(result.closures),
                    "settlements": list(result.settlements),
                }
                fixture_fragment["payload_sha256"] = canonical_sha256(
                    fixture_fragment
                )

    blockers = sorted(
        (
            {
                "code": str(row["code"]),
                "detail": str(row["detail"]),
            }
            for row in blockers
        ),
        key=lambda row: (row["code"], row["detail"]),
    )
    receipt: dict[str, Any] = {
        "schema_version": CLI_RECEIPT_SCHEMA,
        "authority_id": authority["authority_id"],
        "authority_raw_sha256": authority_raw_sha256,
        "stage": authority["stage"],
        "state": "READY" if not blockers and fixture_fragment else "BLOCKED",
        "code_raw_sha256": code_raw_hashes,
        "input_raw_sha256": input_raw_hashes,
        "materialization": result_payload,
        "fixture_fragment": fixture_fragment,
        "blockers": blockers,
    }
    receipt["payload_sha256"] = canonical_sha256(receipt)
    return receipt


def write_receipt_create_once(
    output_path: Path,
    document: Mapping[str, Any],
) -> str:
    """Publish one canonical receipt without overwrite or symlink following."""

    if not output_path.is_absolute():
        raise A01CliError("create-once output path must be absolute")
    parent = output_path.parent
    if os.path.realpath(parent) != str(parent):
        raise A01CliError(
            "create-once output parent may not traverse symlinks"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        raise A01CliError(
            "create-once output parent cannot be opened safely"
        ) from exc

    raw = canonical_json_bytes(document) + b"\n"
    raw_sha = hashlib.sha256(raw).hexdigest()
    temporary_name = (
        f".{output_path.name}.tmp.{os.getpid()}."
        f"{os.urandom(12).hex()}"
    )
    temporary_fd: int | None = None
    try:
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        temporary_fd = os.open(
            temporary_name,
            create_flags,
            0o400,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(raw):
            written = os.write(temporary_fd, raw[offset:])
            if written <= 0:
                raise A01CliError(
                    "create-once output write made no progress"
                )
            offset += written
        os.fsync(temporary_fd)
        os.fchmod(temporary_fd, 0o444)
        os.close(temporary_fd)
        temporary_fd = None
        try:
            os.link(
                temporary_name,
                output_path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise A01CliError(
                    "create-once refuses overwrite"
                ) from exc
            raise A01CliError(
                "create-once output publish failed"
            ) from exc
        os.fsync(directory_fd)
        return raw_sha
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Root-authorized, read-only A01 materialization preflight; "
            "writes one create-once blocker receipt or fixture fragment"
        )
    )
    parser.add_argument(
        "--authority",
        required=True,
        help="absolute root-owned read-only authority JSON path",
    )
    parser.add_argument(
        "--authority-sha256",
        required=True,
        help="operator-pinned raw SHA-256 of the authority JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if __package__:
        os.write(
            2,
            (
                b"a01-materialize: production forbids python -m; "
                b"invoke the absolute script path\n"
            ),
        )
        return 3
    args = _parser().parse_args(argv)
    try:
        authority_pin = _sha(
            "authority-sha256", args.authority_sha256
        )
        authority_raw = _read_regular_file(
            args.authority,
            label="authority",
            require_root_read_only=True,
        )
        actual_authority_sha = hashlib.sha256(
            authority_raw
        ).hexdigest()
        if actual_authority_sha != authority_pin:
            raise A01CliError("authority raw SHA-256 mismatch")
        authority = _validate_authority(
            _parse_json(authority_raw, label="authority")
        )
        receipt = execute_authority(
            authority,
            authority_raw_sha256=actual_authority_sha,
            require_root_read_only_code=True,
        )
        output = Path(str(authority["output_path"]))
        output_raw_sha = write_receipt_create_once(output, receipt)
    except (A01CliError, OSError) as exc:
        os.write(2, f"a01-materialize: {exc}\n".encode("utf-8"))
        return 3
    summary = {
        "state": receipt["state"],
        "output": str(output),
        "output_raw_sha256": output_raw_sha,
        "receipt_payload_sha256": receipt["payload_sha256"],
    }
    os.write(1, canonical_json_bytes(summary) + b"\n")
    return 0 if receipt["state"] == "READY" else 2


if __name__ == "__main__":
    sys.exit(main())
