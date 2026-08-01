# ROUND3 因果回放：最终 DISCOVERY 报告

生成日期：2026-07-26  
结论：`NO_CANDIDATE`  
部署状态：`deployable=false`

## 1. 结论先行

本轮只读取 2026-07-20、2026-07-21、2026-07-22，三天全部属于
`DISCOVERY`。四个策略臂全部未通过预注册的严格门槛；没有任何历史
`VALIDATE` 结果，也没有可部署候选。

- 最好的历史均值仍为负：A1 为 **-1.319077¢/cycle**。
- 四个策略臂的 market-cluster 95% CI 下界和上界都小于 0。
- A3 的模型预测最乐观（proxy **+4.683737¢/cycle**），实际却为
  **-1.507351¢/cycle**，是最明显的概率校准失效。
- 本轮没有读取 2026-07-23；下一份有效证据只能来自模型和代码封存后的
  真正 forward holdout。

因此，这份结果不能作为开实盘或放大资金的依据。

## 2. 为什么 ROUND2 会出现负盘口

ROUND2 使用 `(ts_utc, ws_seq)` 排序，而采集器已经明确规定
`ts_utc = COALESCE(exchange_ts_us, local_recv_ts_us)` 仅用于旧系统兼容，
不是可交易的回放时钟。混用交易所时间与本地接收时间，会把后来收到的
delta 排到 snapshot 前面。

在真实市场 `KXBTC15M-26JUL192315-15` 上，对同一组 422,564 个事件：

| 排序方式 | snapshot 前 delta | 负数量事件 | 接收顺序回退 |
|---|---:|---:|---:|
| 旧 `(ts_utc, ws_seq)` | 3 | 1,120 | 1 |
| 因果 `(recv_wall_ns, recv_mono_ns, ws_seq)` | 0 | 0 | 0 |

原始 L2 receipt 同时显示 seq gap、missed、regression、lost frame 和 parse
error 全部为 0。因此根因是回放排序错误，不是这段数据真的缺包。

## 3. ROUND3 因果与可执行性契约

本轮的 book 和 trade 决策都只使用本地接收字段
`recv_wall_ns`、`recv_mono_ns`、`local_recv_ts_us`，并强制
`local_recv_ts_us == recv_wall_ns // 1000`。`ts_utc` 和
`exchange_ts_us` 禁止用于事件顺序、决策、流量窗口或 TTE。

- 合并顺序为
  `(recv_wall_ns, recv_mono_ns, channel_priority, ws_seq_or_sentinel, stable_id)`。
- 同一个接收 envelope 内先处理 BOOK、再处理 TRADE。
- snapshot 完整重锚 book、`ws_sid` 和 `ws_seq`。
- delta 必须有先前 snapshot、保持同一 `ws_sid`，且 `ws_seq` 严格递增。
- 数量更新为 `next = prior + delta`；`next < 0` 使整个 market-day 失效，
  `next == 0` 删除价位，禁止把负数 clamp 成 0。
- 每日原始 L2 receipt 是 gap 权威；receipt 不绿则整天中止。
- 报价必须在真实合法价格网格上，严格低于当前对应 ask，且
  `p_yes + p_no <= 99¢`。
- 排队 ahead 只取报价精确价位的实际显示数量；更优价位深度只进入 ETA
  的 barrier，不伪装成实际排队 ahead。
- 成交采用严格 trade-through：累计合格主动成交量必须
  `> exact_level_ahead + clip`，相等不算成交。
- 撤单 ACK 固定延迟 60ms；撤单 pending 期间仍允许被成交。
- 双 maker 配对进入按零 maker fee；孤腿 IOC 退出使用官方 7% 二次式
  taker fee，并向上取整到 1 美分：
  `fee_$ = ceil_cent(0.07 × contracts × p × (1-p))`，其中 `p` 以美元计。

残余假设：若 book 与 trade 拥有完全相同的纳秒接收 envelope，回放按
预注册规则先 BOOK 后 TRADE。真实 wire 内部先后在这种情况下不可观测，
所以这是一项可能偏乐观的 tie-break 假设，不是已被数据证明的事实。
60 秒窗口和 TTL 的基准时间精度为微秒，但事件合并顺序仍保留纳秒精度。

## 4. 策略与公式

四个策略臂共享 1 张 clip、60 秒 TTL、距离 touch 落后 2¢ 时退出孤腿，
以及相同的成交、撤单和费用模型。

