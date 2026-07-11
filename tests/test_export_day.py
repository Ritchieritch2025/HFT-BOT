#!/usr/bin/env python3
"""Acceptance proofs for tools/export_day.py + tools/warehouse.py load().

Demonstrates, with synthetic fixtures and no network:
  1. EXPORT: a completed day leaves staging as self-describing partitions
     (<table>__<C>__<S>__<date>.parquet / .csv.gz) under
     facts/<table>/category=<C>/subcategory=<S>/date=<date>/.
  2. MANIFEST: manifest.csv rows == archive files; md5 + row counts verify.
  3. PRUNE: staging retains today + 1 prior day only.
  4. WRITE-ONCE: re-export refuses without --force; --force replaces.
  5. LOAD ROUTING: load() reads the archived day from the archive and today
     from staging, with no double-count of the overlap day; ffill works.

stdlib + duckdb only.
"""
import csv
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name,
                        (" - " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def us(day, hour=12):
    return int(datetime.datetime(day.year, day.month, day.day, hour,
               tzinfo=datetime.timezone.utc).timestamp() * 1_000_000)


def tick(mt, ts_us, bid):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000,
           "yes_bid_dollars": "%.4f" % bid, "yes_ask_dollars": "%.4f" % (bid + 0.02),
           "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00",
           "price_dollars": "%.4f" % (bid + 0.01), "volume_fp": "1.00",
           "open_interest_fp": "1.00"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "ticker", "msg": msg})})


def trade(mt, ts_us, tid):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000, "trade_id": tid,
           "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
           "count_fp": "2.00", "taker_side": "yes"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "trade", "msg": msg})})


def book(mt, ts_us, typ, sid=None, seq=None, **msg_extra):
    """orderbook_snapshot/delta capture line; sid/seq are frame-level (W5)."""
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000}
    msg.update(msg_extra)
    frame = {"type": typ, "msg": msg}
    if sid is not None:
        frame["sid"] = sid
    if seq is not None:
        frame["seq"] = seq
    return json.dumps({"recv_wall_ns": ts_us * 1000, "raw": json.dumps(frame)})


def _write_late_capture(tmp, yd):
    """Trades for yesterday that arrive in staging AFTER the midnight export."""
    path = os.path.join(tmp, "late.ndjson")
    with open(path, "w") as f:
        f.write(trade("KXMLB-25JUL05-BOS", us(yd, 18), "late1") + "\n")
        f.write(trade("KXMLB-25JUL05-BOS", us(yd, 19), "late2") + "\n")
    return path


