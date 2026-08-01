# Work Orders — parallel track to first live dollar (2026-07-18)

Audit basis: full-repo audit 2026-07-18 (system map, execution/safeguards, backtesting,
in-flight branch survey). Master roadmap: `.attic/docs/PLAN_PROD_V1.md` (P0–P2 done;
restore it to `docs/` — WO-F does this). Companion docs: `docs/ARCHITECTURE.md`,
`docs/PLAN_TOKEN_RULES.md` (T7/T8/T9 open).

## The finding in one paragraph

~35 worktrees / 11 workstreams are active, and essentially ALL of them are on the
data/research side (deep03, seals, RFQ, receipts, cache sync). Nobody is building the
execution engine, the risk layer, or the engine-level backtest. Order *transmission*
is production-grade at the socket level (`apps/tradingd.cpp` lanes, RSA-PSS, warm
HTTP/2), but: strategy roster is empty, the live bus engine throws, there is NO
position ledger, NO fill handling, NO order state machine, NO kill switch (the
dashboard badge has no producer), NO position/notional/loss caps, the order lane
bypasses the token-accounted `RequestExecutor`, and its own rate cap defaults to OFF.
The Python backtester (`mm_backtest.py`, worktree-only) is honest but Phase-1: no
fees, placeholder latency p99s, no queue/partial-fill model, no settlement ledger.

## Work orders

| WO | Title | Roadmap | Depends on | Agent profile |
|----|-------|---------|-----------|---------------|
| A | WS feed into tradingd | P6 | none | C++ eng |
| B | RiskGate + kill switch + order-lane accounting | P7 / T8 | none | C++ eng |
| C | OrderSubmitter + order state machine + positions + reconcile | P8 | B (gate seam) | C++ eng |
| D | Engine replay backtest + shadow PnL + strategy roster | P9 | none (mocks) | C++ eng |
| E | Research backtest hardening: W-FS1 fills + fees + latency + settlement | deep03 D3-W4 | none | Python quant |
| F | Platform: CI + spec-drift + doc/plan consolidation | P3/P4 | none | infra |

A, B, D, E, F start **immediately in parallel**. C starts once B's `RiskGate` header
lands (it only needs the seam, not the full phase).

## File-ownership map (no two agents touch the same file)

- **WO-A owns**: `apps/ws_feed.hpp` (new), `apps/tradingd.cpp` *feed/dispatch section
  only*, `tests/test_ws_feed.cpp`, `tests/mock_ws_exchange.py` (extend).
- **WO-B owns**: `include/trading/risk.hpp` (new), `src/risk.cpp` (new),
  `apps/tradingd.cpp` *submit/gate section only*, `tests/test_risk.cpp`.
- **WO-C owns**: `include/kalshi/order_submitter.hpp` (new), `src/order_submitter.cpp`
  (new), `src/gateway.cpp` (Live arm), `tests/mock_rest.py` (order scenarios),
  `tests/test_order_path.cpp`.
- **WO-D owns**: `apps/backtest.cpp` (new), `include/trading/shadow_pnl.hpp` (new),
  `src/strategies.cpp`, `tests/test_shadow_pnl.cpp`, `tests/test_backtest.cpp`.
- **WO-E owns**: Python only, on the research worktree lineage (`sandbox/wa-dev`
  branch `w-a-seal-staging-loop` or its successor) — `tools/fill_sim.py` (new),
  `tools/fees.py` (new), `tools/mm_backtest.py`, `config/backtest_latency.yaml`,
  `tests/test_fill_sim.py`.
- **WO-F owns**: `.github/workflows/`, `docs/` (moves/new docs), `tools/check_spec_drift.sh`,
  `docs/vendor/kalshi/`. Touches no C++/engine code.
- **A and B both edit `apps/tradingd.cpp`** in disjoint regions (feed loop vs
  `process_order`). Sequence merges: whoever finishes second rebases; conflicts are
  mechanical.

## Shared guardrails (verbatim from PLAN_PROD_V1 — binding on every WO)

1. Ops console can never place an order (server-side 403 stays).
2. No new third-party deps (C++ or Python).
3. No floats on price/count paths — `trading::fixedpoint` only.
4. Every WO ends green: `make && make check && make san` (+ `make tsan` where
   threads are touched) and `./tests/run_pipeline.sh`.
5. New binaries/tests go into Makefile AND CMakeLists.txt AND `tools.json`
   (registry check enforces).
6. Never log secrets; extend `tools/check_gates.sh`, never weaken it.
7. Observability never touches the hot path: fixed-size structs → bounded rings →
   telemetry thread formats/writes. No file/JSON/network work in feed, strategy,
   or submit-critical code.
8. Fail closed on ambiguity, everywhere.

## Do-not-collide list (other live agents' territory)

Do not touch: `w-deep03-*` / `codex/deep03-*` worktrees, `w-pub-ref-*`,
`w-research-inbox-*`, `w-w09-*`, RFQ branches, seal/staging loop (`w-a-seal-staging-loop`
is WO-E's base — coordinate before rebasing it), `pipe-w05-ui-data-root` UI work.
WO-E extends the research tree; it must land as a new branch off the current
wa-dev tip, not rewrite it.

## Sequencing to "money printer"

1. Parallel wave 1: A, B, D, E, F.
2. Wave 2: C (after B's seam), then P5 empirical verification (read-only prod
   probes — cheap, can interleave).
3. Wave 3: first real strategy behind D's harness, validated by E's
   fee-after pessimistic bound → P10 canary ladder (1-lot, post-only, caps floored).
No live order until: B's kill switch verified + C's reconcile proven against the
mock + D/E agree on a positive fee-after pessimistic PnL for the candidate strategy.
