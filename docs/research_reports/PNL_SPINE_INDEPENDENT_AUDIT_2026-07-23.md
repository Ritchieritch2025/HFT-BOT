# PnL Spine Independent Integration Audit — 2026-07-23

## Decision

**FAIL — BLOCKED_FOR_NET_PNL**

The reviewed PnL components contain several sound, fail-closed primitives, but
the integrated runner can emit `NET_PNL_COMPLETE` from internally consistent
yet unauthenticated or semantically contradictory fixtures. It must not be
used to publish real strategy NetPnL until every P0 below is closed and the
immutable repaired HEAD passes a fresh adversarial audit.

This audit changed no runtime or test code. The only audit write is this
report.

## Audit identity and scope

- Initial immutable target: `bcd3d91` on `w-pnl-spine-v1`.
- Accepted post-audit account-precision correction:
  `f3d72ed49672919be523b29a6e0546a71e8ea3ac`.
- Subsequent evidence commits reviewed:
  `5bcdaa2` (redacted actual historical fee aggregate) and `b387770`
  (latency evidence inventory and fail-closed blocker receipt).
- Target includes experiment freeze/adapters, MoneyE6 core ledger, fees,
  offline fee facts, strict public fills, exact L2 IOC, terminal handling,
  provenance, preflight, runner, redacted private-fee capture, exact real-data
  inventory, and empirical account-precision evidence.
- Review dimensions: fixed-point units and YES/NO direction; fees and
  rounding; measured-latency gate; strict fills; L2 IOC and settlement;
  baseline/no-trigger/no-fill zero retention; source/release binding; and
  A01/A11/B09 frozen semantics.
- Primary adversarial objective: find any input that should be blocked but
  instead returns `NET_PNL_COMPLETE`.
- Independence disclosure: the auditor authored the runner commit
  `1116170`. This is a separate adversarial review of the integrated immutable
  result, but it is not a blinded third-party sign-off. A new agent should
  re-run the attacks after repair.

## Test evidence

At target `bcd3d91`:

```text
PYTHONPYCACHEPREFIX=/private/tmp/pnl_spine_audit_pycache \
python3 -m pytest -q \
  tests/test_pnl_spine*.py tests/test_capture_private_fee_truth.py

129 passed in 0.96s
```

Passing tests do not overturn the audit result. The adversarial reproductions
below exercise cross-module trust and semantic gaps not covered by those
tests.

Accepted account-precision correction at `f3d72ed`:

```text
PYTHONPYCACHEPREFIX=/private/tmp/pnl_spine_audit_pycache \
python3 -m pytest -q tests/test_pnl_spine_account_precision.py

4 passed in 0.03s
```

Latency evidence audit at `b387770`:

```text
25 passed
state = LATENCY_BLOCKED
PLACE valid causal samples = 0
CANCEL valid causal samples = 0
IOC_EXIT valid causal samples = 0
```

The latency result is correct and conservative: ping/HTTP GET RTT, CPU signing
benchmarks, and retrospective order/fill inventories are not treated as
decision-to-effective real-order latency.

## Findings

| ID | Severity | Result | False-complete consequence |
|---|---:|---|---|
| P0-01 | P0 | FAIL | Self-authored fee schedules and receipts can declare zero fees and complete NetPnL. |
| P0-02 | P0 | FAIL | Rows, trades, books, and terminal records are not bound to exact release objects or dates. Synthetic 1970 data completes against 2026 releases. |
| P0-03 | P0 | FAIL | Runner does not enforce `RiskLedger`, per-root fill caps, A11 max hold, or dynamic cancel rules. |
| P0-04 | P0 | FAIL | IOC closure can cherry-pick an older favorable snapshot even when a newer valid snapshot exists at effective time. |
| P0-05 | P0 | FAIL | Same market can use mutually contradictory finalized settlement records across paths. |
| P0-06 | P0 | FAIL | Receipt hashes prove integrity but not authority; synthetic latency/fee/release/terminal/card/code claims can call themselves production truth. |
| P1-01 | P1 | RESOLVED | `f3d72ed` now keeps fee/principal arithmetic diagnostic and emits `INCONCLUSIVE` without an observed posted balance delta or official account class. |
| P1-02 | P1 | FAIL | A01 two-leg strategy rows and one-leg abstain rows have different strategy-row denominators; baseline is one row per decision. |
| P1-03 | P1 | FAIL | Core and preflight expose different latency receipt/path models; runner does not consume the core measured-latency profile. |
| C-01 | Control | PASS | MoneyE6 arithmetic, strict-through inequality, per-public-trade allocation, ledger cash/position identities, residual blocking, and B09 freeze are locally sound under trusted inputs. |
| C-02 | Control | PASS | `b387770` honestly blocks all three latency paths because no complete real-order causal traces exist. |

