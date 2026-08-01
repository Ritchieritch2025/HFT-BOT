# ROUND4 Stage-2 public-proxy preregistration V4.1

Status: `PRE-DATA CONTRACT SEALED / DISCOVERY ONLY / NO FIT / NO CANDIDATE / NO LIVE`

V4.1 replaces V4 for any future Stage-2 extraction. The rejected V4 JSON,
Markdown, and SHA manifest remain unchanged as failure evidence. No source
discovery market-days have been extracted and no learner has been updated or
run. The external fee/grid receipts below are contract evidence only.

The machine-readable authority is
`ROUND4_POSTFILL_KEEP_FLATTEN_FOK_PREREG_V4_1.json`.

## Why V4 was rejected

The initial V4.1 counterexample run had ten cases: nine exposed a defect and
one passed only because an unrelated identity check happened to reject the
mutation. V4 allowed an `ACKED` historical cancel, accepted a public book at
the synthetic FOK effective instant, omitted the complement reserve from
capital, released ZERO/HARD capital too early, treated scheduled close as a
settlement receipt, used self-reported FOK slices, and called a low/base/high
fee scenario an upper bound.

V4.1 uses strict-pre-effective fail-closed timing. A public book source must
have both receive clocks strictly before effective time. A same-effective
event remains unavailable even if its stable id or ingest sequence sorts
after every other source row.

## Official sources and account receipt

