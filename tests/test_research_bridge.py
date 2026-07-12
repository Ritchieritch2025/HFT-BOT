#!/usr/bin/env python3
"""PIPE-W05 Phase A acceptance proofs: research release publisher + Mac CLI,
per the operator correction order 2026-07-12 (fixes 1-6 live here; fix 7's UI
fixtures live in the main repo's sandbox/research/test_event_intel.py).

Fixture-based, no network, no credentials: tmp directories stand in for the
S3 research/ prefix AND the ec2/raw vault. Demonstrates:
  1.  SEAL GATE: an unsealed day is refused; nothing lands.
  2.  IDENTITY (fix 1): release_id embeds the publication-state digest;
      manifest freezes the full publication_state; re-publishing the same
      state is a no-op; a NEW correction publishes a DISTINCT release.
  3.  STAGING (fix 2): mutable aux is hashed from staged copies (asserted
      via LocalDest post-upload re-verification passing byte-for-byte).
  4.  FAIL-CLOSED: a tampered archive aborts with nothing exposed.
  5.  POST-UPLOAD VERIFY (fix 3): every object re-verified at the
      destination; version_id recorded per object (null on filesystems);
      a destination mismatch aborts (unit-tested on LocalDest).
  6.  L2 HONESTY (fix 4): sealed orderbooks_full facts expose
      INCLUDED_SEALED_FACTS; otherwise ABSENT_FROM_THIS_RELEASE, and no
      "no L2 extractor" claim exists anywhere in the manifest.
  7.  TIER DERIVATION (fix 5): SEALED_CONFIRMATION only when earned;
      missing gap evidence / not-go-eligible seals publish as
      SEALED_DEGRADED_EVIDENCE with reasons in evidence_tier_basis.
  8.  RFQ (fix 6): enumerated from the EXACT seal raw_files list including
      cross-day receipt hours; a locally-pruned file is reconstructed from
      the raw vault (byte-verified); pruned+vault-missing aborts; the
      operator switch defaults OFF.
  9.  CLI: inventory/fetch/verify/view; corruption => exit 2, not exposed;
      torn release never exposed; PRE-TL1 labeling; version-pinned fetch
      plumbing (null version ids offline).
  10. HYGIENE: explicit DuckDB memory_limit on every connection.

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
import time

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


def next_day(date):
    d = datetime.date.fromisoformat(date)
    return (d + datetime.timedelta(days=1)).isoformat()


def make_day(tmp, date, tl1=True, with_rfq_raw=True, with_l2=False,
             go_eligible=True, cross_day_rfq=False):
    """Synthetic sealed warehouse day: L1 parquet + trades csv.gz
    (+ optional orderbooks_full) + manifest rows + v2 seal (+ sealed rfq raw
    incl. optional cross-day receipt hour + decoy firehose)."""
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
    pairs = [("orderbooks_l1", l1)]
    if with_l2:
        l2_dir = os.path.join(facts, "orderbooks_full", "category=Sports",
                              "subcategory=Baseball", "date=%s" % date)
        os.makedirs(l2_dir, exist_ok=True)
        l2 = os.path.join(
            l2_dir, "orderbooks_full__Sports__Baseball__%s.parquet" % date)
        conn.execute(
            "COPY (SELECT 'T-%s' AS market_ticker, 1719999990000000 AS "
            "ts_utc, 'snapshot' AS msg_type, 7 AS ws_sid, 99 AS ws_seq%s) "
            "TO '%s' (FORMAT PARQUET)" % (date, ladder_sql, l2))
        pairs.append(("orderbooks_full", l2))
    conn.close()
    tr = os.path.join(tr_dir, "trades__Sports__Baseball__%s.csv.gz" % date)
    cols = ["trade_id", "market_ticker", "ts_utc", "count"] + \
        (list(LADDER) if tl1 else [])
    with gzip.open(tr, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow(["t1", "T-%s" % date, 1719999991000000, 5] +
                   ([1, 2, 3, 4] if tl1 else []))
    pairs.append(("trades", tr))

    # warehouse manifest rows for the date
    mpath = os.path.join(tmp, "warehouse", "manifest.csv")
    new = not os.path.exists(mpath)
    with open(mpath, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "table", "category", "subcategory",
                        "row_count", "file_path", "file_md5", "created_ts"])
        for t, p in pairs:
            w.writerow([date, t, "Sports", "Baseball", 1, p, "x", "now"])
    manifest_sha, _ = wc.manifest_date_sha256(mpath, date)

    # raw: sealed rfq (+ optional cross-day receipt hour) + decoy firehose
    raw_files = []

    def raw_file(day_dir_date, fn):
        rawd = os.path.join(tmp, "raw", "date=%s" % day_dir_date)
        os.makedirs(rawd, exist_ok=True)
        p = os.path.join(rawd, fn)
        with open(p, "w") as f:
            f.write('{"chan":"%s","d":"%s"}\n' % (fn, day_dir_date))
        raw_files.append({"file": "date=%s/%s" % (day_dir_date, fn),
                          "sha256": sha256_file(p),
                          "size": os.stat(p).st_size, "checkpoint": None,
                          "inode": 1, "ctime_ns": 1, "mtime_ns": 1})
        return p

    if with_rfq_raw:
        raw_file(date, "rfq_13.ndjson")
        raw_file(date, "rfq_13.ndjson.1")
        raw_file(date, "rfq_receipts_13.ndjson")
        if cross_day_rfq:
            raw_file(next_day(date), "rfq_00.ndjson")
    raw_file(date, "firehose_13.ndjson")

    stats = [{"file": os.path.relpath(p, facts), "table": t,
              "size": os.stat(p).st_size, "sha256": sha256_file(p),
              "md5": "x", "inode": 1, "ctime_ns": 1, "mtime_ns": 1}
             for t, p in pairs]
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
            "go_no_go_eligible": go_eligible}
    with open(os.path.join(seals, "date=%s.json" % date), "w") as f:
        json.dump(seal, f, indent=1)
    return l1, tr


def main():
    scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir()
    tmp = tempfile.mkdtemp(prefix="w05_bridge_", dir=scratch)
    day_us = wc.day_start_us("2026-07-11")
    try:
        env = dict(os.environ)
        # remediation item 6: the bridge runs in the research namespace —
        # production credentials are never part of its environment, and the
        # research key file location is pinned away from the real host file
        for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
            env.pop(k, None)
        env.update({
            "WAREHOUSE_ROOT": os.path.join(tmp, "warehouse"),
            "ARCHIVE_ROOT": os.path.join(tmp, "warehouse", "facts"),
            "RAW_ROOT": os.path.join(tmp, "raw"),
            "RESEARCH_INCLUDE_RFQ": "0",
            "KALSHI_RESEARCH_ENV_FILE": os.path.join(tmp, "no_such_env.sh"),
        })
        live = os.path.join(tmp, "live")   # affirmative gap-scan markers
        os.makedirs(live)
        for d in ("2026-07-11", "2026-07-06", "2026-07-05"):
            open(os.path.join(live, "gaps_%s.done" % d), "w").close()
        # NOTE: 2026-07-03 deliberately has NO completed-scan marker and
        # 2026-07-05 has a marker but NO gap record (both must degrade —
        # absence is not evidence, item 4)
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
        empty_quality = os.path.join(tmp, "quality_empty")
        os.makedirs(quality)
        os.makedirs(empty_quality)
        with open(os.path.join(quality, "capture_gaps.csv"), "w",
                  newline="") as f:
            w = csv.writer(f)
            w.writerow(["start_us", "end_us"])
            w.writerow([day_us + 1000, day_us + 90_000_000])      # in 07-11
            w.writerow([day_us - 7_200_000_000, day_us - 3_600_000_000])
        with open(os.path.join(quality, "l2_gaps_2026-07-11.json"), "w") as f:
            json.dump({"no_l2_files": True, "seq_gap_events": 0,
                       "seq_missed_total": 0, "sids_total": 0,
                       "sids_with_seq_gaps": 0, "lines": 0}, f)

        l1_file, _tr = make_day(tmp, "2026-07-11", tl1=True,
                                cross_day_rfq=True)
        make_day(tmp, "2026-07-05", tl1=False, with_rfq_raw=False)
        make_day(tmp, "2026-07-03", tl1=True, with_rfq_raw=False,
                 go_eligible=False)
        make_day(tmp, "2026-07-06", tl1=True, with_rfq_raw=False,
                 with_l2=True)
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest)
        vault = os.path.join(tmp, "vault")   # ec2/raw stand-in (fix 6)
        os.makedirs(vault)
        cache = os.path.join(tmp, "cache")
        pub = ["tools/research_release.py", "publish", "--dest", dest,
               "--quality-dir", quality, "--raw-vault", vault,
               "--live-dir", live]
        cli = ["tools/research_data.py", "--root", dest, "--cache", cache]

        print("== 1. seal gate")
        r = run(pub + ["--date", "2026-07-04", "--no-rfq"], env,
                expect_rc=None)
        check("unsealed day refused", r.returncode != 0)
        check("nothing published for unsealed day",
              not glob.glob(os.path.join(dest, "releases", "2026-07-04*")))

        print("== 2. publish sealed day (rfq default OFF) + identity (fix 1)")
        r = run(pub + ["--date", "2026-07-11"], env)
        check("publish rc=0", r.returncode == 0, r.stderr[-300:])
        rels = glob.glob(os.path.join(dest, "releases",
                                      "2026-07-11__seal-*__pub-*"))
        check("one release, id embeds publication-state digest",
              len(rels) == 1, rels)
        rid = os.path.basename(rels[0]) if rels else ""
        mpath = os.path.join(dest, "releases", rid, "MANIFEST.json")
        check("MANIFEST.json present", os.path.isfile(mpath))
        man = json.load(open(mpath)) if os.path.isfile(mpath) else {}
        check("manifest schema v2",
              man.get("schema_version") == "research-release-manifest-v2")
        check("publication_state frozen in manifest",
              man.get("publication_state_sha256")
              and rid.endswith("__pub-%s"
                               % man["publication_state_sha256"][:16]))
        check("tier DERIVED = SEALED_CONFIRMATION with basis (fix 5)",
              man.get("evidence_tier") == "SEALED_CONFIRMATION"
              and man.get("evidence_tier_basis", {}).get(
                  "downgrade_reasons") == [],
              man.get("evidence_tier_basis"))
        check("tl1_status TL1", man.get("tl1_status") == "TL1")
        ch = man.get("channels", {})
        check("L2 honestly ABSENT_FROM_THIS_RELEASE (fix 4)",
              ch.get("orderbooks_l2", {}).get("status")
              == "ABSENT_FROM_THIS_RELEASE")
        check("no false 'extractor' claim anywhere (fix 4)",
              "extractor" not in json.dumps(man).lower())
        check("rfq default EXCLUDED",
              ch.get("rfq", {}).get("status")
              == "EXCLUDED_PENDING_OPERATOR_COST_ACK")
        check("rfq enumerated from seal incl. cross-day (fix 6)",
              ch.get("rfq", {}).get("sealed_rfq_files_in_day_seal") == 4)
        check("gap intervals day-filtered == 1",
              ch.get("orderbooks_l1", {}).get("gap_intervals_for_date") == 1)
        check("AFFIRMATIVE gap receipt frozen (item 4)",
              ch.get("orderbooks_l1", {}).get("gap_receipt_affirmative")
              is True
              and man.get("publication_state", {}).get(
                  "gap_evidence", {}).get("affirmative_receipt") is True
              and man.get("publication_state", {}).get(
                  "gap_evidence", {}).get("gap_receipt_sha256"))
        check("version binding frozen; unversioned dest = LOUD explicit "
              "degraded mode (item 1)",
              man.get("version_binding", {}).get("mode")
              == "UNVERSIONED_DEST_DEGRADED"
              and man.get("version_binding", {}).get("bindings_sha256")
              and "UNVERSIONED_DEST_DEGRADED" in r.stdout
              and "WARNING" in r.stdout, r.stdout[-400:])
        keys = {o["key"] for o in man.get("objects", [])}
        check("gap receipt object shipped",
              "quality/gap_receipt_2026-07-11.json" in keys)
        check("no raw published by default",
              not any(k.startswith("raw_rfq/") for k in keys))
        check("firehose never published",
              not any("firehose" in k for k in keys))
        check("post-upload verification recorded per object (fix 3)",
              man.get("post_upload_verification", {}).get(
                  "objects_verified") == len(man.get("objects", []))
              and all("version_id" in o for o in man.get("objects", [])))
        on_disk = {os.path.relpath(p, rels[0]).replace(os.sep, "/")
                   for p in glob.glob(os.path.join(rels[0], "**", "*"),
                                      recursive=True) if os.path.isfile(p)}
        check("manifest lists exactly the uploaded objects",
              keys == on_disk - {"MANIFEST.json"})
        r = run(pub + ["--date", "2026-07-11"], env)
        check("same-state republish is a no-op", r.returncode == 0
              and "already published" in r.stdout, r.stdout[-200:])

        print("== 3. a NEW correction creates a DISTINCT release (fix 1)")
        cdir = os.path.join(tmp, "warehouse", "corrections",
                            "date=2026-07-11")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "late_rows.ndjson"), "w") as f:
            f.write('{"row":"late"}\n')
        with open(os.path.join(tmp, "warehouse", "corrections",
                               "ledger.ndjson"), "w") as f:
            f.write(json.dumps({"event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
                                "exchange_date": "2026-07-11",
                                "n_rows": 1}) + "\n")
        r = run(pub + ["--date", "2026-07-11"], env)
        check("corrected publish rc=0", r.returncode == 0, r.stderr[-300:])
        rels2 = sorted(glob.glob(os.path.join(dest, "releases",
                                              "2026-07-11__seal-*__pub-*")))
        check("distinct release id for the corrected state",
              len(rels2) == 2, rels2)
        rid_corr = [os.path.basename(p) for p in rels2
                    if os.path.basename(p) != rid]
        rid_corr = rid_corr[0] if rid_corr else ""
        man_c = json.load(open(os.path.join(dest, "releases", rid_corr,
                                            "MANIFEST.json")))
        check("corrections frozen in the new release",
              man_c["corrections"]["included_files"] == 1
              and man_c["corrections"]["ledger_day_entries"] == 1
              and "corrections/date=2026-07-11/late_rows.ndjson"
              in {o["key"] for o in man_c["objects"]})

        print("== 4. fail-closed on tamper")
        good = open(l1_file, "rb").read()
        with open(l1_file, "ab") as f:
            f.write(b"CORRUPT")
        dest2 = os.path.join(tmp, "dest2")
        r = run(["tools/research_release.py", "publish", "--dest", dest2,
                 "--quality-dir", quality, "--raw-vault", vault,
                 "--live-dir", live,
                 "--date", "2026-07-11", "--no-rfq"], env, expect_rc=None)
        check("tampered archive aborts", r.returncode != 0)
        check("nothing exposed after abort",
              not glob.glob(os.path.join(dest2, "releases", "*",
                                         "MANIFEST.json")))
        with open(l1_file, "wb") as f:
            f.write(good)

        print("== 5. rfq: seal-list enumeration, pruning + vault "
              "reconstruction (fix 6)")
        pruned_local = os.path.join(tmp, "raw", "date=2026-07-11",
                                    "rfq_13.ndjson.1")
        pruned_bytes = open(pruned_local, "rb").read()
        os.remove(pruned_local)   # simulate prune_raw
        r = run(pub + ["--date", "2026-07-11", "--include-rfq"], env,
                expect_rc=None)
        check("pruned + vault-missing aborts", r.returncode != 0)
        vdst = os.path.join(vault, "date=2026-07-11", "rfq_13.ndjson.1")
        os.makedirs(os.path.dirname(vdst))
        with open(vdst, "wb") as f:
            f.write(b"WRONG BYTES - not the sealed content")
        r = run(pub + ["--date", "2026-07-11", "--include-rfq"], env,
                expect_rc=None)
        check("vault content not matching the seal aborts (item 2)",
              r.returncode != 0)
        with open(vdst, "wb") as f:
            f.write(pruned_bytes)
        r = run(pub + ["--date", "2026-07-11", "--include-rfq"], env)
        check("rfq publish with reconstruction rc=0", r.returncode == 0,
              r.stderr[-400:])
        rfq_rels = [p for p in glob.glob(os.path.join(
            dest, "releases", "2026-07-11__seal-*__pub-*", "MANIFEST.json"))
            if json.load(open(p))["channels"]["rfq"]["status"]
            == "INCLUDED_SEALED_RAW"]
        check("distinct rfq release exposed", len(rfq_rels) == 1)
        man_r = json.load(open(rfq_rels[0])) if rfq_rels else {}
        rid_rfq = man_r.get("release_id", "")
        k_r = {o["key"] for o in man_r.get("objects", [])}
        check("cross-day receipt-hour file included from seal list",
              "raw_rfq/date=2026-07-12/rfq_00.ndjson" in k_r)
        check("all 4 sealed rfq files present, no firehose",
              {"raw_rfq/date=2026-07-11/rfq_13.ndjson",
               "raw_rfq/date=2026-07-11/rfq_13.ndjson.1",
               "raw_rfq/date=2026-07-11/rfq_receipts_13.ndjson",
               "raw_rfq/date=2026-07-12/rfq_00.ndjson"} <= k_r
              and not any("firehose" in k for k in k_r))
        srcs = {f["file"]: f["source"]
                for f in man_r["channels"]["rfq"]["files"]}
        check("pruned file provenance = vault_reconstructed",
              srcs.get("date=2026-07-11/rfq_13.ndjson.1")
              == "vault_reconstructed"
              and srcs.get("date=2026-07-11/rfq_13.ndjson") == "local", srcs)

        print("== 6. CLI: inventory / fetch / verify / view")
        r = run(cli + ["inventory"], env)
        check("inventory rc=0", r.returncode == 0, r.stderr[-300:])
        check("inventory shows EXPOSED + L2 column + cost + cache lines",
              "EXPOSED" in r.stdout
              and "ABSENT_FROM_THIS_RELEASE" in r.stdout
              and "monthly cost estimate" in r.stdout
              and "local cache" in r.stdout, r.stdout[-500:])
        r = run(cli + ["fetch", "--release", rid_corr], env)
        check("fetch+verify PASS (corrected release)", r.returncode == 0,
              r.stdout[-400:])
        marker = os.path.join(cache, "releases", rid_corr, ".VERIFIED.json")
        check(".VERIFIED.json written", os.path.isfile(marker))
        time.sleep(1.1)  # strict verified_at ordering for the view winner
        r = run(cli + ["fetch", "--release", rid_rfq, "--with-rfq"], env)
        check("rfq fetch+verify PASS", r.returncode == 0, r.stdout[-400:])
        check("rfq VERIFIED_SEALED_RAW", "VERIFIED_SEALED_RAW" in r.stdout)
        view = os.path.join(cache, "view")
        check("view has warehouse shape",
              os.path.isfile(os.path.join(
                  view, "facts", "orderbooks_l1", "category=Sports",
                  "subcategory=Baseball", "date=2026-07-11",
                  "orderbooks_l1__Sports__Baseball__2026-07-11.parquet"))
              and os.path.isfile(os.path.join(view, "seals",
                                              "date=2026-07-11.json")))
        check("view maps rfq raw incl. cross-day date dirs",
              os.path.isfile(os.path.join(view, "raw", "date=2026-07-11",
                                          "rfq_13.ndjson"))
              and os.path.isfile(os.path.join(view, "raw", "date=2026-07-12",
                                              "rfq_00.ndjson")))

        print("== 7. corruption -> not exposed")
        cached_l1 = os.path.join(cache, "releases", rid_rfq, "facts",
                                 "orderbooks_l1", "category=Sports",
                                 "subcategory=Baseball", "date=2026-07-11",
                                 "orderbooks_l1__Sports__Baseball__"
                                 "2026-07-11.parquet")
        with open(cached_l1, "ab") as f:
            f.write(b"X")
        r = run(cli + ["verify", "--release", rid_rfq], env, expect_rc=2)
        check("verify exit 2 on corruption", r.returncode == 2)
        check("marker removed", not os.path.isfile(
            os.path.join(cache, "releases", rid_rfq, ".VERIFIED.json")))
        check(".FAILED.json written", os.path.isfile(
            os.path.join(cache, "releases", rid_rfq, ".FAILED.json")))
        check("loud warning", "NOT exposed" in r.stderr, r.stderr[-300:])
        prov = json.load(open(os.path.join(view, ".view_provenance.json")))
        check("view falls back to the still-verified sibling",
              prov["verified_releases"].get("2026-07-11") == rid_corr, prov)

        print("== 8. torn release never exposed; PRE-TL1 + degraded tiers")
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
        # 07-05: marker present but NO gap record -> no affirmative receipt
        # -> DERIVED degraded tier (fix 5 + item 4)
        r = run(["tools/research_release.py", "publish", "--dest", dest,
                 "--quality-dir", empty_quality, "--raw-vault", vault,
                 "--live-dir", live,
                 "--date", "2026-07-05", "--no-rfq"], env)
        check("PRE-TL1 day publishes", r.returncode == 0, r.stderr[-300:])
        pre = glob.glob(os.path.join(dest, "releases",
                                     "2026-07-05__seal-*__pub-*",
                                     "MANIFEST.json"))
        man_p = json.load(open(pre[0])) if pre else {}
        check("PRE-TL1 labeled", man_p.get("tl1_status") == "PRE-TL1")
        check("missing gap evidence downgrades the tier (fix 5)",
              man_p.get("evidence_tier") == "SEALED_DEGRADED_EVIDENCE"
              and any("gap" in x for x in man_p.get(
                  "evidence_tier_basis", {}).get("downgrade_reasons", [])),
              man_p.get("evidence_tier_basis"))
        r = run(pub + ["--date", "2026-07-03", "--no-rfq"], env)
        check("not-go-eligible day publishes degraded", r.returncode == 0)
        deg = glob.glob(os.path.join(dest, "releases",
                                     "2026-07-03__seal-*__pub-*",
                                     "MANIFEST.json"))
        man_d = json.load(open(deg[0])) if deg else {}
        check("go_no_go_eligible=false downgrades the tier (fix 5)",
              man_d.get("evidence_tier") == "SEALED_DEGRADED_EVIDENCE"
              and any("go_no_go" in x for x in man_d.get(
                  "evidence_tier_basis", {}).get("downgrade_reasons", [])))
        check("record WITHOUT completed-scan marker is NOT evidence "
              "(item 4)",
              any("affirmative" in x for x in man_d.get(
                  "evidence_tier_basis", {}).get("downgrade_reasons", [])),
              man_d.get("evidence_tier_basis"))

        print("== 9. sealed L2 facts exposed when present (fix 4)")
        r = run(pub + ["--date", "2026-07-06", "--no-rfq"], env)
        check("L2 day publishes", r.returncode == 0, r.stderr[-300:])
        l2m = glob.glob(os.path.join(dest, "releases",
                                     "2026-07-06__seal-*__pub-*",
                                     "MANIFEST.json"))
        man_l2 = json.load(open(l2m[0])) if l2m else {}
        check("orderbooks_full INCLUDED_SEALED_FACTS",
              man_l2.get("channels", {}).get("orderbooks_l2", {})
              .get("status") == "INCLUDED_SEALED_FACTS",
              man_l2.get("channels", {}).get("orderbooks_l2"))
        check("l2 facts objects present",
              any(o["key"].startswith("facts/orderbooks_full/")
                  for o in man_l2.get("objects", [])))

        print("== 10. post-upload destination verification unit (fix 3)")
        import research_release as rr
        vd = os.path.join(tmp, "vdest")
        os.makedirs(os.path.join(vd, "releases", "x"))
        with open(os.path.join(vd, "releases", "x", "a.bin"), "wb") as f:
            f.write(b"payload")
        ld = rr.LocalDest(vd)
        ok_sha = hashlib.sha256(b"payload").hexdigest()
        check("destination verify passes + null version id",
              ld.verify_object("releases/x/a.bin", 7, ok_sha) is None)
        try:
            ld.verify_object("releases/x/a.bin", 7, "0" * 64)
            check("destination sha mismatch aborts", False)
        except SystemExit:
            check("destination sha mismatch aborts", True)

        print("== 11. duckdb memory_limit hygiene")
        import duckdb
        import research_data
        control = duckdb.connect()
        control.execute("SET memory_limit='8GB'")
        want = control.execute(
            "SELECT current_setting('memory_limit')").fetchone()[0]
        for mod in (research_data, rr):
            conn = mod.duckdb_connect()
            got = conn.execute(
                "SELECT current_setting('memory_limit')").fetchone()[0]
            check("%s sets memory_limit" % mod.__name__, got == want,
                  (got, want))
            conn.close()
        control.close()

        print("== 12. cache containment: hostile manifest keys (item 3)")
        hostile_rid = "2026-07-01__seal-aaaaaaaa__pub-aaaaaaaaaaaaaaaa"
        hdir = os.path.join(dest, "releases", hostile_rid)
        os.makedirs(hdir)
        evil_dst = os.path.join(tmp, "evil.txt")
        with open(os.path.join(hdir, "MANIFEST.json"), "w") as f:
            json.dump({"schema_version": "research-release-manifest-v2",
                       "release_id": hostile_rid, "date": "2026-07-01",
                       "publication_state_sha256": "a" * 64,
                       "version_binding": {
                           "mode": "UNVERSIONED_DEST_DEGRADED",
                           "bindings_sha256": "b" * 64},
                       "seal": {"sha256": "c" * 64,
                                "manifest_date_sha256": "d" * 64},
                       "objects": [
                           {"key": "../../evil.txt", "size": 4,
                            "sha256": "e" * 64, "version_id": None},
                           {"key": "/abs/evil.txt", "size": 4,
                            "sha256": "e" * 64, "version_id": None}]}, f)
        r = run(cli + ["fetch", "--release", hostile_rid], env,
                expect_rc=None)
        check("hostile manifest keys refused", r.returncode not in (0,),
              (r.returncode, r.stdout[-200:]))
        check("containment named as the reason",
              "containment" in (r.stdout + r.stderr))
        check("no escape file written", not os.path.exists(evil_dst)
              and not os.path.exists("/abs/evil.txt"))
        try:
            research_data.contained_remove(os.path.join(tmp, "evil2"),
                                           os.path.join(tmp, "cache"))
            check("out-of-cache delete refused", False)
        except SystemExit:
            check("out-of-cache delete refused", True)

        print("== 13. version-binding tamper: VERSION_BOUND with null "
              "version ids fails verify (item 1)")
        vb_rid = "2026-07-02__seal-bbbbbbbb__pub-bbbbbbbbbbbbbbbb"
        vb_dir = os.path.join(dest, "releases", vb_rid)
        os.makedirs(vb_dir)
        tampered = dict(json.load(open(os.path.join(
            dest, "releases", rid_corr, "MANIFEST.json"))))
        tampered["release_id"] = vb_rid
        tampered["version_binding"] = dict(tampered["version_binding"],
                                           mode="VERSION_BOUND")
        with open(os.path.join(vb_dir, "MANIFEST.json"), "w") as f:
            json.dump(tampered, f)
        for o in tampered["objects"]:
            src = os.path.join(dest, "releases", rid_corr, o["key"])
            dst = os.path.join(vb_dir, o["key"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        r = run(cli + ["fetch", "--release", vb_rid], env, expect_rc=2)
        check("VERSION_BOUND + null version ids fails verify",
              r.returncode == 2 and "version" in (r.stdout + r.stderr),
              (r.returncode, r.stdout[-300:]))

        print("== 14. quarantined legacy releases (item 5)")
        legacy_rid = "2026-07-06__seal-cccccccccccc"
        lsrc = glob.glob(os.path.join(dest, "releases",
                                      "2026-07-06__seal-*__pub-*"))[0]
        ldir = os.path.join(dest, "releases", legacy_rid)
        shutil.copytree(lsrc, ldir)
        lman = json.load(open(os.path.join(ldir, "MANIFEST.json")))
        lman["schema_version"] = "research-release-manifest-v1"
        lman.pop("publication_state", None)
        lman.pop("publication_state_sha256", None)
        lman.pop("version_binding", None)
        lman["release_id"] = legacy_rid
        with open(os.path.join(ldir, "MANIFEST.json"), "w") as f:
            json.dump(lman, f)
        r = run(cli + ["inventory"], env)
        check("inventory shows QUARANTINED_LEGACY",
              "QUARANTINED_LEGACY" in r.stdout, r.stdout[-400:])
        r = run(cli + ["fetch", "--release", legacy_rid], env,
                expect_rc=None)
        check("quarantined fetch refused without override",
              r.returncode != 0 and "QUARANTINED" in (r.stdout + r.stderr))
        r = run(cli + ["fetch", "--release", legacy_rid,
                       "--allow-legacy-quarantined"], env)
        check("override fetch works and is BRANDED", r.returncode == 0
              and "QUARANTINED-LEGACY OVERRIDE" in (r.stdout + r.stderr),
              r.stdout[-300:] + r.stderr[-300:])
        lmarker = json.load(open(os.path.join(
            cache, "releases", legacy_rid, ".VERIFIED.json")))
        check("override marker branded",
              lmarker.get("quarantined_legacy_override") is True)
        prov = json.load(open(os.path.join(cache, "view",
                                           ".view_provenance.json")))
        check("override-only date enters the view BRANDED",
              prov["verified_releases"].get("2026-07-06") == legacy_rid
              and prov["quarantined_legacy_overrides"].get("2026-07-06")
              == legacy_rid and prov.get("quarantine_brand"), prov)
        clean_rid = os.path.basename(lsrc)
        r = run(cli + ["fetch", "--release", clean_rid], env)
        check("clean 07-06 release verifies", r.returncode == 0,
              r.stdout[-300:])
        prov = json.load(open(os.path.join(cache, "view",
                                           ".view_provenance.json")))
        check("a verified clean release ALWAYS beats the quarantined "
              "override in the view",
              prov["verified_releases"].get("2026-07-06") == clean_rid
              and "2026-07-06" not in prov["quarantined_legacy_overrides"],
              prov)

        print("== 15. credential modes (item 6) + operator gate (item 7)")
        env_prod = dict(env, KALSHI_API_KEY_ID="not-a-real-key")
        r = run(cli + ["inventory"], env_prod, expect_rc=None)
        check("CLI refuses production credentials in env",
              r.returncode != 0
              and "credential mode" in (r.stdout + r.stderr))
        bad_env_file = os.path.join(tmp, "bad_env.sh")
        with open(bad_env_file, "w") as f:
            f.write("export AWS_ACCESS_KEY_ID=x\n"
                    "export AWS_SECRET_ACCESS_KEY=y\n")
        os.chmod(bad_env_file, 0o644)
        env_bad = dict(env, KALSHI_RESEARCH_ENV_FILE=bad_env_file)
        r = run(["tools/research_data.py", "--root", "s3://dummy/research",
                 "--cache", cache, "inventory"], env_bad, expect_rc=None)
        check("CLI refuses group/other-readable key file (0600 required)",
              r.returncode != 0 and "0600" in (r.stdout + r.stderr))
        r = run(["tools/research_release.py", "publish", "--date",
                 "2026-07-11", "--dest", "s3://dummy/research",
                 "--quality-dir", quality, "--live-dir", live], env,
                expect_rc=2)
        check("publisher gate: real s3 publish refused without "
              "--operator-approved", r.returncode == 2
              and "operator-approved" in (r.stdout + r.stderr))
        os.chmod(bad_env_file, 0o600)
        env_mac = dict(env, KALSHI_RESEARCH_ENV_FILE=bad_env_file)
        r = run(["tools/research_release.py", "publish", "--date",
                 "2026-07-11", "--dest", "s3://dummy/research",
                 "--quality-dir", quality, "--live-dir", live,
                 "--operator-approved"], env_mac, expect_rc=2)
        check("publisher refuses on a research-key host (namespace split)",
              r.returncode == 2
              and "READ-ONLY key" in (r.stdout + r.stderr))

        print("== 16. exact-version candidate selection unit (item 2)")
        cands = rr.pick_candidate_versions(
            {"Versions": [
                {"VersionId": "old", "Size": 10,
                 "LastModified": "2026-07-10T00:00:00Z"},
                {"VersionId": "wrong-size", "Size": 11,
                 "LastModified": "2026-07-12T00:00:00Z"},
                {"VersionId": "new", "Size": 10,
                 "LastModified": "2026-07-11T00:00:00Z"}]}, 10)
        check("candidates size-filtered, newest first, never trusts "
              "'latest' blindly", cands == ["new", "old"], cands)
        check("no versions -> no candidates (degraded path)",
              rr.pick_candidate_versions({}, 10) == [])
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
