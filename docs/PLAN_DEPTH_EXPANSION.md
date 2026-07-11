# PLAN_DEPTH_EXPANSION — orderbook_delta expansion: design + bounded probe

Status: **DESIGN DOC + STANDALONE PROBE TOOL ONLY.** No rollout, no production
changes. The probe (`tools/depth_probe.py`) is **operator-gated** — it refuses
to run without `--operator-approved` and has NOT been run as of this writing.
Every number below marked [MEASURED] comes from read-only queries against the
day-06 warehouse; probe-derived numbers are marked **PROBE-PENDING**.

Produced by W6 of `docs/PLAN_GOLD_DATA_CONTRACT.md`. Rollout, if any, needs its
own operator-approved plan (R decides among the options in §4).

Context: the production pipeline today captures **ticker+trade firehose**
(all markets, one WS connection, `tools/pipeline_supervisor.sh` layer 1) plus
full depth for a historical 4-market watchlist sample. This plan sizes what it
costs to stream `orderbook_delta` for the top of W4's depth-target list
(`work/mm/depth_target_2026-07-06.csv`, 9,469 ranked markets: 2,382 High /
741 Mid / 6,346 Low).

---

## 1. Spec-derived limits on orderbook_delta subscriptions

Sources: `docs/vendor/kalshi/latest/asyncapi.yaml` (spec snapshot, sha in
`docs/vendor/kalshi/latest/manifest.json`), `config/kalshi_facts.yaml`
(WP-05 discovery, evidence-tagged).

| # | Limit / fact | Value | Evidence |
|---|---|---|---|
| L1 | `orderbook_delta` requires authentication | yes | [DOCS-ONLY] asyncapi `orderbook_delta` "Authentication required" |
| L2 | `orderbook_delta` requires an **explicit market list** (`market_ticker` or `market_tickers[]`; `market_id(s)` NOT supported) — there is no all-markets firehose mode for depth | yes | [DOCS-ONLY] asyncapi `orderbook_delta` requirements |
| L3 | Per-subscription **market-count limit exists** but its numeric value is **not published** | unknown N | [DOCS-ONLY] asyncapi error code 26 "Subscription market limit exceeded — adding markets would exceed the per-subscription market limit" |
| L4 | Per-subscription **command rate limit exists**, value not published | unknown | [DOCS-ONLY] asyncapi error code 27 "Too many requests — the subscription exceeded its command rate limit" |
| L5 | **Subscription buffer overflow is a terminal error** — a consumer that reads too slowly gets its subscription killed server-side and must resubscribe | terminal err 25 | [DOCS-ONLY] asyncapi "Terminal Errors" table (codes 10, 17, 25) |
| L6 | Per-account **concurrent WS connection limit: not documented** in the snapshot | unknown | [DOCS-ONLY] absence in asyncapi + llms.txt (searched 2026-07-07); treat as a risk, §6.1 |
| L7 | Sequencing is **per-sid**: `orderbook_snapshot` arrives first, then incremental deltas; `update_subscription` supports `add_markets` / `delete_markets` / `get_snapshot` (snapshot re-fetch without resubscribing) | — | [DOCS-ONLY] asyncapi `orderbook_delta` + updateSubscription messages |
| L8 | Server-side sharding exists: `shard_key`/`shard_factor` subscribe params, `shard_factor <= 100` | — | [DOCS-ONLY] asyncapi error codes 19–22 |
| L9 | WS messages do **not** bill the REST token buckets; the documented WS-side protections are exactly L3/L4/L5. REST buckets (advanced tier): read 300 tok/s refill, 600 cap; write identical | 300/s, cap 600 | [VERIFIED-LIVE] `config/kalshi_facts.yaml` rate_limits (GET /account/limits, 2026-07-07); [DOCS-ONLY] rate_limits.md covers REST only |
| L10 | REST cross-check cost: one batched `GET /markets/orderbooks` call = default endpoint cost 10 read tokens — a 50-market snapshot cross-check is one call, ~3.3% of one second's refill | 10 tok | [VERIFIED-LIVE] kalshi_facts `default_endpoint_cost_tokens` |
| L11 | Server pings every 10 s (`heartbeat`); 2 missed pongs ⇒ dead connection in our client | 10 s | [VERIFIED-LIVE] kalshi_facts ws.server_ping + DISCOVERY_REPORT §1.2 |

