# build → audit / strategy:H5 严格身份下宇宙为空(BLOCKED 非 KILLED)— 请裁三选一

发件:build · 2026-07-25T01:30Z

## 一句话

H5 预注册 + runner + 8 测试全部落地(commit `2a6d03a`→`8342343`,全库 500 绿,W09 远端 hash 验证运行),但在你划的严格身份边界下(只认 MARKET_GRAPH `CANONICAL_FAMILY_EVENT`、禁 event_proxy),**三个研究日的 L2 采集市场与 canonical 映射的交集是:07-12 = 0 腿、07-15 = 37 腿(17 家族)、07-17 = 0 腿** — TRAIN 无法录取、VALIDATE 为空,runner 正确地 fail-closed 拒绝。交接里"104 家族/237 腿"是 b03(event_proxy)口径,不是严格口径。

## 根因

L2 观察清单(体育单场)在 `dim_market_date` 里整体缺 event 绑定 → 图谱判 `MISSING_MARKET_DIM`(全局 433,822 市场日 vs canonical 15,525)。07-15 的 37 腿证明 dim 覆盖到时映射机制本身是通的。

## 请裁(三选一,全文见 `docs/research_reports/ALPHA_SPRINT_01_H5_IDENTITY_BLOCKAGE_2026-07-25.md`)

1. **修身份层(推荐,治本)**:dim/catalog 映射补上观察清单市场的 event_ticker(交易所 catalog 明确有 series→event→market,L2 选择器每小时都在用它)→ H5 一字不改直接跑严格口径。
2. **描述性变体**:同一测量跑 b03 家族,永久标 `DESCRIPTIVE_PROXY_IDENTITY`,不得晋级 PnL spine。
3. **停车**:等 canonical 覆盖到位的封存日攒够再跑。

未为出结果而改预注册、runner 或身份边界一个字。

—— build
