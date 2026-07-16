# SPORTS-AUTORESEARCH-02 — Authorization & Execution Plan (FINAL, English)

**Status: operator-approved, ready to dispatch.** Paste this entire document
to the executing agent verbatim.
**Basis:** successor draft by the deep01 execution session, amended and merged
by the operator's companion session (2026-07-15). Sources:
`docs/SESSION_LOG.md` (2026-07-15 21:53 entry),
`docs/PROPOSAL_AUTORESEARCH_CONVERGENCE_CLAUSES_2026-07-15.md`,
`docs/plan_audits/SPORTS_AUTORESEARCH_01_CLAIMS_VERIFICATION_2026-07-15.md`.
Chinese master copy: `docs/SPORTS_AUTORESEARCH_02_AUTHORIZATION_2026-07-15.md`.

---

## 0. Convergence Clauses (the Five Brakes — highest priority; violating any one = departure from authorization)

1. **Time box.** Any single continuous stint of data-repair or tool-hardening
   work ≤ 45 minutes. On timeout the ONLY legal action is to stop and report
   to the operator. Silently continuing is a violation.
2. **Recursion cap.** Code defects in registrars / finalizers / receipt
   tooling are engineering debt, NOT "data repairs"; they never trigger a new
   repair registration. Data-repair registrations for this run are capped at
   3. If the cap is reached and another repair is needed: abort the run and
   hand it to the operator. Never self-start a repair-04.
3. **Definition of done.** Every gate's pass line is frozen BEFORE work
   starts (default: dedicated tests all green + cross-audit 0 Critical /
   0 High). Findings below that line are logged as engineering debt and do
   NOT block. An audit's job is to grade, not to reach zero; "could be
   stricter" is always true and is never a blocking reason.
4. **Heartbeat.** More than 20 minutes without a new on-disk artifact
   (file / commit / state update) requires a proactive status report.
   Sub-agent idle-waiting counts toward the 20 minutes.
5. **Parallelism grant.** Data scanning ∥ tooling wrap-up MAY run in
   parallel; the finalizer only needs to be complete before results are
   opened and never blocks scan ignition. Anything not listed defaults to
   serial; what is listed must not be silently serialized.

**Brake-trip script (mandatory template — no silent continuation):** whenever
any clause in §0 trips, immediately output:
`⚠️ BRAKE TRIPPED: [clause #] | Done so far: [one line] | Stuck on: [one line]
| Request: [continue / change course / abort] | Awaiting operator instruction`
— then stop all work in that direction.

## 1. Identity & Permission Preflight

- Confirm the connected host is the dedicated research instance W09:
  `i-0e53d134dceffe166` (us-east-2), NOT the production EC2.
- Heavy recomputation runs only on W09; the Mac only orchestrates, tails
  logs, and syncs reports.
- Verify W09 instance role, AWS identity, and S3 read-only access
  (`research/*`). Never print or copy credentials.
- Verify instance identity, boot ID, and current IP (the instance has been
  power-cycled; see Appendix A7).
- Head/list-verify sealed release manifests; do not scan large objects yet.
- Confirm no residual processes from the old run; do not modify its files.

## 2. Freeze the Old Run

Old run: `20260715T112538Z__c21a79a8cff__deep01`

- Remains DATA_INTEGRITY_BLOCKED. Do not execute repair-08.
- Never overwrite the old hypothesis ledger, scratch, reports, or
  repair-01..07 receipts. The entire old run directory is read-only.
- The old data (07-12 / 07-13 as used by deep01) is prior-exposed
  development data; it must never be passed off as unseen evaluation.

## 3. Create the New Run

- Brand-new RUN_ID, scratch, logs, resource receipts, and output directory.
- Record the mission-text SHA:
  `9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c`
- Record the inherited research-code boundary commit:
  `6bcb5cb972865abc9c133bd9635c05540b9ced86`
- **Framework reuse (hard rule):** registration / receipt / finalizer
  framework MUST be the deep01-tested version at commit 6bcb5cb. Rewriting or
  refactoring it is forbidden; defects found in it are engineering debt per
  §0.2.
- Register the seven existing non-RFQ hypotheses as successor trials without
  modifying their old statuses. Any new hypothesis (§5) must be preregistered
  with frozen thresholds before any scan.

## 4. Data-Volume Gate (two-tier, operator ruling)

Before any heavy scan, inventory ALL sealed releases in the research bucket
(ground truth = the live S3 manifest, never an assumed count):

- **Date-inventory discipline (correction from the deep01 handoff):**
  "6 releases / 80 GB" is a *publication inventory*, not 6 independent dates.
  07-11 is QUARANTINED_LEGACY (unusable). 07-12 and 07-13 each have multiple
  publication variants; **select exactly one current canonical,
  version-bound release per date** — merging variants double-counts the same
  day. As of 2026-07-15 the qualified independent dates = 2. Use every
  qualified date that exists — no more, no less, no guessing.
- **Intake structural audit (hard prerequisite):** every release admitted to
  this run must first pass the full line-by-line structural audit (reuse the
  deep01 audit tool, ~5 min per 59 GB). Non-conforming objects are handled
  by the established whole-object quarantine rule with receipts, then the
  release may be admitted.
