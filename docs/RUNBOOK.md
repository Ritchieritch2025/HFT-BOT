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

```bash
# 启动(推荐,launchd 守护:崩溃自动拉起,开机自启)
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
`work/warehouse/staging.duckdb`(变化才记录 + 每小时心跳);③每个 UTC 午夜
(北京时间早 8 点)把前一天导出成最终文件(trades=csv.gz,订单簿=zstd-15
Parquet)并写 manifest。

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

# 手动补导出某一天(平时不需要,supervisor 自动做)
python3 tools/export_day.py --date 2026-07-06
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
          start="2026-07-06", end="2026-07-06").df()
```

层级:`category → subcategory(=tags[0],如 Baseball) → group(推导联赛/资产,如 MLB)`。

## 4. 全量测试(改代码后)

```bash
make check              # 纯/离线测试 + 仓库 gates + 新仓库测试
./tests/run_pipeline.sh # 离线全家桶(含 mock 交易所)
```

## 5. EC2 盒子(W-A1 起;13.59.9.97,us-east-2,r8g.large 2核/16GB/200GB)

```bash
# 登录(密钥你自己保管;建议挪到 ~/.ssh/ 并 chmod 400)
ssh -i "<你的密钥.pem>" ubuntu@13.59.9.97

# 送代码上盒子(Mac 上执行;盒子上永远没有 GitHub 凭证,S4)
git push ec2 <分支名>                       # Mac → 盒子裸仓库 ~/kalshi.git
ssh ... 'cd ~/hft-bot && git pull'          # 盒子工作区更新

# 盒子上的服务(替代 Mac launchd;W-A4 割接前 pipeline 单元保持"装而不启")
sudo systemctl status kalshi-pipeline       # 采集管线(W-A4 后才 enable/start)
systemctl list-timers | grep kalshi         # oom-guard 每分钟护栏(已启用)
chronyc tracking                            # 时钟偏移(应 <1ms,Amazon Time Sync)
swapon --show                               # 16GB 交换区(一体机护栏)

# 重装/修复:重跑装机脚本(幂等,不会启动采集)
cd ~/hft-bot && bash deploy/bringup_ec2.sh
```

安全清单(SSH 限 IP、出站收紧步骤):`deploy/SECURITY_CHECKLIST_EC2.md`。
三条 launchd 关键行为如何在 systemd 复现:`deploy/README_EC2.md`。
