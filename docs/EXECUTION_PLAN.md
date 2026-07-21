# docs/EXECUTION_PLAN.md — Kalshi System: Engineering Lifecycle Playbook
# Owner: R (Architect). Executor: Claude Code (syntax only).
# Version: 1.2 — v1.0 verbatim source recovered (docs/plan_audits/
# 2026-07-06_EXECUTION_PLAN_v1.0_original.md) merged with the approved audit
# (docs/plan_audits/2026-07-06_execution_playbook.md @ ac2d2ad): corrections
# C-A/C-B and amendments A1–A6 applied in-body. All v1.1 [GAP]s resolved.
# One WP per fresh session. Supersedes v1.1-reconstructed.

═══════════════════════════════════════════════════════════════════
OPERATING PROTOCOL — prepend this block to EVERY work-package session
═══════════════════════════════════════════════════════════════════
1. SCOPE: You are executing exactly ONE work package (WP), defined below.
   Anything outside it — including obvious nearby bugs — goes into
   docs/BACKLOG.md as a note, never into code. No "while I'm here" fixes.
   (Sole exception, inherited from GUARDRAILS D2/D4: a silent-data-loss
   bug of the shard-glob class may be fixed immediately WITH a regression
   test in the same change, and logged.)
2. TESTS FIRST (TDD): Step 1 of every WP is writing the specified test
   file(s). Run them and SHOW them failing (red) before writing any
   implementation. Then implement until green. Tests are the contract;
   implementation serves tests, never the reverse. Never weaken a test
   to make it pass — if a test seems wrong, STOP and report.
3. LOCKED CONSTRAINTS: Items marked LOCKED are non-negotiable. If live
   API behavior or existing data contradicts a LOCKED item, STOP,
   document the conflict with evidence, and end the session. Correct
   API usage is permanent priority #1 — verified reality beats spec.
4. CONTEXT HYGIENE: Read ONLY the files in this WP's Context Manifest
   plus docs/warehouse_schema.md. Do not load prior chat history,
   other WPs, or unrelated modules. DO read the plan-audit files a WP
   cites — pre-registered facts exist so sessions don't rediscover them.
5. DEMONSTRATE, DON'T REPORT: Acceptance = live command output pasted
   in full (test runs, real load() calls, real file listings). A prose
   summary of success is not acceptance.
6. GLOBAL INVARIANTS (all WPs, forever):
   - ZERO order transmission to production Kalshi. No exceptions.
   - Pipeline data paths are read-only unless the WP explicitly says
     otherwise.
   - No secrets in code, logs, tests, or commits.
   - Every GUARDRAILS.md MUST binds every WP (docs/GUARDRAILS.md).
7. EXIT: End the session by printing: files changed (git diff --stat),
   test suite result, and the WP's Definition of Done checklist with
   each item checked or explicitly failed. Then COMMIT and append the
   SESSION_LOG entry per CLAUDE.md's exit ritual — uncommitted work is
   not done work.

═══════════════════════════════════════════════════════════════════
INDEPENDENT AUDIT PROTOCOL — run in a SEPARATE fresh session after
every WP goes green, before the WP is marked DONE
═══════════════════════════════════════════════════════════════════
You are a read-only auditor. You did not write this code. Inputs: the
WP section, git diff of the WP branch, and the test files.
Verify, with evidence:
  a. Tests genuinely encode the WP contract (no tautologies, no tests
     that pass vacuously, no assertions weakened to fit output). This
     applies to pre-existing suites a WP adopts, not only new ones (A1).
  b. Run the full suite yourself; confirm green independently.
  c. Scope check: diff contains nothing outside the WP scope.
  d. Hunt the failure class that burned us before: silent data loss
     with green dashboards (unhandled file patterns, swallowed
     exceptions, off-by-one at day/hour boundaries, unit mixups
     cents-vs-E4).
  e. (A5) Run the GUARDRAILS §6 checklist; any MUST violation = FAIL.
  f. (PREVENT, 2026-07-06) An audit MUST cite the repo path + git hash
     of the document it audits; if the path does not exist in the
     repo, the audit is REJECTED before any checklist runs.
