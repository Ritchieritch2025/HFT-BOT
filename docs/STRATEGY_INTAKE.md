# STRATEGY_INTAKE — how a new research paper becomes a tested strategy line

Purpose: broad-test many paper ideas fast, without polluting the lines already
running. One paper can spawn several lines; every line walks the same ladder,
and dies cheaply at the earliest rung it fails.

## Naming

- Each **paper** gets a code from author initials + year: Bartlett & O'Hara
  2026 = `BO`; a hypothetical Smith & Lee 2024 would be `SL24`. Lines from that
  paper are `BO-1`, `SL24-1`, `SL24-2`, …
- Our **own variants** stay `RC-x`, even when inspired by a paper (RC-4 is the
  banded variant of BO-1). Separate ledgers per line, always — per-line P&L
  attribution is the whole point.
- Paper numbers (thresholds, bands, edge sizes) are **priors, not truth**.
  Every threshold gets re-estimated on our own tape before it gates money.

## The ladder

Each rung is cheap; most ideas should die on rung 1-2 with numbers attached.

### 0. Paper card (10 minutes, doc only)
One paragraph in the paper's doc under `docs/`: the mechanism (why the edge
exists, who is on the losing side), which Kalshi categories it maps to, and
the paper's claimed numbers labeled as priors.

### 1. Screening sketch (minutes)
~30-line class in `sandbox/expt_bo2026/watchtower/strategies_lib.py`
(subclass `Strategy`, set `universe_sql`, implement `on_trade`). Then:

    cd sandbox/expt_bo2026
    python3 -m watchtower.replay strategies_lib.YourClass

Full 13-day tape (2026-07-07..07-20, 95.6M trades, 145k finalized
settlements) scores in seconds; the verdict prints win/loss/net-¢-per-order
by price band. **Kill here** if the idea is negative in its own claimed band.

### 2. Threshold re-estimation (hour)
Sweep the sketch's parameters in replay (constructor args make this a loop).
Keep the band/threshold map next to the paper card. This is where paper
priors become our numbers.

### 3. Live-fidelity confirm (hour)
Implement the real shadow class in `watchtower/shadow/<line>.py` following
the house discipline: pessimistic fills (makers fill only on strict
trade-through; takers pay +1c slippage), phrase-risk universe, VPIN retreat.
Add an adapter in `watchtower/live_replay.py` and replay again:

    python3 -m watchtower.replay live_replay.YourLineLive

This scores the byte-identical code that will run live. The gap between rung
1 and rung 3 numbers = what the risk gates are worth (on the current tape,
they moved BO-1 by ~$900).

### 4. Live shadow (days)
Wire the class into `watchtower/daemon.py` with its own NDJSON ledger +
`strategies.json` entry. Run >= 5 days against the live feed. Replay says
"the edge existed"; shadow says "we can catch it at today's latency/universe".

### 5. Micro-live
Only after rung 4 stays positive and both gauges are green. Makers are
blocked on WO-B (risk/killswitch) + WO-C (order submitter); takers can use
the micro-live 4-piece kit (<=100/order, <=20 orders/day, <=$200 collateral,
kill-file).

## Default gates between rungs (defaults to ratify, not law)

- 1 -> 2: net > 0 in the claimed band with >= 200 settled orders
- 2 -> 3: a band/threshold exists where net >= +2c/contract after fees
- 3 -> 4: live-fidelity net >= 0 overall AND positive on a majority of tape days
- 4 -> 5: shadow >= 5 days, positive cum P&L, no gate breaches

## Tape facts (as of 2026-07-21)

- `expt.duckdb::trades_all`: 2026-07-07 20:10 -> 07-20 20:00 ET, 95.6M trades
  (Mentions+Companies slice = 1.04M). Warehouse CSVs under `ec2_warehouse/`
  extend daily; refresh trades_all before big runs.
- `expt.duckdb::settlements`: 145,597 finalized binaries. Unsettled fills are
  reported in the 未结算 column — treat lines with many unsettled as unripe.
- Fees in replay: taker 0.07*P*(1-P) approximation; C++ cost model is
  authoritative for live billing. Maker fee 0 on these series.
