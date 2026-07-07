# Discovery Report — Kalshi Ground Truth & System State (2026-07-07)

**Phase-2 pre-work; feeds the World A/B merge design; no gate is satisfied or
skipped by this mission** (discovery plan amendment A1,
docs/plan_audits/2026-07-06_discovery_plan.md — all 7 amendments honored).

Executor: WP-05 (docs/EXECUTION_PLAN.md). Research, not code: the contract is
the evidence-tag system, not TDD. Read-only on all production code and data;
throwaway probes live in `sandbox/discovery/` (kept until this report is
ACCEPTED, per A6). Zero order transmissions. Zero engine env-gate changes.

**Evidence tags:**
`[VERIFIED-LIVE]` live API response, snippet included ·
`[VERIFIED-CODE]` file:line ·
`[VERIFIED-MEASURED]` method + numbers ·
`[DOCS-ONLY]` URL / local spec snapshot ·
`[ASSUMED]` flagged loudly.
Live beats docs; both are recorded where they conflict.

**Machine-readable output:** `config/kalshi_facts.yaml` (fees, rate limits,
endpoint existence, demo status, RTT) — downstream code imports it; no fee or
rate limit is ever hardcoded again.

**Probe / token cost log (A4):** authenticated prod requests this mission:
account_info ×1 (2 reads ≈ 20 tokens), signed header probe ×1 (GET /markets,
10 tokens), bench_rtt 20 (21 signed GETs ≈ 210 tokens) ≈ **240 read tokens
total, 0 write tokens**, spread over ~20 minutes (bucket refills 300/s —
negligible). Unauthenticated GETs (2 demo status, 1 prod status, 2 series) are
not billed to our account. One WS connection, one subscribe command, 25 s,
closed cleanly. No POST/DELETE of any kind.

**A6 stop-rule check:** docs-vs-live conflicts found per Part: Part 1 = 2
(demo availability; fee rounding cent→centicent, resolved by newer docs) — no
Part exceeded 3; no scope-stop was triggered.

---

## Part 1 — Kalshi API ground truth

### 1.1 Order lifecycle endpoints (V2 — the only creation surface)

Source: spec snapshot `docs/vendor/kalshi/latest/openapi.yaml`
[DOCS-ONLY, fetched 2026-07-06 from https://docs.kalshi.com/openapi.yaml,
sha256 `557683c9…4bce` per `docs/vendor/kalshi/latest/manifest.json`].

**Legacy `POST /portfolio/orders` is GONE from the current spec.** The spec's
CreateOrderV2 description states the legacy path "will be deprecated no
earlier than May 6, 2026" — that date has passed and the legacy create no
longer appears in `paths`. Our engine already targets V2:
`kCreateOrderPath = "/portfolio/events/orders"`
[VERIFIED-CODE include/kalshi/wire.hpp:155].

| Operation | Method + path | Notes |
|---|---|---|
| Create | `POST /portfolio/events/orders` | |
| Batch create | `POST /portfolio/events/orders/batched` | array of same create objects |
| Cancel | `DELETE /portfolio/events/orders/{order_id}` | no body |
| Batch cancel | `DELETE /portfolio/events/orders/batched` | body: `orders[]` of `{order_id, subaccount?, exchange_index?, market_ticker?}` |
| Amend | `POST /portfolio/events/orders/{order_id}/amend` | requires `ticker, side, price, count`; optional `client_order_id` (original) + `updated_client_order_id` |
| Decrease | `POST /portfolio/events/orders/{order_id}/decrease` | `reduce_by` XOR `reduce_to` |
| Cancel-ALL | **does not exist** | nearest primitives: batch cancel; order-group `trigger`/`delete` cancels all resting orders in the group |

