# PLAN — Dynamic Pricing Model (MM_ROADMAP Phase 1.5)

**For the executing agent: read `docs/GUARDRAILS.md` and `docs/MM_ROADMAP.md`
before touching anything. This plan binds to them; a conflict means STOP and
ask the operator.**

- **Phase advanced:** 1.5 (dynamic pricing model — the research answer to
  "naive join-the-touch loses to adverse selection"). Feeds Phase 2 (C++
  strategy port + shadow) but reaches for NOTHING live (P2).
- **Gates:** P1 (phase named); P2 (no live-trading reach anywhere in this
  plan); P3 (bounded Ws, demonstrated acceptance, rollback per W); P4
  (capture/ingest/export untouched — every W is read-only research on top of
  the warehouse); Q1/Q3/Q9 are load-bearing design constraints (below);
  E1/E2/E5 (tests + docs in the same change as every W).
- **Safety class of everything in this plan:** `pure` (synthetic fixtures) or
  `offline`/read-only research. No network writes, no order paths, no
  live_order tools, no EC2/S3 mutation.
- **Decision-clock law (W-TL1, 2026-07-10, binding on every W here):**
  every backtest/validation that feeds a go/no-go runs `--clock recv`
  (local_recv_ts_us). `--clock exchange` is look-ahead-biased by construction
  and is permitted ONLY as a labeled diagnostic; an exchange-clock number in
  any go/no-go table = audit REJECT (Q2).

---

## 0. Problem statement

Phase 1 measured it: naive quoting bleeds because adverse-selection cost
exceeds spread capture. Phase 1.5 (MM_ROADMAP) attacks both sides:

- **A. Fair value** — anchor quotes to a micro-price + trade-flow drift +
  cross-market (bracket-sum) constraint instead of the raw mid.
- **B. Quote generation** — Avellaneda-Stoikov-shaped reservation/half-width
  **in log-odds space** (Q1), discretized to legal cents, with time dynamics
  (δ(t) widening, cap(t) shrinking, Q6 settlement-window stop) and a
  quote-velocity jump breaker (NOT trade-volume — the 2026-07-06 BADAMS
  cricket measurement: a 37¢→28¢ 5-minute repricing on ZERO trades).
- **C. Calibration from our own data** (Q4): λ(δ) fill intensity, toxicity
  surface, realized vol — from the warehouse we already run.

This plan splits those into two groups with different start conditions:

| group | content | executable |
|---|---|---|
| **M (math-now)** | pure-math skeleton: transforms, fair value, A-S generator, sign tests — 100% synthetic data | **now** |
| **C (calibration)** | parameters and go/no-go from real warehouse data | **gated** (see §4 gate box) |

Deliverable shape: a **Python reference implementation** under `tools/`
(research-grade, testable, the spec for the Phase 2 C++ port). The C++ port,
shadow wiring, and anything touching the strategy engine are Phase 2 — out of
scope here (P2/P3). Hot-path execution engineering is
`docs/DESIGN_HOTPATH_EXECUTION_2026-07-10.md` → `PLAN_RISK_KILLSWITCH.md` and
a future execution-engine plan, not this document.

## 1. Design constraints (locked; violating any is a reject)

1. **Log-odds space for all strategy math** (Q1): skews, half-widths, vol,
   toxicity, drift corrections computed in logit space; mapped to cents only
   at the quote-emission edge. `mm_calibrate` already emits
   `vol_1min_lo` / `tox_120s_lo` logit-space columns — the model consumes
   those, not price-space stand-ins.
2. **Fees in every model** (Q3): taker ≈ 0.07·P·(1−P) rounded up per
   contract; maker fees exist on designated series and are LOOKED UP, never
   assumed zero. All fee numbers flow through `config/kalshi_facts.yaml` via
   the existing verified-facts gate (`mm_research.FeeNotVerifiedError`
   pattern): gate-mode computation with `fees.verified != true` fail-closes.
   Fee-schedule ratification (standing gate OQ-1) is an operator item; until
   ratified, go/no-go runs are blocked by the gate — by design, not by
   accident.
