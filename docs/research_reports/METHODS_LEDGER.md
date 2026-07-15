# AUTORESEARCH METHODS LEDGER

> 版本化方法账。新增方法必须先登记、后出结果；修改保留日期与原因，不删除旧做法。
>
> 当前任务：`SPORTS-AUTORESEARCH-01` · `EXPLORATORY_ONLY` · `SEALED_DEGRADED_EVIDENCE` · `NOT A LIVE-TRADING AUTHORIZATION`

## M-BASIC-01 — 描述统计、ECDF 与标准区组自助法种子

1. **白话问题。** 先把消息按完整比赛/root event 汇总，再看样本数、分布、中位数和尾部，防止把同一场比赛的数百万条更新误当作数百万次独立实验。
2. **定义与参考。** 每表至少报告 `n_rows/n_markets/n_games/n_days/p50/p99/max`；连续量配直方图或 ECDF。自助抽样单位为完整 UTC 日区组，区组内保留全部 root events。参考 Efron & Tibshirani (1993), *An Introduction to the Bootstrap*。
3. **假设。** 日期区组近似代表未来日期；只有一至两日时区间退化或极不稳定，不能用于 promotion/verdict。
4. **用途。** 全部 atlas 与 10 张 Cycle-1 假设卡的基础描述；推断部分只在结果工件明确标 `EXECUTED` 时成立。
5. **实现与测试。** `sandbox/research/deep_autoresearch/stats.py`、`core_hypothesis_tests.py:event_inference`；`test_stats_features.py` 与 core fixture tests。
6. **读法。** 若每场均值 0.3¢、区间 `[-0.4,1.0]¢`，不能称为正收益；本任务只有一个 evaluation day 时，bootstrap 只是执行回执，不是置信区间。
7. **失败/负控。** 行级伪重复会虚假缩窄区间；防护是强制同表显示 `n_games`、delete-best-root/day，并将同场所有市场聚为一个 root。

## M-ASOF-01 — 严格因果 as-of 连接

1. **白话问题。** 信号只能看到当时已经收到的盘口，未来更新不能倒灌。
2. **定义与参考。** 决策前状态取同市场最大 `book_ts < decision_ts`；结果状态取最大 `book_ts <= target_ts`，同时报告 book age，且窗口不得越过覆盖边界或 capture gap。参照时间序列 point-in-time join 的标准无前视实现。
3. **假设。** 时钟含义一致、TL1 本地接收钟有效；混钟、同 timestamp tie 未固定或 future row 会使结果作废。
4. **用途。** markout、大流量、赛前 TTS、RFQ→CLOB、H-FOLLOW 与 depletion/refill。
5. **实现与测试。** `features.py`、`run_cycle1.py` 及各 stage；每阶段含 look-ahead sentinel。
6. **读法。** trade 在 `t` 到达，只能使用 `t` 前的 book；`t+1s` 的 book 只能是结果，不能参与 `t` 的 eligibility。
7. **失败/负控。** 时间反向和同钟未来行是主要风险；防护为 future-shift signal、`book_ts>=decision_ts`/`outcome_ts>target_ts` 零行断言。

## M-BOOT-01 — root-event/calendar-day block bootstrap

1. **白话问题。** 重抽“完整的一天”，一天里的比赛一起保留，以估计每场比赛平均效应的不确定性。
2. **定义与参考。** `mu=mean_e(effect_e)`；固定 seed 至少 1,000 次抽取 day blocks，取 2.5%/97.5% 分位。Efron & Tibshirani (1993)。
3. **假设。** 日区组可交换且天数足够；单日时每次抽样完全相同，区间退化。
4. **用途。** C1-LARGE-FLOW、C1-PREMATCH-TTS、H-FOLLOW、depletion/refill 的诊断性 event effect。
5. **实现与测试。** `stats.py:block_bootstrap_mean` 与 stage wrapper；seed `20260715`；确定性测试覆盖。
6. **读法。** lower bound ≤0 不能排除无效；`n_days=1, degenerate=true` 时任何窄区间都不得解释为高精度。
7. **失败/负控。** 按消息抽样会伪造精度；与 exact day enumeration、delete-best-day/root 对照。

## M-BH-01 — Benjamini–Hochberg FDR

1. **白话问题。** 同时试很多 horizon 会偶然出现小 p 值，BH 控制同一家族的预期错误发现比例。
2. **定义与参考。** 排序 `p_(i)`，`q_(i)=min_{j>=i}(m/j)p_(j)` 并单调化；探索阈值 `q<=0.10`。Benjamini & Hochberg (1995), JRSS-B 57(1):289–300。
3. **假设。** 独立或正依赖近似；高度相关时必须同时报告原始 trial 数与效应分布。
4. **用途。** 仅在未来有足够独立日、能够产生有效 p 值的固定 horizon 家族；当前单 evaluation day 明确 `NOT_ESTIMABLE`。
5. **实现与测试。** `stats.py:benjamini_hochberg`、`test_stats_features.py`。
6. **读法。** raw `p=.01` 但 `q=.18` 表示多重校正后不存活；本轮不会因为缺 p 值而挑最佳 horizon。
7. **失败/负控。** 漏报失败 trial 会低估 m；防护为 10 卡预注册与 append-only `TRIAL_REGISTRY.jsonl` 对账。