**Create request fields** [DOCS-ONLY openapi.yaml]:
`ticker*`, `side*` ∈ {`bid`,`ask`} (YES-normalized book: `bid` = buy-YES
economics), `count*` (fixed-point contract string, 0–2 dp, fractional
allowed), `price*` (fixed-point dollar string, up to 6 dp accepted; valid
ticks constrained by the market's `price_level_structure`), `time_in_force*`
∈ {`fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`},
`expiration_time` (unix seconds; with GTC ⇒ expiring order),
`post_only` (bool), `self_trade_prevention_type*` ∈ {`taker_at_cross`,
`maker`} (required!), `cancel_order_on_pause` (bool, default false),
`reduce_only` (bool), `client_order_id`, `subaccount` (0 = primary),
`order_group_id`, `exchange_index` (default 0; −1 = auto-route by ticker;
prod currently exposes shard 0 only [VERIFIED-LIVE §1.5]).

**Create response**: `order_id*`, `client_order_id`, `fill_count*`,
`remaining_count*`, `average_fill_price`, `average_fee_paid`, `ts_ms*`
(matching-engine timestamp). Batch create returns per-item results with an
optional `error` per item. Cancel returns `reduced_by*` + `ts_ms*`.

**Price level structures** [DOCS-ONLY
docs.kalshi.com/getting_started/fixed_point_migration.md, fetched live
2026-07-07]: `linear_cent` ($0.01 ticks), `tapered_deci_cent` ($0.001 ticks
below $0.10 and above $0.90), `deci_cent` ($0.001 everywhere). Market
responses carry `price_level_structure` + `price_ranges`.

**Our builder vs spec** [VERIFIED-CODE include/kalshi/wire.hpp:163-194]:
field names and enums match V2 exactly (`ticker, side bid/ask, count, price,
time_in_force, self_trade_prevention_type: taker_at_cross, post_only,
client_order_id`). Deterministic UUID-format `client_order_id` from payload
identity [VERIFIED-CODE wire.hpp:137-151]. **Two limitations found:**
(a) price is formatted `"%d.%02d00"` from integer cents
[VERIFIED-CODE wire.hpp:171-172] — the builder cannot express sub-penny
prices on `deci_cent`/`tapered_deci_cent` markets (sub-penny is 13.4% of
trades in our own data); (b) `count` is an integer — fractional contracts
unsupported. Both fine for Phase 2 start, but must be on the QuoteManager
design list.

Per amendment A2: order-lifecycle *behavior* (acks under load, amend queue
priority, partial-fill sequencing) stays **[DOCS-ONLY]**; real verification is
Phase-4 work via the existing operator-gated preflight `--order`.

### 1.2 WS channels (complete list)

Source: `docs/vendor/kalshi/latest/asyncapi.yaml` [DOCS-ONLY, sha256
`00858d5a…d421`]. Channels:

- Public: `orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`,
  `multivariate_market_lifecycle`
- Private (auth): `fill`, `user_orders` (order created/updated/canceled — the
  "order-status" channel), `market_positions` (fixed-point `_dollars`
  values), `communications` (RFQ flow), `order_group_updates`,
  `cfbenchmarks_value`
- Deprecated: `multivariate`

Commands: `subscribe`, `unsubscribe`, `list_subscriptions`,
`update_subscription` (add_markets/delete_markets), plus CF-benchmarks
variants. Subscribe params: `channels[]*`, optional `market_ticker` /
`market_tickers` (omit ⇒ all your fills/orders/positions),
`send_initial_snapshot`.

**Private-channel subscribe semantics** [VERIFIED-LIVE 2026-07-07, prod,
sandbox/discovery/ws_private_probe.py — subscribe transmits nothing, per A2]:

```
[handshake] HTTP/1.1 101 Switching Protocols
[sent] {"id": 1, "cmd": "subscribe", "params": {"channels": ["fill", "user_orders", "market_positions"]}}
[msg] {"type":"subscribed","id":1,"msg":{"channel":"fill","sid":1}}
[msg] {"type":"subscribed","id":1,"msg":{"channel":"user_orders","sid":2}}
[msg] {"type":"subscribed","id":1,"msg":{"channel":"market_positions","sid":3}}
[ping #1] 23:16:36 payload=b'heartbeat'   (then every ~10s)
[summary] messages=3 server_pings=3 over 25s
```

Findings: one multi-channel subscribe → **one `subscribed` ack per channel**,
each with its own `sid`, all echoing the command `id`; auth = the same
RSA-PSS headers on the HTTP upgrade request, signed over
`ts + "GET" + "/trade-api/ws/v2"` (exactly what our client builds
[VERIFIED-CODE src/ws_client.cpp:32-40]); server pings every 10 s with
payload `heartbeat` (matches our ping-watchdog assumption). No initial
snapshot arrived for `market_positions` (no `send_initial_snapshot`
requested; account has no open positions). Fill *delivery* semantics remain
**[DOCS-ONLY]** per A2 (payload shapes in asyncapi.yaml: `fillPayload` etc.).

### 1.3 Rate limits

**Live account state** [VERIFIED-LIVE 2026-07-07,
`KALSHI_ENV=prod ./build/account_info` — registered `network_read` tool]:

```
usage_tier : advanced
read  bucket: refill_rate=300 tokens/s  bucket_capacity=600
write bucket: refill_rate=300 tokens/s  bucket_capacity=600
grants: 1  - instance=event_contract level=advanced source=manual (permanent)
default_cost = 10 tokens; non-default entries: 12, incl.
  DELETE /trade-api/v2/portfolio/events/orders/:order_id  cost=2
  DELETE /trade-api/v2/portfolio/events/orders/batched    cost=2
  GET    /trade-api/v2/portfolio/orders/:order_id         cost=2
```

Refines the pre-registered fact (A3/EXECUTION_PLAN said "300/s read+write"):
capacity is **600 = two seconds of budget**, i.e. a 2× burst after 2 idle
seconds — exactly the documented Advanced behavior [DOCS-ONLY
docs.kalshi.com/getting_started/rate_limits.md, fetched live 2026-07-07].
Tier ladder (read/write tokens per second): Basic 200/100, Advanced 300/300,
Expert 600/600, Premier 1000/1000, Paragon 2000/2000, Prime 4000/4000,
Prestige 6000/8000; higher tiers earned by 30-day volume share (Earn/Keep
thresholds) or assigned manually. Batch endpoints bill **per item** and the
whole batch must fit the bucket at once — this **confirms** what
`batch_total_cost` treated as a local assumption (F8)
[VERIFIED-CODE include/kalshi/request_spec.hpp:88-92] for writes; batch
*read* billing remains [ASSUMED] per-item (still undocumented).

**Quota headers: none.** A successful signed GET returns no
`X-RateLimit-*` / `Retry-After` [VERIFIED-LIVE 2026-07-07,
sandbox/discovery/rest_headers_output.txt]:

```
HTTP/2 200
date: Tue, 07 Jul 2026 03:16:16 GMT
content-type: application/json
cache-control: public, max-age=15
x-kalshi-cache-hits: 1
```

Docs confirm 429s carry none either, body `{"error": "too many requests"}`,
no penalty/cooldown [DOCS-ONLY rate_limits.md]. Bonus finding: `GET /markets`
is **CDN-cached ~15 s** (`cache-control: public, max-age=15`,
`x-kalshi-cache-hits`) — REST market data can be stale by design; one more
reason Q5 (WS-only trading data) is right.

**Our reserve-before-send machinery** [VERIFIED-CODE]: milli-token integer
buckets with `try_reserve` / `reserve_or_wait` (debt within a deadline)
`include/kalshi/token_bucket.hpp:50,62-81`; every authenticated request is
classified (Read/Write bucket, mutation kind, token cost) via `RequestSpec`
`include/kalshi/request_spec.hpp:57-86`; executor reserves before sending
with a 30 s default budget `include/kalshi/request_executor.hpp:33-44`;
unexpected 429 flags `accounting_drift`, Retry-After honored if it ever
appears (F9) `include/kalshi/backoff.hpp:19,65,86-91`. Matches the
documented regime 1:1.

### 1.4 Fees (seeds kalshi_facts.yaml → unblocks WP-07/WP-09)

**Official schedule** — `https://kalshi.com/docs/kalshi-fee-schedule.pdf`
("Last updated and effective: Feb 5, 2026"). The live URL is bot-gated
(Vercel checkpoint blocks non-browser fetch; also 429s WebFetch). Retrieved
the SAME official URL via Internet Archive snapshot 2026-02-18 (the latest
archived), sha256
`b1a37aa734771f42929ccd9ba90ab845e07303f03f549f1c2f493c08b13d2fc9`,
extracted text at `sandbox/discovery/fee_schedule_text_clean.txt`
[DOCS-ONLY]. Verbatim:

> fees = round up(0.07 x C x P x (1-P)) … round up = rounds to the next cent
>
> Maker Fees: fees = round up(0.0175 x C x P x (1-P)) … Please refer to
> https://kalshi.com/fee-schedule for up-to-date information on which markets
> have maker fees … no fees associated with canceling a resting order.
> There is no settlement fee. There is no membership fee.

**Current rounding supersedes the PDF's "next cent"** [DOCS-ONLY
docs.kalshi.com/getting_started/fee_rounding.md, fetched live 2026-07-07]:
post fixed-point migration, the trade fee is **ceiled to the nearest $0.0001
(centicent)**; a rounding fee then restores the member's balance precision
($0.01 non-direct, $0.0001 direct members), with a per-order accumulator
issuing whole-cent rebates so many small fills converge to the single-fill
fee. Worked examples in the doc (copied to sandbox) include sub-penny and
fractional-contract cases.

