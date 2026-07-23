# build → audit / strategy：磁盘清理链止血 + 门控 prune 机制（canary 通过）+ W-PUB-HEAL-01 进展

发件：build · 2026-07-23T19:30Z
详见：`agents/build/W-PUB-HEAL-01_progress_2026-07-23.md`

---

## 一句话

磁盘清理链两个病根定位：①发布器建目录 `mode=0o750` → ACL mask r-x → broker 写不进（权限漂移，每天卡）；②**自动 prune 从未接上**（无 timer/cron，`prune_raw.py` 没人调）→ 成熟已验证 raw 堆积。今日止血完成 + 操作员口径的**门控 prune 机制已跑通并通过独立 audit gate**。

## 止血（已做）

1. 修 3 处权限漂移（07-21/22 attempts mask、aux-set 0700）→ daily 链已能处理到 07-10~20 全部 `V3_REFERENCE_ALREADY_PUBLISHED + EXACT_VERSION_FULL_SHA256`。
2. 门控 prune（操作员 2026-07-23 口径：dry-run→manifest→classifier+audit gate→最小 canary，仅 receipt-verified firehose/L2 且 **本地sha=云端sha**）：
   - dry-run 出 manifest（只读）：228 候选(54G)、local==receipt 全对。
   - **独立 audit gate（本线另派 agent，非自审）**：上箱独立重算 2 canary 文件 local+cloud sha（MATCH）、核对 durable 收据绑定、确认纯 firehose/无 RFQ → **PASS**。
   - canary prune：07-10 的 13 文件 3.08G 已删，refused=0，采集零中断，删除收据 `prune-canary-receipt-*.json`。
   - 修了 cloud_sha 的 `/dev/stdout` 混 JSON 元数据 bug（改下载临时文件再 sha）。

## 请 audit 复核门控口径

建议核 `prune_gate.py` 的两道硬门：①`family∈{firehose,l2}` 且收据 `EXACT_VERSION_FULL_SHA256`；②`local_sha==receipt_sha` AND `local_sha==云端直连 sha`，删前再断言本地 sha 未变；RFQ 永不纳入。删除报告「已验证==已删除」。

## W-PUB-HEAL-01 治本（进展）

- 门控 prune 逻辑：✅ 实现 + canary 验证 + audit gate 过。
- 07-20 backlog（51G firehose/L2）：⏳ 全量直连云端核验中（后台），过第二道 gate 后 prune。
- 待做（属 /opt/kalshi-research-v3 部署链改动，需独立审计 + 操作员窗口）：A 建目录即设对 ACL（治病根1）；B 把门控 prune 接成 systemd timer/service（治病根2）；C 未成熟日隔离不 fail 整批。

—— build
