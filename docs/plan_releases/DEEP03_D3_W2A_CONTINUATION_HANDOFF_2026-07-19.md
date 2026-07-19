# DEEP03 D3-W2A continuation handoff

Date: `2026-07-19`

Purpose: allow a fresh agent to continue the current research without
reconstructing prior sessions. This file confers **no AWS mutation, spending,
AUTHORITY, ARM, or research-execution permission**.

## One-line objective

Complete the existing `D3-W2A` `MODE 1 / EXPLORATORY_AUTORESEARCH` run over
the eight exact V3 releases for `2026-07-10` through `2026-07-17`, execute
`B01` through `B04`, and produce the evidence-indexed report. RFQ remains
`OFF` for this run.

## Current infrastructure state

- Production instance: `i-0fd427becf740a06b`, `r8g.2xlarge`, `8 vCPU / 64
  GiB`, running and independently collecting data. Do not resize, stop, or
  place research work on it.
- Research instance W09: `i-0e53d134dceffe166`, fixed role/profile
  `w09-research-runner`, region `us-east-2`, fixed Elastic IP previously
  `18.226.151.192`.
- W09 was returned to `r8g.2xlarge` and verified live over SSH on
  `2026-07-19`: IMDS reported exact instance
  `i-0e53d134dceffe166`, type `r8g.2xlarge`; the host reported `8` CPUs and
  `64,643,260 KiB` RAM. Research and its autoresearch timer were inactive.
  The enabled idle-stop timer remained active, so the box may stop itself
  after its normal idle window.
- The prior attempted change to `x8g.4xlarge` could not start because the
  account's `Running On-Demand X instances` quota was `0`. An increase to
  `16` X vCPUs is pending AWS review.
- A later attempt to start `r8g.8xlarge` also required a quota increase. It
  needs `32` Standard vCPUs; together with production it requires an applied
  Standard quota of at least `40`. The exact current applied Standard quota
  has not been read through an account-wide API in this session.
- The only no-new-quota W09 shape proven by prior operation is
  `r8g.2xlarge`, `8 vCPU / 64 GiB`.
- At the same live check, the 300 GB root volume was `79%` used with about
  `62 GB` free. A checkpointed successor must measure its storage bound,
  remove only independently verified disposable scratch/cache, or expand EBS
  before materializing durable partitions. Never delete source evidence or
  immutable receipts to make space.
- S3, IAM, exact-version manifests, EBS, and production-side publication are
  not the blocker. Do not create another IAM policy or copy the dataset.

## Exact research input and scope

- Adopted plan:
  `docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`
- Plan SHA-256:
  `ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`
- Inputs: `2,657` exact-version V3 objects, `29,473,216,651` bytes.
- Evidence tier: `SEALED_DEGRADED_EVIDENCE`; exploratory claims only.
- Holdout: none opened.
- RFQ: `OFF_AND_ABSENT` for this D3-W2A run.
- Methods: unchanged partial open-discovery `B01/B02/B03/B04` scope.

Exact releases:

1. `2026-07-10__v3ref__seal-86c01ed0__pub-45f5b99ff4fdf580`
2. `2026-07-11__v3ref__seal-de2e77c8__pub-e038f638b307da69`
3. `2026-07-12__v3ref__seal-bc37de4c__pub-abd451660aeedf0c`
4. `2026-07-13__v3ref__seal-7f6e5c1b__pub-55f66e41dba1484d`
5. `2026-07-14__v3ref__seal-2d6a4941__pub-748d8d77f665d60a`
6. `2026-07-15__v3ref__seal-22aa1b04__pub-4ae53f539c340052`
7. `2026-07-16__v3ref__seal-60b2674e__pub-fd365b5cd76099a0`
8. `2026-07-17__v3ref__seal-e25887ab__pub-74f48fcbc81a41b7`

## Terminal attempt chain and exact boundary

- `.03`: global L1 materialization OOM; no report; ARM consumed.
- `.04`: narrow L1 with DuckDB `16GB / 2 threads` OOM; no report; ARM
  consumed.
