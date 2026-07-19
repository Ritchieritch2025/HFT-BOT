# DEEP03 D3-W2A `.08` bounded-memory successor — independent pre-release design audit

Date: `2026-07-19`

Verdict: `PRE_RELEASE_DESIGN_PARTIAL_PASS_WITH_MUST_GATES`

Release-ready claim: `NO`

Research execution conferred: `NO`

AWS mutation, spending, AUTHORITY, or ARM conferred: `NO`

## Audit purpose and boundary

This is an independent **design-gate audit**, not a release audit. It reviews
the first bounded-memory primitives on branch `w-deep03-bounded-08` and defines
the evidence required before a `.08` release candidate may be called complete.

The intended successor keeps the exact D3-W2A research scope:

- mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`;
- window: `2026-07-10` through `2026-07-17` inclusive;
- inputs: the same eight exact V3 releases, `2,657` objects and
  `29,473,216,651` bytes;
- evidence: `SEALED_DEGRADED_EVIDENCE`, exploratory claims only;
- methods: the same B01/B02/B03/B04 open-discovery scope;
- RFQ: `OFF_AND_ABSENT`;
- host target: fixed W09 `i-0e53d134dceffe166`, currently proven only as
  `r8g.2xlarge`, `8 vCPU / 64 GiB` without a new quota.

This audit does not approve a change to an estimand, trade winner, ASOF winner,
evidence tier, input release, method, holdout state, RFQ state, fee/PnL claim,
or order authority.

## Exact snapshot inspected

The design primitives inspected are:

| Commit | Purpose |
| --- | --- |
| `40cca4634fec6335301f1904405c7da2cff6893e` | fail-closed durable checkpoint primitives |
| `18890206e7bed682f9f81c9cab03231c65efdbe5` | same-timestamp L1 ASOF ambiguity gate |
| `5cfc08916d3b9eacc8919084e83439b63308bf2d` | one-query physical partition scatter |

The inherited `.06` statistical/runtime lineage is commit
`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`.

The following checks passed at `5cfc089...`:

- `19/19` targeted bounded, ASOF, L1-equivalence, and trade-dedup tests;
- `git diff --check`;
- no AWS, W09, S3, IAM, AUTHORITY, ARM, or research execution was performed.

This was a focused design test, not the complete release suite or a real-data
acceptance run.

## What the current primitives establish

1. `bounded_source_binding()` excludes movable local cache paths while binding
   exact release/object identity at
   `tools/research/deep03_v3_methods.py:110-143`.
2. `BoundedCheckpointStore` records source binding, stage/version, partition,
   Parquet SHA-256, size, row count, schema, and canonical receipts at
   `tools/research/deep03_v3_methods.py:146-496`.
3. A payload without a COMPLETE receipt is not reused; a completed payload is
   verified before reuse; an unexpected receipt prevents stage finalization.
4. `_assert_l1_asof_timestamps_unambiguous()` records and rejects economically
   different equal-`t_us` L1 states at
   `tools/research/deep03_v3_methods.py:637-700`.
5. `write_partitioned_query()` can scatter one logical query into physical
   partitions through one DuckDB partitioned COPY at
   `tools/research/deep03_v3_methods.py:345-438`.

These are useful foundations. They are not yet an end-to-end bounded research
engine.

## Current non-release-ready finding

At the inspected snapshot, the checkpoint and scatter classes are not wired
into `setup_database()`, `execute_all()`, or `run_discovery()`. The live path
still creates process-local/global DuckDB TEMP tables for L1, trades, and B01
at `tools/research/deep03_v3_methods.py:805`, `:950`, and `:1307`; the runner
still opens an in-memory DuckDB connection at
`tools/research/deep03_v3_runner.py:495`.

There is no implemented bounded scheduler, complete physical source-shard
plan, globally correct bounded trade-ID reducer, durable B01 observation
pipeline, exact B01/B02/B03/B04 final reducer, checkpoint-aware runner, or
full report-producing `.08` path. Therefore:

> The current snapshot is a partial design implementation only. It is not a
> `.08` runtime release, is not safe to deploy as the research runner, and
> cannot be represented as capable of completing the eight-release research.

## MUST gates before `.08` can be release-ready

### MUST-01 — complete end-to-end integration

Wire a single bounded entry point from the authorized runner through physical
sharding, L1 intervals, global trade identity, B01 observations, B02/B03/B04
inputs, exact reducers, result receipts, report, and `RUN_COMPLETE.json`.
The legacy global TEMP-table route must not be reachable in `.08` production.

Acceptance requires a test proving the actual CLI/runner invokes the bounded
path and cannot silently fall back to the inherited global plan.

### MUST-02 — separate and close both L1 tie hazards

The inherited B01 right sides use only `(date, market_ticker, t_us)` at
`tools/research/deep03_v3_methods.py:1315-1319` and `:1359-1364`. Economically
different usable quotes at equal `t_us` have no disclosed winner.

The new gate is necessary, but its final contract must distinguish:

1. the B01 ASOF population, which uses only `TWO_SIDED` rows and B01-relevant
   quote economics; and
2. the L1 interval `lead()` population, whose ordering includes receive wall,
   monotonic, session, and sequence keys.

The interval path also needs a fail-closed test for two economically different
rows sharing the **complete** coalesced interval ordering key. A one-sided row
and a two-sided row at one `t_us` must not be mislabeled as a B01 ambiguity if
only one is eligible for B01; any stricter rejection must be explicitly tied
to the interval ambiguity contract.

No receive-order ASOF winner may be introduced as an undisclosed execution
repair. If real-data ambiguity is nonzero, stop or obtain a separately audited
semantic correction.

### MUST-03 — prove true O(1) source scanning

The inherited large-duplicate path explicitly rescans the trade source per
hash bucket at `tools/research/deep03_v3_methods.py:1032-1034`. `.08` must
physically scatter each source row a constant number of times before any
partition loop, and every later step must read only bounded shards.

Use DuckDB 1.4.5 JSON profiling for bucket counts `1`, `8`, and the production
count. Assert that original `READ_PARQUET` nodes, `operator_rows_scanned`, and
`Total Files Read` stay constant rather than multiplying by bucket count.
Static SQL-shape checks alone are insufficient.

The scatter must reject an unexpected partition value and prove row
conservation across expected, null/invalid, and excluded rows. The current
primitive publishes named partitions but does not yet prove that generated
partition directories equal the complete expected domain.

### MUST-04 — preserve global trade identity across all eight dates

Trade-ID QC, economic-conflict exclusion, winner ordering, and ambiguity
failure must operate on complete full-ID hash buckets containing every date.
Date-local dedup is forbidden.

The inherited economic-variant test is
`count(DISTINCT hash(struct_pack(...)))` at
`tools/research/deep03_v3_methods.py:1219-1227`. That is the exact `.06`
behavior but is theoretically hash-collision-sensitive. `.08` must either
preserve and label it as inherited equivalence or treat collision-proof
`DISTINCT struct_pack` as a separately disclosed semantic correction; it may
not silently switch claims.

### MUST-05 — implement exact, not approximate, reducers

The final result must retain and reduce the narrow raw observations needed for:

- B01 p50 and exact distinct event/market/day counts
  (`tools/research/deep03_v3_methods.py:1387-1392`);
- B02 p50/p95 and exact distincts (`:1436-1441`);
- B03 linked-event distinct-market qualification (`:1515-1519`);
- B04 active event-minute, market, and root-event distincts beginning at
  `:1570`.

Partition quantiles must never be averaged. Partition distinct counts must
never be summed. Counts, sums, and means may be merged only under an explicit
numerically reproducible contract. B01 DOUBLE means are order-sensitive at the
bit level; the final reducer needs a fixed canonical observation/summation
order or must define and audit a narrow numeric-equivalence tolerance against
the `.06` oracle while keeping `.08` rerun hashes deterministic.

### MUST-06 — close checkpoint durability, ABI, and concurrency gaps

Before a COMPLETE receipt becomes visible, the Parquet data file and its
parent directory must be fsynced. Metadata fsync alone is not enough. The
current `_publish_temporary()` renames the DuckDB output at
`tools/research/deep03_v3_methods.py:318-337` without an explicit data-file and
data-directory fsync before receipt publication.

Every reusable checkpoint must bind:

- canonical exact source identity;
- stage ABI/algorithm hash, output schema, constants, partition/hash algorithm,
  and pinned DuckDB version;
- complete partition key and upstream stage-manifest hashes.

A human-readable `stage_version` string alone is not a sufficient ABI. Add a
shared checkpoint lock or fencing generation so two authorized run directories
cannot publish the same partition concurrently. Reject symlink/path
substitution and receipt/payload time-of-check/time-of-use drift.

### MUST-07 — define authorized resume without reusing a consumed ARM

The current runner refuses stale/partial run artifacts at
`tools/research/deep03_v3_runner.py:474-481`. The systemd service consumes its
ARM on every outcome and intentionally has no restart at
`deploy/w09/w09-exploratory-autoresearch.service:16` and `:41-42`.

Durable checkpoints therefore need a separate fixed checkpoint namespace
derived from exact dataset identity plus stage ABI, not the run ID or ARM SHA.
A killed run may be resumed only by a newly authorized run/fresh ARM that first
revalidates every checkpoint. Runtime or stage-ABI drift invalidates reuse
unless an explicit audited compatibility mapping exists.

### MUST-08 — deterministic output and complete conservation

Every persisted Parquet dataset must use a canonical full sort order or a
canonical logical row digest. Repeat runs with reversed input-object order and
different partition completion order must produce equal row multisets, equal
stage manifests, and deterministic final result hashes.

For every stage record and assert source rows, accepted rows, excluded rows,
null/invalid rows, and output rows. Empty expected partitions must remain
explicit and schema-correct. Unexpected or duplicate partitions must fail
closed rather than disappear during cleanup.

### MUST-09 — fit the real W09 disk and memory envelope

The handoff records only about `62 GB` free on the 300 GB W09 volume while the
exact inputs total about `29.47 GB`. Before writing any real checkpoint, a
read-only preflight must conservatively bound simultaneous physical shards,
durable intermediates, B01 observations, DuckDB spill, report output, and a
safety reserve. Insufficient space must stop before computation; source
evidence and immutable receipts may not be deleted to create headroom.

Start with sequential partitions under the inherited conservative
`16GB / 2 threads`, `MemoryHigh=40G`, `MemoryMax=52G` envelope. Record peak RSS,
DuckDB `system_peak_buffer_memory`, `system_peak_temp_dir_size`, scratch
high-water, bytes written, and elapsed time before permitting parallelism.

### MUST-10 — fixture, real-subset, and full-data acceptance

The release gate needs all three layers:

1. synthetic adversarial fixtures;
2. an exact real subset small enough to run both `.06` global and `.08`
   bounded plans and compare them;
3. the authorized full eight-release run on W09, producing a sealed
   `RUN_COMPLETE.json`, results, receipt ledger, charts, and report.

Passing unit tests alone cannot prove the 64 GiB runtime or storage envelope.

## Required test matrix

| ID | Test | Required result |
| --- | --- | --- |
| `T-EQ-01` | two-date/multi-market synthetic global-vs-bounded oracle | exact 11-column L1 and 10-column trade multisets; exact waterfalls/results |
| `T-EQ-02` | deterministic real subset that fits both plans | canonical observation and summary equality with receipt |
| `T-ASOF-01` | different usable two-sided quotes at same ASOF key | fail closed before B01 output |
| `T-ASOF-02` | exact economic duplicate at same ASOF key | legal and invariant to input order |
| `T-ASOF-03` | complete receive-order tie with different interval economics | fail closed before L1 interval output |
| `T-ASOF-04` | trade near UTC date boundary | no cross-date halo; exact `.06` equivalence |
| `T-TRD-01` | repeated/conflicting trade ID across dates | one global decision; no date-local survivor |
| `T-TRD-02` | bucket `0`/last, collision, empty bucket, winner tie | complete-ID conservation and exact inherited winner |
| `T-RED-01` | B01 odd/even observations split across shards | exact p50; exact event/market/day union |
| `T-RED-02` | B02 values whose average-of-medians is wrong | exact p50/p95 and distinct union |
| `T-RED-03` | linked B03 markets split across hash buckets | one correctly qualified date/event candidate |
| `T-RED-04` | same B04 event-minute in multiple market buckets | one event-minute, correct market/root distincts |
| `T-CHK-01` | kill before/after data write, fsync, rename, receipt | only fully durable COMPLETE partition reused |
| `T-CHK-02` | byte flip, truncate, wrong hash/size/count/schema/source/ABI | fail closed; never silently repair |
| `T-CHK-03` | symlink, path traversal, unexpected receipt/data, two writers | fail closed with no publication race |
| `T-CHK-04` | killed run then fresh authorized resume | completed partitions reused; incomplete only recomputed; same result |
| `T-DET-01` | reversed files and randomized partition completion | identical canonical stage/final hashes |
| `T-SCAN-01` | DuckDB JSON profile at multiple bucket counts | constant original source rows/files scanned |
| `T-CAP-01` | disk bound below required headroom | zero checkpoint writes and explicit refusal |
| `T-RES-01` | W09 sequential bounded canary | peak memory/scratch within audited envelope |

Existing `.06` tests for bucket/global trade equivalence, trade ambiguity,
trade-to-B01/B04 equality, and date-bounded L1 equivalence must remain green;
they are regression oracles, not substitutes for the matrix above.

## Release discipline after the MUST gates

Only after all MUST gates pass may a separate independent release audit:

1. name one exact `.08` runtime commit and prove the worktree clean;
2. record full focused and regression test counts;
3. bind all deployed module SHA manifests;
4. bind exact W0/W1/W1 completion metadata to the same inputs and audit;
5. specify the measured resource, runtime, and cost envelope;
6. obtain new explicit operator authorization;
7. generate a new one-shot AUTHORITY/ARM only after host/data preflight;
8. permit one run with no automatic repair or retry.

## Audit conclusion

`PRE_RELEASE_DESIGN_PARTIAL_PASS_WITH_MUST_GATES` means the checkpoint,
same-timestamp QC, and one-pass scatter primitives are credible foundations
worth integrating. It does **not** mean `.08` is implemented end to end,
statistically equivalent, scalable at real size, durable across power/process
failure, deployable, authorized, or release-ready.

No report exists from `.08`; no research has been completed by these changes.
Any statement that `.08` can now run the eight exact releases on W09 would be
premature until every MUST gate and the separate release audit are complete.
