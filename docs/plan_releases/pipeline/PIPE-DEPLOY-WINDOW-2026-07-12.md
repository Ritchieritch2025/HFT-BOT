# 分阶段生产窗释放令收据(2026-07-12,Stage A = W03,Stage B = W06 Stage 1)

- received: 2026-07-12 ~05:18Z · deploy base: recovery branch `9f0e6a5`(代码 tip = a8e6b4b)
- 预检(05:18Z 实测):SERVICE=active · 07-10/**07-11 双封印在**(07-11 系现网代码
  整夜自动盖印)· SEAL_ALARM=NONE · MEM 3.5G/59.6G · 采集新鲜 · 已避开封印窗
- status: `STAGE_A_IN_PROGRESS`

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