| 策略臂 | 价格分配规则 |
|---|---|
| A0_COMMON_TOUCH | 两边都挂当前 touch；只有合法、post-only 且总价不超过 99¢ 才进入 |
| A1_SPEND_ALL_SLOW | 快腿保留 touch，把所有可用 pair slack 加到预测较慢的一腿 |
| A2_FULL_MINIMAX_ETA | 穷举完整合法组合，最小化两腿 `max(ETA)` |
| A3_FULL_EV_GATE | 穷举完整合法组合，最大化冻结的 proxy EV，且只在 proxy EV > 0 时进入 |

使用的核心公式：

```text
ETA_side =
  (better_price_barrier + exact_level_ahead + clip)
  / (causal_executable_taker_flow_60s / 60)

q_side = 1 - exp(-60 / ETA_side)
q_both = q_yes × q_no

G = 100¢ - p_yes - p_no
EV_proxy = q_both × G - (1 - q_both) × 3.5¢

EV_empirical =
  completion × mean_pair_gain
  + (1 - completion) × mean_orphan_pnl

q* = L / (G + L)
```

最后一个式子中，`L` 是孤腿平均亏损的正数绝对值，`G` 是配对平均收益。
严格选择门槛为：cycles ≥ 30、markets ≥ 10、Wilson 95% completion LCB
高于经验 `q*`、market-cluster bootstrap 的 mean-PnL 95% CI 下界大于 0、
策略 bug 为 0、且所有原始数据 receipt 通过。

## 5. 三天合计结果

`orphan PnL` 为负数；`capital mean` 是 **first fill → exit**，不是
admission → exit。

| 策略臂 | cycles | mkts | paired / orphan | completion | LCB95 | q* | pair gain ¢ | orphan PnL ¢ | realized EV ¢/cycle | cluster CI95 ¢ | proxy EV ¢ | capital mean s | strict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A0 | 1,133 | 58 | 496 / 637 | 0.437776 | 0.409146 | 0.765349 | 1.064113 | -3.470768 | -1.485507 | [-1.6023, -1.3530] | +0.366142 | 2.576325 | false |
| A1 | 1,266 | 58 | 630 / 636 | 0.497630 | 0.470137 | 0.782362 | 1.008254 | -3.624453 | -1.319077 | [-1.4408, -1.1906] | +0.463100 | 2.479828 | false |
| A2 | 1,707 | 66 | 722 / 985 | 0.422964 | 0.399727 | 0.746201 | 1.054294 | -3.099761 | -1.342745 | [-1.4793, -1.2046] | +0.477480 | 4.302683 | false |
| A3 | 2,183 | 62 | 40 / 2,143 | 0.018323 | 0.013485 | 0.538010 | 1.340000 | -1.560498 | -1.507351 | [-1.5936, -1.4297] | +4.683737 | 1.313742 | false |

排序只是负收益之间的相对排序：A1、A2、A3、A0。它不表示 A1 合格。

## 6. 各 DISCOVERY 日期结果

| 策略臂 | 日期 | cycles | paired | completion | realized EV ¢ | pair gain ¢ | orphan PnL ¢ | first-fill→exit mean s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A0 | 2026-07-20 | 272 | 120 | 0.441176 | -1.305493 | 1.040000 | -3.157197 | 3.313412 |
| A0 | 2026-07-21 | 486 | 207 | 0.425926 | -1.537029 | 1.064734 | -3.467369 | 2.291484 |
| A0 | 2026-07-22 | 375 | 169 | 0.450667 | -1.549304 | 1.080473 | -3.706743 | 2.410847 |
| A1 | 2026-07-20 | 298 | 146 | 0.489933 | -1.215755 | 1.003425 | -3.347336 | 3.118444 |
| A1 | 2026-07-21 | 540 | 265 | 0.490741 | -1.328837 | 1.012453 | -3.584989 | 2.331589 |
| A1 | 2026-07-22 | 428 | 219 | 0.511682 | -1.378703 | 1.006393 | -3.877919 | 2.222216 |
| A2 | 2026-07-20 | 448 | 185 | 0.412946 | -1.300431 | 1.034054 | -2.942559 | 4.918819 |
| A2 | 2026-07-21 | 731 | 316 | 0.432285 | -1.334451 | 1.073101 | -3.167672 | 3.742535 |
| A2 | 2026-07-22 | 528 | 221 | 0.418561 | -1.390129 | 1.044344 | -3.142632 | 4.555409 |
| A3 | 2026-07-20 | 687 | 12 | 0.017467 | -1.502617 | 1.308333 | -1.552590 | 1.275745 |
| A3 | 2026-07-21 | 828 | 14 | 0.016908 | -1.570728 | 1.371429 | -1.621330 | 1.281316 |
| A3 | 2026-07-22 | 668 | 14 | 0.020958 | -1.433662 | 1.335714 | -1.492945 | 1.393013 |

