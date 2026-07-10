# 热路径下单引擎 · 设计思路存档(2026-07-10)

- 性质:**设计输入笔记,不是计划**——无 W 定义,不排队执行。
  操作员指示存档("先起一个文档把我们的思路加进去")。
- 去向:STEP 6 起草 PLAN_PRICING_MODEL / PLAN_RISK_KILLSWITCH 及
  未来执行引擎计划时,本文档九条契约与排序纪律**原文收编**。
- 来源:操作员 × 多 agent 讨论(2026-07-10),含另一会话对 repo
  既有骨架的核对(文件行号引用见 §3)。

## 1. 现状定位(诚实版)

当前 AWS 栈胜任:连续采集、数据落盘、S3 金库、calibration /
backtest / shadow。**它还不是低延迟实盘执行栈**,缺:

- Kalshi PrivateLink / 多 region-AZ 延迟 bakeoff(放量后再买的票)
- signed POST RTT p99 实测、order signing(RSA-PSS)p99 实测
  (W-TL1 已留占位参数,测量任务待开,方案不碰真单、先呈操作员)
- hot path 与 research/export 的进程级分离
- live order kill switch / dead-man / order group 管理(S3/S6,
  这是实盘的前置门,不是优化项)

一句话:现在是"稳定采集 + 数据金库 + 守夜系统";先把时间轴、
延迟、回测钟、shadow gate 校准好,再升级网络与下单链路。

## 2. 操作员意图的正确翻译

诉求原话:"订单数据常驻内存,下单时用指针发给交易所,要快。"
翻译:指针只在自己进程内有意义,交易所收到的只能是 HTTPS 字节流。
真正要建的是**常驻内存订单引擎**:

行情更新内存里的 book → 策略产生 order intent → 热路径进程指向
预建的订单槽位 → 签名器补新 timestamp/签名 → 暖 HTTP lane 发字节。

## 3. Repo 已有骨架(核对过,不是从零开始)

- `include/kalshi/wire.hpp:5` — 既定路径注释:
  `MarketEvent -> ExecPayload -> in-process ring -> submit lanes`
- `include/kalshi/wire.hpp:42` — `ExecPayload` 已是定长 80 字节
  order intent
- `include/kalshi/ring.hpp:3` — 有界无锁进程内队列,push/pop
  无阻塞无分配
- `apps/tradingd.cpp:1` — 架构注释即目标形态:热线程 + submit
  workers + 专用暖 HTTP lanes
- `include/kalshi/client.hpp:144` — per-worker `Lane`,每个
  worker 可持有暖 TLS 连接
- 已知不够极致处:`wire.hpp:163` `order_json()` 每单构造
  `std::string`(安全但非极速;升级排序见 §5)

## 4. 热路径契约(九条,设计定稿输入)

操作员六条:

1. **订单状态常驻内存**:当前仓位、活跃订单、各 market 最佳
   bid/ask、风险额度,策略判断即取即用。
2. **订单模板预建**:market、buy/sell、yes/no、post_only 等固定
   字段提前放好;下单只改价格、数量、client_order_id、timestamp。
3. **固定内存槽位**:预分配一批 OrderSlot,不做临时对象;策略
   拿槽位填关键字段。
4. **热连接常开**:每个下单 worker 自有 warm HTTP/TLS lane,
   绝不临下单才建连接。
5. **签名不可省**:Kalshi 每请求需新 timestamp + RSA-PSS 签名,
   每单必做;可并行、预分配 buffer、减少额外分配。
6. **Redis 不入热路径**:日志/telemetry/cold path 可用;真正
   下单路径全程同进程内存。

补充三条(缺一即经典实盘事故源):

7. **额度是预留式,不是查询式(reserve-before-send,S6)**:
   策略取槽位那一刻即从额度扣除本笔敞口 → 发送 → ack 按实结算,
   失败/超时归还。只"看一眼"额度 = 同毫秒两笔各自看到余额双双
   发出 = Q8 multi-fill 窗口 bug 族(访谈实例:单腿限额 $5k
   被相关腿同时打成 $50k)。
8. **内存是缓存,交易所是真相——必有对账回路(S2)**:冷路径
   定期拉交易所 resting orders/positions 与内存对表;ack 丢失、
   部分成交漏收、断线窗口都会造成漂移;不一致 → 报警 + 以
   交易所为准,永不盲目重试。
9. **client_order_id 承担幂等**:重试必须复用同一 id(换 id
   重发 = 可能双成交);生成规则(进程内单调、含槽位号)在
   设计里写死。

## 5. 排序纪律(与 GUARDRAILS 对齐,防跑偏)

- **安全层先于速度层**:kill switch 先建先练才许第一笔实盘
  (S3);post-only、dead-man、下单前三查(仓位/敞口/日亏)、
  reserve-before-send 是下单路径的准入条件(S6),不是 v2 功能。
- **先测量再优化**:延迟大头是签名(数百 µs 量级)+ signed POST
  RTT(ms 量级);OrderSlot 池省的是 ns-µs。签名与 RTT p99 实测
  先行(W-TL1 占位参数的后续任务),数字出来后工程往大头砸。
- **第一版 edge 不靠极速**(访谈档案结论):maker 吃费率墙 +
  散户流,防快人靠报价熔断与 size 控制;PrivateLink/多 AZ 等
  放量且被延迟真实咬过之后再投。
- 遥测/日志/S3/dashboard 永远滚出热路径(S5/E7)。

## 6. 悬置事项(不在本文档解决)

- signing p99 / signed POST RTT p99 的安全采样方案(不碰真单)
  → 单独提交操作员过目后执行。
- OrderSlot 池 + order_json() 免分配化的具体实现 → STEP 6
  执行引擎计划内定义 W。
- strategy_seen_ns / book_applied_ns 热路径打戳(时间轴第 4 级)
  → STEP 6,BACKLOG 已记。
