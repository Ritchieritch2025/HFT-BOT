# STP_P00_AUTHORITY_MAP — document/section → scope → current authority (STP-P00-W01)

Evidence basis: complete reads this session of every document listed (line
counts in the READ MANIFEST). Format: document/section · scope · authority
status · conflicts · resolution or OPERATOR-TBD.

## 1. Constitutional / operator layer

| # | Document (sections) | Scope | Current authority | Conflicts / notes |
|---|---|---|---|---|
| A1 | `docs/GUARDRAILS.md` (S1–S6, D1–D6, Q1–Q9, E1–E7, P1–P9, §6 checklist) | All safety, data-integrity, engineering, process rules | **BINDING #1** (prompt §4). Verified: file contains exactly the clause IDs the prompt's §10.1 classification names; there is **no H1 clause** (prompt §10.5 confirmed — "H1" is hypothesis H1 of `docs/research_notes/…HYPOTHESES…`, see SESSION_LOG 2026-07-09 23:30). sha256 `9c71a5b3…832b`, untouched by W01 | §10.1 KEEP/STAGE-GATE/REWRITE table is a **proposal only** — not applied |
| A2 | `docs/PLAN_SPORTS_TRADING_DECISIONS.md` (D-1, D-1.1, D-2) | Adjudicated sports-strategy facts | **BINDING #2** within stated scopes. D-1: mainline = pre-game market-dynamics spread capture; Tracks A/B/C; no MLB pre-selection; external tools all deferred/per-item operator approval; provisional numeric gates (7 clean days, 5/7, 25%, ×1.5/×2) stay in force labeled PROVISIONAL. D-1.1: shared account; 100%-automation target with manual space; four execution guardrails → P08/P10 design. D-2: V2.2 = sole canonical prompt (path/SHA pinned); D-1 §-remap (old §5→V2.2 §2+§11.2–11.4; old §11→V2.2 §4+§10); program branch @ base 1c93837; merge-to-main deferred | Append-only, verbatim-operator-only; W01 writes nothing here. D-1's "CANONICAL PROMPT 尚未入库" red flag is EXPLICITLY lifted by D-2 (后条为准) |
| A3 | Operator releases `docs/plan_releases/sports_trading_program/STP-R000…R004` | Session-scoped execution authority | STP-R004-P00 is the **only ACTIVE** release: authorizes exactly STP-P00-W01 + STP-P00-AUD01, session_count 2. R000/R001 (drafting), R002 (canonicalize), R003 (test isolation) = CONSUMED | R004 write-scope vs prompt §31.2 evidence paths → conflict register C-1 |
| A4 | `CLAUDE.md` | Session bootstrap, operator communication contract, exit ritual, authority split pointer | Binding for session conduct. Its note "The referenced CANONICAL PROMPT is NOT yet in the repo" is **STALE**: superseded by D-2 (prompt canonicalized 2026-07-11). Prompt §29 P01 task list already includes "minimally update stale CLAUDE authority pointers" — P01 item, not W01's | Stale-pointer cleanup = STP-P01, unauthorized now |

## 2. Engineering-order layer

| # | Document | Scope | Current authority | Conflicts / notes |
|---|---|---|---|---|
| B1 | `docs/MASTER_SEQUENCE.md` (STEPs 0–6 + amendments) | Engineering/infra W ordering, one-W-per-session, audit discipline | **BINDING #3** for engineering ordering. Carries NO strategy ordering since D-1. Current effective queue (2026-07-10 resequencing ruling): W-K1..K5 done → World A/B merge / shadow wiring → W-P2..P4 regressions → Group C when gated. STEP 1 (AWS) executed (production on EC2 since 2026-07-09); STEP 5 (backfill), STEP 4 (depth) pending | No conflict with STP program: STP-P00 is read-only and touches no engineering W. Future STP-P08 (execution convergence) must subordinate to MASTER_SEQUENCE (prompt §29) |
| B2 | `docs/MM_ROADMAP.md` (Phases 0–5) | MM program phase gates | Binding phase map (Phase 1→1.5 current). Not strategy authority for the sports program (that is the canonical prompt + DECISIONS until MASTER exists) | Roadmap's Phase-1 exit references old mm_backtest pessimistic gate; PLAN_MM_TEST_PROGRAM overrides its test-verdict semantics (its own §"冲突时" clause) — scoped, explicit, no ambiguity |

## 3. Sports-program layer

| # | Document | Scope | Current authority | Conflicts / notes |
|---|---|---|---|---|
| C1 | `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md` (§0–§36) | The entire sports trading program: phases STP-P00..P13, statistics, fill-model authority, reuse contract | **CANONICAL** (D-2 + STP-R002, two-gate promotion complete; sha verified this session). Governs W01 content. §31.2 evidence-path list superseded for THIS session by R004 write scope (conflict register C-1) | Release language references "§43 hypotheses"; prompt has §0–§36 only → conflict register C-2 |
| C2 | `docs/PLAN_SPORTS_TRADING_MASTER.md` | Future strategy authority | **ABSENT** (verified: file does not exist). Created only in STP-P01. Until then adjudicated strategy facts live in DECISIONS | None |
| C3 | `docs/PLAN_SPORTS_TRADING_STATE.md` | Descriptive state | **ABSENT before this W01** — created by this session per prompt §8 schema. Descriptive only; grants no authority | None |

## 4. Supporting-spec layer (prompt §30 — inspected, pinning PREPARED, not yet pinned; pinning is P01)

