# Agent Execution Plan — Kalshi Market-Data Pipeline Audit

> **Generated**: 2026-07-05 from full system audit.
> **Prior audit findings are baked in** — this plan incorporates known root causes
> so the agent doesn't re-derive them, but still verifies each claim independently.

---

## Role & Constraints

**Role**: Senior HFT Systems Auditor + Kalshi API Integration Engineer.

**Safety — hard rules (non-negotiable)**:

- NEVER run `fill_test`, `bench_order`, `tradingd live`, `account_upgrade`, or
  any tool with `"safety": "live_order"` in `tools.json`.
- NEVER place orders or call any Kalshi write endpoint.
- NEVER print API keys, private key contents, or auth signatures.
- Read-only network checks (`network_read` safety class) are allowed ONLY when
  `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` are explicitly present in the
  process environment.
- Dashboard and tooling must remain off the trading hot path.

**Credential handling**: check for env var presence with `[ -n "$KALSHI_API_KEY_ID" ]`,
never `echo` or `cat` key contents.

---

## Known Root Cause (from prior audit — verify, don't assume)

The live feed shows no data because `ws_shadow` defaults to
`KALSHI_WS_TICKERS="MKT-A"` (a mock-only ticker). When run against prod Kalshi
without explicit `--tickers`, it subscribes to a non-existent market. Kalshi's WS
server silently accepts the subscription but sends zero messages.

The `exchange_check.sh` auto-discovery failed because `preflight`'s
`GET /markets` returned no parseable tickers (credential or timing issue at that
moment), so the script aborted before ws_shadow ever ran with real tickers.

**Classification**: Category 3 — Internal pipeline/tooling issue.

---

## Execution Phases

### Phase 1 — Read Documentation & Config (no side effects)

**Goal**: Build a mental model of the pipeline before touching anything.

| Step | File | What to extract |
|------|------|-----------------|
| 1.1 | `docs/CURRENT_STATE.md` | What is real today, what is not a live signal, execution paths |
| 1.2 | `docs/ARCHITECTURE.md` | Two execution paths, thread model, data flows, env vars, on-disk formats |
| 1.3 | `docs/PLAN_LIVE_VALIDATION.md` | Phase plan, existing components, validation gates |
| 1.4 | `tools.json` | Full tool registry: name, safety class, cmd, pass_token. Memorize which are `live_order` (forbidden) vs `network_read` (conditional) vs `pure`/`offline` (safe) |
| 1.5 | `include/kalshi/env.hpp` | `Runtime` struct, `Env`/`Mode` enums, `can_place_orders()`, `require_orders_allowed()` — the safety gate definitions |
| 1.6 | `src/env.cpp` | `resolve_runtime()` — how env vars map to Runtime, host allowlists, TLS enforcement |

**Decision gate**: After this phase, you must be able to answer: "What are the
two execution paths, and which one is the live transmitter?" If not, re-read.

---

### Phase 2 — Audit Pipeline Source Code (no side effects)

**Goal**: Trace the exact data flow from Kalshi WS → dashboard.

Read these files in order. For each, note: inputs, outputs, failure modes.

| Step | File | Focus |
|------|------|-------|
| 2.1 | `include/kalshi/ws_client.hpp` | `WsConfig` struct (especially `use_yes_price`, default `channels_`), `want_orderbook()`, `want_channels()` |
| 2.2 | `src/ws_client.cpp` | `build_subscribe()` — what JSON goes to Kalshi. `on_open()` → `resubscribe()`. `on_text()` message routing. **Key**: what happens if tickers don't exist on the exchange? |
| 2.3 | `apps/ws_shadow.cpp` | Line ~356: `env_or("KALSHI_WS_TICKERS", "MKT-A")` — **this is the root cause**. Trace: how does it write to metrics? What does `write_feed_status()` emit? What does `write_market_line()` emit? What is `ShadowSink`? |
| 2.4 | `tools/exchange_check.sh` | 4 steps: preflight → ws_shadow → verify_ws_capture → verify_feed_metrics. **Key**: lines 55–69 — ticker auto-discovery from preflight output. If discovery fails, script exits before ws_shadow. |
| 2.5 | `tools/feed_readiness.py` | `collect_status()` — what makes `status="active"` vs `"ready"` vs `"missing_prerequisites"`. Note: requires `real_feed` AND `real_market` rows AND `latest_age <= 5000ms` AND `connected=True` |
| 2.6 | `tools/verify_feed_metrics.py` | Checks: non-synthetic `kalshi_ws` feed rows exist + market_data rows exist |
| 2.7 | `tools/verify_ws_capture.py` | Checks: capture has frames, snapshot coverage, per-sid seq continuity |
| 2.8 | `tools/lifecycle_check.py` | 7 stages: kalshi_api_updates → api_spec_alignment → connection_exchange → core_tests → data_pipeline → strategy_shadow → live_execution. Dependency chain enforced. |
| 2.9 | `tools/warehouse_status.py` | `classify_input()` — how it detects synthetic vs real. `collect_status()` — row counts, schema check, missing categories |
| 2.10 | `tools/warehouse_convert_capture.py` | Input: raw capture NDJSON. Output: Parquet partitions in `work/warehouse/`. Read enough to know the interface. |
| 2.11 | `dashboard_server.py` | `/stream` SSE endpoint tails `work/metrics.ndjson`. `/api/feed_readiness`. `/api/warehouse`. `/api/lifecycle`. POST `/api/run` safety enforcement (`may_run()` server-side). |

