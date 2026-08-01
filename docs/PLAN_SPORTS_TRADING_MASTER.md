# PLAN_SPORTS_TRADING_MASTER — 体育交易主线路线图（策略权威·唯一通路图）

**状态：ACTIVE（策略权威文档，Phase 1 交付物，CLAUDE.md 长期缺件"to be built"于此落地）**
**日期：2026-07-22**
**一句话用途：把散落在 6+ 份局部地图里的信息，收敛成一条"从今天到微实盘"的单一通路，每一站都有精确状态，让操作员不再觉得项目是"浆糊"。**

---

## 0. 非干预声明（NON-INTERFERENCE — 本文档最重要的一行，独立审计核心）

> **本文档只是策略权威的规划（planning-only）。它不授权任何事、不改动任何 GUARDRAILS / MASTER_SEQUENCE 门、不触碰任何正在运行的生产/研究进程。每一个执行步骤仍需各自的 W 工程号 + 独立审计 + 操作员按既有仪式的授权。**

具体地，本文档 **不做** 以下任何一件事（第 6 节再列一次，此处先钉死）：

- 不改 24/7 数据管道（`com.ritcardo.kalshi-pipeline` 采集/入库/封存）的任何一行代码或配置；
- 不改 W09 研究运行时（deep03 runner）的任何状态、预算或授权；
- 不改任何 GUARDRAILS 硬门（S1-S6、悲观口径、Q1/Q2/Q7/H1、shadow 5 绿日）；
- 不改 `MASTER_SEQUENCE.md` 的工程排序权（那仍是工程/基建的唯一权威）；
- 不新增任何 authorization、不冻结任何参数、不解除任何 blocker。

**它是路线图（route map），不是执行令（execution order）。** 读它的人得到的是"我们在哪、卡在哪、下一步是谁的活"，不是"去动手"。任何"动手"都要回到既有仪式（一个 fresh session 一个 W → 独立审计 → 操作员授权）。

**它统一，不取代。** 本文档不废止 MM_ROADMAP、PLAN_MM_TEST_PROGRAM、MASTER_SEQUENCE、deep03 计划中任何一份——它把它们摆到同一条时间线上（见第 5 节 crosswalk），让操作员一眼看到它们是同一条路的不同刻度。各文档在各自领域仍是权威。

---

## 1. 权威边界（避免与既有权威冲突）

- **策略权威 = 本文档**（PLAN_SPORTS_TRADING_MASTER）。在它建立之前，唯一已裁决的策略事实在 `docs/PLAN_SPORTS_TRADING_DECISIONS.md`（D-1..D-5）。本文档继承那些裁决，不改写它们。
- **工程/基建排序权 = `docs/MASTER_SEQUENCE.md`**（STEP 0-6，W-A…/W-P…/W-K…）。本文档引用其步骤，绝不复制或覆盖其排序。
- **计划文档生死状态板 = `docs/PLANS_LEDGER.md`**。本文档在该板登记一行（状态 ACTIVE）。
- **主线策略（D-1，2026-07-10 操作员裁决）**：赛前市场动力学价差捕获；Track A=约束扫描、Track B=RFQ（只读先行）、Track C=外部赔率锚定；不预选 MLB；外部工具全部暂缓、逐项单批。
- **正本提示词（D-2，2026-07-11）**：V2.2，release-pinned，sha256 `575ea27a…`。
- **研究终点要求（D-5，2026-07-17）**：deep03 做完必须产出**实盘可测的可执行规格**，或**量化的 no-go**（不允许为满足指令放宽门槛挑好看结果）。

---

## 2. 单一通路（今天 → 微实盘）——十站

**裁决（一行）：⚠️ 十站里第 1 站已完成、第 3 站在自动累积、第 2 站被卡死（L2 采集坏钟未修，每天在流血），其余七站尚未开工。真正的瓶颈是第 2/3/4 站三件工程债，不是策略想法不够。**

每一站固定字段：**[阶段名] · [是什么/目标] · [状态] · [并行/串行] · [技术根因/依赖] · [对应 W 号或 plan doc] · [卡在谁/什么] · [退出条件]**。
状态图例：✅ 完成 · 🔄 进行中 · ⬜ 未开工 · ❌ 受阻/冻结。

