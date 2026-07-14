#!/usr/bin/env python3
"""Acceptance proofs for tools/ingest.py (change-only staging ingester).

Demonstrates, with synthetic fixtures and no network:
  1. CHANGE-ONLY: a market repeating identical ticks 100x then changing once
     writes exactly 1 initial row + 1 change row.
  2. HEARTBEAT: a market with zero changes across 3 hours gets exactly 3
     is_snapshot=true heartbeat rows (hour starts), book state carried.
  3. RESTART: kill mid-file (partial line), restart with a fresh Ingester —
     row counts reconcile against the raw log, no gaps, no duplicates.
  4. CLASS POLICY: Class B categories get NO orderbooks_l1 rows; trades are
     recorded for every market regardless of class.

stdlib + duckdb only.
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402

HOUR_US = 3_600_000_000
T0 = 1_783_300_000_000_000  # 2026-07-06-ish, mid-hour, epoch micros

FAILS = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name,
                        (" - " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def tick(mt, ts_us, bid, ask, bid_q=100, ask_q=100):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000,
           "yes_bid_dollars": "%.4f" % bid, "yes_ask_dollars": "%.4f" % ask,
           "yes_bid_size_fp": "%d.00" % bid_q, "yes_ask_size_fp": "%d.00" % ask_q,
           "price_dollars": "%.4f" % ((bid + ask) / 2), "volume_fp": "10.00",
           "open_interest_fp": "5.00"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "ticker", "msg": msg})})


def trade(mt, ts_us, tid):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000, "trade_id": tid,
           "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
           "count_fp": "1.00", "taker_side": "yes"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "trade", "msg": msg})})


def make_warehouse(tmp):
    """Fixture classification dim: KXBTC=Crypto (Class A), KXACME=Companies (Class B)."""
    import duckdb
    wh = os.path.join(tmp, "warehouse")
    out = os.path.join(wh, "catalog", "series_classified")
    os.makedirs(out)
    nd = os.path.join(tmp, "cls.ndjson")
    with open(nd, "w") as f:
        for st, cat, sub, grp, kl in [
                ("KXBTC", "Crypto", "BTC", "BTC", "A"),
                ("KXACME", "Companies", "_none", "ACME", "B")]:
            f.write(json.dumps({"series_ticker": st, "category": cat,
                                "subcategory": sub, "group": grp,
                                "record_class": kl}) + "\n")
    duckdb.connect().execute(
        "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
        "TO '%s' (FORMAT PARQUET)" % (nd, os.path.join(out, "part-00000.parquet")))
    return wh


def new_ingester(tmp, wh, db="staging.duckdb"):
    import duckdb
    return ingest.Ingester(duckdb.connect(os.path.join(tmp, db)), wh)


def q(ing, sql):
    return ing.con.execute(sql).fetchall()


def main():
    tmp = tempfile.mkdtemp(prefix="test_ingest_")
    try:
        wh = make_warehouse(tmp)
        mt_a = "KXBTC-25DEC31-B50"      # Class A, the quiet/heartbeat market
        mt_b = "KXBTC-25DEC31-B60"      # Class A, the clock-advancing market
        mt_c = "KXACME-25DEC31-YES"     # Class B

        # ---- 1. change-only: 100 identical ticks + 1 change -------------------
        lines = [tick(mt_a, T0 + i * 1_000_000, 0.40, 0.42) for i in range(100)]
        lines.append(tick(mt_a, T0 + 101 * 1_000_000, 0.41, 0.42))  # the change
        cap1 = os.path.join(tmp, "cap1.ndjson")
        open(cap1, "w").write("\n".join(lines) + "\n")
        ing = new_ingester(tmp, wh)
        ing.process_file(cap1)
        rows = q(ing, "SELECT is_snapshot FROM orderbooks_l1 WHERE market_ticker='%s' "
                      "ORDER BY ts_utc" % mt_a)
        check("change-only writes exactly 2 rows (1 snapshot + 1 change)",
              len(rows) == 2 and rows[0][0] is True and rows[1][0] is False,
              "got %s" % rows)

        # ---- 2. heartbeat: 3 quiet hours -> exactly 3 snapshot rows -----------
        # mt_b ticks advance the data clock through 3 hour boundaries; mt_a is silent.
        h1 = ((T0 // HOUR_US) + 1) * HOUR_US
        lines2 = []
        for k in range(3):
            lines2.append(tick(mt_b, h1 + k * HOUR_US + 60_000_000, 0.10 + k / 100, 0.12))
        cap2 = os.path.join(tmp, "cap2.ndjson")
        open(cap2, "w").write("\n".join(lines2) + "\n")
        ing.process_file(cap2)
        hb = q(ing, "SELECT ts_utc, yes_bid_e4, price_e4 FROM orderbooks_l1 "
                    "WHERE market_ticker='%s' AND is_snapshot AND ts_utc > %d "
                    "ORDER BY ts_utc" % (mt_a, T0 + 102 * 1_000_000))
        check("3 quiet hours -> exactly 3 heartbeat rows", len(hb) == 3, hb)
        check("heartbeats land on hour starts",
              all(ts % HOUR_US == 0 for ts, _, _ in hb), hb)
        check("heartbeats carry last book state (bid=0.41 -> 4100 E4)",
              all(bid == 4100 for _, bid, _ in hb), hb)
        check("scheduled heartbeats have NULL price (book fields only)",
              all(p is None for _, _, p in hb), hb)
        total_a = q(ing, "SELECT count(*) FROM orderbooks_l1 WHERE market_ticker='%s'"
                    % mt_a)[0][0]
        check("quiet market total rows = 2 + 3 heartbeats", total_a == 5, total_a)

        # ---- 3. restart mid-file: no gaps, no duplicates ----------------------
        full = "\n".join(lines + lines2) + "\n"
        cap3 = os.path.join(tmp, "cap3.ndjson")
        cut = len(full) // 2
        cut = full.rfind("\n", 0, cut) + 40          # mid-line: a partial tail
        open(cap3, "w").write(full[:cut])
        ing2 = new_ingester(tmp, wh, "restart.duckdb")
        ing2.process_file(cap3)
        ing2.con.close()                              # "kill" the ingester
        open(cap3, "a").write(full[cut:])             # capture keeps appending
        ing3 = new_ingester(tmp, wh, "restart.duckdb")  # restart: state rebuilds
        ing3.process_file(cap3)
        ref = q(ing, "SELECT count(*) FROM orderbooks_l1")[0][0]
        got = q(ing3, "SELECT count(*) FROM orderbooks_l1")[0][0]
        dup = q(ing3, "SELECT count(*) FROM (SELECT DISTINCT * FROM orderbooks_l1)")[0][0]
        check("restart row count reconciles with single-pass reference",
              got == ref, "got=%d ref=%d" % (got, ref))
        check("restart produces no duplicate rows", dup == got,
              "distinct=%d total=%d" % (dup, got))

        # ---- 4. class policy + trades-for-all ---------------------------------
        cap4 = os.path.join(tmp, "cap4.ndjson")
        open(cap4, "w").write("\n".join(
            [tick(mt_c, T0, 0.50, 0.52), trade(mt_c, T0 + 1_000_000, "t1"),
             trade(mt_a, T0 + 2_000_000, "t2")]) + "\n")
        ing.process_file(cap4)
        b_l1 = q(ing, "SELECT count(*) FROM orderbooks_l1 WHERE market_ticker='%s'"
                 % mt_c)[0][0]
        check("Class B market gets NO orderbooks_l1 rows", b_l1 == 0, b_l1)
        n_tr = q(ing, "SELECT count(*) FROM trades")[0][0]
        check("trades recorded for BOTH classes", n_tr == 2, n_tr)
        stats = q(ing, "SELECT sum(ticks_seen) FROM ingest_stats")[0][0]
        check("ingest_stats counted raw ticks", stats and stats >= 107, stats)

        # ---- 5. corrupt timestamps are dropped, never staged, never crash -----
        cap5 = os.path.join(tmp, "cap5.ndjson")
        bad_msg = {"market_ticker": mt_a, "ts_ms": 178332621142632600880,
                   "ts": 178332623231, "yes_bid_dollars": "0.50",
                   "yes_ask_dollars": "0.52"}
        open(cap5, "w").write(json.dumps(
            {"recv_wall_ns": 17833262114263260088000,
             "raw": json.dumps({"type": "ticker", "msg": bad_msg})}) + "\n")
        before = q(ing, "SELECT count(*) FROM orderbooks_l1")[0][0]
        ing.process_file(cap5)
        after = q(ing, "SELECT count(*) FROM orderbooks_l1")[0][0]
        check("corrupt-timestamp frame dropped (no crash, no row)",
              after == before and ing.bad_ts == 1,
              "rows %d->%d bad_ts=%d" % (before, after, ing.bad_ts))

        # ---- 5b. malformed NON-STR ticker (2026-07-14 firehose_23 incident) ----
        # A numeric market_ticker must be rejected BY TYPE (never coerced into a
        # valid identity), must NOT crash ingest ("-" not in <int> raised
        # TypeError and crash-looped the daemon), the checkpoint must still
        # advance through the COMPLETE file, and the VALID record right after the
        # malformed one must still ingest.
        cap_bad = os.path.join(tmp, "cap_badticker.ndjson")
        Tbt = T0 + 500 * 1_000_000
        mt_good = "KXBTC-25DEC31-B99"            # fresh Class-A ticker, never seen
        numeric_line = tick(1784030893, Tbt, 0.40, 0.42)          # int ticker
        good_line = tick(mt_good, Tbt + 1_000_000, 0.30, 0.33)    # valid, right after
        open(cap_bad, "w").write(numeric_line + "\n" + good_line + "\n")
        bt_before = ing.bad_ticker
        crashed = None
        try:
            ing.process_file(cap_bad)
        except Exception as e:      # non-str ticker previously raised TypeError here
            crashed = e
        check("malformed non-str ticker does NOT crash ingest",
              crashed is None, repr(crashed))
        check("numeric ticker rejected and counted (bad_ticker +1)",
              ing.bad_ticker == bt_before + 1,
              "bad_ticker %d->%d" % (bt_before, ing.bad_ticker))
        bad_facts = (q(ing, "SELECT count(*) FROM orderbooks_l1 "
                            "WHERE market_ticker='1784030893'")[0][0]
                     + q(ing, "SELECT count(*) FROM trades "
                            "WHERE market_ticker='1784030893'")[0][0]
                     + q(ing, "SELECT count(*) FROM orderbooks_full "
                            "WHERE market_ticker='1784030893'")[0][0])
        check("no malformed fact written for the numeric ticker",
              bad_facts == 0, bad_facts)
        good_rows = q(ing, "SELECT count(*) FROM orderbooks_l1 "
                           "WHERE market_ticker='%s'" % mt_good)[0][0]
        check("valid record immediately after the malformed one is ingested",
              good_rows == 1, good_rows)
        fsize = os.path.getsize(cap_bad)
        ckpt = q(ing, "SELECT byte_offset FROM checkpoint WHERE file='%s'" % cap_bad)
        check("checkpoint advances through the COMPLETE malformed-ticker file",
              bool(ckpt) and ckpt[0][0] == fsize,
              "ckpt=%s size=%d" % (ckpt, fsize))

        # ---- 7. writer connect survives a reader-held lock (2026-07-07) -------
        import duckdb as _duckdb
        lockdb = os.path.join(tmp, "lock.duckdb")
        _duckdb.connect(lockdb).close()  # create file
        holder = _duckdb.connect(lockdb, read_only=True)  # reader holds it
        import threading
        release = threading.Timer(1.2, holder.close)
        release.start()
        t0 = time.time()
        con_r = ingest.connect_with_retry(_duckdb, lockdb, attempts=20, sleep_s=0.2)
        waited = time.time() - t0
        con_r.close()
        release.cancel()
        check("connect_with_retry waits out a reader lock instead of crashing",
              waited >= 0.5, "waited %.2fs" % waited)
        try:
            ingest.connect_with_retry(_duckdb, os.path.join(tmp, "nodir", "x.duckdb"),
                                      attempts=2, sleep_s=0.05)
            check("connect_with_retry raises after exhausting attempts", False)
        except Exception:
            check("connect_with_retry raises after exhausting attempts", True)

        check("normalize_ts_us: s/ms/ns accepted, absurd/garbage rejected",
              ingest.normalize_ts_us(1783325907) == 1783325907_000_000 and
              ingest.normalize_ts_us(1783325907199) == 1783325907199_000 and
              ingest.normalize_ts_us(1783325907199000999) == 1783325907199000 and
              ingest.normalize_ts_us(178332623231) is None and
              ingest.normalize_ts_us("garbage") is None)

        # ---- 6. orderbook snapshot levels (real Kalshi field names) -----------
        snap_msg = {"market_ticker": mt_a, "ts_ms": T0 // 1000,
                    "yes_dollars_fp": [["0.0100", "310.00"], ["0.7100", "2.50"]],
                    "no_dollars_fp": [["0.2000", "10.00"]]}
        cap6 = os.path.join(tmp, "cap6.ndjson")
        open(cap6, "w").write(json.dumps(
            {"recv_wall_ns": T0 * 1000,
             "raw": json.dumps({"type": "orderbook_snapshot", "msg": snap_msg})}) + "\n")
        ing.process_file(cap6)
        yl, nl = q(ing, "SELECT yes_levels, no_levels FROM orderbooks_full "
                        "WHERE msg_type='snapshot'")[0]
        check("snapshot levels parsed from yes/no_dollars_fp into E4 pairs",
              json.loads(yl) == [[100, 3100000], [7100, 25000]] and
              json.loads(nl) == [[2000, 100000]], (yl, nl))

        # ---- 8. W5: frame-level sid/seq -> ws_sid/ws_seq (orderbooks_full) ----
        # Verified live (W3.3): orderbook_snapshot/orderbook_delta frames carry
        # top-level `sid` and `seq` ints — {type, sid, seq, msg:{...}}.
        def book_frame(typ, ts_us, msg, sid=None, seq=None):
            frame = {"type": typ, "msg": msg}
            if sid is not None:
                frame["sid"] = sid
            if seq is not None:
                frame["seq"] = seq
            return json.dumps({"recv_wall_ns": ts_us * 1000,
                               "raw": json.dumps(frame)})

        t8 = T0 + 10_000_000
        snap8 = {"market_ticker": mt_a, "ts_ms": t8 // 1000,
                 "yes_dollars_fp": [["0.0100", "310.00"]], "no_dollars_fp": []}
        del8a = {"market_ticker": mt_a, "ts_ms": (t8 + 1_000_000) // 1000,
                 "side": "yes", "price_dollars": "0.0100", "delta_fp": "5.00"}
        del8b = {"market_ticker": mt_a, "ts_ms": (t8 + 2_000_000) // 1000,
                 "side": "no", "price_dollars": "0.2000", "delta_fp": "-1.00"}
        cap7 = os.path.join(tmp, "cap7.ndjson")
        open(cap7, "w").write("\n".join([
            book_frame("orderbook_snapshot", t8, snap8, sid=7, seq=41),
            book_frame("orderbook_delta", t8 + 1_000_000, del8a, sid=7, seq=42),
            book_frame("orderbook_delta", t8 + 2_000_000, del8b),  # no sid/seq
        ]) + "\n")
        ing.process_file(cap7)
        rows8 = q(ing, "SELECT msg_type, ws_sid, ws_seq FROM orderbooks_full "
                       "WHERE ts_utc >= %d ORDER BY ts_utc" % t8)
        check("snapshot/delta frames carry top-level sid/seq into ws_sid/ws_seq",
              len(rows8) == 3 and rows8[0] == ("snapshot", 7, 41) and
              rows8[1] == ("delta", 7, 42), rows8)
        check("frames without sid/seq land with NULL ws_sid/ws_seq",
              len(rows8) == 3 and rows8[2] == ("delta", None, None), rows8)

        # ---- 9. W5 migration: pre-W5 13-col staging gains the columns ---------
        import duckdb as _dd
        OLD_FULL_DDL = """CREATE TABLE orderbooks_full (
          ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
          category TEXT, subcategory TEXT, "group" TEXT,
          msg_type TEXT, side TEXT, price_e4 INTEGER, delta_e4 BIGINT,
          yes_levels TEXT, no_levels TEXT)"""
        old_db = os.path.join(tmp, "old.duckdb")
        old_con = _dd.connect(old_db)
        old_con.execute(OLD_FULL_DDL)
        old_con.execute(
            "INSERT INTO orderbooks_full VALUES (%d, '%s', 'KXBTC', "
            "'KXBTC-25DEC31', 'Crypto', 'BTC', 'BTC', 'snapshot', NULL, "
            "NULL, NULL, '[]', '[]')" % (T0, mt_a))
        ing_m = ingest.Ingester(old_con, wh)   # re-open over the old schema
        cols_m = {r[1] for r in old_con.execute(
            "PRAGMA table_info('orderbooks_full')").fetchall()}
        check("migration: re-open over pre-W5 table ALTERs ws_sid/ws_seq in",
              {"ws_sid", "ws_seq"} <= cols_m, cols_m)
        legacy = old_con.execute("SELECT ws_sid, ws_seq FROM orderbooks_full "
                                 "WHERE ts_utc = %d" % T0).fetchone()
        check("migration: legacy rows read back NULL ws_sid/ws_seq",
              legacy == (None, None), legacy)
        ing_m.process_file(cap7)
        got_m = old_con.execute(
            "SELECT count(*) FILTER (ws_seq IS NOT NULL), count(*) "
            "FROM orderbooks_full WHERE ts_utc >= %d" % t8).fetchone()
        check("migration: inserts work on the migrated table (explicit cols)",
              got_m == (2, 3), got_m)
        old_con.close()
        con_i = _dd.connect(old_db)
        ingest.Ingester(con_i, wh)              # idempotent second re-open
        n_cols = len(con_i.execute("PRAGMA table_info('orderbooks_full')").fetchall())
        check("migration is idempotent (no duplicate columns, no crash)",
              n_cols == 19, n_cols)   # 13 original + ws_sid/ws_seq + 4 ladder
        con_i.close()

        # ---- 10. W-TL1 timestamp ladder ---------------------------------------
        # 10a. all four columns land from the raw envelope; exchange_ts_us is
        #      ts_ms-authoritative (ms*1000); ts_utc == exchange when present.
        t10 = T0 + 50 * HOUR_US            # fresh hour, far from earlier data
        mt_l = "KXBTC-25DEC31-B70"
        env = {"recv_mono_ns": 1_849_853_442_697_375,        # arbitrary epoch
               "recv_wall_ns": (t10 + 250_000) * 1000,       # arrived 250ms late
               "raw": json.dumps({"type": "ticker", "msg": {
                   "market_ticker": mt_l, "ts": 1_783_607_448,   # decoy legacy
                   "ts_ms": t10 // 1000,
                   "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200",
                   "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00"}})}
        cap10 = os.path.join(tmp, "cap10.ndjson")
        open(cap10, "w").write(json.dumps(env) + "\n")
        ing.process_file(cap10)
        r = q(ing, "SELECT ts_utc, exchange_ts_us, recv_wall_ns, recv_mono_ns, "
                   "local_recv_ts_us FROM orderbooks_l1 WHERE market_ticker='%s'"
                   % mt_l)[-1]
        check("ladder: exchange_ts_us = ts_ms*1000 (authoritative over ts)",
              r[1] == (t10 // 1000) * 1000, r)
        check("ladder: recv_wall_ns/recv_mono_ns keep raw envelope values",
              r[2] == (t10 + 250_000) * 1000 and r[3] == 1_849_853_442_697_375, r)
        check("ladder: local_recv_ts_us = recv_wall_ns // 1000",
              r[4] == t10 + 250_000, r)
        check("ladder: ts_utc = exchange_ts_us when present (legacy COALESCE)",
              r[0] == r[1], r)

        # 10b. legacy `ts` fallbacks: JSON number = seconds; string = ISO-8601.
        def raw_line(mt, msg_extra, wall_ns=None, mono_ns=None):
            e = {"raw": json.dumps({"type": "ticker", "msg": dict(
                {"market_ticker": mt, "yes_bid_dollars": "0.1000",
                 "yes_ask_dollars": "0.1200", "yes_bid_size_fp": "10.00",
                 "yes_ask_size_fp": "10.00"}, **msg_extra)})}
            if wall_ns is not None:
                e["recv_wall_ns"] = wall_ns
            if mono_ns is not None:
                e["recv_mono_ns"] = mono_ns
            return json.dumps(e)

        sec = (t10 + HOUR_US) // 1_000_000
        iso_us = t10 + HOUR_US + 123_456
        import datetime as _dt
        iso = _dt.datetime.fromtimestamp(
            iso_us / 1e6, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cap11 = os.path.join(tmp, "cap11.ndjson")
        open(cap11, "w").write("\n".join([
            raw_line(mt_l, {"ts": sec}, wall_ns=(sec * 1_000_000 + 99) * 1000),
            raw_line(mt_l, {"ts": iso, "yes_bid_dollars": "0.2000"}),
        ]) + "\n")
        ing.process_file(cap11)
        rows11 = q(ing, "SELECT exchange_ts_us FROM orderbooks_l1 "
                        "WHERE market_ticker='%s' AND ts_utc > %d "
                        "AND exchange_ts_us IS NOT NULL ORDER BY ts_utc"
                   % (mt_l, t10))   # heartbeat rows (NULL ladder) excluded
        check("ladder: legacy numeric ts parsed as SECONDS",
              rows11[0][0] == sec * 1_000_000, rows11)
        check("ladder: legacy string ts parsed as ISO-8601",
              rows11[-1][0] == iso_us, rows11)

        # 10c. no exchange ts at all -> ts_utc falls back to local_recv_ts_us.
        no_exch_us = t10 + 2 * HOUR_US + 42
        cap12 = os.path.join(tmp, "cap12.ndjson")
        open(cap12, "w").write(
            raw_line(mt_l, {"yes_bid_dollars": "0.3000"},
                     wall_ns=no_exch_us * 1000, mono_ns=7) + "\n")
        ing.process_file(cap12)
        r12 = q(ing, "SELECT ts_utc, exchange_ts_us, local_recv_ts_us "
                     "FROM orderbooks_l1 WHERE market_ticker='%s' "
                     "ORDER BY ts_utc DESC LIMIT 1" % mt_l)[0]
        check("ladder: ts_utc falls back to local_recv_ts_us when no exchange ts",
              r12 == (no_exch_us, None, no_exch_us), r12)

        # 10d. scheduled heartbeats: ts_utc = hour start (production contract),
        #      all four ladder columns NULL (no fabricated timestamps).
        hb10 = q(ing, "SELECT ts_utc, exchange_ts_us, recv_wall_ns, recv_mono_ns, "
                      "local_recv_ts_us FROM orderbooks_l1 "
                      "WHERE market_ticker='%s' AND is_snapshot AND price_e4 IS NULL "
                      "AND ts_utc > %d" % (mt_a, T0))
        check("heartbeats: ts_utc stays hour-start AND ladder is all NULL",
              len(hb10) > 0 and all(ts % HOUR_US == 0 and e is None and w is None
                                    and m is None and l is None
                                    for ts, e, w, m, l in hb10),
              hb10[:3])

        # 10e. trades + orderbooks_full carry the ladder too.
        cap13 = os.path.join(tmp, "cap13.ndjson")
        tr_env = {"recv_wall_ns": (t10 + 500) * 1000, "recv_mono_ns": 111,
                  "raw": json.dumps({"type": "trade", "msg": {
                      "market_ticker": mt_l, "ts_ms": t10 // 1000,
                      "trade_id": "tl1", "yes_price_dollars": "0.5000",
                      "no_price_dollars": "0.5000", "count_fp": "1.00",
                      "taker_side": "yes"}})}
        full_env = {"recv_wall_ns": (t10 + 600) * 1000, "recv_mono_ns": 222,
                    "raw": json.dumps({"type": "orderbook_delta", "sid": 3, "seq": 9,
                                       "msg": {"market_ticker": mt_l,
                                               "ts_ms": t10 // 1000, "side": "yes",
                                               "price_dollars": "0.0100",
                                               "delta_fp": "5.00"}})}
        open(cap13, "w").write(json.dumps(tr_env) + "\n" + json.dumps(full_env) + "\n")
        ing.process_file(cap13)
        rt = q(ing, "SELECT exchange_ts_us, recv_wall_ns, recv_mono_ns, "
                    "local_recv_ts_us FROM trades WHERE trade_id='tl1'")[0]
        rf = q(ing, "SELECT exchange_ts_us, recv_wall_ns, recv_mono_ns, "
                    "local_recv_ts_us, ws_seq FROM orderbooks_full "
                    "WHERE market_ticker='%s' AND ws_sid=3" % mt_l)[0]
        check("ladder on trades", rt == ((t10 // 1000) * 1000, (t10 + 500) * 1000,
                                         111, t10 + 500), rt)
        check("ladder on orderbooks_full (coexists with ws_sid/ws_seq)",
              rf == ((t10 // 1000) * 1000, (t10 + 600) * 1000, 222, t10 + 600, 9), rf)

        # 10f. migration: a pre-TL1 staging DB (all three tables, old widths)
        #      gains the four columns; legacy rows read back NULL.
        PRE_TL1_DDL = """
        CREATE TABLE orderbooks_l1 (
          ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
          category TEXT, subcategory TEXT, "group" TEXT, record_class TEXT,
          yes_bid_e4 INTEGER, yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER,
          yes_ask_qty_e4 BIGINT, price_e4 INTEGER, volume_e4 BIGINT,
          open_interest_e4 BIGINT, is_snapshot BOOLEAN);
        CREATE TABLE trades (
          ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
          category TEXT, subcategory TEXT, "group" TEXT,
          trade_id TEXT, yes_price_e4 INTEGER, no_price_e4 INTEGER,
          count_e4 BIGINT, taker_side TEXT);
        CREATE TABLE orderbooks_full (
          ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
          category TEXT, subcategory TEXT, "group" TEXT,
          msg_type TEXT, side TEXT, price_e4 INTEGER, delta_e4 BIGINT,
          yes_levels TEXT, no_levels TEXT, ws_sid BIGINT, ws_seq BIGINT);"""
        pre_db = os.path.join(tmp, "pre_tl1.duckdb")
        pre_con = _dd.connect(pre_db)
        pre_con.execute(PRE_TL1_DDL)
        pre_con.execute("INSERT INTO trades VALUES (%d, '%s', 'KXBTC', "
                        "'KXBTC-25DEC31', 'Crypto', 'BTC', 'BTC', 'old1', "
                        "5000, 5000, 10000, 'yes')" % (T0, mt_a))
        ingest.Ingester(pre_con, wh)
        for tbl in ("orderbooks_l1", "trades", "orderbooks_full"):
            cols_t = {c[1] for c in pre_con.execute(
                "PRAGMA table_info('%s')" % tbl).fetchall()}
            check("TL1 migration: %s gains all four ladder columns" % tbl,
                  {"exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
                   "local_recv_ts_us"} <= cols_t, cols_t)
        old_row = pre_con.execute(
            "SELECT exchange_ts_us, recv_wall_ns, recv_mono_ns, local_recv_ts_us "
            "FROM trades WHERE trade_id='old1'").fetchone()
        check("TL1 migration: legacy rows read back all-NULL ladder",
              old_row == (None, None, None, None), old_row)
        pre_con.close()

        # ---- 11. RFQ FAST-PATH (PIPE-W05 addendum 7, 2026-07-13) --------------
        # rfq_<HH>.ndjson* / rfq_receipts_<HH>.ndjson* are checkpoint-only:
        # raw bytes untouched, no JSON parse, no facts; the byte checkpoint
        # advances through the final COMPLETE newline via the existing atomic
        # transaction. Non-RFQ families are provably unchanged.
        import hashlib
        import warehouse_common as wc

        def sha(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        def ck(ing, path):
            r = ing.con.execute("SELECT byte_offset FROM checkpoint WHERE file=?",
                                [os.path.abspath(path)]).fetchone()
            return r[0] if r else None

        # dispatch regex classifies families correctly, rejects generic raw.
        m = ingest.RFQ_FASTPATH_RE.match
        check("RFQ regex: matches rfq_/rfq_receipts_ families + shards only",
              all(m(b) for b in ("rfq_00.ndjson", "rfq_23.ndjson.1",
                                 "rfq_receipts_09.ndjson", "rfq_receipts_09.ndjson.2"))
              and not any(m(b) for b in ("firehose_00.ndjson", "l2_00.ndjson",
                                         "trades_00.ndjson", "rfq_metrics.ndjson",
                                         "rfq_segments.ndjson", "capture.ndjson")))

        rfq_dir = os.path.join(tmp, "rfqraw", "date=2026-07-12")
        os.makedirs(rfq_dir)
        # Content is deliberately TICKER-SHAPED for the quiet Class A market:
        # if the fast-path parsed it, orderbooks_l1 would gain rows. It must not.
        rfq_lines = [tick(mt_a, T0 + 300 * 1_000_000 + i * 1_000_000, 0.44, 0.46)
                     for i in range(5)]
        rfq0 = os.path.join(rfq_dir, "rfq_00.ndjson")
        open(rfq0, "w").write("\n".join(rfq_lines) + "\n")
        sha0, size0 = sha(rfq0), os.path.getsize(rfq0)

        ing_rfq = new_ingester(tmp, wh, "rfq.duckdb")
        counts0 = ing_rfq.process_file(rfq0)
        # (a) raw bytes byte-for-byte unchanged
        check("RFQ (a): raw SHA-256 + size byte-identical before vs after",
              sha(rfq0) == sha0 and os.path.getsize(rfq0) == size0)
        # fast-path yields ZERO facts despite parseable ticker content
        check("RFQ: ticker-shaped rfq_ file materializes ZERO facts (no parse)",
              counts0 == (0, 0, 0)
              and q(ing_rfq, "SELECT count(*) FROM orderbooks_l1")[0][0] == 0
              and q(ing_rfq, "SELECT count(*) FROM trades")[0][0] == 0
              and q(ing_rfq, "SELECT count(*) FROM orderbooks_full")[0][0] == 0,
              counts0)
        # closed file: checkpoint reaches exact size -> unblocks the seal
        check("RFQ: closed rfq_ file checkpoint == size (seal-ready)",
              ck(ing_rfq, rfq0) == size0, ck(ing_rfq, rfq0))

        # (b) rotated shards each carry their own independent byte checkpoint
        r1 = os.path.join(rfq_dir, "rfq_00.ndjson.1")
        r2 = os.path.join(rfq_dir, "rfq_00.ndjson.2")
        open(r1, "w").write("\n".join(rfq_lines[:3]) + "\n")
        open(r2, "w").write("\n".join(rfq_lines[:2]) + "\n")
        ing_rfq.process_file(r1)
        ing_rfq.process_file(r2)
        check("RFQ (b): rotated shards .1/.2 get independent size checkpoints",
              ck(ing_rfq, r1) == os.path.getsize(r1)
              and ck(ing_rfq, r2) == os.path.getsize(r2)
              and os.path.getsize(r1) != os.path.getsize(r2),
              (ck(ing_rfq, r1), os.path.getsize(r1),
               ck(ing_rfq, r2), os.path.getsize(r2)))

        # (c) partial trailing record stays UN-checkpointed; a later append that
        #     completes the line advances correctly on the next pass.
        rp = os.path.join(rfq_dir, "rfq_01.ndjson")
        complete = ("\n".join(rfq_lines) + "\n")
        complete_bytes = len(complete.encode())
        open(rp, "w").write(complete + '{"partial":true, no newline yet')
        ing_rfq.process_file(rp)
        check("RFQ (c): partial trailing record left UN-checkpointed",
              ck(ing_rfq, rp) == complete_bytes, (ck(ing_rfq, rp), complete_bytes))
        open(rp, "a").write("}\n")            # capture completes the line
        ing_rfq.process_file(rp)
        check("RFQ (c): completing the line advances checkpoint next pass",
              ck(ing_rfq, rp) == os.path.getsize(rp),
              (ck(ing_rfq, rp), os.path.getsize(rp)))

        # (d) rfq_receipts_<HH> uses the same fast path
        rr = os.path.join(rfq_dir, "rfq_receipts_00.ndjson")
        open(rr, "w").write("\n".join(rfq_lines[:4]) + "\n")
        sha_rr = sha(rr)
        counts_rr = ing_rfq.process_file(rr)
        check("RFQ (d): rfq_receipts_<HH> fast path (0 facts, ckpt==size, bytes intact)",
              counts_rr == (0, 0, 0) and ck(ing_rfq, rr) == os.path.getsize(rr)
              and sha(rr) == sha_rr, (counts_rr, ck(ing_rfq, rr)))

        # (e) mixed run: a non-RFQ (firehose) file yields IDENTICAL facts +
        #     checkpoint whether or not RFQ files share the run (regression guard).
        fh = os.path.join(rfq_dir, "firehose_09.ndjson")
        open(fh, "w").write("\n".join([
            tick(mt_a, T0 + 400 * 1_000_000, 0.30, 0.32),
            tick(mt_a, T0 + 401 * 1_000_000, 0.31, 0.32),
            trade(mt_a, T0 + 402 * 1_000_000, "rt1")]) + "\n")
        fh_cols = ("SELECT ts_utc, market_ticker, yes_bid_e4, is_snapshot "
                   "FROM orderbooks_l1 ORDER BY ts_utc, yes_bid_e4")
        ing_ref = new_ingester(tmp, wh, "ref.duckdb")   # firehose ALONE
        ing_ref.process_file(fh)
        ref = (q(ing_ref, fh_cols),
               q(ing_ref, "SELECT count(*) FROM trades")[0][0], ck(ing_ref, fh))
        ing_mix = new_ingester(tmp, wh, "mix.duckdb")   # RFQ files + same firehose
        for p in (rfq0, r1, rr, fh, rp):
            ing_mix.process_file(p)
        mix = (q(ing_mix, fh_cols),
               q(ing_mix, "SELECT count(*) FROM trades")[0][0], ck(ing_mix, fh))
        check("RFQ (e): non-RFQ firehose facts + checkpoint identical with/without RFQ",
              mix == ref and ref[2] == os.path.getsize(fh), (mix, ref))

        # (f) a file already fully-parse-ingested to offset N (old behavior) is
        #     not re-processed; checkpoint is monotonic (no double count).
        old = os.path.join(rfq_dir, "rfq_02.ndjson")
        open(old, "w").write("\n".join(rfq_lines) + "\n")
        N = os.path.getsize(old)
        ing_mono = new_ingester(tmp, wh, "mono.duckdb")
        ing_mono.con.execute(                            # emulate old full-parse ckpt
            "INSERT INTO checkpoint VALUES (?,?, epoch_us(now()))",
            [os.path.abspath(old), N])
        before = (q(ing_mono, "SELECT count(*) FROM orderbooks_l1")[0][0],
                  q(ing_mono, "SELECT count(*) FROM trades")[0][0])
        ing_mono.process_file(old)
        after = (q(ing_mono, "SELECT count(*) FROM orderbooks_l1")[0][0],
                 q(ing_mono, "SELECT count(*) FROM trades")[0][0])
        check("RFQ (f): file checkpointed to N is not re-processed (monotonic, no dup)",
              ck(ing_mono, old) == N and before == after == (0, 0),
              (ck(ing_mono, old), before, after))
        open(old, "a").write("\n".join(rfq_lines) + "\n")   # later append
        ing_mono.process_file(old)
        check("RFQ (f): later append advances checkpoint forward from N to new size",
              ck(ing_mono, old) == os.path.getsize(old) and os.path.getsize(old) > N,
              (ck(ing_mono, old), os.path.getsize(old)))

        # seal inventory is pure disk enumeration -> unchanged by the fast path,
        # and still lists the RFQ raw files (they stay sealable).
        seal_root = os.path.join(tmp, "sealraw")
        d12 = os.path.join(seal_root, "date=2026-07-12")
        d13 = os.path.join(seal_root, "date=2026-07-13")
        os.makedirs(d12); os.makedirs(d13)
        for name in ("firehose_23.ndjson", "rfq_23.ndjson",
                     "rfq_receipts_23.ndjson", "l2_23.ndjson"):
            open(os.path.join(d12, name), "w").write("x\n")
        for h in ("00", "01"):                           # next-day cross-hours req.
            open(os.path.join(d13, "firehose_%s.ndjson" % h), "w").write("x\n")
            open(os.path.join(d13, "rfq_%s.ndjson" % h), "w").write("x\n")
        inv_before = wc.seal_raw_files(seal_root, "2026-07-12")
        ing_seal = new_ingester(tmp, wh, "seal.duckdb")
        for p in (os.path.join(d12, "rfq_23.ndjson"),
                  os.path.join(d12, "rfq_receipts_23.ndjson"),
                  os.path.join(d13, "rfq_00.ndjson"),
                  os.path.join(d13, "rfq_01.ndjson")):
            ing_seal.process_file(p)
        inv_after = wc.seal_raw_files(seal_root, "2026-07-12")
        bn = {os.path.basename(p) for p in inv_after}
        check("RFQ: seal inventory unchanged by fast-path + still lists RFQ files",
              inv_before == inv_after
              and {"rfq_23.ndjson", "rfq_receipts_23.ndjson"} <= bn,
              (len(inv_before), len(inv_after)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print("FAILURES: %s" % ", ".join(FAILS))
        print("TEST FAIL")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
