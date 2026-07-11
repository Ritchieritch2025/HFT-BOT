# PLAN — 做市系统总测试计划（研究 → 影子 → 微实盘）

**状态：PLAN-ONLY，2026-07-10。等待独立审计。** 这份文档把散落在
`PLAN_RESEARCH_CYCLE_1`、`PLAN_PRICING_MODEL`、`PLAN_DEPTH_EXPANSION`、
`PLAN_FULL_MARKET_RESEARCH`、`PLAN_RISK_KILLSWITCH`、`PLAN_LIVE_VALIDATION`
和 `MM_ROADMAP` 里的测试收成
一张总地图。它推进 MM_ROADMAP Phase 1.5→2→3 的验证工作，**不授权任何实盘
订单，也不改变 `MASTER_SEQUENCE` 的工程执行权威**。

冲突时，本文件只在以下三项“测试判决语义”上优先：

1. 30s markout 只衡量价格变化的持续性与库存风险；maker 的 reaction/cancel
   生死线由 10ms–5s 的立即反应测试决定。
2. S5 最多裁决 `edge-candidate / reject / collect`；只有通过成交模拟、未开封
   test、shadow、安全门后，才可进入 operator-gated 微实盘。
3. 旧 `mm_backtest.py` 只能做上下界诊断；未完成 W-FS1 成交模拟器前，任何
   “正收益”都没有 go/no-go 资格。

## 0. 一行裁决与当前基线

**当前裁决：研究管道会算账，但还不会证明“我们的单真的能成交并赚钱”。**

截至 2026-07-10：

- S1 maker-edge pilot 已完成 dev-grade，待独立审计。ITF/Challenger 主指标为负，
  ATP/WTA 样本不足；它成功排除了一个朴素策略，没有发现可上线 alpha。
- Group M（W-P1..P4 定价数学）与 W-K1..K5（离线风控/kill switch/reconcile）
  已完成并审计。
- EC2 已采全18类别 L1，trades/settlements 按全市场保存；但全市场尚无连续、
  完整、经 sequence 校验的 L2 历史。
- `config/backtest_latency.yaml` 仍有未实测参数；撤单/改单的 effective latency
  尚未形成分布。
- 旧成交模拟器没有 cancel-pending 风险、真实 partial fill、校准过的 queue-ahead、
  当前逐系列费用和完整订单状态机，不能做利润裁决。
- maker 策略尚未完成同流 backtest/shadow/live 接线；没有 shadow 5 绿日，更没有
  获准的策略实盘。

## 1. “能赚钱”必须拆成五个乘数

任何报告必须同时展示以下分解，禁止只报一个漂亮的 edge：

```text
每日净收益
= 可报价机会数
× 真实成交概率(queue, size, latency, market state)
× 每次成交后的净 edge(spread - adverse selection - fees)
- 库存/退出成本
- 故障、断线和尾部损失
```

五项里任一项未知，最终结论最多是 `collect`。S1 主要测了第三项；本计划补齐
第一、第二、第四、第五项。

## 2. 总门控：必须按顺序过，不能跳

| Gate | 回答的问题 | 通过后才解锁 | 失败动作 |
|---|---|---|---|
| G0 方法与预注册 | 指标是否在看结果前冻结、可复现？ | 读取研究结论 | 修方法、重新冻结、重跑；旧结果仅留档 |
| G1 数据真伪 | 时钟、L2、trades、状态、结算是否连续可信？ | 毫秒反应与成交研究 | 缺口窗口 fail-closed，继续收数 |
| G2 反应速度 | 市场变坏是否快过我们撤单？ | 设计报价寿命/熔断 | 慢于市场则拒绝该品类/时段或缩小报价 |
| G3 成交模拟 | 排队、部分成交、撤单在途、费用是否被保守模拟？ | 利润回测 | 模拟器不合格则所有 PnL 标 `NON-GATE` |
| G4 样本内候选 | 训练/验证段在悲观口径是否有正 edge？ | 一次性打开 test | `reject` 或 `collect` |
| G5 样本外利润 | 未开封 test 在压力条件下是否仍为正？ | Phase 2 shadow | 退回模型，不准反复看 test 调参 |
| G6 影子一致性 | 同一引擎实时跑是否与回测预测一致？ | 安全 rehearsal 排期 | 回放/影子对不上则修引擎或模型 |
| G7 安全与故障 | kill switch、reserve、reconcile 是否扛住事故？ | operator-gated 微探针 | 任一红灯即禁止 live |
| G8 微实盘校准 | 真实 queue/fill/cancel 是否落在模拟区间？ | 极小策略实盘 | 模拟失配即退回 G3 |
| G9 微实盘盈利 | 两周小规模结果是否可复制且风险受控？ | 每次最多 2× 放量 | 触发回退线即 kill + 回 shadow |

