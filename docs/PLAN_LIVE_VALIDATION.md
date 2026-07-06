# PLAN_LIVE_VALIDATION — 清理 + 一键交易所检查 + 真实下单链路延迟测试（执行 prompt）

> 放入 `docs/`，按 Phase 顺序执行，每个 Phase 有验收门槛（gate），过了才进下一个。
> 沿用仓库惯例：新二进制/脚本同时进 Makefile 和 CMakeLists.txt，并在 `tools.json`
> 注册（`tools/check_registry.py` 会强制检查）；测试输出 `PASS:/FAIL:` + 末行
> `ALL PASS`；改完跑 `make && make check && make san` 全绿。
>
> 本计划**取代**旧计划文档里的阶段性硬护栏（见 Phase 1）。保留的工程规范：
> 不新增第三方依赖；价格/数量路径禁止浮点（只用 `trading::fixedpoint`）；
> 任何日志不得出现私钥/签名/auth header（`tools/check_gates.sh` 的 grep gate
> 只能加强不能削弱）；观测代码不进热路径。这些与"能不能下单"无关，是保护
> 延迟和账户安全的，不删。

## 背景（先读，别重新推导）

- 现有可复用组件：
  - `apps/preflight.cpp` — 已实现：GET /exchange/status（含 HTTP 耗时）、时钟偏移
    检查、GET /portfolio/balance 鉴权验证、GET /markets 解析 top-of-book；
    `--order TICKER` = post-only 1c YES bid 下单+撤单往返（与 tradingd 完全相同的
    ExecPayload → wire::order_json → sign_request → send 路径），打印下单毫秒数。
  - `apps/bench_order.cpp` — N 轮 place+cancel，分段计时（RSA sign / place / cancel），
    post-only 1c，永不成交，自动选市场。
  - `apps/fill_test.cpp` — IOC 真实成交测试（会留仓位，需手动平仓）。
  - `apps/ws_shadow.cpp` — 完整 WS 引擎（订阅 orderbook → SidStream 序列校验 →
    WsRecorder 落盘 NDJSON），退出时打印 completeness report（recorded/dropped、
    gap/loss/epoch markers），`KALSHI_SHADOW_XCHECK=1` 时用 REST batch snapshot
    交叉核对每个订阅市场的 book。
  - `apps/tradingd.cpp` — `TRADINGD_LATENCY_CSV=<path>` 开启全链路延迟探针
    （feed 收包 → 解析 → 策略决策 → ring → sign → send → ack 分段打点），
    `tests/analyze_full_chain_latency.py` 算 p50/p99。
  - `include/kalshi/rest_api.hpp` — `orderbook(ticker, depth)` 与
    `batch_orderbook(...)` 已有类型化封装。
- 策略 roster 为空（`src/strategies.cpp` 返回 `{}`）——Phase 3 需要一个一次性
  测试策略才能测"真实请求→下单"的完整链路（而不只是裸 REST RTT）。
- 环境开关（代码层）：`KALSHI_ENV=local_mock|prod`（demo 已被代码 fail-closed
  拒绝，不再支持；prod 另需 `KALSHI_ALLOW_PROD=1`）、`KALSHI_MODE=live` +
  `KALSHI_ALLOW_LIVE=1` 才会真实发单；不设这些时任何二进制都发不出单。这是
  显式开关，不是障碍，留着。

---

## Phase 0 — 清理垃圾文件

**目标**：仓库根目录零测试残留；确认不会再生成。

任务：
1. 删除仓库根目录下（仅根目录，`build/scratch/` 是正常 scratch 区不动）：
   `rot.ndjson` `rot.ndjson.1` … `rot.ndjson.19`、`replay.ndjson`、
   `replay6.ndjson`、`raw_bin.ndjson`、`raw_utf8.ndjson`、`rec_of.ndjson`、
   `rec_rt.ndjson`、`sid.ndjson`、`trunc.ndjson`、`wouldbe.ndjson`、
   `fuzz_reader.ndjson`。
