# KXBTC15M ROUND4：两阶段条件补齐 Hazard 设计预注册

日期：2026-07-26  
实验 ID：`KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1`  
状态：**DESIGN_AND_SCHEMA_PREREG / ACTION_SET_PENDING / NO_CANDIDATE**  
权限：只允许离线 discovery、代码测试和后续 sealed forward shadow；**不授权实盘**

修订：**R1 / PRE-DATA P1 CONTRACT FIXES / 2026-07-26**。R1 在任何真实
ROUND4 数据被模型读取前锁定以下更正：

1. `DATA_INVALID` 不是 censor、负样本或 risk-set terminal；出现即整
   market-day、所有 action 一起回滚；
2. 同一 causal envelope 内的 complement fill 是独立的 `t=0` atom，不得
   伪造成 epsilon-duration interval；
3. source path 中出现请求日期以外的任意日期 token 都在 open 前拒绝；
4. economic rows 必须通过 `cycle_policy_outcome` 主键、terminal 和账本守恒；
5. Stage-1 必须分 cause 报 RCLL，且 parametric/AJ 方向冲突为
   `MODEL_MISSPECIFIED`。

本文件只对 ROUND4 生效，并取代旧动态 EV 文档中涉及 ROUND4 的数据 split、
`q_Y*q_N` 和具体 action-arm 约定；旧文档不再为 ROUND4 提供候选。

ROUND3 诊断来源：

```text
tmp/crypto_mm_canary_20260726/round3/
  z3_price_allocation_round3_report.json
SHA256:
  0ef11cdd374a75328663c315496d510d8d494f736ba988ebb84da1a8b01b1485
```

## 0. 本文件现在锁什么、不锁什么

现在锁定：

1. 数据日期和禁用日期；
2. 两阶段事件定义、时钟、右删失和 competing-risks 公式；
3. `ENTRY -> FIRST_FILL -> KEEP/REPRICE/IOC -> TERMINAL` 状态机；
4. 因果可用特征白名单；
5. 数据表主键、字段、单位和守恒检查；
6. `EV/cycle` 与 `EV/capital-time` 的报告公式；
7. action-set seal 和最终 forward 裁决流程。

现在**不锁定**具体候选：

- 不从 ROUND3 的 A0–A3 中递补候选；
- 不锁 entry 价格偏移集合；
- 不锁 first-fill 后的决策时间网格；
- 不锁 KEEP 等待长度、REPRICE 目标价格、IOC 路线/限价；
- 不宣称新模型有效。

这些具体动作只能在 `2026-07-20..22` 的 clean post-fill discovery 表完成并通过
本文件的数据契约后，写入独立的 `ACTION_SET_SEAL`。在该 seal 之前不存在可进入
forward 的策略。

## 1. ROUND3 只提供失败诊断

ROUND3 把两边各自的机械 ETA 概率相乘：

```text
q_both = q_Y × q_N
```

其 discovery 结果显示：

| Arm | mean predicted `q_both` | actual pair completion |
| --- | ---: | ---: |
| A1 | 0.879206 | 0.497630 |
| A2 | 0.858696 | 0.422964 |
| A3 | 0.762610 | 0.018323 |

四个 arm 的 realized EV/cycle 均为负：

```text
A0 -1.485507c
A1 -1.319077c
A2 -1.342745c
A3 -1.507351c
```

这只能说明旧代理严重失准且四臂没有 discovery 候选。它不能证明下面的新模型会
盈利，也不能用于挑 KEEP、REPRICE 或 IOC 的参数。

`q_Y × q_N` 的对象本身不对：两腿不是两个独立、同时完成的 Bernoulli 事件。
第一腿成交会改变库存、补齐腿 queue、盘口、flow 和退出成本。下一版必须先建模
“谁先成交”，再直接建模“已知第一腿和当时状态后，补齐腿会不会成交”。

## 2. 数据边界与禁止打开规则

### 2.1 唯一 discovery 数据

本实验所有拟合、交叉拟合、校准、特征缩放和 action-set discovery 只能读取完整
UTC 日：

