# AUDIT — CANONICAL PROMPT V2.2 CANDIDATE

- Date: 2026-07-11
- Audit mode: fresh independent, strictly read-only (zero-context agent)
- Target: `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md`
- Target SHA-256: `575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54`（本会话 `shasum -a 256` 实测，与预期逐字节一致）
- Evidence commit: `2f6512840702edff3217c55f69a61b4c393898d4`
- Closure commit: `3088573f7a4e5393ac55d0776b10756820d95835`
- Verdict: **`PASS`**（含 5 项 P2 清理建议，均不要求改动候选字节；无 P0、无 P1）
- Execution performed: none
- Repository mutation during audit: none

---

PASS

exact path `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md` + exact SHA `575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54` **可以被操作员 canonicalization release（STP-R002）采纳为 `active_prompt_path`/`active_prompt_sha256`**。V2.1 审计的全部 5 项 P0、6 项 P1 均已真实闭环（逐条核验见下），全部 P2 清理已应用，"不得削弱"清单完整保留（21 个未触碰章节与 V2.1 逐字节相同，已用 diff 证实）。

本 verdict 允许与禁止的范围：

- **允许**：将本审计报告逐字归档进 `docs/plan_audits/` 后，按 STP-R002 原文继续执行 canonicalization（promote exact path/SHA）与 D-2 落盘（操作员裁决原文进 DECISIONS）。
- **禁止（本审计不授权任何 phase 执行）**：不授权 BOOTSTRAP-0、STP-P00-W01、STP-P00-AUD01、STP-P01、D-2 以外的任何裁决、live/production/external 动作。canonicalization 之后，W01 仍需单独的 W-specific durable release，且该 release 必须先 pin 一份**独立审计通过的 Section 31.2 test-isolation artifact**——按 V2.2:2939-2952，当前 harness 未证明隔离前 STP-P00 = `BLOCKED`，须先走独立 engineering W。
- 任何对候选的字节或路径改动都会产生新 candidate，需重新独立审计（V2.2:16）。

## Artifact state

只读核验全部通过：

- branch：`plan-sports-market-dynamics-v2`，HEAD = `1a35f51`；`9952bef..HEAD` 提交链线性（`git log --graph` 无分叉）。
- 候选 blob 在 evidence commit 与 HEAD 的 SHA-256 相同（`git show 2f65128:… | shasum` = `git show HEAD:… | shasum` = `575ea27a…`）；`git log -- <候选路径>` 只有 `2f65128` 一个提交——**evidence commit 之后分支上的 8 个后续提交（D-1.1、hypothesis ledger、STP-R002 receipt、MM 计划等）均未触碰任何 prompt/audit 文件**（`git diff --name-only 2f65128 HEAD` 证实）。
- 工作区相对 HEAD 无 docs 差异；未跟踪项只有先前两次审计都记录过的 `?? outputs/`。
- 本地 `main` 仍为 `92918bb42d49626ecc767359d216240c34f1c6dc`，与 V2.1 审计记录一致，未被触碰。（观察项：`origin/main` = `0613e79`，本地 main 未 fast-forward——先前已存在的状态，与本候选无关。）
- evidence commit `2f65128` 只新增候选（3274 行）、V2.1 审计归档、R001 receipt，并对 R000 receipt 做 4 行 metadata 修改；closure commit `3088573` 只追加 `docs/SESSION_LOG.md` 62 行。
- 冻结历史候选完好：V2 SHA `bc2fbf65…f341`、V2.1 SHA `e83160bd…106c`、V2.1 审计归档 SHA `301f2aab…f791`，均与 R001 receipt prerequisites（STP-R001-DRAFT-V22.md:50-56）逐一相符。
- receipt 内部哈希一致：R001 operator-text SHA 实算 = `71e54657…9625c`（receipt 第 7-9 行声明值）；R002 operator-text SHA 实算 = `e0a91f79…5fa3`（receipt 第 49-50 行声明值）。
- R001 receipt 状态正确闭环为 `CONSUMED`，consumed_by 指向真实的 `2f65128`/`3088573`；闭环提交 `2f000e8` 是 metadata-only（4 行），operator verbatim 块未动——正确避开了 self-referential hash。
- V2.1 审计要求的 R000 receipt 修正已落实：STP-R000-DRAFT-V21.md:17-19 现为 `CONSUMED` + 两个真实 commit。
- Desktop docs-mirror 中候选与 V2.1 审计的 SHA 与仓库一致。
- 未运行任何测试/工具执行，保持严格只读（V2.1 审计已证明测试套件会写运营状态）。

