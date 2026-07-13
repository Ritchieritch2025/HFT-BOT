#!/usr/bin/env python3
"""Focused tests for tools/recover_seal_20260712.py (W05 ADDENDUM 8).

Offline fixtures only — production is NEVER touched (proc root, live dir,
warehouse, raw root and the export command are all fixture-scoped; the
one-shot checkpoint uses the REAL deployed tools/ingest.py RFQ fast-path
against the fixture staging DB for full clause-12/13/14 fidelity).

Covers, by clause:
  1-3   mechanical discovery (seal inventory x checkpoints), strict RFQ
        family regex identity with ingest.RFQ_FASTPATH_RE, non-RFQ behind
        file => exact blocker.
  4-5   ordinary/non-symlink/inside-root/non-empty/newline/stability
        validation; invalid existing seal => blocker; --discover-only is
        read-only and prints the exact target inventory.
  6-8   foreign export_pause refused; another recovery process refused;
        ingest PID verification (uid + cwd + full cmdline) accepts only the
        real daemon shape and refuses everything else.
  10    stale-token ownership parsing.
  12-15 full end-to-end recovery on the fixture: verified TERM of a live
        fake daemon, frozen one-shot via the REAL ingest fast-path,
        checkpoint==size + identity/invariance proofs, stub export chain
        (--check-caught-up/--force --no-prune/--seal/--verify-seal), seal
        created + verified, pause token removed, ingest resumed with the
        exact supervisor command line, clause-18 growth/daemon proofs.
  13    a one-shot that materializes facts rows => blocked, day unsealed.

Run: python3 tests/test_recover_seal_20260712.py [scratch]
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest as ingest_mod            # noqa: E402
import recover_seal_20260712 as rs     # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("ok" if cond else "FAIL", name,
                         (" — " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(env, *args, expect_rc=None):
    r = subprocess.run([sys.executable, "tools/recover_seal_20260712.py"]
                       + list(args), cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if expect_rc is not None and r.returncode != expect_rc:
        print("    rc=%d\n    stdout: %s\n    stderr: %s"
              % (r.returncode, r.stdout[-600:], r.stderr[-600:]))
    return r


def write_raw(path, lines=3, newline=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i in range(lines):
            f.write('{"channel":"x","recv_wall_ns":1783900000000000000,'
                    '"n":%d}\n' % i)
        if not newline:
            f.write('{"partial"')
    return os.path.abspath(path)


def build_fixture(tmp):
    """Fixture warehouse + raw + live + proc root + env. Behind set =
    exactly: date=2026-07-12/{rfq_13.ndjson, rfq_receipts_13.ndjson} and
    date=2026-07-13/rfq_00.ndjson."""
    import duckdb
    wroot = os.path.join(tmp, "warehouse")
    rroot = os.path.join(tmp, "raw")
    live = os.path.join(tmp, "live")
    procr = os.path.join(tmp, "proc")
    for d in (wroot, rroot, live, procr,
              os.path.join(wroot, "facts"), os.path.join(wroot, "seals")):
        os.makedirs(d, exist_ok=True)
    d12, d13 = (os.path.join(rroot, "date=2026-07-12"),
                os.path.join(rroot, "date=2026-07-13"))
    done = {}
    done["fh12"] = write_raw(os.path.join(d12, "firehose_13.ndjson"))
    done["l212"] = write_raw(os.path.join(d12, "l2_13.ndjson"))
    done["fh00"] = write_raw(os.path.join(d13, "firehose_00.ndjson"))
    done["fh01"] = write_raw(os.path.join(d13, "firehose_01.ndjson"))
    behind = [
        write_raw(os.path.join(d12, "rfq_13.ndjson")),
        write_raw(os.path.join(d12, "rfq_receipts_13.ndjson")),
        write_raw(os.path.join(d13, "rfq_00.ndjson")),
    ]
    # growth files (hour >= cross_day_hours: OUTSIDE the seal inventory)
    for fn in ("firehose_02.ndjson", "l2_02.ndjson", "rfq_02.ndjson"):
        write_raw(os.path.join(d13, fn))
    staging = os.path.join(wroot, "staging.duckdb")
    con = duckdb.connect(staging)
    con.execute(ingest_mod.STAGING_DDL)
    for p in done.values():
        con.execute("INSERT INTO checkpoint VALUES (?, ?, 1)",
                    [p, os.path.getsize(p)])
    con.execute("INSERT INTO ingest_stats VALUES ('2026-07-12','Sports',"
                "1,1,1)")
    con.close()
    # stub export chain (clause 16 shape; writes a SEALED v2 seal on --seal)
    stub = os.path.join(tmp, "stub_export.py")
    with open(stub, "w") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "a = sys.argv[1:]\n"
            "date = a[a.index('--date')+1]\n"
            "sp = os.path.join(os.environ['WAREHOUSE_ROOT'], 'seals',\n"
            "                  'date=%s.json' % date)\n"
            "if '--seal' in a:\n"
            "    json.dump({'date': date, 'status': 'SEALED', 'version': 2,\n"
            "               'method': 'full_v2', 'archive_file_stats': [],\n"
            "               'raw_files': [], 'manifest_date_sha256': 'x'},\n"
            "              open(sp, 'w'))\n"
            "if '--verify-seal' in a and not os.path.isfile(sp):\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n")
    env = dict(os.environ)
    env.update({
        "WAREHOUSE_ROOT": wroot, "RAW_ROOT": rroot, "STAGING_DB": staging,
        "ARCHIVE_ROOT": os.path.join(wroot, "facts"),
        "RECOVER_LIVE_DIR": live, "RECOVER_PROC_ROOT": procr,
        "RECOVER_EXPECT_USER": pwd.getpwuid(os.getuid()).pw_name,
        "RECOVER_EXPECT_CWD": tmp,
        "RECOVER_STABILITY_SECS": "0.2",
        "RECOVER_TERM_TIMEOUT": "15",
        "RECOVER_GROWTH_TIMEOUT": "12",
        "RECOVER_DB_RETRIES": "3", "RECOVER_DB_RETRY_SECS": "0.1",
        "RECOVER_EXPORT_CMD": "%s %s" % (sys.executable, stub),
    })
    return {"tmp": tmp, "wroot": wroot, "rroot": rroot, "live": live,
            "proc": procr, "behind": behind, "done": done,
            "staging": staging, "env": env, "d13": d13}


def proc_entry(fx, pid, cmdline_tokens, cwd=None):
    d = os.path.join(fx["proc"], str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(t.encode() for t in cmdline_tokens) + b"\0")
    link = os.path.join(d, "cwd")
    if os.path.islink(link):
        os.remove(link)
    os.symlink(cwd or fx["tmp"], link)
    return d


def fake_daemon(fx):
    """A real process that behaves like the ingest daemon under TERM, with a
    matching fixture /proc entry."""
    p = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, sys, time\n"
         "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
         "while True: time.sleep(0.2)\n"])
    proc_entry(fx, p.pid, ["python3", "tools/ingest.py", "--loop"])
    with open(os.path.join(fx["live"], "ingest.pid"), "w") as f:
        f.write(str(p.pid))
    return p


def checkpoints(fx):
    import duckdb
    con = duckdb.connect(fx["staging"], read_only=True)
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT file, byte_offset FROM checkpoint").fetchall()}
    finally:
        con.close()


def main():
    scratch = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
        else tempfile.gettempdir()
    tmp = tempfile.mkdtemp(prefix="w05_recover_", dir=scratch)
    resumed_pids = []
    procs = []
    try:
        print("== 1. clause 3: RFQ family regex identity + strictness")
        check("regex object IS ingest.RFQ_FASTPATH_RE",
              rs.RFQ_RE is ingest_mod.RFQ_FASTPATH_RE)
        check("pattern is the reviewed one",
              rs.RFQ_RE.pattern == rs._EXPECTED_RFQ_PATTERN)
        for good in ("rfq_00.ndjson", "rfq_23.ndjson.10",
                     "rfq_receipts_07.ndjson.2"):
            check("accepts %s" % good, bool(rs.RFQ_RE.match(good)))
        for bad in ("firehose_13.ndjson", "l2_00.ndjson", "rfq_1.ndjson",
                    "rfq_00.ndjson.x", "xrfq_00.ndjson", "rfq_00.ndjsonX"):
            check("rejects %s" % bad, not rs.RFQ_RE.match(bad))

        print("== 2. clause 4: per-file validation units")
        fx = build_fixture(tmp)
        os.environ.update(fx["env"])   # unit-level calls read the env too
        p = fx["behind"][0]
        try:
            rs.validate_target(p, fx["rroot"])
            check("valid rfq target accepted", True)
        except rs.Blocked as b:
            check("valid rfq target accepted", False, b)
        try:
            rs.validate_target(fx["done"]["fh12"], fx["rroot"])
            check("non-rfq refused", False)
        except rs.Blocked as b:
            check("non-rfq refused", "NOT an RFQ-family" in str(b))
        nn = write_raw(os.path.join(fx["rroot"], "date=2026-07-12",
                                    "rfq_14.ndjson"), newline=False)
        try:
            rs.validate_target(nn, fx["rroot"])
            check("non-newline-terminated refused", False)
        except rs.Blocked as b:
            check("non-newline-terminated refused",
                  "newline" in str(b))
        os.remove(nn)
        ln = os.path.join(fx["rroot"], "date=2026-07-12",
                          "rfq_15.ndjson")
        os.symlink(p, ln)
        try:
            rs.validate_target(ln, fx["rroot"])
            check("symlink refused", False)
        except rs.Blocked as b:
            check("symlink refused", "symlink" in str(b) or
                  "ordinary" in str(b))
        os.remove(ln)
        s1 = rs.sample_file(p)
        s2 = rs.sample_file(p)
        check("stability sample reproducible", s1 == s2)
        with open(p, "a") as f:
            f.write('{"late":1}\n')
        check("mutation detected by sampling", rs.sample_file(p) != s1)

        print("== 3. clause 8: ingest PID verification fixture")
        d = fake_daemon(fx)
        procs.append(d)
        pidfile = os.path.join(fx["live"], "ingest.pid")
        check("verified real-shaped daemon",
              rs.verify_ingest_pid(pidfile) == d.pid)
        proc_entry(fx, d.pid, ["python3", "tools/other_tool.py"])
        try:
            rs.verify_ingest_pid(pidfile)
            check("wrong cmdline refused", False)
        except rs.Blocked as b:
            check("wrong cmdline refused", "command line" in str(b))
        wrongdir = os.path.join(tmp, "elsewhere")
        os.makedirs(wrongdir, exist_ok=True)
        proc_entry(fx, d.pid, ["python3", "tools/ingest.py", "--loop"],
                   cwd=wrongdir)
        try:
            rs.verify_ingest_pid(pidfile)
            check("wrong cwd refused", False)
        except rs.Blocked as b:
            check("wrong cwd refused", "cwd" in str(b))
        proc_entry(fx, d.pid, ["python3", "tools/ingest.py", "--loop"])
        d.terminate()
        d.wait(timeout=10)
        check("dead pidfile -> nothing to stop",
              rs.verify_ingest_pid(pidfile) is None)
        os.remove(pidfile)

        print("== 4. clause 10: token ownership parsing")
        check("owner pid parsed",
              rs.owner_pid("recover_seal_20260712 pid=4242 nonce=ab") == 4242)
        check("foreign token has no owner shape",
              rs.owner_pid("seal_chain") is None)

        print("== 5. clause 13: one-shot summary contract")
        m = rs.ONE_SHOT_RE.search(
            "x\ningested: orderbooks_l1 +0, trades +0, orderbooks_full +0\n")
        check("zero-facts line accepted", m and m.groups() == ("0",) * 3)
        m2 = rs.ONE_SHOT_RE.search(
            "ingested: orderbooks_l1 +3, trades +0, orderbooks_full +0")
        check("nonzero counts visible to the gate",
              m2 and m2.groups() != ("0",) * 3)

        print("== 6. clauses 1-5: --discover-only is read-only + exact")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        fx = build_fixture(tmp)
        r = run(fx["env"], "--discover-only", expect_rc=0)
        check("discover-only rc=0", r.returncode == 0, r.stderr[-300:])
        check("inventory lists exactly the 3 behind rfq files",
              all(b in r.stdout for b in fx["behind"])
              and "firehose" not in r.stdout.split(
                  "TARGET-FILE INVENTORY")[-1])
        check("discover-only wrote nothing",
              not os.path.exists(os.path.join(fx["live"], "export_pause"))
              and not os.path.exists(os.path.join(
                  fx["live"], "recover_seal_20260712.lock")))

        print("== 7. blockers (clauses 5-7)")
        # non-RFQ behind: drop the firehose checkpoint
        import duckdb
        con = duckdb.connect(fx["staging"])
        con.execute("DELETE FROM checkpoint WHERE file = ?",
                    [fx["done"]["fh12"]])
        con.close()
        r = run(fx["env"], "--discover-only", expect_rc=2)
        check("non-RFQ behind file blocks with the exact reason",
              r.returncode == 2 and "NOT an RFQ-family" in r.stderr
              and "DATA_PLANE_BLOCKED" in r.stderr)
        con = duckdb.connect(fx["staging"])
        con.execute("INSERT INTO checkpoint VALUES (?, ?, 1)",
                    [fx["done"]["fh12"],
                     os.path.getsize(fx["done"]["fh12"])])
        con.close()
        # foreign export_pause
        pause = os.path.join(fx["live"], "export_pause")
        with open(pause, "w") as f:
            f.write("seal_chain pid=999999\n")
        r = run(fx["env"], "--operator-approved", expect_rc=2)
        check("foreign export_pause refused",
              r.returncode == 2 and "foreign export_pause" in r.stderr)
        check("foreign pause untouched", os.path.isfile(pause))
        os.remove(pause)
        # another recovery process visible in proc
        proc_entry(fx, 424242, ["python3",
                                "tools/recover_seal_20260712.py",
                                "--operator-approved"])
        r = run(fx["env"], "--operator-approved", expect_rc=2)
        check("second recovery process refused",
              r.returncode == 2
              and "another seal/recovery process" in r.stderr)
        shutil.rmtree(os.path.join(fx["proc"], "424242"))
        # invalid existing target-date seal
        sp = os.path.join(fx["wroot"], "seals", "date=2026-07-12.json")
        with open(sp, "w") as f:
            f.write("{corrupt")
        r = run(fx["env"], "--operator-approved", expect_rc=2)
        check("existing invalid seal blocks (never overwritten)",
              r.returncode == 2 and "invalid" in r.stderr
              and open(sp).read() == "{corrupt")
        os.remove(sp)

        print("== 8. clause 13 violation: facts-materializing one-shot "
              "blocks, day left unsealed")
        bad_ing = os.path.join(tmp, "stub_bad_ingest.py")
        with open(bad_ing, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys\n"
                    "if '--loop' in sys.argv: sys.exit(0)\n"
                    "print('ingested: orderbooks_l1 +3, trades +0, "
                    "orderbooks_full +0')\n")
        env_bad = dict(fx["env"],
                       RECOVER_INGEST_CMD="%s %s"
                       % (sys.executable, bad_ing))
        r = run(env_bad, "--operator-approved", expect_rc=2)
        check("facts rows from the one-shot block the recovery",
              r.returncode == 2 and "materialized facts" in r.stderr)
        check("day left unsealed", not os.path.isfile(sp))
        check("pause token cleaned up after the block",
              not os.path.exists(pause))

        print("== 9. full end-to-end recovery (clauses 6-18) on fixture")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        fx = build_fixture(tmp)
        d = fake_daemon(fx)
        procs.append(d)
        # reap the child the moment it exits (models the supervisor's bash
        # reaping in production, so kill(pid, 0) goes dead promptly)
        import threading
        threading.Thread(target=d.wait, daemon=True).start()
        pre_cps = checkpoints(fx)
        check("targets start behind",
              all(pre_cps.get(b) is None for b in fx["behind"]))
        # background appender: growth proof material (non-target files)
        app = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "paths = sys.argv[1:]\n"
             "for _ in range(200):\n"
             "    for p in paths:\n"
             "        open(p, 'a').write('{\"g\":1}\\n')\n"
             "    time.sleep(0.15)\n",
             os.path.join(fx["d13"], "firehose_02.ndjson"),
             os.path.join(fx["d13"], "l2_02.ndjson"),
             os.path.join(fx["d13"], "rfq_02.ndjson")])
        procs.append(app)
        r = run(fx["env"], "--operator-approved", expect_rc=0)
        check("recovery rc=0", r.returncode == 0,
              (r.stdout + r.stderr)[-700:])
        check("RECOVERY_OK printed with next-step commands",
              "RECOVERY_OK" in r.stdout and "research_sync" in r.stdout)
        check("fake ingest daemon was TERMed (verified pid only)",
              d.poll() == 0)
        sp = os.path.join(fx["wroot"], "seals", "date=2026-07-12.json")
        check("2026-07-12 seal written by the export chain",
              os.path.isfile(sp)
              and json.load(open(sp)).get("status") == "SEALED")
        post_cps = checkpoints(fx)
        check("every target checkpoint == exact file size (clause 14)",
              all(post_cps.get(b) == os.path.getsize(b)
                  for b in fx["behind"]))
        check("non-target checkpoints unchanged (clause 15)",
              all(post_cps.get(p) == pre_cps.get(p)
                  for p in fx["done"].values()))
        check("pause token removed; lock released",
              not os.path.exists(os.path.join(fx["live"], "export_pause"))
              and not os.path.exists(os.path.join(
                  fx["live"], "recover_seal_20260712.lock")))
        pidfile = os.path.join(fx["live"], "ingest.pid")
        check("ingest resumed via the exact supervisor line",
              os.path.isfile(pidfile))
        if os.path.isfile(pidfile):
            rpid = int(open(pidfile).read().strip())
            resumed_pids.append(rpid)
            check("resumed ingest daemon alive", rs.pid_alive(rpid))
        check("one-shot ran the REAL deployed fast-path (log shows the "
              "zero-facts summary)",
              "ingested: orderbooks_l1 +0, trades +0, orderbooks_full +0"
              in r.stdout)

        print("== 10. idempotent re-run: existing valid seal is verified, "
              "never resealed")
        r = run(fx["env"], "--operator-approved", expect_rc=0)
        check("second run verifies and stops",
              r.returncode == 0 and "ALREADY_SEALED_VERIFIED" in r.stdout)
    finally:
        for pr in procs:
            if pr.poll() is None:
                pr.kill()
        for rp in resumed_pids:
            try:
                os.kill(rp, 15)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print("FAILURES: %s" % ", ".join(FAILS))
        print("TEST FAIL")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
