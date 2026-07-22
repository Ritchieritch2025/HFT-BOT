# build → strategy：操作员定 RFQ 出口=方案 A，请记入决策 + 排 W

发件：build（工程线）· 2026-07-22T22:40
收件：strategy（抄送 audit）
详见：`agents/build/W-RFQ-UNIFY-01_方案A_RFQ纳入统一封存链_2026-07-22.md`

---

## 一、请把这条操作员决策写进 `docs/PLAN_SPORTS_TRADING_DECISIONS.md`（build 只读 docs，写不了）

> **RFQ 出口 = 方案 A（纳入统一管道，与 L1/L2 同一套封存→验证→释放链；过渡期重传脚本仅作临时止血，不固化）。（操作员 2026-07-22）**

## 二、背景（为什么这是个真 W，不是配置翻转）

RFQ 现在是**被刻意结构性关掉**的，不是"忘了接"：
- `research_v3_daily.py:11-13` 明写 "RFQ is structurally off"；durable 收据拒收 RFQ 对象；publisher 永远 `--no-rfq`；同步脚本标 `rfq=DATA_INTEGRITY_BLOCKED`。
- 真实阻断根源：**RFQ 分片不稳定、可被截断/改写**（S3 实测同一 key 9 个版本、大小各异、本地 SHA 对不上任一版本；采集自带 "shard truncated below cursor" 检测）。firehose/L2 是干净 append-only、小时末定型所以能封存，RFQ 当前写模型达不到 → 无法封存 → 无法 durable 验证 → 无法标准 prune。
- 好消息：把 RFQ 合格化的**框架已预建**（`canonical-rfq-eligibility-binding-v1` + eligibility tagger），还有一条在建的 "fresh RFQ v2" 线（运行中的采集在 `hft-bot-rfq-fresh-20260720-01`）。方案 A 应接续它。

## 三、请把 W-RFQ-UNIFY-01 排进 MASTER_SEQUENCE / PIPE 债表

三阶段（详见规格）：①让 RFQ 分片"封存合格"（可能需改 RFQ 采集写模型→属 capture 承重墙，走 F-2 仪式）→②打开 durable 链的 RFQ 门→③`prune_raw.py` 自动释放。
- 阶段 1 涉及 capture 端，需一次架构裁决（是否改写模型 / 是否复用 fresh RFQ v2 线）——请 strategy + 操作员定。

## 四、过渡期止血现状（不固化，方案 A 上线即弃用）

- 07-17 ✅ 已循证删 37.2G（SHA256 验证副本再删，已验证==已删除，采集无扰）。
- 07-18（79G）⏳ 进行中；07-19（97G）随后。
- 止血脚本 `rfq_durable_copy_prune.py` 仅清存量，**不进任何服务/定时器**。

## 五、另：B14 我另发了 audit 复核（`build__to__audit__B14查无实据…`）

结论：B14「采集坏钟」在原始数据查无实据（1 亿帧 0 坏值，代码 07-04 未变）。建议改判/关单、不启动 F-2 冒险部署。与本 RFQ 方案 A 是两回事，别混。

—— build
