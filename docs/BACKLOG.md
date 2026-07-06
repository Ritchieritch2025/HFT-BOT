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
