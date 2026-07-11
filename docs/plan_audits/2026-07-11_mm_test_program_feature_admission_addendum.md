# 独立审计 — PLAN_MM_TEST_PROGRAM FA-1 Feature Admission/Decay 增补

- 日期：2026-07-11
- 范围：`docs/PLAN_MM_TEST_PROGRAM.md` 的第二次操作员增补、F2c/FA-1、SH-3、
  I4、执行顺序和 supporting-spec 绑定。
- 审计方式：独立只读 subagent 初审 → 执行者修订 → 定向复审。
- 最终裁决：**PASS**。
- 边界：仅审计本次 paper-only 增补；不授权任何研究、生产、shadow、微实盘或 live。

## 操作员原文与目标

> 加入计划 并且把计划给我

目标是把以下门加入 F2c，而非新建阶段：feature stability、相对市场基线的增量预测
力、配对经济增量、衰减/drift、复杂度与相关性检查，并把失效动作接入未来 shadow/
微实盘监控。

## 初审与修复

初审为 FAIL，两项 blocking 均已关闭：

1. **Live fail-closed 不完整。** 原稿只在 `DISABLE` 时停止新报价，没有撤销旧挂单，
   `DEGRADED` 也没有动作。修订后冻结 OK/WARN/DEGRADED/DISABLE 动作；后两者均
   stop-new + cancel affected resting，经 `CANCEL_PENDING` 确认 zero-resting/
   reconcile，闭环前不得 fallback；fallback 仅限当前 release 明确允许的已审计版本。
2. **Holdout 边界可误读。** 修订后六门、role/owner、阈值、相关性/复杂度和 admission
   结论全部只使用 nested TRAIN OOF；VALIDATION/HISTORICAL_CONFIRMATION 只接受/
   拒绝一个冻结整策略。正式 family/K 在 outer-TRAIN 检验前冻结，TRAIN 探索全登记，
   完整 registry 在 VALIDATION 前 hash-seal。

同时落实非阻断建议：sequence/source invalid 至少进入 DEGRADED/PAUSED；shadow
`Delta_e` 标 `simulated_counterfactual`，不得冒充真实成交因果增量；live 没有预注册
识别设计时也不得声称 feature-level causal effect。

## 最终复审

独立审计确认：

- `ALPHA`、`EXECUTION`、`RISK_SAFETY`、`MONITOR_ONLY` 角色边界不会用利润门误伤
  安全 feature，也不允许 monitor-only 影响订单；
- stability → predictive delta → economic `Delta_e` → collinearity/complexity → decay
  证据链闭合；
- 多重比较、holdout、drift 与 fail-closed 语义符合 V2.2 §§20/25/P06/P13；
- 无编号冲突、无暗中授权；
- 残余 blocking：**无**。

## 验证与未运行项

- `git diff --check`：PASS。
- V2.2 candidate 未修改；收尾前复核其 SHA-256。
- 代码/配置/生产路径：未修改。
- `make check`、`tests/run_pipeline.sh`：NOT RUN。纯文档增补无运行时行为；未来实现
  每个 FA/DP W 时仍必须按 E1 配测试并运行完整门。
