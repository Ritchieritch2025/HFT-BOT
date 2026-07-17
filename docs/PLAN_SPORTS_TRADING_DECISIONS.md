# PLAN_SPORTS_TRADING_DECISIONS — 体育交易计划裁决台账

操作员裁决的权威记录(E2:一条裁决 = 一个条目,逐字保存,新条目在上)。
战略权威文档 = `PLAN_SPORTS_TRADING_MASTER`(Phase 1 建,尚不存在);在它
建立之前,本台账是体育策略方向唯一的已裁决事实来源。工程/基建权威仍是
`docs/MASTER_SEQUENCE.md`(不再承载策略排序)。

---

## D-5 · 2026-07-17 — deep03 交付物要求:研究终点必须产出实盘可测形态(操作员指令,原文)

> 要想尽各个角度来创造提炼策略,这份research做完之后一定要有能够上实盘测试的现金策略

### D-5 归档注记(非裁决原文;执行侧对指令的诚实操作化,操作员可否决)

- **广度条款(想尽各个角度):**deep03 除既定三族深测外,增加一个
  预注册的"全角度描述性扫描"层(机制族 × 运动 × 盘口结构 × 时段),
  确保没有机制族未被看过;本轮深测不到的角度进入下一轮候选登记簿,
  不允许"没想到"式遗漏,只允许"看过并记录淘汰理由"式排除。
- **实盘就绪条款:**任何活到 deep03 终点的候选,交付形态必须是
  **可执行规格**(进出场规则、挂单价位与撤退条件、每单规模、kill
  条件、全部参数冻结值、悲观口径的费后经济性档案),而非研究笔记——
  即通过后可直接进入影子/微实盘工程化,无需二次翻译。
- **诚实边界(执行侧明示,不可妥协):**研究可以保证"搜索的广度"和
  "幸存者的实盘就绪度",**不能保证市场里一定存在正期望策略**。若全部
  候选被证据淘汰,交付物 = 每个候选的量化死因 + 下一轮最有希望的
  搜索方向;**禁止为满足本指令而放宽预注册门槛或改口径挑好看结果**
  (那样产出的"策略"上实盘就是送钱)。"上实盘测试" 仍指按既定门序:
  候选冻结 → 影子 → 操作员批准的微实盘探针;S1/Q2 等宪法门不因本
  指令松动。
- **落实路径:**本条目待 deep03 大纲(DEEP03_GRANULAR_STRATEGY_TEST_
  PLAN_CANDIDATE_2026-07-17.md,sha 9a1b70f2…)独立审计返回后,由作者
  会话把广度条款与实盘就绪条款并入修订稿,一并再审。

## D-4 · 2026-07-16 — OQ-1 费率 ratify:fees.verified 翻转为 true(操作员委托裁决,原文)

> 你去自己解决吧

(语境:会话代理报告"7%/1.75% 常数只剩官方 PDF 一处来源、PDF 被反爬关卡挡住、
需要一次真人浏览器核对(OQ-1 原要求)",并列出替代取证方案后,操作员以上句
明确委托代理完成核对与翻转。)

### D-4 归档注记(非裁决原文)

- **裁决内容:**`config/kalshi_facts.yaml` 的 `fees.verified` 由 false 翻转为
  true;OQ-1(WP-05 审计的费率取证悬案)以多来源证据链方式 ratify,替代
  原定的"单次真人浏览器查看 PDF"。
