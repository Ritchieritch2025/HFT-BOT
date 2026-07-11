# PIPE-R001 维护窗口部署计划(2026-07-11 今晚)— 待操作员批准

**一句话:一个窗口做完四件事——W02 封印加固 + W-TL1 时间梯子上 EC2、apt 升级、
graceful 重启、全套 post-check;采集缺口有界、有标注、有回滚。**

- 部署 tip:`codex/pipeline-recovery-hardening` @ **e118a27743d5**
  (= 审计通过的 cfc7c10 + hotfix 合并(祖先修复)+ TL1 两提交移植
  7495173/57a7734 + 集成修正;TL1 自带 `_migrate_additive()` 对存量
  staging 做加列迁移,前向安全)。
- 审计封条条件:部署前**必须在部署机上重跑** make check +
  tests/run_pipeline.sh + 六个封印/TL1 测试文件,全绿并把输出引用进
  部署报告(下方步骤 0)。本计划撰写时的最近一次全绿运行:
  2026-07-11T09:33:56Z @ e118a27743d5(FAIL_count=0 · PIPELINE PASS ·
  seal+TL1 全 pass · registry ok 127)。

## 采集连续性声明(P4)

窗口内 capture **有一次计划内停止**(graceful stop → apt → reboot),
这正是 32ab4f6 预告的 graceful-stop 实测。缺口有界(预计 3–10 分钟),
事后三重记账:capture_gaps 自动记录 + quality_log 追加
deliberate_maintenance 标注 + SESSION_LOG。raw 为追加式文件,停止/回滚
均不损伤已落盘字节;S3 小时级 vault 在窗口前已含最新整点。

## 步骤(操作员执行;[Mac]=你的终端,[EC2]=SSH)

0. **[Mac] 审计条件重跑**(输出存入部署报告):
   cd /Users/ritcardo/HFT-BOT-pipeline-recovery && make check 2>&1 | grep -cE FAIL && bash tests/run_pipeline.sh 2>&1 | tail -1 && python3 -m pytest -q tests/test_pipeline_contract.py tests/test_warehouse_nonuniform_archive.py tests/test_export_day.py tests/test_timestamp_ladder.py tests/test_backtest_clock.py tests/test_jitter_report.py 2>&1 | tail -1 && python3 tools/check_registry.py | tail -1
   预期:0 / PIPELINE PASS / all passed / registry ok。任何一项不绿 = 窗口取消。
1. **[Mac] 推分支**:
   cd /Users/ritcardo/HFT-BOT-pipeline-recovery && GIT_SSH_COMMAND="ssh -i ~/.ssh/kalshi-key.pem" git push ec2 codex/pipeline-recovery-hardening
2. **[EC2] 预检**:
   systemctl is-enabled kalshi-pipeline   # 必须 enabled(重启后自拉起)
   df -h /home/ubuntu | tail -1           # 磁盘余量
3. **[EC2] ff 更新代码(服务仍在跑,旧进程握旧 inode,安全)**:
   cd /home/ubuntu/hft-bot && git fetch ~/kalshi.git codex/pipeline-recovery-hardening && git merge --ff-only FETCH_HEAD && git log --oneline -1
   预期落在 e118a27。ff 被拒 = 停,喊我。
4. **[EC2] 记录窗口开始 + graceful 停止**:
   date -u +%FT%TZ   # 记为 GAP_START
   sudo systemctl stop kalshi-pipeline   # 预期 <30s 干净退出(W-A5 陷阱修复的实测)
5. **[EC2] apt + 重启**:
   sudo apt update && sudo apt upgrade -y && sudo reboot
6. **[EC2 重连后] post-check 全套**(逐条留输出):
   a. systemctl is-active kalshi-pipeline        → active
   b. pid=$(cat /home/ubuntu/hft-bot/work/live/ingest.pid); kill -0 $pid && echo INGEST=ALIVE
   c. raw 在长:同一文件 60 秒两次 stat 尺寸递增
   d. TL1 生效(红旗检查):新行 recv 非 NULL —
      python3 - <<'PY'
      import duckdb,time
      con=duckdb.connect('/home/ubuntu/hft-bot/work/warehouse/staging.duckdb',read_only=True)
      boot_us=int((time.time()-600)*1e6)
      n,nr=con.execute("SELECT count(*),count(local_recv_ts_us) FROM trades WHERE ts_utc>?",[boot_us]).fetchone()
      print('new rows',n,'with recv',nr)
      PY
      → nr>0 且 ≈n。nr=0 = 红旗(ingest 没换代码/没重启)。
   e. 封印链后台化:grep "seal/research chain\|run_seal_chain" work/live/… supervisor 日志出现,
      且 ws_shadow 在链运行期间照常轮转;明晨 02:00 后 seals/date=<昨日>.json 出现、
      work/live/seal_alarm.json 不存在。
   f. 缺口标注:date -u +%FT%TZ 记为 GAP_END,然后
      echo '{"ts":"'$(date -u +%FT%TZ)'","kind":"deliberate_maintenance","gap_start":"<GAP_START>","gap_end":"<GAP_END>","reason":"PIPE-R001 W02+TL1 deploy + apt + reboot (planned graceful-stop live test)"}' >> /home/ubuntu/hft-bot/work/quality_log.ndjson
      (capture_gaps 会在明日归档扫描时把该缺口写入结构化记录。)
7. **[Mac] 部署报告**:步骤 0 与 6 的输出逐字贴进
   docs/plan_releases/pipeline/PIPE-R001-DEPLOY-REPORT-2026-07-11.md,提交。

## 回滚(任一 post-check 失败即执行)

1. cd /home/ubuntu/hft-bot && git checkout pipe-hotfix-01-disable-daily-research   # 回 a011fab(部署前状态)
2. staging 已被 TL1 加列迁移,旧代码的按位 INSERT 会拒——staging 是可重建
   派生层:mv work/warehouse/staging.duckdb work/warehouse/staging.duckdb.tl1 &&
   sudo systemctl restart kalshi-pipeline(ingest 从留存 raw 全量重建,
   raw 零损失,追平约需分钟级)。
3. 回滚后跑 post-check a/b/c 确认回到已知良好态;失败细节留给我复盘。

## 风险表

| 风险 | 缓解 |
|---|---|
| apt/reboot 后服务没起 | 步骤 2 先验 is-enabled;失败则 systemctl start + 看 journalctl |
| ff 被拒(EC2 有未知本地改动) | 停手喊我,绝不 force |
| TL1 迁移在真库上出意外 | 回滚含 staging 重建路径;raw 不受影响 |
| 封印链首夜异常 | 只影响研究门,不影响采集;seal_alarm.json 会举旗,次日修 |
