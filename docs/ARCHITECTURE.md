# Architecture

One page for the whole repo: layout, the two execution paths, tradingd's thread
model, data flows, environment variables, and on-disk formats. The authoritative
list of every binary/script is `tools.json` (validated by
`tools/check_registry.py`); this doc explains how they fit together.

## Two layers

```
include/trading/   source-agnostic bus (no Kalshi types leak in)
  fixedpoint (PriceE4=int32 dollars x1e4, CountFp=int64 contracts x1e2)
  ids (SourceId, deterministic EntityId hash, TraceId)  ·  timestamp
  bus (NormalizedEvent variant: BookSnapshot/BookDelta/Ticker/Trade/Lifecycle)
  storage (RawLogWriter/Reader byte-exact NDJSON, ReplaySource)  ·  risk (P7)
include/kalshi/    Kalshi gateway (all venue specifics stay here)
  client (signed transport, RSA-PSS) · env (Runtime + fail-closed gates)
  rest_api (typed endpoints) · limits (AccountLimits + EndpointCostTable)
  token_bucket (TokenBucketI64) · backoff · request_spec · request_executor
  orderbook · sid_stream · recovery · ws_client · ix_transport · ws_recorder
  gateway (KalshiRawDecoder + KalshiExecutionEngine) · wire (order JSON)
```

Flow: `DataSource -> NormalizedEvent -> MarketDataSink -> FeatureBuilder ->
Signal -> OrderIntent -> ExecutionEngine`. A single `trace_id` is minted at the
event and threaded through to the order intent.

## Two execution paths (important)

1. **tradingd lane path** (`apps/tradingd.cpp`) — the live engine. Pop the SPSC
   ring -> ttl/age/rate gates -> RSA-PSS sign -> `Lane::send()` on a dedicated
   warm HTTP/2 connection. CAN transmit when `orders_enabled` (live + allowed).
   Not yet routed through `RequestExecutor` for accounting — that is PLAN_PROD_V1
   **P7 (= TOKEN_RULES T8)**.
2. **KalshiExecutionEngine bus path** (`src/gateway.cpp`) — DataCollect rejects,
   Shadow logs a would-be order, Live currently throws (fail-closed skeleton, no
   transmit). Unification of the two transmit paths behind one gated,
   reconcile-capable `OrderSubmitter` is **P8**.

Every authenticated **REST** call already goes through the single
`RequestExecutor::send` (reserve-before-send -> sign -> send -> retry ->
telemetry). The order **lane** path is the one remaining exception, closed in P7/P8.

## tradingd thread model

- **hot/feed thread**: market data (REST poll today; WS engine ready, wired in
  P6) -> strategies inline (throwing slots quarantined) -> packs an 80-byte
  `ExecPayload` -> pushes onto a Vyukov MPMC ring (`include/kalshi/ring.hpp`).
- **submit lanes** (default 2): each owns a dedicated warm HTTP/2 TLS connection;
  pop -> gates -> sign -> `Lane::send()`. Idle lanes ping `/exchange/status` so
  orders never pay a handshake.
- **telemetry thread** (COLD): drains a telemetry ring -> formats NDJSON / Redis.
  Trading is unaffected if this stalls or Redis is down.

Guardrail: **observability never touches the hot path** — the hot path only
writes fixed-size structs/counters to bounded rings (drop+count on overflow); the
telemetry thread formats and writes. No file/JSON/network work inside feed,
strategy, or submit-critical code.

Strategy roster (`src/strategies.cpp` `make_strategies()`): empty by default, so
normal `tradingd` startup generates no orders. `STRATEGIES` selects strategies by
name at startup (env read once, never in `on_event`). `once_probe` is a one-shot
latency-probe strategy (slot 29): on the first `MarketEvent` matching
`PROBE_TICKER` it emits a single 1c YES bid, then stays silent — used to exercise
the full feed→strategy→ring→submit→ack chain under `TRADINGD_LATENCY_CSV`.

## Data flows

- **REST poll feed**: `feed::run_poll` -> `RestApi.orderbook` (via executor) ->
  `wire::MarketEvent` -> strategies. ~250 ms staleness (the reason for the WS feed).
- **WS feed** (engine built, P6 wires it): `IxWebSocketTransport` -> `KalshiWsClient`
  -> `OrderBookManager` (per-sid seq, recovery ladder) -> top-of-book ->
  `MarketEvent`. `WsRecorder` durably captures every frame (SPSC ring + writer
  thread + gap/loss/epoch markers).
- **Cold telemetry**: rings -> telemetry thread -> `work/metrics.ndjson` +
  request telemetry NDJSON; the dashboard only tails files.
- **Replay/backtest**: `ReplaySource(capture.ndjson)` -> `KalshiRawDecoder` ->
  same `OrderBookManager` -> strategies (P9).
- **Cold research warehouse** (three layers, docs/warehouse_schema.md): hourly
  raw firehose logs `work/raw/date=<D>/firehose_<HH>.ndjson` -> `tools/ingest.py`
  (change-only + hourly heartbeats + class policy, checkpointed) ->
  `work/warehouse/staging.duckdb` (today, queryable live) -> `tools/export_day.py`
  at UTC midnight -> final zstd-15 Parquet / csv.gz partitions under
  `work/warehouse/facts/` + `manifest.csv`. Single analysis entry point:
  `tools/warehouse.py::load()`. Raw NDJSON is the truth source until archived;
  archive files are write-once and final.
