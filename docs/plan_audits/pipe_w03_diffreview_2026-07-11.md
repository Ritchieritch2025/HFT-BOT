# PIPE-W03 Light Diff-Review Audit(独立代理,只读 diff 审阅;逐字归档)

- Date: 2026-07-12 ~00:3xZ · Reviewer: fresh zero-context agent (did not implement)
- Under review: branch `pipe-w03` commit `a8e6b4b` vs base `7b575e0`
- Mode: read-only `git show`/`diff`/`grep` only; no test reruns (operator-specified light audit)

---

**一行裁决:✅ PASS** — scope matches the five release deliverables exactly, the fixes are correct and additive (no gate weakened, no previously-valid seal is refused or vice versa), the 7 new tests genuinely pin the claimed behaviors including a faithful old-vs-new scanner regression. Four P2 notes recorded below; zero P0, zero P1.

Reviewed: `git diff 7b575e0..pipe-w03` (8 files, +630/-32), read hunk-by-hunk plus full surrounding context for `verify_raw_caught_up`, `discovery_completeness`, and `pipeline_supervisor.sh`'s pause helpers and their call sites. No commands run beyond read-only `git show`/`diff`/`grep`; nothing on disk was modified.

## 1. Scope

Exactly the five release deliverables, nothing else:
- `docs/plan_audits/pipe_w03_rootcause_2026-07-11.md` (new, root-cause writeup)
- `tools/ingest.py` (scanner window fix, 23 lines)
- `tools/warehouse_common.py` (new shared `day_sealed()`, 19 lines)
- `tools/prune_raw.py` (delegates `_sealed` to `day_sealed()` — refactor only, behavior verified byte-identical, see §2)
- `tools/export_day.py` (all-failures reporting + `discovery_completeness`, +76/-15... actually +76 net incl. new function)
- `tools/pipeline_supervisor.sh` (B4 pause-ownership token, +61/-14)
- `tests/test_pipeline_contract.py` (7 new tests, +265, additive only — no existing test touched)
- `docs/BACKLOG.md` (B4 marked FIXED with pointers to implementation+tests)

No unrelated churn found. No existing test weakened or removed. No existing gate's assertion loosened.

## 2. Scanner change (`raw_files_to_scan`)

```python
days = {yesterday.isoformat(), today.isoformat()}
...
for entry in os.listdir(cfg["raw_root"]):
    m = day_dir_re.match(entry)
    if m and not wc.day_sealed(cfg["warehouse_root"], m.group(1)):
        days.add(m.group(1))
```

