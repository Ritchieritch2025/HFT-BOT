# PLAN_SPORTS_TRADING_DECISIONS — 体育交易计划裁决台账

操作员裁决的权威记录(E2:一条裁决 = 一个条目,逐字保存,新条目在上)。
战略权威文档 = `PLAN_SPORTS_TRADING_MASTER`(Phase 1 建,尚不存在);在它
建立之前,本台账是体育策略方向唯一的已裁决事实来源。工程/基建权威仍是
`docs/MASTER_SEQUENCE.md`(不再承载策略排序)。

---

## D-2 · 2026-07-11 — V2.2 canonicalize:唯一正本提示词确立(操作员释放令,原文)

> OPERATOR RELEASE STP-R002-CANONICALIZE-V22 (single session, no phase execution):
>
> Verify prerequisites, stop on any failure: V2.2 candidate SHA-256 = 575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54; an archived independent audit report for V2.2 with verdict PASS exists in docs/plan_audits/ — if it exists only in a chat transcript, stop and ask the operator to paste it for verbatim archival first.
> Promote V2.2 to CANONICAL per its own release schema (exact path + SHA recorded; V2/V2.1 stay frozen historical candidates).
> Record ruling D-2 in docs/PLAN_SPORTS_TRADING_DECISIONS.md: V2.2 = sole canonical prompt; D-1's §-references remapped to V2.2 sections (record exact numbers); program branch = plan-sports-market-dynamics-v2 @ base 1c93837; merge-to-main deferred to a future release.
> This release does NOT authorize BOOTSTRAP-0/P00 or any phase. Save this instruction verbatim to docs/plan_releases/sports_trading_program/STP-R002-CANONICALIZE-V22.md, full exit ritual, report hashes, stop.

### D-2 归档注记(非裁决原文)

- **canonical 正本(release 采纳,两道门齐)**:
  `active_prompt_path` = `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md`
  `active_prompt_sha256` = `575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54`
  门① 独立审计 PASS:`docs/plan_audits/AUDIT_PROMPT_V2_2_2026-07-11.md`
  (SHA-256 `d6794bf54b7375b40b1caad82e563eb33a2385a85bb32f8ec244f7f46f6c6164`,
  零上下文独立代理,零 P0 零 P1);门② 本释放令(operator_text_sha256
  `e0a91f791a2e979a50ce9b1a77258beac41393e624e7e2d1feb559686df95fa3`)。
- **横幅声明(审计 P2-1 要求)**:正本文件内的首行横幅
  "STATUS: CANDIDATE — NOT CANONICAL — NOT EXECUTABLE" 与文件名中的
  `_CANDIDATE` 是**创建时元数据**,自本 release 起被上述
  `active_prompt_path`/`active_prompt_sha256` pinning **取代**;后续会话以
  本条目与 STP-R002 收据为准,不得因横幅字样拒认正本。字节不改(任何
  字节/路径改动 = 新候选,须重新独立审计)。
- **D-1 §-引用重映射(操作员令记录精确编号;依据 = V2.2 审计 P1-2 核验)**:
  D-1 中 "CANONICAL PROMPT §5"(主线定义)→ **V2.2 §2(主假设/主线)+
  §11.2–§11.4(选品宇宙与 Tracks A/B/C)**;D-1 中 "CANONICAL PROMPT §11"
  (OPERATOR-TBD 机制)→ **V2.2 §4(冲突规则)+ §10(GUARDRAILS
  OPERATOR-TBD 提案机制)**。
- **program branch** = `plan-sports-market-dynamics-v2` @ base `1c93837`;
  **merge-to-main 延后**至将来单独 release。
- **D-1 红旗解除**:D-1 归档注记中"CANONICAL PROMPT 尚未入库"的未决依赖
  自本条目起**解除**——正本已在上述 path/SHA(D-1 条目本身按 append-only
  规则不改动,以本条为准)。
- **本 release 未授权**:BOOTSTRAP-0、STP-P00(W01/AUD01)、任何 phase。
  W01 前硬前置:独立审计通过的 §31.2 test-isolation artifact(未证明隔离
  ⇒ STP-P00 = BLOCKED,先走独立 engineering W)。V2/V2.1 保持冻结历史
  候选(SHA `bc2fbf65…f341` / `e83160bd…106c`)。
- 执行收据:`docs/plan_releases/sports_trading_program/STP-R002-CANONICALIZE-V22.md`
  (evidence/closure commit 哈希在收据内登记)。

## D-1.1 · 2026-07-11 — 账户共用与自动化边界(操作员裁决,原文)

> 不用只留给系统 但是后面我们做策略尽量100%自动 但是也给手动留空间

### 归档注记(非裁决原文)

背景:2026-07-11 对账确认账户存在操作员本人的手动交易(07-02 起,
可见净额 −$4,256.08,含当日两笔 ~$3,000 级亏损;逐分对账残差 $0)。
裁决 = 系统与手动共用同一 Kalshi 账户;策略执行以 100% 自动为目标;
保留手动空间。

执行护栏(工程实现要求,进 P08/P10 设计):
1. **归属隔离**:系统只认自己订单台账里的 order_id;凡不在台账中的
   成交/持仓一律标记 external(手动),永不进入系统的成交率/滑点/
   校准样本(P10 校准数据纯净性)。
2. **资金真话**:reserve-before-send 永远按交易所实时余额计算——手动
   占用的保证金自动挤压系统可用额度,系统缺钱就不下单(fail-closed,
   已有设计,无需新代码)。
3. **同市场互斥**:系统正在报价的市场若检测到 external 成交,自动
   暂停该市场报价 + 告警(防止手动与系统在同一薄里互相干扰、
   自成交与库存归因混乱)。
4. **风控边界诚实**:系统的日亏/仓位上限只约束系统自身的单;手动
   交易不受系统风控保护——此点在每次 P10+ 释放单里重申。

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
