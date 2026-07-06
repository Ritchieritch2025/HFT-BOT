# docs/EXECUTION_PLAN.md — Kalshi System: Engineering Lifecycle Playbook
# Owner: R (Architect). Executor: Claude Code (syntax only).
# Version: 1.1-RECONSTRUCTED (2026-07-06)
#
# PROVENANCE: v1.0 was approved via audit but never written to disk (E2
# violation). This file is reconstructed SOLELY from the surviving record —
# docs/plan_audits/2026-07-06_execution_playbook.md — with that audit's 2
# factual corrections and 6 amendments merged into the body. Sections the
# audit does not reveal are marked [GAP — operator must confirm scope] and
# were NOT invented. The operator must fill every [GAP] before the affected
# WP is executable.

═══════════════════════════════════════════════════════════════════
OPERATING PROTOCOL — prepend to EVERY work-package session
(reconstructed from audit; confirm against author's intent)
═══════════════════════════════════════════════════════════════════
1. SCOPE: one WP per fresh session; nothing outside the WP.
   [GAP — exact original wording of scope/backlog rule unconfirmed]
2. TESTS FIRST (TDD): write the WP's tests, SHOW them failing (red),
   then implement to green. Never weaken a test to pass it.
3. LOCKED CONSTRAINTS / STOP RULE ("Rule 3"): if verified reality
   (live API, existing data) contradicts a LOCKED item, STOP and
   document the conflict with evidence. Correct API usage is permanent
   priority #1 — verified reality beats spec.
4. CONTEXT HYGIENE: read only the WP's context manifest; do not load
   prior chat history. (Note: hygiene cuts both ways — pre-registered
   facts in plan audits exist precisely so sessions don't rediscover
   them. Read the WP's cited audit files.)
5. DEMONSTRATE, DON'T REPORT: acceptance = pasted live command output,
   not prose.
6. GLOBAL INVARIANTS: zero order transmission to production Kalshi,
   ever. Pipeline data paths read-only unless the WP says otherwise.
   No secrets anywhere. GUARDRAILS.md MUSTs bind every WP.
7. EXIT: print git diff --stat, test suite result, Definition-of-Done
   checklist, then COMMIT. **Every session ends with a commit —
   uncommitted work is not done work.**

═══════════════════════════════════════════════════════════════════
INDEPENDENT AUDIT PROTOCOL — separate fresh session after each WP,
before the WP is marked DONE
═══════════════════════════════════════════════════════════════════
Read-only auditor; inputs: WP section, branch diff, test files.
  a. Tests genuinely encode the WP contract (no tautologies, no
     vacuous passes, no weakened assertions). Applies to pre-existing
     suites a WP adopts, not just new ones (Amendment A1).
  b. Re-run the full suite independently; confirm green.
  c. Scope check: diff contains nothing outside the WP.
  d. Hunt the house failure class: silent data loss under green
     status — unhandled file patterns, swallowed exceptions,
     day/hour boundary off-by-ones, cents-vs-E4 unit mixups.
  e. (A5) Run the GUARDRAILS §6 checklist; any MUST violation = FAIL.
  f. (PREVENT rule, added 2026-07-06) An audit MUST cite the repo
     path + git hash of the document it audits; if the path does not
     exist in the repo, the audit is REJECTED before any checklist
     runs.
Output: PASS or FAIL + findings. FAIL blocks the WP.

═══════════════════════════════════════════════════════════════════
WORK PACKAGE REGISTER (reconstructed)
Dependency facts known from audit: WP-04 consumes tests folded from
WP-01/WP-02; WP-05 feeds fee facts consumed by gate-mode code; WP-09
depends on dims + verified fees. Full dependency spine: [GAP —
operator must confirm original ordering]
═══════════════════════════════════════════════════════════════════

── WP-00: Install this playbook ────────────────────────────────────
Known scope (audit A4): define quality_log BEFORE any WP writes to it.
  quality_log = work/quality_log.ndjson, append-only, one JSON object
  per entry: {ts, wp, window, finding, action, evidence}.
Remaining original scope: [GAP — operator must confirm scope]

── WP-01: E4 Integrity Verification (RE-SCOPED per correction C-A) ──
The v1.0 premise (sub-penny corruption + recover-from-raw deadline)
was factually wrong: E4 integers are the storage format end-to-end
(60,743 sub-penny trades verified live in staging 2026-07-06); the
~567 frames lost in the 08:18–08:35 UTC double-writer window are
splices of two messages and are UNRECOVERABLE by definition — there
is nothing to race and no deadline.
Scope now: (a) E4 round-trip + fractional-quantity contract tests —
written under WP-04's suite, not here; (b) one quality_log entry:
{window: 2026-07-06T08:18–08:35Z, finding: interleaved double-writer
line corruption ~567 frames, action: discarded-unrecoverable (corrupt
rows purged from staging with counts logged), evidence: session
record + docs/ARCHITECTURE_REVIEW_2026-07-06.md}.
Definition of Done: quality_log entry written; E4 tests exist and are
green in WP-04's suite; audit PASS.

── WP-02: Sports Class A Flip — CLOSED, DONE-PRIOR (correction C-B) ─
config/market_classes.yaml already lists Sports (and Politics) in
class_a_full_l1; 975k Sports L1 rows in staging; classification reads
from yaml (not hardcoded); unknown categories already warn + default
to Class B. H-1 is decided and live.
Residue: (a) its two tests — policy-from-config, unknown-category-
warns — fold into WP-04; (b) the 48h staging-size watch remains valid:
schedule it and log the observation in quality_log.

── WP-03: [GAP — operator must confirm scope] ───────────────────────
(Existence implied by WP numbering only; the audit does not reveal
its content. Do not execute until the operator restores its scope.)

── WP-04: Permanent Acceptance Suite ────────────────────────────────
Substantially EXISTS and is green today: tests/test_ingest.py and
tests/test_export_day.py already encode change-only (100 identical +
1 change → 2 rows), heartbeat scheduling, kill/restart determinism,
and load() archive/staging routing; they run in `make check` and
tests/run_pipeline.sh.
Scope: migrate/extend, don't rewrite. Add the genuinely-new contracts:
  - test_every_byte_accounted (checkpoint offsets cover 100% of
    fixture log bytes)
  - test_league_parse (known league prefixes; unknown → "_unknown" +
    warning, never a guess)
  - test_settlement_partitioning (by settled date)
  - test_denormalized_columns (present on every fact row)
  - folded from WP-01: E4 round-trip, fractional-quantity survival
  - folded from WP-02: policy-from-config, unknown-category-warns
AMENDMENT A1 (binding): ONE test infrastructure. pytest 8.4.2 is
available and may be adopted, but every new suite gets a tools.json
entry + pass token + inclusion in tests/run_pipeline.sh; `make test`
must not fork the existing `make check` convention. The anti-tautology
audit applies to the pre-existing tests as well.
Definition of Done: suite green in both entry points; audit PASS with
explicit anti-tautology notes per test.

── WP-05: Discovery Mission [read-only; parallel] ───────────────────
Executes the discovery prompt as its own session, governed by BOTH
its prior binding audit (docs/plan_audits/2026-07-06_discovery_plan.md
— 7 amendments: phase naming, demo-env reality [our engine rejects
demo fail-closed by design], measurement isolation, build on
ARCHITECTURE_REVIEW rather than re-derive, budget/stop-rule, deferred
script deletion, day-count expectation-setting) and this playbook's
protocol (TDD waived for research; the evidence-tag system
[VERIFIED-LIVE]/[VERIFIED-CODE]/[VERIFIED-MEASURED]/[DOCS-ONLY]/
[ASSUMED] is its contract).
Output: report + config/kalshi_facts.yaml — machine-readable constants
(fee formula/params, rate limits, endpoint existence, demo status),
each entry carrying its evidence tag; downstream code imports this
file and never hardcodes a fee or limit. Seed with already-verified
facts: REST RTT 36.3ms p50 [VERIFIED-MEASURED 2026-07-06], account
tier advanced read/write 300/s [VERIFIED-LIVE preflight], demo env
rejected-by-design [VERIFIED-CODE env.cpp].
Definition of Done: report + yaml delivered; OPEN QUESTIONS returned
to R. [GAP — any further original DoD items unconfirmed]

── WP-06: Research Notebook — maker-viability metrics ───────────────
Known scope: wiggle / reversal-rate / K−z² style viability metrics
computed over recorded mids, tests-first on hand-computed fixtures;
pipeline read-only (consumes load() only).
AMENDMENT A2 (binding): metrics MUST be emitted in BOTH price space
and log-odds space (price-space-only screening overweights mid-range
markets; vol ∝ p(1−p)); reuse mm_calibrate's logit outputs.
Rendering: streamlit is NOT installed on this machine (no brew; pip
install requires operator ok). No-new-dependency fallbacks: static
HTML artifact, or the existing dashboard's Tools tab.
[GAP — original fixture values, exact metric definitions, and full
DoD must be confirmed by the operator; do not guess formulas]

── WP-07: [GAP — operator must confirm scope] ───────────────────────
(Existence implied by WP numbering only; content not revealed by the
audit. Likely relates to consuming WP-05's verified fee facts, given
the audited "mechanical fee-verification gating" — but that is
inference, not record. Do not execute until restored.)

── WP-08: Daily Quality Surfacing ────────────────────────────────────
Known scope: surfaces quality_log daily (audit A4: "surfaced by
WP-08"). [GAP — full scope, tests, and DoD must be confirmed]

── WP-09: Gate Calculator ───────────────────────────────────────────
Known scope: computes the pre-committed gate numbers from recorded
data for the gate meeting; includes bracket-structure signal detection
— lean on dims: markets.csv carries derived event_structure and
bracket_rank from tools/dim_snapshot.py. Gate-mode computation MUST
mechanically refuse to run on unverified fee assumptions (fees come
from kalshi_facts.yaml with verified=True; Q3).
[GAP — the pre-committed threshold values, exact tests, and output
format must be confirmed by the operator]

═══════════════════════════════════════════════════════════════════
HUMAN STEPS (never executed by Claude Code)
═══════════════════════════════════════════════════════════════════
H-1  Sports Class A decision — ALREADY DECIDED AND LIVE (see WP-02).
H-3  Clean-day clock: 7 consecutive clean days = gate admission.
     Day 1 completes 2026-07-06 UTC midnight; earliest admission
     2026-07-13 if all days are clean.
H-4  GATE MEETING: pre-committed thresholds vs WP-09 output; numbers
     are compared, not renegotiated. Whichever branch wins, update
     docs/MM_ROADMAP.md in the SAME change as the verdict so exactly
     one current strategy document exists (Amendment A6).
[GAP — other human steps (e.g. an H-2 daily ritual) implied but not
recorded; operator must restore]

═══════════════════════════════════════════════════════════════════
SEALED — Phase 5 (DO NOT BUILD; unlock = gate verdict only)
═══════════════════════════════════════════════════════════════════
S-A  Branch A: bracket-sum arbitrage first (audit A6 names this fork).
S-B  Branch B: maker-first per MM_ROADMAP (World A/B merge opens it).
S-C  [GAP — third branch implied by A/B/C verdict structure; scope
     unrecorded]
Creating any S-* code before the gate verdict is a protocol violation.
═══════════════════════════════════════════════════════════════════
