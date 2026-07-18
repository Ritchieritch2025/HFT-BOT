# deep03 Granular Strategy & Test Plan — Audit Candidate

> **STATUS: CANDIDATE · AUDIT ONLY · PLAN-ONLY · RISK-0 · NOT EXECUTABLE**
> **NO W09 SPEND · NO HOLDOUT OPENING · NO PRODUCTION MUTATION · NO ORDERS**
> Version: `candidate-0.8` · Date: `2026-07-17`
> Branch observed while drafting: `plan-sports-market-dynamics-v2`
> HEAD observed while revising: `e11d30f3dfb60721eb85f1db51a53e761f03b0a9`
> Canonical term in this document: `deep03` (never bare “V3”; `pub-ref v3` and
> the old Mission Amendment V3 are different objects).
> Supersedes for audit drafting only: `candidate-0.7`, SHA-256
> `e09f99d916ae1c65dffd4cc44566e5cfef5756d52d699df208763de7bf1a04b6`.
> Three independent read-only reviews returned REVISE on those old bytes. They
> remain evidence only and do not approve this revision.

This document is a complete two-lane candidate specification for independent
audit: an open, read-only discovery universe and a separately sealed
confirmation registry. It does not authorize a research run, paid compute,
external data, shadow trading,
micro-live trading, or any order action. After an audit PASS, any change to the
canonical confirmation hypothesis/scope/sample gates first requires a durable
operator ruling that explicitly names the superseded clause. New discovery
ideas may enter only the `OPEN_DISCOVERY_UNIVERSE` rules below and can never
inherit candidate status. Each actual W/phase then requires its own exact-SHA
execution release; one umbrella “deep03 release” is insufficient. A strategy
result never bypasses the operator's final choice.

---

## 0. What the auditor is being asked to decide

Audit whether this plan can, after separately authorized implementation,
answer the following without repeating deep01/deep02's failure mode:

1. Which market states contain a reproducible opportunity?
2. Can a passive order actually be filled under a binding pessimistic model?
3. Does exact fee-after, pathwise, root-event PnL remain positive?
4. Does the result survive latency, cancellation, inventory, terminal-state,
   concentration and tail-risk pressure?
5. Is the opportunity commercially meaningful rather than statistically cute?
6. Did every cell in the frozen all-angle taxonomy receive an explicit tested,
   blocked, deferred, inapplicable or rejected receipt rather than disappear?
7. Which single-name, cross-market, directional, terminal or meta-policy leads
   deserve a newly frozen strategy card on untouched evidence?
8. Which one strategy, if any, deserves an operator-reviewed, translation-
   complete backtest dossier?

The requested audit verdict is one of:

- `PASS_FOR_RELEASE_DRAFTING`;
- `PASS_WITH_EXPLICIT_BLOCKERS`;
- `REVISE`;
- `REJECT`.

The auditor must cite evidence for every finding and must not run research,
open a sealed split, change production, or send orders.

---

## 1. Purpose, phase and non-goals

### 1.1 Phase mapping — no phase advanced

This paper specifies a possible **MM_ROADMAP G0–G5 research design**:

- G0: method, scope and preregistration design;
- G1: data/clock/contract admissibility design;
- G2: market reaction and cancel-race measurement design;
- G3: W-FS1 simulator and strategy-adapter requirements;
- G4: offline TRAIN/VALIDATION candidate design;
- G5: unchanged-policy historical confirmation design.

It satisfies and authorizes none of those gates. Current status advancement is
zero. `OPEN_DISCOVERY_UNIVERSE` may map many directions in parallel using only
`EXPLORATORY_ONLY` evidence. It emits descriptions and proposed hypotheses,
never a candidate. Separately, nested chronological TRAIN freezes at most one
policy per explicitly released confirmation design and all parameters before
VALIDATION. VALIDATION only accepts/rejects that frozen policy; it never selects
among cards/cells. HISTORICAL_CONFIRMATION tests the same unchanged policy under
a separately authorized release. If multiple policies are ever released for a
shared confirmation cohort, K and card-level Holm are frozen first.

### 1.2 Intended output

The terminal output of deep03 is at most:

- one complete `D3-BREADTH_COVERAGE_LEDGER` generated from a frozen taxonomy;
- one corrected market-opportunity atlas plus single-name state-transition
  atlas;
- one feature-admission ledger;
- one lossless hypothesis→experiment→result Telegram research-feed trace,
  with every operator judgment preserved separately from scientific verdicts;
- one fully reproducible dossier per tested strategy family, including every
  rejected/deferred direction and its reason;
- zero or one `BACKTEST_CANDIDATE` finding by default;
- one `TRANSLATION_COMPLETE_SPEC` for every confirmed survivor, with exact
  entry/quote/cancel/exit/size/kill rules, frozen parameters and fee-after
  pessimistic economics, ready for a **future** shadow-engineering review but
  carrying no shadow/micro-live/live authority;
- explicit `REJECT`, `COLLECT_MORE`, or
  `RESEARCH_VALID_NOT_DEPLOYABLE` reasons for every other card.

A VALIDATION pass may emit `EDGE_CANDIDATE_FOR_CONFIRMATION` only.
`BACKTEST_CANDIDATE` is legal only after `CONFIRMATION_COMPLETE` and every
adjusted statistical, control, tail and commercial gate. Confirming more than
one policy requires a separately frozen K/Holm design and explicit release.
The operator, not an agent, chooses whether any candidate is frozen for later
engineering or shadow work.

### 1.3 Non-goals and deferred execution boundaries

- No claim that sports-result prediction or external information is available
  in the current release. Directional/external-information cells stay visible
  in the breadth ledger as `DEFERRED_DATA` or `DEFERRED_AUTHORITY`.
- No score, serve, injury, lineup, courtsiding or external-odds provider call,
  purchase, credential use or dependency without a one-provider release.
- No RFQ PnL or authenticated RFQ action; RFQ remains a read-only/deferred
  research track unless separately authorized.
- No MVE/combo trading. Generic CLOB code keeps excluding it; a future cell
  requires a payout-verified, separately released engine.
- No automatic policy promotion, shadow start, live start or scaling.
- No claim that “one-sided” means “missing market maker.”
- No claim that a post-scheduled-start move was caused by a point or score.
- No use of markout, half-spread or fee-wall proxies as NetPnL.
- No claim that the frozen taxonomy exhausts every imaginable strategy. It
  bounds the auditable search and retains `UNCLASSIFIED` for observations that
  do not fit it.

---

## 2. Authority, evidence labels and unresolved conflicts

### 2.1 Authority precedence

The auditor should apply this precedence:

1. `docs/GUARDRAILS.md`;
2. verbatim durable operator rulings and exact releases in
   `docs/PLAN_SPORTS_TRADING_DECISIONS.md`; a later ruling supersedes only the
   exact scope/ruling IDs it names;
3. the canonicalized Sports Program Prompt V2.2;
4. a future exact-SHA W/phase deep03 release, only within already-authorized
   scope and gates;
5. this exact-SHA candidate, but only after item 4 explicitly adopts it and
   only inside that release's W/phase scope;
6. ordinary plans, design notes, reports and audits not adopted by item 4.

Before adoption this file has no authority. Even after adoption it cannot amend
items 1–4; it may resolve a named conflict with item 6 only inside the adopting
release. In particular, no release may implement the currently conflicted
W-HYPO-FEED-01 bytes by citing their ordinary-plan status.

### 2.2 Labels used below

- `[EXISTING]`: already required by a higher authority.
- `[PROPOSED]`: finite candidate value/rule offered for audit; not yet authority.
- `[TRAIN-FREEZE]`: a finite registry and selection rule are preregistered, but
  the final value is chosen only inside nested chronological TRAIN.
- `[OPERATOR-TBD]`: capital, commercial or scope choice an agent cannot invent.
- `[DIAGNOSTIC]`: may inform design but cannot pass a profit gate.
- `[BLOCKER]`: must be resolved before the affected result can be gate-bearing.
- `[EXPLORATORY_ONLY]`: outcome-visible discovery evidence; permanently barred
  from VALIDATION/HISTORICAL_CONFIRMATION.
- `[DESCRIPTIVE_NOW]`: a read-only mechanism scan estimable with admitted data;
  it cannot emit an economic candidate.
- `[POLICY_LATER]`: a discovery lead requiring a newly frozen card, authority,
  simulator contract and untouched evidence before economic testing.
- `[DEFERRED_DATA]`, `[DEFERRED_AUTHORITY]`, `[NOT_ESTIMABLE]` and
  `[REJECTED_WITH_EVIDENCE]`: explicit coverage outcomes, never omitted cells.

### 2.3 Known cross-document conflicts

| ID | Conflict | Required treatment in deep03 |
|---|---|---|
| C-01 | `DEEP03_SPEC_NOTES` and the D-4 archival aside say “maker 0 fee”; GUARDRAILS Q3 and current fee facts say designated Sports series charge maker fees | Both stale statements are superseded for economics; bind series/date/event/fill formula fee plus verified account precision/rounding/accumulator, UNKNOWN = ineligible |
| C-02 | `MM_ROADMAP` still contains a dynamic-pricing `fees=0` phrase | Do not inherit it; audit should request a later doc correction, outside this plan-only write |
| C-03 | Deep02 promotion language uses ≥20 independent dates/≥200 roots; current provisional program gates also use ≥7 clean test days, ≥5/7 positive days and power | Exploratory work starts immediately; formal confirmation uses the most conservative compatible interpretation until the operator releases a replacement |
| C-04 | Canonical primary is pre-match **two-sided** spread capture; PM-OS extends one-sided, T90/T90-RB are post-start and CS is Track-D directional | PM-TS is the only current confirmatory primary; every other policy stays diagnostic/proposal-only until a durable canonical-tier operator ruling explicitly expands scope, followed by an exact W/phase release |
| C-05 | `occurrence_datetime`, scheduled start, actual start and close time are not interchangeable | Freeze source/as-of semantics; never substitute close time; use `PRE_SCHEDULED_START` / `POST_SCHEDULED_START_PROXY` labels |
| C-06 | Realized outcomes and authoritative payout/void/cancel/retire/postpone rule semantics are different requirements | Every economic card needs complete contract semantics to enumerate legal residual states; PM may avoid the realized outcome label via liquidation + joint worst-state residual, while CS/hold-to-terminal additionally requires 100% realized terminal outcomes |
| C-07 | `mm_sandbox.py` treats a through print as enough to fill the whole remaining order; PLAN_MM FS-3/FS-4 already limit fills by authoritative public size (`public 3 / remaining 10 -> max partial 3`) | Existing authority decides the direction; this plan requires fill quantity ≤ authoritative public sweep/trade evidence and a regression test before G3 |
| C-08 | The candidate-0.3 audit observed fee ratification only in a dirty worktree; revised HEAD contains clean commit `8963cdc`, but later drift remains possible | Bind that commit or a tested successor plus exact facts/config SHA; a dirty override or stale fee fact is ineligible |
| C-09 | 85–99, 87+ and 97–99 all appear as “certainty” ranges | Discovery scans 85–99 continuously; proposed executable family is 97/98, with 99 queue-only diagnostic; audit/operator must ratify |
| C-10 | T90 88–93 overlaps broad certainty bands | Policy families are evaluated separately and never have their PnL/capacity added; any later portfolio must use explicit precedence and a shared root-event cap |
| C-11 | D-5 requires “all angles” and a live-testable cash-strategy shape, while GUARDRAILS.S1/Q2/P2 forbid manufacturing or auto-deploying a winner | Implement a frozen breadth generator plus `TRANSLATION_COMPLETE_SPEC`; zero survivors is legal, and no gate, split, fill bound, end date or authority changes to force a positive result |
| C-12 | Proposed W-HYPO-FEED-01 says replies update a mutable hypothesis ledger and “毙” becomes REJECTED_BY_OPERATOR | Deep03 feed uses a separate append-only judgment journal plus application receipts; “毙” is OPERATOR_DECLINED and never scientific rejection or direct mutation |
| C-13 | The deployed status bot is the sole `/ack` long-poller; a prior duplicate `getUpdates` consumer caused 409 and swallowed an operator message | One locked production-monitor gateway owns the only token, sends cards and routes replies through the same durable-offset status-bot service; Mac/W09 remain credential-free and a second consumer fails deployment |

---

## 3. Seed strategy registry and roles

The following `SEED_REGISTRY_V0` is the vertically specified starting set, not
the allowed strategy universe. It contains **three families**, one currently
in-scope confirmatory policy, four scope-conditional proposal policies and one
non-executable payoff control. Six cards do not mean six independent alphas,
and they do not cap discovery.

| Family | Trial ID | Role | Can become candidate? |
|---|---|---|---|
| PM | `DR3-PM-TS-01` | Primary pre-match two-sided policy | Yes |
| PM | `DR3-PM-OS-01` | One-sided state/policy increment over PM | Diagnostic unless a durable ruling expands the two-sided primary; then absolute + paired gates |
| T90 | `DR3-T90-01` | Tennis 88–93 range policy | Diagnostic unless a durable post-scheduled scope ruling exists |
| T90 | `DR3-T90-RB-01` | Post-retreat rebuild/re-entry increment | Inherits T90 diagnostic lock; then absolute + paired gates |
| CS | `DR3-CS-ASK-01` | Track-D high-price ask / sell-certainty policy | Proposal-only unless durable scope + separate registry/split + terminal authority exist |
| CS | `DR3-CS-BID-CTRL-01` | Non-executable payoff/sign counterfactual on ASK fill set | **No**; control verdict only |

Shared research objects are not independent alpha cards:

| Object | Frozen role |
|---|---|
| `F-RETREAT-01` | `RISK_SAFETY` / cancel-and-re-entry feature |
| `F-OCCUPANCY-01` | `MONITOR_ONLY` until it proves TRAIN-only incremental selection value |
| `F-FLOW-TOX-01` | `EXECUTION` toxicity/no-quote feature in the seed PM card; a separately identified momentum continuation card may still be proposed from open discovery |
| fee sensitivity | Shared accounting/stress dimension |
| blindness cost | External-data EVPI/avoidable-loss analysis, not a strategy |
| cross-market residual | Track-A monitor until payout exhaustiveness and leg execution are proven |

### 3.1 Logical routing (for overlap audit, not simultaneous deployment)

```text
invalid / gap / stale / unknown fee or lifecycle
    -> NO_QUOTE

pre-scheduled-start + valid two-sided
    -> DR3-PM-TS-01

pre-scheduled-start + valid two-sided + high-side ask in {97,98}
    -> overlap: PM-TS and CS-TW both claim this state; run separately,
       enforce one shared root-event cap, never sum PnL/capacity

pre-scheduled-start + valid one-sided
    +-- normalized E4 price [10c,90c)  -> DR3-PM-OS-01
    +-- normalized E4 price [90c,97c)  -> NO_QUOTE (unrouted seed interval)
    +-- normalized E4 price [97c,99c)  -> DR3-CS-ASK-01 / OS proposal
    +-- normalized E4 price [99c,100c) -> queue diagnostic / NO_ORDER
    +-- otherwise                       -> NO_QUOTE

post-scheduled-start proxy + Tennis + leader 88–93c
    +-- no retreat              -> DR3-T90-01
    +-- prior kill, burst, recovered depth -> DR3-T90-RB-01

valid state not classified by SEED_REGISTRY_V0
    -> UNCLASSIFIED_STATE -> DISCOVERY_BACKLOG / NO_ORDER
```

Research runs each family separately. No simulated portfolio may sum their
capacity or PnL without a later, explicit mutual-exclusion and shared-risk
specification.

---

## 4. Prerequisite gates and estimability preflight

### 4.1 Run-level prerequisites

Before any outcome-linked computation:

1. any needed durable scope ruling exists and the exact current W/phase release
   passes the full `AUTHORITY.json` schema;
2. Research reader strict canary passes exact VersionId access;
3. a hash-sealed release inventory and prior-exposure ledger exist;
4. fee facts are in a clean tested commit;
5. the candidate registry, split rule, seeds and code SHA are sealed;
6. RFQ is not a required input;
7. production capture/ingest/export remain untouched;
8. all writes are confined to the authorized run directory and approved docs;
9. cost/time/end-date or sequential stopping rule is frozen;
10. this candidate and any current/internal-data W call no external provider.
    A future external card requires a durable named-supersession ruling that
    explicitly replaces this item for one source, creates a new lineage and
    supplies the §4.5 provider/as-of/license/spend contract; a generic deep03
    release cannot override it.

### 4.2 Card-level estimability preflight

Every card must print this table before its main calculation:

| Count/coverage | Required action |
|---|---|
| eligible dates, root events, markets | Report all three, never only rows/trades |
| treatment/control roots | Either side zero -> `NOT_ESTIMABLE`, stop the card |
| clean receive-clock coverage | Below frozen threshold -> exclude/collect |
| fee-known fills/intents | UNKNOWN never contributes economics |
| payout/lifecycle rule-semantics-complete rows | Required for every economic card; UNKNOWN -> ineligible/red |
| clean L2 episodes | Required for T90/retreat; zero -> stop those tests |
| settlement-complete simulated fills | Required 100% for CS confirmation |
| scheduled-start-known roots | Required for PM gate |
| predicted strict fills | Zero -> no PnL test; report opportunity/fill-model boundary |
| independent day/root blocks | Below the preregistered power requirement -> `COLLECT_MORE`; do not open confirmation. Data may accrue only to the frozen fixed end or under frozen group-sequential alpha spending; opening/stopping/extending never depends on observed significance |

This prevents deep01-style “registered but empty” tests from being presented as
completed strategy research.

### 4.3 Honest current capability by card

This is a capability assessment, not a result. On the observed HEAD, **no card
can yet legally emit `BACKTEST_CANDIDATE`**. Large row counts or daily intake do
not cure an unimplemented execution contract.

| Card | Useful work possible immediately after scope release | Binding blocker before fee-after NetPnL | Highest honest interim state |
|---|---|---|---|
| PM-TS | opportunity map, candidate-fee quote economics, pre-start state/markout and adverse-move atlas | clean net-fee/account facts; payout/lifecycle rule semantics; W-FS1 lifecycle, strict fill, scheduled-start coverage and deterministic liquidation | `BLOCKED_DEPENDENCY` |
| PM-OS | corrected one-sided duration/opportunity after censor fix; onset-anchor coverage | durable one-sided scope ruling; censor regression; strict fill, cancel/timeout and executable exit | `BLOCKED_AUTHORITY` |
| T90 | causal past-only episode count, tug/retreat lead-time and L2 path diagnostics | durable scope, rule semantics, sequence-valid L2 and full order/fill/cancel engine | `BLOCKED_AUTHORITY` (diagnostic evidence only) |
| T90-RB | recovery-state frequency and counterfactual episode diagnostics | durable post-scheduled scope; parent engine; zero-resting reconciliation; re-entry | `BLOCKED_AUTHORITY` |
| CS-ASK | price/regime opportunity and queue-capacity upper bounds only | durable Track-D scope; separate uncontaminated registry/split; terminal outcomes, action mapping and strict fills | `BLOCKED_AUTHORITY` |
| CS-BID | payoff/sign transform can be specified but not called executed | inherits ASK fill-set blockers; no independent capacity/fill claim | `BLOCKED_AUTHORITY` |

The first deep03 implementation receipt must recompute this table. A method
listed in a registration file is not “run”; zero eligible rows, missing input,
or a skipped module must remain visibly unexecuted.

### 4.4 Complete deep03 test catalog

The following catalog is the minimum program. “Pass” means only that the named
test's output is admissible for its next dependency; it is not a strategy
verdict. Each test gets a method ID, implementation entry point, declared and
executed status, input manifest subset, exclusion waterfall and artifact SHA.

#### Layer A — authority, input and causal-clock contract

| ID | Test | Unit/output | Red condition |
|---|---|---|---|
| `D3-A00-AUTH` | durable scope ruling plus exact W/phase release/schema canary | authority chain receipt | umbrella release, unnamed supersession, absent field or mismatched SHA |
| `D3-A01-READ` | strict reader exact-VersionId canary | object read receipt | latest-object fallback, write capability or unsealed key |
| `D3-A02-MANIFEST` | object hash/size/rows/schema inventory | object × release | duplicate/missing object or hash drift |
| `D3-A03-COVERAGE` | date/sport/root/market/trade/L1/L2/lifecycle/settlement coverage | full coverage cube + missingness map | aggregate-only count; unexplained denominator drift |
| `D3-A04-CLOCK` | receive/source clock ordering and future-as-of sentinels | row/episode violations | any decision reads `source_ts > decision_ts` or consumes outcome as a feature |
| `D3-A05-SEQUENCE` | snapshot/delta sequence, gap, reconnect and epoch integrity | market epoch | invalid epoch admitted to L2 economics |
| `D3-A06-IDENTITY` | series/event/root/market/outcome orientation mapping | mapping ledger | linked markets split across roots or unknown side accepted |
| `D3-A07-PHASE` | scheduled-start source/as-of and phase classification | root × market | close time substituted; actual/scheduled silently conflated |
| `D3-A08-FEE` | fee type/rate/multiplier/effective/event override lookup | intended/simulated fill | UNKNOWN or dirty unbound fee fact used in economics |
| `D3-A09-TERMINAL` | payout plus close/determined/settled/void/cancel/retire/postpone rule semantics for all cards; realized outcome coverage for hold-to-terminal | contract/lifecycle path | legal residual state not enumerable, CS outcome incomplete or cash flow twice applied |

#### Layer B — repair and rerun the useful first-generation analyses

| ID | Corrected test | Exact correction | Output/use |
|---|---|---|---|
| `D3-B01-MARKOUT` | trade-side forward markout surface | past-only trade orientation; `decision_ts < outcome_book_ts <= horizon`; non-null `n`; continuous price, sport, phase and liquidity | descriptive adverse-selection surface, never NetPnL |
| `D3-B02-ONESIDE` | one-sided state duration/incidence | end at next update/TTL/gap/pause/terminal/epoch/capture end; last row right-censored | PM-OS opportunity and censor-sensitivity map |
| `D3-B03-XMKT` | linked-market residual | quote-age/as-of alignment, payout exhaustiveness, leg-specific fee and fill feasibility | monitor-only residual; no synthetic arbitrage claim |
| `D3-B04-RHYTHM` | activity rhythm | active market-minute/event denominators, exposure-adjusted counts and phase | capacity/timing strata, not raw UTC volume |
| `D3-B05-FEEWALL` | maker/taker economics by continuous price | net fee by series/date/role/account precision/order accumulator; no maker-zero claim | fee-after quote threshold inputs |
| `D3-B06-REPRO` | reproduce every retained deep02 number | canonical released data, frozen corrected code and delta table | retain/revise/retract ledger for earlier evidence |

#### Layer C — market-mechanism tests before strategy PnL

| ID | Mechanism question | Design | Admission decision |
|---|---|---|---|
| `D3-C01-TOPOLOGY` | Where do valid spreads, depth and two-/one-sided uptime exist? | event-weighted distributions by continuous price × sport × phase × liquidity; zero-activity roots retained | defines only the eligible universe |
| `D3-C02-REACTION` | How quickly does executable price move after public trade/book shocks? | receive-clock event study at frozen horizons with overlapping-event policy and matched quiet anchors | supplies adverse-width/latency budget |
| `D3-C03-CANCELRACE` | Is cancel-effective latency earlier than adverse movement? | empirical lead-time distribution versus measured cancel path p50/p95/p99, including reconnect | reject regimes with no safety headroom |
| `D3-C04-RETREAT` | Does L2 retreat lead bursts/losses and improve a policy when used past-only? | versioned `F-RETREAT-01`, matched roots, balance, false-kill cost, future/shift/reset controls | `RISK_SAFETY`, monitor or reject |
| `D3-C05-REBUILD` | After a completed kill, is recovered widened depth executable before normalization? | zero-resting episodes, burst/recovery path, fill-feasible window and matched no-reentry parent | admits only the T90-RB module |
| `D3-C06-OCCUPANCY` | Does an occupancy fingerprint improve market selection beyond volume/spread/activity? | nested out-of-fold incremental test; delete-best league/event | selector, monitor or reject; never “professional MM” truth label |
| `D3-C07-T90PATH` | Are causal 88–93 leader episodes with prior two-way tug sufficiently frequent and deep? | frozen leader, past-only crossing, event-level paths, niche decomposition | T90 estimability and candidate strata |
| `D3-C08-CSTAIL` | What is settlement loss/upset tail conditional on a strict 85–99 state/fill? | continuous discovery; 97/98 executable cells; terminal-complete roots; ASK/BID separation | CS scope/power input, not short-markout proof |
| `D3-C09-BLIND` | What loss is avoidable by retreat versus an oracle and what is false-pause cost? | observed-signal policy versus future-information upper bound; no provider call | EVPI range and external-data approval case only |
| `D3-C10-HETERO` | Is any effect carried by one date/event/league/series? | full strata, delete-best, HHI/top shares and shrinkage/descriptive intervals | narrow, collect or reject |

#### Layer D — executable replay qualification

| ID | Engine test | Binding assertion |
|---|---|---|
| `D3-D01-REPLAY` | deterministic merged replay | identical ordered events, ledgers and hashes on repeat |
| `D3-D02-ACTION` | outcome/action orientation | all buy/sell × YES/NO golden cases and unknown-side failure |
| `D3-D03-FILL` | strict-through fill | at-price zero; through-only; quantity ≤ authoritative evidence |
| `D3-D04-ORDERFSM` | create/ack/partial/cancel-ACK/effective/replace/unknown/reconnect | no replace before old terminal; ack-before/after and cancel-pending fills modeled |
| `D3-D05-PAIR` | non-atomic two-sided placement | reserve worst sequence; half-active/orphan/cancel-race is explicit |
| `D3-D06-RISK` | per-fill inventory/collateral/write limits | cap checked after every fill; reduce-only never cap-blocked |
| `D3-D07-FEELEDGER` | formula fee + account rounding + order accumulator/rebate | PriceE4/CountE4 positions and MoneyE6-or-wider cash/net-fee/collateral reconcile across partials |
| `D3-D08-LATENCY` | measured placement/cancel/IOC distributions | provenance-bound baseline and required stress matrix |
| `D3-D09-EXIT` | stop, cancel, reconcile, depth-bounded IOC, residual | no midpoint teleport; worst-state/terminal residual explicit |
| `D3-D10-TERMINAL` | settlement/void/retirement/postponement | complete once-only cash flow for hold-to-terminal cards |

#### Layer E — policy, inference and delivery

| ID family | Test | Dependency/output |
|---|---|---|
| `D3-E-PMTS` | PM-TS sequential TRAIN registry, ablations and strict PnL | A–D; absolute root-event estimand |
| `D3-E-PMOS` | PM-OS absolute + paired increment over PM parent | B02 + A–D; intersection-union gate |
| `D3-E-T90` | T90 finite offset/timeout grid + retreat ablation | scope + C04/C07 + A–D |
| `D3-E-T90RB` | parent versus one-cycle rebuild | C05 + parent; absolute + paired increment |
| `D3-E-CSASK` | CS-ASK 97/98 × regime × phase | scope + A09/C08/D10; settlement PnL |
| `D3-E-CSBID` | non-executable BID payoff/sign transform on exact ASK fill IDs | no independent quote/fill/capacity; control verdict only |
| `D3-E-NEG` | deterministic and stochastic negative-control suite | exact sentinels + equivalence results |
| `D3-E-POWER` | event/day blocked power, multiplicity and stopping | frozen K, attrition/zero-fill and rare-tail support |
| `D3-E-BIZ` | capacity/tail/concentration/commercial receipt | one-contract facts; no linear extrapolation |
| `D3-E-RECEIPT` | reproduction, independent recomputation and atomic completion | all hashes + `RUN_COMPLETE`; no stale UI pass |

The dependency order is A → corrected B/mechanism C → engine D → policy E.
B and C can run in parallel after A. Synthetic D engineering can run in
parallel, but no D or E result may borrow unsealed production rows or preempt
its authority gate.

### 4.5 Two-lane research model and containment invariants

deep03 has two logically and statistically separate lanes:

| Lane | May create ideas/features/models? | Evidence class | Legal output | Forbidden output |
|---|---:|---|---|---|
| OPEN_DISCOVERY_UNIVERSE | Yes, within frozen taxonomy and budget | EXPLORATORY_ONLY / PRIOR_EXPOSED | DESCRIPTIVE_FINDING, HYPOTHESIS_PROPOSED, backlog/defer/reject receipt | candidate, confirmatory claim, shadow/live claim |
| CONFIRMATION_REGISTRY | No after TRAIN freeze | chronological TRAIN, one sealed VALIDATION, separately sealed confirmation | §16 verdicts | feature/cell/model/horizon selection on VALIDATION or confirmation |

The following invariants are binding parts of this candidate:

1. **D5-BREADTH-NOT-PROFIT.** D-5 requires complete search receipts, not a
   favorable result. Zero survivors is legal. No threshold, estimand, tail,
   fill authority, split, end date or commercial hurdle changes to manufacture
   a survivor.
2. **DISCOVERY-NOT-AUTHORITY.** Naming a direction authorizes no provider call,
   purchase, authenticated mutation, RFQ action, shadow, micro-live, order or
   scale change.
3. **OPEN-SCAN-EXPLORATORY-ONLY.** Every root/date/artifact exposed by an
   all-angle scan enters the shared prior-exposure ledger and is permanently
   barred from VALIDATION and HISTORICAL_CONFIRMATION.
4. **PROMOTION-REQUIRES-NEW-CARD.** Discovery emits only descriptive findings
   and proposed hypotheses. Promotion requires a frozen card, registry,
   estimand, engine/fill authority, power/stopping rule and untouched evidence.
