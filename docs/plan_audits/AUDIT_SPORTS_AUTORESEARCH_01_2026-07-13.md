# AUDIT — SPORTS-AUTORESEARCH-01 mission draft(2026-07-13)

- auditor: Cowork session, 2026-07-13, branch `plan-sports-market-dynamics-v2`
  @ `2575dcf`
- audited text: `docs/plan_audits/SPORTS_AUTORESEARCH_01_MISSION_TEXT_2026-07-13.md`
  (operator draft, archived verbatim this session)
- method: GUARDRAILS §6 checklist + repo-state verification of every external
  claim the mission makes (5 branches inspected: current, ec2/main,
  codex/pipeline-recovery-hardening, pipe-w05-ui-data-root, w07-rfq)

## 一行裁决

**⚠️ ACCEPT-WITH-FINDINGS(有条件通过)。** 计划本体结构优秀、fail-closed、
与 GUARDRAILS 兼容;但**今天发布必然在 GATE A 立即停止**(PIPE-W05 仍处
ADDENDUM-4 DATA-INTEGRITY P0 NO-GO,Phase-A 验收不存在于任何分支),GATE B
同样会停(W09 只有注册令,未批钱未开机)。发布前需修 4 项 P1、知悉 6 项 P2。

---

## 1. 启动阻断事实(不是计划缺陷 — 计划对它们正确地 fail-closed)

- **B-1 · GATE A 今天必 FAIL。** PIPE-W05 最新已提交状态 = ADDENDUM 4
  "FINAL REVIEW VERDICT: DATA-INTEGRITY P0 — NO-GO"(2026-07-12),
  ADDENDUM 5–8 整改仍在进行(07-12 RFQ 封印定点恢复脚本 approval-gated、
  NOT run;07-12 印被 12 个 checkpoint=None 的跨小时 RFQ 文件卡住)。
  全仓无任何 Phase-A acceptance 工件。
  出处:`docs/plan_releases/pipeline/PIPE-W05-SPEC-2026-07-12.md`
  @ codex/pipeline-recovery-hardening(ADDENDUM 4/7/8)。
- **B-2 · GATE B 今天必 STOP。** W09 状态 = "Registration/specification
  only. Do not implement or launch PIPE-W09 until PIPE-W05 receives separate
  final GO and completes Phase-A acceptance."(同文件 ADDENDUM 5,l.211)。
  即:任务的正确启动顺序 = W05 收尾 → Phase-A 验收 → W09 批钱开机 → 本任务。
- **B-3 · MODE 2 今天不可能。** `docs/PLAN_SPORTS_TRADING_STATE.md`:
  current_status=AWAITING_OPERATOR_RELEASE、next_authorized_action=NONE。
  整个任务将全程 MODE 1;VERDICT_PASS 数量恒为 0;现实的终止条件是
  §22-2(2–4 个 PROMOTION_READY)或 §22-3/4。预期应据此设定。

## 2. 引用事实核验(逐项对仓库)

| 任务引用 | 核验结果 |
|---|---|
| PIPE-W05 S3 research releases | 存在(spec + `tools/research_data.py`),但只在 recovery 分支,未验收(B-1) |
| W09 research machine | 仅注册令,未创建(B-2) |
| `docs/research_notes/HYPOTHESIS_LEDGER.md` | ✅ 存在,H-OP-1/H-OP-2 在册 |
| `sandbox/research/workbench/hypotheses.json` | ✅ 存在,ATL-TTS/BURST/TOX/RES/FLOW/WINDOW-01 六卡全在 |
| D-2 canonical prompt pin(575ea27a…) | ✅ 与 STATE/DECISIONS 一致 |
| USA–Belgium "103–104¢, ~9% joint minutes" pilot | ⚠️ 出处仅为**未提交**的 sandbox 文件(`sandbox/research/reports/event_intel/data/episodes/26JUL06_USABEL.json`);已提交历史中无此数字(F-7) |
| prompt V2.2 的 prior-exposure ledger / immutable split / holdout 规则 | ✅ 存在(prompt l.799/916/1537/1566),GATE C 与其兼容 |

