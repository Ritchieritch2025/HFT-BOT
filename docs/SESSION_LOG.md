# SESSION LOG — newest entry first

Every session appends one entry before ending (see CLAUDE.md "Session exit
ritual"). This file is the cross-session memory: where work actually stopped,
which decisions landed in which files, what the next session must know.

---

## 2026-07-07 18:55 UTC — W-E1 real-dim wiring DONE: index builds on the LIVE warehouse + audit-fixed (2 defects)

- commits: b5b6228 --from-warehouse builder; 32a8ae4 audit remediation (NULL identity, empty build)
- decisions (in files):
  - event_index --from-warehouse (tools/event_index.py build_index_from_warehouse
    + _observed_by_market + _dim_markets): builds the index natively from the REAL
    warehouse + dim/latest, not a synthetic catalog dir. Observed activity
    aggregated in SQL over trades∪L1 (never materializes ticks); category/series/
    group from observed rows; open/close/status/mve join from dim by (event,ticker);
    lifecycle Fork-B. Reuses the tested infer_index_row core.
  - PROVEN ON LIVE DATA: 225,936 units (32,336 cross-midnight; 182,360 Q7-excluded
    MVE). Most units are catalog_incomplete + observed_merged because dim/latest
    holds only CURRENT markets (settled events dropped) — correct fail-closed.
- INDEPENDENT AUDIT: 2 CONFIRMED defects, both FIXED (32a8ae4):
  - NULL event/market identity → bogus unit + None-vs-str sort crash → filtered
    fail-closed. Empty build → executemany([]) crash → guarded (empty parquet).
  - Audit CLEAN: row-count not money-double-count, padding property, market-unit
    isolation, SQL escaping.
- KNOWN LIMITS (BACKLOGged, not defects):
  - dim/latest lacks settled markets → join catalog/settlements for accurate
    settled-game windows (W-E1.1 / STEP 5 settlements-first).
  - research reads (event_pack/export/validate → wh.load) can hit the LIVE ingest
    daemon's staging write lock (DuckDB single-writer, D6); load()'s 12s attach
    retry can be too short during a long ingest window. The real end-to-end export
    (index→pack→validate→CSV) is proven on synthetic (W-E7 tests) but was blocked
    on live data by this lock at the time; the real INDEX build succeeded.
- verification: make check GREEN; run_pipeline PIPELINE PASS; 12 event_index tests.
- STATUS: event-packaging W-E0/E1(+real-dim)/E2/E3/E7 all DONE + audited. The
  operator can select any market/event/series → clean complete separated CSVs,
  and the index now builds on real production data. Remaining: settlements-join
  (accurate settled windows), staging-lock hardening for research reads, W-E4
  (backtest wiring), W-E5/W-LC. SINGLE-OWNER RULE in force.

## 2026-07-07 18:30 UTC — W-E7 DONE: operator-facing per-event CSV export (the deliverable) + audit PASS

- commits: b18d260 W-E7 event_export.py + tests; 3bd016a audit hardening (dest path sanitize)
- decisions (in files):
  - tools/event_export.py: three-axis selector (--event | --market | --series)
    → clean per-event CSV folders under work/event_exports/<series>/<event>/
    (trades.csv, orderbooks_l1.csv, _event_summary.csv, _manifest.json). COMPOSES
    W-E2 packer + W-E3 validator — never re-derives money, so E4 fixed-point is
    byte-exact (test: sub-penny e4=90 stays "90"). _manifest.completeness =
    validator verdict; `pass` ONLY if event_validate passes, interior gap ⇒
    `degraded` (V-EP15/AF-1), never a silent green CSV. --market filters to the
    single market; no mixing events in one file.
- INDEPENDENT AUDIT: VERDICT PASS (0 blocking). New surface only (W-E2/E3 already
  audited). Confirmed clean: money round-trips byte-exact through csv reader/
  writer (no float), --market filter column-correct for both tables, completeness
  never rewritten pass, non-packed units surface skipped/refused (no crash), SQL
  escaped. 2 LOW notes: (1) dest path separators unsanitized → FIXED (3bd016a);
  (2) --market on an event ticker → 0-row export (operator misuse, row_counts=0
  signals it) → left as-is.
- verification: make check GREEN; run_pipeline PIPELINE PASS; registry 119; 4
  W-E7 tests green.
- STATUS — event-packaging plan: W-E0/E1/E2/E3/E7 DONE (each independently
  audited, defects fixed). The operator can now select any market/event/series
  and get clean, complete, separated CSVs — the original 2026-07-07 requirement
  is MET. Remaining (optional/gated): W-E4 (wire axes into mm_backtest/gate_calc),
  W-E5 (daily pack job), W-LC (lifecycle capture, operator-gated). Real-dim
  wiring for the index (W-E1 runs on catalog-dir/fixtures) + a durable structured
  capture-gap record are BACKLOGged. SINGLE-OWNER RULE in force.

## 2026-07-07 18:05 UTC — W-E3 DONE: event_validate (V-EP + V-EP15/AF-1) + load(event=); audit PASS-after-fix (5 defects)

- commits:
  - 4f48407 W-E3: event_validate.py (V-EP checks) + warehouse.load(event=) + schema §Event packs
  - 5ea341a W-E3 audit remediation (5 defects; V-EP15 was inert in production)
  - (BACKLOG: durable structured gap-record builder)
- decisions (in files):
  - warehouse.load(event=KEY, index_path=) resolves the unit's window + market
    set from the W-E1 index and returns exactly that episode across day
    partitions (three-axis selector §3.5); day-mode unchanged. → tools/warehouse.py.
  - event_validate.py stamps pass|degraded|fail. Implemented V-EP1/3/4/6/7/10/12
    + V-EP15 (AF-1 interior-gap: window∩capture-gap ⇒ degraded, never pass).
    Deferred checks (V-EP5/8/9/11/13/14) emitted as explicit skip (D2).
    → tools/event_validate.py; schema §Event packs (E5).
  - AF-1 OBLIGATION DISCHARGED: interior_gap red fixture built + proven RED
    (neuter V-EP15 ⇒ holed pack stamped pass; restored ⇒ degraded).
- INDEPENDENT AUDIT of W-E3 found 5 CONFIRMED defects, all FIXED (5ea341a):
  - D1 HIGH: V-EP15 was INERT in production — defaulted to a nonexistent
    capture_gaps.csv; missing record ⇒ gaps=[] ⇒ pass. Fixed: source is
    quality_log.ndjson (gaps_from_quality_log), FAIL-CLOSED when unavailable
    (V-EP15 skip ⇒ degraded, never pass).
  - D2 MED: deferred checks silently omitted (contradicted the D2 claim) → now
    explicit skip.
  - D3 MED: --refresh updated manifest window but not index.parquet → load(event=)
    truncated; event_pack now writes re-inferred win_end back to the index
    (atomic, preserves markets[]).
  - D4/D5 LOW: null-window TypeError + empty-markets IN () ParserException → now
    fail-closed. Audit CLEAN on day-mode regression, V-EP1 self-consistency,
    money (D5, no float), SQL escaping.
- verification: make check GREEN; run_pipeline PIPELINE PASS; registry 117
  tools; 22 event-packaging pytest cases green.
- blocked / handoff: event-packaging plan status — W-E0/E1/E2/E3 DONE (each
  audited). Remaining: W-E4 (three-axis wiring into mm_backtest/gate_calc),
  W-E7 (CSV export — the operator-facing deliverable), W-E5 (daily pack job),
  W-LC (lifecycle capture, operator-gated). V-EP15's gap source is a coarse
  best-effort parser (fails closed) — a durable structured gap-record builder
  is BACKLOGged. SINGLE-OWNER RULE still in force.

## 2026-07-07 17:35 UTC — Independent audit of AF-1..4 remediation: 1 real DEFECT found + fixed (AF-3 was incomplete)

- commit: 89a6b98 AF-3 residual — per-partition staging dedup for aggregate queries
- WHY: the brief's EXIT required an independent audit of the AF-1..4 remediation
  before DONE. It ran (fresh context) and CAUGHT A REAL DEFECT in my AF-3 fix
  (902db78): I only scoped the GLOB, which fixed pinned category+subcategory
  queries but left the identical undercount on AGGREGATE queries — category=None
  (exactly what `event_measure_split --category all` and every all-category
  backtest tape use) and subcategory=None still applied a scalar global max to
  all staging rows. Repro: category=None → 4, expected 6. My schema doc + §6
  self-audit had WRONGLY claimed it closed (a D2/Q10 "green lies" miss — the
  auditor flagged exactly this).
- FIX (proper this time): _archive_files returns a per-(sani_cat,sani_sub) max-
  date map; load() anti-joins staging against it (keep row iff its own partition
  unarchived OR ts past that partition's cutoff), matching staging (cat,sub) to
  the sanitized archive dir names via a SQL replica of warehouse_common.sanitize
  (sanitize is not invertible; verified real dirs are sanitized, e.g.
  "Aussie Rules"→"Aussie_Rules", "_none"→"none"). Red-first category=None case.
- audit's other findings: AF-1/AF-2/AF-4 confirmed SOUND; all red-first tests
  genuine; AF-2 money math clean (HUGEINT notional is load-bearing vs int64
  overflow, no float); AF-1 V-EP15 conceptually closes the interior-gap hole but
  the interior_gap red fixture is a PROMISSORY NOTE (must be built RED when W-E3
  lands — not satisfied yet, only specified).
- verification: full warehouse-consumer suite (gold/ingest/export/coverage/
  event/warehouse_status, 199 pytest + 3 self-running) GREEN — no regression on
  the shared read path. make check GREEN; run_pipeline PIPELINE PASS.
- blocked / handoff: AF-1..AF-4 brief now genuinely DONE (all 4 confirmed +
  remediated, remediation independently audited, the one found defect fixed).
  W-E3 (event_validate.py) MUST implement V-EP15 + build the interior_gap red
  fixture RED (the outstanding promissory note). SINGLE-OWNER RULE still in force.

## 2026-07-07 17:05 UTC — AF-1..AF-4 money-integrity brief: all 4 CONFIRMED + red-first remediated

- commits (one per item, revertable independently):
  - 4284597 AF-1 plan V-EP15 (interior-gap completeness) + brief verbatim
  - 902db78 AF-3 warehouse.load() per-category staging exclusion + red-first test
  - 3207bd5 AF-2 contract/notional-weighted split headline + reorder test
  - 240af42 AF-4 collect() SQL-path end-to-end test (+ warehouse= passthrough)
  - (BACKLOG: AF-2 L1-quote-window deferral)
- DISPOSITIONS (all re-derived from file:line; brief was a LEAD, verbatim in
  docs/plan_audits/event_packaging_af1-4_brief_2026-07-07.md):
  - AF-1 CONFIRMED — PLAN §6 V-EP1/2/10/12 are day-granular / pack↔warehouse
    self-consistency; none joins the capture-gap record → an interior sub-day
    outage passes while the mid-game (settlement-convergence, highest vol) is
    missing → a green pack feeds a Q2 bound on holed data. Fix = SPEC ONLY
    (event_validate is W-E3, unbuilt): V-EP15 interior-gap check vs quality_log
    data_loss/STALE spans → completeness=`degraded` (never pass), gaps in
    manifest; `interior_gap` red-fixture row; degraded state defined. → PLAN §6.
  - AF-2 CONFIRMED — collect() summed count(*) only. Added Σcount_e4 (contracts)
    + Σ HUGEINT(price_e4)·count_e4 (notional_e8), integer throughout (D5, no
    float on money). NEW WEIGHTED HEADLINE (Sports 7d): cross-midnight =
    40.7% events / 76.3% trade-rows / **82.1% contracts** / 78.2% notional —
    the row metric UNDERSTATED the $ split. → tools/event_measure_split.py.
  - AF-3 CONFIRMED — _archive_files computed max_arch_date from a GLOBAL `*/*`
    glob; a category archived through a later day than another silently dropped
    the lagging category's not-yet-archived staging rows (undercount on EVERY
    backtest tape → Q2). Scoped the date-set glob to the queried category/
    subcategory. → tools/warehouse.py + schema §Access (E5).
  - AF-4 CONFIRMED — tests exercised event_spans (pure) only; collect()'s SQL
    (ts_utc//US_PER_DAY, sum, HUGEINT) was unverified. Added a fixture-warehouse
    end-to-end test + warehouse= passthrough. → tests + tool.
  - REFUTED: none (all four held).
- red-first (anti-fake-green D2): AF-3 Crypto=2 RED before / 4 after; AF-2
  reorder FAILS with weights zeroed; AF-4 FAILS with a corrupted // divisor.
  All GREEN after fix. AF-1 is spec-only (no code path to redden yet — V-EP15
  redness is W-E3's obligation, pinned as the interior_gap fixture).
- GUARDRAILS §6 self-audit of this remediation: (1) phase 1.5, no gate skip ✓
  (2) no live orders ✓ (3) no strategy math; notional is an integer weight ✓
  (4) doesn't compute a Q2 bound — it CLEANS the data feeding it ✓ (5) WS-capture
  warehouse ✓ (6) every fix ships a red-first test ✓ (7) capture/ingest/export
  untouched; only a load() READ-path fix + read-only tools, all existing
  warehouse/gold/ingest/export tests green ✓ (8) each item its own commit,
  ≤300 LOC, AF-1 reverts cleanly ✓ (9) schema §Access + PLAN §6 updated (E5) ✓
  (10) explicitly de-lies: V-EP15, honest weighted labels, no-undercount,
  tested number ✓.
- context capsule: make check GREEN; run_pipeline PIPELINE PASS (registry 114
  tools). event_measure_split CSV columns changed: total_trade_rows +
  total_contracts_e4 + total_notional_e8 (was total_ticks). collect() + main
  gained warehouse=/--warehouse.
- blocked / handoff: brief's EXIT calls for an INDEPENDENT audit of THIS
  remediation vs §6 before DONE — not yet run (next step). Then resume the plan
  at W-E3 (event_validate.py must IMPLEMENT V-EP15 + the interior_gap red
  fixture). SINGLE-OWNER RULE still in force.

## 2026-07-07 16:45 UTC — W-E2 DONE: event pack materializer (money-integrity enforced) + audit PASS-after-fix

- commits:
  - 8c59da9 W-E2: tools/event_pack.py + tests/test_event_pack.py (6) +
    tools.json/run_pipeline registration
  - 8acab89 W-E2 audit remediation (2 MAJOR: AF-5 boundary, L1 idempotency)
- decisions (in files):
  - event_pack materializes per-unit packs work/event_packs/data/unit=<key>/
    {trades,orderbooks_l1}.csv + manifests/<key>.json from a W-E1 index row,
    read-only via wh.load(). MONEY-INTEGRITY enforced as code+tests: E4 integer
    columns selected VERBATIM (tools/event_pack.py _SRC), written byte-exact via
    csv.writer — zero float surface, no dollar-string derivation (AF-1/AF-2).
    count_e4 = contract qty; row_counts = cardinality (AF-3).
  - AF-5 stale-window: sealed-only + pack-time observed_last_ts re-check;
    REFUSE (fail-closed) if activity >= win_end, or --refresh re-infers
    (win_end = obs_last + 1, exclusive-end aware). → tools/event_pack.py.
- context capsule:
  - Independent W-E2 audit VERDICT: money-integrity CORE CLEAN (probed duckdb
    fetchall: INTEGER/BIGINT→int, no Decimal/float path; sub-penny e4=90 stays
    "90"; NULL→empty; archive/staging dedup inherited via load() max_arch_date;
    refuse/skip have no side effects). Found 2 MAJOR CONFIRMED, both FIXED in
    8acab89:
    * Defect-1: guard `obs_last > win_end` inclusive vs extract `ts_utc <
      win_end` exclusive → tick AT win_end silently clipped (reachable with
      post_pad_us:0). Fixed to `>=`.
    * Defect-2: L1 ORDER BY lacked a unique tiebreak (record_class constant per
      market) → same-µs rows reorder under DuckDB parallel sort → nondeterministic
      manifest md5 (breaks §3.3 idempotency). Fixed: order by all payload cols.
      Regression tests added (same-µs L1 determinism; exact-win_end refuse).
  - Pack test harness builds a synthetic 2-day archive (trades csv.gz + L1
    parquet) — reusable pattern for W-E3.
- blocked / handoff:
  - Next W = **W-E3** (event_validate.py V-EP* + warehouse.load(event=...)
    integration). The AF-1..AF-5 findings doc (docs/plan_audits/) is the
    checklist; V-EP checks should assert the money-integrity + no-clip
    guarantees W-E2 now provides.
  - SINGLE-OWNER RULE still in force (do not relaunch the parallel process).
  - Fixture-only so far: W-E1 index + W-E2 packs run on synthetic catalog/
    warehouse; real dim/latest wiring still pending (W-E1.1 or W-E3 prereq).

## 2026-07-07 16:10 UTC — Consolidation: single-owner restored; money-integrity audit recorded (AF-1..AF-5) + plan spec fixed

- commits:
  - 0c6d95d money-integrity audit doc + PLAN_EVENT_PACKAGING §3.6/W-E2 fixes +
    W-E0 label (AF-4)
- WHY THIS ENTRY: a PARALLEL process had been executing/committing the event-
  packaging Ws on this branch (it authored PLAN_EVENT_PACKAGING, rewrote the
  W-E1 fixtures/test under me, and committed 37523e2/771b2de incl. this
  session-log block). Two writers on one branch = clobber risk (hit once: a
  Write rejected mid-edit, a commit found nothing to stage). Operator HALTED the
  parallel process and put this session in sole charge. Branch confirmed
  quiescent (head stayed 771b2de through the audit).
- decisions / findings (now IN A FILE, closing the E2 gap):
  - The parallel process referenced a "W-E0 money-integrity audit, AF-1..AF-4,
    fix plan spec before W-E3" — but those findings were NEVER written anywhere
    (only a one-line SESSION_LOG mention) and are unrecoverable. Superseded by a
    fresh independent audit → docs/plan_audits/event_packaging_money_integrity_
    2026-07-07.md (AF-1..AF-5, verbatim).
  - AF-1/AF-2 (BLOCKER, D5): the §3.6 CSV contract would have floated money.
    export_day.STRATEGY_COLS emits yes_price_e4/10000.0 as FLOAT and drops the
    _e4 columns → sub-penny loss (0.0090 -> 0.01). Plan §3.6 now MANDATES E4
    integer columns byte-exact + integer-4dp dollar strings, forbids float/2dp,
    and forbids reusing STRATEGY_COLS. Gates W-E2/W-E7.
  - AF-3: count_e4 (contract qty) vs row_count/n_trades disambiguated in §3.6.
  - AF-4: W-E0 tool prints an honesty note ('ticks' = trade ROWS, not contract
    volume); plan _event_summary relabeled. (W-E0 corrupts no money — it never
    loads count_e4 — this is labeling only.)
  - AF-5 (correctness): a stale index row can clip late trades at pack time.
    W-E2 acceptance now requires pack-time window RE-INFERENCE + sealed-only
    packing + an anti-clip regression case.
- context capsule:
  - Audited base state: W-E0 (d3791d5,a74ed93) + W-E1 (37523e2) both verified
    green independently (14/14 event tests, pipeline PASS, registry 111 tools).
    W-E1 carries NO money path (audit CLEAN). The money risk is entirely in the
    NOT-YET-BUILT W-E2/W-E7 CSV materializers, now spec-guarded.
  - No W-E2 code written yet.
- blocked / handoff:
  - Next W = **W-E2 (pack materializer)** — build to the AMENDED §3.6 money-
    integrity contract + AF-5 pack-time-reinference acceptance. This is where
    the BLOCKER guards get enforced by tests (anti-float assert; late-trade
    anti-clip case).
  - SINGLE-OWNER RULE: do not re-launch the parallel event-packaging process;
    one writer on this branch.

## 2026-07-07 15:30 UTC — W-E1 DONE: event index builder + fixture tests green

- commits:
  - 37523e2 W-E1: `tools/event_index.py` — §3.2 A–G window inference, §3.4 parquet
    schema; fixture catalog + 9 pytest cases; tools.json + run_pipeline registration
- decisions:
  - W-E1 reads synthetic catalog dir CSVs only (markets/observed/lifecycle); does
    NOT extend warehouse.py (W-E3 owns `load(event=...)`). Derived output:
    `work/event_packs/index.parquet`.
  - Seal-state "recent ticks" gate tightened: `active` requires
    `now - seal_after <= t_last <= now` (future observed ticks no longer force
    active). Lives in `tools/event_index.py::_seal_state`.
  - Dim contract confirmed for later wiring: `dim/latest/markets.csv` uses
    `ticker` (not `market_ticker`), `open_time`/`close_time` as
    `YYYY-MM-DD HH:MM:SS` UTC, `mve_collection_ticker` for Q7.
- context capsule:
  - `infer_index_row()` implements §3.2 B–G; `build_index()` does A (event vs
    market unit from `config/event_packaging.yaml`). Window end priority:
    determined → settled → last_seen → scheduled_close; `observed_merged` when
    only ticks. Divergence flag: >2h between t_last/sched_close or
    sched_start/t_first.
  - Fixture run (`--now 2026-07-07 01:00:00`): 8 units — active 2, excluded 2
    (Exotics + KXMVE), partial 2, sealed 1 (KX-LC all settled), scheduled 1.
    Cross-midnight: KX-SPORT-EV1 only.
  - `make check` green; `./tests/run_pytest.sh tests/test_event_index.py` 9/9.
  - AF-1–AF-4 from W-E0 money-integrity audit still open (fix plan spec before
    W-E3; not remediated this session).
- blocked / handoff:
  - Next W = **W-E2** (pack materializer) per PLAN_EVENT_PACKAGING.
  - W-E1 not yet wired to real `dim/latest` — catalog-dir CLI is the Fork B
    interface until a dim-export helper lands (optional W-E1.1 or W-E2 prereq).

## 2026-07-07 13:53 UTC — Event-packaging plan committed + slotted; W-E0 DONE + audit PASS

- commits:
  - a96024a PLAN_EVENT_PACKAGING (per-event data marts; was untracked on disk —
    committed verbatim per full-text-preservation rule)
  - a3.. MASTER_SEQUENCE amendment: slot PLAN_EVENT_PACKAGING before STEP 5
  - d3791d5 W-E0: config/event_packaging.yaml + tools/event_measure_split.py +
    test + fixture + tools.json/run_pipeline registration
  - a74ed93 W-E0 audit remediation (real per-event category in CSV; gap-day test)
  - (BACKLOG appended: deferred W-E0 audit items)
- decisions:
  - Per-event packaging is a DERIVED layer on the warehouse, NOT a re-capture:
    every row already carries event_ticker/market_ticker/ts_utc; raw+archive stay
    day/hour-partitioned (D1). Design lives in docs/PLAN_EVENT_PACKAGING.md
    (three-axis market|event|series selector; per-event CSV bundles with
    completeness manifests; windows from observed activity + lifecycle Fork A/B,
    NEVER the unreliable scheduled close_time).
  - IMPORTANT provenance note: docs/PLAN_EVENT_PACKAGING.md already existed on
    disk (untracked, ~30KB) when this session went to write it — NOT authored by
    this session. It was read in full, judged guardrail-aligned + matching the
    operator requirement, operator-confirmed, then committed verbatim. My
    independent design converged on the same architecture (validation).
  - Category→packaging-unit policy lives ONLY in config/event_packaging.yaml (E2).
- context capsule:
  - W-E0 headline (real data, Sports, warehouse day 2026-07-06 + staging 07-07):
    42.8% of events (1353/3159) cross a UTC-midnight boundary and account for
    77.6% of TRADE ROWS / ticks (count(*), NOT contract volume — the trades
    `count` field is not summed). Example: KXMLBGAME-26JUL062210COLLAD had 1,348
    trade rows on day-06 vs 70,152 on day-07 → a `--date 2026-07-06` backtest
    sees ~1.9% of that game. Report tool: tools/event_measure_split.py →
    work/event_packs/split_report_<date>.csv (derived, gitignored).
  - Independent audit of W-E0: VERDICT PASS. Confirmed clean: day-index boundary
    math (µs, US_PER_DAY exact), event_spans multi-day/gap-day aggregation,
    load() archive/staging double-count guard (max_arch_date filter — verified
    no overlap, uniform archive to 2026-07-06), guardrails (read-only, P4, E3
    registry valid at 109 tools, no float-narrowing). Two MINORs fixed in
    a74ed93 (CSV real per-event category; gap-day test). Two deferred to BACKLOG:
    collect() SQL path not unit-tested (fold into W-E1); pre-existing load()
    global-max-archive-date guard could undercount if categories archive
    non-uniformly.
  - NEXT W = W-E1 (event index builder, tools/event_index.py): implements
    PLAN_EVENT_PACKAGING §3.2 window inference A–F + writes work/event_packs/
    index.parquet; consumes config/event_packaging.yaml. Read-only derived,
    executable now. §3.2/§3.4 in the plan are the spec.
- blocked / handoff: none blocking. Master-sequence order: STEP 1 (AWS migration)
  is still the top-level next STEP; PLAN_EVENT_PACKAGING (W-E1→E7, W-LC gated) is
  slotted before STEP 5 and its W-E1 is runnable whenever the operator wants to
  continue it. STEP 0 remains deployed/live.

## 2026-07-07 13:19 UTC — STEP 0 DEPLOYED to Mac pipeline (Option A) + capture-continuity measured

- commits: (this commit) docs/BACKLOG.md (capture-continuity finding) + this
  entry. No code change — deployment + measurement only.
- what happened:
  - The fixed ws_shadow binary is LIVE in production. The 13:00:01 UTC hourly
    respawn (PID 58944) loaded the on-disk build/ws_shadow that already carried
    the STEP 0 fix (built 12:27 UTC during verification), so the 401-lockout
    exposure has been CLOSED since 13:00:01 UTC. A clean rebuild from committed
    HEAD (3bdb092) is now staged on disk (13:18 UTC) and auto-loads at the next
    hourly boundary (14:00 UTC). build/ws_shadow.prev is a backup (note: it is
    ALSO a fix-containing binary; the true pre-fix rollback is
    `git checkout 72665c5 -- src include && make build/ws_shadow`).
  - Deploy method = Option A (no manual kill): the launchd supervisor
    (com.ritcardo.kalshi-pipeline) respawns ws_shadow each UTC hour, so the
    rebuilt binary deploys on the natural boundary with a ~0.5s handoff and no
    mid-hour shard-collision hazard.
  - Current session verified healthy: events climbing, capture shard
    firehose_13.ndjson.1 growing ~1MB/3s, reconnects=0 errors=0 epoch=1.
- MEASURED capture continuity (work/raw/date=2026-07-07 first/last recv_wall_ns
  per hour) — answers the operator's discontinuity concern with data:
  - Pure hourly restart handoff = ~0.5s (00->01 0.46s, 04->05 0.94s,
    12->13 0.54s). NOT a strategy problem.
  - The 401 bug WAS the damage: hours 06-11 each captured ~30s then went dark
    for the rest of the hour (2900-3800s holes). STEP 0 converts a mid-hour
    disconnect from "dead till next hour" into a seconds-scale reconnect blip.
  - Residual continuity holes = SYNCHRONOUS export in the capture loop: ~8min
    gap at 02:00 UTC (second-pass --force sweep) + ~78s at 00:00 UTC (daily
    export). Logged to docs/BACKLOG.md with fix direction (decouple Layer-3
    export from Layer-1 capture). This is the real remaining 硬伤 for live
    sports capture; STEP 1 (AWS migration) is a natural place to fix it.
- blocked / handoff: STEP 0 fully done + deployed. HEAD-built binary loads at
  14:00 UTC (verify: new PID != 58944 started ~14:00, log healthy). Next W is
  STEP 1 — AWS FULL MIGRATION (draft docs/PLAN_AWS_MIGRATION.md). Consider
  folding the export/capture decoupling (BACKLOG) into it.

## 2026-07-07 12:36 UTC — STEP 0 DONE + independent audit PASS — WS reconnect re-signs auth (401-lockout fix)

- commits:
  - 6d56aae STEP 0: re-sign WS auth headers before every reconnect (fix 401
    lockout) — src/ws_client.cpp + include/kalshi/ws_client.hpp +
    include/kalshi/ws_transport.hpp (mock inject_error) + tests/test_ws_client.cpp
  - d29c7e2 BACKLOG: audit residual (reconnect re-sign can be up to 30s stale
    at the backoff cap)
  - (quality_log entry appended to work/quality_log.ndjson — git-IGNORED, so it
    is NOT in a commit; it lives on-disk as the ops record. wp=STEP0-ws-resign,
    window 2026-07-07T06:00-09:00Z.)
- decisions (each in the named file):
  - Fix = client re-signs fresh handshake headers before every connection
    attempt via KalshiWsClient::refresh_auth(), called on transport Close AND
    on handshake Error → src/ws_client.cpp (start/on_close/on_message Error).
    Rationale: a 401 handshake rejection emits Error with NO Open/Close
    (ixwebsocket IXWebSocket.cpp:362), so re-signing only on Close would miss
    the 401-loop path entirely. ixwebsocket re-reads _extraHeaders each
    connect() on its own background thread; the synchronous re-sign runs to
    completion on that same thread before the next attempt.
  - Injectable Clock (KalshiWsClient::set_clock) added so the red-first test
    proves a strictly NEWER timestamp deterministically (no sleeps) →
    include/kalshi/ws_client.hpp + tests/test_ws_client.cpp.
  - Residual (max-backoff staleness, undocumented Kalshi auth window) is NOT
    fixed this session; logged with two follow-ups → docs/BACKLOG.md.
- context capsule (brief your replacement):
  - ROOT CAUSE (proven): headers signed once at startup; ixwebsocket
    auto-reconnect replays the stored KALSHI-ACCESS-SIGNATURE/TIMESTAMP → stale
    → Kalshi 401s every reconnect until the hourly process restart re-signs.
    Clock exonerated: sntp 46ms + REST auth passing during the 06:00-09:00 UTC
    outage. (facts: clock_skew_ms_vs_exchange=1138, preflight ±2000ms — that is
    LOCAL skew, NOT the server-side signature-timestamp window, which is
    UNDOCUMENTED anywhere in the vendor snapshot.)
  - VERIFICATION: red-first test_ws_client (+24 checks). With the two
    refresh_auth() calls removed, exactly 3 assertions FAIL (newer-ts-on-close,
    signature-re-signed, newer-ts-on-401); restored → ALL PASS. make check
    GREEN; run_pipeline.sh PIPELINE PASS (ws_shadow_mock pass, test_ws_client
    +24); production apps ws_shadow + ws_smoke build clean (no warnings).
  - INDEPENDENT AUDIT (fresh-context agent, adversarial): VERDICT PASS, 2
    MINOR, none blocking. Confirmed CLEAN: fix correctness (traced ixwebsocket
    threading + _extraHeaders re-read), thread-safety (header access is
    effectively single-threaded; watchdog in ws_shadow only counts silence,
    never reconnects), S4 (Error log prints only ixwebsocket's reason string,
    no key/sig material), fail-closed nullopt-signer path, red-first proof
    (empirically reproduced). MINOR-1 = max-backoff staleness (→ BACKLOG,
    d29c7e2). MINOR-2 = the "recovers/resubscribes" test assertion is not
    fix-dependent (the mock opens unconditionally) — the 3 timestamp
    assertions are the load-bearing proof; left as-is (still a valid
    resubscribe-path regression guard).
  - P4 capture continuity: the already-open data path (on_open/on_text/
    resubscribe/record/decode) is byte-for-byte unchanged; re-signing is added
    only on start/close/error, so the fix strictly CLOSES the capture gap and
    cannot introduce one.
- blocked / handoff: STEP 0 complete. Next fresh session starts **STEP 1 — AWS
  FULL MIGRATION** (docs/MASTER_SEQUENCE.md): first draft
  docs/PLAN_AWS_MIGRATION.md (seven-field Ws, §6 self-audit), then execute
  W-A0..W-A5 with the operator-approved riders. NOTE the STEP 0 fix is not yet
  deployed to the running Mac pipeline — it is committed on branch
  plan-live-validation-p0-p3 but the live ws_shadow is still the old binary;
  rebuild+redeploy of the pipeline binary (or the EC2 cutover in STEP 1) is
  what actually ends the 401-lockout exposure in production. Standing gates
  unchanged (fees OQ-1; 2026-07-13 seven-clean-days; live behind S1).

## 2026-07-07 11:53 UTC — MASTER SEQUENCE adopted as top-level queue (reorder, docs-only)

- commits: (this commit) docs/MASTER_SEQUENCE.md (new, operator sequence
  preserved VERBATIM) + CLAUDE.md queue pointer repointed to it + this entry.
  No code/pipeline/test changes; nothing touches a GUARDRAILS MUST.
- decisions:
  - The operator-authored FINAL MASTER SEQUENCE supersedes all prior
    orderings → docs/MASTER_SEQUENCE.md (verbatim). Prior queue
    (EXECUTION_PLAN + PLAN_GOLD_DATA_CONTRACT) is complete; the new queue is
    STEP 0 → STEP 6.
  - CLAUDE.md "Current execution queue" now points at MASTER_SEQUENCE.md, not
    EXECUTION_PLAN.md.
- context capsule: execution order for all future fresh sessions =
  docs/MASTER_SEQUENCE.md. STEP 0 is the earliest unfinished work and is
  URGENT: WS handshake auth headers are signed once at startup
  (ws_client.cpp:91); ixwebsocket auto-reconnect replays the stale signature
  ⇒ 401 loop until the hourly restart (root cause of the 2026-07-07 06:00–09:00
  UTC capture gap; clock exonerated — sntp 46ms, REST auth passing during the
  outage). Fix = re-sign fresh headers before EVERY connection attempt via the
  client-layer recovery ladder, red-first MockWebSocketTransport test, plus a
  quality_log entry, all in one commit. Standing gates unchanged: fees OQ-1
  awaits operator ratification; 2026-07-13 seven-clean-days gate; live trading
  behind ALL lifecycle gates + per-session operator confirmation (S1).
  Governing rule carried forward: one W per fresh session, independent audit
  after every W, exit ritual always.
- blocked / handoff: next session starts STEP 0 (WS reconnect signature fix).
  It has no plan doc of its own — it is a direct code fix (P4 note) per the
  MASTER_SEQUENCE STEP 0 spec. STEP 1 onward each requires drafting its named
  PLAN_*.md before executing its Ws.

## 2026-07-07 05:56 UTC — ENTIRE EXECUTION SEQUENCE COMPLETE (gold contract W1-W6 + EXECUTION_PLAN WP-00..09)

- commits: full chain fa18ff6(W1)..9343064(W6) — every WP/W landed with its own
  commit + independent-audit PASS in a separate fresh session; 4 audit-caught
  defects red-fixed (W1 FNV constant, WP-05 fees.verified->false, W6 F1/F1b
  work/raw guard), 3 real production incidents fixed with regression tests
  (taker_side BOOLEAN sniff, export/ingest race + shrink guard, ingest lock
  crash-loop)
- decisions:
  - Gold data contract PLAN_GOLD_DATA_CONTRACT W1-W6 fully built; day-06 gold
    certified GREEN (10.2M records, V2-V10 pass, V5/V7 verdicts filled)
  - fees.verified=false (fail-closed) blocks gate-mode net-profit until R
    ratifies OQ-1 -> config/kalshi_facts.yaml
  - depth expansion is DESIGN + operator-gated probe only (docs/PLAN_DEPTH_
    EXPANSION.md); probe NOT run (needs --operator-approved)
- context capsule: 107 tools registered, full pytest 281 passed + 1 xfailed
  (settlements-export xfail is an honest unbuilt-feature pin), make check ALL
  PASS, pipeline 4 procs alive (ws_shadow firehose + ingest daemon retry-
  hardened). Two NON-ENGINEERING gates remain before any trading: H-3 seven
  clean days (earliest 2026-07-13) and OQ-1 fee ratification (browser-confirm
  official PDF, flip fees.verified, rerun gate_calc). Gate-meeting input doc =
  work/mm/gate_report_<date>.md (day-06: 1809 signals, lifespan p50 1.0s, net
  REFUSED; unverified-preview median net -0.8c => naive bracket arb is
  net-negative after fees, confirming edge needs selection+pricing).
- blocked / handoff: sequence done. Next work is R-gated (fees ratify / clean-
  day clock) or a NEW plan (pricing model MM_ROADMAP 1.5, or gold W2.3 ws_seq
  adoption). Nothing auto-runnable is pending.
## 2026-07-07 — W6 DONE (probe-pending) — depth-expansion design doc + operator-gated probe

- commits: (this commit) docs/PLAN_DEPTH_EXPANSION.md + tools/depth_probe.py
  + tests/test_depth_probe.py (8 tests green) + tools.json appends
  (`depth_probe` kind=probe safety=network_read autorun=false needs
  creds+prod_env; `test_depth_probe`) + BACKLOG notes + this entry.
  NO production changes; apps/* untouched (read-only interface study only).
- decisions (each lives in the named file):
  - spec-derived limits table with evidence tags → PLAN_DEPTH_EXPANSION §1;
    the two binding limits (per-subscription market cap = asyncapi error 26,
    per-account connection cap) are NOT numerically documented — the probe
    empirically tests 50-markets-one-subscribe and 2-concurrent-connections
    as free side effects.
  - sizing = measured day-06 L1 rates per tier (read-only warehouse SQL,
    math shown) × full-depth multiplier calibrated on the 4-market watchlist
    sample (30–97×, n=3 in-game MLB — honest error bars); 50/200/500-market
    arithmetic in §3; expected 52/176/343 msg/s, conservative peak
    565/1,584/2,958 msg/s, 2.4–52.5 GB/day capture.
  - rollout options A (second depth instance) / B (extend firehose conn) /
    C (sharded multi-conn) enumerated with trade-offs, NONE chosen → §4,
    R decides in a separate operator-approved rollout plan.
  - probe protocol → §5: 900 s, top-50 of newest depth_target CSV, separate
    process + separate capture path work/probe/ (never work/raw, never
    work/metrics.ndjson — double-writer lesson), metrics redirected, zero
    REST tokens, refuses without --operator-approved (refusal demonstrated,
    exit 2), refuses live mode, refuses work/raw capture paths.
- blocked / handoff: **PROBE-PENDING** — measured msg-rate/bytes-by-tier
  table awaits the operator running `python3 tools/depth_probe.py
  --operator-approved` (15 min, watch freshness.py) and pasting the printed
  table into PLAN_DEPTH_EXPANSION §2/§3.

## 2026-07-07 05:15 UTC — WP-09 DONE (audit pending) — gate calculator: the three H-4 numbers by tested code

- commits: (this commit) tools/gate_calc.py + tests/test_gate_metrics.py
  (7 tests, TDD red→green: ModuleNotFoundError collection red pasted, then
  7 passed) + A1 wiring (tools.json `test_gate_metrics` pass_token "passed"
  + `gate_calc` kind=tool safety=pure; run_pipeline.sh suite line) + two
  BACKLOG notes + this entry. Report under work/mm/ (not committed, by
  the WP's allowed-writes rule).
- decisions (each lives in the named file):
  - signal = contiguous EPISODE of (100 − Σ L1 asks) ≥ min_edge_c over a
    fully-quoted bracket, never per-tick; default min_edge_c = 2.0¢ (sums
    ≤98¢ fire, the plan's 99–101¢ band silent), parameterized
    → tools/gate_calc.py docstring + pinned in tests/test_gate_metrics.py.
  - LOCF max quote age = 3600 s default (hourly-heartbeat cadence = the
    warehouse's documented change-only reconstruction bound; smaller drops
    valid state, larger trusts dead data), parameterized
    → tools/gate_calc.py docstring.
  - bracket universe: dims event_structure='bracket' preferred and CHECKED
    AT RUNTIME, but it is empty today (catalog lacks floor/cap_strike; dims
    today-only + 80k row cap) → documented fallback IS the effective path:
    event_ticker grouping + mutually_exclusive=true from events dim;
    ME-unknown events excluded fail-closed + counted; KXMVE prefix dropped
    (Q7) → tools/gate_calc.py + BACKLOG (catalog_sync rider).
  - full-quote guard: no evaluation until EVERY observed leg has a valid
    ask (0<ask_e4<10000) within the LOCF window — partial sums never fire
    (worst fake-signal class) → pinned by test_partial_quote_guard.
  - fees: mm_research.trade_fee is the ONLY fee fn (no second
    implementation); gate-mode net profit REFUSES on fees.verified=false
    (FeeNotVerifiedError, proven both directions on the real yaml + temp
    yamls); --preview-unverified-fees prints under a NON-GATE banner
    → tools/gate_calc.py + tests/test_gate_metrics.py.
- context capsule: REAL dry run 2026-07-07 ~05:05 UTC, defaults +
  --preview-unverified-fees: 2026-07-06 [FINAL] 8,210,082 L1 rows, 5,311
  events, 1,964 brackets evaluated (excl 1,304 single-leg / 874 ME-unknown /
  1,169 not-ME) → 1,809 signals; 2026-07-07 [PARTIAL] 6,903,405 rows, 2,209
  evaluated → 820 signals. Gate numbers: signal_count/day 1,314.5 (upper
  bound — exhaustiveness unverifiable, see BACKLOG), lifespan p50 1.0 s
  (p90 2,210.9 s, max 55,915.8 s, n=2,629, 257 censored at data end),
  net_profit REFUSED (OQ-1). NON-GATE preview @ unratified taker 0.07: net
  p50 −0.81¢/−0.77¢ per day — the p50 signal is net-NEGATIVE after taker
  fees (the hand-computed test fixture shows a 4¢ gross edge losing 0.57¢);
  positive tail exists (n(net>0) 648/291, likely non-exhaustive fragments).
  Report: work/mm/gate_report_2026-07-07.md (cites WP-06
  work/mm/research_2026-07-07.html, not recomputed). Implementation note:
  staging asks arrive as pandas nullable NA — bracket_signals coerces
  yes_ask_e4 to float64 (NaN) before the validity check (TypeError
  otherwise, hit on the real day-07 load). Suites: pytest 272 passed +
  1 xfail; make check tail ALL PASS; check_registry ok (105 tools).
  Pre-existing unstaged config/*.csv churn left alone per protocol.
- blocked / handoff: WP-09 needs its independent audit session (read-only,
  vs this diff). The gate meeting (H-4, human) is blocked on OQ-1 fee
  ratification (net_profit REFUSES until config/kalshi_facts.yaml
  fees.verified flips true — then rerun `python3 tools/gate_calc.py` for
  the real table; WP-07 fee-swap rerun also pending) and on the H-3
  clean-day clock (earliest admission 2026-07-13). All WP register items
  WP-00…WP-09 now built or closed; remaining: audits, WP-07, human steps.

## 2026-07-07 04:55 UTC — WP-08 DONE (audit pending) — daily quality check: the morning ritual as one command

- commits: (this commit) tools/daily_check.py + tests/test_daily_check.py
  (20 tests, TDD red→green: 20 collection/exec failures with the tool absent
  pasted, then 20 passed) + registry (tools.json `daily_check` kind=check
  safety=offline autorun=false pass_token "DAILY GREEN"; `test_daily_check`)
  + run_pipeline.sh suite line + BACKLOG + this entry.
- decisions (each lives in the named file):
  - freshness is CONSUMED via `python3 tools/freshness.py --json` subprocess,
    never reimplemented; test seam = `--freshness-json <file>` injecting the
    producer's exact JSON (rationale: test_freshness already proves the
    producer against a real Ingester-built staging; A1 one-infrastructure)
    → tools/daily_check.py docstring + tests/test_daily_check.py docstring.
  - hard failures (exit 1): freshness STALE/unreadable, manifest file missing,
    zero manifest rows for the completed day, md5 spot-check mismatch or
    manifest-listed file missing (write-once archive, D1/S2 fail-closed),
    gold day under <root>/quarantine/date=<D>[.N] OR validation verdict !=
    GREEN while the day sits in the green tree → tools/daily_check.py.
  - warnings (loud, exit stays 0): trades/orderbooks_l1 category row-count
    fall > --drop-pct (default 50%) vs prior-day manifest rows, gold built
    without a validation report, nonzero cumulative ws counters, unparseable
    quality_log lines → tools/daily_check.py.
  - quality_log finding extends the plan's <GREEN|WARNINGS:n> with RED:n —
    logging GREEN beside exit 1 would be the D2 green-lie; documented in the
    module docstring + pinned by test_quality_log_finding_red_on_hard_failure.
  - manifest history is deduped by (date, file_path), last row wins — a
    --force re-export's appended rows never double-count → read_manifest().
  - evidence records the measured staging lag every day (WP-03 BACKLOG note:
    a week of lag distribution before retuning the 600s threshold) →
    tools/daily_check.py + pinned in test_quality_log_append_is_schema_conformant.
- context capsule: REAL run 2026-07-07 ~04:49 UTC for window 2026-07-06:
  DAILY GREEN exit 0 — freshness FRESH (staging_lag 63.0s, capture 0.0s);
  manifest 223 rows (orderbooks_l1 8,210,082 / trades 1,913,798 /
  orderbooks_full 103,252), md5 spot 5/5; gold verdict GREEN (V2-V10 PASS),
  note: 2 row-level loader forensic files inside the day dir (known,
  quality_log'd W2.6); seq gaps: ws tail 400 lines, markers=0, all counters
  0, honestly "cumulative/not per-day"; 24h quality_log showed the 5 W2.6/
  WP-01 entries. Drop detection self-skipped (manifest holds ONLY 2026-07-06
  — first archived day); it arms itself after tonight's day-07 export. Two
  daily-check lines exist for the window (04:48 pre-lag-evidence, 04:49
  final) — append-only log, reruns are normal. Suites: pytest 265 passed +
  1 xfail; make check ALL PASS; check_registry ok (103 tools). Known
  pre-existing run_pipeline red (test_console lifecycle stage list, W4
  drift) is in BACKLOG, untouched (rule 1). Env override for the log path:
  DAILY_CHECK_QUALITY_LOG; tests never touch the real log.
- blocked / handoff: WP-08 needs its independent audit session (read-only,
  vs this diff). H-2 is live for R: run `python3 tools/daily_check.py` each
  morning and read it. Two BACKLOG riders added: per-day seq-gap
  instrumentation (ws_shadow untimestamped counters) and the gold
  not-built/unvalidated severity policy (R decision). Remaining WPs: WP-09
  (gate calculator; fees need WP-05 yaml — present) after WP-08's audit.

## 2026-07-07 04:37 UTC — WP-06 DONE (audit pending) — research notebook: K/z² + intervals, both spaces

- commits: (this commit) tools/mm_research.py + tests/test_research_metrics.py
  (9 tests, TDD red→green: ModuleNotFoundError collection red pasted, then
  green) + A1 wiring (tools.json test+tool entries, run_pipeline.sh line) +
  BACKLOG notes. Page/CSV under work/mm/ (gitignored by design, DoD says
  analytics/viz files only).
- decisions (files): **wiggle interpretation** — the plan fixture (mids
  [10,12,10,12] → K=12, z=2, wiggle=4) is degenerate between two readings;
  adopted K = Σ(Δmid)² (realized quadratic variation), z = net displacement,
  wiggle = (K−z²)/2 (mean-reversion-harvest bound), reversal_rate =
  sign-flips/(n_moves−1), space-invariant. Rationale + rejected reading in
  tools/mm_research.py docstring; R-confirm note in docs/BACKLOG.md.
  Heartbeat = is_snapshot AND price_e4 NULL (first-obs snapshots kept);
  intervals nearest-rank (gold convention); book-validity filter identical
  to mm_calibrate (bid>0, ask<100c, ask>bid). Fee guard READS
  config/kalshi_facts.yaml at call time; gate_mode with verified!=true
  raises FeeNotVerifiedError (currently false ⇒ refuses — live-tested in
  the suite against the real yaml AND mutated temp yamls).
- real run (2026-07-06 archived + 2026-07-07 staging PARTIAL): 14,649,242
  L1 rows → 675,800 heartbeats excluded → 11,423,208 valid → 40,550
  market-days. A2 divergence is real and large: price-space top-10 is
  Crypto-heavy (KXSOLE 26JUL0717 B78-B84 bracket, KXSHIBAD); log-odds
  top-10 promotes Commodities strikes (KXNATGASMON-26JUL3117-T2.499 px#6→
  lo#1, KXCOPPERW-26JUL1017 family px#21..47→lo#2..9) and demotes mid-range
  SOL brackets (B82 px#9→lo#47, Δ−38; SHIBAD px#2→lo#19). Exactly the
  vol∝p(1−p) overweighting A2 predicted. Page:
  work/mm/research_2026-07-07.html (68 KB, tables + inline SVG, stdlib-only,
  light/dark) + research_metrics_2026-07-07.csv (all 40,550 rows).
- battery: pytest 245 passed + 1 xfailed; make check tail ALL PASS;
  run_pipeline.sh PIPELINE PASS; check_registry ok (101 tools).
- pipeline continuity (P4): read-only throughout — load() only, ATTACH
  READ_ONLY with lock retry; no capture/ingest/export file touched.
- blocked / handoff: WP-06 independent audit pending per protocol. WP-07
  (fee swap re-run) stays BLOCKED on R ratifying fees provenance (OQ-1);
  when kalshi_facts.yaml flips verified:true, test_fee_placeholder_guard's
  reality assertion will flag for the WP-07 revisit by design. Rankings are
  gross wiggle — no fees/fills/simulation anywhere (WP-06 scope).

## 2026-07-07 — W5 DONE — ws_sid/ws_seq into orderbooks_full (the one production-adjacent change)

- commits: (this commit) tools/ingest.py + tests/test_ingest.py +
  tests/test_export_day.py + docs/warehouse_schema.md (E5 same commit).
- what landed: two nullable BIGINT columns `ws_sid`/`ws_seq` on
  orderbooks_full, populated from frame-level `sid`/`seq` (top-level in the
  WS frame, NOT in msg — W3.3 verified) for orderbook_snapshot /
  orderbook_delta; anything missing/non-int ⇒ NULL (`ws_int()`, D3).
  Migration: `Ingester._migrate_full_seq()` PRAGMA-checks and ALTERs the
  columns into a pre-W5 13-col staging table on init (nullable, instant,
  idempotent). INSERT switched to an explicit column list (`FULL_INSERT`)
  so it is correct on both fresh and migrated DBs.
- export_day.py: UNCHANGED — verified its `SELECT *` picks the columns up
  (test "archived parquet carries ws_sid/ws_seq"); load()'s
  union_by_name/UNION ALL BY NAME proven to union a 13-col pre-W5 archive
  with a new archive, missing cols ⇒ NULL (test 5b in test_export_day.py).
- TDD: both suites red first (BinderException: "ws_sid" not found in
  SELECT — exact missing feature) then green; full battery green:
  pytest 236 passed + 1 xfailed, make check ALL PASS, run_pipeline.sh
  PIPELINE PASS, check_registry ok (99 tools).
- continuity (P4): capture side untouched (ws_shadow already writes raw
  frames containing sid/seq); the ONLY production restart was the ingest
  daemon (checkpoint byte offsets make restart gap/dup-free). Restarted
  pid 35544, fresh [ingest] +L1/+trades lines within 90s; live staging
  migrated to 15 columns; all 103,252 pre-W5 rows read NULL ws_seq —
  expected, and stays NULL until a watchlist capture produces orderbook
  frames (firehose subscribes ticker+trade only).
- rollback: revert commit + restart ingester (checkpoint-safe). The
  ALTER-added columns are harmless if code reverts (nullable, ignored by
  the old 13-value positional INSERT? NO — old positional INSERT would
  break on 15 cols; a revert must also either drop the columns or rely on
  the reverted DDL creating a fresh staging — noted: revert commit ⇒ the
  old INSERT is positional 13-of-15 and DuckDB rejects it, so on rollback
  ALSO run: ALTER TABLE orderbooks_full DROP COLUMN ws_sid; DROP COLUMN
  ws_seq; (or delete staging.duckdb — rebuildable from raw, D1).
- gold adoption of ws_seq (W2.3 merge preferring real seq, per-sid gap
  detection post-W5 mode) is OUT of W5 scope — future workstream; gold
  builds on pre-W5 data keep recording `seq_unavailable`.
- blocked / handoff: PLAN_GOLD_DATA_CONTRACT W1→W5 all landed; W6
  (depth-expansion design + probe) is operator-gated. Independent audit of
  W5 pending per protocol.

## 2026-07-07 — WP-04 DONE (audit pending) + WP-01 CLOSED — permanent acceptance suite

- commits: (this commit) tests/test_pipeline_contract.py (12 tests, TDD
  red→green: exit-4 collection red, then 5 genuine assert fails during
  development, then green) + A1 wiring (tools.json entry pass_token
  "passed", run_pipeline.sh line) + BACKLOG notes.
- WP-04: new contracts — test_every_byte_accounted (checkpoint offsets =
  100% of complete-line bytes incl. rotation shard; partial tail excluded
  until completed, then counted exactly once), test_league_parse
  (KXMLB→MLB, KXKBO→KBO, KXNCAABB→NCAABB known_league; longest-match
  NCAAFB≠NCAAF; unknown sports prefix → family_prefix + needs_review=True —
  the plan's "_unknown + warning" maps to the review flag + V16 Class-B
  default, documented in-test), test_settlement_partitioned_by_settled_date
  (xfail strict=False: exporter has NO settlements fact — feature not built
  per protocol rule 1, BACKLOG filed), test_denormalized_columns_present
  (every archived l1+trade row carries category/subcategory/group/series/
  event, both classes), test_load_routing (late-staged row after export
  does NOT change the archived day = past reads archive; today reads live
  staging; no overlap double-count). Adopted per A1 by importing
  test_ingest helpers with independent fixture values: test_change_only
  (57+1+qty-only-change → 3 rows, full state tuple), test_heartbeat (3
  quiet hours → 3 hour-start heartbeats, state carried, NULL price),
  test_kill_restart_determinism (mid-line byte cut + fresh Ingester →
  row-for-row EXCEPT ALL equality both directions, all 3 tables).
- WP-01 CLOSED: folded tests green (test_e4_roundtrip "0.0325"→325→
  presented 0.0325 exactly via load(); "0.5000"→5000; legacy integer-cent
  levels_e4 *100; test_fractional_qty "5119.00"→51190000, "0.7500"→7500);
  quality_log entry appended (work/quality_log.ndjson, wp=WP-01, window
  2026-07-06T08:18-08:35Z, discarded-unrecoverable); live load() sample:
  2026-07-06 trades 1,913,798 total / 398,045 sub-penny (20.8%), e.g.
  EUCLIMATE-2030 e4=4140 → $0.4140.
- suite state: full pytest 236 passed + 1 xfailed; make check green;
  check_registry ok (99 tools). run_pipeline: all suites pass EXCEPT
  test_console — PRE-EXISTING W4 stage-list drift (verified on clean HEAD
  via stash), BACKLOG'd, out of WP-04 scope.
- blocked / handoff: independent audit session must run before WP-04 is
  marked DONE (anti-tautology check incl. adopted tests); test_console
  drift fix is a one-line expected-list update for a W4 rider; settlements
  export remains BACKLOG (R decision).

## 2026-07-07 — W4 DONE — coverage auditor (S1-S4, V14/V15/V16) + lifecycle research stage

- commits: (this commit) tools/coverage_audit.py + tests/test_coverage_audit.py
  (27 tests, TDD red→green) + lifecycle_check.py appended non-blocking
  "Coverage Audit (research)" stage + tools.json/run_pipeline.sh entries +
  docs/next_actions.md + BACKLOG notes.
- decisions (all in files):
  - V15 declared-list semantics → `tools/coverage_audit.py`: the firehose
    subscribes no orderbook_delta and NO declared watchlist file exists, so
    V15 reports `declared_list_missing` (documented, never invented); the 4
    observed full-depth markets on 2026-07-06 are legacy watchlist leftovers
    (KXMLBSPREAD/TOTAL-26JUL052130BOSLAA-*, KXWCGOAL-26JUL05MEXENG-…).
    Shrinkage vs a declared list = ERROR exit 1 (proven live + fixture).
  - Sanitized-category classification → `classify_category`: warehouse fact
    rows store wc.sanitize()d categories; classifier matches both spellings;
    `_unclassified` = unknown-category leakage, not a "new category" (V16).
  - Supervisor wiring + config/depth_watchlist.txt creation are
    OPERATOR-GATED → `docs/next_actions.md` (W4 forbidden-writes, P4).
- real audit 2026-07-06: traded=127,997 / L1=44,569 / full-depth=4;
  V14: 22,477 of 25,600 High+Mid lack L1 — ZERO Class A violations (all
  Class B by policy: 22,110 Exotics/MVE Q7-excluded + 367 promotion
  candidates → work/mm/promotion_candidates_2026-07-06.csv); S3: 5,274
  Sports traded, 2 missed (KXITFWMATCH-262799.99, KXITFMATCH-26JUL06BEASCO-
  SCR-26-EHAA, both Low tier); S4 → work/mm/depth_target_2026-07-06.csv
  (9,469 scoreable); V16 warns only on 43 null-category markets + 2
  catalog-missing series (KXMLBWINS, KXNEWOUTBREAK).
- blocked / handoff: promotion CSV is report-only — operator decides
  per-market vs wholesale promotion after a week of reports (plan §7);
  depth_target CSV feeds W6 (design+probe, operator-gated).

## 2026-07-07 04:05 UTC — WP-05 DONE — Discovery Mission: API ground truth report + kalshi_facts.yaml

- commits: (this commit) WP-05 discovery report + config/kalshi_facts.yaml +
  sandbox/discovery probe scripts/outputs + BACKLOG additions.
- decisions (all live in files, none chat-only):
  - Kalshi ground-truth constants → `config/kalshi_facts.yaml` (fees
    verified=true from official schedule w/ provenance caveat, rate limits
    VERIFIED-LIVE, endpoint existence, demo status, RTT/lag numbers).
    Downstream code imports this; no hardcoded fees/limits ever (WP-05
    contract). WP-07/WP-09 fee dependency is now UNBLOCKED pending R's
    ratification of OPEN QUESTION 1.
  - Full findings + 6 OPEN QUESTIONS + 5 CORRECTIONS →
    `docs/DISCOVERY_REPORT_2026-07-07.md`. Out-of-scope code fixes → BACKLOG
    (env.cpp stale demo message, request_spec F8 comment, sub-penny builder,
    clock-skew telemetry). No production code/data touched (read-only
    mission; A2 env gates untouched).
- context capsule (for a fresh session):
  - Headline live findings: (1) DEMO ENV IS UP (both hosts HTTP 200,
    2 shards) — engine's "unavailable" premise stale, policy still correct;
    (2) legacy POST /portfolio/orders REMOVED from spec — our wire.hpp
    already targets V2 /portfolio/events/orders and field names match spec
    exactly; (3) tier advanced = 300/s refill AND 600 capacity (2s burst)
    both buckets; cancel costs 2 tokens, create 10; no quota headers exist
    (200 or 429); GET /markets is CDN-cached ~15s (Q5 reinforcement);
    (4) private WS subscribe VERIFIED-LIVE: fill/user_orders/
    market_positions ack per-channel with sids, server pings 10s "heartbeat";
    (5) official fees: taker ceil(0.07·C·P·(1−P)), maker ceil(0.0175·…)
    ONLY on fee_type=quadratic_with_maker_fees series — KXNBA has maker fees
    LIVE, KXBTCD is plain quadratic; rounding is now centicent-ceil +
    per-order accumulator (fee_rounding.md), NOT cent-ceil (old PDF);
    (6) measured: RTT warm p50 35.6ms n=20; decode+apply 281.9 ns/msg;
    feed inter-arrival p50 10.2µs / p99 63ms on clean hour-12 capture
    (hour-08 splice window yields garbage extremes — filter epochs).
  - Gap-register spot checks all CONFIRMED (items 1,3,6,8,13); review deltas:
    gold layer + day-06 archive exist now.
  - Dead ends ruled out: kalshi.com fee PDF is Vercel-bot-gated (curl+
    WebFetch both fail) — used archive.org snapshot 2026-02-18 sha256
    b1a37aa7…; python has no cryptography/websockets libs — WS probe is
    stdlib + vendored OpenSSL 3.5.7 CLI signing (sandbox/discovery/
    ws_private_probe.py, reusable pattern for future probes).
  - Probe budget spent: ~240 read tokens, 0 write, 1 WS connection.
- blocked / handoff: R must adjudicate the 6 OPEN QUESTIONS (fee provenance,
  demo strategy, sub-penny builder, order-group dead-man, skew telemetry,
  batch-read billing probe). Sandbox scripts stay until report ACCEPTED (A6),
  then delete. WP-07 can start once OQ-1 is ratified.

## 2026-07-07 03:10 UTC — WP-03 DONE — freshness monitor: staging + capture lag, one command, STALE alarm

- commits: this commit (tools/freshness.py + tests/test_freshness.py +
  A1 registry appends: tools.json `freshness` check (pass_token FRESH) +
  `test_freshness` test entry + run_pipeline.sh suite line + BACKLOG
  notes). Nothing else touched; config/*.csv churn left unstaged.
- TDD: RED proven (8 failed, implementation absent — subprocess "No such
  file tools/freshness.py"), then 8 passed; full ./tests/run_pytest.sh
  198 passed; check_registry ok (96 tools); make check tail ALL PASS.
- decisions (rationale in tools/freshness.py docstring, E2):
  - TWO lags, alert if EITHER > threshold (default 600s, --threshold):
    (a) staging lag = now − max(ts_utc) over ALL fact tables
    (orderbooks_l1, trades, orderbooks_full — the test fixture puts the
    newest row in `trades` so an L1-only tool is rejected); (b) capture
    lag = now − newest *.ndjson* mtime under work/raw/date=<today>/
    (yesterday's dir also scanned so the first seconds after UTC midnight
    don't false-alarm; glob matches rotation shards .ndjson.N — the exact
    file class the shard incident missed, and there is a test for it).
    This split distinguishes "capture died" from "ingest behind" — both
    2026-07 incidents were capture-fine/staging-stale.
  - fail-closed (S2): missing staging, empty fact tables, no raw files,
    or read-only connect still locked after the retry window (12×5s,
    reader-side mirror of ingest.py connect_with_retry) ⇒ STALE exit 1
    with an explicit "unmeasurable" reason — never green on a metric we
    could not read. Verdict word FRESH appears only on pass (registry
    pass_token; exit code authoritative). --json for WP-08; --now/
    --staging/--raw-root injection for deterministic tests.
- live demo (DoD): injected-stale tmp fixture ⇒ VERDICT STALE exit 1
  (staging lag 93398.1s, capture lag 7207.4s, both reasons printed);
  REAL pipeline ⇒ VERDICT FRESH exit 0, staging lag 48.3s (newest
  ts_utc 2026-07-07T03:05:07.418Z via trades), capture lag 0.13s
  (work/raw/date=2026-07-07/firehose_03.ndjson) — read live against the
  running ingest daemon without disturbing it.
- blocked / handoff: WP-08 consumes `python3 tools/freshness.py --json`
  (fields: verdict, staging_lag_s, capture_lag_s, stale_reasons, ...);
  two BACKLOG notes for WP-08 (quiet-period threshold observation;
  branch on stale_reasons for behind-vs-unreadable paging).

## 2026-07-07 02:57 UTC — W3.2 DONE — V7 race/consistency report (REPORT-ONLY) measured on the real day, manifest verdict filled

- commits: this commit (tools/gold_v7_race.py + tests/test_gold_v7_race.py
  + committed v7_inverted_taker seeded-defect fixture + A1 registry appends
  (tools.json test+check entries, run_pipeline.sh line) + BACKLOG notes;
  nothing else touched — work/gold data and config/*.csv churn not
  committed). NOTE: implementation/tests/fixture/registry were written by
  the prior (interrupted) W3.2 session and left uncommitted; this session
  verified everything red/green from scratch, re-ran the real day live,
  and performed the exit ritual.
- TDD: RED proven (ModuleNotFoundError collection error with the
  implementation absent), then 16 passed; full ./tests/run_pytest.sh 190
  passed; check_registry ok (94 tools); make check tail ALL PASS.
- decisions (rationale in tools/gold_v7_race.py docstring, E2):
  - measurement: per TRADE record, print at the as-of touch? taker=yes ⇒
    trade_yes_price_e4 == ask_px_e4[0], taker=no ⇒ == bid_px_e4[0]; the
    as-of book is the state ON the trade record (§2.2 item 4 guarantees
    pre-trade). Vectorized numpy bincounts over 1.9M trades (~60 s day).
  - two populations NEVER pooled: covered_book (F_BOOK_COVERED, real
    full-depth as-of) vs l1_asof (uncovered; L1 state in slot 0).
  - honesty split (D2): invalid as-of book (incl. book_seq 0) /
    empty reference side / unknown taker_side = UNMEASURABLE buckets
    (book_invalid / side_empty / bad_taker_side), never races.
  - REPORT-ONLY binding (§2.2 point 5): NO threshold-enforcement path —
    module can only `return 0`; grep-proven by test (no `return [1-9]`,
    no sys.exit except sys.exit(main())). Proposals = nearest-rank p95 of
    per-market rates per population per category (+_global), pool =
    markets with ≥ 20 measurable trades (MIN_MEASURABLE, mirrors V5
    min-support). Slices above proposal ⇒ unsafe_for_microstructure=true
    in report + manifest — marked, never failed.
  - market class A/B from config/market_classes.yaml categories with
    PATH-SANITIZATION normalization ("Climate_and_Weather" sidecar vs
    "Climate and Weather" yaml — test-pinned); unknown category ⇒ B (V16).
  - subcategory not in the markets sidecar (BACKLOG W3.1 note) — fetched
    read-only from warehouse trades rows with a lock-retry loop; source
    recorded in the report (subcategory_source).
  - manifest: update_manifest_v7 per the V5 pattern — certified md5s AND
    V5 verdict asserted byte-identical, atomic replace, reader re-opened
    (W2.4 BACKLOG note now FULLY resolved).
- REAL day 2026-07-06 result (exit 0; work/gold/date=2026-07-06/
  v7_race_report_2026-07-06.json, 62 MB; verdict in manifest):
  support n_records=10,222,410, n_trade_records=1,909,088,
  n_markets_traded=127,994. covered_book: 3 markets / 744 trades / 134
  races = 0.180 (all Sports/Baseball/High/A). l1_asof: 1,540,431
  measurable / 394,096 races = 0.2558; unmeasurable 367,913 (book_invalid
  367,557 — dominated by Exotics/class-B traded-no-L1, measurable 0 by
  V10-honest construction; side_empty 356). By category (l1_asof):
  Sports 0.1435, Crypto 0.4051, Climate 0.3362, Financials 0.1545;
  Soccer subcat 0.031 vs BTC subcat 0.442 (15-min crypto ladders are the
  race hotspot). Proposed thresholds (FOR OPERATOR APPROVAL — not
  enforced): l1_asof _global 0.5714 (pool 3,012 mkts), Sports 0.5,
  Crypto 0.6207, Financials 0.4, Climate 0.5607, Commodities/Politics
  0.6667, Economics 0.7826; covered_book _global 0.2046 (pool 3).
  147 slices marked Unsafe for Microstructure Backtest (143 markets +
  3 subcats GDP/HYPE/Local + 1 more) — nothing failed, exit 0.
  Interpretation caveat (BACKLOG): ms-granular capture ts ⇒ l1_asof rates
  upper-bound true races (channel-alignment noise included); day-one
  baseline sample, not global truth (G4).
- red-proof (anti-fake-green): committed fixture tests/fixtures/
  gold_defects/v7_inverted_taker/ = 25 prints exactly at the correct
  touch with every taker_side FLIPPED — correct-sided twin measures
  race_rate 0.0, the fixture measures 1.0 (measurement catches the
  inversion); CLI on the defect day still exits 0 (NOT a threshold
  failure — report-only proven on the defect itself).
- blocked / handoff: next is W4 (coverage auditor) per the workstream
  order; W3.2 day-one thresholds await operator approval before any
  category threshold may become blocking (a separate, operator-gated
  change — no enforcement code exists yet by design).

## 2026-07-07 02:30 UTC — W3.1 DONE — δ distribution (V5) measured on the real day, manifest verdict filled

- commits: this commit (tools/gold_v5_delta.py + tests/test_gold_v5_delta.py
  + committed v5_shifted_book seeded-defect fixture + A1 registry appends +
  BACKLOG notes; nothing else touched — work/gold data not committed).
- TDD: suite written first, RED proven (ModuleNotFoundError collection
  error), then implementation → 14 passed; full ./tests/run_pytest.sh 174
  passed; check_registry ok (92 tools); make check tail ALL PASS.
- decisions (rationale in tools/gold_v5_delta.py docstring, E2):
  - δ per L1 change row = |Δts| to the NEAREST valid covered book record
    (BOOK_SNAPSHOT/BOOK_DELTA, F_BOOK_VALID) whose top (bid_px_e4[0]/
    ask_px_e4[0], empty sides normalized to L1's 0/10000 sentinels) equals
    the L1 view, inside a FIXED ±5 s scan window. SCAN_BOUND_US is a code
    constant, deliberately NOT a CLI flag (§2.3 V5: widening δ to absorb
    mismatches is FORBIDDEN — test-asserted that no bound/window CLI knob
    exists).
  - honesty split (D2): "never_agree" (book records in-window, none agrees)
    is a real mismatch; "uncheckable" (no book record in-window at all) is
    reported separately and NEVER counted as a mismatch — covered capture
    ran 0.5 h, L1 rows ran all day.
  - bad-markets rule: mismatch_rate_at_global_p99 > 0.05 (5x the ~1%
    beyond-p99 by construction) AND n_checkable >= 20 (below that a market
    cannot be condemned; its mismatches still count). Percentiles are
    nearest-rank on µs ints; ms only at the render edge.
  - L1 views come from the warehouse at report time (gold .bin cannot carry
    them: for covered markets the FSM state at an L1_TICKER record is the
    full-depth book; payloads are not serialized) — routed through the
    W2.1 load_l1 gates; scheduler heartbeats excluded and counted (72).
  - manifest verdict update: safety_verdicts.V5 replaced in place, certified
    md5s asserted byte-identical, atomic tmp+os.replace, GoldDayReader
    re-opened to prove certification (BACKLOG W2.4 note partially resolved;
    V7 half stays for W3.2). Report file NOT added to manifest["files"].
- REAL day 2026-07-06 result (exit 0, work/gold/date=2026-07-06/
  v5_delta_report_2026-07-06.json; verdict in the manifest):
  *** BASELINE SAMPLE (support: 4 markets) *** — NOT global truth (G4).
  support: n_markets=4, capture_hours=0.5, n_l1_rows=1767,
  n_full_depth_rows=103252, n_matched_pairs=1731.
  global delta_ms p50=0.000 p90=0.000 p99=0.000 max=245.411;
  mismatches: never_agree=0, beyond_global_p99=1, uncheckable=36;
  bad markets: none (the WCGOAL market has rate@p99=1.0 but only 1
  checkable row — min-support rule correctly refuses to flag on n=1).
  Per market: KXMLBTOTAL…-14 709/709 matched δ=0; KXMLBSPREAD…-BOS5
  628/628 δ=0; KXMLBTOTAL…-16 393/393 δ=0; KXWCGOAL… 1 matched δ=245.4 ms,
  20 uncheckable (its book has only 2 snapshots, L1 spread over the day).
  Cross-check: warehouse L1 fetch for the 4 tickers = 1767 rows, exactly
  the gold day's covered L1_TICKER count (0 rejected).
  Why δ≈0: capture timestamps are ms-granular (L1 ts 100% and book ts
  99.995% end in 000 µs) and ticker+delta frames for the same book event
  land in the same capture ms — day-one δ measures same-clock capture
  alignment, not cross-channel latency; do not read it as physics.
- red-proof (anti-fake-green): committed fixture tests/fixtures/
  gold_defects/v5_shifted_book/ = book tops price-shifted +100 E4 vs its
  committed l1_views CSV ⇒ 25/25 checkable rows never_agree, market
  flagged on bad_markets, δ pool EMPTY (delta_ms=None — no fake δ), and a
  100x scan bound STILL cannot absorb it (test-asserted); CLI on it exits
  1 with "BAD MARKETS SURFACED".
- blocked / handoff: W3.2 (V7 race/consistency report) is next in the gold
  plan; it needs the same warehouse subcategory lookup (sidecar has no
  subcategory column — BACKLOG note filed) and the same manifest-verdict
  helper pattern. warehouse.load() staging-ATTACH retry (12 s) was
  exhausted once during the real run (ingest lock burst) — outer retry
  succeeded; BACKLOG note filed.

## 2026-07-07 01:46 UTC — W3.3 DONE — golden Kalshi frames (V13) pinned on real captures

- commits: this commit (tests/test_kalshi_golden.py + 101 real verbatim
  frames under tests/fixtures/kalshi_golden/ + 3 doctored red-proof
  fixtures under tests/fixtures/gold_defects/golden_* + A1 registry
  appends; nothing else touched).
- fixtures (real, verbatim RawRecord lines; §2.2-style gates applied at
  sampling, 0 gate-skips in the sampled regions — provenance table in
  tests/fixtures/kalshi_golden/README.md):
  - 1 orderbook_snapshot + 50 orderbook_delta from work/live_capture.ndjson
    (2026-07-06 watchlist capture, 03:14–03:44 UTC) — fallback per the WP:
    the 24/7 firehose subscribes ticker+trade only (verified: 0 orderbook
    frames in a 200k-line sample).
  - 50 trades from work/raw/date=2026-07-06/firehose_12.ndjson
    (12:00–12:01 UTC, 36 markets), outside the 08:18–08:35 splice window
    by construction of the source file, with validation as the actual gate.
- REAL semantics discovered and pinned (tests + README):
  - trade frames DO carry sid+seq (doc I9 drift → BACKLOG);
  - snapshots share the sid seq counter; in-stream get_snapshot stamps the
    NEXT sid seq (protocol doc open question #2 answered, seq 2025 observed);
  - delta `ts` = ISO-8601 Zulu string with variable-length fraction
    (".52251Z" breaks fromisoformat) vs trade `ts` = epoch-seconds int;
    snapshot msg has NO ts fields (→ BACKLOG parser-audit note);
  - all prices/qty string fixed-point, byte-exact E4 round-trip via
    gold_load parse_e4/render_e4 on all 101 frames; yes+no price == $1
    on every trade; taker_side strictly yes/no (taker_outcome_side/
    taker_book_side also present).
- V12 wiring: module-level pytest.skip (loud operator message) when saved
  work/kalshi_spec_alignment.json is missing/red/>7d — logic mirrors
  gold_build.spec_gate (not imported: avoids the duckdb/warehouse stack);
  gate proven able to go red on synthetic missing/red/stale files.
- red/green: suite first run 15 failed (fixtures absent, TDD) → extraction
  → 16 passed; doctored dup-trade_id / seq-regression / float-price
  fixtures caught via the SAME checkers the real-frame tests use.
- acceptance demonstrated: ./tests/run_pytest.sh full = 160 passed
  (fixture git-tracking assertion included); tools/check_registry.py ok
  (90 tools). make check NOT run (background gold rebuild running, per WP).
- next: independent audit session for W3.3; protocol-doc drift rider
  (BACKLOG) needs an owner.

## 2026-07-07 01:30 UTC — W2.6 DONE (first real build GREEN) + archive taker_side narrowing found (day is trade-less, rebuild needed)

- commits: this commit (W2.6: tools/gold_build.py thin composition +
  registry entry + 6 BACKLOG notes; no module modified, no tests file —
  composition only per plan, smoke-verified via the real build)
- decisions (rationale in tools/gold_build.py docstring, E2):
  - HEARTBEAT mapping (closes W2.2/W2.3 note): L1 rows with
    is_snapshot=true AND NULL price_e4/volume_e4/open_interest_e4 = the
    ingester's hourly scheduler heartbeats → routed through the FULL W2.1
    L1 gates as source "orderbooks_l1_heartbeat", then re-kinded via
    Event._replace(kind=HEARTBEAT); is_snapshot=true WITH price data
    stays L1_TICKER. Real day: 428,204 of 8,108,826 L1 rows.
  - Merge-order disposition (closes W2.3/W2.5 policy question): merge
    sources are per-(channel, market) slices, each STABLE-sorted by ts_us
    at compose time — "Merge Order Violation" is structurally unreachable
    from loader output; same-ts intra-slice order preserves load() row
    order (Timsort); loader ts_regressions stay report-only (0 on the
    real day).
  - Liquidity tier (sidecar metadata): traded markets ranked by summed
    count_e4 desc (ties by ticker): High = top decile, Mid = next decile,
    Low = rest incl. untraded. (This day: all Low — zero accepted trades,
    see below.)
  - V12 gate reads SAVED work/kalshi_spec_alignment.json (status=pass AND
    ≤7 days old; was pass/0.73d); stale/red ⇒ exit 2 + operator
    instruction; the build NEVER auto-runs the network sync.
  - loader outputs written INSIDE the day partition so a validator
    quarantine moves the forensics with the day.
- acceptance demonstrated (real day 2026-07-06, exit 0, DAY GREEN):
  8,212,066 records / 44,442 markets / 4 covered; .bin 4,204,577,792 B
  (= 8,212,066×512); FSM clean (0 Invalid Book State / Sequence Gap /
  negative-delta; 428,204 heartbeats neutral; 1 crossed book flagged);
  validator matrix V2/V3/V4/V6/V8/V9/V10 all PASS; runtime 539.7 s,
  ru_maxrss 6.33 GB (peak footprint 17.7 GB incl. compressor);
  make check ALL PASS; pytest 144 passed; check_registry ok (89).
- context capsule — CRITICAL FINDINGS (full details + evidence in
  docs/BACKLOG.md 2026-07-07 entries):
  1. taker_side SILENT NARROWING: warehouse.py load("trades") on ARCHIVED
     days lets DuckDB read_csv sniff the yes/no column as BOOLEAN → comes
     back 'true'/'false'. Archive csv.gz verified to hold 'yes'/'no' raw.
     Consequence: the W2.1 gate quarantined ALL 1,863,197 archived trades
     (fail-closed, correct, nothing repaired) → gold date=2026-07-06 has
     ZERO TRADE records and all-Low tiers. The manifest's source_day
     carries the loader summaries, so the partition self-describes this.
     REBUILD the day (derived, deletable) after fixing load() typing
     (explicit types on read_csv) — warehouse.py is forbidden-writes for
     gold WPs, so the fix is an operator/rider change WITH regression
     test. Staging (VARCHAR) was clean — that is why W2.1's demo passed.
  2. trades EXPORT SHORTFALL: archive holds 1,863,231 day-06 rows but
     staging at 01:00 UTC still held 1,909,095 DISTINCT day-06 trade_ids
     → ~45.9k unique trades missing from the write-once archive (ingest
     lag vs midnight export cut?); invisible via load(); lost at staging
     prune unless reconciled.
  3. close_time resolves for only 72/44,442 tickers (catalog dim = 80,000
     open markets, settled intraday markets absent) — sidecar close_time
     left empty, surfaced; Q6 work needs a catalog retention story.
  - perf facts for W-BENCH: fetch 13 s; loaders 76 s; merge 137 s
    (81,515 sources); write_day 53 s; validate_day 251 s. Real day is
    ~12.6× the plan's ~650k/day estimate (8.2M records, 4.2 GB/day
    uncompressed → G8 window ≈ 59 GB, fine on this disk).
- blocked / handoff: gold date=2026-07-06 partition is GREEN but
  trade-less — do not use for trade research; rebuild after the
  warehouse.py taker_side fix (BACKLOG owns it). W3.1 (δ distribution)
  is next per the plan and is meaningful on book data now; W3.2 (V7
  race report) needs the rebuilt day with trades.

- commits: this commit (W2.5: tools/gold_validate.py + tests/
  test_gold_validate.py + 6 seeded-defect gold day fixtures + registry
  wiring + 3 BACKLOG notes)
- decisions (rationale in tools/gold_validate.py docstring, E2):
  - checks: V2, V3, V4, V6, V9, V10 run INDEPENDENTLY over a written day,
    PLUS the V8-shape gold_io.reconcile_trade_hashes wired into every run
    (W2.4 finding: must not stay dormant). A crash inside one check is
    caught as that check's failure — never masks the others.
  - all record access via RawDay, an md5-BLIND reader, so a V2 md5/
    manifest failure cannot stop V3-V10; V2 verifies explicitly (every
    manifest md5, record/trade/market counts vs parsed rows) AND surfaces
    the strict GoldDayReader's open-time refusals as reported violations
  - V3/V6 REUSE gold_merge.v3_violations/v6_violations verbatim on shims
    from .bin rows ("minted" inferred as book_seq exceeding the market's
    previous value — sound, not circular; V9 owns heartbeats explicitly)
  - quarantine = MOVE the whole partition to work/gold/quarantine/
    date=<D>[.N] (collision suffixed, nothing deleted/overwritten, P6) +
    validation_report_<D>.json written inside; CLI exits nonzero. Moving
    beats a marker file: the green tree cannot resolve the day by path
    (S2), partition stays byte-intact for forensics
  - six committed defect fixture days (v2_count_mismatch, v3_stream_seq_
    gap, v4_negative_level, v6_trade_lookahead, v9_heartbeat_diff,
    v10_coverage_lie), built via the W2.4 writer + targeted tampering
    with manifest md5s made self-consistent (so the CHECK fails, not the
    md5 gate; v2's manifest defect IS its check); deterministic
    regeneration: python3 tests/test_gold_validate.py
- acceptance demonstrated: suite RED first (ModuleNotFoundError:
  tools.gold_validate), then 12 tests green; per-check matrix printed —
  good day all-PASS GREEN; each defect day FAILs exactly its own check
  (all six others PASS) => QUARANTINE; V8 uuid-tamper day red with V2
  green (md5 gate not the catch); CLI demo: good day exit 0 in place,
  defect day exit 1 + partition moved under quarantine/ with report;
  full pytest 144 passed; make check ALL PASS; run_pipeline.sh
  == PIPELINE PASS == (test_gold_validate wired); check_registry ok (88)
- rollback: revert commit + delete work/gold/ (derived, rebuildable)
- next: W2.6 first real build (gold_build.py = thin composition of
  W2.1-W2.5, no new logic, --date 2026-07-06)

## 2026-07-06 — W2.4 DONE (gold writer/reader, TDD red-first, mutation/tamper-proven)

- commits: 7aebacb (W2.4: tools/gold_io.py + tests/test_gold_io.py + 2 io
  defect fixtures + registry wiring + 4 BACKLOG notes)
- decisions (rationale in tools/gold_io.py docstring, E2):
  - market_id: dense 0-BASED per-day ints (MARKET_ID_BASE, matching the
    stream_seq convention), minted in first-appearance order over the
    merged stream; DAY-SCOPED per §2.1 — same-day 1:1 market_id<->ticker
    enforced at write time AND re-checked at reader open (defense in
    depth); any violation = loud "BUILD FAILURE" GoldIOError
  - the reader is constructed per (root, date) so every access carries a
    date; cross_day() is the ONLY cross-day helper and requires a
    keyword-only market_ticker string — no market_id form exists, so
    market_id-only cross-day joins are structurally impossible (test
    demonstrates the same ticker minting DIFFERENT ids on two days)
  - manifest written LAST; carries md5 of the gold .bin AND every sidecar,
    record/trade/market counts, markets_missing_dim (missing-dim markets
    are kept with empty fields, never dropped — V10 spirit), builder
    version, source-day ids, V5/V7 "pending" verdict placeholders
  - reader refuses on: missing manifest, manifest not listing required
    files, missing listed file, any md5 mismatch, record-count/file-size
    disagreement (truncation with doctored md5s still refused, D2),
    sidecar bijection violation
  - taker_side encoding: yes=1 no=2 0=none; trade payload zero unless
    TRADE; trade_id_hash = fnv1a64(full UUID); reconcile_trade_hashes()
    (V8 shape) cross-checks every trade + flags missing/orphan sidecar
    rows; nlevels > uint16 = loud build failure (W2.2 BACKLOG resolved)
- acceptance demonstrated: suite RED first (ImportError: gold_io), then 27
  tests green; V11 runtime half = 10,020 records, mmap random access ==
  streamed parse on 1,000 sampled + all-rows equality; 4 mutants each
  turned their fixture red (dup-ticker check dropped, dup-id check
  dropped, reader md5 verify disabled, hash reconciliation disabled) and
  full green after restore; full pytest 132 passed; make check ALL PASS;
  run_pipeline.sh == PIPELINE PASS == (test_gold_io wired); check_registry
  ok (86 tools)
- rollback: revert 7aebacb + delete work/gold/ (derived, rebuildable)
- next: W2.5 validator harness (gold_validate.py: V2,V3,V4,V6,V9,V10 +
  quarantine; one seeded-defect gold file per check — the io writer can
  now produce them)

---

## 2026-07-06 — W2.3 DONE (merge iterator, TDD red-first, mutation-proven)

- commits: 73f4df8 (W2.3: tools/gold_merge.py + tests/test_gold_merge.py +
  4 merge defect fixtures + registry wiring + 3 BACKLOG notes)
- decisions (rationale in tools/gold_merge.py docstring, E2):
  - type_priority: TRADE=0, ALL other kinds=1. §2.2 mandates TRADE <
    BOOK_DELTA at equal ts_us; extended to snapshot/L1 (conservative: a
    trade is never credited with same-µs state that may postdate it);
    non-trade kinds deliberately share one priority — distinct priorities
    would reorder same-µs events INSIDE one source, breaking file order
  - source_file_order = (source_index, position); a "source" is any
    file-ordered event list; equal-key ties exhaust the lower-indexed
    source's run first (deterministic)
  - fail-closed precondition (S2): each source non-decreasing in (ts_us,
    type_priority) or GoldMergeError "Merge Order Violation" — W2.1's
    reported-not-fixed ts regressions therefore fail the merge; the
    disposition policy is W2.5/W2.6's (BACKLOG)
  - stream_seq is 0-BASED dense (documented choice, STREAM_SEQ_BASE)
  - book_seq minted (last+1) exactly on FSM APPLIED/INVALIDATED —
    invalidation IS a state mutation — identical by construction to W2.2
    book_version and enforced by an FSM-oracle replay test; book_seq 0 =
    "no book state ever emitted"; trades carry the market's current
    book_seq (pre-trade book at equal µs)
  - v3_violations/v6_violations read ONLY emitted records — W2.5 reuse
- context capsule: 24 tests green (105 whole scaffold); acceptance
  demonstrated: structural V6 on synthetic streams (every TRADE:
  ts(mint) <= ts(trade) AND merge-pos(mint) < merge-pos(trade), asserted
  by module checker AND independent in-test re-derivation AND FSM-oracle
  replay). Anti-fake-green: priority-inversion mutant => 6 red; book_seq
  mint+2 gap mutant => 13 red; no-mint-on-invalidation (duplicate) mutant
  => 5 red; restored green each time; v3/v6 checkers proven red on
  doctored records. run_pipeline PIPELINE PASS incl. new test_gold_merge
  suite; make check ALL PASS; registry 85 tools ok. gold_merge.py is 197
  physical lines incl. docstring (<= ~300).
- blocked / handoff: next fresh session runs the independent audit of
  W2.3, then W2.4 (gold writer/reader). W2.6 composition notes filed in
  BACKLOG: per-(channel, market) source slicing (warehouse load() orders
  by market, ts), ts-regression disposition policy, per-record
  fsm.state() recompute perf, HEARTBEAT row-mapping (merge side proven).
  GoldRecord layout untouched (frozen).

## 2026-07-06 16:30 UTC — W2.2 DONE (book FSM, TDD red-first, mutation-proven)

- commits: c5a2e9a (W2.2: tools/gold_fsm.py + tests/test_gold_fsm.py + 6 FSM
  defect fixtures + registry wiring + 2 BACKLOG notes)
- decisions (rationale in tools/gold_fsm.py docstring, E2):
  - invalidation CLEARS levels (hpp keeps them but refuses accessors —
    clearing gives identical observable zeros with a stronger
    no-resurrection bound); state() serves zeroed arrays + F_BOOK_VALID=0
  - every invalidation (Sequence Gap OR negative delta) counts as
    "Resync Required" — mirrors orderbook.hpp, which requests a resync on
    corruption exactly as on a gap
  - while invalid, deltas are refused BEFORE gap detection: a gap check is
    meaningless without a baseline; only a snapshot resets state + seq
  - coverage vs validity split: covered-but-invalid keeps F_BOOK_COVERED
    (subscription fact) while dropping F_BOOK_VALID (state fact); L1-only
    markets serve slot-0 top-of-book with F_BOOK_VALID=1, F_BOOK_COVERED=0,
    empty-side sentinels (bid 0 / ask 10000) zero their side
  - F_FROM_SNAPSHOT set by snapshot (and L1 is_snapshot rows), cleared by
    the first applied delta; heartbeats/trades create no book entry at all
- context capsule: 30 tests green (81 whole scaffold); acceptance
  demonstrated: negative-delta => INVALID no-clamp, invalid-until-snapshot,
  revalidation from later snapshot, permanent invalid without one, V9
  heartbeat neutrality, crossed flagged-not-repaired, yes-space transform
  (ask = 10000 - no_price), DEPTH-tail rest aggregation, both seq modes
  (seq_unavailable stated pre-W5). Anti-fake-green: clamp mutant => 9 red,
  latch-neuter mutant => 11 red, restored green both times. run_pipeline
  PIPELINE PASS incl. new test_gold_fsm suite; make check ALL PASS;
  registry 84 tools ok. FSM is 250 physical lines incl. docstring (≤~300).
- blocked / handoff: next fresh session runs the independent audit of W2.2,
  then W2.3 (merge iterator, pure logic on synthetic lists). HEARTBEAT
  row-mapping and uint16 nlevels write-gate filed in docs/BACKLOG.md for
  W2.3/W2.6 and W2.4. GoldRecord layout untouched (frozen).

## 2026-07-06 UTC — W2.1 DONE (typed loaders, TDD red-first, full-day real demo)

- commits: 05b203d (W2.1: tools/gold_load.py + tests/test_gold_load.py +
  golden/defect fixtures + registry wiring)
- decisions:
  - empty-side L1 encodings pinned from measured staging data: no bid =>
    (0, qty 0), no ask => (10000, qty 0); trade yes+no price == 10000 exactly
    -> gates in tools/gold_load.py (rationale in module docstring)
  - float detection without a `float` token (grep gate applies to the loader
    itself): type-name check + _FloatToken str-subclass sentinel for JSON
    numeric tokens — a float is never constructed from input, only rejected
  - string numbers parse as DOLLARS (integer digit accumulation -> E4);
    ints pass through as already-E4 (staging is typed) — both proven
    byte-exact round-trip on real golden rows (V1)
  - ts monotonicity/same-µs clusters tracked PER MARKET (load() orders by
    market_ticker, ts_utc — a global tracker false-counts every market
    boundary); report-only, never reordered
- context capsule: golden rows sampled from real 2026-07-06 staging OUTSIDE
  08:18–08:36 UTC (G5, padded; window noted in
  tests/fixtures/gold_golden_rows/README.md). Full-day demo through the
  gates: trades 1,180,038 rows -> 4,710 quarantined (4,676 duplicate
  trade_ids + nulls + 1 out-of-range + 4 bad pairs), 100% inside the splice
  window with ZERO date logic; l1 5.71M rows -> 12 quarantined; full 103k
  clean. Operator outputs at work/gold/loader_report_20260706_w21_demo.json
  (+ quarantine/, malformed_record_sample). Anti-fake-green: dedupe and
  float-grep gates each demonstrated red by mutation, restored green.
  44 tests; run_pipeline PIPELINE PASS incl. new test_gold_load suite;
  registry 83 tools ok. Staging-dup observation filed in docs/BACKLOG.md
  (possible W5 rider — ingest.py is W5-only).
- blocked / handoff: next fresh session runs the independent audit of W2.1,
  then W2.2 (book FSM, pure, zero I/O) per PLAN_GOLD_DATA_CONTRACT.
  GoldRecord layout untouched (frozen). Loader code lines: 303 effective
  (412 physical incl. docstrings) — within the ~300 size discipline.

## 2026-07-06 15:44 UTC — W1 DONE (implement -> audit FAIL -> red-first fix -> re-audit PASS)

- commits: 7a69329 (W1: GoldRecord 512B layout contract, C++/Python parity,
  gitignore fixture negation G2, TDD red-first), 17adaee (audit F1 fix:
  FNV-1a-64 offset basis had dropped its final digit; 3 known-vector parity
  tests added to BOTH languages, red-first)
- decisions:
  - FNV constants written in hex in both languages so they are character-
    identical -> include/trading/gold_record.hpp + tools/gold_dtype.py
  - numpy endian-test semantics ('<' canonicalizes to '=' on LE) -> corrected
    equivalent-strength assertion in tests/test_gold_dtype.py
- context capsule: independent audit (subagent, separate context) FAILED W1
  first pass on a real cross-language defect: C++ FNV basis constant
  1469598103934665603 (true: 14695981039346656037 = 0xCBF29CE484222325) —
  every hash diverged from Python, zero tests existed on either side. The
  anti-fake-green protocol caught exactly what it was designed to catch.
  Re-audit verified: vectors 0xcbf29ce484222325 (''), 0xaf63dc4c8601ec8c
  ('a'), 0x2b7e2a9e8505c3a4 (uuid) green both sides, make check ALL PASS,
  registry 82 tools. GoldRecord layout freeze now in effect for W2+.
- blocked / handoff: next session executes W2.1 (typed loaders) per
  PLAN_GOLD_DATA_CONTRACT @ 07c682d with R's blanket approval already given
  (2026-07-06 "全部批准"); golden-row fixtures must avoid the 08:18-08:35 UTC
  corrupted window (G5); one W per fresh session.
## 2026-07-06 15:24 UTC — R adjudication merged (07c682d); WP-00 executed and green

- commits: 07c682d (gold plan: R decisions G1-G8 merged; R-side patches for
  G3/G6/G7/G1/G2/G5/G8 + audit-side V5 support-size and never-prune list),
  WP-00 commit (BACKLOG, pytest.ini, tests/conftest.py, tests/run_pytest.sh,
  make test, tools.json run_pytest entry)
- decisions:
  - 342a114 commit-hygiene violation acknowledged: git add -A swept in
    operator plan edits + pipeline-churned config CSVs -> explicit-path adds
    from now on; churn issue -> docs/BACKLOG.md for R
  - pytest scaffold policy -> pytest.ini + tests/conftest.py (legacy suites
    canonical under make check until WP-04 migrates; empty collection = green
    via exit-5 mapping in tests/run_pytest.sh)
- context capsule: WP-00 DoD all green (make test 0-tests exit 0; registry 80
  tools; make check unbroken). Gold plan on disk now carries all 8 R
  decisions; FNV-1a-64 chosen for trade_id_hash; GoldRecord arithmetic
  512B via _reserved[5]. W1 remains BLOCKED on explicit operator approval.
- blocked / handoff: awaiting R approval to start W1. Nothing else in flight.
## 2026-07-06 14:09 UTC — Gold contract plan audited: APPROVED, 8 findings (3 MUST)

- commits: (this commit) docs/plan_audits/2026-07-06_gold_data_contract.md
- decisions:
  - Audit verdict + findings -> docs/plan_audits/2026-07-06_gold_data_contract.md
  - MUST-fix before W1: (G1) W1 blocked on WP-00 pytest scaffold; (G2)
    .gitignore negation for tests/fixtures/** + git-tracked assertion, else
    all gold fixtures are silently swallowed by global *.ndjson/*.csv.gz
    ignores; (G3) GoldRecord sizeof arithmetic is 488 not 512 — fix is
    _reserved[5] (40B) to land exactly on 512
- context capsule: offsets verified field-by-field (identity 0-32 incl
  book_seq, trade 32-56, arrays 56-440, tail 440-488). V5 day-one support is
  only 4 full-depth markets in a ~4h window — report must print support size.
  trade_id_hash algo is an OPEN QUESTION for R (xxhash=new dep; FNV-1a-64
  recommended, V8 collision check makes weakness detectable). V12 needs a
  7-day staleness bound. work/gold needs local retention (keep N days).
- blocked / handoff: R adjudicates G6 (hash algo) + confirms G3 fix; then
  WP-00 -> W1. Queue pointer unchanged (EXECUTION_PLAN WP-00 first).
## 2026-07-06 12:57 UTC — v1.0 verbatim recovered; EXECUTION_PLAN upgraded to v1.2 (all GAPs resolved)

- commits: (this commit) v1.0 verbatim preservation + v1.2 merge + Cowork
  session's CLAUDE.md exit-ritual + SESSION_LOG.md institutionalization
- decisions:
  - v1.0 original text → `docs/plan_audits/2026-07-06_EXECUTION_PLAN_v1.0_original.md` (verbatim, per handoff)
  - EXECUTION_PLAN.md → v1.2: v1.0 content merged with audit corrections
    C-A/C-B + amendments A1-A6; all 11 v1.1 [GAP]s resolved; PREVENT rule
    and quality_log schema retained
- context capsule: WP dependency spine restored (WP-01→04→{06,08,09};
  WP-03 early; WP-05 parallel; WP-02 closed done-prior). WP-06 wiggle
  formula pinned by fixture: mids [10,12,10,12] → K=12, z=2, wiggle=4.
  WP-09 gate fixture: 3-leg bracket asks sum 96c → BUY signal, gross 4c.
  S-B sealed content: World A/B merge → fill-sim calibration → tanh
  inventory + log-odds skew. Pipeline live: staging ~1M L1 rows day 1;
  first archive tonight UTC midnight; earliest gate 2026-07-13 (H-3).
- blocked / handoff: WP-00 is the next executable WP (BACKLOG.md + pytest
  scaffold + make test + quality_log). Nothing else blocked.
## 2026-07-06 (Cowork session) — SESSION_LOG + exit ritual institutionalized

- commits: pending — next repo session must include these files in its commit
- decisions:
  - Session exit ritual (commit + log entry + queue pointer) → `CLAUDE.md`
  - Current-queue pointer (EXECUTION_PLAN.md → PLAN_GOLD_DATA_CONTRACT.md) → `CLAUDE.md`
- blocked / handoff:
  - v1.0 original text still lives in a Claude Code session's context;
    operator has the prompt to write it verbatim to
    `docs/plan_audits/2026-07-06_EXECUTION_PLAN_v1.0_original.md` and fill
    all v1.1 [GAP]s from it. Do this BEFORE that session is closed.
  - Calendar: 48h capacity observation follow-up; 2026-07-13 earliest H-3
    seven-clean-days gate (Branch A vs maker adjudication happens at gate,
    same-change MM_ROADMAP update per audit A6).

## 2026-07-06 (Claude Code session) — rescue + EXECUTION_PLAN v1.1 reconstruction

- commits: ac2d2ad (batch rescue: 77 uncommitted items — pipeline code,
  GUARDRAILS, roadmap, audits, plans; secrets/data verified clean),
  c316329 (EXECUTION_PLAN.md v1.1-RECONSTRUCTED from audit only + PREVENT
  rule: audits must cite repo path + git hash, nonexistent path = reject)
- decisions:
  - quality_log schema defined → `docs/EXECUTION_PLAN.md` (WP-00 scope)
  - audit-protocol rule f → `docs/EXECUTION_PLAN.md`
- blocked / handoff: 11 [GAP] markers in v1.1 (see that session's GAP table);
  v1.0 verbatim text survives in that session's context — recoverable.

## 2026-07-06 (Cowork session) — codebase review, coverage audit, gold contract

- commits: included in ac2d2ad rescue batch
- decisions:
  - Gold Standard data contract (GoldRecord 512B, FSM/merge/validators
    V1–V16, W2 split into 6 atomic sub-steps with must-fail seeded-defect
    fixtures) → `docs/PLAN_GOLD_DATA_CONTRACT.md`
  - Coverage facts (day one): L1 13,455 mkts / traded 5,692 / full-depth 4;
    4,332 traded-no-L1 = Class B by design (4,134 Exotics/MVE); Sports fully
    Class A, 13 subcats present → recorded in the plan §0
- blocked / handoff:
  - Depth expansion (orderbook_delta for high/mid-liquidity + all sports) is
    design+probe only (W6), rollout needs its own operator-approved plan
  - Class B promotion policy decision deferred: per-market vs wholesale —
    operator decides after a week of promotion-candidate reports
  - work/ hygiene approved but not executed: metrics.ndjson rotation, stray
    log cleanup, raw gzip, archive off-box copy

## Before 2026-07-06 — prehistory (reconstructed)

34 commits, 8191062..020deef (2026-07-05): WS engine passes P0–P8, token/
rate-limit system T0–T6, PLAN_LIVE_VALIDATION P0–P3. Data pipeline went 24/7
on 2026-07-06. Note: until ac2d2ad, the entire Python pipeline layer had
never been committed — the failure class this log exists to prevent.
