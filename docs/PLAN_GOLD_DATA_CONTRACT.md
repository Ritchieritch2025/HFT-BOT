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
  // ordering / identity (32 B)
  uint64_t ts_us;                // capture epoch µs — the only clock
  uint64_t stream_seq;           // global monotonic merge order (total-order tie-break)
  uint32_t market_id;            // dense per-day id -> sidecar dim (ticker, category, close_time)
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
  uint64_t trade_id_hash;        // xxh64(trade_id); full UUID in sidecar keyed by stream_seq
  // book state as-of this record, yes-space, best-first (432 B)
  int32_t  bid_px_e4[kDepth];    // from yes_levels (Yes bids)
  int32_t  ask_px_e4[kDepth];    // = 10000 - no_price, from no_levels (No bids)
  int64_t  bid_qty_e4[kDepth];
  int64_t  ask_qty_e4[kDepth];
  int64_t  bid_rest_qty_e4, ask_rest_qty_e4;  // tail beyond kDepth — aggregated, never dropped silently
  uint16_t bid_nlevels, ask_nlevels;
  uint8_t  _pad2[12];
  uint64_t _reserved[2];
};
static_assert(sizeof(GoldRecord) == 512);
```

Python mirror: a numpy structured dtype with identical offsets. Layout parity
is test-enforced (V11): a tiny C++ test target dumps `{field: offset}` JSON;
the Python test compares against the dtype. Little-endian asserted both sides.

Rules: E4 integers everywhere; prices parsed by integer digit accumulation
(no float parse anywhere in the loader, D5). Uncovered books ⇒ arrays zeroed +
`F_BOOK_COVERED=0`; L1-only markets get top-of-book in slot 0 with the same
flag. Invalid books ⇒ arrays zeroed + `F_BOOK_VALID=0` (never stale data).

### 2.2 Synchronization (build-time, single cursor)

1. Normalize each source to typed events. Trades: stable-sort `(ts_us,
   trade_id)`, dedupe on `trade_id`.
2. Per-market book FSM mirroring `include/kalshi/orderbook.hpp` semantics:
   snapshot ⇒ load+valid; delta driving any level negative ⇒ INVALID (no
   clamp), stays invalid until next snapshot; heartbeats never mutate.
3. Global k-way merge on `(ts_us, type_priority, source_file_order)` with
   **TRADE < BOOK_DELTA at equal ts_us** (the same-µs delta is usually the
   decrement caused by the trade; the trade must see the pre-trade book).
4. Emit one GoldRecord per event with the FSM state at merge position.
   Look-ahead is structurally impossible: one forward cursor, no timestamp
   lookups, trades reference only already-emitted `book_seq`.
5. Channel races (trade prints outside as-of touch) are flagged
   `F_TRADE_RACED` and counted — published metric, never repaired (D2).

Output: `work/gold/date=<D>/gold_<D>.bin` + sidecars
(`markets_<D>.csv` dim incl. close_time; `trade_ids_<D>.csv` stream_seq→UUID).
Derived data: rebuildable from the warehouse, safe to delete, excluded from
raw-retention rules.

### 2.3 Validator — build fails or the day is quarantined unless ALL pass

| # | Test | Pass criterion |
|---|---|---|
| V1 | Lossless E4 round-trip | every price/qty string re-renders byte-exact from the parsed integer; zero float parses in loader code (grep-gated) |
| V2 | Manifest reconciliation | parsed row counts == archive manifest counts; md5 match |
| V3 | Total order | `stream_seq` dense; `(ts_us, stream_seq)` non-decreasing; per-market `book_seq` strictly increasing, gap-free |
| V4 | Book integrity | zero negative levels emitted; negative delta ⇒ invalid-until-snapshot (count reported); crossed books flagged not repaired |
| V5 | L1 cross-check | covered markets: reconstructed top-of-book equals L1 change rows within measured channel-race window δ (start 500 ms, tighten to empirical p99); ≥99% agreement. 1 µs equality across independent WS channels is physically meaningless — δ is measured and published |
| V6 | No look-ahead | every TRADE: `ts(book_seq) ≤ ts_us(trade)` AND merge-position(book) < merge-position(trade), asserted structurally on the emitted stream |
| V7 | Economic consistency | ≥X% trades print at as-of best (taker=yes ⇒ ask, taker=no ⇒ bid); X calibrated day one, then regression-locked; race rate reported per category |
| V8 | Trade dedupe | trade_id unique; zero xxh64 collisions vs sidecar |
| V9 | Heartbeat neutrality | hourly heartbeats advance no book_seq, diff no state |
| V10 | Coverage honesty | traded-but-uncovered market count reported; all such records `F_BOOK_COVERED=0`; no silent inner-join shrinkage |
| V11 | Layout parity | C++ static_asserts (size 512 + offsets) and Python dtype match the dumped layout JSON; mmap random access == streamed parse on 1,000 sampled records |
| V12 | Spec-drift gate | `kalshi_spec_sync` green (last saved result) before any gold build; drift in ticker/trade/orderbook_delta schemas blocks the day |
| V13 | Golden-frame semantics | field semantics verified against real frames sampled from `work/raw/`: per-sid seq scoping, trade_id dedupe key, taker_side lift direction (empirical via V7), fixed-point strings |
| V14 | Liquidity coverage | 100% of High+Mid tier markets have L1 coverage; violations listed with category + class |
| V15 | Depth-set stability | full-depth market set matches the declared subscription list; shrinkage is an error not a warning |
| V16 | Class-policy safety net | new/unlisted Kalshi category ⇒ Class B default + surfaced warning (matches build_classification behavior) |

## 3. Workstreams (execute in order; each = one commit; tests in the same change)

### W1 — Contract in code
- `include/trading/gold_record.hpp` + `tests/test_gold_layout.cpp` (offset/size
  static_asserts + layout JSON dump target).
- `tools/gold_dtype.py` (numpy mirror) + `tests/test_gold_dtype.py`.
- Add both to Makefile check + `tests/run_pipeline.sh`; register in
  `tools.json` (safety class: research/read-only); `check_registry` passes.
- **Done when:** V11 green via `make check`.

### W2 — Builder + core validator (SIX atomic sub-steps; one commit each;
### a sub-step does not start until the previous one is green)

**Anti-fake-green rule (binding on every sub-step):** every validator check
and every FSM/merge behavior ships with at least one **seeded-defect fixture
that makes it FAIL**. A check that has never been red is unproven (D2). The
red fixtures live in `tests/fixtures/gold_defects/` and run in CI as
"must-fail" assertions.

Size discipline: each sub-step is one component, ≤ ~300 new lines excluding
tests, pure functions before I/O, no forward references to later sub-steps.

- **W2.1 Typed loaders** — `tools/gold_load.py`: one loader per source
  (l1 / full / trades) → typed event tuples. E4 integer digit-accumulation
  parse only; trades sort + trade_id dedupe here. Tests: golden rows from the
  real 2026-07-06 files, byte-exact re-render (V1), duplicate-trade fixture
  (V8 red test), float-parse grep gate.
  *Done:* loaders round-trip real rows; defect fixtures fail.
- **W2.2 Book FSM** — pure module, zero I/O: snapshot/delta/invalid/heartbeat
  per §2.2 step 2. Synthetic tests: negative-delta ⇒ INVALID (not clamp),
  invalid-until-snapshot, heartbeat neutrality (V9), crossed-book flag,
  yes-space ask transform (10000 − no_price). Red fixtures: clamp bug,
  stale-state-after-invalid bug.
  *Done:* FSM tests green AND red fixtures fail.
- **W2.3 Merge iterator** — pure ordering logic over synthetic event lists:
  total order `(ts_us, TRADE<BOOK_DELTA, file_order)`, stream_seq/book_seq
  minting, trade→book_seq assignment. Structural V6 on synthetic streams.
  Red fixtures: same-µs delta-before-trade bug, book_seq gap bug.
  *Done:* merge tests green AND red fixtures fail.
- **W2.4 Gold writer/reader** — records → `.bin` + sidecars; numpy mmap
  read-back equals input record-for-record (runtime half of V11).
  *Done:* write→mmap→compare on 10k synthetic records.
- **W2.5 Validator harness** — `tools/gold_validate.py` running V2, V3, V4,
  V6, V9, V10 as independent checks over a built file + quarantine-on-fail.
  Each check gets its own seeded-defect gold file that must turn it red.
  *Done:* all checks green on synthetic good file; each red on its defect file.
- **W2.6 First real build** — `python3 tools/gold_build.py --date 2026-07-06`
  (thin composition of W2.1–W2.5, no new logic). Validation report committed.
  *Done:* real day builds + validates; report in `work/gold/`.

### W3 — Cross-source checks (three sub-steps, same discipline)
- **W3.1** V5 L1 agreement: measure δ on 2026-07-06, write into report.
  Red fixture: shifted-book file must break agreement.
- **W3.2** V7 economic consistency: calibrate X on 2026-07-06, lock as
  regression threshold. Red fixture: inverted taker_side must fail.
- **W3.3** V13 golden frames: extract 1 snapshot + 50 deltas + 50 trades from
  `work/raw/` into `tests/fixtures/kalshi_golden/`; assert field semantics.
- **Done when:** report prints δ, X, race rates per category; thresholds
  committed; every red fixture verified failing.

### W4 — Coverage auditor
- `tools/coverage_audit.py` (read-only, daily, invoked by the supervisor's
  post-export hook alongside mm_scan):
  - S1 universe reconciliation: catalog active markets vs observed L1/trades;
    active-but-unseen > N hours ⇒ alert line in report + nonzero warn status.
  - S2 liquidity tiers (High/Mid/Low by volume × trade count from our own
    warehouse, Q4); **requirement: High+Mid ⇒ 100% L1**; Class B violators
    listed as promotion candidates in `work/mm/promotion_candidates_<D>.csv`.
    Auto-editing `market_classes.yaml` is FORBIDDEN in this plan — operator
    applies promotions as a reviewed change.
  - S3 sports completeness: every catalog sports subcategory present in L1;
    root-cause lines for the 2 missed markets pattern (late listing lag).
  - S4 depth-target list: High tier + Mid-tier Sports + mm_scan top-N, written
    to `work/mm/depth_target_<D>.csv` — this is the input to the future
    depth-expansion plan, not a subscription change.
- Tests: `tests/test_coverage_audit.py` with fixture catalogs (incl. a fake
  new category → V16).
- Wire V12+V14+V15+V16 into `lifecycle_check` as a new non-blocking research
  stage (blocking for gold builds only).
- **Done when:** auditor runs clean on live warehouse; report file exists;
  `tests/run_pipeline.sh` green.

### W5 — Exporter seq column (the one production-adjacent change)
- Add per-sid WS `seq` (and sid) columns to `orderbooks_full` in ingest +
  export; additive, nullable for historical rows.
- **D4 obligation:** ingest-side test in the same change.
  **P4 continuity:** ingest.py --loop change is backward-compatible (old rows
  unaffected); deploy = restart ingester only (checkpointed offsets make the
  restart gap/dup-free — cite the checkpoint test); ws_shadow capture process
  is NOT touched. Update `docs/warehouse_schema.md` in the same commit (E5).
- Gold builder prefers real seq when present; falls back to file order for
  pre-change days (recorded in the validation report).
- **Done when:** new column flows raw→staging→export on a test capture;
  pipeline tests green; schema doc updated.

### W6 — Depth-expansion design + bounded probe (NO rollout)
- From spec (asyncapi/openapi) extract subscription limits: max markets per
  subscribe, per-connection caps, rate implications (C3). Where the spec is
  silent, run ONE bounded read-only probe: a separate shadow WS process (never
  the production firehose) subscribing `orderbook_delta` for the top ~50
  markets from `depth_target`, 15 minutes, measuring msg rate + bytes/market
  by tier. Requires operator go-ahead to run (network); refuses live mode.
- Deliverable: `docs/PLAN_DEPTH_EXPANSION.md` draft with measured sizing,
  connection topology, rollout-with-continuity story, and D4 test list —
  for operator approval as its own plan.
- **Done when:** the draft plan exists with real numbers in it.

## 4. Acceptance (demonstrated, not described)

```sh
make check && tests/run_pipeline.sh                      # all green
python3 tools/gold_build.py --date 2026-07-06            # builds + validates
python3 tools/gold_validate.py --date 2026-07-06 --report # V1-V13 pass, δ/X printed
python3 tools/coverage_audit.py --date 2026-07-06        # S1-S4 report, V14-V16
python3 tools/check_registry.py                          # registry complete
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
