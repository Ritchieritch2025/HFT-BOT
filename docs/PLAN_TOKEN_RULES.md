# Kalshi Token Rule System — Claude Code Implementation Plan

> Suggested repo location: `docs/PLAN_TOKEN_RULES.md`. Companion to
> `PLAN_WS_V2.md` (WS market data) and `DATA_QUALITY_GATES_PROMPT.md`
> (Phase 3.5). This plan covers the REST request chain: environment → auth
> signing → cost lookup → token reservation → send → backoff → telemetry.
> All protocol facts verified against docs.kalshi.com on 2026-07-04 and
> RE-verified against OpenAPI v3.23.0 later the same day — the account
> limits schema changed from flat to nested between spec versions, which
> supersedes audit findings E1/E2 in token_rule_plan_audit.md. Facts below
> reflect v3.23.0. Implement THIS version, not the original draft and not
> the audit's flat-schema correction.

## Repo integration points (extend, don't duplicate)

- `include/kalshi/client.hpp` / `src/client.cpp` — signing (RSA-PSS, ms
  timestamps, path-without-query) EXISTS and is correct; do not regress it.
- `include/kalshi/rest_api.hpp` / `src/rest_api.cpp` — typed endpoints,
  `ApiError`, `RetryPolicy`, and a double-based `TokenBucket` (hardcoded
  8/s). This plan REPLACES that bucket and threads everything through a new
  executor.
- `include/kalshi/env.hpp` / `src/env.cpp` — fail-closed Runtime +
  host/env allowlist cross-validation EXISTS; extend tests only.
- `tests/mock_rest.py`, `test_rest_api`, `test_env_safety`, `test_signing` —
  extend, keep the offline-first house style.
- Order-capable binaries (`tradingd`, `preflight`, `bench_order`,
  `fill_test`) — integration in Phase T7/T8 only; transmission gates
  (`require_orders_allowed`) are untouched except being made STRICTER.

## Hard rules

1. No strategy logic. Tier upgrade never runs
   automatically and never from the trading hot path.
2. Never log KALSHI-ACCESS-KEY, signatures, private keys, or raw auth
   headers. A grep test enforces this.
3. Reserve tokens locally BEFORE sending. Never send-and-wait-for-429: 429
   carries no Retry-After and no X-RateLimit headers (official).
4. Integer/fixed-point token accounting (tokens × 10^3 in int64 against a
   nanosecond clock); no doubles in the accounting path.
5. Every authenticated REST call goes through the single RequestExecutor.
   Grep gate: no direct `client.request(` call sites outside the executor
   and the WS handshake.
6. Items marked ASSUMPTION are local policy, commented as such in code.
   Do not cite docs for them.

## Verified protocol facts (implementation must match exactly)

