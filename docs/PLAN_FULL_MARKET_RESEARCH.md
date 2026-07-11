# PLAN — Kalshi 全市场研究计划

**状态：PLAN-ONLY，2026-07-10；待独立审计。**

## 0. 一行裁决

**所有市场都进入研究 universe；只有通过数据、成交和样本外门的市场才进入
交易候选。** 网球是第一份方法学 pilot，不是研究边界，也不享受永久优先权。

本计划推进 MM_ROADMAP Phase 1/1.5 的全市场 discovery 与 calibration，不授权
实盘、不改变 `MASTER_SEQUENCE`。总安全门仍由 `PLAN_MM_TEST_PROGRAM` G0→G9
控制；W-FS1 前所有 fill/PnL 结果均为 `NON-GATE`。

## 1. “全市场”的精确定义

研究 universe = 研究窗口内 catalog 曾出现的**全部 market**，包括 open、closed、
determined、settled、取消、零成交、空盘口和后来下架者。禁止只研究“今天仍活跃”
或“成交过”的幸存者。

当前分类基线来自 `config/market_classes.yaml`，18类全部入库研究：

1. Crypto
2. Financials
3. Economics
4. Climate and Weather
5. Commodities
6. Sports
7. Politics
8. Companies
9. Education
10. Elections
11. Entertainment
12. Exotics
13. Health
14. Mentions
15. Science and Technology
16. Social
17. Transportation
18. World

Kalshi 后续新增类别自动进入 `_unclassified` 并使最终覆盖门变红，直到完成映射；
不能默认当成“不研究”。Exotics/MVE 照样采集、描述和归档，但按 GUARDRAILS Q7
不得进入自动 MM 候选，这是“研究全覆盖”和“交易准入”两个不同问题。

## 2. 两条研究线同时存在

### Universal lane（全市场普查，立即可做）

使用所有市场现有的 catalog + L1 + trades + fee facts + lifecycle/settlement（有则用，
缺则标），回答：哪里有客流、价差、毒性、周转率、规则风险和数据缺口。

它覆盖全部市场，但不能声称精确 queue/fill probability。

### Depth lane（全市场 L2，按容量分波扩展）

目标终态是全部活跃市场 snapshot + delta；rollout 按 `PLAN_DEPTH_EXPANSION` 的
50→200→500→实测容量继续扩，不按观察到的 PnL 只挑赢家。每一波必须按
category × liquidity tier 分层抽取，保证每个类别都有代表；最终逐步覆盖全部活跃
market。未有连续 L2 的市场可以留在普查表，但不能进入 W-FS1 queue-aware 利润门。

“全市场研究”不等于“第一天同时订9,000个 L2 导致采集崩溃”。覆盖必须通过
独立 depth 进程、sequence/gap/磁盘门逐级挣得，主 firehose 永远不能被拖垮。

## 3. 统一全市场维度表 U0

### 现有工具边界（先承认，禁止误用）

- `tools/mm_scan.py` 默认可扫全 category，但当前分数主要是 spread × flow +
  top-depth 过滤；它是候选扫描雏形，不是 fee/toxicity/fill/capacity 完整排行榜。
- `tools/research/build_segments.py` 和 maker-edge pilot 虽有 category 参数，但
  `tour_level`、`market_kind`、1¢ win/loss 主表等语义是 Tennis 专用。不能只改
  `--category` 就宣称已覆盖全市场；全市场 U0 必须有按类别验证的 contract adapter，
  无法解析者进 unknown 并使相应 gate 变红。
- `mm_calibrate`/旧 `mm_backtest` 可提供描述和 strict-through 诊断，但费用、
  queue、cancel-pending、partial fill 等未由 W-FS1 补齐前，不能提供可交易排名。

因此本计划复用经过验证的时间、费用、统计组件，不复用 Tennis-only 分类假设。

新建可重建的 `work/research/dim_market_universe.parquet`。一 market 恰好一行，
至少包含：

- identity：market_ticker、event_ticker、series_ticker；
- taxonomy：category、subcategory、group；
- contract：market_kind、strike_type、mutually_exclusive、MVE flags；
- economics：tick_stratum、maker_fee_class、fee_type/multiplier、fee 生效窗；
- time：open、expected_expiration、close、determined、settled、取消；
- settlement：规则版本、结果、取消/退赛处理、settlement source；
- information：anchor_class（external-index/sports-score/poll/data-release/
  reflexive/no-anchor）、外部锚是否存在；
- trading eligibility：Q7、规则含糊、fee unknown、数据缺口、过期/暂停等每个
  exclusion_reason 独立 bool；
