# Kalshi HFT — session bootstrap

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
`docs/ARCHITECTURE_REVIEW_2026-07-06.md` (verified current state; World A/B
merge is the Phase-2 opener) · `docs/RUNBOOK.md` (ops commands) ·
`docs/warehouse_schema.md` (data layer contract).

**Current execution queue:** `docs/EXECUTION_PLAN.md` (resume at the earliest
unfinished WP), then `docs/PLAN_GOLD_DATA_CONTRACT.md` (W1→W5; W6 probe is
operator-gated). Check `docs/SESSION_LOG.md` (newest entry first) for where
the last session actually stopped.

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