5. **ONE-POLICY-PER-OPENING.** Default TRAIN freezes at most one policy for one
   validation opening. VALIDATION accepts/rejects it without selection and
   confirmation tests it unchanged. This does not reset super-family-wide
   error.
6. **GLOBAL-SELECTION-LEDGER.** Every feature, threshold, model,
   hyperparameter, sport, phase, horizon, action, router, portfolio rule and
   human/agent choice allowed to win enters one immutable selection ledger.
7. **ONE-CASH-SUPERFAMILY.** Before the first TRAIN freeze, seal
   `super_family_id=DEEP03_CASH_STRATEGY_V1` and its deterministic assignment
   rule SHA. Every hypothesis/card that is or could become a cash-strategy claim
   defaults into this one super-family, across sport, track, parent family,
   lineage, cohort and research generation. Child `program_family_id` is a
   reporting/closed-testing node only; it cannot mint alpha. Only a permanently
   non-promotable control/exclusion may use `NO_CASH_CLAIM`. Human/agent/
   Telegram judgment cannot change assignment. A preregistered negative/payoff
   control needed by a cash card may execute as `object_kind=BOUND_CONTROL`
   only inside that parent's exact TRAIN/VALIDATION/CONFIRMATION opening. It
   inherits the parent split, cohort, opening/allocation ID and evidence class,
   carries a sealed `CONTROL_REGISTRY.jsonl` ID, `bound_parent_card_id` and
   `alpha_claimant=false`, cannot open or claim a cohort independently, cannot
   be selected/promoted, and emits only a control verdict. This is the sole execution exception for
   `NO_CASH_CLAIM`; it does not mint or spend a second alpha slice.
8. **SUPERFAMILY-WIDE-ERROR-BUDGET.** Before the first VALIDATION is sealed,
   freeze
   maximum validation/confirmation openings and one super-family alpha-
   spending/closed-testing graph whose child allocations sum to at most the
   single proposed 0.05. Every lineage/opening consumes its preassigned slice,
   including an independent cohort, abandoned claim, failed result or crash
   after claim. K=1 describes one opening only; it never resets project FWER.
   Reports, tracks, sports, agents, dates, releases and newly named research
   generations do not reset this ledger. Once the super-family budget is
   exhausted, remaining Deep03 work is exploratory; a relabeling cannot revive
   confirmatory alpha.
9. **FIXED-SEARCH-END.** Discovery and confirmation each bind a cost/time end
   or valid alpha-spending rule. “Search until one cash strategy survives” is
   forbidden.
10. **BOUNDED-ALL-ANGLES-CLAIM.** “All angles covered” means every atomic cell
    from versioned registries and independently reproduced expected sets, not
    every imaginable idea.
11. **COVERAGE-SET-EQUALITY.** Two independent expected-set implementations
    must match before expected/emitted equality can pass.
12. **NO-SURVIVORSHIP-OMISSION.** Structurally inapplicable, zero-activity,
    zero-trigger, data-blocked, authority-blocked and rejected cells stay rows.
13. **UNCLASSIFIED-BACKLOG.** Anything outside the taxonomy enters
    UNCLASSIFIED with evidence and a next-cycle proposal; it is not forced into
    the nearest family or silently dropped.
14. **D3-PER-CELL.** D-3 Visualize-Everything applies equally to rejected,
    blocked and winning cells.
15. **EXECUTION-GATES-CARRY-FORWARD.** Every economic family retains W-FS1,
    strict-through as the binding historical fill case, exact fees, partial/
    cancel/reconcile, latency, inventory, forced exit, terminal and all-zero
    root accounting.
16. **POLICY-IDENTITY-CLOSED.** New external inputs, model/retraining rules,
    routers, weights, precedence, decision actions, universe or terminal rules
    create a new decision policy and cannot inherit confirmation.
17. **FILL-AUTHORITY-BY-STAGE.** TRAIN freezes decision_policy_fingerprint plus
    fill_authority_by_stage. Historical economics use public strict-through;
    future micro-live observes private authoritative fills only for execution
    calibration. Stage transition under the frozen mapping is not a policy
    change and never re-estimates confirmed PnL. Changing the mapping or a
    within-stage authority starts a new lineage.
18. **PORTFOLIO-IS-A-NEW-POLICY.** Confirmed components do not confirm their
    portfolio. Joint replay, common-root/co-tail accounting, shared capital/
    collateral/write limits, multiplicity and untouched evidence are required.
19. **EXTERNAL-INFO-DEFAULT-DENY.** A future provider release names source,
    endpoint, credential class, spend, licensing, publication/as-of clock,
    provenance and fail-closed behavior. Unknown availability time is leakage.
20. **RFQ-COMBO-SEPARATION.** RFQ/combo stays read-only by default and generic
    CLOB execution excludes MVE/combo. Authenticated actions require a separate
    payout-verified engine and release.
21. **SHADOW-NO-ORDER.** Shadow needs a later RISK-1 release and code-level
    no-order proof. Simulated fills do not establish queue authority.
22. **MICROLIVE-NOT-IMPLIED.** A translation-complete specification is not
    READY_FOR_MICROLIVE. A micro-live allowlist must be an exact subset of the
    confirmed card/action/universe/risk fingerprints. Any new action, size,
    market or phase first requires a new lineage through replay, VALIDATION,
    CONFIRMATION and translation, then separate authority. GUARDRAILS.S1..S6,
    exact live permission and per-session operator confirmation still apply.
23. **ACCOUNT-RISK-ISOLATION.** Future micro-live uses a dedicated subaccount
    and subaccount-bound key; absence is BLOCKED_DEPENDENCY, not a namespace
    fallback. Any external order/position/balance/collateral mutation is
    account-risk evidence and fails closed before a new-risk action.
24. **CALIBRATION-NOT-PROFIT.** Future micro-live samples calibrate execution
    and safety only, stay disjoint from strategy validation and cannot promote
    or reprice the confirmed strategy result.
25. **OPERATOR-VISIBILITY-WITHOUT-BYPASS.** Every distinct hypothesis and
    experiment revision receives an individual Telegram feed event and every
    judgment is preserved; Telegram cannot change a scientific verdict,
    sealed design, authority, spend, holdout state or trading state.

If a future release adopts the Deep02 light process, each read-only descriptive
scan needs one scan-ledger line plus input fingerprint, script SHA and output
fingerprint. Completeness is checked once at run level; no new transaction
framework is built around each chart.

### 4.6 Frozen breadth taxonomy and deterministic cell generator

Parent mechanism families are presentation groups only. Completeness is checked
at the atomic direction level in §4.7.2. The core key is:

~~~text
(parent_family_id, atomic_direction_id, model_row_kind,
 model_owner_atomic_id, model_target_label_contract_id,
 control_class, sport_id, matched_target_sport_id, control_match_id,
 market_structure_id, family_structure_id,
 time_axis_id, time_bucket_id)
~~~

One atomic direction can never satisfy another direction in its parent family.
For non-G directions, all three model fields are `NOT_APPLICABLE`. For G
directions, owner is one of the 94 non-G atomic IDs and row kind is either
PAIR_CLOSURE with target `NOT_APPLICABLE`, or TARGET_CONTRACT with one exact
`target_label_contract_id`, under §4.7.2. A nested target list is not a key.
For `SPORTS_TARGET`, both matched-control fields are `NOT_APPLICABLE`. For
`NONSPORT_MATCHED_CONTROL`, `sport_id=NONSPORT_CONTROL`, target sport and a
deterministic match ID are mandatory. These values are part of the core key,
not optional labels. Every expected key is emitted. Additional overlays
stratify a cell but cannot silently shrink the core universe:

~~~text
(book_topology, normalized_price_band, liquidity_band,
 fee_class, data_quality_state)
~~~

#### 4.6.1 Sport identity and controls

SPORT_TAXONOMY.json freezes exact source-object/version/field paths, raw values,
canonical sport IDs, alias rules and unknown reasons before cell generation.
Binding sport identity may use only authoritative series/event metadata named
in that registry. Ticker/title regex is diagnostic only and cannot turn UNKNOWN
into a known sport. Raw and canonical values are both retained.

Every Sports category in the sealed catalog manifest receives rows, including
zero-trade/zero-L2 categories. `SPORT_EXPECTED_SET` is exactly the unique
canonical Sports IDs in that manifest plus `UNKNOWN_SPORT` iff at least one
in-scope Sports row cannot be mapped; Tennis, Baseball and Golf remain
separate when present and no sport is preselected. A deterministic matched
non-Sports control generator is mandatory, not optional. It freezes candidate
controls, match cardinality and matching on structure, normalized price,
liquidity and time without outcome inspection. `control_class` is exactly
SPORTS_TARGET or NONSPORT_MATCHED_CONTROL. Each control carries
`sport_id=NONSPORT_CONTROL`, its target sport and `control_match_id`; it never
enters the Sports policy candidate pool. Empty control candidate sets remain
explicit BLOCKED_MAPPING rows rather than disappearing.

#### 4.6.2 Orthogonal market-structure identity

A single overlapping label is forbidden. The generator assigns these mutually
exclusive fields from authoritative metadata:

| Field | Exact values |
|---|---|
| execution_surface | CLOB, RFQ, CLOB_AND_RFQ, UNKNOWN |
| instrument_form | SINGLE_CONTRACT, NATIVE_MULTI_OUTCOME, MVE_PACKAGE, UNKNOWN |
| payout_form | WINNER, TOTAL_THRESHOLD, SPREAD_THRESHOLD, ORDERED_RANGE, PROP_OTHER, UNKNOWN |
| family_relation | NONE, YES_NO_COMPLEMENT, MUTEX_EXHAUSTIVE, MUTEX_NONEXHAUSTIVE, MONOTONE_THRESHOLD, BRACKET_GRAPH, SAME_ROOT_CROSS_FAMILY, UNKNOWN |

market_structure_id is the canonical concatenation of execution_surface,
instrument_form and payout_form. family_structure_id is relation type plus the
authoritative family ID and mapping version. If one contract belongs to
multiple proven family relations, the generator emits one row per relation; it
never chooses one after seeing a result. Every mapping row carries source
field/object VersionId, mapping rule/version, authority/confidence class and
unknown reason. UNKNOWN is retained and fail-closed. Title parsing cannot prove
payout exhaustiveness or relation identity.

#### 4.6.3 Independent time axes with exact boundaries

All axes use decision receive time t and the last causally available,
authoritative as-of timestamp. Intervals are left-closed/right-open unless
shown as an unbounded edge.

SCHEDULED_PHASE uses authoritative scheduled start s:

~~~text
PRE_24H_PLUS       t < s-24h
PRE_6_TO_24H       s-24h <= t < s-6h
PRE_1_TO_6H        s-6h <= t < s-1h
PRE_15_TO_60M      s-1h <= t < s-15m
PRE_0_TO_15M       s-15m <= t < s
POST_0_TO_5M       s <= t < s+5m
POST_5_TO_30M      s+5m <= t < s+30m
POST_30_TO_120M    s+30m <= t < s+120m
POST_120M_PLUS     s+120m <= t
UNKNOWN_PHASE      s absent, untrusted or not causally available
~~~

LISTING_AGE uses authoritative open/listing time o:

~~~text
AGE_0_TO_5M, AGE_5M_TO_1H, AGE_1_TO_6H,
AGE_6_TO_24H, AGE_24H_PLUS, UNKNOWN_LISTING_AGE
~~~

CLOSE_DISTANCE uses authoritative close time c and lifecycle transitions:

~~~text
PRE_CLOSE_60M_PLUS, PRE_CLOSE_15_TO_60M, PRE_CLOSE_0_TO_15M,
POST_CLOSE_PRE_DETERMINED, POST_DETERMINED_PRE_SETTLED,
POST_SETTLED, UNKNOWN_CLOSE_DISTANCE
~~~

LIFECYCLE_PROGRESS uses the last authoritative transition at t:

~~~text
PREOPEN, OPEN, PAUSED, CLOSED, DETERMINED, SETTLED, UNKNOWN_LIFECYCLE
~~~

TERMINAL_EXCEPTION is a separate axis:

~~~text
NORMAL, VOID, CANCELED, RETIRED, WALKOVER, POSTPONED, UNKNOWN_EXCEPTION
~~~

CALENDAR is two separate axes: UTC_HOUR_00 through UTC_HOUR_23 and
WEEKDAY_0 through WEEKDAY_6. Exact LISTING_AGE and CLOSE_DISTANCE boundaries
are serialized in TAXONOMY.json rather than inferred from labels.

Each time axis is assigned independently. A terminal state never overwrites
scheduled phase; an unknown on one axis never erases known values on another.
Any overlap or unassigned non-UNKNOWN row is a generator red condition.
POST labels claim only time relative to scheduled start, never actual play or a
serve/point/inning/score/round cause.

#### 4.6.4 Remaining overlays

| Overlay | Frozen values / rule |
|---|---|
| book topology | VALID_TWO_SIDED, BID_ONLY, ASK_ONLY, EMPTY, LOCKED, CROSSED, STALE, GAP, UNKNOWN; data-state precedence is fixed |
| normalized price | continuous surface plus [1,5), [5,15), [15,35), [35,65), [65,85), [85,95), [95,99), [99,100) cents |
| liquidity | continuous spread/depth/activity plus event-weighted bands frozen before outcome inspection |
| fee | exact series/date/event role, multiplier, account precision and accumulator version; UNKNOWN is red |
| data quality | VALID, STALE, GAP, RECONNECT, INVALID_SEQUENCE, UNKNOWN |

#### 4.6.5 Five-field machine status tuple

Human shorthand in §4.7 is non-binding. Every emitted cell carries exactly one
value in each orthogonal field:

| Field | Exact enum |
|---|---|
| applicability_status | APPLICABLE, STRUCTURALLY_INAPPLICABLE, UNKNOWN_APPLICABILITY |
| authority_status | AUTHORIZED_READONLY, DEFERRED_AUTHORITY, OUT_OF_SCOPE_CURRENT_PROGRAM, FORBIDDEN_CURRENT_SCOPE, NOT_APPLICABLE |
| dependency_status | READY, BLOCKED_DATA, BLOCKED_MAPPING, BLOCKED_ENGINE, BLOCKED_TERMINAL, BLOCKED_EXTERNAL, DATA_INVALID, NOT_APPLICABLE |
| cell_execution_status | NOT_STARTED, RUNNING, COMPLETE, FAILED, NOT_REQUIRED |
| research_disposition | NO_RESULT, TIER1_DESCRIBED, DESCRIPTIVE_FINDING, HYPOTHESIS_PROPOSED, MONITOR_ONLY, CONTROL_ONLY, COLLECT_MORE, REJECTED_WITH_EVIDENCE, METHODOLOGY_INVALID |

Separate fields are planned_role, required_depth, achieved_depth,
active_card_ids and next_action. next_action is one of NONE, RUN_TIER1,
DEFINE_CARD, COLLECT_DATA, REQUEST_AUTHORITY, BUILD_ENGINE, REJECT_CLOSE or
REOPEN_TAXONOMY. DEEP_TEST_ACTIVE and POLICY_LATER are therefore not overloaded
machine statuses.

Legal closure is a machine-enforced `oneOf`, not five independently valid
enums. Precedence is STRUCTURAL, DATA_INVALID, FORBIDDEN, OUT_OF_SCOPE,
DEFERRED_AUTHORITY, BLOCKED_DEPENDENCY, then EXECUTED:

| Closure class | Exact tuple requirements | Extra receipt | Breadth-eligible? |
|---|---|---|---|
| EXECUTED | APPLICABLE + AUTHORIZED_READONLY + READY + cell_execution_status=COMPLETE + research_disposition in {TIER1_DESCRIBED, DESCRIPTIVE_FINDING, HYPOTHESIS_PROPOSED, MONITOR_ONLY, CONTROL_ONLY, COLLECT_MORE, REJECTED_WITH_EVIDENCE, METHODOLOGY_INVALID} | achieved_depth>=required_depth; executed method/artifact | Yes |
| BLOCKED_DEPENDENCY | applicability in {APPLICABLE, UNKNOWN_APPLICABILITY} + AUTHORIZED_READONLY + dependency in {BLOCKED_DATA, BLOCKED_MAPPING, BLOCKED_ENGINE, BLOCKED_TERMINAL, BLOCKED_EXTERNAL} + NOT_REQUIRED + NO_RESULT | exact blocker/evidence/reopen trigger | Yes |
| DEFERRED_OR_FORBIDDEN | applicability in {APPLICABLE, UNKNOWN_APPLICABILITY} + authority in {DEFERRED_AUTHORITY, OUT_OF_SCOPE_CURRENT_PROGRAM, FORBIDDEN_CURRENT_SCOPE} + dependency != DATA_INVALID + NOT_REQUIRED + NO_RESULT | exact authority/defer reason, evidence and reopen/supersession trigger | Yes |
| DATA_INVALID_CLOSED | applicability in {APPLICABLE, UNKNOWN_APPLICABILITY} + AUTHORIZED_READONLY + DATA_INVALID + NOT_REQUIRED + METHODOLOGY_INVALID | DQ artifact and mechanical reopen trigger | Yes |
| STRUCTURAL_CLOSED | STRUCTURALLY_INAPPLICABLE + authority_status=NOT_APPLICABLE + dependency_status=NOT_APPLICABLE + NOT_REQUIRED + NO_RESULT | deterministic applicability_rule_id | Yes |
| IN_PROGRESS | applicability in {APPLICABLE, UNKNOWN_APPLICABILITY} + AUTHORIZED_READONLY + READY + cell_execution_status in {NOT_STARTED, RUNNING} + NO_RESULT | current receipt | **No** |
| EXECUTION_FAILED | applicability in {APPLICABLE, UNKNOWN_APPLICABILITY} + AUTHORIZED_READONLY + READY + FAILED + NO_RESULT | infrastructure failure artifact/retry class | **No** |

Every other cross-field combination is exact-red, including deferred +
COMPLETE, inapplicable + finding, blocked + rejected, or READY + NOT_REQUIRED.
`UNKNOWN_APPLICABILITY` is legal only in BLOCKED_DEPENDENCY,
DEFERRED_OR_FORBIDDEN, DATA_INVALID_CLOSED, IN_PROGRESS or EXECUTION_FAILED.
`NO_RESULT` is legal
only in BLOCKED_DEPENDENCY, DEFERRED_OR_FORBIDDEN, STRUCTURAL_CLOSED,
IN_PROGRESS or EXECUTION_FAILED; of those, only the first three satisfy
breadth. Infrastructure failure never becomes METHODOLOGY_INVALID.
REJECTED_WITH_EVIDENCE is legal only in EXECUTED and additionally requires the
preregistered falsifier and artifact; a hunch can only be
HYPOTHESIS_PROPOSED.

The generator writes TAXONOMY.json, DIRECTION_TAXONOMY.jsonl,
PARENT_ATOMIC_MAP.json, MODEL_METHOD_REGISTRY.jsonl,
MODEL_METHOD_COVERAGE.json, SPORT_TAXONOMY.json,
STRUCTURE_MAPPING.parquet, ELIGIBLE_CELL_KEYS.parquet,
D3_BREADTH_COVERAGE_LEDGER.parquet, D3_BREADTH_SET_EQUALITY.json and
UNCLASSIFIED_BACKLOG.jsonl. Each direction row freezes required data,
authority/engine dependency, falsifier, defer reason vocabulary, reopen trigger
and minimum depth. Each cell carries all hashes/keys/statuses; dates, roots,
markets and L1/L2/trade/terminal support; zero-activity/trigger counts; method
IDs; exposure class; artifact SHA and next action.

Set equality is not self-certified by the scan generator. Before any scan:

1. taxonomy compiler A emits `EXPECTED_CELL_KEYS_A` from the sealed registries,
   including the parent/atomic map, 1,128 model-method-owner pairs, applicable
   target contracts and target/control sport keys;
2. an independently implemented enumerator B re-reads the raw catalog manifest
   and every registry, independently reconstructs the parent/atomic and
   model-method-owner/target/control expected sets, and emits
   `EXPECTED_CELL_KEYS_B`;
3. exact key-set/hash equality A==B is required;
4. the coverage runner may only left-join results onto the agreed expected set;
   it is forbidden to generate or filter its own universe;
5. an independent recomputation verifies expected/emitted equality and every
   exact-one/explicit-expansion assignment before `RUN_COMPLETE`.

Golden boundary fixtures cover every time endpoint, sport alias/UNKNOWN,
F08 base/derived identity, target/control match, all G-owner pairs,
market-structure value, multi-relation expansion, topology/data precedence,
every legal closure class, zero activity, one missing key, one duplicate key
and one unexpected key. Each sentinel must turn readiness red.

### 4.7 Horizontal direction ledger

This is a human family index, not the machine direction/status ledger. Its
planning notes cannot satisfy coverage. Every atomic child in §4.7.2 crosses
§4.6 and uses only the five-field tuple, even when inapplicable.

| ID | Direction / mechanism | Human planning note (non-machine) | Minimum read-only test | Before economic promotion |
|---|---|---|---|---|
| M01-LP | passive spread capture / selective liquidity | DEEP_TEST_ACTIVE via PM | spread/depth/dwell, fee wall, strict-fill opportunity | PM card + W-FS1 + untouched evidence |
| M02-INVMM | inventory-aware reservation, skew, dynamic radius/cap | DESCRIPTIVE_NOW / POLICY_LATER | log-odds fair error, vol, toxicity and time surfaces | frozen skew/radius/cap; economic-sign, fee, exit tests |
| M03-SNBD | single-name book states/transitions/action value | DESCRIPTIVE_NOW | §4.9 dwell, hazard, transitions, executable markout | separate finite card for each state-action policy |
| M04-RANGE | consolidation/range scalping | DESCRIPTIVE_NOW / POLICY_LATER | band dwell/crossings, strict round trips, breakout loss | frozen band/entry/timeout/re-entry |
| M05-REVERT | shock overshoot fade / general mean reversion | DESCRIPTIVE_NOW | shock × deceleration × rebuild executable reversion | past-only trigger, continuation kill, action authority |
| M06-MOM | large-flow/quote-velocity continuation | DESCRIPTIVE_NOW | signed flow, no-trade jumps, vacuum and continuation | directional card; maker/taker fee and exit gate |
| M07-DEPTH | depletion, retreat, vacuum, refill, rebuild | seed deep test plus descriptive | depth survival, refill hazard, false-kill cost | zero-resting/cancel-race plus parent/increment card |
| M08-VOL | vol harvesting, calm→burst, jump/regime | DESCRIPTIVE_NOW | realized log-odds vol, spread/vol, jump hazard | frozen detector/radius/breakout kill/drift rule |
| M09-QUEUE | queue join/improve/hold/cancel/amend | descriptive; policy deferred | fill intensity by distance/age/ahead proxy and markout | audited own-order calibration and action authority |
| M10-LATENCY | cancel race, quote velocity, system load, no-trade defense | safety deep test | queue/cancel deterioration vs adverse hazard | measured manifests + W-FS1 |
| M11-LEADLAG | same-root and linked-contract lead-lag | DESCRIPTIVE_NOW | quote-age aligned lag and time-reversal placebo | frozen leader and executable follower card |
| M12-XMKT | payout constraints, parity, executable relative value | monitor | exhaustive payout residual after fees/age/orphan stress | multi-leg authority and joint path replay |
| M13-3WAY | three-way over/underround | monitor | exclusivity/exhaustiveness and net basket residual | three-leg execution/capital/orphan card |
| M14-POISSON | Soccer Poisson relative value | POLICY_LATER / DEFERRED_DATA | internal-price implied-rate consistency | authoritative family map, lawful inputs, separate card |
| M15-XSECT | cross-sectional rank/relative strength/selection | DESCRIPTIVE_NOW | simultaneous matched ranks, neutralization, turnover | frozen selector/allocation and untouched evidence |
| M16-BEHAV | urgency, occupancy, retail proxies, anchoring/overreaction | DESCRIPTIVE_NOW | increment beyond volume+spread+activity, placebos | no identity claim; finite selector/card |
| M17-TERMINAL | convergence, certainty, settlement/exception behavior | CS deep diagnostic | calibration, terminal transitions and loss tails | full terminal data/semantics and Track-D scope |
| M18-DIRINT | venue-internal directional alpha | DESCRIPTIVE_NOW / POLICY_LATER | executable future-return calibration vs baselines | directional/action authority, fees and exits |
| M19-EXTERNAL | odds/score/serve/injury/lineup lead-lag | DEFERRED_AUTHORITY / DATA | schema/fixture and EVPI only | provider release, publication clock, license, cost |
| M20-SPORT | sport-specific hypothesis factory | DESCRIPTIVE_NOW | §4.12 sport mechanics × contract structure | survivor gets a finite card; no invented sport state |
| M21-RFQ | RFQ spread/response/completion | DEFERRED_AUTHORITY | retain old blocked receipt only now | new read authority, integrity repair, RFQ simulator |
| M22-COMBO | MVE/combo/package pricing | DEFERRED_AUTHORITY | catalog/schema receipt only if admitted | Q7 opt-in and payout-verified package engine |
| M23-ML | change-point, HMM, point-process/Hawkes, supervised, ensemble | DESCRIPTIVE_NOW / POLICY_LATER | chronological OOF stability vs simple baseline | model/retraining identity and nested selection |
| M24-META | regime router / mixture / abstention | POLICY_LATER | OOF routing, switch cost, reject option | confirmed children, frozen router, new confirmation |
| M25-PORT | portfolio, allocation and hedge | POLICY_LATER | common-root covariance/co-tail/capital/token use | joint replay, weights/caps and untouched holdout |
| M26-FEE | fee/rebate/incentive maker-taker boundary | DESCRIPTIVE_NOW | exact fee-after break-even by cell | accounting dimension; action change is new policy |
| M27-UNCLASSIFIED | valid observation outside taxonomy | permanent backlog | evidence/context/nearest rejected labels | taxonomy proposal and new lineage |

#### 4.7.1 Breadth-test catalog

These tests make the horizontal layer executable as a research design without
turning descriptive output into strategy PnL:

| ID | Required artifact | Binding completeness condition |
|---|---|---|
| D3-F00-BREADTH | mechanism × sport × structure × time cube | exact expected/emitted key equality; zeros and blocked cells retained |
| D3-F01-SNBD | state, dwell, transition, hazard and outcome atlas | data state precedes dynamic state; full distributions and controls |
| D3-F02-SCALP | general passive scalping opportunity atlas | both strict-fill legs, fees, breakout loss; descriptive only |
| D3-F03-DIRECTION | momentum/reversion/large-flow/OFI/vol/jump atlas | fixed horizons, competing outcomes, no midpoint tradability claim |
| D3-F04-RV | three-way/bracket/threshold/Poisson/lead-lag/stale-leg ledger | payout/mapping status explicit; residual is monitor-only |
| D3-F05-TERMINAL | convergence/migration/freeze/exception-state atlas | every legal lifecycle path and missing outcome shown |
| D3-F06-SPORT | all-Sports transfer/heterogeneity report | every captured sport appears, including zero-support cells |
| D3-F07-EXTERNAL | EVPI/provider/defer/legal receipt | zero paid/authenticated calls; one-item approval requirements |
| D3-F08-RFQ-MVE | old disposition plus authority/data/engine receipt | no restricted read or mutation without new release |
| D3-F09-PORT-META | overlap, selector, niche, capacity, account and backlog map | no component PnL sum; portfolio/router marked new policy |

#### 4.7.2 Minimum atomic direction registry

The following IDs are atomic completeness units. Parent M01–M27 labels are
rollups only. Every atomic ID becomes its own DIRECTION_TAXONOMY.jsonl row and
its own cross-product keys; completing one never completes a sibling.

Microstructure, SNBD and maker:

~~~text
A01-SPREAD-CAPTURE
A02-TOXICITY-MARKOUT
A03-DEPLETION-REFILL
A04-QUEUE-LAMBDA-DISTANCE
A05-SNBD-LIQUIDITY-REGIMES
A06-QUOTE-LIFETIME-BURST
A07A-OCCUPANCY-FINGERPRINT
A07B-COORDINATED-RETREAT
A07C-ADVERSE-SELECTION-TRANSFER
A07D-ANCHOR-MANIPULABILITY
A08-PRICE-GRID-ROUND-NUMBER
A09-LIQUIDITY-SEASONALITY
A10-CROSS-MARKET-QUOTE-SPILLOVER
A11-ONE-SIDED-PROVISION
A12-GENERIC-PASSIVE-SCALPING
A13-T90-RANGE
A14-T90-REBUILD
A15-FAIR-MICROPRICE-PAST-FLOW
A16-INVENTORY-RESERVATION-SKEW
A17-DYNAMIC-QUOTE-RADIUS
A18-TIME-SHRINKING-CAP
A19-NO-TRADE-QUOTE-JUMP
A20-SYSTEM-LOAD-QUEUE-GROWTH
~~~

Directional, reversion and volatility:

~~~text
B01-MOMENTUM-CONTINUATION
B02-MEAN-REVERSION
B03-POST-JUMP-CONTINUATION-VS-REVERSAL
B04-LARGE-FLOW-CONTINUATION-VS-REVERSAL
B05-OFI-PREDICTIVITY
B06-BOOK-IMBALANCE-PREDICTIVITY
B07-MICROPRICE-PREDICTIVITY
B08-CONTINUOUS-PRICE-CALIBRATION
B09-LISTING-TO-START-DRIFT
B10-INPLAY-PROXY-PRICE-EROSION
B11-REALIZED-VOL-BY-TIME
B12-JUMP-HAZARD
B13-CALM-TO-BURST
B14-VOL-HARVEST
B15-VOL-BREAKOUT
B16-CS-ASK
B17-CS-BID-CONTROL
~~~

