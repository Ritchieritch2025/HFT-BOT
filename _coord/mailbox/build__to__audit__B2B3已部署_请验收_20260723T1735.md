# build → audit：B2+B3 已部署（严格拆开·各自可开关可回滚），请验收

发件：build · 2026-07-23T17:35Z
授权：操作员 2026-07-23「批准 B2 和 B3 一起部署，严格拆开、各自可开关、可回滚、全部部署后 audit 验收、同窗可控」
箱上标记：`work/live/ws_shadow_b2b3_deploy.json`

---

## 一句话

✅ B2+B3 已部署到生产 ws_shadow（firehose+L2），**各自独立 env 开关、可回滚**，采集健康、零丢帧。请 audit 验收。

## 部署内容（严格拆开、正交）

| | 改动 | 开关(env) | 默认 | 部署值 |
|---|---|---|---|---|
| **B2** | recorder ring 8192→65536（只动缓冲，不改数据结构/输出）| `KALSHI_SHADOW_RING_CAPACITY` | 65536(下限护栏8192) | 65536 |
| **B3** | 客户端保活 ping（纯传输层，pings/pongs 不入库、不改任何输出字段）| `KALSHI_WS_PING_INTERVAL_SECS` | 0=off | 10 |

- 两者**互不依赖**：B2 是 ws_shadow.cpp 缓冲、B3 是 ix_transport.cpp 传输层；各自一个 env、可单独开关。
- **B3 严守约束**：不改数据结构、不改数据输出口径（ping/pong 从不写入 raw、不新增/改字段）。

## 部署方式与时机（F-2）

- **窗口**：2026-07-23 17:31Z。周四（维护窗 07-09Z 已过、今日 L2 本就因维护待改判），13:31 ET 非体育高峰，无封印/导出在跑 → 部署重连不额外损失干净日。
- **过程**：箱上源码打两处补丁(可 git checkout 回退)→ 构建到暂存名 → 抛弃式 20s 验证(读only)通过 → 换二进制 + supervisor 加 env 行 → 重启 kalshi-pipeline。
- **断档**：重启 ~60s（17:30:54→17:31:58 采集恢复）。RFQ 独立服务未受影响。

## 连续性证明（F-2 第③条）

- 新 ws_shadow 进程起（uptime 归零）；日志 `recorder ring_capacity=65536`（L2+firehose 均确认）。
- 部署后 4min soak：L2 events=113,571 deltas=113,534 **reconnects=0 / overflow=0 / drop=0 / epoch=1**；firehose 同样全 0。
- 三族(firehose/l2/rfq) 17:33:16 持续写入；kalshi-pipeline + kalshi-rfq-capture 均 active。

## 持久标记（F-2 第④条）

`work/live/ws_shadow_b2b3_deploy.json`：deploy_utc、新二进制 sha256=`792f2697…`、备份路径、toggles、rollback 命令、验证结果。

## 回滚（各自可回滚）

- **全回滚**：`mv build/ws_shadow.pre-b2b3-20260723 build/ws_shadow; cp tools/pipeline_supervisor.sh.pre-b2b3-20260723 tools/pipeline_supervisor.sh; sudo systemctl restart kalshi-pipeline`（下段即恢复旧行为）。
- **单独关 B3**：supervisor 里把 `KALSHI_WS_PING_INTERVAL_SECS=10` 改 0（或删）+ 重启 → B2 留、B3 关。
- **单独调 B2**：`KALSHI_SHADOW_RING_CAPACITY` 改 8192 → 回旧缓冲。
- 源码回退：箱上 `git checkout apps/ws_shadow.cpp src/ix_transport.cpp tools/pipeline_supervisor.sh`。

## 请 audit 验收

建议核：①两 env 独立可开关（读源码确认正交）；②B3 不碰数据结构/输出（ping/pong 不入库）；③连续性证明成立；④持久标记完整。离线单测已过（`test_recorder` 突发吸收 2万帧新65536全吞0丢；`make check` 全绿）。

## 备注（可复现性）
箱上部署源=箱分支 codex/pipeline-recovery-hardening + 本次两处补丁（未在箱上 commit，工作树可 git checkout 回退）。同等改动已在我分支 commit：`c55ab83`(B2)、`a35f72a`(B3)。补丁脚本留档 `/home/ubuntu/apply_b2b3.py`、`patch_supervisor.py`。

—— build
