# BACKLOG — out-of-scope observations (never fixed "while I'm here")

Per EXECUTION_PLAN operating protocol rule 1: anything noticed outside the
current WP's scope lands here as a note, never as code. Each entry: date,
noticed-during, observation, suggested owner.

- 2026-07-07 · WP-06 interpretation for R to CONFIRM · the plan's wiggle
  fixture (mids [10,12,10,12] → K=12, z=2, wiggle=(12−4)/2=4) is satisfied by
  two readings; tools/mm_research.py adopted **K = Σ(Δmid)² (realized
  quadratic variation), z = net displacement (last−first), wiggle = (K−z²)/2**
  — the mean-reversion-harvest bound, consistent units in both price and
  log-odds space. The rejected reading ("K = max mid excursion in cents, z =
  # direction reversals") also matches the fixture numerically but mixes
  cents with a squared count. Full rationale in the module docstring. If R
  intended the other reading, only wiggle_metrics() changes; tests pin the
  fixture either way. Owner: R.
- 2026-07-07 · noticed during WP-06 · staging L1 carries rows where
  yes_bid_e4=0 or yes_ask_e4=10000 (one-sided books) and a few crossed
  states; mm_research and mm_calibrate both filter them identically
  (bid>0, ask<100c, ask>bid) — if a third research tool appears, hoist the
  validity predicate into warehouse_common. Owner: next research-tool change.
- 2026-07-07 · noticed during WP-05 discovery · `src/env.cpp:165` +
  `tools/exchange_check.sh` claim "Kalshi's demo exchange is unavailable" —
  live probe shows demo is UP (HTTP 200, trading_active). The fail-closed
  rejection stays (A2); only the stated reason is stale. Fix message/comment
  when next touching env.cpp; demo strategy itself is DISCOVERY OPEN
  QUESTION 2 (R decision). Owner: R / next env.cpp change.
- 2026-07-07 · noticed during WP-05 discovery · `include/kalshi/
  request_spec.hpp:88` comment says per-item batch billing is a "local
  ASSUMPTION (F8, undocumented)" — Kalshi now documents per-item batch WRITE
  billing explicitly (getting_started/rate_limits.md). Narrow the comment to
  batch reads when next touching the file. Owner: next request_spec change.
- 2026-07-07 · noticed during WP-05 discovery · `wire.hpp:171-172,181` order
  builder emits integer cents + integer counts only; `deci_cent`/
  `tapered_deci_cent` markets and fractional counts exist (sub-penny = 13.4%
  of trades). DISCOVERY OPEN QUESTION 3: fix now vs fold into Phase-2
  QuoteManager. Owner: R decision.
- 2026-07-07 · noticed during WP-05 discovery · clock skew vs exchange
  measured +1138 ms (1 s resolution; within ±2 s preflight gate). Consider
  NTP check + adding skew to WP-08 daily health (OPEN QUESTION 5). Owner: R.
- 2026-07-06 · noticed during gold-plan audit response · `config/
  classification_review.csv` and `config/series_tags_report.csv` are
  git-tracked but rewritten hourly by the running pipeline's catalog refresh —
  every commit risks sweeping production churn (this polluted commit 342a114).
  Options: gitignore them (they are derived reports), or move them under
  work/. Owner: R decision.
- 2026-07-06 · noticed during W2.1 · staging `trades` carries 4,676
  duplicated trade_ids and ~29 NULL-field rows, all clustered in the
  08:18–08:35 UTC splice window (measured via the new loader gates on the
  full day: 4,710 quarantines, 100% inside the window). The gold loader
  quarantines them (correct, date-blind), but whether ingest.py should
  ALSO dedupe trade_id at the staging boundary (D3) is a separate
  ingest-side question — touching ingest.py is W5-only in the gold plan.
  Owner: R decision / possible W5 rider.
- 2026-07-06 · noticed during W2.2 · `EVENT_TYPE["HEARTBEAT"]` exists (frozen
  W1) and the FSM handles it (V9 neutrality proven), but the W2.1 loaders never
  emit it — warehouse L1 heartbeat rows load as L1_TICKER events. Which
  warehouse rows map to HEARTBEAT GoldRecords is undecided; belongs to the
  W2.3 merge / W2.6 composition, not the FSM. Owner: W2.3/W2.6.
- 2026-07-06 · noticed during W2.2 · GoldRecord `bid_nlevels`/`ask_nlevels`
  are uint16; the FSM reports full per-side level counts as Python ints. A
  side with >65535 levels would overflow at serialization — the W2.4 writer
  must gate (reject/report, never silently truncate, D2). Owner: W2.4.
- 2026-07-06 · noticed during WP-00 · legacy test suites (test_console,
  test_feed_readiness, test_verify_ws_capture, test_verify_feed_metrics,
  test_ingest, test_export_day, test_warehouse_status) are pytest-collectable
  (59 tests) but their canonical runner is `make check`/run_pipeline.sh; they
  are conftest-ignored in the pytest scaffold until WP-04 migrates them
  deliberately. Owner: WP-04.
- 2026-07-06 · noticed during W2.3 · the merge's fail-closed precondition
  (each source non-decreasing in (ts_us, type_priority)) has two W2.6
  composition consequences: (a) warehouse load() returns full/l1 rows
  ordered by (market_ticker, ts_utc), so a whole channel list is NOT
  ts-sorted — W2.6 must pass per-(channel, market) slices as merge sources
  (k = channels x markets), which also preserves per-market file order;
  (b) W2.1-reported within-market ts regressions (report-only at load
  time) will raise GoldMergeError ("Merge Order Violation") at merge time
  — whether the disposition is quarantine-the-rows or quarantine-the-day
  is a W2.5/W2.6 policy decision, not W2.3's. Owner: W2.6 (+W2.5 policy).
- 2026-07-06 · noticed during W2.3 · MergedRecord carries a full BookState
  computed by fsm.state() per emitted record (sorts both sides each call).
  Correct and fine at synthetic/pure scale; a full production day may want
  incremental state maintenance or emit-on-demand in the W2.6 builder.
  Owner: W2.6 (perf only, no semantics change).
- 2026-07-06 · noticed during W2.3 · partial resolution of the W2.2
  HEARTBEAT note: the merge handles HEARTBEAT events generically
  (stream_seq assigned, book_seq never advanced, state untouched — V9),
  so the merge side is proven; which warehouse rows are MAPPED to
  HEARTBEAT events (vs L1_TICKER) remains the open half. Owner: W2.6.
- 2026-07-06 · noticed during W2.4 · RESOLUTION of the W2.2 nlevels note:
  the W2.4 writer now gates uint16 overflow — bid/ask_nlevels > 65535 is a
  loud BUILD FAILURE (GoldIOError), never a silent truncation or a bare
  numpy OverflowError (test_nlevels_overflow_is_loud_build_failure).
- 2026-07-06 · noticed during W2.4 · G8 retention (GOLD_RETENTION_DAYS,
  keep newest 14 derived work/gold/date=<D>/ partitions) has no pruning
  helper yet — gold_io only writes/reads single partitions. Enforcement
  belongs with the W2.6 gold_build composition (or ops), and must prune
  ONLY derived date partitions, never raw/archive/manifests elsewhere.
  Owner: W2.6.
- 2026-07-06 · noticed during W2.4 · records_to_array serializes with a
  per-row Python loop (10k records ≈ 0.1s in-suite; a full production day
  ~650k events extrapolates to seconds). Fine for correctness-first;
  W-BENCH should measure and W2.6 may vectorize (no semantics change).
  Owner: W-BENCH/W2.6.
- 2026-07-06 · noticed during W2.4 · manifest safety_verdicts are written
  as V5/V7 "pending" placeholders. W3.1/W3.2 will rewrite the manifest to
  fill them; note the manifest is the (unhashed) root of trust, so any
  verdict update must NOT touch the gold/sidecar md5s it certifies.
  Owner: W3.1/W3.2.
- 2026-07-06 · noticed during W2.5 · validator hot loops (bin_shims,
  check_v9) walk records one-by-one in Python. Fine at fixture scale
  (11-record days validate in ms); a full production day (~650k records)
  extrapolates to seconds per check. Correctness-first stands; W-BENCH
  should measure and W2.6 may vectorize (no semantics change). Owner:
  W-BENCH/W2.6.
- 2026-07-06 · noticed during W2.5 · partial resolution of the W2.3
  "Merge Order Violation disposition" note: W2.5 validates WRITTEN days
  (post-merge), so a source that failed the merge never reaches the
  validator. The remaining half — what gold_build does when a loader
  reports ts regressions (skip source? build with report? refuse day?) —
  is composition policy. Owner: W2.6.
- 2026-07-06 · noticed during W2.5 · quarantined partitions live under
  work/gold/quarantine/date=<D>[.N] and are NOT covered by the G8
  GOLD_RETENTION_DAYS wording (which prunes date partitions). W2.6's
  pruning helper must decide: quarantine dirs are derived too, but they
  are forensic evidence for a failed build — suggest pruning them only
  after the day rebuilds green. Owner: W2.6.
- 2026-07-07 · noticed during W2.6 · RESOLUTION of the W2.2/W2.3 HEARTBEAT
  mapping note: gold_build maps warehouse L1 rows with is_snapshot=true AND
  NULL price_e4/volume_e4/open_interest_e4 (the ingester's hourly scheduler
  heartbeats, per docs/warehouse_schema.md) to EVENT_TYPE HEARTBEAT — routed
  through the full W2.1 L1 gates as source "orderbooks_l1_heartbeat", then
  re-kinded; is_snapshot=true WITH price data stays L1_TICKER (real state).
  Measured 2026-07-06: 428,204 of 8,108,826 L1 rows are scheduler heartbeats.
  Rationale in tools/gold_build.py docstring (composition decision 1).
- 2026-07-07 · recorded during W2.6 (W2.5-audit finding 1) · RawDay sidecar
  CSV parse sits outside per-check isolation — a corrupt sidecar row (e.g.
  non-integer market_id/stream_seq) raises in RawDay.__init__ before any
  check runs ⇒ traceback exit 1 but no quarantine move and no validation
  report. Owner: W2.5 rider.
- 2026-07-07 · noticed during W2.6 (SILENT DATA NARROWING, D2/D3/D5 class) ·
  warehouse.py load("trades") corrupts taker_side for ARCHIVED days: the
  archive csv.gz files store 'yes'/'no' (verified raw:
  trades__Climate_and_Weather__Climate_change__2026-07-06.csv.gz row 1 has
  taker_side=no), but load()'s read_csv() auto-sniffs a yes/no-only column
  as BOOLEAN, and the staging union casts it back to VARCHAR 'true'/'false'
  (verified: SELECT DISTINCT taker_side via load() for 2026-07-06 returns
  None/'false'/'true'). Staging (typed VARCHAR) is unaffected — that is why
  the W2.1 demo at 12:09 saw clean 'yes'/'no'; the defect appeared when the
  day was first archived at UTC midnight. Consequence: the W2.6 first real
  build's inconsistent-taker_side gate quarantined ALL 1.86M archived trades
  (correct fail-closed behavior; nothing repaired), so gold date=2026-07-06
  contains zero TRADE records and its liquidity tiers default to Low — the
  partition MUST be rebuilt (derived, safe to delete) after the fix. Fix
  belongs in warehouse.py (explicit column types on read_csv, e.g.
  types={'taker_side':'VARCHAR'}, or trades→parquet in the exporter) WITH a
  regression test; warehouse.py is forbidden-writes for gold WPs. Owner:
  R decision / warehouse rider (urgent — silently poisons any archived-day
  trades research).
- 2026-07-07 · noticed during W2.6 · trades EXPORT SHORTFALL for 2026-07-06:
  the write-once archive holds 1,863,231 rows (manifest.csv sum agrees), but
  staging at 01:00 UTC 07-07 still held 1,913,798 rows / 1,909,095 DISTINCT
  trade_ids inside [2026-07-06 00:00, 07-07 00:00) UTC — i.e. ~45.9k unique
  day-06 trades (~2.4%) exist in staging but not in the final archive
  (likely ingest lag vs the midnight export cut). load() reads archived days
  from the archive only, so these rows are invisible now and will be LOST at
  staging prune unless reconciled. Needs an export straggler audit /
  late-row reconciliation policy (export_day.py is W5-only for gold WPs).
  Owner: R decision / export rider.
- 2026-07-07 · noticed during W2.6 · close_time coverage gap: the catalog
  dim (work/warehouse/catalog/markets, mirrored by dim/latest and
  dim/snapshots) holds exactly 80,000 rows (pull cap) of currently-open
  markets and does NOT retain settled markets — only 72 of the gold day's
  44,442 tickers resolve a close_time (settled intraday sports dominate).
  Sidecar close_time is left empty (surfaced by gold_build's coverage
  line, never invented), but Q6 expiry-awareness work will need a
  settled-market dim retention/backfill story. Owner: R decision /
  catalog rider.
- 2026-07-07 · noticed during W3.3 · protocol doc drift: real trade frames
  DO carry `sid`+`seq` (monotonic on their own sid), but
  docs/kalshi_ws_protocol.md invariant I9 states ticker/trade/lifecycle
  have no seq. Also the doc's open question #2 (whether in-stream
  `get_snapshot` seq-stamps with the next sid seq or re-baselines) is now
  answered empirically: a snapshot at seq 2025 mid-delta-stream, i.e. it
  stamps the next sid seq. Both pinned by tests/test_kalshi_golden.py;
  the doc update itself is out of W3.3's allowed writes. Owner: protocol
  doc rider (with SidStream owner review — does gap logic assume trades
  are unsequenced?).
