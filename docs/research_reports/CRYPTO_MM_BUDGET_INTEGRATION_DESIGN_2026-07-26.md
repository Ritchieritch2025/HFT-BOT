# Crypto MM absolute-budget integration design — 2026-07-26

Status: implemented locally in `mm_engine.py` plus pure adapter/math modules
and regression tests. No deployment is authorized by this document. Live
ignition remains fail-closed until the remaining accounting and timing
contracts are proven.

## Safety quantity

All money is integer micro-USD and all quantities are integer
micro-contracts.

For market `m`, settlement state `s ∈ {YES, NO}`, and every independently
optional resting/pending/unknown/candidate order `o`:

```text
payoff(o, s) = quantity(o) when o wins in state s, else 0
V(m, s)      = existing_position_payout(m, s)
               + Σ_o min(0, payoff(o, s) - cost(o) - worst_fee(o))
W(m)         = min(V(m, YES), V(m, NO))
E_terminal   = available_balance + other_guaranteed_cash + Σ_m W(m)
E_mark       = available_balance
E_safe       = min(E_mark - $0.01, E_terminal)
```

This is O(N). Each `min(0, ...)` lets an adversary choose the losing fill and
omit the profitable fill, so two complementary orders cannot create a fake
guarantee by being assumed to fill atomically. The order universe is the
union of resting, POST-pending, cancel/submit-unknown, unresolved IOC, and the
proposed candidate.

Server order IDs and client order IDs share one canonical identity registry
across every local namespace. The same raw ID with the same economics is one
reservation reclassified as unknown; conflicting economics or aliases that
join two reservations fail closed.

`portfolio_value` is excluded by default because current official references
conflict on whether it is positions-only or total value. The 2026-07-26
account-bound probe did establish that a resting bid does not reduce
`balance_dollars`; therefore the local order cost/fee reserve is required and
is not a double subtraction. Enabling additive portfolio value requires a
separate verified position-state probe.

Probe receipt:
`tmp/crypto_mm_canary_20260726/resting_collateral_contract_probe_result.json`;
SHA-256
`df08677b830177bd7a8de449f3ca35dd1fa0ad03ba76b3c1ae64cf11dc6c0d98`.

A candidate is allowed only when:

```text
budget.latched == false
and baseline E_safe >= absolute_floor + operator_buffer
and projected E_safe >= absolute_floor + operator_buffer
```

Candidate-only failure rejects that candidate without latching, leaving a
risk-reducing alternative possible. Baseline failure or blind state latches,
cancels, and halts.

## Startup sequence

1. Obtain a stable non-secret account identity. The currently available
   engine field is `KALSHI_API_KEY_ID`; using it intentionally makes key
   rotation block startup. Prefer a stable member/account ID when the API
   exposes and verifies one.
2. Hash identity + subaccount + exchange index with
   `account_fingerprint(...)`.
3. Call `load_bound_budget(...)`. There is no initialize fallback.
4. Missing, corrupt, wrong-account, wrong-subaccount, wrong-exchange,
   inconsistent floor, invalid generation, or latched records block ignition.
5. Fetch a complete exchange snapshot, reconcile REST positions with
   `S.net_pos`, evaluate current reservations, and allow quoting only if
   baseline `E_safe` clears the persisted floor.

Budget creation remains a separate operator action. A restart only loads the
same file. The adapter contains no rebase or unlatch API. File deletion cannot
silently clear a trip: before attempting the latch, the guard atomically
creates a durable `<budget>.trip-pending` marker bound to the immutable budget
identity. A generation-1 budget with that marker blocks restart and retries
the latch. Production should additionally protect the path with host
permissions and retain an append-only/operator receipt outside the process;
simultaneous failure of both local durable writes still requires an external
sticky halt.

## Snapshot acquisition contract

The future engine integration must add `S.risk_generation`. Increment it on
every:

- fill application;
- resting-order insertion, quantity reduction, or removal;
- POST-pending insertion or removal;
- IOC-pending insertion, fill application, or removal;
- transition into or out of unknown order state;
- position reconciliation mutation.

For startup, preflight, and monitor evaluation:

1. capture `generation_before`;
2. fetch every page of
   `/portfolio/positions?count_filter=position&limit=1000&subaccount=0`;
3. fetch every page of
   `/portfolio/orders?status=resting&subaccount=0&limit=1000`, then prove
   each row's server/client identity, ticker, status, remaining quantity,
   outcome/book direction, and YES/NO wire prices against its local reserve;
4. fetch `/portfolio/balance?subaccount=0`, then
   `/exchange/user_data_timestamp`;
5. capture `generation_after`;
6. reject generation races, incomplete pagination, position mismatch, or
   malformed/future/regressing validation metadata;
7. run the pure assessment.

Every preflight and monitor call reloads the durable budget before assessment.
Thus a latch written by another process is visible before the next decision.
The production POST path must invoke preflight immediately before routing and
must enforce a single live engine instance; a file reload alone cannot close
an arbitrary post-check/process race.

