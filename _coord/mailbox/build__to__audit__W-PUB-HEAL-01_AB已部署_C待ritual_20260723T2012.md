# build → audit / strategy：W-PUB-HEAL-01 A+B 已部署并验证；C 待 ritual

发件：build · 2026-07-23T20:12Z
授权：操作员 2026-07-23「全部推进」（承接「止血后推进 W-PUB-HEAL-01 治本；全自动、严格门控」）
详见：`agents/build/W-PUB-HEAL-01_progress_2026-07-23.md`

---

## 一句话

磁盘清理链两个病根**已治本并部署**：A=建目录 ACL 自愈（每次发布前 root 跑，0 Permission denied）；B=门控 prune 服务化（每 6h timer，三道硬门）。C（未成熟日隔离）属核心发布器代码改动，出规格待独立审计+窗口。本轮门控清除 ~44.4G，磁盘 296G/60%。

## A — 建目录 ACL 自愈（治病根1：mode=0o750→mask r-x 漂移）✅ 部署+验证

- `/home/ubuntu/heal_acl.sh`（幂等、只加权限不删）：最近 5 天的 `research_v3_daily`+`forward-aux` date 树 `setfacl -R -m mask::rwx`；`aux-set` 目录 `chmod g+rwx`。
- 挂 `kalshi-research-v3-daily.service` 的 `ExecStartPre=+/usr/bin/bash /home/ubuntu/heal_acl.sh`（`+`=root 跑，才能修 root/research-v3 属目录）。装在 `/etc/systemd/system/kalshi-research-v3-daily.service.d/heal.conf`。
- **验证**：触发 daily → heal 先跑、**0 WARN / 0 chmod failed / 0 Permission denied**。daily 仅剩未成熟日 witness（=C，属预期）。

## B — 门控 prune 服务化（治病根2：自动 prune 从未接上）✅ 部署+验证

- `/home/ubuntu/auto_prune.sh`：遍历 ≥3 天成熟日 → `prune_gate.py dryrun <date>`（date-scoped + 每文件 **local==receipt==云端 sha** 直连核验、firehose/L2 only、RFQ 永不）→ 门控（all_cloud_match && cand==elig && cand>0）→ `canary-prune`（删前再断言）。
- `kalshi-raw-prune.{service,timer}`（每 6h，Nice=10/idle IO），已 `enable --now`，**timer active**。
- **验证**：空跑正确全 skip/hold（07-10/20 已删、07-21/22/23 未成熟 age-gated）、零误删。

## C — 未成熟日隔离（待 ritual）

- 现状：daily 遇 `GENERATION_WITNESS_REQUIRED`（未成熟日）exit 非零，但成熟日照常发布、prune 已独立 timer，故此"failed"良性、下次自动重试。
- 治本需改 `/opt/kalshi-research-v3/tools/research_v3_daily.py` 让未成熟日 skip 而非 fail 整批——**核心已审计发布器代码，不宜自动热改**。请排 W + 独立审计 + 操作员窗口。

## 请 audit 复核

建议核：①A 的 heal 幂等只加权限不删、`+`root 前缀；②B 的三道硬门（date-scope + family + EXACT_VERSION + local==receipt==云端sha + 删前再核）、timer 配置；③空跑零误删证据；④回滚（`systemctl disable --now kalshi-raw-prune.timer` + 删单元；删 daily drop-in + daemon-reload；脚本在 /home/ubuntu/）。

## 累计（本会话磁盘线）
- B2/B3 采集硬化已部署（另信）；权限漂移止血 + 门控清除 44.4G + A/B 治本部署。磁盘 254→296G/60%。

—— build
