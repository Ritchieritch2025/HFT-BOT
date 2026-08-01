# Corrected crypto-MM shadow diagnostic

## Verdict

The source directory is nominally a 24-hour experiment, but the frozen
receipt set contains only **93.3
minutes**, and the selected corrected session contains **55.4
minutes**. These are an early diagnostic, not a 24-hour acceptance result.

The corrected session emitted **46 raw
cycle-done receipts**. One receipt was contaminated by
**2 non-shadow fills** and is not a valid
strategy outcome. The clean denominator therefore contains
**45 first-leg episodes**:
**32 natural maker pairs** and
**13 `unpaired_age` forced closes**.

Natural maker completion was
**71.11%**, below the empirical
break-even requirement of
**91.16%**. Strategy-native
realized contribution was
**-121.120c**; the raw engine counter was
**-128.180c**, including
**-7.060c** from the contaminated
cycle.

## Corrected-session boundary

Selected START: `2026-07-26T05:39:06.710270Z` through evidence cutoff
`2026-07-26T06:34:30.491982Z`.

Selection facts:

- mode=shadow
- pair and pair_prequote enabled
- shadow queue simulation enabled
- explicit corrected unpaired_age_s=60
- completed_cycles is contiguous from 1
- pair_locked_total/PAIR_LOCK.realized/HEALTH.realized agree

| session | START UTC | unpaired age | completed cycles | ending P&L | contiguous |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | 2026-07-26T05:01:10.095137Z | None | 0 | 0.000c | True |
| 2 | 2026-07-26T05:02:54.405535Z | None | 0 | 0.000c | True |
| 3 | 2026-07-26T05:07:09.809496Z | None | 10 | 13.000c | True |
| 4 | 2026-07-26T05:23:11.697169Z | None | 2 | -7.174c | True |
| 5 | 2026-07-26T05:29:18.460197Z | None | 0 | 0.000c | True |
| 6 | 2026-07-26T05:30:24.250653Z | None | 17 | -28.032c | True |
| 7 | 2026-07-26T05:39:06.710270Z | 60.0 | 46 | -128.180c | True |

Earlier START segments are excluded because process restarts reset
`completed_cycles` and `realized`. The current segment is internally
continuous from cycle 1 through cycle
46. Counter continuity establishes the
boundary; it does not establish shadow purity.

## Outcome decomposition

| class | n cycles | total | mean | p05 | p50 | p95 | wait p50 | wait p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| natural maker | 32 | 38.000c | 1.188c | 1.000c | 1.000c | 2.000c | 3.781s | 46.939s |
| unpaired-age forced | 13 | -159.120c | -12.240c | -28.936c | -8.680c | -2.748c | 60.399s | 60.785s |
| external-fill contaminated | 1 | -7.060c | -7.060c | -7.060c | -7.060c | -7.060c | 42.766s | 42.766s |

Definitions:

- Natural completion rate = natural maker closes / clean resolved first-leg
  episodes. Any cycle interval containing a non-shadow `FILL` is excluded
  from both numerator and denominator.
- Break-even rate uses the empirical class means:
  `-mean(forced) / (mean(natural) - mean(forced))`.
- Mechanical closure includes forced IOC exits. At cutoff it was
  **97.87%**, with
  **1** unresolved episode(s).
- P&L comes from consecutive `PAIR_CYCLE_DONE.pair_locked_total` differences,
  so partial `PAIR_LOCK` receipts are not double-counted.

## Shadow-purity exception

- Cycle 36: 2 non-shadow FILL receipts, 2 paired contracts, contribution -7.060c, max observed |net| 2, LIMIT_BREACH receipts 1. ask_no 32.0c order `1ba9dcfd-9128-4d80-9772-027842aea380` trade `0acb4b9c-7049-747e-d16c-28c07214f6cb`; bid 68.0c order `8f6c3446-bde9-44f9-8923-eac2eb48741c` trade `f7773920-8af5-5691-9355-0c75a07e05fd`

The raw ending counter remains reconciled, but this cycle cannot be used to
estimate strategy-native natural completion or outcome distributions.

## YES 13c → NO 96c forced-close replay

- Market: `KXBTC15M-26JUL260230-30`
- First fill: YES at **13.0c**
- Preserved maker exit: NO at
  **85.0c**
- Forced fill: NO at
  **96.0c** plus
  **0.270c** fee
- Receipt wait: **60.167s**
- Realized result: **-9.270c**

| target after first fill | observed at | touch NO ask | touch depth | touch-cross pair P&L | engine +1c IOC limit | conservative IOC pair P&L | quote age |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.25 | 0.002 | 88.0c | 1115.98 | -1.740c | 89.0c | -2.690c | 0.248 |
| 0.5 | 0.002 | 88.0c | 1115.98 | -1.740c | 89.0c | -2.690c | 0.498 |
| 1 | 0.002 | 88.0c | 1115.98 | -1.740c | 89.0c | -2.690c | 0.998 |
| 2 | 1.004 | 92.2c | 235.00 | -5.710c | 93.2c | -6.650c | 0.996 |
| 5 | 4.032 | 92.5c | 31.00 | -5.990c | 93.5c | -6.930c | 0.968 |
| 10 | 9.073 | 93.1c | 8.00 | -6.550c | 94.1c | -7.490c | 0.927 |
| 30 | 29.221 | 94.4c | 994.00 | -7.780c | 95.4c | -8.710c | 0.779 |
| 60 | 59.369 | 95.0c | 2042.00 | -8.340c | 96.0c | -9.270c | 0.631 |

The path shows the practical failure mode: the old 85c maker exit retained
queue age while the executable NO ask moved away. An immediate touch cross
was already outside the 99c maker-pair objective, but its modeled loss was
far smaller than waiting for the 60-second 96c forced fill.

Sub-second limitation: QUOTE_EVAL is throttled to roughly 1 Hz. The 0.25s and 0.5s rows carry forward the first post-fill receipt; this NDJSON cannot support tick-perfect sub-second book reconstruction.

## Evidence integrity

- Source snapshot cutoff: `2026-07-26T06:34:30.491982Z`
- Source files: 2
- Parsed receipts: 5315
- Malformed receipts: 0
- No engine/service mutation and no trading action were performed.

Exact cycle rows, distributions, checkpoint sources, source hashes, and
session-boundary receipts are in `shadow_24h_diagnostic.json`.
