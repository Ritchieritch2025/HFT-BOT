# A01 Official Market Terminal Adapter — Independent Adversarial Audit

Date: 2026-07-23

Audit target: commit `c486206fb48a2d3e7d5f199b457160e2dc70f30f`

Scope: only `official_market_terminal_adapter.py` and its tests

Network calls: 0

Financial mutations: 0
Implementation changes by auditor: 0

## Verdict

**FAIL — do not deploy this commit as the production terminal-evidence adapter yet.**

The core terminal-result path is conservative and most evidence controls are
implemented correctly. However, two adversarial cases violate explicit control
claims:

1. an HTTP attempt can start and finish after the authority has expired, because
   authority validity is checked only once at batch start;
2. the transport URL validator accepts noncanonical userinfo and dot-segment
   URLs even though the configuration claims a fixed host and fixed endpoint
   paths.

The first defect also permits cross-attempt wall and monotonic clock regression,
so a multi-market capture receipt can contain an impossible request chronology
and still validate.

## Immutable target identity

| Item | Identity |
|---|---|
| Commit | `c486206fb48a2d3e7d5f199b457160e2dc70f30f` |
| Adapter Git blob | `483d621617b0f3d8067f66c416b0daab8558413b` |
| Adapter raw SHA-256 | `1ae5d4addd8561e7cf5b9dba39d69b610eebe3d1d13c59dedbfdfde63a440ac4` |
| Test Git blob | `566b86adcae5667e622af73d845ccce7a059ff02` |
| Test raw SHA-256 | `9ea5947544110640dc9c548d1b8e5bc4df2ce18d1fc6e3a1ba8661f40bc898e9` |
| Adapter config SHA-256 | `2dc20de5bb0c25005c4f5476052672bcdf6b3731a76e51629098544ec2fac9fa` |

The audit was run from a clean archive of the exact commit, not from the shared
dirty worktree.

## Test results

```text
tests/test_pnl_spine_official_market_terminal_adapter.py
26 passed in 0.09s

tests/test_pnl_spine*.py tests/test_pnl_latency_probe_safety.py
297 passed in 8.21s
```

Those green tests are real, but neither failing adversarial case below is in the
26 adapter tests.

## Blocking findings

### F-01 — Authority expiry and global clock continuity are not enforced per HTTP attempt

Severity: **P1 / deployment blocker**

Authority validity is checked once from the first wall-clock sample at
`official_market_terminal_adapter.py:633-639`. Each request later checks only
that its own `after >= before` at `:569-575`. The capture loop at `:642-692`
does not compare an attempt with the prior attempt and does not re-check the
authority window before or after a request.

Adversarial replay accepted both of these receipts:

```text
authority: expires 2026-07-24T00:00:00Z
batch validation wall: 2026-07-23T23:59:59Z
HTTP before wall:       2026-07-24T00:00:01Z
HTTP after wall:        2026-07-24T00:00:02Z
result: ACCEPT
```

```text
attempt A after:  wall=2026-07-23T12:00:02Z, monotonic=1200
attempt B before: wall=2026-07-23T12:00:01Z, monotonic=1100
attempt B after:  wall=2026-07-23T12:00:02Z, monotonic=1200
result: ACCEPT
```

Impact:

- the receipt can include reads outside the exact authority validity window;
- the purported wall/monotonic observation sequence can move backwards;
- downstream code cannot treat the batch chronology as a trustworthy capture
  ledger.

Required repair:

- carry the parsed authority issue/expiry bounds into every attempt;
- require every HTTP `wall_before` and `wall_after` to lie inside the authority
  window;
- maintain batch-wide monotonic continuity, including current-to-historical
  fallback and ticker-to-ticker transitions;
- validate the same invariants again in `_validate_capture_receipt`;
- add attacks for expiry during request, expiry between tickers, regression
  between current and historical, and regression between tickers.

### F-02 — `_build_request` accepts noncanonical paths and URL userinfo

Severity: **P1 / fixed-origin contract blocker**

The validator at `official_market_terminal_adapter.py:404-436` checks hostname
and `path.startswith(...)`, but it does not reject `username`/`password` and it
does not require an exact canonical endpoint plus canonical ticker.

The exact commit accepted:

```text
https://attacker@api.elections.kalshi.com/trade-api/v2/markets/KXATPMATCH-26JUL17RUBBAE-RUB
https://api.elections.kalshi.com/trade-api/v2/markets/../portfolio/balance
https://api.elections.kalshi.com/trade-api/v2/markets/%2e%2e/portfolio/balance
```

