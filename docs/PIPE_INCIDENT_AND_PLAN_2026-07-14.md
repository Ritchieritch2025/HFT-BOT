# PIPE — Production state, live incident, and forward plan (2026-07-14)

STATUS: DRAFT for operator audit. Numbers carry provenance. Written per E2.

---

## RESOLUTION ~21:20Z — 07-13 SEALED + VERIFIED; data plane recovered

**07-13 is SEALED + VERIFY PASS** (seals/date=2026-07-13.json, status=SEALED,
method=full_v2, raw_files=342, archive_files=317; alarm cleared). The all-day
block is over.

**Fixes shipped this session (branch codex/pipeline-recovery-hardening):**
- 2e7e7ce — ticker type guard (isinstance)
- 3bf5635 — e4() output range guard (INT32 price / INT64 qty-delta) + e4p()
- a9de8a5 — recv_ladder nulls out-of-INT64 recv_mono_ns; ws_int range-guards sid/seq
- 22b0d94 — ingest DuckDB memory_limit=32GB (rebuild spills, no OOM) [B5]
- 1c4a9bc — INGEST_SKIP_REBUILD escape hatch (one-shot catch-up on bloated staging)
- ef85dea — verify_raw_caught_up: checkpoint≥last complete newline = caught up;
  partial trailing record / 0-byte closed file are caught up (+ regression fixture)
- e287778 — export_day DuckDB memory_limit=32GB
Each with a focused regression test; full Python warehouse gate green throughout.

**Recovery mechanics:** EBS 200→600 GiB (Gate 1); export_pause froze respawn while
a memory-capped skip-rebuild one-shot checkpointed the RFQ backlog; 2 partial-
trailing raw tails truncated to their last complete record + 1 empty file removed
(the operator ran these, then we replaced the hack with the caught-up LOGIC fix so
future partial records need no raw edits); export_day --force→--seal→--verify-seal;
staging then pruned to 07-14 only (52.6M L1 rows, verified by explicit ts bounds —
07-13 sealed/archived, 07-15 corrupt future-ts removed); export_pause removed; a
fresh daemon (memory-capped, fast rebuild on the small staging) resumed. Capture
(L1/L2/RFQ) never stopped; all raw preserved; zero real data loss.

## REMAINING DEBT (NOT resolved by sealing 07-13 — operator ledger)
- **B14 (root cause, LIVE): ws_shadow stamps garbage recv_mono_ns (~2e19..2e24) on
  L2 frames.** Ingest now nulls it (no crash) but capture keeps producing it —
  CAPTURE-side fix needed, else the bad data keeps arriving.
- **B17: corrupt future-dated ts** (rows landing >= next UTC day within the
  plausibility window). 14.7M such 07-14-batch rows were pruned; SOURCE not yet
  pinned (needs a raw scan — was it exchange ts_ms or a recv clock).
- **B11: seal-chain staging writer-lock race** (07-12 all-day root cause) — unfixed.
- **B15: _rebuild_state is O(full L1 table)** — capped + skip-hatch added, not yet
  incremental; a bloated staging still means a slow startup.
- **B16: staging prune lags when seals lag** (why staging bloated to 12 GB / 195M).
- **07-09** still unsealed (deferred back-seal); **07-12 sealed** but its staging
  rows lingered un-pruned until this session.
- B9 (RFQ parse tax, partial), B5 (research memory — fused off), B8 (key rotation).
**Discipline call: pay B14/B11/B15/B16 before any new load (W06 Stage 2).**

## UPDATE ~19:00Z — full incident arc + where the recovery stands

**What broke (root causes, all confirmed by replaying raw through deployed helpers):**
The 07-13 seal was blocked by malformed-value crashes in the NEW L2/full-depth
ingest path (live since 07-12 W06 Stage-1) — latent weak boundary guards finally
hit by real L2 volume:
1. non-str `market_ticker` (int) → `"-" not in mt` TypeError.
2. (mis-diagnosed first as delta_e4) — actual: garbage capture monotonic clock
   `recv_mono_ns` ~2e19..2e24 (146+ frames per L2 hour, in-play sports/crypto)
   → overflowed the recv_mono_ns BIGINT column at FULL_INSERT/TRADE_INSERT.
   recv_wall_ns is NORMAL → the tradable local clock is unaffected. This is a
   CAPTURE-side defect (ws_shadow stamps a bad mono clock on L2 frames) — the
   ingest-side fix nulls it; capture fix = BACKLOG follow-up.

