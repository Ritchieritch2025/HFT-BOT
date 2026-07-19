# DEEP03 D3-W2A `.08` bounded-memory repair — independent release audit

Audit completed: `2026-07-19T14:24:36Z`

Independent audit verdict: `PASS_FOR_RELEASE_DRAFTING`

Audited runtime commit:
`b0d56eb62068d31f3846a1cf20ccb755ff6ca848`

Release-integration commit inspected:
`0360019cd60a86f4b5a5086a1b196c14c8ea8b93`

## Exact meaning of the verdict

`PASS_FOR_RELEASE_DRAFTING` means the bounded `.08` runtime is suitable to be
bound into `.08` W0/W1/W1_COMPLETE metadata and, after a new exact operator
authorization and fresh one-shot ARM, installed for **one** exploratory run on
the fixed W09 host.

It does **not** mean:

- research execution is authorized by this audit;
- an AUTHORITY or ARM exists;
- the eight-release full-data run has completed;
- a report exists;
- real-scale memory, scratch, elapsed-time, or result acceptance has passed;
- strict evidence acceptance, candidate strategy, profitability, PnL,
  promotion, shadow, order, or production authority exists.

The first full W09 run remains the full-scale runtime acceptance. A failure,
OOM, timeout, conservation mismatch, ambiguity, or corrupt checkpoint must
produce no completed research claim and requires a fresh operator-authorized
run/ARM.

## Scope audited

The release remains the narrowly authorized D3-W2A open-discovery wave:

- mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`;
- phase: `OPEN_DISCOVERY`;
- dates: `2026-07-10` through `2026-07-17` inclusive;
- exact inputs: the same eight V3 releases, `2,657` objects and
  `29,473,216,651` bytes;
- evidence: `SEALED_DEGRADED_EVIDENCE`, exploratory use only;
- methods: `D3-B01-MARKOUT`, `D3-B02-ONESIDE`, `D3-B03-XMKT`, and
  `D3-B04-RHYTHM` only;
- RFQ: `OFF_AND_ABSENT`;
- host: W09 `i-0e53d134dceffe166`, `r8g.2xlarge`, 8 vCPU / 64 GiB;
- runtime: DuckDB `16GB`, 2 threads; systemd `MemoryHigh=40G` and
  `MemoryMax=52G`.

This is not the whole Deep03 plan. It does not execute the L2 mechanism waves,
the breadth compiler, fee-after strategy replay, terminal-outcome validation,
or monetizable-strategy selection. L2 quality metadata may appear in the data
quality section, but this W2A runtime's analytical estimands are L1/trades,
dimensions, and capture-gap based. RFQ remains outside this release.

The adopted plan inspected was
`docs/research_reports/DEEP03_GRANULAR_STRATEGY_TEST_PLAN_CANDIDATE_2026-07-17.md`,
SHA-256
`ded84065dec179ce713377b53607b04adad342ae77e5c3c2b894247bc07b5d36`.

## Lineage and immutable payload binding

The statistical lineage starts at the last `.06` runtime
`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`. The `.08` successor changes the
physical execution plan to bounded, durable partitions while retaining the
same B01-B04 descriptive estimands and global trade-ID rules.

The inspected payload pins are:

| Artifact | SHA-256 |
| --- | --- |
| `tools/research/deep03_v3_methods.py` | `8644f943d3ec5a747c7a10034b76371e3e0f6a41928c4b2e567bebe8b0abc791` |
| `tools/research/deep03_v3_runner.py` | `cc70edb11f002301797e030940bd744f11cfec4d14f7d370210dad45fcd404d1` |
| `deploy/w09/deep03_open_discovery_modules.sha256` | `e91f190500590a22f33102a591b8f6bdfa5af52d2721a9259cd429962adc3439` |
| `deploy/w09/exploratory_autoresearch_payload.sha256` | `2488a424ad7c46032fb01d1b40e0d440ec1fbc4b2b6d53ada9698573ee5dee32` |

All entries in the Deep03 module, exploratory payload, research-reader, and V3
query-canary manifests verified against the inspected worktree. The packager
now requires an explicit `W09_RUNTIME_COMMIT`, requires the clean source
worktree HEAD to equal it, writes that source commit into the payload, and
allows later audit/release metadata commits to live in the separate clean
release worktree (`deploy/w09/push_and_install.sh:6-40,47-101`). This closes the
earlier runtime/metadata self-reference defect.

## Statistical-equivalence evidence

The bounded route is the only route called by the production runner
(`tools/research/deep03_v3_runner.py:737-742`). It cannot silently fall back to
the inherited global TEMP-table route.

The adversarial end-to-end fixture compares the legacy global engine and the
bounded engine across two dates, multiple markets and market-hash boundaries
at `tests/test_deep03_v3_bounded_reducers.py:578`. It checks:

- identical method order, status, reason, waterfalls, incidence, and
  estimability evidence;
- exact non-floating fields and floating results within `1e-12` relative and
  absolute tolerance;
- global, not date-local, resolution of a repeated/conflicting trade ID;
- exact continuous B01/B02 quantiles from global narrow observations rather
  than averages of shard quantiles;
- exact event, market, and day set unions;
- B03 qualification after all market buckets are united;
- B04 event-minute and trade-count union across market buckets.

Separate reducer fixtures exercise odd/even B01 medians, interpolated B02 p95,
and cross-bucket B04 event-minutes at
`tests/test_deep03_v3_bounded_reducers.py:85-283`.

Same-timestamp L1 ambiguity is fail-closed in both B01's usable two-sided ASOF
population and the complete receive-order interval population
(`tools/research/deep03_v3_methods.py:758-843`). Exact economically identical
duplicates remain legal; economically different equal-key rows stop the run.
Global trade identity remains in complete 32-way full-ID hash buckets before
any date/market reshuffle (`tools/research/deep03_v3_methods.py:2032-2160`).

## Row conservation and exclusion visibility

The final candidate added explicit fail-closed conservation gates rather than
relying only on SQL construction:

| Boundary | Enforced invariant |
| --- | --- |
| L1 source -> physical shards | Per-date partition rows, total partition rows, and finalized manifest rows equal exact Sports L1 source rows (`deep03_v3_methods.py:1814-1866`) |
| Trade source -> full-ID shards | Partition and manifest rows equal source rows minus explicitly counted null trade IDs (`:1968-2029`) |
| Global trade dedup -> date/market shards | Partition and manifest rows equal the global dedup manifest (`:2211-2241`) |
| B01 observations | Payload rows equal the sum of the exact three horizon metrics; missing or extra horizons fail (`:2520-2540`) |
| B04 observations | Payload rows equal active market-minute metrics (`:2710-2727`) |

Conservation receipts are durable stages and are revalidated on a fresh-ARM
resume. Tests deliberately corrupt source counts and observation metrics and
prove refusal before reuse at
`tests/test_deep03_v3_bounded_reducers.py:687-774`.

The existing trade-QC profile still exposes non-null IDs, duplicates,
conflicts, raw rows, repeated rows, and eligible duplicate rows. L1 interval
receipts expose right censoring, gap starts, invalid books, positive intervals,
and one-/two-sided exposure. These are exclusions, not silently discarded
research conclusions.

## Checkpoint, resume, and failure behavior

The shared checkpoint namespace binds the exact immutable source set rather
than run ID or ARM (`deep03_v3_runner.py:60-97`). The checkpoint ABI binds the
pinned DuckDB version, complete methods-module SHA, hash algorithms and bucket
counts, horizons, TTL, book-age limit, and output schemas
(`deep03_v3_methods.py:851-875`).

A reusable partition requires canonical receipt bytes, exact source/ABI/stage
binding, payload SHA-256, size, schema, and row count. Payload and directory
fsync precede atomic COMPLETE receipt publication. A single nonblocking writer
lock fences concurrent publishers (`deep03_v3_methods.py:163-576`). A failed
run directory is immutable and cannot be retried; a new authority/ARM may reuse
only reverified dataset-scoped checkpoints. The resume fixture proves identical
results and complete partition reuse at
`tests/test_deep03_v3_bounded_reducers.py:644-685`.

The HTML report no longer labels prose/receipt descriptions as executable
"Exact queries". It labels them `Execution queries / receipt references`
(`deep03_v3_runner.py:567`), while exact source objects and VersionIds remain in
the report/object ledger.

## Resource and disk assessment

For the exact input manifest, the runner's disk gate includes only exact Sports
L1 and trade facts:

- Sports L1: `2,254,583,707` bytes;
- Sports trades: `1,459,924,666` bytes;
- exact checkpoint source basis: `3,714,508,373` bytes;
- four-times checkpoint envelope plus 8 GiB reserve:
  `23,447,968,084` bytes, approximately `21.84 GiB`.

That initial requirement is below the last read-only W09 inventory of roughly
`62 GB` free. Therefore disk preflight is not an initial-run blocker. The
runner still recomputes the gate immediately before computation and refuses
before checkpoint writes if the current free-space fact differs
(`deep03_v3_runner.py:184-260`).

The runtime remains sequential at 32 market buckets, `16GB / 2 threads`, under
the 40G/52G systemd soft/hard limits. No claim is made yet about measured
full-data peak RSS, DuckDB peak buffer memory, spill high-water, checkpoint
bytes, or completion time; those are outputs of the first authorized run.

## Verification run

At integration commit `0360019...`, the following selected release suites
passed `133/133` with no exclusions:

- `tests/test_deep03_v3_bounded.py`;
- `tests/test_deep03_v3_bounded_reducers.py`;
- `tests/test_deep03_v3_runner_bounded.py`;
- `tests/test_deep03_v3_open_discovery.py`;
- `tests/test_deep03_v3_w1_preflight.py`;
- `tests/test_deep03_one_shot_arm.py`;
- `tests/test_w09_exploratory_autoresearch.py`;
- `tests/test_w09_bringup.py`.

`git diff --check` passed and the worktree was clean. All four deployment SHA
manifests passed `shasum -a 256 -c`.

No AWS mutation, W09 start, deployment, S3 write, AUTHORITY generation, ARM
generation, or research execution occurred during this audit.

## Explicit residual blockers and limitations

These do not block drafting the release or attempting one fail-closed
operator-authorized exploratory run. They do block stronger claims:

1. **Full-scale acceptance pending.** No `.08` full eight-release run exists.
   `RUN_COMPLETE.json`, result tables, charts, report, peak memory/scratch, and
   elapsed-time evidence are absent. Research completion must not be claimed.
2. **Real-subset cross-engine acceptance pending.** Synthetic global-vs-bounded
   equivalence is strong, but a deterministic real-data subset that fits both
   engines has not been executed. Until it is, the output remains exploratory
   and cannot be promoted to strict/confirmatory evidence.
3. **Source-scan profiling pending.** Code structure scatters each fact source
   a constant number of times before bucket loops, but DuckDB JSON profiles for
   bucket counts 1/8/32 have not been archived. This is a scale-efficiency
   acceptance item, not evidence that facts were multiplied.
4. **Physical-byte determinism is incomplete.** An independent diagnostic with
   reversed object order produced equal numerical summaries but different
   Parquet/stage-manifest hashes for some downstream stages. The authorized
   input order is exact and fixed and a COMPLETE namespace is immutable, so
   this does not change the one-shot estimands; it prevents a stronger claim
   that independent physical rebuilds are byte-identical.
5. **Conservative resume disk credit.** Verified checkpoint bytes are measured
   but credited as zero in preflight (`deep03_v3_runner.py:223-249`). This is
   safe for correctness and the initial run fits, but a later fresh-ARM resume
   can conservatively refuse if partial checkpoints reduce free space below
   the full initial envelope.
6. **Additional hardening remains engineering debt.** Separate explicit
   expected-output conservation receipts for the L1-interval and global
   trade-dedup transforms, and rejection of every possible inner-stage
   symlink/TOCTOU substitution, would improve defense in depth. Current
   protection consists of exact synthetic equivalence, trade/L1 ambiguity QC,
   payload hash/schema/row validation, source/ABI binding, the single-writer
   fence, canonical W09 paths, and the systemd filesystem/resource sandbox.
7. **Economic scope remains deliberately closed.** `SEALED_DEGRADED_EVIDENCE`,
   B01-B04 descriptive results, RFQ OFF, absent L2 mechanism tests, and absent
   fee/fill/terminal inference cannot support a monetizable-strategy or PnL
   conclusion.

## One-shot release conditions

Before any execution, a separately generated `.08` release/authority chain
must bind this exact audit SHA, runtime commit, eight release IDs, evidence
tier, W09 instance type, write roots, maximum runtime, remaining cumulative
cost cap, operator text, and a fresh single-use ARM. Installation must use a
clean source worktree exactly at `b0d56eb...` and a clean release worktree that
contains the later immutable audit/release metadata.

Success requires all of the following from the same run:

- authority and ARM gate pass;
- exact input/VersionId prepare receipts;
- disk preflight pass;
- every checkpoint and conservation stage COMPLETE;
- all four declared methods closed honestly, including NOT_ESTIMABLE where
  required;
- artifact hashes, `RESULTS.json`, HTML report, and final `RUN_COMPLETE.json`;
- zero RFQ reads, zero production mutations, zero order actions, and no
  candidate/profit claim.

## Final conclusion

No unresolved P0 code or packaging defect was found after the conservation,
runtime-binding, report-label, and payload-pin repairs. The exact `.08` runtime
is suitable for release drafting and one newly authorized exploratory W09
one-shot. The research itself is **not complete** until the real eight-release
run finishes and its full-scale receipts are independently checked. Any report
from that run is a descriptive hypothesis-generation artifact, not proof of a
monetizable strategy.