**Per-series fee structure is a live lookup, never an assumption**
[DOCS-ONLY openapi.yaml `FeeType` = {`quadratic`,
`quadratic_with_maker_fees`, `flat`}; `Series.fee_type` +
`Series.fee_multiplier`; scheduled changes via `GET /series/fee_changes` and
per-event overrides]. Confirmed in the wild [VERIFIED-LIVE 2026-07-07,
unauthenticated GETs]:

```
GET /trade-api/v2/series/KXBTCD → "fee_type": "quadratic",                 "fee_multiplier": 1
GET /trade-api/v2/series/KXNBA  → "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1
```

**Maker fees are real on sports series** — directly material to our
tennis/baseball edge candidates (GUARDRAILS Q3 upheld). The canonical
constants now live in `config/kalshi_facts.yaml` with `verified: true`
(provenance caveat → OPEN QUESTION 1).

### 1.5 Demo environment — docs vs live vs our engine

- Our engine rejects `KALSHI_ENV=demo` fail-closed **by design**
  [VERIFIED-CODE src/env.cpp:163-166], message: "Kalshi's demo exchange is
  unavailable". Per amendment A2 the gate was NOT touched.
- Kalshi docs describe a demo env with dedicated hosts [DOCS-ONLY
  docs.kalshi.com/getting_started/demo_env.md + api_environments.md, fetched
  live 2026-07-07]: REST `https://external-api.demo.kalshi.co/trade-api/v2`,
  WS `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`; separate
  credentials from prod.
