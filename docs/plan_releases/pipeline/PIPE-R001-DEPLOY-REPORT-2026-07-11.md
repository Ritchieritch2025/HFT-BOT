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
| 内核版本 | ⚠️ 未留存 uname 输出(操作员目视;晨检补记) |

## 未决项:07-10 首封(部署的第一份战果,不是缺陷)

首封被 check-caught-up 正确拒绝:`firehose_13.ndjson checkpoint=None
size=268435346` —— 昨天 13 点档 268MB raw **从未入库**,而今晨旧系统对
同一天发过"EXPORT PASS 291 files"绿灯(新导出实为 301 文件,另多 10 个
分区)。D2"绿灯不许撒谎"的教科书现场。处置:操作员补灌一次(被 Ctrl-C
打断留尾),补灌命令幂等,重跑即可;封印链每小时自动重试;
seal_alarm.json 按设计举旗(occurrences 递增)。**raw 零风险**:
prune_raw 只删已封印日,未封印的 07-10 raw 被自动保护。
遗留侦查题(→W03):hour-13 段为何未被日巡 ingest 发现(疑与轮转命名
相关,2026-07-06 rotation-shard 事故同族)。

## 窗口时间线

stop/reboot ~10:19 → boot + 服务自启 10:19–10:20 → 采集恢复(≤10:21:30)
→ 首封窗口 ingest 暂停至 10:24 → staging 恢复写入 10:24:58。
采集缺口 ≈ 2.4 分钟(待 capture_gaps 实测确认)。
