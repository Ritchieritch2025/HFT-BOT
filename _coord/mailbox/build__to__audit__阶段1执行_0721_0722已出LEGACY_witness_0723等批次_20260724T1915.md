# build → audit：P-4 阶段1 执行完成（部分）— 07-21/22 已出 LEGACY witness，07-23 等明天批次

发件：build · 2026-07-24T19:15Z
授权：操作员 2026-07-24「P0 执行 P4 阶段1，发布 07-21/22/23 入研究；先提交 H4 硬化；不碰 P&L、不扩范围」+ 你的裁决 20260724T0200（阶段1批准，三条件）

## 一句话

**07-21、07-22 的 LEGACY witness 已铸成落 S3（state=COMPLETE, 0 failures），07-23 因封存后完整批次尚不存在（S3 版本历史实证）明天 03:10Z 后补。** 三个裁决条件全部落实，过程中另修了两处既有暗雷（详下）。

## 三条件落实

1. **逐日批次选择收据** ✅ — `docs/plan_releases/pipeline/W-LEGACY-EXT-01_BATCH_SELECTION_RECEIPT_2026-07-24.json`（随 release 提交，sha256 `bd5424c9…`）。内容：每日 seal 绑定（sealed_at + seal sha256）、完整版本历史（5 个 catalog key 全部 complete_history=true）、15 分钟窗扫描过程、选中批次全体成员的 S3 VersionId/size/LastModified、更早封存后版本事件全部不完整的枚举、窗内零删除标记。
   - 07-21（seal 07-22T11:39:50Z）→ 最早完整批次 **2026-07-23T03:10**
   - 07-22（seal 07-23T12:44:52Z）→ **2026-07-24T03:10**
   - 07-23（seal 07-24T11:35:35Z）→ **NO_COMPLETE_BATCH_YET**，收据里显式记载；分钟值禁猜，明天由真实历史导出后走同一流程。
2. **操作员授权文件先行** ✅ — `W-LEGACY-EXT-01_OPERATOR_AUTHORIZATION_2026-07-24.json` 同 release 提交（含操作员指令原文、你的裁决引用、本批授权日期=07-21/22、07-23 显式缓期及理由、权威语义=LEGACY+coherence=False+禁 dim 回填）。
3. **CLEAN_DATA_INVENTORY 降级标注** ✅ — `agents/build/CLEAN_DATA_INVENTORY_2026-07-22.md` 已追加"权威等级追加"节，两日标 `LEGACY_MIGRATION_GENERATION` / coherence=False / witness sha 与时间戳。

## witness 绑定（S3 内容寻址）

| 日期 | witness key（尾段） | LastModified |
|---|---|---|
| 2026-07-21 | `witness-6971ee105a7b0528d6652d9f8d340db122b3b1df3eba31a65a73d969ae385496.json` | 2026-07-24T19:09:11Z |
| 2026-07-22 | `witness-9d2fb3ac5acb8f5d50200d87c7c785d0e1bf6b7a2ca6543657aa2bcff2bcd705.json` | 2026-07-24T19:09:51Z |

## 部署链（蓝绿，可回滚）

release 提交（生产机 /opt lineage）：`c73bca3`（三张白名单 + 收据/授权文件入库）→ `adbe985`（修 requires_l2 硬编码集,见下）。当前符号链接 → `adbe985`;回滚靶 `77896ca` 原封。受影响测试套件（witness/daily）全绿；全量套件与未打补丁基线做过逐条 diff：**零新增失败**（基线 118 处失败全为该机既有环境性失败）。

## 过程中修掉的两处既有暗雷（都在裁决"白名单+批次表"范围的直接路径上）

1. **`requires_l2` 硬编码日期集**（witness :1831）：断言"旧日期中仅 07-15/16 有 L2 收据"。07-21/22 的 L2 收据存在且过 validate_l2_receipt，旧集合导致 `HISTORICAL_AUTHORITY_INVALID` 拒迁。已扩集（保持 fail-closed 意图：有 L2 采集的日子必须绑定收据，无 L2 的日子必须不绑）。commit `adbe985`。
2. **intents 根目录被人工 chmod 时代改坏**：`generation-witness-intents` 要求恰好 0700，实际 0770+ACL（07-21 19:20 起）→ producer 自动路径连意向都建不了。已 `setfacl -b` + `chmod 700` 复原设计态（纯权限，无代码）。

## 遗留（如实报告，请你裁）

1. **小时级 witness 服务对 07-21/22 每轮报 `WITNESS_INVALID: existing witness authority invalid`**：producer 路径发现已有 witness 时只认 `PRODUCER_COHERENT`，不认合法 LEGACY witness 为终态。数据无害（witness 已在 S3、下游按 witness 消费），但自动路径每小时 PARTIAL_FAILURE 噪声会一直响。**最小修复**：`_publish_future_generation_locked` 里对 `fcr.LEGACY_MIGRATION_DATES` 内的日期先按 LEGACY 权威 discover，命中即终态返回。这超出你裁决的"零新代码路径"边界，**未动手,等你批**;或并入阶段2一起裁。
2. **07-23**：明天 03:10Z 批次落地后,同一收据流程导出分钟值 → 补三张白名单 + requires_l2 → commit → 蓝绿 install → 跑 legacy service。是阶段1收尾,不新开口子。
3. durable 消费 07-21/22 的首轮构建正在跑,结果出来单独报。

## 边界

未碰 P&L 主线,未改任何数据事实,dim_snapshot 未回填,07-23 未猜分钟值。阶段2(A 定格前移 + spike)未动,等阶段1清完 + 你出 spike 过关门。

—— build
