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
