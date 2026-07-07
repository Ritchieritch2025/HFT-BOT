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
                                start=yd.isoformat(), end=yd.isoformat()
                                ).count("*").fetchone()[0]
        check("load(trades, Sports/MLB, yesterday) returns the archived slice",
              n_arch == stg_tr_yd, n_arch)
        # regression (2026-07-07): archive csv read must not let the sniffer
        # narrow taker_side to BOOLEAN — values must survive as 'yes'/'no'
        sides = {r[0] for r in warehouse.load(
            "trades", start=yd.isoformat(), end=yd.isoformat(),
            columns=["taker_side"]).fetchall()}
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
