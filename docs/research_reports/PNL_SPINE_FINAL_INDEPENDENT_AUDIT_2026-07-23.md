# PnL Spine Final Independent Audit — 2026-07-23

## Overall decision

`PASS_FAIL_CLOSED_WITH_REAL_RUN_BLOCKERS`

The code gate at commit
`f93278f42e18cde8e3e6431bb4f1ec7d824111e3` passes this independent,
local, read-only adversarial audit. The new external lineage contract and
the CLI raw-file/canonical-document SHA boundary fail closed under the
attacks listed below. The terminal and risk-lifecycle results previously
accepted at
`c775b83458a1a19f2f93d20ff4fc3665a8484b12` did not regress.

This is **not** approval to publish real Net PnL. The three frozen
experiments remain blocked on real input, latency, fee, exit, settlement,
execution-lifecycle, or training evidence. No production extractor or
production `pnl-spine-lineage-receipt-v1` artifact is present in the
audited repository state. Until both exist and are independently pinned,
the real run must remain `PNL_BLOCKED` and its `totals` must remain null.

## Audit identity and scope

- Audited code commit:
  `f93278f42e18cde8e3e6431bb4f1ec7d824111e3`
- Terminal/risk predecessor:
  `c775b83458a1a19f2f93d20ff4fc3665a8484b12`
- Branch observed at audit start: `w-pnl-spine-v1`
- Audit mode: local and read-only for all runtime code and data
- Runtime modifications made by this audit: none
- Only this report is intended to be committed
- Primary scope:
  - external/root-pinned record-lineage contract;
  - exact release/object/date/channel/record/transform binding;
  - CLI raw-file SHA versus parsed canonical-document SHA;
  - regression check for terminal truth and risk lifecycle;
  - current real-data readiness of A01, A11, and B09.
- Out of scope:
  - generating a production lineage receipt;
  - changing extractors, runners, experiments, IAM, AWS, or real data;
  - claiming strategy profitability.

The target commit was exported into an isolated directory for the
adversarial PnL tests. The full top-level test suite was also run from the
tracked `f93278f` workspace because the dashboard tests use local built
artifacts that are not included in `git archive`.

## Test results

| Test gate | Command | Result |
|---|---|---:|
| PnL suite | `python3 -m pytest -q tests/test_capture_private_fee_truth.py tests/test_pnl_spine_*.py` | **176 passed** |
| Top-level suite | `python3 -m pytest -q tests` | **216 passed** |
| Focused lineage/CLI attacks | focused selection in `tests/test_pnl_spine_runner.py` | **18 passed** |
| Focused terminal/risk regression | terminal, settlement, daily-loss, exposure, equal-time, partial-exit and root-cap selection | **12 passed** |

The 18 focused lineage/CLI cases were:

1. changed L2 values plus re-signed ordinary authority against the old
   lineage;
2. forged lineage against the old external lineage pin;
3. missing lineage record;
4. extra lineage record;
5. duplicate lineage record;
6. wrong exact object;
7. wrong UTC date;
8. wrong channel;
9. runtime-record hash drift;
10. extractor transform-code drift;
11. input-set hash drift;
12. empty source-member set;
13. duplicate source member;
14. a normalized row falsely declared `DIRECT`;
15. extra object-read declaration;
16. duplicate object-read declaration;
17. missing external authority/lineage;
18. CLI newline-bearing authority and lineage files.

No scoped test failed.

## Adversarial findings

### 1. Changed L2 record plus ordinary-authority re-sign

Attack:

- change the L2 bid and ask values;
- refresh the fixture evidence bindings;
- re-sign all ordinary fixture-derived authority fields;
- retain the independently pinned old lineage receipt.

Result:

- state: `PNL_BLOCKED`;
- `totals`: null;
- blocker: `RECORD_LINEAGE_AUTHORITY_INVALID`;
- the attack cannot hide behind a freshly self-consistent ordinary
  authority.

The controlling checks are in
`tools/research/pnl_spine/runner.py:1027-1478`, with fixture evidence
cross-checked against the external lineage at lines 1418-1477.

### 2. Forged lineage plus old external pin

Attack:

- change the L2 values;
- build a new internally self-consistent lineage receipt for the changed
  fixture;
- supply the original externally approved lineage SHA.

Result:

- state: `PNL_BLOCKED`;
- `totals`: null;
- blocker: `RECORD_LINEAGE_AUTHORITY_INVALID`;
- detail identifies that the canonical lineage SHA does not match the
  external pin.

