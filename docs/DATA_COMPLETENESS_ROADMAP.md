# DATA COMPLETENESS ROADMAP — 数据完整性配齐计划

Operator-requested 2026-07-08 ("要把完整性全部配齐"). This file SEQUENCES the
four missing pieces; ordering authority remains MASTER_SEQUENCE. Each piece
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
  同窗口执行（同为订阅面变更，一次审计覆盖两个变更面）。

---

## 时间线（挂在 AWS 迁移主线上）

```
现在        ── 拼图① 升舱 W（Mac，独立会话，今天可做）
            ── 拼图② 计划起草（纸面，可与其他纸面活同会话）
W-A0..A4    ── AWS 迁移执行（数据完整性线暂停，只有①在积累新数据）
W-A4 完成   ── 拼图② 执行：settlements 爬取 → 校准研究解锁
            ── 拼图③ STEP 4 深度 rollout（EC2）
            ── 拼图④ W-LC 随窗执行
全部就位后  ── 数据资产终态：全类别×全深度×结算真相×生命周期×历史回填
```

## 完成判据（每块的"配齐"标准）

1. ① 升舱：全类别当日归档含 L1 行数 > 0 且 3 天回灌入账（loader 报告佐证）。
2. ② 回填：settlements 覆盖率 = Kalshi 全部已结算市场的 100%（对照目录数）；
   开机前 trades 至操作员选定的起始日期；全部行带 rest_backfill。
3. ③ 深度：全市场 orderbook_delta 在采集，ws_seq 连续性检查绿。
4. ④ W-LC：determined 帧实时入库，事件包索引含状态时间戳。

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
