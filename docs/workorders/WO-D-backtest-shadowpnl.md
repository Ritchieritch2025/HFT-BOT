# WO-D — Engine replay backtest, shadow PnL, strategy roster (PLAN_PROD_V1 P9)

Branch: `wo-d-backtest` off `main`. Owner files: see WO-INDEX ownership map.
Read first: `.attic/docs/PLAN_PROD_V1.md` §P9, `include/trading/storage.hpp`
(`ReplaySource`), `src/gateway.cpp` (`KalshiRawDecoder`), `tests/test_replay.cpp`
(replay-equivalence pattern), `include/kalshi/strategy.hpp`, `src/strategies.cpp`
(returns `{}` — yours to fix), `include/trading/test_doubles.hpp`.

## Context — audit deltas (2026-07-18)

- C++ replay infra is real and tested (byte-exact NDJSON, gap-marker-aware,
  book-checksum equivalence) but there is NO fill simulation and NO PnL anywhere
  in C++. The only backtester is Python `mm_backtest.py` on a research worktree.
- WO-E is hardening the *research* fill model in parallel (fees, queue bounds,
  settlement). Your fill model should mirror its **pessimistic** semantics so the
  two harnesses can cross-check on the same tape. Coordinate constants, don't
  duplicate research logic: your job is the *engine-grade* harness real strategies
  run inside; WO-E's is candidate discovery.

## Tasks

1. **Roster config**: `make_strategies()` reads `STRATEGIES` env (comma list) or a
   JSON config path; unknown name = startup failure (fail closed). Static factory
   registry in `src/strategies.cpp`; each strategy gets a fixed slot id (0..29)
   declared at registration — `client_order_id` determinism depends on stable ids;
   document this. Ship one trivial registered strategy (`noop_probe`: quotes
   nothing, counts events) so the roster path is exercised end-to-end.
2. **Shadow PnL accountant** (`include/trading/shadow_pnl.hpp`, pure + testable):
   consumes would-be orders + subsequent market data.
   - Fill model (pessimistic, mirrors WO-E): post-only limit fills only when the
     tape trades STRICTLY THROUGH the price; optional optimistic variant (trade AT
     price) reported side-by-side, never headline.
   - Partial fills capped by printed trade size; marks to top-of-book;
     all fixedpoint.
   - Emits per-strategy `strategy` NDJSON events (`triggers`,
     `edge_signal_cents`, `shadow_pnl_cents`) via the telemetry thread — the
     dashboard Strategy panel already renders these fields; match its parser.
3. **`apps/backtest.cpp`**: `ReplaySource(capture.ndjson)` → `KalshiRawDecoder` →
   `OrderBookManager` → strategies → `MockExecutionEngine` + shadow PnL.
   - Deterministic: same tape + roster → identical PnL; assert via checksum.
   - Virtual clock from `recv_mono_ns` deltas for ttl semantics;
     as-fast-as-possible replay.
   - Gap/loss markers on the tape → conservative handling (invalidate book, cancel
     resting sim orders, count blind-spot fills as misses) — never optimistic
     through a blind spot.
   - CLI: tape path, roster, out path for a machine-readable result NDJSON
     (per-strategy PnL, fills, triggers, checksum). Registry entry safety class
     `offline`.
4. **Latency realism**: apply a configurable quote-activation delay
   (`BACKTEST_LATENCY_US`, default from measured LATENCY_FACTS when WO-E's
   calibration lands; conservative placeholder until then, clearly labeled).

## Tests (new)

Fill-model units (trade-through fills; no fill when price never crossed; partial
fills by size; blind-spot conservatism); backtest determinism (fixture tape →
exact expected PnL cents, run twice → identical checksum); roster tests (unknown
name refused; slot collision refused). All wired into `run_pipeline.sh`.

## Acceptance gate

`backtest` on a recorded `ws_shadow` tape reproduces identical results across two
runs; shadow-mode tradingd against the mock exchange shows strategy + PnL panels
live on the dashboard; suite + sanitizers green; registry/build systems updated.
Strategy *alpha* itself is out of scope — this WO delivers the harness the first
real strategy must pass through (positive fee-after pessimistic expectancy on
recorded prod tapes + clean shadow run).