| # | Document (sections) | Allowed future role | Authority status | Conflicts / notes |
|---|---|---|---|---|
| D1 | `docs/PLAN_MM_TEST_PROGRAM.md` (G0–G9 gates; A1–A5 data/clock; B1–B3 reaction; C1–C6 latency; D maker actions; E W-FS1 simulator FS-1..FS-5; F1–F4 incl. F2c/FA-1/F2d; SH-1..3; RK-1..4; I1–I4; §13 order; §14 bindings) | Measurement procedures for arrivals/burstiness/reaction/latency/maker actions/fill simulator/shadow/calibration (prompt §30.1) | PLAN-ONLY, paper-only; its §13 execution order is a **candidate strategy ordering with NO automatic authority** (prompt §30.1: "Do not grant its conflicting strategy ordering automatic authority") | Its three test-verdict semantics (30s markout scope; S5 max edge-candidate; mm_backtest diagnostic-only) are consistent with the prompt's §15/§21/§26.3. FA-1/F2c/F2d are declared supporting detail for prompt §25/P06/P13 (its §14 states this explicitly) |
| D2 | `docs/PLAN_RESEARCH_CYCLE_1.md` (S0–S6, 12 统计纪律, dim_segments spec, 附录A) | S2/S3/S5 + signing/RTT/EC2-rerun/reproducibility procedures preserved as ACTIVE SUPPORTING SPEC candidates; S1 = HISTORICAL EVIDENCE (dev-grade, done 2026-07-10, commit 6056c353 per memory/SESSION_LOG) | PLAN (operator-approved 2026-07-10). S1/S4 burstiness source correction **VERIFIED present in current docs**: S4 lines 122–124 & S1 line 288 (raw book messages only, three channels separate); PLAN_MM_TEST_PROGRAM B1 (lines 189–199) repeats it | S5's split design (val=middle, test=last-2-days, open once) predates the prompt §20 four-way split manifest — P02 must reconcile; registered in conflict register C-6 |
| D3 | `docs/PLAN_FULL_MARKET_RESEARCH.md` (U0–U6, waves 0–6) | All-18-category universe/atlas doctrine; discovery-lane definition | PLAN-ONLY, awaiting independent audit (its §0). Consistent with prompt §11.1 discovery universe | Explicitly warns build_segments/maker-edge are Tennis-oriented (§3) — matches prompt §26.3 known issue |
| D4 | `docs/PLAN_PRICING_MODEL.md` (Group M W-P1..P4 DONE+audited; Group C gated) | Pricing math reference implementation; Group C calibration gated (7 clean days, recv-only, OQ-1, W-FS1 hard prereq for W-C4) | Executed plan (M) + gated plan (C). Its pricing semantics = candidate input to prompt §25 policy features (OPERATOR_TBD/EXTEND per §26.3) | None with STP-P00 |
| D5 | `docs/PLAN_RISK_KILLSWITCH.md` (W-K1..K5 DONE+audited; W-K6 operator-gated) | Execution-safety components for future STP-P08/P10 | Executed except W-K6 (operator-scheduled). Money = **E6 micro-dollars** (deliberate, live-verified deviation from E4 — recorded in W-K1/K3 results) | Prompt §18 requires "audited fixed-point money type distinct from PriceE4" — E6 Micros satisfies this; no conflict |
| D6 | `docs/PLAN_LIVE_VALIDATION.md` (Phases 0–2 done-class components; Phase 3) | Read-only checks + mocks reusable; ANY real-order step subordinated to PLAN_MM_TEST_PROGRAM C5/I1–I3 (its 2026-07-10 safety header) | Partially superseded; its Phase-3 live steps must NEVER execute standalone | The subordination header resolves the old conflict; no open ambiguity |
| D7 | `docs/ARCHITECTURE.md` + `docs/ARCHITECTURE_REVIEW_2026-07-06.md` | Verified system inventory; two-worlds finding; gap register | Descriptive, evidence-grade (review = file-by-file audit). Basis for §26.3 verification below | Review predates W-K1..K5/W-P1..P4 (gaps #9/#10/#11 since built as dry-run components, not wired) — noted in REPOSITORY_AUDIT |
| D8 | `docs/warehouse_schema.md` | Data-layer contract (locked) | Binding schema doc for the warehouse; W-TL1 ladder + ts_utc look-ahead warning are load-bearing for all STP research | None |
| D9 | `docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_ARTIFACT.md` (+ MANIFEST.json, independent audit PASS) | §31.2 prerequisite; how W01/AUD01 must run tests | ESTABLISHED (R003, audit PASS ef58bb36…). W01 test runs use `tests/isolated_run.sh --new` exclusively | R2 (rtt sampler) and R4 (two unregistered test scripts) inherited as known limitations |

## 5. State/handoff layer

| # | Document | Scope | Authority |
|---|---|---|---|
| E1 | `docs/SESSION_LOG.md` | Cross-session memory, newest-first | Descriptive only (prompt §4.6). Relevant entries read: 2026-07-11 (×8: R004 receipt, ISO-AUD01, ISO-W01, R002 ×2, FA-1, F2c/F2d, account/D-1.1, R001), 2026-07-10 (D-1 adjudication era), 2026-07-09 (hypotheses H1–H13) |
| E2 | Memory files (`~/.claude/projects/...`) | Assistant memory | NOT authority; nothing relied on without repo verification |

## 6. Conflict-rule application summary

- Every conflict found is either (a) resolved by scope with the governing text
  quoted, or (b) registered as OPERATOR-TBD in
  `STP_P00_CONFLICT_RISK_REGISTER.md`. None was resolved by convenience.
- OPERATOR-TBD items: C-1 (evidence-path scope amendment ratification),
  C-2 (§43 referent), C-6 (S5 split vs §20 manifest — P02 design decision),
  plus the standing operator items OQ-1 (fee ratification) and W-K6
  scheduling, which are pre-existing and merely re-listed.
