# Fresh RFQ minimal IAM delta — draft only

Status: `DRAFT_NOT_APPLIED`. This commit performs zero AWS writes.

Draft JSON file SHA-256:
`765fcee3257bd94b887e96cedbf23b3743ea7aa933082a4b72b72708abc0d131`.

The adjacent JSON is a reviewable change set, not a policy to paste directly.
It names the old explicit-Deny statements that must be replaced atomically;
adding the Allow statements alone cannot work because an explicit Deny wins.

The tagger receives only exact-version tag GET/PUT for `rfq_??.ndjson*` and the
PUT must contain both `research-eligible=true` and `research-channel=rfq`.
Versionless tag mutation, RFQ receipt containers, firehose/L2 raw objects, and
the blocked 2026-07-13 RFQ family remain explicitly denied. The existing 284
content-identity deny set is also rechecked by the eligibility package before
the tagger is invoked.

W09 receives no raw LIST and no versionless raw GET. Exact-version RFQ reads
require both tags. IAM cannot dynamically compare a requested VersionId with
the contents of a MANIFEST, so “manifest-selected only” is the intersection of
the IAM dual-tag gate and the existing W09 exact reader's application allowlist;
the reader must reject any exact version absent from the independent overlay
MANIFEST.

S3 `PutObjectVersionTagging` replaces the complete tag set. IAM can require the
two new request tags but cannot prove preservation of arbitrary older tags.
The tagger therefore retains its read-union-write-readback contract and must
abort if any prior tag would be lost.
