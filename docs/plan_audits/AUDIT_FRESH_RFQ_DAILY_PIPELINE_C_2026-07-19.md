# Independent Audit — Fresh RFQ Daily Eligibility/Publication Pipeline (Worktree C)

**VERDICT: PASS_WITH_EXPLICIT_BLOCKERS**

- **Audited SHA:** `81d9e3bd40a2f32678e704d481e8f5ee11511561` (branch
  `codex/fresh-rfq-daily-auto`, worktree `/private/tmp/hft-fresh-rfq-daily-auto`).
  HEAD verified equal to the target SHA; working tree clean. No SHA drift.
- **Auditor scope:** read-only on code; ran test suites and constructed
  adversarial bypasses in a scratch dir. Zero code/commit/deploy/SSH/AWS actions.
- **Findings:** 0 P0, 0 P1, 3 notes. Two explicit undeployed deployment gates
  (by design, correctly represented as gates, not applied).

The pipeline is internally correct and its honesty gates hold under adversarial
probing. The "BLOCKERS" in the verdict are the two intended, undeployed
deployment gates (IAM delta + publisher adapter) plus the absence of any
production service/timer deployment — none of which this repair work applied.

---

## What was verified (claimed properties, each confirmed)

1. **Cheap preflight before heavy work** — `fresh_rfq_daily_runner.py:546-608`
   streams the session ledger, checks single-session, health, seal, local 24+2
   completeness, and final-container inventory *before* line 610-611's explicit
   boundary; only past that line does it hash large bodies or issue exact GETs.

2. **Per-date alerts / health gate** — `fresh_rfq_daily_eligibility.py:273-351`
   `build_health_receipt` observes the fixed producer alert path
   (`/home/ubuntu/hft-bot/work/live/rfq_alert.json`, pinned line 75). An ALERT
   inside the analysis-day window fails closed (`CAPTURE_ALERT_PRESENT`, line
   312-316). Health receipt is mandatory and cannot be self-declared absent
   (validated `fresh_rfq_daily_eligibility.py:354-426`; test
   `test_health_receipt_is_mandatory_and_cannot_be_self_declared_absent`).

3. **Streaming ledger** — `_select_segments_path`
   (`fresh_rfq_daily_runner.py:390-440`) reads the append-only ledger line by
   line with per-line hard bound `MAX_LEDGER_LINE_BYTES` (4 MiB) and no
   whole-file ceiling; fstat/inode signature re-check fails closed on concurrent
   change (`LOCAL_INPUT_CHANGED`, 433-437). Test
   `test_session_ledger_is_streamed_and_not_bound_to_whole_file_limit` passes.

4. **Per-object exact overlay/cache/checkpoints** — `ExactS3Reader.open_exact`
   (`fresh_rfq_daily_runner.py:225-256`) caches each object by a digest of its
   full exact identity (bucket/key/version_id/size/sha256, lines 212-217) and
   re-verifies size+sha on every cache hit (`_verify_cache`, 219-223,
   `EXACT_CACHE_IDENTITY_MISMATCH`). Overlay binds to the eligibility marker's
   exact set (`fresh_rfq_overlay_release.py:344-356`, `OverlayError` if the RFQ
   set differs). Test `test_exact_reader_disk_checkpoint_prevents_repeat_get`
   and `test_streaming_overlay_checkpoints_completed_heavy_stage` pass.

5. **Durable date queue** — `fresh_rfq_daily_pipeline.py` seeds from T0
   (`seed_queue:210-224`, `first = PRODUCTION_STRICT_T0_UTC[:10]`), never drops
   a date on age (module docstring + `test_date_queue_never_drops_a_date_after_
   ninety_days`), atomic per-date state files with self-digest
   (`_atomic_state:130-159`, `_validate_state:162-182`).

6. **Bad-date quarantine/retry** — `_error_state:340-354`: permanent-integrity
   codes (`OLD_LINEAGE_OVERLAP`, `CAPTURE_ALERT_PRESENT`,
   `MULTIPLE_CAPTURE_SESSIONS`, `BASE_TERMINAL_AMBIGUOUS`) → `QUARANTINED_DATA`
   with sentinel next-attempt `9999-12-31`; transient → `RETRY_SCHEDULED` with
   exponential backoff (`_next_retry:318-321`). One quarantined date does not
   head-block later dates (`test_one_quarantined_date_does_not_head_block_
   later_dates`).

