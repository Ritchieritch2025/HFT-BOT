#!/bin/bash
# Runs test_request_executor against the local mock_rest.py (no network).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${1:-18112}
python3 tests/mock_rest.py "$PORT" >/tmp/mock_rest_exec.$PORT.log 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
sleep 0.7
./build/test_request_executor "http://127.0.0.1:$PORT"
