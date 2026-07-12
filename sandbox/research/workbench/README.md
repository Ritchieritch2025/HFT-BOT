# Research Workbench

Local, read-only entry point for turning frozen sports-market hypotheses and
experiment artifacts into inspectable data. It has no production integration,
credentials, order path, network client, shell execution, or mutation API.

## Event Intelligence Dashboard (primary surface)

The decision-grade rebuild of the sports visualization lives in
`event_intel.py` + `intel.html`. It answers, on one synchronized timeline per
match: when large activity entered, whether it was a real trade / displayed
liquidity / cancellation-or-depletion / L1 proxy / L2 level update, what price,
spread and depth did around it, which regime the market was in, and whether the
observation is descriptive, causal-replayable, or unavailable.

```bash
# build per-episode artifacts + offline dashboard (reads the local archive)
python3 sandbox/research/workbench/event_intel.py build
# open directly (works over file:// — artifacts load as JSONP script tags)
open sandbox/research/reports/event_intel/index.html
# or serve alongside the legacy workbench
python3 sandbox/research/workbench/app.py serve   # -> http://127.0.0.1:8791/intel
```

Deep links: `index.html#sport=Baseball&ep=26JUL05_BOSLAA&mode=causal&thr=p99.5`.

Layout: left sport/episode/market selector (episode identity = catalog titles
+ deterministic `{YYMONDD}:{TEAMS}` ticker-suffix key spanning ALL series of
one match — never market_ticker alone); center six linked ECharts tracks
(price with intraminute min/max band; signed executed flow with log-scaled
large-print bubbles; displayed liquidity — real L2 depth when captured, else
touch quantity labeled AGGREGATE L1 PROXY; tempo with explainable
CALM/BUILDING/BURST/DISLOCATION/RECOVERY bands; RFQ; score/game state — the
last two render honest empty states locally); right exact activity tape synced
to the visible zoom (row click jumps the crosshair); bottom drawer with
per-metric ECDF/histogram distributions (n, p50, p99, max, unit, provenance),
channel inventory and definitions.

Honesty invariants (enforced by `test_event_intel.py`): duplicate trade IDs
collapse; conflicting trade bodies are excluded and counted; every marker is a
`LARGE-ACTIVITY CANDIDATE — NO ACTOR IDENTITY`; descriptive percentiles carry
a permanent LOOK-AHEAD banner; causal replay uses hourly-frozen strictly-prior
cohorts and refuses p99/p99.5/p99.9 labels below n=500/1,000/5,000; >65 min of
L1 silence is shaded `DATA GAP / BOOK STATE UNTRUSTED` (hourly heartbeats make
that a capture-loss proof); pre-TL1 archive rows are labeled
`PRE-TL1 / EXCHANGE-OR-COARSE TIME ONLY`; score markers exist only when a real
payload is present via the `event-intel-score-input-v1` adapter contract
(POST-HOC backfill must be labeled); RFQ renders through
`event-intel-rfq-input-v1` (locally unavailable — EC2-only channel).

## Start

From the repository root:

```bash
python3 sandbox/research/workbench/app.py build
python3 sandbox/research/workbench/app.py serve
```

`run` combines both commands:

```bash
python3 sandbox/research/workbench/app.py run --host 127.0.0.1 --port 8791
```

The server rejects non-loopback hosts and exposes only:

- `GET /` and `GET /index.html`
- `GET /api/overview`
- `GET /api/liquidity`
- `GET /api/main-events`
- `GET /api/hypotheses`
- `GET /api/experiments`
- `GET /assets/echarts.min.js` (repository-vendored ECharts; no CDN)

POST, PUT, PATCH, and DELETE return `405`. `build` reads
`work/warehouse/manifest.csv`, archived local Sports L1/trade facts, the
hypothesis registry, and JSON experiment artifacts. Its only output is five
generated snapshots under
`sandbox/research/reports/workbench/`.

`build` also creates `sandbox/research/reports/workbench/index.html`, a
single-file offline copy with the exact snapshot and vendored ECharts embedded.
It can be opened directly without starting the HTTP server; the loopback server
remains useful while iterating on code.

## Liquidity Atlas

The first dashboard lens compares liquidity across absolute UTC clock hours.
It deliberately uses the latest `is_snapshot=true` hourly heartbeat for each
market-hour, then plots the distribution across market-hours. This avoids the
classic error of letting fast-changing markets receive extra statistical
weight simply because they emit more change rows.

