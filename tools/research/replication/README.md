# Paper-replication toolkit (data map + shared code)

**Purpose**: reproduce research-paper experiments (FLB, calibration, maker-edge …)
on our own data without re-discovering where anything lives. Import this package;
don't write one-off path-digging scripts.

```python
import sys; sys.path.insert(0, '<repo>/tools/research')  # or rsync the dir next to your script
from replication import data_access as da, taxonomy, stats

dates = da.sealed_dates()                 # ONLY use sealed dates
files = da.trade_files(dates)             # trades csv.gz
da.build_settlement_index('idx.parquet')  # settlement truth (finalized yes/no)
con = da.duckdb_connect()                 # prod-safe: 2 threads / 8GB cap
```

## Data map (verified 2026-07-27)

| Dataset | Where | Format | Notes |
|---|---|---|---|
| Trade prints (firehose, whole exchange) | `<warehouse>/facts/trades/category=*/subcategory=*/date=*/` | csv.gz | cols incl `ts_utc`(µs) `market_ticker` `series_ticker` `yes_price_e4` `no_price_e4` `count_e4` `taker_side` |
| L1 quotes | `<warehouse>/facts/orderbooks_l1/...` same hive layout | parquet | `yes_bid_e4/yes_ask_e4` + qty; 0 = side empty |
| Full book | `<warehouse>/facts/orderbooks_full/...` | parquet | |
| Seals (which dates are trustworthy) | `<warehouse>/seals/date=*.json` | json | sealed 2026-07-10..07-25 as of 07-27 |
| Settlement truth (all families) | catalog_normal `snapshot=*/shards/*.json` (3,303 series) | json | per-market `result` yes/no, `status=finalized`, `close_time`, `settlement_ts`; snapshot 20260724T213017Z → markets settled after 07-24T21:30Z absent |
| Politics-only settlement extract | `h6b_settlements_v4.json` (EC2 `~/h6b_inputs` + Mac `Deepresearch V3/alpha_sprint/h6b_politics_path_study/inputs/`) | json | 1,053 outcomes, subset of the above |

**Warehouse roots** (auto-detected by `data_access.warehouse_root()`):
- EC2 prod: `/home/ubuntu/hft-bot/work/warehouse` — full sealed history 07-10..
- Mac: `<repo>/work/warehouse` — pre-cutover only (07-07..09); production moved to EC2 on 07-09.
- catalog_normal full snapshot: **EC2 only** (`/home/ubuntu/h6b_inputs/catalog_normal`).

## Unit conventions (stop re-deriving these)
- `*_e4` prices are 1e-4 dollars → **cents = e4/100** (deci-cent ticks exist: 140 = 1.4¢)
- `count_e4` = contracts × 1e4 (memory: daily-volume-metrics)
- `ts_utc` = µs UTC epoch; yes+no trade prices sum to exactly 10000 e4
- fees: see the `fees.py` section below — taker 7·p·(1−p) ¢/contract by default, and
  maker is **not** always zero (~80 sports series charge it)

## Methodology rules baked into `taxonomy.py`
- **Never pool across market families** (operator audit ruling 2026-07-25);
  first stratification layer is always the family (`taxonomy.family_of`)
- Sample unit for settlement studies = **market**, not trade prints (RC-2 lesson:
  per-print n inflates by tens of times); use `stats.cluster_bootstrap_ci`
- Standard price buckets / time-to-close bands + DuckDB CASE fragments provided

## How to run heavy jobs (EC2 = prod box, be polite)
- `rsync` this dir to a scratch dir on EC2, run with `nice -n 19 ionice -c3`
- `da.duckdb_connect()` caps threads=2 / memory 8GB by default — keep it
- Never write into `<warehouse>`; outputs go to your scratch dir

