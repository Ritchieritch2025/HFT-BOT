# A01 Official Market Terminal Adapter — Independent Repair Re-audit

Date: 2026-07-23

Repair target: `f6da844d22ffb9413abb9aa397791bd29d64f872`

Predecessor audit: `fff1af0`

Scope: only the official market terminal adapter and its tests

Network calls: 0

Financial mutations: 0

Implementation changes by auditor: 0

## Verdict

**PASS.**

The two deployment blockers in the predecessor audit are closed:

1. every HTTP attempt is now bounded by the authority window, batch-wide wall
   and monotonic chronology, and a second receipt-time validation;
2. the transport now accepts only the exact fixed origin, one exact endpoint,
   and one canonical ticker, rejecting userinfo, ports, dot segments, encoded
   path syntax, queries, fragments, and noncanonical reconstruction.

The new bounded 429 policy also passes the requested adversarial cases. This
verdict permits packaging and read-only W09 deployment of this exact commit. It
does not claim that a live capture, external root pin, final source-lineage
receipt, or A01 PnL run has already occurred.

## Immutable target identity

| Item | Identity |
|---|---|
| Commit | `f6da844d22ffb9413abb9aa397791bd29d64f872` |
| Adapter Git blob | `feb7e5bb5571c7885f56781c32d4e20ce702cda5` |
| Adapter raw/code SHA-256 | `b3a4d24a2c101e89b8516942ea6dc09ed102e79ee8bb27d8a3382e95f5acdfdf` |
| Test Git blob | `009ad1f8875e33b5fcc282622a27a2286833b6e2` |
| Test raw SHA-256 | `7b44cbd8274c1002d441ef06cc3f00926b9b0088006a5f4afb63be6883fa798d` |
| Adapter config SHA-256 | `03086bdca2c0f989eadc4dda72c2d5c9433c344fff1af38c82e98f8d61c4dfec` |

The tests and attacks were run from a clean archive of the exact repair commit,
not from the shared dirty worktree.

## Test results

```text
tests/test_pnl_spine_official_market_terminal_adapter.py
55 passed in 0.12s

tests/test_pnl_spine*.py tests/test_pnl_latency_probe_safety.py
331 passed in 7.03s
```

No test depended on network availability or credentials.

## Predecessor findings

### F-01 — Authority expiry and cross-attempt clock regression

**CLOSED**

Independent replay established:

- a request whose response clock crosses authority expiry preserves its raw
  response for audit but produces no `CAPTURE_RECEIPT`;
- expiry before the next ticker prevents the next transport call;
- wall or monotonic rollback between tickers is rejected before the second
  transport call;
- rollback between current and historical endpoints is rejected;
- the receipt validator independently checks authority bounds, contiguous
  attempt indexes, non-regressing clocks, and the required inter-request delay.

Exact replay output included:

```text
old_F01_expiry_during_http
REJECT: HTTP attempt after is outside authority validity window

old_F01_cross_ticker_clock_regression
REJECT: HTTP attempt before wall clock regressed across batch
```

### F-02 — Noncanonical URL accepted by transport

**CLOSED**

The following classes were rejected before opener construction:

- `https://userinfo@api.elections.kalshi.com/...`;
- literal `../` and encoded `%2e%2e`;
- encoded slash/backslash;
- an extra path segment;
- port, case-altered host, trailing-dot host, query, and fragment;
- percent-encoded ticker characters.

Both current and historical canonical URLs remain accepted.

## New adversarial matrix

| Attack | Result |
|---|---|
| 429 retry delay crosses authority expiry | Rejected after one call; no sleep, retry, or success receipt |
| Missing `Retry-After` | Rejected |
| `0`, negative, leading-zero, fractional, whitespace-padded value | Rejected |
| HTTP-date, comma-combined, Unicode-digit value | Rejected |
| Value above 30-second bound | Rejected |
| Multiple `Retry-After` header instances | Rejected |
| Three consecutive 429 responses | Exactly three uniquely named raw files, then bounded refusal |
| Pre-created retry raw filename | `O_EXCL` refusal; sentinel not overwritten; no success receipt |
| Retry then 200 | Both raw responses retained; one selected terminal record |
| Retry then current 404 | Historical allowed only after terminal current 404 |
| Current 301/400/500/503 | Refused; historical endpoint never called |
| 51-ticker authority | Refused before capture; explicit sharding requirement |
| Wall rollback during throttle sleep | Refused before retry transport |
| Monotonic rollback during throttle sleep | Refused before retry transport |
| Delete a retry attempt from receipt | Refused by noncontiguous batch attempt index |
| Reorder/renumber endpoint attempts | Refused by batch/endpoint sequence and delay invariants |
| Hide attempt while retaining original raw files | Full normalize refuses on raw size/SHA mismatch |
| Mutate root-pinned receipt bytes | Exact expected raw SHA refuses before parsing |

