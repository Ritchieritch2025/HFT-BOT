# PLAN_SPORTS_TRADING_DECISIONS — 体育交易计划裁决台账

操作员裁决的权威记录(E2:一条裁决 = 一个条目,逐字保存,新条目在上)。
战略权威文档 = `PLAN_SPORTS_TRADING_MASTER`(Phase 1 建,尚不存在);在它
建立之前,本台账是体育策略方向唯一的已裁决事实来源。工程/基建权威仍是
`docs/MASTER_SEQUENCE.md`(不再承载策略排序)。

---

## D-1 · 2026-07-10 — 对 AUDIT_ZERO_BASE_2026-07-10.md 的正式裁决(操作员原文,逐字)

> 操作员裁决(2026-07-10,对 AUDIT_ZERO_BASE_2026-07-10.md 的正式回应,Phase 1 记入 PLAN_SPORTS_TRADING_DECISIONS.md):
>
> 审计整体:部分采纳。 诊断层(S1 证据、模拟器缺陷、引擎未接线、L2 不足、burstiness 错标)采纳;策略处方修改后采纳:主线 = 赛前市场动力学价差捕获(CANONICAL PROMPT §5),外部赔率锚定降为 Track C、RFQ 为 Track B(只读先行)、约束扫描为 Track A——不预选 MLB,选品由 Phase 6 数据决定。
> 宪法与权威: GUARDRAILS Q1/Q2/Q7/H1 不直接重写,按 CANONICAL PROMPT §11 进 OPERATOR-TBD 提案等我逐项批;不建 CURRENT_AUTHORITY.md,战略权威 = PLAN_SPORTS_TRADING_MASTER(Phase 1 建),MASTER_SEQUENCE 保留工程/基建权威,不再承载策略排序。
> 外部工具:全部暂缓。 第一个 pilot 不依赖外部数据;OpticOdds/Pinnacle/Betfair/Sportradar/MM Program 每项到需要时单独找我批;Betfair 在核实我的开户资格合法性之前不得列入任何计划。
> 审计中"暂停既有数值门"的建议驳回:保守默认值在被功效分析替换前继续有效,标注 provisional。

### D-1 归档注记(非裁决原文)

- 裁决对象:`docs/plan_audits/AUDIT_ZERO_BASE_2026-07-10.md`(commit
  92919db 归档的零基审计)。该文件状态自本裁决起 = **已裁决(部分采纳)**,
  以本条目为准。
- 生效要点(执行会话速查):
  1. 采纳 = 审计诊断层:S1 负 edge 证据、模拟器缺陷清单、tradingd 未接线、
     L2 覆盖不足、burstiness 错标(已在 8d57be0 以 erratum 修正)。
  2. 主线 = **赛前市场动力学价差捕获**;Track A = 约束扫描、Track B = RFQ
     (只读先行)、Track C = 外部赔率锚定。**不预选 MLB**,选品等 Phase 6
     数据。
  3. GUARDRAILS Q1/Q2/Q7/H1:不直接重写;走 OPERATOR-TBD 提案,逐项批。
  4. 不建 CURRENT_AUTHORITY.md。
  5. 外部工具全部暂缓,逐项单批;Betfair 在操作员开户资格合法性核实前
     不得入任何计划。
  6. 既有数值门(7 clean days、5/7、25% 集中度、×1.5/×2 等)**继续有效**,
     标 provisional,直到功效分析给出替代值。
- **未决依赖(红旗)**:裁决引用的 CANONICAL PROMPT(§5 主线定义、§11
  OPERATOR-TBD 机制)**尚未入库**——目前只存在于 Cowork 对话。按全文保存
  规则,Phase 1 开工前必须把 CANONICAL PROMPT 全文逐字存入 docs/
  (建议 docs/CANONICAL_PROMPT_SPORTS_TRADING.md);在此之前任何会话
  不得凭记忆或转述解读 §5/§11 的细节。
- Phase 1(建 PLAN_SPORTS_TRADING_MASTER)由后续专场执行,本条目仅落盘
  裁决本身。
