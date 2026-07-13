#!/usr/bin/env python3
"""ONE-TIME bounded 2026-07-12 RFQ seal recovery (W05 ADDENDUM 8, verbatim
archived in docs/plan_releases/pipeline/PIPE-W05-SPEC-2026-07-12.md).

Single-purpose, operator-approval-gated. This script supersedes the
"no manual kill / no manual backfill" restriction ONLY for this bounded
window, exactly as the override authorizes. It is presented to the operator
COMPLETE before any execution; run modes:

    python3 tools/recover_seal_20260712.py --discover-only
        read-only: derive + validate the blocking set, print the exact
        target-file inventory and any blocker. Touches nothing.
    python3 tools/recover_seal_20260712.py --operator-approved
        the full bounded recovery (clauses 1-18). REFUSES without the flag.

Clause map (addendum numbering):
  1-5   discovery: seal_raw_files("2026-07-12") x staging checkpoint offsets
        (read-only duckdb, bounded lock retry); every behind file must match
        the RFQ families EXACTLY (regex identity-checked against
        ingest.RFQ_FASTPATH_RE); ordinary non-symlink file inside the
        canonical raw root, non-empty, newline-terminated, stable in
        inode/size/mtime/ctime/SHA-256 across two samples; any non-RFQ
        behind file, set change during preparation, or invalid existing
        seal => STOP with the exact blocker (exit 2).
  6-11  safety: single-instance lock dir; unique export_pause ownership
        token "recover_seal_20260712 pid=<pid> nonce=<hex>"; refuse foreign
        pause / other export_day or recovery processes (read-only proc
        scan); ingest PID verified via pidfile + /proc uid + cwd + full
        cmdline before ONE os.kill(pid, SIGTERM) — never pkill/pattern/
        SIGKILL; capture (firehose ws_shadow / targeted-L2 / RFQ) is never
        signalled; EXIT/INT/TERM cleanup resumes ingest (exact supervisor
        start_ingest command line) and removes only OUR pause token;
        stale-token reclaim only after proving the recorded owner is dead;
        a refused TERM prints the exact operator invocation and exits 3.
  12-15 targeted checkpoint: one-shot `python3 tools/ingest.py <frozen
        explicit absolute file list>` (deployed RFQ fast-path; no glob, no
        backlog scan); REQUIRE "ingested: orderbooks_l1 +0, trades +0,
        orderbooks_full +0"; prove per-target checkpoint == exact file size
        and raw SHA-256/stat identity unchanged; prove facts tree,
        ingest_stats, staging schema, all NON-target checkpoints and the
        seal inventory unchanged. Failure leaves the day unsealed (partial
        checkpoint progress is not rolled back).
  16-17 seal: while OUR pause is held, only export_day.py --check-caught-up
        / --force --no-prune / --seal / --verify-seal; no prune, legacy
        seal, invalidation or overwrite; seal_alarm cleared ONLY after
        exact verify-seal success.
  18    cleanup proofs: exactly one ingest daemon; the pre-recovery capture
        listener PIDs are still present and unsignalled; firehose/L2/RFQ
        raw keep growing; no active capture alarm.
  19-20 are the already-authorized W05 publication + Mac acceptance and are
        NOT executed here — on success this script prints the exact next
        commands and stops.

Exit codes: 0 success (sealed + verified + cleanup proofs) or verified
already-sealed; 2 BLOCKED (exact blocker printed, production untouched or
safely cleaned up); 3 TERM refused by the environment (exact operator
invocation printed); 4 cleanup/post-proof failure (loud).

Env overrides exist ONLY for offline fixtures (documented next to each
default); production runs use the defaults. stdlib + duckdb.
"""
import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import signal
import stat as stat_mod
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import warehouse_common as wc  # noqa: E402
import ingest as ingest_mod    # noqa: E402  (regex identity, clause 3)

TARGET_DATE = "2026-07-12"     # clause 1: exactly this date
# clause 3: EXACT RFQ families; identity with the deployed fast-path is
# asserted at startup and in tests — never a second, drifting copy.
RFQ_RE = ingest_mod.RFQ_FASTPATH_RE
_EXPECTED_RFQ_PATTERN = r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$"

TOKEN_NAME = "recover_seal_20260712"


def _env(name, default):
    return os.environ.get(name) or default


def proc_root():
    return _env("RECOVER_PROC_ROOT", "/proc")          # fixture override


