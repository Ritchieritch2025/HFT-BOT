# PnL Spine latency boundary independent re-audit

Date: 2026-07-23

Audited commit: `264a9dc31a687acee80019c250f3d072daff92c9`

Audited parent: `7df5923e4cbb0534af911eb216ee84b084bd5128`

Audited tree: `acb01efff5f968bebd28a8e76dfef3c5f730cfd8`

Verdict: **FAIL — live PLACE/CANCEL deployment remains blocked**

## Executive result

The repair genuinely closes the false-READY and authority-boundary defects
identified by audit commit
`8cf9517e1dcb362c0ea8ff5d3deaa748cb6be654`:

- one promoted trace must contain exactly one PLACE, one CANCEL and one
  IOC_EXIT sample;
- a second PLACE/CANCEL lifecycle or duplicate IOC sample is rejected;
- the root preamble consumes the PLACE/CANCEL nonce in a root-owned,
  non-deletable object before credentials or network and permanently drops to
  the configured execution UID;
- the final publisher independently opens, hashes and cross-validates the
  binary, config, CA, environment, host, clock, both raw authorities, both
  durable consumption receipts, both terminal receipts and the root promotion
  receipt;
- PLACE authorization is checked against both wall and monotonic deadlines
  before signing and immediately before sending; an expired authority permits
  at most the one already-authorized risk-reducing CANCEL;
- caller-supplied code/config/context SHA and clock facts are no longer
  accepted by either production CLI;
- ambient proxy and CA override variables are scrubbed, while the selected CA
  path and bytes are root-pinned and rechecked before mutations;
- Get Order reconciliation now uses the documented fixed-point count fields
  instead of requiring undocumented status fields;
- `--execute-ioc-exit` still exits before runtime, credentials or a socket, and
  the two-path C++ result explicitly remains
  `PLACE_CANCEL_RECONCILED_IOC_EXIT_MISSING`.

The remaining FAIL is the prior audit's ambiguous-POST production blocker.
When a PLACE POST may have reached the exchange but its response is missing,
the repaired program consumes the authority and refuses to publish successful
evidence, but it still performs no deterministic exchange reconciliation and
no cleanup. A real resting order may remain live. That is evidence fail-closed
but not order-risk fail-closed, so the live mutation branch is not releasable.

No network was contacted, no credential was read and no order was sent during
this re-audit.

## Immutable audit object

| Object | SHA-256 / identity |
|---|---|
| Commit | `264a9dc31a687acee80019c250f3d072daff92c9` |
| Parent | `7df5923e4cbb0534af911eb216ee84b084bd5128` |
| Tree | `acb01efff5f968bebd28a8e76dfef3c5f730cfd8` |
| Commit patch | `8cc855566e43cdaea9dbd5e0912682a8b6718587516ce46c8183285fdd40b9e3` |
| `apps/pnl_latency_probe.cpp` | `964e7cc075b62b4dca866d4cc20d91c945295a776415296d6c22447b0430dbb4` |
| `measured_latency_producer.py` | `9bd94414b1b485e3ac793b7d844d523ae6aea3a21e4386593ed1c77800fe25cd` |
| C++ safety tests | `ff3673a45fb9a5e3681c514ba3504b7f003d8f375647d5e8dbc32ea9c263b7cb` |
| producer tests | `5b31b142b6e9e333df2156cfb5cb570882b8341c608e16ef539e041ce74244a2` |
| implementation report | `303c6cb3a35dd491a9f61a762c3c9cfeb546ac8bdf9f58ff423cb7a0f6ad6b72` |

The patch digest was independently reproduced with:

```text
git show --format= --no-ext-diff --binary 264a9dc | shasum -a 256
8cc855566e43cdaea9dbd5e0912682a8b6718587516ce46c8183285fdd40b9e3
```

## Replayed attack matrix

