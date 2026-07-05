# Production Readiness Plan v1 — Claude Code Implementation Plan

> Drop this file into `docs/` and work it phase by phase. Every phase has an
> acceptance gate; do not start phase N+1 until phase N's gate passes.
> Companion to `docs/PLAN_WS_V2.md` (WS engine, phases 0–8 landed) and
> `docs/PLAN_TOKEN_RULES.md` (token rules, T0–T6 landed; T7/T8/T9 open).
> House style applies: offline-first tests, fail-closed safety, no new deps.

## Repo context (read first, do not re-derive)

- Build: `make` (full, incl. ixwebsocket-linked ws_smoke/ws_shadow), CMake
  mirror for the core lib + most tests. Keep BOTH in sync when adding files.
- Checks: `make check` (gates + pure/offline tests), `make test` (signing),
  `make san`, `make tsan`, `make fuzz`, `make gate`.
- Test output is MOSTLY uniform, not fully: the unit-test family prints
  `PASS: ...` / `FAIL: ...` lines and a final `ALL PASS` / `FAILURES`, but
  `test_integration` ends with `INTEGRATION PASS|FAIL`, and app-level tools
  use their own verdicts (`WS SMOKE PASS`, `WS SHADOW PASS`,
  `PREFLIGHT PASS`, `ORDER EXECUTION PASS`). Shell-driven suites
  (`tests/run_rest_api.sh`, `tests/run_request_executor.sh`,
  `tests/run_ws_smoke.sh`) spin up Python mocks. Therefore the runner (P2)
  must NOT assume one raw format: each `tools.json` entry declares its
  `pass_token` (default `ALL PASS`) and whether `PASS:/FAIL:` line counting
  applies; normalization happens in the runner/wrappers. Exit codes are the
  authoritative pass/fail signal everywhere (all tools return nonzero on
  failure); tokens are for display parsing only. New C++ unit tests must
  still follow the `PASS:/FAIL:` + `ALL PASS` house convention.
- Two execution paths exist today:
  1. `apps/tradingd.cpp` lane path: pop ring → gates → sign → `Lane::send()`.
     CAN transmit when `orders_enabled` (live + allows). Working but not yet
     routed through `RequestExecutor` (T8 open).
  2. `KalshiExecutionEngine` (`src/gateway.cpp`) bus path: DataCollect
     rejects, Shadow logs, Live throws (fail-closed skeleton, no transmit).
- Strategy roster is EMPTY (`src/strategies.cpp` returns `{}`).
- Feed is REST polling (`apps/feed.hpp`); the WS engine
  (`src/ws_client.cpp` + orderbook/sid_stream/recovery/recorder) is complete
  and shadow-tested but NOT wired into tradingd.
- Known broken refs: `deploy/bootstrap.sh` calls missing
  `tests/run_pipeline.sh`; docs mandate missing `tools/check_spec_drift.sh`
  and `docs/vendor/kalshi/`; `docs/KALSHI_RULEBOOK.md` (T9) unwritten.

## Hard guardrails (apply to every phase)

1. **The ops console can never place an order.** Orders always go through the
   CLI tools; the console stays read-only. `dashboard_server.py` and
   `tools/run_tests.py` stay Python-stdlib-only, bind 127.0.0.1, and refuse
   to execute anything registered with safety class `live_order` — even with
   flags (the server-side 403 refusal is enforced in the request handler, not
   just the UI). `network_read` tools require an explicit `--allow-network`
   server start flag.
2. No new third-party dependencies (C++ or Python).
3. No floats on price/count paths (`trading::fixedpoint` only).
4. Every phase ends green: `make && make check && make san`; `make tsan`
   where threads are touched; `./tests/run_pipeline.sh` once it exists (P0).
5. New binaries/tests go into Makefile AND CMakeLists.txt.
6. Never log secrets. `tools/check_gates.sh` stays in `make check`; extend
   it, never weaken it.