- **Live probe** (sandbox-only unauthenticated GET, allowed by A2)
  [VERIFIED-LIVE 2026-07-07, sandbox/discovery/demo_probe_output.txt]:

```
GET https://external-api.demo.kalshi.co/trade-api/v2/exchange/status → HTTP 200 (0.33s)
{"exchange_active":true, …"exchange_index_statuses":[{index 0…},{index 1…}], "trading_active":true}
GET https://demo-api.kalshi.co/…/exchange/status → HTTP 200 (0.28s)  (same body)
```

**The demo exchange is up and trading-active.** The engine's premise
("unavailable") is stale → CORRECTIONS #1. Note demo runs **two exchange
shards** (indices 0, 1) while prod exposes only shard 0
[VERIFIED-LIVE `GET https://external-api.kalshi.com/trade-api/v2/exchange/status`:
`exchange_index_statuses` = [index 0 only]] — the spec's "currently only 0
supported" holds for prod, and demo appears to be where multi-shard is
staged. Order-lifecycle behavior on demo remains untested (needs separate
demo credentials we do not have); per A2 this stays Phase-4-or-R's-call.

---

## Part 2 — Internal architecture: verify/delta vs ARCHITECTURE_REVIEW_2026-07-06

Baseline: `docs/ARCHITECTURE_REVIEW_2026-07-06.md` (independent-audit
verified). Per A5 this section verifies and deltas — it does not re-derive.
The module map and the Two-Worlds finding **hold**. Spot-verified gap-register
claims (5 of 14):

| Gap # | Claim | Verification |
|---|---|---|
| 1 | No private WS fills/order-status channel in engine | [VERIFIED-CODE] `grep fill\|user_orders\|market_positions src/ws_client.cpp include/kalshi/ws_client.hpp` → zero hits; client subscribes market-data channels only |
| 3 | order_id never captured → can't cancel/reprice | [VERIFIED-CODE apps/tradingd.cpp] (656 lines): response handling logs HTTP status + latency only; `order_id` never parsed from any response body (only outbound `client_order_id` at :453) |
| 6 | post_only not passed on live path | [VERIFIED-CODE apps/tradingd.cpp:295] calls `wire::order_json(m.intent)` — the `post_only` parameter defaults to false and tradingd never sets it (builder supports it, wire.hpp:189) |
| 8 | Order path bypasses reserve-before-send executor | [VERIFIED-CODE apps/tradingd.cpp:10,293-306] pipeline is "rate gates → order_json → RSA-PSS sign → lane.send()" — `RequestExecutor` appears nowhere in tradingd |
| 13 | No fee awareness anywhere in engine | [VERIFIED-CODE] `grep -rn "0.07\|fee" src/ include/` → no fee code exists (only false positives: "feed", "coffee-class" comments) |