Output: PASS or FAIL + findings list. FAIL blocks the WP.

═══════════════════════════════════════════════════════════════════
WORK PACKAGE REGISTER
Dependency spine: WP-01 → WP-04 → {WP-06, WP-08, WP-09}; WP-02 closed,
WP-03 early & independent; WP-05 parallel anytime. Phase 5 WPs SEALED.
═══════════════════════════════════════════════════════════════════

── WP-00: Install this playbook ────────────────────────────────────
Scope: Create docs/EXECUTION_PLAN.md (this file), docs/BACKLOG.md
(empty), tests/ scaffolding with pytest configured, and a `make test`
target. No logic.
(A4) Also defines quality_log before any WP writes to it:
  work/quality_log.ndjson — append-only, one JSON object per entry:
  {ts, wp, window, finding, action, evidence}. Surfaced by WP-08.
(A1) `make test` must NOT fork the test infrastructure: pytest suites
  (pytest 8.4.2 is installed) each get a tools.json entry + pass token
  + inclusion in tests/run_pipeline.sh alongside the existing `make
  check` convention. One infrastructure, two entry points.
Definition of Done: `make test` runs (0 tests, exit 0); files committed.

── WP-01: E4 Integrity Verification (re-scoped per correction C-A) ──
v1.0 premise corrected: there is NO sub-penny corruption and NOTHING
to recover — E4 integers have been the storage format end-to-end since
the warehouse was built (60,743 sub-penny trades verified live in
staging, 2026-07-06). The ~567 frames lost in the 08:18–08:35 UTC
double-writer window are splices of two messages: unrecoverable by
definition. No retention deadline exists.
Context Manifest: ingester module, type schema, one raw log sample.
LOCKED: Prices stored as E4 integers (price × 10,000) end-to-end;
  conversion to display cents happens only at load()/presentation.