`G4 edge-candidate` 不等于 `trade`；`G9` 之前不使用“印钞机”一词。

## 3. 统一测试产物合同

每个测试必须生成同样的证据包；少一项，自动 FAIL：

1. `PRE_REGISTRATION.md`：假设、唯一主指标、样本窗口、分桶、门槛、失败动作。
2. `manifest.json`：code SHA、完整命令、配置 SHA、输入文件 md5、clock、host、
   venue spec/facts 版本。
3. `results.json`：每个数字同时带单位、n、事件/比赛数、不确定性和剔除计数。
4. `index.html`：自包含交互报告；图前写预注册，探索性图明确 `NON-GATE`。
5. `decision.md`：只能从该测试预先列出的合法结论集中选择。
6. `reproduce.sh` 或注册工具的一条命令；同输入复跑除生成时间外逐位一致。
7. 独立复算：另一实现抽查至少一个核心数；偏差 >5% 必须对账，不能平均掉。

统计通则：

- sports 按整场比赛、其他品类按 event 做 block bootstrap，≥1,000 次。
- train 只选模型和阈值，validation 只决定是否产生候选，test 只开一次。
- 探索桶不能反过来改主指标；发现新假设必须进入下一轮新预注册。
- 每个价格带/tour/时段都报 support；`n<200` 不作主判读。
- 收益使用 E4 定点数、逐笔费用与交易日有效的 fee facts，禁止 float 会计。
- 缺口、时钟不可信、序号断裂、fee unknown 都 fail-closed，不贡献虚构 PnL。

## 4. A 组 — 数据与时钟测试（G1）

### A1 · 数据覆盖与来源对账

**目的：**证明研究没有漏读、重复读或把不同 provenance 混在一起。

**输入：**raw、staging、parquet manifest、catalog、settlement、capture_gaps。

**测试：**

- raw→staging→parquet 按 date/category/channel 逐层对账；抽查差异阈值 ≤1%。
- trade_id 去重；重复、冲突 body、跨重连重复分别计数。
- `ws_capture` 与 `rest_backfill` 分栏，禁止静默合并。
- 每个研究窗口列出未解释 gap；gap 窗口在回测与实盘规则中都等于“不报价”。
- market ticker 必须恰好 join 一个 `dim_segments` 组合；unsegmented ≤2%。

**PASS：**所有差异可解释且落 quality report；无静默截断。否则 `collect`。

### A2 · 双钟与消息排序

**目的：**证明 10ms/50ms 等立即反应不是批量落盘或错误时钟制造的假象。

**必须保存：**`exchange_ts`、`recv_wall_ns`、`recv_mono_ns`、
`book_applied_ns`、`strategy_seen_ns`；跨进程比较只用可比较时钟。

**测试：**

- recv 覆盖率 ≥95%，缺失按市场/频道/日期列出。
- chrony offset、跳钟、负延迟、同 timestamp 批量大小分布。
- 每 sid 的 seq 单调连续；snapshot 先于 delta；重连形成新 epoch。
- 同一原始流重放两次，book state 与事件顺序逐位一致。
- 对 10ms/25ms horizon，数据有效分辨率必须明显小于 horizon；否则该格 NULL，
  不能用 0 填充。

**PASS：**用于 go/no-go 的数据全部 `clock=recv`，排序可复现，无未解释负延迟。

### A3 · L2 全盘完整性

**范围：**研究终态覆盖全部活跃 Kalshi markets 全价位；按
`PLAN_FULL_MARKET_RESEARCH` U5 和 `PLAN_DEPTH_EXPANSION` 50→200→500→实测容量
分波扩展，每波在 category × liquidity 分层，不能只挑已观察到的赢家。原始
snapshot + 每条 delta 全存，再派生 top-5/top-10。未覆盖 L2 的市场仍进入全市场
L1/trades 普查，但不能进入 queue-aware 利润门。

