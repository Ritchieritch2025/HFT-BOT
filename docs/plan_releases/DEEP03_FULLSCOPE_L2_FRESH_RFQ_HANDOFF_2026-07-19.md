# DEEP03 full-scope L2 + fresh RFQ handoff

Timestamp: `2026-07-19T15:47Z`

Status: **WIP / independent audits FAIL / no research run active**

This document supersedes the RFQ-OFF execution direction in
`DEEP03_D3_W2A_CONTINUATION_HANDOFF_2026-07-19.md`. It records the operator's
new scope decision: the final research must use the complete legitimate
L1/trades/market-graph/L2 data and the complete eligible **fresh** RFQ data.
It grants no new AWS mutation, IAM, deployment, spending, AUTHORITY, ARM, or
research-execution permission.

## Copy/paste instruction for the next agent

> Read `docs/plan_releases/DEEP03_FULLSCOPE_L2_FRESH_RFQ_HANDOFF_2026-07-19.md`
> completely. Verify every cited worktree, branch and SHA before editing.
> Continue from the existing tested commits; do not rediscover ingestion,
> zero-copy publication or prior OOM work. The required final scope is full
> legitimate L1, trades, market graph, L2 and eligible fresh RFQ, with no
> sampling. Old damaged RFQ remains `DATA_INTEGRITY_BLOCKED` and must never be
> repaired or analyzed. First close the listed L2 and RFQ P0 audit findings,
> obtain independent PASS receipts, then integrate the separate RFQ overlay.
> Do not deploy or start W09 research from any currently listed HEAD.

## Operator objective and exact meaning of “full”

The final result must analyze every row in every exact, authorized object in
scope. “Full” does not authorize fabricated coverage or use of known-damaged
evidence:

- historical base cohort: eight exact V3 releases, `2026-07-10..2026-07-17`;
- channels: L1, trades, market graph and every physically captured L2 row;
- L2 estimands: only independently quality-valid dates `2026-07-12`,
  `2026-07-15`, `2026-07-17`;
- L2 coverage ledger only: captured but quality-excluded `2026-07-13`,
  `2026-07-14`, `2026-07-16`;
- L2 explicitly absent: `2026-07-10`, `2026-07-11`;
- RFQ: every row from the clean fresh generation after its strict T0;
- old 284-object damaged RFQ cohort: permanently
  `DATA_INTEGRITY_BLOCKED / NO_REPAIR / NO_ANALYSIS`;
- RFQ D01--D07 may run only when their exact evidence gates pass;
- D08 direct quote PnL remains `BLOCKED_PRIVATE_DATA` unless separately
  audited private quote/fill/fee/cashflow evidence is supplied.

The historical V3 cohort ends on July 17, while the new RFQ generation starts
at `2026-07-20T00:00:00Z`. Therefore this is intentionally two cohorts, not a
fake same-day join. D07 RFQ-to-CLOB impact needs a same-date exact July 20+
base release with L1/L2 and an exact L2 quality receipt before it may emit
results.

## Current infrastructure truth

Read-only verification at `2026-07-19T15:46Z`:

- W09 `18.226.151.192` is reachable and remains `r8g.2xlarge`.
- W09 has no Deep03, DuckDB or autoresearch process running.
- `w09-exploratory-autoresearch.service` is `inactive`.
- W09 root disk is `290G total / 98G used / 193G available`.
- Production `3.130.232.109` remains separate; do not run research there.
- Production fresh capture is active under
  `kalshi-rfq-capture.service`, PID `1987742`, child `ws_shadow` PID
  `1987753`, started `2026-07-19T14:58:27Z`.
- Its generation is `fresh-rfq-20260720-01`, strict T0
  `2026-07-20T00:00:00Z`.
- At the read-only check: connected/valid, gaps=0, reconnects=0,
  disconnects=0, errors=0, recorder dropped=0, write failures=0.
- Production root disk was `581G total / 379G used / 203G available`.
- The first offset-aware pre-T0 diagnostic segment closes at `16:00Z` on
  July 19. Inspect its receipt after that time. It proves parser/capture
  mechanics only; it can never become eligible research data.