**Deltas since the review (2 days of building later):**

1. **Gold layer now EXISTS** — `include/trading/gold_record.hpp` (512-byte
   fixed-layout research records, layout-versioned, mmap-able), gold
   writer/reader + merge iterator + book FSM all landed W2.2–W2.6 with
   red-first TDD [VERIFIED-CODE include/trading/gold_record.hpp:1-17;
   SESSION_LOG 2026-07-06/07 W2.x entries]; `work/gold/` holds 5.1 GB
   [VERIFIED-MEASURED `du -sh`]. The review's L4 description predates this.
2. **First archived day exists** — review said "first real archive lands
   tonight"; it landed: 223 files for date=2026-07-06 (87 L1, 134 trades,
   2 full-book; ~122 MB compressed) under `work/warehouse/facts/`
   [VERIFIED-MEASURED file walk]. Day-06 trades were **rebuilt** after two
   incidents (see Part 6).
3. **WP-03 freshness monitor DONE** (2026-07-07 03:10 UTC, commit 923bfda) —
   the review's "31-minute shard lesson" countermeasure is now permanent.
4. **market_filter.hpp added** (Q7 MVE defense-in-depth)
   [VERIFIED-CODE include/trading/market_filter.hpp exists].
5. Review says engine benchmarks "µs-scale decode" — now measured better:
   **282 ns/msg decode+apply** (below).

**Book-update lag, replay-only per A4** (no live WS from the engine, no
production capture paths touched):

- Decode+apply cost: `./build/bench_ws_decode 200000` →
  **281.9 ns/msg (3.55 M msg/s)** end-to-end JSON→book
  [VERIFIED-MEASURED, local replay bench].
- Feed arrival cadence from an existing capture
  (`work/raw/date=2026-07-06/firehose_12.ndjson`, first 500k lines,
  read-only, epoch-filtered): inter-arrival p50 = 10.2 µs (bursty batches),
  p90 = 20.5 µs, p99 = 62.7 ms, max = 458 ms; mean ≈ 472 events/s
  [VERIFIED-MEASURED recv_mono_ns deltas].
- Conclusion: intra-process book lag (hundreds of ns) is ~5 orders of
  magnitude below both network arrival gaps and the 35.6 ms REST RTT. The
  latency budget for the future maker is dominated by **placement (36 ms
  RTT) and exchange-side queueing, not by our book pipeline**.

(Caveat noted for the record: `firehose_08.ndjson` — the corrupted-window
hour — yields garbage inter-arrival extremes from spliced lines; the clean
hour was used. The ingester already validates these at the boundary, D3.)

---

## Part 4 — Environment

