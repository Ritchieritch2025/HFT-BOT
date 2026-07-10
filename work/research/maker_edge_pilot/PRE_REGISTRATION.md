# Maker-edge pilot — PRE-REGISTRATION (frozen BEFORE any metric computation)

Frozen 2026-07-10T14:20:55Z, code SHA 3575c41 (PLAN_RESEARCH_CYCLE_1 S1,
discipline #1), written to the NEW scope of the operator ruling 2026-07-10
(all tennis match series, stratified — see the PLAN S1 node). No primary
metric has been computed yet; computing it before this exists = audit REJECT.

## Scope (operator ruling 2026-07-10)
Universe = ALL tennis MATCH (win/loss) series present in 2026-07-06..08:
zero-fee (KXITFMATCH, KXITFWMATCH, KXATPCHALLENGERMATCH, KXWTACHALLENGERMATCH)
AND charged (KXATPMATCH, KXWTAMATCH). Stratified by
**maker_fee_class × tour_level** via dim_segments.

## Primary metric (the ONE verdict number PER LAYER, discipline #1)
Volume-weighted **`half-spread − 30s mid markout − maker_fee`**, ¢ per
contract, **pessimistic** fill convention, **pre-match** window, regime =
slam-week (Wimbledon).
- **Computed and reported PER LAYER (maker_fee_class × tour_level).
  CROSS-LAYER MERGE INTO A SINGLE TOTAL IS FORBIDDEN** — the zero-fee and
  charged layers are two species.
- half-spread = (yes_ask − yes_bid)/2 at fill time, cents.
- 30s mid markout = drift(30s) = sign·(mid(t+30s) − mid(t)), cents.
- maker_fee: **zero layer = 0** (fee_type=quadratic). **charged layer =**
  the discipline-#12 inline preview `ceil_to_centicent(maker_rate · P ·
  (1−P))` (maker_rate from kalshi_facts, NON-GATE/verified=false banner);
  it is subtracted as its own column.
- volume-weighted by contract count (count_e4).
- ALL OTHER buckets/horizons are EXPLORATORY (hypotheses, not conclusions).

## H1 (fee-wall hypothesis) validity
H1 — "spread capture is eaten by the fee wall" — is tested ONLY WITHIN the
**zero-fee** layer (there the fee is 0, so any negative net edge is
adverse-selection, not fees). In the charged layer the fee column is shown
for context but H1's clean form does not apply; the report states this.

## Selection / measurement separation (discipline #3)
- Universe (market name list) FROZEN on **train = 2026-07-06 + 2026-07-07**.
- **val = 2026-07-08** is measure-only (the per-layer verdict reads val).
- No same-period selection.

## Maker-fee-class verification (fail-closed Q3)
Verified 2026-07-10 against catalog/series (live GET /series fee_type) +
kalshi_facts.yaml: quadratic ⇒ zero; quadratic_with_maker_fees ⇒ charged;
series absent from the catalog ⇒ unknown ⇒ DROPPED (fail-closed).

## Fill model (pessimistic/optimistic dual, PILOT_SPEC #7)
Join-the-touch maker, no queue-position data:
- **PESSIMISTIC (back of queue, the verdict convention)**: a trade fills my
  resting touch quote only if its price goes STRICTLY THROUGH my price
  (taker buys yes at yes_price > ask_at_fill; taker sells yes at
  yes_price < bid_at_fill).
- OPTIMISTIC (front of queue, diagnostic): a trade AT the touch fills me.
- The pessimistic-fill count is n. **Per layer, n < 200 ⇒ that layer's
  conclusion is automatically "collect more data"; its primary metric is
  NOT interpreted (discipline #10).**

## Bounce / drift decomposition (discipline #11, the ONLY algorithm)
sign = +1 if taker_side='yes', −1 if taker_side='no'.
- bounce_i     = sign·(p_fill,i − mid(t_i))            [cents]
- drift_i(h)   = sign·(mid(t_i+h) − mid(t_i))          [cents]
- markout_total_i(h) ≡ bounce_i + drift_i(h)
Self-check (machine gate): per bucket, |mean(bounce)+mean(drift) −
mean(markout_total)| < 0.01¢ at every horizon. Failure = REJECT.

## Mid hygiene (discipline #5)
mid = (yes_bid_e4+yes_ask_e4)/2, LOCF from L1. As-of book must be ≤ 60s
before the trade else DROP (counted). No-market (DROP, counted): either side
qty 0, OR spread ≥ 20¢ (NOT an "ask≥99¢" blanket). locked/crossed DROP.

## Horizons / tick / causal (disciplines #6/#7)
markout horizons 1/10/30/120s (primary=30s). tick: 1¢ and sub-cent counted
separately, MAIN table limited to 1¢. phase (pre-match/in-play): a causal
detector using only info ≤ t, params frozen on train, applied to val;
pre-match only for the primary metric.

## Uncertainty (discipline #4)
Per-match block bootstrap ≥1000 resamples (resample whole matches, not
trades). CI on every headline and chart.

## Fees (discipline #12)
Inline research preview via the documented formula, forced NON-GATE /
verified=false banner. NO facts-gated tool's gate touched (OQ-1 unratified).

## Regime & grade
regime = slam-week; no extrapolation to regular tour. Mac-era data ⇒
**dev-grade**; methodology re-run on EC2 recv data in S5 (only S5 may produce
trade/reject). **S1 conclusions ∈ {methodology-valid+collect,
methodology-flawed}, per layer.**
