# 操作员指令:尾部区报价策略 + σ快撤(2026-07-26T15:10 本地)

给 build 线(mm_engine)。操作员裁决:尾部区**不关**,要赚波动的钱;
但趋势不对必须立刻撤。翻译成三个引擎改动,全部env门控、默认关、先影子:

## 1. MM_FAST_PULL:σ挂钩快撤(优先级最高)

每个数据tick对每张resting挂单评估:

```
adverse_move_c = (挂单时fair_c − 当前fair_c),按对该单不利方向取正
threshold_c    = max(该单入场时的edge_c, MM_PULL_K · σ_c · sqrt(age_s))
adverse_move_c > threshold_c  →  立即order_cancel(reason="FAST_PULL")
```

- σ_c = pricing_state当前sigma换算成"每√秒美分"(与fair同口径)
- MM_PULL_K 默认 2.5
- **快撤优先于 MM_MIN_QUOTE_AGE_S 和 MM_MIN_REQUOTE**(与F5"队列年龄不得
  凌驾于关闭时区"同一原则;需要新guard测试:快市中年龄保护不得挡撤单)
- 挂单时需在订单槽记录 fair_at_place_c 与 edge_at_place_c(receipts里也要,
  便于事后markout按此分组)
- 15¢ sentinel保留为冗余备份

## 2. 尾部区侧向不对称(120–300s极端区,以及全时区价<20¢/>80¢的单)

依据:BRTI 1s增量|z|>4频率为正态224倍(18.5h, n=66488);买便宜侧=做多凸性,
最大亏损=入场价,肥尾利好;买贵侧=卖尾部保险,肥尾致命。

- MM_TAIL_CHEAP_ONLY=1:尾部区只挂"便宜侧"(入场价<50¢的那侧的买单;
  即YES<20¢时只做bid,YES>80¢时只做ask_no)
- MM_TAIL_EXPENSIVE_MARGIN_C(若不想全关贵侧):贵侧额外加价,默认+2.0¢
- 便宜侧库存**不需要**趋势出场(亏损天生封顶),不要给它加IOC路径

## 3. MM_MARGIN側向拆分(已口头列过,正式登记)

MM_MARGIN_BID / MM_MARGIN_NO,缺省回落到MM_MARGIN。
依据:markout bid −1.76¢@5s vs ask_no −0.12¢@5s(n≈800/侧,单日,需多日复核)。

## 验证路径说明

grid_replay_v2报价窗是10–30min,**覆盖不到120–300s尾部区**,所以#2无法回放验证,
直接上影子(改动后的引擎跑shadow,与现行影子并行对照)。#1可在影子对照中直接
读FAST_PULL触发次数与被扫单率变化。#3回放可测(front/tail臂已分侧)。

—— 操作员线,依据数据:markout分析、尾部超越率、方差比检验(见当日会话与
docs/MM_CONFIG_REFERENCE_2026-07-26.md)
