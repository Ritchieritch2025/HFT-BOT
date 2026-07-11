# AUDIT — CANONICAL PROMPT V2.1 CANDIDATE

- Date: 2026-07-11
- Audit mode: fresh independent, strictly read-only
- Target: `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_1_CANDIDATE.md`
- Target SHA-256: `e83160bd289dc96fe8e05b6ffdb9ade047c921354733cbcaa210928c7a60106c`
- Evidence commit: `594603e381f372abf415d60aa42089cc60755448`
- Closure commit: `9952befd73f79195f2d38e2c45110601d8edbbea`
- Verdict: `PASS WITH REQUIRED REVISIONS`
- Execution performed: none
- Repository mutation during audit: none; this file preserves the returned audit

---
PASS WITH REQUIRED REVISIONS

当前 SHA `e83160bd289dc96fe8e05b6ffdb9ade047c921354733cbcaa210928c7a60106c` 不得 canonicalize，也不得执行 BOOTSTRAP-0、STP-P00 或 D-2。由于该候选已经提交并声明 immutable，必须保留为历史候选，创建新路径、新 SHA 的 V2.2 candidate，再做一次独立只读审计。

## Artifact state

只读核验通过：

- branch：`plan-sports-market-dynamics-v2`
- HEAD：`9952befd73f79195f2d38e2c45110601d8edbbea`
- 线性提交链：`1c93837 → 594603e → 9952bef`
- `main` 未动，仍为 `92918bb42d49626ecc767359d216240c34f1c6dc`
- evidence commit `594603e` 只新增冻结 V2、V2.1 candidate、旧审计和 release receipt
- closure commit `9952bef` 只修改 `docs/SESSION_LOG.md`
- tracked/staged tree 干净；只有先前已存在的 `?? outputs/`，旧审计也记录了该状态（`AUDIT_PROMPT_V2_2026-07-10.md:14`）
- V2.1 SHA：`e83160bd289dc96fe8e05b6ffdb9ade047c921354733cbcaa210928c7a60106c`
- frozen V2 SHA：`bc2fbf6562c19fabbb7ebd7e8c77f88a720d1b6a2a467b39fbffce996bb1f341`
- prior audit SHA：`53ec7573f675006e60d82cc299f2933f35d534464aa4913f3785f1fda7f8c83f`
- release receipt 文件 SHA：`617023e4379b89e8e9001288e48f0cf22b2afba677b56b95c1cac4cb24368f67`
- receipt 中 operator text SHA：`d1d8d1d271f024146012761a566c5cfbd7e49eafdf2d9716e1c3fbd5176915f6`，与其第 7–8 行定义一致
- Desktop mirror 中 candidate、V2、audit、receipt 和 `CLAUDE.md` 均与仓库 SHA 一致
- 未重跑测试，以保持严格只读。现有 `work/test_results.ndjson` 有 65 条且全部 pass；其中 63 个是 pipeline 顶层 suites，另外 2 条由 console 测试内部运行产生

但 receipt 当前状态有缺陷：`STP-R000-DRAFT-V21.md:17` 写成 `ACTIVE (consumed by this session...)`。该单场 release 已产生 evidence + closure commits，应为 `CONSUMED`。

## 旧审计十项 remediation 核验

- P0-1：核心 release receipt 和 AUD01 start-condition 语言已写入 `V2.1:475-486, 2962-2965, 3086-3088`，但 candidate promotion 和 release-request 仍未闭环。
- P0-2：替换块已写入 `V2.1:2815-2822`，但真实测试副作用和 AUD01 本地 allowlist 仍冲突。
- P0-3：PASS。checkpoint 正确排除 active prompt，见 `V2.1:405-410`；archive/checkpoint 分 commit，见 `V2.1:3021`。
- P0-4：未闭环。主动态 census 语言正确，但 matrix/AUD01 又缩回 registered/144。
- P1-1：D-2、Track D 和禁止夹带 merge 的主体语言均已加入，见 `V2.1:303-308, 400-401, 916-922`；但 D-2 仍引用 frozen V2，而非最终 active prompt。
- P1-2：DECISIONS purity 主体正确，见 `V2.1:310-315, 2801-2803, 2973-2975`；rider 规则仍有歧义。
- P1-3：PASS，完整财富恒等式见 `V2.1:1346-1357`。
- P1-4：PASS，四分裂与 prior-exposure 强制归类见 `V2.1:1443-1450`。
- P1-5：PASS，多状态撤单和校准代表性门见 `V2.1:1263-1269, 1750-1755`。
- P1-6：已加入全部源码证实限制，见 `V2.1:2155-2163`，但 `HISTORICAL / DIAGNOSTIC_ONLY` 与 exact-one primary classification 冲突。

## P0 — canonicalization blockers