Consequences for design: the two limits that actually bound expansion (L3
market cap per subscription, L6 connection cap per account) are **not
numerically documented**. The probe (§5) subscribes 50 markets in a single
subscribe command on a second connection — it therefore empirically tests
"50 ≤ L3" and "2 ≤ L6" as a side effect, at zero incremental cost.

## 2. Sizing model — measured L1 rates × calibrated depth multiplier

### 2.1 L1 update rates by tier [MEASURED, day-06 warehouse, read-only]

Method: per-market row counts from
`work/warehouse/facts/orderbooks_l1/*/*/*/*.parquet` WHERE date='2026-07-06'
(8,210,082 rows total that day), joined to `depth_target_2026-07-06.csv` on
`market_ticker`. L1 rows are change-only ticker-channel updates, so they
*under*-count book-level activity — that is what the multiplier (§2.2) is for.
"Active rate" = rows / (max ts − min ts) per market, floored at 60 s.

| Tier | markets | L1 rows (day) | median rows/mkt | p90 | max | median active r/s | p90 active r/s | max r/s | tier Σ rows/s (day-avg) |
|---|---|---|---|---|---|---|---|---|---|
| High | 2,382 | 2,826,032 | 750 | 2,595 | 15,921 | 0.018 | 0.063 | 1.03 | 32.7 |
| Mid  | 741   | 439,017   | 288 | 1,452 | 6,417  | 0.007 | 0.036 | 0.60 | 5.1 |
| Low  | 6,346 | 2,126,207 | 94  | 841   | 16,128 | 0.003 | 0.029 | 0.59 | 24.6 |

Top-N of the W4 ranked list (all High tier down through rank 500):

| N | L1 rows (day) | day-avg rows/s | Σ per-market active rates (≈ concurrent peak proxy) |
|---|---|---|---|
| 50  | 148,586 | 1.72  | 5.65/s |
| 200 | 507,987 | 5.88  | 15.84/s |
| 500 | 988,582 | 11.44 | 29.58/s |

### 2.2 Full-depth multiplier [MEASURED calibration, n=3 — honest error bars]

The only full-depth ground truth we own is the legacy 4-market watchlist
capture (2026-07-06 UTC, ~30 min, in-game MLB), in
`work/warehouse/facts/orderbooks_full/`. Same-window L1 counts joined from
`orderbooks_l1`:

| market | full deltas | span s | full msg/s | L1 msgs same window | **multiplier full/L1** |
|---|---|---|---|---|---|
| KXMLBTOTAL-…BOSLAA-14   | 68,853 | 1,799.8 | 38.3 | 709 | **97.1×** |
| KXMLBSPREAD-…BOSLAA-BOS5| 22,493 | 1,799.5 | 12.5 | 628 | **35.8×** |
| KXMLBTOTAL-…BOSLAA-16   | 11,901 | 1,799.7 | 6.6  | 393 | **30.3×** |
| KXWCGOAL-… (dead market)| 0      | —       | ~0   | 1   | — (dead ⇒ ~0 traffic) |

**Error bars, stated honestly:** n=3 live markets, all one product type
(in-game MLB totals/spreads) during in-game peak — plausibly near the top of
the multiplier distribution (dense books, constant requoting). We do NOT know
the multiplier for crypto 15-min markets, tennis, Fed markets, etc. Sizing
below therefore brackets with **expected = 30×** (our observed minimum) and
**conservative = 100×** (our observed maximum, rounded up). The probe's whole
purpose is to replace this 3-point calibration with a 50-market measurement.
**PROBE-PENDING.**

### 2.3 Bytes per message [MEASURED from `work/live_capture.ndjson`, 103,247 deltas]

| message | wire B/msg | NDJSON capture B/msg |
|---|---|---|
| orderbook_delta    | 270 | 531 |
| orderbook_snapshot | 763 (n=5, thin books; deep books = a few KB) | 1,130 |
| ticker             | 476 | 729 |