### ⚠️ 两轨执行说明（不许把并行画成串行，否则白等一个月）

**站号 1→10 是逻辑顺序，不是执行顺序。** 站 3（≥20 封存日）是日历约束、催不动——**唯一不浪费这段等待期的办法，是同时开跑不依赖它的引擎轨**。两条轨：

- **数据轨（串行·卡日历）**：站 2 → 站 3 → 站 6 → 站 7。这条被 B14 和 20 天日历卡死，只能等。
- **引擎轨（并行·等待期就做）**：站 4（引擎接线）+ 站 5.5（成交真实性）+ 站 8（影子引擎）——**这些不依赖 20 天数据，现在就能建**，等数据轨到位时引擎正好就绪，直接进站 5 冻结 → 站 6 验证。

**读图纪律：谁把引擎轨（站 4/5.5/8）排到站 3 之后顺读，就是犯了这张图要治的病。** 每站标题下的 `[并行/串行]` 字段是硬约束。

---

### 站 1 · 数据基础 DATA FOUNDATION · ✅ 完成

- **[轨]**：数据轨基座 · 已完成（承重墙，永不降级）
- **是什么**：24/7 三层数据管道（raw firehose → staging.duckdb → 每日 zstd 归档），全市场 L1 + trades，分类系统，坏行双重校验。
- **状态**：✅ 已完成（MM_ROADMAP 阶段 0，2026-07-06 起）；生产已在 EC2（2026-07-09 cutover）。
- **技术根因/依赖**：launchd `com.ritcardo.kalshi-pipeline` 常驻；封存链异步、只认 `seals/date=D.json` + verify PASS。
- **W 号/plan**：MASTER_SEQUENCE STEP 0-1（W-A0…A5 迁移已完）；MM_ROADMAP 阶段 0。
- **卡在谁**：无（这一层是全项目唯一稳固的地基）。
- **退出条件**：已满足——连续封存、采集全程未停、零真实数据丢失（2026-07-14 事故后确认）。

---

### 站 2 · L2 深度数据修复 L2 DEPTH REPAIR · ❌ 受阻（瓶颈 a）

- **[轨]**：`串行·卡日历` — 数据轨起点；B14 未修则站 3 时钟不真正走
- **是什么**：让盘口深度（L2 orderbook_delta）数据干净可用。**这是全部做市信号（C1 深度回补、C2 价差捕获）的原料。**
- **状态**：❌ 受阻 / 冻结。**8 个封存日里只有 3 天 L2 干净（07-12 / 07-15 / 07-17）**；07-13/14/16 因数据质量被排除，07-10/11 根本没抓 L2（来源：`Deepresearch V3/CANDIDATES.md` 数据口径行）。
- **技术根因/依赖**：**B14 = 采集端 ws_shadow 给 L2 帧盖了垃圾 `recv_mono_ns`（~2e19..2e24），溢出 INT64 崩 FULL/TRADE_INSERT**（来源：SESSION_LOG 2026-07-14 事故 context capsule）。入库侧已兜住（置空坏值），但**采集端未修 = 坏数据还会继续来**。叠加 W06 Stage-2（targeted L2 spec）被卡：Stage-1 硬门 a/b/c 已获批，但 **B11（封存导出 vs ingest 写锁竞争）必须先修，否则 W06 Stage-2 不能上**。
- **W 号/plan**：**B14 → `PIPE_DEBT_PAYDOWN_PLAN` 的 W-B（采集坏钟）**；W06 Stage-2 → PIPE-W06 spec（Stage-1 a/b/c 已批，Stage-2 待 B11）。工程排序在 MASTER_SEQUENCE / PIPE 债务计划，不在本文档。
- **卡在谁**：工程队列——B14 采集修复 + B11 写锁破环 + W06 Stage-2。这些都是 MASTER_SEQUENCE / PIPE 债务计划的活，需各自 W + 审计。
- **退出条件（可证伪，不许写"接近全覆盖"这种模糊话）**：
  连续 **N 个封存日**，其 L2 帧的 `recv_mono_ns` **全部落在合法区间**（单调递增、无 INT64 溢出、无 ~2e19..2e24 垃圾值），且**坏帧率 < X%**，且期间**零次 FULL/TRADE_INSERT 崩溃**，且这 N 天的 L2 能直接进 deep03 economics 而不触发质量剔除。
  **N、X 的具体值由工程线（build）出候选、操作员拍板**（本主线图不擅自定阈值）。检验必须是可核数字（帧计数 + 区间校验），不是"我改好了"。

