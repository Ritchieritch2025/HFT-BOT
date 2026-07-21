#!/usr/bin/env python3
"""Load live_e2e / ws_shadow run data into a SQLite database for research.

Stdlib only (json + sqlite3). Idempotent per run: reloading the same run
directory replaces that run's rows.

Usage:
    python3 tools/load_db.py <run_dir> [more_run_dirs...] [--db market_data.db]
    python3 tools/load_db.py work/e2e_1783216000000 --db market_data.db

Accepts any directory containing some of:
    capture.ndjson   raw WS frames (byte-exact, from WsRecorder) + markers
    orders.ndjson    order_round records from the live_e2e order leg
    books.ndjson     final book depth per ticker
    summary.json     run summary metrics
A bare .ndjson file path also works (loaded as capture into run_id = filename).

Schema:
    runs(run_id PK, dir, summary_json, loaded_at)
    events(run_id, n, recv_mono_ns, recv_wall_ns, source, channel, ticker,
           sid, seq, epoch, marker, raw)        -- one row per WS frame/marker
    order_rounds(run_id, round, ts_ms, ticker, client_order_id, order_id,
                 sign_us, place_us, cancel_us, place_status, cancel_status)
    book_levels(run_id, ticker, valid, book_seq, side, price_e4, size_fp)

Example queries:
    -- event rate per channel
    SELECT channel, COUNT(*) FROM events WHERE run_id=? GROUP BY channel;
    -- inter-arrival gaps (ns) for one market's deltas
    SELECT recv_mono_ns - LAG(recv_mono_ns) OVER (ORDER BY recv_mono_ns)
    FROM events WHERE run_id=? AND ticker=? AND channel='orderbook_delta';
    -- order latency distribution
    SELECT place_us FROM order_rounds WHERE place_status IN (200,201);
"""
import base64
import json
import os
import sqlite3
import sys
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    dir         TEXT,
    summary_json TEXT,
    loaded_at   INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    run_id      TEXT NOT NULL,
    n           INTEGER NOT NULL,          -- line number within the capture
    recv_mono_ns INTEGER,
    recv_wall_ns INTEGER,
    source      TEXT,
    channel     TEXT,
    ticker      TEXT,
    sid         INTEGER,
    seq         INTEGER,
    epoch       INTEGER,
    marker      TEXT,                       -- gap/loss/epoch_change/... or NULL
    raw         TEXT                        -- exact original payload (JSON text)
);
CREATE TABLE IF NOT EXISTS order_rounds (
    run_id      TEXT NOT NULL,
    round       INTEGER,
    ts_ms       INTEGER,
    ticker      TEXT,
    client_order_id TEXT,
    order_id    TEXT,
    sign_us     INTEGER,
    place_us    INTEGER,
    cancel_us   INTEGER,
    place_status INTEGER,
    cancel_status INTEGER
);
CREATE TABLE IF NOT EXISTS book_levels (
    run_id      TEXT NOT NULL,
    ticker      TEXT,
    valid       INTEGER,
    book_seq    INTEGER,
    side        TEXT,                       -- 'yes' | 'no'
    price_e4    INTEGER,                    -- dollars x 10^4
    size_fp     INTEGER                     -- contracts x 10^2
);
CREATE INDEX IF NOT EXISTS ix_events_run     ON events(run_id);
CREATE INDEX IF NOT EXISTS ix_events_ticker  ON events(run_id, ticker);
CREATE INDEX IF NOT EXISTS ix_events_chan    ON events(run_id, channel);
CREATE INDEX IF NOT EXISTS ix_events_wall    ON events(recv_wall_ns);
CREATE INDEX IF NOT EXISTS ix_orders_run     ON order_rounds(run_id);
"""


def iter_ndjson(path):
    """Yield (lineno, dict) for parseable lines; skip corrupt/truncated ones."""
    with open(path, "rb") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield n, json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # truncated final line / corrupt line: skip, like RawLogReader


def load_capture(cur, run_id, path):
    rows = []
    for n, o in iter_ndjson(path):
        raw = o.get("raw")
        if raw is None and "raw_b64" in o:
            try:
                raw = base64.b64decode(o["raw_b64"]).decode("utf-8", "replace")
            except Exception:
                raw = None
        rows.append((
            run_id, n,
            o.get("recv_mono_ns"), o.get("recv_wall_ns"),
            o.get("source"), o.get("channel"), o.get("source_ticker"),
            o.get("sid"), o.get("source_sequence"), o.get("stream_epoch"),
            o.get("marker"), raw,
        ))
    cur.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_orders(cur, run_id, path):
    rows = []
    for _, o in iter_ndjson(path):
        if o.get("type") != "order_round":
            continue
        rows.append((
            run_id, o.get("round"), o.get("ts_ms"), o.get("ticker"),
            o.get("client_order_id"), o.get("order_id"),
            o.get("sign_us"), o.get("place_us"), o.get("cancel_us"),
            o.get("place_status"), o.get("cancel_status"),
        ))
    cur.executemany(
        "INSERT INTO order_rounds VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_books(cur, run_id, path):
    rows = []
    for _, o in iter_ndjson(path):
        if o.get("type") != "book_final" or not o.get("present"):
            continue
        for side in ("yes", "no"):
            for level in o.get(side, []):
                if isinstance(level, list) and len(level) == 2:
                    rows.append((run_id, o.get("ticker"),
                                 1 if o.get("valid") else 0, o.get("seq"),
                                 side, level[0], level[1]))
    cur.executemany("INSERT INTO book_levels VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_run(con, target):
    """Load one run dir (or bare capture file). Replaces prior rows for run_id."""
    if os.path.isdir(target):
        run_id = os.path.basename(os.path.normpath(target))
        d = target
        capture = os.path.join(d, "capture.ndjson")
        orders = os.path.join(d, "orders.ndjson")
        books = os.path.join(d, "books.ndjson")
        summary = os.path.join(d, "summary.json")
    else:
        run_id = os.path.basename(target)
        d = os.path.dirname(target) or "."
        capture, orders, books, summary = target, None, None, None

    cur = con.cursor()
    for table in ("events", "order_rounds", "book_levels"):
        cur.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    cur.execute("DELETE FROM runs WHERE run_id=?", (run_id,))

    n_ev = n_or = n_bl = 0
    if capture and os.path.exists(capture):
        n_ev = load_capture(cur, run_id, capture)
        # rotated capture files (capture.ndjson.1, .2 ...) from RawLogWriter
        i = 1
        while os.path.exists(capture + "." + str(i)):
            n_ev += load_capture(cur, run_id, capture + "." + str(i))
            i += 1
    if orders and os.path.exists(orders):
        n_or = load_orders(cur, run_id, orders)
    if books and os.path.exists(books):
        n_bl = load_books(cur, run_id, books)

    summary_json = None
    if summary and os.path.exists(summary):
        with open(summary) as f:
            summary_json = f.read()
    cur.execute("INSERT INTO runs VALUES (?,?,?,?)",
                (run_id, os.path.abspath(d), summary_json, int(time.time())))
    con.commit()
    print(f"loaded run {run_id}: {n_ev} events, {n_or} order rounds, {n_bl} book levels")
    return run_id


def report(con, run_id):
    cur = con.cursor()
    print(f"--- {run_id} ---")
    for chan, cnt in cur.execute(
            "SELECT CASE WHEN marker IS NOT NULL THEN '(marker:'||marker||')' "
            "ELSE channel END, COUNT(*) "
            "FROM events WHERE run_id=? GROUP BY 1 ORDER BY 2 DESC", (run_id,)):
        print(f"  {chan:32s} {cnt}")
    row = cur.execute(
        "SELECT COUNT(*), MIN(recv_wall_ns), MAX(recv_wall_ns) "
        "FROM events WHERE run_id=? AND marker IS NULL", (run_id,)).fetchone()
    if row and row[0] and row[1] and row[2] and row[2] > row[1]:
        span_s = (row[2] - row[1]) / 1e9
        print(f"  {row[0]} frames over {span_s:.1f}s = {row[0]/span_s:.1f} msg/s")
    orders = cur.execute(
        "SELECT COUNT(*), AVG(place_us), AVG(cancel_us) FROM order_rounds "
        "WHERE run_id=? AND place_status IN (200,201)", (run_id,)).fetchone()
    if orders and orders[0]:
        print(f"  orders: {orders[0]} placed, avg place {orders[1]/1000.0:.1f}ms, "
              f"avg cancel {(orders[2] or 0)/1000.0:.1f}ms")


def main():
    args = sys.argv[1:]
    db = "market_data.db"
    targets = []
    i = 0
    while i < len(args):
        if args[i] == "--db":
            i += 1
            db = args[i]
        else:
            targets.append(args[i])
        i += 1
    if not targets:
        print(__doc__)
        return 2
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for t in targets:
        if not os.path.exists(t):
            print(f"skip (not found): {t}")
            continue
        run_id = load_run(con, t)
        report(con, run_id)
    print(f"database: {os.path.abspath(db)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