Relative value:

~~~text
C01-THREEWAY-OVERROUND
C02-BRACKET-MONOTONICITY
C03-THRESHOLD-MONOTONICITY
C04-SAME-EVENT-FAMILY-COHERENCE
C05-SOCCER-POISSON
C06-WITHIN-EVENT-LEADLAG
C07-STALE-LEG
C08-COMBO-VS-LEG-COST
C09-PAYOUT-STATE-DOMINANCE
C10-MULTIDAY-SERIES
~~~

RFQ and MVE:

~~~text
D01-RFQ-FLOW-CENSUS
D02-RFQ-SIZE-INTENT
D03-RFQ-LIFECYCLE-SURVIVAL
D04-RFQ-COMBO-DEMAND-LEG-PRESSURE
D05-RFQ-REQUESTER-HASH
D06-RFQ-DIRECTION-VOL-SIGNAL
D07-RFQ-TO-CLOB-IMPACT
D08-RFQ-DIRECT-QUOTE-PNL
D09-MVE-CATALOG-SCHEMA
D10-MVE-DIRECT-TRADING
~~~

Terminal:

~~~text
E01-SETTLEMENT-CONVERGENCE
E02-EXPIRY-LIQUIDITY-MIGRATION
E03-FREEZE-WINDOW
E04-SETTLEMENT-CALIBRATION
E05-TERMINAL-EXCEPTION-STATES
E06-HOLD-TO-TERMINAL-TAIL
~~~

Ecology, selection, external and sport:

~~~text
F01-PARTICIPANT-MIX-PROXY
F02-FEE-STRUCTURE-EFFECT
F03-NEW-MARKET-COLD-START
F04-CROSS-SECTIONAL-RANK
F05-SCANNER-SPREAD-FLOW-DEPTH
F06-NICHE-COMPOSITE
F07-LOW-OCC-INDEPENDENT-FAIR
F08-SPORT-FACTORY
F09-TENNIS-BO3-BO5
F10-BASEBALL-ONE-SIDE
F11-GOLF-THIN-LONG-LIFECYCLE
F12-EXTERNAL-ODDS-LEADLAG
F13-EXTERNAL-SPORT-STATE
F14-PROVIDER-PRICE-LATENCY-TIER
F15-BLINDNESS-EVPI
F16-COURTSIDE-SPEED-OUT-OF-SCOPE
F17-CRYPTO-SPOT-ANCHOR-OUT-OF-SCOPE
~~~

Model and inference escalation:

~~~text
G01-CHANGEPOINT-REGIME
G02-HMM-HSMM-STATE
G03-POINT-PROCESS-HAWKES
G04-GARCH-STOCHASTIC-VOL
G05-STATE-SPACE-KALMAN
G06-PCA-FACTOR-CLUSTER
G07-COINTEGRATION-VECM
G08-REGULARIZED-GLM-SURVIVAL
G09-TREE-BOOSTING
G10-NEURAL-SEQUENCE
G11-ENSEMBLE-STACKING
G12-DRIFT-CALIBRATION-RETRAINING
~~~

Portfolio and meta:

~~~text
P01-STRATEGY-OVERLAP-PRECEDENCE
P02-ROUTER-ABSTENTION-SWITCH
P03-COMMON-ROOT-COVARIANCE-COTAIL
P04-MULTIMARKET-ALLOCATION
P05-BRACKET-HEDGE
P06-DYNAMIC-DAILY-SELECTION
P07-CAPACITY-FILL-DECAY-IMPACT
P08-PROGRESSIVE-SCALING
P09-MANUAL-SYSTEM-COEXISTENCE
P10-COMMERCIAL-ROC-COST
P11-UNCLASSIFIED-BACKLOG
~~~

V0 therefore contains exactly 106 base atomic IDs: A=23, B=17, C=10, D=10,
E=6, F=17, G=12 and P=11. The only canonical F08 atomic ID is
`F08-SPORT-FACTORY` (`F08` in the parent map). Core `sport_id` generates one
SPORTS_TARGET instance for every member of `SPORT_EXPECTED_SET`; the derived
display-only `generated_instance_id=F08::<sport_id>` never enters the atomic ID,
parent map or 106-ID count. NONSPORT_MATCHED_CONTROL rows follow the exact
core-key rule in §4.6.1 and remain rows even when F08 is structurally
inapplicable. A golden fixture rejects base-F08×sport double expansion,
`F08::<sport>` as an atomic ID, mismatched target sport, missing UNKNOWN_SPORT
and a missing/duplicate control match.

F16 records the
already rejected business direction of fighting top markets on courtside speed;
F17 records the Crypto spot-anchor idea as outside Sports deep03. They cannot
vanish merely because they do not run.

`PARENT_ATOMIC_MAP.json` is not inferred from names. The sealed V0 mapping is:

| Parent | Exact atomic children |
|---|---|
| M01-LP | A01, A11 |
| M02-INVMM | A15, A16, A17, A18 |
| M03-SNBD | A05, A06, A09 |
| M04-RANGE | A12, A13 |
| M05-REVERT | B02, B03 |
| M06-MOM | B01, B04 |
| M07-DEPTH | A03, A07B, A14 |
| M08-VOL | B11, B12, B13, B14, B15 |
| M09-QUEUE | A04 |
| M10-LATENCY | A02, A07C, A19, A20 |
| M11-LEADLAG | A10, C06, C07 |
| M12-XMKT | C02, C03, C04, C09, C10 |
| M13-3WAY | C01 |
| M14-POISSON | C05 |
| M15-XSECT | F04, F05, F06, F07 |
| M16-BEHAV | A07A, A07D, A08, F01, F03 |
| M17-TERMINAL | B16, B17, E01, E02, E03, E04, E05, E06 |
| M18-DIRINT | B05, B06, B07, B08, B09, B10 |
| M19-EXTERNAL | F12, F13, F14, F15, F16, F17 |
| M20-SPORT | F08, F09, F10, F11 |
| M21-RFQ | D01, D02, D03, D04, D05, D06, D07, D08 |
| M22-COMBO | C08, D09, D10 |
| M23-ML | G01, G02, G03, G04, G05, G06, G07, G08, G09, G10, G11, G12 |
| M24-META | P01, P02, P06 |
| M25-PORT | P03, P04, P05, P07, P08, P09, P10 |
| M26-FEE | F02 |
| M27-UNCLASSIFIED | P11 |

Seal tests require every M01–M27 exactly once, every atomic ID exactly once as
a child, no unknown/range-expansion ambiguity and exact equality between the
human-readable table, `PARENT_ATOMIC_MAP.json` and both independent compilers.

These frozen defaults apply independently to every listed ID:

| Atomic IDs | Required data/dependency for Tier-1 | Planned role / depth |
|---|---|---|
| A01/A02/A06/A08/A09/A11/A12/A15/A17/A18/A19 | L1_TRADE + fee/time metadata | internal read-only, L1 DESCRIBE_ONLY |
| A03/A05/A07A–D/A10/A13/A14 | SEQUENCE_VALID_L2 plus L1_TRADE; A13/A14 Tennis overlay | mechanism/seed feature or card, L1; L3 only through named card |
| A04 | L1_TRADE public proxy at L1; OWN_ORDER_CALIBRATION before queue economics | monitor at L1; BLOCKED_DATA for gate-bearing queue claim |
| A16 | L1_TRADE plus W-FS1 inventory/risk engine | mechanism at L1; policy requires new/existing card |
| A20 | decision-queue/system-load telemetry plus L1_TRADE | BLOCKED_DATA if telemetry absent |
| B01–B15 | L1_TRADE; B05–B07 also L2; exact fees/latency for any executable label | internal descriptive L1; directional policy is DEFERRED_AUTHORITY |
| B16/B17 | L1_TRADE + TERMINAL; Track-D authority | B16 existing proposed card; B17 CONTROL_ONLY |
| C01–C10 | FAMILY_MAP + L1_TRADE + fee/payout mapping; TERMINAL where residual can survive | MONITOR_ONLY L1; multi-leg engine/authority before policy |
| D01–D07 | RFQ_RAW + CLOB join under a new read-only RFQ release | DEFERRED_AUTHORITY/BLOCKED_DATA; L0 until released |
| D08/D10 | RFQ/MVE execution engine, private lifecycle and explicit action authority | DEFERRED_AUTHORITY + BLOCKED_ENGINE; L0 |
| D09 | CATALOG only under admitted read scope | schema/coverage receipt, otherwise deferred |
| E01–E06 | TERMINAL + L1_TRADE + lifecycle mapping | L1; E06 policy use requires terminal-complete card |
| F01–F11 | CATALOG/L1_TRADE; L2 or TERMINAL where named | internal descriptive L1 |
| F12–F14 | EXTERNAL_ASOF and one-source release | DEFERRED_AUTHORITY + BLOCKED_EXTERNAL; L0 |
| F15 | internal L1/L2 oracle upper bound only | CONTROL_ONLY L1; no provider call |
| F16/F17 | no compute input | OUT_OF_SCOPE_CURRENT_PROGRAM, L0 exclusion receipt |
| G01–G12 | every exact G-method × non-G-owner pair, then its finite target/label contracts, required data and chronological fit split | MODEL_METHOD axis; L0 pair closure first, then L1 feasibility/increment or exact defer per applicable target contract |
| P01–P08/P10 | confirmed child outputs, common-root ledger and joint engine where applicable | POLICY_LATER; L1 overlap/capacity description |
| P09 | PRIVATE_ACCOUNT_STATE plus D-1.1 ownership mapping | future deployment dependency; L0/L1 receipt only |
| P11 | evidence/context from any unmatched observation | UNCLASSIFIED_BACKLOG, L0 |

Seed-role exceptions are fixed: A01/A11/A13/A14/B16 are CARD_DEFINED;
A02/A07A/A07B are FEATURE_DEFINED; B17 is CONTROL_ONLY; C-family residuals are
MONITOR_ONLY. Every other atom begins HYPOTHESIS_ONLY. These roles neither grant
authority nor prove execution.

Each atomic row freezes these fields before data inspection:

~~~text
parent_family_id
plain_question
required_data_classes
required_authority_class
required_engine_class
planned_role
required_depth
tier1_method_id
falsifier_id_or_DESCRIBE_ONLY
allowed_defer_reason_codes
reopen_trigger
promotion_card_requirement
~~~

Data classes are exactly CATALOG, L1_TRADE, SEQUENCE_VALID_L2, TERMINAL,
FAMILY_MAP, RFQ_RAW, EXTERNAL_ASOF, OWN_ORDER_CALIBRATION and
PRIVATE_ACCOUNT_STATE; a G-pair also requires TARGET_LABEL and OWNER_ATOMIC.
A row missing any contract field blocks taxonomy seal.

Default required_depth is L1-DESCRIBED. F16/F17 and currently forbidden RFQ/MVE
execution directions require L0-SEEN plus their durable exclusion/defer receipt.
An L1 row without a frozen numerical/equivalence falsifier is
DESCRIBE_ONLY: it may become TIER1_DESCRIBED or HYPOTHESIS_PROPOSED but cannot
be REJECTED_WITH_EVIDENCE. This prevents after-result invention of a rejection
rule. Any L2+ or economic promotion requires a new finite card and frozen
falsifier.

Reopen triggers are mechanical:

- BLOCKED_DATA: a later sealed manifest contains the named field and frozen
  minimum support;
- BLOCKED_MAPPING: the named independent mapping audit passes;
- BLOCKED_ENGINE: the exact engine/test receipt passes;
- BLOCKED_TERMINAL: terminal contract and coverage receipt passes;
- BLOCKED_EXTERNAL: named source/as-of/license approval exists;
- DEFERRED_AUTHORITY: a durable ruling explicitly names the atomic ID;
- OUT_OF_SCOPE_CURRENT_PROGRAM: a later ruling explicitly moves that ID into a
  named program.

`MODEL_METHOD_REGISTRY.jsonl` closes the M23 method axis at two disjoint machine
keys:

~~~text
MODEL_METHOD_PAIR_KEY=(g_method_id, owner_non_g_atomic_id)
MODEL_METHOD_TARGET_KEY=(g_method_id, owner_non_g_atomic_id,
                         target_label_contract_id)
~~~

Compiler A and independently implemented enumerator B each emit the full
12 × 94 = 1,128 pair expected set. Every pair freezes method family/version,
owner, applicability rule, required data/dependency/authority and transparent
baseline, and has exactly one pair state: `EXPANDED_TO_TARGETS` with a nonempty
finite target-ID set, or `PAIR_CLOSED` with an empty target set and exactly one
§4.6.5 closure. No owner can be omitted because a method designer considered it
uninteresting.

For an EXPANDED_TO_TARGETS pair, every target-label contract is its own
TARGET_CONTRACT core row and separately freezes task,
target/label clock, horizon, transparent baseline, finite hyperparameters/
seeds, chronological fit/OOF split, calibration, retraining/drift rule,
attempt/status/defer/reject reason, one §4.6.5 closure and artifact. Multiple
targets or materially different designs are separate target keys, experiment
revisions and Telegram obligations. A structurally incompatible pair closes as
PAIR_CLOSED/STRUCTURAL_CLOSED; a pair with missing target, label or data uses
the matching blocked closure. `MODEL_METHOD_COVERAGE.json` proves pair-A ==
pair-B == pair-emitted and target-A == target-B == target-emitted as separate
sets, with missing/duplicate/unexpected and pair/target key collision exact-red.
A G-pair or target is never alpha or a candidate by itself: any economic use
attaches to a non-G strategy card and enters the global selection and
super-family program-test ledgers.

BREADTH_COMPLETE requires parent/atomic exact coverage, all 1,128 G-owner pair
registry closures and every expected target-key §4.6.5 closure,
both independent expected sets to match, then expected==emitted with no missing/
duplicate/unexpected key. Every emitted row must match exactly one breadth-
eligible closure class in §4.6.5; IN_PROGRESS, EXECUTION_FAILED or an illegal
cross-field tuple makes it false. `UNKNOWN_APPLICABILITY` is legal only in the
five classes named in §4.6.5; `NO_RESULT` only in the five named classes there,
including STRUCTURAL_CLOSED, and never by itself. Every captured sport and
mandatory non-Sports control is counted including zeros; no Tier-1 output is
NetPnL/promotion; every exclusion has a durable receipt.

No DESCRIPTIVE_NOW row promises profit.

### 4.8 Deep02 and roadmap inheritance

| Earlier direction | Prior disposition | deep03 owner / required disposition |
|---|---|---|
| C1-SPREAD-CAPTURE-01 | DATA_STARVED | A01 / PM-TS; deep seed test or quantified reject |
| C1-LARGE-FLOW-CONTINUATION-01 | DATA_STARVED | B04; standalone scan, never hidden inside toxicity |
| C1-PREMATCH-TTS-01 | COLLECT_MORE | A01 + all time axes + E01; full curve, not one pre-match aggregate |
| C1-HFOLLOW-RETREAT-01 | DATA_STARVED | A07B; safety increment plus false-kill cost |
| C1-DEPLETION-REFILL-01 | DATA_STARVED | A03; general refill scan beyond T90 |
| C1-THREEWAY-OVERROUND-01 | DATA_STARVED | C01; explicit monitor/reject/defer receipt |
| C1-SOCCER-POISSON-RV-01 | DATA_STARVED | C05; explicit data/structure estimability receipt |

Prior source for every row is
`docs/research_reports/SPORTS_AUTORESEARCH_01_TERMINAL_REPORT_2026-07-15.md`;
Deep02 inherited the seven without converting these dispositions into
rejections. A future ledger stores prior_run, prior_status, prior_artifact and
artifact SHA per row. None may be called rejected merely because it was
DATA_STARVED or COLLECT_MORE.

The roadmap elements also stay explicit: participant urgency, temporary price
consolidation, realized volatility, dynamic log-odds quote radius,
settlement-near widening, time-shrinking inventory caps, no-trade quote jumps,
quote/book velocity, fill intensity by distance, fill clustering, system-load/
decision-queue growth, niche score and low-occupancy independent pricing with a
tighter cap. Each gets a feature/ablation receipt under M02/M04/M08/M09/M10/M16.

Selection retains two frozen baselines rather than replacing them with a model:

- scanner baseline = spread × observed volume × displayed depth, reported
  beside its executable-flow correction;
- niche composite proposal = tug frequency × executable volume × spread width
  − upset/forced-exit loss, with each component and any weight frozen before
  evaluation.

Capacity receipts separately cover fill decay with size, own queue addition,
book impact, participation limit, competition/capacity decay, collateral-hours
and write headroom. P06 records dynamic daily selection; P05 records bracket
hedging; P08 records the roadmap's future progressive scaling cap of at most 2×
per step as `DEFERRED_AUTHORITY`, not as a current ladder. F14 owns the provider
price/latency comparison; F16/F17 make the courtside-speed and Crypto exclusions
durable instead of silent.

### 4.9 Universal single-name book-dynamics layer

Single-name means one individual contract order book. SNBD is a shared
state/transition representation across many contracts; it is not itself alpha
and does not assert one policy transfers unchanged across sports.

State is hierarchical so an attractive regime cannot hide invalid data:

1. DATA_STATE: valid, stale, gap, reconnect, unknown fee/tick/lifecycle.
2. TOPOLOGY_STATE: two-sided, bid-only, ask-only, empty, locked, crossed.
3. DYNAMIC_STATE overlays: quiet-balanced, quiet-imbalanced, consolidation,
   signed-flow trend, depletion, vacuum, shock, overshoot, retreat, refill,
   rebuild, spread-collapse, vol-jump, no-trade reprice and queue/load stress.
4. CONTRACT_OVERLAY: sport, structure, normalized price, liquidity, fee,
   scheduled phase and terminal/certainty band.

Past-only inputs include spread, top-k side depth, imbalance, microprice
displacement, log-odds velocity, quote/update/trade rate, signed volume,
depletion/refill, two-sided availability, visible-queue proxy, quote age, fill
clustering, realized vol, system queue age and cancel-effective latency.
Missing telemetry is blocked, not zero.

The atlas emits time-weighted occupancy; full dwell/survival distributions;
transition counts/probabilities/hazards; executable bid/ask markout and
spread/depth recovery; matched quiet/time-shift/label-shuffle controls;
cross-date and leave-one-sport/league/event stability; and threshold
sensitivity. NO_ORDER is the only discovery action.

A research-only action-value table may compare JOIN, IMPROVE, HOLD, CANCEL,
PASSIVE_REDUCE, AGGRESSIVE_OPEN, IOC_EXIT and HOLD_TO_TERMINAL, but every cell
states action authority and fill source. It is not policy PnL until converted
to §4.13. INVALID, UNKNOWN and UNCLASSIFIED always map to NO_ORDER.

Model order is deterministic states → empirical transition/hazard → regularized
or tree baseline → change-point/HMM/point-process/ensemble only after
chronological OOF increment, calibration, drift and simplicity checks. Model
sophistication cannot rescue negative fee-after execution.

### 4.10 Granular policy-archetype stubs

These are discovery stubs, not registered policies:

| Stub | Past-only evidence | Exit/kill to test | Action posture now | Dominant falsifier |
|---|---|---|---|---|
| SC-RANGE | consolidation dwell, executable crossings, spread beyond costs | breakout/vol jump, collapse, timeout, cap | passive pair/single | strict round trips vanish or breakout tail dominates |
| SC-REBUILD | completed kill, zero resting/reconciled, recovery | renewed depletion, normalization, one cycle | passive re-entry | safe window closes before fill |
| MR-OVERSHOOT | shock, deceleration, opposing refill, stable anchor | continuation, second shock, timeout | passive fade; aggressive deferred | continuation-adjusted edge ≤0 |
| MOM-CONT | flow, quote velocity, depletion, continuation | reversal, refill, latency expiry | passive join diagnostic; aggressive deferred | fees/reaction consume move |
| VOL-HARVEST | wide spread vs past vol with stable depth | vol jump, no-trade reprice, terminal | selective passive pair | adverse-jump ES exceeds spread |
| VOL-BREAK | calm→burst and directional confirmation | false breakout/reversal/time stop | monitor until action authority | low precision after latency |
| DEP-REFILL | depletion then persistent replenishment | refill failure/opposite retreat/gap | passive after hysteresis | refill is nonpersistent/unfillable |
| LIQ-VACUUM | coordinated withdrawal and quote jump | normalization or continuation end | cancel/no quote; alpha separate | no lead or loss avoidance |
| QUEUE-JOIN | distance/age/ahead proxy predicts benign fill | queue growth/move/timeout | diagnostic until own orders | proxy fails actual queue |
| SN-LEADLAG | stable internal leader after age alignment | reversal/relation break/orphan | monitor; multi-leg deferred | time reversal/placebo matches |
| LOW-OCC-MM | low occupancy plus stable independent fair | occupancy/fair uncertainty/tight cap | one contract, tighter cap proposed | empty means no fill/informed selection |
| TERMINAL-CONV | verified lifecycle and calibrated discrepancy | settlement uncertainty/retire/void | terminal authority only | rare legal states erase edge |

Maker/maker, maker/taker, taker/maker and taker/taker are distinct policy
identities. Current seed paths allow passive entry and forced reduce-only IOC;
aggressive opening is DEFERRED_AUTHORITY.

### 4.11 Horizon, action and model registries

Each horizon binds horizon_id, clock, anchor, lookback, activation delay,
target type/start/end, executable side, overlap, censor/terminal rule, minimum
coverage, role, family and K. The proposed discovery grid is:

- micro: 10/25/50/100/250/500 ms;
- short: 1/2/5/10/30/60/120 s;
- meso: 5/15/60 min;
- phase: TTS 24h/6h/1h/15m/5m/0 plus §4.6 post-proxy buckets;
- terminal: frozen liquidation cutoff and actual settlement only where legal.

Lookback, forecast, hold, exit deadline and terminal are distinct. A family
freezes at most one primary horizon unless a larger multiplicity family is
preregistered. Executable returns use bid/ask plus fee/latency, never midpoint.

| Action | Meaning | Current posture |
|---|---|---|
| A0 | no quote / abstain | always; mandatory on unknown |
| A1 | passive single GTC post-only | admitted simulated card only |
| A2 | passive non-atomic pair | worst-sequence reserve/orphan FSM required |
| A3 | cancel single/group | mandatory safety |
| A4 | cancel-then-create | old order terminal/reconciled first |
| A5 | passive reduce-only GTC | authoritative position-derived |
| A6 | forced reduce-only IOC | exit only; effective-time depth |
| A7 | hold to frozen terminal | terminal-authorized card only |
| A8 | amend/decrease | DEFERRED_AUTHORITY; priority semantics first |
| A9 | aggressive opening IOC | DEFERRED_AUTHORITY; new directional policy |
| A10 | dynamic size >1 | capacity diagnostic only |
| A11 | multilevel/replenishing | deferred; queue/impact tests |
| A12 | multi-leg/hedge | deferred; joint engine/orphan risk |
| A13 | meta switch | deferred; cancel→zero-resting→reconcile |

Every action binds outcome/venue side, fixed-point price/quantity, TIF,
post-only, reduce-only derivation, order group/STP, expiry, reserve,
latency/fee and states. Unregistered means forbidden. Unknown never opens risk,
cancel-pending remains reserved and write limits cannot suppress cancel/exit.

Transparent no-signal, midpoint/microprice, simple flow, state/hazard and
fixed-rule baselines precede ML. Every ML feature set, transform,
hyperparameter, seed, fit window and recalibration/drift rule enters the global
selection ledger. Confirmatory K counts only policy-level claims inside one
opening; it is not the number of tried model settings. Forecast quality and
trading economics are separate gates.

### 4.12 Sport, relative-value, external and meta factories

| Factory | Mandatory subcells when captured | Data-honest question |
|---|---|---|
| Tennis | league/series, bo3/bo5 if authoritative, pre/post proxy, T90/tail | Do transitions differ by format/price without pretending serve/score is known? |
| Baseball | league/series, pre/post proxy, one-side, low occupancy, threshold/bracket | Are gaps/refill/flow effects repeatable without inning/pitch labels? |
| Golf | tournament/round structures, long lifecycle, thin/one-side, tail | Do duration/field concentration erase spread or terminal edge? |
| every other sport | generated, never pooled away | What transfers after matching price/liquidity/structure? |

Serve, score, inning, pitch, injury, lineup, cut and round state are not inferred
from price; they remain external fields until separately approved.

Cross-market work proceeds in six layers: authoritative identity/payout
exhaustiveness; quote-age residual after leg fees; all-leg strict fill/depth;
sequential/orphan capital paths; unwind/terminal worst state; then a new basket
card. Backlog includes complement, three-way overround, bracket/threshold
monotonicity, Soccer Poisson, same-root lead-lag and paired hedge. Track A stays
monitor-only until all layers pass.

External backlog names OpticOdds, Pinnacle, Sportradar, MM Program and generic
score/serve/injury/lineup sources. Listing authorizes nothing; each needs
one-item approval. A provider barred by D-1 pending legal eligibility is
intentionally absent and represented only by
LEGAL_ELIGIBILITY_BLOCKED_PROVIDER; it cannot be named or added here until a
later durable ruling clears that prohibition. Before approval only
fixtures/mocks/schema, publication-clock design and EVPI are permitted.

RFQ and MVE stay outside generic CLOB code. Current scope may retain the old
blocked receipt and coverage row; it may not read restricted RFQ payloads,
repair replay, submit/answer/accept RFQ, or opt generic code into MVE.

Meta/portfolio rules:

- each child keeps its own fingerprint and verdict;
- router inputs, precedence, switch cost and hysteresis freeze;
- switching requires NO_NEW_RISK → CANCEL → ZERO_RESTING → RECONCILE;
- overlapping children aggregate by root before covariance/co-tail analysis;
- collateral, tokens, loss caps and terminal exposure are joint;
- weights/hedges/router form a new policy with untouched evidence;
- no PnL/capacity sum precedes the joint contract.

### 4.13 Vertical strategy-card and promotion contract

Every run/card artifact separately records object_kind, research_stage,
run_execution_status, selection_disposition, research_verdict and
deployment_spec_status. Selection is not a favorable verdict, and the six
fields must match exactly one §16.1 `RUN_STATE_ONEOF` row. The research-stage
graph is:

~~~text
OPEN_DISCOVERY
 -> DISCOVERY_COMPLETE -> CARD_PROPOSED
 -> TRAIN_FROZEN -> TRAIN_EXECUTING -> TRAIN_COMPLETE
 -> VALIDATION_SEALED -> VALIDATION_EXECUTING -> VALIDATION_COMPLETE
 -> CONFIRMATION_SEALED -> CONFIRMATION_EXECUTING -> CONFIRMATION_COMPLETE
 -> IMPLEMENTATION_CONFORMANCE -> PROCESS_COMPLETE
~~~

TRANSLATION_COMPLETE_SPEC belongs only to deployment_spec_status.
BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, DATA_INVALID and EXECUTION_FAILED belong
to run_execution_status; METHODOLOGY_INVALID belongs to research_verdict. They are
never implicit forward edges.

| object_kind / from receipt | Exact guard for next stage | Otherwise |
|---|---|---|
| CASH_CARD / TRAIN_COMPLETE | selection_disposition=TRAIN_SELECTED, immutable super-family assignment, all TRAIN bound-control execution receipts complete, untouched VALIDATION, a preallocated super-family slice atomically CLAIMED before cohort read and exact release | TRAIN_NOT_SELECTED is terminal; absent control/lost CAS alpha blocks the opening |
| BOUND_CONTROL / TRAIN_COMPLETE | selection_disposition=NOT_APPLICABLE, null verdict, exact parent/opening/split binding and completed TRAIN control result; it mirrors the parent only when the parent CASH_CARD guard passes and never claims alpha | no independent next-stage edge |
| CASH_CARD / VALIDATION_COMPLETE | research_verdict=EDGE_CANDIDATE_FOR_CONFIRMATION, all required CONTROL_* validation verdicts incorporated, remaining preallocated super-family alpha atomically CLAIMED and a new confirmation release | REJECT/METHODOLOGY_INVALID/NOT_ESTIMABLE/RESEARCH_VALID_NOT_DEPLOYABLE/COLLECT_MORE are terminal for this opening |
| BOUND_CONTROL / VALIDATION_COMPLETE | one legal CONTROL_* verdict bound into the parent; it mirrors confirmation only if the parent CASH_CARD guard passes | no independent confirmation edge |
| CASH_CARD / CONFIRMATION_COMPLETE | research_verdict=BACKTEST_CANDIDATE, all required CONTROL_* confirmation verdicts incorporated and a separately released engineering W | RESEARCH_VALID_NOT_DEPLOYABLE/REJECT/METHODOLOGY_INVALID/NOT_ESTIMABLE/COLLECT_MORE are terminal for this opening |
| BOUND_CONTROL / CONFIRMATION_COMPLETE | one legal CONTROL_* verdict bound into the parent | terminal; no implementation edge |
| CASH_CARD / IMPLEMENTATION_CONFORMANCE | all conformance tests, policy risk/commercial fields and blockers closed | deployment_spec_status remains BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY or IMPLEMENTATION_CONFORMANCE_PENDING |
| CASH_CARD / PROCESS_COMPLETE | deployment_spec_status may become TRANSLATION_COMPLETE_SPEC | no shadow, account or order authority follows |

BOUND_CONTROL never advances itself; its stage mirrors the linked parent only
under the corresponding parent edge. TRAIN control output is an execution/
result receipt with null research_verdict. CONTROL_* is produced only at
VALIDATION_COMPLETE or CONFIRMATION_COMPLETE.

