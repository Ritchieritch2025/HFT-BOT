# Ops Console (localhost dashboard)

A read-only operational console for observing the Kalshi PoC during REST +
WebSocket benchmark/shadow testing. Not a trading UI, not a charting app —
tables and status badges only.

## Run

```sh
python3 dashboard_server.py --metrics work/metrics.ndjson --port 8765
# open http://127.0.0.1:8765
```

Python 3 stdlib only — no dependencies, no database, no build step. Binds
`127.0.0.1` by default (localhost only). Works whether or not the metrics file
exists yet; it appears in the console the moment the trading system writes it.

Flags: `--metrics <path>` (default `work/metrics.ndjson`), `--port` (8765),
`--host` (127.0.0.1), `--backfill` (1000 history lines sent on connect).

## How it stays off the hot path

The trading engine (`tradingd`) writes append-only NDJSON from its **cold
telemetry thread** — never the hot decision/order path. The dashboard only
*reads* that file (tails it) and streams new lines to the browser over Server-
Sent Events. Killing, reloading, or lagging the dashboard cannot affect
trading: it is a separate process that touches nothing the engine depends on.

```
tradingd cold telemetry thread ──append──▶ work/metrics.ndjson
                                                   │ tail
                                    dashboard_server.py ──SSE──▶ browser
```

## Wiring the real engine

`tradingd` emits dashboard NDJSON when `TRADINGD_NDJSON` is set:

```sh
TRADINGD_NDJSON=work/metrics.ndjson TRADINGD_MODE=bench ./build/tradingd --poll 500
```

It writes `system` events (started / heartbeat every 2s / stopped) and one
`order` event per submission (real strategy, ticker, side, price, size, http
status, sign µs, submit→ack ms, signal→ack ms). Feed / strategy / risk event
types are supported by the schema and the dashboard renders them today; the
engine will emit them once the WebSocket feed and risk layer land.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the dashboard (single HTML page) |
| GET | `/stream?backfill=N` | SSE: N history lines then live tail |
| GET | `/healthz` | `{"ok":true}` |

There is no live-order action anywhere in the dashboard or server.

## Sections

1. **System Status** — process/heartbeat, mode, uptime, REST/WS/provider
   status (derived from feed events), kill switch, config profile, API env.
2. **Feed Status** — per-source table: connected, last-msg age, freshness,
   rate, gaps, reconnects, stale/valid.
3. **Order Status** — latest 200: time, strategy, ticker, side, price, size,
   mode, status, http, sign µs, submit→ack, signal→ack, reason.
4. **Strategy Status** — per-strategy: enabled, triggers, last trigger, edge
   signal/ack ¢, shadow pnl ¢, reason.
5. **Risk Status** — rate limits, exposure, rejection counters (stale /
   duplicate / risk-check), kill switch.
6. **Event Log** — latest 200, filter by type, substring find, click a row to
   expand raw JSON.

## Performance notes

- SSE backfill of 1000 lines arrives in ~8 ms; thousands of live events stream
  with no loss (verified: 8000-event load test).
- The browser bounds memory: 1000 events retained, 200 rows rendered per table.
- Rendering is throttled to 4 Hz and decoupled from ingest rate, so a burst of
  events never stalls the UI.
- The server tails by polling file size every ~150 ms — negligible CPU, and
  portable across macOS (dev) and Linux (deploy) with no inotify dependency.

## Event schema

One JSON object per line. Types: `system`, `feed`, `strategy`, `order`,
`risk`. Renderers are field-tolerant — present fields are shown, missing ones
render as `—` — so the minimal example events and richer future events both
work.
```