**Decision gate**: You must now be able to draw the pipeline:
```
Kalshi WS → ws_shadow subscribe(tickers) → ShadowSink → FeedLine ring →
  write_market_line() → metrics.ndjson (type=market_data)
  write_feed_status() → metrics.ndjson (type=feed)
→ dashboard /stream SSE → Feed Status panel + Market Feed Tape
→ (optional) warehouse_convert_capture → Parquet
```

---

### Phase 3 — Run Safe Local Checks

**Goal**: Confirm offline path is green, identify what's broken in the real path.

Run these commands **in this order**. All are `pure` or `offline` safety class.

```bash
cd /path/to/repo

# 3.1 — Feed readiness (pure, no network)
python3 tools/feed_readiness.py --json
# Expected: status="missing_prerequisites" if no creds, or "ready" if creds present but no active feed.
# Check: real_feed_rows, real_market_rows, latest_real_age_ms

# 3.2 — Warehouse status (pure, no network)
python3 tools/warehouse_status.py --json
# Expected: source_kind="synthetic_fixture", input="build/scratch/demo_capture.ndjson"
# This confirms warehouse is NOT real data.

# 3.3 — Lifecycle check (offline, no network)
python3 tools/lifecycle_check.py --json
# Expected: data_pipeline="not_started", live_execution="blocked"
# Check each stage status and blocking_reason.

# 3.4 — make check (offline tests + gates)
make check
# Expected: ALL PASS (may show "Exec format error" in sandbox — ignore if macOS binaries in Linux sandbox)

# 3.5 — Full offline pipeline
tests/run_pipeline.sh
# Expected: PIPELINE PASS. If it fails, record which suite failed.

# 3.6 — WS shadow mock path
bash tests/run_ws_shadow_mock.sh
# Expected: WS SHADOW MOCK PASS. This proves the mock path works end-to-end.
```

**Record for each**: exit code, key output fields, pass/fail.

**Decision gate after 3.6**: If mock path passes but feed_readiness shows
`real_market_rows=0`, the problem is NOT the pipeline code — it's the input
(tickers/credentials/connection).

---

### Phase 4 — Analyze Existing Metrics Evidence

**Goal**: Determine what the last real run actually did.

```bash
# 4.1 — How many rows, what types?
wc -l work/metrics.ndjson
python3 -c "
import json
with open('work/metrics.ndjson') as f:
    rows = [json.loads(l) for l in f if l.strip()]
types = {}
for r in rows:
    t = r.get('type','?')
    types[t] = types.get(t, 0) + 1
print('Row counts by type:', types)
market = [r for r in rows if r.get('type')=='market_data']
print('market_data rows:', len(market))
feed = [r for r in rows if r.get('type')=='feed']
print('Total feed rows:', len(feed))
msgs = set(r.get('messages', -1) for r in feed)
print('Unique message counts in feed rows:', msgs)
# If msgs == {0}, no WS data messages were ever received.
"

# 4.2 — Check timestamps and freshness pattern
python3 -c "
import json, datetime
with open('work/metrics.ndjson') as f:
    rows = [json.loads(l) for l in f if l.strip()]
feed = [r for r in rows if r.get('type')=='feed']
for i, r in enumerate(feed):
    ts = datetime.datetime.utcfromtimestamp(r['ts_ms']/1000).isoformat()
    print(f'  [{i}] {ts} connected={r[\"connected\"]} msgs={r[\"messages\"]} fresh={r[\"freshness_ms\"]}ms')
"

# 4.3 — Check capture file
wc -l work/exchange_check_capture.ndjson
# If 0 bytes: ws_shadow connected but received nothing.

# 4.4 — Check exchange_check log
cat work/logs/exchange_check.txt
# Look for: "could not auto-discover open-market tickers"
```