---

### 站 3 · ≥20 封存日验证样本 SEALED-DATE COHORT · 🔄 进行中（瓶颈 b）

- **[轨]**：`串行·卡日历` — 全项目唯一催不动的日历瓶颈；等待期必须并行跑引擎轨
- **是什么**：把干净的封存日堆到 ≥20 个独立合格日 / ≥200 个 root，才够做统计上站得住的确认（confirmation）。
- **状态**：🔄 自动累积中，但**被站 2 拖慢**——L2 层每天只有在 B14 修好后才算数；目前 L2 干净日只有 3 天，离 20 还差 ~17 天。
- **技术根因/依赖**：deep03 沿用最保守解释——**≥20 独立合格日 + ≥200 相关 root**（来源：DEEP03 计划 C-03 / §"deep02 handoff"；provisional 门，操作员未释放替代值前有效）。样本靠管道每天自动产。
- **W 号/plan**：无独立 W——是站 1 管道 + 站 2 修复的时间函数；门定义在 deep03 计划（Layer E 的 `D3-E-POWER`：frozen K、day-blocked power）。
- **卡在谁**：时间 + 站 2。**只要 B14 一天不修，L2 的 20 天时钟就一天不真正走**——这是"每天在流血"的那条（见第 3 节 severity）。
- **退出条件（日期写成依赖的函数，不是拍的日历）**：
  > **验证就绪日 = B14 修复落地日 + 20 个干净封存日**（W06 Stage-2 扩面影响的是覆盖面/root 数，另计）
  即：**不存在"月底到位"这种日历承诺。** 干净 L2 的 20 天时钟从 B14 落地那天才起算。
  **后果量化：B14 今天修 vs 一周后修，验证就绪日就差整整一周，微实盘也顺延一周。** 现有 3 个干净 L2 日**不能**计入这 20 天（它们在 B14 前采集，坏钟风险未除）——严格口径下时钟尚未起步。
  具体门：≥20 个 B14 后的干净独立封存日、≥200 root，且过 `D3-A03-COVERAGE`。

---

### 站 4 · 引擎接线 ENGINE WIRING · ⬜ 未开工（瓶颈 c）

- **[轨]**：`并行·等待期可做` ★ — 不依赖 20 天数据，现在就建；合成/离线部分不碰未封存生产行
- **是什么**：把"能算费后、逐笔、root-event PnL、能重放下单生命周期"的引擎接起来。三块缺件：**① mm_backtest 的结算 PnL（settlement PnL）② tradingd 执行接线 ③ deep03 Layer D 可执行重放（executable replay）**。
- **状态**：⬜ 未开工 / 部分退役。旧 `mm_backtest.py` **仅作诊断，W-FS1 前不得作 go/no-go**（来源：MM_ROADMAP 阶段 1 + PLAN_MM_TEST_PROGRAM §0.3）；**tradingd 未接线**（D-1 采纳的诊断层事实；`CURRENT_ENGINEERING_STATE` 退役参考清单）；Layer D 是纯设计，尚未实现。
- **技术根因/依赖**：现有回测器没有有状态成交模拟器（stateful fill simulator）、没有订单 FSM、没有费用账本重放，所以任何"费后正期望"结论都不可信。deep03 Layer D（`D3-D01-REPLAY`…`D3-D10-TERMINAL`）+ PLAN_MM_TEST_PROGRAM E 组（`FS-1`…`FS-5`）定义了要补的合同。
- **W 号/plan**：**W-FS1**（PLAN_MM_TEST_PROGRAM E 组有状态成交模拟器，G3 门，W-C4 硬前置）；deep03 **Layer D**（`D3-D*`）；tradingd 接线走 MASTER_SEQUENCE 的 World A/B merge / shadow wiring（2026-07-10 resequencing ruling）。
- **卡在谁**：工程——W-FS1 未建；tradingd 接线排在 K1..K5 之后（RESEQUENCING RULING）。合成/离线部分可先做，但不得借未封存的生产行。
- **退出条件**：W-FS1 三轨成交（strict/queue/optimistic）+ 订单 FSM + 费用账本三处可对账（Layer D `D3-D07-FEELEDGER`）;确定性重放两遍同 hash（`D3-D01`）;可在同一底盘接 backtest / shadow / live（PLAN_MM_TEST_PROGRAM F3 同流三模式）。

