# 对照审计:Kalshi 官方文档(隐形规范)vs 执行层现状 · 2026-07-23

> 审计(Cowork Claude)写。**纯对照,零代码改动。** 所有"建议"仅为候选,逐条等操作员批准。
> 文档侧:docs.kalshi.com 在线版,2026-07-23 拉取(rate_limits / create-order-v2 / batch /
> amend / get-orders / websockets 各页 / maintenance / fee_rounding / order_groups / demo_env)。
> 代码侧:main @ e999bf6 + 工作区。状态:**In place** = 机制已有且合规;**部分** = 有但没接全;
> **缺口** = 没有;**冲突** = 文档与我们的实测/裁决相抵,需复测或拍板;**待核** = 本轮没查透,不下结论。

## A. 签名与认证

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| A1 | 三个头:KALSHI-ACCESS-KEY / -TIMESTAMP(毫秒)/ -SIGNATURE | **In place** | src/client.cpp:272 起,毫秒时间戳 ✓ |
| A2 | 签名串 = ts+METHOD+完整路径(带 /trade-api/v2 前缀),**去掉 query 参数** | **In place** | client.cpp:279-287 与文档逐字一致 |
| A3 | RSA-PSS / SHA256 / MGF1-SHA256 / salt=digest 长度,base64 | **In place** | test_signing.cpp 全参数断言 |
| A4 | 时钟偏移容忍未写明 | **In place** | 我们自己监控 server Date 头算 skew(Response.server_date_ms),比文档要求多做了 |
| A5 | WS 握手时同三头认证 | **In place** | ws_client.cpp:30-40 build_auth_headers |

## B. 限速与 Token

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| B1 | 分层读/写桶(Basic 200/100 … Prestige 6000/8000),`GET /account/limits` 查 | **In place** | limits.cpp + request_executor,T 规则体系;服务端派生桶 ✓ |
| B2 | 非默认端点成本查 `GET /account/endpoint_costs` | **In place** | endpoint_costs 已接,P4 计划有小时级刷新(未做,已知开口) |
| B3 | 429 无 Retry-After 头,官方建议指数退避 | **In place** | backoff.hpp;代码解析 Retry-After 属多余但无害 |
| B4 | 突发规则:Advanced+ 桶容量=2 秒预算,可瞬时打 2 倍速 | **待核** | token_bucket.hpp 的容量语义没逐条核对 2 秒桶模型;保守侧安全,精确对齐待查 |
| B5 | 批量下单按条计费:N 单 = N×10 token;批量撤单 = N×2 | **冲突** | 我们 07-20 实测记的是 **flat batch billing**(commit 4977de3);现行文档明确按条。两者必有一个过时。probe_batch_cost 工具 In place,**建议重测一次定案**(只读成本探针,待批) |

## C. 下单接口(POST /portfolio/events/orders)

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| C1 | 路径 /portfolio/events/orders | **In place** | wire.hpp kCreateOrderPath ✓ |
| C2 | side = bid/ask(YES 归一化) | **In place** | order_json 的 is_bid 归一化正是文档语义 |
| C3 | price = 定点美元字符串(≤6 位小数) | **In place** | "0.4200" 四位小数 ✓ |
| C4 | count = 定点 2 位小数字符串(示例 "10.00") | **部分** | 代码发整数串 "1"(wire.hpp:181)。真单探针(bench_order/fill_test)在生产被接受过 → 服务端宽容;但偏离文档示例格式。低风险,建议对齐 "%.2f"(待批) |
| C5 | time_in_force ∈ {GTC, IOC, FOK} | **In place** | GTC + 市价→IOC;FOK 未用(不需要) |
| C6 | self_trade_prevention_type ∈ {taker_at_cross, maker} | **In place** | 固定 taker_at_cross(保护在场挂单的队列位)——对做市是正确默认 |
| C7 | post_only | **In place** | order_json 参数 ✓ |
| C8 | expiration_time(服务端 GTC 到期,秒) | **缺口** | 我们只有客户端 ttl(发单前丢弃)。**服务端到期是"客户端死了挂单自动消失"的保险丝**,做市安全网,建议阶段2 加(待批) |
| C9 | cancel_order_on_pause(暂停时自动撤单) | **缺口** | 未用。配合 I1 周四维护窗:默认挂单**留在盘上**跨暂停——做市多半想要 true。建议阶段2 加(待批) |
| C10 | reduce_only / subaccount / exchange_index(新字段) | 缺口(记录) | 均未用;reduce_only 可作缩仓保险,不急 |
| C11 | 201 响应带 fill_count/remaining_count/avg_fill_price/ts_ms | **部分** | lane 路径只看 HTTP 状态不解析体(热路径纪律正确);**阶段2 记账线程必须解析**(立即成交部分若不读,仓位账本从第一笔就错) |
| C12 | 409 = 资源冲突(重复 client_order_id) | **缺口** | rest_api/tradingd 无 409 特判。**409 是幂等对账的钩子**(见 F1) |

