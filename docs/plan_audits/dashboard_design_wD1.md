# W-D1 — Dashboard Observatory design (LIVE prototype; STOP: awaiting operator approval)

Per the W-D1 amendment (commit e897141): **not a static mock — a read-only LIVE
prototype driven by real local files, zero fabricated numbers.** Every rendered
figure is traceable to a `work/` source line/row (acceptance hard clause).

- **Prototype:** `sandbox/obs_server.py` (read-only HTTP, localhost, no
  order/panic — S5; run MANUALLY, never supervised) + `sandbox/obs.html`.
  Run: `python3 sandbox/obs_server.py` → `http://127.0.0.1:8790/`.
- **v4 interaction:** charts use **uPlot** (vendored `third_party/uplot`
  v1.6.30, pinned, NO CDN) — box-zoom / dbl-click-reset / crosshair synced
  across the small multiples. Data arrives by **SSE push** (`/stream`:
  `snapshot`→`feed`→`aux` events + `: ping`, mirroring dashboard_server.py),
  not polling. A DuckDB query panel is deferred to W-D6's Data tab (not W-D1).
- **Real sources:** `metrics.ndjson` (feed health series), `capture_gaps.csv`
  (gap intervals), `capture_alert.json` (live active-gap/freshness),
  `lifecycle_status.json` (gates). No mirroring layer (audit A1).
- **P8:** sandbox is free-fire — this iterates without W/audit ceremony; the hard
  line is that production `dashboard_server.py` must never import sandbox. W-D6
  (the real frontend) still needs this design approved IN WRITING + W-D2..D5
  collectors built.

## Binding style — DESIGN SYSTEM RULES (from PLAN_DASHBOARD_OBSERVATORY W-D1, c5d45bb)
Restated here as the acceptance style section:
1. **Scan, don't showcase** — 11-12px monospace, tabular right-aligned numerals,
   flat 1px panels, radius ≤2px, **no gradients/glow/animation** (live tick dot
   is the sole exception). Colour = meaning only: green=ok, amber=warn/UNKNOWN,
   red=bad, dim=not-live.
2. **No hero charts / no KPI tiles** — headline stats = one inline status strip
   with mini-sparklines; charts = equal-height **small multiples** (~120px,
   shared time axis, **min/avg/max in each header**).
3. **Gap/coverage = timelines** — 00:00–24:00 strip per day, events at true
   position/duration; never proportional pills.
4. **Dense state tables** — one row per item, reason text dim inline. Raw
   tail/tape panel at the bottom of live tabs.
5. **Counters as rates** (trades/min), never cumulative curves.
6. **Axes never lie** — zero-floored where negatives are impossible; no
   decorative autoscale padding. Any figure disagreeing with its record →
   UNVERIFIED until reconciled (D2).
7. **Density** — ~3× tighter than a hero-tile layout; a whole tab fits ~1 screen.
8. **Generalisation** — new tabs/modules assemble from the component kit fed by
   the §1 artifact envelope; adding a module = register artifact + pick
   components, zero bespoke dashboard code.

## Decision-driven information architecture (v5 — operator 2026-07-08)
Panels are not chosen by "what data we have" but by **which operator decision
they drive**. Every panel MUST declare, inline, (a) the question it answers and
(b) the action an anomaly triggers. A panel that answers no question is deleted.
Four questions → four zones:
- **① HEALTHY NOW?** — per-feed last-msg-age, rate vs a **yesterday-same-time
  baseline band**, disk, process liveness. (Answers: is the machine capturing
  right now? Action: dead proc / stale feed → restart.)
- **② DATA USABLE?** — day×category **coverage matrix heatmap**, latency
  **p50/p95/p99 distribution** (mean forbidden as a headline), parse/drop/corrupt
  counts, **7-clean-days progress with evidence status**. (Answers: is what we
  captured complete + trustworthy? Action: thin cell / p99 spike / drop>0 →
  investigate that slice.)
- **③ WHAT'S MISSING FOR TRADING?** — the lifecycle gates table (kept).
- **④ INCIDENT FORENSICS** — clickable incident list that jumps a timeline to
  the event; gap / reconnect / error aligned on one axis.

### Granularity — three binding principles (bind every panel)
1. **Distribution over average** — never headline a mean. Latency, fill, queue:
   render p50/p95/p99 (+max). The tail is the decision.
2. **Breakdown over global** — split by the axis that localises a fault:
   per-feed / per-category / per-channel, never one aggregate number that hides
   which slice broke.
