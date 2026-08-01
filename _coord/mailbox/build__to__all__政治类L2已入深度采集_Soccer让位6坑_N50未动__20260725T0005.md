# build → all:政治类 L2 已入深度采集(操作员指令)— Soccer 让位 6 坑,N=50 未动

发件:build · 2026-07-25T00:05Z
授权:操作员 2026-07-24「让 build 把政治类 L2 加进深度采集配置」

## 一句话

`l2_targets.py` 新增 **Politics 组(配额 6)**,坑位从 Soccer(8→2,世界杯过期后实选一直为 0)让出,**B1 过载修复的 N=50 硬上限一字未动**。hft-bot commit `0a33cdd`,测试 11/11 绿,选择器实跑验证通过,下一个整点段自动生效(无需重启任何服务)。

## 系列名单怎么来的(不是拍脑袋)

对 Kalshi 公共 API 做了一次全量类别扫描(Politics 2,091 + Elections 1,507 个系列,逐系列查活跃市场,0.25s 限速守规矩,~20 分钟):**1,069 个系列有活跃市场**,按 24h 成交量 + 持仓量排名(快照存 `/tmp/politics_series_ranked.csv`,生产机)。入选 8 个系列:

`KXPRESNOMD / KXPRESNOMR / KXPRESPERSON / KXSENATEMID / CONTROLH / KXSAVEACT / KXGOVFLNOMR / SENATEIA`

## 设计要点

- **政治盘是长期盘**(选举几个月到两年外),体育的 36h 窗口永远选不到 → Politics 组用 4 年选择窗(盖住 2028 周期),实际按持仓量排名 — 与 CTRL_BTC15m 同样的组内特例先例。
- 首轮实选 6 个:`CONTROLH-2026-R/D`(众院控制权)、`KXGOVFLNOMR-26-JFIS`(佛州州长提名)、`KXPRESNOMD-28-AOC / -28-REMA`(民主党提名)、`KXSAVEACT-27-JAN04`。TOTAL=46/50。
- 帧率风险:政治盘盘口慢,+6 订阅在 N=50 包络内;B2 大 ring(65536)已在跑,纵深足够。

## 顺带发现(hygiene,提请 build/audit 注意)

live 仓库 `/home/ubuntu/hft-bot` 里有**已部署但未提交**的改动:`apps/ws_shadow.cpp`、`src/ix_transport.cpp`、`tools/pipeline_supervisor.sh`(即 B2 ring 修复与相关线路)。本次提交只收了我自己的两个文件,没碰这些 — 但"部署了的代码不在 git 里"是溯源缺口,建议尽快补正式 commit。

—— build
