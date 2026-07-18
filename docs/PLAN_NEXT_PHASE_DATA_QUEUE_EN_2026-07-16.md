# Next-Phase Data Queue — Priority Plan v2 (POST-AUDIT AMENDED)

**Status: AMENDED per independent audit 2026-07-16 — awaiting operator
ratification.** All six audit findings accepted; ordering restructured into
two parallel tracks per the auditor's proposal.
**Revision record:** v1 (pre-audit) findings accepted: settlement is
backfillable (not time-debt); data-debts-before-new-load ordering restored
(PIPE_INCIDENT_AND_PLAN_2026-07-14:34); zero-copy elevated to P0 with full
scope (publisher + consumer + IAM, not IAM alone); canonical per-object
receipts added as zero-copy prerequisite; S3 tiering demoted to last
(dangling-reference hazard); receive-clock column pinned to
`local_recv_ts_us` explicitly (recv_mono_ns carries B14 defect).
Chinese pre-audit draft retained for history; THIS file is authoritative.

**Ordering principle:** sort by interest rate (time-debts first), subject
to the incident-plan precedent: **repay data debts before adding capture
load**.

---

## TRACK 1 — Data Line (sequential; each step gates the next)

### T1-1 · Seal Reconciliation + W-A Deploy (now)
- Produce the authoritative per-day seal status table (seal? verify PASS?
  published?) — resolve the contradictory session reports; pin one truth.
- W-A three-in-one seal fix: independent audit (order already in
  SESSION_LOG 2026-07-14 23:40) → deploy on PASS under capture-continuity
  review.
- Acceptance: status table on disk; 3 consecutive unattended daily seals.

