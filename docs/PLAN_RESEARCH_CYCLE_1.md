# PLAN: 第一轮研究循环(操作员手把手版,2026-07-10 操作员批准)

目的:把近期确定的全部测试收进一个可顺序执行的计划。每一步:
操作员粘贴引文/敲命令 → 产出落盘 → **回 Cowork 会话读数**(由 Cowork
逐个数字讲解)→ 满足条件才进下一步。
性质:研究(T2 只读为主)+ 一项测量任务;不碰生产、不动
MASTER_SEQUENCE 既有步骤;悲观口径与全部闸门不变。
关联档案:sandbox/research/pilot_maker_edge/PILOT_SPEC.md(操作员
十点规格)、RESEARCH_EDGE_HYPOTHESES(H1-H13)、PLAN_PRICING_MODEL
Group C(正式校准,gated ≥2026-07-13)。

---

## S0 · 前置(日历事件,已排)
- STEP 1 收官验收(24h 零缺口)→ 通过后同一维护窗口:apt 升级 +
  重启 + **W-TL1 部署上 EC2**。自此 recv 时间戳开始在生产积累。
- 状态:待操作员今晚执行(引文已在手)。

## S1 · Maker-edge pilot 完整版(spread − markout − fees 分解)
- 目的:H1 第一次开庭——赛前网球的价差收入是否盖得住毒性。
- 定位(操作员 2026-07-10):**网球 = 滩头,不是疆界**——选它因为
  比赛密度/样本量最大、常规巡回赛平稳;pilot 产出的是可复用模板,
  S4 用它横扫全品类出排名表,后续品类扩张照表点名,数据驱动。
- 执行:操作员在 VSCode 粘给 Claude Code(引文见附录 A)。
- 产出:DQ 报告 + 分解表(spread capture vs mid-口径 markout,
  含 bounce/drift 拆分)+ 盘口级 burstiness + HTML,落 work/research/。
- 读数:回 Cowork,逐表讲解;结论三选一(trade/reject/collect)。
- 注意:Mac-era 数据 → 结论标 dev-grade;方法学在 S5 用 EC2 数据重跑。
- 选品钉死:零 maker 费的 series(逐 series 查证,Q3)。

## S2 · 反应秒表·上半(本地可测,无风险)
- 目的:回答 Rhys 问题#3 的"我方钟":签名到底多贵。
- 执行:RSA-PSS 签名基准 **Mac 与 EC2 各跑一份**(生产为 ARM
  盒子,config 采用 EC2 数字;p50/p99,≥1e4 次),
  结果写进 config/backtest_latency.yaml 替换对应 PLACEHOLDER,
  注明 MEASURED + 日期。纯本地计算,不碰网络。
- 读数:回 Cowork——签名占整个反应预算的几成,值不值得优化。

## S3 · 反应秒表·下半(需操作员批准后执行)
- 目的:signed-POST 全链 RTT p99(真实往返,不碰真单)。
- 执行:执行会话先提交采样方案(端点选择、频率、只读性论证)
  → **操作员过目批准** → **从 EC2 发起**,≥3 个时段各采样 ≥100 次
  (RTT 是分布不是快照)→ 替换 RTT PLACEHOLDER。
- 读数:回 Cowork——合成 order_effective 总预算,对照 S1 的
  市场心跳表,正式回答"我们比市场快几倍/慢几倍"。

## S4 · 盘口级 burstiness + 品类横向对比
- 目的:Rhys 问题#2 的完整版(quote 更新比成交更密)+ 品类对比
  (sports 各子类 vs crypto)。
- 执行:并入 S1 的 pilot 会话或单独一场(L1 数据,duckdb)。
- 读数:每品类一行:心跳中位/p99、爆发系数、与我方预算的倍数。

## S5 · EC2 时代重跑(唯一有资格进 go/no-go 的版本)
- 门:≥2026-07-13 七天净数据 + recv 列积累(实际 ≥07-17 成熟)。
- 执行:同 S1 的命令在 EC2-era 数据上原样重跑(capture_host=EC2);
  markout 双钟并报(exchange 与 recv),**主判决用 recv**;
  产出替换 dev-grade 版本,并做 slam-week vs 常规赛跨 regime 对比。
