# Fresh RFQ follow-up architecture

Status: implemented and tested locally; not deployed; zero AWS writes.

## Fixed production epoch

- strict T0: `2026-07-19T00:00:00Z`
- generation: `fresh-rfq-20260719-01`
- authority SHA: `6540583799317c7f19a57f7d67c639afbd8832186fe55fd735e4f3cbb2cb9160`
- envelope logical SHA: `523543a651870b1611c8320957aa5ed0912da21b64a26ca0ee7bba1a8e47840b`
- envelope file SHA: `8de2bef22158879e706f7af3bdd8b0cea13ae7cda9cfd4a98a09ee709c830b9b`

## Independent pipelines

The base v3 coordinator, durable receipt, tagger-precommit publisher, and base
MANIFEST are restored to the original structural `RFQ OFF` implementation.
They never inspect an RFQ eligibility marker and cannot include RFQ objects.

The independent producer performs only S3 `HEAD`, exact-version `GET`, and
fully manual/paginated `ListObjectVersions`. It builds and atomically binds:

1. the exact production authority envelope;
2. full_v2 seal and 24 analysis + 2 watermark capture exact versions;
3. the complete D+1 `rfq_receipts_02.ndjson*` current-version inventory;
4. exact source evidence and all 26 canonical hour receipts;
5. the selected persistent-session ledger;
6. a mandatory fixed-path health receipt (there is no optional alert input);
7. `ELIGIBLE.json` as the final local commit marker.

The overlay builder runs only after an RFQ-free base terminal receipt exists.
It writes a separate body-free overlay receipt, local overlay MANIFEST, and
content-addressed cache. A tagger failure writes `RFQ_OVERLAY_OFF` only in the
overlay status. The base terminal bytes are read before and after the attempt
and must be identical. A later retry can attach a successful overlay to the
same immutable base binding without rebuilding base data.

## Deployment boundary

`kalshi-fresh-rfq-daily-producer.service` and its hourly timer have no
`Requires=` dependency on capture, base research, or W09. The installer enables
the timer but deliberately does not start it. The runner has no upload or tag
API and performs no AWS write.

The IAM change set is a draft only. It documents the atomic replacement of the
old raw explicit-Deny statements, exact-version dual-tag permissions for the
dedicated tagger, and W09's dual-tag + application-manifest intersection. It
has not been applied to AWS.