2. 删除各目录游离的 `.DS_Store`（根、`docs/`）。
3. 不要删：`tests/` 的 mock（离线测试依赖）、`work/`（gitignored 运行数据）、
   `build/`（gitignored）。`.gitignore` 已含 `*.ndjson` / `*.ndjson.*`，无需改。
4. 验证不再生成：跑一遍 `make check` 和 `./tests/run_pipeline.sh`，结束后
   `ls *.ndjson*` 必须为空（P0 的 scratch-dir 修复已落地，这些是历史残留；
   若有任何 suite 仍往根目录写，修它的调用参数而不是加忽略）。

Gate：根目录零 `*.ndjson*`；全套离线测试跑完后依然为零；`git status` 干净
（除本计划的有意变更）。

## Phase 1 — 移除文档硬护栏

**目标**：三份计划文档不再冻结下单路径、不再强制 demo-only；工程规范保留。

任务（纯文本编辑，不碰任何代码）：
1. `docs/PLAN_PROD_V1.md` §Hard guardrails：
   - 删除 #1（P8 前冻结 order transmission）、#8（P10 前只准 demo）。
   - #2（ops console 不能下单）改为一句设计说明："console 保持只读，下单一律
     走 CLI 工具"——`dashboard_server.py` 的 server-side 403 代码不动。
   - 保留 #3–#7、#9、#10（依赖、fixedpoint、测试全绿、双构建系统、不记密钥、
     观测不进热路径），重新编号。
   - 同步修正正文里对已删护栏的引用（如 P8 的 "guardrail 1 lifts here"、
     P6 的 "submit path is untouched (guardrail 1)" 等，改成普通描述）。
2. `docs/PLAN_WS_V2.md` §Hard guardrails：删除 #1（永不碰 order transmission）、
   #2（Phase 8 前只准 demo）；保留 #3–#7，重新编号，修正正文引用。
3. `docs/PLAN_TOKEN_RULES.md` §Hard rules：#1 删去 "No new transmission paths"
   一句（保留 "tier upgrade 不自动执行、不进热路径"）；其余规则保留。
4. `README.md`：如有 "orders frozen/demo until …" 之类表述一并更新。

Gate：三份文档 grep 不到 "frozen until P8" / "until Phase 8" / "until the canary
phase" 的护栏表述；`make check` 依然全绿（check_gates.sh 的密钥/host gate 不受
影响）。

## Phase 2 — 一键交易所检查工具 `tools/exchange_check.sh`

**目标**：一条命令跑完 REST 多项状态 + orderbook 细节 + WS orderbook 消息落盘
验证，输出统一 PASS/FAIL 汇总。

设计：shell 脚本编排现有二进制 + 两个小增量，不写新的大程序。

任务：
1. **preflight 增强**（`apps/preflight.cpp`，只加只读检查）：
   - 新增 `--orderbook TICKER[,TICKER…]`：调 `RestApi::orderbook(ticker)`（走
     RequestExecutor），打印每侧前 5 档（价格、数量，fixedpoint 格式化）、
     spread、双侧档位总数；空 book / 解析失败 = FAIL。
   - 新增默认检查两项：GET `/account/limits`（打印 usage_tier、read/write
     refill_rate 与 bucket_capacity），GET `/account/endpoint_costs`（打印
     default_cost + 非默认条目数）。二者失败 = FAIL（说明鉴权或 schema 有变）。
   - 沿用现有 `result()` PASS/FAIL 风格与退出码语义。
2. **WS 捕获结构校验器** `tools/verify_ws_capture.py`（stdlib-only）：
   输入 ws_shadow 的 capture NDJSON，校验并打印报告——
   - 每个订阅 ticker 至少 1 条 `orderbook_snapshot`；
   - 每个 sid 的 seq 严格连续（容忍带 marker 说明的空洞）；
   - 统计 snapshot/delta/ticker 条数、gap/loss/epoch_change marker 数
     （目标 0，非 0 列明细）；
   - 末条消息时间覆盖到会话尾部（说明没有中途断流）；
   - 退出码 0/1，输出 `PASS:/FAIL:` + `ALL PASS`。
