# WO-B — RiskGate, kill switch, order-lane accounting (PLAN_PROD_V1 P7 / TOKEN_RULES T8)

Branch: `wo-b-risk` off `main`. Owner files: see WO-INDEX ownership map.
Read first: `.attic/docs/PLAN_PROD_V1.md` §P7, `docs/PLAN_TOKEN_RULES.md` §T8,
`apps/tradingd.cpp` (submit side: `process_order`, gates, `TelemetryRecord`),
`src/request_executor.cpp`, `include/kalshi/token_bucket.hpp`.

## Context — audit deltas you must know (2026-07-18)

- The real order path (`tradingd.cpp:294 process_order`) bypasses `RequestExecutor`
  entirely; its own rate cap `TRADINGD_MAX_ORDERS_PER_SEC` **defaults to 0 = OFF**
  (`tradingd.cpp:570`). A live session today would run uncapped.
- The dashboard already renders a kill-switch badge and exposure fields
  (`dashboard_server.py:506,534`) fed by a `risk`-type NDJSON record **that nothing
  produces**. Your NDJSON schema must match what the dashboard parses — read its
  parser before defining fields.
- `tradingd.cpp:242` labels a latency stage `RiskDone` though no risk check runs —
  after this WO that label becomes true.
- No position state exists anywhere; this WO introduces the first (intent-side)
  position tracking. Exchange-truth reconciliation is WO-C — keep the seam clean.

## Tasks

1. **Startup self-config**: in tradingd, build `RestApi` on the existing client,
   fetch `account_limits()` + `endpoint_costs()`, configure a shared
   `RequestExecutor`. Live-mode preconditions (fail closed at startup): runtime
   valid, limits from_server (explicit conservative opt-in allowed for
   `local_mock`), cost table loaded, Write bucket initialized.
2. **Write-bucket accounting on the lane path**: `process_order()` does a
   non-blocking `try_reserve(cost(POST /portfolio/events/orders))` before
   sign+send. Refusal = drop with telemetry kind `RateLimited` (same semantics as
   today's bucket drop). Keep sign+`Lane::send` mechanics untouched — full
   send-through-executor unification is WO-C/P8. Add the grep gate: no `Lane::send`
   call sites outside `process_order`/`Lane::ping`.
3. **Rate-cap policy**: fold `TRADINGD_MAX_ORDERS_PER_SEC` into bucket policy —
   if both set, stricter applies. **Change the default from 0/off to a conservative
   floor (ASSUMPTION: 5/s) — off must become an explicit opt-out**, documented in
   README + `deploy/tradingd.env.example`.
4. **`include/trading/risk.hpp` — `RiskGate`** (pure, unit-testable, no I/O):
   - Caps: per-market position (`RISK_MAX_POSITION_PER_MARKET`), open orders
     (`RISK_MAX_OPEN_ORDERS`), global notional (`RISK_MAX_NOTIONAL_CENTS`),
     per-strategy order budget. All fixedpoint, no floats.
   - Decision enum {Allow, RejectStale, RejectDuplicate, RejectRisk, RejectKilled};
     duplicate suppression by `client_order_id`.
   - Position book (intent-side this phase): updated from own
     submissions/acks; periodic `/portfolio/positions` poll on the **telemetry
     thread** reconciles counts (ASSUMPTION until WO-C's fill channel).
5. **Kill switch**: `RISK_KILL_FILE` (existence = engaged) + SIGUSR1 toggle.
   NO filesystem checks on the submit path: a control/telemetry-thread watcher
   (~100 ms cadence) and the signal handler both write one `std::atomic<bool>`;
   submit workers do a relaxed load per intent. Engage latency ≤ watcher cadence —
   document in the runbook. Optional `RISK_KILL_CANCELS_OPEN=1` is deferred to
   WO-C (needs cancel support) — leave the hook.
6. **Telemetry**: every rejection emits a `risk` NDJSON event via the telemetry
   thread, with fields matching the dashboard's existing Risk panel; emit a
   periodic `risk` heartbeat carrying kill-switch state + current exposure so the
   badge goes live.
7. **`KalshiExecutionEngine` seam**: `set_risk_gate(...)` so both paths share one
   policy object (WO-C consumes this — land the header early and ping WO-C's agent).

## Tests (new)

- `tests/test_risk.cpp`: all caps, kill file, SIGUSR1, duplicate suppression,
  notional math at fixedpoint edges.
- tradingd integration: bucket-refused intent → `RateLimited` telemetry and **zero
  HTTP** (mock server counter, reuse `test_request_executor` technique).
- Knob interaction: env cap 5/s + bucket 100/s → 5/s governs.
- Kill-switch latency: touch file → next intent rejected (bounded by cadence).

## Acceptance gate

`make check && make san && make tsan` green; shadow-mode run against mocks shows
limits pulled from (mock) server, risk events on the dashboard, kill switch
verified (touch file → intents rejected within one intent). Registry + both build
systems updated.
