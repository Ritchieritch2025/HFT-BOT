# Execution Audit — API Pull + Token Bucket + Warehouse Test Plan

Date: 2026-07-05
Audited plan: `API_PULL_AND_WAREHOUSE_TEST_EXECUTION_PLAN.md`
Method: static read of the codebase (no build/tests executed). Paths and behaviors
below were confirmed by reading source, not by running it.

## Verdict

The plan is **executable and well-matched to the repo**, and the safety boundary it
relies on is enforced in code (not just prose). If you run it as written you will get
the API pull lane exercised, paced, and a warehouse populated — **but three things will
bite you** before "results start to store," so fix them first:

1. **Phase B needs a real timed-loop harness, not just a `.py` file.** The executor is C++.
2. **Phase A + Phase E need `duckdb` installed** — the plan lists "no dependencies," which is wrong for `test_warehouse_converter.py`.
3. **A WS-only capture only fills `orderbooks_*` + `trades`.** `markets` / `events` / `market_settlements` stay empty unless you feed REST snapshots the plan never wires up.

Everything else is either correct or a one-line correction.

## What is verified solid (green)

- **Single outbound path + reserve-before-send.** `src/request_executor.cpp` reserves
  tokens at step 5 (`bucket.reserve_or_wait`, line 99). On failure it records telemetry
  and `return`s a local `rate_limited` error at line 106 — **before** the sign/send loop
  (line 112+). So the "local no-token path sends zero HTTP" claim (Section 2, Stop
  Condition #3) holds by construction.
- **Batch reserves the full total up front.** `SendOpts.cost_override` reserves the batch
  sum as one unit (line 79-81); no partial send. Matches Section 3 batch rule + Phase B #6.
- **Telemetry has every field the plan and dashboard want.** `include/kalshi/telemetry.hpp`
  `RequestTelemetry` carries method, normalized_path, bucket, cost_tokens, wait_ns,
  tokens_before/after, attempts, http_status, outcome, endpoint_cost_source, usage_tier.
  `NdjsonTelemetrySink` writes it as one JSON line per request — exactly the Phase F /
  Section 11 evidence source.
- **Safety boundary is enforced in `src/env.cpp`, not just documented.**
  `KALSHI_ENV` defaults to `local_mock`; `prod` requires `KALSHI_ALLOW_PROD=1` (fail
  closed); `KALSHI_MODE` defaults to `data_collect`; `live` requires `KALSHI_ALLOW_LIVE=1`
  (fail closed) and is refused on `local_mock`. `apps/preflight.cpp` is read-only and
  states orders require live mode. This backs Section 0 directly.
- **Phase A gates are real.** `make check` = `gate` + `PURE_TESTS` + `OFFLINE_TESTS`
  (Makefile lines 24-38, 337-345), covering `test_token_bucket`, `test_request_spec`,
  `test_account_limits`, `test_endpoint_costs`, `test_request_executor`, `test_batch_cost`.
  `tests/run_pipeline.sh` runs `make check` plus `test_greed_schema.py` and
  `test_warehouse_converter.py`, and writes `work/test_results.ndjson` (the dashboard's
  documented data source).
- **Every file the plan names exists** except the intentionally-missing frequency test.
  The relevant binaries (`preflight`, `ws_shadow`, `rate_probe`, `test_request_executor`,
  `test_token_bucket`, `test_batch_cost`, `test_account_limits`, `test_endpoint_costs`)
  are already built in `build/`.
- **Phase B mock side is ready.** `tests/mock_rest.py` already serves `/account/limits`,
  `/account/endpoint_costs`, `/markets`, `/markets/orderbooks` (batch), single orderbook,
  and a 429 matrix (`RETRY_NONE` / `RETRY_DELTA` / `RETRY_GARBAGE`).
- **Warehouse CLI matches the plan exactly.** `tools/warehouse_convert_capture.py` accepts
  `--input`, `--warehouse`, `--run-id`, `--replace-run` (plus an extra `--run-dir`), and
  enforces the internal-vs-public column split and `--replace-run` idempotency guard.

## Gaps and blockers (ranked)

### G1 — Phase B is understated: no timed-loop harness exists (fix before Phase B)

The plan asks for "one repeated-pull integration test that proves a loop … is paced by the
token bucket over time," preferably `tests/test_api_pull_frequency.py` "using the existing
mock REST server and RequestExecutor telemetry." Two problems:

- `RequestExecutor` is **C++**. A pure-Python test cannot drive its bucket/telemetry. The
  repo's own pattern for this is a **C++ test binary + shell wrapper against the Python
  mock** (`tests/run_request_executor.sh` → `./build/test_request_executor`).
- The existing `test_request_executor.cpp` only does **one-shot** reservation checks (it
  asserts a single `rate_limited`, line 121). There is **no** binary anywhere that loops
  repeated pulls over wall-clock time and emits pacing telemetry.
- `apps/rate_probe.cpp` looks like a candidate but is the **opposite**: it deliberately
  **bypasses** the local bucket (raw `client.request`) to test *server* pushback. Do not
  reuse it for the pacing test.

**Fix:** add a small C++ loop driver (e.g. `tests/test_api_pull_frequency.cpp` +
`tests/run_api_pull_frequency.sh` mirroring `run_request_executor.sh`), point it at
`mock_rest.py` with a tiny bucket (`refill_rate=20, cap=20, default_cost=10`), attach an
`NdjsonTelemetrySink`, and assert observed sends/sec ≤ `refill_rate/cost` from the emitted
telemetry. A `.py` wrapper is fine as the *assertion* layer reading the telemetry NDJSON,
but the loop that touches `RequestExecutor` must be compiled. Then register it in
`run_pipeline.sh` and the Makefile so `make check` actually gates it.

### G2 — `duckdb` is an unlisted precondition for Phase A and Phase E

`tests/test_warehouse_converter.py` does a **top-level** `import duckdb` (line 17) with no
skip guard, and the converter needs duckdb to write Parquet (`tools/warehouse_convert_capture.py`
lines 146, 844). So:

- Phase A's `run_pipeline.sh` **fails** at `test_warehouse_converter` if duckdb is absent —
  contradicting the Phase A acceptance line "No credentials required / No network required"
  (it silently also requires duckdb).
- Phase E conversion fails without it too.

**Fix:** add a precondition step `pip install duckdb` (verify with
`python3 tools/warehouse_convert_capture.py --check-deps`) and correct the Phase A acceptance
wording. `test_greed_schema.py` is fine without duckdb; only the converter test needs it.

### G3 — Phase E `--input` path won't exist as written (fix Phase C→E wiring)

`tools/exchange_check.sh` writes its capture to `work/exchange_check_capture.ndjson`. Phase E's
example feeds `--input work/<capture-run>/capture.ndjson`, which nothing produces. First run
will be a file-not-found.

**Fix:** point Phase E at the real path, e.g.
`--input work/exchange_check_capture.ndjson`, or have exchange_check.sh write into a
per-run directory and pass that.

### G4 — WS-only capture leaves `markets` / `events` / `market_settlements` empty

This is the biggest gap for the "results starting to store" goal. `exchange_check.sh` →
`ws_shadow` captures WS frames (orderbook snapshot/delta + trades). The converter derives
`markets` / `events` / `market_settlements` only from `market*` / `event*` / `settlement*`
message types (converter lines 581, 732-742), which a plain orderbook WS subscription does
**not** emit. Those tables will be empty. The plan's "row counts nonzero for categories
present in the capture" is internally consistent, but an operator will reasonably expect
those three tables to fill and they won't.

**Fix:** either (a) explicitly document that a WS orderbook capture only fills
`orderbooks_l1`, `orderbooks_full`, and `trades`, or (b) add a REST-snapshot step (markets /
events / settlements pulls) and feed it to the converter's `--run-dir` — which the plan
currently never mentions.

### G5 — `rate_probe` must be kept out of the prod read-only smoke

`apps/rate_probe.cpp` bypasses the local bucket and hammers the server to force 429s.
Running it during Phase C/D would trip two Stop Conditions at once ("any request bypasses
`RequestExecutor`" and "read-only smoke receives 429"). It isn't in the plan's command list —
just add an explicit "do not run `rate_probe` as part of read-only smoke" note so nobody
reaches for it.

### G6 — minor: verify `orderbooks_l1.yes_ask_dollars` vs "derive ask only in views"

Section 8 says "YES ask is derived as `1.00 - best_no_bid` only in views," yet the public
`orderbooks_l1` schema stores a `yes_ask_dollars` column (converter lines 40-48). The raw
book (`orderbooks_full`) correctly stores bids only. This is probably fine (l1 is a derived
summary, not the raw book) and is dictated by the Greed snapshot, but confirm the l1
derived-ask matches `schema_snapshot.json` so the "no fabricated asks in the stored book"
intent isn't violated.

## Corrected execution order + preconditions

Preconditions (add to the top of the plan):

- Toolchain to build C++ (the Makefile targets); confirm `make check` compiles clean.
- `pip install duckdb` (Phase A converter test + Phase E).
- Prod (real) credentials in env only at Phase C (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`); the demo environment is no longer supported, so Phase C runs read-only against prod.

Order (largely as written, with the fixes folded in):

1. `pip install duckdb`; `make check`; `tests/run_pipeline.sh` (Phase A).
2. **Build the C++ pacing harness (G1)**, register it in `run_pipeline.sh`/Makefile.
3. Re-run Phase A so the new gate is included.
4. Run Phase B pacing + 429 + batch + reserve-before-send assertions from telemetry.
5. Phase C prod read-only smoke via `exchange_check.sh --env prod` (never `rate_probe`; demo env no longer supported).
6. Convert with the **real** capture path (G3):
   `python3 tools/warehouse_convert_capture.py --input work/exchange_check_capture.ndjson --warehouse work/warehouse --run-id prod_smoke_<ts> --replace-run`.
7. Verify `orderbooks_l1/full` + `trades` fill; expect `markets/events/settlements` empty
   unless you added the REST-snapshot `--run-dir` step (G4).
8. Optional short Phase D prod read-only smoke, then convert again.
9. Phase F dashboard panels last (telemetry + `work/test_results.ndjson` already exist as
   sources; the specific API-pull/warehouse panels are still to build).

## Evidence (files read)

`src/request_executor.cpp`, `include/kalshi/request_executor.hpp`,
`include/kalshi/telemetry.hpp`, `src/env.cpp`, `apps/preflight.cpp`, `apps/rate_probe.cpp`,
`apps/ws_shadow.cpp`, `Makefile`, `tests/run_pipeline.sh`, `tests/run_request_executor.sh`,
`tests/mock_rest.py`, `tests/test_request_executor.cpp`, `tools/exchange_check.sh`,
`tools/warehouse_convert_capture.py`, `tests/test_warehouse_converter.py`.
