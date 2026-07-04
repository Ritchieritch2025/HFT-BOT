# Kalshi WS Market-State Engine v2 — Claude Code Implementation Plan

> Drop this file into the repo root (suggested: `docs/PLAN_WS_V2.md`) and work it
> phase by phase. Every phase has an acceptance gate; do not start phase N+1
> until phase N's gate passes. All protocol facts below were verified against
> official Kalshi docs/OpenAPI/AsyncAPI on 2026-07-04 — cite lines from
> `docs/kalshi_ws_protocol.md` (created in Phase 0) in code comments, not URLs.

## Repo context (read first, do not re-derive)

- Build: `make` (clang++, C++23, vendored OpenSSL on macOS / system on Linux,
  libcurl). Tests: `make check` (pure), `make test` (signing), `make tsan`,
  `make san`. CMake mirror exists — keep `CMakeLists.txt` in sync when adding
  files.
- Existing components to EXTEND, never duplicate:
  - `include/trading/bus.hpp` — NormalizedEvent, EntityRegistry, DataSource/
    EventSink/ExecutionEngine. Source-agnostic: no Kalshi field names may enter
    `include/trading/`.
  - `include/trading/fixedpoint.hpp` — PriceE4 (dollars×10⁴, [0,10000]),
    CountFp (contracts×10²), strict integer parsers. NO floats for prices/counts
    anywhere, ever.
  - `include/kalshi/orderbook.hpp` — OrderBook/OrderBookManager (needs seq
    rework, Phase 0).
  - `include/kalshi/rest_api.hpp` — typed REST client, TokenBucket, retry.
  - `include/trading/storage.hpp` — NDJSON RawLogWriter/Reader, ReplaySource.
    KEEP NDJSON; do not introduce a binary log format in this pass.
  - `include/kalshi/gateway.hpp` — KalshiRawDecoder, mode-aware
    KalshiExecutionEngine (fail-closed live path — do not touch the order
    transmission path in this plan).
  - `include/kalshi/ring.hpp` — Vyukov MPMC ring (reusable for SPSC).
  - `tests/` — mock-exchange + mini_redis harness style to imitate.
- Known-bad code this plan deletes: `apps/feed.hpp` float parsing (`strtod`)
  and its legacy integer-field fallbacks (legacy fields were removed from the
  API on 2026-03-12 — the fallbacks are dead code).

## Hard guardrails (apply to every phase)

1. NEVER touch order transmission. `KalshiExecutionEngine` live path stays
   fail-closed. All new code is market-data only. Any diff touching
   `build_order_json`, submit lanes, or `require_orders_allowed` is out of scope.
2. All network testing against DEMO (`external-api.demo.kalshi.co` /
   `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`) until Phase 8.
3. No new third-party dependencies. WS client is hand-rolled RFC6455 over the
   existing OpenSSL, client role only, NO extensions (do not negotiate
   permessage-deflate), masking + fragmentation + control frames + close
   handshake only.
4. No floats on price/count paths. Wire strings go through
   `trading::fixedpoint` parsers; excess precision = reject + telemetry.
5. Hot path (WS read loop → book apply → NormalizedEvent publish) may not:
   block on I/O, call REST, take contended locks, allocate per-message beyond
   the freelist, or hash strings after ticker→EntityId interning.
6. Never implement behavior for undocumented protocol features. Specifically:
   WS error codes 26/27 DO NOT EXIST in official docs — unknown error codes go
   through the conservative path (log raw payload, treat as channel error,
   alert). Official code list: 1–22 (AsyncAPI) plus 25 (docs page + changelog).
7. Every phase ends green: `make && make check && make san`; `make tsan` where
   threads are touched. Add new tests to both Makefile and CMakeLists.

## Protocol invariants (the "why" behind the tasks; violating any = P0 bug)