---

### 站 5 · 候选冻结 TRAIN-FREEZE · ⬜ 未开工（瓶颈 d / 候选注册）

- **[轨]**：`串行` — 需站 4 引擎 + 站 5.5 成交真实性都就绪后才冻结
- **是什么**：从 OPEN_DISCOVERY 的存活信号里，**冻结一个策略**（一次只冻一个）成可执行规格——进出场规则、挂单价位、撤退条件、每单规模、kill 条件、全部参数冻结值、悲观费后经济性档案（D-5 实盘就绪条款）。
- **状态**：⬜ 未开工。**第一顺位候选 = C1「深度回补做市」refill-timed requoting**（来源：`Deepresearch V3/CANDIDATES.md`，TOP PICKS 第 1）。目前 C1 只是 EXPLORATORY 描述，不是冻结策略——CANDIDATES.md 全篇 `candidate_or_profit_claim=False`。
- **技术根因/依赖**：C1 的信号在 3 个干净日方向/量级一致（首 100ms 回补 24–31%、1s 累计 50–57%，`refill_hazard`，n=27–31 万事件/日），是全篇最稳的；**但 C1 测的是「显示深度回补」，不是「我自己的挂单能不能成交」——这道缺口是独立的一关（站 5.5），不是脚注**。冻结前必须先过站 5.5。
- **W 号/plan**：deep03 funnel 的 **TRAIN 阶段**（`[TRAIN-FREEZE]`，Layer E `D3-E-PMTS`）；PLAN_MM_TEST_PROGRAM F 组（F1 edge screen、F2 calibration、F4 样本外硬门）。
- **卡在谁**：站 4（引擎）+ 站 5.5（成交真实性）；且 ONE-POLICY-PER-OPENING——默认一次只冻一个策略供一次 validation。
- **退出条件**：C1 的可执行规格 + 全参数冻结 + 悲观口径费后经济性档案落盘，且所有值只在嵌套 chronological TRAIN 内选定（不偷看 validation/confirmation）。

---

### 站 5.5 · 显示深度 → 真实成交验证 DISPLAYED-DEPTH → REAL-FILL · ⬜ 未开工（C1 的命门，独立成关）

- **[轨]**：`并行·等待期可做` ★ — 依赖站 4 引擎，不依赖 20 天数据；C1 命门
- **是什么**：把 C1 的信号从「显示深度回补」升级为「我自己的挂单在队列里能不能、多快成交」。**这是一道独立的硬关，专门防止 C1 拿着一个「不是它看起来那个意思」的数字被提拔上去。**
- **技术根因/依赖**：`refill_hazard` 量的是盘口显示深度恢复，**不含队列位置、不含被穿价才成交的口径**。真实成交取决于：我的单排在队列第几位、对手单是否穿过我方报价、撤单延迟。这三样在 C1 的探索数据里**完全没有**。
- **W 号/plan**：受 `agents/audit/ACCEPTANCE_影子做市引擎_成交真实性.md` 的 10 条门管辖（尤其成交真实性 + 第 10 条热路径纪律）；实现依赖站 4 的有状态成交模拟器（W-FS1 三轨 strict/queue/optimistic）。
- **卡在谁**：站 4（没有成交模拟器就证不了成交）+ 执行层 agent 线（影子做市引擎）。
- **过关条件（可证伪）**：在有状态成交模拟器上，C1 的「悲观口径费后 root-event PnL」（strict/queue 轨，被穿价才算成交）**> 0 且在 3 个干净日符号一致**；且实测「自有挂单成交率」与 `refill_hazard` 的显示回补率**差距被量化并纳入 spec**（即证明我们知道"显示"和"成交"差多少，而不是假设相等）。任一不满足 = C1 不得进站 5 冻结。

