# Maker-edge pilot — RESUME (for a zero-history new session)

Paused 2026-07-10 by operator (halt the Plotly/ECharts download step). This
file hands the pilot to a fresh session with no chat memory. Read
`work/research/maker_edge_pilot/PRE_REGISTRATION.md` and
`docs/PLAN_RESEARCH_CYCLE_1.md` S1 (incl. the 2026-07-10 operator ruling
widening scope to ALL tennis match series, stratified) FIRST — the primary
metric is pre-registered and MUST NOT be re-defined.

## What this pilot is
S1 of PLAN_RESEARCH_CYCLE_1: a full maker-edge pilot on tennis pre-match,
2026-07-06..08 (Wimbledon slam-week, Mac-era → dev-grade). Hypothesis:
maker edge = spread captured − adverse-selection markout − fees. Delivered as
a PARAMETERIZED pipeline (change --category/--date = S4/S5, zero code fork),
registered in tools.json. Conclusions limited to {methodology-valid+collect,
methodology-flawed} — trade/reject belong to S5 (EC2-era recv data).

## DONE (committed code + on-disk parquet checkpoints)
Committed to git:
- `tools/research/build_segments.py` — builds dim_segments.parquet. RAN OK:
  2904 tennis markets; tour_level ITF 1883 / Challenger 526 / ATP 332 /
  WTA 156 / _unsegmented 7 (0.24% < 2% ✓); tick_stratum all 1c;
  maker_fee_class zero 2864 / charged 40; 0 dup, 0 unknown-fee. SEGMENTS OK.
- `tools/research/maker_edge_pilot.py` — pipeline driver, stages slice /
  markout / aggregate (aggregate delegates to aggregate_maker_edge.py).
- `tools/research/aggregate_maker_edge.py` — per-layer primary metric +
  per-match block bootstrap + causal phase detector + DQ + exploratory
  buckets + results.json; it calls a `html_report_maker_edge` module that
  DOES NOT EXIST YET (the remaining work — see below). WRITTEN, NOT YET RUN.
- `docs/vendor/js/echarts.min.js` (1.0MB, Apache-2.0, md5
  ef12c5c63df2acdf59f8a86cf0317711) + `docs/vendor/js/VERSION`. Vendored,
  no CDN. **RESOLVED — operator ruling 2026-07-10: vendored interactive lib
  = ECharts (the file already in repo); do NOT switch to Plotly.**
- PLAN_RESEARCH_CYCLE_1.md S1 ruling (commit before this pause).

On-disk parquet checkpoints (under work/, gitignored, rebuildable — DO NOT
re-run slice/markout unless the window changes; they took the longest):
- `work/research/dim_segments.parquet` (61 KB) — the segmentation dim table.
- `work/research/maker_edge_pilot/book.parquet` (34 MB) — L1 tennis slice,
  LOCF mid + spread, valid two-sided only. **4,413,144 rows.**
- `work/research/maker_edge_pilot/trades.parquet` (14 MB) — tennis trades,
  sign = +1 (taker yes) / −1 (taker no). **2,041,592 rows.**
- `work/research/maker_edge_pilot/markout.parquet` (38 MB) — per-trade
  scored: bounce, drift_{1,10,30,120}, markout_{h}, markout_chk_{h},
  half_spread_c, fill_class, dq_class, event_id. **2,041,592 rows scored.**
- `work/research/maker_edge_pilot/PRE_REGISTRATION.md` — frozen 14:20:55Z,
  SHA 3575c41, BEFORE any metric computation (also committed to git).

