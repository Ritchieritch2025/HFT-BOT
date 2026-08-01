# KXBTC15M 动态配对 EV 与价格分配预注册

日期：2026-07-26  
状态：**预注册；只允许离线回放和影子验证；不授权实盘**  
目标：替换“固定两边贴 touch + 固定 depth/TTL”的配对策略。每轮在合法
maker 价格中同时决定 **报不报价** 和 **两边各报多少钱**，用整轮而非单腿
EV 判断；一腿成交后，再按条件补齐概率与可执行退出损失动态决定继续等还是退出。

## 0. 数据封印与本报告看过什么

- 拟合区：2026-07-12..19，只能用 KXBTC15M FIT/TRAIN。
- 2026-07-20..23 **全部已经消耗**，只能称 DISCOVERY。07-23 的结果文件已由
  项目主线打开并用于形成当前 Z3/动态 EV 假设，因此它绝不是 untouched holdout，
  也不能再称 FINAL_VALIDATE。
- 真正的 FINAL 必须是本预注册、模型和执行代码全部 seal 之后才产生的
  **forward holdout**。窗口从 seal 之后的第一个完整 UTC 日 `00:00:00Z`
  开始，连续收集至少 24h，并继续到 admitted cycles `>=30` 且不重叠的
  market clusters `>=10`；结束点取这两个条件中较晚者。
- forward 窗口内不得调参、换 arm、改特征或复用同一 cycle。发生数据缺口、
  代码/模型变化，或在裁决时仍不够上述样本门，结论只能是 `NO_DECISION`。

本报告只读了现有回放器 `grid_replay_v2.py`、本地影子日志和已有策略规格。
影子日志可见 pair completion 等待时间明显不恒定，且出现过超时退出意图；但该批
日志跨越多次重启和已知旧记账缺陷，所以只用来提出假设，不进入拟合或成绩。
项目主线已读取 `pair_fullzone_clip1_discovery_val23.json`；本轮文档修订没有
重新读取该文件，但按已知使用事实把 07-23 明确归入 DISCOVERY。

## 1. 经济对象：赚的是整轮，不是“第二腿看起来能补”

以下价格均为每张美元价格，首版固定 `C = 1` 张。动作
`a = (p_Y, p_N)` 是同时挂出的 YES、NO maker 买价。

### 1.1 两腿都以 maker 成交

同一二元市场的一张 YES 和一张 NO 合计终值为 `$1`，因此：

```text
G(a) = C × (1 - p_Y - p_N) - f_M(p_Y,C) - f_M(p_N,C)
```

`p_Y + p_N = 0.99` 且 maker fee 为零时，`G = $0.01/张`。这只是成功轮的
收益，不是整轮期望收益。

Kalshi 2026-07-07 费表的通用公式为：

```text
raw taker fee = M × 0.07   × C × P × (1-P)
raw maker fee = M × 0.0175 × C × P × (1-P)
```

maker multiplier 默认 0；该费表的非标准列表中没有 KXBTC15M。实现仍必须在每次
启动读取 series fee changes，未知或变化即 fail closed，不能把“当前为零”写死。
费用还必须按官方规则，使 `positionCost + fee` 向上对齐至 `$0.0001`
（centicent），不能只对裸公式四舍五入。

### 1.2 概率几何使用 log-odds，现金经济仍使用价格

2026-07-26、任何 Stage-2 拟合开始前，预注册以下特征变换。对
`0 < p < 1`：

```text
logit(p) = log[p / (1-p)]
sigmoid(z) = 1 / (1 + exp(-z))
```

YES 盘口由可执行互补报价构造：

```text
p_bid,Y = best_yes_bid
p_ask,Y = 1 - best_no_bid

mid_logodds =
    [logit(p_bid,Y) + logit(p_ask,Y)] / 2

spread_logodds =
    logit(p_ask,Y) - logit(p_bid,Y)
```

方向统一到目标侧后：

```text
mid_logodds,NO = -mid_logodds,YES
move_logodds_s(Delta t) =
    mid_logodds,s(t) - mid_logodds,s(t-Delta t)
quote_skew_logodds_s =
    logit(p_quote,s) - logit(p_fair,s)
```

这样 `50c -> 49c` 的 log-odds 变化约为 `-0.040`，而
`2c -> 1c` 约为 `-0.703`；后者的相对概率变化会被模型正确放大。
所有输入价格必须来自当时合法 `price_ranges`，严格位于 `(0,1)`；
不允许为了避免无穷大而事后选择任意 epsilon。

