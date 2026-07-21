# Plan Audit: EXECUTION_PLAN.md v1.0 (Engineering Lifecycle Playbook)

Verdict: **APPROVED WITH 2 FACTUAL CORRECTIONS + 6 AMENDMENTS.**
GUARDRAILS §6: 10/10 pass. The playbook's operating protocol (TDD-red-first,
anti-tautology independent audit, demonstrate-don't-report, sealed Phase-5,
mechanical fee-verification gating) is the strongest process document this
project has; it operationalizes E1/D2/P2/P3 better than the constitution
itself. The corrections below are facts the playbook's author could not see
(context hygiene cuts both ways); per its own Rule 3, they are documented
here pre-execution instead of burning a session to discover them.

## FACTUAL CORRECTIONS (playbook premises vs verified reality)

**C-A — WP-01's premise is false: there is no sub-penny corruption and
nothing to recover.** E4 integers (price × 10,000) have been the storage
format end-to-end since the warehouse was built; verified live right now:
60,743 sub-penny trades in staging (`yes_price_e4 % 100 != 0`). There is no
"OLD path" that truncates. The real incidents were different: (i) interleaved
double-writer line corruption 08:18–08:35 UTC 2026-07-06 — ~567 frames are
splices of two messages and are UNRECOVERABLE by definition (no duplicate
copy exists); valid frames from that window were ingested normally; corrupt
rows were purged from staging with counts logged. (ii) rotation-shard glob
miss (31-min staleness) — fixed same day with a regression path.
→ Re-scope WP-01 to pure verification: keep the E4 round-trip/fractional-qty
tests (they are good permanent contracts; fold into WP-04), write the
quality_log entry recording the ~567-frame permanent loss (window, cause,
action: discarded-unrecoverable), and drop the "recover from raw before
retention" deadline — there is nothing to race.

**C-B — WP-02 is already implemented.** `config/market_classes.yaml` has had
Sports (and Politics) in `class_a_full_l1` since before this playbook
("per operator priority" comment in the file); staging holds 975k Sports L1
rows right now; classification is read from the yaml by
tools/build_classification.py (not hardcoded), and unknown categories already
warn + default to Class B. H-1 is therefore already decided and live.
→ Close WP-02 as done-prior; keep its two tests (policy-from-config,
unknown-category-warns) by folding them into WP-04. The 48h staging-size
watch remains valid — schedule it.

## AMENDMENTS (binding)

**A1 — One test infrastructure, not two (E3).** The repo already has a wired
convention: self-contained test runners with pass tokens, registered in
tools.json, run by `make check` and tests/run_pipeline.sh. WP-04's suite
substantially EXISTS there (tests/test_ingest.py, tests/test_export_day.py
already encode change-only, heartbeat, kill/restart, load-routing — green in
CI today). pytest 8.4.2 is available and fine to adopt, but `make test` must
not fork the infrastructure: new pytest suites get tools.json entries + a
pass token + inclusion in run_pipeline.sh, and WP-04 becomes "migrate/extend
the existing suites, add the genuinely-new contracts" —
test_every_byte_accounted, test_league_parse, test_settlement_partitioning,
test_denormalized_columns, plus the folded-in tests from C-A/C-B. The
anti-tautology audit applies to the existing tests too.

**A2 — WP-06 metrics: compute in log-odds alongside cents (Q1).** Wiggle/
reversal/K−z² on raw cent mids systematically overweights mid-range markets
(price vol ∝ p(1−p)). Screening in price space is permitted, but the notebook
must emit both parameterizations so the gate isn't decided on a price-space
artifact. mm_calibrate already emits logit-space vol/toxicity — reuse.
Environment note: streamlit is NOT installed (no brew on this machine; pip
install needs operator ok). Fallback that needs no new dependency: static
HTML artifact or the existing dashboard's Tools tab.

**A3 — WP-05 carries its prior audit.** The discovery mission was already
audited (docs/plan_audits/2026-07-06_discovery_plan.md, 7 amendments —
demo-env reality, measurement isolation, build-on-ARCHITECTURE_REVIEW). Those
amendments bind. kalshi_facts.yaml is approved enthusiastically — it makes Q3
mechanical. Seed it with already-verified facts: RTT 36.3ms p50
[VERIFIED-MEASURED 2026-07-06], advanced tier read/write 300/s
[VERIFIED-LIVE preflight], demo-env rejected-by-design [VERIFIED-CODE
env.cpp].

**A4 — quality_log needs a definition before WPs write to it (WP-00 scope).**
Proposed: work/quality_log.ndjson, append-only, one JSON object per entry
{ts, wp, window, finding, action, evidence}; surfaced by WP-08. Without this,
three WPs write to an undefined artifact.

**A5 — Independent Audit Protocol addition: run GUARDRAILS §6 too.** The
per-WP auditor checks scope/tests/failure-classes; add one line: "verify no
GUARDRAILS MUST is violated (docs/GUARDRAILS.md §6 checklist)."

**A6 — Reconcile strategy docs at gate time (E5).** The playbook's Branch A
(bracket-sum arb first) vs MM_ROADMAP's maker-first is a strategic fork, not
a violation — the gate verdict decides. Whichever branch wins, MM_ROADMAP.md
gets updated in the same change as the verdict so there is exactly one
current strategy document.

## Notes for the executor
- WP-04's "3 simulated hours → 3 heartbeat rows" and kill/restart tests
  exist and are green — extend, don't duplicate.
- WP-09's bracket detection can lean on dims: markets.csv carries derived
  event_structure (bracket/binary/…) and bracket_rank from dim_snapshot.
- Clean-day clock (H-3): day 1 completes tonight UTC midnight; earliest gate
  admission is 2026-07-13 if all days are clean.