Available lenses are quoted spread, conservative two-sided top-of-book depth,
L1 state-change count, deduplicated trade count, and deduplicated contract
volume. Each continuous result is displayed with an ECDF plus p50/p99/max/n;
active-market counts and two-sided shares are distributed across archive days.
The heatmap remains absolute UTC time because historical markets do not yet
join to a verified `occurrence_datetime`.

The current local snapshot is explicitly `EXPLORATORY · UNSEALED /
GAP-DEGRADED`: it uses dated 2026-07-06..08 L1 and trade archives. It does not
claim full-book depth, time-to-kickoff, in-play status, live L2, or RFQ coverage.

## Main Events · Whale Tape

The second lens groups contracts into a match-level `episode_key`, ranks the
most actively traded episodes within Soccer, Baseball, Basketball, and Tennis,
and overlays three evidence types on a one-minute L1 mid-price path:

- real deduplicated trade prints, ranked by contract size inside that market;
- same-price best-bid displayed-size increases; and
- same-price best-ask displayed-size increases.

“Whale” is only a visual shorthand for unusually large activity. The archive
does not contain account identity or individual order IDs: a trade ID identifies
a transaction, while an L1 quantity increase is aggregate displayed liquidity
that can combine several orders or disappear without trading. Every marker
therefore carries a `NO_ACTOR_IDENTITY` boundary, and L1 candidates additionally
carry `AGGREGATE_TOUCH_PROXY`.

The tape provides p99/p99.5/p99.9 selectors, exact archived timestamps, size
tooltips, full ECDFs with p50/p99/max/n, and an exact-candidate table. These
percentiles are descriptive full-sample ranks with look-ahead; strategy
thresholds must later be frozen on TRAIN and applied forward.

No score or game-state payload exists in the dated local archive, so historical
markers cannot currently be attributed to a run, goal, point, set, period, or
clock state. The dashboard records the prospective integration contract:
Kalshi milestone mappings plus live-data/game-stats payloads, captured raw with
receive timestamps, per-sport schema validation, corrections, and gap
accounting. Historical backfill remains `NOT_VERIFIED` and is not presented as
decision-time evidence.

## Experiment artifact schema

Put each artifact at `sandbox/research/reports/experiments/<name>.json`:

```json
{
  "schema_version": "research-experiment-v1",
  "experiment_id": "tts-basketball-2026w28-v1",
  "hypothesis_id": "ATL-TTS-01",
  "title": "Basketball spread by time-to-start",
  "result_status": "NO_RESULTS",
  "generated_at_utc": "2026-07-11T15:00:00Z",
  "summary": "Definition frozen; measurement has not run.",
  "metrics": [],
  "charts": [],
  "tables": []
}
```

Required fields are `schema_version`, a non-empty `experiment_id`, a non-empty
`title`, a registry-backed `hypothesis_id`, and `result_status`. Allowed result
statuses are `NO_RESULTS`, `EXPLORATORY`, `INCONCLUSIVE`, `PASS`, and `FAIL`.
`metrics`, `charts`, and `tables` are optional but must each be a list. Missing
or unknown hypothesis IDs—and all other schema failures—remain visible as
`INVALID` entries with `validation_errors`; they are never silently accepted or
dropped.

Generic charts use this shape (multiple named series are allowed):

```json
{
  "title": "Median spread by time-to-start",
  "kind": "line",
  "x_label": "Minutes to start",
  "y_label": "Spread (cents)",
  "series": [
    {"name": "Basketball", "points": [{"x": 60, "y": 4.2}]}
  ]
}
```

`kind` is `line` or `bar`. Generic tables use
`{"title":"Coverage","columns":["date","rows"],"rows":[["2026-07-06",10]]}`.
The API passes these generic structures through; experiment producers remain
responsible for keeping labels, values, and row widths internally consistent.

The initial registry deliberately leaves every hypothesis at
`DRAFT_NEEDS_FREEZE`. A visual surface is not evidence: an experiment should
not move to PASS/FAIL until its population, outcome, analysis unit, minimum
effect, uncertainty method, and holdout assignment are frozen before results
are inspected.

Registry lifecycle statuses are `DRAFT_NEEDS_FREEZE`, `FROZEN_EXPLORATORY`,
`FROZEN_TRAIN`, and `RETIRED`. A draft or retired hypothesis may have a
`NO_RESULTS` artifact for planning and documentation, but any artifact marked
`EXPLORATORY`, `INCONCLUSIVE`, `PASS`, or `FAIL` is valid only while its linked
hypothesis status begins with `FROZEN_`. Otherwise the artifact remains visible
as `INVALID` with a validation error.