log-odds 只用于成交 hazard、逆向选择、价格漂移和报价 skew 特征。
`G`、`X_sell`、`X_pair`、手续费、逐档 FOK、`p_Y+p_N<=0.99`
和资本占用仍按真实美元价格逐张计算，因为合约现金损益对价格是线性的。
DISCOVERY 的主规格使用 log-odds；原始美分特征只作为预先声明的机械基准，
不能在看到结果后把两者任意拼接。

### 1.3 首腿成交后，立即退出值

若先买到侧 `s`，成交价为 `p_s`，在时间 `v` 有两条完整退出路线：

```text
X_sell,s(v) =
    C × (executable_bid_s(v) - p_s) - exact_taker_fee

X_pair,s(v) =
    C × (1 - p_s - executable_ask_opposite(v)) - exact_taker_fee

X_s(v) = max(X_sell,s(v), X_pair,s(v))
```

两条路线都必须按完整 L2 逐档 walk；深度不足的剩余量在线上按最坏终值保留风险，
回放也用同一保守口径，不能用未来结算结果替线上决策“补答案”。

定义孤腿退出损失 `L_s(v) = -X_s(v)`。它是一个条件分布，不是常数。当前策略
“成功赚 1¢、失败可能亏数倍”的不对称，正是固定 TTL 负期望的来源。

### 1.4 最简 break-even

若成功收益和失败损失可近似为常数 `G > 0, L > 0`，首腿后在期限内补齐概率为
`q`，则：

```text
W = qG - (1-q)L
q* = L / (G+L)
```

例如 `G=1¢, L=8¢` 时，`q* = 88.89%`。只看“多数时候能补上”远远不够。
正式模型不使用这个常数近似做交易，只把逐样本 `q*` 作为解释性诊断。

## 2. 第一阶段：两边谁先成交是 competing risks

一对 maker 单同时在场时，YES-first 和 NO-first 是互斥的第一事件。令
`lambda_Y(u|x,a)`、`lambda_N(u|x,a)` 为给定入场状态 `x` 和价对 `a` 的
cause-specific hazards：

```text
S_0(u|x,a) =
    exp{- integral_0^u [lambda_Y(r|x,a)+lambda_N(r|x,a)] dr}

pi_s(H|x,a) =
    integral_0^H S_0(u|x,a) lambda_s(u|x,a) du
```

`pi_Y`、`pi_N` 才是“哪一腿会先把我们变成有库存”的概率。不能分别拟合两个
独立二分类器后把概率直接相加；两边 queue、flow 和价格状态相关，必须用共同的
event-free survival 重建 cumulative incidence。

`H=60s` 是滚动预测尺度，**不是 60 秒一到就撤单或停止挂单**。若 60 秒没有
首腿，下一次评分以已经存活 60 秒的订单年龄和新盘口为条件继续算；只要当前价对
的 continuation EV 非负，就保留原订单和队龄。

## 3. 第二阶段：首腿后的条件补齐与退出

侧 `s` 先成交后，令 `mu_opposite(v|z_s,a)` 为原先已在场的补齐单在 elapsed
time `v` 的 fill hazard，`nu(v|z_s,a)` 为数据中断、市场暂停、价格失去
post-only 合法性或硬结算边界等外生退出 hazard：

```text
S_s(v|z_s,a) =
    exp{- integral_0^v [mu_opposite(r)+nu(r)] dr}

W_s(h|z_s,a) =
    integral_0^h S_s(v) mu_opposite(v) G(a) dv
  + integral_0^h S_s(v) nu(v) X_s(v) dv
  + S_s(h) X_s(h)
```

无外生退出时：

```text
q_s(h|z_s,a) = 1 - S_s(h)
W_s(h) = q_s(h)G(a) + [1-q_s(h)] E[X_s(h) | no fill by h]
```

首版只比较预先封印的 `{5, 15, 30, 60}` 秒四个 horizon。首腿一到即计算：

```text
h* = argmax_h LCB10[W_s(h)]

若 max_h LCB10[W_s(h)] <= X_s(0)，立即走当前最优完整 taker 路线；
否则保留原补齐 maker 单，到下一秒用新状态重复比较。
```

因此没有“一律等 60/90 秒”的 TTL；等待时间随 queue、flow、价格、退出成本和
剩余 TTE 变化。补齐 maker 价不得突破入场时锁定的 pair-cost ceiling。