Engine headroom for context: decode+apply is 281.9 ns/msg [VERIFIED-MEASURED,
kalshi_facts.latency] ⇒ CPU is not the constraint at any size below; the
constraints are exchange-side limits (L3/L5/L6), disk, and ingest.

## 3. Candidate-set arithmetic — what fits at 50 / 200 / 500 markets

msg/s = (L1 rate) × (multiplier); bytes use 531 B/msg capture encoding.
"Peak" uses the Σ-active-rate proxy; "day-avg" uses rows/day ÷ 86,400.

| N | expected day-avg msg/s (30×) | expected capture/day | conservative peak msg/s (100×) | conservative capture/day (100×) |
|---|---|---|---|---|
| 50  | 1.72×30 ≈ **52/s**   | 148,586×30×531 B ≈ **2.4 GB** | 5.65×100 ≈ **565/s** | ≈ **7.9 GB** |
| 200 | 5.88×30 ≈ **176/s**  | 507,987×30×531 B ≈ **8.1 GB** | 15.84×100 ≈ **1,584/s** | ≈ **27.0 GB** |
| 500 | 11.44×30 ≈ **343/s** | 988,582×30×531 B ≈ **15.7 GB** | 29.58×100 ≈ **2,958/s** | ≈ **52.5 GB** |

Plus a subscribe-time snapshot burst: N × ~1–4 KB ≈ ≤2 MB even at N=500 —
negligible.

Reading of the table under conservative assumptions:

- **50 markets** fits trivially everywhere: peak 565 msg/s is ~1.2× today's
  whole firehose mean (472 events/s [VERIFIED-MEASURED kalshi_facts]); disk
  ≤8 GB/day against 3-day raw retention.
- **200 markets** is comfortably within engine + recorder capacity; the open
  questions are the undocumented per-subscription market cap (L3, may force
  subscription chunking or multiple connections) and ingest keeping up with
  ~1.6 k rows/s bursts (ingest cycles every 60 s; typed-row inserts are
  batch, expected fine but unproven — probe data lets us replay-test this).
- **500 markets** conservative worst case ≈ 3 k msg/s, 52 GB/day raw. Disk
  becomes the first real cost (156 GB across the 3-day retention window at
  worst case); L5 (server-side buffer overflow on slow consume) becomes worth
  respecting. Doable on paper, but this is exactly where measured — not
  30/100× bracketed — numbers are mandatory before committing.

All three columns collapse to real numbers after the probe: measured msg/s
and bytes/market **by tier** replace the multiplier bracket. **PROBE-PENDING.**

## 4. Rollout OPTIONS — enumerated, NOT chosen (R decides)

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A. Second dedicated depth instance** — a separate supervised ws_shadow process, own connection, `KALSHI_WS_CHANNELS=orderbook_delta`, explicit top-N tickers, own raw-log family (e.g. `work/raw/date=*/depth_<HH>.ndjson`) | production firehose completely untouched (P4); independent restart/blast radius; ingest already types orderbook_delta rows into `orderbooks_full`; capture-side naming change ships with an ingest-side test (D4) | second concurrent connection vs undocumented L6; a second supervised daemon (E6: launchd/watchdog/lock work); daily ticker rotation needs restart or update_subscription plumbing |
| **B. Extend the production firehose connection** — add an `orderbook_delta` subscription (explicit tickers) to the existing connection alongside ticker/trade | one connection (no L6 exposure); one supervisor; one raw-log stream | touches the revenue-critical 24/7 capture path (P4 — highest-risk option); depth burst competes head-of-line with ticker/trade on one TCP stream and one recorder ring (L5 risk now endangers the *firehose*); hourly logs mix channels (ingest fine, but rotation-size assumptions change); any bug rolls back capture itself |
| **C. Multiple sharded depth connections** — k connections × ≤m markets each (and/or server-side shard_key/shard_factor, L8), fanned out from the same target list | scales past any per-subscription cap (L3); per-connection blast radius; shard math documented in spec (L8) | most moving parts (k daemons or one multi-conn process); k× exposure to undocumented L6; sequencing is per-sid per-connection — cross-connection book assembly needs care; over-engineered below ~500 markets on current numbers |

