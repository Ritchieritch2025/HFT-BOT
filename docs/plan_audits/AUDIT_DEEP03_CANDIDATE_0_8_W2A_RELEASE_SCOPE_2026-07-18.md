# DEEP03 candidate-0.8 — D3-W2A release-scope independent audit

Date: `2026-07-18`  
Verdict: `PASS_WITH_EXPLICIT_BLOCKERS`  
Permission conferred: `RELEASE_DRAFTING_ONLY`  
Research execution conferred: `NO`

## Exact target

- Repository object: commit `9346c05d`, path
  `docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`
- SHA-256:
  `ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`
- Size: `233135` bytes
- Lines: `4117`
- Version declared by the target: `candidate-0.8`

The archived audit with target SHA beginning `9a1b70f2` does not cover these
bytes and grants no authority to this revision.

## Decision

The target is sufficiently specified to draft separate exact releases for
`D3-W0`, an exploratory subset of `D3-W1`, and a narrowly named partial
`D3-W2A` run.  It is not approved as an umbrella deep03 release, a complete
W2A implementation, a confirmation design, or a cash-strategy test.

The only admissible first research payload is branded:

```text
D3-W2A-B01B04-PARTIAL-OPEN-DISCOVERY
MODE 1 / EXPLORATORY_AUTORESEARCH
EXPLORATORY_ONLY / PRIOR_EXPOSED / NOT_STRICT_ACCEPTANCE
```

It may execute only the currently implemented partial descriptive methods:

- `D3-B01-MARKOUT`: partial receive-clock descriptive markout;
- `D3-B02-ONESIDE`: partial one-sided duration/censor description;
- `D3-B03-XMKT`: linked-event estimability preflight only, with the current
  terminal result fixed to `NOT_ESTIMABLE`;
- `D3-B04-RHYTHM`: partial active-minute description.

It must not claim complete price-by-liquidity B01 coverage, complete lifecycle
censor treatment for B02, a calculated cross-market residual for B03, complete
event-minute/phase segmentation for B04, `D3-B05-FEEWALL`,
`D3-B06-REPRO`, complete W1, complete W2A, NetPnL, a candidate, promotion,
confirmation, profitability, shadow readiness, live readiness, or a trading
verdict.

## Explicit blockers that must close before execution

1. **Separate release chain.** Persist distinct exact `D3-W0`, `D3-W1`, and
   partial `D3-W2A` release identities.  One umbrella authority is invalid.
2. **W0 adoption.** Bind these exact plan bytes, this audit and its SHA, the
   exact runtime commit, operator text, fixed W09 identity/role, allowed write
   roots, cost cap, runtime/expiry and one session.
3. **W1 exploratory receipts.** Bind every input by release ID, S3 Key,
   VersionId, size and SHA-256; persist input, DQ, prior-exposure and split
   receipts.  All admitted dates must be marked prior-exposed/open discovery.
   No VALIDATION, CONFIRMATION or holdout is opened.
4. **Evidence containment.** `SEALED_DEGRADED_EVIDENCE` is admissible only in
   the exact Mode 1 exploratory lane.  The strict canary remains red by design.
5. **Telegram conflict.** Section `4.15.1` currently requires feed delivery and
   reconciliation before reader access, while W-HYPO-FEED-01 is explicitly
   conflicted/unimplemented and the W09 release correctly has no Telegram
   credential.  Before this run, the operator must explicitly name and defer
   that pre-reader feed gate for this partial W2A release only.  The run must
   instead register its four methods locally and may not send/read Telegram.
   This deferral does not carry into any later W.
6. **Authority cannot be bypassed.** Direct prepare/runner invocation must
   validate the same exact authority as the service.  The run bundle,
   `INPUT_MANIFEST.json` and `RUN_COMPLETE.json` must bind the plan, audit,
   W0/W1/W2A releases, authority, arm and runtime commit SHAs.
7. **RFQ remains absent.** Old damaged RFQ remains
   `DATA_INTEGRITY_BLOCKED`; fresh RFQ is outside this release.  No RFQ object,
   repair, replay or result is allowed.
8. **No external mutation.** S3 is read-only and exact-version only.  AWS
   writes, production mutation, credentials, paid APIs, external-account
   actions, Telegram, orders, shadow, micro-live and live actions are all
   explicitly false.

## Required termination semantics

- One bounded session only; no automatic extension based on findings.
- Every method closes as partial descriptive `EXECUTED` or
  `NOT_ESTIMABLE` with a receipt.
- `research_verdict` remains `null`.
- Completion consumes the execution arm; a later date or method requires a
  new exact release.
- Any authority, input, code, plan, evidence-tier or release-ID mismatch fails
  closed before research computation.

## Audit conclusion

`PASS_WITH_EXPLICIT_BLOCKERS` permits the release artifacts and hardening work
above to be drafted and independently tested.  Execution remains `NO-GO`
until every numbered blocker is represented in machine-checked artifacts and
the operator approves the final exact release text.

This audit was read-only.  It did not run research, open a holdout, mutate
production, access a trading credential, write S3, send Telegram messages or
send orders.