SESSION_LOG（2026-07-11 06:42 UTC 条目）的两条 load-bearing 声明独立复核为真：全文 diff（`git diff --no-index` V2.1→V2.2，349 insertions/175 deletions）显示改动只落在 header 块与 §0、§4、§5、§6、§7、§8、§17、§20、§26、§29、§31–§36 共 16 个编号章节——**其余 §1、§2、§3、§9–§16、§18、§19、§21–§25、§27、§28、§30 恰好 21 个章节逐字节相同**；§20 内的唯一改动就是 `ci95_lower` 字段替换（V2.2:1616-1619）。

## V2.1 审计 remediation 逐项核验

### P0（5/5 闭环）

- **P0-1 promotion gate 自相矛盾 → 闭环。** 审计的最小替换语言近逐字落在文件头 CANDIDATE PROMOTION GATE（V2.2:8-18）；§36 开头加入了要求的 stop 语句（V2.2:3234-3237）。旧矛盾点全部翻转："BOOTSTRAP-0 currently executable" 全文已无（grep 零命中），§7 改为 "This candidate grants no executable phase"（V2.2:593-598），§29 改为 "This candidate authorizes no phase"（V2.2:2606-2608），§31 标题/status 改为 "NOT AUTHORIZED BY THIS CANDIDATE"（V2.2:2860-2873），§34 明确 prompt-candidate audit 走 promotion gate 而非 W 协议（V2.2:3184-3186）。prompt-candidate audit 与 STP-P00-AUD01 的区分明确（V2.2:17-18, 518-519）。
- **P0-2 frozen V2 被定义为 active/writable → 闭环。** §8 改为 release-pinned active prompt、其余冻结路径只读（V2.2:606-608）；§31.2 allowlist 删除了 V2 路径，改为 "release-named `active_prompt_path` is already immutable and is read-only during W01… W01 may not repair or rewrite it"（V2.2:2916-2921）；§6.3 把 V2 与 V2.1 都列为冻结历史工件并给出 SHA（V2.2:489-494, 513-515）。
- **P0-3 测试副作用与 allowlist 冲突 → 闭环并强化。** §31.2 落入完整的 test-isolation 硬起动门：四个具名 operational 路径（`work/lifecycle_status.json`、`work/lifecycle_events.ndjson`、`work/test_results_latest.json`、`work/live/alerts.log`）加 `.pytest_cache/**`（比审计替换语言更全）明令禁写，隔离未证明 ⇒ STP-P00 `BLOCKED`（V2.2:2939-2952）；AUD01 段改为三个 evidence 文件 + 引用 31.2 派生路径规则（V2.2:3111-3119）；acceptance、§33、§35、§36 全部同步（V2.2:3082-3084, 3159-3160, 3211-3213, 3257-3260）；§0 interpretation 第 4 条把它设为 release 前置（V2.2:116-119）。
- **P0-4 census 被 matrix/AUD01 缩窄 → 闭环。** §26.2 matrix 覆盖面按审计语言替换（V2.2:2106-2108）；execution-path 标题改为 "not the complete live-capable census"（V2.2:2317）；五个已知 standalone live-capable surfaces 具名且不声称完整（V2.2:2322-2325）；§31.4 CODE REUSE MATRIX 同步（V2.2:3015-3018）；AUD01 改为 "census completeness 证明后 sampling 才可用于分类深度"（V2.2:3130-3133）；"144-tool matrix" 硬编码消失，仅存的两处 "144" 都带 "generation-time evidence only" 限定（V2.2:2039, 2907）。
- **P0-5 release 请求不满足自身 schema → 闭环。** §5 的请求模板包含 release_id、path/SHA、branch、base、worktree、`authorized_W_ids=[STP-P00-W01]`、逐路径 writes、tool/network classes、defaults、prerequisites（含 isolation artifact path/SHA）、`session_count=1`、AUD01 单独点名（V2.2:423-431）；§36 第 4 条加入了要求的 pre-receipt 禁写语句（V2.2:3241-3242），第 5 条复述完整字段清单（V2.2:3243-3254）。

### P1（6/6 闭环）

