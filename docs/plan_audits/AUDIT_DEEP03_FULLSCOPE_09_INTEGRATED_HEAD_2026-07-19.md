# Independent adversarial audit — DEEP03 full-scope .09 INTEGRATED HEAD

**VERDICT: PASS**

- Audited HEAD: `3c643cb18ea995f045965359b785fa2c8a051416`
  ("Draft .09 W0/W1 release candidates bound to the integrated runtime commit")
- Worktree: `/Users/ritcardo/HFT-BOT-deep03-fullscope-09`, branch
  `w-deep03-fullscope-09`, `git status --porcelain` empty at audit start AND
  after all test/attack runs (re-verified; nothing in this audit modified
  tracked files — this report is the only write).
- Auditor: independent adversarial session, 2026-07-19. I did not write the
  code under audit. Scope per tasking: certify what INTEGRATION added on top
  of two already-PASSed component audits — (a) L2/base runtime at `1fa7481`
  (AUDIT_DEEP03_FULLSCOPE_L2_TTL_SNAPSHOT_2026-07-19.md, VERDICT PASS,
  header re-read and confirmed) and (b) fresh-RFQ module at `77416c0` of
  `w-deep03-fullscope-rfq-repair-09` (AUDIT_DEEP03_RFQ_QUALITY_D07_
  ATTESTATION_2026-07-19.md re-audit, PASS with MANDATORY blocker I4b).
  Those audits were NOT redone here; their conclusions are relied upon only
  for code proven byte-identical below.
- Handoff `docs/plan_releases/DEEP03_FULLSCOPE_L2_FRESH_RFQ_HANDOFF_
  2026-07-19.md` read in full; continuation-order steps 2, 3 and 5 are what
  this HEAD claims to complete; hard prohibitions re-checked (no deploy, no
  research start, no IAM, no old-RFQ analysis — nothing at this HEAD does
  any of these; `main()` gained no overlay wiring, no service auto-start).

Commit accounting `1fa7481..3c643cb` (13 commits, every one classified):

| Commits | Content | Audit treatment |
|---|---|---|
| `65c974b`, `ee2623f` | doc-only audit archives (+188, +187 lines, one file each — verified by `--stat`) | archive review only |
| `70eb858`..`768b4bd` (8) | cherry-pick of audited RFQ range | §1 blob identity |
| `b939fd8` | integration wiring | §2 adversarial review |
| `d65b53c` | .09 release-chain bump | §3 |
| `3c643cb` | .09 W0/W1 candidates | §3, §5 |

## 1. BLOB-IDENTITY — PASS (byte-exact)

The cherry-picked range `ee2623f..768b4bd` touches EXACTLY the 8 files of the
audited repair range `33f779e..ab220f8` (name-lists compared; no extra file).
Git blob SHAs compared three-way (integrated HEAD vs `77416c0` vs `ab220f8`
in `/Users/ritcardo/HFT-BOT-deep03-fullscope-rfq-repair`):

| File | blob @HEAD | identical to |
|---|---|---|
| tools/research/deep03_v3_rfq_bounded.py | `491a4186…` | 77416c0 ✅ ab220f8 ✅ |
| tests/test_deep03_v3_rfq_bounded.py | `faa4886c…` | 77416c0 ✅ ab220f8 ✅ |
| tools/fresh_rfq_request_provenance.py | `01ed17ff…` | 77416c0 ✅ ab220f8 ✅ |
| tests/test_fresh_rfq_request_provenance.py | `601839d9…` | 77416c0 ✅ ab220f8 ✅ |
| tests/data/l2_gaps_2026-07-12.json | `55d0bae3…` | 77416c0 ✅ ab220f8 ✅ |
| tests/data/l2_gaps_2026-07-13.json | `f31dd15d…` | 77416c0 ✅ ab220f8 ✅ |
| docs/research_reports/DEEP03_FRESH_RFQ_D01_D07_BOUNDED_MODULE_2026-07-19.md | `a969372f…` | 77416c0 ✅ ab220f8 ✅ |
| docs/plan_audits/AUDIT_DEEP03_RFQ_QUALITY_D07_ATTESTATION_2026-07-19.md | `a781ceb2…` | ab220f8 ✅ (vs 77416c0: +150/-0 — pure append of the re-audit containing I4b; zero removed lines, verified by diff grep) |

