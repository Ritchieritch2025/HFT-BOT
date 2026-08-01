# ROUND3 Addendum：Direct 账户 Centicent 手续费敏感性

本 addendum 适用于 sealed ROUND3 report：

```text
z3_price_allocation_round3_report.json
SHA-256 0ef11cdd374a75328663c315496d510d8d494f736ba988ebb84da1a8b01b1485
```

原 report、原 raw 和原 Markdown 均未修改；这是一个通过原 report SHA
单向绑定的 sidecar addendum，不把事后费用修正伪装成原预注册实验。

## 结论

账户特定的 direct-centicent 费用确实让历史 PnL 变好，但四个策略臂仍全部
为负，所有 market-cluster 95% CI 仍完整低于 0，四臂 `strict=false`。

**没有任何策略臂翻正；`deployable=false`。**

| Arm | 原 EV ¢/cycle | direct EV ¢/cycle | 改善 ¢/cycle | direct orphan PnL ¢ | direct q* | completion LCB95 | direct cluster CI95 ¢ |
|---|---:|---:|---:|---:|---:|---:|---:|
| A0 | -1.485507 | **-1.242735** | +0.242772 | -3.038962 | 0.740655 | 0.409146 | [-1.3564, -1.1122] |
| A1 | -1.319077 | **-1.102916** | +0.216161 | -3.194170 | 0.760078 | 0.470137 | [-1.2180, -0.9819] |
| A2 | -1.342745 | **-1.052762** | +0.289983 | -2.597223 | 0.711272 | 0.399727 | [-1.1796, -0.9225] |
| A3 | -1.507351 | **-0.931089** | +0.576262 | -0.973480 | 0.420786 | 0.013485 | [-1.0284, -0.8423] |

A3 改善最大，是因为 98.17% 的 cycle 是 orphan；但它的 completion LCB
仍只有 0.013485，远低于修正后的 break-even `q*=0.420786`。费用修正没有
改变“联合成交概率模型严重高估”的主结论。

三天逐日 EV 也全部为负：

| Arm | 2026-07-20 | 2026-07-21 | 2026-07-22 |
|---|---:|---:|---:|
| A0 | -1.061375 | -1.287317 | -1.316504 |
| A1 | -0.997399 | -1.105522 | -1.173096 |
| A2 | -1.003667 | -1.044971 | -1.105205 |
| A3 | -0.942253 | -0.974050 | -0.866356 |

## 账户实测契约

2026-07-26 authenticated production one-contract roundtrip probe 的 entry 和
exit 都在平均成交价 `$0.6800` 返回：

```text
average_fee_paid = $0.0153

0.07 × 1 × 0.68 × (1 - 0.68)
= $0.015232
ceil to $0.0001
= $0.0153
```

余额也逐项吻合：

```text
19.8205 - 19.4852 = 0.3353 = 0.3200 collateral + 0.0153 fee
19.7899 - 19.4852 = 0.3047 = 0.3200 collateral - 0.0153 fee
19.7899 - 19.8205 = -0.0306 = -2 × 0.0153
```

因此本账户 direct route 的 base trade fee 不能按“每单向上取整到 1 美分”
记账。该 roundtrip 两腿都是 taker；它用于识别 taker fee 精度，不改变
策略中 resting maker entry 仍按零 maker fee 处理的假设。

本敏感性严格采用用户指定的账户公式：

```text
fee_direct_$ =
  ceil_to_$0.0001(
    Σ_fill 0.07 × quantity × p × (1-p)
  )
```

所有价格和数量分别由精确整数 `price_e4/10000`、
`qty_e4/10000` 转为 `Decimal`，使用 50 位十进制上下文。

## 动作与成交冻结证明

本次没有重新选择策略动作。对 sealed raw 中每一个 orphan：

1. 固定原 arm、cycle index、market、date、first fill、entry、exit reason
   和 `exit_ts`。
2. 按 ROUND3 的 receive-clock 因果契约重建 `exit_ts` 前的盘口。
3. 固定 IOC route 和原 book walk，用旧 whole-cent fee 重新计算 PnL。
4. 只有旧 PnL 在绝对误差 `1e-9` 内逐笔匹配 sealed PnL，才替换费用。
5. 任一不匹配、日期缺失、paired 改变或 fee delta 越界都会使结果作废。

审计结果：

- **4,401 / 4,401** 个 orphan 的旧 PnL 逐笔 exact match。
- 每个 orphan 恰好对应一个 IOC flatten。
- 所有 paired cycle byte-for-byte 不变，paired PnL delta 全部为 0。
- 2026-07-20、21、22 三天完整；72 个 clean strategy market-days，
  invalid market-days 为 0。
- 本次未读取 2026-07-23。
- 除 `cycle.pnl_c` 与不参与决策的 fee-dependent
  `cycle.trigger_est_pnl_c` 外，完整 raw 投影的两份 SHA 相同：

```text
sealed projection
56823a8ab4f4ae71c3a3a80c955869b2b135ae9e04739842c3773df2ef963fbc

sensitivity projection
56823a8ab4f4ae71c3a3a80c955869b2b135ae9e04739842c3773df2ef963fbc
```

唯一一笔 YES/NO entry 价格相同、仅靠价格无法判方向的 cycle，使用相同
`first_ts` 的因果 trade 明确消歧：trade id
`5d0c82f0-bef2-7c6d-59c2-db56efdef88a`、`taker_side=no`、
`yes_price_e4=1100`，因此被成交的是 YES bid。该证据也写入逐笔 audit。

这不是人工硬编码后直接放行。repricer 会读取并校验唯一原始 trade row：

