# Independent Adversarial Audit — deep03 fullscope release .12 (threads 8 / pinned L2)

**VERDICT: PASS** ✅

- Audited commit: `5d5d27836891ae22cebecae85f90f2674e579909` ("perf(deep03): .12 -- 8 DuckDB threads for B/graph, deterministic pinned L2 session")
- Parent: `289d769` — Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`, branch `w-deep03-fullscope-09`, HEAD verified, working tree clean at audit start.
- Auditor: `claude-independent-fullscope-12-auditor-2026-07-20` (independent session; read-only on code; no deploy, no commit).
- Date: 2026-07-20.

## 1. Diff confinement — PASS

`git diff --stat 289d769..5d5d278` touches exactly 11 files:

| Area | Files | Content |
|---|---|---|
| deploy/w09 | `deep03_authority_gate.py`, `deep03_one_shot_arm.py`, `exploratory_autoresearch.py`, `w09-exploratory-autoresearch.service`, both `.sha256` manifests | release ids .11→.12 (W2A/W0/W1 + service `D3-W0-20260719-12.json` / `D3-W1-20260719-12.json`), `DEEP03_THREADS = 2 → 8`, manifest hash refresh |
| runner | `tools/research/deep03_fullscope_runner.py` | `L2_PINNED_THREADS = 1`, `_l2_pinned_connection`, session split, `l2_pinned_threads` in run receipt |
| tests | `test_deep03_thread_determinism.py` (new, 546 lines), 3 existing tests updated (.12 ids, threads 8) | |

**Protected modules byte-unchanged (hard requirement):** `git diff 289d769..5d5d278 -- tools/research/deep03_v3_l2.py tools/research/deep03_v3_methods.py` is empty (0 lines). Their sha256 at HEAD equal the .11 receipt values:
- `deep03_v3_l2.py` = `1530798e9e822ba70c5d9a636fcc2bb70fdd2048a797991a67f3c3a625a3a627`
- `deep03_v3_methods.py` = `2ad2e6dcf7713b320b0d7a474fff2b4613f85e2339745f3cbf9fee65b11db064`
- `deep03_fullscope_graph.py` = `061d0cdc4921cc2eea032b24caaea5daf8185a15dfc356668940a71e31abf5aa` (also unchanged)
- `deep03_fullscope_runner.py` = `f914b804caa7b38a952e07cd359f6ba1203f9d8fcc15712da7ac94429fb56bcd` (the only changed audited module; matches the refreshed `deploy/w09/deep03_open_discovery_modules.sha256` line)

No files outside the claimed scope. No strategy-math, fee, or pipeline files touched.

## 2. Tests run by this auditor — PASS

- `tests/test_deep03_thread_determinism.py`: **3 passed** in 11.4s (run directly, this machine, Python 3.9.6 / pytest 8.4.2).
- Full tree `python3 -m pytest -p no:cacheprovider`: **1834 passed, 3 skipped, 1 xfailed** in 110s — exactly the expected 1834. Skips/xfail are pre-existing environment gates, none introduced by this diff: 2× Linux-memfd-only IAM transport tests, 1× V12 spec-drift gate (missing `work/kalshi_spec_alignment.json` in this worktree, operator-rerunnable), 1× documented settlements-export backlog xfail (2026-07-07).
- Both SHA manifests verified with `shasum -a 256 -c`: `deploy/w09/deep03_open_discovery_modules.sha256` — 15/15 OK; `deploy/w09/exploratory_autoresearch_payload.sha256` — 10/10 OK (self-consistent after the .12 edits).

The new determinism suite is a genuine adversarial proof, not a smoke test: it builds a multi-market fixture with ROW_GROUP_SIZE 512 (asserts >4 row groups so parallel scans really interleave), runs the exact production `_configure_duckdb` (including `preserve_insertion_order=false`) and the exact shipped `_l2_pinned_connection`, asserts the pinned session reports `threads=1`, and (1) proves .11-regime vs .12-regime byte-identity on every L2 stage except `l2_exact_atlas`, whose rows must be value-equal on all columns except the DOUBLE sum `total_dwell_us` within rel_tol 1e-9; (2) proves full byte self-reproducibility of the .12 mechanism (the property .11 measurably lacked); (3) proves B-stage reduced statistics are exactly equal (floats included) at threads 2 vs 8, with payload diffs confined to intra-file row order and receipt diffs confined to order-only stages.

## 3. L2 pinned session wiring — PASS

`run_fullscope_discovery` (deep03_fullscope_runner.py:1104-1146):
- B01–B04 (`execute_all_bounded`) and the market graph run on `con` configured with the CLI `threads` (8 in production per `DEEP03_THREADS`/service args, asserted by `test_w09_exploratory_autoresearch.py`).
- The full-thread session is closed in a `finally` **before** `_l2_pinned_connection` opens (explicit comment: the two 16GB buffer pools never coexist). The pinned connection is a **fresh** `duckdb.connect()` configured via the same production `_configure_duckdb` with `L2_PINNED_THREADS = 1` and its own scratch dir (`.scratch-l2` vs `.scratch`) — a startup pin, not a runtime `SET threads` (which was measured to change reduction order again; documented in the runner comment block).
- No shared mutable state between the sessions: `graph_result` is eagerly-fetched plain Python data (dicts/lists from `fetchall()`; `build_market_graph` returns a dict), `graph_source` is a file already published to disk; the B store is context-managed inside `execute_all_bounded` (`with BoundedCheckpointStore(...)`) and closed before L2 opens its own `with BoundedCheckpointStore(checkpoint_namespace, source_binding)`. `_l2_report_tables` runs all its aggregations (including `sum(total_dwell_us)`) on the threads=1 `l2_con` — deterministic. The run receipt records both `threads` and `l2_pinned_threads`.
- The pin deliberately lives in the runner, not in `deep03_v3_l2.py`, because that module's sha is part of the L2 stage ABI (`_l2_abi` → `module_sha256` → `abi_sha256` → `stage_version`); changing it would re-version every stage and orphan the live store's completed partitions. Verified: `_l2_abi` contains no thread-count field, so the pin does not invalidate existing checkpoints.

## 4. Resume against an existing store cannot mix regimes unsafely — PASS

`BoundedCheckpointStore.write_partition` (deep03_v3_methods.py:289-339) is fail-closed on reuse: a partition with a receipt is only reused after `validate_partition` re-verifies schema_version, state=COMPLETE, stage, `stage_version` (ABI-bound), partition_key, `source_binding`, plus payload path, **size, sha256, parquet schema and row count** against the receipt. A payload without a receipt (crash window) is deleted and deterministically recomputed. So on a .12 resume of a store partially completed under .11: completed partitions are reused byte-as-written (sha-verified), and every newly computed L2 partition comes from the pinned threads=1 session — deterministic (test 2). The only cross-regime variation possible in a mixed store is the atlas `total_dwell_us` float-reassociation noise (~1e-9 rel), which .11 could not reproduce even against itself (1-of-4 divergence, per the measurement record in the test docstring) — row content is proven thread-invariant (test 1). The store never promised cross-run byte reproducibility of scatter payload row order (pre-existing status quo, test 3). No unsafe mixing path found.

## 5. Release chain — PASS

`grep -rE '2026-07-19\.(10|11)|20260719-(10|11)' deploy/ tests/` → **zero matches**. All gate/arm/service/test identities are `.12` (`D3-W2A-2026-07-19.12`, `D3-W0-2026-07-19.12`, `D3-W1-2026-07-19.12`, `D3-W0-20260719-12.json`, `D3-W1-20260719-12.json`). The only remaining ".11" strings are descriptive prose in runner/test comments explaining what the .11 regime measured — correct and intentional, not identity references.

## Notes (non-blocking)

- `_configure_duckdb` does `scratch.mkdir(mode=0o750)` without `exist_ok`; a leftover `.scratch-l2` from a killed run would make a retry fail loudly. Same pre-existing behavior as `.scratch` — fail-closed, not a regression.
- The `.11` receipt's `audited_runtime_commit` was `416168cf…` (deployed release commit); per the audit instruction the refreshed `.12` receipt binds `5d5d2783…`, the audited repo commit.

## Receipt

Refreshed machine-readable receipt written to
`docs/plan_audits/DEEP03_FULLSCOPE_L2_INDEPENDENT_AUDIT_RECEIPT_2026-07-19.12.json`:
byte-copy of the `.11` receipt except `audited_runtime_commit`, `audited_modules_sha256` (runner sha only), `auditor_id`, `audit_report_sha256` (sha256 of this report file); field set validated equal to the runner's `L2_AUDIT_FIELDS` and serialization validated byte-equal to the runner's `_canonical_json` (sorted keys, compact separators, ASCII, trailing newline). Nothing committed — publication is the release owner's step.
