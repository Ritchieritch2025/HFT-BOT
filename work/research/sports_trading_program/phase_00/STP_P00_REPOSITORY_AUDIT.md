# STP_P00_REPOSITORY_AUDIT — verified facts, contradictions, defects, risks (STP-P00-W01)

Method: complete reads of all mandatory documents (READ MANIFEST); mechanical
bidirectional census (`STP_P00_CENSUS.json`); source-level inspection of every
phase-critical component with file:line citations (delegated source reads were
performed inside this W01 session under strict read-only instruction and are
marked "inspected"; every load-bearing claim carries its citation). Every
number states its provenance. Isolated-root test evidence in §5.

## 1. Verified current facts (system state)

1. **Governance chain is complete and internally consistent.** Canonical
   prompt = `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md`,
   sha256 `575ea27a…3fbe54` (measured; matches D-2 and STP-R004 pin).
   Receipts R000→R004 present; R004 ACTIVE for exactly W01+AUD01.
   GUARDRAILS sha `9c71a5b3…832b` unchanged through this session.
2. **Registry census:** `REGISTRY_COUNT_OBSERVED = 144` (parsed from
   tools.json; equals the 2026-07-11 isolation-run `registry ok: 144 tools`).
   Kinds: test 78 / check 17 / tool 29 / bench 5 / probe 11 / daemon 4.
   Safety: pure 86 / offline 34 / network_read 19 / live_order 5
   (bench_order, account_upgrade, panic_live, fill_test, tradingd).
3. **Runnable census (bidirectional):** 16 `apps/*.cpp` mains + 33
   `tests/*.cpp` mains = 49 C++ mains; 48 registry `./build/…` binaries
   (47 in Makefile BINS/PURE_TESTS + fill_test `build:on_demand`); Makefile
   has 54 `$(BUILD)/…` rules (extras = 5 tsan variants + ixws lib dir) and 9
   named targets (all/check/clean/dashboard/fuzz/gate/san/test/tsan); CMake
   builds lib `kalshi` + 5 explicit executables + 28 test executables.
   All 95 registry-referenced scripts exist on disk; 22 filesystem scripts
   are not directly registry-referenced — 19 are libraries/pytest
   modules/mocks reachable via registered entries (`run_pytest`,
   `run_pipeline` needs-list), 2 are the documented R4 isolation scripts,
   and 3 are true unregistered entry points:
   `tests/analyze_full_chain_latency.py`, `tests/verify_captured.py`,
   `tests/run_ws_shadow_mock.sh` (all offline-class helpers; E3 drift, minor).
   **The single registry/build/safety hole is `apps/live_e2e.cpp`** (§3.1).
4. **Two general engine paths confirmed** (H-02): tradingd lane path and the
   `KalshiExecutionEngine` bus path (citations in §2). The
   `NormalizedEvent → SportsDecisionCore → OrderIntent → OrderSubmitter`
   architecture of prompt §26.3 does not exist yet; its substrate does
   (bus.hpp pipeline comment, `include/trading/bus.hpp:8-13`).
5. **Execution-safety components exist as audited but UNWIRED libraries:**
   RiskLedger (five-layer, E6 micro-dollars) and RuleEngine are referenced
   only by their own headers and tests (grep over apps/src/include/tests);
   no production binary instantiates either. Reconcile is manual
   (`tools/reconcile.py:239-245` says alert forwarding is BACKLOG;
   registry `autorun:false`).
6. **Production data path is on EC2** (systemd kalshi-pipeline, cutover
   2026-07-09); the local tree is a frozen mirror (newest local raw
   2026-07-09 23:09 UTC; facts end 2026-07-08; details in
   `STP_P00_DATA_CAPABILITY_MATRIX.md`). W01 touched neither production nor
   local pipeline state.
7. **`docs/PLAN_SPORTS_TRADING_MASTER.md` ABSENT (correct — P01 creates it);
   `docs/PLAN_SPORTS_TRADING_STATE.md` did not exist before this W01**
   (created this session per prompt §8).
8. **v1 prompt full text remains missing from the repo** (prompt §6.3): no
   authoritative v1 source found under docs/ (only V2/V2.1/V2.2). Recorded as
   the known P01 blocker resolved by D-2's interpretation map — D-2 exists,
   so P01 is NOT blocked on v1 recovery (prompt §4: D-2 "does not reconstruct
   v1"; missing v1 archival stays a historical-artifact note, not a live
   blocker).

