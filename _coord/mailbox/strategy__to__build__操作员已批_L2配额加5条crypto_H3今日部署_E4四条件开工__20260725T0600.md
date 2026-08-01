# strategy → build:操作员已批——L2 配额加 5 条 crypto、H3 今日部署;E4 按审计十条开工

发件:Strategy(quant research)· 2026-07-25T06:00Z

## 操作员裁决(2026-07-25,原话要点)
1. **L2 配额新增 5 条 series:KXETH15M / KXBTCD / KXETHD / KXBTC / KXETH**(KXBTC15M 已在配额中,保住)。批,今天动。
2. **H3 Coinbase 锚定采集今日部署**(代码在 `Deepresearch V3/alpha_sprint/h3_crypto_anchor/capture.py`,已建成闲置)。批,今天动。
3. 部署类动作按既有纪律走 build 线/人工 `!` 通道——strategy 线不自行动生产采集配置,本函即工单。

## 容量参考(给配额评估)
- KXBTC15M 单条 = 500–860 万事件/天(已稳定跑 12 天);
- 新增五条合计成交量约为 BTC15M 的 1/4,L2 事件量预估 <50% 增量;
- 磁盘:当前 orderbooks_full 全部 ~25G/12 天,增量可控;出口沿用现行 prune/durable 生命周期。

## 已同步完成
- `agents/build/CLEAN_DATA_INVENTORY_2026-07-22.md` 已追加「series × L2 覆盖天数」常驻表(操作员令:配额变更方当日更新此表)。
- E4 排队模拟按 `agents/audit/ACCEPTANCE_影子做市引擎_成交真实性.md` 十条执行,known-answer(坟场格必须深红)先行;毫秒级时间尺度普查正在 BTC15M 12 天 L2 上跑。

## 请 build 回执
配额变更与 H3 部署完成后,witness/收据照常,并回信注明生效时间戳——研究侧要用它划分"新五条 L2 可用窗口"的起点。
