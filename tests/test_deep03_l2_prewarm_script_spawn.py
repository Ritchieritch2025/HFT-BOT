#!/usr/bin/env python3
"""Script-spawn regression proof for the .14 L2 episode prewarm fix.

This is the test class the .13 release lacked, and whose absence let the .13
deadlock ship.  The audited .13 tests
(``tests/test_deep03_l2_parallel_prewarm.py``) drive
``prewarm_l2_episode_partitions`` from a PYTEST process, where the worker
callable's ``__module__`` is the importable ``deep03_fullscope_runner`` — so a
spawned child just ``import``s that module.  PRODUCTION instead runs the runner
as a SCRIPT (``deploy/w09/exploratory_autoresearch.py`` execs
``python .../deep03_fullscope_runner.py``), so the worker's ``__module__`` is
``"__main__"`` and every spawned child re-executes the script's whole import
chain via spawn's ``__mp_main__`` fixup.  The .13 bug lived entirely on that
never-exercised path:

  * .13 used ``multiprocessing.Pool(maxtasksperchild=1).imap_unordered``.
    ``multiprocessing.Pool`` has NO detection for a worker that dies during
    spawn bootstrap (before it takes a task): ``_maintain_pool`` respawns it
    forever while ``imap_unordered`` waits on results that never arrive, so
    the parent hangs indefinitely (observed: 17h at 100% CPU, zero
    checkpoints).
  * .14 uses ``concurrent.futures.ProcessPoolExecutor``, which raises
    ``BrokenProcessPool`` the instant any worker dies unexpectedly — the silent
    infinite hang becomes a LOUD, typed, immediate abort.

Both tests below launch a throwaway script whose ``__main__`` hosts the worker
(reproducing the production ``__module__ == "__main__"`` condition) and run it
as a real subprocess under a wall-clock timeout:

  (a) the REAL prewarm, driven through a ``__main__``-hosted worker wrapper on
      the small deterministic L2 fixture, completes without hang — proving the
      whole script-spawn path (child re-imports ``deep03_fullscope_runner``
      purely, workers really spawn and compute, store reuse works);
  (b) a ``__main__``-hosted worker that dies during bootstrap makes the run
      abort with a ``BrokenProcessPool``-derived ``Deep03InputError`` within
      seconds — NOT hang.  Under the .13 ``multiprocessing.Pool`` mechanism the
      identical scenario hangs until killed (demonstrated out-of-tree in the
      .14 handoff); this test asserts the fixed behavior and fails (times out)
      against the .13 mechanism.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS_RESEARCH = ROOT / "tools" / "research"
TESTS = ROOT / "tests"

# Generous ceilings: the assertions are "finished before this", and any value
# far below the ceiling still proves "did not hang".  The bootstrap-death abort
# is milliseconds in practice; the ceiling exists only to fail a genuine hang.
_BUILD_TIMEOUT_S = 600
_ABORT_TIMEOUT_S = 90
_ABORT_MUST_FINISH_WITHIN_S = 45


def _run_script(body: str, tmp_path: Path, *args: str, timeout: float):
    script = tmp_path / "spawn_entrypoint.py"
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(TOOLS_RESEARCH)!r})\n"
        f"sys.path.insert(0, {str(TESTS)!r})\n"
    )
    script.write_text(header + body, encoding="utf-8")
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed, time.monotonic() - started


# The prewarm worker is submitted BY VALUE via a __main__-level wrapper so the
# spawned child must resolve it through spawn's __main__ fixup — exactly the
# production condition (runner-as-script) that the pytest-module tests never
# reproduce.  The wrapper is monkeypatched onto the runner module INSIDE the
# __main__ guard, so a spawned child (which re-executes this file as
# __mp_main__, guard false) still sees the real, unpatched worker.
_REAL_PREWARM_BODY = """
from pathlib import Path

import deep03_fullscope_runner as runner
import test_deep03_l2_parallel_prewarm as par

_REAL_WORKER = runner._l2_prewarm_worker


def _main_hosted_worker(spec):
    # __module__ == "__main__": forces the production spawn-resolution path.
    return _REAL_WORKER(spec)


