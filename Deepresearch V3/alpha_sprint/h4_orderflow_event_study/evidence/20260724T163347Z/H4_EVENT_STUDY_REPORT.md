# H4 order-flow event study

- State: `COMPLETE`
- TRAIN: `2026-07-12, 2026-07-15`
- VALIDATE: `2026-07-17`
- Model: `ENABLED`
- TRAIN-locked sports: `Basketball, Esports, Tennis`
- Threshold: `1.897442`

## Baseline results

| split | date | sport | horizon | triggers | evaluable | mean gross (¢/contract) |
|---|---|---|---:|---:|---:|---:|
| TRAIN | 2026-07-12 | Basketball | 0.1s | 5556 | 5541 | -3.3162 |
| TRAIN | 2026-07-12 | Basketball | 1s | 5556 | 5548 | -3.0869 |
| TRAIN | 2026-07-12 | Basketball | 5s | 5556 | 5117 | -2.5007 |
| TRAIN | 2026-07-12 | Esports | 0.1s | 39892 | 39864 | -4.2668 |
| TRAIN | 2026-07-12 | Esports | 1s | 39892 | 39853 | -3.7553 |
| TRAIN | 2026-07-12 | Esports | 5s | 39892 | 38633 | -3.0676 |
| TRAIN | 2026-07-12 | Tennis | 0.1s | 400063 | 398770 | -2.2038 |
| TRAIN | 2026-07-12 | Tennis | 1s | 400063 | 398338 | -1.7557 |
| TRAIN | 2026-07-12 | Tennis | 5s | 400063 | 385215 | -1.4614 |
| TRAIN | 2026-07-12 | _ALL_LOCKED_SPORTS | 0.1s | 445511 | 444175 | -2.4029 |
| TRAIN | 2026-07-12 | _ALL_LOCKED_SPORTS | 1s | 445511 | 443739 | -1.9519 |
| TRAIN | 2026-07-12 | _ALL_LOCKED_SPORTS | 5s | 445511 | 428965 | -1.6184 |
| TRAIN | 2026-07-15 | Basketball | 0.1s | 6601 | 6591 | -5.7254 |
| TRAIN | 2026-07-15 | Basketball | 1s | 6601 | 6591 | -5.0366 |
| TRAIN | 2026-07-15 | Basketball | 5s | 6601 | 6341 | -4.8999 |
| TRAIN | 2026-07-15 | Esports | 0.1s | 37883 | 37798 | -3.8390 |
| TRAIN | 2026-07-15 | Esports | 1s | 37883 | 37665 | -3.1743 |
| TRAIN | 2026-07-15 | Esports | 5s | 37883 | 36585 | -2.3346 |
| TRAIN | 2026-07-15 | Tennis | 0.1s | 732562 | 730845 | -2.5239 |
| TRAIN | 2026-07-15 | Tennis | 1s | 732562 | 729864 | -2.1251 |
| TRAIN | 2026-07-15 | Tennis | 5s | 732562 | 714034 | -1.7639 |
| TRAIN | 2026-07-15 | _ALL_LOCKED_SPORTS | 0.1s | 777046 | 775234 | -2.6153 |
| TRAIN | 2026-07-15 | _ALL_LOCKED_SPORTS | 1s | 777046 | 774120 | -2.2009 |
| TRAIN | 2026-07-15 | _ALL_LOCKED_SPORTS | 5s | 777046 | 756960 | -1.8178 |
| VALIDATE | 2026-07-17 | Basketball | 0.1s | 15613 | 15600 | -4.1735 |
| VALIDATE | 2026-07-17 | Basketball | 1s | 15613 | 15600 | -4.0086 |
| VALIDATE | 2026-07-17 | Basketball | 5s | 15613 | 14528 | -3.4808 |
| VALIDATE | 2026-07-17 | Esports | 0.1s | 53888 | 53082 | -3.4554 |
| VALIDATE | 2026-07-17 | Esports | 1s | 53888 | 53152 | -2.7910 |
| VALIDATE | 2026-07-17 | Esports | 5s | 53888 | 52378 | -2.0379 |
| VALIDATE | 2026-07-17 | Tennis | 0.1s | 608635 | 600821 | -2.3269 |
| VALIDATE | 2026-07-17 | Tennis | 1s | 608635 | 600451 | -1.8879 |
| VALIDATE | 2026-07-17 | Tennis | 5s | 608635 | 590784 | -1.5008 |
| VALIDATE | 2026-07-17 | _ALL_LOCKED_SPORTS | 0.1s | 678136 | 669503 | -2.4594 |
| VALIDATE | 2026-07-17 | _ALL_LOCKED_SPORTS | 1s | 678136 | 669203 | -2.0090 |
| VALIDATE | 2026-07-17 | _ALL_LOCKED_SPORTS | 5s | 678136 | 657690 | -1.5874 |

## Interpretation boundary

These are past-only, effective-time displayed-touch event-study returns. They assume one contract of displayed depth at entry and exit, before fees and decision/order latency. They are not observed fills, are not portfolio-additive, and are **not cash PnL**.

The 100ms and 1s horizons are primary. The 5s horizon is a sensitivity / displayed-state proxy only; every exit quote, including the 5s sensitivity, must have been effective within the prior 1s and must remain in the entry snapshot epoch.

The source has no authenticated game phase. Every row is treated as `UNKNOWN_PHASE`; this report makes no live/pre-game claim. The 2026-07-17 slice is held out only for this H4 fit and is not described as a globally untouched confirmation cohort.

Reverse and deterministic hash-direction placebo controls, complete zero-trigger/no-depth denominators, and source lineage are in `H4_EVENT_STUDY_RECEIPT.json`.
