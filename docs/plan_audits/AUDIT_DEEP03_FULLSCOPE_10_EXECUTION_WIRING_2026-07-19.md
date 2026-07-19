# Independent adversarial audit — DEEP03 full-scope .10 execution wiring

**VERDICT: PASS_FOR_RELEASE_DRAFTING**

- Audited runtime commit: `61281b425a39c09b7057d128a9603fa10d6871a8`
- Audited delta: `47206b1..61281b4`, with repair delta `99f1a1e..61281b4`
- Auditor: `codex-independent-fullscope-10-auditor-20260719`
- Audit completed: `2026-07-19T18:36:15Z`
- Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`
- AWS/remote mutation: none; deployment and research start were not performed.

This verdict authorizes release drafting only. It does not itself create an
AUTHORITY/ARM, install artifacts on W09, start Research, include fresh RFQ, or
make a profitability claim.

## Predecessor failure and repair

The predecessor `99f1a1e34cc2f45adec8c5e8c1c4a461576cfe60` was independently
refused for release drafting:

1. `exploratory_autoresearch.main()` passed two unsupported L2 path keywords
   into `validate_claimed_authority()`, producing a deterministic `TypeError`
   after the systemd ARM claim.
2. Its subsequent `run_cycle()` call omitted those same two required paths.
3. Its completion consumer accepted digest-shaped strings without recomputing
   the report, L2, graph, execution-receipt and artifact-ledger bytes.

Commit `61281b4` changes exactly three paths: the W09 automation entry point,
its test, and its payload SHA manifest. The real CLI now routes the L2 paths
to `run_cycle()` rather than the authority API, and completion independently
rehashes every fixed artifact plus the complete artifact ledger. Replayed
attacks for missing report, tampered L2 receipt, forged SHA ledger, symlink,
metadata-only completion and nonzero RFQ reads were all refused. A coherent
positive full-scope fixture passed.

## Exact authority and one-shot scope

The gate accepts exactly these six methods and no others:

- `D3-B01-MARKOUT`: `PARTIAL_DESCRIPTIVE_ONLY`
- `D3-B02-ONESIDE`: `PARTIAL_DESCRIPTIVE_ONLY`
- `D3-B03-XMKT`: `NOT_ESTIMABLE_PREFLIGHT_ONLY`
- `D3-B04-RHYTHM`: `PARTIAL_DESCRIPTIVE_ONLY`
- `D3-FULL-MARKET-GRAPH-01`: `DESCRIPTIVE_ONLY_NO_PNL`
- `D3-FULL-L2-SNBD-01`: `DESCRIPTIVE_ONLY_NO_PNL`

Positive `.10` authority validation passed. The following attacks failed
closed before compute: old `.09` authority, old `.09` ARM, four-method narrow
scope, missing full-scope method, wrong method tier, extra RFQ method, and
missing L2 prerequisite hash. The fixed release is
`D3-W2A-2026-07-19.10`; an old ARM cannot be rebound to it.

## L2 audit v2 and exact source binding

The machine receipt uses schema `deep03-fullscope-l2-independent-audit-v2`
and binds:

- runtime commit `61281b425a39c09b7057d128a9603fa10d6871a8`;
- all four live full-scope module SHA-256 values;
- eight exact V3 release IDs for 2026-07-10 through 2026-07-17;
- six real L2 quality objects, including exact VersionId and SHA-256;
- source binding
  `c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979`;
- the exact bytes of this report; and
- an empty blocker list with claim tier `DESCRIPTIVE_ONLY_NO_PNL`.

The source binding was computed from the real W09 input twice: the old W1
manifest and the enriched prepared manifest produce the same value. Moving
the local cache, changing run ID, or changing authority metadata does not
change it. Mutating any source VersionId, object SHA, size, row count or
release evidence changes it and is refused. Runtime commit, live module,
quality-object, report-byte, file-mode and authority-prerequisite mutations
were also refused by their respective gates.

The prior L2 semantic audit is safely reused because the estimator blobs for
`deep03_v3_methods.py`, `deep03_fullscope_graph.py` and
`deep03_v3_l2.py` are byte-identical to audited commit `1fa7481`. That audit
assessed the same six real receipts and classified 07-12/07-15/07-17 clean,
07-13/07-14/07-16 excluded. Changes to `deep03_fullscope_runner.py` are
execution/audit wiring and were reviewed in this audit.

## W09 execution contract

The systemd order is fail closed:

1. verify the installed payload SHA manifest;
2. validate the exact `.10` authority and the two root-owned, mode `0444` L2
   audit artifacts;
3. only then claim the one-shot ARM;
4. call `exploratory_autoresearch.py`, which calls only
   `deep03_fullscope_runner.py` for research; and
5. consume the ARM on every exit.

The fixed runtime envelope is DuckDB `16GB`, two threads, 16 L2 market
buckets, 8 GiB reserve, `MemoryHigh=40G`, `MemoryMax=52G`, and a 24-hour
systemd timeout. The completion contract requires six exact methods,
full-scope schema, real artifact hashes and ledger, no failed/scratch state,
`rfq_reads=0`, `production_mutations=0`, and `order_actions=0`. No old narrow
runner or fresh-RFQ CLI is reachable from this service.

## Real W09 input and capacity evidence

Read-only W09 inspection at `2026-07-19T18:31:55Z` found:

- old W1 manifest SHA-256:
  `1c7ba5d28be9f674e72d19f30ecfae33dcefcd1a5ebe977219c267eead453ca5`;
- 2,657 objects, 29,473,216,651 bytes, eight exact releases;
- full rehash of all 2,657 local object paths: exit 0, zero missing and zero
  mismatches;
- prepared row authority: `sealed_fact_rows=728556781` and
  `fact_objects_without_row_count=0`;
- available disk: 206,276,960,256 bytes; and
- conservative full-scope requirement: 44,559,138,740 bytes, leaving
  161,717,821,516 bytes headroom.

The raw old W1 manifest lacks the two top-level row summaries, so it must
continue through the existing prepare enrichment. The deployed workflow does
so; the raw old manifest is not passed directly to the full-scope runner.

## Reuse boundary for RFQ

RFQ core modules and their tests are byte-identical to audited commit
`77416c0`. This run nevertheless keeps RFQ strictly OFF: fresh RFQ remains a
separate cohort, queue, authority and later overlay. Old damaged RFQ remains
outside this execution.

## Verification results

- Four SHA manifests: 29/29 entries verified.
- Focused exact-commit suite: 236 passed.
- Full exact-commit suite: 1,829 passed, 3 skipped, 1 xfailed, 0 failures or
  errors.
- Independent local focused replay of the narrower wiring set: 106 passed.
- Git diff check: clean; runtime worktree was clean at audit start.

The three skips are environment/fixture skips and the single xfail is a
pre-existing settlements-export placeholder; none touches this execution
wiring.

## Final decision

Known release blockers for the audited runtime are empty. The companion
canonical machine receipt may be bound into the `.10` AUTHORITY prerequisites
and installed as root-owned mode `0444`. Release metadata, AUTHORITY/ARM,
installation and the one-time start remain separate subsequent actions.