### P0-1：candidate promotion gate 自相矛盾

文件头在 `V2.1:4-5` 绝对禁止审计 PASS 前执行；正文却在：

- `V2.1:524-526` 声称 BOOTSTRAP-0 当前可执行；
- `V2.1:2753-2757` 声称 P00 原则上已授权；
- `V2.1:3071-3089` 给出立即执行 BOOTSTRAP/W01/AUD01 的指令。

而 `V2.1:436-443` 又正确表明 prompt audit PASS 后仍需 operator canonicalization release。当前 prompt-candidate audit 和未来 `STP-P00-AUD01` 也没有被明确区分。

最小替换：

> **Candidate promotion gate.** At creation this artifact is `CANDIDATE / NOT EXECUTABLE`. Only a fresh independent read-only prompt audit may occur. A prompt-audit PASS is necessary but not sufficient: a later verbatim operator canonicalization release must adopt this exact audited path and SHA as `active_prompt_path` and `active_prompt_sha256`. Until both conditions are satisfied, stop before BOOTSTRAP-0. Audit PASS alone grants no BOOTSTRAP-0, W01 or AUD01 authority. Any byte or path change creates a new candidate requiring independent audit. The prompt-candidate audit is distinct from `STP-P00-AUD01`. Even after canonicalization, execution requires a separate W-specific durable release.

在 §36 开头加入：

> If the exact prompt-audit PASS and operator canonicalization release are absent, report the candidate path and SHA and stop before BOOTSTRAP-0.

### P0-2：frozen V2 仍被定义为 active、writable prompt

`V2.1:436-443` 已声明原 V2 是 frozen historical candidate，永不覆盖；但：

- `V2.1:532-535` 仍把旧 V2 路径定义为 immutable active prompt；
- `V2.1:2797-2803` 仍把旧 V2 列为 W01 exact allowed write。

最小替换：

> - immutable active prompt: the exact `active_prompt_path` and `active_prompt_sha256` named by the current operator canonicalization release;

W01 allowlist 对应项替换为：

> - the release-named `active_prompt_path`, solely for immutable prompt archival; every other frozen historical prompt path is read-only and never writable.

### P0-3：P0-2 derived-write allowlist 不符合真实测试行为

候选只允许 `build/**`、`work/logs/**`、`work/test_results.ndjson`、OS temp 和 mirror（`V2.1:2815-2822`）。实际 required pipeline 同时写入：

- `.pytest_cache/**`：`tests/run_pytest.sh:7-8`
- `work/lifecycle_status.json`、`work/lifecycle_events.ndjson`：`tests/test_console.py:265-273`，写入定义见 `tools/lifecycle_check.py:4-7,24-25`
- `work/test_results_latest.json`：`tools/run_tests.py:4-7,25-28`
- `work/live/alerts.log`：`tests/test_reconcile.py:204-229`，固定路径见 `tools/reconcile.py:67,238-250`

这些文件的 mtime 均落在 V2.1 实现测试窗口内。尤其 lifecycle 和 alert 文件属于 dashboard/ops 状态，不能简单视作无害缓存；候选同时禁止 production/service-state writes（`V2.1:2827-2842`）。

此外，`V2.1:2967-2971` 又以更局部的绝对语言把 AUD01 writes 限制为三个 docs 文件，未引用 `V2.1:2815-2822` 的 derived-write 例外。

最小替换：

> **Test isolation prerequisite.** Required W01 and AUD01 tests must run in an operator-named isolated worktree or with demonstrated overrides that redirect every test-owned cache and state write to `build/**` or test-owned OS temporary paths. Tests must not write `work/lifecycle_status.json`, `work/lifecycle_events.ndjson`, `work/test_results_latest.json`, `work/live/alerts.log` or any production/dashboard operational state. If the current harness cannot prove those redirects before execution, STP-P00 is `BLOCKED` pending a separate audited engineering W. All permitted derived paths are snapshotted before and after and are never staged or committed.

AUD01 段改为：

> Audit evidence-document writes are limited to:
>
> [保留现有三个 docs 文件]
>
> The sole additional AUD01 writes are the test-owned derived and mirror paths authorized by Section 31.2, subject to its isolation, snapshot, no-stage and no-commit rules.

### P0-4：动态 census 被 matrix/AUD01 再次缩窄

动态主规则 `V2.1:1941-1953`、W01 reads `2788-2795` 和 acceptance `2933-2935` 正确；但：

- `V2.1:2007` 只要求 matrix 覆盖 registered tools 和 phase-critical libraries；
- `V2.1:2214-2233` 又以无修饰的 “Execution paths” 只列两个 general engines；
- `V2.1:2875-2882` 的 CODE REUSE MATRIX 同样只写 registered tools；
- `V2.1:2982` 仍写死 `144-tool matrix`。

