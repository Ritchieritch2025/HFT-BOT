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
