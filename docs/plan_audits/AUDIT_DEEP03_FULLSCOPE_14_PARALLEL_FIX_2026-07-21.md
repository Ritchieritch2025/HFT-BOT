# Independent Adversarial Audit — deep03 fullscope .14 (L2 prewarm deadlock fix)

## VERDICT: ✅ PASS — .14 fixes the .13 script-spawn deadlock; approved for deploy

**Audited runtime commit:** `b1fff2ad7ab13753b2dd8775f94d59b6b9804e1b`
(branch `w-deep03-fullscope-09`, HEAD, clean tree)
**Auditor:** `claude-independent-fullscope-14-auditor-2026-07-21`
**Interpreter used for all reproductions:** CPython **3.9.6** (`/usr/bin/python3`)
— i.e. the older-interpreter path (`ProcessPoolExecutor` **without**
`max_tasks_per_child`, fresh-executor WAVES branch). This matters and is
called out explicitly below.

This audit was written specifically to not repeat the .13 miss: the .13 release
passed audit yet deadlocked in production for 17h because the audit only
exercised the **pytest-module** worker-resolution path and never the
**production script-spawn** path (`__module__ == "__main__"`). Every claim
below was reproduced by the auditor, including an **independently constructed**
script-spawn deadlock repro and an independent stub of the old .13 mechanism.

---

## 1. Diff confinement — PASS

`.14`-only window `51de58c..HEAD` touches exactly:

| File | Nature |
|---|---|
| `tools/research/deep03_fullscope_runner.py` | pool mechanism swap (the fix) |
| `tests/test_deep03_l2_prewarm_script_spawn.py` | **new** script-spawn regression test |
| `deploy/w09/deep03_authority_gate.py` | `.13`→`.14` release ids |
| `deploy/w09/deep03_one_shot_arm.py` | `.13`→`.14` release id |
| `deploy/w09/deep03_open_discovery_modules.sha256` | runner hash bump |
| `deploy/w09/exploratory_autoresearch_payload.sha256` | manifest re-hash |
| `deploy/w09/w09-exploratory-autoresearch.service` | `20260719-13`→`-14` release json paths |
| `tests/test_deep03_one_shot_arm.py`, `tests/test_deep03_v3_w1_preflight.py`, `tests/test_w09_exploratory_autoresearch.py` | test-expectation bumps (`.14` ids) |

**ABI pins byte-identical (MUST):**
- `tools/research/deep03_v3_l2.py` — `1530798e…a3a627` at both `bc761cd` and HEAD → IDENTICAL
- `tools/research/deep03_v3_methods.py` — `2ad2e6dc…1db064` at both → IDENTICAL

No source module outside the runner changed. The runner diff is confined to:
adding `_run_l2_prewarm_pool`, the `ProcessPoolExecutor`/`BrokenProcessPool`
imports, the `_PREWARM_MAX_TASKS_PER_CHILD_SUPPORTED` capability probe, and
replacing the `context.Pool(...).imap_unordered` block in
`prewarm_l2_episode_partitions` with a call to the new helper. The L2 stage
constants (`_L2_PHYSICAL_STAGE`, `_L2_REPLAY_STAGE`, `_L2_EPISODE_STAGE` =
`causal-lifecycle-v3`) are untouched (context lines only).

## 2. THE CRITICAL CHECK — script-spawn failure mode reproduced and fixed — PASS

This is the check .13's audit lacked.

**2a. Their new test.** `tests/test_deep03_l2_prewarm_script_spawn.py` — 3
tests, all pass in 7.7s. It genuinely hosts the worker in a throwaway script's
`__main__` (so the spawned child must resolve it through spawn's `__main__`
fixup — the production condition), and asserts (a) the real prewarm completes
on the deterministic fixture and (b) a bootstrap-dying `__main__`-hosted worker
aborts with a typed error, not a hang.

**2b. Independent repro constructed by the auditor** (not their test):
- **`.14` path (`_run_l2_prewarm_pool`) with a worker that dies during spawn
  bootstrap** (module-top `os._exit(70)` fired only in the re-imported
  `__mp_main__` child): aborts with
  `Deep03InputError: L2 episode prewarm aborted: a worker process died
  unexpectedly (BrokenProcessPool: …); refusing to retry-forever` in **0.05s**.
  Typed, loud, immediate.
- **Old `.13` mechanism stubbed** (`ctx.Pool(processes=2, maxtasksperchild=1)`
  `.imap_unordered`, same bootstrap-dying worker): **HANG confirmed** — did not
  abort within a Python-enforced 30s wall-clock kill and had to be
  `SIGKILL`ed. This is the same failure class as the 17h production deadlock
  (`multiprocessing.Pool._maintain_pool` respawns the bootstrap-dying worker
  forever while `imap_unordered` waits on results that never arrive).
  → I reproduced the hang directly rather than relying on the implementer's
  measured 25s figure.