Every forward edge also requires `RESEARCH_FEED_COMPLETENESS` for all feed
events due through the source receipt, including its individual result/decision
message where applicable. Feed outage does not alter the scientific verdict;
it leaves the transition blocked and queued for delivery.

OPEN_DISCOVERY still needs exact scope/W/write-root authority. Completion seals
taxonomy/coverage/backlog; later ideas create a lineage. TRAIN_FROZEN binds
cards, super-family assignment/alpha graph, global/program/allocation ledgers,
features/actions/horizons, universe, split/embargo, K, opening alpha, seeds,
engine/fill-authority-by-stage/fee/
latency/terminal, power/stopping and commercial fields.
TRAIN selects only within that finite set. VALIDATION is one-shot and does not
select. CONFIRMATION uses untouched evidence and the identical decision policy
and historical economic-replay fingerprints. A semantic bug, new model/rule/
mapping/horizon/action or unregistered fill mapping starts a lineage unless a
predefined infrastructure retry proves no outcome was read.

A group-sequential interim look remains VALIDATION_EXECUTING or
CONFIRMATION_EXECUTING with run_execution_status=EXECUTING, null verdict and no
RUN_COMPLETE/feed result; its outcome-linked receipt stays sealed. Only the
frozen efficacy/futility/final boundary ends that opening. COLLECT_MORE at a
`*_COMPLETE` stage is terminal for that run/opening and creates no back-edge.
More data can become a later test only if that additional opening, policy
identity, untouched cohort, allocation and alpha-spending edge were
preallocated before the first VALIDATION, then receive a new run/opening ID and
exact release. Otherwise it is exploratory.

Implementation conformance requires replay/shadow/future executable paths to
share the strategy core. TRANSLATION_COMPLETE_SPEC closes the rule-translation
gap; it is not SHADOW_READY, MICROLIVE_READY or LIVE_READY. Every transition
atomically binds prior receipt and code/diff/config/data/split/engine/result
hashes. `RUN_COMPLETE` remains last inside each immutable research-result
bundle; its result-feed delivery receipt is appended outside that bundle and
is required before any next-stage transition.

Every STRATEGY_CARD must contain:

~~~yaml
identity: {card_id, family_id, super_family_id, program_family_id, object_kind,
           control_registry_id_or_null, bound_parent_card_id_or_null,
           alpha_claimant,
           superfamily_assignment_sha, version, lineage_id,
           parent_id, controls, authority_scope, role, promotable,
           decision_policy_fingerprint, economic_replay_fingerprint}
hypothesis: {mechanism, falsifiable_statement, falsifier, economic_rationale}
universe: {base_root_rule, sport, series, structure, phase, price, liquidity,
           selection, zero_trigger_policy}
data: {release_versions, required_fields, receive_clock, asof, ttl, warmup,
       gap_reconnect, missing_action, payout_lifecycle_contract}
horizons: {lookbacks, forecasts, hold, exit_deadline, terminal, primary,
           diagnostics, multiplicity_family}
features: [{id, role, owner, formula, source, transform, fit_scope,
            threshold_registry, decay_drift_rule}]
models: {model_spec_registry_sha, transparent_baseline, finite_candidates,
         hyperparameters, seeds, fit_oof, calibration, retraining_drift}
market_state: {schema, entry, transitions, dwell_hysteresis,
               exit_kill_reentry, terminal_states}
actions: {allowlist, denylist, side_mapping, state_action_matrix,
          max_active, replacement}
execution: {fill_authority_by_stage, execution_environment_fingerprints,
            w_fs1, same_time, fixed_point, latency, fee_rounding_accumulator,
            fill_tracks, partial_cancel_reconcile}
risk: {worst_sequence_reserve, root_event_factor_day_caps, collateral,
       max_inventory, write_emergency_tokens, stop, forced_exit, residual}
variants: {finite_registry, total_k, train_selection, simplicity_tiebreak}
inference: {estimands, blocks, split_shas_by_stage_and_opening, power, delta_min,
            superfamily_alpha_graph_sha, program_test_ledger_sha,
            alpha_allocation_id, opening_alpha, multiplicity,
            stopping, controls}
tail_capacity: {stress, tails, concentration, rare_support,
                one_contract_capacity, size_diagnostics, commercial_tbds}
artifacts: {ledgers, tests, independent_recalc, receipts, report}
verdicts: {legal_set, automatic_reds, reopen, next_stage}
deployment_boundary: {spec_only: true, no_shadow_or_order_authority: true}
~~~

Meta/portfolio cards additionally bind child fingerprints, precedence, switch
cost, hysteresis and joint limits. Missing fields make a card unfreezable.

### 4.14 Breadth × depth matrix and fast research waves

Horizontal completeness does not mean running every idea to maximum depth.
Each direction records its deepest legal/evidenced level:

| Depth | Meaning | Required receipt |
|---|---|---|
| L0-SEEN | taxonomy cell exists, including zero/inapplicable/blocked | coverage row and reason |
| L1-DESCRIBED | opportunity/state distribution and falsifier measured | input/script/output receipts plus D-3 report |
| L2-MECHANISM | past-only increment, controls, stability and execution boundary tested | feature/mechanism admission ledger |
| L3-POLICY | finite strategy card and W-FS1 pessimistic replay | full card, ledgers, exact fee-after root PnL |
| L4-CONFIRMED | one-shot validation and unchanged confirmation | split/registry/power/multiplicity receipts |
| L5-TRANSLATED | implementation conformance and no-translation-gap future-test spec | §17.1 artifact; still no order authority |

D-5 breadth requires L0 for every generated cell and L1 or a specific defer/
inapplicable reason for every direction. It does not require weak directions to
consume L2–L5 compute. Only evidence-based survivors deepen.

To move quickly without closing the universe, work is scheduled in parallel
waves whose membership is frozen before viewing their new outcomes:

1. **Foundation wave:** reader/data quality, breadth generator, receive-clock
   tape, W-FS1 and SNBD. These unlock many families at once.
2. **Internal single-name fast wave:** M01–M10, M16–M18 and M26 using current
   L1/trade/L2/lifecycle data—spread capture, scalping, momentum/reversion,
   depletion/refill, volatility, queue proxy, occupancy and terminal risk.
3. **Structure/sport wave:** M11–M15, M20 and sport-specific overlays—lead-lag,
   relative value, cross-sectional selection, Tennis/Baseball/Golf and every
   other captured sport.
4. **Dependency wave:** M19 and M21–M25—external data, RFQ/MVE, ML escalation,
   router and portfolio. Tier-1/defer receipts run now; deeper work waits only
   for its named data/authority/confirmed-child dependency.

Within a wave, prioritization is frozen from evidence availability, time to
falsify, one-leg/W-FS1 reuse, expected executable opportunity count, likely
capacity, implementation distance, compute cost and authority friction.
Weights and commercial hurdles are OPERATOR-TBD before outcome inspection.
Priority changes after seeing results enter the global selection ledger.

The fastest path to a cash-testable specification is therefore not “prematch
only” or “scalping only.” It is reusable execution/SNBD foundations plus several
parallel single-name candidates, while relative-value, external and portfolio
tracks remain visible and deepen as their dependencies clear.

### 4.15 Human-in-the-loop Telegram research feed

The operator requirement is binding for the future Deep03 workflow: **every
distinct hypothesis and every proposed experiment design/revision must become
an individually traceable Telegram description**, so the operator can follow
progress and add judgment before the research program silently narrows. The
separate proposed `W-HYPO-FEED-01` plan records the original request, but its
current mutable-ledger/direct-rejection semantics are
`CONFLICTED_DO_NOT_IMPLEMENT`. It is evidence only, not an eligible W spec. W2C
requires a revised, independently audited, exact-SHA W-HYPO-FEED-01 that matches
this section before any implementation release. Nothing in this candidate sends
a message or authorizes Telegram credentials/network use.

#### 4.15.1 Source of truth and what counts as an idea

Agent prose and the existing mutable/snapshot `HYPOTHESIS_LEDGER.json` are never
notification sources. The canonical source union is exactly:

- creation/design receipts in `HYPOTHESIS_EXPERIMENT_LEDGER.jsonl`;
- `STAGE_TRANSITION_RECEIPTS.jsonl`;
- `HYPOTHESIS_LIFECYCLE_RECEIPTS.jsonl`;
- `OPERATOR_JUDGMENT_LEDGER.jsonl`; and
- `OPERATOR_JUDGMENT_APPLICATIONS.jsonl`.

Each source receipt commits first with its deterministic `feed_event_id` and
canonical payload hash. One append-only, hash-chained
`RESEARCH_FEED_EVENTS.jsonl` is an idempotent projection of this union and
assigns monotone sequence numbers/revisions. Judgment causal order is journal
commit → `OPERATOR_JUDGMENT_RECORDED` event/ack → later safe-point
application receipt → `OPERATOR_JUDGMENT_APPLIED_OR_NOTE_ONLY` event; delivery
receipts never recursively create feed events.

Research state is derived exclusively from those immutable source receipts;
no mutable snapshot, scheduler flag or report text may close/reopen an idea or
change a stage. A versioned obligation table maps every lifecycle event to one
and only one source subtype:

| Required feed event | Unique canonical source subtype |
|---|---|
| HYPOTHESIS_PROPOSED | HYPOTHESIS_EXPERIMENT / HYPOTHESIS_CREATED |
| EXPERIMENT_DESIGN_PROPOSED | HYPOTHESIS_EXPERIMENT / DESIGN_PROPOSED |
| EXPERIMENT_DESIGN_REVISED | HYPOTHESIS_EXPERIMENT / DESIGN_REVISED |
| EXPERIMENT_REGISTERED | HYPOTHESIS_EXPERIMENT / EXPERIMENT_REGISTERED |
| CARD_FROZEN | STAGE_TRANSITION / CARD_FROZEN |
| RUN_AUTHORIZED | STAGE_TRANSITION / RUN_AUTHORIZED |
| RUN_STARTED | STAGE_TRANSITION / RUN_STARTED |
| RUN_BLOCKED_OR_DATA_INVALID | STAGE_TRANSITION / RUN_BLOCKED_OR_DATA_INVALID |
| RUN_FAILED | STAGE_TRANSITION / RUN_FAILED |
| RUN_COMPLETE | STAGE_TRANSITION / RUN_COMPLETE |
| DESCRIPTIVE_FINDING | STAGE_TRANSITION / DESCRIPTIVE_FINDING |
| DISCOVERY_COMPLETE | STAGE_TRANSITION / DISCOVERY_COMPLETE |
| RESULT_AND_NEXT_DECISION | STAGE_TRANSITION / RESULT_AND_NEXT_DECISION |
| OPERATOR_JUDGMENT_RECORDED | OPERATOR_JUDGMENT / JUDGMENT_RECORDED |
| OPERATOR_JUDGMENT_APPLIED_OR_NOTE_ONLY | OPERATOR_JUDGMENT_APPLICATION / APPLIED_OR_NOTE_ONLY |
| HYPOTHESIS_CLOSED | HYPOTHESIS_LIFECYCLE / CLOSED |
| HYPOTHESIS_REOPENED | HYPOTHESIS_LIFECYCLE / REOPENED |

An independent expected-event enumerator applies this obligation table to the
hash-chained source/state transitions, rather than treating observed feed rows
or source rows as their own expected set. Missing source subtype for a claimed
state transition, two source types claiming one event, a source without its
required event or an event without its source is exact-red. HYPOTHESIS_PROPOSED
establishes lifecycle OPEN. Thereafter CLOSED is legal only OPEN→CLOSED and
REOPENED only CLOSED→OPEN; strict alternation, prior-receipt linkage and
from/to-state equality are machine-checked. Duplicate close/reopen, reversed
direction or broken prior SHA is exact-red. Lifecycle has no other write path.

~~~yaml
event: {feed_event_id, event_seq, event_type, emitted_at_utc,
        prior_event_sha, source_receipt_sha, payload_sha}
identity: {run_id, work_package_id, producer_agent_id, owner_agent_id,
           hypothesis_id, hypothesis_revision, experiment_id,
           experiment_revision, parent_ids, atomic_direction_id,
           taxonomy_cell_keys, card_id_or_null, lineage_id,
           super_family_id, program_family_id,
           superfamily_assignment_sha, object_kind,
           control_registry_id_or_null, bound_parent_card_id_or_null,
           alpha_claimant}
idea: {one_sentence_claim, mechanism, who_may_pay, competing_explanations,
       falsifier, why_now}
experiment: {question, data_release_classes, population, public_split_rule_id,
             membership_free_design_sha, sealed_commitment_id_or_null,
             features, finite_variants, baselines, negative_controls,
             estimands, horizons, execution_fill_fee_boundary,
             stopping_rule, expected_cost_time}
state: {object_kind, research_stage_or_null, evidence_class,
        hypothesis_lifecycle_status,
        authority_status, dependency_status,
        cell_execution_status_or_null, run_execution_status_or_null,
        research_disposition, research_verdict,
        deployment_spec_status, selection_disposition,
        support_snapshot, blockers, next_gate, review_window_policy,
        review_window_end_utc_or_null,
        review_window_waiver_release_id_or_null, artifact_hashes, authority_sha}
lifecycle_or_null: {prior_lifecycle_receipt_sha_or_genesis, transition_kind,
                    from_status, to_status, reason_code,
                    reopen_trigger_or_null}
operator: {operator_disposition, latest_judgment_id, pending_question}
~~~

At creation, a hypothesis receives `DEEP03_CASH_STRATEGY_V1` unless the sealed
assignment rule proves it permanently non-promotable and assigns
`NO_CASH_CLAIM`. Missing/unknown assignment is legal for raw UNCLASSIFIED intake
only; it blocks CARD_PROPOSED/TRAIN and cannot be repaired after seeing an
outcome. A `BOUND_CONTROL` follows the parent-only exception in §4.5; all other
NO_CASH objects remain non-executable in sealed cohorts. Child family and
super-family identity are immutable across revisions.

The mutable JSON/Markdown hypothesis views are derived snapshots only. Polling
or diffing them is forbidden because an in-place rewrite can coalesce events,
lose order or fabricate a transition. A crash between source-receipt commit and
projection leaves a detectable missing event; reconciliation re-emits the same
deterministic ID before any promotion. It never invents a transition from a
snapshot.

A distinct mechanism/question is a hypothesis. A distinct population, action,
horizon, feature set, model family, finite variant registry, estimand, fill
authority, stopping rule or falsifier that could change selection is an
experiment revision. It receives a new individually delivered feed event; it
cannot hide as a “parameter tweak.” Finite variants tested jointly may share one
experiment message only if every variant and the selection rule are explicitly
listed. Message length limits cause deterministic numbered parts, never field
truncation or silent aggregation. Repeated execution heartbeat/progress on an
unchanged design may enter the daily digest, but no new idea/design may do so.

The auditable idea boundary is every **materialized** research proposal: before
an agent may persist it anywhere other than its canonical source receipt,
score, compare, prioritize, queue, test, summarize, defer, reject or place it
in any report/job/config, it must commit that receipt and deliver the individual
description. Selection/priority writers require the delivery receipt. A batch
brainstorm therefore
creates one event per distinct proposal. Exact duplicates may reference one
canonical ID only with a duplicate-link receipt. Private unmaterialized model
reasoning is not observable and is not falsely claimed as feed-complete; once
any part crosses into an artifact or decision, this gate applies. Silent
pre-ledger pruning is forbidden and the design/source reconciliation tests it.

Every experiment revision first creates a row in
`DESIGN_REVISION_REGISTRY.jsonl` and references immutable
`ACTION_REGISTRY.jsonl`, `HORIZON_REGISTRY.jsonl` and
`MODEL_SPEC_REGISTRY.jsonl` rows. Every runner then freezes a fully default-
resolved `RESOLVED_RUN_DESIGN_MANIFEST.json` and `ENGINE_MANIFEST.json` before
any RUN_AUTHORIZED receipt or split/cohort/data access. The
join key is exactly `{experiment_revision_id, run_or_trial_id, source_row_id,
design_fingerprint}`. The versioned canonical fingerprint uses explicit nulls,
sorted collections where order is non-semantic, preserved order where it is
semantic, and resolved engine defaults; it includes every population, variant/
selection rule, feature/transform, action, model/hyperparameter/seed, baseline/
negative control, estimand/horizon, split/stopping rule and fill/fee/latency/
exit/terminal boundary.

`FEED_DESIGN_RECONCILIATION.json` is produced by code independent of the feed
producer and records `independent_reconciler_code_sha`,
`input_ledger_manifest_sha`, `expected_fingerprint_set_sha` and
`feed_fingerprint_set_sha`. Before RUN_AUTHORIZED **and before any reader or
split handle can open**, it compares the declared and selected set from
`DESIGN_REVISION_REGISTRY`, `CANDIDATE_REGISTRY`,
`GLOBAL_SELECTION_LEDGER`, `PROGRAM_TEST_LEDGER`, all frozen strategy cards and
the action/horizon/model registries **plus the resolved-run and engine sets**
against individually delivered feed revisions. RUN_AUTHORIZED atomically binds
the resolved-run SHA, engine SHA, reconciliation SHA and delivered design-event
IDs. Runner startup re-resolves actual argv/config/environment/defaults and
verifies the same fingerprint before any reader/split open.

Before RUN_COMPLETE, reconciliation only adds every actually attempted method
from `METHOD_EXECUTION_RECEIPT` and proves the executed fingerprint still equals
the pre-authorized resolved fingerprint. The contract applies to OPEN_DISCOVERY
as well as TRAIN and sealed stages. If a run exists, any missing or empty
mandatory source, extra/duplicate source row, orphan feed revision, hidden
runner default or fingerprint mismatch is exact-red. No RUN_AUTHORIZED receipt
may exist until the complete design message has a matching delivery receipt and
the resolved/engine equality passes. If drift is somehow detected after a data
read, the run becomes DATA_INVALID or METHODOLOGY_INVALID as applicable, every
opened row/cohort becomes PRIOR_EXPOSED, any claimed alpha remains consumed and
the run cannot continue or promote.

#### 4.15.2 Required lifecycle events and leakage boundary

One stable `feed_event_id` is emitted for each applicable transition:

~~~text
HYPOTHESIS_PROPOSED
EXPERIMENT_DESIGN_PROPOSED
EXPERIMENT_DESIGN_REVISED
EXPERIMENT_REGISTERED
CARD_FROZEN
RUN_AUTHORIZED
RUN_STARTED
RUN_BLOCKED_OR_DATA_INVALID
RUN_FAILED
RUN_COMPLETE
DESCRIPTIVE_FINDING
DISCOVERY_COMPLETE
RESULT_AND_NEXT_DECISION
OPERATOR_JUDGMENT_RECORDED
OPERATOR_JUDGMENT_APPLIED_OR_NOTE_ONLY
HYPOTHESIS_CLOSED
HYPOTHESIS_REOPENED
~~~

OPEN_DISCOVERY messages may describe exploratory evidence, always labeled
`EXPLORATORY_ONLY / NOT A TRADING CLAIM`. During VALIDATION or CONFIRMATION,
Telegram receives only stage/health/DQ metadata until atomic `RUN_COMPLETE`;
no interim effect sign, PnL, p-value, ranking or threshold proximity is exposed.
Before either cohort is opened, freeze a per-event `outbound_schema_version`.
For sealed-stage events before RUN_COMPLETE it may render only stable IDs,
preregistered public design text, stage/heartbeat, mechanical blind-DQ status,
the public split-rule ID, a public design SHA that excludes membership and
**planned aggregate** support. A raw deterministic split/membership-manifest
hash is forbidden. If a pre-result commitment is needed, its domain is exactly
`{research_stage, opening_id, cohort_id, split_version}` and each domain uses a
fresh ≥128-bit random secret salt. Only an opaque commitment ID is rendered;
the salt/manifest remain sealed until that exact cohort's completed result.
VALIDATION disclosure can never reveal a CONFIRMATION salt, manifest or member.
It must render `REDACTED_SEALED` instead of observed support or
membership. It forbids exact
holdout/confirmation dates, roots, tickers, events, markets, series, cohort
window, object keys/paths/VersionIds, outcome-dependent artifact hashes/file
sizes, runtime/completion deltas or any low-entropy value from which sign/pass/
rank/proximity can be dictionary-inferred. Heartbeats use a preregistered fixed
cadence and byte-constant payload independent of work completed, support,
stopping or outcome. Early completion emits no standalone timing signal:
the two logical events RUN_COMPLETE and RESULT_AND_NEXT_DECISION render into
one non-multipart `SEALED_COMPLETION_TRANSPORT` sent by one Telegram API call
and acknowledged by one message ID. If it cannot fit one text message, it is
one `sendDocument` object with a concise result caption, never two sends. Its
receipt binds one `sealed_batch_id`, both logical event IDs, one combined
content SHA and the same Telegram message ID; neither logical event counts as
delivered alone. Crash/retry may duplicate the whole object only. After that
receipt, the completed message may bind decision/artifact hashes. The local
immutable artifact may reveal only that same cohort's salt/manifest for
verification; Telegram binds the verification receipt and still does not send
raw members or paths. An
operator comment after sealing annotates the run but cannot change its card,
stopping, split or verdict; a requested change becomes a new revision/lineage.
All payloads pass a schema allowlist/redaction step: no credential, signed URL,
private account/order identifier, raw object path, holdout row or outcome-linked
field outside the allowed stage schema may enter Telegram. A leak sentinel
must prove that membership, small-result hashes, sizes, timing, field names and
support deltas cannot reveal interim rank, significance or threshold proximity.

#### 4.15.3 Thirty-second decision card

Every hypothesis/design message must let the operator judge it without opening
the repository:

~~~text
🔬 [HYPOTHESIS or EXPERIMENT] <H-ID>/<E-ID> rev <N> · <sport/market>
Question: <one sentence, with direction/action/horizon>
Mechanism: <why it may exist; who may pay; main rival explanation>
Falsifier: <what observation would kill or only defer it>
Experiment: <population definition/public split-rule ID+membership-free design SHA, never sealed members/raw split hash; finite variants; baseline/control; primary metric>
Reality boundary: <data/as-of; strict fill/fee/exit/terminal assumption or descriptive-only>
Support now: <discovery counts, or planned aggregate/REDACTED_SEALED during a sealed stage>
State: <owner agent/work package; EXPLORATORY_ONLY/TRAIN/...; authority/dependency/blocker; review-window policy>
Next: <exact next test/gate, estimated cost/time, judgment-window duration/end rule and artifact/event IDs>
Your judgment: 深挖 / 降低优先 / 暂停 / 毙掉 / 备注 / 需澄清 + ID
⚠️ Judgment changes discovery priority only; it is not run/live authority.
~~~

RUN_COMPLETE adds the preregistered estimand, uncertainty, tails, failure cases,
fee-after/execution label, legal verdict and next choice. Daily digest is an
index—new, active, blocked, completed and awaiting judgment—and never substitutes
for an individual required message.

The default and required policy is BOUNDED_DISCOVERY_COMMENT_WINDOW. Each
future W freezes its duration. The accepted Telegram message timestamp is the
window start; its delivery receipt computes the exact UTC end, and the next
digest repeats that end. The card states the duration/end rule, so a delayed
delivery never shortens the operator's window. Until the computed end, a new
hypothesis/design revision cannot be scored, pruned,
prioritized, queued or started. `NO_WAIT_AFTER_DELIVERY` is forbidden by
default and becomes legal only when a durable exact operator release names the
specific W, event class/scope and waiver expiry; the waiver ID is visible in
the message and selection ledger. This is a scheduling window, never approval
or authority: silence means no priority change or consent. After an unwaived
window expires, already authorized discovery may proceed under its existing W.
Any judgment received before narrowing/start is CAS-applied at the next safe
point; after start it annotates that run and can affect only a later revision.
A Telegram reply cannot substitute for a higher-tier release.

#### 4.15.4 Operator judgment is a separate append-only axis

Inbound replies accept only an exact allowlisted chat and operator user ID, and
must bind `feed_event_id`, hypothesis/experiment revision and Telegram
reply-to message ID. The already deployed `status_bot.py` remains the **only**
`getUpdates` consumer; research replies are routed inside that consumer rather
than starting another long-poller. They may use reply-to semantics without
creating a new slash command, preserving the current operator ruling that
`/ack` is the single exposed command. The bot appends, rather than overwrites,
`OPERATOR_JUDGMENT_LEDGER.jsonl`:

~~~text
PRIORITIZE_DISCOVERY
LOWER_PRIORITY
PAUSE_DISCOVERY
OPERATOR_DECLINED
COMMENT_ONLY
REQUEST_CLARIFICATION
REOPEN_REQUESTED
~~~

These values never replace `research_disposition`, `research_verdict` or an
authority release. “毙掉” therefore means `OPERATOR_DECLINED`, not
`REJECTED_WITH_EVIDENCE`. “深挖” can reorder already authorized discovery
work **only when the research scheduler next reaches an OPEN_DISCOVERY safe
point**. A judgment cannot expand the released permission envelope. The
gateway may use only the exact Telegram send/update credential and mutate only
the exact journal/offset/delivery paths named in its release; the reply handler
cannot access any other credential or production path, spend, start compute,
open a holdout, change a sealed card, promote a result or reach an order path.
At or after TRAIN_FROZEN, every judgment is NOTE_ONLY; a requested
change creates a later lineage proposal. Each accepted reply gets a judgment
ID and a Telegram acknowledgment stating its limited effect; stale revisions,
unknown senders, unbound text and replayed updates fail closed.

The research controller never edits the judgment journal. It appends a separate
`OPERATOR_JUDGMENT_APPLICATIONS.jsonl` row with judgment/source SHA, stage at
application, `APPLIED_PRIORITY`, `NOTE_ONLY` or `REJECTED_STALE`, before/after
priority and reason. This makes receipt, interpretation and application three
auditable events instead of one chat-side mutation. Every applied human choice
also enters `GLOBAL_SELECTION_LEDGER`; any affected evidence is prior-exposed,
and later confirmation still requires a newly frozen card and untouched split.

Application is an atomic compare-and-swap on `{hypothesis_revision,
latest_stage_receipt_sha, research_stage=OPEN_DISCOVERY}` and shares the same
single-writer lock/transaction boundary as CARD_PROPOSED/TRAIN_FROZEN. If a
stage transition or revision wins the race, the judgment is recorded but the
application must become NOTE_ONLY; priority/pause state cannot change. A
read-OPEN/then-freeze/then-apply sequence is forbidden and tested with two
processes.

#### 4.15.5 Delivery, completeness and isolation

The future adapter may reuse the monitoring-side Telegram transport pattern,
but current `deploy/tg_common.py` returns only a boolean and has no inbound
judgment or durable message-ID contract. W-HYPO-FEED-01 must therefore add a
separate research-feed adapter and tests rather than claim the existing sender
already satisfies this section.

Producer and credentialed gateway are separated. A research worker writes only
signed/hashed feed-event bundles inside its authorized research output. The
single reference topology is: W09 produces signed bundles; the Mac control
plane pulls, verifies and renders them, then forwards a credential-free bounded
spool to the production monitoring host. One supervised credentialed service
on that host—an audited extension of the existing `status_bot.py` service—owns
the sole bot token, sends cards/acknowledgments and is the sole `getUpdates`
consumer. The Mac later pulls its append-only delivery/judgment receipts
read-only. Neither W09 nor the Mac holds the Telegram token, and no second
runnable entrypoint may read it. W09 gains no S3/capture/production write merely
to make the feed work. The exact one-way event/spool handoff and reverse receipt
handoff must be named, bounded and audited in W-HYPO-FEED-01; until frozen, the
feed is `BLOCKED_DEPENDENCY`. A second `getUpdates` process is forbidden: the
documented 2026-07-16 duplicate-consumer 409/lost-`/ack` incident is a mandatory
regression sentinel.

Before any Telegram API call, the supervised sole gateway must acquire a
monitor-owned 0600 `flock`/lease. A loser exits before send, read, offset update
or `getUpdates`; stale-lock/restart semantics are explicit. Systemd
`Restart` is supervision, not the lock. Token placement is restricted to that
one locked gateway service on the production monitoring host, so the host lock,
file permissions and tools-registry reachability proof cover the entire
consumer set. A two-process race proves exactly one process reaches the API;
the credential-free Mac spool cannot call `sendMessage` or `getUpdates`.

The sole gateway must durably journal an accepted update **before** advancing
and fsyncing its persistent Telegram offset; startup may not skip an unseen
backlog. Current `status_bot.py` keeps its offset only in memory and current
`tg_common.write_json()` lacks the required append-only/fsync/0600 receipt
contract, so neither is compliant without W-HYPO-FEED-01 changes. The future
release needs `credential_use=true`, `external_account_actions=true`, an exact
`api.telegram.org` network allowlist and explicit component-level
`telegram_send_permission` / `telegram_update_read_permission` only for the
one named gateway service. It keeps `paid_api_access=false`,
`live_order_permission=false` and repository `push_permission=false`, and grants
no trading credential or client. Because the sole gateway persists its inbound
spool state, outbox/delivery receipt, judgment journal and update offset on the
production monitoring host, its future release must set
`production_mutation=true` **only** for those exact monitor-owned paths; that
flag grants no code, capture, ingest, seal, account or order mutation.

GUARDRAILS.E3 also applies: every new runnable producer, publisher, dispatcher,
consumer/service and test entrypoint is registered in `tools.json`, and
`check_registry` reverse coverage includes the named deployed entrypoints.
Credential-free render/producer tools may be `pure`/`offline`. Under the current
four-class registry, Telegram send/update and external journal mutation round
up to `live_order` solely as a conservative **console-forbidden safety class**;
that label grants no live-order permission. A future narrower class would need
its own audited GUARDRAILS/registry/console change first. No credentialed
Telegram entrypoint may appear on a console run surface.

