# WO-C — OrderSubmitter, order state machine, positions, reconcile (PLAN_PROD_V1 P8)

Branch: `wo-c-submitter` off `main` (rebase on WO-B once its `risk.hpp` seam lands —
you need only the header to start; build everything else against mocks meanwhile).
Read first: `.attic/docs/PLAN_PROD_V1.md` §P8, `src/gateway.cpp:174-210` (Live arm
throws — that's yours), `apps/tradingd.cpp` `process_order`, `include/kalshi/wire.hpp`
(`order_json`, `client_order_id`, `kCreateOrderPath`), `include/kalshi/backoff.hpp`
(F9 matrix, `reconcile_required`).

## Context — audit deltas (2026-07-18)

- Today's engine is fire-and-forget: `TelemetryRecord` keeps HTTP status only.
  There is NO open-order registry, NO order lifecycle, NO fill consumption, NO
  position ledger from exchange truth. `RestApi::fills()/positions()` exist but
  return raw JSON that nothing consumes.
- Cancel exists only in probe tools (`bench_order.cpp:153`, `preflight.cpp`);
  amend is classified in `request_spec.hpp:49-50` but never sent.
- On ambiguous transport failure the executor flags `reconcile_required` and
  tradingd does no blind retry — correct, but nothing performs the reconcile;
  recovery is manual ("exit manually", `fill_test.cpp:149`).

## Tasks

1. **`kalshi::OrderSubmitter`** — extract the sign→send→classify-outcome core from
   `process_order()` into one shared component used by both tradingd lanes (keeping
   the dedicated warm-connection property) and `KalshiExecutionEngine::submit`
   (whose Live arm stops throwing and delegates). Gate order, unchanged semantics:
   ttl/age → rate/bucket → risk (WO-B seam) → `require_orders_allowed` → transmit.
2. **Order state machine**: per-intent lifecycle
   `Pending → Sent → Acked(order_id) | Rejected | PendingReconcile → Adopted | Dead`,
   keyed by deterministic `client_order_id`. Fixed-size open-order registry
   (bounded, fail-closed on full: refuse new intents, telemetry). Hot-path writes
   are struct/counter only; formatting on the telemetry thread (guardrail 7).
3. **Reconcile flow**: `RestApi::order_by_client_id(coid)` → GET
   `/portfolio/orders?client_order_id=` — verify the exact query param against the
   vendored OpenAPI (WO-F artifact) before coding. Ambiguous outcome (transport
   error / 5xx) → `PendingReconcile` + telemetry; reconcile runs on the
   telemetry/control thread: found → adopt order_id as submitted; not found after
   N attempts → `Dead`, ops-initiated resend only (never automatic).
4. **Fill/position truth**: poll `/portfolio/fills` (cursor-paged) on the control
   thread; parse into a position ledger (fixedpoint); push deltas to WO-B's
   RiskGate position book so caps act on exchange truth, not just intents.
   (WS fills channel, if any, is a later upgrade — keep the source seam.)
5. **Cancel**: `OrderSubmitter::cancel(order_id)` (DELETE, path already exercised
   by preflight/bench_order). Wire WO-B's deferred `RISK_KILL_CANCELS_OPEN=1`:
   kill-switch engage → cancel all open orders via the control thread.
6. **Mock**: extend `tests/mock_rest.py` order endpoint with scenario knobs — 201,
   429, 5xx-after-accept (returns 504 but records the order so reconcile finds it),
   timeout-past-client-deadline (records order). This proves no-duplicate-on-ambiguity.

## Tests (new)

`tests/test_order_path.cpp` vs the extended mock: happy place+cancel; ambiguous
timeout → exactly ONE order server-side and reconcile adopts it; 429 → drop, no
server order; risk-reject → no HTTP; shadow mode → zero HTTP ever (server counter
0); kill-engage → open orders canceled. Fill-poll parser unit tests (paged, dedup
by trade id). All in `run_pipeline.sh`.

## Acceptance gate

Full suite green incl. sanitizers. Deterministic no-duplicate proof against the
mock. NO live/prod orders from this WO — the P8 live validation (real 1-lot orders)
is a separate operator-approved session after WO-B+C+D land and the operator signs
off. Grep gate holds: no `Lane::send` outside the submitter/ping.