**2c. Import purity** — verified directly. A fresh interpreter importing
`deep03_fullscope_runner` from a cwd (`repo root`) / `sys.path` mimicking the
deploy invocation returns rc=0 in **0.07s** with no stdout/stderr side effects
and no hang; `_run_l2_prewarm_pool` and `prewarm_l2_episode_partitions` are
present. The module's `if __name__ == "__main__"` guard (line 2191) does not
fire on import.

## 3. Determinism + resume — PASS

- `test_deep03_thread_determinism.py`, `test_deep03_l2_parallel_prewarm.py`,
  `test_deep03_v3_l2.py` — all pass (44 tests). The parallel-prewarm suite
  includes the resume/skip and crash-safety cases
  (`test_worker_failure_aborts_without_accepting_corruption`), which assert the
  post-crash rerun's checkpoint tree is byte-identical to the sequential
  baseline. The `ProcessPoolExecutor` swap did not change checkpoint bytes or
  skip/resume logic.
- **Namespace continuity:** `source_binding_sha256 =
  c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979` is
  preserved (the L2 ABI modules are byte-identical and the stage-version tuples
  are untouched), so `.14` resumes the existing `source-c08083fe` namespace and
  `stage_versions` are unchanged.

## 4. Regression breadth — PASS

- **In-task worker `RuntimeError` is NOT misclassified as `BrokenProcessPool`.**
  Independently verified from a real importable script: a worker that raises
  `RuntimeError` in-task (process stays alive; exception returns via
  `future.result()`) surfaces as
  `Deep03InputError: L2 episode prewarm worker failed: RuntimeError: …` —
  `prewarm worker failed` = True, `BrokenProcessPool` in message = False. The
  ordering is load-bearing and correct: `BrokenProcessPool` **is** a
  `RuntimeError` subclass (MRO: BrokenProcessPool→BrokenExecutor→RuntimeError→
  Exception), so it is caught by the `except BrokenProcessPool` arm placed
  *before* `except Exception`; an in-task exception is a plain `RuntimeError`
  (not a `BrokenProcessPool` instance) and correctly falls through to the
  generic wrap. The existing `test_worker_failure_aborts_without_accepting_
  corruption` covers the same guarantee.
- **`BrokenProcessPool` is never caught-and-retried.** It is caught once (runner
  line 380) and converted to a fail-closed abort. The only `while True` in the
  runner (line 237) is the `_SerializedWriterStore` checkpoint writer-lock
  acquisition loop — unrelated to the pool.

## 5. Full-tree pytest + manifests + stale ids — PASS

- **Full tree:** `1843 passed, 0 failures, 0 errors, 4 skipped (1 xfail)`
  (JUnit-parsed; 1847 total cases). Matches the expected 1843 exactly
  (`.13` baseline 1843 + 3 new script-spawn tests − but reported "passed" is
  the non-skipped count; total collected 1847). rc=0.
- **Both SHA manifests verify:** `deep03_open_discovery_modules.sha256` (15/15
  files OK, runner = `617c6284…c84359`) and
  `exploratory_autoresearch_payload.sha256` (10/10 OK).
- **Zero stale `.13`/`.12` ids** in `deploy/` and `tests/`.

## Observation (non-blocking)

`docs/plan_releases/DEEP03_D3_W0_RELEASE_CANDIDATE_2026-07-18.json` and the W1
candidate still carry `"release_id": "D3-W0-2026-07-19.13"` / `…W1…13`; they
were not re-stamped in the `.14` window. This is documentation lag, not a
runtime defect: these candidate docs do not bind any module hash, and the
deploy gate the service actually enforces (`deploy/w09/*` manifests +
`w09-exploratory-autoresearch.service`) is consistently `.14` and verifies.
Recommend re-stamping the candidate docs to `.14` on the next docs pass.

## Reproduction commands (auditor, CPython 3.9.6)

```
git -C /Users/ritcardo/HFT-BOT-deep03-fullscope-09 rev-parse HEAD   # b1fff2ad…
python3 -m pytest tests/test_deep03_l2_prewarm_script_spawn.py -v   # 3 passed
python3 repro_14_abort.py        # ABORTED_TYPED=True in 0.05s, exit 23
python3 repro_13_hang.py         # HANG (killed at 30s wall-clock)
python3 repro_intask_raise.py    # wraps_as_worker_failed=True, misclassified=False
python3 -m pytest -q             # 1843 passed, 0 failed, 4 skipped
```
