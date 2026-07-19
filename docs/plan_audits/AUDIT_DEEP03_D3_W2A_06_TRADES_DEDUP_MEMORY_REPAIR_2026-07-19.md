# DEEP03 D3-W2A `.06` trade-dedup memory repair — independent release audit

Date: `2026-07-19`

Verdict: `PASS_WITH_EXPLICIT_BLOCKERS`

Permission conferred: `REPAIR_RELEASE_DRAFTING_ONLY`

Research execution conferred: `NO`

## Exact inherited research scope

- Adopted plan:
  `docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`
- Adopted plan SHA-256:
  `ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`
- Governing independent scope audit:
  `docs/plan_audits/AUDIT_DEEP03_CANDIDATE_0_8_W2A_RELEASE_SCOPE_2026-07-18.md`
- Governing scope-audit SHA-256:
  `7b9c448096488a34324ab543e4bbeeed36ee5d62f5613c7f85efed1f95115ad2`
- Mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`.
- Window: `2026-07-10` through `2026-07-17` inclusive.
- Inputs: the same `2,657` exact-version V3 objects totaling
  `29,473,216,651` bytes at `SEALED_DEGRADED_EVIDENCE`.
- Holdout: none is opened; all selected inputs remain prior-exposed.
- RFQ: `OFF_AND_ABSENT`.
- DuckDB execution envelope: `16GB / 2 threads`.
- Method scope: the same partial open-discovery B01/B02/B03/B04 scope.
  This release does not add a method, change an estimand, claim candidate
  profitability, promote a strategy, open a holdout, or authorize an order.

The eight exact input releases remain:

1. `2026-07-10__v3ref__seal-86c01ed0__pub-45f5b99ff4fdf580`
2. `2026-07-11__v3ref__seal-de2e77c8__pub-e038f638b307da69`
3. `2026-07-12__v3ref__seal-bc37de4c__pub-abd451660aeedf0c`
4. `2026-07-13__v3ref__seal-7f6e5c1b__pub-55f66e41dba1484d`
5. `2026-07-14__v3ref__seal-2d6a4941__pub-748d8d77f665d60a`
6. `2026-07-15__v3ref__seal-22aa1b04__pub-4ae53f539c340052`
7. `2026-07-16__v3ref__seal-60b2674e__pub-fd365b5cd76099a0`
8. `2026-07-17__v3ref__seal-e25887ab__pub-74f48fcbc81a41b7`

The governing scope audit remains controlling.  This document audits only a
narrow physical-memory repair after three terminal attempts.  It grants no
execution authority and does not weaken any evidence, statistical, safety,
budget, or one-shot gate.

## Terminal failure chain

### `.03`: global L1 materialization OOM, no report, ARM consumed

Release `D3-W2A-2026-07-18.03` passed the eight exact-release canaries and
local input verification, then terminated during L1 interval materialization:

- run ID:
  `mode1-20260710-20260717-3cde714ed188-41d54379c38f-a1`;
- Research Inbox job ID:
  `RJOB-20260719T012222459450Z-ded84065dec1`;
- exception: `OutOfMemoryException`, attempting to pin a `256 KiB` block with
  `29.8 GiB / 29.8 GiB` used under the `32GB` DuckDB limit;
- observed service memory peak: `35.3G`;
- failure time: `2026-07-19T01:28:28Z`;
- terminal Inbox `STATUS.json` SHA-256:
  `66eeed4d2bf470788a49ec3d8836991208e0ae7cb16172d74cd7f1d201b234cf`;
- `RUN_COMPLETE.json` was not written; no results or report were produced;
- no retry was started and the `.03` one-shot ARM was consumed.

### `.04`: eleven-column narrowing still OOM, no report, ARM consumed

Release `D3-W2A-2026-07-18.04` used exact runtime commit
`40a1bf13eca85811dba1cbdc45dbdc0e4d2841bc`, narrowed the L1 interval
materialization to its eleven downstream-consumed columns, and changed the
DuckDB envelope to `16GB / 2 threads`.  It still terminated before producing
a later-stage completion receipt:

- run ID:
  `mode1-20260710-20260717-3cde714ed188-dde75a994495-a1`;
- Research Inbox job ID:
  `RJOB-20260719T014726297058Z-ded84065dec1`;
- exception: `OutOfMemoryException`, attempting to allocate a `256 KiB` block
  with `14.9 GiB / 14.9 GiB` used;
- observed service memory peak: `38.5G`;
- failure time: `2026-07-19T02:01:14Z`;
- terminal Inbox `STATUS.json` SHA-256:
  `157023cf94f24f1751d74515c17dca94baeee7f9335df6897e00420e6f2be740`;
- `RUN_FAILED.json` SHA-256:
  `e9f9516f1697fb5825805b94853f515736271c0398acd1f0d79f388a25884e4f`;
- consumed claim SHA-256:
  `c5ee3812e1f0972b3b3b93218ce3661221325b6b5b898a87f579e4bf5fc4df91`;
- AUTHORITY SHA-256:
  `dde75a994495696544075b37ca2eb070457f6fe663beab9f36cf0eb403e44971`;
- ARM SHA-256:
  `99b39184503565bec7e335089371a430c02fcb655c58959299cc7934934a47da`;
- `RUN_COMPLETE.json` was not written; no results or report were produced;
- no retry was started and the `.04` one-shot ARM was consumed.

The `.04` runtime did not emit an internal SQL-stage receipt.  The global
eight-date L1 window was therefore the highest-confidence candidate from code
and memory review, but that attribution remained an inference rather than
direct proof.

### `.05`: L1 repair succeeded; `trades_dedup` directly proven as the OOM stage

Release `D3-W2A-2026-07-18.05` used exact runtime commit
`eb56b45c04d00fa0ac64267f22b2b3d2a3d7d1d9`.  It preserved the eleven-column
L1 estimand and `16GB / 2 threads`, while decomposing L1 interval
materialization into one exact-manifest-date insert at a time.

The `.05` execution terminated with the following facts:

- run ID:
  `mode1-20260710-20260717-3cde714ed188-bdd1523a1ef3-a1`;
- Research Inbox job ID:
  `RJOB-20260719T022718424121Z-ded84065dec1`;
- failure time: `2026-07-19T02:36:42Z`;
- exception: `OutOfMemoryException`, attempting to pin a `256 KiB` block with
  `14.9 GiB / 14.9 GiB` used;
- observed service memory peak: `20.0G`;
- observed scratch peak during the run: approximately `4.8G`;
- AUTHORITY SHA-256:
  `bdd1523a1ef3d6f0b4370d8a5b74ac15c1317cff0b418b570f081d8a0e3b7b90`;
- ARM SHA-256:
  `7deee762ff795f3fe5ea615dd2a57119655a884b36455b9bf4eae560f7c84554`;
- the ARM was claimed at `2026-07-19T02:32:16Z`, consumed at the failure
  time, and may not be reused;
- `RUN_COMPLETE.json` was not written; no results or report were produced;
- no retry was started.

The four immutable local failure snapshots inspected read-only under
`/Users/ritcardo/HFT-BOT-deep03-repair-release-05/work/deep03_failure_05/`
are:

| Snapshot | SHA-256 |
| --- | --- |
| `08-research-run.log` | `e0d15077543551be01f889c21d964250fe54f842a6e2f00c1aa6ff585b699221` |
| `RUN_FAILED.json` | `a71ee4c7e7ee9b59a37ab5f70dd46d213a3ebebdac1ab3414ad979a172f28ac1` |
| `ARM_CLAIM_CONSUMED.json` | `0198029e996155315b55bd1bc7aeb387bb68d3f1b2c5d23b826cecf8def19e94` |
| `status.json` | `d9a44b9515770375afe8789eb7f1109b8336efbf6555909ef4a7b801f4bb0d83` |

The separately pulled terminal Research Inbox `STATUS.json` SHA-256 is
`1f6af3af4fcb4eea131efb289e6e9b0a1a39461e7a5df68ce29a347a9d9de0a5`.

The persistent `.05` research log directly records, in order:

1. `START` and `COMPLETE` for every L1 date from `2026-07-10` through
   `2026-07-17`;
2. `trade_id_qc state=START` and `trade_id_qc state=COMPLETE`;
3. `trades_dedup state=START`;
4. the DuckDB OOM, with no `trades_dedup state=COMPLETE`.

There is no unmarked SQL between the flushed `trades_dedup START` marker and
the synchronous materialization call.  The `.05` evidence therefore directly
proves that all eight per-date L1 materializations and global trade-ID QC
completed, and that the terminal exception occurred inside the global
`trades_dedup` statement.  It does not by itself identify whether the exact
DuckDB operator was a hash join, window sort, or output materialization.
Accumulated L1 state may have reduced available headroom, but it was not the
failing SQL statement.

All `.03`, `.04`, and `.05` ARMs are terminally spent.  None grants authority
for `.06`.

## Exact `.06` repair runtime

The exact audited runtime is commit:

`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`

Its parent is runtime-release commit
`fe05641169a467d879d2ac18f07efe4505b0b86a`.  This audit document is committed
separately as metadata; its commit must not be substituted for the exact
runtime commit above.

The `.06` implementation preserves the original global trade-ID quality
estimand:

```text
trade_id -> count(*) and count(DISTINCT hash(struct_pack(
  date, t_us, market_ticker, yes_price_e4, no_price_e4,
  count_e4, lower(taker_side)
)))
```

It also preserves the original winner definition:

```text
PARTITION BY trade_id
ORDER BY t_us,
         coalesce(recv_wall_ns,0),
         coalesce(recv_mono_ns,0),
         market_ticker