| # | Fact | Source |
|---|---|---|
| F1 | Hosts: prod `external-api.kalshi.com`, demo `external-api.demo.kalshi.co`; compatibility `api.elections.kalshi.com` / `demo-api.kalshi.co`; same signature scheme on all | api_environments |
| F2 | Sign `timestamp_ms + METHOD + path_without_query` (no scheme/host/query); RSA-PSS SHA-256, MGF1-SHA256, salt = digest len, base64; KALSHI-ACCESS-TIMESTAMP is unix **milliseconds** | api_environments, quickstarts |
| F3 | `GET /trade-api/v2/account/limits` (wire path incl. prefix) → **nested** schema, all four fields REQUIRED: `{usage_tier: string, read: BucketLimit, write: BucketLimit, grants: []}` where `BucketLimit = {refill_rate: int (tokens/s), bucket_capacity: int (max tokens)}` and each grant = `{exchange_instance: "event_contract"\|"margined", level: string, source: "volume"\|"manual", expires_ts?: int64 (absent = permanent)}`. Public tiers: basic, advanced, expert, premier, paragon, prime, prestige | get-account-api-limits OpenAPI v3.23.0 |
| F4 | Units RESOLVED by v3.23.0: `refill_rate` is explicitly **tokens per second**. Keep the tier-table sanity check as a fallback SAFEGUARD only (warn on mismatch with published budgets; never silently rescale) | get-account-api-limits BucketLimit schema; rate_limits |
| F5 | `GET /trade-api/v2/account/endpoint_costs` → `{default_cost (currently 10), endpoint_costs: [{method, path, cost}]}`; only non-default entries listed; absent ⇒ default_cost | list-non-default-endpoint-costs OpenAPI |
| F6 | Read bucket = GET + anything not explicitly routed; Write bucket = order placement, amends, cancels, order groups, RFQ quote flow. (NOT in docs: block-trade accepts — route to Write as ASSUMPTION if desired) | rate_limits |
| F7 | Bucket capacity IS server-provided: initialize `TokenBucketI64` directly from `read.bucket_capacity` / `write.bucket_capacity`. No hardcoded 1 s / 2 s derivation. (The spec's note that capacity == refill_rate means 1 s of budget and larger values are burst headroom is EXPLANATION, not something to re-derive locally) | get-account-api-limits BucketLimit schema |
| F8 | Batch order endpoints bill per item: 25 creates × 10 = 250; 25 cancels × 2 = 50. Whole batch must fit at once. Batch READ billing is UNDOCUMENTED ⇒ per-item ASSUMPTION, verify in demo | rate_limits |
| F9 | 429 body `{"error":"too many requests"}`; no Retry-After / X-RateLimit; no cooldown; continuous refill | rate_limits |
| F10 | `POST /trade-api/v2/account/api_usage_level/upgrade`: costs **30 tokens from the Write bucket**; 201 = permanent **Advanced** grant (Predictions instance only); 403 = "no API-created order in user's last 100 Predictions orders"; still requires signed auth. Resulting grant is visible in F3's `grants` array (`source: "volume"` or `"manual"`) | upgrade-account-api-usage-level OpenAPI |
| F11 | Costs change server-side (read costs announced to drop below default) ⇒ refresh table periodically | changelog Apr 2026 |
| F12 | The limits schema itself changed from flat (v3.14.0) to nested (v3.23.0) between page-cache states — per-page embeds drift and even churn within days ⇒ vendor the single live `openapi.yaml`, run the spec-drift check, treat page embeds as illustrations | observed 2026-07-04 |

Note on paths: all `paths:` keys in the OpenAPI are relative to the
`/trade-api/v2` server base. Wire paths — and therefore SIGNATURE paths —
always include the prefix (e.g. sign `/trade-api/v2/account/limits`). The
plan writes full wire paths everywhere to prevent 404s and signature
mismatches.

---

## Phase T0 — Environment/host hardening (verify + test, mostly exists)

Tasks: confirm `env.cpp` enforces: prod↔demo host cross-use throws;
non-TLS only for localhost mock; unsafe override refused in live; zero
`find("demo")` string matching anywhere (grep gate — commit history says
this was already purged; keep the gate).
Tests: the six §1 cases from the draft (defaults, allowlist, cross-env
throw, http-non-localhost throw, live override refusal).
Gate: `test_env_safety` extended and green.

## Phase T1 — Limits + cost metadata layer

Tasks:
1. `AccountLimits` type matching F3 EXACTLY (nested):
   `BucketLimit {std::int64_t refill_rate; std::int64_t bucket_capacity;}`;
   `ApiUsageLevelGrant {exchange_instance enum(event_contract, margined,
   +Unknown open slot); std::string level; std::string source;
   std::optional<std::int64_t> expires_ts;}` (absent expires_ts =
   permanent); `AccountLimits {std::string usage_tier; BucketLimit read;
   BucketLimit write; std::vector<ApiUsageLevelGrant> grants; bool
   from_server;}`. All four top-level fields REQUIRED by schema — missing
   any ⇒ parse failure. `usage_tier` is an open string (7 public tiers
   today; tolerate unknown values). Additional unknown JSON fields
   tolerated, never required.
2. **Tier-table sanity check (F4, fallback safeguard only)**: after
   parsing, compare `refill_rate` values against the published tier table
   {basic: 200/100, advanced: 300/300, premier: 1000/1000, paragon:
   2000/2000, prime: 4000/4000; expert/prestige: not yet published}. On
   mismatch for a known tier: WARN loudly with both values, keep the
   server values (server is authoritative), and emit
   `limits_table_mismatch` telemetry. Never silently rescale. Sanity-check
   invariants that DO fail closed in live mode: any refill_rate <= 0,
   bucket_capacity < refill_rate ÷ 2, or capacity/refill ratio > 10.
3. `EndpointCostTable`: default_cost + overrides keyed by (method,
   path-template). **Matchers are built from the server's own path strings**
   (template segments like `{ticker}` become wildcards); the hand-written
   normalization table from the draft (§4) becomes the tested FALLBACK only
   (A1). Log the raw table at startup (cold path).
4. Refresh: fetch both at startup; re-fetch endpoint_costs hourly (control
   thread) and on any "impossible 429" (local accounting said tokens were
   available) — that event is also telemetry `token_accounting_drift` (feeds
   the global trip in DATA_QUALITY_GATES).
5. Live mode fail-closed if either fetch fails; data_collect/shadow fall
   back conservatively (basic tier, all-default costs) with warning.

Tests: parse the real nested JSON (usage_tier + read/write BucketLimits +
grants incl. one permanent grant without expires_ts and one expiring
grant); missing any required top-level field ⇒ parse failure; unknown
usage_tier value tolerated; unknown extra fields tolerated; tier-table
mismatch ⇒ warn + server values kept + telemetry emitted; invariant
violations (refill_rate 0, absurd capacity ratio) ⇒ fail closed in live,
fallback + warning in shadow; missing override ⇒ default; malformed live
⇒ throw; malformed shadow ⇒ fallback + warning; server-derived matcher
beats hardcoded fallback.

## Phase T2 — RequestSpec + classification + normalization

Tasks: `RequestSpec` {method, raw_path, normalized_path, bucket_kind,
cost_tokens, mutation_kind, idempotency_required}. Classification per F6.
`normalize_endpoint_path`: strip query → exact match → server-template
match → hardcoded fallback patterns → default_cost.
Tests: §4 and §5 cases from the draft verbatim (GET /markets ⇒ Read, POST
order ⇒ Write, cancel/amend/order-group ⇒ Write, query stripped, order-id
and ticker paths normalized, unknown ⇒ default).

## Phase T3 — Integer token buckets

Tasks: replace the double-based `TokenBucket` with `TokenBucketI64`
(tokens ×10^3, int64, monotonic ns clock injectable for tests). Two
instances (Read, Write) initialized DIRECTLY from the server values (F7):
rate = `read/write.refill_rate`, capacity = `read/write.bucket_capacity`.
No local burst derivation of any kind. API:
`try_reserve(cost, now_ns)` (hot-safe, lock-free or single-mutex —
executor runs on control/lane threads, NOT the market-data hot path; a
plain mutex is acceptable, document the choice) and
`reserve_or_wait(cost, now_ns, deadline)`. Telemetry per reservation:
bucket, before, after, wait_ns, cost.
Tests: refill, cap, independence, exact-cost reserve, insufficient ⇒ wait,
**zero drift after 10^7 cycles (integer property)**, deterministic fake
clock, concurrency hammer + TSan.

## Phase T4 — Backoff/retry policy

Tasks: capped exponential backoff, multiplier 2, jitter from a seeded PRNG
(deterministic in tests). Order of defense: local bucket wait FIRST;
backoff only on unexpected 429/transient. Retry matrix: GET retryable on
429/502/503/504/transport; POST order NEVER blindly retried after
ambiguous timeout — requires client_order_id + reconcile (existing
idempotency machinery) before any resend; 429-before-accepted may retry
after wait. If a Retry-After ever appears (F9 says it won't today), record
as telemetry only.
Tests: §8 cases verbatim, incl. POST-timeout-no-duplicate (assert via mock
that at most one order reaches the server) and deterministic jitter.

## Phase T5 — RequestExecutor (single outbound path)

Tasks: `RequestExecutor::send(method, path, body, opts)` implementing the
11-step flow (spec → sig-path → normalize → bucket → cost → reserve → sign
→ send → parse → retry policy → telemetry). Signing stays pure-CPU before
lane acquisition (existing client.cpp split API). Secret redaction helper
for all logging. Migrate every existing `RestApi` endpoint call through it.
Grep gates: no bypassing call sites; no auth-header values in any log
format string.
Telemetry per request (§12 list, plus `endpoint_cost_source` and
`usage_tier`). Cold-path NDJSON, dashboard picks it up with a request-chain
section.
Tests: reserves-before-send ordering (mock asserts no HTTP before
reservation), signs-after-cost-resolution, 429 handling, malformed
response, telemetry fields present, secret-grep clean.

## Phase T6 — Batch cost rules

Tasks: batch create = n × cost(POST single); batch cancel = n × cost(single
cancel); batch reads = n × single-read cost **ASSUMPTION (F8)** unless
endpoint_costs says otherwise; local rejection if bucket lacks TOTAL cost
(partial batches never sent). Add the demo-environment empirical probe
script (`tools/probe_batch_cost.sh`: sustained batch-orderbook calls at
known tier, log throughput to first 429) and record findings in the
rulebook.
Tests: 250/50 token cases, local rejection, no partial send.

## Phase T7 — Tier upgrade command

Tasks: `preflight --upgrade-api-tier`, explicit flag only. Flow: resolve
runtime → GET `/trade-api/v2/account/limits` (before) → print
tier + read/write refill_rate/bucket_capacity + grants table
(exchange_instance, level, source, expires_ts or "permanent") → reserve
**30 Write tokens through the executor (F10)** → POST
`/trade-api/v2/account/api_usage_level/upgrade` → 201: report "permanent
Advanced grant created/refreshed"; 403: print the documented criteria
("no API-created order in your last 100 Predictions orders"); other 4xx:
server message → GET limits (after) → print tier/limits/grants delta,
highlighting the new grant. Never callable from tradingd.
Tests: §10 cases + 30-token Write reservation asserted + 403 criteria
message.

## Phase T8 — Execution-path integration

Tasks: live-mode preconditions extended (fail closed unless: runtime valid,
AccountLimits loaded+normalized, EndpointCostTable loaded, Write bucket
initialized, idempotency active). Order path draws from Write bucket via
the executor. Shadow initializes the limiter for realism but never
transmits (existing behavior). Depleted Write bucket ⇒ intent dropped with
telemetry (consistent with existing TRADINGD_MAX_ORDERS_PER_SEC semantics —
fold that env knob into this system: if both set, the stricter applies;
document).
Tests: §11 cases verbatim + knob-interaction test.

## Phase T9 — Mock server, suite, rulebook

Tasks: extend `mock_rest.py` with the three account endpoints (full
`/trade-api/v2/...` paths, nested limits schema incl. grants variants:
empty, permanent, expiring) + scenario matrix from §13 (tiers incl.
expert/prestige, malformed, 429-no-header, repeated-429-then-ok, override
cost, batch rejection, upgrade success/failure — and add: tier-table
mismatch variant for the F4 safeguard test). Write
`docs/KALSHI_RULEBOOK.md` with RULE-ID/Statement/Source/Implemented-in/
Tested-by/Severity rows: the draft's ten rules corrected to v3.23.0 facts
(nested limits schema with required grants; capacities server-provided,
no local burst derivation; upgrade = 30 tokens Write; batch-read billing
= assumption; tier-table check is a warn-only safeguard) plus F11/F12
entries.
Full suite: test_signing, test_env_safety, test_account_limits,
test_endpoint_costs, test_token_bucket, test_backoff,
test_request_executor, test_upgrade_api_tier, test_order_rate_limit,
test_secret_redaction — in Make AND CMake.

## Sequencing & coordination

- T0–T6 are independent of the WS plan and can proceed NOW in parallel with
  WS Phases 0–2 review — they touch `rest_api.*`, `env.*`, `preflight`, not
  the WS transport. Shared file `client.cpp`: coordinate merges.
- T8 gates live trading and should land before any live session that
  follows the WS work.
- The `token_accounting_drift` event feeds DATA_QUALITY_GATES' global trip —
  land the telemetry name/shape now so 3.5 can consume it.
- Standing precondition: WS Phases 0–2 still must be pushed to GitHub for
  review; this plan does not change that.

## Acceptance criteria

Original draft's list, amended: clean build; all ten test binaries green +
sanitizers; no `/account/api_limits` or `/account/non-default-endpoint-costs`
strings anywhere; all account wire paths carry the `/trade-api/v2/` prefix
(and signature paths include it); prod default host
`external-api.kalshi.com`; limits parser handles the NESTED v3.23.0 schema
(read/write BucketLimits + required grants, permanent + expiring variants)
with the tier-table warn-only safeguard and fail-closed invariant checks
tested; buckets initialized from server `bucket_capacity` values with no
local burst derivation; upgrade command reserves 30 Write tokens via the
executor, explains 403, and prints the grants delta; batch tests pass incl.
no-partial-send; live order path fails closed without loaded limiter
metadata; endpoint_costs refresh path exists; secret-grep clean; rulebook
complete with v3.23.0-corrected entries.
