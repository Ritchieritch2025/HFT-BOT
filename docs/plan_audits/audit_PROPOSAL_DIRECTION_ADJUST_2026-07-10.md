# 审计:PROPOSAL_DIRECTION_ADJUST_2026-07-09(周密性 + agent 可执行性)

- 审计日期:2026-07-10。审计对象:提案 v1.0(commit 9b541c3)。
- 方法:逐条对照 GUARDRAILS §6 checklist + MASTER_SEQUENCE + 仓库实测
  (warehouse 数据存量、tools/ 现状、七字段 W 模板)。
- 结论:⚠️ **方向合格、骨架合格,不违反任何 MUST;但 v1.0 不可直接
  交付执行**——9 findings(2 HIGH / 3 MED / 4 LOW),已全部在提案
  v1.1 附录中修复。

## 通过项(有证据)

- P1/P2:声明推进阶段(1.5A 锚替换),闸门全保留,不越 Phase 2/3。✅
- Q1/Q2/Q3:log-odds、悲观口径、费用模型显式保留。✅
- S1-S6:范围内无实盘。✅
- 资产复用声明属实:实测 Sports 类 21 个 subcategory 已在采集
  (Aussie_Rules…Tennis),trades/L1 按 category 分区,taker_side 在列。✅
- mm_scan/mm_backtest/mm_calibrate/mm_research 四工具实存于 tools/。✅

## Findings

**F1 (HIGH) 数据面事实过时。** v1.0 称"数据现成、零采购"。实测:
2026-07-09 23:09 UTC cutover 后 EC2 为唯一 owner;Mac 本地
archive_root=work/warehouse/facts 仅存 2026-07-06..08 三天;新数据在
EC2 + S3。且 seven-clean-days gate = 2026-07-13 未到。
修复:W-S1 数据面 = EC2 上执行(16GB 盒子,分块处理)或 S3 拉取;
三天数据仅做初筛,校准级结论等 gate;遵守 D6(单写者、读者重试)。

**F2 (HIGH) W-S1 缺七字段任务卡**(repo 惯例:Purpose/Allowed reads/
Allowed writes/Forbidden writes/Acceptance/Risks/Rollback,Acceptance
须可运行演示,P3)。v1.0 为散文,毒性窗口、通过线、产出路径、写清单
全部缺失。修复:v1.1 附录 A 给出完整任务卡。

**F3 (MED) 与 MASTER_SEQUENCE 的排序机制缺失。** MASTER_SEQUENCE 为
FINAL ordering,修改须操作员批准的 amendment。修复:W-S1 定性为
纯读研究(P8 精神),与 STEP 2+ 并行、不占序列;W-S2 起(feed 采购/
接入 = 新基础设施)必须以 amendment 插入 MASTER_SEQUENCE。

**F4 (MED) 交接指令违反 one-W-per-session。** 计划变更 W(纸面)与
W-S1(研究代码)不可同 session(P9 仅允许纸面 W 合并)。
修复:拆两段交接引文(v1.1 附录 B)。

**F5 (MED) Q7 冲突未登记。** GUARDRAILS Q7 将 MVE/combo 防御性排除
于 MM 候选;访谈证据最强的收入端(RFQ combos)正是 MVE 类。
远期走 RFQ 须正式修订 Q7(宪法修改:操作员批准、单独 commit、
附 rationale)。现阶段不动;登记为显式冲突,防止未来 session 无感
违宪或无谓绕路。W-S1 阶段 Q7 照常生效(单一市场做市不受影响)。

**F6 (LOW) W-S2 缺评估标准。** 修复:延迟实测法(同一事件 feed 时戳
vs Kalshi 盘口反应,本地时钟)、覆盖率(Kalshi sports 市场匹配率)、
价格质量(与盘口中价偏离分布)、ToS/法务检查、月成本上限占位,
五项进 v1.1。

**F7 (LOW) 世界杯时段偏差未标注。** 当前体育样本被 World Cup 扭曲:
既是代表性风险(结论标注时段),也是机会(散户流极端样本,恰好
检验 H7/H10 的上界)。进 W-S1 任务卡 Risks。

**F8 (LOW) 缺 rollback story(P3)。** 修复:W-S1 若显示体育品类在
悲观口径下不优于 crypto(毒性过高/价差过薄),决策点回到操作员,
方向回退成本 ≈ 0(crypto 管道未停)。

**F9 (LOW) 缺受影响文档清单(E5)。** 修复:批准后须同步
MM_ROADMAP(1.5A 锚、阶段指针)、GUARDRAILS 头部 current-phase 行
(如措辞涉及 crypto)、OPERATOR_PLAYBOOK 相应条目;列于 v1.1。

## 审计意见

v1.1 修复后:**可执行**。执行顺序:①操作员批准 → ② session A
(纸面:MM_ROADMAP 修订 + MASTER_SEQUENCE amendment 草案 + 合并审计,
P9)→ ③ session B(W-S1,按附录 A 任务卡)→ ④ W-S1 数字出来后
操作员裁决品类权重与 feed 预算(W-S2)。
