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
