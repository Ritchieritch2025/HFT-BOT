# WO-A — Wire the WS market-data engine into tradingd (PLAN_PROD_V1 P6)

Branch: `wo-a-ws-feed` off `main`. Owner files: see WO-INDEX ownership map.
Read first: `.attic/docs/PLAN_PROD_V1.md` §P6 (authoritative task list — this WO adds
audit deltas only), `docs/PLAN_WS_V2.md`, `apps/feed.hpp`, `src/ws_client.cpp`,
`include/kalshi/{orderbook,sid_stream,recovery,ws_recorder,ring}.hpp`.

## Context (audited 2026-07-18)

The WS v2 engine is complete and shadow-tested (per-sid sequencing, recovery ladder,
durable recorder, `ws_shadow` harness) but tradingd still runs a ~250 ms REST poll
(`feed::run_poll` in `apps/feed.hpp`). This WO swaps the read side only; the submit
path is untouched (that's WO-B/C territory — do not edit `process_order`).

## Tasks

1. `apps/ws_feed.hpp` — `feed::run_ws(rt, client, tickers, on_event)` mirroring
   `run_poll`'s blocking shape. Internals: `IxWebSocketTransport` + `KalshiWsClient`
   + `OrderBookManager` + optional `WsRecorder` (`TRADINGD_WS_CAPTURE`, default off).
   - Transport thread: decode + book apply → top-of-book CHANGES only →
     `wire::MarketEvent` (kind=ticker, e4→cents via fixedpoint, never float) →
     SPSC `kalshi::Ring<MarketEvent>` (`TRADINGD_WS_RING`, default 4096,
     overflow = drop+count, never block).
   - Dispatch (main) thread pops and invokes the handler — strategies stay off the
     socket thread.
   - Only VALID books emit; invalid/resyncing books are silent.
   - `RecoveryLadder` wired with client builders; ping-silence watchdog on dispatch.
2. `tradingd --ws` mode: tickers from `KALSHI_WS_TICKERS` (explicit list; dynamic
   discovery out of scope). Fallback to `--poll` if WS URL unresolvable.
   `resolve_runtime()` already validates the WS URL — do not add a second validator.
3. Staleness stamps: `received_steady_ns` at frame receive (recorder already stamps
   once — reuse, don't double-stamp), `parsed_steady_ns` post-book-apply, so the
   latency probe compares REST vs WS honestly.
4. Feed telemetry via the telemetry thread ONLY (guardrail 7): fixed-size
   `FeedStatus` struct (source id, connected, last-msg mono_ns, msg count,
   gap/reconnect/drop counters) pushed ≤1/s onto the telemetry ring; worker formats
   `{"type":"feed","source":"kalshi_ws",...}`. The dashboard Feed panel currently
   renders nothing because no producer exists — this closes that.

## Tests (new)

- `tests/test_ws_feed.cpp` vs `MockWebSocketTransport`: snapshot+delta → exactly one
  MarketEvent per top-of-book CHANGE (not per delta); invalid book emits nothing
  until re-validated; ring overflow drops+counts; epoch bump mid-stream produces no
  stale events.
- TSan target for the transport/dispatch handoff.
- Extend `tests/run_ws_smoke.sh`: tradingd --ws ~2 s against `mock_ws_exchange.py`,
  assert events flowed. Extend the mock with a scripted gap; assert ladder recovery
  with no strategy-visible corruption.

## Acceptance gate

`make tsan` clean on the handoff; mock-driven gap soak recovers per the ladder;
registry + both build systems updated; `run_pipeline.sh` green. Prod read-only
staleness measurement (≪250 ms) is deferred to the P5 verification pass — record a
TODO in the rulebook, do not run prod from this WO.