Tests (written under WP-04's suite, folded from here):
  - test_e4_roundtrip: raw msg with price 325 → stored 325 → load()
    presents 3.25¢. Also 5000 → 50.00¢, and integer-cent legacy input.
  - test_fractional_qty: fractional size survives ingest exactly.
Build: one quality_log entry — {window: 2026-07-06T08:18–08:35Z,
  finding: interleaved double-writer line corruption ~567 frames,
  action: discarded-unrecoverable (corrupt rows purged from staging,
  counts logged), evidence: docs/ARCHITECTURE_REVIEW_2026-07-06.md}.
Definition of Done: quality_log entry written; E4 tests green in
  WP-04's suite; live load() sample shows sub-penny prices; audit PASS.

── WP-02: Sports Class A Flip — CLOSED, DONE-PRIOR (correction C-B) ─
H-1 was decided and is live: config/market_classes.yaml lists Sports
(and Politics) in class_a_full_l1; 975k Sports L1 rows in staging;
classification reads from yaml, not hardcoded; unknown categories
already warn + default Class B.
Residue: (a) fold tests into WP-04 — test_policy_from_config (mutate a
temp config, assert behavior follows; never hardcoded),
test_unknown_category_warns; (b) the 48h staging-size watch remains:
schedule it, log the observation to quality_log.

── WP-03: Freshness Monitor ─────────────────────────────────────────
Objective: One number — seconds since newest staging row — surfaced
daily; alert when stale. (The 31-minute shard lesson, made permanent.)
Tests first (tests/test_freshness.py):
  - test_freshness_computation on fixture DB with known newest ts.
  - test_alert_threshold: stale fixture → alert fires; fresh → silent.
Definition of Done: metric visible via one command AND in daily check
  output (WP-08 will consume it); injected-staleness demo shown live.

── WP-04: Permanent Acceptance Suite (the TDD conversion) ───────────
Objective: Every past ad-hoc acceptance criterion becomes a permanent
suite run on every change. This is the rigid architectural contract
for the whole pipeline.
(A1) Substantially EXISTS and is green: tests/test_ingest.py and
tests/test_export_day.py already encode change-only, heartbeat,
kill/restart, and load-routing, wired into `make check` +
run_pipeline.sh. This WP migrates/extends — it does not rewrite. The
anti-tautology audit covers the adopted tests too.
Context Manifest: ingester, exporter, load(), dims module.
Tests (tests/test_pipeline_contract.py) — all on synthetic fixtures:
  - test_change_only: 100 identical ticks + 1 change → exactly 2 rows.
  - test_heartbeat: 3 simulated hours, no changes → 3 is_snapshot rows
    at top-of-hour.
  - test_kill_restart_determinism: ingest fixture log interrupted at a
    random offset, resume → table identical row-for-row to an
    uninterrupted run.
  - test_every_byte_accounted: checkpoint offsets cover 100% of
    fixture log bytes (no unprocessed gap).
  - test_league_parse: KXMLB→MLB, KXKBO→KBO, KXNCAABB→NCAA;
    unknown prefix → "_unknown" + logged warning, never a guess.
  - test_settlement_partitioned_by_settled_date.
  - test_load_routing: past date reads archive path, today reads
    staging (fixture-mocked roots).
  - test_denormalized_columns_present on every fact row.
  - folded from WP-01: test_e4_roundtrip, test_fractional_qty.
  - folded from WP-02: test_policy_from_config,
    test_unknown_category_warns.
Definition of Done: suite green in `make test` AND registered per A1
  (tools.json + run_pipeline.sh); audit PASS with explicit
  anti-tautology check on each test.

── WP-05: Discovery Mission  [read-only; parallel] ──────────────────
The already-written DISCOVERY prompt executes as its own session,
governed by its prior binding audit (docs/plan_audits/
2026-07-06_discovery_plan.md @ ac2d2ad — 7 amendments: phase naming,
demo-env reality [our engine rejects demo fail-closed by design;
private-WS subscribe semantics MAY be verified live with zero
transmit], measurement isolation, build on ARCHITECTURE_REVIEW rather
than re-derive, budget/stop-rule, deferred script deletion, day-count
expectations).
TDD does not apply (research, not code); its contract is the evidence
tags ([VERIFIED-LIVE] / [VERIFIED-CODE] / [VERIFIED-MEASURED] /
[DOCS-ONLY] / [ASSUMED]).
Addition: findings land as machine-readable constants in
config/kalshi_facts.yaml (fee formula + params, write-rate limits,
amend-endpoint existence, demo-env status), each entry carrying its
evidence tag. Downstream code imports this file — no formula code
ever hardcodes a fee or a rate limit. (A3) Seed with already-verified
facts: REST RTT 36.3ms p50 [VERIFIED-MEASURED 2026-07-06], tier
advanced read/write 300/s [VERIFIED-LIVE preflight], demo env
rejected-by-design [VERIFIED-CODE env.cpp].
Definition of Done: report + yaml delivered; OPEN QUESTIONS list
  returned to R for adjudication.

── WP-06: Research Notebook — K/z² + Intervals ─────────────────────
Objective: The maker-viability metrics, tests-first, on recorded data.
Context Manifest: load() only. Pipeline is read-only.
Tests first (tests/test_research_metrics.py), hand-computed fixtures:
  - test_wiggle: mids [10,12,10,12] → K=12, z=2, wiggle=(12−4)/2=4.
  - test_reversal_rate: same series → 1.0 (every move flips sign).
  - test_heartbeats_excluded from all metrics.
  - test_interval_distribution: fixture timestamps → known p50/p90.
  - test_fee_placeholder_guard: fee fn carries verified=False until
    kalshi_facts.yaml provides it; any "gate-mode" call with
    verified=False must raise.
(A2) All metrics are emitted in BOTH price space and log-odds space —
price-space-only screening overweights mid-range markets (vol ∝
p(1−p)); reuse mm_calibrate's logit outputs. Rankings shown in both.
Build: metrics per market-day; rollups by category/subcategory/league
  and hour-of-day; ONE page (ranking + drill-down + interval
  histograms). Rendering note: streamlit is NOT installed (no brew;
  pip install needs operator ok) — no-new-dependency fallbacks: static
  HTML artifact or the existing dashboard's Tools tab. No class
  frameworks, no simulation, no fill models.
Definition of Done: tests green; page renders live over all clean
  days; git diff = analytics/viz files only; audit PASS.

── WP-07: Fee Swap Re-run  [BLOCKED ON: WP-05] ─────────────────────
Replace placeholder fee with verified formula from kalshi_facts.yaml
(verified=True), re-run WP-06 outputs, diff the rankings, one-page
note on what changed. Tests: fee fn against hand-computed cases from
the verified formula.

── WP-08: Daily Quality Check Automation ───────────────────────────
Objective: The two-minute morning ritual as one command.
Tests first: fixture manifests → detects category row-count drop;
  consumes WP-03 freshness; detects sequence-gap entries; appends a
  structured line to quality_log.
Definition of Done: single command prints yesterday's health + writes
  the log line; demo on real data. (R still runs and reads it daily —
  the habit is human; the fetching is code.)

── WP-09: Gate Calculator  [BLOCKED ON: WP-04; fees need WP-05] ────
Objective: The three pre-committed gate numbers, computed by tested
code — not by last-minute scripts on gate day.
Tests first (tests/test_gate_metrics.py), synthetic bracket fixtures:
  - test_signal_detection: 3-market bracket, asks sum 96¢ → one BUY
    signal, gross edge 4¢; sum within band → no signal.
  - test_lifespan: fixture timeline → hand-computed persistence secs.
  - test_net_profit: gross − verified fee fn = expected net; and
    gate-mode REFUSES to run with verified=False fees (mechanical
    enforcement: the gate cannot be computed on assumptions).
Build: scan over N clean days → signal count, lifespan distribution,
  theoretical net profit table + the WP-06 wiggle table as context.
  Bracket detection can lean on dims: markets.csv carries derived
  event_structure and bracket_rank from tools/dim_snapshot.py.
Definition of Done: tests green; dry run over available days executes
  live; audit PASS. Output format = the gate meeting's input document.

═══════════════════════════════════════════════════════════════════
HUMAN STEPS (not WPs — Claude Code never executes these)
═══════════════════════════════════════════════════════════════════
H-1  Sports Class A decision — DECIDED AND LIVE (see WP-02). Owner: R.
H-2  Daily: run WP-08 command, read it, act on anomalies same-day.
H-3  Clean-day clock: 7 consecutive clean days = gate admission.
     Day 1 completes 2026-07-06 UTC midnight; earliest admission
     2026-07-13 if all days are clean.
H-4  GATE MEETING: pre-committed thresholds vs WP-09 output. Verdict
     A / B / C. Numbers are compared, not renegotiated. (A6) Whichever
     branch wins, docs/MM_ROADMAP.md is updated in the SAME change as
     the verdict so exactly one current strategy document exists.

═══════════════════════════════════════════════════════════════════
SEALED — Phase 5 work packages (DO NOT BUILD; unlock = gate verdict)
═══════════════════════════════════════════════════════════════════
S-A  Branch A (arb GO): scanner + paper-trading ledger on load().
S-B  Branch B (maker pivot): World A/B merge per gap register →
     fill-simulator CALIBRATION first → manual formulas (tanh
     inventory, log-odds skew, K−z² attribution dashboard, the two
     curve checks). Every S-B item inherits this same WP+TDD format.
S-C  Branch C (no-go): pipeline to low-cost mode; time reallocates to
     recruiting + studio. Also pre-committed.
Creating any S-* code before the gate verdict is a protocol violation.
═══════════════════════════════════════════════════════════════════
