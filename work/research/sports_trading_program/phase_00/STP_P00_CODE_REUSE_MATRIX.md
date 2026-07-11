# STP_P00_CODE_REUSE_MATRIX — complete reuse matrix (STP-P00-W01)

Machine-readable full matrix (one row per registry entry, plus phase-critical
libraries and build targets): `STP_P00_CODE_REUSE_MATRIX.json` (same
directory). This document is the human digest plus the load-bearing rows.

## Coverage statement (census completeness — bidirectional)

- `REGISTRY_COUNT_OBSERVED = 144` (dynamic parse; equals isolated-run
  `registry ok: 144 tools, 48 build targets covered`). **All 144 have a
  matrix row** in the JSON. No duplicate names. Kind/safety distributions in
  `STP_P00_CENSUS.json`.
- Filesystem→registry direction: 49 C++ `main()` files (16 apps + 33 tests)
  — all covered by registry/Makefile except **`apps/live_e2e.cpp`**
  (unregistered, unbuilt, live-capable — DO_NOT_USE, register C-13);
  95 registry-referenced scripts all exist; 22 non-referenced scripts
  classified (19 libs/mocks/pytest modules reachable via registered
  runners, 2 = §31.2 isolation scripts [documented R4], 3 = unregistered
  helper entry points → OPERATOR_TBD row).
- Registry→build direction: 47 registry binaries in Makefile
  BINS/PURE_TESTS + `fill_test` (`build:on_demand`, by design) = 48.
  Makefile extras beyond the registry: 5 tsan build variants + `ixws` lib
  dir + named targets (all/check/clean/dashboard/fuzz/gate/san/test/tsan) —
  all rows in the JSON `build_targets` section. CMake mirrors the core lib +
  5 executables + 28 test executables (ws_smoke/ws_shadow Makefile-only,
  documented in CMakeLists.txt:21-24).
- **Argument-mode census:** every entry's `args_template` is captured in
  the JSON. Live-capable argument modes: `panic --execute` (separately
  registered as `panic_live`, live_order ✓ correct), **`preflight --order`
  (transmits; entry is network_read; mode documented but NOT split into a
  live_order entry — registry/safety mismatch surfaced, C-13)**,
  `tradingd` (live via KALSHI_MODE/ALLOW_LIVE env — entry correctly
  live_order), `depth_probe --operator-approved` (read-only but
  operator-gated). No other mutating argument modes found (complete
  Method::Post/Delete sweep, REPOSITORY_AUDIT H-08).

## Classification policy (applied uniformly)

Exactly one primary classification per artifact (§26.1 — no slashes).
REUSE_AS_IS only where source/tests were inspected (≥L2) and relevant tests
pass (L3 = executed in the isolated root this session, 66/66 suites, 469/0
assertions). Families not source-inspected this session take the
conservative single value. All 5 live_order runnables = DO_NOT_USE at the
current phase ceiling (§26.3). Slash-form "starting classifications" from
§26.3 were resolved to single values; where evidence was insufficient the
value is OPERATOR_TBD, never a guess.

Distribution over the 144 registry rows: REUSE_AS_IS 89 (78 tests + 11
infra/tools) · EXTEND 31 · PRESERVE 12 · DIAGNOSTIC_ONLY 7 · DO_NOT_USE 5.

## Phase-critical rows (full citations in JSON / REPOSITORY_AUDIT §2)