**Key diagnostic**: If feed rows show `connected=True, messages=0` throughout,
the WS connection was alive but no market data arrived. Combined with default
ticker `MKT-A`, this confirms the root cause.

---

### Phase 5 — Credential & Environment Check

**Goal**: Determine if credentials exist for a real exchange check.

```bash
# 5.1 — Check env vars (presence only, never print values)
[ -n "$KALSHI_API_KEY_ID" ] && echo "KALSHI_API_KEY_ID is set" || echo "KALSHI_API_KEY_ID is MISSING"
[ -n "$KALSHI_PRIVATE_KEY_PATH" ] && echo "KALSHI_PRIVATE_KEY_PATH is set ($KALSHI_PRIVATE_KEY_PATH)" || echo "KALSHI_PRIVATE_KEY_PATH is MISSING"
[ -n "$KALSHI_PRIVATE_KEY_PATH" ] && [ -f "$KALSHI_PRIVATE_KEY_PATH" ] && echo "key file exists" || echo "key file does NOT exist"

# 5.2 — Check for key files in standard locations (existence only)
ls -la ~/.kalshi/*.pem 2>/dev/null || echo "no keys in ~/.kalshi/"
```

**Branch point**:
- If credentials are missing → **skip Phase 6**, classify as "Category 2 —
  credential/env issue compounds Category 3", proceed to Phase 7.
- If credentials exist → proceed to Phase 6.

---

### Phase 6 — Read-Only Exchange Check (CONDITIONAL — only if creds exist)

**Goal**: Prove Kalshi API is reachable and collect real market data.

**CRITICAL**: You MUST supply real tickers. Do NOT rely on auto-discovery if it
has failed before. Get ticker names from Kalshi.com first, or use the preflight
discovery flow with explicit fallback.

```bash
# 6.1 — Option A: Run exchange_check with explicit tickers
KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 \
KALSHI_API_KEY_ID="$KALSHI_API_KEY_ID" \
KALSHI_PRIVATE_KEY_PATH="$KALSHI_PRIVATE_KEY_PATH" \
tools/exchange_check.sh --env prod \
  --tickers <REAL_TICKER_1>,<REAL_TICKER_2> \
  --ws-seconds 30

# 6.2 — Option B: If you don't know real tickers, run preflight alone first
KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 \
KALSHI_API_KEY_ID="$KALSHI_API_KEY_ID" \
KALSHI_PRIVATE_KEY_PATH="$KALSHI_PRIVATE_KEY_PATH" \
./build/preflight
# Parse the "market data parses (GET /markets) — TICKER1 (...) TICKER2 (...)" line
# Then run exchange_check.sh with --tickers using those discovered tickers.

# 6.3 — After exchange_check, verify results
python3 tools/verify_ws_capture.py work/exchange_check_capture.ndjson \
  --tickers <TICKERS_USED>
python3 tools/verify_feed_metrics.py work/metrics.ndjson \
  --source kalshi_ws --capture work/exchange_check_capture.ndjson \
  --tickers <TICKERS_USED>
python3 tools/feed_readiness.py --json
```

**Success criteria**: `verify_feed_metrics` shows `PASS` for both feed AND
market_data rows. `feed_readiness` shows `status="active"`.

---

### Phase 7 — Audit Safety Gates & Mode Blocks

**Goal**: Confirm no obsolete blockers exist; all real safety gates are intact.

| Check | File(s) | What to verify |
|-------|---------|----------------|
| 7.1 Env resolution | `src/env.cpp` | `resolve_runtime()` defaults to `local_mock + data_collect`. Prod requires `KALSHI_ALLOW_PROD`. Live requires `KALSHI_ALLOW_LIVE`. No blanket blockers. |
| 7.2 Order gates | `include/kalshi/env.hpp` | `can_place_orders()` = Live && allow_live && Prod (the demo env is rejected fail-closed and no longer a valid environment). `require_orders_allowed()` throws otherwise. |
| 7.3 Exec engine | `src/gateway.cpp` | DataCollect→Rejected, Shadow→Logged, Live→throws "not implemented" (fail-closed skeleton). |
| 7.4 tradingd gate | `apps/tradingd.cpp` | `engine.orders_enabled = can_place_orders(rt)`. If false, orders go to shadow log. |
| 7.5 ws_shadow gate | `apps/ws_shadow.cpp` | Refuses `mode=live`. Asserts `can_place_orders(rt)==false`. |
| 7.6 Dashboard policy | `tools/run_tests.py` `may_run()` | `live_order` → always refused. `network_read` → only with `--allow-network`. |
| 7.7 check_gates.sh | `tools/check_gates.sh` | No host-sniffing, no banned endpoints, no secrets in logs, registry valid. |