**测试：**

- 每市场每 epoch 恰好由 snapshot 起步，delta 后 book 不出现负数量。
- seq gap、buffer overflow、drop、reconnect、snapshot refresh 全部计数并报警。
- 随机时点用 REST orderbook 交叉核对；不一致记录价位和数量差，不静默修。
- 高峰重放证明 ingest/recorder 不丢消息、不 OOM；磁盘/日与保留窗实测。
- 动态订阅增删市场时，旧市场停止、新市场 snapshot 到达，生产 firehose 不受影响。

**PASS：**连续候选市场窗口无未解释 seq gap；有 gap 的市场/窗口从 queue 研究剔除。

### A4 · 生命周期、暂停与结算

**测试：**开盘、暂停、恢复、determined、settled、取消/退赛逐类 fixture；市场规则
版本和 fee schedule 绑定生效日期；事件包的比赛开始时间不能拿 `close_time` 冒充。

**PASS：**每笔模拟仓位能走到明确结算/取消结果；unknown 状态禁止报价。

### A5 · Tennis 比分/发球状态（独立能力门）

当前没有同步的 point/game/set、发球方和准确 first-serve 到达时刻。因此：

- 不依赖比分的 L1/L2 microstructure、queue、reaction 研究可以继续，但结论必须标
  `market-microstructure-only`，不得解释为“某一分导致价格怎样变化”。
- 任何利用比分预测 fair value、按局间/分间调整报价或比较“我们的体育信息是否
  领先市场”的策略，必须先选定数据源；付费/签约由操作员决定。
- 接入测试必须覆盖 player/match 映射、发球方、point/game/set 顺序、抢七、退赛、
  更正消息、provider event-time 与我们 recv-time、断流与重复；因果 join 只能使用
  当时已经收到的比分，禁止赛后最终比分回填进决策时钟。
- 抽样人工对照比赛录像/官方记分；同步误差按 p50/p95/p99 和错配率报告。

**PASS：**映射和顺序无未解释错误，延迟分布可用，覆盖率达到预注册门；否则体育
预测层关闭，但不阻塞纯盘口 maker 的保守研究。

## 5. B 组 — 市场反应与爆发测试（G2）

### B1 · 原始 inter-arrival 分布

**修正 S1：**必须直接从原始 L1/L2/trade 消息计算，不能从 `markout.parquet`
交易行冒充“book inter-update”。三个频道分别报告，不混合。

按 category × tour × phase × market 分桶，报告：

- inter-arrival p0.1/p1/p5/p10/p50/p90/p95/p99（µs/ms）；
- CV、Fano factor、每 10/25/50/100/250/500ms 消息数；
- burst 的大小、持续时间、burst 间隔与同时受影响的相关市场数；
- recv 时钟与 exchange 时钟并排，只用 recv 做决策。

**解释纪律：**“市场是否比我们快”看下尾部和短窗消息数；p99 inter-arrival
主要表示安静期，不能拿它证明系统追得上。

### B2 · Immediate adverse-selection 曲线（主反应研究）

**anchor：**每次真实/模拟 maker fill。买/卖方向统一成 maker 视角。

**horizon：**10/25/50/100/250/500ms，1/2/5/10/30/120s；对不够分辨率的
毫秒格标 NULL。每个 horizon 报：

- signed mid markout、可执行退出价 markout、spread capture、逐笔 fee、净 edge；
- 第一次不利 book update、第一次不利 mid move、第一次 strict-through trade 的时间；
- CI、n、事件数、价格带/tour/phase/liquidity 分层。

**判决主区：**10ms–5s 判断 toxicity 与撤单窗口；30/120s 只判断漂移是否持续和
库存价值。S1 冻结的 30s 主指标保留，不事后改写。

### B3 · Cancel race

把系统的实测 `cancel_effective` 分布叠到 B1/B2，直接报告：

```text
P(first adverse update < cancel_effective)
P(fill occurs while cancel_pending)
P(two or more adverse updates before cancel_effective)
```

按 p50/p90/p99 cancel latency 和 ×1.5/×2 压力情景计算。若目标品类在 p99
反应预算内高概率已变坏，该品类/时段即使 30s edge 为正也不能过 G2。

## 6. C 组 — 我方延迟秒表（G2/G7）

