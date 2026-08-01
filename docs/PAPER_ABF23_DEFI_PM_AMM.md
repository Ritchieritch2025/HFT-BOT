# Paper card — ABF23 "Decentralized Prediction Markets and Sports Books"

Amini, Bichuch & Feinstein (2023-08-01, SSRN 4513239). Axiomatic theory of
liquidity-based AMMs (LBAMMs) for prediction markets + Super Bowl LVII LP
backtest. Source PDF:
`/Applications/Decentralized prediction markets and sports books .pdf` (30pp).

## Mechanism (why the edge exists, who loses)

Passive LPs in a prediction-market AMM earn fees from flow but hold the
losing side of every informed trade. The paper proves the structure is
default-free and arbitrage-free *within* the pool, but concedes the fatal
economics in its own conclusion: **as an event approaches certainty, a
static-fee LP is arbitraged continuously until nothing remains** — the LP is
the designated loser against informed flow unless fees widen dynamically
with the informational edge of the takers. Losing side = passive liquidity;
winning side = anyone trading the pool late with better event information.

## What maps to Kalshi

- **No strategy line spawned.** Kalshi is a CLOB, not an AMM — there is no
  pool to LP. Value is doctrine for our own maker quoting, because a maker
  quoting a fixed spread IS a static-fee LP:
  - Their dynamic-fee recommendation = our VPIN retreat / spread widening
    near resolution and after game events. This paper is the theory-side
    justification: static spread → certain bleed-out at event end.
  - LP P&L decomposition (fees earned vs terminal inventory loss dependent
    on outcome) is exactly the maker ledger split we should report per line:
    spread capture vs settlement-direction P&L.
- Venue watch: if an AMM-based sports venue with meaningful volume appears
  (DeFi sportsbooks per this paper), LPing it with our own faster fair-value
  model is a *separate future line* — the paper says naive LPs get eaten,
  which means informed LPs eat.

## Priors (labeled as priors — Super Bowl LVII, one game, one venue-model)

- Log-utility AMM, 1% fee: fees ≈ +2.3% of liquidity pre-game; realized
  +5.4% (KC won) vs −0.7% counterfactual (PHI). Break-even fee 1.3%.
- StableSwap-utility AMM, 1% fee: +6.5% pre-game, +15.7%/−2.5% by outcome;
  break-even 1.38%. Outcome dependence of "market-neutral" LP P&L is large —
  same caution applies to our maker inventory at settlement.
- Authors note their fee numbers are a *lower bound* (bookmaker repriced
  sparsely); real flow adds volatility ⇒ more fees but also more adverse
  selection. Single-event N=1 — mechanism transfers, numbers don't.

## Verdict

Rung 0 only, no replay sketch — nothing here proposes an entry rule on a
CLOB. Value = (1) theoretical backing for dynamic spread/retreat in the MM
doctrine (cite in MM_DOCTRINE_FROM_LITERATURE.md company), (2) the
fees-vs-settlement P&L split as a reporting convention for maker lines,
(3) a watch item on AMM sports venues as future prey.
