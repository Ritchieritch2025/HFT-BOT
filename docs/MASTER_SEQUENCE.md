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
- 2026-07-10 — STEP 6 drafting DONE (PLAN_PRICING_MODEL + PLAN_RISK_KILLSWITCH,
  combined P9 audit applied; see SESSION_LOG). **Operator sequencing ruling
  (2026-07-10):** PLAN_PRICING_MODEL Group M is approved to start AHEAD of
  the post-gate default — one W per session, order W-P1→W-P2→W-P3→W-P4;
  PLAN_RISK_KILLSWITCH W-K1..K5 interleave AFTER Group M; W-K6 stays
  operator-gated and operator-scheduled (S1/S3). Group C remains gated
  (7 clean days + recv-clock exclusive; pre-ladder shape-only option
  REFUSED — see the plan's gate box).
  [SUPERSEDED on ordering by the 2026-07-10 resequencing ruling below;
  Group-C gates and W-K6 clauses unchanged. W-P1 executed and audited
  BEFORE the resequencing — it stands.]
- 2026-07-10 — **RESEQUENCING RULING (operator, verbatim): 框架优先,数学
  后迭代。① M 组余下 W(P2..P4)让位排后;② 优先执行:K1..K5(风控/
  kill switch 非实盘部分,含五层预留账本)→ Phase 2 引擎/shadow 接线
  前置(World A/B merge,按 ARCHITECTURE_REVIEW 与 PLAN_LIVE_VALIDATION
  既有定义),接口遵守 DESIGN_HOTPATH_EXECUTION §4/§5.5(同流三模式、
  策略可插拔、零分配验收);③ P2..P4 在 shadow 底盘跑通后穿插回归,
  校准组照旧等 recv 数据。不变项(重申,非商量):S1-S6、悲观口径、
  shadow 5 绿日、W-K6 与一切实盘动作由我排期。**
  Executor's reference mapping (the design doc has no numbered §5.5; the
  operator's three terms map to): 同流三模式 = one pipeline serving
  data_collect / shadow / live via KALSHI_MODE (S2 fail-closed defaults);
  策略可插拔 = the strategy-roster plug interface (test_strategies
  contract); 零分配验收 = hot-path zero-unbounded-allocation acceptance
  (E7 + DESIGN §4 contracts #2/#3 OrderSlot/prebuilt-template era).
  Effective queue: W-K1 → W-K2 → W-K3 → W-K4 → W-K5 → World A/B merge /
  shadow wiring (its own plan, drafted against ARCHITECTURE_REVIEW +
  PLAN_LIVE_VALIDATION) → W-P2..P4 interleave as regressions on the
  running shadow chassis → Group C when recv-era data + gates allow.
