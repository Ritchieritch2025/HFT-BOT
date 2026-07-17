# W-PUB-REF-01A Read-Only Shadow Canary

**Decision:** STOP AT GATE — canonical control coverage is incomplete

**Canary host:** production EC2 `i-0fd427becf740a06b` (`3.130.232.109`)

**Canary date:** `2026-07-13`

**Checked at:** `2026-07-17T00:21:57Z`

**Implementation commit:** `596ada18c483c1e7837c3fbc5ef5afe1d1598c54`

**Active production commit:** `e287778d30b56170f427d2b03eb17ee31e61a5ec`

## Scope

This was the first production-isolated canary for the receipt shadow. It ran
from a detached worktree at:

```text
/home/ubuntu/w-pub-ref-01a-canary-596ada1
```

It did not switch or edit the active production checkout at
`/home/ubuntu/hft-bot`. The canary was limited to:

1. local sealed-day inventory planning; and
2. S3 `HeadObject` metadata checks.

It did not perform exact-version content downloads, S3 writes, tag changes,
prune changes, service changes, IAM changes, lifecycle changes, or any W09
operation.

## Frozen implementation

```text
tools/canonical_receipts.py
  sha256 5a6c6e15779230908e472e0ea1d8aaea161dc62883501619031344e96faa6875

tests/test_canonical_receipts.py
  sha256 3e9390e973610b7987627344437d921e2bab72f4e7aee1d382cf8e95cb8ce65c
```

Pre-canary validation was PASS: 33 dedicated tests, existing bridge/RFQ/
pipeline contract suites, `py_compile`, `git diff --check`, and `make gate`.
An independent frozen-diff audit also returned PASS for a local read-only
canary.

## Inventory plan result

The local plan completed successfully:

```text
state                       INVENTORY_PLANNED
objects                     673
bytes                       79,048,121,273
durability objects          673
research-candidate objects  329
s3 writes                   0
tag writes                  0
prune changes               0
```

All nine local families were `PRESENT_VERIFIED`:

| Family | Observed / expected |
|---|---:|
| raw durability | 342 / 342 |
| facts | 317 / 317 |
| seal | 1 / 1 |
| manifest date projection | 1 / 1 |
| dated dim snapshot | 3 / 3 |
| catalog at cutoff | 3 / 3 required |
| corrections at cutoff | 2 / 2 |
| capture gap receipt | 1 / 1 |
| L2 quality | 1 / 1 |

RFQ was not excluded. The inventory contained 122 sealed RFQ raw objects and
24 RFQ receipt objects. They remained durability-only and retained the
`FORBIDDEN_RFQ_DEFAULT` exposure policy.

## Metadata-only result

The complete 673-object metadata pass returned:

```text
state                         BLOCKED_INTEGRITY
exit code                     2
failures                      10
objects without metadata failure  663
content verification complete false
receipt emitted               no
authority                     SHADOW_LOCAL_ONLY
s3 writes                     0
tag writes                    0
prune changes                 0
```

The local output directory contained only `SHADOW_STATUS.json`; it contained
no `receipt-*.json` file.

### Missing canonical objects (4)

| Family | Key |
|---|---|
| capture gap receipt | `ec2/control/quality/v1/date=2026-07-13/capture_gap_receipt.json` |
| L2 quality | `ec2/control/quality/v1/date=2026-07-13/l2_gaps.json` |
| corrections | `ec2/warehouse/corrections/date=2026-07-13/late_rows.ndjson` |
| corrections ledger | `ec2/warehouse/corrections/ledger.ndjson` |

Each returned `404 Not Found` from `HeadObject`.

### Mutable-current size mismatches (6)

| Object | S3 current size | Frozen local size |
|---|---:|---:|
| catalog events | 16,802,300 | 16,802,135 |
| catalog markets | 94,864,223 | 97,039,223 |
| catalog series | 1,023,693 | 1,023,989 |
| catalog series classified | 512,302 | 509,019 |
| catalog settlements | 1,690,221 | 1,762,807 |
| warehouse manifest | 373,595 | 531,687 |

The current catalog and manifest keys are mutable. Their S3 HEAD versions did
not bind the same bytes as the local publication-cutoff snapshot, so the tool
correctly refused to issue a receipt.

### Plan deviation exposed by the canary

The plan calls for date-scoped/frozen auxiliary snapshots. The current shadow
prototype instead maps the live `warehouse/catalog/*`, the complete live
`warehouse/manifest.csv`, and the complete live corrections ledger while
labelling them `CURRENT_AT_CUTOFF`. The implementation does not resolve a
historical cutoff VersionId for those mutable keys. The label therefore does
not create the historical binding required by the plan.

The seal-bound manifest date projection did pass local validation. The
manifest failure is consequently evidence that the complete mutable container
drifted, not evidence that the sealed 2026-07-13 projection is corrupt. The
receipt checker behaved correctly by failing closed, but this mapping must not
advance unchanged.

Example current S3 metadata recorded at `2026-07-17T00:29:51Z`:

```text
seal VersionId     MHe.j.LsM0odxwSRdWrGjTX5rD6BwciK
catalog VersionId  n55yNAyjAryKVoZfmj6SG0m9IFmjYf7V
manifest VersionId Doukd7qT51faHiwpDx2m.0e8xbfLrU..
```

The canonical receipt prefix remained empty after the canary.

## Production-isolation evidence

Independent snapshots at `2026-07-17T00:22:02Z` and `00:22:44Z` showed:

- `kalshi-pipeline.service` remained `active`;
- production HEAD remained
  `e287778d30b56170f427d2b03eb17ee31e61a5ec`;
- `pipeline_supervisor`, `rfq_capture`, and `ingest` PIDs did not change;
- firehose grew by 16,113,664 bytes;
- L2 grew by 14,884,864 bytes; and
- RFQ grew by 23,002,763 bytes and then rotated normally.

This is evidence that the canary remained outside the production capture hot
path.

## Permission gates still open

The production credential is able to perform object metadata reads, but both
of these required preflight checks returned `AccessDenied`:

```text
s3:GetBucketVersioning
s3:GetLifecycleConfiguration
```

Historical version discovery was not attempted. The canary has no evidence
that lifecycle preserves every referenced version, so it cannot advance to a
v3 reference cutover.

## Stop decision and smallest next phase

Do not run full-byte verification, tagger work, prune dry-run, v3 publication,
or W09 IAM work from this result.

The next separately audited phase must first close the canonical publication
contract:

1. locally build an immutable, date-scoped auxiliary bundle containing the
   date-D manifest projection, a defined catalog cutoff snapshot, date-D late
   rows, a date-filtered ledger projection, and both quality receipts;
2. change the inventory to reference those date-scoped objects instead of the
   live manifest, live catalog, and complete live ledger;
3. add regression tests proving that D+1 manifest/catalog/ledger changes cannot
   change date D's desired inventory;
4. independently audit that local change before authorizing one canonical
   backfill;
5. grant a read-only inspector enough permission to prove bucket versioning
   and lifecycle state; and
6. rerun this same metadata-only shadow until the desired set and canonical
   set match exactly, then separately authorize exact-VersionId SHA checks.

Waiting for a periodic sync is not a sufficient contract for mutable catalog
and manifest keys. The uploader and receipt builder must bind one frozen byte
set atomically enough that a later update cannot change what the receipt means.
