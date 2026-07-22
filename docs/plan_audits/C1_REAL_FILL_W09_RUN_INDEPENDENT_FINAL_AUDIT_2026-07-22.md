# C1 Real-Fill W09 Run — Independent Final Audit

- Audit schema: `c1-real-fill-independent-final-audit-v1`
- Audit timestamp: `2026-07-22T23:26:44Z`
- Audit mode: `READ_ONLY`
- Final state: **PASS**
- Blocking findings: **0**
- Audited experiment: `C1-REAL-FILL-2026-07-22.01`
- Audited implementation HEAD: `8a62e78576014cf9249f490f7237a783d5080137`

## Machine-readable summary

```json
{
  "analysis_artifact_ledger_entries": 258,
  "analysis_artifact_ledger_total_bytes": 166015768,
  "attribution_cells": 432,
  "audit_schema_version": "c1-real-fill-independent-final-audit-v1",
  "audit_state": "PASS",
  "blocking_findings": 0,
  "campaign_rows": 1116054,
  "chart_count": 5,
  "chart_source_count": 5,
  "experiment_id": "C1-REAL-FILL-2026-07-22.01",
  "fill_cells": 54,
  "fill_slice_rows": 171816,
  "markout_cells": 216,
  "markout_rows": 687264,
  "partition_artifacts_verified": 192,
  "partition_receipts": 48,
  "pdf_pages": 4,
  "report_artifact_ledger_entries": 15,
  "verdict_recomputed": "INDETERMINATE_MORE_CLEAN_DAYS"
}
```

## Audited scope

| Item | Exact location |
|---|---|
| W09 result root | `tmp/c1_real_fill_run/c1-real-fill-20260722-214710-8a62e78/work/results` |
| Formal publication root | `output/c1_real_fill_report_2026-07-22` |
| Formal PDF | `output/pdf/C1_REAL_FILL_KILL_TEST_REPORT_2026-07-22.pdf` |

The audit independently performed the following checks without changing any
run artifact, source file, or published report:

1. Reconstructed the exact `3 dates x 16 buckets = 48` partition-receipt key
   set. For all 192 partition artifacts, recalculated path safety, file type,
   byte count, row count, and SHA-256.
2. Recalculated every entry in the 258-item analysis ledger and confirmed its
   exact set equality with the result tree, excluding only the ledger and
   completion receipt themselves.
3. Confirmed that the merged Parquet tables and the 48 partition artifacts are
   equal as multisets, not merely equal in row count.
4. Recalculated the 15-item publication ledger and all transitive bindings in
   `C1_PUBLICATION_COMPLETE.json`.
5. Rebuilt the exact `54` fill, `216` markout, and `432` attribution key grids;
   checked duplicate fields, ratios, legal-price bounds, queue monotonicity,
   and fill-to-markout-to-attribution conservation.
6. Independently recomputed the frozen verdict.
7. Independently rebuilt the data behind all five chart-source CSVs and
   compared every field with the aggregates. Rendered and visually inspected
   all four PDF pages.

## Integrity and conservation results

All checks passed with zero mismatch.

| Quantity | Recomputed value |
|---|---:|
| Campaign rows | 1,116,054 |
| Fill-slice rows | 171,816 |
| Markout rows | 687,264 |
| Total filled quantity across 54 cells | 1,644,201,000 CountE4 |
| Total markout quantity across 216 cells | 6,576,804,000 CountE4 |
| Observed markout quantity | 6,114,554,800 CountE4 |
| Censored markout quantity | 462,249,200 CountE4 |
| Weighted observed gross point | -749,674,130,000 PriceE4×CountE4 |
| Censored entry-price sum | 2,177,773,280,000 PriceE4×CountE4 |
| Legal-price weighted lower sum | -2,927,447,410,000 PriceE4×CountE4 |
| Legal-price weighted upper sum | 1,695,044,590,000 PriceE4×CountE4 |

The following identities held exactly in every applicable cell and globally:

```text
observed_count_e4 + censored_count_e4 = total_filled_count_e4
weighted_lower = weighted_point - weighted_censored_entry_price
weighted_upper = weighted_point + 10000*censored_count_e4
                 - weighted_censored_entry_price
sum(attribution groups) = corresponding markout cell
markout rows = 4 * fill slices
STRICT_THROUGH <= QUEUE_PESSIMISTIC <= OPTIMISTIC_AT_TOUCH
```

Duplicate fill IDs, cross-partition trade reuse, public-quantity excess,
fill/markout linkage failures, and quantity-conservation failures were all
zero.

## Verdict recomputation

The exact recomputed verdict is:

```text
INDETERMINATE_MORE_CLEAN_DAYS
```

This is not caused by missing support:

- PRIMARY / STRICT_THROUGH filled quantity is `111,672,300 CountE4`.
- All `12/12` required date/timer/horizon decision cells have support.
- All six 200 ms decision cells have a legal-price upper bound at or below
  zero. Their upper bounds range from `-1.022 cents` to `-0.021 cents` per
  filled contract.
