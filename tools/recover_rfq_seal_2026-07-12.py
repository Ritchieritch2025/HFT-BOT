#!/usr/bin/env python3
"""Bounded one-time recovery: the 2026-07-12 RFQ-blocked day seal.

Operator override archived VERBATIM in
docs/plan_releases/pipeline/PIPE-W05-SPEC-2026-07-12.md ADDENDUM 8; every
numbered clause (1-20) is binding. This script implements clauses 1-18 ONLY;
19-20 (W05 acceptance/publication) run separately after the seal verifies —
see the NOTE printed on success. Single-purpose: the target date is frozen to
2026-07-12 (clause 1); nothing else is authorized.

Phases (clause map):
  DISCOVERY  (2-5)   blocking set = seal_raw_files ∩ {checkpoint != size};
                     every blocker must be RFQ-family, regular, root-contained,
                     closed, non-empty, newline-terminated, stat+SHA-256 stable
                     across two samples; existing seals must probe valid;
                     otherwise STOP with the exact blocker.
  SAFETY     (6-11)  refuse on conflicting seal/recovery process or foreign
                     export_pause; single-instance lock; own pause token
                     "rfq_recovery pid=<pid> <utc>"; ingest identified by
                     pidfile + user + cwd + full cmdline and TERMed by exact
                     PID only — never pkill/pattern/SIGKILL; capture processes
                     are never signalled; cleanup resumes ingest and removes
                     only our own token; stale-token reclaim requires a
                     provably dead recorded owner.
  CHECKPOINT (12-15) deployed RFQ fast-path one-shot (tools/ingest.py with a
                     frozen explicit file array, no glob/backlog scan);
                     reported counts must be L1=0 trades=0 orderbooks_full=0;
                     byte-identity + no-side-effect proofs; any failure leaves
                     the day unsealed.
  SEAL       (16-18) exactly the four export_day subcommands, in order;
                     seal_alarm cleared only after exact --verify-seal success;
                     cleanup proves one ingest daemon, capture listeners
                     present, firehose/L2/RFQ raw growing, no capture alarm.

Modes:
  --date 2026-07-12                  full recovery (requires operator approval)
  --date 2026-07-12 --discover-only  read-only discovery + expected target
                                     inventory; no lock, no pause, no signal,
                                     no writes of any kind.

Exit codes: 0 ok · 1 recovery step failed (day unsealed) · 2 refused to start
· 3 discovery STOP · 4 environment refused SIGTERM (clause 11) · 5 seal OK
but post-cleanup environment proof failed.  stdlib + duckdb only.
"""
import argparse
import datetime
import glob
import os
import re
import signal
import stat as stat_mod
import subprocess
import sys
import time
import types

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import warehouse_common as wc          # noqa: E402
import ingest                          # noqa: E402  (RFQ_FASTPATH_RE, connect_with_retry)
from export_day import sha256_file     # noqa: E402

TARGET_DATE = "2026-07-12"             # clause 1: this recovery only
OWNER = "rfq_recovery"                 # our export_pause token family (B4)
SEAL_STEPS = (("--check-caught-up",),  # clause 16: exactly these, in order
              ("--force", "--no-prune"),
              ("--seal",),
              ("--verify-seal",))
FACT_TABLES = ("orderbooks_l1", "trades", "orderbooks_full")
ALL_TABLES = FACT_TABLES + ("ingest_stats", "checkpoint")

# Every signal this script ever sends is recorded here; cleanup prints it.
# Structural proof of clauses 8/9: the only os.kill(SIGTERM) call site is
# _term(), so the ledger IS the complete signal history.
KILL_LEDGER = []

_HOUR_RE = re.compile(r"_(\d{2})\.ndjson(?:\.\d+)?$")
_PAUSE_OWNER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*) pid=(\d+)")
_INGEST_CMD_RE = re.compile(r"(?:^|[/\s])ingest\.py(?:\s|$)")
_EXPORT_CMD_RE = re.compile(r"(?:^|[/\s])export_day\.py(?:\s|$)")
_RECOVER_CMD_RE = re.compile(r"recover_rfq_seal_\d{4}-\d{2}-\d{2}\.py")
_FASTPATH_SUMMARY_RE = re.compile(
    r"^ingested: orderbooks_l1 \+(\d+), trades \+(\d+), orderbooks_full \+(\d+)\s*$",
    re.M)
_FASTPATH_FILE_RE = re.compile(r"\+L1=(\d+) \+trades=(\d+) \+full=(\d+)")


class Refuse(Exception):
    """Preconditions not met; nothing was touched (exit 2)."""


