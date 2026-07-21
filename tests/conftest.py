# WP-00 scaffold: legacy suites are canonical under `make check` /
# tests/run_pipeline.sh (self-runner style, registered in tools.json with
# pass tokens). They stay out of pytest collection until WP-04 migrates each
# one deliberately — remove a file from this list in the same commit that
# migrates it (A1: one test infrastructure, no double-running).
collect_ignore = [
    "test_console.py",
    "test_feed_readiness.py",
    "test_verify_ws_capture.py",
    "test_verify_feed_metrics.py",
    "test_ingest.py",
    "test_export_day.py",
    "test_warehouse_status.py",
]