- coverage：L1/trade/L2/lifecycle/settlement 的 first/last ts、rows、gap count、
  recv-clock coverage；
- study status：universe / descriptive-only / L2-eligible / model-eligible /
  rejected / collect。

禁止用单个 `eligible=false` 吞掉原因。研究报告必须能回答“这个市场为什么没进入
下一层”。

### U0 验收

- catalog 对账：研究窗口全部 markets 100% 有一行；重复0、漏行0。
- 18类及新类别计数全部打印；`_unclassified >0` 时最终门 FAIL。
- open/closed/zero-trade/empty-book markets 都存在，避免幸存者偏差。
- fee unknown、rule unknown、settlement missing 不删除，只标 descriptive-only。
- 输入 manifest、code SHA、命令、clock、每个 exclusion count 齐全。

## 4. 全市场日级事实表 U1

建立增量、分区的 `fact_market_research_daily`（date × market），不用每次扫描数千万
原始行。DuckDB/columnar + partition pushdown，禁止把全仓一次装进 pandas。

每 market-day 至少计算以下字段。

### 数据质量

- L1/L2/trade 行数、recv 覆盖率、seq gaps、stale/no-book/locked/crossed 秒数；
- active observed span、catalog coverage、lifecycle/settlement completeness；
- duplicate/conflicting trade_id、未知 side、异常 timestamp 数。

### 客流和机会

- trades、contracts、notional proxy、unique active minutes；
- trade size p10/p50/p90/p99、整数手比例、同 timestamp batch；
- quoted-time、two-sided-time、spread cents 与 log-odds 分布；
- top quantity、可见 depth（L2有则全层）、price-band occupancy；
- event/market lifetime、距结算时间、资金周转次数/日。

### 速度与毒性

- L1/L2/trade 分频道 inter-arrival p0.1/p1/p5/p10/p50/p90/p99；
- 每10/25/50/100/250/500ms消息数、burst size/duration、跨市场同步 burst；
- 10/25/50/100/250/500ms、1/2/5/10/30/120s signed markout；
- first adverse update/mid move/strict-through trade 时间；
- 大小单、单边 trade run、锚移动后/平静期的 toxicity 差。

### 经济性与风险

- half-spread、逐笔 maker fee、gross/net edge preview；
- strict-through opportunity count（明确不是 fill probability）；
- settlement/取消后的持仓价值、规则风险等级；
- event/factor 相关性、同 event 同向最大潜在敞口；
- anchor latency/quality、无锚反身性、结算参照可操纵性状态。

所有数同时带 n、event count、clock、support flag；不足不插值，输出 NULL。

### U1 验收

- 原始→daily 按 category/date 抽样对账，差异 ≤1% 或逐项解释。
- 已知 synthetic tape 的 spread/markout/burst/fee 手算 golden 全匹配。
- 相同输入复跑逐位一致；增量重算与全量重算抽样一致。
- 缺口窗口在 opportunity/PnL 中为 fail-closed，不产生虚构收入。

## 5. 全市场图谱 U2（描述，不挑赢家）

交付一个自包含交互 HTML，默认显示全 universe，支持：category、subcategory、
series、market_kind、tick、fee、anchor、lifecycle、liquidity、data quality 筛选。

必出视图：

1. universe 漏斗：catalog→有L1→有trade→有L2→fee/rule known→model eligible；
2. category/series 市场数、成交、contracts、活跃分钟、零成交率；
3. spread × trade arrival × visible depth 三维机会图；
4. 全类别 immediate/long markout 曲线与 CI；
5. inter-arrival lower tail、burst 和 preliminary reaction budget 对比；
6. fee 后净 edge preview 分布（强制 `NON-GATE`）；
7. 生命周期曲线：open→event→close/settle 的客流、spread、toxicity；
8. 规则/结算/fee/data gaps 热图；
9. event/factor 相关性和潜在集中风险；
10. 每 market 可搜索明细卡：所有数 + n + exclusion reasons。

禁止只画 category 平均值。每层必须能下钻到 series/event/market；长尾用中位、
分位和总量并报，不能让几个大市场掩盖零流动市场。

### U2 合法结论

只有 `{atlas-valid, atlas-flawed}`。图谱本身不产生 trade/reject；它用来冻结后续
假设、分层和选样规则。

## 6. 全市场假设法庭 U3

把 `RESEARCH_EDGE_HYPOTHESES_2026-07-09.md` 从单一品类问题改成全市场同口径
比较。每个假设必须先写哪些 category/anchor_class 适用，不能强迫无锚市场回答
“锚领先性”。

