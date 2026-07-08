# PLAN — AWS / EC2 MIGRATION (MASTER_SEQUENCE STEP 1)

**Phase:** infra for Phase 1→2 (24/7 always-on capture). **Gate satisfied:** none
directly — this is the substrate every later gate depends on. A laptop that sleeps
can never produce a clean day (07-08 was **36.7% down** to Mac Deep-Idle sleep;
`docs/plan_audits/capture_gap_taxonomy_2026-07-08.md`). Interim mitigation (`pmset
-a disablesleep 1`) is live; this migration is the permanent fix.

**Status: FULL DRAFT.** Drafting needs no AWS account. **EXECUTION (W-A1 onward) is
gated on the operator** having an AWS account and creating all
credentials/`~/.kalshi/env.sh` by hand on the box (**S4 — the agent never touches
account creation or credentials**). W-A4 cutover is **operator go/no-go, operator
present.** One W per fresh session, independent audit after each (as the capture Ws).

## Governing rules (bind every W-A)
- **S4:** account + credentials are operator-only; `env.sh` is hand-created on the
  box (600), never via repo or agent, never printed.
- **P4:** the Mac pipeline is live revenue-critical until W-A4 completes. Nothing
  breaks Mac capture until EC2 is *proven*; the cutover keeps WS on **both** boxes
  so capture never drops. Mac launchd stays **installed but dormant** through
  cutover = instant rollback.
- **Single REST owner** (W-A4): never two REST pollers at once (rate-budget +
  catalog-writer race). WS concurrency is exempt (2026-07-06 three-connection test).
- **E1/D4:** every code/config change ships a test; `make check` +
  `tests/run_pipeline.sh` green **on the EC2 box** (W-A2) before cutover.
- **D1:** raw/archive are the source of truth; S3 copies are versioned, restore is
  proven BEFORE cutover (W-A3), never destroy Mac data until EC2 is authoritative.
- **Credential model (operator ruling 2026-07-08): long-lived IAM-user access keys
  in `~/.kalshi/env.sh` on the EC2 box** (NOT an instance role). Operator
  hand-creates env.sh (600, S4); the agent never reads/prints/places it. Because the
  key is a standing secret on a 24/7 host, the mitigations below are load-bearing,
  not optional.
- **Least-privilege IAM (makes D1 structural, not just procedural):** the IAM **user
  whose keys go in env.sh** gets `PutObject`/`GetObject`/`ListBucket` on the vault
  prefixes but **NO `DeleteObject` (and no lifecycle-expiry) on
  raw/archive/catalog prefixes** — so a bug or a leaked key *cannot* erase the
  vaulted history. Back it with S3 Object Versioning + a bucket policy denying delete
  on those prefixes. Deletes, if ever needed, are a separate operator action with
  separate credentials. Standing-secret hygiene: env.sh 600; the key is never in the
  repo/logs/reports; plan a rotation (W-A5 records the cadence).

---

## W-A0 — sizing gate (paper + read-only probe)
Purpose:        Confirm the instance is big enough BEFORE any bring-up; decide
                where the memory-heavy gold builds run.
Blocked by:     operator has launched an EC2 instance (else W-A0 is paper: state
                the required floor).
Allowed reads:  the instance `nproc` / `free -g` / `lsblk` (read-only over SSH);
                docs/warehouse_schema.md (peak-size facts).
Allowed writes: docs/PLAN_AWS_MIGRATION.md (record the measured sizing + decision).
Forbidden writes: anything on the box beyond reading; no installs.
Acceptance:     printed vCPU / RAM / EBS, **sized for the POST-migration load, not
                today's** — rider (b) promotes ALL categories to class_a_full_l1, so
                the box must carry the full-market L1 subscription universe
                (subscription count, bandwidth, storage/day), materially more than
                the current filtered set. Recommend ≥ 8 vCPU / **32 GB** / 200 GB
                gp3; the WS capture is light, the warehouse build is the RAM driver.
                **≥32 GB is the strong path** — gold builds run ON EC2 and the Mac
                becomes truly dormant (W-A5). If RAM < 32 GB is accepted, the daily
                gold build **stays on the Mac reading from S3** (17.7 GB peak) — a
                deliberate split; in that branch the Mac is **NOT dormant** (it runs
                a wake-scheduled daily build) and W-A5 does **NOT** fully re-enable
                Mac sleep (see W-A5). Pick the branch here so W-A5 is unambiguous.