```text
2026-07-20
2026-07-21
2026-07-22
```

它们全部标记为 `DISCOVERY_ONLY`，不产生确认性 OOS 结论。

### 2.2 硬禁用

以下日期对本实验的模型拟合、阈值选择、动作选择、校准和验证全部禁用：

```text
2026-07-23
2026-07-26
```

loader 必须在读取 parquet/CSV/NDJSON 内容前检查路径日期；命中禁用日立即
fail closed。不得先读取后“从 dataframe 里过滤”。除 07-20..22 外的旧日期也
不得进入本实验 TRAIN。

ROUND3 已生成的汇总数字只允许作为本节的设计动机，不得转换成训练行。

### 2.3 唯一 FINAL

完成以下全部 seal 后：

1. discovery source manifest；
2. table schema；
3. feature schema、缩放参数和 hazard 参数；
4. exact replay 代码与测试；
5. entry action set；
6. KEEP/REPRICE/IOC action set；
7. fee、queue、latency、risk 配置；

FINAL 从 seal 时间之后的**第一个全新完整 UTC 日** `00:00:00Z` 开始。若 seal
发生在某日 00:00Z 之后，不能补用该日剩余小时。

裁决只能发生在完整 UTC 日结束后。最低门为：

```text
完整 forward 覆盖 >= 24h
resolved/admitted terminal episodes >= 300
互不重叠的 unique 15-minute markets >= 30
```

若首个完整日不够，只能继续收集后续完整 UTC 日，并在某个完整日结束点裁决。
运行中改代码、模型、动作或数据契约会使窗口作废；重新 seal 后从另一个从未打开
的完整 UTC 日开始。07-23 和 07-26 永久不能充当 FINAL。

## 3. 唯一时钟、因果顺序与数据闸门

### 3.1 时钟

所有可交易判断使用本地 receipt 时钟：

```text
local_recv_ts_us = recv_wall_ns // 1000
```

禁止用 exchange event timestamp、文件排序时间或未来 corrected timestamp
决定当时动作。合并顺序固定为：

```text
(recv_wall_ns,
 recv_mono_ns,
 channel_priority,       # BOOK before TRADE in same envelope
 ws_seq_or_sentinel,
 stable_source_id)
```

### 3.2 L2 合约

- snapshot 完整 re-anchor，记录 `ws_sid/ws_seq`；
- delta 必须有同 sid snapshot 且 seq 严格递增；
- `next_depth = prior_depth + signed_delta`；
- `<0` 使整个 market-day 无效，`=0` 删除价位，绝不 clamp；
- gap、regression、parse error、未知方向、缺失 first-fill 账本任一出现，完整
  market-day 从所有 action 中一起回滚；
- `DATA_INVALID` 只允许出现在 source-day rejection receipt；不得物化进
  `entry_episode`、Stage-1/Stage-2 risk interval、模型输入或经济指标；
- action 间必须使用同一 source rows，不能某个 action 偷用更多数据。

## 4. 状态机与动作语义

### 4.1 Entry 动作

允许的动作类型现在只定义语义：

```text
ENTRY_SKIP
ENTRY_PAIR_POST_ONLY(p_Y, p_N, C=1)
```

`ENTRY_PAIR_POST_ONLY` 必须满足：

1. flat market 才能开始；
2. YES/NO 同一 decision receipt 生成；
3. 两价来自当时 market `price_ranges` 的合法 grid；
4. 两单均严格 post-only；
5. `p_Y + p_N <= locked_pair_cost_ceiling`；
6. 两单 ACK 后才进入 risk set；一单失败则撤另一单并记 `ACK_FAILED`；
7. 同一 market 同时最多一个 active cycle，不生成重叠独立样本。

具体候选价格/offset 仍为 `ACTION_SET_PENDING`。

### 4.2 第一阶段 terminal cause

两单均 ACK 且 active 后，第一个 terminal cause 只能是：

```text
YES_FIRST
NO_FIRST
ADMIN_CENSOR_NO_FIRST_FILL
```

