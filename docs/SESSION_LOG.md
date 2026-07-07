# SESSION LOG — newest entry first

Every session appends one entry before ending (see CLAUDE.md "Session exit
ritual"). This file is the cross-session memory: where work actually stopped,
which decisions landed in which files, what the next session must know.

---

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
