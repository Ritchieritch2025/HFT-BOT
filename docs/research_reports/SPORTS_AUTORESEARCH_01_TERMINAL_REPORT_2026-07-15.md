# SPORTS-AUTORESEARCH-01 — terminal report

> **DATA_INTEGRITY_BLOCKED · EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION**

Run ID: `20260715T112538Z__c21a79a8cff__deep01`  
Research commit: `6bcb5cb972865abc9c133bd9635c05540b9ced86`  
Operator stop effective: 2026-07-15T21:47:04Z

## 一行裁决

L1、L2 与不同盘口/Market Atlas 三模块保留为最终诊断报告；RFQ 完整扫描
没有形成结果，按操作员止损令关闭为 `DATA_INTEGRITY_BLOCKED`。repair-08
被明确禁止，后续不得自动继续。

## 数据与三模块结果

数据覆盖两个 `SEALED_DEGRADED_EVIDENCE` release：

- `2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03`
- `2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5`

L1/Core 使用 22,271,678 行 L1 与 7,952,620 笔 trades：
`C1-SPREAD-CAPTURE-01`、`C1-LARGE-FLOW-CONTINUATION-01` 为
`DATA_STARVED`；`C1-PREMATCH-TTS-01` 为 `COLLECT_MORE`（153 roots、
1 个评估日，低于 200 roots/20 days 门槛）。

L2 使用 48,897,095 行、485 个 markets：2026-07-13 receipt 有 1 个
sequence gap、8 个 missed frames，唯一评估日被隔离；
`C1-HFOLLOW-RETREAT-01` 与 `C1-DEPLETION-REFILL-01` 均为
`DATA_STARVED`。

盘口图谱覆盖 48,022 identity-clean market-days、35,148 个 markets、
382 个 provisional root events 与 9 个 sport。108 个三腿 family candidates
中 81 个互斥，但未证明 payout exhaustiveness；`C1-THREEWAY-OVERROUND-01`
与 `C1-SOCCER-POISSON-RV-01` 均为 `DATA_STARVED`。

## RFQ 终止边界

冻结选择为 282/284 个对象、59,185,856,724 bytes，2 个整对象、
534,038,690 bytes 被隔离。repair-06 在全局 dedup window 达到
42.8 GiB/42.8 GiB 后 OOM；repair-07 formal run 在创建 scratch 前因历史
evidence replay 根目录错误 fail-closed。其 resource receipt 为
`logs/resources/rfq_full_stage_repair07.json`，return code 1、218.733 秒、
SHA-256 `7dc5271d1588b7f797c9c7126d11a59b7c92f03712fc3bdbc044fcf72c165124`。

repair-01～07 的完整路径与哈希索引位于 run artifact
`REPORT/RFQ_BLOCKED.md`。3 个 RFQ 假设均为
`DATA_INTEGRITY_BLOCKED`、未测试；不得从 trigger-only 图表推断 RFQ 效应。

## 总账与边界

- 预注册 10；完成分析状态 7；`REJECTED=0`；`COLLECT_MORE=1`；
  `DATA_STARVED=6`；RFQ blocked/untested=3。
- `PROMOTION_READY=0`；formal `VERDICT_PASS=0`；shortlist 为空。
- Stage wall time 6,160.684 秒、stage cost 估算 $0.806535；从 orchestration
  start 到关机广播的 W09 elapsed 估算 8.935556 小时、$4.211327，费率
  $0.4713/h；不是 AWS billed cost。
- 没有可执行性、扣费后收益、保守成交、未见数据存活或实盘授权结论。

## Exit confirmation

- `2026-07-15T21:51:27Z`: W09 接受 `shutdown -h now`，广播
  `The system will power off now!` 后 SSH 正常断开。
- 随后对 `18.226.151.192:22` 的连接持续 timeout，主机已关机且不可达。
- 安装门 `W09_SHUTDOWN_BEHAVIOR_CONFIRMED=stop` 已要求并记录
  `InstanceInitiatedShutdownBehavior=stop`，所以 instance-initiated poweroff
  的配置结果是 stop，而不是 terminate。
- 本 Mac 没有 AWS CLI，且无可用浏览器登录会话，故本 session 没有直接读取
  EC2 控制面 `State.Name`；不得把这一点写成“控制台亲眼观察”。操作层面的关机
  已完成，若需要独立控制面证据，操作员可在 AWS Console 只读确认 `stopped`。
