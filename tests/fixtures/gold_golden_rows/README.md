# gold_golden_rows — real-row fixtures for W2.1 typed loaders

Real warehouse rows sampled 2026-07-06 via `tools/warehouse.py load()`
(read-only, staging DB), for the V1 byte-exact E4 round-trip and clean-load
tests in `tests/test_gold_load.py`.

**Sampling window (G5, R decision 2026-07-06):** all rows are from 2026-07-06
UTC **outside 08:18–08:36 UTC** (the corrupted source window from the splice
incident, padded from 08:35 to 08:36 for margin). Excluded epoch-µs range:
`[1783325880000000, 1783326960000000)`. Every fixture row's `ts_utc` is
asserted outside this range at generation time. This is a *sampling* choice
for golden (known-good) rows only — the loaders themselves are date-blind;
malformed input is detected by inspecting the record, never by calendar
(PLAN_GOLD_DATA_CONTRACT §2.2).

Files: `trades.json` (8 rows: sub-penny prices, fractional counts, both taker
sides, incl. one price < $0.01), `l1.json` (8 rows: snapshots + changes,
sub-penny, empty-bid `0.0000/0.00`, empty-ask `1.0000/0.00`, bid > $0.99),
`full.json` (1 snapshot + 5 deltas: yes/no side, positive/negative delta).

Format: `{"meta": {...}, "rows": [{"input": {...}, "expect_e4": {...}}]}`.
`input` carries price/qty fields as canonical dollar-decimal strings (price =
4 fraction digits, qty = 2; `docs/warehouse_schema.md` examples
`"0.0090"→90`, `"5119.00"→51190000`); `expect_e4` carries the warehouse's E4
integers for the same row, so tests assert string→int→string byte-exactness
in both directions with zero float parses.

Generator (scratch, one-shot): sampled with deterministic per-property
queries (`SELECT ... WHERE ts_utc NOT BETWEEN ...`), deduped on trade_id.
Regeneration is fine any time from any clean window; these are pinned data,
not code.
