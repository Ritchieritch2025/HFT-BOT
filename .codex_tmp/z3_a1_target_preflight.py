#!/usr/bin/env python3
"""Fast, no-trajectory reproduction of the sealed A1 admission failure."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import duckdb


os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    "/tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = "/tmp/z3_price_allocation_train.py"
os.environ["ROUND3_CAUSAL_CONTRACT"] = "/tmp/causal_replay_contract.py"

FULL = "/tmp/z3_postfill_stopping_receive_clock.py"
DATE = "2026-07-20"
MARKET = "KXBTC15M-26JUL200915-15"


def load_full():
    spec = importlib.util.spec_from_file_location("a1_preflight_full", FULL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


F = load_full()
C = F.C


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


class NullWriter:
    rows = 0

    def emit(self, _row):
        return None


class NullTracker:
    def __init__(self):
        self.results = []
        self.markouts = []
        self.episodes_started = 0

    def before_event(self, _ts):
        return None

    def apply_trade(self, _ts, _yes_px, _qty, _taker):
        return None

    def observe(self, _ts, _kind, _fields):
        return None

    def after_book(self, _ts, _fields):
        return None

    def start(self, _state, _ts):
        self.episodes_started += 1

    def finish(self, _last_ts):
        return None


class A1Found(RuntimeError):
    pass


class PreflightMarket(F.CausalTrackedMarket):
    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, policies, writer)
        self.tracker = NullTracker()

    def _admit(self, policy, state, ts, features, allocation):
        eta_y = allocation.get("pred_eta_y_s")
        eta_n = allocation.get("pred_eta_n_s")
        invalid = {}
        for side, eta in (("y", eta_y), ("n", eta_n)):
            if eta is None:
                invalid[side] = "missing"
            elif not math.isfinite(float(eta)):
                invalid[side] = "nonfinite"
            elif float(eta) <= 0:
                invalid[side] = "nonpositive"
        if invalid:
            recent = list(self.recent_trades)
            detail = {
                "schema": "z3-a1-exact-input-v1",
                "date": DATE,
                "market": self.ticker,
                "ts": int(ts),
                "close_us": int(self.close),
                "tte_s": (int(self.close) - int(ts)) / 1e6,
                "policy": {
                    "name": policy.name,
                    "allocation": policy.allocation,
                    "shift_ticks": policy.shift_ticks,
                },
                "invalid_eta_side": invalid,
                "features_at_touch": features,
                "allocation": allocation,
                "touch": {
                    "yes_e4": F.B.best(self.books["y"]),
                    "no_e4": F.B.best(self.books["n"]),
                },
                "recent_trade_window": {
                    "rows": len(recent),
                    "first_ts": recent[0][0] if recent else None,
                    "last_ts": recent[-1][0] if recent else None,
                },
                "reconstructor_audit": self.reconstructor.audit,
                "book_level_min_qty_e4": {
                    side: min(book.values()) if book else None
                    for side, book in self.books.items()
                },
                "state_before_admit": {
                    "eligible_decisions": state.eligible_decisions,
                    "skipped_support": state.skipped_support,
                    "skipped_pair_cost": state.skipped_pair_cost,
                    "skipped_post_only": state.skipped_post_only,
                    "placed": state.placed,
                    "fills": state.fills,
                    "paired": state.paired,
                    "orphan_side": state.orphan_side,
                },
            }
            print(
                "A1_DETAIL "
                + json.dumps(
                    json_safe(detail),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                flush=True,
            )
            raise A1Found("exact A1 input captured")
        super()._admit(policy, state, ts, features, allocation)


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
    print(
        json.dumps(
            {
                "date": DATE,
                "market": MARKET,
                "book_events": len(books),
                "trade_events": len(trades),
            },
            sort_keys=True,
        ),
        flush=True,
    )

    sim = PreflightMarket(
        MARKET, close_us, result, [F.P3], NullWriter()
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
            sim.on_book_causal(
                event_us,
                mtype,
                side,
                px,
                delta,
                yes_levels,
                no_levels,
                sid,
                seq,
                {
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
                },
            )
            bi += 1
        else:
            row = trades[ti]
            key = trade_key
            wall, mono, local_us, trade_id, yes_px, qty, taker = row
            event_us = C.assert_receive_clock(
                wall, mono, local_us, f"{MARKET}/trade/{trade_id}"
            )
            sim.on_trade_causal(
                event_us,
                yes_px,
                qty,
                taker,
                {
                    "trade_id": trade_id,
                    "yes_price_e4": yes_px,
                    "count_e4": qty,
                    "taker_side": taker,
                    "recv_wall_ns": wall,
                    "recv_mono_ns": mono,
                    "local_recv_ts_us": event_us,
                    "causal_channel_priority": 1,
                },
            )
            ti += 1
        if last_key is not None and key <= last_key:
            raise RuntimeError("non-strict target merge key")
        last_key = key
    print("A1_NOT_REPRODUCED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except A1Found:
        print("PREFLIGHT_STOPPED_ON_A1", flush=True)
