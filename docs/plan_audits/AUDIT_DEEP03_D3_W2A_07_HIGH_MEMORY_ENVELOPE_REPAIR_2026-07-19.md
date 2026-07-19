# DEEP03 D3-W2A `.07` high-memory envelope repair — independent release audit

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
- Method scope: the same partial open-discovery B01/B02/B03/B04 scope.
- Statistical semantics: unchanged from exact `.06` runtime commit
  `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`.

The eight exact input releases remain:

1. `2026-07-10__v3ref__seal-86c01ed0__pub-45f5b99ff4fdf580`
2. `2026-07-11__v3ref__seal-de2e77c8__pub-e038f638b307da69`
3. `2026-07-12__v3ref__seal-bc37de4c__pub-abd451660aeedf0c`
4. `2026-07-13__v3ref__seal-7f6e5c1b__pub-55f66e41dba1484d`
5. `2026-07-14__v3ref__seal-2d6a4941__pub-748d8d77f665d60a`
6. `2026-07-15__v3ref__seal-22aa1b04__pub-4ae53f539c340052`
7. `2026-07-16__v3ref__seal-60b2674e__pub-fd365b5cd76099a0`
8. `2026-07-17__v3ref__seal-e25887ab__pub-74f48fcbc81a41b7`

This document audits only a physical resource-envelope change after four
terminal attempts. It does not add a method, change an estimand, claim
candidate profitability, promote a strategy, open a holdout, enable RFQ,
authorize an order, or confer permission to execute research.

## Prior terminal chain

The `.06` repair audit, SHA-256
`fd1daf3b0d5324ac74aa5a45cc623a593b8611040221411f7c28da646cf1daf0`,
records the complete `.03` through `.05` evidence chain. In summary:

1. `.03` failed during the original global L1 materialization with DuckDB at
   `29.8 GiB / 29.8 GiB`; no report was generated and its ARM was consumed.
2. `.04` narrowed L1 columns and used DuckDB `16GB / 2 threads`, but still
   failed with DuckDB at `14.9 GiB / 14.9 GiB`; no report was generated and
   its ARM was consumed.
3. `.05` completed all eight per-date L1 materializations and then directly
   proved its terminal OOM occurred inside the global `trades_dedup`
   statement; no report was generated and its ARM was consumed.
4. `.06` preserved the research/statistical semantics and replaced only the
   physical trade-dedup plan. That repair completed L1, global trade-ID QC,
   the unique-ID stream, duplicate-candidate QC, and all 32 complete-ID hash
   buckets before a later DuckDB OOM; no report was generated and its ARM was
   consumed.

No authority or ARM from `.03`, `.04`, `.05`, or `.06` is reusable for `.07`.

## Immutable `.06` failure evidence

The following local snapshots were inspected read-only under
`/Users/ritcardo/HFT-BOT-deep03-trades-fix/work/deep03_failure_06/`:

| Snapshot | SHA-256 |
| --- | --- |
| `08-research-run.log` | `45ec065ef6129fc2904ccbfac435a030ff964a837de5539107dfbc716e9a7526` |
| `RUN_FAILED.json` | `7785d94feb91c96f5fb51eb17454a3d7ea2d6e2cf3f9a24b479d63556b01b4b6` |
| `ONE_SHOT_CONSUMED.json` | `220bbb4eddeed05a56964ea137835988d29002ee3f2fdef45faaa9545fde3153` |
| `INBOX_STATUS_TERMINAL.json` | `ccdc11ef753fd4ccf6c88c410880811744f650d318642caac8176cb2fd98815e` |

These receipts establish:

- release: `D3-W2A-2026-07-18.06`;
- runtime commit:
  `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`;
- Research Inbox job:
  `RJOB-20260719T033359899338Z-ded84065dec1`;
- W09 start time: `2026-07-19T03:36:47Z`;
- terminal failure time: `2026-07-19T03:43:57Z`;
- exception: DuckDB `OutOfMemoryException`, unable to allocate a `256 KiB`
  block with `14.9 GiB / 14.9 GiB` used;
- observed systemd memory peak: `42,951,962,624` bytes;
- AUTHORITY SHA-256:
  `2f3d1dcf99fc9e28b289351f336d540a38960e8f755ab69b5b9d41da3faeb20d`;
- ARM SHA-256:
  `3ea7dff9327fa3445c54f2a9cd9a3b6741b077b06f833427be73b2d93e2a696b`;
- systemd invocation ID: `16b4fb435c2d4a7bb4632cd76dddbcad`;
- one-shot state: `CONSUMED`, exit status `2`;
- `RUN_COMPLETE.json`: absent;
- report: not generated.

The persistent `.06` research log directly records, in order:

1. `START` and `COMPLETE` for every L1 date from `2026-07-10` through
   `2026-07-17`;
2. `trade_id_qc state=START` and `state=COMPLETE`;
3. the duplicate profile:
   `raw_rows=29,982,274`, `non_null_id_count=29,959,253`,
   `duplicate_id_count=23,021`, `conflicting_id_count=12`,
   `duplicate_raw_rows=46,042`, and
   `eligible_duplicate_raw_rows=46,018`;