3. **Pessimistic fill bound is the go/no-go** (Q2): every validation reports
   the pessimistic bound; optimistic numbers are diagnosis-only.
4. **Economic-sign tests ship in the same change as the math they test**
   (Q9/E1): each Group-M W below carries its own sign tests; a later "fix"
   to strategy math without updated sign tests is a reject.
5. **Q6 expiry awareness**: no quoting inside the settlement-convergence
   window; `close_time` read and enforced in the generator's time dynamics.
6. **Q7 MVE/combo filter** applies to every candidate universe used in
   calibration/backtests (research filter; storage is full-L1 per W-A5).
7. **Recv decision clock** (W-TL1): see the law in the header block.
8. **Inventory is event risk** (Q8): the generator must never logically
   suppress the exit quote at max inventory; caps are enforced between book
   updates too. (The five-layer reservation LEDGER itself is
   PLAN_RISK_KILLSWITCH W-K3 — this plan only consumes its semantics.)

## 2. Module architecture (what gets built)

```
tools/pricing/
  __init__.py
  lo.py          W-P1  log-odds core: logit/expit, cent<->lo maps, clamps,
                       fee-aware edge arithmetic (facts-gated)
  fair.py        W-P2  micro-price + taker-flow drift + bracket-sum constraint
  quote.py       W-P3  A-S generator: reservation, delta(t), cap(t),
                       settlement-window stop, jump breaker, cent clamps
tests/test_pricing_lo.py      + sign tests   (W-P1)
tests/test_pricing_fair.py    + sign tests   (W-P2)
tests/test_pricing_quote.py   + sign tests   (W-P3)
tests/test_pricing_pipeline.py golden scenarios (W-P4)
```

Numpy + stdlib only; no network; no DuckDB inside the pure modules (fixtures
feed plain arrays/dicts). Warehouse access only in Group C via
`warehouse.load()` (read-only).

---

## 3. Group M — pure-math skeleton Ws (executable NOW, synthetic only)

### W-P1 — Log-odds core (`tools/pricing/lo.py`)
Purpose:          The numeric foundation everything else imports: logit/expit
                  with [0.01, 0.99] price clips; cents(E4)↔log-odds maps;
                  half-width application in lo-space with cent re-quantization;
                  taker-fee function 0.07·P·(1−P) with the rounding regime
                  READ FROM `config/kalshi_facts.yaml` (current regime there:
                  ceil_to_centicent $0.0001 — NOT legacy cent-ceiling; the
                  yaml is authoritative, code never hardcodes a regime) and
                  maker-fee lookup, both through the kalshi_facts verified
                  gate (fail-closed when unverified in gate mode).
Allowed writes:   `tools/pricing/{__init__.py,lo.py}`;
                  `tests/test_pricing_lo.py`; tools.json entries.
