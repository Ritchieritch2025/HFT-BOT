# W-PUB-REF-01C AWS read-only evidence

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

The metadata-only S3 shadow was not run. The production login shell has no
instance-role/CLI credentials, and a static credential was discovered in shell
history and must be revoked/rotated, not reused. The sync timers still reported
successful runs and explicitly source static credentials from
`~/.kalshi/env.sh`; treat that credential as potentially compromised and
replace it before the next timer. After clean credentials are installed, rerun
exact-version tag permission checks before any metadata shadow or publication.
W09's pre-restart public address did not answer SSH and must be rediscovered
through the authenticated EC2 control plane.
