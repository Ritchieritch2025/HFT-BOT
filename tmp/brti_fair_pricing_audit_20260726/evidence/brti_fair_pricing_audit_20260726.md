# BRTI fair-pricing independent audit

Status: **NO_DECISION_DATA_ALIGNMENT_BLOCKED**.

This is a discovery-only, read-only audit. It did not import the engine, connect to the exchange, place/cancel an order, or touch 2026-07-23.

## Source-time availability

- 2026-07-20: CF Benchmarks files = **0**; book parquet files = **1**.
- 2026-07-21: CF Benchmarks files = **0**; book parquet files = **1**.
- 2026-07-22: CF Benchmarks files = **0**; book parquet files = **1**.
- Earliest fixed, non-banned later date with a capture: `2026-07-25`.

## Blocking gaps

- Missing authoritative `cfbenchmarks_value`/BRTI capture for 2026-07-20: expected `/home/ubuntu/hft-bot/work/live/cfbenchmarks/date=2026-07-20/cfb_*.ndjson`.
- Missing authoritative `cfbenchmarks_value`/BRTI capture for 2026-07-21: expected `/home/ubuntu/hft-bot/work/live/cfbenchmarks/date=2026-07-21/cfb_*.ndjson`.
- Missing authoritative `cfbenchmarks_value`/BRTI capture for 2026-07-22: expected `/home/ubuntu/hft-bot/work/live/cfbenchmarks/date=2026-07-22/cfb_*.ndjson`.
- Without per-second BRTI source time/value, sigma60, sigma300, kernel fair, fair velocity, BRTI momentum, and standardized distance are unidentified.
- Market metadata/settlement and Kalshi books cannot backfill the missing BRTI path without changing the pre-registered model.

## Requested analyses

- `kernel_vs_settlement_calibration`: **BLOCKED** — Authoritative BRTI source-time path is absent on one or more required discovery dates; no proxy substitution is permitted.
- `kernel_vs_market_mid_benchmark`: **BLOCKED** — Authoritative BRTI source-time path is absent on one or more required discovery dates; no proxy substitution is permitted.
- `fair_mid_gap_future_mid_change`: **BLOCKED** — Authoritative BRTI source-time path is absent on one or more required discovery dates; no proxy substitution is permitted.
- `p3_first_fill_pnl_oof`: **BLOCKED** — Authoritative BRTI source-time path is absent on one or more required discovery dates; no proxy substitution is permitted.
- `p3_completion_cost_deterioration_oof`: **BLOCKED** — Authoritative BRTI source-time path is absent on one or more required discovery dates; no proxy substitution is permitted. Existing P3 schema must also contain first side/price.

## Calibration versus execution

Calibration asks whether BRTI fair probabilities predict the final YES/NO result and lead market mid. Execution asks whether a first maker fill can be economically completed after queueing, adverse selection, fees, and forced exit. A loss in the latter does not identify an error in the former.

No proxy was substituted for missing BRTI. In particular, later 2026-07-25..26 capture, Coinbase, market mid, and terminal settlement cannot reconstruct the missing 60s/300s source-time volatility or the contemporaneous fair path for 2026-07-20..22.

Exact manifests, schema checks, guards, and seal bindings are in `brti_fair_pricing_audit.json`.