- All six 1000 ms cells have intervals crossing zero. Coverage falls to
  `86.76%–90.96%`, and legal-price upper bounds range from `+2.908 cents` to
  `+5.866 cents` per filled contract.
- `0/12` required cells have a strictly positive legal-price lower bound.

Therefore the frozen rule permits neither `KILL_C1_ENTRY` nor
`RETAIN_FOR_20_DAY_VALIDATION`. The precise cause is the one-second censored
quantity widening the conservative legal-price interval across zero.

Observed evidence is nevertheless uniformly adverse:

- All `24/24` PRIMARY / STRICT_THROUGH markout cells have a negative observed
  point contribution: `-1.834 cents` to `-1.577 cents` per filled contract.
- Conditional on executable observed exit quantity, their mean is
  `-1.899 cents` to `-1.757 cents`.
- All `216/216` markout cells have a negative observed point contribution.

PRIMARY three-track native-denominator fill-rate ranges across the three dates
and two cancel timers are:

| Track | Minimum | Maximum |
|---|---:|---:|
| STRICT_THROUGH | 1.891% | 3.691% |
| QUEUE_PESSIMISTIC | 2.399% | 4.609% |
| OPTIMISTIC_AT_TOUCH | 6.165% | 9.910% |

On the common queue-reconstructable cohort:

| Cancel timer | Strict | Queue | Optimistic |
|---|---:|---:|---:|
| 200 ms | 2.517% | 3.177% | 7.423% |
| 300 ms | 3.221% | 4.057% | 9.015% |

## Publication and claim-safety result

All five PNG charts and all five chart-source CSVs match independently
recomputed aggregate values. The four-page PDF has no clipping, overlapping
content, broken glyphs, or incorrect labels. The HTML and PDF correctly keep
the claim tier at `EXPLORATORY_NON_GATE` and state that:

- gross markout is not net PnL or profit;
- exact fees remain unresolved;
- latency values are preregistered stresses, not production measurements;
- no portfolio-capacity or statistical-significance conclusion is made;
- the result neither promotes a strategy nor authorizes live trading.

## Non-blocking wording findings

1. The generic verdict reason says the interval overlaps zero **or** a required
   cell lacks support. In this run every required cell has support; the exact
   cause is only the 1000 ms censoring interval crossing zero.
2. The PDF's “next decision” section describes retain and kill branches but
   does not explicitly state the operational branch for the current
   `INDETERMINATE_MORE_CLEAN_DAYS` outcome.

These are clarity improvements only. They do not alter any number, verdict,
claim ceiling, hash binding, or publication-integrity result.

## Audited artifact hashes

The two artifact ledgers contain the complete per-file hash lists. Their own
hashes, plus the principal transitive bindings, are:

| Artifact | SHA-256 |
|---|---|
| `C1_AGGREGATES.json` | `06eaebad7e1ff469c7bee462c3c75f1563f0c3acdb36da422390719162e92f11` |
| `ANALYSIS_ARTIFACT_SHA256.json` | `3c33faa8122d859e9ca7259a5e4b032a55ae5aa37a9d95b354875dca813831ef` |
| `C1_RUN_COMPLETE.json` | `c5b1a3e390ea45101b74bf389b541e77bce13d38464b7c0a730a6be95da6fc00` |
| `REPORT_ARTIFACT_SHA256.json` | `db878465d6a48f10a95ceffc4b6fe45af290b83ee39f0b1978e1f41c61b8109c` |
| `C1_PUBLICATION_COMPLETE.json` | `ca950c4dbbd504be31cfd444b9d92f33476e5fa2a299c1130165e3a5e0b876b4` |
| `index.html` | `de802267eb95023bb2b2fe95f333f8a608b6127990e3ae719b20370c4270e101` |
| `C1_REAL_FILL_KILL_TEST_REPORT_2026-07-22.pdf` | `bb48d2d862c863f7b0d68d393d0eed8162f494260d718f1dcf2ce412f5d7673b` |
| Audited runner source | `b85d4e6eb5eda01f77ca686175019812926f9d5d8282eb2bb9954b4c79096258` |
| Audited publisher source | `9bd85496165275546addbf8a633c696063b224e440b7a134d1ab38fa242c48f0` |
| Frozen config | `30d09f4305b7f7bfa938cd0bf167f5e44e88b337ade17ea08631ff2cc328a51a` |

The frozen upstream successful-runtime commit recorded by the run is
`5d5d27836891ae22cebecae85f90f2674e579909`; its source binding is
`c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979`.

## Final disposition

**PASS.** The real W09 analysis, immutable receipts, aggregate result, chart
sources, and formal publication are internally consistent and byte-bound. No
audit finding blocks use of this report at its declared exploratory claim
tier. This audit does not upgrade the result to net profitability, promotion,
or live-trading authorization.
