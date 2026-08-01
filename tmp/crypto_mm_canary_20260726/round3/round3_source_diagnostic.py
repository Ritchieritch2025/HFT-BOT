#!/usr/bin/env python3
"""Read-only diagnosis of ROUND2 negative displayed book quantities.

This is not a strategy replay.  It compares the legacy ``ts_utc`` ordering
against the causal local-receive ordering on explicitly named discovery
markets, applies snapshots and signed deltas without repair/clamping, and
emits a compact root-cause receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os

import duckdb


ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FORBIDDEN_DATE = "2026-07-23"
L2_TEMPLATE = (
    "/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/"
    "category=Crypto/subcategory=*/date={date}/*.parquet"
)
QUALITY_TEMPLATE = (
    "/home/ubuntu/hft-bot/work/event_packs/l2_gaps_{date}.json"
)


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replay(rows):
    books = {"yes": {}, "no": {}}
    have_snapshot = False
    negatives = []
    deltas_before_snapshot = 0
    sequence_regressions = 0
    receive_regressions = 0
    last_seq = None
    last_recv = None
    snapshots = 0
    deltas = 0
    for row in rows:
        (
            ts_utc,
            msg_type,
            side,
            price_e4,
            delta_e4,
            yes_levels,
            no_levels,
            ws_sid,
            ws_seq,
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
        ) = row
        recv_key = (int(recv_wall_ns), int(recv_mono_ns), int(ws_seq))
        if last_recv is not None and recv_key <= last_recv:
            receive_regressions += 1
        last_recv = recv_key
        if msg_type == "snapshot":
            snapshots += 1
            yes = json.loads(yes_levels or "[]")
            no = json.loads(no_levels or "[]")
            books = {
                "yes": {int(p): int(q) for p, q in yes},
                "no": {int(p): int(q) for p, q in no},
            }
            have_snapshot = True
            last_seq = int(ws_seq)
            continue
        if msg_type != "delta":
            raise RuntimeError(f"unexpected msg_type {msg_type!r}")
        deltas += 1
        if not have_snapshot:
            deltas_before_snapshot += 1
            continue
        seq = int(ws_seq)
        if last_seq is not None and seq <= last_seq:
            sequence_regressions += 1
        last_seq = seq
        book = books[side]
        before = int(book.get(int(price_e4), 0))
        after = before + int(delta_e4)
        if after < 0:
            negatives.append(
                {
                    "ts_utc": int(ts_utc),
                    "local_recv_ts_us": int(local_recv_ts_us),
                    "recv_wall_ns": int(recv_wall_ns),
                    "recv_mono_ns": int(recv_mono_ns),
                    "ws_sid": int(ws_sid),
                    "ws_seq": seq,
                    "side": side,
                    "price_e4": int(price_e4),
                    "before_e4": before,
                    "delta_e4": int(delta_e4),
                    "after_e4": after,
                }
            )
        if after == 0:
            book.pop(int(price_e4), None)
        else:
            book[int(price_e4)] = after
    return {
        "rows": len(rows),
        "snapshots": snapshots,
        "deltas": deltas,
        "deltas_before_snapshot": deltas_before_snapshot,
        "negative_events": len(negatives),
        "first_negative": negatives[0] if negatives else None,
        "sequence_regressions_in_iteration_order": sequence_regressions,
        "receive_regressions_in_iteration_order": receive_regressions,
        "final_negative_levels": sum(
            value < 0 for book in books.values() for value in book.values()
        ),
    }


def load_rows(connection, paths, ticker, order_clause):
    query = f"""
        SELECT ts_utc, msg_type, side, price_e4, delta_e4,
               CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR),
               ws_sid, ws_seq, recv_wall_ns, recv_mono_ns, local_recv_ts_us
        FROM read_parquet(?, union_by_name=true)
        WHERE market_ticker=?
        ORDER BY {order_clause}
    """
    return connection.execute(query, [paths, ticker]).fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, choices=ALLOWED_DATES)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if FORBIDDEN_DATE in args.ticker:
        raise RuntimeError("forbidden-date token in ticker")
    root = L2_TEMPLATE.format(date=args.date).split("*", 1)[0]
    date_root = os.path.dirname(root.rstrip("/"))
    paths = [
        os.path.join(path, name)
        for path, _dirs, files in os.walk(date_root)
        for name in files
        if name.endswith(".parquet")
    ]
    paths = sorted(path for path in paths if f"date={args.date}" in path)
    if not paths:
        raise RuntimeError(f"no exact-date parquet paths for {args.date}")
    if any(FORBIDDEN_DATE in path for path in paths):
        raise RuntimeError("forbidden date path resolved")

    quality_path = QUALITY_TEMPLATE.format(date=args.date)
    quality_bytes = open(quality_path, "rb").read()
    quality = json.loads(quality_bytes)
    quality_core = {
        key: quality.get(key)
        for key in (
            "date",
            "no_l2_files",
            "sids_total",
            "sids_with_seq_gaps",
            "seq_gap_events",
            "seq_missed_total",
            "seq_regressions",
            "stream_restarts",
            "recorder_markers",
            "markers_lost_frames",
            "snapshot_re_anchors_total",
            "parse_errors",
        )
    }

    connection = duckdb.connect()
    old_rows = load_rows(
        connection, paths, args.ticker, "ts_utc, ws_seq"
    )
    causal_rows = load_rows(
        connection,
        paths,
        args.ticker,
        "recv_wall_ns, recv_mono_ns, ws_seq",
    )
    connection.close()
    old_seq = [(row[9], row[10], row[8]) for row in old_rows]
    causal_seq = [(row[9], row[10], row[8]) for row in causal_rows]
    receipt = {
        "schema": "z3-round3-source-diagnostic-v1",
        "claim": "ROOT_CAUSE_DIAGNOSTIC_ONLY",
        "date": args.date,
        "ticker": args.ticker,
        "forbidden_date_opened": False,
        "paths": [
            {"path": path, "bytes": os.path.getsize(path)}
            for path in paths
        ],
        "quality_receipt": {
            "path": quality_path,
            "sha256": hashlib.sha256(quality_bytes).hexdigest(),
            "core": quality_core,
        },
        "legacy_ts_utc_order": replay(old_rows),
        "causal_receive_order": replay(causal_rows),
        "same_event_multiset": sorted(old_seq) == sorted(causal_seq),
        "event_order_differs": old_seq != causal_seq,
        "root_cause": (
            "ts_utc is COALESCE(exchange_ts_us, local_recv_ts_us) and is "
            "explicitly legacy-only; sorting on it can move exchange-timed "
            "deltas ahead of their locally received snapshot.  Causal replay "
            "must use recv_wall_ns/recv_mono_ns and signed delta semantics."
        ),
    }
    with open(args.out, "w") as handle:
        json.dump(receipt, handle, indent=2)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
