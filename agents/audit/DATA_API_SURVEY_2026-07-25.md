# 外部数据接口全网调研 · 汇总与理由(2026-07-25)

> 调研:Audit(Cowork Claude),基于 2026-07 公开信息。原则:**POC 用免费源,证明滞后里有扣费后的肉,再花钱买"更快"。**
> 所有接入的硬要求(不分免费付费):①我方接收时间戳(receive ts)必须落盘;②点时采集进自己管道(封存+收据);③出生即定义生命周期(prune 规则);④与 Kalshi ticker 的映射可机器核;⑤不做违反 ToS 的爬取。

---

## A · 已在手、免费、已运行

| 接口 | 喂哪条线 | 为什么需要 | 状态 |
|---|---|---|---|
| **Kalshi WS/REST**(自有 tape) | 全部 | 一切研究的本体 | ✅ 24/7 采集中 |
| **币安/Coinbase WebSocket** | **H3 加密锚** | 外部快价 vs Kalshi 慢价 = 搬运路线的免费试验田;毫秒级、零成本 | ✅ 已上线,等首份诊断 |
| **MLB StatsAPI**(官方免费) | H1 体育公允价(状态模型) | 逐球比赛状态(出局/垒包/比分,~20-30s 延迟);上周已用它核 scheduled start | ✅ 用过,待正式接管道 |

## B · 免费、建议现在立项

| 接口 | 喂哪条线 | 为什么需要 | 关键事实 |
|---|---|---|---|
| **Polymarket Gamma/CLOB/WS** | **politics 跨所领先滞后**(H5/H6 族) | 与 Kalshi 大量同事件盘(政治重合度最高);同一事件两个交易所谁先动 = 零成本跨所锚定;可复用已建好的 politics universe | 行情数据**完全免费、无需鉴权**;WS: `wss://ws-subscriptions-clob.polymarket.com/ws/market`;REST: `clob.polymarket.com` + Gamma 发现层 |
| **ESPN 隐藏 API** | H1 多运动比赛状态 | NFL/NBA/NHL/MLB/足球全覆盖、免key免鉴权 | **非官方、无 SLA、随时可能改**——只配 POC,不配生产依赖 |
| **BLS/Fed 官方发布** | 经济类市场(KXCPI/KXFED) | 结算真相 + 发布时刻表(事件研究的官方时间戳) | 免费官方,低频 |

## C · 低成本付费,POC 证明有肉后再买

| 接口 | 价格 | 喂哪条线 | 买它的触发条件 |
|---|---|---|---|
| **The Odds API** | 免费 500 credits/月(太薄);**$30/月 20K** 起 | H1/H2 共识赔率锚 | 便宜档更新慢(几十秒级)——**够测"分钟级陈旧价",不够秒级抢跑**。触发:H3 或 Polymarket 任一条跑出正滞后 edge |
| **EarningsCall.biz / API Ninjas** | 低价订阅/免费key起步 | **mention 市场**(财报会逐字稿——Ally"credit card"那类信息落地流的原料) | 触发:mention 校准测试出绿灯后,把"实时听会"能力补上 |

## D · 贵/封闭,有战绩后再谈

| 接口 | 现实约束 | 定位 |
|---|---|---|
| **Pinnacle API** | **2025-07-23 起对公众关闭**;bespoke 合作走 api@pinnacle.com 申请审核 | 尖锐锚金标准(podcast 验证过的原型)。拿到几个月实盘战绩后去申请 |
| **Betfair 交易所 API** | 开发用延迟 key 免费(数据合流打折);**Live key £499 一次性** + 美国可用性/合规需先查 | 交易所级尖锐价的备选;先用延迟 key 做研究可行性 |
| **Benzinga/Quartr**(实时电话会转写+webhook) | 机构定价 | mention 线放大时的生产级替代 |
| **OpticOdds/Unabated/Sportradar** | 机构定价 | 秒级赔率军备,POC 全绿后的加码选项 |

---

## 需求 → 接口对照(为什么各条线非要外部数据不可)

| 线 | 缺口 | 接口 | 没有它会怎样 |
|---|---|---|---|
| H3 加密锚 | ✅ 无缺口 | 币安 WS(已有) | — |
| politics 跨所 | 对手所行情 | Polymarket(免费) | 这条免费 alpha 线根本不存在 |
| H1 体育公允价 | 共识赔率 + 比赛状态 | Odds API + MLB/ESPN | 公允价没有锚,退化成拿 Kalshi 自己解释自己(家规禁止) |
| H2 事件反应延迟 | 带时间戳的外部事件流 | 同上 | prereg 写死:无外部流不启动、不许拿 Kalshi 成交代替 |
| mention 信息落地 | 实时逐字稿 | EarningsCall 级起步 | "听会的人"永远比我们快,只能跟二手流 |
| 经济类 | 官方发布时刻 | BLS/Fed(免费) | 事件研究没有真时间零点 |

## 执行顺序(总花费 $0 起步)
1. **现在**:Polymarket×Kalshi politics 领先滞后实验立项(免费,Registry 合同格式);MLB StatsAPI 正式接管道(出生带生命周期)。
2. **等读数**:H3 首份诊断 + Polymarket 实验结果 = "滞后里有没有扣费后的肉"的免费判决。
3. **有肉才花钱**:$30 Odds API 试 H1/H2 → 有战绩后申请 Pinnacle/评估 Betfair。
4. mention 线等校准绿灯,再谈转写接口。