Cross-branch clobber check: for every shared pre-existing file
(fresh_rfq_request_provenance.py, its test, deep03_v3_rfq_bounded.py, its
test, the module doc), the L2-audited version at `1fa7481` is blob-identical
to the RFQ branch's base `33f779e` — so the cherry-pick could not and did
not overwrite any L2-audited semantics.

The six RFQ dependency modules pinned by the new manifest but outside the
repair diff (fresh_rfq_exact_reader / base_binding / market_mapping /
receipts / universe_provenance, plus request_provenance above) are each
blob-identical between HEAD and `ab220f8` — the audited module's imports
resolve to exactly the audited bytes.

L2 side: `git diff --name-status 1fa7481..HEAD` = 25 paths, every one either
(a) an audited-RFQ blob above, (b) a doc-only audit archive, or (c) a
deliberate integration/release file reviewed hunk-by-hunk in §2/§3. No
unexplained file. `deep03_fullscope_runner.py` diff removes exactly ONE
pre-existing line (the `typing` import, re-added with `Callable`); all other
audited L2 code (`deep03_v3_methods.py`, `deep03_v3_l2.py`,
`deep03_fullscope_graph.py`, `deep03_v3_runner.py`, `exploratory_
autoresearch.py`, gate cost/scope logic) is untouched — confirmed both by
diff absence and by both SHA manifests verifying (§4).

## 2. NEW WIRING (b939fd8) — PASS (survived the attack battery)

Design reviewed in full (+319 runner lines, +491 test lines, deploy
packaging). Findings:

- `run_fresh_rfq_overlay` signature has NO `d07_external_anchor` parameter,
  NO `**kwargs`, and no other anchor-object-shaped parameter (verified by
  `inspect.signature`, mirrored by the committed contract test
  `test_anchor_contract_is_root_pinned_and_object_free`). Repo-wide grep:
  the ONLY call sites of `load_d07_external_anchor` /
  `run_bounded_fresh_rfq` in `tools/` + `deploy/` are inside the runner
  itself; `main()`/CLI expose neither. The I4b forgery (caller-built anchor)
  is no longer expressible through this runtime — closing the MANDATORY
  deployment blocker, including its requirement that the integration audit
  verify the wiring "with its own adversarial test" (below).
- Anchor may exist only at the pinned path
  `/etc/w09/deep03/d07-external-anchor.json`; loader enforces path equality,
  `O_NOFOLLOW|O_CLOEXEC`, `fstat` on the OPENED fd (not a racy pre-stat):
  regular file, owner uid == 0 by default, mode EXACTLY 0444, size in
  (0, 1 MiB], re-read length == fstat size, JSON object, then defers schema
  entirely to the audited module's `_validate_d07_external_anchor`
  (state must be `INDEPENDENT_AUDIT_PASS`, exact key set, digest lists,
  self-digest).
- Missing anchor is honest: `D07_ANCHOR_NOT_INSTALLED` is caught, recorded
  as `NOT_INSTALLED_D07_BLOCKED_UNANCHORED`, and the audited module
  (verified at `deep03_v3_rfq_bounded.py:2900-2911`) returns D07
  `BLOCKED_UNANCHORED_EVIDENCE` with `claim:
  NO_RFQ_TO_CLOB_IMPACT_RESULT` on a None anchor. Any other anchor error
  fails the run closed (verified: no receipt written on tampered anchor).
