# 代码库完成度地图 — 2026-07-17(全库三路并行勘查汇总)

状态:评估报告(非计划、非裁决)。三个零上下文侦察代理并行勘查
C++ 交易核心 / Python 研究栈 / 测试覆盖,本文为汇总;每条结论均有
file:line 出处(细节在勘查原文,本文取关键项)。

## 一行裁决

⚠️ 这台"赚钱机器"的完成度**远高于此前外部审计的描述**:绝大多数关键
零件已造好且带硬测试,真正缺失的只有 5 件;主要矛盾不是"造零件",
而是"零件全在仓库里没组装"。

## 三层解剖

### 数据层 — 🟢 生产级(全库最成熟)

ingest(字节断点续传)/export_day(写一次+清单校验)/warehouse.load()
统一读口/gold 层(FSM+合并+验证+隔离)/seal-verify 链 — 全部 WORKING
且测试最密。约 20 个 pytest 文件覆盖。

### 研究/定价层 — 🟡 骨架完整,数字未校准,闭环未连

- 定价数学(tools/pricing/lo.py, fair.py, quote.py):log-odds 核心、
  公平价估计、Avellaneda-Stoikov 报价生成——数学与不变量已测试冻结,
  但**每个系数都是 NAMED PLACEHOLDER**,等 mm_calibrate 校准值回填。
- mm_calibrate.py 已能算出这些校准值,**但没有任何代码消费其输出**。
- **悲观成交模拟器已存在**:tools/mm_backtest.py(331 行)= W-FS1 要的
  东西——同报乐观/悲观双界(悲观 = 只有穿过我价位的成交才算 fill)、
  recv-clock 默认+防前视测试(test_backtest_clock.py)。
  但:①maker fee 硬编码 0;②**不模拟结算**(敞口按最后中价计,自认
  "makers often win/lose here");③模拟的是固定 join-the-touch 规则,
  **没有驱动 pricing/quote.py 模型**。
- 研究工具(maker_edge_pilot、autoresearch_cycle、rfq_flow_report、
  mm_scan、mm_research)全部 WORKING,但全部刻意止步于"产生假设",
  无晋级/裁决闭环(设计如此)。
- sandbox/deep_autoresearch:冻结的 Cycle-1 深度假设检验框架,
  7 个 repair 预注册重冻结记录,测试齐;自述不能产生 verdict。

### 执行层 — 🟡 管道生产级,安全件"造好未接线",4 件真缺

Stack A(实际能发单的路径):tradingd → REST L1 轮询 → 策略 → 环 →
签名发送。Stack B(数据引擎):WS 全深度 + OrderBookManager + 影子
执行引擎。两栈**互不相连**。

已是生产级:签名(RSA-PSS)、热通道、无锁环、令牌桶限速、TTL/新鲜度
闸、orders_enabled 影子闸(fail-closed)、确定性 client_order_id、
E4 定点、panic 杀开关 CLI(独立进程,cancel-all→verify→清仓轮→报告,
默认 dry-run)。

**造好未接线(BUILT-UNWIRED,只差接线):**
1. RiskLedger(include/kalshi/risk_ledger.hpp)— 5 层 reserve-before-
   send 原子预留,410 行测试含 4 个命名事故重放;**没有任何 app include**。
2. RuleEngine(include/kalshi/rule_engine.hpp)— 断线撤全单、dead-man
   过期、日亏熔断→QuoteStop;同样无人 include。
3. post_only — wire.hpp 支持,tradingd 调用时用默认 false
   (tradingd.cpp:294);改一个实参的事。
4. L2 订单簿→策略 — OrderBookManager 生产级在跑(ws_shadow),但
   tradingd 的行情源仍是 L1 REST 轮询,两者事件类型未桥接
   (Q5:REST 轮询上的 maker = 宪法 reject,此桥必须建)。
5. 盘口不可交易守卫(gateway.cpp:176)— 只在未接线的 Stack B 里。

**真缺失(MISSING,要新造):**
1. **真策略本体** — roster 只有 once_probe,默认空。
2. **C++ 费用模块** — 全库 C++ 无费用计算(fixedpoint.hpp:12 明令
   PriceE4 不得用于费用)。
3. **实盘订单状态机/对账** — tradingd 发后即忘(:330-347);
   reconcile 原语在 request_executor 有但未接;Python 侧
   test_reconcile.py 已定义好语义(ORDER_ONLY_AT_EXCHANGE 等)。
4. **结算感知 PnL + 结算数据** — 采集端不采结算/生命周期帧,回测
   无法算真实结局;**这是策略验证链最大的洞,且是唯一"越晚开始
   积累越吃亏"的项**。
5. C++ 回放/成交模拟引擎(Python 版已有,C++ 同内核版属 F3 同流要求)。

### 测试层 — 🟢 结构健康

82 注册测试全离线(72 pure + 10 offline),零网络依赖,无 skip/xfail;
风控/对账/费用/定价符号测试扎实;薄弱点 = 策略层(只有探针)与
成交模拟(1 个文件);实弹发射路径唯一未覆盖(故意未实现,测试
断言其 fail-closed 抛错)。

## 对既有认知的三处纠偏

1. 此前外部审计称"没接 RiskLedger/OrderManager/reconcile"——半对:
   RiskLedger/RuleEngine **不是没有,是造好没接**;订单状态机才是真缺。
2. "W-FS1 悲观模拟器待建"——实际 mm_backtest.py 已是其雏形,缺的是
   费用/结算/模型驱动三块补强,不是从零。
3. 测试覆盖远好于"收据不可信"印象所暗示——收据登记是假的(已修),
   测试本体是真的。

## 最短装配顺序(研究与工程两线并行)

1. **研究线(即刻)**:deep03 三族对比(等操作员三按钮:补发布 A/B、
   holdout 机制、费率 commit)。
2. **验证线(数日)**:mm_calibrate 回填 pricing 系数 → pricing/quote.py
   接进 mm_backtest 悲观轨 → maker fee 接 kalshi_facts →
   **结算数据采集立项(尽早,积累不可压缩)**。
3. **执行线(数日,与上并行)**:tradingd 接 WS/L2 桥 + RiskLedger +
   RuleEngine + post_only=true + 新写订单状态机(唯一大件新码)。
4. **合流**:候选冻结 → 同内核影子 → 5 绿日+故障演练 → 操作员微实盘
   决策(2-3 周现实估计不变)。

## 其他

- sandbox/ 内有 4 个整库克隆(source-seal-hash-binding、w05-recovery-fix、
  wa-dev 等)属半成品/交接件,择日清理;w05-recovery-handoff 的 27KB
  patch 是待交付物。
- 生产可重建性缺口(7 个 untracked 部署文件 + 未锁定 venv)见
  docs/plan_audits/DETECTION_RUN_2026-07-17.md。
