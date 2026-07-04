# Kalshi WebSocket v2 — protocol ground truth

Single source of truth for the WS market-data engine. Code comments cite this
file by invariant id (e.g. "I3") rather than URLs. Verified against Kalshi
OpenAPI/AsyncAPI + docs on 2026-07-04. When the spec-drift check
(`tools/check_spec_drift.sh`, Phase 0 task 2) fires, reconcile here first.

Vendored specs: `third_party/kalshi_specs/openapi.yaml`,
`third_party/kalshi_specs/asyncapi.yaml` (+ `FETCHED_AT`).

## Connection & auth (I10)

- Prod: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
- Auth headers on the HTTP upgrade (same scheme as REST, reuse `client.cpp`
  RSA-PSS signing): `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE`. **Signed string = `timestamp + "GET" +
  "/trade-api/ws/v2"`** — no host, no query. On 401, surface a server-date skew
  hint (clock-discipline parity with the REST path).

## Commands (client → server)

```json
{"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],
  "market_tickers":["MKT-A","MKT-B"],"use_yes_price":false}}
{"id":2,"cmd":"unsubscribe","params":{"sids":[7]}}
{"id":3,"cmd":"update_subscription","params":{"sids":[7],"action":"get_snapshot",
  "market_tickers":["MKT-A"]}}
{"id":4,"cmd":"update_subscription","params":{"sids":[7],"action":"add_markets",
  "market_tickers":["MKT-C"]}}
```
- `id` is a client-chosen correlation counter (start 1, increment). Error 18
  exists (unknown/duplicate id) — keep a correlation table with timeouts.
- `use_yes_price` **must be sent explicitly** (I5). We send `false` (no-leg
  pricing for the no side) via the single constant `kOrderbookNoSideLeg`;
  Kalshi will flip the default and later remove the flag — the drift check must
  catch it, and flipping our convention is a 1-line change + tests.
- `update_subscription get_snapshot` takes **tickers only** and returns an
  in-stream `orderbook_snapshot` without disturbing the delta stream (I4).

## Responses (server → client)

```json
{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}}
{"id":123,"sid":7,"seq":222,"type":"ok","msg":{"market_tickers":["MKT-1"]}}
{"id":2,"sid":7,"seq":223,"type":"unsubscribed","msg":{}}
{"id":9,"type":"error","msg":{"code":25,"msg":"..."}}
```
Envelope fields: `id`, `type`, `sid`, `seq`, `msg`. `ok` and `unsubscribed`
**may/do carry sid+seq** and therefore advance the per-sid seq counter (I1).

## Market-data messages

### orderbook_snapshot / orderbook_delta (channel `orderbook_delta`)

```json
{"type":"orderbook_snapshot","sid":7,"seq":2,"msg":{
  "market_ticker":"FED-23DEC-T3.00","market_id":"<uuid>",
  "yes_dollars_fp":[["0.0800","300.00"],["0.2200","333.00"]],
  "no_dollars_fp":[["0.5400","20.00"],["0.5600","146.00"]]}}

{"type":"orderbook_delta","sid":7,"seq":3,"msg":{
  "market_ticker":"FED-23DEC-T3.00","price_dollars":"0.960","delta_fp":"-54.00",
  "side":"yes","ts_ms":1669149841000,"client_order_id":"<optional>"}}
```
- Prices are dollar strings (≤4 dp) → `trading::PriceE4` (dollars×10⁴,
  [0,10000]); excess precision = reject + telemetry (no floats — guardrail 4).
- Sizes/deltas are 2-dp fixed-point strings → `trading::CountFp` (contracts×10²);
  delta uses the SIGNED parser.
- Arrays omitted when empty. `client_order_id` on deltas is passthrough-only.

### ticker (channel `ticker`) — no seq (I9)

