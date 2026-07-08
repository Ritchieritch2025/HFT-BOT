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

## Reconciliations (D2)
- **freshness < 0** → sub-ms clock jitter (record stamped µs ahead of receipt);
  floored to 0 in health views + zero-floored axes; raw value kept only in the
  METRICS TAPE.
- **07-08 "2 gaps"** VERIFIED against `capture_gaps.csv`: `00:00→00:10:25`
  (midnight pre-deploy) + `02:00→02:02:59` (W-C5 respawn). Both real; the second
  is CASE #1 in zone ④.

## STOP — ready for approval review
The four zones are complete and driven entirely by real files. W-D1 acceptance =
operator approves IN WRITING (SESSION_LOG + a plan amendment note); no
`dashboard_server.py` (W-D6) code before that. Open questions for the operator:
(1) density/feel OK? (2) explicit Live/Readiness split, or keep the 4-zone flow?
(3) once W-D2..D5 collectors exist on EC2, which zone graduates to production
first? Known limits (honest): coverage/rate baselines strengthen as clean days
accumulate (only ~2 archived days today, both gappy); corrupt-line count needs
the capture_gaps daily wiring active (W-C2.1, dormant until the supervisor
restarts).
