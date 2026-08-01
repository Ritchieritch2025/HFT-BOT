# strategy → build:采集工单增补——cfbenchmarks_value 通道优先接入,Coinbase 锚降为第二优先

发件:Strategy(quant research)· 2026-07-25T08:00Z
关联:`strategy__to__build__操作员已批_L2配额加5条crypto_H3今日部署_E4四条件开工__20260725T0600.md`

## 增补内容
操作员提供官方文档(docs.kalshi.com/websockets/cfbenchmarks-value):
Kalshi 官方 WS `wss://external-api-ws.kalshi.com/cfbenchmarks_value` 直推
**结算指数本尊**(BRTI / ETHUSD_RTI,~1Hz)+ 滚动 60s 均值 +
**每刻钟收盘前最后一分钟的结算 TWAP 实时累计值**。

## 请调整的执行顺序
1. **第一优先:接 cfbenchmarks_value**。订阅 `["BRTI","ETHUSD_RTI"]`,
   原始帧全量落盘(带 recv_wall_ns + recv_mono_ns,沿用现行 firehose 落盘纪律),
   API key 鉴权用生产机现有 Kalshi key。这是定价模型的唯一正确锚(合约按它结算)。
2. 第二优先:原 H3 Coinbase 采集照部署——新角色是**领先性研究**
   (现货领先 RTI 聚合值多少 ms = 毒单的提前量),不再是主锚。
3. L2 配额加五条不变。

## 研究侧后续用途(供理解,不需 build 动作)
- 早段定价:P(up) = f(RTI 距行权线距离, RTI 短波动率);
- 最后一分钟:官方直推部分锁定 TWAP,剩余不确定性成纯算术——暂只用于研究,
  T−5m 撤退纪律不因此放松。

请在回执里给两条流各自的生效时间戳。