3. **编排脚本** `tools/exchange_check.sh`：
   ```
   用法: tools/exchange_check.sh [--env prod] [--tickers T1,T2] [--ws-seconds 30]
   ```
   顺序执行，任何一步非零即整体 FAIL，最后打印汇总表：
   a. `./build/preflight --orderbook <tickers>`（含 status/skew/balance/markets/
      limits/costs/orderbook 全套只读检查）；
   b. `KALSHI_MODE=data_collect KALSHI_SHADOW_SECONDS=<n> KALSHI_SHADOW_CAPTURE=work/exchange_check_capture.ndjson KALSHI_SHADOW_XCHECK=1 KALSHI_WS_TICKERS=<tickers> ./build/ws_shadow`
      （WS 订阅→收 snapshot+delta→落盘→REST 交叉核对）；
   c. `python3 tools/verify_ws_capture.py work/exchange_check_capture.ndjson`；
   d. 汇总：`EXCHANGE CHECK PASS|FAIL`。
   - `--env prod` 时导出 `KALSHI_ENV=prod KALSHI_ALLOW_PROD=1`（全程只读，
     ws_shadow 本身拒绝 live mode，安全）。
   - 未给 tickers 时从 `GET /markets?status=open&limit=3` 自动选。
4. 注册：`tools.json` 新增 `exchange_check`（kind=check, safety=network_read）
   与 `verify_ws_capture`（kind=check, safety=pure）；`check_registry.py` 的
   期望清单同步。Makefile/CMake 无新二进制（preflight 已存在）。

新测试：`tests/test_verify_ws_capture.py`（对固定 fixture：正常流 PASS、
snapshot 缺失 FAIL、seq 空洞 FAIL、loss marker 计数正确）；preflight 的
`--orderbook` 解析逻辑若抽成函数则加进 `test_rest_api` 用 mock_rest 覆盖
（mock 已有 orderbook 端点则复用，没有则给 mock_rest.py 加一个）。

Gate：prod 环境跑 `tools/exchange_check.sh --env prod` 一条命令全绿（全程只读，
demo 环境已不再支持）；`make check` 全绿（含新注册项校验）。

## Phase 3 — 真实下单链路延迟 + 下单时刻 orderbook 细节

**目标**：拿到两组数：(A) 裸下单往返延迟（sign/place/cancel 分段），
(B) 从市场数据事件到订单 ack 的完整链路延迟（经过 tradingd 引擎），并在下单
前后留存 orderbook 快照。

任务：
1. **一次性测试策略** `once_probe`（`src/strategies.cpp` 注册，固定 slot id，
   如 29）：
   - 行为：收到第一条匹配 `PROBE_TICKER` 的 MarketEvent 时发出一个
     post-only 1c YES bid（1 张）的 ExecPayload，之后永久静默；
   - 参数全部走 env：`PROBE_TICKER`（必填，缺失=启动失败 fail closed）、
     `PROBE_PRICE_CENTS`（默认 1）、`PROBE_COUNT`（默认 1）；
   - roster 机制：`STRATEGIES=once_probe` 环境变量启用（未设=空 roster，
     现状不变；未知名字=启动失败）。
2. **下单前后 orderbook 留存**：`tools/order_latency_test.sh` 编排——
   a. `./build/preflight --orderbook $TICKER` → 存 `work/ob_before.txt`；
   b. 跑测试（模式二选一，见下）；
   c. 再取一次 orderbook → `work/ob_after.txt`；打印 before/after 前 5 档对比。
3. **模式一（裸 RTT）**：`./build/bench_order 5 $TICKER` —— 5 轮 place+cancel，
   输出 sign/place/cancel 各段 p50/max。post-only 1c，不会成交。