- **P1-1 provenance 不真实 → 闭环。** "Truthful provenance" 区分 V2（无仓库变更）、V2.1（STP-R000，base `1c93837`，evidence `594603e`，closure `9952bef`）、V2.2（STP-R001，base `9952bef`），并明确 drafting release 不授予任何 phase 权限、P00 branch/base/worktree 保持 OPERATOR-TBD（V2.2:84-99）。所引 commit 均与 git 实况一致。
- **P1-2 D-2 绑定最终 active prompt → 闭环。** §4 按审计替换语言改写，D-2 记录最终 `active_prompt_path`/`active_prompt_sha256`（V2.2:342-349）。映射的章节号（旧 §5 → §2、§11.2–§11.4；旧 §11 → §4 conflict rule、§10）在 V2.2 编号下语义正确：§2 含主假设/mainline 定义（V2.2:201-218），§11.2–11.4 含选品宇宙与 Tracks（V2.2:963-1014），§10 是 GUARDRAILS OPERATOR-TBD 机制——STP-R002 的 D-2 可按此直接记录 exact numbers，可执行。
- **P1-3 STATE 状态映射 → 闭环。** `current_status`/`phase_conclusion`/`audit_result` 三字段分离（V2.2:634-636），binding mapping 覆盖 W01 后、REVISE/REJECT、PASS 三种情形（V2.2:663-675），枚举与 §3 的 STP_P00_* conclusion 清单（V2.2:276-283）闭合。
- **P1-4 DECISIONS rider → 闭环。** §6.3 riders 列表改为 "只有 operator verbatim 才进 DECISIONS，agent-authored metadata 留在 release/audit/STATE/SESSION_LOG"（V2.2:526-534），与 §4 的 verbatim-only + newest-first append 语义（V2.2:351-356）一致。
- **P1-5 exact-one classification → 闭环。** §26.3 改为 "Primary artifact classification: `DIAGNOSTIC_ONLY`. Evidence status: `HISTORICAL`."（V2.2:2263-2266, 2229-2231）；并新增 §26.1 的 shortlist 规则把所有残留 "A/B"、"A or B" 定义为 pre-audit shortlist、最终 matrix 行禁止多重 primary（V2.2:2060-2064）——这是对审计意图的完备化处理，覆盖了 §26.3 各处残留的 slash 写法。
- **P1-6 receipt CONSUMED + SESSION_LOG erratum → 闭环。** R000 receipt metadata 已改（见 Artifact state）；SESSION_LOG 06:42 条目含完整 erratum，逐一点名 `.pytest_cache/**` 与四个 operational 路径（SESSION_LOG.md:142-147）。

### P2（4/4 应用）

`ci95_lower`（V2.2:1616-1619）；mandatory docs "read completely" 与 source "relevant sections 记入 READ MANIFEST" 分离（V2.2:2882-2905）；§17 枚举加入 `SAVED_SPEC_VERIFIED` 并限定 saved spec 只能支持该状态（V2.2:1411, 1415-1417）；change log 不再谎称 "verbatim"，改为 "semantic implementations… not falsely described as verbatim quotations"（V2.2:26-27）。change log 声称 "three content cleanups plus the wording correction" 与 V2.1 审计 P2 节实际条目数相符。

## 内容审计 — 新发现（全部 P2，不阻断 canonicalization）

