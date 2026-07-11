# PIPE-R001-RECOVERY-HARDENING — operator release receipt

Archival metadata (not part of the verbatim operator text):

- issued_at_utc: 2026-07-11 07:11:17 UTC
- operator_text_sha256: 5b4f69a4dad87f5cc09010b55798bbd74ab373e7fac27558a164d5cb1adda4e6
- production_base_commit: 32ab4f6556804ebebb57b1a5d215195b7874bba3
- authorized_branch: codex/pipeline-recovery-hardening
- authorized_worktree: /Users/ritcardo/HFT-BOT-pipeline-recovery
- objective: execute every recovery and hardening action in the immediately
  preceding pipeline diagnosis while preserving uninterrupted raw capture
- authorized work packages:
  - PIPE-W01: terminate only the blocking background research process; recover
    ingest; prove raw capture continuity and backlog catch-up
  - PIPE-W02: make completed-day research archive-only; make initial ingest
    connection retry; prevent stale export-log success from masking failure
  - PIPE-W03: recorder fail-closed and observable write errors; data-only
    watchdog; channel/sid/sequence/loss-marker gap evidence
  - PIPE-W04: decouple capture from export/research; gate raw pruning on
    durable-vault evidence
  - PIPE-W05: controlled EC2/S3-to-research ingress and historical rebuild
  - PIPE-W06: targeted L2/lifecycle collection with bounded rollout
- allowed repository writes: source, tests, config, deploy units/scripts,
  registry entries, runbook/schema/plan/audit/release/session documentation
  necessary for the authorized work packages
- allowed production actions: deploy only tested and independently audited
  commits; terminate/restart background research or ingest processes; inspect
  and verify systemd/data state; push the authorized branch to the `ec2`
  remote; update the EC2 checkout non-destructively
- capture continuity rule: do not restart or stop the active WebSocket capture
  for PIPE-W01/W02; later capture-affecting deployment requires an explicit
  zero-gap rollout/rollback procedure and evidence before cutover
- raw/archive rule: no deletion, rewrite or force-export is authorized by this
  release; raw remains the source of truth
- test rule: all tests run in this isolated worktree with test-owned state
  redirected away from production/dashboard files; new behavior requires
  regression tests and independent audit before deployment
- live_order_permission: false
- account/credential mutation: false
- paid API/external account actions: false
- Kalshi order/account tools: prohibited
- GitHub origin push: not required and not authorized by this release
- EC2 branch push/deployment: authorized only for the above pipeline work
- status: ACTIVE

---BEGIN OPERATOR TEXT VERBATIM---
全部执行
---END OPERATOR TEXT VERBATIM---

## Interpreted scope evidence

The operator's instruction responds directly to the preceding diagnosis and
its numbered execution recommendation: preserve capture, recover ingest, fix
the live-staging lock, prove catch-up, then implement recorder/gap/export
isolation and the controlled research/L2 data lanes. It does not authorize
orders, account changes, paid services, raw deletion or an unbounded production
restart.

## Execution ledger

- PIPE-W01 recovery action (2026-07-11 07:08 UTC): terminated only the proven
  lock holder `mm_calibrate` PID 171869. Raw WebSocket capture was not stopped
  or restarted. The ingest watchdog started PID 174343; capture stayed current
  with zero observed reconnect/error/overflow/drop counters. Catch-up proof is
  still pending the first ingest cycle boundary; do not call W01 complete until
  staging freshness is measured below 600 seconds.
- PIPE-W02 implementation is isolated in the authorized worktree. Its source
  boundary is now: completed-day raw bytes fully checkpointed → exact two-way
  staging/archive content equality → atomic dated seal → archive-only research.
  An unsealed or stale-manifest day fails closed, including partial all-market
  archives. Ingest retries only recognized lock failures; other failures retain
  their original exception immediately.
- The first PIPE-W02 candidate was independently BLOCKED. Confirmed defects
  included incomplete-day sealing, partial-universe undercount, count-only
  verification, provisional research races, stale supervisor state, and a
  prior live staging attachment surviving the archive-only call. Those defects
  drove the seal/catch-up/content-parity redesign and regression tests.
- Deployment split: Python fail-closed components may be activated without a
  capture restart only after the revised independent audit passes. That bridge
  excludes `tools/export_day.py` and `tools/pipeline_supervisor.sh`: the running
  legacy supervisor would otherwise force-rewrite/unseal without resealing.
  Under the bridge, old daily research commands intentionally fail closed on
  unsealed dates and therefore cannot reacquire the staging reader lock. The changed
  monolithic supervisor is **NOT deployable under W01/W02** because activating
  it requires a capture restart and its batch work still precedes the capture
  segment. It remains a tested transition artifact for PIPE-W04; production
  activation waits for process decoupling and a zero-gap cutover release.
- Assurance boundary: the W02 seal proves raw file bytes were checkpointed and
  staging equals archive. It does not yet prove every raw JSON line was valid,
  nor losslessness of conflated ticker/trade channels; PIPE-W03 supplies those
  per-channel/session/sequence/loss-marker integrity artifacts.
- `Ingester.process_file` now commits fact rows, ingest stats and the file byte
  checkpoint in one DuckDB transaction. The former autocommit crash window
  could replay already-written facts after restart; an injected checkpoint
  failure regression proves the fact rows roll back and replay exactly once.
- Historical source policy is fail-closed by default: every fully bounded past
  `warehouse.load()` window automatically requires valid seals. This covers
  gold/event research callers as well as maker tools. Pre-W02 archives remain
  LEGACY-UNSEALED and unavailable to unattended historical research until a
  controlled rebuild can reproduce raw→staging→archive evidence.
- A day seal means `ARCHIVE-SEALED / CAPTURE-UNASSESSED`, never “FINAL” in the
  profitability sense. `gate_calc` and `mm_research` label that distinction;
  no profitability/live gate may consume it before PIPE-W03 capture-quality
  evidence exists.
- Candidate supervisor raw mtime deletion is disabled. PIPE-W04 must replace it
  with per-file S3 checksum/version receipts before any local raw prune. Until
  then, a seal requires its exact local raw inventory; portable restores are
  accepted when sizes and SHA-256 match even if inode/timestamps differ.
