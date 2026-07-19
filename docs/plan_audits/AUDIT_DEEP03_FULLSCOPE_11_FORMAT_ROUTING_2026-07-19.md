# INDEPENDENT AUDIT — deep03 fullscope .11 format-routing fix

## VERDICT: PASS

- **Audited commit:** `416168cf094aa3da952a01d6d57dbd2ecc783265`
  ("Route fullscope graph fact reads by production file format and bump chain to .11"),
  parent `84089f5`, branch `w-deep03-fullscope-09`, tree clean at audit time
  (`git status --porcelain` empty; HEAD verified by `git rev-parse`).
- **Auditor:** independent adversarial session, read-only on code; this report is
  the only write. No commit, no deploy, no SSH.
- **Scope:** the .10 production failure fix — `_fact_relation` format routing in
  `tools/research/deep03_fullscope_graph.py`, fixture fidelity, tripwires,
  .10→.11 release-chain bump.

## Summary of evidence

| Check | Result |
|---|---|
| 1. Diff confinement (84089f5..416168c) | PASS — 11 files, all hunks reviewed |
| 2a. Real gzip csv trades + parquet orderbooks builds, rows conserved | PASS |
| 2b. Parquet bytes named `.csv.gz` fails closed | PASS |
| 2c. `.ndjson` fact → typed refusal | PASS |
| 2d. Uppercase `.CSV.GZ` deterministic | PASS (fail-closed, see OBS-1) |
| 2e. Missing `market_ticker` → identity error | PASS |
| 3. all_varchar cannot corrupt counts; declared-vs-actual binds csv | PASS |
| 4. .11 chain bump completeness in code/tests/service | PASS |
| 5. Full suite + both SHA manifests | PASS — 1831 passed / 3 skipped / 1 xfailed |
| Revert tripwire (simulated .10 regression) | PASS — fires with the exact production error |

## 1. Diff confinement — the only semantic change is routing + fixtures + chain bump

`git diff --stat 84089f5..416168c`: 11 files. Every hunk reviewed:

- `tools/research/deep03_fullscope_graph.py` (+28/−3): new `_fact_relation(path)`
  and the single call-site replacement in `_scan_fact_support` (the previous
  hard-coded `read_parquet(...)` view source). No other logic touched.
- `tests/test_deep03_fullscope_graph.py`: production-header gzip-csv fixture
  writer, trades fixture switched from `.parquet` to `.csv.gz`, two new tests
  (fixture-fidelity tripwire; unknown-extension refusal).
- `tests/test_deep03_fullscope_runner.py`: fixture manifest now ships trades as
  `.csv.gz`, orderbooks as `.parquet` (comment cites docs/warehouse_schema.md).
- `deploy/w09/deep03_authority_gate.py`, `deep03_one_shot_arm.py`,
  `w09-exploratory-autoresearch.service`, both `.sha256` manifests,
  `tests/test_deep03_one_shot_arm.py`, `tests/test_deep03_v3_w1_preflight.py`,
  `tests/test_w09_exploratory_autoresearch.py`: pure `.10`→`.11` /
  `20260719-10`→`20260719-11` identifier bumps + manifest hash refresh. No
  behavioral change beyond the pinned IDs and two error-message strings.
- **Audited modules untouched:** `deep03_v3_methods.py`, `deep03_v3_l2.py`,
  `deep03_v3_rfq_bounded.py`, `deep03_v3_runner.py`, `deep03_fullscope_runner.py`
  do not appear in the diff at all.

### Routing parity with the audited methods handler

`deep03_v3_methods._relation_sql` (lines 611–627, unchanged): csv branch is
`read_csv(...,header=true,union_by_name=true,hive_partitioning=true,all_varchar=true)`.
`_fact_relation`'s csv branch is byte-identical in options. Parquet branch
identical (`union_by_name=true,hive_partitioning=true`).

**Deliberate, safe divergence (OBS-2):** `_relation_sql` treats every
non-`.parquet` path as csv (implicit fallback); `_fact_relation` refuses
unknown extensions with a typed `MarketGraphError`. Strictly fail-closed
relative to the audited handler — an improvement, not a deviation of the read
semantics for the two contract formats.

### Fixture header fidelity

