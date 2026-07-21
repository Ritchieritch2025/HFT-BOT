# GUARDRAILS — the constitution of this project

This file is the invariant set that carries development to the end goal. Every
plan, every agent session, every commit is audited against it. A plan that
violates a MUST here is rejected regardless of how good it looks. Change this
file only with explicit operator approval, in its own commit, with rationale.

**End goal:** a profitable, low-latency, safety-gated automated market maker on
Kalshi, reached through the phase gates of `docs/MM_ROADMAP.md`.
**Current phase:** 1→1.5 (data accumulating since 2026-07-06; research tools
live; pricing model next; Phase 2 starts with the World A/B merge — see
`docs/ARCHITECTURE_REVIEW_2026-07-06.md`).

---

## 1. Safety invariants (violating any of these is an automatic reject)

- S1 **No live orders without ALL of:** every lifecycle gate green + risk caps
  implemented and tested + kill switch implemented and TESTED FIRST + the
  operator's explicit, per-session confirmation. No agent presses the button.
- S2 **Fail-closed everywhere.** Defaults are the safe mode (`data_collect`,
  `local_mock`). Errors reduce permissions, never expand them (risk snapshot
  fails ⇒ zero new-risk capacity). Ambiguity ⇒ reconcile, never blind-retry.
- S3 **The kill switch is built and rehearsed before the first live order**,
  as a standalone process independent of the strategy (panic CLI: cancel-all →
  verify zero resting → reprice-cross liquidation rounds → report).
- S4 **Credentials never enter the repo.** They live in `~/.kalshi/env.sh`
  (600). No key material in code, logs, docs, or command output.
- S5 **Dashboard/research/ops stay off the trading hot path.** Read-only,
  localhost, live_order tools refused by the console forever.
- S6 **Live-order-capable code paths require:** post-only for maker quotes,
  cancel-on-disconnect or dead-man expiry on every resting order, position/
  notional/day-loss caps checked BEFORE placement, orders through the
  reserve-before-send executor (never raw lane sends).

## 2. Data integrity invariants

- D1 **Raw capture is the source of truth** until archived; archive files are
  write-once and final; staging is rebuildable. Never destroy raw data before
  its retention window; never edit archived files (--force requires a reason).
- D2 **Green dashboards must not lie.** Any bounded/skipped/dropped work must
  be surfaced (counters, WARN logs, status fields). Silent truncation is the
  most dangerous failure class we have (rotation-shard incident, 2026-07-06).
- D3 **Validate every external input at the boundary.** Timestamps normalized
  + plausibility-windowed; tickers shape-checked; corrupt frames dropped and
  counted, never staged, never crashing the daemon (splice incident).
- D4 **Every capture-side behavior change (naming, rotation, format) ships
  with an ingest-side test in the same change.**
- D5 **No lossy narrowing of market data.** E4 fixed-point stays (sub-penny is
  real: 13.4% of trades). Never floats on accounting paths.
- D6 **One writer per DuckDB file**; writers hold the lock only inside a
  processing window; readers retry. The trading path never depends on DuckDB.

## 3. Quant decision anchors (learned, verified, not to be relitigated)

- Q1 **Skews/spreads/vol/toxicity are computed in log-odds space**, mapped to
  cents at the edge. Price-space math on probability contracts is a reject.
- Q2 **The pessimistic fill bound is the go/no-go metric.** Optimistic numbers
  are for diagnosis only. Backtests without a queue-conservative bound reject.
- Q3 **Fees are in every model**: taker ≈ 0.07·P·(1−P) per contract rounded
  up; maker fees exist on designated series and must be looked up, not assumed
  zero.
- Q4 **Parameters come from our own calibration** (mm_calibrate on our
  warehouse), not from literature defaults or vibes. Literature supplies
  structure; our data supplies numbers.
