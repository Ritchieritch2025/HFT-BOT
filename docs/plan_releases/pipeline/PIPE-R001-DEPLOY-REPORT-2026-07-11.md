# PIPE-R001 部署报告(2026-07-11 维护窗口)

**裁决:部署成功 ✅(全部 post-check 绿);07-10 首封为未决项 ⚠️(原因是
新门抓到了真实缺数据,非部署缺陷;raw 受封印门控保护,零数据风险)。**

## 审计条件门(部署前重跑,原样引用)

```
=== PRE-DEPLOY GATE RUN (audit condition) 2026-07-11T09:58:47Z @ f4769ad4cf56 ===
make check FAIL count:      0
tests/run_pipeline.sh:      == PIPELINE PASS ==
seal + TL1 test files:      18 passed (100%)
check_registry:             registry ok: 127 tools, 45 build targets covered
worktree:                   clean
```

## 执行记录

- 部署 tip f4769ad(含 W02+TL1+批准记录)推送 EC2 并 ff(操作员执行)。
- apt:11 包升级,新内核 6.17.0-1019 安装。
- 重启:第一次命令粘贴串行未执行(`sudo rebootssh` not found,当场发现);
  补跑 `sudo reboot` 成功。服务 systemd 自启。
- 缺口标注:quality_log 追加 deliberate_maintenance
  (gap 10:19:09→10:21:30,操作员记录值;capture_gaps 的实测记录出来后
  以实测为准对账)。

## Post-checks

| 项 | 结果 |
|---|---|
| systemd active | ✅ |
| ingest | ✅(首封窗口设计内暂停 10:19–10:24 后复活;**新重试代码当晚在生产自证**:`[ingest] staging lock ... retrying up to 150s` 后成功接管——今晨同场景旧代码直接死亡) |
| raw 增长 | ✅ 两次独立读数递增(10:21:39 操作员 / 10:21:40 本会话,1 秒差 176KB) |
| TL1 recv 物化 | ✅ **铁证**:真实 raw 尾部 200 行经已部署 Ingester 入临时库,trades with_recv=30/30(100%) |
| 封印链后台化 | ✅ 链自动运行、ws_shadow 不等封印、告警机制工作(见下) |
| 内核版本 | ✅ `6.17.0-1019-aws`(**原样回执归档**:操作员 2026-07-11 ~12:03Z 亲跑 `uname -r` 粘贴回传;此前出处仅为 apt 升级记录 + 目视,现已闭环) |

## ~~未决项~~ 已解决:07-10 首封(部署的第一份战果,不是缺陷)

**RESOLVED 2026-07-11 13:20:10Z**:`seals/date=2026-07-10.json` 落地,
`SEAL_ALARM=NONE`,ingest 接锁正常(pid 19427)。双源确认:上会话遗留
监视器 13:20:10Z 首报 + 本会话 ec2_health 13:20:45Z 独立复核。时间线:
操作员 10:45:36Z 经 stdin 重启补灌(PID 3175,heredoc 形态,未死于 Ctrl-C)
→ 12:59 写毕(库 2,307MB,WAL 收缩释放 ~20G 磁盘)→ 13:07–13:11 间退出
(内存 -2.4G 为证)→ 守护接锁追平 → 封印链 13:20 盖印、清警报。全程零
数据丢失、采集零缺口。衍生:BACKLOG B4(链不识别他人 export_pause)。

首封被 check-caught-up 正确拒绝:`firehose_13.ndjson checkpoint=None
size=268435346` —— 昨天 13 点档 268MB raw **从未入库**,而今晨旧系统对
同一天发过"EXPORT PASS 291 files"绿灯(新导出实为 301 文件,另多 10 个
分区)。D2"绿灯不许撒谎"的教科书现场。处置:操作员补灌一次(被 Ctrl-C
打断留尾),补灌命令幂等,重跑即可;封印链每小时自动重试;
seal_alarm.json 按设计举旗(occurrences 递增)。**raw 零风险**:
prune_raw 只删已封印日,未封印的 07-10 raw 被自动保护。
遗留侦查题(→W03):hour-13 段为何未被日巡 ingest 发现(疑与轮转命名
相关,2026-07-06 rotation-shard 事故同族)。

## 同日追加维护(16:32–16:45Z):HOTFIX-02 浸泡中 + 换型 r8g.2xlarge ✅

