# W-PUB-REF-01C AWS and production evidence

Checked at `2026-07-17T04:20:27Z` from the production host. No AWS write API,
IAM mutation, S3 mutation, instance-state change, or research compute was run.

- Active publisher identity: `arn:aws:iam::321572485933:user/vaultWriter`.
- `kalshi-vault-ritcardo` bucket versioning: `Enabled`.
- `GetBucketLifecycleConfiguration`: `NoSuchLifecycleConfiguration`.
  Therefore the bucket currently has no lifecycle rule that can expire or
  transition a referenced VersionId. This must be rechecked at cutover and by
  the daily policy-evidence patrol; it is not a permanent guarantee.
- Exact probe object:
  `ec2/warehouse/seals/date=2026-07-15.json`, VersionId
  `LzbTVM32lf4rkbs9n0GtpqD2_IDSydhD`.
- Exact `GetObjectTagging` with that VersionId: `AccessDenied` because
  `vaultWriter` has no identity-based allow for
  `s3:GetObjectVersionTagging`.

Cutover consequence: publisher IAM/tag-inspection remains a P0 external gate.
No v3 production manifest may be published and no copied-v2 cutover approval
file may be created until the permission is added to the actual publisher
identity (or the publisher is moved to an audited role) and the exact-version
positive/negative tests pass.

## Post-fix production-local shadow evidence

Checked at `2026-07-17T04:48Z` from detached worktree
`/home/ubuntu/w-pub-ref-01b-r2` at commit
`09880dd77eeb32f173d7463591c77e2c27dbc18e`. These commands made no AWS call,
did not stop a service, and wrote only the isolated auxiliary bundle.

- Date `2026-07-14` forward freeze: `FORWARD_AUXILIARY_SET_FROZEN`;
  auxiliary set
  `b6dccebe4665b47defd20555ab9a4ba2e03146a0b6b51573f5adb55bf4788580`;
  `catalog_bytes_copied=0`; `s3_writes=0`; elapsed 9.42s; maximum RSS
  34,692 KiB; swap 0.
- Forward inventory plan: `FORWARD_INVENTORY_PLANNED`; 577 durability objects,
  61,197,144,096 bytes, 337 research candidates; `s3_writes=0`; elapsed 1.75s;
  maximum RSS 30,644 KiB; swap 0.
- The selected research contract includes L1, L2 (`orderbooks_full`), trades,
  and only the three dated dim snapshots. RFQ remains excluded/deferred.

## Operator adjudication and bounded AWS writes

The operator subsequently adjudicated that the existing production credential
must not be classified as leaked and explicitly authorized continued use for
this bounded publication run. No credential value was printed or copied. W09's
public address remains `18.226.151.192`; SSH is unavailable because the
instance is stopped, not because its address changed. No EC2 state change was
made.

The detached production worktree was advanced to
`4ed311cfa6c0c6cfd1c20dd5975282f391d36eff`. The live production checkout
remained untouched. Under an owned, trap-cleared REST/catalog refresh pause:

- four missing small controls were conditionally uploaded and read back by
  exact version; `large_data_upload_bytes=0`;
- five stale canonical catalog objects were updated in place under an exact
  five-object allowlist, with no delete and no research-bucket copy;
- a new stable forward auxiliary set was frozen as
  `ada72c93d6af7297be69b3d65cb9e1b29d482e57ba18b9327de24423d74cf65a`;
- the metadata-only shadow verified 577/577 objects, 61,212,551,314 bytes and
  337 research candidates, with zero failures, S3 writes, tag writes or prune
  changes.

## Durable receipt publication

At `2026-07-17T13:28:48Z`, commit `4ed311c` completed exact-VersionId GET plus
full SHA-256 verification for all 577 objects. The run took 22m38s, peaked at
132,764 KiB RSS, used no swap, and retained only one temporary object at a
time. It then made exactly one S3 write: an 823,320-byte immutable control
receipt in the existing canonical bucket.

- state: `DURABLE_RECEIPT_VERIFIED`
- receipt set:
  `763dfaa04f226d5e25aa3564629f3f7e7fd2d23b902d9f8bf3a37aa6d52a00d2`
- object key:
  `ec2/control/canonical-receipts/v1/date=2026-07-14/receipt-763dfaa04f226d5e25aa3564629f3f7e7fd2d23b902d9f8bf3a37aa6d52a00d2.json`
- object VersionId: `U19yyf4MDY790MK5mlndQ5IrOB8KDxbk`
- object SHA-256:
  `38c3d1c585cf10fd7e7df4b670f3b06d45965d97f76c31e64f37713304425fcb`
- exact-version verified objects: 577; RFQ objects: 0; research candidates:
  337; total protected bytes: 61,212,551,314
- facts: 324/324; raw durability: 240/240; dated dim snapshots: 3/3;
  publication catalog: 5/5; all required families `PRESENT_VERIFIED`
- RFQ family: `NOT_APPLICABLE / RFQ_BRANCH_CLOSED_NO_REPAIR`
- prune eligibility: false

The seal intentionally includes a bounded receipt-day overlap: one registered
late `2026-07-13/23` firehose shard and non-RFQ `2026-07-15/00-01` shards.
These objects are durability-only (`research_candidate=false`) and cross-date
objects are deferred by prune authority. They are not research exposure and
are not a copied dataset.

The owned pause flag cleared on exit, the verification temp directory was
empty, and both `kalshi-pipeline.service` and
`kalshi-rfq-capture.service` were active after publication.

## Remaining external gates

The production host has no configured tagger role/profile or instance profile.
The tagger IAM and bucket-policy artifacts are still draft, and `vaultWriter`
cannot call `s3:GetObjectVersionTagging`. Therefore the byte receipt is final,
but eligibility tagging and v3 manifest publication remain blocked at the
tagging gate. Do not bypass it. Once an audited tagger identity is installed,
run exact-version tag tests, publish the eligibility audit/tags, start the
unchanged-address W09 instance, and run the strict W09 consumer canary. RFQ
remains DATA_INTEGRITY_BLOCKED/OFF throughout.
