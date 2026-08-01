# Paper card — GS24 "Algorithmic Trading in Financial and Sports (Exchanges)"

Goodacre & Schlagman, UCL (2024-07-29). Survey/comparison of financial vs
sports exchange microstructure, Betfair-centric. Source PDF:
`/Applications/Algo Trading in Financial and sports .pdf` (27pp).

## Mechanism (why the edge exists, who loses)

Survey paper — no single strategy, but three mechanisms with a losing side:

1. **Favourite-longshot bias (FLB)**: recreational bettors systematically
   overpay for longshots and underpay favourites (risk-love + probability
   misperception, Snowberg-Wolfers). Losing side = retail directional flow
   chasing big payouts. Paper notes the bias is *weaker* on exchanges than
   sportsbooks but not gone.
2. **Latency/state-of-game edge in-play**: no co-location on sports venues;
   the edge is faster game-state data (courtsiding, GPS, drone feeds) vs
   broadcast-delayed retail. Losing side = anyone quoting stale prices
   across a game event.
3. **Sport-specific price dynamics**: football drifts predictably without
   goals (Poisson/Dixon-Coles), tennis jumps on break points (Markov),
   horse racing goes vertical near the off. Model-driven quoting beats
   naive quoting because volatility is *state-dependent, not stationary*.

## What maps to Kalshi

- **GS24-1 (spawned)**: FLB band screen on Sports — on a binary, fading a
  longshot IS backing the favourite, so one taker-buy-NO sketch across all
  bands gives the full bias map. Rung-1 sketch:
  `watchtower/strategies_lib.Gs24FlbTaker` (band = NO price at entry,
  fine-grained 80-89 / 90+ split at the favourite end).
- **GS24-2 (candidate, not built)**: in-play reversion/momentum after price
  jumps in MLB/tennis — needs the jump detector on our own tape first;
  Vizard (2023) claims momentum is *fadeable*. Park until GS24-1 verdict.
- Overround-removal methods (linear / power / Shin) are a **tooling** note:
  use Shin or power devig when converting sportsbook odds to fair value for
  cross-venue comparison, not raw normalization — linear devig misprices
  exactly the tails where FLB lives.
- Doctrine confirmations, no action: suspensions happen every game (keep-
  order behaviour matters), in-play delay ≈ Kalshi's matching latency is a
  fairness feature we can't co-locate around, uncorrelated-to-financials
  claim supports the sports-first battlefield choice already made.

## Priors (labeled as priors, mostly Betfair numbers)

- FLB: documented since Griffith 1949 across horse racing, football,
  greyhounds; blind longshot backing has "significantly lower" returns than
  favourites. No effect size given for exchanges — must estimate on tape.
- Betfair spread ≈ 1.77% at minimum tick pre-race vs ~5bps US equities;
  commissions 1-5% of winnings. Kalshi taker fee 0.07·P·(1-P) is the analog.
- Tennis hierarchical Markov (Knottenbelt): 3.8% long-term profit claim.
- Sports funds base rate: Centaur Galileo folded (-$2.5M), Stratagem
  absorbed, Priomha +17% ROI 2010-15, SIG active. Survivorship-heavy.

## Verdict

Rung 0 done; GS24-1 sketch at rung 1 — see replay verdict recorded below.
Kill rule per intake ladder: negative in its own claimed band (NO 80+ = the
favourite side) ⇒ dead; positive there with ≥200 settled ⇒ rung 2 sweep
(entry band edges, cap, in-play vs pre-game split).

### Rung-1 verdict (2026-07-21, 13-day tape 07-07→07-20): **KILL — bias is reversed on Kalshi**

Note: `watchtower.replay` crashes on the full Sports universe (43.9M rows >
3GB DuckDB limit → corrupt temp spill, duckdb 1.4.5); verdict below is the
same rule pushed down into SQL (8GB limit), qty-weighted ¢/contract, taker
fee 0.07·P·(1-P) included. Band = NO cost at entry, first 200 contracts per
market.

| NO band | net ¢/ct (+1¢ slip) | net ¢/ct (no slip) | settled contracts |
|---------|--------------------:|-------------------:|------------------:|
| 00-09   | −0.17 | +0.90 | 943k |
| 10-19   | **+0.75** | **+1.42** | 442k |
| 20-39   | −0.59 | +0.56 | 1.29M |
| 40-59   | −0.79 | +0.07 | 2.46M |
| 60-79   | −0.85 | +0.28 | 2.39M |
| 80-89   | −0.71 | +0.15 | 1.30M |
| 90+     | −0.59 | +0.31 | 1.62M |

- **Classic FLB fails in its own claimed band**: backing the favourite
  (NO 80+/90+) is −0.6 to −0.7¢/ct after realistic taker friction, and only
  +0.15/+0.31 gross — the *worst* end of the table. Kill per ladder rule.
- What the screen actually found is the **reverse**: the NO side is cheap
  *everywhere* (gross positive in all 7 bands), fattest where YES is the
  expensive favourite (NO 10-19 ⟺ YES ~82-90: +1.42¢/ct gross). This is
  independent re-confirmation of B2 YES-optimism (sports = super-single-name,
  MM doctrine §1b), not a new edge.
- The one band surviving +1¢ slippage (10-19 at +0.75¢/ct) is still under
  the +2¢/ct rung-2→3 gate, and taker-side harvest pays slippage+fee that
  the maker seat collects instead. **Disposition: no GS24-1 line; feed the
  band map to S1 sports-maker quoting density (bias quote mass toward the
  NO 10-19 / YES 80-89 region), where the same anomaly is harvested from
  the maker seat.** GS24-2 (in-play reversion) stays parked.