所有延迟报告至少给 p50/p90/p99/p99.9、超时率、样本数、时段和 host；平均值
只作背景。测量链分开，禁止用一个 REST ping 代替完整下单链：

1. **C1 RSA-PSS：**生产同款 signing path，Mac/EC2 各 ≥10,000 次。
2. **C2 行情热路径：**socket receive→decode→book apply→strategy seen→intent→ring，
   同一引擎全速重放；预热后稳态热路径 allocation=0。
3. **C3 网络只读基线：**同区 authenticated REST/WS 延迟，≥3 时段×100，零副作用。
4. **C4 mock 完整链：**intent→reserve→sign→send→ack/cancel/reconcile，注入网络延迟。
5. **C5 真实 order action：**create/amend/decrease/cancel/batch cancel 分开测 ack 与
   effective 时间；只能在 G7 后、operator-gated tiny probe 中执行。
6. **C6 资源争用：**gold build/ingest/export 满载时重复 C2；决策 p99 不得出现
   统计可辨的退化，否则 CPU/cgroup 隔离 FAIL。

`config/backtest_latency.yaml` 每个值必须是 `MEASURED(date,host,n)`；PLACEHOLDER
存在时回测报告强制 `NON-GATE`。

## 7. D 组 — Maker 可用动作与交易所行为（G3/G7）

先生成一张版本化 action matrix。每个动作逐项记录：API 字段、fee、queue priority
影响、rate token、ack/reject/timeout/ambiguous 状态、模拟器转移、代码是否支持、
文档证据与实测证据。

最少覆盖：GTC/IOC/FOK、expiration、post_only、cancel-on-pause、reduce_only、
self-trade prevention、amend、decrease、batch create/cancel、order group、
queue position、user_orders、user_fills。

测试分两级：

- **D1 离线 contract tests：**用 vendor spec + captured fixtures；缺字段、未知枚举、
  409 幂等、429、5xx、超时、重复回报、乱序回报全部 fail-closed。
- **D2 operator-gated venue probes：**post-only crossing reject、expiration 自灭、
  amend/decrease 后 priority、同 client_order_id 幂等、pause 行为、batch partial
  failure、queue position 单调变化。每个 probe 前后 `assert-zero-resting`。

文档与实测不同，以实测为事实并更新 `kalshi_facts`；在 D2 前不做记忆式 API 假设。

## 8. E 组 — W-FS1 有状态成交模拟器（G3，W-C4 硬前置）

### FS-1 · 唯一状态机

同一状态机服务 backtest、shadow 和 live 观测；至少包含：

```text
desired -> submit_pending -> resting -> partial_fill
        -> cancel_pending -> canceled / filled / rejected / expired
        -> unknown -> reconcile -> terminal
```

旧 quote 在 cancel effective 前继续暴露，禁止 requote 时瞬间消失。

### FS-2 · 成交规则三轨并报

1. **悲观下界（gate）：**strict-through；有缺口/顺序歧义则不记盈利成交，但保留
   已在途订单可能产生的不利成交。
2. **queue-aware 中轨：**L2 下单时价位数量作为 queue-ahead 起点；trade 只能消耗
   不超过公开成交量，partial fill ≤ order remaining；取消在前/在后无法识别时输出
   区间，不假装精确。
3. **乐观上界（诊断）：**at-touch；永不参与 go/no-go。

### FS-3 · 必须有的模拟细节

- create/cancel/amend/decrease 分别使用实测延迟分布，不用单个常量。
- cancel_pending 期间允许 partial/full fill；ack 丢失进入 unknown→reconcile。
- queue priority 按 venue 实证；自己真实订单的 queue-position 用于校准，不外推成
  历史 L3 真相。
- 每次 fill 受 public trade size、remaining size、价格穿透和 side 约束。
- 每 series/date 逐笔费用与 ceil 规则；取消、退赛、settlement、linked contracts、
  event/factor inventory 全入账。
- missing L2、seq gap、stale book、pause、clock anomaly 全部触发 fail-closed。

### FS-4 · 红灯测试（缺一即模拟器作废）