每一天、每一个策略臂的实际 EV 都为负，未出现依赖某一天正收益掩盖其他
日期的情况。

## 7. A3 为什么错得最明显

A3 冻结的孤腿 loss prior 是 3.5¢：

- 相比本次历史回放的 A3 经验孤腿亏损 1.560498¢，3.5¢ 是其 **2.24×**，
  单看这份历史样本反而偏保守。
- 但最新 fresh shadow forced mean 为 15.15¢；3.5¢ 仅是它的 23.1%，
  少估 11.65¢、即 **76.9%**。fresh shadow 是冻结 prior 的 **4.33×**，
  也是本次历史回放经验值的约 **9.71×**。

即便历史 loss prior 偏保守，A3 仍从 proxy **+4.683737¢** 变成实际
**-1.507351¢**。主因是：

```text
预测 mean q_both = 0.762610
实际 completion = 0.018323
```

也就是填单概率/联合概率校准和由此产生的动作选择严重失真，而不是只把
3.5¢ 调大一点就能修好。同时，fresh shadow 又说明孤腿亏损分布非平稳，
把 3.5¢ 继续用于在线决策也很危险。

## 8. 数据质量、动作空间和等价性

| 日期 | book rows | trade rows | 策略市场日 | snapshot / reanchor | invalid / rollback |
|---|---:|---:|---:|---:|---:|
| 2026-07-20 | 7,896,432 | 2,213,973 | 24 | 72 / 48 | 0 / 0 |
| 2026-07-21 | 8,842,044 | 2,390,834 | 24 | 72 / 48 | 0 / 0 |
| 2026-07-22 | 7,967,427 | 2,042,643 | 24 | 72 / 48 | 0 / 0 |

三天共 72/72 个 clean strategy market-days；所有 daily raw L2 receipts
通过，所有必为零的 gap/loss/parse 指标均为零。四个策略臂的 `bugs`
数组均为空，非法网格、pair cost > 99¢、非正 ETA、负 barrier/ahead、
A3 选择非正 proxy 的次数均为 0。

完整 Cartesian 价格组合为 77,841 个，合法且 pair sum ≤ 99¢ 的组合为
37,071 个。加速算法没有删掉可行动作。合成测试中 A2/A3 各 12 个状态与
朴素穷举逐点完全一致，并通过 9.9↔10、90↔90.1 网格边界测试。真实旧样本
前缀包含 281,241 个 book 事件和 41,543 笔 trade，A2 和 A3 各 8/8 个
状态与朴素穷举逐点一致。

## 9. 没有读取 2026-07-23 的证明

- 预注册文件把 2026-07-23 放入 `forbidden_dates`。
- 最终 report 与 raw 均记录
  `this_run_read_2026_07_23=false`、
  `historical_validation_claim=false`、`deployable=false`。
- 本轮 admissions/cycles 的唯一日期恰好是
  2026-07-20、2026-07-21、2026-07-22。
- 所有实际 source fact path 只包含上述三天，0 个 path 指向
  `date=2026-07-23`。
- raw L2 receipt 中的 `l2_23.ndjson` 表示当天第 23 小时的文件，
  不是 7 月 23 日。

因此本轮只有 DISCOVERY 数字；`VALIDATION = NONE`。

## 10. 封存证据

| 文件 | SHA-256 |
|---|---|
| `z3_price_allocation_round3_report.json` | `0ef11cdd374a75328663c315496d510d8d494f736ba988ebb84da1a8b01b1485` |
| `z3_price_allocation_round3_raw.json` | `a9bca54a22b0ae06076bd81192538a23d01058e965e0961bddaeae24ee9d3b90` |
| `z3_price_allocation_round3.py` | `293ab66c1ce753dde7893e481d06032c74f87594bd0cffac47f0a53043fa64da` |
| `z3_price_allocation_round3_prereg.json` | `0131bcb839e97b5feffc56a1a3fc7e79bd090cd1bf301fa3d000542ef63c402c` |
| `z3_price_allocation_round3_equivalence.json` | `d06b0396694e1014afe2f06da776f757822cad77c0f4a2442d0331edf7d3b753` |
| `causal_replay_contract.py` | `4b7a3379a69aba3ea95361294db4ffea775791f56942f5c10818b14b18c04afd` |
| `round3_source_diag_2026-07-20_2315.json` | `9e174c262b7ae4d9a94c5995e9703ac6ece77c3334130fc3cb5648508b7884e6` |

下一步的最低有效证据不是继续重切这三天，而是先冻结新的概率/损失模型和
完整代码，再收集从未参与设计的 forward holdout。当前四个策略臂均不应
进入实盘。
