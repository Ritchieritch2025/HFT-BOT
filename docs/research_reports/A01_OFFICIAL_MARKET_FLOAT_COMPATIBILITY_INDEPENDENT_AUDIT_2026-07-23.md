# A01 Official Market Float Compatibility — Independent Adversarial Audit

Date: 2026-07-23

Audit target: `a14474b9305bad68bbce4d1cffd6e2e2b07f7bed`

Scope: official market terminal adapter float-compatibility repair and tests

Network calls: 0

Deployments: 0

Financial mutations: 0

Implementation changes by auditor: 0

## Verdict

**FAIL — do not deploy `a14474b` as the production adapter.**

The repair correctly accepts the real W09 source forms
`floor_strike: 0.5/1.5/3.5/7.5` as exact `Decimal` values, without binary-float
conversion, rounding, or promotion into canonical receipts. However, its source
numeric policy is unbounded and explicitly permits values that this audit
requires to fail closed:

- negative zero in integer, decimal, and exponent forms;
- extreme positive and negative exponents;
- values beyond a bounded production magnitude;
- multi-thousand-digit decimal coefficients and integers.

Those values do not merely parse in an isolated helper. When inserted as an
additive official `market` field, they pass the complete terminal-response
parser and still produce an eligible finalized terminal record. Green tests do
not cure this defect: one new test expressly asserts that `1e309` and `-0.0`
must be accepted, which conflicts with the stated acceptance contract.

## Immutable target identity

| Item | Identity |
|---|---|
| Commit | `a14474b9305bad68bbce4d1cffd6e2e2b07f7bed` |
| Adapter Git blob | `229f52657dc29aff1c9641edfcf578f25f044479` |
| Adapter raw/code SHA-256 | `e12ba1fe309f0df2d55a05b26933d16281073b7c48e4e1fb457789104e1be4f1` |
| Test Git blob | `69d6c16a88dda817d9284d0e0dca0dbb2e806a00` |
| Test raw SHA-256 | `02b48ca7e8c7258529c5e69063a4e47ae406b48614f49c6cd452f22342397ac7` |
| Adapter config SHA-256 | `c776e7a1bd9862f10e39bd9b939681127c214edd17e7e6cfbf70071da33cb972` |

The audit ran from a clean archive of the immutable target, not the shared
worktree.

## Test results

```text
tests/test_pnl_spine_official_market_terminal_adapter.py
62 passed in 0.14s

tests/test_pnl_spine*.py tests/test_pnl_latency_probe_safety.py
339 passed in 8.76s
```

These results establish regression compatibility. They do not cover the
required bounded-number policy.

## Blocking findings

### F-01 — Finite Decimal acceptance has no lexical, precision, exponent, magnitude, or signed-zero bound

Severity: **P1 / deployment blocker**

`_parse_exact_finite_number` at
`official_market_terminal_adapter.py:224-244` calls `Decimal(value)` and checks
only `is_finite()`. The official response parser enables it for every
non-integer number at `:1154-1163`. Additive market fields are intentionally
tolerated and then omitted from the normalized whitelist, so no later field
validator constrains them.

The exact commit incorrectly accepted all of the following complete market
responses:

```text
floor_strike: -0.0
floor_strike: -0e999999999
floor_strike: 1e309
floor_strike: 1e999999999
floor_strike: 1e-999999999
floor_strike: <5000-digit coefficient>.0
```

It also accepts `-0.000` and preserves the signed-zero bit in `Decimal`.

Impact:

- violates the explicit fail-closed contract for negative zero, extreme
  exponent, and oversized numeric values;
- permits an adversarial official response to consume parser memory/CPU up to
  the general 8 MiB body limit;
- emits an otherwise successful terminal record rather than a refusal, hiding
  the malformed numeric field behind the fact that it is unconsumed.

The fact that the value is not promoted into the receipt limits arithmetic
corruption, but it does not satisfy input validation or availability safety.

### F-02 — Integer literals bypass the new Decimal hook and remain unbounded