- Earliest possible complete fresh RFQ day is July 20 after the frozen `24h
  + 2h` evidence delay, approximately `2026-07-21T02:00Z`.

Do not resize or stop production. Do not launch compute on production. W09
may stop via its normal idle policy; that does not imply production stopped.

## Worktree A — integrated full-scope base/L2 runner

- Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`
- Branch: `w-deep03-fullscope-09`
- Runtime code commit immediately before this handoff document:
  `e3302c8d9c947c748e8608d9c39d3ae896378c40`
- Runtime commit label: `WIP fail closed L2 tail and fact row authority`
- Worktree state before adding this handoff document: clean.
- Verification at handoff:
  - `python3 -m pytest -q tests/test_deep03*.py` PASS;
  - Python compile PASS;
  - `git diff --check` PASS;
  - both W09 SHA manifests verify PASS.

This commit fixed several real errors: snapshot sequence regressions now fail
closed, reconnects cannot be expired as an observed one-second no-refill
window, terminal market rows no longer extend to a date-wide clock, censored
reset/invalid dwell is zeroed, all fact objects require exact row counts, and
trades are compared with declared source counts.

It is deliberately WIP and **must not be deployed**. Independent L2 audit is
still FAIL for these remaining issues:

1. Freeze state staleness TTL. Primary descriptive TTL is 1 second; the plan's
   finite registry requires `{250ms, 1s, 5s}` sensitivity. No interval may
   contribute unlimited hours of uptime merely because the next update is
   hours later. Emit `RIGHT_CENSORED_STALE_TTL` where appropriate.
2. Distinguish snapshots:
   - same sid + strictly increasing seq + valid prior epoch is a continuous
     next relevant update; observe to the snapshot clock, censor there, and
     never label the snapshot as refill;
   - new sid/reconnect, malformed, duplicate or regressed snapshot censors at
     the last proven valid market clock.
3. Add adversarial tests for TTL, legal same-sid snapshot, delayed new-sid
   reconnect, duplicate/regressed snapshots, and exact one-second boundary.
4. Pause/close/terminal lifecycle is not yet a proven L2 boundary. Either
   implement exact lifecycle boundaries or label output narrowly as
   `TTL-capped update-to-update dwell`, never full market uptime.
5. Make standalone `build_market_graph()` reject missing/wrong fact
   `row_count`, not only the integrated runner.
6. The independent audit gate must bind the changed
   `deep03_v3_methods.py` SHA and add blocker classes for TTL, snapshot
   regression, reset-before-expiry and manifest row authority.
7. The final report must show actual hazard, balance, match coverage and
   concentration tables/figures, not only counts of L2 strata.

After those changes: update both W09 SHA manifests, make one clean tested
commit, and ask a separate agent to audit the immutable exact HEAD and the
real L2 quality objects. No PASS receipt exists yet.

## Worktree B — fresh RFQ research/D07 repair

- Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-rfq-repair`
- Branch: `w-deep03-fullscope-rfq-repair-09`
- HEAD: `6d2a2c16498162d51759315f16c54ee4fd04d94a`
- Repair range: `33f779e..6d2a2c1`
- Worktree state at handoff: clean.
- Author verification: full Deep03 + fresh RFQ `567/567` PASS; fresh RFQ
  focused `485/485` PASS.
- Independent audit verdict: **FAIL**, despite the tests.

Remaining RFQ P0 findings:

1. `build_l2_quality_gate()` accepts a minimal caller-created JSON and treats
   omitted sequence/loss fields as zero. Require the canonical quality schema,
   state, complete typed required counters, canonical key, exact VersionId,
   bytes and SHA, and prove the object belongs to the same exact base release
   through the exact reader attestation.
2. D07 price/spread/depth values are still caller-trust. Bind an audited D07
   producer/algorithm SHA, exact L1/L2 reader attestation, partition/checkpoint
   receipts and independently recomputed pre/post values. Caller booleans and
   a self-consistent hash are not proof.

Remaining P1 findings:

- cover both checkpoint and exact-temp filesystems in disk preflight;
- bind and cap DuckDB spill, then justify the checkpoint expansion bound;
- bind the five-second RFQ clock tolerance to explicit method authority;
- keep all already-correct full-ID dedup, lifecycle/delete conservation,
  integer-microsecond KM, hard memory caps and no-sampling behavior.

