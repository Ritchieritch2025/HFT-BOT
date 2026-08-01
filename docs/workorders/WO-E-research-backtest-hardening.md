# WO-E — Research backtest hardening: W-FS1 fill sim, fees, latency, settlement

Branch: new branch off the current `w-a-seal-staging-loop` tip (worktree
`sandbox/wa-dev`) — coordinate with the seal-loop agent before rebasing anything.
Python only; no C++ changes. This is deep03's **D3-W4 / W-FS1** prerequisite: until
it exists, no `BACKTEST_CANDIDATE` finding may legally be emitted (deep03 plan
line ~297), and several strategy families sit `BLOCKED_DEPENDENCY` on it.

Read first: `sandbox/wa-dev/tools/mm_backtest.py` (356 lines — the honest Phase-1
baseline; keep its recv-clock fail-closed discipline exactly), `sandbox/wa-dev/docs/
{RESEARCH_METRICS_BRIEF.md,GUARDRAILS.md,MM_ROADMAP.md,warehouse_schema.md}`,
`config/backtest_latency.yaml` (all three p99s are PLACEHOLDERS), deep03 plan
`DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md` (W-FS1 spec, C-01/C-02
fee findings, C-07 mm_sandbox over-fill bug), and audit notes N3/N4 in mm_backtest.

## Context — audit deltas (2026-07-18)

- Fees are entirely unmodeled, and MM_ROADMAP's "maker 0 fee" is stale/superseded:
  designated Sports series DO charge maker fees; "UNKNOWN fee = economically
  ineligible" (deep03 C-01/C-02).
- `mm_sandbox.py` over-fills (a through print fills the whole remaining order —
  C-07). Do not inherit its fill logic.
- Settlement is mark-to-last-mid only; deep03 confirmation requires
  settlement-complete simulated fills = 100%.
- Pre-W-TL1 rows (before 2026-07-10) have NULL recv timestamps → recv-clock
  backtests fail closed on them. W-TL2 backfill is out of scope here; just report
  the eligible window honestly.
- No market-wide L2 exists (`orderbooks_full` is watchlist-only) — queue modeling
  stays bounded (optimistic/pessimistic), not empirical. Do not block on L2.

## Tasks

1. **`tools/fill_sim.py` (W-FS1 core)** — a library, consumed by `mm_backtest.py`
   and future deep03 runners:
   - Order lifecycle: place, cancel, cancel-pending race window (order live until
     cancel-ack time = t + rtt), requote = cancel+place (no free amend assumption).
   - Fill rules: pessimistic = strictly-through only; optimistic = at-price;
     **partial fills capped by printed public size** (spec FS-3/FS-4: public 3 /
     remaining 10 → max partial 3); resting order persists with remaining size.
   - Deterministic given (tape, params, seed); pure functions where possible.
2. **`tools/fees.py`** — series/date-aware maker/taker fee table with explicit
   provenance per entry (source URL/doc + fetched-at). Fail closed: a market whose
   fee schedule is UNKNOWN is marked economically ineligible, never assumed free.
   Rounding to the cent per Kalshi rules; unit tests against hand-computed cases.
3. **Settlement ledger**: join warehouse settlements (csv.gz archive layer) so
   every simulated position reaches a terminal state (settled win/loss or forced
   exit at last tradable price, labeled). Headline PnL = fee-after, settlement-
   complete; mark-to-mid allowed only as a labeled diagnostic.
4. **Latency calibration**: replace the three placeholder p99s with measured
   values. Sources: LATENCY_FACTS.md (sign p99 453 µs; W09 ~6 ms path) + a small
   measurement script deriving signed-post RTT from existing request-telemetry
   NDJSON on the EC2 box (read-only). If a number cannot be measured yet, keep a
   placeholder but make the sim print a `CALIBRATION: PLACEHOLDER` banner and
   exclude results from go/no-go eligibility. Resolve audit note N4 (downlink-lag
   fill optimism) by adding the lag term to the pessimistic activation time.
5. **Integrate into `mm_backtest.py`**: swap its inline `_fill` for `fill_sim`,
   wire fees + settlement, keep `--clock recv` fail-closed semantics untouched,
   and update the output header: results become eligible for go/no-go ONLY when
   fees=modeled, settlement=complete, latency=measured — the script self-reports
   eligibility.
6. **Golden tests** (`tests/test_fill_sim.py`): the deep03 plan demands golden
   tests as a hard prerequisite — fixture tapes with hand-computed expected fills,
   partials, cancel races, fee-after PnL, settlement outcomes; plus a regression
   fixture reproducing the C-07 over-fill showing it now caps correctly.

## Acceptance gate

Golden tests green; `mm_backtest.py` on the sealed 2026-07-12/13 dates produces a
fee-after pessimistic PnL table with an eligibility stamp; the C-07 bug is
demonstrably fixed; recv-clock fail-closed behavior unchanged (test_backtest_clock
still passes). Deliverable doc: one-page `docs/FILL_SIM_V1.md` in the research tree
stating model scope + known bounds, so deep03 can cite it and unblock its
`BLOCKED_DEPENDENCY` families.