```

No trade ID is partitioned by date.  A complete non-null `trade_id` is the
unit of both QC and deduplication, so every row belonging to one ID remains in
one equivalence class even when source rows span dates.  Distinct IDs sharing
a hash bucket remain separate because the window still partitions on the full
ID.

### Fixed narrow output

`trades_dedup` now materializes only the ten columns consumed downstream:

1. `date`
2. `t_us`
3. `market_ticker`
4. `event_proxy`
5. `sport`
6. `trade_id`
7. `yes_price_e4`
8. `count_e4`
9. `taker_side`
10. `occurrence_us`

The discarded columns remain available where needed before winner selection:
`recv_wall_ns` and `recv_mono_ns` still determine the original order;
`fact_event_ticker` still contributes to `event_proxy`; and `no_price_e4`
still participates in global economic-variant QC.  `series_ticker` and
`league` are not consumed by B01 or B04 after deduplication.  B01 and B04
synthetic end-to-end regressions produce the same result structures under the
legacy wide query and the narrow `.06` relation.

### Unique-ID stream

The completed global `trade_id_qc` is reduced to
`trade_duplicate_ids`, containing only IDs with `raw_rows > 1`, their global
variant count, and `hash(trade_id) % 32`.  The larger global QC table is then
dropped before new materializations allocate memory.

Every non-null ID absent from `trade_duplicate_ids` has exactly one raw row;
under the pinned DuckDB hash semantics its struct hash is non-null and its
economic variant count is exactly one.  Those rows stream directly into the
ten-column result after the same date, receive-time, and market validity
filters.  They do not enter a blocking `row_number()` window.

### Adaptive duplicate path

The profile records both:

- `duplicate_raw_rows`: all raw rows belonging to IDs with `raw_rows > 1`;
- `eligible_duplicate_raw_rows`: raw rows belonging to duplicate IDs whose
  global `economic_variants = 1`.

Only `eligible_duplicate_raw_rows` controls the adaptive memory path:

1. **Zero eligible duplicate rows.** If there are no duplicate IDs, or every
   duplicate ID is globally conflicting, the implementation drops the
   duplicate-ID table and completes without creating any candidate relation
   or running an empty bucket loop.  Conflicting IDs remain excluded exactly
   as in the legacy query.
2. **Small duplicate set.** If
   `eligible_duplicate_raw_rows <= 1,000,000`, including the exact boundary,
   the implementation scans `trades_norm` once to create one narrow global
   duplicate-candidate table.  The table is checked for winner ambiguity,
   consumed through the 32 complete-ID hash buckets, and then dropped.
3. **Large duplicate set.** If
   `eligible_duplicate_raw_rows > 1,000,000`, the implementation never creates
   an all-duplicate global candidate table.  For each of 32 fixed complete-ID
   hash buckets, it explicitly filters both the source and QC sides, creates
   one narrow per-bucket candidate table, runs ambiguity QC and the unchanged
   winner window, inserts the ten-column winners, and drops that candidate
   table before advancing to the next bucket.

The large path may scan `trades_norm` up to 32 times.  That is an intentional
I/O/time tradeoff to keep the global duplicate candidate and global winner
window out of memory.  It does not split a trade-ID equivalence class or alter
the statistical result.

### Full winner-key ambiguity gate

Changing physical joins or materialization can change which row DuckDB picks
when two rows collide on every existing winner-order component.  `.06` does
not silently add a new tiebreaker, because doing so would change the authorized
winner definition.  Instead, each candidate set is grouped by the exact full
winner key:

```text
(trade_id, t_us,
 coalesce(recv_wall_ns,0), coalesce(recv_mono_ns,0),
 market_ticker)