Do not cherry-pick this range into Worktree A until these findings are fixed
and an independent immutable-HEAD re-audit returns PASS.

## Worktree C — daily fresh RFQ eligibility/publication pipeline

- Worktree: `/private/tmp/hft-fresh-rfq-daily-auto`
- Branch: `codex/fresh-rfq-daily-auto`
- HEAD: `81d9e3bd40a2f32678e704d481e8f5ee11511561`
- Relevant commits: `93126df`, `19113f5`, `89da066`, `81d9e3b`.
- Worktree state at handoff: clean.
- Focused daily runner/receipt tests after the final commit: PASS.
- AWS access/writes by this repair work: zero.

Implemented locally: cheap preflight, per-date alerts, streaming ledger,
per-object exact overlay/cache/checkpoints, durable date queue, bad-date
quarantine/retry, honest `WAITING_IAM`, and an isolated runtime installer.

Not complete and not deployed:

- fresh RFQ exact tag/read IAM delta is still `DRAFT_NOT_APPLIED`;
- the publisher credential adapter is not installed;
- without IAM it must stop at `WAITING_IAM`;
- with IAM but no adapter it must stop at
  `WAITING_PUBLISHER_CREDENTIALS`;
- independent audit has not passed;
- no production service/timer deployment has occurred.

Audit this exact HEAD before any IAM or deployment proposal. Never deploy the
timer merely because local tests pass.

## Exact historical base input already on W09

- Manifest:
  `/srv/w09-research/automation/w1/D3-W1-2026-07-18.03/INPUT_MANIFEST.json`
- Dates: `2026-07-10..2026-07-17`
- Exact V3 objects: `2,657`
- Exact bytes: `29,473,216,651`
- RFQ policy in this base manifest: forbidden and absent.

That is correct: the base runner must remain uncontaminated. Fresh RFQ is a
separate exact overlay with separate cohort, source binding and claims. Do
not edit the historical manifest to insert July 20 RFQ.

## Required continuation order

1. After `16:00Z`, read-only verify the first offset-aware RFQ segment and
   update only its deployment/canary receipt. Do not mark it eligible.
2. Fix Worktree A's L2 TTL/snapshot/audit-binding P0s and get independent
   PASS on a clean exact commit.
3. Fix Worktree B's exact quality and D07 producer-attestation P0s and get a
   separate independent PASS.
4. Independently audit Worktree C. Present the minimal IAM delta and publisher
   credential adapter as explicit remaining deployment gates; do not pretend
   they are applied.
5. Integrate the audited RFQ overlay into the audited full-scope runtime while
   preserving separate base and RFQ source bindings and row-conservation
   ledgers. Run all regression, adversarial and package-SHA tests.
6. Install only the final clean audited runtime on W09. Installation must not
   auto-start research.
7. Obtain one new exact operator AUTHORITY/ARM for the final runtime, dates,
   resource/time/cost envelope and two-cohort RFQ scope. Never reuse `.03`--
   `.06` ARM files.
8. Run historical base/L2 checkpointed computation on W09. The full legitimate
   physical L2 cohort is processed, but only clean dates enter estimands.
9. Once a complete eligible fresh RFQ date and same-date V3 base release exist,
   run D01--D07 full-row overlay and merge the evidence-indexed report.
10. Final audit must reconcile object counts, bytes, declared/actual rows,
    every exclusion, checkpoint hashes and report tables/figures before any
    strategy is described as monetizable.

## Hard prohibitions

- Do not repair, line-filter, infer from, or analyze the old damaged RFQ.
- Do not deploy `e3302c8`, `6d2a2c1`, or `81d9e3b` as production-ready.
- Do not run research on production or interrupt its base collection.
- Do not silently apply the draft IAM delta.
- Do not claim “full RFQ research complete” before a full eligible post-T0 day.
- Do not claim L2 PnL, executable fills, RFQ gross margin or D08 PnL from
  public market data.
- Do not turn quality-excluded L2 dates into estimand rows.
- Do not start an old narrow `.08` or reuse a consumed one-shot ARM.