当日下午两起连续动作(详情见各自档案,此处登记核验与缺口):
1. **PIPE-HOTFIX-02(自动研究保险丝)** 16:20Z 部署——独立审计 PASS,释放
   文档 `PIPE-HOTFIX-02-AUTO-RESEARCH-FUSE-2026-07-11.md`(另一会话执行,
   其 SESSION_LOG 条目 16:25Z)。背景:研究链两次把 16G 机器打穿
   (含 14G swap 动用,操作员+root 会话先后终止 PID 21331/27344)。
2. **换型 r8g.large → r8g.2xlarge**(操作员执行,16:32:09Z 停机(journal
   实测)→ 16:36:13Z 开机,停机约 4 分钟)。

本会话收尾核验(2026-07-11 16:4x–16:50Z,操作员授权只读命令):
| 项 | 读数 | 判定 |
|---|---|---|
| 箱上代码 | `git log -2` = e63b771(HOTFIX-02)@ f4769ad | ✅ 与 HOTFIX-02 记录的 runtime hash 一致 |
| CPU | nproc = **8** | ✅ 2xlarge 到位 |
| 内存 | free -g total **61Gi**(=64GB) | ✅,swap 0 |
| 采集/入库 | raw 16 点档持续写入;staging 恢复推进 | ✅ |
| 缺口标注 | quality_log 追加 deliberate_maintenance,**16:32:09→16:45:35Z 保守括号**(实际恢复约 16:37;终点取第一笔有实证 raw 写入,宁宽勿漏),实测以封印后 capture_gaps 对账 | ✅ 16:50:26Z 已入账 |

待观察:HOTFIX-02 两次整点浸泡检查(自删心跳);**07-11 日封印(07-12
02:00Z 后)**——预期正常盖印(07-10 同构且已顺利盖印;若有异常,
seal_alarm 03:00Z 会举旗,次日晨检收口)。另:重启追赶期 ingest 峰值
12.9G RSS(无界尾部物化)= W03 技术债,HOTFIX-02 会话已登记。

## 窗口时间线

stop/reboot ~10:19 → boot + 服务自启 10:19–10:20 → 采集恢复(≤10:21:30)
→ 首封窗口 ingest 暂停至 10:24 → staging 恢复写入 10:24:58。
采集缺口 ≈ 2.4 分钟(待 capture_gaps 实测确认)。

## 晨检对账状态(2026-07-11 11:27Z 补记)

- **capture_gaps ↔ quality_log 对账:PENDING(结构性等待,非遗漏)。**
  capture_gaps 按设计只在日封印之后跑(supervisor run_seal_chain 封印成功
  才触发);07-11 的实测缺口记录要等 07-11 封印(≥07-12 02:00Z)。届时对账
  项:实测缺口应 ≈ 10:19:09→10:21:30(quality_log deliberate_maintenance
  标注,操作员记录值),以实测为准修正标注。下一场晨检收口。
- 07-10 首封仍未决:seal_alarm occurrences=2(10:23Z 首见);ingest 守护
  本会自动扫"昨天+今天"raw(源码核对 raw_files_to_scan),故 07-10 的
  firehose_13 今日会被自动补灌,封印链每小时自动重试——处置见 SESSION_LOG。
- **12:03Z 破案补记(staging 锁竞争)**:三次探针(11:27/11:39/11:49Z)见
  staging 被 PID 3175 连续持锁写入(+6MB/min),正牌守护 20 分钟换 4 个
  pid(6640→8082→8745→9127)等锁循环。操作员 `ps -p 3175` 回执:
  `.venv/bin/python3 -`,START 10:45:36Z——**即操作员 10:45 经 stdin
  (heredoc 形态)启动的补灌本灌,未被 Ctrl-C 杀死,存活至今**;其
  export_pause 暂停牌被 11:00Z 封印链收尾的 `rm -f export_pause` 误删
  (链不检查既有 pause——设计缺口,登记 BACKLOG),此后补灌与守护抢锁
  (DuckDB 单写锁保证无损,仅浪费)。**处置(操作员已知会)**:时限
  13:05Z,届时仍持锁则 `kill -TERM 3175`(checkpoint+facts 原子入账,
  零丢失),守护接手补齐,封印链整点自动盖印。W03 侦查线索不变。