## 4. 入场整轮 EV

第一腿在 `u` 时发生后，状态 `Z_s(u)` 包含当时补齐腿 queue、最近 flow、盘口
移动和立即退出价格。整轮条件期望为：

```text
EV(a|x) =
  sum_{s in {Y,N}} integral_0^H
    S_0(u|x,a) lambda_s(u|x,a)
    E[ W_s(Z_s(u),a) | x,a,T_s=u ] du
```

在 `H` 内没有首腿时现金 PnL 为 0。另报两个资金周转量：

```text
expected orphan-seconds_s(h) = integral_0^h S_s(v) dv
expected quote-seconds(H)    = integral_0^H S_0(u) du
```

它们只用于同 EV 价对的周转优先级，不允许把负 EV 用“成交更快”包装成可交易。

## 5. 价格也是动作：五个冻结 arms

### 5.1 合法候选

从每个 market response 的 `price_ranges` 读取当下合法 tick，不能假设永远是
1¢。令两侧当前 maker touch 为 `b_Y,b_N`，每侧候选偏移
`k_s in {-1,0,+1,+2}`：

```text
p_s = b_s + k_s × tick_s(p_s)
```

最多枚举 16 对，随后过滤：

1. 两价均在 market 的合法 price range；
2. 两单均为 post-only，不能与当时 opposite resting liquidity 相交；
3. `p_Y + p_N <= 0.99`；
4. 该价位/特征在 TRAIN 支持域内；
5. 完整最坏风险和 `$10` 绝对预算守门另行通过。

**成交模拟与 ETA 特征必须分开：**

- fresh hypothetical order 的 `queue_ahead` 只等于**同价、比我们先到的显示量**；
- 更优价位累计深度是 `better_depth_barrier`，可作为 hazard/ETA 特征；
- exact replay 仍按 price-time priority、strict trade-through 和 60ms
  cancel race 判成交，不能把 better depth 先放进 queue_ahead、又在
  trade-through 中再扣一次。
- 影子/实盘订单 ACK 后，优先用 Kalshi 官方 `queue_position_fp` 替代同价估计。

### 5.2 冻结 arms

先在 touch 用：

```text
ETA_s = (better_depth_barrier_s + same_price_ahead_s + C)
        / max(executable_flow_60s_s/60, C/300)
```

定义 `slow` 为 ETA 较大侧，`fast` 为另一侧。

| Arm | 价对规则 | 目的 |
|---|---|---|
| P0 TOUCH | `(b_Y,b_N)` | 当前固定 touch 对照 |
| P1 SHIFT1 | slow `+1 tick`，fast `-1 tick` | 把 1 tick 成本预算从快腿挪给慢腿 |
| P2 SHIFT2 | slow `+2 ticks`，fast `-2 ticks` | 检验更强的完成时间平衡 |
| P3 SPEND1 | 有 `<=99¢` 余量时 slow `+1 tick`，fast 不动 | 用未花的 pair budget 买慢腿 hazard |
| P4 EV_ENUM | 枚举全部合法价对，按下式选择 | 主候选；同时决定价格或跳过 |

P1–P3 在规则生成非法价对时该 epoch 记为 `SKIP`，不悄悄回退成另一个 arm。
它们是机制诊断；只有 P4 是晋级候选。

P4 的每个候选都重新计算该价格处的 queue、barrier、executable flow、两阶段
hazards、退出分布和整轮 EV：

```text
admissible(a) iff
    LCB10[EV(a|x)] >= +$0.0025       # +0.25¢ 固定模型/执行缓冲
and LCB10[W_Y(a|first=Y)] >= 0
and LCB10[W_N(a|first=N)] >= 0
and all support / execution / risk gates pass
```

从 admissible 集合选择 `LCB10[EV]` 最大者。若多对与最优相差不超过
`$0.0005`（0.05¢），依次用以下固定 tie-break：

1. 最小化 `|log median(T_Y) - log median(T_N)|`；
2. 最小化 expected orphan-seconds；
3. 选择更低 `p_Y+p_N`；
4. 固定字典序 `(p_Y,p_N)`。

无 admissible 候选就不挂这一个 market/epoch。

这与“简单 admission gate”有本质区别：简单 gate 只能对固定 touch 价回答
yes/no；P4 把 `(p_Y,p_N)` 本身当作动作，把 99¢ 以内的成本预算从快腿重新分配
到慢腿，每个价位重新估计完成概率和失败损失，然后选择价对或跳过。

