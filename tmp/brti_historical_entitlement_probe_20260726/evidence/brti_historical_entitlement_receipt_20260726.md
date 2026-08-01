# Authoritative historical BRTI availability probe

Result: **ENTITLED_HISTORICAL_AVAILABLE**

- HTTP status: `200`
- Elapsed: `209.995 ms`
- Payload rows: `0`
- Error/detail: `{"data":{"payload":{"omitted":true,"row_count":0},"serverTime":"2026-07-26T06:54:01.759Z"}}`
- Response body SHA-256: `0a952f7f619205c98572524d899030b36993f335f1f3a67d5192ef9aa0ffd98b`

The single request was:

```text
GET /trade-api/v2/cfbenchmarks/history/values
id=BRTI
timespan=HOUR
timestamp=2009-01-01T00:00:00.000Z
```

The hour predates BRTI and all project experiments. No 2026-07-20..23 value
was requested, persisted, printed, hashed, counted, or summarized. The recent
`/cfbenchmarks/values?id=BRTI` endpoint was not called.

Historical entitlement is confirmed. No experiment-period value was requested. A separate calibration-download preregistration must now be written and sealed before accessing 2026-07-20..22.

Official sources:

- https://docs.kalshi.com/cfbenchmarks/rest-passthrough.md
- https://docs.cfbenchmarks.com/api/rest/historical-values/
- https://docs.cfbenchmarks.com/api/rest/values/
