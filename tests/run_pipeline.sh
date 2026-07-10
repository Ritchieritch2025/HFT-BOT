#!/usr/bin/env bash
# Canonical "run everything offline" entry point (PLAN_PROD_V1 P0). Runs the full
# offline suite — gates, pure/offline unit tests, mock-backed integration — and
# appends one machine-readable NDJSON record per suite to work/test_results.ndjson
# (the ops-console data source). Exits nonzero if any suite fails.
#
# No real network: every suite is pure, or talks only to a localhost Python mock.
# Port base from PIPELINE_PORT_BASE (default 18300) to avoid collisions.
#
# Usage: tests/run_pipeline.sh
set -u
cd "$(dirname "$0")/.."

PORT_BASE="${PIPELINE_PORT_BASE:-18300}"
RESULTS="work/test_results.ndjson"
LOGDIR="work/logs"
SCRATCH="build/scratch"
mkdir -p "$LOGDIR" "$SCRATCH"
: > "$RESULTS"   # fresh run

OVERALL=0
now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }

# record_suite <name> <status> <passed> <failed> <dur_ms> <log>
record_suite() {
  printf '{"type":"test_suite","ts_ms":%s,"suite":"%s","status":"%s","passed":%s,"failed":%s,"duration_ms":%s,"log":"%s"}\n' \
    "$(now_ms)" "$1" "$2" "$3" "$4" "$5" "$6" >> "$RESULTS"
}

# run_suite <name> <cmd...> : run, capture log, time, parse PASS/FAIL, record.
run_suite() {
  local name="$1"; shift
  local log="$LOGDIR/$name.txt"
  local start end dur rc passed failed status
  start="$(now_ms)"
  "$@" > "$log" 2>&1; rc=$?
  end="$(now_ms)"; dur=$((end - start))
  passed="$(grep -c '^PASS:' "$log" 2>/dev/null || true)"; passed="${passed:-0}"
  failed="$(grep -c '^FAIL:' "$log" 2>/dev/null || true)"; failed="${failed:-0}"
  if [ "$rc" -eq 0 ]; then status=pass; else status=fail; OVERALL=1; fi
  record_suite "$name" "$status" "$passed" "$failed" "$dur" "$log"
  printf '  %-26s %-4s  %5sms  (+%s/-%s)\n' "$name" "$status" "$dur" "$passed" "$failed"
}

# --- mock lifecycle -------------------------------------------------------------
MOCK_PID=""
start_mock() {  # start_mock <script> <port> [extra args...]
  python3 "tests/$1" "${@:2}" > "$LOGDIR/mock_$1.$2.log" 2>&1 &
  MOCK_PID=$!
  sleep 0.7
}
stop_mock() {
  if [ -n "$MOCK_PID" ]; then
    kill "$MOCK_PID" 2>/dev/null
    wait "$MOCK_PID" 2>/dev/null || true  # absorb the job-control "Terminated" notice
    MOCK_PID=""
  fi
}

# run_suite_with_mock <name> <mock_script> <port> [-- <mock extra args>] <cmd...>
run_suite_with_mock() {
  local name="$1" mock="$2" port="$3"; shift 3
  local extra=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift  # drop --
  # ${arr[@]+"${arr[@]}"} expands to nothing when empty (safe under set -u / bash 3.2).
  start_mock "$mock" "$port" ${extra[@]+"${extra[@]}"}
  run_suite "$name" "$@"
  stop_mock
}

echo "== run_pipeline (port base $PORT_BASE) =="

# 0. tool registry, existence-checked (needs a full `make` beforehand)
run_suite "check_registry" python3 tools/check_registry.py --require-built

# 1. gates + pure/offline unit tests (make check runs them all through one target)
run_suite "make_check" make check

