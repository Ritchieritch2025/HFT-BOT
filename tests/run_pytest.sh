#!/usr/bin/env bash
# WP-00: pytest entry point for the contract suites. An empty scaffold is a
# valid green state: pytest exits 5 when zero tests are collected, which this
# wrapper maps to success so `make test` is green before the first suite
# lands (EXECUTION_PLAN WP-00 DoD: "0 tests, exit 0"). Any other nonzero
# exit (real failures, collection errors) passes through unchanged.
cd "$(dirname "$0")/.."
python3 -m pytest "$@"
rc=$?
if [ "$rc" -eq 5 ]; then
  echo "pytest: no tests collected yet (empty scaffold) — OK"
  exit 0
fi
exit "$rc"
