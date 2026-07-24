# H3 crypto external-anchor capture

This is the prospective data intake for `AS03-CRYPTO-EXTERNAL-ANCHOR`.
It records public data only. It cannot place an order, read an account, use a
private key, call the authenticated CF Benchmarks feed, or mutate production.

The first research question is narrow: after clocks are aligned, do public
BTC/ETH spot changes precede a still-actionable change in the exact Kalshi
15-minute contract? This collector creates evidence for that question; it does
not itself claim alpha or cash PnL.

## Exact sources and identity gates

- Coinbase Advanced Trade public WebSocket:
  `wss://advanced-trade-ws.coinbase.com`.
- Products are fixed to `BTC-USD` and `ETH-USD`.
- Default public channels are `market_trades,ticker,heartbeats`. Full `level2`
  is explicit opt-in because a 24-hour raw book can be much larger.
- Kalshi public REST: `https://external-api.kalshi.com/trade-api/v2`.
- Series are fixed to `KXBTC15M` and `KXETH15M`. Discovery is not performed by
  a substring search.
- Each series must match its exact ticker, exact current title, category
  `Crypto`, frequency `fifteen_min`, and exact settlement source
  `CF Benchmarks / https://www.cfbenchmarks.com/`.
- Each discovered market is independently joined through its event response:
  exact event `series_ticker`, category, anchored title shape, settlement
  source, and event-market membership must pass before snapshots are admitted.

Metadata drift is a refusal receipt, not an automatic remap.

## Run

Use an isolated environment:

```bash
cd "Deepresearch V3/alpha_sprint/h3_crypto_anchor"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python capture.py --duration-seconds 60
```

A 24-hour default-channel capture is still finite and disk-bounded:

```bash
python capture.py \
  --duration-seconds 86400 \
  --max-data-bytes 50000000000
```

Do not begin a 24-hour L2 capture from an estimate. Measure a short window:

```bash
python capture.py \
  --sources coinbase \
  --coinbase-channels market_trades,level2,heartbeats \
  --duration-seconds 60
```

Then compare the `projected_24h_data_bytes_at_measured_rate` in the final
`RUN_STOPPED` receipt with available storage. The default 50,000,000,000-byte
hard cap applies across raw data files; when reached, the data append fails
before crossing the cap and a terminal worker receipt remains writable.

The 2026-07-24 60-second default-channel smoke wrote 1,256,883 raw-data bytes
in 60.253 seconds: 20,860 bytes/second and a straight-line 24-hour projection
of 1,802,323,505 bytes (about 1.68 GiB). It observed 501 Coinbase envelopes,
24 exact Kalshi market snapshots and 24 orderbook snapshots with zero parse
errors, sequence gaps, HTTP errors, mapping refusals or worker failures. This
is a capacity check, not a guarantee that a volatile future day will have the
same rate; the 50 GB hard cap is the operative protection. The immutable
summary and file hashes are in `evidence/SMOKE_60S_20260724.json`.

A separate 60-second Coinbase `market_trades,level2,heartbeats` smoke wrote
13,898,730 bytes in 60.030 seconds: 231,531 bytes/second and a straight-line
24-hour projection of 20,004,308,409 bytes (about 18.63 GiB). That is below
W09's reported 182 GiB free-space figure, but it is roughly eleven times the
default-channel rate and one quiet minute is not a capacity guarantee. L2
therefore remains explicit opt-in and subject to the same 50 GB cap. Its
summary and hashes are in `evidence/SMOKE_L2_60S_20260724.json`.

The Kalshi-only smoke path has no third-party package dependency:

```bash
python capture.py --sources kalshi --duration-seconds 10
```

Tests are offline and synthetic:

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## Output and schema

Every invocation exclusively creates a new run directory under `captures/`.
Existing runs are never reopened or truncated. Files use one JSON object per
line and are opened with `O_APPEND | O_EXCL`:

- `coinbase_ws.ndjson`: exact raw WebSocket JSON plus source event-time
  summary, local receive wall/monotonic clocks, connection ID, sequence and
  validation diagnostics;
- `kalshi_catalog.ndjson`: raw exact-series, event and market-discovery GET
  responses;
- `kalshi_market.ndjson`: raw market and orderbook point-in-time GET
  responses;
- `receipts.ndjson`: lifecycle, reconnect, sequence-gap, mapping-refusal,
  HTTP-error, byte-cap and final rate/projection receipts.

`schema/record-v1.schema.json` describes the common outer record. Every raw
payload has a SHA-256. HTTP records retain request-send and response-receive
wall/monotonic clocks and allow only `GET`.

## Clock and gap semantics

- `source_envelope_time` and the raw per-trade/per-book event times are
  exchange clocks. They are not replaced with local time.
- `local_receive_wall_ns` is the local UTC-alignable observation clock.
- `local_receive_monotonic_ns` is the within-host ordering/latency clock; it
  is not comparable across reboots or hosts.
- Coinbase source sequence is checked over the observed multiplexed
  connection stream. A jump, duplicate, out-of-order message, reconnect or
  update-before-L2-snapshot is recorded; missing rows are never fabricated.
- Kalshi REST is sampled, not a lossless event feed. HTTP `Date`,
  round-trip time and raw market `updated_time` are retained, but unobserved
  changes between polls remain unknown.
- A reconnect creates a new connection ID and an explicit uncertainty
  receipt. It does not pretend continuity with the previous socket.

## What is still missing

- The exact CF Benchmarks BRTI/ETHUSD_RTI feed used for settlement is
  intentionally absent because it requires separate authenticated authority.
- A second independent spot exchange such as Kraken is not in this MVP.
- Default capture has top-of-book ticker, not Coinbase full depth. L2 is an
  opt-in short-window measurement until its storage rate is accepted.
- Kalshi REST snapshots cannot replace effective-time Kalshi WebSocket L2.
- Fees, decision-to-order latency, strict fills and complete exits remain
  PnL Spine inputs. Until they are bound, results are lead/lag diagnostics,
  not cash PnL.
