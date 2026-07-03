# kalshi-cpp — minimal C++23 Kalshi HFT stack

A lean, dependency-light client for the [Kalshi Trade API v2](https://docs.kalshi.com)
plus a three-layer trading pipeline built on it.

## Architecture

```
┌────────────┐  MarketEvent (88B)   ┌────────────┐  ExecPayload (80B)   ┌────────────┐
│  ingestd   │ ──PUBLISH md:events──▶│   stratd   │ ──LPUSH exec:orders──▶│   execd    │
│ Ingestion  │                      │ Processing │                      │ Execution  │
│ REST poll /│                      │ 30 strategy│                      │ N workers  │
│ synthetic  │        Redis         │   slots    │        Redis         │ BRPOP →    │
└────────────┘                      └────────────┘                      │ signed POST│
                                                                        └─────┬──────┘
                                                          exec:results ◀──────┤
                                                          (telemetry)         ▼
                                                                      api.elections.kalshi.com
```

The three daemons are isolated processes glued by Redis. Data formats are
strictly minimal: fixed-size packed structs (`include/kalshi/wire.hpp`) whose
bytes are the wire bytes — no serialization on the hot path. When any of the
30 strategies finds an edge it appends one uniform 80-byte `ExecPayload` onto
the `exec:orders` Redis list; `execd` consumes, staleness-gates (`ttl_ns`),
signs, and fires. Redis I/O uses a from-scratch RESP2 client
(`include/kalshi/resp.hpp`) — no hiredis dependency.

- **Transport** — native libcurl. A fixed pool of persistent easy handles, one
  keep-alive TLS connection each. Thread-safe: call `request()` from any thread;
  callers block only when all pooled connections are in flight.
- **Auth** — OpenSSL 3.x RSA-PSS request signatures per the Kalshi API-key spec
  (SHA-256, MGF1-SHA256, salt = digest length, base64), over
  `timestamp_ms + METHOD + path-without-query`.
- **JSON** — responses come back as raw bytes; parse with the vendored
  [simdjson](https://github.com/simdjson/simdjson) amalgamation
  (`third_party/simdjson`) or anything else. The client itself does not parse.

## Layout

```
include/kalshi/client.hpp    KalshiClient: pooled, signed REST transport
include/kalshi/wire.hpp      ExecPayload / MarketEvent wire formats + validation
include/kalshi/resp.hpp      from-scratch RESP2 (Redis) client
include/kalshi/strategy.hpp  IStrategy + ExecSink, the 30-slot roster
src/                         implementations (+ demo strategies)
apps/ingestd.cpp             Ingestion daemon  (REST poll or synthetic tape)
apps/stratd.cpp              Processing daemon (event dispatch -> 30 strategies)
apps/execd.cpp               Execution daemon  (BRPOP -> signed order POST)
examples/kalshi_example.cpp  status + balance walkthrough with simdjson
tests/test_signing.cpp       RSA-PSS roundtrip + openssl CLI cross-check
tests/test_integration.cpp   16-thread pool hammer vs tests/mock_server.py
tests/test_resp.cpp          RESP client + wire-format roundtrips
tests/run_pipeline.sh        end-to-end pipeline test (mini_redis + mock exchange)
third_party/simdjson/        simdjson 4.6.4 amalgamation
third_party/openssl/         vendored static OpenSSL 3.5.7 (macOS arm64 build)
```

## Running the pipeline

```sh
make                                   # builds daemons + tests
./tests/run_pipeline.sh                # full e2e against local stand-ins

# against real infra:
export REDIS_HOST=... REDIS_PORT=6379
export KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=...
./build/execd &                        # EXEC_WORKERS=2 EXEC_POOL=4 defaults
./build/stratd &
./build/ingestd --poll 500             # or --synthetic for a test tape
```

The demo strategies (slots 0–1) only react to `TEST-*` tickers, so the live
feed cannot trigger real orders until you install your own strategies in
`src/strategies.cpp`.

## Build

```sh
make            # library objects + example + tests (uses third_party/openssl)
make test       # signing self-test
make tsan       # ThreadSanitizer build of the integration test
```

CMake works too (`cmake -B build-cmake && cmake --build build-cmake`) and prefers
the vendored OpenSSL when present; on a Linux deploy host with system OpenSSL 3.x
and libcurl dev packages it uses those instead.

## Usage

```cpp
#include "kalshi/client.hpp"

kalshi::Config cfg;
cfg.api_key_id      = /* key id from kalshi.com account settings */;
cfg.private_key_pem = kalshi::read_file("/path/to/private_key.pem");
cfg.pool_size       = 4;                       // == max in-flight requests
kalshi::KalshiClient client(std::move(cfg));

client.warmup();  // establish TCP+TLS on every pooled connection up front

auto r = client.request(kalshi::Method::Get, "/portfolio/balance");
if (!r)          { /* transport/signing failure: r.error().message */ }
else if (r->ok()) { /* parse r->body with simdjson */ }
else              { /* HTTP error: r->status, JSON error in r->body */ }

// Orders: paths are relative to /trade-api/v2; query strings are fine
// (sent, but excluded from the signature automatically).
client.request(kalshi::Method::Post, "/portfolio/orders",
               R"({"ticker":"...","action":"buy","side":"yes","count":1,
                   "type":"limit","yes_price":50,"client_order_id":"..."})");
```

Demo environment: set `cfg.base_url = "https://demo-api.kalshi.co"` (check the
current demo host in Kalshi's docs — it has moved before).

## Production notes (HFT)

- **Warm up before trading.** `warmup()` pays the TCP+TLS setup cost once per
  pooled connection at startup instead of on your first order.
- **Idle timeouts.** Kalshi's edge closes idle connections after tens of
  seconds; libcurl transparently reconnects (with TLS session resumption), but
  that request eats a handshake. If you need consistently warm paths during
  quiet periods, tick a cheap `GET /exchange/status` periodically.
- **Clock discipline.** Signatures embed a millisecond timestamp; run NTP/chrony.
  Persistent skew shows up as 401s.
- **No automatic retries.** A timed-out order may still have reached the
  exchange — retry policy belongs in your trading logic (use `client_order_id`
  idempotency), not in the transport.
- **Pool sizing.** `pool_size` bounds concurrent in-flight requests; extra
  callers queue on a condition variable. Size it to your real concurrency, and
  mind Kalshi's rate-limit tier.
- **Signing cost.** RSA-2048 PSS signing costs roughly 100–200 µs per request on
  Apple Silicon — inherent to Kalshi's auth scheme and small next to network RTT.
- **Market data** belongs on Kalshi's WebSocket feed, not REST polling; this
  client covers the REST (order/portfolio) side.
