# Fresh RFQ follow-up architecture

Status: repaired and tested locally; independent audit and deployment pending;
zero AWS writes.

## Fixed production epoch

- strict T0: `2026-07-20T00:00:00Z`
- generation: `fresh-rfq-20260720-01`
- authority SHA: `11faaf27e1f7b49689e77e6b58034d83ff23b94ed4d507291cfcff4deee37912`
- envelope logical SHA: `0ef4e0d52911cfdd0b10ffd09ee770e56e5e19f0370d5068951627d6a3eb561e`
- envelope file SHA: `2fa1caf792d540e0b7ce4641bb82161f09ce3926a81652a2006468a9649ceaf2`

## Independent pipelines

The base v3 coordinator, durable receipt, tagger-precommit publisher, and base
MANIFEST are restored to the original structural `RFQ OFF` implementation.
They never inspect an RFQ eligibility marker and cannot include RFQ objects.

The independent pipeline performs only S3 `HEAD`, exact-version `GET`, and
fully manual/paginated `ListObjectVersions`. It builds and atomically binds:

1. the exact production authority envelope;
2. full_v2 seal and 24 analysis + 2 watermark capture exact versions;
3. the complete D+1 `rfq_receipts_02.ndjson*` current-version inventory;
4. exact source evidence and all 26 canonical hour receipts;
5. the selected persistent-session ledger;
6. a mandatory fixed-path health receipt (there is no optional alert input);
7. `ELIGIBLE.json` as the final local commit marker.

Before the first large local hash or exact-version GET, the pipeline requires
the D+1 02:00 clock, the complete streamed 24+2 session-ledger set, one capture
session, the date-scoped capture-health observation, local 24+2 file presence,
and the fully paginated close-family inventory. The append-only session ledger
has a per-line bound but no whole-file 64 MiB lifetime limit.

The overlay builder runs only after an RFQ-free base terminal receipt exists.
It opens one exact RFQ/Parquet object at a time, applies object-count,
single-object, total-byte, and scratch-capacity bounds before the first body,
and never constructs an all-day body map. Request and L1/L2-universe stages
commit content-addressed body-free checkpoints. The exact reader also retains
a SHA/size-bound scratch object across retries, then the coordinator deletes
that private cache after the overlay commit. A later-stage failure therefore
does not force another tens-of-GB S3 download.

Every UTC date from strict T0 onward has its own atomic queue file. Dates never
expire after a 90-day scan window; a quarantined bad date does not head-block a
later healthy date. Retryable stages use bounded exponential retry. The latest
single capture alert is attributed only to the analysis window it intersects,
so an old alert cannot poison all future dates.

The pipeline states are explicit:

1. `PENDING_ELIGIBILITY`
2. `ELIGIBLE_WAITING_BASE`
3. `OVERLAY_READY`
4. `WAITING_IAM`
5. `WAITING_PUBLISHER_CREDENTIALS`
6. `PUBLISHED`

The current reviewed IAM delta remains external and unapplied. Therefore the
expected production state after local overlay completion is durable
`WAITING_IAM`, with `aws_write_state=NOT_AUTHORIZED`. It is never called
research-ready. Once an independently attested capability and the isolated
exact tag/publish adapter are supplied, the same queue entry resumes at
`TAG_AND_PUBLISH`; no eligibility or heavy overlay stage is repeated.

## Deployment boundary

`kalshi-fresh-rfq-daily-producer.service` and its hourly timer have no
`Requires=` dependency on capture, base research, or W09. The installer enables
the timer but deliberately does not start it. The installed service runs from
the independent root-owned clean checkout `/opt/kalshi-fresh-rfq-daily`; it
must not reuse `/opt/kalshi-research-v3`. The installer independently checks
both source and runtime cleanliness and exact commit equality.

The deployed coordinator has no AWS write transport and performs no AWS write.
The tag/publish boundary is an injected, separately credentialed adapter. An
IAM marker without that adapter remains
`WAITING_PUBLISHER_CREDENTIALS`; it cannot be mistaken for publication.

The IAM change set is a draft only. It documents the atomic replacement of the
old raw explicit-Deny statements, exact-version dual-tag permissions for the
dedicated tagger, and W09's dual-tag + application-manifest intersection. It
has not been applied to AWS.
