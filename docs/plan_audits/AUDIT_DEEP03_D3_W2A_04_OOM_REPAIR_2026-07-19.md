# DEEP03 D3-W2A `.04` OOM repair — independent release audit

Date: `2026-07-19`

Verdict: `PASS_WITH_EXPLICIT_BLOCKERS`

Permission conferred: `REPAIR_RELEASE_DRAFTING_ONLY`

Research execution conferred: `NO`

## Exact scope inherited from the approved `.03` attempt

- Adopted plan:
  `docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`
- Adopted plan SHA-256:
  `ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`
- Original independent plan audit:
  `docs/plan_audits/AUDIT_DEEP03_CANDIDATE_0_8_W2A_RELEASE_SCOPE_2026-07-18.md`
- Original independent plan-audit SHA-256:
  `7b9c448096488a34324ab543e4bbeeed36ee5d62f5613c7f85efed1f95115ad2`
- Mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`
- Window: `2026-07-10` through `2026-07-17`, using the same eight exact
  V3 release IDs already bound by D3-W1 `.03`.
- Inputs: `2,657` objects and `29,473,216,651` bytes at
  `SEALED_DEGRADED_EVIDENCE`; all inputs remain prior-exposed and no holdout is
  opened.
- RFQ: `OFF_AND_ABSENT`.
- Method scope remains the same partial open-discovery B01/B02/B03/B04 scope.
  This repair does not expand the plan, evidence tier, dates, inputs, method
  definitions, acceptance claims, external actions or write permissions.

The original plan audit remains the governing scope audit.  This document is
an additional, narrowly scoped audit of the post-failure memory repair and does
not replace or loosen any of the original audit's explicit blockers.

## `.03` terminal failure facts

D3-W2A release `D3-W2A-2026-07-18.03` did start its one permitted W09
execution after all eight exact-release canaries and local input verification
passed.  The research runner then failed while DuckDB was materializing the L1
interval relation:

- run ID:
  `mode1-20260710-20260717-3cde714ed188-41d54379c38f-a1`;
- job ID: `RJOB-20260719T012222459450Z-ded84065dec1`;
- terminal exception: `OutOfMemoryException` while trying to pin a `256 KiB`
  block with `29.8 GiB / 29.8 GiB` already used under the configured `32GB`
  DuckDB limit;
- observed systemd service memory peak: `35.3G`;
- failure time: `2026-07-19T01:28:28Z`;
- `RUN_COMPLETE.json` was not written and no results/report was produced;
- the `.03` one-shot ARM was consumed and no retry was started.

Local immutable references used for this release audit are:

- `.03` AUTHORITY SHA-256:
  `41d54379c38fd9526599c63159de40ab671266f7e785989e0111692b6b0ed614`;
- `.03` ARM file SHA-256:
  `9ae69d5598dd236607ece9d57fd1803d8be6276b58928518b131c3d52f22c1e9`;
- pulled terminal Research Inbox `STATUS.json` SHA-256:
  `66eeed4d2bf470788a49ec3d8836991208e0ae7cb16172d74cd7f1d201b234cf`.

The old ARM is terminally spent.  It must not be reused, reinstalled, edited,
or treated as authority for `.04`.

## Audited repair

The proposed exact repair runtime is commit
`40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`, based on the failed `.03`
runtime commit `3f2071493967f05cd466230c3e8eeb83ac241dab`.

The independent code review found the failure concentrated in the blocking
window sort used to construct `l1_intervals`.  The failed code carried the
entire enriched L1 row through that sort even though downstream B02/B04 logic
consumes only these eleven materialized columns:

```text
date
t_us
market_ticker
event_proxy
sport
book_state
right_censored
starts_in_gap
interval_end_us
duration_us
phase
```

The repair preserves the original partition and causal ordering keys while
computing `lead(t_us)`, then projects only the eleven downstream-consumed
columns into `l1_intervals`.  Prices, quantities, labels and tie-break columns
that are not consumed after `next_t_us` is derived are no longer carried into
the materialized table.  Review found this projection semantically equivalent
for the currently authorized partial B02/B04 computations: the interval,
censor, gap, phase and duration expressions are unchanged, and B01/B03 do not
consume `l1_intervals`.

The W09 launcher is additionally reduced from `32GB / 8 threads` to
`16GB / 2 threads`.  This is a bounded-memory operating change; it does not
alter the analytical method scope.

## Verification performed

At exact repair runtime commit
`40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`:

- the four focused suites collected and passed `69` tests:
  `test_deep03_v3_open_discovery.py`,
  `test_w09_exploratory_autoresearch.py`,
  `test_deep03_one_shot_arm.py`, and
  `test_deep03_v3_w1_preflight.py`;
- the tests assert the exact eleven-column `l1_intervals` contract and the
  exact `16GB / 2 threads` runner arguments;
- `deploy/w09/exploratory_autoresearch_payload.sha256` passed full
  `sha256sum -c` verification;
- `deploy/w09/deep03_open_discovery_modules.sha256` passed full
  `sha256sum -c` verification;
- the repair diff passed `git diff --check`.

No eight-day or production-scale research run was performed as part of this
audit.  Therefore the checks establish code-level binding and semantic scope,
not proof that the repaired runtime will complete at full scale or produce a
report.

## Explicit blockers before one repair attempt

1. Persist exact `.04` W0 and W1 release candidates bound to this audit's
   exact bytes/SHA, runtime commit
   `40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`, the same eight input releases,
   the same evidence tier/object totals, and RFQ `OFF`.
2. Bind W1 `.04` to the exact W0 `.04` SHA.  A mechanically rebound
   `W1_COMPLETE.json` may retain the already completed `.03` preflight facts
   only if it preserves the original completion time, canary rows, DQ content
   and artifact hashes and makes no claim that preflight or research was rerun.
3. Obtain a new, explicit operator repair authorization naming exact release
   `D3-W2A-2026-07-18.04`.  This audit by itself grants no execution authority.
4. Only after that authorization, generate a new exact AUTHORITY and a new
   one-shot ARM bound to `.04`, this audit, the exact W0/W1/W1-completion
   hashes, the same eight releases and the exact repair runtime commit.
5. Before execution, independently verify the installed runtime and every
   release/authority/ARM/prerequisite SHA and keep the original `24h` runtime
   and `$15` spending caps.  No hot patch or drift is admissible.
6. Permit exactly one `.04` repair attempt.  Any authority, input, evidence,
   code, release, memory-policy or receipt mismatch must fail closed before
   computation.  A second OOM or any other terminal failure consumes the new
   ARM and requires a separately audited release and fresh operator decision;
   there is no automatic retry.
7. A report may be claimed only after a valid `RUN_COMPLETE.json` and sealed
   result bundle exist.  Until then, the accurate outcome remains “no report.”

## Audit conclusion

`PASS_WITH_EXPLICIT_BLOCKERS` means the narrow `.04` repair artifacts may be
drafted and checked.  The eleven-column projection plus `16GB / 2 threads` is
an admissible semantics-preserving memory repair for the already authorized
partial open-discovery methods, but it has not been validated at eight-day
scale.  D3-W2A `.04` remains `NO-GO` until all blockers above are represented
in exact machine-checked artifacts and the operator separately authorizes its
single execution.

This audit did not connect to W09, deploy files, generate AUTHORITY/ARM,
start research, open a holdout, write S3, mutate production, send Telegram
messages, access trading credentials, or send orders.
