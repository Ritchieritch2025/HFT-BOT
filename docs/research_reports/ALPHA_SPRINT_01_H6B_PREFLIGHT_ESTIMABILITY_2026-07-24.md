# ALPHA-SPRINT-01 H6b — Pre-flight Estimability Memo

Date: 2026-07-24
Mode: read-only research; no live orders; no cash-PnL claim
Authority: `ALPHA-SPRINT-01 H6b Research Execution File (2026-07-24)`
(supersedes the H6 calibration/extremization preregistration)

## Conclusion (confidence: high on the window fact, medium on the rest)

> **REVISION 2026-07-24 (principal decision):** the sealed §3 14-day
> window requirement is overridden. The runner now uses the entire most
> recent contiguous block ending at the latest available date, split
> TRAIN = first ceil(N/2) days / VALIDATE = remainder, with a hard
> floor of 2 days. Every receipt, seal, and report carries
> `spec_deviations` and `meets_sealed_14_day_design: false` when the
> window is short — a sub-14-day run is a **revised test**, not the
> sealed H6b design, and its results are not comparable to a 7+7 run.
> On today's 8 sealed dates this yields TRAIN 07-10..07-13 /
> VALIDATE 07-14..07-17. Effective-sample and MDE warnings from G0
> apply with full force; a 4-day VALIDATE leg makes a NOT ESTIMABLE
> G0 verdict materially more likely.

The most recent sealed evidence available locally (deep03 full-scope
run receipts, 2026-07-22, `ESTIMABILITY_PREFLIGHT.json`) shows exactly
**8 contiguous sealed dates: 2026-07-10 → 2026-07-17**. The original
execution file required 14 (7 TRAIN + 7 VALIDATE); under the revision
above the runner proceeds on 8 and reports the deviation instead of
halting. `WINDOW_TOO_SHORT` now fires only below 2 contiguous days.

**The gap closes at exactly the boundary.** 2026-07-10 → 2026-07-23 is
14 calendar days. If the stranded publication dates (07-18 … 07-23) are
sealed — the mkstemp-600 publication defect is already an open work
order (`operator__to__build … 20260723T1920`, W-PUB-HEAL A+B deployed,
C queued) — H6b becomes runnable with zero further data acquisition.
Healing the publication chain **is** the data-acquisition request that
§16 says a NOT-ESTIMABLE finding must produce.

## What was verified locally (no W09 access from this session)

| Fact | Source | Status |
|---|---|---|
| 8 contiguous sealed dates 07-10..07-17 | `Deepresearch V3/run_2026-07-22_D3-W2A/ESTIMABILITY_PREFLIGHT.json` | verified |
| Politics categories exist in checkpoint market graph (`Politics__Trump__2026`, `__Congress__`, `__SCOTUS_courts__`, `Politicians__2026`, …) | `RESULTS.json` of same run | verified |
| L2 stage retains top-of-book only (`bid_e4/ask_e4/qty` + `imbalance_depth3` aggregate; no per-level depth) | H4 runner `L2_REQUIRED_COLUMNS`, H4 evidence | verified for the H4-era stages |
| Trade tape has per-trade size + `taker_side` | H4 runner `TRADE_REQUIRED_COLUMNS` | verified |
| Settlement outcomes: **not present** in `l2_replay`/`trades_market` stages | H4 runner stage inventory | inferred — must be re-checked on W09 |
| Politics quote/trade density (G0 cluster counts) | — | **unknown; measurable only on W09** |

## Design consequences already baked into the runner

1. **Depth**: with only displayed top-of-book, "walking L2 for CLIP"
   degrades to the conservative rule *eligible iff displayed top size ≥
   CLIP*, plus the 1-tick adverse haircut both ways. DEPTH_REJECT is
   tallied and reported. The CLIP=100 capacity point will likely be
   mostly DEPTH_REJECT in politics books; that is a finding, not a bug.
2. **H6a-MV**: without settlement outcomes in the checkpoint, the module
   is emitted as first-class `NOT_ESTIMABLE (DATA_UNAVAILABLE)`. If W09
   has a settlement stage the operator can pass `--settlement-json`.
3. **Complement/ladder SCAN checks**: within one Kalshi market, YES/NO
   share a single book, so a binary complement violation is just a
   crossed book. Real §9.3 checks are cross-market and require pair and
   ladder metadata in the universe file; without it they are reported
   `NOT_CONSTRUCTIBLE` (stale-side and any declared pairs still run).
4. **Fees**: no live schedule is fetchable offline; the frozen fallback
   is the official 2026-07-07 quadratic (taker 7 %, maker 1.75 % of
   p(1−p), ceil-to-cent per order) from `pnl_spine/fee_facts.py`. Every
   number is flagged `FEE_MODELED` until an operator supplies the live
   schedule snapshot.

## Deliverables in this commit

- `tools/research/alpha_sprint/h6b_politics_path_study.py` — runner
  (G0 gate, B0/B1/B2 sequential path engine with K_cluster=1, C1/C2
  controls, cluster block bootstrap ×10k, BH q=0.10, SCAN census,
  TRAIN seal hash, §13 receipt, §14 halt receipts).
- `tests/test_alpha_sprint_h6b_politics_path_study.py` — 13 tests, all
  passing: strict `[t−L, t)` look-ahead rule, stop-beats-target,
  non-overlap, DEPTH_REJECT, fee arithmetic, complement census,
  WINDOW_TOO_SHORT halt, contract-only stage, full sealed run.

## Dispatch instructions (W09 operator)

```
# 1. Cheap estimability probe — answers the window + density question
#    without touching the lattice:
/opt/w09/venv/bin/python -I -B tools/research/alpha_sprint/h6b_politics_path_study.py \
  --checkpoint-namespace <deep03-namespace> \
  --output-dir /srv/w09-research/runs/alpha-sprint-h6b-contract-<ts> \
  --universe-json <politics-universe.json> \
  --stage contract

# 2. Full run (only after the probe reports a 14-day window):
#    same command without --stage contract, plus
#    --fee-schedule-json <live-schedule.json> if available.
```

The universe JSON (`markets: {ticker: {category, event_cluster,
ladder?, complement_of?}}`) must be generated from the checkpoint's
market graph with cluster assignment from contract rules, then frozen —
it hashes into the TRAIN seal. Cluster assignment that cannot be
constructed halts the runner (§14).

## Biggest hole

The G0 answer — how many independent politics event clusters a two-week
window actually contains — is unknowable from this machine. If politics
turns out to be a handful of mega-clusters (one Trump cluster absorbing
most tickers under the coarser-grouping default), the MDE will blow
past 3¢ and H6b will be NOT ESTIMABLE even with a perfect 14-day
window. That outcome is reportable and would redirect the sprint toward
SCAN and H6a-MV universes instead.