7. **Honest WAITING_IAM / WAITING_PUBLISHER_CREDENTIALS states** —
   `process_date` (`fresh_rfq_daily_pipeline.py:438-459`): if no capability file
   → `WAITING_IAM` / `NOT_AUTHORIZED`; if capability present but
   `publisher is None` → `WAITING_PUBLISHER_CREDENTIALS` /
   `IAM_ATTESTED_NO_CREDENTIAL_ADAPTER`. The production CLI path
   (`main:531-537` → `run` with default `publisher=None`, line 481) **never**
   supplies a publisher; a publisher is injected only in tests. Confirmed by
   adversarial run below — the states cannot be bypassed to reach any AWS write.

8. **Strict T0 / generation binding consistency** — `2026-07-20T00:00:00Z` and
   `fresh-rfq-20260720-01` are pinned identically in eligibility constants
   (`fresh_rfq_daily_eligibility.py:63-70`), runner authority path
   (`fresh_rfq_daily_runner.py:39`), pipeline capability path
   (`fresh_rfq_daily_pipeline.py:43`), installer
   (`install_kalshi_fresh_rfq_daily_producer.sh:10-11`), service unit
   (`kalshi-fresh-rfq-daily-producer.service:17`), and release JSON
   (`W-RFQ-FRESH-01_PRODUCTION_AUTHORITY_BINDING_2026-07-18.json`). Authority
   file SHA `2fa1caf7…`, authority_sha256 `11faaf27…`, envelope_sha256
   `0ef4e0d5…` agree across code, installer, and binding JSON. `build_eligibility`
   rejects any analysis day `< T0` (`BEFORE_STRICT_T0`, line 517-518) and
   `seed_queue` never seeds a pre-T0 date. Overlay derives generation/authority
   transitively from the eligibility marker (`fresh_rfq_overlay_release.py:
   467,504`), so the whole chain shares one binding.

9. **No stale `20260719` / old SHAs (`654058`/`523543`/`8de2be`)** in the
   fresh-RFQ production scope. `git grep` over `tools/fresh_rfq*.py`,
   `deploy/*fresh-rfq*`, `deploy/install_kalshi_fresh*`, and
   `W-RFQ-FRESH-01_*` returned **zero** old SHAs and zero stale generation.
   The only surviving `2026-07-19` strings are legitimate: the
   `precommitted_at_utc` timestamp in the authority-binding JSON, and test
   fixture dates (incl. a pre-T0 alert-window test and a CLI-plumbing test with
   `produce` stubbed). The stale `fresh-rfq-20260719-01` / `8de2bef2…` appear
   only in `docs/SESSION_LOG.md` history (prior generation record), not in any
   executable or release artifact.

10. **Old 284-object cohort remains DATA_INTEGRITY_BLOCKED** — `OLD_284_OBJECT_
    COUNT == 284`, `OLD_284_OBJECT_SET_SHA256 == 1873803765e70de6…` (matches the
    authority binding JSON `old_284_object_set_sha256`).
    `old_lineage_state: DATA_INTEGRITY_BLOCKED`, `repair_state: FORBIDDEN`
    emitted in the eligibility marker (`fresh_rfq_daily_eligibility.py:648-652`)
    and receipts (`fresh_rfq_receipts.py:219-263`). `build_eligibility` fails
    `OLD_LINEAGE_OVERLAP` (line 583-586) if any evidence object matches a
    denied (size, sha256).

11. **Zero AWS access/writes by the repair work.** `AwsReadOnly._run`
    (`fresh_rfq_daily_runner.py:137-141`) hard-allowlists only
    `s3api head-object`, `s3api get-object`, `s3api list-object-versions` and
    raises `AWS_OPERATION_FORBIDDEN` for anything else — verified live below.
    The installer performs no AWS call (grep: none). Coordinator run summary
    reports `aws_writes_by_coordinator: 0` while `publisher is None`.