class Stop(Exception):
    """Discovery blocker (clause 5); exact blocker in the message (exit 3)."""


class TermRefused(Exception):
    """Environment denied SIGTERM (clause 11); do not improvise (exit 4)."""


class RecoveryError(Exception):
    """A recovery step failed; the day is left unsealed (exit 1)."""


# --------------------------------------------------------------- process probes
def _pid_alive(pid):
    """True unless the PID provably does not exist (PermissionError = alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _list_procs():
    """[(pid, cmdline)] — /proc on the deploy host, `ps` fallback elsewhere."""
    procs = []
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8",
                                                               "replace").strip()
            except OSError:
                continue
            if cmd:
                procs.append((int(entry), cmd))
        return procs
    out = subprocess.run(["ps", "-axo", "pid=,command="],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            procs.append((int(parts[0]), parts[1]))
    return procs


def _proc_uid(pid):
    if os.path.isdir("/proc"):
        try:
            return os.stat("/proc/%d" % pid).st_uid
        except OSError:
            return None
    out = subprocess.run(["ps", "-o", "uid=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def _proc_cwd(pid):
    link = "/proc/%d/cwd" % pid
    if os.path.islink(link):
        try:
            return os.path.realpath(link)
        except OSError:
            return None
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return None


def _term(pid):
    """The ONLY signal-sending call site (clauses 8/9): exact-PID SIGTERM."""
    KILL_LEDGER.append(pid)
    os.kill(pid, signal.SIGTERM)


# ------------------------------------------------------------------- staging IO
def connect_ro(staging, attempts=30, sleep_s=5.0):
    """Read-only staging connect with the deployed lock-retry discipline
    (reuses ingest.connect_with_retry verbatim via a read-only shim)."""
    import duckdb
    shim = types.SimpleNamespace(
        connect=lambda path: duckdb.connect(path, read_only=True))
    return ingest.connect_with_retry(shim, staging, attempts=attempts,
                                     sleep_s=sleep_s)


# -------------------------------------------------------- discovery (clauses 2-5)
def compute_blocking(cfg, con):
    """(inventory, blocking, non_rfq_behind); entries = (abspath, ckpt, size).

    Blocking set (clause 2) = seal_raw_files(raw_root, TARGET_DATE) ∩
    {staging checkpoint offset != current os size}. Split by the deployed
    RFQ_FASTPATH_RE (clause 3): a behind non-RFQ file is a STOP blocker."""
    inventory = [os.path.abspath(p) for p in wc.seal_raw_files(
        cfg["raw_root"], TARGET_DATE, warehouse_root=cfg["warehouse_root"])]
    ckpts = dict(con.execute(
        "SELECT file, byte_offset FROM checkpoint").fetchall())
    blocking, non_rfq = [], []
    for path in inventory:
        size = os.path.getsize(path)
        ckpt = ckpts.get(path)
        if ckpt == size:
            continue
        row = (path, ckpt, size)
        if ingest.RFQ_FASTPATH_RE.match(os.path.basename(path)):
            blocking.append(row)
        else:
            non_rfq.append(row)
    return inventory, blocking, non_rfq


def _file_hour_key(path):
    day = os.path.basename(os.path.dirname(path))
    m = _HOUR_RE.search(os.path.basename(path))
    if not day.startswith("date=") or not m:
        return None
    return (day[len("date="):], int(m.group(1)))


def _stat_key(st):
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def validate_and_capture(blocking, raw_root, wait_s, sleep_fn=time.sleep,
                         utcnow=None):
    """Clause 4 per-file validation + the pre-run identity capture (clause 14).

    Returns {path: {"size", "sha256", "stat"}} or raises Stop listing every
    failed file. Stability = identical (inode,size,mtime,ctime) AND SHA-256
    across two samples wait_s seconds apart (single sleep for the whole set)."""
    problems = []
    real_root = os.path.realpath(raw_root)
    now = utcnow or datetime.datetime.now(datetime.timezone.utc)
    now_key = (now.strftime("%Y-%m-%d"), now.hour)
    paths = [row[0] for row in blocking]
    for path in paths:
        base = os.path.basename(path)
        if not ingest.RFQ_FASTPATH_RE.match(base):
            problems.append("non-RFQ family name: %s" % path)
            continue
        if os.path.islink(path):
            problems.append("symlink (not an ordinary file): %s" % path)
            continue
        st = os.stat(path)
        if not stat_mod.S_ISREG(st.st_mode):
            problems.append("not a regular file: %s" % path)
            continue
        real = os.path.realpath(path)
        if os.path.commonpath([real_root, real]) != real_root:
            problems.append("escapes canonical raw root: %s -> %s"
                            % (path, real))
            continue
        key = _file_hour_key(path)
        if key is None or key >= now_key:
            problems.append("not a closed past hour (active/unparseable): %s"
                            % path)
            continue
        if st.st_size <= 0:
            problems.append("empty file: %s" % path)
            continue
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                problems.append("not newline-terminated: %s" % path)
    if problems:
        raise Stop("target validation failed:\n  " + "\n  ".join(problems))
    sample1 = {p: (_stat_key(os.stat(p)), sha256_file(p)) for p in paths}
    sleep_fn(wait_s)
    sample2 = {p: (_stat_key(os.stat(p)), sha256_file(p)) for p in paths}
    for path in paths:
        if sample1[path] != sample2[path]:
            problems.append("still changing (stat/sha unstable across %.1fs): %s"
                            % (wait_s, path))
    if problems:
        raise Stop("target stability failed:\n  " + "\n  ".join(problems))
    return {p: {"size": sample2[p][0][1], "sha256": sample2[p][1],
                "stat": sample2[p][0]} for p in paths}


def _export_day_runner(step_args, date=TARGET_DATE):
    cmd = [sys.executable, os.path.join(TOOLS, "export_day.py"),
           "--date", date] + list(step_args)
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=wc.ROOT)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout


def probe_existing_seals(cfg, runner=_export_day_runner):
    """Clause 5: every existing day seal must pass the deployed validity probe
    (export_day --verify-seal, read-only) or the recovery STOPs."""
    seal_glob = os.path.join(cfg["warehouse_root"], "seals",
                             "date=????-??-??.json")
    for path in sorted(glob.glob(seal_glob)):
        date = os.path.basename(path)[len("date="):-len(".json")]
        if date == TARGET_DATE:
            continue  # target handled separately (write-once pre-flight)
        if not wc.day_sealed(cfg["warehouse_root"], date):
            raise Stop("existing seal is malformed: %s" % path)
        rc, _out = runner(("--verify-seal",), date=date)
        if rc != 0:
            raise Stop("existing seal FAILED its validity probe: %s "
                       "(export_day --verify-seal rc=%d)" % (path, rc))
        print("seal probe PASS %s" % date)


def print_inventory(inventory, blocking, capture=None):
    print("raw seal inventory for %s: %d file(s)" % (TARGET_DATE, len(inventory)))
    print("EXPECTED TARGET INVENTORY (blocking RFQ files): %d file(s)"
          % len(blocking))
    for path, ckpt, size in blocking:
        line = "  %s  size=%d  checkpoint=%s" % (path, size, ckpt)
        if capture and path in capture:
            line += "  sha256=%s" % capture[path]["sha256"]
        print(line)


# ------------------------------------------------- process safety (clauses 6-11)
def find_conflicts(procs=None):
    """Clause 6: any other seal/recovery process is a refusal."""
    me = os.getpid()
    hits = []
    for pid, cmd in (procs if procs is not None else _list_procs()):
        if pid == me:
            continue
        if _EXPORT_CMD_RE.search(cmd) or _RECOVER_CMD_RE.search(cmd):
            hits.append((pid, cmd))
    return hits


def read_pause_owner(pause_path):
    """(owner, pid) from the token line, or None if unparseable (foreign)."""
    try:
        with open(pause_path, encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None
    m = _PAUSE_OWNER_RE.match(first)
    return (m.group(1), int(m.group(2))) if m else None


def acquire_pause(pause_path, token, alive_fn=_pid_alive):
    """Clauses 6/7/10: own-token pause; foreign pause refuses; a recorded
    owner is reclaimed ONLY when provably dead."""
    if os.path.exists(pause_path):
        owner = read_pause_owner(pause_path)
        if owner is None:
            raise Refuse("FOREIGN export_pause (no parseable owner — operator/"
                         "manual property): %s; left untouched" % pause_path)
        name, pid = owner
        if alive_fn(pid):
            raise Refuse("export_pause held by LIVE %s pid=%d; refusing to "
                         "start" % (name, pid))
        print("reclaiming stale export_pause (recorded owner %s pid=%d is "
              "provably dead)" % (name, pid))
        os.unlink(pause_path)
    try:
        fd = os.open(pause_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Refuse("export_pause appeared concurrently; refusing the race")
    try:
        os.write(fd, (token + "\n").encode())
    finally:
        os.close(fd)


def release_pause(pause_path, token):
    """Remove ONLY our own token (clause 10); anything else is left in place."""
    try:
        with open(pause_path, encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return
    if first == token:
        os.unlink(pause_path)
        print("export_pause released (own token)")
    else:
        print("WARN: export_pause no longer carries our token; left in place",
              file=sys.stderr)


def acquire_lock(lock_dir, alive_fn=_pid_alive):
    """Single-instance lock (clause 7): atomic mkdir + pid, dead-owner reclaim."""
    pid_path = os.path.join(lock_dir, "pid")
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        try:
            with open(pid_path) as f:
                owner = int(f.read().strip())
        except (OSError, ValueError):
            owner = None
        if owner is not None and alive_fn(owner):
            raise Refuse("another recovery instance holds %s (pid=%d)"
                         % (lock_dir, owner))
        print("reclaiming stale recovery lock (owner %s dead)" % owner)
        os.unlink(pid_path)
        os.rmdir(lock_dir)
        os.mkdir(lock_dir)
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))


def release_lock(lock_dir):
    try:
        os.unlink(os.path.join(lock_dir, "pid"))
        os.rmdir(lock_dir)
    except OSError:
        pass


def _ingest_daemons(procs, root, cwd_fn=_proc_cwd):
    """PIDs whose full cmdline is an ingest --loop daemon (cwd-filtered to the
    repo when the platform can prove cwd)."""
    out = []
    for pid, cmd in procs:
        if not (_INGEST_CMD_RE.search(cmd) and "--loop" in cmd):
            continue
        cwd = cwd_fn(pid)
        if cwd is not None and cwd != os.path.realpath(root):
            continue
        out.append(pid)
    return out


def identify_ingest(live_dir, root, procs=None, uid_fn=_proc_uid,
                    cwd_fn=_proc_cwd, alive_fn=_pid_alive):
    """Clause 8: pidfile PID cross-checked against user + cwd + full cmdline.

    Returns the verified PID, or None when no daemon exists (nothing to stop).
    Any ambiguity (daemon without pidfile proof, multiple daemons) refuses."""
    procs = procs if procs is not None else _list_procs()
    daemons = _ingest_daemons(procs, root, cwd_fn=cwd_fn)
    pidfile = os.path.join(live_dir, "ingest.pid")
    pid = None
    if os.path.isfile(pidfile):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            raise Refuse("unreadable ingest pidfile: %s" % pidfile)
    if pid is None or not alive_fn(pid):
        if daemons:
            raise Refuse("ingest daemon(s) %s running without matching pidfile "
                         "proof; cannot TERM safely" % daemons)
        return None
    if daemons != [pid]:
        raise Refuse("ingest daemon set %s != pidfile pid %d; refusing"
                     % (daemons, pid))
    cmd = dict(procs).get(pid, "")
    if not (_INGEST_CMD_RE.search(cmd) and "--loop" in cmd):
        raise Refuse("pid %d cmdline is not 'ingest.py --loop': %r" % (pid, cmd))
    uid = uid_fn(pid)
    if uid is not None and uid != os.geteuid():
        raise Refuse("pid %d belongs to uid %s, not us (uid %d)"
                     % (pid, uid, os.geteuid()))
    cwd = cwd_fn(pid)
    if cwd is not None and cwd != os.path.realpath(root):
        raise Refuse("pid %d cwd %s is not the repo %s" % (pid, cwd, root))
    return pid


def stop_ingest(pid, kill_fn=_term, alive_fn=_pid_alive, sleep_fn=time.sleep,
                wait_s=120):
    """TERM exactly this PID; wait for exit; NEVER escalate to SIGKILL."""
    try:
        kill_fn(pid)
    except PermissionError:
        raise TermRefused("SIGTERM to ingest pid=%d was refused by the "
                          "environment" % pid)
    except ProcessLookupError:
        return
    for _ in range(int(wait_s)):
        if not alive_fn(pid):
            print("ingest daemon pid=%d exited after SIGTERM" % pid)
            return
        sleep_fn(1)
    raise RecoveryError("ingest pid=%d did not exit within %ds after SIGTERM; "
                        "NOT escalating (no SIGKILL, clause 8)" % (pid, wait_s))


def start_ingest(live_dir, root):
    """Supervisor-identical daemon start (start_ingest in pipeline_supervisor)."""
    log = open(os.path.join(live_dir, "ingest.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(TOOLS, "ingest.py"), "--loop"],
        stdout=log, stderr=subprocess.STDOUT, cwd=root,
        start_new_session=True)
    log.close()
    with open(os.path.join(live_dir, "ingest.pid"), "w") as f:
        f.write(str(proc.pid))
    print("ingest daemon started by recovery pid=%d" % proc.pid)
    return proc.pid


def resume_ingest(live_dir, root, resume_wait, sleep_fn=time.sleep):
    """Clause 10/18: after our pause is gone, prefer the supervisor watchdog's
    own restart; start our own daemon only if none returns, and never leave
    two running (a double-start is resolved by TERMing only OUR child)."""
    pidfile = os.path.join(live_dir, "ingest.pid")

    def alive():
        try:
            with open(pidfile) as f:
                return _pid_alive(int(f.read().strip()))
        except (OSError, ValueError):
            return False

    deadline = time.time() + resume_wait
    while time.time() < deadline:
        if alive():
            print("ingest resumed by supervisor watchdog")
            return
        sleep_fn(2)
    own = start_ingest(live_dir, root)
    sleep_fn(3)
    daemons = _ingest_daemons(_list_procs(), root)
    if len(daemons) > 1 and own in daemons:
        print("supervisor also restarted ingest; retiring our own child pid=%d"
              % own)
        _term(own)
        others = [p for p in daemons if p != own]
        with open(pidfile, "w") as f:
            f.write(str(others[0]))


# ----------------------------------------- targeted checkpoint (clauses 12-15)
def capture_baseline(con, cfg):
    """Everything clause 15 requires to be provably unchanged, captured after
    ingest stopped and immediately before the fast-path one-shot."""
    return {
        "checkpoints": dict(con.execute(
            "SELECT file, byte_offset FROM checkpoint").fetchall()),
        "fact_counts": {t: con.execute("SELECT count(*) FROM %s" % t)
                        .fetchone()[0] for t in FACT_TABLES},
        "ingest_stats": sorted(tuple(r) for r in con.execute(
            "SELECT * FROM ingest_stats").fetchall()),
        "schema": {t: [tuple(r) for r in con.execute(
            "PRAGMA table_info('%s')" % t).fetchall()] for t in ALL_TABLES},
        "inventory": [os.path.abspath(p) for p in wc.seal_raw_files(
            cfg["raw_root"], TARGET_DATE, warehouse_root=cfg["warehouse_root"])],
    }


def invoke_fastpath(files):
    """Clause 12: the deployed one-shot with a FROZEN EXPLICIT array — the
    exact discovered paths, no glob, no backlog scan."""
    cmd = [sys.executable, os.path.join(TOOLS, "ingest.py")] + list(files)
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=wc.ROOT)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def check_fastpath_output(rc, out, err, n_files):
    """Clause 13: L1=0, trades=0, orderbooks_full=0 — per file AND in total."""
    if rc != 0:
        raise RecoveryError("fast-path one-shot exited rc=%d" % rc)
    if "skip (missing)" in err:
        raise RecoveryError("fast-path skipped a missing target file")
    m = _FASTPATH_SUMMARY_RE.search(out)
    if not m:
        raise RecoveryError("fast-path summary line missing from output")
    if m.groups() != ("0", "0", "0"):
        raise RecoveryError("fast-path reported NONZERO counts: L1=%s trades=%s "
                            "orderbooks_full=%s" % m.groups())
    per_file = _FASTPATH_FILE_RE.findall(out)
    if len(per_file) != n_files:
        raise RecoveryError("fast-path reported %d file line(s), expected %d"
                            % (len(per_file), n_files))
    if any(g != ("0", "0", "0") for g in per_file):
        raise RecoveryError("fast-path reported a nonzero per-file count: %s"
                            % (per_file,))


def verify_after_fastpath(con, cfg, capture, baseline):
    """Clauses 14-15: exact checkpoints; raw byte identity; facts/ingest_stats/
    schema/non-target-checkpoints/seal-inventory all unchanged."""
    problems = []
    ckpts = dict(con.execute(
        "SELECT file, byte_offset FROM checkpoint").fetchall())
    for path, cap in sorted(capture.items()):
        st = os.stat(path)
        if ckpts.get(path) != cap["size"]:
            problems.append("checkpoint != size for %s (ckpt=%s size=%d)"
                            % (path, ckpts.get(path), cap["size"]))
        if _stat_key(st) != cap["stat"]:
            problems.append("raw stat identity changed: %s" % path)
        elif sha256_file(path) != cap["sha256"]:
            problems.append("raw SHA-256 changed: %s" % path)
    targets = set(capture)
    if {k: v for k, v in ckpts.items() if k not in targets} != \
       {k: v for k, v in baseline["checkpoints"].items() if k not in targets}:
        problems.append("a NON-TARGET checkpoint changed")
    now_counts = {t: con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                  for t in FACT_TABLES}
    if now_counts != baseline["fact_counts"]:
        problems.append("facts row counts changed: %s -> %s"
                        % (baseline["fact_counts"], now_counts))
    if sorted(tuple(r) for r in con.execute(
            "SELECT * FROM ingest_stats").fetchall()) != baseline["ingest_stats"]:
        problems.append("ingest_stats changed")
    for t in ALL_TABLES:
        if [tuple(r) for r in con.execute(
                "PRAGMA table_info('%s')" % t).fetchall()] != baseline["schema"][t]:
            problems.append("schema changed: %s" % t)
    if [os.path.abspath(p) for p in wc.seal_raw_files(
            cfg["raw_root"], TARGET_DATE,
            warehouse_root=cfg["warehouse_root"])] != baseline["inventory"]:
        problems.append("seal_raw_files inventory changed")
    if problems:
        raise RecoveryError("post-checkpoint proof FAILED (day left unsealed):\n"
                            "  " + "\n  ".join(problems))
    print("post-checkpoint proofs PASS: %d target(s) exact, facts/ingest_stats/"
          "schema/non-target checkpoints/inventory unchanged" % len(capture))


# --------------------------------------------------------- seal (clauses 16-18)
def run_seal_sequence(live_dir, runner=_export_day_runner):
    """Clause 16/17: the four subcommands in order; alarm cleared only after
    exact --verify-seal success. No prune/legacy/invalidate/overwrite."""
    for step in SEAL_STEPS:
        rc, out = runner(step)
        if rc != 0:
            raise RecoveryError("seal step '--date %s %s' failed rc=%d"
                                % (TARGET_DATE, " ".join(step), rc))
        if step == ("--verify-seal",) and \
                ("DAY SEAL VERIFY PASS %s" % TARGET_DATE) not in out:
            raise RecoveryError("--verify-seal rc=0 but PASS line for %s "
                                "missing; alarm NOT cleared" % TARGET_DATE)
    alarm = os.path.join(live_dir, "seal_alarm.json")
    if os.path.isfile(alarm):
        os.unlink(alarm)
        print("seal_alarm cleared (after exact --verify-seal success)")


def _newest_family_file(raw_root, family):
    day_dir = wc.raw_day_dir(
        raw_root, datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d"))
    cands = glob.glob(os.path.join(day_dir, "%s_*.ndjson*" % family))
    return max(cands, key=os.path.getmtime) if cands else None


def verify_environment(cfg, live_dir, root, growth_wait, sleep_fn=time.sleep):
    """Clause 18 proofs. Returns a list of failures (empty = all proven)."""
    fails = []
    procs = _list_procs()
    daemons = _ingest_daemons(procs, root)
    if len(daemons) != 1:
        fails.append("expected exactly one ingest daemon, found %s" % daemons)
    listeners = [pid for pid, cmd in procs if "ws_shadow" in cmd]
    if not listeners:
        fails.append("no ws_shadow capture listener found")
    else:
        print("capture listeners present: %s" % listeners)
    signalled = set(KILL_LEDGER)
    print("signals sent by this recovery (complete ledger): %s"
          % (sorted(signalled) or "none"))
    if signalled & set(listeners):
        fails.append("KILL LEDGER intersects capture listeners — must never "
                     "happen")
    for family in ("firehose", "l2", "rfq"):
        newest = _newest_family_file(cfg["raw_root"], family)
        if newest is None:
            fails.append("no current %s_* raw file found" % family)
            continue
        size0 = os.path.getsize(newest)
        deadline = time.time() + growth_wait
        grown = False
        while time.time() < deadline:
            cur = _newest_family_file(cfg["raw_root"], family)
            if cur and (cur != newest or os.path.getsize(cur) > size0):
                grown = True
                break
            sleep_fn(3)
        if grown:
            print("raw growth confirmed: %s" % family)
        else:
            fails.append("%s raw did not grow within %ds" % (family, growth_wait))
    gaps = subprocess.run(
        [sys.executable, os.path.join(TOOLS, "capture_gaps.py"), "--live"],
        capture_output=True, text=True, cwd=root)
    if gaps.returncode != 0:
        fails.append("capture_gaps --live reports an ACTIVE capture alarm "
                     "(rc=%d): %s" % (gaps.returncode, gaps.stdout.strip()))
    else:
        print("no active capture alarm (capture_gaps --live PASS)")
    return fails


# ------------------------------------------------------------------ orchestration
def discover_only(cfg, args):
    """Read-only discovery: no lock, no pause, no signal, no writes."""
    print("READ-ONLY DISCOVERY for %s (touches nothing)" % TARGET_DATE)
    inventory = [os.path.abspath(p) for p in wc.seal_raw_files(
        cfg["raw_root"], TARGET_DATE, warehouse_root=cfg["warehouse_root"])]
    try:
        con = connect_ro(cfg["staging_db"], attempts=args.ro_attempts,
                         sleep_s=2.0)
    except Exception as e:
        # Writer lock persistently held: degrade to the checkpoint-less view.
        rfq = [(p, "UNKNOWN(writer lock held)", os.path.getsize(p))
               for p in inventory
               if ingest.RFQ_FASTPATH_RE.match(os.path.basename(p))]
        print("staging checkpoint unavailable (%s)" % e)
        print("EXPECTED PENDING LIVE --discover-only (RFQ files in seal "
              "inventory; checkpoint diff not yet applied):")
        print_inventory(inventory, rfq)
        return 0
    try:
        inventory, blocking, non_rfq = compute_blocking(cfg, con)
    finally:
        con.close()
    if non_rfq:
        print("DISCOVERY STOP: non-RFQ family behind:", file=sys.stderr)
        for path, ckpt, size in non_rfq:
            print("  %s checkpoint=%s size=%d" % (path, ckpt, size),
                  file=sys.stderr)
        return 3
    try:
        probe_existing_seals(cfg)
        capture = validate_and_capture(blocking, cfg["raw_root"],
                                       args.stability_wait)
    except Stop as e:
        print("DISCOVERY STOP: %s" % e, file=sys.stderr)
        return 3
    print_inventory(inventory, blocking, capture)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="Bounded 2026-07-12 RFQ seal recovery (ADDENDUM 8)")
    ap.add_argument("--date", required=True,
                    help="must be exactly %s (clause 1)" % TARGET_DATE)
    ap.add_argument("--discover-only", action="store_true",
                    help="read-only discovery + inventory; touches nothing")
    ap.add_argument("--stability-wait", type=float, default=5.0,
                    help="seconds between the two stat/SHA-256 samples")
    ap.add_argument("--resume-wait", type=float, default=90.0,
                    help="seconds to let the supervisor watchdog restart "
                         "ingest before starting it ourselves")
    ap.add_argument("--growth-wait", type=float, default=180.0,
                    help="seconds allowed for each raw family to grow")
    ap.add_argument("--ro-attempts", type=int, default=15,
                    help="read-only staging connect retries")
    ap.add_argument("--live-dir", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv[1:])

    if args.date != TARGET_DATE:
        print("REFUSED: this is the bounded %s recovery only (clause 1); "
              "got --date %s" % (TARGET_DATE, args.date), file=sys.stderr)
        return 2
    args.stability_wait = max(args.stability_wait, 2.0)

    cfg = wc.load_config()
    live_dir = args.live_dir or os.path.join(wc.ROOT, "work", "live")
    if args.discover_only:
        return discover_only(cfg, args)

    # Pre-flight: write-once seal state for the target day itself.
    if os.path.exists(wc.seal_path(cfg["warehouse_root"], TARGET_DATE)):
        rc, out = _export_day_runner(("--verify-seal",))
        if rc == 0 and ("DAY SEAL VERIFY PASS %s" % TARGET_DATE) in out:
            alarm = os.path.join(live_dir, "seal_alarm.json")
            if os.path.isfile(alarm):
                os.unlink(alarm)
                print("seal_alarm cleared (existing seal verified)")
            print("%s is already SEALED and verifies; nothing to recover"
                  % TARGET_DATE)
            return 0
        print("STOP: %s has an EXISTING seal that FAILS verification. Seals "
              "are write-once — operator remediation "
              "(--operator-invalidate-seal) required; this recovery will not "
              "touch it." % TARGET_DATE, file=sys.stderr)
        return 3

    conflicts = find_conflicts()
    if conflicts:
        print("REFUSED: conflicting seal/recovery process(es) running:",
              file=sys.stderr)
        for pid, cmd in conflicts:
            print("  pid=%d %s" % (pid, cmd), file=sys.stderr)
        return 2

    lock_dir = os.path.join(live_dir, "rfq_recovery.lock")
    pause_path = os.path.join(live_dir, "export_pause")
    token = "%s pid=%d %s" % (
        OWNER, os.getpid(),
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"))
    state = {"lock": False, "pause": False, "stopped": None, "resumed": False}

    def cleanup():
        # EXIT/INT/TERM path (clause 10): release only our token, then make
        # sure ingest is back (prefer the supervisor's own restart).
        if state["pause"]:
            release_pause(pause_path, token)
            state["pause"] = False
        if state["stopped"] is not None and not state["resumed"]:
            resume_ingest(live_dir, wc.ROOT, args.resume_wait)
            state["resumed"] = True
        if state["lock"]:
            release_lock(lock_dir)
            state["lock"] = False

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        acquire_lock(lock_dir)                                   # clause 7
        state["lock"] = True
        acquire_pause(pause_path, token)                         # clauses 6/7/10
        state["pause"] = True
        print("export_pause acquired: %s" % token)

        # ---- DISCOVERY under our pause (clauses 2-5) ----
        con = connect_ro(cfg["staging_db"], attempts=args.ro_attempts)
        try:
            inventory, blocking, non_rfq = compute_blocking(cfg, con)
        finally:
            con.close()
        if non_rfq:
            raise Stop("non-RFQ family behind (recovery is RFQ-only):\n  " +
                       "\n  ".join("%s checkpoint=%s size=%d" % row
                                   for row in non_rfq))
        probe_existing_seals(cfg)
        capture = validate_and_capture(blocking, cfg["raw_root"],
                                       args.stability_wait)
        print_inventory(inventory, blocking, capture)

        # ---- PROCESS SAFETY: stop the verified ingest daemon (clause 8) ----
        pid = identify_ingest(live_dir, wc.ROOT)
        if pid is not None:
            print("verified ingest daemon pid=%d (pidfile+user+cwd+cmdline); "
                  "sending SIGTERM to that PID only" % pid)
            state["stopped"] = pid
            stop_ingest(pid)
        else:
            print("no ingest daemon running (pause holds the watchdog off)")

        # ---- set-stability recheck + baseline (clauses 5, 15) ----
        con = connect_ro(cfg["staging_db"], attempts=args.ro_attempts)
        try:
            _inv2, blocking2, non_rfq2 = compute_blocking(cfg, con)
            if non_rfq2 or [r[0] for r in blocking2] != [r[0] for r in blocking]:
                raise Stop("candidate set changed during preparation "
                           "(was %d file(s), now %d + %d non-RFQ)"
                           % (len(blocking), len(blocking2), len(non_rfq2)))
            baseline = capture_baseline(con, cfg)
        finally:
            con.close()

        # ---- TARGETED CHECKPOINT (clauses 12-15) ----
        frozen = [row[0] for row in blocking]
        if frozen:
            rc, out, err = invoke_fastpath(frozen)
            check_fastpath_output(rc, out, err, len(frozen))     # clause 13
            con = connect_ro(cfg["staging_db"], attempts=args.ro_attempts)
            try:
                verify_after_fastpath(con, cfg, capture, baseline)
            finally:
                con.close()
        else:
            print("no blocking RFQ file remains; proceeding to the seal "
                  "sequence directly")

        # ---- SEAL (clauses 16-17), pause still held ----
        run_seal_sequence(live_dir)

        # ---- CLEANUP + environment proofs (clause 18) ----
        cleanup()
        fails = verify_environment(cfg, live_dir, wc.ROOT, args.growth_wait)
        if fails:
            print("SEAL SUCCEEDED but cleanup verification FAILED:",
                  file=sys.stderr)
            for f in fails:
                print("  " + f, file=sys.stderr)
            return 5
        print("RECOVERY COMPLETE: %s sealed, verified, alarm cleared, "
              "pipeline healthy." % TARGET_DATE)
        print("NOTE (clauses 19-20, NOT this script): W05 version-bound "
              "publication + Mac no-SSH acceptance now run separately — the "
              "supervisor seal chain's `deploy/ec2_s3_sync.sh research_sync "
              "%s` publishes the sealed release on its next cycle."
              % TARGET_DATE)
        return 0
    except Refuse as e:
        print("REFUSED TO START: %s" % e, file=sys.stderr)
        return 2
    except Stop as e:
        print("DISCOVERY STOP: %s" % e, file=sys.stderr)
        return 3
    except TermRefused as e:
        print("ENVIRONMENT REFUSED SIGTERM: %s" % e, file=sys.stderr)
        print("Not improvising (clause 11). Operator: run exactly this on "
              "the pipeline host, from the repo root:", file=sys.stderr)
        print("  python3 tools/recover_rfq_seal_2026-07-12.py --date "
              "2026-07-12", file=sys.stderr)
        return 4
    except RecoveryError as e:
        print("RECOVERY FAILED (day left unsealed): %s" % e, file=sys.stderr)
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
