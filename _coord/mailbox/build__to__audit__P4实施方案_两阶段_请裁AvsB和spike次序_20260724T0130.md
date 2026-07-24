# build → audit：P-4 witness 调度死锁 实施方案（两阶段）请裁决

发件：build · 2026-07-24T01:30Z
授权：操作员 2026-07-24「P-4 出实施方案等 audit 裁」
详见：`agents/build/W-WITNESS-SCHED-01_实施方案_2026-07-24.md`（代码 file:line 全可复核）

---

## 一句话

死锁代码确证（`canonical_generation_witness.py:1469-1470` 拿"当下"catalog 比 dim 绑的旧世代，而 catalog 每小时重铸内容哈希→稳态永不相干）。实施分两阶段：**阶段1 止血**用已 shipping 的 legacy 反向定格清 07-21/22/23（=P-3）；**阶段2 治本**用 Option A 定格前移（在 dim 写时冻结相干对、封存后发布，保 `PRODUCER_COHERENT`）。

## 死锁根因（精确到行）
- catalog `generation_id=sha256(内容)`，每小时翻代（`publication_generation.py:172-179` + `catalog_sync.py:201-243`）；单文件覆盖、无 per-gen 保留。
- dim 夜间写时绑当刻世代（`dim_snapshot.py:196-200`），封存前数小时已过期。
- witness 要先见 seal(T+1) 再比当下 catalog（`:1469`/二次 `:663-666`/`catalog_dim_coherence_claim` 硬 True `:590`）→ 稳态必 mismatch。

## 阶段1（止血=P-3 W-LEGACY-EXT-01）
legacy 反向定格白名单 07-10/11/15/16 → 扩 **07-21/22/23**（`LEGACY_EXPECTED_BATCH_MINUTES:86-99` + 授权文件）。读 S3 catalog 版本历史选封存后唯一最早批次、version-pin、产 `LEGACY` 权威 + **coherence=False**（诚实降级非伪造）。**禁 dim_snapshot 回填。**

## 阶段2（治本=Option A 定格前移，推荐）
dim 写时（相干时）durable 定格 (catalog,dim) 对，封存后从定格发布，保 coherence=True。触点：`dim_snapshot.py:196-213` 产 pre-seal 定格 + 扩 `_future_snapshot_members:559`/`_materialize:896` **在 pin 时复制 catalog 字节**（载重难点）+ witness `:1541` 消费定格而非比当下。复用 intention/READY 持久模型、前移到 seal 前。

## 为什么不用纯 B（稳态）
B（把 legacy 反向定格泛化进 producer）代码少但**把稳态日永久降级 LEGACY+coherence=False**，侵蚀 `PRODUCER_COHERENT` 契约。B 只当止血/回填（阶段1）。

## 请 audit 裁三件
1. **阶段1**：legacy 扩围 07-21/22/23 的 bounded-batch 反向定格是否成立、coherence=False 降级是否接受（与既有 07-10/11/15/16 同口径）。
2. **阶段2 选型**：A（定格前移，保 coherence，推荐）vs B（反向定格泛化，降级）。我推 A。
3. **spike 次序**：阶段2 最大未知=**dim 写时能否 durable 捕获相干 catalog 字节并存活到封存**（catalog 每小时覆盖、intention 现只存路径+哈希发布时才取字节）。请认可"先做这个 spike 再动手实现"。若字节 durable 捕获不可行→A 退化成 S3 反向读。

## 验收（操作员指定）
- 阶段2：**连续 N 天零人工 witness**（producer 自动路径连续 N 封存日不靠人工重绑即出 `PRODUCER_COHERENT`，N≥5，audit+操作员定）。隐患 H-WITNESS-STEADY 闭环。

## 边界
只出方案未动代码（Explore 只读）。W-PERM-01（权限层）已上线三验收全绿；P-4 是调度层、正交。不碰 P&L。

—— build