7. Items marked ASSUMPTION are local policy — comment them as such in code.
8. **Observability stays off the hot path.** No file writes, JSON/string
   building, subprocesses, network calls, or filesystem polling inside
   feed callbacks, strategy callbacks, or the order-submit critical path.
   The only permitted pattern (already the house pattern): hot path emits
   fixed-size structs/counters to bounded rings or atomics → the telemetry
   thread formats and writes NDJSON → the dashboard only tails files and
   never touches trading state. Rings are bounded and drop+count on
   overflow, never block. The live order path keeps its dedicated
   `Lane::send()` behavior.

---

## Phase P0 — Hygiene: scratch dirs, broken refs, doc drift

**Goal**: clean repo, single command that runs every offline suite.

Tasks:
1. Scratch discipline. The root `*.ndjson` / `rot.ndjson.*` litter is test
   output (tests default their scratch dir to `.`). Fix:
   - Add `$(BUILD)/scratch` dir target; in the `check` loop invoke every
     test as `$$t $(BUILD)/scratch` (tests that ignore argv are unaffected).
   - `fuzz` target: `fuzz_decode` takes `[iters] [dir]` positionally, so
     BOTH must be passed — `$(BUILD)/fuzz_decode 200000 $(BUILD)/scratch`.
     (Passing only a dir would be parsed as `atoi(dir)` → 0 iterations,
     silently fuzzing nothing. The current bare invocation is what left
     `fuzz_reader.ndjson` in the repo root.)
   - Delete the root litter; extend `.gitignore` (`/*.ndjson`,
     `/*.ndjson.*`, `build/`, `work/`, `*.dSYM/`) if not already covered.