```bash
# 7.8 — Run the gate check
bash tools/check_gates.sh
# Expected: all "gate ok", exit 0
```

**Grep for obsolete blockers**:
```bash
grep -rnI 'demo.only\|FROZEN\|DISABLED.*block\|TODO.*remove.*block' \
  src/ include/ apps/ --include='*.cpp' --include='*.hpp'
```
Expected: no hits in live code (tests/docs may mention these strings legitimately).

**Verdict template**:
- "No obsolete blockers found" — if grep returns nothing in live code.
- "Obsolete blocker at FILE:LINE" — if something is found, detail what it blocks
  and whether it should be removed.

---

### Phase 8 — Compile Report

**Goal**: Answer every audit question with evidence.

Fill in this template:

```
## Pipeline Map

| # | Stage | File/Tool | Input | Output | Status | Evidence |
|---|-------|-----------|-------|--------|--------|----------|
| 1 | Kalshi WS API | wss://external-api-ws.kalshi.com | auth headers | WS frames | ? | ? |
| 2 | ws_shadow subscribe | apps/ws_shadow.cpp | tickers + channels | orderbook/trade/ticker | ? | ? |
| 3 | Raw Capture | KALSHI_SHADOW_CAPTURE | WS frames | NDJSON | ? | ? |
| 4 | Metrics NDJSON | write_feed_status + write_market_line | feed + market events | work/metrics.ndjson | ? | ? |
| 5 | Dashboard Feed | dashboard_server.py /stream | metrics.ndjson | SSE → browser | ? | ? |
| 6 | Warehouse | warehouse_convert_capture.py | capture NDJSON | Parquet | ? | ? |

## Failure Classification

- [ ] Category 1: Kalshi API / exchange issue
- [ ] Category 2: credential / environment issue
- [ ] Category 3: internal pipeline / tooling issue
- [ ] Category 4: dashboard display issue
- [ ] Category 5: synthetic / fixture warehouse confusion

## Root Cause
<one paragraph>

## Kalshi API Reached?
yes / no — evidence: <quote metrics or logs>

## Fresh Non-Synthetic market_data in metrics?
yes / no — evidence: real_market_rows=<N>

## Warehouse Data Real or Synthetic?
<source_kind from warehouse_status.py>

## Orders Sent?
CONFIRMED: zero orders sent — evidence: <quote exec.transmitted(), metrics grep>

## Next Safe Command
<exact bash command>

## Required Fixes (priority order)
1. ...
2. ...
```

---

## Decision Tree Summary

```
START
  │
  ├─ Phase 1–2: Read docs + code → understand pipeline
  │
  ├─ Phase 3: Run offline checks
  │    ├─ mock path fails? → internal code issue (Category 3)
  │    └─ mock path passes → input/connection issue, continue
  │
  ├─ Phase 4: Analyze existing metrics
  │    ├─ messages > 0, market_data > 0? → data exists, check dashboard (Cat 4)
  │    ├─ messages = 0, connected = true? → bad tickers or silent rejection (Cat 3)
  │    └─ connected = false? → auth/network issue (Cat 1 or 2)
  │
  ├─ Phase 5: Check credentials
  │    ├─ missing? → Category 2, advise operator to set env vars
  │    └─ present? → Phase 6
  │
  ├─ Phase 6: Exchange check with REAL tickers
  │    ├─ market_data rows appear? → pipeline works, previous run had wrong tickers (Cat 3, resolved)
  │    ├─ connected but still 0 messages? → Kalshi API issue or ticker validity (Cat 1)
  │    └─ connection refused / auth error? → Category 1 or 2
  │
  ├─ Phase 7: Safety gate audit → confirm no obsolete blocks
  │
  └─ Phase 8: Compile report with evidence
```

---

## Files Quick-Reference

