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

(v5 supersedes the v3 per-tab mapping below for the Overview → Q1/Q2 zones; the
component→source table still documents each widget's real feed.)

## v3 Overview — component → real source (all live, 3s poll)
| Component | Kind | Source (real) |
|---|---|---|
| Verdict + header live strip | state | `lifecycle_status.json` + `metrics.ndjson` latest feed |
| Status strip (rate/fresh/trade-rate/recon/drop/capture) | live+state | `metrics.ndjson` feed record + derived trade-rate (Δtrades/Δt) |
| Small multiples (msg_rate_hz, freshness_ms, trades/min) | live→chart | `metrics.ndjson` feed series; freshness zero-floored |
| Capture-gap timeline (per-day 00:00–24:00 bands) | timeline | `capture_gaps.csv` intervals; 07-06 hatched=capture-start |
| Capture state table | state | `capture_alert.json` + feed record (drops, msgs, trades) |
| Lifecycle gates (one row/item) | state | `lifecycle_status.json.stages[]{status,blocking_reason,checks}` |
| Metrics tape | raw | `metrics.ndjson` feed tail |

Not-yet-live tabs (Backtest/Strategy/Execution) + the WS-RTT latency collector
(W-D2, EC2 post-cutover) render **MODULE NOT LIVE / UNKNOWN**, never faked.

## Reconciliations (D2)
- **freshness < 0**: raw records carry sub-ms negative freshness (record stamped
  µs ahead of receipt = clock jitter). Floored to 0 in all health views; raw
  value kept only in the diagnostic METRICS TAPE. Axis zero-floored.
- **07-08 "2 gaps"**: VERIFIED against `capture_gaps.csv` — `00:00→00:10:25`
  (midnight pre-deploy hole) + `02:00→02:02:59` (the W-C5 hour-boundary respawn
  gap). Both real; timeline tags `·02:00=W-C5`. Not a rendering error.

## STOP
W-D1 acceptance = operator approves this live prototype IN WRITING (SESSION_LOG +
a plan amendment note). No `dashboard_server.py` (W-D6) code before that. Open
questions for approval: density feel, whether to add an explicit Live/Readiness
split, and which of the 7 tabs to build first once W-D2..D5 exist.