4. the unique-ID stream and global duplicate-candidate relation completing;
5. ambiguity count `0` for the full winner-key output gate;
6. `START` and `COMPLETE` for every bucket `00/32` through `31/32`;
7. `trades_dedup state=COMPLETE`;
8. the DuckDB OOM immediately afterward, without any later stage marker.

This directly proves that `.06` fixed the stage it targeted. It also rules out
an incomplete L1 or trade-dedup materialization as the terminal `.06` stage.

## Exact failure attribution boundary

The `.06` log has no B01 `START` marker. It therefore does **not** directly
prove that B01 began or that a particular B01 DuckDB operator failed.

At exact `.06` method bytes, `setup_database()` creates only the lazy
`trades_clean` view after the flushed `trades_dedup state=COMPLETE` marker and
then returns. `execute_all()` invokes `run_b01()` first, and the first blocking
B01 statement is the `b01_pre` ASOF materialization at
`tools/research/deep03_v3_methods.py:814`.

Consequently, line 814 is the **highest-confidence control-flow attribution**
for the `.06` OOM. It is not a log stage marker and is not direct proof. The
exact DuckDB operator remains unknown; plausible operators include ASOF join
state, sort, or output materialization. `.07` is therefore a broad memory-
capacity repair for the unchanged downstream workload, not a claim that a
specific B01 operator was conclusively diagnosed.

## Exact `.07` repair runtime

The exact audited runtime commit is:

`768c3017164f141b99e5062008386b9d1bb0d2bf`

Its parent is exact `.06` methods/runtime commit:

`8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`

This audit and the W0/W1 metadata are committed separately. Their metadata
commit must not be substituted for the exact runtime commit above.

The `.07` change is limited to the deployment/resource envelope and release
bindings:

| Contract | `.07` value |
| --- | --- |
| Fixed instance ID | `i-0e53d134dceffe166` |
| Fixed instance type | `x8g.4xlarge` |
| Architecture / region | Ubuntu 24.04 arm64 / `us-east-2` |
| Physical memory | `256 GiB` |
| DuckDB memory / threads | `128GB / 2` |
| systemd memory controls | `MemoryHigh=192G`, `MemoryMax=224G` |
| Effective running rate | `$1.60108/hour` |
| Proposed maximum runtime | `28,800` seconds (`8` hours) |
| Proposed spending cap | `$15.00` |
| Maximum-runtime cost at fixed rate | `$12.80864` |

The installer fails closed unless IMDS reports exact instance type
`x8g.4xlarge` and `/proc/meminfo` reports at least `251,658,240 KiB`. The
authority gate binds the instance type in both AUTHORITY and ARM. The proposed
`28,800`-second maximum costs `28,800 / 3,600 × $1.60108 = $12.80864`, below
the `$15` cap; `$12.80863` is rejected by the tested gate.

These runtime and cost values are audited bounds only. This document does not
create, resize, start, stop, inspect, or connect to an EC2 instance and does
not approve spending.

## Statistical and code identity proof

The diff from `.06` runtime commit `8fb7a80...` to `.07` runtime commit
`768c301...` changes only W09 deployment documentation, cost/envelope gates,
release identifiers, payload hashes, and their tests. It does not change
either research implementation file:

| File | SHA-256 at `.06` | SHA-256 at `.07` | Result |
| --- | --- | --- | --- |
| `tools/research/deep03_v3_methods.py` | `73dfc0893bcadb113797b8a775083adb2a2b8b9b880ef4ec41d17567fd755635` | `73dfc0893bcadb113797b8a775083adb2a2b8b9b880ef4ec41d17567fd755635` | exact byte identity |
| `tools/research/deep03_v3_runner.py` | `4c3aa3cc081689f358ca99ea24c78a621d65d862d6d9ed99d771607ce7463aa0` | `4c3aa3cc081689f358ca99ea24c78a621d65d862d6d9ed99d771607ce7463aa0` | exact byte identity |

The two-thread setting is intentionally unchanged. The memory limit increase
does not change the selected releases, relation definitions, estimands,
filters, dedup winner definition, method ordering, output schemas, evidence
labels, or report semantics.

## Verification performed

At exact runtime commit
`768c3017164f141b99e5062008386b9d1bb0d2bf`:

- five focused suites collected and passed `107` tests:
  `test_deep03_one_shot_arm.py`,
  `test_deep03_v3_open_discovery.py`,
  `test_deep03_v3_w1_preflight.py`,
  `test_w09_exploratory_autoresearch.py`, and
  `test_w09_bringup.py`;
- deployment tests bind `x8g.4xlarge`, the minimum physical-memory check,
  DuckDB `128GB / 2 threads`, systemd `192G / 224G`, the `.07` release IDs,
  and the effective `$1.60108/hour` cost gate;
- cost-gate tests reject `$12.80863` for `28,800` seconds and accept a `$15`
  cap with an exact required cost of `$12.80864`;