- 2026-07-07 · noticed during W3.3 · orderbook_delta `ts` is an ISO-8601
  Zulu string with VARIABLE-length fractional seconds (trailing zeros
  trimmed, e.g. ".52251Z"); Python's `datetime.fromisoformat` rejects it
  on 3.9. Any consumer parsing delta `ts` that way will crash on ~random
  frames; trade `ts` is epoch-seconds int (different encoding, same field
  name). Audit ingest/dashboard parsers for fromisoformat use on WS ts.
  Owner: R decision / ingest rider.
- 2026-07-07 · noticed during W2.6 · G8 retention pruning (keep newest 14
  work/gold/date=<D> partitions) and the quarantine-dir pruning policy are
  deliberately NOT implemented in gold_build (composition-only per the plan;
  only one gold day exists). Owner: ops / R decision (follow-up rider).
- 2026-07-07 · noticed during W3.1 · PARTIAL RESOLUTION of the W2.4
  manifest-placeholder note: gold_v5_delta.update_manifest_v5 fills
  safety_verdicts.V5 in place — certified md5s (manifest["files"]) asserted
  byte-identical before an atomic tmp+os.replace write, then GoldDayReader
  is re-opened to prove the certification chain still verifies; the V5
  report file is deliberately NOT added to manifest["files"]. V7 half
  remains for W3.2 (same helper pattern applies).
