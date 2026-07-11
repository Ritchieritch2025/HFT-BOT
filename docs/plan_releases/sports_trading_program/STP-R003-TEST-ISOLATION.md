# STP-R003-TEST-ISOLATION — 操作员释放令收据

- received: 2026-07-11(操作员会话消息,issued_at_utc 2026-07-11T17:19:24Z)
- 前置校验(receipt commit 前,全部只读实测):
  - active_prompt_path SHA-256 = `575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54` ✅(shasum 实测,逐字节相符)
  - branch = `plan-sports-market-dynamics-v2` ✅ · HEAD = `17ee487b1527f475e721c2bc137dd13ae23cf093` = base_commit ✅(精确相等)
  - worktree = `/Users/ritcardo/HFT BOT` ✅;未跟踪项仅先前已存在的 `outputs/` ✅
- operator_text_sha256(下方 VERBATIM 块内容,含末行换行):
  `fec0ec9919d6c94abfbabfb2ddac95a7fe6c2a5f7499803aca0b4d62092d00ce`
- status: `CONSUMED — BOTH SESSIONS COMPLETE, AUD01 VERDICT = PASS`
  (W01 实现 commits `0dbeb75`+`770dd0f`,IMPLEMENTED_AWAITING_AUDIT 后停;
  AUD01 独立审计 commit `3ed13e2`,verdict **PASS**,零 P0;§31.2 前置工件
  自此成立。artifact sha `66549d28…9b5b1` · manifest sha `3112972a…f9` ·
  audit sha `ef58bb36…4098`。本 PASS 不授权 STP-P00/BOOTSTRAP-0/任何
  phase——下一步各需单独操作员 release。)
- session plan: Session 1(STP-P00-ISO-W01 实现)与 Session 2(STP-P00-ISO-AUD01
  独立审计)各由**零上下文独立代理**承担(审计代理对实现代理不可见,满足
  "fresh agent that did not implement" 要求);编排会话本身只做收据、转录与
  最终汇总,不做工程实现、不做审计。

## 操作员释放令原文(VERBATIM)