Delivery is durable-at-least-once: a local outbox is written before network
send; acknowledgment stores Telegram message ID, content SHA and time; retries
reuse the stable visible `feed_event_id`. A crash may duplicate a message but
cannot lose or reinterpret the event. Feedback deduplicates by update/judgment
ID. `RESEARCH_FEED_COMPLETENESS.json` independently derives every required
message-part tuple and proves equality with logical delivered parts on:

~~~text
(feed_event_id, source_receipt_sha, payload_sha,
 rendered_content_sha, outbound_schema_version, part_index, part_count,
 sealed_batch_id_or_null, logical_event_ids_sha_or_null,
 telegram_transport_kind)
~~~

Telegram message ID/time attach to the matching delivered tuple. Retries may
produce more than one transport receipt for the **same** tuple, but conflicting
hashes for one event ID, stale/wrong body, schema mismatch, missing/duplicate
part index, wrong part_count, orphan receipt or truncated content is exact-red.
For SEALED_COMPLETION_TRANSPORT, both logical events map to the same batch,
content, one-part transport and Telegram message ID; a receipt for only one is
exact-red. The completeness artifact also binds `FEED_DESIGN_RECONCILIATION`,
`independent_event_enumerator_code_sha`, `input_source_manifest_sha`, the event-
obligation-table SHA, independently enumerated expected/feed-event-set SHAs and
source-union equality. Telegram failure never blocks capture/ingest or touches
trading, but an undelivered or mismatched materialized idea cannot enter
selection/priority, and an affected design/result blocks RUN_AUTHORIZED,
promotion and Deep03 `PROCESS_COMPLETE` until corrected. Unrelated research
whose complete feed receipts already exist may continue.

The new producer/publisher/reply-handler modules have only their named research-
ledger/outbox/receipt write roots and Telegram destination. They import no
trading/order client, read no trading credential, call no exchange/AWS endpoint,
start no research job and have no S3/capture/ingest write path. The host status
bot may retain its separately authorized `/ack` read-only probes, but the
research-reply dispatch path cannot invoke them. Secrets remain outside the
repository. Required offline tests cover message cardinality, multipart
reassembly, retry/outage, crash-before/after-send, duplicate/out-of-order
updates, stale revision, unauthorized sender/chat, command/free-text injection,
holdout redaction, judgment acknowledgment and proof that every research reply
is non-executing.

---

## 5. Shared data, clock and contract

### 5.1 Required inputs

Per admitted release/card:

- exact release ID and object VersionId;
- object key, size, ETag/hash and row count;
- L1/orderbook, trades and, where required, sequence-valid L2;
- catalog/series/event/market metadata;
- root-event mapping version;
- scheduled-start source and as-of timestamp;
- fee type/multiplier/effective date/event override;
- authoritative payout and lifecycle rule semantics, including
  settlement/void/cancellation/retirement/walkover/postponement, for every
  economic row; realized terminal outcome additionally where required;
- declared gaps, reconnect/recovery intervals and capture-quality label.

`SEALED` or readable does not automatically mean profit-qualified.

### 5.2 Clock and as-of invariants

- Decision clock: `local_recv_ts_us` (or higher-resolution receive clock).
- Deterministic order: `(recv_ts, sid, seq, source_position)`.
- Same-timestamp ambiguity resolves against the strategy.
- Every feature field records `source_ts <= decision_ts`.
- Outcome book must satisfy `decision_ts < outcome_book_ts <= target_ts`.
- Every horizon reports non-null effective `n`, not `count(*)`.
- Gaps/reconnects/epoch boundaries terminate or invalidate episodes under one
  rule frozen before VALIDATION.
- Same input replays bit-identically except allowed generation timestamps.

Any future-as-of sentinel row makes the affected method
`METHODOLOGY_INVALID`.

### 5.3 Book-state duration rule

A book/one-sided state survives only until the earliest of:

1. next relevant update;
2. frozen staleness TTL;
3. capture gap/reconnect;
4. market pause/close/terminal lifecycle;
5. date/release boundary.

The last observed row is right-censored and never extended to a sport/category
day end. This explicitly corrects the deep02 one-sided duration defect.

### 5.4 Root-event contract

- All related markets for one sports event share one inference/risk root.
- Main denominator includes eligible zero-quote/zero-fill roots with zero PnL.
- One root cannot be counted multiple times for sample size or capital.
- The binding PM policy activates at most one market per root.
- Selection tie-break is deterministic and frozen before VALIDATION.

Each family freezes its base-root universe before observing whether its trigger
later occurs. `U_PM` does not require a later two-/one-sided episode; `U_T90`
does not require later entry into 88–93 or a tug/retreat; `U_CS` does not
require later entry into 97/98. Primary `mu_event` includes every base root,
with `Pi=0` when no trigger, quote or fill occurs. Trigger-conditional results
are secondary. Paired deltas use the identical frozen base-root universe, never
a post-trigger/post-fill survivor intersection.

---

## 6. Shared execution, fee and PnL contract

### 6.1 Current repository implementation red lights

The strategy prose below is **not implemented by the current live/shadow
foundations**. The audit must verify these observations against the frozen code
SHA rather than assuming an existing engine can be reused:

| Observed code surface | Current limitation | Consequence |
|---|---|---|
| `include/kalshi/strategy.hpp:15-31` | `ExecSink` exposes create-like `submit` only; no cancel/amend/reduce/reconcile contract | A policy cannot express the lifecycle specified below |
| `include/kalshi/wire.hpp:163-193` and `apps/tradingd.cpp:294-295` | `order_json` defaults `post_only=false`; this call site does not pass `true` | Current create path is not a binding post-only maker path |
| `include/kalshi/wire.hpp:43-55,163-193` | payload uses integer contracts/cents and generated IOC lacks an independently verified reduce-only/cancel-on-pause/order-group contract | Cannot replay current E4/fractional quantity/price or trust caller-declared exit safety |
| `apps/tradingd.cpp:294-329` | sends a create request and telemetry but establishes no persistent order manager/private fill state in this path | Cannot prove resting, partial, cancel-effective or unknown-order state |
| `include/kalshi/rule_engine.hpp:108-115,164-169` | disconnect/cancel intent erases the local resting entry immediately | Contradicts required cancel-pending exposure |
| `src/gateway.cpp:73-91` | absent/invalid `orderbook_delta.side` falls through to YES | Unknown orientation can become a false valid book instead of fail-closed |
| `src/gateway.cpp:107-147` | missing/invalid trade aggressor can retain a default side; lifecycle enum mapping is not yet proven against every saved-v2 fixture | Strict-fill direction or market state can be fabricated |
| `include/kalshi/risk_ledger.hpp:148-213` | caller can assert `reduces_risk`; reservation `settle` closes a slot once | Repeated partials/cancel-race leaves require a new slice-aware reserve lifecycle |
| `sandbox/research/mm_sandbox.py` | a qualifying through event can consume the whole remaining simulated order; terminal accounting is diagnostic | Does not yet implement authoritative public-size bound or binding ledger |

Additional P0 capabilities not evidenced in that path are: private
ack/reject/cancel/fill ingestion, duplicate suppression across reconnect,
order/position REST reconciliation, atomic worst-case risk reservation,
exchange-side `cancel_on_pause`/order-group protection, E4 quantity/price,
account-precision fee accumulator and measured effective cancel latency.

Therefore W-FS1 completion and golden tests are hard prerequisites. Reusing
current output without those changes yields `METHODOLOGY_INVALID`, even if
`make check` is green.

### 6.2 Required policy and order state machines

Policy state:

```text
UNMAPPED -> WARMUP -> ELIGIBLE -> QUOTING
                                  |     \
                                  |      -> INVENTORY
                                  v              |
                           CANCEL_PENDING <------+
                                  |
                    ZERO_RESTING + RECONCILED
                                  v
                                DRAIN
                                  |
                          FLAT -> COOLDOWN -> ELIGIBLE/OFF

STALE / PAUSED / KILL -> desired=NO_QUOTES -> CANCEL/RECONCILE/DRAIN
Any uncertain order/position state -> RECONCILING -> ZERO_RESTING or DISABLED
CLOSED only after all orders terminal/reconciled and flat, or residual booked
```

Per-order state:

```text
PLANNED -> PENDING_NEW -> ACTIVE / PARTIAL / FILLED / REJECTED / UNKNOWN
                          |         |
                          +---------+-> CANCEL_REQUESTED
                                           |
                  CANCEL_ACKED / PARTIAL_CANCEL_PENDING / FILLED / UNKNOWN
                         |             |
                         +-------------+-> PARTIAL_CANCEL_PENDING / FILLED /
                                           CANCELED_EFFECTIVE / UNKNOWN
UNKNOWN -> RECONCILING -> any authoritative current state above
```

Every transition has a machine-readable reason and source-event identifier.
An order may fill before create ACK and may partial/full-fill after cancel
request or cancel ACK. HTTP 2xx/`CANCEL_ACKED` proves only request acceptance.
`CANCELED_EFFECTIVE` requires an authoritative private terminal update or REST
reconciliation whose cumulative fills and remaining quantity balance. A
replacement may activate only after the old order is terminal/reconciled plus
the new placement latency. Unknown order/position state admits no new quote,
cannot reach `CLOSED`, and cannot produce `RUN_COMPLETE`.

### 6.3 Deterministic event loop

For timestamp `t`, the binding replay order is:

```text
1. apply scheduled actions with effective_ts < t;
2. apply the next public/private/lifecycle input at t in frozen tape order;
3. dedupe by authoritative event/order/fill ID;
4. update book, lifecycle, acks/cancels and the selected fill authority;
5. apply exactly one source of fills and enforce caps after EACH fill slice;
6. advance timers, staleness, loss limits and kill conditions;
7. if any state is unknown: enter RECONCILING and request no new risk;
8. compute the policy's desired order set from past/current admissible state;
9. validate orientation, post-only, collateral, reserve and write budget;
10. diff desired versus working set: cancel first, replace only when terminal;
11. schedule newly admitted actions with their measured effective latency;
12. actions whose effective_ts == t occur only AFTER the external input at t.
```

Same-time ambiguity is therefore adverse to the strategy. Features are updated
before the policy decision but cannot consume a future event. The simulator
must replay public market data and private order/fill state on one deterministic
timeline; a public-only “would have quoted” loop is not an execution result.

Each run freezes exactly one `fill_authority`:

- `PUBLIC_COUNTERFACTUAL`: public strict-through evidence synthesizes fills;
  private fills cannot change cash/position;
- `PRIVATE_AUTHORITATIVE`: only deduped private fills change cash/position;
  public trades remain features/markouts and cannot synthesize a second fill.

The modes never mix. The engine receipt binds the mode, source IDs and dedupe
rule. Own-order calibration must use a disjoint dataset or a separately
registered private-authoritative run.

TRAIN also freezes this stage mapping:

```text
HISTORICAL_REPLAY       -> PUBLIC_COUNTERFACTUAL_STRICT_THROUGH
SHADOW_SIMULATION       -> PUBLIC_COUNTERFACTUAL_STRICT_THROUGH
MICROLIVE_CALIBRATION   -> PRIVATE_AUTHORITATIVE
```

`decision_policy_fingerprint` is identical across those stages;
`execution_environment_fingerprint` and run ledger are stage-specific.
Micro-live private fills compare simulator adequacy and safety only: they never
replace the historical economic ledger, recompute confirmed PnL or inherit a
public fill claim as private truth. Changing this mapping starts a new lineage.

On every partial or full fill, binding V0 performs the conservative containment
sequence:

```text
STOP_NEW_RISK
-> cancel every sibling/risk-adding order
-> preserve their fillability while CANCEL_PENDING
-> reconcile exact working orders and position
-> after zero risk-adding orders, place only exact-position REDUCE_ONLY action
-> return to FLAT/COOLDOWN or force exit
```

This prevents a supposedly “reducing” opposite full order from filling past
flat and flipping inventory. More permissive matched-pair inventory management
is a future diagnostic, not the initial binding policy.

Reservation accounting is slice-aware: price/quantity retain exact
`PriceE4`/`CountE4`, while every monetary product/liability uses `MoneyE6` or a
wider exact integer/rational with no narrowing. On each partial fill, one atomic
transaction converts that filled slice from order reserve to position liability
while preserving worst-case reserve for every unfilled
leaf through cancel-pending. Unfilled reserve is released only after the order
is terminal and cumulative fills reconcile. Repeated partials must not call a
one-shot whole-order `settle`. Order reserve, fee accumulator, leaves, fills
and position survive process restart; duplicate/out-of-order fills advance none
of them twice. `REDUCE_ONLY` is derived from authoritative position, outcome
side and quantity (`qty <= abs(position)`), never trusted from a caller flag.

### 6.4 Two-sided placement and action-orientation contract

Two-sided placement is non-atomic. Before either side is scheduled, reserve
collateral/inventory for the worst sequence in which both create requests are
accepted and both fill before the next book event. The ledger must record
`PAIR_FULLY_ACTIVE`, `PAIR_HALF_ACTIVE`, `PAIR_ORPHAN_REJECT`, and
`PAIR_CANCEL_RACE`; failure of either create immediately cancels the other but
does not pretend that cancellation was instant.

All strategies define an **economic outcome side** first, then map it to venue
action and YES-normalized book side. Required golden cases:

| Economic intent | Venue instruction | YES-normalized resting side |
|---|---|---|
| buy YES | `buy/YES @ p` | bid at `p` |
| sell YES | `sell/YES @ p` | ask at `p` |
| buy NO | `buy/NO @ q` | YES ask at `1-q` |
| sell NO | `sell/NO @ q` | YES bid at `1-q` |

“High-side ASK” in CS is an economic sell of the frozen high-price outcome;
from flat it is implemented as the collateral-valid buy of the opposite
low-price outcome, not assumed naked `sell YES`. Missing/unknown side, action,
complement or leader identity is fail-closed.

### 6.5 Three fill tracks

1. **Binding strict-through**: after quote activation, a correctly oriented
   public trade/executable sweep must trade strictly through the active price.
   An at-price print never fills. Fill size is bounded by remaining order and
   authoritative public sweep/trade evidence. Same-time ambiguity is adverse.
2. **Queue-aware diagnostic**: starts behind displayed quantity and consumes
   only observed qualifying volume; never gate-bearing without disjoint
   own-order calibration.
3. **Optimistic diagnostic**: at-touch upper bound; never a go/no-go result.

Only strict-through can support `BACKTEST_CANDIDATE`.

### 6.6 Fee model

For a fill whose verified `formula_id` is quadratic, first compute:

```text
trade_fee = ceil_to_$0.0001(
    rate(fee_type, maker_or_taker)
    * fee_multiplier
    * contracts
    * price_dollars
    * (1 - price_dollars)
)
```

Dispatch by exact series/date/event `formula_id`; flat, tiered or future fee
types use their own versioned implementation and golden fixture. Unsupported or
unknown formula IDs fail closed rather than falling through to quadratic.

That is not yet the complete venue cash flow. Bind the verified account class
and target balance precision (`$0.0001` direct member; `$0.01` non-direct under
the observed fee-rounding contract), then for signed fill revenue:

```text
pre_round_balance_change = signed_revenue - trade_fee
floored_balance_change = floor_toward_negative_infinity(
    pre_round_balance_change, target_balance_precision)
rounding_fee = pre_round_balance_change - floored_balance_change
order_accumulator += rounding_fee
rebate = whole-$0.01 amount emitted by the versioned accumulator rule
net_fee = trade_fee + rounding_fee - rebate
```

The accumulator is keyed by order ID, persists across all partials/reconnects
and maker/taker transitions, and advances once per deduped fill. A duplicate or
out-of-order replay cannot emit another rebate. Formula lookup includes series,
effective date, event override, fill role, actual E4 price/quantity and fee
version; rounding lookup includes verified account class/precision and
accumulator version. External incentives count only when contractually verified
and actually realized, separately from venue rounding rebate.

Unknown account precision, fee override or accumulator semantics is
fail-closed for gate-bearing economics. A deliberately conservative assumed
precision may be reported only `NON-GATE`. Every report reconciles
`trade_fee + rounding_fee - rebate = net_fee` by fill, order and root.

### 6.7 Latency model

- Placement, cancel and forced-exit latencies are separate distributions.
- Binding values must be `MEASURED(date, host, n, path)`.
- While any value is placeholder/conservative-only, run a diagnostic matrix
  `{5, 50, 500} ms` plus current conservative config; label all profit results
  `NON-GATE`.
- Required stress: measured baseline, ×1.5 and ×2; cancel p99×2 in the combined
  stress.
- A market regime is rejected when adverse movement regularly precedes the
  cancel-effective budget.

### 6.8 Forced-exit algorithm

A stop/terminal signal never teleports a position to midpoint:

- entry/passive-reduce: `GTC`, `post_only=true`, verified
  `cancel_on_pause=true` and, where available, a versioned order group;
- forced exit: `IOC`, `post_only=false`, engine-verified `reduce_only=true`.

Illegal combinations such as `IOC + post_only` fail validation.

```text
1. stop every new risk-increasing action;
2. send cancels for all working orders and retain their possible exposure;
3. process all public/private events until cancel-effective or timeout;
4. reconcile open orders and exact position;
5. while any risk-adding order is nonterminal OR its remaining quantity and
   cumulative fills are not authoritatively reconciled (including PENDING_NEW,
   ACTIVE, PARTIAL, CANCEL_REQUESTED, CANCEL_ACKED,
   PARTIAL_CANCEL_PENDING or UNKNOWN): send no IOC and remain RECONCILING;
6. if the reconcile deadline expires unknown, value current position PLUS all
   possible pending fills under the joint worst state, mark run red, and stop;
7. otherwise schedule a bounded IOC with qty <= authoritative abs(position);
8. at IOC effective time, read then-current sequence-valid L2 (never send-time
   depth), fill only against that depth, charge exact taker net fee/slippage;
9. after each IOC fill, reconcile position before deriving the next quantity;
10. stop at the frozen round/time/reserved-exit-token cap;
11. value any residual by the joint worst legal terminal state, or by actual
   settlement only where that terminal contract is complete;
12. prove cash + position + trade/rounding/rebate fees + collateral conservation.
```

The report separates passive inventory reduction, forced IOC fill, unfilled
residual and cancel-pending loss. An exit action cannot be suppressed merely
because the position cap or normal write budget is reached. Cancels and exits
use reserved emergency tokens; exhaustion itself is a hard incident/red test.

### 6.9 Pathwise event PnL

For root event `e`:

```text
NetPnL_e
= executed cash inflows
- executed cash outflows
- exact venue fees
- distinct realized non-venue variable costs
+ TerminalValue_e(remaining inventory under frozen terminal policy)
```

Price and count use their exact E4 types; cash, collateral, fee components,
terminal value and NetPnL use `MoneyE6` or a wider exact integer/rational, never
E4 narrowing or float. Fill, fee, unwind and terminal flows enter once.
Attributions (spread, adverse selection, cancel-pending, forced exit, outcome)
must reconcile exactly and are not subtracted twice.

Primary estimand:

```text
mu_event = mean(NetPnL_e over all eligible roots)
```

Fill-conditional PnL, total PnL and raw trade-row means are secondary.

### 6.10 Shared research-size and risk rules

- Binding displayed size: 1 contract per active side.
- Binding absolute inventory: at most 1 contract per root event.
- More size is `CAPACITY_DIAGNOSTIC_ONLY` until a new operator ladder.
- Caps are checked after every fill, including multiple fills between books.
- Inventory-reducing action is never suppressed at the cap.
- No replenishment/martingale on CS or PM-OS.
- Root/day/factor loss caps are `[OPERATOR-TBD]`; proposed numbers in discovery
  material are not authority.

### 6.11 Minimum frozen execution config

The implementation must accept one versioned, schema-validated config. Unknown
keys and missing required values fail closed. At minimum it binds:

```yaml
identity: {run_id, policy_id, card_id, lineage_id, super_family_id,
           program_family_id, superfamily_assignment_sha,
           decision_policy_fingerprint, economic_replay_fingerprint,
           research_stage, code_sha, dirty_diff_sha, config_sha}
input: {release_id, object_version_ids, manifest_sha, receive_clock,
        deterministic_tie_break, gap_policy, lifecycle_version,
        fill_authority, execution_environment_fingerprint}
market: {root_map_version, scheduled_start_source, tick_table_version,
         fee_facts_sha, fee_formula_id, account_class, target_balance_precision,
         fee_accumulator_version, terminal_contract_version,
         allowed_series, allowed_phases}
execution: {fill_authority_by_stage_sha, strict_fill_evidence,
            at_price_fill: false,
            public_size_bound, e4_price_quantity: true,
            entry: {tif: GTC, post_only: true, cancel_on_pause: true},
            passive_reduce: {tif: GTC, post_only: true,
                             reduce_only: verified, cancel_on_pause: true},
            forced_exit: {tif: IOC, post_only: false, reduce_only: verified},
            cancel_before_replace: true,
            same_timestamp: adverse, private_event_dedupe,
            reconcile_timeout, action_retry_cap}
latency: {placement_manifest, cancel_manifest, ioc_manifest,
          baseline_quantile, stress_multipliers}
risk: {size_per_side: 1, max_abs_root_inventory: 1,
       max_active_markets_per_root: 1, root_loss_cap, day_loss_cap,
       collateral_reserve_mode: slice_aware_worst_sequence,
       write_budget, reserved_cancel_exit_tokens}
terminal: {normal_cutoff, drain_cutoff, ioc_round_cap, ioc_time_cap,
           residual_policy, terminal_contract_version,
           realized_outcome_required}
statistics: {split_sha, registry_sha, global_selection_ledger_sha,
             superfamily_alpha_graph_sha, program_test_ledger_sha,
             alpha_allocation_ledger_sha, alpha_allocation_id,
             allocation_claim_receipt_sha, allocation_state,
             test_opening_id,
             opening_alpha, cumulative_superfamily_alpha_before,
             cumulative_superfamily_alpha_after, timezone_calendar,
             multi_day_event_assignment, bootstrap_family, block_length_rule,
             bootstrap_reps, deterministic_seeds, studentization,
             empty_block_handling, adjusted_alpha, target_power,
             delta_min_abs_by_policy, delta_min_increment_by_variant,
             epsilon_control_by_control, required_blocks, stopping_rule}
```

For TRAIN/VALIDATION/CONFIRMATION, policy-specific blocks may add only frozen
registered values. OPEN_DISCOVERY may append a new proposed value only to the
global discovery ledger; that value cannot be used on a sealed cohort and must
start a new card/lineage before promotion. The run aborts if loaded hashes
differ from registration or if an `[OPERATOR-TBD]` field remains unresolved
for confirmation.

---

## 7. Shared features and admission tests

### 7.1 `F-FLOW-TOX-01` — toxicity no-quote feature

Proposed inputs, all past-only:

- rank of message count over 100 ms;
- rank of absolute signed traded quantity over 1 s;
- rank of absolute log-odds midpoint move over 1 s.

Proposed score:

```text
toxicity = max(rank_msg_100ms, rank_signed_qty_1s, rank_abs_lo_move_1s)
```

Threshold family `[TRAIN-FREEZE] = {p90, p95, OFF}` within frozen
sport × phase × liquidity strata. Admission requires predictive increment over
a simple spread/activity baseline, paired economic increment in its owner
policy, latency headroom and negative-control survival. Otherwise
`MONITOR_ONLY` or `REJECT`.

### 7.2 `F-RETREAT-01` — L2 retreat safety feature

Candidate definition for audit, not an existing calibrated threshold:

- `D_bid`, `D_ask`: leader-oriented cumulative depth over the first three
  legal price levels;
- `S`: spread in legal ticks;
- baseline: time-weighted median over `[t-30s, t-3s]`, requiring ≥10 seconds
  valid coverage.

Candidate flags:

```text
R1: D_bid <= 0.40 * baseline_D_bid
R2: D_ask <= 0.40 * baseline_D_ask
R3: S >= max(baseline_S + 1 tick, 1.50 * baseline_S)
RETREAT = any 2 of {R1, R2, R3}
```

These numeric values are `[PROPOSED]` and must be audited/calibrated on TRAIN.
The trigger itself uses only current/past evidence; future burst labels are
outcomes, never trigger inputs.

Admission as `RISK_SAFETY` requires:

- lead-time distribution versus measured cancel-effective latency;
- matched non-retreat controls with balance diagnostics;
- lower tail improvement, false-kill opportunity cost and paired mean PnL;
- future-shift/label-shuffle/snapshot-reset negative controls;
- a frozen recovery/hysteresis rule.

### 7.3 `F-OCCUPANCY-01` — market-selection feature

Components:

- two-sided uptime;
- touch/near-touch share;
- post-trade requote delay;
- quote-reset rate;
- coordinated retreat frequency;
- executable flow and spread.

No “professional MM” label exists. The feature can become a selector only if
it adds out-of-fold paired economic value over simple volume + spread + active
minutes. Otherwise it remains a report/dashboard field.

### 7.4 Blindness cost

Estimate only:

- losses avoided by `F-RETREAT-01`;
- remaining losses an oracle/future score would avoid;
- false-pause opportunity cost;
- maximum economically rational external-data cost (EVPI range).

No provider, paid call, account or purchase is authorized.

---

## 8. Card DR3-PM-TS-01 — pre-match two-sided selective maker

### 8.1 Falsifiable statement

In eligible standard Sports markets before scheduled start, a one-contract
selective two-sided post-only policy has positive strict-through event-level
NetPnL after exact fees, order lifecycle, cancel-race exposure, inventory
liquidation and joint worst-state residual valuation, compared with eligible
no-quote events.

### 8.2 Population and unit

- All captured Sports enter discovery; no sport is preselected.
- Standard compatible binary CLOB; MVE/combo excluded.
- Reliable scheduled start, payout/lifecycle/void/cancel/retire/postpone rule
  semantics and fee/account-precision class known.
- Proposed primary time window: `15 minutes <= TTS <= 6 hours`.
- `0–15m` and `6–24h` are mandatory diagnostic controls, not pooled.
- Valid two-sided, nonlocked, noncrossed book.
- One deterministic selected market per root event.
- Unit: complete root sports event.

### 8.3 Entry economics

```text
required_capture_margin
= modeled matched spread capture
- exact expected maker net fees (formula + rounding - rebate)
- conditional executable adverse-selection bound
- cancel-race bound
- executable unwind bound
- inventory-risk bound
```

Quote only when the margin exceeds the frozen cost-safety buffer and every
eligibility gate is true. All components are in log-odds for model decisions,
then mapped outward to legal venue prices; accounting remains MoneyE6-or-wider
exact fixed-point cash.

TRAIN estimates the mutually exclusive path probabilities `{no fill,
bid-only, ask-only, both, orphan/cancel-race}` with out-of-fold features. The
entry score is the lower bound of complete pathwise NetPnL across those paths,
not raw spread minus an average fee. Per root, choose the eligible market with
the largest frozen risk-adjusted score; ties use lower worst-state loss, then
lexicographic market ID. Triggering later is not required for base-universe
membership.

### 8.4 Baseline policy

- Warm-up: `[PROPOSED] 120s` after start/reconnect/snapshot reset.
- Book TTL primary: `[PROPOSED] 1s`.
- Activity: trailing-60s update count ≥ TRAIN stratum p75.
- Raw spread state persists `[PROPOSED] 1s`.
- Cost-safety buffer: `[PROPOSED] 1 legal tick`.
- Flat: join current bid and ask, post-only, one contract each; the two create
  requests are non-atomic and use the shared worst-sequence reserve/orphan rule.
- Any partial/full fill: stop new risk, cancel all siblings, retain their
  cancel-pending exposure, reconcile, then submit only exact-position
  reduce-only action under §6.3.
- Normal quote age: `[PROPOSED] 5s`.
- Reprice when desired fair differs by ≥2 ticks; 1-tick move waits until age.
- Normal requote cooldown: `[PROPOSED] 1s`.
- Stop new risk at TTS 15m; passively drain until TTS 5m; then force unwind.
- Unliquidatable residual receives joint worst-state value.

### 8.5 Kill conditions

- fee/account precision/start/payout/lifecycle/tick/root mapping becomes unknown;
- gap/reconnect/stale/locked/crossed/pause;
- cost margin drops below buffer;
- toxicity gate trips;
- post-only would cross;
- inventory/write-token/latency/risk headroom fails;
- final cutoff is reached.

### 8.6 Finite sequential TRAIN registry

No Cartesian grid. Start with the most conservative value and relax one
dimension. Keep at most one simplest survivor at each step. Maximum actual
candidate evaluations: 23.

| Step | Candidate set | Initial conservative value |
|---|---|---|
| PM_STEP_01 book TTL | `{250ms, 1s, 5s}` | 250ms |
| PM_STEP_02 minimum raw spread | `{4, 3, 2}` legal ticks | 4 |
| PM_STEP_03 spread dwell | `{5s, 1s, 0}` | 5s |
| PM_STEP_04 max quote aggressiveness | `{behind1, touch, improve1, improve2}` | behind1 |
| PM_STEP_05 cost buffer | `{2, 1, 0}` ticks | 2 |
| PM_STEP_06 toxicity | `{p90, p95, OFF}` | p90 |
| PM_STEP_07 adverse-width bound | `{conditional p90, p75, mean}` | p90 |
| PM_STEP_08 fair | `{lo midpoint, lo microprice, microprice+past flow}` | midpoint |
| PM_STEP_09 inventory skew | `gamma={0, 0.5, 1.0}` tick-distance/contract | 0 |
| PM_STEP_10 requote | `R0 safety-only; R1 3ticks/30s/5s; R2 2ticks/10s/2s; R3 1tick/2s/500ms` | R0 |