The raw filenames bind both ticker index and global batch attempt index, so
retries cannot overwrite each other. The normalized evidence retains all
attempts; an independent retry-then-success run yielded:

```text
raw_response_evidence statuses: [429, 200]
terminal_record_count: 1
```

## Receipt-tamper trust boundary

A fully rewritten in-memory receipt can be made internally self-consistent by
removing an attempt and rewriting every later index, delay, and filename. The
standalone `_validate_capture_receipt` structure checker cannot prove that an
unseen HTTP call once existed. This is expected for a producer-owned object and
is not the production trust boundary.

The complete production route remains fail-closed:

1. `capture` emits the original receipt raw SHA;
2. the caller independently retains that SHA;
3. the receipt, raw bodies, authority, and selected-response pins are moved to
   a root-owned, no-write, sealed directory;
4. `normalize` opens those exact files by pinned raw SHA and re-hashes every raw
   attempt.

In the adversarial replay, a fully rewritten receipt with the unchanged raw
directory passed the structural checker but failed complete normalization on
raw size/SHA mismatch. Post-pin receipt edits fail before parsing.

Therefore:

- the production CLI plus external pin ritual is approved;
- direct calls that pass parsed objects and caller-supplied SHA strings are not
  independent trust boundaries and must not replace the CLI;
- a root operator who deliberately discards the originally returned receipt
  SHA and blesses forged replacement bytes defeats the declared trust anchor
  and is outside this adapter's threat model.

## Preserved safety and semantic controls

The repair did not regress:

- no proxy, redirect, credential, cookie, or financial mutation path;
- exact current-to-historical fallback only on current 404;
- strict UTF-8 JSON, duplicate-key and float rejection;
- response byte bound and create-once writes;
- exact code/config/authority/capture/raw-pin bindings;
- root-owned, no-write control files and sealed parent trees;
- exact finalized `yes`/`no` result and 0/10000 E4 payout agreement;
- standard `linear_cent`, full-range, one-cent tick eligibility;
- no use of occurrence, expected expiration, or close time as scheduled start;
- no claim that current fetch time is historical point-in-time metadata;
- explicit blockers for historical metadata/lifecycle, scheduled start, and
  settlement revision history.

## Remaining production work outside this PASS

1. **Deploy exact bytes:** W09 must use adapter code SHA
   `b3a4d24a2c101e89b8516942ea6dc09ed102e79ee8bb27d8a3382e95f5acdfdf`
   and config SHA
   `03086bdca2c0f989eadc4dda72c2d5c9433c344fff1af38c82e98f8d61c4dfec`.

2. **Shard the real ticker set:** the fixed limit is 50 tickers per authority.
   A 295-ticker set therefore requires six exact, independently pinned
   authorities/capture directories. This is an intentional bounded policy, not
   permission to omit tickers.

3. **Perform and pin the real capture:** this audit made no network call.
   Production must retain the capture-command receipt SHA before changing
   ownership, then independently verify and seal every raw response.

4. **Run normalization and lineage binding:** official terminal evidence still
   must be incorporated into the final lineage contract, either as explicit
   externally pinned members or exact-version immutable release objects.

5. **Keep unresolved metadata honest:** this adapter resolves official terminal
   payout and fetch-time terminal metadata. It does not resolve historical
   lifecycle intervals, authoritative scheduled start, or settlement revision
   sequence.

6. **Natural 429 observation:** the bounded retry logic is mock-proven but has
   not yet crossed a real rate-limit response. Production must retain the first
   natural run receipt; a failure produces no trusted terminal output and must
   be retried only with a fresh exact authority/output directory.

## Deployment acceptance

`f6da844d22ffb9413abb9aa397791bd29d64f872` is approved for the read-only W09
capture-and-normalize stage, provided the root-pinning and exact-hash workflow
above is followed. This PASS does not authorize S3 writes, trading actions, or
opening A11/B09.
