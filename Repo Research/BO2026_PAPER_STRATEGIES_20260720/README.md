# BO2026 paper strategies — as-is reproduction, closed 2026-07-20

**Source**: Bartlett & O'Hara 2026, SSRN 6615739 (`/Applications/ssrn-6615739.pdf`, 71pp).
Their sample is the full Kalshi history 2021-07 → 2026-03; ours is our own tape.

**Verdict**: Stage 1 ("原样复现" — reproduce as-is) **PASS, closed 2026-07-20**. Every metric in
the paper reproduced on our own data. The magnitudes did not transfer, which is the whole point
of running it.

## The report

**`BO2026_STAGE1_REPRODUCTION.pdf`** (8 pp) — the YES-preference gap against the paper's anchor,
calibration deviation by group, the frequency/magnitude split with its day-by-day instability,
the four-cell tax ledger plus the sports-book table the paper does not have, the VPIN quintiles,
and the parameter rule this experiment produced. Rendered from the experiment's own
`charts_data.json` + `bo_state.json` by
`tools/research/replication/experiments/bo2026_report.py`:

```bash
python3 tools/research/replication/experiments/bo2026_report.py \
        sandbox/expt_bo2026 "Repo Research/BO2026_PAPER_STRATEGIES_20260720/BO2026_STAGE1_REPRODUCTION.pdf"
```

Note the state file keeps rolling (the launchd job updates it daily), so a re-render will move
the right-hand panels on pages 4 and 7. The stage-1 window itself is closed.

## Where the work lives

`workdir/` is a **symlink to `sandbox/expt_bo2026`** — a live 12 GB working directory with a
running `com.ritcardo.bo-daily` launchd job, referenced by `dashboard_server.py`. It was
deliberately not moved into this folder. Read it there:

| file | what |
|---|---|
| `workdir/MASTER.md` | the single entry point — four phases, current state, what closed when |
| `workdir/RESULTS_V2.md` | the 11-day results tables |
| `workdir/CONCLUSIONS.md` | H1–H5 verdicts vs the paper's anchors (07-19, 2-day window) |
| `workdir/PAPER_EDGE_MAP.md` | every technical edge in the paper → is it testable live, what do we still lack |
| `workdir/STRATEGY_SPECS.md`, `STRATEGY_CODEBOOK.md` | BO-x (paper as-is) vs RC-x (our variants) line registry |
| `workdir/ARCHIVE.md` | full appendix index |

## What reproduced (11 days, 88.6M prints, 153k settled markets)

| metric | our result |
|---|---|
| calibration curve | single-name and sports sag in the middle; broad-based hugs the line |
| maker win rate, frequency/magnitude split (their eq. 7) | single-name 71.6% = +10.01 − 9.45 |
| anti-informedness gap | single-name green all 12 days, +9~19pp |
| log-odds λ and its directional decomposition | single-name λC:λW ≈ 90:1; γ₁↔λ correlation 0.917 |
| VPIN quintile → next-bucket P&L | **single-name only** (Q5 median −$101.5); the sports/broad-based null also reproduces |
| per category/series grouping | series-level tax table (World Cup props gap +75pp, UFC +7.9¢ × 187M contracts …) |

Earlier 2-day pass (07-19, `CONCLUSIONS.md`) on 9.53M prints: H1 PASS **stronger than the paper**
(single-name gap +41pp vs their +28pp), H2 partial, H3 PASS with magnitude discounted
(+0.86¢/contract = frequency +8.28 − magnitude 7.42 vs their +1.91 = 5.31 − 3.40 — same
structure), H4 PASS sharper (NO-settling ratio 2.25× / YES-settling 0.48× vs their 1.52/0.80),
H5 direction consistent but underpowered (~47 buckets per quintile).

Beyond the paper: **sports behaved like a super-single-name** in our window — taker YES 75.0% vs
settle YES 39.7% (gap +35pp), tax/adverse-flow ratio 3.07× (highest in the sample), maker P&L
+1.22¢/contract. The paper excludes sports from its main analysis.

## The parameter rule this experiment established

Recorded by the operator on 2026-07-20 and now house doctrine (memory: `paper-stats-are-priors`):

> **The paper's mechanisms transfer; its numbers do not.**

Every numeric threshold in the paper (VPIN cliff 0.9, margin m = 3¢, tax/toxicity 1.2/1.0,
top-20% λ, frequency/magnitude 1.5×) is treated as an **initial prior only**, re-estimated on our
own rolling window before go-live and rolled forward after. A shadow test asks "does this
mechanism still exist in our window, and what is it worth now" — not "is the paper's number
right". If the mechanism itself disappears (calibration curve flattens onto the 45° line), the
line is shut down rather than re-tuned. Evidence for the rule: their tax/toxicity 1.52× vs our
2.59×, and their frequency edge still rising into 2026 while ours went negative 07-15..18.