- `.05`: all eight date-bounded L1 materializations completed; global
  `trades_dedup` OOM; no report; ARM consumed.
- `.06`: runtime commit
  `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`. It completed all eight L1
  dates, global trade-ID QC, unique-ID stream, duplicate QC, and all `32/32`
  full-ID hash buckets, then OOMed immediately after
  `trades_dedup state=COMPLETE`; no report; ARM consumed.

The `.06` log has no explicit B01 `START` marker. Control flow makes the first
blocking `b01_pre` ASOF materialization the highest-confidence failure
boundary, but the exact DuckDB operator was not directly proven. Do not claim
stronger attribution.

Immutable `.06` evidence is under:
`/Users/ritcardo/HFT-BOT-deep03-trades-fix/work/deep03_failure_06/`.
The governing details and receipt hashes are in
`docs/plan_audits/AUDIT_DEEP03_D3_W2A_07_HIGH_MEMORY_ENVELOPE_REPAIR_2026-07-19.md`.

Important correction: `.06` used `duckdb.connect()` in memory and built L1
and trade intermediates as DuckDB `TEMP TABLE`s. The log proves those stages
completed, but their materializations are **not reusable checkpoints** after
the failed process exited. A new run must recompute those transforms from the
already available exact-version inputs. It must not re-ingest, reseal, retag,
or copy the raw dataset.

## Ready path A: X quota becomes applied

An independently audited high-memory release already exists:

- Branch/worktree: `w-deep03-highmem-07` /
  `/Users/ritcardo/HFT-BOT-deep03-highmem-07`
- Runtime commit: `768c3017164f141b99e5062008386b9d1bb0d2bf`
- Metadata/audit commit: `bd4ee95cda3d3bf73f63fbd3dad84b7820a03fb2`
- Audit SHA-256:
  `7c23b850c93d8091bdc011ed686d812016187d2acf792f85eaea5e26fe456310`
- W0 SHA-256:
  `3eb0d850667ac4abd015ff2ebdcc6f6dd3476e445eccaf684d45706e8b9e7bed`
- W1 SHA-256:
  `30e5973f44af0fbf9760b339a8c94b996a95eca3f74bdd5dedb0b39e2fcc2625`
- W1 completion SHA-256:
  `99b63c69475f116ccef9264a8aa2bbbbbd58e7b1bfe1b3575a15c6a982eb6e0c`
- Envelope: exact `x8g.4xlarge`, `256 GiB`, DuckDB `128GB / 2 threads`,
  systemd `MemoryHigh=192G`, `MemoryMax=224G`.
- Tests: runtime `146/146` and metadata `107/107` passed during release
  preparation.
- No `.07` AUTHORITY or ARM has been generated and no `.07` run has started.

If and only if the X quota is `Applied`, start W09 as exact
`x8g.4xlarge`, attest IMDS/memory/profile/role, obtain a fresh explicit
operator authorization, generate one new one-shot AUTHORITY/ARM, and run the
audited package. Never reuse an older ARM.

Residual risk: the audit explicitly states that `128GB` is a limit, not proof
that the unchanged global query will complete. The 300 GB gp3 scratch volume
can also become a bottleneck.

## Ready path B: bounded-memory successor on `r8g.2xlarge`

If X remains unavailable, do not perform another memory-only retry. Build a
new, independently audited successor release using durable bounded
partitions. This is an execution-plan repair; the research questions and
estimands remain fixed unless the tie QC below forces a separately disclosed
semantic correction.

Minimum design requirements:

1. **Physical sharding once.** Partition by exact date plus a complete
   `market_ticker` hash bucket. Read each source row O(1) times. Never loop
   over markets while rescanning whole-day Parquet, which would be
   O(markets x day bytes).
2. **Durable checkpoints.** Write narrow Parquet stage outputs plus canonical
   manifests, SHA-256s, row counts, source-release bindings, schema/version,
   and atomic completion receipts. A restart may reuse only a fully verified
   completed partition. Partial scratch is never publishable.
