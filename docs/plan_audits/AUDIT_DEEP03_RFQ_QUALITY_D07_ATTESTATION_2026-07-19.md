# Independent adversarial audit — fresh RFQ quality gate + D07 attestation repair

- **VERDICT: ❌ FAIL**
- Audited HEAD: `e9308e378199c46b6845ce8e3bd2eaf7d3393450`
  (branch `w-deep03-fullscope-rfq-repair-09`, parent `6d2a2c1`), worktree
  `/Users/ritcardo/HFT-BOT-deep03-fullscope-rfq-repair`, verified clean at
  audit start. Diff scope: exactly two files
  (`tools/research/deep03_v3_rfq_bounded.py` +730/-64,
  `tests/test_deep03_v3_rfq_bounded.py` +580/-30).
- Auditor: independent adversarial session, 2026-07-19. Read-only on code;
  only this report was written.

One-line ruling: the mechanical hardening is real and every scripted
consistency attack was refused with the right error code, but **P0-1's new
quality-gate schema is impossible to satisfy on genuine production
receipts** (it refuses every real sealed `l2-gap-receipt-v1` because of
`generated_at_utc`), and **P0-2 still admits a fully offline-forgeable D07
evidence chain** (fabricated book rows, self-signed producer receipt,
synthesizable reader attestation). Cosmetic-vs-genuine test: P0-1 passes
only on the implementer's synthetic fixture, never on real data → FAIL by
the stated verdict rules.

## Test executions (all at HEAD e9308e3)

| Suite | Result |
|---|---|
| `tests/test_deep03_v3_rfq_bounded.py` | 26 passed (matches claim) |
| 14-file `*rfq*` set | **580 passed in 51.35s** (matches claim) |
| Full `tests/` tree (minus `test_w09_bringup.py`, 1734 collected) | exit 0, no failures |

## Finding F1 — P0-1 quality gate: fail-closed on ALL genuine data (P0, decisive)

Claimed: gate requires the canonical sealed `l2-gap-receipt-v1` schema.
Actual: the required field set is the shape of the *pure* `scan_date()`
dict, not the shape of any receipt ever **written, sealed, or released**.

Evidence chain (every step verified in this audit):

1. `tools/research/deep03_v3_rfq_bounded.py:148-154`
   (`L2_QUALITY_RECEIPT_FIELDS`) omits `generated_at_utc`; line 635 enforces
   `set(receipt) != L2_QUALITY_RECEIPT_FIELDS` → hard refusal on any extra
   field. The comment at lines 144-147 says the set is "the exact field set
   that tools/l2_gap_check.py scan_date() writes" — `scan_date()` does not
   write files; `main()` does.
2. `tools/l2_gap_check.py:253-255` — `main()` unconditionally adds
   `record["generated_at_utc"] = ...` before `write_record(...)`. Every
   receipt the production writer has ever emitted (and will emit at this
   HEAD — the file is untouched by this commit) carries the field.
3. All six real production receipts at
   `/private/tmp/claude-501/-Users-ritcardo-HFT-BOT/ec9683c0-3454-4978-bf11-1785f94da169/scratchpad/l2_quality/l2_gaps_2026-07-{12..17}.json`
   contain exactly `L2_QUALITY_RECEIPT_FIELDS ∪ {generated_at_utc}` (field
   sets compared programmatically; all counters correctly int-typed, so the
   ONLY blocker is the extra field).
4. Sealing copies the file bytes verbatim:
   `tools/canonical_receipts.py:1563-1575` and
   `tools/forward_canonical_receipts.py:997-1023` upload
   `_freeze_file(l2_path)[0]` unmodified (forward path even content-addresses
   the key by `sha256(l2_payload)`); the sealing validator
   `tools/research_release.py:840-890` (`validate_l2_receipt`) checks
   required stats but never strips or forbids extra fields.
5. The V3 release pins the sealed object's own sha
   (`tools/research_release.py:2578-2586`), and the gate verifies body bytes
   against that manifest-declared sha
   (`deep03_v3_rfq_bounded.py:625-628`) — so no caller can strip the field
   without failing `D07_L2_QUALITY_BYTES`.

