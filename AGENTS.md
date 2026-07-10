# AGENTS.md — binding entry point for ALL coding agents (Codex, Claude Code, others)

You are working in a production trading-system repository. Before doing
ANYTHING, read these two files in full — they are the constitution and the
current execution queue:

1. `CLAUDE.md` (repo root) — session bootstrap, operator communication
   contract, session exit ritual. Everything in it applies to you exactly as
   it applies to Claude: if you are an agent working in this repo, "Claude"
   means YOU.
2. `docs/GUARDRAILS.md` — the invariant set. Violating a MUST there is an
   automatic reject of your work, regardless of quality.

Non-negotiable digest (full versions in the files above):

- NEVER place live orders or run live_order-class tools (S1).
- The 24/7 data pipeline is production. Do not break capture/ingest/export;
  if you touch them, preserve capture continuity and say how (P4).
- Strategy math in log-odds space; fees in every model; pessimistic fill
  bound is the go/no-go; E4 fixed-point stays (Q1-Q3, D5).
- `make check` + `tests/run_pipeline.sh` green before "done"; new behavior
  ships a test in the same change (E1).
- No decision exists until it is in a file (E2). Chat is already-lost.
- Credentials live only in `~/.kalshi/env.sh` — never in repo/logs/output (S4).
- One W per session; independent audit after every W; session exit ritual
  (commit + SESSION_LOG entry) is mandatory (see CLAUDE.md).
- Cross-agent audit rules: `docs/AUDIT_PROTOCOL.md` — executor and auditor
  must be different agents; auditors RUN acceptance, never just read it.

Current execution queue: `docs/MASTER_SEQUENCE.md`. Where the last session
stopped: `docs/SESSION_LOG.md` (newest first).