The canonical external-pin comparison is at
`tools/research/pnl_spine/runner.py:1056-1066`. The lineage self-hash is a
separate check at lines 1067-1073.

### 3. Missing, extra, duplicate, and drifted lineage content

All requested mutation classes failed closed:

- missing, extra, or duplicate runtime record;
- missing, extra, or duplicate object read/source member;
- wrong exact release object;
- wrong record date;
- wrong source channel;
- record hash drift;
- transform-code/config authority drift;
- input-set hash drift;
- empty source members;
- invalid `NORMALIZED_ROW=DIRECT`.

The contract enforces:

- strict top-level and row schemas;
- exact release-set identity;
- exact-version object identity and full-byte verification declaration;
- unique, sorted object reads;
- unique, sorted, exhaustive runtime records;
- UTC date and allowed channel for every record;
- extractor code/config equality;
- exact canonical runtime-record equality;
- exact source-member/input-set equality;
- exact agreement between fixture evidence bindings and external lineage.

Relevant code:

- ordinary trusted authority v2 and its lineage/extractor pins:
  `tools/research/pnl_spine/runner.py:379-498`;
- runtime evidence collection and causal UTC dates:
  `tools/research/pnl_spine/runner.py:843-915`;
- lineage validation:
  `tools/research/pnl_spine/runner.py:1027-1478`;
- fail-closed conversion to receipt blockers:
  `tools/research/pnl_spine/runner.py:2517-2550`.

### 4. Missing lineage only

An additional manual replay retained a coherent ordinary authority and
its canonical pin but passed no lineage document.

Result:

- state: `PNL_BLOCKED`;
- `totals`: null;
- blockers:
  `RECORD_LINEAGE_AUTHORITY_INVALID` and
  `EXTERNAL_AUTHORITY_INVALID`.

The second blocker is expected: trusted-authority v2 is required to bind
the actual lineage document and extractor identities, so the ordinary
authority cannot validate in the lineage document's absence.

### 5. CLI raw SHA versus canonical SHA, including trailing newline

The authority and lineage JSON files were each written as canonical JSON
plus a trailing newline. Therefore each raw-file SHA intentionally
differed from its parsed canonical-document SHA.

Results:

- supplying the two correct **raw-file** SHAs:
  - the CLI accepted both files;
  - no external-authority or record-lineage blocker was emitted;
  - the fixture still returned exit code 2 and `PNL_BLOCKED` solely
    because its measured-latency mode was deliberately changed to
    `SCENARIO_ASSUMPTION`;
- supplying the authority canonical SHA in the raw-authority pin
  argument:
  - CLI exited 1 before execution;
  - error: raw trusted-authority SHA mismatch;
- supplying the lineage canonical SHA in the raw-lineage pin argument:
  - CLI exited 1 before execution;
  - error: raw trusted-lineage SHA mismatch.

This confirms the intended two-stage boundary:

1. `_read_json_once` reads one regular file descriptor and returns both
   the parsed document and SHA of the exact raw bytes
   (`tools/research/pnl_spine/runner.py:3490-3534`);
2. CLI compares those raw SHAs before calling the runner
   (`tools/research/pnl_spine/runner.py:3563-3585`);
3. the runner receives and validates canonical document SHAs
   (`tools/research/pnl_spine/runner.py:3586-3595`).

A trailing newline is neither silently discarded at the external raw
boundary nor incorrectly treated as a canonical-document mismatch.

## Terminal and risk lifecycle regression

No terminal/risk regression was found.

- `tools/research/pnl_spine/terminal.py` has no diff between `c775b83`
  and `f93278f`.
- `tools/research/pnl_spine/risk.py` has no diff between those commits.
- AST comparison found the following runner functions semantically
  identical across the two commits:
  - `_parse_settlements`;
  - `_lot_matched_realized_pnl`;
  - `_replay_risk_lifecycle`.
- The focused 12-case regression set passed.

The preserved guarantees include:

- only `FINALIZED` can carry payout authority;
- no market can have multiple finalized settlement authorities;
- partial IOC exits realize principal and fees at the exact exit time by
  FIFO lot matching;
- residual quantity must have a finalized settlement;
- equal-timestamp closure and realized loss precede new reservation/fill;
- realized loss is kept by UTC date;
- a breached daily-loss date remains latched after later same-day profit;
- open exposure survives UTC midnight;
- a risk or terminal blocker forces `PNL_BLOCKED`;
- totals are created only when the final state is `NET_PNL_COMPLETE`.