def expect_user():
    return _env("RECOVER_EXPECT_USER", "ubuntu")       # fixture override


def expect_cwd():
    return _env("RECOVER_EXPECT_CWD", "/home/ubuntu/hft-bot")


def live_dir():
    return _env("RECOVER_LIVE_DIR", os.path.join(wc.ROOT, "work", "live"))


def export_cmd():
    """The ONLY seal commands (clause 16). Test fixtures may substitute a
    stub via RECOVER_EXPORT_CMD; production uses tools/export_day.py."""
    c = os.environ.get("RECOVER_EXPORT_CMD")
    return c.split() if c else [sys.executable, "tools/export_day.py"]


def ingest_cmd():
    c = os.environ.get("RECOVER_INGEST_CMD")
    return c.split() if c else [sys.executable, "tools/ingest.py"]


def stability_secs():
    return float(_env("RECOVER_STABILITY_SECS", "3"))


def term_timeout():
    return float(_env("RECOVER_TERM_TIMEOUT", "180"))


def growth_timeout():
    return float(_env("RECOVER_GROWTH_TIMEOUT", "120"))


class Blocked(Exception):
    """STOP with the exact blocker (clause 5 and friends)."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# read-only staging access (bounded writer-lock retry)
# ---------------------------------------------------------------------------

def staging_read(cfg, sql, args=()):
    """Read-only duckdb query with bounded retry: the ingest daemon opens
    and closes the writer once per cycle, so contention is transient. Never
    opens a writer; never waits unboundedly."""
    import duckdb
    last = None
    for _ in range(int(_env("RECOVER_DB_RETRIES", "60"))):
        try:
            con = duckdb.connect(cfg["staging_db"], read_only=True)
            try:
                con.execute("SET memory_limit='2GB'")
                return con.execute(sql, list(args)).fetchall()
            finally:
                con.close()
        except Exception as e:                      # lock contention path
            last = e
            if "lock" not in str(e).lower() and \
                    "Conflicting" not in str(e):
                raise
            time.sleep(float(_env("RECOVER_DB_RETRY_SECS", "2")))
    raise Blocked("staging writer lock never released for a read-only "
                  "checkpoint query: %s" % last)


def checkpoint_map(cfg):
    return {r[0]: r[1] for r in
            staging_read(cfg, "SELECT file, byte_offset FROM checkpoint")}


# ---------------------------------------------------------------------------
# clauses 1-5: discovery + strict validation
# ---------------------------------------------------------------------------

def sample_file(path):
    """One clause-4 sample: lstat identity + SHA-256."""
    st = os.lstat(path)
    if not stat_mod.S_ISREG(st.st_mode):
        raise Blocked("BLOCKER: %s is not an ordinary file (mode %o)"
                      % (path, st.st_mode))
    return {"inode": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "sha256": sha256_file(path)}


def validate_target(path, raw_root):
    """Clause 3+4 static checks for ONE behind file (pre-sampling)."""
    base = os.path.basename(path)
    if not RFQ_RE.match(base):
        raise Blocked("BLOCKER: behind file %s is NOT an RFQ-family file "
                      "(firehose/L1/trades/L2/unknown files must never be "
                      "recovered by this script)" % path)
    st = os.lstat(path)
    if stat_mod.S_ISLNK(st.st_mode) or not stat_mod.S_ISREG(st.st_mode):
        raise Blocked("BLOCKER: %s is a symlink or non-regular file" % path)
    real, root = os.path.realpath(path), os.path.realpath(raw_root)
    if not real.startswith(root + os.sep):
        raise Blocked("BLOCKER: %s resolves outside the canonical raw root "
                      "%s" % (path, raw_root))
    if st.st_size <= 0:
        raise Blocked("BLOCKER: %s is empty" % path)
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            raise Blocked("BLOCKER: %s is not newline-terminated (still "
                          "being written?)" % path)


def discover_behind(cfg):
    """Clause 2: mechanically derive the blocking set — seal inventory x
    checkpoint offsets. Returns (behind, all_targets)."""
    targets = wc.seal_raw_files(cfg["raw_root"], TARGET_DATE,
                                warehouse_root=cfg["warehouse_root"])
    cps = checkpoint_map(cfg)
    behind = []
    for p in targets:
        ap = os.path.abspath(p)
        if cps.get(ap) != os.path.getsize(ap):
            behind.append(ap)
    return sorted(behind), sorted(os.path.abspath(p) for p in targets)


def discover_and_validate(cfg):
    """Clauses 1-5. Returns (behind, samples) with per-file stability
    proven across two samples."""
    if RFQ_RE.pattern != _EXPECTED_RFQ_PATTERN:
        raise Blocked("BLOCKER: deployed ingest.RFQ_FASTPATH_RE %r drifted "
                      "from the reviewed pattern %r — re-review before "
                      "recovery" % (RFQ_RE.pattern, _EXPECTED_RFQ_PATTERN))
    for prior in ("2026-07-10", "2026-07-11"):
        sp = wc.seal_path(cfg["warehouse_root"], prior)
        if os.path.isfile(sp) and not wc.day_sealed(cfg["warehouse_root"],
                                                    prior):
            raise Blocked("BLOCKER: existing seal for %s is invalid "
                          "(not a well-formed SEALED v2 seal) — operator "
                          "remediation, no recovery" % prior)
    behind, _targets = discover_behind(cfg)
    for p in behind:
        validate_target(p, cfg["raw_root"])
    s1 = {p: sample_file(p) for p in behind}
    time.sleep(stability_secs())
    s2 = {p: sample_file(p) for p in behind}
    if s1 != s2:
        drift = [p for p in s1 if s1[p] != s2.get(p)]
        raise Blocked("BLOCKER: candidate set unstable across two samples "
                      "(%s) — files still changing; not a closed backlog"
                      % ", ".join(drift[:4]))
    behind2, _ = discover_behind(cfg)
    if behind2 != behind:
        raise Blocked("BLOCKER: candidate set changed during preparation "
                      "(was %d file(s), now %d)"
                      % (len(behind), len(behind2)))
    return behind, s1


# ---------------------------------------------------------------------------
# clauses 6-11: single-instance lock, pause token, verified TERM
# ---------------------------------------------------------------------------

def read_pause(pause):
    try:
        with open(pause) as f:
            return f.read().strip()
    except OSError:
        return None


def owner_pid(token):
    m = re.search(r"pid=(\d+)", token or "")
    return int(m.group(1)) if m else None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_running(pid):
    """Zombie-aware liveness: a Z-state process has already exited (its
    parent just hasn't reaped it) — for the TERM wait that counts as
    stopped. Falls back to kill(pid, 0) where procfs is unavailable."""
    stat_p = os.path.join(proc_root(), str(pid), "stat")
    if os.path.isfile(stat_p):
        try:
            with open(stat_p) as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except (OSError, IndexError):
            pass
    return pid_alive(pid)


def scan_processes(needles, exclude_pid):
    """Read-only full-cmdline scan under proc_root(). Returns matches
    [(pid, cmdline)]. Never signals anything."""
    hits = []
    root = proc_root()
    if not os.path.isdir(root):
        return hits
    for entry in os.listdir(root):
        if not entry.isdigit() or int(entry) == exclude_pid:
            continue
        try:
            with open(os.path.join(root, entry, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
        except OSError:
            continue
        if any(n in cmd for n in needles):
            hits.append((int(entry), cmd))
    return hits


def verify_ingest_pid(pidfile):
    """Clause 8: pidfile + /proc uid + cwd + exact full command line."""
    if not os.path.isfile(pidfile):
        return None  # no daemon: nothing to stop
    raw = open(pidfile).read().strip()
    if not raw.isdigit():
        raise Blocked("BLOCKER: ingest pidfile %s is corrupt (%r)"
                      % (pidfile, raw))
    pid = int(raw)
    if not pid_alive(pid):
        return None  # dead pidfile: nothing to stop (cleanup will restart)
    pdir = os.path.join(proc_root(), str(pid))
    try:
        st = os.stat(pdir)
        want_uid = pwd.getpwnam(expect_user()).pw_uid
        if st.st_uid != want_uid:
            raise Blocked("BLOCKER: pid %d is owned by uid %d, not %s — "
                          "refusing to signal it"
                          % (pid, st.st_uid, expect_user()))
        cwd = os.path.realpath(os.path.join(pdir, "cwd"))
        if cwd != os.path.realpath(expect_cwd()):
            raise Blocked("BLOCKER: pid %d cwd %s != %s — refusing to "
                          "signal it" % (pid, cwd, expect_cwd()))
        with open(os.path.join(pdir, "cmdline"), "rb") as f:
            argv = [a.decode("utf-8", "replace")
                    for a in f.read().split(b"\0") if a]
    except OSError as e:
        raise Blocked("BLOCKER: cannot verify pid %d via %s (%s) — "
                      "refusing to signal an unverified process"
                      % (pid, pdir, e))
    joined = " ".join(argv)
    if "tools/ingest.py --loop" not in joined:
        raise Blocked("BLOCKER: pid %d command line %r is not the ingest "
                      "daemon (tools/ingest.py --loop) — refusing to "
                      "signal it" % (pid, joined))
    return pid


def term_verified_pid(pid):
    """Clause 8+11: ONE SIGTERM to the verified pid, wait up to 180s."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (PermissionError, OSError) as e:
        print("=" * 68)
        print("TERM REFUSED by the environment (%s)." % e)
        print("Clause 11: no improvisation. Operator invocation:")
        print("  ssh ubuntu@3.130.232.109 'kill -TERM %d'" % pid)
        print("then re-run:")
        print("  python3 tools/recover_seal_20260712.py --operator-approved")
        print("=" * 68)
        raise SystemExit(3)
    deadline = time.time() + term_timeout()
    while time.time() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.5)
    raise Blocked("BLOCKER: ingest pid %d did not exit within %ds after "
                  "SIGTERM (no SIGKILL will be sent)"
                  % (pid, int(term_timeout())))