## D. 撤单 / 改单

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| D1 | 撤单 DELETE /portfolio/orders/{id} | **In place** | bench_order 真单验证过(wire→ack 实测 p50 也有了) |
| D2 | amend:**只有纯减量保队列位**;改价/加量 = 丢位排队尾 | **In place(以保守方式)** | 影子引擎只建模 place/cancel,重报价=撤+新挂=队尾,比真实 amend 更悲观 → 方向安全。**若将来用 amend,fill_sim 必须按此规则建模** |
| D3 | decrease-order:减量单独端点,保队列位 | 缺口(记录) | 未用。对做市是好工具:缩敞口不丢排位。候选,不急 |

## E. 批量端点

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| E1 | POST /portfolio/events/orders/batched;**允许部分成功**(逐单 error 字段) | **部分** | 客户端未实现批量下单(单发够用)。注意我们 T6 的 no-partial-send 是**客户端侧**纪律,与文档"服务端可部分成功"不矛盾,但记账要按逐单结果算 |
| E2 | 批量上限随写桶伸缩,无固定 N | 记录 | 与 B5 重测一起定案 |

## F. 幂等与对账(阶段2 生死项)

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| F1 | **GetOrders 没有 client_order_id 查询参数**(有 ticker/status/min_ts/max_ts/cursor) | **缺口(设计级)** | PLAN_PROD_V1 P8 的对账设计假设 `GET /portfolio/orders?client_order_id=` ——**该参数不存在**(P8 原文自己也标注了"先核实")。可行替代:① 歧义失败后**用同 client_order_id 重 POST,409=单已在**(最直接的幂等探测);② GetOrders 按 ticker+时间窗拉取,客户端匹配响应里的 client_order_id 字段。阶段2 设计必须改,方案待批 |
| F2 | 订单状态枚举只有 resting / canceled / executed;更老的进 /historical/orders | **待核** | 影子 FSM(order_registry.hpp)的状态集与此枚举的映射没逐一核;阶段2 对账状态机要以这三态+历史端点为准 |

## G. WS 行情(orderbook_delta)

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| G1 | wss://external-api-ws.kalshi.com,命令 {id, cmd, params},id 会话内递增唯一 | **In place** | ws_client build_subscribe/命令 id ✓ |
| G2 | snapshot 字段 yes_dollars_fp/no_dollars_fp,delta 字段 price_dollars/delta_fp(定点美元) | **In place** | gateway.cpp:68-79 与现行字段名完全一致(已在 _fp 新格式上) |
| G3 | seq 连续性自查,断则重取快照 | **In place** | sid_stream + RecoveryLadder;超时/乱序走恢复梯 |
| G4 | update_subscription get_snapshot(中途快照)的 seq 语义文档未写明 | **待实证** | 恰是 P5.3 的实验项(KALSHI_SHADOW_GETSNAP);文档不给答案,只能测 |
| G5 | 服务器每 10s 发 WS 协议层 Ping(0x9, body=heartbeat),客户端须回 Pong | **In place** | ixwebsocket 自动回 Pong(ws_client.cpp:98 注释),ping_silent 30s 看门狗兜底 |
| G6 | 错误码 1–28(6 已订阅/25 缓冲溢出/27 过频…) | **部分** | error-25 有告警处置(runbook);全枚举 1–28 未逐个映射处置。低优先 |

## H. WS 私有频道

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| H1 | `fill` 频道:成交推送(order_id/client_order_id/count_fp/yes_price_dollars/**is_taker**/ts_ms);**断线不补发**(无投递保证) | **缺口** | 未订阅。P7 计划用 REST positions 轮询(自标 ASSUMPTION)。阶段2 成交回读正解 = **WS fill 为主 + 断线后 REST /portfolio/fills 兜底对账**(文档明说无补发,所以 REST 兜底不是可选项)。记账线程消费,不上热线程(第10条门 10.3) |
| H2 | market_and_event_lifecycle 频道(暂停/结算等事件) | 缺口(记录) | 未订阅;是暂停感知(I1)的推送源。候选 |

