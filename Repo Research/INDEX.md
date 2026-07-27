# Repo Research — reproduced research, one folder

Everything in here is **outside research re-run on our own data**: a paper (or a published
methodology) whose mechanism we tested against our own tape and settlement truth, with a
verdict. Own-research lines (alpha_sprint hypotheses, RC-x strategy variants, engine work)
do **not** live here — see `docs/MM_MASTER_INDEX.md` for those.

Two rules this folder exists to enforce:

1. **Mechanisms transfer, numbers do not** (memory: `paper-stats-are-priors`,
   `strategy-line-separation`). Every literature threshold gets re-estimated on our own rolling
   window before it is allowed near a quote.
2. **Every reproduction ships as a PDF with graphs** (operator, 2026-07-27). A results markdown
   is not a deliverable. Build it with `tools/research/replication/report.py`, which carries the
   house chrome — validated palette, A4 landscape pages, cover with stat tiles, figure footnotes,
   tables — so every report reads as one document. Set `REPORT_PNG=<dir>` and look at the pages
   before shipping: the palette is validated, the layout is not.

## Completed reproductions

| # | Reproduction | Source | Our window | Verdict | Report PDF |
|---|---|---|---|---|---|
| 1 | **BO2026 paper strategies** — YES-preference, calibration sag, maker P&L frequency/magnitude split, four-cell ledger, VPIN cliff | Bartlett & O'Hara 2026, SSRN 6615739 (`/Applications/ssrn-6615739.pdf`) | 11 days, 88.6M prints, 153k settled markets (closed 2026-07-20) | **Stage-1 as-is reproduction PASS** — every metric reproduced on our data; magnitudes differ from the paper (tax/toxicity 2.59× vs their 1.52×), VPIN cliff single-name only | [8 pp](BO2026_PAPER_STRATEGIES_20260720/BO2026_STAGE1_REPRODUCTION.pdf) · [folder](BO2026_PAPER_STRATEGIES_20260720/) |
| 2 | **Favourite–longshot bias, stratified** — is the "longshots overpriced" regularity real on Kalshi, per family, per price bucket, per time-to-close | Snowberg–Wolfers FLB literature via GS24 survey (`docs/PAPER_GS24_ALGO_SPORTS.md`) | 16 sealed days (07-10..25), 111.7M settled prints, 613 cells | **Bias exists but is not one phenomenon** — 133/613 cells beat two-sided cost; six families have six different shapes; econ/politics failures are directional YES-optimism, not FLB | [6 pp figures](FLB_STRATIFIED_20260727/FLB_FIGURES_2026-07-27.pdf) + [prose report](FLB_STRATIFIED_20260727/REPORT_FLB_分层测量结果_2026-07-27.pdf) · [folder](FLB_STRATIFIED_20260727/) |
| 3 | **Sports maker replay v0** — pre-game/in-game split, four-line worst-case bill, queue reality for the FLB cells | follow-on to #2 (operator instruction) | same 16 sealed days | **Only the favourite end survives, and only as an upper bound** — fade-favourites on props +$4,875/16d with worst single fill capped −10¢; the longshot "lottery tax" evaporates once the whole in-game phase is aggregated. **Superseded in part by #5**: this replay assumes a free fill at the volume-weighted taker price, which the queue simulation shows is not available | [6 pp](FLB_STRATIFIED_20260727/sports_maker_replay/SPORTS_MAKER_REPLAY_2026-07-27.pdf) · [folder](FLB_STRATIFIED_20260727/sports_maker_replay/) |
| 4 | **Who Profits from Prediction?** — execution vs forecasting, benchmark contamination, lifecycle timing, accuracy × price-distance | Della Vedova 2026-06-30 (`/Applications/Research Ritch/Who profits in prediction markets .pdf`) | 17 sealed days, 98.5M prints, 124k settled markets, 10.9B contracts | **Mechanism replicates almost digit for digit** (interaction −2.31…−2.89 vs −2.244; contamination −0.178→0.000 reproduced). The accuracy inversion does not: our takers are *less* accurate, not more | [11 pp](WHO_PROFITS_20260727/WHO_PROFITS_ON_KALSHI_2026-07-27.pdf) · [folder](WHO_PROFITS_20260727/) |
| 5 | **Queue-level capture** — does the favourite-fade edge survive real queue position? (event-level L1 replay, 39 fee-netted books, k=1..50 lots) | follow-on to #3 (operator instruction) | same 16 sealed days | **Naive back-of-queue quoting DIES: −$59 (k=1) → −$2,879 (k=50); sweep-moment adverse selection eats the whole edge.** Survivors inside the wreck: pre-game phase (+$66 @k=10), and ~17 books still positive under pessimistic queueing (ATP/Challenger/NBA-Summer/ITF-men +$127-158 each). Next: pre-game-only + deci-cent queue-jump v2, out-of-sample book validation | [5 pp](FLB_STRATIFIED_20260727/queue_sim/QUEUE_CAPTURE_2026-07-27.pdf) · [folder](FLB_STRATIFIED_20260727/queue_sim/) |
| 6 | **Microstructure anatomy (E15)** — spread-vs-price map with tick binding, ten-level depth shape by side and moneyness, family effective spreads against the exchange's own `taker_side`, and the within-market depth-vs-time-to-close panel | Dubach 2026, arXiv:2604.24366v2 (`/Applications/Research Ritch/The Anatomy of a Decentralized Prediction Market…pdf`) | L1+trades 16 sealed days (36k markets, 28.8M prints, 444.8M quote-seconds); L2 clean three days (210 markets, 84.9k book samples) | **Pre-registered, exploratory only.** SF1/SF2/SF5/SF8 reproduce; the shapes do not. The "longshot spread premium" is a division artifact (in cents our curve is an inverted U); depth peaks at **level 2, not level 1** (PM decays monotonically); the book is side-asymmetric (bid side 38.8%); and the within-market panel **answers the question PM explicitly deferred** — depth withdrawal near resolution is a real independent channel (+0.251, CI [0.131, 0.372]) where PM's cross-section collapsed to +0.008 | [13 pp](MICROSTRUCTURE_E15_20260727/MICROSTRUCTURE_E15_2026-07-27.pdf) · [folder](MICROSTRUCTURE_E15_20260727/) |

