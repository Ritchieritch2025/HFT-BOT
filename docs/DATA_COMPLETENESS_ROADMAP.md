# DATA COMPLETENESS ROADMAP — 数据完整性配齐计划

Operator-requested 2026-07-08 ("要把完整性全部配齐"). This file SEQUENCES the
five missing pieces; ordering authority remains MASTER_SEQUENCE. Each piece
maps to an existing plan/step — nothing here invents new scope.

**目标状态：全类别 × 全深度 × 带结算真相 × 带生命周期事件 × 历史回填，
provenance 分桶（ws_capture / rest_backfill 永不混）。**

---

## 拼图① 全类别 L1 升舱 — NOW（Mac 上立即，不等 EC2）

- WHY NOW: capture 端本来就全收，是入库策略在丢弃 Class B 订单簿；raw 只留
  3 天 ⇒ 每拖一天，Elections/Entertainment/Mentions 等类别的 L1 永久少一天。
- WHAT: config/market_classes.yaml 全类别 → class_a_full_l1（Exotics 照存，
  Q7 只管 MM 候选排除——研究过滤 ≠ 存储过滤）+ 用 raw 3 天窗口回灌 B 舱 L1。
- DISCIPLINE: 生产入库变更，全 W 纪律（D4 测试同 change、P4 连续性声明）。
- STATUS: operator-approved 2026-07-08；playbook item 3e（paste-ready）。

## 拼图② 历史回填（结算 + 开机前行情）— 计划现在起草，执行在 EC2 切换后

- = MASTER_SEQUENCE **STEP 5**（settlements first）+ 已批准的事件打包 insert。
- WHY THIS ORDER: 结算结果解锁校准研究（价格 vs 真实发生率 = alpha 线入口），
  是全部拼图里研究价值最高的；爬取吃 REST 预算 ⇒ 必须等单一 REST 所有者
  落定在 EC2（W-A4 步骤 4 之后），迁移期间禁止开爬。
- WHAT: docs/PLAN_HISTORICAL_BACKFILL.md（七字段 W）：settlements 全量 →
  开机前 trades/candles 历史；endpoints 对照 vendor spec 验证（no memory-based
  API claims）；read-token 预算上限；provenance=rest_backfill 列；可断点续爬；
  首次真实爬取 operator-gated。
- PAPER NOW: 起草是纯文档（P9 可与其他纸面活合并会话）；执行等 W-A4 完成。

## 拼图③ 全深度 L2（orderbook_delta 全市场）— EC2 上，STEP 4

- = MASTER_SEQUENCE **STEP 4**（PLAN_DEPTH_EXPANSION + 其 probe 数字）。
- WHY AFTER CUTOVER: 深度流量/存储量显著高于 L1（probe 已有实测），Mac 带不动
  也没必要带；W-A0 sizing 已按含深度的目标负载核算。
- WHAT: 动态订阅管理器（独立 shadow-first 进程）+ D4 入库测试同 change +
  capture 连续性声明；operator 明确批准后 rollout。
- UNLOCKS: 队列位置研究（Moallemi）、精细成交模拟——悲观界主路不依赖它，
  但它把回测从"悲观界"升级到"悲观界 + 队列感知"双轨。

## 拼图④ 生命周期事件（开盘/暂停/determined 帧）— W-LC，随 STEP 4 窗口

- = PLAN_EVENT_PACKAGING 的 **W-LC**（operator-gated，已在批准的 insert 里）。
- WHY: determined/settled 帧给每场事件打上精确的状态时间戳——事件时间轴分析
  （赛前/赛中/终局收敛）的骨架；与结算回填互补（W-LC=实时今后，回填=历史）。
- WHAT: 新增 WS 频道订阅 = capture 端变更 ⇒ D4 测试同 change；建议与 STEP 4
  同窗口执行（同为订阅面变更，一次审计覆盖两个变更面）。本拼图明确覆盖
  三个频道：`market_lifecycle_v2`（Market & Event Lifecycle）、
  **Multivariate Market & Event Lifecycle**（MVE 组合盘的同类事件——Q7 排除
  做市但数据照存）、**Communications**（交易所公告/维护通知——维护暂停
  直接影响缺口归因，quality_log 需要它做交叉引用）。
  Multivariate Lookups 频道已被 Kalshi 废弃，明确不接。

## 拼图⑤ CF Benchmarks 指数流（operator 发现 2026-07-08）— 随拼图③同窗执行

