# PLAN — Dashboard Observatory & Redesign (MASTER_SEQUENCE STEPs 2–3)

Phase advanced: 1→1.5 operational readiness (P1). Gate satisfied: none directly —
this is the monitoring substrate the Phase-2 gates (and eventually S1 live
sessions) are *read through*. A dashboard that lies blocks every later gate (D2).

Status: **W-D0 requirements APPROVED by operator 2026-07-07** (this doc, §2–§3).
W-D1 design is the next STOP. Sequencing note: MASTER_SEQUENCE puts STEP 1 (AWS)
before STEPs 2–3; the operator initiated W-D0 early on 2026-07-07 — permissible
because W-D0/D1 are paper-only STOPs that gate only W-D6 (amendment in
MASTER_SEQUENCE STEP 2). Implementation Ws still queue behind STEP 1.

---

## 1. Binding rules (audited at every W's acceptance)

- **TREND RULE** (operator-approved, MASTER_SEQUENCE STEP 2): every live
  statistic renders with a visual trend (sparkline/time-series), never a bare
  number. A bare number on any tab = acceptance FAIL.
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
   cannot be green.
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
Blocked by:       nothing (amendment 2026-07-07); runs on Mac now, EC2 later.
Allowed reads:    work/metrics.ndjson; work/raw (timestamps only); existing
                  latency probe outputs (full_chain_latency).
Allowed writes:   tools/observatory_collect.py; work/observatory/latency.ndjson
                  (rotated, 512MB keep 3 like metrics); tests; tools.json entry;
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
Blocked by:       W-D2 (series to detect on).
Allowed reads:    work/observatory/; work/live/capture_alert.json;
                  work/quality_log.ndjson; ws_shadow.log.
Allowed writes:   tools/incident_detect.py; work/observatory/incidents.ndjson;
                  tests; tools.json; supervisor wiring (same rules as W-D2).
Forbidden writes: capture/ingest/export code.
Acceptance:       replay of the REAL 2026-07-07 401 segment ⇒ exactly one
                  auth-lockout incident with correct [start,end]; seeded gap
                  fixture ⇒ open+close pair; healthy day fixture ⇒ zero incidents.
Rollback:         remove supervisor line; delete incidents.ndjson.
Exit evidence:    commit; test tail; real replayed-incident record.

## W-D4 — Readiness/gates snapshot collector
Purpose:          The production-readiness half: lifecycle gate states, test-
                  suite freshness (when did make check last pass, on what sha),
                  registry status, standing gates (7-clean-days countdown,
                  fees OQ-1) — as one snapshot JSON the Overview banner reads.
Blocked by:       W-D2 (shared collector harness).
Allowed reads:    lifecycle_check outputs; tools.json; git HEAD; SESSION_LOG
                  (dates only); capture_gaps.csv (clean-day computation).
Allowed writes:   tools/readiness_snapshot.py; work/observatory/readiness.json;
                  tests; tools.json entry.
Forbidden writes: everything else.
Acceptance:       fixture inputs ⇒ deterministic snapshot; any missing input ⇒
                  that gate = UNKNOWN, overall = NOT-GO (fail-closed, S2/D2).
Rollback:         delete tool + snapshot.
Exit evidence:    commit; test tail; real snapshot rendered in existing console.

## W-D5 — Warehouse catalog builder (Data-tab access path)
Purpose:          "A path to access all the data pulled": walk archive/staging/
                  event_packs/warehouse, emit catalog.json — dataset, path,
                  format, row count, date range, freshness, provenance
                  (ws_capture vs rest_backfill, never merged silently).
Blocked by:       W-D2 (harness); pairs with PLAN_EVENT_PACKAGING outputs.
Allowed reads:    work/ tree (read-only walk); DuckDB read-only with D6 retry.
Allowed writes:   tools/warehouse_catalog.py; work/observatory/catalog.json;
                  tests; tools.json entry.
Forbidden writes: any data file it catalogs (it is a pure reader).
Acceptance:       fixture tree ⇒ exact catalog; a DuckDB held by a writer ⇒
                  retry-then-UNKNOWN, never a lock conflict (D6); row counts
                  match a direct count on fixtures.
Rollback:         delete tool + catalog.json.
Exit evidence:    commit; test tail; real catalog covering 07-06→today.

## W-D1 — Design (STOP: operator approves before W-D6)
Purpose:          Wireframe + component spec for all 7 tabs: layout per tab,
                  every widget mapped to its W-D2..D5 field, every status
                  element's UNKNOWN rendering specified (D2), sparkline
                  placement per TREND RULE. Static HTML mock, no live data.
Blocked by:       W-D0 ✅.
Allowed reads:    this doc; existing dashboard_server.py.
Allowed writes:   docs/plan_audits/dashboard_design_wD1.md + static mock under
                  sandbox/ (throwaway).
Forbidden writes: dashboard_server.py, anything production.
Acceptance:       operator reviews the mock and approves IN WRITING (SESSION_LOG
                  + amendment note here). No code before that.
Rollback:         n/a (paper).
Exit evidence:    approved design doc committed.

## W-D6 — Frontend rebuild + tab endpoints
Purpose:          Implement §2 on the kept backend: modular frontend, shared
                  component kit, per-tab read-only JSON endpoints reading
                  work/observatory/. Overview+Data full; Backtest/Strategy/
                  Execution as contract-true scaffolds.
Blocked by:       W-D1 approval + W-D2..D5 outputs existing.
Allowed reads:    work/observatory/; existing endpoints.
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
Acceptance:       replay the real 401 segment through W-D3 ⇒ Overview banner
                  degrades + incident row + notification file within one refresh
                  cycle; pull a source file ⇒ affected widgets go UNKNOWN (never
                  stale-green); independent fresh-context adversarial audit of
                  the whole plan (mandatory — it has caught defects every time).
Rollback:         n/a.
Exit evidence:    audit writeup in docs/plan_audits/; SESSION_LOG entry.

## 4. Backtest result-artifact contract (fixed now so the future engine targets it)

`work/backtest/<run_id>/result.json`: {run_id, git_sha, params, date_range,
markets[], equity_curve[[ts,pnl_pessimistic,pnl_optimistic]], fills[{ts,market,
side,px_e4,qty,queue_model}], summary{n_fills, pnl_pessimistic, pnl_optimistic,
max_drawdown, fees_paid}}. Prices E4 fixed-point (D5); **pessimistic bound is
the headline number everywhere; optimistic is diagnosis-only** (Q2). The
Backtest tab renders any conforming file; the engine (STEP 6+) writes them.

## 5. Self-audit against GUARDRAILS §6

1. Advances 1→1.5 readiness, skips no gate — yes. 2. Live orders — none, S5
hardened with a pinned regression test (W-D6). 3–4. No strategy math here;
where PnL appears the pessimistic bound is contractual (§4). 5. No trading
data path touched. 6. Every W ships tests. 7. Pipeline: collectors are
supervised readers; kill -9 test proves non-interference (W-D2); supervisor
edits are append-only. 8. All rollbacks are file deletions or reverts.
9. RUNBOOK gains an observatory section at W-D6 (E5). 10. D2 is a named
acceptance criterion on every W with UNKNOWN states specified.
