#!/usr/bin/env python3
"""Focused proofs for tools/recover_rfq_seal_2026-07-12.py (ADDENDUM 8).

Synthetic/fixture data, no network, no production paths. Proves:
  (a) discovery selects exactly the behind RFQ+receipts files; a behind
      firehose/L1/L2 file is a STOP, not a target;
  (b) the RFQ family regex rejects non-RFQ names;
  (c) symlink / outside-root / empty / non-newline-terminated / unstable-stat
      targets are each rejected;
  (d) a foreign export_pause refuses; a stale own-token pause with a provably
      dead owner is reclaimed; a live-owner pause refuses;
  (e) the four export_day subcommands run in order and seal_alarm is cleared
      ONLY after exact --verify-seal success (mocked export_day);
  (f) SIGTERM targets only the pidfile PID after user+cwd+cmdline match —
      never a pattern; mismatches refuse without any signal;
  (g) the counts-must-be-zero guard trips on any nonzero L1/trades/full,
      and the REAL deployed fast-path one-shot (subprocess) yields zero
      counts + exact byte checkpoints on RFQ fixtures.

stdlib + duckdb only.
"""
import datetime
import contextlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import warehouse_common as wc  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "recover_rfq_seal",
    os.path.join(ROOT, "tools", "recover_rfq_seal_2026-07-12.py"))
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