- quote 已发撤单、撤单 ACK 前被打：必须成交并产生库存。
- 10 张挂单前有 100 张 queue，公开成交 20：不得填自己。
- 公开成交 3，自己剩余 10：最多 partial fill 3。
- amend 丢 priority 与 decrease 保 priority 按 D2 实测分别重放。
- duplicate trade/fill 回报不重复计仓；out-of-order fill 最终对账守恒。
- 429/timeout/5xx 后不盲目重发；同 client_order_id reconcile。
- fee rounding 边界、99¢/1¢、次美分、部分成交、取消/退赛结算手算 golden。
- gap 窗口零新报价；已有 resting order 的最坏损失仍入账。
- 同输入重放 bit-reproducible；现金+仓位+预留守恒逐事件成立。

### FS-5 · 真实校准

shadow 没有真实订单，不能校准 queue。G7 后用极小 post-only probe 收集自己的
queue_position、user_orders、user_fills、cancel timing。按 decile 比较：预测成交率
区间 vs 实际成交率、预测 fill time vs 实际 fill time、预测 partial-fill 分布 vs 实际。

**PASS：**实际覆盖落在预注册的模拟区间内，悲观轨不得系统性高估 fill/PnL；否则
退回 FS-2/FS-3，所有旧利润报告作废重跑。

## 9. F 组 — Alpha、定价和样本外利润（G4/G5）

### F1 · S1/S5 edge screen

- S1 保留为方法学完成的 dev-grade screen。
- S5 用 EC2 recv-era 数据原样重跑；合法结论改为
  `{edge-candidate, reject, collect}`。
- 立即反应曲线 B2 与30/120s库存曲线并报；不能用慢 horizon 掩盖撤单竞赛。

### F2 · Calibration

- λ(δ) 严格说是“公开流下的机会强度”，不是已知真实 fill probability；真实
  probability 由 W-FS1 + tiny-probe calibration 给出。
- toxicity surface 至少含 10/25/50/100/250/500ms、1/2/5/10/30/120s，
  30/120s 不再独占。
- vol/jump 用 log-odds，BADAMS 零成交报价跳变仍是 breaker regression anchor。
- 每项按 category/tour/liquidity/phase 支持数分层；不足输出 NULL。

### F2b · 三层选品防过拟合

按既有框架分别测试 series 周级画像、event 日级评分、盘中实时 radar：

- 画像/评分权重只用 train 拟合，validation 前冻结 top-k 和候选进入/退出阈值。
- test 报全部 eligible 市场、被选市场和未选市场三组，防止只展示赢家。
- 实时 radar 只消费当时可见 recv-clock 特征；暂停、临近结算、gap、burst/
  toxicity 恶化立即退出候选。
- 报候选稳定性、日换手、机会覆盖率、被排除市场后来表现和选择带来的增益 CI。

市场选择本身没有样本外增益，full model 即使单笔 edge 为正也不能通过容量门。

### F3 · 同流 modeled-quote backtest

行情输入、策略决策和风险 reserve 使用未来 C++ 引擎同一逻辑；出口在 backtest
接 W-FS1，在 shadow 接记录器，在 live 接 executor。热循环里不按 mode 分支。

比较组：naive join-touch、no-signal、fair-only、full model；做 ablation 验证每个
模型项是否真的增加样本外净收益，禁止只比较最终胜者。

### F4 · 样本外利润硬门（在打开 test 前冻结）

必须同时满足：

1. validation 与一次性 test 的**悲观轨**逐笔 fee 后，event-block bootstrap
   95% CI 下界都 >0。
2. 至少 7 个完整交易日；test 中正 PnL 天数 ≥5/7。
3. 任一比赛/event 不贡献总利润的 >25%，任一探索桶不能单独扛起结论。
4. latency ×1.5 后 95% CI 下界仍 >0；×2、cancel p99×2、queue-ahead p90、
   fee 上调一档、gap fail-closed 的组合压力下净收益不得显著为负。
5. 最大 event/factor/总库存、最大回撤、退出成本全部在 W-K3/K4 caps 内。
6. 报告同时给机会数、模拟 fill rate、每 fill edge、日容量和 capital-at-risk；
   只有 edge/张而没有可成交容量，不能 PASS。

这些是技术研究门，不是实盘授权；资本规模和可接受日亏损仍由操作员决定。

## 10. G 组 — Shadow 与回测/实盘对称（G6）

### SH-1 · 决策一致性

同一条已记录输入流分别喂 backtest 和 shadow；每个 quote intent、风险拒单、撤单、
熔断逐事件 diff。除随机数种子已冻结的模拟 fill 外，决策必须逐位一致。

### SH-2 · 零下单证明