| # | Invariant | Source |
|---|---|---|
| I1 | `seq` is per-sid (subscription stream), monotonic across ALL markets and ALSO consumed by sequenced control responses (`unsubscribed` requires sid+seq; `ok` may carry sid+seq). Gap check lives at sid level only. | AsyncAPI okResponse/unsubscribedResponse/sequenceNumber schemas |
| I2 | One subscription per channel per session. Repeated subscribe MERGES (same tickers = no action — and NO fresh snapshot; new tickers = added). "Resubscribe" as recovery must be unsubscribe→subscribe. | changelog 2025-09-25; error 6 semantics |
| I3 | A sid seq gap invalidates EVERY market on that sid. Re-validation is per-market via in-stream `orderbook_snapshot`. | I1 + orderbook-updates page |
| I4 | REST orderbook snapshots carry NO sequence field and must never re-seed a WS-live book. In-stream recovery only: `update_subscription {action:"get_snapshot", market_tickers:[...]}` (tickers-only param), then delete/add, then unsub/sub, then reconnect. | OpenAPI OrderbookCountFp; changelog 2026-04-20 |
| I5 | `use_yes_price` must be sent explicitly (currently `false` = no-leg pricing for no side). Kalshi will flip the default and later remove the flag. Decoder asserts the convention; migration must be a single constant. | AsyncAPI subscribe params; order_direction page |
| I6 | Kalshi pings every 10 s with body `"heartbeat"`; client must pong promptly; >~30 s ping silence = dead connection → reconnect. | connection-keep-alive page |
| I7 | Error 25 = server-side subscription buffer overflow = data ALREADY LOST: full-sid invalidate + in-stream resync + gap marker in raw log. | quick_start_websockets error table; changelog 2026-05-12 |
| I8 | sid values die with the connection. Every enqueued message carries a `stream_epoch`; consumers drop epoch mismatches. Post-reconnect, run `list_subscriptions` as a self-check. | AsyncAPI subscriptionId (server-generated); changelog 2025-09-25 |
| I9 | Only orderbook messages have seq. `ticker`: no seq, use `ts_ms` (ts/time deprecated). `trade`: no seq, dedupe by `trade_id`; direction fields migrating to `taker_outcome_side`/`taker_book_side` (`taker_side` deprecated). `market_lifecycle_v2`: no seq, no market filter, also emits `event_lifecycle`; event_type set is OPEN (types were added AND removed within 2026) — tolerate unknown types. | market-ticker/public-trades/market-&-event-lifecycle pages; changelog 2026-05-06, 2026-04-17 |
| I10 | WS auth: headers KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP (ms); sign `timestamp + "GET" + "/trade-api/ws/v2"` — no host, no query. Reuse client.cpp signing. | api_environments; quick_start_websockets |
| I11 | Rate limits are token-based (default 10/request), separate Read/Write buckets, no Retry-After on 429. Pull actual tier via `GET /account/api_limits` and costs via `GET /account/non-default-endpoint-costs` at startup instead of hardcoding. Batch orderbooks (`GET /markets/orderbooks?tickers=A&tickers=B`, form-explode, 1–100, auth required) token cost is UNDOCUMENTED — measure in demo, budget worst-case. | rate_limits page; OpenAPI |
| I12 | WS connections per user: tier-limited, default 200 — multi-connection sharding is officially supported. | changelog 2025-09-18 |

---

## Phase 0 — Protocol ground truth + schema + seq rework

**Goal**: invariants written down, specs vendored, seq semantics fixed, resync
made non-blocking. Everything else builds on this.

Tasks:
1. Create `docs/kalshi_ws_protocol.md` containing the invariant table above
   plus the exact message schemas (subscribe/unsubscribe/update_subscription/
   ok/subscribed/unsubscribed/error; orderbook_snapshot `yes_dollars_fp`/
   `no_dollars_fp`; delta `price_dollars`/`delta_fp`/`side`/`ts_ms`/optional
   `client_order_id`; ticker/trade/lifecycle field lists).
2. Vendor specs: download `https://docs.kalshi.com/openapi.yaml` and
   `https://docs.kalshi.com/asyncapi.yaml` into `third_party/kalshi_specs/`
   with a `FETCHED_AT` stamp file. Add `tools/check_spec_drift.sh` (fetch,
   diff, nonzero exit on change) — wire into CI if CI exists, else document
   manual cadence in the protocol doc.