The balance fetch is the final portfolio-data read; only the validation-time
metadata request follows it. `balance.updated_ts` is the last account
mutation, not response freshness, so quiet age is valid. The exchange
`as_of_time` is parsed with timezone, future, and monotonic checks. A maximum
age/deadline and nonblocking behavior under latency/429s remain required
before live promotion. Exchange surfaces are not transactionally atomic, so
an ambiguous fill/order state remains reserved rather than assumed absent.

The process writer lock is account/subaccount/exchange-fingerprint bound at a
fixed `/tmp` path and cannot be bypassed by changing `MM_BUDGET_PATH`. It is
same-host advisory coordination only; cross-host exclusion remains an
external deployment requirement.

## Existing field mapping

| Risk concept | Existing source | Required mapping |
| --- | --- | --- |
| Account | `KALSHI_API_KEY_ID` | `account_identity`; stable account ID preferred |
| Subaccount | fill `subaccount_number` / configured default | budget `subaccount`, currently `0` |
| Exchange | balance breakdown | budget `exchange_index`, currently `0` |
| Cash/mark | `GET /portfolio/balance?subaccount=0` | cash-only `balance_dollars`, legacy `balance` cross-check, non-additive-by-default `portfolio_value`, mutation-only `updated_ts`, `balance_breakdown` |
| Positions | `GET /portfolio/positions` | signed `position_fp`: positive=YES, negative=NO |
| Filled ledger | `S.net_pos[mt]` | signed local quantity=`y-n`; must equal REST |
| Resting | `S.orders[(mt, side)]` | `id`, risk `px`, remaining `qty`; `bid`=YES, `ask_no`=NO |
| POST pending | `S.pending_new[(mt, side)]` | `client_order_id`, risk `px`, `qty` |
| Cancel unknown | `S.unknown_orders[order_id]` | reclassify the matching still-reserved `S.orders` entry; do not add it twice |
| IOC unknown | `S.flatten_pending[(mt, orphan_side)]` | buy the opposite side; requires new `risk_px`, `remaining_risk_qty`, `fee_reserve` fields |
| Candidate | final `order_place` arguments | unique preflight reference, ticker, engine side, `risk_px`, quantity, worst fee |

The current `flatten_pending` record has `qty` but not the price, remaining
unapplied quantity, or fee reserve. Therefore it cannot prove terminal cash.
The adapter rejects that shape. Formal engine integration must populate:

```text
risk_px
remaining_risk_qty
fee_reserve
```

For maker/post-only orders, the configured worst fee can be zero only while
the order is guaranteed maker. IOC/taker orders must receive an explicit
conservative fee reserve; the adapter does not guess a fee schedule.

## Exact engine insertion points for a later patch

### Immediately before every POST

After the final control reload and final quantity/risk-price selection, but
before consuming the API request:

1. create the client order ID/reference;
2. capture the generation-stable exchange/local snapshot;
3. call `BudgetGuard.preflight(candidate=...)`;
4. POST only if `assessment.allowed` is true;
5. otherwise emit the full baseline/projected receipt.

Risk-reducing and exit orders are not exempt. They normally pass because
joint terminal guarantee improves, but their taker fee can still make a
specific route unsafe.

### Independent 1 Hz task

Call `BudgetGuard.monitor_once(...)` once per second on fresh data. It does
not rely on quote ticks, fills, or the strategy loop. On baseline breach or
blind data it performs, in order:

1. atomic durable latch;
2. `cancel_all("BUDGET_LATCH")`;
3. set `S.halted = True`, log the reason, and write control status.

Callbacks are injected into the adapter; the pure module has no exchange
client. `cancel_all` must return literal `True`; `False`, `None`, or an
exception leaves the trip pending. Latch, cancel, and halt enforcement retries
on subsequent monitor ticks until the latch is durable, cancel succeeds, a
fresh risk snapshot proves resting/pending/unknown empty, and halt succeeds.
Once all four facts hold, repeated clean monitor calls are idempotent.

### Restart

Constructing `BudgetGuard` only calls `load_bound_budget`. A generation-2
latched record remains blocked across restart, triggers cancel/halt at the
startup gate, and retains the original anchor/max-loss/floor identity. A
generation-1 record accompanied by its identity-bound trip-pending marker is
also blocked; it cannot resume merely because the prior latch fsync failed.

## Test fixtures

`tests/test_mm_budget.py` covers exact balance parsing, immutable record
validation, atomic latch, independent fill-subset settlement, duplicate-hedge
non-additivity, and the historical incident state.

`tests/test_mm_budget_adapter.py` covers account binding, missing-record
failure, no restart rebase, real `position_fp` direction, local/REST
reconciliation, validation timestamp parsing, resting/pending/unknown
de-duplication, IOC missing-field failure, candidate-only rejection,
generation races, cross-namespace raw-ID collisions, cancel failure retry,
latch-write failure and trip-pending restart, cross-process latch reload,
1 Hz single-step trip, and latched restart behavior.

`tests/test_mm_engine_budget_integration.py` covers exact endpoint scoping,
strict remote order economics (including NO=`ask/sell YES` and partial
remaining quantity), account-fixed writer locking, pre-POST candidate
rejection, non-atomic complementary fills, and the explicit live accounting
contract marker.
