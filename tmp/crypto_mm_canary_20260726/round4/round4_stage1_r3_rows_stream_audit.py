#!/usr/bin/env python3
"""Independent streaming audit for the sealed Stage-1 R3 rows artifact.

Input is the selected ``jq --stream`` leaf sequence from the gzip JSON.  The
audit intentionally does not import the extractor or its row contract.
"""
from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
import sys


EXPECTED_TABLE_ROWS = {
    "entry_episode": 2_182_648,
    "entry_risk_interval": 1_351,
    "entry_capital_interval": 1_216,
    "entry_stage1_outcome": 2_182_648,
}
EXPECTED_DATES = {
    "2026-07-20": 559_164,
    "2026-07-21": 1_019_662,
    "2026-07-22": 603_822,
}
EXPECTED_ACTIONS = {
    "ENTRY_SKIP": 2_181_432,
    "ENTRY_PAIR_POST_ONLY": 1_216,
}
EXPECTED_TERMINALS = {
    "ENTRY_SKIP": 2_181_432,
    "YES_FIRST": 590,
    "NO_FIRST": 612,
    "ADMIN_CENSOR_NO_FIRST_FILL": 14,
}
EXPECTED_ACKS = {
    "NOT_SENT": 2_181_432,
    "BOTH_ACKED": 1_216,
}
EXPECTED_CANONICAL_ROWS_SHA256 = (
    "a1339ced02d52d8fab7e30108ef6294f3ded668c088baab3a31946e1ef69918d"
)
FORBIDDEN_DATES = {"2026-07-23", "2026-07-26"}


def fail(message: str) -> None:
    raise SystemExit(f"ROWS_AUDIT_FAIL: {message}")


def increment(counter: Counter, value: object) -> None:
    counter["null" if value is None else str(value).lower()] += 1