`ACK_FAILED` 发生在进入 risk set 前。任何 `DATA_INVALID` 都使完整
market-day 回滚，不能成为这里的 terminal cause。

YES 和 NO 在同一 causal envelope 内都成交时，仍按第 3 节的固定总顺序确定
first cause；第二腿必须写入 `postfill_zero_time_atom`。该 atom：

- 与 first fill 的 `recv_wall_ns/recv_mono_ns` 和 envelope identity 相同；
- 由 `stable_source_id` 固定先后；
- 没有 elapsed duration，不生成 `[0, epsilon)`；
- 不进入 `postfill_risk_interval`、`mu_C` 或 `mu_X`；
- atom 已补齐的 episode 不再生成 KEEP/REPRICE/IOC 决策。

### 4.3 First-fill 后动作

第一腿 fill receipt 到达时立即物化 `first_fill_state`。允许的 action family：

#### KEEP

保留原先已在场的 complement maker order：

- 价格不变；
- queue age 和已有 priority 不重置；
- 官方 `queue_position_fp` 可用时优先使用；
- 不可用时只用 causal L2/trade 重建的 same-price-ahead；
- KEEP 不是“永远等”，其具体复评时点由后续 action seal 固定。

#### REPRICE

撤原 complement order，再按锁定的新 maker 价格 post-only 重挂：

- cancel ACK/成交竞态完整模拟；
- cancel 生效前的 fill 仍归旧单；
- 新单 queue age 为 0，不能继承旧队龄；
- 新价格必须合法且通过 pair ceiling/risk gate；
- cancel 或 replace 状态未知即 fail closed，不可把订单当作已撤。

#### IOC

先处理 complement maker 的 cancel race，再立即 reduce-only：

- 具体路线必须是 seal 中锁定的
  `BUY_COMPLEMENT` 或 `SELL_FIRST_LEG`；
- exact L2 逐档 walk、精确 taker fee 和实际可成交量；
- 深度不足的剩余库存不得消失，必须继续留在风险账本；
- 不允许用未来 settlement 或后来出现的盘口填补当时缺失深度。

具体 decision grid、REPRICE 价和 IOC limit/slippage 均待 clean post-fill
discovery 后单独 seal。

## 5. 第一阶段：谁先成交的 competing risks

给定入场因果状态 `X_0` 和 entry 动作 `a`，定义 cause-specific hazards：

```text
lambda_Y(t | X_0(t), a)
lambda_N(t | X_0(t), a)
```

共同 event-free survival：

```text
S_0(t | a) =
  exp{- integral_0^t [lambda_Y(u|a) + lambda_N(u|a)] du}
```

first-fill cumulative incidence：

```text
F_Y(h | a) = integral_0^h S_0(u|a) lambda_Y(u|a) du
F_N(h | a) = integral_0^h S_0(u|a) lambda_N(u|a) du
```

必须满足：

```text
F_Y(h) + F_N(h) + S_0(h) = 1
```

禁止重新引入 `q_Y*q_N`，也禁止把两个独立二分类概率直接相加。

## 6. 第二阶段：直接估计条件补齐

若侧 `s` 先成交，令 `Z_s(0)` 为 first-fill receipt 当时可见状态。对 post-fill
动作 `b in {KEEP, REPRICE}`，定义：

```text
mu_C(v | first=s, Z_s(v), b)  # complement maker fill hazard
mu_X(v | first=s, Z_s(v), b)  # inventory exit / hard boundary cause
```

共同 survival：

```text
S_1(v | s,z,b) =
  exp{- integral_0^v [mu_C(r)+mu_X(r)] dr}
```

本实验的核心概率对象是：

```text
Q_C(h | first=s, state=z, action=b)
  = integral_0^h S_1(v|s,z,b) mu_C(v|s,z,b) dv
```

它直接回答：

> 已知哪一腿刚成交、补齐单当时的真实 queue/flow/价格状态以及采取的动作，
> complement 在 horizon 前成交的概率是多少？

连续 Stage-2 hazard 明确条件化在 **没有** same-envelope atom：

