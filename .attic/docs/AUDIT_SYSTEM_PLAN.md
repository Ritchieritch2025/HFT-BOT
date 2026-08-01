# 审计报告：Dashboard 改动核查 + HFT_ENGINE_SYSTEM_PLAN 审计

日期：2026-07-05。范围：dashboard 新改动（Lifecycle / Core Tests / Live Truth）
的代码核查,以及 `HFT_ENGINE_SYSTEM_PLAN.md`(lifecycle-first 方案)对照仓库
现状的可行性审计。

## 一、Dashboard 改动核查结果（全部属实）

| 声明 | 核查结果 |
|---|---|
| Lifecycle 一键检查(API Updates / Spec Alignment / Connection / Core Tests / Data Pipeline / Strategy Shadow / Live Gate) | ✅ `tools/lifecycle_check.py` 是唯一 readiness 来源;dashboard 读 `work/lifecycle_status.json` |
| 未开 network 时 Exchange/API 显示 Skipped | ✅ `--allow-network` 未启用时标记 skipped,不算失败 |
| 不下单、不启动 live | ✅ 计划清单只含 pure 检查 + 只读 network_read;`live_order` 服务端 403 不变 |
| Core Test Status 只显示核心 pure/offline 测试 | ✅ `isCoreTest()`:kind∈{test,check} ∧ safety∈{pure,offline},排除 `run_*` wrapper;account_info/probe_batch_cost 因 kind=probe 天然排除 |
| Tests tab 同步清理,probe/bench 留在 Tools | ✅ 两个网格共用同一过滤器 |
| 后端新增 `POST /api/init` | ✅ 兼容入口,返回 lifecycle JSON;真实编排由 `tools/lifecycle_check.py` 执行 |
| 清除合成日志 | ✅ `work/metrics.ndjson` 已清空;Live Truth 面板区分 real/synthetic/stale;`docs/CURRENT_STATE.md` 内容准确 |

测试:`python3 tools/run_tests.py --tool test_console --json` 通过:24/24,
`ALL PASS`。普通沙盒无法绑定 localhost 时需提升权限运行该测试。

遗留操作项(更新于 2026-07-05 第三轮):
- [x] 已确认无 Git 进程并删除 stale `.git/index.lock`。
- [x] `run_tool` 异常兜底已修:TimeoutExpired/FileNotFoundError/OSError →
      `{"status":"error","reason":"timeout|binary missing|exec failed"}`,
      dashboard 不再因坏二进制断连
- [x] `test_console.py` 已覆盖 `/api/init` 与 `/api/lifecycle/run`:
      合法 JSON 结构、永无 live_order、未开 network 时 preflight/account_info
      为 skipped、坏工具返回 JSON 而非断连。

## 当前生命周期真实状态(2026-07-05)

| Stage | 状态 | 依据 |
|---|---|---|
| 1 Connection/Exchange | 可运行(需 --allow-network + 凭据) | /api/init 已接 preflight/account_info |
| 2 Core Tests | 可运行 | tools.json 驱动,dashboard 已过滤核心集 |
| 3 Data Pipeline | **not_started/blocked** | 引擎 feed 遥测(P2.3)未实现,WS 未接入 tradingd(P6) |
| 4 Strategy Shadow | **not_started/blocked** | 策略 roster 为空,shadow PnL 成交模型(P9)未建 |
| 5 Live Execution | **blocked** | 风控/kill switch/reconcile(P7/P8)未建,预期如此 |

## 二、HFT_ENGINE_SYSTEM_PLAN 审计结论

**总体:方向正确,采纳。** 五段生命周期(Connection → Core Tests → Data
Pipeline → Strategy Shadow → Live Execution)与仓库既有架构一致:
"pipeline 不知道 live/shadow、strategy 只产意图、execution engine 负责路由"
即现有 bus 设计;Stage 5 门槛清单实质是把已删除的文档护栏改写为
**运行时状态机** —— 这是护栏正确的存在位置(代码检查,而非文档冻结)。

### 采纳前必须修正的 8 处

