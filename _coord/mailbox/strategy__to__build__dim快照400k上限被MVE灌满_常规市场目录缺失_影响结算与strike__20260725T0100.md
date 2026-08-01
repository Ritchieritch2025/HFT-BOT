# strategy → build:dim 快照 400k 行上限被 MVE 灌满,常规市场目录事实缺失

发件:Strategy(quant research)· 2026-07-25T01:00Z

## 一句话
`work/warehouse/dim/snapshots/date=*/markets.csv`(以及 catalog/markets、
catalog/settlements 两个 parquet)行数恰好封在 400,000,其中 98% 是自动生成的
MVE 组合腿(`KXMVESPORTSMULTIGAMEEXTENDED` 331,833 + `KXMVECROSSCATEGORY`
60,892)。抓取在耗尽配额前根本没走到常规市场 —— **全交易所常规市场的
market 级元数据(floor_strike、result、结算状态)在 dim 层等于不存在**。

## 证据(2026-07-25 实测,date=2026-07-23 快照)
- `markets.csv` COUNT(*) = 400,000 整;top 前缀:MVESPORTS 331,833 /
  MVECROSS 60,892 / KXNASDAQ100U 2,800…
- 政治+选举 13 天报价宇宙 11,525 个 market_ticker,与 markets.csv 交集 = 41;
  与 catalog/settlements 交集 = 0(8,000 条 settlements 全是 MVE 腿)。
- `events.csv` 不受影响(分类分布正常,Politics 1,520 / Elections 3,232),
  说明是 market 分页被 MVE 淹没,不是抓取全坏。

## 影响
1. H6a-MV(结算校准)对政治宇宙 = NOT_ESTIMABLE(DATA_UNAVAILABLE),
   H6b 的 close 桶只能标 UNRESOLVED_CLOSE —— 结算真值全链路缺失。
2. 阶梯/strike 元数据缺失(11,525 个报价市场只 enrich 到 41)。
3. 每天 witness 盖章的 dim 快照,盖的是一份 98% 停车场废票的目录。

## 请求
- 抓取侧:markets 分页按 series/category 分片抓,或排除 MVE 系列
  (`KXMVE*`),或提高上限;settled 市场(status=settled + result)单独拉。
- 修好后回填最近窗口(07-10 起)各日 dim,重新 witness;研究侧即可解锁
  H6a-MV 与 close 桶结算标注。
- 本函不改任何现有收据;引用数据均为只读实测,可复核命令见
  strategy 线 session。
