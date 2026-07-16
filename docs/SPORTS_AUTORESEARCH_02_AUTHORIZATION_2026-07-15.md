# SPORTS-AUTORESEARCH-02 — 授权与执行计划(修订版,含操作员追加条款)

**Status: 操作员定稿待发。**发给执行 agent 时,整份文档原样粘贴。
**基底:** deep01 终止后由执行 session 起草的继任计划,经操作员方
陪跑 session 修订合并(2026-07-15)。修订来源:deep01 全天复盘
(`docs/SESSION_LOG.md` 2026-07-15 21:53 条目)与
`docs/PROPOSAL_AUTORESEARCH_CONVERGENCE_CLAUSES_2026-07-15.md`。

---

## 0. 收敛条款(五条刹车 — 全程最高优先级,违反任一条即偏离授权)

1. **时间盒:**数据修复/工具加固类工作单次连续投入 ≤ 45 分钟;超时唯一
   合法动作是停下向操作员报告,禁止沉默续干。
2. **递归上限:**登记器/终审器/收据工具自身的代码缺陷属工程债,不是
   "数据修复",不触发新 repair 注册。本 run 数据修复注册上限 = 3 次,
   达到上限再遇需修复事项 → 任务中止移交操作员。
3. **完成的定义:**每道关卡开工前写死合格线(默认:专项测试全绿 +
   交叉审计 0 Critical/High)。低于该线的发现一律登记工程债,不阻塞。
4. **心跳契约:**超过 20 分钟无新落盘产出必须主动报告状态;子任务空转
   计入 20 分钟。
5. **并行许可:**数据扫描 ∥ 工具收尾允许并行;终审器只需在打开结果前
   完工,不阻塞扫描点火。未列明的默认串行。

## 1. 身份与权限预检

- 确认连接的是独立研究机 W09:`i-0e53d134dceffe166`,不是主 EC2。
- 重计算只能在 W09;主机仅负责调度、查看日志和同步报告。
- 验证 W09 instance role、AWS identity 和 S3 只读访问;不得打印或复制密钥。
- 对 sealed release manifest 做 head/list 验证,不立即扫描大对象。
- 检查旧 run 没有残留进程,但不得修改其文件。

## 2. 冻结旧任务

旧 run:`20260715T112538Z__c21a79a8cff__deep01`

- 保持 DATA_INTEGRITY_BLOCKED;不执行 repair-08。
- 不覆写旧 hypothesis ledger、scratch、报告或 repair-01~07 收据。
- 旧数据(deep01 用过的 07-12/07-13)视为已看过的开发数据,不得再
  冒充 unseen evaluation。

## 3. 建立新 run

- 全新 RUN_ID、scratch、日志、资源收据和输出目录。
- 记录基础任务书 SHA:
  `9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c`
- 记录继承研究 commit:`6bcb5cb972865abc9c133bd9635c05540b9ced86`
- **框架复用(硬性):**登记/收据/终审框架必须复用 deep01 已测版本
  (commit 6bcb5cb),禁止重写或重构;发现其缺陷按条款 0.2 记工程债。
- 原有七个非 RFQ 假设注册为 successor trials,不修改旧状态;本计划
  新增假设(见 §5)在开扫前完成预注册并冻结门槛。

## 4. 数据量门槛(操作员修订:双档制)

任何重扫描前,盘点全部已封存 sealed releases(约 6 个 release / 80GB,
以 S3 manifest 实测为准):

- **新数据入册体检(硬性前置):**每个纳入本 run 的 release 在注册前,
  必须先通过全量逐行结构体检(复用 deep01 audit 工具,~5 分钟/59GB);
  不合格对象按既定整对象隔离规则处置并留收据,处置后方可入册。
- **判决档(维持冻结门槛,不变):**PROMOTION_READY 判定要求
  ≥20 个独立日期、每核心假设 ≥200 相关 root events、chronological
  unseen 考试通过。本轮数据不足该门槛 ⇒ **本轮不产生任何晋级**。
- **日期盘点纪律(deep01 交接书更正,2026-07-15):**S3 的
  "6 releases / 80GB" 是 publication inventory,不是 6 个独立日期。
  07-11 为 QUARANTINED_LEGACY 不可用;07-12/07-13 各有多个 variants,
  **每个日期只选一个当前有效 canonical、version-bound release**,
  禁止把 variants 合并重复计算。截至 2026-07-15,合格独立日期 = 2。
- **探索档(操作员裁决,本轮执行):**不足 20 日**不停机**,进入
  EXPLORATORY 档。鉴于合格日期仅 07-12/13 两天且三大主线在其上
  已由 deep01 跑完(结果不可变),**本轮不重跑 §5 A/B/C 三主线的
  已有假设检验**(重跑同数据 = 零新信息),只执行:
  (a) §5.D 三项描述性扫描(deep01 未做过,系新知识);
  (b) 若预检发现 07-13 之后已有新封存合格日期,可将其纳入三主线
  增量计算,并同时保留其"未来考卷"身份的替代安排报操作员裁决。
  所有产出强制 EXPLORATORY + PRIOR_EXPOSED 横幅。