Empirical proof (attack G1): a receipt in canonical shape **plus**
`generated_at_utc` — i.e. the real production shape — is refused:
`D07_L2_QUALITY_SCHEMA: quality receipt fields differ from the canonical
sealed schema (missing=[] extra=['generated_at_utc'])`.

Why the 26 tests pass anyway: the fixture `_canonical_l2_receipt`
(`tests/test_deep03_v3_rfq_bounded.py:841-865`) builds a synthetic receipt
without `generated_at_utc`. No test feeds a real (or realistically shaped)
receipt through the gate.

Consequence: with real base releases (e.g. the W09 base
`D3-W1-2026-07-18.03`, dates 2026-07-10..17, whose quality objects descend
from exactly these writer outputs), every D07 run supplying the genuine
quality chain raises `D07_L2_QUALITY_SCHEMA` before any gating decision.
The P0-1 closure is therefore **cosmetic on real data** (it fails closed,
which is safe, but the finding required a *satisfiable* canonical gate).
Fix is one token (add `generated_at_utc` to the required set, typed as
UTC text) — but that fix does not exist at the audited HEAD.

## Finding F2 — P0-2 D07: evidence chain remains offline-forgeable (P0, partial closure)

The handoff criterion was: "Caller booleans and a self-consistent hash are
not proof." At this HEAD the *declared* mid/spread/depth are indeed
recomputed (attacks E all refused), but every element of the surrounding
proof chain can still be fabricated by the adapter producer without touching
one attested byte:

- **I1 — fabricated book evidence ACCEPTED.** `_recompute_book_side`
  (`deep03_v3_rfq_bounded.py:2374-2421`) validates `source_row_sha256` only
  as 64-hex (`:2402`); nothing ever compares it to any row of the attested
  L1/L2 objects (sole other occurrence of the field is the constant at
  `:134`). I replaced a valid observation's book with an invented state
  (bid 100000/ask 900000, depth 1+1, `source_row_sha256="11"*32`), set the
  declared values arithmetically consistent, re-signed the digests — the
  adapter VALIDATED and the fabricated `pre_mid_e6=500000` flowed into the
  accepted observation set. The caller-trust P0 has moved from
  (mid, spread, depth) down to (bid, ask, depth, row-sha), not been removed.
- **I2 — self-signed producer receipt ACCEPTED.**
  `_validate_d07_producer_receipt` (`:2269-2300`) checks shape,
  `state == "AUDITED_PASS"`, hex-ness and a self-digest. `algorithm_id`,
  `producer_module_sha256` and `audit_receipt_sha256` are anchored to
  nothing (no registry, no repo-pinned audited value, no artifact lookup):
  a forged receipt `algorithm_id="totally-unaudited-algo"` with arbitrary
  shas validates. "Audited" is a caller-typed string.
- **I3 — reader attestation forgeable offline.**
  `_validate_reader_attestation` (`:472-522`) accepts any dict whose counts
  and digests are internally consistent over the expected object list — all
  computable without performing a single read (the implementer's own test
  helper `_reader_attestation` does exactly this). It proves set agreement,
  not that reads occurred.

What DID hold (genuine improvements, kept): absent producer receipt →
`BLOCKED_PRODUCER_AUDIT_NOT_SUPPLIED` (`:2767-2772`, attack D); adapter
embedding a *different* self-consistent producer receipt than the run's →
`IMPACT_ADAPTER_PRODUCER` (`:2484-2490`, attack D2); ±1 on any declared
pre/post mid/spread/depth or book clock → `IMPACT_ADAPTER_RECOMPUTE`
(`:2660-2679`, attacks E/E2); censored row carrying book evidence →
`IMPACT_ADAPTER_CENSOR` (`:2680-2686`, attack F); quality gate not bound to
the same release → `D07_L2_QUALITY_RELEASE` (`:2773-2782`); quality receipts
without base manifest bytes → `D07_L2_QUALITY_BASE_REQUIRED`
(`:3454-3460`); exact-mapping enumeration/coverage and clock-censor forcing
(`:2540-2696`). These block honest inconsistency and accidental corruption.
They do not deliver the claimed "independently recomputed ... not
caller-trust" property against the adversary the finding named, because the
"raw evidence" itself is unverifiable caller input. Closure is **partial**.