- [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
  establishes per-fill trade-fee rounding, balance rounding, and a per-order
  accumulator/rebate mechanism.
- [Fixed-point migration](https://docs.kalshi.com/getting_started/fixed_point_migration)
  pins minimum contract granularity at `0.01` and requires the per-market
  `price_level_structure`/`price_ranges` grid.
- [Market settlement](https://docs.kalshi.com/getting_started/market_settlement)
  explains that settlement follows expiration, timing varies, and funds move
  when finalized positions resolve. Scheduled close is not a cash receipt.
- [Order directions](https://docs.kalshi.com/getting_started/order_direction)
  supplies the held-YES ASK and held-NO BID exit mapping.

The July 7 fee schedule is backed by the pre-existing local source
`/Applications/Research Ritch/kalshi-fee-schedule July'.pdf`, not by a
fabricated re-download:

```text
bytes                   382507
pages                   12
raw PDF sha256          815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c
WhereFroms (twice)      https://kalshi.com/docs/kalshi-fee-schedule.pdf
WhereFroms xattr sha256 1f0c5b6d7b9718fbe402ee43501b9aa649b97a69abba972f1639fb3c71fceaee
PyMuPDF page-text LF join
  chars                 9612
  sha256                5f90733a0dae6e3d9577efb37895011ddda96385731481a7706c48643b58276d
```

Visual and text checks show taker rate `0.07` with default multiplier `1`,
maker rate `0.0175` with default multiplier `0`, centicent rounding, July 7,
2026 effective date, and no settlement fee. `KXBTC15M` occurs zero times in
the non-standard list (`KXBTCMAX150` and `KXBTCY` each occur once), so
KXBTC15M inherits the defaults. A direct GET at `2026-07-26T11:47:09Z`
returned a Vercel `429` checkpoint; that response is not represented as PDF
bytes and does not displace the authenticated existing local file.

The reproducible API bodies and extracted facts are recorded in
`ROUND4_POSTFILL_V4_1_EXTERNAL_SOURCE_RECEIPTS.json`:

```text
series GET       2026-07-26T11:43:46Z
sha256           be24516ae4825de53af89b12ead76e3a93ac52e18ef67bf519621adde3ed5bc2
fee_type         quadratic
fee_multiplier   1

fee_changes GET  2026-07-26T11:43:51Z
sha256           6780c8eb7edbb5e1ca3b15166f17cef054befacb2cc8f4bc479c54074a9a2c18
historical rows  0

open event GET   2026-07-26T11:46:20Z
sha256           c65e94f4c8919171866048e1ee237a596a4c125d98a6113eff311ab618ead304
grid             tapered_deci_cent: .001/.01/.001
```

The open event response has no event/market fee override fields. That
absence is only an observation; the fee decision requires joint agreement
of the authenticated PDF, series response, and empty historical
`fee_changes` response. Any source path/hash/API fee/grid drift fails closed
before candidate use.

The target subaccount's direct-account precision is pinned only by:

```text
tmp/crypto_mm_canary_20260726/position_value_contract_probe_result.json
sha256 816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6
h = $0.0001
```

That July 26 artifact proves account precision only. July 26 remains forbidden
as a discovery reward, action, fill, or fitting source.

## Public proxy and market identity

Every first/complement maker fill has a normalized evidence summary plus a
contiguous `postfill_v41_public_trade_row` spine. Maker order price is stored
both as outcome cost and on the YES-book scale. Public trade side/outcome are
taker direction, and public price is always YES-book price: a resting YES
BID is crossed by taker NO/ASK strictly below the maker YES price; a resting
NO bid is a YES-book ASK and is crossed by taker YES/BID strictly above the
maker YES price.

The SQL gate derives cumulative strict-through quantity from the child rows,
requires it to cover the full maker quantity, binds the trigger to the final
child receipt envelope, checks contiguous causal trade indices, and binds
the shared spine hash. Summary-only cumulative or direction rewrites are not
commit-authorized.

Ticker, market id, UTC source day, market-window open/close, market metadata
stable id, metadata hash, `price_level_structure`, and `delta_qty=0.01` are
bound. Unknown minimum fill increment fails closed. First and complement
maker quotes are whole-cent aligned for the primary candidate arm; tapered
subcent quotes remain sensitivity-only.

`COMPLEMENT_FILL` must be strictly before market close. A zero-time pair
shares wall/monotonic clocks but the complement ingest sequence must be
strictly later. A race must reference that same complement evidence.

## Fee formulas

Let `delta=0.01`, `tau=$0.0001`, direct-account precision `h=$0.0001`, and
public level `(p_i,q_i)`. Define `n_i=q_i/delta`.

```text
raw_total
  = sum_i 0.07*q_i*p_i*(1-p_i)

aggregate_centicent_lower
  = ceil_tau(raw_total)

public_l2_level_scenario
  = sum_i ceil_tau(0.07*q_i*p_i*(1-p_i))

Tmax
  = sum_i n_i*ceil_tau(0.07*delta*p_i*(1-p_i))

DP_i(m)
  = max_{1<=k<=m}[
      DP_i(m-k)
      + ceil_tau(0.07*k*delta*p_i*(1-p_i))
      + rho(k,p_i)
    ]

partition_DP_no_rebate_upper
  = sum_i DP_i(n_i)

SQL_safe_upper
  = Tmax + n_max*h

DOC_LITERAL_tight_sensitivity
  = raw_total + n_max*$0.0001 + $0.01/order
```

The candidate hard gate uses `partition_DP_no_rebate_upper`. The doc-literal
tight expression is stored only as sensitivity and cannot be selected without
an authenticated accumulator-recurrence receipt. The SQL-safe value is also
stored and independently recomputed.

Maker multiplier zero proves maker trade fee zero, not automatically total
net fee zero. For the primary arm, however, whole-cent price, `delta=0.01`,
and direct `h=tau=0.0001` make every possible private maker partition
balance-aligned; its rounding remainder is therefore provably zero.

The required regression vector is:

```text
p=.30, q=2, 200 possible .01 fills
raw                         = .0294
old V4 "high"               = .03      (not an upper)
trade/partition-DP upper    = .04
SQL-safe upper              = .06
```

## Cash and capital formulas

Let:

```text
B = q*p0 + F0_upper
R = q*pc + Fc_upper
C = directional FOK reserve
x = cancel-effective/order-expiry elapsed time
f = FOK terminal elapsed time
r = public FINALIZED/settlement receipt elapsed time
```

`R` is a local engineering reserve for an independently fillable resting
order. It is not a claim that the exchange deducts that cash from the balance.

```text
locked pair capital = B + R
peak capital        = max(B+R, B+C)

RACE/paired KEEP(t) = (B+R)*t
FOK_FULL            = (B+R)*x + (B+C)*(f-x)
FOK_ZERO            = (B+R)*x + (B+C)*(f-x) + B*(r-f)
HARD                 = (B+R)*x + B*(r-x)
```

For held NO, the BID exit requires `q*limit + F_upper`. For held YES, ASK
sale proceeds offset the fee debit, giving
`max(0,F_upper-q*limit)`. The complement reserve is not returned until
synthetic cancel effective `x`.

HARD uses a sealed post-close synthetic public-lifecycle order-expiry/cancel
scenario for `x`, and the actual public FINALIZED receipt for `r`. It never
claims a private cancel acknowledgement or silently backfills settlement to
scheduled close.

## Normalized DDL and commit authority

V4.1 owns eleven tables and does not reuse the legacy nullable zero atom:

```text
postfill_v41_fee_schedule_receipt
postfill_v41_market_metadata_receipt
postfill_v41_settlement_receipt
postfill_v41_causal_state
postfill_v41_action
postfill_v41_keep_transition
postfill_v41_flatten_fok_outcome
postfill_v41_fok_slice
postfill_v41_public_proxy_evidence
postfill_v41_public_trade_row
postfill_v41_zero_time_atom
```

The table builder inserts all eleven tables inside one transaction, runs
`round4_postfill_v4_1_validator.sql`, and commits only if the violations view
is empty. The SQL independently binds the frozen fee schedule, market
metadata, settlement receipt, and public trade-row spine; walks FOK source
levels; and recomputes FULL versus ZERO, execution quantity, gross PnL, fee
values, conservative net, directional cash, peak capital, and x/f/r
dollar-seconds. A forged row with gross, net, and capital set to `$123` is
rejected and the transaction returns to zero rows.

The V4.1 zero-atom table declares ticker, market id, data origin, first and
complement provenance, execution nature, and fee provenance `NOT NULL`.

Current reproducible checks are:

```text
python3 -m pytest tests/test_round4_postfill_state_contract_v4_1.py -q
45 passed

python3 -m pytest \
  tests/test_round4_postfill_state_contract_v4_1.py \
  tests/test_round4_postfill_state_contract.py \
  tests/test_round4_table_builder.py \
  tests/test_round4_stage1_entry_extractor.py \
  tests/test_round4_stage1_entry_contract.py -q
125 passed
```

Passing the schema and tests authorizes no extraction, fit, candidate,
shadow, or live strategy. No final V4.1 manifest is generated by this
preregistration pass. A future learner must explicitly pin a separately
approved V4.1 manifest and receive separate authorization.