- 2026-07-07 · noticed during W3.1 · the markets_<D>.csv sidecar dim has no
  subcategory column (category/close_time/liquidity_tier only), so the V5
  report fetches subcategory from warehouse L1 rows at report time — V7
  (W3.2) will need the same lookup for its per-subcategory breakdown, and
  any offline consumer of the sidecar alone cannot slice by subcategory.
  Adding the column is a gold_io MARKETS_COLS change (sidecar format, not
  the frozen 512-B struct) + rebuild. Owner: R decision / gold_io rider.
- 2026-07-07 · noticed during W3.1 · V5 needs the warehouse at report time
  because the gold .bin cannot carry the L1 channel's view of covered
  markets (FSM state at an L1_TICKER record is the full-depth book; event
  payloads are not serialized — layout frozen). Fine while gold days and
  their warehouse days coexist; if gold partitions ever outlive warehouse
  access, V5 becomes unmeasurable retroactively. Option: persist covered
  markets' L1 views as a (new, manifest-unlisted or listed-at-build)
  sidecar at build time. Owner: R decision.
- 2026-07-07 · noticed during W3.1 · warehouse.load()'s staging ATTACH
  retry (8 x 1.5 s = 12 s) was exhausted once during the real V5 run — the
  ingest daemon's lock window can exceed it (run failed with "Conflicting
  lock", succeeded on outer retry). Callers currently need their own
  retry-or-be-patient loop; consider a longer/backoff window in load().
  warehouse.py is forbidden-writes for gold WPs. Owner: warehouse rider.