**Fixes shipped (branch codex/pipeline-recovery-hardening, deployed to box):**
- `2e7e7ce` — ticker type guard (isinstance).
- `3bf5635` — e4() output range guard (INT32 price / INT64 qty-delta) + e4p().
- `a9de8a5` — recv_ladder nulls out-of-INT64 recv_mono_ns; ws_int range-guards
  sid/seq. Now EVERY numeric path into a fixed-width column is bounded (D3).
- Each with a focused regression test (test_ingest.py 5b/5c/5d); full Python
  warehouse gate green. Deployed via `git push ec2` + `git fetch/merge --ff-only`.

**Disk (Gate 1):** EBS vol0bbed61d0c69abb21 grown 200→600 GiB, `growpart`+
`resize2fs` online; root now 581G / **407G free / 30%** (was 22G/89%).

**The remaining bottleneck (operator authorized "排空"/drain):**
staging.duckdb bloated to ~12 GB because seals stuck for days → not
exported/pruned. Composition (measured, read-only): orderbooks_l1 195M rows
(07-12 33.5M **[sealed but un-pruned]**, 07-13 75M, 07-14 75M, 07-15 11M
**[corrupt future-dated ts]**); trades 21.8M; orderbooks_full 62.6M. The daemon's
startup `_rebuild_state` (window query over 195M L1 rows) takes ~24 min and the
hourly seal-chain `stop_ingest` kept killing it before it finished → never caught
up. NOT OOM (60G free), NOT the data bug (fixed) — slow-rebuild + hourly-kill.

**Drain in progress (this session):**
1. Wrote `work/live/export_pause` (operator token) → seal chain paused, supervisor
   won't respawn/kill ingest.
2. Launched a PROTECTED one-shot catch-up `.venv/bin/python3 tools/ingest.py
   --loop` (pid 274690, RNl ~150% CPU, holds staging lock, spilling to .tmp) — it
   can now finish the rebuild + drain the backlog uninterrupted.
3. NEXT (once `--check-caught-up` passes): stop 274690 → export_day 07-13
   `--force --no-prune` → `--seal` → `--verify-seal` → prune 07-12+07-13 from
   staging (shrink it) → `rm export_pause` → supervisor resumes with a small
   staging → fast rebuild → healthy.

**Data-safety invariant held throughout:** capture (L1/L2/RFQ) never stopped, all
raw NDJSON on disk, ingest replays by byte-checkpoint → zero real data loss. The
guards only NULL/reject garbage values, never alter valid data.

**Operator ruling this session:** stop obsessing over perfect historical recovery;
priority = healthy going forward + no missing data. 07-09 = old unsealed day
(since-fixed W03 ingest bug + deferred back-seal), raw preserved, verify+back-seal
deferred. 07-15 corrupt future-dated rows = separate cleanup.

**New BACKLOG items to record:** (B14) ws_shadow stamps garbage recv_mono_ns on L2
frames (capture-side); (B15) `_rebuild_state` is O(full L1 table) → make it
incremental/bounded so a bloated staging can't cause 24-min startups; (B16) staging
prune not keeping up when seals lag (07-12 sealed but un-pruned); (B17) corrupt
future-dated ts (07-15) slipped in within the plausibility window.

---

## UPDATE 13:15Z — EVIDENCE IN: STORAGE_DIAGNOSIS=TRANSIENT_PEAK, disk filling ~5.5 GB/h

Source: `tools/ec2_disk_diagnose.sh` (fixed read-only, run on-box via SSH
`bash -s`, corrected `DB=` path), 2026-07-14T13:10–13:15Z.

- **One volume, genuinely near-full.** Single root FS `/dev/nvme0n1p1` ext4,
  **199 GB partition on a 200 GB EBS disk, 88% used, 25–26 GB free.** raw,
  warehouse, staging.duckdb, WAL, /tmp are ALL on this one mount → NOT
  wrong-mount. Inodes 1% used → NOT inode. No quota. Deleted-open = 26 KB
  (one deleted supervisor.sh held by a bash) → NOT deleted-open.