3. **Baseline over bare number** — a live figure is shown against its own
   history/peer band (yesterday-same-time, 7-day p10–p90), so "is this normal?"
   is answerable at a glance. A bare number that can't be judged is a defect.

## Delivered build — the four zones, every panel → its real source
Backend endpoints `/api/q1 /api/q2 /api/incidents /api/gates` (+ pushed in the
SSE `snapshot`/`aux`). Every panel declares its question + anomaly action inline.

**① HEALTHY NOW?**
| Panel | Kind | Source (real) |
|---|---|---|
| Process & feed liveness | state | `pgrep` ws_shadow/ingest/supervisor (+pid,uptime) · `metrics.ndjson` feed age |
| Rate vs baseline | live+baseline | now `msg_rate_hz` vs **multi-day same-clock-time median band** (binary-searched in `metrics.ndjson`); **auto-degrades + flags** when comparable days were gaps |
| Channel breakdown | live-breakdown | trade/s vs ticker/s (Δ over last 2 feed records) |
| Disk | state | `os.statvfs(work/)` |

**② DATA USABLE?**
| Panel | Kind | Source (real) |
|---|---|---|
| Coverage matrix | breakdown+baseline | `warehouse/manifest.csv` day×category rows; **each cell coloured by % vs that category's own median** (naturally-small ≠ shortfall) |
| Latency distribution | distribution | `metrics.ndjson` `freshness_ms` → p50/p95/p99/max (**mean banned**) |
| 7-clean-days progress | state+evidence | `capture_gaps.csv` + per-day scan evidence (clean/gaps/no-scan; silence never counts, A3) |
| Integrity counts | state | feed `recorder_dropped`/`telemetry_dropped`; corrupt = "not persisted" (honest) |

**③ WHAT'S MISSING FOR TRADING?** — `lifecycle_status.json.stages[]{status,blocking_reason,checks}`, one row/gate.