- Missing eligibility is honest: invalid fresh authority →
  `BLOCKED_FRESH_AUTHORITY`; wrong generation/T0 vs the pinned
  `fresh-rfq-20260720-01` / `2026-07-20T00:00:00Z` → `BLOCKED_COHORT_PIN`;
  no READY overlay → `WAITING_FRESH_RFQ_ELIGIBILITY` — all with
  `result_kind: EXPLICIT_NON_RESULT_STATE`, `fabricated_rows: 0`, and
  `client_factory` provably never invoked (test uses a raising factory).
  Receipt writes are exclusive-create; a second run refuses to overwrite;
  a symlinked/pre-existing receipt path is refused before any work.
- Cohort separation: receipts pin `cohort: FRESH_RFQ_POST_T0_2026-07-20`,
  `base_cohort: HISTORICAL_BASE_L1_TRADES_L2_2026-07-10_2026-07-17`,
  `cohort_join: FORBIDDEN_SEPARATE_SOURCE_BINDINGS_AND_LEDGERS`,
  `historical_input_manifest_rfq_entries: 0`, old damaged RFQ
  `DATA_INTEGRITY_BLOCKED_NO_REPAIR_NO_ANALYSIS`. The base runner's
  pre-existing `_require_base_without_rfq` (unchanged from the audited
  `1fa7481` state; only comments reference it in the diff) refuses any
  base manifest whose policy ≠ `FORBIDDEN_AND_ABSENT` or that contains any
  rfq kind/channel/path object — re-exercised by the committed contamination
  test and green in this run. The .09 authority gate additionally
  hard-requires `rfq_included: False` in the W1 release binding
  (`deep03_authority_gate.py:641`) and canary `rfq: OFF_AND_ABSENT`, so an
  RFQ-bearing base release cannot even pass the gate.
- Packaging: the 7 RFQ modules are added to BOTH deploy scripts and BOTH
  SHA manifests; a committed test recomputes every pinned digest against the
  repo bytes and asserts push/install coverage. Installation adds files
  only; no service change auto-starts anything.

### Attack log (this auditor's own battery, `attack_anchor.py`, all run at HEAD)

