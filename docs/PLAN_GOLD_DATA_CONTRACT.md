# PLAN — Gold Standard Data Contract + Coverage Audit

**For the executing agent: read `docs/GUARDRAILS.md` and `docs/MM_ROADMAP.md`
before touching anything. This plan binds to them; a conflict means STOP and
ask the operator.**

- **Phase advanced:** 1.5 (dynamic pricing model — data substrate). Satisfies
  the 1.5-C prerequisite: a bias-free, join-correct event stream for
  calibration/backtests.
- **Gates:** P1 (phase named), P2 (no live-trading reach), P3 (bounded scope,
  demonstrated acceptance, rollback below), P4 (pipeline continuity stated per
  workstream), E1/E2/E5 (tests + docs in the same change).
- **Safety class of everything in this plan:** read-only research + one
  additive exporter column. Nothing touches order paths. No live_order tools.

---

## 0. Grounding facts (measured 2026-07-06, day one of capture)

| Fact | Value |
|---|---|
| Distinct markets with L1 rows | 13,455 |
| Distinct markets traded | 5,692 |
| Distinct markets with full depth | **4** (watchlist leftovers) |
| Traded markets with no L1 | 4,332 — 4,134 Exotics/MVE (Class B by design, Q7-excluded from MM) + ~200 Class B (Elections/Entertainment/Mentions/etc.) |
| Sports subcategories traded | 13, all present in L1 (Sports is Class A); only 2 sports markets missed L1 |
| Timestamp key | `ts_utc` epoch **microseconds** in all warehouse tables (the 1-s `time_utc` is a human-readable sibling) |
| Known ordering defect | `orderbooks_full` export has **no seq column**; same-µs deltas ordered only by file position. Trades export is not time-sorted. |
| Unit inconsistency | L1/trades CSV exports are dollar-decimal strings; full book is E4 ints. Gold layer is E4 everywhere (D5). |

