# PIPE-W06 — 操作员批准收据(2026-07-11)

- 批准对象:`docs/plan_releases/pipeline/PIPE-W06-TARGETED-L2-SPEC-2026-07-11.md`
  v1.0 @ commit `46bad42`,SHA-256
  `e00782c2fa9a52ec58e17be22b5cc4a79031f10504f3e0e4cef3f41a04403e85`。
- 传达途径:操作员会话消息,2026-07-11(07-10 封印补齐进行中同日)。
- 授权范围:Stage 0 今日执行 + 三阶段路线 + universe 构成;
  **每个后续阶段仍需单独 release**(见附加条件)。

## 操作员批准原文(VERBATIM)

```
OPERATOR APPROVAL — PIPE-W06 spec v1.0 (46bad42):

Stage 0 probe: APPROVED — run today (15 min, zero production writes, operator-gated tool).
Three-stage framework: APPROVED as the route; each stage still individually released.
Universe Tennis/MLB/Soccer/WNBA + controls (Esports/Golf/BTC15m): APPROVED.
Added condition: Stage 1 release requires (a) 2026-07-10 seal landed and raw pruning resumed, (b) W03 rotation-naming conclusion absorbed into the implementation, (c) probe-measured rates replacing the 30–100× bracket.
```

## Stage 1 硬门(操作员附加条件,逐字对应)

Stage 1 的 release 必须同时满足:
- (a) 2026-07-10 日封印落地 **且 raw 清理(prune_raw)恢复运转**;
- (b) **W03 轮转命名侦查结论已吸收**进 Stage 1 实现(hour-13 漏采根因);
- (c) **probe 实测速率已替换** 30–100× 倍率括号(规格 §4 数表更新)。

## Stage 0 执行注记(实施细节,不改变授权)

- 工具:`tools/depth_probe.py`(部署 tip 在册,`--operator-approved` 门控,
  拒绝 live 模式;写 `work/probe/`,不进 raw、不进仓、零 REST token)。
- 清单时效问题(执行侧发现):体育市场按日轮换,EC2 上既有的
  `depth_target_*.csv` 为旧日清单,直接用会订到大量已结算死市场、
  测不出速率。处置:用公开只读 REST(无凭证、不碰账户)生成**当日**
  四大类+对照组活跃市场清单,经 `--csv` 传入(工具既有参数,零代码改动);
  清单文件名刻意避开 `depth_target_*` 前缀,不污染其他工具的"最新清单"glob。
- 建议窗口:~22:30–23:30 UTC(MLB/WNBA 赛前+早场并行,Tennis/Soccer 仍活跃),
  满足"today"且四类均有活市场。

## Stage 0 第一次执行记录(2026-07-11 20:32Z)— BLOCKED,零生产影响,待操作员批修正命令

- 当日清单 20:23Z 生成成功:EC2 `work/mm/l2_probe_targets_2026-07-11.csv`,
  48 市场(Tennis/Baseball/Soccer/Basketball 12/12/8/8 + 对照 4/3/1),晚间
  数据比中午肥得多(Baseball 36h 活跃 503 个,top OI 206 万)。CSV 对
  22:30–23:30Z 复跑仍有效。
- **BLOCKER(runbook 缺口,非工具缺陷)**:批准的探针命令
  `source ~/.kalshi/env.sh && python3 tools/depth_probe.py …` 不完整——
  `src/env.cpp:160` ws_shadow 默认 `KALSHI_ENV=local_mock`,prod 还需
  `KALSHI_ALLOW_PROD=1`(env.cpp:169,fail-closed);这两个变量由生产
  supervisor(pipeline_supervisor.sh:39)注入,不在 env.sh 里。按原命令
  只会拨打不存在的本地 mock(127.0.0.1:18200 拒连循环,零交易所流量、
  零数据)。执行代理按纪律**未自授生产旗标**,已中止己方进程并上报。
- 生产全程健康(19:54/20:22/20:35Z 三查:capture 新鲜、SEAL_ALARM=NONE);
  遗留一个惰性孤儿进程(本地 mock 循环,900 秒上限 ~20:47Z 自灭,只写
  work/probe/ 下一个 ~93KB metrics 文件)。
- **待操作员裁决的修正命令(逐字)**:
  `cd /home/ubuntu/hft-bot && source ~/.kalshi/env.sh && KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 python3 tools/depth_probe.py --operator-approved --csv work/mm/l2_probe_targets_2026-07-11.csv`
  (与生产自身相同的环境选择;depth_probe 仍强制 data_collect 且双层拒绝
  live。)批准后于 22:30–23:30Z 用既有 CSV 复跑一次 15 分钟。

### 操作员批准(2026-07-11 ~20:5xZ,VERBATIM)

```
APPROVED — corrected probe command authorized as written (KALSHI_ENV=prod + KALSHI_ALLOW_PROD=1, sourced creds, read-only depth_probe, --operator-approved). Run in the 22:30–23:30Z window, but regenerate the target list immediately before the run (the 20:23Z CSV will be stale). Deliver the per-tier rate table verbatim.
```

执行修订(按批准):22:25Z 左右**重新生成**当日清单(覆盖同一 CSV 路径),
22:30Z 起跑;结果表逐字回呈。

## Stage 0 执行完毕 ✅(2026-07-11 22:44:31–22:59Z,第二次尝试成功)

- 清单 22:39Z 重生成(48 市场);probe 由主编排会话按操作员逐字批准的
  命令执行(权限系统正确拒绝了转述授权的子代理——授权在主会话内成立)。
- **结果:PASS。** 505,879 book msgs / 694.54 msg/s / 0 错误 0 掉线 0 丢包;
  L3、L6 实测通过;生产采集全程秒级新鲜(哨兵 4/4),SEAL_ALARM=NONE,
  transmitted=0。完整表:PLAN_DEPTH_EXPANSION §8;规格 §4 已更新。
- **Stage 1 硬门盘点**:(a) 07-10 封印 ✅ + prune_raw 恢复验证(待下次
  清理运行);(b) W03 结论吸收(未启动——Stage 1 关键路径);
  (c) **实测替换括号 ✅(本记录)**。
- EC2 工件:work/probe/depth_probe_20260711T224431Z.ndjson(~268MB 级
  capture)+ .metrics.ndjson;capture 不入仓(工具设计隔离,可作 Stage 1
  ingest 回放测试 fixture)。
