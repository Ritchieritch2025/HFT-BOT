# MASTER SEQUENCE

**FINAL MASTER SEQUENCE — supersedes all prior orderings.** One W per fresh
session, independent audit after every W, exit ritual always. This is the
top-level execution queue; individual plan docs (referenced per step) hold the
seven-field W definitions and self-audits.

_Preserved verbatim (operator-authored, 2026-07-07). Do not paraphrase this
file; amend only with an operator-approved edit recorded in SESSION_LOG._

---

STEP 0 — WS reconnect signature fix (URGENT, before anything ships to
EC2): handshake auth headers are signed once at startup
(ws_client.cpp:91); ixwebsocket auto-reconnect replays the stale
signature ⇒ 401 loop until hourly restart (root cause of 2026-07-07).
Fix: re-sign fresh headers before EVERY connection attempt via the
client-layer recovery ladder. Red-first MockWebSocketTransport test:
reconnect carries a NEWER signature timestamp; replay of the real 401
segment RECOVERS. P4 note; quality_log entry for the 06:00–09:00 UTC
gap (sleep thinning + stale-signature lockout; clock exonerated:
sntp 46ms + REST auth passing during the outage) in the same commit.

STEP 1 — AWS FULL MIGRATION. Draft docs/PLAN_AWS_MIGRATION.md
(seven-field Ws, §6 self-audit), then execute:
 W-A0 sizing gate: print vCPU/RAM/EBS; RAM<32GB ⇒ gold builds stay on
   Mac reading S3 (17.7GB peak), stated not wedged.
 W-A1 hardening: clone repo, deps, modern Python, chrony, systemd
   units from deploy/; SSH-only-from-operator-IP checklist printed;
   operator creates ~/.kalshi/env.sh manually (S4 — never via repo).
 W-A2 Linux validation: full battery green ON the box (mocks); one
   read-only preflight from the EC2 IP.
 W-A3 S3 vault BEFORE cutover: Mac archive + raw(3d) + catalog → S3
   (versioned, internal/product prefixes); RESTORE TEST mandatory.
 W-A4 ZERO-GAP CUTOVER (operator go/no-go): start EC2 pipeline,
   verify FRESH + climbing 10 min (brief overlap harmless — 3
   concurrent WS connections ran fine 2026-07-06), then launchctl
   unload on Mac. Rollback: Mac launchd stays installed dormant.
 W-A5 post-cutover: daily EC2→S3 sync; detectors run on EC2, alerts
   routed to dashboard-readable file (+ operator choice email/webhook,
   ask); storage/cost budget section mandatory.
 RIDERS in this step (operator-approved): (a) metrics.ndjson rotation
   EXECUTED (512MB keep 3) — on both boxes; (b) promote ALL categories
   to class_a_full_l1 in the EC2 config (full-market L1; Exotics stay
   Q7-excluded from MM candidacy — research filter not storage filter);
   (c) gitignore the two pipeline-churned config CSVs (BACKLOG item).

STEP 2 — Latency observatory backend: PLAN_DASHBOARD_OBSERVATORY.md
W-D2→D3→D4→D5 (amendment: unblocked from W-D0/D1, which now gate only
W-D6). Append TREND RULE to plan binding rules: every live statistic
renders with a visual trend (sparkline/time-series), never a bare
number — bare number = acceptance FAIL. All detectors/collectors run
on EC2 post-cutover.

STEP 3 — Dashboard redesign: W-D0 (STOP: operator approves
requirements) → W-D1 (STOP: operator approves design) → W-D6 → W-D7
end-to-end acceptance (real 401 replay ⇒ banner+incident+notification)
+ whole-plan independent audit.

STEP 4 — Depth expansion rollout ON EC2, per PLAN_DEPTH_EXPANSION +
its probe numbers, gated on explicit operator approval. Dynamic
subscription manager as a separate shadow-first process; D4 ingest
tests same change; capture continuity stated.

STEP 5 — Historical backfill: draft docs/PLAN_HISTORICAL_BACKFILL.md —
settlements first (unlocks calibration research), then trades history;
endpoints/pagination verified against docs/vendor/kalshi spec + 
kalshi_facts.yaml (no memory-based API claims); read-token budget cap;
provenance=rest_backfill columns, never silently merged with
ws_capture; resumable checkpointed crawler; first live crawl
operator-gated.

STEP 6 — Draft (plan-only) docs/PLAN_PRICING_MODEL.md (Phase 1.5
decomposed: pure-math skeleton Ws executable now — log-odds AS quote
generator, micro-price fair value, calibration-correction term,
Q9 sign tests on synthetic data; calibration Ws gated on 7 clean days)
and docs/PLAN_RISK_KILLSWITCH.md (panic CLI dry-run, typed read-only
endpoints, rule engine on synthetic scenarios; live rehearsal
operator-gated per S1/S3). Queue both for post-gate execution.

Standing gates untouched: fees OQ-1 awaits operator ratification;
2026-07-13 seven-clean-days gate; live trading remains behind ALL
lifecycle gates + per-session operator confirmation (S1).

---

## Amendments (operator-approved, dated — verbatim STEPs above unchanged)

- 2026-07-07 — STEP 0 DONE + deployed to Mac (fix live since 13:00:01 UTC;
  see SESSION_LOG). Sequence continues at STEP 1 (AWS migration).
- 2026-07-07 — **INSERT before STEP 5:** docs/PLAN_EVENT_PACKAGING.md
  (per-event data marts). Operator-approved 2026-07-07. Rationale: it pairs
  with STEP 5's settlements backfill and unlocks per-event backtesting/
  calibration. Executable now (W-E0/E1 are read-only derived research);
  W-LC (lifecycle capture) is operator-gated. Does not reorder STEPs 1–4.
