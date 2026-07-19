# DEEP03 D3-W2A `.05` exact-date L1 chunk repair — independent release audit

Date: `2026-07-19`

Verdict: `PASS_WITH_EXPLICIT_BLOCKERS`

Permission conferred: `REPAIR_RELEASE_DRAFTING_ONLY`

Research execution conferred: `NO`

## Exact scope inherited from the approved `.03` and `.04` attempts

- Adopted plan:
  `docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`
- Adopted plan SHA-256:
  `ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`
- Original independent plan audit:
  `docs/plan_audits/AUDIT_DEEP03_CANDIDATE_0_8_W2A_RELEASE_SCOPE_2026-07-18.md`
- Original independent plan-audit SHA-256:
  `7b9c448096488a34324ab543e4bbeeed36ee5d62f5613c7f85efed1f95115ad2`
- Mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`.
- Window: `2026-07-10` through `2026-07-17`, using the same eight exact
  V3 release IDs already bound by D3-W1 `.03` and `.04`.
- Inputs: `2,657` objects and `29,473,216,651` bytes at
  `SEALED_DEGRADED_EVIDENCE`; all inputs remain prior-exposed and no holdout is
  opened.
- RFQ: `OFF_AND_ABSENT`.
- Method scope remains the same partial open-discovery B01/B02/B03/B04 scope.
  This repair does not expand the plan, evidence tier, dates, input objects,
  method definitions, acceptance claims, external actions or write
  permissions.

The original plan audit remains the governing scope audit.  This document is
an additional, narrowly scoped audit of the second post-failure memory repair.
It does not replace or loosen any explicit blocker in the original audit.

## Terminal failure chain

### `.03`: first DuckDB OOM, no report, ARM consumed

D3-W2A release `D3-W2A-2026-07-18.03` started its one permitted W09
execution only after all eight exact-release canaries and local input
verification passed.  It then failed while DuckDB was materializing the L1
interval relation:

- run ID:
  `mode1-20260710-20260717-3cde714ed188-41d54379c38f-a1`;
- job ID: `RJOB-20260719T012222459450Z-ded84065dec1`;
- terminal exception: `OutOfMemoryException`, trying to pin a `256 KiB` block
  with `29.8 GiB / 29.8 GiB` already used under the `32GB` DuckDB limit;
- observed systemd service memory peak: `35.3G`;
- failure time: `2026-07-19T01:28:28Z`;
- pulled terminal Research Inbox `STATUS.json` SHA-256:
  `66eeed4d2bf470788a49ec3d8836991208e0ae7cb16172d74cd7f1d201b234cf`;
- `RUN_COMPLETE.json` was not written, no result bundle or report was
  produced, and no retry was started;
- the `.03` one-shot ARM was consumed and is not authority for any later
  attempt.

### `.04`: narrow projection still OOM, no report, ARM consumed

D3-W2A release `D3-W2A-2026-07-18.04` narrowed `l1_intervals` to its eleven
downstream-consumed columns and ran with `16GB / 2 threads`.  Its exact-release
fetch, verification and eight canaries passed, but it failed with DuckDB OOM
before any later-stage completion receipt was produced.  `.04` did not emit a
stage receipt that directly identifies the failing SQL statement.  Review of
the query structure, memory evidence and exact runtime makes the global
eight-date `l1_intervals` window the highest-confidence candidate, but this is
an inference rather than direct proof of root cause:

- exact `.04` runtime commit:
  `40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`;
- run ID:
  `mode1-20260710-20260717-3cde714ed188-dde75a994495-a1`;
- job ID: `RJOB-20260719T014726297058Z-ded84065dec1`;
- terminal exception: `OutOfMemoryException`, trying to allocate a `256 KiB`
  block with `14.9 GiB / 14.9 GiB` already used under the `16GB` DuckDB limit;
- observed systemd service memory peak: `38.5G`;
- failure time: `2026-07-19T02:01:14Z`;
- `.04` terminal Research Inbox `STATUS.json` SHA-256:
  `157023cf94f24f1751d74515c17dca94baeee7f9335df6897e00420e6f2be740`;
- `.04` `RUN_FAILED.json` SHA-256:
  `e9f9516f1697fb5825805b94853f515736271c0398acd1f0d79f388a25884e4f`;
- `.04` consumed one-shot claim receipt SHA-256:
  `c5ee3812e1f0972b3b3b93218ce3661221325b6b5b898a87f579e4bf5fc4df91`;
- `.04` AUTHORITY SHA-256:
  `dde75a994495696544075b37ca2eb070457f6fe663beab9f36cf0eb403e44971`;
- `.04` ARM SHA-256:
  `99b39184503565bec7e335089371a430c02fcb655c58959299cc7934934a47da`;
- `RUN_COMPLETE.json` was not written, no result bundle or report was
  produced, and no retry was started.

Both old ARMs are terminally spent.  Neither may be reused, reinstalled,
edited or treated as authority for `.05`.

## Audited `.05` repair

The proposed exact repair runtime is commit
`eb56b45c04d00fa0ac64267f22b2b3d2a3d7d1d9`, based on the failed `.04`
runtime commit `40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`.

The `.04` implementation contains one blocking L1 window sort across all
eight dates.  The `.05` narrow repair targets that highest-confidence memory
candidate without claiming the prior failure point was directly proven.  It
obtains its date set exclusively from the
exact `INPUT_MANIFEST.release_dates`, validates canonical ISO dates, and
cross-checks those dates against the manifest release summaries and L1 object
dates.  It creates the same fixed eleven-column `l1_intervals` schema, then
executes one bounded `INSERT` for each exact date in chronological order.

This is a relationally equivalent decomposition for the authorized L1
interval estimand because the unchanged window partitions on
`(date, market_ticker)`.  No window partition can cross a date boundary.  The
following analytical definitions remain byte-for-byte unchanged inside each
date-bound insert:

- the causal ordering keys used by `lead(t_us)`;
- the `60,000,000 us` book TTL;
- close-time and next-state bounds;
- capture-gap clipping and `starts_in_gap` handling;
- right-censor behavior and zero-duration rule;
- phase classification; and
- the eleven downstream materialized columns:
  `date`, `t_us`, `market_ticker`, `event_proxy`, `sport`, `book_state`,
  `right_censored`, `starts_in_gap`, `interval_end_us`, `duration_us`, and
  `phase`.

B01/B03 still do not consume `l1_intervals`; B02/B04 continue to consume the
same relation under the same statistical definitions.  DuckDB remains fixed
at `16GB / 2 threads`.  Trade ingestion, trade-ID quality control,
deduplication and every trade-side method are unchanged.  New flushed stage
markers only identify completion of each L1 date, `trade_id_qc`, and
`trades_dedup`; they do not alter data or results.

The repair reduces the maximum L1 blocking-sort scope from eight manifest
dates to one exact manifest date.  It is a bounded-memory structural change,
not a claim that the full research run is now proven to complete.

## Inherited sort-key collision technical debt

The complete L1 order key remains:

```text
(date, market_ticker, t_us,
 coalesce(recv_wall_ns,0), coalesce(recv_mono_ns,0),
 coalesce(ws_sid,0), coalesce(ws_seq,0))
