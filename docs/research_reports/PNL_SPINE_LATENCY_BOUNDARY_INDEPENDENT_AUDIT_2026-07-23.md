# PnL Spine latency boundary independent adversarial audit

Date: 2026-07-23

Audited commit: `6b3974edcb76fbb84ea1393771aee50ec529d89f`

Audited tree: `aa2cdb8329d77cfc53b0b697347a4accd601d606`

Verdict: **FAIL — live deployment and `LATENCY_AGGREGATE_READY` publication are blocked**

## Executive result

The commit is honest about one important limitation: `IOC_EXIT` is not
implemented, `--execute-ioc-exit` refuses before credentials or network, the
default mode is socket-free dry-run, and the PLACE/CANCEL result cannot claim
three-path readiness. The missing IOC executor is therefore an explicit
production blocker, not the reason for this FAIL.

The FAIL is caused by independent evidence-integrity and live-authority
defects:

1. one externally pinned “single-use” authority can be replayed inside a
   canonical private trace as multiple PLACE/CANCEL lifecycles, and the
   producer still publishes `LATENCY_AGGREGATE_READY`;
2. one IOC order lifecycle can be counted more than once, with the same IOC
   authority, and still publish READY;
3. the execution-owned consumption ledger and trace are unlinkable by the
   execution identity, and the publishing producer does not consume or verify
   the ledger or either authority document;
4. authority expiration is checked only near startup, not immediately before
   PLACE transmission;
5. the final producer does not load and validate the pinned clock, host,
   environment, code, config, authority, or consumption documents. It accepts
   caller-supplied SHA strings and a trace-local clock identifier, including a
   future-dated synthetic trace, as READY after root promotion.

These are false-READY and mutation-authority failures. They must be fixed
before any real order probe.

## P0 findings

### P0-1 — semantic authority replay produces READY

`load_private_trace()` requires at least one sample for each path, but does not
require exact cardinality. It groups any number of PLACE/CANCEL samples by
order reference and checks only that the two sets match
(`measured_latency_producer.py:509-517, 695-765`). It also checks IOC order
references only against the PLACE set; it does not reject duplicate IOC order
references.

An audit-only reproducer took the shipped valid trace, added:

- a second distinct PLACE/CANCEL order pair carrying the same externally
  pinned PLACE/CANCEL authority SHA; and
- separately, a second IOC sample carrying the same IOC authority and same
  IOC `order_ref_sha256`, with unique request/response/event hashes.

Observed results:

```text
AUTHORITY_REPLAY_ACCEPTED 5 LATENCY_AGGREGATE_READY
{'PLACE': 2, 'CANCEL': 2, 'IOC_EXIT': 1}

DUPLICATE_IOC_LIFECYCLE_ACCEPTED 4 LATENCY_AGGREGATE_READY True
```

The raw source can be externally SHA-pinned and root-promoted without changing
this semantic result. Immutability authenticates the accepted bytes; it does
not make the bytes comply with `max_attempts=1`.

Required fix:

- require exactly one PLACE, one CANCEL, and one IOC_EXIT sample for this
  receipt schema;
- require global order-lifecycle uniqueness, with only the intended
  PLACE/CANCEL pair allowed to share one order reference;
- load, hash, and validate both exact authority documents and their exact
  durable consumption receipts;
- enforce authority action, cardinality, nonce, ticker, host, code/config,
  time window, and output/source bindings in the publishing gate;
- add both reproductions as permanent negative regression tests.

### P0-2 — the one-shot ledger is not a durable one-shot boundary

The C++ process reserves output and ledger before credentials/network, which is
good crash ordering (`pnl_latency_probe.cpp:1461-1495`). A crash leaves a file
and an honest automatic retry will fail closed.

However, both files are created as the execution identity and remain owned by
that identity. Read-only mode does not prevent the owner from unlinking its own
files from the documented sticky, group-writable parent. The same authority
and exact bound paths can then be invoked again. The implementation report
acknowledges this threat-model exclusion, but the authority schema still calls
itself `single_use=true`.

More importantly, the Python publication CLI takes only the private trace. It
does not require the consumption ledger at all. A trace can therefore be
root-promoted and published even when the ledger is absent, deleted, or
inconsistent.

Required fix:

- have a root broker or external append-only service atomically consume the
  nonce before the execution identity can obtain credentials;
- keep the durable nonce record outside a directory where the execution
  identity can unlink it;
- require the exact consumption record in the final producer and bind it to
  the authority SHA, nonce, attempt, host, trace path/source SHA, and mutation
  counts;
- retain the current output-first crash reservation as defense in depth, not
  as the only one-shot control.

