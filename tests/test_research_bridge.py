#!/usr/bin/env python3
"""PIPE-W05 Phase A acceptance proofs: research release publisher + Mac CLI.

Fixture-based, no network, no credentials: a tmp directory stands in for the
S3 research/ prefix (both tools take local roots by design). Demonstrates:
  1. SEAL GATE: publishing an unsealed day is refused; nothing lands.
  2. PUBLISH: a sealed day publishes byte-verified objects with MANIFEST.json
     frozen fields (tier, TL1 status, channel labels incl. L2
     NOT_RESEARCH_EXPOSABLE_PHASE_A, rfq default EXCLUDED, versioning caveat,
     day-filtered capture-gap evidence) and is idempotent on re-run.
  3. FAIL-CLOSED: a tampered archive file aborts the publish with nothing
     uploaded (manifest absent = not exposed).
  4. RFQ SWITCH: --include-rfq publishes the sibling "-rfq" release with
     seal-verified rfq raw only (never firehose/l2); default stays OFF.
  5. CLI: inventory / fetch / verify / view against the published root —
     verify PASS writes .VERIFIED.json and builds the warehouse-shaped
     symlink view; a corrupted cached object fails verify (exit 2, marker
     removed, .FAILED.json, view drops the day); a torn release (no
     MANIFEST) is never exposed; PRE-TL1 day is labeled PRE-TL1.
  6. HYGIENE: both tools set an explicit DuckDB memory_limit on connect.

stdlib + duckdb only.  Run: python3 tests/test_research_bridge.py [scratch]
"""
import csv
import datetime
import glob
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import warehouse_common as wc  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("ok" if cond else "FAIL", name,
                         (" — " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, env, expect_rc=0):
    r = subprocess.run([sys.executable] + args, cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if expect_rc is not None and r.returncode != expect_rc:
        print("    cmd: %s\n    rc=%d\n    stdout: %s\n    stderr: %s"
              % (" ".join(args), r.returncode, r.stdout[-800:],
                 r.stderr[-800:]))
    return r


LADDER = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
          "local_recv_ts_us")


def make_day(tmp, date, tl1=True, with_rfq_raw=True):
    """Synthetic sealed warehouse day: L1 parquet + trades csv.gz + manifest
    rows + v2 seal (+ sealed rfq raw & decoy firehose)."""
    import duckdb
    facts = os.path.join(tmp, "warehouse", "facts")
    l1_dir = os.path.join(facts, "orderbooks_l1", "category=Sports",
                          "subcategory=Baseball", "date=%s" % date)
    tr_dir = os.path.join(facts, "trades", "category=Sports",
                          "subcategory=Baseball", "date=%s" % date)
    os.makedirs(l1_dir, exist_ok=True)
    os.makedirs(tr_dir, exist_ok=True)
    l1 = os.path.join(l1_dir, "orderbooks_l1__Sports__Baseball__%s.parquet"
                      % date)
    ladder_sql = (", 1719999999000000 AS exchange_ts_us, "
                  "1719999999000000111 AS recv_wall_ns, "
                  "42 AS recv_mono_ns, 1719999999000000 AS local_recv_ts_us"
                  if tl1 else "")
    conn = duckdb.connect()
    conn.execute("SET memory_limit='1GB'")
    conn.execute(
        "COPY (SELECT 'T-%s' AS market_ticker, 1719999990000000 AS ts_utc, "
        "50 AS yes_bid_e4, 52 AS yes_ask_e4, 7 AS ws_sid, 99 AS ws_seq%s) "
        "TO '%s' (FORMAT PARQUET)" % (date, ladder_sql, l1))
    conn.close()
    tr = os.path.join(tr_dir, "trades__Sports__Baseball__%s.csv.gz" % date)
    cols = ["trade_id", "market_ticker", "ts_utc", "count"] + \
        (list(LADDER) if tl1 else [])
    with gzip.open(tr, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow(["t1", "T-%s" % date, 1719999991000000, 5] +
                   ([1, 2, 3, 4] if tl1 else []))

    # warehouse manifest rows for the date
    mpath = os.path.join(tmp, "warehouse", "manifest.csv")
    new = not os.path.exists(mpath)
    with open(mpath, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "table", "category", "subcategory",
                        "row_count", "file_path", "file_md5", "created_ts"])
        for t, p in (("orderbooks_l1", l1), ("trades", tr)):
            w.writerow([date, t, "Sports", "Baseball", 1, p, "x", "now"])
    manifest_sha, _ = wc.manifest_date_sha256(mpath, date)

    # raw: sealed rfq + receipts + decoy firehose (must never publish)
    raw_files = []
    rawd = os.path.join(tmp, "raw", "date=%s" % date)
    os.makedirs(rawd, exist_ok=True)
    names = (["rfq_13.ndjson", "rfq_13.ndjson.1", "rfq_receipts_13.ndjson"]
             if with_rfq_raw else []) + ["firehose_13.ndjson"]
    for fn in names:
        p = os.path.join(rawd, fn)
        with open(p, "w") as f:
            f.write('{"chan":"%s","d":"%s"}\n' % (fn, date))
        raw_files.append({"file": "date=%s/%s" % (date, fn),
                          "sha256": sha256_file(p),
                          "size": os.stat(p).st_size, "checkpoint": None,
                          "inode": 1, "ctime_ns": 1, "mtime_ns": 1})

    stats = []
    for t, p in (("orderbooks_l1", l1), ("trades", tr)):
        stats.append({"file": os.path.relpath(p, facts), "table": t,
                      "size": os.stat(p).st_size, "sha256": sha256_file(p),
                      "md5": "x", "inode": 1, "ctime_ns": 1, "mtime_ns": 1})
    seals = os.path.join(tmp, "warehouse", "seals")
    os.makedirs(seals, exist_ok=True)
    seal = {"date": date, "status": "SEALED", "version": 2,
            "method": "full_v2", "sealed_at": "2026-07-12T04:00:00Z",
            "archive_files": len(stats), "archive_rows": 2,
            "archive_file_stats": stats, "raw_files": raw_files,
            "manifest_date_sha256": manifest_sha,
            "capture_quality_status": "UNASSESSED_PENDING_PIPE_W03",
            "raw_retention_requirement": "LOCAL_OR_VAULT_VERIFIED_RECEIPT",
            "receipt_cross_day_hours": 2, "unverified": [],
            "go_no_go_eligible": True}
    with open(os.path.join(seals, "date=%s.json" % date), "w") as f:
        json.dump(seal, f, indent=1)
    return l1, tr


def main():
    scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir()
    tmp = tempfile.mkdtemp(prefix="w05_bridge_", dir=scratch)
    day_us = wc.day_start_us("2026-07-11")
    try:
        env = dict(os.environ)
        env.update({
            "WAREHOUSE_ROOT": os.path.join(tmp, "warehouse"),
            "ARCHIVE_ROOT": os.path.join(tmp, "warehouse", "facts"),
            "RAW_ROOT": os.path.join(tmp, "raw"),
            "RESEARCH_INCLUDE_RFQ": "0",
        })
        # shared warehouse extras
        os.makedirs(os.path.join(tmp, "warehouse", "catalog", "events"),
                    exist_ok=True)
        with open(os.path.join(tmp, "warehouse", "catalog", "events",
                               "part-00000.parquet"), "wb") as f:
            f.write(b"PARQUET-STANDIN")
        os.makedirs(os.path.join(tmp, "warehouse", "dim", "snapshots",
                                 "date=2026-07-11"), exist_ok=True)
        with open(os.path.join(tmp, "warehouse", "dim", "snapshots",
                               "date=2026-07-11", "markets.csv"), "w") as f:
            f.write("ticker\nT-1\n")
        quality = os.path.join(tmp, "quality")
        os.makedirs(quality, exist_ok=True)
        with open(os.path.join(quality, "capture_gaps.csv"), "w",
                  newline="") as f:
            w = csv.writer(f)
            w.writerow(["start_us", "end_us"])
            w.writerow([day_us + 1000, day_us + 90_000_000])      # in-day
            w.writerow([day_us - 7_200_000_000, day_us - 3_600_000_000])
        with open(os.path.join(quality, "l2_gaps_2026-07-11.json"), "w") as f:
            json.dump({"no_l2_files": True, "seq_gap_events": 0,
                       "seq_missed_total": 0, "sids_total": 0,
                       "sids_with_seq_gaps": 0, "lines": 0}, f)

        l1_file, _tr = make_day(tmp, "2026-07-11", tl1=True)
        make_day(tmp, "2026-07-05", tl1=False, with_rfq_raw=False)
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest, exist_ok=True)
        cache = os.path.join(tmp, "cache")
        pub = ["tools/research_release.py", "publish", "--dest", dest,
               "--quality-dir", quality]
        cli = ["tools/research_data.py", "--root", dest, "--cache", cache]

        print("== 1. seal gate")
        r = run(pub + ["--date", "2026-07-04", "--no-rfq"], env,
                expect_rc=None)
        check("unsealed day refused", r.returncode != 0)
        check("nothing published for unsealed day",
              not glob.glob(os.path.join(dest, "releases", "2026-07-04*")))

        print("== 2. publish sealed day (rfq default OFF)")
        r = run(pub + ["--date", "2026-07-11"], env)
        check("publish rc=0", r.returncode == 0, r.stderr[-300:])
        rels = glob.glob(os.path.join(dest, "releases", "2026-07-11__seal-*"))
        check("one release dir", len(rels) == 1, rels)
        rid = os.path.basename(rels[0]) if rels else ""
        check("no -rfq suffix by default", not rid.endswith("-rfq"), rid)
        mpath = os.path.join(dest, "releases", rid, "MANIFEST.json")
        check("MANIFEST.json present", os.path.isfile(mpath))
        man = json.load(open(mpath)) if os.path.isfile(mpath) else {}
        check("tier SEALED_CONFIRMATION",
              man.get("evidence_tier") == "SEALED_CONFIRMATION")
        check("tl1_status TL1", man.get("tl1_status") == "TL1", man.get("tl1_status"))
        ch = man.get("channels", {})
        check("L2 NOT_RESEARCH_EXPOSABLE_PHASE_A",
              ch.get("orderbooks_l2", {}).get("status")
              == "NOT_RESEARCH_EXPOSABLE_PHASE_A")
        check("rfq default EXCLUDED",
              ch.get("rfq", {}).get("status")
              == "EXCLUDED_PENDING_OPERATOR_COST_ACK", ch.get("rfq"))
        check("L1 conflation label",
              ch.get("orderbooks_l1", {}).get("completeness")
              == "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS")
        check("gap intervals day-filtered == 1",
              ch.get("orderbooks_l1", {}).get("gap_intervals_for_date") == 1)
        check("versioning caveat recorded",
              man.get("s3_versioning", {}).get("bucket_versioning")
              == "UNKNOWN_TO_WRITER")
        keys = {o["key"] for o in man.get("objects", [])}
        check("no raw published by default",
              not any(k.startswith("raw_rfq/") for k in keys))
        check("firehose never published",
              not any("firehose" in k for k in keys))
        check("seal+manifest+quality+catalog+dim frozen",
              {"seal/date=2026-07-11.json", "warehouse_manifest/manifest.csv",
               "quality/capture_gaps_2026-07-11.csv", "quality/l2_gaps.json",
               "catalog/events/part-00000.parquet",
               "dim/snapshots/date=2026-07-11/markets.csv"} <= keys, keys)
        on_disk = {os.path.relpath(p, rels[0]).replace(os.sep, "/")
                   for p in glob.glob(os.path.join(rels[0], "**", "*"),
                                      recursive=True) if os.path.isfile(p)}
        check("manifest lists exactly the uploaded objects",
              keys == on_disk - {"MANIFEST.json"})
        r = run(pub + ["--date", "2026-07-11"], env)
        check("republish idempotent no-op", r.returncode == 0
              and "already published" in r.stdout, r.stdout[-200:])

        print("== 3. fail-closed on tamper")
        good = open(l1_file, "rb").read()
        with open(l1_file, "ab") as f:
            f.write(b"CORRUPT")
        shutil.rmtree(os.path.join(dest, "releases2"), ignore_errors=True)
        dest2 = os.path.join(tmp, "dest2")
        r = run(["tools/research_release.py", "publish", "--dest", dest2,
                 "--quality-dir", quality, "--date", "2026-07-11",
                 "--no-rfq"], env, expect_rc=None)
        check("tampered archive aborts", r.returncode != 0)
        check("nothing exposed after abort",
              not glob.glob(os.path.join(dest2, "releases", "*",
                                         "MANIFEST.json")))
        with open(l1_file, "wb") as f:
            f.write(good)

        print("== 4. rfq switch -> sibling -rfq release")
        r = run(pub + ["--date", "2026-07-11", "--include-rfq"], env)
        check("rfq publish rc=0", r.returncode == 0, r.stderr[-300:])
        rfq_rel = glob.glob(os.path.join(dest, "releases", "*-rfq"))
        check("-rfq sibling release created", len(rfq_rel) == 1)
        if rfq_rel:
            man2 = json.load(open(os.path.join(rfq_rel[0], "MANIFEST.json")))
            k2 = {o["key"] for o in man2["objects"]}
            check("rfq objects included (data+receipts only)",
                  {"raw_rfq/rfq_13.ndjson", "raw_rfq/rfq_13.ndjson.1",
                   "raw_rfq/rfq_receipts_13.ndjson"}
                  <= k2 and not any("firehose" in k for k in k2))
            check("rfq status INCLUDED_SEALED_RAW",
                  man2["channels"]["rfq"]["status"] == "INCLUDED_SEALED_RAW")

        print("== 5. CLI: inventory / fetch / verify / view")
        r = run(cli + ["inventory"], env)
        check("inventory rc=0", r.returncode == 0, r.stderr[-300:])
        check("inventory shows EXPOSED + cost + cache lines",
              "EXPOSED" in r.stdout and "monthly cost estimate" in r.stdout
              and "local cache" in r.stdout, r.stdout[-400:])
        r = run(cli + ["fetch", "--release", rid], env)
        check("fetch+verify PASS", r.returncode == 0, r.stdout[-400:])
        marker = os.path.join(cache, "releases", rid, ".VERIFIED.json")
        check(".VERIFIED.json written", os.path.isfile(marker))
        check("verify prints channel truth",
              "NOT_RESEARCH_EXPOSABLE_PHASE_A" in r.stdout
              and "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS" in r.stdout)
        view = os.path.join(cache, "view")
        check("view has warehouse shape",
              os.path.isfile(os.path.join(
                  view, "facts", "orderbooks_l1", "category=Sports",
                  "subcategory=Baseball", "date=2026-07-11",
                  "orderbooks_l1__Sports__Baseball__2026-07-11.parquet"))
              and os.path.isfile(os.path.join(view, "seals",
                                              "date=2026-07-11.json")))
        # rfq variant with raw
        rid2 = os.path.basename(rfq_rel[0]) if rfq_rel else ""
        r = run(cli + ["fetch", "--release", rid2, "--with-rfq"], env)
        check("rfq fetch+verify PASS", r.returncode == 0, r.stdout[-400:])
        check("rfq VERIFIED_SEALED_RAW",
              "VERIFIED_SEALED_RAW" in r.stdout, r.stdout[-300:])
        check("view exposes rfq raw under raw/date=D",
              os.path.isfile(os.path.join(view, "raw", "date=2026-07-11",
                                          "rfq_13.ndjson")))

        print("== 6. corruption -> not exposed")
        cached_l1 = os.path.join(cache, "releases", rid, "facts",
                                 "orderbooks_l1", "category=Sports",
                                 "subcategory=Baseball", "date=2026-07-11",
                                 "orderbooks_l1__Sports__Baseball__"
                                 "2026-07-11.parquet")
        with open(cached_l1, "ab") as f:
            f.write(b"X")
        r = run(cli + ["verify", "--release", rid], env, expect_rc=2)
        check("verify exit 2 on corruption", r.returncode == 2)
        check("marker removed", not os.path.isfile(marker))
        check(".FAILED.json written", os.path.isfile(
            os.path.join(cache, "releases", rid, ".FAILED.json")))
        check("loud warning", "NOT exposed" in r.stderr, r.stderr[-300:])
        prov = json.load(open(os.path.join(view, ".view_provenance.json")))
        check("view keeps date via still-verified -rfq sibling",
              prov["verified_releases"].get("2026-07-11") == rid2, prov)

        print("== 7. torn release never exposed; PRE-TL1 labeling")
        torn = os.path.join(dest, "releases", "2026-07-05__seal-aaaaaaaaaaaa")
        os.makedirs(os.path.join(torn, "facts"), exist_ok=True)
        with open(os.path.join(torn, "facts", "x.parquet"), "wb") as f:
            f.write(b"torn")
        r = run(cli + ["inventory"], env)
        check("torn release flagged, not exposed",
              "TORN/UNPUBLISHED" in r.stdout, r.stdout[-400:])
        r = run(cli + ["fetch", "--release",
                       "2026-07-05__seal-aaaaaaaaaaaa"], env, expect_rc=None)
        check("fetch refuses torn release", r.returncode != 0)
        r = run(pub + ["--date", "2026-07-05", "--no-rfq"], env)
        check("PRE-TL1 day publishes", r.returncode == 0, r.stderr[-300:])
        pre = [p for p in glob.glob(os.path.join(dest, "releases",
                                                 "2026-07-05__seal-*",
                                                 "MANIFEST.json"))]
        check("PRE-TL1 labeled", pre and
              json.load(open(pre[0]))["tl1_status"] == "PRE-TL1")

        print("== 8. duckdb memory_limit hygiene")
        import duckdb
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import research_data
        import research_release
        control = duckdb.connect()
        control.execute("SET memory_limit='8GB'")
        want = control.execute(
            "SELECT current_setting('memory_limit')").fetchone()[0]
        for mod in (research_data, research_release):
            conn = mod.duckdb_connect()
            got = conn.execute(
                "SELECT current_setting('memory_limit')").fetchone()[0]
            check("%s sets memory_limit" % mod.__name__, got == want,
                  (got, want))
            conn.close()
        control.close()
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