| # | Attack | Result |
|---|---|---|
| A1 | relative path `etc/w09/…` | refused `D07_ANCHOR_PATH` |
| A2 | `/etc/w09/deep03/../deep03/…` dot-dot alias | refused `D07_ANCHOR_PATH` (Path keeps `..` parts — no normalization bypass) |
| A3/A4 | `//` double-slash and trailing-slash aliases | normalize to the SAME pinned file (no redirection possible); proceed to open → `D07_ANCHOR_NOT_INSTALLED` on this host |
| A5–A8 | mode 0555, 0400, 0644, 04444 (setuid bit) | all refused `D07_ANCHOR_MODE` (S_IMODE exact-equality catches permission AND setuid/sticky bits; tasking's 0555 case explicitly covered) |
| A9 | non-root-owned file vs default uid 0 | refused `D07_ANCHOR_OWNER` |
| A10 | symlink as final component | refused `D07_ANCHOR_PROVENANCE` (O_NOFOLLOW) |
| A11 | symlink as PARENT directory | accepted (probe; see observations) |
| A12 | real 1 MiB + 1 byte file | refused `D07_ANCHOR_PROVENANCE` |
| A13 | empty file | refused `D07_ANCHOR_PROVENANCE` |
| A14 | `state: SELF_DECLARED_PASS` with recomputed self-digest | refused `D07_ANCHOR_INVALID` |
| A15 | forged producer digest, stale self-digest | refused `DIGEST_MISMATCH` |
| A16 | extra key smuggling (`extra_grant: FULL_SCOPE`) | refused `SCHEMA_FIELDS` (exact key set) |
| A17/A18 | JSON array / directory at path | refused `D07_ANCHOR_INVALID` / `D07_ANCHOR_PROVENANCE` |
| A19 | signature probe for anchor-object or var-kwargs smuggling path | none exists |
| A20 | valid 0444 receipt at (repointed) pin | accepted — checks are reachable, not vacuous |

Committed tests independently cover the same ground plus an fstat-spoof
owner check and module-not-called-on-tamper; my battery and the suite agree.

### Observations (recorded, judged non-blocking — neither reopens I4b)

1. **Parent-directory symlink not detected** (A11): `O_NOFOLLOW` guards only
   the final component. Exploiting this requires replacing a component of
   `/etc/w09/deep03/` — root-owned in production — so the attacker must
   already be root, at which point every control is moot. Optional hardening
   for a future change: `O_DIRECTORY`+`openat` walk or `os.path.realpath`
   equality.
2. **`anchor_owner_uid` parameter** on `run_fresh_rfq_overlay` /
   `load_d07_external_anchor` (default 0, asserted by the contract test):
   overriding it requires in-process Python invocation — the same trust
   level that could monkeypatch the pin constant — and the file must still
   live at the root-writable pinned path with mode 0444 and pass the full
   digest schema. Not reachable from CLI, config, service unit, or any
   production call site (none exist). Recommendation: when the future W09
   overlay entrypoint is written (a later, separately-gated step), it must
   not surface `anchor_owner_uid`/`anchor_path` as configuration — that
   entrypoint's audit should re-check this.

## 3. RELEASE CHAIN (.09) — PASS (consistent, byte-verified)

- `deep03_authority_gate.py`: W2A/W0/W1 = `D3-W2A/W0/W1-2026-07-19.09`;
  error strings updated; NOTHING else in the gate changed (10-line diff).
- `deep03_one_shot_arm.py`: `RELEASE_ID = D3-W2A-2026-07-19.09` (2-line diff).
- Service unit: gate/runner args now bind
  `/etc/w09/deep03/releases/D3-W0-20260719-09.json` and
  `…/D3-W1-20260719-09.json`; no other unit change (guards, sha
  ExecCondition, one-shot claim/consume, 24h timeout intact).
- Tests (one_shot_arm / w1_preflight / w09_exploratory) updated to .09 in
  lock-step; hunks are pure ID substitutions, zero assertions removed.
- Stale-ID sweep of `deploy/` + release-chain tests for any `.01`–`.08` /
  `20260718` remnant: **zero hits** (the untouched historical
  `DEEP03_D3_W1_COMPLETE_2026-07-18.08.json` record is a past artifact, not
  chain wiring).
- W0/W1 candidates: `release_id` .09; `runtime_commit =
  d65b53c37b3a36ef5ca220a1c396d0233153a14f` = HEAD^ (the release-chain bump
  atop the integration — verified `git rev-parse`); `deployment_state:
  FORBIDDEN_UNTIL_INTEGRATED_HEAD_AUDIT_AND_NEW_AUTHORITY_ARM`;
  `integrated_head_audit_state: REQUIRED_NOT_YET_PERFORMED` (true at commit
  time; THIS report is that audit, performed after and against that HEAD).
  Every referenced digest recomputed by me byte-exact: base audit sha
  `d7512801…` ✅, RFQ overlay audit sha `482bfd6c…` ✅ (binds the HEAD
  version WITH the I4b re-audit), adopted plan sha `ded84065…` ✅,
  `w0_release_sha256` `1bbf6f63…` ✅, RFQ module commit
  `77416c04922bd695cf4df83b4adf64c56b35f394` ✅ full-length match.
- Candidate diff vs .08 reviewed line-by-line: only release ids, audit
  bindings, runtime_commit, and the two new honesty fields changed; the
  8-date input release set (seal/pub ids), object count 2,657, bytes
  29,473,216,651, evidence tier, and all False-flags are unchanged.
- Instance `r8g.2xlarge` / `i-0e53d134dceffe166` unchanged; cost contract
  untouched (`$0.50918/hour`, spending cap `(0,15]` USD, runtime-cost
  coverage check — all outside the diff); DuckDB `16GB` / `2 threads`
  unchanged in both the W09 wrapper and the runner defaults.

## 4. SUITE — PASS (0 failures; counts reconciled)

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/` at HEAD: exit 0.
  JUnit-verified counts: **1820 collected, 0 failures, 0 errors,
  1816 passed, 3 skipped, 1 xfailed**.
- Expected "1816 passed / 2 skipped / 1 xfailed": the passed count matches
  EXACTLY. The third skip is `test_kalshi_golden` (V12 spec-drift gate)
  skipping because gitignored `work/kalshi_spec_alignment.json` is absent in
  this fresh worktree (it exists in the main repo) — an environment
  artifact, not a code delta; the RFQ branch audit saw the same 3-skip
  shape for the same reason.
- The two anticipated skips are `test_ephemeral_tagger_bootstrap` memfd
  tests, gated by `sys.platform.startswith("linux") and hasattr(os,
  "memfd_create")` — environment-dependent PLATFORM branches (this file is
  unchanged since the audited base). Note for the record: they are
  Linux-memfd gates, not "uid-0" branches as the tasking phrased it; no
  uid-0-conditional skip exists in this run. Nothing skipped is
  integration-relevant.
- The 1 xfail is the pre-existing settlements-export placeholder.
- SHA manifests, both verified with `shasum -a 256 -c`:
  `deep03_open_discovery_modules.sha256` — 15/15 OK (8 original + 7 RFQ
  modules); `exploratory_autoresearch_payload.sha256` — 10/10 OK.

## 5. SCOPE HONESTY — PASS

- `AUTHORIZED_METHOD_SCOPE` is still exactly `{D3-B01-MARKOUT, D3-B02-
  ONESIDE, D3-B03-XMKT, D3-B04-RHYTHM}` at partial-descriptive /
  preflight-only levels, and the gate REFUSES any AUTHORITY whose
  `authorized_method_scope` differs (`deep03_authority_gate.py:656`).
  Widening to fresh-RFQ/full-scope therefore requires an explicit operator
  AUTHORITY-schema change — it cannot be smuggled through data.
- Nothing at this HEAD claims fresh-RFQ or full-scope execution authority:
  candidates carry `research_execution_authority: false`, `rfq_included:
  false` (gate-enforced), `strict_acceptance_claimed: false`,
  `holdout_opened: false`; the overlay unit is a library capability with no
  CLI/service invocation path; the gate's canary contract still requires
  RFQ `OFF_AND_ABSENT`; the D07 anchor loader additionally requires a
  root-installed independent-audit receipt that does not exist yet.
- The overlay receipt itself disclaims (`candidate_or_profit_claim: false`,
  base-cohort claims NONE) and the pinned-generation check makes July-20+
  eligibility a data-driven gate, not a code assertion.
- Handoff prohibitions re-checked at HEAD: no deployment artifact marks any
  commit production-ready; no old-RFQ path is readable by the new code (the
  fresh authority embeds the 284-object deny-set digest, audited module
  behavior); no IAM/AWS mutation appears anywhere in the diff.

## What remains open (deliberately, per the release chain — not audit findings)

1. Operator AUTHORITY/ARM for the .09 series does not exist; deployment and
   any W09 start remain forbidden (release metadata says so itself).
2. The `/etc/w09/deep03/d07-external-anchor.json` receipt is not installed
   anywhere; D07 stays `BLOCKED_UNANCHORED_EVIDENCE` until a real
   independent-audit receipt is root-installed at deploy — the honest state.
3. Fresh-RFQ eligibility evidence cannot exist before ~2026-07-21T02:00Z;
   until then the overlay can only emit WAITING/BLOCKED receipts.
4. Any widening of `AUTHORIZED_METHOD_SCOPE` beyond narrow B01–B04 is an
   explicit operator decision at the authority step (§5).

**Final verdict: PASS.** Blob identity holds byte-exact against both audited
component states; the integration wiring closes I4b and survived a 20-case
adversarial battery with only two root-privilege-equivalent residual
observations; the .09 release chain is internally consistent with every
digest re-verified; the full suite is green at 1816 passed / 0 failed; and
the runtime's authority surface still claims exactly nothing it has not been
granted.

— end of audit —