- `deploy/w09/deep03_open_discovery_modules.sha256` SHA-256 is
  `46bc852222d8cabb6948a33ceeef4c4e1f9859ee2cd188b366fcdf0a4cc90605`
  and passed `sha256sum -c`;
- `deploy/w09/exploratory_autoresearch_payload.sha256` SHA-256 is
  `ccb78c62256bb1e700149412def123f2d067751479640d46a8e03b023c4a17ab`
  and passed `sha256sum -c`;
- the method and runner SHA values exactly equal the bytes at parent
  `8fb7a80a7d7ae4558a883a1f71502b057a9c4f87`;
- the exact runtime diff passed `git diff --check`.

## Explicit remaining risks

1. `.07` has not run over the real eight-release workload. No full-scale
   completion, result bundle, chart, or report exists for `.07`.
2. `128GB` is a limit, not proof of sufficient memory. The unchanged B01 ASOF
   join and later B01/B02/B03/B04 stages can still exceed the new envelope.
3. Raising the DuckDB limit may shift pressure to system memory or scratch.
   The `192G / 224G` systemd controls cap the process but cannot guarantee
   completion.
4. The 300 GB gp3 volume remains finite. A spill-heavy run can fail for disk
   space or I/O before reaching the time cap.
5. The fixed effective hourly rate is a release cost contract, not an AWS
   invoice guarantee. Any actual instance/type/region drift must fail closed.
6. All inputs are prior-exposed `SEALED_DEGRADED_EVIDENCE`; results remain
   exploratory only and cannot satisfy strict acceptance.
7. A terminal failure produces no valid report. Partial scratch state must not
   be published or resumed, and a consumed ARM must never be reused.
8. `deploy/w09/acceptance_on_host.sh:91` still prints the prior compute-only
   rate `$0.4713/hour`. The `.07` exploratory autoresearch service does not
   invoke that strict-acceptance script, while the authority gate and cost
   contract use `$1.60108/hour`; the stale informational echo is therefore a
   non-blocking residual, not the `.07` budget authority. It should be cleaned
   up in a later runtime release rather than changing exact commit `768c301...`.
9. Exact instance type and minimum `MemTotal` are queried fail-closed by
   `install_on_host.sh`, but the systemd service does not query IMDS and
   `/proc/meminfo` again on every start. The proposed same-window sequence is
   resize, install, gate verification, then one start; any delay or instance
   mutation between installation and start requires a fresh explicit
   identity/type/memory check and must otherwise fail closed.

These risks are not permission to change threads, methods, dates, releases,
evidence tier, RFQ scope, runtime, spending, or retry behavior.

## Explicit blockers before any `.07` execution

1. Persist this audit and record its exact SHA-256.
2. Draft exact `.07` W0 and W1 release candidates bound to this audit, runtime
   commit `768c3017164f141b99e5062008386b9d1bb0d2bf`, the same eight releases,
   `2,657` objects, `29,473,216,651` bytes, the same evidence tier, and RFQ
   `OFF`.
3. Bind W1 `.07` to the exact W0 `.07` SHA. A mechanically rebound
   `W1_COMPLETE.json` may change only its audit SHA and the W0/W1 IDs and
   hashes. It must preserve completed canaries, DQ, input projection,
   completion time, artifact hashes, and the fact that no research ran during
   preflight.
4. Obtain a new explicit operator authorization naming exact release
   `D3-W2A-2026-07-18.07`, exact runtime commit
   `768c3017164f141b99e5062008386b9d1bb0d2bf`, exact `x8g.4xlarge` envelope,
   the same inputs/mode, RFQ `OFF`, and separately approved maximum runtime and
   spending cap. This audit is not that authorization.
5. Only after that authorization, generate a new exact AUTHORITY and a new
   one-shot ARM. No earlier authority artifact may be edited or reused.
6. Before computation, independently verify actual W09 identity/type/memory,
   the installed exact runtime, both SHA-manifest layers, plan, audit, W0, W1,
   W1 completion, AUTHORITY, ARM, and all prerequisite hashes. Any mismatch
   must fail closed before the one-shot claim.
7. Permit at most the one execution explicitly authorized by the operator.
   There is no automatic retry. Any terminal outcome consumes the ARM.
8. Claim a report only after a valid sealed `RUN_COMPLETE.json`, complete
   artifact hash list, result bundle, and rendered report exist.

## Audit conclusion

`PASS_WITH_EXPLICIT_BLOCKERS` means `.07` release metadata may be drafted and
independently checked around exact runtime commit
`768c3017164f141b99e5062008386b9d1bb0d2bf`. It does not mean the high-memory
repair has completed at real scale and does not authorize W09 execution.

D3-W2A `.07` remains `NO-GO` until a new explicit `.07` operator authorization
and newly generated exact AUTHORITY/ARM satisfy every blocker above. Research
execution conferred by this audit is `NO`.

This audit did not connect to W09, modify W09, generate an AUTHORITY or ARM,
start research, read RFQ, open a holdout, write S3, mutate production, access
trading credentials, send messages, or place orders.