```text
pi_0(z) = P(same-envelope complement atom | first fill state=z)

Q_C,total(h | z,b)
  = pi_0(z) + [1-pi_0(z)] Q_C,continuous(h | z,b, no atom)
```

`pi_0` 单独用 atom/非-atom first-fill 母集估计和校准。atom positive row
只能来自 `postfill_zero_time_atom`；negative denominator 来自
`first_fill_state LEFT JOIN postfill_zero_time_atom`。禁止把 atom row 送入
piecewise-exponential continuous hazard。

IOC 是立即 terminal 动作，直接用 exact execution PnL，不给它伪造 fill hazard。

若 complement 成交后的净收益为 `G_s(v,b)`，inventory exit 的净值为
`X_s(v,b)`，则：

```text
V_s(h | z,b) =
    integral_0^h S_1(v) mu_C(v) G_s(v,b) dv
  + integral_0^h S_1(v) mu_X(v) X_s(v,b) dv
  + S_1(h) X_s(h,b)
```

主动 REPRICE、IOC 或 hard boundary 是 policy/event outcome，不得伪装成无信息
right censor。

## 7. Entry 整轮 EV

给定 entry 动作 `a` 与 sealed post-fill policy `pi`：

```text
EV_cycle(a, pi | X_0) =
  sum_{s in {Y,N}} integral_0^H
    S_0(u|a) lambda_s(u|a)
    E[V_s(Z_s(u), pi) | first=s, T_first=u] du
```

`H` 内没有 first fill 的 cycle PnL 为 0，但它占用了报价资本和时间，必须进入
capital-time 分母。所有费用逐 fill 用 Decimal centicent 规则计算。

## 8. 两个并列主经济指标

### 8.1 EV/cycle

对所有 admitted entry cycles，包括 no-fill：

```text
EV_cycle_hat = sum_i net_pnl_i_usd / N_admitted_cycles
```

不得只在有 first fill 或已完成 pair 的条件下计算。

### 8.2 EV/capital-time

沿用策略风险账本定义 episode 的实时锁定资本：

```text
K_i(t) =
  sum(resting_order_price × remaining_qty)
  + unpaired_inventory_cost_basis
  + pending_new_reservation
```

YES/NO 配对并被 exchange netting 后，episode capital 归零；未成交撤单 ACK 后释放。

```text
capital_dollar_seconds_i = integral K_i(t) dt

EV_capital_time_hat =
  sum_i net_pnl_i_usd / sum_i capital_dollar_seconds_i

reported_cents_per_locked_dollar_hour =
  100 × 3600 × EV_capital_time_hat
```

`EV/cycle` 和 `EV/capital-time` 是并列主指标，必须同时报告。高周转不能把负
EV/cycle 包装成候选。还需报告 `cycles/hour`、总 capital-dollar-seconds 和
峰值 episode capital。

## 9. 因果特征白名单

所有 time-varying 特征取 interval start 时的最后完整 receipt；必须记录
`feature_asof_wall_ns <= decision_wall_ns`。

### 9.1 Entry/first-cause

按目标侧 side-oriented：

1. `same_price_ahead_fp`；
2. `better_depth_barrier_fp`；
3. `queue_position_fp`（真实订单有官方值时）；
4. `flow_1s_fp, flow_5s_fp, flow_10s_fp, flow_60s_fp`；
5. `flow_10s/flow_60s` acceleration；
6. top touch imbalance；
7. candidate 相对 touch 的合法 tick offset；
8. spread 与 1s/10s causal mid move；
9. `TTE`；
10. order age；
11. candidate pair cost 与成功净 gain；
12. first-side indicator 只在第二阶段使用。

### 9.2 First-fill/conditional completion

除补齐侧上列状态，再允许：

1. first side、first price、first quantity；
2. first-fill elapsed time；
3. complement 原价、order age、官方/reconstructed queue position；
4. first-fill 前后 causal mid move；
5. 当前完整 L2 的 `X_buy_complement`、`X_sell_first`；
6. pair locked gain；
7. 距 market close/settlement hard boundary 的时间；
8. 当前 action kind、REPRICE offset 或 IOC route；
9. cancel pending/ACK 状态。

