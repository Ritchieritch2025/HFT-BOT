# RUNBOOK — 数据收集系统 & Dashboard 操作指令

一页纸操作手册。所有命令都在仓库根目录 `~/HFT BOT` 下执行。
安全底线:整条管道只读(data_collect),永不下单;凭证只放 `~/.kalshi/env.sh`,永不进仓库。

## 0. 一次性准备(只做一次)

```bash
# 0a. 创建凭证文件(把 PASTE_YOUR_KEY_ID 换成你的真实 key id)
printf 'export KALSHI_API_KEY_ID="PASTE_YOUR_KEY_ID"\nexport KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/private_key.pem"\n' > ~/.kalshi/env.sh && chmod 600 ~/.kalshi/env.sh

# 0b. 清理遗留进程(旧 tradingd + 多余的 dashboard 实例)
pkill -f "build/tradingd" ; pkill -f "dashboard_server.py"
```

## 1. 数据收集系统(三层管道)

**⚠️ 割接后现状(2026-07-09 23:09 UTC 起):生产管线跑在 EC2 上(见 §5),
Mac 是休眠回滚载体。下面的 Mac 启动命令是【回滚/应急路径】,平时不要跑——
在 EC2 采集正常时启动它会造成双机采集(WS 双跑无害但浪费;真正回滚时
还需先删 work/live/rest_disabled 才恢复 Mac 的 REST,并停掉 EC2 侧,
守住单一 REST 所有者铁律,详见 plan_audits/wA4_cutover_2026-07-09.md)。**

```bash
# 回滚/应急启动(launchd 守护:崩溃自动拉起,开机自启)
cp deploy/com.ritcardo.kalshi-pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ritcardo.kalshi-pipeline.plist

# 停止
launchctl unload ~/Library/LaunchAgents/com.ritcardo.kalshi-pipeline.plist
pkill -f pipeline_supervisor.sh ; pkill -f "ingest.py --loop" ; pkill -f build/ws_shadow

# 临时前台启动(不用 launchd,调试用;Ctrl-C 停止)
bash tools/pipeline_supervisor.sh
```

启动后自动做三件事:①ws_shadow 全市场 firehose 实时写小时级 raw 日志
`work/raw/date=<日期>/firehose_<小时>.ndjson`;②ingest 守护每 60 秒吸入
`work/warehouse/staging.duckdb`(变化才记录 + 每小时心跳);③候选的新封存流程在
UTC 02:00 后才处理前一天:先证明所有 closed raw 字节都已 checkpoint,再导出、
逐行核对 staging↔archive、写 day seal,之后历史研究才可运行。该 supervisor
切换须等 capture 拆分后部署;当前生产 supervisor 仍是旧流程。

```bash
# 运行状态检查
python3 tools/feed_readiness.py            # 环境/凭证/feed 新鲜度
./tools/warehouse_status.py                # staging 行数 + 归档分区 + 数据来源
tail -5 work/live/ws_shadow.log            # 采集器心跳
tail -5 work/live/ingest.log               # 入库进度
tail -5 work/live/export.log               # 午夜导出结果
python3 tools/lifecycle_check.py           # 全生命周期就绪度

# 一次性 30 秒只读交易所体检(不开管道时用)
source ~/.kalshi/env.sh && tools/exchange_check.sh --env prod --ws-seconds 30

# 手动验证/封存某一天(必须先停 ingest writer;不触碰 capture)
python3 tools/export_day.py --date 2026-07-06 --check-caught-up
python3 tools/export_day.py --date 2026-07-06
python3 tools/export_day.py --date 2026-07-06 --verify-only
python3 tools/export_day.py --date 2026-07-06 --seal
python3 tools/export_day.py --date 2026-07-06 --verify-seal
# WRITE-ONCE:已封印日再跑 --seal = 只验不写;坏印需操作员显式拆印(留档+记账):
python3 tools/export_day.py --date 2026-07-06 --operator-invalidate-seal "<原因>"
# 存量历史(seal 系统之前的旧日子)一次性 legacy 封印(永无 go/no-go 判决权):
python3 tools/export_day.py --date 2026-07-05 --legacy-seal
# raw 清理:只删已封印日(fail-closed,报告 work/live/raw_retention_alert.json):
python3 tools/prune_raw.py --retention-days 2 --dry-run
```

## 2. Dashboard(操作台,localhost 只读)

```bash
# 启动(端口 8765;--allow-network 允许面板里跑只读网络检查,永不允许下单类工具)
python3 dashboard_server.py --metrics work/metrics.ndjson \
    --results work/test_results.ndjson --port 8765 --allow-network &

# 打开
open http://127.0.0.1:8765

# 停止
pkill -f "dashboard_server.py"
```