4. **模式二（完整链路）**：
   ```
   KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=live KALSHI_ALLOW_LIVE=1 \
   STRATEGIES=once_probe PROBE_TICKER=$TICKER \
   TRADINGD_LATENCY_CSV=work/full_chain.csv \
   timeout 30 ./build/tradingd --poll 250
   ```
   （demo 环境已不再支持，只能对 prod 跑；这会下**真实**订单——post-only 1c
   bid 最坏被动成交每张 1 美分，脚本当轮撤单，仍需人工确认无 resting 残留。）
   之后 `python3 tests/analyze_full_chain_latency.py work/full_chain.csv`
   打印 feed→parse→decision→ring→sign→send→ack 各段 p50/p99；订单本身由脚本
   用返回的 order_id 撤掉（或 once_probe 发单后自带 5s 延迟撤单逻辑——实现
   选一种，倾向脚本撤，策略保持纯粹）。
5. **执行阶梯**（脚本 `--env` 参数控制，只能 prod）：
   a. demo 环境已不再支持，没有免费/无风险环境；任何真实下单都直接对 prod，
      会产生**真实**订单与真实成交风险，先在 mock（`local_mock`）上把逻辑跑通；
   b. prod：`KALSHI_ENV=prod KALSHI_ALLOW_PROD=1`。post-only 1c bid
      最坏情况成本 = 每张 1 美分（若真被动成交），bench_order/preflight 均
      当轮撤单。若要测真实成交与仓位回读，用
      `./build/fill_test $TICKER $PRICE`——注意它不平仓，测完手动平。
6. 注册：`tools.json` 新增 `order_latency_test`（kind=probe,
   safety=live_order——console 永不可跑，CLI 显式跑）；`once_probe` 在
   ARCHITECTURE.md 的策略段补一行说明。

新测试（全部离线，打 mock）：
- `once_probe` 单测：首个匹配事件恰好发 1 单、之后静默、ticker 不匹配不发、
  缺 `PROBE_TICKER` 启动抛异常；
- roster 测试：`STRATEGIES=once_probe` 装载成功、未知名失败、未设=空；
- tradingd 集成（mock_server）:once_probe 端到端恰好 1 个 POST 到达 mock，
  latency CSV 生成且各段单调递增。

Gate：prod 上 `tools/order_latency_test.sh --mode chain`（真实订单）输出完整分段延迟表 +
before/after orderbook 对比，交易所侧订单已撤（`GET /portfolio/orders` 无
resting 残留);全套 `make && make check && make san && make tsan` 绿。

---

## 官方规范核对结果（2026-07-05 对照 docs.kalshi.com 逐项验证）

**已验证正确（无需改动）**：
- 下单 `POST /portfolio/events/orders`（Create Order V2，2026-04-22 上线；旧
  `/portfolio/orders` 计划弃用）；请求体必填字段 ticker / client_order_id /
  side(bid|ask) / count(定点字符串) / price(定点美元字符串) /
  time_in_force(good_till_canceled|immediate_or_cancel|fill_or_kill) /
  self_trade_prevention_type(taker_at_cross|maker)——`wire::order_json` 全部命中；
  响应顶层 `order_id`/`ts_ms` 与 preflight 解析一致。
- 撤单 `DELETE /portfolio/events/orders/{order_id}` ✓。
- `GET /exchange/status`、`/portfolio/balance`、`/markets?status=open&limit=`、
  `/markets/{ticker}/orderbook?depth=`（响应 `orderbook_fp` →
  `yes_dollars`/`no_dollars` 二元数组，与 rest_api.cpp 解析一致）、
  `/markets/orderbooks`（batch）、`/account/limits`、`/account/endpoint_costs` ✓。
- `/account/limits` 嵌套 schema（`read`/`write` × `refill_rate`/`bucket_capacity`）
  为 2026-04-30 起的官方行为（changelog 确认）；文档页面内嵌 spec（v3.14 扁平版）
  是过期快照，以 changelog/live yaml 为准——仓库押注正确。
