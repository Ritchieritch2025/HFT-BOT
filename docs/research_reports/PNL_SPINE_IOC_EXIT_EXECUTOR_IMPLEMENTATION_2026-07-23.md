# PnL Spine IOC_EXIT executor implementation

Date: 2026-07-23
Scope: code, offline mocks, and documentation only
Financial mutations performed: **0**
Credentials read: **0**
Kalshi exchange/account API calls made by this work unit: **0**

## Outcome

`apps/pnl_latency_probe.cpp` now contains a production-shaped Kalshi V2
reduce-only full-flatten IOC executor.  Its release gate remains compile-time
`false`:

```text
kIocExitExecutorIndependentlyAudited = false
kIocPretradeFeeScheduleBound = false
```

Therefore `--execute-ioc-exit` still refuses before reading authority files,
credentials, or network configuration and before creating the account lock,
consumption ledger, terminal receipt, or trace fragment.  This implementation
is an audit candidate, not live authorization.

The pure transition system is isolated in
`apps/pnl_ioc_exit_state.hpp`.  It can be exhaustively tested without a socket,
credential, AWS resource, or exchange account.

## Exact execution contract

The executor follows the official Kalshi V2 contracts:

- `POST /portfolio/events/orders`;
- the V2 side is the YES book: positive YES position exits with `ask`,
  negative YES position exits with `bid`;
- `reduce_only=true`;
- `time_in_force=immediate_or_cancel`;
- no `expiration_time`;
- `post_only=false`;
- `cancel_order_on_pause=true`;
- `self_trade_prevention_type=taker_at_cross`;
- exact authority-bound fixed-point count and limit price;
- a separate exact pre-trade maximum-fee fact no greater than the authority
  cap; the dormant production adapter deliberately supplies this as unbound;
- primary subaccount `0`, exchange index `-1` so the official ticker selects
  the exchange automatically.

References:

- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/api-reference/orders/get-order
- https://docs.kalshi.com/api-reference/portfolio/get-positions

The client order ID is deterministic and immutable.  It is a UUID-shaped
encoding derived from the one-shot transaction identity, ticker, action, and
subaccount.  The executor never generates a new identity for recovery and
never retries the POST.

## Proof required before READY

The successful path requires every one of the following:

1. A fresh exact-position GET for the authority ticker and subaccount.
2. A nonzero signed position whose absolute fixed-point count equals the
   authority quantity.
3. A second exact-position GET immediately before the only POST.
4. A valid 201 create ack containing the exact client identity, a valid known
   order ID, full `fill_count`, zero `remaining_count`, matching-engine time,
   average fill price, and average fee.
5. A bounded GET by that exact known order ID.  There is no GetOrders list
   fallback and list absence is never evidence.
6. Exact order identity binding: order ID, client order ID, ticker,
   subaccount, requested count, filled count, and remaining count.
7. Exact fixed-point cross-recalculation between create-ack averages and
   Get Order aggregate fill cost and fees; IOC maker cost and maker fee must
   both be zero.
8. Authority price, fee, and cash bounds plus a separately bound pre-trade
   fee maximum; absence of that fact refuses before the state machine's
   immediate pre-position GET and, critically, before the IOC POST.
9. A post-order position GET proving the signed position is exactly zero.
10. Causal monotonic timestamps and an exchange timestamp inside the consumed
    authority transaction and trusted clock window.

Only that state is `IOC_EXIT_RECONCILED`.  The implementation emits one
private IOC fragment.  It explicitly reports
`fragment_only_not_three_path_ready=true`; it is not by itself a complete
PLACE/CANCEL/IOC_EXIT measured-latency publication.

## Ambiguous and partial outcomes

After the POST, the only permitted operations are at most three read-only
reconciliation attempts using:

- `GET /portfolio/orders/{known_order_id}`;
- exact ticker/subaccount position GET.

There is no POST retry, second flatten order, cancel mutation, or list search.

- If the POST response is lost and no trustworthy order ID exists, the result
  is unresolved and locked.
- If a known order ID proves full fill and a zero position but the causal 201
  ack is missing, the state is
  `BLOCKED_RISK_RESOLVED_EVIDENCE_NOT_PUBLISHABLE`.
- Partial fill, nonzero remaining count, residual position, flipped position,
  identity drift, fee mismatch, price mismatch, timeout, and malformed JSON
  are explicit BLOCKED states and never READY.
- An unambiguous 4xx rejection is a failed measurement, not a latency sample.

## Persistent mutation lock

Before authority consumption or credential access, the dormant production
path reserves:

```text
/var/lib/w09-pnl/account-mutation.lock
```

The file is root-owned, created with exclusive-create semantics, fsynced, and
intentionally remains after both success and failure.  A root supervisor may
remove it only after validating the terminal receipt.

The release gate must not be flipped until deployment proves that the sole
writer credential broker for the account refuses every financial credential
while this file exists.  The repository alone cannot prevent a separate
manual client holding an unrelated API key from trading.  The current false
gate is therefore the correct fail-closed deployment state.

## One-shot authority and immutable receipts

The executor accepts only
`pnl-spine-ioc-exit-authority-v1`, bound to:

- exact binary/config/environment/host/clock/CA hashes;
- exact ticker, receipt ID, quantity, price, cash, and fee limits;
- a maximum 15-minute wall and monotonic deadline;
- `single_use=true`, `allow_ioc_exit=true`;
- exactly one allowed attempt and one IOC order;
- exact output, consumption-ledger, and terminal-receipt paths.

Before any credential or network access it persists the root-only
`pnl-spine-ioc-exit-authority-consumption-v1` ledger.  Every exit then produces
an explicit immutable attempt receipt and
`pnl-spine-ioc-exit-authority-terminal-v1`.  Failure receipts contain hashes,
not raw order IDs, request bodies, or response bodies.

## Offline attack coverage

`--self-test-ioc-exit-executor` covers:

- positive and negative signed positions;
- missing, zero, reversed, stale, and wrong-sized pre-position;
- lost POST response with and without a known order ID;
- unambiguous rejection;
- temporary and permanent known-order GET failures;
- exact three-attempt recovery bound;
- order ID, client ID, ticker, and subaccount substitution;
- partial fills, remaining quantity, residual position, and position flip;
- fill-cost, fee, average price, fee-limit, and cash-limit mismatch;
- missing or authority-exceeding pre-trade fee bounds, before POST;
- signed-absolute-value and fixed-point multiplication overflow;
- malformed order JSON, pagination ambiguity, and duplicate positions;
- exact V2 body safety fields;
- one POST only and no list-order recovery.

Required commands:

```text
make build/pnl_latency_probe
build/pnl_latency_probe --self-test-ioc-exit-executor
python3 -m unittest tests.test_pnl_latency_probe_safety -v
python3 -m unittest \
  tests.test_pnl_latency_probe_safety \
  tests.test_pnl_spine_measured_latency_producer \
  tests.test_pnl_spine_latency_evidence -v
```

## Remaining release gates

1. Independent adversarial audit of the immutable implementation commit.
2. Trusted pre-trade fee-schedule facts bound to the one-shot authority; the
   current response reconciliation proves the charged fee after execution but
   deliberately does not claim to enforce an unbound fee cap before the POST.
3. W09 sole-writer credential-broker enforcement of the persistent account
   lock.
4. A separately audited fragment assembler/promotion step for the complete
   three-path private trace.
5. A new exact, single-use operator financial authority for any real order.

Until all five are closed, the code remains incapable of sending IOC_EXIT.