shadow 启动前后账户 resting orders/fills/positions 不变；executor sink 只能是记录器，
代码层证明没有 HTTP order path。任何 POST 尝试自动 FAIL + 报警。

### SH-3 · 连续5绿日

沿 MM_ROADMAP：连续5个完整交易日 shadow 悲观 PnL >0；总最大回撤 < 平均单日
利润 3×。同时要求：

- 预测 fill/edge 分布没有日间结构性漂移；
- 无未解释 seq gap、clock anomaly、fee unknown；
- quote/requote/cancel 意图在 rate limits 内并留至少预注册 headroom；
- jump/pause/close-time/库存熔断全部至少在 replay 或实时出现一次并正确动作。

没有真实订单时的 shadow fill 仍是模拟结果，所以 SH-3 只解锁安全 rehearsal，不证明
真实 fill calibration。

## 11. H 组 — 风控、故障注入与运行稳定性（G7）

W-K1..K5 已完成不代表系统接线后自动安全；必须在完整引擎上重跑：

### RK-1 · 五层 reserve-before-send

单 market/event/factor/总敞口/日亏五层原子预留；并发十单只允许额度内的单；
失败、超时、partial fill 精确归还；减风险单在满额时仍放行。现金、仓位、预留
全程 E4 守恒。

### RK-2 · 故障矩阵

逐项自动注入：WS 断线、REST timeout、429、5xx、ACK 丢失、重复/乱序 fill、
cancel-pending fill、进程崩溃、磁盘满、日志 writer 卡住、时钟跳变、exchange pause、
市场 determined、账户状态读取失败、私有频道断流。

每项预注册唯一安全动作：撤单、停止新风险、reconcile、panic 或保持减仓通路；
不允许错误扩大权限。

### RK-3 · Kill switch 与 reconcile

- mock 中 N resting + M positions：cancel-all→verify zero→分轮退出→报告。
- 网络中断后重复 panic 幂等；client_order_id 不变，不双成交。
- engine vs exchange 差异按 class 报警；exchange 为真相，永不盲重发。
- 独立进程、策略挂死时仍可运行；报警链路端到端送达。

### RK-4 · Soak 与热路径

24h shadow soak；稳态热路径 allocation=0；队列有界无 drop；内存无增长趋势；
research/export 满载不影响交易 p99；重启恢复后先 reconcile 再允许 intent。

**PASS：**全部 fixture、sanitizer、TSAN、`make check`、`tests/run_pipeline.sh`
全绿；任一故障注入没有产生未受控新风险。

## 12. I 组 — Operator-gated 实证与微实盘（G8/G9）

以下每一步都是 live-order 行为，必须满足 GUARDRAILS S1–S6、账户资金、
operator 当次书面确认；agent 不能自行启动。

### I1 · W-K6 kill-switch rehearsal（第一笔真实订单）

一张远离 touch 的 tiny post-only 订单只为给 panic 一个目标：place→panic→
zero-resting；再做一次中途断网后的幂等恢复。结束账户必须 flat/zero-resting。

### I2 · Maker action/latency probes

逐个验证 D2/C5；每轮最多一个市场、一张、远离 touch，前后 assert-zero-resting。
任何会留仓的 probe 必须事先有自动退出和 panic 退路，不能靠“之后手动平”。

### I3 · Queue/fill calibration probes

在候选市场用1张 post-only、严格次数/日和亏损预算，采 own queue position、
user_orders、user_fills；只为校准 W-FS1，不宣称策略盈利。

### I4 · 策略微实盘

仅在 G0..G8 全绿后：单市场、1–5张、总风险 ≤$20、post-only、dead-man、
cancel-on-disconnect、完整 caps。每日对账实际 vs 预测成交率/edge/fees/cancel race。

升级门：连续两周净 PnL >0，实际成交率 ≥模拟预测的70%，模拟区间覆盖率合格，
无安全事故。回退门：单日亏损 >$10、任何账本/对账异常、实际 fill 系统性差于
悲观轨、kill switch/alert 失效 ⇒ 立刻 panic，回 G3/G6。

放量每次最多2×，每次放量后重新跑容量、rate limit、库存和回撤压力测试。

## 13. 执行顺序与当前最近动作