## 2. §43 hypothesis-verification ledger

The release requires "verify §43 hypotheses from code/data with citations".
The pinned prompt has §0–§36 and no §43 (conflict register C-2); the
verification family applied is the prompt's own verify-items (§26 census
assertions, §26.3 starting classifications and known issues/limitations,
§10.1/§10.5 GUARDRAILS checks, §30 supporting-spec checks), enumerated below
as H-01…H-43 (partitioning of items into 43 rows is ours; the count is not
evidence about the release's referent).

**Tallies: VERIFIED 43 · CONTRADICTED 0 · PARTIAL 0 · NOT-VERIFIED 0.**
Contradictions list: EMPTY — no prompt claim was contradicted by code/data.
(Two adjacent-to-hypothesis findings that are new facts, not contradictions:
live_e2e.cpp unregistered; RiskLedger money is E6 not E4 — deliberate,
documented in PLAN_RISK_KILLSWITCH W-K3 result.)

| id | hypothesis (source) | verdict | evidence (file:line / measured) |
|---|---|---|---|
| H-01 | registry count 144 is generation-time only; observe dynamically (§26) | VERIFIED | tools.json parse = 144; `registry ok: 144 tools, 48 build targets covered` (isolated run 2026-07-11) |
| H-02 | two general engine paths (§26/§26.3) | VERIFIED | tradingd lane: apps/tradingd.cpp:294-305; bus engine: src/gateway.cpp:183-208; docs/ARCHITECTURE.md §"Two execution paths" |
| H-03 | bench_order is live-capable (§26.3) | VERIFIED | apps/bench_order.cpp:121/127 (POST create), :153-156 (DELETE cancel), gate :75-77 `require_orders_allowed` |
| H-04 | fill_test is live-capable (§26.3) | VERIFIED | apps/fill_test.cpp:91/97 (POST IOC, position not exited), gate :47-48 |
| H-05 | `panic --execute` is live-capable (§26.3) | VERIFIED | apps/panic.cpp:397 (DELETE cancel), :471 (POST IOC reduce_only), execute gate :347-364 |
| H-06 | `preflight --order` is live-capable (§26.3) | VERIFIED | apps/preflight.cpp:293-294 (POST, post_only=true), :317-318 (DELETE), gates :272-276 |
| H-07 | apps/live_e2e.cpp is live-capable (§26.3) | VERIFIED | apps/live_e2e.cpp:351/359 (POST create), :386-389 (DELETE), gates :316-321; file = 497 lines, real E2E exchange test |
| H-08 | census must enumerate ALL additional mutation surfaces (§26) | VERIFIED | complete `Method::Post|Method::Delete` sweep over apps/src/include: only H-03…H-07 + tradingd (H-10 lane) + apps/account_upgrade.cpp:88 (account mutation via RequestExecutor). No others |
| H-09 | python tools transmit no mutations; account_view GET-only (§26.3) | VERIFIED | tools/account_view.py:12-13,146-149 (GET-only by construction); tools/reconcile.py:205-236 (reads via account_view); catalog_sync/kalshi_spec_sync/kalshi_update_watcher urlopen with no data payload |
| H-10 | tradingd uses REST polling as feed input (§26.3) | VERIFIED | apps/tradingd.cpp:621-626 (`--poll` only mode), apps/feed.hpp:60-63 (GET /markets loop) |
| H-11 | order lane bypasses full RequestExecutor accounting (§26.3) | VERIFIED | apps/tradingd.cpp:294-305 (sign_request + Lane::send, no executor/ledger includes); include/kalshi/request_executor.hpp:3-4 ("SINGLE outbound path" claim excludes the lane) |
| H-12 | main submission may not force post_only (§26.3) | VERIFIED | include/kalshi/wire.hpp:163 (`post_only = false` default), :189 (field emitted only if true); tradingd.cpp:294 calls without it |
| H-13 | bus Live mode is fail-closed / not transmitting (§26.3) | VERIFIED | src/gateway.cpp:183-208 (DataCollect⇒Rejected; Shadow⇒Logged would-be; Live⇒throw SafetyViolation) |
| H-14 | risk/reconciliation controls not fully unified (§26.3) | VERIFIED | RiskLedger/RuleEngine referenced only by own header+tests (grep); tradingd pre-send checks = decode/TTL/optional bucket/orders_enabled only (tradingd.cpp:226,271-277,285-289); reconcile.py:239-245 BACKLOG note |
| H-15 | mm_backtest: old quotes disappear immediately on requote (§26.3) | VERIFIED | tools/mm_backtest.py:47-48 (stated), :116-123 (requote overwrites unconditionally) |
| H-16 | mm_backtest: fixed/full fill assumptions (§26.3) | VERIFIED | tools/mm_backtest.py:138-142 (`_fill` always qty=self.size) |
| H-17 | mm_backtest: no partial-fill/cancel lifecycle (§26.3) | VERIFIED | same as H-16; no order-state machine anywhere in file |
| H-18 | mm_backtest: latency placeholders (§26.3) | VERIFIED | config/backtest_latency.yaml:5 ("ALL THREE VALUES ARE PLACEHOLDERS — … NOT measurements."); mm_backtest.py:79-83, banner :297-300 |
| H-19 | mm_backtest: decision-clock limitations (§26.3) | VERIFIED | recv default fail-closed / exchange DIAGNOSTIC ONLY (tools.json mm_backtest entry; docs/warehouse_schema.md:108-113 ts_utc look-ahead warning) |
| H-20 | mm_backtest: floating-point cash/inventory (§26.3) | VERIFIED | tools/mm_backtest.py:112-113 (`self.inv = 0.0`, `self.cash = 0.0`), :141 (float division) |
| H-21 | mm_backtest: maker fee hard-coded zero (§26.3) | VERIFIED | tools/mm_backtest.py:16 ("maker fee = 0"); no fee variable in file |
| H-22 | mm_backtest: no settlement (§26.3) | VERIFIED | tools/mm_backtest.py:16-18 |
| H-23 | mm_backtest: residual inventory marked at last midpoint (§26.3) | VERIFIED | tools/mm_backtest.py:150-151 (`pnl = cash + inv·last_mid`) |
| H-24 | maker-edge: exchange-time ASOF analysis (§26.3) | VERIFIED | tools/research/maker_edge_pilot.py:137,147-148 (ASOF on ts_utc); `local_recv_ts_us` absent from file; ts_utc = exchange time on most rows (warehouse_schema.md:108-111) |
| H-25 | maker-edge: public-trade touch-cross classification without own-order lifecycle (§26.3) | VERIFIED | maker_edge_pilot.py:174-180 (fill_class CASE on trade price vs ASOF touch) |
| H-26 | maker-edge: no inventory/cash/terminal/zero-fill-event ledger (§26.3) | VERIFIED | grep over maker_edge_pilot.py + aggregate_maker_edge.py + html_report_maker_edge.py: no such tracking |
| H-27 | maker-edge: heuristic ticker-based event IDs (§26.3) | VERIFIED | maker_edge_pilot.py:190 (regexp strip last `-` segment); same in build_segments.py:120 |
| H-28 | build_segments is Tennis-oriented; must not silently define the all-Sports universe (§26.3) | VERIFIED | tools/research/build_segments.py:49-56 (tour_level knows only KXATP/KXWTA/KXITF/CHALLENGER), :73 (default `--subcategory Tennis`), :156-159 (>2% _unsegmented fails ⇒ non-tennis run fails as-is); PLAN_FULL_MARKET_RESEARCH.md §3 states the same boundary |
| H-29 | fixedpoint: PriceE4 int32 ×1e4, CountFp int64 ×1e2, no-float parsing (§26.3 foundation) | VERIFIED | include/trading/fixedpoint.hpp:27-28, :4-5 |
| H-30 | bus: source-agnostic normalized schema + trace propagation (§26.3) | VERIFIED | include/trading/bus.hpp:1-20 (layer-boundary rule, pipeline, trace_id mint/threading, threading contract) |
| H-31 | ids/timestamp: deterministic IDs, dual-clock labeling (§26.3) | VERIFIED | include/trading/ids.hpp:5-12 (FNV-1a deterministic EntityId, Unknown fails closed); include/trading/timestamp.hpp:4-9 (wall/mono always labeled) |
| H-32 | storage: byte-exact raw log + replay compatibility (§26.3) | VERIFIED | include/trading/storage.hpp:5-13 (byte-exact NDJSON, ReplaySource preserves SourceId ⇒ live≡replay) |
| H-33 | warehouse.load() is the single default research entry point (§26.3) | VERIFIED | tools/warehouse.py:2-11 (single entry, archive/staging routing), def load :150; AF-3 per-partition dedup (warehouse_schema.md §Access) |
| H-34 | gold chain exists with tests (§26.3) | VERIFIED | tools/gold_{load,fsm,merge,io,validate,build,dtype,v5_delta,v7_race}.py present; tests test_gold_* registered (tools.json) and green in isolated run |
| H-35 | production data path (ingest/export/supervisor/deploy units) present ⇒ PRESERVE (§26.3) | VERIFIED | tools/ingest.py, tools/export_day.py, tools/pipeline_supervisor.sh on disk + registered; deploy/ contains kalshi-pipeline.service, tradingd.service, launchd plists, S3 sync/alert/oom units |
| H-36 | pricing lo/fair/quote exist and compute in log-odds (§26.3) | VERIFIED | tools/pricing/lo.py:1-5, fair.py:3, quote.py:3-4 (all state lo-space per Q1); W-P1..P4 audited results in PLAN_PRICING_MODEL.md |
| H-37 | request_executor implements reserve-before-send (§26.3) | VERIFIED | src/request_executor.cpp:94-99 (RESERVE before signing/HTTP; local-429 refusal :103-108) |
| H-38 | risk_ledger: five-layer atomic reservation ledger (§26.3) | VERIFIED | include/kalshi/risk_ledger.hpp:13-27 (five layers, any-short⇒reject-all), :59 (Micros int64 E6), :152-188 (one lock across check-and-deduct) |
| H-39 | rule_engine: disconnect⇒cancel-all, dead-man, day-loss breaker, cancels never shed (§26.3) | VERIFIED | include/kalshi/rule_engine.hpp:6-21 (all four rules), :109-116, :166-170 |
| H-40 | panic: dry-run default; S3 sequence; IOC+reduce_only liquidation (§26.3) | VERIFIED | apps/panic.cpp:14-15 (dry-run default), :380-414/417-427/439-483/495-531 (cancel-all→verify-zero→reprice-cross→report), :313-314 (IOC+reduce_only) |
| H-41 | latency tools measure only / venue-authenticated ones need future release (§26.3) | VERIFIED | include/kalshi/full_chain_latency_probe.hpp = pure timestamp bookkeeping, no network includes; bench_rtt/probe entries classed network_read with creds/prod_env needs (tools.json) |
| H-42 | GUARDRAILS has NO H1 identifier; §10.1 clause IDs all exist (§10.1/§10.5) | VERIFIED | complete read of docs/GUARDRAILS.md (149 lines): clauses S1-S6, D1-D6, Q1-Q9, E1-E7, P1-P9 all present; no "H1" token; "H1" = hypothesis H1 of the 2026-07-09 RESEARCH_EDGE_HYPOTHESES ledger (SESSION_LOG 2026-07-09 23:30 entry) |
| H-43 | S1/S4 burstiness source correction is in current code/docs (§30) | VERIFIED | tools/research/aggregate_maker_edge.py:225-227 (ERRATUM 2026-07-10 string in output), html_report_maker_edge.py:118; DECISIONS D-1 归档注记 line 91 (fixed in 8d57be0); PLAN_RESEARCH_CYCLE_1 S4 lines 122-124; PLAN_MM_TEST_PROGRAM B1 §5 |

## 3. Known defects / architectural gaps (neither hidden nor overstated)

### 3.1 live_e2e.cpp — the one census hole (E3 drift)
`apps/live_e2e.cpp` (497 lines) transmits real orders (H-07) yet has NO
tools.json entry, NO Makefile rule, NO CMake target — the only order-capable
runnable outside the registry safety-class system. Mitigations in place: it
is env-gated internally (`can_place_orders` + `require_orders_allowed`,
:316-321) and cannot be built by `make all` (manual compile required). Last
touched in commit ac2d2ad (2026-07-06). Disposition: DO_NOT_USE + OPERATOR-TBD
(register C-13); registration/retirement is an engineering W outside P00's
write scope.

### 3.2 Execution gaps (from ARCHITECTURE_REVIEW gap register, re-checked 2026-07-11)
Gaps #1–#8 (private fills channel, position/PnL tracker, resting-order
registry, QuoteManager, amend/batch/cancel-all, post-only on live path, WS→
strategy dispatch, order path through executor) remain open — future
STP-P08/MASTER_SEQUENCE World A/B merge. Gaps #9–#11 (kill switch, risk
caps, reconcile) now EXIST as audited standalone components (W-K1..K5,
2026-07-10) but are NOT wired into any engine path (H-14) — the review
predates them; both understatement and overstatement avoided.

