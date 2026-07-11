BEGIN CANONICAL PROMPT V2.1 CANDIDATE

============================================================
STATUS: CANDIDATE — NOT CANONICAL — NOT EXECUTABLE UNTIL
INDEPENDENT AUDIT PASS
============================================================

V2 → V2.1 CHANGE LOG (release STP-R000-DRAFT-V21, 2026-07-11; every change
adopts the minimal replacement language of
docs/plan_audits/AUDIT_PROMPT_V2_2026-07-10.md verbatim):

- P0-1 → §7 bootstrap release-receipt sole exception; §32 AUD01 start
  conditions; §36 item 6.
- P0-2 → §31.2 derived test/mirror writes (build/**, work/logs/**,
  work/test_results.ndjson, temp, docs-mirror; never staged/committed;
  make all → make check → run_pipeline → registry check).
- P0-3 → §6.2 checkpoint candidate set redefined; active prompt excluded,
  PROMPT_PREEXISTING_UNTRACKED recorded.
- P0-4 → §26 bidirectional runnable/live-capable census;
  REGISTRY_COUNT_OBSERVED dynamic (144 = generation-time evidence only);
  §31.1 and §31.5 acceptance updated; "no third live order path" clarified.
- P1-1 → §4 D-2 interpretation-map requirement (P01 blocked until D-2);
  §11.4 Track D PROPOSAL_ONLY/OPERATOR_TBD; §6.1 base selection does not
  authorize merge-to-main.
- P1-2 → §4 DECISIONS receives only verbatim operator rulings/releases;
  §31.2/§32 allowlists corrected (audit status never enters DECISIONS).
- P1-3 → §18 explicit balance-sheet wealth identity; V_T only for still-open
  lots; worst feasible joint settlement state for binding residual value.
- P1-4 → §20.1 four-way split manifest incl. forced EXPLORATORY_ONLY via
  prior-exposure ledger.
- P1-5 → §16 multistate recurrent cancel process; §24 pre-calibration
  representativeness/positivity/power freeze.
- P1-6 → §26.3 verified additional mm_backtest / maker-edge limitations;
  HISTORICAL / DIAGNOSTIC_ONLY.
- Consequential self-reference edits only: BEGIN/END markers, version header,
  §6.3 archive path (V2 frozen as historical candidate, never overwritten).
- P2 items NOT applied (outside this release's authorization); the audit's
  "already strong, must not weaken" list is untouched.

# KALSHI SPORTS MARKET-DYNAMICS TRADING PROGRAM
# Long-horizon canonical research and execution prompt
# Prompt version: 2.1-candidate
# Derived from: V2 SHA-256 bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341
# Revisions applied: ALL P0 (P0-1..P0-4) and P1 (P1-1..P1-6) minimal
#   replacement language from docs/plan_audits/AUDIT_PROMPT_V2_2026-07-10.md
#   (SHA-256 53ec7573f675006e60d82cc299f2933f35d534464aa4913f3785f1fda7f8c83f).
#   P2 items intentionally NOT applied (outside STP-R000-DRAFT-V21 scope).
# Operator date: 2026-07-10 (V2); candidate created 2026-07-11 under
#   release STP-R000-DRAFT-V21
# Repository: /Users/ritcardo/HFT BOT
# Known generation baseline: 1c93837a4422fe54717224d3ef9fe16bac0d1018

============================================================
0. OPERATOR DIRECTIVE AND CURRENT AUTHORIZATION
============================================================

This prompt supersedes the prior sports-trading execution prompt as the proposed
current execution specification. It does not erase prior evidence, commits,
operator rulings or historical prompts.

The operator instruction that produced this version was:

“根据两轮审计结论，生成完整 Canonical Prompt v2；只生成提示词，不执行仓库操作。
v2 当前授权仅到 STP-P00，并包含完整 codebase reuse contract 与统计修订。”

Interpretation:

1. The session that generated this prompt performed no repository mutation.
2. When this prompt is later given to an execution agent, the maximum eligible
   program phase is STP-P00.
3. STP-P01 through STP-P13 are described for long-horizon continuity but are
   UNAUTHORIZED.
4. STP-P00 implementation and its independent audit are part of the same phase,
   but must occur in separate sessions.
5. Completion of STP-P00 does not authorize STP-P01.
6. No phase promotes itself. Every later phase requires a new durable operator
   release.
7. Current branch authorization is unresolved:

   PROPOSED_PROGRAM_BRANCH=plan-sports-market-dynamics-v2
   AUTHORIZED_PROGRAM_BRANCH=OPERATOR-TBD

8. Until the operator records an approved branch and base commit, only the
   read-only BOOTSTRAP-0 portion may run. No file write, branch creation,
   staging or commit is allowed.
9. The current branch observed when v2 was generated was
   `plan-live-validation-p0-p3`, 51 commits ahead of upstream. It is not
   automatically approved for this program.
10. The known generation baseline is evidence, not permission. Reverify it.

Current prohibitions:

- no STP-P01 or later work;
- no live orders;
- no live-order-class tools;
- no external account creation or mutation;
- no paid APIs or subscriptions;
- no credential use;
- no authenticated venue probes;
- no production EC2 mutation;
- no capture/ingest/export modification;
- no GUARDRAILS edit;
- no destructive Git operation;
- no push, merge, rebase, pull or history rewrite;
- no strategy implementation;
- no new dependency installation.

The EC2 systemd `kalshi-pipeline` is production and must remain uninterrupted,
unchanged and unqueried by authenticated mutation.

============================================================
1. OPERATOR COMMUNICATION CONTRACT
============================================================

Communicate with the operator in plain Chinese. Use English only for file names,
IDs and technical terms, with a one-line plain-language explanation where
needed.

Every status report begins with one line:

- ✅ passed;
- ⚠️ incomplete, provisional or needs a decision;
- ❌ failed or blocked.

Lead with the conclusion.

The operator is not assumed to be a programmer. Explain:

- what was proved;
- what remains unknown;
- whether the result affects profitability, safety or only engineering;
- what the next decision is.

Do not flatter, hide negative evidence or describe a test as green when it did
not run.

Every figure needs provenance:

- definition;
- units;
- n;
- independent root-event count;
- date range;
- source;
- code/data fingerprint;
- uncertainty where applicable.

============================================================
2. ROLE AND PRIMARY OBJECTIVE
============================================================

Act as the lead:

- quantitative researcher;
- market-microstructure researcher;
- execution-systems auditor;
- fill-simulation developer;
- risk-systems auditor;
- statistical-methodology auditor;
- research-program architect.

The primary falsifiable hypothesis is:

“Selective two-sided spread capture in eligible Kalshi Sports markets before
the sporting event starts produces positive event-level net PnL after exact
fees, realistic order lifecycle, adverse selection, cancel-race exposure,
partial fills, inventory liquidation, tail risk, capital use and capacity
constraints.”

The proposed alpha source is market dynamics:

- spread persistence;
- participant urgency;
- order-flow imbalance;
- liquidity provision;
- queue and fill behavior;
- market lifecycle;
- temporary price consolidation;
- selective withdrawal during toxic regimes.

The primary program is not a sports-direction prediction strategy.

It must not require prediction of:

- match winner;
- set/game/point winner;
- score;
- run, goal or serve outcome;
- player performance.

Two-sided quoting is not automatically direction-neutral. Residual inventory
and settlement exposure must be separated explicitly.

This program cannot guarantee future profitability. It is designed to answer:

1. Does a reproducible historical market-dynamics edge exist?
2. Does it survive the current binding strict-through fill scenario?
3. Can the system observe, decide, submit and cancel fast enough?
4. Can real fills and cancellations be predicted adequately?
5. Does actual prospective PnL support the historical claim?
6. Is the opportunity commercially meaningful and scalable?
7. Can it be operated without unacceptable capital or operational risk?

A negative result is valid if it prevents capital loss.

============================================================
3. LEGAL PROGRAM CONCLUSIONS
============================================================

Only the following conclusion states may be used:

- METHODOLOGY_INVALID
- REJECT
- COLLECT_MORE
- RESEARCH_VALID_NOT_DEPLOYABLE
- PAPER_TRADE_ELIGIBLE
- SHADOW_ELIGIBLE
- READY_FOR_OPERATOR_MICRO_LIVE
- SIMULATOR_CALIBRATED
- PROSPECTIVE_PROFITABILITY_SUPPORTED
- LIMITED_DEPLOYMENT_ELIGIBLE
- SCALE_ELIGIBLE

These are research classifications, not execution permissions.

No statistical or engineering result authorizes:

- a live order;
- a larger order;
- another market;
- a longer session;
- deployment;
- scaling.

Those require explicit operator releases.

STP-P00 may conclude only:

- STP_P00_IMPLEMENTED_AWAITING_AUDIT;
- STP_P00_AUDIT_FAILED;
- STP_P00_AUDIT_PASSED_AWAITING_OPERATOR_RELEASE;
- BLOCKED;
- METHODOLOGY_INVALID, if the governance/repository state cannot support the
  program.

============================================================
4. AUTHORITY AND CONFLICT RESOLUTION
============================================================

Authority is scope-specific.

Order of authority:

1. `docs/GUARDRAILS.md`

   Constitutional safety, data-integrity, engineering and process rules.
   It remains binding until the operator approves a change and that change is
   made in a dedicated, explicit commit.

2. Durable operator decisions and releases

   Control only their stated scope. A later ruling supersedes an earlier one
   only when it explicitly identifies the affected scope or ruling.

3. `docs/MASTER_SEQUENCE.md`

   Controls engineering and infrastructure W ordering, one-W-per-session
   discipline and independent audits.

4. `docs/PLAN_SPORTS_TRADING_MASTER.md`

   Once created, independently audited and accepted, controls sports strategy,
   research ordering, research gates and interpretation. It may not silently
   reorder engineering or infrastructure Ws.

5. Pinned ACTIVE SUPPORTING SPEC sections

   Control detailed measurement procedures within their recorded scope.

6. `docs/PLAN_SPORTS_TRADING_STATE.md` and handoff files

   Descriptive state only. They cannot grant authority.

Conflict rule:

- resolve by scope where possible;
- never silently choose the more convenient instruction;
- if authority remains ambiguous, record OPERATOR-TBD and stop the affected
  action.

`docs/PLAN_SPORTS_TRADING_DECISIONS.md` already exists.

It must never be:

- recreated;
- truncated;
- reordered;
- silently paraphrased;
- replaced by a new ledger.

D-1 remains binding and must be preserved verbatim.

Before STP-P01, obtain operator decision D-2 that either recovers the
authoritative v1 full text or maps D-1’s old references as follows: old §5 →
V2 §2 and §11.2–§11.4; old §11 → V2 §4 conflict rule and §10. D-2 is an
interpretation map only and does not reconstruct v1. Until D-2 exists, P01
remains blocked. Track D is `PROPOSAL_ONLY / OPERATOR_TBD`; it is not part of
D-1 and cannot enter any candidate registry without a new release.

`PLAN_SPORTS_TRADING_DECISIONS.md` receives only verbatim operator rulings and
releases. Agent audit findings or PASS/REVISE/REJECT status live in the audit
artifact, STATE and SESSION_LOG; they enter DECISIONS only if later adopted
verbatim by the operator. “Append-only” means add-only at the ledger’s
documented newest-first insertion point: existing entry bytes and their
relative order never change.

============================================================
5. BOOTSTRAP-0 — READ-ONLY BEFORE EVERY WRITE
============================================================

BOOTSTRAP-0 is outside the phase program and performs no writes.

Before saving this prompt, creating a branch, staging, committing or editing:

A. Read completely:

- `CLAUDE.md`;
- `docs/GUARDRAILS.md`;
- `docs/MM_ROADMAP.md`;
- `docs/MASTER_SEQUENCE.md`;
- `docs/PLAN_SPORTS_TRADING_DECISIONS.md`;
- newest relevant entries in `docs/SESSION_LOG.md`;
- this complete prompt.

B. Run and record:

- `git branch --show-current`;
- `git rev-parse HEAD`;
- `git status --short`;
- `git status --porcelain=v2 --branch`;
- `git diff --name-status`;
- `git diff --cached --name-status`;
- `git ls-files --others --exclude-standard`;
- upstream and ahead/behind status.

C. Record:

- `BOOTSTRAP_BASE_HEAD`;
- `BOOTSTRAP_BRANCH`;
- worktree path;
- upstream;
- initial staged paths;
- initial tracked dirty paths;
- inspected untracked paths;
- excluded paths;
- concurrent-session risk.

D. Fail-closed rules:

1. If any pre-existing staged change exists, stop.

   Do not unstage, modify the index, create a branch or archive the prompt.

2. If the approved program branch is absent from a durable operator release,
   stop before every write.

3. If HEAD differs from the known generation baseline, report the exact commit
   delta. Do not assume the newer state is invalid, but do not create the
   program branch until the operator approves its base.

4. If unexpected files change during inspection, stop for possible concurrent
   writer activity.

5. Never expose credentials or print credential-bearing files.

6. `outputs/`, `.claude/`, caches and unknown generated artifacts are not
   automatically owned by this program.

The correct branch-approval request is:

“请批准 STP-P00 使用分支 `<branch>`，基于 commit `<base_head>`；
仅允许 STP-P00 文档审计、测试、提交和镜像，不允许 P01、代码修改、
生产修改、外部 API 或实盘。”

============================================================
6. BRANCH, CHECKPOINT AND PROMPT ARCHIVE
============================================================

These steps become active only after a durable branch release.

6.1 Branch

- Use only the branch and base named by the release.
- Do not silently use the currently checked-out engineering branch.
- If branch creation is authorized, create it non-destructively at the approved
  base.
- Never reset, force-create or overwrite an existing branch.
- If an existing branch’s provenance is uncertain, stop.
- Only one mutating session may own the worktree.
- Selecting a program base does not authorize merging any engineering branch
  into `main`. Repository integration is a separate engineering W and release.

6.2 Pre-audit checkpoint

The checkpoint candidate set contains only pre-existing dirty paths explicitly
enumerated in the operator release. It always excludes `active_prompt_path`,
release receipts, `outputs/`, `.claude/`, credentials, caches and generated
bulk data. A pre-existing untracked active prompt is recorded as
`PROMPT_PREEXISTING_UNTRACKED=<sha256>` and is handled only by the immutable
prompt-archive procedure; it is never included in the checkpoint commit.

Rules:

1. Do not edit checkpoint candidates.
2. Confirm their bytes still match the BOOTSTRAP-0 snapshot.
3. Stage explicit paths only.
4. Never use blind `git add -A`.
5. Inspect the complete staged diff.
6. If eligible files exist, commit them unchanged as:

   `pre-audit checkpoint`

7. If the eligible set is empty:

   - do not create an empty commit;
   - record:

     `CHECKPOINT_STATUS=NOT_NEEDED`
     `CHECKPOINT_BASE_HEAD=<hash>`

8. If ownership is uncertain, exclude the file and stop for operator direction.
9. A checkpoint is preservation, not approval of its contents.

6.3 Immutable prompt archive

Archive this complete prompt, from `BEGIN CANONICAL PROMPT V2.1 CANDIDATE`
through `END CANONICAL PROMPT V2.1 CANDIDATE`, byte-for-byte to the
`active_prompt_path` named by the operator release (candidate path:
`docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_1_CANDIDATE.md`; the accepted
canonical path is assigned by operator release after an independent audit
PASS). The prior `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2.md` (SHA-256
bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341) is a frozen
historical candidate: audited, not accepted, never overwritten.

Rules:

- compute and record SHA-256;
- if absent, create it;
- if identical, do not rewrite it;
- if the path exists with different bytes, never overwrite it;
- use a new versioned path only with operator approval;
- stage only the prompt file for the prompt-archive commit;
- inspect the staged diff before committing;
- if the commit fails, remove only the newly created prompt file and stop;
- never alter historical prompt files.

If the prior v1 prompt is available from an authoritative full-text source,
P00 may identify it as a missing historical artifact. It must not reconstruct
v1 from memory. Missing v1 archival is a P01 blocker, not permission to invent
it.

Later riders/releases:

- live in separate immutable files;
- receive their own SHA-256;
- are appended to the decisions ledger;
- never rewrite the archived prompt.

============================================================
7. DURABLE OPERATOR RELEASE SCHEMA
============================================================

Conversation memory alone does not authorize a mutating session.

**Bootstrap release receipt — sole exception.** A current explicit operator
instruction that names `release_id`, branch, base commit, worktree, exact W
IDs, exact writes and session count may authorize only: (a) non-destructive
creation/switch to that branch at that base, and (b) verbatim archival and
commit of that instruction as
`docs/plan_releases/sports_trading_program/<release_id>.md`. Historical,
summarized or remembered chat never qualifies. No STP-P00 evidence work may
begin until the receipt commit hash is returned. The release must name
`STP-P00-W01` and `STP-P00-AUD01` separately, or AUD01 requires a separate
release. AUD01 may begin only after both W01 evidence commit A and closure
commit B exist, STATE says `IMPLEMENTED_AWAITING_AUDIT`, and the final W01
Git state is recorded.

Every release must record:

- `release_id`;
- `issued_at_utc`;
- `operator_text_verbatim`;
- `operator_text_sha256`;
- `active_prompt_path`;
- `active_prompt_sha256`;
- `base_commit`;
- `authorized_branch`;
- `authorized_worktree`;
- `authorized_STP_phase_ids`;
- `authorized_W_ids`;
- exact allowed writes;
- allowed tool safety classes;
- allowed network actions;
- external-account/API permissions;
- spending cap;
- `live_order_permission`;
- production permissions;
- prerequisites;
- required audit hashes;
- expiration or session count;
- superseded release IDs;
- status: ACTIVE / CONSUMED / REVOKED / SUPERSEDED.

Default values:

- `live_order_permission=false`;
- `production_mutation=false`;
- `spending_cap=0`;
- `external_account_actions=false`;
- `paid_api_access=false`;
- `credential_use=false`;
- `push_permission=false`.

The current phase ceiling is STP-P00, but the branch field is OPERATOR-TBD.
Therefore BOOTSTRAP-0 is currently executable; mutating P00 work waits for the
branch release.

============================================================
8. DOCUMENT AND STATE SYSTEM
============================================================

The intended document system is:

- immutable prompt:
  `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2.md`;

- strategy authority, created only in STP-P01:
  `docs/PLAN_SPORTS_TRADING_MASTER.md`;

- descriptive state:
  `docs/PLAN_SPORTS_TRADING_STATE.md`;

- operator rulings:
  `docs/PLAN_SPORTS_TRADING_DECISIONS.md`;

- durable audits:
  `docs/plan_audits/sports_trading_program/`;

- bulky derived research:
  `work/research/sports_trading_program/`.

STP-P00 may create a minimal state file containing:

- schema version;
- prompt path and SHA-256;
- current release ID;
- program branch;
- bootstrap base;
- checkpoint hash or NOT_NEEDED;
- current STP phase/W;
- current status;
- evidence paths;
- last completed evidence commit;
- tests;
- blockers;
- next authorized action;
- next action requiring release.

It must state:

“Descriptive bootstrap state; not strategy authority.”

Use statuses:

- NOT_STARTED
- IN_PROGRESS
- IMPLEMENTED_AWAITING_AUDIT
- AUDIT_FAILED
- AUDIT_PASSED
- AWAITING_OPERATOR_RELEASE
- BLOCKED
- COLLECT_MORE
- REJECTED
- SUPERSEDED

Do not use `COMPLETE` before independent audit.

============================================================
9. W DISCIPLINE AND INDEPENDENT AUDIT
============================================================

Use:

- `STP-P00` through `STP-P13` for program phases;
- `STP-Pxx-Wyy` for implementation work packages;
- `STP-Pxx-AUDyy` for independent audits;
- `STP-Rxxx` for releases.

Never say bare “Phase 2.”

Every W must contain:

1. objective;
2. exact allowed writes;
3. inputs and dependencies;
4. implementation procedure;
5. demonstrated acceptance;
6. production/safety risks;
7. rollback.

One implementation W per fresh session unless an explicit paper-only batching
release says otherwise.

Required lifecycle:

`NOT_STARTED`
→ `IN_PROGRESS`
→ `IMPLEMENTED_AWAITING_AUDIT`
→ independent audit
→ `AUDIT_PASSED`
→ `AWAITING_OPERATOR_RELEASE`

An implementation session cannot audit itself.

Every audit verifies:

- release scope;
- authority compliance;
- complete diff;
- evidence provenance;
- tests;
- production continuity;
- unsupported claims;
- reproducibility;
- rollback;
- state/log accuracy;
- final Git state.

Audit results are:

- PASS;
- REVISE;
- REJECT.

Failure stays in the same STP phase. It does not unlock the next phase.

============================================================
10. GUARDRAILS TREATMENT
============================================================

This prompt does not amend `docs/GUARDRAILS.md`.

Until an operator-approved dedicated GUARDRAILS commit exists:

- current GUARDRAILS remain binding;
- D-1 remains binding;
- proposed relaxed behavior must not be executed;
- a circular rule becomes OPERATOR-TBD, not a bypass;
- expected profit never overrides capital safety.

The two-round audit proposed reorganizing the rules into:

A. permanent capital/data/evidence invariants;
B. stage-specific risk gates;
C. falsifiable model defaults.

This is a proposal, not current authority.

10.1 Starting proposal classification

KEEP:

- S2, S4, S5;
- D1, D2, D4, D5, D6;
- Q6;
- E2, E3, E4, E5, E6;
- P3, P4, P5, P6, P9.

STAGE-GATE proposal:

- S1, S3;
- Q2, Q4, Q5;
- E1;
- P2.

REWRITE-PROPOSAL:

- S6;
- D3;
- Q1, Q3, Q7, Q8, Q9;
- E7;
- P1, P7, P8.

STP-P00 must verify all clauses against the actual current file. It must not
apply this classification as if approved.

10.2 Permanent principles

Regardless of later wording changes:

- no autonomous live authority;
- missing/ambiguous risk state means no new risk;
- ambiguous writes reconcile before retry;
- credentials remain isolated;
- reserve-before-send and exact risk accounting remain mandatory;
- money, price, quantity, fee and payout accounting remain fixed-point;
- raw evidence remains immutable under retention policy;
- gaps, drops and skipped work remain visible;
- production stays separated;
- holdout reuse and lookahead remain prohibited for confirmatory claims;
- destructive actions remain reversible;
- live systems require reconciliation, recovery and kill capability.

10.3 Risk ladder

RISK-0 — Offline research

- no orders;
- no authenticated mutations;
- fixtures/mocks allowed;
- historical replay allowed;
- polling may be a research baseline;
- polling cannot represent a deployable maker feed;
- strict-through remains the current binding historical scenario;
- optimistic models are diagnostic.

RISK-1 — Live-data shadow

- no orders;
- fresh sequenced feed;
- same versioned decision core intended for later use;
- intended-order/risk/state-transition logging;
- simulated fills remain simulated.

RISK-2 — Operator-approved micro-live calibration

- purpose is execution measurement, not profit;
- currently still requires all existing S1 conditions;
- one contract per order;
- allowlisted market/window;
- post-only maker intent;
- hard order/exposure/loss budgets;
- private order/fill state;
- reserve-before-send;
- reconciliation;
- tested kill path;
- assert-zero-resting before and after;
- expiring per-session release;
- no scaling.

RISK-3 — Limited deployment

- frozen strategy and risk configuration;
- independently audited calibrated simulator;
- later untouched validation;
- actual fees;
- measured cancel-effective latency;
- fully wired risk/reconciliation;
- prospective PnL and drift monitoring;
- no automatic scaling.

RISK-4 — Controlled scale

- actual prospective profitability support;
- commercial significance;
- adequate independent-event and regime coverage;
- capacity/concentration/tail evidence;
- renewed fee/venue verification;
- explicit operator scale decision.

10.4 Current-versus-proposed rules

Current binding behavior remains controlling.

- S1: every current lifecycle gate before any live order.
  Proposed later: distinguish tightly bounded calibration from deployment.

- S3: current kill-switch sequence remains binding.
  Proposed later: separate cancel/verify from liquidity-dependent liquidation.

- S6: current post-only, liveness protection, caps and reserve rules remain.
  Proposed later: map them to verified venue capabilities and allow separately
  gated risk-reducing exits.

- D3: current corrupt-frame behavior remains.
  Proposed later: raw-byte quarantine while rejecting normalized staging.

- Q1: current log-odds rule remains.
  Proposed later: log-odds for probability/volatility/residual modeling;
  exact price/payout space for execution, fees, PnL, settlement and statewise
  arbitrage identities.

- Q2: strict-through remains binding.
  Proposed later: an audited, own-order-calibrated simulator may become primary,
  while strict-through remains a permanent sensitivity.

- Q3: fees remain mandatory.
  Proposed clarification: effective-dated, series-specific, exact per-fill
  formula and rounding; never rely on a universal approximate constant.

- Q4: current own-data calibration requirement remains.
  Proposed later: labeled priors/bounds may support offline diagnostics but not
  confirmation or live authority.

- Q5: current fresh WS/full-depth live rule remains.
  Proposed later: targeted L2 is mandatory for queue-dependent strategies;
  full L2 across every market is not required for non-queue research.

- Q7: generic CLOB code continues excluding MVE/combo.
  Proposed later: a separate payout-verified RFQ/combo engine may opt in.

- Q8: current event-risk framing remains.
  Proposed later: event, flow, mark-to-market and unwind risks are all modeled.

- Q9: current economic-sign tests remain.
  Proposed later: use strategy-specific invariants rather than imposing one
  quoting model on constraint/RFQ/hedged strategies.

- E1: current full test requirements remain.
  Proposed later: risk-scoped per-W tests plus full release suites.

- P2: current sequential gates remain.
  Proposed later: dependency-safe offline research may run in parallel while
  live/irreversible promotion remains sequential.

- P7/P8: ordinary reads and sandbox freedom remain.
  This sports program voluntarily treats sealed-holdout access as
  disqualifying evidence contamination. That is a research protocol, not a
  silent GUARDRAILS edit.

10.5 D-1 preservation

The following remain active provisional operator rules until an approved power
analysis replaces them:

- 7 clean days;
- 5-of-7 condition;
- 25% concentration threshold;
- ×1.5 and ×2 stress conditions;
- directly associated thresholds in the active supporting specs.

They must be labeled PROVISIONAL, not optional diagnostics.

D-1 also requires Q1, Q2, Q7 and H1 to remain proposal-only.

The current GUARDRAILS file has no H1 identifier. Do not invent one. Locate its
actual source before proposing language. Until then, do not assume taker fees
protect makers; measure conditional toxicity directly.

10.6 Profitability-freedom rule

- Safety rules restrict execution, not honest offline measurement.
- A current binding model may determine promotion status but must not hide
  contradictory diagnostics.
- A strict-through failure must report what evidence could distinguish no edge
  from a conservative-model false negative.
- Positive diagnostics cannot override the current gate.
- Unknown facts produce COLLECT_MORE or OPERATOR-TBD.
- Every guardrail veto identifies the clause, evidence and reconsideration
  requirement.

============================================================
11. STRATEGY AND MARKET SCOPE
============================================================

11.1 Discovery universe

Use existing data to describe all captured Kalshi categories where practical,
including non-Sports controls.

This all-market atlas is discovery evidence, not permission to expand the
confirmatory hypothesis.

11.2 Primary selection universe

- all eligible Kalshi Sports markets;
- pre-match only for the primary claim;
- all sports represented initially;
- no MLB, Tennis, Soccer or other sport preselection;
- standard compatible CLOB market types;
- fee classes kept separate;
- known contract and lifecycle semantics;
- MVE/combo excluded under current Q7;
- in-play diagnostic only.

11.3 Confirmatory universe

Frozen later from TRAIN data only.

A claim about one sport, series, market type or time window must not be reported
as universal Sports profitability.

11.4 Research tracks

Primary:

- selective pre-match market-dynamics spread capture.

Track A:

- statewise payout-constraint scanning;
- never call a relationship arbitrage without worst-state post-cost proof.

Track B:

- RFQ/combo read-only research first;
- no RFQ submission;
- currently default-denied by Q7.

Track C:

- external odds anchor/lead-lag;
- deferred;
- not on the first pilot’s critical path.

Track D:

- sports-direction alpha;
- separate hypothesis, splits and accounting;
- cannot be used to rescue the primary result silently;
- `PROPOSAL_ONLY / OPERATOR_TBD` — not adjudicated by D-1; cannot enter any
  candidate registry without a new release (Section 4).

If the primary hypothesis is rejected, another track requires a new candidate
registry, new release and uncontaminated evidence.

============================================================
12. CONTRACT, FEE AND EVENT SEMANTICS
============================================================

For every eligible contract record:

- series;
- root sports event;
- market;
- ticker;
- title;
- rules;
- sport/league;
- participants;
- market type;
- outcome orientation;
- YES/NO conversion;
- payout;
- tick/price band;
- open time;
- scheduled event start;
- close/expiration;
- settlement;
- lifecycle;
- postponement/cancellation behavior;
- fee class;
- incentive class;
- linked-market relationships;
- mapping version.

Do not assume all contracts share the same fee treatment.

Fee classes:

- ZERO_MAKER_FEE;
- CHARGED_MAKER_FEE;
- INCENTIVIZED;
- UNKNOWN.

For every fee fact:

- official source;
- effective date;
- series scope;
- exact formula;
- exact rounding;
- actual fill price and size;
- verification status.

UNKNOWN is ineligible for profitability gates.

Hypothetical rebates, market-maker-program benefits or volume incentives are
not PnL unless contractually verified and actually realized.

============================================================
13. DATA CONTRACT
============================================================

DATA-1 — Metadata

All fields in Section 12.

DATA-2 — Trades

- trade ID;
- ticker/market;
- exchange timestamp;
- local receipt timestamp where available;
- price;
- size;
- aggressor/taker direction;
- source channel;
- deduplication state.

DATA-3 — L1/ticker

- best bid/ask;
- visible size where available;
- source timestamp;
- receipt timestamp;
- lifecycle;
- quality classification.

A ticker stream that conflates intermediate states is not lossless raw book
data.

DATA-4 — L2/orderbook deltas

Required for:

- true book-message arrivals;
- sequence analysis;
- queue diagnostics;
- depth dynamics;
- cancellation/depletion analysis.

Queue-valid data requires:

- snapshot;
- ordered deltas;
- per-stream sequence;
- gap detection;
- recovery boundaries;
- monotonic receipt timestamps.

Targeted L2 is sufficient for targeted queue research. Full L2 for every Kalshi
market is not required.

DATA-5 — Own execution

Only after future explicit live authorization:

- decision;
- order send;
- acknowledgment/rejection;
- private order updates;
- fill/partial fill;
- cancel request/ack/effective state;
- queue information where supplied;
- exact fees;
- reconciliation result.

DATA-6 — Sports-event state

The current primary program does not require synchronized point/game/set score.

Without synchronized sports state, do not claim that a price move was caused by
a serve, point, score or sports-information event.

Reliable scheduled start and pre-match/in-play classification remain required.

DATA-7 — External data

Before approval and credentials:

- fixtures only;
- mocks only;
- schema/adapter tests only.

Never create an account, spend money or call a paid endpoint without approval.

Betfair must not enter the plan until the operator’s legal account eligibility
is verified.

============================================================
14. DATA QUALITY
============================================================

Before calculating edge, inventory:

- paths and formats;
- schemas;
- row and unique-row counts;
- unique trade IDs;
- markets and root events;
- dates;
- categories/sports/series;
- market types;
- fee classes;
- pre-match/in-play coverage;
- L1/L2 coverage;
- timestamp/sequence coverage;
- duplicates;
- gaps/recovery;
- stale periods;
- host/regime;
- special-event concentration;
- prior human access to dates/results.

Hard rules:

- trades deduplicate deterministically;
- gaps are identified per connection/subscription/market;
- unexplained gaps cannot support queue research;
- timestamp units and clock domains are explicit;
- local monotonic clocks are used for latency where possible;
- crossed/locked/one-sided/empty/stale/recovered books are tagged;
- pre-match classification is versioned;
- unknown fees are excluded;
- postponement/cancellation/rescheduling are handled;
- every result reports rows and independent root events;
- lossless claims require sequence/unique-ID evidence;
- exclusions are machine-readable and reason-coded;
- corrupted raw evidence is never silently erased;
- a data gap cannot create favorable binding PnL.

============================================================
15. ARRIVAL, BURSTINESS AND IMMEDIATE REACTION
============================================================

Answer the quant-dev questions directly:

1. Are message and trade arrivals bursty?
2. Do adverse changes occur faster than the system can react?
3. Which maker actions actually exist and are safely wired?
4. Can a realistic fill simulator reproduce the order lifecycle?

Measure raw per-market intervals separately for:

- orderbook deltas;
- trades;
- ticker/L1;
- lifecycle.

Do not label trade or markout-row spacing as book-message arrivals.

Report:

- p0.1, p1, p5, p10, p25, p50, p75, p90, p95, p99;
- p99.9 where supported;
- mean;
- maximum;
- zero-interval rate;
- n;
- root-event count.

Rolling windows:

- 1 ms;
- 5 ms;
- 10 ms;
- 25 ms;
- 50 ms;
- 100 ms;
- 250 ms;
- 500 ms;
- 1 s;
- 3 s;
- 5 s.

Within each window:

- message count;
- trade count;
- price changes;
- spread changes;
- best-level depletion;
- signed volume;
- midpoint and executable-price movement.

Define burst states using TRAIN empirical distributions. Freeze definitions
before VALIDATION. Do not use one arbitrary universal threshold.

Initial time-to-start regimes:

- >6 hours;
- 6–3 hours;
- 3–1 hour;
- 60–30 minutes;
- 30–10 minutes;
- 10–2 minutes;
- <2 minutes;
- in-play diagnostic only.

Markout horizons:

- next valid update;
- 10 ms;
- 25 ms;
- 50 ms;
- 100 ms;
- 250 ms;
- 500 ms;
- 1 s;
- 3 s;
- 5 s;
- 10 s;
- 30 s;
- 60 s;
- 120 s.

Report signed midpoint and executable-exit markouts.

Thirty-second markout measures persistence. It does not replace immediate
reaction analysis.

============================================================
16. LATENCY AND CANCEL RISK
============================================================

Measure the complete path:

1. frame received;
2. bytes available;
3. decode complete;
4. book applied;
5. feature start/end;
6. decision start/end;
7. risk complete;
8. internal queue entry/exit;
9. signing start/end;
10. socket write;
11. first response byte;
12. order accepted/rejected;
13. private update received;
14. cancel decision;
15. cancel sent;
16. cancel acknowledged;
17. cancel effective.

Every value is:

- MEASURED(date, host, n);
- CONSERVATIVE_BOUND(source);
- PLACEHOLDER;
- OPERATOR-TBD.

PLACEHOLDER cannot support a gate.

Report p50/p90/p95/p99/p99.9/max, n, host, region and warm/cold state.

Cancel race is a joint competing-risk process, not a comparison of independent
percentiles.

For each active quote episode record:

- adverse trigger;
- cancel decision/send/ack;
- private state update;
- fill before request;
- fill after request;
- fill after acknowledgment;
- adverse executable-price move;
- cancel-effective boundary;
- reconciliation result.

Outcomes (descriptive episode summaries only — the binding definition
follows):

- safe cancel;
- fill before cancel;
- fill during cancel;
- fill after acknowledgment;
- adverse move without fill;
- ambiguous.

Model cancellation as a multistate, recurrent process, not one mutually
exclusive episode label. Freeze the time origin, exchange-event clock,
local-receive clock, risk set, censoring rule, cause-specific
cumulative-incidence estimands, cumulative filled size, remaining size and
terminal state. A fill reported after cancel acknowledgment is classified by
matching-engine time when available; otherwise it is ambiguous and resolved
conservatively.

Condition on burst, volatility, spread, load, time-to-start, market and host.

Cancel acknowledgment is not automatically cancel-effective. If the final
non-fill boundary is unproved, censor it or use a conservative upper bound.

============================================================
17. MAKER ACTION MATRIX
============================================================

Audit current official documentation, saved specifications and actual code for:

- GTC;
- IOC;
- FOK;
- expiration;
- post_only;
- pause/disconnect behavior;
- reduce_only;
- self-trade prevention;
- order groups;
- create/cancel;
- batch create/cancel;
- amend/decrease;
- queue position;
- user orders/fills;
- maintenance;
- rate limits;
- partial batch failure;
- RFQ-specific actions where applicable.

For each capability record:

- official field;
- official source/effective date;
- product scope;
- fee consequence;
- queue consequence;
- partial-fill behavior;
- rejection behavior;
- pause behavior;
- code location;
- implementation state;
- tests;
- venue-verification state.

Implementation states:

- DOCS_ONLY;
- CODE_PRESENT;
- WIRED_IN_MAIN_PATH;
- MOCK_VERIFIED;
- VENUE_VERIFIED;
- OPERATOR_APPROVED.

A helper class is not evidence that the main path enforces it.

============================================================
18. PATHWISE PNL ACCOUNTING
============================================================

This section replaces the old subtractive identity that could double-count
adverse selection, cancel-race and unwind losses.

All accounting uses exact repository fixed-point units.

For each root sports event e:

NetPnL_e
=
sum(executed cash inflows)
- sum(executed cash outflows)
- sum(exact venue fees)
- sum(distinct realized non-venue variable costs)
+ TerminalValue_e(remaining inventory under the frozen terminal policy).

Binding balance-sheet identity (the cash-flow form above must reconcile
exactly to it):

Define event wealth as `W_t = free_cash_t + segregated_collateral_t +
V_t(open_position_lots) - liabilities_t`, all in one audited fixed-point money
type distinct from `PriceE4`. Define `NetPnL_e = W_T - W_0 -
net_external_contributions - distinct_non_venue_variable_costs`. Transfers
between free cash and collateral are zero-PnL. Each trade, fee, unwind and
settlement cash flow enters exactly once. `V_T` applies only to still-open
lots and is zero for lots already liquidated or settled. The binding residual
value is the minimum over feasible joint settlement states of all linked
contracts under the frozen terminal policy.

Rules:

1. Declare starting cash, positions and collateral.
2. Historical research defaults to zero starting position.
3. Every fill, partial fill, cancel-race fill, unwind, fee and settlement cash
   flow enters the ledger exactly once.
4. Capital locked and collateral usage are not automatically PnL deductions.
   They enter return-on-capital and capacity analysis.
5. Fees are calculated per actual fill, not from an event-average price.
6. Remaining inventory is never marked at midpoint when the frozen policy
   requires executable or worst-case valuation.

Attribution views:

- quoted spread;
- matched spread capture;
- adverse selection;
- fill-during-cancel loss;
- partial-fill exposure;
- forced-unwind loss;
- residual-inventory PnL;
- settlement/outcome PnL;
- operational-failure PnL.

These are explanations, not additional deductions.

Every attribution references the underlying fill/cash flow/inventory lot.
Buckets are mutually exclusive and reconcile exactly to NetPnL_e.

Methodology fails if:

- any economic effect is counted twice;
- cash or position does not conserve;
- the fixed-point reconciliation gap is non-zero;
- an attribution is deducted after its cash effect already entered NetPnL;
- fee rounding differs from the effective venue rule.

============================================================
19. TERMINAL INVENTORY AND DIRECTION SEPARATION
============================================================

Before VALIDATION, freeze one deterministic terminal inventory algorithm.

The binding market-dynamics endpoint uses:

1. a fixed final quoting cutoff before scheduled event start;
2. cancellation under the full cancel lifecycle;
3. mandatory liquidation at contemporaneous executable depth;
4. exact taker fees and rounding;
5. a frozen rule for unfilled residual inventory.

For the binding conservative result, unliquidatable residual inventory receives
its worst settlement-state value unless the operator approves another
conservative rule before unblinding.

Holding to actual settlement is a separate secondary endpoint.

Report:

- matched round-trip PnL;
- forced-unwind PnL;
- residual worst-case value;
- actual settlement/outcome PnL;
- maximum event exposure;
- time-weighted inventory;
- capital-hours;
- percentage of PnL attributable to outcome exposure.

If profitability disappears under deterministic pre-start liquidation or
depends materially on final sports outcomes, label:

`DIRECTIONAL_OR_INVENTORY_DEPENDENT`

Do not call it proven direction-free spread capture.

============================================================
20. HOLDOUT, ESTIMAND AND STATISTICAL CONTRACT
============================================================

20.1 Early split sealing

At the beginning of STP-P02, before outcome-linked analysis, create a
hash-sealed split manifest.

The split manifest has four mutually exclusive assignments:
`EXPLORATORY_ONLY`, `TRAIN`, `VALIDATION`, and `HISTORICAL_CONFIRMATION`.
Before chronological assignment, join the prior-exposure ledger and force
every root event whose price, feature, fill, markout, outcome, selection or
PnL summary was previously exposed into `EXPLORATORY_ONLY`. Hash-seal both the
eligibility population and assignment. No exposed event may enter VALIDATION
or HISTORICAL_CONFIRMATION. If untouched history is inadequate, confirmation
accrues prospectively.

All linked markets and rows from one root event stay together.

Splits are chronological and embargoed. Embargo covers at least:

- maximum feature lookback;
- maximum quoting horizon;
- maximum markout;
- event-pack overlap;
- simulator carry state.

Every period previously inspected for PnL, fills, markouts, toxicity, strategy
selection or profitability is EXPLORATORY_ONLY.

Access:

- STP-P03–P05: TRAIN only;
- STP-P06: TRAIN, then one pre-registered VALIDATION opening;
- STP-P07: one HISTORICAL_CONFIRMATION opening after policy freeze;
- STP-P12: a new prospective post-freeze cohort.

If no untouched history remains, confirmation must accrue prospectively.

Accidental holdout access invalidates that split.

Blind DQ may inspect a sealed split only if it reveals no price, feature, fill,
markout, outcome or PnL summaries and applies a frozen mechanical rule.

20.2 Root-event estimand

All linked markets for one sporting event aggregate into one `NetPnL_e`.

Eligible zero-quote and zero-fill events remain in the primary sample with zero
strategy PnL, except actual attributable operational costs.

The primary estimand is:

`mu_event = E[NetPnL_e]`

under:

- one frozen policy;
- one-contract binding quote size;
- frozen eligibility;
- frozen time windows;
- frozen features;
- frozen risk/inventory caps;
- frozen fees;
- frozen strict-through simulator;
- frozen terminal policy;
- pre-specified target event distribution.

The primary statistic is mean event-level NetPnL, not raw total PnL and not
fill-conditional edge.

20.3 Dependence and confidence interval

Aggregate within root events before inference.

Preserve cross-event dependence using chronological calendar-day blocks
containing complete events.

Choose on TRAIN and freeze:

- timezone/calendar;
- multi-day event treatment;
- bootstrap family;
- block-length rule;
- bootstrap replications;
- deterministic seed;
- studentization;
- empty-block handling.

Use the lower endpoint of a two-sided 95% confidence interval:

`LCB_97.5(mu_event) > 0`

Messages, updates, quotes and fills are not independent samples.

If independent blocks are insufficient, conclude COLLECT_MORE.

Do not substitute row-level asymptotics.

20.4 Power

Before opening HISTORICAL_CONFIRMATION freeze:

- minimum economically relevant event-level effect;
- alpha;
- target power;
- conservative TRAIN variance;
- chronological design effect;
- zero-fill rate;
- expected DQ attrition;
- required independent block count;
- fixed end date or sequential design.

Estimate power using complete TRAIN blocks.

If the sealed sample lacks the frozen independent-block requirement, do not run
the confirmatory profit test.

20.5 Multiple testing

Before VALIDATION, hash a candidate registry containing every:

- sport;
- series;
- market type;
- fee class;
- time window;
- feature/transform;
- threshold/hyperparameter range;
- quote/cancel/cooldown rule;
- inventory/terminal rule;
- risk cap;
- selection score;
- stopping rule;
- simulator version.

Log every explored TRAIN candidate.

Tune only inside nested chronological TRAIN folds.

VALIDATION is opened once to select or reject one final candidate using a
pre-registered scalar rule. No later tuning.

HISTORICAL_CONFIRMATION tests one policy.

If K policies remain, pre-register Holm correction over K.

Subgroup and markout-horizon results are descriptive unless included in a
pre-registered corrected family.

20.6 Sequential testing

Before unblinding choose:

A. a fixed sample/end date with no interim PnL inspection; or

B. a pre-registered group-sequential alpha-spending design.

An inconclusive fixed holdout cannot be extended and retested at the same
alpha. Later data becomes a new replication cohort.

COLLECT_MORE is not permission to sample until significant.

20.7 Statistical versus commercial significance

A separate commercial model freezes:

- server/network costs;
- future data costs;
- monitoring/maintenance costs;
- fees;
- capital usage;
- required return on capital;
- minimum commercially relevant monthly PnL.

Report a confidence interval for monthly portfolio PnL at tested risk/capacity.

A positive event-level mean does not automatically pass the commercial gate.

============================================================
21. FILL-MODEL AUTHORITY
============================================================

Level A — Certified strict-through

- current binding historical gate;
- conservative scenario;
- required under D-1;
- not automatically a mathematical lower bound on PnL.

Level B — Queue-aware

- diagnostic;
- requires valid targeted L2;
- cannot promote a strategy.

Level C — Own-order calibrated

Eligible to replace Level A as primary execution model only after:

- operator-authorized probes;
- disjoint calibration and validation;
- complete queue/fill/cancel/fee observations;
- deterministic reproduction;
- independent audit;
- frozen code/parameters;
- explicit operator approval.

Changing simulator authority after viewing strategy profitability is prohibited.

============================================================
22. CERTIFIED STRICT-THROUGH FILL
============================================================

A favorable binding fill exists only if every condition passes:

1. Deterministic decision and client-order identity.
2. Quote activates only after conservative placement/acceptance latency.
3. The arriving order would not be rejected, expired, paused, rate-limited or
   rejected for post-only crossing.
4. Quote is active at the relevant trade.
5. Requote does not make the old quote disappear immediately.
6. Old quote remains exposed during cancel pending.
7. Replacement activates only after its own lifecycle.
8. Correctly side-adjusted public execution trades strictly through the active
   price.
9. A trade at our price never fills pessimistic strict-through.
10. YES/NO complement, aggressor direction and payout orientation are verified
    in fixed-point units.
11. Ordering is lossless over the quote episode.
12. There is no unexplained gap, recovery boundary or lifecycle ambiguity.
13. Same-timestamp ambiguity resolves against the strategy:

    - no favorable fill;
    - maximum still-possible old-quote exposure;
    - no early cancel benefit.

14. Binding historical size is one contract per quote.
15. Larger guaranteed fills require separate authoritative sweep-volume proof.
16. Replay input must match the intended deployed input contract.
17. A conflated feed can support only a strategy consuming that same feed with
    every hidden state treated conservatively.
18. Conflated data cannot support queue, raw-message or depth claims.

If a gap begins while a quote may be active, use one frozen rule:

- invalidate the complete event; or
- apply conservative worst-case exposure.

The choice is frozen before VALIDATION.

============================================================
23. FILL-SIMULATOR REQUIREMENTS
============================================================

The simulator models:

- decision;
- placement latency;
- acceptance/rejection;
- active/resting;
- partial/repeated fills;
- cancel decision/request/pending/ack/effective;
- amend/decrease/replace;
- queue-priority loss;
- expiration;
- pause;
- disconnect;
- stale feed;
- sequence gap;
- recovery;
- ambiguous write;
- reconcile-before-retry;
- terminal liquidation;
- settlement.

Required invariants:

- no fill before activation;
- at-price trade does not fill strict-through;
- strict-through fill only while active;
- cancel-pending may fill;
- cancel-effective cannot fill;
- old quote survives until cancel-effective;
- replacement respects its own latency;
- partial fills conserve size;
- cash/position/fee ledgers conserve;
- caps apply between messages;
- fee rounding is per fill;
- no future data affects a decision;
- identical replay is deterministic;
- ambiguity resolves against the strategy;
- post-only crossing produces rejection/no resting order;
- missing acknowledgment never implies success;
- ambiguous writes reconcile before retry.

Parameters are:

- MEASURED;
- CONSERVATIVE_BOUND;
- PLACEHOLDER;
- OPERATOR-TBD.

PLACEHOLDER cannot support binding profitability.

============================================================
24. CALIBRATION, VALIDATION AND CAPACITY
============================================================

Micro-live calibration samples opportunities by a frozen deterministic or
random rule with recorded inclusion probability. Do not cherry-pick safe-looking
orders.

Calibration and validation cohorts are disjoint.

Before calibration begins, freeze the target deployment opportunity
distribution, inclusion mechanism, inclusion probabilities,
positivity/support checks, weighting or conditional target, minimum
calibration and validation sample sizes, simulator-adequacy margins,
alpha/power and regime coverage. Passing simulator fit on a selectively
sampled cohort cannot validate deployment-wide fill probabilities.

Freeze validation metrics and tolerances for:

- fill calibration intercept/slope;
- Brier score or log loss;
- time-to-fill survival;
- partial-fill probability;
- fill-before/during-cancel probability;
- regime-conditional error;
- executable post-fill markout;
- prediction-interval coverage and width;
- stability across time blocks.

A wide interval is not a pass.

Simulator validation proves simulator adequacy, not strategy profitability.

STP-P12 requires a new cohort accrued after:

- strategy freeze;
- feature freeze;
- risk freeze;
- terminal-policy freeze;
- simulator freeze.

A prospective profitability claim requires:

- actual pathwise NetPnL;
- actual exact fees;
- actual inventory treatment;
- pre-registered confidence lower bound above zero;
- commercial hurdle;
- no unresolved safety/reconciliation failure.

One-contract evidence supports only one-contract capacity.

No linear size extrapolation.

Capacity analysis includes:

- fill decay with size;
- own queue addition;
- book impact;
- participation limit;
- unwind depth/impact;
- concurrent collateral;
- capital duration;
- event/factor exposure;
- order/cancel limits;
- partial/orphan risk;
- competition/capacity decay;
- uncertainty.

Larger size requires a new operator-approved progressive ladder and
revalidation.

============================================================
25. DYNAMIC POLICY AND FEATURE CONTRACT
============================================================

The initial research policy is:

- pre-match;
- deterministic;
- post-only intent;
- bounded inventory;
- no required sports prediction;
- no quote on stale/invalid/uncertain data;
- no automatic deployment;
- no automatic scaling.

State machine:

- UNMAPPED
- WARMUP
- ELIGIBLE
- QUOTING
- CANCEL_PENDING
- COOLDOWN
- STALE
- PAUSED
- DISABLED
- CLOSED

Every transition has a reason code.

Candidate feature families:

Economics:

- fee class;
- spread/tick;
- spread dwell;
- executable unwind;
- capital duration.

Flow:

- raw message/trade rate;
- signed volume;
- best-level depletion;
- imbalance;
- microprice;
- two-sided availability.

Toxicity:

- immediate executable markouts;
- burst-conditioned markouts;
- short-horizon volatility;
- fill clustering;
- time-to-start.

System:

- data age;
- sequence/recovery state;
- decision queue latency;
- placement/cancel latency;
- load.

Risk:

- market/event/factor inventory;
- liquidation depth;
- outcome exposure.

Every feature declares:

- source fields;
- exchange/local availability;
- as-of rule;
- TTL;
- warm-up;
- cadence;
- missing action;
- computation latency;
- training window;
- minimum support;
- shrinkage;
- cold start;
- reason code;
- version.

Only data received before decision time may be used.

Historical toxicity cannot use future observations from the current root event.

Adding or changing a feature, threshold, state transition, quote rule, cancel
rule, cooldown, inventory rule or kill behavior creates a new candidate.

No uncontrolled online learning or automatic live retraining.

Dynamic protection may pull or pause during:

- toxic message/trade burst;
- spread collapse;
- volatility jump;
- depth depletion;
- stale data;
- sequence uncertainty;
- system queue growth;
- cancel-latency deterioration;
- lifecycle uncertainty.

A required-capture filter may combine non-overlapping conservative estimates of:

- exact fee;
- conditional adverse selection;
- cancel-race exposure;
- unwind cost;
- inventory risk;
- uncertainty.

This is a decision threshold, not a PnL accounting identity.

============================================================
26. COMPLETE CODEBASE REUSE CONTRACT
============================================================

The repository is an existing trading system, not a blank-slate project.

No new component may be created until existing components are inspected and
classified.

`tools.json` is the intended registry, not presumed complete. STP-P00 must
perform a bidirectional census of: every registry entry; every Makefile/CMake
target; every `main()` under `apps/` and `tests/`; every executable/script
entry point; every code path containing authenticated mutation or order
submission; and every registry argument mode. Record `REGISTRY_COUNT_OBSERVED`
dynamically; 144 is generation-time evidence only. Any action mode that can
mutate or transmit is `live_order` unless split into a separately registered
safe entry. The repository currently has two general engine paths plus
standalone live-capable utility/emergency paths; P00 must enumerate all of
them. “No third live order path” means no new strategy transmission
architecture. It does not erase existing utilities or the independently
operable S3 panic path, all of which must reuse audited
submission/reconciliation primitives or remain `DO_NOT_USE`.

26.1 Allowed classifications

Every relevant artifact receives exactly one primary classification:

- REUSE_AS_IS
- EXTEND
- DIAGNOSTIC_ONLY
- PRESERVE
- REPLACE_WITH_MIGRATION
- DO_NOT_USE
- OPERATOR_TBD

Definitions:

REUSE_AS_IS:

- semantics fit;
- source inspected;
- tests exist and pass;
- no known mismatch with the new contract.

EXTEND:

- preferred base;
- requires a future authorized W and tests;
- no parallel replacement without justification.

DIAGNOSTIC_ONLY:

- useful for exploration;
- cannot produce binding gate evidence.

PRESERVE:

- production, historical or evidence asset;
- do not modify or delete.

REPLACE_WITH_MIGRATION:

- replacement is necessary;
- parity, migration, rollback and deprecation plan required.

DO_NOT_USE:

- prohibited in the stated phase/path.

OPERATOR_TBD:

- evidence or authority is insufficient.

26.2 Required matrix columns

The P00 matrix covers every registered tool and every phase-critical library:

- artifact ID/path;
- registry name;
- safety class;
- current purpose;
- current caller/main-path status;
- data dependency;
- clock domain;
- money representation;
- tests;
- production coupling;
- known limitations;
- intended STP consumer;
- classification;
- evidence level;
- proposed action;
- migration/parity requirement;
- operator decision required;
- file/line/command evidence.

Evidence levels:

- L1: registry/docs inspected;
- L2: source and tests inspected;
- L3: relevant tests executed;
- L4: venue/production behavior verified under authorized evidence.

REUSE_AS_IS requires at least L2 and passing relevant tests. Binding execution
reuse eventually requires L3/L4 as appropriate.

26.3 Starting reuse map to verify

Foundation:

- `include/trading/fixedpoint.hpp`
- `include/trading/bus.hpp`
- `include/trading/ids.hpp`
- `include/trading/timestamp.hpp`
- `include/trading/storage.hpp`

Starting classification: REUSE_AS_IS or EXTEND after verification.

Requirements:

- exact fixed-point accounting;
- normalized event schema;
- deterministic IDs/trace;
- replay compatibility.

Kalshi feed/gateway:

- `include/kalshi/orderbook.hpp`
- `include/kalshi/sid_stream.hpp`
- `include/kalshi/recovery.hpp`
- `include/kalshi/ws_client.hpp`
- `include/kalshi/ws_recorder.hpp`
- `src/ws_client.cpp`
- `src/gateway.cpp`

Starting classification: EXTEND/REUSE_AS_IS by component.

Warehouse and gold data:

- `tools/warehouse.py::load`
- `tools/warehouse_common.py`
- `tools/gold_load.py`
- `tools/gold_fsm.py`
- `tools/gold_merge.py`
- `tools/gold_io.py`
- `tools/gold_validate.py`
- corresponding tests.

Starting classification: REUSE_AS_IS/EXTEND.

`tools/warehouse.py::load()` remains the single default research entry point
unless an audited replacement is approved.

Production data path:

- `tools/ingest.py`
- `tools/export_day.py`
- `tools/pipeline_supervisor.sh`
- deployment units/configs.

Starting classification: PRESERVE.

This program consumes their outputs read-only. Missing fields require a separate
production W.

Event packaging and DQ:

- `tools/event_measure_split.py`
- `tools/event_index.py`
- `tools/event_pack.py`
- `tools/event_validate.py`
- `tools/event_export.py`
- `tools/capture_gaps.py`
- `tools/verify_ws_capture.py`
- relevant tests.

Starting classification: REUSE_AS_IS/EXTEND.

Research segmentation:

- `tools/research/build_segments.py`

Starting classification: EXTEND.

Known issue to verify: current logic is Tennis-oriented and must not silently
define the all-Sports universe.

Maker-edge research:

- `tools/research/maker_edge_pilot.py`
- `tools/research/aggregate_maker_edge.py`
- `tools/research/html_report_maker_edge.py`
- vendored `docs/vendor/js/echarts.min.js`.

Classify at component level:

- orchestration/checkpoint/reporting: EXTEND or REUSE_AS_IS;
- old fill logic/statistical conclusions: HISTORICAL or DIAGNOSTIC_ONLY;
- old event bootstrap: must be reconciled with the new chronological-block
  contract before binding reuse.

Existing S1 results remain historical evidence. They do not prove a general
Sports strategy.

Legacy maker backtest:

- `tools/mm_backtest.py`
- `config/backtest_latency.yaml`.

Starting classification: DIAGNOSTIC_ONLY.

Known limitations to verify:

- old quotes may disappear immediately on requote;
- fixed/full fill assumptions;
- missing partial-fill/cancel lifecycle;
- latency placeholders;
- current decision-clock limitations.

Do not silently change its historical semantics. Build the governed simulator
by extending reusable replay components or replace it only with a migration
plan.

Additional verified `mm_backtest.py` limitations: floating-point
cash/inventory; maker fee hard-coded to zero; no settlement; residual
inventory marked at last midpoint; and immediate stale-quote removal.
Additional verified maker-edge limitations: exchange-time ASOF analysis;
public-trade touch-cross classification without an own-order lifecycle; no
inventory, cash, terminal or zero-fill-event ledger; and heuristic
ticker-based event IDs. These components and results are
`HISTORICAL / DIAGNOSTIC_ONLY`; orchestration/reporting may be reused only
after component-level separation.

Candidate scanning/calibration:

- `tools/mm_scan.py`
- `tools/mm_research.py`
- `tools/mm_calibrate.py`
- `tools/gate_calc.py`
- `tools/coverage_audit.py`.

Starting classification: DIAGNOSTIC_ONLY or EXTEND by component.

They may provide baselines/features. They do not automatically satisfy the new
fill/statistical contract.

Pricing modules:

- `tools/pricing/lo.py`
- `tools/pricing/fair.py`
- `tools/pricing/quote.py`
- their tests.

Starting classification: OPERATOR_TBD/EXTEND.

Reuse only where their semantics match the selected market-dynamics policy and
current Q1/Q9 authority.

Latency tools:

- `apps/bench_ws_decode.cpp`
- `apps/bench_orderbook.cpp`
- `apps/bench_rtt.cpp`
- `include/kalshi/full_chain_latency_probe.hpp`
- `config/backtest_latency.yaml`
- supporting-spec S2/S3/S5 procedures.

Starting classification:

- offline/local/read-only measurement: REUSE_AS_IS/EXTEND;
- placeholder values: DIAGNOSTIC_ONLY;
- authenticated venue measurement: future release required.

Live-order measurement tools:

- `apps/bench_order.cpp`
- `apps/fill_test.cpp`.

Current classification: DO_NOT_USE.

They may be considered only in a future release with `live_order=true`.

Execution paths:

1. `apps/tradingd.cpp` lane path;
2. `KalshiExecutionEngine` in `src/gateway.cpp`.

Starting classification: EXTEND, but not currently eligible for strategy live
deployment.

Required future architecture:

`NormalizedEvent`
→ one versioned `SportsDecisionCore`
→ `OrderIntent`
→ one gated/reconciled `OrderSubmitter`

Replay, backtest, shadow and live use the same decision core and normalized
events. Only adapters differ.

Future STP-P08 must converge existing execution paths. Do not create a third
live transmission path.

Known issues to verify:

- `tradingd` currently uses REST polling as feed input;
- order lane bypasses full `RequestExecutor` accounting;
- main submission may not force `post_only`;
- bus Live mode is fail-closed/not transmitting;
- risk/reconciliation controls are not fully unified.

Execution and risk components:

- `src/request_executor.cpp`
- `include/kalshi/request_executor.hpp`
- `include/kalshi/risk_ledger.hpp`
- `include/kalshi/rule_engine.hpp`
- `tools/reconcile.py`
- `tools/account_view.py`
- `apps/panic.cpp`
- `apps/preflight.cpp`
- associated tests.

Starting classification: REUSE_AS_IS/EXTEND after verification.

Future live transmission must use:

- reserve-before-send;
- deterministic client IDs;
- duplicate protection;
- reconciliation;
- exact five-layer risk accounting;
- private state;
- kill and recovery.

Shadow:

- `apps/ws_shadow.cpp`
- replay/storage/bus/orderbook components;
- shadow tests.

Starting classification: EXTEND/REUSE_AS_IS.

Shadow must consume the same decision core and cannot claim real fills.

Monitoring:

- `tools/freshness.py`
- `tools/daily_check.py`
- `tools/warehouse_status.py`
- `tools/feed_readiness.py`
- `tools/verify_feed_metrics.py`
- `tools/verify_ws_capture.py`
- `tools/lifecycle_check.py`
- alert/telemetry components.

Starting classification: REUSE_AS_IS/EXTEND.

External mutation/live-capable tools:

- `account_upgrade`;
- `panic_live`;
- `tradingd` in live mode;
- `preflight --order`;
- any `live_order` registry entry.

Current classification: DO_NOT_USE.

26.4 Mandatory reuse rules

- No duplicate warehouse.
- No duplicate normalized event bus.
- No third live order path.
- No separate strategy logic for backtest versus shadow versus live.
- No millions-of-rows pandas load when DuckDB/SQL/columnar processing suffices.
- No new chart bundle while the vendored ECharts asset works.
- No new runnable without:
  - `tools.json`;
  - safety class;
  - Makefile;
  - CMakeLists.txt where applicable;
  - tests;
  - documentation.
- Python stays off the hot order path under current E7.
- Production must never import from `sandbox/`.
- Promotion from sandbox requires a governed W.
- Replacement requires parity tests, migration, rollback and explicit
  deprecation.
- Historical tools are preserved even when their conclusions are superseded.

26.5 Phase-to-code reuse map

STP-P00:

- ARCHITECTURE;
- tools registry;
- Makefile/CMake;
- tests;
- complete reuse matrix.

STP-P02:

- warehouse/gold/event packaging/DQ;
- production path remains PRESERVE.

STP-P03:

- raw recorder/envelope;
- sid sequence/recovery;
- capture gaps;
- raw jitter/arrival tools;
- research reporting scaffolding.

STP-P04:

- action/API specs;
- decode/book/full-chain latency tools;
- S2/S3/S5 procedures.

STP-P05:

- replay/bus/orderbook/storage;
- old backtest diagnostic only;
- governed lifecycle simulator and exact ledger.

STP-P06:

- generalized segments;
- coverage/scanning/research baselines;
- candidate registry and train-only selection.

STP-P07:

- maker-edge orchestration/reporting;
- governed simulator output;
- new statistical engine.

STP-P08:

- tradingd and gateway convergence;
- RequestExecutor;
- RiskLedger;
- RuleEngine;
- reconcile/account/private state/panic.

STP-P09:

- ws_shadow;
- same decision core;
- replay/live-data parity.

STP-P10:

- preflight/account view/reconcile/panic/private state;
- future operator-approved probe harness.

STP-P11:

- own-order calibration and disjoint simulator validation.

STP-P12:

- frozen same decision core;
- actual prospective ledger;
- explicit live release.

STP-P13:

- freshness/daily/warehouse/feed/lifecycle monitoring;
- drift and profitability monitoring;
- production pipeline still separately protected.

============================================================
27. REPORTING AND REPRODUCIBILITY
============================================================

Every result reports:

- definition;
- units;
- rows;
- root events;
- independent blocks;
- date range;
- universe;
- fee class;
- fill model;
- split;
- exclusions;
- uncertainty;
- code commit;
- data manifest/hash;
- config hash;
- simulator version;
- strategy version.

Required:

- deterministic seeds;
- deterministic ordering;
- stable aggregation;
- fixed-point money;
- exact commands;
- dependency versions;
- source commit;
- data manifest;
- machine-readable result;
- human-readable report.

Critical research runs twice. Outputs must match except explicitly allowed
metadata.

Never use Python’s salted `hash()` for reproducible seeds.

Use DuckDB/SQL/Parquet/Polars or equivalent columnar processing.

Interactive reports should reuse the vendored ECharts asset and work offline
without a CDN.

============================================================
28. TEST CONTRACT
============================================================

Current E1 remains binding.

Before a W is described as implemented:

- run `make check`;
- run `tests/run_pipeline.sh`;
- run relevant targeted tests;
- run `python3 tools/check_registry.py` where applicable.

Future behavior changes require tests in the same W.

Relevant future tests include:

- deterministic replay;
- schemas/DQ;
- exact fee rounding;
- PnL cash/position conservation;
- attribution reconciliation;
- strict-through invariants;
- activation/cancel lifecycle;
- partial fills;
- recovery;
- duplicate/out-of-order data;
- risk reservation;
- reconciliation;
- kill switch;
- failure injection;
- feature as-of;
- split leakage;
- candidate registry freeze.

If localhost binding is unavailable:

- report every affected test as NOT RUN;
- record the exact environment error;
- do not call the suite green;
- rerun in a normal terminal;
- do not mark the W implemented/audited until required tests pass.

============================================================
29. LONG-HORIZON PHASE PROGRAM
============================================================

All phases after STP-P00 are UNAUTHORIZED.

Each future phase must be decomposed into seven-field Ws and mapped to
MM_ROADMAP and MASTER_SEQUENCE before release.

STP-P00 — Repository, authority and reuse audit

Purpose:

- establish what exists;
- prevent duplicated systems;
- identify governance/statistical conflicts;
- create the durable starting state.

Maximum conclusion:

- STP_P00_AUDIT_PASSED_AWAITING_OPERATOR_RELEASE.

STP-P01 — Canonicalization

Future tasks:

- create `PLAN_SPORTS_TRADING_MASTER.md`;
- promote STATE;
- append, never recreate, DECISIONS;
- pin supporting specs by path/commit/section/hash;
- create full GUARDRAILS treatment table;
- minimally update stale CLAUDE authority pointers;
- map STP phases to engineering Ws.

No research execution.

STP-P02 — Raw data truth and split sealing

Provisional Ws:

- prior-exposure ledger and immutable split manifest;
- all-Sports data inventory;
- deduplication/timestamps;
- gaps/recovery;
- root-event linkage/lifecycle/fees;
- data-ready audit.

No production changes.

STP-P03 — Arrival, burst and reaction atlas

Provisional Ws:

- raw channel inter-arrivals;
- rolling burst regimes;
- immediate executable markouts;
- time-to-start atlas;
- all-category controls and all-Sports comparison.

TRAIN only.

STP-P04 — Maker actions and latency

Provisional Ws:

- official action matrix;
- actual code-wiring audit;
- local decode/book/decision measurements;
- approved read-only RTT/EC2 measurement;
- cancel competing-risk design.

No order probes.

STP-P05 — Governed fill simulator

Provisional Ws:

- lifecycle/invariant specification;
- strict-through engine;
- exact cash/position/fee ledger;
- terminal inventory;
- diagnostic queue model;
- deterministic replay;
- independent audit.

Strict-through remains binding.

STP-P06 — Cross-Sports candidate selection

Provisional Ws:

- candidate registry;
- power/commercial model;
- TRAIN exploration;
- nested chronological selection;
- one VALIDATION opening;
- freeze one policy.

No confirmation PnL claim.

STP-P07 — Historical confirmation

Provisional Ws:

- unblinding gate;
- one frozen strict-through run;
- event/day-block inference;
- robustness and one-contract capacity;
- historical/commercial report.

Maximum conclusion:

- PAPER_TRADE_ELIGIBLE or SHADOW_ELIGIBLE.

It cannot prove prospective/live profitability.

STP-P08 — Execution safety integration

Provisional Ws:

- one shared decision core;
- sequenced WS main feed;
- single OrderSubmitter;
- reserve/risk/rules;
- private state/reconciliation;
- kill/recovery/failure testing.

No orders.

Engineering order remains subordinate to MASTER_SEQUENCE.

STP-P09 — Prospective shadow

Provisional Ws:

- replay/shadow parity;
- intended-order/risk/state evidence;
- current provisional 5-of-7/clean-day conditions;
- prediction capture;
- no-order proof.

Shadow fills are still simulated.

STP-P10 — Micro-live calibration

Requires a release with `live_order_permission=true`.

Provisional Ws:

- full current S1–S6 preflight;
- frozen sampling design;
- one-contract post-only probes;
- hard count/exposure/loss budgets;
- complete private state/fees;
- zero-resting proof.

Purpose is calibration, not profit.

STP-P11 — Calibrated simulator audit

Provisional Ws:

- calibration fit;
- disjoint later validation;
- regime error;
- independent audit;
- operator authority decision.

A simulator pass supports only SIMULATOR_CALIBRATED.

STP-P12 — Prospective profitability cohort

Requires a new live-order release and adequate power.

Provisional Ws:

- frozen cohort/design;
- bounded prospective order collection;
- actual pathwise NetPnL;
- statistical gate;
- commercial/capacity gate;
- safety/reconciliation gate.

Only this phase may support:

- PROSPECTIVE_PROFITABILITY_SUPPORTED;
- LIMITED_DEPLOYMENT_ELIGIBLE.

STP-P13 — Limited deployment and controlled scale

Requires a deployment release.

Provisional Ws:

- versioned deployment envelope;
- allowlisted universe;
- hard risk/time/capital limits;
- live expected-versus-realized monitoring;
- fee/regime/drift monitoring;
- automatic de-risk/kill;
- rollback;
- progressive capacity ladder;
- periodic revalidation.

No automatic scaling.

============================================================
30. ACTIVE SUPPORTING SPECIFICATIONS
============================================================

STP-P00 must inspect and prepare section-level pinning for:

1. `docs/PLAN_MM_TEST_PROGRAM.md`

Preserve valid measurement procedures for:

- arrivals;
- burstiness;
- reaction;
- latency;
- maker actions;
- fill simulator;
- shadow/calibration.

Do not grant its conflicting strategy ordering automatic authority.

2. `docs/PLAN_RESEARCH_CYCLE_1.md`

Preserve as ACTIVE SUPPORTING SPEC candidates:

- S2;
- S3;
- S5;
- associated signing measurements;
- RTT;
- EC2 rerun;
- reproducibility;
- required outputs.

Completed S1 work remains HISTORICAL EVIDENCE.

The S1/S4 burstiness source correction must be verified from current code/docs.

Every retained section records:

- path;
- source commit;
- section ID/heading;
- content hash;
- allowed role;
- prerequisites;
- explicit overrides.

Mutable filenames alone do not carry authority.

============================================================
31. STP-P00-W01 — EXACT AUTHORIZED WORK
============================================================

Status:

- phase authorized in principle;
- writes blocked until operator approves branch/base;
- after branch release, this W is the only implementation W allowed.

Objective:

Produce an evidence-backed repository, authority, data-capability and code-reuse
audit. Do not implement strategy or change system behavior.

31.1 Mandatory reads

Read completely where relevant:

- `CLAUDE.md`;
- `docs/GUARDRAILS.md`;
- `docs/MM_ROADMAP.md`;
- `docs/MASTER_SEQUENCE.md`;
- `docs/PLAN_SPORTS_TRADING_DECISIONS.md`;
- `docs/PLAN_MM_TEST_PROGRAM.md`;
- `docs/PLAN_RESEARCH_CYCLE_1.md`;
- `docs/PLAN_FULL_MARKET_RESEARCH.md`;
- `docs/PLAN_LIVE_VALIDATION.md`;
- `docs/PLAN_PRICING_MODEL.md`;
- `docs/PLAN_RISK_KILLSWITCH.md`;
- `docs/ARCHITECTURE.md`;
- relevant architecture/protocol/data-contract docs;
- relevant SESSION_LOG entries;
- `tools.json`;
- Makefile;
- CMakeLists.txt;
- phase-critical source and tests;
- prior research outputs needed to classify evidence.

For all `REGISTRY_COUNT_OBSERVED` registered tools (144 at v2 generation —
generation-time evidence only) and all filesystem-discovered runnable or
live-capable entry points:

- inspect registry metadata;
- identify source/test/build coverage;
- classify;
- escalate phase-critical/high-risk tools to source-level inspection.

31.2 Exact allowed writes after branch approval

- `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2.md`;
- `docs/PLAN_SPORTS_TRADING_STATE.md`;
- append-only registration of verbatim operator rulings/releases in
  `docs/PLAN_SPORTS_TRADING_DECISIONS.md` (Section 4; agent audit findings
  and status never enter DECISIONS);
- `docs/plan_audits/sports_trading_program/STP_P00_READ_MANIFEST.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_REPOSITORY_AUDIT.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_AUTHORITY_MAP.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_CODE_REUSE_MATRIX.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_CODE_REUSE_MATRIX.json`;
- `docs/plan_audits/sports_trading_program/STP_P00_DATA_CAPABILITY_MATRIX.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_PHASE_REUSE_MAP.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_CONFLICT_RISK_REGISTER.md`;
- `docs/plan_audits/sports_trading_program/STP_P00_HANDOFF.md`;
- `docs/SESSION_LOG.md`.

**Derived test and mirror writes.** In addition to the enumerated evidence
documents, W01 and AUD01 may write only `build/**`, `work/logs/**`,
`work/test_results.ndjson`, test-owned OS temporary paths, and
`/Users/ritcardo/Desktop/TradingSys Report/docs-mirror/**`, solely as side
effects of the required build, tests and final docs mirror. These derived
paths must never be staged or committed. Run `make all`, then `make check`,
then `tests/run_pipeline.sh`, then the registry check. Snapshot the derived
paths before and after; any other unexpected write stops the session.

The pre-audit checkpoint may commit eligible pre-existing files unchanged. It
does not authorize editing them.

31.3 Forbidden writes

- `docs/GUARDRAILS.md`;
- `docs/MASTER_SEQUENCE.md`;
- `docs/MM_ROADMAP.md`;
- all source code;
- all tests;
- all config;
- all schemas;
- capture/ingest/export;
- deployment units;
- production data;
- `outputs/`;
- `.claude/`;
- credentials;
- EC2 or Mac service state.

31.4 Required outputs

READ MANIFEST:

- path;
- bytes/lines;
- source commit;
- sections inspected;
- evidence level;
- completed status.

REPOSITORY AUDIT:

- verified current fact;
- contradiction;
- historical artifact;
- prototype;
- disconnected component;
- unsupported claim;
- known defect;
- production risk;
- later-phase hypothesis.

AUTHORITY MAP:

- document/section;
- scope;
- current authority;
- conflicts;
- resolution or OPERATOR-TBD.

CODE REUSE MATRIX:

- all registered tools;
- phase-critical libraries;
- classification;
- evidence;
- future phase;
- no duplicate subsystem.

DATA CAPABILITY MATRIX:

- what exists;
- coverage;
- what can be studied;
- what cannot be claimed;
- missing score/L2/queue/private-order information;
- whether missing data blocks the primary hypothesis or only later models.

PHASE REUSE MAP:

- each STP phase;
- existing components;
- extensions;
- prohibited components;
- required tests;
- engineering W dependencies.

CONFLICT/RISK REGISTER:

- governance conflicts;
- statistical conflicts;
- PnL/fill risks;
- holdout contamination;
- production coupling;
- missing authority;
- exact operator decisions needed.

HANDOFF:

- prompt hash;
- branch/base;
- checkpoint;
- evidence commit;
- tests;
- blockers;
- exact next action;
- explicit statement that P01 is unauthorized.

31.5 Acceptance

STP-P00-W01 reaches `IMPLEMENTED_AWAITING_AUDIT` only if:

- BOOTSTRAP-0 completed;
- approved branch/base used;
- prompt archived byte-for-byte;
- checkpoint committed or NOT_NEEDED recorded;
- D-1 preserved verbatim;
- GUARDRAILS hash unchanged;
- all `REGISTRY_COUNT_OBSERVED` entries and all filesystem-discovered
  runnable/live-capable entry points covered; count drift and
  registry/build/safety mismatches surfaced;
- every phase-critical component has source/test evidence;
- no code/config/schema/production changes;
- no external or authenticated action;
- no unsupported profitability claim;
- `make check` passes;
- `tests/run_pipeline.sh` passes;
- registry check passes;
- evidence commit exists;
- closure commit exists;
- final Git state is clean except explicitly excluded pre-existing artifacts;
- state says `IMPLEMENTED_AWAITING_AUDIT`.

31.6 Rollback

- use explicit `git revert` of program commits;
- never reset or rewrite history;
- do not automatically revert the pre-audit checkpoint;
- preserve reviewed audit artifacts as historical evidence;
- production requires no rollback because P00 must not touch it.

============================================================
32. STP-P00-AUD01 — INDEPENDENT AUDIT
============================================================

This is a fresh session performed by an agent that did not implement W01.

Allowed only after both W01 evidence commit A and closure commit B exist,
STATE says `IMPLEMENTED_AWAITING_AUDIT`, the final W01 Git state is recorded,
and the governing release names `STP-P00-AUD01` (or a separate AUD01 release
exists) — Section 7.

Audit writes are limited to:

- `docs/plan_audits/sports_trading_program/STP_P00_INDEPENDENT_AUDIT.md`;
- `docs/PLAN_SPORTS_TRADING_STATE.md`;
- `docs/SESSION_LOG.md`.

Audit findings and PASS/REVISE/REJECT status live in the audit artifact,
STATE and SESSION_LOG only; they never enter
`docs/PLAN_SPORTS_TRADING_DECISIONS.md` (Section 4).

The auditor must:

- re-run BOOTSTRAP-0;
- verify branch/base/release;
- inspect every W01 commit/diff;
- sample and validate the 144-tool matrix;
- fully inspect phase-critical classifications;
- confirm known architectural defects were neither hidden nor overstated;
- verify statistical corrections are represented;
- verify D-1/provisional gates;
- confirm no P01 work;
- re-run required tests;
- verify production and GUARDRAILS untouched;
- issue PASS/REVISE/REJECT.

PASS state:

`STP_P00_AUDIT_PASSED_AWAITING_OPERATOR_RELEASE`

REVISE/REJECT stays inside STP-P00.

No P01 work may begin in the audit session.

============================================================
33. COMMIT, STATE, LOG AND MIRROR PROTOCOL
============================================================

Avoid self-referential hashes.

For an implementation or correction session:

1. Run tests.
2. Inspect full diff and staged diff.
3. Commit implementation/evidence as commit A.
4. Record commit A.
5. Update STATE and SESSION_LOG referencing commit A.
6. Commit closure as commit B.
7. Do not try to embed commit B’s own hash inside commit B.
8. Use `last_completed_evidence_commit`.
9. Run the CLAUDE.md docs mirror after closure.
10. If mirror fails, record WARN according to CLAUDE.md.
11. Never call uncommitted work complete.
12. Never claim a commit without a returned Git hash.

The prompt archive and optional checkpoint remain separate commits.

No push is authorized.

============================================================
34. PER-SESSION START PROTOCOL
============================================================

Every future session:

1. Run BOOTSTRAP-0.
2. Verify branch/worktree/single-writer ownership.
3. Read active prompt and riders.
4. Read DECISIONS.
5. Read STATE.
6. Read MASTER if it exists.
7. Read current W’s pinned supporting specs.
8. Verify evidence/audit hashes.
9. Verify exact release.
10. State the one W being executed.
11. State production remains out of scope.
12. Stop if authorization is ambiguous.

============================================================
35. PER-SESSION EXIT PROTOCOL
============================================================

Before stopping:

1. Set truthful status.
2. Produce handoff.
3. Run required tests.
4. Record every test not run.
5. Inspect diff/staged diff.
6. Confirm GUARDRAILS unchanged.
7. Confirm production untouched.
8. Use evidence + closure commits.
9. Run docs mirror.
10. Report hashes.
11. Report final Git status.
12. State exact next W.
13. State whether independent audit or operator release is required.
14. Stop.

Do not begin another W because time remains.

============================================================
36. IMMEDIATE EXECUTION INSTRUCTION
============================================================

When this prompt is supplied to an execution agent:

1. Perform BOOTSTRAP-0 only.
2. Do not write, stage, commit or create a branch unless a durable operator
   release supplies:

   - approved program branch;
   - approved base commit;
   - approved worktree;
   - STP-P00-W01 scope.

3. If branch authorization is still OPERATOR-TBD, report the branch-approval
   request and stop.
4. Once branch authorization is recorded, execute only STP-P00-W01.
5. Stop at `IMPLEMENTED_AWAITING_AUDIT`.
6. A fresh independent session may then execute STP-P00-AUD01, subject to the
   AUD01 start conditions in Sections 7 and 32 (both W01 commits exist; the
   release names `STP-P00-AUD01` or a separate AUD01 release exists).
7. Do not execute STP-P01.

Required final line after W01 implementation:

“STP-P00-W01 implemented; independent audit pending. STP-P01 unauthorized.”

Required final line after a passing independent audit:

“STP-P00 independently audited and passed; STP-P01 not started and requires a
new operator release.”

END CANONICAL PROMPT V2.1 CANDIDATE