```text
/home/ubuntu/hft-bot/work/warehouse/facts/trades/category=Crypto/
subcategory=BTC/date=2026-07-22/trades__Crypto__BTC__2026-07-22.csv.gz

SHA-256
0ceefe810a3206992da85204967001a3c4b3d68617cbd3f369da381925b27700
```

脚本程序化断言文件 SHA、row cardinality=1、market、trade id、`first_ts`、
`recv_wall_ns`、`recv_mono_ns`、receive-clock identity、price、quantity 和
`taker_side`。ROUND3 的 sealed fill mapping 是
`YES resting bid <- taker_side=no`、`NO resting bid <- taker_side=yes`；
所以这笔 `taker_side=no`、YES price=1100 的交易击中 1100 YES bid，
第一腿持仓只能是 YES。任一源字段或文件 SHA 改变都会 result void。

## 实际费用变化

| Arm | orphan 数 | 费用节省合计 ¢ | 每 orphan 平均节省 ¢ | 最大节省 ¢ | partial IOC | multi-level IOC |
|---|---:|---:|---:|---:|---:|---:|
| A0 | 637 | 275.06 | 0.431805 | 0.98 | 1 | 11 |
| A1 | 636 | 273.66 | 0.430283 | 0.99 | 1 | 9 |
| A2 | 985 | 495.00 | 0.502538 | 0.99 | 13 | 10 |
| A3 | 2,143 | 1,257.98 | 0.587018 | 0.99 | 6 | 32 |

在得到精确结果前，结论已经可由严格上界锁死。whole-cent ceiling 改为
centicent ceiling，每个 orphan IOC 最多改善 0.99¢，paired 不变：

```text
EV_new <= EV_old + orphan_rate × 0.99¢
```

四臂最乐观 EV 上界分别为 A0 -0.928905、A1 -0.821731、
A2 -0.771480、A3 -0.535491¢/cycle，全部仍小于 0。相同界也证明
completion/q* gate 和 cluster-CI gate 不可能翻为通过。

## 官方文档与旧假设的关系

当前 [Kalshi Fee Rounding API 文档](https://docs.kalshi.com/getting_started/fee_rounding)
明确区分：

- direct member 目标余额精度为 `$0.0001`；
- non-direct member 目标余额精度为 `$0.01`；
- base trade fee 先向上取整到 `$0.0001`。

当前 [2026-07-07 Fee Schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
也把 general fee 写为 `0.07 × C × P × (1-P)`，并说明 fee 与 position
cost 按 centicent 对齐。因此 authenticated direct receipt 与当前
route-specific 文档一致。

真正的冲突是：sealed ROUND3 沿用了旧的 generic whole-cent 假设，并把
通用的一合约整分展示表当成了 direct API 响应契约。该假设现在由本
addendum 明确覆盖，但原 sealed 文件保持不变以保护证据链。

范围限制：真实 probe 是一张整合约、单一价格。当前 API 文档说明，当一个
IOC 被拆成 fractional/multi-price fills、余额变化精度超过 `$0.0001` 时，
还可能出现 route-specific rounding component 和 accumulator/rebate。
本敏感性按指定的“每 IOC 聚合后 centicent ceiling”公式计算，没有凭一笔
probe 猜测多笔 fill 的额外组件。由于数据里确有少量 multi-level/partial
IOC，启动时仍必须用真实响应对账。

## 上线前强制自检

每次启动和每个 `exchange_index` 都必须：

1. 从最近一笔受控 taker fill 读取 exact quantity、average fill price、
   `average_fee_paid` 和 position `fees_paid_dollars`。
2. 用 `Decimal` 同时计算 direct-centicent、non-direct-cent 与当前
   fee schedule 候选。
3. 用 `balance_dollars`、collateral/exposure、成交 proceeds 做前后余额
   恒等式核对。
4. 只有一个模型逐字段匹配时才启动；无法唯一匹配或路由改变立即 halt。
5. 对 fractional 或 multi-fill order，必须核对 order 级累计费用、
   rounding component 和 rebate，不能从单 fill 外推。

## 证据与哈希

| Artifact | SHA-256 |
|---|---|
| 原 sealed report | `0ef11cdd374a75328663c315496d510d8d494f736ba988ebb84da1a8b01b1485` |
| 原 sealed raw | `a9bca54a22b0ae06076bd81192538a23d01058e965e0961bddaeae24ee9d3b90` |
| 原 Markdown | `2ea22a3b101f1b76eb9024bb784ef89e3b1cf7ce78735f6fc51709e35a04087c` |
| authenticated account probe | `816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6` |
| ambiguous-side source trade fact | `0ceefe810a3206992da85204967001a3c4b3d68617cbd3f369da381925b27700` |
| sensitivity JSON | `29f70c2822f40ca79eb0b3cc431efa29ed950decfd6a38b0d89f402dbc64de2c` |
| adjusted raw | `46af89c62fb75f665188e766afac98f2059b969d255301a076ddaabda3b769ad` |
| 4,401-cycle reprice audit | `7e4243cb1ff728d7c6f4117988c80747e2c0188b84495102b1ee81d5fa37c373` |
| targeted repricer script | `ff8ccc9a483e19b33db601333dc9a14c7f81c01f57926396a4fa298b8dd4a7bf` |
| metric/report script | `b64f3192ace993021911a6b634e398e66d4eb40affb6ec423a201d2068672550` |

最终解释仍是 `DISCOVERY_ONLY`、`historical_validation_claim=false`、
`deployable=false`。费用账本应修正，但这次修正没有救活任何策略臂。