```text
独立审计 S1
  -> 全市场 U0/U1 universe + daily mart（18类先DQ）
  -> A1/A2 数据与双钟
  -> 全市场 A3 L2 分波采集（同时 A4 生命周期/结算）
  -> 全市场 B1/B2 立即反应 atlas + C1..C4 我方离线/只读秒表
  -> D1 maker action contract matrix
  -> W-FS1(FS-1..FS-4) + 历史上下界回放
  -> 数据成熟后 F1/F2 校准
  -> F3/F4 未开封样本外利润门
  -> SH-1..SH-3 shadow 5绿日 + RK-1..RK-4 安全/soak
  -> operator 排期 I1
  -> I2/I3 极小实证，回校 W-FS1
  -> 必要时重跑 F4/SH-3
  -> operator 决定 I4 微实盘
```

现在最近的五件事，按顺序：

1. 独立审计 S1；不改其冻结指标。
2. 按 `PLAN_FULL_MARKET_RESEARCH` 构建全18类 U0 universe 与 U1 daily mart，
   先交覆盖/DQ，不先挑赢家。
3. 修正 S1/S4 burstiness 数据源，并扩成全市场 raw book/trade 分频道 atlas。
4. 启动全市场 L2 分层 rollout；数据无法事后补回，但不得冲击主 firehose。
5. 完成 reaction budget 与 W-FS1；在它们完成前不发布可交易排行榜。

## 14. 与现有计划的绑定和纠偏

- `PLAN_RESEARCH_CYCLE_1`：S1 结果保留；S4 按 B1 修正；S5 结论改为
  edge-candidate；新增 B2/C3 作为 maker reaction 必修门。
- `PLAN_PRICING_MODEL`：W-C1 的 λ 只叫 opportunity intensity；W-C4 必须消费
  W-FS1，不再直接复用旧 `mm_backtest.py` fill semantics。
- `PLAN_DEPTH_EXPANSION`：A3 复用其独立 shadow-first、probe 和 capture continuity
  纪律；终态覆盖全活跃市场，但按容量分波，不把一次性全订阅变成生产事故。
- `PLAN_FULL_MARKET_RESEARCH`：定义全18类 universe、daily mart、atlas、假设法庭、
  多重比较、L2分层扩展和每-market合法结论；Tennis S1只是其方法学 baseline。
- `PLAN_RISK_KILLSWITCH`：W-K1..K5 作为 H 组组件；W-K6 = I1，仍由 operator
  排期，未被本计划提前。
- `PLAN_LIVE_VALIDATION`：只读检查与 mock 测试可复用；其中任何真实下单阶段
  一律延后到 G7 后并并入 I 组，不能独立执行。
- `MM_ROADMAP`：shadow 5绿日、微实盘1–5张/≤$20、两周与70%成交率门保持；
  maker fee 不再假设为0，按 series/date 事实逐笔计算。

## 15. 风险、生产连续性与回滚

- A/B/F 研究只读 warehouse，不触碰24/7生产；派生物可删除重建。
- A3/A4 是 capture-side 变更，必须独立 shadow-first、D4 ingest test 同 change、
  显式 continuity plan；异常立即停新增 depth process，不停主 firehose。
- W-FS1 是新工具，不原地改变旧回测语义；双轨并存至独立审计通过，回滚为停用
  新工具和删除 derived outputs。
- I 组任何失败的回滚都是 panic→zero-resting→reconcile→回 shadow；禁止把
  “测试失败”解释成继续下单收更多样本。
- 本计划自身为 paper-only；按 P9 需要一次独立计划审计后才可成为执行依据。

## 16. 计划审计自查（GUARDRAILS）

1. 推进 Phase 1.5→2→3，逐门顺序，不提前 live：PASS。
2. 实盘全部集中 I 组，S1–S6 + operator 当次确认：PASS。
3. log-odds、逐笔费用、E4 fixed-point：PASS。
4. 悲观成交下界是唯一 go/no-go，乐观只诊断：PASS。
5. 交易/影子由 WS full-depth，同流三模式：PASS。
6. 新行为有 golden/property/fault/economic-sign tests：PASS。
7. 生产 pipeline 风险与 continuity 写明：PASS。
8. 工作有界、派生可重建、live 回滚为 panic：PASS。
9. 已点名需同步纠偏的旧计划：PASS。
10. gap、PLACEHOLDER、小样本、unknown fee、queue 歧义均不能伪装绿色：PASS。

**Self-audit verdict：PASS，等待独立审计。**