---

### 站 6 · 样本外验证 VALIDATION · ⬜ 未开工

- **[轨]**：`串行·卡日历` — 需站 3（20天）+ 站 5（冻结）都到位
- **是什么**：对**站 5 冻结的那一个策略**做一次样本外验证——只接受/拒绝，不做任何 feature/cell/model 选择。
- **状态**：⬜ 未开工（依赖站 5 冻结完成）。
- **技术根因/依赖**：deep03 规则——VALIDATION 只对冻结策略 accept/reject，通过只能产出 `EDGE_CANDIDATE_FOR_CONFIRMATION`，绝不能在 validation 上选特征/格子（DEEP03 §"ONE-POLICY-PER-OPENING"）。
- **W 号/plan**：deep03 funnel **VALIDATION**；PLAN_MM_TEST_PROGRAM **F4 样本外利润硬门**（在打开 test 前冻结）。
- **卡在谁**：站 5。
- **退出条件**：冻结策略在 sealed VALIDATION 上通过悲观费后 root-event PnL，产出 `EDGE_CANDIDATE_FOR_CONFIRMATION`。

---

### 站 7 · 不变策略历史确认 HISTORICAL_CONFIRMATION · ⬜ 未开工

- **[轨]**：`串行·卡日历` — 需更多独立封存日
- **是什么**：同一个**不变的**策略，在单独授权的 release 下、在 ≥20 封存日样本上做最终历史确认。
- **状态**：⬜ 未开工（依赖站 3 样本 + 站 6 通过）。
- **技术根因/依赖**：确认队列在 TRAIN freeze 后不再改；K 与 card-level Holm 先冻（多重比较控制）；`BACKTEST_CANDIDATE` 只有在 `CONFIRMATION_COMPLETE` 后才合法（DEEP03 §1.2）。
- **W 号/plan**：deep03 funnel **HISTORICAL_CONFIRMATION**（Layer E `D3-E-POWER` 冻结 K、day-blocked power、attrition/zero-fill/rare-tail）。
- **卡在谁**：站 3（≥20 天样本）+ 站 6。
- **退出条件**：不变策略在 ≥20 封存日、≥200 root 上确认，所有 hash + `RUN_COMPLETE`（`D3-E-RECEIPT`）;产出可交操作员的完整 backtest dossier。

---

### 站 8 · 影子验证 SHADOW · ⬜ 未开工

- **[轨]**：`并行·等待期可做` ★ — 影子引擎可提前建（受成交真实性 10 条门管辖）
- **是什么**：`KALSHI_MODE=shadow`——实时行情驱动、决策全记录、**零下单**；影子成交判定与回测同悲观口径。
- **状态**：⬜ 未开工（MM_ROADMAP 阶段 2）。
- **技术根因/依赖**：需站 4 的同流底盘（同一热循环接 backtest/shadow/live，不按 mode 分支，PLAN_MM_TEST_PROGRAM F3）。
- **W 号/plan**：MM_ROADMAP 阶段 2；PLAN_MM_TEST_PROGRAM **G 组**（SH-1 决策一致性、SH-2 零下单证明、SH-3 连续 5 绿日）。
- **卡在谁**：站 4（引擎）+ 站 7（要有确认过的策略才值得影子）。
- **退出条件（GUARDRAILS 硬门，不松动）**：影子连续 **5 个交易日正 PnL**，最大回撤 < 单日均利 3 倍（SH-3）。

---

### 站 9 · 上线安全门 GO-LIVE GATES · 🔄 部分就绪