### 5.3 动态不等于追价

价格分配只在 flat 新 cycle 入场时决定。两单已在场后：

- 当前 bundle continuation EV 非负：保持原价和队龄；
- 数据失真、价格不合法、pair ceiling/budget 失守：双撤；
- 只是出现一个看起来更好的新价对：不追，不为小幅模型波动丢队龄；
- 一腿成交：进入第 3 节的补齐/退出状态，不再开启新入场腿。

这使策略可以跨连续 15 分钟市场 24h 轮转，而不是每 60 秒暂停，也不是每秒改价。

## 6. 小样本模型：piecewise exponential + ridge

不用深网。TRAIN 当前有效 market 数有限，先用可审计的 piecewise-exponential
cause-specific hazard：

```text
lambda_s(u|phi_s) =
    lambda_0,bin(u) × exp(beta' phi_s)

time bins = [0,2), [2,5), [5,15), [15,30), [30,60] seconds
```

YES/NO 通过 side-oriented 特征共享一套系数，只保留一个 side intercept；
不加交互项。ridge `lambda in {0.1,1,10}`，只按 TRAIN leave-one-date-out 的
right-censored log-likelihood 选择一次。所有连续特征用 TRAIN median/IQR
缩放，并裁剪到 TRAIN 1%/99%；超支持域线上 fail closed。

### 6.1 入场/第一腿固定特征

每个候选价、每个目标侧固定使用：

1. `log1p(same_price_ahead/C)`；
2. `log1p(better_depth_barrier/C)`；
3. `log1p(executable_flow_60s/C)`；
4. flow acceleration：
   `log[(flow10/10 + C/300)/(flow60/60 + C/300)]`；
5. side-oriented top-touch imbalance；
6. candidate 相对 touch 的 tick offset；
7. 当前 `spread_logodds`；
8. side-oriented 1s/10s `move_logodds`；
9. `log(TTE/60)`；
10. resting order age（continuation 评分时；新单为 0）。

若使用 fair value，价格偏离只以预注册的 `quote_skew_logodds` 表示；
不在本轮追加神经网络 embedding 或任意事后挑选指标。原始美分 spread/move
保留在经济账本和 raw-cents baseline 中，不与主规格并列堆入同一小样本模型。

### 6.2 首腿后固定特征

补齐 hazard 使用上列补齐侧特征，再加：

- first side；
- first price、pair locked gain `G`；
- first-fill elapsed time；
- 首腿后相对入场的 side-oriented `move_logodds`；
- 当前 `X_sell`、`X_pair` 和二者较大值；
- 距离硬 market boundary 的秒数。

退出 PnL 在 `{0,5,15,30,60}` 秒逐档用 exact L2/fee 生成。条件均值用
Huber ridge；条件分布用 market-cluster bootstrap 的模型+残差联合重采样，
不用正态误差假设。少于 30 个补齐事件的侧/时间桶只用 pooled side-symmetric
模型，不报告单侧“精确概率”。

## 7. 右删失、标签和防止虚假样本

1. 一对 hypothetical maker orders 从同一 flat epoch 同时开始。
2. YES-first、NO-first 是 competing events；估 cause-specific hazard 时可把
   另一 cause 视作该 cause 的终止，但最后必须用共同 `S_0` 重建 CIF。
3. 数据文件结束、连接缺口、外生市场暂停、实验窗口结束而未成交是右删失，
   不是“未成交负样本”。
4. 人为 horizon 到期：对自然 fill-time 模型是行政右删失；对策略经济结果则
   必须把当时 exact exit PnL 记入，不能两边都丢。
5. policy 因可观测状态主动退出不是随机删失。首版用固定 horizon 集合并在 exact
   replay 中完整执行该 policy，避免把有信息的撤单伪装成独立删失。
6. partial fill 按实际数量拆成风险 lot；本轮 clip=1 的报告仍以 contract-weighted
   PnL 和 market-cluster cycle PnL同时给出。
7. 同一 market 内大量重叠 hypothetical epochs 不能被当独立样本。各 arm 使用
   非重叠 cycle clock；跨价 arms 的同一 epoch 保持 paired comparison。

非参数 sanity check 使用按 ETA 三分位分层的 Aalen–Johansen / Kaplan–Meier。
若参数模型方向与非参数曲线冲突，状态为 model misspecification，不能晋级。

