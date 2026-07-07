# BACKLOG — out-of-scope observations (never fixed "while I'm here")

Per EXECUTION_PLAN operating protocol rule 1: anything noticed outside the
current WP's scope lands here as a note, never as code. Each entry: date,
noticed-during, observation, suggested owner.

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