| artifact | class | lvl | key evidence | STP consumer |
|---|---|---|---|---|
| include/trading/fixedpoint.hpp | REUSE_AS_IS | L3 | PriceE4 int32×1e4 / CountFp int64×1e2, no-float (H-29) | all |
| include/trading/bus.hpp | REUSE_AS_IS | L3 | source-agnostic + trace threading (H-30) | P05/P08/P09 |
| include/trading/ids.hpp · timestamp.hpp · storage.hpp | REUSE_AS_IS | L3 | H-31/H-32 | all / P03/P05 |
| include/kalshi/{orderbook,sid_stream,recovery,ws_client,ws_recorder}.hpp | EXTEND | L3 | tests green; component-level work in P05/P08/P09 | P02–P09 |
| src/gateway.cpp (decoder + engine) | EXTEND | L3 | fail-closed engine (H-13) | P05/P08 |
| apps/tradingd.cpp lane | EXTEND | L2 | REST poll (H-10), executor bypass (H-11), no post_only (H-12), orders_enabled fail-closed gate | P08 convergence base; NOT strategy-live now |
| include/kalshi/wire.hpp | EXTEND | L2 | post_only default false (H-12) | P08 must force post_only |
| src/request_executor.cpp | REUSE_AS_IS | L3 | reserve-before-send (H-37) | P08 single submitter |
| include/kalshi/risk_ledger.hpp | REUSE_AS_IS | L3 | five layers, E6 Micros, atomic (H-38); **UNWIRED** (H-14) | P08 |
| include/kalshi/rule_engine.hpp | REUSE_AS_IS | L3 | 4 defensive rules, cancels never shed (H-39); **UNWIRED** | P08 |
| apps/panic.cpp | EXTEND | L3 | dry-run default, S3 sequence, IOC+reduce_only (H-40); `panic_live` runnable = DO_NOT_USE | P10 |
| tools/account_view.py | REUSE_AS_IS | L3 | GET-only by construction (H-09) | P08/P10 |
| tools/reconcile.py | REUSE_AS_IS | L3 | exchange-wins, never resend; alert forwarding BACKLOG | P08/P10 |
| tools/warehouse.py::load | REUSE_AS_IS | L3 | single research entry (H-33); AF-3 partition dedup | P02+ |
| tools/gold_* chain | EXTEND | L3 | green tests (H-34) | P02 |
| tools/ingest.py · export_day.py · pipeline_supervisor.sh · deploy/** | PRESERVE | L2/L1 | production path (H-35); read-only consumption | P02 outputs only |
| tools/event_* + capture_gaps | EXTEND | L2 | green tests; packs not yet real locally | P02 |
| tools/research/build_segments.py | EXTEND | L2 | Tennis-oriented (H-28) — must not define all-Sports universe | P02/P06 |
| tools/research/maker_edge_pilot.py (+aggregate/html libs) | DIAGNOSTIC_ONLY | L2 | HISTORICAL evidence; H-24..27; C-8 bootstrap conflict | P07 after component split |
| tools/mm_backtest.py + config/backtest_latency.yaml | DIAGNOSTIC_ONLY | L2 | H-15..23, H-18 placeholders; never go/no-go | P05 replaces |
| tools/{mm_scan,mm_research,mm_calibrate,gate_calc}.py | DIAGNOSTIC_ONLY | L1 | baselines/features only | P06 |
| tools/pricing/{lo,fair,quote}.py | OPERATOR_TBD | L2 | lo-space verified (H-36); semantics-fit vs market-dynamics policy = P06 decision | P06 |
| apps/{bench_orderbook,bench_ws_decode}.cpp | REUSE_AS_IS | L2 | offline/local measurement (H-41) | P04 |
| bench_rtt / probes / spec-sync (network_read family) | EXTEND | L1 | authenticated use requires a per-release grant | P04 |
| apps/ws_shadow.cpp | EXTEND | L2 | never transmits; production firehose engine | P09 |
| monitoring family (freshness, daily_check, warehouse_status, feed_readiness, verify_*, lifecycle_check) | EXTEND | L2 | dedicated tests green | P13 |
| docs/vendor/js/echarts.min.js | REUSE_AS_IS | L2 | present; §26.4 no new chart bundle | reporting |
| bench_order · fill_test · panic_live · account_upgrade · tradingd (runnables) | DO_NOT_USE | L2 | live_order class; current phase ceiling | P08+/P10+ under live_order=true release |
| apps/live_e2e.cpp | DO_NOT_USE | L2 | live-capable, UNREGISTERED/UNBUILT (census hole, C-13) | none — register-or-retire OPERATOR_TBD |
| tests/isolated_run.sh + test_isolation_controls.sh | REUSE_AS_IS | L3 | §31.2 audited mechanism (R003) | every W/AUD |
| sandbox/** | DIAGNOSTIC_ONLY | L1 | P8 free-fire; production never imports | exploration |

## No-duplicate-subsystem check (§26.4)

PASS. One warehouse (`tools/warehouse.py::load`), one normalized event bus
(`include/trading/bus.hpp`), two existing transmit paths and no third
strategy-transmission architecture, one vendored chart bundle, one registry.
The governed simulator (P05), decision core (P08) and statistical engine
(P07) do NOT exist yet — building them as extensions of the classified
components above, not as parallel systems, is the binding plan
(`STP_P00_PHASE_REUSE_MAP.md`).