面板:Feed Status / Market Feed Tape(实时行情带)/ Warehouse(入库+归档状态)/
Tests / Tools。只 tail 文件,不碰交易热路径。

## 3. 查数据(单一入口 load())

```bash
# 命令行任意切片(当天=staging 实时,历史=归档文件,自动路由)
python3 tools/warehouse.py trades --category Sports --group MLB --start 2026-07-06
python3 tools/warehouse.py orderbooks_l1 --category Crypto --ffill --limit 20
```

```python
# Python 里
import sys; sys.path.insert(0, "tools")
from warehouse import load
df = load("trades", category="Sports", group="MLB",
          start="2026-07-06", end="2026-07-06",
          archive_only=True).df()  # requires a valid day seal; never opens staging
```

层级:`category → subcategory(=tags[0],如 Baseball) → group(推导联赛/资产,如 MLB)`。

有 start+end 且窗口完全早于今天的 `warehouse.load()` 会自动切换到
archive-only,并要求每一天的 seal、raw SHA-256/文件清单及 archive
manifest/文件 MD5 全部仍有效。旧归档没有 seal 时会硬失败,不会退回 live
staging;迁移/合成 fixture 若确实需要旧混合语义,必须显式传
`archive_only=False`。seal 的 `capture_quality_status` 在 PIPE-W03 前仍是
`UNASSESSED`,不能当作盈利或实盘 gate。

## 4. 全量测试(改代码后)

```bash
make check              # 纯/离线测试 + 仓库 gates + 新仓库测试
./tests/run_pipeline.sh # 离线全家桶(含 mock 交易所)
```

## 5. EC2 盒子 —— 生产主机(固定 Elastic IP 3.130.232.109;us-east-2,r8g.large 2核/16GB/200GB)

```bash
# 登录(密钥在 ~/.ssh/kalshi-key.pem,chmod 400)
ssh -i ~/.ssh/kalshi-key.pem ubuntu@3.130.232.109   # Elastic IP,停/起不变(2026-07-09 起)

# 送代码上盒子(Mac 上执行;盒子上永远没有 GitHub 凭证,S4)
git push ec2 <分支名>                       # Mac → 盒子裸仓库 ~/kalshi.git
ssh ... 'cd ~/hft-bot && git pull'          # 盒子工作区更新

# 盒子上的服务 —— 2026-07-09 23:09 UTC 起这就是生产管线(enabled+active)
sudo systemctl status kalshi-pipeline       # 采集管线(应为 active;停=事故)
systemctl list-timers | grep kalshi         # oom-guard 每分钟护栏(已启用)
chronyc tracking                            # 时钟偏移(应 <1ms,Amazon Time Sync)
swapon --show                               # 16GB 交换区(一体机护栏)

# 重装/修复:重跑装机脚本(幂等,不会启动采集)
cd ~/hft-bot && bash deploy/bringup_ec2.sh
```

安全清单(SSH 限 IP、出站收紧步骤):`deploy/SECURITY_CHECKLIST_EC2.md`。
三条 launchd 关键行为如何在 systemd 复现:`deploy/README_EC2.md`。

## 6. EC2 稳态运维面(W-A5 起)

```bash
# 三个定时器(都应 enabled;停了 = 异常)
systemctl list-timers | grep kalshi   # oom-guard(每分钟) s3-sync-hourly(:05) s3-sync-daily(03:10Z) alert(每分钟)

# 报警流(仪表盘数据源;Telegram 配好后同时推手机)
tail work/live/alerts.log             # 无文件 = 一直健康
journalctl -u kalshi-alert -n 5       # 报警器自身的运行记录

# metrics 轮转(512MB 保 3 代;rider a)
ls -la work/metrics.ndjson*           # 超 512MB 后应出现 .1/.2/.3

# S3 同步核对
journalctl -u kalshi-s3-sync-hourly -n 3
aws s3 ls s3://kalshi-vault-ritcardo/ec2/raw/ --recursive | tail -3

# 已知行为:supervisor 重启后 ingest 可能滞后 ~20-30 分钟(导出链竞争
# staging 写锁,watchdog 会自动拉起;从 raw 偏移量追平,无数据损失)。
# 重启纪律:避开 00:00-00:15 UTC 导出窗口。
```

Mac 侧唯一常驻任务:com.ritcardo.kalshi-report-pull(每日 09:00 拉 EC2
reports/ 落桌面 TradingSys Report;日志 /tmp/kalshi-report-pull.log)。
