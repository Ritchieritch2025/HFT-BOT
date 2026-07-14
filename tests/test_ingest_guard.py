#!/usr/bin/env python3
"""B11 (W-A) contract tests for tools/ingest_guard.sh — the single-writer
ingest lifecycle guard the supervisor sources.

The 07-12 incident contract, exercised against the REAL bash functions:
  1. stop_ingest_for_export TERMs EVERY live ingest daemon — including an
     orphan the pidfile lost track of — and only returns 0 at zero survivors.
  2. start_ingest under an export_pause never spawns a writer.
  3. start_ingest next to a live daemon adopts it (no second writer, ever).
  4. TOCTOU: a pause that lands between spawn and the post-spawn re-check
     kills the fresh daemon again (simulated by a stub ingest that creates
     the pause itself the moment it starts).

Each test runs in an isolated tmp cwd with a STUB tools/ingest.py (sleeps
until TERM), so pgrep's cmdline match sees realistic daemons while the /proc
cwd filter (production Linux) keeps any real box daemon out of scope.

stdlib + pytest only.
"""
import os
import subprocess
import sys
import time

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
GUARD = os.path.join(ROOT, "tools", "ingest_guard.sh")

STUB = """\
import os, sys, time
# stub ingest daemon: optionally reproduce the B11 TOCTOU by creating the
# export_pause the instant we start, then just stay alive until TERM.
if os.environ.get("STUB_TOUCH_PAUSE") == "1":
    open(os.path.join("work", "live", "export_pause"), "w").write(
        "seal_chain pid=99999999\\n")
time.sleep(600)
"""


def make_cwd(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ingest.py").write_text(STUB)
    (tmp_path / "work" / "live").mkdir(parents=True)
    return str(tmp_path)


def run_guard(cwd, script, env_extra=None):
    env = dict(os.environ)
    env.pop("STUB_TOUCH_PAUSE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", 'set -u; LIVE=work/live; . "%s"; %s' % (GUARD, script)],
        cwd=cwd, env=env, capture_output=True, text=True)


def spawn_stub(cwd):
    p = subprocess.Popen([sys.executable or "python3", "tools/ingest.py",
                          "--loop"], cwd=cwd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    assert p.poll() is None
    return p


def live_procs(cwd):
    r = run_guard(cwd, "ingest_procs")
    return [int(x) for x in r.stdout.split()]


def test_stop_kills_orphan_too(tmp_path):
    """Contract 1: the pidfile names ONE daemon; a second (orphan) writer is
    still TERMed — the process table, not the pidfile, is the truth."""
    cwd = make_cwd(tmp_path)
    tracked = spawn_stub(cwd)
    orphan = spawn_stub(cwd)
    (tmp_path / "work" / "live" / "ingest.pid").write_text(str(tracked.pid))
    seen = live_procs(cwd)
    assert tracked.pid in seen and orphan.pid in seen, seen
    r = run_guard(cwd, "stop_ingest_for_export; echo rc=$?")
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    # wait() (with reap) proves both daemons actually exited — os.kill(pid,0)
    # would false-alarm on the unreaped zombies
    tracked.wait(timeout=10)
    orphan.wait(timeout=10)
    assert not (tmp_path / "work" / "live" / "ingest.pid").exists()
    assert live_procs(cwd) == []


def test_start_refuses_under_pause(tmp_path):
    """Contract 2: an export_pause means the seal chain owns the DB window —
    no writer may start."""
    cwd = make_cwd(tmp_path)
    (tmp_path / "work" / "live" / "export_pause").write_text(
        "seal_chain pid=1\n")
    r = run_guard(cwd, "start_ingest; echo rc=$?")
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    assert live_procs(cwd) == []
    assert not (tmp_path / "work" / "live" / "ingest.pid").exists()


def test_start_adopts_survivor_never_doubles(tmp_path):
    """Contract 3: a live daemon (orphaned from the pidfile) is adopted into
    the pidfile; a second writer is never spawned."""
    cwd = make_cwd(tmp_path)
    orphan = spawn_stub(cwd)
    try:
        r = run_guard(cwd, "start_ingest; echo rc=$?")
        assert "rc=0" in r.stdout, r.stdout + r.stderr
        assert live_procs(cwd) == [orphan.pid]
        pidfile = tmp_path / "work" / "live" / "ingest.pid"
        assert pidfile.read_text().strip() == str(orphan.pid)
    finally:
        orphan.terminate()
        orphan.wait()


def test_pause_winning_the_spawn_race_leaves_zero_writers(tmp_path):
    """Contract 4 (the B11 TOCTOU, both belts): the stub creates the pause
    the instant it starts — i.e. the pause lands mid-spawn. Whichever side
    samples first, the interlock must converge to zero writers: either
    start_ingest's post-spawn re-check undoes the spawn, or the seal chain's
    process-table-verified stop (which runs while it owns the pause) TERMs
    the sneak. Any interleaving is covered by one of the two — that is the
    invariant the 07-12 incident violated."""
    cwd = make_cwd(tmp_path)
    r = run_guard(cwd, "start_ingest; echo rc=$?",
                  env_extra={"STUB_TOUCH_PAUSE": "1"})
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    # wait until the stub has run far enough to have created the pause
    pause = tmp_path / "work" / "live" / "export_pause"
    end = time.time() + 10
    while time.time() < end and not pause.exists():
        time.sleep(0.05)
    assert pause.exists()
    # the chain owns the pause now; its verified stop must leave zero writers
    r2 = run_guard(cwd, "stop_ingest_for_export; echo rc=$?")
    assert "rc=0" in r2.stdout, r2.stdout + r2.stderr
    assert live_procs(cwd) == []
    assert not (tmp_path / "work" / "live" / "ingest.pid").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
