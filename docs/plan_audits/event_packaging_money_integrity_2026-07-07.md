# Event-Packaging money-integrity + correctness audit — 2026-07-07

Independent, fresh-context audit of the committed W-E0 (`d3791d5`, `a74ed93`) +
W-E1 (`37523e2`) and `docs/PLAN_EVENT_PACKAGING.md`, focused on money-integrity
(D5: E4 fixed-point stays, never floats on accounting paths; sub-penny is real)
plus correctness. This doc RECONSTRUCTS and supersedes the prior "AF-1..AF-4"
findings that were referenced in SESSION_LOG but never written to a file (an E2
gap — findings that block W-E3 must live in a file). Tests at audit time:
`pytest tests/test_event_measure_split.py tests/test_event_index.py` → 14 passed.

Ground truth: the trades table stores `yes_price_e4`, `no_price_e4`, `count_e4`
as E4 fixed-point ints (`tools/ingest.py`; raw `yes_price_dollars`/`count_fp`).
Per `docs/warehouse_schema.md` §Types, sub-penny prices and fractional sizes are
common — losing E4 loses real money data.

**Status: findings recorded; plan spec amended (AF-1/2/3/5) + W-E0 labels
(AF-4). AF-1 & AF-2 are BLOCKERs that gate W-E2/W-E7 (the CSV materializers).**

---

## AF-1 — BLOCKER — CONFIRMED — §3.6 Column rules, Trades line
The Trades CSV contract omits the E4-integer retention guarantee the L1 line
gets. §3.6 L1 says "E4 ints AND dollar-decimal strings"; the Trades line said
only "`trade_id`, prices, `count`, `taker_side`" — no E4-int guarantee. The
existing template W-E7 would copy — `tools/export_day.py` `STRATEGY_COLS["trades"]`
— emits `yes_price_e4/10000.0 AS yes_price`, `count_e4/10000.0 AS contracts` as
**floats and drops every `_e4` column**.
Failure: trade `yes_price_e4=90` ($0.0090), `count_e4=12345` (1.2345 contracts)
→ CSV `yes_price=0.009, contracts=1.2345` float-only, no integer column to
recover the exact value. D5 violation.
Fix: §3.6 Trades must retain `yes_price_e4`, `no_price_e4`, `count_e4` (E4 ints,
authoritative) + optional dollar-decimal for readability; W-E2/W-E7 acceptance
asserts the E4 integer columns present and byte-exact vs warehouse. Do NOT reuse
`STRATEGY_COLS` as-is.

## AF-2 — BLOCKER — CONFIRMED — §3.6 L1 + Trades dollar-decimal
"dollar-decimal strings" had no precision/arithmetic mandate → sub-penny
narrowing. The in-repo idiom is float division `yes_bid_e4/10000.0`. A naive
`f"{e4/10000:.2f}"` collapses `0.0090 → "0.01"`; even `/10000.0`+`str()` invites
float drift on an accounting path.
Fix: the dollar-decimal is derived by INTEGER arithmetic to exactly 4 dp
(`f"{e4//10000}.{abs(e4)%10000:04d}"` or `Decimal(e4)/10000`), never float, and
is a labeled readability duplicate of the always-present E4 integer column.
Applies to L1 and Trades.

## AF-3 — MAJOR — CONFIRMED — §3.6 Trades, §3.4 `pack_row_trades`, §6 V-EP10
Field-name drift: plan said `count`; the warehouse field is `count_e4` (contract
quantity, E4) — and "count" was overloaded with trade-row cardinality
(`pack_row_trades`, V-EP10 "trade count").
Fix: use `count_e4` for contract quantity everywhere; reserve `row_count`/
`n_trades` strictly for row cardinality. V-EP10 verifies row cardinality +
`trade_id` uniqueness, not summed quantity. Any volume stat sums `count_e4` as int.

## AF-4 — MAJOR — PLAUSIBLE — §3.6 `_event_summary.csv`, W-E0 output labels
Every "count"/"ticks" surface is trade-ROW cardinality; contract volume
(`sum(count_e4)`) is never computed — honest only if labeled, else a D2 "green
lie". `event_measure_split.py` reports `count(*)` as `total_ticks`; the plan's
`_event_summary "counts"` is unlabeled.
Fix: label every row-count field explicitly (`trade_rows`/`l1_rows` — "not
contract volume") in CSV headers, prints, and `_event_summary.csv`; if economic
volume is wanted add a separate integer `contracts_e4 = SUM(count_e4)`. No money
is corrupted today (W-E0 touches no price), but pin the labels before W-E7.

## AF-5 — MAJOR — PLAUSIBLE — §3.2 Step D/E + W-E2 acceptance (correctness)
The index window is a build-time snapshot; a pack built later from a stale index
row can CLIP real trades. The padding property (`win_start ≤ t_first`,
`win_end ≥ t_last`) holds only for `t_last` known at `build_index` time. If a
unit is `active`/`partial` at index build and more trades arrive after, a W-E2
pack over the stale `[win_start, win_end]` omits later trades (V-EP1 conservation
/ V-EP3 leakage fail, or silent drop).
Fix: §3.2/W-E2 mandate window RE-INFERENCE (index refresh) at pack time, and pack
only `sealed` units whose `t_last` is stable (no ticks within
`seal_after_close_us`). State it in W-E2 acceptance.

---

## Checked and CLEAN
- W-E1 introduces no money path — `event_index.py` carries only timestamps,
  tickers, status, market lists; no price/qty column in `COLS`.
- Window padding arithmetic is monotone-safe (pre_pad/post_pad ≥ 0 never narrow
  below KNOWN observed bounds); the only clip risk is the AF-5 stale-index timing
  gap, not the math.
- No tz / µs-vs-ms bug — `parse_dt_us` uses `calendar.timegm` (UTC) ×1e6;
  `day_index = ts//86_400_000_000` consistent SQL↔Python; boundary pinned by test.
- Q7 exclusion is defense-in-depth (KXMVE prefix on ticker/event, mve_collection,
  Exotics category).
- Fee-awareness (Q3) not triggered — packs/exports expose raw prices/counts only;
  no pack computes net/PNL. If a future summary adds notional/PNL it must sum
  `count_e4` as int and apply Q3 taker fees.
- `event_measure_split` aggregation (min/max ts, count(*), per-(event,day)) is
  correct; per-event category via `any_value` safe (one category per event).
