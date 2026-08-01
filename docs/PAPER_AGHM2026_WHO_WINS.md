# Paper card — AGHM 2026 "Who Wins and Who Loses in Prediction Markets? Evidence from Polymarket"

Akey, Grégoire, Harvie, Martineau (2026-06-12 draft). Polymarket full tape
2022-11-11 → 2026-03-29: 2.4M users, $67B volume. Source PDF:
`/Applications/WHO WINS?.pdf` (158pp, LaTeX).

## Mechanism (why the edge exists, who loses)

Retail takers cross the spread with market orders and systematically buy
contracts that resolve *worse* than their price implied (excess hit rate −15
to −18pp in the loss tail). Makers sit on the other side: winners are net
spread *earners* (top 0.1% earn +3.2% extra from the spread), and the maker
side both keeps them active and keeps them profitable. The losing side is
recreational directional flow, concentrated in Sports/Politics.

## What maps to Kalshi

- Descriptive paper, **no new strategy line to spawn** — it is third-party
  validation of the S1 sports-maker doctrine (see
  MM_DOCTRINE_FROM_LITERATURE.md and EXPT-BO2026 S1 GO).
- Category detail: spread costs explain ~100%+ of small-loser losses in
  Politics/Culture (wide spreads), less in Sports (97.9% for the 0-20pct
  losers) — i.e. sports spread is tighter but taker flow is still paying it.
- Caution transferred: maker *level* advantage is robust, but month-to-month
  persistence is modest even for makers, and may be survivorship. Don't read
  our own good months as durable skill; keep the rolling re-estimation rule.

## Priors (labeled as priors, all Polymarket numbers)

- Top 1% of positive-PnL users capture 76.5% of profits; ~69% of all users
  end negative.
- Frac Maker Volume is the single largest predictor of positive PnL: 1 s.d.
  ↑ maker share → +9.0pp probability of positive performance (+33.1pp for
  0→1 unit).
- Maker-heavy vs taker-heavy user-months: +6.6 to +11pp probability of
  positive next-month risk-adjusted PnL, in every bucket.
- Removing just the minimum half-tick spread flips 18.5% of losers to
  non-negative — the small-loser cohort *is* the spread we would earn.
- Of the top-100 winners, a meaningful fraction are pure LPs; concentrated
  winners earn 81% of gains in sports. Insider trading not the dominant
  driver (top two prosecuted insiders rank #115 and #307).

## Rung-1/2 σ-sketch results (2026-07-21, VAR-1/2/3 in watchtower/strategies_lib.py)

Sketches: maker sell_yes on strict trade-through, hold-to-settlement, Sports,
banded by pre-trade 10-min trade-price σ. Full tape 07-07..07-20, then
train(→07-14)/test(07-15→) split. Split runs reset per-market inventory caps,
so halves are not strictly additive with the full-tape run.

- **KILLED: "quiet-market tight quote" (σ<0.5, +2¢)** — full-tape +83¢/order
  (n=2k) was entirely week-1; test half −119¢/order. All σ<0.5 cells flip
  sign across halves. Per-order PnL std is ~$20-40, so n≲1k cells are pure
  noise. Any future cell needs n≫10k + a t-stat before it may be believed.
- **ROBUST NEGATIVE: σ4+ toxic at every tested width** (+3..+8¢, n=28-46k
  per cell, both halves −10..−50¢/order, monotonically less bad with width
  but never positive). Validates σ-retreat as a hard gate for this摆法.
- **Sole surviving candidate: σ1-2 with wide quotes (+6/+8¢)** — positive in
  both halves (+8.6/+38 train, +79/+112 test, n=1.3-2.7k per cell/half).
  Only sign-consistent positive region; still ≲2 SE. Needs finer sweep +
  significance before rung 3.
- σ2-4 wide-quote profit (train +33) flipped to −44 in test — week-1 regime
  (WC?) artifact, dead.
- Adverse-selection gradient confirmed: in quiet windows, *deeper*
  trade-throughs are worse fills (quiet+big jump = news onset), the paper's
  maker-edge is at the tight end of the book, not the deep end.

## Verdict

Rung 0 only. No replay sketch needed — nothing here proposes an entry rule
we don't already run. Value = doctrine confirmation + the persistence
caveat. Cite in MM go/no-go memos as external evidence that maker-side
sports flow is the structurally winning seat.