1. ~~`test_console` 未注册~~ **勘误(2026-07-05)**:`test_console` **已注册**
   (tools.json 第 51/53 条,kind=test, safety=offline)——原结论源于审计时
   `head -50` 截断了注册表输出,又一次"否定性结论未取全证据"(参见
   `KALSHI_API_VERIFICATION_INSTRUCTIONS.md` 规则 1,同样适用于查本仓库)。
   修正后的建议:**Stage 2 核心测试清单不要在计划文档里手写,由 tools.json
   自动驱动**(kind∈{test,check} ∧ safety∈{pure,offline} 即核心测试,与
   dashboard `isCoreTest()` 同一过滤器)——注册表是唯一事实源,手抄清单
   必然漂移。test_console 自嵌 dashboard 实例的并发/端口问题仍需注意。
2. **Stage 2 清单缺 live 关键测试**:`test_fixedpoint`(价格定点数学——
   价格路径正确性的核心)、`test_account_limits`、`test_rest_api`、
   `test_integration`、`test_resp`、`test_shadow`。补入分组,或逐项写明
   排除理由;不允许隐式遗漏(与 P1 注册表原则一致)。
3. **Stage 1 "WebSocket Availability" 无需新建检查器**:复用
   `ws_shadow` + `KALSHI_SHADOW_SECONDS=5`(已注册 network_read),
   判据=连接成功、鉴权通过、收到首个 orderbook_snapshot。
4. **Stage 3 硬依赖 feed 遥测事件**:tradingd 目前只发 system/order 事件,
   `feed` NDJSON(PLAN_PROD_V1 P2.3)未落地 —— 不先实现它,dashboard
   无从得知 pipeline 的 Not Started/Running/Failed。实现顺序上 P2.3 前置。
5. **Stage 4 依赖未建组件**:"Mock Fill Simulated" 需要 shadow PnL 成交
   模型(P9);策略 roster 当前为空。此层近期只能显示 Not Started,
   属预期而非缺陷,计划中应标注。
6. **消除重复的检查链**:`/api/lifecycle/run` 是刚建的 `/api/init` 的
   超集 —— 吸收合并,不并存两套;同时与 `docs/PLAN_LIVE_VALIDATION.md`
   Phase 2 的 exchange_check 高度重合,统一由 `tools/lifecycle_check.py`
   作唯一编排层,exchange 检查成为其 Stage 1 实现。
7. **`work/lifecycle_status.json` 需要明确 schema**,否则"前层未过、
   后层不得 ready"只是口头约定。建议每层:
   `{stage, status: pass|fail|skipped|not_started|blocked, blocking_reason,
   depends_on: [...], checked_at_ms, evidence_log}`。
   全量核心测试耗时几十秒,`POST /api/lifecycle/run` 应异步执行并复用
   现有 SSE run_stream 流式输出。
8. **house rules 补齐**:`lifecycle_check.py` 进 `tools.json` +
   `check_registry.py` 期望清单(kind=check, safety=network_read,
   离线部分 pure);Stage 1 头部展示 `resolve_runtime()` 的 env/mode
   (preflight 已打印,透传即可);dashboard 保持只读——lifecycle 文件
   全部由 lifecycle_check.py 写入,dashboard 只 tail。

### 依赖顺序(实施用)

```
P2.3 feed 遥测事件(引擎侧)          ← Stage 3 的前置
lifecycle_check.py + status schema    ← Stage 1/2 立即可做(复用现有工具)
/api/lifecycle + 首页 lifecycle-first ← 吸收 /api/init
once_probe 测试策略 + shadow 记录     ← Stage 4 点亮(PLAN_LIVE_VALIDATION P3)
风控 + kill switch + reconcile(P7/P8)← Stage 5 解锁的唯一途径
```

## 三、v2 计划审计(2026-07-05:新增 API Updates / Spec Alignment / Watcher / 热路径隔离)

**结论:采纳。**七段生命周期(API Updates → Spec Alignment → Connection →
Core Tests → Data Pipeline → Strategy Shadow → Live Gate)与"热路径零侵入"
原则均与仓库既有架构(ARCHITECTURE.md 观测护栏、冷遥测线程模式)一致。

**任务 1–4 状态:已完成**(本次会话),计划文本部分过时:
task 2(run_tool 结构化 JSON)✅、task 3(/api/init 四个测试)✅、
task 4(审计文档勘误)✅、task 1(index.lock 清理)✅。

**逐项审计意见:**

