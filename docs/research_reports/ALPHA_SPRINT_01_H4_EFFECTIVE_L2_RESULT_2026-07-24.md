# ALPHA-SPRINT-01 H4 Effective-L2 Result

Date: 2026-07-24  
Decision: `KILL_AS_STANDALONE_AGGRESSIVE_ORDERFLOW_STRATEGY`  
Cash-PnL claim: forbidden  
Execution/fill claim: forbidden

## Question

Can past-only signed flow, quote velocity, displayed-depth imbalance, and the
current microprice identify an aggressive one-contract trade that earns a
positive displayed-touch gross return before fees and latency?

## Frozen run

- Code commit: `80fa07e819fd87c5c252219f34b8a23d1da9d446`
- Source binding:
  `c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979`
- TRAIN: 2026-07-12 and 2026-07-15
- Sprint-held-out VALIDATE: 2026-07-17
- Full payload hashes recomputed: yes
- W09 partition membership checks: 147/147 passed
- TRAIN model sealed before first VALIDATE payload read: yes
- Primary horizons: 100 ms and 1 s
- Sensitivity only: 5 s

The frozen TRAIN model used 12,225,561 eligible rows and locked Basketball,
Esports, and Tennis. Cricket failed the frozen >=100 feature-eligible
TRAIN-decision sport-lock rule. The model triggered on 678,136 held-out events,
or 9.92% of feature-eligible locked-sport decisions.

## Held-out displayed-touch result

Mean gross return is cents per one-contract event. It crosses the displayed
touch at entry and liquidation, but excludes fees, decision/order latency,
queue position, overlap netting, and observed-fill uncertainty.

| Segment | 100 ms | 1 s | 5 s sensitivity |
|---|---:|---:|---:|
| All locked sports | -2.4594¢ | -2.0090¢ | -1.5874¢ |
| Basketball | -4.1735¢ | -4.0086¢ | -3.4808¢ |
| Esports | -3.4554¢ | -2.7910¢ | -2.0379¢ |
| Tennis | -2.3269¢ | -1.8879¢ | -1.5008¢ |

At the primary 100 ms horizon, 669,503 of 678,136 triggers were evaluable
(98.73%), and only 1.17% had a positive gross return. At 1 s, 669,203 were
evaluable and 5.99% were positive.

## Controls

The held-out point estimates are directionally ordered in favor of the baseline.
This is consistent with a directional component but, without dependence-aware
uncertainty or matched evaluable samples, does not establish one. In every case
the displayed-touch gross return remains negative.

| Horizon | Baseline | Reverse | Hash-direction placebo |
|---|---:|---:|---:|
| 100 ms | -2.4594¢ | -2.8583¢ | -2.6595¢ |
| 1 s | -2.0090¢ | -3.1090¢ | -2.5586¢ |
| 5 s sensitivity | -1.5874¢ | -3.2447¢ | -2.4189¢ |

## Decision

H4 fails the pre-registered first kill condition: no frozen segment has a
positive executable displayed-touch return even before fees and latency.
Exact fees would reduce these gross returns further. Decision/order latency,
queueing, and observed-fill effects were not measured and could change the
event set and returns; no execution-PnL rescue is claimed. H4 therefore does
not advance to the shared cash-PnL Spine as a standalone aggressive strategy.

The order-flow score may remain available as a risk filter, execution-timing
feature, or input to a model with an independent fair-value anchor. That is a
feature-use decision, not a strategy or profitability claim. Because the first
economic kill gate already failed across every locked segment and horizon,
later delayed-signal and unrelated-market placebo stages are not required to
reject this standalone action.

## Interpretation boundary

- Results are displayed-touch event-study returns, not observed fills or cash
  PnL.
- The source has no authenticated live/pre-game phase; all rows remain
  `UNKNOWN_PHASE`.
- Repeated decisions within a market are dependent, so the event count is not
  an independent sample size.
- The 2026-07-17 slice is held out for this sprint, not a globally untouched
  confirmation cohort.
- Five-second results require a quote no older than one second and are
  sensitivity-only.

Machine evidence and the exact model seal are stored under
`Deepresearch V3/alpha_sprint/h4_orderflow_event_study/evidence/20260724T163347Z/`.