- Q5 **No strategy runs on polled/stale market data.** Phase 2 begins with the
  World A/B merge: WS full-depth book drives execution. A maker on the REST
  poll feed is a reject (rodlaf bleed-mode #1).
- Q6 **Expiry awareness is mandatory**: no quoting inside a market's
  settlement-convergence window; `close_time` is read and enforced.
- Q7 **MVE/combo markets are filtered defense-in-depth** (KXMVE prefix,
  mve_collection_ticker, mve_selected_legs, strike_type=functional) — the
  API-side filter alone is insufficient.
- Q8 **Inventory is event risk, not diffusion risk.** Caps are hard, enforced
  between book updates too (multi-fill window bug class), and the exit quote
  must never be logically suppressed at max inventory (rodlaf deadlock).
- Q9 **Economic-sign tests are mandatory for strategy math**: long inventory ⇒
  reservation below fair; spread widens with |inventory|; skew sign correct at
  both price extremes. A strategy change without them is a reject.

## 4. Engineering discipline

- E1 **Tests are the contract.** `make check` + `tests/run_pipeline.sh` green
  before any task is "done". New behavior ⇒ new test in the same change.
  Incidents ⇒ regression test that would have caught them.
- E2 **No decision exists until it's in a file** (doc, test, or config).
  Session memory is not state. Rationale for locked choices lives next to the
  choice (docstrings/docs), not in chat history.
- E3 **The tool registry (`tools.json`) is complete and safety-classed**;
  anything runnable has a safety class; live_order class is console-forbidden;
  `check_registry` must pass.
- E4 **Correct API usage outranks convenience.** Field names/semantics are
  verified against live captures or official docs (spec-drift watcher stays
  green). Legacy fallbacks are kept only with scale-correct conversion.
- E5 **Docs move with the code in the same change** (schema doc, runbook,
  roadmap phase pointer). Doc drift is a defect.
- E6 **Long-running services are supervised** (launchd/watchdogs, single-
  instance locks, background QoS). Ad-hoc daemons are not left running.
- E7 **Hot path is C++; Python is research-only.** Nothing on the order path
  allocates unboundedly, blocks on I/O it doesn't own, or parses JSON it
  doesn't have to.

## 5. Process rules for agents & plans

- P1 **Read this file and `docs/MM_ROADMAP.md` before planning.** A plan must
  state which phase it advances and which gate it satisfies.
- P2 **Phase gates are sequential.** A plan that reaches for live trading
  while Phase 2/3 exit criteria are unmet is rejected, whatever its upside.
- P3 **Scope is bounded per plan**: concrete deliverables, acceptance that is
  *demonstrated* (run, not described), and a rollback story.
- P4 **Every plan names its risks** to running production (the 24/7 pipeline
  is live revenue-critical infrastructure — nothing may silently break
  capture, ingest, or export; if a change touches them, it says how capture
  continuity is preserved).
- P5 **External code/ideas get the adversarial treatment** (as rodlaf's repo
  did): audit in an isolated context, extract steal/avoid lists, port with
  tests — never wholesale adoption.
- P6 **Destructive operations are reversible by default** (attic, not rm;
  purge with counts logged; --force flags require stated reason).
- P7 **Reads are free** (operator-approved 2026-07-08): any session/W may
  read any repo or work/ file without enumeration; only WRITES are
  enumerated per W. Rationale: per-W read lists produced documentation bugs
  (the W-D0 audit's A1 contradiction) and friction, while reading is
  harmless — credentials never live in the repo (S4).
- P8 **sandbox/ is a free-fire zone** (operator-approved 2026-07-08): work
  under sandbox/ needs no W definition, no independent audit, and only a
  one-line SESSION_LOG note. Hard line: production code must never import
  or depend on anything in sandbox/; promotion out of sandbox/ goes through
  full W discipline. Rationale: cheap, reversible mistakes deserve freedom;
  rigor is reserved for the irreversible.
- P9 **Paper-only Ws may share a session** (operator-approved 2026-07-08):
  Ws that change only docs/plans/design (no code, no config behavior) may
  be batched in one session with one combined independent audit at the end.
  The audit itself is never waived — it has caught real defects even on
  pure paper (W-D0: 11 confirmed findings).

## 6. Plan-audit checklist (how plans are reviewed against this file)

Reject if any answer is "no":
1. Does it advance a named phase without skipping gates? (P1, P2)
2. Does anything touch live orders? If yes: S1–S6 all satisfied?
3. Is all strategy math in log-odds with fees included? (Q1, Q3)
4. Is validation demonstrated with a pessimistic bound? (Q2)
5. Does market data for trading come from the WS path? (Q5)
6. Are new behaviors covered by tests incl. economic-sign tests? (E1, Q9)
7. Does it keep the pipeline running and say how? (P4)
8. Are its destructive steps reversible and its scope bounded? (P3, P6)
9. Does it update the docs it invalidates? (E5)
10. Could any part of it lie with a green status? (D2)