Selection rule `[PROPOSED]`: require risk non-inferiority and latency/token
headroom first; then maximize nested-TRAIN paired `ci95_lower(Delta NetPnL_e)`;
differences below `[OPERATOR-TBD Delta_min]` select the simpler/safer version.

### 8.7 Controls and outcomes

- `NO_QUOTE=0`;
- matched random eligibility;
- DP-0 static touch;
- cumulative DP-1 toxicity → DP-2 width → DP-3 fair → DP-4 inventory → DP-5
  requote;
- no-signal, fair-only, add-one and full-minus-one diagnostics;
- sign/side flip, root shuffle, unrelated event and future-shift sentinels;
- delayed execution and latency stresses.

Primary gate: absolute strict-through `mu_event` and `ci95_lower`. Every complex
step also reports paired `Delta_e` versus its immediate simpler parent.

### 8.8 Reopen/kill

- Reject if powered strict PnL is nonpositive, only optimistic/queue is
  positive, or outcome dependence is material.
- Collect if clock/start/fill support or independent blocks are insufficient.
- Label `DIRECTIONAL_OR_INVENTORY_DEPENDENT` if deterministic liquidation removes
  profitability or final outcome drives it.

---

## 9. Card DR3-PM-OS-01 — pre-match one-sided liquidity

### 9.1 Role and hypothesis

This is an incremental PM policy, not proof that one-sided books lack a
professional maker. In active pre-match non-extreme markets, supplying the
missing side from a fresh prior two-sided reference may improve event PnL over
the matched PM baseline after exact forced-exit costs.

Current scope is `DIAGNOSTIC_PAIRED_ABLATION`. It cannot become a confirmatory
policy unless a durable ruling explicitly expands the canonical two-sided
primary, followed by a separate exact W/phase release.

### 9.2 Proposed population

- `15m <= TTS <= 240m`;
- normalized E4 price `[10c,90c)`; `[90c,97c)` is seed-unrouted,
  `[97c,99c)` routes to CS proposal and `[99c,100c)` is queue-only;
- corrected one-sided state persists 30s;
- decision-time surviving-side book age ≤1s;
- at least five updates in 60s and one trade in 5m;
- not a final censored row, pause, empty market, gap or terminal state;
- at one-sided **onset**, a valid two-sided midpoint observation is ≤1s old;
  freeze it as `reference_mid_onset` and record its elapsed age at entry;
- throughout persistence, the observed surviving side may not move more than
  one legal tick away from its onset value; otherwise the episode ends.

Thus “reference age ≤1s” is evaluated at onset, not impossibly again after 30s
of one-sided persistence. A synthetic fixture must prove OS0 has a nonempty
eligible path before any data run; the report stratifies by elapsed anchor age.

### 9.3 Quote rules

```text
if bid exists and ask is missing:
    ask = max(bid + k*tick, ceil_outward(reference_mid_onset + 1*tick))

if ask exists and bid is missing:
    bid = min(ask - k*tick, floor_outward(reference_mid_onset - 1*tick))
```

- Binding `k=2`; `k=1` is one sensitivity.
- One contract, one fill maximum per root, no replenish.
- Quote age 30s.
- Cancel if missing side returns, surviving side moves >1 tick from onset,
  activity bursts, rule semantics become unknown, gap/pause occurs or TTS
  reaches 15m.
- After any partial/full fill, execute the shared contain-cancel-reconcile
  sequence; only then try a passive exact-position reducing exit.
- Binding max hold 120s; 30s is one sensitivity.
- On timeout, force exit at executable depth with taker fee/slippage.
- Binding residual uses explicit legal joint worst state; realized settlement
  is a secondary diagnostic when available, never a midpoint substitute.

### 9.4 Finite variants

- `OS0`: persistence30s / k2 / hold120s (binding proposal).
- `OS1`: persistence5s only (diagnostic false-opportunity sensitivity).
- `OS2`: k1 only.
- `OS3`: max-hold30s only.

No cross-product selection.

### 9.5 Co-primary gate

Both must pass:

1. absolute strict-through `ci95_lower(mu_event_OS) > 0`;
2. paired same-root/common-random-number
   `ci95_lower(NetPnL_OS - NetPnL_PM_parent) > 0`.

If worst-state residual fallback exceeds `[TRAIN-FREEZE limit]`, or forced exit consumes
more than `[TRAIN-FREEZE share]` of gross capture, the short-hold strategy is
not deployable. The auditor must reject any plan that silently uses the old
deep02 duration percentages.

---

## 10. Card DR3-T90-01 — Tennis 88–93 range maker

### 10.1 Scope label

Until a durable canonical-tier ruling expands post-scheduled confirmation and
a specific W/phase release admits the run, this card is
`EXPLORATORY_DIAGNOSTIC_POST_SCHEDULED_START_PROXY`. It does not amend the
canonical pre-match primary by existing merely in this plan.

### 10.2 Falsifiable statement

In sequence-valid Tennis match-winner markets, when a causally observed leader
price is in 88–93c, a recent two-way 90c tug is already present and no admitted
L2 retreat exists, one-contract passive range quoting has positive strict
event PnL and retreat-based cancellation improves tail risk.

### 10.3 Normalized price and episode

```text
p_mid = expit((logit(best_yes_bid) + logit(best_yes_ask)) / 2)
leader = YES if p_mid >= 0.5 else NO
leader_mid = max(p_mid, 1 - p_mid)
```

Freeze leader identity for the episode. Entry band `[0.88, 0.93]` is inclusive.
A qualifying prior tug uses 1c hysteresis over the past 120s:

```text
up-cross   = <=0.895 then >=0.905
down-cross = >=0.905 then <=0.895
```

At least one of each is required before entry. “Ever touched 90 later” is
forbidden.

### 10.4 Proposed eligibility

- Tennis match-winner with authoritative root/orientation/tick/fee/account
  precision/payout/lifecycle rule semantics;
- `POST_SCHEDULED_START_PROXY` reported separately from pre-start;
- open, unpaused, two-sided, sequence-valid L2;
- book age ≤500ms `[PROPOSED]`;
- both first-three-level depths ≥1 contract;
- past-30s valid book-time coverage ≥80%;
- fee-adjusted pair margin ≥0.50c `[PROPOSED]`;
- no retreat and inventory flat.

### 10.5 Quote and inventory

- Construct outcome-specific `leader_bid/leader_ask`; for `touch`, join them;
  for `behind1`, bid one legal tick lower and ask one legal tick higher. Map
  YES/NO to the normalized book only after prices are frozen (§6.4).
- `offset ∈ {touch, behind1 legal tick}`.
- One contract per side; `|root inventory| <= 1`.
- Flat -> non-atomic two-sided placement with worst-sequence reserve.
- Any fill -> stop, cancel siblings, reconcile, then exact-position reducing
  side only; an old opposite full order may not remain and flip inventory.
- Desired move ≥2 ticks cancels immediately; 1 tick waits for age ≥1s.
- Normal requote minimum interval 250ms `[PROPOSED]`.
- Cancel-before-replace; old quote remains exposed.

### 10.6 Retreat kill — no parent re-entry

Use the single versioned `F-RETREAT-01`. On trigger:

```text
NO_NEW_QUOTES -> CANCEL_PENDING -> ZERO_RESTING -> DRAIN
```

No rebuild quote can coexist with kill/cancel pending. The base T90 policy does
**not** re-enter after its first valid retreat kill: after reconciliation and
drain it transitions to `OFF_FOR_ROOT`. Every post-kill re-entry belongs only
to the separate T90-RB treatment below. This makes the paired parent
`T90 never re-enter` reproducible.

### 10.7 Forced exits

- band exit;
- leader identity change;
- original leader executable midpoint ≤80c `[PROPOSED hard-flip]`;
- inventory timeout;
- retreat/gap/stale/pause/terminal;
- `[OPERATOR-TBD]` root loss cap.

After cancel effectiveness, liquidate at effective-time executable depth;
binding residual uses legal joint worst state, with realized settlement only a
secondary diagnostic. No mid-price disappearance.

### 10.8 Finite grid

Only:

```text
offset in {touch, behind1}
inventory_timeout in {10s, 30s, 120s}
```

Total K=6, all preregistered. Nested TRAIN selects at most one using risk first,
then strict paired event-PnL LCB; ties choose shorter timeout, then touch. Band,
tug definition, size and signal version are not tuned in this card.

### 10.9 Controls and required reports

- no-quote;
- same range without retreat kill;
- deterministic single-side by root hash;
- same band without prior-tug filter (diagnostic);
- matched prior non-retreat anchors;
- future-retreat, trigger+10s, root-label rotation and snapshot-reset sentinels.

Report round trips, orphan fills, spread, fees, cancel-pending loss, forced exit,
conditional final leader loss, ES/lower tail, capital-hours and
ATP/WTA/ITF/Challenger + delete-best heterogeneity.

---

## 11. Card DR3-T90-RB-01 — post-retreat rebuild

### 11.1 Role

This is a re-entry increment over `DR3-T90-01`, not a simultaneous strategy.
It begins only after kill completed and zero-resting was proven.
It inherits the parent's post-scheduled diagnostic lock; a ruling admitting
T90 does not implicitly admit RB unless RB is named.

### 11.2 Proposed entry

All must hold:

- a valid prior `F-RETREAT-01` kill occurred;
- one versioned burst event occurred (`rolling 1s trades >= 8`,
  discovery-derived), carrying `burst_event_id`, trigger time and proposed
  TTL=3s; it latches once and cannot be counted again;
- all retreat flags false continuously for 3s;
- first-three-level depth recovered to ≥80% of the frozen **pre-kill baseline**;
- spread remains ≥1.25× that same pre-kill baseline;
- last 500ms price movement ≤1 tick;
- price remains in 88–93, inventory=0 and zero-resting confirmed.

The numeric thresholds above are proposed finite definitions, not prior
authority. The auditor may require TRAIN calibration before freeze.

### 11.3 Quote and exit

- touch, one contract per side, same T90 cap;
- exit when spread normalizes to ≤1.10× the same pre-kill baseline;
- maximum episode 30s;
- a new burst with a different authoritative event ID, renewed retreat,
  gap or stale state immediately cancels;
- at most one rebuild cycle per root `[PROPOSED]`; no second rebuild attempt
  after any renewed kill or root stop.

This is a toxicity/re-entry module over the parent state machine, not PM's
DP-5 normal-requote relaxation and not a sixth independent policy.

### 11.4 Co-primary gate

Both must pass:

1. absolute strict event PnL LCB >0;
2. paired same-root LCB of
   `NetPnL(T90 with rebuild) - NetPnL(T90 never re-enter)` >0.

If signal lead is shorter than cancel-effective, negative controls reproduce
the effect, or false re-entry costs dominate, reject the module while leaving
the parent T90 verdict unchanged.

---

## 12. Cards DR3-CS-ASK-01 and DR3-CS-BID-CTRL-01

### 12.1 Scope and directional separation

CS is canonical Track-D proposal-only. Gate-bearing work requires a durable
canonical-tier scope ruling, then a separate exact W/phase release, registry,
split/accounting package and uncontaminated evidence. PM/T90 authority cannot
implicitly admit it. Complete terminal outcomes are mandatory.

ASK and the BID payoff counterfactual have opposite tails and are never
averaged. Only ASK is an executable policy card in this plan.

For high-side price `q` in dollars and terminal winner indicator `Y`:

```text
ASK / economic sell-high: PnL = q - Y - exact_net_fee
BID payoff counterfactual: PnL = Y - q - counterfactual_exact_net_fee
```

`CS-BID` is calculated on the exact ASK fill IDs/prices/terminal outcomes. It
does not submit an order and has no independent fill probability, queue,
latency or capacity claim. It cannot be promoted from this family.

### 12.2 Population proposal

- Discovery: continuous price 85–99, no favorite/longshot narrative.
- Executable candidate family: 97c and 98c.
- 99c ASK: queue/capacity diagnostic only because no legal public price can
  trade strictly through a 99c ask under the binding rule.
- Binary match-winner; exact settlement, cancellation, retirement and void
  semantics for 100% of simulated fills.
- Pre-scheduled and post-scheduled proxy phases are separate registry/split
  claims; post-scheduled remains locked unless named by the durable ruling.
- Book age ≤1s; at least one trade in 5m; intended quote state persists 30s.
- Fee/account precision/tick/root/orientation/payout/lifecycle all known.

### 12.3 Proposed quote regimes

- Cell identity is the actual intended high-side ask `q`, not a midpoint/trade
  bucket. `CS-TW` requires a valid two-sided high-side best ask exactly `q` and
  joins it after the persistence rule.
- `CS-OS`: when high-side book is bid-only, quote missing ask at
  `best_bid + 1 legal tick = q`, provided it is legal/noncrossing.
- Freeze whether the high side is YES or NO for the episode; use the four-case
  orientation mapping in §6.4. A leader flip cancels and ends the episode.
- From flat, implement economic sell-high as **buy the low-side outcome at
  `1-q`** (high=YES -> buy NO; high=NO -> buy YES). Selling owned high-side
  inventory is only a reducing action, never an assumed naked opening action.
- Binding size one contract, one fill maximum per root, no replenish.
- Quote age 30s.
- Cancel on price-cell exit, high-side bid retreat ≥1 tick, better ask,
  stale/gap/pause/terminal/fee/tick invalidity.
- Binding endpoint is actual settlement; short-horizon markout is explanatory.

For each realized strict ASK fill, compute BID's sign/payoff transform with the
same fill ID, `q`, root and `Y`, but recompute exact net fee for the
counterfactual signed revenue/account-rounding path. Never describe this as a
BID order, BID fill rate, self-trade test or executable capacity.

### 12.4 One-contract fee and break-even illustration

For multiplier-1 maker-fee series under clean fee-facts commit `8963cdc`, a
one-contract/one-fill order illustrates why account class is binding:

| q | Direct net fee | Direct: ASK upset > | Direct: BID upset < | Non-direct net fee | Non-direct: ASK upset > | Non-direct: BID upset < |
|---:|---:|---:|---:|---:|---:|---:|
| 97c | 0.06c | 3.06% | 2.94% | 1.00c | 4.00% | 2.00% |
| 98c | 0.04c | 2.04% | 1.96% | 1.00c | 3.00% | 1.00% |
| 99c | 0.02c | 1.02% | 0.98% | 1.00c | 2.00% | <0% (no positive region) |

This remains `NON-GATE` illustration only. Binding calculations require
`8963cdc` or a tested successor with no dirty override, a live
series/date/event lookup and verified account precision/accumulator. The
relevant upset rate is conditional on a binding ASK simulated fill, not all
markets that ever touched the price.

### 12.5 Finite family

Gate-bearing ASK cells:

```text
price in {97,98}
book regime in {CS-TW, CS-OS}
phase in {PRE_SCHEDULED, POST_SCHEDULED_PROXY}
```

All 8 are proposed; no cell is registered for confirmation until Track-D
authority exists. Phase claims remain separate. Nested TRAIN freezes at most
one cell before VALIDATION. 99c and 94–96c are diagnostic/benchmark cells.

### 12.6 Outcomes and controls

Required:

- actual settlement PnL and terminal coverage;
- conditional upset count/rate/CI per price/regime/phase;
- longest loss streak, worst event/day and lower-tail ES/CVaR;
- capital-hours/collateral;
- retirement/walkover/void/postponement accounting;
- delete-best event/day/league;
- 5m markout versus settlement direction-conflict rate;
- CS-ASK versus non-executable BID payoff/sign transform on exact ASK fills;
- 94–96 placebo; persistence30s versus instantaneous diagnostic;
- settlement-label shuffle and unrelated/root-shuffle controls.

### 12.7 Verdict rules

ASK requires absolute strict settlement-PnL LCB >0 and a preregistered
ASK-minus-BID-payoff contrast in the expected direction. CS-OS additionally
must improve over CS-TW or be removed from the final policy. The card-level
intersection-union p-value is
`max(p_abs_ASK, p_ASK_minus_BID, p_CSOS_minus_CSTW if final=CS-OS)`; every
required lower bound must clear its frozen threshold.

CS-BID legal outcomes are only:

- `CONTROL_SUPPORTS`;
- `CONTROL_REFUTES`;
- `CONTROL_INCONCLUSIVE`.

Any missing terminal fact, insufficient upset support or dependence on one
upset/day/league produces `COLLECT_MORE` or `REJECT`, never a profit claim.

---

## 13. Negative-control suite

Every main result and its controls use the same eligible roots, tape, fee
facts, latency random numbers and output schema.

### 13.1 Shared controls

- `NO_QUOTE = 0` sanity;
- matched random eligibility with equal quote opportunity;
- root/ticker label rotation inside day/league;
- unrelated-event feature;
- side/sign flip with expected sign reversal;
- future feature/lookahead sentinel;
- delayed execution;
- at-price trades never strict-fill;
- zero-fee/zero-latency only as clearly nonbinding optimistic bounds;
- injected gap stops new quotes but preserves old cancel-pending exposure;
- duplicate trade/order event idempotency.

Controls are preregistered in five disjoint classes:

1. **Deterministic invariants** must pass exactly: at-price produces zero
   strict fills; duplicates produce zero incremental fills/PnL; future-as-of
   and invalid orientation hard-fail.
2. **Stochastic null controls** use TOST equivalence against frozen
   `epsilon_control`, root/day blocks and their multiplicity family. “p > .05”
   is not evidence of harmlessness.
3. **Directional comparators** have a frozen sign/contrast; they are not
   required to equal zero (for example the CS BID payoff transform).
4. **Ablations/benchmark bands** measure mechanism specificity and may contain
   real effects (toxicity OFF, no-tug, 94–96).
5. **Stress scenarios** test robustness and are not null hypotheses (latency,
   delayed execution, higher fees, depth loss).

`epsilon_control` is `[OPERATOR-TBD]`/TRAIN-frozen before VALIDATION and must be
small enough that an effect at the boundary cannot satisfy the commercial
hurdle.

### 13.2 Family-specific controls

- PM: time-of-day/TTS shuffle, toxicity OFF, simple spread/activity baseline.
- T90: no-tug filter, retreat+10s, future retreat, snapshot-reset exclusion,
  far-from-touch depth removal.
- CS: settlement-label shuffle, 94–96 placebo, ASK/BID contrast, instantaneous
  versus persistent state.

Only failure of a deterministic invariant or true stochastic-null equivalence
gate makes the method invalid. Directional comparator, ablation and stress
outcomes use their own frozen decision rules; a failed mechanism/robustness
rule rejects or narrows the strategy claim without mislabeling it a null.

---

## 14. Split, power, multiplicity and stopping

### 14.1 Exposure and splits

- Join a complete prior-exposure ledger before assignment.
- Any root whose price, feature, markout, fill, outcome, selection or PnL was
  previously inspected is `EXPLORATORY_ONLY`.
- If an exposed artifact cannot be mapped exactly to root IDs, every root in
  its full date/release × analysis population is `PRIOR_EXPOSED` and forced to
  `EXPLORATORY_ONLY`. Unknown exposure mapping never means unseen.
- Root events and all linked markets stay together.
- Chronological assignments: EXPLORATORY → TRAIN → one VALIDATION → one
  HISTORICAL_CONFIRMATION/prospective cohort.
- Embargo covers maximum feature lookback, quote episode, markout, terminal
  carry and event-pack overlap.
- Blind DQ may inspect sealed coverage only under a frozen mechanical rule and
  without revealing price/fill/outcome/PnL summaries.

Exploration and engineering begin without waiting for 20 days once separately
authorized. Formal confirmation does not silently waive existing sample gates.

### 14.2 Current provisional sample interpretation

Until the operator releases a replacement, preserve the sources separately:

- Deep02 handoff proposed ≥20 independent qualified dates and ≥200 relevant
  roots per core hypothesis for Deep03 promotion. Treat this as a conservative
  provisional requirement that a durable Deep03 ruling must explicitly ratify
  or replace; it does not inherit Deep02 run/budget authority.
- Program-wide D-1 requires seven **predesignated qualified clean complete**
  test dates and at least five of those exact seven positive days.
- If power requires more than seven days, the operator/ruling must freeze the
  replacement or nested seven-day rule before unblinding; never choose the best
  seven afterward.
- TRAIN power may require more blocks, never fewer without an explicit ruling;
- primary strategy support is ≥200 roots; subgroup cells below 200 are
  descriptive unless separate power is preregistered.

This section is an explicit audit target. Exploratory/engineering work before
20 qualified dates is permitted only by a separately authorized release; no
chat-only instruction is citable authority until archived verbatim in
`PLAN_SPORTS_TRADING_DECISIONS.md`.

### 14.3 Power proposal

Before the first VALIDATION is sealed:

- one `DEEP03_CASH_STRATEGY_V1` super-family alpha `[PROPOSED] 0.05`; all child
  families/openings share it and receive only a frozen allocation from the
  sealed closed-testing graph/`PROGRAM_TEST_LEDGER`;
- target power `[PROPOSED] 0.80`;
- freeze `Delta_min_abs[policy]`, `Delta_min_increment[variant]` and
  `epsilon_control[control]` in fixed-point economic units;
- variance/design effect/zero-trigger/zero-quote/zero-fill/DQ attrition from
  complete TRAIN blocks;
- power simulation applies the actual intersection-union rule and post-Holm
  alpha if K>1;
- for CS-ASK, power includes sparse favorable-upset support/concentration; for
  the BID payoff control, it includes adverse-upset downside; void/retirement
  paths and worst-plausible tails are separate; raw root count is not power;
- required independent blocks;
- freeze timezone/calendar, multi-day-event assignment, bootstrap family,
  block-length rule, replications, seed, studentization and empty-block rule;
- fixed end date **or** group-sequential alpha spending, never indefinite
  “collect until significant.”

### 14.4 Inference

- For policy `j` and eligible root `e`, freeze `Pi[j,e]` as complete pathwise
  root-event PnL, including zero-quote/zero-fill roots, and
  `mu[j] = mean_e(Pi[j,e])` as the absolute primary estimand.
- PM-OS also freezes `Delta[OS,e] = Pi[OS,e] - Pi[PM-parent,e]`; T90-RB freezes
  `Delta[RB,e] = Pi[T90+RB,e] - Pi[T90-no-RB,e]`; CS freezes a paired ASK-minus-
  BID payoff contrast on exact ASK fills (no BID execution claim).
- Aggregate PnL within root event first.
- Use complete chronological calendar-day blocks.
- Exploratory bootstrap ≥1,000; final candidate bootstrap 5,000 deterministic
  reps `[PROPOSED]`.
- Primary gate is two-sided 95% CI lower endpoint `ci95_lower(mu_event)>0`.
- Report p01/p05/median/p95/p99/max, ES/CVaR, drawdown and full distributions.
- Delete-best event/day/sport/league/series and concentration are mandatory.

For each incremental card, the co-primary intersection-union p-value is
`max(p_absolute, p_increment)`; both confidence lower endpoints must clear
their frozen threshold. A strong parent cannot rescue a negative increment,
and a positive increment over a losing parent cannot create a candidate.

For CS, the card-level composite is
`max(p_abs_ASK, p_ASK_minus_BID_payoff, p_CSOS_minus_CSTW if final=CS-OS)`.
Every required lower bound must pass. If multiple policy cards enter a
separately authorized confirmation, Holm applies to card-level composite
p-values, not their internal component p-values as separate “winners.”

### 14.5 Multiple testing and selection

Before the first VALIDATION, seal `DEEP03_SUPERFAMILY.json` and
`PROGRAM_TEST_LEDGER.jsonl` with the fixed super-family ID/assignment-rule SHA,
single alpha, every child `program_family_id`, maximum validation/confirmation
openings, opening sequence, per-opening allocation/closed-testing graph, every
lineage/policy fingerprint and cumulative spend. Child allocations must be
nonnegative and their closed graph/family-wise bound must prove total error
≤0.05. Any cash-capable new card, sport, track, cohort, lineage or generation
inherits this same super-family. An unassigned object cannot enter TRAIN or a
sealed cohort. A `NO_CASH_CLAIM` object cannot independently enter, open or
claim TRAIN/VALIDATION/CONFIRMATION and can never become promotable; only a
preregistered `BOUND_CONTROL` may execute inside its named cash parent's exact
opening, using the parent's split/allocation with `alpha_claimant=false` and a
control-only verdict.

Alpha allocation is concurrency-safe, not a read-then-append convention. A
single-writer lock plus compare-and-swap on `prior_ledger_sha` enforces:

~~~text
PREALLOCATED(allocation_id, opening_id, alpha)
  -> CLAIMED(opening_id, claimant_run_id, prior_ledger_sha)
  -> SPENT(result_receipt_sha)
     or CONSUMED_NO_RESULT(failure_receipt_sha)
~~~

CLAIMED occurs atomically **before** any sealed cohort read. Only one claimant
can win; duplicate opening/allocation IDs fail. A crash or abandonment after
CLAIMED consumes the slice unless a separately audited pre-read receipt proves
the cohort was never accessed; the default is no refund. Ledger/fsync/lock
failure is fail-closed. A failed, abandoned, independent-cohort or newly named
lineage remains visible. New dates/reports/agents/research generations do not
create fresh 0.05 tests. Exhaustion makes remaining Deep03 work exploratory.

The global selection ledger and confirmatory K are related but distinct. Every
TRAIN feature/model/hyperparameter/seed and human choice is recorded; nested
TRAIN plus one-shot VALIDATION controls that selection path. Confirmatory K is
the number of policy-level hypotheses allowed to claim in an opening. It is not
the raw hyperparameter count, and K=1 for one opening does not reset the
super-family alpha budget.

Proposed registry families, activated only by their own scope authority:

- current canonical: `PM-TS` only;
- one-sided scope extension: `PM-OS` with PM parent;
- post-scheduled extension: `T90 = {range, rebuild}`;
- separate Track-D registry/split/accounting: `CS-ASK` plus BID payoff control.

PM-OS and T90-RB use intersection-union gates: absolute PnL and paired
increment must both pass. CS-BID is not counted as a promotable winner.

Preferred per-opening design: nested TRAIN selects and freezes at most one
simplest policy **before** one VALIDATION. VALIDATION accepts/rejects it and
performs no selection; unchanged confirmation then has within-opening K=1 but
spends the preassigned super-family alpha. If one opening contains multiple
policies, freeze K and apply Holm across them. Splitting cards or openings into
different reports does not reset within-opening K or cumulative super-family
error.

---

## 15. Tail, concentration, capacity and commercial gate

Every strategy report includes:

- opportunity roots/day and active minutes;
- quoted roots, zero-quote roots and reason distribution;
- strict/queue/optimistic fill rate;
- gross capture, fees, adverse move, cancel-pending, forced-exit and terminal
  waterfall reconciling to NetPnL;
- event/day PnL ECDF and lower tail;
- minimum, p01, p05, median, p95, p99, maximum, CVaR/ES95, CVaR/ES99,
  maximum drawdown and longest loss streak;
- maximum/time-weighted inventory and capital-hours;
- event/day/sport/league/series/factor concentration, including PnL HHI,
  top-1/top-5 share of positive PnL and top-1/top-5 share of losses;
- write-token/action rate and headroom;
- 1-contract capacity and size diagnostics without linear extrapolation;
- latency ×1.5 and ×2, cancel p99×2, fee-up-one-class and combined stress;
- delete-best results and worst root narrative.

For CS-ASK, `Y=1` is the frequent small-loss state and `Y=0` upset is the rare
large-gain state. Report favorable-upset count, top-upset profit share,
delete-one-upset and zero/one-fewer-upset sensitivity. For the BID payoff
counterfactual, `Y=1` is frequent small gain and `Y=0` upset is rare large loss;
report upset loss contribution, downside ES/CVaR and one-additional-upset
stress. Void, retirement/walkover, cancellation and postponement are separate
signed categories. ASK is not powered with insufficient rare favorable-payoff
support; BID downside is not identified when no adverse upset is observed.

Five-factor business receipt:

```text
daily net PnL
= eligible opportunities/day
* strict fill probability
* fee-after edge/fill
- inventory/exit cost
- failure/tail loss
```

Commercial fields that must be operator-ratified before confirmation:

- `[OPERATOR-TBD] minimum_daily_or_monthly_pnl`;
- `[OPERATOR-TBD] required_return_on_capital`;
- `[OPERATOR-TBD] maximum_loss_per_root_event`;
- `[OPERATOR-TBD] maximum_day_loss_for_research_candidate`;
- `[OPERATOR-TBD] maximum_capital_hours / collateral`;
- `[OPERATOR-TBD] minimum_write_headroom_pct`;
- server/data/monitoring/maintenance cost assumptions.

Passing statistics but failing this receipt yields
`RESEARCH_VALID_NOT_DEPLOYABLE`.

---

## 16. Process status and legal verdicts

### 16.1 Process status is not a research verdict

Every declared method has exactly one `run_execution_status`, independent of its
research stage, TRAIN selection, economic verdict and deployment specification:

- `NOT_STARTED` — declared/sealed, not yet executable or run;
- `BLOCKED_AUTHORITY` — scope/release is absent;
- `BLOCKED_DEPENDENCY` — required data/engine/terminal contract is absent;
- `READY_TO_EXECUTE` — all preflights pass, but no result yet;
- `EXECUTING` — incomplete run; never visible as a result;
- `COMPLETE` — atomic execution receipt exists; this says nothing about edge;
- `EXECUTION_FAILED` — software/infrastructure failure;
- `DATA_INVALID` — DQ/clock/manifest contract failed.