## Fees — use `fees.py`, do not hand-roll
`stats.taker_fee_c` is the DEFAULT taker rate only. The real schedule (July 7 2026) is:
- taker `roundup(Mt × 0.07 × C × P × (1−P))`, default `Mt = 1`
- maker `roundup(Mm × 0.0175 × C × P × (1−P))`, **default `Mm = 0`**

`fees.NONSTANDARD` carries the 86 series with published multipliers. ~80 of them (every
per-game sports book: KXMLBGAME, KXNFLGAME, KXWCGAME, KXATPMATCH, KXWTAMATCH …) charge the
maker 0.4375 ¢/contract at the money — the same order as the whole maker edge in those books.
A handful (KXBTCY, KXETHY …) are fee-free on both sides. Crypto 15-min/hourly carry no maker
fee. Net every edge before calling it profitable.

## Output — PDF with graphs, always (operator rule, 2026-07-27)
`report.py` is the house report style: validated palette, A4 landscape pages, cover with stat
tiles, `header`/`footnote`/`table`/`small_multiples` helpers, and a `Report` context manager.

```python
from replication.report import Report, header, footnote, S1, S2
with Report('out.pdf', 'My study') as rep:
    fig = rep.page(); header(fig, 'E1 · Finding', 'what the reader sees', 'tag')
    ax = fig.add_axes(rep.AX_WIDE); ...
    footnote(fig, 'definitions and caveats for THIS chart'); rep.save(fig)
```
`REPORT_PNG=<dir>` also drops one PNG per page — look at them before shipping. Never hand-roll
matplotlib chrome; never ship a results markdown as the deliverable.

**Chinese reports need `Report(..., cjk=True)`.** matplotlib does not walk the `font.sans-serif`
list per glyph (measured on 3.9.4 — the first family wins and missing glyphs are dropped with a
warning), and DejaVu carries no Han glyphs, so without the flag every Chinese character renders
as a box. The CJK mode puts `Arial Unicode MS` first, which also carries ¢ ▸ ① and the
typographic minus so both modes look the same. `wrap()` counts CJK glyphs as two display columns
and breaks between them (Chinese has no word spaces, so a whitespace-only wrapper runs the
paragraph straight off the page).

## Experiments built on this
- `experiments/flb_stratified.py` — FLB stratified measurement
  (spec: `agents/audit/EXPT_FLB_分层实验设计_2026-07-27.md`)
- `experiments/sports_maker_replay.py` — pre-game/in-game split + four-line worst-case bill
  + queue reality for the FLB cells (report: `agents/audit/REPORT_SPORTS_MAKER_REPLAY_2026-07-27.md`)
- `experiments/flb_report.py`, `experiments/sports_maker_replay_report.py`,
  `experiments/bo2026_report.py`, `experiments/who_profits_report.py` — the PDF renderers; each
  reads only its experiment's artifacts, so a report can be rebuilt without re-running the pass
- `experiments/microstructure_e15{,_analyze,_report}.py` — E15, replication of Dubach (2026)
  "The Anatomy of a Decentralized Prediction Market" on our tape: spread/tick-binding/empty-book
  map by price decile and time-to-close, ten-level depth shape (both sides, moneyness, strike
  ladder), family effective half-spread against the exchange's own `taker_side`, and the
  within-market depth-vs-time-to-close panel. Carries a **known-answer gate**: the L2 delta
  stream is replayed under both candidate orderings and scored point-by-point against each
  market's own L1 quote timeline — never assume a feed's replay order, measure it.
  Pre-registration + report: `agents/audit/PREREG_E15_*`, `Repo Research/MICROSTRUCTURE_E15_20260727/`
- `experiments/who_profits{,_analyze,_report}.py` — replication of Della Vedova (2026)
  "Who Profits from Prediction? Execution, not Information" on our tape: maker/taker
  decomposition, benchmark contamination, lifecycle timing, accuracy × price-distance,
  per-series maker economics. Report + method notes: `Repo Research/WHO_PROFITS_20260727/`
