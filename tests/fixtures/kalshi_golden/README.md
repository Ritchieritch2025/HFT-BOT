# kalshi_golden — real captured Kalshi WS frames (W3.3 / V13)

Frames are REAL and VERBATIM: each line is an unmodified RawRecord capture
line (envelope: `recv_wall_ns`/`recv_mono_ns`/`channel`/`sid`/`stream_epoch`
+ `raw` holding the original Kalshi WS frame byte-for-byte as received).
Consumed by `tests/test_kalshi_golden.py`, which pins field semantics
(per-sid seq scoping, trade_id dedupe key, fixed-point strings, taker_side,
ts encodings) against these frames. Never edit these files — replace them
by re-sampling if the wire format ever changes (that is a V12 spec-drift
event first).

## Source + sampling window (provenance)

| file | frames | source | window (UTC, from recv_wall_ns) |
|---|---|---|---|
| `orderbook_snapshot.ndjson` | 1 | `work/live_capture.ndjson` (2026-07-06 watchlist capture) | 2026-07-06 03:14:05 (first snapshot, sid 1 seq 1) |
| `orderbook_deltas.ndjson` | 50 | `work/live_capture.ndjson` (2026-07-06 watchlist capture) | 2026-07-06 03:14:05 → 03:43:32, every ~2065th gated delta of 103,247 (capture order preserved; 3 markets, single sid=1, stream_epoch=1) |
| `trades.ndjson` | 50 | `work/raw/date=2026-07-06/firehose_12.ndjson` (24/7 firehose, file covers 12:00:01–12:13:25 UTC) | 2026-07-06 12:00:01 → 12:01:15, every 100th gated trade from the first 5,000 (capture order preserved; 36 markets, sid=2) |

Why two sources: the 24/7 firehose subscribes `ticker`+`trade` only —
verified empirically (200k-line sample of `firehose_12.ndjson`: 176,647
ticker / 23,351 trade / 2 subscribed / 0 orderbook frames). Snapshot/delta
frames therefore come from the watchlist capture `work/live_capture.ndjson`
(same RawRecord format, `orderbook_delta` channel subscribed). Trades come
from the raw firehose per the W3.3 spec.

Sampling was gated, not calendar-filtered (plan §W3.3: validation is the
gate): every candidate line had to parse as JSON, carry all required fields,
pass `tools/gold_load.py` byte-exact E4 round-trip on every fixed-point
string, have prices inside (0,1) dollars, positive counts / nonzero deltas,
UUID-shaped distinct trade_ids, taker_side ∈ {yes,no}, and ts/ts_ms
agreement ≤1 s. Gate-skip counts in the sampled regions: 0 (both sources).
(The known splice-corrupted 08:18–08:35 UTC window remains a documented
audit fact; the trade source file covers 12:00–12:13 UTC and no date-based
exclusion was applied — the gates did all the filtering.)

## Observed REAL semantics these frames pin

- `seq` is per-sid, shared across ALL markets on the sid (I1), strictly
  increasing; snapshots consume the same counter (in-stream `get_snapshot`
  observed stamping the next sid seq, e.g. seq 2025 mid-delta-stream).
- Trade frames DO carry `sid`+`seq` (protocol doc I9 claims they don't —
  doc drift, filed in docs/BACKLOG.md).
- All prices/quantities are STRING fixed-point decimals
  (`"0.4100"`, `"142.00"`); byte-exact E4 round-trip holds on all 50+50+1.
- `ts` has two encodings by channel: trade `ts` = epoch-seconds int;
  orderbook_delta `ts` = ISO-8601 Zulu string with VARIABLE-length
  fractional seconds (trailing zeros trimmed: ".791454Z" but also
  ".52251Z" — Python's `fromisoformat` chokes on it; parsers must accept
  1–9 fraction digits). orderbook_snapshot msg has NO ts/ts_ms at all.
- Trade msgs carry `taker_outcome_side` and `taker_book_side` alongside the
  deprecated-but-present `taker_side`.

Doctored red-proof fixtures derived from these frames live in
`tests/fixtures/gold_defects/golden_*.ndjson` (duplicate trade_id, per-sid
seq regression, float-typed price) — those are tampered on purpose and are
NOT verbatim captures.