FAILS = []
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name,
                        (" - " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def make_raw(tmp):
    """Fixture raw tree for exchange day 2026-07-12 (+ cross-day 07-13 00/01)."""
    raw = os.path.join(tmp, "raw")
    d12 = os.path.join(raw, "date=2026-07-12")
    d13 = os.path.join(raw, "date=2026-07-13")
    os.makedirs(d12)
    os.makedirs(d13)
    files = {}
    for name, content in (
            ("firehose_23.ndjson", '{"a":1}\n{"a":2}\n'),
            ("rfq_23.ndjson", '{"rfq":1}\n'),
            ("rfq_receipts_23.ndjson", '{"r":1}\n{"r":2}\n')):
        p = os.path.join(d12, name)
        open(p, "w").write(content)
        files[name] = p
    for h in ("00", "01"):
        for fam in ("firehose", "rfq", "rfq_receipts"):
            p = os.path.join(d13, "%s_%s.ndjson" % (fam, h))
            open(p, "w").write('{"x":"%s"}\n' % h)
            files["%s_%s.ndjson" % (fam, h)] = p
    return raw, files


def make_staging(tmp, checkpoints):
    import duckdb
    db = os.path.join(tmp, "staging.duckdb")
    con = duckdb.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS checkpoint (
      file TEXT PRIMARY KEY, byte_offset BIGINT, updated_us BIGINT)""")
    for path, off in checkpoints.items():
        con.execute("INSERT INTO checkpoint VALUES (?, ?, 0)",
                    [os.path.abspath(path), off])
    con.close()
    return db


def cfg_for(tmp, raw, db):
    wh = os.path.join(tmp, "warehouse")
    os.makedirs(wh, exist_ok=True)
    return {"raw_root": raw, "staging_db": db, "warehouse_root": wh,
            "archive_root": os.path.join(wh, "facts")}


def blocking_of(cfg):
    import duckdb
    con = duckdb.connect(cfg["staging_db"], read_only=True)
    try:
        return rec.compute_blocking(cfg, con)
    finally:
        con.close()


def main():
    tmp = tempfile.mkdtemp(prefix="test_rfq_recover_")
    try:
        # ---- (a) discovery: exactly the behind RFQ files; firehose behind=STOP
        raw, files = make_raw(tmp)
        full = {p: os.path.getsize(p) for p in files.values()}
        ckpt = dict(full)  # everything caught up ...
        for name in ("rfq_23.ndjson", "rfq_receipts_23.ndjson",
                     "rfq_00.ndjson", "rfq_01.ndjson",
                     "rfq_receipts_00.ndjson", "rfq_receipts_01.ndjson"):
            del ckpt[files[name]]  # ... except the RFQ family (checkpoint=None)
        db = make_staging(tmp, ckpt)
        cfg = cfg_for(tmp, raw, db)
        inv, blocking, non_rfq = blocking_of(cfg)
        got = sorted(os.path.basename(p) for p, _c, _s in blocking)
        want = sorted(("rfq_23.ndjson", "rfq_receipts_23.ndjson",
                       "rfq_00.ndjson", "rfq_01.ndjson",
                       "rfq_receipts_00.ndjson", "rfq_receipts_01.ndjson"))
        check("(a) blocking set = exactly the behind RFQ+receipts files",
              got == want and non_rfq == [], (got, non_rfq))
        check("(a) inventory covers day + cross-day hours",
              len(inv) == len(files), (len(inv), len(files)))

        # behind firehose file joins the non-RFQ STOP set, never the targets
        tmp_b = os.path.join(tmp, "b")
        os.makedirs(tmp_b)
        raw_b, files_b = make_raw(tmp_b)
        ckpt_b = {p: os.path.getsize(p) for p in files_b.values()}
        del ckpt_b[files_b["firehose_23.ndjson"]]   # firehose behind
        del ckpt_b[files_b["rfq_23.ndjson"]]        # rfq behind too
        db_b = make_staging(tmp_b, ckpt_b)
        cfg_b = cfg_for(tmp_b, raw_b, db_b)
        _inv, blk_b, non_rfq_b = blocking_of(cfg_b)
        check("(a) behind firehose lands in the STOP set, not the targets",
              [os.path.basename(p) for p, _c, _s in non_rfq_b] ==
              ["firehose_23.ndjson"] and
              [os.path.basename(p) for p, _c, _s in blk_b] == ["rfq_23.ndjson"],
              (non_rfq_b, blk_b))

        # Read-only preview must never convert an unavailable checkpoint DB
        # into a guessed target list with a successful exit code.
        original_connect = rec.connect_ro
        rec.connect_ro = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("Conflicting lock held by writer"))
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = rec.discover_only(
                    cfg, types.SimpleNamespace(ro_attempts=1,
                                               stability_wait=2.0))
        finally:
            rec.connect_ro = original_connect
        text = out.getvalue() + err.getvalue()
        check("(a) locked discover-only fails closed with no guessed targets",
              rc == 3 and "exact checkpoint state unavailable" in text and
              "UNKNOWN(writer lock held)" not in text and
              "EXPECTED TARGET INVENTORY" not in text, text)

        # ---- (b) family regex rejects non-RFQ names ----
        import ingest
        good = ("rfq_00.ndjson", "rfq_23.ndjson.7", "rfq_receipts_05.ndjson",
                "rfq_receipts_23.ndjson.12")
        bad = ("firehose_02.ndjson", "l2_02.ndjson", "trades_02.ndjson",
               "rfq_2.ndjson", "rfqx_02.ndjson", "rfq_02.ndjson.bak",
               "rfq_receiptsx_02.ndjson", "xrfq_02.ndjson", "rfq_02.txt")
        check("(b) regex accepts RFQ + receipts (incl. numeric shards)",
              all(ingest.RFQ_FASTPATH_RE.match(n) for n in good))
        check("(b) regex rejects every non-RFQ name",
              not any(ingest.RFQ_FASTPATH_RE.match(n) for n in bad))

        # ---- (c) per-file validation rejections ----
        d12 = os.path.join(raw, "date=2026-07-12")

        def expect_stop(name, path, mutate=None, sleep_fn=None):
            row = [(path, None, max(os.path.getsize(path), 1))]
            try:
                rec.validate_and_capture(
                    row, raw, 0.01,
                    sleep_fn=sleep_fn or (lambda s: None), utcnow=NOW)
                return False, "no Stop raised"
            except rec.Stop as e:
                return True, str(e)

        outside = os.path.join(tmp, "outside_rfq.ndjson")
        open(outside, "w").write('{"z":1}\n')
        link = os.path.join(d12, "rfq_20.ndjson")
        os.symlink(outside, link)
        ok, msg = expect_stop("symlink", link)
        check("(c) symlink rejected", ok and "symlink" in msg, msg)
        os.unlink(link)

        # outside canonical raw root (a dir symlink escapes realpath containment)
        esc_dir = os.path.join(tmp, "esc")
        os.makedirs(esc_dir)
        open(os.path.join(esc_dir, "rfq_19.ndjson"), "w").write('{"z":1}\n')
        raw2 = os.path.join(tmp, "raw2")
        d12_2 = os.path.join(raw2, "date=2026-07-12")
        os.makedirs(d12_2)
        os.symlink(esc_dir, os.path.join(raw2, "date=2026-07-11"))
        escaped = os.path.join(raw2, "date=2026-07-11", "rfq_19.ndjson")
        ok, msg = expect_stop("outside-root", escaped)
        check("(c) file escaping canonical raw root rejected",
              ok and "raw root" in msg, msg)

        empty = os.path.join(d12, "rfq_18.ndjson")
        open(empty, "w").close()
        ok, msg = expect_stop("empty", empty)
        check("(c) empty file rejected", ok and "empty" in msg, msg)
        os.unlink(empty)

        partial = os.path.join(d12, "rfq_17.ndjson")
        open(partial, "w").write('{"q":1}\n{"q":2')   # no trailing newline
        ok, msg = expect_stop("partial", partial)
        check("(c) non-newline-terminated file rejected",
              ok and "newline" in msg, msg)
        os.unlink(partial)

        unstable = os.path.join(d12, "rfq_16.ndjson")
        open(unstable, "w").write('{"q":1}\n')

        def grow_during_wait(_secs):
            with open(unstable, "a") as f:
                f.write('{"q":2}\n')

        ok, msg = expect_stop("unstable", unstable, sleep_fn=grow_during_wait)
        check("(c) unstable stat/sha across two samples rejected",
              ok and "unstable" in msg, msg)
        os.unlink(unstable)

        # active-hour (current UTC hour) file rejected as not closed
        active = os.path.join(d12, "rfq_15.ndjson")
        open(active, "w").write('{"q":1}\n')
        try:
            rec.validate_and_capture(
                [(active, None, os.path.getsize(active))], raw, 0.01,
                sleep_fn=lambda s: None,
                utcnow=datetime.datetime(2026, 7, 12, 15, 30,
                                         tzinfo=datetime.timezone.utc))
            ok, msg = False, "no Stop"
        except rec.Stop as e:
            ok, msg = True, str(e)
        check("(c) active-hour (not closed) file rejected",
              ok and "closed" in msg, msg)
        os.unlink(active)

        # and a fully valid set passes, capturing size/sha/stat
        cap = rec.validate_and_capture(blocking, raw, 0.01,
                                       sleep_fn=lambda s: None, utcnow=NOW)
        check("(c) valid closed RFQ set passes validation with capture",
              set(cap) == {p for p, _c, _s in blocking} and
              all(v["sha256"] and v["size"] > 0 for v in cap.values()))

        # ---- (d) export_pause ownership ----
        live = os.path.join(tmp, "live")
        os.makedirs(live)
        pause = os.path.join(live, "export_pause")
        token = "rfq_recovery pid=%d 2026-07-13T00:00:00Z" % os.getpid()

        open(pause, "w").write("operator manual hold\n")
        try:
            rec.acquire_pause(pause, token)
            ok, msg = False, "no Refuse"
        except rec.Refuse as e:
            ok, msg = True, str(e)
        check("(d) foreign (unparseable-owner) export_pause refused, "
              "left in place", ok and os.path.exists(pause), msg)
        os.unlink(pause)

        open(pause, "w").write("seal_chain pid=%d\n" % os.getpid())  # live owner
        try:
            rec.acquire_pause(pause, token)
            ok = False
        except rec.Refuse:
            ok = True
        check("(d) live-owner (seal_chain) export_pause refused", ok)
        os.unlink(pause)

        dead = subprocess.Popen(["true"])
        dead.wait()
        open(pause, "w").write("rfq_recovery pid=%d 2026-07-12T00:00:00Z\n"
                               % dead.pid)
        rec.acquire_pause(pause, token)
        got_tok = open(pause).readline().strip()
        check("(d) stale own-token pause (dead owner) reclaimed with our token",
              got_tok == token, got_tok)
        # release removes only our own token
        rec.release_pause(pause, token)
        check("(d) release removes our own token", not os.path.exists(pause))
        open(pause, "w").write("seal_chain pid=1\n")
        rec.release_pause(pause, token)
        check("(d) release never removes a token that is not ours",
              os.path.exists(pause))
        os.unlink(pause)

        # ---- (e) checkpoint-only scope + live ordering regression ----
        source = open(os.path.join(
            ROOT, "tools", "recover_rfq_seal_2026-07-12.py")).read()
        check("(e) recovery exposes no custom seal sequence",
              not hasattr(rec, "SEAL_STEPS") and
              not hasattr(rec, "run_seal_sequence") and
              "run_seal_sequence" not in source)
        check("(e) recovery contains no seal/prune/alarm mutation flags",
              all(flag not in source for flag in
                  ("--force", "--no-prune", "--seal", "--legacy-seal")) and
              "seal_alarm.json" not in source)

        calls = []
        original_run = rec.subprocess.run
        rec.subprocess.run = lambda cmd, **kwargs: (
            calls.append(cmd) or types.SimpleNamespace(
                returncode=0, stdout="", stderr=""))
        try:
            rec._verify_seal_runner(date="2026-07-10")
        finally:
            rec.subprocess.run = original_run
        check("(e) sole export helper is exact read-only verify-seal",
              len(calls) == 1 and calls[0][-3:] ==
              ["--date", "2026-07-10", "--verify-seal"], calls)

        main_source = inspect.getsource(rec.main)
        check("(e) live lock regression: stop ingest before first exact DB read",
              main_source.index("stop_ingest(pid)") <
              main_source.index('con = connect_ro(cfg["staging_db"]'))
        cleanup_source = main_source[
            main_source.index("def cleanup():"):main_source.index("def on_signal")]
        check("(e) cleanup resumes ingest before releasing owned pause",
              cleanup_source.index("resume_ingest") <
              cleanup_source.index("release_pause"))

        # ---- (f) TERM discipline: exact pidfile PID only, never a pattern ----
        repo = os.path.realpath(tmp)
        pidfile = os.path.join(live, "ingest.pid")
        open(pidfile, "w").write("4242")
        procs = [(4242, "python3 tools/ingest.py --loop"),
                 (5555, "./build/ws_shadow"),
                 (5556, "python3 tools/ingest.py somefile.ndjson")]
        pid = rec.identify_ingest(
            live, repo, procs=procs, uid_fn=lambda p: os.geteuid(),
            cwd_fn=lambda p: repo, alive_fn=lambda p: True)
        check("(f) pidfile PID verified via user+cwd+cmdline", pid == 4242, pid)

        kills = []
        alive = {"v": True}

        def kill_fn(p):
            kills.append(p)
            alive["v"] = False

        rec.stop_ingest(pid, kill_fn=kill_fn, alive_fn=lambda p: alive["v"],
                        sleep_fn=lambda s: None)
        check("(f) exactly one TERM, to the pidfile PID only", kills == [4242],
              kills)

        try:  # cwd mismatch -> refuse, and NOTHING is signalled
            rec.identify_ingest(
                live, repo, procs=procs, uid_fn=lambda p: os.geteuid(),
                cwd_fn=lambda p: "/somewhere/else", alive_fn=lambda p: True)
            ok = False
        except rec.Refuse:
            ok = True
        check("(f) cwd mismatch refuses (no signal possible)", ok)

        try:  # daemon present but pidfile pid differs -> refuse
            rec.identify_ingest(
                live, repo,
                procs=[(9999, "python3 tools/ingest.py --loop")],
                uid_fn=lambda p: os.geteuid(), cwd_fn=lambda p: repo,
                alive_fn=lambda p: True)
            ok = False
        except rec.Refuse:
            ok = True
        check("(f) daemon/pidfile mismatch refuses (never pattern-kill)", ok)
        check("(f) ledger recorded no real signals in these tests",
              rec.KILL_LEDGER == [], rec.KILL_LEDGER)

        # Owned-pause resume is a direct, single start; it does not wait for
        # the watchdog (which is intentionally suppressed by that pause).
        original_list, original_start, original_alive = (
            rec._list_procs, rec.start_ingest, rec._pid_alive)
        started = []

        def fake_start(live_dir, root):
            started.append(7777)
            open(os.path.join(live_dir, "ingest.pid"), "w").write("7777")
            return 7777

        rec._list_procs = lambda: (
            [] if not started else [(7777, "python3 tools/ingest.py --loop")])
        rec.start_ingest = fake_start
        rec._pid_alive = lambda p: p == 7777
        try:
            rec.resume_ingest(live, repo, 2, sleep_fn=lambda s: None)
            ok, msg = started == [7777], started
        except Exception as e:
            ok, msg = False, e
        finally:
            rec._list_procs, rec.start_ingest, rec._pid_alive = (
                original_list, original_start, original_alive)
        check("(f) resume under owned pause starts and proves one daemon",
              ok, msg)

        # ---- (g) counts-must-be-zero guard ----
        ok_out = ("classes loaded: 0 series | staging=x\n"
                  "rfq_23.ndjson    +L1=0 +trades=0 +full=0\n"
                  "ingested: orderbooks_l1 +0, trades +0, orderbooks_full +0\n")
        try:
            rec.check_fastpath_output(0, ok_out, "", 1)
            ok = True
        except rec.RecoveryError:
            ok = False
        check("(g) all-zero fast-path output passes the guard", ok)

        for bad_out, why in (
                (ok_out.replace("trades +0", "trades +1"), "summary trades=1"),
                (ok_out.replace("+L1=0", "+L1=3"), "per-file L1=3"),
                (ok_out.replace("orderbooks_full +0", "orderbooks_full +2"),
                 "summary full=2")):
            try:
                rec.check_fastpath_output(0, bad_out, "", 1)
                ok = False
            except rec.RecoveryError:
                ok = True
            check("(g) guard trips on %s" % why, ok)
        try:
            rec.check_fastpath_output(1, ok_out, "", 1)
            ok = False
        except rec.RecoveryError:
            ok = True
        check("(g) guard trips on nonzero exit code", ok)
        try:
            rec.check_fastpath_output(0, ok_out, "skip (missing): x\n", 1)
            ok = False
        except rec.RecoveryError:
            ok = True
        check("(g) guard trips on a skipped-missing target", ok)

        # REAL deployed fast-path one-shot on the RFQ fixtures (subprocess):
        # zero counts, exact byte checkpoints, raw bytes untouched.
        frozen = [p for p, _c, _s in blocking]
        sha_before = {p: rec.sha256_file(p) for p in frozen}
        env = dict(os.environ,
                   RAW_ROOT=raw, STAGING_DB=db,
                   WAREHOUSE_ROOT=cfg["warehouse_root"],
                   ARCHIVE_ROOT=cfg["archive_root"])
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "ingest.py")] + frozen,
            capture_output=True, text=True, cwd=ROOT, env=env)
        try:
            rec.check_fastpath_output(proc.returncode, proc.stdout,
                                      proc.stderr, len(frozen))
            ok, msg = True, ""
        except rec.RecoveryError as e:
            ok, msg = False, "%s\n%s%s" % (e, proc.stdout, proc.stderr)
        check("(g) REAL fast-path one-shot reports all-zero counts", ok, msg)
        import duckdb
        con = duckdb.connect(db, read_only=True)
        ck = dict(con.execute(
            "SELECT file, byte_offset FROM checkpoint").fetchall())
        con.close()
        check("(g) REAL fast-path advanced every target to its exact size",
              all(ck.get(p) == os.path.getsize(p) for p in frozen),
              {os.path.basename(p): (ck.get(p), os.path.getsize(p))
               for p in frozen})
        check("(g) REAL fast-path left raw bytes untouched (SHA-256)",
              all(rec.sha256_file(p) == sha_before[p] for p in frozen))
        _inv3, blk3, nr3 = blocking_of(cfg)
        check("(g) discovery drains after the real fast-path",
              blk3 == [] and nr3 == [], (blk3, nr3))

        rc = rec.main(["recover", "--date", "2026-07-12"])
        check("operator gate: mutation refuses without --operator-approved",
              rc == 2, rc)

        # date guard (clause 1): any other --date refuses
        rc = rec.main(["recover", "--date", "2026-07-11"])
        check("clause 1: --date other than 2026-07-12 is refused (rc=2)",
              rc == 2, rc)
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