```
OPERATOR RELEASE — STP-R003-TEST-ISOLATION
issued_at_utc: 2026-07-11T17:19:24Z

OBJECTIVE

Create and independently audit the Section 31.2 test-isolation artifact required before STP-P00 may begin.

This release authorizes exactly one engineering W and one separate independent-audit session. It does not authorize STP-P00 or any strategy phase.

AUTHORITY

- active_prompt_path:
  docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md
- active_prompt_sha256:
  575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54
- base_commit:
  17ee487b1527f475e721c2bc137dd13ae23cf093
- authorized_branch:
  plan-sports-market-dynamics-v2
- authorized_worktree:
  /Users/ritcardo/HFT BOT
- isolated_test_root_prefix:
  /private/tmp/stp-p00-test-isolation
- authorized_STP_phase_ids: []
- authorized_W_ids:
  - STP-P00-ISO-W01
  - STP-P00-ISO-AUD01
- session_count: 2
  - Session 1: implementation
  - Session 2: fresh independent audit

FIRST ACTION — RELEASE RECEIPT

Before any engineering work:

1. Verify the active prompt path and SHA-256.
2. Verify branch, base commit and worktree.
3. Archive this operator release verbatim.
4. Compute operator_text_sha256.
5. Commit the receipt at:

docs/plan_releases/sports_trading_program/STP-R003-TEST-ISOLATION.md

No engineering work may begin before the receipt commit exists.

EXACT ALLOWED REPOSITORY WRITES

- tests/**
- Makefile
- docs/plan_releases/sports_trading_program/STP-R003-TEST-ISOLATION.md
- docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_ARTIFACT.md
- docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_MANIFEST.json
- docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_INDEPENDENT_AUDIT.md
- docs/SESSION_LOG.md

Allowed derived writes, never staged or committed:

- build/**
- /private/tmp/stp-p00-test-isolation*/**
- the existing documentation mirror required by the repository exit ritual

Do not touch the pre-existing untracked outputs/ directory.

If implementation requires any repository write outside this exact list, stop and report the precise blocker plus the smallest proposed release amendment. Do not silently expand scope.

IMPLEMENTATION FREEDOM

The objective is isolation, not one predetermined design.

The implementation may use:

- a fresh isolated temporary worktree;
- an isolated repository copy;
- environment-variable root redirection;
- test-owned OS temporary directories;
- or a minimal combination of these.

Choose the smallest reliable mechanism that proves the operational worktree is unchanged.

IMPLEMENTATION REQUIREMENTS

1. Make this complete test sequence runnable from a fresh isolated root:

   make all
   make check
   tests/run_pipeline.sh
   python3 tools/check_registry.py --require-built

2. External networking is forbidden, but test-owned loopback mocks are explicitly allowed:

   - allowed: 127.0.0.1, localhost and ::1;
   - allowed: ephemeral local ports and test mock servers;
   - forbidden: DNS resolution for external services;
   - forbidden: Kalshi, AWS or any other external endpoint.

3. Remove or isolate all production authority from the test environment:

   - no KALSHI production credentials;
   - no AWS credentials;
   - no SSH;
   - no EC2 access;
   - no production database connection;
   - no production API base URL;
   - no live-order capability.

4. Tests must not write operational state in the authorized worktree, including:

   - work/lifecycle_status.json
   - work/lifecycle_events.ndjson
   - work/test_results_latest.json
   - work/test_results.ndjson
   - work/logs/**
   - work/live/alerts.log
   - any production/dashboard state
   - any .pytest_cache outside the isolated test root

5. Existing tests may not be deleted, weakened, skipped, reclassified or reduced. Changes under tests/** may only:

   - implement path/state isolation;
   - support loopback-only mocks;
   - add fail-closed isolation tests;
   - preserve or strengthen existing assertions.

6. Snapshot before each run:

   - complete path/content manifest of operational work/**
   - repository .pytest_cache state
   - git status with all untracked paths
   - hashes of specifically forbidden operational files
   - relevant environment variables with secrets redacted

7. Run the complete test sequence twice, using two fresh isolated roots.

8. After each run, prove:

   - all four commands passed;
   - no required suite was skipped;
   - operational-state paths and hashes are unchanged;
   - git status matches the baseline except authorized source/document edits and pre-existing outputs/;
   - no derived output was staged or committed;
   - no external connection or credential was used.

9. Add a fail-closed negative control proving that the harness rejects:

   - a missing isolation root;
   - an isolation root resolving to the operational worktree;
   - a production work/** path;
   - inherited production credentials or production API URLs.

10. The artifact and JSON manifest must record:

   - implementation commit;
   - exact commands;
   - isolated root strategy;
   - environment allowlist and denied variables;
   - test counts and results for both runs;
   - before/after operational manifests and hashes;
   - negative-control results;
   - residual limitations;
   - artifact SHA-256.

IMPLEMENTATION LIFECYCLE

The implementation session must:

1. Commit only authorized implementation and artifact files.
2. Complete the repository exit ritual.
3. Update SESSION_LOG.
4. Sync the permitted documentation mirror.
5. End with status:

IMPLEMENTED_AWAITING_AUDIT

6. Stop. The implementation agent must not audit its own work.

INDEPENDENT AUDIT

A fresh agent that did not implement STP-P00-ISO-W01 must perform STP-P00-ISO-AUD01.

The auditor must:

1. Verify the release receipt, prompt SHA, branch, base and implementation commits.
2. Inspect the complete diff.
3. Confirm no existing test was weakened, removed, skipped or reclassified.
4. Verify production defaults remain unchanged.
5. Rerun the complete four-command sequence in a third fresh isolated audit root.
6. Reproduce zero operational-state change.
7. Reproduce the fail-closed negative controls.
8. Confirm only loopback mock traffic occurred.
9. Return exactly one verdict:

   PASS
   REVISE
   REJECT

The audit session may write only:

- docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_INDEPENDENT_AUDIT.md
- docs/SESSION_LOG.md
- permitted temporary/build outputs
- the existing documentation mirror required by the exit ritual

The audit report must include:

- artifact SHA-256;
- audit SHA-256;
- implementation and audit commits;
- reproduced test results;
- reproduced state-diff result;
- any P0/P1/P2 findings.

A PASS establishes the Section 31.2 prerequisite artifact only. It does not authorize STP-P00.

PERMISSIONS

- allowed tool safety classes:
  - pure
  - offline
- allowed network actions:
  - loopback-only test mocks
- external network: false
- credential_use: false
- external_account_actions: false
- paid_api_access: false
- spending_cap: 0
- live_order_permission: false
- production_mutation: false
- push_permission: false
- merge_permission: false
- superseded_releases: none
- status: ACTIVE until both authorized sessions are consumed

EXPLICITLY UNAUTHORIZED

- BOOTSTRAP-0
- STP-P00-W01
- STP-P00-AUD01
- STP-P01 or any later phase
- D-2 changes
- GUARDRAILS changes
- canonical prompt changes
- production deployment
- EC2/systemd operations
- real API access
- live or paper orders
- push
- merge

After the independent audit, report all paths, hashes, commits and the final verdict, then stop.
```