## P0-01 — Fee authority can be replaced by a self-consistent zero schedule

The runner parses caller-supplied `fee_rules`, then considers them verified
when their deterministic SHA equals a value inside the caller-supplied fee
receipt (`runner.py:985-998`). The preflight checks schema, nonempty formula
identifiers, booleans, and the receipt's self-hash, but it does not validate
official rate constants or resolve event/series history
(`preflight.py:878-1047`).

The separately audited `FeeFacts` implementation does validate official
rates, series changes, event waiver/override truth, account precision, and
capture time (`fee_facts.py:428-607`), but the runner never loads or invokes
it.

Adversarial reproduction:

1. Start from the passing complete runner fixture.
2. Set both maker and taker `rate_e4=0` and `multiplier_e4=0`.
3. Recalculate the fee-schedule SHA and self-hash the fee receipt.
4. Run the unchanged runner.

Observed:

```json
{"blockers":[],"fee_cost_e6":0,"state":"NET_PNL_COMPLETE"}
```

This is a direct false-complete path. SHA consistency cannot substitute for
fee authority.

Commit `5bcdaa2` adds a useful redacted aggregate of 372 authenticated
historical fills and their actual `fee_cost` total. It validates that real fee
data exist; an aggregate does not by itself determine the correct fee for
each simulated fill, its series/event waiver state, or account rounding
behavior.

Required closure:

- Runner must consume a pinned canonical `FeeFacts` object, not arbitrary
  materialized `FeeRule` rows.
- Every fill must resolve series, event, effective interval, waiver/override,
  liquidity role, official rate, account precision, and private-fee
  precedence through that object.
- The facts file SHA and external authority pins must be supplied outside the
  mutable run fixture.
- Add a regression in which a zero-rate self-signed schedule is rejected.

## P0-02 — Economic rows are not members of the bound exact releases

`RunBinding` validates the syntax and internal equality of release metadata
(`provenance.py:144-184`). The repository also has a strong
`validate_local_payloads` implementation that verifies local files by size
and SHA (`provenance.py:187-229`), but the runner never calls it.

More importantly:

- normalized rows carry no exact source-object identity;
- public trade and L2 snapshot `source_sha256` values are only checked for
  shape and need not appear in any `RunBinding` release object;
- settlements need not be part of a settlement release object;
- row/trade/snapshot timestamps are not checked against the three release
  dates;
- `code_sha256` is accepted as any 64-hex string rather than being compared
  with the deployed runtime.

The runner's own passing test fixture proves the escape:

```json
{
  "state": "NET_PNL_COMPLETE",
  "fixture_decision_utc": "1970-01-12T13:46:40Z",
  "release_dates": "2026-07-12..2026-07-17",
  "code_sha256_is_dummy": true,
  "latency_source_is_dummy": true,
  "blockers": []
}
```

The same fixture uses entry/exit source hashes not present in its sole L2
release-object hash and still completes.

The exact real-data audit provides the opposite, honest result:

- audit SHA:
  `f2ce956846ae12ec3bccc951566876ccd1e4c7b0cd759b88510dcf1131922125`;
- classification: `ENGINEERING_INPUT_ONLY_NOT_NET_PNL`;
- dates: 2026-07-12, 2026-07-15, 2026-07-17;
- A01/A11/B09 normalized rows: 0;
- exact exits: 0;
- NetPnL rows: 0.

