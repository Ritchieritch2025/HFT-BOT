# Deep03 fresh-RFQ D01–D07 bounded research module

Status: implementation and offline adversarial tests complete. This document
does not authorize an AWS read, deployment, or research run.

## Scope

`tools/research/deep03_v3_rfq_bounded.py` consumes only the isolated
`W-RFQ-FRESH-01` overlay lane. It requires the full fresh-epoch authority and
revalidates its strict T0, generation, historical 284-object deny set,
`DATA_INTEGRITY_BLOCKED` old-lineage state, `repair_state=FORBIDDEN`, all 24
analysis hours, the two watermark hours, exact source resolution, subscription
continuity, source evidence, and the overlay digest chain before calling any
transport client.

The module never discovers an S3 key, resolves `latest`, reads `raw_rfq/*`,
repairs old RFQ bytes, publishes data, changes tags, or implements AWS writes.

The production capture generation at handoff is
`fresh-rfq-20260720-01`, with strict T0 `2026-07-20T00:00:00Z`.
Consequently 2026-07-19 is permanently ineligible and the earliest possible
analysis overlay is 2026-07-20 after its full 24 hours plus D+1 watermark are
complete. No date or generation is hard-coded in the implementation: the
validated authority and overlay provide them. The caller can additionally pin
the exact ordered date list with `expected_eligible_dates`; any mismatch stops
before the exact client is created.

## Bounded execution

- Each pinned RFQ object is opened through `ExactReadSession` by bucket, key,
  VersionId, size, and SHA-256.
- One ephemeral body is active at a time. The canonical request-provenance
  reader and a narrow lifecycle observer consume that same verified file before
  it is deleted.
- Narrow create/delete rows are partitioned by the full RFQ ID using
  `SHA256(id)[0:8]` interpreted as an unsigned big-endian integer modulo N.
- Global create deduplication and create/delete lifecycle joins run one hash
  bucket at a time. Exact duplicates collapse; conflicting create identities
  are excluded and counted; no prefix hash is used for identity joins.
- Parquet payloads are reusable only behind fenced, canonical, immutable
  COMPLETE receipts and exact stage manifests. Partial payloads are not
  reusable.
- One run accepts at most 31 contiguous UTC dates. Each overlay JSON is capped
  at 64 MiB. DuckDB is pinned to two threads and 16 GiB.

## Research outputs

| Module | Result |
|---|---|
| D01 | Flow census by UTC hour, exact-dedup counts, excluded frame counts |
| D02 | Separate `contracts_fp` E2 and `target_cost_dollars` E6 distributions; no unit conversion or population merge |
| D03 | Consistent create/delete lifetimes, explicit right censoring, Kaplan–Meier survival checkpoints |
| D04 | Combo share, leg-count distribution, observable yes/no/unknown leg pressure |
| D05 | Delete-joined requester SHA-256 concentration with known-ID coverage and censoring bounds |
| D06 | Direction/volume proxy only for combo legs whose side is actually present; no single-RFQ side inference |
| D07 | Exact RFQ→CLOB adapter contract bound to every mapping component, L1/L2 mapping SHA, gap state, and clock state |
| D08 | Always blocked: passive RFQ frames do not observe quote acceptance, fills, fees, inventory, settlement, or realized PnL |

The D07 adapter must enumerate the exact mapping set. Unmapped and
date-boundary rows have mandatory censor states. A mapped row may be observed
only with nonempty L1 and L2 evidence and zero gaps; otherwise it must be
gap- or clock-censored. Without a complete adapter set D07 reports `BLOCKED`
and emits no impact statistic.

## Conservation and fail-closed gates

The run stops on any of the following:

1. overlay/authority/hour/source-evidence/digest drift;
2. exact VersionId, response identity, size, or full-body SHA drift;
3. physical-line classifications not conserving against canonical request
   provenance;
4. fresh market mapping not exactly reproducing the overlay mapping;
5. hash-partition row counts not conserving;
6. global unique create IDs not equaling lifecycle rows plus conflicting IDs;
7. mapping component checkpoints not equaling embedded mapping totals;
8. incomplete or semantically inconsistent D07 adapter coverage.

## Remaining integration work

- The full-scope runner must supply the production exact-VersionId client and
  separately audited W09 transport/IAM evidence.
- The L1/L2 module must emit the D07 adapter document. This RFQ module does not
  rescan L1/L2 facts itself.
- The full-scope report renderer must turn the canonical JSON distributions
  into charts and preserve all blocked/censored labels.
- A real fresh-overlay date must exist before an RFQ empirical result can be
  produced. Historical damaged RFQ data remains unusable and is never a
  fallback.

## Verification

The focused suite includes end-to-end exact local transport, checkpoint resume
without a second read, exact duplicate collapse, consistent delete joining,
orphan-delete accounting, cross-ID same-bucket isolation, conflicting-ID
exclusion, combo side/ticker preservation, source-hour gap rejection, authority
and overlay tamper rejection, exact-body corruption rejection, and D07 exact
coverage/censor semantics.

At implementation handoff, the focused module has 8 passing tests and the
combined relevant Deep03/fresh-RFQ suite has 384 passing tests.