## M-MATCH-01 — 分层、严格向前的匹配事件研究

1. **白话问题。** 把信号发生时段与市场、价格、点差、活跃度相近的普通时段比较，减少“本来就更热”的混淆。
2. **定义与参考。** 在冻结 strata 内 deterministic exact/nearest-prior matching；控制结果必须在 treatment 前已完整发生；报告未匹配率、balance、root-event effect。Rosenbaum & Rubin (1983), *Biometrika* 70(1):41–55。
3. **假设。** 只能控制观察到的混淆；positivity 或 balance 失败则 DATA_STARVED/COLLECT_MORE。
4. **用途。** C1-LARGE-FLOW 的 same-market prior control；RFQ→CLOB 的 prior pseudo-event；L2 两卡的同状态 control。
5. **实现与测试。** `core_hypothesis_tests.py:build_large_flow`、`rfq_full_stage.py:build_clob_context`、`l2_hypothesis_stage.py` 及对应 fixture tests。
6. **读法。** treatment 与 control 的 prior activity 仍差两个标准差时，post effect 不能归因于信号；未匹配 treatment 不可静默丢掉。
7. **失败/负控。** 未观察比赛状态仍会混淆；防护为 future signal、unrelated root、label shuffle 和明确 balance/unmatched 表。

## M-KM-01 — 右删失 Kaplan–Meier 生存曲线

1. **白话问题。** 扫描结束或断流前未看到 RFQ 删除，只能说它至少活到该时刻，不能硬算成完整寿命。
2. **定义与参考。** `S(t)=prod_{t_i<=t}(1-d_i/n_i)`；首个 loss/close/error/epoch 边界截断风险集，跨界 delete 不倒填。Kaplan & Meier (1958), JASA 53(282):457–481。
3. **假设。** 在已观察分层内删失近似独立；边界漏记、ID 错连或时钟回退会破坏曲线。
4. **用途。** C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01 的 RFQ 生命周期，以及 C1-DEPLETION-REFILL-01 的 refill-time 描述；不推断成交、接受、own-order fill 或 PnL。
5. **实现与测试。** `rfq_full_stage.py:kaplan_meier_from_counts`、`l2_hypothesis_stage.py:generate_charts`；grouped-censor、observation-boundary、snapshot/reset/right-censor fixture tests。
6. **读法。** Cycle-0 触发样本未校正寿命 p50=`13.34562717s`；若边界校正 `S(10s)=0.6`，含义是估计 60% 至少存活 10 秒，不是 60% 会成交。
7. **失败/负控。** scan end 当删除会向下偏；跨断流 delete 倒填会伪造完整。负控为 boundary shift、future-delete sentinel 和跨界 delete 隔离计数。

## M-L2-REPLAY-01 — snapshot-aware L2 确定性回放

1. **白话问题。** 先用完整 snapshot 建盘口，再按接收顺序应用 delta；没有锚点或遇到重置时绝不能猜深度，snapshot 本身也不能冒充 refill。
2. **定义与参考。** 每 `(date,market,side,price)` 状态由最近 snapshot 重建，之后应用有界 delta；pre-snapshot delta 排除。流完整性只认封存的 per-sid `quality/l2_gaps.json`，每市场 `ws_seq` 跳号不作丢包推断。方法结构对应事件驱动 limit-order-book reconstruction 的状态机做法。
3. **假设。** snapshot 与 delta schema 有效、接收钟确定、封存 receipt 覆盖完整 sid stream；任一失败都隔离相应 release/day。
4. **用途。** C1-HFOLLOW-RETREAT-01 与 C1-DEPLETION-REFILL-01。
5. **实现与测试。** `l2_hypothesis_stage.py` 的 replay/FSM；synthetic snapshot/delta、reset sentinel 与 sealed-receipt fixture tests。
6. **读法。** depletion 后 100ms 内恢复到 prior depth 的冻结比例才算 rapid refill；若中途出现 snapshot，该 episode 右删失而不是成功 refill。
7. **失败/负控。** 把 snapshot 当新增量、按市场 ws_seq 误判缺口、负深度或前视 refill 都会造假；防护为 reset sentinel、future-refill、unrelated-root/label shuffle 与非负深度断言。

## Revision trail

- 2026-07-15：在 `SPORTS-AUTORESEARCH-01` 首个深度结果前建立 M-BASIC/ASOF/BOOT/BH/MATCH/KM/L2-REPLAY 条目。实际结果的真实表/图读法将在同一 run 的 `METHODS.md` 末尾追加，不回写或删除本次预注册定义。
