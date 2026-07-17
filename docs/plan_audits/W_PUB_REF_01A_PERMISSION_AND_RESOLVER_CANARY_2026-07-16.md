# W-PUB-REF-01A — Permission and metadata resolver canary

**Local date:** 2026-07-16

**Observation completed:** 2026-07-17T02:08:00Z

**Result:** AWS READ PERMISSIONS PASS; CATALOG IDENTITY BLOCKED

**Branch:** `w-pub-ref-01a-aux-bundle`

## Outcome

The operator-added read-only permissions work. The canary then stopped at the
intended byte-identity gate: none of the five canonical catalog objects current
at the authenticated source-release cutoff has the same size as the catalog
object copied into the old v2 release.

This is not an IAM failure. Different size proves different bytes, so no large
catalog object was downloaded and no SHA comparison was needed.

## Permission evidence

Read-only principal observed on the production box:

- `arn:aws:iam::321572485933:user/vaultWriter`

Bucket checked: `kalshi-vault-ritcardo`.

- `s3:GetBucketVersioning`: PASS; versioning status is `Enabled`.
- `s3:GetLifecycleConfiguration`: PASS; S3 returned
  `NoSuchLifecycleConfiguration`, meaning the permission works and the bucket
  currently has no lifecycle configuration.
- `s3:ListBucketVersions`: PASS; exact-key version history was returned for the
  source manifest and all five canonical catalog keys.

No credential value was printed or copied. Credentials were sourced only from
the existing `/home/ubuntu/.kalshi/env.sh` on the production box.

## Authenticated source release

- Release ID:
  `2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5`
- Source key:
  `research/releases/2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5/MANIFEST.json`
- Unique source VersionId: `1ZFrFjSr2UbFODHEsiw_MfsbBOM1bKxY`
- S3 LastModified / catalog cutoff: `2026-07-15T00:10:49Z`
- Size: `218581` bytes
- Exact-version manifest SHA-256:
  `1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd`
- Version history: one version and no delete marker.
- Sealed RFQ is present in this v2 release; its research policy remains
  `FORBIDDEN_RFQ_DEFAULT`.

The exact GET was limited to this small manifest witness. It was downloaded to
a temporary file on the production box only, was not uploaded anywhere, and
was removed after its size and SHA were recorded.

## Catalog resolution result

The next canonical versions after the cutoff are dated
`2026-07-15T03:10:18Z`, so they did not yet exist. The cutoff-current versions
are the versions dated `2026-07-14T03:10:46Z`.

| Catalog | v2 expected bytes | cutoff-current VersionId | canonical bytes | Result |
|---|---:|---|---:|---|
| settlements | 1,917,942 | `TX9nJhbik02XljLwZmT2VnVzgP6XQMpy` | 1,857,363 | MISMATCH |
| series | 974,038 | `47gjPno73y6CkbRs36eYKyhZ8vcqGp7.` | 975,464 | MISMATCH |
| events | 16,749,830 | `nAadHaBQf9k272ybpSET8VZyF.XGzCxC` | 16,686,467 | MISMATCH |
| series_classified | 491,936 | `SGqFNnhvZKXe5a9CK2fOzogeKw_2FrQX` | 491,858 | MISMATCH |
| markets | 96,117,574 | `aHIN17FQD.RR6FPY2_NwnhISLd7Za4Hl` | 105,187,321 | MISMATCH |

All five fail at size comparison. In addition, none of the other versions in
the complete version-history responses returned for these keys has the v2
expected size. This statement is limited to the version history currently
returned by S3; it does not claim that a historically removed version never
existed.

## Safety receipts

- Catalog full-object GETs: **0**
- Catalog bytes downloaded: **0**
- S3 PUT/COPY/TAG/DELETE operations: **0**
- W09 start or compute operations: **0**
- Production checkout/deploy/service changes: **0**
- Production data-collection changes: **0**

The production checkout remained unchanged during the canary.

## Decision gate

The canary is correctly `BLOCKED_INTEGRITY`: the old v2 release captured a
catalog state that is not present as an identical canonical object version.
The current contract must not guess a nearby version.

No fallback is authorized by this canary. The operator must separately choose
one of these paths:

1. Keep existing old dates on their immutable v2 release, and turn on canonical
   zero-copy only for future dates where catalog VersionIds are captured at
   publication time. This is the simplest path and creates no new duplicate.
2. Add a narrowly scoped old-release exception that references the already
   existing v2 catalog VersionIds while facts/raw use canonical references.
   This creates no new copy, but changes the receipt contract and requires a
   new independent audit.
3. Create a one-time content-addressed canonical catalog snapshot from the v2
   catalog. This would add about 116,251,320 bytes and requires explicit S3
   write authorization.

Recommended default: option 1. Do not retrofit the old date; capture exact
canonical catalog identities in the next publication and prove the new
zero-copy path there.

An independent read-only review returned PASS on cutoff selection, all five
size mismatches, the no-large-download stop decision, and the decision paths.
