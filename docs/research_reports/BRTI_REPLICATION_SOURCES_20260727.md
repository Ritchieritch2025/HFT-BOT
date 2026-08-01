# BRTI 复算器 — 方法论出处与实现依据(2026-07-27)

## 权威出处(按优先级)
1. **现行方法论(必须以此为准)**: CF Benchmarks《CME CF Real Time Indices Methodology》
   https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf
2. **现行成分所名单**: https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf
3. 历史版本(公式骨架相同,参数已过时): CME BRTI Methodology v2 (2017)
   https://www.cmegroup.com/trading/files/bitcoin-real-time-index-methodology-version-2.pdf
4. BRTI 官方页: https://www.cfbenchmarks.com/data/indices/BRTI

## 算法(已确认的部分,来源=上述 1/3)
每秒一次:
1. 取各成分所 Relevant Order Book,合并为 consolidated book;
2. **动态单量帽** C_T = trimmed_mean + 5·winsorized_σ(两侧各取最优 50 档的 size 样本,
   1% 截尾)——注意 2017 版是固定 100 BTC,已废弃;
3. 按整数 BTC 粒度(s=1)构造 bid/ask/mid 价格-量曲线,midSV(v)=ask(v)/mid(v)−1;
4. **利用深度** v_T = max{v: midSV(v) ≤ 0.5%},最小为 1;
5. mid 曲线按指数分布密度加权(归一化),BRTI = Σ_{v=1..v_T} midPV(v)·w(v)。

## 待一手确认的参数(上线前必须核对第 1 号 PDF 原文)
- **λ 数值与形式**:二手来源(Grokipedia)称 λ=10.3。⚠ 若 λ=10.3 且 v 以 BTC 计,
  权重在 v=1 之后衰减到 ~3e-5,等价于"只看 1 BTC 深度的 mid"——数值上可疑,
  可能实际是 λ 随 v_T 缩放(如 λ=10.3/v_T)。**处理**:复算器两种模式都实现
  (env 切换),验证器对官方值做 λ 网格搜索,让数据裁决;同时 agent 用浏览器
  读 1 号 PDF 原文核对 λ 定义,双保险。
- 熔断细则:延迟>30s 剔除该所;book 缺一侧/不可解析剔除;单所 mid 偏离全体中位
  数>25% 剔除;全部剔除→该秒不发布。(v2 文档已确认,现行版需复核数值未变)

## 成分所覆盖(免费公开 WS 行情)
| 所 | 公开流 | v0 状态 |
|---|---|---|
| Coinbase | ws-feed.exchange.coinbase.com level2_batch | ✅ 已实现 |
| Kraken | ws.kraken.com book | ✅ 已实现 |
| Bitstamp | ws.bitstamp.net order_book(top100) | ✅ 已实现(深度有限,标注) |
| Gemini | api.gemini.com/v2/marketdata l2 | ✅ 已实现 |
| Crypto.com | 公开 book 频道 | ⬜ v0.1 适配器留槽 |
| itBit/Paxos | 公开 REST/WS | ⬜ v0.1 留槽 |
| Bullish | 公开行情 | ⬜ v0.1 留槽 |
| LMAX Digital | ❌ 无免费公开流 | 永久残差来源,量化进误差预算 |

## 验收(和其他一切同一纪律)
- 离线并录 ≥6h:synthetic vs 官方 RTI 捕获,报误差分布(中位/p95/最大,$);
- λ 网格 + cap 实现的敏感性报告;
- **中位绝对误差 < $5 且 p95 < $15** → 有资格进 B 段定价锚候选;
  达不到 → 保持统计 β,复算器降级为研究工具。
- 误差分布必须按波动状态分层(平静/爆发)——爆发时误差大才是真正的失格。
