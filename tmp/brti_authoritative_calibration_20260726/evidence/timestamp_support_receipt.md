# BRTI timestamp-only support diagnostic

Status: **TIMESTAMP_SUPPORT_MAPPED**.

No BRTI value was deserialized, accessed, converted, derived, or written. Only integer `time` fields and support booleans were used.

- Projected one-second timestamps: `259051`
- Missing one-second timestamps: `149`
- Markets: `288`
- All-horizon supported markets: `283`
- Markets with one or more unsupported horizons: `5`

## Checkpoints

- 120s: supported `284`, unsupported `4`.
- 180s: supported `285`, unsupported `3`.
- 240s: supported `285`, unsupported `3`.

Support manifest SHA-256: `2d104cc1715348a2cdea18af176792da970f0ba6e6fe16172085422294bb9d41`.

This receipt does not run or authorize model calibration. Any later analyzer must bind this manifest hash and exclude every unsupported market-checkpoint without interpolation.
