# ALPHA-SPRINT-01 H6 Calibration Extremization Preregistration

Date: 2026-07-24  
Mode: read-only research and shadow evaluation  
Financial authority: none  
Live orders: forbidden  
Cash-PnL claim: forbidden

## Objective

Test whether favorite-side price miscalibration in politics markets is large
enough, stable enough, and cheap enough to survive executable bid/ask costs.
The goal is not to prove a theory about all prediction markets. The goal is to
produce one or more backtest-worthy strategy templates that remain positive
after a frozen cost model and that do not disappear on placebo universes.

This experiment is deliberately narrower than a generic longshot paper:

- it only uses the past two weeks of trade, L1, and L2 data available in the
  source-bound checkpoint at run time;
- it only promotes strategies if they remain stable on a frozen validation
  slice;
- it treats sports, crypto, and finance as placebos, not as additional
  promotion targets;
- it uses executable bid/ask only, never midpoint fills;
- it reports modeled shadow EV only, not live or realized PnL.

## Research question

Does a calibrated and extremized favorite-side probability surface predict
future outcomes well enough, after executable costs, to support a tradable
politics strategy?

The experiment answers that question at three levels:

1. raw price calibration;
2. raw price calibration with a large-trade filter;
3. frozen calibrated fair-value gap with a single extremization factor.

The output must include heat maps and a shortlist of candidate strategy cells,
but no candidate may be promoted from validation alone.

## Frozen split

Use the most recent contiguous two-week block of source-bound trade, L1, and L2
data available when the runner is instantiated.

- TRAIN: first contiguous week in the block.
- VALIDATE: second contiguous week in the block.

No row-level mixing is allowed. All thresholds, transformation rules,
calibration parameters, and promotion gates are fit on TRAIN only and sealed
before the first VALIDATE payload is read.

If the source checkpoint exposes more than two weeks, ignore the rest for this
experiment. If it exposes fewer than two contiguous weeks, the experiment is
not estimable and must stop as not estimable.

## Universe and placebos

Primary universe:

- politics markets only.

Placebo universes:

- sports;
- crypto;
- finance.

Placebos must reuse the exact same machinery:

- same q normalization;
- same horizon buckets;
- same large-trade definition;
- same entry/exit rules;
- same fee model;
- same validation split;
- same reporting format.

The only difference is the market universe. Do not rematch placebos on outcome
or on observed profitability.

## Observable unit

The fundamental unit is a market-day opportunity.

A market-day opportunity is eligible when:

- the market belongs to the selected universe;
- there is a valid two-sided executable quote;
- the market has an identifiable favorite side;
- the quote is inside one of the frozen q buckets;
- the opportunity has enough subsequent data to evaluate the chosen exit
  horizon;
- the quote is not synthetic midpoint data.

Repeated opportunities within the same market are allowed, but every entry is
closed before the next one can open in that market. No overlapping position
stacking is allowed in H6.

## q normalization

Normalize each eligible state to the favorite side:

- `fav_ask` = executable ask for the side with the higher executable price;
- `anti_ask` = executable ask for the other side;
- `q` = `fav_ask`;
- `fav_side` = side associated with `q`.

This makes the bins comparable across YES/NO orientation and across markets
with different label conventions.

Bucket `q` in exact 5-cent bands:

- 50 to 55
- 55 to 60
- 60 to 65
- 65 to 70
- 70 to 75
- 75 to 80
- 80 to 85
- 85 to 90

Primary focus:

- 55 to 75.

Higher-risk secondary band:

- 75 to 90.

The 50 to 55 band is retained as a lower-edge calibration reference. Values
below 50 or above 90 may be reported if present, but they are exploratory only
and cannot drive promotion.

## Horizon buckets

Horizon buckets are frozen ex ante and map to the forced exit clock:

- 30 minutes
- 1 hour
- 3 hours
- 24 hours
- close

The horizon bucket is the maximum holding time for the trade. If the market
closes earlier, the position closes at close. If a bracket exit fires earlier,
that exit wins.

The primary report should stratify by horizon bucket, not pool all holding
times together.

## Large-trade flag

Define `large_trade_flag` once on TRAIN and freeze it for VALIDATE.

Recommended rule:

- compute trade notional or trade size on TRAIN politics only;
- freeze the 90th percentile threshold;
- round the threshold up to a clean exchange-size boundary;
- apply the same threshold unchanged to all universes and all validation data.

If the source data make trade size and notional disagree materially, prefer the
contract-size definition and keep the choice fixed. Do not choose the threshold
after looking at validation EV.

## Strategy variants

Three frozen variants are admitted.

### Variant A: price only

Use only `q_bucket` and `horizon_bucket`.

Training target:

- realized outcome frequency of the favorite side in each cell.

Trading rule:

- if the frozen fair probability of the favorite side is above the executable
  ask by more than costs, buy the favorite side;
- if the frozen fair probability is below the executable price by more than
  costs, buy the anti-favorite side;
- otherwise abstain.

This is the raw benchmark.

### Variant B: price only, large-trade filtered

Same as Variant A, but only admit opportunities with `large_trade_flag = 1`.

This isolates whether calibration quality is materially different after large
prints or large bursts of activity.

### Variant C: calibrated fair-value gap

Use a frozen calibration surface and one extremization scalar.

Recommended model:

- fit a monotone or isotonic calibration surface on TRAIN using
  `q_bucket x horizon_bucket x large_trade_flag` for politics;
- shrink sparse cells toward adjacent buckets rather than fitting a separate
  free parameter for every cell;
- freeze one scalar extremization factor `lambda > 1` on TRAIN only;
- define `p_ext = sigmoid(lambda * logit(p_cal))`.

Trading rule:

- buy the favorite side if `p_ext - fav_ask` exceeds costs;
- buy the anti-favorite side if `(1 - p_ext) - anti_ask` exceeds costs;
- otherwise abstain.

This is the main candidate for a backtest-worthy shortlist if it survives
validation.

## Entry and exit rules

Entry:

- use executable bid/ask only;
- no midpoint fills;
- no synthetic fills from future information;
- no position if the required side is not currently executable.

Exit:

- evaluate bracket exits and time exits on the same executable path;
- no overlap with a second entry in the same market before the first trade is
  closed;
- if a stop and a time exit become eligible on the same timestamp, the stop
  wins;
- if both bracket targets and stops are reachable in the same bar, use the
  first executable touch in sequence order, never the best-of-bar.

Bracket families:

- `+5 / -5`
- `+10 / -10`

Time stops:

- 30 minutes
- 1 hour
- 3 hours
- 24 hours
- close

These are not free parameters. They are a frozen evaluation lattice.

## Metrics

Report all metrics at the market-day opportunity level and at the unique
market level.

Required outputs:

- sample count;
- unique market count;
- hit rate;
- average and median MFE;
- average and median MAE;
- modeled net EV after costs;
- gross EV before costs;
- exit mix by target / stop / time stop;
- payoff by q bucket;
- payoff by horizon bucket;
- payoff by large-trade flag;
- placebo comparison by universe;
- heat maps by q bucket and horizon bucket;
- cell-level counts.

MFE and MAE must be computed on the executable path, not on midpoint data.
Report them in cents per one-contract opportunity.

## Cost model

Costs must be frozen before validation.

Required cost components:

- executable spread crossing;
- exchange fees;
- slippage / impact haircuts;
- time-stop carry / opportunity loss if modeled;
- cancel or exit latency if the simulation includes it.

If exact fee tables are available from the source-bound environment, use them.
If not, use a conservative frozen fee table from the repo's cost model and mark
it explicitly as modeled. Never fit the cost model on VALIDATE.

## Overfitting guardrails

This experiment is only allowed to use a small frozen lattice. It may not grow
new bins after validation starts.

Guardrails:

- no bin merging after seeing validation results;
- no threshold re-tuning after seeing validation results;
- no market-universe changes after seeing validation results;
- no placebo reassignment after seeing validation results;
- no horizon additions after validation starts;
- no price-bucket additions after validation starts;
- no use of midpoint fills;
- no use of realized or live orders;
- no promotion on cells with `n < 100`.

Cells with `n < 100` are exploratory. They may be reported, but they may not
be used alone to promote a strategy template.

## Promotion rule

A strategy template is backtest-worthy only if all of the following hold:

- politics VALIDATE modeled net EV is positive after costs in the primary band
  `55 to 75`;
- the same template is not dependent on a single horizon bucket;
- the same template is not dependent on a single price cell;
- the placebo universes do not show the same or better EV pattern;
- validation does not reverse the sign established on TRAIN;
- `n >= 100` in every promoted cell;
- the result survives both bracket families or survives one bracket family with
  the other clearly non-harmful;
- the result survives the large-trade filter if the variant uses it.

If only the `75 to 90` region works, report it as higher-risk exploratory and
do not promote it as the primary strategy family.

## Shortlist output

The final report must produce a shortlist with three labels:

- `PROMOTE` — stable positive modeled EV after costs, not explained by
  placebos;
- `WATCH` — promising but not yet stable enough for a backtest allocation;
- `DROP` — negative after costs, placebo-matched, or overfit.

The shortlist should contain at most one primary candidate per variant family.
Do not flood the report with dozens of cells that differ only by tiny bin
changes.

## Report layout

The runner must emit:

1. a machine-readable receipt;
2. a markdown report;
3. a validation appendix;
4. heat maps by price and horizon;
5. a cell summary table with exploratory flags;
6. a placebo comparison table;
7. a shortlist table.

The report must explicitly label:

- sample;
- market count;
- hit rates;
- MFE;
- MAE;
- modeled net EV after costs;
- placebo comparison;
- exploratory cells.

## What this does not claim

- no live orders;
- no real PnL;
- no midpoint fills;
- no fee-free profitability claim;
- no overfitted bin search;
- no guarantee that a later exact-fill backtest will survive execution costs.

## Intended downstream use

If H6 survives, it should feed a later exact-fill backtest and execution
integration step, not the live trading path directly. The purpose of this
experiment is to identify a small number of robust, backtest-worthy candidate
templates, not to maximize the number of interesting cells.

## Cross-declaration (audit sync 2026-07-25)

H6 and H6b maintain separately closed multiple-comparison budgets: every
test computed under this preregistration counts only against this
document's BH denominator, and none against H6b's. Neither program may
read, condition on, or tune against the other's VALIDATE split under any
circumstance. Status note: this preregistration is superseded by the H6b
execution file (2026-07-24); it remains in the record for lineage and its
budget is closed at zero VALIDATE reads.