3. **L1 intervals.** Preserve the existing date/market ordering, TTL, gap,
   terminal-boundary, phase, censoring, and 11-column semantics. Persist
   verified date/bucket outputs rather than one process-local temp table.
4. **Global trade identity.** `trade_id` QC and conflict handling remain
   global across all eight dates. Date-local dedup is forbidden. Use complete
   full-ID hash buckets, preserve the existing economic-conflict exclusion,
   winner ordering, and ambiguity fail-closed check, then checkpoint the
   narrow ten-column outputs.
5. **B01 partitioning.** Both ASOF joins key on `(date,market_ticker)`, so
   complete date/market shards require no cross-shard halo under the current
   estimand. Persist only the narrow observations needed for final results.
6. **Same-timestamp ASOF gate.** Current B01 ASOF sources order only by
   `t_us`. Multiple economically different L1 rows at the same
   `(date,market_ticker,t_us)` can produce insertion-order-dependent winners.
   Before claiming exact equivalence, run full-data QC. If ambiguity count is
   zero, record it. Otherwise fail closed or introduce a full receive-order
   canonical winner as an explicitly audited semantic correction.
7. **Exact reducers.** Counts, sums, and means are mergeable. Exact
   `quantile_cont` medians/p95 and distinct event/market/day counts are not
   obtained by averaging partition quantiles or summing distinct counts.
   Retain narrow observation values and exact union/dedup keys, then run a
   bounded exact reducer one stratum at a time.
8. **B02/B03/B04.** B02 interval construction is date/market separable but
   needs the exact duration-quantile reducer. B03 is date/event reducible and
   remains `NOT_ESTIMABLE` under its existing blockers. B04 is naturally
   date-oriented but needs a date-level union/dedup reducer for active-event
   and root-event distincts.
9. **Conservation/equivalence tests.** On fixtures and a real subset that fits
   both plans, compare global and partitioned waterfalls/results exactly.
   Include same-timestamp L1 conflicts, cross-date repeated/conflicting trade
   IDs, hash-boundary cases, capture gaps, terminal intervals, exact
   quantiles, distinct counts, killed-process resume, corrupted checkpoint
   rejection, and deterministic rerun hashes.
10. **Bounded scheduler.** Start sequentially on W09 `r8g.2xlarge` with the
    previously proven conservative memory/thread envelope. Permit bounded
    parallel partitions only after measured peak-RSS and scratch headroom
    prove it safe.

Scalability verdict: this design bounds peak RAM; added dates/markets grow
runtime and durable intermediate storage approximately linearly. Independent
partitions can later run concurrently or across workers. It is more scalable
and recoverable than repeatedly increasing one global DuckDB memory envelope.

## Required release discipline

- Start the successor from exact `.06` method semantics/runtime lineage; do
  not silently edit the immutable `.07` release.
- One measurable/testable unit per commit; keep the worktree clean between
  units.
- Independent audit must verify source/input identity, statistical
  equivalence, checkpoint fail-closed behavior, resource/cost envelope, and
  all generated SHA manifests.
- Code/audit preparation confers no research execution. Before W09 execution,
  obtain one explicit operator authorization for the exact new runtime,
  resource envelope, time/cost cap, releases, mode, and RFQ state.
- Generate a new one-shot AUTHORITY/ARM only after preflight passes. A terminal
  result consumes it. No automatic repair or rerun is authorized.

## Minimal fresh-agent instruction

Paste this with the file path to a replacement agent:

> Read
> `docs/plan_releases/DEEP03_D3_W2A_CONTINUATION_HANDOFF_2026-07-19.md`
> completely and verify its cited commits/files. Preserve production and make
> no AWS writes. First check whether the X quota is Applied. If yes, continue
> exact audited `.07` only after fresh operator authority. If not, implement
> the bounded-memory successor exactly under Path B, one tested commit at a
> time, obtain an independent audit, then present one exact execution
> authorization. Do not rediscover or repeat ingestion/IAM work.