Rollback:       n/a (read-only).
Exit evidence:  the sizing line + the gold-build-location decision in this doc.

## W-A1 — hardening & bring-up
Purpose:        Turn a bare Ubuntu box into a locked-down, reproducible pipeline
                host — repo, deps, time sync, service units — WITHOUT capture yet.
Blocked by:     W-A0.
Allowed reads:  the repo; deploy/ (created here); the box.
Allowed writes: `deploy/` — **systemd** unit files (pipeline supervisor, ingest)
                replacing the Mac's launchd; a bring-up script (clone, build-deps,
                modern Python ≥3.12 via pyenv/apt, chrony for time sync replacing
                macOS sntp, ixwebsocket/simdjson/openssl vendored build); an
                **SSH-only-from-operator-IP** security checklist (printed, applied
                by operator); README in deploy/.
Forbidden writes: `~/.kalshi/env.sh` (operator hand-creates it — S4); any secret;
                capture/ingest/export SOURCE (only build + service wiring).
Acceptance:     box builds `build/ws_shadow` + all binaries clean; systemd units
                load (dry, capture NOT started) and **reproduce the three
                load-bearing launchd behaviors** — (a) **top-of-hour respawn /
                hourly raw-log rotation** (the warehouse is built on hourly raw
                partitions; a wrong rotation cadence breaks the three-layer build),
                (b) the **single-instance lock** (systemd + app flock — never two
                ws_shadow writers), (c) the app's **internal W-C1 watchdog** must
                own reconnect, so systemd uses `Restart=on-failure` only (NOT
                `WatchdogSec=`) to avoid two supervisors fighting (restart storms /
                double reconnect). chrony synced (offset printed); SSH restricted to
                the operator IP; **egress allow-listed to 443 (Kalshi + AWS/S3
                endpoints)** — not left at the AWS all-allowed default; `env.sh`
                present (operator-made, 600) and the auth smoke (REST
                `/exchange/status`) passes.
Rollback:       terminate/rebuild the box; nothing on the Mac touched.
Exit evidence:  commit (deploy/ units + script); the hardening checklist output.

## W-A2 — Linux validation (prove it on the box)
Purpose:        Prove the whole pipeline is correct on Linux before trusting it
                with capture.
Blocked by:     W-A1.
Allowed reads:  the repo on the box.
Allowed writes: any test-only fixes for Linux portability (path/endianness/clock);
                a validation log.