- 2026-07-07 · noticed during W3.2 · RESOLUTION of the W2.4
  manifest-placeholder note (V7 half): gold_v7_race.update_manifest_v7
  fills safety_verdicts.V7 + safety_verdicts.unsafe_for_microstructure in
  place with the same guarantees as the V5 helper (certified md5s AND the
  V5 verdict asserted byte-identical, atomic tmp+os.replace, GoldDayReader
  re-opened to prove certification). Both verdicts are now filled on the
  real 2026-07-06 day; the "pending" placeholder path only applies to
  freshly built days. The report file is NOT added to manifest["files"].
- 2026-07-07 · noticed during W3.2 · the warehouse trades dim carries BOTH
  "Interest_Rates" and "Interest_rates" as subcategory spellings (both
  appear in the real V7 by-subcategory breakdown with different rates:
  0.481 on 79 vs 0.026 on 39 measurable). Any per-subcategory consumer
  splits this population in two. Classification-dim hygiene (case-fold or
  canonicalize at ingest/classification time). Owner: warehouse /
  classification rider (forbidden-writes for gold WPs).
- 2026-07-07 · noticed during W3.2 · v7_race_report_2026-07-06.json is
  62 MB (full per-market detail for 127,994 traded markets — D2: nothing
  hidden). It lives inside the derived day partition (G8 retention prunes
  it) and the printed table caps at top-30 + marked; consumers should
  stream/slice the JSON. If day sizes grow, a compact per-market CSV
  sidecar could accompany the JSON. Owner: W-BENCH / ops rider.
- 2026-07-07 · noticed during W3.2 · interpretation caveat for the day-one
  race numbers (same physics as the W3.1 δ≈0 finding): capture timestamps
  are ms-granular and trade-vs-L1 frames of one exchange event can land in
  either order inside the same ms, so l1_asof "races" (25.6% global;
  40-60% on 15-min crypto ladders) measure capture-channel alignment PLUS
  true races, upper-bounding the latter. covered_book (true full-depth
  as-of) is only 3 markets / 744 trades / 0.5 h — 18.0% raced. All
  proposed thresholds are day-one baseline samples (G4), not global truth;
  operator approval decides what activates.
- 2026-07-07 · noticed during WP-03 · quiet-period semantics of the staging
  lag: max(ts_utc) over fact tables advances with market activity plus the
  hourly heartbeat scheduler, so an exchange-wide dead-quiet stretch could
  legitimately push the lag toward ~1h and trip the 600s default without any
  pipeline fault (never observed — live lag is ~48s — the firehose covers all
  markets). WP-08 should record the observed lag distribution for a week
  before anyone tightens/loosens the threshold. Owner: WP-08.
- 2026-07-07 · noticed during WP-03 · freshness distinguishes measured-stale
  from UNMEASURABLE (missing staging / lock held through the whole retry
  window / no raw files) — both exit 1 per fail-closed S2, but
  `stale_reasons` strings differ. If WP-08 ever pages differently for
  "pipeline behind" vs "cannot even read the pipeline", it should branch on
  that field, not on the exit code. Owner: WP-08.
