# RESEARCH METRICS BRIEF — market-maker monitoring, by lifecycle phase

For the operator's research day. Purpose: a decision-driven menu of the metrics a
professional MM watches, ordered by WHEN our own lifecycle lets us compute each,
plus the reference frame we're standing on and a two-week research agenda.

**How to read the tags (honesty contract):**
- `[VALIDATED]` — we already compute this on OUR warehouse (tool + output exist).
- `[PARTIAL]` — the method/inputs partly exist; full metric needs a later phase.
- `[LIT]` — literature/industry structure only; we have neither the data nor the
  engine to compute it yet. Structure is real; **no parameter values are invented
  here** (Q4: parameters come only from our own calibration).
- `Q#` = the GUARDRAILS anchor the metric serves.

**Lifecycle phases** (from MM_ROADMAP; the 5th field of every metric = the earliest
phase it becomes computable):
1. **COLLECTION (now)** — data accumulating; only OFFLINE metrics over captured
   trades+L1 are computable (phase 0/1/1.5).
2. **RESEARCH-CALIBRATION (after clean days)** — calibrate params on our data (Q4).
3. **SHADOW (phase 2)** — `KALSHI_MODE=shadow`, real feed drives decisions, ZERO
   orders, decisions+would-be-fills logged (pessimistic, same as backtest).
4. **LIVE (phase 4)** — micro-live real orders.

---

## A. METRICS MENU

Each metric: **Q** (question) · **⚠** (anomaly action) · **formula** · **data** ·
**when/tag/anchor**.

### A1 — computable in COLLECTION (now), offline over captured data

**Adverse selection / markout**  `[VALIDATED]` · Q1 (toxicity)
- **Q:** after we (would) trade, does the mid move against us — are we being picked off?
- **⚠:** a bucket's markout ≥ half-spread ⇒ that market is toxic; drop it from the candidate set (do not quote it live).
- **formula:** for a trade at t, side sign `s`, mid in log-odds `L`: `markout(τ) = s·(L(t+τ) − L(t))`. Adverse = markout > 0 against the resting side. Report a **distribution** per horizon τ∈{…} (never a mean).
- **data:** captured trades + L1 mid series (have it). `mm_calibrate` computes `tox_lo[τ]` exactly this way, and `edge_after_tox = avg_spread/2 − tox_120s`.
- **when:** NOW. Log-odds space (Q1). *Our measured result (ARCHITECTURE_REVIEW): some sports buckets show positive edge-after-toxicity, BTC-15min ≈ 0 — cite the doc, not repeated here as a parameter.*

**Spread capture (gross edge)**  `[VALIDATED]` · Q2/Q3
- **Q:** how many cents of touch-spread is theoretically on offer per bucket?
- **⚠:** gross spread < fee + toxicity ⇒ no edge; deselect.
- **formula:** `avg_spread = time-avg(ask_e4 − bid_e4)`; net edge candidate = `spread/2 − markout(τ) − fee`. Fee per Q3: taker ≈ `ceil(0.07·P·(1−P))` per contract; maker on designated series looked up, never assumed 0.
- **data:** L1 book (have it). `mm_calibrate.avg_spread`. NOW.

**Per-market pessimistic fill bound (go/no-go proxy)**  `[VALIDATED]` · **Q2**
- **Q:** if we only count fills that a trade STRICTLY THROUGH our price would clear (back-of-queue), is the strategy still positive? (The only bound that decides go/no-go.)
- **⚠:** pessimistic PnL ≤ 0 ⇒ reject the bucket, whatever the optimistic number says.
- **formula:** `pessimistic`: fill only if `trade_px < bid` (buy) / `> ask` (sell); `optimistic`: fill at equal price too. Report both side-by-side; **pessimistic is the headline** (Q2).
- **data:** captured trades+L1. `mm_backtest` computes both bounds + per-market fills/spread-capture/end-inventory/PnL. NOW (offline).

### A2 — RESEARCH-CALIBRATION (after clean days)

**Volatility / uncertainty per bucket**  `[VALIDATED, needs clean days for stable params]` · Q1/Q4
- **Q:** how fast does fair value move — how much to widen for?
- **⚠:** realized vol ≫ calibrated ⇒ widen or pull quotes.
- **formula:** `vol_lo = std(Δ mid_lo)` over 1-min windows (log-odds). `mm_calibrate.vol_1min_lo`.
- **data:** clean multi-day L1. Params are OURS (Q4); need clean days for stability.

**Inventory & inventory skew (reservation)**  `[LIT structure; calibrated on our data]` · **Q8**, Q1
- **Q:** given current inventory q, where should the fair/reservation price sit so we lean to flatten?
- **⚠:** |q| rising toward cap while skew not pulling it back ⇒ pricing bug (rodlaf deadlock class).
- **formula (structure, A-S-shaped, adapted):** reservation `r = mid_lo − q·(risk term)`; spread widens with |q| and uncertainty. **Adapted, not copied:** the risk term is **EVENT risk, not diffusion σ²(T−t)** (Q8) — near settlement the diffusion form is structurally wrong (Q6). Params from our calibration (Q4), never literature.
- **data:** needs a pricing skeleton + calibrated vol/tox. Structure buildable now; params after clean days.

