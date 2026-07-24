# A01 Official Market Float Compatibility — Independent Repair Re-audit

Date: 2026-07-23

Repair target: `d051a61fa1cafaaa8eb447b0ef29a45a9336c59b`

Predecessor failing audit report commit: `f8ff2cbc3bcf4619ae3d56183ca55819fceec847`

Scope: official market terminal adapter bounded-number repair and tests

Network calls: 0

Deployments: 0

Financial mutations: 0

Implementation changes by auditor: 0

## Verdict

**PASS.**

The repair closes both blockers from the predecessor audit:

1. the official-response JSON path now applies one config-bound numeric
   envelope to both `parse_int` and `parse_float`;
2. negative zero, extreme exponents, excessive precision, excessive token
   length, excessive total digits, and values outside the configured magnitude
   are rejected before a successful capture receipt can be emitted.

The four retained W09 `floor_strike` forms (`0.5`, `1.5`, `3.5`, `7.5`) remain
accepted exactly as `Decimal`, never as binary float. Control and canonical
documents remain strict, and raw response bytes, size, and SHA-256 remain
cryptographically bound.

This PASS approves the exact repair bytes for the existing read-only
capture/pin/normalize workflow. It does not claim that a live W09 capture,
deployment, production lineage integration, or A01 cash-PnL run occurred during
this audit.

## Immutable target identity

| Item | Identity |
|---|---|
| Commit | `d051a61fa1cafaaa8eb447b0ef29a45a9336c59b` |
| Adapter Git blob | `6ec706c76740a2820e0bc47cddcd154d1f05373a` |
| Adapter raw/code SHA-256 | `3c28dfaa05ab78135a478532b3e3d384879293c9934270076b03669270906c79` |
| Test Git blob | `ad8154473b2c291159d61c0c02e139b123f74485` |
| Test raw SHA-256 | `324ee292923e6a5f376b73300ca272cd3631f932aaba7ae2cbb11d0e6c8516e0` |
| Adapter config SHA-256 | `f23bc7cddd39154fc52c0135ac2b6b7f31ac65f292bb8cd618d702bbbd0ba7ea` |

The tests and adversarial replay ran from a clean archive of the immutable
target at `/private/tmp/terminal-adapter-float-reaudit.6luNwV`, not from the
shared dirty worktree.

## Test results

```text
tests/test_pnl_spine_official_market_terminal_adapter.py
87 passed in 0.19s

tests/test_pnl_spine*.py tests/test_pnl_latency_probe_safety.py
364 passed in 8.45s
```

## Config-bound shared numeric envelope

The independently recomputed config hash binds this policy:

| Property | Bound |
|---|---|
| Integer parser | bounded plain integer |
| Non-integer parser | bounded exact `Decimal`, no rounding |
| Negative zero | forbidden in every lexical form |
| Maximum token length | 32 |
| Maximum total digits | 21 |
| Maximum Decimal precision | 19 |
| Maximum absolute exponent | 18 |
| Maximum absolute magnitude | `9223372036854775807` |
| Promotion into control/receipt | forbidden |

An independent spy around `_validate_bounded_official_number` parsed
`{"i":7,"d":7.5}` and observed the shared validator receive both lexical tokens
in order: `["7", "7.5"]`. The outputs were exact `int(7)` and
`Decimal("7.5")`. This closes the predecessor's `parse_int` bypass.

The chosen envelope is explicitly based on the retained 666 W09 responses
containing `0.5/1.5/3.5/7.5`, with symmetric signed-int64 headroom. It is not a
binary-float limit and must not be silently expanded.

## Adversarial replay

### Previously accepted attacks

Every predecessor attack now refuses:

| Class | Replayed literals | Result |
|---|---|---|
| Negative zero | `-0`, `-0.0`, `-0.000`, `-0e0`, `-0e999999999`, `-0.0E+18` | Rejected |
| Extreme finite exponent | `1e309`, `1e999999999`, `1e-999999999`, `1e+999999999` | Rejected |
| Oversized Decimal | 5,000-digit coefficient | Rejected |
| Oversized integer | 4,000-digit and 5,000-digit integers | Rejected |

The oversized integer cases reject through the adapter's own lexical bound,
before Python integer conversion. They do not depend on the interpreter's
global integer-string limit.

### Exact boundary replay

The following configured boundary forms were accepted exactly:

```text
0
0.0
9223372036854775807
-9223372036854775807
9.223372036854775807e18
-9.223372036854775807e18
1e18
1e-18
1.234567890123456789e+18
-1e-18
```

The following immediately outside or structurally excessive forms refused:

```text
9223372036854775808
-9223372036854775808
1e19
1e-19
12.345678901234567890
1e0000000000000000000018
0.00000000000000000000000000000001
```

`0.123456789012345678` also returned as the identical exact Decimal string,
confirming no rounding or binary-float conversion.

## Retained W09 value compatibility end to end

Each retained real `floor_strike` value was replayed in an otherwise valid
official response through capture, raw pinning, and normalization:

| `floor_strike` | Parse type | Synthetic response SHA-256 | Result |
|---|---|---|---|
| `0.5` | exact `Decimal` | `4f09920395200e6c853fd6b5cc6a1841a4e79fb0076413da1a3431baffc1e6fc` | PASS |
| `1.5` | exact `Decimal` | `634b01777cc5a0e1dfce3b5ece363994ecb96bc97d2eae412d81bf781067348a` | PASS |
| `3.5` | exact `Decimal` | `a60372170dd11260e982077753a21cf10d836ed821e7d35320c2239dcbbe8fcf` | PASS |
| `7.5` | exact `Decimal` | `fae8d03867194fa254065947ddd685b828b8027d4defd879aa7227b0752686ea` | PASS |

These hashes identify the audit's synthetic replay responses, not historical
W09 raw objects. For all four:

- the captured raw bytes were byte-identical to the supplied response;
- capture and normalization independently produced the same raw SHA-256;
- `floor_strike` remained only in raw evidence and did not enter the canonical
  capture receipt or normalized terminal output;
- a recursive scan found no `Decimal` or `float` in either canonical artifact.

## Preserved strict controls

Independent replay confirmed:

- authority JSON rejects non-integer numeric values;
- capture-receipt JSON rejects non-integer numeric values;
- raw-pins JSON rejects non-integer numeric values;
- `canonical_json_bytes` recursively rejects both Python `float` and `Decimal`;
- `NaN`, `Infinity`, and `-Infinity` reject;
- duplicate JSON keys reject;
- consumed fields keep their field-specific contracts; numeric compatibility
  applies only while validating official source JSON;
- exact raw bytes, response size, and SHA-256 remain retained.

## Remaining production work outside this PASS

1. Deploy only the exact adapter code and config hashes listed above.
2. Run the real W09 capture, retain the command-returned receipt SHA externally,
   move the authority/receipt/raw pins/raw responses into the declared
   root-owned read-only boundary, and normalize from those exact pins.
3. Bind the normalized terminal evidence into the production lineage receipt
   before calculating A01 cash PnL.
4. Keep the 50-ticker authority sharding and all earlier endpoint, time,
   retry, raw-preservation, settlement, and metadata-temporality controls.
5. Treat a future official numeric token outside this envelope as a refused
   source/schema event requiring a new corpus-backed review; do not widen the
   policy in place.

## Deployment acceptance

`d051a61fa1cafaaa8eb447b0ef29a45a9336c59b` passes the independent
float-compatibility repair gate and is approved for the existing read-only W09
capture-and-normalize stage, provided the exact-hash and root-pin ritual is
followed. This PASS does not authorize S3 writes, trade placement, A11/B09
opening, or any financial mutation.