- **[轨]**：`并行·可提前备` — kill switch / 风控闸部分已就绪，可提前补齐
- **是什么**：live 前必须全绿的一组安全闸——风险闸、kill switch、reconcile-on-ambiguity、token 预算、账户注资、操作员书面确认。
- **状态**：🔄 部分就绪。kill switch / 五层 reserve-before-send / 风控规则引擎在 PLAN_RISK_KILLSWITCH 有设计（W-K1..K5 排在 RESEQUENCING RULING 里），部分已建；**账户注资未做**（微实盘至少需 $50-100，MM_ROADMAP 阶段 3；余额由 Telegram `/balance` 实时查，不入 memory）；W-K6 kill-switch rehearsal（第一笔真实订单）**由操作员排期**。
- **技术根因/依赖**：D-1.1 账户共用护栏（归属隔离、资金真话、同市场互斥、风控边界诚实）必须进 P08/P10 设计。
- **W 号/plan**：MM_ROADMAP 阶段 3；PLAN_MM_TEST_PROGRAM **H 组**（RK-1..RK-4）+ **I 组**（I1 = W-K6 rehearsal）；MASTER_SEQUENCE W-K1..K6。
- **卡在谁**：工程（W-K 系列）+ **操作员**（注资 + W-K6 排期 + 每次开 live 书面确认，S1）。
- **退出条件**：lifecycle_check 的 live_execution 门全绿 + 操作员本人显式确认。

---

### 站 10 · 微实盘 MICRO-LIVE · ⬜ 未开工（终点）

- **[轨]**：`串行` — 终点，需前序全绿 + 操作员逐次授权
- **是什么**：post-only 限价单、单市场、1-5 张、总风险 ≤ $20；每日复盘实际成交率 vs 影子预测、滑点、费用。
- **状态**：⬜ 未开工（MM_ROADMAP 阶段 4）。
- **技术根因/依赖**：所有前站 + 站 9 全门通过；D-1.1 = 系统只认自己台账里的 order_id，external（手动）成交永不进校准样本。
- **W 号/plan**：MM_ROADMAP 阶段 4；PLAN_MM_TEST_PROGRAM **I 组**（I2 maker action/latency probes）；deep03 `BACKTEST_CANDIDATE`→ 操作员最终选择。
- **卡在谁**：**操作员**（每次开 live 亲自确认，S1，永不代办）。
- **退出条件（升级）**：2 周正 PnL 且实际成交率 ≥ 影子预测 70%。**回退**：单日亏损 > $10 或行为异常 → kill switch + 回站 8。

---

## 3. 四大瓶颈表（哪一条在每天流血）

**裁决（一行）：真正堵住"从研究到钱"的是三件工程债 + 一个尚未冻结的候选。其中只有瓶颈 a（L2 采集坏钟 B14）在每天持续流血——它没修一天，≥20 天 L2 样本时钟就白走一天。**

| 瓶颈 | 为什么它挡住钱 | 精确技术根因 | 哪个 W 修 | 严重度 |
|---|---|---|---|---|
| **a · L2 数据薄** | 所有做市信号（C1/C2）的原料就是 L2 深度；没有干净 L2，验证样本堆不起来 | **B14**：采集端 ws_shadow 给 L2 帧盖垃圾 `recv_mono_ns`（~2e19..2e24）溢出 INT64 崩入库；入库侧只兜住不治本。叠加 **W06 Stage-2** 被 B11（导出↔ingest 写锁竞争）卡住 | B14 → `PIPE_DEBT_PAYDOWN_PLAN` **W-B**；W06 Stage-2 → PIPE-W06 spec（待 B11） | 🔴 **每天流血**——8 封存日只 3 天 L2 干净，未修则 20 天时钟不真正走 |
| **b · ≥20 封存日门** | 统计确认要 ≥20 独立合格日 / ≥200 root，样本不够就没有站得住的 go/no-go | provisional 门（deep03 C-03，操作员未释放替代值前有效）；样本靠管道每天自动产，但 L2 维度受瓶颈 a 拖累 | 无独立 W（时间函数）；门定义在 deep03 `D3-E-POWER` | 🟡 时间瓶颈，随 a 一起松动 |
| **c · 引擎未接线** | 没有有状态成交模拟器 + 订单 FSM + 费用账本，任何"费后正期望"都不可信 | 旧 `mm_backtest.py` 只能上下界诊断（W-FS1 前不得 go/no-go）；**tradingd 未接线**；deep03 **Layer D** 纯设计未实现 | **W-FS1**（PLAN_MM_TEST_PROGRAM E 组）；deep03 **Layer D**（`D3-D01..D10`）；tradingd 接线走 World A/B merge | 🟠 硬阻——不修则站 5/6/7 全部不可信，但不是每天新增损失 |
| **d · 候选未冻结** | 有信号 ≠ 有可上实盘的策略；D-5 要求实盘可测的可执行规格或量化 no-go | **C1** 深度回补做市是第一顺位存活信号（refill 首 100ms 24–31%、1s 50–57%，n 27–31 万/日，3 天符号一致），但只是描述，"显示深度回补 ≠ 自有挂单成交"，冻结前要先证成成交 | deep03 **TRAIN-FREEZE**（`D3-E-PMTS`）；PLAN_MM_TEST_PROGRAM F 组 | 🟠 依赖 c；机会本身不流血（信号不会因等待而消失，但也未验证费后为正） |

