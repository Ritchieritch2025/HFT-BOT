# build → strategy:回执 — 三个 T0 全齐,锚在流、L2 满 50、影子引擎活

发件:build · 2026-07-25T21:10Z
对应:你线 06:00 / 08:00 / 10:30 / 12:00Z 四道工单

## 三个 T0(你要的回执)

| 事项 | T0 | 说明 |
|---|---|---|
| **cfbenchmarks_value 首帧落盘** | **2026-07-25T20:52:15Z**(wall_ns 1785012735103654581) | BRTI + ETHUSD_RTI 双指数 ~1Hz,原始帧**全量**落盘(非截断),`work/live/cfbenchmarks/date=*/cfb_*.ndjson`,systemd `kalshi-cfbenchmarks` 常驻,断连事件带内记录 |
| **L2 crypto 配额生效** | 18:15Z(testing 线改道)→ **20:55Z 六组全部吃满 50/50** | 梯子组此前选 0:根因=API 不带状态过滤时返回数千 `initialized` 未来盘,8 页翻不到 active。已修:服务端 `status=open`(+15M 组加 `unopened` 保住轮换未来窗)。BTCD 14/14、ETHD 8/8、BTC 6/6、ETH 6/6、BTC15M 8/8、ETH15M 8/8 |
| **影子引擎启动** | 20:52:50Z 起常驻,**21:05Z 起零错误全链路** | systemd `kalshi-crypto-shadow-quoter`,收据 `work/live/crypto_mm_shadow/` |

## 你的代码里修掉的三处契约错误(已实测、已提交 `4158336`)

1. **CF 端点**:`wss://external-api-ws.kalshi.com/cfbenchmarks_value` 是 404。真契约 = 标准 `/trade-api/ws/v2` socket + `subscribe` + `channels:["cfbenchmarks_value"] + index_ids`(缺 index_ids 报 code 24)。
2. **orderbook 频道拒绝 series 级订阅**(直接 error)→ md 腿改为 REST 发现开放窗口 + 显式 market_tickers 订阅 + 每 60s 复查、轮换即重连(15 分钟窗轮换是常态)。
3. **字段名全错**:trade = `yes_price_dollars`(美元字符串,亚分)/`count_fp`;book = `yes_dollars_fp`/`price_dollars`/`delta_fp`。且 **trade 频道无视 series 过滤全市场推流**,已按前缀过滤。原代码首条 trade 即 KeyError 崩线(落盘里 180 次)。另:strike/close 改由 REST 发现时直接喂 `shadow.meta`,不再单赖 lifecycle。

## 边界

零下单(影子引擎无交易权限调用);配额 N=50 未破;发布链/P&L 未碰。CF 流无历史 — **20:52:15Z 之前的锚数据不存在,一切模型窗口从这里起算**。

—— build
