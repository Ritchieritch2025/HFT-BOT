# W-PUB-REF-01C tagger policy delivery

State: **AUDITED / OPERATOR-APPLIED / CONSOLE READBACK CONFIRMED**
Produced at: `2026-07-17T19:28:24Z`
AWS writes performed while preparing this delivery: `0`

The operator later reported creation of the pathless IAM user, attachment of
the audited identity policy, and application/readback of the complete bucket
policy.  That report changes deployment state; it does not change either
policy byte stream or SHA-256 below.

## Operator-attested baseline

The operator reported that S3 Console → `kalshi-vault-ritcardo` →
Permissions → Bucket policy displayed `No policy to display`.  The existing
bucket-policy statement set is therefore empty.  The seven-statement rendered
policy is the complete target bucket policy, not a fragment to merge with any
pre-existing bucket statement.  Its SHA-256 correctly remains equal to the
previous rendered-fragment SHA-256.

Read-only AWS checks separately confirmed:

- active publisher principal: `arn:aws:iam::321572485933:user/vaultWriter`;
- bucket versioning: `Enabled`;
- lifecycle configuration: absent (`NoSuchLifecycleConfiguration`);
- `vaultWriter` cannot call `s3:GetBucketPolicy`, `iam:GetUser`, or IAM Access
  Analyzer `ValidatePolicy`.

## Final artifacts

- Target tagger user ARN:
  `arn:aws:iam::321572485933:user/canonical-eligibility-tagger`
- Identity policy:
  `W-PUB-REF-01C_TAGGER_IDENTITY_POLICY.json`
  - bytes: `6304`
  - compact policy characters: `5354`
  - SHA-256:
    `2fa7dbe4103f01b26fe871c1a0c8f215f2aba4396e6902db1cdcf08eeae275a0`
- Complete target bucket policy:
  `W-PUB-REF-01C_BUCKET_POLICY_FULL_TARGET_2026-07-17.json`
  - bytes: `6872`
  - statements: `7`, all `Effect: Deny`
  - SHA-256:
    `d9961561ce2e434a40bde5f62e716c0567a1148c0a5f9b42a07a8b70d2bb02d5`

The target ARN was reported back from the operator's successful console
creation.  The immutable IAM `UserId` is still intentionally treated as
unverified until it is obtained through an authenticated AWS API response.

## Independent audit result

Two independent read-only audits returned PASS with P0=0 and P1=0.  They
verified valid JSON with no duplicate keys/Sids, no `TAGGER_ARN` placeholder,
one exact target ARN, seven bucket Deny statements, the manifest conditional
create Deny, deletion Denies, matching canonical tag allowlists, versioned-only
tag mutation, raw/RFQ hard Deny, and the complete GET/merge/PUT/readback path
that preserves existing object tags.

IAM Access Analyzer validation remains an operator-console acceptance step
because the available `vaultWriter` principal is not permitted to call
`access-analyzer:ValidatePolicy`.

## Console application order

1. Create pathless IAM user `canonical-eligibility-tagger`; record its returned
   ARN and `UserId`, and require the ARN to equal the target above.
2. Create a customer-managed IAM policy from the identity-policy JSON and
   attach it only to that user.  Do not use an inline user policy: its 5,354
   non-whitespace characters exceed the 2,048-character aggregate user-inline
   limit but fit the 6,144-character customer-managed-policy limit.
3. Run IAM Access Analyzer policy validation in the console.
4. Paste the complete target bucket-policy JSON into the S3 bucket policy
   editor and save it.
5. Export both live policies, re-hash them, list every attachment to the managed
   policy, and run the documented positive and negative exact-version canary.

This base policy intentionally keeps RFQ tag mutation denied for every
principal.  Fresh RFQ reopening requires its separately audited IAM/bucket
delta and is not enabled by this delivery.