- WHAT: WS 频道 `cfbenchmarks_value`（需认证）：Kalshi 加密市场的**结算指数**
  实时值，每秒一跳，含 trailing 60s 均值 + 刻钟收盘前最后一分钟的结算均值
  直播（`last_60s_windowed_average_15min`——KXBTC15M 类市场的结算变量本体）。
- WHY: Crypto 类（第二大类）做市的公允价直接是该指数的函数；临近刻钟收盘，
  市场价必须向成形中的结算均值收敛——可测的定价锚。没有它，Crypto 定价
  模型只能从盘口反推标的；有它，标的真值是官方喂价。
- HOW: 订阅面变更 = capture 端改动 ⇒ D4 测试同 change；先 `indexlist` 发现
  可用指数，初期订 BTC/ETH 相关；存储走独立 channel 标记（provenance 清晰）；
  与拼图③（深度）同窗口一次审计。成本：每指数 ~1 条/秒，可忽略。
- 完成判据: 指数流入库、与对应市场结算价的对账测试绿（结算值 = 指数均值）。

---

## 时间线（挂在 AWS 迁移主线上）

```
现在        ── 拼图① 升舱 W（Mac，独立会话，今天可做）
            ── 拼图② 计划起草（纸面，可与其他纸面活同会话）
W-A0..A4    ── AWS 迁移执行（数据完整性线暂停，只有①在积累新数据）
W-A4 完成   ── 拼图② 执行：settlements 爬取 → 校准研究解锁
            ── 拼图③ STEP 4 深度 rollout（EC2）
            ── 拼图④ W-LC + 拼图⑤ CF 指数流 随窗执行
全部就位后  ── 数据资产终态：全类别×全深度×结算真相×生命周期×历史回填
```

## 完成判据（每块的"配齐"标准）

1. ① 升舱：全类别当日归档含 L1 行数 > 0 且 3 天回灌入账（loader 报告佐证）。
2. ② 回填：settlements 覆盖率 = Kalshi 全部已结算市场的 100%（对照目录数）；
   开机前 trades 至操作员选定的起始日期；全部行带 rest_backfill。
3. ③ 深度：全市场 orderbook_delta 在采集，ws_seq 连续性检查绿。
4. ④ W-LC：determined 帧实时入库，事件包索引含状态时间戳。
5. ⑤ CF 流：指数入库 + 结算对账测试绿（市场结算值 = 指数最后一分钟均值）。

---

## 附：私有频道清单（执行引擎阶段，不属于本数据线——记录在此防遗忘）

`User Fills`（自己的成交回报）· `User Orders`（自己挂单状态）·
`Market Positions`（自己持仓）· `Order Group Updates`（订单组状态）——
四个私有频道是执行引擎与风控对账的原材料（S6：cancel-on-disconnect 验证、
仓位上限核对全靠它们），在执行引擎/kill-switch 计划（STEP 6 之后）起草时
必须全部纳入其 W 定义。数据完整性线不碰它们，但清单在此，无人可忘。

---

## 战略焦点备案（操作员 2026-07-08）

**主攻类别 = Sports**（流动性最强：07-07 单日 1275 万行成交，居全场之首；
且结算反馈以天计——校准样本积累速度全类别最快）。由此的资源倾斜：
深度名单（3g）Sports 占主体；结算回填（拼图②）Sports 优先爬；事件打包
与事件时间轴分析以比赛为第一单元；Crypto 刻钟盘（有 CF 官方锚）为第二
战场。已知数据需求缺口：**比赛开球时间** 不等于 close_time（D2 警告已
钉）——来源待定（lifecycle 事件/市场元数据/外部赛程表），在事件打包
W-E1 中解决并记录来源选择。

---

## 附 2：执行引擎的两条数据质量要求（操作员问答钉定 2026-07-08，STEP 6 起草时必须纳入）

1. **热路径去重环**：策略若消费逐笔成交信号，须带一个近期 trade_id 环形
   集合做 O(1) 去重（重连时刻的重复打印不得进入信号），纳秒级，E7 合规。
2. **回测-实盘对称规则**：回测重放器遇到缺口窗口必须模拟"空仓停手"
   （因为实盘在该窗口会被 fail-closed 停止报价）——缺口在两个世界都
   贡献零 PnL，对比才成立。缺口记录（capture_gaps.csv）即此规则的
   机器可读依据。违反此规则的回测结果无效（Q2 精神）。

---

## 附 3：市场遴选三层框架（操作员问答钉定 2026-07-08，STEP 6 定价/候选计划必须继承）