def start_ingest_exact():
    """Clause 10: resume ingest with the EXACT supervisor start_ingest
    command line (pipeline_supervisor.sh):
        python3 tools/ingest.py --loop >> "$LIVE/ingest.log" 2>&1 &
        echo $! > "$LIVE/ingest.pid"
    """
    live = live_dir()
    line = ('%s --loop >> "%s" 2>&1 & echo $! > "%s"'
            % (" ".join(ingest_cmd()),
               os.path.join(live, "ingest.log"),
               os.path.join(live, "ingest.pid")))
    subprocess.run(["bash", "-c", line], cwd=wc.ROOT, check=True)


# ---------------------------------------------------------------------------
# clauses 12-15: frozen one-shot + invariance proofs
# ---------------------------------------------------------------------------

ONE_SHOT_RE = re.compile(
    r"^ingested: orderbooks_l1 \+(\d+), trades \+(\d+), "
    r"orderbooks_full \+(\d+)\s*$", re.M)


def facts_fingerprint(cfg):
    out = []
    root = cfg["archive_root"]
    for base, _d, files in os.walk(root):
        for fn in sorted(files):
            p = os.path.join(base, fn)
            st = os.lstat(p)
            out.append((os.path.relpath(p, root), st.st_size,
                        st.st_mtime_ns, st.st_ino))
    return sorted(out)