## Attack log (all executed against HEAD; script preserved at scratchpad `attack_rfq.py`)

| # | Attack | Result |
|---|---|---|
| G1 | Real production-shape receipt (with `generated_at_utc`) | REFUSED `D07_L2_QUALITY_SCHEMA` → **F1 proven** |
| G0 | Synthetic fixture shape | PASS state (gate satisfiable only on synthetic data) |
| A | Omit `seq_missed_total` / `parse_errors` / `markers_lost_frames` / `seq_gap_events` / `lines` | all REFUSED `D07_L2_QUALITY_SCHEMA` (missing named; never zero-defaulted) ✅ |
| B | Extra field; counters as `True`/`0.0`/`"0"`/`-1`/`None`; bool recorder marker | all REFUSED `D07_L2_QUALITY_SCHEMA` ✅ |
| B2 | `recorder_markers.gap=1` | gate returns `state=REFUSED`, blocker `recorder_marker:gap=1` ✅ |
| C | Substituted VersionId / SHA / key vs manifest declaration | REFUSED `D07_L2_QUALITY_BASE_BINDING` ✅ |
| C2 | `base_manifest_bytes=None` | REFUSED `D07_L2_QUALITY_BASE_BYTES` (run-level absent set → `D07_L2_QUALITY_BASE_REQUIRED`, `:3454`) ✅ |
| C3 | Same-length body byte tamper | REFUSED `D07_L2_QUALITY_BYTES` (re-hashed) ✅ |
| C4 | Attestation over a different object | REFUSED `D07_L2_QUALITY_ATTESTATION` ✅ |
| D | D07 without producer receipt | `BLOCKED_PRODUCER_AUDIT_NOT_SUPPLIED` ✅ |
| D2 | Adapter embeds different self-consistent producer receipt | REFUSED `IMPACT_ADAPTER_PRODUCER` ✅ |
| E | Declared pre/post mid/spread/depth ±1 vs book (6 variants) | REFUSED `IMPACT_ADAPTER_RECOMPUTE` ✅ |
| E2 | Book `ts_us` differs from declared observation clock | REFUSED `IMPACT_ADAPTER_RECOMPUTE` ✅ |
| F | Censored row with book evidence attached | REFUSED `IMPACT_ADAPTER_CENSOR` ✅ |
| H | Import with `deep03_v3_methods.MAX_BOOK_AGE_US=4_000_000` | import raises `CLOCK_TOLERANCE_AUTHORITY_DRIFT` ✅ guard fires |
| I1 | Fabricated book rows, consistent arithmetic, invented `source_row_sha256` | **ACCEPTED** → F2 |
| I2 | Self-signed producer receipt (arbitrary shas, `AUDITED_PASS`) | **ACCEPTED** by `_validate_d07_producer_receipt` → F2 |
| I3 | Offline-forged L1/L2 reader attestation (no reads performed) | **ACCEPTED** by `_validate_reader_attestation` → F2 |

## P1 verdicts

- **P1-1 dual-filesystem disk preflight — ✅ CLOSED.**
  `deep03_v3_rfq_bounded.py:1391-1426`: probes nearest existing ancestors of
  checkpoint root and exact-temp parent, compares `st_dev`; shared device →
  single summed demand, else two independent demands. Tests
  `tests/test_deep03_v3_rfq_bounded.py:1061-1092` prove the combined-budget
  boundary at exactly one byte.
- **P1-2 DuckDB spill bound — ✅ CLOSED.**
  `:3491-3499` pins `SET temp_directory` to
  `<checkpoint_root>/.duckdb-spill` and
  `SET max_temp_directory_size='16384MiB'`; spill cap is added to the
  checkpoint-filesystem demand (`:1384-1388`). I verified both pragmas take
  effect on the installed DuckDB 1.4.5 (`current_setting` → 16.0 GiB, pinned
  dir).
