# Current State - 2026-07-05

This file is the short truth map for the repo. It is intentionally separate
from the longer phase plans.

## What Is Real Today

- `tradingd` is the main in-process trading engine.
- The order-lane path in `apps/tradingd.cpp` can transmit only when runtime
  safety allows live orders.
- `src/strategies.cpp` currently returns an empty strategy roster, so normal
  `tradingd` startup does not generate orders.
- The WebSocket market-data engine exists and is shadow-tested, but it is not
  wired into `tradingd` as the live feed yet.
- The default runtime is fail-closed: local mock plus data collection.
- The dashboard is an observer/operator console. It tails engine telemetry,
  reads `work/lifecycle_status.json`, and streams public Kalshi update events;
  it is not part of the trading path.
- `tools/lifecycle_check.py` is the readiness source of truth. It skips
  network checks unless `--allow-network` is explicit and never runs
  `live_order` tools.
- Official Kalshi docs snapshots are stored under `docs/vendor/kalshi/latest/`
  with a reviewed baseline under `docs/vendor/kalshi/baseline/`.

## What Is Not A Live Signal

- Old `work/metrics.ndjson` rows may be synthetic or stale.
- Feed, strategy, and risk panels are only meaningful after real telemetry is
  emitted by the engine.
- `work/test_results_latest.json` is test/tool history, not live trading state.
- `work/lifecycle_status.json` is readiness evidence, not an engine dependency.
- Mock servers under `tests/` are offline harnesses only.

## Execution Paths

1. `tradingd` lane path:
   feed -> strategies -> `ExecPayload` -> ring -> submit lanes -> Kalshi.
   This is the only current real order path.

2. `KalshiExecutionEngine` bus path:
   `DataCollect` rejects, `Shadow` logs would-be orders, `Live` currently
   throws fail-closed. This path is not the live transmitter yet.

## Dashboard Rules

- Live tab proves only what fresh non-synthetic NDJSON proves.
- Tests tab should show core pure/offline tests only.
- Tools tab is where probes, benches, wrappers, and daemons belong.
- `live_order` tools must never be runnable from the dashboard.

## Open Production Work

The repo is not production-live complete until these land in order:

1. P6: wire WebSocket feed into `tradingd`.
2. P7: server-derived token bucket, risk gate, and kill switch.
3. P8: unified live order submitter plus reconcile-on-ambiguity.
4. P9: strategy framework, shadow PnL, and replay backtest.
5. P10: small prod canary (the demo exchange is no longer supported; prod places REAL orders).

## Current Cleanup Policy

- Archive generated NDJSON rather than leaving it in the repo root.
- Keep `work/metrics.ndjson` empty unless a real process is writing it.
- Do not treat dashboard samples as proof of a running engine.
- Do not enable new live transmission until P7/P8 safety pieces exist.