def main() -> None:
    if len(sys.argv) != 2:
        fail("expected sealed first-fill receipt path")
    receipt_path = sys.argv[1]

    table_rows = Counter()
    dates = Counter()
    actions = Counter()
    terminals = Counter()
    acks = Counter()
    outcome_terminals = Counter()
    outcome_admitted = Counter()
    strict_verified = Counter()
    risk_flags = Counter()
    risk_spread = Counter()
    top_level = {}
    contract = {}
    forbidden_values = 0
    first_fill_ids = set()
    first_fill_id_rows = 0
    first_fill_outcome_indexes = set()
    first_fill_markets = set()

    for line_number, line in enumerate(sys.stdin, 1):
        try:
            streamed = json.loads(line)
        except Exception as exc:
            fail(f"invalid stream line {line_number}: {exc}")
        if len(streamed) != 2:
            continue
        path, value = streamed
        if not path:
            continue
        if len(path) == 1:
            top_level[str(path[0])] = value
            continue
        if path[0] == "contract_receipt" and len(path) == 2:
            contract[str(path[1])] = value
            continue
        if len(path) != 4 or path[0] != "tables":
            continue
        table = str(path[1])
        row_index = int(path[2])
        field = str(path[3])

        if field == "entry_episode_id":
            table_rows[table] += 1
        if field == "source_date_utc":
            if value in FORBIDDEN_DATES:
                forbidden_values += 1
            if table == "entry_episode":
                dates[str(value)] += 1
        if table == "entry_episode":
            if field == "action_kind":
                actions[str(value)] += 1
            elif field == "terminal_cause":
                terminals[str(value)] += 1
            elif field == "ack_state":
                acks[str(value)] += 1
        elif table == "entry_risk_interval":
            if field == "spread_e4":
                risk_spread[
                    "null" if value is None else "nonnull"
                ] += 1
            elif field in (
                "event_yes_first",
                "event_no_first",
                "admin_censor",
                "data_invalid",
            ):
                risk_flags[field] += int(value)
        elif table == "entry_stage1_outcome":
            if field == "terminal_type":
                outcome_terminals[str(value)] += 1
            elif field == "admitted":
                increment(outcome_admitted, value)
            elif field == "first_fill_episode_id" and value is not None:
                first_fill_id_rows += 1
                first_fill_ids.add(str(value))
                first_fill_outcome_indexes.add(row_index)
            elif field == "market_ticker":
                if row_index in first_fill_outcome_indexes:
                    first_fill_markets.add(str(value))
            elif field == "strict_trade_through_verified":
                increment(strict_verified, value)

    with gzip.open(receipt_path, "rt", encoding="utf-8") as handle:
        sealed = json.load(handle)
    expected_first_fill_ids = {
        str(row["episode_id"])
        for row in sealed["policy_rows"]
        if row["policy"] == "IOC_60MS"
    }

    if dict(table_rows) != EXPECTED_TABLE_ROWS:
        fail(f"table rows {dict(table_rows)!r}")
    if dict(dates) != EXPECTED_DATES:
        fail(f"date denominator {dict(dates)!r}")
    if dict(actions) != EXPECTED_ACTIONS:
        fail(f"action partition {dict(actions)!r}")
    if dict(terminals) != EXPECTED_TERMINALS:
        fail(f"entry terminals {dict(terminals)!r}")
    if dict(acks) != EXPECTED_ACKS:
        fail(f"ACK partition {dict(acks)!r}")
    if dict(outcome_terminals) != EXPECTED_TERMINALS:
        fail(f"outcome terminals {dict(outcome_terminals)!r}")
    if dict(outcome_admitted) != {
        "false": 2_181_432,
        "true": 1_216,
    }:
        fail(f"outcome admitted {dict(outcome_admitted)!r}")
    if dict(strict_verified) != {
        "null": 2_181_446,
        "true": 1_202,
    }:
        fail(f"strict trade-through flags {dict(strict_verified)!r}")
    if dict(risk_spread) != {"nonnull": 1_351}:
        fail(f"risk spread availability {dict(risk_spread)!r}")
    if dict(risk_flags) != {
        "event_yes_first": 590,
        "event_no_first": 612,
        "admin_censor": 14,
        "data_invalid": 0,
    }:
        fail(f"risk terminal flags {dict(risk_flags)!r}")
    if forbidden_values:
        fail(f"forbidden date values={forbidden_values}")
    if first_fill_id_rows != 1_202 or len(first_fill_ids) != 1_202:
        fail(
            "first-fill ID row/set count "
            f"{first_fill_id_rows}/{len(first_fill_ids)}"
        )
    if first_fill_ids != expected_first_fill_ids:
        fail(
            "first-fill identity difference "
            f"missing={len(expected_first_fill_ids - first_fill_ids)} "
            f"extra={len(first_fill_ids - expected_first_fill_ids)}"
        )
    if len(first_fill_markets) != 58:
        fail(f"first-fill markets={len(first_fill_markets)}")
    if contract.get("canonical_rows_sha256") != EXPECTED_CANONICAL_ROWS_SHA256:
        fail(f"canonical rows SHA {contract.get('canonical_rows_sha256')!r}")
    expected_top = {
        "schema": "z3-round4-stage1-entry-full-roster-rows-v3",
        "status": "DISCOVERY_EXTRACTION_ONLY",
        "candidate": False,
        "candidate_status": "ACTION_SET_PENDING",
        "deployable": False,
        "live_authorized": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
    }
    if any(top_level.get(key) != value for key, value in expected_top.items()):
        fail(f"top-level seal {top_level!r}")

    identity_digest = hashlib.sha256(
        "\n".join(sorted(first_fill_ids)).encode()
    ).hexdigest()
    result = {
        "status": "INDEPENDENT_STAGE1_R3_ROWS_AUDIT_OK",
        "table_rows": dict(table_rows),
        "dates": dict(dates),
        "actions": dict(actions),
        "terminals": dict(terminals),
        "acks": dict(acks),
        "outcome_admitted": dict(outcome_admitted),
        "strict_trade_through_verified": dict(strict_verified),
        "risk_spread": dict(risk_spread),
        "risk_flags": dict(risk_flags),
        "forbidden_date_values": forbidden_values,
        "first_fill_id_rows": first_fill_id_rows,
        "first_fill_id_set": len(first_fill_ids),
        "first_fill_identity_exact": True,
        "first_fill_identity_set_sha256": identity_digest,
        "first_fill_markets": len(first_fill_markets),
        "canonical_rows_sha256": contract["canonical_rows_sha256"],
        "top_level_seal": expected_top,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