### Verified-good intermediate results (from the markout parquet)
- **IDENTITY SELF-CHECK PASSES EXACTLY**: |bounce+drift − markout_total| =
  0.00 and |markout − independent recompute| = 0.00 at ALL horizons
  (discipline #11 machine gate — the algorithm is correct).
- DQ classes: ok 2,013,410 · stale_book_gt60s 27,884 · no_book_before 298
  (all counted, none silent).
- fill_class: optimistic 1,532,089 · **pessimistic 391,749** (>> 200, so the
  primary metric will be interpretable) · inside_or_other 89,572.
- markout means (pre-match-agnostic sanity, pessimistic, cents): bounce 4.08,
  drift 2.49→2.92 (1s→120s), markout_total 6.58→7.00 — drift (adverse
  selection) grows with horizon, as expected.

## REMAINING WORK (the new session's job)
1. ~~**Vendored interactive lib**~~ — RESOLVED: operator ruled 2026-07-10
   ECharts (the vendored copy in docs/vendor/js/); no Plotly swap.
2. **`tools/research/html_report_maker_edge.py`** — the ONLY missing code.
   Must render the S1 SEVEN charts as INTERACTIVE ECharts (hover value+n,
   dataZoom, legend toggle, filter by tour/price-band/tick/phase), honoring
   the honest-chart FIVE rules (one-chart-one-question title; bootstrap CI
   bands not just means; zero line on markout/edge charts + no truncated
   y-axis; NO dual y-axis; log horizon axis, ¢ units). Self-contained HTML,
   local lib (inline or ../../../docs/vendor/js path), offline-openable.
   The seven charts (data already assembled in results.json by aggregate):
     1 toxicity curve (markout vs horizon by price band, CI bands, log x)
     2 bounce/drift decomposition stacked bars per horizon
     3 pre-match spread histogram (by tour_level, since tick is all 1c)
     4 primary-metric waterfall PER LAYER (half-spread → −markout → −fee →
       net edge, with CI) — the verdict chart
     5 time-of-day heatmap (hour × weekday, volume/spread)
     6 per-match scatter (half-spread vs drift, one point per match)
     7 DQ two panels (drop-class bars + mid-staleness histogram)
   Header of the HTML MUST carry: the PRE-REGISTRATION section (before
   results), NON-GATE/verified=false fee banner, regime=slam-week +
   dev-grade labels, and the fingerprint (code SHA + parquet manifest md5 +
   command line — aggregate already puts these in results["fingerprint"]).
3. **Run the aggregate stage** end-to-end (it will fail today only because
   html_report_maker_edge is missing) and read out results.json:
   per-layer primary metric (maker_fee_class × tour_level), n<200 → collect,
   H1 fee-wall valid only in the zero layer. NO cross-layer merge.
4. **Register in tools.json**: build_segments + maker_edge_pilot (safety
   offline/pure; the whole pipeline is read-only over the warehouse). Wire
   into run_pipeline only if a fast synthetic smoke exists; otherwise
   autorun:false research tool.
5. **Machine gate + five-eye checklist** (S1 verification): make check +
   run_pipeline green; identity self-check printed (already 0.00); reject
   counters in report; pessimistic n printed vs 200; fingerprint header.
   Operator five-eye: ① pre-registration section before results ② n beside
   every number ③ conclusion ∈ allowed set ④ regime=slam-week label
   ⑤ fingerprint header.
6. **Exit ritual + independent audit** (the audit MUST check: primary metric
   was pre-registered before computation; identity self-check present;
   per-layer no-merge; fee preview never touched a facts gate; every number
   has n; conclusion in the allowed set).

## RESUME COMMANDS (verbatim)
```
cd "/Users/ritcardo/HFT BOT"
# checkpoints already on disk — DO NOT re-run slice/markout unless the window changes.
# after writing tools/research/html_report_maker_edge.py:
python3 tools/research/maker_edge_pilot.py --stage aggregate          # reads the parquets, writes results.json + index.html
# to rebuild a checkpoint (only if needed):
python3 tools/research/build_segments.py                              # dim_segments
python3 tools/research/maker_edge_pilot.py --stage slice              # book + trades parquet (slow)
python3 tools/research/maker_edge_pilot.py --stage markout            # markout parquet
# full run (S4/S5 reparam example):
python3 tools/research/maker_edge_pilot.py --category Sports --subcategory Tennis --start 2026-07-06 --end 2026-07-08 --train-end 2026-07-07
```

## Discipline reminders the new session MUST NOT break
- Primary metric already pre-registered (PRE_REGISTRATION.md) — do not
  redefine; compute it, don't invent a new one.
- Per LAYER only (maker_fee_class × tour_level); NEVER a single cross-layer
  total (operator ruling 2026-07-10).
- Fees are a NON-GATE preview; never touch a facts-gated tool's gate (OQ-1).
- n<200 in a layer ⇒ that layer's conclusion is auto "collect".
- Conclusions ∈ {methodology-valid+collect, methodology-flawed} only.
- Mac-era ⇒ dev-grade, regime=slam-week; no extrapolation.