- **Task 5/6(lifecycle schema + checker)**:批准。补两条硬语义:
  (a) `fail` 阻塞下游,`skipped` **不阻塞**——否则离线模式下 Stage 1/2
  skipped 会把整个生命周期卡死;(b) lifecycle_check.py 进 tools.json +
  check_registry 期望清单;`POST /api/lifecycle/run` 调用该工具并写状态;
  `/api/init` 保留为兼容入口,同样返回 lifecycle JSON,不再维护第二套
  readiness 逻辑。
- **Task 7(Spec Alignment)**:批准,三处修正:
  (a) **存放位置冲突**——PLAN_PROD_V1/PLAN_WS_V2/kalshi_ws_protocol.md
  三处既有文档写的是 `third_party/kalshi_specs/`,新计划写
  `docs/vendor/kalshi/latest/`。二选一(建议新位置),同步改掉三处旧引用,
  不允许两个 vendored spec 目录并存。
  (b) 快照分两层:`baseline/`(已审阅、提交)+ `latest/`(抓取工作区),
  drift = latest vs baseline;否则每次抓取都产生提交噪音。
  (c) 对齐检查以 `KALSHI_API_VERIFICATION_INSTRUCTIONS.md` 的基线断言为主
  (关键路径/字段/版本号),全量 yaml diff 只作 hash 变化信号——spec
  版本号数天一变,全量 diff 会淹没真信号。
- **Task 8(Update Watcher)**:批准。RSS URL 已实测存在
  (`changelog/rss.xml`,含 guid/category/pubDate,适合去重)。
  **关键发现:RSS 滞后于 changelog 页面**(实测 lastBuildDate 2026-05-19,
  而页面有更新的条目)——watcher 必须同时 hash changelog 页面 + llms.txt +
  两个 yaml(计划已含 ✓),不能只依赖 RSS。轮询礼仪:间隔 ≥15min +
  条件请求/hash 比对。watcher 永不参与引擎启动依赖(ops 工具 fail-open,
  交易门槛 fail-closed,方向相反,不可混用)。
  实测抓到的真实漂移案例(watcher 价值证明):changelog 2026-05-15 条目
  称 V2 cancel/amend 响应数量字段为 `reduced_by_centicount`/
  `remaining_centicount`,而 cancel-order-v2 页面内嵌 spec 写 `reduced_by`
  (FixedPointCount)——字段命名冲突,以 prod 只读实测定案(demo 环境已不再支持);当前代码只查 cancel
  的 HTTP 状态,不受影响。
- **Task 9(UI)**:批准。补充:skipped 与 fail 必须视觉区分;replay 标记
  依赖 recorder marker 记录透传到前端(格式已有,接线未做)。
- **Task 10(热路径隔离)**:与既有 guardrail 10 完全一致,零冲突。所有新
  组件均为独立进程;唯一引擎侧改动是 P2.3 feed 遥测,走既有"固定结构体→
  有界环→遥测线程"模式。
- **顺序语义**:Stage 1/2 是网络操作,离线时 skipped——不得阻塞 3–7
  (同 Task 5 修正 a)。

## 四、实现审查(2026-07-05:lifecycle_check / kalshi_spec_sync / watcher / dashboard 接线)

**核验通过的部分:**
- 三个新工具落盘、可执行、py_compile 通过;tools.json 注册 56 项,
  check_registry 绿;`test_console` 已同步 lifecycle 形状并通过 24/24。
- 递归防护正确:`runnable_test_set()` 与前端 `isCoreTest()` 均排除
  lifecycle_check,--run-core-tests 不会跑到自己。
- 依赖语义正确:`dep.status ∉ {pass, skipped} → blocked`——skipped 不阻塞,
  离线模式下 Stage 1–3 跳过、Core Tests 照常可跑(实测验证)。
- spec_sync 为 report-only、原子写 JSON(os.replace)、latest/baseline
  两层快照、manifest 含 hash+时间——符合审计要求。
- 单写者原则保持:lifecycle/spec/updates 文件由工具写,dashboard 只读。

**已修正的后续问题:**
1. changelog/RSS/llms hash 变化只作为 update-watcher 信息;只有
   openapi/asyncapi hash 变化会让 Spec Alignment fail。
2. WS 对齐检查已拆分 `repo_tokens` 与 `official_tokens`,避免 YAML 官方
   spec 被 JSON 字面量误判。