| 研究族 | 全市场问题 | 主输出 |
|---|---|---|
| Fee wall | maker/taker 费能否覆盖各类 toxicity？ | series/date fee 后 edge 分解 |
| Retail flow | 哪些类别有小额、规律、与锚不同步的流？ | arrival/size/toxicity mixture |
| Anchor lead | external index、体育比分、民调、数据发布谁领先？ | recv-clock lead-lag minus reaction budget |
| Queue value | 哪些市场价值集中在队首？ | L2 queue strata + W-FS1 calibration |
| Size toxicity | 大单是否更毒，放量会不会毁 edge？ | size decile markout curves |
| One-sided flow | 连续单边流是否预示继续不利移动？ | run-length conditional drift |
| Lifecycle | 何时开摊、何时退出、settlement convergence 多长？ | phase curves + no-quote proposal |
| Cross-market | 同 event/因子相关性如何放大仓位？ | correlation/tail co-move |
| Rule risk | 哪些类别规则含糊/可裁量/可能作废？ | rules checklist + eligibility |
| Capacity | 机会数×fill×edge×周转能赚多少，而非只看edge/张？ | daily lower-bound capacity |

每个研究族单独预注册。探索发现的新模式只能进入下一轮，不能回头修改本轮门槛。

## 7. 多市场筛选而不 p-hack：U4

### 先分池，不先打一个总分

所有市场先进入四个互斥研究池：

1. **CLOB microstructure maker：**只靠盘口/流，外部锚可无。
2. **Anchored maker：**有可实时验证的外部锚，可研究领先性。
3. **Event-information：**需要比分、民调、数据发布等信息源；源未验证前只描述。
4. **Descriptive-only：**Q7、规则/fee/data unknown、容量极低；仍在图谱，不交易。

### 硬过滤后才排名

先过 data/fee/rules/expiry/Q7/reaction gates，再对剩余市场报告一个**向量**，不靠
单一神秘分数：

- pessimistic net edge lower CI；
- opportunity rate；
- W-FS1 pessimistic fill rate；
- daily capacity / capital-at-risk；
- reaction margin（adverse clock − cancel effective）；
- settlement turnover；
- worst event/day loss、concentration；
- data quality/coverage。

若运营需要单一排序，权重必须在 train 段冻结，并同时发布每个原始分量；禁止按
validation/test 表现修改权重。

### 多重比较纪律

- train：生成特征、分池、阈值、top-k；
- validation：确认候选，按预定义 hypothesis family 做 BH-FDR q≤0.05；
- test：只开一次，只使用 F4 的 event-block bootstrap 和悲观 W-FS1 PnL；
- 单 market/series 的漂亮结果不能代表 category；category 的漂亮均值也不能代表
  每个 market；两层都报。
- hierarchical shrinkage 只可按预注册的 category→subcategory→series 层级，
  raw estimate 与 shrink estimate 并排，禁止把负值平滑成正值。

### U4 合法结论

每 market/series 只能是：

- `deep-dive-candidate`
- `collect-L2`
- `collect-settlement/lifecycle`
- `reject-microstructure`
- `descriptive-only`

没有 `trade`。

## 8. L2 全市场扩展与信息增益 U5

每个 rollout wave 的名单不是“收益榜前N”，而是两部分：

- 70%：按全市场 research value（活跃、价差、成交、潜在容量）排名；
- 30%：按 category × liquidity × fee × market_kind 分层覆盖长尾，用于证明我们
  没因早期偏见漏掉某类 edge。

具体比例在首次 probe 前冻结；70/30 是本计划默认提案，独立审计可要求修改，
但看过结果后不得修改。

每波验收：

- 每类别市场数、消息率、GB/day、seq gaps、drop、CPU/RAM/disk；
- 与未收 L2 市场的特征分布对比，量化 selection bias；
- 新 L2 对 queue/fill 不确定区间缩窄了多少；
- buffer overflow/主 firehose 影响 = 0；不满足立即回滚该 depth wave。

终态目标是全部活跃市场 L2。若交易所连接/磁盘/预算让终态暂不可达，报告必须列
出未覆盖 markets 和原因；不允许写“全市场 L2 已完成”。

## 9. 深度回测与样本外裁决 U6

仅 `deep-dive-candidate` 且有连续 L2 的市场进入 W-FS1。每个 category/series
独立报告：