def main():
    import duckdb
    tmp = tempfile.mkdtemp(prefix="test_export_")
    try:
        wh = os.path.join(tmp, "warehouse")
        staging = os.path.join(wh, "staging.duckdb")
        archive = os.path.join(wh, "facts")
        env = dict(os.environ, WAREHOUSE_ROOT=wh, STAGING_DB=staging,
                   ARCHIVE_ROOT=archive, RAW_ROOT=os.path.join(tmp, "raw"))
        os.environ.update({"WAREHOUSE_ROOT": wh, "STAGING_DB": staging,
                           "ARCHIVE_ROOT": archive,
                           "RAW_ROOT": os.path.join(tmp, "raw")})

        # classification fixture: Crypto/BTC (A) + Sports/MLB (A)
        cls_dir = os.path.join(wh, "catalog", "series_classified")
        os.makedirs(cls_dir)
        nd = os.path.join(tmp, "cls.ndjson")
        with open(nd, "w") as f:
            f.write(json.dumps({"series_ticker": "KXBTC", "category": "Crypto",
                                "subcategory": "BTC", "group": "BTC",
                                "record_class": "A"}) + "\n")
            f.write(json.dumps({"series_ticker": "KXMLB", "category": "Sports",
                                "subcategory": "MLB", "group": "MLB",
                                "record_class": "A"}) + "\n")
        duckdb.connect().execute(
            "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
            "TO '%s' (FORMAT PARQUET)" % (nd, os.path.join(cls_dir, "part-00000.parquet")))

        today = datetime.datetime.now(datetime.timezone.utc).date()
        yd = today - datetime.timedelta(days=1)
        d3 = today - datetime.timedelta(days=3)

        cap = os.path.join(tmp, "cap.ndjson")
        with open(cap, "w") as f:
            f.write(trade("KXMLB-25JUL04-NYY", us(d3), "old1") + "\n")   # prune bait
            f.write(tick("KXBTC-25DEC31-B50", us(yd), 0.40) + "\n")
            f.write(tick("KXBTC-25DEC31-B50", us(yd) + 60_000_000, 0.41) + "\n")
            f.write(trade("KXMLB-25JUL05-BOS", us(yd) + 1_000_000, "y1") + "\n")
            f.write(trade("KXMLB-25JUL05-BOS", us(yd) + 2_000_000, "y2") + "\n")
            f.write(tick("KXBTC-25DEC31-B50", us(today, 10), 0.45) + "\n")
            f.write(trade("KXMLB-25JUL06-CHC", us(today, 10), "t1") + "\n")
            # W5: full-book frames for yesterday, with and without sid/seq
            f.write(book("KXBTC-25DEC31-B50", us(yd) + 3_000_000,
                         "orderbook_snapshot", sid=9, seq=101,
                         yes_dollars_fp=[["0.4000", "10.00"]],
                         no_dollars_fp=[]) + "\n")
            f.write(book("KXBTC-25DEC31-B50", us(yd) + 4_000_000,
                         "orderbook_delta", sid=9, seq=102, side="yes",
                         price_dollars="0.4000", delta_fp="1.00") + "\n")
            f.write(book("KXBTC-25DEC31-B50", us(yd) + 5_000_000,
                         "orderbook_delta", side="yes",
                         price_dollars="0.4000", delta_fp="-1.00") + "\n")
        con = duckdb.connect(staging)
        ingest.Ingester(con, wh).process_file(cap)
        day_lo, day_hi = us(yd, 0) - 43_200_000_000 * 0, None
        yd_lo = int(datetime.datetime(yd.year, yd.month, yd.day,
                    tzinfo=datetime.timezone.utc).timestamp() * 1_000_000)
        yd_hi = yd_lo + 86_400_000_000
        stg_l1_yd = con.execute("SELECT count(*) FROM orderbooks_l1 WHERE ts_utc >= ? "
                                "AND ts_utc < ?", [yd_lo, yd_hi]).fetchone()[0]
        stg_tr_yd = con.execute("SELECT count(*) FROM trades WHERE ts_utc >= ? "
                                "AND ts_utc < ?", [yd_lo, yd_hi]).fetchone()[0]
        old_tr = con.execute("SELECT count(*) FROM trades WHERE ts_utc < ?",
                             [yd_lo]).fetchone()[0]
        con.close()
        check("fixture staged (yd L1=%d yd trades=%d old trades=%d)"
              % (stg_l1_yd, stg_tr_yd, old_tr),
              stg_l1_yd >= 2 and stg_tr_yd == 2 and old_tr >= 1)

        # ---- 1+2. export yesterday, verify layout + manifest + md5 ------------
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                            "--date", yd.isoformat()], env=env,
                           capture_output=True, text=True)
        check("export exits 0 with EXPORT PASS", r.returncode == 0 and
              "EXPORT PASS" in r.stdout, r.stdout[-400:] + r.stderr[-400:])
        l1_file = os.path.join(archive, "orderbooks_l1", "category=Crypto",
                               "subcategory=BTC", "date=%s" % yd,
                               "orderbooks_l1__Crypto__BTC__%s.parquet" % yd)
        tr_file = os.path.join(archive, "trades", "category=Sports",
                               "subcategory=MLB", "date=%s" % yd,
                               "trades__Sports__MLB__%s.csv.gz" % yd)
        check("self-describing partition files exist",
              os.path.exists(l1_file) and os.path.exists(tr_file),
              [l1_file, tr_file])
        arch_files = [os.path.join(dp, f) for dp, _, fs in os.walk(archive) for f in fs]
        manifest = list(csv.DictReader(open(os.path.join(wh, "manifest.csv"))))
        check("manifest rows == archive files",
              len(manifest) == len(arch_files),
              "manifest=%d files=%d" % (len(manifest), len(arch_files)))
        m_tr = next(m for m in manifest if m["table"] == "trades")
        md5 = hashlib.md5(open(os.path.join(ROOT, m_tr["file_path"]) if not
                          os.path.isabs(m_tr["file_path"]) else m_tr["file_path"],
                          "rb").read()).hexdigest()
        check("manifest md5 spot-check", md5 == m_tr["file_md5"],
              "%s != %s" % (md5, m_tr["file_md5"]))
        n_tr_file = duckdb.connect().execute(
            "SELECT count(*) FROM read_csv('%s', header=true)" % tr_file).fetchone()[0]
        check("archived trade rows == staged yesterday trades",
              n_tr_file == stg_tr_yd == int(m_tr["row_count"]),
              "file=%d staged=%d manifest=%s" % (n_tr_file, stg_tr_yd, m_tr["row_count"]))
        check("compression report written",
              os.path.exists(os.path.join(wh, "compression_report.csv")))
        # W5: exporter's SELECT * must pick up ws_sid/ws_seq in the parquet
        fu_file = os.path.join(archive, "orderbooks_full", "category=Crypto",
                               "subcategory=BTC", "date=%s" % yd,
                               "orderbooks_full__Crypto__BTC__%s.parquet" % yd)
        check("orderbooks_full archive partition exists", os.path.exists(fu_file),
              fu_file)
        fu_rows = duckdb.connect().execute(
            "SELECT msg_type, ws_sid, ws_seq FROM read_parquet('%s') "
            "ORDER BY ts_utc" % fu_file).fetchall()
        check("archived parquet carries ws_sid/ws_seq (SELECT * pickup)",
              fu_rows == [("snapshot", 9, 101), ("delta", 9, 102),
                          ("delta", None, None)], fu_rows)

        # ---- 3. prune: older-than-yesterday rows are gone ---------------------
        con = duckdb.connect(staging, read_only=True)
        left_old = con.execute("SELECT count(*) FROM trades WHERE ts_utc < ?",
                               [yd_lo]).fetchone()[0]
        left_yd = con.execute("SELECT count(*) FROM trades WHERE ts_utc >= ? AND "
                              "ts_utc < ?", [yd_lo, yd_hi]).fetchone()[0]
        con.close()
        check("prune removed pre-yesterday rows, retained today + 1 prior day",
              left_old == 0 and left_yd == stg_tr_yd,
              "old=%d yd=%d" % (left_old, left_yd))

        # ---- 4. write-once + --force ------------------------------------------
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                             "--date", yd.isoformat()], env=env,
                            capture_output=True, text=True)
        check("re-export refused (write-once)", r2.returncode != 0 and
              "already archived" in r2.stdout + r2.stderr, r2.stdout + r2.stderr)
        r3 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                             "--date", yd.isoformat(), "--force"], env=env,
                            capture_output=True, text=True)
        check("--force re-export passes", r3.returncode == 0, r3.stdout + r3.stderr)

        # ---- 5. load() routing -------------------------------------------------
        import warehouse
        n_arch = warehouse.load("trades", category="Sports", subcategory="MLB",
                                start=yd.isoformat(), end=yd.isoformat(),
                                archive_only=False
                                ).count("*").fetchone()[0]
        check("load(trades, Sports/MLB, yesterday) returns the archived slice",
              n_arch == stg_tr_yd, n_arch)
        # regression (2026-07-07): archive csv read must not let the sniffer
        # narrow taker_side to BOOLEAN — values must survive as 'yes'/'no'
        sides = {r[0] for r in warehouse.load(
            "trades", start=yd.isoformat(), end=yd.isoformat(),
            columns=["taker_side"], archive_only=False).fetchall()}
        check("archived taker_side stays 'yes'/'no' strings (no BOOLEAN sniff)",
              sides and sides <= {"yes", "no"}, sides)
        n_today = warehouse.load("trades", start=today.isoformat(),
                                 end=today.isoformat()).count("*").fetchone()[0]
        check("load(trades, today) returns live staging data", n_today == 1, n_today)
        n_all = warehouse.load("trades").count("*").fetchone()[0]
        check("no double-count across archive/staging overlap",
              n_all == stg_tr_yd + 1, n_all)
        n_ffill = warehouse.load("orderbooks_l1", category="Crypto",
                                 ffill=True).count("*").fetchone()[0]
        check("ffill=True LOCF query runs", n_ffill >= 2, n_ffill)

        # ---- 5b. W5: old (pre-seq, 13-col) + new archives union via load() ----
        # Simulate a pre-W5 archive day: a parquet WITHOUT ws_sid/ws_seq.
        d2 = today - datetime.timedelta(days=2)
        old_dir = os.path.join(archive, "orderbooks_full", "category=Crypto",
                               "subcategory=BTC", "date=%s" % d2)
        os.makedirs(old_dir)
        old_pq = os.path.join(old_dir,
                              "orderbooks_full__Crypto__BTC__%s.parquet" % d2)
        duckdb.connect().execute(
            "COPY (SELECT %d AS ts_utc, 'KXBTC-25DEC31-B50' AS market_ticker, "
            "'KXBTC' AS series_ticker, 'KXBTC-25DEC31' AS event_ticker, "
            "'Crypto' AS category, 'BTC' AS subcategory, 'BTC' AS \"group\", "
            "'snapshot' AS msg_type, CAST(NULL AS VARCHAR) AS side, "
            "CAST(NULL AS INTEGER) AS price_e4, CAST(NULL AS BIGINT) AS delta_e4, "
            "'[]' AS yes_levels, '[]' AS no_levels) TO '%s' (FORMAT PARQUET)"
            % (us(d2), old_pq))
        fu = warehouse.load("orderbooks_full", start=d2.isoformat(),
                            end=yd.isoformat(),
                            columns=["ts_utc", "msg_type", "ws_sid", "ws_seq"],
                            archive_only=False
                            ).fetchall()
        check("old 13-col + new archive union via load() (missing cols -> NULL)",
              len(fu) == 4 and (fu[0][2], fu[0][3]) == (None, None) and
              (fu[1][2], fu[1][3]) == (9, 101) and
              (fu[2][2], fu[2][3]) == (9, 102) and
              (fu[3][2], fu[3][3]) == (None, None), fu)

        # ---- 6. second-pass sweep semantics (2026-07-07 export/ingest race) ---
        # late rows land in staging AFTER the midnight export; a --force
        # re-export must pick them up (archive grows, never shrinks)
        # release warehouse.load()'s cached read-only attach — DuckDB's
        # single-writer rule blocks the subprocess writer while any process
        # holds the file, even read-only
        def _release_warehouse_con():
            if warehouse._CON is not None:
                warehouse._CON.close()
            warehouse._CON = None
            warehouse._ATTACHED.clear()
            warehouse._VALIDATED_ARCHIVES.clear()
            warehouse._VALIDATED_RAW.clear()

        _release_warehouse_con()
        late_cap = _write_late_capture(tmp, yd)
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ingest.py"),
                        "--warehouse", wh, "--staging", staging, late_cap],
                       env=env, capture_output=True, text=True, check=True)
        n_late = int(subprocess.run(
            [sys.executable, "-c",
             "import duckdb;print(duckdb.connect('%s',read_only=True).execute("
             "'SELECT count(*) FROM trades WHERE ts_utc >= %d AND ts_utc < %d'"
             ").fetchone()[0])" % (staging, yd_lo, yd_hi)],
            capture_output=True, text=True, check=True).stdout.strip())
        r4 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                             "--date", yd.isoformat(), "--force", "--no-prune"],
                            env=env, capture_output=True, text=True)
        check("sweep force re-export passes", r4.returncode == 0, r4.stdout + r4.stderr)
        n_after = warehouse.load("trades", start=yd.isoformat(),
                                 end=yd.isoformat(),
                                 archive_only=False).count("*").fetchone()[0]
        check("late-ingested rows reached the archive (sweep semantics)",
              n_after == n_late and n_after > stg_tr_yd,
              "archived=%d staged=%d before=%d" % (n_after, n_late, stg_tr_yd))
        # shrink guard: delete yesterday from staging, then --force must REFUSE
        _release_warehouse_con()
        subprocess.run(
            [sys.executable, "-c",
             "import duckdb;c=duckdb.connect('%s');c.execute("
             "'DELETE FROM trades WHERE ts_utc >= %d AND ts_utc < %d');c.close()"
             % (staging, yd_lo, yd_hi)],
            capture_output=True, text=True, check=True)
        r5 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                             "--date", yd.isoformat(), "--force", "--no-prune"],
                            env=env, capture_output=True, text=True)
        check("shrink guard refuses --force when staging < certified archive",
              r5.returncode == 3 and "SHRINK" in r5.stderr, r5.stdout + r5.stderr)
        n_intact = warehouse.load("trades", start=yd.isoformat(),
                                  end=yd.isoformat(),
                                  archive_only=False).count("*").fetchone()[0]
        check("archive intact after refused shrink", n_intact == n_after, n_intact)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for k in ("WAREHOUSE_ROOT", "STAGING_DB", "ARCHIVE_ROOT", "RAW_ROOT"):
            os.environ.pop(k, None)

    if FAILS:
        print("FAILURES: %s" % ", ".join(FAILS))
        print("TEST FAIL")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
