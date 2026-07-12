# Research Workbench

Local, read-only entry point for turning frozen sports-market hypotheses and
experiment artifacts into inspectable data. It has no production integration,
credentials, order path, network client, shell execution, or mutation API.

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
- `GET /api/hypotheses`
- `GET /api/experiments`

POST, PUT, PATCH, and DELETE return `405`. `build` reads
`work/warehouse/manifest.csv`, the hypothesis registry, and JSON experiment
artifacts. Its only output is three generated snapshots under
`sandbox/research/reports/workbench/`.

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