- 2026-07-07 · noticed during W4 coverage auditor · `gold_validate`,
  `gold_build`, `gold_v5_delta`, `gold_v7_race` are registered kind=check/
  tool, safety=offline WITHOUT `autorun: false`, but all require `--date` —
  the console "Run all" set and `lifecycle_check --run-core-tests` would
  invoke them argument-less (argparse exit 2 → recorded fail). The
  `freshness`/`feed_readiness` entries already use `autorun: false` for
  exactly this; the gold entries should too. Owner: next tools.json change.
- 2026-07-07 · noticed during W4 coverage auditor · warehouse fact rows store
  PATH-SANITIZED category values (`Climate_and_Weather`,
  `Science_and_Technology`, `_unclassified` null sentinel) while
  config/market_classes.yaml and the live catalog use human names
  ("Climate and Weather"). W4's classifier is sanitize-aware
  (coverage_audit.classify_category), but any other consumer string-joining
  fact-row categories against the yaml/catalog will silently misclassify.
  Consider documenting in docs/warehouse_schema.md (or normalizing at
  ingest/export — needs its own D4-compliant change). Owner: next
  warehouse_schema/ingest change.
- 2026-07-07 · noticed during W4 coverage auditor · 43 traded markets on
  2026-07-06 carry NULL/`_unclassified` category (series missing from the
  classification dim at ingest time, e.g. KXMEDNOMJUL, KXBBCHARTTOP3,
  KXWCPREPACK) and 2 traded series are absent from the live catalog
  (KXMLBWINS, KXNEWOUTBREAK) — catalog/classification refresh lag. The daily
  coverage audit lists them (S1); a catalog_sync + build_classification rerun
  should absorb them. Owner: pipeline ops / WP-08 health.
- 2026-07-07 · noticed during WP-04 · PRE-EXISTING run_pipeline red (not
  introduced by WP-04; verified identical on clean HEAD f0f68b5 via stash):
  tests/test_console.py::test_api_init_returns_valid_json expects the
  lifecycle stage list WITHOUT "Coverage Audit (research)", but W4 (bf09122)
  appended that stage to tools/lifecycle_check.py without updating
  test_console in the same change (E1/D4 drift). `make check` is unaffected
  (test_console runs only in run_pipeline.sh); fix = add the stage to the
  expected list in tests/test_console.py. Out of WP-04 scope (test_console is
  not a WP-04 module; Operating Protocol rule 1). Owner: W4 rider / next
  console change.
- 2026-07-07 · noticed during WP-04 · settlements are NOT exported as facts:
  `tools/export_day.py` TABLES covers orderbooks_l1/orderbooks_full/trades
  only, while docs/warehouse_schema.md says "trades + settlements are recorded
  for EVERY market" and "Settlements partition by settlement date; capture ts
  kept as a column" (settlements currently exist only as a catalog/dim table
  from dim_snapshot/catalog_sync, not as a partitioned fact). The contract is
  pinned as an honest xfail in tests/test_pipeline_contract.py::
  test_settlement_partitioned_by_settled_date (strict=False) — when the
  exporter grows a settlements fact, extend that test: settlement captured on
  day D with settled_time D-1 must land under date=<D-1> with capture ts kept
  as a column. Building the feature was out of WP-04's test-conversion scope
  (Operating Protocol rule 1). Owner: R decision / export rider.
- 2026-07-07 · noticed during W4 coverage auditor · PLAN_GOLD_DATA_CONTRACT
  §0 grounding facts were measured EARLY on day one (5,692 traded / 13,455 L1
  markets); the full 2026-07-06 day is 127,997 traded / 44,569 L1 / 4
  full-depth. §0 is a snapshot, not a contract — the coverage audit is now
  the daily source for these numbers. Owner: documentation note only.
- 2026-07-07 · noticed during WP-08 · sequence-gap accounting is honestly
  "not instrumented" per day: work/live/ws_shadow.log stats lines carry
  cumulative counters (reconnects/errors/overflow/drop) with NO timestamps,
  and the richer feed-status JSON (apps/ws_shadow.cpp write_feed_status:
  ts_ms + gaps/resyncs + reconnects) goes to the metrics stream, not a
  daily-scannable file. daily_check tails the log and says so; if per-day
  gap attribution is wanted, teach ws_shadow to emit a timestamped counter
  line (e.g. at UTC rollover) or land feed-status snapshots somewhere
  date-addressable, then extend daily_check + tests (D4). Owner: R /
  ws_shadow rider.
- 2026-07-07 · noticed during WP-08 · policy gap for R: daily_check treats a
  NOT-BUILT gold day as report-only (gold is derived + rebuildable; archive
  checks own the loss alarm), and a built-but-unvalidated day as a WARNING.
  If the expectation becomes "every completed day has a GREEN gold build by
  morning", those should be promoted to hard failures — one-line change in
  tools/daily_check.py check_gold + test flip. Owner: R decision.