### 9.3 禁止特征

- interval end 后的任何 quote/trade/depth；
- realized wait、未来 queue clear、最终 pair/exit 标签；
- settlement outcome；
- 全 episode max/min/均值；
- 07-23、07-26 或未来 forward 日的任何统计量；
- 用 forbidden 日期拟合的 fair value、embedding、scaler 或 calibration；
- exchange timestamp 推导的、当时本地尚未收到的信息。

未知、缺失或超 TRAIN 支持域的关键特征只能 `SKIP/IOC_FAIL_CLOSED`，不得填未来值。

## 10. 模型和估计约束

第一版只允许可审计的小样本模型：

```text
cause-specific piecewise exponential hazard
log lambda_k(t) = alpha_{k,time_bin} + beta_k' x(t)
```

固定 time bins：

```text
entry:    [0,1), [1,2), [2,5), [5,15), [15,30),
          [30,60), [60,120), [120,300] seconds
postfill: [0,.25), [.25,.5), [.5,1), [1,2), [2,5),
          [5,10), [10,30), [30,60] seconds
```

- YES/NO 使用 side-oriented pooled coefficients，只允许 side intercept；
- ridge 候选只允许 `{0.1, 1, 10}`；
- 选择只用 07-20/21/22 leave-one-date-out right-censored likelihood；
- 连续特征只用 discovery median/IQR 缩放并裁剪至 discovery 1%/99%；
- 概率 calibration 只允许在 cross-fitted discovery predictions 上做
  intercept+slope；
- Aalen–Johansen/Kaplan–Meier 作为无参数 sanity check；
- 不允许神经网络、树模型 sweep 或看完 07-23/26 后添加交互。

必须分别报告：

1. Stage-1 `YES_FIRST` 与 `NO_FIRST` 各自的 cause-specific RCLL，及两者
   合计；只报 pooled total 不合格；
2. Stage-1 CIF calibration/Brier；
3. Stage-2 `Q_C` 在 0.5/1/2/5/10/30/60s 的 IPCW Brier 和 calibration；
4. 按 first side、TTE、queue 三分位的 calibration；
5. pooled-null 与 ETA mechanical baseline。pooled-null 必须是真正不含连续
   特征的 time-bin/side baseline；ETA 必须显式声明 queue-ahead、clip、
   consumption-rate 单位和正负号契约，未锁定这些语义时只能
   `ETA_BASELINE_NOT_IDENTIFIED`，不得悄悄猜符号。

若参数模型与 Aalen–Johansen 方向冲突，状态为 `MODEL_MISSPECIFIED`，不能写
action seal。方向门至少比较最终锁定 horizon 的 `YES vs NO` first-cause
排序，以及每个可比较 post-fill action/first-side cell 的 complement-incidence
排序；差异落在预注册 tolerance 内只能记 `NO_DIRECTION_DECISION`，不能强判一致。

## 11. 可执行表契约

SQL DDL 位于：

```text
tmp/crypto_mm_canary_20260726/round4/
  round4_two_stage_tables.sql
```

核心表：

| 表 | 一行代表什么 |
| --- | --- |
| `source_day_gate` | 一个 UTC source day 的读取许可和完整性 |
| `entry_episode` | flat 状态下一个 entry action 的完整 episode |
| `entry_risk_interval` | Stage-1 counting-process interval |
| `first_fill_state` | first fill receipt 时的因果状态 |
| `postfill_zero_time_atom` | 同一 receipt envelope 内 elapsed=0 的 complement fill |
| `postfill_decision` | first-fill 后一次可执行决策状态 |
| `postfill_action` | 该状态下一个 KEEP/REPRICE/IOC 动作 |
| `postfill_risk_interval` | Stage-2 conditional completion interval |
| `postfill_action_outcome` | 一个 post-fill action 的 terminal 结果 |
| `cycle_policy_outcome` | entry + sealed policy 的整轮经济结果 |

硬检查：

1. 每个 Stage-1 interval 最多一个 `YES_FIRST/NO_FIRST`，且
   `data_invalid=0`；