---

## 4. 候选注册表（来自 `Deepresearch V3/CANDIDATES.md`）

**裁决（一行）：2 个信号活下来、1 个勉强、3 个被否；唯一够格排第一进验证门的是 C1。全部 EXPLORATORY_ONLY，无任何盈利结论。**

| ID | 名称（机制） | 判定 | 一行签名（来源：CANDIDATES.md） | 进哪一站 |
|---|---|---|---|---|
| **C1** | 深度回补做市 refill-timed requoting（机制2） | ✅ 第一顺位 | 被吃穿后 1s 内显示深度回补 50–57%、头 100ms 24–31%，3 个干净日方向与量级全一致（`refill_hazard`，n=27–31 万事件/日） | **站 5（TRAIN-FREEZE 第一个）** |
| **C2** | 高流动性价差捕获 cross-sectional spread capture（机制1） | ✅ 有条件（费后待定） | 1s half-spread(0.07–0.10) 普遍大于逆选 markout(0.025–0.05)，36/37 sport 格毛边缘为正（B01，n=2,290 万笔/1s）；毛口径，deep01 已判纯做市费后为负 | 站 5（第二，带费后闸门） |
| **C3** | 赛前转瞬单边 fade（机制3 窄口径） | ⚠️ 勉强（薄） | Tennis 赛前单边 p50=1.0s、p95=31s；只在赛前窄口径成立，样本 6 天 | 备选——证明 C1/C2 且样本堆到 20+ 天后再碰 |
| **R1** | 广义 fade 单边失衡（机制3） | ❌ 否 | 大票单边 p50=8–11s、p95 多为删失上限 60s，站对面会被 run over（证据反向） | 不进门 |
| **R2** | 深度蒸发抢跑（机制4） | ❌ 暂缓（交付物估不出） | 事件抽出 85.3 万，但差分 outcome 表缺失、配对覆盖仅 1.6–1.9%、深度未配平（SMD −0.31…−0.40） | 等验证门补 outcome 表 + 20+ 天 |
| **R3** | 跨市场残差/合成套利（机制外 B03） | ❌ 否 | `NOT_ESTIMABLE`；28,080 候选无一过 payout 互斥 + 费后多腿成交门 | 不进门 |

---

## 5. Crosswalk：本文档如何统一既有各图（不取代）

**裁决（一行）：下表证明本文档是把 4 份既有权威文档摆到同一条十站时间线上，而非另起炉灶。各文档在各自领域仍是权威。**

