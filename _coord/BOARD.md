# BOARD — 任务板

> **只有策略 agent 写此文件。** 其他 agent 只读。
> 状态取值:`TODO` / `CLAIMED_BY_<agent>` / `IN_PROGRESS` / `WAIT_DECISION` / `DONE` / `BLOCKED`

## 当前 Sprint：Phase 0 + Phase 1（依据 docs/AGENT_BRIEF.md）

| # | 任务 | 负责 | 状态 | 结果去哪 |
|---|------|------|------|----------|
| T-01 | 造 T0:读现有已封存数据(成交带 taker 方向 + 盘口 + 结算),计数无缺口 | General | TODO | agents/build/RESULTS_T0.md |
| T-02 | 造 T1:市场分类器(单名/宽基/网球/其他体育/政治…),把全部市场归族 | General | TODO | agents/build/RESULTS_T1.md |
| T-03 | 造 T4:校准(逐族:隐含价 vs 真实结算率,10¢桶,等权+量权)+ 排名表 | General | TODO | agents/build/RESULTS_T4.md |
| T-04 | 审计 T0/T1/T4 产出(数据真/合规/样本量/无造假) | Audit(我) | TODO | agents/audit/ |
| T-05 | 出结论:哪个族 YES 高估最肥、够不够进 Phase 2 | Strategy | TODO | docs/ + BOARD |

## 待操作员决策
（agent 把 `WAIT_DECISION` 的问题+选项+推荐默认列在这里,操作员到 DECISIONS.md 回）

- 暂无(Phase 0/1 的决定已在 docs/AGENT_TASK_01_CALIBRATION_TEST.md 定为推荐默认)。

## 变更日志
- 2026-07-20 初始化(Cowork Claude 代建骨架;策略 agent 接手后维护)。
