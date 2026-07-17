# W-PUB-REF-01A — Zero-copy auxiliary descriptor implementation

**Date:** 2026-07-16  
**Result:** LOCAL IMPLEMENTATION PASS; AWS PERMISSION GATE REMAINS CLOSED  
**Branch:** `w-pub-ref-01a-aux-bundle`  
**Base commit:** `43fc039`

## Outcome

The local receipt-shadow implementation now represents the sealed research
inputs without copying facts, raw, or catalog bytes into a second research
dataset.

- Facts and sealed raw, including RFQ, remain exact canonical S3 references.
- Catalog objects remain exact historical canonical VersionId references;
  catalog bytes copied into the auxiliary set: **0**.
- The auxiliary set freezes only small date-scoped controls plus one small,
  exact-version v2 `MANIFEST.json` witness.
- RFQ remains in the durability inventory and remains
  `FORBIDDEN_RFQ_DEFAULT`; this work does not reopen the terminated RFQ repair
  branch.
- `plan` and `shadow` require an explicit `--aux-bundle`; there is no mutable
  `latest` selection.
- No uploader, tagger, pruner, W09 integration, timer, supervisor hook, or
  production hot-path change is included.

## Authenticated catalog cutoff

The catalog migration path is fail closed:

1. The source v2 manifest must be under
   `research/releases/<derived-release-id>/MANIFEST.json` in the same trusted
   canonical bucket.
2. Its release ID, date, seal digest, publication-state digest, per-object
   VersionId binding, and post-upload verification count are validated.
3. A stable evidence identity additionally covers generated time, corrections
   cutoff, the complete object inventory, VersionIds, size/SHA, and the
   verification count.
4. S3 version history must prove that the source manifest has exactly one
   non-null original version and no delete marker. Exact HEAD and exact-version
   GET then verify VersionId, S3 LastModified, size, and full SHA-256.
5. The authenticated S3 LastModified of that source manifest is the catalog
   cutoff. The manifest's self-reported generated time may not be later.
6. Each canonical catalog reference must be the version current at that
   cutoff. Truncated history, timestamp ambiguity, a delete marker, an older
   selected version, or a null VersionId blocks the run.
7. The canonical catalog allowlist must be bidirectionally equal to the v2
   catalog set, including optional objects, and every size/SHA must match.

This requires `ListObjectVersions`; IAM names that permission
`s3:ListBucketVersions`.

## Other fail-closed repairs

- Bundle controls are opened through a pinned directory fd with
  `openat`/`O_NOFOLLOW`. Every file is read once, and the same retained bytes
  drive size, SHA, and semantic validation. Final fd/path checks detect leaf,
  intermediate-directory, or post-read replacement.
- Corrections require exchange date D, a valid late-row fact timestamp in D,
  a safe raw source path, matching observation/source identity, valid offsets,
  `seal_untouched=true`, and bidirectionally equal per-source counts between
  late rows and the date ledger.
- Every shadow invocation first atomically replaces any old READY status with
  `SHADOW_RUNNING/complete=false`. A precheck failure writes a new
  `BLOCKED_INTEGRITY` status, so an earlier READY cannot remain current.

## Verification

- `tests/test_canonical_receipts.py`: **62 passed**.
- `tests/test_rfq_capture.py` + `tests/test_pipeline_contract.py`: PASS, with
  the existing expected xfail only.
- `tests/test_research_bridge.py`: all **19 phases PASS**.
- `make gate`: PASS.
- `py_compile`: PASS.
- `git diff --check`: PASS.

Frozen implementation hashes independently reviewed:

- `tools/canonical_receipts.py`  
  `036480fb739a5c0f1f4350db5c33d705af0b2a5f23025d8c7ac1b881a9da1ec9`
- `tests/test_canonical_receipts.py`  
  `d9d8c715027a837b19f20dc923229a5a7a6077e869dc239fea6f37f357dc5947`

Two independent final reviews returned PASS. The adversarial review first
found and reproduced four blockers, then re-reviewed the repaired frozen diff:
authenticated catalog trust/cutoff, bundle TOCTOU, corrections date/source
isolation, and stale READY handling all passed.

## Production and AWS state

No AWS mutation or W09 action was performed in this phase:

- S3 writes/copies/tags/deletes: **0**
- EC2 start/stop: **0**
- production checkout switch/deploy: **0**
- production timer/service/supervisor changes: **0**

The observed production checkout remains at
`e287778d30b56170f427d2b03eb17ee31e61a5ec`.

Existing 2026-07-13 v2 candidates recorded for the future read-only resolver:

- `2026-07-13__seal-7f6e5c1b__pub-be44d2be5e80e7fd` — RFQ excluded.
- `2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5` — sealed RFQ included.

These releases provide immutable byte evidence, but the canonical historical
VersionIds still must be resolved and verified. They have not been assumed.

## Closed permission gate and next step

The current `vaultWriter` principal could not prove:

- `s3:GetBucketVersioning`
- `s3:GetLifecycleConfiguration`
- `s3:ListBucketVersions`

The next step is therefore deliberately not a backfill. After a dedicated
read-only inspector receives those three permissions, run a metadata-only
resolver/canary that:

1. proves bucket versioning and records lifecycle state;
2. selects one explicit v2 source release;
3. resolves the unique source-manifest version and the canonical catalog
   versions current at its authenticated S3 cutoff;
4. verifies catalog/v2 set and byte equality; and
5. stops at a new independent audit gate.

If no matching canonical catalog version exists, the run must block. A v2
migration exception or a one-time small content-addressed catalog snapshot is
a separate design decision; neither is authorized here. No S3 write, full-byte
canary, receipt publication, tag, prune, W09 action, or automatic routing is
authorized by this report.
