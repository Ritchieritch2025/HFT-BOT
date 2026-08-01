# ROUND4 post-fill route executability addendum R2

Status: `PRE-DATA_P0_CONTRACT_FIX / NO_CANDIDATE / NO_LIVE`

This addendum was frozen before the FULL-COVERAGE post-fill extractor opened
any real ROUND4 row.  It supersedes only the V1 compact Stage-2 route-selection
boundary; the shared ROUND4 preregistration and its forbidden-date rules remain
unchanged.

## Defect fixed

The V1 compact state carried low/base/high PnL for `BUY_COMPLEMENT` and
`SELL_FIRST_LEG`, but did not carry each route's executable quantity and
residual inventory.  A higher displayed PnL on a partial route could therefore
be mistaken for a valid full exit.

## R2 hard rule

Every decision state must carry:

```text
buy_complement_executable_qty_fp
buy_complement_residual_inventory_fp
sell_first_executable_qty_fp
sell_first_residual_inventory_fp
```

For each route:

```text
executable_qty_fp >= 0
residual_inventory_fp >= 0
executable_qty_fp + residual_inventory_fp = remaining_inventory_fp
```

IOC selection is:

```text
selectable = routes where residual_inventory_fp = 0
route = argmax(selectable, conservative/high-fee PnL)
exact tie = BUY_COMPLEMENT
```

If `selectable` is empty, IOC is illegal, carries no route or limit, uses
`IOC_ALL_ROUTES_INCOMPLETE_RESIDUAL_HELD`, and KEEP is the only legal action.
An incomplete route cannot win on its filled-portion PnL.

## Version and predecessor

- Contract: `ROUND4_POSTFILL_FULL_COVERAGE_V2`
- Action family: `ROUND4_KEEP_IOC_FULL_COVERAGE_V2`
- V1 Python predecessor SHA-256:
  `b90cf10feb041ad93d42ca31ce33f097ee87e03c721e0fe38c2698ffbd375b7f`
- V1 supplemental DDL predecessor SHA-256:
  `f203374753eed49915af6044e9b0d2a626d40aacc43694caf7db73c5ca8cdee7`

R2 remains discovery-only.  It does not authorize fitting, candidate
selection, forward deployment, or live trading.