- **The eater = raw at 127 GB, pruning fully blocked.** `date=2026-07-13` =
  **65.8 GB** (firehose 24.4 + L2 16.0 + **RFQ 25.4**) pinned because unsealed;
  `date=2026-07-09` = 10.9 GB (5-day chronic unsealed); `date=2026-07-14` =
  47 GB and climbing. staging.duckdb = 12.14 GB.
- **MEASURED fill rate:** avail 26,684,510,208 B (13:10:08) → 26,229,661,696 B
  (13:15:08) = **−454,848,512 B / 300 s = −5.46 GB/h.** ⇒ **~4.8 h to true
  ENOSPC (~17:00–18:00Z), then capture/ingest die = data-loss guardrail
  breach.** (This corrects the earlier "probably not imminent" hedge — it IS
  imminent; the two-sample delta proves it.)
- **Verdict TRANSIENT_PEAK is the symptom mechanism:** the seal catch-up commit
  (12 GB db rewrite + WAL + temp) does not fit the shrinking headroom, fails
  with ENOSPC, rolls back, retries → 07-13 never catches up → never seals →
  pruning stays blocked → headroom keeps shrinking. Circular, self-reinforcing.
- **Deployed HEAD** = 0b6518c (branch codex/pipeline-recovery-hardening);
  ingest PID 250884 healthy, cwd /home/ubuntu/hft-bot, python3.12.

**Decisive fix = add disk headroom (grow EBS), zero data risk.** Grow the 200 GB
volume to ~400 GB in the AWS console (EC2 → Volumes → the gp3 on this instance →
Modify → 400 GiB), then on-box (approval-gated): `sudo growpart /dev/nvme0n1 1`
&& `sudo resize2fs /dev/nvme0n1p1` && `df -h /`. Headroom → catch-up commit fits
→ 07-13 seals → pruning reclaims ~66 GB + eventually 07-09 → disk recovers.
Fallback if the console is not reachable in time (reversible, buys ~2× runway
but sacrifices RFQ research data for the paused window): `touch
work/live/rfq_disable` cuts ~45% of the bleed (RFQ ≈ 21 GB of today's 47 GB).
Nothing on disk is safely deletable without sealing first, so pruning is not an
option until a day seals.

---

Every production write below is approval-gated. Written per E2.

---

## 0. One-line ruling

⚠️ **The data plane is UP but a day-seal is stuck again — 07-13 has not sealed
for ~9h because the ingest catch-up commit is hitting "No space left on device"
on the DuckDB write-ahead log, and that same stuck seal is blocking raw
pruning, which keeps disk headroom tight (a self-reinforcing loop).** Capture is
still alive (no data loss yet), but the seal will NOT self-resolve. Root cause is
the capacity debt we already logged (B9/B11) plus a new disk-headroom dimension
(B13). Recommendation: add disk headroom to break the loop today, then pay the
capacity debt BEFORE adding any more market load (freeze W06 Stage 2).

---

## 1. What the authoritative snapshot shows

Source: `ec2_monitor.sh` (fixed read-only script, operator-authorized),
run 2026-07-14T12:08:13Z.

| Signal | Value | Read |
|---|---|---|
| Seals present | 07-10, 07-11, 07-12 | 07-13 **missing** |
| Seal alarm | 07-13 `UNSEALED_PAST_ALARM_LINE`, first 03:00Z, latest 12:00Z, 10× | seal ~9h overdue (T+1.5 would be ~03:30Z) |
| Ingest daemons | 1 | alive |
| Catch-up | `FAIL 2026-07-13: behind raw: 31 file(s)` | daemon cannot catch up |
| Ingest log tail | `_duckdb.TransactionException: Failed to commit: Could not write file ".../staging.duckdb.wal": No space left on device` | **commit is failing on disk** |
| staging.duckdb | 12,141,211,648 B (~12.1 GB), mtime 12:07:10Z | large; being rewritten 1 min before snapshot |
| Raw newest | `date=2026-07-14/l2_12.ndjson` | capture ALIVE (writing hour 12) |
| Disk | `DISK_USED=86% AVAIL=28G` on /home/ubuntu | nominally 28G free — see paradox below |
| Memory | `MEM_AVAIL_MB=57739`, swap 168 MB | **RAM is fine — this is NOT a memory incident** (contrast B5) |
| Retention | checked 12:00Z, `deleted: 0`; retained_overdue = 07-09 firehose hrs18-23 (`day_unsealed`), 07-10 hrs00-01 (`cross_day_prev_unsealed`), 07-12 hr23 (`unsealed_dependency`) | pruning is BLOCKED by the unsealed-day chain |

## 2. Diagnosis (what is actually happening)

**The paradox:** WAL commit fails with "No space left on device" while `df`
reports 28G free. Two candidate explanations, both plausible; the deeper read to
disambiguate was correctly blocked by the permission boundary and needs operator
authorization:

1. **Transient-space blowout (most likely).** Committing a 31-file backlog forces
   DuckDB to grow the WAL and/or checkpoint-rewrite the 12 GB `staging.duckdb`
   plus temp spill. Peak transient footprint (12 GB db rewrite + WAL + temp) can
   momentarily exceed the 28 G free; the commit fails, rolls back, frees the
   space, and `df` reads 28G again a moment later. The daemon then retries the
   same oversized commit → **doom loop**.
2. **Tighter/other mount or inodes.** `staging.duckdb` could sit on a smaller
   volume than the root `df` measured, or inodes could be exhausted. Unconfirmed.

**The self-reinforcing loop:**
```
catch-up commit needs > free space  →  ENOSPC on WAL  →  rollback/retry
        ↑                                                      │
        │                                                      ▼
 headroom stays tight  ←  pruning blocked  ←  07-13 cannot seal (not caught up)
```
Pruning is blocked because a day's raw can only be pruned once the day (and its
cross-day dependency) is sealed; 07-13 won't seal, so 07-12's tail and the
chronic **07-09 (unsealed 5 days)** + 07-10 cross-day files stay pinned.