`TRADES_CSV_HEADER` = the 12 contract columns from docs/warehouse_schema.md
("trades: ts_utc, market_ticker, series_ticker, event_ticker, category,
subcategory, group, trade_id, yes_price_e4, no_price_e4, count_e4, taker_side
+ ladder columns") + the four W-TL1 ladder columns
(`exchange_ts_us, recv_wall_ns, recv_mono_ns, local_recv_ts_us`), in that
order. Matches the production contract exactly.

## 2. Attack log (all run by the auditor against 416168c, scratch fixtures only)

Harness: scratchpad script importing the repo test helpers via importlib;
nothing written inside the repo.

**(a) Real gzip csv trades (production header, 5 rows) + parquet orderbooks.**
Graph builds; `support_conservation.source_rows = {orderbooks_full: 2,
orderbooks_l1: 2, trades: 5}` — exactly the fixture row counts (l1: 1+1,
l2: 2, trades: 5). Row conservation intact. (First run tripped the auditor's
own miscounted assertion l1=3; fixture truth is 2 — auditor error, corrected.)

**(b) `.csv.gz` containing real parquet bytes, declared row_count=1.**
Fails closed at view creation:
`IOException: IO Error: Input is not a GZIP stream: .../trades__corrupt__2026-07-12.csv.gz`.
Clear, path-naming error; never returns zero rows silently. (DuckDB-typed, not
`MarketGraphError` — acceptable: the spec demanded fail-closed with a clear
error, and the run aborts before any support row is written.)

**(c) `.ndjson` trades fact.**
`MarketGraphError: fact object has an unsupported file format (expected
.parquet, .csv.gz or .csv): .../trades__x.ndjson` — typed refusal, rogue
filename in the message. Also covered by the new in-repo test
`test_graph_refuses_unknown_fact_extension`.

**(d) Uppercase `TRADES__2026-07-12.CSV.GZ` (valid gzip csv content).**
`_fact_relation` lowercases the name, so routing is deterministic → `read_csv`
(never misrouted to parquet). DuckDB's own compression inference then does not
recognize uppercase `.GZ`, reads raw gzip bytes as text, and fails dialect
sniffing (`InvalidInputException: Error when sniffing file`). Deterministic
fail-closed refusal — not a finding (see OBS-1).

**(e) `.csv.gz` with no `market_ticker`/`ticker` column.**
`MarketGraphError: fact market identity missing: .../trades__noid__2026-07-12.csv.gz`
— the pre-existing identity gate fires unchanged for csv facts.

**(f — auditor-added) csv fact with declared row_count=3, actual=1.**
`MarketGraphError: fact row conservation failed: ... declared=3 actual=1` —
the manifest row authority binds csv facts identically to parquet.

**Revert tripwire simulation.** Patched `_fact_relation` in memory back to the
.10 behavior (`read_parquet` for everything) and ran the new mixed-format
fixture: fails with `InvalidInputException: ... No magic bytes found at end of
file '...csv.gz'` — the exact production failure signature from
2026-07-19T19:33:25Z. The fixture-fidelity test cannot pass a reverted module.

## 3. all_varchar and count integrity

- Rows are counted with `SELECT count(*)` on the view — format- and
  type-independent; `all_varchar=true` affects column typing only, never row
  cardinality.
- The declared-vs-actual check (`declared != actual` → `MarketGraphError`, and
  non-int/negative/missing `row_count` → refusal) sits on the shared code path
  after `_fact_relation`, so it binds csv facts exactly as parquet facts
  (proven by attack f).
- The per-channel support-sum conservation check (`source_rows` vs graph sum)
  also fails closed if any ticker were nulled out of the INSERT
  (`try_cast(... ) IS NOT NULL` filter would create a source/graph mismatch).

## 4. Chain bump completeness (.10 → .11)

`grep -rn "2026-07-19\.10\|20260719-10" deploy/ tests/ tools/` → **zero hits**.
Bumped and consistent: `deep03_authority_gate.py` (W2A/W0/W1 release IDs +
error strings), `deep03_one_shot_arm.py` (RELEASE_ID), service unit
(`D3-W0-20260719-11.json` / `D3-W1-20260719-11.json` in both ExecCondition and
ExecStart), and the four test files.

Expected out-of-scope residue, flagged for completeness only:
`docs/plan_releases/DEEP03_D3_W0_RELEASE_CANDIDATE_2026-07-18.json` and
`DEEP03_D3_W1_RELEASE_CANDIDATE_2026-07-18.json` still bind `.10` — the release
re-binding is a separate act per the audit scope (OBS-3).

## 5. Suite + manifest verification (run by the auditor)

- `shasum -a 256 -c deploy/w09/deep03_open_discovery_modules.sha256` — all 15
  entries OK, including the new
  `061d0cdc4921cc2eea032b24caaea5daf8185a15dfc356668940a71e31abf5aa
  tools/research/deep03_fullscope_graph.py`.
- `shasum -a 256 -c deploy/w09/exploratory_autoresearch_payload.sha256` — all
  10 entries OK (gate, arm, service, modules-manifest hashes all refreshed to
  the .11 bytes).
- `python3 -m pytest` (junitxml-verified): **1831 passed, 3 skipped, 1 xfailed
  in 101.8s** — matches the claimed counts exactly
  (tests=1835, failures=0, errors=0).

## Observations (non-blocking)

- **OBS-1 (informational):** uppercase `.CSV.GZ` is routed to `read_csv`
  deterministically but then refused by DuckDB's compression sniffing. The
  warehouse contract emits lowercase extensions, so no production path hits
  this; behavior is fail-closed either way. If uppercase archives ever become
  possible, add `compression='gzip'` explicitly or normalize before routing.
- **OBS-2 (informational):** `_fact_relation` is stricter than the audited
  `_relation_sql` (typed refusal vs implicit csv fallback for unknown
  extensions). Divergence is in the safe direction.
- **OBS-3 (expected):** docs/plan_releases candidate JSONs still bind `.10`;
  re-binding is a separate act and was explicitly out of audit scope.

## Verdict

**PASS.** The change is exactly what it claims: production-format routing with
fail-closed refusal, fixtures that mirror the warehouse byte format, tripwires
that reproduce the .10 failure on revert, an intact conservation chain for csv
facts, and a complete .11 bump across gate/arm/service/tests with both SHA
manifests verifying. No semantic drift in the audited L2/methods/runner
modules.