**④ INCIDENT FORENSICS** — `capture_gaps.csv` (gaps; a gap's end = a reconnect/recovery mark) + `quality_log.ndjson` (401/data-loss) on one timeline (07-06→now). **Click a row → the timeline guide jumps to it.** CASE #1 is PINNED: `2026-07-08 02:00:00→02:02:59` (179s), labelled `W-C5?` — candidate W-C5 (hour-boundary non-zero-exit + 15s retry loop, NOT a wedge, forced=0); it blocks the 7-clean-days gate.

**Provenance (hard clause):** every figure carries a `⌕`; clicking opens a drawer
with the source record — coverage cell → the `manifest.csv` rows (path+md5),
feed → the raw metrics record, latency → the `freshness_ms` sample window.

Not-yet-live modules (Backtest/Strategy/Execution) + WS-RTT latency (W-D2, EC2
post-cutover) render **MODULE NOT LIVE / UNKNOWN**, never faked.

## Full design scope — five contracts (①–⑤)
These were in the issued W-D1 scope. Each is specified here (contract pinned) and
marked BUILT / DEFERRED (blocked-by). Deferred items reserve their data contract
now so the collectors/engines target it; none is faked in the prototype.

### ① Analytics identity contract + generic catalog renderer  (contract PINNED; renderer DEFERRED → W-D6)
Every analytics result under `work/analytics/<name>/result.json` MUST carry a
`definition` block (an analysis "ID card") — no anonymous number reaches a panel:
```
{ schema_version, generated_at_us, source_sha, max_age_s,       // §1 envelope
  definition: {
    id, title,
    formula:  "precise expression, e.g. markout(τ)=s·(mid(t+τ)−p)",
    plain:    "one-paragraph plain-language meaning",
    inputs:   [{dataset, fields, provenance}],   // what it reads (traceable)
    window:   {start_us, end_us, tz:"UTC"},
    params:   { ... },                           // horizons, thresholds, knobs
    units:    "e4-cents | ms | count | log-odds",
    render:   "distribution | timeseries | table | heatmap | scalar-baseline" },
  result: <payload matching `render`> }
```
The **generic analytics catalog renderer** walks `work/analytics/`, shows each
`definition` as a foldable ID card, then renders `result` via the component named
by `render` — **zero per-analysis code** (generalisation rule §1/P8). A new
analysis appears by dropping a conforming file; over-age envelope → UNKNOWN.
Prototype today: no `work/analytics/` producers exist yet → renderer DEFERRED to
W-D6; markout (⑤) is the first consumer.

### ② Data catalog browser + DuckDB query panel  (DEFERRED → W-D6 Data tab, blocked-by W-D5)
- **Catalog browser:** from W-D5 `catalog.json` — every dataset {name, path,
  format, rows, date_range, freshness, provenance}. An archived **parquet is
  clickable → preview** (first N rows, read-only) + a **pre-filled SQL** scoped to
  that file/partition.
- **DuckDB query panel — the FOUR RAILS (bright-line, all four mandatory):**
  1. **READ-ONLY** — SELECT/WITH/DESCRIBE/EXPLAIN only; allowlist rejects
     DDL/DML/PRAGMA/COPY/ATTACH-write; warehouse attached `READ_ONLY` with D6
     retry — never the writer, never holds a lock the ingest daemon needs.
  2. **BOUNDED** — server-injected `LIMIT` (default 10k) + statement timeout +
     memory/temp cap; truncation is SURFACED (row-count + `truncated` flag, D2 —
     never silent).
  3. **S5-SEALED** — localhost only, no network egress, no order/panic; a reader
     like every other panel; results never leave localhost.
  4. **PROVENANCE-STAMPED** — every result carries the exact SQL + the
     files/partitions read + row count + `generated_at_us`; exportable but stamped.
- Blocked-by: W-D5 (catalog) + a read-only DuckDB reader (W-D6, on EC2).

### ③ Activity heatmap group — day/week/month  (Week tier BUILT as coverage matrix; Day/Month DEFERRED, blocked-by activity_daily collector W-D4/D5)
- **Three tiers:** **Day** = hour(0–23) × category (intraday coverage); **Week** =
  day × category × 7d (the delivered ② coverage matrix is this tier); **Month** =
  day × category × ~30d from the rollup.
- **`activity_daily` rollup contract:** `work/observatory/activity_daily.ndjson`,
  one row per (date, category): `{date, category, rows_by_table{trades,l1,…},
  msg_rate_p50, active_hours, envelope}` — unbounded small daily downsample (like
  latency_daily, audit C4) so month/quarter horizons survive raw/staging rotation.
- **Gap diagonal-hatch linkage:** capture_gaps intervals overlaid — any hour/day
  cell overlapping a recorded gap is **diagonally hatched** (07-06 capture-start
  style), so "low activity" ≠ "capture was down" is never confused (D2); a hatched
  cell → the ④ incident.

### ④ Event timeline view  (DEFERRED, blocked-by W-E1)
- Purpose: market events (e.g. matches) on a timeline — open/active/settle windows
  aligned with capture gaps + trades, to answer "was capture healthy during this
  event?".
- **Reserved data contract (from the W-E1 event index):** `{event_ticker,
  market_ticker, series, category, open_time, event_start_time, close_time,
  status, mve_flags}`.
- **D2 WARNING (pinned):** `close_time ≠ event/game-start time`. Kalshi's
  `close_time` is settlement/close, often well after the event actually starts.
  Rendering the event window off `close_time` would MISLEAD. The view MUST use the
  true `event_start_time` (event index / catalog / derived) and mark `close_time`
  separately as "settlement". Until W-E1 supplies the real start, this view
  renders **BLOCKED-BY W-E1** — never faked.

### ⑤ markout / toxicity analytics  (FUTURE, formula + stages PINNED)
- **Markout** (adverse selection): for a fill at price `p`, `side` s∈{+1,−1}, time
  `t`, and mid `m`: **`markout(τ) = s · (m(t+τ) − p)`** in E4 cents; negative =
  adverse (picked off). Horizons τ ∈ {1s, 5s, 30s, 60s, to-settlement} — a
  **distribution** per horizon (p10/p50/p90), never a mean.
- **Toxicity:** fraction of notional with adverse markout beyond a threshold at a
  horizon, broken down **per market / per liquidity-context** (breakdown rule).
- **Stages:** quote → fill → markout(τ) → settlement; each stage's inputs pinned.
- **Contract:** emitted as an ① analytics result `work/analytics/markout/result.json`
  (`definition.formula` above, `inputs`: fills[] from tradingd/backtest + L1 mid
  series from the warehouse, `params`: horizons+threshold, `units`: e4-cents,
  `render`: distribution) → renders via the ① generic renderer, zero bespoke code.
- Blocked-by: fills exist only once execution/backtest ships (STEP 6). Contract
  pinned now so the engine targets it.

## Productionisation order + decisions (operator 2026-07-08)
- **Zone flow stays UN-SPLIT** — the four-zone Overview is one view, no
  Live/Readiness tab split.
- **Graduation order ①→④→②→③, riding the collector build W-D2→D3→D4/D5:**
  ① healthy-now on W-D2 (latency/feed-health series) → ④ incident forensics on
  W-D3 (incident detector) → ② data-usable on W-D4/D5 (readiness + catalog) →
  ③ gates on W-D4 (readiness). Sandbox prototype proves all four now on real
  files; production wiring follows this order.

## Reconciliations (D2)
- **freshness < 0** → sub-ms clock jitter (record stamped µs ahead of receipt);
  floored to 0 in health views + zero-floored axes; raw value kept only in the
  METRICS TAPE.
- **07-08 "2 gaps"** VERIFIED against `capture_gaps.csv`: `00:00→00:10:25`
  (midnight pre-deploy) + `02:00→02:02:59` (W-C5 respawn). Both real; the second
  is CASE #1 in zone ④.

## Status of the five contracts
| # | Item | Status |
|---|---|---|
| ① | analytics identity contract | contract PINNED · generic renderer DEFERRED → W-D6 (no `work/analytics/` producers yet) |
| ② | data catalog browser + DuckDB four rails | DEFERRED → W-D6 Data tab · blocked-by W-D5 |
| ③ | activity heatmap day/week/month | Week tier BUILT (coverage matrix) · Day/Month DEFERRED · blocked-by `activity_daily` collector (W-D4/D5) |
| ④ | event timeline view | DEFERRED · blocked-by W-E1 · close_time≠start D2 warning pinned |
| ⑤ | markout / toxicity | FUTURE · formula+stages+contract PINNED · blocked-by execution/backtest (STEP 6) |

Operator decisions (2026-07-08) recorded: four-zone flow un-split;
productionisation ①→④→②→③ per W-D2→D3→D4/D5.

## STOP — resubmitted for approval
The Overview four zones are BUILT on real files; the five broader-scope contracts
(①–⑤) are now specified (built or deferred with blocked-by). W-D1 acceptance =
operator approves IN WRITING (SESSION_LOG + a plan amendment note); no
`dashboard_server.py` (W-D6) code before that. Known honest limits: coverage/rate
baselines strengthen as clean days accumulate (~2 archived days today, both
gappy); corrupt-line count needs the W-C2.1 daily wiring active (dormant until the
supervisor restarts).

## OPERATOR ADDENDUM (2026-07-08, part of the approval — binding as contracts ⑥⑦)

⑥ **Live·partial "today" column** (all heatmaps/coverage matrices): closed days
render from rollup/archive (authoritative, immutable); TODAY renders as a
separate column incrementally accumulated from the already-tailed SSE stream
(in-memory, category×hour buckets via the config classification map; no new
full scans). The column header reads IN PROGRESS and is visually distinct
(hatch/translucent) — a half-elapsed day must not read as "volume halved" (D2).
At day-end export the column switches to the archived authoritative values;
the live-accumulated vs archived delta is logged, and any delta >1% is surfaced
in the provenance drawer (reconciliation principle).

⑦ **Per-panel data-layer annotation**: every panel declares which storage tier
feeds it — raw / staging / archive / rollup / live-stream — and that tier's
expected update cadence, rendered in the panel's provenance header next to the
source file. A panel that "looks static" must be answerable in one glance:
which tier, when does it refresh. (Origin: the 07-08 "why is the heatmap not
dynamic" confusion — heatmaps fed from day-end archive while the operator
expected live.)

## OPERATOR APPROVAL — W-D1 APPROVED (written, 2026-07-08)

Approved by operator via advisor session, 2026-07-08: four-zone decision-driven
IA (un-split), dense-terminal style per DESIGN SYSTEM RULES, contracts ①–⑤ as
specified (built or deferred with stated blocked-by), plus addendum contracts
⑥–⑦ above. Productionisation order ①→④→②→③ riding W-D2→D3→D4/D5. The design
is FROZEN as of this approval; changes henceforth require an operator-approved
amendment recorded here and in SESSION_LOG. W-D6 remains blocked by W-D2..D5
(B1 ruling: collectors built on EC2 after STEP 1 cutover).


## 契约① 补充字段（操作员 2026-07-08）：source
definition 块新增必填字段 `source`：该公式的思想出处（论文引用 / 自研推导）
+ 本地化改造说明（如 "Avellaneda-Stoikov 保留价格式，按 Q1 移植至 log-odds
空间"）。目的：任何渲染数字可追问到思想族谱；与 P5（外来思想审查）、
Q4（文献给结构、数据给参数）闭环。