**Queue-position value**  `[LIT]` · Q2, Q5
- **Q:** how much is being at the front of the queue worth vs the back (the gap between optimistic and pessimistic fills)?
- **⚠:** value concentrated in front-of-queue ⇒ our pessimistic bound is fragile; require depth/latency edge.
- **formula:** expected fill-rate and adverse-selection conditioned on queue rank; approximated today by the **optimistic−pessimistic gap** in the backtest.
- **data:** full-depth L2 (we capture some; full-depth expansion is a later step) + our resting rank (needs live orders). LIT for now.

### A3 — SHADOW (phase 2: real feed, zero orders)

**Fill ratio (shadow)**  `[LIT]` · Q2, Q5
- **Q:** of our resting quotes, what fraction actually (would) fill, pessimistically?
- **⚠:** shadow fill-ratio ≪ backtest assumption ⇒ our fill model is optimistic; re-derive the bound.
- **formula:** `filled_quotes / resting_quotes` under the pessimistic rule. Same fill judgement as backtest (roadmap phase-2 exit condition).
- **data:** a shadow quoting engine on the WS feed (Q5), zero orders. SHADOW.

**Quote uptime**  `[LIT]` · Q5, Q6
- **Q:** what fraction of quotable time were we actually two-sided in-market (and correctly OUT during settlement-convergence, Q6)?
- **⚠:** uptime low from feed gaps ⇒ this is the capture-hardening link (a holed tape = no quotes); uptime inside a Q6 window ⇒ we quoted when we must not.
- **formula:** `time_two_sided / time_quotable`, excluding Q6 no-quote windows by design.
- **data:** shadow engine + `close_time` enforcement (Q6). SHADOW.

**Quote-to-fill latency budget (decomposition)**  `[PARTIAL]` · Q5, E7
- **Q:** where do the milliseconds go on the path book-update → decision → (would-be) order → ack?
- **⚠:** any leg blows its budget ⇒ we're too slow to hold queue priority; fix that leg.
- **formula:** decompose into legs: feed/ws recv → decode → decision → send → exchange-ack; **distribution (p50/p95/p99) per leg**, never a mean.
- **data:** feed-side legs `[VALIDATED]` (we measure ws freshness/decode); decision→order→ack legs need the engine (SHADOW for internal legs, LIVE for exchange-ack).

**Inventory path & skew (live-shaped)**  `[PARTIAL]` · **Q8**
- **Q:** does inventory stay bounded and mean-revert under our skew, between book updates too?
- **⚠:** inventory breaches cap in a multi-fill window ⇒ the **multi-fill window bug class** (Q8) — caps must be enforced between updates, not only on requote.
- **formula:** track q(t); cap breach = `|q| > max_inv` at ANY fill, not just at requote. Backtest tracks end-inventory + `max_inv`; live path needs the engine.

### A4 — LIVE (phase 4: real orders)

**Fill ratio: real vs shadow**  `[LIT]` · roadmap phase-4 exit
- **Q:** does reality match the shadow prediction (≥70% per roadmap)?
- **⚠:** real ≪ shadow ⇒ queue model wrong → back to shadow (roadmap rollback).
- **data:** real fills vs shadow log. LIVE.

**Risk-cap utilization**  `[LIT]` · S6, Q8
- **Q:** how close are we to position / notional / day-loss caps (checked BEFORE placement)?
- **⚠:** utilization → 100% ⇒ new-risk capacity zero (S2 fail-closed); a snapshot failure ⇒ zero capacity, never expand.
- **formula:** `used / cap` per axis (position, notional, day-loss). Caps hard (Q8), checked pre-placement via the reserve-before-send executor (S6).
- **data:** live risk snapshot + executor state. LIVE.

**Self-fill detection**  `[LIT]` · S6, D2
- **Q:** are our own two-sided quotes crossing/filling each other (wash)?
- **⚠:** any self-fill ⇒ halt that market; it's fee-burn + a correctness bug.
- **formula:** match our resting order ids against our own aggressing fills within a market/window.
- **data:** our order lifecycle (executor). LIVE.

**Per-market post-fee PnL attribution**  `[PARTIAL]` · **Q3**, Q2
- **Q:** after fees and mark-to-settlement, which markets actually made money and why (spread vs inventory vs adverse selection)?
- **⚠:** a market's post-fee PnL negative despite positive gross ⇒ fees/toxicity ate it; deselect.
- **formula:** `pnl_net = spread_capture − fees − adverse_selection − inventory_mark`; fees rounded up per Q3 BEFORE summation; settlement mark from the settlement join. Backtest has per-market gross PnL `[VALIDATED]`; **fees + settlement not yet modeled** (backtest note) → needs the fee model wired + settlement backfill.
- **data:** live fills + fee schedule (Q3) + settlements. PARTIAL now, full at LIVE + backfill.

