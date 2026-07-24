# ALPHA-SPRINT-01 H4 Early Diagnostic

Date: 2026-07-24  
Status: `MARKOUT_DIAGNOSTIC_ONLY`  
Cash-PnL claim: forbidden  
Execution claim: forbidden

## Question

Does an aggressive Kalshi trade contain short-horizon directional information,
and which sports are worth putting through the full effective-time L2 test?

## Exact input

Read-only W09 checkpoint:

`source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979/b01_observations`

TRAIN dates are 2026-07-12 and 2026-07-15. The 2026-07-17 date is held out
within this sprint; it is not represented as a globally untouched confirmation
set.

The diagnostic uses the existing signed trade markout and contemporaneous
spread in log-odds space. `beats_one_spread_rate` means
`signed_markout_logodds > spread_logodds`. It is a screening proxy, not a
fill, fee or PnL calculation.

## Broad result

| Date | Horizon | Observations | Positive | Unchanged | Negative | Beats one-spread proxy |
|---|---:|---:|---:|---:|---:|---:|
| 2026-07-12 | 1s | 4,420,838 | 26.45% | 66.78% | 6.76% | 7.75% |
| 2026-07-12 | 5s | 4,474,990 | 30.44% | 56.80% | 12.76% | 11.68% |
| 2026-07-15 | 1s | 3,604,041 | 30.15% | 62.80% | 7.05% | 8.85% |
| 2026-07-15 | 5s | 3,652,984 | 34.37% | 51.52% | 14.11% | 13.62% |
| 2026-07-17 | 1s | 2,504,148 | 34.61% | 57.14% | 8.26% | 9.60% |
| 2026-07-17 | 5s | 2,546,229 | 39.88% | 43.64% | 16.48% | 14.77% |

The immediate falsification is that following every aggressive trade is not a
strategy: most one-second observations do not move, and only a minority clear
even this loose one-spread proxy before exact fees and executable depth.

## TRAIN-selected segments

The following sports were consistently strongest on both TRAIN dates. No sport
was selected using the held-out date.

| Sport | 7/12 1s | 7/15 1s | 7/12 5s | 7/15 5s |
|---|---:|---:|---:|---:|
| Esports | 13.34% | 15.28% | 19.98% | 22.36% |
| Basketball | 12.96% | 13.39% | 18.67% | 18.80% |
| Cricket | 12.70% | 14.48% | 18.53% | 19.30% |
| Tennis | 11.26% | 11.70% | 16.70% | 17.46% |

Held-out screening values were directionally consistent:

| Sport | 7/17 1s | 7/17 5s |
|---|---:|---:|
| Esports | 11.84% | 18.91% |
| Basketball | 11.42% | 16.78% |
| Cricket | 10.78% | 15.93% |
| Tennis | 10.21% | 16.29% |

This admits those four segments to the full H4 runner; it does not admit a
trade.

## Hard limitations

- Every row currently has `phase=UNKNOWN_PHASE`; no live versus pre-game claim
  is possible.
- The markout is not an effective-time entry and exit through L2.
- Exact fees, measured decision latency, queue position and complete exit are
  absent.
- Repeated observations within a market are dependent; the raw row count is
  not an independent sample size.
- Thresholds and feature weights still need to be fitted on TRAIN only.

## Fastest decisive next test

Aggregate past-only signed flow, quote velocity, depth depletion/refill and
microprice imbalance on TRAIN; freeze sport selection, thresholds and model
weights; then walk effective-time L2 on 2026-07-17 at 100ms, 1s and 5s. Retain
zero-trigger and no-depth rows, and run sign reversal, delayed-signal and
unrelated-market placebos.

Kill H4 immediately if no frozen segment has positive executable return before
fees, or if the effect matches its placebos. Only survivors advance to the
shared exact-fee, measured-latency, strict-exit PnL Spine.
