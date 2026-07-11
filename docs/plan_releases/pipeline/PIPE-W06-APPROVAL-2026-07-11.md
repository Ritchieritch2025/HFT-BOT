# PIPE-W06 — 操作员批准收据(2026-07-11)

- 批准对象:`docs/plan_releases/pipeline/PIPE-W06-TARGETED-L2-SPEC-2026-07-11.md`
  v1.0 @ commit `46bad42`,SHA-256
  `e00782c2fa9a52ec58e17be22b5cc4a79031f10504f3e0e4cef3f41a04403e85`。
- 传达途径:操作员会话消息,2026-07-11(07-10 封印补齐进行中同日)。
- 授权范围:Stage 0 今日执行 + 三阶段路线 + universe 构成;
  **每个后续阶段仍需单独 release**(见附加条件)。

## 操作员批准原文(VERBATIM)

```
OPERATOR APPROVAL — PIPE-W06 spec v1.0 (46bad42):

Stage 0 probe: APPROVED — run today (15 min, zero production writes, operator-gated tool).
Three-stage framework: APPROVED as the route; each stage still individually released.
Universe Tennis/MLB/Soccer/WNBA + controls (Esports/Golf/BTC15m): APPROVED.
Added condition: Stage 1 release requires (a) 2026-07-10 seal landed and raw pruning resumed, (b) W03 rotation-naming conclusion absorbed into the implementation, (c) probe-measured rates replacing the 30–100× bracket.
```

## Stage 1 硬门(操作员附加条件,逐字对应)

Stage 1 的 release 必须同时满足:
- (a) 2026-07-10 日封印落地 **且 raw 清理(prune_raw)恢复运转**;
- (b) **W03 轮转命名侦查结论已吸收**进 Stage 1 实现(hour-13 漏采根因);
- (c) **probe 实测速率已替换** 30–100× 倍率括号(规格 §4 数表更新)。

## Stage 0 执行注记(实施细节,不改变授权)

- 工具:`tools/depth_probe.py`(部署 tip 在册,`--operator-approved` 门控,
  拒绝 live 模式;写 `work/probe/`,不进 raw、不进仓、零 REST token)。
- 清单时效问题(执行侧发现):体育市场按日轮换,EC2 上既有的
  `depth_target_*.csv` 为旧日清单,直接用会订到大量已结算死市场、
  测不出速率。处置:用公开只读 REST(无凭证、不碰账户)生成**当日**
  四大类+对照组活跃市场清单,经 `--csv` 传入(工具既有参数,零代码改动);
  清单文件名刻意避开 `depth_target_*` 前缀,不污染其他工具的"最新清单"glob。
- 建议窗口:~22:30–23:30 UTC(MLB/WNBA 赛前+早场并行,Tennis/Soccer 仍活跃),
  满足"today"且四类均有活市场。
