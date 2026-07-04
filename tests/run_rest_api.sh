#!/bin/bash
# Runs test_rest_api against the local mock_rest.py (no network, no real orders).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${1:-18110}
python3 tests/mock_rest.py "$PORT" >/tmp/mock_rest.$PORT.log 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
sleep 0.7
./build/test_rest_api "http://127.0.0.1:$PORT"