- **证据链(全部 2026-07-16 取证,细节在 kalshi_facts.yaml 注释块):**
  ① archive.org 对 kalshi.com/fee-schedule 网页 2026-05-08 快照:标准市场
  倍率 ×1,100 张合约费用区间 $0.07–$1.75(恰为 0.07·P·(1−P) 的值域),
  且"无已排期费率变更";② 搜索引擎对现行 PDF(标题 "Fee Schedule for
  July 2026 - 7.7.26 Update")的索引:预测市场公式不变
  (round_up(0.07·C·P·(1−P)),上限 $1.75/100,无结算费),7.7.26 的实质变更
  = 永续合约(另一产品线)梯度费率 + 非标准倍率表刷新;③ 两份独立 2026
  指南(pm.wiki 04-21、marketmath.io 07-16)一致给出 taker 7%、maker 1.75%
  (= taker 的 25%);④ 实时 API:GET /series/fee_changes = 空(无排期变更),
  KXBTCD=quadratic、KXNBA/KXATPMATCH/KXWTAMATCH=quadratic_with_maker_fees、
  KXATP=quadratic,倍率均 ×1;⑤ 实时官方文档 fee_rounding.md:舍入制度
  与档案一致。
- **残余风险(明示并接受):**现行 7.7.26 PDF 正文本身未读到(反爬 429;
  archive.org 爬虫自 2026-03 起也被挡,最后完整 PDF 快照停在 2026-02-18)。
  缓解 = Q3 规则本就禁止假设 ×1/quadratic:每系列 fee_type/fee_multiplier
  必须走 API/catalog 实时查询,该路径今日实测有效。若未来任何来源显示
  常数变动,立即翻回 false(fail-closed 不变)。
- **哨兵触发与下游义务:**tests/test_research_metrics.py 与
  tests/test_gate_metrics.py 中钉死"仓库 yaml=false"的哨兵断言按设计触发,
  已随本裁决更新为钉死 true;**WP-07/WP-09 依赖旧 false 状态的产物标记
  rerun due**(哨兵原报警文案的本意),下次触碰相关研究时执行。
- **顺带发现(与费率相关,供后续研究引用):**CFTC 备案
  rules0501262787.pdf(2026-05-01)记录了一个 rebate 计划,其适用范围
  **排除 Sports 类别**(细节未展开核读,仅存指针)——即体育市场的费用
  护城河(maker 0 费 vs taker 全费)不受该计划稀释,方向上有利主线。
- **本条目效力:**解除 DATA_LAYER_OVERVIEW_2026-07-16.md 问题清单 #1
  (费率闸门);deep_autoresearch F02 的 DATA_STARVED_OQ1_FEE_RATIFICATION
  重开条件之一自此满足。

## D-3 · 2026-07-12 — Visualize-Everything 全报告证据规则(操作员裁决,原文)

> Operator standing ruling (doctrine ledger, upgrade to Visualize-Everything): NO scalar statistic may be reported alone. Every reported statistic ships with its full distribution plot (histogram or ECDF) marked with p50/p99/max and n. Inherently-scalar quantities (shares, counts) instead show their distribution across the natural unit (per-hour/per-event) or a bootstrap CI plot. Bimodality, long tails, and discontinuities MUST be named and explained in the caption. Definition block (plain meaning + formula + provenance + code location) and tier banner on every chart. Applies to ALL segments, ALL reports, starting with the 24h observation window and the RFQ 48h report.

### D-3 归档注记(非裁决原文)

- **适用范围:**全部 segment、全部研究/回测/影子/实盘校准报告;首批约束对象
  = 24h observation window 与 RFQ 48h report。任何只报一个数字而没有规定
  分布或 CI 图的产物,不得标记 COMPLETE。
- **连续量:**同页展示 histogram 或 ECDF,并在图中标出 p50、p99、max、n。
- **天然标量:**按自然单位展开成 per-hour/per-event 分布;确实无法展开时展示
  bootstrap CI plot,不得只放 KPI 数字。
- **每图必备:**tier banner;definition block(白话含义、公式、数据 provenance、
  计算代码位置);caption 明说并解释 bimodality、long tail、discontinuity;
  这些形态一旦出现不得沉默或只留给读者猜。
- **统计口径不变:**本裁决改变证据呈现与解释门,不替代 chronological split、
  root-event estimand、day-block bootstrap、pessimistic fill bound 或既有 go/no-go
  门槛。

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
