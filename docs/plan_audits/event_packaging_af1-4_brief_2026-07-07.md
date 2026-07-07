# AUDIT BRIEF — W-E0 money-integrity: AF-1..AF-4 (operator-authored, 2026-07-07)

Preserved verbatim (full-text preservation rule). Dispositions + remediation
recorded in SESSION_LOG and inline below each item.

---

AUDIT BRIEF — independent verification + red-first remediation of four
money-integrity findings on the W-E0 event-packaging work. Save verbatim to
docs/plan_audits/ (full-text preservation rule), run GUARDRAILS §6 self-audit
on your remediation plan, then execute.

POSTURE (read first): this brief is a LEAD, not ground truth. For every item,
re-derive the claim from the named file:line yourself. If a finding is wrong or
already handled, REFUTE it in writing with evidence and skip its fix — a false
finding "fixed" is worse than an open one. Report CONFIRMED vs REFUTED per item.
Anti-fake-green (D2): every fix ships ≥1 seeded-defect fixture proven RED before
the fix and GREEN after; a check never seen red is unproven.

WHY THIS SESSION: W-E0 (tools/event_measure_split.py + docs/PLAN_EVENT_PACKAGING
.md) is read-only and off the hot path — it cannot place orders or corrupt raw.
But the number it reports and the completeness verdict it will stamp on event
packs feed every future per-event backtest and the Q2 pessimistic-fill bound
(the go/no-go metric). Wrong here ⇒ wrong PnL. Do NOT build W-E1/E3 or widen
scope (P3); verify + remediate the four items, then stop.

GLOBAL CONSTRAINTS: forbidden writes — capture/ingest/export code, C++ engine,
config/market_classes.yaml, anything live_order (S5 stays). warehouse.py is the
shared read entry point: editable (AF-3) but every existing load() test stays
green and the change ships its own red-first test. Anything touching the LOCKED
docs/warehouse_schema.md contract is plan-first (amend the doc same commit, E5).
Size ≤ ~300 LOC excluding tests; pure logic before I/O.

── AF-1 (HIGHEST $ IMPACT) — completeness is blind to sub-day capture gaps ──
EVIDENCE: PLAN_EVENT_PACKAGING.md V-EP2 (L531 "every spanned day has ≥1 row"),
  V-EP10 (L539 "trade count matches warehouse filter" = self-consistency, not
  warehouse completeness), V-EP12 (L553 drop-whole-day), completeness=pass gate
  (L363). No check joins the capture-gap record (work/quality_log.ndjson /
  freshness / incidents).
CLAIM: an event window with an INTERIOR gap (today's 06:00–09:00Z 401/lid-close
  outage, or any sleep gap) has rows on BOTH sides of the hole, so V-EP2/10/12
  all PASS and the pack is stamped completeness=pass while the middle of the
  game is missing. The warehouse is currently full of such gaps.
WHY MONEY: the excised segment is often the settlement-convergence run — highest
  vol, where maker edge AND adverse selection peak (Q6/Q8) — so a "green" pack
  yields a Q2 bound computed on holed data. D2: green must not lie.
FIX (plan amendment — event_validate.py is W-E3, not built yet, so fix the SPEC
  before it ships): add V-EP15 "Interior-gap completeness" to §6 — cross-
  reference win_start..win_end against the known capture-gap record (quality_log
  data_loss windows / freshness STALE spans); ANY overlap ⇒ completeness=
  degraded (never pass), offending window surfaced in _manifest.json. Add a
  seeded-defect row to the §5 table (interior_gap → V-EP15 FAIL) with the
  fixture spec pinned.
GUARDRAILS: D2, Q2, Q6. DISPOSITION: confirm the three checks are day-granular,
  then amend the plan doc; do NOT build event_validate here.

── AF-2 — headline % is trade-ROW count, not volume/notional ──
EVIDENCE: event_measure_split.py L83 count(*) AS n; L70 total_ticks; L115-124
  headline uses total_ticks. trades carries count_e4 (contract vol) + yes_price
  _e4 (schema trades table) — both ignored.
CLAIM: 42.8% events / 77.6% "ticks" are trade-row-weighted; 70k tiny sub-penny
  trades rank identically to 70k large ones, so the figure does not size profit.
  Direction (cross-midnight dominates) likely right; $ magnitude not established.
  Also split is TRADES-only, but MM edge lives on the L1 quote window (wider than
  the trade window) ⇒ true cross-midnight fraction for MM is understated.