### 3.3 Statistical/instrument defects binding on research reuse
- Old fill/backtest semantics unusable for binding PnL (H-15…H-23).
- maker-edge S1 evidence is HISTORICAL, dev-grade, regime=slam-week,
  exchange-clock ASOF (H-24…H-27); its per-match bootstrap conflicts with
  prompt §20.3 calendar-day blocks (register C-8).
- config/backtest_latency.yaml placeholders (H-18) — any gate consuming them
  is NON-GATE by construction.

### 3.4 Production risks (stated, unchanged by W01)
- EC2 kalshi-pipeline untouched; no production query or mutation this session.
- Mac launchd `com.ritcardo.rtt-baseline` still appends locally (isolation R2)
  — benign, but it will keep tripping strict state-diff verdicts (see §5).
- Credentials: never read; `~/.kalshi/env.sh` never sourced.

## 4. Historical artifacts / prototypes / disconnected components

| artifact | status |
|---|---|
| S1 maker-edge outputs (work/research/maker_edge_pilot/*, results 2026-07-10) | HISTORICAL evidence, DIAGNOSTIC_ONLY conclusions (matrix) |
| work/gold date=2026-07-06 | HISTORICAL single-day gold build + reports |
| work/event_packs/index.parquet | prototype (8 test-fixture rows, no real packs) |
| sandbox/ (discovery/, research/, obs.html, obs_server.py, wa4_capture_diff.py) | P8 free-fire zone; nothing imported by production (rule §26.4 upheld) |
| tools/load_db.py | legacy loader, superseded by warehouse (registry description says so) |
| frozen prompts V2/V2.1 | immutable historical candidates (D-2) |
| apps/live_e2e.cpp | disconnected component (3.1) |
| outputs/ (Muchova–Gauff xlsx) | pre-existing operator export, unowned, untouched |

## 5. Test evidence (isolated root, §31.2 mechanism only)

Run: `bash tests/isolated_run.sh --new`, root
`/private/tmp/stp-p00-test-isolation.PEO6fh`, clone HEAD `50018a5`, port 18400,
2026-07-11 ≈18:37 UTC:
- make all rc=0 (16s) · make check rc=0 (8s, 23 suites) ·
  tests/run_pipeline.sh rc=0 (48s) — **66/66 suites, 469/0 assertions** ·
  `check_registry --require-built` rc=0 — `registry ok: 144 tools, 48 build
  targets covered`.
- Harness verdict `FAIL_STATE_CHANGED` on the operational-tree diff —
  attributed byte-exactly (proof
  `…PEO6fh/proof/ops_work_manifest_diff.txt`, 22 lines): (a)
  `work/latency_baseline/{samples.ndjson,sampler.out.log}` = documented R2
  production sampler; (b)
  `work/research/sports_trading_program/phase_00/{STP_P00_BOOTSTRAP0.md,
  STP_P00_AUTHORITY_MAP.md}` = THIS session's release-authorized evidence
  writes racing the snapshot window. pytest_cache / git-status / all five
  forbidden operational files UNCHANGED (hashes byte-identical). The tests
  themselves wrote nothing operational.
- A clean re-run with no concurrent evidence writes was performed after all
  evidence files were finalized — result recorded in `STP_P00_HANDOFF.md`.
- No test was run outside the isolated mechanism. No make/pytest in the
  operational tree.

## 6. Later-phase hypotheses (recorded, NOT investigated beyond inventory)

- Primary program hypothesis (prompt §2) untested — P00 makes NO
  profitability claim in any direction.
- H-OP-1 (favorite miscalibration) / H-OP-2 (in-play erosion) live in
  `docs/research_notes/HYPOTHESIS_LEDGER.md` (EXPLORATORY, operator-biased
  provenance); H1–H13 in RESEARCH_EDGE_HYPOTHESES_2026-07-09.md. All are
  candidate-registry inputs for STP-P06 at the earliest; none grant
  authority.