仓库事实证明这会漏掉 `apps/live_e2e.cpp`：它有独立 `main()`（`apps/live_e2e.cpp:138`）并真实 place/cancel（`312-399`），但不在 registry/build 中。`preflight --order` 也会真实下单但整体注册为 `network_read`（`tools.json:67`; `apps/preflight.cpp:9-15,271-329`）。

最小替换：

> The P00 matrix covers every `REGISTRY_COUNT_OBSERVED` registry entry, every filesystem-discovered runnable/live-capable entry point and argument mode, every Makefile/CMake target, and every phase-critical library.

把 execution-path 标题改为：

> General engine execution paths — not the complete live-capable census:

并加入：

> Known standalone live-capable surfaces include `bench_order`, `fill_test`, `panic --execute`, `preflight --order` and `apps/live_e2e.cpp`; P00 must discover and classify all additional surfaces.

AUD01 项替换为：

> verify complete coverage of all `REGISTRY_COUNT_OBSERVED` entries and all filesystem-discovered runnable/live-capable entry points; sampling may validate classification depth only after census completeness is proved.

### P0-5：提供给操作员的 branch-release 请求不满足自身 schema

`V2.1:379-383` 称为 “correct branch-approval request”，但缺少 §7 在 `475-512` 强制要求的：

- `release_id`
- worktree
- exact W IDs
- exact write paths
- session count
- AUD01 是否独立授权

因此操作员按该模板回复后，agent 仍必须拒绝落盘。

最小替换：

> 请签发一个 durable release，明确记录 `release_id`、`active_prompt_path`/SHA、branch、base commit、authorized worktree、`authorized_W_ids=[STP-P00-W01]`、逐路径 exact writes、允许的 tool/network classes、production/live defaults、prerequisites 和 `session_count=1`。本 release 不自动授权 `STP-P00-AUD01`; AUD01 必须在同一 release 中单独点名或另发 release。

并在 §36 写明：

> Before the receipt commit exists, no write or branch/worktree creation is allowed except the Section 7 bootstrap-release-receipt sole exception.

## P1 — required governance/content revisions

### P1-1：V2.1 provenance 不真实，draft branch 与 P00 branch 混淆

`V2.1:61-68` 把原 V2 指令描述成生成 “this version”，并称生成会话没有 repository mutation。V2.1 实际由 `STP-R000-DRAFT-V21` 创建并提交了 `594603e`、`9952bef`。

`V2.1:78-89,524-526` 的 `OPERATOR-TBD` 应明确是未来 P00 scope，而不是已经获准用于 candidate construction 的 branch。

替换为：

> The quoted instruction produced the frozen V2 parent without repository mutation. This V2.1 candidate was produced under `STP-R000-DRAFT-V21` from base `1c93837a4422fe54717224d3ef9fe16bac0d1018` on the candidate-construction branch, with evidence commit `594603e` and closure commit `9952bef`. That release granted no BOOTSTRAP-0, STP-P00, STP-P01 or D-2 authority. `P00_AUTHORIZED_BRANCH`, `P00_AUTHORIZED_BASE` and `P00_AUTHORIZED_WORKTREE` remain `OPERATOR-TBD` until a separate P00 release names them.

### P1-2：D-2 mapping 应绑定最终 active prompt，而不是 rejected V2

`V2.1:303-308` 逐字采纳了旧审计文本，但仍写 `V2 §...`。正式 D-2 若继续引用 frozen/rejected V2，会留下二次解释链。

替换为：

> Before STP-P01, obtain operator decision D-2 that either recovers the authoritative v1 full text or maps D-1’s old references to the final release-pinned active prompt as follows: old §5 → active prompt §2 and §11.2–§11.4; old §11 → active prompt §4 conflict rule and §10. D-2 records the final `active_prompt_path` and `active_prompt_sha256`; it is an interpretation map only and does not reconstruct v1.

### P1-3：STATE lifecycle status 与 phase conclusion 未映射

`V2.1:237-242` 使用 `STP_P00_*`；`573-580,614-621,2925-2946` 使用无前缀状态；`2994` 又回到带前缀字符串。STATE schema 目前只有一个 “current status” 字段，机器状态不唯一。

替换为：

> `STATE.current_status` uses the unprefixed lifecycle enum. `STATE.phase_conclusion` uses the `STP_P00_*` conclusion enum. After W01: `current_status=IMPLEMENTED_AWAITING_AUDIT` and `phase_conclusion=STP_P00_IMPLEMENTED_AWAITING_AUDIT`. After audit PASS: `audit_result=PASS`, `current_status=AWAITING_OPERATOR_RELEASE`, and `phase_conclusion=STP_P00_AUDIT_PASSED_AWAITING_OPERATOR_RELEASE`.