- **Python 3.9.6 system interpreter** — 3.9 reached end-of-life 2025-10-31
  [DOCS-ONLY https://devguide.python.org/versions/]; entire data layer runs
  on it (known debt; migrate on the us-east-1 box).
  Libs [VERIFIED-MEASURED `pip3 list`]: duckdb 1.4.5, numpy 2.0.2,
  pandas 2.3.3, PyYAML 6.0.3, requests 2.32.5. NOT installed: `cryptography`,
  `websockets`, `pypdf`, streamlit (pip install requires operator OK) — the
  WS probe was built stdlib-only + vendored OpenSSL for this reason.
- **Vendored OpenSSL 3.5.7** (third_party/openssl) alongside system
  LibreSSL 3.3.6; no brew/cmake on this machine.
- **Disk** [VERIFIED-MEASURED `df -h /`, `du -sh`]: 747 GiB free of 926 GiB
  (2% used). `work/` = 31 GB total: raw 17 GB (3-day retention, ~2 days on
  disk), gold 5.1 GB, warehouse/staging the rest. Trend: raw ≈ 8–9 GB/day,
  archive ≈ 122 MB/day compressed, gold ≈ 5 GB/day → at current mix ~2
  months of headroom even if retention discipline failed entirely; with
  3-day raw retention working, steady state is comfortable.
- **REST RTT re-measured** [VERIFIED-MEASURED `./build/bench_rtt 20`,
  registered network_read]: cold 112.7 ms; warm lanes n=20: min 33.8 /
  p50 35.6 / p90 36.9 / p99 36.9 ms (prior fact was 36.3 p50 — consistent).
  Clock skew vs exchange Date header: **+1138 ms** (1 s header resolution;
  preflight gate is ±2000 ms [VERIFIED-CODE apps/preflight.cpp:175-179]) —
  worth an NTP check before any signing-sensitive work; signature timestamps
  are ms-precision.

---

## Part 5 — Failure-mode inventory for the future execution layer

For each mode: what the CURRENT system does, and which existing signal would
detect it. "Nothing" answers are the Phase-2/3 build list, stated plainly.

| # | Failure mode | Current behavior | Existing detection signal |
|---|---|---|---|
| 1 | **Engine crash with resting orders** | Nothing cancels them — no resting-order registry (order_id never captured [VERIFIED-CODE tradingd.cpp]), no kill switch, no cancel-on-disconnect arming. Orders would rest until filled/expired. | None in-engine. Server-side option now confirmed: `cancel_order_on_pause` only covers exchange pauses, NOT our crash; **order groups** (rolling-limit trigger, delete-cancels-all) are the documented server-side dead-man primitive [DOCS-ONLY order_groups.md]. launchd would restart the process but restart ≠ reconcile (gap #11). |
| 2 | **WS disconnect while quoting** | Market-data side handles it: transport owns backoff+jitter reconnect; epoch increments and old-sid state drops [VERIFIED-CODE src/ws_client.cpp:117-127]; recorder marks `epoch_change`. But quoting logic doesn't exist, so nothing pulls quotes on disconnect. | `reconnects_` counter + epoch marks in capture [VERIFIED-CODE ws_client.cpp:118-119]; ping watchdog (server pings every 10 s [VERIFIED-LIVE §1.2] — 2 missed pings ⇒ dead connection). |
| 3 | **Seq gap while holding position** | Book fail-safe works: a sid gap invalidates EVERY market on the sid [VERIFIED-CODE include/kalshi/orderbook.hpp:114-116,300-302] and the recovery ladder escalates GetSnapshot→UnsubResub→Reconnect [VERIFIED-CODE include/kalshi/recovery.hpp:3-10]. No position exists today, so "trade only on valid book" is a rule to build, not a bug. | `gap` markers in raw capture [VERIFIED-CODE src/ws_client.cpp:215]; `books_valid` status; W3.x δ-distribution reports. |
| 4 | **Clock skew breaks signing** | Preflight gates at ±2 s [VERIFIED-CODE apps/preflight.cpp:175-179]; currently +1.1 s measured (§Part 4). Nothing monitors skew continuously. | preflight/exchange_check (on-demand only). Continuous skew telemetry = gap. |
| 5 | **Partial fills** | Invisible — no fill channel subscription (gap #1 [VERIFIED-CODE]), no position tracker (gap #2). The V2 create RESPONSE does return `fill_count`/`remaining_count` synchronously [DOCS-ONLY §1.1], which tradingd discards. | None. (`fill` + `user_orders` channels verified subscribable §1.2 — the plumbing target.) |
| 6 | **Cancel fails / order half-dead** | No cancel path exists at all (gap #5). Retry matrix already classifies cancel-by-id as idempotent-safe-to-retry [VERIFIED-CODE include/kalshi/backoff.hpp:9,99]. | None runtime; `MutationKind::CancelOrder` classification exists [VERIFIED-CODE src/request_spec.cpp:20,96] ready for telemetry. |
| 7 | **Rate-limit lockout (429 storm)** | Strong: reserve-before-send makes an unexpected 429 near-impossible; if one arrives it's flagged `accounting_drift`, backoff applies, Retry-After honored if Kalshi ever adds it [VERIFIED-CODE include/kalshi/backoff.hpp:65,86-91,19]. Docs confirm no penalty beyond the empty bucket [DOCS-ONLY rate_limits.md]. | `accounting_drift` telemetry counter; bucket state introspection. |

Cross-cutting: the **synchronous create response already carries
`order_id` + `fill_count` + `remaining_count` + `average_fee_paid`** — a
resting-order registry can be seeded from create responses alone before the
private WS channels land. (Design option for R, not a decision.)

---

## Part 6 — Data-side status (A7: observations only — 1 complete day)

- **Clean-day count: 1 complete archived day** (2026-07-06), certified after
  remediation; day-07 capture in progress (day 2 of the 7-day H-3 clock;
  earliest gate admission 2026-07-13). Stated plainly per A7: everything
  below is a day-one observation, not calibration truth (Q4).
- **Archive**: 223 files for date=2026-07-06 under `work/warehouse/facts/`
  (orderbooks_l1 87 files / 57.7 MB, trades 134 / 63.5 MB, orderbooks_full
  2 / 0.4 MB) [VERIFIED-MEASURED].
- **Staging (live)** [VERIFIED-MEASURED `python3 tools/warehouse_status.py`,
  reader-retry respected — first attempts hit the ingest lock window, D6
  working as designed]: `orderbooks_l1 13,451,720` rows, `trades 4,110,481`,
  `orderbooks_full 103,252`; ALL PASS, schema_ok=true. Day-06 alone had
  ~1.91 M distinct trades; L1 volume ≈ 13.4 M rows / ~2 days.
- **quality_log** (`work/quality_log.ndjson`, 4 entries, all 2026-07-07):
  1. `W2.6-exception-fix-1` — DuckDB `read_csv` sniffer narrowed
     `taker_side` yes/no to BOOLEAN on archived csv.gz; gates fail closed.
  2. `W2.6-exception-fix-2` — midnight export raced ingest backlog: archive
     held 1,863,231 day-06 trades vs 1,909,095 distinct in staging → day-06
     rebuilt (this is the "rebuilt trades" in the mission brief).
  3. `W2.6-audit-remediation` — audit found fix-2 missing its same-change
     regression test (A3 discipline enforced); `--force` hazard noted.
  4. `exception-fix-3` — daemon crash-looped when a long-lived reader held a
     read-only attach on staging (single-writer rule) — the reason this
     mission used `warehouse_status.py` with retries instead of attaching.
- **Corrupted-window remediation** (2026-07-06 08:18–08:35 UTC double-writer
  splice, ~567 frames): purged from staging; raw shards retain spliced lines
  (harmless — ingester validates at boundary, D3); root cause fixed
  (single-instance lock). Status: CLOSED, pre-registered in the discovery
  plan. Confirmed side-effect during this mission: hour-08 capture yields
  garbage inter-arrival extremes if consumed naively (§Part 2 caveat).
- **Calibration observations** (day-06 only — observations, NOT parameters):
  `work/mm/calibration_2026-07-06.csv` (25 category/subcategory rows) and
  `candidates_2026-07-06.csv` (16 candidates). Eyeballed: Sports rows (Golf,
  Cricket) show large edge_after_tox_120s (0.30–0.37 in the file's units)
  on thin trade counts (115, 1074 trades) — small-sample flags apply;
  crypto candidates (KXBTC15M) rank by volume/score. **Now cross-lit by
  Part 1:** NBA (and likely other sports series) carry
  `quadratic_with_maker_fees` [VERIFIED-LIVE §1.4] — any maker-edge readout
  on sports must subtract the 0.0175 maker fee curve before it means
  anything. Fee-aware re-ranking is exactly WP-07.

---

## OPEN QUESTIONS (decisions for R — options, not choices)

1. **Fee-schedule provenance.** `kalshi_facts.yaml` sets `verified: true`
   from the official PDF retrieved via Internet Archive (2026-02-18 snapshot
   of kalshi.com/docs/kalshi-fee-schedule.pdf, sha256 recorded) + live API
   docs corroboration (fee_rounding.md, Series.fee_type). The live PDF URL is
   bot-gated from this environment. Options: (a) accept archived-official
   provenance as-is; (b) R opens the live PDF in a browser once and confirms
   the Feb-5-2026 version is still current → upgrade evidence note;
   (c) downgrade to `verified: false` until (b), which re-blocks WP-07/WP-09
   gate-mode. Recommendation implicit in (a)+(b) being cheap; choice is R's.
2. **Demo environment strategy.** Demo is live (contradicting our env gate's
   premise). Options: (a) keep the gate as-is (fail-closed, no demo code
   path) and do first order-lifecycle verification on prod 1-cent post-only
   via operator-gated preflight `--order` (current Phase-4 plan);
   (b) provision separate demo credentials, add `demo` as a THIRD env with
   its own host allowlist (S2-preserving change, needs tests), and verify
   amend/cancel/batch/fill semantics there before any prod order;
   (c) sandbox-only demo scripts outside the engine (no engine change) for
   protocol exploration. Each trades safety-code churn against
   verification realism.
3. **Sub-penny + fractional-count support in the order builder.**
   `wire.hpp` emits integer cents / integer counts only; `deci_cent`
   markets and 13.4% sub-penny trade share exist. Fix now (small, testable,
   E4→dollars formatter exists) vs. defer to the QuoteManager build
   (Phase 2, where prices originate anyway)?
4. **Server-side dead-man via order groups.** Order groups give
   trigger/delete-cancels-all and rolling-limit auto-cancel. Adopt as the
   Phase-3 kill-switch substrate (S3 requires a standalone panic CLI anyway
   — group-delete could be its first rung), or keep kill-switch purely
   client-side batch-cancel? Affects gap #9 design.
5. **Continuous clock-skew telemetry.** +1138 ms measured today (within the
   ±2 s preflight gate, 1 s measurement resolution). Add skew to the daily
   WP-08 health output, or leave as on-demand preflight check?
6. **Batch-read billing probe.** Write billing per-item is now
   docs-confirmed; batch READ billing is still [ASSUMED] per-item. A
   bounded prod probe (`probe_batch_cost` exists in build/) would settle it
   for ~40 read tokens. Run it, or leave assumed-conservative?

## CORRECTIONS (what prior docs/code comments got wrong)

1. **"Kalshi's demo exchange is unavailable" is FALSE as of 2026-07-07.**
   `src/env.cpp:165` and `tools/exchange_check.sh` state it; both documented
   demo hosts return HTTP 200 with `exchange_active: true, trading_active:
   true` [VERIFIED-LIVE §1.5]. The fail-closed rejection itself stays
   correct policy (A2); only its stated *reason* is stale. Fix the message/
   comment when next touching env.cpp (not in this read-only mission).
2. **EXECUTION_PLAN/A3 pre-seeded "tier advanced read/write 300/s" was
   incomplete**: bucket capacity is 600 (2 s burst), not 300, on both
   buckets [VERIFIED-LIVE §1.3]. Matters for batch sizing (a 60-order
   create batch fits a full bucket; 30 was the implied ceiling).
3. **ARCHITECTURE_REVIEW L4/L3 sections are stale on two facts**: the gold
   layer now exists (W2.x landed post-review), and the first archived day
   exists (223 files, day-06, rebuilt trades). The review's "µs-scale
   decode" understates the engine: measured 281.9 ns/msg decode+apply
   [VERIFIED-MEASURED §Part 2].
4. **`request_spec.hpp` F8 comment "batch billed per item is a local
   ASSUMPTION (undocumented)"** is out of date for writes: Kalshi now
   documents per-item batch billing explicitly [DOCS-ONLY rate_limits.md
   §"Batch endpoints don't save tokens"]. The comment should be narrowed to
   batch reads when the file is next touched.
5. **Prior fee placeholder in GUARDRAILS Q3 ("taker ≈ 0.07·P·(1−P) rounded
   up")** — the shape is right; the rounding is now centicent-ceil +
   accumulator (not cent-ceil), and the multiplier/fee_type override layer
   (per-series, per-event) must be part of any fee function signature
   [§1.4].

---

## Appendix — probe artifacts (sandbox/discovery/, kept until report accepted per A6)

| File | What |
|---|---|
| `rest_headers_probe.sh` | signed GET header-dump probe (openssl-CLI RSA-PSS signing) |
| `rest_headers_output.txt` | its output (response headers only, no key material) |
| `ws_private_probe.py` | stdlib-only WS client: auth handshake + private-channel subscribe |
| `ws_private_output.txt` | its output (acks, ping cadence) |
| `demo_probe_output.txt` | demo host status responses |
| `prod_exchange_status.json` | prod shard status |
| `fee_schedule_text_clean.txt` | extracted text of the official fee PDF (archived copy) |
| `fee_schedule_wayback_20260218.pdf` | the archived official PDF itself (sha256 b1a37aa7…) |
| `api_environments.md`, `demo_env.md`, `rate_limits.md`, `fee_rounding.md`, `fixed_point_migration.md`, `order_groups.md`, `maintenance_and_pauses.md` | live doc-page fetches (2026-07-07) backing the [DOCS-ONLY] cites |

**Evidence-tag counts (distinct tagged claims):** VERIFIED-LIVE 11 ·
VERIFIED-CODE 24 · VERIFIED-MEASURED 9 · DOCS-ONLY 17 · ASSUMED 1
(batch-read billing).