Key locations in `f93278f`:

- settlement parsing: `tools/research/pnl_spine/runner.py:732`;
- FIFO realized-PnL attribution:
  `tools/research/pnl_spine/runner.py:1769`;
- causal risk replay: `tools/research/pnl_spine/runner.py:1954`;
- totals gate: `tools/research/pnl_spine/runner.py:2320-2356`;
- final state selection:
  `tools/research/pnl_spine/runner.py:3455`;
- daily UTC loss latch:
  `tools/research/pnl_spine/risk.py:205-229,334-346,532-569`;
- settlement truth:
  `tools/research/pnl_spine/terminal.py:48-96`.

## Real three-experiment status

The exact locally materialized, hash-pinned 2026-07-12,
2026-07-15, and 2026-07-17 C1/Deep03 artifacts were inspected read-only
through `audit_real_data`. The result remains:

- classification: `ENGINEERING_INPUT_ONLY_NOT_NET_PNL`;
- Deep03 claim tier: `DESCRIPTIVE_ONLY_NO_PNL`;
- every experiment has `net_pnl_rows=0`.

| Frozen experiment | Real-data state | Candidate rows | Normalized rows | Net PnL rows | Principal blockers |
|---|---|---:|---:|---:|---|
| A01 Spread Capture | `BLOCKED_SOURCE_FIELDS` | 1,116,054 | 0 | 0 | C1 depletion rows are not spread-dwell rows; exact bid/ask dwell and warm-up missing; train gates missing; measured latency missing; exact L2 exit missing; settlement missing; fee receipt fail-closed; scheduled-start binding missing |
| A11 One-Sided Provision | `BLOCKED_SOURCE_FIELDS` | 896,920 | 0 | 0 | required direct state fields missing; depletion is not proven 30-second one-sided persistence; root event/onset/survivor state missing; measured latency, exact L2 exit, settlement, fee truth and scheduled start missing; reviewed post-decision cancel/lifecycle stream is also not installed |
| B09 Listing-to-Start Drift | `BLOCKED_SOURCE_FIELDS` | 0 | 0 | 0 | train artifact unbound; direction/cell/horizon SHA unbound; no listing-age/scheduled-phase rows; measured latency, exact L2 exit, settlement, fee truth and scheduled start missing |

These are honest blockers, not test failures. The code correctly refuses
to reinterpret scenario latency as measured production latency, gross
markouts as executable exits, or descriptive evidence as Net PnL.

## Production lineage delivery gap

A repository-wide file and schema search found:

- the lineage validator and synthetic test helper;
- no production lineage extractor command/service;
- no production `pnl-spine-lineage-receipt-v1` artifact;
- only the existing PnL latency blocked/inventory receipts under the
  frozen registry receipts directory.

Consequently, the new lineage contract is a working **consumer gate**, but
the real producer side is not delivered yet. Synthetic lineage created in
`tests/test_pnl_spine_runner.py` proves the validator; it is not evidence
that exact S3 objects were read by a production extractor.

The real-data adapter also states that current C1 inputs are engineering
evidence only: scenario latency, gross top-of-book markouts, missing
settlement truth, and a fail-closed fee receipt. Passing the lineage code
gate does not remove any of those economic blockers.

## Release recommendation

Approve `f93278f` as the fail-closed code foundation for the shared PnL
spine. Do **not** approve publication of real experiment Net PnL yet.

Before the first publishable real run:

1. implement and independently audit a production extractor that reads
   the exact versioned release objects and emits
   `pnl-spine-lineage-receipt-v1`;
2. externally pin the raw lineage receipt, trusted-authority v2, extractor
   code, and extractor config;
3. deliver measured PLACE/CANCEL/IOC_EXIT latency;
4. deliver exact executable L2 exits, strict fills, fee truth, finalized
   settlement truth, and complete position closure;
5. supply the direct A01/A11 source fields and the reviewed A11
   post-decision lifecycle/cancel stream;
6. train and seal B09 using its precommitted split and required artifacts;
7. rerun the same 176-test PnL gate, 216-test top-level gate, and lineage
   attack matrix against the production package.

Until then, the only correct overall state is:

`PASS_FAIL_CLOSED_WITH_REAL_RUN_BLOCKERS`
