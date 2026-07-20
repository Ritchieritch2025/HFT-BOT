# Independent adversarial audit — deep03 fullscope release .13 (parallel L2 episode prewarm)

## VERDICT: ✅ PASS

Commit `bc761cdfb1603877ebcb3d35413d770294f42e5a` ("perf(deep03): .13 --
parallel L2 episode prewarm across partitions (4 workers)") on branch
`w-deep03-fullscope-09` is approved. Diff confinement holds, the L2 stage ABI
is byte-untouched, the flock-serialized store access survives direct
adversarial concurrency attack (including a real SIGKILL between the two
partition publishes), the 4-worker memory bound is arithmetically consistent
with the service limits, the .13 release chain is internally consistent with
zero stale ids, and the full tree is green (1840 passed, 0 failures).

Auditor: claude-independent-fullscope-13-auditor-2026-07-20 (independent
session; read-only against the target worktree; every check below was
executed by the auditor, not accepted from the implementer's claims).

- Target: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`, HEAD =
  `bc761cdfb1603877ebcb3d35413d770294f42e5a`, working tree clean, branch
  `w-deep03-fullscope-09` (all verified via `git status` / `rev-parse`).
- Runner under audit: `tools/research/deep03_fullscope_runner.py`, sha256
  `44fc25e82af347521c6a5d3945e5cfee874a81eb19f205593e743b7d8ada6d5d`
  (recomputed; matches `deploy/w09/deep03_open_discovery_modules.sha256`).

## 1. Diff confinement — PASS

`git diff --name-status bc761cd^..bc761cd` touches exactly:

- `tools/research/deep03_fullscope_runner.py` (the prewarm machinery),
- `tests/test_deep03_l2_parallel_prewarm.py` (new, 365 lines, 6 tests),
- deploy/w09 chain: `deep03_authority_gate.py`, `deep03_one_shot_arm.py`,
  `w09-exploratory-autoresearch.service` (.12→.13 ids/paths only),
  both SHA manifests regenerated,
- test expectation updates only: `test_deep03_one_shot_arm.py`,
  `test_deep03_v3_w1_preflight.py`, `test_w09_exploratory_autoresearch.py`.

ABI safety: `deep03_v3_l2.py` and `deep03_v3_methods.py` are byte-identical
across the commit — git blob ids equal at `bc761cd^` and `bc761cd`
(`879ad028…` / `cb8ff7d6…`), and their working-tree sha256s equal the .12
receipt's pins (`1530798e…` / `2ad2e6dc…`). Every L2 `stage_version` is
therefore unchanged and the live namespace's completed partitions stay
reusable. In `deep03_open_discovery_modules.sha256` only the runner line
moved (`f914b804… → 44fc25e8…`).

## 2. Tests and manifests — PASS

Run by the auditor on the target worktree (Python 3.9.6, pytest 8.4.2,
darwin):

- `tests/test_deep03_l2_parallel_prewarm.py` +
  `tests/test_deep03_thread_determinism.py`: **9 passed** (6 + 3) in 32s.
- Full tree: **1844 collected / 1840 passed / 0 failed / 0 errors**
  (2 platform skips: Linux memfd; 1 legacy collection skip:
  `test_kalshi_golden`; 1 xfail: settlements export). Matches the commit
  message's "1840 passed, 3 skipped, 1 xfailed". Note: pytest's final
  summary line is swallowed on stdout (a test closes/redirects the stream),
  so counts were taken from `--junitxml` output, exit code 0.
- `shasum -a 256 -c deploy/w09/deep03_open_discovery_modules.sha256`: all OK.
- `shasum -a 256 -c deploy/w09/exploratory_autoresearch_payload.sha256`:
  all OK (includes the regenerated modules manifest, gate, arm, service).

## 3. Adversarial concurrency attacks (auditor-written, outside the shipped tests) — PASS

Mechanism audited first: writer exclusivity is the kernel flock
(`LOCK_EX|LOCK_NB` on `.CHECKPOINT_WRITER.lock`) taken in
`BoundedCheckpointStore.__init__`; `_SerializedWriterStore._with_store`
retries only the constructor's lock-contention `RuntimeError` — validation
failures, `ValueError` (bad root/binding) and `FileNotFoundError` propagate
un-retried (fail-closed, confirmed by code read of runner lines 216–242 and
methods lines 173–400).

**Attack A — 4-process flock stress** (`attack_a_flock_stress.py`): 4 spawned
processes hammered one namespace through `_SerializedWriterStore`
(40 `write_partition` publishes + 27 contended cross-process
`validate_partition` calls), each store operation entering an
`O_CREAT|O_EXCL` critical-section oracle that fails loudly on any overlap.
Result: zero mutual-exclusion violations; afterwards a single real
`BoundedCheckpointStore` validated all 40 partitions (payload sha256, schema,
row counts, per-partition content check), all receipts byte-canonical, zero
`.partial/` leftovers.

**Attack B part 1 — real SIGKILL between the two publishes**
(`attack_b_sigkill.py`): on the shared determinism fixture, a child process
ran the real `_replay_one_partition` through `_SerializedWriterStore` and
SIGKILLed itself the instant the replay `write_partition` returned — before
the episode publish. Verified on-disk state: replay receipt durable, episode
receipt absent (child exit = -9). A subsequent
`prewarm_l2_episode_partitions` did NOT list the half-published partition in
`already_complete` (both receipts must exist to skip), recomputed it through
a worker (which validated and reused the COMPLETE replay receipt), and the
finished store was **byte-identical** to the fully sequential ground truth on
every stage.

**Attack B part 2 — parent skip path opens nothing for write**: with the
namespace fully COMPLETE, the auditor held the exclusive writer flock in the
parent process and called the prewarm. It returned `NOOP_NOTHING_PENDING`
instantly with all 15 partitions in `already_complete` — impossible if the
skip path constructed a store or opened receipts for write (any store
construction would have blocked/raised on the held flock). Code confirms the
scan uses only lock-free `_paths(...)[1].exists()` and read-only receipt
parsing (`_complete_physical_receipt_row_count`).

The shipped tests independently prove the same class of properties (proof 1
byte-identity under 4 workers, proof 2 mixed resume, proof 3a corrupt-shard
abort with mid-run pool termination, proof 3b receiptless-payload /
replay-without-episode / `.partial` remnants recomputed) — all re-run and
green.

Eligibility-rule fidelity: the prewarm admits a date iff it is in
`L2_ANALYSIS_DATES`, its quality assessment is PASS, it is not in
`L2_KNOWN_EXCLUDED_DATES`, every physical shard receipt is COMPLETE, and the
conserved physical row-count sum is > 0 — a faithful mirror of the
sequential `included` rule plus the `source_count<=0` empty-capture demotion
(l2.py lines 1958–1966, 2005–2008). Quality-receipt errors skip the date
(sequential: refuses it). Any residual divergence is caught by the
byte-identity proofs and, at runtime, by the sequential chain's own
fail-closed revalidation of every reused partition.

## 4. Memory claim — PASS (with stated provenance)

- `L2_EPISODE_WORKERS = 4` (runner line 168), asserted by
  `test_worker_count_is_memory_bounded_and_stage_names_match_l2`.
- Service limits verified in `deploy/w09/w09-exploratory-autoresearch.service`:
  `MemoryHigh=40G`, `MemoryMax=52G`.
- Math checks out: 6 workers × ~10GB ≈ 60GB > MemoryMax=52G (and > the ~45GB
  planning bound) ⇒ 6 rejected; 4 × ~10GB ≈ 40GB = MemoryHigh soft ceiling,
  < MemoryMax. The ~10GB-per-partition figure's provenance is the live .12
  run observation (commit message + runner comment); it is an operational
  measurement this offline audit cannot re-take, but it is the conservative
  input to a bound that still leaves 12GB to MemoryMax.
- `maxtasksperchild=1` confirmed (runner line 450): every partition gets a
  fresh spawn-context process, no cross-partition RSS accumulation.
- Per-worker scratch isolation confirmed: each spec gets
  `scratch_root/<partition_key>`; `_configure_duckdb` does a fresh
  `mkdir(mode=0o750)` (fails on reuse) and sets a per-session
  `temp_directory`; `scratch_root` is recreated before and removed after a
  successful prewarm (asserted by the shipped proof 1).
- Fixture-scale guard: the shipped test bounds peak child RSS < 4GB.

## 5. Release chain .13 consistency — PASS

- Gate: `W2A/W0/W1_RELEASE_ID = D3-{W2A,W0,W1}-2026-07-19.13`; arm:
  `RELEASE_ID = D3-W2A-2026-07-19.13`; service ExecCondition/ExecStart point
  at `D3-W0-20260719-13.json` / `D3-W1-20260719-13.json`; tests assert the
  same strings.
- Stale-id sweep: `grep -rn` for `2026-07-19.11`, `2026-07-19.12`,
  `20260719-11`, `20260719-12` across `deploy/` and `tests/` returns
  **zero** matches.

## 6. Refreshed L2 independent audit receipt

`docs/plan_audits/DEEP03_FULLSCOPE_L2_INDEPENDENT_AUDIT_RECEIPT_2026-07-19.13.json`
is the .12 receipt with exactly four fields changed:
`audited_runtime_commit = bc761cdfb1603877ebcb3d35413d770294f42e5a`,
`audited_modules_sha256` recomputed from the working tree (only the runner
line differs: `44fc25e8…`; methods/graph/l2 unchanged),
`audit_report_sha256` = sha256 of this report file, and
`auditor_id = "claude-independent-fullscope-13-auditor-2026-07-20"`.
Validated by the auditor against the runner's contract: field set ==
`L2_AUDIT_FIELDS`, bytes == `_canonical_json(receipt)`, and every `fixed`
gate value in `_validate_l2_independent_audit` matches (the 0444/root-owner
file checks apply at deploy time on W09, as with .12).

## Caveats (none blocking)

1. The ~10GB/partition RSS figure is inherited from the live .12 run's
   observation, not re-measured here (offline audit; no SSH). The resulting
   40GB projection still has 12GB headroom to MemoryMax and workers are
   additionally bounded by `maxtasksperchild=1`.
2. Installing .13 on W09 still requires the deploy-side gate artifacts
   (0444 root-owned receipt/report copies, refreshed AUTHORITY/ARM with the
   new prerequisite sha256s and operator text) — out of this audit's scope,
   enforced fail-closed by `deep03_authority_gate.py`.
3. Nothing in this audit was committed; per instructions the only writes are
   this report and the .13 receipt alongside it.