## 3. P1 发现(发布前必须修)

- **F-1 · 无数据保留(holdout reservation)规则。** MODE 1 探索会读完全部
  已封印数据;Tier-5 HISTORICAL_CONFIRMATION 要求 "untouched sealed
  period",届时只剩未来新增日可用。若这是本意,请明写"确认集只来自任务
  开始后新封印的数据";否则在任务文本中划出一段**本任务永不打开**的日期
  切片(如最新 K 个封印日),并写入 PRIOR_EXPOSURE.json。不修则未来确认
  实验的数据成本被静默转嫁。
- **F-2 · 缺 GUARDRAILS Q1(log-odds)条款。** §8 特征字典与 §11-E DP 阶梯
  涉及 vol/toxicity/偏移等策略数学,但全文无一处要求 log-odds 空间计算。
  Q1 是宪法级 MUST("Price-space math on probability contracts is a
  reject")。加一句:凡 vol/skew/spread/toxicity 类特征与策略量,计算在
  log-odds,映射回美分只在边缘。
- **F-3 · §25 与会话退出仪式冲突 + 无断点续跑协议。** "return
  AUTORESEARCH_COMPLETE… Then stop" 不得豁免 CLAUDE.md 强制退出仪式
  (commit + SESSION_LOG + mirror)。且本任务体量远超单会话,但文本没有
  会话边界规则。补:每会话照常退出仪式;同一 RUN_ID 断点续跑,续跑会话
  必须重验 §2 身份清单后才可追加 TRIAL_REGISTRY。
- **F-4 · 写权限未逐路径枚举(P7 契约:WRITES are enumerated)。**
  "updates to research-owned ledgers" 有歧义:
  `docs/research_notes/HYPOTHESIS_LEDGER.md` 是经裁决的正典台账,受
  dedupe-before-number 规则约束。建议枚举:可写 =
  `work/research/auto_research/<RUN_ID>/**` + `sandbox/research/**`;
  正典台账只允许**追加**新假设条目并标注来源 RUN_ID,合并/改号仍走
  dedupe 裁决;`docs/**` 其余、config、生产代码一律不可写。

## 4. P2 发现(知悉/顺手修)

- **F-5 ·** §4 先验暴露下限 "2026-07-06 through 2026-07-10" 已过时:Event
  Intelligence episodes、mm_sandbox、W06 Stage-0 probe、RFQ 48h 检视覆盖
  到 07-13。通配句能兜住,但明写的日期会误导,改为 "through mission start"。
- **F-6 ·** 未钉基线分支/commit。`research_data.py` 与 W05 spec 在
  recovery 分支,策略分支上没有;任务须声明其运行基线是哪个合并态
  (建议:W05 Phase-A 验收后并回 main 的那个 commit)。
- **F-7 ·** Belgium pilot 数字无已提交出处(见 §2 表)。种子研究 1 引用它
  之前,先把该 episode 工件提交或记 sha 存档(E2:只活在未提交文件里的
  事实等于不存在)。
- **F-8 ·** 费用权威:OQ-1(费率表 ratification)仍开放。§18 "exact fee
  facts" 应注明出处 = 仓库 fee facts + OQ-1 未追认的保留意见。
- **F-9 ·** 违反 GUARDRAILS P1 的形式要求:计划未声明推进哪个
  MM_ROADMAP 阶段/满足哪个门。一句话即可(支撑阶段 1.5 研究、为 STP
  后续 phase 供料,不动任何门)。
- **F-10 ·** 预期管理:§14 强制 RFQ 分析可能整体 DATA_STARVED —— W05
  交叉验证已记录 "sealed research-ready ARCHIVES 目前不含 RFQ 内容"
  (解决途径 a/b 未定)。§22 的隔离-继续规则能兜住,不阻断。

## 5. GUARDRAILS §6 检查单

1. 推进命名阶段不跳门?⚠️ 未声明(F-9,一句话修复)。
2. 触实盘?✅ 否——§0 明令禁止下单/RFQ/shadow/live,§23-31 强制未授权横幅。
3. log-odds + 费用?⚠️ 费用 ✅(逐笔、出处、unknown-fee 隔离);log-odds ❌(F-2)。
4. 悲观口径验证?✅ strict-through 一手为绑定口径、at-price 不成交、
   歧义对己不利、promotion 用 conservative——与 Q2 完全一致。
5. 交易数据走 WS?N/A(纯研究,无交易)。
6. 新行为带测试含经济符号测试?✅ §8 要求逐公式 unit+sign tests(Q9)。
7. 管道续跑?✅ 不触生产(S3 只读、禁 SSH/staging/服务变更),P4 满足。
8. 破坏性可逆、范围有界?✅ 只读+run 目录写;写权限枚举待补(F-4)。
9. 更新其失效的文档?⚠️ 台账链接有规定,基线钉定缺(F-6)。
10. 绿色状态会撒谎吗?✅ 四标签体系+覆盖立方体+exclusions waterfall 正面
    防住 D2;"per-release 判 L2 存在性"直接纠正了旧文档谎言模式。

## 6. 值得点名的优点

四标签体系(证据/时钟/切分/结论)从制度上禁止用一种标签冒充另一种;
§16 以 root event 为推断单元 + 全事件(含零成交/零报价)进估计量,堵死了
"只数赢的" 的经典自欺;§17 负控制清单与 BH/Holm 双层校正齐备;§20 逐字
落实 D-3 Visualize-Everything;§9 每周期强制 2 条"只有我们的数据才可能
发现"的假设,是对"通用量化扫描器"的正确防御;全文无新治理框架,符合
D-1 与操作员"一个任务,不建王国"的意图。

## 7. 建议的发布顺序

1. 修 F-1…F-4(文本改动,半小时量级);顺手改 F-5/F-8/F-9。
2. 等 PIPE-W05:ADDENDUM 8 恢复脚本获批执行 → 07-12 印落 → 版本绑定
   publication → Mac no-SSH Phase-A 验收。
3. W09 spend gate:批实例类型/时价/预算/idle-shutdown,开机。
4. 届时以定稿文本+钉定基线 commit 正式发布本任务,并在
   `docs/PLAN_SPORTS_TRADING_DECISIONS.md` 记一条裁决(保持 D-1 单一
   权威链:本任务=探索供料,不动 STP phase 门)。

---

## ADDENDUM 1(2026-07-13 晚)— B-1 事实更新:07-12 封印已落,GATE A 剩两步

出处:操作员转达的 EC2 侧收口报告,逐字归档于
`SPORTS_AUTORESEARCH_01_STATUS_RELAY_2026-07-13.md`(本会话无法独立核验,
数字以生产侧归档为准)。

- **已清:** 07-12 SEALED + verify PASS(18:31Z,322 文件 / 1.0914 亿行 /
  go_no_go=True / 印 sha bc37de4c…)。§1 B-1 中"07-12 印被 12 个
  checkpoint=None 文件卡住"一条不再成立。
- **GATE A 仍未通,剩余链条 =** (a) **B12**:vaultWriter 缺
  `s3:GetObjectVersion`(+GetObject)on research/*,07-12 研究 release
  发布失败关闭——**操作员控制台 IAM 动作**,任何 agent 无权代办(与本
  任务 §0 禁 IAM 变更一致);(b) 版本绑定 publication 完成;
  (c) Mac no-SSH **Phase-A 验收**。
- **GATE B(B-2)与 MODE(B-3)判断不变**;两个新登记的容量债
  B11(封印链 vs ingest 写锁竞争)与 B9(RFQ 解析税)属 W06 Stage 2
  前置工程,不阻塞本任务发布。
- §7 发布顺序更新:第 2 步现余「B12 IAM → publication → Phase-A 验收」。
  P1 修文本(F-1…F-4)仍是第 1 步,未动。
