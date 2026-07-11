# STP_P00_CONFLICT_RISK_REGISTER — governance conflicts, statistical conflicts, risks, operator decisions needed (STP-P00-W01)

Each entry: what conflicts / what is at risk · evidence · resolution status.
OPERATOR-TBD means: stop the affected action until the operator rules; nothing
here blocks the rest of P00.

## A. Governance conflicts

### C-1 — Evidence-path divergence: prompt §31.2 vs release STP-R004-P00 (RESOLVED BY SCOPE, ratification recommended)
- Prompt §31.2 (lines 2922–2937) limits W01 evidence writes to
  `docs/plan_audits/sports_trading_program/STP_P00_*.{md,json}` + STATE +
  SESSION_LOG. Release STP-R004-P00 (line "Allowed repository writes") allows
  "the P00 evidence paths the canonical prompt itself designates
  (work/research/sports_trading_program/phase_00/**)" and reserves
  `docs/plan_audits/sports_trading_program/STP_P00_AUDIT.md` for the audit
  session only. The parenthetical characterizes the prompt's designated paths
  incorrectly — the prompt designates docs/plan_audits paths for these
  documents; `work/research/sports_trading_program/` appears in the prompt
  only as "bulky derived research" (§8).
- Resolution applied: a durable operator release (§4 authority order #2)
  governs write scope for its session; the prompt governs content. All eight
  §31.4 outputs were produced with prompt-specified content under
  `work/research/sports_trading_program/phase_00/**`. No write to
  `docs/plan_audits/sports_trading_program/` was made by W01.
- Residual: **OPERATOR-TBD (non-blocking)** — the operator should either
  ratify these evidence paths in the AUD01/next release or authorize a
  one-time relocation W. Smallest amendment: one sentence adopting
  `work/research/sports_trading_program/phase_00/**` as the canonical P00
  evidence location (or granting the docs/plan_audits STP_P00_* writes).

### C-2 — "§43 hypotheses" has no referent in the canonical prompt (INTERPRETED, honest record)
- Release STP-R004-P00 constraint line: "Verify §43 hypotheses from code/data
  with citations; contradictions list; honest NOT-VERIFIED where evidence is
  unreachable." The pinned prompt has sections §0–§36; grep of the repo finds
  "§43" only inside the release itself; frozen V2/V2.1 also contain no §43.
- Interpretation applied (recorded, not silent): the referent is the prompt's
  enumerated verify-item family — §26.3 starting classifications and "known
  issues/limitations to verify", §26 census assertions, §10.1 GUARDRAILS
  clause verification, §10.5 H1-absence check, §30 S1/S4 burstiness
  correction check. These were enumerated as hypotheses H-01…H-43 in
  `STP_P00_REPOSITORY_AUDIT.md` §2 and each verified with citations.
- **OPERATOR-TBD (non-blocking):** confirm the referent; if the operator
  meant a different artifact, name it and a follow-up verification is a
  paper-only addendum.

### C-3 — CLAUDE.md stale authority pointer (KNOWN, P01-owned)
- CLAUDE.md still says "The referenced CANONICAL PROMPT is NOT yet in the
  repo — it must be preserved verbatim into docs/ before Phase 1 starts."
  Superseded by D-2 (2026-07-11): prompt canonicalized at pinned path/SHA.
- Resolution: prompt §29 assigns "minimally update stale CLAUDE authority
  pointers" to STP-P01. W01 is read-only toward CLAUDE.md. No action now.

### C-4 — Prompt file name/banner vs canonical status (RESOLVED by D-2)
- File is named `…V2_2_CANDIDATE.md` and carries "STATUS: CANDIDATE — NOT
  CANONICAL — NOT EXECUTABLE". D-2 归档注记 rules the banner creation-time
  metadata superseded by release pinning; bytes must never change (any byte
  change = new candidate). Sessions must not refuse the prompt over the
  banner. No open conflict; recorded so AUD01 does not re-flag it.

### C-5 — MASTER_SEQUENCE vs sports program ordering (NO CONFLICT, boundary restated)
- MASTER_SEQUENCE remains engineering authority (D-1); its current effective
  queue (post-2026-07-10 resequencing: World A/B merge next, W-P2..P4 as
  regressions, Group C gated) does not order STP phases. Prompt §29 keeps
  STP-P08 subordinate to MASTER_SEQUENCE. Future risk only: when STP-P08 is
  reached, the World A/B merge W and STP-P08 must be mapped 1:1 before
  release (P01 task: map STP phases to engineering Ws).

## B. Statistical conflicts

### C-6 — PLAN_RESEARCH_CYCLE_1 S5 split design vs prompt §20 split manifest (P02 DESIGN DECISION NEEDED)
- S5 (lines 135–152): selection frozen on earliest EC2-era segment, val =
  middle, test = last 2 days opened once. Prompt §20.1 requires a four-way
  hash-sealed manifest (EXPLORATORY_ONLY/TRAIN/VALIDATION/
  HISTORICAL_CONFIRMATION) with prior-exposure ledger forcing every exposed
  root event into EXPLORATORY_ONLY, chronological + embargoed.
- Consequence: S1 dev-grade windows 2026-07-06..08 (and any data whose PnL/
  markouts were inspected: S1 outputs, mm_calibrate/mm_research/gate_calc
  runs, the 2026-07-10 zero-base audit's S1 evidence) are prior-exposed and
  can never enter VALIDATION/HISTORICAL_CONFIRMATION for the sports program.
- Resolution: not W01's to decide. STP-P02 must build the prior-exposure
  ledger FIRST and reconcile/supersede the S5 split for STP purposes.
  OPERATOR decision needed only if S5 (MM-program lane) and STP-P02 (sports
  program lane) are to share sealed splits.

### C-7 — Provisional numeric gates await power analysis (STANDING, D-1)
- 7 clean days / 5-of-7 / 25% concentration / ×1.5,×2 stress remain BINDING
  PROVISIONAL (D-1; prompt §10.5). Prompt §20.4 requires a frozen power
  analysis before HISTORICAL_CONFIRMATION. Risk: treating provisional gates
  as final. No action in P00; P06/P07 owner.

### C-8 — Old bootstrap convention vs prompt §20.3 (KNOWN, P05/P07-owned)
- Existing tools bootstrap per match/event (maker_edge ≥1000 per-match block
  bootstrap; PLAN_MM_TEST_PROGRAM §3 "按整场比赛 block bootstrap") while
  prompt §20.3 requires chronological calendar-day blocks containing complete
  events, studentized, ci95_lower. §26.3 already flags: "old event bootstrap
  must be reconciled with the new chronological-block contract before binding
  reuse." Classification consequence applied in the reuse matrix
  (DIAGNOSTIC_ONLY for the old statistical conclusions).

## C. PnL/fill risks (verified, carried into matrix + data capability)

### C-9 — Old fill simulators cannot produce binding PnL
- mm_backtest.py: float cash/inventory, maker fee 0, no settlement, residual
  at last midpoint, immediate stale-quote removal, no cancel lifecycle,
  placeholder latency (evidence: REPOSITORY_AUDIT H-19…H-28). All PnL from it
  = DIAGNOSTIC_ONLY/NON-GATE until W-FS1-class governed simulator (STP-P05)
  exists. Risk of accidental reuse is mitigated by MM_ROADMAP note + plan
  language + this register.

### C-10 — config/backtest_latency.yaml placeholders
- Placeholders remain (S2 signing measured on Mac-era? — see REPOSITORY_AUDIT
  H-28 for measured-vs-placeholder status). PLACEHOLDER cannot support a gate
  (prompt §16). Owner: S2/S3 measurement tasks + STP-P04.

## D. Holdout contamination

### C-11 — Prior human exposure ledger does not exist yet
- Exposed-so-far inventory (to seed the P02 ledger): Tennis 2026-07-06..08
  (S1 dev-grade full pipeline incl. PnL/markout), all-category mm_calibrate/
  mm_research/mm_scan/gate_calc outputs over 2026-07-06..10 era days,
  BADAMS cricket episode 2026-07-06, Muchova–Gauff export (outputs/,
  2026-07-10), operator's own live trading knowledge (accounts, D-1.1 —
  operator saw real fills/settlements incl. MLB COLSF-SF and ITF SHIROB
  2026-07-11). RISK: any of these entering VALIDATION/CONFIRMATION.
  P02 must force them EXPLORATORY_ONLY.

## E. Production coupling

### C-12 — Production pipeline is on EC2; local tree is a stale mirror
- Production capture/ingest/export runs on EC2 (systemd kalshi-pipeline)
  since 2026-07-09 cutover; the Mac tree's work/ layers go stale after
  2026-07-09/11 (measured cutoffs in STP_P00_DATA_CAPABILITY_MATRIX). P00
  touched neither. Risk for P02+: research sessions must state which host's
  data they consume; EC2-era recv-clock data is the only go/no-go-eligible
  source (W-TL1 + Group-C gate box). The Mac launchd rtt-baseline sampler
  still writes work/latency_baseline/* locally (isolation artifact R2).

### C-13 — live-capable surfaces census (risk containment)
- 5 registered live_order entries (bench_order, account_upgrade, panic_live,
  fill_test, tradingd) + preflight --order argument mode (registry-documented
  live_order semantics inside a network_read entry) + UNREGISTERED
  `apps/live_e2e.cpp` (no Makefile/CMake rule, no tools.json entry — build
  requires a manual compile). All DO_NOT_USE for STP (matrix). The prompt's
  "no third live order path" is architecture doctrine; existing utilities
  stay but must reuse audited primitives or remain DO_NOT_USE.
- Registry/build mismatches surfaced: `preflight --order` mode makes one
  binary dual-class (network_read entry with a live_order argument mode —
  documented in its registry description; acceptable but AUD01 should note);
  `live_e2e.cpp` is the only main() with zero registry/build coverage —
  E3 drift, needs either registration+classification or explicit retirement
  (OPERATOR-TBD, engineering W, not P00's write scope).

## F. Missing authority / operator decisions needed (consolidated)

| id | decision | owner/when |
|---|---|---|
| C-1 | ratify P00 evidence location (work/research/...) or authorize relocation | operator, AUD01/next release |
| C-2 | confirm "§43" referent | operator, next release |
| C-13 | live_e2e.cpp: register (DO_NOT_USE class) or retire | operator + engineering W |
| OQ-1 | fee facts ratification (kalshi_facts.yaml verified=false blocks gate-mode fees) | operator, standing |
| W-K6 | live kill-switch rehearsal scheduling + funding policy after D-1.1 balance $34.41 | operator, standing |
| D-2-residual | none — D-2 complete | — |
| P01 | STP-P01 release (MASTER creation, CLAUDE pointer cleanup, spec pinning) — NOT authorized by a P00 PASS | operator, future |