### Must-Read (pipeline-critical)
- `apps/ws_shadow.cpp` — the data collector binary
- `include/kalshi/ws_client.hpp` + `src/ws_client.cpp` — WS protocol client
- `tools/exchange_check.sh` — orchestrates the full read-only check
- `tools/feed_readiness.py` — dashboard feed status source
- `tools/verify_feed_metrics.py` — validates metrics completeness
- `dashboard_server.py` — the ops console

### Must-Read (safety-critical)
- `include/kalshi/env.hpp` + `src/env.cpp` — runtime resolution + safety gates
- `src/gateway.cpp` — execution engine mode dispatch
- `apps/tradingd.cpp` — order transmission gate
- `tools/run_tests.py` — console safety policy (`may_run()`)
- `tools/check_gates.sh` — grep-based safety gates

### Reference (read if needed)
- `tools/warehouse_status.py` — warehouse panel data
- `tools/warehouse_convert_capture.py` — capture → Parquet converter
- `tools/lifecycle_check.py` — lifecycle readiness checker
- `tools/verify_ws_capture.py` — capture structural validator
- `tests/run_ws_shadow_mock.sh` — mock end-to-end path
- `tests/run_pipeline.sh` — full offline suite

### Never Run
- `apps/fill_test.cpp` — places real orders
- `apps/bench_order.cpp` — places real orders
- `apps/account_upgrade.cpp` — mutates account
- `apps/tradingd.cpp` with `KALSHI_MODE=live` — the live engine

---

## Environment Variables Cheat Sheet

| Variable | Required for | Default | Notes |
|----------|-------------|---------|-------|
| `KALSHI_ENV` | all | `local_mock` | `prod` for the real exchange (`demo` is rejected fail-closed) |
| `KALSHI_ALLOW_PROD` | prod | unset | must be truthy for `KALSHI_ENV=prod` |
| `KALSHI_MODE` | tradingd/ws_shadow | `data_collect` | `shadow` logs would-be orders; `live` transmits |
| `KALSHI_ALLOW_LIVE` | live mode | unset | must be truthy for `KALSHI_MODE=live` |
| `KALSHI_API_KEY_ID` | signed requests | unset | presence check only, never print |
| `KALSHI_PRIVATE_KEY_PATH` | signed requests | unset | file path, never read contents |
| `KALSHI_WS_TICKERS` | ws_shadow | `MKT-A` | **⚠ default is mock-only — always override for real runs** |
| `KALSHI_SHADOW_SECONDS` | ws_shadow | `30` | session duration |
| `KALSHI_SHADOW_CAPTURE` | ws_shadow | `ws_shadow_capture.ndjson` | raw capture output path |
| `KALSHI_SHADOW_METRICS` | ws_shadow | `work/metrics.ndjson` | dashboard metrics output path |
| `KALSHI_SHADOW_XCHECK` | ws_shadow | `0` | enable REST cross-check at exit |
| `KALSHI_WS_CHANNELS` | ws_shadow | `orderbook_delta` | channels to subscribe |

---

## Post-Audit: Recommended Fixes (Priority Order)

1. **[P0] Fix ticker defaulting** — `ws_shadow.cpp` line 356: change default from
   `MKT-A` to empty string, and fail-closed if no tickers are configured for
   non-local_mock environments. This prevents silent no-data runs.

2. **[P0] Fix exchange_check.sh auto-discovery** — when preflight ticker parsing
   fails, the script should emit a more actionable error (e.g., suggest checking
   credentials or manually specifying `--tickers`).

3. **[P1] Run exchange_check with real tickers** — the next operator action:
   ```bash
   KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 \
   KALSHI_API_KEY_ID="$KALSHI_API_KEY_ID" \
   KALSHI_PRIVATE_KEY_PATH="$KALSHI_PRIVATE_KEY_PATH" \
   tools/exchange_check.sh --env prod \
     --tickers <REAL_OPEN_TICKER_1>,<REAL_OPEN_TICKER_2> \
     --ws-seconds 30
   ```

4. **[P1] Re-run warehouse converter** with real capture data once exchange_check
   passes:
   ```bash
   python3 tools/warehouse_convert_capture.py \
     --input work/exchange_check_capture.ndjson \
     --warehouse work/warehouse --run-id prod-first --replace-run
   ```

5. **[P2] Run lifecycle_check with network** to fill all stages:
   ```bash
   python3 tools/lifecycle_check.py --allow-network --run-core-tests --json
   ```