- 签名（ts_ms + METHOD + 不含 query 的路径，RSA-PSS，KALSHI-ACCESS-* 头）、
  WS URL 与签名路径 `/trade-api/ws/v2`、subscribe 命令结构
  `{id,cmd,params:{channels,market_tickers}}`、`orderbook_snapshot`
  (`yes_dollars_fp`/`no_dollars_fp`)、`orderbook_delta`
  (`price_dollars`/`delta_fp`/`side`/`ts_ms`/`client_order_id`)、sid/seq 语义、
  `update_subscription` 的 `get_snapshot`、错误码 1–22+25、429 无 Retry-After、
  批量按条计费（25×10 / 25×2）、tier 预算表、写桶 2 秒突发（Basic 1 秒）✓。
- 主机名 allowlist（external-api.kalshi.com / api.elections.kalshi.com /
  external-api-ws.*）✓。（原 demo 主机 external-api.demo.kalshi.co /
  demo-api.kalshi.co 已随 demo 环境下线从 allowlist 移除，不再支持。）

**补充核对（2026-07-05 二次修正）**：
- `POST /account/api_usage_level/upgrade` **确认存在**（官方页面
  upgrade-account-api-usage-level,spec v3.23.0）:30 tokens Write 桶、
  201=永久 Advanced grant(仅 Predictions 实例)、403="最近 100 笔 Predictions
  订单无 API 创建"——与 PLAN_TOKEN_RULES F10 及 `apps/account_upgrade.cpp`
  逐项一致。该页明确 "Use Get Account API Limits to inspect the resulting
  usage tier and **grants**",即 `grants` 字段同样有官方出处;`src/limits.cpp`
  按 v3.23.0 把它当必填是符合最新 spec 的。此前基于 llms.txt 索引与 v3.14
  页面内嵌快照得出的"端点不存在/grants 无出处"结论**作废**——教训与仓库
  F12 一致:页面索引和内嵌 spec 会滞后,以最高版本 spec embed + changelog
  为准,存疑处以 prod 实测定案（demo 环境已不再支持）。

**仅剩一处待定（并入 Phase 2 验证,不预先改代码）**：
1. **`use_yes_price`**:websocket-connection 页的 AsyncAPI embed 订阅参数列表
   未包含它(channels、market_ticker(s)、market_id(s)、send_initial_snapshot、
   skip_ticker_ack、shard_factor、shard_key),changelog 亦无记录;但鉴于
   embed 可能滞后(见上),不下结论。→ Phase 2 的 ws_shadow prod 只读冒烟即可
   定案(demo 环境已不再支持):订阅若返回 error 11(invalid parameter)则移除该
   参数并更新协议文档 I5;若正常 subscribed 则保留现状并在协议文档标注"embed
   未列出,prod 实测服务器接受"。

另:`wire.hpp` 注释"fixed-point migration removed POST /portfolio/orders"措辞
偏早——官方为"不早于 2026-05-21 弃用";V2 路径选择正确,注释顺手修正即可。

## 验收总表

| 项 | 证据 |
|---|---|
| 垃圾清理 | 根目录零 `*.ndjson*`，run_pipeline 后依然为零 |
| 护栏移除 | 三份 PLAN 文档无冻结/demo-only 条款，工程规范保留 |
| 一键检查 | `exchange_check.sh --env prod`（只读）单命令全绿：status/skew/balance/limits/costs/markets/orderbook 5 档/WS 捕获+落盘校验+REST 交叉核对 |
| 下单链路 | bench_order 分段 RTT + tradingd 全链路 p50/p99 CSV 分析 + 下单前后 orderbook 对比,只能对 prod 跑（demo 已不再支持，为真实订单） |

## 执行顺序与会话切分

Phase 0+1 一个会话（纯删除+文本，半小时级）；Phase 2 一个会话；Phase 3 一个
会话（策略+脚本+测试）。每个会话开始先 `./tests/run_pipeline.sh` 确认基线绿，
结束时 gate 过 + 双构建系统同步。