## I. 交易所状态与维护窗

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| I1 | **每周四 3:00–5:00 AM ET 维护暂停**:能撤单、不能挂/改;挂单默认**留在盘上** | **部分** | 研究线 L2 质量门已加周四天窗(e999bf6);**执行层无暂停感知**:tradingd 不看 /exchange/status,不订 lifecycle。做市挂单跨维护窗 = 盲区暴露。配 C9 一起在阶段2 解决(待批) |
| I2 | /exchange/status、/exchange/schedule 端点 | **部分** | status 已用作预检/暖连接(client.cpp:344);未接入运行时策略 |

## J. 费用

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| J1 | 逐笔成交费**向上取整到 $0.0001**,按订单跨成交累计;净费=交易费+取整费−返点 ≥ 0 | **In place(更保守)** | shadow/fees.hpp 逐笔 ceil 到**整分**,粒度比官方粗、方向更悲观 → 影子 P&L 不会虚高。可接受;若要精确对账再对齐 $0.0001 粒度 |
| J2 | 费率变更有专门端点(get-series-fee-changes / get-event-fee-changes) | **缺口** | fees.json 是静案(有出处、fail-closed ✓),但**没有费率漂移监听**——Kalshi 改某系列费率我们不会知道,经济学悄悄变化。建议:把 fee-changes 端点接进 P4 周检(待批) |

## K. 服务器侧风控(订单组)

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| K1 | Order Groups:滚动 15 秒成交量超限 → **服务器自动撤组内全部挂单**;可手动 trigger/reset;限额 1–1,000,000 | **缺口(高价值)** | 完全未用。这是**服务器侧 kill switch**:客户端崩了、断网了它照样生效——与 RISK_KILL_FILE(客户端侧)正交互补。做市上线前把全部报价放进一个 order group 应列为阶段2/3 硬候选(待批)。request_spec.cpp 已知这些端点的 token 成本分类,只差客户端实现 |

## L. 环境与规范流程

| # | 文档要求 | 现状 | 分析 |
|---|---|---|---|
| L1 | **Demo 环境文档在线且无弃用声明**(REST external-api.demo.kalshi.co / WS 同域名;凭据与生产隔离;声明"demo 价格行为未必代表真实市场") | **冲突(需拍板)** | 仓库现行裁决:"demo 已死"(LATENCY_FACTS §5、PLAN_PROD_V1 P8 前提、修正案 A2 引擎 fail-closed 拒 demo)。**文档说 demo 存在** → F-4(阶段2 环境)可能有转机。但:①文档可能滞后;②demo 盘口质量差(官方自己警告),做市成交模型在 demo 上未必有意义。**建议:先花 10 分钟申请 demo key 实测可用性,再拍阶段2 环境的板**(探针为只读+demo,风险为零;待批) |
| L2 | openapi.yaml / asyncapi.yaml 可下载 | **缺口(已知)** | P4 的 vendored spec + 周检 drift 未做。本次对照即是手工 drift 检查——**把它自动化就是 P4** |

---

## 汇总

- **In place:16 项**——签名/限速/下单主干/WS 行情/费用内核/撤单,底子与现行文档高度一致;两处(D2、J1)是"以更保守方式合规",方向安全。
- **缺口 9 项**,按对做市的要紧程度排:F1(对账查询参数不存在,设计级)> K1(服务器侧订单组 kill switch)> H1(fill 频道+断线 REST 兜底)> C8/C9+I1(服务端到期、暂停撤单、维护窗感知,同属"客户端死了/停了怎么办")> C12(409 幂等钩子)> J2(费率漂移)> D3/H2/C10(工具性,不急)。
- **冲突 2 项**:B5 批量计费(实测 flat vs 文档按条,重测定案)、L1 demo 环境(文档活着 vs 仓库裁决已死,实测定案)。
- **待核 3 项**:B4 突发桶容量、F2 状态枚举映射、G4 快照 seq(=P5.3 实验)。

## 改动候选清单(全部等批,本轮零改动)

1. **重测批量计费**(B5):跑一次 probe_batch_cost(只读成本探针)定案 flat vs 按条。
2. **Demo 可用性探测**(L1):申请 demo key,preflight 打 demo 一次,定 F-4。
3. **阶段2 对账设计改道**(F1+C12):重 POST 同 client_order_id 靠 409 探测,替代不存在的查询参数。
4. **阶段2 成交回读**(H1+C11):WS fill 频道 + 断线 REST fills 兜底;解析 201 响应体(记账线程)。
5. **阶段2/3 服务器侧保险丝**(K1+C8+C9+I1):order group 包报价、expiration_time、cancel_order_on_pause、维护窗感知。
6. **count 格式对齐 "%.2f"**(C4):一行改动,低风险。
7. **费率漂移监听**(J2):fee-changes 端点入 P4 周检。
8. **vendored spec + drift 自动化**(L2):即 P4,本次手工对照的自动化版。