- 2026-07-07 · noticed during WP-09 · bracket dims are structurally dead:
  the catalog parquets carry NO floor_strike/cap_strike columns, so
  tools/dim_snapshot.py's event_structure derivation can never emit
  'bracket' (numeric-strike test always fails — day-07 latest snapshot has
  only binary/head_to_head/multi_outcome); additionally BOTH
  dim/latest/events.csv and markets.csv sit at exactly 80,000 rows (a
  fetch/row cap in catalog_sync, and the snapshot is today-only, so
  prior-day settled markets are absent — only 4 of 1,964 day-06 candidate
  events matched the markets dim at all). WP-09's gate_calc therefore runs
  its documented fallback (event_ticker grouping + mutually_exclusive from
  events.csv; ME-unknown excluded fail-closed — 874 events on day 06, 1,146
  on day 07). If bracket_rank/event_structure are wanted for real, teach
  catalog_sync to fetch strike fields + lift/paginate the 80k cap, then
  dims become the preferred source automatically (gate_calc already checks
  at runtime). Owner: next catalog_sync/dim_snapshot change.
- 2026-07-07 · noticed during WP-09 · exhaustiveness is unverifiable from
  our data: mutually_exclusive=true guarantees at most ONE bracket leg pays,
  not at least one, so buy-all-legs "arbs" with huge gross edges (up to 94c
  observed, mostly few-leg events priced near 0) are very likely
  non-exhaustive event fragments, not riskless profit. The gate report
  states signal counts are an UPPER BOUND. A settlement-based tightening —
  join evaluated events against settlements and keep only events where
  exactly one leg settled YES — would turn the upper bound into a measured
  arb rate; needs the settlements table + a day of settled brackets. Owner:
  R / gate-meeting interpretation, optional WP-09 rider.
- 2026-07-07 · noticed during W6 · the full-depth multiplier (orderbook_delta
  msgs per L1 ticker update) rests on n=3 markets, all in-game MLB during
  in-game peak (30–97×, PLAN_DEPTH_EXPANSION §2.2) — plausibly the top of
  the distribution; crypto 15-min / tennis / Fed multipliers are unknown.
  The operator-gated depth probe replaces this with a 50-market measurement.
  Owner: R (approve + run the probe, §5 runbook).
- 2026-07-07 · noticed during W6 · two exchange-side WS limits that bound any
  depth rollout are undocumented in the vendor snapshot: the per-subscription
  market cap (asyncapi error 26 exists, no number) and the per-account
  concurrent-connection cap (no mention at all). The probe empirically tests
  50-in-one-subscribe and 2 concurrent connections; anything larger needs its
  own discovery. Consider asking Kalshi support for the numbers. Owner: R.
- 2026-07-07 · noticed during W6 · at N≥200 depth markets the conservative
  capture projection is 27–52.5 GB/day against raw_retention_days=3 — the
  already-noted gzip-on-rotate work item becomes a rollout PREREQUISITE, and
  ingest has never been load-tested at ~1.6–3k rows/s bursts (the probe
  capture doubles as a replay fixture for exactly that test). Owner: the
  future depth-rollout plan.
- 2026-07-07 · noticed during W6 · depth_target_*.csv is a daily artifact
  whose top ranks are dominated by short-lived markets (15-min BTC, same-day
  matches) — any depth subscription set goes stale within hours, so a rollout
  needs a rotation story (restart at day boundary vs update_subscription
  add/delete_markets, PLAN_DEPTH_EXPANSION §4 cross-cutting note); the probe
  tool warns when the list is not dated today. Owner: R at rollout decision.
- 2026-07-07 · noticed during STEP 0 independent audit · the reconnect re-sign
  (ws_client.cpp refresh_auth, commit 6d56aae) signs at the moment a handshake
  Error fires, then ixwebsocket sleeps the backoff (capped 30s,
  ix_transport.cpp:40) BEFORE the next connect() — so on a PERSISTENT
  handshake-failure streak the presented signature can be up to ~30s stale.
  The common transient-drop path reconnects with backoff=0 (fresh) and is
  fully fixed; this residual only bites if (a) failures persist to the 30s cap
  AND (b) Kalshi's WS auth-timestamp acceptance window is < 30s — and that
  window is UNDOCUMENTED (kalshi_facts.yaml has only local clock_skew 1138ms /
  preflight +/-2000ms, not the server-side signature window). Two follow-ups:
  (1) discovery — pin Kalshi's WS auth timestamp tolerance (protocol doc says
  "on 401 surface server-date skew"); (2) if it proves < 30s, sign closer to
  connect (lower maxWait, or a pre-connect header hook / client-driven
  reconnect ladder that re-signs per attempt). Owner: next WS-hardening pass.
- 2026-07-07 · measured during STEP 0 deploy · CAPTURE CONTINUITY: the hourly
  ws_shadow restart itself is a NON-issue — measured handoff gap is ~0.5s
  across clean UTC boundaries (00->01 0.46s, 04->05 0.94s, 12->13 0.54s). The
  real continuity holes are (a) the 401 lockout — ~30s captured then dark for
  the rest of the hour on affected hours 06-11 (2900-3800s gaps), now FIXED by
  STEP 0; and (b) SYNCHRONOUS export pausing capture: export_day.py runs inside
  the supervisor's capture loop so ws_shadow does not respawn until it finishes
  — measured ~497s (~8min) gap at 02:00 UTC (second-pass --force sweep) and
  ~78s at 00:00 UTC (daily export). For live sports capture (b) is the residual
  硬伤. Fix direction: decouple Layer-3 export from the Layer-1 capture loop
  (background worker / separate process against staging, single-writer DuckDB
  respected), or at minimum respawn ws_shadow BEFORE the export runs. Owner:
  a capture-continuity W (fold into AWS migration STEP 1, or its own plan).