### P0-3 — an expired authority can still transmit PLACE

Authority time validity is evaluated once in `parse_authority()`
(`pnl_latency_probe.cpp:1143-1152`). After that check the process writes and
fsyncs the ledger, resolves runtime, reads credentials, builds the client,
warms the lane, and fetches the starting position. There is no expiration
check immediately before `lane.send(*place_request)` at
`pnl_latency_probe.cpp:1555-1565`.

An authority valid for one more second at parse time can therefore authorize a
PLACE sent after expiry when warmup/preflight is delayed. The wall-clock
expiry must be rechecked at the mutation boundary.

Required fix:

- derive a monotonic deadline when the authority is accepted;
- recheck both wall and monotonic deadlines immediately before signing/sending
  PLACE;
- define CANCEL cleanup as a narrowly bounded risk-reduction operation if
  expiry occurs after PLACE, while still refusing any new risk;
- add a deterministic delayed-warmup expiry test.

### P0-4 — final READY still trusts self-reported context

The C++ PLACE/CANCEL process does derive its own executable, host, EC2
instance, config, environment and clock facts from root-controlled paths. That
part rejects CLI-supplied runtime hashes.

The final Python producer, however, receives all expected hashes as CLI
strings. It does not load the referenced root-pinned receipts, authorities,
consumption ledgers, or generating executables. It also accepts any ASCII
`clock_id`; the clock receipt SHA is never opened to prove that its clock ID
matches. Its wall-clock checks are only relative to trace-local matching-engine
timestamps and do not reject future evidence.

The shipped positive fixture demonstrates the gap: it uses
`clock_id=CLOCK_MONOTONIC_RAW:prod-exec-1` and epoch milliseconds
`1800000000xxx` (future-dated relative to this audit), yet produces
`LATENCY_AGGREGATE_READY`.

Root ownership/read-only mode proves which bytes root promoted; it does not
prove how those bytes were generated. A separate independent promotion
receipt or cryptographic producer attestation remains mandatory.

Required fix:

- make the final producer read and hash the exact clock, host, environment,
  code/config, authority, and consumption artifacts itself;
- cross-validate their structured contents, including clock ID, timestamps,
  EC2 instance ID, hostname, machine ID, build/config hashes and nonce;
- reject evidence outside the current authority/clock window;
- pin an independently generated root-promotion receipt that binds the
  pre-promotion source inode/bytes to the promoted object.

## Additional production blockers

### B-1 — current Get Order contract compatibility is not proven

`parse_order_snapshot()` requires `status` or `order_status`
(`pnl_latency_probe.cpp:915-934`), and PLACE/CANCEL success later requires
`RESTING`/`CANCELED`. The local “official V2” self-test invents a Get Order
fixture containing `status`.

The current official Kalshi Get Order response example and exposed response
schema document `initial_count_fp`, `fill_count_fp`, and
`remaining_count_fp`, but do not document `status` or `order_status`:

https://docs.kalshi.com/api-reference/orders/get-order

No real read-only response receipt is supplied to resolve the discrepancy.
Before a live mutation, capture and independently pin a real response from the
production account, or derive state fail-closed from documented fields and
test the exact real bytes. As written, a documented response can make the
probe place an order, fail reconciliation, issue only a best-effort cancel,
and produce no usable sample.

### B-2 — ambiguous POST outcome has no deterministic recovery

If POST reaches the exchange but the HTTP response is lost,
`lane.send(*place_request)` returns an error and the code exits without an
order ID or reconciliation (`pnl_latency_probe.cpp:1562-1567`). The fixed
client order ID helps exchange idempotency but the probe does not query by
client order ID/open orders before exiting. A resting order can require manual
cleanup.

Add an authority-bound recovery path that queries exact client order ID/open
orders, cancels any discovered remainder, and writes a terminal incident
receipt. A failed sample must still remain failed.

### B-3 — ambient proxy/CA environment is not explicitly closed

The shared client fixes and allowlists the URL host, but after
`curl_easy_reset()` it does not explicitly disable ambient proxy use or bind a
reviewed CA path (`src/client.cpp:361-388`). Production service configuration
must either scrub proxy/CA-related environment variables or the client must
set explicit no-proxy and TLS verification controls before credentials are
loaded and signed requests are sent.

### B-4 — IOC_EXIT remains deliberately absent

This is a correctly reported blocker:

- default dry-run reports `three_path_latency_ready=false`;
- `--execute-ioc-exit` exits 2 before runtime, credentials, or socket;
- PLACE/CANCEL output reports
  `PLACE_CANCEL_RECONCILED_IOC_EXIT_MISSING`;
- the producer requires a real, fully filled, reduce-only, position-flattening
  IOC sample.

