# ROUND4 post-fill V4.2 data-integrity addendum

Status: **NO SEAL — AUTHORITATIVE SOURCE ADAPTER V3/EOF PENDING**

V4.2 is an offline integrity boundary. It cannot extract, fit, select,
deploy, connect to an account, or trade. FIT, CANDIDATE, DEPLOYABLE, and
LIVE remain false.

## Accepted interim changes

- The commit builder hashes the normative validator bytes and requires the
  exact pinned SHA256 before opening the transaction.
- The validator must create both an exact attestation TEMP view and an exact
  violations TEMP view. Missing, empty replacement, wrong schema, or any
  violation rolls the whole batch back.
- Market metadata and settlement receipts are one-to-one with a legal
  episode in both directions. Orphans, duplicates, and market/date/episode
  mismatches are rejected.
- Every V4.2-owned episode table participates in a closed graph. Projected
  V4.1 rows have an episode count and ordered payload spine; deleting the
  independently audited `...SLICE::1` counterexample is rejected.
- All V4.1 artifacts and hashes remain frozen.

## Source boundary remains closed

The first draft constructed a small canonical JSON file from the V4.1 rows
being checked, then let that file call itself complete. That was circular:
omitting a later book or changing a trade and re-manifesting the smaller file
would still pass inside the fabricated universe.

That path is removed from every public V4.2 construction and commit entry
point. Stage-2 V1 is also not an accepted parent authority because its local
root/link containment and completeness proof did not pass independent
review.

Stage-2 V2 is accepted only as a safer preparation foundation: it fixes
no-link/root containment and internally recomputes typed-result coverage.
It is not an authoritative V4.2 adapter because it explicitly claims no
real-warehouse coverage, does not couple the causal result to the six-parent
manifest, and exposes only post-envelope book hashes rather than the full
level states needed for an independent FOK recomputation.

V4.2 will remain fail-closed until an independently reviewed authoritative
Stage-2 V3/EOF adapter supplies:

1. all six exact-version parent identities and actual-byte checks;
2. strict regular-file, root-containment, and no-link proof;
3. program-derived 72-market/three-day EOF completeness;
4. the complete receive-clock atomic terminal-state spine;
5. `recv_mono_ns`-primary latest-as-of selection, strictly before both
   effective clocks; and
6. a deterministic transform receipt that is re-run at commit.

No V4.2 manifest, candidate, extraction, model, shadow run, or live run is
authorized by this addendum.