- **P2-1（promotion 后的不可变横幅）**：候选被 promote 后字节不变，因此文件首行横幅将永远写着 "STATUS: CANDIDATE — NOT CANONICAL — NOT EXECUTABLE / ONLY A FRESH INDEPENDENT READ-ONLY PROMPT AUDIT IS ALLOWED"（V2.2:4-5），文件名也永久带 `_CANDIDATE`。promotion gate 段（V2.2:10-18）给出了退出条件（"Until both conditions are satisfied, stop"），§34/§36 可自洽解读，且误读方向是拒绝执行（fail-safe）。但为消除未来会话的死锁风险，**建议 STP-R002 receipt 与 D-2 文本显式声明：文件内横幅是 creation-time metadata，被 release 的 `active_prompt_path`/`active_prompt_sha256` pinning 取代**。无需改候选字节。
- **P2-2（§31.2 override 模式的措辞张力）**：隔离要求写 "redirect every test-owned cache and state write to `build/**` or test-owned OS temporary paths"（V2.2:2942-2944），而隔离证明后的允许清单仍含 `work/logs/**` 与 `work/test_results.ndjson`（V2.2:2954-2956）。在 isolated-worktree 模式下两者自洽（这些路径落在隔离 worktree 内）；在 "demonstrated overrides" 模式下字面冲突。fail 方向安全（"Any other unexpected write stops the session"，且 isolation artifact 须先独立审计）。**W01 release 与 isolation artifact 必须显式声明隔离边界及这两个路径归属哪个树**；无需改候选字节。
- **P2-3（`AUDIT_PASSED` 成为不可达状态）**：§9 lifecycle 列出 `AUDIT_PASSED` → `AWAITING_OPERATOR_RELEASE`（V2.2:703-710），但 §8 binding mapping 在 audit PASS 时直接置 `current_status=AWAITING_OPERATOR_RELEASE`（V2.2:672-675）。此写法来自 V2.1 审计自身的替换语言，语义无危险，仅留一个永不被赋值的枚举值。将来修订时可注明 `AUDIT_PASSED` 为瞬时/保留状态。
- **P2-4（D-1.1 晚于本候选）**：操作员 2026-07-11 D-1.1 裁决（共用账户 + 四条执行护栏，DECISIONS:10-32，commit `7fc5863`）在 V2.2 evidence commit 之后落盘。逐条比对未发现矛盾（V2.2:2365-2373 的 reserve-before-send/private state/reconciliation 与护栏 1/2 相容；V2.2 未假设账户为系统独占），且 §4 authority order 使 DECISIONS 自动 binding。**P08/P10 的 W 定义必须从 DECISIONS 引入 D-1.1 四护栏**——这是执行期义务，不是候选缺陷。
- **P2-5（§34 对 prompt-audit 会话的措辞）**："it must stop before BOOTSTRAP-0"（V2.2:3186）字面上可能被误读为禁止审计会话运行只读 git 检查；实际 BOOTSTRAP-0 本身定义为 read-only（V2.2:362），审计做等价只读检查不构成越权。措辞小疵，误读方向无害。

## "不得削弱"清单核验 — 全部完好

V2.1 审计列出的十项 protected strengths 逐项确认：phase ceiling 与全禁令（强化，V2.2:106-148, 2606-2608）；GUARDRAILS/D-1 binding（§10 全节、§4 相关段与 V2.1 逐字节相同；GUARDRAILS 文件本身 `git diff 1c93837 HEAD` 为空，D-1 条目字节级 diff 相同）；7 clean days/5-of-7/25%/×1.5/×2 provisional 门（V2.2:920-937，未动）；strict-through 全套语义（§22 未动）；完整财富恒等式与 settled-lot `V_T=0`（§18 未动，V2.2:1441-1449）；四分裂/root-event/统计纪律（§20.1 未动，V2.2:1535-1542）；multistate cancel 与 calibration 代表性/功效门（§16/§24 未动，V2.2:1352-1358, 1843-1848）；one-contract capacity 与禁止线性外推（§24 未动）；reuse taxonomy/同一 decision core/production PRESERVE（§26 改动均为强化）；evidence commit A + closure commit B 协议（V2.2:3161-3167 保留）。

## Final recommendation

1. 将本审计报告**逐字**归档为 `docs/plan_audits/AUDIT_PROMPT_V2_2_2026-07-11.md`（保留原字节并记录 SHA-256）——这是 STP-R002 receipt（`9041a39`）stop 条款指定的恢复动作。
2. 归档后，新会话可按 STP-R002 原文执行：promote exact path/SHA → D-2 落盘（含旧 §5→§2、§11.2–§11.4，旧 §11→§4 conflict rule、§10 的 exact numbers）→ full exit ritual。R002 明文不授权 BOOTSTRAP-0/P00/任何 phase。
3. R002 执行时落实 P2-1 的横幅声明；未来 W01 release 落实 P2-2 的隔离边界声明。
4. W01 之前的硬前置保持不变：独立审计通过的 Section 31.2 test-isolation artifact。当前 harness 未证明隔离 ⇒ 先开独立 engineering W，STP-P00 保持 `BLOCKED`。
5. 本审计授权范围到此为止：**只支持 canonicalization 与 D-2 按 R002 进行；不授权任何 phase、任何 BOOTSTRAP-0、任何 live/production/external 动作。**