| Attack from `8cf9517` | Re-audit result |
|---|---|
| Default invocation reaches credentials/network | **REJECTED** — socket-free dry-run, `order_transmitted=false` |
| Missing IOC is laundered into three-path READY by C++ | **REJECTED** — the C++ trace has two paths and declares IOC missing; publisher requires exactly three |
| `--execute-ioc-exit` reads credentials or transmits | **REJECTED** before runtime/credentials/socket |
| Caller self-reports C++ code/config/host/clock hashes | **REJECTED** — flags are absent and real artifacts are loaded |
| Mutable or symlinked C++ control files | **REJECTED** by root owner/mode/path and `O_NOFOLLOW` checks |
| Execution UID unlinks ledger/output/terminal and replays | **REJECTED** — all three are root-created `0400` leaves under root-only parents before permanent privilege drop |
| Second PLACE/CANCEL lifecycle reuses one authority | **REJECTED** — exact sample cardinality is three and every required path cardinality is one |
| Duplicate IOC lifecycle/order reference | **REJECTED** — exact cardinality plus global two-lifecycle uniqueness |
| Publisher omits or ignores consumption/terminal receipts | **REJECTED** — both action chains are mandatory and transaction/path/count/time/SHA-bound |
| Publisher trusts CLI expected SHA strings | **REJECTED** — production CLI takes paths, opens the bytes and derives hashes |
| Trace-local clock ID or future evidence spoofs readiness | **REJECTED** — root clock receipt, host binding, age/error bound and wall-time windows are enforced |
| Root promotion names bytes without binding source/promoted object | **REJECTED** — promotion receipt binds source and promoted path/metadata/size/SHA, and the promoted leaf is rechecked in production mode |
| Authority expires during warmup/preflight | **REJECTED** for PLACE by wall and monotonic checks at sign/send boundaries |
| Authority expires after PLACE but before CANCEL | **BOUNDED** — only one DELETE cleanup is permitted and terminal receipt must classify the post-expiry risk reduction |
| Ambient proxy or CA override redirects a mutation | **REJECTED** — environment is scrubbed and reviewed CA path/SHA is revalidated before mutation |
| Current documented Get Order response lacks status | **ACCEPTED SAFELY** — reconciliation uses documented requested/filled/remaining fixed-point fields |
| POST accepted by exchange but response is lost | **NOT CLOSED** — no query-by-client-ID/open-orders recovery and no cancellation |

## Passed repair details

### Exact authority and lifecycle cardinality

`load_private_trace()` rejects any sample array whose length differs from the
three required paths and separately rejects duplicate paths. PLACE and CANCEL
must share one order reference, IOC must use a second distinct reference, and
request, response and source-event hashes are globally unique.

The publication context loads two distinct raw authorities with distinct
nonces. Each authority must declare `single_use=true`, `max_attempts=1`, exact
action/cardinality limits and exact ticker, host, binary, config, CA, clock and
output-path bindings. Both durable consumption objects and both successful
terminal receipts are mandatory.

The original semantic replay reproductions now fail before receipt
construction:

```text
second PLACE/CANCEL pair -> private trace requires exactly one ...
second IOC sample        -> private trace requires exactly one ...
```

### Root broker and execution identity

The PLACE/CANCEL process now requires real and effective UID 0 for its short
broker preamble. It creates the consumption ledger, trace and terminal leaves
with `openat(O_EXCL|O_NOFOLLOW, 0400)` under root-owned, non-group/world
writable parents. It durably writes the nonce consumption before clearing
ambient network overrides, then calls `setgroups`, `setgid`, `setuid` and
Linux `PR_SET_NO_NEW_PRIVS` before reading credentials.

The inherited write-only descriptors can finish the transaction after the
drop, but the execution UID cannot unlink, rename, reopen or truncate their
root-owned directory entries. Reinvocation against the authority-bound paths
therefore refuses before credentials.

### Publisher independently revalidates raw evidence

The production producer CLI no longer accepts expected code, config,
environment, host, clock, authority or source SHA strings. It requires exact
artifact paths and independently reads the bytes through no-symlink regular
file checks. In production mode all artifacts must remain root-owned and
read-only under non-writable root-owned parents.

The producer cross-validates:

- actual binary, config and CA bytes;
- prod/live environment and exact W09 host identity;
- synchronized clock identity, maximum error, freshness and future skew;
- authority action, nonce, time, ticker, host/context and path bindings;
- durable consumption transaction IDs and exact mutation maxima;
- terminal success state, exact actual mutation counts and final trace SHA;
- promotion source/promoted metadata, path, size and raw SHA;
- each sample's action, HTTP result, authority, timing, request facts, fill
  conservation and position conservation.

Missing, changed or self-consistently rehashed trace bytes cannot become READY
unless the independently loaded authority and terminal/promotion chain also
matches.

### Mutation expiry and network boundary

The PLACE path evaluates authority validity before signing and again
immediately before `lane.send()`, using both system-wall and derived steady
clock deadlines. The only mutation allowed after expiration is the single
DELETE for the already-placed exact order. That classification is persisted
in the terminal receipt and revalidated by the publisher.