# 2. signing + ops-console backend (Python)
run_suite "test_signing" ./build/test_signing
run_suite "test_strategies" ./build/test_strategies
run_suite "test_market_filter" ./build/test_market_filter
run_suite "test_console" python3 tests/test_console.py
run_suite "test_feed_readiness" python3 tests/test_feed_readiness.py
run_suite "test_verify_ws_capture" python3 tests/test_verify_ws_capture.py
run_suite "test_capture_gaps" ./tests/run_pytest.sh tests/test_capture_gaps.py
run_suite "test_verify_feed_metrics" python3 tests/test_verify_feed_metrics.py
run_suite "test_gold_layout" ./build/test_gold_layout
run_suite "test_gold_dtype" ./tests/run_pytest.sh tests/test_gold_dtype.py
run_suite "test_gold_load" ./tests/run_pytest.sh tests/test_gold_load.py
run_suite "test_gold_fsm" ./tests/run_pytest.sh tests/test_gold_fsm.py
run_suite "test_gold_merge" ./tests/run_pytest.sh tests/test_gold_merge.py
run_suite "test_gold_io" ./tests/run_pytest.sh tests/test_gold_io.py
run_suite "test_gold_validate" ./tests/run_pytest.sh tests/test_gold_validate.py
run_suite "test_kalshi_golden" ./tests/run_pytest.sh tests/test_kalshi_golden.py
run_suite "test_gold_v5_delta" ./tests/run_pytest.sh tests/test_gold_v5_delta.py
run_suite "test_gold_v7_race" ./tests/run_pytest.sh tests/test_gold_v7_race.py
run_suite "test_coverage_audit" ./tests/run_pytest.sh tests/test_coverage_audit.py
run_suite "test_event_measure_split" ./tests/run_pytest.sh tests/test_event_measure_split.py
run_suite "test_event_index" ./tests/run_pytest.sh tests/test_event_index.py
run_suite "test_event_pack" ./tests/run_pytest.sh tests/test_event_pack.py
run_suite "test_freshness" ./tests/run_pytest.sh tests/test_freshness.py
run_suite "test_daily_check" ./tests/run_pytest.sh tests/test_daily_check.py
run_suite "test_pipeline_contract" ./tests/run_pytest.sh tests/test_pipeline_contract.py
run_suite "test_alert_notify" ./tests/run_pytest.sh tests/test_alert_notify.py
run_suite "test_catalog_sync_pacing" ./tests/run_pytest.sh tests/test_catalog_sync_pacing.py
run_suite "test_dim_snapshot_schema_drift" ./tests/run_pytest.sh tests/test_dim_snapshot_schema_drift.py
run_suite "test_research_metrics" ./tests/run_pytest.sh tests/test_research_metrics.py
run_suite "test_gate_metrics" ./tests/run_pytest.sh tests/test_gate_metrics.py
run_suite "test_ingest" python3 tests/test_ingest.py
run_suite "test_export_day" python3 tests/test_export_day.py
run_suite "test_timestamp_ladder" ./tests/run_pytest.sh tests/test_timestamp_ladder.py
run_suite "test_backtest_clock" ./tests/run_pytest.sh tests/test_backtest_clock.py
run_suite "test_jitter_report" ./tests/run_pytest.sh tests/test_jitter_report.py
run_suite "test_pricing_lo" ./tests/run_pytest.sh tests/test_pricing_lo.py
run_suite "test_pricing_fair" ./tests/run_pytest.sh tests/test_pricing_fair.py
run_suite "test_pricing_quote" ./tests/run_pytest.sh tests/test_pricing_quote.py
run_suite "test_pricing_pipeline" ./tests/run_pytest.sh tests/test_pricing_pipeline.py
run_suite "test_account_view" ./tests/run_pytest.sh tests/test_account_view.py
run_suite "test_panic_dryrun" ./tests/run_pytest.sh tests/test_panic_dryrun.py
run_suite "test_risk_ledger" ./build/test_risk_ledger "$SCRATCH"
run_suite "test_rule_engine" ./build/test_rule_engine "$SCRATCH"
run_suite "test_reconcile" ./tests/run_pytest.sh tests/test_reconcile.py
run_suite "test_warehouse_status" python3 tests/test_warehouse_status.py
run_suite "test_warehouse_nonuniform_archive" ./tests/run_pytest.sh tests/test_warehouse_nonuniform_archive.py
run_suite "test_warehouse_event" ./tests/run_pytest.sh tests/test_warehouse_event.py
run_suite "test_event_validate" ./tests/run_pytest.sh tests/test_event_validate.py
run_suite "test_event_export" ./tests/run_pytest.sh tests/test_event_export.py

# 3. RESP client against mini_redis
run_suite_with_mock "test_resp" mini_redis.py "$((PORT_BASE+1))" -- \
  ./build/test_resp "$((PORT_BASE+1))"

# 4. self-contained shell suites (each starts + stops its own mock)
run_suite "rest_api"          bash tests/run_rest_api.sh "$((PORT_BASE+10))"
run_suite "request_executor"  bash tests/run_request_executor.sh "$((PORT_BASE+11))"
run_suite "ws_smoke"          bash tests/run_ws_smoke.sh
run_suite "ws_shadow_mock"    bash tests/run_ws_shadow_mock.sh

# 5. simdjson-linked offline unit tests (scratch dir keeps the root clean)
run_suite "test_storage"   ./build/test_storage   "$SCRATCH"
run_suite "test_decode"    ./build/test_decode
run_suite "test_replay"    ./build/test_replay    "$SCRATCH"
run_suite "test_recorder"  ./build/test_recorder  "$SCRATCH"
run_suite "test_ws_client" ./build/test_ws_client
run_suite "test_shadow"    ./build/test_shadow    "$SCRATCH"

# 6. threaded REST integration against mock_server.py (3 threads x 50 reqs)
KEY="$SCRATCH/itest_key.pem"
[ -f "$KEY" ] || openssl genrsa -out "$KEY" 2048 >/dev/null 2>&1
run_suite_with_mock "test_integration" mock_server.py "$((PORT_BASE+2))" \
  "$SCRATCH/mock_capture.jsonl" -- \
  ./build/test_integration "http://127.0.0.1:$((PORT_BASE+2))" "$KEY" 3 50

echo "== $( [ "$OVERALL" -eq 0 ] && echo 'PIPELINE PASS' || echo 'PIPELINE FAIL' ) =="
exit "$OVERALL"
