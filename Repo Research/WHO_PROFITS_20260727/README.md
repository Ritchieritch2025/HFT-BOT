# Who Profits on Kalshi? — replication of Della Vedova (2026)

**Deliverable**: `WHO_PROFITS_ON_KALSHI_2026-07-27.pdf` (11 pages)
**Source paper**: *Who Profits from Prediction? Execution, not Information*, Joshua Della Vedova,
2026-06-30 — `/Applications/Research Ritch/Who profits in prediction markets .pdf`
(222M Polymarket trades; bots at 49.9% accuracy earn +$133M, retail at 51.3% lose $79M).

## What was run

| file | where | what |
|---|---|---|
| `tools/research/replication/experiments/who_profits.py` | EC2 prod (`nice -19`) | one DuckDB pass over 17 sealed days of the full trade tape → `cells.parquet` (1.16M cells, 124k markets, 98.5M prints, 10.9B contracts) |
| `tools/research/replication/experiments/who_profits_analyze.py` | Mac | E1–E9 statistics, cluster bootstrap, OLS → `results.json` |
| `tools/research/replication/experiments/who_profits_report.py` | Mac | `results.json` + `meta.json` → the PDF |
| `tools/research/replication/fees.py` | — | published Kalshi fee schedule incl. the 86-series non-standard multiplier table |

Re-run end to end:

```bash
rsync -az tools/research/replication/ ubuntu@EC2:/home/ubuntu/wp_scratch/replication/
ssh EC2 'cd /home/ubuntu/wp_scratch && nice -n 19 ionice -c3 \
    /home/ubuntu/hft-bot/.venv/bin/python replication/experiments/who_profits.py all'
scp EC2:/home/ubuntu/who_profits_20260727/cells.parquet .
python3 .../who_profits_analyze.py cells.parquet .
WP_PNG=png python3 .../who_profits_report.py results.json out.pdf meta.json
```
Runtime: ~25 min for the EC2 pass, seconds for the rest. Intermediates stay in the scratch
dir; nothing is written into the warehouse.

## Method, in one paragraph

Kalshi's public tape has no trader identity, so the paper's wallet-level classification cannot
be reproduced. What every print does carry is `taker_side`, i.e. the **order role** — which is
the paper's own mechanism margin (its profitable type is 57% maker; its losing type pays the
spread as taker). Each print is joined to settlement truth and decomposed from the taker's
side into `directional = outcome − fair` and `execution = fair − price paid`, under three
reference prices (backward cumulative VWAP, local 50-print VWAP, lagged single print), with
Kalshi's own per-series fees applied to each role. Maker components are the exact mirror.
Families are never pooled; the sample unit for every headline number is the market, with a
1,000-resample cluster bootstrap.

## Headline results

| | result | paper |
|---|---|---|
| value of +10 pts accuracy at 50¢ → 95¢ | 11.8–13.5¢ → 0.5–1.8¢ | 11.6¢ → 1.5¢ |
| interaction coefficient accuracy × price distance | −2.31 to −2.89 | −2.244 |
| cross-skill β, contaminated → clean benchmark | −0.10…−0.34 → −0.000…−0.037 | −0.178 → 0.000 |
| taker net P&L, 15 days, six families | −$235M | −$79M (retail) |
| maker net P&L | +$113M | +$133M (bots) |
| exchange fees | $122M | $0 (Polymarket was fee-free) |

The accuracy inversion does **not** replicate in the paper's form: our takers are less accurate
than makers (42–49% contract-weighted), not more. Kalshi taker flow is tilted into cheap
longshots, so it loses on the side picked as well as on the price paid.

## What this changes for our own book

1. **Crypto 15-min (+0.27¢/ct, CI [0.11, 0.44]) and crypto hourly/daily (+0.47¢, CI [0.25, 0.68])
   are the two fee-free books with a tight, positive, per-market-repeatable maker edge.**
2. **Kalshi charges makers 0.0175·p·(1−p) on ~80 listed series** (all the per-game sports books)
   and nothing elsewhere — `fees.py`. On KXATPMATCH that fee *is* the whole gross edge.
3. The +$99M sports-prop maker profit is 78% concentrated in five series and its per-market
   interval straddles zero — a favourite–longshot windfall, not a strategy.
4. Maker edge decays monotonically into the close and goes negative in the last two minutes.
5. Any execution-quality number we compute against a VWAP built from our own prints carries the
   bias quantified on page 5 of the PDF.

## Caveats

No trader identity → role, not wallet. Settlement snapshot cut at 2026-07-24T21:30Z, so
politics joins at 79% and economics at 60% (selected toward early-resolving markets); crypto
and sports join at 98–100%. Backward-VWAP is rebuilt per day for the four high-volume families
(their markets are intraday). Fees modelled continuously; Kalshi rounds up per order.
Markout uses subsequent trade prints, not quotes.
