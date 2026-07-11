# STP_P00_READ_MANIFEST — everything read/inspected in STP-P00-W01

Source commit for ALL entries: `50018a55e383fd4f7cb331466da79ef023a42e88`
(HEAD, unchanged throughout the read phase; working tree clean except
pre-existing untracked `outputs/`). Evidence levels per prompt §26.2.
"COMPLETE" = every line read in this session. Partial source reads record
their exact scope and are NOT represented as complete-file reads (§31.1).

## A. Mandatory documents — read COMPLETELY (prompt §31.1)

| path | lines | bytes | sections | level | status |
|---|---|---|---|---|---|
| docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md | 3,274 | 96,597 | §0–§36, all (sha verified 575ea27a…3fbe54) | L2 | COMPLETE |
| docs/plan_releases/sports_trading_program/STP-R004-P00.md | 35 | 3,225 | all (release + verbatim block) | L2 | COMPLETE |
| CLAUDE.md | 99 | 6,467 | all | L2 | COMPLETE |
| docs/GUARDRAILS.md | 149 | 9,063 | all (S/D/Q/E/P + checklist) | L2 | COMPLETE |
| docs/MM_ROADMAP.md | 87 | 5,501 | all (Phases 0–5) | L2 | COMPLETE |
| docs/MASTER_SEQUENCE.md | 124 | 7,276 | all (STEPs + amendments) | L2 | COMPLETE |
| docs/PLAN_SPORTS_TRADING_DECISIONS.md | 107 | 7,838 | all (D-2, D-1.1, D-1) | L2 | COMPLETE |
| docs/PLAN_MM_TEST_PROGRAM.md | 745 | 44,188 | all (G0–G9, A–I groups, F2c/FA-1/F2d, §13–16) | L2 | COMPLETE |
| docs/PLAN_RESEARCH_CYCLE_1.md | 316 | 20,307 | all (S0–S6, 12 disciplines, viz spec, 附录A) | L2 | COMPLETE |
| docs/PLAN_FULL_MARKET_RESEARCH.md | 379 | 17,070 | all (U0–U6, waves) | L2 | COMPLETE |
| docs/PLAN_LIVE_VALIDATION.md | 264 | 17,381 | all (Phases 0–3 + spec verification) | L2 | COMPLETE |
| docs/PLAN_PRICING_MODEL.md | 542 | 33,401 | all (W-P1..P4 results, Group C gate box) | L2 | COMPLETE |
| docs/PLAN_RISK_KILLSWITCH.md | 613 | 39,472 | all (contracts §1, W-K1..K6 results) | L2 | COMPLETE |
| docs/ARCHITECTURE.md | 147 | 8,827 | all | L2 | COMPLETE |
| docs/ARCHITECTURE_REVIEW_2026-07-06.md | 177 | 10,610 | all (two-worlds, gap register) | L2 | COMPLETE |
| docs/warehouse_schema.md | 232 | 14,940 | all (layers, TL1 ladder, event packs) | L2 | COMPLETE |
| tools.json | 150 | 67,748 | all 144 entries (lines 1–108 + 109–150) | L2 | COMPLETE |
| Makefile | 387 | 21,497 | all | L2 | COMPLETE |
| CMakeLists.txt | 81 | 3,664 | all | L2 | COMPLETE |
| docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_ARTIFACT.md | 251 | 14,349 | all | L2 | COMPLETE |

## B. Relevant SESSION_LOG entries (mandatory "relevant entries" — file is 3,700 lines / 250,346 bytes total; the relevant-scope read is recorded honestly as PARTIAL-BY-DESIGN)

| scope | content | status |
|---|---|---|
| lines 1–260 (2026-07-11 entries, 8 entries: R004-adjacent ISO-AUD01/ISO-W01, R002 ×2, FA-1, F2c/F2d, account+D-1.1, R001) | read fully | COMPLETE for scope |
| targeted greps of full file | "hypotheses"/H1–H13 provenance (lines ~1336, 1423–1443), burstiness erratum (~405–408) | SECTION |

## C. Governance/receipt files also read completely

`docs/plan_releases/sports_trading_program/` receipts R000–R004 (R004 fully;
others identified by receipt-status greps + prior entries), frozen prompt V2
section headers (structure only, for the §43 search — PARTIAL: grep +
header scan; V2 bytes are frozen-historical and not authority).