- Yesterday+today are seeded unconditionally, before the unsealed-day scan — matches pre-W03 behavior exactly regardless of their seal status (edge case: no regression if today/yesterday happen to already carry a seal).
- `day_sealed()` requires `status=="SEALED" and version==2`; anything else (missing seals dir, missing file, corrupt JSON, legacy `version` other than 2) reads as unsealed → stays in-scan. Fail-closed by construction (docstring states this explicitly), consistent with D2.
- "Mixed sealed state" isn't a real category here: seals are written atomically per date via `tmp + os.fsync + os.replace` (confirmed in `write_day_seal`), so a day is binary sealed/unsealed, never partial.
- Performance: for a day whose files are already fully checkpointed, `process_file` does one SQL lookup + `os.getsize` + an open/seek/read-empty (confirmed by reading `process_file`, lines 517-534) — cheap, no full-file re-read. The real bound is *how many unsealed legacy day-dirs accumulate*, which is currently 1 (`date=2026-07-09`, 41 files, per the writeup's live example). There is no hard code-level cap on this — if seal-chain itself broke silently for weeks, the scan set would grow unbounded. The writeup explicitly names the mitigant (03:00 seal alarm bounds exposure to ~1 day) rather than a code cap. **P2** (not blocking): no enforced ceiling on legacy-unsealed-day count scanned per 60s cycle; relies on the pre-existing alarm as backstop.
- `prune_raw._sealed` now delegates to the same `day_sealed()` — read the diff: byte-for-byte the same isfile/open/json.load/status+version check that was inlined before, just deduplicated. Verified this is a pure refactor with the corrected root-cause claim "behavior unchanged."

## 3. `export_day.py` changes — additive-only check

**`verify_raw_caught_up`**: a new first pass collects every `checkpoint != size` file into `behind` and raises one combined error if non-empty, *before* the original per-file loop (empty-file check, trailing-newline check, TOCTOU stat/sha256, proof-building) runs. I traced the full function post-diff (not just the hunk) — the original loop is untouched below the new pass. Net effect: **for any single case, the final accept/reject outcome is unchanged** — a day that was previously accepted is still accepted (the new pass is a no-op when nothing is behind); a day that was previously rejected is still rejected (only the specific error message/ordering can differ when *multiple* simultaneous problems exist — e.g. one file "behind" and a different file "empty" — because the new pass now always reports the behind-file class first). This is a diagnostics change, not a gate-semantics change. No case exists where old code passed and new code fails, or vice versa.

**`discovery_completeness()`**: called only from `write_day_seal` (with `proof_files`/`proof_ckpts` derived from `raw_proof`, which by construction already has `checkpoint==size` for every entry — `verify_raw_caught_up` guarantees this) and from `main()`'s `--check-caught-up` branch (placed *after* the success path, printing extra lines, never touching the return code). So `undiscovered_files`/`files_discovered != files_present` can never actually fire at seal time — the field that does real work is `exchange_day_hours_missing_on_disk` (capture holes, no file at all for a given hour), which is genuinely new evidence. The docstring's claim "Evidence only: the hard gate remains verify_raw_caught_up" checks out against the call sites. **No REFUSE/ACCEPT flip is possible from this addition.**

## 4. Supervisor B4 ownership logic

`acquire_export_pause`/`release_export_pause` replace the old unconditional `touch`/`rm -f`. Traced against call sites:
- Single-instance guard (`$LOCK` dir + pid file, pre-existing) means only one supervisor process runs; `SEAL_PID` guards against a second concurrent `run_seal_chain` background job from the *same* supervisor. So there is no legitimate concurrent-writer race on `export_pause` other than an operator's manual write — the check-then-write in `acquire_export_pause` is not atomic (`[ -f ]` then `echo >`), so a manual `touch`/write landing in that narrow window could theoretically be clobbered. **P2**: not atomic (no `mkdir`/`flock`-style test-and-set), but the window is a few milliseconds and the actor (operator) is a human, not a competing automated process — low real-world risk, consistent with how the rest of the pipeline already signals via plain files.
- Stale-pid reclaim: `kill -0 "$owner_pid"` correctly distinguishes a live vs dead chain, logs loudly on reclaim (`"[supervisor] reclaiming stale seal-chain export_pause"`), and only then overwrites with a fresh token — this is the intended recovery path for "chain died mid-window, pause orphaned."
- Foreign-pause refusal: when the pause is either token-less or belongs to a *live* PID that isn't reclaimable, `acquire_export_pause` returns 1, and the `if acquire_export_pause; then ... fi` wrapper means `stop_ingest_for_export`, `release_export_pause`, and `ingest_alive || start_ingest` are **all** skipped — the chain touches nothing. Whether ingest stays paused during this window is governed by pre-existing, *unmodified* code (`[ -f "$LIVE/export_pause" ] && continue` in the watchdog; `[ -f "$LIVE/export_pause" ] || ingest_alive || start_ingest` in the main loop) — both key off mere file-existence, not ownership, so a foreign pause correctly keeps ingest down everywhere in the script, not just inside the chain. This is the right fail-safe (avoids exactly the DuckDB lock contention the original B4 incident produced) and is a genuinely coherent design across the three loops.
- bash 3.2 vs prod: `${BASHPID:-$$}` — in bash ≥4, `BASHPID` reflects the actual backgrounded subshell's PID (matches `$!` captured by the caller as `SEAL_PID`), so acquire/release/reclaim all key off the correct PID. On bash 3.2 (no `BASHPID`), `$$` inside a backgrounded subshell stays the *parent* shell's PID, not the chain's own — the comment's claim "ownership match stays exact either way because release greps the same expansion" is true for the acquire/release self-consistency, but it silently weakens **stale-reclaim** correctness on such hosts (a truly-dead chain's token would still show the ever-alive parent PID as "live", so `acquire_export_pause` could refuse forever until the whole supervisor restarts). The comment scopes this to "bash 3.2 test hosts" only. **P2**: worth an explicit one-line confirmation that the EC2 production host's `#!/usr/bin/env bash` resolves to bash ≥4 (virtually certain on modern Linux, but "assumed" not "verified" per the comment's own wording) — not blocking since it degrades to fail-*safe* (refuses exports) not fail-open.

## 5. Implementer's four scrutiny points — assessed

1. **Backlog auto-ingest on deploy (2026-07-09)**: correctly disclosed in the writeup's "仍开放" list — after deploy the scanner will auto-checkpoint 07-09's stray hours, but sealing that back-date is *not* automatic (the chain only ever calls `run_seal_chain "$YESTERDAY"`), so whether/how to seal 07-09 is explicitly left as an operator decision. Correct scoping — not a code defect.
2. **"Behind" vs "empty" wording**: as analyzed in §3, the reordering only changes which error string surfaces when multiple simultaneous problems exist on the same day; it never changes accept/reject outcome. Benign.
3. **Discovery evidence not seal-blocking**: confirmed by call-site tracing in §3 — `discovery_completeness` only ever sees already-caught-up proof data; it cannot cause a false REFUSE or false ACCEPT.
4. **Unproven PID identity (90986/90988)**: the writeup itself flags this as an honest boundary (no timestamps in surviving logs, no journalctl access under this read-only session) and explicitly states the design-flaw conclusion (bare `duckdb.connect` at startup, no retry) doesn't depend on resolving which process held the lock. Appropriately caveated, doesn't weaken the fix.

## 6. Tests — do they pin the claims?

Read all 7 new tests in full (not just names):
- `test_unsealed_old_day_rotated_raw_stays_discoverable` — genuinely faithful regression: it inlines the *literal* pre-W03 glob logic (`for d in (today-1, today): glob(...)`) inline in the test, asserts the old-window reconstruction misses both the base file and its `.1` rotation shard, then asserts the new `raw_files_to_scan` catches both, still covers yesterday, and still excludes a sealed 4-days-old day. It then runs an end-to-end `process_file` + checkpoint-byte-exact assertion. This is a strong, non-trivial regression test.
- `test_caught_up_failure_reports_every_behind_file` — asserts `"2 file(s)"` and both absolute paths appear in the raised message; pins the all-failures-reported behavior precisely.
- `test_seal_evidence_records_per_family_discovery` — rides the pre-existing `exported_day` fixture (not new scaffolding), asserts `files_present`/`files_discovered`/per-hour buckets/`exchange_day_hours_missing_on_disk` on a real seal write.
- `test_discovery_completeness_is_per_family_and_shard_aware` — direct unit test proving family-split (`firehose` vs `l2`), shard aggregation into one hour bucket, and per-family `undiscovered_files` — forward-compat with the planned `l2_` family, as claimed.
- The three pause-ownership tests extract the **actual** `acquire_export_pause`/`release_export_pause` function bodies verbatim out of the production shell script via string-slicing (`_pause_functions_script()`) and execute them in an isolated `bash -c` subprocess against a scratch `$LIVE` dir — this means the tests exercise the real production code, not a reimplementation, eliminating drift risk. Scenarios covered: foreign pause (both token-less content and empty-file `touch`) refused and left untouched by both acquire and release; normal acquire→release lifecycle; stale dead-pid reclaim (uses `( : ) & dead=$!; wait "$dead"` to guarantee the PID is actually dead before use — avoids flakiness); live-chain pause not stolen (`sleep 5 &` stand-in, verified via return code + message).
- `test_seal_chain_pause_ownership_static_contract` — a structural assertion that the old unconditional `touch "$LIVE/export_pause"` / `rm -f "$LIVE/export_pause"` pattern is gone from the whole script, that `acquire_export_pause`/`release_export_pause` appear inside `run_seal_chain` in the correct order (acquire < stop_ingest < release < restart), and that `rm -f "$pause"` occurs exactly once in the entire file (inside `release_export_pause` only) — this directly guards against a future patch silently reintroducing the unconditional-delete bug.

All 7 tests pin real, specific behavior; none are shallow/tautological. Count matches the claimed "7 new tests."

## Findings summary

- **P0:** none.
- **P1:** none.
- **P2 (4, non-blocking, recommend noting in BACKLOG or next-W follow-up):**
  1. No enforced cap on the number of legacy unsealed day-dirs rescanned every ~60s ingest cycle; correctness is fine, growth is currently bounded only by the pre-existing 03:00 seal alarm, not by code.
  2. `acquire_export_pause`'s check-then-write is not atomic against a concurrent manual pause write (narrow, human-timescale race, pre-existing file-signaling pattern elsewhere in this script).
  3. Stale-reclaim correctness silently degrades on bash <4 (no `BASHPID`) — comment scopes this to test hosts; recommend a one-line confirmation of the EC2 host's bash version rather than assumption.
  4. `2026-07-09` sealing path is an explicit open operator decision, not resolved by this W (correctly disclosed, not a defect).

**Verdict: PASS.** Recommend proceeding to local merge into `codex/pipeline-recovery-hardening` per the receipt's stated flow, and letting Stage-1 implementation W unlock as specified in the release.
