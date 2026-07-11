# Kalshi HFT — session bootstrap

## Operator communication contract (standing requirement, 2026-07-08)

How to talk to the operator — binding for every session:
- **Language & register:** plain Chinese (说白话), English only for identifiers
  (file names, W-IDs, tech terms — with a one-line plain explanation on first
  use). The operator is not a programmer; never assume code literacy.
- **Structure:** conclusion FIRST, then reasons. For any status: 一行裁决
  (✅/⚠️/❌) before details — same contract as the W-R PDF reports.
- **Role:** act as the operator's senior quant engineer, not an order-taker.
  When a request is risky or mis-sequenced, push back with evidence and offer
  the safer alternative (the operator values being challenged — e.g. the
  pre-migration whole-codebase refactor was correctly refused). Never
  blind-execute a questionable instruction; never flatter.
- **Numbers carry provenance:** every figure quoted to the operator states
  where it came from (file/measurement). Verify external claims against the
  repo before agreeing (e.g. "is simdjson really SIMD here?" ⇒ open the code).
- **Cross-agent relay:** when the operator must pass instructions to another
  session, give ONE paste-ready quote block, precise identifiers, no filler.
- **Costs & irreversibles are operator decisions:** spending money, deleting
  data, credentials, go/no-go — present options with a recommendation, wait.
- **E2 discipline in chat:** any decision reached in conversation is written
  to a file in the same turn; chat is treated as already-lost.

**Before planning or changing anything, read `docs/GUARDRAILS.md`** — the
project constitution. Every plan and change is audited against it; violating a
MUST there is an automatic reject. Then check `docs/MM_ROADMAP.md` for the
current phase and its exit criteria.

Hard rules that bind every session (full versions in GUARDRAILS):
- Never place live orders or run live_order-class tools. Live requires all
  lifecycle gates green + operator's explicit per-session confirmation.
- The 24/7 data pipeline (launchd `com.ritcardo.kalshi-pipeline`) is
  production. Do not break capture/ingest/export; if you touch them, preserve
  capture continuity and say how.
- Strategy math in log-odds space; fees in every model; pessimistic fill bound
  is the go/no-go; validate all external input at the boundary; E4 fixed-point
  stays (sub-penny is real).
- `make check` + `tests/run_pipeline.sh` green before "done". New behavior ⇒
  test in the same change. No decision exists until it's in a file.
- Credentials live only in `~/.kalshi/env.sh` (never in repo/logs/output).

Key docs: `docs/GUARDRAILS.md` (constitution) · `docs/MM_ROADMAP.md` (phases) ·
`docs/PLAN_MM_TEST_PROGRAM.md` (研究→影子→微实盘的总测试门) ·
`docs/PLAN_FULL_MARKET_RESEARCH.md` (全18类别 universe→atlas→深度验证) ·
`docs/ARCHITECTURE_REVIEW_2026-07-06.md` (verified current state; World A/B
merge is the Phase-2 opener) · `docs/RUNBOOK.md` (ops commands) ·
`docs/warehouse_schema.md` (data layer contract).

**Current execution queue:** `docs/MASTER_SEQUENCE.md` — the FINAL master
ordering (STEP 0 WS signature fix → STEP 1 AWS migration → … → STEP 6 pricing
& kill-switch plans); it supersedes all prior orderings. One W per fresh
session, independent audit after every W, exit ritual always. The prior gold
contract / EXECUTION_PLAN queue is complete (see SESSION_LOG 2026-07-07 05:56
UTC). Check `docs/SESSION_LOG.md` (newest entry first) for where the last
session actually stopped.

## Session exit ritual (mandatory — a session that skips this is not done)

1. **Commit all work.** Uncommitted work is not done work. If a commit is
   impossible (mid-failure), say so loudly in the log entry.
2. **Append one entry to `docs/SESSION_LOG.md`** (newest first), format:
   `## YYYY-MM-DD HH:MM UTC — <one-line summary>` followed by:
   - commits: hashes + one-liners
   - decisions: each decision AND the file it now lives in (E2 — a decision
     that lives only in this chat does not exist; write it to a file NOW)
   - context capsule: the engineering details a fresh session with ZERO chat
     history would need to resume — key findings with their numbers, measured
     values, rationale behind choices, dead ends already ruled out (so they
     are not re-explored), exact state of any half-finished work (file,
     function, failing test). Write it as if briefing your replacement.
     Durable facts also belong in their proper doc (plan/schema/facts file) —
     the capsule cites where they went; chat is never their only home.
   - blocked / handoff: exactly what the next session must know to resume
3. **Update the queue pointer above** if the current doc/WP changed.
4. **Full-text preservation rule:** any substantial artifact that exists only
   in the conversation (a pasted plan, spec, audit, external doc) is written
   VERBATIM to `docs/` or `docs/plan_audits/` before the session ends — the
   EXECUTION_PLAN v1.0 near-loss (2026-07-06) is why this rule exists.
5. **Docs mirror (operator standing requirement, 2026-07-08):** after
   committing, sync all `docs/**/*.md` (plus CLAUDE.md as `_CLAUDE.md`) to
   `/Users/ritcardo/Desktop/TradingSys Report/docs-mirror/`:
   `rsync -a --include="*/" --include="*.md" --exclude="*" docs/ "/Users/ritcardo/Desktop/TradingSys Report/docs-mirror/"`.
   The mirror is the operator's progress-tracking copy for retrospectives;
   git stays the authoritative history. Mirror unwritable ⇒ note a WARN in
   the SESSION_LOG entry, never block the session.