`research_stage` uses only the §4.13 graph (including `TRAIN_COMPLETE`,
`VALIDATION_COMPLETE` and `CONFIRMATION_COMPLETE`). `selection_disposition` is
separately one of `NOT_APPLICABLE`, `PENDING`, `TRAIN_SELECTED` or
`TRAIN_NOT_SELECTED`. `research_verdict` is null before a verdict-bearing
VALIDATION/CONFIRMATION/control completion and otherwise uses §16.2 only. No
value from one field is legal in another.

Breadth-cell status uses the §4.6 set and is not an economic verdict.
`deployment_spec_status` is separately one of `NOT_STARTED`,
`BLOCKED_AUTHORITY`, `BLOCKED_DEPENDENCY`,
`IMPLEMENTATION_CONFORMANCE_PENDING`, or `TRANSLATION_COMPLETE_SPEC`.
Deep03 has no legal `SHADOW_READY`, `MICROLIVE_READY` or `LIVE_READY` status.

The five fields do not form a free Cartesian product. `object_kind` is exactly
DISCOVERY_RUN, CASH_CARD or BOUND_CONTROL. Define the following conditional
values:

~~~text
PRETRAIN_SELECTION(CASH_CARD)=PENDING
PRETRAIN_SELECTION(BOUND_CONTROL)=NOT_APPLICABLE
SEALED_SELECTION(CASH_CARD)=TRAIN_SELECTED
SEALED_SELECTION(BOUND_CONTROL)=NOT_APPLICABLE

VALIDATION_VERDICT(CASH_CARD) in
  {METHODOLOGY_INVALID, NOT_ESTIMABLE, REJECT, COLLECT_MORE,
   RESEARCH_VALID_NOT_DEPLOYABLE, EDGE_CANDIDATE_FOR_CONFIRMATION}
VALIDATION_VERDICT(BOUND_CONTROL) in
  {CONTROL_SUPPORTS, CONTROL_REFUTES, CONTROL_INCONCLUSIVE}

CONFIRMATION_VERDICT(CASH_CARD) in
  {METHODOLOGY_INVALID, NOT_ESTIMABLE, REJECT, COLLECT_MORE,
   RESEARCH_VALID_NOT_DEPLOYABLE, BACKTEST_CANDIDATE}
CONFIRMATION_VERDICT(BOUND_CONTROL) in
  {CONTROL_SUPPORTS, CONTROL_REFUTES, CONTROL_INCONCLUSIVE}
~~~

Every run/card state must match exactly one row of `RUN_STATE_ONEOF`; all
unlisted cross-products are exact-red:

| State class | object_kind / research_stage | run_execution_status | selection_disposition | research_verdict | deployment_spec_status |
|---|---|---|---|---|---|
| DISCOVERY_OPEN | DISCOVERY_RUN / OPEN_DISCOVERY | NOT_STARTED, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, READY_TO_EXECUTE, EXECUTING, COMPLETE, EXECUTION_FAILED or DATA_INVALID | NOT_APPLICABLE | null | NOT_STARTED |
| DISCOVERY_SEALED | DISCOVERY_RUN / DISCOVERY_COMPLETE | COMPLETE | NOT_APPLICABLE | null | NOT_STARTED |
| PRETRAIN | CASH_CARD or BOUND_CONTROL / CARD_PROPOSED or TRAIN_FROZEN | NOT_STARTED, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, READY_TO_EXECUTE or DATA_INVALID | PRETRAIN_SELECTION(object_kind) | null | NOT_STARTED |
| TRAIN_RUNNING_OR_STOPPED | CASH_CARD or BOUND_CONTROL / TRAIN_EXECUTING | EXECUTING, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, EXECUTION_FAILED or DATA_INVALID | PRETRAIN_SELECTION(object_kind) | null | NOT_STARTED |
| TRAIN_DONE | CASH_CARD or BOUND_CONTROL / TRAIN_COMPLETE | COMPLETE | CASH_CARD: TRAIN_SELECTED or TRAIN_NOT_SELECTED; BOUND_CONTROL: NOT_APPLICABLE | null | NOT_STARTED |
| VALIDATION_WAIT | CASH_CARD or BOUND_CONTROL / VALIDATION_SEALED | NOT_STARTED, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, READY_TO_EXECUTE or DATA_INVALID | SEALED_SELECTION(object_kind) | null | NOT_STARTED |
| VALIDATION_RUNNING_OR_STOPPED | CASH_CARD or BOUND_CONTROL / VALIDATION_EXECUTING | EXECUTING, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, EXECUTION_FAILED or DATA_INVALID | SEALED_SELECTION(object_kind) | null | NOT_STARTED |
| VALIDATION_DONE | CASH_CARD or BOUND_CONTROL / VALIDATION_COMPLETE | COMPLETE | SEALED_SELECTION(object_kind) | VALIDATION_VERDICT(object_kind) | NOT_STARTED |
| CONFIRMATION_WAIT | CASH_CARD or BOUND_CONTROL / CONFIRMATION_SEALED | NOT_STARTED, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, READY_TO_EXECUTE or DATA_INVALID | SEALED_SELECTION(object_kind) | null | NOT_STARTED |
| CONFIRMATION_RUNNING_OR_STOPPED | CASH_CARD or BOUND_CONTROL / CONFIRMATION_EXECUTING | EXECUTING, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, EXECUTION_FAILED or DATA_INVALID | SEALED_SELECTION(object_kind) | null | NOT_STARTED |
| CONFIRMATION_DONE | CASH_CARD or BOUND_CONTROL / CONFIRMATION_COMPLETE | COMPLETE | SEALED_SELECTION(object_kind) | CONFIRMATION_VERDICT(object_kind) | NOT_STARTED |
| CONFORMANCE_ACTIVE | CASH_CARD / IMPLEMENTATION_CONFORMANCE | NOT_STARTED, BLOCKED_AUTHORITY, BLOCKED_DEPENDENCY, READY_TO_EXECUTE, EXECUTING, COMPLETE, EXECUTION_FAILED or DATA_INVALID | TRAIN_SELECTED | BACKTEST_CANDIDATE | BLOCKED_AUTHORITY iff run is BLOCKED_AUTHORITY; BLOCKED_DEPENDENCY iff run is BLOCKED_DEPENDENCY; otherwise IMPLEMENTATION_CONFORMANCE_PENDING |
| PROCESS_CLOSED | CASH_CARD / PROCESS_COMPLETE | COMPLETE | TRAIN_SELECTED | BACKTEST_CANDIDATE | TRANSLATION_COMPLETE_SPEC |

A BOUND_CONTROL row is legal only with a registered `control_registry_id`, the
immutable parent/opening/split/allocation binding and `alpha_claimant=false` in
§4.5. A CASH_CARD with
TRAIN_NOT_SELECTED cannot appear after TRAIN_COMPLETE. A control verdict on a
cash card, a cash verdict on a control, non-null pre-result verdict, or any
deployment status outside the row is exact-red. A property test enumerates the
full enum Cartesian product and proves each legal tuple matches once and every
other tuple zero times, including control_registry_id/object-kind legality. A
parent cannot emit RUN_COMPLETE or advance until every preregistered
BOUND_CONTROL for that opening is COMPLETE at the matching stage: TRAIN
requires its null-verdict execution/result receipt, while VALIDATION and
CONFIRMATION require its CONTROL_* verdict to be incorporated. A failed/missing
control blocks the parent rather than disappearing.

If authority expires/is revoked or a dependency disappears after an
`*_EXECUTING` stage begins, the same stage records BLOCKED_AUTHORITY or
BLOCKED_DEPENDENCY and is terminal for that run; it cannot advance. A retry
needs a new run receipt and may reuse an allocation only under separately
audited proof that no cohort/data row was opened. Otherwise exposure and alpha-
consumption rules apply.

A run with `research_stage=VALIDATION_COMPLETE` and
`run_execution_status=COMPLETE` may carry the nonterminal verdict
`EDGE_CANDIDATE_FOR_CONFIRMATION`; that is neither a process status nor a
`BACKTEST_CANDIDATE` and does not permit opening confirmation.

The report carries separate `declared_methods[]` and `executed_methods[]`.
Registration, a function name, or a `latest.json` pointer cannot make an
unexecuted test appear `pass`, `not_started`-but-green, or economically valid.

### 16.2 Legal research verdict set

Every verdict-bearing VALIDATION/CONFIRMATION cash card or BOUND_CONTROL must
end with exactly one value allowed by `RUN_STATE_ONEOF`; before that point the
field is null:

- `METHODOLOGY_INVALID`;
- `NOT_ESTIMABLE`;
- `REJECT`;
- `COLLECT_MORE`;
- `RESEARCH_VALID_NOT_DEPLOYABLE`;
- `EDGE_CANDIDATE_FOR_CONFIRMATION` (VALIDATION only);
- `BACKTEST_CANDIDATE`;
- for any `CONTROL_REGISTRY.jsonl`-registered BOUND_CONTROL (currently only
  CS-BID): `CONTROL_SUPPORTS`, `CONTROL_REFUTES`, or `CONTROL_INCONCLUSIVE`.

`BACKTEST_CANDIDATE` requires `research_stage=CONFIRMATION_COMPLETE`,
`run_execution_status=COMPLETE` and every adjusted statistical/control/tail/
commercial gate. It means only that an
operator may consider the next engineering/replay gate; it does not authorize
shadow or live.

Automatic rejection/invalidity includes:

- lookahead, clock ambiguity not treated conservatively or non-determinism;
- fee/account precision/payout/root/orientation/action/lifecycle rule unknown in
  economic rows;
- nonconserving PriceE4/CountE4 position or MoneyE6-or-wider
  cash/collateral/trade-fee/rounding/rebate ledger;
- only queue/optimistic profitability;
- strict powered CI lower endpoint ≤0;
- combined latency/fee/gap/exit stress materially negative;
- existing >25% single-root profit concentration or delete-best sign reversal;
- risk/capital cap breach;
- terminal coverage missing for hold-to-settlement cards;
- commercial capacity below the frozen hurdle ->
  `RESEARCH_VALID_NOT_DEPLOYABLE`, unless the underlying technical estimand or
  capacity calculation fails, in which case use `REJECT` or
  `METHODOLOGY_INVALID` as applicable.

Insufficient powered support is `COLLECT_MORE`, not a favorable verdict and not
permission to extend a fixed holdout until significant. It is terminal for the
current opening under §4.13; it never reuses a completed run or alpha claim.

---

## 17. Required artifacts and schemas

Proposed run root after authorization:

```text
work/research/deep03/<RUN_ID>/
```

Required immutable artifacts:

1. `AUTHORITY.json` — durable ruling chain plus exact W/phase release. Required
   fields: `release_id`, `issued_at_utc`, `operator_text_verbatim`, operator text
   SHA, active prompt path/SHA, named superseded IDs, base commit, authorized
   branch/worktree, authorized phase/W IDs, exact write roots, tool/network/API/
   credential classes, `spending_cap` (default 0), prerequisite/audit hashes,
   expiry/session count and ACTIVE/CONSUMED/REVOKED/SUPERSEDED status. It also carries the
   canonical booleans `external_account_actions`, `paid_api_access`,
   `credential_use`, `live_order_permission`, `production_mutation` and
   `push_permission`; all default false, each must be explicitly present, and a
   missing/non-boolean/true-without-exact-release value fails closed.
   W-HYPO-FEED-01 additionally binds boolean `telegram_send_permission` and
   `telegram_update_read_permission` (both default false), exact API host/method
   allowlist, bot identity, chat ID/operator-user-ID hashes, sole-gateway
   service ID, credential-free Mac/W09 identities, cross-host spool roots,
   bounded review-window duration and any exact event-class NO_WAIT waiver.
   Missing/non-boolean values are
   blocked, never inferred from an existing monitoring deployment.
2. `PRE_REGISTRATION.md` — every card, primary/co-primary metric and verdicts.
3. `REGISTRATION_RECEIPT.json` — registration SHA, UTC seal time, code/config/
   split identity and the frozen `declared_methods[]`.
4. `INPUT_MANIFEST.json` — release/object VersionId/hash/size/rows/quality.
5. `DATA_QUALITY_RECEIPT.json` — canaries, gaps, exclusions and blocking state.
6. `PRIOR_EXPOSURE_LEDGER.jsonl` and one separately salted
   `SPLIT_MANIFEST_<stage>_<opening>.json` per commitment domain; no manifest or
   salt spans VALIDATION and CONFIRMATION.
7. `DEEP03_SUPERFAMILY.json`, `CANDIDATE_REGISTRY.jsonl`,
   `GLOBAL_SELECTION_LEDGER.jsonl`, `PROGRAM_TEST_LEDGER.jsonl` and
   `ALPHA_ALLOCATION_LEDGER.jsonl` — immutable assignment rule, every TRAIN
   attempt/choice/opening and CAS claim/spend under the one super-family,
   including parent-bound `alpha_claimant=false` controls, plus sealed
   `CONTROL_REGISTRY.jsonl` identities.
8. `ENGINE_MANIFEST.json` — code commit, dirty status/diff SHA, config hashes,
   command, host, simulator authority, default-resolution schema SHA and every
   fully resolved runtime default used by the design fingerprint; frozen and
   reconciled before RUN_AUTHORIZED or reader/split access.
9. `ESTIMABILITY_PREFLIGHT.json` and `EXCLUSION_WATERFALL.json`.
10. `METHOD_EXECUTION_RECEIPT.json` — `declared_methods[]`,
    `executed_methods[]`, experiment/run/source-row/design-fingerprint join,
    status, eligible roots, reason and artifact for each.
11. `FEATURE_LEDGER.jsonl` with role/source/as-of/TTL/version/verdict.
12. `EPISODE_LEDGER.parquet`.
13. `INTENT_LEDGER.parquet`.
14. `ORDER_LIFECYCLE_LEDGER.parquet`.
15. `FILL_LEDGER.parquet`.
16. `INVENTORY_LOT_LEDGER.parquet`.
17. `EVENT_PNL_LEDGER.parquet` and exact reconciliation receipt.
18. `NEGATIVE_CONTROL_RESULTS.json`.
19. `RESULTS.json` — each scalar includes unit, roots/days/markets/fills,
    uncertainty, exclusions and provenance.
20. `INDEPENDENT_RECALC.json` binding independent implementation SHA,
    input-manifest SHA and recomputed outputs.
21. `DECISION.md` using only the legal verdict set.
22. self-contained `index.html`, `ARTIFACT_SHA256SUMS` and one-command
    reproduction receipt.
23. `TAXONOMY.json`, `DIRECTION_TAXONOMY.jsonl`, `PARENT_ATOMIC_MAP.json`,
    `MODEL_METHOD_REGISTRY.jsonl`, `MODEL_METHOD_COVERAGE.json`,
    `SPORT_TAXONOMY.json`, structure/time registries and both independently
    emitted pair and target expected key sets, including all 1,128 G-owner
    pairs, every `MODEL_METHOD_TARGET_KEY` and exact F08/control sport expansion.
24. `INDEPENDENT_TAXONOMY_RECALC.json` binding independent code SHA, raw
    manifest, parent/atomic/method closure, exact-one/explicit-expansion
    assignments and expected-set hash.
25. `ELIGIBLE_CELL_KEYS.parquet`, `D3_BREADTH_COVERAGE_LEDGER.parquet` and
    exact expected-A/expected-B/emitted set-equality receipt.
26. `UNCLASSIFIED_BACKLOG.jsonl` and direction-level reopen triggers.
27. `SNBD_STATE_LEDGER.parquet` and state/transition/hazard atlas.
28. immutable `STRATEGY_CARD.yaml` files and policy fingerprints for every
    card reaching TRAIN freeze, plus `DESIGN_REVISION_REGISTRY.jsonl`,
    `ACTION_REGISTRY.jsonl`, `HORIZON_REGISTRY.jsonl`,
    `MODEL_SPEC_REGISTRY.jsonl` and each `RESOLVED_RUN_DESIGN_MANIFEST.json`.
29. `RUN_STATE_ONEOF_SCHEMA.json` and `STAGE_TRANSITION_RECEIPTS.jsonl` binding
    object_kind, research_stage,
    run_execution_status, selection_disposition, research_verdict and
    deployment_spec_status to exactly one legal §16.1 tuple.
30. `SHADOW_TESTABLE_SPEC.yaml` when a confirmed survivor reaches the required
    implementation-conformance boundary; status remains spec-only.
31. `MICROLIVE_TESTABLE_SPEC.yaml` and `LIVE_TESTABLE_SPEC.yaml` only as
    non-authorizing future-test specifications under §17.1–§17.2.
32. `HYPOTHESIS_EXPERIMENT_LEDGER.jsonl` plus
    `HYPOTHESIS_LIFECYCLE_RECEIPTS.jsonl` — every idea/design revision and
    directional CLOSE/REOPEN transition with prior SHA, from/to state, reason
    and reopen trigger; mutable hypothesis views are derived only.
33. `RESEARCH_FEED_EVENTS.jsonl` — hash-chained idempotent projection of the
    exact hypothesis/design/stage/lifecycle/judgment/application source union
    under the event-obligation table.
34. `RESEARCH_FEED_OUTBOX.jsonl` and immutable rendered message bodies/parts.
35. `TELEGRAM_DELIVERY_RECEIPTS.jsonl` — event/content SHA, sealed batch/logical-
    event IDs where applicable, transport kind, Telegram chat/message IDs,
    attempts and accepted timestamp; no credential material.
36. `OPERATOR_JUDGMENT_LEDGER.jsonl` — allowlisted reply binding, append-only
    judgment, acknowledgment and limited-effect receipt.
37. `OPERATOR_JUDGMENT_APPLICATIONS.jsonl` — safe-point application or
    NOTE_ONLY/stale receipt; never an in-place hypothesis/card mutation.
38. `FEED_DESIGN_RECONCILIATION.json` plus
    `RESEARCH_FEED_COMPLETENESS.json` — attempted-design/feed-visible equality;
    exact source/payload/render/schema/multipart/sealed-batch tuple equality;
    missing/orphan/conflict diagnostics and latest judgment/application state.
    The former binds independent reconciler code SHA, all input-ledger manifest
    SHA and expected/feed fingerprint-set SHAs at pre-authorization and pre-
    completion; the latter binds independent event-enumerator code SHA, input-
    source-manifest SHA and expected/feed-event-set SHAs.
39. `RUN_COMPLETE.json` — written atomically **last inside the immutable
    research-result bundle**, binds every research SHA available at run close,
    start/end time, exit status and `executed_methods[]`. The subsequent
    sealed-stage SEALED_COMPLETION_TRANSPORT receipt binds this RUN_COMPLETE SHA
    and RESULT_AND_NEXT_DECISION into one Telegram message ID; that receipt is
    mandatory before a next-stage transition.

Any mutable `latest.json` is only a pointer. It may target a run iff
`RUN_COMPLETE.json` verifies and must repeat `run_id`, code commit,
dirty-diff SHA, config SHA, input-manifest SHA and results SHA. Dashboards and
lifecycle checks must reject a stale pointer, missing method receipt or partial
run; they must never infer execution merely from registration.

`INDEPENDENT_TAXONOMY_RECALC.json` first reproduces target/control sport,
structure/time assignments, M01–M27 parent/atomic exact coverage, all 1,128
G-method-owner pair keys and every unique target key/closure, F08 identity,
multi-relation expansion and the entire expected atomic cell set from the raw
manifest; any key/hash/assignment discrepancy is exact-red.
`INDEPENDENT_RECALC.json` then independently reproduces base-root count,
primary/co-primary point estimates, at least one tail/concentration statistic
and exact PriceE4/CountE4 positions plus MoneyE6-or-wider cash/collateral/
trade-fee/rounding/rebate totals. Any population or fixed-point mismatch, or
>5% discrepancy in a non-exact core statistic, blocks `DECISION` and
`RUN_COMPLETE` until reconciled; discrepancies are never averaged.

Every ledger row carries, as applicable:

- run/trial/policy/feature/config IDs;
- release/object VersionId;
- date/root/market/series;
- receive/source/decision/effective timestamps;
- fee/tick/action/lifecycle versions;
- reason codes;
- simulator/fill track;
- event, order, fill and inventory identifiers;
- typed `PriceE4`, `CountE4` and `MoneyE6`-or-wider exact values; no narrowing.

Visualize-Everything remains active: no scalar appears alone. Continuous
values require histogram/ECDF with p50/p99/max/n; counts/shares require their
natural per-event/day distribution or bootstrap CI; every figure includes a
definition, formula, provenance, tier and caption naming multimodality/tails.

### 17.1 Translation-complete survivor artifact

LIVE_TESTABLE_SPEC.yaml is an unfortunately easy name to misread, so its only
legal completed state in deep03 is TRANSLATION_COMPLETE_SPEC. Its permanent
top banner is:

~~~text
SPEC_ONLY_NOT_AUTHORIZED
NOT AN ORDER AUTHORIZATION
REQUIRES A NEW EXACT-SHA SHADOW OR MICROLIVE RELEASE
~~~

Its immutable source confirmation receipt must bind
`research_stage=CONFIRMATION_COMPLETE`, `run_execution_status=COMPLETE` and
`research_verdict=BACKTEST_CANDIDATE`. It may be emitted only after
implementation conformance and all synthetic/replay qualification tests, when
the **current** state matches §16.1 PROCESS_CLOSED exactly:
`PROCESS_COMPLETE` + `COMPLETE` + `TRAIN_SELECTED` + `BACKTEST_CANDIDATE` +
`TRANSLATION_COMPLETE_SPEC`. If a dependency remains open, the current state
stays CONFORMANCE_ACTIVE with deployment_spec_status BLOCKED_DEPENDENCY or
BLOCKED_AUTHORITY, never “mostly ready.”

Required sections:

1. source confirmation run, result, independent-recalc and dossier hashes;
2. immutable policy, feature, state, action and horizon fingerprints;
3. exact entry, quote, price, cancel, replacement, inventory, exit, timeout,
   re-entry and kill rules with frozen values;
4. exact state-action table, four YES/NO action mappings and golden traces;
5. engine, fee, tick, latency, fill, terminal and fixed-point versions;
6. one-contract size, root/event/factor/day/collateral/write caps, commercial
   hurdles and forced closeout behavior; no confirmed-policy risk/commercial
   value may remain OPERATOR-TBD;
7. current repository-blocker closure receipts and regression-test hashes;
8. account/order ownership isolation and external-fill same-market pause;
9. shadow specification, micro-live specification and their distinct purposes;
10. monitoring SLOs, alerts, private reconciliation, automatic/manual kill,
    rollback and zero-resting/flat proof;
11. pessimistic fee-after economics, tails, concentration, capital-hours and
    capacity at the exact tested size;
12. unresolved blockers and the exact authority/gate that can close each; any
    policy-risk blocker keeps the artifact `BLOCKED_*`, not complete.

Future session/live budgets use a separate `REQUIRES_FUTURE_OPERATOR_VALUE`
schema. They may be stricter than, but never loosen or replace, the confirmed
policy risk envelope. A looser risk limit, new universe or action is a new
policy lineage, not a session-budget choice.

The artifact closes translation work only. It cannot mark SHADOW_READY,
MICROLIVE_READY, LIVE_READY or authorize engineering, account or order changes.

### 17.2 Future shadow and micro-live test specifications

SHADOW_TESTABLE_SPEC.yaml remains SPEC_ONLY_NOT_AUTHORIZED and must bind:

| Block | Required contents |
|---|---|
| source | confirmation run, decision-policy/economic-replay fingerprints, engine/config/code hashes |
| scope | markets/phases/calendar/duration and minimum clean support |
| sink | RECORD_ONLY or NULL sink; network order mutations forbidden |
| parity | replay trace/state/intent hashes, bit-exact except named timestamps |
| inputs | public feed plus private read-only state, clock/gap/lifecycle rules |
| faults | disconnect, gap, stale, pause, clock jump, simulated 429/5xx, crash/restart and fee/tick/lifecycle drift |
| metrics | feature/state/intent parity, decision latency, would-write rate, cancel/exit intent latency, DQ uptime |
| system non-mutation | authenticated mutation count=0 and system-owned orders/fills/positions remain empty; pre/post global snapshots are context, not an unchanged-account assertion |
| manual coexistence | manual/external deltas are labeled by non-owned IDs and excluded; any external open order/fill/position in a scoped market pauses that market |
| stops | any system mutation, unowned scoped-market exposure, parity mismatch, unknown state, cap or DQ failure |
| receipts | preflight, intent ledger, parity/fault/system-non-mutation/external-delta reports and atomic completion |

Shadow fills/PnL are diagnostic and cannot replace confirmation or establish
queue truth. A future shadow release is RISK-1 and must prove no order path at
code level.

MICROLIVE_TESTABLE_SPEC.yaml also remains SPEC_ONLY_NOT_AUTHORIZED and must
bind, without inventing numeric operator limits:

| Block | Required contents |
|---|---|
| authority | future exact W/release/operator text, UTC start/end and named human owner |
| identity | unchanged decision-policy fingerprint, frozen stage mapping, new private execution-environment fingerprint, shadow receipt and executor hashes |
| account | mandatory dedicated subaccount plus subaccount-bound API key; starting-flat/zero-resting proof; unavailable capability => BLOCKED_DEPENDENCY, never namespace fallback |
| coexistence | same customer account may retain manual trading in other subaccounts; any unowned order/position/balance/collateral mutation in the system subaccount immediately blocks new risk and reconciles |
| scope | exact subset of confirmed tickers/series/phases/windows plus session and attempt maxima |
| actions | exact subset of confirmed action fingerprint; one contract/post-only entry/cancel-on-pause/verified reduce-only exit unless the confirmed card is stricter; aggressive opening is forbidden unless already confirmed |
| budgets | future operator session values, each no looser than confirmed gross/day/root loss, collateral, fill, create/cancel/write/spend and emergency-token envelope |
| controls | private WS and REST reconciliation, deadman, watchdog, manual/automatic kill, stale/gap/pause/unknown behavior |
| goal | queue/fill/cancel/latency and safety calibration only; no profit promotion |
| abort | unexpected fill/side, unknown order, position drift, cancel timeout, rate limit, lifecycle drift or namespace-external mutation |
| closeout | cancel all owned, prove terminal, reconcile, bounded reduce-only exit and flat/zero-resting proof |
| receipts | authority, pre/post account snapshots, order/fill/queue/latency, incidents, kill test, closeout and atomic completion |
| scaling | forbidden; any new action/size/market/phase first requires a new card, lineage, replay, VALIDATION, CONFIRMATION and translation spec, and only then new authority |

Research uses an exact-VersionId read-only reader, no trading credentials and a
separate simulated cash/position namespace. Shadow proves **the system** made no
authenticated mutation; it does not assume the operator's shared customer
account stayed static. Manual changes remain external to strategy PnL, but
global balance/collateral and any scoped-market exposure still reduce capacity
or pause the system fail-closed. Future own-order calibration data automatically
becomes exposed and may serve only its preregistered execution-calibration
purpose or a new lineage; it cannot flow back into opened validation or
confirmation.

---

## 18. Implementation and run sequence (future, separately authorized)

| Work package | Deliverable | Dependency | Gate-bearing? |
|---|---|---|---|
| D3-W0 | durable scope ruling where needed plus exact W/phase authority schema | audit PASS + operator ruling/release | Yes |
| D3-W1 | strict reader canary, exact VersionId manifest, DQ and split seal | Research reader | G0/G1 |
| D3-W2A | corrected deep02 screens: markout as-of, one-sided censor, quote-age cross-market, active-minute rhythm | W1 | Discovery only |
| D3-W2B | frozen parent/atomic/G-owner-pair/G-target/F08-control/sport/structure/time registries, closed breadth-status schema, taxonomy compiler A, independently implemented raw-manifest enumerator B, coverage left-join and prior-exposure receipts | W1 | D-5 breadth only; 106 IDs + 1,128 G pairs + exact target-key/control/F08 closure + pair/target A==B==emitted required |
| D3-W2C / W-HYPO-FEED-01 | append-only hypothesis/experiment/directional-lifecycle producer, credential-free cross-host spool, one locked production-monitor Telegram gateway, bounded operator window, single-object sealed results, durable outbox/content receipts, sole-consumer judgment intake and independent design/event reconciliation | revised exact-SHA W-HYPO-FEED-01 matching §4.15 + W0 exact Telegram credential/network/write-root/review-window release + frozen handoff + independent audit | Current W-HYPO bytes are CONFLICTED_DO_NOT_IMPLEMENT; no W09/Mac Telegram credential, no W09 S3 or research/trade authority |
| D3-W3 | receive-clock tape adapter, deterministic merge, gaps/fees/metadata | W1 | G1/G3 prerequisite |
| D3-W4 | W-FS1 completion: OrderManager/private replay, public-size fill, partial/cancel-pending/reconcile, exact fee, forced exit and terminal ledger | W3 | G3 |
| D3-W5 | strategy-card/action interface and synthetic fixtures; only authority-admitted policies may receive economic adapters | W4 contract + per-family scope | No until tested |
| D3-W6A | SNBD, volatility, flow, queue-proxy, load and mechanism tests | W1; L2/system telemetry where needed | G2/diagnostic |
| D3-W6B | sport, scalping, directional, relative-value, terminal, external/RFQ defer and portfolio/meta breadth tracks | W1 + W2B; track-specific data | Discovery only |
| D3-W7A | OPEN_DISCOVERY breadth run on EXPLORATORY_ONLY evidence; seal coverage/backlog | W2A/W2B/W6A/W6B | No candidate output |
| D3-W7B | finite design/card/feature/action/horizon/model registries, nested TRAIN, power/commercial proposal, sealed super-family assignment/alpha graph and PROGRAM_TEST_LEDGER | W3–W7A + admitted scope + preregistered single super-family error budget | G4 TRAIN only |
| D3-W8 | one VALIDATION accept/reject of the already frozen policy; atomically CAS-claim its preassigned opening slice before cohort read | frozen registry/policy + unspent super-family allocation + W8 release | Edge-candidate decision only |
| D3-W9 | unchanged-policy confirmation/prospective opening; atomically CAS-claim its preassigned confirmation slice | W8 EDGE_CANDIDATE verdict + support gates + unspent super-family allocation + separate W9 release | G5 research gate |
| D3-W10 | dossiers, independent recomputation, operator review | W9 | No automatic next phase |
| D3-W11 | implementation conformance plus translation-complete shadow/micro-live specifications | W10 dossier with exact CONFIRMATION_COMPLETE/BACKTEST_CANDIDATE + future engineering W | Specification only; no shadow/order authority |

