# kalshi-cpp — minimal C++23 Kalshi HFT stack

A lean, dependency-light client for the [Kalshi Trade API v2](https://docs.kalshi.com)
plus a single-process trading engine built on it.

## Architecture

```
                 ┌────────────────────── tradingd (one process) ──────────────────────┐
 Kalshi ────────▶│ hot thread: feed (REST poll | WS later) → MarketEvent               │
 (market data)   │   → configured strategies inline (throwing slots quarantined)       │
                 │   → ExecPayload (80B POD, passed as a function argument)            │
                 │   → lock-free ring (~ns)                                            │
                 │ submit lanes (default 2): each owns a DEDICATED warm HTTP/2 TLS     │
                 │   connection — no shared socket, no pool mutex, workers can never   │
                 │   block each other. pop → ttl/age/rate gates → sign (RSA-PSS,       │
                 │   ~300µs) → lane.send() ────────────────────────────────────────────┼─▶ Kalshi (orders)
                 │   idle lanes ping /exchange/status so orders never pay a handshake  │
                 │ telemetry thread: ring → Redis exec:results (COLD path; drops if    │
                 │   Redis is down — trading unaffected) ──────────────────────────────┼─▶ Redis (optional)
                 └──────────────────────────────────────────────────────────────────────┘
```

The order path never leaves the process: market data → decision → signed order
happens in one address space, the packed `ExecPayload` crossing threads through
a from-scratch Vyukov MPMC ring (`include/kalshi/ring.hpp`). Redis appears only
on the cold path (async telemetry, and the optional `ingestd` market-tape
recorder for research/replay) via a from-scratch RESP2 client — no hiredis.

### Measured (dev box, loopback mock exchange)
- decision → worker pop (in-process handoff): **p50 7µs** (was 88µs when orders
  relayed through a Redis list; spin mode `TRADINGD_SPIN=1` goes lower)
- RSA-PSS sign: ~300µs/core (mandated by Kalshi's auth; parallelizes across lanes)
- honest context: exchange RTT is 10–40ms and REST-poll staleness is ~250ms mean —
  the next real latency win is a WebSocket feed, not micro-tuning this path.

## Layout

```
include/kalshi/client.hpp    KalshiClient: signed transport; SignedRequest split
                             (sign_request/send_signed) + per-worker Lane API
include/kalshi/ring.hpp      bounded lock-free MPMC ring (Vyukov)
include/kalshi/wire.hpp      ExecPayload / MarketEvent PODs, validation, order JSON
include/kalshi/resp.hpp      from-scratch RESP2 (Redis) client — cold path
include/kalshi/strategy.hpp  IStrategy + ExecSink
src/                         implementations
apps/tradingd.cpp            THE ENGINE (hot thread + lanes + telemetry)
apps/feed.hpp                feed sources: live REST poll
apps/ingestd.cpp             optional market-tape recorder (cold path)
examples/kalshi_example.cpp  status + balance walkthrough with simdjson
tests/                       signing/RESP/ring/integration tests, mock exchange,
                             mini_redis stand-in
third_party/simdjson/        simdjson 4.6.4 amalgamation
third_party/openssl/         vendored static OpenSSL 3.5.7 (macOS arm64 build)
```

## Build & test

```sh
make            # engine + tools + tests (vendored OpenSSL on macOS; system on Linux)
make test       # signing self-test
make tsan       # ThreadSanitizer: ring hammer + full engine
```

CMake works too (`cmake -B build-cmake && cmake --build build-cmake`). Requires a
C++23 compiler (GCC 12+/Clang 16+, enforced at compile time), libcurl, OpenSSL 3.x.

## Running

```sh
export KALSHI_API_KEY_ID=...            # from kalshi.com account settings
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/private_key.pem   # unencrypted RSA PEM
./build/tradingd --poll 500             # live REST-poll feed
```

Knobs (env): `TRADINGD_WORKERS=2` (dedicated connections), `TRADINGD_SPIN=0`,
`TRADINGD_RING=1024`, `TRADINGD_MAX_QUEUE_AGE_MS=2000`,
`TRADINGD_MAX_ORDERS_PER_SEC=0` (token bucket, 0=off), `TRADINGD_KEEPALIVE_S=15`,
`TRADINGD_DRAIN_MS=2000`, `REDIS_HOST/REDIS_PORT` (telemetry, optional),
`KALSHI_BASE_URL` (demo/testing).

Strategies live in `src/strategies.cpp`. The default roster is empty; install
real strategy code before expecting the engine to emit orders.

## Production notes (HFT)

- **Warm paths.** Each lane pings `/exchange/status` when idle (default 15s,
  staggered) so a real order never pays the 10–80ms TCP+TLS handshake — with
  sparse orders this is the largest controllable latency item on the path.
- **Idempotency.** `client_order_id` is derived deterministically from
  (decision time ns, strategy id, seq). A duplicate submission of the same
  intent maps to the same id and dedupes at the exchange. No blind transport
  retries: a timed-out POST may still have executed.
- **Staleness.** Orders carry `ttl_ns`; the engine additionally enforces
  `TRADINGD_MAX_QUEUE_AGE_MS` on a monotonic clock (NTP steps can't expire or
  immortalize queued orders).
- **Rate limits.** Kalshi caps orders/second by tier — set
  `TRADINGD_MAX_ORDERS_PER_SEC` to your tier; over-limit intents are dropped
  with telemetry, not queued into staleness.
- **Clock discipline.** Signatures embed a millisecond wall-clock timestamp;
  run NTP/chrony. Persistent 401 streaks + a skewed `Response::server_date_ms`
  = your clock.
- **Linux deploy.** Pin lanes/hot thread to performance cores (macOS scheduling
  of background threads inflated sign latency ~4x in testing). The Makefile
  auto-selects system OpenSSL on Linux.
- **Next milestone.** A from-scratch WSS market-data client (OpenSSL over POSIX
  sockets, same ethos as the RESP client) — REST polling's ~250ms staleness is
  the dominant end-to-end latency item, worth ~2,800x more than the Redis-hop
  removal was.
