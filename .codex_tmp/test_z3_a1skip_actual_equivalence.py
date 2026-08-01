#!/usr/bin/env python3
"""Non-vacuous actual-market equivalence outside the A1 skip boundary."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import duckdb


os.environ["ROUND3_SAMPLED_PARENT"] = (
    "/tmp/z3_postfill_stopping_receive_clock_sampled.py"
)
os.environ["ROUND3_FULL_CONSUMER"] = (
    "/tmp/z3_postfill_stopping_receive_clock.py"
)
os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    "/tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = "/tmp/z3_price_allocation_train.py"
os.environ["ROUND3_CAUSAL_CONTRACT"] = "/tmp/causal_replay_contract.py"

SCRIPT = (
    "/tmp/z3_postfill_stopping_receive_clock_sampled_a1skip.preseal.py"
)
DATE = "2026-07-22"
MARKET = "KXBTC15M-26JUL212215-15"

spec = importlib.util.spec_from_file_location(
    "a1skip_actual_equivalence", SCRIPT
)
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)
F = M.F
C = F.C


class DigestWriter:
    def __init__(self):
        self.rows = 0
        self.digest = hashlib.sha256()

    def emit(self, row):
        payload = json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.digest.update(payload)
        self.rows += 1


def canonical_summary(sim):
    state = sim.states[F.P3.name]
    by_policy, current, _cycles, shadow = F.canonical_policy_rows(
        state.cycles,
        sim.tracker.results,
    )
    order = [
        "IOC_60MS",
        "WAIT_0P25",
        "WAIT_0P5",
        "WAIT_1",
        "WAIT_2",
        "WAIT_5",
        "WAIT_10",
        "WAIT_30",
        "WAIT_60",
        F.D.CURRENT_POLICY,
        F.D.ORACLE_POLICY,
    ]
    return {
        "cycles": state.cycles,
        "admissions": state.admissions,
        "bugs": state.bugs,
        "counters": {
            key: getattr(state, key)
            for key in (
                "placed",
                "fills",
                "paired",
                "flattened",
                "carried",
            )
        },
        "tracker_results": sim.tracker.results,
        "tracker_markouts": sim.tracker.markouts,
        "shadow_audit": shadow,
        "metrics": {
            name: F.D.policy_metrics(
                name, by_policy[name], current
            )
            for name in order
        },
        "markout_metrics": F.D.markout_metrics(
            sim.tracker.markouts
        ),
    }


def main():
    C.verify_pinned_contract_tools()
    C.load_gap_receipt(DATE)
    meta = F.B.load_meta()
    close_us, result = meta[MARKET]
    l2_root = F.B.L2_GLOB.split("/**", 1)[0]
    tr_root = F.B.TR_GLOB.split("/**", 1)[0]
    l2_paths = [
        str(path)
        for path in Path(l2_root).glob(
            f"subcategory=*/date={DATE}/*.parquet"
        )
    ]
    tr_paths = [
        str(path)
        for path in Path(tr_root).glob(
            f"subcategory=*/date={DATE}/*.csv.gz"
        )
    ]
    for path in l2_paths + tr_paths:
        C.assert_allowed_date_path(path, DATE)
    connection = duckdb.connect()
    connection.execute("SET threads=4")
    connection.execute("SET memory_limit='12GB'")
    trades = connection.execute(
        """
        SELECT recv_wall_ns, recv_mono_ns, local_recv_ts_us, trade_id,
               yes_price_e4, count_e4, taker_side
        FROM read_csv(?, header=true, union_by_name=true,
                      types={'taker_side':'VARCHAR'})
        WHERE market_ticker=?
        ORDER BY recv_wall_ns, recv_mono_ns, trade_id
        """,
        [tr_paths, MARKET],
    ).fetchall()
    books = connection.execute(
        """
        SELECT recv_wall_ns, recv_mono_ns, local_recv_ts_us, msg_type,
               side, price_e4, delta_e4, CAST(yes_levels AS VARCHAR),
               CAST(no_levels AS VARCHAR), ws_sid, ws_seq,
               concat_ws('|',
                   coalesce(CAST(ws_sid AS VARCHAR), '<NULL>'),
                   coalesce(CAST(ws_seq AS VARCHAR), '<NULL>'),
                   coalesce(msg_type, '<NULL>'),
                   coalesce(side, '<NULL>'),
                   coalesce(CAST(price_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(delta_e4 AS VARCHAR), '<NULL>'),
                   coalesce(CAST(yes_levels AS VARCHAR), '<NULL>'),
                   coalesce(CAST(no_levels AS VARCHAR), '<NULL>')
               ) AS stable_id
        FROM read_parquet(?, union_by_name=true)
        WHERE market_ticker=?
        ORDER BY recv_wall_ns, recv_mono_ns, ws_seq, stable_id
        """,
        [l2_paths, MARKET],
    ).fetchall()
    connection.close()

    old_writer = DigestWriter()
    new_writer = DigestWriter()
    old = M.S.SampledCausalTrackedMarket(
        MARKET, close_us, result, [F.P3], old_writer
    )
    new = M.EtaGuardedSampledMarket(
        MARKET, close_us, result, [F.P3], new_writer
    )
    ti = 0
    bi = 0
    last_key = None
    while ti < len(trades) or bi < len(books):
        trade_key = (
            C.trade_key(trades[ti][0], trades[ti][1], trades[ti][3])
            if ti < len(trades)
            else None
        )
        book_key = (
            C.book_key(books[bi][0], books[bi][1], books[bi][10], books[bi][11])
            if bi < len(books)
            else None
        )
        if book_key is not None and (
            trade_key is None or book_key < trade_key
        ):
            row = books[bi]
            key = book_key
            (
                wall,
                mono,
                local_us,
                mtype,
                side,
                px,
                delta,
                yes_json,
                no_json,
                sid,
                seq,
                stable_id,
            ) = row
            event_us = C.assert_receive_clock(
                wall, mono, local_us, f"{MARKET}/book/{sid}/{seq}"
            )
            yes_levels = no_levels = None
            if mtype == "snapshot":
                yes_levels = json.loads(yes_json) if yes_json else []
                no_levels = json.loads(no_json) if no_json else []
            fields = {
                "msg_type": mtype,
                "side": side,
                "price_e4": px,
                "delta_e4": delta,
                "ws_sid": sid,
                "ws_seq": seq,
                "recv_wall_ns": wall,
                "recv_mono_ns": mono,
                "local_recv_ts_us": event_us,
                "causal_channel_priority": 0,
                "book_stable_id": stable_id,
            }
            old.on_book_causal(
                event_us,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
                sid,
                seq,
                fields,
            )
            new.on_book_causal(
                event_us,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
                sid,
                seq,
                fields,
            )
            bi += 1
        else:
            row = trades[ti]
            key = trade_key
            wall, mono, local_us, trade_id, yes_px, qty, taker = row
            event_us = C.assert_receive_clock(
                wall, mono, local_us, f"{MARKET}/trade/{trade_id}"
            )
            fields = {
                "trade_id": trade_id,
                "yes_price_e4": yes_px,
                "count_e4": qty,
                "taker_side": taker,
                "recv_wall_ns": wall,
                "recv_mono_ns": mono,
                "local_recv_ts_us": event_us,
                "causal_channel_priority": 1,
            }
            old.on_trade_causal(
                event_us, yes_px, qty, taker, fields
            )
            new.on_trade_causal(
                event_us, yes_px, qty, taker, fields
            )
            ti += 1
        if last_key is not None and key <= last_key:
            raise RuntimeError("non-strict actual merge key")
        last_key = key

    old.finalize_with_tracker()
    new.finalize_with_tracker()
    old_summary = canonical_summary(old)
    new_summary = canonical_summary(new)
    assert old_summary == new_summary
    assert new.audit["post_allocation_eta_skips"] == 0
    assert old_writer.rows == new_writer.rows
    assert old_writer.digest.hexdigest() == new_writer.digest.hexdigest()
    summary_sha = hashlib.sha256(
        json.dumps(
            new_summary,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    print(
        json.dumps(
            {
                "schema": "z3-a1skip-actual-equivalence-v1",
                "date": DATE,
                "market": MARKET,
                "book_events": len(books),
                "trade_events": len(trades),
                "cycles": len(new.states[F.P3.name].cycles),
                "episodes": new.tracker.episodes_started,
                "post_allocation_eta_skips": 0,
                "canonical_summary_equal": True,
                "trajectory_equal": True,
                "canonical_summary_sha256": summary_sha,
                "trajectory_rows": new_writer.rows,
                "trajectory_sha256": new_writer.digest.hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