- **Verdict tier (frozen thresholds, unchanged):** PROMOTION_READY requires
  ≥ 20 independent dates, ≥ 200 relevant root events per core hypothesis,
  and a passed chronological unseen test. With less data, **this run
  produces zero promotions.**
- **Exploratory tier (operator ruling — this run executes it):** with fewer
  than 20 dates, do NOT shut down. Since the qualified dates are the same
  two days deep01 already analyzed (its three mainline modules are complete
  and immutable), **do not re-run the §5 A/B/C hypothesis tests on the same
  data** (re-running identical data = zero new information). Execute only:
  (a) the three descriptive scans in §5.D (never done before — new
      knowledge);
  (b) if the preflight finds newly sealed qualified dates after 07-13,
      report to the operator and await a ruling on their use before touching
      them.
  All outputs carry mandatory `EXPLORATORY + PRIOR_EXPOSED` banners.
- L2 dates must pass sequence-gap, missed-frame, and integrity checks.

## 5. Research Scope

**A. L1 / trades** — C1-SPREAD-CAPTURE-01, C1-LARGE-FLOW-CONTINUATION-01,
C1-PREMATCH-TTS-01. Cross-date stability, post-fee economics, pessimistic
fills, latency, capacity. *(Not re-run this cycle unless new dates are
admitted per §4.)*

**B. L2 / order book** — C1-HFOLLOW-RETREAT-01, C1-DEPLETION-REFILL-01.
Complete L2 dates only; queue position and microprice remain diagnostics
until independently calibrated. *(Same re-run restriction.)*

**C. Market Atlas / cross-market** — C1-THREEWAY-OVERROUND-01,
C1-SOCCER-POISSON-RV-01. Mutual exclusivity and payout exhaustiveness must
be proven before any relative-value claim. *(Same re-run restriction.)*

**D. Descriptive scans (EXPLORATORY — the actual new compute of this run):**
1. **Tennis side-conditioned markout:** distribution of 1-min / 5-min
   markouts for taker buys of the underdog side after large swings —
   quantifies the retail favorite-longshot bias (existence + thickness),
   evidence base for the maker-side strategy.
2. **Cross-market consistency scan:** frequency, thickness, and lifetime of
   windows where mathematically linked markets of the same event diverge
   beyond the fee line.
3. **One-sided book opportunity scan:** per-sport two-sided quote share and
   the duration distribution of one-sided windows (deep01 atlas showed
   Baseball ≈ 12.5% one-sided).

**§5.D-bis Open exploration mandate (operator's statement of purpose,
2026-07-15):** the purpose of this run is **not strategy proof** but mining
basic regularities and finding directions in the data we already have. The
three scans above are therefore a **minimum set, not a ceiling**: the agent
may autonomously add descriptive analyses on admitted data (e.g. intraday
liquidity rhythms, spread dynamics around game events, order-flow imbalance
signatures, retail behavior patterns, per-sport market-efficiency profiles),
each opened with one line in the scan ledger + the three receipts — no
per-item operator approval needed. Only four constraints: ① read-only,
admitted data only; ② every output carries EXPLORATORY + PRIOR_EXPOSED
banners and makes no promotion/tradability claim; ③ do not re-run the
existing preregistered hypothesis verdicts (that belongs to the verdict
tier); ④ the §7 cost/time caps stand — rank exploration items by expected
information per cost, and put the pen down when the cap trips. All findings
flow into the "signal map" chapter of the §7-bis report.

**§5.D light-process exemption (anti-overgovernance, operator ruling):**
descriptive scans are read-only exploratory compute and are **exempt** from
the repair-registrar / finalizer machinery. Full paperwork per scan = three
receipts: input-data fingerprint, script SHA-256, output-file fingerprint,
plus the EXPLORATORY banner. Building ANY new registrar, transaction layer,
or cross-audit tier for a descriptive scan = violation of this
authorization. Scan-script bugs: fix directly and re-run (inputs are
read-only; re-runs have no side effects); note the change in SESSION_LOG.

**RFQ is explicitly excluded.** Reference only the old run's blocked
disposition and receipts. Do not read full RFQ data, do not repair replay,
do not run repair-08. Only a new explicit operator authorization can reopen
`W-SAR-RFQ-REPLAY-01`.

## 6. Candidate-Strategy Discipline

Priority order: 1. Spread capture; 2. H-FOLLOW; 3. Three-way overround;
4. Pre-match dynamics; 5. Descriptive research.

Every (future) promoted candidate requires: explicit
entry/quote/cancel/exit rules; fee and pessimistic-fill assumptions;
latency, capacity, and kill conditions; chronological unseen-test
performance; data-quality and concentration checks; a PROMOTION_READY
dossier. **This run is EXPLORATORY: it promotes nothing. Its product is a
signal map plus a target list for deep03.**

## 7. Resource & Shutdown Control

- Releases that fail the intake audit never enter a scan.
- Materialize incrementally by date with checkpoints; never reprocess old
  data wholesale.