The normal `capture_markets` route currently constructs its URL through
`_market_url`, whose ticker allowlist prevents these strings. Therefore this is
not evidence that the current authority-to-capture route contacted another
host. It is still a failed defense boundary: `urllib_transport` is a callable
transport entry point, and its own validator does not satisfy the adapter's
declared fixed-path rule.

Required repair:

- reject URL username and password explicitly;
- accept only an exact reconstruction of
  `OFFICIAL_ORIGIN + {CURRENT|HISTORICAL}_MARKET_PATH + canonical_ticker`;
- reject dot segments in literal and percent-encoded form;
- add direct tests against `_build_request` and `urllib_transport`.

## Passed adversarial controls

| Control | Result |
|---|---|
| Authority ticker traversal (`../../...`) | Rejected before transport |
| Off-host HTTPS URL | Rejected |
| Proxy environment variables | Explicit empty `ProxyHandler`; not used |
| Redirect replay | Disabled; changed final URL rejected |
| Credential/cookie headers | Not emitted; forbidden header names checked |
| Historical fallback | Only current HTTP 404 can select historical |
| 301/302/307, 429, 500, 503 | Fail closed; never fall back to historical |
| Response byte limit | Enforced on HTTP and captured raw bodies |
| Duplicate JSON keys, including nested keys | Rejected |
| Floating-point JSON | Rejected |
| Missing consumed response fields | Rejected |
| Wrong ticker / extra envelope keys | Rejected |
| Non-final status or non-`yes`/`no` result | Rejected |
| Result/payout mismatch | Rejected |
| Payout representation | Exact four-decimal E4; only exact 0/10000 mapping survives |
| Tick structure | Only `linear_cent`, one `[0,1]`, one-cent interval |
| Current/historical selected response binding | Revalidated against capture and raw pins |
| Raw tamper, size tamper, pin tamper | Rejected |
| Capture output | New directory plus `O_EXCL` create-once files |
| Control files | Absolute, no symlink components, fd-stable read, root-owned, no write bits, sealed parent tree |
| Raw files during production normalize | Root-owned/no-write required; capture receipt parent has already established a sealed root tree |
| Code/config/SHA bindings | Bound through authority, attempts, capture receipt, raw pins, and normalized output |
| Scheduled-start honesty | Correctly remains `None`; occurrence/expiry/close are explicitly prohibited as substitutes |
| Historical-as-of honesty | Correctly states fetch-time observation only |

## Non-blocking observations and remaining production blockers

1. **429 availability:** rejecting 429 is safe, but the CLI has no bounded
   throttle or retry. One 429 aborts the whole create-once multi-ticker batch.
   Before a 295-ticker production capture, either add an audited bounded
   rate-limit policy that records every attempt, or shard/throttle exact
   authorities. A 429 must never trigger historical fallback.

2. **Timestamp semantics:** timestamp syntax and calendar validity are checked,
   but cross-field causal ordering is not. A format-valid 2099
   `settlement_ts`, or `open_time > close_time`, is currently accepted. This
   does not silently become scheduled start and does not alter the exact
   yes/no payout, so it is not promoted to a blocker here. If downstream logic
   will use these timestamps for event ordering, an official schema-backed
   temporal policy must be frozen first.

3. **CLI is the trust boundary:** the production CLI reads exact root-pinned raw
   bytes and computes their SHA before parsing. Direct calls to
   `capture_markets`/`normalize_capture` receive parsed objects and SHA strings
   separately and cannot prove those pairs came from the same bytes. Production
   deployment must call the CLI only unless those APIs are changed to accept
   raw bytes.

4. **External pinning is still an operation, not delivered evidence:** no real
   official response was fetched in this audit. A root-owned, no-write capture
   receipt, selected-response pins, raw bodies, and normalized output still
   have to be produced and independently SHA-pinned on W09.

5. **Lineage integration remains external:** this adapter produces official
   terminal evidence, but the final PnL lineage contract must either include
   these externally pinned members explicitly or publish them into an
   exact-version immutable release. The existing release-only lineage receipt
   cannot be relabeled as covering this source.

6. **Truthful unresolved data:** the adapter correctly leaves historical
   point-in-time metadata/lifecycle, authorized scheduled start, and settlement
   revision history blocked. This commit resolves terminal payout and observed
   terminal metadata only.

## Re-audit acceptance gate

A successor immutable commit may receive `PASS` only if:

1. F-01 and F-02 each have regression tests that fail on `c486206` and pass on
   the successor;
2. the original 26 tests and the full PnL Spine suite remain green;
3. current 404 remains the sole historical-fallback trigger;
4. proxy, redirect, authentication, root-pin, raw-SHA, final-status, payout,
   tick-table, and honest-blocker behavior remains unchanged;
5. the audit is repeated on the successor commit bytes.
