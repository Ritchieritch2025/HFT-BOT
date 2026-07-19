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
  and exchange/receive-clock-invalid creates are separately excluded and
  counted; no prefix hash is used for identity joins.
- Delete occurrences conserve through exact-duplicate collapse into orphan,
  excluded-parent, or valid-parent candidates. Valid-parent candidates then
  conserve into consistent, temporal/payload-inconsistent, or clock-censored
  classes. Request endpoints are mutually exclusive; an inconsistent or
  clock-invalid delete is never relabelled as ordinary no-delete censoring.
- Parquet payloads are reusable only behind fenced, canonical, immutable
  COMPLETE receipts and exact stage manifests. Partial payloads are not
  reusable.
- One run accepts at most 31 contiguous UTC dates. Each overlay JSON is capped
  at 64 MiB. DuckDB is pinned to two threads and 16 GiB. Request provenance
  has fixed one-million occurrence/unique-ID limits, a conservative 8 GiB
  projection envelope, and preserves 2 GiB of observable OS memory headroom.
  Disk is preflighted against source bytes times four, the peak exact object,
  and a 2 GiB reserve. Resume is only from a COMPLETE exact-date stage; partial
  dates are never reused.

## Research outputs

| Module | Result |
|---|---|
| D01 | Flow census by UTC hour, exact-dedup counts, excluded frame counts |
| D02 | Separate `contracts_fp` E2 and `target_cost_dollars` E6 distributions; no unit conversion or population merge |
| D03 | Consistent create/delete lifetimes in exact integer microseconds, mutually exclusive censor reasons, Kaplan–Meier survival checkpoints compared as `duration_us <= horizon_ms * 1000` |
| D04 | Combo share, leg-count distribution, observable yes/no/unknown leg pressure |
| D05 | Delete-joined requester SHA-256 concentration with known-ID coverage and censoring bounds |
| D06 | Direction/volume proxy only for combo legs whose side is actually present; no single-RFQ side inference |
| D07 | V2 exact RFQ→CLOB adapter bound to every mapping component, exact rebuilt base-manifest bytes, exact L1/L2 object identities, exact L2 quality-receipt bytes, fixed past/event/future clocks, and gap/clock state |
| D08 | Always blocked: passive RFQ frames do not observe quote acceptance, fills, fees, inventory, settlement, or realized PnL |

The D07 adapter must enumerate the exact mapping set. `run_bounded_fresh_rfq`
accepts optional `base_manifest_bytes_by_date` and
`l2_quality_receipts_by_date` exact-byte maps; D07 remains `BLOCKED` when
either proof is absent or when the exact quality receipt refuses the date.
Unmapped, date-boundary, clock-anomalous, and gap rows have mandatory,
mutually exclusive censor states. An observed row must bind the exact RFQ
exchange and receive clocks, use a pre observation in
`[event-pre,event]`, a post observation in `(event,event+post]`, nonempty L1
and L2 rows, and zero gaps. Counts may not exceed either the exact source rows
or the fixed per-component cap.

D07 reports two different populations. The unaligned market-response table
contains every observed mapped component and makes no direction inference.
The direction-aligned table contains only combo legs with an actually observed
`yes` or `no` side; single-market RFQs and unknown-side combo legs are never
assigned a direction.

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
8. incomplete or semantically inconsistent D07 adapter coverage;
9. READY/receipt/embedded-base identity drift or a byte-level exact V3
   manifest rebuild mismatch;
10. request-provenance memory/event bounds or checkpoint-disk headroom.

## Remaining integration work

- The full-scope runner must supply the production exact-VersionId client and
  separately audited W09 transport/IAM evidence.
- The L1/L2 module must emit the D07 v2 adapter document. This RFQ module does
  not rescan L1/L2 facts itself, but it independently rebuilds the exact base
  binding and exact L2 quality gate before accepting that adapter.
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

At this repair handoff, the focused module has 18 passing tests. The selected
fresh-RFQ authority, receipt, exact-reader, base-binding, universe, mapping,
request-provenance, W09-adapter, and bounded-analysis suite has 485 passing
tests. No AWS access was used by this repair.
