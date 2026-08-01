# Authoritative BRTI V3 boundary-projection acquisition

Status: **NO_DECISION_BOUNDARY_ACQUISITION_FAILED**.

- Logical hours: `72`
- Successful hours: `63`
- Raw envelopes persisted: `72`
- Attempts: `72`
- Retried hours: `0`
- Maximum observed concurrency: `2`

Complete subsecond envelopes are hashed and retained. The one-second series is selected only by `time_ms % 1000 == 0`; no row-position sampling is used. Individual BRTI values are absent from this receipt.