The runner does not consume this audit as a mandatory gate. Therefore its
synthetic complete fixture contradicts the authoritative real-input status.

Required closure:

- Require a root-owned/pinned real-data audit receipt whose source inventory
  is part of `RunBinding`.
- Every normalized row, public trade, exact snapshot, and settlement must
  carry an object identity `(logical_key, VersionId, SHA-256)` present in the
  exact releases.
- Validate materialized bytes with `validate_local_payloads`.
- Enforce UTC date membership for every economic timestamp.
- Compare runtime/code SHA to a value outside the fixture.
- Until direct normalized inputs and exits exist, output only
  `ENGINEERING_INPUT_ONLY_NOT_NET_PNL`.

## P0-03 — Frozen root risk and lifecycle rules are not executed

Frozen intents carry `root_entry_quantity_cap_e4` and A11 carries
`max_hold_ms` (`experiments.py:325-343`). A01 freezes a two-contract root cap
(`experiments.py:879-903`); A11 freezes one fill per root and a 120-second
maximum hold. The core contains `RiskLedger`, but the runner neither imports
nor uses it. It only stores the risk policy's SHA.

Adversarial reproduction:

1. Supply two valid A01 decision rows with the same `root_event_id`.
2. Supply four distinct strict-through public trades.
3. Supply enough exact L2 depth to close all four fills.
4. Adjust the self-authored terminal path count to six.

Observed:

```json
{
  "blockers": [],
  "entry_filled_quantity_e4": 40000,
  "frozen_root_cap_e4": 20000,
  "path_count": 6,
  "state": "NET_PNL_COMPLETE"
}
```

The runner also ignores:

- A11 `max_hold_ms`;
- A11 cancel-on-side-return, move, activity burst, unknown rule, gap, pause,
  and TTS boundary;
- A01 safety-only cancel/requote events after decision time.

This can manufacture fills after a frozen policy should have canceled and can
double the admitted root exposure.

Required closure:

- Integrate `RiskLedger` before fill allocation and reconcile it after every
  fill/exit/settlement.
- Enforce root caps across all rows, not independently per intent.
- Implement an append-only lifecycle state stream and frozen cancel rules.
- Enforce A11 hold timeout and the exact reduce-only IOC exit time.
- Add repeated-same-root and post-cancel-fill regression attacks.

## P0-04 — Exact L2 IOC does not require the latest snapshot at effective time

The single-snapshot walker correctly rejects future, stale, invalid, gapped,
locked, and crossed books and walks executable depth in price order
(`fills.py:402-467`). Portfolio depth reuse is also checked.

The integration gap is snapshot selection. The closure chooses a
`snapshot_id`, and the runner passes that object directly to the walker
(`runner.py:1364-1407`). It does not prove that this snapshot is the latest
valid receive-clock state at or before the latency-adjusted IOC effective
time.

Adversarial reproduction:

1. Keep the favorable exit snapshot selected by the closure.
2. Add a newer, valid, gap-free but materially worse snapshot for the same
   market, with receive time equal to IOC effective time.
3. Run unchanged.

Observed:

```json
{"blockers":[],"gross_pnl_e6":-40000,"state":"NET_PNL_COMPLETE"}
```

The newer authoritative book was silently ignored.

Required closure:

- The runner, not the fixture, must select the maximal receive sequence/time
  not later than effective time from the exact event stream.
- Require sequence-contiguous reconstruction and reject duplicate or
  incomparable latest states.
- Bind the selected snapshot identity and the selection proof in the receipt.
- Add an older-favorable/newer-adverse regression.

## P0-05 — Contradictory finalized settlements can both close

Settlement handling is fail-closed within one ledger: non-final or unknown
records censor, residual positions block, and exact final payouts close.
However, terminal consistency is path-local. Separate paths on the same
market may choose different finalized settlement IDs, revisions, and values.

Adversarial reproduction:

1. Fill both A01 complementary legs on one market.
2. Close the YES path with a finalized YES value of 1.
3. Close the NO path with a different finalized record whose YES value is 0.
4. Both records use valid-shaped but unrelated source hashes.

Observed:

```json
{
  "blockers": [],
  "gross_pnl_e6": 1060000,
  "settlement_closures": [
    "CLOSED_BY_SETTLEMENT",
    "CLOSED_BY_SETTLEMENT"
  ],
  "state": "NET_PNL_COMPLETE"
}
```

The two complementary paths received a combined $2 payout from one binary
market.

Required closure:

- Resolve one canonical highest finalized revision per market.
- Derive NO payout only as the complement of that same YES payout.
- Require all paths on a market to bind the identical terminal source object
  and revision.
- Reject conflicting finalized records before any ledger finalizes.

## P0-06 — Integrity hashes are mistaken for authority

Preflight verifies receipt shape, causal timestamp ordering, required path
names, non-placeholder hashes, and canonical payload SHA. These are valuable
integrity checks. They do not prove that:

- latency samples came from actual orders on the execution environment;
- fee history came from the pinned official snapshots;
- terminal values came from the venue;
- release metadata came from the exact S3 objects;
- the card-parameter artifact contains the claimed training outputs;
- the risk-policy SHA identifies the installed policy.

All of these documents live inside the same caller-controlled fixture
(`runner.py:900-1094`). A caller can generate every document and every
content hash together. The complete test fixture does exactly that and is
accepted as `REAL_ORDER_MEASURED`.

Commit `b387770` now supplies an honest production-evidence answer:
`LATENCY_BLOCKED`, with zero valid causal samples for PLACE, CANCEL, and
IOC_EXIT. That receipt is a useful control, but the runner must consume it
from an external trust anchor; accepting a separately self-authored
`REAL_ORDER_MEASURED` fixture would still be a false-complete escape.

Required closure:

- Separate evidence from the experiment fixture.
- Load each authority only from a root-owned, non-symlink, immutable path with
  an expected external SHA.
- Add producer identity/signature or deployment-attestation checks where an
  external SHA is not available.
- Bind latency environment fingerprint to the actual execution runtime.
- Never allow a fixture field to promote itself from scenario to measured.

## P1-01 — Account precision evidence was circular; accepted correction

At the initial target, `account_precision.py` joined real private fills to
orders but did not read an actual posted account-balance delta. It calculated:

```text
signed_balance_change = ±notional - fee_cost
```

and then declares direct-centicent precision when that calculated value is
centicent-aligned but not cent-aligned.

This cannot distinguish:

- a direct account posting the sub-cent value; from
- a non-direct account subsequently flooring to cents and applying its
  rounding accumulator/rebate.

The 14/14 centicent alignment observation proves only that the input notional
and `fee_cost` arithmetic has that granularity. It does not prove what the
venue posted to the account balance.

Required closure:

- Observe exact before/after posted balance changes or an authoritative
  account-class field; and
- reconcile each observed balance delta to principal, actual fee, rounding
  fee, accumulator, and rebate.

The finding was accepted and corrected in exact commit
`f3d72ed49672919be523b29a6e0546a71e8ea3ac`. The implementation now requires
a separately captured `observed_balance_change_e6` on every matched row
before it can claim `DIRECT_CENTICENT`; fee/principal arithmetic is retained
only as a diagnostic.

The regenerated `ACCOUNT_PRECISION_2026-07-23.json` now honestly records:

```json
{
  "state": "INCONCLUSIVE",
  "claim_tier": "ARITHMETIC_DIAGNOSTIC_NOT_ACCOUNT_AUTHORITY",
  "account_balance_precision": null,
  "matched_actual_fill_count": 14,
  "observed_balance_change_fill_count": 0,
  "precision_blocker":
    "MISSING_ACTUAL_POSTED_BALANCE_DELTA_OR_OFFICIAL_ACCOUNT_CLASS"
}
```

This closes the false authority claim. It does not establish the account
precision needed for complete fee assessment; that remains an explicit data
blocker rather than an implementation misstatement.

## P1-02 — Strategy/baseline row denominators are not one-to-one

One baseline row is emitted per decision row (`runner.py:1219-1244`). An A01
trigger emits two independent strategy path rows, while an abstain emits one
strategy zero row and a triggered/no-fill opportunity emits one zero per
intent. Path-level averages, win rates, and counts therefore have different
denominators between strategy and baseline and between trigger states.