- 读数:回 Cowork,与 dev-grade 版对比——结论变没变,为什么。
- 此后接 Group C 正式校准(PLAN_PRICING_MODEL,recv-only,无例外)。

## S6 · 订单行为实证(W-K6,操作员排期)
- 目的:Rhys 问题#4 的收尾——订单类型行为与文档一致性
  (post-only 拒单、到期自灭、client_order_id 409 去重)。
- 门:操作员排期 + 注资;按 PLAN_RISK_KILLSWITCH W-K6 原文执行,
  全程 operator-gated(S1/S3)。

---

## 统计与可信性纪律(2026-07-10 审计后追加,约束 S1/S4/S5 全部分析)

1. **预注册主指标(唯一判决数)**:赛前时段、成交量加权的
   `半价差 − 30s 中价 markout`(¢/张,悲观口径)。开跑前钉死;
   其余全部桶为探索性,只产假设不产结论。
2. **Regime 标签**:07-06..08 = Wimbledon slam-week;所有结论携带
   `regime=slam-week`,禁止外推常规巡回赛;S5 做跨 regime 对比。
3. **选样/测量分离**:市场与 series 名单仅用 train 段(07-06/07)
   冻结;val 段(07-08)只测不选。
4. **不确定性 = 按比赛整场的块状 bootstrap**(≥1000 次重抽),
   禁止按笔独立假设算标准误(成交高度聚簇,CV≈6-8)。
5. **中价卫生**:L1 LOCF 补价带最大陈旧度上限(默认 60s,超限
   剔除并计数);单边空书(bid≤1¢ 且 ask≥99¢)剔除;locked/crossed
   剔除并计数。
6. **tick 一致性**:1¢ 与次美分 series 分开统计,pilot 主表限 1¢。
7. **因果分界**:赛前/开打探测器只用截至当时的信息,参数在
   train 段冻结后原样用于 val。
8. **可复现指纹**:每份产出头部 = code commit SHA + 数据文件
   manifest md5 清单 + 完整命令行。
9. **性能纪律**:网球切片一次性物化为 parquet(work/research/
   下,derived 可重建);一切查询带 category/subcategory/date
   分区过滤。

## 附录 A · S1 引文(粘给 Claude Code)

> 读 sandbox/research/pilot_maker_edge/PILOT_SPEC.md(操作员十点
> 规格)与 docs/PLAN_RESEARCH_CYCLE_1.md S1 后,在本机用 duckdb +
> tools/warehouse.load() 执行完整 maker-edge pilot:
> 品类 = Sports/Tennis 赛前时段,2026-07-06..08 按时间顺序切
> train/val;mid 口径 markout(1s/10s/30s/120s),把 markout 分解
> 为 bid-ask bounce 与 true drift 两项;fee 用
> --preview-unverified-fees 口径挂 NON-GATE 横幅(OQ-1 未批);
> 选品限零 maker 费 series(逐 series 查证);成交模型假设显式
> 成段写出(join-the-touch 悲观/乐观双口径,无队列数据 = 悲观
> 按队尾);附盘口级 inter-update 间隔分布(赛前 vs in-play,
> 按品类)。产出 DQ + 结果表 + HTML 落 work/research/,每个数字
> 带样本量与分位数;结论三选一。全程只读仓库;Mac-era 数据结论
> 标注 dev-grade(regime=slam-week)。**严格执行本计划
> 「统计与可信性纪律」全部 9 条**(预注册主指标、选样/测量分离、
> 按场 bootstrap、中价卫生、tick 分层、因果分界、可复现指纹、
> parquet 物化 + 分区过滤);主指标未预先声明即开算 = 审计 REJECT。
> 退出仪式 + 独立审计。

## 执行台账(操作员勾选)
- [ ] S0 收官 + TL1 部署
- [ ] S1 pilot(dev-grade)→ 已回 Cowork 读数
- [ ] S2 签名基准 → 已回读
- [ ] S3 方案批准 → RTT 实测 → 已回读
- [ ] S4 盘口 burstiness → 已回读
- [ ] S5 EC2 重跑(正式版)→ 已回读
- [ ] S6 W-K6(另行排期)