def seals_fingerprint(cfg):
    d = os.path.join(cfg["warehouse_root"], "seals")
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            out.append((fn, os.path.getsize(p), sha256_file(p)))
    return out


def staging_fingerprint(cfg):
    stats = staging_read(cfg, "SELECT * FROM ingest_stats ORDER BY day, "
                              "category")
    schema = staging_read(
        cfg, "SELECT table_name, column_name, data_type FROM "
             "information_schema.columns ORDER BY table_name, column_name")
    return {"ingest_stats": stats, "schema": schema}


def run_one_shot(cfg, frozen_targets):
    """Clause 12+13: explicit frozen absolute file array; require the exact
    zero-facts summary line."""
    r = subprocess.run(ingest_cmd() + list(frozen_targets), cwd=wc.ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Blocked("BLOCKER: one-shot checkpoint run failed rc=%d: %s"
                      % (r.returncode, (r.stderr or r.stdout)[-400:]))
    m = ONE_SHOT_RE.search(r.stdout)
    if not m:
        raise Blocked("BLOCKER: one-shot output missing the summary line; "
                      "stdout tail: %s" % r.stdout[-400:])
    if m.groups() != ("0", "0", "0"):
        raise Blocked("BLOCKER: one-shot materialized facts rows (%s) — "
                      "RFQ fast-path must checkpoint bytes only; day left "
                      "unsealed" % (m.group(0)))
    return r.stdout


# ---------------------------------------------------------------------------
# clauses 16-17: seal, exactly four export_day invocations
# ---------------------------------------------------------------------------

def run_seal_chain(cfg):
    for extra in (["--check-caught-up"], ["--force", "--no-prune"],
                  ["--seal"], ["--verify-seal"]):
        cmd = export_cmd() + ["--date", TARGET_DATE] + extra
        r = subprocess.run(cmd, cwd=wc.ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            raise Blocked("BLOCKER: %s failed rc=%d: %s"
                          % (" ".join(cmd), r.returncode,
                             (r.stderr or r.stdout)[-400:]))
    alarm = os.path.join(live_dir(), "seal_alarm.json")
    if os.path.isfile(alarm):        # clause 17: only after verify-seal OK
        os.remove(alarm)
        print("[recover] seal_alarm cleared (verify-seal succeeded)")


# ---------------------------------------------------------------------------
# clause 18: cleanup proofs
# ---------------------------------------------------------------------------

def newest(dirpath, prefix):
    cands = []
    if os.path.isdir(dirpath):
        for fn in os.listdir(dirpath):
            if fn.startswith(prefix):
                p = os.path.join(dirpath, fn)
                cands.append((os.lstat(p).st_mtime_ns, p))
    return max(cands)[1] if cands else None


def prove_capture_growth(cfg):
    """firehose/L2/RFQ raw files continue growing (newest file per family
    grows, or a newer file appears, within the bounded window)."""
    families = ("firehose_", "l2_", "rfq_")
    day_dirs = sorted(
        p for p in (os.path.join(cfg["raw_root"], d)
                    for d in os.listdir(cfg["raw_root"]))
        if os.path.isdir(p))
    if not day_dirs:
        raise Blocked("cleanup proof failed: no raw day dirs at all")
    latest_dir = day_dirs[-1]
    base = {}
    for fam in families:
        p = newest(latest_dir, fam)
        if p is None:
            raise Blocked("cleanup proof failed: no %s* raw files in %s"
                          % (fam, latest_dir))
        base[fam] = (p, os.path.getsize(p))
    deadline = time.time() + growth_timeout()
    pending = set(families)
    while pending and time.time() < deadline:
        time.sleep(min(2.0, growth_timeout() / 10))
        for fam in sorted(pending):
            p0, s0 = base[fam]
            pn = newest(latest_dir, fam)
            if (pn != p0) or (os.path.getsize(p0) > s0):
                pending.discard(fam)
    if pending:
        raise Blocked("cleanup proof failed: raw families not growing "
                      "within %ds: %s"
                      % (int(growth_timeout()), ", ".join(sorted(pending))))


def prove_cleanup(cfg, pre_listeners):
    live = live_dir()
    pidfile = os.path.join(live, "ingest.pid")
    if not os.path.isfile(pidfile):
        raise Blocked("cleanup proof failed: ingest pidfile missing after "
                      "resume")
    pid = int(open(pidfile).read().strip())
    if not pid_alive(pid):
        raise Blocked("cleanup proof failed: resumed ingest pid %d not "
                      "alive" % pid)
    daemons = scan_processes(["tools/ingest.py --loop"], exclude_pid=-1)
    if len(daemons) != 1:
        raise Blocked("cleanup proof failed: expected exactly one ingest "
                      "daemon, found %d: %s" % (len(daemons), daemons))
    for lpid, lcmd in pre_listeners:
        if not pid_alive(lpid):
            raise Blocked("cleanup proof failed: capture listener pid %d "
                          "(%s) disappeared during recovery" % (lpid, lcmd))
    prove_capture_growth(cfg)
    alert = os.path.join(live, "capture_alert.json")
    if os.path.isfile(alert):
        with open(alert) as f:
            a = json.load(f)
        if a.get("status") == "gap":
            raise Blocked("cleanup proof failed: capture alarm is ACTIVE "
                          "(%s)" % a)


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------

def print_inventory(behind, samples, cps):
    print("TARGET-FILE INVENTORY (mechanically derived, clause 2):")
    if not behind:
        print("  (empty — nothing is behind; only the seal chain is needed)")
    for p in behind:
        s = samples.get(p, {})
        print("  %s  size=%d  checkpoint=%s  sha256=%s"
              % (p, s.get("size", -1), cps.get(p), s.get("sha256", "?")))


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--discover-only", action="store_true",
                   help="read-only discovery + validation; print the "
                        "target inventory and any blocker; touch nothing")
    g.add_argument("--operator-approved", action="store_true",
                   help="the operator has seen THIS COMPLETE SCRIPT and "
                        "approved this one bounded run")
    args = ap.parse_args(argv[1:])
    cfg = wc.load_config()
    live = live_dir()
    os.makedirs(live, exist_ok=True)
    pause = os.path.join(live, "export_pause")
    lock = os.path.join(live, "%s.lock" % TOKEN_NAME)

    try:
        # ---- existing 07-12 seal? verify, never reseal (clause 5) ---------
        sp = wc.seal_path(cfg["warehouse_root"], TARGET_DATE)
        if os.path.isfile(sp):
            if not wc.day_sealed(cfg["warehouse_root"], TARGET_DATE):
                raise Blocked("BLOCKER: a %s seal EXISTS but is invalid — "
                              "operator remediation "
                              "(--operator-invalidate-seal path), this "
                              "script never overwrites" % TARGET_DATE)
            if args.discover_only:
                print("ALREADY_SEALED: %s seal present and well-formed; "
                      "--operator-approved would verify it and stop"
                      % TARGET_DATE)
                return 0
            cmd = export_cmd() + ["--date", TARGET_DATE, "--verify-seal"]
            r = subprocess.run(cmd, cwd=wc.ROOT, capture_output=True,
                               text=True)
            if r.returncode != 0:
                raise Blocked("BLOCKER: existing %s seal fails "
                              "verify-seal: %s"
                              % (TARGET_DATE, (r.stderr or r.stdout)[-300:]))
            print("ALREADY_SEALED_VERIFIED: %s — nothing to recover"
                  % TARGET_DATE)
            return 0

        # ---- discovery (read-only, clauses 1-5) ---------------------------
        behind, samples = discover_and_validate(cfg)
        print_inventory(behind, samples, checkpoint_map(cfg))
        if args.discover_only:
            print("[discover-only] no lock taken, nothing signalled, "
                  "nothing written")
            return 0

        # ---- safety gates (clauses 6-7) ------------------------------------
        others = scan_processes(
            ["export_day.py", TOKEN_NAME + ".py"], exclude_pid=os.getpid())
        others = [(p, c) for (p, c) in others
                  if "--discover-only" not in c]
        if others:
            raise Blocked("BLOCKER: another seal/recovery process is "
                          "running: %s" % others[:2])
        tok = read_pause(pause)
        if tok is not None:
            opid = owner_pid(tok)
            if tok.startswith(TOKEN_NAME) and opid and not pid_alive(opid):
                print("[recover] reclaiming OUR stale pause token "
                      "(dead pid=%d) — loudly, clause 10" % opid)
                os.remove(pause)
            else:
                raise Blocked("BLOCKER: foreign export_pause present (%r) "
                              "— never touched, recovery refused" % tok)
        try:
            os.mkdir(lock)
        except FileExistsError:
            owner = None
            meta = os.path.join(lock, "owner.json")
            try:
                with open(meta) as f:
                    owner = json.load(f)
            except OSError:
                pass
            if owner and not pid_alive(int(owner.get("pid", -1))):
                print("[recover] removing stale single-instance lock "
                      "(dead pid=%s)" % owner.get("pid"))
                os.remove(meta)
                os.rmdir(lock)
                os.mkdir(lock)
            else:
                raise Blocked("BLOCKER: another %s instance holds the "
                              "lock (%s)" % (TOKEN_NAME, owner))
        with open(os.path.join(lock, "owner.json"), "w") as f:
            json.dump({"pid": os.getpid(),
                       "started_utc": time.strftime(
                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)

        nonce = secrets.token_hex(8)
        my_token = "%s pid=%d nonce=%s" % (TOKEN_NAME, os.getpid(), nonce)

        # capture listeners snapshot BEFORE any signal (clause 18 proof)
        pre_listeners = scan_processes(["ws_shadow"],
                                       exclude_pid=os.getpid())

        stopped_pid = None
        resumed = False

        def cleanup():
            nonlocal resumed
            # remove ONLY our token
            if read_pause(pause) == my_token:
                os.remove(pause)
            # resume ingest only if the pidfile is empty/dead (clause 10)
            pidfile = os.path.join(live, "ingest.pid")
            alive = False
            if os.path.isfile(pidfile):
                raw = open(pidfile).read().strip()
                alive = raw.isdigit() and pid_alive(int(raw))
            if not alive and stopped_pid is not None and not resumed:
                start_ingest_exact()
                resumed = True
                print("[recover] ingest resumed via the exact supervisor "
                      "start_ingest command line")
            for p in (os.path.join(lock, "owner.json"),):
                if os.path.isfile(p):
                    os.remove(p)
            if os.path.isdir(lock):
                os.rmdir(lock)

        def on_signal(signum, _frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)

        try:
            # ---- own the pause (clause 7) ---------------------------------
            try:
                with open(pause, "x") as f:
                    f.write(my_token + "\n")
            except FileExistsError:
                raise Blocked("BLOCKER: export_pause appeared between the "
                              "foreign-pause check and ownership — another "
                              "seal window is racing; recovery refused")

            # ---- verified TERM of the ingest daemon only (clause 8) -------
            pidfile = os.path.join(live, "ingest.pid")
            pid = verify_ingest_pid(pidfile)
            if pid is not None:
                print("[recover] verified ingest daemon pid=%d — sending "
                      "ONE SIGTERM" % pid)
                term_verified_pid(pid)
                stopped_pid = pid
                if os.path.isfile(pidfile):
                    os.remove(pidfile)
            else:
                stopped_pid = -1   # nothing was running; still resume after

            # ---- pre-one-shot snapshots (clauses 14-15) --------------------
            pre_cps = checkpoint_map(cfg)
            pre_facts = facts_fingerprint(cfg)
            pre_seals = seals_fingerprint(cfg)
            pre_staging = staging_fingerprint(cfg)
            behind2, _ = discover_behind(cfg)
            if behind2 != behind:
                raise Blocked("BLOCKER: candidate set changed during "
                              "preparation (post-stop rescan differs)")
            for p in behind:
                if sample_file(p) != samples[p]:
                    raise Blocked("BLOCKER: %s changed after ingest "
                                  "stopped — not a closed backlog" % p)

            # ---- frozen one-shot (clauses 12-13) ---------------------------
            if behind:
                out = run_one_shot(cfg, behind)
                print(out.strip()[-400:])

            # ---- post-conditions (clauses 14-15) ---------------------------
            post_cps = checkpoint_map(cfg)
            for p in behind:
                size = samples[p]["size"]
                if post_cps.get(p) != size:
                    raise Blocked("BLOCKER: checkpoint for %s is %s, "
                                  "expected exact size %d — day left "
                                  "unsealed"
                                  % (p, post_cps.get(p), size))
                if sample_file(p) != samples[p]:
                    raise Blocked("BLOCKER: raw identity of %s changed "
                                  "during checkpointing — day left "
                                  "unsealed" % p)
            nt_pre = {k: v for k, v in pre_cps.items() if k not in behind}
            nt_post = {k: v for k, v in post_cps.items()
                       if k not in behind}
            if nt_pre != nt_post:
                raise Blocked("BLOCKER: NON-target checkpoints changed — "
                              "day left unsealed")
            if facts_fingerprint(cfg) != pre_facts:
                raise Blocked("BLOCKER: facts tree changed — day left "
                              "unsealed")
            if seals_fingerprint(cfg) != pre_seals:
                raise Blocked("BLOCKER: seal inventory changed — day left "
                              "unsealed")
            if staging_fingerprint(cfg) != pre_staging:
                raise Blocked("BLOCKER: ingest_stats/schema changed — day "
                              "left unsealed")

            # ---- seal (clauses 16-17), pause still owned -------------------
            run_seal_chain(cfg)
        finally:
            cleanup()

        # ---- clause 18 proofs (after resume) -------------------------------
        prove_cleanup(cfg, pre_listeners)
        seal_sha = sha256_file(sp)
        print("RECOVERY_OK: %s sealed + verified (seal sha256=%s). "
              "Clauses 19-20 (already-authorized W05 publication + Mac "
              "acceptance) are the NEXT, separate steps:\n"
              "  bash deploy/ec2_s3_sync.sh research_sync %s   # on the box"
              "\n  python3 tools/research_data.py fetch --release <id> "
              "--with-rfq   # on the Mac" % (TARGET_DATE, seal_sha,
                                             TARGET_DATE))
        return 0
    except Blocked as b:
        print(str(b), file=sys.stderr)
        print("DATA_PLANE_BLOCKED: %s" % str(b).splitlines()[0],
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