- strict-through 下界、queue-aware 中轨、at-touch 上界；
- create/cancel/amend/decrease latency、cancel-pending fills、partial fills；
- 逐笔 fee、settlement、inventory unwind、gap/failure tax；
- baseline vs fair-only vs full model ablation；
- validation + 一次性 test、latency/queue/fee/gap 压力测试；
- 机会数、fill rate、edge/fill、日容量和 capital-at-risk 五项分解。

通过线完全继承 `PLAN_MM_TEST_PROGRAM` F4；全市场扫描不能降低任何门。每个类别
可以得出不同结论，禁止合并一个“Kalshi总体正收益”。

## 10. 输出目录与工具拆分

建议落点（执行 W 再正式批准 writes）：

```text
work/research/full_market/
  PRE_REGISTRATION.md
  manifest.json
  dim_market_universe.parquet
  fact_market_research_daily/category=<...>/date=<...>/*.parquet
  universe_audit.json
  atlas.html
  market_ranking.parquet
  hypotheses/<family>/results.json
  l2_coverage_plan.csv
  DECISION.md
```

工具责任分开，禁止一个500行脚本包办所有逻辑：

1. `build_market_universe`：catalog/fees/rules/coverage 一行一市场；
2. `build_market_daily_features`：增量事实表；
3. `full_market_atlas`：只读事实表出图；
4. `full_market_screen`：冻结分池/硬门/排名；
5. 各 hypothesis 独立工具；
6. W-FS1 独立于 screen，不能由被评分工具修改成交语义。

全部注册 `tools.json`、一条命令可按 category/date/series 重跑、输出指纹。全市场
运行支持 checkpoint/resume，不因单类别失败丢掉已完成分区。

## 11. 执行波次

### Wave 0 — 独立审计与预注册

审计本计划、总测试计划和 S1；冻结 U0/U1 schema、研究窗口、分池规则、70/30
L2名单规则、train/val/test。

### Wave 1 — 全市场 universe + daily mart（现有数据即可）

构建 U0/U1，全18类对账；先交 DQ，不先看盈利结果。

### Wave 2 — 全市场 atlas + raw-message reaction

完成 U2 和 B1/B2；旧 S1 burstiness 只作为待纠正的 tennis 诊断，不进入图谱。

### Wave 3 — 假设族与第一版 screen

运行 U3/U4，输出所有 market（含 rejected/collect），冻结 deep-dive 与 L2名单。

### Wave 4 — L2 rollout

依 PLAN_DEPTH_EXPANSION 50→200→500→实测扩大；每波独立审计，生产连续性优先。

### Wave 5 — W-FS1 + validation/test

按 category/series 跑 U6；只产生 edge-candidate/reject/collect。

### Wave 6 — Rolling research

每日增量 mart、每周 series画像、每月/重大 regime change 重估；旧 test 封存，
下一轮必须使用新的未来 test 窗，不能反复复用。

## 12. 最近动作

1. 先完成 S1 独立审计，保留它作为 tennis methodology baseline。
2. 起草 U0/U1 的正式 W：只读、全18类、先 DQ 后指标。
3. 把 S4 改成全市场 raw L1/L2/trade 分频道 reaction atlas。
4. 执行 depth probe 并按 U5 分层名单扩展，不再只写“候选 Tennis”。
5. W-FS1 完成前，不发布任何全市场“可交易排行榜”。

## 13. 风险、回滚和生产连续性

- U0..U4 全部只读 warehouse，输出 derived parquet/HTML；回滚为停工具、删 derived。
- 全市场大查询必须分 category×date checkpoint；OOM/超时只使该分区 FAIL，不允许
  静默少算后仍出绿色总报告。
- U5 是 capture-side change：独立 depth process、D4 ingest test、sequence/drop
  monitoring、磁盘上限；异常停 depth，不停主 L1/trade firehose。
- 新数据源（sports score、poll、index）若收费/签约，先由操作员决定；本计划不
  自动购买或传 credentials。
- Exotics/MVE、规则含糊、fee unknown 研究可见但 fail-closed 不交易。

## 14. 自审计（GUARDRAILS）

1. Phase 1/1.5 全市场研究，不跳 live：PASS。
2. 悲观 W-FS1 是唯一利润门：PASS。
3. log-odds + 逐笔 fee + E4：PASS。
4. 全市场 WS L1、L2 rollout 服从 Q5/P4：PASS。
5. 新类别、长尾、零成交、缺口不会静默消失：PASS。
6. train/val/test + FDR + block bootstrap 防多市场 p-hack：PASS。
7. 所有市场可见，Q7 只限制交易不限制研究：PASS。
8. 生产连续性、容量分波和回滚写明：PASS。

**Self-audit verdict：PASS，等待独立审计。**