- L2 日期必须通过 sequence gap、missed frame 和完整性检查。

## 5. 三条研究主线 + 描述性扫描

**A. L1 / trades**
- C1-SPREAD-CAPTURE-01 · C1-LARGE-FLOW-CONTINUATION-01 · C1-PREMATCH-TTS-01
- 检查跨日期稳定性、手续费后收益、悲观成交、延迟和容量。

**B. L2 / order book**
- C1-HFOLLOW-RETREAT-01 · C1-DEPLETION-REFILL-01
- 只使用完整 L2 日期;队列位置和 microprice 仍为诊断项,除非独立校准完成。

**C. 不同盘口 / Market Atlas**
- C1-THREEWAY-OVERROUND-01 · C1-SOCCER-POISSON-RV-01
- 扩展 sport、market type、比赛阶段和盘口结构;声称相对价值前先证明
  outcome family 互斥性与 payout exhaustiveness。

**D. 描述性扫描(EXPLORATORY,操作员增补)**
- 网球买卖分侧 markout:剧烈摆动后买入冷门侧的 1/5 分钟 markout 分布,
  量化散户热门-冷门偏差的存在性与厚度(做市补位方向的证据基础)。
- 跨市场一致性失调扫描:同赛事关联市场间数学关系失调超过费用线的
  窗口频率、厚度、存活时长。
- 单边盘口补位机会:各 sport 双边报价率与单边窗口时长分布
  (deep01 图谱已示棒球 ~12.5% 单边)。

**§5.D-bis 开放勘探授权(操作员定位声明,2026-07-15):**本 run 的
目的**不是证明策略**,而是在已有数据中发掘基本规律、探寻方向。因此
上列三项扫描是**最低集**,不是上限:agent 可在已入册数据上自主增加
描述性分析(例:日内流动性节律、比赛事件前后价差动力学、订单流不
平衡特征、散户行为模式、各运动市场效率画像等),每项在扫描台账登记
一行 + 三张收据即可开跑,无需操作员逐项批准。约束仅四条:①只读已
入册数据;②全部产出 EXPLORATORY + PRIOR_EXPOSED 横幅,不做任何
晋级/可交易性声明;③不重跑已有预注册假设的原判(那是判决档的事);
④总费用/时长上限(§7)不变,勘探项目按"预期信息量/成本"自行排序,
上限到即收笔。产出统一汇入 §7-bis 图文报告的"信号地图"章。

**§5.D 轻量流程豁免(防过度治理,操作员裁定):**描述性扫描为只读
探索计算,**不适用** repair 注册器/终审器机制。每项扫描的全部手续 =
三张收据:输入数据指纹、脚本 SHA-256、输出文件指纹,外加 EXPLORATORY
横幅。为描述性扫描新建任何注册器、事务机制、交叉审计层 = 违反授权。
扫描脚本出 bug 直接改直接重跑(输入只读,重跑无副作用),改动记入
SESSION_LOG 即可。

**刹车触发话术(统一模板,防沉默续干):**任何 §0 条款触发时,必须
立即输出:"⚠️ 刹车触发:[条款号] | 已完成:[一句话] | 卡点:[一句话] |
申请:[继续/改道/中止] | 等待操作员指令",然后停止该方向的一切工作。

**RFQ 明确排除**
- 只引用旧 run 的 blocked 结论和收据;不读全量 RFQ、不修复 replay、
  不运行 repair-08。仅新的操作员授权可重开 `W-SAR-RFQ-REPLAY-01`。

## 6. 候选策略纪律

优先顺序:1. Spread capture;2. H-FOLLOW;3. Three-way overround;
4. Pre-match dynamics;5. 描述性研究。

每个(未来的)晋级候选必须包含:明确 entry/quote/cancel/exit 规则、
手续费和悲观成交假设、延迟/容量/kill conditions、chronological
unseen-test 表现、数据质量和集中度、PROMOTION_READY dossier。
**本轮为 EXPLORATORY,不产生晋级;产出物为信号地图 + deep03 火力清单。**

## 7. 资源与停机控制

- 入册体检未通过的 release 禁止参与扫描。
- 按日期增量物化并写 checkpoint,避免重复处理旧数据。
- 全局去重等重内存操作必须分桶/分日期实现,禁止单条全局窗口物化
  (deep01 OOM 教训,42.8GiB 上限实测)。
- W09 时间/费用上限:本 run 计算总时长 ≤ 6 小时或估算费用 ≤ $5,
  触线即收尾出报告(哪怕部分完成),不得申请豁免。
- 每阶段记录 wall time、CPU、内存峰值和估算费用。
- 成功、失败或数据不足,一律:提交记录、写 SESSION_LOG、同步 docs
  镜像、停止 W09。

