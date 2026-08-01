# Favourite–longshot bias, stratified — 2026-07-27

**Question**: the literature says recreational flow overpays for longshots and underpays
favourites (Snowberg–Wolfers; surveyed in `docs/PAPER_GS24_ALGO_SPORTS.md`). Is that true on
*our* tape, in *which* markets, at *which* prices, and by *how much* — measured against what it
would actually cost us to trade it?

**Answer in one line**: the bias is real but it is **not one phenomenon** — six market families
produce six different shapes, and in economics/politics the deviation is not FLB at all but
directional YES-optimism.

## Artifacts here

| file | what |
|---|---|
| **`FLB_FIGURES_2026-07-27.pdf`** | **the figures (6 pp)** — bias by price bucket per family, the convergence check, the cells-that-clear-cost map, the 45–55¢ null cell, method. Rendered 07-27 from the artifacts below. The prose report's 图版 section promises "three charts to read the whole table"; its `@@FIGURES@@` marker was filled with four numeric tiles, so no chart ever shipped |
| `REPORT_FLB_分层测量结果_2026-07-27.pdf` | the prose report (spec-by-spec, three pre-registered sanity checks, full stratified table) — text and tables only, no charts |
| `cells_full.csv` | all 613 cells: family × side × price bucket × time band, deviation, cost, CI, n |
| `gap_events_top.csv`, `gaps_summary.csv` | ≥30¢ jump events between adjacent prints |
| `sports_series_map.csv` | series → sports family mapping used |
| `sanity.json`, `stats_cells.json` | coverage and sanity-check output |
| `sports_maker_replay/` | the follow-on replay (see its own section below) |

Spec: `agents/audit/EXPT_FLB_分层实验设计_2026-07-27.md` ·
report source: `agents/audit/REPORT_FLB_分层测量结果_2026-07-27.md` ·
code: `tools/research/replication/experiments/flb_stratified.py` ·
figures: `tools/research/replication/experiments/flb_report.py` (re-render with
`python flb_report.py . FLB_FIGURES_2026-07-27.pdf` from this directory)

## Coverage, honestly

16 sealed days (07-10..25), **135.8M prints**, of which **111.7M (82.3%)** joined settlement
truth (`catalog_normal` snapshot 20260724T213017Z, 2,899,760 finalized yes/no markets).
Unjoined splits as: Exotics 11.8M (100%, out of scope by design), Sports 9.9% / Crypto 8.0%
(window-tail, normal truncation), **Politics 68% / Elections 91% / Economics 47%** — so those
families are a *fast-settling subset* and must be read as indicative only.

Sample unit = market. Cells with n < 30 are greyed and carry no conclusion. Bootstrap ×1000,
seed 20260727.

## Results

- **133 of 613 cells** show a deviation larger than the two-sided cost (median in-cell spread
  + taker fee 7·p·(1−p)) with n ≥ 30 and a CI excluding zero.
- **crypto_hourly = textbook FLB**: >60m longshots +2~8pp, favourites −2~−7pp, and the
  convergence check PASSES (3.35 → 1.23pp).
- **Sports near the end (<10m) = a symmetric lottery tax**, +10~16pp.
- **sports_prop overprices both ends**: 95-99¢ YES +5.86pp on n=15,663 — the largest statistical
  power in the table, 3.6× cost.
- **econ / politics fail the null-hypothesis cell (45–55¢)**: econ YES +18.8pp / NO −14.3pp,
  politics >60m YES +6.65 / NO −5.27. By the spec's own logic that means a systematic component
  unrelated to FLB — directional YES over-pricing, same direction as the 07-18 audit's
  "optimism tax". Price-bucket gradients in these two families must be read *after* netting the
  directional component.
- Complementarity check PASSES: of the 90 pairs where both sides deviate ≥2pp, 84 (93.3%) have
  opposite signs. The 6 same-sign violations sit in the middle buckets — both sides paying the
  spread, economically coherent, not a measurement error.
- Convergence FAILS for sports_game/prop (4.91 → 6.84pp). Diagnosed as **not** an anchor problem
  (settlement_ts is never earlier than close_time anywhere in the库): the <2m band is in-game
  trading, where desperate comeback tickets get *more* expensive as the clock runs out.

## Three confounds that must travel with these numbers

1. Both crypto families drifted up over the 16 days — a directional difference is not a bias.
2. The sports >60m band mixes in-game prints; the convergence failure comes from end-of-game
   behaviour, not from the anchor.
3. Politics/Elections/Economics are a fast-settling selection (see coverage above).

## Follow-on: `sports_maker_replay/`

Pre-game/in-game split + four-line worst-case bill + queue reality. Report:
**`sports_maker_replay/SPORTS_MAKER_REPLAY_2026-07-27.pdf`** (6 pp) ·
source `agents/audit/REPORT_SPORTS_MAKER_REPLAY_2026-07-27.md` ·
code `tools/research/replication/experiments/sports_maker_replay.py` ·
figures `tools/research/replication/experiments/sports_maker_replay_report.py`.

It **corrects** the "trade both ends" reading of the table above:

| line | total | EV ¢/contract | worst single fill | worst day | max drawdown |
|---|---|---|---|---|---|
| fade favourites (props, all phases) | **+$4,875** | +2.2~+4.9 | **−10¢ (capped)** | −1 | **≈0** |
| fade favourites (single game) | +$465 | +2.9~+9.3 | −10¢ | −4 | −4 |
| fade longshots (props) | +$1,011 | +0.4~+1.2 | **−99¢** | −54 | −75 |
| fade longshots (single game) | **−$178** | −0.0~−2.2 | −99¢ | −37 | −149 |

The lottery tax only lives in the narrow <10-minute band; aggregate the whole in-game phase and
fair pricing earlier in the game dilutes it away. The favourite end is the one that clears the
loss-budget doctrine: small wins, high frequency, worst single fill capped at −10¢.
Favourite over-pricing exists **pre-game** too (+11.4pp single-game YES), which releases the
convergence-check suspension for that end.

Capacity is queue-bound, not flow-bound: prop-favourite NO-side displayed depth is only
**25–33 contracts** against 49.7M contracts of volume in the same cells.

Not a go-live basis. Still missing: per-event L2 queue simulation, cross-window robustness, a
loss-budget bill recomputed at real size, and RC-line registration.