Cross-cutting for every option: the daily depth-target list rotates (W4
regenerates it), so the subscription set must be refreshed — either process
restart at UTC-day boundary (option A/C, simple, one snapshot burst) or
`update_subscription add/delete_markets` (L7, no reconnect). That choice also
belongs to the rollout plan, not this document.

## 5. The probe protocol (operator-gated; the ONLY runnable thing in this plan)

Tool: `tools/depth_probe.py` (registry: `depth_probe`, kind=probe,
safety=network_read, autorun=false, needs creds+prod_env).

- **Gate:** refuses to run without `--operator-approved` (loud message
  pointing here). Refuses `KALSHI_MODE=live` outright, even with the flag
  (and ws_shadow itself refuses live — two layers). `--dry-run` prints the
  exact env/tickers without opening any socket.
- **What it runs:** a SECOND ws_shadow instance (read-only harness, transmits
  nothing, S5-compatible) for **900 s** with:
  - `KALSHI_WS_CHANNELS=orderbook_delta`
  - `KALSHI_WS_TICKERS=` top **50** (`--markets`) from the newest
    `work/mm/depth_target_*.csv` (warns if the list is stale — dated lists
    contain settled markets that subscribe cleanly and stream nothing)
  - `KALSHI_MODE=data_collect` (forced), `KALSHI_WS_FIREHOSE` scrubbed
  - **Separate capture path:** `work/probe/depth_probe_<UTCts>.ndjson` and
    `KALSHI_SHADOW_METRICS=work/probe/depth_probe_<UTCts>.metrics.ndjson`.
    NEVER the production hourly logs and never `work/metrics.ndjson` — the
    double-writer lesson (D6 / rotation-shard incident). The tool refuses any
    capture path under `work/raw/`. `work/probe/` is not tailed by the
    ingester, so probe data cannot contaminate the warehouse.
- **What it measures** (printed after capture, from the capture file):
  msg/s and bytes/market **by liquidity tier** (tier joined from the same
  CSV), snapshot vs delta counts, dead-subscription list (markets that sent
  nothing — surfaced loudly, D2), overall duration and totals. The analysis
  is a pure function (`analyze_capture`) tested on a fixture capture in
  `tests/test_depth_probe.py`.
- **Side-effect observations, free:** whether 50 markets in one subscribe
  trips error 26 (L3), and whether a second concurrent connection is refused
  (L6). Either failure is a loud, bounded, 15-minute-max event.
- **Budget:** expected ~52 msg/s avg / ≤565 msg/s conservative peak (§3, N=50)
  ⇒ capture between ~25 MB (expected) and ~270 MB (conservative) for 15 min.
  Zero REST tokens (no cross-check; `KALSHI_SHADOW_XCHECK` stays off). Zero
  write-bucket tokens ever (ws_shadow asserts transmitted()==0).
- **Operator runbook:** confirm pipeline green (`python3 tools/freshness.py`),
  run `python3 tools/depth_probe.py --operator-approved`, watch freshness
  during the window, eyeball the printed table, then paste the table into
  §2/§3 of this doc replacing the PROBE-PENDING marks.

## 6. Risks

1. **Rate-limit / connection interaction with the production firehose.** The
   probe uses a second WS connection under the same account; per-account
   connection limits are undocumented (L6). Worst plausible case: the
   subscribe is refused (probe fails loudly, firehose unaffected) or — worst
   case — the exchange drops the older connection. Mitigation: bounded 15 min,
   operator watching `freshness.py` live; the supervisor auto-reconnects the
   firehose with backoff within seconds and the recorder marks the epoch, so
   the worst case is a small, visible, marked capture gap. WS messages do not
   bill REST tokens (L9), and the probe spends zero REST tokens, so there is
   no token-bucket interaction with anything.
2. **Disk growth.** Probe: ≤~0.3 GB one-off under `work/probe/`. Rollout
   projections in §3 (2.4–52.5 GB/day depending on N and multiplier) against
   `raw_retention_days: 3`; at N≥200 the rollout plan must budget disk (and
   likely gzip-on-rotate, already a BACKLOG item) before enabling.
