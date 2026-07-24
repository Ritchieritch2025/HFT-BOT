# PnL Spine latency boundary repair-02 independent re-audit

Date: 2026-07-23

Audited commit: `70a4ab5d2df24ee87cbef87cb3bf3c73d873b690`

Audited parent: `d051a61fa1cafaaa8eb447b0ef29a45a9336c59b`

Audited tree: `a4a6660b751feae7647aca0b11fc6c9f7bfcf6a8`

Verdict: **PASS — current binary provably hard-refuses all live
PLACE/CANCEL transmission**

## Scope of this PASS

This is a PASS for a release lock, not a PASS for live latency collection.

The audited binary does not implement a safe ambiguous-POST recovery path.
Instead, it places a compile-time `false` gate at the first statement of
`execute_place_cancel()`. Every invocation of `--execute-place-cancel`
therefore returns 2 before validating options, reading authority or deployment
artifacts, creating a root consumption ledger or output, reading credentials,
constructing a client, opening a socket, signing a request or transmitting an
order.

The old PLACE/CANCEL implementation remains in source only as unreachable
scaffolding. Changing the compile-time gate, moving it, adding another
mutation entry point or building from different source invalidates this PASS
and requires a new independent audit.

`--execute-ioc-exit` also remains hard-refused before runtime, credentials or
network. The separate `--ioc-exit-preflight` mode is authenticated read-only
network I/O and was not invoked during this audit.

No network was contacted, no credential was read and no order was sent.

## Immutable audit object

| Object | SHA-256 / identity |
|---|---|
| Commit | `70a4ab5d2df24ee87cbef87cb3bf3c73d873b690` |
| Parent | `d051a61fa1cafaaa8eb447b0ef29a45a9336c59b` |
| Tree | `a4a6660b751feae7647aca0b11fc6c9f7bfcf6a8` |
| Commit patch | `b1c8dfe42a1c90f42685302f42b16fcf5afdf93b0d310f76024d96854172133d` |
| `apps/pnl_latency_probe.cpp` | `0a11ddb6c9f6dbb0b024ef065a1c3bffc7be5d2d8b8f0fd98d522aab9c95afda` |
| C++ safety tests | `821f722e3fa129ec87bef9e8fb35a87cd7e15478ebd4fa555ade2bee9bc387b0` |
| `tools.json` | `5496c9e9217e6d76e066079ca4ec049b53b910807c9460b345529cd7e509bc4e` |
| implementation report | `4520be4eef953949c4b1aa4db93c9d63ecb421c75d2cc1f170f10eb9506abb04` |
| unchanged publisher | `9bd94414b1b485e3ac793b7d844d523ae6aea3a21e4386593ed1c77800fe25cd` |
| unchanged publisher tests | `5b31b142b6e9e333df2156cfb5cb570882b8341c608e16ef539e041ce74244a2` |

The patch digest was independently reproduced:

```text
git show --format= --no-ext-diff --binary 70a4ab5 | shasum -a 256
b1c8dfe42a1c90f42685302f42b16fcf5afdf93b0d310f76024d96854172133d
```

## Unbypassable early refusal

The release constant is fixed in the audited source:

```text
apps/pnl_latency_probe.cpp:85-91
constexpr bool kAmbiguousPlaceRecoveryProven = false;
```

The first executable statement in the PLACE/CANCEL function is:

```text
apps/pnl_latency_probe.cpp:1821-1831
if constexpr (!kAmbiguousPlaceRecoveryProven) {
    ...
    return 2;
}
```

Everything capable of touching external state occurs after that unconditional
return:

| Later operation | First location |
|---|---|
| option/required-pin validation | `1822` gate precedes `1832` |
| code/config/environment/host/clock reads | gate precedes `1839` |
| raw authority read and validation | gate precedes `1847-1848` |
| root ledger/output/terminal reservations | gate precedes the dormant broker block |
| credential environment access/private-key read | gate precedes the dormant client block |
| client construction, ping and account GET | gate precedes the dormant network block |
| PLACE signing and `lane.send()` | gate precedes the dormant mutation block |
| private trace/terminal output completion | gate precedes the dormant publication block |

`main()` reaches `execute_place_cancel()` only for the explicit
`--execute-place-cancel` mode. A simultaneous IOC preflight request is rejected
as a conflicting mode first. A simultaneous `--execute-ioc-exit` is caught by
the IOC hard refusal first. Default invocation remains a credential-free,
socket-free dry run.

The optimized audit build also contained the hard-refusal string but not the
dormant PLACE-specific `authority refused` or `PLACE outcome ambiguous`
strings. That binary observation is defense in depth; the source-level
unconditional return is the controlling proof.

## Independent hostile-input replay

An audit-only subprocess supplied:

- a syntactically complete `--execute-place-cancel` command;
- prod/live environment flags;
- nonempty credential identifiers;
- authority, config, environment, host, clock and private-key paths implemented
  as blocking FIFOs;
- all three nonexistent output paths;
- an attacker proxy override.

If the process attempted to open any control or credential FIFO, it would
block and hit the two-second subprocess timeout. If it entered the root broker,
one or more output leaves would appear.

Observed result:

```text
return code: 2
stderr: PLACE/CANCEL LIVE TRANSMISSION IS DISABLED AND FAILS CLOSED ...
control/private-key FIFOs: untouched and still FIFOs
trace/ledger/terminal outputs: all absent
HARD_REFUSE_BEFORE_ALL_EXTERNAL_INPUTS PASS
```

This directly replays the options/authority/root-ledger/credentials/output
boundary without using valid credentials, AWS, DNS or an exchange endpoint.
The exact source order proves no network constructor or request is reachable.

## Ambiguous POST state matrix

The commit adds a pure, offline recovery predicate. Its only nominally safe
state is:

- exact client order found;
- cancellation confirmed;
- terminal readback complete;
- requested quantity exactly one contract;
- no fill before cancellation;
- no remainder after cancellation;
- position exactly unchanged.

The self-test replays all required outcomes:

| Outcome | Predicate result | Live result |
|---|---|---|
| order exists, clean cancel, exact flat readback | true | still hard-refused because release gate is false |
| order not found | false | hard-refused before POST |
| order query timeout | false | hard-refused before POST |
| cancel timeout/unknown | false | hard-refused before POST |
| partial fill/residual position | false | hard-refused before POST |

Observed:

```text
AMBIGUOUS POST RECOVERY SELF-TEST PASS
```

Because at least four possible real outcomes are not provably safe and the
repository has neither an audited account-level trading block nor a
position-flattening IOC transmitter, keeping the release gate false is the
correct fail-closed decision. An ambiguous POST cannot arise from this binary
because no PLACE POST is reachable.

## Historical authority and evidence attacks

The repair does not weaken the independently verified publisher and
single-use evidence boundaries from commit `264a9dc`.

The following prior attacks were replayed by the tracked producer tests and
remained rejected:

| Attack | Result |
|---|---|
| second PLACE/CANCEL pair under one authority | rejected by exact path cardinality |
| duplicate IOC sample/order lifecycle | rejected by exact cardinality and lifecycle uniqueness |
| PLACE and IOC reuse one nonce or client order identity | rejected |
| missing/tampered consumption or terminal receipt | rejected |
| mutation count exceeds one | rejected |
| execution UID deletes/recreates root ledger | dormant broker design remains root-owned/non-deletable; current live gate never creates it |
| caller supplies code/config/source/clock SHA | production CLI does not expose those flags |
| trace rewrites SHA and refreshes trace-local receipt | rejected against raw authority and promotion chain |
| future clock, trace or exchange time | rejected |
| symlink, duplicate JSON, float, unknown field or partial publication | rejected |

The publisher can still construct READY from a complete, valid, independently
promoted three-path evidence graph. This PASS does not claim such a graph
currently exists. Since both mutation transmitters are closed, no new
legitimate full three-path trace can be produced by this binary.

## IOC transmitter and network modes

At `apps/pnl_latency_probe.cpp:2259-2265`, `--execute-ioc-exit` returns 2 with:

```text
IOC_EXIT TRANSMISSION IS NOT IMPLEMENTED AND FAILS CLOSED
```

This occurs before any self-test, runtime, credential or preflight branch.
Supplying fake credential environment values and a nonexistent key path did
not change the result or cause a key-read error.

The read-only IOC preflight remains a separate reachable mode. It may perform
authenticated GET requests if explicitly invoked with real credentials. It is
not an IOC transmitter and was not invoked in this audit.

## Reproduced tests

All tests ran in a detached worktree at the exact audited commit.

```text
make build/pnl_latency_probe
PASS, no compiler warnings

python3 -m unittest \
  tests.test_pnl_latency_probe_safety \
  tests.test_pnl_spine_measured_latency_producer -v
29/29 PASS

python3 -m unittest \
  tests.test_pnl_spine_measured_latency_producer \
  tests.test_pnl_spine_latency_evidence -v
23/23 PASS

python3 -m unittest discover -s tests -p 'test_pnl_spine*.py' -q
65/65 PASS

python3 -m pytest -q tests/test_pnl_spine*.py
352/352 PASS

python3 tools/check_registry.py
PASS: 60 tools, 48 build targets; pnl_latency_probe remains
console-forbidden as live_order

git diff --check 70a4ab5^ 70a4ab5
PASS
```

The locally built audit binary SHA-256 was
`68b21062a5bbbe4b0a09ad79b193e39b2ce34b999ad82913855a4751d239e2f1`.
It is an audit-machine build receipt, not a production deployment pin.

## Release decision

Commit `70a4ab5` passes the requested stop-loss objective:

> Every real PLACE/CANCEL attempt is rejected before authority, root
> consumption, credentials, network, or output.

It is safe to retain this binary as a disabled scaffold and dry-run/parser
self-test tool. This PASS does **not** authorize:

- a live PLACE/CANCEL authority;
- execution of a real PLACE/CANCEL probe;
- an IOC_EXIT mutation;
- representing three-path latency as currently collected or READY.

Reopening PLACE/CANCEL requires a new immutable commit implementing official
deterministic order recovery, query/cancel ambiguity handling, an enforceable
account-level trading block, exact terminal reconciliation and authorized
partial-fill flattening. That new commit must set or replace the release gate
and receive a fresh independent audit.