Severity: **P1 / deployment blocker**

`json.loads` receives a custom `parse_float` but no custom `parse_int` at
`official_market_terminal_adapter.py:259-270`. Therefore additive official
fields containing integers take the ordinary Python integer path.

The exact commit accepted:

```text
floor_strike: -0
floor_strike: <4000-digit integer>
floor_strike: <5000-digit integer>
```

`-0` is normalized to ordinary integer zero, losing the lexical sign before any
validator can reject it. Large integers are parsed and silently discarded.

An official data-plane numeric envelope must cover both JSON integer and
non-integer productions. Limiting only `Decimal` tokens leaves a direct bypass
for the same negative-zero and oversized-value attacks.

## Passed controls

### Real W09 compatibility

All four retained failure-response forms were replayed inside otherwise valid
official market responses:

```text
0.5  ACCEPT
1.5  ACCEPT
3.5  ACCEPT
7.5  ACCEPT
```

The parsed terminal whitelist contained no `float` or `Decimal`.

### Exact arithmetic

`0.10000000000000000000000000000000000001` was parsed as an exact `Decimal`
with the identical digit string. No Python binary float was created and no
rounding occurred.

### Control-plane isolation

Authority, capture receipt, and raw-pins parsing still rejected nested and
top-level `0.5`, `-0.0`, `1e2`, and `1.5`. Their production loader continues to
call `parse_strict_json` with finite-number compatibility disabled.

`canonical_json_bytes` also rejects a `Decimal`, so a source numeric object
cannot silently enter:

- authority;
- capture receipt;
- raw-pins document;
- normalized terminal output;
- adapter config hash material.

### Other hostile syntax

The official response path rejected:

- `NaN`;
- `Infinity`;
- `-Infinity`;
- duplicate numeric keys.

Consumed fields remain strict. For example,
`settlement_value_dollars: 1.0` is rejected because the payout contract requires
the exact four-decimal string representation.

### Raw preservation

A bounded `floor_strike: 3.5` capture preserved:

- byte-identical raw response;
- exact response size;
- SHA-256 in the capture attempt;
- the same SHA-256 after normalization re-read.

Neither the capture receipt nor normalized canonical output contained
`floor_strike`; only the raw bytes and their cryptographic binding retained it.

## Required repair

A successor commit must add a config-bound official-source numeric policy
without weakening the control plane:

1. Define and hash explicit maximum token length, significant-digit count,
   absolute exponent, and absolute magnitude.
2. Reject all negative-zero lexical forms, including at least `-0`, `-0.0`,
   `-0.000`, `-0e0`, and `-0e999`.
3. Add a bounded `parse_int` path so integer literals cannot bypass the source
   policy.
4. Keep non-integer values as `Decimal`; never call `float`, quantize, round, or
   apply ambient Decimal-context precision.
5. Continue accepting the four observed `floor_strike` forms exactly.
6. Continue rejecting every float in authority, capture receipt, raw pins, and
   all other control/canonical documents.
7. Keep explicit output whitelisting and add a recursive assertion that no
   `Decimal` or `float` reaches a canonical receipt.
8. Preserve raw response bytes, size, and SHA exactly.
9. Replace the current test that treats `1e309` and `-0.0` as acceptable with
   boundary tests that distinguish authorized finite values from hostile
   values.

The numeric limits must be justified against retained official response
samples; they must not be improvised from binary-float limits.

## Re-audit gate

A successor immutable commit may receive `PASS` only when:

- the four real W09 values pass end-to-end capture and normalization;
- every attack listed in F-01 and F-02 refuses before a successful
  `CAPTURE_RECEIPT`;
- NaN, both infinities, duplicates, numeric consumed fields, and control-plane
  floats remain refused;
- exact raw bytes and SHA survive;
- the adapter suite and full PnL Spine suite remain green;
- the successor's new code and config hashes are independently recomputed.

Until then, the previously audited `f6da844` logic remains safer but
incompatible with the retained real responses; neither commit is approved for
production terminal capture.