Parallelism allowed after W0:

- W1/W2A/W2B reader, corrected screens and breadth generator;
- W2C research-feed adapter with fixtures only until its own release/audit;
- W3/W4 replay/simulator engineering;
- W5 policy interface with synthetic fixtures only;
- W6A/W6B L1/L2 mechanism and horizontal scans.

They converge before any profit-bearing replay. No work package touches capture,
ingest or production orders. Any future production-adjacent write requires its
own W, tests and continuity plan.

No umbrella release authorizes W8 or W9 implicitly. Each opening names its
exact split, policy/config/engine/input hashes, write root, end/stopping rule
and allowed artifacts.

Engineering-code packages (D3-W2C/W3/W4/W5/W11 class) enter through the
`docs/MASTER_SEQUENCE.md` engineering queue: one W per fresh session and an
independent audit per W. Their releases name both the deep03 dependency and the
queue position. Research breadth does not jump that queue or mutate capture,
ingest or export.

### 18.1 Minimum tests before a strategy result can exist

- independent taxonomy compilers reproduce identical atomic expected keys from
  the raw manifest; coverage code cannot define its own expected universe;
- parent-map/model-axis goldens prove every M01–M27 and all 106 atomic IDs appear
  exactly once, pair-A==pair-B==pair-emitted for all 12×94 G-owner pairs,
  target-A==target-B==target-emitted for every unique
  MODEL_METHOD_TARGET_KEY, pair/target collisions fail, and F08 has only its
  base atomic ID plus exact target/control sport instances;
- golden taxonomy fixtures cover every sport alias/UNKNOWN, structure value,
  multi-relation expansion, each time endpoint, topology/data precedence and
  missing/duplicate/unexpected sentinels; every input maps exactly once per
  single-valued axis or by an explicit frozen expansion;
- five-field breadth truth-table tests include a positive fixture for each of
  the seven legal closure classes—including STRUCTURAL_CLOSED + NOT_APPLICABLE
  + NO_RESULT—and reject unknown/free-text and every
  contradictory legal-enum tuple (deferred+complete, inapplicable+finding,
  blocked+rejected, ready+not-required); infrastructure FAILED stays NO_RESULT,
  and `BREADTH_COMPLETE` rejects any unfinished/unreasoned cell;
- `RUN_STATE_ONEOF` property tests enumerate the full object/stage/run/selection/
  verdict/deployment Cartesian product, accept every specified tuple exactly
  once and reject every other combination, including TRAIN_FROZEN+COMPLETE,
  OPEN_DISCOVERY+TRAIN_SELECTED and validation+translation status; mid-run
  authority revocation/dependency loss has one blocked terminal tuple,
  BOUND_CONTROL mirrors only a passing parent guard, TRAIN control stays null-
  verdict, and PROCESS_CLOSED is the sole translation-complete tuple;
- hypothesis/experiment feed cardinality proves every distinct hypothesis and
  experiment revision creates an individual stable event and delivered
  description; a materialized-idea fixture cannot enter a report, score,
  priority, defer/reject list, queue or job before source+delivery; the
  independent reconciler binds its code/input/set SHAs and
  injects an orphan then a missing/empty source independently into each design,
  candidate, selection, method, program, card, action, horizon, model, resolved-
  run and engine source; declared/selected/resolved/engine sets must equal
  delivered revisions before RUN_AUTHORIZED or any reader/split access, startup
  actual argv/config/env/defaults must match, and pre-completion attempted/
  executed fingerprints must still equal the pre-authorized fingerprint;
- Telegram fixtures prove durable retry/multipart receipts, stale/duplicate/
  out-of-order and unauthorized replies fail closed, holdout interim outcomes
  and exact date/root/ticker membership, low-entropy hashes/sizes/timing/support
  deltas/raw split hashes are redacted, stage/opening/cohort-domain-separated
  salted commitments never reveal a future cohort and fixed-cadence constant
  heartbeats do not leak; RUN_COMPLETE/result use one non-multipart Telegram
  object/one message ID, and crash-before/after-send can deliver neither or both
  logical events but never one; source/payload/render/schema/part/batch tuples
  reconcile; independent event code/input/set SHAs bind completeness; CLOSED
  and REOPENED source events strictly alternate with exact from/to/prior SHA;
  judgments remain separate from verdict/authority and no command
  can start research, spend, mutate production or reach an order path; a second
  gateway loses the 0600 single-instance-lock race before any Telegram API call;
  Mac/W09 fixtures prove they have no token or API reachability;
  crash-before/after journal and offset advancement proves a reply is neither
  lost nor applied twice; tools.json/check_registry cover every runnable entry,
  and credentialed entries remain console-forbidden;
- super-family tests reject child-family splitting and any post-result change to
  assignment/max openings/alpha graph; CAS races, duplicate IDs and crashes
  prove only one agent can claim a slice and CLAIMED cannot silently refund;
  BOUND_CONTROL fixtures prove it uses the parent's exact split/opening/
  allocation with claimant=false and a registered control ID, cannot
  independently open/claim/promote, emits CONTROL_* only at validation/
  confirmation and still executes the preregistered untouched-evidence gate;
  missing or failed control prevents parent RUN_COMPLETE;
- judgment-application versus TRAIN_FROZEN race uses the same revision/stage-SHA
  CAS lock; only an OPEN_DISCOVERY winner may change priority, otherwise
  NOTE_ONLY; default bounded review-window blocks scoring/pruning/queue/start,
  while NO_WAIT requires an exact scoped operator waiver and silence is not consent;
- stage-transition guards prove REJECT/COLLECT_MORE/NOT_DEPLOYABLE cannot flow
  to confirmation/implementation and only exact favorable verdicts advance;
  sequential interim stays EXECUTING/null/no-RUN_COMPLETE, and completed
  COLLECT_MORE has no back-edge or reused alpha claim;
- economic-sign tests for YES/NO mapping and inventory skew;
- four golden action mappings: buy/sell × YES/NO, including complementary
  YES-normalized price, full V2 JSON and ledger sign at 1c/99c/E4 fractional
  values, including CS high-side=NO;
- post-only is explicit at the transmission/adapter boundary; crossing intent
  rejects rather than silently becoming taker;
- passive-reduce serialization golden proves GTC + post_only + verified
  reduce_only + cancel_on_pause; pause/cancel-late-fill/position-change
  sequences cannot leave an orphan, add risk or flip through flat;
- illegal `IOC+post_only`, unverified caller `reduce_only`, missing/conflicting
  trade aggressor/outcome side and missing book side all hard-reject;
- saved-v2 lifecycle fixtures cover every activated/deactivated/paused/closed/
  determined/settled/void/cancel/retire/postpone state used by the terminal
  contract;
- same-timestamp adverse ordering;
- public-counterfactual/private-authoritative fill modes are mutually exclusive;
- strict at-price no-fill and through-fill size bound;
- repeated E4 partials -> cancel race -> late fill preserve reservation;
  same-order partials versus multiple orders, duplicates and out-of-order fills
  conserve leaves/position/collateral and recover after restart;
- create reject, create-accepted/response-lost, half-active/orphan pair,
  fill-before-create-ACK, cancel 429/5xx, cancel-ACK-before-effective,
  partial-after-cancel and replace sequencing;
- fill during cancel-pending and no inventory flip after sibling fill;
- unknown ack/order/position enters reconciliation and blocks replacement;
- private WS down while public remains live; disconnect/reconnect/crash REST
  reconciliation covers exchange-only order, local-only order, position drift
  and duplicate private fill replay by client-order ID;
- gap while quote active;
- stale/pause/disconnect/unknown/reconcile;
- direct/non-direct fee golden cases cover buy/sell, maker/taker transition,
  same-order partials versus multiple orders, exact 1c rebate boundary,
  subcent/fractional E4 and series/event override;
- the official `$0.3301 × 0.03` three-fill rounding example reproduces
  byte-exact MoneyE6 revenue/rounding/net-fee/accumulator values;
- duplicate fill never advances order fee accumulator/rebate;
- PriceE4/CountE4 position and MoneyE6-or-wider cash/collateral/trade-fee/
  rounding/rebate conservation;
- forced exit ordering, multiple IOC depth levels, insufficient depth and
  worst-state residual;
- IOC latency can remove depth; fill uses effective-time book; each IOC qty is
  recomputed from reconciled position and unknown old risk blocks IOC;
- `PARTIAL_CANCEL_PENDING` with nonzero leaves blocks IOC; a later fill updates
  authoritative position before any IOC quantity is derived;
- settlement/void/retirement once-only cash flow;
- max inventory between book updates;
- deterministic bit replay;
- property tests after every event/action assert caps, no new risk while
  unknown, cancel/exit non-suppression and no `CLOSED` before reconciliation;
- deterministic sentinels fail on one mismatch; stochastic controls pass only
  the frozen equivalence rule;
- receipt lifecycle: declared-but-skipped stays unexecuted, partial run has no
  `RUN_COMPLETE`, stale `latest.json` is rejected, and every SHA mismatch turns
  readiness red.

`make check` and `tests/run_pipeline.sh` are necessary but not sufficient; the
test receipt must bind commit, dirty diff, config, data and completion state.

---

## 19. Audit Questions — every item requires YES/NO + evidence

### Authority and scope

1. Does any scope/gate change require a durable named-supersession ruling, and
   each actual W/phase a separate exact release? Expected: YES.
2. Does this plan avoid inheriting deep02's closed-run authority/budget? YES.
3. Are withdrawn agent operating-model/seven-rule documents excluded? YES.
4. Does RFQ remain deferred and nonblocking? YES.
5. Are all permitted external scores/odds/providers deferred pending one-item
   approval, while a legally blocked provider is represented anonymously and
   not named in the plan? YES.
6. Is PM-TS the only current confirmatory primary, with PM-OS, T90/T90-RB and
   CS diagnostic/proposal-only until the proper durable scope ruling? YES.

### Sample, split and statistics

7. Are provisional 20/200 handoff support and program-wide seven predesignated
   clean dates/5-of-those-7 sourced separately and protected from cherry-pick?
8. Can power lower an existing provisional gate without operator release?
   Expected: NO.
9. Is every exposed root forced to EXPLORATORY_ONLY before split sealing? YES.
10. Is the stopping rule fixed or alpha-spending, never “collect until
    significant”? YES.
11. Are root event and calendar-day, rather than trade rows, the inference
    units? YES.
12. Are scope-specific registries frozen separately, with card-level Holm when
    K>1 inside one opening and no suggestion that this resets the one super-
    family error budget? YES.
13. Are statistical and commercial gates separate? YES.

### Fees, time and terminal state

14. Is every economic row bound to clean-commit series/date/event/fill fee,
    verified account precision, rounding fee and order accumulator/rebate? YES.
15. Is the stale “maker 0 Sports moat” excluded from all economics? YES.
16. Is scheduled start sourced/as-of versioned, never inferred from close time?
    YES.
17. Are actual and scheduled starts honestly distinguished? YES.
18. Can PM omit the realized outcome label while still requiring authoritative
    payout/void/cancel/retire/postpone semantics for worst-state enumeration?
    YES.
19. Are CS and any hold-to-settlement claims additionally blocked without 100%
    realized terminal outcomes for simulated fills? YES.
20. Does terminal accounting cover retirement, walkover, cancellation, void
    and postponement without double counting? YES.

### Strategy identity and overlap

21. Does `SEED_REGISTRY_V0` correctly contain three families rather than six
    independent alphas, while the breadth ledger remains a discovery universe
    rather than a claim of 27 alphas? YES.
22. Is PM-OS a diagnostic incremental/single-side policy until durable scope
    expansion, not automatically “missing maker”? YES.
23. Does base T90 never re-enter after retreat, while T90-RB alone may quote
    after kill reaches zero-resting/reconciled using one pre-kill baseline? YES.
24. Is CS-BID explicitly a non-orderable payoff/sign BOUND_CONTROL that may run
    only on the ASK parent's exact fill/split/opening, with no BID fill/capacity,
    independent alpha claim or promotion path? YES.
25. Are T90 and certainty PnL/capacity never added despite overlapping prices?
    YES.
26. Are retreat, occupancy, flow and blindness assigned safety/selector/
    diagnostic roles instead of repeatedly counted as alpha? YES.

### Execution realism

27. Does strict-through reject at-price fills and bound quantity by
    authoritative evidence? YES.
28. Does cancel ACK remain distinct from cancel-effective, with all legal late
    partial/full fills and replacement only after terminal reconciliation? YES.
29. Are caps enforced after every fill, and can exit actions never be blocked
    by the cap? YES.
30. Are all unknown/gap/stale/pause/action ambiguities fail-closed? YES.
31. Do PriceE4/CountE4 positions and MoneyE6-or-wider cash, collateral,
    formula fee, rounding, rebate and terminal flows reconcile exactly once?
    YES.
32. Are proposed numeric retreat/re-entry/stale/timeout/stop values explicitly
    candidate grids rather than fabricated operator rulings? YES.

### Reporting and decision

33. Does each result include the five-factor receipt, full distributions,
    failure cases and data provenance? YES.
34. Is each base-root universe frozen before trigger, with all zero-trigger/
    zero-quote/zero-fill roots in the primary denominator? YES.
35. Does VALIDATION avoid selection, and is `BACKTEST_CANDIDATE` both
    confirmation-only and non-authorizing? YES.
36. Before confirmation, are unresolved commercial/capital choices explicitly
    OPERATOR-TBD, while confirmation/translation is blocked until every policy
    risk/commercial value is frozen? YES.

### Current implementation and receipts

37. Does the plan treat current create-only strategy plumbing and absent
    persistent private order manager as a P0 blocker, not reusable evidence?
    YES.
38. Does it catch that the observed `tradingd` call does not explicitly enable
    `post_only`? YES.
39. Does it reject immediate local removal of canceling orders as proof of
    cancel effectiveness? YES.
40. Does it require missing/invalid outcome side to fail closed rather than
    defaulting to YES? YES.
41. Are two-sided create requests modeled as non-atomic with worst-sequence
    reserve, orphan handling and fill-during-cancel tests? YES.
42. Are process status and economic verdict represented independently, while
    the full object/stage/run/selection/verdict/deployment cross-product is
    closed by one exhaustive `RUN_STATE_ONEOF`, including mid-run revocation/
    dependency loss, bound-control parent guards, terminal COLLECT_MORE and the
    sole PROCESS_CLOSED translation tuple? YES.
43. Do receipts distinguish declared from executed methods and bind an atomic
    `RUN_COMPLETE` plus code/diff/config/input/result hashes? YES.
44. Do deterministic sentinels require exact behavior while stochastic null
    controls use frozen equivalence bounds instead of “p > .05”? YES.
45. Is each run bound to exactly one mutually exclusive public-counterfactual
    or private-authoritative fill source? YES.
46. Do repeated partials preserve leaves reserve until terminal reconciliation
    and survive restart without one-shot-settle leakage? YES.
47. Does forced IOC wait for known old-order exposure, use effective-time L2,
    derive quantity from authoritative position and preserve emergency tokens?
    YES.
48. Does the PM-OS synthetic fixture prove its onset-anchor/persistence contract
    has a nonempty eligible path before production data is read? YES.
49. Does the CS fee illustration distinguish direct/non-direct net fees and
    remain NON-GATE until clean facts/account class are bound? YES.
50. Does independent recomputation cover population, core estimates, one tail
    statistic and exact ledgers, blocking >5% unresolved discrepancy? YES.
51. Does `AUTHORITY.json` require release ID/time, verbatim operator text and
    SHA, prompt/supersession/base-commit/worktree/W/write-root/class identity,
    spending cap default 0, prerequisites, expiry/session/status and all six
    canonical permission booleans default false; does W2C additionally require
    both Telegram permission booleans default false; and must W8/W9 be
    separately named? YES.

### Open discovery, D-5 breadth and deployment boundary

52. Is every open hypothesis scan confined to EXPLORATORY_ONLY evidence, with
    every exposed root/date/artifact added to prior exposure? YES.
53. Can a descriptive scan emit EDGE_CANDIDATE_FOR_CONFIRMATION or
    BACKTEST_CANDIDATE without a new frozen card and untouched evidence?
    Expected: NO.
54. Does D-5 permit zero survivors and forbid changing gates, fills, splits or
    stopping merely to produce a cash strategy? YES.
55. Are finite TRAIN selection, one-shot VALIDATION and unchanged-policy
    CONFIRMATION still separate, with no model/cell/router/portfolio selection
    on VALIDATION? YES.
56. Does the global selection ledger contain every feature, transform,
    threshold, model, hyperparameter and human choice allowed to win, while
    separate immutable super-family, program-test and alpha-allocation ledgers
    record every child node, opening, claim and spend? YES.
57. Is every cash-capable hypothesis/card deterministically assigned to the
    single `DEEP03_CASH_STRATEGY_V1` super-family, with child program families
    unable to mint alpha; and can report, track, sport, agent, cohort, lineage,
    release, date or newly named generation reset 0.05 or erase a failed,
    abandoned or claimed attempt? Does a registered NO_CASH BOUND_CONTROL
    inherit the parent's exact opening with claimant=false, mirror only its
    parent guards and produce no independent promotion?
    First/third expected: YES; second expected: NO.
58. Are maximum openings and the one super-family alpha/closed-testing graph
    frozen before the first VALIDATION is sealed, with each slice CAS-claimed
    under a single-writer lock before any cohort read, crash-after-claim
    consuming by default, and K/Holm separately frozen inside each opening?
    YES.
59. Are model changes, retraining, meta-routing and portfolio allocation new
    policies rather than inherited confirmations? YES.
60. Is “all angles” bounded by the exact M01–M27→106-ID map, canonical base
    F08 plus unambiguous target/control sport keys, all 1,128 G-method-owner
    pairs, unique G-owner-target keys and exact structure/time registries rather
    than asserted narratively? YES.
61. Do independently implemented compiler A and raw-manifest enumerator B each
    reconstruct parent/atomic/G-owner-pair/G-target/F08/control expected sets,
    separately prove pair-A==B==emitted and target-A==B==emitted, then prove cell
    equality with zero missing, duplicate, collision or unexpected keys? YES.
62. Does a machine-enforced closed `oneOf` truth table reject every
    contradictory five-field breadth tuple, while retaining zero-activity/
    zero-trigger and exact blocked, deferred, out-of-scope, data-invalid and
    structurally-inapplicable closures without free text, using neutral
    NOT_APPLICABLE fields for structural closure and NO_RESULT for infrastructure
    failure? YES.
63. Does the atomic taxonomy include microstructure/liquidity, time-series,
    cross-sectional, behavioral, terminal, relative-value, directional,
    external-info, ML/model methods, meta-routing, portfolio, RFQ/combo and
    UNCLASSIFIED across every captured sport, orthogonal structure, independent
    time axis and mandatory matched non-Sports control? YES.
64. Does every atomic direction freeze data/authority/engine dependencies,
    depth, method/falsifier, defer vocabulary, reopen trigger and promotion
    requirement; does every G01–G12 × 94-owner pair close and every applicable
    target have its own machine key and freeze label clock, baseline, finite
    hyperparameters/seeds, chronological fit/calibration/drift rules; and does every cell carry
    support/status/evidence/next action? YES.
65. Do all cell results obey D-3 distribution/definition/provenance/chart rules,
    including rejected and blocked cells? YES.
66. Do all new economic families retain strict-through as the binding
    historical fill gate and the complete order/fee/latency/inventory/exit/
    terminal ledger? YES.
67. Can queue-aware, at-touch or optimistic profitability independently create
    a candidate without disjoint audited own-order calibration? Expected: NO.
68. Must a portfolio undergo joint common-root replay, shared-risk/capital
    accounting, multiplicity and untouched portfolio evidence? YES.
69. Can listing an external-information track—or an anonymously represented
    legally blocked provider—authorize a call, purchase, credential or external-
    account action? Expected: NO.
70. Does a future external release bind publication/as-of time, provider,
    licensing, spend, provenance and fail-closed behavior? YES.
71. Are RFQ submission/response/acceptance prohibited and generic CLOB code
    still required to exclude MVE/combo? YES.
72. Does shadow retain a separate RISK-1 release, code-level no-order proof,
    simulated-fill label and system-non-mutation proof without falsely requiring
    the operator's entire shared account to remain unchanged? YES.
73. Can TRANSLATION_COMPLETE_SPEC or Deep03 itself authorize shadow,
    micro-live or live? Expected: NO.
74. Does micro-live still require explicit live-order permission,
    GUARDRAILS.S1..S6, a dedicated subaccount and subaccount-bound key,
    per-session operator confirmation, an exact subset of confirmed scope/
    actions/budgets, tested kill and flat/zero-resting proof? YES.
75. Is fill authority frozen by stage, with decision-policy identity unchanged,
    execution-environment identity stage-specific and private micro-live
    calibration prohibited from recomputing confirmed PnL or proving profit?
    YES.

### Telegram operator research loop

76. Does every materialized distinct hypothesis and every population/action/
    horizon/feature/model/variant/estimand/fill/stopping/falsifier experiment
    revision receive its own stable event and individual Telegram description
    before it can be scored, pruned, queued or run, without claiming access to
    private unmaterialized reasoning? YES.
77. Are all jointly tested finite variants and their selection rule visible in
    the experiment message without truncation, and does independent design/feed
    reconciliation match every declared, attempted and selected fingerprint in
    the design/candidate/selection/method/program/card/action/horizon/model/
    resolved-run/engine sources **before RUN_AUTHORIZED or any data access**,
    recheck actual execution before completion, bind independent code/input/set
    hashes and fail on a missing/empty source? YES.
78. Does an independent completeness receipt prove exact source-union versus
    feed-event closure and exact delivered equality on event, source, payload,
    rendered-content, schema-version and multipart-index/count tuples, rejecting
    stale, conflicting, truncated and orphan receipts; does it bind independent
    event-enumerator code/input/set SHAs; and are CLOSED and REOPENED directional,
    prior-linked, strictly alternating lifecycle sources rather than a mutable
    snapshot? YES.
79. Are operator judgment and application journals members of the canonical
    source union; are judgments append-only and orthogonal to disposition,
    verdict, stage and authority; and does application use the same revision/
    stage-receipt CAS boundary as CARD/TRAIN, becoming NOTE_ONLY on a lost race
    or after TRAIN freeze? YES.
80. Can “深挖”, “暂停”, “毙掉” or free text start compute, spend,
    open a holdout, change a sealed card, promote a result or send an order?
    Expected: NO.
81. Are chat/user allowlists, reply-to event/revision binding, replay protection,
    acknowledgment and stale/unauthorized/injection rejection mandatory; must
    one production-monitor gateway holding the only bot token win a 0600 lock
    before any Telegram API call; do Mac/W09 remain credential-free; and is
    accepted input journaled/fsynced before a persistent offset advances, with
    that status-bot service the sole `getUpdates` consumer? YES.
82. During VALIDATION/CONFIRMATION, are interim sign/PnL/p-value/ranking,
    observed support/membership, exact cohort identifiers and inferable low-
    entropy hashes/sizes/timing/deltas withheld; are commitments domain-separated
    by stage/opening/cohort so one result cannot reveal a future cohort; and are
    heartbeat cadence/payload constant until RUN_COMPLETE plus full result are
    delivered as one non-multipart Telegram object/message ID? YES.
83. Does Telegram outage leave capture/ingest/trading and already feed-complete
    unrelated research untouched while durably queuing messages and blocking
    only the affected idea's scoring/scheduling/run/promotion until delivery
    completeness catches up? YES.
84. Do the new producer/publisher/reply-handler paths import no order client,
    call no exchange/AWS endpoint, read no trading credential and write no
    capture/ingest/S3 path, with a separately audited handoff and no reachability
    from research replies into `/ack`; and are every runnable entrypoint and
    deployed service covered by `tools.json`/reverse registry checks, with
    credentialed Telegram entries console-forbidden? YES.
85. Does every idea/result message name owner/work package, review-window policy,
    run/artifact identity, exact evidence class, uncertainty/tails/failure
    cases, legal verdict and next operator decision; does a bounded judgment
    window block first narrowing/start unless an exact scoped operator waiver
    names NO_WAIT; and are neither silence nor Telegram treated as authority?
    YES.

Any NO on authority, clock/leakage, fee, strict fill, ledger conservation,
terminal state, split integrity, exposure containment or coverage set equality
requires `REVISE`/`REJECT`, not a waiver.

---

## 20. Ready-to-send independent audit instruction

```text
Task: independently audit
docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md

Repository: /Users/ritcardo/HFT BOT
Branch: plan-sports-market-dynamics-v2

Read docs/GUARDRAILS.md and docs/MM_ROADMAP.md first. This is a read-only,
paper-only audit: do not run research, open holdouts, mutate production, spend
W09 budget, edit capture/ingest/export, or send orders. Treat “deep03” and
“pub-ref v3” as different objects.

First compute and pin the target SHA-256; neither the archived candidate-0.3
audit nor reviews of candidate-0.4/candidate-0.5/candidate-0.6/candidate-0.7
cover candidate-0.8. Verify
every claim against repository code/docs and current git state. Answer all 85
Audit Questions YES/NO with exact file:line
evidence. Specifically try to falsify: authority/scope; D-5 taxonomy and exact
106-ID parent/atomic, F08 target/control, 1,128 G-owner-pair and unique G-target
coverage-set equality; breadth and run-state closed truth tables, including
mid-run revocation, bound-control guards and COLLECT_MORE; prior-exposure containment;
the one-cash-super-family assignment, alpha graph and claim-before-read CAS;
NO_CASH parent-bound control semantics;
single-name state definitions; fee facts; scheduled-start/terminal semantics;
strategy identity/overlap; state-machine implementability; strict-through fill
quantity; cancel-pending exposure; pathwise accounting; sample/power/stopping;
negative controls; tail/concentration/capacity; external/RFQ/portfolio/shadow/
micro-live boundaries; fill-authority-by-stage; dedicated-account isolation;
hypothesis/experiment Telegram cardinality, pre-authorization resolved-engine/
design/feed reconciliation, directional lifecycle-source obligations, exact
content-part/sealed-batch equality, domain-separated salted split/constant-
heartbeat sealed-cohort side channels, bounded operator-review opportunity and
judgment non-authority; one-token/one-gateway pre-API locking, persistent-offset
delivery safety and E3 registry coverage; and whether empty or stale tests can
appear green.

Deliver:
1. verdict PASS_FOR_RELEASE_DRAFTING / PASS_WITH_EXPLICIT_BLOCKERS / REVISE /
   REJECT;
2. findings ordered P0/P1/P2;
3. contradictions and missing prerequisites;
4. exact proposed text corrections (do not apply them);
5. a seed-card table: estimable now / blocked / required data / legal verdict;
6. a direction-ledger coverage table naming any missing/duplicate/unbounded
   mechanism, sport, structure, time or status cell;
7. confirmation that the audit itself made no project changes.
```

---

## 21. References

- `docs/GUARDRAILS.md`
- `docs/MM_ROADMAP.md`
- `docs/PLAN_SPORTS_TRADING_DECISIONS.md`
- `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md`
- `docs/PLAN_MM_TEST_PROGRAM.md`
- `docs/research_reports/DEEP03_SPEC_NOTES.md`
- `docs/SPORTS_AUTORESEARCH_02_AUTHORIZATION_2026-07-15.md`
- `docs/SPORTS_AUTORESEARCH_02_AUTHORIZATION_EN_2026-07-15.md`
- `docs/DESIGN_BACKTEST_GAP_MAP_2026-07-16.md`
- `docs/plan_audits/DETECTION_RUN_2026-07-17.md`
- `docs/plan_audits/AUDIT_DEEP03_GRANULAR_PLAN_2026-07-17.md`
- `docs/PLAN_HYPOTHESIS_FEED_TELEGRAM_2026-07-17.md` (original-request and
  conflict evidence only; current bytes are `CONFLICTED_DO_NOT_IMPLEMENT`)
- `docs/SESSION_LOG.md`
- `docs/vendor/kalshi/latest/openapi.yaml`
- `config/kalshi_facts.yaml`
- `config/backtest_latency.yaml`
- `sandbox/discovery/fee_rounding.md`
- `sandbox/research/mm_sandbox.py`
- `deploy/tg_common.py`
- `deploy/tg_intel.py`
- `deploy/status_bot.py`
- `deploy/w09/research_data_instance_profile.py`
- `deploy/w09/install_on_host.sh`
- `include/kalshi/strategy.hpp`
- `include/kalshi/wire.hpp`
- `include/kalshi/risk_ledger.hpp`
- `include/kalshi/rule_engine.hpp`
- `apps/tradingd.cpp`
- `src/gateway.cpp`

End of audit candidate. Nothing below this line is authorized execution.