Class policy source: `config/market_classes.yaml` (Class A = L1+trades,
Class B = trades only). The policy is category-driven; the operator
requirement is **liquidity-driven** ("all high-to-mid liquidity markets, all
sports"). This plan makes the disagreement visible daily (W4) and reports
promotion candidates; it does NOT auto-edit the class policy.

## 1. Scope

**In:** GoldRecord format + builder + validator (contract below); Kalshi API
compliance gates; daily coverage auditor; exporter seq column; depth-expansion
*design + probe* (not rollout).

**Out (each needs its own plan):** production firehose subscription changes,
class-policy promotions, the pricing model itself, anything World A/B merge.

## 2. The contract (approved by operator, 2026-07-06)

### 2.1 GoldRecord — 512 B, zero-copy, one record per event

```cpp
// include/trading/gold_record.hpp  (new; source-agnostic layer — no kalshi types)
constexpr int kDepth = 16;

enum EventType : uint16_t { BOOK_SNAPSHOT=1, BOOK_DELTA=2, TRADE=3, L1_TICKER=4, HEARTBEAT=5 };
enum Flags : uint16_t {
  F_BOOK_VALID=1<<0, F_BOOK_COVERED=1<<1, F_CROSSED=1<<2,
  F_FROM_SNAPSHOT=1<<3, F_TRADE_RACED=1<<4,
};

struct GoldRecord {
  // ordering / identity (24 B)
  uint64_t ts_us;                // capture epoch µs — the only clock
  uint64_t stream_seq;           // global monotonic merge order (total-order tie-break)
  uint32_t market_id;            // dense per-day id -> sidecar dim (ticker, category, close_time).
                                 // DAY-SCOPED: meaningful only within one date partition.
                                 // Cross-day joins on market_id alone are FORBIDDEN;
                                 // cross-day access requires (date, market_id) or market_ticker.
  uint16_t event_type;
  uint16_t flags;
  // join key (8 B)
  uint64_t book_seq;             // per-market monotonic book version this record sees.
                                 // JOIN KEY = (market_id, book_seq). Never join on time.
  // trade payload, zero unless TRADE (24 B)
  int32_t  trade_yes_price_e4;
  uint8_t  taker_side;           // 0=none 1=yes 2=no
  uint8_t  _pad[3];
  int64_t  trade_qty_e4;
  uint64_t trade_id_hash;        // FNV-1a-64(trade_id) — G6 adjudicated: stdlib-free, 5 lines
                                 // in both languages; V8 collision-reconciles every build;
                                 // full UUID always preserved in sidecar keyed by stream_seq
  // book state as-of this record, yes-space, best-first (416 B)
  int32_t  bid_px_e4[kDepth];    // from yes_levels (Yes bids)
  int32_t  ask_px_e4[kDepth];    // = 10000 - no_price, from no_levels (No bids)
  int64_t  bid_qty_e4[kDepth];
  int64_t  ask_qty_e4[kDepth];
  int64_t  bid_rest_qty_e4, ask_rest_qty_e4;  // tail beyond kDepth — aggregated, never dropped silently
  uint16_t bid_nlevels, ask_nlevels;
  uint8_t  _pad2[12];
  uint64_t _reserved[5];         // pad to 512 (40 B; G3 fix: 472+40=512, 8-aligned)
};
static_assert(sizeof(GoldRecord) == 512);
// Section arithmetic (audited 2026-07-06, G3): 24+8+24+416+40 = 512.
```

Python mirror: a numpy structured dtype with identical offsets. Layout parity
is test-enforced (V11): a tiny C++ test target dumps `{field: offset}` JSON;
the Python test compares against the dtype. Little-endian asserted both sides.

Rules: E4 integers everywhere; prices parsed by integer digit accumulation
(no float parse anywhere in the loader, D5). Uncovered books ⇒ arrays zeroed +
`F_BOOK_COVERED=0`; L1-only markets get top-of-book in slot 0 with the same
flag. Invalid books ⇒ arrays zeroed + `F_BOOK_VALID=0` (never stale data).

### 2.2 Synchronization (build-time, single cursor)

1. Normalize each source to typed events THROUGH the dynamic validation
   gates below. Trades: stable-sort `(ts_us, trade_id)`, dedupe on
   `trade_id`. The gates run identically on ANY date and any replayed or
   future stream. Historically known-bad windows (e.g. the 2026-07-06
   08:18–08:35 UTC splice incident) remain documented audit facts, but
   **the calendar is never the validation mechanism** — malformed input is
   detected by inspecting the record, not by date arithmetic. ALL data on
   ALL dates is processed; nothing is date-filtered.

   **Loader validation gates (W2.1; every gate detected AND reported):**
   malformed JSON / corrupt NDJSON line; invalid or missing market_ticker /
   event_ticker / series_ticker; timestamp outside plausible range;
   non-monotonic or suspicious timestamp clusters (reported, never "fixed");
   price/qty accounting columns arriving as float dtype; price outside the
   valid E4 range; negative or impossible quantities; malformed orderbook
   level arrays; duplicate trade_id; inconsistent taker_side values;
   missing required source fields; rows failing schema expectations.

   **Loader outputs (operator-facing):** malformed_record_count;
   malformed_record_sample CSV; rejected_record_reason counts; per-source
   validation summary; quarantine file for rejected rows (original bytes
   preserved — quarantine is "stored elsewhere", never "deleted"); loader
   report for operator inspection.

   **No silent repair (binding):** allowed actions — reject row, quarantine
   row, mark record invalid, emit report, continue processing valid rows
   when safe. FORBIDDEN — silently coercing float→E4; clamping invalid
   prices; fabricating missing book state; inner-joining away uncovered
   markets; hiding malformed rows from reports.
2. Per-market book FSM mirroring `include/kalshi/orderbook.hpp` semantics,
   robust to stream defects on any date: snapshot ⇒ load+valid; valid delta
   mutates; delta driving any level negative ⇒ INVALID (no clamp); missing
   or impossible delta ⇒ INVALID; crossed book ⇒ F_CROSSED flagged, never
   repaired; heartbeats never mutate book state or book_seq. Sequence gap
   (when seq exists) ⇒ INVALID + **Resync Required** marker. An invalid
   book stays invalid until a LATER SNAPSHOT in the replay revalidates it;
   if no later snapshot exists, every subsequent record for that market
   stays F_BOOK_VALID=0.
   **"Self-healing" means exactly deterministic replay recovery from the
   next valid snapshot. It does NOT mean silent repair, does NOT mean
   production re-subscription, does NOT change ws_shadow/firehose. Any
   real-time subscription-resync mechanism belongs to a separate future
   capture/execution plan.**

   **Sequence-gap modes (W2.2 must NOT depend on W5):**
   - Pre-W5 (no seq/sid columns exist): no true gap detection; file order
     governs; every report states `seq_unavailable`.
   - Post-W5: per-sid/per-market gap detection; gap ⇒ invalid-until-snapshot
     as above.
3. Global k-way merge on `(ts_us, type_priority, source_file_order)` with
   **TRADE < BOOK_DELTA at equal ts_us** (the same-µs delta is usually the
   decrement caused by the trade; the trade must see the pre-trade book).
4. Emit one GoldRecord per event with the FSM state at merge position.
   Look-ahead is structurally impossible: one forward cursor, no timestamp
   lookups, trades reference only already-emitted `book_seq`.
5. Channel races (trade prints outside as-of touch) are flagged
   `F_TRADE_RACED` and counted — published metric, never repaired (D2).
   **Policy: day one is REPORT-ONLY.** No hardcoded blocking threshold
   (0.1% or otherwise). Race rate is reported by category, subcategory,
   market, market class (A/B), and liquidity tier. After operator approval,
   category-specific thresholds MAY become blocking. A high race rate marks
   the affected slice `unsafe_for_microstructure` in the manifest (fill
   simulation / queue studies must refuse it) — it does NOT quarantine the
   day for other research uses (spread/vol calibration remains valid).

Output: `work/gold/date=<D>/gold_<D>.bin` + sidecars
(`markets_<D>.csv` dim with a `date` column + ticker/category/close_time +
liquidity tier; `trade_ids_<D>.csv` stream_seq→UUID) + `manifest_<D>.json`
(record count, gold file md5, **md5 of every sidecar**, builder version,
source-day identifiers, and the day-level safety verdicts from V5/V7).
Derived data: rebuildable from the warehouse, safe to delete, excluded from
raw-retention rules. Local retention (G8): keep the most recent 14 days of
`work/gold/` (`GOLD_RETENTION_DAYS`, operator-tunable); older partitions are
deleted and rebuilt on demand via `gold_build --date`. GOLD_RETENTION_DAYS
applies ONLY to derived `work/gold/date=<D>/` partitions — it NEVER prunes
raw capture, archive facts, manifests, catalog dims, classification dims,
registry files, or operator reports (R decision 2026-07-06).

### 2.3 Validator — build fails or the day is quarantined unless ALL pass

| # | Test | Pass criterion |
|---|---|---|
| V1 | Lossless E4 round-trip | every price/qty string re-renders byte-exact from the parsed integer; zero float parses in loader code (grep-gated) |
| V2 | Manifest reconciliation | parsed row counts == archive manifest counts; md5 match |
| V3 | Total order | `stream_seq` dense; `(ts_us, stream_seq)` non-decreasing; per-market `book_seq` strictly increasing, gap-free |
| V4 | Book integrity | zero negative levels emitted; negative/missing/impossible delta ⇒ invalid-until-snapshot (count reported); seq gap (when seq exists) ⇒ invalid + Resync Required; revalidation from a later snapshot PROVEN; crossed books flagged not repaired |
| V5 | L1 cross-check | covered markets: reconstructed top-of-book equals L1 change rows within measured channel-race window δ. δ is REPORTED AS A DISTRIBUTION — p50/p90/p99/max delta_ms + mismatch counts, broken down by market_id, market_ticker, category, subcategory, and liquidity tier. **Widening δ to absorb mismatches is forbidden**: markets whose mismatch rate stays high at the global p99 δ go on a surfaced bad-markets list, not into a looser window. 1 µs equality across independent WS channels is physically meaningless — δ is measured, published, per-slice. Report MUST print support size: n_markets, capture_hours, n_l1_rows, n_full_depth_rows, n_matched_pairs; day-one δ is labeled a BASELINE SAMPLE, not global truth (R decision 2026-07-06) |
| V6 | No look-ahead | every TRADE: `ts(book_seq) ≤ ts_us(trade)` AND merge-position(book) < merge-position(trade), asserted structurally on the emitted stream |
| V7 | Economic consistency | trades printing at as-of best (taker=yes ⇒ ask, taker=no ⇒ bid) measured and REPORTED per category, subcategory, market, market class, and liquidity tier. **Day one: report-only — no blocking threshold.** Category-specific thresholds activate only after operator approval of the day-one report; until then V7 may mark slices `unsafe_for_microstructure` but cannot fail the build |
| V8 | Trade dedupe | trade_id unique; zero FNV-1a-64 collisions vs sidecar (reconciled every build) |
| V9 | Heartbeat neutrality | hourly heartbeats advance no book_seq, diff no state |
| V10 | Coverage honesty | traded-but-uncovered market count reported; all such records `F_BOOK_COVERED=0`; no silent inner-join shrinkage |
| V11 | Layout parity | C++ static_asserts (size 512 + offsets) and Python dtype match the dumped layout JSON; mmap random access == streamed parse on 1,000 sampled records |
| V12 | Spec-drift gate | `kalshi_spec_sync` green AND its saved result ≤ 7 days old (staler ⇒ gate fails, rerun sync first) before any gold build; drift in ticker/trade/orderbook_delta schemas blocks the day |
| V13 | Golden-frame semantics | field semantics verified against real frames sampled from `work/raw/`: per-sid seq scoping, trade_id dedupe key, taker_side lift direction (empirical via V7), fixed-point strings |
| V14 | Liquidity coverage | 100% of High+Mid tier markets have L1 coverage; violations listed with category + class |
| V15 | Depth-set stability | full-depth market set matches the declared subscription list; shrinkage is an error not a warning |
| V16 | Class-policy safety net | new/unlisted Kalshi category ⇒ Class B default + surfaced warning (matches build_classification behavior) |
| V17 | Input integrity gates | all twelve W2.1 gate classes detect, quarantine, and report on any date; quarantine file + loader report produced; zero silent repairs (each gate has a seeded-defect fixture proving it turns red) |

## 3. Workstreams (execute in order; one W = one commit; a W does not start
## until the previous one is green)

**Anti-fake-green rule (binding on every W):** every validator check and every
FSM/merge behavior ships with at least one seeded-defect fixture that makes it
FAIL (in `tests/fixtures/gold_defects/`, run in CI as must-fail assertions).
A check that has never been red is unproven (D2).
**Size discipline:** one component per W, ≤ ~300 new lines excluding tests,
pure functions before I/O, no forward references.
**Global forbidden writes (every W, no exceptions unless its own Allowed
writes says otherwise):** `tools/ingest.py`, `tools/export_day.py`,
`tools/pipeline_supervisor.sh`, `apps/ws_shadow.cpp`, `config/*`,
`work/raw/*`, `work/warehouse/*` (read-only via load()), `dashboard_server.py`,
anything live_order-classed.
**GoldRecord layout freeze (after W1):** no workstream may add, remove, or
move struct fields. All new metadata goes to sidecars, manifests, reports,
or preview CSVs — NEVER into the struct. Anything that would touch the 512-B
layout, static_asserts, offset JSON, numpy dtype parity, endianness or
alignment assumptions ⇒ STOP and obtain operator approval (C++/Python parity
break risk).
**Required seeded-defect fixtures (minimum set, all must-fail in CI):**
malformed JSON line; float-dtype price column; out-of-range price; negative
quantity; duplicate trade_id; bad timestamp; malformed level array; negative
book delta; crossed book; sequence gap (seq available); missing snapshot
after invalidation; silent-clamp bug; stale-state-after-invalid bug.
**Operator-facing report vocabulary (use verbatim in reports):** Malformed
Records · Rejected Rows · Quarantined Input Rows · Invalid Book State ·
Resync Required · Sequence Gap · Unsafe for Microstructure Backtest.

### W1 — Contract in code
Purpose:          land the GoldRecord layout contract (C++/Python parity). Nothing else.
Blocked by:       EXECUTION_PLAN WP-00 (pytest scaffold + make test) — G1. The A1
                  single-test-infrastructure rule binds the gold suite.
Allowed reads:    this plan; include/trading/*; Makefile; tools.json; tests/run_pipeline.sh
Allowed writes:   include/trading/gold_record.hpp; tests/test_gold_layout.cpp;
                  tools/gold_dtype.py; tests/test_gold_dtype.py;
                  Makefile / tools.json / tests/run_pipeline.sh (append-only entries);
                  .gitignore (append `!tests/fixtures/**` negation — G2: global
                  *.ndjson/*.csv.gz ignores would silently swallow all fixtures)
Forbidden writes: everything else. NO benchmarks, NO validator logic, NO real-day builds.
Acceptance:       make check green incl. layout test; C++ layout JSON == numpy dtype
                  offsets; sizeof==512 + little-endian asserts; check_registry passes.
                  Suite asserts every fixture file referenced by tests is git-tracked
                  (fresh-clone safety, G2).
Rollback:         revert commit (additive files + appended lines only).
Exit evidence:    commit hash; make check tail; committed layout JSON path.

### W2.1 — Typed loaders
Purpose:          l1/full/trades → typed events THROUGH the §2.2 dynamic validation
                  gates; E4 integer-only parse; trade sort + dedupe; quarantine +
                  loader report. Works on ANY date — no hard-coded bad-window logic.
Allowed reads:    warehouse via tools/warehouse.py load() (read-only); real 2026-07-06 rows.
Allowed writes:   tools/gold_load.py; tests/test_gold_load.py;
                  tests/fixtures/gold_golden_rows/*; tests/fixtures/gold_defects/*;
                  work/gold/quarantine/*; work/gold/loader_report_*;
                  malformed_record_sample CSVs
Forbidden writes: warehouse.py itself; any data under work/ EXCEPT work/gold/**;
                  GoldRecord layout (frozen after W1 — STOP + operator approval).
Acceptance:       byte-exact E4 round-trip on golden rows (V1); dtype anti-float test
                  passes; dynamic validation report produced; malformed rows
                  quarantined WITH reasons; all W2.1 seeded-defect fixtures (malformed
                  JSON, float dtype, out-of-range price, negative qty, duplicate
                  trade_id, bad timestamp, malformed level array) FAIL when injected;
                  no production capture files modified.
Rollback:         revert commit.
Exit evidence:    commit hash; pytest output showing green suite + must-fail proof.

### W2.2 — Book FSM
Purpose:          pure book state machine (snapshot/delta/invalid/crossed/seq-gap/
                  heartbeat) with deterministic replay recovery, zero I/O. Recovery =
                  revalidate from next valid snapshot; never silent repair, never
                  production resync.
Allowed reads:    include/kalshi/orderbook.hpp (semantics reference); this plan §2.2.
Allowed writes:   tools/gold_fsm.py; tests/test_gold_fsm.py; gold_defects fixtures.
Forbidden writes: everything else; no network, no files read at runtime; GoldRecord
                  layout (frozen after W1 — STOP + operator approval);
                  ingest/export/ws/live-order/strategy code.
Acceptance:       snapshot/delta/heartbeat/invalid/crossed/seq-gap cases all handled;
                  invalid-until-snapshot PROVEN; revalidation from a later snapshot
                  PROVEN; no-later-snapshot ⇒ permanent F_BOOK_VALID=0 PROVEN; both
                  seq modes work (pre-W5 `seq_unavailable`, post-W5 gap detection)
                  with no dependency on W5; yes-space transform (10000−no_price).
                  Red fixtures: clamp bug, stale-state-after-invalid, negative delta,
                  crossed book, seq gap, missing-snapshot-after-invalidation — ALL
                  FAIL when injected. No production capture behavior changed.
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green + red-fixture proof.

### W2.3 — Merge iterator
Purpose:          total order (ts_us, TRADE<BOOK_DELTA, file_order); stream_seq/book_seq
                  minting; trade→book_seq assignment. Pure logic on synthetic lists.
Allowed reads:    this plan §2.2.
Allowed writes:   tools/gold_merge.py; tests/test_gold_merge.py; gold_defects fixtures.
Forbidden writes: everything else.
Acceptance:       structural V6 on synthetic streams. Red fixtures: same-µs
                  delta-before-trade bug, book_seq gap — both FAIL.
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green + red-fixture proof.

### W2.4 — Gold writer/reader
Purpose:          records → .bin + sidecars + manifest; mmap reader; market_id stability.
Allowed reads:    outputs of W2.1–W2.3 modules.
Allowed writes:   tools/gold_io.py; tests/test_gold_io.py; gold_defects fixtures;
                  work/gold/* (derived data only).
Forbidden writes: everything else.
Acceptance:       write→mmap→compare on 10k synthetic records (V11 runtime half);
                  same-day 1:1 market_id↔ticker asserted (violation = build failure);
                  sidecar has date column; manifest has sidecar md5s; reader refuses
                  market_id-only cross-day access. Red fixtures: duplicate-ticker-two-ids,
                  one-id-two-tickers, sidecar-md5 mismatch — all three FAIL.
Rollback:         revert commit; delete work/gold/ (derived, rebuildable).
Exit evidence:    commit hash; pytest green + 3 red-fixture proofs.

### W2.5 — Validator harness
Purpose:          gold_validate.py running V2,V3,V4,V6,V9,V10 independently + quarantine.
Allowed reads:    gold files from W2.4.
Allowed writes:   tools/gold_validate.py; tests/test_gold_validate.py;
                  gold_defects/* (one seeded-defect gold file PER CHECK); work/gold/*.
Forbidden writes: everything else.
Acceptance:       all checks green on synthetic good file; EACH check red on its own
                  defect file; failed day → quarantine dir + nonzero exit.
Rollback:         revert commit.
Exit evidence:    commit hash; per-check green/red matrix printed.

### W2.6 — First real build
Purpose:          gold_build.py = thin composition of W2.1–W2.5. NO new logic.
Allowed reads:    warehouse (read-only).
Allowed writes:   tools/gold_build.py; work/gold/date=2026-07-06/*; validation report.
Forbidden writes: everything else; zero new business logic (composition only).
Acceptance:       `python3 tools/gold_build.py --date 2026-07-06` builds + validates.
Rollback:         revert commit; delete work/gold/.
Exit evidence:    commit hash; report path; validator summary printed.

### W-BENCH — volume benchmark (opt-in; NEVER in make check / CI / W1)
Purpose:          catch memory blowups before multi-week datasets exist.
Allowed reads:    none required (synthetic generation).
Allowed writes:   tools/gold_bench.py; work/gold/bench_report_*.
Forbidden writes: everything else; no CI wiring, no Makefile default targets.
Acceptance:       BENCH_LARGE=1 run on 10M + 20M synthetic events reports rows/sec,
                  GB/sec, peak RSS, file size, mmap/lazy vs full-load. Never gates builds.
Rollback:         revert commit.
Exit evidence:    commit hash; bench report path (when run).

### W3.1 — δ distribution (V5)
Purpose:          measure L1↔book agreement window δ as a distribution, not a scalar.
Allowed reads:    gold file + L1 (read-only).
Allowed writes:   validator/report modules; tests; gold_defects fixture; work/gold/*.
Forbidden writes: everything else.
Acceptance:       report has p50/p90/p99/max delta_ms + mismatch counts by market_id,
                  ticker, category, subcategory, liquidity tier; bad-markets list
                  surfaced (widening δ to absorb mismatches FORBIDDEN).
                  Report MUST print support size (n markets, capture hours) — day one
                  is 4 full-depth markets × ~4h and MUST NOT be read as global truth (G4).
                  Red fixture: shifted-book file breaks agreement.
Rollback:         revert commit.
Exit evidence:    commit hash; report path with δ table.

### W3.2 — Race/consistency report (V7, REPORT-ONLY)
Purpose:          day-one economic-consistency measurement. No blocking thresholds.
Allowed reads:    gold file (read-only).
Allowed writes:   report modules; tests; gold_defects fixture; work/gold/*.
Forbidden writes: everything else; NO threshold enforcement code paths.
Acceptance:       race rate per category/subcategory/market/class/tier; proposed
                  thresholds listed FOR OPERATOR APPROVAL only;
                  unsafe_for_microstructure marking works. Red fixture: inverted
                  taker_side fails the measurement (not a threshold).
Rollback:         revert commit.
Exit evidence:    commit hash; report path; proposed-threshold table.

### W3.3 — Golden frames (V13)
Purpose:          pin Kalshi field semantics against real captured frames.
Allowed reads:    work/raw/ (read-only sampling). Sampled frames MUST pass the §2.2
                  dynamic validation gates before becoming fixtures — validation is
                  the gate, NOT the calendar; no date-based exclusion anywhere.
                  (Known-bad windows stay documented audit facts only.)
Allowed writes:   tests/fixtures/kalshi_golden/* (1 snapshot + 50 deltas + 50 trades);
                  tests/test_kalshi_golden.py.
Forbidden writes: everything else.
Acceptance:       per-sid seq scoping, trade_id dedupe key, fixed-point strings all
                  asserted on real frames; V12 spec-drift gate wired as precondition.
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green.

### W4 — Coverage auditor
Purpose:          daily scope proof: S1 universe reconciliation, S2 liquidity tiers
                  (High+Mid ⇒ 100% L1), S3 sports completeness, S4 depth-target list.
Allowed reads:    catalog + staging/archive (read-only).
Allowed writes:   tools/coverage_audit.py; tests/test_coverage_audit.py;
                  work/mm/promotion_candidates_*.csv; work/mm/depth_target_*.csv;
                  tools/lifecycle_check.py (append one non-blocking research stage);
                  tools.json (append).
Forbidden writes: config/market_classes.yaml (auto-editing FORBIDDEN — promotions are
                  operator-reviewed changes); pipeline_supervisor.sh (hook wiring goes
                  to next_actions.md as an operator-gated line item, P4).
Acceptance:       clean run on live warehouse; V14/V15/V16 implemented; fixture
                  catalogs incl. fake-new-category test (V16).
Rollback:         revert commit.
Exit evidence:    commit hash; audit report path; promotion-candidates CSV path.

### W5 — Exporter seq column (the ONE production-adjacent change)
Purpose:          per-sid WS seq (+sid) into orderbooks_full, raw→staging→export;
                  gold builder prefers real seq, falls back to file order for
                  pre-change days (recorded in the validation report).
Allowed reads:    ingest/export source; test captures.
Allowed writes:   tools/ingest.py; tools/export_day.py; their tests (same commit);
                  docs/warehouse_schema.md (same commit, E5).
Forbidden writes: apps/ws_shadow.cpp and ALL capture-side code; supervisor.
Acceptance:       additive nullable column flows end-to-end on a test capture;
                  ingest-side test same commit (D4); pipeline tests green;
                  continuity statement: ingester restart only, checkpointed offsets
                  make it gap/dup-free (P4).
Rollback:         revert commit + restart ingester (checkpoint-safe).
Exit evidence:    commit hash; schema doc diff; pipeline test tail.

### W6 — Depth-expansion design + probe (operator-gated)
Purpose:          measured sizing for orderbook_delta expansion; design doc only.
Allowed reads:    docs/vendor/kalshi/latest/* (spec limits); depth_target list.
Allowed writes:   docs/PLAN_DEPTH_EXPANSION.md; probe tool (separate shadow process).
Forbidden writes: production firehose/supervisor/config — NO rollout in this W.
Acceptance:       spec-derived limits documented; IF operator approves the bounded
                  probe (15 min, ~50 markets, read-only, refuses live): measured msg
                  rate + bytes/market by tier in the draft plan.
Rollback:         revert commit (docs + standalone tool only).
Exit evidence:    commit hash; draft plan path with real numbers or probe-pending mark.

## 4. Acceptance (demonstrated, not described)

```sh
make check && tests/run_pipeline.sh                      # all green
python3 tools/gold_build.py --date 2026-07-06            # builds + validates
python3 tools/gold_validate.py --date 2026-07-06 --report # V1-V13 pass, δ/X printed
python3 tools/coverage_audit.py --date 2026-07-06        # S1-S4 report, V14-V16
python3 tools/check_registry.py                          # registry complete
# opt-in only, never in CI:
# BENCH_LARGE=1 python3 tools/gold_bench.py              # W-BENCH volume numbers
```

## 5. Rollback

Everything new is additive files (`gold_*`, `coverage_audit`, header, tests) —
rollback = revert commit, delete `work/gold/` (derived, rebuildable). W5 is the
only change to existing behavior: additive nullable columns; rollback = revert
+ restart ingester (checkpoints make this safe). No capture-process changes
anywhere in this plan.

## 6. Risks to production (P4)

- ws_shadow/firehose: untouched. Capture continuity: guaranteed by not
  deploying anything into that process.
- ingest.py: touched only in W5, additive, restart-safe via checkpoints,
  shipped with tests.
- DuckDB single-writer: gold_build reads via load() with reader retries (D6);
  never holds the staging lock across the merge phase.
- Disk: gold ≈ 512 B × ~650k events/day ≈ ~350 MB/day uncompressed; derived +
  pruned with raw retention cadence. Noted: `work/metrics.ndjson` rotation is
  a separate approved cleanup, not this plan.

## 7. Open item for the operator

Class B promotion policy: this plan only *reports* candidates (S2). Decide
after the first week of promotion-candidate reports whether to promote
per-market, or wholesale categories (Elections/Entertainment). Exotics/MVE
stay Class B regardless (Q7).