- **Passive RFQ flow** (implemented, not deployed): a separate systemd unit
  launches a second read-only `ws_shadow` connection subscribed only to
  `communications`. One persistent socket writes `rfq_<HH>.ndjson` by TL1
  receive-clock partitioning, without an hourly reconnect. It shares no PID/lock/file with
  the firehose; `rfq_disable` stops only this connection. The first 48-hour
  atlas reads sealed raw and makes no trading/profitability claim.

## Environment variables

| Var | Used by | Meaning |
|---|---|---|
| `KALSHI_ENV` | all | `local_mock` (default) / `prod` (`demo` is rejected fail-closed) |
| `KALSHI_MODE` | tradingd | `data_collect` (default) / `shadow` / `live` |
| `KALSHI_ALLOW_PROD` | env | required truthy for `prod` (fail-closed) |
| `KALSHI_ALLOW_LIVE` | env | required truthy for `live` (+ non-local env) |
| `KALSHI_BASE_URL` | all | override REST host; cross-validated vs env allowlist |
| `KALSHI_WS_URL` / `KALSHI_WS_SIGN_PATH` | ws | override WS URL / signed path |
| `KALSHI_HOST_UNSAFE_OVERRIDE` | env | skip host allowlist (never TLS; refused in live) |
| `KALSHI_MOCK_PORT` / `KALSHI_MOCK_WS_PORT` | local_mock | mock REST/WS ports |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | signed tools | credentials |
| `KALSHI_WS_TICKERS` | ws_shadow | markets to subscribe |
| `KALSHI_SHADOW_SECONDS` / `_CAPTURE` / `_XCHECK` | ws_shadow | soak length / capture path / REST cross-check |
| `STRATEGIES` | tradingd | comma-separated roster (unset = empty = no orders); unknown name / missing required param fails closed |
| `PROBE_TICKER` / `PROBE_PRICE_CENTS` / `PROBE_COUNT` | once_probe | market to probe (required) / limit price (default 1) / contracts (default 1) |
| `TRADINGD_WORKERS` / `_RING` / `_SPIN` | tradingd | lanes / ring size / busy-poll |
| `TRADINGD_MAX_QUEUE_AGE_MS` / `_MAX_ORDERS_PER_SEC` | tradingd | ttl gate / order-rate cap (folds into the Write bucket in P7) |
| `TRADINGD_KEEPALIVE_S` / `_DRAIN_MS` | tradingd | lane keepalive / shutdown drain |
| `REDIS_HOST` / `REDIS_PORT` / `INGEST_MARKET_LIMIT` | ingestd | cold-path telemetry / poll breadth |
| `PIPELINE_PORT_BASE` | run_pipeline | base port for mocks (default 18300) |

## On-disk formats (all NDJSON, one record per line)

- **Raw capture** (`RawLogWriter`): `recv_mono_ns, recv_wall_ns,
  source_event_time_ms?, source, channel, source_ticker, source_sequence?, sid?,
  stream_epoch?, marker?, raw | raw_b64`. `raw` = byte-exact payload (base64 when
  not UTF-8). `marker` records (`gap`/`loss`/`epoch_change`) annotate blind spots.
- **Request telemetry** (`RequestTelemetry`): `ts_ns, method, path, bucket,
  mutation, cost, tokens_before_milli, tokens_after_milli, wait_ns, attempts,
  http_status, outcome, endpoint_cost_source, usage_tier, trace_id`. Secret-free
  by construction.
- **Would-be-order log** (shadow): `ts_ns, mode, env, decision, entity_id,
  trace_id, side, price, size, tif, post_only`. Never transmitted.
- **Test results** (`run_pipeline.sh`): `type=test_suite, ts_ms, suite, status,
  passed, failed, duration_ms, log`. The ops console's data source.
- **Greed-compatible warehouse** (cold path): public Parquet tables
  `orderbooks_l1`, `orderbooks_full`, `trades`, `markets`, `events`,
  `market_settlements`, and `rfq_events` use the column order pinned in
  `docs/vendor/greed/schema_snapshot.json`. Internal warehouse metadata is JSON
  under `work/warehouse/_meta/warehouse_*.json`, never public table columns.

## Cold research dependency rule

DuckDB is the only new dependency and is allowed only for offline research
warehouse tooling. It is forbidden in `tradingd`, `ws_client`,
`OrderBookManager`, strategy, execution, risk, and any live-order code. If
DuckDB is missing, warehouse tools/tests fail with
`DuckDB is required for the offline research warehouse.` There is no SQLite
fallback for the Greed-compatible warehouse target.

## Build + check

`make` (full, incl. ixwebsocket-linked `ws_smoke`/`ws_shadow`); CMake mirrors the
core lib + tests. `make check` = gates (`tools/check_gates.sh` incl. the registry
check) + pure/offline unit tests. `make san` / `make tsan` / `make fuzz` for
sanitizers. `tests/run_pipeline.sh` runs the whole offline suite and writes
`work/test_results.ndjson`. Every new binary/test goes into **both** Makefile and
CMakeLists.txt, and gets a `tools.json` entry (the registry check enforces it).