3. `tools.json` 的 `kalshi_spec_sync`/`kalshi_update_watcher` 命令已内置
   `--allow-network`/`--once`;`check_registry.py` 校验 script command 的
   executable token。
4. `kalshi_spec_sync.py --accept-baseline` 已实现;本轮已用官方 latest
   快照建立 `docs/vendor/kalshi/baseline/` 初始基线。
5. 旧 `third_party/kalshi_specs/` 引用已改为 `docs/vendor/kalshi/`。

**运维注意:**
- lifecycle_check 整体退出码=非 pass 即 1(系统未全绿前
   永远"失败")——语义正确,但绝不能作为阻塞项接进 run_pipeline.sh/CI;
   `/api/lifecycle/run` 目前同步阻塞(600s 上限)且无并发锁,两个并发
   POST 会同时写状态文件——v2 改异步+SSE+单飞锁。

## 五、GREED_COMPAT_WAREHOUSE_SCHEMA 验证(2026-07-05)

**结论:方案正确,采纳;6 个决策/修正点先定,再动工。**

**对照已验证的 Kalshi 官方 API,以下映射全部正确:**
- orderbook 存 YES/NO 双 bid 原始形态、不在存储层转 ask ✓(官方 book 只有
  yes/no bids);`yes_ask = 1.00 − best NO bid` ✓;
- 数量用 NUMERIC/定点字符串 ✓(FixedPointCount 支持 0.01 碎股);
- trades 以 trade_id 去重 ✓、yes/no_price_dollars 字段存在 ✓;
- `yes_sub_title`(metadata_updated)、settlement_value、
  price_level_structure 均有官方出处 ✓;
- "raw 捕获 → converter → 公共表"与仓库架构一致(NDJSON 捕获已有,
  load_db.py 是先例),入库全在冷路径,热路径零接触 ✓;
- 内部元数据独立成 warehouse_* 表 ✓;列名断言测试 ✓。

**6 个决策点(2026-07-05 已落地到 GREED_COMPAT_SCHEMA):**
1. **存储引擎已定为 DuckDB + day-partitioned Parquet**。`load_db.py`
   的 SQLite 路径保留为旧的 run-import 研究工具,但 Greed-compatible
   warehouse target 不再以 SQLite 起步。依赖规则已修订:只允许离线研究
   warehouse tooling 使用 DuckDB/Parquet,禁止热路径和 live-order 代码依赖。
2. **taker_side 取数源**:legacy `taker_side` 官方已弃用(2026-05-28 后
   随时可移除,今天已是 07-05)——列名保留 Greed 形态,但取值必须来自
   `taker_outcome_side`(fallback legacy)。
3. **规模数学**:全市场 1 Hz L1 = 数千市场 × 86400 行/天,最坏数亿行/天。
   采样规则固定为"变化才写 + 每市场最小间隔 1s",输出按天分区。
4. **各表主键/幂等未定义**:trades=trade_id ✓ 已写;补
   orderbooks_l1=(market_ticker, exchange_ts)、
   orderbooks_full=(market_ticker, recorded_at),重放/重灌沿用
   load_db.py 的 per-run replace 模式。
5. **exchange_ts 回落 wall clock 丢失来源信息**:Greed 兼容禁止加公共列,
   把 per-row 时间来源标记放进 warehouse_* 内部表,或文档明确接受歧义。
6. **"Greed 兼容"本身不可验证**:仓库里没有 Greed schema 的参照物。
   把 Greed 公开 schema 快照钉进 `docs/vendor/greed/`(与 Kalshi spec
   同模式),列名测试对着快照断言——否则"兼容"必然漂移。
   另:dollar_volume/dollar_open_interest 为 INT64,单位(分/元)需对照
   Greed 参照确认。

### 与既有计划的关系

- 本方案**取代** `PLAN_LIVE_VALIDATION.md` Phase 2 中"exchange_check.sh
  编排脚本"的交付形态(检查项全部保留,搬进 lifecycle_check.py Stage 1)。
- `PLAN_LIVE_VALIDATION.md` Phase 0(清垃圾)、Phase 1(文档护栏移除)、
  Phase 3(下单链路延迟测试)不受影响,照常执行。
- Stage 5 的门槛清单与 PLAN_PROD_V1 P7/P8 的验收标准一一对应,完成
  P7/P8 即自然解锁,无需额外工作。
