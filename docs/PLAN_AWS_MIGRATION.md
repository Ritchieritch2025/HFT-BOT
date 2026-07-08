# PLAN — AWS / EC2 MIGRATION (MASTER_SEQUENCE STEP 1)  · SEED DRAFT

**Status: SEED (partial).** The full seven-field W queue — W-A0 sizing gate →
W-A1 hardening → W-A2 Linux validation → W-A3 S3 vault + RESTORE TEST → W-A4
zero-gap cutover (operator go/no-go) → W-A5 post-cutover, plus the STEP-1 riders
and the §6 self-audit — is **OPERATOR_PLAYBOOK item 4**, drafted in its own
session once an AWS account exists (S4: the agent never touches account creation
or credentials). This file pre-records the **W-A4 provisions the operator locked
2026-07-08** so they survive until then.

**W-A5 provision (operator standing requirement, 2026-07-08 — PDF reports
flow back to the Mac):** every daily/acceptance-class detection emits a typed
PDF report with numbers (playbook item 3c, tools/daily_report.py). Post-cutover
these are generated ON EC2 into reports/, included in the daily EC2→S3 sync
under a reports/ prefix, and a small launchd job on the Mac runs a daily
`aws s3 sync` of that prefix into the operator's local reports folder — EC2
generates, S3 relays, Mac lands. Offline Mac ⇒ reports arrive late, never lost.
PDFs are derived copies; the structured artifacts remain the authoritative
record (D1).

**Motivation (empirical, 2026-07-08).** Capture is NOT 24/7 because the Mac enters
Deep-Idle **sleep** constantly — proven to be the dominant cause of every capture
gap (`docs/plan_audits/capture_gap_taxonomy_2026-07-08.md`, APPENDIX: cases align
1:1 to `pmset` Sleep→Wake; the ~15-min "wedge" IS the DarkWake cycle). An
always-on Linux host that never sleeps is the permanent fix — this finding is
STEP 1's concrete justification.

---

## W-A4 — zero-gap cutover (operator go/no-go) · provisions pre-recorded

### RULE — SINGLE REST OWNER (operator-locked 2026-07-08)
During the Mac↔EC2 **overlap**, the periodic **REST** tasks (catalog_sync,
dim_snapshot, build_classification, settlements, batch-orderbook cross-check —
anything that spends the shared REST **rate budget** or writes catalog/dim state)
run on **exactly one machine at any instant**. Two machines polling REST at once
= double rate-budget burn + racing catalog writers (a corruption + throttle
hazard).

**WS capture concurrency is EXEMPT** — running the WS firehose on BOTH machines at
once is fine and in fact *wanted* for the overlap diff (below). Empirically
de-risked by the **2026-07-06 three-concurrent-connection test** (three live WS
connections captured cleanly; the exchange does not penalise duplicate read-only
market-data subscriptions).

**Cutover sequence (ordered — no instant where REST has two owners, and capture
never drops because WS overlaps throughout):**
1. **EC2 starts WS capture, REST DISABLED** (WS-only; the Mac remains the sole
   REST owner).
2. **Verify EC2 capture health** over a soak window — feed fresh, `ws_seq`
   continuous, zero gaps — before proceeding.
3. **Mac stops REST** (pause its periodic REST tasks; WS still running on both
   machines).
4. **EC2 starts REST** (now the single REST owner).
5. **Mac unloads launchd** (WS + everything) — EC2 is the sole owner of both WS
   and REST.

Rollback at any step: reverse the last action; because WS runs on both machines
through steps 1–4, no rollback loses capture.

### ACCEPTANCE — BONUS: dual-machine capture-completeness report
While both machines run WS (the overlap window), record BOTH firehoses and **diff
them record-by-record, aligned by `ws_seq`** (the per-channel sequence number).
Produce the project's first **"capture completeness empirical report"**:
- exact count of records present on one machine but missing on the other, **each
  direction**;
- the **single-machine miss rate** — what fraction a lone capturer actually drops;
- attributed by cause where possible (Mac sleep window vs other).

This measured miss-rate is the **baseline for the `seq_gaps` observability
counter** (the atomic per-channel sequence-continuity counter proposed
2026-07-08): the counter is calibrated and validated against this ground truth, so
a single-machine capturer's true loss is finally **quantified, not estimated**.
It also independently confirms the cutover was genuinely zero-gap (EC2 captured
everything the Mac did across the window).