选品 = 三层各自节奏的评分机，不是静态名单，也不是全做：
1. **系列画像**（周级重估）：每个 series 的客流曲线/典型价差/分阶段毒性/
   结算节奏，由归档+结算数据估计——赛事级别差异沉淀于此（mm_scan 为雏形）。
2. **赛事评分**（每日晨算）：当日每场比赛 = 系列画像 × 场次特征（级别/
   时段/队伍历史客流——后者随自有数据积累启用），产出今日候选名单，
   以事件包为分析单元。
3. **实时雷达**（盘中秒级）：实际客流/价差/毒性征兆达标才上单，恶化即撤
   ——市场动态进出做市名单。2026-07-08 的一小时手动筛（193/18731 活跃、
   TOP12 表）即本层原型；日更版并入 opportunity radar 报告。
边界规则：从三层全绿的头部开始，按系列逐个验证外扩；广度是挣来的终局，
不是初始配置。每层的评分公式与阈值 = 定价计划的 Acceptance 必填项。

---

## 附 4：Polymarket 第二场馆备案（操作员意向 2026-07-08，Kalshi 判决后启动）

- 定位：已验证机器的第二战场 + 解锁跨场馆套利（同一事件两场馆价差，
  文献已记录）。启动前提 = Kalshi 线回测判决 + 影子验证完成。
- 关键差异（移植计划必须覆盖）：链下撮合/链上结算（钱包私钥/gas/USDC，
  S4 需扩展至加密凭证）；UMA 预言机结算 = 新增"结算争议"风险类；
  费用结构不同（Q3 重参数化）；各辖区准入合规由操作员先行核实。
- **现在生效的唯一纪律：场馆隔离**——STEP 6 起所有新代码中，场馆特定
  逻辑（采集 adapter、执行网关、费用表、结算语义）与场馆无关核心
  （log-odds 定价、悲观界回测、选品三层、风控规则）保持接口隔离；
  审计项：核心模块中出现 "kalshi" 字样即为违规。

---

## 附 5：延迟优化分层备案（操作员问答 2026-07-08；仅适用于未来交易盒，Phase 4/STEP 6）

优先级铁律：**盈利判决前不调延迟**（Kalshi 现以毫秒计价，抠微秒不改变
有无 edge；先回测判决→实盘→延迟探针指认瓶颈→再优化）。旋钮按收益排：
- L0 位置（已拿·占>99%）：EC2 同区 us-east-2，30ms→~1ms。
- L1 网络路径：Kalshi PrivateLink（institutional@kalshi.com，流量走 AWS
  骨干不出公网），够institutional资格再谈。
- L2 内核/网卡 config（"通过 config 调延迟"即此层，纯配置不改码）：CPU
  亲和+核隔离(isolcpus)、performance governor 关节能、网卡中断绑核、
  禁超线程、tuned network-latency、大页内存。收益微秒~数十微秒。
- L3 应用热路径（已有）：持久连接、E7 无阻塞、simdjson(281.9ns/条实测)、
  无锁环。
- L4 极端（当前不需要，Kalshi 无 colo 选项）：kernel bypass/DPDK、FPGA。
触发规则：只在 full_chain_latency 探针于实盘指认某段真吃成交时，才拧对应
旋钮。数据机（本次 W-A0 的盒子）不适用——它只采集入库，延迟无意义。

---

## 附 6：一体机 CPU/内存隔离要求（操作员裁定 2026-07-08：交易+数据同机，预算约束）

现阶段单机跑 采集+仓库+回测+交易进程（预算所迫，正确）。机型不需变大——
四负载错峰且交易进程是"低延迟瘦子"，16GB(方案1)放得下。但一体机唯一真
风险 = 批处理抢占交易进程的 CPU ⇒ 报价延迟飙升。W-A1 装机必须做**配置级
硬隔离**（不改机型即可解决，S5 精神在单机上的落地）：
- cgroup/cpuset：交易进程独占 N 个核（isolcpus 候选），gold 构建/采集/ingest
  禁止调度到这些核；
- 交易进程 nice 最高 + gold 构建 `nice`/`MemoryMax` 压在其下（已有 16GB 护栏）；
- ws_shadow `OOMScoreAdjust=-1000`（采集永不被 OOM 杀，S2 fail-closed）；
- 验收：批处理满载时，交易进程的决策环延迟 p99 无可测退化（full_chain
  探针佐证）。
盈利后拆分独立交易盒时，本隔离配置作为过渡桥；届时 latency 附5 的 L2 层
旋钮才上场。