## 8. 拟合、校准与 OOS 裁决

### 8.1 TRAIN（07-12..19）

- 按 market 分组、leave-one-date-out 生成完全 OOS 的 TRAIN 预测。
- 主要 survival 分数：right-censored log-likelihood（RCLL）。
- 辅助分数：IPCW Brier at 5/15/30/60s、calibration intercept/slope。
- 概率校准固定用 cross-fitted prediction 上的 Platt intercept+slope；
  不按图形临时换 isotonic。
- 不确定度按 market cluster 重采样 10,000 次；不按 fill 行重采样。
- null 基准：无特征 pooled survival；机械基准：当前 60s-flow ETA 排序。

### 8.2 DISCOVERY（07-20..23，全部已消耗）

退出 arm 使用此前已冻结的 DISCOVERY 选择结果，P0–P4 共用，禁止同时重新调退出。
P4 是唯一主候选；P0 是固定 touch control，P1–P3 只解释价格分配机制。
07-23 的 Z3 结果已经参与形成当前假设，故本节所有数字只能用于模型诊断、arm
选择和 GO/NO-GO 到 forward shadow，不能提供确认性 OOS 证据。

P4 才能进入 sealed forward holdout 的必要条件：

1. admitted cycles `>=30` 且 market clusters `>=10`；
2. exact net PnL/cycle 的 market-cluster bootstrap 95% CI 下界 `>0`；
3. 相对 P0，paired market-cluster PnL 差的 95% CI 下界 `>=0`；
4. OOS RCLL 优于 pooled null，30s 和 60s IPCW Brier 均不劣于 ETA baseline；
5. 无漏记/方向错误/不完整退出，所有 cycle 守恒；
6. 两种 first-side 条件的实现 PnL 点估计均非负。

任一项失败即 `NO_CANDIDATE`。不准把 P1–P3 中偶然最好的一项递补成主候选。

### 8.3 FINAL：seal 后的 forward holdout

开始收集前封印：

- source SHA256；
- TRAIN input manifest SHA256；
- feature schema；
- hazard、calibration、loss-model 参数；
- fee schedule/effective timestamp；
- P4 candidate/tie-break；
- exit arm；
- replay latency 和 queue semantics。

seal receipt 必须记录 UTC 时间。FINAL 起点固定为该时间之后第一个完整 UTC 日的
`00:00:00Z`；例如 2026-07-26 14:00Z seal，则起点为 2026-07-27 00:00Z。
从该起点连续收集，裁决点为以下二者较晚者：

1. 已覆盖完整 24h；
2. 已有 `>=30` admitted cycles 和 `>=10` 个不重叠 market clusters。

同一 market/cycle 只允许进入一个 episode；前后窗口不得重叠。FINAL 同时要求：

1. 样本门满足；发生运行中断、数据缺口或无法达到样本门时为 `NO_DECISION`；
2. net PnL/cycle 的 market-cluster bootstrap 95% CI 下界 `>0`；
3. paired-vs-P0 差的 95% CI 下界 `>=0`；
4. 所有孤腿有完整退出，`carried/unknown = 0`；
5. observed completion、退出损失和模型校准未越过 TRAIN 预注册的 95%
   prediction bands。

失败后不得在同一 forward 窗口调参重跑。下一版必须另写假设、重新 seal，并从
新的首个完整 UTC 日开始收集互不重叠的新 holdout。

## 9. 最小实现顺序

1. 新建独立 TRAIN feature/replay 模块；不改 live `mm_engine.py`。
2. 先写 queue tests：
   same-price-ahead、better-depth barrier 分离、strict trade-through、
   fill-during-cancel、post-only 过滤、不同 price range tick。
3. 物化非重叠 candidate episodes 和两阶段 labels；每行带
   `market_ticker, decision_ts, arm, price_pair, source_row_ids`。
4. 拟合 cause-specific hazards、loss model、Platt calibrator；输出 TRAIN
   cross-fit receipt 和 Aalen–Johansen sanity plots。
5. 用纯函数计算 `EV/LCB/W_Y/W_N`，跑 P0–P4 TRAIN 回放并封印模型。
6. 07-20..23 只能作为已消耗 DISCOVERY 解释，不得再充当任何验证。
7. DISCOVERY GO 后 seal 模型、代码和配置；从 seal 后首个完整 UTC 日开始
   forward shadow FINAL，连续至少 24h 且达到 30 cycles/10 markets。
