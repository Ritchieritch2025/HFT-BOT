# 独立审计 — PLAN_MM_TEST_PROGRAM F2c/F2d 动态定价增补

- 日期：2026-07-11
- 范围：`docs/PLAN_MM_TEST_PROGRAM.md` 的状态/操作员增补块、F2c、F2d、F3
  交叉引用、执行顺序与旧计划绑定文字。
- 审计方式：独立只读 subagent 审查当前 diff；执行者按 finding 修订后复审。
- 最终裁决：**PASS**。
- 本审计只覆盖本次增补，不把原 `PLAN-ONLY` 全文升级成已授权计划。

## 操作员授权边界

操作员原文：

> 加入计划吧

上下文仅指两项纸面测试要求：动态定价消融矩阵，以及价格带 × tick × 显示数量 ×
requote 微结构实验。没有研究执行、生产变更、外部服务、shadow、微实盘或 live
授权。V2.2 candidate 未改，复核 SHA-256 仍为
`575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54`。

## 初审 findings 与处置

初审为 FAIL，修订后全部关闭：

1. 临时 B0–B6 与既有 B1–B3 撞号 → 入库唯一编号 `DP-0`–`DP-6`。
2. policy feature 与 W-FS1 queue/cancel 环境混合 → 所有 DP 行共用同一状态机；
   DP-1 独占 toxicity eligibility/exit/re-entry，DP-5 只处理仍 eligible 时的
   refresh/cancel-replace。
3. 只报各行 PnL 不能证明增量 → 增加配对事件
   `Delta_e = NetPnL_e(DP-k) - NetPnL_e(DP-(k-1))`、common random numbers 和
   calendar-day block-bootstrap CI。
4. holdout 可能反复打开 → 逐项比较只在 nested TRAIN out-of-fold；VALIDATION 与
   test/HISTORICAL_CONFIRMATION 只验证一个预先冻结策略；K>1 正式 family 用 Holm。
5. >1 contract 越过 V2.2 binding estimand → 1-contract 保持唯一主轨；DP-6 和更大
   size 仅 `DIAGNOSTIC/CAPACITY_ONLY`，升级需 sweep-volume/own-order 证据与新授权。
6. replay 的“真实 token”可能暗示 API 写入 → 改为 version-pinned simulated
   token-bucket debit，明确零 API mutation。
7. 连续 L2 被误当作 queue 真相充分条件 → 无 disjoint own-order calibration 时，
   queue-ahead/priority 只报区间和诊断，strict-through 仍是主轨。
8. tick/price-band/grid 边界不足 → 增加 decision-time E4 分桶、官方/实测 tick、
   post-only/non-crossing、有限 grid/总 K/power/min-block 和跑后禁扩格要求。
9. `DECISIONS` 纯度风险 → 不写新的 agent-authored DECISIONS 条目；逐字操作员原文
   与范围保存在被修改的计划及本审计中。

## 最终复审

独立审计员最终确认：

- DP-1 与 DP-5 ownership 冲突已关闭；
- F3 的逐项比较与单次 holdout 规则不再冲突；
- 编号、1-contract authority、多重比较、queue authority、rate-token/live 歧义均已
  关闭；
- V2.2 candidate 字节未改；
- 残余 blocking finding：**无**。

## 验证与未运行项

- `git diff --check`：PASS。
- 代码/配置/生产路径：未修改。
- `make check`、`tests/run_pipeline.sh`：NOT RUN。原因：纯文档增补没有运行时行为，
  且 V2.2 已记录全套测试会写派生 operational/dashboard state；本次不以无关测试
  制造副作用。未来实现每个 DP/F2d W 时仍须按 E1 为新行为配测试并跑完整门。