2. 每个 Stage-2 interval 最多一个 `COMPLEMENT/INVENTORY_EXIT`，且
   `data_invalid=0`；
3. `feature_asof_wall_ns <= decision_wall_ns <= interval_stop_wall_ns`；
4. fill/exit/censor 三者互斥；
5. `p_Y+p_N`、quantity、fee、PnL 和 capital-time 可逐行重算；
6. 每个 first fill 必须且只能连接一个 first-fill state；
7. 同一 action-set 比较使用相同 episode IDs；
8. discovery 行日期只能是 07-20/21/22；
9. 07-23、07-26 行数必须为 0；
10. 数据无效不能作为普通负样本，整 market-day 回滚。
11. zero-time atom 与 continuous Stage-2 risk-set episode 互斥；
12. `cycle_policy_outcome` 的五列主键唯一、`reconciliation_ok=true`，且
    `NO_FIRST_FILL/NO_ENTRY` 的 PnL 和 fill fees 均为零。

## 12. Action-set seal 协议

clean post-fill discovery 完成后，只允许生成一次
`round4_action_set_seal.json`，至少包含：

```text
entry action IDs and exact legal price rules
post-fill decision times
KEEP horizons
REPRICE prices/ticks and cancel rules
IOC routes, limits and residual-inventory rule
model/scaler/calibrator hashes
table/source/code hashes
fee schedule identity
queue and latency semantics
risk/capital ledger version
```

seal 前报告只能写：

```text
candidate_status = ACTION_SET_PENDING
deployable = false
```

seal 后不得再用 07-20..22 改动作，不得打开 07-23/26“确认一下”。任何变化生成新
版本，并等待另一个全新完整 UTC 日。

## 13. Forward 裁决

forward report 必须同时满足：

1. source-day gate 全绿；
2. 所有 admitted cycle 有完整 terminal ledger；
3. `EV/cycle` market-cluster bootstrap 95% CI 下界 `>0`；
4. `EV/capital-time` 同样按 market cluster 重采样，95% CI 下界 `>0`；
5. 相对 sealed control 的 paired PnL/cycle CI 下界 `>=0`；
6. 两种 first side 的 realized conditional value 点估计均 `>=0`；
7. Stage-1 CIF 与 Stage-2 `Q_C` 未越过 seal 中的 calibration guard；
8. `unknown/carried/unreconciled = 0`；
9. 完整 UTC 日和样本门同时满足。

任一失败为 `NO_CANDIDATE`；数据/样本不足为 `NO_DECISION`。同一 forward 窗口
不得调参重跑。

## 14. 最小实现顺序

1. 执行 SQL DDL，建立空表并实现 forbidden-date pre-open gate。
2. 用合成事件写时钟、snapshot/delta、strict trade-through、cancel-race 测试。
3. 只从 07-20..22 物化 non-overlapping `entry_episode` 和 Stage-1 intervals。
4. 物化每个 first fill 的 causal state；同 envelope 第二腿先写独立
   `postfill_zero_time_atom`，atom episode 到此终止；完成守恒和 no-lookahead。
5. 只对明确的 non-atom first fill 为 KEEP/REPRICE/IOC action family 生成
   paired post-fill action rows 和
   Stage-2 intervals；此时只做 discovery，不排名候选。
6. 拟合并 cross-fit Stage-1 competing risks 与 Stage-2 conditional hazards；
   输出 calibration、Aalen–Johansen sanity 和 source/hash receipt。
7. clean post-fill 结果通过后，才冻结少量具体 entry/post-fill actions，生成
   `ACTION_SET_SEAL`。
8. 封印 scorer、replay、feature schema、action set 和风险账本。
9. 从 seal 后第一个新完整 UTC 日启动 forward shadow；只在完整日结束点裁决。

本轮的下一步不是“马上挑一个新价格上线”，而是先让数据表能无歧义地回答：

```text
P(complement | first fill, causal state, chosen action)
```

然后再判断是否存在同时改善 `EV/cycle` 和 `EV/capital-time` 的可封印动作。