- Heavy-memory operations (global dedup etc.) MUST be bucketed by
  date/partition. Single global window materialization is FORBIDDEN
  (deep01 OOM at a measured 42.8 GiB ceiling).
- Hard caps: total compute wall time ≤ 6 hours OR estimated cost ≤ $5 —
  whichever trips first ends the run with a (possibly partial) report. No
  exemption requests.
- Log wall time, CPU, peak memory, and estimated cost per stage.
- On success, failure, or insufficient data alike: commit, write
  SESSION_LOG, sync the docs mirror, and stop W09.

## 7-bis. Deliverable: the First Illustrated Research Report (REPORT_VISUAL_01)

The final deliverable is not bare tables but a report the operator can read
and analyze directly (Word/PDF; every figure gets a plain-language caption).
Gap list and sources:

1. **repair-04 archive retrieval (F-1 — first action after boot):**
   read-only sync of the repair-04 directory from W09 back to the local run
   directory, fingerprints verified (see
   `docs/plan_audits/SPORTS_AUTORESEARCH_01_CLAIMS_VERIFICATION_2026-07-15.md`).
2. **The three §5.D descriptive-scan results (new compute):** each outputs
   CSV + at least one figure.
3. **Visualization of frozen deep01 products (zero compute, read-only
   plotting):** coverage atlas (sport × market-days × trades heatmap),
   spread-width comparison, hypothesis status board, data-quality QC
   summary, repair-01..07 timeline.
4. **Assembly:** the above + FULL_REPORT prose conclusions →
   `docs/research_reports/SPORTS_AUTORESEARCH_VISUAL_REPORT_01.docx`
   (or PDF), `EXPLORATORY + PRIOR_EXPOSED` banner on the cover, and an
   explicit statement: *this report is a signal map and contains no
   tradable conclusion; the verdict edition (20-day sample + unseen test)
   will come from deep03.*

## 8. Completion Criteria

This run may close only as one of:
- EXPLORATORY signal map complete (all §5.D scans + §7-bis report on disk);
- all directions COLLECT_MORE / DATA_STARVED;
- a new core data-integrity problem (handled within the §0.2 cap);
- cost/time cap reached.

**No live trading, order placement, or micro-live is authorized.**

---

## Appendix A: Ledger of Overturned Assumptions (deep01 audit findings — successors must not re-adopt these)

The following beliefs were overturned by evidence during deep01. If you find
yourself acting on any of them, stop and report via the brake-trip script.

| # | Overturned assumption | Overturning evidence | Plan clause |
|---|---|---|---|
| A1 | "6 releases ≈ 6 independent dates" | Inventory: 07-11 quarantined; 07-12/13 one canonical each; qualified dates = 2 | §4 date discipline |
| A2 | "Re-publishing fixes corrupt lines" | Bad-line SHA matches the sealed manifest ⇒ written at capture time | §4 intake audit; capture debt filed separately |
| A3 | "Unlimited hardening is harmless" | repair-04/05/06 governance recursion; the day's main delay | §0 brakes; §5.D exemption |
| A4 | "Global-window dedup fits a 61 GB box" | repair-06 measured OOM at 42.8 GiB | §7 mandatory bucketing |
| A5 | "RFQ is quickly repairable" | two OOMs + replay entanglement; marginal value zero (data insufficient anyway) | §5 RFQ exclusion |
| A6 | "Repair archives exist on both machines" | repair-04 exists only on W09 | §7-bis F-1 |
| A7 | "W09 control-plane state was confirmed" | execution session admitted it never read EC2 State.Name directly | §1 identity/boot preflight |

---

## Authorization text (operator)

> SPORTS-AUTORESEARCH-02 is approved. This document, in full, is the
> authorization; §0 (Five Brakes) and Appendix A bind everything else.
> First action: commit this document and pin its SHA. Second action: restate
> §0, §5.D exemption, and Appendix A clause by clause; execution begins only
> after the restatement.
> Build a brand-new RUN_ID on W09. Never append to or overwrite old run
> 20260715T112538Z__c21a79a8cff__deep01. Preflight identity, permissions,
> and the release inventory first; every admitted release passes the
> line-by-line structural audit; exactly one canonical release per date.
> This run is EXPLORATORY: if qualified dates are still only 07-12/13, do
> not re-run the existing mainline hypothesis tests — execute the three
> mandatory descriptive scans (tennis side-conditioned markout, cross-market
> consistency, one-sided book windows), plus any additional exploratory
> analyses under the §5.D-bis open mandate, and the REPORT_VISUAL_01
> deliverable;
> all outputs carry EXPLORATORY + PRIOR_EXPOSED banners; nothing is
> promoted. If newly sealed dates after 07-13 exist, report and await an
> operator ruling before touching them. RFQ stays DATA_INTEGRITY_BLOCKED;
> repair-08 and W-SAR-RFQ-REPLAY-01 remain forbidden. Hard caps: $5 or
> 6 compute-hours. No live trading of any kind. On completion: commit code
> and report, update SESSION_LOG, sync the docs mirror, and stop W09.
