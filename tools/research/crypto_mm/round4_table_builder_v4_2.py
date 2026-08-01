#!/usr/bin/env python3
"""Transactional commit gate for the independent ROUND4 V4.2 tables."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
from pathlib import Path

from tools.research.crypto_mm import (
    round4_postfill_state_contract_v4_2 as V42,
)


NORMATIVE_VALIDATOR_SHA256 = (
    "d15df13f0dccb171352dd3df4211f2a4111ec5e7336dff398456299805668c24"
)
VALIDATOR_ATTESTATION_VIEW = "postfill_v42_validator_attestation"
VALIDATOR_VIOLATIONS_VIEW = "postfill_v42_validation_violations"
VALIDATOR_RUNTIME_INPUT = "postfill_v42_validator_runtime_input"


class Round4V42CommitError(V42.PostfillV42ContractError):
    """V4.2 rows cannot cross the normative transaction gate."""


def create_postfill_v42_schema(
    connection: object,
    ddl_path: os.PathLike[str] | str = V42.DEFAULT_DDL_PATH,
) -> Path:
    path = Path(ddl_path)
    connection.execute(path.read_text())
    return path


def _require_exact_validator(
    validator_sql_path: os.PathLike[str] | str,
) -> tuple[Path, str]:
    path = Path(validator_sql_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Round4V42CommitError(
            f"cannot read normative validator: {path}"
        ) from exc
    actual = hashlib.sha256(raw).hexdigest()
    if not raw or actual != NORMATIVE_VALIDATOR_SHA256:
        raise Round4V42CommitError(
            "normative validator SHA256 mismatch: "
            f"expected={NORMATIVE_VALIDATOR_SHA256} actual={actual}"
        )
    try:
        sql = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Round4V42CommitError(
            "normative validator is not UTF-8"
        ) from exc
    return path, sql


def _assert_temp_view(
    connection: object,
    view_name: str,
    expected_columns: tuple[str, ...],
) -> None:
    matches = connection.execute(
        "SELECT count(*) FROM duckdb_views() "
        "WHERE view_name = ? AND temporary",
        [view_name],
    ).fetchone()
    if matches != (1,):
        raise Round4V42CommitError(
            f"normative {view_name!r} violations view is missing"
        )
    columns = tuple(
        str(info[1])
        for info in connection.execute(
            f"PRAGMA table_info('{view_name}')"
        ).fetchall()
    )
    if columns != expected_columns:
        raise Round4V42CommitError(
            f"{view_name}: exact schema mismatch {columns!r}"
        )


def insert_postfill_v42_tables(
    connection: object,
    rows_by_table: Mapping[
        str, Sequence[Mapping[str, object]]
    ],
    *,
    source_reverification: Sequence[Mapping[str, object]],
    validator_sql_path: os.PathLike[str] | str = (
        V42.DEFAULT_VALIDATOR_SQL_PATH
    ),
) -> dict[str, object]:
    """Re-read source bytes and commit only behind the pinned SQL gate."""
    supplied = set(rows_by_table)
    expected = set(V42.POSTFILL_V42_TABLES)
    if supplied != expected:
        raise Round4V42CommitError(
            "V4.2 table roster mismatch: "
            f"missing={sorted(expected - supplied)} "
            f"extra={sorted(supplied - expected)}"
        )
    validator_path, validator_sql = _require_exact_validator(
        validator_sql_path
    )
    V42.reverify_source_bound_rows(
        rows_by_table=rows_by_table,
        source_reverification=source_reverification,
    )

    # Remove any prior TEMP objects so a missing/replaced script can never
    # inherit a clean view from an earlier transaction on this connection.
    connection.execute(
        f"DROP VIEW IF EXISTS {VALIDATOR_ATTESTATION_VIEW}"
    )
    connection.execute(
        f"DROP VIEW IF EXISTS {VALIDATOR_VIOLATIONS_VIEW}"
    )
    connection.execute(
        f"DROP TABLE IF EXISTS {VALIDATOR_RUNTIME_INPUT}"
    )
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            f"CREATE TEMP TABLE {VALIDATOR_RUNTIME_INPUT} "
            "(validator_sha256 VARCHAR NOT NULL)"
        )
        connection.execute(
            f"INSERT INTO {VALIDATOR_RUNTIME_INPUT} VALUES (?)",
            [NORMATIVE_VALIDATOR_SHA256],
        )
        row_count = 0
        for table in V42.POSTFILL_V42_TABLES:
            info = connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
            table_columns = {str(item[1]) for item in info}
            if not table_columns:
                raise Round4V42CommitError(
                    f"V4.2 owned table is missing: {table}"
                )
            required = {
                str(item[1])
                for item in info
                if bool(item[3]) and item[4] is None
            }
            for source_row in rows_by_table[table]:
                row = dict(source_row)
                columns = tuple(row)
                unknown = set(columns) - table_columns
                missing = required - set(columns)
                if unknown or missing:
                    raise Round4V42CommitError(
                        f"{table}: row/DDL mismatch "
                        f"unknown={sorted(unknown)} "
                        f"missing_required={sorted(missing)}"
                    )
                connection.execute(
                    f"INSERT INTO {table} "
                    f"({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
                row_count += 1

        connection.execute(validator_sql)
        _assert_temp_view(
            connection,
            VALIDATOR_ATTESTATION_VIEW,
            ("contract_version", "validator_sha256"),
        )
        _assert_temp_view(
            connection,
            VALIDATOR_VIOLATIONS_VIEW,
            ("violation_code", "row_key", "detail"),
        )
        attestation = connection.execute(
            f"SELECT contract_version, validator_sha256 "
            f"FROM {VALIDATOR_ATTESTATION_VIEW}"
        ).fetchall()
        if attestation != [
            (V42.CONTRACT_VERSION, NORMATIVE_VALIDATOR_SHA256)
        ]:
            raise Round4V42CommitError(
                "normative validator attestation mismatch"
            )
        violations = connection.execute(
            f"SELECT violation_code, row_key, detail "
            f"FROM {VALIDATOR_VIOLATIONS_VIEW} "
            "ORDER BY violation_code, row_key"
        ).fetchall()
        if violations:
            preview = "; ".join(
                f"{code}:{key}:{detail}"
                for code, key, detail in violations[:8]
            )
            raise Round4V42CommitError(
                "V4.2 normative SQL validation failed: "
                f"count={len(violations)} {preview}"
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return {
        "status": "POSTFILL_V42_SQL_GATE_VALID",
        "table_count": len(V42.POSTFILL_V42_TABLES),
        "row_count": row_count,
        "validator_sql_path": os.fspath(validator_path),
        "validator_sha256": NORMATIVE_VALIDATOR_SHA256,
        "fit_authorized": False,
        "candidate_selection_authorized": False,
        "deployable": False,
        "live_authorized": False,
    }