- **P1-3 expansion bound 4→8 with justification — ✅ CLOSED.**
  Constant + recorded rationale at `:98-110`; surfaced in the preflight
  receipt (`:1443-1451`, `checkpoint_expansion_justification`). Note: the
  justification is stated engineering reasoning (durable ≤4.0×B + transient
  ≤3.0×B + slack), not a measurement — acceptable as a conservative
  ceiling, flagged for the eventual W09 run to confirm empirically.
- **P1-4 clock tolerance authority — ✅ CLOSED.**
  `MAX_RFQ_CLOCK_ABS_SKEW_US = deep03_v3_methods.MAX_BOOK_AGE_US` (`:94`)
  under `RFQ_CLOCK_TOLERANCE_AUTHORITY` (`:90-93`); import-time drift guard
  (`:200-206`) empirically fires (attack H). Registry value confirmed
  5,000,000 µs (`tools/research/deep03_v3_methods.py:37`).
- **Preserved behaviors — ✅ intact.** Diff hunks touch none of: full-ID
  dedup (`_parse_event`/`_HashObserver`), lifecycle/delete conservation SQL
  (`_lifecycle_sql`), integer-µs Kaplan–Meier (`_kaplan_meier_us`), hard
  caps, and no sampling was introduced (no diff hits for
  sample/TABLESAMPLE). Only two test assertions changed and both are
  strictly stronger (blockers list gained `seq_missed_total=3`; disk-error
  match string updated to the new dual-FS message). The other 13 rfq test
  files are byte-unchanged from `6d2a2c1`.

## Required to reach PASS on re-audit

1. F1: make the canonical schema match reality — include `generated_at_utc`
   (typed, e.g. exact `%Y-%m-%dT%H:%M:%SZ` UTC text) in
   `L2_QUALITY_RECEIPT_FIELDS` with its own validation, and add a test that
   feeds one of the six REAL sealed receipts (byte-exact) through
   `build_l2_quality_gate` to prove the gate is satisfiable on genuine data.
2. F2: anchor the D07 proof chain to something the validator can actually
   verify — at minimum (a) verify `source_row_sha256` against rows read
   in-process from the attested L1/L2 objects (the run already holds an
   exact client), or recompute observations in-process instead of accepting
   an external adapter; and (b) pin the producer receipt to a repo-committed
   audited-producer registry (algorithm id → module sha → audit artifact
   sha) instead of accepting any self-signed `AUDITED_PASS`.
3. Re-run this attack battery; I1/I2/I3 must become refusals (or become
   irrelevant by construction), and G1 must become a PASS-state gate.

— end of audit —

---

# Re-audit of 77416c0 (repair commit on top of e9308e3)

- **VERDICT: ✅ PASS — with one explicit deployment-gate blocker (anchor
  provenance wiring, below). Both reopened findings F1 and F2 are genuinely
  closed at module scope; all four P1s remain closed; no regressions.**
- Audited HEAD: `77416c04922bd695cf4df83b4adf64c56b35f394` (one commit on
  top of the previously audited `e9308e3`; parent verified), worktree clean
  at re-audit start. Diff scope: the tool (+214/-28 lines), its test file
  (+341 lines, zero removed assertions — verified by diff grep), two real
  receipt fixtures under `tests/data/`, and this report file.
- Prior report preservation: the committed copy of this file is byte-identical
  to the original I wrote (sha256
  `e3e58cfd952711463a548f1ccde2cc4ae0d2121901fa6516f786c0e9d0eae3a6`,
  `git show` vs working tree — match).

## Test executions (all at 77416c0, run by this auditor)

| Suite | Result | Claim |
|---|---|---|
| `tests/test_deep03_v3_rfq_bounded.py` | 30 passed | 30 ✅ |
| 14-file `*rfq*` set | 584 passed in 51.69s | 584 ✅ |
| Full branch tree `tests/` | 1758 passed, 3 skipped, 1 xfailed | 1758 ✅ |

## F1 re-verdict — CLOSED (schema now matches reality, proven on real bytes)