- 2026-07-07 · W-E0 independent audit (deferred, non-blocking) · (a) event_
  measure_split.collect() — the warehouse SQL path (ts_utc//US_PER_DAY GROUP BY,
  any_value, column list, archive/staging double-count guard) is validated only
  on real data, not unit-tested; the pure event_spans core is. A fixture-backed
  test driving collect() against a tiny synthetic warehouse (load() accepts a
  warehouse= arg) would close it. Fold into W-E1 (same load() path, gets tested).
  (b) PRE-EXISTING shared-loader property: tools/warehouse.load() gates staging
  reads on the GLOBAL max archived date across all categories; if categories were
  ever archived non-uniformly (one lagging), the lagging category's newest day
  could be excluded from staging while absent from its own archive → silent
  undercount. Not occurring now (uniform archive to 2026-07-06), but it bounds
  the trust of any load()-based figure (gate_calc/coverage_audit too). Owner:
  warehouse-loader hardening pass.
- 2026-07-07 · AF-2 deferral · event_measure_split measures the split on TRADES
  only; MM maker edge lives on the L1 QUOTE window, which is wider than the
  trade window (quotes stand before/after prints). So the trades-based
  cross-midnight fraction (40.7% events / 76.3% rows / 82.1% contracts /
  78.2% notional, Sports 7d) likely UNDERSTATES the true cross-midnight fraction
  for market-making. An L1-quote-window split is a separate measure — fold into
  the W-E1 index (which already carries per-event L1 observed bounds). Owner:
  W-E1 enhancement / pricing-model calibration input.
- 2026-07-07 · W-E3 audit residual · event_validate V-EP15's gap source
  (gaps_from_quality_log) is a BEST-EFFORT free-text parser (minute-resolution,
  same-day windows, keyword-matched). It fails CLOSED (missing/empty record ->
  V-EP15 skip -> verdict degraded, never pass), so it cannot green-lie, but it
  can MISS a real gap that quality_log recorded in a shape it doesn't parse
  (=> a hole slips as pass only if the record also lacks other signals). Build a
  DURABLE structured capture-gap record (start_us,end_us) from metrics freshness
  + raw-feed inter-record gaps + quality_log data_loss (PLAN §4 capture_gaps),
  and make it V-EP15's authoritative source. Owner: a gap-record-builder W
  (pairs with W-E1 §4). Until then --gaps <csv> can supply it explicitly.