8. FINAL 通过后，才允许把该 frozen scorer 作为实盘候选；forward 期间持续记录
   官方 queue position、订单年龄、两阶段校准和 exact cycle PnL。
9. 即使策略验证通过，实盘还必须独立通过绝对 `$10` durable budget guard、
   对账和单写者锁；本预注册不替代风控开闸。

## 10. 可直接复用的现有字段

现有 `QUOTE_EVAL` 已有：

`tte, spread_c, y/n_touch_ct, touch_imbalance, pair_quote_sum_c,
y/n_flow_10s, y/n_flow_30s, y/n_flow_60s, y/n_queue_ahead,
y/n_clear_eta_s, bid/no_order_age_s`。

回放 L2/trade 数据已能派生：

`same_price_ahead, better_depth_barrier, candidate-price executable flow,
10s mid move, exact walk exit, strict trade-through fill`。

还需新增但不需要新数据源：

`candidate tick offset, first-event cause, right-censor reason,
counterpart state at first fill, exit-route PnL at 0/5/15/30/60s,
market-cluster episode id`。

## 11. 一手来源

- Huang, Lehalle & Rosenbaum，
  [Queue-Reactive Model](https://arxiv.org/abs/1312.0563)：订单流强度依赖当前
  queue state；简单 Poisson 会高估 execution probability。
- Arroyo, Cartea, Moreno-Pino & Zohren，
  [Deep Attentive Survival Analysis in LOBs](https://arxiv.org/abs/2306.05479)：
  time-to-fill、右删失 likelihood、survival/hazard 与 proper scoring rules。
  本方案采用其统计对象，不采用其深网。
- Lokin & Yu，
  [State-Dependent Fill Probabilities](https://arxiv.org/abs/2403.02572)：
  各价位 fill probability 必须随 queue/order-flow state 变化。
- Madrigal-Cianci, Monsalve Maya & Breakey，
  [Prediction Markets as Bayesian Inverse Problems](https://arxiv.org/abs/2601.18815)：
  对 prediction-market price/volume history 直接在 log-odds 空间建模；
  本方案只采用该概率坐标和增量，不采用其 latent-type 模型。
- Aalen & Johansen，
  [Non-homogeneous Markov chains with censored observations](https://www.math.ku.dk/bibliotek/arkivet/preprints-fra-ims/1977/preprint_1977_-_no_6_aalen__odd__johansen__s_ren_-_an_empirical_transition_matrix_for_non-homogeneous---.pdf)：
  competing-risk transition probability的非参数 sanity check。
- Avellaneda & Stoikov，
  [High-frequency trading in a limit order book](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)：
  `r=s-q gamma sigma^2(T-t)` 展示库存风险应进入报价；到单强度必须与报价距离共同
  决定，而不是只追最大名义点差。
- Kalshi，
  [官方 queue position](https://docs.kalshi.com/api-reference/orders/get-order-queue-position)：
  price-time priority 与 preceding shares；
  [fixed-point/price ranges](https://docs.kalshi.com/getting_started/fixed_point_migration)：
  每个 market 的合法 tick 由 `price_ranges` 决定；
  [V2 post-only orders](https://docs.kalshi.com/api-reference/orders/create-order-v2)；
  [2026-07-07 fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)；
  [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)；
  [YES/NO settlement and netting](https://docs.kalshi.com/getting_started/market_settlement)；
  [canonical order direction](https://docs.kalshi.com/getting_started/order_direction)。

## 12. 2026-07-26 风险修订：配对不是强制凑合约

在任何一腿成交后，反腿只作为条件性风险降低动作，不得因为
`p_Y+p_N<=99¢` 就无条件继续占用资金。新增冻结门槛：

1. `pair_sum <= PAIR_LOCK_C - PAIR_MIN_EDGE_C`，默认仍保留 0.5¢ 净边际；
2. 配对后的总锁资不得超过 `min(MAX_OPEN_COST, PAIR_MAX_LOCKED_COST)`，默认
   `PAIR_MAX_LOCKED_COST = 25% * MAX_OPEN_COST`；
3. 如果门槛不通过，反腿不转成普通新仓位，孤腿只能进入受限的退出/超时清仓路径；
4. 替换已有反腿报价时，只计算替换后的增量锁资，不能把旧挂单和新挂单重复计入。

该修订把 pair lock 定义为“有正边际且资金可承受的条件性转换”，不是库存无限扩张器。