- Committed fixtures are byte-identical to the production receipts:
  `tests/data/l2_gaps_2026-07-12.json` sha256
  `b7d9aa3764c903872bc487924e324f18917131d6fb5349d96b0e4af651a9c7cf` and
  `tests/data/l2_gaps_2026-07-13.json` sha256
  `a18d01c39ef8bd587cbe2a89045100cc1d697409fc6fcee025e164649404b0d7` — both
  hashed by me against the scratchpad production copies; exact match, and
  they match the shas the repair claims.
- `L2_QUALITY_RECEIPT_FIELDS` now includes `generated_at_utc`
  (`deep03_v3_rfq_bounded.py:158-164`) with strict format validation
  (`L2_QUALITY_GENERATED_RE`, exact `%Y-%m-%dT%H:%M:%SZ`, plus a real
  calendar-validity parse, `:666-678`) — matching the writer's stamp at
  `tools/l2_gap_check.py:254-255` exactly.
- Field-set tripwire re-run by me against ALL SIX real receipts
  (2026-07-12..17), not just the two committed: every one now equals the
  canonical set exactly (symmetric difference empty).
- G1 attack re-run on byte-exact real receipts through the full gate
  (manifest-bound identity + reader attestation + body re-hash):
  real 2026-07-12 → `state=PASS`, `blockers=[]`, `lines=28381830`;
  real 2026-07-13 → `state=REFUSED`,
  `blockers=['seq_gap_events=1', 'seq_missed_total=8']` — a genuine
  quality refusal on real counters, not a schema error. Exactly as required.
- No strictness regression: a receipt *missing* `generated_at_utc` (the old
  synthetic shape) is now refused as missing; extra fields, omitted
  counters, and five malformed `generated_at_utc` variants (int, space
  separator, fractional seconds, numeric offset, invalid calendar date) are
  all refused with `D07_L2_QUALITY_SCHEMA`.
- The committed drift-tripwire test
  (`test_l2_quality_gate_accepts_real_production_receipts`) asserts
  `set(real_receipt) == L2_QUALITY_RECEIPT_FIELDS` on the real bytes, so a
  future writer schema change breaks the build instead of silently
  refusing production data.

## F2 re-verdict — CLOSED at module scope (evidence chain now anchored); one named deployment blocker

Attack replays (script preserved at scratchpad `attack_rfq_v2.py`):