## D. Source-section inspections (NOT complete-file reads; §31.1 separate recording)

Direct (this conversation):
| path | scope inspected | purpose |
|---|---|---|
| include/trading/bus.hpp | lines 1–20 | H-30 |
| include/trading/ids.hpp | 1–12 | H-31 |
| include/trading/timestamp.hpp | 1–10 | H-31 |
| include/trading/storage.hpp | 1–14 | H-32 |
| include/trading/fixedpoint.hpp | 1–28 + float-grep | H-29 |
| docs/vendor/js/ | listing + sizes | H-40 (ECharts) |

Delegated read-only inspections inside this W01 session (two sub-readers,
strict no-write/no-network instruction; all load-bearing citations spot-
checkable at the cited lines; recorded as inspections, not complete reads):
| path | scope | purpose |
|---|---|---|
| apps/tradingd.cpp | 3, 29–37, 133–157, 171, 226, 271–305, 578, 621–628 | H-10..H-12, gating |
| apps/feed.hpp | 4–5, 60–63 | H-10 |
| src/gateway.cpp | 176–208 | H-13 |
| src/env.cpp | 168–197 | orders_enabled fail-closed |
| include/kalshi/wire.hpp | 163, 189 | H-12 |
| include/kalshi/request_executor.hpp / src/request_executor.cpp | 3–11 / 94–108 | H-37 |
| include/kalshi/risk_ledger.hpp | 1–27, 59, 152–188 | H-38 |
| include/kalshi/rule_engine.hpp | 2–21, 109–116, 166–170 | H-39 |
| apps/panic.cpp | 3–15, 158–181, 224–225, 313–314, 347–364, 380–531 | H-05, H-40 |
| apps/preflight.cpp | 272–318 | H-06 |
| apps/bench_order.cpp | 75–77, 118–156 | H-03 |
| apps/fill_test.cpp | 47–48, 91–97 | H-04 |
| apps/live_e2e.cpp | 316–389 (+ file size 497 lines) | H-07 |
| apps/account_upgrade.cpp | 88 | H-08 |
| include/kalshi/full_chain_latency_probe.hpp | whole header (120 lines) | H-41 |
| tools/mm_backtest.py | 16–18, 47–48, 79–83, 112–151, 216, 297–300 | H-15..H-23 |
| config/backtest_latency.yaml | whole file | H-18 |
| tools/research/maker_edge_pilot.py | 137–190, 227 | H-24..H-27 |
| tools/research/build_segments.py | 49–73, 120, 156–159 | H-28 |
| tools/research/aggregate_maker_edge.py | 225–227 | H-43 |
| tools/research/html_report_maker_edge.py | 118 | H-43 |
| tools/warehouse.py | 2–11, 150 | H-33 |
| tools/account_view.py | 12–13, 146–149 | H-09 |
| tools/reconcile.py | 205–245 | H-09/H-14 |
| tools/pricing/{lo,fair,quote}.py | headers (1–5) | H-36 |
| src/strategies.cpp | 16–18 | empty-roster fact |
| config/market_classes.yaml | whole file | data capability §6 |
| work/** data layers | listings/manifest/schemas per DATA_CAPABILITY provenance column | data matrix |

## E. Registry census escalation record (§31.1: registry metadata inspected for ALL 144; escalation to source for phase-critical/high-risk)

All 144 registry entries: metadata inspected (complete tools.json read).
Escalated to source-level (list above): the 5 live_order entries + preflight
(--order mode) + live_e2e (unregistered) + execution/risk/foundation/
warehouse/research phase-critical set. Non-escalated entries carry L1/L3
evidence in the matrix with their conservative classification — none of
them received REUSE_AS_IS without executed-green tests (L3) or inspection.

## F. Explicitly NOT read (recorded so absence is visible)

- `~/.kalshi/env.sh` or any credential material (never read; forbidden).
- `outputs/` contents (release read-only; unowned).
- EC2 host state (external network false — all EC2-side facts are quoted
  from repo docs and marked NOT-MEASURED-HERE).
- Frozen prompts V2/V2.1 full bodies (historical, not authority; structure
  greps only).
