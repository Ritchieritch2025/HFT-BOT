# 分阶段生产窗释放令收据(2026-07-12,Stage A = W03,Stage B = W06 Stage 1)

- received: 2026-07-12 ~05:18Z · deploy base: recovery branch `9f0e6a5`(代码 tip = a8e6b4b)
- 预检(05:18Z 实测):SERVICE=active · 07-10/**07-11 双封印在**(07-11 系现网代码
  整夜自动盖印)· SEAL_ALARM=NONE · MEM 3.5G/59.6G · 采集新鲜 · 已避开封印窗
- status: `COMPLETE — STAGE A ✅ + W07 RFQ ✅(追加授权)+ STAGE B ✅`
  - **Stage A(W03)**05:22:56.851→57.142Z 重启部署 tip 5561115:八项检查全绿
    (恰一进程、bash 5.2.21、无警报、AUTO_RESEARCH=0);07-09 积压 06:19Z 前
    自动清完(CATCH-UP PASS 54 文件;discovery 证据:0–17 点盘上无文件属
    史实)。补封 = 操作员决定,证据在。
  - **W07 RFQ(操作员单独授权激活)**:read-scope 专用钥匙(id 前缀
    9f939229,私钥未过网)、独立单元、验收 1–8 全绿、订阅 ACK 逐字留档;
    首小时即录真实 rfq_created/deleted 洪流(rfq_07 单文件打满 256MiB 轮
    转)。样本 CSV 200 行 = LIVE_KALSHI_PROD(EC2 + 操作员桌面)。env 两行
    由操作员显式授权后代理补齐(分类器两拒的诚实记录在案)。
  - **Stage B(W06 Stage 1)**:实现 commit 2f57178 变基为 a755fe5(RFQ 之后,
    零冲突,门禁复跑全绿),操作员批准(含"复用二进制"修正——实测
    WS_SHADOW_BINARY_CURRENT 未重建);07:12:24.058→.316Z 重启(258ms,已
    标注);验收:选择器首周期自跑(l2_targets.csv 50 市场,晨间网球领
    衔)、l2_07.ndjson 2 分钟 11.7MB(0 err/0 reconnect/0 drop)、无告警、
    firehose 与 RFQ 双双不受扰。一键停用 = work/live/l2_disable。
  - 生产 tip:**a755fe5**(W03+W07+W06S1);两次重启缺口均入 quality_log,
    实测对账待封印后 capture_gaps(明日缺口共 3 个:05:22 / 07:12 + RFQ 无
    主管道缺口)。

## 操作员释放令原文(VERBATIM)

```
Proceed with a staged production window. Stage A: deploy PIPE-W03 only, verify capture continuity, exactly one ingest/ws process, Bash >=4, no seal alarm, and allow the protected 2026-07-09 backlog to ingest with memory monitoring; do not run research or L2 concurrently. Verify full caught-up/discovery evidence before proposing any back-date seal. Stage B may deploy W06 Stage 1 targeted L2 only after Stage A is green and backlog pressure has cleared, using an isolated connection and one-touch disable. Stop/rollback on capture staleness, ingest crash-loop, seal alarm, swap growth, or memory exhaustion. No new governance work; report only deployment results and blockers.
```

## Stage A 计划(执行时逐项打勾)

0. 门禁重跑 @ 9f0e6a5(make check 0 FAIL + run_pipeline PASS)
1. push → EC2 ff → `systemctl restart kalshi-pipeline`(优雅,缺口按例标注)
2. post-checks:采集连续 · ingest/ws 各恰一 · bash≥4(B6-③)· 无封印警报 ·
   AUTO_RESEARCH=0 在位 · 无 L2 进程
3. 07-09 积压自动补灌(~10GB)内存监视;期间不跑研究/L2
4. 补灌完成后:`export_day --date 2026-07-09 --check-caught-up` 取全量
   caught-up + discovery 证据 → **只呈报,不补封**(补封 = 操作员另批)
5. 回滚预案:checkout e63b771 + restart(触发条件:采集变陈 / ingest 崩溃
   循环 / 封印警报 / swap 增长 / 内存耗尽)