Forbidden writes: behavior changes to capture/ingest/export.
Acceptance:     **`make check` + `tests/run_pipeline.sh` fully GREEN on the EC2
                box** (mocks, no live orders); `check_registry` green; ONE
                read-only preflight (`/exchange/status` + a 10 s `ws_smoke`) from
                the **EC2 IP** confirming the exchange is reachable + auth valid
                from AWS. No `ws_shadow` firehose yet (that's W-A4).
Rollback:       n/a (tests).
Exit evidence:  the green test tails from the box; the preflight output.

## W-A3 — S3 vault + RESTORE TEST (before cutover)
Purpose:        Get the Mac's accumulated data safely into S3, restore-verified,
                BEFORE the cutover — so a cutover failure can never lose history.
Blocked by:     W-A2; operator has created the S3 bucket + IAM (S4).
Allowed reads:  Mac `work/warehouse/archive`, `work/raw` (retention window),
                `work/warehouse/catalog`.
Allowed writes: a sync script → S3 (**versioned** bucket; `internal/` and
                `product/` prefixes per warehouse_schema); a **RESTORE TEST**
                script; provenance manifest.
Forbidden writes: DELETE anything on the Mac (D1 — Mac stays authoritative until
                W-A4); never `--delete` on the sync.
Acceptance:     archive + raw(3 d) + catalog uploaded (byte/md5 verified against
                manifest.csv); **RESTORE TEST MANDATORY** — pull a sample partition
                back from S3 to a scratch dir and diff it byte-for-byte against the
                Mac original (must be identical). Cost of the vault estimated.
Rollback:       delete the S3 objects (versioned, reversible); Mac untouched.
Exit evidence:  upload manifest + the restore-diff = 0 result.

## W-A4 — zero-gap cutover (operator go/no-go, operator present)
Purpose:        Move live capture Mac→EC2 with ZERO gap and a clean REST handoff.
Blocked by:     W-A3 green + operator go/no-go.

### RULE — SINGLE REST OWNER (operator-locked 2026-07-08)
During the Mac↔EC2 **overlap**, the periodic **REST** tasks (catalog_sync,
dim_snapshot, build_classification, settlements, batch-orderbook cross-check —
anything that spends the shared REST **rate budget** or writes catalog/dim state)
run on **exactly one machine at any instant**. Two machines polling REST at once
= double rate-budget burn + racing catalog writers (a corruption + throttle
hazard). **WS capture concurrency is EXEMPT** — running the WS firehose on BOTH
machines at once is fine and *wanted* for the overlap diff (empirically de-risked
by the **2026-07-06 three-concurrent-connection test**).

**Cutover sequence (no instant where REST has two owners; capture never drops
because WS overlaps throughout):**
1. **EC2 starts WS capture, REST DISABLED** (WS-only; Mac remains the sole REST
   owner). EC2 **seeds its subscription universe from the S3-vaulted catalog**
   (W-A3), issuing **NO REST call** — so the single-REST-owner rule is never
   momentarily violated by a catalog fetch. No REST runs on EC2 until step 4.
2. **Verify EC2 capture health** over a soak — feed FRESH + event counter climbing
   ≥10 min, `ws_seq` continuous, zero gaps — AND confirm the **hourly raw-log
   rotation** fires correctly on EC2 across an hour boundary (the warehouse depends
   on hourly partitions; systemd must reproduce it) — before proceeding.
3. **Mac stops REST** (pause its periodic REST tasks; WS still on both machines).
4. **EC2 starts REST** (now the single REST owner).
5. **Mac `launchctl unload`** (WS + everything) — EC2 is the sole owner of WS+REST.

Rollback: through steps 1–4, WS runs on both boxes, so reversing the last step
loses NO capture. **After step 5** (Mac unloaded), rollback is not instant: it
needs `launchctl load` + reconnect + resubscribe, and the Mac's raw has a HOLE
from unload→reload that only EC2 captured — so a post-step-5 rollback must
**backfill that window from EC2/S3** (which is why W-A5 syncs EC2→S3 promptly, not
only daily, during the fragile early window). Mac launchd stays installed but
dormant throughout. (In-flight **staging DuckDB** on the unloaded Mac is safe by
D1 — it is derived from raw and rebuildable; the two boxes keep separate
raw/staging, so a partial staging is harmless.)

### ACCEPTANCE — BONUS: dual-machine capture-completeness report
While both machines run WS (the overlap window), record BOTH firehoses and **diff
them record-by-record, aligned by `ws_seq`**. Produce the project's first
**"capture completeness empirical report"**: exact cross-machine missing counts
(each direction), the **single-machine miss rate**, attributed by cause where
possible. This miss-rate becomes the **baseline for the `seq_gaps` observability
counter** (W-C5) — the counter is calibrated against ground truth, and the diff
independently proves the cutover was genuinely zero-gap.

Exit evidence:  the cutover timeline; the completeness report (miss counts +
                rate); EC2-only capture running FRESH; Mac dormant.

## W-A5 — post-cutover (steady state on EC2)
Purpose:        Make EC2 the durable, observable, cost-bounded home; land reports
                back to the operator; retire the interim Mac mitigation.
Blocked by:     W-A4.
Allowed reads:  EC2 pipeline outputs; S3.
Allowed writes: **FIRST action — a final incremental Mac→S3 sync** of the archive
                + catalog delta accumulated since the W-A3 vault (the Mac kept
                capturing between W-A3 and W-A4-step-1; that window lives only on
                Mac disk and is NOT yet in S3), re-run the byte/md5 verify on the
                delta — so "the S3 vault has the full history" is TRUE before the
                Mac is ever treated as disposable. Then: a **daily EC2→S3 sync**
                (archive + reports/ prefix) — but **prompt (sub-daily) EC2→S3 sync
                during the fragile early post-cutover window** so a post-step-5
                rollback can backfill; detectors run ON EC2 with alerts to a
                dashboard-readable file (+ operator-chosen email/webhook — **ask**);
                a **storage/cost budget section** (mandatory — S3 + EBS + egress
                monthly estimate + a cap); systemd timers.
Forbidden writes: nothing on the Mac except the report-landing job (below).

**Report flow-back (operator standing requirement, 2026-07-08):** every
daily/acceptance-class detection emits a typed **PDF report** with numbers
(playbook 3c, `tools/daily_report.py`). Post-cutover these generate ON EC2 into
`reports/`, ride the daily EC2→S3 sync under a `reports/` prefix, and a small
launchd job on the Mac runs a daily `aws s3 sync` of that prefix into
**`/Users/ritcardo/Desktop/TradingSys Report/`** — EC2 generates, S3 relays, Mac
lands. Offline Mac ⇒ reports arrive late, never lost. PDFs are derived copies; the
structured artifacts remain authoritative (D1).

**Retire the interim Mac sleep-disable (post-cutover):** once EC2 is authoritative
and all pipelines are verified healthy there, revert the Mac's interim mitigation
— `sudo pmset -a disablesleep 0` (re-enable normal Mac sleep). This is deliberately
a W-A5 step, NOT before (the Mac must stay awake as the dormant rollback host until
cutover is proven). **EXCEPTION — the W-A0 <32 GB branch:** if the daily gold build
was kept on the Mac (RAM<32 GB), the Mac is NOT fully dormant — it wakes daily to
build. In that branch do NOT fully re-enable sleep; instead schedule a daily
wake (`pmset repeat wake`) around the build and leave sleep off during it. The
≥32 GB path (gold on EC2, Mac dormant) avoids this entirely.

Acceptance:     24 h of EC2-only capture with zero gaps (the `capture_gaps`
                detector); daily S3 sync + report landing on the Mac verified once;
                cost budget within the stated cap; Mac sleep re-enabled.
Rollback:       re-activate the dormant Mac launchd (Mac still restore-capable from S3).
Exit evidence:  24 h zero-gap report from EC2; a landed PDF on the Mac Desktop;
                the cost line.

---

## Riders (operator-approved, executed within STEP 1)
- **(a) metrics.ndjson rotation** EXECUTED (512 MB, keep 3) — **on both boxes**
  (the current single unbounded `work/metrics.ndjson` is 17.8 GB; rotate it like
  the raw logs). Ships with an ingest/reader test (D4).
- **(b) promote ALL categories to `class_a_full_l1`** in the EC2 config (full-market
  L1 capture; **Exotics stay Q7-excluded from MM candidacy** — that is a *research*
  filter, not a *storage* filter, so we still capture them).
- **(c) gitignore the two pipeline-churned config CSVs**
  (`config/classification_review.csv`, `config/series_tags_report.csv`) — the
  standing BACKLOG item; stops the perpetual dirty-tree noise.

## §6 self-audit (this plan vs GUARDRAILS)
1. Phase/gates (P1,P2): infra for Phase 1→2; skips no gate; enables the 7-clean-days gate. ✅
2. Live orders (S1–S6): none. Credentials operator-only (S4); cutover operator go/no-go. ✅
3. Log-odds + fees (Q1,Q3): n/a (infra). ✅
4. Pessimistic bound (Q2): unaffected; this PROTECTS it by giving it a holed-free tape. ✅
5. WS trading data (Q5): the WS capture path is preserved and **relocated AND
   EXPANDED** — rider (b) promotes all categories to full-market L1, so W-A0
   sizing must carry the larger subscription universe (noted in W-A0). ✅
6. Tests incl. behavior (E1,D4): W-A2 = full green on the box; riders ship tests. ✅
7. Pipeline continuity (P4): the whole plan is built around zero-gap; WS overlaps
   both boxes; Mac stays dormant-recoverable; S3 restore proven BEFORE cutover. ✅
8. Reversible/bounded (P3,P6): every W reverts (terminate box / delete S3 versions /
   re-enable Mac launchd); no Mac data destroyed until EC2 is authoritative. ✅
9. Docs move with code (E5): deploy/ units + RUNBOOK EC2 section + schema notes in-change. ✅
10. Could green lie (D2): W-A4 completeness diff + W-A5 24 h zero-gap detector make
    a "cutover looked fine" claim falsifiable with real numbers. ✅

## Execution order + operator prerequisites
W-A0 → W-A1 → W-A2 → W-A3 → **W-A4 (operator go/no-go)** → W-A5, one W per fresh
session, independent audit after each. **Only the operator can:** create the AWS
account, launch the instance, create IAM + the S3 bucket, hand-create
`~/.kalshi/env.sh` on the box (S4), and be present for the W-A4 cutover. The agent
does everything else (bring-up scripts, systemd units, validation, sync + restore
scripts, the cutover runbook) — but never touches account or credentials.