3. Schema: add `std::optional<std::uint64_t> source_stream_id` (sid) and
   `std::uint32_t stream_epoch` to `trading::RawRecord` and
   `trading::NormalizedEvent`. NDJSON: absent fields = legacy line (reader
   must keep accepting old logs — extend `test_storage`).
4. Seq rework in `include/kalshi/orderbook.hpp`:
   - New `SidStream` (in a new `include/kalshi/sid_stream.hpp` or inside the
     manager): owns {sid, epoch, expected next seq, member EntityId set,
     per-market valid flags}. EVERY sequenced message on the sid (snapshot,
     delta, ok-with-seq, unsubscribed) advances the counter (I1).
   - `OrderBook::apply_delta` loses its internal `seq == last+1` gate (keep
     `last_seq_` as informational); gap detection moves to SidStream. Negative-
     level ⇒ Invalid stays as is.
   - Gap ⇒ invalidate all member markets (I3); expose which markets need
     snapshots.
5. Make recovery non-blocking: `ResyncHandler::fetch_validation` must no longer
   be called from `on_delta`. Replace with `request_resync(sid, tickers)` that
   enqueues onto a control queue (use `kalshi::ring`). REST cross-check becomes
   a control-thread-only compare-and-log (never load_snapshot from REST while
   WS-live — I4).

New tests (extend `tests/test_orderbook.cpp` or add `tests/test_sid_stream.cpp`):
- multi-market interleaved deltas on one sid, per-sid contiguous seq ⇒ zero
  false resyncs;
- sequenced `ok`/`unsubscribed` interleaved into the stream ⇒ no false gap;
- true gap ⇒ all sid members invalid; snapshot re-validates only its market;
- invalid book: accessors nullopt, deltas dropped and counted.

Gate: `make check` green incl. new tests; grep shows no call sites of
`fetch_validation` on any sink/hot path.

## Phase 1 — Decoder + normalized-schema completion (still no sockets)

Tasks:
1. `apps/feed.hpp`: delete `strtod` price/count lambdas and legacy int
   fallbacks; parse `_dollars`/`_fp` strings via `trading::fixedpoint`. Keep
   REST poll functional behind existing flags (it remains the fallback feed).
2. `KalshiRawDecoder` (gateway.cpp): add decode for `trade`
   (`trade_id`, `yes_price_dollars`, `no_price_dollars`, `count_fp`,
   prefer `taker_outcome_side`/`taker_book_side`, fallback deprecated
   `taker_side`; `ts_ms` preferred), `market_lifecycle_v2` (open event_type
   set per I9, incl. `determined` + `settlement_value`, `settled`,
   `price_level_structure_updated` with `linear_cent`/`deci_cent`/
   `tapered_deci_cent`, `metadata_updated`), `event_lifecycle`, and
   `client_order_id` passthrough on deltas.
3. Extend `trading::Lifecycle` variant payload to the full v2 event set +
   an `Unknown{string}` alternative (unknown types must round-trip to raw log
   and count, not crash).
4. Ticker decode: `ts_ms` only; treat `ts`/`time` as absent-legacy.

New tests: golden-message unit tests per type (copy exact JSON examples out of
the protocol doc); unknown lifecycle event_type tolerated; unknown error code
tolerated; excess-precision price rejected.

Gate: `make check && make san` green; `grep -rn "strtod" apps/ src/` empty.

## Phase 2 — WS transport (`include/kalshi/ws.hpp`, `src/ws.cpp`)

