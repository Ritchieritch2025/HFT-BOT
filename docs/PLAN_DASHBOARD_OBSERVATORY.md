# PLAN — Dashboard Observatory & Redesign (MASTER_SEQUENCE STEPs 2–3)

Phase advanced: 1→1.5 operational readiness (P1). Gate satisfied: none directly —
this is the monitoring substrate the Phase-2 gates (and eventually S1 live
sessions) are *read through*. A dashboard that lies blocks every later gate (D2).

Status: **W-D0 requirements APPROVED by operator 2026-07-07** (this doc, §2–§3).
Independently audited 2026-07-07 (docs/plan_audits/
dashboard_wD0_requirements_audit_2026-07-07.md): findings A1–A4, B2–B4, C1–C4
remediated in this revision. W-D1 design is the next STOP.

Sequencing note: MASTER_SEQUENCE puts STEP 1 (AWS) before STEPs 2–3; the
operator initiated W-D0 early on 2026-07-07 — permissible because W-D0/D1 are
paper-only STOPs that gate only W-D6 (amendment in MASTER_SEQUENCE STEP 2).

**B1 ordering — RULED by operator 2026-07-07:** the collector Ws (W-D2..D5)
queue behind STEP 1 and are built directly on EC2 after cutover (W-A4). No
Mac deployment of collectors. The header/W-D2 contradiction is resolved in
favor of the header.

---

## 1. Binding rules (audited at every W's acceptance)

- **TREND RULE** (operator-approved, MASTER_SEQUENCE STEP 2): every live
  statistic renders with a visual trend (sparkline/time-series), never a bare
  number. A bare number on any tab = acceptance FAIL. *Definition (audit B2):*
  a **live statistic** is a time-varying measurement used for health judgment
  (rates, latencies, lag, counts-over-time, PnL, budget consumption) — trend
  mandatory. **State** (gate status, kill-switch armed, git sha, catalog
  attributes like row counts/date ranges/paths) is exempt — rendered as status
  chips or plain values, no sparkline.
- **Artifact envelope (audit A4):** every observatory artifact (latency series
  header, incidents, notify, readiness, catalog) carries `schema_version`,
  `generated_at_us` (int64 µs UTC), `source_sha`, and a declared `max_age_s`.
  The frontend renders any artifact older than its max_age as UNKNOWN —
  a dead collector must never leave stale-green widgets. Max ages: latency
  series 120s; notify/readiness 180s; catalog 1800s.
- **Refresh cadence (audit A2):** frontend polls tab endpoints every 5s (SSE
  retained where it already exists). "Within one refresh cycle" in any
  acceptance below means ≤ 2× poll interval (≤ 10s).
- **S5 forever:** read-only, localhost-bound, live_order-class tools refused by
  the console. No trade button, no panic button (operator decision 2026-07-07:
  panic stays the standalone CLI per S3; the dashboard may *display* kill-switch
  state and a copyable panic command line, nothing more).
- **D2 fail-closed rendering:** every status element has an explicit UNKNOWN
  state rendered as attention-demanding (amber/red), never as green or blank.
  A collector that can't read its source reports UNKNOWN + why. Bounded/skipped/
  dropped counters are surfaced on the tab that consumed them.
- **E7 / P4:** collectors are supervised, off the hot path, and read files the
  pipeline already writes. No collector opens DuckDB in write mode; readers
  retry per D6. Nothing here may stall capture/ingest/export — the dashboard
  reads work/ artifacts, never the daemons' sockets or locks.
- **E3:** every runnable this plan adds enters tools.json with a safety class;
  check_registry green.

## 2. Requirements (W-D0 output — operator-approved 2026-07-07)

**Audience & mode (Q1):** single-operator console, two modes in one UI:
(a) live monitoring — is everything healthy *right now*; (b) production-
readiness — which gates are green, what blocks the next phase. Research
analysis stays in CLI/notebooks; the dashboard renders result artifacts only.

**First screen / Overview tab (Q2):**
1. Go/no-go banner: lifecycle gates + kill-switch state; any UNKNOWN ⇒ banner
   cannot be green. *Scope (audit B3):* the banner aggregates **deployed
   modules only**. MODULE NOT LIVE is a distinct, honest, banner-exempt state
   (grey, labeled, visually unmistakable from both green and UNKNOWN) for
   modules that have not shipped (Backtest/Strategy/Execution pre-STEP-6).
   Rendering a scaffold as green, or silently folding it into the banner to
   make it green, is a named D2 violation. UNKNOWN (expected data missing or
   over-age) always forces non-green.