3. **Ingest load.** Probe data is deliberately outside `work/raw` ⇒ zero
   ingest impact. For rollout, `orderbooks_full` typed-row inserts at 176 avg
   /1,584 peak rows/s (N=200 conservative) are untested against the 60 s
   ingest cycle — the probe capture doubles as a replay fixture to test
   exactly this before any rollout.
4. **Slow-consumer kill (L5).** At probe scale (≤ ~600 msg/s vs 281.9 ns/msg
   decode) there is ~3 orders of magnitude of headroom; the recorder's drop
   counters + gap/epoch markers make any loss visible rather than silent (D2).
5. **Stale target list.** The depth-target CSV is a daily artifact; probing
   yesterday's list yields dead subscriptions and understated rates. The tool
   warns when the CSV date ≠ today and prints the dead-market count.

## 7. Exit evidence / DoD for W6

- [x] Spec-derived limits documented with evidence tags (§1).
- [x] Sizing model from measured day-06 L1 rates + calibrated multiplier with
      honest error bars (§2).
- [x] Candidate-set arithmetic for 50/200/500 (§3).
- [x] Rollout options enumerated, none chosen (§4).
- [x] Probe tool shipped, refusal path demonstrated, analysis function
      pytest-covered on a fixture capture; registry entries appended.
- [x] **PROBE RUN 2026-07-11 22:44–22:59Z(operator-approved,W06 Stage 0)**
      — measured table below replaces the 30–100× bracket.

## 8. PROBE RESULTS (2026-07-11, 48 markets, 900 s, in-play evening window)

Command: `KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 python3 tools/depth_probe.py
--operator-approved --csv work/mm/l2_probe_targets_2026-07-11.csv`(操作员
逐字批准);list = same-day four-sports+controls,OI-ranked,22:39Z 生成。
ws_shadow: 591,445 events,0 reconnects,0 errors,0 overflow,recorder
COMPLETE(0 dropped),transmitted=0。生产 firehose 全程秒级新鲜(4 次
哨兵检查),SEAL_ALARM=NONE。**L3:48 市场单次 subscribe 成功(无 error
26);L6:第二连接与生产共存无碍;无 error 25/27。**

Capture analysis (tool output VERBATIM): duration 728.4 s | book msgs
505,879 | **694.54 msg/s** | parse errors 0

```
  tier    markets       msgs      msg/s    capture_B          B/s      B/msg
  Baseball       12     170831     234.54     90659647     124470.8      530.7
  Basketball        8      29776      40.88     15653768      21491.8      525.7
  CTRL_BTC15m        1        273       0.37       144144        197.9      528.0
  CTRL_Esports        4        236       0.32       128825        176.9      545.9
  CTRL_Golf        3        420       0.58       221028        303.5      526.3
  Soccer        8     301615     414.10    160182938     219922.6      531.1
  Tennis       12       2728       3.75      1444370       1983.0      529.5
```

判读(对照 §2/§3 的括号):
- **B/msg 实测 526–546,§2.3 的 531 B/msg 估计几乎精确命中。**
- **总速率 694.5 msg/s 高于 N=50 的"保守峰值" 565/s**——但分布极端偏斜:
  60% 来自 8 个 Soccer 市场(世界杯 NOR–ENG **赛中**,单市场峰 181.6
  msg/s),34% 来自 MLB(多场赛中)。**赛中 ≫ 赛前**得到直接证实;D-1
  主线(赛前窗口)的真实负载远低于本表——本表是"最热窗口"上界。
- 外推(若全天持续此热度):694.5 × 530 B ≈ 368 KB/s ≈ **31.8 GB/天**
  ——上界;赛前为主的 Stage 1 选择器实际量级预期显著更低,Stage 1 用
  真实 48h 采集数据重算 Stage 2 磁盘账。
- Tennis 夜间静默(3.75 msg/s/12 市场)佐证"清单必须临跑前生成 +
  选择器要按赛程时区轮动"。
