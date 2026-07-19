# Fresh RFQ daily pipeline repair

Status: local implementation complete; production deployment blocked on an
independent audit; AWS reads/writes performed by this repair: **0 / 0**.

## What this repair closes

- The hourly unit now invokes the durable date coordinator, not the old
  eligibility-only 90-day scanner.
- Every date from strict T0 has an individual atomic state file. Nothing ages
  out after 90 days, and one bad date cannot block another date.
- `ELIGIBLE.json` automatically feeds the one-object-at-a-time overlay builder
  after the immutable RFQ-free V3 base terminal is present.
- Complete session, health, 24+2 clock/presence, and close-inventory gates run
  before any large local hash or S3 exact GET.
- The session ledger is streamed with a hard per-line ceiling and no permanent
  whole-ledger 64 MiB limit.
- RFQ and L1/L2 bodies are consumed one exact object at a time. Counts, largest
  object, total input bytes, and two-copy ephemeral scratch capacity are hard
  bounded before opening the first body.
- Request and universe derivations have exact content-addressed checkpoints.
  The exact reader has a SHA/size-bound disk cache across failures, removed
  after an overlay commits.
- Alerts are scoped to the analysis date's `[D 00:00, D+1 02:00)` window. A
  later alert remains recorded but cannot permanently contaminate an old day.
- The installer requires a separate clean, root-owned, non-writable runtime at
  `/opt/kalshi-fresh-rfq-daily`, exact-commit equal to the clean reviewed
  source. It explicitly refuses the existing base runtime path.

## Honest delivery boundary

The pipeline does not claim that local eligibility or a local overlay is a
published research channel. With the IAM delta still external, the durable
state is:

```text
WAITING_IAM
aws_write_state=NOT_AUTHORIZED
resume_stage=IAM
```

The queue entry remains resumable. After an independently validated IAM
capability is installed, an isolated tag/publish adapter must return a strict
proof containing both exact-version tag readback and exact overlay-MANIFEST
verification. Without the adapter/credentials the state is explicitly
`WAITING_PUBLISHER_CREDENTIALS`; only the strict proof advances it to
`PUBLISHED`.

No base MANIFEST, base terminal receipt, raw object, capture service, or W09
state is mutated by the local overlay stages.