**#3 and #5 bracket the same trade.** #3 (+$4,875) assumes a fill at the volume-weighted taker
price — an upper bound. #5 (−$465 at k=10) assumes joining the back of the queue — a lower
bound. The edge is real but it belongs to whoever holds queue priority, not to a naive joiner.
The lesson generalises: **a measured edge is not a capturable edge, and the capture simulation
has to run before shadow trading, not after.**

## Shared machinery

All post-07-27 reproductions are built on **`tools/research/replication/`** — data map, sealed-day
list, settlement truth, market taxonomy, cluster bootstrap, and the **real fee schedule**
(`fees.py`, including the ~80 series that charge makers 0.0175·p·(1−p); read it before calling
any edge profitable). Read that package's README before starting a new reproduction; do not
write one-off path-digging scripts.

House rules baked in, from earlier lessons:
- never pool across market families — family is always the first stratification layer
- sample unit for settlement studies is the **market**, not the print (per-print n inflates by tens of times)
- sealed days only; settlement truth = `catalog_normal` finalized yes/no
- heavy passes run on EC2 under `nice -n 19 ionice -c3`, outputs to a scratch dir, never into the warehouse

## What a finished entry must contain

1. **A PDF report with graphs**, built on `replication/report.py`. Shape that works: a cover
   (title, one-line thesis, up to four stat tiles, "what replicates and what does not"), one page
   per finding with the chart and a footnote carrying the definitions and caveats for that chart,
   a numbers table, and a closing method page. Put the paper's own numbers next to ours wherever
   the comparison exists.
2. `README.md` — the paper, what we could and could not reproduce, the honest coverage/join
   account, headline numbers side by side with the paper's, caveats, and the exact re-run command.
3. The machine-readable results (`results.json` / CSVs) the PDF was rendered from, so the report
   can be regenerated without re-running the heavy pass.
4. A pointer to the code under `tools/research/…` — code lives with the toolkit, not in here.

Report generators currently in the toolkit: `who_profits_report.py`, `flb_report.py`,
`sports_maker_replay_report.py`, `bo2026_report.py`, `microstructure_e15_report.py`. Copy the
closest one when starting a new reproduction.

**Writing a Chinese report?** `report.py` now has a CJK mode — `Report(..., cjk=True)`. matplotlib
does not fall through the `font.sans-serif` list per glyph (measured on 3.9.4), and DejaVu carries
no Han glyphs, so without it every Chinese character renders as a box. `wrap()` also counts CJK
glyphs as two display columns and breaks between them, since Chinese has no word spaces.

## Reproductions running elsewhere (not yet folded in)

- **CME CF BRTI index recomputation** — code `tools/research/crypto_mm/brti_replicator{,_validate}.py`,
  methodology sources `docs/research_reports/BRTI_REPLICATION_SOURCES_20260727.md`, evidence dirs
  under `tmp/brti_*_20260726/`. Acceptance is defined (median |err| < $5, p95 < $15 in both calm
  and burst strata) but not yet reported as met — fold in once it is.

## Reading list, not reproductions

Papers read and carded but not re-run on our data: `docs/PAPER_ABF23_DEFI_PM_AMM.md`,
`docs/PAPER_AGHM2026_WHO_WINS.md`, `docs/PAPER_GS24_ALGO_SPORTS.md`, plus
`docs/MM_DOCTRINE_FROM_LITERATURE.md` and `docs/BUILD_PLAN_LIT_TO_MODEL.md`.
The PDF library is `/Applications/Research Ritch/` with the queue in
`KALSHI Research paper Stack.xlsx` and `99 papers.html`.

## Note on paths

`Strat Reports/FLB_20260727` and `Strat Reports/WHO_PROFITS_20260727` are now symlinks into this
folder, so every existing reference in `agents/`, `docs/` and the mailboxes still resolves.
`BO2026_PAPER_STRATEGIES_20260720/workdir` is a symlink to `sandbox/expt_bo2026`, which is a live
12 GB working directory driven by the `com.ritcardo.bo-daily` launchd job — it was deliberately
not moved.