**Markout on REAL fills**  `[VALIDATED method, LIVE data]` · Q1
- Same markout formula as A1, applied to our actual fills — the truest toxicity read. Method validated offline; needs real fills.

---

## B. REFERENCE FRAME

### B1 — rodlaf autopsy (OUR verified lessons, P5 — cite, don't re-derive)
Already extracted in GUARDRAILS + ARCHITECTURE_REVIEW; the monitoring implications:
- **AVOID — bleed-mode #1:** a maker on a polled/stale REST feed is a reject (Q5).
  → *quote uptime* and *latency budget* exist partly to prove we're on the WS feed.
- **AVOID — max-inventory deadlock:** the exit/flattening quote must NEVER be
  logically suppressed at max inventory (Q8). → *inventory skew* monitoring must
  show the exit side still quoting at the cap.
- **AVOID — multi-fill window bug class:** caps enforced BETWEEN book updates, not
  only on requote (Q8). → *inventory path* metric checks breach at any fill.
- **AVOID — inventory as diffusion risk:** it is EVENT risk (Q8) — the risk term
  in pricing is event/settlement-based, not σ²(T−t).
- **STEAL — deselect-drain lifecycle** + **verified-inventory-not-assumptions**
  (adopted from the post-mortem, ARCHITECTURE_REVIEW): trust measured state, and
  give a deselected market a clean drain path.

### B2 — Avellaneda-Stoikov-class literature (STRUCTURE ONLY, Q4)
Literature supplies the SHAPE, our data supplies every number (Q4):
- Reservation price skews with inventory; optimal spread widens with uncertainty
  and inventory — we keep this shape.
- **Three mandatory adaptations for our world (why we can't copy it):**
  1. Probability contracts ⇒ all skew/vol/toxicity in **log-odds** (Q1), not price.
  2. Inventory is **event risk, not diffusion** (Q8) — the classic σ²(T−t) term is
     structurally wrong near settlement.
  3. **Expiry is a hard boundary** (Q6) — no quoting inside settlement-convergence;
     the "T−t→0" limit is a no-quote zone, not a spread that →0.
- Standard MM monitoring we adopt in shape: markout curves (have), fill-ratio,
  inventory distribution, adverse-selection decomposition, latency budgets.
- **No parameter (γ, k, horizons-as-tuned, thresholds) is taken from literature** —
  all come from `mm_calibrate` on our warehouse (Q4).

---

## C. TWO-WEEK RESEARCH AGENDA (ordered by unlock condition)

### C1 — doable NOW (no new data / no clean-day gate)
- **Event-packaging microstructure study** — using the W-E per-event CSV exports,
  measure spread / markout / volume across a market's life per event & category.
  *Deliverable:* an event-level toxicity+spread report (log-odds), candidate-market
  shortlist by edge-after-toxicity.
- **Pricing skeleton (phase 1.5)** — implement the reservation/skew STRUCTURE in
  log-odds (Q1) with our vol/tox as inputs, plus the **economic-sign tests (Q9)**
  (long inv ⇒ reservation below fair; spread widens with |inv|; skew sign correct
  at both extremes). No live params. *Deliverable:* a pricing module skeleton whose
  Q9 sign-tests pass.
- **Lifecycle curves** — how spread/toxicity/volume evolve open→settlement per
  category, and mark the **Q6 no-quote window** empirically. *Deliverable:* per-
  category lifecycle curves + a proposed settlement-convergence cutoff to enforce.

### C2 — gated on CLEAN DAYS (calibration)
- **Bucket calibration (Q4)** — stable vol_1min_lo / tox_lo[τ] / avg_spread per
  category·subcategory on ≥N clean days. *Deliverable:* our calibration table (the
  numbers), refreshed as clean days accrue.
- **Pessimistic-bound go/no-go (Q2)** — run `mm_backtest` over clean days; which
  buckets clear the pessimistic bound net of fees. *Deliverable:* go/no-go list per
  bucket (the market-selection decision).
  *(Unlock is the capture-hardening + 7-clean-days gate — the dashboard's zone ②.)*

### C3 — gated on HISTORICAL BACKFILL (settlement calibration)
- **Settlement-join research** — mark-to-settlement PnL, settlement-convergence
  dynamics, and **post-fee attribution (Q3)** using settled outcomes. Requires the
  historical backfill + settlements join (STEP 5). *Deliverable:* a settlement-
  calibrated PnL/attribution model + validated fee treatment.

---

*Every metric above is tagged VALIDATED / PARTIAL / LIT; no specific parameter
value is asserted — those live only in `mm_calibrate` output on our own data (Q4).
Sources: GUARDRAILS Q1–Q9, MM_ROADMAP phases, ARCHITECTURE_REVIEW_2026-07-06
(measured edge-after-toxicity), tools/mm_calibrate.py, tools/mm_backtest.py.*