2. Capture health: freshness, active-gap state (capture_alert.json), gap count
   trend from capture_gaps.csv.
3. Latency snapshot: WS RTT + full-chain latency, sparklined (TREND RULE).
4. Positions/PnL + **pessimistic fill bound** (Q2 anchor) — scaffolded until
   the execution module is live; renders MODULE NOT LIVE honestly.
5. Incident/alert stream (newest first), fed by the W-D3 incident log.

**Rewrite scope (Q3, operator decision):** KEEP the existing dashboard_server.py
backend skeleton — read-only HTTP + SSE, localhost, tools.json integration —
because it embodies audited S5/E3 properties. REBUILD the frontend as modular
tabs over a shared component kit (status chip, sparkline, kv-table, incident
row). New per-tab read-only JSON endpoints; existing endpoints preserved.

**Visual direction (Q4, operator decision):** dense terminal dark. GitHub-dark
palette retained, tabular-nums, high information density, minimal chrome.

**Tab specification:**

| Tab | Content | Data source (all read-only) |
|---|---|---|
| Overview | go/no-go, capture health, latency, PnL/pessimistic bound (scaffold), alerts | W-D2/D3/D4 outputs |
| Data | live pulling state (per-feed freshness, msg-rate trends), gap record browser, latency observatory (full time-series), capture safety status (watchdog/forced counters, D2 drop counters), **warehouse catalog browser**: every dataset with path, row count, date range, freshness — the "path to access all data pulled" | metrics.ndjson, capture_gaps.csv, capture_alert.json, W-D5 catalog |
| Backtest | scaffold: renders a result-artifact directory. Contract fixed NOW (§4): equity curve, fill distribution, **pessimistic vs optimistic bound side-by-side** (Q2), per-market breakdown. Empty dir ⇒ NO RESULTS, honest. | work/backtest/*.json (contract §4) |
| Strategy | scaffold: calibration params (mm_calibrate output) vs literature-default flag (Q4 anchor), economic-sign-test status (Q9), per-market candidacy/stats | work/mm/*.json |
| Execution | scaffold: order lifecycle monitor, rate-limit budget vs consumed, executor reserve state, cancel-on-disconnect / dead-man status, kill-switch armed state + copyable panic CLI line. STRICTLY read-only (S5). | future tradingd telemetry files |
| Tests | carried over: lifecycle check, core test runs, registry status | existing endpoints |
| Tools | carried over: safety-classed tool runner (live_order refused) | tools.json |

**Launch scope (operator decision):** Overview + Data fully functional at W-D6
acceptance; Backtest/Strategy/Execution ship as final layout + fixed data
contracts rendering explicit MODULE NOT LIVE states (D2-honest scaffolds).

**Non-goals:** no order entry, no panic button, no research plotting engine,
no remote exposure (localhost only; EC2 access via SSH tunnel post-migration).

## 3. W queue (numbering fixed by MASTER_SEQUENCE — operator-authored)

STEP 2 (backend, unblocked by W-D0/D1): W-D2 → W-D3 → W-D4 → W-D5.
STEP 3 (redesign): W-D0 ✅ → W-D1 (STOP) → W-D6 → W-D7. One W per fresh session.

## W-D2 — Latency & feed-health collector
Purpose:          One structured, append-only time-series for every number the
                  Overview/Data tabs sparkline: WS RTT, full-chain latency,
                  per-feed msg rate, ingest lag, freshness. The observatory's
                  backbone (TREND RULE needs history, not instants).
Blocked by:       STEP 1 cutover (W-A4) — operator-ruled 2026-07-07 (header).
                  Built on EC2.
Allowed reads:    work/metrics.ndjson; work/raw (timestamps only); existing
                  latency probe outputs (full_chain_latency).
Allowed writes:   tools/observatory_collect.py; work/observatory/latency.ndjson
                  (rotated, 512MB keep 3 like metrics) + latency_daily.ndjson
                  (audit C4: unbounded small daily downsample so long-horizon
                  trends survive rotation; Data tab "full time-series" = the
                  rotation window, longer horizons render from the downsample);
                  tests; tools.json entry;
                  supervisor wiring (append-only block, capture continuity stated).
Forbidden writes: capture/ingest/export code; anything under src/.
Acceptance:       fixture metrics stream ⇒ deterministic series rows; rotation
                  proven; collector death does NOT stall pipeline (kill -9 test);
                  UNKNOWN emitted when a source file is absent (D2).
Rollback:         remove supervisor line; delete work/observatory/.
Exit evidence:    commit; test tail; 1h of real series collected + row counts.

## W-D3 — Incident detector & log
Purpose:          Machine-readable incident stream: auth-401 bursts, capture
                  gap open/close (from W-C2's alert), freshness stalls, forced-
                  reconnect spikes. Feeds the Overview alert stream and W-D7's
                  401-replay acceptance.
Blocked by:       W-D2 (series to detect on); W-C2 artifacts (capture_alert.json
                  contract — cross-plan dep, audit C1); acceptance fixture
                  ARCHIVED 2026-07-07: tests/fixtures/incidents/
                  ws_shadow_401_lockout_2026-07-07.log (full live log, 488
                  401-lines, all lockout bursts) + quality_log slice — preserved
                  before raw/log rotation could destroy it.
Allowed reads:    work/observatory/; work/live/capture_alert.json;
                  work/quality_log.ndjson; ws_shadow.log (fallback path — audit
                  C3: forced_reconnects_ exists in ws_client but is NOT in
                  metrics.ndjson; log-parse is pinned by fixture; RIDER,
                  operator-gated: export forced= into metrics.ndjson —
                  capture-side change, D4 ingest test in the same change).
Allowed writes:   tools/incident_detect.py; work/observatory/incidents.ndjson;
                  work/observatory/notify.json (audit A2 — THE notification
                  file: {schema_version, generated_at_us, source_sha, active:[
                  {id, severity, kind, start_us, summary}]}; empty active list
                  when healthy; consumed by the banner and by W-A5's alert
                  routing); tests; tools.json; supervisor wiring (same rules
                  as W-D2).
Forbidden writes: capture/ingest/export code.
Acceptance:       replay of the ARCHIVED 2026-07-07 401 fixture ⇒ auth-lockout
                  incident(s) with correct burst boundaries + notify.json gains
                  an active entry; seeded gap fixture ⇒ open+close pair; healthy
                  day fixture ⇒ zero incidents + empty notify active list.
Rollback:         remove supervisor line; delete incidents.ndjson + notify.json.
Exit evidence:    commit; test tail; real replayed-incident record.

## W-D4 — Readiness/gates snapshot collector
Purpose:          The production-readiness half: lifecycle gate states, test-
                  suite freshness (when did make check last pass, on what sha),
                  registry status, standing gates (7-clean-days countdown,
                  fees OQ-1) — as one snapshot JSON the Overview banner reads.
Blocked by:       W-D2 (shared collector harness).
Allowed reads:    work/lifecycle_status.json + work/lifecycle_events.ndjson
                  (verified existing, written by tools/lifecycle_check.py —
                  audit C2 resolved; if a recorded run lacks git sha, sha
                  renders UNKNOWN, fail-closed); tools.json; git HEAD;
                  SESSION_LOG (dates only); capture_gaps.csv + the per-day
                  scan records; work/metrics.ndjson (coverage evidence).
Allowed writes:   tools/readiness_snapshot.py; work/observatory/readiness.json
                  (envelope per §1); tests; tools.json entry; supervisor wiring
                  (audit A4 — cadence: every 60s, same append-only rules as
                  W-D2).
Forbidden writes: everything else.
Acceptance:       fixture inputs ⇒ deterministic snapshot; any missing input ⇒
                  that gate = UNKNOWN, overall = NOT-GO (fail-closed, S2/D2);
                  **clean-day requires POSITIVE coverage evidence** (audit A3):
                  a day counts clean only if (a) its capture_gaps scan record
                  exists, (b) its metrics/freshness series is present and
                  continuous, AND (c) zero gaps recorded. A day with no
                  evidence — collector down, scan never ran, day pruned
                  unscanned — is NOT clean; the 7-clean-days counter renders
                  UNKNOWN, never advances on silence. Red-first test: absent-
                  evidence fixture day ⇒ counter UNKNOWN.
Rollback:         delete tool + snapshot; remove supervisor line.
Exit evidence:    commit; test tail; real snapshot rendered in existing console.

## W-D5 — Warehouse catalog builder (Data-tab access path)
Purpose:          "A path to access all the data pulled": walk archive/staging/
                  event_packs/warehouse, emit catalog.json — dataset, path,
                  format, row count, date range, freshness, provenance
                  (ws_capture vs rest_backfill, never merged silently).
Blocked by:       W-D2 (harness); pairs with PLAN_EVENT_PACKAGING outputs.
Allowed reads:    work/ tree (read-only walk); DuckDB read-only with D6 retry.
Allowed writes:   tools/warehouse_catalog.py; work/observatory/catalog.json
                  (envelope per §1); its (path,size,mtime) count cache;
                  tests; tools.json entry; supervisor wiring (audit A4 —
                  cadence: every 10 min, niced/backgrounded).
Forbidden writes: any data file it catalogs (it is a pure reader).
Acceptance:       fixture tree ⇒ exact catalog; a DuckDB held by a writer ⇒
                  retry-then-UNKNOWN, never a lock conflict (D6); row counts
                  match a direct count on fixtures; **IO budget** (audit C4):
                  unchanged files (same path+size+mtime) are NEVER re-counted —
                  proven by a fixture rerun with a touched-nothing tree
                  completing without opening data files.
Rollback:         delete tool + catalog.json.
Exit evidence:    commit; test tail; real catalog covering 07-06→today.

## W-D1 — Design (STOP: operator approves before W-D6)
*(AMENDED 2026-07-08, operator-approved: v1 static mock REJECTED — grey
placeholder aesthetic + hardcoded fake data made design review meaningless.
W-D1 is now a READ-ONLY LIVE PROTOTYPE, not a static mock.)*
Purpose:          Throwaway sandbox prototype of all 7 tabs driven by REAL
                  existing local files — zero fabricated numbers. Every panel
                  either reads a real source or renders MODULE NOT LIVE;
                  faking a series in a design artifact is itself a D2-spirit
                  violation (it lied to the operator, hence this amendment).
                  Must demonstrate: dense dark terminal aesthetic (§2 Q4),
                  three-way green/UNKNOWN/MODULE NOT LIVE distinction (B3),
                  stale-artifact UNKNOWN rendering (§1 envelope), TREND RULE
                  sparklines from real history (B2).
Blocked by:       W-D0 ✅ incl. audit remediation 2026-07-07.
Allowed reads:    this doc; existing dashboard_server.py; READ-ONLY:
                  work/metrics.ndjson, work/event_packs/capture_gaps.csv,
                  work/lifecycle_status.json, work/live/capture_alert.json,
                  work/quality_log.ndjson.
Allowed writes:   docs/plan_audits/dashboard_design_wD1.md + prototype under
                  sandbox/ (throwaway; may include a trivial localhost-only
                  read-only file server for preview — run manually, never
                  supervised/launchd, killed after review).
Forbidden writes: dashboard_server.py, anything production; no daemons, no
                  supervisor entries, no writes to work/.
Acceptance:       operator reviews the LIVE prototype and approves IN WRITING
                  (SESSION_LOG + amendment note here). No W-D6 code before
                  that. Zero fabricated data points — spot-checkable: every
                  rendered number traceable to a source file line/row.
Rollback:         delete sandbox/ prototype.
Exit evidence:    approved design doc committed; prototype screenshot set.

**DESIGN SYSTEM RULES (operator feedback 2026-07-08 — bind W-D1 v2+ and all
future tabs; dashboard_design_wD1.md must restate them as its style section):**
- Scan, don't showcase: 11-12px monospace, tabular right-aligned numerals,
  flat 1px-border panels, radius ≤2px, NO gradients/glow/animation (a live
  tick dot is the only exception). Color carries meaning only: green=ok,
  amber=warn/UNKNOWN, red=bad, dim=not-live.
- No hero charts, no KPI tiles: headline stats = one inline status strip
  with mini-sparklines; charts = equal-height small multiples (~120px,
  shared time axis, min/avg/max in each header).
- Gap/coverage displays are TIMELINES (00:00–24:00 strip per day, events at
  true positions/durations), never proportional pills.
- Dense tables for state (gates, catalogs): one row per item, reason text
  dim inline. Raw tail/tape panel at the bottom of live tabs.
- Counters render as rates (trades/min), never cumulative sparklines.
- Axes never lie: zero-floored where negatives are impossible; no decorative
  autoscale padding. Any rendered figure that disagrees with the underlying
  record is marked UNVERIFIED until reconciled (D2).
- Density acceptance: a tab's information must fit ~3× tighter than the
  rejected v2 hero-tile layout; if a panel has more padding than content,
  shrink it.
- GENERALIZATION RULE: new tabs/modules are assembled from this component
  kit and fed via the §1 artifact envelope — adding a module must require
  zero dashboard code beyond registering its artifact + choosing components.

## W-D6 — Frontend rebuild + tab endpoints
Purpose:          Implement §2 on the kept backend: modular frontend, shared
                  component kit, per-tab read-only JSON endpoints reading
                  work/observatory/. Overview+Data full; Backtest/Strategy/
                  Execution as contract-true scaffolds.
Blocked by:       W-D1 approval + W-D2..D5 outputs existing.
Allowed reads:    work/observatory/; existing endpoints; AND (audit A1 —
                  granted here explicitly, not improvised at build time) the pipeline-owned read-only files the
                  §2 table names: work/event_packs/capture_gaps.csv,
                  work/live/capture_alert.json, work/metrics.ndjson (tail).
                  Rationale: no mirroring layer — copying them into observatory
                  would add a staleness hop for zero safety gain; the server
                  already reads work/ files read-only.
Allowed writes:   dashboard_server.py (endpoints + templates); static assets;
                  tests (endpoint contract tests + a headless render smoke).
Forbidden writes: collectors (they're done); capture/ingest/export; tools.json
                  live_order classes.
Acceptance:       every §2 first-screen element present; TREND RULE audit: zero
                  bare live numbers; kill-switch/PnL scaffolds show MODULE NOT
                  LIVE; server still binds localhost only; live_order refusal
                  test still green (S5 regression pinned).
Rollback:         git revert (frontend-only change).
Exit evidence:    commit; endpoint test tail; screenshot set of all 7 tabs.

## W-D7 — End-to-end acceptance + whole-plan audit
Purpose:          Prove the console tells the truth under real failure.
Blocked by:       W-D6.
Allowed reads:    everything above.
Allowed writes:   fixes found by the audit; docs.
Forbidden writes: new features.
Acceptance:       replay the ARCHIVED 401 fixture through W-D3 ⇒ Overview
                  banner degrades + incident row + notify.json entry, all
                  visible within one refresh cycle (≤10s per §1 cadence);
                  pull a source file ⇒ affected widgets go UNKNOWN (never
                  stale-green); **stop each collector** (audit A4) ⇒ its
                  widgets degrade to UNKNOWN within max_age + one refresh;
                  independent fresh-context adversarial audit of
                  the whole plan (mandatory — it has caught defects every time).
Rollback:         n/a.
Exit evidence:    audit writeup in docs/plan_audits/; SESSION_LOG entry.

## 4. Backtest result-artifact contract (fixed now so the future engine targets it)

`work/backtest/<run_id>/result.json`: {**schema_version: 1** (audit B4 —
renderer rejects unknown majors with an explicit UNSUPPORTED SCHEMA state,
never a guess), run_id, git_sha, params, date_range, markets[],
equity_curve[[ts,pnl_pessimistic,pnl_optimistic]], fills[{ts,market,
side,px_e4,qty,queue_model}], summary{n_fills, pnl_pessimistic, pnl_optimistic,
max_drawdown, fees_paid}}.

Units (audit B4, pinned): `ts` = int64 **microseconds UTC** (same convention as
capture_gaps start_us/end_us); `px_e4` = int64, price in cents × 10⁴ (E4, D5);
all money fields (`pnl_*`, `max_drawdown`, `fees_paid`) = int64 cents × 10⁴
(E4, fees rounded up per Q3 before summation); `qty` = int64 whole contracts.
No floats anywhere in the file (D5).

**Pessimistic bound is the headline number everywhere; optimistic is
diagnosis-only** (Q2). The Backtest tab renders any conforming file; the
engine (STEP 6+) writes them.

## 5. Self-audit against GUARDRAILS §6

1. Advances 1→1.5 readiness, skips no gate — yes. 2. Live orders — none, S5
hardened with a pinned regression test (W-D6). 3–4. No strategy math here;
where PnL appears the pessimistic bound is contractual (§4). 5. No trading
data path touched. 6. Every W ships tests. 7. Pipeline: collectors are
supervised readers; kill -9 test proves non-interference (W-D2); supervisor
edits are append-only. 8. All rollbacks are file deletions or reverts.
9. RUNBOOK gains an observatory section at W-D6 (E5). 10. D2 is a named
acceptance criterion on every W with UNKNOWN states specified.