Tasks:
1. RFC6455 client per guardrail 3. TLS via existing OpenSSL (mirror the RESP
   client's from-scratch ethos). `TCP_NODELAY`. Single large reusable read
   buffer; in-place frame parse; handle control frames interleaved between
   fragments; reject fragmented control frames (RFC).
2. Auth handshake per I10 reusing `client.cpp` RSA-PSS signing. On 401,
   surface server-date skew hint (clock-discipline parity with README).
3. Keep-alive per I6: pong echoes `"heartbeat"` payload from the read loop
   with a deadline; ping-silence watchdog (30 s) triggers reconnect.
4. Single-writer outbound: all frames (pongs, commands) go through one SPSC
   command ring drained by one writer; no direct writes from other threads.
5. Reconnect: capped exponential backoff WITH jitter; increments
   `stream_epoch`; drops all state keyed to old sids (I8); resubscribes;
   issues `list_subscriptions` and asserts the reply matches intent.
6. Subscription manager: client command `id` counter (start 1, increment);
   correlation table with timeouts (error 18 exists); handles `subscribed`/
   `ok`/`error`; error 6 ⇒ assert (should be unreachable given I2-aware
   logic); error 25 ⇒ I7 flow; unknown code ⇒ conservative path (guardrail 6).
7. Connection layout (I12): conn A `orderbook_delta` only (explicit
   `market_tickers` always; `use_yes_price: false` explicitly — I5); conn B
   `ticker`+`trade` (explicit tickers; optional `send_initial_snapshot`);
   conn C `market_lifecycle_v2` (global, unfiltered by design). Sharding a 2nd
   orderbook connection is OUT of scope until triggered (error 25 / read-loop
   saturation / p99 budget breach) — leave a seam, don't build it.

New tests: `tests/test_ws_frames.cpp` (pure): frame encode/decode, 16/64-bit
lengths, masking, fragmentation reassembly, interleaved ping, fragmented
control frame rejected, close handshake. `tests/mock_ws_exchange` (harness in
the style of the existing mock exchange): auth header check, subscribe→
subscribed→snapshot→deltas script, heartbeat ping expecting timely pong,
scripted error injection.

Gate: `make check` + new frame tests green; `make tsan` clean on a
read-loop/writer/control 3-thread soak against the mock; demo-env manual smoke:
connect, subscribe 2 markets, receive snapshot+deltas, survive ≥5 min
(≥30 heartbeats), clean close.

## Phase 3 — State application + recovery ladder

Tasks:
1. Wire conn A frames → decoder → SidStream/OrderBookManager → NormalizedEvents
   (with sid+epoch stamped) on the bus.
2. Decoder asserts pricing convention per I5: one constant
   `kOrderbookNoSideLeg` used by both subscribe-builder and decoder; flipping
   it to yes-leg must be a 1-line change + tests.
3. Recovery ladder exactly I4, escalation on timeout at each rung; while a
   market is invalid: drop its deltas, count drops, hold model/signal (bus
   consumers see validity via book accessors refusing + an explicit resync
   NormalizedEvent…use existing Lifecycle/Unknown or add a `StreamStatus`
   variant alternative if needed — keep it venue-neutral).
4. Lifecycle wiring: `determined`/`settled` ⇒ enqueue `delete_markets` for
   conn A + free book state; `created` matching tracked series ⇒ catalog insert
   (+ optional auto-add behind config).
5. Execution-side hard gate: `KalshiExecutionEngine::submit` rejects intents
   for entities whose book is invalid/stale (belt-and-braces on top of model
   discipline). This is a read-only check — does not touch transmission.

New tests: mock-WS integration: snapshot→deltas→gap→get_snapshot recovery;
get_snapshot arrives while later deltas continue (drop-then-resume correct);
unsub/resub rung (verify fresh snapshot only after unsubscribe, per I2);
REST-poisoning guard: assert no code path calls `load_snapshot` from any
`RestApi` result while sid-live (make it a compile-time seam if feasible:
the WS book loader takes a `SnapshotView` tagged with sid+seq origin).

Gate: full mock-driven soak: scripted 10k-message session with 3 injected gaps
and 1 injected error-25 recovers to a book byte-identical to a straight-line
replay of the same script; `make tsan` clean.

## Phase 4 — Recorder integration

Tasks:
1. Freelist/slab of frame buffers; read loop stamps `recv_mono/wall_ns` ONCE
   per TCP read, copies frame into an owned buffer, enqueues
   {epoch, sid, channel, ticker, seq?, times, buffer} on an SPSC ring to the
   writer thread. Overflow: drop + increment counter + write a loss marker
   record when the queue drains — NEVER block the read loop.
2. Writer thread: existing `RawLogWriter`. Add first-class marker records
   (NDJSON lines with a `marker` type): `gap`, `resync_begin/end`, `loss`,
   `epoch_change`. Reader/replay must surface them.
3. Record on ALL connections (A/B/C), one log stream per connection or a
   tagged shared stream — pick per existing RawLogWriter rotation semantics and
   document.

New tests: recorder overflow under load with ASan (no UAF, loss marker
present); byte-exactness round-trip incl. a non-UTF8 payload (raw_b64 path);
marker records survive writer crash-truncation (existing truncated-line
tolerance).

Gate: `make san` + ASan recorder test green; sustained mock firehose shows
zero read-loop stalls attributable to recorder (measure with existing
EventTiming-style probes).

## Phase 5 — REST catalog completion (control threads only)

Tasks:
1. Extend `RestApi`: series list/get, events list/get, event metadata, market
   candlesticks (incl. batch), trades, batch orderbooks
   (`?tickers=A&tickers=B` form-explode, ≤100, auth — I11), historical/* as
   read-only research stubs.
2. Startup self-configuration: fetch `GET /account/api_limits` +
   `GET /account/non-default-endpoint-costs`; initialize TokenBuckets from the
   response (fallback to conservative defaults on failure).
3. Measure batch-orderbooks token cost empirically in demo (script under
   `tools/`), record the finding in the protocol doc, set budget worst-case
   until proven otherwise.
4. Revised startup flow: mode → catalog → registry → WS connect+subscribe →
   books valid on WS snapshots. REST batch snapshot only for display/
   cross-check (flagged not-tradeable) and never fed to live books (I4).

Gate: catalog bootstrap against demo for a chosen series set; pagination
exercised past one cursor page; token spend stays within pulled limits.

## Phase 6 — Replay equivalence

Tasks:
1. `ReplaySource`: epoch/sid aware; replays marker records as invalidations so
   replayed state honestly mirrors live blind spots.
2. Periodic live book checksums (per market: fold of sorted level array +
   last applied per-sid seq) written as marker records; replay recomputes and
   compares.

New tests: replay a recorded mock session across an epoch boundary ⇒ identical
final books + identical checksum trail; legacy logs (pre-sid/epoch fields)
still replay (fields absent ⇒ single-epoch assumption).

Gate: recorded demo session (≥1 reconnect forced) replays checksum-identical.

## Phase 7 — Hardening + perf (benchmark-gated)

Tasks:
1. Frame fuzzer over the RFC6455 parser (ASan/UBSan, seed corpus from mock
   scripts). TSan soak of the full 3-connection engine against mock.
2. Error-25 chaos drill in demo: over-subscribe conn B, throttle reads, verify
   I7 flow end-to-end.
3. `bench_orderbook` WS-path variant (frame bytes → book apply, p50/p99).
   ONLY if numbers justify: flat-array book (price grid ≤10000 slots per side)
   behind the same OrderBook interface; ticker interning table built from the
   `ok` ack market list. Re-run bench; keep whichever wins.

Gate: sanitizers + fuzzer clean; bench results recorded in the protocol doc.

## Phase 8 — Read-only production shadow smoke

Tasks: run the full engine in Shadow/DataCollect against production for a
bounded session; assertions: `KalshiExecutionEngine` transmitted == 0; zero
Write-bucket token spend for the entire session; recorder completeness report
(gap/loss markers) at exit; books cross-checked against REST batch snapshots
on the control thread (compare-only) with divergence telemetry.

Gate: ≥24 h soak, zero missed-pong disconnects, zero cross-epoch applications,
divergence within expected REST-staleness bounds.

---

## Open items to verify empirically (do not assume)

1. Batch orderbooks token cost (per request vs per ticker) — Phase 5 script.
2. Whether `update_subscription get_snapshot` seq-stamps the snapshot with the
   next sid seq or re-baselines — confirm against demo before finalizing
   SidStream accounting for it (changelog says it does not disturb the delta
   stream; verify the seq value observed).
3. `use_yes_price` default-flip date — watch changelog; the flip must be
   caught by the spec-drift check (Phase 0) before it ships.
4. Exact current WS error-code list beyond 22/25 — the AsyncAPI enum (≤22) is
   known-stale vs docs (25); conservative path covers surprises either way.