### T1-2 · B14 Timestamp Fix (standalone W, after T1-1)
- Fix the L2 capture-side bad-timestamp defect. Regression test that bites.
- NOT bundled with any capacity change (audit finding #4).

### T1-3 · Lifecycle WS Capture (the one true time-debt, after T1-2)
- New raw family: market state transitions (open/pause/close/settle) via
  WS, byte-exact envelope, enters seal/publish/intake chain.
- Game-start times: inference from market metadata/behavior only, every
  derived value hard-marked `INFERRED` — never presented as authoritative.
- Sources: WS messages + periodic REST status snapshots + inferred
  transitions (some state changes emit no WS message — WS alone cannot
  yield a complete chain; every inferred transition marked `INFERRED`).
- Acceptance: 7-day completeness audit — every market that settled in the
  window has a full transition chain from the combined sources (not a 48h
  spot check); plus settlement cross-check vs T1-4 backfill where both
  exist.

### T1-4 · Settlement Backfill (independent W, checkpointable, anytime)
- Backfill historical settlement results + times via the official
  Get Markets API for settled markets (read-only, paginated, resumable).
- Reclassified from time-debt to backfillable (audit finding #5).
- Acceptance: coverage report (settled markets with result populated /
  total); sample n≥50 verified against exchange pages.

### T1-5 · L2 Capacity Test → Staged Expansion (after T1-1..T1-3)
- First a capacity test (bandwidth/storage/cost at 2×, 4× current target
  count) — numbers to operator BEFORE any expansion.
- Then staged expansion prioritizing active sports game markets.
  Fail-closed design preserved: L2 failure never touches main firehose.
- Explicit boundary: NO full-market L2.

## TRACK 2 — Research-Access Line (parallel to Track 1)

### T2-1 · Canonical Per-Object Receipts (prerequisite, now)
- Today's `aws s3 sync` records no per-object receipt. Add durable receipts
  for canonical uploads: bucket, key, VersionId, size, SHA-256, seal
  linkage (audit finding #3). Without these, manifest-only releases cannot
  guarantee reproducibility.
- Rider (from earlier survey): S3 durable receipt becomes a precondition
  for local raw prune (closes the "prune before upload confirmed" hole).
- Execution contract and staged stop gates are fixed in
  [`PLAN_W_PUB_REF_01_ZERO_COPY_2026-07-16.md`](PLAN_W_PUB_REF_01_ZERO_COPY_2026-07-16.md),
  W-PUB-REF-01A. Receipt shadow and prune dry-run come before any prune
  behavior change; missing receipts always retain data.

### T2-2 · W-PUB-REF-01 v3 Zero-Copy Publishing (P0, full scope)
- Authoritative implementation plan:
  [`PLAN_W_PUB_REF_01_ZERO_COPY_2026-07-16.md`](PLAN_W_PUB_REF_01_ZERO_COPY_2026-07-16.md).
  It must receive independent audit PASS and operator ratification before
  any implementation or AWS mutation.
- Three coordinated changes (audit finding #2 — IAM alone is insufficient):
  1. Publisher: emits MANIFEST.json only (bucket/key/VersionId/size/sha256
     per object) — no byte copies;
  2. W09 IAM: read-only access to the canonical prefixes, scoped as tightly
     as prefix design allows; NO Put/Delete anywhere outside its run area;
     W09 must never read in-flight or unsealed raw objects. IAM permits
     tagged, allowlisted exact versions, while the consumer permits only
     objects named by the manifest. RFQ remains an explicit opt-in and is
     eligible only when already sealed, receipt-verified and not marked
     quarantined/DATA_INTEGRITY_BLOCKED; this does not reopen RFQ repair;
  3. Consumer: `research_data.py` taught to resolve manifest references
     (currently hardcoded to research/releases/ paths).
- Hard riders (retained from prior ruling): no-delete guarantee on
  referenced objects; daily metadata reference-integrity patrol (every
  referenced VersionId alive, immediately readable, correctly tagged, and
  in an allowed storage class). Failures freeze new v3 publication and
  create a durable yellow alert artifact. This W adds no Telegram feature.

### T2-3 · Canary Acceptance → Retire Copy-Mode Publishing
- Minimum acceptance (audit's list, adopted verbatim):
  research prefix gains manifests only; W09 reads exact VersionIds from
  canonical prefixes; W09 retains zero Put/Delete; same-day v2 copy release
  and v3 reference release, compared at the same `rfq_included` setting,
  yield identical input SHA-256 sets and
  identical core research results (NOT byte-identical report files —
  reports legitimately embed run timestamps); repeat-publish uploads zero
  data bytes; old v2 releases retained for rollback; S3 lifecycle must not
  break any referenced version.
- Only after canary PASS and rollback drill: put copy-mode publishing in
  standby. Existing VERIFIED v2 releases stay immutable and selectable as
  the rollback path; they are not deleted or rewritten.

## PARALLEL SMALL ITEMS (slot anywhere)

- **Fee ratification:** pin maker/taker fees per product line (official
  schedule + measurement); fees.verified → true; single facts entry
  referenced by both the risk framework (A3) and the backtest fee model.
- **Receive-clock discipline:** verdict-grade time axis =
  **`local_recv_ts_us`** (explicitly this column; `recv_mono_ns` carries
  B14 and must not be used until T1-2 lands). Inventory tools keyed on
  ts_utc; fix defaults where feasible; hard-stamp the rest
  `DIAGNOSTIC_ONLY_EXCHANGE_CLOCK`.

## LAST — S3 Lifecycle Tiering (strictly after T2-3 canary PASS)

- Demoted from "one config" to a gated change (audit finding #6): tiering
  or noncurrent-version cleanup BEFORE zero-copy acceptance could silently
  break reference manifests. Design must exclude every referenced
  VersionId; deploy only after the reference-integrity patrol is live.

## Standing Prohibitions (unchanged)

Research auto-controller, more Telegram features, more descriptive reports,
any RFQ repair, complex ML, external data API purchases, live deployment,
AUTO_RESEARCH=1, automating the old copy publisher.

## Universal Discipline (unchanged)

Five Brakes; one session one W; independent audit per W; PLANS_LEDGER
registration; exit ritual. Production-touching items: capture-continuity
review + heartbeat evidence before/after. Spending: >$100/month recurring
per-item ruling; else monthly report.