Implementing IOC_EXIT requires a separate exact one-shot financial authority,
an independently audited executor, real fee/fill/order/position receipts, and
the same durable nonce controls described above.

## Attack matrix

| Attack | Result |
|---|---|
| Default invocation reaches credentials/network | Rejected; dry-run is socket-free and does not claim READY |
| Missing IOC is laundered into three-path READY by C++ | Rejected |
| `--execute-ioc-exit` reads credentials or sends | Rejected before both |
| CLI self-reports C++ code/config/host/clock hashes | Rejected |
| Mutable/symlinked C++ root controls | Rejected by root owner/mode/component checks and `O_NOFOLLOW` |
| C++ hostile output substitution | `openat(O_EXCL|O_NOFOLLOW)` and directory fd protect the reserved leaf |
| Python symlinked input/output parent or existing output | Rejected |
| Python partial bundle publication | Paths are reserved before writing; tested |
| EC2 instance mismatch in C++ host receipts | Rejected against local DMI/device-tree instance ID |
| Authority replay in final evidence | **Accepted; P0-1** |
| Duplicate IOC lifecycle | **Accepted; P0-1** |
| Execution identity deletes ledger and retries | **Possible; P0-2** |
| Publisher proves ledger exists and matches | **Absent; P0-2** |
| Authority expires during warmup/preflight | **PLACE can still transmit; P0-3** |
| Clock receipt contents match trace clock ID/current time | **Not proven by final producer; P0-4** |
| Root promotion proves actual producer origin | **Not by ownership alone; P0-4** |
| Official Get Order response parses in live path | **Not proven; B-1** |
| Lost POST response is deterministically reconciled | **Absent; B-2** |

## Reproduced tests

All commands ran against a detached worktree at the exact audited commit. No
AWS credentials were read, no account API was called, and no order was sent.

```text
make build/pnl_latency_probe
PASS

python3 -m unittest \
  tests.test_pnl_latency_probe_safety \
  tests.test_pnl_spine_measured_latency_producer -v
29/29 PASS

python3 -m unittest \
  tests.test_pnl_spine_measured_latency_producer \
  tests.test_pnl_spine_latency_evidence -v
27/27 PASS

python3 -m unittest discover -s tests -p 'test_pnl_spine*.py' -q
69/69 PASS

python3 tools/check_registry.py
PASS: 60 tools, 48 build targets; pnl_latency_probe is console-forbidden

git diff --check 6b3974e^ 6b3974e
PASS
```

The green tracked tests do not cover exact per-authority cardinality, duplicate
IOC order references, deletion/replay of the execution-owned ledger,
mid-flight authority expiry, or the official Get Order response bytes.

## Audited hashes

| Artifact | SHA-256 |
|---|---|
| `Makefile` | `aa116dbb4e4d08c7d551206ad42fecf06617ddcc85ec1025095af2641d448d8f` |
| `apps/pnl_latency_probe.cpp` | `d272341fae4050555ea69f70f4740087775cc8cd8929617a0e5bb0872f5271c3` |
| implementation report | `93d6006965f1bad5ef77a889d0c39f482f6967530cc78263671b104d47937ba7` |
| `tests/test_pnl_latency_probe_safety.py` | `faefffff8b92aebc082df07ff40d0638cad63f4b57a10d38b5dd32a8bca7b57e` |
| `tests/test_pnl_spine_measured_latency_producer.py` | `dbca9a22f8d33db69877e7a9d079eaa867f38e533925e439325bef0b6fa95865` |
| `tools.json` | `0ce04e96e8e37fa6ea17d344424da1b0ca12957499f63738680847da29f8a365` |
| `tools/research/pnl_spine/measured_latency_producer.py` | `fd1776513253e3594775884de0d5fd166dda013ad4e67ab2ebfa8275188ea25c` |

The locally built binary SHA was
`2987a27566b81dddad6f7a012b9c156210d998a536bf5ea74191c8e071e556a6`;
it is an audit-machine build receipt, not a production deployment pin.

## Release decision

Do not execute the live PLACE/CANCEL branch from `6b3974e`, do not install it
as the production latency evidence authority, and do not accept a READY
receipt produced by this version.

Re-audit may begin after:

1. exact per-authority sample cardinality and lifecycle uniqueness are enforced;
2. authority and durable consumption records are loaded and validated by the
   final producer;
3. mutation-time expiration and durable nonce consumption are fixed;
4. clock/context/root-promotion provenance is bound, not merely named by SHA;
5. exact real Get Order bytes and ambiguous-POST cleanup are covered;
6. the separate IOC_EXIT executor is implemented and independently audited.