```

If two rows collide on every component of this existing key, their internal
relative order is not additionally resolved.  That ambiguity already existed
in `.03` and `.04`; exact-date chunking neither adds nor removes it because
the partition and order expressions are unchanged.  It is therefore recorded
as inherited engineering/statistical technical debt, not a new `.05` release
blocker.  Any future tie-policy change would alter semantics and requires a
separate audited release rather than a hot patch.

## Verification performed

At exact repair runtime commit
`eb56b45c04d00fa0ac64267f22b2b3d2a3d7d1d9`:

- the five focused suites collected and passed `95` tests:
  `test_deep03_one_shot_arm.py`,
  `test_deep03_v3_open_discovery.py`,
  `test_deep03_v3_w1_preflight.py`,
  `test_w09_exploratory_autoresearch.py`, and `test_w09_bringup.py`;
- a bidirectional `EXCEPT ALL` regression proves the date-chunked and legacy
  one-shot interval queries return the same multiset on synthetic two-date
  data covering causal ties, capture gaps, gap starts, right-censoring and
  phase assignment;
- tests prove the date list is manifest-bound and sorted, reject duplicated,
  noncanonical or code-shaped dates, assert the exact eleven-column schema,
  preserve the original window expressions, and assert `16GB / 2 threads`;
- `deploy/w09/exploratory_autoresearch_payload.sha256` passed full
  `sha256sum -c` verification;
- `deploy/w09/deep03_open_discovery_modules.sha256` passed full
  `sha256sum -c` verification; and
- the exact runtime and metadata diffs passed `git diff --check`.

No eight-day or production-scale research run was performed as part of this
audit.  The `.03` and `.04` failure receipts were inspected read-only; no old
canary, DQ or completion receipt was rerun.  These checks establish exact
code binding and semantic scope, not completion or report production.

## Explicit blockers before one `.05` repair attempt

1. Persist exact `.05` W0 and W1 release candidates bound to this audit's
   exact bytes/SHA, runtime commit
   `eb56b45c04d00fa0ac64267f22b2b3d2a3d7d1d9`, the same eight exact releases,
   the same evidence tier/object totals, and RFQ `OFF`.
2. Bind W1 `.05` to the exact W0 `.05` SHA.  A mechanically rebound
   `W1_COMPLETE.json` may retain the already completed preflight facts only
   if it preserves the original completion time, canary rows, embedded DQ,
   input projection and artifact hashes and explicitly makes no claim that
   preflight or research was rerun.
3. Obtain a new explicit operator repair authorization naming exact release
   `D3-W2A-2026-07-18.05`.  This audit grants no execution authority.
4. Only after that authorization, generate a new exact AUTHORITY and new
   one-shot ARM bound to `.05`, this audit, exact W0/W1/W1-completion hashes,
   the same eight releases and the exact `.05` runtime commit.
5. Before execution, independently verify the installed runtime and every
   release/authority/ARM/prerequisite SHA.  No hot patch or drift is
   admissible.
6. Permit exactly one `.05` repair attempt within the separately authorized
   cumulative runtime and spending caps.  Any mismatch must fail closed
   before computation.  Any terminal failure consumes the new ARM and
   requires another audited release and fresh operator decision; there is no
   automatic retry.
7. A report may be claimed only after a valid `RUN_COMPLETE.json` and sealed
   result bundle exist.  Until then, the accurate outcome remains “no report.”

## Audit conclusion

`PASS_WITH_EXPLICIT_BLOCKERS` means the narrowly scoped `.05` release
metadata may be drafted and checked.  Exact-date L1 materialization is an
admissible semantics-preserving memory repair for the already authorized
partial open-discovery methods, but it has not been validated at eight-day
scale.  D3-W2A `.05` remains `NO-GO` until a new operator authorization and
new exact one-shot authority artifacts satisfy every machine-checked gate.

This audit did not connect to W09, deploy files, generate AUTHORITY/ARM, start
research, open a holdout, write S3, mutate production, send Telegram messages,
access trading credentials or send orders.