if __name__ == "__main__":
    runner._l2_prewarm_worker = _main_hosted_worker
    tmp = Path(sys.argv[1])
    manifest, binding, seq_root, seq_tree, _ = par._sequential_ground_truth(tmp)
    root = par._resume_root_with_physical(tmp, seq_root, seq_tree, "spawn")
    receipt = par._prewarm(tmp, manifest, binding, root, "spawn", workers=2)
    assert receipt["state"] == "COMPLETE", receipt
    assert len(receipt["computed"]) == par.EXPECTED_REPLAY_PARTITIONS, receipt
    print("SCRIPT_SPAWN_PREWARM_OK computed=%d" % len(receipt["computed"]))
"""

_BOOTSTRAP_DEATH_BODY = """
import os
import multiprocessing

import deep03_fullscope_runner as runner
from deep03_v3_common import Deep03InputError


def _bootstrap_dying_worker(spec):
    # A worker that dies before returning a result — the exact class of failure
    # (spawn-bootstrap death) that multiprocessing.Pool respawns forever.  Its
    # __module__ is "__main__", reproducing the production runner-as-script
    # worker-resolution path.
    os._exit(70)


if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    specs = [{"partition_key": "date=2026-07-12/bucket=%d" % i} for i in range(6)]
    try:
        runner._run_l2_prewarm_pool(_bootstrap_dying_worker, specs, 2, context)
    except Deep03InputError as exc:
        sys.stderr.write("PREWARM_ABORTED: %s\\n" % exc)
        raise SystemExit(23)
    sys.stderr.write("PREWARM_DID_NOT_ABORT\\n")
    raise SystemExit(11)
"""


def test_script_spawn_real_prewarm_completes_without_hang(tmp_path):
    """(a) The real prewarm runs to completion under production script-spawn."""
    work = tmp_path / "fixture"
    work.mkdir()
    completed, elapsed = _run_script(
        _REAL_PREWARM_BODY, tmp_path, str(work), timeout=_BUILD_TIMEOUT_S
    )
    assert completed.returncode == 0, (
        f"script-spawn prewarm failed rc={completed.returncode}\n"
        f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )
    assert "SCRIPT_SPAWN_PREWARM_OK" in completed.stdout, completed.stdout
    # It finished, so by construction it did not hang; keep the timeout as the
    # hang guard rather than asserting a tight wall-clock bound on real compute.
    assert elapsed < _BUILD_TIMEOUT_S


def test_script_spawn_bootstrap_death_aborts_fast_not_hang(tmp_path):
    """(b) A bootstrap-dying worker → loud BrokenProcessPool abort, no hang."""
    try:
        completed, elapsed = _run_script(
            _BOOTSTRAP_DEATH_BODY, tmp_path, timeout=_ABORT_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - the .13 bug
        pytest.fail(
            "prewarm HUNG on a bootstrap-dying worker (the .13 deadlock): "
            f"no abort within {_ABORT_TIMEOUT_S}s. stderr so far:\n"
            f"{(exc.stderr or b'') if isinstance(exc.stderr, bytes) else exc.stderr}"
        )
    assert completed.returncode == 23, (
        "expected a typed abort (exit 23), not "
        f"rc={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
        f"STDERR:\n{completed.stderr}"
    )
    assert "PREWARM_ABORTED" in completed.stderr, completed.stderr
    assert "BrokenProcessPool" in completed.stderr, completed.stderr
    # The abort is near-instant; assert a wall-clock bound well under the
    # timeout so a slow-but-eventually-returning regression is still caught.
    assert elapsed < _ABORT_MUST_FINISH_WITHIN_S, (
        f"abort took {elapsed:.1f}s (>= {_ABORT_MUST_FINISH_WITHIN_S}s): "
        "suspiciously slow for a BrokenProcessPool abort"
    )


def test_prewarm_pool_uses_process_pool_executor_not_multiprocessing_pool():
    """Source guard: never regress to the .13 multiprocessing.Pool mechanism."""
    source = (TOOLS_RESEARCH / "deep03_fullscope_runner.py").read_text()
    assert "ProcessPoolExecutor(" in source
    assert "BrokenProcessPool" in source
    # The .13 hang CALL primitives must be gone from the prewarm path (match
    # call-forms, not the docstring prose that explains why they were removed).
    assert ".imap_unordered(" not in source
    assert ".Pool(" not in source
    assert "maxtasksperchild=" not in source