| 既有文档 / 刻度 | 它在本通路的哪一站 |
|---|---|
| **MM_ROADMAP 阶段 0**（数据基础） | 站 1 |
| **MM_ROADMAP 阶段 1 / 1.5**（研究工具、动态定价模型） | 站 4（引擎）+ 站 5（候选冻结的定价内核） |
| **MM_ROADMAP 阶段 2**（策略实现 + 影子） | 站 8 |
| **MM_ROADMAP 阶段 3**（上线安全门） | 站 9 |
| **MM_ROADMAP 阶段 4 / 5**（微实盘 / 放量） | 站 10（放量在站 10 之后，本图不展开） |
| **PLAN_MM_TEST_PROGRAM A 组**（G1 数据/时钟） | 站 1–2 |
| **PLAN_MM_TEST_PROGRAM B/C/D 组**（G2/G3 反应/延迟/动作） | 站 4 |
| **PLAN_MM_TEST_PROGRAM E 组 = W-FS1**（G3 成交模拟器） | 站 4 |
| **PLAN_MM_TEST_PROGRAM F 组**（F1 edge screen / F2 calib / F4 样本外硬门） | 站 5–6 |
| **PLAN_MM_TEST_PROGRAM G 组**（SH-1/2/3 影子） | 站 8 |
| **PLAN_MM_TEST_PROGRAM H/I 组**（RK 风控 / I1=W-K6 / I2 probes） | 站 9–10 |
| **MASTER_SEQUENCE STEP 0-1**（WS 修复 + AWS 迁移） | 站 1 |
| **MASTER_SEQUENCE STEP 4-5 + PIPE 债务**（深度扩展 / backfill / B14 / W06） | 站 2 |
| **MASTER_SEQUENCE STEP 6 + World A/B merge + W-K1..K5**（定价骨架 / 引擎接线 / kill switch） | 站 4、站 9 |
| **deep03 Layer A**（authority/input/clock 合同） | 站 1–2（数据可采性） |
| **deep03 Layer B**（修复重跑一代分析） | 站 3（描述性证据基座） |
| **deep03 Layer C**（strategy PnL 前的机制测试） | 站 3–5（定义合格宇宙 + 机制存活判定） |
| **deep03 Layer D**（可执行重放资格） | 站 4 |
| **deep03 Layer E**（policy/inference/delivery，含 TRAIN/VALIDATION/POWER/RECEIPT） | 站 5–7 |
| **deep03 funnel OPEN_DISCOVERY**（产出 CANDIDATES.md） | 已发生——喂给站 5 |
| **DATA_COMPLETENESS_ROADMAP**（数据完整度门：18 类 universe→atlas→深度验证、封存缺口检查） | 站 1（承重墙，✅ 维持）+ 站 2（L2 覆盖面）+ 站 3（≥20 封存日的完整性核查 `D3-A03-COVERAGE`） |
| **PLANS_LEDGER**（计划状态板） | 不是一道门，是元文档——继续作为各计划生死状态的登记处，本主线图已在其中登记 ACTIVE |

**「本次丢弃」项（显式列出，不静默消失）：** 经逐份核对，5 份旧图中**没有任何一道门被丢弃**——全部映射进上表某一站。若后续审计发现遗漏的门，按规则1 必须补映射或补"丢弃+理由"，不允许静默消失。（承重墙类门——capture / seal / S3 upload——一律不得标为噪音/降级/丢弃，见第 0 节规则0。）

---

## 6. 本文档明确"不做"什么（非干预声明重述）

再钉一次（独立审计以本节 + 第 0 节为准）：

1. **不改门**：不改任何 GUARDRAILS 硬门、不改 provisional 数值门、不冻结/解冻任何参数、不释放任何 phase（STP-P00/BOOTSTRAP-0 仍 BLOCKED，与 D-2 一致）。
2. **不写代码**：本文档只是 `.md`，不含、不触发、不要求任何代码或配置改动。
3. **不碰生产/研究进程**：不动 24/7 管道（采集/入库/封存）、不动 W09 研究运行时、不动 EC2 任何服务、不动 launchd。
4. **不夺权**：MASTER_SEQUENCE 仍是工程排序唯一权威；本文档只把工程步骤映射到时间线，一个 W 都不重排。
5. **不新增授权**：任何一站的推进仍需各自的 W + 独立审计 + 操作员按既有仪式授权；任何花钱/删数据/开 live 仍是操作员决定。
6. **不产出策略结论**：候选注册表（第 4 节）逐字来自 CANDIDATES.md 的 EXPLORATORY_ONLY 证据，本文档不新增任何盈利主张，也不把 C1 从"描述"升格为"策略"。

**一句话：读完本文档，正确的下一步是"知道路在哪、该找谁排哪个 W"，而不是"去动手"。**

---

*登记：本文档在 `docs/PLANS_LEDGER.md` 登记状态 ACTIVE。应在 `plan-sports-market-dynamics-v2` 分支（策略计划文档所在分支）提交，并按会话退出仪式镜像到 docs-mirror。提交与镜像由审计后的父会话执行。*
