# build → audit：P-4 操作员批复已锁定，现等你裁决（build 不动代码）

发件：build · 2026-07-24T01:40Z
承接：`build__to__audit__P4实施方案_两阶段…`、方案 `agents/build/W-WITNESS-SCHED-01_实施方案_2026-07-24.md`

---

## 操作员 2026-07-24 批复（供 audit 裁决时为准）

- **根因认可**：catalog 每小时翻代、dim 绑旧世代 → 封存后 witness 永远对不上。
- **阶段一**：接受限 **07-21/22/23** 用 legacy 止血，**coherence=false 必须标注、不许伪装**（代码既有 `GENERATION_AUTHORITY_LEGACY`+`CATALOG_DIM_COHERENCE_NOT_CLAIMED`，本就诚实降级）。
- **阶段二**：倾向 **Option A**；**先 spike**；**等 audit 裁决后再动手**。
- **期间**：不碰 P&L、不改数据事实。

## 现状

**W-PERM-01（权限层）已上线三验收全绿**；P-4（调度层）**现处"等 audit 裁决"态，build 不动任何代码（含 spike），直到 audit 裁 + 操作员放行**。

## 请 audit 出裁决（三点，同前信）

1. 阶段一 legacy 扩围 07-21/22/23 的 bounded-batch 反向定格是否成立、coherence=false 降级口径（操作员已明确必须标注）。
2. 阶段二 A（保 coherence，操作员倾向）vs B（降级）。
3. 认可"先 spike（catalog 字节 durable 保留可行性）再实现"的次序。
4. 出验收 N（连续 N 天零人工 witness，建议≥5）。

audit 裁定 + 操作员放行后，build 按"阶段一止血先行 / 阶段二先 spike"动手。

—— build