Proxy, SOCKS, curl/SSL/Requests/AWS CA override variables are removed before
credentials. Their absence, the libcurl default CA path and the root-pinned CA
SHA are rechecked before PLACE and CANCEL.

### IOC transmitter remains closed

`main()` handles `--execute-ioc-exit` before any runtime resolution or
credential access and returns 2 with
`IOC_EXIT TRANSMISSION IS NOT IMPLEMENTED AND FAILS CLOSED`. The only IOC
capability is an explicitly read-only GET preflight. No IOC POST implementation
exists in this commit.

This is not a three-path delivery. A legitimate READY receipt still requires a
separately implemented, authorized and independently audited IOC executor and
its real consumption/terminal chain.

## Remaining blocker — ambiguous PLACE POST is not reconciled

The critical branch is:

```text
apps/pnl_latency_probe.cpp:1907-1916
```

The process increments `place_post_attempts`, calls
`lane.send(*place_request)`, and, if the response is missing or not HTTP 201,
immediately writes
`BLOCKED_AMBIGUOUS_PLACE_OUTCOME_NO_RETRY` and exits. There is no subsequent:

- query by the still-in-memory deterministic `client_order_id`;
- bounded open-orders lookup;
- exact order-ID recovery;
- one-shot risk-reducing cancellation;
- order/position readback proving that nothing remains.

The `best_effort_cancel()` calls at lines 1923-1927 and 1945-1946 only run
after a response supplied an exchange order ID. They cannot handle the lost
response case.

### Adversarial outcome

1. The exchange accepts the one-cent post-only bid.
2. The HTTP response is lost between exchange and client.
3. The process enters the ambiguous branch.
4. The authority is consumed and no duplicate POST occurs, which is correct.
5. Evidence cannot publish READY, which is also correct.
6. The accepted order can remain resting and later fill because no exchange
   reconciliation or cancellation is attempted.

The configured one-cent cash cap limits the amount, but it does not turn an
unknown live order into a closed state. This remains the exact B-2 production
blocker from the first audit.

### Required closure

Before live PLACE/CANCEL authorization:

1. retain the raw deterministic client order ID in the private transaction
   boundary until terminal reconciliation;
2. on an ambiguous POST, never retry POST, but perform an authority-bound,
   bounded lookup by client order ID or exact open-orders inventory;
3. if the order exists, issue the one permitted risk-reducing DELETE and prove
   `filled=0`, `remaining=0` and unchanged position;
4. if lookup itself is ambiguous, emit a durable incident receipt, engage the
   account-level trading block and require proven external reconciliation;
5. add a transport mock in which the exchange accepts POST and drops the
   response, asserting that no resting order survives and READY remains
   impossible.

## Reproduced tests

All commands ran in a detached worktree at exact commit `264a9dc`. Only dry
run, local parser/self-test and synthetic filesystem evidence paths were
executed.

```text
make build/pnl_latency_probe
PASS, no compiler warning

python3 -m unittest \
  tests.test_pnl_latency_probe_safety \
  tests.test_pnl_spine_measured_latency_producer -v
28/28 PASS

python3 -m unittest \
  tests.test_pnl_spine_measured_latency_producer \
  tests.test_pnl_spine_latency_evidence -v
23/23 PASS

python3 -m unittest discover -s tests -p 'test_pnl_spine*.py' -q
65/65 PASS

python3 -m pytest -q tests/test_pnl_spine*.py
320/320 PASS

python3 tools/check_registry.py
PASS: 60 tools, 48 build targets; pnl_latency_probe is console-forbidden

git diff --check 264a9dc^ 264a9dc
PASS
```

The audit-machine binary SHA-256 was
`d6c9bb5b7937caaa485e5d471e29cf63e789039d07427bac05e60fc5c6689d4d`.
It is a local build receipt, not a production deployment pin.

## Release decision

Do not issue or install a live PLACE/CANCEL authority for commit `264a9dc`.
Do not execute its live mutation branch until ambiguous POST recovery is
independently repaired and re-audited.

The evidence-integrity repairs may remain as the correct base for that narrow
follow-up. Default dry-run and the hard-refused IOC transmitter remain safe.
No legitimate three-path latency receipt exists yet, so
`LATENCY_AGGREGATE_READY` must not be represented as a current production
fact.