`msg`: `market_ticker`, `market_id`, `price_dollars`, `yes_bid_dollars`,
`yes_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `volume_fp`,
`open_interest_fp`, `dollar_volume`, `dollar_open_interest`,
`last_trade_size_fp`, `ts_ms`. Use `ts_ms` only; `ts`/`time` are deprecated.

### trade (channel `trade`) — no seq, dedupe by `trade_id` (I9)

`msg`: `trade_id`, `market_ticker`, `yes_price_dollars`, `no_price_dollars`,
`count_fp`, `ts_ms`, and taker direction. Prefer
`taker_outcome_side`/`taker_book_side`; `taker_side` is deprecated fallback.

### market_lifecycle_v2 / event_lifecycle (global, unfiltered) — no seq (I9)

Event-type set is OPEN (types added AND removed within 2026 — tolerate
unknowns via a `Lifecycle::Unknown{string}` alternative that round-trips to the
raw log). Known types include: `created`, `open`, `paused`, `closed`,
`determined` (+ `settlement_value`), `settled`,
`price_level_structure_updated` (`linear_cent`/`deci_cent`/`tapered_deci_cent`),
`metadata_updated`. `market_lifecycle_v2` also emits `event_lifecycle`.

## Sequencing invariants (violating any = P0 bug)

| # | Invariant |
|---|-----------|
| I1 | `seq` is per-sid, monotonic across ALL markets on the sid, and advanced by sequenced control responses (`ok` with sid+seq, `unsubscribed`). Gap check lives at sid level only (`SidStream`). |
| I2 | One subscription per channel per session. Repeated subscribe MERGES (same tickers = no-op, NO fresh snapshot; new tickers = added). "Resubscribe" as recovery = unsubscribe→subscribe. |
| I3 | A sid seq gap invalidates EVERY market on that sid. Re-validation is per-market via in-stream `orderbook_snapshot`. |
| I4 | REST orderbook snapshots carry NO seq and must NEVER re-seed a WS-live book. In-stream recovery only: `get_snapshot` → delete/add → unsub/sub → reconnect (escalating ladder). |
| I5 | `use_yes_price` sent explicitly (currently `false`). One constant, drift-checked. |
| I6 | Server pings every ~10 s with body `"heartbeat"`; client must pong the same payload promptly; >~30 s ping silence = dead → reconnect. |
| I7 | Error **25** = server-side subscription buffer overflow = data ALREADY LOST: full-sid invalidate + in-stream resync + `gap`/`loss` marker in the raw log. |
| I8 | sid values die with the connection. Every enqueued message carries a `stream_epoch`; consumers drop epoch mismatches. Post-reconnect, `list_subscriptions` self-check. |
| I9 | Only orderbook messages carry seq. `ticker`/`trade`/`lifecycle` have none — dedupe/order by `ts_ms` (ticker), `trade_id` (trade); lifecycle unordered + unfiltered. |
| I10 | WS auth per "Connection & auth" above. |
| I11 | Rate limits token-based (default 10/request), separate Read/Write buckets, **no Retry-After on 429**. Pull tier via `GET /account/api_limits` and costs via `GET /account/non-default-endpoint-costs` at startup. Batch-orderbook token cost is UNDOCUMENTED — measure in demo, budget worst-case. |
| I12 | WS connections per user tier-limited (default 200); multi-connection sharding is officially supported. |

## Error codes

Official list: **1–22** (AsyncAPI enum) plus **25** (docs page + changelog).
**Codes 26/27 DO NOT EXIST** in official docs. Any unknown code → conservative
path: log the raw payload, treat as a channel error, alert — never implement
behavior for undocumented codes (guardrail 6). Notable: 6 = already-subscribed
(should be unreachable given I2-aware logic → assert), 18 = bad/duplicate
command id, 25 = buffer overflow (I7).

## Connection layout (I12; Phase 2)

- conn A: `orderbook_delta` only, explicit `market_tickers`, `use_yes_price:false`.
- conn B: `ticker` + `trade`, explicit tickers, optional `send_initial_snapshot`.
- conn C: `market_lifecycle_v2`, global/unfiltered by design.

Sharding a 2nd orderbook connection is OUT of scope until triggered (error 25 /
read-loop saturation / p99 budget breach) — leave a seam, don't build it.

## Open items to verify empirically (do not assume)

1. Batch-orderbooks token cost (per request vs per ticker) — Phase 5 script.
2. Whether `get_snapshot` seq-stamps with the next sid seq or re-baselines —
   confirm in demo before finalizing SidStream accounting (changelog: does not
   disturb the delta stream; verify the observed seq value). Current code treats
   the snapshot's seq as a normal sequenced observation.
3. `use_yes_price` default-flip date — watch changelog; drift check must catch it.
4. Exact current WS error-code list beyond 22/25 — conservative path covers it.
