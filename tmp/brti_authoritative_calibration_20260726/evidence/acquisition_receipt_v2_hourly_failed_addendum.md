# V2 hourly acquisition receipt addendum

This addendum is rendered only from the frozen JSON receipt
`acquisition_receipt_v2_hourly_failed.json` (SHA-256
`c64d549c4fbbee32b6094c7eb0a5419b928cfc677e8fde481c20cf8fd78de551`).
It does not alter the sealed downloader, its rules, or its result.

- Status: `NO_DECISION_HOURLY_ACQUISITION_FAILED`
- Logical hours attempted: `72`
- HTTP results: `72 × 200`
- Retry count: `0`
- Maximum observed concurrency: `2`
- Elapsed time: `42997.923 ms`
- Validation results: `72 × FAILED_VALIDATION`
- Raw experiment envelopes persisted: `0`
- Common cause: the history endpoint returned approximately five values per
  second (usually `18000` rows/hour), while V2 required exactly `3600`.

After the JSON receipt had already been written, the optional Markdown
renderer raised `NameError: Counter is not defined`. This is a presentation
layer defect only. It happened after all requests and after the authoritative
JSON receipt was durably written; it did not change request execution,
validation, persistence (`0` files), or the fail-closed decision.

Official CF documentation resolves the cadence distinction:

- Historical values accepts only `id`, `timespan`, and `timestamp`; it has no
  `maxResolution` query parameter.
- The Value Channel defines `PER_SECOND` as values at second boundaries and
  `PER_200MS` as all ticks including those between second boundaries.

Therefore a later preregistration may project historical data by the timestamp
predicate `time_ms % 1000 == 0`. It must not select every fifth row.