```

Within each such group, the implementation counts distinct exact structs of
all ten downstream output columns.  If one order key maps to more than one
ten-column output, the run fails closed before selecting an arbitrary winner
from that candidate set.  If tied rows have an identical ten-column output,
either physical winner yields the same relation and the tie is allowed.

This gate closes the output-drift risk introduced by physical replanning
without inventing a new statistical tie policy.  Synthetic tests exercise the
gate in both the small-global and large-per-bucket paths, and separately prove
that identical-output ties remain legal.

## Verification performed

At exact runtime commit
`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`:

- five focused suites collected and passed `107` tests:
  `test_deep03_one_shot_arm.py`,
  `test_deep03_v3_open_discovery.py`,
  `test_deep03_v3_w1_preflight.py`,
  `test_w09_exploratory_autoresearch.py`, and
  `test_w09_bringup.py`;
- bidirectional `EXCEPT ALL` tests compare both the small-global and forced
  large-per-bucket results with the legacy global query;
- tests cover no duplicate IDs, an all-conflicting duplicate set, the exact
  `1,000,000` boundary, `1,000,001`, cross-date IDs, conflicting economic
  variants, null receive-clock ordering, taker-side case normalization,
  same-bucket distinct IDs, empty buckets, failure-stage markers, the exact
  ten-column schema, full-key output ambiguity, and identical-output ties;
- B01 and B04 result structures are equal under the legacy and narrow
  materializations on synthetic method inputs;
- the `16GB / 2 threads` W09 runtime contract remains pinned by the existing
  deployment test;
- `tools/research/deep03_v3_methods.py` SHA-256 is
  `73dfc0893bcadb113797b8a775083adb2a2b8b9b880ef4ec41d17567fd755635`;
- `deploy/w09/deep03_open_discovery_modules.sha256` SHA-256 is
  `46bc852222d8cabb6948a33ceeef4c4e1f9859ee2cd188b366fcdf0a4cc90605`
  and passed `sha256sum -c`;
- `deploy/w09/exploratory_autoresearch_payload.sha256` SHA-256 is
  `e48cb9b967cbfde2cfc98efc1e2f3defd6811f526bebf5923e7bdd96b759be84`
  and passed `sha256sum -c`;
- the outer payload manifest now explicitly pins the inner
  `deep03_open_discovery_modules.sha256` bytes.  The installed payload can no
  longer substitute a different inner manifest while satisfying only the
  outer deployment check;
- the exact runtime diff passed `git diff --check`; and
- the post-commit worktree was clean before this metadata-only audit document
  was added.

## Explicit remaining risks

This audit establishes relational equivalence and a materially narrower
memory plan.  It does not prove production-scale completion:

1. No real eight-day execution over the roughly `30 million` trade rows has
   occurred under `.06`.
2. `1,000,000` is a row-count threshold, not a byte cap.  Candidate rows
   contain variable-width strings.
3. The 32 hash buckets are complete physical partitions, but they are not an
   absolute one-million-row cap per bucket.  Hash skew can make one bucket
   larger than the average.
4. The large path may scan the source up to 32 times.  Its wall-clock and EBS
   I/O cost at the exact eight-day scale have not been measured.
5. The retained L1 interval relation and final ten-column `trades_dedup`
   relation still coexist for downstream methods.  DuckDB may spill them, but
   the full downstream B01/B02/B03/B04 run has not passed at this scale.
6. B01's full-scale ASOF joins and later aggregation/materialization stages
   remain unvalidated memory hotspots.  New stage markers improve diagnosis;
   they do not prove those stages will complete.
7. A failure after some per-bucket inserts still produces no valid result:
   without `RUN_COMPLETE.json`, partial in-memory or scratch state is not a
   report and must never be published or resumed in place.

These are explicit execution risks, not permission to raise memory, change
threads, alter methods, extend dates, enable RFQ, or retry automatically.

## Explicit blockers before any `.06` execution

1. Persist this audit as a dedicated audit-only commit and record its exact
   document SHA-256.
2. Draft exact `.06` W0 and W1 release candidates bound to this audit's exact
   bytes/SHA, runtime commit
   `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`, the same eight releases,
   `2,657` objects, `29,473,216,651` bytes, the same evidence tier, RFQ `OFF`,
   and `16GB / 2 threads`.
3. Bind W1 `.06` to the exact W0 `.06` SHA.  Any mechanically rebound
   `W1_COMPLETE.json` must preserve the completed canary, DQ, input projection,
   completion time, and artifact hashes and must not claim that W1 or research
   was rerun.
4. Obtain a new explicit operator authorization naming exact release
   `D3-W2A-2026-07-18.06`, exact runtime commit
   `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`, the same input releases and
   mode, RFQ `OFF`, the adaptive repair, and separately approved cumulative
   runtime and spending caps.  This audit is not that authorization.
5. Only after that operator authorization, generate a new exact AUTHORITY and
   a new one-shot ARM bound to the exact plan, audit, W0, W1, W1-completion,
   runtime and eight input-release hashes.  No `.03`, `.04`, or `.05` authority
   artifact may be edited or reused.
6. Before starting computation, independently verify the installed runtime,
   both SHA-manifest layers, every release and prerequisite hash, the new
   AUTHORITY, and the new ARM.  Any mismatch must fail closed before the
   one-shot claim.
7. Permit at most the single execution separately authorized by the operator.
   There is no automatic retry.  Any terminal failure consumes the new ARM
   and requires a new operator decision.
8. Claim a report only after a valid sealed `RUN_COMPLETE.json`, complete
   artifact hash list, result bundle, and rendered report exist.  Until then,
   the accurate project outcome remains “no report.”

## Audit conclusion

`PASS_WITH_EXPLICIT_BLOCKERS` means `.06` release metadata may be drafted and
independently checked around exact runtime commit
`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`.  It does not mean the repair has
completed at real scale, and it does not authorize W09 execution.

D3-W2A `.06` remains `NO-GO` until a new explicit `.06` operator
authorization and newly generated exact AUTHORITY/ARM satisfy every blocker
above.  Research execution conferred by this audit is `NO`.

This audit did not connect to W09, modify code, modify W0/W1, generate an
AUTHORITY or ARM, start research, read RFQ, open a holdout, write S3, mutate
production, access trading credentials, send messages, or place orders.