- 2026-07-07 · W-E1 real-dim wiring · event_index --from-warehouse works on the
  live warehouse (built 225,936 units, 32,336 cross-midnight, 182,360 Q7-excluded
  MVE), BUT dim/latest/markets.csv holds only CURRENT markets, so settled/rotated
  events lose their catalog open/close -> catalog_incomplete=true + observed_merged
  windows (fail-closed, flagged). For accurate windows on SETTLED games, also join
  work/warehouse/catalog/settlements/*.parquet (has settlement_ts, latest_expiration
  _time per settled market) — the "settlements-first" idea. Owner: W-E1.1 / STEP 5.
- 2026-07-07 · W-E1/E2/E7 vs live ingest · research read tools (event_pack/export/
  validate -> wh.load) fail with a DuckDB "Conflicting lock" IOException when the
  live ingest daemon (PID) holds the staging.duckdb write lock during a long write
  window; load()'s ATTACH retry is only 8x1.5s=12s. Affects ALL warehouse readers
  (gate_calc/coverage_audit/mm_scan too), not just event-packaging (D6 property).
  Fix options: lengthen/backoff the staging-attach retry; OR read archive + a
  staging read-replica snapshot; OR run research reads when ingest is paused. Owner:
  warehouse-loader hardening.
- 2026-07-07 · W-C1 (capture hardening) · operator requirement: for LIVE
  short-term trading the market-data feed must recover in **≤ 1 second** with
  effectively ZERO perceptible gap. W-C1's watchdog force-reconnect gets the
  CAPTURE (backtest-data) feed from up-to-60-min holes down to ~20-25s recovery
  (20s silence detection + ~1-3s reopen) — sufficient for complete backtest
  data, but NOT for a 1s-seamless execution feed. A single socket can never hit
  1s reliably: detecting a wedged (half-open) socket needs >~0.5-1s of missed
  heartbeats, and a fresh Kalshi handshake+auth+resubscribe+first-frame is
  typically 1-3s. DECISION: sub-second continuity is achieved by REDUNDANCY, not
  faster single-socket reconnect — run two independent WS connections (primary +
  hot standby, both live), so a wedge on one causes ZERO gap because the other is
  already delivering; the dead one reconnects in the background. This belongs to
  the **Phase-2 EXECUTION feed** (World A/B merge, Q5: WS full-depth book drives
  execution) — NOT the Phase-1 capture path. Design notes for that W: dual
  ws_client instances with de-dup by (sid,seq)/epoch at the book layer; aggressive
  sub-second liveness (enable our own ping cadence on the execution transport, or
  a per-connection watchdog << 1s); failover is instant (consume whichever feed is
  fresher), reconnect is lazy. Owner: Phase-2 execution-feed W (new plan; gate:
  Q5 WS-drives-execution). Capture (W-C1..C4) stays single-socket + watchdog.
- 2026-07-08 · W-C2 (discovered building capture_gaps) · raw firehose ROTATION
  can leave TIME-OVERLAPPING segments: 2026-07-08 hour-00 had firehose_00.ndjson
  (511MB, spanning 00:10-00:32) AND firehose_00.ndjson.1 (264MB, spanning
  00:17-00:33) — overlapping ~15 min, and file0 is 2x the ~256MB rotation cap.
  capture_gaps global-sort proves NO data LOSS there (0 real holes), but the
  overlap implies records may be DUPLICATED across segments (record count 1.23M
  for the hour looks inflated). NOT loss, so W-C1/W-C2 unaffected, but for
  "bulletproof": investigate (1) why a within-hour respawn/rotation produces
  overlapping + oversized segments, (2) whether ingest DEDUPES the duplicated
  records (staging is keyed by (sid,seq)/epoch — likely yes, verify), (3) whether
  the 256MB cap is being enforced. Owner: capture-rotation review (read-only
  first: diff record sets of the two overlapping segments).
- 2026-07-08 · W-C2 · the live capture-gap ALERT (capture_gaps.py --live ->
  work/live/capture_alert.json, exit 2 on active gap) is BUILT + tested but not
  yet WIRED to run automatically. Options: add to pipeline_supervisor.sh's 60s
  watchdog loop (it already revives ingest — a natural home) OR a launchd/cron;
  plus an operator-chosen push (email/webhook) on status=gap. Needs operator
  decision on the push channel + touches the supervisor (production), so left as
  a follow-up W (W-C2.1). Until then run --live manually or via cron. Owner: R
  (channel choice) + a supervisor-wiring W.
- 2026-07-08 · W-C3 acceptance (found verifying the deploy) · SECOND capture-gap
  mechanism, distinct from the wedge W-C1 fixes: ws_shadow EXITS NON-ZERO at some
  hour boundaries and the supervisor retries every 15s (~15x = ~3-4min gap).
  Confirmed post-W-C1: 2026-07-08 02:00:00->02:02:59Z (179s) on the W-C1 binary,
  forced=0 (NOT a wedge). supervisor.out.log shows clustered "ws_shadow exited
  non-zero (will retry in 15s)". Likely cause: at :00 the supervisor runs catalog
  classify (11240 series) + daily/second-pass export + holds the DuckDB lock, and
  ws_shadow's relaunch fails transiently (crash? connect/auth on a contended
  boundary? OOM?). W-C1 does NOT address this — it recovers mid-session wedges,
  not a process that exits and slow-retries. So W-C3's 24h-zero-gap acceptance is
  NOT met yet; capture_gaps DOES record these (events over them degrade
  correctly, D2 holds). Fix ideas: shrink the 15s retry to ~1-2s; find why the
  relaunch exits non-zero (add exit-reason logging); stagger catalog/export off
  the ws_shadow relaunch instant. Owner: W-C5 diagnosis (read-only first).
- 2026-07-08 · W-C5 ROOT CAUSE PROVEN (operator full-repo scan + code trace) ·
  The hourly gaps are NOT just "non-zero-exit at the boundary" — a 62-gap scan
  (criterion: `subscribed` record within 60s of gap-end) splits them into 58
  RECONNECT-RECOVERY (connection died → Open → resubscribe) and 4 SAME-CONNECTION
  DATA-DROPOUT (data resumed on a LIVE connection, no reconnect): 07-06 12:26 &
  14:48 (~62s each, precede larger wedges — periodicity suspect), 07-07
  17:29→17:51 (22.7min, key sample), 07-07 23:55 (4.3min). PROVEN: W-C1's
  any-frame watchdog (last_activity_ms_ bumped by ping/pong, ws_client.cpp:114)
  is blind to data holes on a live connection — it fires at ~901s (whole
  connection dies, pings stop), not 20s, so the 58 are ~15-min holes not ~20s.
  FIX (input pinned): a DATA-frame-silence trigger (last_data_ms_ on Text only),
  two-stage — re-subscribe on the live socket first (fixes the 4), escalate to
  force_reconnect if no data (fixes the 58). Handles both classes; red-first test
  injects ping-only frames. FULL WRITEUP + autopsy TODOs:
  docs/plan_audits/capture_gap_taxonomy_2026-07-08.md. Owner: W-C5 session
  (production code, full discipline).