| # | Attack | e9308e3 | 77416c0 |
|---|---|---|---|
| I1 | Fabricated book row, invented `source_row_sha256` | ACCEPTED | REFUSED `IMPACT_ADAPTER_BOOK: hash does not bind the row's canonical bytes` (`:2489-2500` — row hash must equal SHA-256 of the row's own canonical bytes) ✅ |
| I1b | Stronger: rebound self-consistent row hash + re-signed partition receipts (adapter-level consistency holds by construction) | n/a | adapter validates, then `_d07_result` → `BLOCKED_UNANCHORED_EVIDENCE` naming the mutated `partition_receipt_set` digest (`:2900-2960`) ✅ |
| I2 | Self-signed producer receipt, `state=AUDITED_PASS` | ACCEPTED | `BLOCKED_UNANCHORED_EVIDENCE` (`producer_receipt:<sha>` unanchored) ✅ |
| I3 | Offline-synthesized reader attestation | ACCEPTED | `BLOCKED_UNANCHORED_EVIDENCE` ✅ |
| I0 | No anchor supplied at all | n/a | `BLOCKED_UNANCHORED_EVIDENCE` with detail "self-consistent ... are not proof" ✅ |
| I5 | Genuine anchored path sanity | n/a | `EXPLORATORY_OBSERVED`, observed=1, anchor sha + authority recorded in the result ✅ |
| I6 | Anchor `state` tamper (stale self-digest) | n/a | refused (`D07_ANCHOR_INVALID`; digest check would also catch it) ✅ |
| **I4b** | **Caller-forged anchor** (`state=INDEPENDENT_AUDIT_PASS`, enumerating MY forged digests) + fully forged chain | n/a | **`EXPLORATORY_OBSERVED` from fabricated books** — see judgment below |

Mechanism verified in code: `source_row_sha256` is now binding
(recomputed from the row's canonical bytes, `:2489-2500`); per-family
evidence row-set digests are accumulated during recompute (`:2606-2608`,
`:2764-2769`) and must equal the `evidence_row_set_sha256` in each
partition receipt (`:2803-2810`); the partition receipt set digest, the
reader attestation digest, and the producer receipt digest must all appear
in the external anchor (`:2900-2960`) or D07 is
`BLOCKED_UNANCHORED_EVIDENCE`; the anchor requires
`state=INDEPENDENT_AUDIT_PASS`, a named `audit_authority`, valid unique
hex digests, and a self-digest (`:2331-2377`). Order-of-checks is safe:
anchored digests are matched against fields whose content↔digest binding
is enforced later in `_validate_impact_adapter` via `_verify_self_digest`,
so an anchored sha cannot be pasted onto different content.

**Judgment on I4b (the anchor itself as attack surface):** the
`d07_external_anchor` argument is caller-supplied, and no code in the repo
derives it from anything — I verified there is NO caller of
`run_bounded_fresh_rfq` anywhere in `tools/` or `deploy/` at this HEAD, so
no runner entrypoint exists that could wire it correctly or incorrectly.
The module's stated contract (`:141-147`, `:2331-2338`) is that the
runtime gate validates a root-installed 0444 independent-audit receipt at
startup and sources the anchor exclusively from it. A Python callee cannot
verify the provenance of a dict argument; the anchor correctly moves the
trust root from "any evidence-payload supplier" (the F2 adversary — now
fully blocked) to "the run invoker's harness", which is the outermost
boundary an in-process design can reach without taking a *path* and
enforcing `os.stat` root/0444 checks in-module. I judge this an
**acceptable deployment-side dependency per the stated contract**, NOT a
remaining P0, because (a) the named F2 adversary (adapter/evidence
producer) can no longer forge anything, (b) integration/deployment of this
branch is already hard-gated behind separate independent audits in the
handoff continuation order, and (c) the result permanently records
`external_anchor_sha256` + `external_anchor_authority` for final
reconciliation (step 10). **BUT this is only sound if enforced downstream —
explicit blocker below.**

### MANDATORY BLOCKER for the future W09 runner / integration audit

The (not-yet-written) runner entrypoint MUST derive `d07_external_anchor`
exclusively by reading and validating the root-installed 0444 runtime
audit receipt (path-pinned, `os.stat` uid==0 and mode==0444 checked,
sha-verified), never accept it as configuration or CLI input, and the
integration audit MUST verify that wiring with its own adversarial test.
Recommended hardening for that stage: change the module to accept an
anchor *file path* and perform the stat/provenance checks itself.
Until that audit passes, my I4b forgery (caller-forged anchor →
`EXPLORATORY_OBSERVED` from fabricated books) remains reproducible by
whoever controls the run invocation.

## P1 regression check — all four still closed

The repair diff contains no hunks in `_resource_preflight`
(dual-filesystem `st_dev` logic), the DuckDB spill pinning/cap section,
the expansion-factor constants/justification, or the clock-tolerance
constants and import guard (verified by hunk inspection of
`git diff e9308e3..77416c0`). Constants re-checked live:
`MAX_RFQ_CLOCK_ABS_SKEW_US == 5_000_000`, authority string intact; the
preflight and spill tests are among the 30 passing.

## Other checks

- No weakened assertions: `git diff e9308e3..77416c0 -- tests/` removes
  zero `assert`/`raises` lines; the one modified test
  (`stray_book`) now re-binds the row hash so the *unattested-object*
  check is reached — the invented-hash case is separately covered by the
  new I1 test. `_minimal_mapping` gained `mapping_input_ticker_count` (a
  field the full-path conservation check requires), not a weakening.
- Worktree clean at HEAD before this append; nothing committed by this
  audit.

**Re-audit verdict: PASS** (F1 closed on real bytes; F2 closed at module
scope with the anchor-wiring blocker recorded above as a mandatory item
for the runner/integration audit).

— end of re-audit —