Required closure:

- Define one opportunity-level strategy result that aggregates all frozen
  legs, plus exactly one same-opportunity baseline result.
- Retain leg ledgers as drill-down rows, not as opportunity denominators.
- Add count conservation:
  `decision_rows == strategy_opportunity_rows == baseline_opportunity_rows`.

## P1-03 — Two incompatible latency contracts exist

The core latency model uses `PLACE`, `CANCEL`, and `EXIT` and can create a
`MeasuredLatencyProfile`. Preflight uses `PLACE`, `CANCEL`, and `IOC_EXIT`
with a different receipt schema. The runner parses the preflight document
again and never constructs the core profile.

Required closure:

- Establish one canonical measured-latency schema/path vocabulary.
- Make preflight, runtime timing, and run binding consume the same parsed
  object.
- Reject any conversion that loses source-event or environment bindings.

## Controls that passed

Subject to trusted inputs, the following local properties passed inspection
and tests:

- price and quantity use E4; money uses E6; floats are rejected at the
  economic boundary;
- notional and settlement arithmetic fail closed when not exactly
  representable;
- official fee formula arithmetic and centicent rounding worked examples pass
  in the standalone fee core;
- SELL YES to BUY NO complement has equivalent principal, binary payout, and
  symmetric quadratic fee direction;
- passive entry fills require a later matching public trade strictly through
  the quote;
- one public trade ID is globally allocated at most once;
- exact IOC walks the chosen valid snapshot in executable price order;
- aggregate L2 level quantity cannot be reused across exit paths;
- open residuals and provisional/unknown settlements do not publish totals;
- cash, fee, variable-cost, position, and result hash identities are checked;
- baseline, abstain, and triggered/no-fill zero rows are retained;
- frozen experiment and revision hashes are pinned;
- B09 cannot produce an order before a new trained immutable revision;
- the current real-data adapter honestly labels C1/Deep03 as engineering-only
  and reports zero normalized/exact-exit/NetPnL rows.

## Current real-data readiness

| Experiment | Candidate rows | Normalized rows | Strict-fill rows | Exact exits | NetPnL rows | Honest state |
|---|---:|---:|---:|---:|---:|---|
| A01-SPREAD-CAPTURE | 1,116,054 | 0 | 33,624 | 0 | 0 | `BLOCKED_SOURCE_FIELDS` |
| A11-ONE-SIDED-PROVISION | 896,920 | 0 | 28,680 | 0 | 0 | `BLOCKED_SOURCE_FIELDS` |
| B09-LISTING-TO-START-DRIFT | 0 | 0 | 0 | 0 | 0 | `BLOCKED_SOURCE_FIELDS` |

Therefore the present system has reusable engineering evidence, not three
completed end-to-end real PnL experiments.

## Mandatory repair and re-audit gate

No `NET_PNL_COMPLETE` receipt is admissible until all of the following are
true:

1. Fee assessment is exclusively driven by externally pinned `FeeFacts`,
   including event/waiver history. Account precision must either be supported
   by observed posted balance deltas or remain explicitly inconclusive.
2. Every economic row and terminal record is proven to belong to exact
   release objects and dates; local bytes are verified.
3. Measured latency is loaded from a trusted producer path and bound to the
   run environment.
4. Risk/root caps, cancel rules, and max hold are executed, not represented by
   hashes alone.
5. IOC uses the uniquely latest valid receive-clock snapshot.
6. One market has one canonical finalized terminal value/revision.
7. Opportunity-level strategy and baseline row counts are one-to-one.
8. The current real-data audit changes from zero normalized/exact-exit rows to
   direct, source-bound inputs without inference.
9. All attacks in this report become regression tests that return
   `PNL_BLOCKED`.
10. A blinded auditor replays the attacks against one immutable repaired HEAD
    and records its exact commit and test results.

Until that gate passes, the only permitted claim is:

> The PnL spine contains audited engineering primitives and honest blocker
> receipts; it does not yet contain publishable real strategy NetPnL.