## 7-bis. 交付物:第一份图文并存研究报告(REPORT_VISUAL_01,操作员钦定)

本 run 的最终交付不是裸表格,而是一份操作员可直接阅读分析的图文报告
(Word/PDF,中文,每图配一段白话解读)。缺口清单与来源:

1. **repair-04 档案回传(F-1,开机第一动作):**从 W09 只读同步
   repair-04 目录回本地并核对指纹(见
   `docs/plan_audits/SPORTS_AUTORESEARCH_01_CLAIMS_VERIFICATION_2026-07-15.md`)。
2. **三项描述性扫描结果(本 run 新算):**网球分侧 markout 分布、
   跨市场失调窗口统计、单边盘口时长分布 —— 每项输出 CSV + 至少一图。
3. **deep01 冻结成品可视化(零计算,纯出图):**覆盖图谱(运动×市场日
   ×成交热图)、价差宽度对比、假设状态板、数据质量 QC 摘要、
   repair-01~07 时间线。deep01 数据不重算,只读绘图。
4. **组装:**以上 + FULL_REPORT 的文字结论,合成
   `docs/research_reports/SPORTS_AUTORESEARCH_VISUAL_REPORT_01.docx`
   (或 pdf),EXPLORATORY + PRIOR_EXPOSED 横幅置于封面。
   明确标注:本报告是信号地图,不含任何可交易结论;判决版
   (含 20 日样本与 unseen 考试)另行由 deep03 产出。

## 8. 完成标准

本 run 只能以以下结果之一关闭:
- EXPLORATORY 信号地图完成(含 §5 全部主线与扫描的落盘结果);
- 所有方向 COLLECT_MORE/DATA_STARVED;
- 发现新的核心数据完整性问题(按条款 0.2 上限内处置);
- 达到费用/时间上限。

**不授权实盘、报单或微实盘。**

---

## 附录 A:已推翻假设登记表(deep01 审计结论,继任者禁止重新采信)

以下认知在 deep01 期间被证据推翻。执行中若发现自己正基于其中任何一条
行动,立即停止并按刹车话术上报。

| # | 被推翻的假设 | 推翻证据 | 对应计划条款 |
|---|---|---|---|
| A1 | "6 releases ≈ 6 个独立日期" | 库存盘点:07-11 隔离,07-12/13 各取一个 canonical;合格日期=2 | §4 日期盘点纪律 |
| A2 | "重发布可修复坏数据行" | 坏行 SHA 与封存清单一致 ⇒ 采集期写入 | §4 入册体检;采集债另案 |
| A3 | "无限加固无害" | repair-04/05/06 治理递归,全天延误主因 | §0 五条刹车;§5.D 豁免 |
| A4 | "全局窗口去重可在 61GB 机器上完成" | repair-06 实测 42.8GiB OOM | §7 强制分桶 |
| A5 | "RFQ 可短期修通" | 两次 OOM + 回放缠绕;边际价值为零(数据本不足) | §5 RFQ 排除 |
| A6 | "repair 档案双机齐备" | repair-04 仅存 W09 | §7-bis F-1 回传令 |
| A7 | "W09 控制面状态已确认" | 执行 session 自认未直读 EC2 State.Name | §1 身份/boot 预检 |

## 可直接发给执行 agent 的授权文本

> 批准启动 SPORTS-AUTORESEARCH-02,按本文档
> (`docs/SPORTS_AUTORESEARCH_02_AUTHORIZATION_2026-07-15.md`)全文执行,
> §0 五条收敛条款为最高优先级约束。以 SPORTS-AUTORESEARCH-01 的
> L1、L2、Market Atlas 结果为不可变已观察基线,在 W09 上建全新 RUN_ID。
> 不得续写或覆写旧 run 20260715T112538Z__c21a79a8cff__deep01。
> 第一步仅执行身份/权限/新增 release 盘点预检;每个纳入的 release 先过
> 逐行结构体检再入册;每个日期只选一个 canonical release,禁止合并
> variants。本轮为 EXPLORATORY 档:合格日期若仍仅 07-12/13,则不重跑
> 三主线已有假设(deep01 结果不可变、重跑零新信息),执行网球分侧
> markout、跨市场失调、单边补位三项必做扫描,并按 §5.D-bis 开放勘探
> 授权自主增加描述性分析;全部产出打
> EXPLORATORY + PRIOR_EXPOSED 横幅,不产生任何晋级;若发现 07-13 后
> 新封存合格日期,先报操作员裁决其用途再动。
> RFQ 维持 DATA_INTEGRITY_BLOCKED,禁止 repair-08 与
> W-SAR-RFQ-REPLAY-01。费用上限 $5 / 计算时长上限 6 小时,触线收尾。
> 全程不得授权或执行实盘。结束时提交代码和报告、更新 SESSION_LOG、
> 同步 docs 镜像并停止 W09。收到后先逐条复述 §0 与本授权,再执行。
