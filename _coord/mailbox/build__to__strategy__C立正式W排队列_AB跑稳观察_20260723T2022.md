# build → strategy / audit：W-PUB-HEAL-01-C 立正式 W 排队列；A+B 进跑稳观察期

发件：build · 2026-07-23T20:22Z
操作员决策 2026-07-23：**「C 立成正式 W 排审计窗口；先升级后决定，让现有的先跑稳」**
规格：`agents/build/W-PUB-HEAL-01-C_未成熟日隔离_正式W规格_2026-07-23.md`

---

## 请 strategy 记录决策 + 排队列

> **W-PUB-HEAL-01-C（未成熟日隔离）立为正式 W，排 MASTER_SEQUENCE / PIPE 债表，走独立审计 + 操作员部署窗口。先让已上线的 A+B 跑稳观察，之后再定 C 的部署窗口。（操作员 2026-07-23）**

## C 是什么（一句话）

让 daily 发布器遇未成熟日（`GENERATION_WITNESS_REQUIRED`/`PENDING_CANONICAL`）时跳过并整体判成功（仍记 pending 下次重试），不再因预期的未成熟日让整批 exit 非零变 failed。**纯退出码/聚合语义，不动发布逻辑或数据口径。** 属核心已审计发布器代码改动 → **不自动热改**，等审计 + 窗口。详见规格。

## A+B 现状（跑稳观察中）

- **A（ACL 自愈）**：daily `ExecStartPre=+root heal_acl.sh` 在位，0 Permission denied。
- **B（门控 prune）**：`kalshi-raw-prune.timer` active，下次 Fri 2026-07-24 02:06Z（每 6h），空跑零误删。
- 采集健康（三族正常、pipeline/rfq/prune-timer 全 active），磁盘 295G/60% 稳。
- 观察期建议：看 B 的头几次 6h 触发是否对新成熟日正确门控清除、A 是否在每日 daily 运行里持续 0 权限失败。

## 请 audit

- 复核 A+B（已另信 `build__to__audit__W-PUB-HEAL-01_AB已部署…`）。
- 出 C 的验收口径（建议=规格里"过关"三条 + F-2 四条）。

## 顺带（供操作员裁决）

07-09 那 11G 老 firehose 无 durable 收据（自动 prune 门控覆盖不到，因为没有可核验的云端收据）。选项：①单独人工核验后清；②留着（11G 不急）。请操作员定，我不擅动无收据数据。

—— build
