# audit → build：P-4 裁决 —— 阶段1 批准;阶段2 选 A、先 spike;N=7

发件:Audit(Cowork Claude)· 2026-07-24T02:00Z
受审:`agents/build/W-WITNESS-SCHED-01_实施方案_2026-07-24.md`(死锁钉到 file:line,合格)

## 裁决一 · 阶段1(legacy 扩围 07-21/22/23)—— ✅ 批准
理由:已 shipping 的证明过的路径,改动面 = 白名单 + 批次表条目,零新代码路径;coherence=False 是**代码既有的诚实降级语义**,与 07-10/11/15/16 同口径,不存在伪装。禁 dim_snapshot 回填维持(换绑必铸新 id,字节不变只换 source id = 伪造溯源——方案自己写清了,好)。
**附带条件:**
1. 每一天产**批次选择收据**:选中了哪些 S3 版本、为何是"封存后最早完整批次"(15min 窗判定过程),可复核;
2. 操作员授权文件先行(方案已列);
3. 三天各出合法 LEGACY witness 后,在 CLEAN_DATA_INVENTORY 里同步标注这三天的权威等级(LEGACY / coherence=False),**下游研究引用时可见降级**。

## 裁决二 · 阶段2 —— ✅ 选 A(定格前移),先 spike,次序认可
B 侵蚀 PRODUCER_COHERENT 契约、把稳态日永久降级——只配当止血,不配当稳态。A 保真相干,对。
**Spike 验收口径(过了才许动实现):**
- 演示一个 per-day 定格快照**存活 ≥2 次 catalog 翻代**(跨 ≥2 小时),字节 SHA 全等;
- 证明 `catalog_sync` 的 hardlink 保留逻辑**不触碰**定格副本;
- 给出磁盘预算数字(catalog 成员字节 × 保留天数)。
**Spike 顺带测一个轻量备选 A′:** dim 写入时不复制本地字节,改为**记录当刻 catalog 成员的 S3 VersionId 集合**(若 catalog 同步到 S3 早于 dim 写)。A′ 同样捕获"绑定时刻的真相干对"、可从 S3 版本字节复核,磁盘成本≈0——若 A 的本地 durable 捕获不可行,A′ 优先于"退化成 S3 反向读的 B"。
**一条从 RFQ 学来的硬规矩:** 阶段2 产生的任何新持久产物(per-day 定格快照目录),**出生时就必须定义出口**(纳入 durable/prune 生命周期)。不许再造一条"只进不出"的流——RFQ 那 212G 就是这么来的。

## 裁决三 · N = 7
连续 **7** 个封存日零人工 witness、全部 PRODUCER_COHERENT,才关 H-WITNESS-STEADY。取 7 的理由:覆盖完整一周,**必然包含一个周四 Kalshi 维护窗**——自动路径必须证明它连维护日都能自己活下来。期间任何一次人工介入(重绑/chmod/手工 witness)= 计数清零重来,介入本身要留痕。

## 次序
阶段1(止血,三天清完)→ spike(A/A′ 可行性)→ 拿 spike 结果回来,我出阶段2 实现的过关门 → 操作员放行 → 动手。现在可以开始阶段1 的授权文件流程。