2. Create `tests/run_pipeline.sh` (referenced by `deploy/bootstrap.sh`,
   currently missing). It is the canonical "run everything offline" entry:
   - `make check` (gates + pure + offline tests);
   - `./build/test_signing`;
   - `tests/mini_redis.py` on a free port → `./build/test_resp <port>`;
   - `tests/run_rest_api.sh`, `tests/run_request_executor.sh`,
     `tests/run_ws_smoke.sh`;
   - `./build/test_storage build/scratch`, `test_decode`, `test_replay
     build/scratch`, `test_recorder build/scratch`, `test_ws_client`,
     `test_shadow build/scratch`, `test_integration` against
     `tests/mock_server.py` (3 threads × 50 reqs);
   - Machine-readable output: append one NDJSON line per suite to
     `work/test_results.ndjson`:
     `{"type":"test_suite","ts_ms":...,"suite":"test_ring","status":"pass|fail","passed":N,"failed":N,"duration_ms":...,"log":"work/logs/test_ring.txt"}`
     (this is the P2 dashboard's data source — schema is an API from day 1).
   - Exit nonzero if any suite fails. Port selection: base port from
     `PIPELINE_PORT_BASE` (default 18300) to avoid collisions.
3. `ingestd` safety alignment: route `apps/ingestd.cpp` through
   `resolve_runtime()` like tradingd/preflight (it currently reads
   `KALSHI_BASE_URL` raw). Read-only daemon, but the rule is: EVERY binary
   that dials Kalshi validates env/host. Refuse on `SafetyViolation`.
4. Doc drift fixes (text only):
   - README: replace "WS later / next milestone" framing with the actual
     state (WS engine + shadow harness exist; not yet the tradingd feed);
     add `docs/PLAN_PROD_V1.md` to the docs list.
   - `docs/kalshi_ws_protocol.md` I11: old endpoint names
     `/account/api_limits` → `/account/limits`,
     `/account/non-default-endpoint-costs` → `/account/endpoint_costs`
     (live source + gates already use the new names).

New tests: none (behavioral no-op). `run_pipeline.sh` IS the test.

Gate: `./tests/run_pipeline.sh` green end-to-end on a clean checkout;
`git status` clean after a full run (no new files outside `build/`/`work/`);
root contains zero `*.ndjson`.

## Phase P1 — Tool registry + architecture map

**Goal**: one machine-readable source of truth for every tool; one page that
explains the whole repo.

Tasks:
1. Create `tools.json` at repo root. One entry per binary/script:
   ```json
   {
     "name": "test_ring",
     "kind": "test|bench|probe|daemon|check|example",
     "safety": "pure|offline|network_read|live_order",
     "cmd": "./build/test_ring",
     "args_template": "[scratch_dir]",
     "needs": ["make"],           // or "mock_rest", "mini_redis", "creds", "demo_env"
     "description": "Vyukov MPMC ring unit + concurrency test",
     "docs": "tests/test_ring.cpp"
   }
   ```
   Safety classes (drive P2 run-button policy):
   - `pure`: no sockets at all (test_ring, test_fixedpoint, bench_orderbook…)
   - `offline`: localhost mocks only (run_rest_api.sh, test_resp, ws_smoke…)
   - `network_read`: real Kalshi, read-only (preflight sans --order,
     bench_rtt, probe_batch_cost, ws_shadow)
   - `live_order`: transmits real orders (bench_order, fill_test,
     preflight --order). NEVER runnable from the console.
   Classify every entry conservatively: anything ambiguous rounds UP.
2. Create `docs/ARCHITECTURE.md`: directory map, every binary (from
   tools.json), the two execution paths, thread model of tradingd, data-flow
   diagrams (REST poll path, WS path, cold telemetry path), full env-var
   table (the knobs listed in README + shadow/probe vars), file formats
   (raw NDJSON schema incl. markers, telemetry NDJSON, would-be-order log,
   metrics.ndjson event types).
3. Add a `tools.json` validation check to `tools/check_gates.sh` (or a tiny
   `tools/check_registry.py`, stdlib-only): valid JSON, required fields,
   safety class in the enum, `pass_token` present where line-parsing is
   claimed, every `cmd` exists after `make`, and — the inverse — every
   buildable tool has a registry entry. Coverage must NOT be derived from
   `$(BINS)` alone: `fill_test` has a Makefile rule but is absent from
   `BINS` (not built by default). Derive the tool set by parsing all
   `$(BUILD)/%` targets from the Makefile (plus the Python/shell tools),
   and keep an explicit expected-tools list in the checker so an
   unregistered new target fails loudly. Also decide `fill_test`'s status
   explicitly: either add it to `BINS` or mark it `build_on_demand` in the
   registry — no implicit omissions.

Gate: registry check green in `make check`; every binary in `BINS` is
registered; `live_order` entries are exactly {bench_order, fill_test} plus
`preflight --order` noted on the preflight entry.

## Phase P2 — Ops console v2 (tests + tools + live)

**Goal**: the single localhost page that shows all test results, lets you run
safe tools, and keeps the existing live telemetry view.

Tasks:
1. `tools/run_tests.py` (stdlib-only): the orchestration backend.
   - Reads `tools.json`; knows how to satisfy `needs` (start/stop
     `mock_rest.py` / `mini_redis.py` / `mock_ws_exchange.py` /
     `mock_server.py` on ephemeral ports).
   - Runs a named tool or the whole `kind=test`+`kind=check` set
     (delegating to `tests/run_pipeline.sh` for the full sweep is fine).
   - Parses the uniform output: counts `PASS:`/`FAIL:` lines, final
     `ALL PASS`/`FAILURES`, captures full log to `work/logs/<name>.txt`.
   - Appends `test_suite` NDJSON records (P0 schema) to
     `work/test_results.ndjson`; maintains `work/test_results_latest.json`
     (map suite → last result) for cheap dashboard load.
   - CLI: `run_tests.py --all`, `--tool NAME`, `--list`, `--json`.
2. `dashboard_server.py` v2 (keep: stdlib-only, 127.0.0.1 bind, SSE,
   read-only wrt trading). Three tabs:
   - **Tests**: grid of suites from `test_results_latest.json` — green/red,
     pass/fail counts, duration, last-run age; click → full log; "Run all" /
     per-suite "Run" buttons (spawn `run_tests.py`, stream output over SSE);
     history sparkline from `test_results.ndjson`.
   - **Tools**: cards from `tools.json` grouped by kind, showing
     description, safety badge, command line, docs link. Run buttons:
     `pure`/`offline` → enabled; `network_read` → enabled only when the
     server was started with `--allow-network` AND per-click confirm;
     `live_order` → rendered with a red badge and NO run affordance,
     server-side refusal as well (guardrail 1 — enforce in the request
     handler, not just the UI).
   - **Live**: the existing metrics.ndjson tail UI, unchanged.
   - New endpoints: `GET /api/tools`, `GET /api/results`,
     `POST /api/run` (name; enforces safety policy server-side),
     `GET /api/run_stream?id=` (SSE of a run's output).
3. tradingd emits `feed` events — VIA THE TELEMETRY THREAD ONLY (guardrail
   8). The feed loop must not write files or build JSON: add a small
   fixed-size `FeedStatus` struct (source id, connected flag, last-msg
   mono_ns, msg count, gap/reconnect counters) pushed onto the existing
   telemetry ring (or a dedicated tiny SPSC ring) at most ~1/s; the
   telemetry worker formats it into
   `{"type":"feed","source":"kalshi_rest",...}` and appends to the same
   NDJSON file it already owns (single writer, no FILE* sharing). The
   dashboard's Feed panel currently renders nothing because nothing emits
   these.
4. `make dashboard` target: `python3 dashboard_server.py --metrics
   work/metrics.ndjson --results work/test_results.ndjson --port 8765`.

New tests: `tests/test_console.py` (stdlib unittest): output-parser unit
tests (PASS/FAIL counting, FAILURES detection, truncated log), safety-policy
tests (live_order refused with 403 even when requested directly; network_read
refused without the flag), registry round-trip. Wire into `run_pipeline.sh`.

Gate: `make dashboard` up; Tests tab shows every suite green after "Run
all"; attempting `POST /api/run {"name":"fill_test"}` returns 403; console
process can be killed mid-run without affecting anything (it only touches
`work/`).

## Phase P3 — CI

**Goal**: nothing merges red.

Tasks:
1. `.github/workflows/ci.yml`, Ubuntu 24.04: install `g++-12 libcurl4-openssl-dev
   libssl-dev python3`; `make -j`, then `./tests/run_pipeline.sh`, then
   `make san`, `make tsan`, `make fuzz` (bounded, e.g. 50k iters via arg).
   Upload `work/test_results.ndjson` + logs as artifacts.
2. Add the P4 spec-drift check as a separate NON-blocking scheduled job
   (weekly cron) once it exists — drift alerts must not block unrelated PRs.
3. Badge in README.

Gate: CI green on a PR that touches a source file; a deliberately broken
test turns it red (verify once, then revert).

## Phase P4 — Spec drift, rulebook, cost refresh (close TOKEN_RULES T-items)

**Goal**: the protocol-drift and metadata-refresh machinery the plans mandate.

Tasks:
1. Vendor specs: `docs/vendor/kalshi/latest/openapi.yaml` + `asyncapi.yaml`
   + `FETCHED_AT` stamp (fetch from docs.kalshi.com).
2. `tools/check_spec_drift.sh`: re-fetch, diff against vendored, nonzero
   exit + summary on change. Watch specifically (grep the diff): the
   `use_yes_price` flag (I5 — code pins `kUseYesPrice=false` and Kalshi has
   announced a default flip), the account-limits schema, WS error-code enum,
   endpoint cost paths. Wire into the P3 scheduled CI job.
3. Endpoint-cost refresh (T1.4, currently missing): in `RequestExecutor` or
   a small control-thread helper, re-fetch `/account/endpoint_costs` hourly
   and on any `token_accounting_drift` (unexpected 429 — the stderr hook in
   `src/request_executor.cpp` already fires; add a callback seam there so
   the owner can trigger refresh). No hot-path work.
4. T7 tier-upgrade command: `preflight --upgrade-api-tier` per
   PLAN_TOKEN_RULES (GET limits before → reserve 30 Write tokens via the
   executor → POST upgrade → explain 201/403 → GET limits after, print
   grants delta). Never callable from tradingd.
5. Write `docs/KALSHI_RULEBOOK.md` (T9): RULE-ID / statement / source /
   implemented-in / tested-by / severity for every F1–F12 fact plus the
   batch-cost findings from P5.

New tests: drift-check dry run against fixture copies (modified yaml →
nonzero exit); refresh-on-drift unit test (callback fires on synthetic 429
via mock_rest scenario); upgrade-command tests against `mock_rest.py`
extended with the upgrade endpoint (201 + 403 variants, 30-token Write
reservation asserted via bucket introspection).

Gate: `make check` green incl. new tests; scheduled CI drift job runs;
rulebook committed.

## Phase P5 — Empirical demo verification campaign

**Goal**: burn down every "verify empirically, do not assume" item. Needs a
demo API key; all read-only.

Tasks (record every result in `docs/KALSHI_RULEBOOK.md` + protocol doc):
1. `preflight` against demo: creds, clock skew, market-data parse.
2. Batch-orderbook token cost: `tools/probe_batch_cost.sh` at a known tier,
   batch sizes 1/10/50/100 → infer per-item vs per-request billing; replace
   the F8 ASSUMPTION comment in `src/rest_api.cpp` with the measured fact
   (keep the conservative math if inconclusive).
3. `get_snapshot` seq semantics: scripted demo session — subscribe, force
   `update_subscription get_snapshot`, log the snapshot's seq vs the delta
   stream (extend `ws_shadow` with a `KALSHI_SHADOW_GETSNAP=1` mode that
   issues one mid-session get_snapshot). Confirm or fix SidStream's
   treatment (currently: snapshot seq observed as a normal sequenced msg).
4. `ws_shadow` demo soak: hours-long run, then the ≥24 h run with
   `KALSHI_SHADOW_XCHECK=1`. Required outcome: 0 transmitted, 0 write-bucket
   spend, recorder COMPLETE (or loss markers explained), missed-pong
   disconnects = 0, xcheck divergence within staleness bounds.
5. `bench_rtt` from the intended deploy region (also feeds P10 placement).

Gate: all five recorded with numbers in the rulebook; any code change they
force (e.g. SidStream get_snapshot accounting) lands with tests before P6.

## Phase P6 — WS feed into tradingd

**Goal**: replace ~250 ms REST-poll staleness with the WS book as the
engine's feed. Read-side only; the submit path is untouched.

Design decision (make it explicit in code comments): tradingd's strategy API
stays `wire::MarketEvent` for this phase (adapter approach). Full migration
of strategies onto `trading::NormalizedEvent`/bus interfaces is a separate
later refactor — do not couple it to the feed swap.

Tasks:
1. `apps/ws_feed.hpp`: `feed::run_ws(rt, client, tickers, on_event)`
   mirroring `run_poll`'s shape (blocking loop until `g_stop`). Internally:
   `IxWebSocketTransport` + `KalshiWsClient` + `OrderBookManager` +
   `WsRecorder` (capture path from env `TRADINGD_WS_CAPTURE`, default off).
   - Threading per the bus contract: the transport thread does decode + book
     apply, then converts top-of-book changes into `wire::MarketEvent`
     (kind=ticker: yes_bid/yes_ask from `best_yes_bid()`/`implied_yes_ask()`,
     e4→cents) and pushes onto an SPSC `kalshi::Ring<MarketEvent>` (cap from
     `TRADINGD_WS_RING`, default 4096, overflow = drop+count, never block).
   - The dispatch (main) thread pops and invokes the handler — strategies
     keep running OFF the socket thread, same as today.
   - Only VALID books emit events (invalid/resyncing books are silent —
     strategies never see a corrupt top-of-book).
   - Recovery: `RecoveryLadder` wired with actions using the client's
     builders; ping-silence watchdog on the dispatch loop.
2. `tradingd --ws` mode: `KALSHI_WS_TICKERS` (explicit list; ASSUMPTION:
   dynamic discovery out of scope this phase), falls back to `--poll` if WS
   URL unresolvable. `resolve_runtime()` already validates the WS URL.
3. Staleness stamps: `EventTiming.received_steady_ns` = frame receive time
   (recorder already stamps once), `parsed_steady_ns` = post-book-apply, so
   the full-chain latency probe compares REST vs WS honestly.
4. Feed telemetry: `feed` NDJSON events (P2.3) now report the WS source:
   connected, epoch, reconnects, gaps, drops.

New tests: `tests/test_ws_feed.cpp` against `MockWebSocketTransport`:
snapshot+delta → exactly one MarketEvent per top-of-book CHANGE (not per
delta); invalid book emits nothing until re-validated; ring overflow drops
and counts; epoch bump mid-stream produces no stale events. TSan target for
the 2-thread (transport/dispatch) handoff. Extend `run_ws_smoke.sh` to also
run tradingd --ws for ~2 s against the mock and assert events flowed.

Gate: `make tsan` clean on the new handoff; mock-driven soak (extend
`mock_ws_exchange.py` with a scripted gap) recovers per the ladder without
strategy-visible corruption; demo run shows measured event staleness ≪ the
250 ms REST baseline (record numbers in the rulebook).

## Phase P7 — T8: executor on the order path + risk layer + kill switch

**Goal**: the order path draws from the server-derived Write bucket, and a
real risk gate exists. STILL no change to what gets transmitted (shadow
continues to shadow; live continues to work exactly as today for the lane
path — this phase changes gating/accounting, not transmission mechanics).

Tasks:
1. Startup self-config in tradingd: build a `RestApi` on the existing
   client, fetch `account_limits()` + `endpoint_costs()`, configure a shared
   `RequestExecutor`. Live-mode preconditions (fail closed at startup):
   runtime valid, limits from_server (or explicit conservative opt-in for
   demo), cost table loaded, Write bucket initialized.
2. Route lane submissions' ACCOUNTING through the executor's Write bucket:
   `process_order()` reserves `cost(POST /portfolio/events/orders)` tokens
   (non-blocking `try_reserve`; refusal = drop with telemetry kind
   `RateLimited`, consistent with today's semantics). Keep the existing
   sign+`Lane::send` mechanics untouched. ASSUMPTION note: full
   send-through-executor unification is deferred to P8 where transmission
   is open for modification.
3. Fold `TRADINGD_MAX_ORDERS_PER_SEC` into the bucket policy: if both set,
   stricter applies; document in README + env example.
4. `include/trading/risk.hpp` — `RiskGate` (pure, unit-testable):
   - per-market position + open-order caps (`RISK_MAX_POSITION_PER_MARKET`,
     `RISK_MAX_OPEN_ORDERS`), global notional cap (`RISK_MAX_NOTIONAL_CENTS`),
     per-strategy order budget;
   - kill switch: `RISK_KILL_FILE` (existence = engaged) + SIGUSR1 toggle.
     NO filesystem checks on the submit path (guardrail 8): a control/
     telemetry-thread watcher polls the file (~100 ms cadence) and the
     signal handler both write one `std::atomic<bool> killed_`; the submit
     worker and RiskGate only do a relaxed atomic load per intent. Engage
     latency ≤ the watcher cadence — document that in the runbook;
   - decision enum {Allow, RejectStale, RejectDuplicate, RejectRisk,
     RejectKilled}; every rejection emits a `risk` NDJSON event (the
     dashboard's Risk panel already renders these fields).
   - Position tracking this phase: from own submissions/acks (fill
     reconciliation via `/portfolio/positions` polling on the telemetry
     thread; ASSUMPTION until a fills WS channel is added).
5. `KalshiExecutionEngine` gains the same RiskGate seam (`set_risk_gate`)
   so both paths share one policy object.

New tests: risk gate pure tests (caps, kill file, duplicate suppression by
client_order_id); tradingd integration: bucket-refused intent emits
RateLimited telemetry and no HTTP (mock server counter, reuse the
`test_request_executor` technique); knob-interaction test (env cap 5/s +
bucket 100/s → 5/s governs). Gates green incl. a new grep gate: no
`Lane::send` call sites outside `process_order`/`Lane::ping`.

Gate: tradingd in shadow mode on demo (WS feed from P6) runs with limits
pulled from the server, risk events visible on the dashboard, kill switch
verified live (touch file → intents rejected within one intent).

## Phase P8 — Live transmission unification + reconcile

**Goal**: ONE gated, reconcile-capable transmit implementation. This phase
unifies all order transmission into a single gated, reconcile-capable path.

Tasks:
1. Reconcile flow (the missing piece behind `reconcile_required`):
   `RestApi::order_by_client_id(coid)` → GET
   `/portfolio/orders?client_order_id=` (verify exact query param against
   the vendored OpenAPI first — P4 artifact). After an ambiguous submit
   outcome (transport error / 5xx on the lane path): mark intent
   `PendingReconcile`, telemetry event, reconcile on the telemetry/control
   thread: found → treat as submitted (adopt order_id); not found after N
   attempts → declare dead, allow ops-initiated resend (never automatic).
2. Unify: `KalshiExecutionEngine::submit` Live arm stops throwing and
   delegates to the same submit machinery tradingd's lanes use (extract the
   sign→send→classify-outcome core from `process_order()` into a shared
   `kalshi::OrderSubmitter` used by both; lanes keep their dedicated-
   connection performance property). Order of gates (unchanged semantics):
   ttl/age → rate/bucket → risk → `require_orders_allowed` → transmit.
3. Cancel support: `OrderSubmitter::cancel(order_id)` (DELETE path already
   exercised by preflight/bench_order) — needed by strategies and the kill
   switch's optional cancel-all-on-engage (`RISK_KILL_CANCELS_OPEN=1`).
4. Extend `tests/mock_rest.py`: order create endpoint with scenario knobs —
   201, 429, 5xx-after-accept (returns 504 but records the order so the
   reconcile lookup finds it), timeout (sleep past client timeout, record
   order). This is the harness that proves no-duplicate-on-ambiguity.

New tests: `tests/test_order_path.cpp` against the extended mock: happy
place+cancel; ambiguous-timeout → exactly ONE order server-side and
reconcile adopts it; 429 → drop (no server order); risk-reject → no HTTP;
shadow mode → no HTTP ever (server counter 0). Run in `run_pipeline.sh`.

Gate: full suite green; demo-live validation: `preflight --order` and
`fill_test` on demo, then tradingd live on demo with a throwaway 1-contract
strategy (P9 test double) placing post-only 1c bids — every order visible on
the dashboard, canceled cleanly, reconcile path exercised at least once by
a forced timeout (mock; on demo it's opportunistic).

## Phase P9 — Strategy framework, shadow PnL, replay backtest

**Goal**: strategies can be developed, backtested on recorded tapes, and
proven in shadow with PnL numbers before touching live.

Tasks:
1. Roster config: `make_strategies()` reads `STRATEGIES` env (comma list of
   registered names) or a JSON config path; unknown name = startup failure
   (fail closed). Registry pattern: static factory map in
   `src/strategies.cpp`; each strategy gets a fixed slot id (0..29) declared
   with its registration (client_order_id determinism depends on stable ids
   — document this).
2. Shadow PnL accountant (`include/trading/shadow_pnl.hpp`): consumes
   would-be orders + subsequent market data; fill model: post-only limit
   fills when the book trades through the price (from WS trade/delta
   stream); marks to top-of-book; emits per-strategy `strategy` NDJSON
   events (`triggers`, `edge_signal_cents`, `shadow_pnl_cents` — dashboard
   Strategy panel already renders them).
3. `apps/backtest.cpp`: ReplaySource(capture.ndjson) → OrderBookManager →
   strategies → MockExecutionEngine + shadow PnL; deterministic (same tape +
   roster → identical PnL, assert via checksum); speed: as-fast-as-possible
   with virtual clock derived from `recv_mono_ns` deltas for ttl semantics.
4. First real strategy lands behind this harness; acceptance = positive
   expectancy on recorded demo tapes + clean shadow run. (Strategy logic
   itself is out of scope for this plan doc.)

New tests: fill-model unit tests (trade-through fills, no-fill when price
never crossed, partial fills by size); backtest determinism test (tape
fixture → exact expected PnL cents); roster config tests (unknown name
refused; slot collision refused).

Gate: `backtest` on a recorded ws_shadow tape reproduces identical results
across two runs; tradingd shadow on demo shows strategy + PnL panels live.

## Phase P10 — Deploy + canary rollout

**Goal**: production, gradually.

Tasks:
1. Box in the region `bench_rtt` selected (P5). `deploy/bootstrap.sh` run
   (now unbroken per P0); chrony verified (`chronyc tracking`); key at
   `/etc/kalshi/private_key.pem` 600; `/etc/kalshi/tradingd.env` from the
   example; systemd unit enabled. Add logrotate config for `work/*.ndjson`
   and capture files (`deploy/logrotate.d/kalshi`).
2. Ops runbook `docs/RUNBOOK.md`: start/stop/drain, kill switch, how to read
   each dashboard panel, alert conditions + responses (heartbeat gap, 401
   streak = clock skew, error-25, recorder drops, reconcile pending),
   incident checklist (engage kill → cancel open → diagnose via NDJSON).
3. Alerting: `tools/alert_watch.py` (stdlib) tails metrics.ndjson, applies
   the runbook's threshold rules, notifies (exec a configurable command —
   ASSUMPTION: notification transport is operator-supplied).
4. Canary ladder — each step runs until its exit criteria hold, gated by
   dashboard evidence, one variable at a time:
   a. prod `data_collect` (WS feed + recorder only) — ≥24 h clean (the P5
      soak may already satisfy this);
   b. prod `shadow` with the real roster — shadow PnL sane vs backtest;
   c. demo `live` with the real roster — orders/cancels/reconcile clean;
   d. prod `live`, caps floored: `RISK_MAX_POSITION_PER_MARKET=1`,
      post-only where the strategy allows, `TRADINGD_MAX_ORDERS_PER_SEC`
      per tier; e. raise caps stepwise.
5. Ops cadence: weekly spec-drift review; hourly cost refresh already live
   (P4); periodic `run_pipeline.sh` on the deploy box after any pull.

Gate: canary step (d) completes a full session with zero unexplained
telemetry anomalies and PnL/fills reconciled against
`/portfolio/fills`; caps raised only after that.

---

## Cross-phase acceptance criteria (the definition of "production system")

- One command (`tests/run_pipeline.sh`) proves the whole offline suite; CI
  enforces it; the dashboard shows it.
- Every tool is registered, safety-classed, and runnable (or deliberately
  not) from the console.
- Zero ASSUMPTION comments remaining for facts that P5 measured.
- The order path: WS-fed decisions → risk gate → server-derived Write
  bucket → single gated submitter → reconcile on ambiguity → telemetry at
  every step — with tests proving no-duplicate and no-bypass (grep gates).
- Shadow PnL and backtest agree on recorded tapes; live results reconcile
  against exchange fills.
- Kill switch verified in production. Runbook exists and has been used.

## Suggested Claude Code session boundaries

Each phase is sized for one focused session (P2 and P8 may take two). Start
each session by reading this file's phase, the referenced sources, and
running `tests/run_pipeline.sh` to confirm a green baseline. End each
session with the phase gate demonstrably passing and both build systems in
sync. Do not batch phases: P7/P8 in particular must land separately so the
transmission-touching diff (P8) is reviewable in isolation.
