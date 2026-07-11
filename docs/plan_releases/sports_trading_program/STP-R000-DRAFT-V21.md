# STP-R000-DRAFT-V21 — Durable operator release receipt

Archival metadata (not part of the verbatim operator text):

- release_id: STP-R000-DRAFT-V21
- issued_at_utc: 2026-07-11 (operator message; archived same session)
- operator_text_sha256 (bytes between the BEGIN/END markers below, exclusive,
  UTF-8, no trailing newline): d1d8d1d271f024146012761a566c5cfbd7e49eafdf2d9716e1c3fbd5176915f6
- base_commit: 1c93837a4422fe54717224d3ef9fe16bac0d1018
- authorized_branch: plan-sports-market-dynamics-v2
- authorized scope: THIS RELEASE ONLY — candidate revision + archival + tests +
  SESSION_LOG + mirror. NOT BOOTSTRAP-0, NOT STP-P00, NOT STP-P01, no D-2,
  no merge, no push, no GUARDRAILS/code/config/production/outputs changes.
- live_order_permission=false; production_mutation=false; spending_cap=0;
  external_account_actions=false; paid_api_access=false; credential_use=false;
  push_permission=false
- status: CONSUMED
- consumed_by_evidence_commit: 594603e381f372abf415d60aa42089cc60755448
- consumed_by_closure_commit: 9952befd73f79195f2d38e2c45110601d8edbbea

---BEGIN OPERATOR TEXT VERBATIM---
操作员指令 STP-R000-DRAFT-V21，单场只修订候选，不执行研究：
先完整读取 CLAUDE.md、docs/GUARDRAILS.md、docs/PLAN_SPORTS_TRADING_DECISIONS.md、docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2.md 和 docs/plan_audits/AUDIT_PROMPT_V2_2026-07-10.md。
已批准从 commit 1c93837a4422fe54717224d3ef9fe16bac0d1018 创建/使用分支 plan-sports-market-dynamics-v2。不得 merge/rebase/push，不得修改 main。若同名分支已存在，先核验来源，禁止 force。
本 release 仅授权：
将本指令逐字保存为 docs/plan_releases/sports_trading_program/STP-R000-DRAFT-V21.md；
将当前 V2 原文以 SHA-256 bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341 保存为历史候选，不得静默改写；
保留审计报告 docs/plan_audits/AUDIT_PROMPT_V2_2026-07-10.md，其 SHA-256 应为 53ec7573f675006e60d82cc299f2933f35d534464aa4913f3785f1fda7f8c83f；
根据审计报告的全部 P0/P1 修订，创建新文件 docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_1_CANDIDATE.md；
V2.1 文件头必须标明 CANDIDATE — NOT CANONICAL — NOT EXECUTABLE UNTIL INDEPENDENT AUDIT PASS；
完整采纳审计给出的最小替换语言，不得削弱其中列出的“已经很强、不能削弱”部分；
更新 docs/SESSION_LOG.md，按 CLAUDE.md 完成必要测试与镜像；测试产生的 build/**、work/logs/**、work/test_results.ndjson 只能作为派生产物，禁止提交；
明确记录当前 144 只是 observed registry count，P00 将双向扫描所有 runnable、main()、Make/CMake target、脚本入口和 live-capable argument modes；
不修改 GUARDRAILS、代码、测试、配置、生产管道或 outputs/；
不执行 BOOTSTRAP-0、STP-P00、STP-P01，不写 D-2，不合并 main，不推送。
只 stage 明确授权的文档，检查 staged diff 后提交 candidate/evidence 与独立 closure commit。最终状态必须是 V2.1 IMPLEMENTED_AWAITING_INDEPENDENT_AUDIT，随后停止并报告文件路径、SHA、commit hash、测试和 Git status。
---END OPERATOR TEXT VERBATIM---