**Chronic anchor:** 07-09 has been unsealed since it happened (logged as "07-09
back-seal = open operator decision"). It is no longer harmless — it permanently
pins the oldest raw and blocks 07-10's cross-day pruning.

**This is the same failure family as the 07-12 all-day block** (B11 seal-chain
lock-race), now compounded by disk headroom. The RFQ fast-path (B9, deployed
5c6d7b9 + eb89e96c) helped, but it did not remove the seal-chain lock-race
(B11, unfixed) or add disk headroom (B13, new). So the incident recurs.

## 3. Ongoing issues — the strategically live set

(Filtered from `docs/BACKLOG.md`; the ~60 historical W2.x notes are omitted.)

| ID | Issue | Severity | Owner/fix |
|---|---|---|---|
| **B13 (new)** | Disk-headroom / transient commit-space under backlog; ENOSPC on WAL blocks seal | **URGENT** | add EBS headroom + free-space preflight + bound staging.duckdb growth |
| **B11** | Seal chain loses staging writer-lock race to ingest at `--force` export (root cause of 07-12) | **HIGH** | seal chain hard-holds ingest stopped for whole export+seal (recover_rfq_seal clauses 16-18) |
| **07-09 unsealed** | 5 days; pins oldest raw, blocks 07-10 cross-day prune | **HIGH** | operator decision + controlled seal |
| **B12** | W05 publish IAM gap: vaultWriter lacks `s3:GetObjectVersion` on research/* → 07-12+ release fail-closes | MED | operator IAM add; then finalize publish + clean partial upload |
| **B9** | RFQ parse tax (fast-path deployed = partial relief) | MED | confirm effectiveness; parallel-ingest by family if still >1.2× capture |
| **B5** | Research memory ceiling (HOTFIX-02 fused prod research off) | MED | DuckDB memory_limit + cgroup fence before re-enable |
| **B10** | `test_l2_targets` fail-closed refresh test fails on clean base | LOW | triage in next pipeline W |
| **B8** | researchReader key rotation (value entered transcript) | LOW | rotate at next console session |
| **B7** | Visualize-Everything doctrine retrofit of report generators | process | next report-generator W |
| **B6** | 4 W03 review P2s (incl. verify EC2 bash≥4) | LOW | Stage-1 deploy checklist |
| capture 硬伤 | export pauses capture ~8 min at 02:00Z (synchronous export in capture loop) | Phase-2 | decouple export from capture loop / dual-socket execution feed |

## 4. Forward plan (operator audits, then authorizes each gated step)

### Phase 0 — STABILIZE today (break the loop; zero-data-risk first)
0.1 **Authorize the deeper read-only diagnostic** (df of ALL mounts, `du` of
    raw-by-day + warehouse, inode count) so we confirm transient-blowout vs
    tight-mount vs inodes. Cheapest way: add `ec2_monitor.sh` a sibling
    read-only `ec2_disk.sh` (fixed command set) to the allow-list, OR run the
    one-shot under an approval prompt. Read-only, no writes.
0.2 **Add disk headroom — RECOMMENDED lever, no data risk.** Grow the EBS data
    volume (operator: `aws ec2 modify-volume`, then on-box `growpart` +
    `resize2fs` under per-action approval — online, no downtime). This
    immediately lets the 31-file commit land. Operator call: costs a few $/mo,
    needs AWS creds.
0.3 **Let 07-13 seal** (chain lands once commit succeeds) or approve a controlled
    seal; verify via `seals/date=2026-07-13.json` + verify-seal PASS (the ONLY
    authoritative signal — see lesson below), clear the alarm.
0.4 **Seal 07-09** (decision + controlled execution) so retention prunes its
    held raw + 07-10 cross-day. Removes the chronic anchor for good.

### Phase 1 — PAY THE CAPACITY DEBT (before W06 Stage 2 raises L2 load)
1.1 **B11 fix (highest leverage):** seal chain hard-holds ingest stopped for the
    ENTIRE export+seal (own-pause token + verified process stop, mirroring the
    approved `recover_rfq_seal` clauses 16-18). Removes the lock-race doom loop
    at its root; new behavior gets a contract test in the same change.
1.2 **B13 guardrail:** (a) pre-seal free-space preflight + alert; (b) periodic
    `CHECKPOINT`/vacuum to bound `staging.duckdb`; (c) size EBS for worst-case
    backlog transient (≈2–3× the db size); (d) retention-under-pressure: allow
    pruning a SEALED day's archived raw even when a LATER day is still unsealed,
    where the cross-day dependency permits.
1.3 **B9 residual:** confirm RFQ fast-path throughput; if ingest still runs
    >~1.2× capture generation, add parallel ingest by file family.

### Phase 2 — UNBLOCK the research bridge (W05)
2.1 **B12:** operator adds `s3:GetObjectVersion` (+`s3:GetObject`) on `research/*`
    to vaultWriter IAM. Then finalize the 07-12+ version-bound publish + Mac T+1
    verify; clean up the partial 07-12 upload (no MANIFEST = not exposed).

### Phase 3 — hygiene / lower priority
B10 test triage · B8 key rotation · B7 Visualize-Everything retrofit ·
B6 W03 P2s · capture-continuity export-decouple (Phase-2 execution feed).

## 5. Where we are in the queue (honest position)

- **Active front = pipeline data plane hardening.** W06 Stage 1 (targeted L2)
  LIVE; **W06 Stage 2 (sizing) is GATED** behind B9/B11 — and now B13. W07 RFQ
  capture LIVE. W05 research bridge partially live (07-11 published; 07-12+
  blocked on B12 IAM).
- **Strategy program has NOT started.** STP-P00 remains BLOCKED (W01 needs its
  own release + an independently-audited §31.2 test-isolation artifact). Mainline
  strategy = 赛前市场动力学价差捕获 (D-1), but no phase is authorized. We are
  building the data plane that a strategy will later consume; we are not yet in
  trading research.

## 6. Senior-engineer call (the uncomfortable part)

We keep having day-seal incidents (07-10, 07-12, now 07-13) because we added
load — L2 + RFQ capture pushed generation ~30 → ~80 GB/day (B9) — **without first
paying the capacity debt** (B9 parse tax, B11 lock-race), and now disk headroom
(B13) has joined memory (B5) as a binding constraint. The disciplined sequence is:
**freeze new load (do NOT start W06 Stage 2), stabilize disk, fix B11 + B13, THEN
resume expansion.** Adding more markets now deepens the hole. This is the same
"pay-down-before-expand" logic that correctly refused the pre-migration refactor.

## 7. Standing lesson (do not regress)

The seal chain is asynchronous. Only `seals/date=<D>.json` (status=SEALED,
method=full_v2) + `verify-seal` PASS is authoritative. Intermediate signals
(log lines, `export_pause` windows, "no ingest daemon" mid-chain, behind-count)
are NOT success/failure signals — reporting off them caused three premature
07-12 status flips. Report only from the seal file.