### P1-4：DECISIONS rider 规则仍可能容纳 agent-authored rider

`V2.1:310-315` 已正确限制 DECISIONS；但 `462-467` 又无条件称所有 later riders/releases 都 append 到 ledger。

替换为：

> Only a rider or release that is itself verbatim operator text may enter `PLAN_SPORTS_TRADING_DECISIONS.md`. Agent-authored rider metadata, findings and status remain in release/audit artifacts, STATE and SESSION_LOG.

### P1-5：legacy 标签违反 exact-one primary classification

`V2.1:1957-1965` 要求每个 artifact 恰好一个 primary classification，且没有 `HISTORICAL` 类；`2128-2129,2161-2163` 却写 `HISTORICAL / DIAGNOSTIC_ONLY`。

替换为：

> Primary artifact classification: `DIAGNOSTIC_ONLY`. Evidence status: `HISTORICAL`. Orchestration/reporting components are classified separately as `REUSE_AS_IS` or `EXTEND` only after component-level separation.

### P1-6：release receipt 状态必须 closure

不修改 BEGIN/END 之间的 operator 原文；metadata 应改为：

> - status: CONSUMED  
> - consumed_by_evidence_commit: 594603e381f372abf415d60aa42089cc60755448  
> - consumed_by_closure_commit: 9952befd73f79195f2d38e2c45110601d8edbbea

SESSION_LOG 应追加 erratum，说明 required tests 还写过 `.pytest_cache/**` 和上述四个 operational paths；不得继续笼统声称派生写入只有三类。

## P2 — 非 canonical blocker，但 V2.2 可顺手清理

- `V2.1:1524-1526` 的 `LCB_97.5(mu_event)` 叙述数学上清楚，但机器字段名易误读；建议采用 `ci95_lower`。
- `V2.1:2766` 的 “Read completely where relevant” 应拆成 mandatory docs `read completely` 与 source `relevant sections recorded in READ MANIFEST`。
- P00 禁止外部动作（`V2.1:2938`），saved spec 只能支持 `SAVED_SPEC_VERIFIED`，不能升级为 `VENUE_VERIFIED`；建议在 §17 enum 中补该状态。
- change log 宣称所有 replacement “verbatim”，但 P0-4 acceptance 在 bullet 中以分号代替原审计句号；语义无差别，不单独阻断。
- 上述三个原审计 P2 均不是独立 canonical blocker。

## Verified strengths — 不得削弱

以下均保持且未被 V2→V2.1 diff 削弱：

- P00 phase ceiling 与 P01+、live/external/production 禁令：`V2.1:68-106,2498-2502`
- GUARDRAILS 和 D-1 binding：`651-659,831-848`
- 7 clean days、5-of-7、25%、×1.5/×2 provisional 门：`833-842`
- strict-through 的 latency、cancel-pending、at-price no-fill、same-timestamp adverse、gap fail-closed、one-contract capacity 和非数学下界限定：`1618-1684`
- 完整财富恒等式、settled lot `V_T=0`、joint worst-state、attribution 不重复扣减：`1328-1394`
- 四分裂、root-event、zero-fill、nested TRAIN、单次 VALIDATION/confirmation、multiplicity/sequential discipline：`1438-1596`
- multistate cancel 和校准 representativeness/power：`1263-1269,1744-1779`
- one-contract evidence only、禁止线性外推、scale 需新 ladder：`1790-1810`
- reuse taxonomy、同一 decision core、production path PRESERVE：`1955-2036,2082-2095,2224-2233`
- evidence commit A + closure commit B：`3004-3023`

## Final recommendation

- 不得 canonicalize 当前 exact SHA。
- 必须创建 `V2.2_CANDIDATE` 新路径和新 SHA；不要覆盖 V2 或 V2.1。
- D-2 的实质映射已经足够清楚，操作员可以先裁决其语义；但正式落盘应绑定通过复审的 V2.2 active path/SHA。D-2 不得被解释为 P01 或 P00 授权。
- 下一 release 只能授权：
  1. 保存 release receipt；
  2. 将当前 V2.1 冻结为历史候选；
  3. 创建 V2.2 candidate 并应用全部上述 P0/P1；
  4. 将旧 draft receipt closure 为 CONSUMED，并追加 SESSION_LOG erratum；
  5. 做必要的文档检查和镜像。
- 下一 release 不应授权 BOOTSTRAP-0、STP-P00、STP-P01、live/network/production、merge、push。
- V2.2 必须再经新的独立只读审计。只有 `PASS` 后，operator canonicalization release 才可采用其 exact path/SHA。
- 在启动 P00 前，还必须先证明测试副作用已隔离；若现有 harness 做不到，先开独立 engineering W 修复 test isolation，再发 W01 release。