12. **Test suites green.** `python3 -m pytest -q
    tests/test_fresh_rfq_daily_eligibility.py tests/test_fresh_rfq_daily_runner.py
    tests/test_fresh_rfq_overlay_release.py` → 19 passed. Also ran
    `tests/test_fresh_rfq_daily_pipeline.py` → 4 passed.

---

## Adversarial attempts that did NOT break it

- **Bypass WAITING_IAM with no capability file** → still `WAITING_IAM` /
  `NOT_AUTHORIZED`, publisher never invoked. (scratch run)
- **Forge a valid `IAM-CAPABILITY.json` from the public constants** (generation
  and authority_sha256 are public; policy SHAs are unconstrained; self-digest is
  a plain sha256) → `load_capability` *accepts* it, but the production path
  (publisher `None`) still halts at `WAITING_PUBLISHER_CREDENTIALS` /
  `IAM_ATTESTED_NO_CREDENTIAL_ADAPTER`. **No AWS write is reachable.** A local
  actor cannot inject a publisher through any state/config file — the publisher
  is a code-level callable absent from the production wiring.
- **Invoke write-class AWS ops through the transport** (`put-object`,
  `put-object-tagging`, `s3 cp`, `delete-object`) → all rejected with
  `AWS_OPERATION_FORBIDDEN` before exec.
- **Craft a pre-T0 analysis date** → `BEFORE_STRICT_T0` (gate) and `seed_queue`
  will not enqueue it.
- **Enter the queue without quality receipts** → `build_eligibility` requires
  exactly 26 PASS hour receipts reconciled byte-for-byte against the session
  ledger (lines 527-561), single capture session (567-570), full_v2 seal
  (587-593), mandatory health receipt (572-574), and a paginated close inventory
  bound to the source exact set (600-601); the atomic `ELIGIBLE.json` is written
  last by directory rename (`create_package:747-769`) and re-derived on every
  load (`load_package:772-810`, `ELIGIBILITY_INVALID` if the marker differs from
  rebuilt evidence). A hand-written marker cannot survive `load_package`.

---

## Notes (defense-in-depth, non-blocking)

- **NOTE-1 (capability marker is local trust-on-file, not real IAM proof).**
  `load_capability` validates schema/generation/authority + self-digest but
  cannot prove the AWS IAM delta was actually applied; its policy SHAs are
  free-form. This is acceptable *only because* the downstream publisher-absence
  gate makes a forged capability inert (no write path). If a real publisher
  adapter is ever installed, the capability marker must be tied to an
  independently verifiable AWS-side attestation, not merely a local file.
- **NOTE-2 (queue state self-digest is forgeable).** `state_sha256` is a plain
  canonical sha256 with no secret; a local actor with write access to the state
  dir could forge a terminal `PUBLISHED` state. Impact is limited to marking a
  date locally-complete — it injects no publisher and causes no AWS write. Worth
  hardening (or documenting the state dir as a trusted boundary) before any
  write-capable deployment.
- **NOTE-3 (installer/timer not exercised here).** The installer refuses on a
  dirty tree / non-root / runtime-commit mismatch and enables but does not start
  the timer ("INSTALLED_NOT_STARTED"). It was read, not executed (read-only
  audit); its refusal logic is sound on inspection but unverified at runtime.

---

## Explicit remaining deployment gates (NOT applied by this work)

1. **Fresh-RFQ exact tag/read IAM delta = `DRAFT_NOT_APPLIED`.**
   `W-RFQ-FRESH-01_MINIMAL_IAM_DELTA_DRAFT_2026-07-18.json` state field is
   literally `DRAFT_NOT_APPLIED`, `aws_mutations: 0`. Present it as a draft
   requiring separate operator-authorized application — never as applied.
2. **Publisher credential adapter = NOT installed.** No publisher callable is
   wired into the production coordinator path; the pipeline can at best reach
   `WAITING_PUBLISHER_CREDENTIALS`. Installing the adapter is a separate,
   post-IAM, independently reviewed step.
3. **No production service/timer deployment has occurred**, and this independent
   audit (now delivered) was itself a listed gate. Do not deploy the timer
   merely because local tests pass.

Old damaged RFQ (284-object cohort, date=2026-07-13 family) stays
`DATA_INTEGRITY_BLOCKED` / repair `FORBIDDEN` everywhere; do not repair,
line-filter, or analyze it.