FIX: add count_e4-weighted and notional (Σ price_e4·count_e4)-weighted headline
  + CSV columns alongside the row count (keep row count). Red-first test: a
  fixture with one row-heavy/low-volume event and one row-light/high-volume event
  must REORDER between row-weighted and volume-weighted views. BACKLOG: "trades-
  only split understates MM cross-midnight; L1-quote-window split is a separate
  measure (fold into W-E1 index)."
GUARDRAILS: Q2/Q4, D2. DISPOSITION: fix in tool + test; re-run --category Sports
  and record the new weighted headline next to the old one in SESSION_LOG.

── AF-3 — warehouse.load() may under/over-count under non-uniform archival ──
EVIDENCE: prior W-E0 audit deferred item (SESSION_LOG); warehouse_schema §Access
  L128-131 "any day present in the archive is excluded from the staging scan";
  guard in tools/warehouse.py::load().
CLAIM: if the exclusion uses a GLOBAL max-archive-date while categories archive
  on different days, querying the boundary day can drop lagging-category staging
  rows (undercount) or double-count. Hits EVERY backtest tape, not just packs.
FIX: first CONFIRM the guard's granularity by reading warehouse.py; if global,
  make the archive-vs-staging exclusion PER (category[,subcategory]), not global.
  Red-first test: category A archived through day D, category B only through D-1;
  a query spanning D returns B's day-D staging rows exactly once, none dropped.
  If already per-partition, REFUTE with evidence and close the BACKLOG item.
GUARDRAILS: D2, E1. DISPOSITION: verify-then-fix; full load() test set + new test
  green; schema doc updated if routing-contract wording changes (E5).

── AF-4 — the headline SQL path (collect()) is unit-tested only in Python ──
EVIDENCE: event_measure_split.py collect() L76-90 runs the real DuckDB aggregate
  (ts_utc // US_PER_DAY, any_value, min/max, count); tests exercise event_spans()
  (pure Python), not the SQL. Prior audit deferred this to W-E1.
CLAIM: 42.8%/77.6% come from an unverified SQL path; a divergence between the SQL
  day-index/any_value and the tested Python would silently move the number.
FIX: add a test building a minimal fixture warehouse (or in-memory DuckDB table)
  asserting collect() equals a hand-computed span set incl. a cross-midnight
  event; seed a defect (wrong // divisor) proven RED. ≤ a few dozen LOC.
GUARDRAILS: D2 (anti-fake-green), E1. DISPOSITION: fix in tests.

EXIT: one commit chain; tools.json/registry updated if any tool signature
changes (E3, check_registry green); make check + tests/run_pipeline.sh +
run_pytest green; SESSION_LOG entry (newest first) recording CONFIRMED/REFUTED
per item, the new weighted headline (AF-2), BACKLOG deferrals; then an
independent audit session reviews this remediation vs GUARDRAILS §6 before DONE.
Rollback: each item reverts as its own commit; the AF-1 plan amendment reverts
cleanly (no code).

---

## Dispositions (this session, 2026-07-07)

- **AF-1 — CONFIRMED.** V-EP1/V-EP2/V-EP10/V-EP12 (PLAN §6, L556-567) are
  day-granular or pack↔warehouse self-consistency; none joins the capture-gap
  record. An interior sub-day gap passes all checks. Remediation: plan-only —
  V-EP15 added to §6 + `interior_gap` red fixture row + completeness=`degraded`
  state. event_validate.py (W-E3) not built here.
- **AF-2 — CONFIRMED.** `collect()` loads `count(*)` only. Remediation: add
  `count_e4`-weighted (contracts) + notional (Σ HUGEINT price_e4·count_e4)
  weighted headline + CSV columns; keep row count; red-first reorder test.
- **AF-3 — CONFIRMED.** `_archive_files` `all_dates` glob (warehouse.py L94-95)
  uses `*/*` for category/subcategory → GLOBAL max-archive-date. Remediation:
  scope that glob to the queried category/subcategory; red-first non-uniform-
  archival test; all existing load() tests stay green.
- **AF-4 — CONFIRMED.** Tests exercise `event_spans` (pure) only. Remediation:
  fixture-warehouse test of `collect()` incl. cross-midnight, seeded wrong-
  divisor proven RED.