Forbidden writes: pipeline (capture/ingest/export); mm_backtest; config/*.
Acceptance:       hand-computed golden values (e.g. 2c→1c ≈ −0.70 lo shift vs
                  50c→49c ≈ −0.04 — the asymmetry that motivates Q1) assert
                  exactly; round-trip cent→lo→cent identity on all 99 legal
                  cents; fee function reproduces the published schedule rows
                  for at least {1c, 50c, 99c}; gate-mode with a mutated
                  `verified: false` facts file raises (behavior follows the
                  yaml, never hardcoded — same proof shape as WP-06).
                  Q9-sign: the PRE-ROUNDING fee curve is symmetric around
                  50c and → 0 at the extremes (post-rounding fees have a
                  one-tick floor, so the limit test applies before rounding).
Rollback:         revert commit; the package is imported by nothing else yet.
Exit evidence:    commit hash; pytest green; golden table printed in the log.

#### W-P1 RESULT (2026-07-10 — DONE, audited)
- Delivered: `tools/pricing/{__init__.py,lo.py}` + `tests/test_pricing_lo.py`
  (13 tests) + registry entry. Fee formula/rounding NOT reimplemented —
  delegated to `mm_research.trade_fee` (single source of truth, WP-05
  contract); lo.py adds the maker-rate lookup (fail-closed on flat/unknown
  fee_type) and the pre-rounding curve for the Q9 limit tests.
- Golden numbers (printed in session log): 2c→1c = ln(49/99) = −0.70330;
  50c→49c = ln(49/51) = −0.04001; edge/mid asymmetry 17.6×. Fees C=1:
  {1c,50c,99c} → {$0.0007, $0.0175, $0.0007}.
- Conservatism rule locked: off-grid quantization floors bids / ceils asks
  (float fuzz can only widen a quote, never tighten it).
- Session riders (operator ruling ③, same change): check_registry gained the
  disk→registry REVERSE SCAN (red-first: it flagged 4 real unregistered
  scripts on its first run — rtt_baseline_sampler.sh, rotate_metrics.sh,
  dashboard_net.sh, load_db.py — all now registered with safety classes).

### W-P2 — Fair value estimator (`tools/pricing/fair.py`)
Purpose:          fair = f(book, recent trades, sibling legs):
                  (a) micro-price = (bid·ask_qty + ask·bid_qty)/(bid_qty+ask_qty)
                      computed in lo-space per Q1 (price-space micro-price is
                      only the seed; the blend happens in lo);
                  (b) taker-flow drift term: signed recent taker imbalance →
                      bounded short-horizon lo-drift (shape only; COEFFICIENTS
                      are Group-C outputs, shipped as named placeholders);
                  (c) bracket-sum constraint: same-event legs' yes-sum ≈ 1;
                      deviation redistributes correction across legs, weighted
                      by each leg's quoted depth (thin legs move more);
                  (d) a named `external_anchor_lo=None` input slot — inert in
                      this plan (anchor feed is deferred, §5), reserved so the
                      Crypto spot-index term bolts on additively later.
Allowed writes:   `tools/pricing/fair.py`; `tests/test_pricing_fair.py`;
                  tools.json.
Forbidden writes: as W-P1; also `tools/pricing/lo.py` (frozen by W-P1 unless
                  a defect is filed in the W's log).
Acceptance:       synthetic books with hand-computed micro-prices (incl. the
                  degenerate one-sided and empty-book cases → fail-closed
                  None, never a fabricated fair); drift term: buy-heavy tape
                  ⇒ fair above micro, sell-heavy ⇒ below, zero-imbalance ⇒
                  exactly micro (Q9-sign trio); bracket fixture: 3-leg event
                  summing to 1.08 ⇒ every leg's correction is downward, total
                  correction ≈ the excess, thin leg moves most (Q9-sign);
                  a lone leg (no siblings) gets zero bracket correction.
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green; the three sign-test names listed.

### W-P3 — A-S quote generator (`tools/pricing/quote.py`)
Purpose:          quotes = g(fair_lo, inventory, t_remaining, market state):
                  reservation_lo = fair_lo − inventory·γ(t) (γ risk coeff);
                  half-width δ_lo = f(vol_lo, toxicity_lo, min-tick, fee=via
                  W-P1); time dynamics: δ(t) widens toward settlement, cap(t)
                  shrinks toward zero, HARD STOP inside the Q6 convergence
                  window (close_time consumed here); jump breaker triggers on
                  lo-mid velocity + book-update rate (never trade volume) ⇒
                  action = pull BOTH sides (defense only — momentum-taking is
                  a different strategy, forbidden here per roadmap);
                  emission = clamp(reservation ± δ) to legal cents, one-sided
                  suppression by inventory cap, exit quote NEVER suppressed
                  at max inventory (Q8).
Allowed writes:   `tools/pricing/quote.py`; `tests/test_pricing_quote.py`;
                  tools.json.
Forbidden writes: as W-P2; also `tools/pricing/fair.py`.
Acceptance:       Q9 sign battery, each a named test:
                  (1) long inventory ⇒ reservation strictly below fair, short
                      ⇒ above, magnitude monotone in |inventory|;
                  (2) δ widens with |inventory| growth of the ASYMMETRY —
                      i.e. spread never narrows as risk grows;
                  (3) skew sign correct at both price extremes (2c and 98c —
                      lo-space keeps the 1c step meaningful);
                  (4) δ(t2) ≥ δ(t1) for t2 closer to settlement, quotes = None
                      inside the Q6 window;
                  (5) cap(t) monotone nonincreasing to zero;
                  (6) jump fixture (BADAMS-shaped: fast lo-mid move on zero
                      trades) trips the breaker and pulls both sides; the
                      same tape with trades-only volume spike does NOT trip
                      the velocity breaker (proves it watches quotes, not
                      volume);
                  (7) at max long inventory the ask (exit) side is still
                      emitted (Q8 anti-deadlock).
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green; all 7 sign tests named in log.

### W-P4 — Synthetic pipeline golden scenarios
Purpose:          end-to-end fair→quote cycle over hand-built scenario tapes
                  (calm two-sided; buy-pressure trend; bracket dislocation;
                  pre-settlement wind-down; jump event), each with FULLY
                  hand-computed expected quote sequences — the executable spec
                  the Phase-2 C++ port must reproduce number-for-number.
Allowed writes:   `tests/test_pricing_pipeline.py`;
                  `tests/fixtures/pricing_scenarios/` (synthetic JSON tapes);
                  tools.json.
Forbidden writes: `tools/pricing/*` (defects found here go back to the owning
                  W — this W is a consumer, keeping spec and impl separate).
Acceptance:       every scenario's expected quotes committed as fixtures and
                  asserted exactly; one seeded-defect run per scenario class
                  (e.g. price-space micro-price instead of lo-space) proven
                  RED — the suite must be able to fail (D2 mutation-mindset).
                  Seeded defects are TRANSIENT in-test mutations
                  (monkeypatch/uncommitted), never committed edits to
                  `tools/pricing/*` (audit N8 — Forbidden-writes intact).
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green incl. the red-proof run output.

Group-M ordering: W-P1 → W-P2 → W-P3 → W-P4 (each imports the previous; one W
per fresh session; independent audit after each, MASTER_SEQUENCE rule).

---

## 4. Group C — calibration & validation Ws (GATED)

> **GATE BOX (all Group-C Ws):**
> 1. **Seven clean days** (H-3 clean-day clock, day 1 = 2026-07-06):
>    earliest 2026-07-13, verified by the gate calculator / capture_gaps —
>    not by the calendar.
> 2. **Decision clock = recv, for EVERY Group-C W without exception**
>    (operator instruction 2026-07-10: 校准类 W 一律使用 clock=recv;
>    audit B1 closed the carve-out this plan first drafted). All
>    calibration inputs and every fill-model / go-no-go run use ladder-era
>    recv-clock data (`mm_backtest --clock recv`, fail-closed;
>    `--allow-missing-recv` FORBIDDEN in anything feeding go/no-go —
>    exchange-clock parameters would launder look-ahead into a formally
>    clean recv-clock backtest). **Honesty note: ladder columns populate
>    only from the EC2 deploy of W-TL1 ingest (2026-07-10/11), so ≥7 days
>    of ladder-era data means Group C realistically starts ≥ ~2026-07-17;
>    the 2026-07-13 label is the clean-days gate, not a promise.
>    OPERATOR RULING (2026-07-10): the pre-ladder shape-only option is
>    **REFUSED**. Calibration is recv-clock only, no exceptions.
>    Exploratory shape analysis on pre-ladder data is permitted ONLY under
>    `sandbox/` (P8 free-fire), and its outputs must never enter any
>    calibration parameter or go/no-go — promotion out of sandbox/ goes
>    through full W discipline.**
> 3. Fee facts ratified (OQ-1) for any gate-mode expectation number.

### W-C1 — λ(δ) fill-intensity calibration
Purpose:          from L1+trades: intensity of being filled vs distance from
                  touch (lo-space bins), pessimistic queue convention (strictly
                  -through only), per category/liquidity tier; output a
                  versioned parameter table `work/mm/lambda_<range>.csv` +
                  the coefficient file the generator's placeholders point at.
Allowed writes:   `tools/mm_lambda.py`; `tests/test_mm_lambda.py` (synthetic
                  fixture tape with hand-computed intensities); `work/mm/*`
                  outputs; tools.json.
Forbidden writes: `tools/pricing/*` semantics; pipeline.
Acceptance:       fixture tape reproduces hand-computed λ table; monotonicity
                  property (deeper ⇒ never MORE fills) on real data or the
                  violation is surfaced loudly, not smoothed (D2); every output
                  row carries sample_count + the clock used.
Rollback:         revert commit; delete derived csvs.
Exit evidence:    commit hash; pytest green; one real-data table with its
                  sample sizes printed.

### W-C2 — Toxicity surface
Purpose:          extend `mm_calibrate` output into the model's toxicity
                  term: post-trade lo-drift at 30s/120s conditioned on
                  category × time-of-day × spread-state; emitted in lo-space
                  (`tox_*_lo` convention already exists).
Allowed writes:   `tools/mm_calibrate.py` (additive columns/buckets only);
                  `tests/test_research_metrics.py` extensions; `work/mm/*`.
Forbidden writes: existing output columns' semantics (additive only, like the
                  warehouse ladder discipline).
Acceptance:       synthetic tape with planted drift reproduces hand-computed
                  toxicity by bucket; bucket with < min-support is emitted as
                  NULL + counted, never extrapolated (D2).
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green; real-data surface printed with
                  support counts.

### W-C3 — Vol & jump-threshold calibration
Purpose:          realized lo-vol by market type (BTC 15min ≫ tennis ≫ econ
                  prints — verify, don't assume) + empirical quote-velocity
                  distribution ⇒ jump-breaker trigger percentile proposal
                  (PROPOSED, operator approves the final trigger).
Allowed writes:   `tools/mm_calibrate.py` (additive); `work/mm/*`; tests.
Forbidden writes: as W-C2.
Acceptance:       planted-jump fixture puts the breaker threshold where
                  hand-math says; BADAMS 2026-07-06 episode replayed from the
                  archive registers as a would-trigger (the reason this
                  breaker exists — regression anchor).
Rollback:         revert commit.
Exit evidence:    commit hash; pytest green; proposal table labeled PROPOSED.

### W-C4 — Modeled-quote backtest vs naive baseline (**the Phase-1.5 exit**)
Purpose:          drive mm_backtest with W-P3 quotes (parameters from
                  W-C1..C3) vs the join-the-touch baseline on identical tapes,
                  `--clock recv`, pessimistic bound; Phase-1.5 exit criterion:
                  modeled quotes ≥ 7 days positive pessimistic expectation AND
                  strictly better than baseline.
Allowed writes:   `tools/mm_model_backtest.py` (a driver composing existing
                  pieces — mm_backtest fill logic is reused, not forked);
                  tests; `work/mm/*` reports.
Forbidden writes: `tools/mm_backtest.py` fill semantics (any change there is
                  its own audited W — the go/no-go instrument must not be
                  modified by the party being graded).
Acceptance:       DEMONSTRATED on the look-ahead fixture first: exchange-clock
                  run refused/labeled, recv-clock run clean (reuses the W-TL1
                  demo contract); then real-data report with per-day
                  pessimistic PnL, baseline comparison, all latency params
                  from `config/backtest_latency.yaml` cited (PLACEHOLDER
                  status printed until the measurement task lands).
Rollback:         revert commit; reports are derived files.
Exit evidence:    commit hash; pytest green; the go/no-go table (or the
                  honest "gate not yet satisfied" statement with dates).

Group-C ordering: W-C1 → W-C2 → W-C3 → W-C4, strictly (audit N3: W-C2 and
W-C3 both touch `mm_calibrate.py`, so they are sequenced, never concurrent;
W-C4 last, consumes all three).

---

## 5. Queue position & dependencies

- **OPERATOR SEQUENCING RULING (2026-07-10, recorded in the MASTER_SEQUENCE
  amendment of the same date):** Group M starts ahead of the post-gate
  default — one W per fresh session, strictly W-P1 → W-P2 → W-P3 → W-P4,
  independent audit after each. PLAN_RISK_KILLSWITCH W-K1..K5 interleave
  AFTER Group M completes; W-K6 stays operator-scheduled.
- Group C additionally consumes: seven-clean-days gate, ladder-era data
  accumulation, OQ-1 fee ratification, and (for honest latency numbers in
  W-C4's model) the separate signing/RTT p99 measurement task (sampling plan
  → operator first; W-TL1 handoff).
- Phase 2 (C++ port, shadow wiring) starts only after W-C4's exit criterion
  is met — and begins with the World A/B merge per GUARDRAILS Q5, not with
  this Python code.

### Explicitly deferred (recorded so the audit sees intent, not loss)

- **External-reference fair-value anchor** (MM_ROADMAP 1.5-A fourth
  component; the roadmap calls the Crypto spot-index lead "最大的单一优势"):
  BTC/ETH spot-index feed → lo-space anchor term for Crypto markets; Sports
  `game_data` enrichment stays schema-reserved. Deferred because it needs an
  external market-data feed (new dependency class, its own W with operator
  sign-off on the source); the W-P2 fair-value interface reserves a named
  `external_anchor_lo` input slot so bolting it on later is additive
  (audit N1).
- Real p99 latency measurement, hot-path timestamp stamps, C++ port — see
  W-TL1 handoff + DESIGN_HOTPATH §6 (owned elsewhere).

## §6 self-audit (this plan vs GUARDRAILS)

1. Phase/gates (P1, P2): advances Phase 1.5 only; Group C explicitly gated;
   nothing reaches Phase 2+ or live. ✅
2. Live orders (S1–S6): zero live-order surface; everything pure/offline
   research. ✅
3. Log-odds + fees (Q1, Q3): §1.1–1.2 lock both; W-P1 carries the fee gate
   fail-closed proof; price-space micro-price seed is blended in lo (stated).
   ✅
4. Pessimistic bound (Q2): W-C4 defines go/no-go on the pessimistic bound
   with clock=recv; optimistic = diagnosis. ✅
5. WS-path data (Q5): research consumes the warehouse (WS-captured); no
   strategy runs on polled data — no strategy runs at all in this plan. ✅
6. Tests + sign tests (E1, Q9, D4): every W ships tests in-change; Q9
   batteries are named per W; W-P4 adds red-proof mutations. ✅
7. Pipeline continuity (P4): no W touches capture/ingest/export; all reads
   go through `warehouse.load()` read-only with its lock-retry. ✅
8. Reversible/bounded (P3, P6): every W is one commit + derived files;
   rollback stated per W. ✅
9. Docs move with code (E5): tools.json per W; MM_ROADMAP 1.5 status updates
   at W-C4 exit; this plan's placeholders (drift coefficients, breaker
   thresholds) are all NAMED and their owning W stated. ✅
10. Could a green lie (D2): min-support NULLs surfaced (W-C1/C2), red-proof
    seeded defects (W-P4), monotonicity violations surfaced not smoothed
    (W-C1), PLACEHOLDER banners on unmeasured latency params (W-C4), and the
    gate box makes "calibrated on 2026-07-13" impossible to claim without
    ladder-era data actually existing. ✅

Self-audit verdict: PASS with two flagged operator items in §4's gate box:
(i) the go/no-go date mismatch (clean-days 2026-07-13 vs ladder-era data ≥
~2026-07-17) is stated, not hidden; (ii) the recv-clock rule is EXCLUSIVE
for all Group-C Ws per the operator's instruction (independent-audit B1
closed the draft's shape-only carve-out; the operator REFUSED it outright
on 2026-07-10 — sandbox/-only exploration, outputs never enter calibration
or go/no-go; see §4 gate box).
