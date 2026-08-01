# WO-F — Platform: CI, spec-drift, doc/plan consolidation (PLAN_PROD_V1 P3+P4 + drift fixes)

Branch: `wo-f-platform` off `main`. Infra only — touches no engine code.
Read first: `.attic/docs/PLAN_PROD_V1.md` §P3/§P4, `docs/PLAN_TOKEN_RULES.md`
§T7/T9, `tools/check_gates.sh`, `tools/check_registry.py`, `tests/run_pipeline.sh`.

## Context — audit deltas (2026-07-18)

- `docs/PLAN_PROD_V1.md` is referenced by README and ARCHITECTURE but lives only in
  `.attic/docs/` — the master roadmap is invisible to agents working off `main`.
- `work/lifecycle_status.json` shows `kalshi_spec_sync = fail (stale)` — spec drift
  is already biting and there is no vendored spec or drift check on main.
- The backtest/warehouse pipeline exists only on worktree branches; near-done
  branches (`pipe-w05-ui-data-root`, `w06-stage1`) sit unmerged; three worktrees
  are prunable. Main has no CI, so nothing enforces green on merge.

## Tasks

1. **Restore the roadmap**: move `.attic/docs/PLAN_PROD_V1.md` → `docs/`, with a
   status header (P0–P2 DONE with commit shas; P3+ open; pointer to
   `docs/workorders/`). Fix any README/ARCHITECTURE drift while there.
2. **P3 CI** — `.github/workflows/ci.yml`, Ubuntu 24.04: install `g++-12
   libcurl4-openssl-dev libssl-dev python3`; `make -j` → `./tests/run_pipeline.sh`
   → `make san` → `make tsan` → bounded `make fuzz` (~50k iters). Upload
   `work/test_results.ndjson` + logs as artifacts. Badge in README. Verify red-on-
   broken once (deliberate break on a scratch branch), then revert.
3. **P4 spec vendoring + drift check**: fetch OpenAPI/AsyncAPI from
   docs.kalshi.com into `docs/vendor/kalshi/latest/` with a `FETCHED_AT` stamp;
   `tools/check_spec_drift.sh` re-fetches, diffs, nonzero-exits with a summary.
   Grep the diff specifically for: `use_yes_price` (code pins false; Kalshi
   announced a default flip), account-limits schema, WS error-code enum, endpoint
   cost paths, and `/portfolio/orders?client_order_id=` (WO-C depends on this
   param — flag it to WO-C's agent once vendored). Wire as a **non-blocking
   weekly scheduled CI job** — drift alerts must not block unrelated PRs.
4. **Endpoint-cost refresh (T1.4)**: control-thread helper re-fetches
   `/account/endpoint_costs` hourly and on `token_accounting_drift` (the stderr
   hook in `src/request_executor.cpp` already fires — add a callback seam only;
   if this edges into engine code, keep it to the seam and hand anything bigger
   to WO-B's agent to avoid ownership conflicts).
5. **T9 rulebook**: write `docs/KALSHI_RULEBOOK.md` — RULE-ID / statement /
   source / implemented-in / tested-by / severity for every F1–F12 fact plus the
   measured batch-cost findings (see `4977de3`, `0613e79` commits). Leave explicit
   `UNMEASURED` rows for the P5 empirical items (batch cost tiers, get_snapshot
   seq semantics, 24 h soak) so the verification campaign has a checklist.
6. **Branch hygiene report** (report, don't act): one markdown table of all
   unmerged branches with tip date, diffstat-net-of-data, and a
   merge/park/delete recommendation — flag `pipe-w05-ui-data-root` (near-done) and
   `w06-stage1` (done, deploy-gated) as merge candidates for the operator.
   Do NOT delete or merge anything yourself; worktrees belong to live agents.

## Tests / acceptance gate

CI green on a PR touching a source file; deliberate-break turns it red (verified,
then reverted). Drift check dry-run: modified fixture yaml → nonzero exit.
Cost-refresh unit test via mock 429 scenario. Rulebook committed with zero
unreferenced F-facts. `make check` stays green throughout; registry updated for
any new tool. Hygiene report delivered to `docs/workorders/BRANCH_HYGIENE.md`.
